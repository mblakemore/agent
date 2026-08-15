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
        input_fn = scripted(["1", "sparky", "1", "", "", "", "n"])
        with tempfile.TemporaryDirectory() as d:
            A.run_agent_wizard(cwd=d, input_fn=input_fn, print_fn=lambda s: None)
        self.assertIn("agent type", input_fn.prompts[0])
        self.assertIn("agent name", input_fn.prompts[1])

    def test_name_defaults_to_directory_name(self):
        input_fn = scripted(["1", "", "1", "", "", "", "n"])   # ENTER on name
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
        _, files, body = self._run(["1", "dc", "1", "", "", "", "n"])
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
        _, files, body = self._run(["3", "tiny", "1", "", "", "none", "n"])
        self.assertNotIn("messages/from-creator.md", files)
        self.assertNotIn("state/memories/patterns.jsonl", files)
        self.assertIn("state/memories/context.json", files)

    def test_worker_gets_decisions_log(self):
        _, files, body = self._run(["2", "worker", "1", "", "", "", "n"])
        self.assertIn("state/decisions/log.jsonl", files)
        self.assertIn("4-Phase", body)

    def test_claude_md_filename_choice(self):
        _, files, _ = self._run(["1", "x", "1", "", "CLAUDE.md", "", "n"])
        self.assertIn("CLAUDE.md", files)
        self.assertNotIn("AGENT.md", files)

    def test_existing_files_skipped_not_clobbered(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = Path(d)
            (cwd / "AGENT.md").write_text("precious")
            A.run_agent_wizard(cwd=cwd, input_fn=scripted(["1", "x", "1", "", "", "", "n"]),
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
            A.run_agent_wizard(cwd=cwd, input_fn=scripted(["1", "x", "1", "", "", "", "n"]),
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
        answers = iter(["n",                  # scan: skip
                        "", "", "", "",       # main defaults
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
        answers = iter(["n", "", "", "", "", "y", "", "y"])   # chain answer absent
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


class TestIdentityFileLoading(unittest.TestCase):
    """The engine must full-load BOTH identity filenames — a >50-line
    CLAUDE.md was preview-truncated to 10 lines by @-reference (caught in
    the 2026-08-15 scaffold review; agentx's own 192-line CLAUDE.md loaded
    as a stub)."""

    def _expand_in(self, d, fname):
        import agent
        body = "\n".join(f"line {i}: instruction detail" for i in range(120))
        (Path(d) / fname).write_text(f"# {fname}\n{body}\n")
        old = os.getcwd()
        os.chdir(d)
        try:
            expanded, _files, err = agent._expand_file_refs(f"@{fname} run the loop")
        finally:
            os.chdir(old)
        return expanded, err

    def test_claude_md_loads_whole_with_identity_header(self):
        with tempfile.TemporaryDirectory() as d:
            expanded, err = self._expand_in(d, "CLAUDE.md")
        self.assertIsNone(err)
        self.assertIn("AGENT IDENTITY FILE", expanded)
        self.assertIn("line 119", expanded, "CLAUDE.md was preview-truncated")
        self.assertIn("SYSTEM CONTEXT", expanded)   # cwd preamble fires too

    def test_agent_md_unchanged_contract(self):
        with tempfile.TemporaryDirectory() as d:
            expanded, err = self._expand_in(d, "AGENT.md")
        self.assertIsNone(err)
        self.assertIn("AGENT IDENTITY FILE", expanded)
        self.assertIn("line 119", expanded)


class TestRepoProvisioning(unittest.TestCase):
    """The cycles require a repo: provision before writing, finalize with an
    explicit-paths init commit, and honor 'I'll clone it myself' by writing
    NOTHING."""

    def test_wait_option_aborts_writing_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            written = A.run_agent_wizard(
                cwd=d, input_fn=scripted(["1", "x", "3"]),
                print_fn=lambda s: None)
            leftover = [p for p in Path(d).rglob("*")]
        self.assertEqual(written, [])
        self.assertEqual(leftover, [])

    def test_local_init_plus_init_commit(self):
        with tempfile.TemporaryDirectory() as d:
            A.run_agent_wizard(cwd=d,
                               input_fn=scripted(["1", "dc", "1", "", "", "", "y"]),
                               print_fn=lambda s: None)
            rc, out = A._git(["git", "log", "--oneline"], d)
            self.assertEqual(rc, 0)
            self.assertIn("C0: dc scaffolded", out)
            rc, tracked = A._git(["git", "ls-files"], d)
            self.assertIn("AGENT.md", tracked)
            self.assertIn(".gitignore", tracked)
            self.assertNotIn(".agent/", tracked)     # runtime state never tracked
            rc, status = A._git(["git", "status", "--porcelain"], d)
            self.assertEqual(status, "", f"dirty after init commit: {status}")

    def test_existing_repo_skips_provision_question(self):
        with tempfile.TemporaryDirectory() as d:
            A._git(["git", "init", "-q"], d)
            input_fn = scripted(["1", "dc", "", "", "", "n"])
            A.run_agent_wizard(cwd=d, input_fn=input_fn, print_fn=lambda s: None)
            self.assertFalse(any("NOT a git repo" in p for p in input_fn.prompts))
            self.assertTrue((Path(d) / "AGENT.md").exists())

    def test_github_option_defers_create_until_after_commit(self):
        calls = []
        real_git = A._git

        def spy(args, cwd, timeout=60):
            calls.append(args)
            if args[0] == "gh" and args[1] == "auth":
                return 0, "authenticated"
            if args[0] == "gh" and args[1] == "repo":
                return 0, "created"
            return real_git(args, cwd, timeout)
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(A, "_git", side_effect=spy):
            A.run_agent_wizard(
                cwd=d,
                input_fn=scripted(["1", "hubbot", "2", "private", "",
                                   "", "", "", "y"]),
                print_fn=lambda s: None)
        gh_create = [c for c in calls if c[:3] == ["gh", "repo", "create"]]
        self.assertEqual(len(gh_create), 1)
        self.assertIn("--private", gh_create[0])
        self.assertIn("--push", gh_create[0])
        commit_idx = next(i for i, c in enumerate(calls) if "commit" in c)
        create_idx = next(i for i, c in enumerate(calls) if c[:3] == ["gh", "repo", "create"])
        self.assertLess(commit_idx, create_idx, "gh create must ride ON the commit")

    def test_gh_unavailable_falls_back_to_local(self):
        real_git = A._git

        def spy(args, cwd, timeout=60):
            if args[0] == "gh":
                return 1, "gh: command not found"
            return real_git(args, cwd, timeout)
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(A, "_git", side_effect=spy):
            written = A.run_agent_wizard(
                cwd=d, input_fn=scripted(["1", "x", "2", "y", "", "", "", "y"]),
                print_fn=lambda s: None)
            self.assertTrue(written)
            rc, out = real_git(["git", "log", "--oneline"], d)
        self.assertEqual(rc, 0)


class TestTrackedConfigWarning(unittest.TestCase):
    def test_tracked_config_warns_loudly(self):
        import agent, io
        with tempfile.TemporaryDirectory() as d:
            A._git(["git", "init", "-q"], d)
            (Path(d) / "config.json").write_text('{"llm": {"api_key": "x"}}')
            A._git(["git", "add", "config.json"], d)
            A._git(["git", "commit", "-qm", "oops"], d)
            old = os.getcwd()
            os.chdir(d)
            try:
                with mock.patch("sys.stderr", new=io.StringIO()) as err:
                    agent._warn_if_config_tracked()
            finally:
                os.chdir(old)
        self.assertIn("TRACKED by git", err.getvalue())
        self.assertIn("git rm --cached config.json", err.getvalue())

    def test_untracked_config_stays_silent(self):
        import agent, io
        with tempfile.TemporaryDirectory() as d:
            A._git(["git", "init", "-q"], d)
            (Path(d) / "config.json").write_text("{}")
            old = os.getcwd()
            os.chdir(d)
            try:
                with mock.patch("sys.stderr", new=io.StringIO()) as err:
                    agent._warn_if_config_tracked()
            finally:
                os.chdir(old)
            self.assertEqual(err.getvalue(), "")
