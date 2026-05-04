"""Find symbol tool — AST-aware Python symbol lookup."""

import ast
import os
from pathlib import Path
from typing import Optional

DEFAULT_EXCLUDES = [
    "temp/", "worktrees/", "state/debug/", ".venv",
    "node_modules/", "__pycache__/", ".git/",
]


def _is_excluded(path_str: str) -> bool:
    """Return True if any exclude pattern appears in the path string."""
    for excl in DEFAULT_EXCLUDES:
        if excl in path_str:
            return True
    return False


def _collect_py_files(root: Path) -> list[Path]:
    """Collect all .py files under root, applying DEFAULT_EXCLUDES.

    followlinks=True ensures that symlinked subdirectories are walked, so that
    .py files reachable via directory symlinks are not silently skipped (#828).
    """
    results = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        # Prune excluded directories in-place so os.walk won't descend into them.
        dirnames[:] = [
            d for d in dirnames
            if not _is_excluded(str(Path(dirpath) / d) + "/")
        ]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            fpath = Path(dirpath) / fname
            if not _is_excluded(str(fpath)):
                results.append(fpath)
    return results


def _find_definitions(tree: ast.AST, name: str, kind: Optional[str], src_path: str) -> list[dict]:
    """Walk AST and return definition matches."""
    matches = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name != name:
                continue
            node_kind = "function"
            # Determine if this is a method (parent is a ClassDef).
            # We check by seeing if there's a ClassDef ancestor — use a separate
            # parent-aware walk below.
        elif isinstance(node, ast.ClassDef):
            if node.name != name:
                continue
            node_kind = "class"
        else:
            continue

        if kind and node_kind != kind:
            continue

        # Build context line
        if isinstance(node, ast.ClassDef):
            context = f"class {node.name}:"
        else:
            args = ast.unparse(node.args) if hasattr(ast, "unparse") else "..."
            context = f"def {node.name}({args}):"

        matches.append({
            "path": src_path,
            "line": node.lineno,
            "kind": node_kind,
            "scope": node.name,
            "context": context,
        })
    return matches


def _find_definitions_with_scope(tree: ast.AST, name: str, kind: Optional[str], src_path: str) -> list[dict]:
    """Walk AST with parent tracking to distinguish methods from top-level functions."""
    matches = []

    def _walk(node, class_stack):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                if child.name == name and kind in (None, "class"):
                    if child.bases:
                        try:
                            bases_str = ", ".join(ast.unparse(b) for b in child.bases)
                        except Exception:
                            bases_str = "..."
                        context = f"class {child.name}({bases_str}):"
                    else:
                        context = f"class {child.name}:"
                    matches.append({
                        "path": src_path,
                        "line": child.lineno,
                        "kind": "class",
                        "scope": child.name,
                        "context": context,
                    })
                _walk(child, class_stack + [child.name])
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name == name:
                    node_kind = "method" if class_stack else "function"
                    if kind is None or kind == node_kind:
                        try:
                            args_str = ast.unparse(child.args)
                        except Exception:
                            args_str = "..."
                        prefix = "async def" if isinstance(child, ast.AsyncFunctionDef) else "def"
                        context = f"{prefix} {child.name}({args_str}):"
                        matches.append({
                            "path": src_path,
                            "line": child.lineno,
                            "kind": node_kind,
                            "scope": child.name,
                            "context": context,
                        })
                # Entering a function scope resets the class context — any function
                # nested inside this one is a closure/helper, not a method.
                _walk(child, [])
            else:
                _walk(child, class_stack)

    _walk(tree, [])
    return matches


def _find_callers(tree: ast.AST, name: str, src_path: str) -> list[dict]:
    """Walk AST and find Call nodes for the given symbol name."""
    matches = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        matched = False
        if isinstance(func, ast.Name) and func.id == name:
            matched = True
        elif isinstance(func, ast.Attribute) and func.attr == name:
            matched = True
        if matched and hasattr(node, "lineno"):
            try:
                context = ast.unparse(node)
            except Exception:
                context = f"{name}(...)"
            # Truncate long call expressions
            if len(context) > 120:
                context = context[:117] + "..."
            matches.append({
                "path": src_path,
                "line": node.lineno,
                "kind": "call",
                "scope": name,
                "context": context,
            })
    return matches


_VALID_MODES = {"definition", "callers", "both"}
_VALID_KINDS = {"function", "class", "method"}


