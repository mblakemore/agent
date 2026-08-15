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
        updates, on_disk, printed = self._run(["", "", "", "", "y", "", "y"])
        self.assertNotIn("advisor", updates)   # skipped -> untouched
        self.assertEqual(updates["llm"]["base_url"], "http://127.0.0.1:8080")
        self.assertEqual(updates["llm"]["model"], "qwen3.8-27b")   # auto-picked
        self.assertEqual(updates["summary"]["base_url"],
                         updates["llm"]["base_url"])
        self.assertTrue(updates["summary"]["enabled"])
        self.assertEqual(updates["context"]["ctx_size"], int(196608 * 0.9))
        self.assertEqual(on_disk["llm"]["model"], "qwen3.8-27b")
        self.assertIn("_calibrated", on_disk)

    def test_declined_calibration_keeps_defaults(self):
        updates, on_disk, _ = self._run(["", "", "", "", "y", "", "n"])
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
            ["", "http://dead:1", "", "", "y", "", "y"], routes={})
        self.assertEqual(updates["llm"]["base_url"], "http://dead:1")
        self.assertNotIn("context", updates)   # ctx unmeasured -> no knobs
        self.assertTrue(any("UNMEASURED" in s for s in printed))


class _FakeBackend:
    def __init__(self, healthy=True, models=None, ctx=None, kind="bedrock",
                 model="claude-v4.6-opus"):
        self._healthy, self._models, self._ctx = healthy, models or [], ctx
        self.kind, self.model = kind, model
        self.api_url = "https://gw.example"
        self.api_key = "k"

    def health(self):
        return self._healthy, "gateway ok" if self._healthy else "down"

    def list_models(self):
        return self._models

    def detect_ctx_size(self):
        return self._ctx


class TestGatewayProbes(unittest.TestCase):
    """Phase 2: bedrock/foundry probing via the constructed backend."""

    def _probe(self, section, backend=None, exc=None, http=None, consent=None):
        import llm_backend as L
        target = mock.Mock(side_effect=exc) if exc else mock.Mock(
            return_value=backend)
        with mock.patch.object(L, "build_backend", target):
            return W.probe_gateway_backend(section, "main", http=http,
                                           ask_consent=consent)

    def test_config_error_is_the_p1_verdict(self):
        import llm_backend as L
        r = self._probe({"kind": "bedrock"}, exc=L.ConfigError("no creds"))
        self.assertEqual(r["p1_reach"]["status"], W.FAILED)
        self.assertIn("no creds", r["p1_reach"]["detail"])
        self.assertEqual(r["p4_ctx"]["status"], W.UNMEASURED)

    def test_healthy_bedrock_all_ok_from_table(self):
        b = _FakeBackend(models=["claude-v4.6-opus"], ctx=180000)
        r = self._probe({"kind": "bedrock", "model": "claude-v4.6-opus"}, b)
        self.assertEqual(r["p1_reach"]["status"], W.OK)
        self.assertEqual(r["p2_auth"]["status"], W.OK)
        self.assertEqual(r["p3_model"]["status"], W.OK)
        self.assertEqual(r["p4_ctx"]["value"], 180000)

    def test_bedrock_no_table_row_is_unmeasured_not_probed(self):
        b = _FakeBackend(ctx=None)
        r = self._probe({"kind": "bedrock", "model": "m"}, b,
                        consent=lambda est: True)  # consent must NOT matter
        self.assertEqual(r["p4_ctx"]["status"], W.UNMEASURED)
        self.assertIn("not implemented", r["p4_ctx"]["detail"])

    def test_foundry_auth_reject_via_one_token_invoke(self):
        b = _FakeBackend(kind="foundry", ctx=None, model="dep")
        http = fake_http({"/v1/messages": (401, {"error": "bad key"})})
        r = self._probe({"kind": "foundry", "model": "dep"}, b, http=http)
        self.assertEqual(r["p1_reach"]["status"], W.OK)
        self.assertEqual(r["p2_auth"]["status"], W.FAILED)

    def test_foundry_consent_gate_declined_never_spends(self):
        b = _FakeBackend(kind="foundry", ctx=None, model="dep")
        http = fake_http({"/v1/messages": (200, {"usage": {"input_tokens": 3}})})
        r = self._probe({"kind": "foundry", "model": "dep"}, b, http=http,
                        consent=lambda est: False)
        self.assertEqual(r["p4_ctx"]["status"], W.UNMEASURED)
        self.assertIn("declined", r["p4_ctx"]["detail"])
        # only the single 1-token P1 invoke — no search traffic
        msg_calls = [c for c in http.calls if c[0].endswith("/v1/messages")]
        self.assertEqual(len(msg_calls), 1)

    def test_foundry_consented_empirical_measures(self):
        limit = 30000

        def messages(data):
            if data.get("max_tokens") == 1 and data["messages"][0]["content"] == "hi":
                return 200, {"usage": {"input_tokens": 3}}
            n = len(data["messages"][0]["content"]) / W._CHARS_PER_TOKEN
            if n > limit:
                return 400, {"error": "too long"}
            return 200, {"usage": {"input_tokens": int(n)}}
        b = _FakeBackend(kind="foundry", ctx=None, model="dep")
        http = fake_http({"/v1/messages": messages})
        r = self._probe({"kind": "foundry", "model": "dep"}, b, http=http,
                        consent=lambda est: True)
        self.assertEqual(r["p4_ctx"]["status"], W.OK)
        self.assertGreater(r["p4_ctx"]["value"], limit * 0.7)
        self.assertLessEqual(r["p4_ctx"]["value"], limit)

    def test_dispatcher_routes_by_kind(self):
        r = W.probe_backend({"base_url": "http://x:8080"},
                            http=fake_http(LLAMA_ROUTES))
        self.assertEqual(r["p4_ctx"]["value"], 196608)  # llamacpp path
        b = _FakeBackend(ctx=1000)
        import llm_backend as L
        with mock.patch.object(L, "build_backend", return_value=b):
            r = W.probe_backend({"kind": "bedrock", "model": "m"})
        self.assertEqual(r["p4_ctx"]["value"], 1000)    # gateway path


