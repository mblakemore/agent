"""Regression guard for CICD cycle 0013 — no dead local assignments.

Cycle 0013 deleted five assigned-but-never-read locals across `tools/file.py`,
`agent.py`, and `tool_recovery.py`. This test walks each touched function with
`ast` and fails if a new dead local slips back in.

Baseline before cycle 0013: **5** dead locals (listed in issue #24).
After cycle 0013: **0**.

Scope is intentionally narrow — only the four functions we cleaned up — so
the guard stays deterministic. Expanding the whitelist is fine; removing an
entry is a regression.
"""

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# (file, qualified function name). Nested functions are walked by walking the
# outer function's body until a FunctionDef with the matching name is found.
GUARDED_FUNCTIONS = [
    ("tools/file.py", "_resolve_path"),
    ("agent.py", "run_agent_single"),
    ("agent.py", "_worker"),                 # nested inside _AsyncSummarizer.kick
    ("tool_recovery.py", "_ask_for_param"),
]


def _find_function(tree, target_name):
    """Return the first FunctionDef matching `target_name`, or None."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == target_name:
            return node
    return None


def _collect_arg_names(func):
    """All names the function binds through its signature."""
    args = func.args
    names = set()
    for a in args.args + args.kwonlyargs + args.posonlyargs:
        names.add(a.arg)
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _dead_locals(func):
    """Names assigned inside `func` but never loaded.

    Treats as 'used':
      - function arguments
      - for-loop targets (idiomatic — `for i in range(n)` is still a use)
      - augmented-assignment targets (`x += 1` both stores and loads)
      - walrus targets (`while (chunk := f.read()):`)
      - except-handler bindings (`except E as e:` — `e` is scoped but may
        be referenced in the body)
      - names that are also read anywhere in the function

    Does NOT walk into nested FunctionDef bodies — each nested function is
    checked independently if it appears in GUARDED_FUNCTIONS.
    """
    stores = set()
    loads = set()

    def walk(node, inside_nested=False):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef) and child is not func:
                # Skip nested function bodies — they have their own scope.
                continue
            if isinstance(child, ast.Name):
                if isinstance(child.ctx, ast.Store):
                    stores.add(child.id)
                elif isinstance(child.ctx, ast.Load):
                    loads.add(child.id)
            if isinstance(child, ast.AugAssign) and isinstance(child.target, ast.Name):
                # x += 1 — target is both read and written.
                loads.add(child.target.id)
            if isinstance(child, ast.NamedExpr) and isinstance(child.target, ast.Name):
                # (x := expr) — walrus. Target is typically read later, but
                # even if it isn't, we treat it as used because the walrus
                # is semantically "bind and evaluate".
                loads.add(child.target.id)
            if isinstance(child, ast.For) and isinstance(child.target, ast.Name):
                loads.add(child.target.id)
            if isinstance(child, ast.ExceptHandler) and child.name:
                loads.add(child.name)
            walk(child)

    walk(func)
    args = _collect_arg_names(func)
    return stores - loads - args


class TestNoDeadLocals(unittest.TestCase):
    def test_guarded_functions_have_no_dead_locals(self):
        failures = []
        for rel_path, func_name in GUARDED_FUNCTIONS:
            path = REPO_ROOT / rel_path
            tree = ast.parse(path.read_text())
            func = _find_function(tree, func_name)
            self.assertIsNotNone(
                func,
                f"{rel_path}: function '{func_name}' not found — "
                f"did it get renamed? Update GUARDED_FUNCTIONS.",
            )
            dead = _dead_locals(func)
            if dead:
                failures.append(f"{rel_path}::{func_name} — {sorted(dead)}")

        self.assertEqual(
            failures,
            [],
            "Dead local assignments reintroduced. Cycle 0013 deleted these; "
            "do not add them back without reading the comment on their use:\n  "
            + "\n  ".join(failures),
        )


if __name__ == "__main__":
    unittest.main()
