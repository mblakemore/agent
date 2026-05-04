"""Search files tool — grep through files for patterns."""

import os
import re
from pathlib import Path
from collections import deque

_MAX_RESULTS = 100
_MAX_CONTEXT = 20

# Directories/patterns excluded by default to avoid noise from CICD
# session artifacts and dependency caches.  Callers can pass
# include_temp=True to skip these exclusions entirely.
DEFAULT_EXCLUDES = [
    "temp/",
    "worktrees/",
    "state/debug/",
    ".venv*/",
    "node_modules/",
    "__pycache__/",
    ".git/",
    "*.log",
]

# Directory-name stems extracted from DEFAULT_EXCLUDES (entries ending
# with "/" are treated as directory names for fast os.walk pruning).
_DEFAULT_EXCLUDE_DIRS = {e.rstrip("/") for e in DEFAULT_EXCLUDES if e.endswith("/")}
# File-level glob patterns from DEFAULT_EXCLUDES.
_DEFAULT_EXCLUDE_FILE_GLOBS = [e for e in DEFAULT_EXCLUDES if not e.endswith("/")]

_BINARY_CHUNK = 8192  # bytes to sample when probing for binary content


def _is_binary(file_path: Path) -> bool:
    """Return True if the file appears to be binary (contains a null byte in the first chunk)."""
    try:
        with file_path.open("rb") as f:
            chunk = f.read(_BINARY_CHUNK)
        return b"\x00" in chunk
    except Exception:
        return False


def _search_single_file(file_path, base_dir, regex, context, count_only):
    """Search a single file and return formatted results."""
    from collections import deque
    rel = str(file_path.relative_to(base_dir))
    resolved = file_path
    total_matches = 0
    files_searched = 1
    files_matched = 0
    truncated = False
    read_error = None
    match_lines = []
    context_groups = []

    context_capped = False
    if context < 0:
        context = 0
    elif context > _MAX_CONTEXT:
        context = _MAX_CONTEXT
        context_capped = True

    if _is_binary(file_path):
        return f"[Skipped '{resolved}': binary file]\nNo matches found."

    try:
        with file_path.open(encoding='utf-8', errors='ignore') as f:
            if count_only:
                for line in f:
                    if regex.search(line):
                        total_matches += 1
                if total_matches > 0:
                    files_matched = 1
            elif context == 0:
                for line_num, line in enumerate(f, 1):
                    if regex.search(line):
                        files_matched = 1
                        total_matches += 1
                        match_lines.append(f"{rel}:{line_num}: {line.rstrip()}")
                        if len(match_lines) >= _MAX_RESULTS:
                            truncated = True
                            break
            else:
                buffer = deque(maxlen=context)
                current_group = []
                lines_to_emit = 0
                for line_num, line in enumerate(f, 1):
                    text_line = line.rstrip()
                    is_match = bool(regex.search(text_line))
                    if is_match:
                        files_matched = 1
                        total_matches += 1
                        if total_matches >= _MAX_RESULTS:
                            truncated = True
                            break
                        if not current_group:
                            for b_num, b_text in buffer:
                                current_group.append(f"{rel}:{b_num}- {b_text}")
                        current_group.append(f"{rel}:{line_num}: {text_line}")
                        lines_to_emit = context
                    else:
                        if current_group:
                            if lines_to_emit > 0:
                                current_group.append(f"{rel}:{line_num}- {text_line}")
                                lines_to_emit -= 1
                            else:
                                context_groups.append(current_group)
                                current_group = []
                        buffer.append((line_num, text_line))
                if current_group:
                    context_groups.append(current_group)
    except PermissionError as e:
        read_error = f"Warning: could not read '{file_path}': {e}"
    except Exception as e:
        read_error = f"Warning: error reading '{file_path}': {e}"

    display_count = total_matches
    if not count_only and truncated:
        display_count = _MAX_RESULTS

    header = (
        f"[Searched '{resolved}' "
        f"({files_searched} files, {files_matched} matched, {display_count} results)"
    )
    if truncated:
        header += " (truncated)"
    if context_capped:
        header += f" (context capped to {_MAX_CONTEXT})"
    if read_error:
        header += f" ({read_error})"
    header += "]\n"

    if count_only:
        return header.rstrip("\n")

    if total_matches == 0:
        return header + "No matches found."

    if context == 0:
        return header + "\n".join(match_lines)

    return header + "\n--\n".join("\n".join(g) for g in context_groups)


