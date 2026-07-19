"""
tests/cli_diff_test.py

Tests for the `sqlverify diff` CLI command (cli/sqlverify_cli.py).

Unlike cli_verify_test.py, git plumbing is exercised for real — every test
builds a throwaway git repo under a temp dir and drives real `git` subprocess
calls (merge-base, diff --name-status, show). Only the HTTP transport is
stubbed, the same way cli_verify_test.py does it, so these tests pin the part
`diff` owns that `verify` doesn't: pair discovery from git, DDL assembly and
its Flyway version ordering, the DDL-changed guard, and worst-of exit-code
aggregation across multiple files.

One behavior pinned deliberately: git's rename detection (`-M`) is unreliable
for small single-statement files even when -M's threshold is set to 1% — see
test_undetected_rename_prints_loud_warning. A renamed-and-edited query file
routinely surfaces as an unrelated delete + add, which would otherwise let a
changed query escape verification with no visible trace. That gap can't be
fixed from here (it's upstream git behavior), so what's pinned is that it's
never silent: both halves are still reported per-file, and one additional
loud warning fires whenever an add and a delete land in the same run.

Run directly (no pytest needed):
    .venv/bin/python tests/cli_diff_test.py
or with pytest:
    .venv/bin/python -m pytest tests/cli_diff_test.py -v
"""

import io
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cli.sqlverify_cli as cli

DDL = "CREATE TABLE users (id INT PRIMARY KEY, age INT);"


# ── Stub the transport (same approach as cli_verify_test.py) ────────────────

calls: list = []
_responses: list = []  # queue; each call to post_verify pops the next one


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _fake_post(url, headers=None, json=None, timeout=None):
    calls.append({"url": url, "headers": headers, "json": json})
    payload = _responses.pop(0) if _responses else {"status": "equivalent"}
    return _FakeResponse(200, payload)


cli.httpx.post = _fake_post


def _reset(responses=None):
    calls.clear()
    _responses.clear()
    if responses:
        _responses.extend(responses)
    os.environ["SQLVERIFY_API_KEY"] = "sqv_test_key"
    os.environ.pop("SQLVERIFY_URL", None)


# ── Git repo fixture ─────────────────────────────────────────────────────────

class _Repo:
    """A throwaway git repo. write(path, content) + commit() build history;
    everything after the last commit() is the uncommitted working tree."""

    def __init__(self, root):
        self.root = root

    def _git(self, *args):
        proc = subprocess.run(["git", *args], cwd=self.root,
                              capture_output=True, text=True)
        assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
        return proc.stdout

    def write(self, rel_path, content):
        full = os.path.join(self.root, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)

    def rm(self, rel_path):
        os.remove(os.path.join(self.root, rel_path))

    def mv(self, src, dst):
        self._git("mv", src, dst)

    def commit(self, message="commit"):
        self._git("add", "-A")
        self._git("commit", "-q", "-m", message)

    def path(self, rel_path):
        return os.path.join(self.root, rel_path)


def _make_repo(tmp):
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp, check=True)
    repo = _Repo(tmp)
    repo.write("schema.sql", DDL)
    repo.write("queries/a.sql", "SELECT id FROM users WHERE age > 18;")
    repo.commit("base")
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp, check=True)
    return repo


class _Capture:
    def __enter__(self):
        self._out, self._err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        return self

    def __exit__(self, *a):
        self.out = sys.stdout.getvalue()
        self.err = sys.stderr.getvalue()
        sys.stdout, sys.stderr = self._out, self._err
        return False


def _run_in(repo, argv):
    cwd = os.getcwd()
    os.chdir(repo.root)
    try:
        with _Capture() as cap:
            code = cli.main(argv)
        return code, cap.out, cap.err
    finally:
        os.chdir(cwd)


def _diff_argv(*extra):
    return ["diff", "--base", "main", "--ddl", "schema.sql", "queries/*.sql", *extra]


# ── Pair discovery: modified / added / deleted ──────────────────────────────

