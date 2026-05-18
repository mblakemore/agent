"""read_file — read file contents with optional line range."""

from tools.file import _read, _resolve_path

definition = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read file contents, returning numbered lines. "
            "Provide start_line/end_line to read a specific range (1-indexed). "
            "Without a range, returns up to 500 lines from the start. "
            "To locate a Python symbol, use find_symbol instead. "
            "To find which file contains something, use search_files first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "First line to return (1-indexed). Omit to start from line 1.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Last line to return (1-indexed). Omit to use default window.",
                },
            },
            "required": ["path"],
        },
    },
}


def fn(path: str, start_line: int = 0, end_line: int = 0) -> str:
    try:
        resolved = str(_resolve_path(path.strip()))
        return _read(resolved, start_line, end_line)
    except Exception as e:
        return f"Error: read_file failed: {e}"
