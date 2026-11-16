"""The agent's configuration: the defaults, the load of ``.agent/config.json`` (or the
legacy ``./config.json``) deep-merged over them, the ``backends`` registry synthesized
from the legacy ``llm`` / ``summary`` blocks, what the command line writes onto the
loaded dict, and the role setters ``/model`` and ``/effort`` use, which also persist
their change to the file.

The loaded dict and the live backend objects belong to agent.py (tests patch them
there); this module reads them through ``DEPS`` at call time and never rebinds them.
Rebuilding a backend after a config change stays in agent.py for the same reason.
"""
import json
import logging
import os
import sys
from pathlib import Path

DEPS = {
    "config": lambda: {},                  # the loaded config dict (agent._config)
    "main_backend": lambda: None,          # the live backends (agent._main_backend, ...)
    "summary_backend": lambda: None,
    "emit": lambda *a, **k: None,          # the UI callback (agent._emit)
}


def configure(**deps):
    """agent.py registers the getters once; each is read again at every call."""
    unknown = set(deps) - set(DEPS)
    if unknown:
        raise TypeError("unknown deps: %s" % ", ".join(sorted(unknown)))
    DEPS.update(deps)


def _cfg():
    return DEPS["config"]()


def _main():
    return DEPS["main_backend"]()


def _summary():
    return DEPS["summary_backend"]()


# ── The defaults ──────────────────────────────────────────────────────


