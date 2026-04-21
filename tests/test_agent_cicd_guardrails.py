import unittest
from unittest.mock import patch, MagicMock
import agent

class TestAgentCICDGuardrails(unittest.TestCase):
    def setUp(self):
        self.log = MagicMock()
        self.summary_state = {"text": "", "up_to": 0}
        self.initial_files = "test initial files"

    @patch("agent._llm_request")
    @patch("agent.MAP_FN")
    def test_pr_creation_missing_trailer_warning(self, mock_map_fn, mock_llm_request):
        """
        Test that when 'gh pr create' is called without a 'Closes #N' trailer,
        the agent injects a system warning into the conversation history.
        """
        # 1. Setup the LLM to trigger a tool call to exec_command
        mock_response_tool = MagicMock()
        mock_response_tool.status_code = 200
        
        chunk_tool = MagicMock()
        chunk_tool.choices = [MagicMock(delta={
            "content": "", 
            "tool_calls": [{
                "id": "call_123",
                "type": "function",
                "function": {
                    "name": "exec_command",
                    "arguments": '{"command": "gh pr create --title \'Fix bug\' --body \'Fixed it\'"}'
                }
            }]
        })]
        mock_response_tool.__iter__.return_value = [chunk_tool]
        
        # 2. Setup the LLM to terminate after the tool is called
        mock_response_final = MagicMock()
        mock_response_final.status_code = 200
        chunk_final = MagicMock()
        chunk_final.choices = [MagicMock(delta={"content": "I have created the PR."})]
        mock_response_final.__iter__.return_value = [chunk_final]

        mock_llm_request.side_effect = [mock_response_tool, mock_response_final]

        # 3. Mock the command output to trigger the guardrail:
        # - Must contain 'exit=0'
        # - Must contain a PR number (e.g., #456)
        mock_exec_command = MagicMock(return_value="Created pull request #456\nexit=0")
        mock_map_fn.__getitem__.side_effect = lambda key: mock_exec_command if key == "exec_command" else None

        history = []
        # We call run_agent_single. The loop will:
        # - Request LLM -> get tool call
        # - Execute tool -> get "Created pull request #456\nexit=0"
        # - Process result -> trigger guardrail at lines 2460-2485
        # - Request LLM -> get final response -> loop ends (or we mock it to end)
        agent.run_agent_single(
            history,
            self.summary_state,
            self.initial_files,
            self.log,
        )

        # 4. Verify the warning is in the history
        warning_found = any(
            "was created without a `Closes #<issue>` trailer" in str(msg.get("content", ""))
            for msg in history if isinstance(msg, dict)
        )
        self.assertTrue(warning_found, "The guardrail warning for missing 'Closes #N' trailer was not found in history")

    @patch("agent._llm_request")
    @patch("agent.MAP_FN")
    def test_pr_creation_with_trailer_no_warning(self, mock_map_fn, mock_llm_request):
        """
        Test that when 'gh pr create' is called WITH a 'Closes #N' trailer,
        no warning is injected into the conversation history.
        """
        mock_response_tool = MagicMock()
        mock_response_tool.status_code = 200
        chunk_tool = MagicMock()
        chunk_tool.choices = [MagicMock(delta={
            "content": "", 
            "tool_calls": [{
                "id": "call_123",
                "type": "function",
                "function": {
                    "name": "exec_command",
                    "arguments": '{"command": "gh pr create --title \'Fix bug\' --body \'Fixed it. Closes #123\'"}'
                }
            }]
        })]
        mock_response_tool.__iter__.return_value = [chunk_tool]
        
        mock_response_final = MagicMock()
        mock_response_final.status_code = 200
        chunk_final = MagicMock()
        chunk_final.choices = [MagicMock(delta={"content": "Done."})]
        mock_response_final.__iter__.return_value = [chunk_final]

        mock_llm_request.side_effect = [mock_response_tool, mock_response_final]

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
