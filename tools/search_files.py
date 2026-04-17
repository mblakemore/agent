"""Search files tool — grep through files for patterns."""

import os
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
            Use this when you only need to know how many matches exist, not where they are.
    """
    if not pattern or not pattern.strip():
        return "Error: Search pattern cannot be empty."

    try:
        flags = re.IGNORECASE if ignore_case else 0
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"Error: invalid regex pattern: {e}"

    # Resolve search_path immediately to avoid absolute vs relative mismatch in relative_to()
    search_path = Path(path).resolve()
    if not search_path.exists():
        return f"Error: path '{path}' does not exist"

    # Since search_path is now already resolved, we can use it directly for os.walk
    resolved = search_path

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
    permission_errors = 0

    # We use os.walk instead of rglob to capture PermissionErrors via the onerror callback.
    # os.walk(top, topdown=True, onerror=onerror)
    def handle_error(os_error):
        nonlocal permission_errors
        if isinstance(os_error, PermissionError):
            permission_errors += 1

    for root, dirs, files in os.walk(resolved, onerror=handle_error):
        # Filter directories to skip hidden ones (mimicking rglob behavior with .agent filter)
        # Modifying 'dirs' in-place allows os.walk to prune the search tree.
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != ".agent"]
        
        # Further exclude common large/unnecessary directories
        if "__pycache__" in root or "node_modules" in root:
            continue
            
        for file_name in files:
            # Basic glob filter. For simplicity and consistency with the previous 
            # rglob implementation, we'll support basic globbing via fnmatch.
            # If the user provided a specific glob like '*.py', we filter here.
            import fnmatch
            if not fnmatch.fnmatch(file_name, glob):
                continue
            
            # Skip hidden files
            if file_name.startswith("."):
                continue

            file_path = Path(root) / file_name
            
            if truncated:
                break
            
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
                        if file_has_match:
                            files_matched += 1

                    if not count_only:
                        if len(match_lines) >= _MAX_RESULTS or len(context_groups) >= _MAX_RESULTS:
                            truncated = True

            except Exception:
                # Individual file open errors are still skipped silently as before, 
                # but we don't count them as 'PermissionError' for the directory-level skip.
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


definition = {
    "type": "function",
    "function": {
        "name": "search_files",
        "description": (
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
                              "Capped at 20. Default 3 — pass 0 only if you want the legacy "
                              "bare-match shape."
                              ),
                          "default": 3,
                          "minimum": 0,
                      },
                      "count_only": {
                          "type": "boolean",
                          "description": "If True, return only the match count summary (files searched, files matched, total matches) without the match lines. Use this when you only need to know how many matches exist, not where they are. Default: false.",
                          "default": False,
                      },
                  },
                  "required": ["pattern"],
              },
          },
      }
