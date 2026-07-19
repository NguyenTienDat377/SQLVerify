"""
tests/cli_verify_test.py

Tests for the `sqlverify verify` CLI command (cli/sqlverify_cli.py).

The CLI is a thin HTTP client, so the transport is stubbed: httpx.post is
replaced with a recorder that returns a canned VerifyResponse. What's exercised
for real is everything the CLI itself owns — argument validation, file/stdin
reading, the request payload, the exit-code policy (--fail-on), and the three
output renderers.

The exit-code contract under test:
    0  policy pass
    1  policy fail — a verdict the caller asked to fail on
    2  the CLI's own failure (bad flags, unreadable file, auth, network)

Run directly (no pytest needed):
    .venv/bin/python tests/cli_verify_test.py
or with pytest:
    .venv/bin/python -m pytest tests/cli_verify_test.py -v
"""

import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cli.sqlverify_cli as cli

# ── Stub the transport ───────────────────────────────────────────────────────

DDL = "CREATE TABLE users (id INT PRIMARY KEY, name TEXT);"
Q1 = "SELECT id FROM users WHERE id > 1;"
Q2 = "SELECT id FROM users WHERE id >= 2;"

calls: list = []
_next_response = {"status": "equivalent"}
_next_status_code = 200
_raise_transport = None


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)

    def json(self):
        if not isinstance(self._payload, dict):
            raise ValueError("not json")
        return self._payload


def _fake_post(url, headers=None, json=None, timeout=None):
    calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
    if _raise_transport is not None:
        raise _raise_transport
    return _FakeResponse(_next_status_code, _next_response)


cli.httpx.post = _fake_post


def _reset():
    global _next_response, _next_status_code, _raise_transport
    calls.clear()
    _next_response = {"status": "equivalent"}
    _next_status_code = 200
    _raise_transport = None
    os.environ["SQLVERIFY_API_KEY"] = "sqv_test_key"
    os.environ.pop("SQLVERIFY_URL", None)


# ── Helpers ──────────────────────────────────────────────────────────────────

class _Capture:
    """Capture stdout/stderr and force a non-TTY (so --output auto → json)."""

    def __enter__(self):
        self._out, self._err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        return self

    def __exit__(self, *a):
        self.out = sys.stdout.getvalue()
        self.err = sys.stderr.getvalue()
        sys.stdout, sys.stderr = self._out, self._err
        return False


def _run(argv):
    """Run main(argv), returning (exit_code, stdout, stderr)."""
    with _Capture() as cap:
        code = cli.main(argv)
    return code, cap.out, cap.err


def _files(tmp, ddl=DDL, v1=Q1, v2=Q2):
    paths = {}
    for name, content in (("ddl", ddl), ("v1", v1), ("v2", v2)):
        p = os.path.join(tmp, f"{name}.sql")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)
        paths[name] = p
    return paths


def _argv(paths, *extra):
    return ["verify", "--ddl", paths["ddl"], "--v1", paths["v1"],
            "--v2", paths["v2"], *extra]


# ── Exit-code policy ─────────────────────────────────────────────────────────

def test_equivalent_exits_zero():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        code, out, _ = _run(_argv(_files(tmp), "--output", "json"))
    assert code == cli.EXIT_PASS, f"expected 0, got {code}"
    assert json.loads(out)["status"] == "equivalent"


def test_divergent_exits_one():
    global _next_response
    _reset()
    _next_response = {"status": "divergent", "divergence_reason": "row presence differs"}
    with tempfile.TemporaryDirectory() as tmp:
        code, _, _ = _run(_argv(_files(tmp), "--output", "json"))
    assert code == cli.EXIT_FAIL, f"divergent must exit 1, got {code}"


def test_unknown_passes_by_default_but_fails_when_requested():
    """A timeout is not a proof, but it must not paint the check red by default
    — that is what gets a CI gate uninstalled. Opt in via --fail-on."""
    global _next_response
    _reset()
    _next_response = {"status": "unknown"}
    with tempfile.TemporaryDirectory() as tmp:
        paths = _files(tmp)
        code, _, _ = _run(_argv(paths, "--output", "json"))
        assert code == cli.EXIT_PASS, f"unknown should pass by default, got {code}"

        code, _, _ = _run(_argv(paths, "--output", "json",
                                "--fail-on", "divergent,unknown"))
        assert code == cli.EXIT_FAIL, f"unknown should fail under --fail-on, got {code}"


def test_error_status_passes_by_default():
    global _next_response
    _reset()
    _next_response = {"status": "error", "error_message": "CTEs are not supported"}
    with tempfile.TemporaryDirectory() as tmp:
        code, _, _ = _run(_argv(_files(tmp), "--output", "json"))
    assert code == cli.EXIT_PASS, f"unsupported SQL should not block by default, got {code}"


