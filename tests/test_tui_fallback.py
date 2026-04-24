import unittest
from unittest import mock
import sys
import importlib
from pathlib import Path

# Ensure the agent directory is in path
sys.path.insert(0, str(Path(__file__).parent.parent))

import tui
import agent

class TestTuiFallback(unittest.TestCase):
    def test_run_agent_interactive_tui_fallback_notice(self):
        """Verify that the agent emits a warning notice when TUI is requested but prompt_toolkit is missing."""
        
        # 1. Patch tui._AVAILABLE to False
        with mock.patch('tui._AVAILABLE', False):
            # 2. Reload agent to be safe
            importlib.reload(agent)
            
            # 3. Patch _emit and input
            with mock.patch('agent._emit') as mock_emit, \
                 mock.patch('builtins.input', side_effect=['quit']):
                
                try:
                    # Call run_agent_interactive with tui=True, auto=False.
                    agent.run_agent_interactive(tui=True, auto=False)
                except SystemExit:
                    pass
                except Exception as e:
                    print(f"Caught exception: {e}")

                # Verify the notice was emitted.
                # The exact string is: "prompt_toolkit not installed — using plain prompt. `pip install prompt_toolkit` (or pass --no-tui to silence)."
                mock_emit.assert_any_call(
                    "on_notice", 
                    "warn", 
                    "prompt_toolkit not installed — using plain prompt. `pip install prompt_toolkit` (or pass --no-tui to silence)."
                )

if __name__ == "__main__":
    unittest.main()
