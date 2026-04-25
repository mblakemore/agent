import subprocess
import tempfile
import os
from typing import Annotated

def subagent(
    prompt: Annotated[str, "The task or question to delegate to a sub-agent"]
) -> str:
    """
    Spawns a new autonomous agent process to solve a specific sub-task.
    The sub-agent will work independently and return its final answer.
    Use this to break down complex problems into smaller, manageable pieces.
    """
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

        # Execute the process
        process = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            check=False
        )

        # Check if the process failed
        if process.returncode != 0:
            return f"The sub-agent process failed with exit code {process.returncode}. Error: {process.stderr}"

        # Read the result from the file
        if os.path.exists(result_file_path):
            with open(result_file_path, "r", encoding="utf-8") as f:
                result = f.read().strip()
            
            if result:
                return result
            else:
                return "The sub-agent completed but returned no final answer."
        else:
            return "The sub-agent completed successfully but no result file was created."

    except Exception as e:
        return f"An error occurred while running the sub-agent: {str(e)}"
    
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
                }
            },
            "required": ["prompt"],
        },
    },
}
