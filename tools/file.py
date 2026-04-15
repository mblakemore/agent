"""Unified file operations tool — read, write, insert, append, delete, list."""

import os
import tempfile
import difflib
from pathlib import Path
import theme

# Paths that would create suspiciously deep nesting are probably mistakes
_MAX_NEW_DIRS = 3

# When reading an entire file (no start_line/end_line), cap at this many lines
# to avoid flooding the context window.
_MAX_READ_LINES = 500

# Track files that have been read or written this session — writes to existing
# unread files are blocked to prevent blind overwrites.  Shared with exec_command.
_accessed_files = set()


def _resolve_path(path):
    """Resolve path, stripping accidental cwd prefix duplications."""
    p = Path(path)
    if not p.is_absolute():
        cwd = Path.cwd()
        # Detect when the agent passes something like "droid/repos/project/e2/file"
        # which duplicates the cwd structure. Try to resolve it.
        try:
            cwd_str = str(cwd)
            # Ensure we have a trailing slash to avoid prefix collisions with similar dir names
            # Also ensure we aren't just at root '/', which would cause almost any path to start with prefix.
            if cwd_str == '/':
                return p
            
            prefix = cwd_str[1:] if cwd_str.endswith('/') else cwd_str[1:] + '/'
            if path.startswith(prefix):
                # Strip the cwd prefix from the relative path and make it absolute
                # We already know it starts with 'prefix', so we just prepend '/'
                return Path("/" + path)
        except (ValueError, OSError):
            pass
    return p


    def _get_diff(old_content, new_content):
        """Generate a colorized unified diff between old and new content."""
        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff = difflib.unified_diff(old_lines, new_lines, lineterm='')
        
        result = []
        for line in diff:
            if line.startswith('+') and not line.startswith('+++'):
                result.append(theme.c(theme.MINT, line))
            elif line.startswith('-') and not line.startswith('---'):
                result.append(theme.c(theme.ROSE, line))
            else:
                result.append(line)
        return "\n".join(result)


def fn(action: str, path: str = ".", content: str = "", start_line: int = 0, end_line: int = 0) -> str:
    """Perform file operations.

    Args:
        action: One of "read", "write", "insert", "append", "delete", "list".
        path: File or directory path.
        content: Content for write/append/insert actions.
        start_line: For read: first line (1-indexed). For write: first line to replace. For insert: line number to insert BEFORE.
        end_line: For read: last line (1-indexed). For write: last line to replace (REQUIRED when start_line is set).
    """
    try:
        resolved = str(_resolve_path(path))
        if action == "read":
            return _read(resolved, start_line, end_line)
        elif action == "write":
            return _write(resolved, content, start_line, end_line)
        elif action == "insert":
            return _insert(resolved, content, start_line)
        elif action == "append":
            return _append(resolved, content)
        elif action == "delete":
            return _delete(resolved)
        elif action == "list":
            return _list(resolved)
        else:
            return f"Error: unknown action '{action}'. Use: read, write, insert, append, delete, list."
    except Exception as e:
        return f"Error ({action}): {e}"


_BLOCKED_FILENAMES = {"conversation_checkpoint.json"}


def _read(path, start_line, end_line):
    p = Path(path)
    if p.name in _BLOCKED_FILENAMES:
        return f"Error: '{p.name}' is an internal runtime file and cannot be read."
    if not p.exists():
        return f"Error: '{path}' does not exist"
    if p.is_dir():
        return f"Error: '{path}' is a directory. Use action='list' instead."

    with open(p, 'r', encoding='utf-8', errors='replace') as f:
        s = max(1, start_line) if start_line > 0 else 1
        full_read = (start_line <= 0 and end_line <= 0)
        
        if end_line > 0:
            e = end_line
        elif full_read:
            e = s + _MAX_READ_LINES - 1
        else:
            e = float('inf')

        lines_to_return = []
        total = 0
        for line in f:
            total += 1
            if s <= total <= e:
                lines_to_return.append(line)
    
    if not lines_to_return:
        if total == 0:
            return f"[{path}: 0 lines of 0]\n(empty file)"
        if s > total:
            return f"Error: start_line ({s}) exceeds file length ({total} lines)"

    actual_end = int(min(total, e)) if e != float('inf') else total
    numbered = "".join(f"{i:4d}  {line}" for i, line in enumerate(lines_to_return, s))
    info = f"[{path}: lines {s}-{actual_end} of {total}]\n"
    if actual_end < total:
        info += f"[Use start_line={actual_end + 1} to continue reading]\n"

    _accessed_files.add(str(p.resolve()))
    return info + numbered


