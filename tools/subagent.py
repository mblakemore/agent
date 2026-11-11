import math
import subprocess
import tempfile
import threading
import os
from typing import Annotated

try:
    import monitor_bus as _bus
except ImportError:  # a harness without the bus: subagents block, as before
    _bus = None

# Default wall-clock timeout for a sub-agent process (seconds).
# Sub-agents may call LLMs and run multi-step tasks, so they legitimately need
# several minutes.  600 s (10 min) is generous without being unbounded.
_DEFAULT_TIMEOUT = 600


def _inherited_args() -> list:
    """Backend flags the parent exported (agent._export_subagent_args): its
    --backend-* overrides and the subagent tier's --model/--effort (M6)."""
    import json
    try:
        val = json.loads(os.environ.get("AGENT_SUBAGENT_ARGS") or "[]")
    except ValueError:
        return []
    return [str(v) for v in val] if isinstance(val, list) else []


def subagent(
    prompt: Annotated[str, "The task or question to delegate to a sub-agent"],
    timeout: int = _DEFAULT_TIMEOUT,
    background: bool = False,
) -> str:
    """
    Spawns a new autonomous agent process to solve a specific sub-task.
    The sub-agent will work independently and return its final answer.
    Use this to break down complex problems into smaller, manageable pieces.
    With background=true it returns at once with an id; the answer arrives on
    the bus when the sub-agent finishes (wait() blocks until then).
    """
    if not isinstance(prompt, str):
        return f"Error: prompt must be a string, got {type(prompt).__name__!r}"
    if not prompt.strip():
        return "Error: prompt must not be empty"
    if "\x00" in prompt:
        return "Error: prompt must not contain null bytes"
    if timeout is None:
        timeout = _DEFAULT_TIMEOUT
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
        return f"Error: timeout must be a number, got {type(timeout).__name__!r}"
    if not math.isfinite(timeout) or timeout <= 0:
        return "Error: timeout must be a finite positive number"
    if background is True or background == 1:
        if _bus is None:
            return "Error: background sub-agents need the monitor bus, which this harness lacks"
        sid = _bus.new_id("sub")
        _bus.source_started(sid, "subagent", note=prompt.strip().splitlines()[0][:80])

        def _run_bg():
            try:
                result = _run_subagent(prompt, timeout)
            except Exception as e:  # noqa: BLE001
                result = f"Error: running sub-agent: {e}"
            _bus.source_finished(sid, f"[subagent: {sid}] finished\n{result}")

        threading.Thread(target=_run_bg, name=f"subagent:{sid}", daemon=True).start()
        return (f"[subagent: {sid}] started in the background (timeout {timeout:g}s). Its "
                f"answer arrives by itself; wait() or wait(any_of=[\"{sid}\"]) blocks until it does.")
    return _run_subagent(prompt, timeout)


def _run_subagent(prompt, timeout):
    # Determine the absolute path to agent.py
    # tools/subagent.py -> ../agent.py
    tool_dir = os.path.dirname(os.path.abspath(__file__))
    agent_path = os.path.abspath(os.path.join(tool_dir, "..", "agent.py"))

    # Create a temporary file to capture the sub-agent's final result
    with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp:
        result_file_path = tmp.name

    try:
        # Construct the command to run the agent in auto-mode
        # --auto: ensures the agent runs until completion without user input
        # --result-file: ensures the final answer is written to our temp file
        cmd = [
            "python3",
            agent_path,
            "--auto",
            "--result-file", result_file_path,
            *_inherited_args(),
            prompt
        ]

        # Execute the process with a wall-clock timeout so a hung or infinite-
        # loop sub-agent cannot block the parent agent indefinitely.
        try:
            process = subprocess.run(
                cmd,
                # not the terminal: the child agent's exit handler would reset its modes
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return (
                f"Error: sub-agent timed out after {timeout}s. "
                "The sub-task may be too complex or the agent process hung. "
                "Try breaking the task into smaller pieces or increase timeout."
            )

        # Check if the process failed
        if process.returncode != 0:
            return f"Error: sub-agent process failed with exit code {process.returncode}: {process.stderr}"

        # Read the result from the file
        if os.path.exists(result_file_path):
            with open(result_file_path, "r", encoding="utf-8") as f:
                result = f.read().strip()

            if result:
                return result
            else:
                return "Error: sub-agent completed but returned no final answer"
        else:
            return "Error: sub-agent completed but no result file was created"

    except Exception as e:
        return f"Error: running sub-agent: {str(e)}"
    
    finally:
        # Clean up the temporary file
        if os.path.exists(result_file_path):
            os.remove(result_file_path)

# Auto-discovery exports
fn = subagent

definition = {
    "type": "function",
    "function": {
        "name": "subagent",
        "description": "Spawns a new autonomous agent process to solve a specific sub-task. The sub-agent will work independently and return its final answer. background=true starts it and returns an id; run several at once and wait() for them.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The task or question to delegate to a sub-agent",
                },
                "timeout": {
                    "type": "number",
                    "description": (
                        f"Maximum seconds to wait for the sub-agent to finish "
                        f"(default {_DEFAULT_TIMEOUT}). Increase for very long tasks."
                    ),
                    "default": _DEFAULT_TIMEOUT,
                },
                "background": {
                    "type": "boolean",
                    "description": "Return at once with an id; the answer arrives on its own when the sub-agent finishes (wait() blocks until then). Default false.",
                },
            },
            "required": ["prompt"],
        },
    },
}

# Registry hints (tools/__init__.py): catalog phrase, other harnesses' names, search words.
_aliases = ['spawn_agent', 'delegate', 'run_subagent']
_short = 'run a task in a child agent'
_keywords = ['delegate', 'child', 'parallel', 'spawn']
