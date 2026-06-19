"""Config relocation (.agent/config.json), git-repo-gated .gitignore upkeep,
and the Windows skip for the POSIX world-readable warning.
"""

import logging
import os

import pytest

import agent


# ── _in_git_repo ────────────────────────────────────────────────────────────

def test_in_git_repo_detects_dot_git_in_ancestor(tmp_path):
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert agent._in_git_repo(str(sub)) is True


def test_in_git_repo_false_outside(tmp_path):
    assert agent._in_git_repo(str(tmp_path)) is False


# ── _ensure_gitignored ──────────────────────────────────────────────────────

def test_ensure_gitignored_adds_entries_in_repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    agent._ensure_gitignored((".agent/", "config.json"))
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert ".agent/" in lines and "config.json" in lines


def test_ensure_gitignored_idempotent(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    agent._ensure_gitignored((".agent/", "config.json"))
    agent._ensure_gitignored((".agent/", "config.json"))
    text = (tmp_path / ".gitignore").read_text()
    assert text.count("config.json") == 1
    assert text.count(".agent/") == 1


def test_ensure_gitignored_noop_outside_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no .git here
    agent._ensure_gitignored((".agent/", "config.json"))
    assert not (tmp_path / ".gitignore").exists()


def test_ensure_gitignored_preserves_existing_and_appends_cleanly(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("*.pyc\nnode_modules/\n")
    monkeypatch.chdir(tmp_path)
    agent._ensure_gitignored((".agent/", "config.json"))
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert "*.pyc" in lines and "node_modules/" in lines
    assert ".agent/" in lines and "config.json" in lines


# ── _load_config location preference ────────────────────────────────────────

def test_load_config_prefers_agent_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "config.json").write_text('{"llm": {"model": "from-agent-dir"}}')
    (tmp_path / "config.json").write_text('{"llm": {"model": "from-root"}}')
    cfg = agent._load_config()
    assert cfg["llm"]["model"] == "from-agent-dir"


def test_load_config_falls_back_to_legacy_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text('{"llm": {"model": "from-root"}}')
    cfg = agent._load_config()
    assert cfg["llm"]["model"] == "from-root"


# ── world-readable warning ──────────────────────────────────────────────────

def test_world_readable_warn_skipped_on_windows(monkeypatch, caplog):
    monkeypatch.setattr(agent.os, "name", "nt")
    cfg = {"backends": {"main": {"api_key": "secret"}}}
    with caplog.at_level(logging.WARNING, logger="agent"):
        agent._warn_if_world_readable_with_key(r"C:\x\config.json", cfg)
    assert not any("world-readable" in r.message for r in caplog.records)


def test_world_readable_warns_on_posix(tmp_path, monkeypatch, caplog):
    if os.name == "nt":
        pytest.skip("POSIX-only behavior")
    monkeypatch.setattr(agent.os, "name", "posix")
    p = tmp_path / "config.json"
    p.write_text("{}")
    os.chmod(p, 0o666)
    cfg = {"backends": {"main": {"api_key": "secret"}}}
    with caplog.at_level(logging.WARNING, logger="agent"):
        agent._warn_if_world_readable_with_key(str(p), cfg)
    assert any("world-readable" in r.message for r in caplog.records)
