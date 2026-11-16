"""The context window: which messages a request carries, and the token accounting
that decides it (agent.py's `_build_context`).

`build_context` picks the window for one request from the stored history: greedy from
the newest message back on a sliding-window backend; on a prompt-caching backend it keeps
the window's head where it is while everything still fits, evicts in chunks to a fraction
of the budget, aligns an eviction to a summary made ahead of it, and rebuilds the window
small after an idle gap that outlived the cache. The lead (identity files, summary) is
built by agent.py and measured here. The token accounting shared with the request loop
lives here as well: `estimate_tokens`, the calibration ratio `TOKEN_CAL`, the tool
schemas' size `TOOLS_TOKENS` and `MAX_CONTEXT_MESSAGES`. agent.py reads them from this
module at call time, so a test that changes one changes it everywhere.

The builder's environment is `DEPS`: functions agent.py hands over once in `configure`
(the lead message, the summary condenser, whether the backend caches, the config
fractions, the cache record). The defaults make the module usable on its own.
"""
import time

import telemetry
from token_utils import count_tokens_from_message, count_tools_tokens

TRIMMED_LEAD = "\n\n[Earlier conversation trimmed. Continue.]"
CACHE_EVICT_TO_FRAC = 0.6
ALIGN_BAND = (0.75, 1.25)   # an aligned window may be this much smaller or larger than a greedy one
MAX_CONTEXT_MESSAGES = 30          # context.max_context_messages; set by configure
TOOL_LIST = []                     # the tool schemas a request carries; set by configure
TOOLS_TOKENS = None

DEPS = {
    "footnote": lambda summary_text, initial_files: {
        "role": "user", "content": ((initial_files + "\n\n") if initial_files else "")
        + "Progress summary of work done so far:\n" + summary_text},
    "condense_summary": lambda text, log: text,
    "prompt_caching": lambda: False,
    "summary_ahead_frac": lambda: 0.0,
    "collapse_frac": lambda: 0.0,
    "cache_alive_s": lambda: None,
    "last_request_at": lambda: 0.0,
}


def configure(max_context_messages=None, tool_list=None, **deps):
    """agent.py's one call: the message cap, the tool list whose schemas ride in every
    request, and the builder's environment (any DEPS key)."""
    global MAX_CONTEXT_MESSAGES, TOOL_LIST, TOOLS_TOKENS
    if max_context_messages is not None:
        MAX_CONTEXT_MESSAGES = int(max_context_messages)
    if tool_list is not None:
        TOOL_LIST = tool_list
        TOOLS_TOKENS = None
    unknown = set(deps) - set(DEPS)
    if unknown:
        raise KeyError(f"unknown context_window dependency: {sorted(unknown)}")
    DEPS.update(deps)


# ── Token accounting ──────────────────────────────────────────────────────────

def estimate_tokens(msg):
    return count_tokens_from_message(msg)


def estimate_tools_tokens():
    global TOOLS_TOKENS
    if TOOLS_TOKENS is None:
        TOOLS_TOKENS = count_tools_tokens(TOOL_LIST)
    return TOOLS_TOKENS


# F3b, drift-free context accounting. The estimator (token_utils) is a fixed tokenizer /
# chars-per-token guess; every streamed turn returns the server's REAL prompt_tokens for
# what was actually sent. Keep an observed ratio (actual / estimated) and budget the next
# window with it. A tokenizer mismatch between the estimator and the served model shows
# up here as a ratio far from 1.0.
TOKEN_CAL = {"ratio": 1.0, "n": 0, "last_logged": 1.0}
TOKEN_CAL_ALPHA = 0.3        # EMA weight of the newest observation
TOKEN_CAL_MIN, TOKEN_CAL_MAX = 0.25, 4.0   # a single absurd reading cannot wreck the budget


