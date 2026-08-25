"""F8b end to end (review finding 1): a REAL `agent.py -a` run driven through the fake backend's
streaming path — the suite's other subprocess tests exit before streaming, and the loop-level
tests feed mocks, so a fake whose iter_lines() rejected the live path's kwargs exited 0 with an
empty result file. These runs assert on the result file and the exit code, nothing else."""
import json
import os
import subprocess
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
import agent as _agent  # noqa: E402

VALID_BLOCK = ('```json\n{"contract": 1, "status": "done", "summary": "hello done", "artifacts": [], '
               '"verify_output": "", "scope": {"examined": [], "skipped": [], "not_covered": []}}\n```')


def _run(tmp_path, script, extra_args):
    (tmp_path / ".agent").mkdir(exist_ok=True)
    (tmp_path / ".agent" / "config.json").write_text(json.dumps(
        {"llm": {"kind": "fake", "script": script}, "summary": {"enabled": False}, "advisor": {"enabled": False}}))
    out = tmp_path / "r.out"
    p = subprocess.run([sys.executable, os.path.join(_REPO, "agent.py"), "-a", "--result-file", str(out)] + extra_args + ["say hello"],
                       cwd=tmp_path, capture_output=True, text=True, timeout=180,
                       env={**os.environ, "PYTHONUNBUFFERED": "1", "NO_COLOR": "1"})
    return p, out


@pytest.mark.slow
def test_fake_streams_through_the_real_loop_and_fills_the_result_file(tmp_path):
    p, out = _run(tmp_path, ["hello from the fake"], [])
    assert p.returncode == 0, p.stderr[-800:]
    assert "Unexpected error during streaming" not in p.stderr
    assert out.read_text(encoding="utf-8").strip() == "hello from the fake"
    assert "AGENT-EXIT: completed" in p.stderr


@pytest.mark.slow
def test_contract_valid_block_end_to_end(tmp_path):
    p, out = _run(tmp_path, ["work done\n" + VALID_BLOCK], ["--result-contract"])
    assert p.returncode == 0, p.stderr[-800:]
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["status"] == "done" and data["summary"] == "hello done" and data["exit"]["name"] == "completed"


@pytest.mark.slow
def test_contract_no_block_synthesizes_and_exits_11(tmp_path):
    p, out = _run(tmp_path, ["no block here", "still none", "nope"], ["--result-contract"])
    assert p.returncode == _agent.EXIT_CONTRACT, p.stderr[-800:]
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["status"] == "failed" and data["synthesized"] is True and data["exit"]["name"] == "contract"
    assert "AGENT-EXIT: contract" in p.stderr


@pytest.mark.slow
def test_three_streaming_failures_exit_as_backend_error(tmp_path):
    # the request succeeds and the STREAM raises — the loop's streaming handler path
    bad = {"stream_error": "simulated mid-stream failure"}
    p, out = _run(tmp_path, [bad, bad, bad, bad], [])
    assert p.returncode in (_agent.EXIT_BACKEND, _agent.EXIT_ERROR), p.stderr[-800:]
    assert "AGENT-EXIT: completed" not in p.stderr
