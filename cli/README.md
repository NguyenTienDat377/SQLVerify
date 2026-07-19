# SQLVerify CLI

Formally prove that a rewritten SQL query is equivalent to the original — or get
a concrete counterexample database where they differ — from a terminal or a CI
pipeline.

It holds no Z3 solver. It forwards each call to a running SQLVerify's
`POST /api/verify/text`, authenticated with a per-user `sqv_` API key. The engine
stays on the server; this client runs wherever you do.

This lives inside the SQLVerify repo (versioned with the API contract it calls)
but is self-contained — `httpx` is its only dependency, so installing it never
drags in Z3.

## Install

```bash
pipx install ./cli          # or: pip install ./cli
sqlverify --version
```

Mint a `sqv_…` key in the SQLVerify UI (**Keys** page), then:

```bash
export SQLVERIFY_API_KEY=sqv_...
export SQLVERIFY_URL=https://sqlverify.com   # optional; this is the default
```

Both have flag equivalents (`--api-key`, `--url`). The env names match the
[MCP server](../mcp/README.md)'s, so one setup covers both surfaces.

## Usage

### `verify` — one explicit pair

```bash
sqlverify verify --ddl schema.sql --v1 before.sql --v2 after.sql
```

Any input may be `-` to read from stdin (at most one):

```bash
git show HEAD~1:queries/report.sql | sqlverify verify --ddl schema.sql --v1 - --v2 queries/report.sql
```

| flag | meaning |
|---|---|
| `--ddl` | Flyway-style `CREATE TABLE` DDL defining the schema |
| `--v1` | the original / trusted query |
| `--v2` | the rewritten query to check against `--v1` |

### `diff` — every query a branch actually changed

```bash
sqlverify diff --base origin/main --ddl migrations/ 'queries/*.sql'
```

For each file matching a glob that changed since `--base`, verifies the
working-tree version against the version at the merge-base with `--base` — so
running it locally on a dirty tree and running it in CI after checkout give
the same answer. Requires a git repository.

| flag | meaning |
|---|---|
| `GLOB` (positional, 1+) | shell-style glob(s) matching changed query files, e.g. `'queries/*.sql'`. `*` matches across directory separators too — there's no narrower "this directory only" form. |
| `--base` | git ref to diff against. Compared via merge-base, so commits landed on it after your branch point are ignored |
| `--ddl` | a file, a directory of `.sql` files (concatenated in **Flyway `V<n>__` version order** — numeric, so `V10` sorts after `V2`), or a glob |

Added and deleted files are skipped (nothing to compare against) and noted on
stderr, not silently dropped. If the schema (`--ddl`) itself changed since
`--base`, **every** pair is skipped — the engine needs one fixed schema for
both queries, so a query's meaning can't be checked across a schema change.
That's a warn-and-pass by default; add `ddl-changed` to `--fail-on` for a
strict gate.

**Known gap:** git's rename detection is unreliable for small, single-statement
files — which query files usually are — even when the rename carries an edit.
A renamed-and-rewritten query can surface as an unrelated delete + add, and
that pair is **not verified**. When an add and a delete land in the same run,
`diff` prints one explicit `WARNING` naming both files so this never fails
silently; if you need that pair checked, split the rename from the content
change into separate commits.

### Flags shared by both commands

| flag | meaning |
|---|---|
| `--dialect` | SQL dialect for parsing (default `generic`) |
| `--bound` | max rows per table Z3 explores, 1–6 (default 3) |
| `--timeout-ms` | solver timeout (default 60000, max 120000) |
| `--project` | tag the run with a SQLVerify project id |
| `--output` | `auto` (default) \| `human` \| `json` \| `github` |
| `--fail-on` | statuses that exit 1 (default `divergent`; `diff` also accepts `ddl-changed`) |

## Exit codes

```
0  policy pass
1  policy fail — a verdict you asked --fail-on to fail on
2  the CLI's own failure: bad flags, unreadable file, network, 401, 402, 429
```

Transport failure is deliberately **2, not 1**: a lapsed API key or a network
blip must never be mistakable for a broken query.

`--fail-on` defaults to `divergent` only, so `unknown` and `error` warn loudly
but pass. That default is about adoption — a check that goes red the first time
someone writes a CTE gets uninstalled. For a strict gate:

```bash
sqlverify verify ... --fail-on divergent,unknown,error
```

Note that `unknown` means the solver timed out. It is **not** a proof of
equivalence — the CLI says so on every `unknown`, whichever way you gate it.

## Output

`auto` prints `human` to a terminal and `json` when piped, so this works:

```bash
sqlverify verify --ddl schema.sql --v1 a.sql --v2 b.sql | jq .counterexample_db
```

`json` is SQLVerify's `VerifyResponse` verbatim — see the
[MCP README](../mcp/README.md#exposed-tool) for the field table.

`github` emits a single GitHub Actions annotation whose level follows your
`--fail-on` policy (`::error` when it fails the run, `::warning` otherwise,
`::notice` when equivalent), so the check's colour can't contradict its exit
code.

## Tests

```bash
.venv/bin/python tests/cli_verify_test.py   # from the repo root
```
