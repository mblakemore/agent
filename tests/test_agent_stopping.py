import pytest
import json
import hashlib
from unittest.mock import MagicMock, patch
from agent import run_agent_single

def create_mock_response(content_text="", tool_calls=None):
    """
    Creates a mock response object that simulates the streaming output of _llm_request.
    """
    chunks = []
    if content_text:
        for char in content_text:
            chunks.append({"choices": [{"delta": {"content": char}}]})
    
    if tool_calls:
        for i, (name, args) in enumerate(tool_calls):
            tc = {
                "index": i,
                "id": f"t{i}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}
            }
            chunks.append({"choices": [{"delta": {"tool_calls": [tc]}}]})
            
    chunks.append({"choices": []})
    return chunks

# Common patches for all tests to prevent early exits and noise
COMMON_PATCHES = [
    ('agent._release_memory', MagicMock()),
    ('agent._check_memory_watermark', MagicMock()),
    ('agent._emit', MagicMock()),
    ('agent.cancellable', MagicMock()),
    ('agent.check_cancelled', MagicMock()),
]

def apply_common_patches():
    patches = []
    for target, value in COMMON_PATCHES:
        p = patch(target, value)
        p.start()
        patches.append(p)
    return patches

def test_stopping_text_loop():
    """Targets lines 2366-2367 (approx): Stopping when a text loop is detected."""
    patches = apply_common_patches()
    try:
        # Return the SAME text multiple times to trigger _TEXT_LOOP_THRESHOLD = 3
        responses = [create_mock_response("Same text")] * 5
        
        with patch('agent._llm_request') as mock_llm:
            mock_llm.side_effect = responses
            
            result = run_agent_single(
                conversation_history=[{"role": "user", "content": "test"}],
                summary_state={"text": "", "up_to": 0},
                initial_files=[],
                log=MagicMock(),
                async_summarizer=MagicMock()
            )
            assert result == "done"
    finally:
        for p in patches: p.stop()

def test_stopping_nudge_disabled():
    """Targets Line 2474: Stopping when _NUDGE_ENABLED is False and text-only response."""
    patches = apply_common_patches()
    try:
        with patch('agent._NUDGE_ENABLED', False), \
             patch('agent._llm_request') as mock_llm:
            
            mock_llm.return_value = create_mock_response("Just text")
            
            result = run_agent_single(
                conversation_history=[{"role": "user", "content": "test"}],
                summary_state={"text": "", "up_to": 0},
                initial_files=[],
                log=MagicMock(),
                async_summarizer=MagicMock()
            )
            assert result == "done"
    finally:
        for p in patches: p.stop()

def test_stopping_persisted_completion():
    """Targets lines 2413-2423 (approx): Stopping when work is persisted and completion signal matched."""
    patches = apply_common_patches()
    try:
        # 1. First turn: call a tool that persists work
        # 2. Second turn: return text that looks like completion
        responses = [
            create_mock_response(tool_calls=[("exec_command", {"command": "git commit -m 'done'"})]),
            create_mock_response("I have completed the task. Done."),
        ]
        
        with patch('agent._llm_request') as mock_llm, \
             patch('agent.MAP_FN', {'exec_command': lambda command, **kw: "Success"}), \
             patch('agent._is_read_only_command', return_value=False):
            
            mock_llm.side_effect = responses
            
            result = run_agent_single(
                conversation_history=[{"role": "user", "content": "test"}],
                summary_state={"text": "", "up_to": 0},
                initial_files=[],
                log=MagicMock(),
                async_summarizer=MagicMock()
            )
            assert result == "done"
    finally:
        for p in patches: p.stop()