def _write(path, content, start_line, end_line):
    p = Path(path)
    if p.name in _BLOCKED_FILENAMES:
        return f"Error: '{p.name}' is an internal runtime file and cannot be written."

    # If file exists but hasn't been read this session, force a read first
    if p.exists() and str(p.resolve()) not in _accessed_files:
        return (f"Error: '{path}' exists but has not been read this session. "
                f"You must read the file first (action='read') before writing to it, "
                f"so you can verify your changes are accurate and won't overwrite important content.")

    # Line-range replacement
    if start_line > 0 or end_line > 0:
        if not p.exists():
            return f"Error: cannot replace lines — '{path}' does not exist"
        if start_line <= 0:
            start_line = 1
        if end_line <= 0:
            end_line = start_line
        if start_line > end_line:
            return f"Error: start_line ({start_line}) > end_line ({end_line})"
        
        # Capture old content for diff
        with open(p, 'r', encoding='utf-8', errors='replace') as f:
            old_content = f.read()
            total_lines = len(old_content.splitlines(True))
        
        if start_line > total_lines:
            return f"Error: start_line ({start_line}) exceeds file length ({total_lines} lines)"
        if end_line > total_lines:
            return f"Error: end_line ({end_line}) exceeds file length ({total_lines} lines)"

        # Prepare new content
        new_lines = content.splitlines(True) if content else []
        if new_lines and not new_lines[-1].endswith("\n"):
            if end_line < total_lines:
                new_lines[-1] += "\n"

        # Streaming replace
        temp_fd, temp_path = tempfile.mkstemp(dir=p.parent, text=True)
        try:
            with os.fdopen(temp_fd, 'w', encoding='utf-8') as temp_f:
                with open(p, 'r', encoding='utf-8', errors='replace') as src_f:
                    for i, line in enumerate(src_f, 1):
                        if i < start_line:
                            temp_f.write(line)
                        elif i == start_line:
                            temp_f.writelines(new_lines)
                        elif i > end_line:
                            temp_f.write(line)
            os.replace(temp_path, p)
        except Exception as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return f"Error during streaming write: {e}"

        with open(p, 'r', encoding='utf-8', errors='replace') as f:
            new_content = f.read()
        
        diff_text = _get_diff(old_content, new_content)
        
        old_count = end_line - start_line + 1
        new_count = len(new_lines)
        delta = new_count - old_count
        delta_str = f"+{delta}" if delta > 0 else str(delta)
        
        msg = (f"Replaced lines {start_line}-{end_line} in '{path}' "
               f"({old_count} → {new_count} lines, {delta_str}). "
               f"File now has {total_lines - old_count + new_count} lines.\n\nDiff:\n{diff_text}")
        return msg

    # Full file write
    dirs_to_create = []
    check = p.parent
    while check and not check.exists():
        dirs_to_create.append(check)
        check = check.parent
    if len(dirs_to_create) > _MAX_NEW_DIRS:
        return (f"Error: writing '{path}' would create {len(dirs_to_create)} nested directories. "
                f"This usually means the path is wrong. Use a relative path from your working directory "
                f"(e.g. '.agent/state/file.json' not '/droid/repos/.../state/file.json').")
    
    old_content = ""
    if p.exists():
        with open(p, 'r', encoding='utf-8', errors='replace') as f:
            old_content = f.read()
            
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        f.write(content)
    _accessed_files.add(str(p.resolve()))
    
    if old_content:
        diff_text = _get_diff(old_content, content)
        return f"Wrote '{path}' ({len(content)} chars)\n\nDiff:\n{diff_text}"
    return f"Wrote '{path}' ({len(content)} chars)"


def _append(path, content):
    p = Path(path)
    if p.name in _BLOCKED_FILENAMES:
        return f"Error: '{p.name}' is an internal runtime file and cannot be written."
    if p.suffix.lower() == '.json':
        return (f"Error: cannot append to JSON file '{path}' — breaks structure. "
                f"Use action='write' with full contents instead.")
    
    old_content = ""
    if p.exists():
        with open(p, 'r', encoding='utf-8', errors='replace') as f:
            old_content = f.read()

    with open(p, 'a', encoding='utf-8') as f:
        f.write(content)
    
    with open(p, 'r', encoding='utf-8', errors='replace') as f:
        new_content = f.read()
    
    diff_text = _get_diff(old_content, new_content)
    return f"Appended to '{path}' ({len(content)} chars)\n\nDiff:\n{diff_text}"