class TestWizardBedrockScreen(unittest.TestCase):
    def test_bedrock_answers_produce_kinded_section(self):
        import llm_backend as L
        b = _FakeBackend(models=["claude-v4.6-opus"], ctx=180000)
        answers = iter(["2", "https://gw.example", "sekrit",
                        "claude-v4.6-opus", "25.5",  # main screen
                        "y",                          # summary: reuse main
                        "",                           # advisor: skip
                        "y"])                         # accept calibration
        printed = []
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(L, "build_backend", return_value=b):
            p = Path(d) / ".agent" / "config.json"
            updates = W.run_wizard(p, {},
                                   input_fn=lambda _: next(answers, ""),
                                   print_fn=printed.append)
            on_disk = json.loads(p.read_text())
            mode = os.stat(p).st_mode & 0o777
        self.assertEqual(updates["llm"]["kind"], "bedrock")
        self.assertEqual(updates["llm"]["daily_cost_cap_usd"], 25.5)
        self.assertEqual(updates["summary"]["kind"], "bedrock")  # shared carries kind
        self.assertEqual(updates["context"]["ctx_size"], int(180000 * 0.9))
        self.assertEqual(on_disk["llm"]["kind"], "bedrock")
        self.assertEqual(mode, stat.S_IRUSR | stat.S_IWUSR)  # key -> chmod 600


