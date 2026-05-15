"""LLM backend abstraction for agent.py.

See ``plan/bedrock-integration.md`` for the full design rationale. This module
implements the Phase-1 backend surface: a ``Backend`` Protocol, a concrete
``LlamacppBackend`` wrapping the current llama.cpp / OpenAI-compatible HTTP
transport, and a ``build_backend(cfg)`` factory. A ``BedrockBackend`` is *not*
included here — the factory raises ``NotImplementedError`` for ``kind="bedrock"``
so Phase-1 tests can confirm dispatch without the Bedrock port existing yet.

Key decisions reflected in this module (see plan § 24 decision log):

- D1: Single ``Backend`` class exposing both streaming and non-streaming
  methods. The method set is small; two narrow protocols would double the
  registry and force dual instantiation when main and summary share a kind.
- D2: ``stream_chat()`` returns a ``requests.Response`` whose ``iter_lines``
  yields OpenAI-style SSE deltas — same shape the agent's existing loop
  already consumes (``agent.py`` around line 1963). Phase 2 will revisit
  this return type if Bedrock needs to synthesize SSE.
- D3: Config shape is the ``backends`` registry with ``main`` and ``summary``
  pointers; each backend dict carries its own ``kind`` field.
- D5: Default when nothing is configured is both backends = llamacpp at
  today's URLs (:8080 main, :8082 summary). Rollback-safe.
- D7: Token counts for non-llamacpp backends are approximate (Gemma-3
  fallback) — over-reserves context, which is the safe direction.
- D9: ``detect_ctx_size()`` returns ``None`` when a backend can't introspect;
  callers already handle ``None``.
- D10: Cancellation flows through a ``cancel_check`` callback threaded from
  the agent's existing ``check_cancelled()`` plumbing.
- D11: The Bedrock factory path (Phase 2) reads env vars once at backend
  construction; we do not wire env reads into backend ``__init__`` itself.

Import boundary: this module does not import ``agent``. Callers instantiate
backends via ``build_backend(cfg)`` and pass them in.
"""

from __future__ import annotations

import json
import logging
import os
import random
import tempfile
import threading
import time
from datetime import date
from pathlib import Path
from typing import Callable, Iterator, Protocol

import requests


# ── Internal retry / exception scaffolding (parallels agent.py defaults) ──


class ContextOverflowError(Exception):
    """Raised when the server returns persistent 500s likely due to context overflow."""

    pass


class ConfigError(Exception):
    """Raised when backend construction fails due to a missing / invalid config."""

    pass


class BedrockBudgetExceeded(Exception):
    """Raised when the daily Bedrock spend cap has been exceeded.

    See plan § 6.5 and § 17 K14.
    """

    pass


# Defaults chosen to match agent.py's current behavior.
_DEFAULT_RETRY_CFG = {
    "max_retries": 10,
    "base_delay_seconds": 2,
    "max_delay_seconds": 60,
    "backoff_multiplier": 2.0,
    "jitter_factor": 0.1,
}

_LLM_REQUEST_TIMEOUT = 300  # 5 minutes per request (matches agent.py)


def _calc_retry_delay(attempt: int, cfg: dict) -> float:
    base = cfg["base_delay_seconds"]
    mult = cfg["backoff_multiplier"]
    max_d = cfg["max_delay_seconds"]
    jitter = cfg["jitter_factor"]
    delay = min(base * (mult ** attempt), max_d)
    if jitter > 0:
        span = delay * jitter
        delay = max(0.0, delay + random.uniform(-span, span))
    return round(delay, 2)


# ── Protocol ───────────────────────────────────────────────────────────


class Backend(Protocol):
    """Backend Protocol.

    Concrete backends expose a uniform surface for the agent. Every backend
    must carry a ``kind`` string, a ``model`` string, and a ``base_url``
    string so logging / telemetry can report which transport served a given
    call.
    """

    kind: str
    model: str
    base_url: str

    def health(self) -> tuple[bool, str]: ...

    def detect_ctx_size(self) -> int | None: ...

    def list_models(self) -> list[str]: ...

    def stream_chat(
        self,
        log: logging.Logger,
        *,
        json: dict,
        stream: bool = True,
        timeout: tuple[int, int] | int = (30, 300),
    ) -> "requests.Response": ...

    def complete(
        self,
        *,
        prompt: str,
        gen_params: dict | None = None,
        cancel_check: Callable[[], None] | None = None,
        timeout: float = 120,
    ) -> str: ...


# ── LlamacppBackend ────────────────────────────────────────────────────


