"""AI Commons config, credentials and models (aicommons-integration.md P3, §7 M4/E3)."""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

import bedrock_store as bs
import cli_bedrock
import llm_backend as lb


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "creds.json"
    monkeypatch.setenv(bs.ENV_OVERRIDE, str(path))
    monkeypatch.delenv("AGENT_FAKE_BACKEND", raising=False)
    for v in ("AICOMMONS_BASE_URL", "AICOMMONS_API_KEY", "AICOMMONS_MODEL_CATALOG"):
        monkeypatch.delenv(v, raising=False)
    return path


def _write(path, entries):
    path.write_text(json.dumps({"entries": entries}))


# ── keystore ────────────────────────────────────────────────────────────


def test_old_rows_are_published_and_never_picked_for_messages(store):
    _write(store, [{"name": "bot", "url": "https://old/api", "key": "k1", "status": "up"}])
    data = bs.load_store()
    assert bs.select_entry(data)["name"] == "bot"
    assert bs.select_entry(data, bs.API_MESSAGES) is None
    with pytest.raises(LookupError, match="AICOMMONS"):
        bs.select_credentials(api=bs.API_MESSAGES)


def test_messages_rows_are_never_picked_for_the_legacy_kind(store):
    _write(store, [{"name": "ac", "url": "https://gw/v1", "key": "k2", "status": "up", "api": "messages"}])
    data = bs.load_store()
    assert bs.select_entry(data) is None
    assert bs.select_credentials(api=bs.API_MESSAGES) == ("https://gw/v1", "k2", "ac")


def test_backend_falls_back_to_the_store(store):
    _write(store, [{"name": "ac", "url": "https://gw/v1/", "key": "k2", "status": "up", "api": "messages"}])
    b = lb.AICommonsBackend({"kind": "aicommons"})
    assert (b.base_url, b.api_key, b.store_entry) == ("https://gw/v1", "k2", "ac")


def test_shared_block_supplies_credentials(store):
    b = lb.AICommonsBackend({"kind": "aicommons", "model": "claude-v5.5-opus",
                             "aicommons": {"base_url": "https://blk/v1", "api_key": "kb",
                                                                "max_context_tokens": 300_000}})
    assert b.base_url == "https://blk/v1" and b.detect_ctx_size() == 300_000


def test_messages_health_check_uses_models(store):
    resp = MagicMock(status_code=200)
    with patch("requests.get", return_value=resp) as get:
        assert bs.health_check("https://gw/v1", "k", api=bs.API_MESSAGES) == (True, None)
    assert get.call_args[0][0] == "https://gw/v1/v1/models"
    assert get.call_args[1]["headers"] == {"x-api-key": "k"}
    with patch("requests.get", return_value=MagicMock(status_code=403)):
        ok, err = bs.health_check("https://gw/v1", "k", api=bs.API_MESSAGES)
    assert not ok and "openai group" in err


def test_aicommons_cli_group_defaults_to_messages(store, capsys):
    with patch("bedrock_store.health_check", return_value=(True, None)) as hc:
        assert cli_bedrock.maybe_dispatch(["aicommons", "add", "--name", "a", "--url", "https://gw/v1",
                                           "--key", "k"]) == 0
    assert hc.call_args[1]["api"] == "messages"
    _write_bot = bs.load_store()
    _write_bot["entries"].append({"name": "bot", "url": "u", "key": "k", "status": "up"})
    bs.write_store(_write_bot)
    capsys.readouterr()
    cli_bedrock.maybe_dispatch(["aicommons", "list"])
    out = capsys.readouterr().out
    assert "a " in out and "bot" not in out and "messages" in out
    cli_bedrock.maybe_dispatch(["bedrock", "list"])
    assert "bot" in capsys.readouterr().out


# ── models ──────────────────────────────────────────────────────────────


def test_catalog_supplies_prices_and_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(lb, "AICOMMONS_MODELS", dict(lb.AICOMMONS_MODELS))
    monkeypatch.setattr(lb, "_catalog_loaded", set())
    cat = tmp_path / "model_catalog.json"
    cat.write_text(json.dumps({
        "claude-v5.5-sonnet": {"maxOutputTokens": 128000, "reasoning": True,
                               "pricing": {"input": 0.0033, "output": 0.0165, "cache_read": 0.00033,
                                           "cache_write": 0.004125}},
        "retired-model": {"active": False, "maxOutputTokens": 1},
    }))
    assert lb.load_aicommons_catalog(str(cat)) == 1
    row = lb.aicommons_model_info("claude-v5.5-sonnet")
    assert row["price_in"] == 0.0033 and row["context_tokens"] == 1_000_000
    assert "retired-model" not in lb.AICOMMONS_MODELS


