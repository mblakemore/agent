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

import logging
import random
import time
from typing import Callable, Iterator, Protocol

import requests


# ── Internal retry / exception scaffolding (parallels agent.py defaults) ──


class ContextOverflowError(Exception):
    """Raised when the server returns persistent 500s likely due to context overflow."""

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


# ── Factory ────────────────────────────────────────────────────────────


def build_backend(cfg: dict) -> Backend:
    """Instantiate a concrete backend from a config dict.

    The dict must carry a ``kind`` key. For Phase 1, only ``kind="llamacpp"``
    returns a usable backend; ``kind="bedrock"`` raises ``NotImplementedError``
    so the dispatch path can be tested even before Phase 2 lands. Any other
    value raises ``ValueError``.
    """
    kind = cfg.get("kind", "llamacpp")
    if kind == "llamacpp":
        return LlamacppBackend(cfg)
    if kind == "bedrock":
        raise NotImplementedError(
            "Bedrock backend not yet implemented — Phase 2 "
            "(see plan/bedrock-integration.md § 16)"
        )
    raise ValueError(f"Unknown backend kind: {kind!r}")