def fn(
    pattern: str,
    path: str = ".",
    glob: str = "*",
    ignore_case: bool = True,
    context: int = 3,
    count_only: bool = False,
    include_temp: bool = False,
    include_hidden: bool = False,
) -> str:
    """Search file contents for a regex pattern.

    Args:
        pattern: Regex pattern to search for.
        path: Directory to search in (default: current directory).
        glob: File glob pattern to filter (default: * for all files).
        ignore_case: Case-insensitive search (default: True).
        context: Lines of context to include before/after each match, like
            grep -C. Capped at _MAX_CONTEXT. Default 3. Match lines use
            'file:linenum: text'; context lines use 'file:linenum- text'.
            Pass 0 to get match lines only (no context).
        count_only: If True, return only the match count summary (files
            searched, files matched, total matches) without the match lines.
            Use this when you only need to know how many matches exist, not where they are.
        include_temp: If True, disable DEFAULT_EXCLUDES so that temp/,
            worktrees/, state/debug/, and similar high-noise directories
            are included in the search.  Default False.
        include_hidden: If True, include hidden files and directories (names
            starting with '.', e.g. .env, .gitignore, .claude/).  .git/ is
            always excluded regardless of this flag.  Default False.
    """
    import fnmatch as _fnmatch

    if not isinstance(pattern, str) or not pattern.strip():
        return "Error: Search pattern cannot be empty."

    if glob is not None and (not isinstance(glob, str) or not glob.strip()):
        return "Error: glob filter cannot be empty — omit the argument or pass '*' to match all files."

    if glob is not None and ("/" in glob or os.sep in glob):
        return (
            f"Error: glob pattern {glob!r} contains a path separator. "
            "Use a plain filename pattern (e.g. '*.py') and set path= to the "
            "desired subdirectory to restrict the search."
        )

    try:
        flags = re.IGNORECASE if ignore_case else 0
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"Error: invalid regex pattern: {e}"

    # Resolve search_path immediately to avoid absolute vs relative mismatch in relative_to()
    if not isinstance(path, str):
        return f"Error: path must be a string, got {type(path).__name__}"
    path = path.strip()
    search_path = Path(path).resolve()
    if not search_path.exists():
        return f"Error: path '{path}' does not exist"

    # Validate context type before any comparison — must come before the single-file branch
    # so both code paths receive a properly-typed integer.
    if not isinstance(context, int) or isinstance(context, bool):
        try:
            context = int(context)
        except (TypeError, ValueError):
            return f"Error: context must be an integer, got {type(context).__name__!r}"

    # If path points to a single file, search just that file.
    if search_path.is_file():
        return _search_single_file(search_path, search_path.parent, regex, context, count_only)

    # Since search_path is now already resolved, we can use it directly for os.walk
    resolved = search_path

    context_capped = False
    if context < 0:
        context = 0
    elif context > _MAX_CONTEXT:
        context = _MAX_CONTEXT
        context_capped = True

    match_lines: list[str] = []
    context_groups: list[list[str]] = []
    total_matches = 0
    files_searched = 0
    files_matched = 0
    truncated = False
    permission_errors = 0

    # We use os.walk instead of rglob to capture PermissionErrors via the onerror callback.
    # os.walk(top, topdown=True, onerror=onerror)
    def handle_error(os_error):
        nonlocal permission_errors
        if isinstance(os_error, PermissionError):
            permission_errors += 1

    for root, dirs, files in os.walk(resolved, onerror=handle_error):
        # Filter directories to skip hidden ones (mimicking rglob behavior).
        # .git/ is always excluded; other dotdirs are skipped unless include_hidden=True.
        if include_hidden:
            dirs[:] = [d for d in dirs if d != ".git"]
        else:
            dirs[:] = [d for d in dirs if not d.startswith(".")]

        if not include_temp:
            # Prune directories that are in DEFAULT_EXCLUDES.
            dirs[:] = [d for d in dirs if d not in _DEFAULT_EXCLUDE_DIRS]

        for file_name in files:
            if not _fnmatch.fnmatch(file_name, glob):
                continue

            # Skip hidden files unless include_hidden=True
            if not include_hidden and file_name.startswith("."):
                continue

            if not include_temp:
                # Skip files matching DEFAULT_EXCLUDES file-level patterns.
                if any(_fnmatch.fnmatch(file_name, pat) for pat in _DEFAULT_EXCLUDE_FILE_GLOBS):
                    continue

            file_path = Path(root) / file_name

            if truncated:
                break

            # Skip binary files silently — returning raw binary content would
            # mislead an agent into thinking it has useful search results.
            if _is_binary(file_path):
                continue

            rel = str(file_path.relative_to(search_path))
            files_searched += 1
            file_has_match = False

            try:
                with file_path.open(encoding='utf-8', errors='ignore') as f:
                    if count_only:
                        file_hits = 0
                        for line in f:
                            if regex.search(line):
                                file_hits += 1
                        if file_hits > 0:
                            files_matched += 1
                            total_matches += file_hits
                        continue

                    if context == 0:
                        for line_num, line in enumerate(f, 1):
                            if regex.search(line):
                                file_has_match = True
                                total_matches += 1
                                match_lines.append(f"{rel}:{line_num}: {line.rstrip()}")
                                if len(match_lines) >= _MAX_RESULTS:
                                    truncated = True
                                    break
                        if file_has_match:
                            files_matched += 1
                    else:
                        buffer = deque(maxlen=context)
                        current_group = []
                        lines_to_emit = 0

                        for line_num, line in enumerate(f, 1):
                            text_line = line.rstrip()
                            is_match = bool(regex.search(text_line))

                            if is_match:
                                file_has_match = True
                                total_matches += 1
                                if total_matches >= _MAX_RESULTS:
                                    truncated = True
                                    break
                                if not current_group:
                                    for b_num, b_text in buffer:
                                        current_group.append(f"{rel}:{b_num}- {b_text}")
                                current_group.append(f"{rel}:{line_num}: {text_line}")
                                lines_to_emit = context
                            else:
                                if current_group:
                                    if lines_to_emit > 0:
                                        current_group.append(f"{rel}:{line_num}- {text_line}")
                                        lines_to_emit -= 1
                                    else:
                                        context_groups.append(current_group)
                                        current_group = []

                                buffer.append((line_num, text_line))

                        if current_group:
                            context_groups.append(current_group)
                        if file_has_match:
                            files_matched += 1

                    if not count_only:
                        if len(match_lines) >= _MAX_RESULTS or len(context_groups) >= _MAX_RESULTS:
                            truncated = True

            except Exception:
                continue

    display_count = total_matches
    if not count_only and truncated:
        display_count = _MAX_RESULTS

    header = (
        f"[Searched '{resolved}' "
        f"({files_searched} files, {files_matched} matched, {display_count} results)"
    )
    if truncated:
        header += " (truncated)"
    if context_capped:
        header += f" (context capped to {_MAX_CONTEXT})"
    if permission_errors > 0:
        header += f" (Warning: {permission_errors} directories skipped due to permissions)"
    header += "]\n"

    if count_only:
        return header.rstrip("\n")

    if total_matches == 0:
        if files_searched == 0:
            return (
                header
                + f"No files were searched under '{resolved}'. "
                + f"If you meant a different directory, pass path= with an absolute path."
                )
        return header + "No matches found."

    if context == 0:
        return header + "\n".join(match_lines)

    return header + "\n--\n".join("\n".join(g) for g in context_groups)


