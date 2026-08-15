"""Tests for the upgraded /agent scaffold wizard (agentx-standard templates,
type-before-name ordering, config-driven dynamic sections, /setup chaining)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_scaffold as A
import setup_wizard as W


def scripted(answers):
    it = iter(answers)
    prompts = []

    def input_fn(prompt):
        prompts.append(prompt)
        return next(it, "")
    input_fn.prompts = prompts
    return input_fn


class TestQuestionOrder(unittest.TestCase):
    def test_type_asked_before_name(self):
        input_fn = scripted(["1", "sparky", "", "", ""])
        with tempfile.TemporaryDirectory() as d:
            A.run_agent_wizard(cwd=d, input_fn=input_fn, print_fn=lambda s: None)
        self.assertIn("agent type", input_fn.prompts[0])
        self.assertIn("agent name", input_fn.prompts[1])

    def test_name_defaults_to_directory_name(self):
        input_fn = scripted(["1", "", "", "", ""])   # ENTER on name
        with tempfile.TemporaryDirectory() as d:
            agent_dir = Path(d) / "emberling"
            agent_dir.mkdir()
            A.run_agent_wizard(cwd=agent_dir, input_fn=input_fn,
                               print_fn=lambda s: None)
            body = (agent_dir / "AGENT.md").read_text()
        self.assertIn("I am emberling", body)
        self.assertIn("[emberling]", input_fn.prompts[1])   # shown as default


class TestScaffoldByType(unittest.TestCase):
    def _run(self, answers, subdir="a"):
        with tempfile.TemporaryDirectory() as d:
            cwd = Path(d) / subdir
            cwd.mkdir()
            written = A.run_agent_wizard(cwd=cwd, input_fn=scripted(answers),
                                         print_fn=lambda s: None)
            files = {str(p.relative_to(cwd)) for p in cwd.rglob("*") if p.is_file()}
            body = (cwd / "AGENT.md").read_text() if (cwd / "AGENT.md").exists() \
                else (cwd / "CLAUDE.md").read_text()
        return written, files, body

    def test_creature_full_tree(self):
        _, files, body = self._run(["1", "dc", "", "", ""])
        for f in ("AGENT.md", "state/current-state.json", "state/focus.json",
                  "state/memories/context.json", "state/memories/patterns.jsonl",
                  "state/memories/anchors.jsonl", "messages/from-creator.md",
                  "messages/to-creator.md", "logs/consciousness.log", ".gitignore"):
            self.assertIn(f, files, f"missing {f}")
        self.assertIn("PERCEIVE → REFLECT → DECIDE", body)
        self.assertIn("verification gate", body.lower())
        self.assertIn("git remote -v", body)          # repo guard
        self.assertIn("One cycle per invocation", body)

    def test_minimal_skips_messages_and_extras(self):
        _, files, body = self._run(["3", "tiny", "", "", "none"])
        self.assertNotIn("messages/from-creator.md", files)
        self.assertNotIn("state/memories/patterns.jsonl", files)
        self.assertIn("state/memories/context.json", files)

    def test_worker_gets_decisions_log(self):
        _, files, body = self._run(["2", "worker", "", "", ""])
        self.assertIn("state/decisions/log.jsonl", files)
        self.assertIn("4-Phase", body)

    def test_claude_md_filename_choice(self):
        _, files, _ = self._run(["1", "x", "", "CLAUDE.md", ""])
        self.assertIn("CLAUDE.md", files)
        self.assertNotIn("AGENT.md", files)

    def test_existing_files_skipped_not_clobbered(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = Path(d)
            (cwd / "AGENT.md").write_text("precious")
            A.run_agent_wizard(cwd=cwd, input_fn=scripted(["1", "x", "", "", ""]),
                               print_fn=lambda s: None)
            self.assertEqual((cwd / "AGENT.md").read_text(), "precious")


class TestDynamicTiers(unittest.TestCase):
    CFG = {"llm": {"base_url": "http://gpu:8080", "model": "qwen3.8-27b"},
           "summary": {"base_url": "http://cpu:8082", "model": "qwen3-4b",
                       "enabled": True},
           "advisor": {"base_url": "http://gpu:8080", "enabled": True},
           "context": {"ctx_size": 176947},
           "_calibrated": {"measured_ctx_tokens": 196608,
                           "ctx_source": "/props n_ctx", "date": "2026-08-15"}}

    def test_tiers_from_real_config(self):
        s = A.tiers_section(self.CFG)
        self.assertIn("qwen3.8-27b", s)
        self.assertIn("http://cpu:8082", s)
        self.assertIn("advisor", s)
        self.assertIn("196,608", s)                 # measured ctx surfaced
        self.assertIn("When to call the advisor", s)

    def test_disabled_advisor_omitted(self):
        cfg = json.loads(json.dumps(self.CFG))
        cfg["advisor"]["enabled"] = False
        s = A.tiers_section(cfg)
        self.assertNotIn("advisor", s.split("| Role |")[1].split("###")[0]
                         if "###" in s else s.split("| Role |")[1])
        self.assertNotIn("When to call the advisor", s)

    def test_no_config_points_at_setup_never_invents(self):
        s = A.tiers_section({})
        self.assertIn("/setup", s)
        self.assertNotIn("|", s.split("\n\n")[1])    # no fabricated table

    def test_scaffold_embeds_live_config(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = Path(d)
            (cwd / ".agent").mkdir()
            (cwd / ".agent" / "config.json").write_text(json.dumps(self.CFG))
            A.run_agent_wizard(cwd=cwd, input_fn=scripted(["1", "x", "", "", ""]),
                               print_fn=lambda s: None)
            body = (cwd / "AGENT.md").read_text()
        self.assertIn("qwen3.8-27b", body)
        self.assertIn("196,608", body)


class TestSetupChain(unittest.TestCase):
    ROUTES = {"/health": (200, {"status": "ok"}),
              "/v1/models": (200, {"data": [{"id": "m1"}]}),
              "/props": (200, {"default_generation_settings": {"n_ctx": 8192}})}

    def _fake_http(self):
        def http(url, data=None, headers=None, timeout=20):
            for suffix, resp in self.ROUTES.items():
                if url.endswith(suffix):
                    return resp
            return 0, "no route"
        return http

    def test_setup_offers_agent_chain_and_launches_on_yes(self):
        answers = iter(["", "", "", "",      # main defaults
                        "y",                  # summary: reuse
                        "",                   # advisor: skip
                        "y",                  # accept calibration
                        "y"])                 # configure an agent? YES
        with tempfile.TemporaryDirectory() as d, \
             mock.patch("agent_scaffold.run_agent_wizard") as agent_wiz:
            p = Path(d) / ".agent" / "config.json"
            W.run_wizard(p, {}, input_fn=lambda _: next(answers, ""),
                         print_fn=lambda s: None, http=self._fake_http())
            agent_wiz.assert_called_once()
            self.assertEqual(Path(agent_wiz.call_args.kwargs["cwd"]), Path(d))

    def test_setup_chain_defaults_to_no(self):
        answers = iter(["", "", "", "", "y", "", "y"])   # chain answer absent
        with tempfile.TemporaryDirectory() as d, \
             mock.patch("agent_scaffold.run_agent_wizard") as agent_wiz:
            p = Path(d) / ".agent" / "config.json"
            W.run_wizard(p, {}, input_fn=lambda _: next(answers, ""),
                         print_fn=lambda s: None, http=self._fake_http())
            agent_wiz.assert_not_called()

    def test_role_jump_never_offers_chain(self):
        answers = iter(["", "http://s:9090", "", "", "y"])
        with tempfile.TemporaryDirectory() as d, \
             mock.patch("agent_scaffold.run_agent_wizard") as agent_wiz:
            p = Path(d) / ".agent" / "config.json"
            W.run_wizard(p, {}, jump_to="summary",
                         input_fn=lambda _: next(answers, ""),
                         print_fn=lambda s: None, http=self._fake_http())
            agent_wiz.assert_not_called()


if __name__ == "__main__":
    unittest.main()
