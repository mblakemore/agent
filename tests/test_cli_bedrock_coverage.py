import pytest
from cli_bedrock import main
import sys
from unittest.mock import patch

def test_cli_bedrock_help():
    with patch('sys.argv', ['cli_bedrock.py', '--help']):
        with pytest.raises(SystemExit) as e:
            main()
        assert e.value.code == 0

def test_cli_bedrock_no_args():
    with patch('sys.argv', ['cli_bedrock.py']):
        with pytest.raises(SystemExit) as e:
            main()
        # Assuming it exits with error or shows help
        assert e.value.code in [0, 1, 2]
