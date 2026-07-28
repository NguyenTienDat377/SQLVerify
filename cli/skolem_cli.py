"""Skolem CLI — a thin HTTP client.

Formally proves whether two SQL SELECT queries are semantically equivalent, or
prints a concrete counterexample database where they diverge.

It holds no solver: it forwards to a hosted Skolem's POST /api/verify/text,
authenticated with a per-user `skm_` API key. Only dependency is httpx — never
import `core/` here, or `pipx install skolem` would drag in Z3.

    skolem verify --ddl schema.sql --v1 before.sql --v2 after.sql
"""
import argparse
import fnmatch
import glob
import json
import os
import re
import subprocess
import sys

import httpx

__version__ = "0.1.0"

# Where the hosted Skolem lives. Override for local dev (http://localhost:8000).
DEFAULT_URL = "https://skolem.dev"

# Identifies CLI traffic to the server. /api/verify/text is shared by the CLI,
# the MCP proxy and raw pipeline clients; this User-Agent is what lets
# api/verify.py:_resolve_surface tell them apart. Analytics-only — it never
# affects auth, quota, or the verdict.
_USER_AGENT = f"skolem-cli/{__version__}"

# Must sit above Skolem's CI solve ceiling (120s) plus transport slack.
_HTTP_TIMEOUT_S = 130.0

# Exit codes. A verification verdict you asked to fail on (1) is kept distinct
# from the CLI's own failure (2) so a lapsed API key or a network blip can never
# be mistaken for a broken query.
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_CLI_ERROR = 2

# Mirrors of the server's limits (api/verify.py). Checked client-side so a typo
# costs no round trip; the server remains the authority.
MAX_BOUND = 6
DEFAULT_BOUND = 3
DEFAULT_TIMEOUT_MS = 60_000
MAX_TIMEOUT_MS = 120_000

ALL_STATUSES = ("equivalent", "divergent", "unknown", "error")

# `diff` only: the schema itself changed between --base and the working tree,
# so no pair in this run has a trustworthy verdict (the engine needs one
# schema for both queries). Not a VerifyResponse status — a --fail-on-only
# pseudo-status callers can opt into.
DDL_CHANGED = "ddl-changed"

# Default gate policy: only a proven divergence fails the run. `unknown` and
# `error` warn loudly but pass, because a CI check that goes red the first time
# someone writes a CTE gets uninstalled. Strict callers ask for
# --fail-on divergent,unknown,error.
DEFAULT_FAIL_ON = "divergent"

# Flyway migration filename convention: V1__x.sql, V1.2__x.sql, V10__x.sql.
# Sorted numerically per dot-separated segment so V10 doesn't fold before V2.
_FLYWAY_VERSION_RE = re.compile(r"^V([0-9]+(?:\.[0-9]+)*)__")


class CliError(Exception):
    """Anything that is the CLI's fault, not a verdict: bad flags, unreadable
    file, network, auth, quota. Always exits EXIT_CLI_ERROR."""


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

def _read_source(path: str, label: str, stdin_claimed: list) -> str:
    """Read a SQL file, or stdin when path is '-'. Only one flag may claim
    stdin — a second would silently read empty."""
    if path == "-":
        if stdin_claimed:
            raise CliError(
                f"--{label} and --{stdin_claimed[0]} both requested stdin ('-'); "
                "only one input can be read from stdin."
            )
        stdin_claimed.append(label)
        text = sys.stdin.read()
    else:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:
            raise CliError(f"--{label}: no such file: {path}")
        except IsADirectoryError:
            raise CliError(f"--{label}: is a directory, expected a .sql file: {path}")
        except UnicodeDecodeError:
            raise CliError(f"--{label}: not valid UTF-8 text: {path}")
        except OSError as exc:
            raise CliError(f"--{label}: could not read {path}: {exc}")

    if not text.strip():
        raise CliError(f"--{label}: file is empty: {path}")
    return text