DEFAULTS = {
    # Lazy tools (tools/__init__.py): which tools the model sees in every request
    # (core), which it loads with tool_search (deferred, named in one catalog line) and
    # which only a search finds (hidden). Empty lists keep the shipped tiers.
    "tools": {
        "core": [],
        "deferred": [],
        "hidden": [],
        "catalog": True,             # the one-line list of deferred tools in the system prompt
        "preload_from_prompt": True, # load tools the task text names before the first request
        "implicit_run": True,        # a deferred tool called with fitting arguments runs at once
    },
    # Skills (skills.py): instruction files the model loads by name with
    # `tool_search skill:NAME`, named in one catalog line beside the tools; optionally
    # picked by the summary model from the task text. Every key is off or harmless by
    # default; discovery of .agent/skills and .claude/skills is always on.
    "skills": {
        "dirs": [],                  # extra directories of <name>/SKILL.md
        "catalog": [],               # registered documents: {name, path, description, keywords?}
        "max_chars": 20000,          # a bigger file is not a skill (the spill cap)
        "max_loaded_tokens": 12000,  # loads past this are refused with a notice
        "pick": "off",               # "start": one summary-model pick before the first request;
                                     # "always": also after each later user message
        "pick_on_events": False,     # "always" also judges delivered bus items
        "load_threshold": 0.75,      # a pick at or above this is loaded
        "suggest_threshold": 0.4,    # ... at or above this is suggested in the runtime note
        "max_picks_per_turn": 2,
        "pick_timeout_s": 15,
    },
    # No model name ships: an empty model takes the server's own (startup reads
    # /v1/models), and /setup writes the one picked.
    "llm": {"base_url": "http://127.0.0.1:8080", "model": ""},
    "retry": {
        "max_retries": 10,
        "base_delay_seconds": 2,
        "max_delay_seconds": 60,
        "backoff_multiplier": 2.0,
        "jitter_factor": 0.1,
    },
    "context": {
        "max_full_lines": 800,
        "preview_lines": 200,
        "summary_threshold": 5,
        "summary_max_chars": 3000,
        "max_context_messages": 30,
        "ctx_size": 114688,   # _DEFAULT_CTX_SIZE
        "max_tokens": 16384,
        # Prompt-caching backends only (aicommons). When the next request follows an
        # idle gap longer than the cache's recorded lifetime, the window is rebuilt to
        # this fraction of the budget behind a fresh summary instead of re-sending the
        # whole dead prefix (0 disables). The keepalive re-sends the last request with
        # a one-token output limit at a fraction of the recorded lifetime while the
        # agent waits for input, for at most max_idle_s; every probe_every-th keepalive
        # waits a little longer to learn the real lifetime (0 never probes).
        "cache_collapse_frac": 0.15,
        "cache_keepalive": True,
        # Prompt text that steers small models (the identity-file sentence, the
        # working-directory preamble, the /home/user clause). Off for models that
        # need none of it: every request is a little shorter.
        "model_guards": True,
        "cache_keepalive_max_idle_s": 3600,
        "cache_keepalive_probe_every": 4,
    },
    "cycle": {
        "max_turns": 250,
        "wind_down_turns": 10,
        "max_text_only": 3,
        "max_total_nudges": 6,
        # F1 (plan/headless-hardening.md): wall-clock deadline in seconds, 0 = off (today's
        # behavior exactly). --deadline wins over this. Escalating injections at these
        # fractions reuse the wind-down channel; at 1.0 tool dispatch is refused and the
        # run takes the final-result path with exit code 10.
        "deadline_s": 0,
        "deadline_warn_fracs": [0.6, 0.8, 0.92],
        # F9: capability-refusal stop (headless only). When a tool result says the run was never
        # GRANTED something it needs — a missing key, blocked egress, a permission — the run is
        # told once, in that result, that blocked-with-reasoning is the required exit and that
        # building a substitute is prohibited. If it is still issuing tool calls this many turns
        # later, tool calls are refused and the final result is forced, exactly as the deadline
        # does. Measured before this existed: a refusal at turn 2, two hand-written fetchers,
        # 43 more turns, no result. 0 = off.
        "refusal_max_turns": 5,
        # F2: result contract. false = off (raw --result-file text, today's behavior);
        # true = built-in schema; a path = JSON schema file. When armed the final message must
        # end with a fenced JSON block carrying "contract": 1 that validates; missing/invalid
        # exits are redirected up to result_contract_max_blocks times, then a synthesized
        # {"status": "failed"} block is written so the caller ALWAYS gets valid JSON (exit 11).
        "result_contract": False,
        "result_contract_max_blocks": 2,
        # F4: goal anchoring + deliverable guard. Empty = off. Auto-derived from a GOAL: /
        # DELIVERABLE: stanza in the initial prompt when the result contract is armed.
        # Anchors are injected at these fractions of the deadline (turn fraction when no
        # deadline); a final `done` while a named deliverable is absent draws a correction.
        "goal": "",
        "deliverables": [],
        "goal_anchor_fracs": [0.5, 0.8],
        # F7: the same read-class call repeated n times within `window` turns is a stall
        # signature sharper than "no edits lately"; one nudge per unique call, not a nag.
        "repeat_read_nudge": {"n": 3, "window": 6},
    },
    # F3: per-result spill cap. A tool result (or an inbound prompt/message, F3a) over this many
    # chars is written to .agent/spill/ and replaced by a reference + excerpt; 0 = legacy
    # head/tail truncation only. Sits at the boundary the old truncation used, now lossless.
    "limits": {
        "tool_result_max_chars": 20_000,
    },
    "generation": {
        "temperature": 0.6,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        # F8a: sampling seed for reproducible runs (None = server default). Overridable per run
        # with --seed; honored by llama.cpp, warned-once-and-ignored by backends without one.
        "seed": None,
        # Whether the MAIN loop generation may emit thinking. Default False: the
        # design is no-thinking-in-main + the opt-in `think` tool, so the model
        # chooses when to reason hard. Set True to let the main model think on
        # every turn (bounded by max_tokens); the `think` tool stays available
        # either way. Consumed at the main request_body (chat_template_kwargs).
        "enable_thinking": False,
    },
    # `think` tool depth->max_tokens budgets. Preset names are fixed
    # (brief/normal/deep); values are tunable per deployment (a fast small model
    # may want smaller ceilings, a slow reasoning model larger). Consumed by
    # tools/think.py::_depth_budgets(); a bad/non-positive value falls back to
    # the tool's built-in default for that preset.
    "think": {
        "depths": {"brief": 2048, "normal": 4096, "deep": 16384},
    },
    "summary": {
        "base_url": "http://127.0.0.1:8082",
        "model": "",
        "enabled": True,
        "max_wait_on_save": 10,
        # At a window head move (eviction, cache collapse, overflow retry) with no
        # summary ready: null summarizes now, in front of the request (blocking); a
        # number waits at most that many seconds for a running summary, then sends
        # the request with the last summary and lets the worker catch up.
        "wait_s": None,
        # Prompt-caching backends: once the window stands at this fraction of its
        # budget (or of the message cap), summarize what the next eviction is certain
        # to drop, so the eviction finds its summary ready. 0 = off.
        "ahead": 0.0,
    },
    "bedrock": {
        "adaptive_max_tokens": True,
    },
    "preferences": {
        # Your own instructions, placed first in the system message of every
        # request (before the agent's own lines). Empty = none. Set it with
        # /system or by hand.
        "system_prompt": "",
        # T5.14 Option B — when true, the framework prepends a system-prompt
        # directive recommending file(action='edit') over heredoc rewrites
        # for existing-file modifications. Off by default so existing agents
        # with rich AGENT.md aren't double-nudged. Operator turns on per
        # agent when they want stronger tool-selection guidance.
        "tool_selection_hints": False,
        # When true, the harness checks git status before accepting a
        # text-only stop: if there are uncommitted changes and no commit
        # happened this session, it injects one nudge to run PERSIST.
        # Budget: 2 retries (shared with _consecutive_text_only).
        # Intended for long-running, git-native agents that must commit
        # every cycle. Off by default.
        "persist_nudge": False,
        # List of task descriptions to pre-seed into task_tracker at session
        # start. Seeding only fires when there are no open tasks — either the
        # first run ever, or the previous cycle fully completed all tasks.
        # If open tasks remain (interrupted cycle) the list is left untouched
        # so the agent picks up from where it left off.
        # Intended for agents with a fixed phase loop (e.g. PERCEIVE→PERSIST).
        "initial_tasks": [],
        # Per-turn text-only response cap (characters). Only enforced when nudge
        # is enabled — has no effect when nudge is off.  Prevents context-filling
        # spirals (field observation: 50K-char monologue → 10 ctx-overflow errors).
        # ~24000 chars ≈ 6000 tokens — allows long reasoning/analysis turns while
        # still cutting runaway loops well before they exhaust the context window.
        "max_text_response_chars": 24000,
        # Enable auto-nudge on text-only responses. Off by default.
        # Also settable via --nudge CLI flag (either enables it).
        "nudge": False,
        # Cap on text content generated AFTER tool calls in the same turn.
        # Prevents post-tool prose spirals (field observation: garbled task_tracker
        # adds set receiving_tools=True, bypassing the pre-tool cap, then 50K
        # chars of post-tool prose filled the context).
        # 2000 chars is generous for any legitimate wrap-up text.
        "max_post_tool_text_chars": 2000,
        # Restrict the tool schema sent to the LLM to this exact list of names.
        # null (default) = all tools available. Use to reduce schema tokens and
        # context pressure for agents that don't need read_pdf / subagent / etc.
        # Example: ["read_file", "write_file", "edit_file", "append_file",
        #           "list_files", "exec_command", "search_files", "find_symbol",
        #           "think", "task_tracker"]
        "tools_whitelist": None,
        # The end_cycle tool (a clean "I am finished" exit for a cycle). Off by
        # default: the model never sees it and a call to it is refused, so an
        # always-on interactive agent cannot end its own session. A cycle runner
        # that wants the explicit exit opts in with true; the text-only completion
        # paths end a cycle either way.
        "end_cycle_tool": False,
        # The restart tool: the model may re-execute its own process with -c (the same
        # session, from the checkpoint written as the turn ends). Off by default; /restart
        # at the keyboard always works.
        "restart_tool": False,
        # After a restart the model asked for (the restart tool), send this as the user's
        # message and run a turn, instead of waiting at the prompt until someone types
        # or a monitor pings. Empty: wait at the prompt. A keyboard /restart always
        # returns to the prompt.
        "restart_resume_prompt": "",
        # The event bus (monitor_bus.py): a detached or background exec_command
        # session, and a background subagent, queue one item when they exit.
        "session_events": True,
        # The gate in front of event sources (gate.py). `enabled` is the master
        # switch, read at startup: off, nothing is judged or held, a saved brief is
        # inert, and the `gate` tool and monitor's brief/bypass/quiet are refused.
        # On, the summary model judges a source only once it carries a brief, so
        # the switch itself costs nothing. The other keys (timeout_s,
        # digest_after_s, max_held_chars, digest_raw_chars, log_dir) default from
        # gate.DEFAULTS; see docs/config-reference.md.
        "gate": {"enabled": False},
    },
}


