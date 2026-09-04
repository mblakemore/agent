"""--job: declarative job files and the runner-verified acceptance gate.

The gate is the reason this feature exists, so most of these tests are end-to-end
subprocess runs rather than unit tests of the helpers. A gate that passes in-process
and fails as a real invocation would be worse than no gate at all.
"""
import json
import os
import subprocess
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
from agent import (EXIT_ACCEPTANCE, EXIT_CONFIG, _apply_env_allow,  # noqa: E402
                   _job_prompt, _load_job_spec, _run_acceptance)

_AGENT = os.path.join(_REPO, "agent.py")


def _run(tmp_path, spec, *extra, env=None, name="job.json"):
    """Run agent.py -a --job <spec> against the fake backend. Returns CompletedProcess."""
    p = tmp_path / name
    p.write_text(spec if isinstance(spec, str) else json.dumps(spec), encoding="utf-8")
    e = dict(os.environ, AGENT_FAKE_BACKEND="1")
    if env:
        e.update(env)
    return subprocess.run([sys.executable, _AGENT, "-a", "--job", str(p), *extra, "go"],
                          cwd=str(tmp_path), capture_output=True, text=True, timeout=180, env=e)


# ── the gate ───────────────────────────────────────────────────────────────────────────
class TestAcceptanceGate:
    def test_passing_check_leaves_the_run_successful(self, tmp_path):
        (tmp_path / "out.txt").write_text("x", encoding="utf-8")
        r = _run(tmp_path, {"goal": "g", "acceptance": "test -f ./out.txt"})
        assert r.returncode == 0
        assert "AGENT-ACCEPTANCE: pass" in r.stderr

    def test_run_reports_done_but_produced_nothing__gate_overrides_it(self, tmp_path):
        """The whole point. The run exits happy; the artifact is absent; the gate decides."""
        r = _run(tmp_path, {"goal": "g", "acceptance": "test -f ./never-written.txt"})
        assert r.returncode == EXIT_ACCEPTANCE
        assert "AGENT-ACCEPTANCE: FAIL" in r.stderr

    def test_missing_binary_fails_closed(self, tmp_path):
        """A check that cannot run is not a check that passed."""
        r = _run(tmp_path, {"goal": "g", "acceptance": "definitely-not-a-real-binary-xyz --check"})
        assert r.returncode == EXIT_ACCEPTANCE

    def test_unparseable_command_is_refused_at_launch(self, tmp_path):
        """Refuse before spending a run, rather than discovering a dead gate at the end."""
        r = _run(tmp_path, {"goal": "g", "acceptance": "echo (unbalanced"})
        assert r.returncode == EXIT_CONFIG
        assert "not a parseable command" in r.stderr

    def test_no_acceptance_declared_is_a_silent_no_op(self, tmp_path):
        r = _run(tmp_path, {"goal": "g"})
        assert r.returncode == 0
        assert "AGENT-ACCEPTANCE" not in r.stderr

    def test_timeout_fails_closed_rather_than_hanging_the_runner(self):
        ok, code, out = _run_acceptance("sleep 30", timeout=1)
        assert ok is False
        assert "exceeded" in out


# ── loading ────────────────────────────────────────────────────────────────────────────
class TestJobLoading:
    def test_yaml_and_json_produce_the_same_spec(self, tmp_path):
        pytest.importorskip("yaml")
        j = tmp_path / "a.json"
        y = tmp_path / "a.yaml"
        j.write_text('{"goal": "same", "timebox_sec": 60}', encoding="utf-8")
        y.write_text("goal: same\ntimebox_sec: 60\n", encoding="utf-8")
        assert _load_job_spec(str(j)) == _load_job_spec(str(y))

    def test_json_content_in_a_yaml_named_file_still_loads(self, tmp_path):
        """Dispatched jobs get renamed; content decides, not the extension."""
        p = tmp_path / "misnamed.yaml"
        p.write_text('{"goal": "json really"}', encoding="utf-8")
        assert _load_job_spec(str(p))["goal"] == "json really"

    @pytest.mark.parametrize("body,name", [
        ('["not", "a", "mapping"]', "list.json"),
        ('{"goal": "unterminated', "bad.json"),
    ])
    def test_malformed_job_is_a_config_refusal_not_a_crash(self, tmp_path, body, name):
        r = _run(tmp_path, body, name=name)
        assert r.returncode == EXIT_CONFIG

    def test_absent_job_file_is_a_config_refusal(self, tmp_path):
        e = dict(os.environ, AGENT_FAKE_BACKEND="1")
        r = subprocess.run([sys.executable, _AGENT, "-a", "--job",
                            str(tmp_path / "nope.json"), "go"],
                           cwd=str(tmp_path), capture_output=True, text=True, timeout=180, env=e)
        assert r.returncode == EXIT_CONFIG


