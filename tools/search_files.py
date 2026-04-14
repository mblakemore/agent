"""Search files tool — grep through files for patterns."""

import re
from pathlib import Path
from collections import deque

_MAX_RESULTS = 100
_MAX_CONTEXT = 20


def fn(
    pattern: str,
    path: str = ".",
    glob: str = "*",
    ignore_case: bool = True,
    context: int = 3,
    count_only: bool = False,
) -> str:
    """Search file contents for a regex pattern.

    Args:
        pattern: Regex pattern to search for.
        path: Directory to search in (default: current directory).
        glob: File glob pattern to filter (default: * for all files).
        ignore_case: Case-insensitive search (default: True).
        context: Lines of context to include before/after each match, like
            grep -C. Capped at _MAX_CONTEXT. Default 3; pass 0 to get the
            legacy bare-match shape.
        count_only: If True, return only the match count summary (files
            searched, files matched, total matches) without the match lines.
            Use this when you only need to know how many matches exist.
    """
    if not pattern or not pattern.strip():
        return "Error: Search pattern cannot be empty."

    try:
        flags = re.IGNORECASE if ignore_case else 0
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"Error: invalid regex pattern: {e}"

    search_path = Path(path)
    if not search_path.exists():
        return f"Error: path '{path}' does not exist"

    try:
        resolved = search_path.resolve()
    except (OSError, RuntimeError):
        resolved = search_path.absolute()

    if context < 0:
        context = 0
    elif context > _MAX_CONTEXT:
        context = _MAX_CONTEXT

    match_lines: list[str] = []
    context_groups: list[list[str]] = []
    total_matches = 0
    files_searched = 0
    files_matched = 0
    truncated = False

    for file_path in sorted(search_path.rglob(glob)):
        if truncated:
            break
        if not file_path.is_file():
            continue
        rel = str(file_path.relative_to(search_path))
        if any(part.startswith(".") for part in file_path.parts if part != "." and part != ".agent"):
            continue
        if "__pycache__" in rel or "node_modules" in rel:
            continue

        files_searched += 1
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
                            files_matched += 1
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
                            files_matched += 1
                            total_matches += 1
                            if not current_group:
                                for b_num, b_text in buffer:
                                    current_group.append(f"{rel}-{b_num}- {b_text}")
                            current_group.append(f"{rel}:{line_num}: {text_line}")
                            lines_to_emit = context
                        else:
                            if current_group:
                                if lines_to_emit > 0:
                                    current_group.append(f"{rel}-{line_num}- {text_line}")
                                    lines_to_emit -= 1
                                else:
                                    context_groups.append(current_group)
                                    current_group = []
                            
                            buffer.append((line_num, text_line))
                    
                    if current_group:
                        context_groups.append(current_group)

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


definition = {
    "type": "function",
    "function": {
        "name": "search_files",
        "description": (
            "Search file contents for a regex pattern (like grep). "
            "Searches recursively through a directory and returns each hit "
            "with surrounding context lines by default, so you can tell a "
            "definition from a call from a documentation mention without "
            "needing a follow-up file read. Use this to find patterns in "
            "code, search memory files, review past cycle logs, or locate "
            "specific content across the project. Prefer this over reading "
            "whole files when you only need to look at a handful of matches. "
            "Pass count_only=true when you only need to look at a handful of matches. "
            "Pass count_only=true when you only need a match count (e.g. "
            "how many TODOs, test methods, or call sites) — returns just the "
            "summary line without match content. "
            "IMPORTANT: always pass `path` explicitly when you know the "
            "directory you want to search. The default `'.'` is the process "
            "working directory, which in automation mode is usually an empty "
            "temp dir — not the repo the user asked about."
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
                        "process working directory, which in automation "
                        "mode is usually an empty temp dir — pass the "
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
                        "Lines of context to show before and after each hit, "
                        "like grep -C. Matched lines are emitted as "
                        "'path:line: text'; context lines use 'path-line- text'. "
                        "Disjoint groups are separated by '--'. Capped at 20. "
                        "Default 3 — pass 0 only if you want the legacy "
                        "bare-match shape."
                        ),
                    "default": 3,
                    "minimum": 0,
                },
                "count_only": {
                    "type": "boolean",
                    "description": (
                        "Return only the match count summary (files searched, "
                        "files matched, total matches) without the match lines "
                        "themselves. Use this when you only need to know how many matches exist, not where they are. Default: false."
                        ),
                    "default": False,
                },
            },
            "required": ["pattern"],
        },
    },
}