def _parse_fail_on(raw: str, valid_statuses=ALL_STATUSES) -> set:
    statuses = {s.strip().lower() for s in raw.split(",") if s.strip()}
    unknown = statuses - set(valid_statuses)
    if unknown:
        raise CliError(
            f"--fail-on: unknown status {', '.join(sorted(unknown))}. "
            f"Choose from: {', '.join(valid_statuses)}."
        )
    if not statuses:
        raise CliError("--fail-on: expected at least one status, got an empty value.")
    if "equivalent" in statuses:
        raise CliError("--fail-on: 'equivalent' is a proof of safety and cannot be a failure.")
    return statuses


def _validate_bound_and_timeout(bound: int, timeout_ms: int) -> None:
    if bound < 1 or bound > MAX_BOUND:
        raise CliError(f"--bound must be between 1 and {MAX_BOUND} (got {bound}).")
    if timeout_ms < 1_000 or timeout_ms > MAX_TIMEOUT_MS:
        raise CliError(
            f"--timeout-ms must be between 1000 and {MAX_TIMEOUT_MS} (got {timeout_ms})."
        )


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def post_verify(url, api_key, payload):
    """POST /api/verify/text and return the VerifyResponse dict verbatim.

    Every non-200 becomes a CliError: these are all problems with the request or
    the account, never a statement about the queries.
    """
    try:
        resp = httpx.post(
            f"{url}/api/verify/text",
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": _USER_AGENT},
            json=payload,
            timeout=_HTTP_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        raise CliError(f"Could not reach Skolem at {url}: {exc}")

    if resp.status_code == 401:
        raise CliError(
            "Skolem rejected the API key (401). Check SKOLEM_API_KEY or --api-key."
        )
    if resp.status_code == 402:
        raise CliError(
            "Skolem free-tier monthly quota exhausted for this API key. "
            "Upgrade the plan or wait for the next UTC calendar month."
        )
    if resp.status_code == 429:
        raise CliError("Skolem rate limit hit (429). Slow down and retry shortly.")
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("detail", "")
        except ValueError:
            detail = resp.text[:200]
        raise CliError(f"Skolem returned HTTP {resp.status_code}: {detail}")

    try:
        return resp.json()
    except ValueError:
        raise CliError(
            f"Skolem returned a non-JSON body (HTTP {resp.status_code}). "
            f"Is {url} really a Skolem instance?"
        )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_BADGES = {
    "equivalent": "PROVEN EQUIVALENT",
    "divergent": "DIVERGENT",
    "unknown": "UNKNOWN (no verdict)",
    "error": "ERROR (unsupported SQL or bad input)",
}


def _rows_table(rows, indent="    "):
    """Render a list of row dicts as an aligned text table."""
    if not rows:
        return f"{indent}(no rows)"
    if not isinstance(rows[0], dict):
        return "\n".join(f"{indent}{r}" for r in rows)

    cols = list(rows[0].keys())
    cells = [[("NULL" if r.get(c) is None else str(r.get(c))) for c in cols] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(cols)]

    out = [indent + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cols))]
    out.append(indent + "-+-".join("-" * w for w in widths))
    out += [indent + " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))
            for row in cells]
    return "\n".join(out)


def render_human(resp, failed):
    status = resp.get("status", "error")
    mark = "x" if failed else ("ok" if status == "equivalent" else "!")
    lines = [f"[{mark}] {_BADGES.get(status, status)}"]

    if status == "equivalent":
        lines.append("    The two queries return the same result on every database "
                     "within the bound.")
    if resp.get("divergence_reason"):
        lines.append(f"    {resp['divergence_reason']}")
    if resp.get("error_message"):
        lines.append(f"    {resp['error_message']}")
    if status == "unknown":
        lines.append("    The solver timed out. This is NOT a proof of equivalence — "
                     "retry with a longer --timeout-ms or a lower --bound.")

    db = resp.get("counterexample_db")
    if db:
        lines.append("")
        lines.append("Counterexample database:")
        for table, rows in db.items():
            lines.append(f"  {table}")
            lines.append(_rows_table(rows))

    if resp.get("query_v1_output") is not None or resp.get("query_v2_output") is not None:
        lines.append("")
        lines.append("On that database:")
        lines.append("  v1 returns")
        lines.append(_rows_table(resp.get("query_v1_output") or []))
        lines.append("  v2 returns")
        lines.append(_rows_table(resp.get("query_v2_output") or []))

    if resp.get("explanation"):
        lines.append("")
        lines.append("Explanation:")
        lines.append(f"  {resp['explanation']}")

    return "\n".join(lines)