class LlamacppBackend:
    """Concrete backend for llama.cpp / OpenAI-compatible HTTP servers.

    Wraps the request plumbing that was previously at module scope in
    ``agent.py`` (``_llm_request``, ``_summary_request``, ``_check_api_health``,
    ``_detect_ctx_size``, ``_list_available_models``). Behavior matches the
    pre-refactor code — this is a pure refactor carrier for Phase 1.
    """

    kind = "llamacpp"

    def __init__(self, cfg: dict):
        self._cfg = cfg
        self.base_url = cfg.get("base_url", "http://127.0.0.1:8080")
        self.model = cfg.get("model", "")
        self._retry_cfg = cfg.get("retry", _DEFAULT_RETRY_CFG)
        self.enabled = cfg.get("enabled", True)
        self.max_wait_on_save = cfg.get("max_wait_on_save", 10)

    # ── Introspection probes ──

    def health(self, timeout: int = 3) -> tuple[bool, str]:
        """Probe the LLM endpoint. Return ``(ok, detail)``."""
        try:
            resp = requests.get(f"{self.base_url}/health", timeout=timeout)
            if resp.status_code == 200:
                return True, "ok"
            return False, f"HTTP {resp.status_code}"
        except requests.Timeout:
            return False, "timeout"
        except requests.ConnectionError:
            return False, "unreachable"
        except requests.RequestException as e:
            return False, str(e)[:60]

    def detect_ctx_size(self, timeout: int = 3) -> int | None:
        """Query llama-server ``/slots`` endpoint and return n_ctx for slot 0, or None."""
        try:
            resp = requests.get(f"{self.base_url}/slots", timeout=timeout)
            if resp.status_code != 200:
                return None
            slots = resp.json()
            if slots and isinstance(slots, list):
                return slots[0].get("n_ctx")
        except (requests.RequestException, ValueError, KeyError, IndexError):
            pass
        return None

    def list_models(self, timeout: int = 3) -> list[str]:
        """Query ``/v1/models`` and return a list of model id strings, or ``[]``."""
        try:
            resp = requests.get(f"{self.base_url}/v1/models", timeout=timeout)
            if resp.status_code != 200:
                return []
            data = resp.json()
            return [m.get("id", "") for m in data.get("data", []) if m.get("id")]
        except (requests.RequestException, ValueError, KeyError):
            return []

    def check_tool_caps(self, timeout: int = 3) -> dict:
        """Query ``/props`` for ``chat_template_caps``. Returns ``{}`` on failure or non-llamacpp servers."""
        try:
            resp = requests.get(f"{self.base_url}/props", timeout=timeout)
            if resp.status_code == 200:
                return resp.json().get("chat_template_caps", {})
        except (requests.RequestException, ValueError, KeyError):
            pass
        return {}

    # ── Main-path: streaming chat ──

    def stream_chat(
        self,
        log: logging.Logger,
        *,
        json: dict | None = None,
        stream: bool = True,
        timeout: tuple[int, int] | int = (30, 300),
        **extra_kwargs,
    ) -> "requests.Response":
        """POST to ``/v1/chat/completions`` with retries and exponential backoff.

        Returns the ``requests.Response`` object so the caller can
        ``iter_lines()`` over it. Mirrors the pre-refactor ``_llm_request``
        signature exactly so existing test patches against ``agent._llm_request``
        continue to work. Any extra kwargs are forwarded to ``requests.post``.

        Raises ``ContextOverflowError`` after 3 consecutive 500s.
        """
        cfg = self._retry_cfg
        max_retries = cfg["max_retries"]
        consecutive_500s = 0

        t0 = time.monotonic()
        ok = False
        deltas = 0  # placeholder for telemetry; actual delta count lives in the caller's SSE loop
        try:
            for attempt in range(max_retries + 1):
                try:
                    response = requests.post(
                        f"{self.base_url}/v1/chat/completions",
                        json=json,
                        stream=stream,
                        timeout=timeout,
                        **extra_kwargs,
                    )
                    if response.status_code >= 500:
                        if response.status_code == 500:
                            consecutive_500s += 1
                            if consecutive_500s >= 3:
                                raise ContextOverflowError(
                                    "3 consecutive HTTP 500 errors — likely context overflow"
                                )
                        else:
                            consecutive_500s = 0
                        raise requests.exceptions.HTTPError(
                            f"Server error {response.status_code}", response=response
                        )
                    response.raise_for_status()
                    ok = True
                    return response
                except ContextOverflowError:
                    raise
                except (
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.HTTPError,
                ) as e:
                    if attempt == max_retries:
                        raise
                    if isinstance(e, requests.exceptions.HTTPError):
                        resp = getattr(e, "response", None)
                        if resp is None or resp.status_code < 500:
                            raise
                    delay = _calc_retry_delay(attempt, cfg)
                    log.warning(
                        "LLM request failed (attempt %d/%d): %s — retrying in %ds",
                        attempt + 1,
                        max_retries + 1,
                        e,
                        delay,
                    )
                    time.sleep(delay)
        finally:
            latency_ms = int((time.monotonic() - t0) * 1000)
            log.info(
                "backend.stream_chat.latency_ms backend=%s model=%s latency_ms=%d deltas=%d ok=%s",
                self.kind,
                self.model,
                latency_ms,
                deltas,
                ok,
            )

    # ── Summary-path: single-shot completion ──

    def complete(
        self,
        *,
        prompt: str,
        gen_params: dict | None = None,
        cancel_check: Callable[[], None] | None = None,
        timeout: float = 120,
    ) -> str:
        """Non-streaming single-shot completion. Returns response text.

        Mirrors the pre-refactor ``_summary_request`` body shape. The
        ``gen_params`` dict overrides the default "summary-shaped"
        temperature/top_p/max_tokens; omitted keys fall back to summary
        defaults.
        """
        gp = gen_params or {}
        request_body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": gp.get("temperature", 0.3),
            "top_p": gp.get("top_p", 0.9),
            "top_k": gp.get("top_k", 20),
            "presence_penalty": gp.get("presence_penalty", 0.0),
            "max_tokens": gp.get("max_tokens", 1024),
            "chat_template_kwargs": {"enable_thinking": False},
            "stream": False,
        }

        log = logging.getLogger("llm_backend")
        t0 = time.monotonic()
        ok = False
        try:
            response = requests.post(
                f"{self.base_url}/v1/chat/completions",
                json=request_body,
                timeout=timeout,
            )
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"].strip()
            ok = True
            return text
        finally:
            latency_ms = int((time.monotonic() - t0) * 1000)
            log.info(
                "backend.complete.latency_ms backend=%s model=%s latency_ms=%d ok=%s",
                self.kind,
                self.model,
                latency_ms,
                ok,
            )


# ── BedrockBackend ────────────────────────────────────────────────────


# Port of ``llmbox_lib.py:169-179`` — per-model context defaults (in chars,
# not tokens; the gateway doesn't expose a real ctx size). Used for the
# banner and context budgeter when ``detect_ctx_size()`` is called.
# Reserve this many chars of the context budget for the dev-mode preamble
# (tool manual + RULES + one-shot example) when main=bedrock. 8000 chars is
# ~2k tokens at Claude's BPE, matching the K12/§ 8.4 estimate. Subtracted
# from the per-model context cap in ``BedrockBackend.detect_ctx_size`` for
# role=main only — summary path doesn't use dev-mode.
_DEV_MODE_PREAMBLE_RESERVE_CHARS = 8000


_MODEL_CONTEXT_CHARS = {
    "claude-v4.6-opus": 700000,
    "claude-v4.6-sonnet": 700000,
    "claude-v4.5-opus": 700000,  # keep for back-compat during rollover
    "claude-v4.5-sonnet": 700000,
    "claude-v4.5-haiku": 700000,
    "claude-v3.7-sonnet": 700000,
    "claude-v3.5-haiku": 700000,
    "claude-v3-opus": 700000,
    "llama-4-maverick-17b-instruct": 450000,
    "llama-4-scout-17b-instruct": 450000,
    "llama3-3-70b-instruct": 450000,
    "llama": 450000,
    "mistral-large-2": 112000,
    "mistral-large": 112000,
    "mistral": 112000,
    "mixtral": 112000,
    "deepseek-r1": 224000,
    "deepseek": 224000,
    "amazon-nova-pro": 450000,
    "amazon-nova-lite": 450000,
    "amazon-nova-micro": 450000,
    "amazon-nova": 450000,
    "qwen3-32b": 112000,
    "qwen": 112000,
}


