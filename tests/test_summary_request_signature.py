"""Regression guard for CICD cycle 0016 (#36).

`_summary_request` in `agent.py` used to take a dead `log` positional parameter
that every caller passed and the function body never read. This test parses
`agent.py` as text (no `import agent` — avoids pulling network/runtime setup)
and asserts:

  1. `_summary_request`'s signature contains no parameter named `log`.
  2. No call site in `agent.py` passes a `log` identifier (bare `log` or a
     `.log` / `._log` attribute) as the second positional arg to
     `_summary_request`.

If a future edit re-introduces the dead wiring, this test fails and the
score-based metric from `plan/CICD/improvements/0016-summary-request-dead-log.md`
regresses from 0.
"""

import ast
import pathlib
import unittest


_AGENT_PY = pathlib.Path(__file__).resolve().parent.parent / "agent.py"


def _parse_agent() -> ast.Module:
    return ast.parse(_AGENT_PY.read_text())


class SummaryRequestSignatureTest(unittest.TestCase):
    def test_signature_has_no_log_parameter(self):
        tree = _parse_agent()
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_summary_request":
                found = True
                arg_names = [a.arg for a in node.args.args]
                self.assertNotIn(
                    "log",
                    arg_names,
                    f"_summary_request signature still carries dead 'log' param: {arg_names}",
                )
        self.assertTrue(found, "_summary_request not found in agent.py")

    def test_no_call_site_passes_log_positional(self):
        tree = _parse_agent()
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "_summary_request"):
                continue
            if len(node.args) < 2:
                continue
            second = node.args[1]
            if isinstance(second, ast.Name) and "log" in second.id:
                offenders.append(f"line {node.lineno}: positional '{second.id}'")
            elif isinstance(second, ast.Attribute) and "log" in second.attr:
                offenders.append(f"line {node.lineno}: positional '.{second.attr}'")
        self.assertEqual(
            offenders,
            [],
            "_summary_request call sites still pass a dead log positional: "
            + "; ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
