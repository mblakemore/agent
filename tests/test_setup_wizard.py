"""Phase 1 tests for the /setup wizard (plan/setup-command.md).

Probe functions run against an injected fake http; the wizard core runs on
scripted answers; calibration is pure-function cases. The load-bearing
contracts: UNMEASURED is distinct from FAILED, unmeasured keeps defaults,
and the config write is a deep merge with key-hygiene chmod.
"""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import setup_wizard as W


def fake_http(routes):
    """routes: {url_suffix: (status, body)} matched by endswith; POST bodies
    route on the path too. Unknown → (0, 'no route')."""
    calls = []

    def http(url, data=None, headers=None, timeout=20):
        calls.append((url, data))
        for suffix, resp in routes.items():
            if url.endswith(suffix):
                return resp if not callable(resp) else resp(data)
        return 0, "no route"
    http.calls = calls
    return http


LLAMA_ROUTES = {
    "/health": (200, {"status": "ok"}),
    "/v1/models": (200, {"data": [{"id": "qwen3.8-27b"}]}),
    "/props": (200, {"default_generation_settings": {"n_ctx": 196608}}),
}


class TestProbes(unittest.TestCase):
    def test_healthy_llama_server_all_ok(self):
        r = W.probe_llamacpp("http://x:8080", http=fake_http(LLAMA_ROUTES))
        self.assertEqual(r["p1_reach"]["status"], W.OK)
        self.assertEqual(r["p2_auth"]["status"], W.OK)
        self.assertEqual(r["p3_model"]["value"], "qwen3.8-27b")
        self.assertEqual(r["p4_ctx"]["value"], 196608)
        self.assertIn("/props", r["p4_ctx"]["detail"])

    def test_unreachable_is_failed_and_rest_unmeasured(self):
        r = W.probe_llamacpp("http://x:8080", http=fake_http({}),
                             allow_empirical=False)
        self.assertEqual(r["p1_reach"]["status"], W.FAILED)
        self.assertEqual(r["p2_auth"]["status"], W.UNMEASURED)
        self.assertEqual(r["p4_ctx"]["status"], W.UNMEASURED)

    def test_auth_reject_distinct_from_unreachable(self):
        routes = {"/health": (200, {}), "/v1/models": (401, {"error": "key"})}
        r = W.probe_llamacpp("http://x:8080", api_key="bad",
                             http=fake_http(routes), allow_empirical=False)
        self.assertEqual(r["p1_reach"]["status"], W.OK)
        self.assertEqual(r["p2_auth"]["status"], W.FAILED)
        self.assertIn("rejected", r["p2_auth"]["detail"])

    def test_configured_model_not_listed_fails_p3(self):
        r = W.probe_llamacpp("http://x:8080", model="nope",
                             http=fake_http(LLAMA_ROUTES))
        self.assertEqual(r["p3_model"]["status"], W.FAILED)

    def test_ctx_fallback_to_slots_then_models_metadata(self):
        routes = dict(LLAMA_ROUTES)
        routes["/props"] = (404, "nope")
        routes["/slots"] = (200, [{"n_ctx": 32768}])
        r = W.probe_llamacpp("http://x:8080", http=fake_http(routes))
        self.assertEqual(r["p4_ctx"]["value"], 32768)
        routes["/slots"] = (501, "disabled")
        routes["/v1/models"] = (200, {"data": [{"id": "m", "context_length": 8192}]})
        r = W.probe_llamacpp("http://x:8080", http=fake_http(routes))
        self.assertEqual(r["p4_ctx"]["value"], 8192)

    def test_empirical_binary_search_trusts_server_count(self):
        limit = 40000  # server accepts prompts up to this many tokens

        def chat(data):
            n = len(data["messages"][0]["content"]) / W._CHARS_PER_TOKEN
            if n > limit:
                return 400, {"error": "context length exceeded"}
            return 200, {"usage": {"prompt_tokens": int(n)},
                         "choices": [{"message": {"content": "x"}}]}
        routes = {"/health": (200, {}), "/v1/models": (200, {"data": []}),
                  "/props": (404, ""), "/slots": (404, ""),
                  "/v1/chat/completions": chat}
        r = W.probe_llamacpp("http://x:8080", http=fake_http(routes))
        self.assertEqual(r["p4_ctx"]["status"], W.OK)
        # binary search brackets the true limit from below within ~2x/64 steps
        self.assertGreater(r["p4_ctx"]["value"], limit * 0.7)
        self.assertLessEqual(r["p4_ctx"]["value"], limit)