def test_stopping_grace_period_exhausted():
    """Targets lines 2529-2534: Stopping when cycle persisted and grace period exhausted."""
    patches = apply_common_patches()
    try:
        with patch('agent._NUDGE_ENABLED', True), \
             patch('agent._CYCLE_GRACE_TURNS', 2), \
             patch('agent._llm_request') as mock_llm, \
             patch('agent.MAP_FN', {'exec_command': lambda command, **kw: "Success"}), \
             patch('agent._is_read_only_command', return_value=False):
            
            # Turn 1: Persist (git push)
            # Turns 2-4: Text-only responses (exceed _CYCLE_GRACE_TURNS=2)
            responses = [
                create_mock_response(tool_calls=[("exec_command", {"command": "git push"})]),
            ] + [create_mock_response("still working")] * 4
            
            mock_llm.side_effect = responses
            
            result = run_agent_single(
                conversation_history=[{"role": "user", "content": "test"}],
                summary_state={"text": "", "up_to": 0},
                initial_files=[],
                log=MagicMock(),
                async_summarizer=MagicMock(),
                nudge=True
            )
            assert result == "done"
    finally:
        for p in patches: p.stop()

def test_stopping_overtime_text_only():
    """Targets lines 2536-2539: Stopping when overtime + text-only response."""
    patches = apply_common_patches()
    try:
        with patch('agent._NUDGE_ENABLED', True), \
             patch('agent._MAX_TURNS', 1), \
             patch('agent._llm_request') as mock_llm:
            
            # Turn 1: OK
            # Turn 2: Overtime + text-only
            mock_llm.side_effect = [
                create_mock_response("T1"), 
                create_mock_response("T2 overtime text-only")
            ]
            
            result = run_agent_single(
                conversation_history=[{"role": "user", "content": "test"}],
                summary_state={"text": "", "up_to": 0},
                initial_files=[],
                log=MagicMock(),
                async_summarizer=MagicMock(),
                nudge=True
            )
            assert result == "done"
    finally:
        for p in patches: p.stop()

def test_stopping_overtime_hard_cap():
    """Targets Line 2257: Stopping due to hard overtime cap."""
    patches = apply_common_patches()
    try:
        # Inner patch takes precedence over any global _MAX_TURNS
        with patch('agent._MAX_TURNS', 1), \
             patch('agent._llm_request') as mock_llm:
            
            # Turn 1: OK
            # Turn 2: Overtime
            # Turn 3: Hard Cap (overtime >= 1)
            mock_llm.side_effect = [
                create_mock_response("T1"), 
                create_mock_response("T2"), 
                create_mock_response("T3")
            ]
            
            result = run_agent_single(
                conversation_history=[{"role": "user", "content": "test"}],
                summary_state={"text": "", "up_to": 0},
                initial_files=[],
                log=MagicMock(),
                async_summarizer=MagicMock()
            )
            assert result == "done"
    finally:
        for p in patches: p.stop()

def test_stopping_nudge_budget_exhausted():
    """Targets lines 2544-2548: Stopping when total nudge budget exhausted."""
    patches = apply_common_patches()
    try:
        # Patch _MAX_TOTAL_NUDGES to 1 to trigger the budget quickly
        with patch('agent._NUDGE_ENABLED', True), \
             patch('agent._MAX_TOTAL_NUDGES', 1), \
             patch('agent._llm_request') as mock_llm:
            
            # Text-only responses trigger nudges.
            responses = [
                create_mock_response("Text 1"), 
                create_mock_response("Text 2"), 
                create_mock_response("Text 3"), 
            ]
            mock_llm.side_effect = responses
            
            result = run_agent_single(
                conversation_history=[{"role": "user", "content": "test, please use a tool"}],
                summary_state={"text": "", "up_to": 0},
                initial_files=[],
                log=MagicMock(),
                async_summarizer=MagicMock(),
                nudge=True
            )
            assert result == "done"
    finally:
        for p in patches: p.stop()

def test_debug_paths():
    import agent
    import sys
    import os
    print(f"\nDEBUG: agent file path: {agent.__file__}")
    print(f"DEBUG: sys.path: {sys.path}")
    print(f"DEBUG: Current Working Directory: {os.getcwd()}")