def find_symbol(
    name: str,
    path: str = ".",
    kind: Optional[str] = None,
    mode: str = "definition",
) -> list[dict]:
    """Find Python symbols (functions, classes, methods, callers) using AST analysis.

    Args:
        name: Symbol name to search for.
        path: File or directory to search (default: current directory).
        kind: Filter by symbol kind — "function", "class", "method", or None for any.
        mode: "definition" to find where the symbol is defined, "callers" to find
              call sites, "both" to return both definitions and callers.

    Returns:
        List of match dicts with keys: path, line, kind, scope, context.
        Returns [{"error": "..."}] for invalid arguments, a non-existent path,
        a directory with no .py files, a single non-.py file, or a single file
        with a SyntaxError.
    """
    if not isinstance(name, str):
        return [{"error": f"name must be a string, got {type(name).__name__!r}"}]
    if not name.strip():
        return [{"error": "name must not be empty"}]
    if '\x00' in name:
        return [{"error": "name contains a null byte, which is not allowed"}]
    # Validate mode/kind types before normalization.
    if not isinstance(mode, str):
        return [{"error": f"mode must be a string, got {type(mode).__name__!r}"}]
    # kind=None means no filter; any other non-string is an error.
    if kind is not None and not isinstance(kind, str):
        return [{"error": f"kind must be a string or None, got {type(kind).__name__!r}"}]
    # Normalize mode and kind to lowercase so callers can pass 'Definition',
    # 'CALLERS', 'FUNCTION', etc. without getting a spurious validation error.
    mode = mode.lower()
    if isinstance(kind, str):
        kind = kind.lower()
    if mode not in _VALID_MODES:
        return [{"error": f"Invalid mode {mode!r}. Must be one of: {sorted(_VALID_MODES)}"}]
    if kind is not None and kind not in _VALID_KINDS:
        return [{"error": f"Invalid kind {kind!r}. Must be one of: {sorted(_VALID_KINDS)}"}]

    if not isinstance(path, str):
        return [{"error": f"path must be a string, got {type(path).__name__!r}"}]
    if '\x00' in path:
        return [{"error": "path contains a null byte, which is not allowed"}]
    if not path.strip():
        return [{"error": "path must be a non-empty string"}]
    search_path = Path(path.strip())
    try:
        cwd_resolved = Path.cwd().resolve()
        cwd_prefix = str(cwd_resolved) + os.sep
        resolved_path = search_path.resolve()
        if resolved_path != cwd_resolved and not str(resolved_path).startswith(cwd_prefix):
            return [{"error": (
                f"path '{path}' resolves to '{resolved_path}' which is outside "
                f"the working directory '{cwd_resolved}'. "
                f"find_symbol only searches within the working directory."
            )}]
    except (OSError, ValueError):
        pass  # Let the existing existence check handle OS errors
    try:
        path_exists = search_path.exists()
        path_is_file = search_path.is_file() if path_exists else False
    except OSError as exc:
        # Very long paths (> NAME_MAX bytes on Linux) raise ENAMETOOLONG (errno 36).
        # Treat any OS-level error during path resolution as "path does not exist"
        # rather than propagating an unhandled exception.
        return [{"error": f"path '{search_path}': {exc.strerror}"}]
    if not path_exists:
        return [{"error": f"path '{search_path}' does not exist"}]

    if path_is_file:
        if search_path.suffix != ".py":
            return [{"error": f"not a Python file: '{search_path}'. find_symbol only supports .py files."}]
        py_files = [search_path]
    else:
        py_files = _collect_py_files(search_path)

    single_file = path_is_file

    results = []

    for py_file in py_files:
        try:
            source = py_file.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError as exc:
            # When the caller targeted a single file, surface the parse failure
            # so the agent can distinguish "not found" from "file is broken".
            # When scanning a directory, silently skip broken files.
            if single_file:
                return [{"error": f"SyntaxError: {exc}", "path": str(py_file)}]
            continue
        except Exception:
            continue

        # Always use the absolute path so callers can open the file regardless of cwd.
        display_path = str(py_file.resolve())

        if mode in ("definition", "both"):
            defs = _find_definitions_with_scope(tree, name, kind, display_path)
            results.extend(defs)

        if mode in ("callers", "both"):
            callers = _find_callers(tree, name, display_path)
            results.extend(callers)

    return results


# Public alias expected by tool dispatch
fn = find_symbol

definition = {
    "type": "function",
    "function": {
        "name": "find_symbol",
        "description": (
            "Find Python symbols (functions, classes, methods, or call sites) using "
            "accurate AST-based analysis — no regex false positives. "
            "Prefer this over `file read` when you need to locate where a Python symbol "
            "is defined or called. Reading a 4000-line file to find one function wastes "
            "context; `find_symbol` returns just the definition in one call. "
            "Use mode='definition' to locate where a symbol is defined, "
            "mode='callers' to find all call sites, or mode='both' for both. "
            "Optionally filter by kind: 'function', 'class', or 'method'. "
            "Returns a list of matches, each with path, line number, kind, scope, and context. "
            "Returns [] when nothing is found. "
            "Returns [{\"error\": \"path '...' does not exist\"}] when path does not exist — "
            "this means the path is wrong, not that the symbol is absent. "
            "Returns [{\"error\": \"SyntaxError: ...\", \"path\": \"...\"}] when a single "
            "target file cannot be parsed — check this before assuming a symbol is absent. "
            "Returns [{\"error\": \"not a Python file: '...'\"}] when path points to a single "
            "non-.py file — find_symbol only supports .py files. "
            "Always check for 'error' in the first result before treating [] as 'not found'. "
            "IMPORTANT: always pass `path` explicitly with an absolute path when you know "
            "the directory you want to search."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The symbol name to search for (exact match).",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "File or directory to search. Default '.' is the process working "
                        "directory — pass an absolute path when you know the repo location."
                    ),
                    "default": ".",
                },
                "kind": {
                    "type": "string",
                    "enum": ["function", "class", "method"],
                    "description": (
                        "Restrict matches to a symbol kind. "
                        "Omit (or pass null) to match any kind."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["definition", "callers", "both"],
                    "description": (
                        "'definition' — find where the symbol is defined (default). "
                        "'callers' — find all call sites (foo() and obj.foo()). "
                        "'both' — definitions and callers."
                    ),
                    "default": "definition",
                },
            },
            "required": ["name"],
        },
    },
}