# ── agent wiring ────────────────────────────────────────────────────────


def test_cfg_with_role_carries_the_shared_block(monkeypatch):
    import agent
    monkeypatch.setitem(agent._config, "aicommons", {"base_url": "https://gw/v1", "thinking": "adaptive"})
    entry = agent._cfg_with_role({"main": {"kind": "ucsb", "aicommons": {"thinking": "off"}}}, "main")
    assert entry["aicommons"] == {"base_url": "https://gw/v1", "thinking": "off"}
    plain = agent._cfg_with_role({"main": {"kind": "llamacpp"}}, "main")
    assert "aicommons" not in plain


def test_display_kind_is_not_aws():
    import agent
    assert agent._display_backend_kind("aicommons", "https://x.execute-api.us-west-2.amazonaws.com/v1") == "ai-commons"


def test_tool_recovery_goes_through_summary_complete(monkeypatch):
    import agent
    main, summary = MagicMock(kind="aicommons"), MagicMock()
    summary.complete.return_value = "42"
    monkeypatch.setattr(agent, "_main_backend", main)
    monkeypatch.setattr(agent, "_summary_backend", summary)
    call = agent._recovery_llm_call(logging.getLogger("t"))
    resp = call(json={"messages": [{"role": "user", "content": "which line?"}], "max_tokens": 64}, timeout=30)
    assert resp.json()["choices"][0]["message"]["content"] == "42"
    assert summary.complete.call_args[1]["prompt"] == "which line?"
    main.stream_chat.assert_not_called()


def test_advisor_on_a_backend_kind_uses_complete_with_max_effort(tmp_path, monkeypatch):
    """M4/E3: an advisor block naming a backend kind is served by build_backend().complete()."""
    from tools import consult_advisor as ca
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "config.json").write_text(json.dumps({
        "backends": {"main": {"kind": "aicommons", "model": "claude-v5.5-sonnet"}},
        "aicommons": {"base_url": "https://gw/v1", "api_key": "k"},
        "advisor": {"kind": "aicommons", "model": "claude-v5.5-opus"},
    }))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_FAKE_BACKEND", raising=False)
    role = ca._read_role("advisor")
    assert role["role"] == "advisor" and role["aicommons"]["api_key"] == "k"
    built = {}

    def fake_build(cfg):
        b = lb.AICommonsBackend(cfg)
        built["b"] = b
        b.complete = MagicMock(return_value="use a heap")
        return b
    with patch("llm_backend.build_backend", side_effect=fake_build):
        text, _, finish = ca._chat_full(role, [{"role": "user", "content": "Q?"}], 900, 0.2, 60)
    assert text == "use a heap" and finish == "stop"
    assert built["b"].effort == "max" and built["b"].model == "claude-v5.5-opus"
    assert built["b"].complete.call_args[1]["gen_params"] == {"max_tokens": 900}


def test_backends_only_config_advisor_self_consults_main(tmp_path, monkeypatch):
    """M4: with no llm block the advisor used to probe 127.0.0.1:8080 and get unloaded."""
    from tools import consult_advisor as ca
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "config.json").write_text(json.dumps({
        "backends": {"main": {"kind": "aicommons", "model": "claude-v5.5-opus"},
                     "summary": {"kind": "aicommons", "model": "claude-v5.5-haiku"}}}))
    monkeypatch.chdir(tmp_path)
    adv = ca._read_role("advisor")
    assert adv["kind"] == "aicommons" and "127.0.0.1" not in str(adv.get("base_url"))
    summ = ca._read_role("summary")
    assert summ["model"] == "claude-v5.5-haiku" and summ["role"] == "summary"


