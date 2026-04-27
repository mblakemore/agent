import json
import sys
import pytest
from unittest.mock import MagicMock, patch
import cli_bedrock
import bedrock_store as bs

@pytest.fixture
def mock_store():
    with patch("bedrock_store.load_store") as mock_load, \
         patch("bedrock_store.write_store") as mock_write, \
         patch("bedrock_store.with_locked_store") as mock_lock:
        
        # Simple in-memory store
        store_data = {"entries": []}
        
        def side_effect_lock():
            class LockedStore:
                def __enter__(self):
                    return (store_data, MagicMock())
                def __exit__(self, *args):
                    pass
            return LockedStore()

        mock_load.return_value = store_data
        mock_lock.return_value = side_effect_lock()
        yield store_data, mock_write

def test_maybe_dispatch_no_bedrock():
    # Should return None for non-bedrock commands
    assert cli_bedrock.maybe_dispatch(["other", "cmd"]) is None
    assert cli_bedrock.maybe_dispatch([]) is None

def test_cmd_add_success(mock_store):
    store, mock_write = mock_store
    args = MagicMock()
    args.name = "test-entry"
    args.url = "http://api.test"
    args.key = "secret-key"
    
    with patch("bedrock_store.health_check", return_value=(True, None)):
        result = cli_bedrock.cmd_add(args)
        
    assert result == 0
    assert len(store["entries"]) == 1
    assert store["entries"][0]["name"] == "test-entry"
    assert store["entries"][0]["status"] == bs.STATUS_UP
    mock_write.assert_called()

def test_cmd_add_health_fail(mock_store):
    store, mock_write = mock_store
    args = MagicMock()
    args.name = "fail-entry"
    args.url = "http://api.fail"
    args.key = "secret-key"
    
    with patch("bedrock_store.health_check", return_value=(False, "Connection Error")):
        result = cli_bedrock.cmd_add(args)
        
    assert result == 0
    assert store["entries"][0]["status"] == bs.STATUS_DOWN
    assert store["entries"][0]["last_error"] == "Connection Error"

def test_cmd_list_json(mock_store):
    store, _ = mock_store
    store["entries"] = [{"name": "e1", "status": "up", "daily_spend_usd": 1.0}]
    args = MagicMock()
    args.as_json = True
    
    with patch("builtins.print") as mock_print:
        result = cli_bedrock.cmd_list(args)
        
    assert result == 0
    # Verify JSON was printed
    printed_text = mock_print.call_args_list[0][0][0]
    assert "e1" in printed_text

def test_cmd_list_table(mock_store):
    store, _ = mock_store
    store["entries"] = [{"name": "e1", "status": "up", "daily_spend_usd": 1.0}]
    args = MagicMock()
    args.as_json = False
    
    with patch("builtins.print") as mock_print:
        result = cli_bedrock.cmd_list(args)
        
    assert result == 0
    # Verify table headers printed
    printed_text = mock_print.call_args_list[0][0][0]
    assert "NAME" in printed_text
    assert "e1" in printed_text

def test_cmd_rm_yes(mock_store):
    store, mock_write = mock_store
    store["entries"] = [{"name": "to-delete"}]
    args = MagicMock()
    args.name = "to-delete"
    args.yes = True
    
    result = cli_bedrock.cmd_rm(args)
    assert result == 0
    assert len(store["entries"]) == 0
    mock_write.assert_called()

def test_cmd_rm_no_confirm(mock_store):
    store, _ = mock_store
    store["entries"] = [{"name": "keep-me"}]
    args = MagicMock()
    args.name = "keep-me"
    args.yes = False
    
    with patch("builtins.input", return_value="n"):
        result = cli_bedrock.cmd_rm(args)
        
    assert result == 1
    assert len(store["entries"]) == 1

def test_cmd_retest_all(mock_store):
    store, mock_write = mock_store
    store["entries"] = [
        {"name": "e1", "url": "u1", "key": "k1", "status": "down"},
        {"name": "e2", "url": "u2", "key": "k2", "status": "down"},
    ]
    args = MagicMock()
    args.name = None
    args.all_entries = True
    
    with patch("bedrock_store.health_check", return_value=(True, None)):
        result = cli_bedrock.cmd_retest(args)
        
    assert result == 0
    assert all(e["status"] == bs.STATUS_UP for e in store["entries"])
    mock_write.assert_called()

def test_cmd_prune_yes(mock_store):
    store, mock_write = mock_store
    # Entry e1 is stale, e2 is not
    store["entries"] = [
        {"name": "stale", "status": "down", "last_checked": "2000-01-01T00:00:00Z"},
        {"name": "fresh", "status": "up", "last_checked": "2026-01-01T00:00:00Z"},
    ]
    args = MagicMock()
    args.stale_days = 30
    args.yes = True
    
    # Mock is_stale to ensure it hits the logic
    with patch("bedrock_store.is_stale") as mock_stale:
        mock_stale.side_effect = lambda e, d: e["name"] == "stale"
        result = cli_bedrock.cmd_prune(args)
        
    assert result == 0
    assert len(store["entries"]) == 1
    assert store["entries"][0]["name"] == "fresh"
    mock_write.assert_called()
