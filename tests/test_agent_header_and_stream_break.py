"""Field report 2026-08-14, three display contracts around the turn loop:

1. STATUS MUST NOT SPLIT A STREAMED BLOCK — the token-stats line and the
   'preparing tool calls' status printed mid-sentence because the reasoning
   renderer's tag-parsing holdback still owned the tail of the last line.
   The tool-call transition now flushes the renderer FIRST.

2. THE Agent: HEADER IS DATA-DRIVEN — printed lazily on the first text
   token, once per contiguous agent-output run. A tool-only response never
   prints a bare label; a queued user message folded in mid-burst starts a
   new run (the label re-appears as a continuation marker).

3. Assistant: renamed to Agent:.
"""

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import spinner


class TestDeferredHeader(unittest.TestCase):
    """spinner.StreamStatus defer_header contract (non-interactive path)."""

    def _status(self):
        return spinner.StreamStatus(emit=lambda *a: None)

    def test_deferred_header_not_printed_at_start(self):
        st = self._status()
        st._interactive = False
        with mock.patch.object(spinner, "_pt_app", return_value=None), \
             mock.patch.object(spinner.sys, "stdout") as m_stdout:
            st.start("\nAgent: ", defer_header=True)
            writes = "".join(c.args[0] for c in m_stdout.write.call_args_list)
            self.assertNotIn("Agent:", writes)

    def test_deferred_header_prints_once_at_first_token(self):
        st = self._status()
        st._interactive = False
        with mock.patch.object(spinner, "_pt_app", return_value=None), \
             mock.patch.object(spinner.sys, "stdout") as m_stdout:
            st.start("\nAgent: ", defer_header=True)
            st.first_token()
            writes = "".join(c.args[0] for c in m_stdout.write.call_args_list)
            self.assertEqual(writes.count("Agent:"), 1)
            self.assertTrue(writes.startswith("\n"), "leading newline deferred too")
            st.first_token()  # idempotent — never twice
            writes = "".join(c.args[0] for c in m_stdout.write.call_args_list)
            self.assertEqual(writes.count("Agent:"), 1)

    def test_tool_only_response_never_prints_header(self):
        """No first_token (no text) -> finish() drops the deferred header."""
        st = self._status()
        st._interactive = False
        with mock.patch.object(spinner, "_pt_app", return_value=None), \
             mock.patch.object(spinner.sys, "stdout") as m_stdout:
            st.start("\nAgent: ", defer_header=True)
            st.finish()
            writes = "".join(c.args[0] for c in m_stdout.write.call_args_list)
            self.assertNotIn("Agent:", writes)

    def test_undeferred_start_still_prints_upfront(self):
        st = self._status()
        st._interactive = False
        with mock.patch.object(spinner, "_pt_app", return_value=None), \
             mock.patch.object(spinner.sys, "stdout") as m_stdout:
            st.start("\nAgent: ")
            writes = "".join(c.args[0] for c in m_stdout.write.call_args_list)
            self.assertIn("Agent:", writes)


class TestLoopHeaderAndOrdering(unittest.TestCase):
    """run_agent_single: header lifecycle + block-completes-before-status."""

    def setUp(self):
        import agent
        self.agent = agent

    def _run(self, turns, emits):
        agent = self.agent
        with mock.patch.object(agent, "_llm_request") as m_llm, \
             mock.patch.object(agent, "_NUDGE_ENABLED", False), \
             mock.patch.object(agent, "_emit",
                               side_effect=lambda name, *a: emits.append((name,) + a)), \
             mock.patch.object(agent, "_MAX_TURNS", 6):
            m_llm.side_effect = [iter(t) for t in turns]
            from tools import MAP_FN
            with mock.patch.dict(MAP_FN, {"exec_command": lambda **kw: "exit=0"}):
                agent.run_agent_single(
                    [{"role": "user", "content": "hi"}],
                    {"text": "", "up_to": 0}, None,
                    mock.Mock(), 0.7, 0.9, 40, 0.0, 256, 8000)

    @staticmethod
    def _tc(name="exec_command", args='{"command": "true"}'):
        return {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "t1", "type": "function",
             "function": {"name": name, "arguments": args}}]}}]}

    def test_streamed_tail_lands_before_tool_status(self):
        """The renderer holdback tail must be flushed to on_stream_chunk
        BEFORE the '' notice that precedes the stats/status lines."""
        emits = []
        turns = [
            # text whose tail sits in the renderer's tag holdback, then tools
            [{"choices": [{"delta": {"content": "Let me look at your quantum work!"}}]},
             self._tc()],
            [{"choices": [{"delta": {"content": "done"}}]}],
        ]
        self._run(turns, emits)
        text = "".join(a[0] for n, *a in emits if n == "on_stream_chunk")
        self.assertIn("work!", text, f"tail lost: {emits[:10]}")
        chunk_idx = [i for i, e in enumerate(emits)
                     if e[0] == "on_stream_chunk" and "work!" in e[1]]
        notice_idx = [i for i, e in enumerate(emits)
                      if e[0] == "on_notice" and len(e) > 2 and e[2] == ""]
        self.assertTrue(chunk_idx and notice_idx)
        self.assertLess(chunk_idx[0], notice_idx[0],
                        "status line printed before the streamed block finished")

    def test_header_lifecycle_owed_once_per_run(self):
        """Iteration 1 text earns the header; iteration 2 must start with
        pt_header suppressed (no re-print) absent a user injection."""
        agent = self.agent
        starts = []
        real_start = spinner.StreamStatus.start

        def spy_start(self, prefix="", pt_header=True, defer_header=False):
            starts.append({"prefix": prefix, "pt_header": pt_header,
                           "defer": defer_header})
            return real_start(self, prefix, pt_header=pt_header,
                              defer_header=defer_header)

        emits = []
        turns = [
            [{"choices": [{"delta": {"content": "digging in"}}]}, self._tc()],
            [self._tc()],   # tool-only iteration — no text
            [{"choices": [{"delta": {"content": "done"}}]}],
        ]
        with mock.patch.object(spinner.StreamStatus, "start", spy_start):
            self._run(turns, emits)
        main_starts = [s for s in starts if "Agent" in spinner.theme.strip_ansi(s["prefix"])]
        self.assertGreaterEqual(len(main_starts), 3)
        self.assertTrue(main_starts[0]["defer"], "first start defers the header")
        for s in main_starts[1:]:
            self.assertFalse(s["pt_header"] and s["defer"],
                             f"later iteration re-arms a header: {s}")
            self.assertFalse(s["pt_header"], f"later iteration prints header: {s}")


if __name__ == "__main__":
    unittest.main()
