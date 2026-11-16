"""The summary of what left the window: the prompt, the request to the summary
backend, the condense pass, the background summarizer, the fold when the window head
moves, the idle watcher, and the compression of repeated tool results.

The module owns SUMMARY_THRESHOLD, SUMMARY_MAX_CHARS, SUMMARY_IDLE and the
TOOL_RESULT_COMPRESS_* knobs; agent.py keeps the old names as aliases and reads the
live values here. What it borrows from agent.py comes in through `configure` as DEPS:
callables read at call time, because a failover swaps the summary backend mid-run and
the tests patch agent's names.
"""
import logging
import os
import re
import threading
import time
from copy import deepcopy

import requests

import context_window as _cw
import skills as _skills
import telemetry
from llm_backend import BedrockBudgetExceeded

DEPS = {
    "summary_backend": lambda: None,            # the backend summaries go to (swapped by a failover)
    "main_backend": lambda: None,               # the fallback when the summary backend fails
    "config": lambda: {},                       # the live config dict
    "base_url": lambda: None,                   # the main model's base_url, for the llamacpp fallback
    "cache_req": lambda: {},                    # the cache record: when the last request went out
    "emit": lambda *a, **k: None,               # UI events
    "trigger_failover": lambda log, role, cause=None: False,
    "build_backend": None,                      # llm_backend.build_backend, for a transient override
    "cache_lifetime_key": lambda backend=None: None,
    "cache_lifetime_estimate": lambda record, key: (None, None),
    "cache_lifetimes": lambda: {},
    "cache_collapse_frac": lambda: 0.0,
    "prompt_caching_backend": lambda: None,
    "cicd_facts": lambda: (None, None, None, None, None),   # worktree, edited files, issue, PR, branch
}
SUMMARY_THRESHOLD = 5       # messages out of the window before a summary is due
SUMMARY_MAX_CHARS = 4000    # a longer summary is condensed


def configure(threshold=None, max_chars=None, **deps):
    """Set the knobs and the environment (any DEPS key)."""
    global SUMMARY_THRESHOLD, SUMMARY_MAX_CHARS
    unknown = set(deps) - set(DEPS)
    if unknown:
        raise TypeError("unknown summarize deps: %s" % ", ".join(sorted(unknown)))
    DEPS.update(deps)
    if threshold is not None:
        SUMMARY_THRESHOLD = threshold
    if max_chars is not None:
        SUMMARY_MAX_CHARS = max_chars


# ── Config readers ────────────────────────────────────────────────────


