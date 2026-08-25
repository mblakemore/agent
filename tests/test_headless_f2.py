"""Headless hardening F2 — result contract (plan/headless-hardening.md).

Golden matrix: a valid block passes; a decoy block without the discriminator at message end
is prose and draws a correction; two valid blocks → last wins; no block at the correction bound
→ synthesized failed record (exit 11); --result-file without the contract is byte-identical to
the raw behaviour (must-still-pass); an unreadable schema refuses to start (exit 14)."""
import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
import agent as _agent  # noqa: E402
from agent import (RESULT_CONTRACT_DEFAULT_SCHEMA, _extract_result_block, _validate_result,  # noqa: E402
                   _write_result_file, run_agent_single)

VALID = '```json\n{"contract": 1, "status": "done", "summary": "built it", "artifacts": ["out/x"], "verify_output": "ok", "scope": {"examined": [], "skipped": [], "not_covered": []}}\n```'
DECOY = '```json\n{"status": "done", "summary": "this is what a result looks like"}\n```'


# ── pure ──────────────────────────────────────────────────────────────────────
class TestExtractValidate:
    def test_valid_block_extracts_and_validates(self):
        obj, why = _extract_result_block("work done.\n" + VALID)
        assert obj and obj["status"] == "done" and why == ""
        assert _validate_result(obj, RESULT_CONTRACT_DEFAULT_SCHEMA) == (True, "")

    def test_decoy_without_discriminator_is_prose(self):
        obj, why = _extract_result_block("an example result:\n" + DECOY)
        assert obj is None and "contract" in why

    def test_two_valid_blocks_last_wins(self):
        first = VALID.replace('"summary": "built it"', '"summary": "FIRST"')
        obj, _ = _extract_result_block(first + "\n...then I reconsidered...\n" + VALID)
        assert obj["summary"] == "built it"

    @pytest.mark.parametrize("bad,needle", [
        ({"contract": 1, "status": "maybe", "summary": "x"}, "status"),
        ({"contract": 1, "summary": "x"}, "status"),
        ({"contract": 1, "status": "done", "summary": 7}, "summary"),
        ({"contract": 1, "status": "done", "summary": "x", "artifacts": "not-a-list"}, "artifacts"),
    ])
    def test_invalid_shapes_are_named(self, bad, needle):
        ok, why = _validate_result(bad, RESULT_CONTRACT_DEFAULT_SCHEMA)
        assert ok is False and needle in why


# ── result file writer ────────────────────────────────────────────────────────
@pytest.fixture
def contract_cfg():
    snap = dict(_agent._config["cycle"])
    yield _agent._config["cycle"]
    _agent._config["cycle"].clear()
    _agent._config["cycle"].update(snap)
    _agent._RESULT_CONTRACT_SCHEMA = None
    _agent._LAST_EXIT = None


class TestResultFile:
    def test_without_contract_raw_text_is_byte_identical(self, tmp_path, contract_cfg):
        contract_cfg["result_contract"] = False
        hist = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "plain final text — no JSON"}]
        out = tmp_path / "r.txt"
        assert _write_result_file(str(out), hist) == (None, False)
        assert out.read_text(encoding="utf-8") == "plain final text — no JSON"

    def test_with_contract_validated_json_only_plus_exit(self, tmp_path, contract_cfg):
        contract_cfg["result_contract"] = True
        hist = [{"role": "assistant", "content": "prose before\n" + VALID}]
        out = tmp_path / "r.json"
        obj, synth = _write_result_file(str(out), hist, exit_info={"code": 0, "name": "completed", "detail": ""})
        assert synth is False and obj["status"] == "done"
        data = json.loads(out.read_text())
        assert data["summary"] == "built it" and data["exit"]["name"] == "completed"
        assert "prose before" not in out.read_text()

    def test_no_block_synthesizes_failed_record(self, tmp_path, contract_cfg):
        contract_cfg["result_contract"] = True
        hist = [{"role": "assistant", "content": "I am done, everything works."}]
        out = tmp_path / "r.json"
        obj, synth = _write_result_file(str(out), hist, exit_info={"code": 0, "name": "completed", "detail": ""})
        assert synth is True
        data = json.loads(out.read_text())
        assert data["contract"] == 1 and data["status"] == "failed" and data["synthesized"] is True
        assert "no valid result block" in data["summary"]


# ── loop level ────────────────────────────────────────────────────────────────
def _resp(content):
    r = MagicMock()
    r.status_code = 200
    r.iter_lines.return_value = [f"data: {json.dumps({'choices': [{'delta': {'content': content}}]})}".encode(), b"data: [DONE]"]
    return r


class TestContractLoop:
    @patch("agent._emit")
    @patch("agent._llm_request")
    def test_missing_block_draws_correction_then_valid_block_passes(self, mock_llm, _emit, contract_cfg):
        contract_cfg["result_contract"] = True
        contract_cfg["result_contract_max_blocks"] = 2
        mock_llm.side_effect = [_resp("All done, the feature works."), _resp("Right — " + VALID)]
        history = [{"role": "user", "content": "x"}]
        with patch("agent._NUDGE_ENABLED", False):
            result = run_agent_single(history, {"text": "", "up_to": 0}, [], MagicMock())
        assert result == "done" and mock_llm.call_count == 2
        corrections = [m for m in history if m.get("role") == "user" and "valid result block" in str(m.get("content"))]
        assert len(corrections) == 1

    @patch("agent._emit")
    @patch("agent._llm_request")
    def test_decoy_at_message_end_is_corrected(self, mock_llm, _emit, contract_cfg):
        contract_cfg["result_contract"] = True
        mock_llm.side_effect = [_resp("Here is an example of the shape:\n" + DECOY), _resp(VALID)]
        history = [{"role": "user", "content": "x"}]
        with patch("agent._NUDGE_ENABLED", False):
            result = run_agent_single(history, {"text": "", "up_to": 0}, [], MagicMock())
        assert result == "done" and mock_llm.call_count == 2

    @patch("agent._emit")
    @patch("agent._llm_request")
    def test_no_block_at_bound_exits_and_synthesizes(self, mock_llm, _emit, contract_cfg, tmp_path):
        contract_cfg["result_contract"] = True
        contract_cfg["result_contract_max_blocks"] = 2
        mock_llm.side_effect = [_resp(f"still no block, attempt {i}") for i in range(6)]
        history = [{"role": "user", "content": "x"}]
        with patch("agent._NUDGE_ENABLED", False):
            result = run_agent_single(history, {"text": "", "up_to": 0}, [], MagicMock())
        assert result == "done"
        assert mock_llm.call_count == 3, "two corrections, then the exit proceeds"
        out = tmp_path / "r.json"
        _, synth = _write_result_file(str(out), history, exit_info={"code": 0, "name": "completed", "detail": ""})
        assert synth is True and json.loads(out.read_text())["status"] == "failed"


# ── launch ────────────────────────────────────────────────────────────────────
def test_unreadable_schema_refuses_to_start_exit_14(tmp_path):
    p = subprocess.run([sys.executable, os.path.join(_REPO, "agent.py"), "-a", "--result-contract",
                        "--result-schema", str(tmp_path / "missing-schema.json"), "hello"],
                       cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert p.returncode == _agent.EXIT_CONFIG, p.stderr[-600:]
    assert "AGENT-EXIT: config" in p.stderr and "schema" in p.stderr