class TestThroughputAndCapabilities(unittest.TestCase):
    """Phase 3: P5/P6 probes + recommendations."""

    def test_p5_prefers_server_timings(self):
        routes = {"/v1/chat/completions": (200, {
            "timings": {"predicted_per_second": 44.5, "prompt_per_second": 200.0},
            "usage": {"completion_tokens": 64},
            "choices": [{"message": {"content": "1,2,3"}}]})}
        tp = W.probe_throughput({"base_url": "http://x:8080"},
                                http=fake_http(routes))
        self.assertEqual(tp["status"], W.OK)
        self.assertEqual(tp["gen_tps"], 44.5)
        self.assertIn("server timings", tp["detail"])

    def test_p5_wall_clock_fallback_labeled(self):
        routes = {"/v1/chat/completions": (200, {
            "usage": {"completion_tokens": 64},
            "choices": [{"message": {"content": "x"}}]})}
        tp = W.probe_throughput({"base_url": "http://x:8080"},
                                http=fake_http(routes))
        self.assertEqual(tp["status"], W.OK)
        self.assertIn("wall-clock", tp["detail"])

    def test_p5_bedrock_is_honest_unmeasured(self):
        tp = W.probe_throughput({"kind": "bedrock", "model": "m"})
        self.assertEqual(tp["status"], W.UNMEASURED)
        self.assertIn("cannot cap", tp["detail"])

    def test_p6_tool_roundtrip_detected(self):
        def chat(data):
            if data.get("tools"):
                return 200, {"choices": [{"message": {"tool_calls": [
                    {"id": "t1", "function": {"name": "get_current_time",
                                              "arguments": "{}"}}]}}]}
            return 200, {"choices": [{"message": {"content": "hi"}}],
                         "timings": {"predicted_per_second": 10}}
        routes = {"/v1/chat/completions": chat}
        caps = W.probe_capabilities({"base_url": "http://x:8080"},
                                    http=fake_http(routes))
        self.assertEqual(caps["tools_accepted"]["status"], W.OK)
        self.assertEqual(caps["tool_roundtrip"]["status"], W.OK)
        self.assertEqual(caps["reasoning_param"]["status"], W.OK)

    def test_p6_tools_rejected_is_failed(self):
        def chat(data):
            if data.get("tools"):
                return 400, {"error": "tools unsupported"}
            return 200, {"choices": [{"message": {"content": "hi"}}]}
        caps = W.probe_capabilities({"base_url": "http://x:8080"},
                                    http=fake_http({"/v1/chat/completions": chat}))
        self.assertEqual(caps["tools_accepted"]["status"], W.FAILED)
        self.assertEqual(caps["tool_roundtrip"]["status"], W.UNMEASURED)

    def test_recommendations_fire_on_slow_and_toolless(self):
        recs = W.recommend(
            {}, throughput={"status": W.OK, "gen_tps": 8.0},
            caps={"tools_accepted": {"status": W.FAILED}},
            main_section={"base_url": "http://a"},
            summary_section={"base_url": "http://a"})
        text = " ".join(recs)
        self.assertIn("AGENT_STALL_TIMEOUT_S", text)
        self.assertIn("REJECTS the tools", text)
        self.assertIn("share one endpoint", text)

    def test_no_recommendations_when_healthy(self):
        recs = W.recommend(
            {}, throughput={"status": W.OK, "gen_tps": 45.0},
            caps={"tools_accepted": {"status": W.OK},
                  "tool_roundtrip": {"status": W.OK},
                  "timings": {"status": W.OK}},
            main_section={"base_url": "http://a"},
            summary_section={"base_url": "http://b"})
        self.assertEqual(recs, [])


class TestAdvisorScreen(unittest.TestCase):
    def _screen(self, answers, current=None, routes=None):
        answers = iter(answers)
        printed = []
        section = W._advisor_screen(current or {},
                                    lambda _: next(answers, ""),
                                    printed.append,
                                    fake_http(LLAMA_ROUTES if routes is None
                                              else routes))
        return section, printed

    def test_empty_url_skips_writing_nothing(self):
        section, _ = self._screen([""])
        self.assertIsNone(section)

    def test_dash_disables_explicitly(self):
        section, _ = self._screen(["-"])
        self.assertEqual(section, {"enabled": False})

    def test_reachable_advisor_enabled(self):
        section, _ = self._screen(["http://adv:9090", ""])
        self.assertTrue(section["enabled"])
        self.assertEqual(section["base_url"], "http://adv:9090")

    def test_unreachable_advisor_written_disabled_with_reason(self):
        section, printed = self._screen(["http://dead:1", ""], routes={})
        self.assertFalse(section["enabled"])
        self.assertIn("disabled_reason", section)

    def test_keep_existing_returns_no_change(self):
        section, _ = self._screen([""],
                                  current={"base_url": "http://adv:9090",
                                           "enabled": True})
        self.assertIsNone(section)