# Per-model pricing table (USD per 1M tokens). Source: AWS Bedrock price
# list (Anthropic section) as of 2026-04-24. Earlier values for opus
# (15/75) and haiku (0.25/1.25) were list-price estimates from an older
# tier — 3× over for opus and 4× under for haiku against actual Bedrock.
# Unknown models get 0.0 and a WARN (see plan § 6.5).
_BEDROCK_PRICING = {
    "claude-v4.7-opus":   {"in": 5.00, "out": 25.00},
    "claude-v4.6-opus":   {"in": 5.00, "out": 25.00},
    "claude-v4.6-sonnet": {"in": 3.00, "out": 15.00},
    "claude-v4.5-opus":   {"in": 5.00, "out": 25.00},
    "claude-v4.5-sonnet": {"in": 3.00, "out": 15.00},
    "claude-v4.5-haiku":  {"in": 1.00, "out":  5.00},
    "claude-v3.7-sonnet": {"in": 3.00, "out": 15.00},
    "claude-v3.5-haiku":  {"in": 1.00, "out":  5.00},
}


# Persistence file for the daily spend counter (plan § 6.5). File mode
# is enforced at write time to ``0o600`` per § 18.75 security checklist.
_SPEND_FILE = "/droid/repos/agent/CICD/bedrock_spend.json"

# Default daily caps (USD). See plan § 24 S5 decision.
_DEFAULT_DAILY_CAPS = {"main": 60.00, "summary": 3.00}

# Module-level lock serialising the read-modify-write cycle in _record_spend
# and _load_today_spend.  Without this, concurrent calls from the main loop
# and the async SummaryThread both read the same stale file, each add their
# own increment, and one os.replace() silently discards the other's write —
# causing daily spend to be underreported and the budget cap to be bypassed.
# (Issue #834)
_spend_lock = threading.Lock()


def _spend_file_path() -> Path:
    """Return the spend-file Path. Exposed so tests can monkeypatch."""
    return Path(_SPEND_FILE)


def _load_today_spend(role: str) -> float:
    """Read the persisted spend counter for ``role`` on today's date.

    Returns 0.0 if the file doesn't exist, is corrupt, or has no entry
    for today's role. Robustness over strictness — a corrupt counter is
    better than a hard failure at startup.
    """
    path = _spend_file_path()
    with _spend_lock:
        try:
            raw = path.read_text()
            data = json.loads(raw)
            today = date.today().isoformat()
            return float(data.get(today, {}).get(role, 0.0))
        except (OSError, ValueError, TypeError):
            return 0.0


