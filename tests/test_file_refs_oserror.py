"""Tests for _expand_file_refs OSError path."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))
import agent

class TestExpandFileRefsOSError(unittest.TestCase):
    def test_os_error_during_resolve_returns_error(self):
        # We need to avoid mocking Path.cwd().resolve() because that's called first
        # We want to mock the resolve() call on the specific Path object created from the ref.
        
        with patch("pathlib.Path.resolve") as mock_resolve:
            # First call is for cwd_resolved = Path.cwd().resolve()
            # Second call is for resolved_ref = p.resolve() inside the loop
            # We can use side_effect to return a value for the first call and raise for the second.
            
            mock_cwd = Path.cwd().resolve()
            mock_resolve.side_effect = [mock_cwd, OSError("Mocked OS Error")]
            
            text, files, err = agent._expand_file_refs("@some_file")
            self.assertIsNone(text)
            self.assertIsNone(files)
            self.assertIsNotNone(err)
            self.assertIn("Error: 'some_file': Mocked OS Error", err)

if __name__ == "__main__":
    unittest.main()
