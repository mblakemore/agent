"""write_file — create or fully overwrite a file."""

from tools.file import _write, _resolve_path

definition = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "Create a new file or fully overwrite an existing one. "
            "Parent directories are created automatically. "
            "You MUST read an existing file with read_file before writing to it. "
            "For surgical changes to existing files, prefer edit_file. "
            "For appending to log/JSONL files, use append_file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to create or overwrite.",
                },
                "content": {
                    "type": "string",
                    "description": "Full file content to write.",
                },
            },
            "required": ["path", "content"],
        },
    },
}


def fn(path: str, content: str = "") -> str:
    try:
        resolved = str(_resolve_path(path.strip()))
        return _write(resolved, content, 0, 0)
    except Exception as e:
        return f"Error: write_file failed: {e}"