# ── Loading and merging ───────────────────────────────────────────────


ENDPOINT_KEYS = ("base_url", "api_key", "api_key_env", "model")


def drop_llamacpp_defaults(block, user_block):
    """A user ``llm`` / ``summary`` block that names another backend kind (e.g.
    ``{"kind": "aicommons", "model": ...}``) must not inherit the shipped llamacpp
    endpoint: a role's own ``base_url`` outranks every credential source the
    gateway backends have, so the default http://127.0.0.1:8080 (main) and :8082
    (summary) silently won and every request went to the local server. Drops the
    endpoint keys the user did not write themselves."""
    from llm_backend import normalize_kind
    try:
        kind = normalize_kind((user_block or {}).get("kind") or "llamacpp")
    except Exception:  # noqa: BLE001 — an unknown kind is build_backend's error to report
        return
    if kind == "llamacpp":
        return
    for k in ENDPOINT_KEYS:
        if k not in user_block:
            block.pop(k, None)


def synthesize_backends_registry(config):
    """Build a ``backends`` registry dict from the legacy ``llm`` / ``summary``
    top-level blocks, or pass through an explicit ``backends`` block.

    See plan/bedrock-integration.md § 6 "Migration strategy". Preserves every
    field from the legacy block so unknown keys survive the shim intact.
    """
    if "backends" in config and isinstance(config["backends"], dict):
        return config["backends"]

    main = {"kind": "llamacpp"}
    main.update(config.get("llm", {}))

    summary = {"kind": "llamacpp"}
    summary.update(config.get("summary", {}))

    return {"main": main, "summary": summary}


