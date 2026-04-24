import unittest
from unittest import mock
import sys
from pathlib import Path

# Ensure the agent directory is in path
sys.path.insert(0, str(Path(__file__).parent.parent))

import tui
import agent

class TestTuiFallback(unittest.TestCase):
    def test_run_agent_interactive_tui_fallback_notice(self):
        """Verify that the agent emits a warning notice when TUI is requested but prompt_toolkit is missing."""
        
        # We patch tui._AVAILABLE to False. 
        # Since agent imports tui inside the function, this will be picked up.
        with mock.patch('tui._AVAILABLE', False):
            # We patch _emit and input to prevent the agent from actually running/blocking
            with mock.patch('agent._emit') as mock_emit, \
                 mock.patch('builtins.input', side_effect=['quit']):
                
                try:
                    agent.run_agent_interactive(tui=True, auto=False)
                except SystemExit:
                    pass
                except Exception as e:
                    print(f"Caught exception: {e}")
                
                # The exact string from agent.py:2104-2108
                mock_emit.assert_any_call(
                    "on_notice", 
                    "warn", 
                    "prompt_toolkit not installed — using plain prompt. `pip install prompt_toolkit` (or pass --no-tui to silence)."
                )

if __name__ == "__main__":
    unittest.main()