def update_token_calibration(estimated_tokens, actual_prompt_tokens, log=None, cal=None):
    """Pure enough to test: feed (estimate we sent, tokens the server counted) and get the
    updated ratio. Ignores readings that cannot be right (zero/negative, or outside the clamp
    after smoothing). Logs when the ratio moves by more than 10% since the last log line."""
    cal = TOKEN_CAL if cal is None else cal
    try:
        est, act = float(estimated_tokens), float(actual_prompt_tokens)
    except (TypeError, ValueError):
        return cal["ratio"]
    if est <= 0 or act <= 0:
        return cal["ratio"]
    obs = act / est
    if not (TOKEN_CAL_MIN <= obs <= TOKEN_CAL_MAX):
        if log:
            log.debug("token calibration: ignored implausible ratio %.2f (est %d, actual %d)", obs, est, act)
        return cal["ratio"]
    new = obs if cal["n"] == 0 else (1 - TOKEN_CAL_ALPHA) * cal["ratio"] + TOKEN_CAL_ALPHA * obs
    cal["ratio"] = min(TOKEN_CAL_MAX, max(TOKEN_CAL_MIN, new))
    cal["n"] += 1
    if log and abs(cal["ratio"] - cal["last_logged"]) / max(cal["last_logged"], 1e-9) > 0.10:
        log.info("token calibration: observed/estimated ratio now %.2f (n=%d, this turn %.2f) — "
                 "context budget scales by 1/ratio", cal["ratio"], cal["n"], obs)
        cal["last_logged"] = cal["ratio"]
    return cal["ratio"]


def calibrated_budget(budget, cal=None):
    """Tokens the estimator may spend so that the SERVER sees at most `budget`."""
    cal = TOKEN_CAL if cal is None else cal
    return int(budget / cal["ratio"]) if cal.get("n", 0) > 0 and cal["ratio"] > 0 else int(budget)