def redact_api_keys(cfg):
    """Return a copy of ``cfg`` with every nested ``api_key`` value redacted.

    Plan § 18.75 security checklist: any ``log.debug("config: %s", _config)``
    line must not surface the literal key. Walks ``backends``
    specifically (the only surface that carries an ``api_key``) plus any
    top-level ``api_key`` field for defensive-depth reasons.
    """
    if not isinstance(cfg, dict):
        return cfg
    result = {}
    for k, v in cfg.items():
        if k == "api_key" and v:
            result[k] = "***REDACTED***"
        elif isinstance(v, dict):
            result[k] = redact_api_keys(v)
        else:
            result[k] = v
    return result


def display_backend_kind(kind, base_url) -> str:
    """Display label for a backend's ``kind``. AWS gateways are configured as
    ``llamacpp`` but live behind ``*.amazonaws.com`` — surface those as ``aws``
    so the banner is meaningful without exposing the endpoint URL.
    """
    if kind == "aicommons":
        return "ai-commons"
    if base_url and "amazonaws.com" in str(base_url).lower():
        return "aws"
    return kind or ""


def warn_if_world_readable_with_key(config_path, user_config):
    """Emit a WARN log line if ``config.json`` is world-readable AND
    contains a non-empty ``api_key`` under any ``backends`` entry.

    Plan § 18.75 security checklist. Don't enforce; just warn.
    """
    if os.name == "nt":
        # Windows uses NTFS ACLs, not POSIX mode bits — os.stat().st_mode is
        # synthetic (typically 0o666) and `chmod 600` is a no-op here, so the
        # warning is a false positive. The redistributable protection against
        # committing keys is the .gitignore entry (see _ensure_agent_dirs).
        return
    if not isinstance(user_config, dict):
        return
    # Walk backends → {main,summary} → api_key to check for a non-empty key.
    has_key = False
    backends = user_config.get("backends", {})
    if isinstance(backends, dict):
        for entry in backends.values():
            if isinstance(entry, dict) and entry.get("api_key"):
                has_key = True
                break
    if isinstance(user_config.get("aicommons"), dict) and user_config["aicommons"].get("api_key"):
        has_key = True
    if not has_key:
        return
    try:
        mode = os.stat(str(config_path)).st_mode & 0o777
    except OSError:
        return
    if mode & 0o077:  # any permission bits for group or other
        logging.getLogger("agent").warning(
            "config.json is world-readable (mode 0o%o); chmod 600 %s",
            mode,
            config_path,
        )


