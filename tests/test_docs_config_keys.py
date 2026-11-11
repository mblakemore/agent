"""docs/config-reference.md documents every config key the code reads.

The reference promises "every key the agent reads from its config file, with its default".
This test holds it to that: every key of ``agent._DEFAULT_CONFIG`` (two levels deep) and every
key read ad hoc elsewhere in the code (listed in ``EXTRA_KEYS`` below, by hand, because those
reads are ``_config.get(...)`` calls with no central default) must appear in the reference as
a backticked name. Adding a key to the defaults without a row here fails the suite, which is
the point: the drift this catches was found by hand on 2026-11-11, when the ``cycle`` table had
four rows and the code read fifteen keys from that block.

A key that appears only inside a fenced JSON block does not count: the test strips the code
blocks first, so a key must have prose or a table row.
"""
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = Path(__file__).resolve().parent.parent
REFERENCE = REPO_ROOT / "docs" / "config-reference.md"

# Keys the code reads straight from the loaded config, with no entry in _DEFAULT_CONFIG.
# Each one is a (section, key) pair, or (key, None) for a top-level key. Keep this list in
# step with the ``_config.get(...)`` reads in agent.py, tools/ and the backends.
EXTRA_KEYS = [
    ("backends", "kind"), ("backends", "base_url"), ("backends", "model"),
    ("backends", "api_key"), ("backends", "stream"), ("backends", "enabled"),
    ("backends", "effort"),
    ("advisor", "enabled"), ("advisor", "base_url"), ("advisor", "model"),
    ("advisor", "api_key"), ("advisor", "prefill_token_budget"), ("advisor", "max_tokens"),
    ("advisor", "max_calls_per_task"), ("advisor", "timeout_s"),
    ("subagent", "model"), ("subagent", "effort"),
    ("cycle", "success_check"), ("cycle", "claim_trace_max_blocks"),
    ("cycle", "grind_elapsed_s"), ("cycle", "grind_no_progress_turns"),
    ("preferences", "extra_allowed_paths"), ("preferences", "turn_hooks"),
    ("preferences", "switchable_models"), ("preferences", "seed_tasks_persistent"),
    ("think", "system_prompt"),
    ("interactive", "live_input"),
    ("bootstrap", "placeholder_files"), ("bootstrap", "placeholder_dirs"),
    ("llamacpp", "repeat_penalty"), ("llamacpp", "repeat_last_n"),
    ("command_guards", None), ("allow_catastrophic_commands", None),
    ("log_dir", None), ("log_prefix", None), ("env", None),
]

# Sections documented on their own page rather than in the reference; the reference must
# still name the section and link out.
LINKED_SECTIONS = {"aicommons", "bedrock"}


def _reference_text_without_code():
    text = REFERENCE.read_text(encoding="utf-8")
    return re.sub(r"```.*?```", "", text, flags=re.S)


def _backticked(text):
    return set(re.findall(r"`([^`\n]+)`", text))


def _heading_anchors(text):
    return {re.sub(r"[^a-z0-9_-]", "", h.strip().lower().replace(" ", "-"))
            for h in re.findall(r"^#{2,3}\s+(.+?)\s*$", text, flags=re.M)}


class TestConfigReferenceCoversDefaults(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import agent as _agent
        cls.defaults = _agent._DEFAULT_CONFIG
        cls.text = _reference_text_without_code()
        cls.names = _backticked(cls.text)
        cls.anchors = _heading_anchors(cls.text)

    def _documented(self, key):
        """A key is documented when its bare name, or a dotted path ending in it, is in
        backticks outside the code blocks."""
        if key in self.names:
            return True
        return any(n.endswith("." + key) or n.startswith(key + ".") for n in self.names)

    def test_every_default_section_has_a_heading(self):
        missing = []
        for section in self.defaults:
            if section in ("llm", "summary"):
                # Legacy flat blocks, documented under `backends`.
                self.assertIn(section, self.names, f"legacy block `{section}` not named")
                continue
            if section not in self.anchors:
                missing.append(section)
        self.assertEqual(missing, [], "sections in _DEFAULT_CONFIG without a `## section` "
                                      "heading in docs/config-reference.md: " + ", ".join(missing))

    def test_every_default_key_is_documented(self):
        missing = []
        for section, block in self.defaults.items():
            if not isinstance(block, dict):
                continue
            for key, value in block.items():
                if not self._documented(key):
                    missing.append(f"{section}.{key}")
                if isinstance(value, dict) and section != "preferences":
                    for sub in value:
                        if not self._documented(sub):
                            missing.append(f"{section}.{key}.{sub}")
        self.assertEqual(missing, [], "keys in _DEFAULT_CONFIG with no row in "
                                      "docs/config-reference.md: " + ", ".join(missing))

    def test_extra_keys_are_documented(self):
        missing = [f"{s}.{k}" if k else s for s, k in EXTRA_KEYS
                   if not self._documented(k or s)]
        self.assertEqual(missing, [], "keys read by the code with no row in "
                                      "docs/config-reference.md: " + ", ".join(missing))

    def test_linked_sections_are_named(self):
        for section in LINKED_SECTIONS:
            self.assertIn(section, self.names | self.anchors,
                          f"`{section}` must be named in the reference with a link to its page")

    def test_gate_subkeys_are_documented(self):
        # preferences.gate carries its defaults in gate.DEFAULTS, not _DEFAULT_CONFIG.
        import gate as _gate
        missing = [k for k in _gate.DEFAULTS if not self._documented(k)]
        self.assertEqual(missing, [], "gate.DEFAULTS keys without a mention in the "
                                      "`gate` row: " + ", ".join(missing))


if __name__ == "__main__":
    unittest.main()
