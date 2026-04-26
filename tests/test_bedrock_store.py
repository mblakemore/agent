"""Tests for the bedrock credential store (issue #405).

10 cases per the issue's "Tests required" section. Network is mocked
throughout so no real HTTP is issued.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


# Make sure the worktree root is importable for `bedrock_store`,
# `cli_bedrock`, and `llm_backend` regardless of how pytest is invoked.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import bedrock_store as bs  # noqa: E402
import cli_bedrock  # noqa: E402


@pytest.fixture
def store_path(tmp_path, monkeypatch):
    p = tmp_path / "creds.json"
    monkeypatch.setenv("AGENT_BEDROCK_STORE", str(p))
    return p


def _make_store(path: Path, entries: list[dict]) -> None:
    bs.write_store({"entries": entries}, path)


# ── Case 1: empty store + env vars → BedrockBackend constructs from env ──


def test_empty_store_falls_back_to_env(store_path, monkeypatch):
    """Issue #405 criterion 2 — back-compat path."""
    assert not store_path.exists()
    monkeypatch.setenv("BEDROCK_API_URL", "https://env.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)

    from llm_backend import BedrockBackend
    b = BedrockBackend({"kind": "bedrock", "model": "claude-v4.5-haiku"})
    # ``api_url`` was trimmed of trailing slash but otherwise unchanged.
    assert b.api_url == "https://env.example.com/api"
    # No store entry — running in implicit ``env`` mode.
    assert b._store_entry_name is None


# ── Case 2: store with 2 up entries → lowest spend wins ──


def test_two_up_entries_picks_lowest_spend(store_path, monkeypatch):
    monkeypatch.delenv("BEDROCK_API_URL", raising=False)
    monkeypatch.delenv("BEDROCK_API_KEY", raising=False)
    _make_store(store_path, [
        {
            "name": "g1", "url": "https://g1.example/api", "key": "K1",
            "status": "up", "last_checked": "2026-04-26T10:00:00Z",
            "last_error": None, "daily_spend_usd": 5.0,
        },
        {
            "name": "g2", "url": "https://g2.example/api", "key": "K2",
            "status": "up", "last_checked": "2026-04-26T11:00:00Z",
            "last_error": None, "daily_spend_usd": 2.0,
        },
    ])
    from llm_backend import BedrockBackend
    b = BedrockBackend({"kind": "bedrock", "model": "claude-v4.5-haiku"})
    assert b._store_entry_name == "g2"
    assert b.api_url == "https://g2.example/api"


# ── Case 3: 1 up + 1 down → up one selected, never tries the down ──


def test_skips_down_entries(store_path, monkeypatch):
    monkeypatch.delenv("BEDROCK_API_URL", raising=False)
    monkeypatch.delenv("BEDROCK_API_KEY", raising=False)
    _make_store(store_path, [
        {
            "name": "g1", "url": "https://g1.example/api", "key": "K1",
            "status": "down", "last_checked": "2026-04-26T10:00:00Z",
            "last_error": "timeout", "daily_spend_usd": 0.0,
        },
        {
            "name": "g2", "url": "https://g2.example/api", "key": "K2",
            "status": "up", "last_checked": "2026-04-26T11:00:00Z",
            "last_error": None, "daily_spend_usd": 99.0,
        },
    ])
    from llm_backend import BedrockBackend
    b = BedrockBackend({"kind": "bedrock", "model": "claude-v4.5-haiku"})
    assert b._store_entry_name == "g2"


# ── Case 4: all-down + env unset → ConfigError ──


def test_all_down_no_env_raises_config_error(store_path, monkeypatch):
    monkeypatch.delenv("BEDROCK_API_URL", raising=False)
    monkeypatch.delenv("BEDROCK_API_KEY", raising=False)
    _make_store(store_path, [
        {
            "name": "g1", "url": "https://g1.example/api", "key": "K1",
            "status": "down", "last_checked": "2026-04-26T10:00:00Z",
            "last_error": "timeout", "daily_spend_usd": 0.0,
        },
    ])
    from llm_backend import BedrockBackend, ConfigError
    with pytest.raises(ConfigError):
        BedrockBackend({"kind": "bedrock", "model": "claude-v4.5-haiku"})


# ── Case 5: `add` writes 0600 perms, runs health check, sets status ──


def test_add_writes_0600_and_runs_health_check(store_path, capsys):
    args = type("A", (), {
        "name": "test1",
        "url": "https://example/api",
        "key": "xxx",
    })()
    with patch("bedrock_store.health_check", return_value=(True, None)):
        rc = cli_bedrock.cmd_add(args)
    assert rc == 0
    assert store_path.exists()
    mode = stat.S_IMODE(store_path.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    data = bs.load_store(store_path)
    entries = data["entries"]
    assert len(entries) == 1
    assert entries[0]["name"] == "test1"
    assert entries[0]["status"] == "up"
    assert entries[0]["last_checked"] is not None


def test_add_marks_down_when_health_check_fails(store_path):
    args = type("A", (), {
        "name": "bad",
        "url": "https://bad/api",
        "key": "kkk",
    })()
    with patch("bedrock_store.health_check", return_value=(False, "HTTP 500")):
        rc = cli_bedrock.cmd_add(args)
    assert rc == 0
    data = bs.load_store(store_path)
    assert data["entries"][0]["status"] == "down"
    assert data["entries"][0]["last_error"] == "HTTP 500"


# ── Case 6: `rm` removes entry; missing name → exit 1 ──


def test_rm_removes_entry(store_path):
    _make_store(store_path, [
        {"name": "g1", "url": "u1", "key": "k1", "status": "up",
         "last_checked": None, "last_error": None, "daily_spend_usd": 0.0},
        {"name": "g2", "url": "u2", "key": "k2", "status": "up",
         "last_checked": None, "last_error": None, "daily_spend_usd": 0.0},
    ])
    args = type("A", (), {"name": "g1", "yes": True})()
    rc = cli_bedrock.cmd_rm(args)
    assert rc == 0
    data = bs.load_store(store_path)
    assert [e["name"] for e in data["entries"]] == ["g2"]


def test_rm_missing_name_exit_1(store_path):
    _make_store(store_path, [])
    args = type("A", (), {"name": "ghost", "yes": True})()
    rc = cli_bedrock.cmd_rm(args)
    assert rc == 1


# ── Case 7: `retest --all` updates statuses based on mocked endpoint ──


def test_retest_all_updates_statuses(store_path):
    _make_store(store_path, [
        {"name": "g1", "url": "https://g1/api", "key": "K1", "status": "down",
         "last_checked": "2020-01-01T00:00:00Z", "last_error": "old",
         "daily_spend_usd": 0.0},
        {"name": "g2", "url": "https://g2/api", "key": "K2", "status": "up",
         "last_checked": "2020-01-01T00:00:00Z", "last_error": None,
         "daily_spend_usd": 0.0},
    ])

    def fake_check(url, key, **kwargs):
        # g1 recovers, g2 dies.
        if "g1" in url:
            return True, None
        return False, "HTTP 503"

    args = type("A", (), {"name": None, "all_entries": True})()
    with patch("bedrock_store.health_check", side_effect=fake_check):
        rc = cli_bedrock.cmd_retest(args)
    assert rc == 0
    data = bs.load_store(store_path)
    by_name = {e["name"]: e for e in data["entries"]}
    assert by_name["g1"]["status"] == "up"
    assert by_name["g2"]["status"] == "down"
    assert by_name["g2"]["last_error"] == "HTTP 503"


# ── Case 8: `prune --stale-days N` only removes down-and-old ──


def test_prune_only_removes_stale_down(store_path):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _make_store(store_path, [
        # Down + old -> prune.
        {"name": "old_down", "url": "u", "key": "k", "status": "down",
         "last_checked": old, "last_error": "x", "daily_spend_usd": 0.0},
        # Down + recent -> keep.
        {"name": "recent_down", "url": "u", "key": "k", "status": "down",
         "last_checked": recent, "last_error": "x", "daily_spend_usd": 0.0},
        # Up + old -> keep (status==up).
        {"name": "old_up", "url": "u", "key": "k", "status": "up",
         "last_checked": old, "last_error": None, "daily_spend_usd": 0.0},
    ])
    args = type("A", (), {"stale_days": 30, "yes": True})()
    rc = cli_bedrock.cmd_prune(args)
    assert rc == 0
    data = bs.load_store(store_path)
    names = sorted(e["name"] for e in data["entries"])
    assert names == ["old_up", "recent_down"]


# ── Case 9: atomic write — store remains valid if .tmp is killed mid-write ──


def test_atomic_write_no_corruption(store_path):
    # Seed the store, then simulate a crash during a subsequent write by
    # raising mid-writefile. The original file must still parse.
    _make_store(store_path, [
        {"name": "g1", "url": "u1", "key": "k1", "status": "up",
         "last_checked": "2026-04-26T10:00:00Z", "last_error": None,
         "daily_spend_usd": 1.0},
    ])
    # Write a partial tmp file that os.replace would have completed.
    tmp = store_path.with_suffix(store_path.suffix + ".tmp")
    tmp.write_text("{ this is not valid json", encoding="utf-8")
    # Live file must still parse correctly.
    data = bs.load_store(store_path)
    assert len(data["entries"]) == 1
    assert data["entries"][0]["name"] == "g1"
    # And the original write_store path leaves the live file consistent
    # even after a simulated mid-write failure.
    with patch("os.fsync", side_effect=OSError("simulated kill")):
        with pytest.raises(OSError):
            bs.write_store({"entries": [{"name": "x"}]}, store_path)
    # The live file is still the original.
    data = bs.load_store(store_path)
    assert data["entries"][0]["name"] == "g1"


# ── Case 10: concurrent flock — two processes don't corrupt the store ──


def _child_add(path_str, name):
    """Helper: add an entry from a child process under flock."""
    os.environ["AGENT_BEDROCK_STORE"] = path_str
    # Re-import inside the fork to make sure env override is fresh.
    import importlib
    import bedrock_store as _bs
    importlib.reload(_bs)
    with _bs.with_locked_store() as locked:
        if locked is None:
            return
        data, p = locked
        try:
            _bs.add_entry(data, name=name, url=f"https://{name}/api", key="k" * 8)
        except ValueError:
            return
        _bs.write_store(data, p)


def test_concurrent_flock_no_corruption(store_path):
    # Seed the file so both children see a consistent starting state.
    _make_store(store_path, [])
    ctx = multiprocessing.get_context("fork")
    procs = [
        ctx.Process(target=_child_add, args=(str(store_path), f"p{i}"))
        for i in range(4)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=10)
        assert p.exitcode == 0, f"child exited {p.exitcode}"
    # File must parse and contain all 4 names — no lost writes, no
    # corrupted JSON.
    raw = store_path.read_text(encoding="utf-8")
    data = json.loads(raw)
    names = sorted(e["name"] for e in data["entries"])
    assert names == ["p0", "p1", "p2", "p3"]


# ── Bonus coverage: rotation hook, selection helpers, lock timeout ──


def test_select_credentials_lookup_error_when_empty(store_path, monkeypatch):
    monkeypatch.delenv("BEDROCK_API_URL", raising=False)
    monkeypatch.delenv("BEDROCK_API_KEY", raising=False)
    with pytest.raises(LookupError):
        bs.select_credentials(env_url="", env_key="")


def test_mark_status_round_trips(store_path):
    _make_store(store_path, [
        {"name": "g1", "url": "u", "key": "k", "status": "up",
         "last_checked": None, "last_error": None, "daily_spend_usd": 0.0},
    ])
    assert bs.mark_status("g1", status="down", last_error="boom")
    data = bs.load_store(store_path)
    e = data["entries"][0]
    assert e["status"] == "down"
    assert e["last_error"] == "boom"
    assert e["last_checked"] is not None


def test_rotate_marks_down_and_swaps(store_path, monkeypatch):
    monkeypatch.delenv("BEDROCK_API_URL", raising=False)
    monkeypatch.delenv("BEDROCK_API_KEY", raising=False)
    _make_store(store_path, [
        {"name": "g1", "url": "https://g1/api", "key": "K1", "status": "up",
         "last_checked": "2026-04-26T10:00:00Z", "last_error": None,
         "daily_spend_usd": 0.0},
        {"name": "g2", "url": "https://g2/api", "key": "K2", "status": "up",
         "last_checked": "2026-04-26T10:00:00Z", "last_error": None,
         "daily_spend_usd": 1.0},
    ])
    from llm_backend import BedrockBackend
    b = BedrockBackend({"kind": "bedrock", "model": "claude-v4.5-haiku"})
    # Lowest spend wins -> g1.
    assert b._store_entry_name == "g1"
    import logging
    rotated = b._rotate_to_next_entry(TimeoutError("boom"), logging.getLogger("test"))
    assert rotated is True
    assert b._store_entry_name == "g2"
    assert b.api_url == "https://g2/api"
    # g1 is now marked down on disk.
    data = bs.load_store(store_path)
    by_name = {e["name"]: e for e in data["entries"]}
    assert by_name["g1"]["status"] == "down"
    assert "TimeoutError" in (by_name["g1"]["last_error"] or "")


def test_rotate_returns_false_when_no_alt_entry(store_path, monkeypatch):
    monkeypatch.delenv("BEDROCK_API_URL", raising=False)
    monkeypatch.delenv("BEDROCK_API_KEY", raising=False)
    _make_store(store_path, [
        {"name": "g1", "url": "https://g1/api", "key": "K1", "status": "up",
         "last_checked": "2026-04-26T10:00:00Z", "last_error": None,
         "daily_spend_usd": 0.0},
    ])
    from llm_backend import BedrockBackend
    b = BedrockBackend({"kind": "bedrock", "model": "claude-v4.5-haiku"})
    import logging
    rotated = b._rotate_to_next_entry(TimeoutError("boom"), logging.getLogger("test"))
    assert rotated is False


def test_cli_list_outputs_table(store_path, capsys):
    _make_store(store_path, [
        {"name": "g1", "url": "u1", "key": "k1", "status": "up",
         "last_checked": "2026-04-26T10:00:00Z", "last_error": None,
         "daily_spend_usd": 1.23},
    ])
    args = type("A", (), {"as_json": False})()
    rc = cli_bedrock.cmd_list(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "g1" in out
    assert "up" in out


def test_cli_list_json(store_path, capsys):
    _make_store(store_path, [
        {"name": "g1", "url": "u1", "key": "k1", "status": "up",
         "last_checked": None, "last_error": None, "daily_spend_usd": 0.0},
    ])
    args = type("A", (), {"as_json": True})()
    rc = cli_bedrock.cmd_list(args)
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["entries"][0]["name"] == "g1"


def test_health_check_treats_2xx_as_up():
    """Criterion 7 — any 2xx is up."""
    import requests
    fake = type("R", (), {"status_code": 204})()
    with patch.object(requests, "get", return_value=fake):
        ok, err = bs.health_check("https://x/api", "k")
    assert ok is True
    assert err is None


def test_health_check_500_is_down():
    import requests
    fake = type("R", (), {"status_code": 500})()
    with patch.object(requests, "get", return_value=fake):
        ok, err = bs.health_check("https://x/api", "k")
    assert ok is False
    assert "500" in err


def test_is_stale_predicate():
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert bs.is_stale(
        {"status": "down", "last_checked": old}, 30
    )
    assert not bs.is_stale(
        {"status": "down", "last_checked": recent}, 30
    )
    assert not bs.is_stale(
        {"status": "up", "last_checked": old}, 30
    )