def test_backend_flag_drops_the_replaced_kinds_endpoint(monkeypatch):
    """Found live: --backend-main aicommons kept the llamacpp default base_url, which outranks
    AICOMMONS_BASE_URL, so every request went to http://127.0.0.1:8080."""
    import agent
    monkeypatch.delenv("AGENT_FAKE_BACKEND", raising=False)
    monkeypatch.setenv("AICOMMONS_BASE_URL", "https://gw/v1")
    monkeypatch.setenv("AICOMMONS_API_KEY", "k")
    monkeypatch.setitem(agent._config, "backends", {
        "main": {"kind": "llamacpp", "base_url": "http://127.0.0.1:8080", "model": "local-27b"},
        "summary": {"kind": "aicommons", "base_url": "https://own/v1", "api_key": "own"}})
    monkeypatch.setattr(agent, "_main_backend", None)
    monkeypatch.setattr(agent, "_summary_backend", None)
    agent._apply_backend_overrides("aicommons", "aicommons")
    assert agent._main_backend.base_url == "https://gw/v1"
    assert agent._main_backend.model == "claude-v5.5-sonnet"    # the CLI default
    assert agent._summary_backend.base_url == "https://own/v1"     # already aicommons: kept


def test_advisor_follows_the_command_line_backend(tmp_path, monkeypatch):
    """Found live: with --backend-main aicommons the advisor still self-consulted the llamacpp
    endpoint named in the config file."""
    import agent
    from types import SimpleNamespace
    from tools import consult_advisor as ca
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "config.json").write_text(json.dumps({
        "llm": {"base_url": "http://127.0.0.1:8080", "model": "local-27b"},
        "backends": {"main": {"kind": "llamacpp", "base_url": "http://127.0.0.1:8080"}}}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_BACKEND_OVERRIDES", raising=False)
    monkeypatch.setitem(agent._config, "backends", {"main": {"kind": "aicommons", "model": "claude-v5.5-opus"}})
    agent._export_backend_overrides(SimpleNamespace(backend_main="aicommons", backend_summary=None,
                                                    model=None, effort="xhigh"))
    adv = ca._read_role("advisor")
    assert adv["kind"] == "aicommons" and adv["model"] == "claude-v5.5-opus" and adv["effort"] == "xhigh"
    assert "127.0.0.1" not in str(adv.get("base_url"))


def test_llm_block_with_a_gateway_kind_drops_the_llamacpp_defaults(tmp_path, monkeypatch):
    """/setup writes {"llm": {"kind": "aicommons", ...}}. Merged over the shipped
    defaults it inherited base_url http://127.0.0.1:8080 (summary :8082), which
    outranks every AI Commons credential source: main went to the local server."""
    import agent
    from tools import consult_advisor as ca
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "config.json").write_text(json.dumps({
        "llm": {"kind": "aicommons", "model": "claude-v5.5-opus"},
        "summary": {"enabled": True, "kind": "aicommons", "model": "claude-v5.5-haiku"}}))
    monkeypatch.chdir(tmp_path)
    cfg = agent._load_config()
    for role in ("main", "summary"):
        assert "base_url" not in cfg["backends"][role], role
    assert cfg["llm"]["base_url"]       # legacy block keeps it: BASE_URL reads it at import
    assert cfg["backends"]["main"]["model"] == "claude-v5.5-opus"
    adv = ca._read_role("advisor")
    assert adv["kind"] == "aicommons" and not adv.get("base_url")
    summ = ca._read_role("summary")
    assert summ["kind"] == "aicommons" and summ["model"] == "claude-v5.5-haiku"


def test_llamacpp_block_keeps_the_default_endpoint(tmp_path, monkeypatch):
    import agent
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "config.json").write_text(json.dumps({"llm": {"model": "m"}}))
    monkeypatch.chdir(tmp_path)
    assert agent._load_config()["llm"]["base_url"] == "http://127.0.0.1:8080"


def test_live_setup_apply_switching_kind_drops_the_old_endpoint(monkeypatch):
    import agent
    live = json.loads(json.dumps(agent._DEFAULT_CONFIG))
    live["llm"]["api_key"] = "sk-old"
    monkeypatch.setattr(agent, "_config", live)
    # _apply_setup_updates reassigns these globals; patch them so they're restored
    for name in ("_main_backend", "_summary_backend", "BASE_URL"):
        monkeypatch.setattr(agent, name, getattr(agent, name))
    monkeypatch.setattr(agent, "_build_backend", lambda cfg: MagicMock(model=cfg.get("model")))
    agent._apply_setup_updates({"llm": {"kind": "aicommons", "model": "claude-v5.5-opus"},
                                "summary": {"enabled": True, "kind": "aicommons",
                                            "model": "claude-v5.5-haiku"}})
    for role in ("main", "summary"):
        entry = agent._config["backends"][role]
        assert entry["kind"] == "aicommons" and "base_url" not in entry and "api_key" not in entry