def sane_summary_threshold(ctx_cfg, log=None):
    """The summary threshold as a MESSAGE count. Setup wizards before 2026-10-29 wrote it
    in characters (ctx_chars * 0.6, e.g. 432000), which the run loop compares to the
    number of messages that have fallen out of the window — no calibrated session ever
    summarised. A value above the message cap cannot be meant as messages, so it is
    replaced by a fifth of the cap and the replacement is logged with the config key."""
    cap = int(ctx_cfg.get("max_context_messages") or 30)
    try:
        value = int(ctx_cfg.get("summary_threshold", 5))
    except (TypeError, ValueError):
        value = 5
    if value <= cap:
        return value
    fixed = max(5, cap // 5)
    (log or logging.getLogger("agent")).warning(
        "context.summary_threshold=%d is larger than max_context_messages=%d (a chars value "
        "from an older /setup calibrate); using %d messages. Re-run /setup calibrate to fix the config.",
        value, cap, fixed)
    return fixed


def summary_wait_s():
    """summary.wait_s: None when a head move with no summary ready summarizes in front
    of the request; otherwise the seconds to wait for a running summary, 0 or more."""
    v = DEPS["config"]().get("summary", {}).get("wait_s")
    if v is None or v == "":
        return None
    try:
        return max(0.0, float(v))
    except (TypeError, ValueError):
        return None


def summary_ahead_frac():
    """summary.ahead: the window fill (a fraction of the budget, or of the message
    cap) at which the summary of what the next eviction will drop is started; 0 off."""
    try:
        v = float(DEPS["config"]().get("summary", {}).get("ahead", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    return v if 0.0 < v < 1.0 else 0.0


# ── The summary request ───────────────────────────────────────────────

def format_for_summary(messages):
    """Format messages into a readable transcript for the summarizer.

    Preserves error messages and tool results more fully than regular content,
    since errors are critical for avoiding repeated failed approaches.
    """
    parts = []
    for m in messages:
        role = m["role"].upper()
        if role == "TOOL":
            name = m.get("name", "?")
            content = m.get("content", "")
            # Preserve error messages fully (up to 800 chars) — they're critical context
            is_error = content.startswith("Error") or "Error:" in content[:50]
            max_len = 800 if is_error else 500
            if len(content) > max_len:
                content = content[:max_len] + "..."
            parts.append(f"TOOL RESULT ({name}): {content}")
        elif role == "ASSISTANT":
            text = m.get("content", "")
            tool_calls = m.get("tool_calls", [])
            if text:
                if len(text) > 600:
                    text = text[:600] + "..."
                parts.append(f"ASSISTANT: {text}")
            for tc in tool_calls:
                fn_info = tc.get("function", {})
                fn_name = fn_info.get("name", "?")
                args = fn_info.get("arguments", "")
                # For file writes, extract and preserve the path before truncating
                if fn_name == "file" and '"write"' in args:
                    _path_m = re.search(r'"path"\s*:\s*"([^"]+)"', args)
                    if _path_m:
                        parts.append(f"ASSISTANT called file(action=write, path={_path_m.group(1)})")
                        continue
                if len(args) > 200:
                    args = args[:200] + "..."
                parts.append(f"ASSISTANT called {fn_name}({args})")
        else:
            content = m.get("content", "")
            if len(content) > 800:
                content = content[:800] + "..."
            parts.append(f"{role}: {content}")
    return "\n".join(parts)


def summary_request(prompt, base_url=None, model=None, **kwargs):
    """POST a summary prompt to the summary backend. Returns summary text.
    
    Plan task 1.4: routes through ``_summary_backend.complete(prompt=prompt)``.
    The ``base_url`` / ``model`` parameters are preserved for signature
    compatibility (see ``tests/test_summary_request_signature.py``). They are
    only honored when ``_summary_backend.kind == "llamacpp"``; for non-llamacpp
    backends a DEBUG log line is emitted and the backend's own config wins.
    """
    # Use default logger if none provided
    log = kwargs.get("log")
    if log is None:
        import logging
        log = logging.getLogger("agent")

    try:
        backend = DEPS["summary_backend"]()
        if backend.kind == "llamacpp" and (base_url or model):
            # Build a transient backend with the overrides
            override_cfg = dict(backend._cfg)
            if base_url:
                override_cfg["base_url"] = base_url
            if model:
                override_cfg["model"] = model
            transient = DEPS["build_backend"]({**override_cfg, "kind": "llamacpp"})
            return transient.complete(prompt=prompt)
        if backend.kind != "llamacpp" and (base_url or model):
            log.debug(
                "summary_request ignoring base_url/model override — "
                "summary backend kind=%s",
                backend.kind,
            )
        return backend.complete(prompt=prompt)
    except Exception as e:
        raise e


def condense_summary(text, log=None, quiet=False):
    """Re-summarize if text exceeds the character cap. Preserves all info.
    `quiet` skips the notices: the worker thread condenses its own result there,
    and nothing on the main thread is waiting for it."""
    if len(text) <= SUMMARY_MAX_CHARS:
        return text
    if log:
        log.info("Summary too long (%d chars, limit %d) — condensing", len(text), SUMMARY_MAX_CHARS)
    if not quiet:
        DEPS["emit"]("on_summary_start", 0)
    prompt = (
        f"The following summary is too long ({len(text)} chars). "
        f"Rewrite it in under {SUMMARY_MAX_CHARS // 2} characters. "
        f"Keep ALL file paths, decisions, failures, and completed actions. "
        f"Remove filler and verbose descriptions. Be maximally terse.\n\n"
        f"{text}"
    )
    try:
        condensed = summary_request(prompt)
        if len(condensed) > SUMMARY_MAX_CHARS:
            # Model didn't comply — hard truncate as last resort
            if log:
                log.warning("Condensed summary still too long (%d chars), truncating", len(condensed))
            condensed = condensed[:SUMMARY_MAX_CHARS].rsplit('\n', 1)[0] + "\n[...truncated]"
        if log:
            log.info("Condensed summary: %d → %d chars", len(text), len(condensed))
        if not quiet:
            DEPS["emit"]("on_notice", "info", f"[summary condensed: {len(text)} → {len(condensed)} chars]")
        return condensed
    except Exception as e:
        if log:
            log.error("Condense failed (%s), truncating as fallback", e)
        return text[:SUMMARY_MAX_CHARS].rsplit('\n', 1)[0] + "\n[...truncated]"


def build_summary_prompt(old_summary, new_messages):
    """Build the summary prompt from old summary + new messages."""
    transcript = format_for_summary(new_messages)

    structure_instruction = (
        "Write a CONCISE summary (under 400 words) with these sections:\n"
        "GOAL: One line — the objective.\n"
        "DONE: Bullet list of completed actions with file paths. Critical — prevents re-doing work.\n"
        "FAILED: Approaches that failed and why (one line each).\n"
        "STATE: Current state in 1-2 sentences.\n"
        "NEXT: The single next action.\n"
        "Be terse. Use file paths, not descriptions. No filler.\n\n"
        "CRITICAL PRESERVATION RULES:\n"
        "- Always preserve EXACT file paths and line numbers that were modified.\n"
        "- Always preserve the specific code change (e.g. 'added sys.stdout.reconfigure() to agent.py:main()').\n"
        "- Always preserve git branch names, commit hashes, and PR numbers.\n"
        "- Always preserve GitHub issue numbers and whether they were opened, closed, or commented on.\n"
        "- Always preserve installed dependencies and environment setup steps.\n"
        "- Always preserve metric baselines and measurements.\n"
        "- Never summarize these as 'made changes' or 'worked on the issue' — be specific."
    )

    # Inject CICD ground-truth state so the summarizer preserves it verbatim
    (_cicd_worktree_path, _cicd_edited_files, _cicd_issue_number,
     _cicd_pr_number, _cicd_branch) = DEPS["cicd_facts"]()
    cicd_facts = []
    if _cicd_worktree_path:
        cicd_facts.append(f"Worktree path: {_cicd_worktree_path}")
    if _cicd_edited_files:
        cicd_facts.append(f"Files already edited: {', '.join(sorted(_cicd_edited_files))}")
    if _cicd_issue_number:
        cicd_facts.append(f"Issue: #{_cicd_issue_number}")
    if _cicd_pr_number:
        cicd_facts.append(f"PR: #{_cicd_pr_number}")
    if _cicd_branch:
        cicd_facts.append(f"Branch: {_cicd_branch}")
    if cicd_facts:
        structure_instruction += (
            "\n\nGROUND TRUTH (include verbatim in your summary under STATE):\n"
            + "\n".join(f"- {f}" for f in cicd_facts)
        )
    # A skill loaded among these messages leaves the window with them: the summary
    # says it was loaded, so the model knows it once had those instructions.
    _skill_names = _skills.skills_in(new_messages)
    if _skill_names:
        structure_instruction += (
            "\n\nUnder STATE, also say that these skills (instruction files) were loaded and "
            "are no longer in the conversation; each can be loaded again with "
            "tool_search skill:NAME: " + ", ".join(_skill_names)
        )

    if old_summary:
        return (
            f"Here is the previous summary of the conversation so far:\n\n"
            f"{old_summary}\n\n"
            f"Here are the new messages since that summary:\n\n"
            f"{transcript}\n\n"
            f"Write an updated summary that combines the previous summary with the new messages.\n\n"
            f"{structure_instruction}"
        )
    return (
        f"Here is a conversation transcript:\n\n"
        f"{transcript}\n\n"
        f"Write a concise summary.\n\n"
        f"{structure_instruction}"
    )


# ── Repeated tool results ─────────────────────────────────────────────

# Number of same-tool occurrences in history before older ones are compressed.
TOOL_RESULT_COMPRESS_AFTER = 3
# Don't compress results shorter than this — short results are cheap to keep.
TOOL_RESULT_COMPRESS_MIN   = 200
# Each model summary is a blocking round trip inside the tool loop. After a resume the whole
# backlog of old results qualifies at once, and one summary call per result froze the session
# for minutes after its first command. Beyond this many per pass, use the local head+tail.
TOOL_RESULT_COMPRESS_MODEL_MAX = 2


def local_compress(content: str) -> str:
    """Head+tail compression with no model call."""
    lines = content.strip().split("\n")
    if len(lines) <= 4:
        return f"[compressed: {content[:300]}]"
    head = "\n".join(lines[:2])
    tail = "\n".join(lines[-2:])
    return f"[compressed: {head}\n... ({len(lines)} lines) ...\n{tail}]"


def summarize_for_compression(content: str, func_name: str, log) -> str:
    """Compress one tool result to key facts.  Tries summary backend; falls back to head+tail."""
    try:
        prompt = (
            f"Compress this {func_name} result to 1-3 lines. "
            f"Preserve all key values: progress percentages, step counts, "
            f"file paths, error messages, exit codes.\n\n{content[:3000]}"
        )
        summary = summary_request(prompt, log=log).strip()
        return f"[compressed by summary: {summary}]"
    except Exception as e:
        log.debug("summary compression unavailable (%s) — using head+tail", e)
        return local_compress(content)


# History rewrites are PROMPT-CACHE KILLERS. A server that caches the key-value state of the
# previous request reuses it only for the longest common PREFIX of the next one; an in-place
# rewrite at position p invalidates everything after p. Measured on a live run: this pass
# rewrote one earlier tool result per turn (five consecutive calls to the same tool), the
# server reported a 17% common prefix with its cache on every request, re-processed ~32k of a
# 39k-token prompt each turn (~60 s of prefill), and the run's stall guard cancelled the request
# at the moment prefill finished. So the pass runs only when the history is actually PRESSING
# on the context window, and then does everything it can in one batch — steady-state turns
# never touch the prefix. Compression under pressure also shrinks the history, which lowers the
# pressure, which is the hysteresis that keeps it from firing every turn.
TOOL_RESULT_COMPRESS_PRESSURE = float(os.environ.get("AGENT_COMPRESS_PRESSURE_FRAC", "0.70") or 0.70)


def history_pressure(conversation_history, start=0):
    """Estimated prompt tokens / context size, using the calibrated estimator (F3b). 0 when the
    context size is unknown, which reads as 'not under pressure' — the rewrite is the risky side.

    `start` is the index the SENT window begins at (the cache head on a caching backend).
    Measured without it: a 1,061-message stored history read as 3.4× the context against a
    0.70 gate, so the compression pass ran — and broke the cache — on nearly every turn,
    although the window actually sent was a third of the context."""
    try:
        ctx = float((DEPS["config"]().get("context") or {}).get("ctx_size") or 0)
    except Exception:  # noqa: BLE001
        ctx = 0.0
    if ctx <= 0:
        return 0.0
    window = conversation_history[start:] if start else conversation_history
    est = ((_cw.TOOLS_TOKENS or 0) + sum(_cw.estimate_tokens(m) for m in window)) \
        * float(_cw.TOKEN_CAL.get("ratio", 1.0) or 1.0)
    return est / ctx


def compress_repeated_tool_results(conversation_history: list, func_name: str, log, pressure=None,
                                    protect_from=None, local_only=False) -> list:
    """When the same tool appears TOOL_RESULT_COMPRESS_AFTER+ times in history,
    compress all but the most recent occurrence.  Targets polling patterns
    (e.g. exec_command(cat log) × N turns) that would otherwise fill the context
    window with near-identical large results.

    `pressure` (estimated prompt tokens / ctx_size) gates the pass: below
    TOOL_RESULT_COMPRESS_PRESSURE nothing is rewritten, because a rewrite costs a full
    prefill on a prompt-caching server (see the note above). None = ungated (legacy callers).

    `protect_from`: messages at or after this index are never rewritten — on a caching
    backend that is the sent window, whose bytes must stay a prefix of the next request.
    `local_only`: head+tail compression only, no blocking model round trip (for messages
    below the head, which are never sent again, a model summary buys nothing).

    Returns the indices that were rewritten (empty when nothing changed)."""
    if pressure is not None and pressure < TOOL_RESULT_COMPRESS_PRESSURE:
        return []
    indices = [
        i for i, m in enumerate(conversation_history)
        if m.get("role") == "tool" and m.get("name") == func_name
    ]
    if len(indices) < TOOL_RESULT_COMPRESS_AFTER:
        return []
    changed = []
    for idx in reversed(indices[:-1]):  # keep the most recent entry intact; newest first
        if protect_from is not None and idx >= protect_from:
            continue
        content = conversation_history[idx].get("content", "")
        if len(content) < TOOL_RESULT_COMPRESS_MIN:
            continue
        if content.startswith("[compressed"):
            continue
        if _skills.skills_of(conversation_history[idx]):
            continue   # a skill's text is instructions; it leaves by trimming, not in pieces
        conversation_history[idx]["content"] = (
            summarize_for_compression(content, func_name, log)
            if (not local_only and len(changed) < TOOL_RESULT_COMPRESS_MODEL_MAX)
            else local_compress(content))
        conversation_history[idx].pop("_tokens", None)   # token_utils caches per message
        changed.append(idx)
    if changed:
        log.info("history compression: %d older %s result(s) compressed", len(changed), func_name)
    return changed


# ── The summary ───────────────────────────────────────────────────────

def generate_summary(old_summary, new_messages, log):
    """Call the LLM to produce an updated conversation summary.

    The summary prompt explicitly preserves decisions, outcomes, and failed
    approaches to prevent the agent from repeating mistakes.

    Tries the dedicated summary endpoint first (CPU model on port 8082),
    falls back to the main model on connection failure.
    """
    prompt = build_summary_prompt(old_summary, new_messages)
    log.info("Generating conversation summary...")

    summary_cfg = DEPS["config"]()["summary"]
    summary_url = summary_cfg["base_url"]

    # Guard: If summary is disabled or no base_url is provided, skip attempt.
    if not summary_cfg["enabled"] or not summary_url:
        return old_summary

    def _fallback_to_main(reason_exc):
        """Route the summary through the main backend after a summary failure.

        The legacy path called ``summary_request(prompt, base_url=BASE_URL, ...)``
        which only re-homes to a llamacpp override and is a no-op when the
        summary backend is non-llamacpp (Bedrock). Go directly to the
        ``_main_backend.complete()`` so the fallback actually runs through a
        different backend kind when the primary is Bedrock and e.g. the
        daily cost cap tripped. See plan section 15 rollback / section 6.5 guardrail.
        """
        log.warning(
            "Summary failed on %s backend (%s); falling back to main model",
            getattr(DEPS["summary_backend"](), "kind", "?"),
            reason_exc,
        )
        try:
            summary = DEPS["main_backend"]().complete(prompt=prompt)
            log.info("SUMMARY (fallback): %s", summary)
            return summary
        except Exception as e2:
            log.error("Summary fallback also failed: %s", e2)
            return old_summary or ""

    _summary_t0 = time.monotonic()
    try:
        # Try dedicated summary endpoint first — via summary_request, which
        # now routes through _summary_backend.complete (plan task 1.4).
        summary = summary_request(prompt, log=log)
        telemetry.record_summary()
        log.info("SUMMARY: %s", summary)
        if telemetry.verbose_enabled():
            telemetry.record_turn(
                role="summary",
                duration_s=time.monotonic() - _summary_t0,
                tool_calls=0,
                in_tokens=0,
                out_tokens=0,
                model=getattr(DEPS["summary_backend"](), "model", "")
                or DEPS["config"]().get("summary", {}).get("model", ""),
            )
        return summary
    except (requests.ConnectionError, requests.Timeout,
            TimeoutError, BedrockBudgetExceeded) as e:
        # BedrockBudgetExceeded lands here too: _trigger_failover refuses a
        # bedrock->bedrock swap on budget trips, so the cap case degrades to
        # _fallback_to_main — avoiding cascading context overflow from
        # missing summaries without moving spend to the other role's cap.
        if DEPS["trigger_failover"](log, 'summary', cause=e):
            log.info("Retrying summary with failover backend...")
            try:
                summary = summary_request(prompt, log=log)
                telemetry.record_summary()
                log.info("SUMMARY (retry): %s", summary)
                return summary
            except Exception as e2:
                log.error("Summary retry failed: %s", e2)
                return _fallback_to_main(e2)
        return _fallback_to_main(e)
    except Exception as e:
        # For any other summary-backend error, if the summary backend is
        # different from the main one, try the main backend before giving up.
        if getattr(DEPS["summary_backend"](), "kind", None) != getattr(
            DEPS["main_backend"](), "kind", None
        ):
            return _fallback_to_main(e)
        log.error("Summary generation failed: %s", e)
        telemetry.record_error(kind=type(e).__name__)
        return old_summary or ""


# ── Async summarizer ──────────────────────────────────────────────────

class AsyncSummarizer:
    """Background-thread summarizer targeting a separate (CPU) model endpoint.

    Usage:
        summarizer.kick(old_text, messages, up_to_idx)  # non-blocking
        if summarizer.harvest(summary_state):            # pick up result
            ...
        summarizer.drain()                               # block before checkpoint

    The worker hands over finished text: the condense pass, when the summary comes
    back over the character cap, runs on the worker too (measured on one deployment
    before this: 5 to 19 s on the main thread at harvest time, 21 times in six
    sessions). `kind` names why a summary was made (threshold, ahead, idle, late,
    resumed) and is in the summary.harvest log line.
    """

    def __init__(self, config, log):
        self._config = config
        self._log = log
        self._lock = threading.Lock()
        self._thread = None
        self._pending_result = None
        self._pending_up_to = None
        self._pending_kind = ""
        self._running = False

    def kick(self, old_summary_text, messages_snapshot, up_to_idx, kind="threshold"):
        """Start background summarization if not already running. True when started."""
        with self._lock:
            if self._running:
                return False
            self._running = True

        msgs = deepcopy(messages_snapshot)

        def _worker():
            try:
                prompt = build_summary_prompt(old_summary_text, msgs)
                # Try dedicated endpoint, fall back to main model.
                # Routes through summary_request which delegates to the
                # summary backend under the hood (plan task 1.4).
                try:
                    result = summary_request(prompt)
                except (requests.ConnectionError, requests.Timeout) as e:
                    self._log.warning("Async summary endpoint unavailable (%s), "
                                      "falling back to main model", e)
                    result = summary_request(
                        prompt,
                        base_url=DEPS["base_url"](),
                        model=self._config["llm"]["model"],
                    )
                self._log.info("ASYNC SUMMARY: %s", result)
                result = condense_summary(result, self._log, quiet=True)   # here, never on the main thread
                with self._lock:
                    self._pending_result = result
                    self._pending_up_to = up_to_idx
                    self._pending_kind = kind
            except Exception as e:
                self._log.error("Async summary failed: %s", e)
            finally:
                with self._lock:
                    self._running = False

        self._thread = threading.Thread(target=_worker, daemon=True)
        self._thread.start()
        return True

    def pending_up_to(self):
        """The edge (history index) of a finished summary not yet applied, or None."""
        with self._lock:
            return self._pending_up_to if self._pending_result is not None else None

    def seed(self, text, up_to, kind="resumed"):
        """Hold a finished summary, from a checkpoint, as if the worker had just made it."""
        with self._lock:
            self._pending_result = text
            self._pending_up_to = up_to
            self._pending_kind = kind

    def harvest(self, summary_state):
        """Apply a finished summary when it ends at or before the window's head
        (summary_state["_head"], set by every window build): the messages it covers
        have left the window, and the lead message is the only place they still are.
        One that ends past the head, made ahead of an eviction, is held until the head
        moves there: applying it earlier would rewrite the lead, and so the cached
        prefix, while the messages are still in the window. A held one rides along
        in summary_state["_pending"] so a checkpoint keeps it (see main's resume).
        Returns True if the state was updated."""
        with self._lock:
            if self._pending_result is None:
                return False
            head = summary_state.get("_head")
            if isinstance(head, int) and self._pending_up_to > head:
                summary_state["_pending"] = {"text": self._pending_result, "up_to": self._pending_up_to,
                                             "kind": self._pending_kind}
                return False
            summary_state["text"] = self._pending_result
            summary_state["up_to"] = self._pending_up_to
            # A new summary rewrites the lead message of the next request, i.e. one
            # expected cache miss. Harvests happen at several sites (turn start, the
            # stop paths), so the flag lives on the state and the cache.miss log pops it.
            summary_state["_lead_changed"] = True
            summary_state.pop("_pending", None)
            self._log.info("summary.harvest up_to=%d kind=%s", self._pending_up_to, self._pending_kind or "threshold")
            self._pending_result = None
            self._pending_up_to = None
            self._pending_kind = ""
            return True

    @property
    def is_running(self):
        with self._lock:
            return self._running

    def drain(self, timeout=None):
        """Block until pending summary completes (for checkpoint saves)."""
        if timeout is None:
            timeout = self._config["summary"]["max_wait_on_save"]
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def reset(self):
        """Discard any pending result (for /clear)."""
        with self._lock:
            self._pending_result = None
            self._pending_up_to = None
            self._pending_kind = ""


# ── When the window head moves ────────────────────────────────────────

def maybe_resummarize(conversation_history, summary_state, oldest_idx, log, force=False):
    """Check if enough messages have fallen out of the window to warrant a new summary."""
    unsummarized = oldest_idx - summary_state["up_to"]

    if not force and unsummarized < SUMMARY_THRESHOLD:
        return False

    new_messages = conversation_history[summary_state["up_to"]:oldest_idx]

    # Nothing new to summarize — keep existing summary intact
    if not new_messages:
        log.debug("Resummarize skipped: 0 new messages (up_to=%d, oldest_idx=%d)",
                  summary_state["up_to"], oldest_idx)
        return False

    DEPS["emit"]("on_summary_start", len(new_messages))
    summary = generate_summary(summary_state["text"], new_messages, log)
    summary_state["text"] = condense_summary(summary, log)
    summary_state["up_to"] = oldest_idx
    summary_state["_lead_changed"] = True   # see AsyncSummarizer.harvest
    DEPS["emit"]("on_summary_done")
    return True


def fold_summary_at_head_move(conversation_history, summary_state, oldest_idx, log, why, summarizer):
    """The window head just moved (an eviction, a cache collapse, an overflow retry).
    The prefix after the lead is dead on this request anyway, so the lead may change
    at no extra cost, and the summary of what left belongs in this request rather
    than in a later one as a second miss (measured 2026-11-02: 11 harvest misses,
    220k uncached tokens, in 40 minutes). A summary that is ready is applied. When
    none is, `summary.wait_s` decides: None summarizes now, in front of the request
    (blocking; measured 11 s at the median); a number waits at most that long for a
    running summary, then sends the request with the last summary and lets the
    worker catch up, which lands at a later harvest (kind=late). Returns True when
    the summary state changed, so the window must be rebuilt."""
    changed = bool(summarizer is not None and summarizer.harvest(summary_state))
    if oldest_idx - summary_state["up_to"] <= 0:
        if changed:
            log.info("%s: the ready summary of the history up to message %d folded into this request",
                     why, summary_state["up_to"])
        return changed
    wait_s = summary_wait_s()
    running = summarizer is not None and summarizer.is_running
    if wait_s is None or summarizer is None:
        if running:
            log.info("%s: a summary is already running; the next turn harvests it", why)
            return changed
        if maybe_resummarize(conversation_history, summary_state, oldest_idx, log, force=True):
            log.info("%s: summary of the history up to message %d folded into this request", why, oldest_idx)
            return True
        return changed
    if running and wait_s > 0:
        DEPS["emit"]("on_notice", "info", f"[summary: waiting up to {wait_s:g}s]")
        t0 = time.monotonic()
        summarizer.drain(timeout=wait_s)
        if summarizer.harvest(summary_state):
            changed = True
            if oldest_idx - summary_state["up_to"] <= 0:
                log.info("%s: the summary arrived after %.1fs of the %gs wait and is folded into this request",
                         why, time.monotonic() - t0, wait_s)
                return True
        running = summarizer.is_running
    left = conversation_history[summary_state["up_to"]:oldest_idx]
    if running:
        log.info("summary.nowait %s: %d messages left the window unsummarized; a summary is still "
                 "running after the %gs wait, the next harvest catches up", why, len(left), wait_s)
    elif left and summarizer.kick(summary_state["text"], left, oldest_idx, kind="late"):
        log.info("summary.nowait %s: %d messages left the window unsummarized; the worker catches up "
                 "(wait_s=%g)", why, len(left), wait_s)
    DEPS["emit"]("on_notice", "info", "[summary: going on without one]")
    telemetry.record_patch_event("summary_nowait", kind=why.replace(" ", "_"))
    return changed




# ── Summary ahead of a cache collapse ────────────────────────────────────────
# While the agent waits for input on a caching backend, nothing in the history
# changes, so the window a collapse would rebuild (context.cache_collapse_frac)
# after an idle gap longer than the cache's lifetime is known now. When the gap
# reaches half that lifetime the summary of what the collapse will drop is
# started, and the wake request goes out at once instead of after it (measured
# 2026-11-02: 32 collapses, each behind a synchronous summary of ~11 s).
SUMMARY_IDLE = {"stop": None, "thread": None, "run": None}

SUMMARY_IDLE_AT_FRAC = 0.5    # of the proven cache lifetime
SUMMARY_IDLE_POLL_S = 30.0    # keepalives move last_at, so the gap is re-read this often


def summary_idle_loop(stop, run, log, now=time.time):
    """Wait for the idle gap to reach half the cache's proven lifetime, then start the
    summary a collapse would need, once. `run` holds the history, state, summarizer
    and sizes of the run that just ended. Returns why it stopped, for the tests."""
    key = DEPS["cache_lifetime_key"]()
    while True:
        alive_s, _ = DEPS["cache_lifetime_estimate"](DEPS["cache_lifetimes"](), key)
        last = DEPS["cache_req"]()["last_at"]
        if not last or not alive_s:
            return "no-record"
        gap = now() - last
        due_in = alive_s * SUMMARY_IDLE_AT_FRAC - gap
        if due_in > 0:
            if stop.wait(min(due_in, SUMMARY_IDLE_POLL_S)):
                return "run"
            continue
        summarizer, state = run["summarizer"], run["state"]
        if summarizer.is_running or summarizer.pending_up_to() is not None:
            return "busy"
        edge = _cw.collapse_edge(run["history"], state, run["initial_files"], run["ctx_size"], run["max_tokens"])
        if edge is None:
            return "no-collapse"
        msgs = run["history"][state["up_to"]:edge]
        if not msgs or not summarizer.kick(state["text"], msgs, edge, kind="idle"):
            return "nothing"
        log.info("summary.idle kick after %ds idle: up_to=%d edge=%d (%d messages), for the collapse due past %ds",
                 int(gap), state["up_to"], edge, len(msgs), int(alive_s))
        telemetry.record_patch_event("summary_idle", kind="fired")
        return "kicked"


def summary_idle_disarm():
    """Stop the idle watcher (a run is starting)."""
    stop = SUMMARY_IDLE.get("stop")
    if stop is not None:
        stop.set()
    SUMMARY_IDLE["stop"] = None
    SUMMARY_IDLE["thread"] = None


def summary_idle_arm(log):
    """Start the idle watcher for the run that just ended, when summary.ahead is on,
    the backend caches and a collapse is configured. Called with the keepalive."""
    summary_idle_disarm()
    run = SUMMARY_IDLE.get("run")
    if (not run or run.get("summarizer") is None or not summary_ahead_frac()
            or not DEPS["prompt_caching_backend"]() or not DEPS["cache_collapse_frac"]()):
        return False
    stop = threading.Event()

    def _worker():
        try:
            why = summary_idle_loop(stop, run, log)
            log.debug("summary.idle thread done: %s", why)
        except Exception as e:  # noqa: BLE001
            log.debug("summary.idle thread error: %s", e)

    t = threading.Thread(target=_worker, name="summary-idle", daemon=True)
    SUMMARY_IDLE["stop"], SUMMARY_IDLE["thread"] = stop, t
    t.start()
    return True