# Public alias used by some callers (e.g. issue #567 reproduction case).
_do_search = fn


definition = {
    "type": "function",
    "function": {
        "name": "search_files",
        "description": (
            "Use this as your first step when you need to find where a symbol, function, variable, or pattern appears in the codebase — before reading any file. "
            "Search file contents for a regex pattern (like grep). "
            "Searches recursively through through a directory and returns each hit "
            "with surrounding context lines by default, so you can tell a "
            "definition from a call from a documentation mention without "
            "needing a follow-up file read. Use this to find patterns in "
            "code, search memory files, review past cycle logs, or locate "
            "specific content across the project. Prefer this over reading "
            "whole files when you only need to know how many matches exist, not where they are. "
            "Pass count_only=true when you only need to look at a handful of matches. "
            "Pass count_only=true when you only need a match count (e.g. "
            "how many TODOs, test methods, or call sites) — returns just the "
            "summary line without match content. "
            "IMPORTANT: always pass `path` explicitly when you know the "
            "directory you want to search. The default `'.'` is the process "
            "working directory, which in automation mode is usually an empty "
            "temp dir — not the repo the user asked about. "
            "When searching for TODO/FIXME comments in code, use `# TODO|# FIXME` as the pattern (with the `#` prefix) to avoid matching these words inside string literals or docstrings."
            ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for.",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Directory to search in. The default '.' is the "
                        "process "
                        "working directory, which in automation "
                        "mode is usually an empty "
                        "temp dir — pass the "
                        "absolute path to the directory you actually want "
                        "to search whenever you know it."
                        ),
                        "default": ".",
                    },
                "glob": {
                    "type": "string",
                    "description": "File glob to filter, e.g. '*.py', '*.json', '*.md' (default: all files).",
                },
                "ignore_case": {
                    "type": "boolean",
                    "description": "Case-insensitive search (default: true).",
                    "default": True,
                },
                "context": {
                    "type": "integer",
                    "description": (
                        "Lines of context to show before and after each match, like grep -C. "
                        "Capped at 20. Default 3. "
                        "Output format: match lines use 'file:linenum: text'; "
                        "context lines use 'file:linenum- text' (trailing dash instead of colon "
                        "distinguishes context from the matched line). "
                        "Pass 0 to get match lines only (no context)."
                        ),
                        "default": 3,
                        "minimum": 0,
                    },
                "count_only": {
                    "type": "boolean",
                    "description": "If True, return only the match count summary (files searched, files matched, total matches) without the match lines. Use this when you only need to know how many matches exist, not where they are. Default: false.",
                        "default": False,
                    },
                "include_temp": {
                    "type": "boolean",
                    "description": (
                        "If true, disable the default exclusion of high-noise directories "
                        "(temp/, worktrees/, state/debug/, .venv*/, node_modules/, "
                        "__pycache__/, .git/, *.log). Use this only when you specifically "
                        "need to search inside those directories. Default: false."
                    ),
                    "default": False,
                },
                "include_hidden": {
                    "type": "boolean",
                    "description": (
                        "If true, include hidden files and directories (names starting with "
                        "'.', e.g. .env, .gitignore, .claude/). .git/ is always excluded "
                        "regardless of this flag. Default: false."
                    ),
                    "default": False,
                },
                },
                "required": ["pattern"],
            },
        },
    }
