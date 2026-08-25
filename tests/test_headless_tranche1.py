"""Headless hardening, tranche 1 (plan/headless-hardening.md): F5 canonical cwd, F6 classifiable
exit status, F8a per-run generation overrides. Every feature has its failing case AND its
must-still-pass case (interactive/unset behaviour unchanged)."""
import argparse
import json
import logging
import os
import subprocess
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
import agent as _agent  # noqa: E402


# ── F5: canonical working directory ─────────────────────────────────────────
class TestCanonicalCwd:
    def test_symlinked_cwd_is_rewritten_and_pwd_follows(self, tmp_path, monkeypatch):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        env = {"PWD": str(link)}
        chdirs = []
        monkeypatch.setattr(_agent, "_NO_REALPATH_CWD", False)
        out = _agent._canonicalize_cwd(env=env, chdir=chdirs.append, getcwd=lambda: str(link))
        assert out == (str(link), str(real.resolve()))
        assert chdirs == [str(real.resolve())]
        assert env["PWD"] == str(real.resolve())

    def test_real_path_is_a_noop(self, tmp_path, monkeypatch):
        real = str((tmp_path / "real").resolve())
        os.makedirs(real, exist_ok=True)
        env = {"PWD": real}
        chdirs = []
        monkeypatch.setattr(_agent, "_NO_REALPATH_CWD", False)
        assert _agent._canonicalize_cwd(env=env, chdir=chdirs.append, getcwd=lambda: real) is None
        assert chdirs == [] and env["PWD"] == real

    def test_stale_pwd_alone_is_repaired(self, tmp_path, monkeypatch):
        # Linux reports a physical cwd already; a shell's $PWD is what carries the symlink.
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        env = {"PWD": str(link)}
        monkeypatch.setattr(_agent, "_NO_REALPATH_CWD", False)
        out = _agent._canonicalize_cwd(env=env, chdir=lambda p: None, getcwd=lambda: str(real.resolve()))
        assert out == (str(link), str(real.resolve())) and env["PWD"] == str(real.resolve())

    def test_escape_hatch_keeps_symlinked_view(self, tmp_path, monkeypatch):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        env = {"PWD": str(link)}
        chdirs = []
        monkeypatch.setattr(_agent, "_NO_REALPATH_CWD", True)
        assert _agent._canonicalize_cwd(env=env, chdir=chdirs.append, getcwd=lambda: str(link)) is None
        assert chdirs == [] and env["PWD"] == str(link)


# ── F6: classifiable exit status ────────────────────────────────────────────
class TestExitStatus:
    @pytest.mark.parametrize("result,kind,last,expected", [
        ("error", "context", None, _agent.EXIT_CONTEXT),
        ("error", "backend", None, _agent.EXIT_BACKEND),
        ("error", None, None, _agent.EXIT_ERROR),
        ("done", None, None, _agent.EXIT_OK),
        ("cancelled", None, None, _agent.EXIT_OK),
        ("done", None, {"code": _agent.EXIT_MEMORY, "detail": "rss"}, _agent.EXIT_MEMORY),
        ("error", "context", {"code": _agent.EXIT_DEADLINE, "detail": "t"}, _agent.EXIT_DEADLINE),
    ])
    def test_classification_matrix(self, result, kind, last, expected):
        code, _detail = _agent._classify_auto_result(result, kind, last)
        assert code == expected

    def test_exit_line_format(self):
        assert _agent._exit_line({"name": "context", "detail": "overflow"}) == "AGENT-EXIT: context overflow"
        assert _agent._exit_line({"name": "completed", "detail": ""}) == "AGENT-EXIT: completed"

    def test_set_exit_names_every_code(self):
        for code in (0, 1, 10, 11, 12, 13, 14, 15):
            info = _agent._set_exit(code, "d")
            assert info["name"] in _agent._EXIT_NAMES.values() and info["code"] == code
        _agent._LAST_EXIT = None

    @pytest.mark.parametrize("auto,expected_rc", [(True, _agent.EXIT_CONFIG), (False, 2)])
    def test_config_error_exit_code_auto_vs_interactive(self, tmp_path, auto, expected_rc):
        """An unbuildable backend at start exits 14 with an AGENT-EXIT line in -a mode, and
        the historical 2 (no line) interactively — the must-still-pass half."""
        (tmp_path / ".agent").mkdir()
        (tmp_path / ".agent" / "config.json").write_text(json.dumps(
            {"backends": {"main": {"kind": "no-such-backend-kind"}}}))
        cmd = [sys.executable, os.path.join(_REPO, "agent.py")] + (["-a"] if auto else []) + ["hello"]
        p = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True, timeout=120,
                           env={**os.environ, "PYTHONUNBUFFERED": "1"})
        assert p.returncode == expected_rc, p.stderr[-800:]
        if auto:
            assert "AGENT-EXIT: config" in p.stderr
        else:
            assert "AGENT-EXIT" not in p.stderr


# ── F8a: per-run generation overrides ───────────────────────────────────────
class TestGenerationOverrides:
    def _restore(self, snapshot):
        _agent._config["generation"].clear()
        _agent._config["generation"].update(snapshot)

    def test_flag_beats_config_and_unset_flag_leaves_config(self):
        snap = dict(_agent._config["generation"])
        try:
            _agent._config["generation"]["top_p"] = 0.42
            applied = _agent._apply_generation_overrides(
                argparse.Namespace(temperature=0.1, top_p=None, seed=7))
            assert applied == {"temperature": 0.1, "seed": 7}
            assert _agent._config["generation"]["temperature"] == 0.1
            assert _agent._config["generation"]["top_p"] == 0.42          # untouched
            assert _agent._generation_request_extras() == {"seed": 7}
        finally:
            self._restore(snap)

    def test_no_seed_means_no_extra_request_key(self):
        snap = dict(_agent._config["generation"])
        try:
            _agent._config["generation"]["seed"] = None
            assert _agent._generation_request_extras() == {}
            applied = _agent._apply_generation_overrides(argparse.Namespace(temperature=None, top_p=None, seed=None))
            assert applied == {} and _agent._generation_request_extras() == {}
        finally:
            self._restore(snap)

    def test_seed_on_backend_without_seed_warns_once(self, caplog, monkeypatch):
        snap = dict(_agent._config["generation"])
        kind = _agent._config["backends"]["main"].get("kind")
        try:
            _agent._config["backends"]["main"]["kind"] = "bedrock"
            monkeypatch.setattr(_agent, "_SEED_WARNED", False)
            with caplog.at_level(logging.WARNING, logger="agent"):
                _agent._apply_generation_overrides(argparse.Namespace(temperature=None, top_p=None, seed=3))
                _agent._apply_generation_overrides(argparse.Namespace(temperature=None, top_p=None, seed=3))
            warns = [r for r in caplog.records if "seed" in r.getMessage()]
            assert len(warns) == 1
        finally:
            _agent._config["backends"]["main"]["kind"] = kind
            self._restore(snap)