def load():
    """Load configuration from CWD/config.json, deep-merged with defaults.

    Also synthesizes a ``backends`` registry (plan § 6 / D3) from the legacy
    ``llm`` / ``summary`` blocks for back-compat. Existing call sites that
    read ``_config["llm"]`` / ``_config["summary"]`` continue to work.
    """
    config = json.loads(json.dumps(DEFAULTS))  # deep copy

    # Prefer .agent/config.json (keeps local, key-bearing config out of the repo
    # root and alongside the other .agent/ runtime files); fall back to the
    # legacy ./config.json so existing setups keep working unchanged.
    _agent_cfg = Path(os.getcwd()) / ".agent" / "config.json"
    _legacy_cfg = Path(os.getcwd()) / "config.json"
    config_path = _agent_cfg if _agent_cfg.exists() else _legacy_cfg
    user_config = None
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8", errors="replace") as f:
                user_config = json.load(f)
            for section in config:
                if section in user_config and isinstance(user_config[section], dict):
                    config[section].update(user_config[section])
            # Carry through top-level overrides that aren't DEFAULTS
            # sections: scalar keys (log_dir, log_prefix) AND new dict sections
            # such as `advisor` (a role the defaults don't ship). Without this a
            # user-provided advisor block would be silently dropped and the
            # escalation tier would never activate. Dicts are deep-copied so the
            # loaded config can't alias user_config.
            for key, val in user_config.items():
                if key not in config:
                    config[key] = (json.loads(json.dumps(val))
                                   if isinstance(val, dict) else val)
            warn_if_world_readable_with_key(config_path, user_config)
        except (json.JSONDecodeError, IOError) as e:
            # load runs at MODULE IMPORT, before _cb is wired, so the
            # on_notice emit fires into the void (verified 2026-08-16: a
            # malformed .agent/config.json was silently ignored and the agent
            # ran on DEFAULTS with no word — the user's endpoint/model settings
            # vanished invisibly). Stderr + log always reach the user; name the
            # file and say defaults are now in effect. Same stderr-for-config-
            # integrity pattern as _warn_if_config_tracked.
            msg = (f"could not parse {config_path} ({e}) — ALL its settings are "
                   f"IGNORED and engine DEFAULTS are in effect. Fix the JSON or "
                   f"re-run /setup.")
            DEPS["emit"]("on_notice", "warn", f"Warning: {msg}")
            logging.getLogger("agent").error("config load failed: %s", msg)
            print(f"⚠ {msg}", file=sys.stderr)
            user_config = None

    # Back-compat shim: if user provided an explicit `backends` block, use it
    # as-is; otherwise synthesize from legacy `llm` / `summary` blocks.
    merge_src = {}
    if user_config and isinstance(user_config.get("backends"), dict):
        merge_src["backends"] = user_config["backends"]
    # Merge the synthesized registry back on top of the deep-copied default
    # `llm` / `summary` blocks so each backend entry carries kind + defaults.
    merge_src.setdefault("llm", config["llm"])
    merge_src.setdefault("summary", config["summary"])
    config["backends"] = synthesize_backends_registry(merge_src)
    if user_config and "backends" not in merge_src:
        # Scrub the registry, not the legacy blocks: plenty of code reads
        # _config["llm"]["base_url"] / ["model"] directly (BASE_URL at import).
        for role, section in (("main", "llm"), ("summary", "summary")):
            if isinstance(user_config.get(section), dict):
                drop_llamacpp_defaults(config["backends"][role], user_config[section])

    return config


# ── What the command line and the roles put on it ────────────────────


# M6: what a subagent process inherits. The parent's backend choices ride in the
# environment (the subagent tool runs in this process and builds the command);
# `subagent.model` / `subagent.effort` in config pick a cheaper tier for children,
# which start with a cold cache anyway (E2).
SUBAGENT_ARGS_ENV = "AGENT_SUBAGENT_ARGS"


def export_backend_overrides(args) -> dict:
    """Tell in-process tools that read config from disk (consult_advisor) which backend
    the command line chose: kind / model / effort per role, never URLs or keys."""
    out = {}
    for role, kind in (("main", getattr(args, "backend_main", None)),
                       ("summary", getattr(args, "backend_summary", None))):
        if kind:
            out[role] = {"kind": kind, "model": _cfg()["backends"].get(role, {}).get("model")}
    if getattr(args, "model", None) or getattr(args, "effort", None):
        out.setdefault("main", {"kind": _cfg()["backends"].get("main", {}).get("kind")})
        out["main"].update({"model": _cfg()["backends"]["main"].get("model"),
                            "effort": getattr(args, "effort", None)})
    if out:
        os.environ["AGENT_BACKEND_OVERRIDES"] = json.dumps(out)
    return out