def test_transport_failure_is_cli_error_not_a_verdict():
    """A network blip must never be mistakable for a broken query (2, not 1)."""
    global _raise_transport
    _reset()
    _raise_transport = cli.httpx.ConnectError("connection refused")
    with tempfile.TemporaryDirectory() as tmp:
        code, _, err = _run(_argv(_files(tmp), "--output", "json"))
    assert code == cli.EXIT_CLI_ERROR, f"expected 2, got {code}"
    assert "Could not reach SQLVerify" in err, err


def test_auth_and_quota_failures_are_cli_errors():
    global _next_status_code, _next_response
    for status, needle in ((401, "rejected the API key"),
                           (402, "quota exhausted"),
                           (429, "rate limit")):
        _reset()
        _next_status_code = status
        _next_response = {"detail": "nope"}
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = _run(_argv(_files(tmp), "--output", "json"))
        assert code == cli.EXIT_CLI_ERROR, f"HTTP {status} → expected 2, got {code}"
        assert needle in err, f"HTTP {status}: {err}"


def test_missing_api_key_is_cli_error():
    _reset()
    os.environ.pop("SQLVERIFY_API_KEY", None)
    with tempfile.TemporaryDirectory() as tmp:
        code, _, err = _run(_argv(_files(tmp)))
    assert code == cli.EXIT_CLI_ERROR, f"expected 2, got {code}"
    assert "No API key" in err, err
    assert not calls, "must not hit the network without a key"


# ── Argument validation ──────────────────────────────────────────────────────

def test_bound_above_server_max_is_rejected_locally():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        code, _, err = _run(_argv(_files(tmp), "--bound", "9"))
    assert code == cli.EXIT_CLI_ERROR, f"expected 2, got {code}"
    assert "--bound" in err, err
    assert not calls, "a bad bound must not cost a round trip"


def test_timeout_above_ceiling_is_rejected_locally():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        code, _, err = _run(_argv(_files(tmp), "--timeout-ms", "999999"))
    assert code == cli.EXIT_CLI_ERROR, f"expected 2, got {code}"
    assert "--timeout-ms" in err, err


def test_unknown_fail_on_status_is_rejected():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        code, _, err = _run(_argv(_files(tmp), "--fail-on", "divergant"))
    assert code == cli.EXIT_CLI_ERROR, f"expected 2, got {code}"
    assert "unknown status" in err, err


def test_fail_on_equivalent_is_rejected():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        code, _, err = _run(_argv(_files(tmp), "--fail-on", "equivalent"))
    assert code == cli.EXIT_CLI_ERROR, f"expected 2, got {code}"
    assert "cannot be a failure" in err, err


def test_missing_file_is_cli_error():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        paths = _files(tmp)
        paths["v2"] = os.path.join(tmp, "nope.sql")
        code, _, err = _run(_argv(paths))
    assert code == cli.EXIT_CLI_ERROR, f"expected 2, got {code}"
    assert "no such file" in err, err


def test_empty_file_is_cli_error():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        paths = _files(tmp, v1="   \n")
        code, _, err = _run(_argv(paths))
    assert code == cli.EXIT_CLI_ERROR, f"expected 2, got {code}"
    assert "empty" in err, err


# ── Input handling ───────────────────────────────────────────────────────────

def test_stdin_input():
    _reset()
    real_stdin = sys.stdin
    try:
        sys.stdin = io.StringIO(Q2)
        with tempfile.TemporaryDirectory() as tmp:
            paths = _files(tmp)
            code, _, _ = _run(["verify", "--ddl", paths["ddl"], "--v1", paths["v1"],
                               "--v2", "-", "--output", "json"])
    finally:
        sys.stdin = real_stdin
    assert code == cli.EXIT_PASS, f"expected 0, got {code}"
    assert calls[0]["json"]["sql_v2"] == Q2, calls[0]["json"]["sql_v2"]


def test_two_stdin_inputs_rejected():
    """The second '-' would silently read an empty string — reject instead."""
    _reset()
    real_stdin = sys.stdin
    try:
        sys.stdin = io.StringIO(Q1)
        with tempfile.TemporaryDirectory() as tmp:
            paths = _files(tmp)
            code, _, err = _run(["verify", "--ddl", paths["ddl"],
                                 "--v1", "-", "--v2", "-"])
    finally:
        sys.stdin = real_stdin
    assert code == cli.EXIT_CLI_ERROR, f"expected 2, got {code}"
    assert "only one input" in err, err


# ── Request payload ──────────────────────────────────────────────────────────