def _gh_escape(text):
    """GitHub workflow-command escaping: annotation messages are one line."""
    return (text.replace("%", "%25").replace("\r", "%0D")
                .replace("\n", "%0A").replace("::", "%3A%3A"))


def render_github(resp, failed, file_hint):
    """A GitHub Actions annotation. Level follows the gate policy, so a status
    the caller chose not to fail on cannot paint the check red."""
    status = resp.get("status", "error")
    level = "error" if failed else ("notice" if status == "equivalent" else "warning")

    parts = [_BADGES.get(status, status)]
    for key in ("divergence_reason", "error_message"):
        if resp.get(key):
            parts.append(resp[key])
    if status == "unknown":
        parts.append("Solver timed out — this is not a proof of equivalence.")

    loc = f" file={file_hint}" if file_hint and file_hint != "-" else ""
    return f"::{level}{loc}::{_gh_escape(' — '.join(parts))}"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_verify(args):
    _validate_bound_and_timeout(args.bound, args.timeout_ms)
    fail_on = _parse_fail_on(args.fail_on)

    api_key = args.api_key or os.environ.get("SKOLEM_API_KEY")
    if not api_key:
        raise CliError(
            "No API key. Mint one in the Skolem UI (Keys page), then set "
            "SKOLEM_API_KEY or pass --api-key."
        )

    claimed = []
    payload = {
        "ddl_sql": _read_source(args.ddl, "ddl", claimed),
        "sql_v1": _read_source(args.v1, "v1", claimed),
        "sql_v2": _read_source(args.v2, "v2", claimed),
        "dialect": args.dialect,
        "bound": args.bound,
        "timeout_ms": args.timeout_ms,
    }
    if args.project:
        payload["project_id"] = args.project

    url = (args.url or os.environ.get("SKOLEM_URL") or DEFAULT_URL).rstrip("/")
    resp = post_verify(url, api_key, payload)

    status = resp.get("status", "error")
    failed = status in fail_on

    output = args.output
    if output == "auto":
        output = "human" if sys.stdout.isatty() else "json"

    if output == "json":
        print(json.dumps(resp))
    elif output == "github":
        print(render_github(resp, failed, args.v2))
    else:
        print(render_human(resp, failed))

    return EXIT_FAIL if failed else EXIT_PASS


# ---------------------------------------------------------------------------
# git plumbing (diff)
# ---------------------------------------------------------------------------

def _run_git(args):
    """Run git, returning the CompletedProcess without raising — callers that
    need to distinguish "no diff" (1) from a real error decide for themselves.
    Never uses shell=True; args are always a list, so there's no shell
    injection surface regardless of what --base/globs/paths contain."""
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True)  # nosec B603 B607
    except FileNotFoundError:
        raise CliError("git is not installed or not on PATH.")


def _git(args):
    """Run git, raising CliError on a non-zero exit. Returns stdout."""
    proc = _run_git(args)
    if proc.returncode != 0:
        raise CliError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _flyway_sort_key(path):
    """Numeric Flyway version order (V2 before V10), not lexicographic.
    Files that don't follow the V<n>__ convention sort after versioned ones,
    by name, rather than raising — a repo may mix migrations with other
    schema notes in the same directory."""
    name = os.path.basename(path)
    m = _FLYWAY_VERSION_RE.match(name)
    if not m:
        return (1, name)
    return (0, tuple(int(p) for p in m.group(1).split(".")))


