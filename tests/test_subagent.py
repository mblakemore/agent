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
        """Integer prompt must return a type-specific error, not 'must be non-empty string' (#911)."""
        result = subagent(42)
        self.assertTrue(result.startswith("Error:"), f"Expected error: {result!r}")
        self.assertIn("string", result, f"Error must mention 'string': {result!r}")
        self.assertIn("int", result, f"Error must name the bad type: {result!r}")

    def test_subagent_none_prompt(self):
        """None prompt must return a type-specific error (#911)."""
        result = subagent(None)
        self.assertTrue(result.startswith("Error:"), f"Expected error: {result!r}")
        self.assertIn("string", result, f"Error must mention 'string': {result!r}")
        self.assertIn("NoneType", result, f"Error must name the bad type: {result!r}")

    def test_subagent_list_prompt(self):
        """List prompt must return a type-specific error (#911)."""
        result = subagent(["do something"])
        self.assertTrue(result.startswith("Error:"), f"Expected error: {result!r}")
        self.assertIn("string", result, f"Error must mention 'string': {result!r}")
        self.assertIn("list", result, f"Error must name the bad type: {result!r}")

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

    # ── Timeout tests ──────────────────────────────────────────────────────────

    @patch('subprocess.run')
    def test_subagent_timeout_passed_to_subprocess(self, mock_run):
        """subprocess.run must be called with the timeout kwarg so hung agents don't block forever."""
        def side_effect(args, **kwargs):
            # Record that timeout was passed, then simulate success
            idx = args.index("--result-file")
            file_path = args[idx + 1]
            with open(file_path, "w") as f:
                f.write("done")
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        subagent("Test prompt")

        call_kwargs = mock_run.call_args[1]
        self.assertIn("timeout", call_kwargs, "subprocess.run must receive a 'timeout' kwarg")
        self.assertGreater(call_kwargs["timeout"], 0, "timeout must be positive")

    @patch('subprocess.run')
    def test_subagent_timeout_expired_returns_error(self, mock_run):
        """TimeoutExpired from subprocess.run must be caught and return an error string."""
        import subprocess as _sp
        mock_run.side_effect = _sp.TimeoutExpired(cmd="agent", timeout=5)
        result = subagent("Test prompt", timeout=5)
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix, got: {result!r}")
        self.assertIn("timed out", result)
        self.assertIn("5", result)

    def test_subagent_timeout_zero_rejected(self):
        """timeout=0 must be rejected before launching subprocess."""
        with patch("subprocess.run") as mock_run:
            result = subagent("Test prompt", timeout=0)
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix, got: {result!r}")
        mock_run.assert_not_called()

    def test_subagent_timeout_negative_rejected(self):
        """Negative timeout must be rejected before launching subprocess."""
        with patch("subprocess.run") as mock_run:
            result = subagent("Test prompt", timeout=-1)
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix, got: {result!r}")
        mock_run.assert_not_called()

    def test_subagent_timeout_non_numeric_rejected(self):
        """Non-numeric timeout must be rejected before launching subprocess."""
        with patch("subprocess.run") as mock_run:
            result = subagent("Test prompt", timeout="fast")
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix, got: {result!r}")
        mock_run.assert_not_called()

    def test_subagent_timeout_bool_rejected(self):
        """Boolean timeout (True/False) must be rejected as non-numeric."""
        with patch("subprocess.run") as mock_run:
            result = subagent("Test prompt", timeout=True)
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix, got: {result!r}")
        mock_run.assert_not_called()

    @patch('subprocess.run')
    def test_subagent_custom_timeout_used(self, mock_run):
        """A custom timeout value must be forwarded to subprocess.run."""
        def side_effect(args, **kwargs):
            idx = args.index("--result-file")
            file_path = args[idx + 1]
            with open(file_path, "w") as f:
                f.write("result")
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        subagent("Test prompt", timeout=30)
        call_kwargs = mock_run.call_args[1]
        self.assertEqual(call_kwargs["timeout"], 30)

class TestSubagentNaNInfTimeout(unittest.TestCase):
    """NaN and Inf timeout values must be caught before subprocess.run (#891).

    Before the fix, both values passed the 'timeout <= 0' guard (NaN and Inf
    comparisons return False) and reached subprocess.run() which raised
    ValueError/OverflowError, producing obscure 'cannot convert float NaN to
    integer' messages caught by the outer except-handler.
    """

    def test_nan_timeout_returns_clear_error(self):
        """timeout=float('nan') must return a descriptive error, not a confusing
        'cannot convert float NaN to integer' message (#891)."""
        import math
        result = subagent("echo hi", timeout=math.nan)
        self.assertTrue(result.startswith("Error:"), f"Expected error: {result!r}")
        self.assertIn("finite", result, f"Error should mention 'finite': {result!r}")

    def test_inf_timeout_returns_clear_error(self):
        """timeout=float('inf') must return a descriptive error (#891)."""
        import math
        result = subagent("echo hi", timeout=math.inf)
        self.assertTrue(result.startswith("Error:"), f"Expected error: {result!r}")
        self.assertIn("finite", result, f"Error should mention 'finite': {result!r}")

    def test_negative_timeout_still_rejected(self):
        """A negative timeout must continue to be rejected normally (#891)."""
        result = subagent("echo hi", timeout=-1)
        self.assertTrue(result.startswith("Error:"), f"Expected error: {result!r}")
        self.assertNotIn("nan", result.lower())
        self.assertNotIn("inf", result.lower())


class TestSubagentNoneTimeout(unittest.TestCase):
    """Issue #948: timeout=None must coerce to _DEFAULT_TIMEOUT, not return a type error."""

    def test_timeout_none_does_not_return_type_error(self):
        """timeout=None must not immediately return a type error (#948)."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = RuntimeError("subprocess skipped in this test")
            result = subagent("test", timeout=None)
        self.assertNotIn("NoneType", result, f"timeout=None must not produce type error: {result!r}")

    @patch('subprocess.run')
    def test_timeout_none_passes_default_to_subprocess(self, mock_run):
        """timeout=None must coerce to _DEFAULT_TIMEOUT (600) so subprocess gets the right value (#948)."""
        def side_effect(args, **kwargs):
            try:
                idx = args.index("--result-file")
                file_path = args[idx + 1]
                with open(file_path, "w") as f:
                    f.write("ok")
            except (ValueError, IndexError):
                pass
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        subagent("test task", timeout=None)
        call_kwargs = mock_run.call_args[1]
        self.assertEqual(call_kwargs.get("timeout"), 600,
                         f"timeout=None must coerce to 600, got {call_kwargs.get('timeout')}")


if __name__ == '__main__':
    unittest.main()
