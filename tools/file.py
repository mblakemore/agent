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


def fn(action: str, path: str = ".", content: str = "", start_line: int = 0, end_line: int = 0, **kwargs) -> str:
    """Perform file operations.

    Args:
        action: One of "read", "write", "insert", "append", "delete", "list".
        path: File or directory path.
        content: Content for write/append/insert actions.
        start_line: For read: first line (1-indexed). For write: first line to replace. For insert: line number to insert BEFORE.
        end_line: For read: last line (1-indexed). For write: last line to replace (REQUIRED when start_line is set).
    """
    if kwargs:
        unexpected = ", ".join(f"'{k}'" for k in sorted(kwargs))
        return (
            f"Error: unexpected argument(s) {unexpected}. "
            f"Valid parameters are: action, path, content, start_line, end_line. "
            f"Valid actions are: read, write, insert, append, delete, list."
        )
    if not isinstance(content, str):
        return (
            f"Error: 'content' must be a string, got {type(content).__name__}. "
            f"Pass a plain string value for write/append/insert actions."
        )
    if '\x00' in content and action in ("write", "append", "insert"):
        return "Error: content contains a null byte, which is not allowed"
    if not isinstance(path, str):
        return (
            f"Error: 'path' must be a string, got {type(path).__name__}: {path!r}. "
            f"Pass a plain string file path."
        )
    if '\x00' in path:
        return "Error: path contains a null byte, which is not allowed"
    # Booleans are a subclass of int in Python; reject them for line-number params
    # so that start_line=True (silently 1) or end_line=False (silently 0) don't
    # slip through as valid line numbers.
    if isinstance(start_line, bool):
        return (
            f"Error: 'start_line' must be a plain integer, got bool ({start_line!r}). "
            f"Pass a plain integer line number."
        )
    if isinstance(end_line, bool):
        return (
            f"Error: 'end_line' must be a plain integer, got bool ({end_line!r}). "
            f"Pass a plain integer line number."
        )
    # Floats are not valid line numbers — reject them so that start_line=1.5 or
    # end_line=2.9 don't silently truncate to wrong values or produce corrupt
    # output like "2.5 lines remain".
    if isinstance(start_line, float):
        return (
            f"Error: 'start_line' must be a plain integer, got float ({start_line!r}). "
            f"Pass a plain integer line number."
        )
    if isinstance(end_line, float):
        return (
            f"Error: 'end_line' must be a plain integer, got float ({end_line!r}). "
            f"Pass a plain integer line number."
        )
    try:
        resolved = str(_resolve_path(path.strip()))
        if action == "read":
            return _read(resolved, start_line, end_line)
        elif action == "write":
            return _write(resolved, content, start_line, end_line)
        elif action == "insert":
            return _insert(resolved, content, start_line)
        elif action == "append":
            return _append(resolved, content)
        elif action == "delete":
            return _delete(resolved, start_line, end_line)
        elif action == "list":
            return _list(resolved)
        else:
            return f"Error: unknown action '{action}'. Use: read, write, insert, append, delete, list."
    except Exception as e:
        return f"Error: action '{action}' failed: {e}"


_BLOCKED_FILENAMES = {"conversation_checkpoint.json"}


def _check_write_confinement(path, p):
    """Return an error string if the resolved path is outside the working directory, else None.

    Mirrors the confinement logic in _expand_file_refs (fixed in #845):
    both absolute paths (/tmp/evil) and relative traversals (../../secret) are
    caught by resolving the path and comparing it to cwd.
    """
    try:
        resolved = p.resolve()
    except (OSError, ValueError):
        return None  # let the write attempt fail naturally
    cwd_resolved = Path.cwd().resolve()
    cwd_prefix = str(cwd_resolved) + os.sep
    if resolved != cwd_resolved and not str(resolved).startswith(cwd_prefix):
        return (
            f"Error: path '{path}' resolves to '{resolved}' which is outside "
            f"the working directory '{cwd_resolved}'. "
            f"Only files inside the current working directory can be written."
        )
    return None


