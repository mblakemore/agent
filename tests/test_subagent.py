import unittest
from unittest.mock import patch, MagicMock
import os
from tools.subagent import subagent

class TestSubagent(unittest.TestCase):

    @patch('subprocess.run')
    def test_subagent_success(self, mock_run):
        """Path 1: Process succeeds and result file contains data."""
        def side_effect(args, **kwargs):
            try:
                idx = args.index("--result-file")
                file_path = args[idx + 1]
                with open(file_path, "w") as f:
                    f.write("Success result from subagent")
            except (ValueError, IndexError):
                pass
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect

        result = subagent("Test prompt")
        self.assertEqual(result, "Success result from subagent")

    @patch('subprocess.run')
    def test_subagent_empty_result(self, mock_run):
        """Path 2: Process succeeds but result file is empty."""
        def side_effect(args, **kwargs):
            try:
                idx = args.index("--result-file")
                file_path = args[idx + 1]
                with open(file_path, "w") as f:
                    f.write("") # Empty file
            except (ValueError, IndexError):
                pass
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect

        result = subagent("Test prompt")
        self.assertEqual(result, "Error: sub-agent completed but returned no final answer")

    @patch('subprocess.run')
    @patch('os.path.exists')
    def test_subagent_missing_file(self, mock_exists, mock_run):
        """Path 3: Process returns 0 but fails to create the result file."""
        # We simply return returncode=0 without writing any file
        mock_run.return_value = MagicMock(returncode=0)
        mock_exists.return_value = False

        result = subagent("Test prompt")
        self.assertEqual(result, "Error: sub-agent completed but no result file was created")

    @patch('subprocess.run')
    def test_subagent_process_failure(self, mock_run):
        """Path 4: Process returns a non-zero exit code."""
        mock_run.return_value = MagicMock(returncode=1, stderr="Something went wrong")

        result = subagent("Test prompt")
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix, got: {result!r}")
        self.assertIn("exit code 1", result)
        self.assertIn("Something went wrong", result)

    @patch('subprocess.run')
    def test_subagent_exception(self, mock_run):
        """Path 5: subprocess.run raises an exception."""
        mock_run.side_effect = Exception("Unexpected process error")

        result = subagent("Test prompt")
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix, got: {result!r}")
        self.assertIn("Unexpected process error", result)

    def test_subagent_empty_prompt(self):
        """Empty string prompt must fail fast without launching subprocess."""
        result = subagent("")
        self.assertEqual(result, "Error: prompt must not be empty")

    def test_subagent_whitespace_only_prompt(self):
        """Whitespace-only prompt must fail fast without launching subprocess."""
        result = subagent("   ")
        self.assertEqual(result, "Error: prompt must not be empty")

    def test_subagent_integer_prompt(self):
        """Integer prompt must return an error string, not raise AttributeError."""
        result = subagent(42)
        self.assertEqual(result, "Error: prompt must be a non-empty string")

    def test_subagent_none_prompt(self):
        """None prompt must return an error string, not raise AttributeError."""
        result = subagent(None)
        self.assertEqual(result, "Error: prompt must be a non-empty string")

    def test_subagent_list_prompt(self):
        """List prompt must return an error string, not raise AttributeError."""
        result = subagent(["do something"])
        self.assertEqual(result, "Error: prompt must be a non-empty string")

    def test_subagent_null_byte_prompt(self):
        """Null byte in prompt must fail fast without launching subprocess."""
        with patch("subprocess.run") as mock_run:
            result = subagent("test\x00null")
        self.assertIn("Error", result)
        self.assertIn("null byte", result)
        mock_run.assert_not_called()

    # ── Regression tests: error message format (must start with "Error: ") ──

    @patch('subprocess.run')
    def test_subagent_process_failure_error_prefix(self, mock_run):
        """Non-zero exit code must return a string starting with 'Error: '."""
        mock_run.return_value = MagicMock(returncode=2, stderr="crash")
        result = subagent("Test prompt")
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix, got: {result!r}")

    @patch('subprocess.run')
    def test_subagent_empty_result_error_prefix(self, mock_run):
        """Empty result file must return a string starting with 'Error: '."""
        def side_effect(args, **kwargs):
            idx = args.index("--result-file")
            with open(args[idx + 1], "w") as f:
                f.write("")
            return MagicMock(returncode=0)
        mock_run.side_effect = side_effect
        result = subagent("Test prompt")
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix, got: {result!r}")

    @patch('subprocess.run')
    @patch('os.path.exists')
    def test_subagent_missing_file_error_prefix(self, mock_exists, mock_run):
        """Missing result file must return a string starting with 'Error: '."""
        mock_run.return_value = MagicMock(returncode=0)
        mock_exists.return_value = False
        result = subagent("Test prompt")
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix, got: {result!r}")

    @patch('subprocess.run')
    def test_subagent_exception_error_prefix(self, mock_run):
        """Unexpected exception must return a string starting with 'Error: '."""
        mock_run.side_effect = RuntimeError("kaboom")
        result = subagent("Test prompt")
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix, got: {result!r}")
        self.assertIn("kaboom", result)

if __name__ == '__main__':
    unittest.main()