def test_rank_puts_known_models_first_newest_version_first():
    ids = ["amazon-nova-pro", "claude-v4.5-haiku", "claude-v5.5-opus", "qwen3-32b",
           "claude-v5-sonnet"]
    ranked = lb.rank_aicommons_models(ids)
    assert ranked[:3] == ["claude-v5.5-opus", "claude-v5-sonnet", "claude-v4.5-haiku"]
    assert ranked[3:] == ["amazon-nova-pro", "qwen3-32b"]      # unknown: gateway order


def test_model_choices_drop_aliases(store):
    b = lb.AICommonsBackend({"kind": "aicommons", "base_url": "https://g/v1", "api_key": "k"})
    rows = [MagicMock(id="claude-v5.5-haiku", owned_by="bedrock"),
            MagicMock(id="gpt-4o", owned_by="openai-alias"),
            MagicMock(id="qwen3-32b", owned_by="bedrock")]
    b.client = MagicMock()
    b.client.with_options.return_value.models.list.return_value = MagicMock(data=rows)
    assert b.model_choices() == ["claude-v5.5-haiku", "qwen3-32b"]


def test_no_model_configured_is_unhealthy_with_a_way_out(store):
    b = lb.AICommonsBackend({"kind": "aicommons", "base_url": "https://g/v1", "api_key": "k"})
    assert b.model == ""
    with patch.object(b, "_model_ids", return_value=["claude-v5.5-haiku"]), \
         patch.object(b, "quota", return_value=None):
        ok, detail = b.health()
    assert not ok and "/setup" in detail


def test_every_backend_accepts_timeout_on_list_and_ctx():
    """Found live: /model crashed with AICommonsBackend.list_models() got an unexpected
    keyword argument 'timeout' — agent.py passes timeout= to whichever backend is active."""
    import inspect
    classes = [c for c in vars(lb).values() if inspect.isclass(c) and c.__module__ == lb.__name__
               and hasattr(c, "list_models") and hasattr(c, "detect_ctx_size")
               and c.__name__ != "Backend"]
    assert {"LlamacppBackend", "AICommonsBackend"} <= {c.__name__ for c in classes}
    for cls in classes:
        for meth in ("list_models", "detect_ctx_size"):
            params = inspect.signature(getattr(cls, meth)).parameters
            assert "timeout" in params, f"{cls.__name__}.{meth} has no timeout parameter"


def test_model_picker_lists_the_gateway_through_the_live_backend(monkeypatch, store):
    import agent
    b = lb.AICommonsBackend({"kind": "aicommons", "base_url": "https://g/v1", "api_key": "k",
                             "model": "claude-v5.5-haiku"})
    monkeypatch.setattr(b, "model_choices", lambda timeout=10.0: ["claude-v5.5-sonnet", "qwen3-32b"])
    monkeypatch.setattr(agent, "_main_backend", b)
    assert agent._list_available_models("https://g/v1") == ["claude-v5.5-sonnet", "qwen3-32b"]


def test_switching_model_rederives_its_limits(monkeypatch, store):
    import agent
    b = lb.AICommonsBackend({"kind": "aicommons", "base_url": "https://g/v1", "api_key": "k",
                             "model": "claude-v5.5-opus"})
    assert b._thinking_kwargs()                       # opus: thinking on
    live = json.loads(json.dumps(agent._DEFAULT_CONFIG))
    live["backends"] = {"main": {"kind": "aicommons", "model": "claude-v5.5-opus"}}
    monkeypatch.setattr(agent, "_config", live)
    monkeypatch.setattr(agent, "_main_backend", b)
    monkeypatch.setattr(agent, "_persist_config_value", lambda *a: None)
    agent._set_model_for_role("main", "qwen3-32b")
    assert b.model == "qwen3-32b" and not b._thinking_kwargs()   # unknown: no thinking sent
    assert b.max_tokens <= lb._AICOMMONS_UNKNOWN_MODEL["max_output"]
    assert live["backends"]["main"]["model"] == "qwen3-32b"
