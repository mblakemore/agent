"""list_files — list directory contents."""

from tools.file import _list, _resolve_path

definition = {
    "type": "function",
    "function": {
        "name": "list_files",
        "description": (
            "List the contents of a directory. "
            "Skip this if you already know the paths you need. "
            "For recursive listings, use search_files with a glob pattern instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory to list. Defaults to current directory.",
                },
            },
            "required": [],
        },
    },
}


def fn(path: str = ".") -> str:
    try:
        resolved = str(_resolve_path(path.strip()))
        return _list(resolved)
    except Exception as e:
        return f"Error: list_files failed: {e}"
