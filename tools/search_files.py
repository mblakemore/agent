"""Search files tool — grep through files for patterns.

SHARED RUNTIME — DO NOT MODIFY. This file is part of tool-agent/ and is used by all agents.
"""

import os
import re
from pathlib import Path


_MAX_RESULTS = 100
_MAX_CONTEXT = 20


def fn(
    pattern: str,
    path: str = ".",
    glob: str = "*",
    ignore_case: bool = True,
    context: int = 0,
) -> str:
    """Search file contents for a regex pattern.

    Args:
        pattern: Regex pattern to search for.
        path: Directory to search in (default: current directory).
        glob: File glob pattern to filter (default: * for all files).
        ignore_case: Case-insensitive search (default: True).
        context: Lines of context to include before/after each match, like
            grep -C. Capped at _MAX_CONTEXT. Default 0 (no context).
    """
    try:
        flags = re.IGNORECASE if ignore_case else 0
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"Error: invalid regex pattern: {e}"

    search_path = Path(path)
    if not search_path.exists():
        return f"Error: path '{path}' does not exist"

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
        if not file_path.is_file():
            continue
        rel = str(file_path.relative_to(search_path))
        if any(part.startswith(".") for part in file_path.parts if part != "."):
            continue
        if "__pycache__" in rel or "node_modules" in rel:
            continue

        files_searched += 1
        try:
            text = file_path.read_text(errors="ignore")
        except Exception:
            continue

        lines = text.splitlines()
        hit_nums: list[int] = []
        for line_num, line in enumerate(lines, 1):
            if regex.search(line):
                hit_nums.append(line_num)
                if total_matches + len(hit_nums) >= _MAX_RESULTS:
                    break

        if not hit_nums:
            continue

        files_matched += 1

        if context == 0:
            for n in hit_nums:
                match_lines.append(f"{rel}:{n}: {lines[n - 1].rstrip()}")
            total_matches += len(hit_nums)
            if total_matches >= _MAX_RESULTS:
                truncated = True
                break
            continue

        # context > 0: build merged windows
        total = len(lines)
        windows: list[list[int]] = []
        for n in hit_nums:
            lo = max(1, n - context)
            hi = min(total, n + context)
            if windows and lo <= windows[-1][1] + 1:
                if hi > windows[-1][1]:
                    windows[-1][1] = hi
            else:
                windows.append([lo, hi])

        hit_set = set(hit_nums)
        for lo, hi in windows:
            group: list[str] = []
            for n in range(lo, hi + 1):
                text_line = lines[n - 1].rstrip()
                if n in hit_set:
                    group.append(f"{rel}:{n}: {text_line}")
                else:
                    group.append(f"{rel}-{n}- {text_line}")
            context_groups.append(group)

        total_matches += len(hit_nums)
        if total_matches >= _MAX_RESULTS:
            truncated = True
            break

    header = f"[Searched {files_searched} files, {files_matched} matched, {total_matches} results"
    if truncated:
        header += " (truncated)"
    header += "]\n"

    if total_matches == 0:
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
            "Searches recursively through a directory. Supports context lines "
            "around each hit so you can disambiguate definitions, calls, and "
            "doc mentions in a single call. Use this to find patterns in code, "
            "search memory files, review past cycle logs, or locate specific "
            "content across the project."
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
                    "description": "Directory to search in (default: current directory).",
                    "default": ".",
                },
                "glob": {
                    "type": "string",
                    "description": "File glob to filter, e.g. '*.py', '*.json', '*.md' (default: all files).",
                    "default": "*",
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
                        "Default 0 (no context)."
                    ),
                    "default": 0,
                    "minimum": 0,
                },
            },
            "required": ["pattern"],
        },
    },
}
