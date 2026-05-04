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
    """Collect all .py files under root, applying DEFAULT_EXCLUDES."""
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
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
                    args_str = ""
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
                        context = f"def {child.name}({args_str}):"
                        matches.append({
                            "path": src_path,
                            "line": child.lineno,
                            "kind": node_kind,
                            "scope": child.name,
                            "context": context,
                        })
                _walk(child, class_stack)
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
        Returns [{"error": "..."}] for invalid arguments.
    """
    if mode not in _VALID_MODES:
        return [{"error": f"Invalid mode {mode!r}. Must be one of: {sorted(_VALID_MODES)}"}]
    if kind is not None and kind not in _VALID_KINDS:
        return [{"error": f"Invalid kind {kind!r}. Must be one of: {sorted(_VALID_KINDS)}"}]

    search_path = Path(path)
    if not search_path.exists():
        return []

    if search_path.is_file():
        py_files = [search_path] if search_path.suffix == ".py" else []
    else:
        py_files = _collect_py_files(search_path)

    single_file = search_path.is_file()
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

        # Use relative path if search_path is a directory, else just the filename
        if search_path.is_dir():
            try:
                display_path = str(py_file.relative_to(search_path))
            except ValueError:
                display_path = str(py_file)
        else:
            display_path = str(py_file)

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
            "Returns [{\"error\": \"SyntaxError: ...\", \"path\": \"...\"}] when a single "
            "target file cannot be parsed — check this before assuming a symbol is absent. "
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