def _resolve_ddl_paths(ddl_arg):
    """--ddl accepts a file, a directory of .sql files, or a glob."""
    if os.path.isdir(ddl_arg):
        paths = glob.glob(os.path.join(ddl_arg, "*.sql"))
    elif any(ch in ddl_arg for ch in "*?["):
        paths = glob.glob(ddl_arg)
    elif os.path.isfile(ddl_arg):
        paths = [ddl_arg]
    else:
        raise CliError(f"--ddl: no such file or directory: {ddl_arg}")
    if not paths:
        raise CliError(f"--ddl: no .sql files matched: {ddl_arg}")
    return sorted(paths, key=_flyway_sort_key)


def _read_ddl_paths(paths):
    parts = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as fh:
                parts.append(fh.read())
        except OSError as exc:
            raise CliError(f"--ddl: could not read {p}: {exc}")
    return "\n\n".join(parts)


def _ddl_changed_since(merge_base, ddl_paths):
    """True if any --ddl path differs between merge_base and the working tree.
    Only meaningful for git-tracked paths; an untracked/unresolvable path is
    silently skipped rather than treated as "changed" — not every repo keeps
    its schema file under version control next to the code, and the guard
    should degrade gracefully rather than block diff entirely."""
    for p in ddl_paths:
        proc = _run_git(["diff", "--quiet", merge_base, "--", p])
        if proc.returncode == 1:
            return True
    return False


def _match_any(path, globs):
    # fnmatch's '*' already spans '/', so 'q/*.sql' and 'q/**/*.sql' behave
    # the same — any depth. There is no narrower "this directory only" glob.
    return any(fnmatch.fnmatch(path, g) for g in globs)


def _discover_pairs(merge_base, globs):
    """Every changed file matching `globs` since merge_base, paired with its
    before/after content. Returns (targets, skipped) — targets is a list of
    (path, sql_v1, sql_v2); skipped is a list of (path, reason, kind) for
    changes that matched but have no comparable pair. kind is 'added',
    'deleted', or 'unreadable' — the caller uses it to warn about the git
    rename-detection gap (see _fold_undetected_rename_warning).

    `git diff <ref>` (no --cached) only reports paths already known to git —
    a file that was never `git add`ed is invisible to it, not just untracked.
    In a CI checkout everything in the diff is already committed, so this
    doesn't matter there, but a developer running this against their own
    dirty working tree with a brand-new, never-staged query file would
    otherwise see it vanish instead of getting the "added, no pair" notice.
    `git ls-files --others` closes that gap without touching the index."""
    status_out = _git(["diff", "--name-status", "-M", merge_base])
    targets, skipped = [], []
    seen = set()

    for line in status_out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        code = parts[0]
        if code.startswith(("R", "C")):
            old_path, new_path = parts[1], parts[2]
        else:
            old_path = new_path = parts[1]

        seen.add(new_path)
        if not _match_any(new_path, globs):
            continue

        if code == "A":
            skipped.append((new_path, "added — nothing to compare against", "added"))
            continue
        if code == "D":
            skipped.append((new_path, "deleted — nothing to compare against", "deleted"))
            continue

        # M, R*, C* all have a "before" (old_path @ merge_base) and an
        # "after" (new_path in the working tree).
        sql_v1 = _git(["show", f"{merge_base}:{old_path}"])
        try:
            with open(new_path, "r", encoding="utf-8") as fh:
                sql_v2 = fh.read()
        except OSError as exc:
            skipped.append((new_path, f"could not read working-tree file: {exc}", "unreadable"))
            continue

        if not sql_v1.strip() or not sql_v2.strip():
            skipped.append((new_path, "empty before or after content", "unreadable"))
            continue
        targets.append((new_path, sql_v1, sql_v2))

    untracked = _git(["ls-files", "--others", "--exclude-standard"])
    for path in untracked.splitlines():
        if path and path not in seen and _match_any(path, globs):
            skipped.append((path, "added — nothing to compare against", "added"))

    return targets, skipped