def test_payload_and_headers():
    _reset()
    os.environ["SQLVERIFY_URL"] = "http://localhost:8000/"
    with tempfile.TemporaryDirectory() as tmp:
        code, _, _ = _run(_argv(_files(tmp), "--output", "json", "--dialect", "postgres",
                                "--bound", "4", "--timeout-ms", "30000",
                                "--project", "proj-1"))
    assert code == cli.EXIT_PASS
    call = calls[0]
    assert call["url"] == "http://localhost:8000/api/verify/text", call["url"]
    body = call["json"]
    assert body == {"ddl_sql": DDL, "sql_v1": Q1, "sql_v2": Q2, "dialect": "postgres",
                    "bound": 4, "timeout_ms": 30000, "project_id": "proj-1"}, body
    assert call["headers"]["Authorization"] == "Bearer sqv_test_key"
    # _resolve_surface() keys analytics off this prefix.
    assert call["headers"]["User-Agent"].startswith("sqlverify-cli/"), call["headers"]
    # httpx must outlive the server's 120s solve ceiling.
    assert call["timeout"] > 120, call["timeout"]


def test_project_omitted_when_unset():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        _run(_argv(_files(tmp), "--output", "json"))
    assert "project_id" not in calls[0]["json"], calls[0]["json"]


def test_api_key_flag_overrides_env():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        _run(_argv(_files(tmp), "--output", "json", "--api-key", "sqv_flag"))
    assert calls[0]["headers"]["Authorization"] == "Bearer sqv_flag"


# ── Output renderers ─────────────────────────────────────────────────────────

def test_json_output_is_verbatim():
    global _next_response
    _reset()
    _next_response = {"status": "divergent", "divergence_reason": "row presence differs",
                      "counterexample_db": {"users": [{"id": 2, "name": None}]},
                      "query_v1_output": [], "query_v2_output": [{"id": 2}],
                      "explanation": "v2 includes id=2."}
    with tempfile.TemporaryDirectory() as tmp:
        code, out, _ = _run(_argv(_files(tmp), "--output", "json"))
    assert code == cli.EXIT_FAIL
    assert json.loads(out) == _next_response, out


def test_human_output_shows_counterexample():
    global _next_response
    _reset()
    _next_response = {"status": "divergent", "divergence_reason": "row presence differs",
                      "counterexample_db": {"users": [{"id": 2, "name": None}]},
                      "query_v1_output": [], "query_v2_output": [{"id": 2}]}
    with tempfile.TemporaryDirectory() as tmp:
        code, out, _ = _run(_argv(_files(tmp), "--output", "human"))
    assert code == cli.EXIT_FAIL
    for needle in ("DIVERGENT", "row presence differs", "users", "NULL", "(no rows)"):
        assert needle in out, f"{needle!r} missing from:\n{out}"


def test_human_output_warns_unknown_is_not_a_proof():
    global _next_response
    _reset()
    _next_response = {"status": "unknown"}
    with tempfile.TemporaryDirectory() as tmp:
        code, out, _ = _run(_argv(_files(tmp), "--output", "human"))
    assert code == cli.EXIT_PASS
    assert "NOT a proof" in out, out


def test_github_annotation_level_follows_policy():
    """A status the caller chose not to fail on must not emit ::error — that
    would paint the check red behind the exit code's back."""
    global _next_response
    _reset()
    _next_response = {"status": "unknown"}
    with tempfile.TemporaryDirectory() as tmp:
        paths = _files(tmp)
        _, out, _ = _run(_argv(paths, "--output", "github"))
        assert out.startswith("::warning "), out

        _, out, _ = _run(_argv(paths, "--output", "github",
                               "--fail-on", "divergent,unknown"))
        assert out.startswith("::error "), out

        _next_response = {"status": "equivalent"}
        _, out, _ = _run(_argv(paths, "--output", "github"))
        assert out.startswith("::notice "), out


def test_github_annotation_is_single_line_and_escaped():
    global _next_response
    _reset()
    _next_response = {"status": "divergent",
                      "divergence_reason": "line one\nline two",
                      "error_message": None}
    with tempfile.TemporaryDirectory() as tmp:
        paths = _files(tmp)
        code, out, _ = _run(_argv(paths, "--output", "github"))
    assert code == cli.EXIT_FAIL
    assert len(out.strip().splitlines()) == 1, f"annotation must be one line:\n{out}"
    assert "%0A" in out, out
    assert f"file={paths['v2']}" in out, out


def test_auto_output_is_json_when_piped():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        code, out, _ = _run(_argv(_files(tmp)))  # _Capture makes stdout non-TTY
    assert code == cli.EXIT_PASS
    assert json.loads(out)["status"] == "equivalent", out


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
