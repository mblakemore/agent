"""Search files tool — grep through files for patterns."""

import math
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
    abs_path = str(file_path.resolve())
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
                    if regex.search(line.rstrip()):
                        total_matches += 1
                if total_matches > 0:
                    files_matched = 1
            elif context == 0:
                for line_num, line in enumerate(f, 1):
                    text_line = line.rstrip()
                    if regex.search(text_line):
                        files_matched = 1
                        total_matches += 1
                        match_lines.append(f"{abs_path}:{line_num}: {text_line}")
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
                        if not current_group:
                            for b_num, b_text in buffer:
                                current_group.append(f"{abs_path}:{b_num}- {b_text}")
                        current_group.append(f"{abs_path}:{line_num}: {text_line}")
                        lines_to_emit = context
                        if total_matches >= _MAX_RESULTS:
                            truncated = True
                            break
                    else:
                        if current_group:
                            if lines_to_emit > 0:
                                current_group.append(f"{abs_path}:{line_num}- {text_line}")
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
    glob=None,
    ignore_case: bool = False,
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
            Accepts a plain string ('*.py'), a comma-separated string
            ('*.py,*.txt'), or a list of strings (['*.py', '*.txt']).
        ignore_case: Case-insensitive search (default: False).
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

    if not isinstance(pattern, str):
        return f"Error: pattern must be a string, got {type(pattern).__name__!r}"
    if not pattern.strip():
        return "Error: Search pattern cannot be empty."

    if isinstance(pattern, str) and '\x00' in pattern:
        return "Error: pattern contains a null byte, which is not allowed"

    # Normalise glob into a list of patterns so callers can pass:
    #   • a plain string:            glob='*.py'
    #   • a comma-separated string:  glob='*.py,*.txt'  (common user mistake)
    #   • a list of strings:         glob=['*.py', '*.txt']
    # Internally we always work with a list; the legacy single-string path
    # separator check is applied to every element.
    if glob is None:
        glob_patterns: list[str] = ["*"]
    elif isinstance(glob, list):
        # Flatten list; reject non-string elements
        bad = [i for i, g in enumerate(glob) if not isinstance(g, str)]
        if bad:
            return (
                f"Error: glob list contains non-string element(s) at index {bad[0]}. "
                "Each element must be a plain string pattern (e.g. '*.py')."
            )
        flat = [g.strip() for g in glob if g.strip()]
        if not flat:
            return "Error: glob filter cannot be empty — omit the argument or pass '*' to match all files."
        glob_patterns = flat
    elif isinstance(glob, str):
        if '\x00' in glob:
            return "Error: glob pattern contains a null byte, which is not allowed"
        if not glob.strip():
            return "Error: glob filter cannot be empty — omit the argument or pass '*' to match all files."
        # Split on commas so 'glob=*.py,*.txt' works as expected
        parts = [g.strip() for g in glob.split(",") if g.strip()]
        if not parts:
            return "Error: glob filter cannot be empty — omit the argument or pass '*' to match all files."
        glob_patterns = parts
    else:
        return (
            f"Error: glob must be a string or list of strings, got {type(glob).__name__!r}. "
            "Pass a plain filename pattern (e.g. '*.py') or a comma-separated list."
        )

    for g in glob_patterns:
        if '\x00' in g:
            return "Error: glob pattern contains a null byte, which is not allowed"
        if "/" in g or os.sep in g:
            return (
                f"Error: glob pattern {g!r} contains a path separator. "
                "Use a plain filename pattern (e.g. '*.py') and set path= to the "
                "desired subdirectory to restrict the search."
            )

    # Coerce None to False for all optional boolean flags so that an LLM passing
    # null for any of these defaults to the documented False behaviour (#952).
    if ignore_case is None:
        ignore_case = False
    if count_only is None:
        count_only = False
    if include_temp is None:
        include_temp = False
    if include_hidden is None:
        include_hidden = False

    # Validate boolean parameters: accept bool and integer 0/1; reject strings
    # and other types that would silently coerce (#887).  A non-empty string like
    # "false" is truthy — ignore_case="false" would make the search case-insensitive
    # when the caller intended case-sensitive.
    for _bname, _bval in (
        ("ignore_case", ignore_case),
        ("count_only", count_only),
        ("include_temp", include_temp),
        ("include_hidden", include_hidden),
    ):
        if isinstance(_bval, bool):
            pass
        elif isinstance(_bval, int) and _bval in (0, 1):
            pass
        else:
            _hint = " Pass true or false without quotes." if isinstance(_bval, str) else ""
            return (
                f"Error: '{_bname}' must be a boolean, "
                f"got {type(_bval).__name__!r}: {_bval!r}.{_hint}"
            )

    try:
        flags = re.IGNORECASE if ignore_case else 0
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"Error: invalid regex pattern: {e}"

    # Resolve search_path immediately to avoid absolute vs relative mismatch in relative_to()
    if path is None:
        path = "."
    elif not isinstance(path, str):
        return f"Error: path must be a string, got {type(path).__name__!r}"
    if '\x00' in path:
        return "Error: path contains a null byte, which is not allowed"
    path = path.strip()
    search_path = Path(path).resolve()

    # Confinement check: reject paths outside the working directory (#863).
    try:
        cwd_resolved = Path.cwd().resolve()
        cwd_prefix = str(cwd_resolved) + os.sep
        if search_path != cwd_resolved and not str(search_path).startswith(cwd_prefix):
            return (
                f"Error: path '{path}' resolves to '{search_path}' which is outside "
                f"the working directory '{cwd_resolved}'. "
                f"search_files only searches within the working directory."
            )
    except (OSError, ValueError):
        pass  # let the existing existence check handle OS errors

    if not search_path.exists():
        return f"Error: path '{path}' does not exist"

    # Validate context type before any comparison — must come before the single-file branch
    # so both code paths receive a properly-typed integer.
    # Booleans are a subclass of int in Python; reject them explicitly so that
    # context=True (silently 1) or context=False (silently 0) don't sneak through.
    if context is None:
        context = 3
    if isinstance(context, bool):
        return f"Error: context must be an integer, got 'bool': {context!r}. Pass a plain integer (e.g. context=3)."
    if isinstance(context, str):
        return (
            f"Error: context must be an integer, got 'str': {context!r}. "
            f"Pass an integer without quotes (e.g. context=3)."
        )
    if not isinstance(context, int):
        if isinstance(context, float) and not math.isfinite(context):
            return f"Error: context must be a finite integer, got {context!r}"
        try:
            context = int(context)
        except (TypeError, ValueError):
            return f"Error: context must be an integer, got {type(context).__name__!r}"
    if context < 0:
        return f"Error: context must be >= 0, got {context}"

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
    files_glob_skipped = 0
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
            if not any(_fnmatch.fnmatch(file_name, g) for g in glob_patterns):
                files_glob_skipped += 1
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

            abs_path = str(file_path.resolve())
            files_searched += 1
            file_has_match = False

            try:
                with file_path.open(encoding='utf-8', errors='ignore') as f:
                    if count_only:
                        file_hits = 0
                        for line in f:
                            if regex.search(line.rstrip()):
                                file_hits += 1
                        if file_hits > 0:
                            files_matched += 1
                            total_matches += file_hits
                        continue

                    if context == 0:
                        for line_num, line in enumerate(f, 1):
                            text_line = line.rstrip()
                            if regex.search(text_line):
                                file_has_match = True
                                total_matches += 1
                                match_lines.append(f"{abs_path}:{line_num}: {text_line}")
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
                                if not current_group:
                                    for b_num, b_text in buffer:
                                        current_group.append(f"{abs_path}:{b_num}- {b_text}")
                                current_group.append(f"{abs_path}:{line_num}: {text_line}")
                                lines_to_emit = context
                                if total_matches >= _MAX_RESULTS:
                                    truncated = True
                                    break
                            else:
                                if current_group:
                                    if lines_to_emit > 0:
                                        current_group.append(f"{abs_path}:{line_num}- {text_line}")
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
            if files_glob_skipped > 0:
                glob_display = glob_patterns[0] if len(glob_patterns) == 1 else str(glob_patterns)
                return (
                    header
                    + f"No files matched glob {glob_display!r} under '{resolved}'. "
                    + f"To search all files, omit glob= or pass glob='*'."
                )
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
                    "description": (
                        "File glob to filter, e.g. '*.py', '*.json', '*.md' (default: all files). "
                        "To match multiple extensions, pass a comma-separated string: '*.py,*.txt'. "
                        "A list of strings is also accepted: ['*.py', '*.txt']."
                    ),
                },
                "ignore_case": {
                    "type": "boolean",
                    "description": "Case-insensitive search (default: false). Pass true to match regardless of letter case.",
                    "default": False,
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
