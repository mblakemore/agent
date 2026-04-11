"""Unit tests for callbacks.py — NullCallbacks, TerminalCallbacks, safe_cb."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import callbacks


class TestNullCallbacks(unittest.TestCase):
    def test_all_hooks_return_none(self):
        cb = callbacks.NullCallbacks()
        # Sample from each category — every hook should be a no-op
        self.assertIsNone(cb.check_cancelled())
        self.assertIsNone(cb.on_session_start({}))
        self.assertIsNone(cb.on_api_retry("err", 1, 3, 2.0))
        self.assertIsNone(cb.on_stream_chunk("x"))
        self.assertIsNone(cb.on_assistant_text("txt"))
        self.assertIsNone(cb.on_tool_batch_start(1))
        self.assertIsNone(cb.on_tool_start("t", {}))
        self.assertIsNone(cb.on_tool_result("t", {}, "r", False))
        self.assertIsNone(cb.on_forced_think("t", 1))
        self.assertIsNone(cb.on_overtime("text_only"))
        self.assertIsNone(cb.on_notice("info", "m"))
        self.assertIsNone(cb.on_error("e"))


class TestTerminalCallbacks(unittest.TestCase):
    def test_construction_defaults(self):
        cb = callbacks.TerminalCallbacks()
        self.assertFalse(cb.verbose)
        self.assertEqual(cb.compact_limit, 400)
        self.assertEqual(len(cb.tool_history), 0)
        self.assertEqual(cb.tool_history.maxlen, 50)
        self.assertFalse(cb._last_was_stream)

    def test_tool_history_records_results(self):
        cb = callbacks.TerminalCallbacks()
        # swallow stdout
        cb._print = lambda *a, **kw: None
        cb.on_tool_result("file", {"action": "read"}, "some result", False)
        cb.on_tool_result("exec", {"cmd": "ls"}, "err-output", True)
        self.assertEqual(len(cb.tool_history), 2)
        name, args, result, is_err = cb.tool_history[0]
        self.assertEqual(name, "file")
        self.assertEqual(args, {"action": "read"})
        self.assertEqual(result, "some result")
        self.assertFalse(is_err)
        self.assertTrue(cb.tool_history[1][3])

    def test_tool_history_max_size(self):
        cb = callbacks.TerminalCallbacks(tool_history_size=3)
        cb._print = lambda *a, **kw: None
        for i in range(5):
            cb.on_tool_result(f"t{i}", {}, "r", False)
        self.assertEqual(len(cb.tool_history), 3)
        # Oldest two dropped
        self.assertEqual(cb.tool_history[0][0], "t2")
        self.assertEqual(cb.tool_history[-1][0], "t4")

    def test_compact_args_truncates_long_values(self):
        cb = callbacks.TerminalCallbacks()
        out = cb._compact_args({"path": "x" * 80}, max_val=20)
        self.assertLess(len(out), 60)
        self.assertIn("…", out)

    def test_render_tools_empty(self):
        cb = callbacks.TerminalCallbacks()
        self.assertEqual(cb.render_tools(), "No tool calls yet.")

    def test_render_tools_populated(self):
        cb = callbacks.TerminalCallbacks()
        cb._print = lambda *a, **kw: None
        cb.on_tool_result("file", {"action": "read"}, "line1\nline2", False)
        out = cb.render_tools()
        self.assertIn("file", out)
        self.assertIn("line1", out)

    def test_stream_then_assistant_text_no_double_print(self):
        cb = callbacks.TerminalCallbacks()
        emitted = []
        cb._print = lambda text="", end="\n": emitted.append(text)
        # Chunks go through on_stream_chunk (raw print, bypasses _print)
        cb._last_was_stream = True  # simulate a streamed turn
        cb.on_assistant_text("full text")
        # Should not re-emit via _print — the text was already streamed
        self.assertEqual(emitted, [])
        self.assertFalse(cb._last_was_stream)

    def test_assistant_text_without_stream_prints(self):
        cb = callbacks.TerminalCallbacks()
        emitted = []
        cb._print = lambda text="", end="\n": emitted.append(text)
        cb.on_assistant_text("hello")
        self.assertEqual(emitted, ["hello"])

    def test_verbose_off_compacts_long_results(self):
        cb = callbacks.TerminalCallbacks(compact_limit=20)
        captured = []
        cb._print = lambda text="", end="\n": captured.append(text)
        long_result = "x" * 100
        cb.on_tool_result("t", {}, long_result, False)
        # The compacted display should be shorter than the raw result
        self.assertTrue(any("truncated" in c for c in captured))
        # History keeps the full result (D12)
        self.assertEqual(cb.tool_history[0][2], long_result)

    def test_verbose_on_shows_full_result(self):
        cb = callbacks.TerminalCallbacks(verbose=True, compact_limit=20)
        captured = []
        cb._print = lambda text="", end="\n": captured.append(text)
        long_result = "x" * 100
        cb.on_tool_result("t", {}, long_result, False)
        self.assertTrue(any("x" * 100 in c for c in captured))
        self.assertFalse(any("truncated" in c for c in captured))

    def test_signal_args_surface_in_output(self):
        """Cycle 0017 regression: on_cancelled/on_forced_think/on_text_loop_detected
        must surface every arg they receive — not just print a placeholder.
        See plan/CICD/improvements/0017-callbacks-surface-signal.md."""
        cb = callbacks.TerminalCallbacks()
        captured = []
        cb._print = lambda text="", end="\n": captured.append(text)
        cb.on_cancelled("streaming")
        cb.on_forced_think("exec_command", 3)
        cb.on_text_loop_detected(5)
        joined = "\n".join(captured)
        for token in ("streaming", "exec_command", "3", "5"):
            self.assertIn(token, joined,
                          f"token {token!r} missing from hook output: {joined!r}")


class TestHookWiring(unittest.TestCase):
    """Confirms that the loop emit sites actually reach the installed cb."""

    def _counting_cb(self):
        counts = {}

        class Counter(callbacks.NullCallbacks):
            def on_auto_nudge(self_inner, n, max_n):
                counts.setdefault("nudge", []).append((n, max_n))

            def on_tool_recovery(self_inner, name, attempt):
                counts.setdefault("recovery", []).append((name, attempt))

        return Counter(), counts

    def test_emit_auto_nudge_reaches_cb(self):
        import agent
        cb, counts = self._counting_cb()
        prev_cb, prev_log = agent._cb, agent._cb_log
        agent._cb, agent._cb_log = cb, None
        try:
            agent._emit("on_auto_nudge", 1, 3)
        finally:
            agent._cb, agent._cb_log = prev_cb, prev_log
        self.assertEqual(counts.get("nudge"), [(1, 3)])

    def test_emit_tool_recovery_reaches_cb(self):
        import agent
        cb, counts = self._counting_cb()
        prev_cb, prev_log = agent._cb, agent._cb_log
        agent._cb, agent._cb_log = cb, None
        try:
            agent._emit("on_tool_recovery", "file", 1)
        finally:
            agent._cb, agent._cb_log = prev_cb, prev_log
        self.assertEqual(counts.get("recovery"), [("file", 1)])

    def test_nudge_emit_site_present_in_source(self):
        """Guard against accidental removal of the emit line in the loop body."""
        from pathlib import Path
        src = (Path(__file__).parent.parent / "agent.py").read_text()
        self.assertIn('_emit("on_auto_nudge"', src)

    def test_tool_recovery_emit_site_present_in_source(self):
        from pathlib import Path
        src = (Path(__file__).parent.parent / "agent.py").read_text()
        self.assertIn('_emit("on_tool_recovery"', src)


class TestHookInterfaceShape(unittest.TestCase):
    """Dead/live hook assertions — guards Phase A § 7.4 deletion and § 7.2/7.3 emits."""

    def test_truncation_hooks_removed(self):
        """on_truncation_* were unwired dead surface; they must stay gone."""
        self.assertFalse(hasattr(callbacks.NullCallbacks, "on_truncation_recovered"))
        self.assertFalse(hasattr(callbacks.NullCallbacks, "on_truncation_failed"))
        self.assertFalse(hasattr(callbacks.TerminalCallbacks, "on_truncation_recovered"))
        self.assertFalse(hasattr(callbacks.TerminalCallbacks, "on_truncation_failed"))

    def test_wired_recovery_hooks_present(self):
        """on_auto_nudge / on_tool_recovery are emitted from the loop."""
        self.assertTrue(hasattr(callbacks.NullCallbacks, "on_auto_nudge"))
        self.assertTrue(hasattr(callbacks.NullCallbacks, "on_tool_recovery"))
        self.assertTrue(hasattr(callbacks.TerminalCallbacks, "on_auto_nudge"))
        self.assertTrue(hasattr(callbacks.TerminalCallbacks, "on_tool_recovery"))

    def test_no_dead_params_on_assistant_text_or_context_recovery(self):
        """Cycle 0018 regression: on_assistant_text and on_context_recovery
        must not regrow a parameter whose only call-site value is a literal.
        See plan/CICD/improvements/0018-callbacks-dead-params.md."""
        import inspect
        at = inspect.signature(callbacks.TerminalCallbacks.on_assistant_text)
        self.assertEqual(list(at.parameters.keys()), ["self", "text"])
        cr = inspect.signature(callbacks.TerminalCallbacks.on_context_recovery)
        self.assertEqual(list(cr.parameters.keys()), ["self"])
        at_null = inspect.signature(callbacks.NullCallbacks.on_assistant_text)
        self.assertEqual(list(at_null.parameters.keys()), ["self", "text"])
        cr_null = inspect.signature(callbacks.NullCallbacks.on_context_recovery)
        self.assertEqual(list(cr_null.parameters.keys()), ["self"])

    def test_no_dead_null_callbacks_hooks(self):
        """Cycle 0019 regression: every public hook declared on NullCallbacks
        must have at least one non-definition call site in the repo source.
        `check_cancelled` is excluded — it's a query-style hook governed by
        the callbacks.py:12 'no raise except check_cancelled' rule, not an
        event hook on the _emit/safe_cb dispatch path.
        See plan/CICD/improvements/0019-dead-null-hooks.md."""
        import ast
        import inspect
        import os
        import re

        repo_root = Path(__file__).parent.parent
        cb_path = repo_root / "callbacks.py"
        tree = ast.parse(cb_path.read_text())

        hooks = []  # list of (lineno, name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "NullCallbacks":
                for item in node.body:
                    if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    name = item.name
                    if name.startswith("_"):
                        continue
                    if name == "check_cancelled":
                        continue
                    hooks.append((item.lineno, name))

        self.assertTrue(hooks, "NullCallbacks has no hook methods — test is misconfigured")

        dead = []
        for lineno, name in hooks:
            pattern = re.compile(r"\b" + re.escape(name) + r"\b")
            hits = 0
            for root, _, files in os.walk(repo_root):
                if os.sep + ".git" in root:
                    continue
                for fn in files:
                    if not fn.endswith(".py"):
                        continue
                    path = os.path.join(root, fn)
                    with open(path) as f:
                        for i, line in enumerate(f, 1):
                            if pattern.search(line):
                                if path == str(cb_path) and i == lineno:
                                    continue
                                hits += 1
            if hits == 0:
                dead.append(name)

        self.assertEqual(
            dead, [],
            f"NullCallbacks declares hook stubs that no call site in the repo "
            f"ever invokes (via _emit, safe_cb, or direct attribute access): "
            f"{dead}. Either wire them up or delete the stubs."
        )


class TestSafeCb(unittest.TestCase):
    def test_calls_method_and_returns_value(self):
        class C(callbacks.NullCallbacks):
            def on_notice(self, level, msg):
                return f"{level}:{msg}"
        self.assertEqual(callbacks.safe_cb(C(), "on_notice", "info", "m"), "info:m")

    def test_swallows_exception(self):
        class C(callbacks.NullCallbacks):
            def on_notice(self, level, msg):
                raise RuntimeError("boom")
        # Should not raise
        self.assertIsNone(callbacks.safe_cb(C(), "on_notice", "info", "m"))

    def test_missing_method_is_noop(self):
        cb = callbacks.NullCallbacks()
        self.assertIsNone(callbacks.safe_cb(cb, "on_nonexistent_hook"))

    def test_logs_exception_when_log_given(self):
        class DummyLog:
            def __init__(self):
                self.calls = []
            def exception(self, *args, **kwargs):
                self.calls.append(args)

        class C(callbacks.NullCallbacks):
            def on_error(self, msg):
                raise ValueError("x")

        log = DummyLog()
        callbacks.safe_cb(C(), "on_error", "test", log=log)
        self.assertEqual(len(log.calls), 1)


class TestTerminalCallbacksDispatchArms(unittest.TestCase):
    """Cycle 0020 regression: TerminalCallbacks string-switch bodies must
    not contain a dispatch arm whose guarding literal (or dict-key / else
    fallback) is unreachable given the actual set of literals that
    `_emit` / `safe_cb` call sites in the repo produce for that hook.
    """

    TARGET_HOOKS = (
        "on_notice",
        "on_hallucination_stripped",
        "on_overtime",
    )

    def _repo_root(self):
        return Path(__file__).parent.parent

    def test_no_known_dead_dispatch_arms(self):
        """The 3 specific dead-arm markers from cycle 0020 baseline must
        not reappear in callbacks.py source."""
        src = (self._repo_root() / "callbacks.py").read_text()
        dead_markers = [
            ('on_notice "error" elif arm',
             'elif level == "error":'),
            ('on_hallucination_stripped unknown-kind fallback',
             "[hallucination stripped: {kind}]"),
            ('on_overtime unknown-reason fallback',
             "[overtime: {reason}]"),
        ]
        found = [label for label, marker in dead_markers if marker in src]
        self.assertFalse(
            found,
            "Dead dispatch arm(s) reappeared in callbacks.py: " + ", ".join(found)
        )

    def _collect_emitted_keys(self):
        """Walk every .py file under the repo root (skipping .git, tests,
        plan) and collect the set of string literals passed as the first
        positional argument to `_emit("hook", ...)` or
        `safe_cb(cb, "hook", ...)` for each target hook. Returns
        (emitted_map, dynamic_sites).
        """
        import ast
        emitted = {h: set() for h in self.TARGET_HOOKS}
        dynamic = []
        root = self._repo_root()
        for path in root.rglob("*.py"):
            rel = path.relative_to(root)
            parts = rel.parts
            if parts[0] in (".git", "tests", "plan"):
                continue
            try:
                tree = ast.parse(path.read_text(), str(rel))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                hook = None
                arg_offset = 0
                if isinstance(func, ast.Name) and func.id == "_emit":
                    if node.args and isinstance(node.args[0], ast.Constant):
                        hook = node.args[0].value
                        arg_offset = 1
                elif isinstance(func, ast.Name) and func.id == "safe_cb":
                    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                        hook = node.args[1].value
                        arg_offset = 2
                if hook not in self.TARGET_HOOKS:
                    continue
                if arg_offset >= len(node.args):
                    continue
                first = node.args[arg_offset]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    emitted[hook].add(first.value)
                else:
                    dynamic.append((str(rel), node.lineno, hook))
        return emitted, dynamic

    def _classify_hook_body(self, body, arg_name):
        """Walk a TerminalCallbacks method body; return (literal_keys,
        has_else_fallback, has_default_fallback).

        - literal_keys: strings the body explicitly dispatches on, either
          via `if/elif <arg> == "lit":` or via dict literal keys used in
          `dict.get(<arg>, ...)` or `dict[<arg>]` expressions.
        - has_else_fallback: True iff any top-level if/elif chain that
          compares against string literals has a trailing else: with a
          non-pass body.
        - has_default_fallback: True iff the body contains a
          `dict.get(<arg>, default)` where default is not None.
        """
        import ast
        literal_keys = set()
        has_else_fallback = False
        has_default_fallback = False

        def walk_if_chain(if_stmt):
            nonlocal has_else_fallback
            t = if_stmt.test
            is_literal_cmp = False
            if isinstance(t, ast.Compare) and len(t.ops) == 1 and isinstance(t.ops[0], ast.Eq):
                left, right = t.left, t.comparators[0]
                if (isinstance(left, ast.Name) and left.id == arg_name
                        and isinstance(right, ast.Constant)
                        and isinstance(right.value, str)):
                    literal_keys.add(right.value)
                    is_literal_cmp = True
            if if_stmt.orelse:
                # If orelse is a single If → elif; otherwise else branch.
                if len(if_stmt.orelse) == 1 and isinstance(if_stmt.orelse[0], ast.If):
                    walk_if_chain(if_stmt.orelse[0])
                else:
                    # Trailing else: any non-pass body → fallback present.
                    if is_literal_cmp and not (
                        len(if_stmt.orelse) == 1
                        and isinstance(if_stmt.orelse[0], ast.Pass)
                    ):
                        has_else_fallback = True

        for stmt in body:
            if isinstance(stmt, ast.If):
                walk_if_chain(stmt)
        for sub in ast.walk(ast.Module(body=body, type_ignores=[])):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                # dict.get(arg, default) on a Dict literal.
                if (sub.func.attr == "get"
                        and isinstance(sub.func.value, ast.Dict)):
                    for k in sub.func.value.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            literal_keys.add(k.value)
                    if len(sub.args) >= 2:
                        default = sub.args[1]
                        if not (isinstance(default, ast.Constant) and default.value is None):
                            has_default_fallback = True
            if isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Dict):
                for k in sub.value.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        literal_keys.add(k.value)
        return literal_keys, has_else_fallback, has_default_fallback

    def test_dispatch_arms_are_reachable(self):
        """AST-driven guard: every explicit arm in TerminalCallbacks.<hook>
        must correspond to an emitted literal, and any else/default
        fallback must be reachable via at least one emitted literal that
        isn't covered by the explicit arms."""
        import ast
        emitted, dynamic = self._collect_emitted_keys()
        self.assertFalse(
            dynamic,
            "Dynamic-key call sites for target hooks (expected all literals): "
            + str(dynamic)
        )

        src = (self._repo_root() / "callbacks.py").read_text()
        tree = ast.parse(src)
        term_cls = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "TerminalCallbacks":
                term_cls = node
                break
        self.assertIsNotNone(term_cls, "TerminalCallbacks class not found")

        methods = {
            item.name: item
            for item in term_cls.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        for hook in self.TARGET_HOOKS:
            with self.subTest(hook=hook):
                self.assertIn(hook, methods, f"{hook} missing from TerminalCallbacks")
                meth = methods[hook]
                # First non-self arg is the switch key.
                args = meth.args.args
                self.assertGreaterEqual(
                    len(args), 2,
                    f"{hook} must take at least one switch arg besides self"
                )
                arg_name = args[1].arg
                literal_keys, has_else, has_default = self._classify_hook_body(
                    meth.body, arg_name
                )
                emit_set = emitted[hook]

                # Invariant 1: every literal handled must be emitted.
                dead_literals = literal_keys - emit_set
                self.assertFalse(
                    dead_literals,
                    f"{hook}: literal(s) {sorted(dead_literals)} are handled "
                    f"but no call site emits them "
                    f"(emitted={sorted(emit_set)})"
                )

                # Invariant 2: else fallback is reachable only if some
                # emitted literal isn't covered by the explicit chain.
                uncovered = emit_set - literal_keys
                if has_else:
                    self.assertTrue(
                        uncovered,
                        f"{hook}: has an else: fallback branch but every "
                        f"emitted literal ({sorted(emit_set)}) is already "
                        f"covered by an explicit arm — else is dead code"
                    )

                # Invariant 3: dict.get(arg, default) default is reachable
                # only if some emitted literal isn't a dict key.
                if has_default:
                    self.assertTrue(
                        uncovered,
                        f"{hook}: has a dict.get(arg, default) with a "
                        f"non-None default but every emitted literal "
                        f"({sorted(emit_set)}) is already a dict key — "
                        f"default is dead code"
                    )


if __name__ == "__main__":
    unittest.main()
