import math
import subprocess
import tempfile
import os
from typing import Annotated

# Default wall-clock timeout for a sub-agent process (seconds).
# Sub-agents may call LLMs and run multi-step tasks, so they legitimately need
# several minutes.  600 s (10 min) is generous without being unbounded.
_DEFAULT_TIMEOUT = 600


def subagent(
    prompt: Annotated[str, "The task or question to delegate to a sub-agent"],
    timeout: int = _DEFAULT_TIMEOUT,
) -> str:
    """
    Spawns a new autonomous agent process to solve a specific sub-task.
    The sub-agent will work independently and return its final answer.
    Use this to break down complex problems into smaller, manageable pieces.
    """
    if not isinstance(prompt, str):
        return f"Error: prompt must be a string, got {type(prompt).__name__!r}"
    if not prompt.strip():
        return "Error: prompt must not be empty"
    if "\x00" in prompt:
        return "Error: prompt must not contain null bytes"
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
        return f"Error: timeout must be a number, got {type(timeout).__name__!r}"
    if not math.isfinite(timeout) or timeout <= 0:
        return "Error: timeout must be a finite positive number"

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
            prompt
        ]

        # Execute the process with a wall-clock timeout so a hung or infinite-
        # loop sub-agent cannot block the parent agent indefinitely.
        try:
            process = subprocess.run(
                cmd,
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
        "description": "Spawns a new autonomous agent process to solve a specific sub-task. The sub-agent will work independently and return its final answer.",
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
            },
            "required": ["prompt"],
        },
    },
}