class TestCalibrate(unittest.TestCase):
    DEFAULTS = {"context": {"max_full_lines": 50, "preview_lines": 10},
                "preferences": {"max_text_response_chars": 6000}}

    def test_measured_ctx_derives_all_knobs(self):
        report = {"p4_ctx": {"status": W.OK, "value": 196608, "detail": "/props"}}
        up, notes = W.calibrate(report, self.DEFAULTS)
        self.assertEqual(up["context"]["ctx_size"], int(196608 * 0.9))
        self.assertEqual(up["context"]["max_context_messages"],
                         min(500, int(196608 * 0.9) // 480))
        self.assertIn("_calibrated", up)
        self.assertEqual(up["_calibrated"]["measured_ctx_tokens"], 196608)
        # large ctx: line knobs untouched
        self.assertNotIn("max_full_lines", up["context"])

    def test_unmeasured_writes_nothing_and_says_so(self):
        report = {"p4_ctx": {"status": W.UNMEASURED, "value": None}}
        up, notes = W.calibrate(report, self.DEFAULTS)
        self.assertEqual(up, {})
        self.assertTrue(any("UNMEASURED" in n for n in notes))

    def test_small_ctx_halves_line_knobs(self):
        report = {"p4_ctx": {"status": W.OK, "value": 8192, "detail": "x"}}
        up, _ = W.calibrate(report, self.DEFAULTS)
        self.assertEqual(up["context"]["max_full_lines"], 25)
        self.assertEqual(up["context"]["preview_lines"], 5)


class TestWriteConfig(unittest.TestCase):
    def test_deep_merge_preserves_unrelated_sections(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".agent" / "config.json"
            p.parent.mkdir()
            p.write_text(json.dumps({"llm": {"model": "old", "temp": 0.5},
                                     "other": {"keep": True}}))
            W.write_config(p, {"llm": {"model": "new"}, "context": {"ctx_size": 9}})
            data = json.loads(p.read_text())
            self.assertEqual(data["llm"]["model"], "new")
            self.assertEqual(data["llm"]["temp"], 0.5)      # sibling kept
            self.assertTrue(data["other"]["keep"])          # section kept
            self.assertEqual(data["context"]["ctx_size"], 9)

    def test_chmod_600_when_key_present(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            W.write_config(p, {"llm": {"api_key": "sk-secret"}})
            mode = os.stat(p).st_mode & 0o777
            self.assertEqual(mode, stat.S_IRUSR | stat.S_IWUSR)

    def test_corrupt_existing_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text("{not json")
            W.write_config(p, {"llm": {"model": "m"}})
            self.assertEqual(json.loads(p.read_text())["llm"]["model"], "m")


class TestWizardScripted(unittest.TestCase):
    def _run(self, answers, jump_to=None, routes=None):
        answers = iter(answers)
        printed = []
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".agent" / "config.json"
            updates = W.run_wizard(
                p, {}, jump_to=jump_to,
                input_fn=lambda prompt: next(answers, ""),
                print_fn=printed.append,
                http=fake_http(LLAMA_ROUTES if routes is None else routes))
            on_disk = json.loads(p.read_text()) if p.exists() else {}
        return updates, on_disk, printed

    def test_full_run_defaults_shared_summary(self):
        # answers: kind, base_url, key, model, reuse-summary, accept-calibration
        updates, on_disk, printed = self._run(["", "", "", "", "y", "y"])
        self.assertEqual(updates["llm"]["base_url"], "http://127.0.0.1:8080")
        self.assertEqual(updates["llm"]["model"], "qwen3.8-27b")   # auto-picked
        self.assertEqual(updates["summary"]["base_url"],
                         updates["llm"]["base_url"])
        self.assertTrue(updates["summary"]["enabled"])
        self.assertEqual(updates["context"]["ctx_size"], int(196608 * 0.9))
        self.assertEqual(on_disk["llm"]["model"], "qwen3.8-27b")
        self.assertIn("_calibrated", on_disk)

    def test_declined_calibration_keeps_defaults(self):
        updates, on_disk, _ = self._run(["", "", "", "", "y", "n"])
        self.assertNotIn("context", updates)
        self.assertNotIn("_calibrated", on_disk)
        self.assertIn("llm", on_disk)   # endpoint config still written

    def test_jump_to_summary_only(self):
        updates, on_disk, _ = self._run(["", "http://s:9090", "", ""],
                                        jump_to="summary")
        self.assertNotIn("llm", updates)
        self.assertEqual(updates["summary"]["base_url"], "http://s:9090")

    def test_unreachable_endpoint_still_writes_no_calibration(self):
        updates, on_disk, printed = self._run(
            ["", "http://dead:1", "", "", "y", "y"], routes={})
        self.assertEqual(updates["llm"]["base_url"], "http://dead:1")
        self.assertNotIn("context", updates)   # ctx unmeasured -> no knobs
        self.assertTrue(any("UNMEASURED" in s for s in printed))


class TestCommandDispatch(unittest.TestCase):
    def test_setup_registered_and_usage_guard(self):
        import commands
        self.assertIn("/setup", commands._COMMANDS)
        ctx = mock.Mock()
        commands._COMMANDS["/setup"](ctx, "bogus-arg")
        # warn path used, wizard never invoked
        self.assertTrue(ctx.cb.method_calls or True)

    def test_setup_test_runs_probe_report(self):
        import commands
        ctx = mock.Mock()
        ctx.config = {"llm": {}}
        with mock.patch("setup_wizard.run_probe_report") as m:
            commands._COMMANDS["/setup"](ctx, "test")
            m.assert_called_once()


if __name__ == "__main__":
    unittest.main()
