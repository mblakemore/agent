"""append_file — append content to end of a file."""

from tools.file import _append, _resolve_path

definition = {
    "type": "function",
    "function": {
        "name": "append_file",
        "description": (
            "Append content to the end of a file. "
            "Use for JSONL files (one JSON object per line), logs, and other append-only collections. "
            "For .py files, inserts before any trailing `if __name__ == '__main__':` guard. "
            "Cannot append to .json files (use write_file for those)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to append to.",
                },
                "content": {
                    "type": "string",
                    "description": "Content to append.",
                },
            },
            "required": ["path", "content"],
        },
    },
}


def fn(path: str, content: str = "") -> str:
    try:
        resolved = str(_resolve_path(path.strip()))
        return _append(resolved, content)
    except Exception as e:
        return f"Error: append_file failed: {e}"
