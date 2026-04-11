"""Regression guard for CICD cycle 0014 — no dead top-level imports.

Cycle 0014 deleted five dead module-level imports across `tools/file.py`,
`tool_recovery.py`, `callbacks.py`, and `agent.py`. This test walks each file
with `ast`, collects every imported name, and asserts that each name is
actually referenced elsewhere in the file.

Baseline before cycle 0014: **5** dead imports (listed in issue #28).
After cycle 0014: **0**.

Scope is intentionally narrow — only the four files we cleaned up — so the
guard stays deterministic and matches the measurement in the plan. Expanding
the file list is fine; removing an entry is a regression.

Parallel to `tests/test_no_dead_locals.py` (cycle 0013, #24), which enforces
the same invariant one scope down.
"""

import ast
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

GUARDED_FILES = [
    "tools/file.py",
    "tool_recovery.py",
    "callbacks.py",
    "agent.py",
]


def _dead_top_level_imports(rel_path: str):
    """Return a list of (lineno, name) for imports in `rel_path` whose name
    never appears as a word anywhere else in the file source.

    Skips `from __future__` imports entirely — those are compile-time
    directives, not real names.
    """
    src = (REPO_ROOT / rel_path).read_text()
    tree = ast.parse(src)

    imported = {}  # name -> lineno
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                imported[name] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                name = alias.asname or alias.name
                imported[name] = node.lineno

    lines = src.splitlines()
    dead = []
    for name, lineno in imported.items():
        body = "\n".join(
            line for i, line in enumerate(lines, start=1) if i != lineno
        )
        if not re.search(r"\b" + re.escape(name) + r"\b", body):
            dead.append((lineno, name))
    return sorted(dead)


class TestNoDeadTopLevelImports(unittest.TestCase):
    def test_no_dead_imports_in_guarded_files(self):
        offenders = []
        for rel_path in GUARDED_FILES:
            for lineno, name in _dead_top_level_imports(rel_path):
                offenders.append(f"{rel_path}:{lineno}: {name}")
        self.assertEqual(
            offenders,
            [],
            "Dead top-level imports found (cycle 0014 regression):\n  "
            + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