def _read(path, start_line, end_line):
    p = Path(path)
    if p.name in _BLOCKED_FILENAMES:
        return f"Error: '{p.name}' is an internal runtime file and cannot be read."
    if not p.exists():
        return f"Error: '{path}' does not exist"
    if p.is_dir():
        return f"Error: '{path}' is a directory. Use action='list' instead."

    # Validate start_line/end_line combination before opening the file.
    # start_line=0 is rejected (consistent with write/insert/delete which all
    # require start_line >= 1 when a line number is supplied).  Callers that
    # want to read from the beginning of the file must omit start_line entirely
    # (i.e. leave it at its default of 0) rather than passing 0 explicitly.
    if start_line == 0 and end_line > 0:
        return (
            f"Error: start_line must be >= 1 (got 0). "
            f"Line numbers are 1-indexed. To read from the beginning of the file, "
            f"omit start_line (or pass start_line=1)."
        )
    if start_line < 0:
        return f"Error: start_line must be >= 1 (got {start_line})"
    if end_line > 0 and start_line > 0 and start_line > end_line:
        return f"Error: start_line ({start_line}) > end_line ({end_line})"

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

    # Confinement: reject writes to paths outside the working directory
    err = _check_write_confinement(path, p)
    if err:
        return err

    # If file exists but hasn't been read this session, force a read first
    if p.exists() and str(p.resolve()) not in _accessed_files:
        return (f"Error: '{path}' exists but has not been read this session. "
                f"You must read the file first (action='read') before writing to it, "
                f"so you can verify your changes are accurate and won't overwrite important content.")

    # Line-range replacement
    if start_line > 0 or end_line > 0:
        if not p.exists():
            return f"Error: cannot replace lines — '{path}' does not exist"
        if start_line < 0:
            return f"Error: start_line must be >= 1 (got {start_line})"
        if start_line == 0 and end_line > 0:
            return f"Error: start_line must be >= 1 (got 0). Line numbers are 1-indexed."
        if end_line <= 0:
            return (
                f"Error: end_line is required when start_line is set "
                f"(got start_line={start_line}, end_line=0). "
                f"To replace a single line, pass end_line={start_line}. "
                f"To replace a range, pass end_line=<last line to replace>."
            )
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
        try:
            temp_fd, temp_path = tempfile.mkstemp(dir=p.parent, text=True)
        except PermissionError:
            return f"Error: permission denied: {path}"
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
        except PermissionError:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return f"Error: permission denied: {path}"
        except Exception as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return f"Error: streaming write failed: {e}"

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

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(content)
    except PermissionError:
        return f"Error: permission denied: {path}"
    _accessed_files.add(str(p.resolve()))

    if old_content:
        diff_text = _get_diff(old_content, content)
        return f"Wrote '{path}' ({len(content)} chars)\n\nDiff:\n{diff_text}"
    return f"Wrote '{path}' ({len(content)} chars)"


import re as _re

_MAIN_GUARD_RE = _re.compile(
    r'^if\s+__name__\s*==\s*["\']__main__["\']\s*:(\s*#.*)?$',
    _re.MULTILINE,
)

# Lines that look like module-level metadata — skipped when scanning backwards
# past the guard so that trailing assignments don't stop the search.
_METADATA_LINE_RE = _re.compile(
    r'^(?:[A-Z_][A-Z0-9_]*\s*=|__[a-z]+__\s*=|["\'])'
)

# Detect triple-quoted string delimiters
_TRIPLE_QUOTE_RE = _re.compile(r'(\'\'\'|""")')


def _line_is_in_string(lines, idx):
    """Return True if lines[idx] is inside a triple-quoted string literal.

    Performs a forward scan counting unmatched triple-quote openers so that a
    guard-like line embedded in a docstring is not treated as a real guard.
    """
    depth = 0
    open_delim = None
    for i, line in enumerate(lines):
        if i == idx:
            return depth > 0
        for m in _TRIPLE_QUOTE_RE.finditer(line):
            delim = m.group(1)
            if depth == 0:
                depth = 1
                open_delim = delim
            elif delim == open_delim:
                depth -= 1
                if depth == 0:
                    open_delim = None
    return depth > 0