def context_budget(ctx_size, max_tokens):
    """The token budget of one request's window: the context less the tools, the
    reserved output and a safety margin (ctx_size//4: chat-template overhead the
    tokenizer does not count), calibrated to what the server actually counts (F3b)."""
    global TOOLS_TOKENS
    if TOOLS_TOKENS is None:
        TOOLS_TOKENS = estimate_tools_tokens()
    reserved_output = min(max_tokens, ctx_size // 2)
    budget = ctx_size - TOOLS_TOKENS - reserved_output - max(512, ctx_size // 4)
    return calibrated_budget(budget)


# ── The window ────────────────────────────────────────────────────────────────

def build_initial_files_msg(initial_files):
    """The lead message of a trimmed window when there is no summary yet: the identity
    file(s) the run started with (@AGENT.md / @CLAUDE.md). Byte-stable between calls —
    it heads the request, so any per-turn text here would re-bill the whole history."""
    return {"role": "user", "content": initial_files + TRIMMED_LEAD}


def collapse_due(gap_s, alive_s, frac, window_tokens, budget):
    """Pure: whether the next request should be rebuilt small. The prefix is dead
    when the gap since the last request outlives what the record has proven, and
    everything sent then is billed uncached (measured 2026-10-31 on AI Commons: 21x
    the cached rate), so the window is worth rebuilding only when it would shrink:
    a window already under one and a half times the collapsed size is sent as is."""
    if not frac or gap_s is None or alive_s is None:
        return False
    if gap_s <= alive_s:
        return False
    return window_tokens > budget * frac * 1.5


def fill_window(history, fill_budget, fill_max, context_tokens):
    """Pure: the greedy fill from the newest message back, stopping at `fill_max`
    messages or `fill_budget` tokens, with `context_tokens` reserved for the lead
    whenever the window would not start at message 0. Returns (oldest_idx, used):
    where the window starts (len(history) when nothing fits) and its tokens."""
    used, n, oldest = 0, 0, len(history)
    for i in range(len(history) - 1, -1, -1):
        if n >= fill_max:
            break
        t = estimate_tokens(history[i])
        if used + t + (context_tokens if i > 0 else 0) > fill_budget:
            break
        used += t
        n += 1
        oldest = i
    return oldest, used


def evicted_edge(history, head, used, budget, n_max, ahead, lead=0):
    """Pure: the index up to which the next eviction is certain to drop messages, or
    None while the window stands under `ahead` of both limits. An eviction keeps the
    newest CACHE_EVICT_TO_FRAC of the budget (and of the message cap), so at least
    the other share of the window as it stands now leaves at the next one, and its
    summary can be made now and be ready then. `lead` is the lead message's tokens:
    the eviction fires when window and lead together outgrow the budget, so the line
    is measured on both (a lead of a quarter of the budget kept the window alone under
    80% right up to every eviction, and nothing was ever kicked ahead: 2026-11-14).
    The kept share stays `used` less the leaving share of the budget: with the growth
    still to come before the eviction it lands on the same window a greedy rebuild
    makes, lead or no lead."""
    if not ahead or head is None or head >= len(history):
        return None
    n = len(history) - head
    keep = CACHE_EVICT_TO_FRAC
    by_tokens = used + lead >= budget * ahead
    by_count = n >= n_max * ahead
    if not (by_tokens or by_count):
        return None
    keep_budget = int(used - budget * (1.0 - keep)) if by_tokens else budget
    keep_max = (n - (n_max + 1 - max(2, int(n_max * keep)))) if by_count else n_max
    if keep_budget <= 0 or keep_max <= 0:
        return None
    edge, _ = fill_window(history, keep_budget, keep_max, 0)
    return edge if head < edge < len(history) else None


def aligned_head(history, ready, head, fill_budget, fill_max, n_max, budget, context_tokens):
    """Pure: when a summary is ready up to `ready`, past the current `head`, and the
    window from there is within ALIGN_BAND of the one a greedy rebuild would make,
    that is the new head: nothing leaves unsummarized and nothing is waited for. A
    summary made for a collapse is never taken at an eviction, nor the reverse: the
    band rejects a window a quarter smaller or larger than the greedy one."""
    if not isinstance(ready, int) or head is None or ready <= head or ready >= len(history):
        return None
    n = len(history) - ready
    used = sum(estimate_tokens(m) for m in history[ready:])
    if n > n_max or used + context_tokens > budget:
        return None
    g_idx, g_used = fill_window(history, fill_budget, fill_max, context_tokens)
    g_n = len(history) - g_idx
    if not g_n:
        # The greedy rebuild keeps no message (a lead larger than the share): a summary
        # that ends within a message or two of the end is the whole of it.
        return ready if n <= 2 else None
    if not g_used:
        return None
    lo, hi = ALIGN_BAND
    if used > g_used * hi:
        return None
    if used >= g_used * lo or n >= g_n * lo:
        return ready
    return None


def lead_tokens(summary_state, initial_files):
    """The tokens of the lead message a trimmed window would carry right now."""
    if summary_state.get("text"):
        return estimate_tokens(DEPS["footnote"](summary_state["text"], initial_files))
    if initial_files:
        return estimate_tokens(build_initial_files_msg(initial_files))
    return 0


def collapse_edge(history, summary_state, initial_files, ctx_size, max_tokens):
    """The head a cache collapse would rebuild the window to if it happened now, or
    None when it would not happen (collapse off, no kept head, the window already
    small: the same test as collapse_due) or nothing new would leave. The
    head may be the end of the history: a lead larger than the collapsed share
    leaves the rebuilt window with no message at all (the builder does the same),
    and the summary of everything is what that collapse needs."""
    frac = DEPS["collapse_frac"]()
    head = summary_state.get("_cache_head")
    if not frac or not isinstance(head, int) or not (0 <= head < len(history)):
        return None
    budget = context_budget(ctx_size, max_tokens)
    lead = lead_tokens(summary_state, initial_files)
    used = sum(estimate_tokens(m) for m in history[head:]) + (lead if head > 0 else 0)
    if not used > budget * frac * 1.5:
        return None
    edge, _ = fill_window(history, int(budget * frac), max(2, int(MAX_CONTEXT_MESSAGES * frac)), lead)
    return edge if edge > summary_state.get("up_to", 0) and edge <= len(history) else None


LEAD_LOGGED = {"tokens": None}   # the last context.lead line's size (one per process)


def log_lead(tokens, summary_state, initial_files, log):
    """One INFO line with the lead's size, again when it moves by more than a tenth. The
    lead (the identity files and the summary, as the footnote carries them) rides in every
    trimmed request and sets what a cache collapse can keep; a trim of the identity file
    should be measured here, not estimated from a chars-per-token ratio (2026-11-15: a
    lead of 12.5k tokens in a 56.6k budget was found on the debug line by accident)."""
    last = LEAD_LOGGED["tokens"]
    if last is not None and abs(tokens - last) <= last // 10:
        return
    LEAD_LOGGED["tokens"] = tokens
    text = summary_state.get("text") or ""
    files = estimate_tokens({"role": "user", "content": initial_files}) if initial_files else 0
    summary = estimate_tokens({"role": "user", "content": text}) if text else 0
    log.info("context.lead tokens=%d files=%d summary=%d", tokens, files, summary)


def build_context(conversation_history, summary_state, initial_files, ctx_size, max_tokens, log,
                    max_messages_override=None):
    """Build the context window dynamically based on token budget.

    When messages are dropped and a summary exists, a synthetic user message
    is prepended containing the initial file contents and the progress summary.

    Args:
        max_messages_override: If set, cap messages to this count (used for
            context reduction on 500 errors).

    Returns (messages_to_send, oldest_included_idx).
    """
    budget = context_budget(ctx_size, max_tokens)
    effective_max = max_messages_override if max_messages_override else MAX_CONTEXT_MESSAGES

    context_msg = None
    context_tokens = 0
    if summary_state["text"]:
        context_msg = DEPS["footnote"](summary_state["text"], initial_files)
        context_tokens = estimate_tokens(context_msg)

        # If summary takes a large share of the budget, reduce message count
        # rather than truncating the summary — the summary IS the agent's memory
        # of all prior work and must be preserved intact.
        if context_tokens > budget * 0.8:
            # Summary alone exceeds the budget — condense it
            log.warning("Summary exceeds 80%% of budget (%d/%d tokens) — condensing", context_tokens, budget)
            summary_state["text"] = DEPS["condense_summary"](summary_state["text"], log)
            # Rebuild context_msg with condensed summary
            context_msg = DEPS["footnote"](summary_state["text"], initial_files)
            context_tokens = estimate_tokens(context_msg)
            log.info("Condensed summary, context now %d tokens", context_tokens)
    elif initial_files:
        # No summary yet (or none ever, on a backend whose window evicts in chunks):
        # the identity file still leads any window that no longer starts at message 0.
        # Measured before this: the first eviction dropped CLAUDE.md six minutes into a
        # 2.5 h run and the agent re-read fragments of it from the shell 18 times.
        context_msg = build_initial_files_msg(initial_files)
        context_tokens = estimate_tokens(context_msg)
    if context_msg:
        log_lead(context_tokens, summary_state, initial_files, log)
        if context_tokens > budget * 0.5:
            # Summary is large — reduce message count to make room
            remaining_budget = budget - context_tokens
            avg_msg_tokens = 150  # rough estimate per message
            max_msgs = max(2, int(remaining_budget / avg_msg_tokens))
            if max_msgs < effective_max:
                log.info("Summary uses %d/%d tokens — reducing messages from %d to %d",
                         context_tokens, budget, effective_max, max_msgs)
                effective_max = max_msgs

    used = 0
    selected = []
    oldest_idx = len(conversation_history)
    head = summary_state.get("_cache_head") if DEPS["prompt_caching"]() and not max_messages_override else None
    fill_frac = None
    if head is not None and 0 <= head < len(conversation_history):
        # P4.2: keep the window's head where it is while everything from it still
        # fits, so this request extends the previous one byte for byte.
        window = conversation_history[head:]
        used = sum(estimate_tokens(m) for m in window)
        extra = context_tokens if (context_msg and head > 0) else 0
        last_at = DEPS["last_request_at"]()
        gap_s = (time.time() - last_at) if last_at > 0 else None
        alive_s = DEPS["cache_alive_s"]()
        collapse_frac = DEPS["collapse_frac"]()
        if collapse_due(gap_s, alive_s, collapse_frac, used + extra, budget):
            # The cached prefix outlived its recorded lifetime while the agent was
            # idle, so this request is billed uncached whatever it holds: rebuild it
            # small, behind a summary of what leaves (folded in by the run loop).
            log.info("cache collapse: idle %ds > lifetime %ds; window of %d messages (~%d tokens) "
                     "rebuilt to %d%% of the budget", int(gap_s), int(alive_s), len(window),
                     used + extra, int(collapse_frac * 100))
            summary_state["_collapsed"] = True            # read by the cache.miss log
            summary_state["_collapse_summarize"] = True   # read by the run loop
            fill_frac = collapse_frac
            used = 0
        elif len(window) <= effective_max and used + extra <= budget:
            selected, oldest_idx = list(window), head
        else:
            used = 0
    if not selected:
        # Greedy fill from the newest message back. On a caching backend, fill only
        # to a fraction of the limits, so the new head then holds for many turns.
        frac = fill_frac or (CACHE_EVICT_TO_FRAC if (DEPS["prompt_caching"]() and not max_messages_override) else 1.0)
        fill_budget = int(budget * frac) if frac < 1.0 else budget
        fill_max = max(2, int(effective_max * frac)) if frac < 1.0 else effective_max
        aligned = None
        if frac < 1.0 and head is not None:
            # A summary made ahead of this head move (summary.ahead, or the idle watcher
            # before a collapse) ends at _ready_up_to: start the window there when that
            # is about the size a greedy rebuild would make, so the summary is complete
            # and nothing waits.
            aligned = aligned_head(conversation_history, summary_state.get("_ready_up_to"), head,
                                    fill_budget, fill_max, effective_max, budget, context_tokens)
        if aligned is not None:
            oldest_idx = aligned
            used = sum(estimate_tokens(m) for m in conversation_history[aligned:])
            log.info("summary.align: window head %s -> %d, where the ready summary ends (%d messages, ~%d tokens)",
                     head, aligned, len(conversation_history) - aligned, used)
            telemetry.record_patch_event("summary_align", kind="fired")
        else:
            oldest_idx, used = fill_window(conversation_history, fill_budget, fill_max,
                                            context_tokens if context_msg else 0)
        selected = list(conversation_history[oldest_idx:])
        if frac < 1.0:
            if summary_state.get("_cache_head") not in (None, oldest_idx):
                log.info("cache-friendly eviction: window head %s -> %d (%d messages, ~%d tokens)",
                         summary_state.get("_cache_head"), oldest_idx, len(selected), used)
                summary_state["_head_moved"] = True   # read by the cache.miss log, then cleared
                # The prefix after the lead is dead on this request anyway, so the lead
                # may change at no extra cost: the run loop summarises what left the
                # window before sending, instead of harvesting it two turns later as a
                # second miss (measured 2026-11-02 on kimi: 11 harvest misses, 220k
                # uncached tokens, in 40 minutes of tool-heavy work).
                summary_state["_evict_summarize"] = True
            summary_state["_cache_head"] = oldest_idx

    window_used = used
    if oldest_idx > 0 and context_msg:
        selected.insert(0, context_msg)
        used += context_tokens
    else:
        context_msg = None   # nothing leads an untrimmed window (the log line reports what was sent)
    ahead = DEPS["summary_ahead_frac"]() if (DEPS["prompt_caching"]() and not max_messages_override) else 0.0
    if ahead and oldest_idx < len(conversation_history):
        # Read by the run loop: the worker summarizes up to the edge now, the eviction
        # then aligns to it (above) and goes out with its summary at no wait.
        edge = evicted_edge(conversation_history, oldest_idx, window_used, budget, effective_max, ahead,
                             context_tokens if context_msg else 0)
        if edge is not None and edge > summary_state.get("up_to", 0):
            summary_state["_ahead_edge"] = edge
    if context_msg is None and not any(m["role"] == "user" for m in selected):
        if initial_files:
            selected.insert(0, {"role": "user", "content": initial_files + "\n\nContinue."})
        else:
            for i in range(oldest_idx - 1, -1, -1):
                if conversation_history[i]["role"] == "user":
                    selected.insert(0, conversation_history[i])
                    break

    summary_state["_head"] = oldest_idx   # read by AsyncSummarizer.harvest: a summary past it is held
    log.debug("Context budget: %d/%d tokens, %d messages, oldest_idx=%d, has_context_msg=%s",
              used, budget, len(selected), oldest_idx, context_msg is not None)
    return selected, oldest_idx
