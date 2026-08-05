# Skolem SQL Verification — GitHub Action

A packaged wrapper around [`skolem diff`](../cli/README.md#diff--every-query-a-branch-actually-changed):
verify every SQL query file a pull request changed, and fail the check if any
of them diverge from what they replaced.

This is a **composite action**, not a Docker action — it has no image to
publish or keep in sync. It installs `../cli` (this repo's CLI, httpx-only —
see [`cli/README.md`](../cli/README.md)) with `pip` and calls `skolem diff`.
The Z3 solver runs on your Skolem deployment, not in the runner.

## Usage

```yaml
# .github/workflows/sql-proof.yml
name: sql-proof
on: [pull_request]

jobs:
  prove:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0   # skolem diff needs the merge base — a shallow clone has none

      - uses: NguyenTienDat377/SQLVerify/action@main
        with:
          ddl: migrations/
          glob: 'queries/*.sql'
          base: ${{ github.event.pull_request.base.sha }}
          api-key: ${{ secrets.SKOLEM_API_KEY }}
```

Pin `@main` to a commit SHA or a release tag once one exists, the same as
you would any third-party action.

## Inputs

| input | required | default | meaning |
|---|---|---|---|
| `ddl` | yes | — | Flyway DDL: a file, a directory (concatenated in Flyway `V<n>__` order), or a glob |
| `glob` | yes | — | glob(s) matching changed query files; one per line for more than one |
| `base` | yes | — | git ref to diff against — usually `${{ github.event.pull_request.base.sha }}` |
| `fail-on` | no | `divergent` | comma-separated statuses that fail the check (`divergent`, `unknown`, `error`, `ddl-changed`) |
| `dialect` | no | `generic` | SQL dialect for parsing |
| `bound` | no | server default | max rows per table Z3 explores, 1–6 |
| `timeout-ms` | no | server default | solver timeout in ms |
| `project` | no | — | tag the run with a Skolem project id |
| `url` | no | `https://skolem.dev` | your Skolem deployment |
| `api-key` | yes | — | `skm_...` key — store as a secret, never inline |
| `python-version` | no | `3.12` | Python version for the CLI |

## Outputs

| output | meaning |
|---|---|
| `exit-code` | `skolem diff`'s exit code: `0` policy pass, `1` policy fail, `2` the CLI itself failed (bad flags, network, 401/402/429) |

The step itself also exits with that code, so a default job fails exactly
when the policy does — reading `steps.<id>.outputs.exit-code` is only useful
if you want to branch on it without failing the job (e.g. `continue-on-error:
true` plus a later step).

## Why full clone depth

`skolem diff` compares each changed file's working-tree version against its
content at `git merge-base --base HEAD`, so the same command gives the same
answer locally on a dirty tree and in CI after checkout. That merge base
doesn't exist in a shallow clone — hence `fetch-depth: 0` in the example
above. This action does not check out the repo itself; it assumes
`actions/checkout` already ran.

## Why composite, not Docker

A Docker action's build context is limited to its own directory, so it can't
`COPY ../cli` — the only way to make that work is to either duplicate the CLI
into the action's directory (drift risk) or publish and version a separate
container image (real infrastructure this project doesn't have yet). A
composite action runs in the caller's own runner and can `pip install` a
sibling directory directly, so it stays a thin wrapper with nothing to
publish — consistent with `cli/`'s own "never reimplement, just call the
API" design.
