import unittest
from unittest.mock import patch, MagicMock
import json
import logging
import traceback

# Setup logging to avoid noise during tests
logging.basicConfig(level=logging.ERROR)

import agent

class TestAgentGitSafeguard(unittest.TestCase):

    @patch('agent._llm_request')
    @patch('agent._config')
    @patch('agent._emit')
    def test_git_commit_no_changes_safeguard(self, mock_emit, mock_config, mock_llm):
        """
        Test that the agent receives a system message when 'git commit' 
        is called but no changes are staged.
        """
        # 1. Robust Config Mock
        # Use a side_effect that returns a MagicMock for any unknown key to avoid NoneType errors.
        def config_side_effect(k):
            cfg = {
                "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
                "context": {"max_tokens": 4096, "ctx_size": 32768},
                "llm": {"model": "mock-model", "api_key": "mock-key"},
                "summary": {"enabled": False},
            }
            return cfg.get(k, MagicMock())

        mock_config.__getitem__.side_effect = config_side_effect

        # 2. Robust LLM Response Setup
        tool_call_json = {
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_123",
                        "type": "function",
                        "function": {"name": "exec_command", "arguments": json.dumps({"command": "git commit -m 'fix'"})}
                    }]
                }
            }]
        }
        
        mock_response = MagicMock()
        mock_response.iter_lines.return_value = [b"data: " + json.dumps(tool_call_json).encode('utf-8'), b"data: [DONE]"]
        
        # Use a queue of responses to avoid call_count issues
        responses = [mock_response]
        
        # Create the final stop response
        stop_response = MagicMock()
        stop_response.iter_lines.return_value = [
            b'data: {"choices": [{"delta": {"content": "Done."}}]}',
            b'data: [DONE]'
        ]
        responses.append(stop_response)

        def llm_side_effect(*args, **kwargs):
            if not responses:
                return stop_response
            return responses.pop(0)

        mock_llm.side_effect = llm_side_effect

        # 3. Mock the exec_command tool via MAP_FN
        mock_exec = MagicMock(return_value="nothing to commit, working tree clean\nno changes added to commit")
        
        # Use patch.dict to inject the mock into the tool mapping
        with patch.dict(agent.MAP_FN, {"exec_command": mock_exec}):
            # 4. Run the agent single loop
            conversation_history = [{"role": "user", "content": "Please commit the changes."}]
            summary_state = {"text": "", "up_to": 0}
            logger = logging.getLogger("test_logger")
            
            try:
                agent.run_agent_single(conversation_history, summary_state, [], logger)
            except Exception:
                traceback.print_exc()
                # We don't raise here to allow the final assertion to tell us if it worked
        
        # 5. Verify the safeguard triggered
        system_messages = [msg['content'] for msg in conversation_history if msg['role'] == 'user']
        found_safeguard = any("git commit failed — no test files were staged" in msg for msg in system_messages)
        
        self.assertTrue(found_safeguard, "The agent should have received the git commit safeguard system message.")

if __name__ == '__main__':
    unittest.main()
