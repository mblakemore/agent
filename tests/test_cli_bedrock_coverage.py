import pytest
from cli_bedrock import run
import sys
from unittest.mock import patch

def test_cli_bedrock_help():
    # run() takes argv without the leading 'bedrock' token.
    # For --help, we pass it as a list.
    with patch('sys.argv', ['cli_bedrock.py', 'bedrock', '--help']):
        with pytest.raises(SystemExit) as e:
            run(['--help'])
        assert e.value.code == 0

def test_cli_bedrock_no_args():
    # argparse required=True for subparsers, so no args should cause a SystemExit
    with pytest.raises(SystemExit) as e:
        run([])
    assert e.value.code != 0
