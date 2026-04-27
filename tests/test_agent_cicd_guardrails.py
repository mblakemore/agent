import unittest
from unittest.mock import patch, MagicMock
import agent
import json

class TestAgentCICDGuardrails(unittest.TestCase):
    def setUp(self):
        self.log = MagicMock()
        self.summary_state = {"text": "", "up_to": 0}
        self.initial_files = "test initial files"

    @patch("agent._llm_request")
    @patch("agent.MAP_FN")
    def test_pr_creation_missing_trailer_blocking(self, mock_map_fn, mock_llm_request):
        """
        Test that when 'gh pr create' is called without a 'Closes #N' trailer,
        the agent blocks execution and returns an error message.
        """
        # 1. Setup the LLM to trigger a tool call to exec_command
        mock_response_tool = MagicMock()
        mock_response_tool.status_code = 200

        # Simulate the SSE stream: "data: {...}"
        payload_tool = {
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_123",
                        "type": "function",
                        "function": {
                            "name": "exec_command",
                            "arguments": '{"command": "gh pr create --title \'Fix bug\' --body \'Fixed it\'"}'
                        }
                    }]
                }
            }]
        }
        line_tool = b"data: " + json.dumps(payload_tool).encode("utf-8")
        mock_response_tool.iter_lines.return_value = [line_tool]

        # 2. Setup the LLM to provide a response that triggers a completion signal
        mock_response_final = MagicMock()
        mock_response_final.status_code = 200
        
        payload_final = {
            "choices": [{
                "delta": {
                    "content": "I have fixed the PR body. Cycle complete."
                }
            }]
        }
        line_final = b"data: " + json.dumps(payload_final).encode("utf-8")
        mock_response_final.iter_lines.return_value = [line_final]

        # Provide enough responses to handle any internal retries/nudges
        mock_llm_request.side_effect = [mock_response_tool, mock_response_final, mock_response_final]

        # 3. Mock the command output (should NOT be called)
        mock_exec_command = MagicMock(return_value="Created pull request #456\nexit=0")
        mock_map_fn.__getitem__.side_effect = lambda key: mock_exec_command if key == "exec_command" else None

        history = []
        # Inject builder context into history to satisfy _is_cicd_builder detection
        history.insert(0, {"role": "system", "content": "CICD Improvement Loop — Builder"})
        agent.run_agent_single(
            history,
            self.summary_state,
            self.initial_files,
            self.log,
        )

        # 4. Verify the blocking error is in the tool response
        blocking_found = any(
            "Error: CICD gh pr create blocked" in str(msg.get("content", ""))
            for msg in history if isinstance(msg, dict) and msg.get("role") == "tool"
        )
        self.assertTrue(blocking_found, "The blocking error for missing 'Closes #N' trailer was not found in tool responses")
        
        # 5. Verify the actual tool was never executed
        mock_exec_command.assert_not_called()

    @patch("agent._llm_request")
    @patch("agent.MAP_FN")
    def test_pr_creation_with_trailer_no_warning(self, mock_map_fn, mock_llm_request):
        """
        Test that when 'gh pr create' is called WITH a 'Closes #N' trailer,
        no warning is injected into the conversation history.
        """
        mock_response_tool = MagicMock()
        mock_response_tool.status_code = 200

        payload_tool = {
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_123",
                        "type": "function",
                        "function": {
                            "name": "exec_command",
                            "arguments": '{"command": "gh pr create --title \'Fix bug\' --body \'Fixed it. Closes #123\'"}'
                        }
                    }]
                }
            }]
        }
        line_tool = b"data: " + json.dumps(payload_tool).encode("utf-8")
        mock_response_tool.iter_lines.return_value = [line_tool]
        
        mock_response_final = MagicMock()
        mock_response_final.status_code = 200
        
        payload_final = {
            "choices": [{
                "delta": {
                    "content": "Done. Cycle complete."
                }
            }]
        }
        line_final = b"data: " + json.dumps(payload_final).encode("utf-8")
        mock_response_final.iter_lines.return_value = [line_final]

        mock_llm_request.side_effect = [mock_response_tool, mock_response_final, mock_response_final]

        mock_exec_command = MagicMock(return_value="Created pull request #456\nexit=0")
        mock_map_fn.__getitem__.side_effect = lambda key: mock_exec_command if key == "exec_command" else None

        history = []
        agent.run_agent_single(
            history,
            self.summary_state,
            self.initial_files,
            self.log,
        )

        warning_found = any(
            "was created without a `Closes #<issue>` trailer" in str(msg.get("content", ""))
            for msg in history if isinstance(msg, dict)
        )
        self.assertFalse(warning_found, "The guardrail warning should NOT be injected when the trailer is present")

if __name__ == "__main__":
    unittest.main()
