"""edit_file — surgical exact-string replacement in an existing file."""

from tools.file import _edit, _resolve_path

definition = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": (
            "Replace an exact string in an existing file. "
            "old_string must appear exactly once (unless replace_all=true). "
            "Atomic via temp-file rename. "
            "You MUST read the file first with read_file so your old_string matches the current content. "
            "Preferred over write_file for targeted changes — avoids accidentally dropping unrelated content."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to edit.",
                },
                "old_string": {
                    "type": "string",
                    "description": "Exact text to find and replace. Must be unique in the file.",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text.",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "If true, replace every occurrence of old_string. Default false.",
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
}


def fn(path: str, old_string: str = "", new_string: str = "", replace_all: bool = False) -> str:
    try:
        resolved = str(_resolve_path(path.strip()))
        return _edit(resolved, old_string, new_string, replace_all)
    except Exception as e:
        return f"Error: edit_file failed: {e}"