# ── env_allow ──────────────────────────────────────────────────────────────────────────
class TestEnvAllow:
    def test_absent_allowlist_is_a_no_op(self):
        """Unset and 'set to empty' must not mean the same thing."""
        os.environ["_JOB_CANARY"] = "alive"
        try:
            assert _apply_env_allow(None) is None
            assert os.environ.get("_JOB_CANARY") == "alive"
        finally:
            os.environ.pop("_JOB_CANARY", None)

    def test_empty_allowlist_scrubs(self, tmp_path):
        r = _run(tmp_path, {"goal": "g", "acceptance": 'test -n "$_JOB_CANARY"', "env_allow": []},
                 env={"_JOB_CANARY": "alive"})
        assert r.returncode == EXIT_ACCEPTANCE

    def test_named_variable_survives_the_scrub(self, tmp_path):
        r = _run(tmp_path, {"goal": "g", "acceptance": 'test -n "$_JOB_CANARY"',
                            "env_allow": ["_JOB_CANARY", "AGENT_FAKE_BACKEND"]},
                 env={"_JOB_CANARY": "alive"})
        assert r.returncode == 0


# ── precedence and prompt ──────────────────────────────────────────────────────────────
class TestPrecedence:
    def test_explicit_goal_flag_replaces_the_file_goal_everywhere(self, tmp_path):
        """An override that reaches the anchor but not the prompt gives the run two goals."""
        (tmp_path / "out.txt").write_text("x", encoding="utf-8")
        r = _run(tmp_path, {"goal": "FILE GOAL", "acceptance": "test -f ./out.txt"},
                 "--goal", "FLAG GOAL")
        blob = r.stdout + r.stderr
        assert "GOAL: FLAG GOAL" in blob
        assert "FILE GOAL" not in blob

    def test_prompt_carries_goal_context_constraints_deliverable_and_acceptance(self):
        text = _job_prompt({"goal": "G", "context": ["C1"], "constraints": ["K1"],
                            "deliverable": ["D1"], "acceptance": "true"})
        for expected in ("GOAL: G", "C1", "K1", "D1", "true"):
            assert expected in text

    def test_prompt_states_that_blocked_is_a_legal_outcome(self):
        """Otherwise the incentive is to produce something rather than report a blocker."""
        assert "blocked" in _job_prompt({"goal": "G"})


# ── review tranche (end-to-end review of --job): precedence, schema, diagnostics ───────
import agent as _agent  # noqa: E402


