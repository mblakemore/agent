"""Regression test for cycle 0008.

Cycle 0008 fixed a friction where `agent.py` double-rendered every tool call,
tool result, and assistant message on the console: once via `ConsoleCallback`
and again via a duplicate `log.info(...)` call in the main loop. The fix was
to demote those five log sites to `log.debug(...)`, so the DEBUG file handler
still captures them in the session log file while the INFO console handler
drops them.

This test asserts — at the source-text level — that the five banned templates
are never emitted at `log.info` in `agent.py`. A source assertion is stable:
it catches a regression to `log.info` exactly where the bug lives, without
needing to spin up the full main loop.
"""

import os
import re
import unittest


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
AGENT_PY = os.path.join(REPO_ROOT, "agent.py")


# Each template must appear only as `log.debug(...)`, never as `log.info(...)`.
# The template is the string literal passed as the first positional arg.
BANNED_AT_INFO = [
    '"USER: %s"',
    '"ASSISTANT: %s"',
    '"Executing %d tool calls"',
    '"TOOL CALL: %s(%s) [id=%s]"',
    '"TOOL RESULT [%s]: %s"',
    '"Async summarizer enabled → %s"',
    '"CONTINUE: no checkpoint found, starting fresh"',
]


class TestAgentConsoleDedup(unittest.TestCase):
    def setUp(self):
        with open(AGENT_PY, "r", encoding="utf-8") as f:
            self.source = f.read()

    def test_banned_templates_are_not_info_level(self):
        for template in BANNED_AT_INFO:
            info_pattern = re.compile(r"log\.info\(\s*" + re.escape(template))
            hits = info_pattern.findall(self.source)
            self.assertEqual(
                hits, [],
                f"agent.py emits {template} at log.info — it must be log.debug "
                f"so it does not duplicate the ConsoleCallback render. "
                f"See plan/CICD/improvements/0008-console-dedup.md.",
            )

    def test_banned_templates_are_still_emitted_at_debug(self):
        # Sanity: the debug calls should still exist so the session log file
        # (DEBUG file handler) keeps the full post-mortem record.
        for template in BANNED_AT_INFO:
            debug_pattern = re.compile(r"log\.debug\(\s*" + re.escape(template))
            self.assertTrue(
                debug_pattern.search(self.source),
                f"agent.py should still emit {template} at log.debug "
                f"so the DEBUG file handler keeps it in the session log.",
            )


if __name__ == "__main__":
    unittest.main()