def _insert(path, content, start_line):
    """Insert content BEFORE the given line number. Existing lines shift down."""
    p = Path(path)
    if not p.exists():
        return f"Error: cannot insert — '{path}' does not exist"
    if not content:
        return f"Error: no content to insert"
    if start_line <= 0:
        return f"Error: start_line must be >= 1 (got {start_line})"

    if p.exists() and str(p.resolve()) not in _accessed_files:
        return (f"Error: '{path}' exists but has not been read this session. "
                f"You must read the file first (action='read') before inserting.")

    with open(p, 'r', encoding='utf-8', errors='replace') as f:
        old_content = f.read()
        total_lines = len(old_content.splitlines(True))
    
    if start_line > total_lines + 1:
        return f"Error: start_line ({start_line}) exceeds file length + 1 ({total_lines} lines)"

    new_lines = content.splitlines(True)
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"

    temp_fd, temp_path = tempfile.mkstemp(dir=p.parent, text=True)
    try:
        with os.fdopen(temp_fd, 'w', encoding='utf-8') as temp_f:
            with open(p, 'r', encoding='utf-8', errors='replace') as src_f:
                for i, line in enumerate(src_f, 1):
                    if i == start_line:
                        temp_f.writelines(new_lines)
                    temp_f.write(line)
                if start_line == total_lines + 1:
                    temp_f.writelines(new_lines)
        os.replace(temp_path, p)
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return f"Error during streaming insert: {e}"

    with open(p, 'r', encoding='utf-8', errors='replace') as f:
        new_content = f.read()

    diff_text = _get_diff(old_content, new_content)
    
    _accessed_files.add(str(p.resolve()))
    return (f"Inserted {len(new_lines)} line(s) before line {start_line} in '{path}'. "
            f"File now has {total_lines + len(new_lines)} lines.\n\nDiff:\n{diff_text}")


def _delete(path):
    p = Path(path)
    if p.name in _BLOCKED_FILENAMES:
        return f"Error: '{p.name}' is an internal runtime file and cannot be deleted."
    if not p.exists():
        return f"Error: '{path}' does not exist"
    if p.is_dir():
        if any(p.iterdir()):
            return f"Error: directory '{path}' is not empty"
        p.rmdir()
        return f"Deleted empty directory '{path}'"
    p.unlink()
    return f"Deleted '{path}'"


def _list(path):
    p = Path(path)
    if not p.exists():
        return f"Error: '{path}' does not exist"
    if not p.is_dir():
        return f"Error: '{path}' is not a directory"
    entries = sorted(p.iterdir())
    if not entries:
        return "(empty directory)"
    parts = []
    for e in entries:
        suffix = "/" if e.is_dir() else ""
        parts.append(f"{e.name}{suffix}")
    return "\n".join(parts)


definition = {
    "type": "function",
    "function": {
        "name": "file",
        "description": (
            "Unified file operations for reading and writing files.\n"
            "Actions:\n"
            "- read: Read file contents (with optional line range). Returns numbered lines.\n"
            "- write: Create/overwrite a file, or replace a line range (MUST set both start_line AND end_line). "
            "Parent directories are created automatically — do NOT call mkdir or exec_command before writing a file into a new directory.\n"
            "- insert: Insert content BEFORE a line (set start_line). Existing lines shift down. Does NOT replace anything.\n"
            "- append: Append content to end of file (not for JSON files).\n"
            "- delete: Delete a file or empty directory.\n"
            "- list: List directory contents. "
            "IMPORTANT: skip this action if the user's prompt already names the files or paths "
            "you need — calling list when you already know the paths wastes a turn.\n"
            "IMPORTANT: You MUST read an existing file before writing to it. "
            "The tool will reject writes to files you haven't read this session."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["read", "write", "insert", "append", "delete", "list"],
                    "description": "The operation to perform.",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory path. For 'list', defaults to current directory.",
                },
                "content": {
                    "type": "string",
                    "description": "Content for write/append. For line-range write, replaces the specified lines.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "For read: first line to return (1-indexed). For write: first line to replace. For insert: line to insert BEFORE.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "For read: last line to return (1-indexed). For write: last line to replace (REQUIRED when start_line is set).",
                },
            },
            "required": ["action", "path"],
        },
    },
}
