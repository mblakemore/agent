import pytest
from cli_bedrock import run, cmd_list, cmd_add, cmd_rm, cmd_retest, cmd_prune
import argparse
from unittest.mock import patch, MagicMock

def test_cli_bedrock_help():
    with pytest.raises(SystemExit) as e:
        run(['--help'])
    assert e.value.code == 0

def test_cli_bedrock_no_args():
    with pytest.raises(SystemExit) as e:
        run([])
    assert e.value.code != 0

def test_cmd_list_json():
    with patch('bedrock_store.load_store') as mock_load:
        mock_load.return_value = {"entries": [{"name": "test", "status": "up"}]}
        args = argparse.Namespace(as_json=True)
        with patch('builtins.print') as mock_print:
            cmd_list(args)
            mock_print.assert_called()

def test_cmd_list_table():
    with patch('bedrock_store.load_store') as mock_load:
        mock_load.return_value = {"entries": [{"name": "test", "status": "up", "daily_spend_usd": 1.23}]}
        args = argparse.Namespace(as_json=False)
        with patch('builtins.print') as mock_print:
            cmd_list(args)
            mock_print.assert_called()

def test_cmd_add_success():
    with patch('bedrock_store.with_locked_store') as mock_lock, \
         patch('bedrock_store.add_entry') as mock_add, \
         patch('bedrock_store.health_check') as mock_health, \
         patch('bedrock_store.write_store') as mock_write:
        
        mock_lock.return_value.__enter__.return_value = ({"entries": []}, "path")
        mock_add.return_value = {"name": "t", "url": "u", "key": "k"}
        mock_health.return_value = (True, None)
        
        args = argparse.Namespace(name="t", url="u", key="k")
        assert cmd_add(args) == 0
        mock_write.assert_called()

def test_cmd_rm_yes():
    with patch('bedrock_store.with_locked_store') as mock_lock, \
         patch('bedrock_store.remove_entry') as mock_rm, \
         patch('bedrock_store.write_store') as mock_write:
        
        mock_lock.return_value.__enter__.return_value = ({"entries": []}, "path")
        mock_rm.return_value = True
        
        args = argparse.Namespace(name="t", yes=True)
        assert cmd_rm(args) == 0
        mock_write.assert_called()

def test_cmd_retest_all():
    with patch('bedrock_store.with_locked_store') as mock_lock, \
         patch('bedrock_store.health_check') as mock_health, \
         patch('bedrock_store.write_store') as mock_write:
        
        mock_lock.return_value.__enter__.return_value = ({"entries": [{"name": "t", "url": "u", "key": "k"}]}, "path")
        mock_health.return_value = (True, None)
        
        args = argparse.Namespace(name=None, all_entries=True)
        assert cmd_retest(args) == 0
        mock_write.assert_called()

def test_cmd_prune_yes():
    with patch('bedrock_store.load_store') as mock_load, \
         patch('bedrock_store.is_stale') as mock_stale, \
         patch('bedrock_store.with_locked_store') as mock_lock, \
         patch('bedrock_store.write_store') as mock_write:
        
        mock_load.return_value = {"entries": [{"name": "stale"}]}
        mock_stale.return_value = True
        mock_lock.return_value.__enter__.return_value = ({"entries": [{"name": "stale"}]}, "path")
        
        args = argparse.Namespace(stale_days=30, yes=True)
        assert cmd_prune(args) == 0
        mock_write.assert_called()