def export_subagent_args(args) -> list:
    out = []
    for flag, val in (("--backend-main", getattr(args, "backend_main", None)),
                      ("--backend-summary", getattr(args, "backend_summary", None))):
        if val:
            out += [flag, val]
    sub = _cfg().get("subagent") if isinstance(_cfg().get("subagent"), dict) else {}
    model = sub.get("model") or getattr(args, "model", None)
    effort = sub.get("effort") or getattr(args, "effort", None)
    if model:
        out += ["--model", str(model)]
    if effort:
        out += ["--effort", str(effort)]
    if out:
        os.environ[SUBAGENT_ARGS_ENV] = json.dumps(out)
    return out


def with_role(backends: dict, role: str) -> dict:
    """Return the role's backend config dict with ``role`` injected.

    Plan task 2.4: ``BedrockBackend`` uses ``role`` to pick the daily cost
    cap and label telemetry lines. LlamacppBackend ignores ``role`` so the
    injection is safe for both kinds.
    """
    entry = dict(backends.get(role, {}))
    entry.setdefault("role", role)
    try:
        from llm_backend import normalize_kind
        if normalize_kind(entry.get("kind")) == "aicommons" and isinstance(_cfg().get("aicommons"), dict):
            # the shared top-level block (base_url, api_key, thinking, caps) under the
            # role's own keys, which win
            entry["aicommons"] = {**_cfg()["aicommons"], **(entry.get("aicommons") or {})}
    except NameError:  # no config yet (a bare registry, as the tests pass one)
        pass
    return entry


SEED_WARNED = False


def apply_generation_overrides(args) -> dict:
    """F8a: ``--temperature`` / ``--top-p`` / ``--seed`` onto ``_config["generation"]``
    (flag > config > default). Returns the applied overrides (unit-tested). A seed on a backend
    kind that does not honor one warns ONCE and is kept in config so the request builder can
    still decide per kind."""
    global SEED_WARNED
    gen = _cfg()["generation"]
    applied = {}
    for flag, key in (("temperature", "temperature"), ("top_p", "top_p"), ("seed", "seed")):
        val = getattr(args, flag, None)
        if val is not None:
            gen[key] = val
            applied[key] = val
    seed = gen.get("seed")
    if seed is not None:
        kind = (_cfg().get("backends", {}).get("main", {}) or {}).get("kind", "llamacpp")
        if kind != "llamacpp" and not SEED_WARNED:
            SEED_WARNED = True
            logging.getLogger("agent").warning(
                "generation.seed=%s set but the %s backend does not honor a sampling seed — "
                "runs will not be reproducible on this backend", seed, kind)
    return applied


def generation_request_extras() -> dict:
    """Extra request-body keys derived from generation config: currently the seed, only when
    set (llama.cpp honors `seed` on /v1/chat/completions; absent = server default)."""
    seed = _cfg()["generation"].get("seed")
    return {"seed": int(seed)} if seed is not None else {}

BEDROCK_DEFAULT_MODELS = {"main": "claude-v4.6-opus", "summary": "claude-v4.5-haiku"}
AICOMMONS_DEFAULT_MODELS = {"main": "claude-v5.5-sonnet", "summary": "claude-v5.5-haiku"}


def override_backend_kind(role, kind):
    """``--backend-main`` / ``--backend-summary``: set the role's ``kind`` in the registry.
    A model set for the kind being replaced (e.g. a local server's model name carried over
    from the llm block) means nothing to the new one and a gateway would 422 on it, so only
    a model set for this same kind is kept; the gateway kinds get a default model otherwise,
    and aicommons drops the endpoint keys that belonged to the old kind. No-op without a kind."""
    if not kind:
        return
    from llm_backend import normalize_kind

    def _same_kind(a, b):
        try:
            return normalize_kind(a or "llamacpp") == normalize_kind(b or "llamacpp")
        except Exception:  # noqa: BLE001
            return False

    entry = _cfg()["backends"].setdefault(role, {})
    prev_kind = entry.get("kind")
    entry["kind"] = kind
    foreign_model = not _same_kind(prev_kind, kind)
    if kind == "aicommons":
        if prev_kind not in ("aicommons", "ucsb", "ai-commons", "ai_commons"):
            # The entry's endpoint and key belong to the kind being replaced (usually the
            # llamacpp default http://127.0.0.1:8080). Left in place they outrank the
            # aicommons block, AICOMMONS_* env and the keystore, and every request goes
            # to the local server instead of the gateway.
            for k in ("base_url", "api_key", "api_key_env"):
                entry.pop(k, None)
        existing = entry.get("model", "")
        if not existing or foreign_model:
            entry["model"] = AICOMMONS_DEFAULT_MODELS[role]
    if kind == "bedrock":
        existing = entry.get("model", "")
        if not existing or foreign_model:
            entry["model"] = BEDROCK_DEFAULT_MODELS[role]
            logging.getLogger("agent").info(
                "backend-override role=%s kind=bedrock model=%s "
                "(default — replaced incompatible %r)",
                role,
                entry["model"],
                existing or "<unset>",
            )