def _undetected_rename_warning(skipped):
    """git's rename detection (see _discover_pairs' use of `-M`) is unreliable
    for small, single-statement files — exactly the size of a typical SQL
    query file — even when a renamed file's content also changed in the same
    commit. When that happens, the rename surfaces as an unrelated add +
    delete, and each half looks like an ordinary new/removed file. Silently
    skipping both would let a genuinely changed query escape verification
    with no visible trace, so when both an add and a delete show up in the
    same run, this prints one explicit heads-up rather than two quiet skips
    that look unremarkable on their own."""
    added = [p for p, _, k in skipped if k == "added"]
    deleted = [p for p, _, k in skipped if k == "deleted"]
    if not added or not deleted:
        return None
    return (
        f"{len(deleted)} file(s) deleted and {len(added)} file(s) added in this diff "
        f"({', '.join(deleted)} / {', '.join(added)}). If any pair is actually a rename "
        "with edited content, git's rename detection can miss it for small files — that "
        "pair was NOT verified. Split the rename from the content change into separate "
        "commits if you need it checked."
    )


def cmd_diff(args):
    _validate_bound_and_timeout(args.bound, args.timeout_ms)
    fail_on = _parse_fail_on(args.fail_on, valid_statuses=ALL_STATUSES + (DDL_CHANGED,))

    api_key = args.api_key or os.environ.get("SKOLEM_API_KEY")
    if not api_key:
        raise CliError(
            "No API key. Mint one in the Skolem UI (Keys page), then set "
            "SKOLEM_API_KEY or pass --api-key."
        )
    url = (args.url or os.environ.get("SKOLEM_URL") or DEFAULT_URL).rstrip("/")

    merge_base = _git(["merge-base", args.base, "HEAD"]).strip()
    ddl_paths = _resolve_ddl_paths(args.ddl)
    ddl_sql = _read_ddl_paths(ddl_paths)

    if _ddl_changed_since(merge_base, ddl_paths):
        msg = ("the schema (--ddl) changed between the merge-base and the working "
               "tree — skipping all pairs. A query's meaning can't be checked across "
               "a schema change; the engine takes one schema for both queries.")
        if DDL_CHANGED in fail_on:
            print(f"skolem: {msg}", file=sys.stderr)
            return EXIT_FAIL
        print(f"skolem: {msg} (not failing the run — pass --fail-on ddl-changed "
              "for a strict gate)", file=sys.stderr)
        return EXIT_PASS

    targets, skipped = _discover_pairs(merge_base, args.globs)
    for path, reason, _kind in skipped:
        print(f"skolem: skip {path}: {reason}", file=sys.stderr)
    rename_warning = _undetected_rename_warning(skipped)
    if rename_warning:
        print(f"skolem: WARNING: {rename_warning}", file=sys.stderr)

    if not targets:
        if not skipped:
            print("skolem: no changed files matched.", file=sys.stderr)
        return EXIT_PASS

    output = args.output
    if output == "auto":
        output = "human" if sys.stdout.isatty() else "json"

    worst = EXIT_PASS
    for path, sql_v1, sql_v2 in targets:
        payload = {
            "ddl_sql": ddl_sql, "sql_v1": sql_v1, "sql_v2": sql_v2,
            "dialect": args.dialect, "bound": args.bound, "timeout_ms": args.timeout_ms,
        }
        if args.project:
            payload["project_id"] = args.project

        try:
            resp = post_verify(url, api_key, payload)
        except CliError as exc:
            print(f"skolem: {path}: {exc}", file=sys.stderr)
            worst = EXIT_CLI_ERROR
            continue

        status = resp.get("status", "error")
        failed = status in fail_on

        if output == "json":
            print(json.dumps({**resp, "path": path}))
        elif output == "github":
            print(render_github(resp, failed, path))
        else:
            print(f"--- {path} ---")
            print(render_human(resp, failed))

        if worst != EXIT_CLI_ERROR:
            worst = max(worst, EXIT_FAIL if failed else EXIT_PASS)

    return worst


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _add_common_flags(parser, fail_on_help):
    """Flags shared by `verify` and `diff`: everything about *how* to call the
    solver and gate the result, as opposed to *which* queries to send it."""
    parser.add_argument("--dialect", default="generic",
                        help="SQL dialect for parsing (default: generic).")
    parser.add_argument("--bound", type=int, default=DEFAULT_BOUND,
                        help=f"Max rows per table Z3 explores, 1-{MAX_BOUND} "
                             f"(default: {DEFAULT_BOUND}). An 'equivalent' verdict is "
                             "sound only within this bound.")
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS, dest="timeout_ms",
                        help=f"Solver timeout in ms (default: {DEFAULT_TIMEOUT_MS}, "
                             f"max: {MAX_TIMEOUT_MS}).")
    parser.add_argument("--project", metavar="ID", help="Tag the run with a project id.")
    parser.add_argument("--output", choices=("auto", "human", "json", "github"), default="auto",
                        help="Output format (default: auto — human on a TTY, json when piped).")
    parser.add_argument("--fail-on", default=DEFAULT_FAIL_ON, dest="fail_on", metavar="STATUSES",
                        help=fail_on_help)
    parser.add_argument("--url", metavar="URL",
                        help=f"Skolem base URL (env: SKOLEM_URL, default: {DEFAULT_URL}).")
    parser.add_argument("--api-key", metavar="KEY", dest="api_key",
                        help="Per-user skm_ API key (env: SKOLEM_API_KEY).")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="skolem",
        description="Formally verify that two SQL queries are semantically equivalent.",
    )
    parser.add_argument("--version", action="version", version=f"skolem {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser(
        "verify",
        help="Verify one explicit pair of queries against a schema.",
        description="Verify one explicit pair of queries against a schema. "
                    "Any input may be '-' to read from stdin (at most one).",
    )
    v.add_argument("--ddl", required=True, metavar="PATH",
                   help="Flyway-style CREATE TABLE DDL defining the schema.")
    v.add_argument("--v1", required=True, metavar="PATH",
                   help="The original / trusted query.")
    v.add_argument("--v2", required=True, metavar="PATH",
                   help="The rewritten query to check against v1.")
    _add_common_flags(v, fail_on_help="Comma-separated statuses that exit 1 "
                                      f"(default: {DEFAULT_FAIL_ON}; e.g. divergent,unknown,error).")
    v.set_defaults(func=cmd_verify)

    d = sub.add_parser(
        "diff",
        help="Verify every changed query file against its pre-change version.",
        description="For each file matching GLOB that changed since --base, verify the "
                    "working-tree version against the version at the merge-base with "
                    "--base. Must run inside a git repository.",
    )
    d.add_argument("globs", nargs="+", metavar="GLOB",
                   help="Shell-style glob(s) matching changed query files, e.g. "
                        "'queries/*.sql'. '*' matches across directory separators too.")
    d.add_argument("--base", required=True, metavar="REF",
                   help="Git ref to diff against, e.g. origin/main. Compared via "
                        "merge-base, so commits landed on it after your branch point "
                        "are ignored.")
    d.add_argument("--ddl", required=True, metavar="PATH",
                   help="Schema DDL: a file, a directory of .sql files (concatenated "
                        "in Flyway V<n>__ version order), or a glob.")
    _add_common_flags(d, fail_on_help="Comma-separated statuses that exit 1 (default: "
                              f"{DEFAULT_FAIL_ON}; e.g. divergent,unknown,error,{DDL_CHANGED}). "
                              f"'{DDL_CHANGED}' also fails the run when --ddl itself "
                              "changed since --base (default: warn and pass, since no "
                              "pair has a trustworthy verdict across a schema change).")
    d.set_defaults(func=cmd_diff)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CliError as exc:
        print(f"skolem: {exc}", file=sys.stderr)
        return EXIT_CLI_ERROR
    except KeyboardInterrupt:
        print("skolem: interrupted.", file=sys.stderr)
        return EXIT_CLI_ERROR


if __name__ == "__main__":
    sys.exit(main())
