#!/usr/bin/env python3
"""Main agent script.

Connects to llama-server and runs the agentic tool-calling loop.
Entry points: ``run_agent_interactive()`` for interactive use, ``run_agent()``
for single-prompt runs. See ``docs/cli.md`` for CLI flags.

Interactive slash commands are dispatched from ``commands.py`` (``/help`` lists
them in-session). Notably: ``/model [main|summary] [name]`` sets the main or
summary model and persists the choice to ``.agent/config.json``; ``/alias``
installs an ``agent`` shell alias for the current checkout. See ``docs/cli.md`` →
"Slash commands".

Windows runs under Git-Bash (the runtime is cross-platform Python; bash idioms
shell out to Git-Bash's ``bash.exe``). See ``docs/install.md`` → "Windows
(Git-Bash) setup" for install steps and platform notes.
"""

__version__ = "0.1.0"

# Boot line — fired BEFORE the heavy imports below so the user sees
# something within milliseconds of `python3 agent.py`. Without it the
# terminal is silent for several seconds while requests/prompt_toolkit
# load and the backend health probes run.
#
# Gates: only when run as the main script (silent on test imports), and
# only when stderr is a TTY (no ANSI garbage in piped CICD logs).
# The TerminalCallbacks instance reads _BOOT_LINES_PRINTED later so its
# on_session_start can erase this line via cursor_up_clear before the
# banner renders.
_BOOT_LINES_PRINTED = 0

def _do_boot() -> int:
    """Prints the boot sequence to stderr if it is a tty."""
    import sys as _boot_sys
    if _boot_sys.stderr.isatty():
        _boot_sys.stderr.write("\033[2m  starting agent...\033[0m\n")
        _boot_sys.stderr.flush()
        return 1
    return 0

if __name__ == "__main__":
    _BOOT_LINES_PRINTED = _do_boot()

def _git_short_sha() -> str:
    """Short git SHA of the current checkout, or "" if unavailable.

    Used by the session banner to distinguish builds when operators
    run multiple checkouts of the agent on the same machine.
    """
    try:
        import subprocess as _sp
        import os as _os
        here = _os.path.dirname(_os.path.abspath(__file__))
        out = _sp.check_output(
            ["git", "-C", here, "rev-parse", "--short", "HEAD"],
            stderr=_sp.DEVNULL, timeout=1, text=True,
        )
        return out.strip()
    except Exception:
        return ""


import gc
import hashlib
import json
import logging
import logging.handlers
import os
import random
import re
import requests
import subprocess
import sys
import threading
import time
import telemetry
from datetime import datetime, timezone
from pathlib import Path

from cancel import cancellable, check_cancelled, CancelledError, set_tui_mode
from cancel import reset as cancel_reset, tui_mode as cancel_tui_mode
from cancel import request_cancel as cancel_request
try:
    from circuit_breaker import CircuitBreakerError
except ImportError:
    # Circuit breaker not available - define dummy exception
    class CircuitBreakerError(Exception):
        pass
from spinner import StreamStatus
from token_utils import count_tokens_from_message
from tools import MAP_FN, tools, load_extra_tools
import tools as _registry
import skills as _skills
import live_input as _live
import system_prompt as _sp
import cicd_gates as _cicd
import activity as _act
import agent_config as _cfg
import monitor_bus
import schedule_bus
import tools.end_cycle as _end_cycle_tool
from tools.exec_command import cleanup_temp_sessions
# Import CycleFrequencyTracker for cycle timestamp tracking
try:
    sys.path.insert(0, str(Path(__file__).parent.parent / "e1" / "tools"))
    from cycle_frequency_tracker import CycleFrequencyTracker
    _tracker = CycleFrequencyTracker()
except Exception:
    _tracker = None
import theme
import commands as _commands
from callbacks import NullCallbacks, TerminalCallbacks, safe_cb
from types import SimpleNamespace

RESET = theme.RESET
BOLD = theme.BOLD
DIM = theme.DIM
# The old names, for readers; the loop calls the module and reads its state as _cicd.NAME.
_check_worktree_guard = _cicd.check_worktree_guard
_SHELL_ANCHOR = _cicd.SHELL_ANCHOR
_HEREDOC_BODY_RE = _cicd.HEREDOC_BODY_RE
_strip_noncommand_text = _cicd.strip_noncommand_text
_cmd_has = _cicd.cmd_has
_TRACK_SCOPE_TOOLS = _cicd.TRACK_SCOPE_TOOLS
_TRACK_SCOPE_PATH_RE = _cicd.TRACK_SCOPE_PATH_RE
_is_track_scope_call = _cicd.is_track_scope_call
_handle_cicd_file_edit = _cicd.handle_file_edit
_validate_tool_call = _cicd.validate_tool_call
_cicd_verify_gh_mutation = _cicd.verify_gh_mutation
_cicd_checkpoint_text = _cicd.checkpoint_text


# WS10.c: success-check gate. When config defines cycle.success_check (a
# shell command), end_cycle is blocked until it exits 0 — the agent cannot
# declare done while the declared success criterion fails. Replay baselines
# (2026-07-10) showed BOTH tiers' dominant failure was completion discipline
# (M2/P1: exited with the measurement still red), not reasoning.
def _run_success_check(cmd, log=None):
    """Returns (rc, tail). Never raises; failure to run counts as rc=1."""
    try:
        p = subprocess.run(["bash", "-c", cmd], capture_output=True,
                           text=True, timeout=180)
        out = ((p.stdout or "") + (p.stderr or ""))[-600:]
        return p.returncode, out
    except Exception as e:
        if log:
            log.warning("success_check errored: %s", e)
        return 1, f"(success_check errored: {e})"


# ── Background monitor injection ─────────────────────────────────────────
# A monitor (armed via the `monitor` tool) runs a shell command on an interval
# in a daemon thread and queues any non-empty output on the monitor bus. The
# agentic loop drains that queue at the turn-complete boundary (below) and
# feeds each item into the conversation as a new user turn — so a monitor can
# pop new work in "as the agent continues". Daemon threads die with the
# process: an -a cycle that ends still exits promptly; monitors never extend
# process lifetime.
# Framing for a mid-burst injection: tells the model this is asynchronous
# background input that arrived mid-work, so it finishes its current action
# instead of abandoning it (derailment resistance — a behavioural property
# validated live, not headless).
_BTW_FRAMING = (
    "[Background note — the following arrived while you were working. Finish "
    "your current action first; do NOT abandon or restart it. Address this at "
    "your next natural stopping point.]"
)

# Control sentinel for the live-input thread's exit path. Module-level (not
# local to run_agent_interactive) because the mid-burst drain below must
# recognize it: an 'exit' typed while a turn is streaming lands on the same
# bus as steering input, and without the filter it was framed as a background
# note and delivered to the MODEL while the interactive loop — the only
# consumer that knows what 'exit' means — never saw it (field report
# 2026-08-14: input box gone, exit passed to the backend, loop wedged).
_LIVE_EXIT_MARK = _live.EXIT_MARK

# True while the live-input consumer (the only reader that dispatches slash commands) is
# running: then a `/command` typed during a tool burst is held for it instead of being
# handed to the model as text. Off, the blocking prompt handles its own slash lines and
# nothing on the bus is a command.
_SLASH_HOLD = {"active": False}


def _drain_monitor_injections(conversation_history, framing=None):
    """Append any queued monitor output to conversation_history as a single
    user turn (optionally framed as a background note).

    Returns True if it injected anything (the caller resets its turn-complete
    counters / continues), False if the queue was empty (the caller falls
    through to normal stop logic, so -a still exits at a real cycle end).
    Combines all queued items into ONE user turn to avoid consecutive-user
    sequences. Pure w.r.t. loop-control state; never raises.

    Two kinds of typed line are CONTROL, never model input, and are re-enqueued
    in order for the interactive main loop, which acts on them at the turn
    boundary: 'exit' (as `_LIVE_EXIT_MARK`) and, while the live-input consumer
    runs, a slash command. Before this, `/model NAME` typed during a burst
    reached the model as conversation text and nothing was switched.
    """
    try:
        queued = monitor_bus.drain_items()
    except Exception:
        return False
    items, held, typed, events = [], [], [], []
    for kind, text in queued:
        if text == _LIVE_EXIT_MARK or (
                kind == "user" and _SLASH_HOLD["active"] and text.lstrip().startswith("/")):
            held.append(text)
        else:
            items.append(text)
            (typed if kind == "user" else events).append(text)
    for text in held:
        monitor_bus.put_user(text)
    if not items:
        return False
    body = "\n\n".join(items)
    if framing:
        body = framing + "\n\n" + body
    msg = {"role": "user", "content": body}
    conversation_history.append(msg)
    # Skills (`pick: "always"`): a typed line is judged like any user message, bus
    # items only when `pick_on_events` is on; never blocks (a worker thread).
    try:
        if _skills.SKILLS:
            if _skills.CFG.get("pick") == "always":
                if typed:
                    _skills.PICKER.kick("\n\n".join(typed), source="user")
                elif events and _skills.CFG.get("pick_on_events"):
                    _skills.PICKER.kick("\n\n".join(events), source="events")
            # Judged here or not at all: a monitor's output is never "the task in the
            # user's words" for the session-start pick.
            msg["_skill_pick"] = True
    except Exception:
        pass
    return True


def _has_pending_tool_calls(conversation_history):
    """True if the most recent assistant tool-call batch has any tool_call
    without a matching tool result — i.e. injecting a user turn now would
    strand an unanswered tool_call (which strict templates/validators reject).

    Scans from the end for the last assistant message; if it declared
    tool_calls, every id must appear in a following role:'tool' message.
    Handles tool_call items as dicts or objects. Never raises.
    """
    try:
        last_asst = None
        for i in range(len(conversation_history) - 1, -1, -1):
            if conversation_history[i].get("role") == "assistant":
                last_asst = i
                break
        if last_asst is None:
            return False
        tcs = conversation_history[last_asst].get("tool_calls") or []
        ids = set()
        for tc in tcs:
            tid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            if tid:
                ids.add(tid)
        if not ids:
            return False
        answered = set()
        for j in range(last_asst + 1, len(conversation_history)):
            m = conversation_history[j]
            if m.get("role") == "tool":
                tcid = m.get("tool_call_id")
                if tcid:
                    answered.add(tcid)
        return bool(ids - answered)
    except Exception:
        # On any uncertainty, report "pending" so we do NOT inject (fail safe).
        return True


def _detect_hallucinated_read(full_content: str) -> tuple[bool, str | None]:
    """
    Checks if the agent claims to have read a file that was not actually accessed.
    Returns (is_hallucinated, reason).
    """
    if not full_content:
        return False, None

    try:
        from tools.file import _accessed_files
        pattern = r'(?:read|found|contents? of|file (?:has|contains|shows))\s+[`"\']?(\S+\.(?:py|json|md|txt|yaml|yml|toml|jsonl|sh|cfg))'
        for match in re.finditer(pattern, full_content, re.IGNORECASE):
            claimed_file = match.group(1)
            start = match.start()
            preceding = full_content[max(0, start-20):start].lower()
            if any(word in preceding for word in ['will', 'to ', 'should', 'must', 'need to']):
                continue
            _resolved = str((Path.cwd() / claimed_file).resolve())
            if _resolved not in _accessed_files:
                return True, f"Agent claimed to read {claimed_file}, but it was not accessed."
    except Exception:
        pass
    return False, None


# Module-level UI callback handle. run_agent_interactive replaces this with a
# TerminalCallbacks() by default, or a user-provided subclass (e.g. TuiCallbacks
# from the prompt_toolkit front-end in Phase 3).  Helpers that want to emit a
# UI event use `_emit("method_name", ...)` which routes through `safe_cb` so a
# buggy UI hook can never crash the loop.
_cb: NullCallbacks = NullCallbacks()
_cb_log = None


def _emit(method, *args, **kwargs):
    """Invoke a callback method via safe_cb, logging any exception."""
    return safe_cb(_cb, method, *args, log=_cb_log, **kwargs)


import checkpoint as _ckpt
import job as _job
import memory_pressure as _mem
import context_window as _cw
import summarize as _summ
import tool_args as _ta
import lead as _lead
_RUNTIME_NOTE_HEAD = _lead.RUNTIME_NOTE_HEAD   # the stored note's head; tests read it here
_EXIT_ONLY_RE = re.compile(r'^\[session:[^\]]+\]\s+exit=0\s*$')

# ── Pinned instructions ───────────────────────────────────────────────
# Content inside <pinned>...</pinned> tags in the initial prompt is
# extracted and re-injected into every context-restoration message,
# surviving summarization.  This ensures critical workflow steps
# (like "create a worktree before editing") persist across the entire
# session even as older messages are compressed away.
_PINNED_RE = re.compile(r"<pinned>(.*?)</pinned>", re.DOTALL)
_pinned_instructions = ""  # set once from the initial prompt

# WS1 (wave 2): active PhaseEngine for this session, or None (inert — no
# behavior change without cycle.profile/cycle.phases config). Module-level
# for the same reason as the _cicd_* cluster: read by per-turn injection.
_active_phase_engine = None
# WS1: explicit role from --role flag; None → legacy prompt string-matching.
_explicit_role = None


def _extract_pinned(text):
    """Extract <pinned>...</pinned> blocks from text.

    Returns (cleaned_text, pinned_content).  The pinned blocks are removed
    from the text to avoid double-counting tokens.
    """
    blocks = _PINNED_RE.findall(text)
    if not blocks:
        return text, ""
    cleaned = _PINNED_RE.sub("", text).strip()
    return cleaned, "\n".join(b.strip() for b in blocks)


# ── Configuration ──────────────────────────────────────────────────────

# The old names, for readers; agent.py calls the module. The loaded dict and the live
# backends stay here, and the module reads them through its DEPS getters.
_DEFAULT_CONFIG = _cfg.DEFAULTS
_ENDPOINT_KEYS = _cfg.ENDPOINT_KEYS
_drop_llamacpp_defaults = _cfg.drop_llamacpp_defaults
_synthesize_backends_registry = _cfg.synthesize_backends_registry
_redact_api_keys = _cfg.redact_api_keys
_display_backend_kind = _cfg.display_backend_kind
_warn_if_world_readable_with_key = _cfg.warn_if_world_readable_with_key
_load_config = _cfg.load
_SUBAGENT_ARGS_ENV = _cfg.SUBAGENT_ARGS_ENV
_export_backend_overrides = _cfg.export_backend_overrides
_export_subagent_args = _cfg.export_subagent_args
_cfg_with_role = _cfg.with_role
_apply_generation_overrides = _cfg.apply_generation_overrides
_generation_request_extras = _cfg.generation_request_extras
_EFFORT_LEVELS = _cfg.EFFORT_LEVELS
_persist_config_value = _cfg.persist_value
_set_model_for_role = _cfg.set_model_for_role
_get_effort_for_role = _cfg.get_effort_for_role
_set_effort_for_role = _cfg.set_effort_for_role
_set_system_prompt = _cfg.set_system_prompt


_DEFAULT_CTX_SIZE = 114688


# ── Headless hardening (plan/headless-hardening.md) ───────────────────────────
# F6 — classifiable exit status, `-a` mode only. Interactive sessions keep exiting 0.
# Codes are documented and stable (docs/cli.md); a terminal `AGENT-EXIT: <name> <detail>`
# line goes to stderr so a supervisor can classify a failure without parsing logs.
EXIT_OK = 0
EXIT_ERROR = 1          # generic run error (additive; nothing consumed exit codes before)
EXIT_DEADLINE = 10      # F1 wall-clock stop
EXIT_CONTRACT = 11      # F2 result contract unsatisfied (synthesized `failed` written)
EXIT_CONTEXT = 12       # context exhaustion after every reduction
EXIT_MEMORY = 13        # memory watermark
EXIT_CONFIG = 14        # config error (backend unbuildable, schema unreadable, ...)
EXIT_BACKEND = 15       # backend unreachable / failed after retries
EXIT_ACCEPTANCE = 16    # --job: the RUNNER re-ran the acceptance command and it failed
_EXIT_NAMES = {EXIT_OK: "completed", EXIT_ERROR: "error", EXIT_DEADLINE: "deadline",
               EXIT_CONTRACT: "contract", EXIT_CONTEXT: "context", EXIT_MEMORY: "memory",
               EXIT_CONFIG: "config", EXIT_BACKEND: "backend", EXIT_ACCEPTANCE: "acceptance"}
# Decided from argv at import so import-time failures (backend build) can already classify.
# --job is auto mode too (a job is a run that ENDS): the flag-level implication lives in main(),
# but this gate is read from the raw argv at import time, so it has to know as well. A live job
# with a failing acceptance exited 0 and printed no AGENT-EXIT line because only this scan gated it.
_AUTO_MODE = any(a in ("-a", "--auto", "-r", "--repeat", "--job") for a in sys.argv[1:])
_LAST_EXIT = None           # {"code", "name", "detail"} — set by the terminal path that knows why
_LAST_ERROR_KIND = None     # "context" | "backend" — set beside the run loop's `return "error"`


def _set_exit(code, detail=""):
    """Record the classified exit for `-a` mode. Last writer wins; main() emits it."""
    global _LAST_EXIT
    _LAST_EXIT = {"code": int(code), "name": _EXIT_NAMES.get(int(code), "error"),
                  "detail": str(detail or "")[:300]}
    return _LAST_EXIT


def _exit_line(exit_info):
    return f"AGENT-EXIT: {exit_info['name']} {exit_info['detail']}".rstrip()


def _note_error_kind(kind):
    """Set beside a `return "error"` in the run loop so the auto path can classify it."""
    global _LAST_ERROR_KIND
    _LAST_ERROR_KIND = kind


def _classify_auto_result(result, error_kind=None, last_exit=None):
    """Map the run loop's terminal value to (code, detail). Pure — unit-tested.
    A code already set by a hard stop (deadline/contract/memory) wins over the generic map."""
    if last_exit and last_exit.get("code") not in (None, EXIT_OK):
        return last_exit["code"], last_exit.get("detail", "")
    if result == "error":
        if error_kind == "context":
            return EXIT_CONTEXT, "context overflow after every reduction"
        if error_kind == "backend":
            return EXIT_BACKEND, "backend request failed after retries"
        if error_kind == "refusal":
            return EXIT_BACKEND, "refusal: the model or a gateway guardrail declined the request"
        return EXIT_ERROR, "run loop returned error"
    return EXIT_OK, str(result or "")


# F9 — capability refusal. A tool that says "you were not granted X" is not an error to fix and
# not a wall to route around; it is a fact about the run's grant that nothing the run writes can
# change. The patterns are the shapes such refusals take in practice: missing/invalid secrets,
# authorization, permissions, quotas, and blocked egress. Kept generic on purpose.
_REFUSAL_PATTERNS = re.compile(
    r"\bREFUSED\b|\bnot granted\b|\bcapabilit(?:y|ies) (?:not )?granted\b"
    r"|(?:api[_ -]?key|secret|token|credentials?)\s+(?:is\s+|was\s+|are\s+)?"
    r"(?:not\s+(?:set|configured|provided|found|in (?:the )?environment)|missing|required|invalid|expired|rejected|unset|empty)"
    r"|\bno (?:valid )?(?:api[_ -]?key|credentials?|token)s?\b"
    r"|\b[A-Z][A-Z0-9_]{2,}\b[^\n]{0,60}\bnot in (?:the )?environment\b"
    r"|environment variable[^.\n]{0,40}\b(?:not set|is empty|undefined|required|missing)\b"
    r"|\bpermission denied\b|\baccess denied\b|\boperation not permitted\b|\bEACCES\b|\bEPERM\b"
    r"|\bunauthori[sz]ed\b|\bforbidden\b|\bauthentication (?:failed|required|denied)\b"
    r"|\bHTTP (?:401|403|429)\b|\b(?:401|403|429) (?:unauthorized|forbidden|too many requests)\b"
    r"|\brate limit(?:ed)?\b|\btoo many requests\b|\bquota (?:exhausted|exceeded|reached)\b"
    r"|\bconnection refused\b|\bECONNREFUSED\b|\bproxy (?:blocked|denied|refused|required)\b"
    r"|\bno route to host\b|\bnetwork is (?:not reachable|unreachable)\b|\begress (?:is )?(?:blocked|denied)\b",
    re.I)
# Tools whose result text alone can carry a refusal (no exit status to consult). A file the run
# READ containing the words "permission denied" is a document, not a refusal, so read/search
# tools are never classified.
_REFUSAL_TEXT_TOOLS = frozenset({"web_fetch"})
_LAST_REFUSAL = None     # {"tool", "cmd", "exit", "head", "turn"} — read by _synthesize_result


def _classify_tool_refusal(func_name, result_str):
    """F9, pure: (exit_code_or_None, head_line) if this tool result is a CAPABILITY refusal,
    else None. exec_command results carry `exit=N` in their header and must be non-zero;
    network tools are classified on text; everything else is never a refusal."""
    if not result_str:
        return None
    text = str(result_str)[:4000]
    if func_name == "exec_command":
        m = re.search(r"\bexit=(-?\d+)", text[:300])
        code = int(m.group(1)) if m else None
        if code == 0 or code is None:
            # The session exit is the status of the LAST command in the line. Models habitually
            # append `; echo "EXIT_CODE=$?"` — which makes the shell exit 0 and prints the real
            # status one line down. Measured live: a refusal with exit 2 went unclassified for
            # exactly this reason. Read the echoed status when the session's is 0.
            echoed = re.findall(r"\b(?:EXIT_CODE|EXIT|exit code|exit status|rc|status)\s*[=:]\s*(\d+)\b",
                                text, re.I)
            if echoed and int(echoed[-1]) != 0:
                code = int(echoed[-1])
            else:
                return None
    elif func_name in _REFUSAL_TEXT_TOOLS:
        code = None
    else:
        return None
    if not _REFUSAL_PATTERNS.search(text):
        return None
    lines = [ln.strip() for ln in text.splitlines()
             if ln.strip() and not ln.strip().startswith("[session:")]
    head = next((ln for ln in lines if _REFUSAL_PATTERNS.search(ln)), lines[0] if lines else "")
    return code, head[:200]


def _refusal_notice(tool, cmd, code, head, max_turns):
    """F9: appended ONCE to the refusing tool's own result, so the model reads it in the place
    it is already looking. Names the tool, the command, the exit and the line."""
    what = f"{tool}" + (f" `{cmd}`" if cmd else "")
    return (f"[CAPABILITY REFUSAL] {what} exited {code if code is not None else '(n/a)'}: {head}\n"
            f"This run was NOT GRANTED that capability, and nothing you can write will grant it. Do NOT "
            f"build a replacement — no fetcher, client, downloader, retry through another route, or "
            f"workaround script. If the task cannot be completed without it, reply with your FINAL RESULT "
            f"now: a result block with \"status\": \"blocked\" and a reason naming the tool and its exit "
            f"code. You have at most {max_turns} turn(s) before tool calls are refused.")


def _deadline_step(elapsed_s, deadline_s, fired, fracs):
    """F1, pure: given elapsed wall-clock, the deadline and the set of fractions already
    fired, return (message_or_None, hard_stop_bool). Fires each fraction at most once, in
    order, and the hard stop exactly once (fired gains the sentinel 1.0). Unit-tested."""
    if not deadline_s or deadline_s <= 0:
        return None, False
    frac = elapsed_s / float(deadline_s)
    remaining = max(0, int(deadline_s - elapsed_s))
    if frac >= 1.0:
        if 1.0 in fired:
            return None, True
        fired.add(1.0)
        return (f"[SYSTEM: DEADLINE REACHED ({int(deadline_s)}s). Tool calls are now REFUSED. "
                f"Reply with your FINAL RESULT immediately — state what was done, what was not, "
                f"and where the artifacts are. Do not start anything.]"), True
    due = [f for f in sorted(fracs) if frac >= f and f not in fired]
    if not due:
        return None, False
    f = due[-1]                       # a late tick fires only the highest due fraction
    for skipped in due:
        fired.add(skipped)
    if f >= 0.9:
        msg = (f"[SYSTEM: {remaining}s of wall-clock budget left ({int(f * 100)}% used). "
               f"STOP WORKING NOW — emit your final result on this turn: what was done, what was "
               f"not, artifact paths. The next warning is the hard stop.]")
    elif f >= 0.75:
        msg = (f"[SYSTEM: {remaining}s of wall-clock budget left ({int(f * 100)}% used). "
               f"Begin wrapping up — save progress, finish the current step, prepare your final "
               f"result. Do not start new tasks.]")
    else:
        msg = (f"[SYSTEM: heads-up — {remaining}s of wall-clock budget left ({int(f * 100)}% used). "
               f"Plan to finish inside it.]")
    return msg, False


# ── F7: repeat-read stall signature ──────────────────────────────────────────
def _repeat_read_check(seen, key, turn, n=3, window=6, latched=None):
    """Pure: record a read-class call `key` at `turn`; return True exactly once per key when
    the same key was seen >= n times within the last `window` turns. `seen` maps key -> list
    of turns; `latched` is the set of keys already nudged (one nudge, not a nag)."""
    turns = [t for t in seen.get(key, []) if turn - t < window] + [turn]
    seen[key] = turns
    if latched is None:
        latched = set()
    if len(turns) >= n and key not in latched:
        latched.add(key)
        return True
    return False


def _read_call_key(func_name, func_args):
    """Normalized identity of a read-class call: tool + sorted args (paths as given)."""
    try:
        return (func_name, json.dumps(func_args, sort_keys=True, default=str)[:400])
    except Exception:  # noqa: BLE001
        return (func_name, str(func_args)[:400])


# ── F4: goal anchoring + deliverable guard ───────────────────────────────────
# The stanza may sit mid-line ("WORK ITEM 42. GOAL: ..."), so anchor on a word boundary, not
# the line start; the value runs to the end of that line.
_GOAL_STANZA_RE = re.compile(r"\b(?:GOAL|Goal)\s*:\s*(.+?)\s*$", re.M)
_DELIVERABLE_STANZA_RE = re.compile(r"\b(?:DELIVERABLES?|Deliverables?)\s*:\s*(.+?)\s*$", re.M)


def _derive_goal_and_deliverables(prompt_text):
    """From a recognizable goal/deliverable stanza in the initial prompt: ('goal', [paths]).
    Paths are the comma/space-separated tokens of the deliverable line that look like paths or
    globs (contain a '/', '.', or '*'); prose words are dropped. Empty when nothing matches."""
    goal, dels = "", []
    if not prompt_text:
        return goal, dels
    m = _GOAL_STANZA_RE.search(prompt_text)
    if m:
        goal = m.group(1).strip()[:400]
    m = _DELIVERABLE_STANZA_RE.search(prompt_text)
    if m:
        for tok in re.split(r"[,\s]+", m.group(1)):
            tok = tok.strip("`'\"()[]")
            if tok and any(c in tok for c in "/.*") and not tok.endswith(":"):
                dels.append(tok)
    return goal, dels


def _missing_deliverables(globs, cwd=None):
    """Mechanical: which named deliverables (paths or globs) match nothing on disk right now."""
    import glob as _glob
    base = cwd or os.getcwd()
    missing = []
    for g in globs or []:
        pat = g if os.path.isabs(g) else os.path.join(base, g)
        if not _glob.glob(pat):
            missing.append(g)
    return missing


def _goal_anchor_message(goal, deliverables, missing, frac):
    parts = [f"[SYSTEM GOAL ANCHOR ({int(frac * 100)}% of budget used)"]
    if goal:
        parts.append(f"The stated goal: {goal}")
    if deliverables:
        parts.append("Named deliverables: " + ", ".join(deliverables))
        parts.append("Deliverables that do NOT exist yet: " + (", ".join(missing) if missing else "none — all present"))
    parts.append("Check that what you are doing serves the goal above; produce the missing deliverables before anything else.]")
    return " ".join(parts)


# ── F2: result contract ───────────────────────────────────────────────────────
RESULT_CONTRACT_STATUSES = ("done", "failed", "blocked", "cannot-tell")
RESULT_CONTRACT_DEFAULT_SCHEMA = {
    "type": "object",
    "required": ["contract", "status", "summary"],
    "properties": {
        "contract": {"const": 1},
        "status": {"enum": list(RESULT_CONTRACT_STATUSES)},
        "summary": {"type": "string"},
        "artifacts": {"type": "array"},
        "verify_output": {"type": "string"},
        "scope": {"type": "object"},
    },
}
_RESULT_CONTRACT_SCHEMA = None      # loaded at launch when armed


def _result_contract_armed():
    """Armed only by an explicit True or a schema path — never by a truthy stand-in (tests
    that mock the config object must not accidentally arm the contract)."""
    try:
        v = (_config.get("cycle") or {}).get("result_contract")
    except Exception:  # noqa: BLE001
        return False
    return v is True or (isinstance(v, str) and v.strip() != "" and v.lower() not in ("false", "0", "off"))


def _load_result_contract_schema(spec):
    """True → built-in; a path → JSON schema file. Unreadable → raises (launch refuses)."""
    if spec is True or spec in ("default", "", None):
        return json.loads(json.dumps(RESULT_CONTRACT_DEFAULT_SCHEMA))
    with open(str(spec), "r", encoding="utf-8") as f:
        schema = json.load(f)
    if not isinstance(schema, dict):
        raise ValueError("result contract schema must be a JSON object")
    return schema


def _result_contract_instruction(schema):
    req = ", ".join(schema.get("required") or ["contract", "status", "summary"])
    statuses = "|".join((schema.get("properties", {}).get("status", {}) or {}).get("enum") or RESULT_CONTRACT_STATUSES)
    return ("RESULT CONTRACT: end your FINAL message with one fenced ```json block that is the "
            "result record, shaped {\"contract\": 1, \"status\": \"" + statuses + "\", \"summary\": str, "
            "\"artifacts\": [paths], \"verify_output\": str, \"scope\": {\"examined\": [], \"skipped\": [], "
            "\"not_covered\": []}}. Required keys: " + req + ". The key \"contract\": 1 marks it as the "
            "result — example or discussed JSON without that key is prose. \"blocked\" and \"failed\" "
            "are legal statuses; say what stopped you.")


def _validate_result(obj, schema):
    """Minimal structural validation (no dependency): required keys, const, enum, JSON types.
    Uses jsonschema when importable. Returns (ok, reason)."""
    try:
        import jsonschema  # type: ignore
        jsonschema.validate(obj, schema)
        return True, ""
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001 — jsonschema.ValidationError and friends
        # name the failing key: the library's message ("7 is not of type 'string'") does not
        path = "/".join(str(p) for p in getattr(e, "absolute_path", []) or [])
        head = str(getattr(e, "message", e)).splitlines()[0][:200]
        return False, f"{path}: {head}" if path else head
    if not isinstance(obj, dict):
        return False, "result is not an object"
    for k in schema.get("required") or []:
        if k not in obj:
            return False, f"missing required key {k!r}"
    _types = {"string": str, "array": list, "object": dict, "number": (int, float), "integer": int, "boolean": bool}
    for k, rule in (schema.get("properties") or {}).items():
        if k not in obj or not isinstance(rule, dict):
            continue
        if "const" in rule and obj[k] != rule["const"]:
            return False, f"{k!r} must be {rule['const']!r}"
        if "enum" in rule and obj[k] not in rule["enum"]:
            return False, f"{k!r} must be one of {rule['enum']}"
        t = rule.get("type")
        if t in _types and not isinstance(obj[k], _types[t]) or (t == "number" and isinstance(obj[k], bool)):
            return False, f"{k!r} must be {t}"
    return True, ""


def _extract_result_block(text):
    """The LAST fenced JSON block carrying "contract": 1, parsed. A block without the
    discriminator is prose (the decoy case). Returns (obj|None, reason)."""
    if not text:
        return None, "empty message"
    blocks = re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.S)
    if not blocks:
        return None, "no fenced JSON block"
    reason = "no fenced JSON block carries \"contract\": 1"
    for blk in reversed(blocks):
        try:
            obj = json.loads(blk)
        except ValueError:
            reason = "last fenced block is not valid JSON"
            continue
        if isinstance(obj, dict) and obj.get("contract") == 1:
            return obj, ""
    return None, reason


def _dump_failed_request(conversation_history, exc, path=None):
    """On a terminal backend failure, record the SHAPE of the request that failed.

    A backend that rejects a request leaves the caller holding one line — "Server error
    500" — and nothing about what was sent. If the server's own stderr is not captured
    either (a common setup), the failure has no artifact on either side and cannot be
    diagnosed after the fact, only guessed at.

    This writes a shape summary, not the payload: per-message role, content length, tool
    presence, and a short head of each. That is enough to spot the usual culprits — an
    orphaned tool result, an oversized message, an unexpected role order — without
    dumping conversation content to disk, which may be large and is not ours to spill.

    Best-effort by construction: a diagnostic that can itself raise during failure
    handling would replace a useful error with a confusing one.
    """
    try:
        path = path or os.path.join(".agent", "last-failed-request.json")
        rows = []
        for m in (conversation_history or []):
            content = m.get("content")
            # Everything is coerced to a plain string. An unusual history is exactly when a
            # backend rejects a request, so a dumper that serialises only well-formed input
            # goes silent in the one case it exists for.
            rows.append({
                "role": str(m.get("role")),
                "content_chars": len(content) if isinstance(content, str) else None,
                "content_head": (content[:160] if isinstance(content, str)
                                 else (None if content is None else f"<{type(content).__name__}>")),
                "has_tool_calls": bool(m.get("tool_calls")),
                "tool_call_id": (None if m.get("tool_call_id") is None
                                 else str(m.get("tool_call_id"))),
            })
        payload = {
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            "note": "shape only — no message payloads; heads are truncated to 160 chars",
            "message_count": len(rows),
            "total_content_chars": sum(r["content_chars"] or 0 for r in rows),
            "roles_in_order": [r["role"] for r in rows],
            "messages": rows[-40:],          # the tail is where a bad shape almost always is
        }
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            try:
                json.dump(payload, fh, indent=1, ensure_ascii=False)
            except (TypeError, ValueError):
                # a history item that will not serialise is itself a finding; say so rather
                # than leaving an empty file that reads as "nothing to report".
                fh.write(json.dumps({"error": payload["error"],
                                     "note": "history was not JSON-serialisable — shape below is repr",
                                     "roles_in_order": [str(r.get("role")) for r in rows],
                                     "repr_tail": repr(rows[-8:])[:4000]}, indent=1))
        return path
    except Exception:                                        # noqa: BLE001 — never mask the real error
        return None


def _synthesize_result(reason, exit_info=None):
    # F9: a run that met a capability refusal and then emitted nothing is BLOCKED, not merely
    # failed, and the record says by what. `synthesized` stays true — the caller can still tell
    # the run did not say this itself.
    status, summary = "failed", f"<no valid result block emitted: {reason}>"
    if _LAST_REFUSAL:
        status = "blocked"
        summary = (f"<synthesized: {_LAST_REFUSAL.get('tool')} "
                   f"{('`' + str(_LAST_REFUSAL.get('cmd')) + '` ') if _LAST_REFUSAL.get('cmd') else ''}"
                   f"refused for an ungranted capability at turn {_LAST_REFUSAL.get('turn')} "
                   f"(exit {_LAST_REFUSAL.get('exit')}): {_LAST_REFUSAL.get('head')}; "
                   f"no valid result block was emitted: {reason}>")
    return {"contract": 1, "status": status, "summary": summary,
            "artifacts": [], "verify_output": "", "scope": {"examined": [], "skipped": [], "not_covered": []},
            "synthesized": True, "exit": exit_info or {}}


def _last_assistant_text(conversation_history):
    for msg in reversed(conversation_history):
        if msg.get("role") == "assistant" and msg.get("content"):
            return msg["content"]
    return ""


def _write_result_file(result_file, conversation_history, exit_info=None, schema=None):
    """Raw text when the contract is off (byte-identical to the historical behavior).
    When armed: the validated block as JSON, with the exit classification folded in; no
    valid block → a synthesized failed record, and the caller learns via (obj, synthesized)."""
    text = _last_assistant_text(conversation_history)
    if not _result_contract_armed():
        with open(result_file, "w", encoding="utf-8") as f:
            f.write(text)
        return None, False
    schema = schema or _RESULT_CONTRACT_SCHEMA or RESULT_CONTRACT_DEFAULT_SCHEMA
    obj, why = _extract_result_block(text)
    if obj is not None:
        ok, vwhy = _validate_result(obj, schema)
        if not ok:
            obj, why = None, f"result block invalid: {vwhy}"
    synthesized = obj is None
    if synthesized:
        obj = _synthesize_result(why, exit_info)
    elif exit_info:
        obj.setdefault("exit", dict(exit_info))
    with open(result_file, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
        f.write("\n")
    return obj, synthesized


def _exit_now(code, detail=""):
    """Terminal exit from an import-time or mid-run hard stop. In `-a` mode the classified
    code and the AGENT-EXIT line are used; interactive sessions keep their historical `2`."""
    info = _set_exit(code, detail)
    if _AUTO_MODE:
        print(_exit_line(info), file=sys.stderr)
        sys.exit(info["code"])
    sys.exit(2)


# F5 — canonical working directory. A run launched through a symlinked path made
# `os.path.relpath()` walk out of the repo and `git log -- <path>` return empty WITH exit 0,
# so a validator passed with every field silently defaulted. The kernel already reports a
# physical cwd on Linux; what leaks the logical (symlinked) form is `$PWD`, which child shells
# and `Path.cwd()`-style callers trust. Canonicalize both, log when anything changed, and keep
# `--no-realpath-cwd` for overlay-style setups that want the symlinked view.
_NO_REALPATH_CWD = "--no-realpath-cwd" in sys.argv[1:]


def _canonicalize_cwd(env=None, chdir=os.chdir, getcwd=os.getcwd):
    """Returns (logical, real) when something was rewritten, else None. Pure enough to test:
    env/chdir/getcwd are injectable."""
    if _NO_REALPATH_CWD:
        return None
    env = os.environ if env is None else env
    try:
        cur = getcwd()
        real = os.path.realpath(cur)
    except OSError:
        return None
    logical = env.get("PWD") or cur
    changed = None
    if real != cur:
        chdir(real)
        changed = (cur, real)
    if env.get("PWD") != real:
        changed = changed or (logical, real)
        env["PWD"] = real
    return changed


_CWD_CANONICALIZED = _canonicalize_cwd()

_config = _cfg.load()
# A second execution of this file as a script while the imported `agent` module is
# live (a test runner's runpy) must not re-register the modules' hooks from its own
# throwaway namespace: the hooks would then read that namespace's state, not the
# module's that the tests patch.
_FIRST_LOAD = not (__name__ == "__main__" and "agent" in sys.modules)
if _FIRST_LOAD:
    _cfg.configure(config=lambda: _config, main_backend=lambda: _main_backend,
                   summary_backend=lambda: _summary_backend,
                   emit=lambda *a, **k: _emit(*a, **k))

# Config-declared environment, exported verbatim. A deployment often needs to pin a value that
# an external CLI reads from the environment — a caller identity, a workspace tag, a tenant —
# so that a bad generation cannot talk its way past it: the value is fixed before the model
# runs and the tool downstream refuses anything else.
#
# agent.py deliberately knows NOTHING about what any of these keys mean. It copies
# `config["env"]` into the process environment and stops. Keeping the semantics out of here is
# the point: the moment this file names a specific tool's variable, that tool becomes a
# documented assumption of the agent rather than one thing somebody happens to run.
#
# Deliberately conservative, and both rules matter:
#   • STRICT NO-OP without an explicit `env` block — nothing is ever inferred from cwd,
#     basename or hostname. A guessed value is worse than no value, because it is wrong
#     confidently and the caller cannot see it happen.
#   • NEVER overrides a variable already in the environment — a launch script, systemd unit
#     or CI job that set it wins, because it is closer to the operator than a config file is.
#
# Declare it in .agent/config.json (gitignored, same locality as llm/base_url):
#     {"env": {"SOME_TOOL_IDENTITY": "worker-3"}}
_declared_env = _config.get("env")
if isinstance(_declared_env, dict):
    for _k, _v in _declared_env.items():
        if _v is None or os.environ.get(str(_k)):
            continue
        os.environ[str(_k)] = str(_v)

# Apply configuration
BASE_URL = _config["llm"]["base_url"]

# Whitelist any extra paths the agent is allowed to access outside its working
# directory (preferences.extra_allowed_paths in config.json).
from tools.file import set_extra_allowed_paths as _set_extra_allowed_paths
_set_extra_allowed_paths(_config.get("preferences", {}).get("extra_allowed_paths", []))

# Backend instances (plan task 1.4). Phase 1 only wires LlamacppBackend;
# Phase 2 adds BedrockBackend behind the same factory. Tests may monkeypatch
# these module globals to swap backends without touching the factory.
from llm_backend import ConfigError, build_backend as _build_backend


def _apply_model_effort_overrides(model, effort) -> None:
    """``--model`` / ``--effort``: write them into the main backend's config (so a
    failover rebuild keeps them) and rebuild main. No-op when neither is given."""
    global _main_backend
    if not model and not effort:
        return
    entry = _config["backends"].setdefault("main", {})
    if model:
        entry["model"] = model
    if effort:
        entry["effort"] = effort
    _main_backend = _build_backend(_cfg.with_role(_config["backends"], "main"))
    if getattr(_main_backend, "model", None):
        _config["llm"]["model"] = _main_backend.model
    logging.getLogger("agent").info("main backend override: model=%s effort=%s",
                                    getattr(_main_backend, "model", None), effort or "-")


try:
    _main_backend = _build_backend(_cfg.with_role(_config["backends"], "main"))
    _summary_backend = _build_backend(_cfg.with_role(_config["backends"], "summary"))
except (ValueError, ConfigError) as _be:
    # A bad "kind" (or other unbuildable backend config) must not dump a raw
    # import-time traceback on the user — build_backend already phrases these
    # to name the config field and the fix. Same stderr-for-config-integrity
    # contract as the malformed-config path in _load_config.
    _emit("on_notice", "error", f"Backend config error: {_be}")
    logging.getLogger("agent").error("backend build failed: %s", _be)
    print(f"⚠ Backend config error: {_be}", file=sys.stderr)
    _exit_now(EXIT_CONFIG, f"backend config error: {_be}")   # F6: 14 in -a mode, 2 interactive


def _apply_backend_overrides(main_kind: str | None, summary_kind: str | None) -> None:
    """Apply ``--backend-main`` / ``--backend-summary`` CLI overrides.

    Per plan task 2.5: when either flag is set, override
    ``_config["backends"][role]["kind"]`` and rebuild the corresponding
    backend module-level global. If a flag selects ``bedrock`` without an
    explicit model already in the config block, a sensible default is
    supplied (``claude-v4.6-opus`` for main, ``claude-v4.5-haiku`` for
    summary) and an INFO log line is emitted.
    """
    global _main_backend, _summary_backend

    _cfg.override_backend_kind("main", main_kind)
    _cfg.override_backend_kind("summary", summary_kind)

    if main_kind:
        _main_backend = _build_backend(_cfg.with_role(_config["backends"], "main"))
        # Keep _config["llm"]["model"] in sync so the TUI status bar shows the
        # active model rather than the llamacpp default.
        if getattr(_main_backend, "model", None):
            _config["llm"]["model"] = _main_backend.model
    if summary_kind:
        _summary_backend = _build_backend(
            _cfg.with_role(_config["backends"], "summary")
        )


_MAX_FULL_LINES = _config["context"]["max_full_lines"]
_PREVIEW_LINES = _config["context"]["preview_lines"]
if _FIRST_LOAD:
    _lead.configure(max_full_lines=_MAX_FULL_LINES, preview_lines=_PREVIEW_LINES,
                    model_guards=_config["context"].get("model_guards", True),
                    emit=_emit)
if _FIRST_LOAD:
    _summ.configure(
        threshold=_summ.sane_summary_threshold(_config["context"]),
        max_chars=_config["context"]["summary_max_chars"],
        summary_backend=lambda: _summary_backend, main_backend=lambda: _main_backend,
        config=lambda: _config, base_url=lambda: BASE_URL, cache_req=lambda: _CACHE_REQ,
        emit=lambda *a, **k: _emit(*a, **k),
        trigger_failover=lambda *a, **k: _trigger_failover(*a, **k),
        build_backend=lambda cfg: _build_backend(cfg),
        cache_lifetime_key=lambda *a, **k: _cache_lifetime_key(*a, **k),
        cache_lifetime_estimate=lambda *a, **k: _cache_lifetime_estimate(*a, **k),
        cache_lifetimes=lambda: _cache_lifetimes(),
        cache_collapse_frac=lambda: _cache_collapse_frac(),
        prompt_caching_backend=lambda: _prompt_caching_backend(),
        cicd_facts=_cicd.facts)
# The old names, for readers. The loop reads the live knobs through _summ: a /setup
# update changes them there.
_SUMMARY_THRESHOLD, _SUMMARY_MAX_CHARS = _summ.SUMMARY_THRESHOLD, _summ.SUMMARY_MAX_CHARS
_sane_summary_threshold = _summ.sane_summary_threshold
_summary_wait_s = _summ.summary_wait_s
_summary_ahead_frac = _summ.summary_ahead_frac
_format_for_summary = _summ.format_for_summary
_summary_request = _summ.summary_request
_condense_summary = _summ.condense_summary
_build_summary_prompt = _summ.build_summary_prompt
_TOOL_RESULT_COMPRESS_AFTER = _summ.TOOL_RESULT_COMPRESS_AFTER
_TOOL_RESULT_COMPRESS_MIN = _summ.TOOL_RESULT_COMPRESS_MIN
_TOOL_RESULT_COMPRESS_MODEL_MAX = _summ.TOOL_RESULT_COMPRESS_MODEL_MAX
_local_compress = _summ.local_compress
_summarize_for_compression = _summ.summarize_for_compression
_TOOL_RESULT_COMPRESS_PRESSURE = _summ.TOOL_RESULT_COMPRESS_PRESSURE
_history_pressure = _summ.history_pressure
_compress_repeated_tool_results = _summ.compress_repeated_tool_results
_generate_summary = _summ.generate_summary
AsyncSummarizer = _summ.AsyncSummarizer
_maybe_resummarize = _summ.maybe_resummarize
_fold_summary_at_head_move = _summ.fold_summary_at_head_move
_SUMMARY_IDLE = _summ.SUMMARY_IDLE
_SUMMARY_IDLE_AT_FRAC = _summ.SUMMARY_IDLE_AT_FRAC
_SUMMARY_IDLE_POLL_S = _summ.SUMMARY_IDLE_POLL_S
_summary_idle_loop = _summ.summary_idle_loop
_summary_idle_disarm = _summ.summary_idle_disarm
_summary_idle_arm = _summ.summary_idle_arm

_LLM_MAX_RETRIES = _config["retry"]["max_retries"]
_LLM_BASE_DELAY = _config["retry"]["base_delay_seconds"]
_LLM_MAX_DELAY = _config["retry"]["max_delay_seconds"]
_LLM_BACKOFF_MULTIPLIER = _config["retry"]["backoff_multiplier"]
_LLM_JITTER_FACTOR = _config["retry"]["jitter_factor"]

_MAX_TURNS = _config["cycle"]["max_turns"]
_WIND_DOWN_TURNS = _config["cycle"]["wind_down_turns"]
_MAX_TEXT_ONLY = _config["cycle"]["max_text_only"]
_MAX_TOTAL_NUDGES = _config["cycle"]["max_total_nudges"]

# Auto-nudge on text-only responses. Off by default; enable with --nudge or config.json preferences.nudge.
_NUDGE_ENABLED = _config.get("preferences", {}).get("nudge", False)

# Cap tool result strings stored in conversation_history to limit context pressure.
# ~20K chars ≈ 5K tokens.  Keeps head + tail so the model sees start and end.
_MAX_TOOL_RESULT_CHARS = 20_000

# Classify exec_command calls as read-only vs substantive for nudge counter.
_WRITE_KEYWORDS = ("git commit", "git push", "cat >", ">>", "tee ",
                   "sed -i", "patch ", "mv ", "cp ", "rm ", "mkdir ",
                   "gh pr create", "gh issue create", "gh issue edit",
                   "gh pr merge", "gh pr close", "gh pr review",
                   "gh issue close", "gh issue comment")
_READ_ONLY_COMMANDS = ("grep ", "find ", "ls ", "cat ", "head ", "tail ",
                       "wc ", "git log", "git diff", "git status",
                       "git branch", "gh pr list", "gh pr view",
                       "gh pr diff", "gh issue list", "gh issue view",
                       "gh pr checks", "python3 -m unittest",
                       "python3 -m pytest")


def _is_read_only_command(cmd):
    """A command is read-only if it matches a known read pattern or has no write keywords."""
    if not isinstance(cmd, str):
        return True  # a poll (no command) or a garbled value: nothing is written by it
    cmd_stripped = cmd.lstrip("# \t\n")
    if any(cmd_stripped.startswith(rc) for rc in _READ_ONLY_COMMANDS):
        return True
    return not any(kw in cmd for kw in _WRITE_KEYWORDS)


# Load agent-specific tools from CWD/.agent/tools/ if it exists.
# Note: CWD/tools/ is the builtin package already loaded by tools/__init__.py —
# pointing the loader at it would re-execute every module under a fake
# "extra_tools" parent, breaking relative imports (e.g. exec_command).
_agent_tools_dir = os.path.join(os.getcwd(), ".agent", "tools")
if os.path.isdir(_agent_tools_dir):
    load_extra_tools(_agent_tools_dir)

# Tool tiers (tools/__init__.py): core tools are in every request, deferred ones are
# named in one catalog line and loaded by tool_search, hidden ones only by a search.
# preferences.tools_whitelist is applied first (a tool not on it does not exist to the
# model or the search; MAP_FN keeps it for harness-internal calls). The `tools` block
# moves tools between tiers; a whitelist alone makes everything on it core, as before.
# consult_advisor stays hidden until an advisor backend is configured.
_TOOLS_DEFAULTS = {"core": [], "deferred": [], "hidden": [], "catalog": True,
                   "preload_from_prompt": True, "implicit_run": True}
_TOOLS_CFG = dict(_TOOLS_DEFAULTS)
if isinstance(_config.get("tools"), dict):
    _TOOLS_CFG.update(_config["tools"])
_tools_whitelist = _config.get("preferences", {}).get("tools_whitelist")
try:
    from tools.consult_advisor import advisor_active as _advisor_active_for_tier
    if _advisor_active_for_tier(_config):
        _registry.set_tier("consult_advisor", "deferred")
except Exception:
    pass
# ── Runtime controls a tool may call (tools.RUNTIME) ──────────────────────────


def _agent_mod():
    return sys.modules.get("agent") or sys.modules[__name__]


def _runtime_role(role):
    role = str(role or "main").strip().lower()
    if role not in ("main", "summary"):
        raise ValueError(f"unknown role {role!r}: main or summary")
    return role


def _switchable_models(role="main"):
    """`preferences.switchable_models`: a list (the main role's) or `{role: list}`.
    Empty when nothing is configured, which keeps `switch_model` out of existence."""
    role = _runtime_role(role)
    raw = (_agent_mod()._config.get("preferences") or {}).get("switchable_models")
    if isinstance(raw, dict):
        raw = raw.get(role)
    elif role != "main":
        raw = None
    if not isinstance(raw, list):
        return []
    return [str(n).strip() for n in raw if str(n).strip()]


def _runtime_get_model(role="main"):
    role = _runtime_role(role)
    mod = _agent_mod()
    backend = mod._main_backend if role == "main" else mod._summary_backend
    section = "llm" if role == "main" else "summary"
    return str(getattr(backend, "model", None) or (mod._config.get(section) or {}).get("model") or "")


def _runtime_set_model(role, model):
    model = str(model or "").strip()
    if not model:
        raise ValueError("a model name is required")
    return _agent_mod()._set_model_for_role(_runtime_role(role), model)


def _runtime_get_effort(role="main"):
    return _agent_mod()._get_effort_for_role(_runtime_role(role))


def _runtime_set_effort(role, effort):
    return _agent_mod()._set_effort_for_role(_runtime_role(role), effort)


_registry.RUNTIME.update({
    "get_model": _runtime_get_model, "set_model": _runtime_set_model, "models": _switchable_models,
    "get_effort": _runtime_get_effort, "set_effort": _runtime_set_effort,
})


# ── Restart in place (/restart, the restart tool) ─────────────────────────────
# A restart ends the session the way 'exit' does, after the checkpoint is written, and
# then main() replaces the process with `python agent.py <same arguments> -c`: the same
# pid, terminal and tmux pane, the new code, and the session resumed from the checkpoint.
# The request is a flag read at turn boundaries: the loops end on it, run_agent_single
# returns "restart" once the turn's tool results are recorded (never mid-tool-call), and
# main() does the exec after the normal teardown has restored the terminal.
import restart_state as _restart
_RESTART = _restart.STATE
_RESTART_LIMIT, _RESTART_WINDOW_S = _restart.LIMIT, _restart.WINDOW_S
_restart_too_often, _request_restart = _restart.too_often, _restart.request
_restart_argv, _exec_restart = _restart.restart_argv, _restart.exec_restart
_CKPT_TURN = _ckpt.CKPT_TURN      # the turn the newest checkpoint carries (for a boundary save)


def _restart_resume_prompt():
    """The text to resume with after a restart the TOOL asked for (empty: none). Read
    before the first turn consumes the restart record."""
    rs = _RESTART.get("resumed")
    if not rs or rs.get("by") != "tool":
        return ""
    try:
        text = _config.get("preferences", {}).get("restart_resume_prompt", "")
    except Exception:
        return ""
    return text.strip() if isinstance(text, str) else ""


def _restart_gate():
    """The restart tool exists only with `preferences.restart_tool` true."""
    try:
        return bool(_config.get("preferences", {}).get("restart_tool", False))
    except Exception:
        return False


_registry.RUNTIME["request_restart"] = _request_restart
_registry.GATES["restart"] = _restart_gate
if "restart" in _registry.DEFINITIONS and _restart_gate():
    _registry.TIERS["restart"] = "deferred"
    _registry.rebuild()


def _switch_model_gate():
    """switch_model exists only with a non-empty `preferences.switchable_models`."""
    try:
        return bool(_switchable_models("main") or _switchable_models("summary"))
    except Exception:
        return False


_registry.GATES["switch_model"] = _switch_model_gate


def _apply_switch_model_tier():
    """Shipped hidden; named in the catalog (deferred) once it is enabled. Runs before
    the config's own tier lists below, which still win."""
    if "switch_model" in _registry.DEFINITIONS and _switch_model_gate():
        _registry.TIERS["switch_model"] = "deferred"
        _registry.rebuild()


_apply_switch_model_tier()


# ── The event bus: finished sessions, the gate, wait (monitor_bus.py, gate.py) ────
_GATE_JUDGE_LOCK = threading.Lock()   # one judge call at a time, from any source thread


def _gate_judge(prompt, timeout, max_tokens=200, lock=_GATE_JUDGE_LOCK):
    """The summary model as the gate's judge (and, with its own lock, the skill
    picker's and the F3 status question's): one small deterministic completion, with
    thinking off. On a reasoning model the thinking spends the small budget first and
    the answer comes back cut or empty (haiku, 2026-11-17: F3 printed nothing, two
    gate verdicts failed open as unparseable). Every backend's complete() builds its
    request afresh, so a call from a monitor thread is safe beside the main loop; the
    lock only keeps gated sources from stampeding."""
    backend = _agent_mod()._summary_backend
    gen = {"max_tokens": max_tokens, "temperature": 0, "thinking": "off"}
    with lock:
        try:
            return backend.complete(prompt=prompt, gen_params=gen, timeout=timeout)
        except TypeError as e:  # a backend whose complete() takes only the prompt
            if "unexpected keyword" not in str(e):
                raise
            return backend.complete(prompt=prompt)


_BUS_PREFS = _config.get("preferences") or {}
monitor_bus.configure(session_events=_BUS_PREFS.get("session_events", True),
                      gate=_BUS_PREFS.get("gate") if isinstance(_BUS_PREFS.get("gate"), dict) else {},
                      judge=_gate_judge)
_registry.GATES["gate"] = monitor_bus.gate_enabled
if "gate" in _registry.DEFINITIONS and monitor_bus.gate_enabled():
    _registry.TIERS["gate"] = "deferred"
    _registry.rebuild()


# ── Skills (skills.py): discovered once here; the picker shares the gate's judge call
# but not its lock, so a slow pick never delays a gate verdict ──
_SKILL_JUDGE_LOCK = threading.Lock()


def _skill_judge(prompt, timeout):
    return _gate_judge(prompt, timeout, max_tokens=_skills._PICK_MAX_TOKENS, lock=_SKILL_JUDGE_LOCK)


def _spill_cap_for_skills():
    """limits.tool_result_max_chars as `_tool_result_spill_cap` reads it (defined further
    down this file, after this runs): a real integer, else the legacy boundary."""
    v = (_config.get("limits") or {}).get("tool_result_max_chars", _MAX_TOOL_RESULT_CHARS)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return _MAX_TOOL_RESULT_CHARS
    return max(0, int(v))


_skills.configure(_config.get("skills"), cwd=os.getcwd(), complete=_skill_judge,
                  count_tokens=lambda text: count_tokens_from_message({"role": "user", "content": text}),
                  spill_cap=_spill_cap_for_skills())
_skills.discover(log=logging.getLogger("agent"))

_TOOL_TIERS = _registry.apply_tiers(
    _TOOLS_CFG, whitelist=_tools_whitelist if isinstance(_tools_whitelist, list) else None,
    log=logging.getLogger("agent"))


def _session_tool_names(session_tools):
    return {(t.get("function") or {}).get("name") for t in session_tools}


def _apply_tool_loads(session_tools, log, initial=False):
    """Append the tools asked for since the last turn (tool_search, an implicit call, a
    resumed checkpoint) to the session's tool list, once, in load order; returns the
    names added. Called before each request and after each tool batch, so the list
    changes at most once per turn and a load costs one cache miss. initial=True also
    brings back what this session loaded before (a resume, or an earlier turn in an
    interactive session, which builds its list afresh)."""
    if initial:
        _registry.resume_pending()
        for name in list(_registry.LOADED):
            if _registry.available(name) and name not in _registry.PENDING_LOADS:
                _registry.PENDING_LOADS.append(name)
    present = _session_tool_names(session_tools)
    added = []
    for name in _registry.take_pending():
        if name in present:
            _registry.mark_loaded(name)
            continue
        session_tools.append(_registry.DEFINITIONS[name])
        present.add(name)
        _registry.mark_loaded(name)
        added.append(name)
    if added:
        log.info("tools.loaded %s (the session list is now %d tools)", ", ".join(added), len(session_tools))
        for _ in added:
            telemetry.record_patch_event("tool_loaded", kind="fired")
    return added

# end_cycle is opt-in (preferences.end_cycle_tool). It is never in the registry's
# list (_auto_exclude); when off it is also kept out of the per-session list and
# refused at dispatch, so a model that calls it from memory gets an error, not an exit.
_END_CYCLE_ENABLED = bool(_config.get("preferences", {}).get("end_cycle_tool", False))


def _end_cycle_gate():
    """The registry's gate for end_cycle. It reads the flag from the module Python knows
    as `agent`, not from whichever copy of this file defined the function: coverage tests
    load agent.py a second time under another name, and a plain lambda from that copy
    would read that copy's (unpatched) flag for every session."""
    mod = sys.modules.get("agent")
    return bool(getattr(mod, "_END_CYCLE_ENABLED", _END_CYCLE_ENABLED) if mod is not None else _END_CYCLE_ENABLED)


_registry.GATES["end_cycle"] = _end_cycle_gate


# ── Retry logic ────────────────────────────────────────────────────────

def _calculate_retry_delay(attempt):
    """Calculate retry delay with exponential backoff and jitter."""
    delay = _LLM_BASE_DELAY * (_LLM_BACKOFF_MULTIPLIER ** attempt)
    delay = min(delay, _LLM_MAX_DELAY)
    if _LLM_JITTER_FACTOR > 0:
        jitter_range = delay * _LLM_JITTER_FACTOR
        delay = delay + random.uniform(-jitter_range, jitter_range)
        delay = max(0, delay)
    return round(delay, 2)


# ``ContextOverflowError`` lives in ``llm_backend`` post-refactor. The alias
# here keeps ``from agent import ContextOverflowError`` working unchanged for
# existing tests and callers.
from llm_backend import BedrockBudgetExceeded, ConfigError, ContextOverflowError
from anthropic_wire import THINKING_KEY as _THINKING_KEY


_LLM_REQUEST_TIMEOUT = 300  # 5 minutes per request


def _llm_request_raw(log, **kwargs):
    """POST to the LLM with retries and exponential backoff.

    Raises ContextOverflowError after 3 consecutive 500s (likely context overflow).
    Other transient errors (503, connection, timeout) retry up to _LLM_MAX_RETRIES.

    This is the original pre-refactor body, preserved as the internal transport
    used by ``LlamacppBackend.stream_chat`` (plan task 1.4). Behavior is
    byte-identical to the pre-refactor ``_llm_request``.
    """
    kwargs.setdefault("timeout", _LLM_REQUEST_TIMEOUT)
    consecutive_500s = 0
    for attempt in range(_LLM_MAX_RETRIES + 1):
        try:
            response = requests.post(f"{BASE_URL}/v1/chat/completions", **kwargs)
            if response.status_code >= 500:
                if response.status_code == 500:
                    consecutive_500s += 1
                    if consecutive_500s >= 3:
                        raise ContextOverflowError(
                            f"3 consecutive HTTP 500 errors — likely context overflow")
                else:
                    consecutive_500s = 0
                raise requests.exceptions.HTTPError(
                    f"Server error {response.status_code}", response=response)
            response.raise_for_status()
            return response
        except ContextOverflowError:
            raise  # Don't retry — caller will reduce context
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.HTTPError) as e:
            if attempt == _LLM_MAX_RETRIES:
                raise
            if isinstance(e, requests.exceptions.HTTPError):
                resp = getattr(e, 'response', None)
                if resp is None or resp.status_code < 500:
                    raise
            delay = _calculate_retry_delay(attempt)
            log.warning("LLM request failed (attempt %d/%d): %s — retrying in %ds",
                        attempt + 1, _LLM_MAX_RETRIES + 1, e, delay)
            _emit("on_api_retry", str(e), attempt + 1, _LLM_MAX_RETRIES, delay)
            time.sleep(delay)


def _is_failover_error(e):
    """Check if an exception should trigger failover to the fallback backend.

    Handles both specific exception classes and generic exceptions containing
    failover keywords (useful for some mock tests and generic Bedrock errors).
    """
    if isinstance(e, (BedrockBudgetExceeded, requests.exceptions.ConnectionError,
                      requests.exceptions.Timeout, TimeoutError)):
        return True
    
    err_msg = str(e)
    failover_keywords = ["BedrockBudgetExceeded", "Capacity exceeded", "Rate limit exceeded"]
    return any(kw in err_msg for kw in failover_keywords)

def _trigger_failover(log, backend_type, cause=None):
    """Swap failing backend for the other role's endpoint.

    Args:
        log: Logger instance.
        backend_type: Either 'main' or 'summary'.
        cause: The exception that triggered failover, when the caller has it.
            Lets budget trips refuse a fallback that would just move spend
            to the other role's cap.

    NOTE: uses the module globals directly, NOT ``from agent import ...`` —
    under ``python3 agent.py`` this module is ``__main__``, so importing
    ``agent`` would load a SECOND module instance whose ``_config`` never
    saw ``_apply_backend_overrides`` (and whose top level re-runs).
    """
    global _main_backend, _summary_backend

    try:
        # Use the OTHER role's config as fallback target, but keep the spend
        # cap and telemetry attributed to the role that will actually consume
        # it — otherwise a budget-tripped main quietly spends under the
        # summary role's daily cap.
        other_role = "main" if backend_type == "summary" else "summary"
        fallback_cfg = _cfg.with_role(_config.get("backends", {}), other_role)
        fallback_cfg["role"] = backend_type

        failing = _main_backend if backend_type == "main" else _summary_backend
        same_endpoint = (
            fallback_cfg.get("kind", "llamacpp") == getattr(failing, "kind", None)
            and (fallback_cfg.get("base_url") or "") == (getattr(failing, "base_url", "") or "")
            and (fallback_cfg.get("model") or "") == (getattr(failing, "model", "") or "")
        )
        if same_endpoint:
            # Default single-server setups synthesize main and summary from
            # the same llm block — "failing over" would rebuild the identical
            # backend and retry the endpoint that just failed.
            log.error(
                "Failover skipped: %s and %s configs point at the same %s "
                "endpoint — nothing to fail over to",
                backend_type, other_role, fallback_cfg.get("kind", "?"),
            )
            return False
        # AI Commons quota is per USER, not per model: failing over to another
        # model on the same gateway re-trips the same allowance.
        same_quota = fallback_cfg.get("kind") == "bedrock" or (
            fallback_cfg.get("kind") == "aicommons"
            and getattr(failing, "kind", None) == "aicommons"
            and (fallback_cfg.get("base_url") or getattr(failing, "base_url", ""))
            == getattr(failing, "base_url", ""))
        if isinstance(cause, BedrockBudgetExceeded) and same_quota:
            log.warning(
                "Fallback not tried for %s: it draws on the same allowance that was just "
                "refused, so it would be refused too",
                backend_type,
            )
            return False

        new_backend = _build_backend(fallback_cfg)
        healthy, msg = new_backend.health()
        if healthy:
            if backend_type == 'main':
                _main_backend = new_backend
                # Keep the TUI status bar on the live model, same as
                # _apply_backend_overrides does on rebuild.
                if getattr(new_backend, "model", None):
                    _config["llm"]["model"] = new_backend.model
            else:
                _summary_backend = new_backend
            log.warning(
                "Failover successful: %s backend -> %s (%s)",
                backend_type, fallback_cfg.get("kind", "?"), msg,
            )
            return True
        else:
            log.error("Failover failed: fallback backend unhealthy (%s)", msg)
            return False

    except Exception as e:
        log.error("Failover critical failure: %s", e)
        return False

def _llm_request(log, **kwargs):
    """Main-path streaming request. Thin wrapper that routes through the
    module-level ``_main_backend`` (see plan task 1.4).

    Signature matches the pre-refactor ``_llm_request`` exactly so existing
    tests that ``patch('agent._llm_request')`` still work unmodified.

    # TESTING NOTES (WS8.5 — beewatcher runs 104/106/109/110 each burned
    # 20-50 turns rediscovering this; read BEFORE writing any mock):
    #
    # The request body arrives as ``kwargs['json']`` — messages are at
    # ``kwargs['json']['messages']`` (run 104's root cause: mocks asserted on
    # a positional arg that does not exist).
    #
    # Return shape: EITHER a requests.Response-like object exposing
    # ``iter_lines()`` (legacy SSE; mock with
    # ``resp.iter_lines.return_value = [f"data: {json.dumps(body)}".encode(),
    # b"data: [DONE]"]`` — NOT ``__iter__``, run 106's confusion) OR a plain
    # iterable of OpenAI-shape delta dicts (simplest for tests). See
    # ``_iter_stream_chunks`` below.
    #
    # CANONICAL 4-PATCH PATTERN for run_agent_single tests — tests HANG
    # unless ALL FOUR are patched (runs 109/110). Use the ``agent_loop_mocks``
    # fixture in tests/conftest.py instead of hand-rolling:
    #   1. patch('agent._llm_request', ...)        # scripted deltas
    #   2. agent._NUDGE_ENABLED = False            # else nudge loop retries
    #   3. patch('agent._emit', ...)               # else callbacks block
    #   4. patch tool dispatch via MAP_FN entries  # else real tools run
    """
    try:
        return _main_backend.stream_chat(log, **kwargs)
    except Exception as e:
        if _is_failover_error(e):
            if _trigger_failover(log, 'main', cause=e):
                log.info("Retrying request with failover backend...")
                return _main_backend.stream_chat(log, **kwargs)
        raise e

def _iter_stream_chunks(response):
    """Yield OpenAI-shape delta dicts from either backend shape.

    Accepts two inputs:
      (a) A ``requests.Response`` (or mock thereof) exposing ``iter_lines()`` —
          the legacy llamacpp shape. SSE frames like ``data: {...}\\n`` are
          parsed here; ``data: [DONE]`` stops iteration.
      (b) Any iterable already yielding delta dicts — the Bedrock shape (and
          simpler for tests). Passed through verbatim.

    Keeping both shapes callable from the main loop means Phase 1's existing
    tests that mock ``_llm_request`` with ``iter_lines.return_value = [...]``
    continue to work without modification, while ``BedrockBackend.stream_chat``
    (a plain generator of dicts) now flows through ``run_agent_single``
    end-to-end. See plan § 7.1 open question on StreamDelta Protocol.
    """
    if hasattr(response, "iter_lines"):
        for raw_line in response.iter_lines(decode_unicode=False):
            line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
            if not line or not line.startswith("data: "):
                continue
            payload = line[len("data: "):]
            if payload == "[DONE]":
                return
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                continue
    else:
        for chunk in response:
            yield chunk


def _prompt_caching_backend():
    """True when the main backend caches the request prefix server-side and bills
    misses (aicommons): the history must then grow append-only and evict in chunks
    (aicommons-integration.md P4). Other kinds keep the per-turn sliding window."""
    return getattr(globals().get("_main_backend"), "kind", None) == "aicommons"


# Local-model reasoning keys that never go back on the wire; they must not change the
# signature. Stored Anthropic thinking (_THINKING_KEY) DOES go back, every turn, so it
# is hashed like any content: a turn sent with it once and without it later is a rewrite.
_PREFIX_SIG_SKIP = frozenset({"thinking", "reasoning_content", "reasoning"})


def _request_signature(messages, tools):
    """What the server could have cached for this request: a hash of the tool
    schemas and one hash per outgoing message. Pure. Local reasoning keys and the
    private `_`-prefixed bookkeeping (`_tokens`, `_note_turn`) never reach the wire,
    so they are left out; the stored Anthropic thinking blocks are kept in."""
    def _h(obj):
        return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return (_h(tools), [_h({k: v for k, v in m.items()
                            if k == _THINKING_KEY
                            or (k not in _PREFIX_SIG_SKIP and not str(k).startswith("_"))})
                        for m in (messages or [])])


def _prefix_unchanged(prev_sig, cur_sig):
    """True when the request `cur_sig` resends every message of `prev_sig`, in
    place, with the same tools: the server's cache for `prev_sig` fully applies, so a
    miss on it is the provider's (cold replica, evicted or unreported cache), not ours.
    Measured 2026-10-29 on Kimi K3 via AI Commons: 7 of 180 requests read 0 cached
    tokens with the neighbours at 99-100% and no eviction, harvest or rewrite between."""
    if not prev_sig or not cur_sig or prev_sig[0] != cur_sig[0]:
        return False
    prev_msgs, cur_msgs = prev_sig[1], cur_sig[1]
    return len(cur_msgs) >= len(prev_msgs) and cur_msgs[:len(prev_msgs)] == prev_msgs


def _prefix_diff(prev_sig, cur_sig):
    """Where the request `cur_sig` first stops resending `prev_sig`: "tools", "msg I/N"
    (the first differing message, 0-based, of N previous), "shorter" (messages
    dropped) or "same". Pure; names the culprit on an unexplained miss."""
    if not prev_sig or not cur_sig:
        return "none"
    if prev_sig[0] != cur_sig[0]:
        return "tools"
    prev_msgs, cur_msgs = prev_sig[1], cur_sig[1]
    for i, h in enumerate(prev_msgs):
        if i >= len(cur_msgs):
            return "shorter"
        if cur_msgs[i] != h:
            return f"msg {i}/{len(prev_msgs)}"
    return "same"


# P4.2 — on a prompt-caching backend, evict down to this fraction of the window
# in one step, then grow append-only again: one cache miss per eviction instead
# of one per turn.


_CACHE_EVICT_TO_FRAC = _cw.CACHE_EVICT_TO_FRAC
_ALIGN_BAND = _cw.ALIGN_BAND
_cache_collapse_due = _cw.collapse_due


def _cache_collapse_frac():
    """The fraction of the budget a collapsed window is rebuilt to; 0 when off."""
    try:
        return max(0.0, min(1.0, float(_config.get("context", {}).get("cache_collapse_frac", 0) or 0)))
    except (TypeError, ValueError):
        return 0.0


class _CompletionResponse:
    """Just enough of a chat-completions response (``.json()``) for tool_recovery."""

    def __init__(self, text):
        self._text = text or ""

    def json(self):
        return {"choices": [{"message": {"role": "assistant", "content": self._text}}]}


def _recovery_llm_call(log):
    """The one-shot call tool_recovery makes. llama.cpp keeps the raw non-streaming
    request it was written for; a streaming-only backend (aicommons, bedrock) gets
    the question through the summary backend's complete() instead (M4)."""
    if getattr(_main_backend, "kind", "llamacpp") in ("llamacpp", "fake"):
        return lambda **kw: _llm_request(log, **kw)

    def _call(json=None, timeout=30, **_kw):
        msgs = (json or {}).get("messages") or []
        prompt = "\n\n".join(str(m.get("content", "")) for m in msgs)
        gen = {"max_tokens": max(int((json or {}).get("max_tokens") or 64), 1024)}
        try:
            text = _summary_backend.complete(prompt=prompt, gen_params=gen, timeout=timeout)
        except TypeError:  # a backend whose complete() takes only the prompt (foundry)
            text = _summary_backend.complete(prompt=prompt)
        return _CompletionResponse(text)
    return _call


_persist_runtime_note, _append_turn_text = _sp.persist_runtime_note, _sp.append_turn_text


def _skills_at_boundary(conversation_history, messages_to_send, oldest_idx, log):
    """The skills work of a turn boundary, after the window is built and before the
    runtime note: the loaded set is re-derived from the window (a trimmed skill leaves
    it), a verdict the picker finished is applied, queued skills (keyboard, picker)
    join the newest message, and the picker's suggestions come back as one line for
    the runtime note ('' when none)."""
    if not _skills.SKILLS:
        return ""
    left = _skills.sync_loaded(conversation_history, oldest_idx)
    if left:
        log.info("skill.evicted %s (left the window; can be loaded again)", ", ".join(left))
    verdict = _skills.PICKER.harvest()
    if verdict:
        _skills.apply_verdict(verdict)
    if _skills.PENDING and _has_pending_tool_calls(conversation_history):
        # A user turn here would strand the unanswered tool call; the load waits for
        # the next boundary, as a bus injection does.
        return _skills.take_suggestions()
    for name, text, by in _skills.take_pending():
        msg = _append_turn_text(conversation_history, messages_to_send, text)
        msg.setdefault("_skills", []).append(name)   # the window sync finds it here
        _emit("on_notice", "info", f"skill {name} loaded ({by})")
    return _skills.take_suggestions()


def _skills_on_entry(conversation_history, log):
    """At the start of a run: the newest user message not yet judged goes to the
    picker. The first of the session is judged synchronously before the first request
    (`pick: start` or `always`, else the keyword floor); later ones (`always`) on a
    worker thread, harvested at a turn boundary."""
    if not _skills.SKILLS:
        return
    msg = next((m for m in reversed(conversation_history)
                if m.get("role") == "user" and isinstance(m.get("content"), str)), None)
    if msg is None or msg.get("_skill_pick"):
        return
    msg["_skill_pick"] = True
    if not _skills.STATE["started"]:
        loads, suggests = _skills.pick_at_start(msg["content"])
        if loads or suggests:
            log.info("skills at start: load %s, suggest %s", loads or "-", suggests or "-")
    else:
        _skills.PICKER.kick(msg["content"], source="user")


def _tool_args_complete(tool_call):
    """True when a streamed tool call's arguments parse as JSON (empty counts as {})."""
    args = (tool_call.get("function") or {}).get("arguments") or "{}"
    try:
        json.loads(args)
        return True
    except (json.JSONDecodeError, TypeError):
        return False


def _safe_close(response):
    """Close a streaming response regardless of shape (Response or generator)."""
    closer = getattr(response, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass


# ── Text utilities ─────────────────────────────────────────────────────

_THINK_TAG_RE = re.compile(r'</?think>|<\|channel>thought\n.*?<channel\|>', re.DOTALL)


class _ReasoningRenderer:
    """Stream-aware renderer that wraps <think>…</think> blocks in a
    violet [Reasoning] header + dim body, emitting everything else normally.

    Handles tags split across delta chunks via a small pending buffer.
    The `writer` is a callable taking a pre-styled text chunk — in practice
    this is `lambda t: _emit("on_stream_chunk", t)` so the UI callback
    layer gets every printable chunk.
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"
    _MAX_PENDING = max(len(_OPEN), len(_CLOSE)) - 1  # 7

    def __init__(self, writer):
        self._write = writer
        self._pending = ""
        self._in_think = False

    def feed(self, chunk):
        buf = self._pending + chunk
        self._pending = ""
        while buf:
            if self._in_think:
                close_idx = buf.find(self._CLOSE)
                if close_idx == -1:
                    # Keep last few chars in case the close tag is splitting
                    if len(buf) > self._MAX_PENDING:
                        self._emit_think(buf[:-self._MAX_PENDING])
                        self._pending = buf[-self._MAX_PENDING:]
                    else:
                        self._pending = buf
                    return
                self._emit_think(buf[:close_idx])
                buf = buf[close_idx + len(self._CLOSE):]
                self._close_block()
            else:
                open_idx = buf.find(self._OPEN)
                if open_idx == -1:
                    if len(buf) > self._MAX_PENDING:
                        self._emit_plain(buf[:-self._MAX_PENDING])
                        self._pending = buf[-self._MAX_PENDING:]
                    else:
                        self._pending = buf
                    return
                self._emit_plain(buf[:open_idx])
                buf = buf[open_idx + len(self._OPEN):]
                self._open_block()

    def flush(self):
        if self._pending:
            if self._in_think:
                self._emit_think(self._pending)
            else:
                self._emit_plain(self._pending)
            self._pending = ""
        if self._in_think:
            self._close_block()

    def _emit_plain(self, text):
        if not text:
            return
        self._write(text)

    def _emit_think(self, text):
        if not text:
            return
        self._write(theme.dim(text))

    def _open_block(self):
        self._in_think = True
        self._write(theme.c(theme.VIOLET, "\n[Reasoning]\n", bold=True))

    def _close_block(self):
        self._in_think = False
        self._write(theme.c(theme.VIOLET, "\n[/Reasoning]\n", bold=True))


# The old names, for readers; the loop calls the module.
_FILE_ACTIONS = _ta.FILE_ACTIONS
_HARMONY_TOKEN_RE = _ta.HARMONY_TOKEN_RE
_THINK_LAUNDER_MIN_LEN = _ta.THINK_LAUNDER_MIN_LEN
_detect_context_laundering = _ta.detect_context_laundering
_detect_harmony_token = _ta.detect_harmony_token
_harmony_retry_hint = _ta.harmony_retry_hint
_sanitize_tool_args = _ta.sanitize_tool_args
_repair_tool_args = _ta.repair_tool_args
_salvage_tool_args = _ta.salvage_tool_args


# Some agents reference a small set of scratch files in their AGENT.md / CLAUDE.md
# that are expected to exist but are not in the repo. If such a file is mentioned
# with read intent and is missing, every startup logs a read error — measured at 23
# errors from one missing file across a single run. Declaring it here creates an
# empty placeholder instead.
#
# OPT-IN, and empty by default. This is a convention some setups use, not one this
# tool assumes: shipping a particular project's filenames as the default would make
# that project's layout the tool's expected shape, and every other user would be
# reading around somebody else's workflow. Set them under `bootstrap` in config:
#
#   "bootstrap": {"placeholder_files": ["notes/inbox.md"], "placeholder_dirs": ["state"]}
#
# Nothing is created unless it is BOTH declared here and referenced by the agent's
# own instructions file.
_KNOWN_PLACEHOLDER_FILES = ()
_KNOWN_PLACEHOLDER_DIRS = (
)


def _bootstrap_template_check(log):
    """Run on fresh session start (NOT continue_mode). Find AGENT.md / CLAUDE.md
    in cwd, scan it for references to the placeholder files declared under
    `bootstrap` in config, and create empty placeholders for any that are
    referenced but missing. Also creates declared directories if referenced.

    Returns a list of (path, action) tuples for logging. Empty list when nothing
    was done — including the default case, where nothing is declared.

    Conservative: ONLY touches files explicitly referenced in the agent's own
    cognitive instructions; never auto-creates anything the agent didn't
    declare. Operator-side gaps (uncommitted tools/, missing config) are
    surfaced as warnings, not silently fixed.
    """
    # Find the agent's cognitive instructions file (case-insensitive: agent.md / AGENT.md /
    # Agent.md all count; agent.md preferred over claude.md when both present).
    instructions_file = None
    try:
        _cwd_files = {f.lower(): f for f in os.listdir('.') if os.path.isfile(f)}
    except OSError:
        _cwd_files = {}
    for candidate in ("agent.md", "claude.md"):
        if candidate in _cwd_files:
            instructions_file = _cwd_files[candidate]
            break
    if not instructions_file:
        return []  # no instructions file to read declarations from

    try:
        with open(instructions_file, encoding='utf-8', errors='replace') as f:
            instructions = f.read()
    except OSError:
        return []

    actions = []

    _bs = _config.get("bootstrap") or {} if isinstance(_config, dict) else {}
    _files = _bs.get("placeholder_files")
    _dirs = _bs.get("placeholder_dirs")
    _files = tuple(str(f) for f in _files) if isinstance(_files, list) else _KNOWN_PLACEHOLDER_FILES
    _dirs = tuple(str(d) for d in _dirs) if isinstance(_dirs, list) else _KNOWN_PLACEHOLDER_DIRS
    if not _files and not _dirs:
        return []

    # Auto-create empty placeholder files referenced for reading
    for known in _files:
        if known in instructions and not Path(known).exists():
            try:
                Path(known).parent.mkdir(parents=True, exist_ok=True)
                Path(known).touch()
                actions.append((known, "created empty placeholder"))
                log.info(
                    "Bootstrap: %s referenced in %s but missing — created empty placeholder",
                    known, instructions_file,
                )
            except OSError as e:
                log.warning("Bootstrap: failed to create %s: %s", known, e)

    # Auto-create well-known directories
    for known_dir in _dirs:
        if f"{known_dir}/" in instructions and not Path(known_dir).exists():
            try:
                Path(known_dir).mkdir(parents=True, exist_ok=True)
                actions.append((known_dir + "/", "created directory"))
                log.info(
                    "Bootstrap: %s/ referenced in %s but missing — created directory",
                    known_dir, instructions_file,
                )
            except OSError as e:
                log.warning("Bootstrap: failed to create %s/: %s", known_dir, e)

    return actions


def _load_preamble_bundle(log):
    """Load and execute `.agent/preamble.json` if it exists.

    Schema:
        {
          "files": ["state/current-state.json", "state/focus.json", ...],
          "commands": ["git log --oneline -10", "ls state/memories", ...],
          "max_bytes_per_item": 8192   # optional, default 8192
        }

    All commands must match the read-only prefix regex used by the dedup
    classifier (no side-effect commands, no network, no writes). This keeps
    the preamble's behavior deterministic and side-effect-free.

    Returns the bundled result as a single string, or None if no preamble
    file or it failed to load. The agent-side caller wraps it in a system
    message and prepends to conversation_history at session start.

    Eliminates the 8-tool-call PERCEIVE boilerplate that both audit reports
    (two independent field audits) flagged as the highest-impact friction.
    """
    preamble_path = Path(".agent/preamble.json")
    if not preamble_path.exists():
        return None
    try:
        with open(preamble_path) as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Failed to load .agent/preamble.json: %s", e)
        return None
    if not isinstance(cfg, dict):
        log.warning(".agent/preamble.json must be an object, got %s", type(cfg).__name__)
        return None

    max_bytes = int(cfg.get("max_bytes_per_item", 8192))
    files_list = cfg.get("files", []) or []
    cmds_list = cfg.get("commands", []) or []
    if not isinstance(files_list, list) or not isinstance(cmds_list, list):
        log.warning(".agent/preamble.json: 'files' and 'commands' must be arrays")
        return None
    if not files_list and not cmds_list:
        log.debug(".agent/preamble.json is empty — no preamble to inject")
        return None

    sections = []

    # ── Files ──────────────────────────────────────────────────────────
    for entry in files_list:
        if not isinstance(entry, str):
            sections.append(f"## File: {entry!r}\n(skipped — must be a string path)")
            continue
        p = Path(entry)
        if not p.exists():
            sections.append(f"## File: {entry}\n(not present — agent may want to create or initialize)")
            continue
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                content = f.read(max_bytes + 1)
        except OSError as e:
            sections.append(f"## File: {entry}\n(read error: {e})")
            continue
        truncated = ""
        if len(content) > max_bytes:
            content = content[:max_bytes]
            truncated = f"\n... [truncated at {max_bytes} bytes]"
        sections.append(f"## File: {entry}\n```\n{content}\n```{truncated}")

    # ── Commands ───────────────────────────────────────────────────────
    for entry in cmds_list:
        if not isinstance(entry, str):
            sections.append(f"## Command: {entry!r}\n(skipped — must be a string)")
            continue
        # Same safety check the dedup classifier uses — read-only commands only,
        # no `>` / `>>` / `|tee` redirects.
        if not _is_safe_readonly_command(entry):
            sections.append(
                f"## Command: {entry}\n(skipped — preamble commands must be "
                f"side-effect-free: must start with a read-only prefix and contain "
                f"no `>` / `>>` / `|tee` redirects. Run writes via exec_command "
                f"during the cycle instead.)"
            )
            log.warning("Preamble command %r failed read-only check — skipped", entry)
            continue
        # Run via subprocess; tight timeout because preamble shouldn't be slow.
        try:
            import subprocess
            result = subprocess.run(
                entry, shell=True, capture_output=True, text=True, timeout=20,
            )
            out = (result.stdout or "")[:max_bytes]
            truncated = "\n... [truncated]" if len(result.stdout or "") > max_bytes else ""
            err_tail = ""
            if result.returncode != 0:
                err_tail = f"\n(exit={result.returncode}; stderr: {(result.stderr or '')[:200]!r})"
            sections.append(f"## Command: {entry}\n```\n{out}{truncated}\n```{err_tail}")
        except subprocess.TimeoutExpired:
            sections.append(f"## Command: {entry}\n(timed out at 20s — drop or simplify)")
        except Exception as e:
            sections.append(f"## Command: {entry}\n(error: {e})")

    bundled = "PERCEIVE preamble (auto-loaded from .agent/preamble.json):\n\n" + "\n\n".join(sections)
    log.info("Loaded preamble bundle: %d files, %d commands, %d chars total",
             len(files_list), len(cmds_list), len(bundled))
    return bundled


# Read-only exec_command prefixes safe to dedup (no side effects, no network,
# no wall-clock dependency). Conservative — anything not on this list is
# treated as side-effectful and dispatched normally.
_READONLY_CMD_RE = re.compile(
    r'^\s*(?:cd\s+\S+\s*&&\s*)?'  # tolerate a `cd X && ` prefix
    r'(?:cat|ls|head|tail|grep|find|wc|pwd|stat|which|file\b|du|df|tree|sort'
    r'|git\s+(?:status|log|diff|show|remote|branch|describe|rev-parse|ls-files|ls-remote|config\s+--get)'
    r'|gh\s+(?:pr|issue|repo|api)\s+(?:view|list|status))',
    re.IGNORECASE,
)


def _is_safe_readonly_command(cmd):
    """True iff `cmd` is a side-effect-free shell command we can safely dedup
    OR run in the preamble loader. Two checks: (1) starts with a known
    read-only prefix; (2) contains no output redirects (`>`, `>>`, `|tee`)
    that would convert an otherwise-read command into a write."""
    if not cmd or not isinstance(cmd, str):
        return False
    if not _READONLY_CMD_RE.match(cmd):
        return False
    # Strip stderr redirects (`2>&1`, `2>/dev/null`) before the redirect check —
    # those are typical and benign.
    cmd_no_stderr = re.sub(r'2>\S+|2>&\d+', '', cmd)
    if re.search(r'(?<!\\)>(?!=)|\btee\b', cmd_no_stderr):
        return False
    return True


def _is_dedupable_call(func_name, func_args):
    """Return True if this tool call is safe to dedup. Pure-read tools that
    don't touch network/wallclock/external state and whose result doesn't
    meaningfully change turn-over-turn within the dedup window.

    NEVER dedup writes — the agent might be legitimately retrying after an
    error with corrected args, and we don't want to mask that.
    """
    if func_name in ("sleep", "subagent", "web_fetch", "read_pdf"):
        return False  # network or wall-clock dependent
    if func_name == "think":
        return False  # `think` is opaque enough to let through; loop-detector
                      # at batch level catches think-spirals
    if func_name == "file":
        action = (func_args or {}).get("action", "")
        return action in ("read", "list")  # writes/edits are legit-retryable
    if func_name in ("read_file", "list_files"):
        return True  # per-action read tools are always dedupable
    if func_name in ("find_symbol", "search_files"):
        return True
    if func_name == "task_tracker":
        return (func_args or {}).get("action") == "list"
    if func_name == "exec_command":
        return _is_safe_readonly_command((func_args or {}).get("command", ""))
    return False  # default: do not dedup unknown tools


def _extract_write_target(func_name, func_args):
    """Return the path being written by a tool call, or None.

    Used by the write-loop detector to track repeated writes to the same
    file across turns. Covers `file` mutating actions + `exec_command`
    redirections (delegated to tools.exec_command._extract_write_target).
    """
    if not isinstance(func_args, dict):
        return None
    if func_name == "file":
        action = func_args.get("action", "")
        if action in ("write", "insert", "append", "delete", "edit"):
            return func_args.get("path")
    if func_name in ("write_file", "edit_file", "append_file"):
        return func_args.get("path")
    if func_name == "exec_command":
        cmd = func_args.get("command", "")
        if not cmd:
            return None
        try:
            from tools.exec_command import _extract_write_target as _ext_xc
            return _ext_xc(cmd)
        except Exception:
            return None
    return None


# ── Token estimation ───────────────────────────────────────────────────
# The estimator, the calibration ratio and the tool-schema size live in context_window.py
# and are read from there at call time (`_cw.estimate_tokens`, `_cw.TOKEN_CAL`,
# `_cw.TOOLS_TOKENS`): one value, wherever it is set.
_update_token_calibration, _calibrated_budget = _cw.update_token_calibration, _cw.calibrated_budget

# F3b — drift-free context accounting. The estimator (token_utils) is a fixed tokenizer /
# chars-per-token guess; every streamed turn returns the server's REAL prompt_tokens for what
# was actually sent. Keep an observed ratio (actual / estimated) and budget the next window
# with it. The heuristic is the first guess, not a permanent assumption; a tokenizer mismatch
# between the estimator and the served model shows up here as a ratio far from 1.0.
_PREFILL_TPS_FLOOR = float(os.environ.get("AGENT_PREFILL_TPS_FLOOR", "500") or 500)
# Cache-miss attribution state, for the PROCESS, not the run: the server's cache does
# not know that a new user prompt started a new run_agent_single, so neither may the
# log (measured 2026-10-30: a harvest right before a new prompt was logged as `first`).
# n = requests that reported cache usage; prev_sig = _request_signature of the last
# one; last_at = when it was sent, for the `idle` reason (the cache lives five minutes).
_CACHE_REQ = {"n": 0, "prev_sig": None, "last_at": 0.0, "prev_model": None}
_CACHE_IDLE_SECONDS = 300.0

# ── Prompt-cache lifetime record ─────────────────────────────────────────────
# How long a service keeps a cached prefix is not published for every model family
# and is not what the documentation says for some: Anthropic documents five minutes;
# the AI Commons kimi gateway held a prefix across 25-minute pauses and lost it by 44
# (measured 2026-10-31). The record learns from traffic, per backend kind and model:
# the longest idle gap after which a byte-identical prefix still hit (`hit_max_s`)
# and the shortest after which it missed (`miss_min_s`). It lives in .agent/state so
# a restart keeps it. The seeds below apply until a family has its own evidence.
_CACHE_LIFETIME_SEEDS = (("claude", 300.0), ("kimi", 1500.0))
_CACHE_LIFETIME_FILE = "cache_lifetimes.json"
_CACHE_LIFETIME_MIN_GAP_S = 60.0     # shorter gaps say nothing about the lifetime
_CACHE_LIFETIMES = None              # loaded on first use: {key: {hit_max_s, miss_min_s, hits, misses, updated}}


def _cache_lifetime_key(backend=None):
    """`kind:model` of the backend whose cache is being tracked."""
    b = backend if backend is not None else globals().get("_main_backend")
    return "%s:%s" % (getattr(b, "kind", None) or "?", getattr(b, "model", None) or "")


def _cache_lifetime_seed(key):
    """The lifetime to assume for a family with no evidence of its own."""
    model = key.split(":", 1)[-1].lower()
    for needle, seconds in _CACHE_LIFETIME_SEEDS:
        if needle in model:
            return seconds
    return _CACHE_IDLE_SECONDS


def _cache_lifetime_estimate(record, key):
    """Pure: seconds a cached prefix of `key` is known to survive (`alive_s`) and the
    gap after which it is known to be gone (`dead_s`, None when no miss has been seen
    beyond `alive_s`). A hit at a longer gap than the seed raises `alive_s`; a miss
    seen below the seed lowers it, since the seed was only a guess."""
    entry = (record or {}).get(key) or {}
    hit_max = float(entry.get("hit_max_s") or 0.0)
    miss_min = entry.get("miss_min_s")
    alive = max(hit_max, _cache_lifetime_seed(key))
    if miss_min is not None and float(miss_min) < alive:
        alive = max(hit_max, float(miss_min) * 0.9)
    return alive, (float(miss_min) if miss_min is not None else None)


def _cache_lifetime_observe(record, key, gap_s, hit, now=None):
    """Pure on `record` (mutated in place and returned): fold one observation in. A hit
    after `gap_s` proves the cache lived that long. A miss counts only when the gap is
    longer than every hit seen, because a miss inside the proven lifetime is the
    provider's (cold replica, lost cache), not an expiry; and a later hit beyond a
    recorded miss retires that miss the same way."""
    if record is None:
        record = {}
    if gap_s is None or gap_s < _CACHE_LIFETIME_MIN_GAP_S:
        return record
    entry = record.setdefault(key, {"hit_max_s": 0.0, "miss_min_s": None, "hits": 0, "misses": 0})
    gap_s = float(gap_s)
    if hit:
        entry["hits"] = int(entry.get("hits") or 0) + 1
        if gap_s > float(entry.get("hit_max_s") or 0.0):
            entry["hit_max_s"] = round(gap_s, 1)
        if entry.get("miss_min_s") is not None and gap_s >= float(entry["miss_min_s"]):
            entry["miss_min_s"] = None
    else:
        if gap_s <= float(entry.get("hit_max_s") or 0.0):
            return record
        entry["misses"] = int(entry.get("misses") or 0) + 1
        if entry.get("miss_min_s") is None or gap_s < float(entry["miss_min_s"]):
            entry["miss_min_s"] = round(gap_s, 1)
    stamp = time.time() if now is None else now
    entry["updated"] = datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return record


def _cache_lifetimes_load(path=None):
    """The record from disk (an empty one when there is none or it is unreadable)."""
    path = path or _state_path(_CACHE_LIFETIME_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _cache_lifetimes_save(record, path=None):
    """Best effort: the record is a cost optimisation, never a reason to fail a turn."""
    path = path or _state_path(_CACHE_LIFETIME_FILE)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


def _cache_lifetimes():
    """The process's record, loaded from .agent/state on first use."""
    global _CACHE_LIFETIMES
    if _CACHE_LIFETIMES is None:
        _CACHE_LIFETIMES = _cache_lifetimes_load()
    return _CACHE_LIFETIMES


def _cache_lifetime_record_request(gap_s, hit, log=None, key=None):
    """Fold the outcome of a request whose prefix was resent unchanged into the record
    and persist it. Returns (alive_s, dead_s) for the key afterwards."""
    key = key or _cache_lifetime_key()
    record = _cache_lifetimes()
    before = dict(record.get(key) or {})
    _cache_lifetime_observe(record, key, gap_s, hit)
    after = record.get(key) or {}
    if after != before:
        _cache_lifetimes_save(record)
        if log is not None:
            alive, dead = _cache_lifetime_estimate(record, key)
            log.info("cache.lifetime %s gap=%ds %s alive>=%ds dead%s", key, int(gap_s or 0),
                     "hit" if hit else "miss", int(alive), ("=%ds" % int(dead)) if dead else " unknown")
    return _cache_lifetime_estimate(record, key)


# ── Prompt-cache keepalive ───────────────────────────────────────────────────
# While the agent waits for input on a caching backend, the last request is re-sent
# unchanged with a one-token output limit before the cached prefix expires, so the
# next real request extends it instead of re-sending it uncached. Measured
# 2026-10-31 on AI Commons: cached input is billed at a twentieth of uncached, so a
# keepalive costs a twentieth of the miss it prevents, as long as the pause stays
# under context.cache_keepalive_max_idle_s; beyond that the collapse is cheaper.
_LAST_REQUEST = {"body": None}      # the last request body sent on a caching backend, as JSON
_KEEPALIVE = {"stop": None, "thread": None}
_KEEPALIVE_MIN_INTERVAL_S = 60.0
_KEEPALIVE_FRAC = 0.8               # of the proven lifetime, so a late reply still lands inside it
_KEEPALIVE_STRETCH = 1.2            # a probe waits this much longer than proven, to learn more
_KEEPALIVE_RETRY_BEYOND_MISS = 10   # only every tenth probe reaches past a recorded miss


def _cache_keepalive_plan(alive_s, dead_s, n, probe_every):
    """Pure: seconds the `n`-th keepalive (from 1) waits. Normally a fraction of the
    proven lifetime; every `probe_every`-th one stretches past it to learn whether the
    cache lives longer, bounded by a recorded death except now and then, so that a
    single provider miss cannot pin the lifetime for good."""
    interval = max(_KEEPALIVE_MIN_INTERVAL_S, float(alive_s) * _KEEPALIVE_FRAC)
    if not probe_every or n % int(probe_every):
        return interval
    probe_n = n // int(probe_every)
    target = max(interval, float(alive_s) * _KEEPALIVE_STRETCH)
    if dead_s is not None and target >= float(dead_s) and probe_n % _KEEPALIVE_RETRY_BEYOND_MISS:
        return max(interval, float(dead_s) * 0.85)
    return target


def _cache_keepalive_loop(stop, send, log, key, max_idle_s, probe_every, now=time.time):
    """Run keepalives until the run resumes (`stop` is set), the idle cap passes, the
    model changes under us, two keepalives miss (the server is not caching this
    prefix) or one fails. `send()` re-sends the last request and returns its usage.
    Returns why it stopped, for the log and the tests."""
    started = now()
    n = 0
    misses = 0
    while True:
        n += 1
        alive_s, dead_s = _cache_lifetime_estimate(_cache_lifetimes(), key)
        wait_s = _cache_keepalive_plan(alive_s, dead_s, n, probe_every)
        if now() + wait_s - started > float(max_idle_s):
            log.info("cache.keepalive off after %d: the next would pass the %ds idle cap", n - 1, int(max_idle_s))
            return "cap"
        if stop.wait(wait_s):
            return "run"
        if _cache_lifetime_key() != key:
            return "model"
        gap_s = (now() - _CACHE_REQ["last_at"]) if _CACHE_REQ["last_at"] > 0 else None
        try:
            usage = send() or {}
        except Exception as e:  # noqa: BLE001
            log.warning("cache.keepalive %d failed, stopping: %s", n, e)
            return "error"
        read = int(usage.get("cache_read_input_tokens") or 0)
        write = int(usage.get("cache_creation_input_tokens") or 0)
        unc = int(usage.get("input_tokens") or 0)
        total = read + write + unc
        hit = bool(total) and read * 2 >= total
        log.info("cache.keepalive %d gap=%ds read=%d write=%d uncached=%d %s", n,
                 int(gap_s or 0), read, write, unc, "hit" if hit else "MISS")
        try:
            _cache_lifetime_record_request(gap_s, hit, log, key=key)
        except Exception as e:  # noqa: BLE001
            log.debug("cache.lifetime record skipped: %s", e)
        _CACHE_REQ["last_at"] = now()    # the server holds the prefix again either way
        if not hit:
            misses += 1
            if misses >= 2:
                log.info("cache.keepalive off: two misses, the server is not keeping this prefix")
                return "miss"


def _cache_keepalive_enabled():
    c = _config.get("context", {})
    return bool(c.get("cache_keepalive", False)) and _prompt_caching_backend()


def _cache_keepalive_disarm():
    """Stop the keepalive thread (a run is starting); never blocks on a request in flight."""
    stop = _KEEPALIVE.get("stop")
    if stop is not None:
        stop.set()
    _KEEPALIVE["stop"] = None
    _KEEPALIVE["thread"] = None


def _cache_keepalive_arm(log):
    """Start keepalives for the last request sent, if there is one and the backend
    caches. Called when a run ends and the agent goes back to waiting for input."""
    _cache_keepalive_disarm()
    if not _cache_keepalive_enabled() or not _LAST_REQUEST.get("body"):
        return False
    c = _config.get("context", {})
    try:
        max_idle_s = float(c.get("cache_keepalive_max_idle_s", 3600) or 0)
        probe_every = int(c.get("cache_keepalive_probe_every", 4) or 0)
    except (TypeError, ValueError):
        max_idle_s, probe_every = 3600.0, 4
    if max_idle_s <= 0:
        return False
    key = _cache_lifetime_key()
    body_json = _LAST_REQUEST["body"]
    stop = threading.Event()

    def _send():
        body = json.loads(body_json)
        body["_cache_keepalive"] = True
        usage = {}
        for chunk in _iter_stream_chunks(_llm_request(log, json=body, stream=True, timeout=(30, 120))):
            if isinstance(chunk, dict) and chunk.get("usage"):
                usage = chunk["usage"]
        return usage

    def _worker():
        try:
            why = _cache_keepalive_loop(stop, _send, log, key, max_idle_s, probe_every)
            log.debug("cache.keepalive thread done: %s", why)
        except Exception as e:  # noqa: BLE001
            log.debug("cache.keepalive thread error: %s", e)

    t = threading.Thread(target=_worker, name="cache-keepalive", daemon=True)
    _KEEPALIVE["stop"], _KEEPALIVE["thread"] = stop, t
    t.start()
    return True


def _stall_budget_s(base_s, est_prompt_tokens, prefill_tps_floor):
    """Pure: the zero-delta window a request may stay silent before it is called a stall.
    A backend emits no deltas during prefill, so the window must cover prefill for a prompt
    of this size at a floor rate the backend beats. base_s <= 0 disables (returns 0); an
    unknown estimate or floor keeps the base."""
    try:
        base_s = float(base_s)
    except (TypeError, ValueError):
        return 0.0
    if base_s <= 0:
        return 0.0
    try:
        est = float(est_prompt_tokens or 0)
        floor = float(prefill_tps_floor or 0)
    except (TypeError, ValueError):
        return base_s
    if est <= 0 or floor <= 0:
        return base_s
    return max(base_s, est / floor)


_EXTENDED_KEYWORDS = frozenset({
    "plan", "design", "architect", "refactor", "implement", "rewrite",
    "explain in detail", "write tests", "analyse", "analyze", "migrate",
    "debug", "investigate", "benchmark", "optimize",
})

def _classify_turn_complexity(messages: list[dict]) -> str:
    """
    Classify a turn as 'simple' | 'standard' | 'extended'.
    Inspects the last user message text and count of tool results.
    """
    tool_result_count = sum(
        1 for m in messages
        if isinstance(m.get("content"), list)
        and any(c.get("type") == "tool_result" for c in m["content"])
    )
    if tool_result_count > 4:
        return "extended"

    user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, str):
                user_text = content
            elif isinstance(content, list):
                user_text = " ".join(
                    c.get("text", c.get("body", ""))
                    for c in content
                    if isinstance(c, dict) and c.get("type") in ("text", None)
                )
            break

    lower = user_text.lower()
    if any(kw in lower for kw in _EXTENDED_KEYWORDS):
        return "extended"
    if "```" in user_text or len(user_text) > 400:
        return "extended"
    if tool_result_count > 2:
        return "extended"
    if tool_result_count > 0 or len(user_text) > 100:
        return "standard"
    return "simple"


_COMPLEXITY_MAX_TOKENS = {
    "claude":        {"simple": 512,  "standard": 2048, "extended": 4096},
    "llama":         {"simple": 512,  "standard": 1536, "extended": 2048},
    "mistral":       {"simple": 512,  "standard": 1536, "extended": 2048},
    "mixtral":       {"simple": 512,  "standard": 1536, "extended": 2048},
    "amazon-nova":   {"simple": 512,  "standard": 1536, "extended": 4096},
    "deepseek-r1":   {"simple": 1024, "standard": 2048, "extended": 4096},
    "qwen3":         {"simple": 512,  "standard": 1536, "extended": 2048},
    "_default":      {"simple": 512,  "standard": 2048, "extended": 4096},
}


def _get_adaptive_max_tokens(model: str, complexity: str) -> int:
    """Return the max_tokens budget for a given model prefix and complexity class."""
    for prefix in sorted(_COMPLEXITY_MAX_TOKENS, key=len, reverse=True):
        if prefix != "_default" and model.startswith(prefix):
            return _COMPLEXITY_MAX_TOKENS[prefix][complexity]
    return _COMPLEXITY_MAX_TOKENS["_default"][complexity]


# ── File reference expansion and the lead (lead.py) ───────────────────────────
# Kept under their old names here: the loop, the checkpoint code and the tests reach
# them as agent._lead_place, agent._LEAD and so on. Same objects, not copies.
_LEAD = _lead.LEAD
_LEAD_HEADER_RE = _lead.LEAD_HEADER_RE
_expand_file_refs, _expand_file_refs_ex = _lead.expand_file_refs, _lead.expand_file_refs_ex
_attach_file_refs, _lead_refs_from_text = _lead.attach_file_refs, _lead.refs_from_text
_lead_update, _lead_block, _lead_span = _lead.update, _lead.block, _lead.span
_lead_note, _lead_place, _lead_refs_of = _lead.note, _lead.place, _lead.refs_of
_lead_absorb, _lead_reload, _same_path = _lead.absorb, _lead.reload, _lead.same_path

def _spill_initial_prompt(expanded, files, log):
    """F3a for the first message: typed bulk past `limits.tool_result_max_chars` goes to
    a file with a reference; the files it attached stay, since an identity file is
    loaded whole by design and is the place the lead lives for the whole session."""
    cap = _tool_result_spill_cap()
    if cap <= 0 or not isinstance(expanded, str):
        return expanded
    typed, tail = expanded, ""
    if files and isinstance(files, str):
        body = expanded.rstrip("\n")
        attached = "\n\n" + files.rstrip("\n")
        if body.endswith(attached):
            typed, tail = body[:-len(attached)], expanded[len(body) - len(attached):]
    if len(typed) <= cap:
        return expanded
    ref = _spill_tool_result(0, "prompt", typed, log, kind="prompt")
    if ref is None:
        return expanded
    log.info("F3a spill: initial prompt of %d chars -> file (cap %d)", len(typed), cap)
    return ("[Your initial instructions were too large for the context window and were "
            "saved to a file — read them from there before acting.]\n" + ref + tail)


# ── Context window management ─────────────────────────────────────────


def _build_context_footnote(summary_text, initial_files, volatile=None):
    """Build the synthetic context-restoration user message.

    Returns a dict suitable for insertion at the start of the messages list
    when some history has been dropped from the context window.  Always
    includes the TOOL RULE hint so agents in condensed-summary sessions keep
    the guidance about how to write JSON files.

    Args:
        summary_text: The current progress-summary string (non-empty).
        initial_files: Optional string of initial file content to prepend.
        volatile: whether per-turn state (the CICD phase checkpoint) may ride in
            this message. Default: only on backends without prompt caching. On a
            caching backend this message leads every request, so it must be
            byte-stable between evictions; the checkpoint rides in the trailing
            runtime note instead (measured 2026-10-29: every commit flipped a
            phase flag here and re-wrote a 66k-token prefix).
    """
    if volatile is None:
        volatile = not _prompt_caching_backend()
    parts = []
    if initial_files:
        parts.append(initial_files)
    parts.append(f"Progress summary of work done so far:\n{summary_text}")
    parts.append(
        f"IMPORTANT: Your working directory is '{os.getcwd()}'. "
        "Use relative paths (e.g. '.agent/state/file.json') — do not cd elsewhere. "
        "Continue where you left off. Do not repeat already-completed steps. "
        "TOOL RULE: To write JSON files, use exec_command with heredoc "
        "(e.g. cat > file.json << 'EOF'\\n...\\nEOF). "
        "Do NOT use the file tool with action='write' for JSON content."
    )
    if _pinned_instructions:
        parts.append(f"PINNED INSTRUCTIONS (always follow these):\n{_pinned_instructions}")
    # CICD phase checkpoint — injected here so it survives summary compression
    if volatile:
        phase_line = _cicd.checkpoint_text()
        if phase_line:
            parts.append(phase_line)
    return {"role": "user", "content": "\n\n".join(parts)}


# ── Memory pressure (memory_pressure.py) ──────────────────────────────────────
# The watermark and the spill. Old names kept for the loop and the tests; the limits a
# test lowers are memory_pressure.MEM_WARN_MB / MEM_HARD_MB.
_read_vmrss_mb, _release_memory = _mem.read_vmrss_mb, _mem.release_memory
_check_memory_watermark = _mem.check_memory_watermark
_SPILL_DIR, _SPILL_MARKER, _SPILL_PREVIEW = _mem.SPILL_DIR, _mem.SPILL_MARKER, _mem.SPILL_PREVIEW
_SPILL_THRESHOLD, _SPILL_MIN_THRESHOLD = _mem.SPILL_THRESHOLD, _mem.SPILL_MIN_THRESHOLD
_spill_tool_result, _spill_oversized_tool_results = _mem.spill_tool_result, _mem.spill_oversized_tool_results


def _mem_hard_exit(log, why):
    """Over the hard memory limit: the spend summary, then the clean exit."""
    try:
        _log_bedrock_session_spend(log)
    except NameError:
        pass            # not defined under a reorder; the exit still happens
    _exit_now(EXIT_MEMORY, why)   # F6: 13 in -a mode


if _FIRST_LOAD:
    _mem.configure(hard_limit_exit=_mem_hard_exit)


def _tool_result_spill_cap():
    """limits.tool_result_max_chars as a validated int (0 = spill off). A value that is not a
    real integer — a bool, a string, a test double (int(MagicMock()) == 1 and spilled every
    10-char result in the suite) — falls back to the legacy boundary."""
    try:
        v = (_config.get("limits") or {}).get("tool_result_max_chars", _MAX_TOOL_RESULT_CHARS)
    except Exception:  # noqa: BLE001
        return _MAX_TOOL_RESULT_CHARS
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return _MAX_TOOL_RESULT_CHARS
    return max(0, int(v))


# ── Context window (context_window.py) ────────────────────────────────────────
# The builder keeps its old names here for the loop and the tests; the shared state
# (estimate_tokens, TOKEN_CAL, TOOLS_TOKENS, MAX_CONTEXT_MESSAGES) is read through
# the module at call time, never through a copy.
_TRIMMED_LEAD = _cw.TRIMMED_LEAD
_build_initial_files_msg, _context_budget = _cw.build_initial_files_msg, _cw.context_budget
_fill_window, _evicted_edge, _aligned_head = _cw.fill_window, _cw.evicted_edge, _cw.aligned_head
_lead_tokens, _collapse_edge, _log_lead = _cw.lead_tokens, _cw.collapse_edge, _cw.log_lead
_build_context = _cw.build_context


def _cache_alive_s():
    """The proven cache lifetime of the current backend, for the window builder."""
    alive, _ = _cache_lifetime_estimate(_cache_lifetimes(), _cache_lifetime_key())
    return alive


def _last_request_at():
    return _CACHE_REQ["last_at"]


if _FIRST_LOAD:
    _cw.configure(max_context_messages=_config["context"]["max_context_messages"], tool_list=tools,
                  footnote=_build_context_footnote, condense_summary=_summ.condense_summary,
                  prompt_caching=_prompt_caching_backend, summary_ahead_frac=_summ.summary_ahead_frac,
                  collapse_frac=_cache_collapse_frac, cache_alive_s=_cache_alive_s,
                  last_request_at=_last_request_at)


# ── Logger setup ──────────────────────────────────────────────────────

class _CurrentStdoutHandler(logging.StreamHandler):
    """Writes to whatever ``sys.stdout`` is at emit time.

    A plain StreamHandler keeps the stream it was built with. While the live
    prompt runs, prompt_toolkit's patch_stdout swaps ``sys.stdout`` for a proxy
    that prints above the prompt area; a line written to the original stream
    instead lands inside that area, the next redraw erases from the wrong row,
    and the half-streamed reply line is left behind as a stale copy (seen with
    the first-call "no prices" warning, which the redraw then painted over)."""

    @property
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, _value):
        pass


class _VerboseConsoleFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        cb = globals().get("_cb")
        return bool(cb and getattr(cb, "verbose", False))


def _setup_logger():
    """Create a structured logger with levels, rotation, and console output."""
    log_dir_override = _config.get("log_dir")
    # Only a non-empty STRING is a directory. Any other truthy value (a mock
    # object under test, a mis-typed config) used to be joined into a path and
    # created on disk: one test double grew an 11,503-file directory named
    # after itself over four months, and every search-tool test then read it.
    if isinstance(log_dir_override, str) and log_dir_override.strip():
        history_dir = os.path.join(os.getcwd(), log_dir_override.strip())
    else:
        history_dir = _HISTORY_DIR
    os.makedirs(history_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_prefix = _config.get("log_prefix", "session")
    log_path = os.path.join(history_dir, f"{log_prefix}_{timestamp}.log")
    error_log_path = os.path.join(history_dir, "errors.log")

    logger = logging.getLogger("agent")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    console_handler = _CurrentStdoutHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    console_handler.addFilter(_VerboseConsoleFilter())
    logger.addHandler(console_handler)

    # encoding="utf-8" is required: log messages contain non-ASCII (e.g. the
    # "→" arrow). Without it, RotatingFileHandler opens the file in the
    # platform-default encoding (cp1252 on Windows), which raises
    # UnicodeEncodeError on those characters. errors="replace" is belt-and-
    # suspenders for any codepoint utf-8 somehow can't represent.
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10*1024*1024, backupCount=5,
        encoding="utf-8", errors="replace")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(file_handler)

    error_handler = logging.handlers.RotatingFileHandler(
        error_log_path, maxBytes=5*1024*1024, backupCount=3,
        encoding="utf-8", errors="replace")
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(logging.Formatter('%(asctime)s ERROR %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(error_handler)

    return logger, log_path, error_log_path


# ── Conversation checkpoints (checkpoint.py) ──────────────────────────────────
# The file `-c` resumes from and the .agent/ directories. Old names kept for the loop
# and the tests; the path a test redirects is checkpoint.CHECKPOINT_PATH.
_AGENT_DIR, _STATE_DIR, _HISTORY_DIR = _ckpt.AGENT_DIR, _ckpt.STATE_DIR, _ckpt.HISTORY_DIR
_state_path, _in_git_repo = _ckpt.state_path, _ckpt.in_git_repo
_ensure_gitignored, _warn_if_config_tracked = _ckpt.ensure_gitignored, _ckpt.warn_if_config_tracked
_ensure_agent_dirs = _ckpt.ensure_agent_dirs
_strip_checkpoint_reads, _prune_old_thinking = _ckpt.strip_checkpoint_reads, _ckpt.prune_old_thinking
_save_checkpoint, _load_checkpoint, _delete_checkpoint = _ckpt.save, _ckpt.load, _ckpt.delete
_ckpt.ensure_agent_dirs()

def _seed_phase_tasks(config, log):
    """Pre-load phase tasks from config if no open tasks exist.

    If preferences.initial_tasks is set and there are no currently open
    tasks (either first run or previous cycle fully completed), seeds the
    task list from the config so the agent starts every cycle with a
    structured checklist.  If open tasks remain (interrupted cycle), does
    nothing — the agent continues from where it left off.

    Seeded tasks are ephemeral by default (AC5 of #1028). Set
    ``preferences.seed_tasks_persistent: true`` in ``.agent/config.json``
    to make phase tasks persistent across sessions (AC6); in that mode
    seeding is idempotent — descriptions already present as open tasks
    are skipped instead of duplicated (AC7).
    """
    task_descs = config.get("preferences", {}).get("initial_tasks", [])
    if not task_descs:
        return
    seed_persistent = bool(config.get("preferences", {}).get("seed_tasks_persistent", False))
    try:
        from tools.task_tracker import get_tasks, fn as _tt_fn
        existing = get_tasks()
        open_tasks = [t for t in existing if t.get("status") not in ("done", "completed")]

        if seed_persistent:
            open_descs = {t.get("description") for t in open_tasks}
            seeded = 0
            for desc in task_descs:
                if desc in open_descs:
                    log.debug("seed_phase_tasks: skip duplicate persistent task %r", desc)
                    continue
                result = _tt_fn("add", description=desc, persistent=True)
                log.debug("seed_phase_tasks: %s", result)
                seeded += 1
            if seeded:
                log.info("seed_phase_tasks: seeded %d persistent phase task(s)", seeded)
            return

        if open_tasks:
            log.debug("seed_phase_tasks: %d open task(s) remain — skipping seed", len(open_tasks))
            return
        for desc in task_descs:
            result = _tt_fn("add", description=desc)
            log.debug("seed_phase_tasks: %s", result)
        log.info("seed_phase_tasks: seeded %d phase task(s)", len(task_descs))
    except Exception as exc:
        log.warning("seed_phase_tasks: failed — %s", exc)


def _auto_close_ephemeral_tasks(log):
    """Close ephemeral open tasks at session-start and return a resume summary.

    Implements AC3 + AC4 of #1028. Open tasks without ``persistent: True``
    are transitioned to ``auto_closed`` (AC3). If anything was closed or
    any persistent tasks remain open, a formatted "[Session resume]"
    block is returned (AC4); otherwise ``None``.

    Never raises — failures are logged at WARNING so a corrupted or
    locked tasks file cannot break agent startup.
    """
    try:
        from tools.task_tracker import auto_close_ephemeral
        closed, persistent_open = auto_close_ephemeral()
    except Exception as exc:
        log.warning("auto_close_ephemeral_tasks: failed — %s", exc)
        return None
    if closed:
        log.info("auto_close_ephemeral_tasks: closed %d ephemeral task(s)", len(closed))
    return _build_session_resume_summary(closed, persistent_open)


def _build_session_resume_summary(closed, persistent_open):
    """Format the [Session resume] block, or return None if nothing to report."""
    if not closed and not persistent_open:
        return None
    lines = ["[Session resume]"]
    if closed:
        lines.append(f"{len(closed)} ephemeral task(s) auto-closed from last session:")
        for t in closed:
            tid = t.get("id", "?")
            desc = t.get("description", "(no description)")
            lines.append(f"  #{tid} [auto_closed] {desc}")
    if persistent_open:
        if closed:
            lines.append("")
        lines.append(f"{len(persistent_open)} persistent task(s) still open:")
        for t in persistent_open:
            tid = t.get("id", "?")
            desc = t.get("description", "(no description)")
            status = t.get("status", "open")
            lines.append(f"  #{tid} [{status}] {desc}")
    return "\n".join(lines)


def _build_open_task_nudge():
    """Return a reminder string if open tasks remain in task_tracker, else None."""
    try:
        from tools.task_tracker import get_tasks
        open_tasks = [
            t for t in get_tasks()
            if t.get("status") not in ("done", "completed", "auto_closed")
        ]
        if not open_tasks:
            return None
        lines = [
            f"You signalled cycle completion but {len(open_tasks)} task(s) are still open:"
        ]
        for t in open_tasks:
            tid = t.get("id", "?")
            desc = t.get("description", "(no description)")
            status = t.get("status", "pending")
            lines.append(f"  #{tid} [{status}] {desc}")
        lines.append(
            "Do not end the cycle yet — address the remaining tasks above. "
            "Use task_tracker(action='list') to review them, then take the next action."
        )
        return "\n".join(lines)
    except Exception:
        return None


# ── Cycle auto-increment ─────────────────────────────────────────────

def _auto_increment_cycle(log):
    """Check if the current cycle was already committed and bump if so.

    Compares the cycle number in .agent/state/current-state.json against the
    latest git log entries.  If a 'C<N>:' commit exists for the current
    cycle, the state file is incremented to N+1 so the agent starts the
    next cycle instead of repeating.

    Safety: only bumps if the state file is clean (matches the last commit).
    If the file has uncommitted changes, a previous session may have already
    bumped it — touching it again would cause a double-increment/skip.
    """
    state_path = _state_path("current-state.json")
    try:
        if not os.path.exists(state_path):
            return
        with open(state_path, encoding="utf-8", errors="replace") as f:
            state = json.load(f)
        cycle = int(state.get("cycle", 0))
        if cycle <= 0:
            return

        # Check recent git log for committed cycles
        result = subprocess.run(
            ["git", "log", "--oneline", "-20"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return

        # Find the highest committed cycle number from 'C<N>:' patterns
        committed_cycles = set()
        for line in result.stdout.strip().split("\n"):
            m = re.search(r'\bC(\d+):', line)
            if m:
                committed_cycles.add(int(m.group(1)))

        if not committed_cycles:
            return

        highest_committed = max(committed_cycles)

        # Only bump if current cycle has been committed (or is behind)
        if cycle <= highest_committed:
            new_cycle = highest_committed + 1
            state["cycle"] = new_cycle
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
                f.write("\n")

            # Also bump focus.json if it exists and matches old cycle
            focus_path = os.path.join(os.getcwd(), "state", "focus.json")
            if os.path.exists(focus_path):
                try:
                    with open(focus_path, encoding="utf-8", errors="replace") as f:
                        focus = json.load(f)
                    if int(focus.get("cycle", 0)) <= cycle:
                        focus["cycle"] = new_cycle
                        with open(focus_path, "w", encoding="utf-8") as f:
                            json.dump(focus, f, indent=2)
                            f.write("\n")
                except Exception:
                    pass

            log.info("AUTO-INCREMENT: cycle %d already committed, bumped state to %d",
                    cycle, new_cycle)
            _emit("on_cycle_bumped", cycle, new_cycle)
    except Exception as e:
        log.warning("Auto-increment check failed: %s", e)


# ── Main agent loop ───────────────────────────────────────────────────

def _backend_for_url(base_url):
    """Return the configured backend whose ``base_url`` matches, or a
    transient llamacpp backend if none matches.

    Plan task 1.5: health/ctx/list-models delegate to the matching backend
    so future kinds (bedrock) participate automatically. For base_urls that
    don't correspond to a configured backend, fall back to a throwaway
    llamacpp probe — matches the pre-refactor behavior (any llama-server
    URL can be probed).
    """
    if _main_backend.base_url == base_url:
        return _main_backend
    if _summary_backend.base_url == base_url:
        return _summary_backend
    return _build_backend({"kind": "llamacpp", "base_url": base_url, "model": ""})


def _startup_health_timeout() -> int:
    """Timeout (seconds) for the startup backend health probes.

    Longer than the per-request 3s default because cold-start endpoints — e.g.
    an AWS API Gateway / Lambda that has scaled to zero — can take several
    seconds to answer the first request, then respond instantly once warm. A
    too-tight probe spuriously reports the backend as ``timeout`` at launch.
    Override with the ``AGENT_HEALTH_TIMEOUT`` env var (seconds).
    """
    try:
        return max(1, int(os.environ.get("AGENT_HEALTH_TIMEOUT", "10")))
    except (TypeError, ValueError):
        return 10


def _sync_config_model(backend, config) -> None:
    """Sync ``config['llm']['model']`` to the backend's active model name.

    The startup banner shows ``backend.model`` (an auto-detected gateway model,
    say), but the TUI footer reads ``config['llm']['model']`` — which stays the
    llamacpp config default unless kept in sync. This makes the footer match the
    banner. No-op when the backend has no model name (banner falls back to the
    config value too, so they still agree).
    """
    model = getattr(backend, "model", None)
    if model:
        config.setdefault("llm", {})["model"] = model


def _check_api_health(base_url, timeout=3):
    """Probe the LLM endpoint. Return (ok: bool, detail: str)."""
    return _backend_for_url(base_url).health(timeout=timeout)


def _detect_ctx_size(base_url, timeout=3):
    """Query the backend for context size. Returns ``n_ctx`` or ``None``."""
    return _backend_for_url(base_url).detect_ctx_size(timeout=timeout)


def _list_available_models(base_url, timeout=3):
    """Query the backend for available model ids, or ``[]``."""
    return _backend_for_url(base_url).list_models(timeout=timeout)


def _render_context_bar(history, summary_state, ctx_size, width=30):
    """Return a multi-line string showing current context usage with a bar."""
    body_tokens = sum(_cw.estimate_tokens(m) for m in history) if history else 0
    summary_text = summary_state.get("text", "") or ""
    summary_tokens = _cw.estimate_tokens({"role": "system", "content": summary_text}) if summary_text else 0
    total = body_tokens + summary_tokens
    pct = total / ctx_size if ctx_size else 0.0
    bar = theme.bar(pct, width=width)
    pct_str = f"{pct*100:.1f}%"
    skills_line = ""
    if _skills.LOADED:
        skills_line = (f"\n  skills:  {_skills.loaded_tokens()} tokens in {len(_skills.LOADED)} "
                       f"loaded ({', '.join(_skills.LOADED)})")
    return (
        f"{bar} {pct_str}\n"
        f"  history: {body_tokens} tokens in {len(history)} messages\n"
        f"  summary: {summary_tokens} tokens\n"
        f"  budget:  {total} / {ctx_size} tokens" + skills_line
    )


def _pick_model_interactive(current_model, base_url):
    """Interactive model picker. Returns the chosen model id or None."""
    models = _list_available_models(base_url)
    if not models:
        _emit("on_notice", "warn", theme.c(theme.ROSE, "Could not list models from the configured backend"))
        return None
    _emit("on_notice", "info", theme.c(theme.SKY, "Available models:"))
    for i, m in enumerate(models, 1):
        marker = theme.c(theme.MINT, " *") if m == current_model else "  "
        _emit("on_notice", "info", f"{marker} {i}. {m}")
    try:
        # Prompt printed as its own line: under the terminal borrow a
        # promptless-newline input() prompt never renders (QA 2026-08-15,
        # same class as the /setup wizard's _borrow_input fix).
        print("Pick a model number (blank to cancel):")
        choice = input("").strip()
    except (EOFError, KeyboardInterrupt):
        _emit("on_notice", "info", "")
        return None
    if not choice:
        return None
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(models):
            return models[idx]
    except ValueError:
        pass
    _emit("on_notice", "warn", theme.c(theme.ROSE, "Invalid selection."))
    return None


def _apply_setup_updates(updates):
    """Apply a /setup result to the LIVE session (setup_wizard already
    persisted it to .agent/config.json).

    Deep-merges the updates into ``_config``, re-synthesizes the backends
    registry from the fresh legacy blocks (the loaded registry was built from
    the OLD blocks and must not pass through), rebuilds the live backend
    objects, and keeps the footer/model + BASE_URL in sync — the same
    contract /model honors, extended to endpoints and context knobs.
    Returns a short human summary of what was rebuilt.
    """
    global _main_backend, _summary_backend, BASE_URL

    def merge(dst, src):
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                merge(dst[k], v)
            else:
                dst[k] = v

    merge(_config, updates)
    rebuilt = []
    if "llm" in updates or "summary" in updates:
        _config["backends"] = _cfg.synthesize_backends_registry(
            {k: v for k, v in _config.items() if k != "backends"})
        # The wizard writes a role's whole endpoint: for a gateway kind, any
        # base_url/key/model it didn't write is a llamacpp default or the previous
        # kind's, and would outrank the gateway's own credentials.
        for role, section in (("main", "llm"), ("summary", "summary")):
            if isinstance(updates.get(section), dict):
                _cfg.drop_llamacpp_defaults(_config["backends"][role], updates[section])
        if "llm" in updates:
            _main_backend = _build_backend(_cfg.with_role(_config["backends"], "main"))
            if getattr(_main_backend, "model", None):
                _config["llm"]["model"] = _main_backend.model
            BASE_URL = _config["llm"].get("base_url", BASE_URL)
            rebuilt.append("main backend")
        if "summary" in updates:
            _summary_backend = _build_backend(
                _cfg.with_role(_config["backends"], "summary"))
            rebuilt.append("summary backend")
    if "context" in updates:
        # These were snapshotted into module globals at import — refresh the
        # ones the run loop reads live. The session's ctx_size budget itself
        # is fixed at boot (passed by value into the loop), so a changed
        # ctx_size lands on the NEXT start; say so rather than half-apply.
        c = _config.get("context", {})
        for gname, key in (("_MAX_FULL_LINES", "max_full_lines"),
                           ("_PREVIEW_LINES", "preview_lines")):
            if key in c:
                globals()[gname] = c[key]
        if "summary_max_chars" in c:
            _summ.SUMMARY_MAX_CHARS = c["summary_max_chars"]
        if "max_context_messages" in c:
            _cw.MAX_CONTEXT_MESSAGES = int(c["max_context_messages"])
        if "summary_threshold" in c or "max_context_messages" in c:
            _summ.SUMMARY_THRESHOLD = _summ.sane_summary_threshold(c)
        rebuilt.append("context knobs (ctx_size budget: next start)")
    if "preferences" in updates:
        rebuilt.append("preferences")
    return ", ".join(rebuilt) or "config only"


def _log_bedrock_session_spend(log):
    """Emit an INFO line summarizing today's Bedrock spend per role.

    Fires at session end so operators grep-ing the session log see an
    at-a-glance spend snapshot even when ``bedrock.cost.tick`` is at
    DEBUG (plan § 15.75 telemetry default). Silent when no Bedrock
    backend is in use. AI Commons backends log their session cache totals
    (P4.5) and quota/spend (P6) here too.
    """
    _seen = set()
    for backend in (_main_backend, _summary_backend):
        if (getattr(backend, "kind", None) == "aicommons" and id(backend) not in _seen
                and not getattr(backend, "_session_logged", False)):
            _seen.add(id(backend))
            backend._session_logged = True   # the exit path can run this twice
            try:
                log.info("%s", backend.session_summary())
            except Exception as _e:  # noqa: BLE001 — never fail an exit path over a log line
                log.debug("aicommons session summary skipped: %s", _e)
    try:
        from llm_backend import (
            _load_today_spend as _load,
            _resolve_daily_cap as _cap,
        )
    except Exception:
        return
    for role, backend in (("main", _main_backend), ("summary", _summary_backend)):
        if getattr(backend, "kind", None) != "bedrock":
            continue
        try:
            spent = _load(role)
            cap = _cap(getattr(backend, "_cfg", {}), role)
        except Exception:
            continue
        log.info(
            "bedrock.session_spend role=%s model=%s today_usd=%.4f cap_usd=%.2f",
            role,
            getattr(backend, "model", "?"),
            spent,
            cap,
        )
        # CICD 358 / issue #356 — per-session conversation count.
        # Logs the number of distinct server-side conversations this
        # session opened. With conversation reuse (this issue) we expect
        # count=1 for any run; pre-fix values were ~N (one per turn).
        count = getattr(backend, "_session_conv_count", None)
        if count is not None:
            log.info(
                "bedrock.session_conv_count role=%s model=%s count=%d",
                role,
                getattr(backend, "model", "?"),
                count,
            )


# One-shot flag set by --setup: forces the wizard on the next interactive
# start even when a config already exists (cleared after it runs).
_FORCE_SETUP_WIZARD = False


def _maybe_first_run_wizard(auto, initial_prompt, continue_mode, force=False):
    """First-run /setup (plan/setup-command.md Phase 2).

    Fires ONLY when: interactive (not -a, no initial prompt, not a resumed
    session), stdin is a real TTY, and this folder has neither .agent/ nor a
    legacy ./config.json. Non-interactive contexts must NEVER block on the
    wizard — they keep the defaults and get one log line. Runs pre-TUI, so
    plain input() is safe. Returns True iff the wizard ran and wrote config.
    """
    cwd = Path(os.getcwd())
    # The CONFIG FILE is the configured-signal, NOT the .agent/ directory:
    # the state/history bootstrap creates .agent/ at boot, before this guard
    # runs (caught live 2026-08-15 — the dir-based check made the wizard
    # unreachable in exactly the empty-folder case it exists for).
    # force=True (--setup) runs the wizard regardless of existing config.
    unconfigured = force or (not (cwd / ".agent" / "config.json").exists()
                             and not (cwd / "config.json").exists())
    interactive = (not auto and initial_prompt is None and not continue_mode
                   and sys.stdin.isatty())
    if not unconfigured:
        return False
    if not interactive:
        logging.getLogger("agent").info(
            "no local config — defaults in effect; run /setup to configure")
        return False
    try:
        import setup_wizard as _setup_wizard
        print("No .agent/ configuration found here — running first-time "
              "setup (ctrl-C to skip and use defaults).")
        updates = _setup_wizard.run_wizard(cwd / ".agent" / "config.json",
                                           _config)
        if updates:
            _apply_setup_updates(updates)
            return True
    except (KeyboardInterrupt, EOFError):
        print("\nsetup skipped — defaults in effect (`/setup` runs it later)")
    except Exception as e:  # never let the wizard kill a session start
        logging.getLogger("agent").warning("first-run wizard failed: %s", e)
    return False


_END = object()   # a session step's answer: the session is over


class _Session:
    """One interactive session: the boot, the backends, the resume, the front end, the
    first prompt, the input loop and the shutdown as steps run in order. The locals of
    the former single function that cross a step are attributes under their old names;
    the step-local ones stayed locals."""

    def __init__(self, initial_prompt, auto, continue_mode, cb, tui, verbose, result_file):
        self.initial_prompt = initial_prompt
        self.auto = auto
        self.continue_mode = continue_mode
        self.cb = cb
        self.tui = tui
        self.verbose = verbose
        self.result_file = result_file

    def run(self):
        """The session: each step in order, until one ends it with _END."""
        for step in self._STEPS:
            if step(self) is _END:
                return

    def _boot(self):
        """The first-run wizard, the config knobs, the logger, telemetry, the UI callback."""
        global _FORCE_SETUP_WIZARD, _cb, _cb_log
        # First-run wizard (plan/setup-command.md Phase 2) — BEFORE the ctx_size
        # read so a first-run calibration applies to THIS session, not the next.
        # --setup forces it even when a config exists (one-shot, then cleared).
        _maybe_first_run_wizard(self.auto, self.initial_prompt, self.continue_mode,
                                force=_FORCE_SETUP_WIZARD)
        _FORCE_SETUP_WIZARD = False

        self.ctx_size = _config["context"]["ctx_size"]
        self.max_tokens = _config["context"]["max_tokens"]
        self.gen = _config["generation"]

        self.log, self.log_path, self.error_log_path = _setup_logger()

        # OTLP telemetry: init once per session (no-op if disabled / SDK missing).
        self._telemetry_on = telemetry.init()
        self.t0 = time.time()

        # Install the UI callback handle for this session
        _cb = self.cb if self.cb is not None else TerminalCallbacks(verbose=self.verbose)
        _cb_log = self.log
        # Hand the boot-line count to the callback so on_session_start can
        # erase it (along with the on_boot_progress lines below) before the
        # banner. Setattr instead of constructor arg keeps TerminalCallbacks
        # / TuiCallbacks signatures stable.
        if _BOOT_LINES_PRINTED:
            try:
                _cb._boot_lines_printed += _BOOT_LINES_PRINTED
            except AttributeError:
                pass

        # Wire think tool's output through the callback system (D12 compliance).
        # _emit("on_stream_chunk", text) routes through safe_cb so a buggy UI hook
        # can never crash the think tool.  The default (_output = print) is kept
        # as a safe fallback for standalone/test use outside the agent loop.
        import tools.think as _think_mod
        _think_mod._output = lambda text: _emit("on_stream_chunk", text)

    def _backends(self):
        """The main backend's health and context size, the summary and advisor backends."""
        model_name = _main_backend.model or _config["llm"]["model"]
        _emit(
            "on_boot_progress",
            f"checking main backend — {_cfg.display_backend_kind(getattr(_main_backend, 'kind', ''), getattr(_main_backend, 'base_url', ''))} {model_name}",
        )
        self.ok, self.detail = _main_backend.health(timeout=_startup_health_timeout())

        # Auto-detect the model name from /v1/models so requests and the TUI carry the
        # server's real model. A single listed model always wins; with several, only
        # an unset model takes the first (a configured one is left alone).
        if self.ok and getattr(_main_backend, "kind", None) == "llamacpp":
            _detected_models = _main_backend.list_models(timeout=3)
            if len(_detected_models) == 1 or (_detected_models and not _main_backend.model):
                _main_backend.model = _detected_models[0]
                _config["llm"]["model"] = _detected_models[0]
                self.log.info("Auto-detected main model name from /v1/models: %s", _detected_models[0])

        # Backend banner (plan task 1.5) — one-line log noting which kinds are active.
        self.log.info(
            "backends: main=%s(%s@%s) summary=%s(%s@%s)",
            _main_backend.kind, _main_backend.model, _main_backend.base_url,
            _summary_backend.kind, _summary_backend.model, _summary_backend.base_url,
        )

        # Warn if the main llamacpp backend's chat template doesn't support tool calls.
        # Symptom: the model's native tool-call tokens appear as content text, get
        # stripped, and no tools ever fire. Fix: a template that supports tools.
        if getattr(_main_backend, 'kind', '') == 'llamacpp':
            caps = _main_backend.check_tool_caps()
            if caps and not caps.get('supports_tool_calls', True):
                self.log.warning(
                    "TOOL CALLS DISABLED: main backend chat template does not support tool calls "
                    "(chat_template_caps.supports_tool_calls=false). Tools will not execute. "
                    "Start llama-server with --jinja and a chat template that supports tool calls "
                    "(--chat-template-file <template>; docs/setup.md covers this)."
                )

        # Auto-detect context size from the main backend. Apply 85% buffer,
        # then hard-cap at 85K to avoid llama_decode crashes.
        # If the agent's config.json sets context.ctx_size to a value lower than
        # the default (114688), that value is used as an additional cap — letting
        # operators trade context depth for less fill pressure per turn.
        _CTX_HARD_CAP = 85_000
        _CTX_CONFIG_CAP = _config["context"]["ctx_size"]  # explicit agent cap (may be default 114688)
        detected = _main_backend.detect_ctx_size()
        if detected and getattr(_main_backend, "kind", None) == "aicommons":
            # detect_ctx_size() is already tokens, already capped by
            # aicommons.max_context_tokens — the 85% / 85K llama_decode guards don't
            # apply. Only an explicitly LOWERED context.ctx_size still caps it (M7).
            _explicit = _CTX_CONFIG_CAP if _CTX_CONFIG_CAP != _DEFAULT_CTX_SIZE else None
            self.ctx_size = min(int(detected), _explicit or int(detected))
            _config["context"]["ctx_size"] = self.ctx_size
            self.log.info("AI Commons context: model window capped to %d tokens, using ctx_size=%d",
                     detected, self.ctx_size)
        elif detected:
            self.ctx_size = min(int(detected * 0.85), _CTX_HARD_CAP, _CTX_CONFIG_CAP)
            _config["context"]["ctx_size"] = self.ctx_size
            self.log.info("Auto-detected main model n_ctx=%d, using ctx_size=%d (85%% / cap %dk / config cap %d)",
                     detected, self.ctx_size, _CTX_HARD_CAP // 1000, _CTX_CONFIG_CAP)

        # Probe summary health BEFORE on_session_start so the banner can
        # render main + summary indicators together in a single header.
        self.summary_cfg = _config["summary"]
        self.summary_ok = False
        self.summary_detail = "disabled"
        self.summary_url = getattr(_summary_backend, "base_url", "") if _summary_backend else ""
        if self.summary_cfg["enabled"]:
            _emit(
                "on_boot_progress",
                f"checking summary backend — {_cfg.display_backend_kind(getattr(_summary_backend, 'kind', ''), self.summary_url)} {getattr(_summary_backend, 'model', '?')}",
            )
            try:
                self.summary_ok, self.summary_detail = _summary_backend.health(timeout=_startup_health_timeout())
            except (requests.ConnectionError, requests.Timeout):
                self.summary_ok, self.summary_detail = False, "unreachable"
            if self.summary_ok and getattr(_summary_backend, "kind", None) == "llamacpp":
                _det = _summary_backend.list_models(timeout=3)
                if len(_det) == 1 or (_det and not _summary_backend.model):
                    _summary_backend.model = _det[0]
                    self.log.info("Auto-detected summary model name from /v1/models: %s", _det[0])

        # Probe the advisor (GLM) endpoint. If it is unreachable, remove the tool
        # from the registry so the model never sees it in the schema — avoids
        # wasted escalation attempts against a server that isn't running.
        advisor_cfg = _config.get("advisor", {}) or {}
        # Default-ON with main fallback: advisor_active/_read_role own the rule
        # (explicit enabled wins; else the advisor self-consults main's endpoint).
        try:
            from tools.consult_advisor import (advisor_active as _advisor_active,
                                               _read_role as _advisor_read_role)
            self.advisor_enabled = _advisor_active(_config)
            self._adv_eff = _advisor_read_role("advisor")
        except Exception:
            self.advisor_enabled = advisor_cfg.get("enabled",
                                              bool(advisor_cfg.get("base_url")))
            self._adv_eff = advisor_cfg
        self.advisor_ok = False
        self.advisor_detail = "disabled"
        if self.advisor_enabled and getattr(_main_backend, "kind", "") == "fake":
            # F8b: the fake backend promises "never opens a socket" — the advisor probe is a
            # socket. Under the fake, the advisor is unloaded without probing (review finding 2).
            self.advisor_enabled = False
            self.advisor_detail = "disabled (fake backend active — no probe)"
            try:
                from tools import remove_tool as _remove_tool_fake
                _remove_tool_fake("consult_advisor")
            except Exception:  # noqa: BLE001
                pass
            self.log.info("Advisor probe skipped: fake backend active")
        if self.advisor_enabled:
            _emit("on_boot_progress",
                  f"checking advisor backend — "
                  f"{self._adv_eff.get('base_url') or _cfg.display_backend_kind(self._adv_eff.get('kind') or '?', '')} "
                  f"{self._adv_eff.get('model') or '(served model)'}")
            try:
                from tools.consult_advisor import startup_check as _advisor_check
                self.advisor_ok, self.advisor_detail = _advisor_check(
                    timeout_s=_startup_health_timeout())
            except Exception as _e:
                self.advisor_ok, self.advisor_detail = False, f"{type(_e).__name__}: {_e}"
            if not self.advisor_ok:
                from tools import remove_tool as _remove_tool
                _remove_tool("consult_advisor")
                self.log.warning(
                    "Advisor endpoint unavailable (%s) — consult_advisor tool unloaded",
                    self.advisor_detail,
                )
            else:
                self.log.info("Advisor endpoint healthy — consult_advisor tool active")

        # Footer ↔ banner parity: the banner uses _main_backend.model, the TUI
        # footer reads _config["llm"]["model"]. Sync them before the banner renders
        # and before the TuiSession is built so both show the active model name.
        _sync_config_model(_main_backend, _config)

    def _announce(self):
        """The session-start event and log lines."""
        # Live-input mode reprints what the session showed when the terminal is
        # resized (TuiSession); recording starts here so the banner is part of it.
        self._recorder = None
        if self.tui and not self.auto:
            _ivr = _config.get("interactive") if isinstance(_config, dict) else None
            if not isinstance(_ivr, dict) or _ivr.get("live_input", True):
                import transcript as _transcript
                self._recorder = _transcript.install()

        _adv = self._adv_eff if isinstance(getattr(self, "_adv_eff", None), dict) else {}
        _emit("on_session_start", {
            "version": __version__,
            "sha": _git_short_sha(),
            "api_ok": self.ok,
            "api_detail": self.detail,
            "base_url": getattr(_main_backend, "base_url", None) or BASE_URL,
            "model": _main_backend.model or _config["llm"]["model"],
            "main_kind": _cfg.display_backend_kind(getattr(_main_backend, "kind", ""), getattr(_main_backend, "base_url", "")),
            "summary_enabled": self.summary_cfg["enabled"],
            "summary_ok": self.summary_ok,
            "summary_detail": self.summary_detail,
            "summary_base_url": self.summary_url,
            "summary_model": getattr(_summary_backend, "model", "") if _summary_backend else "",
            "summary_kind": _cfg.display_backend_kind(getattr(_summary_backend, "kind", ""), self.summary_url) if _summary_backend else "",
            "advisor_enabled": self.advisor_enabled,
            "advisor_ok": self.advisor_ok,
            "advisor_detail": self.advisor_detail,
            "advisor_kind": _cfg.display_backend_kind(_adv.get("kind") or "", _adv.get("base_url") or ""),
            "advisor_model": _adv.get("model") or "",
            "ctx_size": self.ctx_size,
            "max_turns": _MAX_TURNS,
            "log_path": self.log_path,
            "error_log_path": self.error_log_path,
        })

        self.log.info("Session started | ctx_size=%d max_turns=%d temperature=%.1f max_tokens=%d",
                 self.ctx_size, _MAX_TURNS, self.gen["temperature"], self.max_tokens)
        if _CWD_CANONICALIZED:
            # F5: say what was rewritten at import (logging was not configured yet then).
            self.log.info("Working directory canonicalized: %s -> %s (pass --no-realpath-cwd to keep "
                     "the symlinked view)", _CWD_CANONICALIZED[0], _CWD_CANONICALIZED[1])
        if self.gen.get("seed") is not None:
            self.log.info("Generation seed=%s (F8a)", self.gen["seed"])
        self.log.info("Tools registered: %s", [t["function"]["name"] for t in tools])

    def _summarizer(self):
        """The background summarizer, when the summary backend answers."""
        # Instantiate the async summarizer from the already-probed state above.
        self._async_summarizer = None
        if self.summary_cfg["enabled"]:
            if self.summary_ok:
                # Auto-detect summary model context size
                summary_ctx = _summary_backend.detect_ctx_size()
                if summary_ctx:
                    _config["summary"]["ctx_size"] = int(summary_ctx * 0.85)
                    self.log.info("Auto-detected summary model n_ctx=%d, using %d (85%%)",
                             summary_ctx, _config["summary"]["ctx_size"])
                self._async_summarizer = _summ.AsyncSummarizer(_config, self.log)
                self.log.debug("Async summarizer enabled → %s", self.summary_url)
                _emit("on_summarizer_status", "online", self.summary_url)
            elif self.summary_detail in ("unreachable", "timeout"):
                # Connection-level failure → "offline" (matches pre-refactor).
                self.log.warning("Summary endpoint unreachable at %s, using main model for summaries",
                            self.summary_url)
                _emit("on_summarizer_status", "offline", self.summary_url)
            else:
                self.log.warning("Summary endpoint unhealthy (%s), using main model for summaries",
                            self.summary_detail)
                _emit("on_summarizer_status", "unhealthy", self.summary_detail)

    def _resume(self):
        """Continue mode: the checkpoint, the lead files, a held summary; then the task seed.
        Ends the session after the resumed run in auto mode."""
        # ── Continue mode: resume from checkpoint ──
        self.start_turn = 0
        if self.continue_mode:
            cp = _load_checkpoint()
            if cp:
                self.conversation_history, self.summary_state, self.start_turn, self.initial_files, _clean_exit = cp
                _CKPT_TURN["turn"] = self.start_turn
                self.log.info("CONTINUE: resuming from checkpoint (turn %d, %d messages, clean_exit=%s)",
                         self.start_turn, len(self.conversation_history), _clean_exit)
                if _RESTART["resumed"]:
                    self.log.info("CONTINUE: after a restart by %s (%s)", _RESTART["resumed"].get("by"),
                             _RESTART["resumed"].get("reason") or "no reason given")
                _cp_lead = self.initial_files
                self.initial_files = _lead_reload(self.initial_files, self.log)
                # The disk copy takes its place in the history too: an untrimmed window
                # sends the history, not the lead slot, so without this an edit made
                # between sessions reached the model only once the window trimmed.
                _LEAD.pop("replaced", None)
                if _lead_place(self.conversation_history, self.initial_files, self.log, old=_cp_lead) \
                        and _LEAD.get("replaced"):
                    self.log.info("lead files placed in the history on resume (changed since the checkpoint)")
                # Cap summary from old checkpoints that may have bloated summaries
                if self.summary_state.get("text"):
                    self.summary_state["text"] = _summ.condense_summary(self.summary_state["text"], self.log)
                _emit("on_continue_resumed", self.start_turn, len(self.conversation_history))
                if self.auto:
                    _seed_phase_tasks(_config, self.log)
                    self.result = run_agent_single(self.conversation_history, self.summary_state, self.initial_files, self.log,
                                              self.gen["temperature"], self.gen["top_p"], self.gen["top_k"],
                                              self.gen["presence_penalty"], self.max_tokens, self.ctx_size,
                                              start_turn=self.start_turn,
                                              async_summarizer=self._async_summarizer)
                    cleanup_temp_sessions()
                    self.log.info("Session ended (continue mode) | %d messages", len(self.conversation_history))
                    if self._telemetry_on:
                        telemetry.record_cycle(status="continue_completed", duration_s=time.time() - self.t0)
                        telemetry.shutdown()
                    _log_bedrock_session_spend(self.log)
                    return _END
                # Non-auto: restore state and fall through to interactive loop
            else:
                _emit("on_continue_none")
                self.log.debug("CONTINUE: no checkpoint found, starting fresh")

        if not self.continue_mode:
            # Check if the current cycle was already committed — bump if so
            _auto_increment_cycle(self.log)

        self.conversation_history = self.conversation_history if self.continue_mode and self.start_turn > 0 else []
        self.summary_state = self.summary_state if self.continue_mode and self.start_turn > 0 else {"text": "", "up_to": 0}
        self.initial_files = self.initial_files if self.continue_mode and self.start_turn > 0 else None
        _held = self.summary_state.pop("_pending", None)
        if (self._async_summarizer is not None and isinstance(_held, dict) and _held.get("text")
                and isinstance(_held.get("up_to"), int)):
            # A summary made ahead of an eviction that had not happened when the checkpoint
            # was written: held again here, applied when the head moves past it.
            self._async_summarizer.seed(_held["text"], _held["up_to"], _held.get("kind") or "resumed")
            self.log.info("summary.resume: a finished summary up to message %d rides along from the checkpoint",
                     _held["up_to"])

        # AC3/AC4 of #1028 — close ephemeral tasks left open from a prior session
        # and build a [Session resume] preamble for the first user message.
        # Skipped on continue-mode resumes (start_turn > 0) since the whole
        # point of continue is to pick up the prior session intact. Runs
        # BEFORE _seed_phase_tasks so idempotent seeding sees the post-close
        # state, not stale ephemeral tasks from the previous abandoned cycle.
        self._session_resume_summary = (
            _auto_close_ephemeral_tasks(self.log)
            if not (self.continue_mode and self.start_turn > 0)
            else None
        )

        _seed_phase_tasks(_config, self.log)

    _STATUS_ASK_TAIL = 24          # messages the F3 question shows the summary model
    _STATUS_ASK_LOCK = threading.Lock()

    def _ask_status(self):
        """F3: the summary model's short read of what the agent is doing, from the
        transcript tail and the loop's own record. Printed, logged, never model input."""
        tail = _summ.format_for_summary(list(self.conversation_history)[-self._STATUS_ASK_TAIL:])
        prompt = (
            "You are watching an AI agent work; a person at the keyboard wants to know what "
            "it is doing. From the loop's own record and the transcript tail, answer in at "
            "most five plain lines: what it is doing now, what it is waiting on, what it "
            "will likely do next, and whether anything looks stuck or repeated. No preamble.\n\n"
            "LOOP RECORD:\n" + _act.describe() + "\n\nTRANSCRIPT TAIL:\n" + tail + "\n\nANSWER:"
        )
        answer = (_gate_judge(prompt, timeout=90, max_tokens=350, lock=self._STATUS_ASK_LOCK) or "").strip()
        self.log.info("status ask (F3): %s", answer)
        return answer or "(the summary model said nothing)"

    def _front_end(self):
        """The prompt_toolkit session and its callbacks, persistent monitors and schedules,
        the resume prompt after a restart."""
        global _cb
        # ── TUI front-end (default in interactive mode) ──
        # Now that history / summary / initial_files have stable identities,
        # instantiate the prompt_toolkit session and swap the UI callback.
        # If prompt_toolkit isn't installed we fall back silently to plain
        # input() with a one-line notice so the default TUI path doesn't
        # break environments that haven't installed the optional dependency.
        self.tui_session = None
        # Concurrent always-live input is the DEFAULT in interactive (TUI) mode:
        # the input line stays usable while the agent works; typed lines and monitor
        # pings queue and are delivered at the next natural boundary. Opt OUT with
        # config.interactive.live_input=false (falls back to the blocking prompt
        # loop). No effect under -a, or under --no-tui / no prompt_toolkit (plain
        # input(), which cannot host a concurrent line).
        _iv = _config.get("interactive") if isinstance(_config, dict) else None
        self._live_input = bool(_iv.get("live_input", True)) if isinstance(_iv, dict) else True
        if self.tui and not self.auto:
            import tui as _tuimod
            if _tuimod._AVAILABLE and self._live_input:
                # Disable CPR (cursor-position requests) BEFORE the session is
                # built: prompt_toolkit's Renderer caches CPR support at
                # construction (renderer.py CPR_Support), so this must precede
                # TuiSession(). Why: on CPR-capable terminals every streamed
                # line's run_in_terminal cycle awaits outstanding CPR responses
                # with the input detached — a response landing in that window is
                # lost, after which every redraw stalls in
                # wait_for_cpr_responses: the prompt/spinner freezes and
                # streamed text lands BELOW the stale paint (field report
                # 2026-08-13). The CPR-less relative-positioning path renders
                # the same stream correctly (verified via pyte replay both
                # ways). Cost: the bottom toolbar needs height-known and stops
                # rendering — the working indicator lives on the prompt line,
                # which every terminal renders. Blocking / --no-tui modes never
                # set this.
                os.environ["PROMPT_TOOLKIT_NO_CPR"] = "1"
            if _tuimod._AVAILABLE:
                self.tui_session = _tuimod.TuiSession(
                    history=self.conversation_history,
                    summary_state=self.summary_state,
                    config=_config,
                    ctx_size=self.ctx_size,
                    cb=_cb,
                    estimate_tokens=_cw.estimate_tokens,
                    enable_cancel_key=self._live_input,
                    status_text=_act.describe,
                    ask_status=self._ask_status,
                )
                _cb = _tuimod.TuiCallbacks(self.tui_session, verbose=getattr(_cb, "verbose", False))
                # Idle-wake (app.exit from a monitor thread) is only for the default
                # blocking loop. Concurrent mode consumes monitor pings from the
                # queue directly (queue.get wakes on any enqueue), so registering
                # the wake notifier there would fire app.exit on the always-running
                # input prompt — do NOT register it in live-input mode.
                if not self._live_input:
                    monitor_bus.set_notifier(self.tui_session.wake)
            else:
                _emit("on_notice", "warn",
                      "prompt_toolkit not installed — using plain prompt. "
                      "`pip install prompt_toolkit` (or pass --no-tui to silence).")

        try:
            _restored = monitor_bus.restore_persisted()
            if _restored:
                self.log.info("re-armed persistent monitors: %s", ", ".join(_restored))
            _sched = schedule_bus.restore()
            if _sched:
                self.log.info("schedules active: %s", ", ".join(_sched))
        except Exception:
            self.log.debug("restore_persisted failed", exc_info=True)

        # A restart the model asked for comes back to work, not to the prompt: the configured
        # text (preferences.restart_resume_prompt) goes in as the user's message and one turn
        # runs before the input loop, as an initial prompt would. A keyboard /restart returns
        # to the prompt. Auto mode resumes its run anyway.
        _resume_text = _restart_resume_prompt() if (self.continue_mode and self.start_turn > 0 and not self.auto) else ""
        if _resume_text:
            _emit("on_user_message", _resume_text)
            self.conversation_history.append({"role": "user", "content": _resume_text})
            self.log.info("RESTART: resuming with the configured prompt (%d chars)", len(_resume_text))
            run_agent_single(self.conversation_history, self.summary_state, self.initial_files, self.log,
                             self.gen["temperature"], self.gen["top_p"], self.gen["top_k"],
                             self.gen["presence_penalty"], self.max_tokens, self.ctx_size,
                             async_summarizer=self._async_summarizer)

    def _first_prompt(self):
        """The initial prompt's turn; in auto mode the whole run, the result file and the exit."""
        global _pinned_instructions
        if self.initial_prompt and not (self.continue_mode and self.start_turn > 0):
            _emit("on_user_message", self.initial_prompt)
            try:
                self.expanded, self.files, self.err = _expand_file_refs(self.initial_prompt)
                if self.err:
                    _emit("on_error", self.err)
                    return _END
            except Exception as e:
                _emit("on_error", f"Unexpected error expanding initial prompt: {e}")
                return _END
            if self.files:
                self.initial_files, self.expanded = _lead_absorb(self.conversation_history, self.initial_files,
                                                      self.expanded, self.files, self.log)
            # Extract <pinned>...</pinned> blocks — these survive summarization
            self.expanded, pinned = _extract_pinned(self.expanded)
            if pinned:
                _pinned_instructions = pinned
                self.log.info("Pinned instructions extracted (%d chars)", len(pinned))

            # Bootstrap-template check (T3.9): create empty placeholders for files the
            # agent's AGENT.md/CLAUDE.md references but which don't exist on disk, when
            # they are declared under `bootstrap` in config. Eliminates a repeated
            # "missing file" error loop at startup. Fresh starts only (not continue_mode);
            # a no-op unless declared.
            _bootstrap_actions = _bootstrap_template_check(self.log)
            if _bootstrap_actions:
                self.log.info(
                    "Bootstrap-template: %d placeholder(s) auto-created — %s",
                    len(_bootstrap_actions),
                    ", ".join(f"{p} ({a})" for p, a in _bootstrap_actions),
                )
                for _ in _bootstrap_actions:
                    telemetry.record_patch_event("bootstrap_create", kind="created")

            # PERCEIVE preamble bundle (.agent/preamble.json): auto-load and inject
            # before the user's initial prompt. Saves the 6-8 boilerplate tool calls
            # both audits flagged as the #1 friction source. Only on fresh starts;
            # continue_mode picks up where it left off and shouldn't re-inject.
            _preamble = _load_preamble_bundle(self.log)
            if _preamble:
                self.conversation_history.append({"role": "system", "content": _preamble})

            # F2: the contract instruction exists only when armed — creative/interactive sessions
            # never see it. Injected as a system message on fresh starts (continue_mode resumes a
            # history that already carries it).
            if _result_contract_armed():
                self.conversation_history.append({"role": "system", "content": _result_contract_instruction(
                    _RESULT_CONTRACT_SCHEMA or RESULT_CONTRACT_DEFAULT_SCHEMA)})
                self.log.info("Result contract armed (%s)", "built-in schema" if _RESULT_CONTRACT_SCHEMA in (None, RESULT_CONTRACT_DEFAULT_SCHEMA) else "custom schema")
                # F4 auto-arm: with the contract armed, a recognizable GOAL: / DELIVERABLE: stanza
                # in the initial prompt fills cycle.goal / cycle.deliverables when they are unset.
                # Only the guard's CORRECTION is ever armed this way, never a hard block.
                _cyc = _config.setdefault("cycle", {})
                if not _cyc.get("goal") or not _cyc.get("deliverables"):
                    _dg, _dd = _derive_goal_and_deliverables(self.expanded if isinstance(self.expanded, str) else "")
                    if _dg and not _cyc.get("goal"):
                        _cyc["goal"] = _dg
                    if _dd and not _cyc.get("deliverables"):
                        _cyc["deliverables"] = _dd
                    if _dg or _dd:
                        self.log.info("F4 derived from the prompt: goal=%r deliverables=%s", _dg[:80], _dd)
            # F4 launch check: a deliverable glob that already matches nothing is fine (it is to be
            # produced) — but a pattern with a typo is caught by warning ONCE what is absent now,
            # so a 45-minute run does not end on a misspelled path.
            _dels_now = (_config.get("cycle") or {}).get("deliverables") or []
            if isinstance(_dels_now, list) and _dels_now:
                _absent = _missing_deliverables([d for d in _dels_now if isinstance(d, str)])
                self.log.info("F4 deliverables at launch: %d named, %d not yet present: %s",
                         len(_dels_now), len(_absent), _absent[:8])

            # WS2: goal-stack hydration — independent of preamble.json. If
            # state/goals.json has active goals, inject "goal → next actionable
            # step" so multi-cycle work resumes without re-derivation from git
            # log. Token-capped inside preamble_summary (~600 tokens).
            try:
                from tools.goal import preamble_summary as _goal_summary
                _goals_txt = _goal_summary()
                if _goals_txt:
                    self.conversation_history.append(
                        {"role": "system", "content": _goals_txt})
                    self.log.info("Goal stack hydrated into preamble (%d chars)",
                             len(_goals_txt))
            except Exception as _ge:
                self.log.debug("goal-stack hydration skipped: %s", _ge)

            # AC4 of #1028 — prepend [Session resume] block to the first user
            # message so the agent immediately sees what was auto-closed and
            # what persistent work remains, without an extra task_tracker call.
            if self._session_resume_summary:
                self.expanded = f"{self._session_resume_summary}\n\n---\n\n{self.expanded}"

            # F3a: inbound bulk spills too. A spec with a data blob pasted in, or a log dumped
            # into the initial prompt, goes to a file with a reference — bulk belongs in files,
            # references in context, in both directions. Same cap as tool results.
            self.expanded = _spill_initial_prompt(self.expanded, self.files, self.log)
            self.conversation_history.append({"role": "user", "content": self.expanded})
            self.log.debug("USER: %s", self.expanded)
            self.result = run_agent_single(self.conversation_history, self.summary_state, self.initial_files, self.log,
                                      self.gen["temperature"], self.gen["top_p"], self.gen["top_k"],
                                      self.gen["presence_penalty"], self.max_tokens, self.ctx_size,
                                      async_summarizer=self._async_summarizer)

            if self.auto:
                if self.result == "cancelled":
                    self.result = _handle_auto_guidance(self.conversation_history, self.summary_state, self.initial_files, self.log,
                                                   self.gen, self.max_tokens, self.ctx_size, self._async_summarizer,
                                                   self._telemetry_on, self.t0)
                monitor_bus.stop_all()
                cleanup_temp_sessions()
                self.log.info("Session ended (auto mode) | %d messages in history", len(self.conversation_history))
                if self._telemetry_on:
                    telemetry.record_cycle(status="auto_completed", duration_s=time.time() - self.t0)
                    telemetry.shutdown()
                _log_bedrock_session_spend(self.log)
                # F6: classify the terminal value for the supervisor; main() emits and exits with it.
                # Classified BEFORE the result file is written so the exit rides inside the JSON (F2).
                _code, _detail = _classify_auto_result(self.result, _LAST_ERROR_KIND, _LAST_EXIT)
                if _code == EXIT_OK and not _last_assistant_text(self.conversation_history):
                    # review finding 3: a run that produced NO assistant output is not 'completed'
                    _code, _detail = EXIT_ERROR, "run produced no assistant output"
                _info = _set_exit(_code, _detail)
                if self.result_file:
                    _obj, _synth = _write_result_file(self.result_file, self.conversation_history, exit_info=_info)
                    if _synth:
                        # F2: no valid result block — the caller still gets valid JSON; the exit code
                        # says the contract was not met unless a harder stop already owns the code.
                        self.log.warning("result contract: synthesized failed record written to %s", self.result_file)
                        if _info["code"] == EXIT_OK:
                            _info = _set_exit(EXIT_CONTRACT, "no valid result block emitted; failed record synthesized")
                            _obj["exit"] = dict(_info)
                            with open(self.result_file, "w", encoding="utf-8") as f:
                                json.dump(_obj, f, indent=1, ensure_ascii=False)
                                f.write("\n")
                return _END

    def _live_loop(self):
        """The always-live input: the prompt thread, this consumer loop, slash commands."""
        # ── Concurrent always-live input (opt-in: config.interactive.live_input) ──
        # A background thread runs the prompt continuously so the operator can type
        # while the agent works; each entered line is queued (put_user) and
        # delivered at the next natural boundary by the same mid-burst drain a
        # monitor ping uses. The main thread here is the SINGLE consumer: it blocks
        # on the shared queue, so a typed line OR a monitor ping wakes it — this
        # supersedes the app.exit idle-wake (not registered in this mode). A
        # process-wide patch_stdout held in the input thread routes all streaming /
        # tool output above the live input line (probe-verified: a bg-thread app is
        # visible to the main thread via the shared default AppSession). esc-esc
        # requests cancellation of a running turn.
        self._skip_blocking = self.tui_session is not None and self._live_input
        if self._skip_blocking:
            _EXIT_MARK = _live.EXIT_MARK
            # The prompt thread, the exit/borrow marks and the terminal borrow live in
            # live_input.py; this loop is the bus's single consumer.
            self._live_in = _live.LiveInput(self.tui_session, monitor_bus, self.log, recorder=self._recorder,
                                       cancel=cancel_request)

            def _dispatch_slash(_ui):
                def _refresh_cb_log(new_log):
                    globals()["_cb_log"] = new_log
                ctx = SimpleNamespace(
                    conversation_history=self.conversation_history, summary_state=self.summary_state,
                    initial_files=self.initial_files, async_summarizer=self._async_summarizer, cb=_cb,
                    log=self.log, log_path=self.log_path, ctx_size=self.ctx_size, config=_config,
                    base_url=getattr(_main_backend, "base_url", None) or BASE_URL,
                    summary_base_url=getattr(_summary_backend, "base_url", None),
                    setup_logger=_setup_logger, pick_model=_pick_model_interactive,
                    set_model=_cfg.set_model_for_role, set_system_prompt=_cfg.set_system_prompt,
                    get_effort=_cfg.get_effort_for_role, set_effort=_cfg.set_effort_for_role,
                    render_context_bar=_render_context_bar,
                    refresh_cb_log=_refresh_cb_log, apply_setup=_apply_setup_updates, request_restart=_request_restart)
                with self._live_in.borrowed_terminal():
                    handled = _commands.handle_command(_ui, ctx)
                if handled:
                    self.initial_files = ctx.initial_files
                    self.log = ctx.log
                    self._live_in.log = self.log
                    self.log_path = ctx.log_path
                    return True
                return False

            set_tui_mode(True)   # input app owns the keyboard continuously
            _SLASH_HOLD["active"] = True   # the consumer below dispatches slash lines
            self._live_in.start()
            try:
                # Items on this bus are (kind, str). Anything else is a defect in whatever
                # produced it, and the consumer below cannot safely act on one: the exit-marker
                # comparison silently returns False for a non-str while .startswith() may return
                # a truthy object, so a malformed item falls through into the dispatcher and the
                # loop spins on it forever. Validate here rather than trust the producer -- an
                # unbounded spin is a far worse failure than a dropped item, and it is the shape
                # that takes the whole machine down rather than just this loop.
                _bad_items = 0
                _MAX_BAD_ITEMS = 10
                while not _RESTART["requested"]:
                    try:
                        self._item = monitor_bus.get_blocking()
                    except KeyboardInterrupt:
                        break
                    if self._item is None:
                        continue
                    if (not isinstance(self._item, tuple) or len(self._item) != 2
                            or not isinstance(self._item[1], str)):
                        _bad_items += 1
                        self.log.error("live-input: dropping malformed bus item (%r); "
                                  "expected a (kind, str) pair", type(self._item).__name__)
                        if _bad_items >= _MAX_BAD_ITEMS:
                            # Bounded rather than infinite: a producer emitting nothing but
                            # malformed items is broken, and continuing to drain it accomplishes
                            # nothing while costing memory on every pass.
                            self.log.error("live-input: %d consecutive malformed items — ending live "
                                      "input instead of spinning", _bad_items)
                            break
                        continue
                    _bad_items = 0
                    _kind, _text = self._item
                    if _kind == "user" and _text == _EXIT_MARK:
                        break
                    if _kind == "user":
                        if _text.lower() in ("exit", "quit"):
                            break
                        if _text.startswith("/") and _dispatch_slash(_text):
                            continue
                        _expanded, _files, _err = _expand_file_refs(_text)
                        if _err:
                            _emit("on_error", _err)
                            continue
                        if _files:
                            self.initial_files, _expanded = _lead_absorb(
                                self.conversation_history, self.initial_files, _expanded, _files, self.log)
                        self._body = _expanded
                        self.log.debug("USER (live): %s", _expanded[:200])
                    else:
                        # A monitor ping consumed while idle IS the task (raw text).
                        self._body = _text
                    # Either way the agent is waking: the gate's held digests ride along
                    # instead of earning a wake of their own later.
                    _digests = monitor_bus.take_held_digests()
                    if _digests:
                        self._body = self._body + "\n\n" + "\n\n".join(_digests)
                    self.conversation_history.append({"role": "user", "content": self._body})
                    # Cancel-flag reset happens at run_agent_single entry (the
                    # turn boundary — single authority for tui_mode flag
                    # lifetime); here we only drop the toolbar's cancelling
                    # feedback segment before the new turn starts.
                    self.tui_session.clear_cancelling()
                    run_agent_single(self.conversation_history, self.summary_state, self.initial_files, self.log,
                                     self.gen["temperature"], self.gen["top_p"], self.gen["top_k"],
                                     self.gen["presence_penalty"], self.max_tokens, self.ctx_size,
                                     async_summarizer=self._async_summarizer)
            finally:
                self._live_in.stop()
                # Stop the input app cleanly so prompt_toolkit erases its render
                # and the terminal is left on a fresh line, not mid-paint. Two
                # attempts cover the narrow window where the thread was between
                # prompts (exit_app no-ops) when the first shot fired.
                for _ in range(2):
                    self.tui_session.exit_app()
                    self._live_in.join(timeout=1.0)
                    if not self._live_in.alive:
                        break
                _SLASH_HOLD["active"] = False
                set_tui_mode(False)
                if self._live_in.alive:
                    # Could not stop the app — best-effort scrub of the painted
                    # prompt/toolbar rows so the shell doesn't land mid-screen.
                    sys.stdout.write("\033[0J")
                sys.stdout.write("\n")
                sys.stdout.flush()

    def _blocking_loop(self):
        """The blocking prompt loop (no TUI, or live input off)."""
        while not self._skip_blocking and not _RESTART["requested"]:
            # Monitor idle-wake pre-check (TUI only): if a monitor queued output
            # while we were between prompts, inject and run it before blocking on
            # input — closes the race where output lands just before prompt() starts.
            if self.tui_session is not None and monitor_bus.has_pending():
                if _drain_monitor_injections(self.conversation_history):
                    run_agent_single(self.conversation_history, self.summary_state, self.initial_files, self.log,
                                     self.gen["temperature"], self.gen["top_p"], self.gen["top_k"],
                                     self.gen["presence_penalty"], self.max_tokens, self.ctx_size,
                                     async_summarizer=self._async_summarizer)
                continue
            try:
                if self.tui_session is not None:
                    self.user_input = self.tui_session.prompt()
                else:
                    self.user_input = input("\nYou: ").strip()
            except EOFError:
                break
            except KeyboardInterrupt:
                _emit("on_notice", "info", "\n\nGoodbye!\n")
                break

            # An idle prompt woken by a monitor returns the WAKE sentinel: drain
            # the bus and run, rather than treating it as typed text (so monitor
            # output never hits the slash-command / file-ref paths).
            if self.tui_session is not None and self.user_input is self.tui_session.WAKE:
                if _drain_monitor_injections(self.conversation_history):
                    run_agent_single(self.conversation_history, self.summary_state, self.initial_files, self.log,
                                     self.gen["temperature"], self.gen["top_p"], self.gen["top_k"],
                                     self.gen["presence_penalty"], self.max_tokens, self.ctx_size,
                                     async_summarizer=self._async_summarizer)
                continue

            if not self.user_input:
                continue
            if self.user_input.lower() in ["exit", "quit"]:
                _emit("on_notice", "info", "Goodbye!\n")
                break
            if self.user_input.startswith("/"):
                def _refresh_cb_log(new_log):
                    globals()["_cb_log"] = new_log
                ctx = SimpleNamespace(
                    conversation_history=self.conversation_history,
                    summary_state=self.summary_state,
                    initial_files=self.initial_files,
                    async_summarizer=self._async_summarizer,
                    cb=_cb,
                    log=self.log,
                    log_path=self.log_path,
                    ctx_size=self.ctx_size,
                    config=_config,
                    # Point /model at the active main backend (e.g. the configured
                    # gateway), not the hardcoded llama-server default — otherwise
                    # the picker probes 127.0.0.1:8080 and lists nothing.
                    base_url=getattr(_main_backend, "base_url", None) or BASE_URL,
                    # Summary backend's URL so /model summary lists from (and uses
                    # the auth of) the summary backend, mirroring main listing.
                    summary_base_url=getattr(_summary_backend, "base_url", None),
                    setup_logger=_setup_logger,
                    pick_model=_pick_model_interactive,
                    # Applies a chosen model to a role (main/summary): updates the
                    # in-memory config + live backend object, then persists to
                    # .agent/config.json. Keeps commands.py free of business logic.
                    set_model=_cfg.set_model_for_role,
                    set_system_prompt=_cfg.set_system_prompt,
                    get_effort=_cfg.get_effort_for_role,
                    set_effort=_cfg.set_effort_for_role,
                    render_context_bar=_render_context_bar,
                    refresh_cb_log=_refresh_cb_log,
                    apply_setup=_apply_setup_updates, request_restart=_request_restart,
                )
                if _commands.handle_command(self.user_input, ctx):
                    # /clear may have rotated log and initial_files — pull them back
                    self.initial_files = ctx.initial_files
                    self.log = ctx.log
                    self.log_path = ctx.log_path
                    continue

            self.expanded, self.files, self.err = _expand_file_refs(self.user_input)
            if self.err:
                _emit("on_error", self.err)
                continue
            if self.files:
                self.initial_files, self.expanded = _lead_absorb(self.conversation_history, self.initial_files,
                                                      self.expanded, self.files, self.log)

            self.conversation_history.append({"role": "user", "content": self.expanded})
            self.log.debug("USER: %s", self.expanded)

            run_agent_single(self.conversation_history, self.summary_state, self.initial_files, self.log,
                             self.gen["temperature"], self.gen["top_p"], self.gen["top_k"],
                             self.gen["presence_penalty"], self.max_tokens, self.ctx_size,
                             async_summarizer=self._async_summarizer)

    def _shutdown(self):
        """The restart checkpoint, the front end, the monitors, the logs, the result file."""
        if _RESTART["requested"]:
            # The checkpoint the re-exec resumes from: the history as it stands at this
            # boundary (a /restart typed before any turn overwrites a stale file with an
            # empty one, so -c starts fresh rather than resuming an older session).
            if self._async_summarizer:
                self._async_summarizer.harvest(self.summary_state)
            _save_checkpoint(self.conversation_history, self.summary_state, _CKPT_TURN["turn"], self.initial_files)
            _emit("on_notice", "info",
                  f"Restarting with -c ({_RESTART['by']}{': ' + _RESTART['reason'] if _RESTART['reason'] else ''})")
            self.log.info("RESTART: by %s (%s); checkpoint at turn %d, %d messages",
                     _RESTART["by"], _RESTART["reason"] or "no reason", _CKPT_TURN["turn"],
                     len(self.conversation_history))
        if self.tui_session is not None:
            self.tui_session.close()
        monitor_bus.clear_notifier()
        monitor_bus.stop_all()
        cleanup_temp_sessions()
        self.log.info("Session ended | %d messages in history", len(self.conversation_history))
        if self._telemetry_on:
            telemetry.record_cycle(status="completed", duration_s=time.time() - self.t0)
        _log_bedrock_session_spend(self.log)

        if self.result_file:
            # raw text unless the contract is armed (F2) — interactive sessions leave it off
            _write_result_file(self.result_file, self.conversation_history)

        if self._telemetry_on:
            telemetry.shutdown()

    _STEPS = (_boot, _backends, _announce, _summarizer, _resume, _front_end, _first_prompt, _live_loop, _blocking_loop, _shutdown)


def run_agent_interactive(initial_prompt=None, auto=False, continue_mode=False, *, cb=None, tui=False, verbose=False, result_file=None):
    """Interactive agent that maintains conversation history.

    When `tui=True`, a prompt_toolkit front-end (tui.TuiSession) owns the
    input prompt and swaps the default TerminalCallbacks for TuiCallbacks
    so the bottom toolbar reflects live model/message/ctx state. The TUI
    is optional — if prompt_toolkit isn't installed, a clean ImportError
    is raised at TuiSession construction time.
    """
    _Session(initial_prompt, auto, continue_mode, cb, tui, verbose, result_file).run()


def _handle_auto_guidance(conversation_history, summary_state, initial_files, log, 
                               gen, max_tokens, ctx_size, async_summarizer, 
                               telemetry_on, t0):
    """
    Handles the operator guidance loop when the agent is cancelled in auto-mode.
    Returns the result of the subsequent run_agent_single call, or 'interrupted'.
    """
    _emit("on_notice", "info",
          f"\n{BOLD}[Agent paused — enter guidance, or press Enter to resume]{RESET}")
    try:
        guidance = input("\nOperator: ").strip()
    except (EOFError, KeyboardInterrupt):
        log.info("Session ended (operator cancelled) | %d messages", len(conversation_history))
        if telemetry_on:
            telemetry.record_cycle(status="cancelled", duration_s=time.time() - t0)
            telemetry.shutdown()
        _log_bedrock_session_spend(log)
        _emit("on_notice", "info", "")
        return "interrupted"
    if guidance:
        expanded_g, files_g, err_g = _expand_file_refs(guidance)
        if err_g:
            _emit("on_error", err_g)
        else:
            if files_g:
                initial_files, expanded_g = _lead_absorb(conversation_history, initial_files,
                                                        expanded_g, files_g, log)
            conversation_history.append({"role": "user", "content": expanded_g})
            log.info("OPERATOR: %s", expanded_g)
    else:
        conversation_history.append({"role": "user", "content":
            "Continue where you left off. Finish your current cycle."})
        log.info("OPERATOR: [resume — no guidance]")
    return run_agent_single(conversation_history, summary_state, initial_files, log,
                           gen["temperature"], gen["top_p"], gen["top_k"],
                           gen["presence_penalty"], max_tokens, ctx_size,
                           async_summarizer=async_summarizer)


# Claim-vs-trace gate constants (module-level so they are not run_agent_single
# locals — see the claim-vs-trace comment in run_agent_single). Detect a past-
# tense assertion that verification/commit was PERFORMED.
_VERIFY_CLAIM_RE = re.compile(
    r"\b(ran|re-?ran|executed)\b[^.\n]{0,40}\b(test|pytest|suite|spec)s?\b"
    r"|\b(verified|tested|retested)\b"   # bare past-tense verification claim
    r"|\bconfirmed\b[^.\n]{0,25}\b(test|pass|work|fix|correct)"
    r"|\ball tests?(?:\s+\w+){0,3}\s+pass"
    r"|\btests?(?:\s+now)?\s+pass(?:es|ed|ing)?\b"
    r"|\b\d+\s+passed\b"
    r"|\btest suite passes\b",
    re.IGNORECASE)
_COMMIT_CLAIM_RE = re.compile(
    r"\bcommi+tt?ed\b|\bpushed the commit\b|\bcommitted (?:it|the|and)\b",
    re.IGNORECASE)
_VERIFY_CMD_TOKENS = ("pytest", "unittest", "npm test", "npm run test",
                      "go test", "cargo test", "make test", "make check",
                      "tox", "jest", "vitest", "phpunit", "rspec", "ctest",
                      "bats", "gradle test", "mvn test")


def _claim_vs_trace_unsupported(text, ran_verification, has_committed):
    """Return the list of actions the text CLAIMS were done but the trace does
    not support (empty = none). Pure — unit-testable in isolation."""
    if not text:
        return []
    out = []
    if _VERIFY_CLAIM_RE.search(text) and not ran_verification:
        out.append("running the tests / verifying")
    if _COMMIT_CLAIM_RE.search(text) and not has_committed:
        out.append("committing")
    return out


_CYCLE_STATUS_PATH = _state_path("cycle_status.json")


def _write_cycle_status(phase, result=None, turn=None, committed=None):
    """Machine-readable cycle status for a SUPERVISING HARNESS (stress test
    2026-08-16: a driver had nothing to poll but ANSI scrollback and misread
    a live-input prompt repaint as 'done', killing a working cycle).

    'running' on entry, the terminal result string ('done'/'error'/'stopped')
    on exit — a harness watches this file, not the terminal. Best-effort:
    never raise into the loop. Timestamp is omitted deliberately (Date.now()
    unavailable in some sandboxes; the mtime carries the clock)."""
    try:
        payload = {"phase": phase}
        if result is not None:
            payload["result"] = result
        if turn is not None:
            payload["turn"] = turn
        if committed is not None:
            payload["committed_this_cycle"] = bool(committed)
        os.makedirs(os.path.dirname(_CYCLE_STATUS_PATH), exist_ok=True)
        with open(_CYCLE_STATUS_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception:
        pass


def _run_turn_hooks(event):
    """Run the shell commands configured under preferences.turn_hooks[event].

    event is "start" or "end". A deployment that shows status (status dots that
    call a script telling an avatar server whether the agent is thinking) used to
    hang this on Claude Code's UserPromptSubmit and Stop hooks; this is the
    same thing for agent.py. Each command gets 5 s, output is discarded, and
    nothing here may raise into the loop: a broken indicator must never stop a
    turn. Commands run in the working directory, like the agent's own tools."""
    try:
        commands = (_config.get("preferences", {}).get("turn_hooks") or {}).get(event) or []
        if isinstance(commands, str):
            commands = [commands]
        for command in commands:
            try:
                subprocess.run(command, shell=True, timeout=5, capture_output=True,
                               stdin=subprocess.DEVNULL)
            except Exception:
                pass
    except Exception:
        pass


def run_agent_single(*args, **kwargs):
    """Thin wrapper: stamp cycle_status.json running->terminal around the real
    loop so a harness has a done-signal independent of terminal scrollback.
    Also fires the configured turn_hooks (see _run_turn_hooks)."""
    _write_cycle_status("running")
    _run_turn_hooks("start")
    _cache_keepalive_disarm()
    _summ.summary_idle_disarm()
    try:
        result = _run_agent_single_impl(*args, **kwargs)
        _write_cycle_status("complete", result=result)
        return result
    except BaseException as e:
        _write_cycle_status("complete", result=f"exception:{type(e).__name__}")
        raise
    finally:
        _run_turn_hooks("end")
        try:
            _log = args[3] if len(args) > 3 else kwargs.get("log")
            if _log is not None:
                _cache_keepalive_arm(_log)
                _summ.summary_idle_arm(_log)
        except Exception:  # the keepalive is a saving, never a failure
            pass


def _get_tc_args(tc):
    raw = tc.function.arguments if hasattr(tc, 'function') else tc["function"]["arguments"]
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _get_tc_name(tc):
    return tc.function.name if hasattr(tc, 'function') else tc["function"]["name"]


_NEXT = object()   # a step's answer: go to the next turn
_BREAK = object()  # a call's answer: no more calls this turn


class _Run:
    """One run of the agentic loop: the state a turn carries, the exit gates, and the
    loop's steps as methods. The locals of the former single function that cross a step
    are attributes under their old names; the step-local ones stayed locals."""

    def __init__(self, conversation_history, summary_state, initial_files, log, temperature,
                 top_p, top_k, presence_penalty, max_tokens, ctx_size, start_turn, async_summarizer):
        self.conversation_history = conversation_history
        self.summary_state = summary_state
        self.initial_files = initial_files
        self.log = log
        self.temperature = temperature
        self.top_p = top_p
        self.top_k = top_k
        self.presence_penalty = presence_penalty
        self.max_tokens = max_tokens
        self.ctx_size = ctx_size
        self.start_turn = start_turn
        self.async_summarizer = async_summarizer
        self._advisor_latches = {}   # once-per-run advisor escalation, per class

        # In TUI/live-input mode the cancel flag's lifetime is the TURN:
        # cancellable() deliberately skips its per-region reset there (a reset
        # per region erased esc-esc presses landing between regions), so the
        # turn boundary — here, the single entry point every caller shares —
        # is where a stale flag is discarded. This also drops an esc-esc
        # pressed while idle at the prompt, which must not cancel the next turn.
        if cancel_tui_mode():
            cancel_reset()

        self.turn = self.start_turn

        # Track repeated tool failures to break infinite loops
        self._recent_tool_errors = []  # list of (tool_name, error_snippet)
        self._REPEAT_THRESHOLD = 3    # inject forced think after this many identical failures

        # Track consecutive text-only responses (no tool calls).
        # Smaller models sometimes "think aloud" without calling a tool, intending
        # to continue on the next turn.  Auto-nudge up to _MAX_TEXT_ONLY times
        # before treating it as a real stop signal.
        self._consecutive_text_only = 0

        # Total nudge budget across the session.  Prevents infinite oscillation
        # where a weak tool call resets the consecutive counter but the model
        # never makes substantive progress.
        self._total_nudges = 0

        # Detect degenerate text loops — model repeating the same output.
        # Store hashes of recent text-only responses; bail if too many match.
        self._recent_text_hashes = []
        self._TEXT_LOOP_THRESHOLD = 3

        # Read-only tools: calls to these don't reset the consecutive text-only
        # counter because they don't represent substantive progress.
        self._READ_ONLY_TOOLS = {"think", "search_files", "read_pdf", "task_tracker", "read_file", "list_files"}

        # After 'git push', allow a few more turns for TRACK work (results file,
        # progress row, issue comments) before stopping on text-only response.
        self._cycle_persisted = False
        self._cycle_persisted_turn = None
        self._CYCLE_GRACE_TURNS = 7
        # WS8.3 (run 096): count of non-TRACK tool calls blocked after grace
        # exhausted (previously only text-only turns were bounded by grace;
        # tool-calling turns ran to the hard cap — ~15 wasted turns).
        self._grace_tool_blocks = 0
        # WS10.e rung 2: count of loop-breaker interventions this cycle; the
        # 2nd+ one escalates from text redirect to a system-invoked think.
        self._loop_interventions = 0
        # WS10.c: success-check gate state (strict type-validation — MagicMock
        # configs and misconfigs must leave the gate off, same rule as the
        # PhaseEngine factory).
        _sc_cycle_cfg = _config.get("cycle") if isinstance(_config, dict) else None
        self._success_check_cmd = (_sc_cycle_cfg.get("success_check")
                              if isinstance(_sc_cycle_cfg, dict) else None)
        if not (isinstance(self._success_check_cmd, str) and self._success_check_cmd.strip()):
            self._success_check_cmd = None
        self._success_check_blocks = 0
        self._SUCCESS_CHECK_MAX_BLOCKS = 3
        # Grind escalation: once per cycle, if a success_check is configured and the
        # model has burned this fraction of its turn budget with the check STILL
        # red (not converging — the distinct-plausible-actions-until-timeout mode
        # that trips no loop/stall/gate detector), suggest the advisor. Runs the
        # check at most once (latch), and only SUGGESTS — the glm call stays model-
        # gated — so a false positive costs nudge tokens, not a glm call.
        self._grind_checked = False
        self._GRIND_TURN_FRACTION = 0.7
        # Slow thinking-models are WALL-CLOCK bound, not turn bound: on a long task
        # the run hits its subprocess timeout well before 70% of the turn budget, so
        # the turn-based grind trigger never fires. cycle.grind_elapsed_s (seconds,
        # 0=off) fires grind on elapsed wall-clock too — whichever comes first.
        self._grind_t0 = time.monotonic()
        self._stream_errors = 0   # F6 (review finding 3): consecutive streaming exceptions; 3 = backend error exit
        try:
            self._GRIND_ELAPSED_S = float((_config.get("cycle") or {}).get("grind_elapsed_s", 0) or 0)
        except Exception:
            self._GRIND_ELAPSED_S = 0.0
        # F1 — wall-clock deadline state. The clock starts here (run start, monotonic), independent
        # of turns. `_deadline_hard` is read by the exit gates and the tool dispatcher below.
        try:
            self._DEADLINE_S = float((_config.get("cycle") or {}).get("deadline_s", 0) or 0)
        except Exception:
            self._DEADLINE_S = 0.0
        self._deadline_fracs = list((_config.get("cycle") or {}).get("deadline_warn_fracs") or [0.6, 0.8, 0.92])
        self._deadline_t0 = self._grind_t0
        self._deadline_fired = set()
        self._deadline_hard = False
        self._deadline_hard_turns = 0
        # F9 — capability-refusal stop state. Headless only: an interactive user answers a refusal
        # themselves. `_refusal_hard` is read by the same exit gates and dispatcher as `_deadline_hard`.
        global _LAST_REFUSAL
        _LAST_REFUSAL = None
        try:
            self._REFUSAL_MAX_TURNS = int((_config.get("cycle") or {}).get("refusal_max_turns", 5) or 0) \
                if _AUTO_MODE else 0
        except Exception:
            self._REFUSAL_MAX_TURNS = 0
        self._refusal_first_turn = None
        self._refusal_tool = None
        self._refusal_hard = False
        self._refusal_hard_turns = 0
        if self._DEADLINE_S > 0 and self._GRIND_ELAPSED_S <= 0:
            # a stuck run should escalate while there is still time to act on advice
            self._GRIND_ELAPSED_S = 0.5 * self._DEADLINE_S
            self.log.info("deadline %.0fs set — grind escalation defaults to %.0fs (50%%)", self._DEADLINE_S, self._GRIND_ELAPSED_S)
        # No-progress gate (fixes the M2 A/B regression: grind fired on a slow-but-
        # CONVERGING run at the elapsed mark and derailed a run the model would have
        # finished on its own — OFF 1/3 vs ON 0/3). Only escalate if the model has
        # NOT mutated a file in the last N turns: a converging coding run edits; a
        # genuinely stuck one spins on reads/re-runs without changing the codebase.
        # So the trigger distinguishes "stuck" from merely "slow". 0 disables it.
        self._last_mutation_turn = 0
        try:
            self._GRIND_NO_PROGRESS_TURNS = int(
                (_config.get("cycle") or {}).get("grind_no_progress_turns", 4) or 0)
        except Exception:
            self._GRIND_NO_PROGRESS_TURNS = 4
        # Claim-vs-trace gate: the fabricated-verification failure mode (d2 probe —
        # the model fixes the code, then asserts "verified with pytest / committed"
        # having run NO pytest and made NO commit; measured 4/8-5/8 on Qwen-27B, and
        # NO sampling/MTP/temperature knob moves it (temp already 0 + MTP off still
        # ~50-62%), so the only fix is structural: reject a completion whose claim the
        # trace does not support). _ran_verification = a test/success-check command
        # actually ran SINCE the last edit — an edit resets it, so a stale pre-edit
        # run does not count as verification of the current state.
        self._ran_verification = False
        self._claim_trace_blocks = 0
        # OFF BY DEFAULT (2026-08-15): the claim-phrase matcher false-positives
        # on conversational/creative text — a story containing 'the test passes'
        # drew a correction demanding a real tool run, and the model dutifully
        # ls'd an empty folder and apologized. Coding sessions should turn it on
        # (the /setup wizard asks, recommending it for coding projects); it
        # writes cycle.claim_trace_max_blocks=2.
        try:
            self._CLAIM_TRACE_MAX_BLOCKS = int(
                (_config.get("cycle") or {}).get("claim_trace_max_blocks", 0) or 0)
        except Exception:
            self._CLAIM_TRACE_MAX_BLOCKS = 0

        self._result_contract_blocks = 0
        try:
            self._RESULT_CONTRACT_MAX_BLOCKS = int((_config.get("cycle") or {}).get("result_contract_max_blocks", 2))
        except Exception:
            self._RESULT_CONTRACT_MAX_BLOCKS = 2
        # Hard cap: even with ongoing tool calls, end the cycle this many turns
        # after persist.  Prevents post-TRACK drift into a second PERCEIVE.
        self._CYCLE_HARD_STOP_TURNS = 15

        # Track whether any commit has been made.  Completion signals are ignored
        # until a commit lands — prevents the agent from declaring "done" before
        # any work is actually persisted.
        self._has_committed = False

        # Reviewer-role persistence signal.  Reviewers rarely commit code; their
        # persistent outputs are `gh pr review` verdicts and appends to
        # CICD/reviews.md.  Tracked separately so completion signals are honored
        # once a verdict has actually landed.
        self._has_reviewer_persisted = False

        # Track whether any file has been written/edited.  If no edit by turn 30,
        # inject a nudge telling the agent to start coding or declare null result.
        self._has_edited = False
        self._EDIT_DEADLINE_TURN = 20
        self._edit_nudge_sent = False
        # Open-task reminder fires at most once per cycle (at completion signal or
        # grace-period exhaustion) to surface remaining task_tracker items.
        self._open_task_nudge_sent = False

        # Per-session tools list — starts as a copy of the module-level tools so
        # we can add end_cycle without mutating the shared list across sessions.
        self._session_tools = list(tools)
        # Lazy tools: what this session loaded before (a resume, an earlier turn), then what
        # the task text asks for by name or a clear match, all before the first request.
        _apply_tool_loads(self._session_tools, self.log, initial=True)
        if _TOOLS_CFG.get("preload_from_prompt", True):
            _preload_text = next((m.get("content") for m in reversed(self.conversation_history)
                                  if m.get("role") == "user" and isinstance(m.get("content"), str)), "")
            _preloaded = _registry.preload_from_text(_preload_text)
            if _preloaded:
                self.log.info("tools.preloaded %s", ", ".join(_preloaded))
            _apply_tool_loads(self._session_tools, self.log)
        # Skills: the first message of the session is judged now (nothing is cached yet);
        # later ones on a worker thread. Queued loads join the newest message at the turn.
        _skills_on_entry(self.conversation_history, self.log)
        # end_cycle is unlocked (appended to _session_tools) after the first nudge
        # fires so the agent can't skip the cycle by calling it before doing work.
        self._end_cycle_unlocked = not _END_CYCLE_ENABLED  # off (the default): never offered
        if _prompt_caching_backend() and not self._end_cycle_unlocked and not any(
                (t.get("function") or {}).get("name") == "end_cycle" for t in self._session_tools):
            # P4.4: tools render FIRST in the request, so adding one mid-run invalidates the
            # whole cached prompt. Offer end_cycle from turn 1. It is NOT gated on the
            # first-nudge unlock: a model that finishes cleanly is never nudged, and live it
            # had its correct end_cycle refused. The success-check / phase / CICD gates still
            # apply, and ending this way is what a text-only finish would do anyway.
            self._session_tools.append(_end_cycle_tool.definition)

        # Reviewer sessions rarely make code edits (they verify and merge), so the
        # edit-deadline nudge is a false positive for that role.  Detect by
        # scanning the initial prompt for the reviewer-template marker.
        # WS1: explicit --role wins; prompt string-matching is the legacy fallback.
        global _active_phase_engine
        if _explicit_role is not None:
            self._is_reviewer_role = _explicit_role == "reviewer"
        else:
            self._is_reviewer_role = any(
                isinstance(m.get("content"), str) and "CICD Reviewer" in m["content"]
                for m in self.conversation_history[:2]
            )
            if self._is_reviewer_role:
                self.log.debug("role detected by prompt string-match (legacy) — prefer --role")

        # WS1: PhaseEngine — inert unless config opts in (cycle.profile/phases).
        from phase_engine import build_phase_engine
        self._phase_engine = build_phase_engine(_config, self.log)
        _active_phase_engine = self._phase_engine
        if self._phase_engine is not None:
            self.log.info("PhaseEngine active: profile=%s", self._phase_engine.profile)

        # cycle 86 (issue #425): CICD-specific guards (e.g. cycle 44 requiring
        # `Closes #N`) only apply in the self-improvement builder loop, not in
        # general agent sessions on other repos.  Detect by the same strategy as
        # _is_reviewer_role — the builder template always opens with
        # "CICD Improvement Loop — Builder".
        if _explicit_role is not None:
            # builder flag semantically means "CICD session" — reviewers carry it
            # too (see WS8.2 note in _validate_tool_call); creature = neither.
            self._is_cicd_builder = _explicit_role in ("builder", "reviewer")
        else:
            self._is_cicd_builder = any(
                isinstance(m.get("content"), str) and "CICD Improvement Loop" in m["content"]
                for m in self.conversation_history[:2]
            )

        # Detect tool-call loops: same command signature repeated N times.
        self._recent_tool_sigs = []  # list of (frozenset of (name, args_hash)) tuples
        self._TOOL_LOOP_THRESHOLD = 3  # inject correction after 3 identical batches

        # Semantic result-loop detection: same tool returning same result despite
        # different arguments.  Catches cases where the batch-signature detector
        # misses because args vary slightly each time.
        self._recent_tool_results = []  # list of (func_name, result_hash) tuples
        self._RESULT_LOOP_WINDOW = 8
        self._RESULT_LOOP_THRESHOLD = 3

        # Per-call dedup cache. Maps (func_name, args_md5) → {
        #   "call_idx": int, "summary": str (first 80 chars), "tokens": int
        # }. On exact-duplicate call within _DEDUP_WINDOW calls, return a synthetic
        # tool result and skip dispatch — saves 30-40% of token budget per long
        # cycle (a field audit: `cat focus.json` x3, `git log -5` x5 in one
        # session, a field-observed run Orphan Paradox audit-rewrite-audit loop). Only applied
        # to tools that are SAFE to dedup (pure reads, no network/wallclock); see
        # _is_dedupable_call() for the policy.
        self._call_dedup_cache = {}
        self._DEDUP_WINDOW = 8
        self._call_idx_counter = 0  # monotonic per-tool-call counter

        # Session-wide identical-call frequency for search_files / find_symbol.
        # The dedup cache only covers _DEDUP_WINDOW=8 calls; the model can evade it
        # by inserting other tool calls between retries.  This counter persists for
        # the full session and fires a forced-think after _SEARCH_REPEAT_THRESHOLD
        # real (non-deduped) dispatches of the same (func, args) pair.
        self._search_sig_counts: dict = {}   # (func_name, args_md5) → dispatch count
        self._SEARCH_REPEAT_THRESHOLD = 3

        # Per-path write tracker for the write-loop detector. Each entry maps
        # `target_path` → list of call_idx values where it was written. On 3rd+
        # write within _WRITE_LOOP_WINDOW, append a system reminder to that
        # turn's tool result. The canonical observed loop: 3 write-audit-rewrite
        # passes on state/memories/context.json in one cycle.
        self._write_path_history = {}
        self._WRITE_LOOP_WINDOW = 8
        self._WRITE_LOOP_THRESHOLD = 3

        # Tier 5 — per-session patch-effectiveness counters. Mirrors the
        # telemetry.record_patch_event calls so a verbose cycle-end log line
        # gives the operator local visibility without grepping the OTEL/Grafana
        # dashboard. Keys match the patch identifiers in telemetry docs.
        self._patch_telemetry = {
            "harmony_reject": 0,        # T1.1 — Harmony control tokens in tool args
            "dedup": 0,                  # T1.2 — exact-duplicate read calls deduped
            "write_loop": 0,             # T1.2 — write-loop detector tripped
            "indent_guard": 0,           # T4.10 — slice-write indent mismatch rejected
            "schema_warning": 0,         # T4.11 — JSON overwrite dropped top-level keys
            "think_launder": 0,          # T2.7 — think prompt re-included recent context
            "stall_abort": 0,            # T3.8 — stream stall guard fired
            "bootstrap_create": 0,       # T3.9 — placeholder file auto-created
            "edit_nudge": 0,             # T5.14 — heredoc-write hint emitted
            "summary_fired": 0,          # T5.13 — async summary launched
            "summary_gated": 0,          # T5.13 — summary skipped (under threshold)
        }

        # CICD phase tracking — the module's state, so the context window and the summary
        # prompt read the same values, surviving summary compression.
        _cicd.reset()
        self._cicd_think_used = False  # reset each cycle; set True when think() is called
        self._cicd_pr_ready_called = False  # tracks whether `gh pr ready` was called before merge
        self._cicd_issue_view_called = False  # tracks whether `gh issue view` was called before merge (PRE-MERGE CHECK)

        self._async_summarizer = self.async_summarizer
        _summ.SUMMARY_IDLE["run"] = {"history": self.conversation_history, "state": self.summary_state, "summarizer": self.async_summarizer,
                                "initial_files": self.initial_files, "ctx_size": self.ctx_size, "max_tokens": self.max_tokens}

        # T5.15 — stall-retry budget (across the cycle, not per-turn). When the
        # streaming stall guard (T3.8) trips, the framework adjusts sampling for
        # the NEXT request and continues to retry. Two retries by default —
        # first with -0.3 temp / +0.05 repeat_penalty; second with an additional
        # -0.2 temp / +0.05 penalty (deeper escalation). Clears automatically
        # once a turn produces deltas, so a single stall in a healthy session
        # doesn't permanently degrade sampling for the rest of the cycle.
        self._stall_retries_budget = int(os.environ.get("AGENT_STALL_RETRIES", "2"))
        self._stall_retries_remaining = self._stall_retries_budget
        self._stall_sampling_override = None  # set when stall detected; cleared on success

        # Gateway-timeout recovery: when every retry on a turn returns 504, inject a
        # size-reduction hint and retry the turn rather than immediately aborting.
        # Capped at 2 recoveries per cycle so a persistently slow model doesn't loop forever.
        self._gateway_timeout_recovery_count = 0
        self._GATEWAY_TIMEOUT_RECOVERY_MAX = 2

        # Action-inertia detection: count consecutive turns where the model only reads
        # files without writing or executing anything. After the threshold, inject a
        # nudge to commit to the fix rather than reading more files.
        self._read_only_turns = 0
        self._READ_ONLY_NUDGE_THRESHOLD = 5
        self._cur_req_sig = None       # _request_signature of the request being sent (see _CACHE_REQ)
        self._READ_ONLY_NUDGE_THRESHOLD = 5
        # F7 — repeat-read stall signature (one nudge per unique call key, latched)
        try:
            _rr_cfg = (_config.get("cycle") or {}).get("repeat_read_nudge") or {}
            self._REPEAT_READ_N = int(_rr_cfg.get("n", 3)) if isinstance(_rr_cfg, dict) else 3
            self._REPEAT_READ_WINDOW = int(_rr_cfg.get("window", 6)) if isinstance(_rr_cfg, dict) else 6
        except Exception:
            self._REPEAT_READ_N, self._REPEAT_READ_WINDOW = 3, 6
        self._repeat_seen, self._repeat_latched, self._repeat_msg = {}, set(), None
        # F4 — goal anchoring + deliverable guard state (config or --goal/--deliverable; derived
        # from the prompt stanza by run_agent_interactive when the contract is armed)
        self._goal = str((_config.get("cycle") or {}).get("goal") or "") if isinstance((_config.get("cycle") or {}).get("goal"), str) else ""
        self._deliverables = [str(d) for d in ((_config.get("cycle") or {}).get("deliverables") or []) if isinstance(d, str)] \
            if isinstance((_config.get("cycle") or {}).get("deliverables"), list) else []
        self._anchor_fracs = [f for f in ((_config.get("cycle") or {}).get("goal_anchor_fracs") or []) if isinstance(f, (int, float))] \
            if isinstance((_config.get("cycle") or {}).get("goal_anchor_fracs"), list) else []
        self._anchor_fired = set()
        self._deliverable_blocks = 0
        self._turn_had_action = False  # set True when write/edit/exec called this turn

        # Consecutive edit_file failures per path — after 2 consecutive failures on
        # the same file, inject a user turn telling the model to stop and rewrite.
        self._edit_fail_counts: dict = {}

        # "Agent:" header discipline (field report 2026-08-14): the label marks the
        # START of a contiguous agent-output run and prints once per run — lazily,
        # on the first TEXT token (a tool-only response never earns a bare label).
        # A queued user message folded in mid-burst starts a new run, so the label
        # re-appears after it as a continuation marker.
        self._agent_header_owed = True

    def _funnel_ev(self, ev, **kw):
        """Append one advisor-escalation funnel event (guarded; never raises).
        LOCAL per-run observability: telemetry is OTLP/Prometheus-gated and the
        replay results schema has no advisor field, so without this a re-run
        cannot say whether the advisor engaged or WHICH guard suppressed it.
        replay_suite reads this back into the result row before worktree cleanup.
        Events: reached | suppressed_latch | no_escalate | suggested |
        auto_invoke_call | auto_invoke_answered | auto_invoke_error.
        Path literal is inlined (not a run_agent_single local) to avoid the
        test_no_dead_locals nested-scope false positive."""
        try:
            rec = {"ev": ev}
            rec.update(kw)
            try:
                rec["t_s"] = round(time.monotonic() - self._grind_t0, 1)
            except Exception:
                pass
            with open(os.path.join(".agent", "advisor_funnel.jsonl"), "a") as _f:
                _f.write(json.dumps(rec) + "\n")
        except Exception:
            pass

    def _build_advisor_context(self, fail_output):
        """Assemble GROUNDED context for the advisor auto-invoke: the task, the
        exact success-check command, its failing output, and the agent's most
        recent action. Without this framing GLM misread a code-fixing agent as a
        chatbot and returned "rewrite your system prompt" meta-advice. Bounded
        (the tool distills further); guarded — falls back to the raw output."""
        try:
            parts = []
            try:
                for _m in self.conversation_history:
                    if isinstance(_m, dict) and _m.get("role") == "user" and _m.get("content"):
                        parts.append("TASK (what I must accomplish):\n"
                                     + str(_m["content"])[:800])
                        break
            except Exception:
                pass
            if self._success_check_cmd:
                parts.append("SUCCESS CHECK (must exit 0):\n$ " + str(self._success_check_cmd))
            if fail_output and str(fail_output).strip():
                parts.append("CURRENT FAILING OUTPUT:\n" + str(fail_output)[:1600])
            try:
                for _m in reversed(self.conversation_history):
                    if (isinstance(_m, dict) and _m.get("role") == "assistant"
                            and str(_m.get("content") or "").strip()):
                        parts.append("WHAT I LAST TRIED:\n" + str(_m["content"])[:600])
                        break
            except Exception:
                pass
            return "\n\n".join(parts) if parts else str(fail_output)[:3000]
        except Exception:
            return str(fail_output)[:3000]

    def _maybe_advisor_nudge(self, source="gate", context=""):
        """Return a one-time advisor-escalation suggestion string if the current
        signals warrant escalation and the advisor tier is enabled; else "".

        Three trigger sources, all routed through escalation_policy:
          - "gate": the success-check gate has blocked repeatedly
            (_success_check_blocks) — the model declared done while wrong.
          - "stall": the model is cognitively STUCK — repeated a failing action
            AND ignored a prior redirect (the loop-intervention ladder).
          - "grind": the model has spent most of its turn budget with the
            success check STILL failing — it is not converging (distinct
            plausible actions until wall-clock). This is the exact failure mode
            the k=3 A/B runs died on, which trips NO other detector.

        gate is structural (_success_check_blocks); stall/grind are the
        "self_reported_stuck" path — a stuck model never cleanly declares done,
        so the gate never sees it. gate SUGGESTS; grind/stall AUTO-INVOKE the
        GLM call (bounded by the tool's own budget). Once per cycle PER CLASS
        (split _auto_fired/_suggest_fired latch — a suggestion must not starve an
        auto-invoke; cycle.advisor_legacy_shared_latch restores the old single
        latch). Fully guarded: never raises, can't break a gate or the turn loop."""
        try:
            _adv_cfg = (_config.get("advisor", {})
                        if isinstance(_config, dict) else {})
            # Default-ON with main fallback — advisor_active is the single
            # source of truth (explicit "enabled" wins; otherwise the advisor
            # self-consults main's endpoint, so it is on whenever main is).
            try:
                from tools.consult_advisor import advisor_active as _aa
                _active = _aa(_config if isinstance(_config, dict) else {})
            except Exception:
                _active = _adv_cfg.get("enabled",
                                       bool(_adv_cfg.get("base_url")))
            if not _active:
                return ""
            # Per-CLASS once-per-cycle latch. The gate source is suggestion-only;
            # grind/stall AUTO-INVOKE the (expensive but tool-budget-bounded) GLM
            # call. A single shared latch let a cheap gate SUGGESTION consume the
            # one budgeted escalation and silently starve grind's auto-invoke on
            # the same cycle — measured: gate-arm grind preconditions met, 0
            # consult_advisor calls. Split the latch so a suggestion can't block
            # an auto-invoke (still ≤1 of EACH class per cycle). The legacy shared
            # latch is restorable via cycle.advisor_legacy_shared_latch for a
            # clean before/after of this fix.
            _auto = source in ("stall", "grind")
            _legacy_latch = bool((_config.get("cycle") or {}).get(
                "advisor_legacy_shared_latch", False)) if isinstance(_config, dict) else False
            _latch = "_fired" if _legacy_latch else (
                "_auto_fired" if _auto else "_suggest_fired")
            self._funnel_ev("reached", source=source, auto=_auto, latch=_latch)
            if self._advisor_latches.get(_latch, False):
                self._funnel_ev("suppressed_latch", source=source, latch=_latch)
                return ""
            _stuck = _auto
            from escalation_policy import should_escalate, LoopSignals
            _dec = should_escalate(LoopSignals(
                consecutive_gate_failures=self._success_check_blocks,
                self_reported_stuck=_stuck,
                task_difficulty=str(_adv_cfg.get("task_difficulty", "unknown")),
                advisor_calls_cap=int(_adv_cfg.get("max_calls_per_task", 3)),
            ))
            if not _dec.escalate:
                self._funnel_ev("no_escalate", source=source)
                return ""
            self._advisor_latches[_latch] = True
            # E2: on a backend with an effort knob, raise effort first (same model, so
            # the cache and stored thinking survive; once per run). The advisor path
            # below still runs.
            _esc = getattr(_main_backend, "escalate_effort", None)
            if callable(_esc):
                _new_effort = _esc()
                if _new_effort:
                    self.log.info("effort escalated to %s (source=%s)", _new_effort, source)
                    telemetry.record_patch_event("effort_escalation", kind=source + ":" + _new_effort)
                    _emit("on_notice", "info", f"[raising reasoning effort to {_new_effort}]")
            # First-class telemetry (same pipeline as success_check_block): the
            # trigger SUGGESTED escalation. Pairs with the tool-side advisor_call
            # outcomes + record_tool_call("consult_advisor") to form the funnel
            # suggested -> called -> answered, queryable per-instance and
            # dashboardable for CICD/beewatcher. kind carries the trigger source
            # so a dashboard can split grind/stall/gate-driven escalations.
            telemetry.record_patch_event(
                "advisor_escalation", kind=source + ":" + _dec.mode)
            _how = ("consider a deliberate takeover via subagent"
                    if _dec.mode == "takeover" else "call consult_advisor")
            _situation = {
                "stall": "you appear STUCK (repeated a failing action and a "
                         "prior redirect was ignored)",
                "grind": "you have spent most of your turn budget and the "
                         "success check is STILL failing — you are not converging",
                "gate": f"the success check has FAILED {self._success_check_blocks}x",
            }[source]
            _reason = {
                "stall": "stuck: repeated-failure loop",
                "grind": "grind: turn-budget spent, check still red",
                "gate": f"failed success_check x{self._success_check_blocks}",
            }[source]
            self.log.info("advisor escalation suggested (source=%s, mode=%s, "
                     "triggers=%s)", source, _dec.mode, _dec.triggers)
            # AUTO-INVOKE for the stuck-state triggers (grind/stall): the k=3 +
            # live-panel demo showed the 27B IGNORES a suggestion to escalate
            # (mirrors its M2/P1 completion-discipline gap). So mirror the
            # forced-THINK ladder — CALL consult_advisor directly and inject its
            # guidance, rather than suggesting the model do it. Bounded by the
            # tool's own max_calls_per_task budget; gate/takeover stay suggest-
            # only. Emit record_tool_call so the funnel "calls" panel counts
            # auto-invokes too (dispatcher record_tool_call is bypassed here).
            if (source in ("stall", "grind") and _dec.mode != "takeover"
                    and "consult_advisor" in MAP_FN):
                try:
                    # Role-grounded, anti-meta framing. GLM previously drifted to
                    # chatbot advice ("rewrite your system prompt") because the
                    # question didn't establish this is a CODING agent whose
                    # success check is a shell/test command. State the role, feed
                    # the task + exact check command + failing output + last
                    # action, and forbid meta-advice.
                    _q = ("I am an autonomous CODING agent stuck mid-task in a git "
                          "repository. I repeated a failing action and a prior "
                          "redirect was ignored. Using the task, the failing check "
                          "command and output, and what I last tried (all below), "
                          "give ONE concrete code-level next action that is "
                          "materially different from what I tried. No meta-advice "
                          "about prompts or process."
                          if source == "stall" else
                          "I am an autonomous CODING agent in a git repository. "
                          "Success is a shell command (a test/check) exiting 0; it "
                          "is STILL FAILING after most of my turn budget. Using the "
                          "task, the failing check command and its output, and what "
                          "I last tried (all below), give ONE concrete code-level "
                          "fix to make that command pass. No meta-advice about "
                          "prompts or process — I need the specific fix.")
                    _adv_context = self._build_advisor_context(context)
                    self._funnel_ev("auto_invoke_call", source=source,
                               ctx_chars=len(_adv_context))
                    # F1: a consult must FIT before the deadline. Same arithmetic the tool
                    # uses for its own timeout; if it cannot finish in the time left, inject
                    # the pending-consult notice instead and let the run wrap without it.
                    if self._DEADLINE_S > 0:
                        try:
                            from tools.consult_advisor import derive_timeout_s as _dts
                            _need = int(_dts(_config.get("advisor") or {}))
                        except Exception:
                            _need = 0
                        _left = self._DEADLINE_S - (time.monotonic() - self._deadline_t0)
                        if _need and _left < _need:
                            self._funnel_ev("auto_invoke_skipped_deadline", source=source,
                                       need_s=_need, left_s=int(_left))
                            self.log.warning("advisor auto-invoke skipped: needs ~%ds, %ds left before the deadline",
                                        _need, int(_left))
                            return ("[SYSTEM: escalation to the advisor was due (" + _situation +
                                    f") but cannot finish before the deadline ({int(_left)}s left, "
                                    f"consult needs ~{_need}s). Wrap up with what you have.]")
                    telemetry.record_tool_call("consult_advisor")
                    _advice = MAP_FN["consult_advisor"](
                        question=_q, context=_adv_context, reason=_reason)
                    self._funnel_ev("auto_invoke_answered", source=source,
                               chars=len(str(_advice)))
                    self.log.info("advisor auto-invoked (source=%s)", source)
                    return ("[SYSTEM: the heavyweight advisor was consulted "
                            "because " + _situation + ". APPLY its guidance now:"
                            "\n" + str(_advice)[:2500] + "]")
                except Exception as _ai_e:  # fall back to a suggestion
                    self._funnel_ev("auto_invoke_error", source=source,
                               err=str(_ai_e)[:200])
                    self.log.warning("advisor auto-invoke failed: %s", _ai_e)
            self._funnel_ev("suggested", source=source, mode=_dec.mode)
            return (
                "[SYSTEM: escalation available — " + _situation +
                f", past the fast model's reliable range ({', '.join(_dec.triggers)}). "
                f"{_how}(question='the specific blocker', context='the failing "
                f"output + what you already tried', reason='{_reason}') to get "
                "the heavyweight advisor's decisive fix, then apply it. Do this "
                "once — don't loop.]"
            )
        except Exception as _adv_e:  # never let the hook break a gate/turn loop
            self.log.debug("advisor escalation hook skipped: %s", _adv_e)
            return ""

    def _success_gate_blocks_exit(self, path_name):
        """WS10.c shared exit gate. Telemetry (2026-07-12) showed the agent
        has (at least) THREE clean-exit paths — end_cycle tool, completion-
        signal return, and the no-nudge text-only stop — and the gate
        guarded only the first, which replay runs never take. One closure,
        every exit. Returns True if the exit was blocked (caller: inject the
        redirect already appended and continue)."""
        if not self._success_check_cmd:
            return False
        if self._deadline_hard or self._refusal_hard:
            # F1/F9: past the deadline (or a refusal stop) the gate must not hold the run for a
            # fix it has no time for — the result says the check failed; the supervisor reads
            # the exit code and the result block.
            self.log.info("success_check gate stood down — %s (%s exit)",
                     "deadline reached" if self._deadline_hard else "capability refusal stop", path_name)
            return False
        if self._success_check_blocks >= self._SUCCESS_CHECK_MAX_BLOCKS:
            return False
        # F9: this gate polices "done while RED". A final message whose result block says
        # blocked / failed / cannot-tell is not claiming done, and turning it back asks the run
        # to manufacture a pass for a check it has already said it cannot honestly satisfy.
        # Measured: a run that met a capability refusal at turn 2 and reported blocked
        # correctly at turn 5 was turned back HERE with "the check still FAILS, fix it", and
        # spent five more turns writing labelled placeholder files so that a file-existence
        # check would pass. The harness, not the model, produced that outcome.
        _final_obj, _ = _extract_result_block(_last_assistant_text(self.conversation_history))
        if _final_obj is not None and _final_obj.get("status") in ("blocked", "failed", "cannot-tell"):
            self.log.info("success_check gate stood down — the run reports %s (%s exit)",
                     _final_obj.get("status"), path_name)
            return False
        _sc_rc, _sc_out = _run_success_check(self._success_check_cmd, self.log)
        if _sc_rc == 0:
            self.log.info("success_check PASSED — %s exit permitted", path_name)
            # The harness just verified the current state — a subsequent
            # "I ran the tests" claim is now supported (don't trip the
            # claim-vs-trace gate on a genuinely-verified completion).
            self._ran_verification = True
            return False
        self._success_check_blocks += 1
        self.log.info("success_check FAILED (rc=%d) — %s exit blocked (%d/%d)",
                 _sc_rc, path_name, self._success_check_blocks,
                 self._SUCCESS_CHECK_MAX_BLOCKS)
        telemetry.record_patch_event("success_check_block", kind=path_name)
        self.conversation_history.append({"role": "user", "content": (
            "Error: you are ending the cycle but the declared success check "
            "still FAILS (exit=" + str(_sc_rc) + "). The work is NOT "
            "complete. Output tail:\n" + _sc_out +
            "\nFix what the check reports, re-run it yourself to confirm, "
            "then commit and finish."
        )})
        _adv_msg = self._maybe_advisor_nudge()
        if _adv_msg:
            self.conversation_history.append({"role": "user", "content": _adv_msg})
        return True

    def _result_contract_blocks_exit(self, path_name, text):
        """F2 exit interception, claim-guard style, on the SAME three exits the success gate
        guards. The final message must end with a fenced JSON block that carries
        "contract": 1 and validates. Missing/invalid → inject a correction and continue,
        bounded by cycle.result_contract_max_blocks; past the bound, or past the deadline, the
        exit proceeds and the result-file writer synthesizes a failed record (exit 11)."""
        if not _result_contract_armed():
            return False
        obj, why = _extract_result_block(text)
        if obj is not None:
            ok, vwhy = _validate_result(obj, _RESULT_CONTRACT_SCHEMA or RESULT_CONTRACT_DEFAULT_SCHEMA)
            if ok:
                self.log.info("result contract satisfied — %s exit permitted", path_name)
                return False
            why = f"the result block is invalid: {vwhy}"
        if self._deadline_hard or self._refusal_hard or self._result_contract_blocks >= self._RESULT_CONTRACT_MAX_BLOCKS:
            self.log.warning("result contract UNSATISFIED at %s (%s) — %s; a record will be synthesized",
                        path_name, why, "deadline reached" if self._deadline_hard
                        else ("capability refusal stop" if self._refusal_hard else "correction bound reached"))
            return False
        self._result_contract_blocks += 1
        self.log.info("result contract unsatisfied (%s) — %s exit redirected (%d/%d)",
                 why, path_name, self._result_contract_blocks, self._RESULT_CONTRACT_MAX_BLOCKS)
        telemetry.record_patch_event("result_contract_block", kind=path_name)
        self.conversation_history.append({"role": "user", "content": (
            "Error: you are ending the run but your message does not end with a valid result "
            "block (" + why + "). Reply again and END with one fenced ```json block: "
            "{\"contract\": 1, \"status\": \"done|failed|blocked|cannot-tell\", \"summary\": ..., "
            "\"artifacts\": [...], \"verify_output\": ..., \"scope\": {...}}. The \"contract\": 1 key is "
            "what makes it the result; \"failed\" and \"blocked\" are acceptable statuses.")})
        return True

    def _deliverable_guard_blocks_exit(self, path_name, text):
        """F4: 'done' while a named deliverable is absent draws the standard correction
        (bounded by result_contract_max_blocks). Polices CLAIMS, not outcomes: a result block
        whose status is blocked/failed/cannot-tell passes untouched, and so does the exit past
        the deadline. Nothing configured → never fires."""
        if not self._deliverables or self._deadline_hard or self._refusal_hard:
            return False
        obj, _why = _extract_result_block(text)
        if obj is not None and obj.get("status") in ("blocked", "failed", "cannot-tell"):
            return False
        missing = _missing_deliverables(self._deliverables)
        if not missing:
            return False
        if self._deliverable_blocks >= self._RESULT_CONTRACT_MAX_BLOCKS:
            self.log.warning("deliverable guard: %s exit proceeds with %d deliverable(s) still missing (bound reached): %s",
                        path_name, len(missing), missing[:5])
            return False
        self._deliverable_blocks += 1
        self.log.info("deliverable guard: %s exit redirected (%d/%d) — missing %s",
                 path_name, self._deliverable_blocks, self._RESULT_CONTRACT_MAX_BLOCKS, missing[:5])
        telemetry.record_patch_event("deliverable_guard_block", kind=path_name)
        self.conversation_history.append({"role": "user", "content": (
            "Error: you report done, but these named deliverables do not exist: "
            + ", ".join(missing[:8]) + ". Produce them now, or change your status to "
            "blocked/failed and say why.")})
        return True

    def _claim_vs_trace_block(self, text, ran_verification, has_committed, blocks, max_blocks):
        """Reject a completion whose CLAIM the trace does not support — the
        fabricated-verification failure mode (d2 probe: the model fixes the code
        then asserts 'verified with pytest / committed' having run neither).
        Fires only when the model CLAIMS it ran the tests / committed AND no such
        tool call happened since the last edit — so a false positive is impossible
        when the action was genuinely performed. Bounded by max_blocks; on a block,
        appends a correction nudge. Returns (blocked, new_blocks).
        Orthogonal to _success_gate_blocks_exit: that catches 'done while RED';
        this catches 'claimed an action never taken' (invisible without the trace,
        and unfixable by any sampling knob — hence structural). State is passed in
        (not closed over) so the reads register in the caller's scope."""
        if self._deadline_hard or self._refusal_hard:
            return False, blocks   # F1/F9: past a hard stop, accept the final result as given
        if not text or max_blocks <= 0 or blocks >= max_blocks:
            return False, blocks
        _unsupported = _claim_vs_trace_unsupported(text, ran_verification, has_committed)
        if not _unsupported:
            return False, blocks
        blocks += 1
        self.log.info("claim-vs-trace: unsupported claim(s) %s — exit blocked (%d/%d)",
                 _unsupported, blocks, max_blocks)
        telemetry.record_patch_event(
            "claim_vs_trace_block",
            kind="+".join(s.split()[0] for s in _unsupported))
        self.conversation_history.append({"role": "user", "content": (
            "You reported " + " and ".join(_unsupported) + ", but there is NO "
            "such tool call in this session — you did not actually do it. Do not "
            "report an action you did not perform. Run it now with exec_command "
            "and show the real output before you finish."
        )})
        return True, blocks

    def _claim_gate_blocks_textonly_exit(self):
        """Backstop the claim-vs-trace gate at the nudge-enabled give-up / strip
        exits (2026-07-15, WS10.c sibling-path class). The three phrase-gated
        exits (completion_signal / first_textonly_completion / no-nudge
        textonly_stop) only run the claim gate when full_content matches the
        fixed _completion_signals list. In a nudge-ENABLED cycle this model
        routinely ends with an UNRECOGNISED phrase ('All done. The test
        passes.') that matches no signal, so those exits are skipped and the run
        terminates via the first-text-only strip / consecutive-text-only /
        nudge-budget / overtime path where the gate was never wired — a
        fabricated 'verified/committed' claim escapes unchecked. Honesty-of-
        claim is orthogonal to done-intent, so guard the exits here rather than
        broadening _completion_signals (which would couple the two and widen the
        blast radius into completion-acceptance + the success gate). Reads
        full_content/_ran_verification/_has_committed from the enclosing turn;
        _claim_trace_blocks stays a run_agent_single local (also read/written at
        the three inline sites) so no dead-locals false positive. Bounded by
        _CLAIM_TRACE_MAX_BLOCKS, so every caller's continue-instead-of-return is
        safe. Returns True if the exit was blocked."""
        _blk, self._claim_trace_blocks = self._claim_vs_trace_block(
            self.full_content, self._ran_verification, self._has_committed,
            self._claim_trace_blocks, self._CLAIM_TRACE_MAX_BLOCKS)
        return _blk

    def run(self):
        """The turn loop. Each step returns None to go on, _NEXT for the next turn, or
        the run's result."""
        while True:
            for step in self._STEPS:
                r = step(self)
                if r is _NEXT:
                    break
                if r is not None:
                    _act.idle()
                    return r

    def _begin_turn(self):
        self.turn += 1
        _act.turn_started(self.turn)
        # Grind escalation (turn-budget): once per cycle, if a success_check is
        # configured and the model has burned most of its budget with the check
        # still red, it is grinding (distinct actions, no convergence) — the
        # failure mode that trips no loop/stall/gate detector. Runs the check at
        # most once (latch); suggest-only, so the glm call stays model-gated.
        _grind_turn_hit = (_MAX_TURNS > 0
                           and self.turn >= int(self._GRIND_TURN_FRACTION * _MAX_TURNS))
        _grind_time_hit = (self._GRIND_ELAPSED_S > 0
                           and (time.monotonic() - self._grind_t0) >= self._GRIND_ELAPSED_S)
        # No-progress gate: only escalate if the model hasn't mutated a file in
        # the last N turns (genuinely stuck), not merely slow-but-converging.
        # Left OUT of the latch when it blocks, so the check re-fires if the run
        # later goes stuck (edits then spins) — escalate on stall, not on clock.
        _grind_no_progress = (self._GRIND_NO_PROGRESS_TURNS <= 0
                              or (self.turn - self._last_mutation_turn) >= self._GRIND_NO_PROGRESS_TURNS)
        if (not self._grind_checked and self._success_check_cmd
                and (_grind_turn_hit or _grind_time_hit) and _grind_no_progress):
            self._grind_checked = True
            try:
                _g_rc, _g_out = _run_success_check(self._success_check_cmd, self.log)
                if _g_rc != 0:
                    _grind_msg = self._maybe_advisor_nudge(source="grind",
                                                      context=_g_out)
                    if _grind_msg:
                        self.conversation_history.append(
                            {"role": "user", "content": _grind_msg})
            except Exception as _grind_e:
                self.log.debug("grind escalation check skipped: %s", _grind_e)
        # Update read-only counter from the previous turn
        if self.turn > 1:
            if self._turn_had_action:
                self._read_only_turns = 0
            else:
                self._read_only_turns += 1
        self._turn_had_action = False  # reset for this turn
        self._turn_t0 = time.monotonic()
        self._turn_in_tokens = 0
        self._turn_out_tokens = 0
        self._harvested_this_turn = False   # a harvested summary rewrites the lead message: one expected miss

        # ── Memory pressure management (tiers 2 + 3) ──
        # Tier 2: force gc + glibc malloc_trim so pymalloc arenas freed during
        # the prior turn actually return to the OS. Fights heap fragmentation.
        # Tier 3: measure resulting RSS; exit cleanly if over hard limit BEFORE
        # the OOM killer destroys the whole tmux/systemd scope. The watermark
        # return value is currently unused (callers only react to the hard
        # exit path); wired as side-effect-only so the dead-locals guard
        # (cycle 0013) doesn't complain.
        _release_memory(self.log)
        _check_memory_watermark(self.log)

        # ── Edit deadline nudge ──
        # Reviewers don't edit — suppress the nudge for that role.
        if (self.turn == self._EDIT_DEADLINE_TURN and not self._has_edited
                and not self._edit_nudge_sent and _NUDGE_ENABLED
                and not self._is_reviewer_role
                and self._is_cicd_builder):  # only meaningful for CICD builder sessions
            self._edit_nudge_sent = True
            _edit_nudge = (
                f"[SYSTEM: You have spent {self.turn} turns without making a code change. "
                f"Create your worktree NOW and make your edit, or declare a null result. "
                f"Do not continue investigating — act immediately.]"
            )
            self.conversation_history.append({"role": "user", "content": _edit_nudge})
            self.log.warning("Edit deadline: %d turns with no file edit — nudging", self.turn)

        # ── Wind-down and overtime warnings ──
        remaining = _MAX_TURNS - self.turn
        self.wind_down_msg = None
        if 0 < remaining <= _WIND_DOWN_TURNS:
            self.wind_down_msg = (
                f"[SYSTEM: {remaining} turns remaining before overtime. "
                f"Begin wrapping up — save your progress (CONSOLIDATE), "
                f"commit your work (PERSIST), and stop. "
                f"Do not start new tasks.]"
            )
            self.log.info("Wind-down: %d turns remaining", remaining)
        elif remaining <= 0:
            overtime = -remaining
            self.wind_down_msg = (
                f"[SYSTEM: You are {overtime} turns past the turn limit. "
                f"Finish what you are doing immediately — CONSOLIDATE and PERSIST now. "
                f"Do not start anything new.]"
            )
            self.log.warning("Overtime: %d turns past limit (%d)", overtime, _MAX_TURNS)
            # Hard cap: never exceed 2x the turn limit regardless of state
            if overtime >= _MAX_TURNS:
                self.log.error("Hard overtime cap reached (%d turns) — force stopping", self.turn)
                return "done"
        # F9 — capability-refusal stop. N turns after a tool refused for an ungranted capability,
        # a run still issuing tool calls is not working: it is building a substitute for the
        # thing it was refused. Same mechanism as the deadline below — tool calls refused, ONE
        # more turn for the final result, a second such turn ends the run. Placed before the
        # deadline block so that when both apply the deadline message wins.
        if (self._refusal_first_turn is not None and self._REFUSAL_MAX_TURNS > 0 and not self._refusal_hard
                and self.turn - self._refusal_first_turn >= self._REFUSAL_MAX_TURNS):
            self._refusal_hard = True
            self.log.error("CAPABILITY REFUSAL STOP at turn %d: %s refused at turn %d and the run is still "
                      "issuing tool calls %d turn(s) later — tool calls refused, forcing the final result",
                      self.turn, self._refusal_tool, self._refusal_first_turn, self.turn - self._refusal_first_turn)
            _emit("on_notice", "warn", f"[Capability refusal stop: {self._refusal_tool} — final result required]")
            self.wind_down_msg = (
                f"[SYSTEM: CAPABILITY REFUSAL STOP. {self._refusal_tool} refused for an ungranted capability at "
                f"turn {self._refusal_first_turn} ({(_LAST_REFUSAL or {}).get('head', '')}) and {self._REFUSAL_MAX_TURNS} "
                f"turn(s) have passed. Tool calls are now REFUSED. Reply with your FINAL RESULT now: a result "
                f"block with \"status\": \"blocked\" naming the tool and its exit code, what was examined, "
                f"and what was not covered.]")
        if self._refusal_hard:
            self._refusal_hard_turns += 1
            if self._refusal_hard_turns > 2:
                self.log.error("REFUSAL STOP: no final result after %d post-stop turns — stopping",
                          self._refusal_hard_turns - 1)
                return "done"
        # F1 — the wall-clock twin of the ladder above. Rides the same message channel; when
        # both apply on one turn the deadline message wins (it is the one with a hard stop
        # behind it). At 100% the run gets ONE more model turn for its final result; a tool
        # call on that turn is refused (dispatcher below), a second such turn ends the run.
        if self._DEADLINE_S > 0:
            _dl_msg, _dl_hard = _deadline_step(time.monotonic() - self._deadline_t0, self._DEADLINE_S,
                                               self._deadline_fired, self._deadline_fracs)
            if _dl_hard and not self._deadline_hard:
                self._deadline_hard = True
                _set_exit(EXIT_DEADLINE, f"wall-clock deadline {int(self._DEADLINE_S)}s reached at turn {self.turn}")
                self.log.error("DEADLINE reached (%.0fs) at turn %d — tool calls refused, forcing final result",
                          self._DEADLINE_S, self.turn)
                _emit("on_notice", "warn", f"[Deadline {int(self._DEADLINE_S)}s reached — forcing the final result]")
            if self._deadline_hard:
                self._deadline_hard_turns += 1
                if self._deadline_hard_turns > 2:
                    self.log.error("DEADLINE: no final result after %d post-deadline turns — stopping",
                              self._deadline_hard_turns - 1)
                    return "done"
            if _dl_msg:
                self.wind_down_msg = _dl_msg
                self.log.warning("deadline: %s", _dl_msg[:90])
        # F4 — goal anchor: at each fraction (of the deadline, else of the turn budget) inject
        # the stated goal, the deliverables, and the mechanical "not there yet" list. Cheap:
        # one glob sweep, no model call. Rides as a system message on this turn.
        if (self._goal or self._deliverables) and self._anchor_fracs:
            _prog = ((time.monotonic() - self._deadline_t0) / self._DEADLINE_S) if self._DEADLINE_S > 0 \
                else (self.turn / float(_MAX_TURNS) if _MAX_TURNS > 0 else 0.0)
            _due = [f for f in sorted(self._anchor_fracs) if _prog >= f and f not in self._anchor_fired]
            if _due:
                for f in _due:
                    self._anchor_fired.add(f)
                self._missing = _missing_deliverables(self._deliverables)
                # USER ROLE, NOT SYSTEM — this message is appended MID-HISTORY, and many chat
                # templates accept system messages only in the LEADING position. Qwen3's
                # rejects a later one outright: "Jinja Exception: System message must be at the
                # beginning" (template line 110), returned as HTTP 500. Because the anchor fires
                # at a FRACTION OF THE DEADLINE, the failure lands mid-run, repeats on every
                # retry (the history stays malformed), and then vanishes — the server is
                # perfectly healthy before and after, so it reads as an intermittent backend
                # fault. Four runs were lost to it, and no probe that kept system messages
                # leading could reproduce it, which is most probes anyone would think to write.
                # The anchor is a reminder injected into the conversation, not a system-level
                # instruction, so `user` is both portable and the honest description of it.
                self.conversation_history.append({"role": "user", "content": "[GOAL ANCHOR] " +
                    _goal_anchor_message(self._goal, self._deliverables, self._missing, _due[-1])})
                # WARNING, not info: this fires rarely and matters when diagnosing a run, and
                # at info it is invisible in every captured log I have (harnesses commonly
                # capture warning and above). Establishing whether the anchor had fired in four
                # failed runs took an hour of log archaeology that ended in inference from the
                # config rather than observation, because the one line that would have said so
                # was below the capture threshold. The deadline ladder already reports at this
                # level for the same reason; an injection into the conversation should be as
                # visible as a notice about the clock.
                self.log.warning("goal anchor FIRED at %.0f%% (user role): %d deliverable(s), %d missing",
                            _due[-1] * 100, len(self._deliverables), len(self._missing))
                telemetry.record_patch_event("goal_anchor", kind="fired")

        # ── Mid-burst "btw" injection ──
        # Deliver any queued monitor / background input at this natural boundary
        # (a fresh turn is about to be built), framed so the model finishes its
        # current action first. Guarded by pairing: never inject a user turn
        # while a tool_call from the previous batch is still unanswered — that
        # would strand it (strict templates reject an unanswered tool_call
        # followed by a user turn). If a tool_call is pending, the queued item
        # waits for the next boundary. The turn caps above still bound the loop,
        # so a chatty monitor cannot prevent exit.
        if monitor_bus.has_pending() and not _has_pending_tool_calls(self.conversation_history):
            if _drain_monitor_injections(self.conversation_history, framing=_BTW_FRAMING):
                self.log.debug("mid-burst monitor injection delivered at turn %d", self.turn)
                # A user message interrupted the output run — the next text the
                # agent streams starts a new run and re-earns its header.
                self._agent_header_owed = True

        # Harvest any completed async summary before building context
        if self._async_summarizer and self._async_summarizer.harvest(self.summary_state):
            self.log.info("Harvested async summary")
            self._harvested_this_turn = True
            _emit("on_summary_ready")

    def _request(self):
        # Build context window, with overflow reduction loop
        _ctx_max_messages = None  # None = use context_window.MAX_CONTEXT_MESSAGES
        _CTX_REDUCE_MAX = 10     # max number of message-reduction attempts
        _ctx_spill_passes = 0    # spill-to-file passes taken this turn (stage 1)
        _gateway_timeout_recovered = False
        _skill_suggestion = ""   # the picker's line for this turn's note, kept across attempts

        for _ctx_attempt in range(_CTX_REDUCE_MAX + 1):
            telemetry.record_context_size(self.ctx_size)
            _ready = self._async_summarizer.pending_up_to() if self._async_summarizer else None
            self.summary_state["_ready_up_to"] = _ready if isinstance(_ready, int) else None
            self.messages_to_send, oldest_idx = _build_context(
                self.conversation_history, self.summary_state, self.initial_files, self.ctx_size, self.max_tokens, self.log,
                max_messages_override=_ctx_max_messages)

            _collapsed_now = self.summary_state.pop("_collapse_summarize", False)
            _evicted_now = self.summary_state.pop("_evict_summarize", False)
            if _collapsed_now or _evicted_now:
                # The window head moved (collapse or eviction): the summary of what left
                # goes into this request, ready, waited for or skipped as summary.wait_s
                # says (_fold_summary_at_head_move). The rebuilt context keeps the new head.
                self._why = "cache collapse" if _collapsed_now else "eviction"
                if _summ.fold_summary_at_head_move(self.conversation_history, self.summary_state, oldest_idx, self.log, self._why,
                                              self._async_summarizer):
                    self.messages_to_send, oldest_idx = _build_context(
                        self.conversation_history, self.summary_state, self.initial_files, self.ctx_size, self.max_tokens, self.log,
                        max_messages_override=_ctx_max_messages)
                    self.summary_state.pop("_evict_summarize", None)   # the rebuild may move the head again

            # Summarize dropped messages: async (background) or sync (blocking).
            # T5.13 — gate the message-count trigger on actual context utilization
            # so summaries don't fire on half-empty windows (a field audit: 17%
            # of all tool calls were summary fires, many at ~25% context usage).
            # Both thresholds must be met: enough new messages AND enough
            # context pressure. Env-tunable for backend-specific calibration.
            if self._async_summarizer:
                unsummarized = oldest_idx - self.summary_state["up_to"]
                _summary_util_threshold = float(os.environ.get(
                    "AGENT_SUMMARY_UTIL_THRESHOLD", "0.6"))
                _ctx_tokens_used = sum(_cw.estimate_tokens(m) for m in self.messages_to_send)
                _ctx_utilization = ((_ctx_tokens_used * float(_cw.TOKEN_CAL.get("ratio", 1.0) or 1.0))
                                    / self.ctx_size if self.ctx_size else 0.0)
                # On a prompt-caching backend messages leave the window only by chunk
                # eviction (P4.2), which already means the window was full — and right
                # after one the window sits at ~20% of ctx, so a utilization gate would
                # block every summary (measured: 0 summaries in a 2.5 h session). The
                # message threshold is waived on the eviction turn for the same reason:
                # an eviction of 73 messages under a threshold of 75 kicked nothing and
                # the next 14 minutes ran without a summary (measured 2026-10-30).
                _msg_threshold_met = unsummarized >= _summ.SUMMARY_THRESHOLD or (
                    _prompt_caching_backend() and unsummarized > 0
                    and bool(self.summary_state.get("_head_moved")))
                _util_threshold_met = (_prompt_caching_backend()
                                       or _ctx_utilization >= _summary_util_threshold)
                if (
                    _msg_threshold_met
                    and _util_threshold_met
                    and not self._async_summarizer.is_running
                ):
                    new_messages = self.conversation_history[self.summary_state["up_to"]:oldest_idx]
                    if new_messages:
                        self._async_summarizer.kick(self.summary_state["text"], new_messages, oldest_idx)
                        self.log.info(
                            "Kicked async summary for %d messages (util=%.2f, threshold=%.2f)",
                            len(new_messages), _ctx_utilization, _summary_util_threshold,
                        )
                        telemetry.record_patch_event("summary_fired", kind="fired")
                        self._patch_telemetry["summary_fired"] += 1
                        _emit("on_notice", "info", "[background summarization started]")
                elif _msg_threshold_met and not _util_threshold_met:
                    # Would have fired pre-T5.13; gating skipped it. Telemetry-
                    # only — no log noise, since this is the COMMON case now.
                    telemetry.record_patch_event("summary_gated", kind="skipped")
                    self._patch_telemetry["summary_gated"] += 1
                _ahead_edge = self.summary_state.pop("_ahead_edge", None)
                if (isinstance(_ahead_edge, int) and _ahead_edge > self.summary_state["up_to"]
                        and not self._async_summarizer.is_running and self._async_summarizer.pending_up_to() is None):
                    # summary.ahead: the window is nearly full and these messages are certain
                    # to leave at the next eviction; their summary is held until then.
                    _ahead_msgs = self.conversation_history[self.summary_state["up_to"]:_ahead_edge]
                    if _ahead_msgs and self._async_summarizer.kick(self.summary_state["text"], _ahead_msgs, _ahead_edge,
                                                              kind="ahead"):
                        self.log.info("summary.ahead kick up_to=%d edge=%d (%d messages, window ~%d tokens)",
                                 self.summary_state["up_to"], _ahead_edge, len(_ahead_msgs), _ctx_tokens_used)
                        telemetry.record_patch_event("summary_ahead", kind="fired")
                        _emit("on_notice", "info",
                              f"[summarizing {len(_ahead_msgs)} messages ahead of the next eviction]")
            elif _summ.maybe_resummarize(self.conversation_history, self.summary_state, oldest_idx, self.log):
                self.messages_to_send, oldest_idx = _build_context(
                    self.conversation_history, self.summary_state, self.initial_files, self.ctx_size, self.max_tokens, self.log,
                    max_messages_override=_ctx_max_messages)
            self.summary_state.pop("_ahead_edge", None)
            self.summary_state.pop("_ready_up_to", None)

            # Inject wind-down as a system message at the end of context
            if self.wind_down_msg:
                self.messages_to_send.append({"role": "user", "content": self.wind_down_msg})

            self.log.info("--- Turn %d/%d | sending %d messages (history has %d total)",
                     self.turn, _MAX_TURNS, len(self.messages_to_send), len(self.conversation_history))

            # Per-turn time-of-now injection. Prepended as a system message to
            # the OUTGOING REQUEST only (not the persistent messages_to_send
            # buffer, so overflow-recovery's message-count logic sees the same
            # count as before this patch). Fixes one agent's hallucinated
            # "2025-05-22T10:00:00Z" anchors — the framework owns the clock,
            # the model owns the work.
            _now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            _apply_tool_loads(self._session_tools, self.log)   # loads asked for last turn join the list here
            # A retried attempt keeps the suggestion the first one harvested.
            _skill_suggestion = _skills_at_boundary(self.conversation_history, self.messages_to_send, oldest_idx, self.log) or _skill_suggestion
            # The per-turn lines, their order and the static/dynamic split are
            # system_prompt.py's; the side effects of consuming a restart record and a
            # repeat-read nudge stay here.
            _rs = None
            if _RESTART["resumed"]:
                _rs, _RESTART["resumed"] = _RESTART["resumed"], None
            _catalog_on = _TOOLS_CFG.get("catalog", True)
            _system_lines = _sp.per_turn_lines(
                _now_iso, os.getcwd(),
                model_guards=_config["context"].get("model_guards", True),
                catalog_line=_registry.catalog_line() if _catalog_on else None,
                skill_catalog=_skills.catalog_line() if _catalog_on else None,
                skill_suggestion=_skill_suggestion,
                restart=_rs,
                phase_line=self._phase_engine.checkpoint_line() if self._phase_engine is not None else None,
                cicd_line=_cicd.checkpoint_text() if _prompt_caching_backend() else None,
                tool_hints=bool(_config.get("preferences", {}).get("tool_selection_hints")),
                read_only_turns=self._read_only_turns, read_only_threshold=self._READ_ONLY_NUDGE_THRESHOLD,
                repeat_msg=self._repeat_msg)
            if self._repeat_msg:
                # F7: the SAME read repeated, set by the tool dispatcher; delivered once.
                self.log.warning("repeat-read stall: %s", self._repeat_msg[:100])
                telemetry.record_patch_event("repeat_read_nudge", kind="fired")
                self._repeat_msg = None
            _static_lines, _dynamic_lines = _sp.split(
                _system_lines, caching=_prompt_caching_backend(),
                user_prompt=_config.get("preferences", {}).get("system_prompt"))
            if _prompt_caching_backend():
                # P4.1: the note is STORED in history (once per turn), so the next request
                # extends this one exactly and the server's last-message cache point hits.
                # Wind-down rides in the same note rather than as an unstored message.
                if self.wind_down_msg and self.messages_to_send and self.messages_to_send[-1].get("content") == self.wind_down_msg:
                    self.messages_to_send.pop()
                    _dynamic_lines.append(self.wind_down_msg)
                _persist_runtime_note(self.conversation_history, self.messages_to_send, _dynamic_lines, self.turn)
                _dynamic_lines = []
            self._outgoing_messages = _sp.outgoing(_static_lines, self.messages_to_send, _dynamic_lines)

            # Unlock end_cycle after the first nudge so the agent can exit cleanly
            # without calling it before doing any real work.
            if self._total_nudges >= 1 and not self._end_cycle_unlocked:
                self._end_cycle_unlocked = True
                if not any((t.get("function") or {}).get("name") == "end_cycle" for t in self._session_tools):
                    self._session_tools.append(_end_cycle_tool.definition)
                self.log.info("end_cycle tool unlocked (nudges=%d)", self._total_nudges)
                telemetry.record_patch_event("end_cycle_unlocked", kind="fired")

            # Call the model (streaming).
            # Plan task 1.5: model comes from the main backend.
            # After a gateway timeout recovery, cap max_tokens to force shorter
            # individual responses so each write fits within the server timeout.
            _effective_max_tokens = self.max_tokens
            if self._gateway_timeout_recovery_count > 0:
                _effective_max_tokens = min(self.max_tokens, 1024)

            # Some gateways silently drop role:"system" — the preamble bundle,
            # goal-stack hydration and the per-turn steering block above would
            # never reach the model. Fold them into the adjacent user message on
            # those backends. No-op where system is honoured (local llama.cpp).
            try:
                from llm_backend import maybe_fold_system
                self._outgoing_messages = maybe_fold_system(
                    self._outgoing_messages, _main_backend, self.log)
            except Exception as _fe:
                self.log.debug("system-fold skipped: %s", _fe)

            self.request_body = {
                "model": _main_backend.model or _config["llm"]["model"],
                "messages": self._outgoing_messages,
                "temperature": self.temperature,
                "top_p": self.top_p,
                "top_k": self.top_k,
                "presence_penalty": self.presence_penalty,
                "max_tokens": _effective_max_tokens,
                "chat_template_kwargs": {
                    "enable_thinking": _config["generation"].get("enable_thinking", False)},
                **_cfg.generation_request_extras(),   # F8a: seed when configured
                "cache_prompt": True,
                "tools": self._session_tools,
                "tool_choice": "auto",
                "stream": True,
                # OpenAI streaming protocol (which llama.cpp implements) only
                # emits a final usage chunk when `stream_options.include_usage`
                # is set. Without this flag, the llamacpp/gemma path produces
                # no token telemetry. The bedrock and aicommons backends build
                # usage themselves and drop this key, so it is safe to include
                # unconditionally.
                "stream_options": {"include_usage": True},
            }
            # Fingerprint what is sent, so a miss can be told apart from a prefix we changed.
            self._cur_req_sig = (_request_signature(self._outgoing_messages, self._session_tools)
                            if _prompt_caching_backend() else None)

            if (
                getattr(_main_backend, "kind", None) == "bedrock"
                and _config.get("bedrock", {}).get("adaptive_max_tokens", True)
            ):
                _complexity = _classify_turn_complexity(self.messages_to_send)
                self.request_body["max_tokens"] = _get_adaptive_max_tokens(_main_backend.model, _complexity)

            # Issue #1007 Bug 2: llama.cpp degeneration loops eat the streaming
            # deadline when repetition is unpenalised. The OpenAI-compatible
            # endpoint llama.cpp exposes accepts these extension keys; Bedrock
            # would 400 on them, so gate on backend kind.
            if getattr(_main_backend, "kind", None) == "llamacpp":
                _llama_gen = _config.get("llamacpp", {}).get("generation", {})
                self.request_body.setdefault("repeat_penalty", _llama_gen.get("repeat_penalty", 1.1))
                self.request_body.setdefault("repeat_last_n", _llama_gen.get("repeat_last_n", 256))

            # T5.15 — apply stall-retry sampling override (set when a prior
            # turn stalled). The override is per-cycle, escalates on each
            # retry, and clears when a turn produces deltas successfully.
            if self._stall_sampling_override:
                _orig_temp = self.request_body.get("temperature")
                _orig_rp = self.request_body.get("repeat_penalty")
                self.request_body.update(self._stall_sampling_override)
                self.log.warning(
                    "Stall-retry sampling active: temp %s→%s, repeat_penalty %s→%s "
                    "(retries_remaining=%d, escalation_level=%d)",
                    _orig_temp, self.request_body.get("temperature"),
                    _orig_rp, self.request_body.get("repeat_penalty"),
                    self._stall_retries_remaining,
                    self._stall_sampling_override.get("_escalation_level", 1),
                )
                # Strip internal-tracking keys before sending
                self.request_body.pop("_escalation_level", None)

            if _prompt_caching_backend():
                # What the keepalive re-sends while the agent waits for input: a snapshot,
                # since the history's messages may be compressed in place later.
                try:
                    _LAST_REQUEST["body"] = json.dumps(self.request_body, default=str)
                except (TypeError, ValueError):
                    _LAST_REQUEST["body"] = None
            try:
                self.response = _llm_request(self.log, json=self.request_body, stream=True, timeout=(30, 300))
                # Only the legacy Response shape exposes ``status_code``;
                # a generator (Bedrock backend) doesn't. Skip the log line in
                # that case — the backend emits its own latency/ok telemetry.
                if hasattr(self.response, "status_code"):
                    self.log.info("Response status: %d", self.response.status_code)
                break  # success — exit the reduction loop
            except ContextOverflowError:
                if _ctx_attempt >= _CTX_REDUCE_MAX:
                    self.log.error("Context overflow: still failing after %d reductions", _CTX_REDUCE_MAX)
                    _emit("on_error", "Error: context overflow — could not fit in server context window")
                    _note_error_kind("context")
                    return "error"
                # STAGE 1 (root cause of the overflow described above): before dropping any message,
                # SPILL oversized tool-result payloads to files and retry — trimming whole
                # messages loses recent work when the real bloat is raw data echoed into
                # tool contents. Threshold halves on each successive spill pass (down to a
                # floor) so repeated overflows bite progressively smaller payloads. Only
                # when a pass finds NOTHING left to spill does stage 2 (trimming) run.
                _spill_thr = max(_SPILL_MIN_THRESHOLD, _SPILL_THRESHOLD >> _ctx_spill_passes)
                _n_spilled = _spill_oversized_tool_results(self.conversation_history, self.log, _spill_thr)
                if _n_spilled:
                    _ctx_spill_passes += 1
                    self.log.warning(
                        "Context overflow (attempt %d/%d): spilled %d bulk tool result(s) "
                        "to %s at threshold %d chars — retrying WITHOUT dropping messages",
                        _ctx_attempt + 1, _CTX_REDUCE_MAX, _n_spilled, _SPILL_DIR, _spill_thr)
                    _emit("on_notice", "info",
                          f"[Context overflow — moved {_n_spilled} bulk tool output(s) to "
                          f"{_SPILL_DIR}/ and left references; retrying]")
                    continue
                current_count = len(self.messages_to_send)
                if current_count <= 2:
                    # Already at minimum messages — aggressively truncate the summary
                    # instead of trying to reduce messages further
                    if self.summary_state["text"]:
                        old_len = len(self.summary_state["text"])
                        self.summary_state["text"] = self.summary_state["text"][:old_len // 2]
                        self.log.warning("Context overflow (attempt %d/%d): at min messages, "
                                    "truncating summary from %d to %d chars",
                                    _ctx_attempt + 1, _CTX_REDUCE_MAX, old_len, len(self.summary_state["text"]))
                        _emit("on_notice", "info", "[Context overflow — truncating summary to fit]")
                    else:
                        self.log.error("Context overflow with no summary and minimal messages — cannot reduce further")
                        _emit("on_error", "Error: context overflow — cannot reduce further")
                        _note_error_kind("context")
                        return "error"
                else:
                    # Reduce: cap messages to current count minus 2 (drop oldest pair)
                    _ctx_max_messages = max(2, current_count - 2)
                    self.log.warning("Context overflow (attempt %d/%d): reducing from %d to max %d messages",
                                _ctx_attempt + 1, _CTX_REDUCE_MAX, current_count, _ctx_max_messages)
                    _emit("on_context_recovery")
                    # The dropped messages are summarized (now, or by the worker: summary.wait_s)
                    _summ.fold_summary_at_head_move(self.conversation_history, self.summary_state, oldest_idx, self.log,
                                               "context overflow", self._async_summarizer)
                continue
            except (BedrockBudgetExceeded, ConfigError) as e:
                # Not retryable within this run (failover already declined or failed):
                # a spent quota or a rejected key. End as a backend error so --job
                # still writes its result file and AGENT-EXIT line (exit 15).
                telemetry.record_error(kind=type(e).__name__)
                if isinstance(e, BedrockBudgetExceeded):
                    # The message is already the notice (llm_backend.allowance_notice):
                    # one readable line for the person, the same line in the log.
                    self.log.error("Stopped: %s", e)
                    _emit("on_error", f"Stopped: {e}")
                else:
                    self.log.error("Backend refused the request: %s: %s", type(e).__name__, e)
                    _emit("on_error", f"Error calling server: {e}")
                _note_error_kind("backend")
                return "error"
            except requests.exceptions.RequestException as e:
                err_str = str(e)
                if "504" in err_str and self._gateway_timeout_recovery_count < self._GATEWAY_TIMEOUT_RECOVERY_MAX:
                    self._gateway_timeout_recovery_count += 1
                    self.log.warning(
                        "Gateway timeout (504) after all retries — injecting size-reduction hint "
                        "(recovery %d/%d)",
                        self._gateway_timeout_recovery_count, self._GATEWAY_TIMEOUT_RECOVERY_MAX,
                    )
                    _emit("on_notice", "warn",
                          f"[Gateway timeout — response may be too large; retrying with size-reduction hint "
                          f"({self._gateway_timeout_recovery_count}/{self._GATEWAY_TIMEOUT_RECOVERY_MAX})]")
                    self.conversation_history.append({
                        "role": "user",
                        "content": (
                            "[SYSTEM: The previous response timed out at the gateway — likely because "
                            "the response was too large to generate within the server time limit. "
                            "Please break your work into smaller pieces: write shorter individual "
                            "outputs, one file or section at a time, rather than generating "
                            "everything in a single large response. "
                            "Do NOT re-add tasks you have already added — check task_tracker(action='list') "
                            "first and continue from where you left off.]"
                        ),
                    })
                    _gateway_timeout_recovered = True
                    break
                self.log.error("Request failed after retries: %s", e)
                telemetry.record_error(kind=type(e).__name__)
                _dumped = _dump_failed_request(self.conversation_history, e)
                if _dumped:
                    self.log.error("request shape that failed -> %s", _dumped)
                    _emit("on_notice", "warn",
                          f"[Backend failed after retries; wrote the failing request's shape to "
                          f"{_dumped} — role order, sizes and heads, no payload]")
                _emit("on_error", f"Error calling server: {e}")
                _note_error_kind("backend")
                return "error"

        if _gateway_timeout_recovered:
            return _NEXT  # restart this turn with size-reduction hint in history

    def _stream(self):
        # Accumulate streamed response
        self.content_parts = []
        self.tool_calls_by_index = {}
        printed_header = False
        self.receiving_tools = False
        _stream_t0 = time.monotonic()
        _stream_deadline = _stream_t0 + 600  # 10 minute wall-clock cap
        # Per-turn text cap: if a text-only response (no tool calls) exceeds this
        # many characters, truncate and inject a correction.  Prevents a single
        # runaway turn from filling or overflowing the context window (field observation:
        # ~50K chars of stream-of-consciousness exhausted the 65K ctx and caused
        # 10 consecutive context-overflow 500 errors).
        # Configurable via preferences.max_text_response_chars; default 6000 (~1500 tokens).
        _MAX_TEXT_RESPONSE_CHARS = int(
            _config.get("preferences", {}).get("max_text_response_chars", 6000)
        )
        _content_chars = 0
        _post_tool_chars = 0
        _MAX_POST_TOOL_TEXT_CHARS = int(
            _config.get("preferences", {}).get("max_post_tool_text_chars", 2000)
        )
        self._text_cap_exceeded = False
        self._text_cap_limit_fired = _MAX_TEXT_RESPONSE_CHARS  # updated when post-tool cap fires
        self._finish_reason = None      # the stream's own finish_reason, when it sends one
        self._wall_cut = False          # the 600 s wall broke the stream off mid-response
        self._turn_thinking = None      # Anthropic thinking blocks to send back with this turn
        self._turn_reasoning = []       # streamed thinking text: kept off screen, logged at DEBUG (H5)
        # Stall detection: per a field audit, long latencies with zero
        # deltas (e.g. session_20260513_010952.log Turn 14: latency_ms=78377
        # deltas=0) are strongly correlated with corrupted/garbled output.
        # If the model stays silent past _STALL_TIMEOUT_S, abort the request
        # rather than waiting for the inevitable malformed completion.
        # Env-tunable; default 60s gives slow CPU backends headroom while
        # cutting failure tails by ~10x. Set 0 to disable.
        # PREFILL-AWARE (measured on a live run): a server streams NOTHING until it has processed
        # the whole prompt, and a 39k-token prompt took ~60 s of prefill on a fast GPU — so a
        # flat 60 s zero-delta guard cancelled every request at the instant the model was about
        # to speak, and "continue" re-sent the same prompt to the same fate. The budget must at
        # least cover the prefill a prompt of THIS size needs at a floor rate the backend is
        # known to beat (AGENT_PREFILL_TPS_FLOOR, default 500 t/s). Base still 0 = disabled.
        _STALL_BASE_S = float(os.environ.get("AGENT_STALL_TIMEOUT_S", "60"))
        # Over the window actually sent, not the stored history (which is unbounded on a
        # caching backend: measured 264k estimated vs 95k real at turn 300).
        _est_prompt_tokens = ((_cw.TOOLS_TOKENS or 0) + sum(_cw.estimate_tokens(m) for m in self.messages_to_send)) \
            * float(_cw.TOKEN_CAL.get("ratio", 1.0) or 1.0)
        _STALL_TIMEOUT_S = _stall_budget_s(_STALL_BASE_S, _est_prompt_tokens, _PREFILL_TPS_FLOOR)
        if _STALL_TIMEOUT_S > _STALL_BASE_S:
            self.log.info("stall budget widened %.0fs -> %.0fs for ~%d prompt tokens (prefill floor %.0f t/s)",
                     _STALL_BASE_S, _STALL_TIMEOUT_S, _est_prompt_tokens, _PREFILL_TPS_FLOOR)
        self._deltas_received = 0
        _stall_detected = False
        self.status = StreamStatus(emit=_emit)
        _act.request_started(getattr(_main_backend, "model", None) or _config["llm"].get("model"))
        # Sky-blue header — the aurora family's complement to the violet
        # "You:" prompt (NO_COLOR-safe; the PT spinner label strips ANSI).
        # DEFERRED to the first text token: a tool-only response shows only
        # its tool status, never a bare label; once a run has its header,
        # later iterations skip it (pt_header=False) until a queued user
        # message resets the run (field report 2026-08-14).
        if self._agent_header_owed:
            self.status.start("\n" + theme.c(theme.SKY, "Agent:", bold=True) + " ",
                         defer_header=True)
        else:
            self.status.start("\nAgent: ", pt_header=False)
        self.renderer = _ReasoningRenderer(lambda t: _emit("on_stream_chunk", t))

        # TESTING NOTES: mock _llm_request to return one of:
        #   (a) Response-style (legacy): `resp.iter_lines.return_value = [f"data: {json.dumps(body)}".encode(), b"data: [DONE]"]`
        #   (b) Iterator-of-dicts (new): any iterable yielding OpenAI delta dicts directly
        # `_iter_stream_chunks` below accepts either shape. LlamacppBackend returns
        # shape (a) — a live requests.Response. BedrockBackend yields (b) — a generator
        # of pre-parsed delta dicts. Mocks can use either form.
        #   tc = {"index": 0, "id": "t1", "type": "function",
        #         "function": {"name": tool_name, "arguments": json.dumps(args_dict)}}
        #   body = {"choices": [{"delta": {"tool_calls": [tc]}}]}
        #   summary_state must be initialized as {"text": "", "up_to": 0}
        try:
            with cancellable():
                for chunk in _iter_stream_chunks(self.response):
                    check_cancelled()
                    if time.monotonic() > _stream_deadline:
                        self.log.warning("Streaming wall-clock deadline exceeded (600s) — aborting response")
                        _safe_close(self.response)
                        self._wall_cut = True
                        break
                    # Stall guard: abort fast if no deltas arrive within the
                    # configured timeout. Cuts the 78-second-to-corrupted-
                    # output failure mode (a field audit) by ~10x.
                    if (
                        _STALL_TIMEOUT_S > 0
                        and self._deltas_received == 0
                        and (time.monotonic() - _stream_t0) > _STALL_TIMEOUT_S
                    ):
                        self.log.warning(
                            "Model stall: 0 deltas after %.1fs — aborting "
                            "(AGENT_STALL_TIMEOUT_S=%.1f). High-latency / "
                            "zero-deltas streams are strongly correlated with "
                            "corrupted output; failing fast lets the turn retry "
                            "cleanly with a fresh request.",
                            time.monotonic() - _stream_t0, _STALL_TIMEOUT_S,
                        )
                        telemetry.record_error(kind="model_stall")
                        telemetry.record_patch_event("stall_abort", kind="fired")
                        self._patch_telemetry["stall_abort"] += 1
                        # T5.15 — set stricter sampling for the NEXT request.
                        # Escalation: each retry deeper. Level 1: temp -0.3,
                        # rp +0.05. Level 2+: additional temp -0.2, rp +0.05.
                        if self._stall_retries_remaining > 0:
                            self._stall_retries_remaining -= 1
                            _prior_level = (self._stall_sampling_override or {}).get("_escalation_level", 0)
                            _new_level = _prior_level + 1
                            if _new_level == 1:
                                self._new_temp = max(0.1, (self.request_body.get("temperature") or 0.6) - 0.3)
                                self._new_rp = max(1.15, (self.request_body.get("repeat_penalty") or 1.1) + 0.05)
                            else:
                                # Build on prior override, escalate further
                                self._new_temp = max(0.1,
                                    (self._stall_sampling_override.get("temperature") or 0.6) - 0.2)
                                self._new_rp = max(1.15,
                                    (self._stall_sampling_override.get("repeat_penalty") or 1.1) + 0.05)
                            self._stall_sampling_override = {
                                "temperature": self._new_temp,
                                "repeat_penalty": self._new_rp,
                                "_escalation_level": _new_level,
                            }
                            self.log.warning(
                                "Stall-retry queued (level=%d, remaining=%d): "
                                "next turn temp=%.2f, repeat_penalty=%.2f",
                                _new_level, self._stall_retries_remaining,
                                self._new_temp, self._new_rp,
                            )
                        else:
                            self.log.warning(
                                "Stall-retry budget exhausted (budget was %d); "
                                "next turn uses default sampling — investigate via telemetry "
                                "if stalls persist (likely backend / model-config issue, "
                                "not a sampling pathology).",
                                self._stall_retries_budget,
                            )
                        _emit("on_notice", "warn",
                              f"[model stalled — aborted after {_STALL_TIMEOUT_S:.0f}s of zero deltas]")
                        _safe_close(self.response)
                        _stall_detected = True
                        break

                    # Capture per-chunk token usage. For the llamacpp path,
                    # the final OpenAI streaming chunk carries `usage` ONLY
                    # when the request body sets
                    # `stream_options: {include_usage: true}` (see request_body
                    # construction above). The bedrock and aicommons backends
                    # emit the same shape themselves; aicommons adds the cache
                    # fields (cache_read/creation_input_tokens) and its
                    # prompt_tokens counts all three input buckets.
                    # Record before the no-choices skip below so we don't drop it.
                    _usage = chunk.get("usage") if isinstance(chunk, dict) else None
                    if _usage:
                        _u_model = self.request_body.get("model") or ""
                        _u_in = _usage.get("prompt_tokens") or _usage.get("input_tokens") or 0
                        _u_out = _usage.get("completion_tokens") or _usage.get("output_tokens") or 0
                        _u_backend = getattr(_main_backend, "kind", None)
                        if _u_in:
                            telemetry.record_tokens(_u_model, "prompt", int(_u_in), backend=_u_backend)
                            self._turn_in_tokens += int(_u_in)
                            # F3b: what we ESTIMATED for this exact request vs what the server counted
                            try:
                                _est_sent = (_cw.TOOLS_TOKENS or 0) + sum(_cw.estimate_tokens(_m) for _m in self._outgoing_messages)
                                _cw.update_token_calibration(_est_sent, int(_u_in), self.log)
                            except Exception as _cal_e:  # never let accounting break a turn
                                self.log.debug("token calibration skipped: %s", _cal_e)
                        if _u_out:
                            telemetry.record_tokens(_u_model, "completion", int(_u_out), backend=_u_backend)
                            self._turn_out_tokens += int(_u_out)
                        for _ck, _kind in (("cache_read_input_tokens", "cache_read"),
                                           ("cache_creation_input_tokens", "cache_write")):
                            if _usage.get(_ck):   # E5: hit rate chartable from the token counter
                                telemetry.record_tokens(_u_model, _kind, int(_usage[_ck]), backend=_u_backend)
                        if _prompt_caching_backend():
                            # Every miss must have a known cause. Measured before this line
                            # existed: 225 of 310 requests missed for 2.5 hours (18M tokens
                            # re-written) and nothing said why. Flags are cleared on every
                            # request so a stale one cannot explain a later miss.
                            _c_read = int(_usage.get("cache_read_input_tokens") or 0)
                            _c_write = int(_usage.get("cache_creation_input_tokens") or 0)
                            _c_unc = int(_usage.get("input_tokens") or 0)
                            _head_moved = bool(self.summary_state.pop("_head_moved", False))
                            _collapsed = bool(self.summary_state.pop("_collapsed", False))
                            _rewrote = bool(self.summary_state.pop("_rewrote_window", False))
                            _lead_placed = bool(_LEAD.pop("replaced", False))
                            self._harvested_this_turn = self._harvested_this_turn or bool(self.summary_state.pop("_lead_changed", False))
                            _c_total = _c_read + _c_write + _c_unc
                            _prev_req_sig = _CACHE_REQ["prev_sig"]
                            _prefix_same = _prefix_unchanged(_prev_req_sig, self._cur_req_sig)
                            _gap_s = ((time.time() - _CACHE_REQ["last_at"])
                                      if _CACHE_REQ["last_at"] > 0 else None)
                            # `idle` is judged against what the lifetime record knows of
                            # this backend and model, not a fixed five minutes: a miss
                            # inside the proven lifetime is the provider's.
                            _alive_s, _ = _cache_lifetime_estimate(_cache_lifetimes(), _cache_lifetime_key())
                            _idle = _gap_s is not None and _gap_s > _alive_s
                            # A switched model (/model, switch_model) has its own cache: the
                            # same prefix misses once and teaches the new model's record nothing.
                            _switched = bool(_CACHE_REQ.get("prev_model")) and bool(_u_model) \
                                and _u_model != _CACHE_REQ.get("prev_model")
                            if _prefix_same and not _switched and _c_total and _gap_s is not None:
                                # An unchanged prefix after a known gap is evidence about
                                # the lifetime either way; the record decides what counts.
                                try:
                                    _cache_lifetime_record_request(_gap_s, _c_read * 2 >= _c_total, self.log)
                                except Exception as _lt_e:   # the record never breaks a turn
                                    self.log.debug("cache.lifetime record skipped: %s", _lt_e)
                            if _c_total and _c_read * 2 < _c_total:
                                # `idle`/`provider`: the previous request is resent byte for byte
                                # as the head of this one, so nothing on our side explains the
                                # miss: the cache expired (idle) or the server lost it. `unknown`
                                # means the prefix changed and no flag says why.
                                _reason = ("first" if _CACHE_REQ["n"] == 0 else
                                           "model-switch" if _switched else
                                           "collapse" if _collapsed else
                                           "eviction" if _head_moved else
                                           "summary-harvest" if self._harvested_this_turn else
                                           "lead-reread" if _lead_placed else
                                           "history-rewrite" if _rewrote else
                                           ("idle" if _idle else "provider") if _prefix_same
                                           else "unknown")
                                _changed = ""
                                if _reason in ("unknown", "history-rewrite"):
                                    # Name the message that broke the prefix: index, role and
                                    # the head of its current content.
                                    _where = _prefix_diff(_prev_req_sig, self._cur_req_sig)
                                    _changed = " changed=" + _where
                                    if _where.startswith("msg "):
                                        try:
                                            _cm = self._outgoing_messages[int(_where.split()[1].split("/")[0])]
                                            self._head = str(_cm.get("content") or _cm.get("tool_calls") or "")
                                            _changed += " role=%s head=%r" % (_cm.get("role"), self._head[:80])
                                        except Exception:
                                            pass
                                self.log.info("cache.miss turn=%d reason=%s read=%d write=%d uncached=%d%s",
                                         self.turn, _reason, _c_read, _c_write, _c_unc, _changed)
                            _CACHE_REQ["n"] += 1
                            _CACHE_REQ["prev_sig"] = self._cur_req_sig
                            _CACHE_REQ["last_at"] = time.time()
                            _CACHE_REQ["prev_model"] = _u_model
                    if isinstance(chunk, dict) and chunk.get(_THINKING_KEY):
                        self._turn_thinking = chunk[_THINKING_KEY]
                    choices = chunk.get("choices")
                    if not choices:
                        continue  # skip usage/stats chunks
                    if choices[0].get("finish_reason"):
                        self._finish_reason = choices[0]["finish_reason"]
                    delta = choices[0].get("delta", {}) or {}

                    # Thinking deltas (reasoning_content — e.g. Qwen3 on
                    # llama-server --reasoning, or any enable_thinking stream)
                    # are the model ACTIVELY generating, not a stall. Credit
                    # them as activity so the zero-deltas stall guard above does
                    # not abort a legitimate thinking phase before the first
                    # answer token arrives. (Measured 2026-08-18: with
                    # enable_thinking on, a >60s think emitted only
                    # reasoning_content, tripped the guard, and aborted the turn
                    # with no output.) Not rendered here — thinking stays out of
                    # the visible answer stream by default; it only counts.
                    if delta.get("reasoning_content"):
                        self._deltas_received += 1
                        self._turn_reasoning.append(delta["reasoning_content"])

                    if delta.get("content"):
                        if not printed_header:
                            self.status.first_token()   # prints the deferred header
                            printed_header = True
                            self._agent_header_owed = False
                        self.renderer.feed(delta["content"])
                        self.content_parts.append(delta["content"])
                        _content_chars += len(delta["content"])
                        if self.receiving_tools:
                            _post_tool_chars += len(delta["content"])
                        self.status.count_token()
                        self._deltas_received += 1
                        # Pre-tool text cap: if nudge is enabled and the model is
                        # generating pure prose (no tool calls yet) and exceeds the
                        # per-turn character limit, truncate to prevent context overflow.
                        if (_NUDGE_ENABLED
                                and not self.receiving_tools
                                and _content_chars > _MAX_TEXT_RESPONSE_CHARS):
                            self.log.warning(
                                "Text-only response cap: %d chars exceeds limit %d — truncating",
                                _content_chars, _MAX_TEXT_RESPONSE_CHARS,
                            )
                            self._text_cap_exceeded = True
                            telemetry.record_patch_event("text_cap", kind="fired",
                                                         value=_content_chars)
                            _safe_close(self.response)
                            break
                        # Post-tool text cap: when nudge is enabled, prose generated
                        # AFTER tool calls is also capped — prevents a field-observed run
                        # pattern where garbled tool calls set receiving_tools=True
                        # then 50K chars of spiral prose followed with no cap applied.
                        if (_NUDGE_ENABLED
                                and self.receiving_tools
                                and _post_tool_chars > _MAX_POST_TOOL_TEXT_CHARS):
                            self.log.warning(
                                "Post-tool text cap: %d chars after tools exceeds limit %d — truncating",
                                _post_tool_chars, _MAX_POST_TOOL_TEXT_CHARS,
                            )
                            self._text_cap_exceeded = True
                            self._text_cap_limit_fired = _MAX_POST_TOOL_TEXT_CHARS
                            telemetry.record_patch_event("text_cap", kind="fired_post_tool",
                                                         value=_post_tool_chars)
                            _safe_close(self.response)
                            break

                    if delta.get("tool_calls"):
                        self._deltas_received += 1
                        if not self.receiving_tools:
                            self.receiving_tools = True
                            if printed_header:
                                # Complete the streamed block BEFORE any status
                                # line: the renderer's tag-parsing holdback can
                                # still own the tail of the last sentence, and
                                # printing stats first split the block around
                                # them (field report 2026-08-14).
                                self.renderer.flush()
                                _emit("on_notice", "info", "")
                                self.status.finish()  # stop old spinner cleanly before reusing var
                                self.status = StreamStatus(emit=_emit)
                                self.status.start(f"{DIM}  preparing tool calls ")
                        for tc_delta in delta["tool_calls"]:
                            idx = tc_delta["index"]
                            if idx not in self.tool_calls_by_index:
                                self.tool_calls_by_index[idx] = {
                                    "id": tc_delta.get("id", ""),
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            tc = self.tool_calls_by_index[idx]
                            if tc_delta.get("id"):
                                tc["id"] = tc_delta["id"]
                            if tc_delta.get("function", {}).get("name"):
                                tc["function"]["name"] = tc_delta["function"]["name"]
                            if tc_delta.get("function", {}).get("arguments"):
                                tc["function"]["arguments"] += tc_delta["function"]["arguments"]
        except CancelledError:
            self.renderer.flush()
            self.status.finish()
            _safe_close(self.response)
            _emit("on_cancelled", "streaming")
            self.log.info(
                "cancel.latency_ms latency_ms=%d site=backend.stream_chat backend=%s",
                int((time.monotonic() - _stream_t0) * 1000),
                _main_backend.kind,
            )
            self.log.info("CANCELLED during streaming")
            # Keep partial history so caller can inject user guidance
            return "cancelled"
        except requests.exceptions.RequestException as e:
            self.renderer.flush()
            self.status.finish()
            _safe_close(self.response)
            self.log.error("Streaming connection lost: %s", e)
            telemetry.record_error(kind=type(e).__name__)
            _emit("on_error", f"Streaming error: {e}")
            # Treat as empty response — the text-only handler will nudge or stop
        except Exception as e:
            self.renderer.flush()
            self.status.finish()
            _safe_close(self.response)
            self.log.error("Unexpected error during streaming: %s", e, exc_info=True)
            telemetry.record_error(kind=type(e).__name__)
            _emit("on_error", f"Streaming error: {e}")
            # F6 (review finding 3): three consecutive streaming failures with nothing produced
            # is a backend failure, not a completed run — exit 15, never 0 "completed".
            self._stream_errors += 1
            if self._stream_errors >= 3:
                self.log.error("Streaming failed %d times in a row — ending the run as a backend error", self._stream_errors)
                _note_error_kind("backend")
                return "error"

    def _record_response(self):
        self.renderer.flush()
        if self.content_parts and not self.receiving_tools:
            _emit("on_notice", "info", "")
        self.status.finish()

        # T5.15 — if this turn produced deltas successfully (and a stall
        # override was active from a prior turn), clear the override so the
        # next turn returns to default sampling. A single stall in an
        # otherwise-healthy session shouldn't permanently degrade quality.
        if self._deltas_received > 0:
            self._stream_errors = 0   # a productive turn resets the consecutive streaming-failure count
        if self._deltas_received > 0 and self._stall_sampling_override is not None:
            self.log.info(
                "Stall-retry succeeded: turn produced %d deltas — clearing "
                "sampling override (was level=%d, temp=%.2f, rp=%.2f).",
                self._deltas_received,
                self._stall_sampling_override.get("_escalation_level", 0),
                self._stall_sampling_override.get("temperature", 0),
                self._stall_sampling_override.get("repeat_penalty", 0),
            )
            self._stall_sampling_override = None
            # Restore retry budget for any FUTURE stall in this cycle
            self._stall_retries_remaining = self._stall_retries_budget

        self.full_content = _THINK_TAG_RE.sub('', "".join(self.content_parts)).strip()
        _emit("on_assistant_text", self.full_content)
        _act.response_text(self.full_content)
        self.tool_calls = [self.tool_calls_by_index[i] for i in sorted(self.tool_calls_by_index)] if self.tool_calls_by_index else []

        # A response cut off by max_tokens or the wall leaves its last tool call with
        # half-written arguments. Executing it would act on a fragment, and storing it
        # would poison every later request (aicommons-integration.md §7 H4). Drop it
        # and tell the model why, so it re-issues the work in smaller pieces.
        _cut_tool_names = []
        if self.tool_calls and (self._finish_reason == "length" or self._wall_cut):
            _kept = [tc for tc in self.tool_calls if _tool_args_complete(tc)]
            _cut_tool_names = [tc["function"].get("name") or "?" for tc in self.tool_calls if tc not in _kept]
            if _cut_tool_names:
                self.log.warning("Response cut off (%s) mid tool call — dropping %d incomplete call(s): %s",
                            "wall" if self._wall_cut else "max_tokens", len(_cut_tool_names), _cut_tool_names)
                self.tool_calls = _kept

        # M3: a refusal (model or gateway guardrail; stop_reason "refusal") is not a
        # text-only turn to nudge — re-sending the same history gets the same answer.
        # Keep the partial text out of history and end the run as a backend error.
        if self._finish_reason == "content_filter":
            self.log.error("Model refused the request (stop_reason=refusal); partial text not stored: %r",
                      self.full_content[:300])
            telemetry.record_error(kind="refusal")
            _emit("on_error", "The model declined this request (refusal / guardrail). Ending the run.")
            _note_error_kind("refusal")
            return "error"

        assistant_msg = {"role": "assistant", "content": self.full_content}
        if self.tool_calls:
            assistant_msg["tool_calls"] = self.tool_calls
        if self._turn_thinking and self.tool_calls:
            # Bedrock needs this turn's signed thinking before its tool_use when the
            # turn is sent back; other backends ignore the key.
            assistant_msg[_THINKING_KEY] = self._turn_thinking
        self.conversation_history.append(assistant_msg)
        if _cut_tool_names:
            self.conversation_history.append({"role": "user", "content": (
                f"[SYSTEM: your response was cut off before the arguments of "
                f"{', '.join(_cut_tool_names)} were complete, so that call was NOT run. "
                f"Split the work: write a large file in several smaller calls (create it, "
                f"then append or edit in sections), each well under the output limit.]")})

        # Text-cap correction: inject a user message so the model understands
        # it was cut off and must use a tool next.  This fires AFTER the
        # assistant message is appended so the correction is a separate user
        # turn (not a modification of the truncated content).
        if self._text_cap_exceeded:
            _emit("on_notice", "warn",
                  f"[text cap: response truncated at {self._text_cap_limit_fired} chars — injecting tool nudge]")
            _cap_correction = (
                f"[SYSTEM: your response was cut off at {self._text_cap_limit_fired} characters because it exceeded "
                f"the per-turn text limit. Text walls fill the context window and crash the session "
                f"(this happened in C206/C207: prose spirals caused 10 context-overflow errors). "
                f"Analysis belongs in short decisions, not prose. "
                f"Call a tool now — if marking a phase done: "
                f"task_tracker(action='done', description='PERCEIVE') or task_tracker(action='done', task_id=1). "
                f"If reading a file: read_file(path='...')]"
            )
            self.conversation_history.append({"role": "user", "content": _cap_correction})
            self._consecutive_text_only = 0  # reset so the model gets a full nudge budget

        if self.full_content:
            self.log.debug("ASSISTANT: %s", self.full_content)
        if self._turn_reasoning:
            self.log.debug("THINKING (%d chars): %s", sum(map(len, self._turn_reasoning)), "".join(self._turn_reasoning)[:4000])

        # Verbose-only per-turn telemetry — fires once per turn iteration after
        # the assistant response is processed, BEFORE any early-return branches
        # (completion-signal stop, tool loops, etc.). Gated at the call site so
        # disabled mode pays only one bool check; SDK PeriodicExportingMetricReader
        # handles flushing — no force-flush per turn (issue #401).
        if telemetry.verbose_enabled():
            telemetry.record_turn(
                role="main",
                duration_s=time.monotonic() - self._turn_t0,
                tool_calls=len(self.tool_calls),
                in_tokens=self._turn_in_tokens,
                out_tokens=self._turn_out_tokens,
                model=(_main_backend.model or _config["llm"]["model"]),
            )
        # Detect degenerate text loops (model repeating itself)
        if self.full_content:
            _text_hash = hashlib.md5(self.full_content.encode()).hexdigest()
            self._recent_text_hashes.append(_text_hash)
            # Keep only recent entries
            if len(self._recent_text_hashes) > 10:
                self._recent_text_hashes[:] = self._recent_text_hashes[-10:]
            _repeat_count = sum(1 for h in self._recent_text_hashes if h == _text_hash)
            if _repeat_count >= self._TEXT_LOOP_THRESHOLD:
                self.log.warning("Text loop detected: same output %d times — stopping",
                            _repeat_count)
                _emit("on_text_loop_detected", _repeat_count)
                return "done"

        # Hard cap on post-persist drift.  After cycle persisted (git push),
        # the agent must wrap TRACK within _CYCLE_HARD_STOP_TURNS.  Longer
        # runs mean it started a second PERCEIVE — ignore and stop.
        if self._cycle_persisted:
            _grace_used = self.turn - (self._cycle_persisted_turn or self.turn)
            if _grace_used >= self._CYCLE_HARD_STOP_TURNS:
                self.log.info("Stopping: cycle persisted %d turns ago, hard cap reached", _grace_used)
                return "done"

    def _text_only(self):
        if not self.tool_calls:
            # Background-monitor injection (turn-complete boundary): if a
            # monitor queued output while the model was working, feed it as a
            # new user turn and keep going instead of stopping. Placed at the
            # top of the no-tool-call branch so genuine new input preempts the
            # "you stopped without a tool" nudges/gates — this is legitimate
            # continuation, not a stalled completion. Empty queue -> fall
            # through to the existing stop logic, so -a still exits when the
            # cycle truly ends. The loop-top hard caps (turn-limit/overtime)
            # still bound the loop, so even a chatty monitor cannot prevent
            # eventual exit. Last message here is the model's text response, so
            # appending a user turn yields the safe assistant->user->assistant
            # shape (never spliced between a tool call and its result).
            if _drain_monitor_injections(self.conversation_history):
                self._consecutive_text_only = 0
                self._recent_text_hashes.clear()
                self.log.debug("monitor injection consumed at turn-complete; continuing")
                return _NEXT

            # Q.01 guard: fires regardless of _NUDGE_ENABLED.  In no-nudge
            # mode the session stops immediately on any text-only response,
            # so this must run before that early exit.  Budget: 2 retries
            # via _consecutive_text_only (incremented here so the loop
            # terminates if the model keeps writing text walls).
            if self.full_content and _NUDGE_ENABLED:
                _q01_blocks = len(re.findall(r'```', self.full_content)) // 2
                if _q01_blocks >= 1 and self._consecutive_text_only < 2:
                    self._consecutive_text_only += 1
                    self.conversation_history.append({
                        "role": "user",
                        "content": (
                            "You wrote code blocks in text but did not call any "
                            "tools. You MUST execute each action by calling the "
                            "appropriate tool directly — exec_command for shell "
                            "commands, file(action='edit') for edits, "
                            "file(action='append') for JSONL appends. "
                            "Do not describe work in markdown — do the work with "
                            "tool calls now, one action at a time."
                        ),
                    })
                    self.log.info(
                        "Q.01 guard: plan-in-text (%d code blocks), "
                        "retry %d/2",
                        _q01_blocks, self._consecutive_text_only,
                    )
                    telemetry.record_patch_event("q01_plan_in_text", kind="fired")
                    return _NEXT

            if not _NUDGE_ENABLED:
                if (not self._has_committed
                        and _config.get("preferences", {}).get("persist_nudge")
                        and self._consecutive_text_only < 2):
                    _gs = subprocess.run(
                        ["git", "status", "--porcelain"],
                        capture_output=True, text=True, cwd=os.getcwd(),
                    )
                    if _gs.returncode == 0 and _gs.stdout.strip():
                        self._consecutive_text_only += 1
                        self.conversation_history.append({
                            "role": "user",
                            "content": (
                                "You have uncommitted changes but have not run PERSIST. "
                                "Complete the cycle: git add the changed files, "
                                "commit with the C{N} message format, and push."
                            ),
                        })
                        self.log.info("persist_nudge: dirty tree, no commit — nudging (%d/2)",
                                 self._consecutive_text_only)
                        telemetry.record_patch_event("persist_nudge", kind="fired")
                        return _NEXT
                if self._success_gate_blocks_exit("textonly_stop"):
                    return _NEXT
                if self._result_contract_blocks_exit("textonly_stop", self.full_content):
                    return _NEXT
                if self._deliverable_guard_blocks_exit("textonly_stop", self.full_content):
                    return _NEXT
                self._cvt_blk, self._claim_trace_blocks = self._claim_vs_trace_block(
                    self.full_content, self._ran_verification, self._has_committed,
                    self._claim_trace_blocks, self._CLAIM_TRACE_MAX_BLOCKS)
                if self._cvt_blk:
                    return _NEXT
                self.log.info("Stopping: text-only response (no tool calls)")
                if self._async_summarizer:
                    self._async_summarizer.harvest(self.summary_state)
                _save_checkpoint(self.conversation_history, self.summary_state, self.turn, self.initial_files,
                                 clean_exit=True)
                return "done"

            # Detect completion-intent responses FIRST — a clean stop phrase
            # must take priority over the mechanical grace-period cap.
            _completion_signals = (
                "cycle is complete", "cycle complete", "concluding this cycle",
                "closing this cycle", "no further actionable", "no remaining",
                "no improvements", "already met", "already resolved",
                "i have completed", "has been achieved", "goal of making",
                "work is done", "task is complete", "actions taken",
                "successfully created pull request", "created a pull request",
                "has been completed", "process is complete",
                "no more open pull requests", "no reviewable prs",
                "standing by", "all tasks", "queue: empty",
            )
            # Completion signals are trusted after a commit OR a successful
            # merge (reviewer role).  _cicd_phase_state["track"] is set when
            # `gh pr merge` exits 0.
            _has_persisted_work = (self._has_committed
                                    or _cicd.phase_state.get("track", False)
                                    or self._has_reviewer_persisted)
            # Cycle 39: regex patterns for natural paraphrases ("cycle 249 is complete")
            _completion_signal_patterns = (
                r"cycle\s+\S+\s+is\s+(now\s+)?complete",  # "cycle 249 is complete", "cycle N is now complete"
                r"cycle\s+is\s+now\s+complete",             # "cycle is now complete"
                r"improvement\s+cycle\s+\S*\s*is\s+complete",  # "improvement cycle is complete"
                r"cycle\s+\w+\s+complete",  # "cycle 543 complete" (without "is")
            )
            _fc_lower = self.full_content.lower() if self.full_content else ""
            _completion_matched = (
                (self.full_content and any(s in _fc_lower for s in _completion_signals))
                or (self.full_content and any(re.search(p, _fc_lower) for p in _completion_signal_patterns))
            )
            if _has_persisted_work and _completion_matched:
                if _NUDGE_ENABLED and not self._open_task_nudge_sent:
                    self._open_reminder = _build_open_task_nudge()
                    if self._open_reminder:
                        self._open_task_nudge_sent = True
                        self._total_nudges += 1
                        self.conversation_history.append({"role": "user", "content": self._open_reminder})
                        self.log.info("open-task nudge: completion signal intercepted, %d nudges used",
                                 self._total_nudges)
                        telemetry.record_patch_event("open_task_nudge", kind="fired")
                        return _NEXT
                # WS10.c SIBLING-PATH FIX (2026-07-12): the success-check
                # gate originally hooked ONLY the end_cycle tool — but
                # telemetry showed replay/creature runs terminate via THIS
                # text-only completion path and never call end_cycle, so the
                # gate was structurally inert in exactly the runs it was
                # built for (zero success_check_block AND zero end_cycle
                # patch events across all replay sessions). Guard on one
                # exit path, missing on the sibling — same defect class the
                # guards in _validate_tool_call had (WS8.1).
                if self._success_gate_blocks_exit("completion_signal"):
                    return _NEXT
                if self._result_contract_blocks_exit("completion_signal", self.full_content):
                    return _NEXT
                if self._deliverable_guard_blocks_exit("completion_signal", self.full_content):
                    return _NEXT
                self._cvt_blk, self._claim_trace_blocks = self._claim_vs_trace_block(
                    self.full_content, self._ran_verification, self._has_committed,
                    self._claim_trace_blocks, self._CLAIM_TRACE_MAX_BLOCKS)
                if self._cvt_blk:
                    return _NEXT
                self.log.info("Stopping: model signalled cycle completion (work persisted)")
                return "done"
            if not _has_persisted_work and _completion_matched:
                self.log.info("Ignoring completion signal — no persisted work yet, nudging to continue")

            # If the cycle already persisted (git push happened), allow a few
            # grace turns for TRACK work, then stop on text-only response.
            if self._cycle_persisted:
                grace_used = self.turn - (self._cycle_persisted_turn or self.turn)
                if grace_used >= self._CYCLE_GRACE_TURNS:
                    if _NUDGE_ENABLED and not self._open_task_nudge_sent:
                        self._open_reminder = _build_open_task_nudge()
                        if self._open_reminder:
                            self._open_task_nudge_sent = True
                            self._total_nudges += 1
                            self.conversation_history.append({"role": "user", "content": self._open_reminder})
                            self.log.info("open-task nudge: grace period intercepted, %d nudges used",
                                     self._total_nudges)
                            telemetry.record_patch_event("open_task_nudge", kind="fired")
                            return _NEXT
                    if self._claim_gate_blocks_textonly_exit():
                        return _NEXT
                    self.log.info("Stopping: cycle persisted %d turns ago, grace period exhausted", grace_used)
                    return "done"
                self.log.info("Cycle persisted but grace period active (%d/%d turns) — nudging for TRACK work",
                        grace_used, self._CYCLE_GRACE_TURNS)
            # Past turn limit + no tool use = end cycle immediately
            if self.turn > _MAX_TURNS:
                if self._claim_gate_blocks_textonly_exit():
                    return _NEXT
                self.log.warning("Overtime + text-only response — ending cycle")
                _emit("on_overtime", "text_only")
                return "done"

            self._consecutive_text_only += 1

            # Total nudge budget across the session.
            self._total_nudges += 1
            if self._total_nudges >= _MAX_TOTAL_NUDGES:
                if self._claim_gate_blocks_textonly_exit():
                    return _NEXT
                self.log.info("Stopping: total nudge budget exhausted (%d/%d)",
                         self._total_nudges, _MAX_TOTAL_NUDGES)
                return "done"

            if self._consecutive_text_only >= _MAX_TEXT_ONLY:
                if self._claim_gate_blocks_textonly_exit():
                    return _NEXT
                self.log.info("Stopping: %d consecutive text-only responses", self._consecutive_text_only)
                return "done"

            # First text-only response: strip it from context and retry silently.
            # Leaving hallucinated content in history poisons subsequent turns —
            # the model builds on its fabricated answer instead of using tools.
            if self._consecutive_text_only == 1:
                # Completion signal takes priority even on the first text-only.
                # When work has already been persisted (git push) and the model
                # says "cycle NNN complete" / "committed and pushed", stripping
                # and retrying causes the agent to start the NEXT cycle rather
                # than exit cleanly.  Check here before the unconditional strip.
                if _has_persisted_work and _completion_matched:
                    if _NUDGE_ENABLED and not self._open_task_nudge_sent:
                        self._open_reminder = _build_open_task_nudge()
                        if self._open_reminder:
                            self._open_task_nudge_sent = True
                            self._total_nudges += 1
                            self.conversation_history.append({"role": "user", "content": self._open_reminder})
                            self.log.info("open-task nudge (first text-only): completion signal, %d nudges used",
                                     self._total_nudges)
                            telemetry.record_patch_event("open_task_nudge", kind="fired")
                            return _NEXT
                    if self._success_gate_blocks_exit("first_textonly_completion"):
                        return _NEXT
                    if self._result_contract_blocks_exit("first_textonly_completion", self.full_content):
                        return _NEXT
                    if self._deliverable_guard_blocks_exit("first_textonly_completion", self.full_content):
                        return _NEXT
                    self._cvt_blk, self._claim_trace_blocks = self._claim_vs_trace_block(
                        self.full_content, self._ran_verification, self._has_committed,
                        self._claim_trace_blocks, self._CLAIM_TRACE_MAX_BLOCKS)
                    if self._cvt_blk:
                        return _NEXT
                    self.log.info("Stopping: first-text-only completion signal with persisted work")
                    telemetry.record_patch_event("completion_signal_first_textonly", kind="fired")
                    return "done"

                # Claim-vs-trace on the FIRST text-only response, BEFORE the strip
                # below silently discards it — the highest-leverage site. An
                # unrecognised-phrase completion ("All done. The test passes.")
                # skips the completion block above; without this check the strip
                # would erase the fabricated claim and retry, so the gate never
                # sees it. Catch it here at first occurrence and correct (don't
                # strip). Fires only on a real unsupported claim, so honest terse
                # and plan-in-text responses fall through to Q.01 + strip unchanged.
                if self._claim_gate_blocks_textonly_exit():
                    return _NEXT

                # Q.01 guard: if the response contains 2+ fenced code blocks, the
                # model wrote bash/python in text instead of calling tools ("plan-
                # in-text").  Stripping silently causes another identical wall on
                # retry; instead keep the message and inject a targeted correction.
                _q01_code_blocks = len(re.findall(r'```', self.full_content)) // 2
                if _q01_code_blocks >= 2:
                    self.nudge = (
                        "You wrote code blocks in text but did not call any tools. "
                        "You MUST execute each action by calling the appropriate "
                        "tool directly — exec_command for shell commands, "
                        "file(action='edit') for edits, file(action='append') for "
                        "JSONL appends. Do not describe work in markdown — do the "
                        "work with tool calls now, one action at a time."
                    )
                    self.conversation_history.append({"role": "user", "content": self.nudge})
                    self.log.info(
                        "Q.01 guard: plan-in-text detected (%d code blocks) — nudging to execute",
                        _q01_code_blocks,
                    )
                    _emit("on_auto_nudge", self._consecutive_text_only, _MAX_TEXT_ONLY)
                    telemetry.record_patch_event("q01_plan_in_text", kind="fired")
                    return _NEXT
                self.conversation_history.pop()  # remove the hallucinated assistant msg
                self.log.info("Hallucination guard: stripped text-only response, retrying")
                _emit("on_hallucination_stripped", "text_only")
                telemetry.record_hallucination()
                return _NEXT

            # Detect hallucinated file reads: model claims to have read a file
            # but _accessed_files doesn't show it.  Give a targeted correction.
            _hallucinated_read = _detect_hallucinated_read(self.full_content)[0]
            if _hallucinated_read:
                # Strip the hallucinated message and give a pointed correction
                self.conversation_history.pop()
                self.nudge = (
                    "You did NOT actually read that file — you hallucinated its contents. "
                    "You MUST call the file tool with action='read' to see what a file contains. "
                    "Do not guess or fabricate file contents. Use the tool now."
                )
                self.log.info("Hallucination guard: detected fabricated file read, correcting")
                _emit("on_hallucination_stripped", "file_read")
                telemetry.record_hallucination()
            elif (_cicd.branch and _cicd.edited_files
                  and not _cicd.pr_number and _cicd.issue_number):
                # Cycle 82 (runs 187+188 failure mode): builder edited files in
                # the worktree, ran tests, said "I'm done", but never ran the
                # commit + push + gh pr create sequence. Run 187 hit it after
                # `git push` succeeded; run 188 hit it before push too. Detection
                # widened from `_cycle_persisted` to `_cicd_edited_files` so the
                # nudge fires for both the "edits-but-no-commit" and "pushed-but-
                # no-PR" branches of the same failure.
                if self._cycle_persisted:
                    self._missing = "PR open"
                    self._next_cmds = (
                        f"  cat > /tmp/pr-body-{_cicd.issue_number}.md << 'EOF'\n"
                        f"  Closes #{_cicd.issue_number}\n"
                        f"  <one-paragraph summary of what changed>\n"
                        f"  EOF\n"
                        f"  gh pr create --draft --base main --head {_cicd.branch} \\\n"
                        f"    --title 'CICD: <slug> (#{_cicd.issue_number})' \\\n"
                        f"    --body \"$(cat /tmp/pr-body-{_cicd.issue_number}.md)\"\n"
                    )
                else:
                    self._missing = "commit + push + PR open"
                    self._next_cmds = (
                        f"  cd <WORKTREE_ROOT>/{_cicd.branch.split('/', 1)[-1] if '/' in _cicd.branch else _cicd.branch}\n"
                        f"  git add <edited files>\n"
                        f"  git commit -m 'CICD <N> (#{_cicd.issue_number}): <what>'\n"
                        f"  git push -u origin {_cicd.branch}\n"
                        f"  cat > /tmp/pr-body-{_cicd.issue_number}.md << 'EOF'\n"
                        f"  Closes #{_cicd.issue_number}\n"
                        f"  <one-paragraph summary of what changed>\n"
                        f"  EOF\n"
                        f"  gh pr create --draft --base main --head {_cicd.branch} \\\n"
                        f"    --title 'CICD: <slug> (#{_cicd.issue_number})' \\\n"
                        f"    --body \"$(cat /tmp/pr-body-{_cicd.issue_number}.md)\"\n"
                    )
                self.nudge = (
                    f"You edited files in worktree on `{_cicd.branch}` but {self._missing} "
                    f"is incomplete (step 8 of MANDATORY IMPLEMENTATION WORKFLOW). "
                    f"Tests passing is NOT the cycle ending — the cycle ends when "
                    f"the PR is open with `Closes #{_cicd.issue_number}`. Run these "
                    f"commands as your next tool calls:\n{self._next_cmds}"
                    f"Do not say you're done until `gh pr create` exits 0."
                )
                self.log.info("Auto-nudge (cycle 82): edits/push without PR — directing builder to %s", self._missing)
            else:
                # Generic nudge
                self.nudge = (
                    "Continue — use your tools to take the next action. "
                    "Empirical > theoretical: if you suspect a bug, verify it "
                    "with a tool before trying to fix it. "
                    "Do not repeat your analysis, just act."
                )
            self.conversation_history.append({"role": "user", "content": self.nudge})
            self.log.info("Auto-nudge (%d/%d, total %d/%d): text-only response, prompting to continue",
                    self._consecutive_text_only, _MAX_TEXT_ONLY, self._total_nudges, _MAX_TOTAL_NUDGES)
            _emit("on_auto_nudge", self._consecutive_text_only, _MAX_TEXT_ONLY)
            return _NEXT

    def _tool_guard(self):
        # Fix 2: Only reset consecutive counter on substantive tool calls.
        # Read-only tools (search, think, read_pdf) don't count as progress —
        # they let the model oscillate between "I'm done" and a weak grep
        # indefinitely.  file(action='read') is also read-only but uses the
        # generic "file" tool name, so we check the action arg.


        _tool_names = {_get_tc_name(tc) for tc in self.tool_calls}

        _is_read_only_file = (_tool_names == {"file"} and all(
            _get_tc_args(tc).get("action") == "read"
            for tc in self.tool_calls if _get_tc_name(tc) == "file"
        ))
        _is_read_only_exec = (_tool_names == {"exec_command"} and all(
            _is_read_only_command(_get_tc_args(tc).get("command", ""))
            for tc in self.tool_calls if _get_tc_name(tc) == "exec_command"
        ))
        _substantive = not (
            _tool_names <= self._READ_ONLY_TOOLS
            or _is_read_only_file
            or _is_read_only_exec
        )
        # F7: record read-class calls by (tool, args); the SAME read n times within the window
        # is the pre-death loop signature (an overrun log showed the model cycling re-reads of
        # its own files for its final minutes). A substantive batch (edit/write/exec) resets the
        # window — the model acted on what it read. The latch survives: one nudge per key.
        if not _substantive:
            for _tc in self.tool_calls:
                _k = _read_call_key(_get_tc_name(_tc), _get_tc_args(_tc))
                if _repeat_read_check(self._repeat_seen, _k, self.turn, self._REPEAT_READ_N, self._REPEAT_READ_WINDOW, self._repeat_latched):
                    self._repeat_msg = (f"[SYSTEM NOTICE: you have issued the same read — {_k[0]} with the same "
                                   f"arguments — {self._REPEAT_READ_N} times in the last {self._REPEAT_READ_WINDOW} turns "
                                   f"without acting on it. Re-reading will not change it. Decide and act: "
                                   f"edit, run, or report blocked with what you found.]")
                    if self._success_check_cmd:
                        _adv = self._maybe_advisor_nudge(source="stall", context=f"repeated read: {_k[0]} {_k[1][:200]}")
                        if _adv:
                            self.conversation_history.append({"role": "user", "content": _adv})
        else:
            self._repeat_seen.clear()
        if _substantive:
            self._consecutive_text_only = 0

        # Detect tool-call loops: if the same set of tool calls (by name +
        # args hash) repeats N times, the model is stuck.  Inject a correction
        # nudge instead of executing the Nth repeat.
        _batch_sig = frozenset(
            (_get_tc_name(tc), hashlib.md5(
                json.dumps(_get_tc_args(tc), sort_keys=True).encode()
            ).hexdigest()[:8])
            for tc in self.tool_calls
        )
        self._recent_tool_sigs.append(_batch_sig)
        if len(self._recent_tool_sigs) > self._TOOL_LOOP_THRESHOLD + 2:
            self._recent_tool_sigs.pop(0)
        # Count consecutive identical signatures at the tail
        _repeat = 0
        for _sig in reversed(self._recent_tool_sigs):
            if _sig == _batch_sig:
                _repeat += 1
            else:
                break
        if _repeat >= self._TOOL_LOOP_THRESHOLD:
            self.log.warning("Tool-call loop detected: same batch repeated %d times", _repeat)
            # Remove the assistant message that requested these tool calls
            self.conversation_history.pop()
            _loop_redirect = (
                "STOP — you have repeated the exact same tool call(s) "
                f"{_repeat} times with no effect. The approach is not working. "
                "Try a COMPLETELY DIFFERENT method. For example: "
                "use the file tool with action='write' to replace a line directly, "
                "or use python -c to do the replacement, "
                "or accept the current state and commit what you have."
            )
            # WS10.e rung 2: a plain text redirect that already failed once
            # this cycle gets escalated to a system-invoked think on the
            # loop evidence (beewatcher: "the injection message doesn't
            # always redirect" — run 114 died at rung 1).
            self._loop_interventions += 1
            if self._loop_interventions >= 2 and "think" in MAP_FN:
                try:
                    self._fk = MAP_FN["think"](prompt=(
                        f"MANDATORY REFLECTION: the same tool batch has now "
                        f"looped {_repeat}x AND a previous redirect this "
                        f"cycle was ignored. Batch signature: "
                        f"{str(_batch_sig)[:300]}. Answer: (1) why is the "
                        f"loop not converging? (2) name ONE materially "
                        f"different next action; (3) if none exists, the "
                        f"answer is: mark this step failed and move to "
                        f"CONSOLIDATE/PERSIST."))
                    _loop_redirect += ("\n\nFORCED REFLECTION (system-invoked "
                                       "think):\n" + str(self._fk)[:1500])
                    telemetry.record_patch_event("loop_forced_think", kind="fired")
                except Exception as _fe:
                    self.log.warning("forced think failed: %s", _fe)
                # Stall-side advisor escalation (batch-loop site): same stuck
                # signal as the semantic-result-loop site — repeated batch +
                # ignored redirect. Once-per-cycle latch dedupes across sites.
                self._adv_nudge = self._maybe_advisor_nudge(source="stall",
                                                  context=_loop_redirect)
                if self._adv_nudge:
                    _loop_redirect += "\n\n" + self._adv_nudge
            self.conversation_history.append({"role": "user", "content": _loop_redirect})
            self._recent_tool_sigs.clear()
            return _NEXT

    def _dispatch(self):
        # Execute tool calls
        self.log.debug("Executing %d tool calls", len(self.tool_calls))
        _emit("on_tool_batch_start", len(self.tool_calls))
        self._garbled_count = 0  # track garbled tool calls for retry
        _tool_exec_t0 = time.monotonic()
        if self._deadline_hard or self._refusal_hard:
            # F1/F9 hard stops are STRUCTURAL: past them no tool runs. Every call still gets a
            # paired tool result (strict templates reject an unanswered tool_call) saying why,
            # so the model's next turn is the final result the run is waiting for.
            self._why = (f"the wall-clock deadline ({int(self._DEADLINE_S)}s) has passed" if self._deadline_hard
                    else f"{self._refusal_tool} refused for an ungranted capability {self._REFUSAL_MAX_TURNS}+ turn(s) ago")
            _ask = ("what was done, what was not, artifact paths" if self._deadline_hard
                    else "a result block with \"status\": \"blocked\" naming the tool and its exit code")
            for tool_call in self.tool_calls:
                _tc_id = tool_call.id if hasattr(tool_call, "id") else tool_call.get("id")
                _tc_name = _get_tc_name(tool_call)
                self.conversation_history.append({
                    "role": "tool", "tool_call_id": _tc_id, "name": _tc_name,
                    "content": f"REFUSED: {self._why} — no tool runs now. Reply with your FINAL RESULT: {_ask}."})
            self.log.warning("%s: refused %d tool call(s) on post-stop turn %d",
                        "deadline" if self._deadline_hard else "refusal stop", len(self.tool_calls),
                        self._deadline_hard_turns if self._deadline_hard else self._refusal_hard_turns)
            _emit("on_notice", "warn", f"[{'Deadline' if self._deadline_hard else 'Refusal stop'}: {len(self.tool_calls)} tool call(s) refused — final result required]")
            return _NEXT
        try:
            with cancellable():
                for tool_call in self.tool_calls:
                    check_cancelled()
                    r = self._dispatch_call(tool_call)
                    if r is _BREAK:
                        break
                    if r is not None:
                        return r
        except CancelledError:
            _emit("on_cancelled", "tool_execution")
            self.log.info(
                "cancel.latency_ms latency_ms=%d site=tool_execution backend=%s",
                int((time.monotonic() - _tool_exec_t0) * 1000),
                _main_backend.kind,
            )
            self.log.info("CANCELLED during tool execution")
            if self._async_summarizer:
                self._async_summarizer.drain()
                self._async_summarizer.harvest(self.summary_state)
            _save_checkpoint(self.conversation_history, self.summary_state, self.turn, self.initial_files)
            return "cancelled"

    def _dispatch_call(self, tool_call):
        """One tool call: parse it, gate and run it, record the result and run the loop
        detectors on it. Returns None, the run's result, or _BREAK to end this turn's batch."""
        if not self._parse_call(tool_call):
            return None
        self.log.debug("TOOL CALL: %s(%s) [id=%s]", self.func_name, json.dumps(self.func_args), self.tool_id)
        telemetry.record_tool_call(self.func_name)

        # Track whether this turn has any "action" (write/exec) tool calls
        # for the action-inertia nudge (fires after _READ_ONLY_NUDGE_THRESHOLD
        # consecutive read-only turns).
        _ACTION_TOOLS = {"write_file", "edit_file", "append_file", "delete_file",
                         "exec_command", "gh_wrapper"}
        if self.func_name in _ACTION_TOOLS or (
            self.func_name == "file" and isinstance(self.func_args, dict)
            and self.func_args.get("action") in ("write", "insert", "append", "delete", "create")
        ):
            self._turn_had_action = True
        # Progress signal for the grind no-progress gate: a file
        # MUTATION (not exec/read) = the model is changing the
        # codebase = converging. exec_command is excluded (it's
        # usually re-running the check, i.e. spinning, not progress).
        if self.func_name in ("write_file", "edit_file", "append_file",
                         "delete_file") or (
            self.func_name == "file" and isinstance(self.func_args, dict)
            and self.func_args.get("action") in ("write", "insert",
                                            "append", "delete", "create")
        ):
            self._last_mutation_turn = self.turn
            # An edit invalidates any prior test run: the current
            # state is unverified until the tests re-run (claim gate).
            self._ran_verification = False

        # Per-call dedup: if this exact (tool_name, args) was dispatched
        # within the last _DEDUP_WINDOW calls AND the tool is safe to
        # dedup (pure reads — see _is_dedupable_call), short-circuit
        # with a synthetic result. Massive token-budget savings on the
        # PERCEIVE-boilerplate pattern two independent field audits
        # flagged as the #1 friction source.
        self._call_idx_counter += 1
        if self._dedup_hit():
            return None
        # Track think tool usage for CICD phase-gate enforcement
        if self.func_name == "think":
            self._cicd_think_used = True

        # Tools that do their own streaming (think) handle
        # their own console output — don't wrap them in a spinner.
        # Under NO_COLOR / no-TTY, CLEAR_LINE is empty, so the
        # spinner's non-interactive prefix would dangle and
        # on_tool_start's header would duplicate it on the same
        # line. Skip the spinner entirely in that mode.
        _STREAMING_TOOLS = {"think"}
        use_spinner = (
            self.func_name not in _STREAMING_TOOLS
            and not theme._no_color()
        )

        if use_spinner:
            self.tool_status = StreamStatus(emit=_emit)
            if self.func_name == "exec_command":
                # Full first line of the command — width fitting
                # happens at render time now (StreamStatus middle-
                # truncates every frame against the live terminal
                # width), so no fixed budget that assumed a "9.9s"
                # counter and broke past 100s.
                self._cmd_preview = self.func_args.get("command", "").split("\n", 1)[0]
                self.prefix = f"  -> exec_command ({self._cmd_preview}) "
            else:
                self.prefix = f"  -> {self.func_name} "
            # pt_header=False: in live-input mode the toolbar
            # spinner carries this status; the scrollback header
            # is printed once by on_tool_start after the tool
            # completes (printing both duplicated every line).
            self.tool_status.start(self.prefix, pt_header=False)

        # T5.18 — pre-write content snapshot for similarity
        # detection. Captured before dispatch so the post-write
        # nudge can compare new vs. old. Only for file(action=
        # 'write') on existing files; cheap when the file is
        # small (state JSON / focus JSON), bounded by tool
        # result truncation cap when not.
        self._prewrite_content = None
        if (self.func_name in ("write_file", "file")
                and isinstance(self.func_args, dict)
                and (self.func_name == "write_file" or self.func_args.get("action") == "write")
                and isinstance(self.func_args.get("path"), str)
                and isinstance(self.func_args.get("content"), str)):
            try:
                import os as _os_t518
                _pw_path = self.func_args["path"]
                if _os_t518.path.isfile(_pw_path):
                    with open(_pw_path, "r", encoding="utf-8",
                              errors="replace") as _pf:
                        self._prewrite_content = _pf.read()
            except Exception:
                self._prewrite_content = None
        r = self._gate_and_execute()
        if r is not None:
            return r
        if use_spinner:
            self.tool_status.first_token()
            self.tool_status.finish()
        _act.tool_finished(self.func_name, len(str(getattr(self, "result_str", "") or "")))
        _emit("on_tool_start", self.func_name, self.func_args)
        self._finish_result()
        self._write_loop_check()
        self._record_result()
        self._exec_followups()
        r = self._result_loop_checks()
        if r is not None:
            return r
        return self._after_result()

    def _parse_call(self, tool_call):
        """Name, arguments and id of one call; the garbled shapes answer with an error and
        False (the call is skipped). Returns True for a call to run."""
        try:
            if hasattr(tool_call, 'function'):
                self.func_name = tool_call.function.name
                self.raw_args = tool_call.function.arguments
                self.tool_id = tool_call.id
            else:
                self.func_name = tool_call["function"]["name"]
                self.raw_args = tool_call["function"]["arguments"]
                self.tool_id = tool_call["id"]
            # A name from another harness (bash, grep, Read) means a tool here.
            self._alias_note = ""
            if self.func_name not in MAP_FN:
                _resolved = _registry.resolve(self.func_name)
                if _resolved and _resolved != self.func_name:
                    self._alias_note = f"({self.func_name} is {_resolved} here; use that name.) "
                    self.log.info("tool.alias %s->%s", self.func_name, _resolved)
                    telemetry.record_patch_event("tool_alias", kind="fired")
                    self.func_name = _resolved
            self.func_args = json.loads(self.raw_args)
            # Coerce non-dict JSON values (null, [], 42, "str") to {}
            # so that **-unpack never raises TypeError (#859).
            # Consistent with _get_tc_args (line ~3218).
            if not isinstance(self.func_args, dict):
                self.func_args = {}
            # Sanitize garbled Gemma 4 args that parsed as valid JSON
            # e.g. {"action": "write**,content:"} — valid JSON but bogus values
            self.func_args = _ta.sanitize_tool_args(self.func_name, self.func_args, self.log)
        except json.JSONDecodeError:
            # Repair the JSON (truncation, a trailing comma, single quotes),
            # else salvage the garbled shapes (e.g. "write**,content:")
            self.func_args = _ta.repair_tool_args(self.func_name, self.raw_args, self.log)
            if self.func_args is None:
                self.log.error("Unsalvageable tool args: %s | raw: %s", self.func_name, self.raw_args)
                self._garbled_count += 1
                self.conversation_history.append({
                    "role": "tool", "tool_call_id": self.tool_id,
                    "name": self.func_name,
                    "content": f"Error: malformed arguments — could not parse. "
                               f"Use separate JSON keys: {{\"action\": \"write\", \"path\": \"...\", \"content\": \"...\"}}"
                })
                return False
        except Exception as e:
            self.log.error("Error parsing tool call: %s | raw: %s", e, tool_call)
            self._garbled_count += 1
            return False

        # Reject any tool call whose args contain Harmony/ChatML
        # control tokens (<|tool_call|>, <|channel|>, etc. — including
        # the partial-pipe variants like <tool_call|> and <|channel>).
        # These leak from model output under context pressure and have
        # caused self-amplifying state corruption (field observation: tokens
        # leaked into file path → file written with tokens in NAME →
        # git-committed → every cycle's `ls` re-injects them). Surface
        # as a tool-error so the model self-corrects; do NOT dispatch.
        _harmony_hit = _ta.detect_harmony_token(self.func_args)
        if _harmony_hit:
            key_path, token = _harmony_hit
            self.log.warning(
                "Rejecting %s call: chat-template token %r in arg %r",
                self.func_name, token, key_path or "(top-level)",
            )
            telemetry.record_tool_call(self.func_name + ":rejected_harmony_token")
            telemetry.record_patch_event("harmony_reject", kind="rejected")
            self._patch_telemetry["harmony_reject"] += 1
            self._garbled_count += 1
            self.conversation_history.append({
                "role": "tool", "tool_call_id": self.tool_id,
                "name": self.func_name,
                "content": (
                    f"Error: tool call rejected — chat-template control token "
                    f"{token!r} appeared in arg {key_path or '(top-level)'}. "
                    f"Your JSON was garbled (the end-of-tool-call delimiter leaked "
                    f"into an argument value). Make ONE clean tool call now with "
                    f"simple quoted string arguments. "
                    f"Example for {self.func_name}: "
                    + _ta.harmony_retry_hint(self.func_name)
                ),
            })
            return False

        # Hard-reject `think` calls that paraphrase recent context.
        # a field audit showed think prompts
        # full of "I am <agent>. Current State: - Cycle: 10, Mode:
        # Memory Curation..." — paraphrased context the model
        # already has, pays the LLM cost to reason about it again.
        # Framework owns the conversation history; check on the way
        # in, refuse on hit, force the model to frame the question
        # itself instead of the background.
        if self.func_name == "think" and isinstance(self.func_args, dict):
            _think_text = (self.func_args.get("prompt", "") or "") + "\n\n" + (self.func_args.get("context", "") or "")
            _launder = _ta.detect_context_laundering(_think_text, self.conversation_history)
            if _launder:
                offender, src_idx = _launder
                self.log.warning(
                    "Rejecting think call: %d-char verbatim overlap with msg[%d]: %r",
                    len(offender), src_idx, offender[:80],
                )
                telemetry.record_tool_call("think:rejected_laundering")
                telemetry.record_patch_event("think_launder", kind="rejected")
                self._patch_telemetry["think_launder"] += 1
                self.conversation_history.append({
                    "role": "tool", "tool_call_id": self.tool_id,
                    "name": self.func_name,
                    "content": (
                        f"Error: think call rejected — your prompt/context "
                        f"re-includes a {len(offender)}-char verbatim chunk "
                        f"from your own recent message (msg[{src_idx}], "
                        f"starts with {offender[:60]!r}). The framework "
                        f"already has that content. Frame the reasoning "
                        f"question itself — what specifically you need to "
                        f"think through — not the background you already "
                        f"know. If you genuinely need extra constraints, "
                        f"summarize them abstractly in `context` (a few "
                        f"sentences, not paragraphs)."
                    ),
                })
                return False

        # Validate required fields — catch cases where sanitizer
        # extracted the action but lost required params (empty garble)
        if self.func_name == "file" and isinstance(self.func_args, dict):
            action = self.func_args.get("action", "")
            if action in ("read", "write", "insert", "append", "delete") and "path" not in self.func_args:
                self.log.warning("Sanitized file call missing 'path' — returning error")
                self._garbled_count += 1
                self.conversation_history.append({
                    "role": "tool", "tool_call_id": self.tool_id,
                    "name": self.func_name,
                    "content": (
                        f"Error: file({action!r}) call is missing the 'path' argument. "
                        f"Call with separate string arguments: "
                        f"file(action={action!r}, path='the/file/path'). "
                        f"Do not concatenate arguments with backticks or commas inside a string."
                    ),
                })
                return False

        # G.01-exec — garbled exec_command args under context pressure.
        # Model produces {"command=\"...reasoning...\"": <number>} instead
        # of {"command": "..."}. No reliable salvage — return clear error.
        # A poll of a background session (session_id, optional wait, no
        # command) is the tool's own documented form, not garbage: it
        # passes (found live 2026-11-02: a correct poll was refused).
        if self.func_name == "exec_command" and isinstance(self.func_args, dict):
            _cmd_val = self.func_args.get("command")
            _sid_val = self.func_args.get("session_id")
            _is_poll = (_cmd_val is None and isinstance(_sid_val, str) and _sid_val.strip())
            if not _is_poll and (_cmd_val is None or not isinstance(_cmd_val, str)):
                self._garbled_count += 1
                self.conversation_history.append({
                    "role": "tool", "tool_call_id": self.tool_id,
                    "name": self.func_name,
                    "content": (
                        "Error: exec_command received garbled arguments "
                        "(missing or non-string 'command' value). "
                        "Call with a single string: "
                        "exec_command(command='your shell command here')."
                    ),
                })
                self.log.warning("G.01-exec: garbled exec_command — keys=%r",
                            list(self.func_args.keys())[:3])
                telemetry.record_patch_event("g01_exec", kind="fired")
                return False

        # R.01 — node runtime used to execute a .py file.
        # The model confuses Python CLI tools with the Node-based
        # discord/email skills that live nearby.  Intercept before
        # dispatch and return a clear correction.
        if self.func_name == "exec_command" and isinstance(self.func_args, dict):
            _r01_cmd = self.func_args.get("command") or ""
            if isinstance(_r01_cmd, str) and re.match(r'\bnode\b.*\.py\b', _r01_cmd):
                self._garbled_count += 1
                self.conversation_history.append({
                    "role": "tool", "tool_call_id": self.tool_id,
                    "name": self.func_name,
                    "content": (
                        f"Error: used 'node' to run a Python file. "
                        f"Python scripts must be run with 'python3', not 'node'. "
                        f"Replace 'node' with 'python3' and retry."
                    ),
                })
                self.log.warning("R.01: node used on .py file — %r", _r01_cmd[:80])
                return False
        return True

    def _dedup_hit(self):
        """An identical read within the dedup window answers with the cached summary and
        returns True; else registers the signature for the store after the run."""
        self._dedup_sig = None
        if _is_dedupable_call(self.func_name, self.func_args):
            try:
                self._args_md5 = hashlib.md5(
                    json.dumps(self.func_args, sort_keys=True, default=str).encode()
                ).hexdigest()
            except Exception:
                self._args_md5 = None
            if self._args_md5 is not None:
                self._dedup_sig = (self.func_name, self._args_md5)
                _cached = self._call_dedup_cache.get(self._dedup_sig)
                if _cached is not None and (self._call_idx_counter - _cached["call_idx"]) <= self._DEDUP_WINDOW:
                    _gap = self._call_idx_counter - _cached["call_idx"]
                    _synthetic = (
                        f"[deduped: identical {self.func_name} call was issued {_gap} "
                        f"tool-call(s) ago — result was ~{_cached['tokens']} tokens, "
                        f"starting with: {_cached['summary']!r}. If state may have "
                        f"changed since, vary the arguments meaningfully and retry; "
                        f"otherwise act on the cached result.]"
                    )
                    self.conversation_history.append({
                        "role": "tool", "tool_call_id": self.tool_id,
                        "name": self.func_name,
                        "content": _synthetic,
                    })
                    self.log.info(
                        "Deduped %s call (last issued %d calls ago, sig %s)",
                        self.func_name, _gap, self._args_md5[:8],
                    )
                    telemetry.record_tool_call(self.func_name + ":deduped")
                    # Approximate tokens saved = the cached result's token estimate
                    telemetry.record_patch_event("dedup", kind="saved", value=_cached.get("tokens", 1))
                    self._patch_telemetry["dedup"] += 1
                    return True
        return False

    def _gate_and_execute(self):
        """The gates (grace, phase, success check, CICD, unknown or unloaded tool), then the
        tool itself; result_str holds the answer. Returns the run's result on end_cycle."""
        # Cycle 24: pre-execute PRE-MERGE CHECK short-circuit.
        # When reviewer attempts `gh pr merge` without prior
        # `gh issue view`, block execution (the merge is irreversible
        # once it runs; post-hoc reminders are too late). Return a
        # synthetic error; next turn the reviewer runs `gh issue view`,
        # WS8.3 (run 096): grace budget must bind tool-call turns
        # too. Previously only text-only turns were grace-capped,
        # so a builder that kept calling tools started a second
        # PERCEIVE inside the window and ran to the hard cap
        # (~15 wasted turns). After grace exhausts, non-TRACK
        # calls are blocked with a redirect; TRACK-scope work
        # (results/progress/review files, git/gh, end_cycle)
        # still runs. Hard cap at :_CYCLE_HARD_STOP_TURNS unchanged.
        _grace_exhausted_block = False
        if (self._cycle_persisted
                and (self.turn - (self._cycle_persisted_turn or self.turn)) >= self._CYCLE_GRACE_TURNS
                and not _cicd.is_track_scope_call(self.func_name, self.func_args)):
            _grace_exhausted_block = True
            self._grace_tool_blocks += 1
            self.log.info("Grace exhausted: non-TRACK tool call blocked (%s, block #%d)",
                     self.func_name, self._grace_tool_blocks)
            telemetry.record_patch_event("grace_tool_block", kind="fired")
            self.result_str = (
                "Error: this cycle already persisted its work and the "
                "post-persist grace window is exhausted — this call is "
                "outside TRACK scope and was NOT executed. Do NOT start "
                "new PERCEIVE/investigation work. Finish TRACK now "
                "(results file, progress row, issue comment) and call "
                "end_cycle."
            )
            self._cicd_blocked, self.cicd_error = False, None
        else:
            self._cicd_blocked, self.cicd_error = _cicd.validate_tool_call(self.func_name, self.func_args, self._cicd_issue_view_called, self.log, self._is_cicd_builder, self._is_reviewer_role)
        # WS1: PhaseEngine pre-dispatch gate (per-phase tool
        # rules). Only consulted when an engine is active.
        _phase_blocked = False
        if (self._phase_engine is not None and not _grace_exhausted_block
                and not self._cicd_blocked):
            _ph_ok, _ph_msg = self._phase_engine.allow(self.func_name, self.func_args)
            if not _ph_ok:
                _phase_blocked = True
                self.result_str = _ph_msg
                telemetry.record_patch_event("phase_gate_block", kind="fired")
        # WS10.c: success-check gate on end_cycle. After
        # _SUCCESS_CHECK_MAX_BLOCKS failed attempts the gate
        # stands aside (a gate that can deadlock a weak model is
        # worse than none — turn caps still bound the cycle).
        _sc_blocked = False
        if (self.func_name == "end_cycle" and self._success_check_cmd
                and not _grace_exhausted_block and not _phase_blocked
                and not self._cicd_blocked
                and self._success_check_blocks < self._SUCCESS_CHECK_MAX_BLOCKS):
            _sc_rc, _sc_out = _run_success_check(self._success_check_cmd, self.log)
            if _sc_rc != 0:
                _sc_blocked = True
                self._success_check_blocks += 1
                self.log.info("success_check FAILED (rc=%d) — end_cycle blocked (%d/%d)",
                         _sc_rc, self._success_check_blocks, self._SUCCESS_CHECK_MAX_BLOCKS)
                telemetry.record_patch_event("success_check_block", kind="fired")
                self.result_str = (
                    "Error: end_cycle blocked — the declared success "
                    "check still FAILS (exit=" + str(_sc_rc) + "). The "
                    "work is NOT complete. Check output tail:\n"
                    + _sc_out +
                    "\nFix what the check reports, re-run it yourself "
                    "to confirm, commit, then call end_cycle again."
                )
                _adv_msg = self._maybe_advisor_nudge()
                if _adv_msg:
                    self.result_str += "\n\n" + _adv_msg
            else:
                self.log.info("success_check PASSED — end_cycle permitted")
        if _grace_exhausted_block or _phase_blocked or _sc_blocked:
            pass  # result_str already holds the redirect
        elif self._cicd_blocked:
            self.result_str = self.cicd_error
        elif self.func_name == "end_cycle" and not _END_CYCLE_ENABLED:
            self.result_str = ("Error: end_cycle is off for this agent (it is opt-in: "
                          "preferences.end_cycle_tool in .agent/config.json). "
                          "Stop when you are done; do not try to end the session.")
        elif self.func_name not in MAP_FN:
            _sugg = _registry.suggest(self.func_name, self.func_args)
            self.result_str = (f"Error: Unknown tool '{self.func_name}'."
                          + (f" Closest: {_sugg}." if _sugg else "")
                          + " Use tool_search to find and load tools.")
            telemetry.record_patch_event("tool_unknown", kind="fired")
        elif self.func_name in _registry.DEFINITIONS and not _registry.available(self.func_name):
            self.result_str = f"Error: {self.func_name} is not available in this session."
            self.log.info("tool.unavailable name=%s", self.func_name)
        elif (self.func_name in _registry.DEFINITIONS
              and self.func_name not in _session_tool_names(self._session_tools)
              and not (_TOOLS_CFG.get("implicit_run", True)
                       and _registry.args_fit(self.func_name, self.func_args))):
            # A deferred or hidden tool called from memory with arguments that do
            # not fit (or implicit runs are off): load it, answer with its schema.
            _registry.request_load(self.func_name)
            self.result_str = (f"Error: {self.func_name} is not loaded and these arguments do not fit it. "
                          f"Its schema: {json.dumps(_registry.DEFINITIONS[self.func_name].get('function'))}. "
                          "It is loaded now; call it again with arguments that fit.")
            self.log.info("tool.schema_returned name=%s", self.func_name)
        else:
            # A MAP_FN entry outside the registry (harness-internal, or a test's
            # stand-in) runs as it always did; a registered tool not yet in the
            # session's list is loaded for the next turn.
            if self.func_name in _registry.DEFINITIONS and self.func_name not in _session_tool_names(self._session_tools):
                _registry.request_load(self.func_name)
                self.log.info("tool.implicit_load name=%s", self.func_name)
            try:
                # Last check before dispatch: an esc-esc that
                # landed during argument parsing / gate checks
                # should stop the batch here, not after another
                # long tool run.
                check_cancelled()
                _act.tool_started(self.func_name, self.func_args)
                self.result_str = str(MAP_FN[self.func_name](**self.func_args))
                if self._alias_note:
                    self.result_str = self._alias_note + self.result_str
                if self.func_name == "tool_search":
                    telemetry.record_patch_event("tool_search", kind="fired")
            except CircuitBreakerError as e:
                # Tool temporarily unavailable - return graceful degradation
                self.result_str = f"Tool {self.func_name} temporarily unavailable: {e}"
            except CancelledError:
                # Propagate to outer handler to return 'cancelled'
                raise
            except Exception as e:
                self.result_str = f"Error executing tool: {str(e)}"
                telemetry.record_tool_error(self.func_name, "execution_error")
            # WS1: PhaseEngine observation — advance on
            # task_tracker phase-done markers, count verify
            # calls; may return a gate message to inject
            # (DECIDE verification gate).
            if self._phase_engine is not None:
                _gate_msg = self._phase_engine.observe(self.func_name, self.func_args, self.result_str)
                if _gate_msg:
                    self.conversation_history.append(
                        {"role": "user", "content": _gate_msg})
                    telemetry.record_patch_event(
                        "phase_verify_gate", kind="fired")
            # end_cycle sentinel — agent requested clean exit.
            if self.result_str == _end_cycle_tool.SENTINEL:
                self._summary = self.func_args.get("summary", "") if isinstance(self.func_args, dict) else ""
                self.log.info("end_cycle called — exiting cleanly: %s",
                         self._summary or "(no summary)")
                telemetry.record_patch_event("end_cycle", kind="fired")
                # Answer the tool_call before leaving: an interactive
                # session keeps this history, and an assistant
                # tool_call with no tool message is rejected by
                # strict APIs on the next turn.
                self.conversation_history.append({
                    "role": "tool",
                    "tool_call_id": self.tool_id,
                    "name": self.func_name,
                    "content": "Cycle ended.",
                })
                return "done"
            # Conversational tool recovery: on error, try to fix params
            if self.result_str.startswith("Error"):
                try:
                    from tool_recovery import attempt_recovery
                    recovered = attempt_recovery(
                        self.func_name, self.func_args, self.result_str,
                        map_fn=MAP_FN,
                        llm_call_fn=_recovery_llm_call(self.log),
                        config=_config, log=self.log,
                    )
                    if recovered is not None:
                        self.result_str = recovered
                        _emit("on_tool_recovery", self.func_name, 1)
                        _emit("on_notice", "info", f"[recovered: {self.func_name} succeeded]")
                except Exception as e:
                    self.log.debug("Tool recovery unavailable: %s", e)

    def _finish_result(self):
        """The result as the model will see it: gh verification, the spill, the truncation,
        the capability-refusal notice, the dedup store."""
        global _LAST_REFUSAL
        # Cycle 77 — verify gh pr/issue mutations against real state.
        # Appends a SUPERVISION note when the tool output references
        # a PR/issue that does not exist or is not in the claimed state.
        if self.func_name == "exec_command" and isinstance(self.func_args, dict):
            _cmd_str = self.func_args.get("command", "")
            if _cmd_str:
                self.result_str = _cicd.verify_gh_mutation(_cmd_str, self.result_str, self.log)

        # F3: an oversized tool result is SPILLED to a file and replaced by a
        # reference + excerpt (lossless — the old head/tail truncation threw the
        # middle away, and a data-heavy run died echoing bulk into a 196k window).
        # limits.tool_result_max_chars, 0 = legacy truncation only.
        _spill_cap = _tool_result_spill_cap()
        if _spill_cap > 0 and len(self.result_str) > _spill_cap and not self.result_str.startswith(_SPILL_MARKER):
            _ref = _spill_tool_result(self.turn, self.func_name, self.result_str, self.log)
            if _ref is not None:
                self.log.info("F3 spill: %s result of %d chars -> file (cap %d)", self.func_name, len(self.result_str), _spill_cap)
                telemetry.record_patch_event("tool_result_spill", kind=self.func_name)
                self.result_str = _ref
        if len(self.result_str) > _MAX_TOOL_RESULT_CHARS:
            half = _MAX_TOOL_RESULT_CHARS // 2
            self.result_str = (
                self.result_str[:half]
                + f"\n\n... [{len(self.result_str) - _MAX_TOOL_RESULT_CHARS} chars truncated] ...\n\n"
                + self.result_str[-half:]
            )

        # F9 — capability refusal: the tool said the run was never granted something
        # it needs. Say so ONCE, inside the result the model is about to read, and
        # start the clock; the turn-top stop is what makes the window bounded.
        if self._REFUSAL_MAX_TURNS > 0:
            _rf = _classify_tool_refusal(self.func_name, self.result_str)
            if _rf is not None:
                _rf_code, _rf_head = _rf
                _rf_cmd = (str(self.func_args.get("command") or "")[:120]
                           if isinstance(self.func_args, dict) else "")
                if self._refusal_first_turn is None:
                    self._refusal_first_turn, self._refusal_tool = self.turn, self.func_name
                    _LAST_REFUSAL = {"tool": self.func_name, "cmd": _rf_cmd, "exit": _rf_code,
                                     "head": _rf_head, "turn": self.turn}
                    self.log.warning("capability refusal at turn %d: %s %s exit=%s — %s (blocked "
                                "result due within %d turn(s))", self.turn, self.func_name, _rf_cmd,
                                _rf_code, _rf_head, self._REFUSAL_MAX_TURNS)
                    _emit("on_notice", "warn",
                          f"[Capability refusal: {self.func_name} exit={_rf_code} — blocked result "
                          f"due within {self._REFUSAL_MAX_TURNS} turn(s)]")
                    telemetry.record_patch_event("capability_refusal", kind=self.func_name)
                    self.result_str += "\n\n" + _refusal_notice(self.func_name, _rf_cmd, _rf_code,
                                                              _rf_head, self._REFUSAL_MAX_TURNS)
                else:
                    self.log.warning("capability refusal AGAIN at turn %d: %s %s exit=%s",
                                self.turn, self.func_name, _rf_cmd, _rf_code)
        # Store in dedup cache if this call was eligible (see
        # _is_dedupable_call). The synthetic result returned by a
        # later duplicate references this entry's `call_idx` and
        # summary; storing after truncation keeps the summary
        # consistent with what the model originally saw.
        if self._dedup_sig is not None:
            self._call_dedup_cache[self._dedup_sig] = {
                "call_idx": self._call_idx_counter,
                "summary": self.result_str[:80].replace("\n", " "),
                "tokens": max(1, len(self.result_str) // 4),
            }
            # Prune entries older than the window so the cache
            # doesn't grow without bound across long sessions.
            if len(self._call_dedup_cache) > 256:
                _stale = [k for k, v in self._call_dedup_cache.items()
                          if (self._call_idx_counter - v["call_idx"]) > self._DEDUP_WINDOW * 4]
                for k in _stale:
                    self._call_dedup_cache.pop(k, None)

    def _write_loop_check(self):
        """The write-loop detector and the edit nudges for a call that wrote a file."""
        # Write-loop detector: if this tool wrote to a path that
        # we've already written 2+ times in the recent window,
        # append a system reminder to the tool result so the model
        # sees the pattern. a field-observed run ("The Orphan Paradox") canonical
        # signature: 3 write-audit-rewrite passes on context.json
        # in one cycle because the model's mental model of the
        # audit tool was wrong. Framework-side detection makes the
        # loop visible without relying on model self-noticing.
        _write_target = _extract_write_target(self.func_name, self.func_args)
        if _write_target:
            # T5.14 Option A — heredoc→state-file write nudge.
            # When the model writes a tracked state file via
            # exec_command 'cat > f <<EOF', append a one-line
            # suggestion that file(action='edit', ...) is safer
            # for surgical changes. a field-observed run noted exactly
            # this failure mode ("accidentally purged multiple
            # edges due to incorrect range replacement") on a
            # heredoc-style state-file rewrite. Only fires once
            # per session per file to avoid noise.
            _heredoc_write = (
                self.func_name == "exec_command"
                and "<<" in self.func_args.get("command", "")
                and _write_target.endswith(".json")
            )
            if _heredoc_write:
                if "_edit_nudges_emitted" not in dir(run_agent_single):
                    run_agent_single._edit_nudges_emitted = set()
                if _write_target not in run_agent_single._edit_nudges_emitted:
                    run_agent_single._edit_nudges_emitted.add(_write_target)
                    self.result_str = self.result_str + (
                        f"\n\n[suggestion] You wrote to {_write_target!r} via "
                        f"a heredoc. For surgical changes to an existing file, "
                        f"file(action='edit', path=..., old_string=..., "
                        f"new_string=...) is safer — it's atomic, validates "
                        f"that the target text exists exactly once (no silent "
                        f"miss), and won't clobber neighbouring content the way "
                        f"a heredoc full-rewrite can. Heredoc is fine for "
                        f"creating new files or wholesale replacement; for "
                        f"single-field updates, prefer edit."
                    )
                    telemetry.record_patch_event("edit_nudge", kind="fired")
                    self._patch_telemetry["edit_nudge"] += 1

            # T5.18 — file(action='write') high-similarity rewrite
            # nudge. The model has been bypassing the heredoc
            # nudge by routing through file(action='write')
            # instead of exec_command. two independent field audits
            # both showed full-file rewrites where most lines
            # were identical to the previous version — surgical
            # changes wearing a full-write costume. Detection:
            # Jaccard line-set similarity vs pre-write snapshot;
            # ≥0.8 triggers the nudge. Tracked separately under
            # kind="similar_rewrite" so we can measure whether
            # the new nudge fires while the model continues to
            # avoid action='edit', or whether behavior shifts.
            _similar_rewrite = (
                not _heredoc_write
                and self._prewrite_content is not None
                and self.func_name in ("file", "write_file")
                and isinstance(self.func_args, dict)
                and (self.func_name == "write_file" or self.func_args.get("action") == "write")
            )
            if _similar_rewrite:
                _new_content = self.func_args.get("content", "")
                _old_lines = set(self._prewrite_content.splitlines())
                _new_lines = set(_new_content.splitlines())
                if _old_lines and _new_lines:
                    _inter = len(_old_lines & _new_lines)
                    # MAX of two asymmetric ratios so both
                    # "most-of-old-survived" (small edit to large
                    # file → preserved_ratio high) and "most-of-
                    # new-came-from-old" (some additions, rest
                    # carried over → overlap_ratio high) trigger
                    # the nudge. Symmetric Jaccard penalizes both
                    # directions and would miss the a field-observed run
                    # "added 30 lines to a 200-line HTML" case.
                    _preserved = _inter / len(_old_lines)
                    _overlap = _inter / len(_new_lines)
                    _sim = max(_preserved, _overlap)
                    if _sim >= 0.8:
                        if "_edit_nudges_emitted" not in dir(run_agent_single):
                            run_agent_single._edit_nudges_emitted = set()
                        if _write_target not in run_agent_single._edit_nudges_emitted:
                            run_agent_single._edit_nudges_emitted.add(_write_target)
                            self.result_str = self.result_str + (
                                f"\n\n[suggestion] You used "
                                f"write_file to rewrite "
                                f"{_write_target!r}, but {int(_sim*100)}% "
                                f"of the lines match the previous version "
                                f"— this is a surgical change in a "
                                f"full-rewrite costume. edit_file("
                                f"path=..., old_string=..., new_string=...) "
                                f"is the right tool: atomic, validates the "
                                f"target text exists exactly once, and won't "
                                f"silently drop unrelated keys (a field-observed run lost "
                                f"theme_tracking this way and regressed for "
                                f"13 cycles). Use write_file only when creating "
                                f"a new file or rewriting one in full."
                            )
                            telemetry.record_patch_event(
                                "edit_nudge", kind="similar_rewrite")
                            self._patch_telemetry["edit_nudge"] += 1

            # T5.18-log — file(action='write') on a .log file destroys
            # all prior entries.  Field-audited agents treat logs as episodic
            # memory; wiping them with write is high-damage (observed 9%
            # of Phase 7 sessions, including the a field-observed run spiral that
            # wiped consciousness.log).  Logs are append-only by design;
            # action='append' is always correct here.  Fires once per
            # file per session to avoid noise.
            _log_overwrite = (
                not _heredoc_write
                and self.func_name in ("file", "write_file")
                and isinstance(self.func_args, dict)
                and (self.func_name == "write_file" or self.func_args.get("action") == "write")
                and _write_target.endswith(".log")
            )
            if _log_overwrite:
                if "_edit_nudges_emitted" not in dir(run_agent_single):
                    run_agent_single._edit_nudges_emitted = set()
                if _write_target not in run_agent_single._edit_nudges_emitted:
                    run_agent_single._edit_nudges_emitted.add(_write_target)
                    self.result_str = self.result_str + (
                        f"\n\n[suggestion] write_file on "
                        f"{_write_target!r} overwrote all prior log "
                        f"content. Log files are append-only — use "
                        f"append_file(path={_write_target!r}, "
                        f"content=...) to add new entries without "
                        f"destroying history."
                    )
                    self.log.warning(
                        "T5.18-log: write on log file %r — nudging append",
                        _write_target,
                    )
                    telemetry.record_patch_event("log_write_nudge", kind="fired")
                    self._patch_telemetry["edit_nudge"] += 1

            _hist = self._write_path_history.setdefault(_write_target, [])
            _hist.append(self._call_idx_counter)
            # Prune outside window
            self._write_path_history[_write_target] = [
                t for t in _hist if (self._call_idx_counter - t) <= self._WRITE_LOOP_WINDOW
            ]
            _n_writes = len(self._write_path_history[_write_target])
            # Single-object state files — ones an agent rewrites
            # wholesale on a schedule rather than appending to — are
            # SUPPOSED to be written repeatedly. One agent's
            # detector misfired with `current-state.json` written
            # 3 times in one cycle (correct behavior!). Bump the
            # threshold so the detector only complains when the
            # rewrite count is unambiguously a loop.
            _is_single_object_state = (
                _write_target.endswith(("current-state.json",
                                        "focus.json",
                                        "context.json"))
            )
            _effective_threshold = 5 if _is_single_object_state else self._WRITE_LOOP_THRESHOLD
            if _n_writes >= _effective_threshold:
                self.result_str = self.result_str + (
                    f"\n\n[write-loop-detector] You have written to {_write_target!r} "
                    f"{_n_writes} times in the last {self._WRITE_LOOP_WINDOW} tool calls. "
                    f"If your incremental edits are being clobbered by full rewrites, "
                    f"use edit_file(...) to apply a surgical change instead "
                    f"of rewriting the whole file. If you've genuinely needed this many "
                    f"writes, ignore this note — but consider whether your mental model "
                    f"of the file's state matches what's actually on disk."
                )
                self.log.info(
                    "Write-loop detector tripped: %s written %d times in last %d calls",
                    _write_target, _n_writes, self._WRITE_LOOP_WINDOW,
                )
                telemetry.record_tool_call(self.func_name + ":write_loop")
                telemetry.record_patch_event("write_loop", kind="fired")
                self._patch_telemetry["write_loop"] += 1

    def _record_result(self):
        """Append the tool result, compress repeats, track edit failures and CICD edits."""
        self.conversation_history.append({
            "role": "tool",
            "tool_call_id": self.tool_id,
            "name": self.func_name,
            "content": self.result_str,
        })
        if _prompt_caching_backend():
            # The sent window (from the cache head) must stay byte-identical:
            # gate on ITS pressure and never rewrite inside it. Below the head
            # nothing is sent again, so head+tail only — no model round trip.
            self._head = self.summary_state.get("_cache_head") or 0
            _rewritten = _summ.compress_repeated_tool_results(
                self.conversation_history, self.func_name, self.log,
                pressure=_summ.history_pressure(self.conversation_history, start=self._head),
                protect_from=self._head, local_only=True)
            if any(i >= self._head for i in _rewritten):
                self.summary_state["_rewrote_window"] = True   # alarm for the cache.miss log
        else:
            _summ.compress_repeated_tool_results(self.conversation_history, self.func_name, self.log,
                                            pressure=_summ.history_pressure(self.conversation_history))

        # Consecutive edit_file failure guard: after 2 consecutive
        # old_string-not-found failures on the same file, inject a
        # user turn directing the model to switch strategy. The
        # existing error hint says "re-read the file" but models
        # often ignore it and retry with equally-wrong content.
        _is_edit_fail = (
            self.func_name in ("edit_file", "file")
            and "`old_string` not found" in self.result_str
            and isinstance(self.func_args, dict)
        )
        if _is_edit_fail:
            self._epath = self.func_args.get("path", "")
            self._edit_fail_counts[self._epath] = self._edit_fail_counts.get(self._epath, 0) + 1
            if self._edit_fail_counts[self._epath] >= 2:
                self.conversation_history.append({
                    "role": "user",
                    "content": (
                        f"[SYSTEM: edit_file has failed {self._edit_fail_counts[self._epath]} "
                        f"consecutive times for '{self._epath}' — the old_string does not "
                        f"match the actual file content. STOP using edit_file for this "
                        f"file. Instead: call read_file(path='{self._epath}') to get the "
                        f"EXACT current content, then use write_file to rewrite the "
                        f"relevant section, or use exec_command with a Python script "
                        f"to patch it in-place. Do not attempt edit_file again for "
                        f"'{self._epath}' until you have read the file.]"
                    ),
                })
        elif self.func_name in ("edit_file", "file", "write_file") and isinstance(self.func_args, dict):
            # Successful edit or full rewrite — reset failure counter for this path.
            # write_file changes file content entirely so prior old_string mismatches
            # are no longer relevant for the new version.
            self._epath = self.func_args.get("path", "")
            self._edit_fail_counts.pop(self._epath, None)

        # Track file edits (file tool with action=write/create, or new per-action tools)
        if isinstance(self.func_args, dict) and (
            (self.func_name == "file" and self.func_args.get("action") in ("write", "create"))
            or self.func_name in ("write_file", "edit_file")
        ):
            self._has_edited, self._has_reviewer_persisted = _cicd.handle_file_edit(
                self.func_args, self.conversation_history, _cicd.worktree_path, _cicd.phase_state, 
                _cicd.edited_files, self._has_edited, self._has_reviewer_persisted, self.turn, self.log
            )

    def _exec_followups(self):
        """What an exec_command result says about the run: commits, pushes, worktrees, PRs,
        reviews, merges (the CICD phase trackers)."""
        # Track commits and pushes through exec_command
        if self.func_name == "exec_command" and isinstance(self.func_args, dict):
            _cmd = self.func_args.get("command", "")
            _cmd_normalized = re.sub(r"\\\n", " ", _cmd)  # Cycle 36: collapse shell line-continuations before all checks
            # Claim-vs-trace: a real test/verify run supports a later
            # "I ran the tests / verified" completion claim.
            if any(_vt in _cmd_normalized.lower() for _vt in _VERIFY_CMD_TOKENS):
                self._ran_verification = True
            if _cicd.cmd_has(_cmd, "git commit"):  # WS8.1: anchored, heredoc/quote-blind
                self._has_committed = True
                self._has_edited = True  # commit implies edit happened
                self.log.info("Commit detected — completion signals now allowed")
                _cicd.phase_state["implement"] = True
                # Cycle 61: if commit fails with nothing staged, builder never wrote test code
                if "no changes added to commit" in self.result_str:
                    self.conversation_history.append({
                        "role": "user",
                        "content": (
                            "[SYSTEM: git commit failed — no test files were staged. "
                            "Only .coverage is modified, which means you ran pytest but "
                            "never wrote any new test code. You must use "
                            "file({'action': 'write', 'path': 'tests/test_<name>.py', "
                            "'content': '...'}) to write new test functions, then "
                            "git add tests/test_<name>.py && git commit -m '<message>'.]"
                        ),
                    })
            if _cicd.cmd_has(_cmd, "git push") and "exit=0" in self.result_str:  # Cycle 35+36 anchoring; WS8.1: heredoc/quote-blind (a doc line `&& git push` must not fake-persist the cycle)
                if not self._cycle_persisted:
                    self.log.info("Cycle persist detected (git push exit=0) — auto-nudge disabled")
                # Interactive sessions keep working after a push: the
                # grace/hard-stop gates are for -a cycles only.
                if _AUTO_MODE:
                    self._cycle_persisted = True
                    self._cycle_persisted_turn = self._cycle_persisted_turn or self.turn
                _cicd.phase_state["verify"] = True
                if self._phase_engine is not None:
                    self._phase_engine.mark_persisted("git push")
                # Cycle 37: block direct push to main — builder must use feature branches.
                # Only enforce for CICD builder sessions; regular agents push to main normally.
                if self._is_cicd_builder and re.search(r"git\s+push\b.*\borigin\s+main\b", _cmd_normalized):
                    self.log.warning("CICD: git push origin main — builder pushed directly to main")
                    self.conversation_history.append({
                        "role": "user",
                        "content": (
                            "[SYSTEM: CRITICAL: You just pushed directly to origin/main. "
                            "This is PROHIBITED — all CICD work must go through a feature branch "
                            "(cicd/NNN-slug) and PR. Immediately revert: "
                            "`git revert HEAD --no-edit && git push origin main`. "
                            "Then create a proper worktree and branch for your changes. "
                            "Do NOT commit test artifacts or state files to main.]"
                        ),
                    })

            # Warn when git add stages Python cache files.
            # CC agents always name files explicitly; agent.py agents
            # use `git add -A` which silently picks up __pycache__ and
            # *.pyc files, polluting git history.
            if (re.search(r"git\s+add\b", _cmd_normalized)
                    and ("__pycache__" in self.result_str or ".pyc" in self.result_str)):
                self.log.warning("git add staged __pycache__/.pyc files — injecting cleanup hint")
                self.conversation_history.append({
                    "role": "user",
                    "content": (
                        "[SYSTEM: Your git add staged Python cache files "
                        "(__pycache__/ or *.pyc). These should not be committed. "
                        "Fix before committing:\n"
                        "  git rm -r --cached __pycache__/ scripts/__pycache__/ bin/__pycache__/ 2>/dev/null || true\n"
                        "  printf '__pycache__/\\n*.pyc\\n.agent/\\n' >> .gitignore\n"
                        "  git add .gitignore\n"
                        "Then re-add only your actual changed files by name (not git add -A).]"
                    ),
                })

            # ── CICD phase detection ──
            if _cicd.cmd_has(_cmd, "gh issue list") or _cicd.cmd_has(_cmd, "gh issue search"):  # WS8.1
                _cicd.phase_state["perceive"] = True
            if ("pytest" in _cmd or "python3 -m pytest" in _cmd
                    or "cat " in _cmd or "grep " in _cmd):
                if _cicd.phase_state["perceive"]:
                    _cicd.phase_state["probe"] = True
            if _cicd.cmd_has(_cmd, "gh issue create") and "exit=0" in self.result_str:  # Cycles 36/55 anchoring; WS8.1: heredoc/quote-blind
                if not self._cicd_think_used:
                    self.log.warning("CICD: gh issue create without think — injecting reminder")
                    self.conversation_history.append({
                        "role": "user",
                        "content": "[SYSTEM: You filed an issue without using the think tool first. "
                        "Per MANDATORY THINK before DECIDE, you must call think() to evaluate "
                        "your candidate before filing. Use think now to validate this was the right choice.]",
                    })
                # Cycle 33: require --label in-progress on gh issue create.
                # Cycle 41: use flag-specific regex (body text can contain "in-progress").
                if not re.search(r"--label[= ]in-progress", _cmd):
                    self.log.warning("CICD: gh issue create without --label in-progress — injecting reminder")
                    _issue_num_m = re.search(r'issues/(\d+)|#(\d+)|Issue\s+#(\d+)', self.result_str)
                    _issue_num = next((g for g in _issue_num_m.groups() if g), "?") if _issue_num_m else "?"
                    self.conversation_history.append({
                        "role": "user",
                        "content": (
                            f"[SYSTEM: Issue #{_issue_num} was filed without `--label in-progress` "
                            "(and/or `--label cicd`). The reviewer's PRE-MERGE CHECK rejects PRs "
                            "whose linked issue lacks these labels. Fix now: "
                            f"`gh issue edit {_issue_num} --add-label in-progress --add-label cicd "
                            f"--add-label cicd-cycle-{_issue_num}`. "
                            "Do this before opening the PR.]"
                        ),
                    })
                _cicd.phase_state["decide"] = True
                self._cicd_think_used = False  # reset for next gate (verdict)
                # Extract issue number from gh output
                _issue_match = re.search(
                    r'issues/(\d+)|#(\d+)|Issue #(\d+)', self.result_str
                )
                if _issue_match:
                    _cicd.issue_number = next(
                        g for g in _issue_match.groups() if g
                    )
                    self.log.info("CICD phase: issue #%s filed",
                             _cicd.issue_number)
            if _cicd.cmd_has(_cmd, "git worktree add") and "exit=0" in self.result_str:  # WS8.1 (run 114: heredoc match here mis-captured the worktree path → guard-misdirection loop, turns 18-77)
                _cicd.phase_state["implement"] = True
                _wt_path_match = re.search(r'git\s+worktree\s+add\s+(?:-b\s+\S+\s+|--?[-\w]+\s+)*(\S+)', _cicd.strip_noncommand_text(_cmd))
                if _wt_path_match:
                    _cicd.worktree_path = _wt_path_match.group(1)
                    self.log.info("CICD: worktree path captured: %s", _cicd.worktree_path)
                _branch_match = re.search(r'-b\s+(\S+)', _cmd)
                if _branch_match:
                    _cicd.branch = _branch_match.group(1)
            # Cycle 82 capture-fix: when builder claims an EXISTING
            # issue via `gh issue view N` or `gh issue edit N`, also
            # capture the issue number so cycle 82's nudge can fire
            # even on inherited/existing-issue cycles. Run 189 hit
            # this gap: builder used `gh issue view 397`, which
            # never set `_cicd_issue_number`, so cycle 82's elif
            # condition `and _cicd_issue_number` was always False.
            if (not _cicd.issue_number) and "exit=0" in self.result_str:
                _existing_match = re.search(
                    r'^gh\s+issue\s+(?:view|edit|comment)\s+(\d+)\b',
                    _cmd.lstrip(),
                )
                if _existing_match:
                    _cicd.issue_number = _existing_match.group(1)
                    self.log.info("CICD phase: issue #%s claimed (existing)",
                             _cicd.issue_number)
            if _cicd.cmd_has(_cmd, "gh pr create") and "exit=0" in self.result_str:  # WS8.1
                # cycle 87 (run 192 false-positive): only recognise a
                # real PR URL (`pull/NNN`) as proof the PR was created.
                # The old `#(\d+)` fallback fired on `Closes #424`
                # appearing in the PR body string inside the result,
                # causing the tracker to record "PR #424 opened" when
                # no PR existed on GitHub — the branch hadn't even been
                # pushed yet.  `gh pr create` always returns the full
                # URL on success; if there is no URL in the result the
                # command silently failed and we should NOT set a PR
                # number (which would suppress the reviewer's "no open
                # PRs" early-exit and cause confusing downstream state).
                _pr_match = re.search(r'pull/(\d+)', self.result_str)
                if _pr_match:
                    _cicd.pr_number = _pr_match.group(1)
                    self.log.info("CICD phase: PR #%s opened",
                             _cicd.pr_number)
                # Guard: PR body must contain `Closes #N` trailer
                # so the linked issue auto-closes on merge. Missing
                # trailer is a recurring builder failure that
                # causes the reviewer to CLOSE the PR.
                if not re.search(r'Closes\s+#\d+', _cmd, re.IGNORECASE) and _cicd.pr_number:
                    self.log.warning("CICD: gh pr create without `Closes #N` trailer — injecting reminder")
                    self.conversation_history.append({
                        "role": "user",
                        "content": (
                            f"[SYSTEM: PR #{_cicd.pr_number} was created without a `Closes #<issue>` "
                            f"trailer in the body. The reviewer will CLOSE this PR for the missing "
                            f"trailer (per pre-merge check rule #4), wasting the cycle. Fix it NOW: "
                            f"`gh pr edit {_cicd.pr_number} --body \"<existing body>\\n\\nCloses #<issue>\"` "
                            f"— use the real issue number this cycle targets.]"
                        ),
                    })
            if (_cicd.cmd_has(_cmd, "gh pr review")  # WS8.1
                    and ("--request-changes" in _cmd
                         or "--approve" in _cmd
                         or "--comment" in _cmd)
                    and "exit=0" in self.result_str):
                if not self._has_reviewer_persisted:
                    self.log.info("Reviewer persistence detected (gh pr review) — completion signals now allowed")
                self._has_reviewer_persisted = True
            if ("reviews.md" in _cmd
                    and (">>" in _cmd or "tee -a" in _cmd or "tee --append" in _cmd)
                    and "exit=0" in self.result_str):
                if not self._has_reviewer_persisted:
                    self.log.info("Reviewer persistence detected (reviews.md append) — completion signals now allowed")
                self._has_reviewer_persisted = True
            if _cicd.cmd_has(_cmd, "gh pr review") and "--approve" in _cmd:  # WS8.1
                if not self._cicd_think_used:
                    self.log.warning("CICD: gh pr review --approve without think — injecting reminder")
                    self.conversation_history.append({
                        "role": "user",
                        "content": "[SYSTEM: You approved a PR without using the think tool first. "
                        "Per MANDATORY THINK before VERDICT, call think() to check: "
                        "did tests pass? was the metric measured? is the issue reference valid? "
                        "is the diff in-scope? Proceed with merge only after thinking.]",
                    })
                if ("exit=0" not in self.result_str
                        and "approve your own pull request" in self.result_str.lower()):
                    self.log.warning("CICD: self-approve failed — injecting skip-approval reminder")
                    self.conversation_history.append({
                        "role": "user",
                        "content": "[SYSTEM: You cannot approve your own PR (same-account setup). "
                        "SKIP the approval step entirely. Go directly to: "
                        "`gh pr ready <N>` (separate command), then "
                        "`gh pr merge <N> --squash` (separate command).]",
                    })
            if _cicd.cmd_has(_cmd, "gh pr ready") and "exit=0" in self.result_str:  # WS8.1
                self._cicd_pr_ready_called = True
                self.log.info("CICD: gh pr ready called")
            if _cicd.cmd_has(_cmd, "gh issue view") and ("exit=0" in self.result_str or "--json" in _cmd):  # WS8.1
                # Cycle 25: validate state + labels in the gh issue view result.
                # Calling gh issue view is necessary but not sufficient — the
                # issue must be OPEN and carry cicd + in-progress (or cicd-cycle-*)
                # labels. If not, leave _cicd_issue_view_called=False so the
                # pre-execute block (cycle 24) continues to block gh pr merge.
                _premerge_ok = False
                try:
                    import json as _json_mod
                    _json_body = self.result_str.split("exit=0", 1)[-1].strip()
                    _json_start = _json_body.find("{")
                    if _json_start >= 0:
                        _issue_data = _json_mod.loads(_json_body[_json_start:])
                        _lnames = [l.get("name", "") for l in _issue_data.get("labels", [])]
                        _istate = _issue_data.get("state", "")
                        _has_valid_labels = any(
                            l == "cicd" or l.startswith("in-progress") or l.startswith("cicd-cycle-")
                            for l in _lnames
                        )
                        if "state" not in _issue_data:
                            # state field absent → not a PRE-MERGE CHECK call
                            # (e.g. `gh issue view N --json body`); skip silently.
                            pass
                        elif _istate.upper() == "OPEN" and _has_valid_labels:
                            _premerge_ok = True
                        else:
                            self.log.warning(
                                "CICD: PRE-MERGE CHECK FAILED — state=%s labels=%s "
                                "(need OPEN + cicd/in-progress[-bot-N] label)",
                                _istate, _lnames,
                            )
                            self.conversation_history.append({
                                "role": "user",
                                "content": (
                                    "[SYSTEM: PRE-MERGE CHECK FAILED. gh issue view returned "
                                    f"state={_istate!r}, labels={_lnames}. "
                                    "The issue must be OPEN with labels `cicd` + `in-progress` "
                                    "or `in-progress-bot-N` (or `cicd-cycle-NNN`). Add missing labels with "
                                    "`gh issue edit <N> --add-label in-progress --add-label cicd` "
                                    "then re-run gh issue view before retrying the merge.]"
                                ),
                            })
                except Exception:
                    _premerge_ok = True  # parse error: don't block on format change
                if _premerge_ok:
                    self._cicd_issue_view_called = True
                    self.log.info("CICD: gh issue view called (PRE-MERGE CHECK satisfied)")
            # Match `gh pr merge` as an actual top-level invocation — not inside
            # heredoc/cat content where the string may appear as documentation.
            # Matches at line start or after a shell separator (&&, ;, |, ||, \n).
            if re.search(r"(?:^|&&\s*|;\s*|\|\|?\s*|\n\s*)gh\s+pr\s+merge\b", _cmd):
                # Guard: PRE-MERGE CHECK — must view linked issue first (reviewer.md §4)
                if not self._cicd_issue_view_called:
                    self.log.warning("CICD: gh pr merge without PRE-MERGE CHECK — injecting reminder")
                    self.conversation_history.append({
                        "role": "user",
                        "content": "[SYSTEM: PRE-MERGE CHECK SKIPPED. You must complete these steps "
                        "IN ORDER before retrying gh pr merge: "
                        "(1) `gh issue view <N> --json state,labels,title,createdAt` — verify OPEN + cicd/in-progress labels; "
                        "(2) `think(...)` — confirm tests passed, metric verified, issue valid; "
                        "(3) `gh pr ready <N>` — promote draft to ready; "
                        "(4) THEN `gh pr merge <N> --squash` as a separate command. "
                        "Do NOT skip or combine steps. Do NOT add --delete-branch.]",
                    })
                # Guard: must use --squash (NOT --delete-branch — builder worktree holds branch)
                if "--squash" not in _cmd:
                    self.log.warning("CICD: gh pr merge without --squash — injecting reminder")
                    self.conversation_history.append({
                        "role": "user",
                        "content": "[SYSTEM: You MUST use `gh pr merge --squash`. "
                        "Never use --merge or --rebase. Never use --delete-branch. Retry with --squash only.]",
                    })
                # Guard: must call `gh pr ready` first (draft PRs)
                if not self._cicd_pr_ready_called and "exit=0" not in self.result_str:
                    if "still a draft" in self.result_str.lower():
                        self.log.warning("CICD: gh pr merge on draft — need gh pr ready first")
                        self.conversation_history.append({
                            "role": "user",
                            "content": "[SYSTEM: The PR is still a draft. You must run "
                            "`gh pr ready <N>` FIRST, then run `gh pr merge <N> --squash` "
                            "as a SEPARATE command. Do NOT chain them. Do NOT add --delete-branch.]",
                        })
                if not self._cicd_think_used and "exit=0" not in self.result_str:
                    self.log.warning("CICD: gh pr merge without think — injecting reminder")
                    self.conversation_history.append({
                        "role": "user",
                        "content": "[SYSTEM: You attempted to merge without using the think tool first. "
                        "Per MANDATORY THINK before VERDICT, you must call think() to verify "
                        "tests passed, metric was measured, and issue reference is valid.]",
                    })
                # Cycle 34: detect || echo / || true which mask merge errors.
                if re.search(r"gh\s+pr\s+merge\b.*\|\|\s*(echo|true|exit\s+0)", _cmd):
                    self.log.warning("CICD: gh pr merge with || suppressor — injecting reminder")
                    self.conversation_history.append({
                        "role": "user",
                        "content": (
                            "[SYSTEM: You used `|| echo` (or `|| true`) with `gh pr merge`. "
                            "This masks real errors — if the merge genuinely failed (draft, "
                            "conflict), exit=0 would be reported falsely. NEVER chain "
                            "`gh pr merge` with `||`. If the only error was local branch "
                            "deletion (worktree lock), that is benign — the PR is already "
                            "merged server-side. Verify with `gh pr view <N> --json state` "
                            "and clean up locally with `git branch -D <branch>` if needed.]"
                        ),
                    })
                if "exit=0" in self.result_str:
                    _cicd.phase_state["track"] = True
            # Detect plan file writes via exec_command (cat/heredoc)
            if ("improvements/" in _cmd
                    and ("cat >" in _cmd or "cat >>" in _cmd
                         or "echo" in _cmd or "tee" in _cmd)
                    and "exit=0" in self.result_str):
                _cicd.phase_state["plan"] = True
                self.log.info("CICD phase: plan written via exec_command")
            # Record cycle timestamp automatically
            if _tracker:
                try:
                    with open(_state_path("current-state.json"), encoding="utf-8", errors="replace") as _sf:
                        self._cycle = json.load(_sf).get("cycle", 0)
                except Exception:
                    self._cycle = 0
                try:
                    timestamp = _tracker.auto_record(agent_id="e1")
                    self.log.debug("Cycle %s recorded: %s", self._cycle, timestamp)
                except Exception as e:
                    self.log.error("Failed to record cycle timestamp: %s", e)

    def _result_loop_checks(self):
        """The search slow-loop, the repeated-result and the semantic result-loop detectors.
        Returns the run's result when overtime meets a repeated result."""
        self.log.debug("TOOL RESULT [%s]: %s", self.func_name, self.result_str)

        # Search/find slow-loop detector.
        # The dedup cache catches close-range repeats (within
        # _DEDUP_WINDOW calls) but the model can evade it by
        # inserting other tool calls between retries.  Count real
        # (non-deduped) dispatches of the same (func, args) and
        # fire a forced-think when the count hits the threshold.
        if self.func_name in ("search_files", "find_symbol") and self._dedup_sig is not None:
            self._search_sig_counts[self._dedup_sig] = self._search_sig_counts.get(self._dedup_sig, 0) + 1
            _search_n = self._search_sig_counts[self._dedup_sig]
            if _search_n >= self._SEARCH_REPEAT_THRESHOLD:
                _s_think_prompt = (
                    f"MANDATORY REFLECTION: I have dispatched {self.func_name} with "
                    f"the same arguments {_search_n} times this session "
                    f"(the dedup cache also blocked earlier repeats).\n\n"
                    f"Arguments: {json.dumps(self.func_args)}\n"
                    f"Last result: {self.result_str[:200]}\n\n"
                    f"I MUST answer:\n"
                    f"1. Why is this search not finding what I need?\n"
                    f"2. Have I already confirmed the answer is not here — "
                    f"is this a dead end?\n"
                    f"3. What DIFFERENT approach (different tool, different "
                    f"path, or direct file read) would actually make progress?\n"
                    f"4. Should I accept this target does not exist and move on?"
                )
                self.log.warning(
                    "Search slow-loop: %s with same args dispatched %d times — forcing think",
                    self.func_name, _search_n,
                )
                _emit("on_forced_think", self.func_name, _search_n)
                if "think" in MAP_FN:
                    _s_think_result = MAP_FN["think"](prompt=_s_think_prompt)
                    _s_think_id = f"forced_think_search_{self.turn}_{_search_n}"
                    self.conversation_history.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": _s_think_id,
                            "type": "function",
                            "function": {
                                "name": "think",
                                "arguments": json.dumps({"prompt": _s_think_prompt})
                            }
                        }]
                    })
                    self.conversation_history.append({
                        "role": "tool",
                        "tool_call_id": _s_think_id,
                        "name": "think",
                        "content": str(_s_think_result),
                    })
                # Reset after intervention — allow one fresh attempt.
                self._search_sig_counts[self._dedup_sig] = 0

        # Track repeated tool results (errors and identical results)
        _result_sig = (self.func_name, self.result_str[:100])
        self._recent_tool_errors.append(_result_sig)
        self._result_repeats = sum(1 for e in self._recent_tool_errors if e == _result_sig)

        # Past turn limit + same tool result 3 times = end cycle
        if self.turn > _MAX_TURNS and self._result_repeats >= 3:
            self.log.warning("Overtime + repeated tool result (%s x%d) — ending cycle",
                        self.func_name, self._result_repeats)
            _emit("on_overtime", "repeated_result")
            return "done"

        # Semantic result-loop detection: same tool returning same
        # result despite different arguments.
        # Skip empty or exit-only results (e.g. `echo >> file`,
        # `git add`, `mkdir`) — they all hash identically and would
        # false-positive after 3 consecutive silent commands.
        # _EXIT_ONLY_RE is compiled at module scope.
        _result_is_noise = (
            not self.result_str.strip()
            or bool(_EXIT_ONLY_RE.match(self.result_str.strip()))
        )
        if not _result_is_noise:
            _res_hash = hashlib.md5(
                self.result_str[:200].encode()
            ).hexdigest()[:8]
            self._tool_result_key = (self.func_name, _res_hash)
            self._recent_tool_results.append(self._tool_result_key)
            if len(self._recent_tool_results) > self._RESULT_LOOP_WINDOW:
                self._recent_tool_results.pop(0)
        _same_result_count = sum(
            1 for k in self._recent_tool_results[-6:]
            if k == self._tool_result_key
        ) if not _result_is_noise else 0
        if _same_result_count >= self._RESULT_LOOP_THRESHOLD:
            self.log.warning(
                "Semantic result loop: %s returned same result %d times",
                self.func_name, _same_result_count)
            if self.func_name == "exec_command":
                self._cmd_preview = ""
                if isinstance(self.func_args, dict):
                    self._cmd_preview = str(self.func_args.get("command", ""))[:120]
                self._hint = (
                    f"SYSTEM: exec_command has returned the same output "
                    f"{_same_result_count} times (e.g. `{self._cmd_preview}`). "
                    f"If you are re-verifying a check (tests, lint, gh status), "
                    f"the prior output is authoritative — do not re-run it. "
                    f"Move to the next step (commit, push, open PR) or take a "
                    f"materially different action."
                )
            else:
                self._hint = (
                    f"SYSTEM: The {self.func_name} tool has returned the "
                    f"same result {_same_result_count} times despite "
                    f"different arguments. Your approach is not working. "
                    f"Either try a completely different method, or accept "
                    f"the current state and move on to the next step."
                )
            # WS10.e rung 2: escalate repeat interventions to a
            # system-invoked think (same ladder as the batch-loop
            # site above).
            self._loop_interventions += 1
            if self._loop_interventions >= 2 and "think" in MAP_FN:
                try:
                    self._fk = MAP_FN["think"](prompt=(
                        f"MANDATORY REFLECTION: {self.func_name} has "
                        f"returned the same result "
                        f"{_same_result_count}x and a previous "
                        f"redirect this cycle was ignored. Result "
                        f"tail: {self.result_str[:300]}. Answer: (1) "
                        f"what is this result actually telling "
                        f"me? (2) ONE materially different next "
                        f"action; (3) if none, mark the step "
                        f"failed and move to CONSOLIDATE/PERSIST."))
                    self._hint += ("\n\nFORCED REFLECTION (system-"
                              "invoked think):\n" + str(self._fk)[:1500])
                    telemetry.record_patch_event(
                        "loop_forced_think", kind="fired")
                except Exception as _fe:
                    self.log.warning("forced think failed: %s", _fe)
                # Stall-side advisor escalation: the model repeated a
                # failing action AND ignored a prior redirect — the
                # cognitively-stuck mode the gate-block trigger never
                # sees (a stuck model never cleanly declares done).
                # Offer the heavyweight advisor alongside the forced
                # self-think; once-per-cycle, budget- and config-gated.
                self._adv_nudge = self._maybe_advisor_nudge(source="stall",
                                                  context=self._hint)
                if self._adv_nudge:
                    self._hint += "\n\n" + self._adv_nudge
            self.conversation_history.append({
                "role": "user",
                "content": self._hint,
            })

    def _after_result(self):
        """Repeated errors: the forced think, or the hard bail that ends this turn's batch
        (_BREAK). Then the on_tool_result event."""
        if self.result_str.startswith("Error"):
            consecutive = self._result_repeats
            if consecutive >= self._REPEAT_THRESHOLD * 2:
                # Model is stuck even after forced thinks — skip this step
                self.log.warning("Hard bail: %s failed %d times — skipping",
                            self.func_name, consecutive)
                _emit("on_tool_skip", self.func_name, consecutive)
                self.conversation_history.append({
                    "role": "user",
                    "content": (
                        f"SYSTEM: The {self.func_name} tool has failed {consecutive} times "
                        f"with the same error. This step is being SKIPPED. "
                        f"Use exec_command with cat/heredoc to write files instead, "
                        f"or move on to the next step."
                    ),
                })
                self._recent_tool_errors[:] = [e for e in self._recent_tool_errors if e[0] != self.func_name]
                return _BREAK
            elif consecutive >= self._REPEAT_THRESHOLD:
                # Force a think call to break the loop
                think_prompt = (
                    f"MANDATORY REFLECTION: I have called {self.func_name} "
                    f"{consecutive} times and gotten the same error each time.\n\n"
                    f"The error is: {self.result_str[:300]}\n\n"
                    f"My last arguments were: {json.dumps(self.func_args) if isinstance(self.func_args, dict) else str(self.func_args)}\n\n"
                    f"I MUST answer these questions:\n"
                    f"1. What exactly is the error telling me?\n"
                    f"2. What parameter am I missing or getting wrong?\n"
                    f"3. What is a DIFFERENT way to accomplish my goal "
                    f"without repeating the same call?\n"
                    f"4. Should I just skip this step and move on to "
                    f"CONSOLIDATE and PERSIST?"
                )
                self.log.warning("Loop detected: %s x%d — forcing think",
                            self.func_name, consecutive)
                _emit("on_forced_think", self.func_name, consecutive)
                if "think" in MAP_FN:
                    think_result = MAP_FN["think"](prompt=think_prompt)
                    # Inject as assistant thought + tool response
                    think_id = f"forced_think_{self.turn}_{consecutive}"
                    self.conversation_history.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": think_id,
                            "type": "function",
                            "function": {
                                "name": "think",
                                "arguments": json.dumps({"prompt": think_prompt})
                            }
                        }]
                    })
                    self.conversation_history.append({
                        "role": "tool",
                        "tool_call_id": think_id,
                        "name": "think",
                        "content": str(think_result),
                    })
                    self.log.info("FORCED THINK RESULT: %s", think_result)
        else:
            # Successful call — reset tracker for this tool
            self._recent_tool_errors[:] = [e for e in self._recent_tool_errors if e[0] != self.func_name]
        _emit("on_tool_result", self.func_name, self.func_args, self.result_str,
              self.result_str.startswith("Error"))

    def _end_turn(self):
        # If ALL tool calls in this turn were garbled, retry the turn once
        # by removing the assistant message + error responses and re-requesting.
        if self._garbled_count > 0 and self._garbled_count >= len(self.tool_calls):
            _garble_retries = getattr(run_agent_single, '_garble_retries', 0)
            if _garble_retries < 1:
                self.log.warning("All %d tool call(s) garbled — retrying turn %d", self._garbled_count, self.turn)
                # Remove the assistant message and all error tool responses from this turn
                # The assistant message is right before the tool responses
                _to_remove = 1 + self._garbled_count  # assistant + tool error responses
                self.conversation_history[-_to_remove:] = []
                run_agent_single._garble_retries = _garble_retries + 1
                return _NEXT  # retry the same turn
            else:
                self.log.warning("Garble retry exhausted — proceeding with error responses")
                run_agent_single._garble_retries = 0
        else:
            run_agent_single._garble_retries = 0

        _apply_tool_loads(self._session_tools, self.log)   # one list change per turn, before the checkpoint
        # Save checkpoint after each turn so -c can resume from here
        if self._async_summarizer:
            self._async_summarizer.harvest(self.summary_state)
        _save_checkpoint(self.conversation_history, self.summary_state, self.turn, self.initial_files)
        if _RESTART["requested"]:
            # The restart tool answered this turn; its result is in the history and the
            # checkpoint. The session loop ends on the flag and main() re-executes.
            self.log.info("restart requested by %s (%s): ending the turn for the re-exec",
                     _RESTART["by"], _RESTART["reason"] or "no reason")
            return "restart"

        # T5.17 — rolling per-session patch-effectiveness summary. Only emits
        # when at least one counter is non-zero (no fires = no log noise).
        # Cumulative within the session; OTEL metrics carry the per-event
        # detail for cross-session aggregation on the telemetry server.
        if any(v > 0 for v in self._patch_telemetry.values()):
            self._summary = " | ".join(
                f"{k}={v}" for k, v in self._patch_telemetry.items() if v > 0
            )
            self.log.info("[patch-telemetry] %s", self._summary)

    _STEPS = (_begin_turn, _request, _stream, _record_response, _text_only, _tool_guard, _dispatch, _end_turn)


def _run_agent_single_impl(conversation_history: list, summary_state: dict, initial_files,
                     log: logging.Logger,
                     temperature=_cfg.DEFAULTS["generation"]["temperature"],
                     top_p=_cfg.DEFAULTS["generation"]["top_p"],
                     top_k=_cfg.DEFAULTS["generation"]["top_k"],
                     presence_penalty=_cfg.DEFAULTS["generation"]["presence_penalty"],
                     max_tokens=_cfg.DEFAULTS["context"]["max_tokens"],
                     ctx_size=_cfg.DEFAULTS["context"]["ctx_size"],
                     start_turn=0, async_summarizer=None):
    """Run the agentic loop with turn limits and wind-down."""
    return _Run(conversation_history, summary_state, initial_files, log, temperature, top_p,
                top_k, presence_penalty, max_tokens, ctx_size, start_turn, async_summarizer).run()


# Make the cycle_status wrapper (run_agent_single, defined above with a bare
# ``*args, **kwargs`` shim) signature-transparent: point ``__wrapped__`` at the
# real impl so ``inspect.signature(run_agent_single)`` and help() report the
# actual parameters and their _DEFAULT_CONFIG-derived defaults, not (*args,
# **kwargs). Without this the CICD-0025 defaults-sync regression guard can't see
# the signature at all (KeyError), and introspecting callers get nothing useful.
run_agent_single.__wrapped__ = _run_agent_single_impl


# ── --job: a declarative job file with a runner-verified acceptance gate ───────────────
# Sugar over existing flags PLUS the one thing flags cannot express: a check the RUNNER
# executes after the agent exits, whose status is the verdict.
#
# The point is that the agent is the last thing that should be asked whether the agent
# succeeded. A run can exit 0, report success fluently, and have produced nothing; the
# acceptance command is re-run here, outside the model's influence, and its exit status
# decides. Whatever the run claimed in its result block is advisory.
#
# See docs/job-contract.md. Fields map onto flags one-to-one except `acceptance` and
# `env_allow`, which are new, so --job changes no execution model.

_JOB_ACCEPTANCE = None      # command string, re-run after the agent exits
_JOB_PATH = None            # for messages
_JOB_ACCEPTANCE_TIMEOUT = 300   # seconds; `acceptance_timeout_sec` in the job file overrides it
# The file's schema and the helpers live in job.py; the three runtime values below are
# set by main() from the file and read by _acceptance_verdict after the run.
_JOB_KNOWN_KEYS = _job.KNOWN_KEYS
_load_job_spec, _normalize_job_lists, _job_prompt = _job.load_spec, _job.normalize_lists, _job.prompt
_apply_env_allow, _acceptance_parses = _job.apply_env_allow, _job.acceptance_parses
_save_acceptance_output, _run_acceptance = _job.save_acceptance_output, _job.run_acceptance
_refresh_result_exit = _job.refresh_result_exit


def _acceptance_verdict():
    """Run the gate and set the process exit accordingly. Called once, after the run ends.

    A gate that cannot be executed is NOT a pass. A missing binary, an unparseable command
    or a timeout all fail closed, because "the check did not run" and "the check passed"
    must never render the same to whoever reads the exit status.
    """
    if not _JOB_ACCEPTANCE:
        return
    ok, code, out = _run_acceptance(_JOB_ACCEPTANCE, timeout=_JOB_ACCEPTANCE_TIMEOUT)
    # The LAST non-empty line: pytest and most tools print a banner first and the verdict last.
    lines = [ln for ln in out.splitlines() if ln.strip()]
    head = lines[-1].strip()[:200] if lines else "(no output)"
    saved = _save_acceptance_output(_JOB_ACCEPTANCE, code, out)
    where = f" [full output: {saved}]" if saved else ""
    if ok:
        print(f"AGENT-ACCEPTANCE: pass — {head}", file=sys.stderr, flush=True)
        return
    print(f"AGENT-ACCEPTANCE: FAIL (exit {code}) — {head}{where}", file=sys.stderr, flush=True)
    # A hard stop already recorded (deadline, contract, context, backend ...) is the CAUSE and
    # keeps the exit code; the gate's failure is appended to its detail. Exit 16 means "the run
    # finished and the artifact is wrong" — a supervisor branching on it would retry a run that
    # actually needs a larger timebox or a live backend. This is the precedence rule
    # _classify_auto_result already applies: a hard stop beats the generic map.
    prior = _LAST_EXIT if (_LAST_EXIT and _LAST_EXIT.get("code") not in (None, EXIT_OK)) else None
    if prior:
        _set_exit(prior["code"],
                  f"{prior.get('detail', '')} | acceptance also FAILED (exit {code}): {head}")
        return
    _set_exit(EXIT_ACCEPTANCE,
              f"runner re-ran the acceptance command and it failed "
              f"(exit {code}): {head}")


def main():
    """Main entry point."""
    # Issue #405 — bedrock credential store CLI. Handled before argparse
    # so the ``bedrock`` subcommand group has its own parser without
    # disturbing the existing positional ``prompt`` slot.
    try:
        import cli_bedrock
        rc = cli_bedrock.maybe_dispatch(sys.argv[1:])
        if rc is not None:
            sys.exit(rc)
    except SystemExit:
        raise
    except Exception:  # pragma: no cover - defensive; never block normal CLI
        pass
    import argparse
    parser = argparse.ArgumentParser(description="Agent with file tools")
    parser.add_argument("-a", "--auto", action="store_true",
                        help="Automation mode: run prompt and exit (no interactive loop)")
    parser.add_argument("-c", "--continue", dest="continue_mode", action="store_true",
                        help="Continue from last checkpoint (resume a crashed cycle)")
    parser.add_argument("-r", "--repeat", type=int, nargs="?", const=0, default=None,
                        help="Repeat N times (fresh each run). 0 or omit = indefinite. Implies -a.")
    parser.add_argument("--setup", action="store_true", dest="run_setup",
                        help="Run the setup wizard at startup even if a config exists "
                             "(interactive TTY sessions only), then continue into the session.")
    parser.add_argument("--nudge", action="store_true",
                        help="Auto-nudge the model when it returns a text-only response.")
    parser.add_argument("--verbose", action="store_true",
                        help="Start the session with full (uncompacted) tool output. "
                             "Toggle in-session with /verbose.")
    parser.add_argument("--no-tui", dest="no_tui", action="store_true",
                        help="Disable the prompt_toolkit TUI even in interactive mode "
                             "(use a plain input() prompt). The TUI is on by default when "
                             "running interactively and falls back to plain input() automatically "
                             "if `prompt_toolkit` isn't installed.")
    parser.add_argument("--backend-main", dest="backend_main",
                        choices=["llamacpp", "bedrock", "foundry", "aicommons"], default=None,
                        help="Override the main backend kind (see plan/bedrock-integration.md).")
    parser.add_argument("--result-file", dest="result_file",
                        help="Write the final assistant response to this file (for subagent use).")
    parser.add_argument("--backend-summary", dest="backend_summary",
                        choices=["llamacpp", "bedrock", "foundry", "aicommons"], default=None,
                        help="Override the summary backend kind.")
    parser.add_argument("--model", dest="model", default=None,
                        help="Override the main backend's model (e.g. claude-v5.5-sonnet).")
    parser.add_argument("--effort", dest="effort", default=None,
                        choices=["low", "medium", "high", "xhigh", "max"],
                        help="Main backend reasoning effort (aicommons; Claude 5.x). "
                             "Default: high for main.")
    parser.add_argument("-cc", dest="cc", nargs="?", const="127.0.0.1:8788", default=None,
                        metavar="HOST:PORT",
                        help="Launch an Anthropic-compatible gateway endpoint for Claude "
                             "Code (default 127.0.0.1:8788), forwarding to the configured "
                             "main backend, then exit. Point Claude Code at it with "
                             "ANTHROPIC_BASE_URL=http://HOST:PORT.")
    parser.add_argument("--role", choices=["builder", "reviewer", "creature"],
                        default=None,
                        help="Explicit session role (WS1). Overrides the "
                             "legacy prompt string-matching: builder/reviewer "
                             "enable CICD guards (reviewer may merge, builder "
                             "may not); creature disables CICD-specific guards.")
    # Headless hardening (plan/headless-hardening.md), tranche 1: F5 / F8a flags. F6's exit
    # codes need no flag — they are additive in -a mode. Interactive behavior is untouched.
    parser.add_argument("--no-realpath-cwd", dest="no_realpath_cwd", action="store_true",
                        help="Keep a symlinked working directory as given instead of canonicalizing "
                             "it at start (F5). Read from argv at import; listed here so argparse "
                             "accepts it.")
    parser.add_argument("--temperature", type=float, default=None,
                        help="Per-run sampling temperature (overrides generation.temperature).")
    parser.add_argument("--top-p", dest="top_p", type=float, default=None,
                        help="Per-run nucleus sampling (overrides generation.top_p).")
    parser.add_argument("--seed", type=int, default=None,
                        help="Per-run sampling seed (overrides generation.seed) for reproducible "
                             "runs on backends that support it; others warn once and ignore it.")
    parser.add_argument("--result-contract", dest="result_contract", action="store_true",
                        help="Require the final message to end with a fenced JSON result block "
                             "({\"contract\": 1, \"status\": ..., \"summary\": ...}); with --result-file "
                             "the file receives the validated JSON only. Missing/invalid at exit → "
                             "correction (bounded), then a synthesized failed record and exit 11. "
                             "Schema: built-in unless --result-schema is given.")
    parser.add_argument("--result-schema", dest="result_schema", default=None, metavar="SCHEMA.json",
                        help="JSON schema file for --result-contract (implies it). A separate flag on "
                             "purpose: an optional value would swallow the positional prompt.")
    parser.add_argument("--goal", default=None,
                        help="The run's stated goal (overrides cycle.goal); injected at goal-anchor fractions (F4).")
    parser.add_argument("--deliverable", action="append", default=None, metavar="PATH_OR_GLOB",
                        help="A named deliverable (repeatable; overrides cycle.deliverables). A final "
                             "'done' while one is absent draws a correction (F4).")
    parser.add_argument("--deadline", type=float, default=None, metavar="SECONDS",
                        help="Wall-clock budget for the run (overrides cycle.deadline_s). The agent "
                             "is warned at 60/80/92%% and forced to its final result at 100%%, "
                             "exiting 10 — it beats the supervisor's kill instead of racing it.")
    parser.add_argument("--job", default=None, metavar="JOB.yaml|JOB.json",
                        help="Declarative job file: goal/context/constraints/deliverable/"
                             "acceptance/timebox_sec/env_allow. Sugar over the flags above PLUS "
                             "a runner-verified gate — after the run exits, the RUNNER re-runs "
                             "`acceptance` and its status is the verdict (exit 16 on failure), "
                             "whatever the run reported. See docs/job-contract.md.")
    parser.add_argument("prompt", nargs="*", help="Initial prompt")
    args = parser.parse_args()

    # --job: fold the file into the flags it is sugar for, then keep the two things flags
    # cannot express (the gate, the env allowlist). Explicit flags WIN over the file, so a
    # caller can override one field of a shared job without editing it.
    if args.job:
        global _JOB_ACCEPTANCE, _JOB_PATH, _JOB_ACCEPTANCE_TIMEOUT
        try:
            _spec = _load_job_spec(args.job)
        except (OSError, ValueError, json.JSONDecodeError) as _je:
            _exit_now(EXIT_CONFIG, f"--job unreadable: {_je}")
        if not isinstance(_spec, dict):
            _exit_now(EXIT_CONFIG, f"--job must contain a mapping, got {type(_spec).__name__}")
        _JOB_PATH = args.job
        # A JOB IS A RUN THAT ENDS. Without -a the folded-in prompt runs and the process then
        # drops to the interactive prompt and waits on stdin: the runner never re-runs the
        # acceptance and the timebox never matters (measured: a two-file job sat at "You:"
        # until an outer timeout killed it, exit 124, acceptance never checked). --job implies
        # --auto; a caller who wants the prompt afterwards is not running a job.
        if not args.auto:
            args.auto = True
            print("[job] --job implies -a/--auto: a job ends when the run ends", file=sys.stderr, flush=True)
        _unknown = sorted(str(k) for k in _spec if str(k) not in _JOB_KNOWN_KEYS)
        if _unknown:
            _exit_now(EXIT_CONFIG,
                      f"--job {_JOB_PATH}: unknown key(s) {', '.join(_unknown)}; known keys are "
                      f"{', '.join(sorted(_JOB_KNOWN_KEYS))}. A misspelled `acceptance` is a "
                      f"gate that never arms, so this is refused rather than ignored.")
        if not args.goal and _spec.get("goal"):
            args.goal = str(_spec["goal"]).strip()
        if not args.deliverable and _spec.get("deliverable"):
            args.deliverable = [str(d) for d in _spec["deliverable"]]
        # `is not None`, not truthiness: timebox_sec: 0 used to be dropped silently, which armed
        # nothing while the file said otherwise. It now reaches the same refusal as --deadline 0.
        if args.deadline is None and _spec.get("timebox_sec") is not None:
            try:
                args.deadline = float(_spec["timebox_sec"])
            except (TypeError, ValueError):
                _exit_now(EXIT_CONFIG,
                          f"--job timebox_sec must be a number, got {_spec['timebox_sec']!r}")
        if _spec.get("acceptance_timeout_sec") is not None:
            try:
                _JOB_ACCEPTANCE_TIMEOUT = float(_spec["acceptance_timeout_sec"])
            except (TypeError, ValueError):
                _exit_now(EXIT_CONFIG, f"--job acceptance_timeout_sec must be a number, got "
                                       f"{_spec['acceptance_timeout_sec']!r}")
            if _JOB_ACCEPTANCE_TIMEOUT <= 0:
                _exit_now(EXIT_CONFIG, "--job acceptance_timeout_sec must be positive")
        # result_contract: true arms the built-in schema, a string names a schema file, false
        # is explicit off. The flags win, as they do for every other field.
        _rc = _spec.get("result_contract")
        if _rc is not None and not args.result_schema and not args.result_contract:
            if isinstance(_rc, str) and _rc.strip():
                args.result_schema = _rc.strip()
            elif _rc is True:
                args.result_contract = True
            elif _rc is not False:
                _exit_now(EXIT_CONFIG, f"--job result_contract must be true, false or a schema "
                                       f"path, got {_rc!r}")
        if _spec.get("refusal_max_turns") is not None:
            try:
                _config["cycle"]["refusal_max_turns"] = int(_spec["refusal_max_turns"])
            except (TypeError, ValueError):
                _exit_now(EXIT_CONFIG, f"--job refusal_max_turns must be an integer, got "
                                       f"{_spec['refusal_max_turns']!r}")
        _JOB_ACCEPTANCE = _spec.get("acceptance") or None
        # An acceptance command that cannot even be PARSED is a dead gate for the whole run,
        # and a dead gate looks exactly like a passing one. Refuse at launch instead.
        if _JOB_ACCEPTANCE:
            _pok, _pmsg = _acceptance_parses(_JOB_ACCEPTANCE)
            if not _pok:
                _exit_now(EXIT_CONFIG, f"--job acceptance is not a parseable command: {_pmsg}")
        # Write the RESOLVED values back before rendering the prompt. The precedence above
        # only reached the flags; the prompt was still rendered from the raw file, so
        # `--job j.yaml --goal X` told the run X through the anchor and the file's goal
        # through the opening prompt — two live objectives, no way for the run to know
        # which one it will be judged against. An override must reach every surface it
        # shows on, or it is not an override.
        if args.goal:
            _spec["goal"] = args.goal
        if args.deliverable:
            _spec["deliverable"] = list(args.deliverable)
        _removed = _apply_env_allow(_spec.get("env_allow"))
        if _removed is not None:
            print(f"[job] env_allow: kept {len(_spec.get('env_allow') or [])} declared "
                  f"variable(s), removed {len(_removed)}", file=sys.stderr, flush=True)
        _jp = _job_prompt(_spec)
        if _jp:
            args.prompt = [_jp] + list(args.prompt or [])

    # WS1: explicit role beats prompt string-matching in run_agent_single.
    if getattr(args, "role", None):
        global _explicit_role
        _explicit_role = args.role

    # Apply backend-kind overrides before any backend-dependent startup logic.
    _apply_backend_overrides(args.backend_main, args.backend_summary)
    _apply_model_effort_overrides(getattr(args, "model", None), getattr(args, "effort", None))
    _cfg.export_subagent_args(args)
    _cfg.export_backend_overrides(args)
    # F8a: generation overrides (flag > config > default), applied after backend overrides so
    # the seed support warning names the backend that will actually run.
    _cfg.apply_generation_overrides(args)
    # F1: --deadline wins over cycle.deadline_s.
    if getattr(args, "deadline", None) is not None:
        if args.deadline <= 0:
            print("--deadline must be a positive number of seconds", file=sys.stderr)
            _exit_now(EXIT_CONFIG, "--deadline must be positive")
        _config["cycle"]["deadline_s"] = float(args.deadline)
    # F2: --result-contract [schema] wins over cycle.result_contract. The schema is loaded HERE,
    # at launch: unreadable → refuse to start (exit 14), never run-then-surprise.
    if getattr(args, "result_schema", None):
        _config["cycle"]["result_contract"] = args.result_schema
    elif getattr(args, "result_contract", False):
        _config["cycle"]["result_contract"] = True
    # F4: --goal / --deliverable win over cycle.goal / cycle.deliverables.
    if getattr(args, "goal", None):
        _config["cycle"]["goal"] = str(args.goal)
    if getattr(args, "deliverable", None):
        _config["cycle"]["deliverables"] = list(args.deliverable)
    if _config["cycle"].get("result_contract"):
        global _RESULT_CONTRACT_SCHEMA
        try:
            _RESULT_CONTRACT_SCHEMA = _load_result_contract_schema(_config["cycle"]["result_contract"])
        except Exception as _rce:  # noqa: BLE001
            print(f"⚠ result contract schema unreadable: {_rce}", file=sys.stderr)
            _exit_now(EXIT_CONFIG, f"result contract schema unreadable: {_rce}")

    # --setup: force the wizard on session start (interactive path only;
    # the TTY/auto guards inside _maybe_first_run_wizard still apply).
    if getattr(args, "run_setup", False):
        global _FORCE_SETUP_WIZARD
        _FORCE_SETUP_WIZARD = True

    # -cc — stand up the Claude Code gateway against the configured main
    # backend and block here. Backend overrides above are already applied, so
    # _main_backend is the final, fully-configured backend.
    if args.cc is not None:
        host, sep, port = args.cc.partition(":")
        host = host or "127.0.0.1"
        try:
            port_num = int(port) if sep else 8788
        except ValueError:
            print(f"Invalid -cc port: {port!r} (expected HOST:PORT)", file=sys.stderr)
            sys.exit(2)
        if getattr(_main_backend, "kind", None) == "aicommons":
            # The gateway already speaks /v1/messages; translating Anthropic -> OpenAI ->
            # Anthropic here would only lose thinking, cache control and tool ids.
            print("Note: AI Commons serves the Anthropic Messages API itself. Claude Code can use it "
                  f"directly:\n  ANTHROPIC_BASE_URL={_main_backend.base_url} "
                  "ANTHROPIC_API_KEY=<your key> claude\nStarting the -cc gateway anyway.",
                  file=sys.stderr)
        import cc_gateway
        cc_gateway.serve(_main_backend, host=host, port=port_num)
        return

    global _NUDGE_ENABLED
    _NUDGE_ENABLED = _NUDGE_ENABLED or args.nudge

    initial_prompt = " ".join(args.prompt).strip() or None

    # TUI is the default in any mode that has an interactive prompt:
    #   plain run, `-c` resume-then-interactive, or initial-prompt + interactive.
    # Automation modes (`-a`, `-r`) never get a TUI since there is no prompt.
    tui_enabled = not args.no_tui and not args.auto and args.repeat is None

    if args.repeat is not None:
        n = args.repeat
        run = 0
        try:
            while n == 0 or run < n:
                run += 1
                label = f"run {run}/{n}" if n > 0 else f"run {run}"
                _emit("on_repeat_run_start", label)
                run_agent_interactive(
                    initial_prompt=initial_prompt,
                    auto=True,
                    verbose=args.verbose,
                    result_file=args.result_file,
                )
        except KeyboardInterrupt:
            _emit("on_repeat_done", run)
    else:
        # `-c` without `-a` resumes the checkpoint and drops into interactive
        # mode; `-c -a` is the old auto-resume-and-exit behaviour.
        run_agent_interactive(
            initial_prompt=initial_prompt,
            auto=args.auto,
            continue_mode=args.continue_mode,
            tui=tui_enabled,
            verbose=args.verbose,
            result_file=args.result_file,
        )
    if _RESTART["requested"]:
        _exec_restart()   # never returns
    # F6: in -a / -r mode, say how the run ended in a form a supervisor can classify and exit
    # with the matching code. Interactive sessions never reach here with _AUTO_MODE set.
    # The gate runs BEFORE the exit line is composed, so its verdict is what a supervisor
    # reads. Deliberately after the run has finished writing: a check run mid-flight would
    # be judging an incomplete workspace.
    _acceptance_verdict()

    if _AUTO_MODE:
        info = _LAST_EXIT or _set_exit(EXIT_OK, "")
        # The result file was written on the completion path BEFORE the gate ran, so its `exit`
        # block said "completed" on a run whose acceptance then failed. A supervisor reading the
        # file and one reading the process status must see the same verdict.
        _refresh_result_exit(getattr(args, "result_file", None), info)
        print(_exit_line(info), file=sys.stderr, flush=True)
        if info["code"] != EXIT_OK:
            sys.exit(info["code"])


if __name__ == "__main__":
    # Force UTF-8 on stdout AND stderr. Under Git-Bash / a non-console handle on
    # Windows, Python picks the locale encoding (cp1252), which raises
    # UnicodeEncodeError on the non-ASCII characters in our banner and log
    # messages (e.g. "→", "●"). errors="replace" keeps a stray codepoint from
    # ever crashing output.
    for _std in ("stdout", "stderr"):
        _stream = getattr(sys, _std, None)
        if _stream is None:
            continue
        if hasattr(_stream, 'reconfigure'):
            _stream.reconfigure(encoding='utf-8', errors='replace')
        elif hasattr(_stream, 'buffer'):
            import io
            setattr(sys, _std, io.TextIOWrapper(
                _stream.buffer, encoding='utf-8', errors='replace'))
    import atexit
    # Always log bedrock session spend at exit — crashes (TimeoutError,
    # BedrockBudgetExceeded, etc.) would otherwise skip it. Safe no-op
    # when no bedrock backend is in use.
    atexit.register(_log_bedrock_session_spend, logging.getLogger("agent"))
    main()