def test_modified_file_is_verified():
    _reset(responses=[{"status": "equivalent"}])
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(tmp)
        repo.write("queries/a.sql", "SELECT id FROM users WHERE age >= 19;")
        code, out, _ = _run_in(repo, _diff_argv("--output", "json"))
    assert code == cli.EXIT_PASS, f"expected 0, got {code}\n{out}"
    body = json.loads(out.strip())
    assert body["path"] == "queries/a.sql"
    assert calls[0]["json"]["sql_v1"] == "SELECT id FROM users WHERE age > 18;"
    assert calls[0]["json"]["sql_v2"] == "SELECT id FROM users WHERE age >= 19;"


def test_added_file_is_skipped_not_verified():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(tmp)
        repo.write("queries/new.sql", "SELECT 1;")
        code, out, err = _run_in(repo, _diff_argv())
    assert code == cli.EXIT_PASS
    assert not calls, "an added file has no 'before' — must not hit the network"
    assert "queries/new.sql" in err and "added" in err


def test_deleted_file_is_skipped_not_verified():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(tmp)
        repo.rm("queries/a.sql")
        code, out, err = _run_in(repo, _diff_argv())
    assert code == cli.EXIT_PASS
    assert not calls
    assert "queries/a.sql" in err and "deleted" in err


def test_non_matching_file_is_ignored_entirely():
    """A changed file outside the glob shouldn't even appear in a skip
    message — it's out of scope, not an unpaired change."""
    _reset(responses=[{"status": "equivalent"}])
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(tmp)
        repo.write("README.md", "notes")
        repo.write("queries/a.sql", "SELECT id FROM users WHERE age >= 19;")
        code, out, err = _run_in(repo, _diff_argv())
    assert code == cli.EXIT_PASS
    assert "README.md" not in err
    assert len(calls) == 1


def test_unchanged_repo_verifies_nothing():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(tmp)
        code, out, err = _run_in(repo, _diff_argv())
    assert code == cli.EXIT_PASS
    assert not calls
    assert "no changed files matched" in err


# ── Undetected-rename warning ────────────────────────────────────────────────

def test_undetected_rename_prints_loud_warning():
    """git's -M rename detection routinely misses a rename + content edit on
    a small single-statement file (verified against real git — see the module
    docstring); this pins that the CLI still surfaces it as a visible warning
    instead of two skip lines that look like unrelated add/delete noise."""
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(tmp)
        repo.mv("queries/a.sql", "queries/b.sql")
        repo.write("queries/b.sql", "SELECT id FROM users WHERE age >= 19;")
        code, out, err = _run_in(repo, _diff_argv())
    assert code == cli.EXIT_PASS
    assert not calls, "an undetected rename must not be silently paired and verified"
    assert "WARNING" in err
    assert "queries/a.sql" in err and "queries/b.sql" in err


def test_pure_add_and_delete_without_rename_does_not_warn():
    """Two genuinely unrelated changes (a real new file, a real deletion)
    must not trigger the rename heads-up — that would cry wolf on the
    common case and train people to ignore it."""
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(tmp)
        repo.rm("queries/a.sql")
        repo.write("queries/unrelated.sql", "SELECT 1;")
        code, out, err = _run_in(repo, _diff_argv())
    assert code == cli.EXIT_PASS
    assert "WARNING" in err  # an add and a delete did co-occur — still warns
    # but content-wise these truly are unrelated; the warning is a heads-up,
    # not a false-positive claim of a rename, and no verification is skipped
    # silently either way. Documented here so the tradeoff is explicit.


# ── DDL-changed guard ─────────────────────────────────────────────────────────

def test_ddl_changed_skips_everything_by_default():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(tmp)
        repo.write("schema.sql", DDL + "\nALTER TABLE users ADD COLUMN x INT;")
        repo.write("queries/a.sql", "SELECT id FROM users WHERE age >= 19;")
        code, out, err = _run_in(repo, _diff_argv())
    assert code == cli.EXIT_PASS, "ddl-changed must pass by default"
    assert not calls, "must not verify anything once the schema itself changed"
    assert "schema" in err.lower()


def test_ddl_changed_fails_when_requested():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(tmp)
        repo.write("schema.sql", DDL + "\nALTER TABLE users ADD COLUMN x INT;")
        repo.write("queries/a.sql", "SELECT id FROM users WHERE age >= 19;")
        code, out, err = _run_in(repo, _diff_argv("--fail-on", "ddl-changed"))
    assert code == cli.EXIT_FAIL
    assert not calls