def _find_main_guard_start(lines):
    """Return the 0-based index of the first line of the `if __name__` guard block,
    or None if no such block is found in the trailing non-empty content."""
    # Walk backwards to find the last non-empty line, then locate the guard.
    last_nonempty = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            last_nonempty = i
            break
    if last_nonempty is None:
        return None

    # Search backwards from the last non-empty line to find the guard header.
    for i in range(last_nonempty, -1, -1):
        if _MAIN_GUARD_RE.match(lines[i]):
            # Edge-case 1: guard-text inside a string literal — skip it.
            if _line_is_in_string(lines, i):
                continue
            return i
        # Stop if we hit a non-empty, non-indented line that is not:
        #   • the last non-empty line (guard body / trailing code)
        #   • a blank line
        #   • a module-level metadata assignment / bare string (edge-case 2)
        if (lines[i].strip()
                and i != last_nonempty
                and not lines[i][0].isspace()
                and not _METADATA_LINE_RE.match(lines[i])):
            break
    return None


def _append(path, content):
    p = Path(path)
    if p.name in _BLOCKED_FILENAMES:
        return f"Error: '{p.name}' is an internal runtime file and cannot be written."

    # Confinement: reject writes to paths outside the working directory
    err = _check_write_confinement(path, p)
    if err:
        return err

    if p.suffix.lower() == '.json':
        return (f"Error: cannot append to JSON file '{path}' — breaks structure. "
               f"Use action='write' with full contents instead.")
    if not content:
        return f"Error: no content to append"

    old_content = ""
    if p.exists():
        with open(p, 'r', encoding='utf-8', errors='replace') as f:
            old_content = f.read()

    # For Python files, insert before any trailing `if __name__ == "__main__":` guard.
    if p.suffix.lower() == '.py' and old_content:
        lines = old_content.splitlines(True)
        guard_idx = _find_main_guard_start(lines)
        if guard_idx is not None:
            # Ensure the content to insert ends with a newline so it doesn't merge with the guard.
            insert_block = content if content.endswith('\n') else content + '\n'
            new_lines = lines[:guard_idx] + [insert_block] + lines[guard_idx:]
            new_content = ''.join(new_lines)
            try:
                with open(p, 'w', encoding='utf-8') as f:
                    f.write(new_content)
            except PermissionError:
                return f"Error: permission denied: {path}"
            diff_text = _get_diff(old_content, new_content)
            return f"Appended to '{path}' ({len(content)} chars, inserted before __main__ guard)\n\nDiff:\n{diff_text}"

    try:
        with open(p, 'a', encoding='utf-8') as f:
            # If the existing file doesn't end with a newline, insert one first so
            # the appended content starts on a new line instead of being fused onto
            # the last character of the existing content.
            if old_content and not old_content.endswith('\n'):
                f.write('\n')
            f.write(content)
    except PermissionError:
        return f"Error: permission denied: {path}"

    with open(p, 'r', encoding='utf-8', errors='replace') as f:
        new_content = f.read()

    diff_text = _get_diff(old_content, new_content)
    return f"Appended to '{path}' ({len(content)} chars)\n\nDiff:\n{diff_text}"


def _insert(path, content, start_line):
    """Insert content BEFORE the given line number. Existing lines shift down."""
    p = Path(path)

    # Confinement: reject inserts to paths outside the working directory (#861)
    err = _check_write_confinement(path, p)
    if err:
        return err

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
        return f"Error: streaming insert failed: {e}"

    with open(p, 'r', encoding='utf-8', errors='replace') as f:
        new_content = f.read()

    diff_text = _get_diff(old_content, new_content)

    _accessed_files.add(str(p.resolve()))
    return (f"Inserted {len(new_lines)} line(s) before line {start_line} in '{path}'. "
            f"File now has {total_lines + len(new_lines)} lines.\n\nDiff:\n{diff_text}")