def _record_spend(role: str, cost_usd: float) -> float:
    """Increment today's counter for ``role`` by ``cost_usd``. Atomic
    write (tempfile + ``os.replace``) with mode ``0o600``. Returns the
    new daily total for this role.

    Thread-safe: the entire read-modify-write cycle is serialised under
    ``_spend_lock`` so concurrent calls from the main loop and the async
    SummaryThread never lose each other's increments (issue #834).
    """
    path = _spend_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _spend_lock:
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}

        today = date.today().isoformat()
        day_entry = data.setdefault(today, {})
        if not isinstance(day_entry, dict):
            day_entry = {}
            data[today] = day_entry
        new_total = float(day_entry.get(role, 0.0)) + float(cost_usd)
        day_entry[role] = round(new_total, 6)

        # Atomic write: tempfile in same directory, then os.replace.
        fd, tmp_path = tempfile.mkstemp(prefix=".bedrock_spend.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, path)
        except Exception:
            # If anything fails, make a best effort to clean up.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return new_total


def _estimate_cost(role: str, in_tokens: int, out_tokens: int, model: str) -> float:
    """Estimate USD cost for a single Bedrock call.

    Looks up ``model`` in ``_BEDROCK_PRICING``. Unknown models get a
    WARN log line and return 0.0 (don't crash). ``role`` is used only
    for the log key; pricing is uniform per model.
    """
    pricing = _BEDROCK_PRICING.get(model)
    if pricing is None:
        logging.getLogger("llm_backend").warning(
            "bedrock.cost.unknown_model model=%s role=%s — cost=0.00 "
            "(update _BEDROCK_PRICING in llm_backend.py to track)",
            model,
            role,
        )
        return 0.0
    cost_in = (in_tokens / 1_000_000.0) * pricing["in"]
    cost_out = (out_tokens / 1_000_000.0) * pricing["out"]
    return cost_in + cost_out


def _resolve_daily_cap(cfg: dict, role: str) -> float:
    """Resolve the daily cap for ``role``. Order: env var override →
    ``cfg['daily_cost_cap_usd']`` → default.
    """
    env_cap = os.environ.get("BEDROCK_DAILY_CAP_USD")
    if env_cap:
        try:
            return float(env_cap)
        except ValueError:
            pass
    cap = cfg.get("daily_cost_cap_usd")
    if isinstance(cap, (int, float)):
        return float(cap)
    if isinstance(cap, dict):
        val = cap.get(role)
        if isinstance(val, (int, float)):
            return float(val)
    return _DEFAULT_DAILY_CAPS.get(role, 10.00)


def _approx_token_count(text: str) -> int:
    """Char-based token approximation used when ``token_utils`` can't be
    imported (lazy import — avoids pulling tokenizer deps into startup).
    The Gemma-3 tokenizer overshoots Claude text by ~10-20% (safe
    direction for a budget guardrail). Fallback: chars/4.
    """
    try:
        from token_utils import count_tokens

        return count_tokens(text or "")
    except Exception:
        return max(1, len(text or "") // 4)


def _resolve_bedrock_credentials(
    *, env_url: str, env_key: str
) -> tuple[str, str, str | None]:
    """Issue #405 — return ``(url, key, entry_name_or_None)``.

    Consults ``bedrock_store`` if importable; on any failure (module
    missing, store unreadable, empty store with no env fallback) returns
    ``(env_url, env_key, None)`` so the legacy env-only path keeps
    working. The ``ConfigError`` is raised by the caller when both are
    blank — that mirrors the pre-#405 behaviour.
    """
    try:
        import bedrock_store  # local import — keeps module side-effect-free
    except Exception:
        return env_url, env_key, None
    try:
        url, key, name = bedrock_store.select_credentials(
            env_url=env_url, env_key=env_key,
        )
        return url, key, name
    except LookupError:
        return env_url, env_key, None
    except Exception:  # pragma: no cover - defensive
        return env_url, env_key, None


# Per-model inference parameter defaults for the Bedrock gateway.
# Keys use snake_case internally; _build_inference_params() renames to camelCase.
# Lookup uses longest-prefix match so "amazon-nova-micro" wins over "amazon-nova".
_BEDROCK_INFERENCE_DEFAULTS: dict[str, dict] = {
    "claude-v4":          {"temperature": 1.0, "max_tokens": 4096, "top_p": 0.999, "top_k": 250},
    "claude-v3":          {"temperature": 1.0, "max_tokens": 4096, "top_p": 0.999, "top_k": 250},
    "llama-4":            {"temperature": 0.7, "max_tokens": 2048, "top_p": 0.9},
    "llama3":             {"temperature": 0.7, "max_tokens": 2048, "top_p": 0.9},
    "llama":              {"temperature": 0.7, "max_tokens": 2048, "top_p": 0.9},
    "mistral-large":      {"temperature": 0.5, "max_tokens": 2048, "top_p": 0.9},
    "mistral":            {"temperature": 0.5, "max_tokens": 2048, "top_p": 0.9},
    "mixtral":            {"temperature": 0.5, "max_tokens": 2048, "top_p": 0.9},
    "amazon-nova-micro":  {"temperature": 0.7, "max_tokens": 1024, "top_p": 0.9},
    "amazon-nova-lite":   {"temperature": 0.7, "max_tokens": 2048, "top_p": 0.9},
    "amazon-nova-pro":    {"temperature": 0.7, "max_tokens": 4096, "top_p": 0.9},
    "deepseek-r1":        {"temperature": 0.6, "max_tokens": 4096, "top_p": 0.95},
    "qwen3":              {"temperature": 0.7, "max_tokens": 2048, "top_p": 0.9},
}

# snake_case → camelCase rename map for gateway payload keys.
_KEY_RENAME = {
    "max_tokens": "maxTokens",
    "top_p": "topP",
    "top_k": "topK",
    "stop_sequences": "stopSequences",
    "temperature": "temperature",
}


def _build_inference_params(model: str, overrides: dict | None = None) -> dict | None:
    """Return an inferenceParams dict for the gateway payload, or None.

    Key names in the returned dict are camelCase: maxTokens, topP, topK,
    stopSequences (matching the gateway's InferenceParams schema).

    Lookup algorithm:
      1. Iterate _BEDROCK_INFERENCE_DEFAULTS keys sorted by length descending.
      2. First key that is a prefix of ``model`` wins (longest-prefix match).
      3. If no match: use empty dict as base.
      4. Merge ``overrides`` on top (override wins). None values stripped.
      5. Rename keys to camelCase before return.
    Returns None (not {}) when the result is empty.
    """
    base: dict = {}
    if model:
        for key in sorted(_BEDROCK_INFERENCE_DEFAULTS, key=len, reverse=True):
            if model.startswith(key):
                base = dict(_BEDROCK_INFERENCE_DEFAULTS[key])
                break
    merged = {**base, **(overrides or {})}
    result = {
        _KEY_RENAME.get(k, k): v
        for k, v in merged.items()
        if v is not None
    }
    return result or None


def _extract_gen_params_from_body(body: dict | None) -> dict:
    """Extract gen params from an OpenAI-style body dict. Ignores None values."""
    if not body:
        return {}
    return {
        k: body[k] for k in ("temperature", "max_tokens", "top_p", "top_k")
        if body.get(k) is not None
    }


class BedrockBackend:
    """Bedrock Chat gateway backend (see plan § 7.3).

    Wraps ``bedrock_api.BedrockChatAPI`` and speaks OpenAI-shape SSE
    deltas upstream (via dev-mode prompt stuffing for tool calls).
    Supports both ``complete()`` (summary path) and ``stream_chat()``
    (main loop, with truncation recovery per § 8.3).
    """

    kind = "bedrock"

    def __init__(self, cfg: dict):
        self._cfg = cfg
        self.role = cfg.get("role", "main")

        # Issue #405 — consult the bedrock credential store first so a
        # multi-gateway setup picks the lowest-spend ``up`` entry. Falls
        # back to env vars when the store is empty (back-compat) or to
        # the cfg-supplied creds (which still override everything for
        # the existing test_bedrock_backend_config_beats_env path).
        cfg_url = cfg.get("api_url")
        cfg_key = cfg.get("api_key")
        store_entry_name: str | None = None
        if cfg_url and cfg_key:
            api_url = cfg_url
            api_key = cfg_key
        else:
            api_url, api_key, store_entry_name = _resolve_bedrock_credentials(
                env_url=cfg_url or os.environ.get("BEDROCK_API_URL", ""),
                env_key=cfg_key or os.environ.get("BEDROCK_API_KEY", ""),
            )
        if not api_url or not api_key:
            raise ConfigError(
                "Bedrock backend requires BEDROCK_API_URL and BEDROCK_API_KEY "
                "(either in config.json, environment, or bedrock_store)"
            )
        # K4 mitigation — trim trailing slash so f"{api_url}/health" doesn't
        # produce a double slash on strict gateways.
        api_url = api_url.rstrip("/")
        self._store_entry_name = store_entry_name

        self.api_url = api_url
        self.base_url = api_url  # banner consistency with LlamacppBackend
        self.model = cfg.get("model", "")
        self.enabled = cfg.get("enabled", True)
        self.max_wait_on_save = cfg.get("max_wait_on_save", 30)

        # Lazy import so the agent can be used without bedrock_api on path
        # for purely-llamacpp sessions.
        from bedrock_api import BedrockChatAPI

        self._api = BedrockChatAPI(
            {
                "api_url": api_url,
                "api_key": api_key,
                "origin": cfg.get("origin", "http://localhost:8000"),
                "model": self.model,
                "poll_interval": cfg.get("poll_interval", 0.3),
                "poll_backoff": cfg.get("poll_backoff", 1.5),
                "poll_max_interval": cfg.get("poll_max_interval", 5.0),
                "poll_timeout": cfg.get("poll_timeout", 180),
            }
        )

        # Issue #864 — cached usage pct for proactive quota check.
        # Must be initialised before _log_token_usage() populates them.
        self._cached_usage_pct: float = 0.0
        self._usage_cache_time: float = 0.0

        # Log monthly token usage at startup (issue #355)
        self._log_token_usage()
        # CICD 358: Conversation reuse tracking
        # CICD 358 / issue #356 — server-side conversation reuse.
        # First stream_chat of a session creates a new conversation (id is
        # None); subsequent calls pass the stored id so the gateway keeps
        # context server-side. Reduces per-bot conversation accumulation
        # from ~60-per-run to 1-per-run (see beewatcher finding — bots on
        # this gateway stop responding after ~50-75 conversations).
        self._active_conv_id: str | None = None
        self._session_conv_count = 0

        # Issue #543 — per-call inference defaults loaded from config.json.
        # None values are stripped so callers can use sentinel-None to unset.
        self._base_inference_params = {
            k: v for k, v in cfg.get("inference_params", {}).items()
            if v is not None
        }

    def _log_token_usage(self) -> None:
        """Fetch and log monthly token usage from the gateway.

        Emits INFO-level log when usage is below 90%, WARNING when at or
        above 90%. Logs a warning and continues if the probe fails.
        """
        log = logging.getLogger("agent")
        try:
            usage = self._api.get_token_usage()
            if usage is None:
                log.warning(
                    "bedrock.token_usage.probe_failed role=%s model=%s",
                    self.role, self.model
                )
                return
            total = usage.get("total_tokens", 0)
            limit = usage.get("token_limit", 1)
            pct = (total / limit * 100) if limit else 0
            # Cache for the proactive quota check (#864)
            self._cached_usage_pct = pct
            self._usage_cache_time = time.monotonic()
            level = logging.WARNING if pct >= 90 else logging.INFO
            log.log(
                level,
                "bedrock.token_usage role=%s model=%s monthly_total=%d/%d used_pct=%.1f",
                self.role, self.model, total, limit, pct
            )
        except Exception as e:  # pragma: no cover - defensive
            log.warning(
                "bedrock.token_usage.probe_failed role=%s model=%s error=%s",
                self.role, self.model, str(e)[:60]
            )

    # ── Proactive quota check (#864) ──

    _USAGE_CACHE_TTL = 300.0  # 5 minutes between usage API calls

    def _get_cached_usage_pct(self) -> float:
        """Return the cached monthly usage %, refreshing if the cache is stale."""
        if time.monotonic() - self._usage_cache_time > self._USAGE_CACHE_TTL:
            self._log_token_usage()
        return self._cached_usage_pct

    # ── Introspection ──

    def health(self) -> tuple[bool, str]:
        """Return ``(ok, detail)``. ``detail`` is the URL on success,
        a short diagnostic on failure. Shape matches LlamacppBackend.
        """
        try:
            ok = self._api.health()
        except Exception as e:  # pragma: no cover - defensive
            return False, f"error: {str(e)[:60]}"
        return (True, self.api_url) if ok else (False, "gateway health failed")

    def detect_ctx_size(self) -> int | None:
        """Gateway doesn't expose ctx_size; return per-model default char
        budget or None if the model is unknown. Plan § 10 / D9.

        For ``role=main``, subtract ``_DEV_MODE_PREAMBLE_RESERVE_CHARS`` to
        reserve headroom for the dev-mode tool manual + one-shot example
        that ``build_dev_prompt`` prepends to every main turn (plan § 10
        and § 8.4 — the preamble adds ~1.5-2k tokens, so ~8k chars at
        Claude's BPE). This prevents the context-budgeter from over-packing
        messages and hitting an "Input too long" error at the gateway.
        Summary path renders its prompt verbatim, so no reserve is needed.
        """
        if not self.model:
            return None
        ctx = _MODEL_CONTEXT_CHARS.get(self.model)
        if ctx is None:
            return None
        if self.role == "main":
            ctx = max(ctx - _DEV_MODE_PREAMBLE_RESERVE_CHARS, 4096)
        return ctx

    def list_models(self) -> list[str]:
        """Try the gateway's OpenAPI spec first; fall back to the
        ported ``_MODEL_CONTEXT_CHARS`` keys if that fails.
        """
        try:
            models = self._api.list_models()
            if models:
                return models
        except Exception:
            pass
        return list(_MODEL_CONTEXT_CHARS.keys())

    # ── Cost accounting ──

    def _record_and_check_cost(self, prompt: str, response: str, log) -> None:
        """Estimate cost, persist, check cap. Raises
        ``BedrockBudgetExceeded`` if the cap would be exceeded.
        """
        in_tokens = _approx_token_count(prompt)
        out_tokens = _approx_token_count(response)
        cost = _estimate_cost(self.role, in_tokens, out_tokens, self.model)
        new_total = _record_spend(self.role, cost)
        cap = _resolve_daily_cap(self._cfg, self.role)
        # Per-call tokens at INFO so CICD run logs (which run at INFO) carry
        # them. Keep the DEBUG tick below for cost-cap detail so verbose
        # runs still get the breakdown without doubling the INFO line.
        log.info(
            "bedrock.tokens role=%s model=%s in=%d out=%d cost_usd=%.4f",
            self.role, self.model, in_tokens, out_tokens, cost,
        )
        log.debug(
            "bedrock.cost.tick role=%s in_tokens=%d out_tokens=%d "
            "estimated_cost_usd=%.4f daily_total_usd=%.4f cap_usd=%.2f",
            self.role,
            in_tokens,
            out_tokens,
            cost,
            new_total,
            cap,
        )
        if new_total > cap:
            log.error(
                "Bedrock daily spend cap exceeded ($%.2f of $%.2f); "
                "aborting Bedrock call",
                new_total,
                cap,
            )
            raise BedrockBudgetExceeded(
                f"Bedrock daily spend cap exceeded (${new_total:.2f} of ${cap:.2f})"
            )

    # ── Retry helper ──

    def _call_with_retry(self, fn, *args, _log=None, _cancel_check=None, **kwargs):
        """Wrap a bedrock_api call with bounded retry on 5xx / timeouts.

        Emits ``backend.retry.attempted`` per plan § 15.75. Respects
        ``_cancel_check`` between attempts — a user double-escape cuts the
        retry loop cleanly instead of waiting out the backoff.
        """
        import requests as _requests

        log = _log or logging.getLogger("llm_backend")
        max_retries = int(self._cfg.get("max_retries", 3))
        base_delay = float(self._cfg.get("retry_base_delay_seconds", 1.0))
        backoff = float(self._cfg.get("retry_backoff", 2.0))

        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            if _cancel_check:
                _cancel_check()
            try:
                return fn(*args, **kwargs)
            except _requests.exceptions.HTTPError as e:
                status = getattr(e.response, "status_code", 0)
                if status < 500 or attempt >= max_retries:
                    raise
                last_exc = e
            except (
                _requests.exceptions.Timeout,
                _requests.exceptions.ConnectionError,
                TimeoutError,  # run 141: bedrock_api.poll() raises the
                               # built-in TimeoutError (not requests'). Catch
                               # both so gateway poll exhaustion triggers the
                               # retry loop instead of crashing the agent.
            ) as e:
                if attempt >= max_retries:
                    # Issue #864: on final timeout, check whether quota exhaustion
                    # caused the gateway's 404-forever silent failure.  Force a
                    # fresh probe (bypass the 5-min cache) and re-raise as
                    # BedrockBudgetExceeded for a clear error instead of TimeoutError.
                    self._usage_cache_time = 0.0
                    if self._get_cached_usage_pct() >= 100.0:
                        raise BedrockBudgetExceeded(
                            f"Monthly token quota exceeded "
                            f"(used_pct={self._cached_usage_pct:.1f}%). "
                            "Gateway will not respond until quota resets."
                        ) from e
                    raise
                last_exc = e

            delay = base_delay * (backoff ** attempt)
            log.warning(
                "backend.retry.attempted backend=bedrock attempt=%d/%d "
                "error=%s delay=%.1fs",
                attempt + 1,
                max_retries,
                last_exc,
                delay,
            )
            if _cancel_check:
                _cancel_check()
            time.sleep(delay)
        assert last_exc is not None  # pragma: no cover
        raise last_exc

    # ── Issue #405 — rotation between calls ──

    def _rotate_to_next_entry(self, exc: BaseException, log) -> bool:
        """Mark the current store entry ``down`` and swap to the next ``up``.

        Returns True if rotation succeeded (``self._api`` is now pointed at
        a fresh URL/key and the caller can retry). Returns False if there's
        no other ``up`` entry — caller re-raises and the outer
        main→summary→llamacpp failover takes over (criterion 4).
        """
        try:
            import bedrock_store
        except Exception:
            return False
        cur_name = getattr(self, "_store_entry_name", None)
        # No-op when running off env vars (back-compat path) — the store
        # has nothing to rotate to.
        if not cur_name:
            return False
        err_summary = bedrock_store.summarize_error(exc)
        bedrock_store.mark_status(
            cur_name,
            status=bedrock_store.STATUS_DOWN,
            last_error=err_summary,
        )
        try:
            new_url, new_key, new_name = bedrock_store.select_credentials(
                env_url=os.environ.get("BEDROCK_API_URL", ""),
                env_key=os.environ.get("BEDROCK_API_KEY", ""),
            )
        except LookupError:
            return False
        if not new_name or new_name == cur_name:
            # Either we fell back to env (no name) or there's no sibling
            # entry to swap to.
            return False
        new_url = new_url.rstrip("/")
        log.info(
            "bedrock.rotate from=%s to=%s reason=%s",
            cur_name, new_name, type(exc).__name__,
        )
        # Rebuild the underlying API client. ``stream_chat`` is the only
        # caller that holds an active conversation id, and per criterion 5
        # we never rotate mid-stream — so it's safe to drop ``_active_conv_id``.
        from bedrock_api import BedrockChatAPI
        self.api_url = new_url
        self.base_url = new_url
        self._store_entry_name = new_name
        self._api = BedrockChatAPI(
            {
                "api_url": new_url,
                "api_key": new_key,
                "origin": self._cfg.get("origin", "http://localhost:8000"),
                "model": self.model,
                "poll_interval": self._cfg.get("poll_interval", 0.3),
                "poll_backoff": self._cfg.get("poll_backoff", 1.5),
                "poll_max_interval": self._cfg.get("poll_max_interval", 5.0),
                "poll_timeout": self._cfg.get("poll_timeout", 180),
            }
        )
        self._active_conv_id = None
        return True

    # ── Summary-path: single-shot completion ──

    def complete(
        self,
        *,
        prompt: str,
        gen_params: dict | None = None,
        cancel_check: Callable[[], None] | None = None,
        timeout: float = 120,
    ) -> str:
        """Non-streaming single-shot completion. Returns the extracted text.

        ``gen_params`` overrides are merged with ``_base_inference_params``
        and forwarded to the gateway. The ``timeout`` argument is accepted
        for signature parity; the gateway poll uses its own ``poll_timeout``
        from config.
        """
        gp = gen_params or {}
        _caller_overrides = {**self._base_inference_params, **gp}
        _inference_params = _build_inference_params(self.model, _caller_overrides)
        # Summary path: always clamp maxTokens to 1024
        if _inference_params:
            _inference_params["maxTokens"] = min(_inference_params.get("maxTokens", 1024), 1024)
        else:
            _inference_params = {"maxTokens": 1024}

        log = logging.getLogger("llm_backend")

        # Issue #864: proactive quota check (same guard as stream_chat).
        usage_pct = self._get_cached_usage_pct()
        if usage_pct >= 100.0:
            raise BedrockBudgetExceeded(
                f"Monthly token quota exceeded (used_pct={usage_pct:.1f}%). "
                "Gateway will not respond until quota resets."
            )

        t0 = time.monotonic()
        ok = False
        try:
            try:
                msg = self._call_with_retry(
                    self._api.send_and_wait,
                    prompt,
                    _log=log,
                    _cancel_check=cancel_check,
                    cancel_check=cancel_check,
                    inference_params=_inference_params,
                )
            except (
                TimeoutError,
                BedrockBudgetExceeded,
                requests.exceptions.HTTPError,
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
            ) as e:
                # Issue #405 criterion 4 — rotate once on failure.
                if self._rotate_to_next_entry(e, log):
                    msg = self._call_with_retry(
                        self._api.send_and_wait,
                        prompt,
                        _log=log,
                        _cancel_check=cancel_check,
                        cancel_check=cancel_check,
                        inference_params=_inference_params,
                    )
                else:
                    raise
            text = self._api.extract_text(msg)
            ok = True
            # Record cost only on a successful call — keeps failed calls
            # from counting toward the cap.
            self._record_and_check_cost(prompt, text, log)
            return text
        finally:
            latency_ms = int((time.monotonic() - t0) * 1000)
            log.info(
                "backend.complete.latency_ms backend=%s model=%s role=%s "
                "latency_ms=%d ok=%s",
                self.kind,
                self.model,
                self.role,
                latency_ms,
                ok,
            )

    # ── Main-path: streaming chat via dev-mode prompt stuffing ──

    def stream_chat(
        self,
        *args,
        **kwargs,
    ):
        """Main streaming path (plan § 8). Returns an iterator of
        OpenAI-shape delta dicts.

        The agent's existing main loop expects the signature
        ``stream_chat(log, json=body, stream=True, timeout=...)`` and
        consumes the return value via ``iter_lines()``. For Bedrock
        we need a richer signature (``messages`` + ``tools`` separately)
        plus ``cancel_check``, so we accept kwargs flexibly:

          - ``messages``: OpenAI-shape message list.
          - ``tools``: OpenAI-shape tool list (or None).
          - ``gen_params``: dict overrides for inference params.
          - ``cancel_check``: callable.
          - ``log``: logger (positional or keyword).

        For convenience when wired through ``_llm_request`` (which passes
        ``json=body``), we also parse ``messages``/``tools`` out of the
        body dict.
        """
        # Lazy import — avoids a circular import during module load and
        # keeps the tool-parse machinery tree-shakeable for llamacpp-only
        # sessions.
        from dev_mode_prompt import (
            build_dev_prompt,
            is_truncated,
            parse_dev_response,
        )

        log = kwargs.pop("log", None)
        if log is None and args:
            log = args[0]
            args = args[1:]
        if log is None:
            log = logging.getLogger("llm_backend")

        body = kwargs.pop("json", None)
        messages = kwargs.pop("messages", None)
        tools = kwargs.pop("tools", None)
        cancel_check = kwargs.pop("cancel_check", None)

        if body is not None:
            if messages is None:
                messages = body.get("messages")
            if tools is None:
                tools = body.get("tools")

        gen_params_kwarg = kwargs.pop("gen_params", None) or {}
        _caller_overrides = {
            **self._base_inference_params,
            **_extract_gen_params_from_body(body),
            **gen_params_kwarg,
        }
        _inference_params = _build_inference_params(self.model, _caller_overrides)

        prompt = build_dev_prompt(messages or [], tools)

        # Issue #864: proactive quota check — fail fast if monthly cap exceeded.
        # The gateway returns 200+ID but then 404-forever when quota is gone,
        # causing a 180s TimeoutError instead of an immediate failure.
        usage_pct = self._get_cached_usage_pct()
        if usage_pct >= 100.0:
            raise BedrockBudgetExceeded(
                f"Monthly token quota exceeded (used_pct={usage_pct:.1f}%). "
                "Gateway will not respond until quota resets."
            )

        t0 = time.monotonic()
        conv_id: str | None = None
        full_text = ""
        MAX_CONTINUATIONS = 3
        for attempt in range(1 + MAX_CONTINUATIONS):
            if cancel_check:
                cancel_check()

            if attempt == 0:
                send_text = prompt
                # CICD 358: Reuse existing conversation if available
                if self._active_conv_id is None:
                    self._session_conv_count += 1

                msg, conv_id = self._call_with_retry(
                    self._api.send_and_wait_conv,
                    send_text,
                    conversation_id=self._active_conv_id,
                    cancel_check=cancel_check,
                    _log=log,
                    _cancel_check=cancel_check,
                    inference_params=_inference_params,
                )
                self._active_conv_id = conv_id
            else:
                tail = full_text[-200:]
                send_text = (
                    "Your response was truncated. Continue from exactly "
                    f"where you left off. Last part ended with:\n...{tail}"
                )
                msg, conv_id = self._call_with_retry(
                    self._api.send_and_wait_conv,
                    send_text,
                    conversation_id=conv_id,
                    cancel_check=cancel_check,
                    _log=log,
                    _cancel_check=cancel_check,
                    inference_params=_inference_params,
                )
                self._active_conv_id = conv_id

            piece = self._api.extract_text(msg)
            full_text += piece

            if not is_truncated(full_text):
                if attempt > 0:
                    log.info(
                        "bedrock.truncation_recovery.succeeded attempts=%d "
                        "conversationId=%s",
                        attempt,
                        conv_id,
                    )
                break
            if attempt == 0:
                log.warning(
                    "bedrock.truncation_recovery.attempted conversationId=%s "
                    "tail_chars=%d",
                    conv_id,
                    len(full_text),
                )
        else:
            log.error(
                "bedrock.truncation_recovery.exhausted conversationId=%s "
                "attempts=%d",
                conv_id,
                MAX_CONTINUATIONS,
            )

        narrative, tool_calls = parse_dev_response(full_text)
        log.info(
            "bedrock.tool_parse.result parsed_calls=%d stripped_chars=%d",
            len(tool_calls),
            len(narrative),
        )

        # Record cost for the whole streamed exchange.
        try:
            self._record_and_check_cost(prompt, full_text, log)
        except BedrockBudgetExceeded:
            # Cap-exceeded on stream still reports the latency log.
            latency_ms = int((time.monotonic() - t0) * 1000)
            log.info(
                "backend.stream_chat.latency_ms backend=%s model=%s role=%s "
                "latency_ms=%d deltas=0 ok=False",
                self.kind,
                self.model,
                self.role,
                latency_ms,
            )
            raise

        deltas = (1 if narrative else 0) + len(tool_calls)
        latency_ms = int((time.monotonic() - t0) * 1000)
        log.info(
            "backend.stream_chat.latency_ms backend=%s model=%s role=%s "
            "latency_ms=%d deltas=%d ok=True",
            self.kind,
            self.model,
            self.role,
            latency_ms,
            deltas,
        )

        def _iter():
            if narrative:
                yield {"choices": [{"delta": {"content": narrative}}]}
            for tc in tool_calls:
                yield {"choices": [{"delta": {"tool_calls": [tc]}}]}

        return _iter()



import foundry_retry_utils
import os
import logging
import time
from anthropic import AnthropicFoundry

class FoundryBackend:
    """Azure AI Foundry backend using AnthropicFoundry client."""

    kind = "foundry"

    def __init__(self, cfg: dict):
        self._cfg = cfg
        self.role = cfg.get("role", "main")
        
        # Resolve credentials based on role (main/summary)
        role_upper = self.role.upper()
        endpoint = cfg.get("api_url") or os.environ.get(f"AZURE_FOUNDRY_ENDPOINT_{role_upper}", "")
        api_key = cfg.get("api_key") or os.environ.get(f"AZURE_FOUNDRY_API_KEY_{role_upper}", "")

        if not endpoint or not api_key:
            raise ConfigError(
                f"Foundry backend requires endpoint and key "
                f"(either in config.json or AZURE_FOUNDRY_ENDPOINT_{role_upper}/AZURE_FOUNDRY_API_KEY_{role_upper})"
            )

        # Azure portal gives the full messages URL; AnthropicFoundry expects
        # the base URL only — strip any trailing /v1/messages path.
        for _suffix in ("/v1/messages", "/v1"):
            if endpoint.rstrip("/").endswith(_suffix):
                endpoint = endpoint.rstrip("/")[: -len(_suffix)]
                break

        self.api_url = endpoint
        self.base_url = endpoint  # alias expected by agent.py display/logging
        self.api_key = api_key
        self.model = os.environ.get(f"AZURE_FOUNDRY_MODEL_{role_upper}", "") or cfg.get("model", "")
        if not self.model:
            raise ConfigError("Foundry backend requires a model deployment name.")

        self.client = AnthropicFoundry(
            api_key=self.api_key,
            base_url=self.api_url,
        )
        self.enabled = cfg.get("enabled", True)

    def health(self) -> tuple[bool, str]:
        return True, self.api_url

    def detect_ctx_size(self) -> int:
        return 200000

    def list_models(self) -> list[str]:
        return [self.model]
    def complete(self, prompt: str, log=None) -> str:
        log = log or logging.getLogger("llm_backend")
        t0 = time.monotonic()
        
        def _do_call():
            return self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )

        try:
            response = foundry_retry_utils.retry_on_429(_do_call, log)
            text = response.content[0].text
            ok = True
        except Exception as e:
            log.error("foundry.complete.error: %s", e)
            text = ""
            ok = False
        finally:
            latency_ms = int((time.monotonic() - t0) * 1000)
            log.info(
                "backend.complete.latency_ms backend=%s model=%s role=%s latency_ms=%d ok=%s",
                self.kind, self.model, self.role, latency_ms, ok
            )
        return text

    @staticmethod
    def _to_anthropic_messages(openai_messages: list[dict]) -> tuple[str | None, list[dict]]:
        """Convert OpenAI-format messages to Anthropic format.
        Returns (system_prompt_or_None, anthropic_messages).

        Handles orphaned tool_results (produced by context compression that
        removes the preceding assistant+tool_use messages): any tool_result
        whose tool_use_id has no matching tool_use in the conversation is
        silently dropped to prevent 400 errors from the Anthropic API.
        """
        system = None
        out: list[dict] = []
        seen_tool_use_ids: set[str] = set()

        for msg in openai_messages:
            role = msg.get("role")
            content = msg.get("content")

            if role == "system":
                system = content
                continue

            if role in ("user", "assistant"):
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    blocks: list[dict] = []
                    # Preserve any narrative text alongside tool_use blocks
                    if content:
                        blocks.append({"type": "text", "text": content})
                    for tc in tool_calls:
                        tid = tc["id"]
                        seen_tool_use_ids.add(tid)
                        blocks.append({
                            "type": "tool_use",
                            "id": tid,
                            "name": tc["function"]["name"],
                            "input": json.loads(tc["function"]["arguments"] or "{}"),
                        })
                    out.append({"role": "assistant", "content": blocks})
                else:
                    out.append({"role": role, "content": content or ""})

            elif role == "tool":
                tid = msg.get("tool_call_id", "")
                if tid not in seen_tool_use_ids:
                    # Orphaned result — its tool_use was compressed away; skip
                    # to avoid "unexpected tool_use_id in tool_result" 400 error.
                    continue
                result_block = {
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": content or "",
                }
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(result_block)
                else:
                    out.append({"role": "user", "content": [result_block]})

        return system, out

    @staticmethod
    def _to_anthropic_tools(openai_tools: list[dict] | None) -> list[dict] | None:
        """Convert OpenAI-format tool definitions to Anthropic format."""
        if not openai_tools:
            return None
        result = []
        for tool in openai_tools:
            if tool.get("type") == "function":
                fn = tool["function"]
                result.append({
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                })
        return result or None

    def stream_chat(self, *args, **kwargs):
        log = kwargs.pop("log", None)
        if log is None and args:
            log = args[0]
        if log is None:
            log = logging.getLogger("llm_backend")

        body = kwargs.pop("json", None) or {}
        openai_messages = kwargs.pop("messages", None) or body.get("messages", [])
        openai_tools = kwargs.pop("tools", None) or body.get("tools")
        max_tokens = body.get("max_tokens", 4096)

        system, anthropic_messages = self._to_anthropic_messages(openai_messages)
        anthropic_tools = self._to_anthropic_tools(openai_tools)

        create_kwargs: dict = dict(
            model=self.model,
            max_tokens=max_tokens,
            messages=anthropic_messages,
        )
        if system:
            create_kwargs["system"] = system
        if anthropic_tools:
            create_kwargs["tools"] = anthropic_tools

        t0 = time.monotonic()
        ok = False
        narrative = ""
        tool_calls: list[dict] = []

        def _do_call():
            return self.client.messages.create(**create_kwargs)

        try:
            response = foundry_retry_utils.retry_on_429(_do_call, log)
            for i, block in enumerate(response.content):
                if block.type == "text":
                    narrative += block.text
                elif block.type == "tool_use":
                    tool_calls.append({
                        "index": i,
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": json.dumps(block.input),
                        },
                    })
            ok = True
        except Exception as e:
            log.error("foundry.stream_chat.error: %s", e)
        finally:
            deltas = (1 if narrative else 0) + len(tool_calls)
            latency_ms = int((time.monotonic() - t0) * 1000)
            log.info(
                "backend.stream_chat.latency_ms backend=%s model=%s role=%s latency_ms=%d deltas=%d ok=%s",
                self.kind, self.model, self.role, latency_ms, deltas, ok,
            )

        if narrative:
            yield {"choices": [{"delta": {"content": narrative}}]}
        for tc in tool_calls:
            yield {"choices": [{"delta": {"tool_calls": [tc]}}]}
# ── Factory ────────────────────────────────────────────────────────────


def build_backend(cfg: dict) -> Backend:
    """Instantiate a concrete backend from a config dict.

    The dict must carry a ``kind`` key. ``kind="llamacpp"`` returns a
    ``LlamacppBackend``; ``kind="bedrock"`` returns a ``BedrockBackend``
    (raises ``ConfigError`` when ``BEDROCK_API_URL`` / ``BEDROCK_API_KEY``
    are missing — see plan § 12 / D8 / D11). Any other value raises
    ``ValueError``.
    """
    kind = cfg.get("kind", "llamacpp")
    if kind == "llamacpp":
        return LlamacppBackend(cfg)
    if kind == "bedrock":
        return BedrockBackend(cfg)
    if kind == "foundry":
        return FoundryBackend(cfg)
    raise ValueError(f"Unknown backend kind: {kind!r}")
