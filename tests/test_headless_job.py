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