def _delete(path, start_line=0, end_line=0):
    p = Path(path)
    if p.name in _BLOCKED_FILENAMES:
        return f"Error: '{p.name}' is an internal runtime file and cannot be deleted."

    # Confinement: reject deletes to paths outside the working directory (#861)
    err = _check_write_confinement(path, p)
    if err:
        return err

    if not p.exists():
        return f"Error: '{path}' does not exist"
    if p.is_dir():
        if start_line > 0 or end_line > 0:
            return f"Error: start_line/end_line cannot be used with a directory path"
        if any(p.iterdir()):
            return f"Error: directory '{path}' is not empty"
        p.rmdir()
        return f"Deleted empty directory '{path}'"

    # Line-range deletion: remove specific lines, keep the file.
    if start_line > 0 or end_line > 0:
        if str(p.resolve()) not in _accessed_files:
            return (f"Error: '{path}' exists but has not been read this session. "
                    f"You must read the file first (action='read') before deleting lines from it, "
                    f"so you can verify you are removing the correct content.")
        if start_line < 0:
            return f"Error: start_line must be >= 1 (got {start_line})"
        if start_line == 0 and end_line > 0:
            return f"Error: start_line must be >= 1 (got 0). Line numbers are 1-indexed."
        if end_line <= 0:
            end_line = start_line
        if start_line > end_line:
            return f"Error: start_line ({start_line}) > end_line ({end_line})"

        with open(p, 'r', encoding='utf-8', errors='replace') as f:
            old_content = f.read()
        total_lines = len(old_content.splitlines(True))

        if start_line > total_lines:
            return f"Error: start_line ({start_line}) exceeds file length ({total_lines} lines)"
        if end_line > total_lines:
            return f"Error: end_line ({end_line}) exceeds file length ({total_lines} lines)"

        temp_fd, temp_path = tempfile.mkstemp(dir=p.parent, text=True)
        try:
            with os.fdopen(temp_fd, 'w', encoding='utf-8') as temp_f:
                with open(p, 'r', encoding='utf-8', errors='replace') as src_f:
                    for i, line in enumerate(src_f, 1):
                        if i < start_line or i > end_line:
                            temp_f.write(line)
            os.replace(temp_path, p)
        except Exception as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return f"Error: line deletion failed: {e}"

        with open(p, 'r', encoding='utf-8', errors='replace') as f:
            new_content = f.read()

        diff_text = _get_diff(old_content, new_content)
        deleted_count = end_line - start_line + 1
        new_total = total_lines - deleted_count
        _accessed_files.add(str(p.resolve()))
        return (
            f"Deleted lines {start_line}-{end_line} from '{path}' "
            f"({deleted_count} line(s) removed, {new_total} lines remain).\n\nDiff:\n{diff_text}"
        )

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
        parts.append(f"{e}{suffix}")
    return "\n".join(parts)


definition = {
    "type": "function",
    "function": {
        "name": "file",
        "description": (
            "Unified file operations for reading and writing files.\n"
            "Actions:\n"
            "- read: Read file contents (with optional line range). Returns numbered lines. "
            "To locate where a Python symbol (function, class, method) is defined or called, "
            "use `find_symbol` instead of reading the whole file. "
            "For locating content in a large file, use `search_files` first to find the file and line, then pass `start_line=` to read just the relevant section.\n"
            "- write: Create/overwrite a file, or replace a line range (MUST set both start_line AND end_line). "
            "Parent directories are created automatically — do NOT call mkdir or exec_command before writing a file into a new directory. "
            "Prefer this over `exec_command` echo redirects or heredocs for writing files — it handles special characters correctly and is easier to review.\n"
            "- insert: Insert content BEFORE a line (set start_line). Existing lines shift down. Does NOT replace anything.\n"
            "- append: Append content to end of file (not for JSON files). "
            "For `.py` files, if the file ends with an `if __name__ == \"__main__\":` guard, "
            "the content is inserted *before* that guard to keep it syntactically valid.\n"
            "- delete: Delete a file or empty directory.\n"
            "- list: List directory contents. "
            "IMPORTANT: skip this action if the user's prompt already names the files or paths "
            "you need — calling list when you already know the paths wastes a turn. "
            "Avoid using `list` as a first-step orientation — go directly to the relevant file or search instead. "
            "For recursive directory contents, use `search_files` with an appropriate glob pattern instead of reaching for `ls -R`.\n"
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
