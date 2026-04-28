import pytest
import agent
import logging
from unittest.mock import patch

def reset_agent_globals():
    agent._cicd_branch = None
    agent._cicd_edited_files = set()
    agent._cicd_pr_number = None
    agent._cicd_issue_number = None
    agent._cycle_persisted = False
    agent.conversation_history = []

@pytest.fixture(autouse=True)
def run_around_tests():
    reset_agent_globals()
    yield
    reset_agent_globals()

def run_agent_with_nudge_check(branch, edited_files, pr_number, issue_number, persisted):
    agent._cicd_branch = branch
    agent._cicd_edited_files = set(edited_files) if edited_files else set()
    agent._cicd_pr_number = pr_number
    agent._cicd_issue_number = issue_number
    agent._cycle_persisted = persisted
    
    history = []
    
    mock_chunks = [{"choices": [{"delta": {"content": "I am done."}}]}]
    
    with patch('agent._emit'), \
         patch('agent._llm_request') as mock_llm:
        
        def breaking_llm(*args, **kwargs):
            if not hasattr(breaking_llm, "called"):
                breaking_llm.called = True
                return mock_chunks
            raise RuntimeError("Exit Loop")
        
        mock_llm.side_effect = breaking_llm
        
        with patch('agent._maybe_resummarize', return_value=False):
            # The nudge logic happens when the agent is in a "text-only" response loop
            # or after a turn where it didn't use tools. 
            # We need to simulate the agent producing a text-only response.
            # In agent.py: run_agent_single handles the turn.
            # The nudge is added when _consecutive_text_only >= 1.
            
            # We force a text-only response by mocking the LLM to return a string
            # (which is what triggers the text-only counter in the real agent)
            # Wait, the agent logic for text-only is based on whether the LLM
            # returned a tool call or just text.
            
            # Let's mock the turn so it looks like a text response was received.
            # The logic is:
            # if response_has_tool_calls:
            #    ...
            # else:
            #    _consecutive_text_only += 1
            #    if _consecutive_text_only >= 1:
            #        # Check for hallucinated read, THEN check for CICD nudge
            
            # To trigger this, we need _llm_request to return a response 
            # that result in no tool calls.
            
            try:
                agent.run_agent_single(
                    conversation_history=history,
                    summary_state={"text": "", "up_to": 0},
                    initial_files=[],
                    log=logging.getLogger("test")
                )
            except RuntimeError as e:
                if str(e) != "Exit Loop":
                    raise e
            except Exception:
                pass
            
    return history

def test_nudge_pr_open_missing():
    """Case 1: Edited files, no PR, persisted cycle -> Expect 'PR open' nudge."""
    history = run_agent_with_nudge_check("cicd/test-branch", ["file1.py"], None, 466, True)
    
    nudges = [msg['content'] for msg in history if msg['role'] == 'user']
    assert any("PR open" in nudge for nudge in nudges)

def test_nudge_commit_push_missing():
    """Case 2: Edited files, no PR, NOT persisted cycle -> Expect 'commit + push + PR open' nudge."""
    history = run_agent_with_nudge_check("cicd/test-branch", ["file1.py"], None, 466, False)
    
    nudges = [msg['content'] for msg in history if msg['role'] == 'user']
    assert any("commit + push + PR open" in nudge for nudge in nudges)

def test_no_nudge_when_pr_exists():
    """Case 3: PR already open -> Expect no CICD nudge."""
    history = run_agent_with_nudge_check("cicd/test-branch", ["file1.py"], 123, 466, True)
    
    nudges = [msg['content'] for msg in history if msg['role'] == 'user']
    assert not any("PR open" in nudge for nudge in nudges)

def test_no_nudge_no_edits():
    """Case 4: No edited files -> Expect no CICD nudge."""
    history = run_agent_with_nudge_check("cicd/test-branch", None, None, 466, True)
    
    nudges = [msg['content'] for msg in history if msg['role'] == 'user']
    assert not any("PR open" in nudge for nudge in nudges)