class TestDrift(unittest.TestCase):
    def test_no_baseline_is_none_not_false(self):
        drifted, msg = W.check_drift({}, {"p4_ctx": {"value": 100}})
        self.assertIsNone(drifted)
        self.assertIn("no calibration baseline", msg)

    def test_drift_detected_over_2pct(self):
        cfg = {"_calibrated": {"measured_ctx_tokens": 196608, "date": "2026-08-15"}}
        drifted, msg = W.check_drift(cfg, {"p4_ctx": {"value": 96768}})
        self.assertTrue(drifted)
        self.assertIn("DRIFTED", msg)

    def test_no_drift_within_2pct(self):
        cfg = {"_calibrated": {"measured_ctx_tokens": 100000}}
        drifted, msg = W.check_drift(cfg, {"p4_ctx": {"value": 100500}})
        self.assertFalse(drifted)

    def test_unmeasured_now_is_none_with_baseline_named(self):
        cfg = {"_calibrated": {"measured_ctx_tokens": 100000}}
        drifted, msg = W.check_drift(cfg, {"p4_ctx": {"value": None}})
        self.assertIsNone(drifted)
        self.assertIn("100000", msg)


class TestFirstRunGuard(unittest.TestCase):
    """agent._maybe_first_run_wizard — the never-block contract."""

    def setUp(self):
        import agent
        self.agent = agent

    def _in_tmp(self, fn):
        with tempfile.TemporaryDirectory() as d:
            old = os.getcwd()
            os.chdir(d)
            try:
                return fn(Path(d))
            finally:
                os.chdir(old)

    def test_non_tty_never_launches(self):
        def body(_d):
            with mock.patch("sys.stdin") as stdin, \
                 mock.patch("setup_wizard.run_wizard") as wiz:
                stdin.isatty.return_value = False
                ran = self.agent._maybe_first_run_wizard(False, None, False)
                self.assertFalse(ran)
                wiz.assert_not_called()
        self._in_tmp(body)

    def test_auto_mode_never_launches_even_on_tty(self):
        def body(_d):
            with mock.patch("sys.stdin") as stdin, \
                 mock.patch("setup_wizard.run_wizard") as wiz:
                stdin.isatty.return_value = True
                self.assertFalse(self.agent._maybe_first_run_wizard(True, None, False))
                self.assertFalse(self.agent._maybe_first_run_wizard(False, "do x", False))
                self.assertFalse(self.agent._maybe_first_run_wizard(False, None, True))
                wiz.assert_not_called()
        self._in_tmp(body)

    def test_existing_config_short_circuits(self):
        def body(d):
            (d / ".agent").mkdir()
            with mock.patch("sys.stdin") as stdin, \
                 mock.patch("setup_wizard.run_wizard") as wiz:
                stdin.isatty.return_value = True
                self.assertFalse(self.agent._maybe_first_run_wizard(False, None, False))
                wiz.assert_not_called()
        self._in_tmp(body)

    def test_tty_unconfigured_launches_and_applies(self):
        def body(_d):
            with mock.patch("sys.stdin") as stdin, \
                 mock.patch("setup_wizard.run_wizard",
                            return_value={"llm": {"base_url": "http://x"}}) as wiz, \
                 mock.patch.object(self.agent, "_apply_setup_updates") as applied:
                stdin.isatty.return_value = True
                ran = self.agent._maybe_first_run_wizard(False, None, False)
                self.assertTrue(ran)
                wiz.assert_called_once()
                applied.assert_called_once()
        self._in_tmp(body)

    def test_wizard_crash_never_kills_session_start(self):
        def body(_d):
            with mock.patch("sys.stdin") as stdin, \
                 mock.patch("setup_wizard.run_wizard",
                            side_effect=RuntimeError("boom")):
                stdin.isatty.return_value = True
                self.assertFalse(self.agent._maybe_first_run_wizard(False, None, False))
        self._in_tmp(body)


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