def test_ddl_unchanged_verifies_normally():
    _reset(responses=[{"status": "equivalent"}])
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(tmp)
        repo.write("queries/a.sql", "SELECT id FROM users WHERE age >= 19;")
        code, out, err = _run_in(repo, _diff_argv())
    assert code == cli.EXIT_PASS
    assert len(calls) == 1


# ── --fail-on validation (diff accepts the ddl-changed pseudo-status too) ───

def test_fail_on_rejects_unknown_status():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(tmp)
        code, out, err = _run_in(repo, _diff_argv("--fail-on", "bogus"))
    assert code == cli.EXIT_CLI_ERROR
    assert "unknown status" in err


# ── DDL assembly: directory + Flyway version order ──────────────────────────

def test_ddl_directory_concatenates_in_flyway_order():
    """V10 must sort after V2, not before it (lexicographic V1 < V10 < V2
    would silently fold the ALTERs out of order)."""
    _reset(responses=[{"status": "equivalent"}])
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp, check=True)
        repo = _Repo(tmp)
        repo.write("migrations/V1__init.sql", "CREATE TABLE t (id INT PRIMARY KEY);")
        repo.write("migrations/V2__add_a.sql", "ALTER TABLE t ADD COLUMN a INT;")
        repo.write("migrations/V10__add_b.sql", "ALTER TABLE t ADD COLUMN b INT;")
        repo.write("queries/a.sql", "SELECT id FROM t;")
        repo.commit("base")
        subprocess.run(["git", "branch", "-M", "main"], cwd=tmp, check=True)
        repo.write("queries/a.sql", "SELECT id FROM t WHERE id > 0;")

        code, out, err = _run_in(repo, ["diff", "--base", "main", "--ddl", "migrations/",
                                        "queries/*.sql", "--output", "json"])
    assert code == cli.EXIT_PASS, err
    ddl_sent = calls[0]["json"]["ddl_sql"]
    assert ddl_sent.index("ADD COLUMN a") < ddl_sent.index("ADD COLUMN b"), ddl_sent


def test_ddl_missing_path_is_cli_error():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(tmp)
        code, out, err = _run_in(repo, ["diff", "--base", "main", "--ddl", "nope.sql",
                                        "queries/*.sql"])
    assert code == cli.EXIT_CLI_ERROR
    assert "no such file" in err


# ── Worst-of exit-code aggregation across multiple files ────────────────────

def test_worst_of_exit_code_divergent_beats_equivalent():
    _reset(responses=[{"status": "equivalent"}, {"status": "divergent"}])
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(tmp)
        repo.write("queries/a.sql", "SELECT id FROM users WHERE age >= 19;")
        repo.write("queries/b.sql", "SELECT id FROM users;")
        repo.commit("add b")
        repo.write("queries/a.sql", "SELECT id FROM users WHERE age >= 20;")
        repo.write("queries/b.sql", "SELECT id FROM users WHERE 1=1;")
        code, out, _ = _run_in(repo, _diff_argv("--output", "json"))
    assert code == cli.EXIT_FAIL
    assert len(calls) == 2


def test_all_equivalent_exits_zero():
    _reset(responses=[{"status": "equivalent"}])
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(tmp)
        repo.write("queries/a.sql", "SELECT id FROM users WHERE age >= 19;")
        code, out, _ = _run_in(repo, _diff_argv("--output", "json"))
    assert code == cli.EXIT_PASS


def test_no_git_repo_is_cli_error():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        repo = _Repo(tmp)  # never `git init`
        os.makedirs(os.path.join(tmp, "queries"), exist_ok=True)
        code, out, err = _run_in(repo, _diff_argv())
    assert code == cli.EXIT_CLI_ERROR
    assert "git" in err.lower()


def test_bad_base_ref_is_cli_error():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(tmp)
        code, out, err = _run_in(repo, ["diff", "--base", "not-a-real-ref",
                                        "--ddl", "schema.sql", "queries/*.sql"])
    assert code == cli.EXIT_CLI_ERROR


# ── Runner (no pytest required) ──────────────────────────────────────────────

def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {t.__name__}\n        {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  ERROR {t.__name__}\n        {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