class TestGateExitPrecedence:
    def test_gate_failure_does_not_mask_a_hard_stop(self, monkeypatch, tmp_path):
        """A run that hit its deadline and then fails the gate exits DEADLINE, not ACCEPTANCE.

        The supervisor's branch on 10 is "size the timebox"; on 16 it is "the artifact is
        wrong". Overwriting the cause with the symptom hides the thing the typed vocabulary
        exists to expose. The file's own rule already says a hard stop beats the generic map;
        the gate is held to the same rule. Its verdict still lands in the detail.
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(_agent, "_JOB_ACCEPTANCE", "false")
        monkeypatch.setattr(_agent, "_LAST_EXIT", None)
        _agent._set_exit(_agent.EXIT_DEADLINE, "wall-clock deadline 5s reached at turn 9")
        _agent._acceptance_verdict()
        assert _agent._LAST_EXIT["code"] == _agent.EXIT_DEADLINE
        assert "acceptance" in _agent._LAST_EXIT["detail"].lower()

    def test_gate_failure_on_a_clean_run_is_the_exit(self, monkeypatch, tmp_path):
        """Control: with no hard stop recorded, the gate's failure IS the exit code."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(_agent, "_JOB_ACCEPTANCE", "false")
        monkeypatch.setattr(_agent, "_LAST_EXIT", None)
        _agent._acceptance_verdict()
        assert _agent._LAST_EXIT["code"] == EXIT_ACCEPTANCE

    def test_gate_pass_after_a_hard_stop_keeps_the_hard_stop(self, monkeypatch, tmp_path):
        """The artifact being right does not un-happen the deadline."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(_agent, "_JOB_ACCEPTANCE", "true")
        monkeypatch.setattr(_agent, "_LAST_EXIT", None)
        _agent._set_exit(_agent.EXIT_DEADLINE, "wall-clock deadline 5s reached at turn 9")
        _agent._acceptance_verdict()
        assert _agent._LAST_EXIT["code"] == _agent.EXIT_DEADLINE


class TestSchemaRefusals:
    def test_unknown_top_level_key_is_refused_at_launch(self, tmp_path):
        """`acceptence:` is a dead gate that looks exactly like a passing one. Same class as
        the unparseable-command refusal: find it before the run is spent, and name the key."""
        r = _run(tmp_path, {"goal": "g", "acceptence": "test -f ./never-written.txt"})
        assert r.returncode == EXIT_CONFIG
        assert "acceptence" in r.stderr

    def test_non_positive_timebox_is_refused_not_ignored(self, tmp_path):
        """`--deadline 0` is refused; `timebox_sec: 0` used to be silently dropped."""
        r = _run(tmp_path, {"goal": "g", "timebox_sec": 0})
        assert r.returncode == EXIT_CONFIG

    def test_parse_check_without_bash_is_a_config_refusal_not_a_traceback(self, monkeypatch):
        def _no_bash(*a, **k):
            raise FileNotFoundError("bash")
        monkeypatch.setattr(_agent.subprocess, "run", _no_bash)
        ok, msg = _agent._acceptance_parses("true")
        assert ok is False
        assert "bash" in msg


class TestGateDiagnostics:
    def test_failure_line_carries_the_last_line_and_the_output_is_kept(self, tmp_path):
        """pytest and most tools put the verdict on the LAST line; the first is a banner.
        And 200 chars of one line is not the evidence — the full output goes to a file."""
        r = _run(tmp_path, {"goal": "g",
                            "acceptance": "printf 'banner line\\nFAILED: the reason\\n'; exit 3"})
        assert r.returncode == EXIT_ACCEPTANCE
        assert "FAILED: the reason" in r.stderr
        saved = tmp_path / ".agent" / "acceptance-out.txt"
        assert saved.exists()
        body = saved.read_text(encoding="utf-8")
        assert "banner line" in body and "FAILED: the reason" in body and "exit: 3" in body

    def test_acceptance_timeout_sec_is_a_job_field(self, tmp_path):
        r = _run(tmp_path, {"goal": "g", "acceptance": "sleep 5", "acceptance_timeout_sec": 1})
        assert r.returncode == EXIT_ACCEPTANCE
        assert "exceeded 1s" in r.stderr


class TestResultContractField:
    def test_result_contract_true_in_the_file_arms_the_contract(self, tmp_path):
        """Without the contract, --result-file gets the raw reply byte-for-byte; with it, the
        file is the validated (or synthesized) record. The file shape is the discriminator."""
        out = tmp_path / "result.json"
        r = _run(tmp_path, {"goal": "g", "result_contract": True}, "--result-file", str(out))
        assert r.returncode in (0, _agent.EXIT_CONTRACT)
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data.get("contract") == 1

    def test_flag_beats_file_for_the_schema(self, tmp_path):
        schema = tmp_path / "s.json"
        schema.write_text('{"type": "object"}', encoding="utf-8")
        out = tmp_path / "result.json"
        r = _run(tmp_path, {"goal": "g", "result_contract": "does-not-exist.json"},
                 "--result-schema", str(schema), "--result-file", str(out))
        assert r.returncode != EXIT_CONFIG


class TestEnvAllowKeepsTheInterpreterKnobs:
    def test_python_runtime_variables_survive_the_scrub(self, tmp_path):
        """A supervisor reads the run through its log. PYTHONUNBUFFERED is how that log arrives
        while the run is alive; a scrub that strips it re-buffers the very thing being watched.
        The interpreter's own knobs are 'what a process needs to run', not job secrets."""
        r = _run(tmp_path, {"goal": "g", "acceptance": 'test "$PYTHONUNBUFFERED" = "1"',
                            "env_allow": ["AGENT_FAKE_BACKEND"]},
                 env={"PYTHONUNBUFFERED": "1"})
        assert r.returncode == 0