# ── The role setters ──────────────────────────────────────────────────


# The one supported way for a tool to act on the running harness. Every entry looks the
# live globals up when it is called, through the module Python knows as `agent` (see
# _end_cycle_gate for why), so a backend rebuilt later is still the one acted on and a
# function defined further down this file is found by then.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def persist_value(section, key, value):
    """Persist ``config[section][key] = value`` to ``./.agent/config.json``.

    Deep-merges into any existing file: pre-existing sections/keys are
    preserved, only ``section.key`` is updated. Creates ``.agent/`` and the
    file if missing. Writes UTF-8 with ``indent=2``. This is the same path
    ``load`` prefers, so the choice survives a restart.

    A corrupt existing file is treated as empty (so a bad on-disk config never
    kills the session); the freshly-written file then contains just the merged
    section.
    """
    cfg_path = Path(os.getcwd()) / ".agent" / "config.json"
    data = {}
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8", errors="replace") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, IOError):
            data = {}
    sect = data.get(section)
    if not isinstance(sect, dict):
        sect = {}
        data[section] = sect
    sect[key] = value
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return cfg_path


def set_model_for_role(role, new_model):
    """Apply ``new_model`` to the given role (``"main"`` / ``"summary"``).

    Updates the in-memory ``_config`` (``llm.model`` / ``summary.model``) AND
    the live backend object (``_main_backend.model`` / ``_summary_backend.model``)
    so the banner/footer reflect it immediately, then persists the change to
    ``./.agent/config.json``.
    """
    section = "llm" if role == "main" else "summary"
    _cfg().setdefault(section, {})["model"] = new_model
    backend = _main() if role == "main" else _summary()
    entry = (_cfg().get("backends") or {}).get(role)
    if isinstance(entry, dict):
        entry["model"] = new_model      # the registry is what a rebuild reads
    if backend is not None:
        try:
            if hasattr(backend, "set_model"):
                backend.set_model(new_model)   # re-derives per-model limits
            else:
                backend.model = new_model
        except AttributeError:
            pass
    return persist_value(section, "model", new_model)


def get_effort_for_role(role):
    """The role's reasoning effort: the live backend's when it has one, else the config's."""
    section = "llm" if role == "main" else "summary"
    backend = _main() if role == "main" else _summary()
    return str(getattr(backend, "effort", None) or (_cfg().get(section) or {}).get("effort") or "")


def set_effort_for_role(role, effort):
    """Apply a reasoning effort to a role's live backend (when it takes one) and persist
    ``<section>.effort``, the key the backend reads at construction. ``/effort`` and
    ``tools.RUNTIME["set_effort"]`` use it. A model switch keeps the backend's effort."""
    effort = str(effort or "").strip().lower()
    if effort not in EFFORT_LEVELS:
        raise ValueError(f"effort must be one of {', '.join(EFFORT_LEVELS)}, not {effort!r}")
    section = "llm" if role == "main" else "summary"
    _cfg().setdefault(section, {})["effort"] = effort
    entry = (_cfg().get("backends") or {}).get(role)
    if isinstance(entry, dict):
        entry["effort"] = effort       # a failover rebuild reads the registry
    backend = _main() if role == "main" else _summary()
    if backend is not None:
        if hasattr(backend, "set_effort"):
            backend.set_effort(effort)
        elif hasattr(backend, "effort"):
            backend.effort = effort
    return persist_value(section, "effort", effort)


def set_system_prompt(text):
    """Set (or, with ``""``, clear) ``preferences.system_prompt`` for this
    session and persist it to ``./.agent/config.json``. The run loop reads it
    on every request, so it applies from the next one."""
    _cfg().setdefault("preferences", {})["system_prompt"] = text
    return persist_value("preferences", "system_prompt", text)
