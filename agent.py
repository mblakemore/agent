#!/usr/bin/env python3
"""Main agent script.

Connects to llama-server and runs the agentic tool-calling loop.
Entry points: ``run_agent_interactive()`` for interactive use, ``run_agent()``
for single-prompt runs. See ``README.md`` for CLI flags.
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


import ctypes
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
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from cancel import cancellable, check_cancelled, CancelledError
try:
    from circuit_breaker import CircuitBreakerError
except ImportError:
    # Circuit breaker not available - define dummy exception
    class CircuitBreakerError(Exception):
        pass
from spinner import StreamStatus
from token_utils import count_tokens_from_message, count_tools_tokens
from tools import MAP_FN, tools, load_extra_tools
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
def _check_worktree_guard(file_path, worktree_path):
    """
    Checks if a file write target is inside the repo clone instead of the worktree.
    Returns (is_violation, correction_path)
    """
    if not worktree_path or not file_path:
        return False, None

    try:
        _abs_file = str(Path(file_path).resolve())
        _abs_wt = str(Path(worktree_path).resolve())
        _abs_cwd = str(Path.cwd().resolve())
        
        if (_abs_file.startswith(_abs_cwd) and not _abs_file.startswith(_abs_wt)):
            _rel = os.path.relpath(_abs_file, _abs_cwd)
            _correct = os.path.join(worktree_path, _rel)
            return True, _correct
    except Exception:
        pass
    return False, None

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

def _handle_cicd_file_edit(func_args, conversation_history, cicd_worktree_path, cicd_phase_state, cicd_edited_files, has_edited, has_reviewer_persisted, turn, log):
    """
    Tracks file edits for CICD state management and enforces worktree guards.
    Returns (updated_has_edited, updated_has_reviewer_persisted)
    """
    if not has_edited:
        log.info("First file edit detected at turn %d", turn)
    
    has_edited = True
    _file_path = func_args.get("path", "")
    if _file_path:
        cicd_edited_files.add(_file_path)

    # Worktree guard
    is_violation, correct = _check_worktree_guard(_file_path, cicd_worktree_path)
    if is_violation:
        log.warning("CICD: file write targets repo clone (%s), not worktree (%s)", _file_path, correct)
        conversation_history.append({
            "role": "user",
            "content": (
                f"[SYSTEM: WRONG PATH! You wrote to {_file_path} which is "
                f"inside the repo clone. Your worktree is at "
                f"'{cicd_worktree_path}'. You MUST write to "
                f"'{correct}' instead. Re-do this edit targeting "
                f"the worktree path now.]"
            ),
        })

    if "improvements/" in _file_path and _file_path.endswith(".md"):
        cicd_phase_state["plan"] = True
        log.info("CICD phase: plan written to %s", _file_path)

    if _file_path.endswith("reviews.md"):
        if not has_reviewer_persisted:
            has_reviewer_persisted = True
            log.info("CICD phase: review persisted to %s", _file_path)
        log.info("Reviewer persistence detected (reviews.md write) — completion signals now allowed")
    
    return has_edited, has_reviewer_persisted


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


_FILE_REF = re.compile(r"(?<!\w)@(\.{0,2}/\S+|(?![^\s@]*[@:])[A-Za-z_]\S*)")
_EXIT_ONLY_RE = re.compile(r'^\[session:[^\]]+\]\s+exit=0\s*$')

# ── Pinned instructions ───────────────────────────────────────────────
# Content inside <pinned>...</pinned> tags in the initial prompt is
# extracted and re-injected into every context-restoration message,
# surviving summarization.  This ensures critical workflow steps
# (like "create a worktree before editing") persist across the entire
# session even as older messages are compressed away.
_PINNED_RE = re.compile(r"<pinned>(.*?)</pinned>", re.DOTALL)
_pinned_instructions = ""  # set once from the initial prompt

# CICD phase tracking — module-level so _build_context_message() can read it.
# Updated by run_agent() as it detects phase transitions from tool calls.
_cicd_phase_state = {}
_cicd_issue_number = None
_cicd_pr_number = None
_cicd_branch = None
_cicd_edited_files = set()  # tracks edited file paths to survive summary compression
_cicd_worktree_path = None  # actual worktree path, captured on successful `git worktree add`


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

_DEFAULT_CONFIG = {
    "llm": {"base_url": "http://127.0.0.1:8080", "model": "gemma-4-31B"},
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
        "max_context_messages": 20,
        "ctx_size": 114688,
        "max_tokens": 16384,
    },
    "cycle": {
        "max_turns": 250,
        "wind_down_turns": 10,
        "max_text_only": 3,
        "max_total_nudges": 6,
    },
    "generation": {
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 64,
        "presence_penalty": 0.0,
    },
    "summary": {
        "base_url": "http://127.0.0.1:8082",
        "model": "gemma-4-E4B",
        "enabled": True,
        "max_wait_on_save": 10,
    },
    "bedrock": {
        "adaptive_max_tokens": True,
    },
}


def _synthesize_backends_registry(config):
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


def _redact_api_keys(cfg):
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
            result[k] = _redact_api_keys(v)
        else:
            result[k] = v
    return result


def _warn_if_world_readable_with_key(config_path, user_config):
    """Emit a WARN log line if ``config.json`` is world-readable AND
    contains a non-empty ``api_key`` under any ``backends`` entry.

    Plan § 18.75 security checklist. Don't enforce; just warn.
    """
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


def _load_config():
    """Load configuration from CWD/config.json, deep-merged with defaults.

    Also synthesizes a ``backends`` registry (plan § 6 / D3) from the legacy
    ``llm`` / ``summary`` blocks for back-compat. Existing call sites that
    read ``_config["llm"]`` / ``_config["summary"]`` continue to work.
    """
    config = json.loads(json.dumps(_DEFAULT_CONFIG))  # deep copy

    config_path = Path(os.getcwd()) / "config.json"
    user_config = None
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8", errors="replace") as f:
                user_config = json.load(f)
            for section in config:
                if section in user_config and isinstance(user_config[section], dict):
                    config[section].update(user_config[section])
            # Copy top-level scalar overrides (e.g. log_dir, log_prefix) that aren't
            # _DEFAULT_CONFIG sections — the loop above only handles dict-valued sections.
            for key, val in user_config.items():
                if key not in config and not isinstance(val, dict):
                    config[key] = val
            _warn_if_world_readable_with_key(config_path, user_config)
        except (json.JSONDecodeError, IOError) as e:
            _emit("on_notice", "warn", f"Warning: Could not load config.json, using defaults: {e}")
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
    config["backends"] = _synthesize_backends_registry(merge_src)

    return config


_config = _load_config()

# Apply configuration
BASE_URL = _config["llm"]["base_url"]

# Backend instances (plan task 1.4). Phase 1 only wires LlamacppBackend;
# Phase 2 adds BedrockBackend behind the same factory. Tests may monkeypatch
# these module globals to swap backends without touching the factory.
from llm_backend import build_backend as _build_backend


def _cfg_with_role(backends: dict, role: str) -> dict:
    """Return the role's backend config dict with ``role`` injected.

    Plan task 2.4: ``BedrockBackend`` uses ``role`` to pick the daily cost
    cap and label telemetry lines. LlamacppBackend ignores ``role`` so the
    injection is safe for both kinds.
    """
    entry = dict(backends.get(role, {}))
    entry.setdefault("role", role)
    return entry


_main_backend = _build_backend(_cfg_with_role(_config["backends"], "main"))
_summary_backend = _build_backend(_cfg_with_role(_config["backends"], "summary"))


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

    defaults = {"main": "claude-v4.6-opus", "summary": "claude-v4.5-haiku"}

    # Model IDs the Bedrock gateway accepts. An existing model that doesn't
    # match one of these prefixes was set for the OTHER backend kind (likely
    # a llamacpp default like ``gemma-4-31B`` carried over from
    # _synthesize_backends_registry), so the CLI override must replace it
    # with a Bedrock-compatible default — otherwise the gateway returns 422.
    _bedrock_prefixes = (
        "claude-", "mistral-", "mixtral-", "amazon-nova-",
        "deepseek-", "llama-", "llama3-", "qwen",
    )

    def _override(role: str, kind: str | None):
        if not kind:
            return
        entry = _config["backends"].setdefault(role, {})
        entry["kind"] = kind
        if kind == "bedrock":
            existing = entry.get("model", "")
            if not existing or not existing.startswith(_bedrock_prefixes):
                entry["model"] = defaults[role]
                logging.getLogger("agent").info(
                    "backend-override role=%s kind=bedrock model=%s "
                    "(default — replaced incompatible %r)",
                    role,
                    entry["model"],
                    existing or "<unset>",
                )

    _override("main", main_kind)
    _override("summary", summary_kind)

    if main_kind:
        _main_backend = _build_backend(_cfg_with_role(_config["backends"], "main"))
    if summary_kind:
        _summary_backend = _build_backend(
            _cfg_with_role(_config["backends"], "summary")
        )
_MAX_FULL_LINES = _config["context"]["max_full_lines"]
_PREVIEW_LINES = _config["context"]["preview_lines"]
_SUMMARY_THRESHOLD = _config["context"]["summary_threshold"]
_SUMMARY_MAX_CHARS = _config["context"]["summary_max_chars"]
_MAX_CONTEXT_MESSAGES = _config["context"]["max_context_messages"]

_LLM_MAX_RETRIES = _config["retry"]["max_retries"]
_LLM_BASE_DELAY = _config["retry"]["base_delay_seconds"]
_LLM_MAX_DELAY = _config["retry"]["max_delay_seconds"]
_LLM_BACKOFF_MULTIPLIER = _config["retry"]["backoff_multiplier"]
_LLM_JITTER_FACTOR = _config["retry"]["jitter_factor"]

_MAX_TURNS = _config["cycle"]["max_turns"]
_WIND_DOWN_TURNS = _config["cycle"]["wind_down_turns"]
_MAX_TEXT_ONLY = _config["cycle"]["max_text_only"]
_MAX_TOTAL_NUDGES = _config["cycle"]["max_total_nudges"]

# Auto-nudge on text-only responses. Off by default; enable with --nudge.
_NUDGE_ENABLED = False

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
    cmd_stripped = cmd.lstrip("# \t\n")
    if any(cmd_stripped.startswith(rc) for rc in _READ_ONLY_COMMANDS):
        return True
    return not any(kw in cmd for kw in _WRITE_KEYWORDS)

def _validate_tool_call(func_name, func_args, cicd_issue_view_called, log, is_cicd_builder=False, is_cicd_reviewer=False):
    """
    Returns (is_blocked, error_message).
    If is_blocked is True, the tool should not be executed.
    """
    # Cycle 84 (runs 190+191 reviewer fix-forward failure mode): block the
    # reviewer from editing production .py files inside its review worktree.
    # Cycle 75 already says reviewer commits may only modify `tests/**`, but
    # the rule was prose only — runs 190 and 191 each saw the reviewer spend
    # 30+ turns rewriting `tools/exec_command.py` in the `pr-N` worktree
    # instead of issuing a verdict. The work is throwaway (no commit path)
    # and the missing verdict leaves the queue stuck.
    #
    # Detection: file tool, write/insert/replace action, target is a non-test
    # `.py` file, AND the target path is inside a `/worktrees/pr-<N>/` review
    # worktree (vs a builder worktree which has different naming). The marker
    # `/worktrees/pr-` is what reviewer.md prescribes (`git worktree add
    # <ROOT>/pr-<N> review/pr-<N>`).
    if func_name == "file" and isinstance(func_args, dict):
        _action = func_args.get("action", "")
        _path = func_args.get("path", "")
        if (_action in ("write", "insert", "replace")
                and _path.endswith(".py")
                and "/tests/" not in _path
                and not _path.startswith("tests/")
                and re.search(r"/worktrees/pr-\d+/", _path)):
            log.warning(
                "CICD: reviewer file edit BLOCKED — production .py inside review worktree (cycle 84)"
            )
            return True, (
                "Error: CICD reviewer file edit BLOCKED — you are editing a "
                f"production Python file ({_path}) inside a review worktree. "
                "Your role is REVIEWER, not BUILDER. Per cycle 75, reviewer "
                "commits may only modify `tests/**`. If the PR has a real bug "
                "in production code, the verdict is REQUEST_CHANGES (or CLOSE "
                "for destruction-class signatures) — leave a `gh pr comment "
                "<N> --body \"Verdict: REQUEST_CHANGES ...\"` citing the "
                "exact file:line and expected fix, then move on. Do NOT fix "
                "the code yourself — the builder owns production changes."
            )

    if func_name != "exec_command":
        return False, None

    _precmd = func_args.get("command", "") if isinstance(func_args, dict) else ""

    # Cycle 96 — skip shell-level guards for python3/python invocations.
    # Guard regexes match CICD keywords appearing as string literals inside
    # python -c scripts, producing false positives (run 207 reviewer, turns 30-62).
    if re.match(r'\s*python3?\s', _precmd):
        return False, None

    # PRE-MERGE CHECK — gated on CICD sessions only (issue #455: don't fire
    # for non-CICD repos that merge PRs without linked issues).
    if ((is_cicd_builder or is_cicd_reviewer)
            and re.search(r"(?:^|&&\s*|;\s*|\|\|?\s*|\n\s*)gh\s+pr\s+merge\b", _precmd)
            and not cicd_issue_view_called):
        log.warning("CICD: gh pr merge BLOCKED — PRE-MERGE CHECK required (cycle 24)")
        return True, (
            "Error: CICD PRE-MERGE CHECK required. Before `gh pr merge`, you "
            "MUST run `gh issue view <N> --json state,labels,title,createdAt` "
            "on the linked issue and verify: state is OPEN, labels include "
            "`cicd` + `in-progress`, the title matches the PR's stated scope. "
            "Run the gh issue view now as a SEPARATE command, then re-attempt "
            "the merge. The merge was NOT executed."
        )
    
    # PR CREATE CHECK
    _precmd_body_check = _precmd
    # cycle 60: read pr-body file so Closes #N is visible even with $(cat ...) expansion
    # cycle 71: also handle per-issue filenames like /tmp/pr-body-324.md
    _pb_match = re.search(r'\$\(cat (/tmp/pr-body(?:-\d+)?\.md)\)', _precmd)
    if _pb_match:
        try:
            with open(_pb_match.group(1)) as _pf:
                _precmd_body_check = _precmd_body_check + " " + _pf.read()
        except OSError:
            pass
            
    # cycle 86: only enforce `Closes #N` requirement in CICD builder sessions;
    # general agent use on other repos may legitimately create PRs without a
    # linked issue (roadmap commits, README updates, etc.).
    if (is_cicd_builder
            and re.search(r"(?:^|&&\s*|;\s*|\|\|?\s*|\n\s*)gh\s+pr\s+create\b", _precmd)
            and not re.search(r'Closes\s+#\d+', _precmd_body_check, re.IGNORECASE)):
        log.warning("CICD: gh pr create blocked — body missing valid Closes #N (cycle 44)")
        return True, (
            "Error: CICD gh pr create blocked — the --body must contain "
            "`Closes #<N>` where N is a numeric issue number (e.g. `Closes #123`). "
            "Non-numeric references like `Closes #slug` or missing Closes trailer "
            "cause the reviewer to CLOSE this PR. "
            "File the issue first with `gh issue create --label in-progress --label cicd ...`, "
            "note the issue number, then include `Closes #<number>` in the PR body. "
            "The PR was NOT created."
        )

    # Cycle 81 (run 186 failure mode): block `git push origin main` pre-execute.
    # Cycle 37's post-execute WARNING let the push through — run 186 builder
    # committed `ed67439` directly to main, push succeeded with only a log line,
    # builder reverted as `42a1dac`, then hit hard-cap with no PR opened. 5
    # turns + the entire cycle were wasted. Make it a hard block at the same
    # point as the other CICD pre-checks.
    # cycle 81 — gated on CICD sessions only (issue #455: non-CICD repos may
    # legitimately push directly to main).
    if ((is_cicd_builder or is_cicd_reviewer)
            and re.search(r"(?:^|&&\s*|;\s*|\|\|?\s*|\n\s*)git\s+push\b[^&;|]*\borigin\s+main\b", _precmd)):
        log.warning("CICD: git push origin main BLOCKED — must use feature branch (cycle 81)")
        return True, (
            "Error: CICD `git push origin main` BLOCKED — direct pushes to main "
            "are prohibited. All CICD work must land via a feature branch + PR.\n"
            "If you have a local commit on main that shouldn't be there:\n"
            "  1. `git reset --hard origin/main` (drops the local commit)\n"
            "  2. `git worktree add <WORKTREE_ROOT>/<slug> -b cicd/<slug>`\n"
            "  3. Re-apply your changes inside the worktree\n"
            "  4. `git push -u origin cicd/<slug>` then `gh pr create --draft ...`\n"
            "Do NOT use `git revert HEAD && git push origin main` — that still "
            "pushes to main. The push was NOT executed."
        )

    # Cycle 80 (run 183 failure mode): block `gh pr create` if any edited .py
    # file fails py_compile. Run 183 PR #398 shipped IndentationError at the
    # four "Session ended" sites + a double `continue` in `_iter_stream_chunks`.
    # Cycle 65's "git checkout HEAD -- agent.py and reapply" was prose only;
    # the builder ignored it and 93 turns + a R-0008 REQUEST_CHANGES were lost.
    # This guard makes py_compile a hard gate at the same point as the Closes
    # check — the only way past is to fix the syntax.
    if re.search(r"(?:^|&&\s*|;\s*|\|\|?\s*|\n\s*)gh\s+pr\s+create\b", _precmd):
        import py_compile
        _syntax_errors = []
        for _path in sorted(_cicd_edited_files):
            if not _path.endswith(".py"):
                continue
            try:
                py_compile.compile(_path, doraise=True)
            except py_compile.PyCompileError as _e:
                _syntax_errors.append(f"{_path}: {str(_e).strip()}")
            except OSError:
                pass
        if _syntax_errors:
            log.warning(
                "CICD: gh pr create BLOCKED — %d edited .py file(s) fail py_compile (cycle 80)",
                len(_syntax_errors),
            )
            return True, (
                "Error: CICD gh pr create BLOCKED — edited Python files fail py_compile:\n  - "
                + "\n  - ".join(_syntax_errors)
                + "\nFix the syntax errors and re-verify with `python3 -m py_compile <file>` "
                "before opening the PR. If the error keeps moving line-to-line, "
                "`git checkout HEAD -- <file>` and reapply the change with exact "
                "indentation (cycle 65). The PR was NOT created."
            )

    return False, None


def _cicd_verify_gh_mutation(command: str, result: str, log) -> str:
    """Cycle 77 — verify claimed gh PR/issue mutations against GitHub's actual state.

    Runs 142 + 146 hallucinated exec_command tool results that *looked* like
    successful `gh pr create` / `gh pr merge` output (URLs, exit=0) but the
    referenced PR did not actually exist on GitHub. The model then proceeded
    as if the work was done.

    This helper runs after any `gh pr create|merge|close|ready` or
    `gh issue create|close` command. It extracts the PR/issue number the
    model claimed in the tool output, queries GitHub directly for its real
    state, and appends a SUPERVISION note to the tool result if the state
    does not match the mutation's expected outcome. The note becomes part
    of the tool-result the model sees next turn, so fabricated numbers are
    corrected with real data the model cannot override.

    Scoped to the CICD exec_command path; returns ``result`` unchanged for
    non-gh commands so non-CICD runs see zero overhead.
    """
    action_match = re.search(r"\bgh\s+(pr|issue)\s+(create|merge|close|ready)\b", command)
    if not action_match:
        return result
    kind_word, action = action_match.group(1), action_match.group(2)
    # Forensic probe (cycle 77 step 3): prove the hook fires on gh mutations.
    # Keep at INFO so it appears in the standard CICD run log.
    log.info("cicd.gh_verify.enter kind=%s action=%s cmd_preview=%r",
             kind_word, action, command[:80])
    expected_state = {
        "create": None,    # just needs to exist
        "merge": "MERGED",
        "close": "CLOSED",
        "ready": "OPEN",
    }[action]

    # Parse claimed number from the result: URLs like .../pull/123 or .../issues/456
    url_match = re.search(
        r"github\.com/[^\s]+/(pull|issues)/(\d+)", result, re.IGNORECASE)
    if not url_match:
        # Also accept bare "#N" patterns in case gh output form differs
        hash_match = re.search(r"(?:pull request|issue) #(\d+)", result, re.IGNORECASE)
        if not hash_match:
            return result
        path = "pr" if kind_word == "pr" else "issue"
        num = hash_match.group(1)
    else:
        path = "pr" if url_match.group(1).lower() == "pull" else "issue"
        num = url_match.group(2)

    try:
        import subprocess
        proc = subprocess.run(
            ["gh", path, "view", num, "--json", "state,title"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.warning("cicd.gh_verify.probe_failed path=%s num=%s error=%s", path, num, e)
        return result

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip().splitlines()[-1][:160]
        log.warning("cicd.gh_verify.missing path=%s num=%s", path, num)
        return result + (
            f"\n\n[CICD SUPERVISION (cycle 77): gh {path} view {num} failed — "
            f"the {path} #{num} referenced in the tool output DOES NOT EXIST on GitHub. "
            f"Do not proceed as if the {action} succeeded. "
            f"gh stderr: {err}]"
        )

    try:
        import json as _json
        data = _json.loads(proc.stdout)
        actual_state = (data.get("state") or "").upper()
    except (ValueError, KeyError) as e:
        log.warning("cicd.gh_verify.parse_failed num=%s error=%s", num, e)
        return result

    if expected_state and actual_state != expected_state:
        log.warning(
            "cicd.gh_verify.state_mismatch path=%s num=%s expected=%s actual=%s",
            path, num, expected_state, actual_state,
        )
        return result + (
            f"\n\n[CICD SUPERVISION (cycle 77): gh {path} view {num} reports state={actual_state}, "
            f"but `gh {kind_word} {action}` claims {expected_state}. "
            f"The {action} did NOT take effect as narrated. Investigate before proceeding.]"
        )

    return result


# Load agent-specific tools from CWD/.agent/tools/ if it exists.
# Note: CWD/tools/ is the builtin package already loaded by tools/__init__.py —
# pointing the loader at it would re-execute every module under a fake
# "extra_tools" parent, breaking relative imports (e.g. exec_command).
_agent_tools_dir = os.path.join(os.getcwd(), ".agent", "tools")
if os.path.isdir(_agent_tools_dir):
    load_extra_tools(_agent_tools_dir)


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
from llm_backend import BedrockBudgetExceeded, ContextOverflowError


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
    """Check if an exception should trigger failover to llamacpp.
    
    Handles both specific exception classes and generic exceptions containing
    failover keywords (useful for some mock tests and generic Bedrock errors).
    """
    if isinstance(e, (BedrockBudgetExceeded, requests.exceptions.ConnectionError,
                      requests.exceptions.Timeout, TimeoutError)):
        return True
    
    err_msg = str(e)
    failover_keywords = ["BedrockBudgetExceeded", "Capacity exceeded", "Rate limit exceeded"]
    return any(kw in err_msg for kw in failover_keywords)

def _trigger_failover(log, backend_type):
    """Swap Bedrock for llamacpp if Bedrock is failing.
    
    Args:
        log: Logger instance.
        backend_type: Either 'main' or 'summary'.
    """
    global _main_backend, _summary_backend
    
    try:
        # Attempt to build a llamacpp backend. 
        from llm_backend import build_backend
        from agent import _config
        
        llamacpp_cfg = _config.get("backends", {}).get("llamacpp", {})
        
        new_backend = build_backend(llamacpp_cfg)
        healthy, msg = new_backend.health()
        if healthy:
            if backend_type == 'main':
                _main_backend = new_backend
            else:
                _summary_backend = new_backend
            log.warning("Failover successful: %s backend is now llamacpp (%s)", backend_type, msg)
            return True
        else:
            log.error("Failover failed: llamacpp backend unhealthy (%s)", msg)
            return False
            
    except Exception as e:
        log.error("Failover critical failure: %s", e)
        return False

def _llm_request(log, **kwargs):
    """Main-path streaming request. Thin wrapper that routes through the
    module-level ``_main_backend`` (see plan task 1.4).

    Signature matches the pre-refactor ``_llm_request`` exactly so existing
    tests that ``patch('agent._llm_request')`` still work unmodified.
    """
    try:
        return _main_backend.stream_chat(log, **kwargs)
    except Exception as e:
        if _is_failover_error(e):
            if _trigger_failover(log, 'main'):
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


_FILE_ACTIONS = {"read", "write", "insert", "append", "delete", "list"}


def _sanitize_tool_args(func_name, args, log):
    """Fix garbled args that parsed as valid JSON but have bogus values.

    Gemma 4 concatenates **,key:value into field values, e.g.:
      {"action": "write**,content:some text"}
      {"action": "write", "path": "foo.json**,start_line:1", "end_line": 14}
    This extracts embedded params from ALL string fields.
    """
    if func_name != "file" or not isinstance(args, dict):
        return args

    # Check if any string value contains the **,key: pattern
    _GARBLE_PAT = re.compile(r'\*\*,(\w+):')
    needs_fix = False
    for v in args.values():
        if isinstance(v, str) and _GARBLE_PAT.search(v):
            needs_fix = True
            break

    action = args.get("action", "")
    if not needs_fix and action in _FILE_ACTIONS:
        return args

    log.warning("Sanitizing garbled file args: %s",
                {k: repr(v)[:60] for k, v in args.items()})

    # Collect all key:value pairs from garbled strings across all fields
    extracted = {}
    clean_vals = {}
    for key, val in args.items():
        if not isinstance(val, str):
            extracted[key] = val
            continue
        # Split on **,key: boundaries to extract embedded params
        parts = _GARBLE_PAT.split(val)
        # parts[0] is the clean prefix of this field's value
        clean_val = parts[0].rstrip('*').strip()
        if clean_val:
            clean_vals[key] = clean_val
        # Remaining parts alternate: key_name, value_before_next_split
        for i in range(1, len(parts) - 1, 2):
            embed_key = parts[i]
            embed_val = parts[i + 1].rstrip('*').strip() if i + 1 < len(parts) else ""
            if embed_val:
                # Try to parse integers for line numbers
                if embed_key in ("start_line", "end_line"):
                    try:
                        extracted[embed_key] = int(embed_val)
                    except ValueError:
                        extracted[embed_key] = embed_val
                else:
                    extracted[embed_key] = embed_val

    # Build fixed args: extracted embedded params + clean field values
    fixed = {}
    for key, val in clean_vals.items():
        fixed[key] = val
    for key, val in extracted.items():
        if key not in fixed:
            fixed[key] = val

    # Fix action if it was garbled
    if "action" in fixed and fixed["action"] not in _FILE_ACTIONS:
        import difflib
        action_lower = str(fixed["action"]).lower()
        # 1. Try substring match (e.g., "read_this_file" -> "read")
        substring_match = next((t for t in _FILE_ACTIONS if t in action_lower), None)
        # 2. Try fuzzy match for typos (e.g., "raed" -> "read")
        fuzzy_matches = difflib.get_close_matches(action_lower, _FILE_ACTIONS, n=1, cutoff=0.6)
        
        if substring_match:
            fixed["action"] = substring_match
        elif fuzzy_matches:
            fixed["action"] = fuzzy_matches[0]
    log.info("Sanitized args: %s",
             {k: repr(v)[:60] if isinstance(v, str) else v for k, v in fixed.items()})
    return fixed


def _salvage_tool_args(func_name, raw_args, log):
    """Try to extract valid arguments from garbled Gemma 4 tool call output.

    Gemma 4 sometimes concatenates keys into the action field
    (e.g. "write**,content:..." or "read,path:..."). This attempts to
    recover the intended arguments.

    Returns a dict on success, None if unsalvageable.
    """
    try:
        # Strip Gemma 4 special tokens that leak into args
        cleaned = raw_args.replace('<|"|>', '"').replace('<|', '').replace('|>', '')
        # Try parsing again after cleanup
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # Pattern: "action**,key:value,key:value" or "action,key:value"
        # Try to extract action and rebuild as JSON
        if func_name == "file":
            for action in ("read", "write", "insert", "append", "delete", "list"):
                if action in raw_args.lower():
                    result = {"action": action}
                    # Try to find path
                    path_match = re.search(r'path["\s:]+([^\s,}"]+)', raw_args)
                    if path_match:
                        result["path"] = path_match.group(1).strip('"\'')
                    # Try to find content
                    content_match = re.search(r'content["\s:]+(.+?)(?:,\s*(?:path|start_line|end_line)|$)', raw_args, re.DOTALL)
                    if content_match:
                        result["content"] = content_match.group(1).strip('"\'')
                    if "path" in result:
                        log.warning("Salvaged garbled tool args: %s → %s", raw_args[:100], result)
                        return result

        # For exec_command, try to find the command string
        if func_name == "exec_command":
            cmd_match = re.search(r'command["\s:]+(.+)', raw_args, re.DOTALL)
            if cmd_match:
                cmd = cmd_match.group(1).strip('"\'').rstrip('}')
                log.warning("Salvaged garbled exec_command: %s", cmd[:100])
                return {"command": cmd}

    except Exception as e:
        log.debug("Salvage attempt failed: %s", e)

    return None


# ── Token estimation ───────────────────────────────────────────────────

def _estimate_tokens(msg):
    return count_tokens_from_message(msg)


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


_TOOLS_TOKENS = None


def _estimate_tools_tokens():
    global _TOOLS_TOKENS
    if _TOOLS_TOKENS is None:
        _TOOLS_TOKENS = count_tools_tokens(tools)
    return _TOOLS_TOKENS


# ── File reference expansion ──────────────────────────────────────────

def _expand_file_refs(text):
    """Expand @filepath references in user input to inline file contents.

    Returns (expanded_text, files_content, error).
    """
    refs = _FILE_REF.findall(text)
    if not refs:
        return text, None, None

    # Resolve working directory once so every ref can be checked against it.
    # We use Path.cwd().resolve() (not os.getcwd()) to follow any symlinks in
    # the cwd itself, giving a canonical base for confinement checks.
    cwd_resolved = Path.cwd().resolve()
    cwd_prefix = str(cwd_resolved) + os.sep  # e.g. /droid/repos/agent/

    seen = set()
    attachments = []
    for ref in refs:
        if ref in seen:
            continue
        seen.add(ref)

        p = Path(ref)
        if not p.exists():
            return None, None, f"Error: file '{ref}' does not exist"
        if p.is_dir():
            return None, None, f"Error: '{ref}' is a directory, not a file"

        # Confinement check: reject any ref that resolves outside the working
        # directory.  This blocks both relative traversals (@../../secret) and
        # absolute paths (@/etc/passwd) that escape the project tree.
        resolved_ref = p.resolve()
        if resolved_ref != cwd_resolved and not str(resolved_ref).startswith(cwd_prefix):
            return (
                None,
                None,
                f"Error: '{ref}' resolves to '{resolved_ref}' which is outside "
                f"the working directory '{cwd_resolved}'. "
                f"Only files inside the current working directory can be referenced with @.",
            )

        lines = p.read_text(encoding='utf-8', errors='replace').splitlines(True)
        total = len(lines)
        if total <= _MAX_FULL_LINES or p.name == "agent.md":
            content = "".join(lines)
            header = f"[{ref}: {total} lines]"
        else:
            content = "".join(lines[:_PREVIEW_LINES])
            header = f"[{ref}: first {_PREVIEW_LINES} of {total} lines]"

        resolved = str(p.resolve())
        if p.name == "agent.md":
            header = (f"[AGENT IDENTITY FILE: {ref} (loaded from {resolved}). "
                      f"This is YOUR agent.md — do not search for it elsewhere. {total} lines]")

        attachments.append(f"{header}\n{content}")
        _emit("on_file_attached", header)

    files_content = "\n\n".join(attachments)
    # Prepend working directory context when agent.md is loaded
    cwd = os.getcwd()
    if any(Path(ref).name == "agent.md" for ref in seen):
        preamble = (
            f"[SYSTEM CONTEXT: Your working directory is {cwd}. "
            f"All relative paths resolve from here. "
            f"Do not cd to other repositories or search for files outside this tree. "
            f"Do not start with a directory listing for orientation. The task prompt specifies the repo path. "
            f"Begin with the first tool call that directly advances the task — read a specific file, "
            f"search for a pattern, or look up a symbol. "
            f"Use `file list` only when you specifically need to enumerate a directory's contents as part of the task. "
            f"**Search before reading large files.** When you need to find where something is defined or called, use `search_files` first to locate the right file and line — then read only from that line using `start_line=`. Never read a whole file >300 lines hoping to find something; search first.]\n\n"
        )
    else:
        preamble = ""
    expanded = text + "\n\n" + preamble + files_content
    return expanded, preamble + files_content if preamble else files_content, None


# ── Summarization ─────────────────────────────────────────────────────

def _format_for_summary(messages):
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


def _summary_request(prompt, base_url=None, model=None, **kwargs):
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
        backend = _summary_backend
        if backend.kind == "llamacpp" and (base_url or model):
            # Build a transient backend with the overrides
            override_cfg = dict(backend._cfg)
            if base_url:
                override_cfg["base_url"] = base_url
            if model:
                override_cfg["model"] = model
            transient = _build_backend({**override_cfg, "kind": "llamacpp"})
            return transient.complete(prompt=prompt)
        if backend.kind != "llamacpp" and (base_url or model):
            log.debug(
                "_summary_request ignoring base_url/model override — "
                "summary backend kind=%s",
                backend.kind,
            )
        return backend.complete(prompt=prompt)
    except Exception as e:
        raise e


def _condense_summary(text, log=None):
    """Re-summarize if text exceeds the character cap. Preserves all info."""
    if len(text) <= _SUMMARY_MAX_CHARS:
        return text
    if log:
        log.info("Summary too long (%d chars, limit %d) — condensing", len(text), _SUMMARY_MAX_CHARS)
    _emit("on_summary_start", 0)
    prompt = (
        f"The following summary is too long ({len(text)} chars). "
        f"Rewrite it in under {_SUMMARY_MAX_CHARS // 2} characters. "
        f"Keep ALL file paths, decisions, failures, and completed actions. "
        f"Remove filler and verbose descriptions. Be maximally terse.\n\n"
        f"{text}"
    )
    try:
        condensed = _summary_request(prompt)
        if len(condensed) > _SUMMARY_MAX_CHARS:
            # Model didn't comply — hard truncate as last resort
            if log:
                log.warning("Condensed summary still too long (%d chars), truncating", len(condensed))
            condensed = condensed[:_SUMMARY_MAX_CHARS].rsplit('\n', 1)[0] + "\n[...truncated]"
        if log:
            log.info("Condensed summary: %d → %d chars", len(text), len(condensed))
        _emit("on_notice", "info", f"[summary condensed: {len(text)} → {len(condensed)} chars]")
        return condensed
    except Exception as e:
        if log:
            log.error("Condense failed (%s), truncating as fallback", e)
        return text[:_SUMMARY_MAX_CHARS].rsplit('\n', 1)[0] + "\n[...truncated]"


def _build_summary_prompt(old_summary, new_messages):
    """Build the summary prompt from old summary + new messages."""
    transcript = _format_for_summary(new_messages)

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


def _generate_summary(old_summary, new_messages, log):
    """Call the LLM to produce an updated conversation summary.

    The summary prompt explicitly preserves decisions, outcomes, and failed
    approaches to prevent the agent from repeating mistakes.

    Tries the dedicated summary endpoint first (CPU model on port 8082),
    falls back to the main model on connection failure.
    """
    prompt = _build_summary_prompt(old_summary, new_messages)
    log.info("Generating conversation summary...")

    summary_cfg = _config["summary"]
    summary_url = summary_cfg["base_url"]

    # Guard: If summary is disabled or no base_url is provided, skip attempt.
    if not summary_cfg["enabled"] or not summary_url:
        return old_summary

    def _fallback_to_main(reason_exc):
        """Route the summary through the main backend after a summary failure.

        The legacy path called ``_summary_request(prompt, base_url=BASE_URL, ...)``
        which only re-homes to a llamacpp override and is a no-op when the
        summary backend is non-llamacpp (Bedrock). Go directly to the
        ``_main_backend.complete()`` so the fallback actually runs through a
        different backend kind when the primary is Bedrock and e.g. the
        daily cost cap tripped. See plan section 15 rollback / section 6.5 guardrail.
        """
        log.warning(
            "Summary failed on %s backend (%s); falling back to main model",
            getattr(_summary_backend, "kind", "?"),
            reason_exc,
        )
        try:
            summary = _main_backend.complete(prompt=prompt)
            log.info("SUMMARY (fallback): %s", summary)
            return summary
        except Exception as e2:
            log.error("Summary fallback also failed: %s", e2)
            return old_summary or ""

    _summary_t0 = time.monotonic()
    try:
        # Try dedicated summary endpoint first — via _summary_request, which
        # now routes through _summary_backend.complete (plan task 1.4).
        summary = _summary_request(prompt, log=log)
        telemetry.record_summary()
        log.info("SUMMARY: %s", summary)
        if telemetry.verbose_enabled():
            telemetry.record_turn(
                role="summary",
                duration_s=time.monotonic() - _summary_t0,
                tool_calls=0,
                in_tokens=0,
                out_tokens=0,
                model=getattr(_summary_backend, "model", "")
                or _config.get("summary", {}).get("model", ""),
            )
        return summary
    except (requests.ConnectionError, requests.Timeout,
            TimeoutError, BedrockBudgetExceeded) as e:
        if _trigger_failover(log, 'summary'):
            log.info("Retrying summary with failover backend...")
            try:
                summary = _summary_request(prompt, log=log)
                telemetry.record_summary()
                log.info("SUMMARY (retry): %s", summary)
                return summary
            except Exception as e2:
                log.error("Summary retry failed: %s", e2)
                return _fallback_to_main(e2)
        return _fallback_to_main(e)
    except BedrockBudgetExceeded as e:
        # Budget cap tripped — fall back to local main for the rest of the
        # session. Avoids cascading context overflow from missing summaries.
        return _fallback_to_main(e)
    except Exception as e:
        # For any other summary-backend error, if the summary backend is
        # different from the main one, try the main backend before giving up.
        if getattr(_summary_backend, "kind", None) != getattr(
            _main_backend, "kind", None
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
    """

    def __init__(self, config, log):
        self._config = config
        self._log = log
        self._lock = threading.Lock()
        self._thread = None
        self._pending_result = None
        self._pending_up_to = None
        self._running = False

    def kick(self, old_summary_text, messages_snapshot, up_to_idx):
        """Start background summarization if not already running."""
        with self._lock:
            if self._running:
                return
            self._running = True

        msgs = deepcopy(messages_snapshot)

        def _worker():
            try:
                prompt = _build_summary_prompt(old_summary_text, msgs)
                # Try dedicated endpoint, fall back to main model.
                # Routes through _summary_request which delegates to the
                # summary backend under the hood (plan task 1.4).
                try:
                    result = _summary_request(prompt)
                except (requests.ConnectionError, requests.Timeout) as e:
                    self._log.warning("Async summary endpoint unavailable (%s), "
                                      "falling back to main model", e)
                    result = _summary_request(
                        prompt,
                        base_url=BASE_URL,
                        model=self._config["llm"]["model"],
                    )
                self._log.info("ASYNC SUMMARY: %s", result)
                with self._lock:
                    self._pending_result = result
                    self._pending_up_to = up_to_idx
            except Exception as e:
                self._log.error("Async summary failed: %s", e)
            finally:
                with self._lock:
                    self._running = False

        self._thread = threading.Thread(target=_worker, daemon=True)
        self._thread.start()

    def harvest(self, summary_state):
        """If a completed summary is available, apply it. Returns True if updated."""
        with self._lock:
            if self._pending_result is None:
                return False
            summary_state["text"] = _condense_summary(self._pending_result, self._log)
            summary_state["up_to"] = self._pending_up_to
            self._pending_result = None
            self._pending_up_to = None
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


# ── Context window management ─────────────────────────────────────────

def _build_context_footnote(summary_text, initial_files):
    """Build the synthetic context-restoration user message.

    Returns a dict suitable for insertion at the start of the messages list
    when some history has been dropped from the context window.  Always
    includes the TOOL RULE hint so agents in condensed-summary sessions keep
    the guidance about how to write JSON files.

    Args:
        summary_text: The current progress-summary string (non-empty).
        initial_files: Optional string of initial file content to prepend.
    """
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
    if _cicd_phase_state and any(_cicd_phase_state.values()):
        phases = " | ".join(
            f"{k.upper()} {'✓' if v else '✗'}"
            for k, v in _cicd_phase_state.items()
        )
        phase_line = f"PHASE CHECKPOINT: {phases}"
        if _cicd_issue_number:
            phase_line += f"\nIssue: #{_cicd_issue_number}"
        if _cicd_pr_number:
            phase_line += f"  PR: #{_cicd_pr_number}"
        if _cicd_branch:
            phase_line += f"  Branch: {_cicd_branch}"
        if _cicd_worktree_path:
            phase_line += f"\nWorktree path: {_cicd_worktree_path} (ALL file edits and commands MUST target this path, not the repo clone)"
        if _cicd_edited_files:
            phase_line += f"\nFiles already edited (DO NOT re-verify — move to commit/push): {', '.join(sorted(_cicd_edited_files))}"
        parts.append(phase_line)
    return {"role": "user", "content": "\n\n".join(parts)}


# ── Memory pressure management (tier 2 + tier 3) ────────────────────
#
# Python's default allocator (pymalloc) does NOT return freed memory to the
# OS well — repeated alloc/free of large objects (like per-turn tokenizer
# encodes) fragments the heap and keeps RSS high even when Python has
# garbage-collected the objects themselves. Long-running agent sessions hit
# OOM because of this fragmentation, not because of a live leak.
#
# `_release_memory()` runs at a natural seam (end of each turn) to:
#   1. `gc.collect()` — finalize any Python garbage now
#   2. `libc.malloc_trim(0)` — ask glibc to return unused arenas to the OS
# and logs `mem.trim released=N vmrss_mb=M` so we can verify it's working.
#
# `_check_memory_watermark()` reads /proc/self/status once per turn:
#   - VmRSS > _MEM_WARN_MB → log mem.watermark (caller may force summary)
#   - VmRSS > _MEM_HARD_MB → log mem.hard_limit, exit(2) cleanly (no OOM,
#                             no tmux death, no systemd scope cleanup)

_MEM_WARN_MB = int(os.environ.get("AGENT_MEM_WARN_MB", 8192))
_MEM_HARD_MB = int(os.environ.get("AGENT_MEM_HARD_MB", 12288))

try:
    _libc = ctypes.CDLL("libc.so.6")
    _MALLOC_TRIM_AVAILABLE = True
except (OSError, AttributeError):
    _libc = None
    _MALLOC_TRIM_AVAILABLE = False


def _read_vmrss_mb():
    """Read VmRSS from /proc/self/status and return MB. 0 on any error."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _release_memory(log):
    """Force a GC + malloc_trim and log the result.

    Cheap (~10ms typical) and idempotent. Intended to run at the end of each
    agent turn. Uses environment override ``AGENT_DISABLE_MEM_TRIM=1`` to
    skip for benchmarking / A-B testing the effect.
    """
    if os.environ.get("AGENT_DISABLE_MEM_TRIM") == "1":
        return
    gc.collect()
    released = 0
    if _MALLOC_TRIM_AVAILABLE:
        try:
            released = int(_libc.malloc_trim(0))
        except Exception:
            released = 0
    rss_mb = _read_vmrss_mb()
    log.info(
        "mem.trim released=%d vmrss_mb=%d trim_available=%s",
        released,
        rss_mb,
        _MALLOC_TRIM_AVAILABLE,
    )


def _check_memory_watermark(log):
    """Check current RSS against configured watermarks.

    Returns:
        ``"ok"``       — under ``_MEM_WARN_MB``
        ``"pressure"`` — over ``_MEM_WARN_MB`` (caller may take action)
        ``"abort"``    — over ``_MEM_HARD_MB`` (process exits before return)
    """
    rss_mb = _read_vmrss_mb()
    if rss_mb <= 0:
        return "ok"  # couldn't read — don't interfere
    if rss_mb > _MEM_HARD_MB:
        log.error(
            "mem.hard_limit vmrss_mb=%d (limit %d) — exiting session cleanly "
            "before OOM killer destroys the tmux scope",
            rss_mb,
            _MEM_HARD_MB,
        )
        # Emit the Bedrock spend summary before exit so operators still see
        # final spend even when the session is killed by this watermark.
        try:
            _log_bedrock_session_spend(log)
        except NameError:
            # Defined later in the module at import time this MAY be missing
            # under a reorder; fail soft rather than block the exit.
            pass
        sys.exit(2)
    if rss_mb > _MEM_WARN_MB:
        log.warning(
            "mem.watermark vmrss_mb=%d (warn %d) — memory pressure",
            rss_mb,
            _MEM_WARN_MB,
        )
        return "pressure"
    return "ok"


def _build_context(conversation_history, summary_state, initial_files, ctx_size, max_tokens, log,
                    max_messages_override=None):
    """Build the context window dynamically based on token budget.

    When messages are dropped and a summary exists, a synthetic user message
    is prepended containing the initial file contents and the progress summary.

    Args:
        max_messages_override: If set, cap messages to this count (used for
            context reduction on 500 errors).

    Returns (messages_to_send, oldest_included_idx).
    """
    global _TOOLS_TOKENS
    if _TOOLS_TOKENS is None:
        _TOOLS_TOKENS = _estimate_tools_tokens()

    reserved_output = min(max_tokens, ctx_size // 2)
    # Safety margin: ctx_size//4 gives ~25% headroom.  This accounts for
    # chat-template overhead (~4 special tokens per message boundary, system
    # prompt formatting for tools) that our tokenizer doesn't count.
    budget = ctx_size - _TOOLS_TOKENS - reserved_output - max(512, ctx_size // 4)
    effective_max = max_messages_override if max_messages_override else _MAX_CONTEXT_MESSAGES

    context_msg = None
    context_tokens = 0
    if summary_state["text"]:
        context_msg = _build_context_footnote(summary_state["text"], initial_files)
        context_tokens = _estimate_tokens(context_msg)

        # If summary takes a large share of the budget, reduce message count
        # rather than truncating the summary — the summary IS the agent's memory
        # of all prior work and must be preserved intact.
        if context_tokens > budget * 0.8:
            # Summary alone exceeds the budget — condense it
            log.warning("Summary exceeds 80%% of budget (%d/%d tokens) — condensing", context_tokens, budget)
            summary_state["text"] = _condense_summary(summary_state["text"], log)
            # Rebuild context_msg with condensed summary
            context_msg = _build_context_footnote(summary_state["text"], initial_files)
            context_tokens = _estimate_tokens(context_msg)
            log.info("Condensed summary, context now %d tokens", context_tokens)
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
    for i in range(len(conversation_history) - 1, -1, -1):
        if len(selected) >= effective_max:
            break
        msg_tokens = _estimate_tokens(conversation_history[i])
        needed = used + msg_tokens
        if context_msg and i > 0:
            needed += context_tokens
        if needed > budget:
            break
        selected.append(conversation_history[i])
        used += msg_tokens
        oldest_idx = i

    selected.reverse()

    if oldest_idx > 0 and context_msg:
        selected.insert(0, context_msg)
        used += context_tokens
    elif not any(m["role"] == "user" for m in selected):
        if initial_files:
            selected.insert(0, {"role": "user", "content": initial_files + "\n\nContinue."})
        else:
            for i in range(oldest_idx - 1, -1, -1):
                if conversation_history[i]["role"] == "user":
                    selected.insert(0, conversation_history[i])
                    break

    log.debug("Context budget: %d/%d tokens, %d messages, oldest_idx=%d, has_context_msg=%s",
              used, budget, len(selected), oldest_idx, context_msg is not None)
    return selected, oldest_idx


def _maybe_resummarize(conversation_history, summary_state, oldest_idx, log, force=False):
    """Check if enough messages have fallen out of the window to warrant a new summary."""
    unsummarized = oldest_idx - summary_state["up_to"]

    if not force and unsummarized < _SUMMARY_THRESHOLD:
        return False

    new_messages = conversation_history[summary_state["up_to"]:oldest_idx]

    # Nothing new to summarize — keep existing summary intact
    if not new_messages:
        log.debug("Resummarize skipped: 0 new messages (up_to=%d, oldest_idx=%d)",
                  summary_state["up_to"], oldest_idx)
        return False

    _emit("on_summary_start", len(new_messages))
    summary = _generate_summary(summary_state["text"], new_messages, log)
    summary_state["text"] = _condense_summary(summary, log)
    summary_state["up_to"] = oldest_idx
    _emit("on_summary_done")
    return True


# ── Logger setup ──────────────────────────────────────────────────────

class _VerboseConsoleFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        cb = globals().get("_cb")
        return bool(cb and getattr(cb, "verbose", False))


def _setup_logger():
    """Create a structured logger with levels, rotation, and console output."""
    log_dir_override = _config.get("log_dir")
    if log_dir_override:
        history_dir = os.path.join(os.getcwd(), log_dir_override)
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

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    console_handler.addFilter(_VerboseConsoleFilter())
    logger.addHandler(console_handler)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10*1024*1024, backupCount=5)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(file_handler)

    error_handler = logging.handlers.RotatingFileHandler(
        error_log_path, maxBytes=5*1024*1024, backupCount=3)
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(logging.Formatter('%(asctime)s ERROR %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(error_handler)

    return logger, log_path, error_log_path


# ── Conversation checkpoints (for -c continue) ──────────────────────

_AGENT_DIR = os.path.join(os.getcwd(), ".agent")
_STATE_DIR = os.path.join(_AGENT_DIR, "state")
_HISTORY_DIR = os.path.join(_AGENT_DIR, "history")


def _state_path(*parts):
    """Return a path inside .agent/state under the current working directory."""
    return os.path.join(_STATE_DIR, *parts)


def _ensure_agent_dirs():
    """Create .agent/state and .agent/history on first use."""
    os.makedirs(_STATE_DIR, exist_ok=True)
    os.makedirs(_HISTORY_DIR, exist_ok=True)


_ensure_agent_dirs()
_CHECKPOINT_PATH = _state_path("conversation_checkpoint.json")


def _strip_checkpoint_reads(conversation_history):
    """Remove tool results that contain conversation_checkpoint.json content.

    Prevents recursive self-inclusion: when the agent reads its own checkpoint
    file, the full conversation history gets embedded in the tool result, causing
    exponential growth on each subsequent checkpoint save.
    """
    cleaned = []
    for msg in conversation_history:
        if msg.get("role") == "tool":
            content = msg.get("content", "")
            if isinstance(content, str) and "conversation_checkpoint.json" in content and len(content) > 10_000:
                msg = dict(msg, content="[conversation_checkpoint.json content stripped — internal runtime file]")
        cleaned.append(msg)
    return cleaned


def _save_checkpoint(conversation_history, summary_state, turn, initial_files):
    """Save conversation state so a crashed cycle can be resumed with -c."""
    try:
        checkpoint = {
            "conversation_history": _strip_checkpoint_reads(conversation_history),
            "summary_state": summary_state,
            "turn": turn,
            "initial_files": initial_files,
        }
        os.makedirs(os.path.dirname(_CHECKPOINT_PATH), exist_ok=True)
        with open(_CHECKPOINT_PATH, "w", encoding="utf-8") as f:
            json.dump(checkpoint, f)
    except Exception:
        pass  # best-effort, don't crash the agent


def _load_checkpoint():
    """Load a saved conversation checkpoint. Returns (history, summary, turn, files) or None."""
    if not os.path.exists(_CHECKPOINT_PATH):
        return None
    try:
        with open(_CHECKPOINT_PATH, encoding="utf-8", errors="replace") as f:
            cp = json.load(f)
        return (
            cp["conversation_history"],
            cp["summary_state"],
            cp.get("turn", 0),
            cp.get("initial_files"),
        )
    except Exception:
        return None


def _delete_checkpoint():
    """Remove checkpoint after a clean exit."""
    try:
        if os.path.exists(_CHECKPOINT_PATH):
            os.remove(_CHECKPOINT_PATH)
    except Exception:
        pass


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
    body_tokens = sum(_estimate_tokens(m) for m in history) if history else 0
    summary_text = summary_state.get("text", "") or ""
    summary_tokens = _estimate_tokens({"role": "system", "content": summary_text}) if summary_text else 0
    total = body_tokens + summary_tokens
    pct = total / ctx_size if ctx_size else 0.0
    bar = theme.bar(pct, width=width)
    pct_str = f"{pct*100:.1f}%"
    return (
        f"{bar} {pct_str}\n"
        f"  history: {body_tokens} tokens in {len(history)} messages\n"
        f"  summary: {summary_tokens} tokens\n"
        f"  budget:  {total} / {ctx_size} tokens"
    )


def _pick_model_interactive(current_model, base_url):
    """Interactive model picker. Returns the chosen model id or None."""
    models = _list_available_models(base_url)
    if not models:
        _emit("on_notice", "warn", theme.c(theme.ROSE, f"Could not list models from {base_url}/v1/models"))
        return None
    _emit("on_notice", "info", theme.c(theme.SKY, f"Available models at {base_url}:"))
    for i, m in enumerate(models, 1):
        marker = theme.c(theme.MINT, " *") if m == current_model else "  "
        _emit("on_notice", "info", f"{marker} {i}. {m}")
    try:
        choice = input("Pick a model number (blank to cancel): ").strip()
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


def _log_bedrock_session_spend(log):
    """Emit an INFO line summarizing today's Bedrock spend per role.

    Fires at session end so operators grep-ing the session log see an
    at-a-glance spend snapshot even when ``bedrock.cost.tick`` is at
    DEBUG (plan § 15.75 telemetry default). Silent when no Bedrock
    backend is in use.
    """
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


def run_agent_interactive(initial_prompt=None, auto=False, continue_mode=False, *, cb=None, tui=False, verbose=False, result_file=None):
    """Interactive agent that maintains conversation history.

    When `tui=True`, a prompt_toolkit front-end (tui.TuiSession) owns the
    input prompt and swaps the default TerminalCallbacks for TuiCallbacks
    so the bottom toolbar reflects live model/message/ctx state. The TUI
    is optional — if prompt_toolkit isn't installed, a clean ImportError
    is raised at TuiSession construction time.
    """

    ctx_size = _config["context"]["ctx_size"]
    max_tokens = _config["context"]["max_tokens"]
    gen = _config["generation"]

    log, log_path, error_log_path = _setup_logger()

    # OTLP telemetry: init once per session (no-op if disabled / SDK missing).
    _telemetry_on = telemetry.init()
    t0 = time.time()

    # Install the UI callback handle for this session
    global _cb, _cb_log
    _cb = cb if cb is not None else TerminalCallbacks(verbose=verbose)
    _cb_log = log
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

    model_name = _main_backend.model or _config["llm"]["model"]
    _emit(
        "on_boot_progress",
        f"checking main backend — {getattr(_main_backend, 'kind', '?')} {model_name} @ {getattr(_main_backend, 'base_url', '?')}",
    )
    ok, detail = _main_backend.health()

    # Backend banner (plan task 1.5) — one-line log noting which kinds are active.
    log.info(
        "backends: main=%s(%s@%s) summary=%s(%s@%s)",
        _main_backend.kind, _main_backend.model, _main_backend.base_url,
        _summary_backend.kind, _summary_backend.model, _summary_backend.base_url,
    )

    # Auto-detect context size from the main backend. Apply 85% buffer,
    # then hard-cap at 85K to avoid llama_decode crashes.
    _CTX_HARD_CAP = 85_000
    detected = _main_backend.detect_ctx_size()
    if detected:
        ctx_size = min(int(detected * 0.85), _CTX_HARD_CAP)
        _config["context"]["ctx_size"] = ctx_size
        log.info("Auto-detected main model n_ctx=%d, using ctx_size=%d (85%% / cap %dk)",
                 detected, ctx_size, _CTX_HARD_CAP // 1000)

    # Probe summary health BEFORE on_session_start so the banner can
    # render main + summary indicators together in a single header.
    summary_cfg = _config["summary"]
    summary_ok = False
    summary_detail = "disabled"
    summary_url = getattr(_summary_backend, "base_url", "") if _summary_backend else ""
    if summary_cfg["enabled"]:
        _emit(
            "on_boot_progress",
            f"checking summary backend — {getattr(_summary_backend, 'kind', '?')} {getattr(_summary_backend, 'model', '?')} @ {summary_url}",
        )
        try:
            summary_ok, summary_detail = _summary_backend.health()
        except (requests.ConnectionError, requests.Timeout):
            summary_ok, summary_detail = False, "unreachable"

    _emit("on_session_start", {
        "version": __version__,
        "sha": _git_short_sha(),
        "api_ok": ok,
        "api_detail": detail,
        "base_url": getattr(_main_backend, "base_url", None) or BASE_URL,
        "model": model_name,
        "main_kind": getattr(_main_backend, "kind", ""),
        "summary_enabled": summary_cfg["enabled"],
        "summary_ok": summary_ok,
        "summary_detail": summary_detail,
        "summary_base_url": summary_url,
        "summary_model": getattr(_summary_backend, "model", "") if _summary_backend else "",
        "summary_kind": getattr(_summary_backend, "kind", "") if _summary_backend else "",
        "ctx_size": ctx_size,
        "max_turns": _MAX_TURNS,
        "log_path": log_path,
        "error_log_path": error_log_path,
    })

    log.info("Session started | ctx_size=%d max_turns=%d temperature=%.1f max_tokens=%d",
             ctx_size, _MAX_TURNS, gen["temperature"], max_tokens)
    log.info("Tools registered: %s", [t["function"]["name"] for t in tools])

    # Instantiate the async summarizer from the already-probed state above.
    _async_summarizer = None
    if summary_cfg["enabled"]:
        if summary_ok:
            # Auto-detect summary model context size
            summary_ctx = _summary_backend.detect_ctx_size()
            if summary_ctx:
                _config["summary"]["ctx_size"] = int(summary_ctx * 0.85)
                log.info("Auto-detected summary model n_ctx=%d, using %d (85%%)",
                         summary_ctx, _config["summary"]["ctx_size"])
            _async_summarizer = AsyncSummarizer(_config, log)
            log.debug("Async summarizer enabled → %s", summary_url)
            _emit("on_summarizer_status", "online", summary_url)
        elif summary_detail in ("unreachable", "timeout"):
            # Connection-level failure → "offline" (matches pre-refactor).
            log.warning("Summary endpoint unreachable at %s, using main model for summaries",
                        summary_url)
            _emit("on_summarizer_status", "offline", summary_url)
        else:
            log.warning("Summary endpoint unhealthy (%s), using main model for summaries",
                        summary_detail)
            _emit("on_summarizer_status", "unhealthy", summary_detail)

    # ── Continue mode: resume from checkpoint ──
    start_turn = 0
    if continue_mode:
        cp = _load_checkpoint()
        if cp:
            conversation_history, summary_state, start_turn, initial_files = cp
            log.info("CONTINUE: resuming from checkpoint (turn %d, %d messages)",
                     start_turn, len(conversation_history))
            # Cap summary from old checkpoints that may have bloated summaries
            if summary_state.get("text"):
                summary_state["text"] = _condense_summary(summary_state["text"], log)
            _emit("on_continue_resumed", start_turn, len(conversation_history))
            # Add a resume nudge so the model knows it's continuing
            conversation_history.append({"role": "user", "content":
                "Continue where you left off. The session was interrupted — "
                "pick up from your current phase and finish the cycle."})
            result = run_agent_single(conversation_history, summary_state, initial_files, log,
                                      gen["temperature"], gen["top_p"], gen["top_k"],
                                      gen["presence_penalty"], max_tokens, ctx_size,
                                      start_turn=start_turn,
                                      async_summarizer=_async_summarizer)
            if auto:
                cleanup_temp_sessions()
                _delete_checkpoint()
                log.info("Session ended (continue mode) | %d messages", len(conversation_history))
                if _telemetry_on:
                    telemetry.record_cycle(status="continue_completed", duration_s=time.time() - t0)
                    telemetry.shutdown()
                _log_bedrock_session_spend(log)
                return
            # Fall through to interactive loop if not auto
        else:
            _emit("on_continue_none")
            log.debug("CONTINUE: no checkpoint found, starting fresh")

    if not continue_mode:
        # Check if the current cycle was already committed — bump if so
        _auto_increment_cycle(log)

    conversation_history = conversation_history if continue_mode and start_turn > 0 else []
    summary_state = summary_state if continue_mode and start_turn > 0 else {"text": "", "up_to": 0}
    initial_files = initial_files if continue_mode and start_turn > 0 else None

    # ── TUI front-end (default in interactive mode) ──
    # Now that history / summary / initial_files have stable identities,
    # instantiate the prompt_toolkit session and swap the UI callback.
    # If prompt_toolkit isn't installed we fall back silently to plain
    # input() with a one-line notice so the default TUI path doesn't
    # break environments that haven't installed the optional dependency.
    tui_session = None
    if tui and not auto:
        import tui as _tuimod
        if _tuimod._AVAILABLE:
            tui_session = _tuimod.TuiSession(
                history=conversation_history,
                summary_state=summary_state,
                config=_config,
                ctx_size=ctx_size,
                cb=_cb,
                estimate_tokens=_estimate_tokens,
            )
            _cb = _tuimod.TuiCallbacks(tui_session, verbose=getattr(_cb, "verbose", False))
        else:
            _emit("on_notice", "warn",
                  "prompt_toolkit not installed — using plain prompt. "
                  "`pip install prompt_toolkit` (or pass --no-tui to silence).")

    if initial_prompt and not (continue_mode and start_turn > 0):
        _emit("on_user_message", initial_prompt)
        try:
            expanded, files, err = _expand_file_refs(initial_prompt)
            if err:
                _emit("on_error", err)
                return
        except Exception as e:
            _emit("on_error", f"Unexpected error expanding initial prompt: {e}")
            return
        if files:
            initial_files = files
        # Extract <pinned>...</pinned> blocks — these survive summarization
        global _pinned_instructions
        expanded, pinned = _extract_pinned(expanded)
        if pinned:
            _pinned_instructions = pinned
            log.info("Pinned instructions extracted (%d chars)", len(pinned))
        conversation_history.append({"role": "user", "content": expanded})
        log.debug("USER: %s", expanded)
        result = run_agent_single(conversation_history, summary_state, initial_files, log,
                                  gen["temperature"], gen["top_p"], gen["top_k"],
                                  gen["presence_penalty"], max_tokens, ctx_size,
                                  async_summarizer=_async_summarizer)

        if auto:
            if result == "cancelled":
                result = _handle_auto_guidance(conversation_history, summary_state, initial_files, log,
                                               gen, max_tokens, ctx_size, _async_summarizer,
                                               _telemetry_on, t0)
            cleanup_temp_sessions()
            _delete_checkpoint()
            log.info("Session ended (auto mode) | %d messages in history", len(conversation_history))
            if _telemetry_on:
                telemetry.record_cycle(status="auto_completed", duration_s=time.time() - t0)
                telemetry.shutdown()
            _log_bedrock_session_spend(log)
            if result_file:
                last_assistant_msg = ""
                for msg in reversed(conversation_history):
                    if msg.get("role") == "assistant" and msg.get("content"):
                        last_assistant_msg = msg["content"]
                        break
                with open(result_file, "w", encoding="utf-8") as f:
                    f.write(last_assistant_msg)
            return
    while True:
        try:
            if tui_session is not None:
                user_input = tui_session.prompt()
            else:
                user_input = input("\nYou: ").strip()
        except EOFError:
            break
        except KeyboardInterrupt:
            _emit("on_notice", "info", "\n\nGoodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ["exit", "quit"]:
            _emit("on_notice", "info", "Goodbye!")
            break
        if user_input.startswith("/"):
            def _refresh_cb_log(new_log):
                globals()["_cb_log"] = new_log
            ctx = SimpleNamespace(
                conversation_history=conversation_history,
                summary_state=summary_state,
                initial_files=initial_files,
                async_summarizer=_async_summarizer,
                cb=_cb,
                log=log,
                log_path=log_path,
                ctx_size=ctx_size,
                config=_config,
                base_url=BASE_URL,
                setup_logger=_setup_logger,
                pick_model=_pick_model_interactive,
                render_context_bar=_render_context_bar,
                refresh_cb_log=_refresh_cb_log,
            )
            if _commands.handle_command(user_input, ctx):
                # /clear may have rotated log and initial_files — pull them back
                initial_files = ctx.initial_files
                log = ctx.log
                log_path = ctx.log_path
                continue

        expanded, files, err = _expand_file_refs(user_input)
        if err:
            _emit("on_error", err)
            continue
        if files:
            initial_files = files

        conversation_history.append({"role": "user", "content": expanded})
        log.debug("USER: %s", expanded)

        run_agent_single(conversation_history, summary_state, initial_files, log,
                         gen["temperature"], gen["top_p"], gen["top_k"],
                         gen["presence_penalty"], max_tokens, ctx_size,
                         async_summarizer=_async_summarizer)

    if tui_session is not None:
        tui_session.close()
    cleanup_temp_sessions()
    _delete_checkpoint()
    log.info("Session ended | %d messages in history", len(conversation_history))
    if _telemetry_on:
        telemetry.record_cycle(status="completed", duration_s=time.time() - t0)
    _log_bedrock_session_spend(log)

    if result_file:
        last_assistant_msg = ""
        for msg in reversed(conversation_history):
            if msg.get("role") == "assistant" and msg.get("content"):
                last_assistant_msg = msg["content"]
                break
        with open(result_file, "w", encoding="utf-8") as f:
            f.write(last_assistant_msg)

    if _telemetry_on:
        telemetry.shutdown()



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
            import telemetry
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
                initial_files = files_g
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

def run_agent_single(conversation_history: list, summary_state: dict, initial_files,
                     log: logging.Logger,
                     temperature=_DEFAULT_CONFIG["generation"]["temperature"],
                     top_p=_DEFAULT_CONFIG["generation"]["top_p"],
                     top_k=_DEFAULT_CONFIG["generation"]["top_k"],
                     presence_penalty=_DEFAULT_CONFIG["generation"]["presence_penalty"],
                     max_tokens=_DEFAULT_CONFIG["context"]["max_tokens"],
                     ctx_size=_DEFAULT_CONFIG["context"]["ctx_size"],
                     start_turn=0, async_summarizer=None):
    """Run the agentic loop with turn limits and wind-down."""

    turn = start_turn

    # Track repeated tool failures to break infinite loops
    _recent_tool_errors = []  # list of (tool_name, error_snippet)
    _REPEAT_THRESHOLD = 3    # inject forced think after this many identical failures

    # Track consecutive text-only responses (no tool calls).
    # Smaller models sometimes "think aloud" without calling a tool, intending
    # to continue on the next turn.  Auto-nudge up to _MAX_TEXT_ONLY times
    # before treating it as a real stop signal.
    _consecutive_text_only = 0

    # Total nudge budget across the session.  Prevents infinite oscillation
    # where a weak tool call resets the consecutive counter but the model
    # never makes substantive progress.
    _total_nudges = 0

    # Detect degenerate text loops — model repeating the same output.
    # Store hashes of recent text-only responses; bail if too many match.
    _recent_text_hashes = []
    _TEXT_LOOP_THRESHOLD = 3

    # Read-only tools: calls to these don't reset the consecutive text-only
    # counter because they don't represent substantive progress.
    _READ_ONLY_TOOLS = {"think", "search_files", "read_pdf", "task_tracker"}

    # After 'git push', allow a few more turns for TRACK work (results file,
    # progress row, issue comments) before stopping on text-only response.
    _cycle_persisted = False
    _cycle_persisted_turn = None
    _CYCLE_GRACE_TURNS = 7
    # Hard cap: even with ongoing tool calls, end the cycle this many turns
    # after persist.  Prevents post-TRACK drift into a second PERCEIVE.
    _CYCLE_HARD_STOP_TURNS = 15

    # Track whether any commit has been made.  Completion signals are ignored
    # until a commit lands — prevents the agent from declaring "done" before
    # any work is actually persisted.
    _has_committed = False

    # Reviewer-role persistence signal.  Reviewers rarely commit code; their
    # persistent outputs are `gh pr review` verdicts and appends to
    # CICD/reviews.md.  Tracked separately so completion signals are honored
    # once a verdict has actually landed.
    _has_reviewer_persisted = False

    # Track whether any file has been written/edited.  If no edit by turn 30,
    # inject a nudge telling the agent to start coding or declare null result.
    _has_edited = False
    _EDIT_DEADLINE_TURN = 20
    _edit_nudge_sent = False

    # Reviewer sessions rarely make code edits (they verify and merge), so the
    # edit-deadline nudge is a false positive for that role.  Detect by
    # scanning the initial prompt for the reviewer-template marker.
    _is_reviewer_role = any(
        isinstance(m.get("content"), str) and "CICD Reviewer" in m["content"]
        for m in conversation_history[:2]
    )

    # cycle 86 (issue #425): CICD-specific guards (e.g. cycle 44 requiring
    # `Closes #N`) only apply in the self-improvement builder loop, not in
    # general agent sessions on other repos.  Detect by the same strategy as
    # _is_reviewer_role — the builder template always opens with
    # "CICD Improvement Loop — Builder".
    _is_cicd_builder = any(
        isinstance(m.get("content"), str) and "CICD Improvement Loop" in m["content"]
        for m in conversation_history[:2]
    )

    # Detect tool-call loops: same command signature repeated N times.
    _recent_tool_sigs = []  # list of (frozenset of (name, args_hash)) tuples
    _TOOL_LOOP_THRESHOLD = 3  # inject correction after 3 identical batches

    # Semantic result-loop detection: same tool returning same result despite
    # different arguments.  Catches cases where the batch-signature detector
    # misses because args vary slightly each time.
    _recent_tool_results = []  # list of (func_name, result_hash) tuples
    _RESULT_LOOP_WINDOW = 8
    _RESULT_LOOP_THRESHOLD = 3

    # CICD phase tracking — module-level globals so _build_context_message()
    # can inject them into every context window, surviving summary compression.
    global _cicd_phase_state, _cicd_issue_number, _cicd_pr_number, _cicd_branch, _cicd_edited_files, _cicd_worktree_path
    _cicd_phase_state = {
        "perceive": False,
        "probe": False,
        "decide": False,
        "plan": False,
        "implement": False,
        "verify": False,
        "track": False,
    }
    _cicd_issue_number = None
    _cicd_pr_number = None
    _cicd_branch = None
    _cicd_think_used = False  # reset each cycle; set True when think() is called
    _cicd_edited_files = set()  # reset each cycle
    _cicd_worktree_path = None  # reset each cycle
    _cicd_pr_ready_called = False  # tracks whether `gh pr ready` was called before merge
    _cicd_issue_view_called = False  # tracks whether `gh issue view` was called before merge (PRE-MERGE CHECK)

    _async_summarizer = async_summarizer

    while True:
        turn += 1
        _turn_t0 = time.monotonic()
        _turn_in_tokens = 0
        _turn_out_tokens = 0

        # ── Memory pressure management (tiers 2 + 3) ──
        # Tier 2: force gc + glibc malloc_trim so pymalloc arenas freed during
        # the prior turn actually return to the OS. Fights heap fragmentation.
        # Tier 3: measure resulting RSS; exit cleanly if over hard limit BEFORE
        # the OOM killer destroys the whole tmux/systemd scope. The watermark
        # return value is currently unused (callers only react to the hard
        # exit path); wired as side-effect-only so the dead-locals guard
        # (cycle 0013) doesn't complain.
        _release_memory(log)
        _check_memory_watermark(log)

        # ── Edit deadline nudge ──
        # Reviewers don't edit — suppress the nudge for that role.
        if (turn == _EDIT_DEADLINE_TURN and not _has_edited
                and not _edit_nudge_sent and _NUDGE_ENABLED
                and not _is_reviewer_role):
            _edit_nudge_sent = True
            _edit_nudge = (
                f"[SYSTEM: You have spent {turn} turns without making a code change. "
                f"Create your worktree NOW and make your edit, or declare a null result. "
                f"Do not continue investigating — act immediately.]"
            )
            conversation_history.append({"role": "user", "content": _edit_nudge})
            log.warning("Edit deadline: %d turns with no file edit — nudging", turn)

        # ── Wind-down and overtime warnings ──
        remaining = _MAX_TURNS - turn
        wind_down_msg = None
        if 0 < remaining <= _WIND_DOWN_TURNS:
            wind_down_msg = (
                f"[SYSTEM: {remaining} turns remaining before overtime. "
                f"Begin wrapping up — save your progress (CONSOLIDATE), "
                f"commit your work (PERSIST), and stop. "
                f"Do not start new tasks.]"
            )
            log.info("Wind-down: %d turns remaining", remaining)
        elif remaining <= 0:
            overtime = -remaining
            wind_down_msg = (
                f"[SYSTEM: You are {overtime} turns past the turn limit. "
                f"Finish what you are doing immediately — CONSOLIDATE and PERSIST now. "
                f"Do not start anything new.]"
            )
            log.warning("Overtime: %d turns past limit (%d)", overtime, _MAX_TURNS)
            # Hard cap: never exceed 2x the turn limit regardless of state
            if overtime >= _MAX_TURNS:
                log.error("Hard overtime cap reached (%d turns) — force stopping", turn)
                return "done"

        # Harvest any completed async summary before building context
        if _async_summarizer and _async_summarizer.harvest(summary_state):
            log.info("Harvested async summary")
            _emit("on_summary_ready")

        # Build context window, with overflow reduction loop
        _ctx_max_messages = None  # None = use default _MAX_CONTEXT_MESSAGES
        _CTX_REDUCE_MAX = 10     # max number of message-reduction attempts

        for _ctx_attempt in range(_CTX_REDUCE_MAX + 1):
            telemetry.record_context_size(ctx_size)
            messages_to_send, oldest_idx = _build_context(
                conversation_history, summary_state, initial_files, ctx_size, max_tokens, log,
                max_messages_override=_ctx_max_messages)

            # Summarize dropped messages: async (background) or sync (blocking)
            if _async_summarizer:
                unsummarized = oldest_idx - summary_state["up_to"]
                if unsummarized >= _SUMMARY_THRESHOLD and not _async_summarizer.is_running:
                    new_messages = conversation_history[summary_state["up_to"]:oldest_idx]
                    if new_messages:
                        _async_summarizer.kick(summary_state["text"], new_messages, oldest_idx)
                        log.info("Kicked async summary for %d messages", len(new_messages))
                        _emit("on_notice", "info", "[background summarization started]")
            elif _maybe_resummarize(conversation_history, summary_state, oldest_idx, log):
                messages_to_send, oldest_idx = _build_context(
                    conversation_history, summary_state, initial_files, ctx_size, max_tokens, log,
                    max_messages_override=_ctx_max_messages)

            # Inject wind-down as a system message at the end of context
            if wind_down_msg:
                messages_to_send.append({"role": "user", "content": wind_down_msg})

            log.info("--- Turn %d/%d | sending %d messages (history has %d total)",
                     turn, _MAX_TURNS, len(messages_to_send), len(conversation_history))

            # Call the model (streaming).
            # Plan task 1.5: model comes from the main backend.
            request_body = {
                "model": _main_backend.model or _config["llm"]["model"],
                "messages": messages_to_send,
                "temperature": temperature,
                "top_p": top_p,
                "top_k": top_k,
                "presence_penalty": presence_penalty,
                "max_tokens": max_tokens,
                "chat_template_kwargs": {"enable_thinking": False},
                "cache_prompt": True,
                "tools": tools,
                "tool_choice": "auto",
                "stream": True,
                # OpenAI streaming protocol (which llama.cpp implements) only
                # emits a final usage chunk when `stream_options.include_usage`
                # is set. Without this flag, the llamacpp/gemma path produces
                # no token telemetry. Bedrock builds usage server-side and
                # ignores this flag, so it is safe to include unconditionally.
                "stream_options": {"include_usage": True},
            }

            if (
                getattr(_main_backend, "kind", None) == "bedrock"
                and _config.get("bedrock", {}).get("adaptive_max_tokens", True)
            ):
                _complexity = _classify_turn_complexity(messages_to_send)
                request_body["max_tokens"] = _get_adaptive_max_tokens(_main_backend.model, _complexity)

            try:
                response = _llm_request(log, json=request_body, stream=True, timeout=(30, 300))
                # Only the legacy Response shape exposes ``status_code``;
                # a generator (Bedrock backend) doesn't. Skip the log line in
                # that case — the backend emits its own latency/ok telemetry.
                if hasattr(response, "status_code"):
                    log.info("Response status: %d", response.status_code)
                break  # success — exit the reduction loop
            except ContextOverflowError:
                if _ctx_attempt >= _CTX_REDUCE_MAX:
                    log.error("Context overflow: still failing after %d reductions", _CTX_REDUCE_MAX)
                    _emit("on_error", "Error: context overflow — could not fit in server context window")
                    return "error"
                current_count = len(messages_to_send)
                if current_count <= 2:
                    # Already at minimum messages — aggressively truncate the summary
                    # instead of trying to reduce messages further
                    if summary_state["text"]:
                        old_len = len(summary_state["text"])
                        summary_state["text"] = summary_state["text"][:old_len // 2]
                        log.warning("Context overflow (attempt %d/%d): at min messages, "
                                    "truncating summary from %d to %d chars",
                                    _ctx_attempt + 1, _CTX_REDUCE_MAX, old_len, len(summary_state["text"]))
                        _emit("on_notice", "info", "[Context overflow — truncating summary to fit]")
                    else:
                        log.error("Context overflow with no summary and minimal messages — cannot reduce further")
                        _emit("on_error", "Error: context overflow — cannot reduce further")
                        return "error"
                else:
                    # Reduce: cap messages to current count minus 2 (drop oldest pair)
                    _ctx_max_messages = max(2, current_count - 2)
                    log.warning("Context overflow (attempt %d/%d): reducing from %d to max %d messages",
                                _ctx_attempt + 1, _CTX_REDUCE_MAX, current_count, _ctx_max_messages)
                    _emit("on_context_recovery")
                    # Force a resummarize with the tighter window so dropped messages aren't lost
                    _maybe_resummarize(conversation_history, summary_state, oldest_idx, log, force=True)
                continue
            except requests.exceptions.RequestException as e:
                log.error("Request failed after retries: %s", e)
                telemetry.record_error(kind=type(e).__name__)
                _emit("on_error", f"Error calling server: {e}")
                return "error"

        # Accumulate streamed response
        content_parts = []
        tool_calls_by_index = {}
        printed_header = False
        receiving_tools = False
        _stream_t0 = time.monotonic()
        _stream_deadline = _stream_t0 + 600  # 10 minute wall-clock cap
        status = StreamStatus(emit=_emit)
        status.start("\nAssistant: ")
        renderer = _ReasoningRenderer(lambda t: _emit("on_stream_chunk", t))

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
                for chunk in _iter_stream_chunks(response):
                    check_cancelled()
                    if time.monotonic() > _stream_deadline:
                        log.warning("Streaming wall-clock deadline exceeded (600s) — aborting response")
                        _safe_close(response)
                        break

                    # Capture per-chunk token usage. For the llamacpp path,
                    # the final OpenAI streaming chunk carries `usage` ONLY
                    # when the request body sets
                    # `stream_options: {include_usage: true}` (see request_body
                    # construction above). Bedrock builds the usage dict
                    # server-side and emits it in the same shape regardless.
                    # Record before the no-choices skip below so we don't drop it.
                    _usage = chunk.get("usage") if isinstance(chunk, dict) else None
                    if _usage:
                        _u_model = request_body.get("model") or ""
                        _u_in = _usage.get("prompt_tokens") or _usage.get("input_tokens") or 0
                        _u_out = _usage.get("completion_tokens") or _usage.get("output_tokens") or 0
                        _u_backend = getattr(_main_backend, "kind", None)
                        if _u_in:
                            telemetry.record_tokens(_u_model, "prompt", int(_u_in), backend=_u_backend)
                            _turn_in_tokens += int(_u_in)
                        if _u_out:
                            telemetry.record_tokens(_u_model, "completion", int(_u_out), backend=_u_backend)
                            _turn_out_tokens += int(_u_out)
                    choices = chunk.get("choices")
                    if not choices:
                        continue  # skip usage/stats chunks
                    delta = choices[0].get("delta", {})

                    if delta.get("content"):
                        if not printed_header:
                            status.first_token()
                            printed_header = True
                        renderer.feed(delta["content"])
                        content_parts.append(delta["content"])
                        status.count_token()

                    if delta.get("tool_calls"):
                        if not receiving_tools:
                            receiving_tools = True
                            if printed_header:
                                _emit("on_notice", "info", "")
                                status = StreamStatus(emit=_emit)
                                status.start(f"{DIM}  preparing tool calls ")
                        for tc_delta in delta["tool_calls"]:
                            idx = tc_delta["index"]
                            if idx not in tool_calls_by_index:
                                tool_calls_by_index[idx] = {
                                    "id": tc_delta.get("id", ""),
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            tc = tool_calls_by_index[idx]
                            if tc_delta.get("id"):
                                tc["id"] = tc_delta["id"]
                            if tc_delta.get("function", {}).get("name"):
                                tc["function"]["name"] = tc_delta["function"]["name"]
                            if tc_delta.get("function", {}).get("arguments"):
                                tc["function"]["arguments"] += tc_delta["function"]["arguments"]
        except CancelledError:
            renderer.flush()
            status.finish()
            _safe_close(response)
            _emit("on_cancelled", "streaming")
            log.info(
                "cancel.latency_ms latency_ms=%d site=backend.stream_chat backend=%s",
                int((time.monotonic() - _stream_t0) * 1000),
                _main_backend.kind,
            )
            log.info("CANCELLED during streaming")
            # Keep partial history so caller can inject user guidance
            return "cancelled"
        except requests.exceptions.RequestException as e:
            renderer.flush()
            status.finish()
            _safe_close(response)
            log.error("Streaming connection lost: %s", e)
            telemetry.record_error(kind=type(e).__name__)
            _emit("on_error", f"Streaming error: {e}")
            # Treat as empty response — the text-only handler will nudge or stop
        except Exception as e:
            renderer.flush()
            status.finish()
            _safe_close(response)
            log.error("Unexpected error during streaming: %s", e, exc_info=True)
            telemetry.record_error(kind=type(e).__name__)
            _emit("on_error", f"Streaming error: {e}")

        renderer.flush()
        if content_parts and not receiving_tools:
            _emit("on_notice", "info", "")
        status.finish()

        full_content = _THINK_TAG_RE.sub('', "".join(content_parts)).strip()
        _emit("on_assistant_text", full_content)
        tool_calls = [tool_calls_by_index[i] for i in sorted(tool_calls_by_index)] if tool_calls_by_index else []

        assistant_msg = {"role": "assistant", "content": full_content}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        conversation_history.append(assistant_msg)

        if full_content:
            log.debug("ASSISTANT: %s", full_content)

        # Verbose-only per-turn telemetry — fires once per turn iteration after
        # the assistant response is processed, BEFORE any early-return branches
        # (completion-signal stop, tool loops, etc.). Gated at the call site so
        # disabled mode pays only one bool check; SDK PeriodicExportingMetricReader
        # handles flushing — no force-flush per turn (issue #401).
        if telemetry.verbose_enabled():
            telemetry.record_turn(
                role="main",
                duration_s=time.monotonic() - _turn_t0,
                tool_calls=len(tool_calls),
                in_tokens=_turn_in_tokens,
                out_tokens=_turn_out_tokens,
                model=(_main_backend.model or _config["llm"]["model"]),
            )
        # Detect degenerate text loops (model repeating itself)
        if full_content:
            _text_hash = hashlib.md5(full_content.encode()).hexdigest()
            _recent_text_hashes.append(_text_hash)
            # Keep only recent entries
            if len(_recent_text_hashes) > 10:
                _recent_text_hashes[:] = _recent_text_hashes[-10:]
            _repeat_count = sum(1 for h in _recent_text_hashes if h == _text_hash)
            if _repeat_count >= _TEXT_LOOP_THRESHOLD:
                log.warning("Text loop detected: same output %d times — stopping",
                            _repeat_count)
                _emit("on_text_loop_detected", _repeat_count)
                return "done"

        # Hard cap on post-persist drift.  After cycle persisted (git push),
        # the agent must wrap TRACK within _CYCLE_HARD_STOP_TURNS.  Longer
        # runs mean it started a second PERCEIVE — ignore and stop.
        if _cycle_persisted:
            _grace_used = turn - (_cycle_persisted_turn or turn)
            if _grace_used >= _CYCLE_HARD_STOP_TURNS:
                log.info("Stopping: cycle persisted %d turns ago, hard cap reached", _grace_used)
                return "done"

        if not tool_calls:
            if not _NUDGE_ENABLED:
                log.info("Stopping: text-only response (no tool calls)")
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
            _has_persisted_work = (_has_committed
                                    or _cicd_phase_state.get("track", False)
                                    or _has_reviewer_persisted)
            # Cycle 39: regex patterns for natural paraphrases ("cycle 249 is complete")
            _completion_signal_patterns = (
                r"cycle\s+\S+\s+is\s+(now\s+)?complete",  # "cycle 249 is complete", "cycle N is now complete"
                r"cycle\s+is\s+now\s+complete",             # "cycle is now complete"
                r"improvement\s+cycle\s+\S*\s*is\s+complete",  # "improvement cycle is complete"
            )
            _fc_lower = full_content.lower() if full_content else ""
            _completion_matched = (
                (full_content and any(s in _fc_lower for s in _completion_signals))
                or (full_content and any(re.search(p, _fc_lower) for p in _completion_signal_patterns))
            )
            if _has_persisted_work and _completion_matched:
                log.info("Stopping: model signalled cycle completion (work persisted)")
                return "done"
            if not _has_persisted_work and _completion_matched:
                log.info("Ignoring completion signal — no persisted work yet, nudging to continue")

            # If the cycle already persisted (git push happened), allow a few
            # grace turns for TRACK work, then stop on text-only response.
            if _cycle_persisted:
                grace_used = turn - (_cycle_persisted_turn or turn)
                if grace_used >= _CYCLE_GRACE_TURNS:
                    log.info("Stopping: cycle persisted %d turns ago, grace period exhausted", grace_used)
                    return "done"
                log.info("Cycle persisted but grace period active (%d/%d turns) — nudging for TRACK work",
                        grace_used, _CYCLE_GRACE_TURNS)
            # Past turn limit + no tool use = end cycle immediately
            if turn > _MAX_TURNS:
                log.warning("Overtime + text-only response — ending cycle")
                _emit("on_overtime", "text_only")
                return "done"

            _consecutive_text_only += 1

            # Total nudge budget across the session.
            _total_nudges += 1
            if _total_nudges >= _MAX_TOTAL_NUDGES:
                log.info("Stopping: total nudge budget exhausted (%d/%d)",
                         _total_nudges, _MAX_TOTAL_NUDGES)
                return "done"

            if _consecutive_text_only >= _MAX_TEXT_ONLY:
                log.info("Stopping: %d consecutive text-only responses", _consecutive_text_only)
                return "done"

            # First text-only response: strip it from context and retry silently.
            # Leaving hallucinated content in history poisons subsequent turns —
            # the model builds on its fabricated answer instead of using tools.
            if _consecutive_text_only == 1:
                conversation_history.pop()  # remove the hallucinated assistant msg
                log.info("Hallucination guard: stripped text-only response, retrying")
                _emit("on_hallucination_stripped", "text_only")
                telemetry.record_hallucination()
                continue

            # Detect hallucinated file reads: model claims to have read a file
            # but _accessed_files doesn't show it.  Give a targeted correction.
            _hallucinated_read = _detect_hallucinated_read(full_content)[0]
            if _hallucinated_read:
                # Strip the hallucinated message and give a pointed correction
                conversation_history.pop()
                nudge = (
                    "You did NOT actually read that file — you hallucinated its contents. "
                    "You MUST call the file tool with action='read' to see what a file contains. "
                    "Do not guess or fabricate file contents. Use the tool now."
                )
                log.info("Hallucination guard: detected fabricated file read, correcting")
                _emit("on_hallucination_stripped", "file_read")
                telemetry.record_hallucination()
            elif (_cicd_branch and _cicd_edited_files
                  and not _cicd_pr_number and _cicd_issue_number):
                # Cycle 82 (runs 187+188 failure mode): builder edited files in
                # the worktree, ran tests, said "I'm done", but never ran the
                # commit + push + gh pr create sequence. Run 187 hit it after
                # `git push` succeeded; run 188 hit it before push too. Detection
                # widened from `_cycle_persisted` to `_cicd_edited_files` so the
                # nudge fires for both the "edits-but-no-commit" and "pushed-but-
                # no-PR" branches of the same failure.
                if _cycle_persisted:
                    _missing = "PR open"
                    _next_cmds = (
                        f"  cat > /tmp/pr-body-{_cicd_issue_number}.md << 'EOF'\n"
                        f"  Closes #{_cicd_issue_number}\n"
                        f"  <one-paragraph summary of what changed>\n"
                        f"  EOF\n"
                        f"  gh pr create --draft --base main --head {_cicd_branch} \\\n"
                        f"    --title 'CICD: <slug> (#{_cicd_issue_number})' \\\n"
                        f"    --body \"$(cat /tmp/pr-body-{_cicd_issue_number}.md)\"\n"
                    )
                else:
                    _missing = "commit + push + PR open"
                    _next_cmds = (
                        f"  cd <WORKTREE_ROOT>/{_cicd_branch.split('/', 1)[-1] if '/' in _cicd_branch else _cicd_branch}\n"
                        f"  git add <edited files>\n"
                        f"  git commit -m 'CICD <N> (#{_cicd_issue_number}): <what>'\n"
                        f"  git push -u origin {_cicd_branch}\n"
                        f"  cat > /tmp/pr-body-{_cicd_issue_number}.md << 'EOF'\n"
                        f"  Closes #{_cicd_issue_number}\n"
                        f"  <one-paragraph summary of what changed>\n"
                        f"  EOF\n"
                        f"  gh pr create --draft --base main --head {_cicd_branch} \\\n"
                        f"    --title 'CICD: <slug> (#{_cicd_issue_number})' \\\n"
                        f"    --body \"$(cat /tmp/pr-body-{_cicd_issue_number}.md)\"\n"
                    )
                nudge = (
                    f"You edited files in worktree on `{_cicd_branch}` but {_missing} "
                    f"is incomplete (step 8 of MANDATORY IMPLEMENTATION WORKFLOW). "
                    f"Tests passing is NOT the cycle ending — the cycle ends when "
                    f"the PR is open with `Closes #{_cicd_issue_number}`. Run these "
                    f"commands as your next tool calls:\n{_next_cmds}"
                    f"Do not say you're done until `gh pr create` exits 0."
                )
                log.info("Auto-nudge (cycle 82): edits/push without PR — directing builder to %s", _missing)
            else:
                # Generic nudge
                nudge = (
                    "Continue — use your tools to take the next action. "
                    "Empirical > theoretical: if you suspect a bug, verify it "
                    "with a tool before trying to fix it. "
                    "Do not repeat your analysis, just act."
                )
            conversation_history.append({"role": "user", "content": nudge})
            log.info("Auto-nudge (%d/%d, total %d/%d): text-only response, prompting to continue",
                    _consecutive_text_only, _MAX_TEXT_ONLY, _total_nudges, _MAX_TOTAL_NUDGES)
            _emit("on_auto_nudge", _consecutive_text_only, _MAX_TEXT_ONLY)
            continue

        # Fix 2: Only reset consecutive counter on substantive tool calls.
        # Read-only tools (search, think, read_pdf) don't count as progress —
        # they let the model oscillate between "I'm done" and a weak grep
        # indefinitely.  file(action='read') is also read-only but uses the
        # generic "file" tool name, so we check the action arg.
        def _get_tc_args(tc):
            raw = tc.function.arguments if hasattr(tc, 'function') else tc["function"]["arguments"]
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                return parsed if isinstance(parsed, dict) else {}
            except (json.JSONDecodeError, TypeError):
                return {}

        def _get_tc_name(tc):
            return tc.function.name if hasattr(tc, 'function') else tc["function"]["name"]

        _tool_names = {_get_tc_name(tc) for tc in tool_calls}

        _is_read_only_file = (_tool_names == {"file"} and all(
            _get_tc_args(tc).get("action") == "read"
            for tc in tool_calls if _get_tc_name(tc) == "file"
        ))
        _is_read_only_exec = (_tool_names == {"exec_command"} and all(
            _is_read_only_command(_get_tc_args(tc).get("command", ""))
            for tc in tool_calls if _get_tc_name(tc) == "exec_command"
        ))
        _substantive = not (
            _tool_names <= _READ_ONLY_TOOLS
            or _is_read_only_file
            or _is_read_only_exec
        )
        if _substantive:
            _consecutive_text_only = 0

        # Detect tool-call loops: if the same set of tool calls (by name +
        # args hash) repeats N times, the model is stuck.  Inject a correction
        # nudge instead of executing the Nth repeat.
        _batch_sig = frozenset(
            (_get_tc_name(tc), hashlib.md5(
                json.dumps(_get_tc_args(tc), sort_keys=True).encode()
            ).hexdigest()[:8])
            for tc in tool_calls
        )
        _recent_tool_sigs.append(_batch_sig)
        if len(_recent_tool_sigs) > _TOOL_LOOP_THRESHOLD + 2:
            _recent_tool_sigs.pop(0)
        # Count consecutive identical signatures at the tail
        _repeat = 0
        for _sig in reversed(_recent_tool_sigs):
            if _sig == _batch_sig:
                _repeat += 1
            else:
                break
        if _repeat >= _TOOL_LOOP_THRESHOLD:
            log.warning("Tool-call loop detected: same batch repeated %d times", _repeat)
            # Remove the assistant message that requested these tool calls
            conversation_history.pop()
            conversation_history.append({
                "role": "user",
                "content": (
                    "STOP — you have repeated the exact same tool call(s) "
                    f"{_repeat} times with no effect. The approach is not working. "
                    "Try a COMPLETELY DIFFERENT method. For example: "
                    "use the file tool with action='write' to replace a line directly, "
                    "or use python -c to do the replacement, "
                    "or accept the current state and commit what you have."
                ),
            })
            _recent_tool_sigs.clear()
            continue

        # Execute tool calls
        log.debug("Executing %d tool calls", len(tool_calls))
        _emit("on_tool_batch_start", len(tool_calls))
        _garbled_count = 0  # track garbled tool calls for retry
        _tool_exec_t0 = time.monotonic()
        try:
            with cancellable():
                for tool_call in tool_calls:
                    check_cancelled()
                    try:
                        if hasattr(tool_call, 'function'):
                            func_name = tool_call.function.name
                            raw_args = tool_call.function.arguments
                            tool_id = tool_call.id
                        else:
                            func_name = tool_call["function"]["name"]
                            raw_args = tool_call["function"]["arguments"]
                            tool_id = tool_call["id"]
                        func_args = json.loads(raw_args)
                        # Sanitize garbled Gemma 4 args that parsed as valid JSON
                        # e.g. {"action": "write**,content:"} — valid JSON but bogus values
                        func_args = _sanitize_tool_args(func_name, func_args, log)
                    except json.JSONDecodeError:
                        # Gemma 4 sometimes garbles arguments (e.g. "write**,content:")
                        # Try to salvage by extracting action from the mess
                        func_args = _salvage_tool_args(func_name, raw_args, log)
                        if func_args is None:
                            log.error("Unsalvageable tool args: %s | raw: %s", func_name, raw_args)
                            _garbled_count += 1
                            conversation_history.append({
                                "role": "tool", "tool_call_id": tool_id,
                                "name": func_name,
                                "content": f"Error: malformed arguments — could not parse. "
                                           f"Use separate JSON keys: {{\"action\": \"write\", \"path\": \"...\", \"content\": \"...\"}}"
                            })
                            continue
                    except Exception as e:
                        log.error("Error parsing tool call: %s | raw: %s", e, tool_call)
                        _garbled_count += 1
                        continue

                    # Validate required fields — catch cases where sanitizer
                    # extracted the action but lost required params (empty garble)
                    if func_name == "file" and isinstance(func_args, dict):
                        action = func_args.get("action", "")
                        if action in ("read", "write", "insert", "append", "delete") and "path" not in func_args:
                            log.warning("Sanitized file call missing 'path' — returning error")
                            _garbled_count += 1
                            conversation_history.append({
                                "role": "tool", "tool_call_id": tool_id,
                                "name": func_name,
                                "content": (
                                    f"Error: your tool call was garbled — 'path' is missing. "
                                    f"Use exec_command to write files instead. Example: "
                                    f'{{\"command\": \"cat > .agent/state/current-state.json << \'EOF\'\\n{{content}}\\nEOF\"}}'
                                ),
                            })
                            continue

                    log.debug("TOOL CALL: %s(%s) [id=%s]", func_name, json.dumps(func_args), tool_id)
                    telemetry.record_tool_call(func_name)

                    # Track think tool usage for CICD phase-gate enforcement
                    if func_name == "think":
                        _cicd_think_used = True

                    # Tools that do their own streaming (think) handle
                    # their own console output — don't wrap them in a spinner.
                    # Under NO_COLOR / no-TTY, CLEAR_LINE is empty, so the
                    # spinner's non-interactive prefix would dangle and
                    # on_tool_start's header would duplicate it on the same
                    # line. Skip the spinner entirely in that mode.
                    _STREAMING_TOOLS = {"think"}
                    use_spinner = (
                        func_name not in _STREAMING_TOOLS
                        and not theme._no_color()
                    )

                    if use_spinner:
                        tool_status = StreamStatus(emit=_emit)
                        tool_status.start(f"  -> {func_name} ")

                    # Cycle 24: pre-execute PRE-MERGE CHECK short-circuit.
                    # When reviewer attempts `gh pr merge` without prior
                    # `gh issue view`, block execution (the merge is irreversible
                    # once it runs; post-hoc reminders are too late). Return a
                    # synthetic error; next turn the reviewer runs `gh issue view`,
                    _cicd_blocked, cicd_error = _validate_tool_call(func_name, func_args, _cicd_issue_view_called, log, _is_cicd_builder, _is_reviewer_role)
                    if _cicd_blocked:
                        result_str = cicd_error
                    elif func_name not in MAP_FN:
                        result_str = f"Error: Unknown tool '{func_name}'"
                    else:
                        try:
                            result_str = str(MAP_FN[func_name](**func_args))
                        except CircuitBreakerError as e:
                            # Tool temporarily unavailable - return graceful degradation
                            result_str = f"Tool {func_name} temporarily unavailable: {e}"
                        except CancelledError:
                            # Propagate to outer handler to return 'cancelled'
                            raise
                        except Exception as e:
                            result_str = f"Error executing tool: {str(e)}"
                            telemetry.record_tool_error(func_name, "execution_error")
                        # Conversational tool recovery: on error, try to fix params
                        if result_str.startswith("Error"):
                            try:
                                from tool_recovery import attempt_recovery
                                _emit("on_tool_recovery", func_name, 1)
                                recovered = attempt_recovery(
                                    func_name, func_args, result_str,
                                    map_fn=MAP_FN,
                                    llm_call_fn=lambda **kw: _llm_request(log, **kw),
                                    config=_config, log=log,
                                )
                                if recovered is not None:
                                    result_str = recovered
                                    _emit("on_notice", "info", f"[recovered: {func_name} succeeded]")
                            except Exception as e:
                                log.debug("Tool recovery unavailable: %s", e)

                    if use_spinner:
                        tool_status.first_token()
                        tool_status.finish()
                    _emit("on_tool_start", func_name, func_args)

                    # Cycle 77 — verify gh pr/issue mutations against real state.
                    # Appends a SUPERVISION note when the tool output references
                    # a PR/issue that does not exist or is not in the claimed state.
                    if func_name == "exec_command" and isinstance(func_args, dict):
                        _cmd_str = func_args.get("command", "")
                        if _cmd_str:
                            result_str = _cicd_verify_gh_mutation(_cmd_str, result_str, log)

                    # Truncate oversized tool results to cap context pressure.
                    if len(result_str) > _MAX_TOOL_RESULT_CHARS:
                        half = _MAX_TOOL_RESULT_CHARS // 2
                        result_str = (
                            result_str[:half]
                            + f"\n\n... [{len(result_str) - _MAX_TOOL_RESULT_CHARS} chars truncated] ...\n\n"
                            + result_str[-half:]
                        )

                    conversation_history.append({
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "name": func_name,
                        "content": result_str,
                    })

                    # Track file edits (file tool with action=write/create)
                    if func_name == "file" and isinstance(func_args, dict) and func_args.get("action") in ("write", "create"):
                        _has_edited, _has_reviewer_persisted = _handle_cicd_file_edit(
                            func_args, conversation_history, _cicd_worktree_path, _cicd_phase_state, 
                            _cicd_edited_files, _has_edited, _has_reviewer_persisted, turn, log
                        )
                    # Track commits and pushes through exec_command
                    if func_name == "exec_command" and isinstance(func_args, dict):
                        _cmd = func_args.get("command", "")
                        _cmd_normalized = re.sub(r"\\\n", " ", _cmd)  # Cycle 36: collapse shell line-continuations before all checks
                        if "git commit" in _cmd:
                            _has_committed = True
                            _has_edited = True  # commit implies edit happened
                            log.info("Commit detected — completion signals now allowed")
                            _cicd_phase_state["implement"] = True
                            # Cycle 61: if commit fails with nothing staged, builder never wrote test code
                            if "no changes added to commit" in result_str:
                                conversation_history.append({
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
                        if re.search(r"(?:^|&&\s*|;\s*|\|\|\s*)git\s+push\b", _cmd_normalized) and "exit=0" in result_str:  # Cycle 35+36: regex on normalized cmd
                            if not _cycle_persisted:
                                log.info("Cycle persist detected (git push exit=0) — auto-nudge disabled")
                            _cycle_persisted = True
                            _cycle_persisted_turn = _cycle_persisted_turn or turn
                            _cicd_phase_state["verify"] = True
                            # Cycle 37: block direct push to main — builder must use feature branches
                            if re.search(r"git\s+push\b.*\borigin\s+main\b", _cmd_normalized):
                                log.warning("CICD: git push origin main — builder pushed directly to main")
                                conversation_history.append({
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

                        # ── CICD phase detection ──
                        if "gh issue list" in _cmd or "gh issue search" in _cmd:
                            _cicd_phase_state["perceive"] = True
                        if ("pytest" in _cmd or "python3 -m pytest" in _cmd
                                or "cat " in _cmd or "grep " in _cmd):
                            if _cicd_phase_state["perceive"]:
                                _cicd_phase_state["probe"] = True
                        if re.search(r"(?:^|&&\s*|;\s*|\|\|?\s*|\n\s*)gh\s+issue\s+create\b", _cmd_normalized) and "exit=0" in result_str:  # Cycles 36/55: anchored + newline-bypass closed
                            if not _cicd_think_used:
                                log.warning("CICD: gh issue create without think — injecting reminder")
                                conversation_history.append({
                                    "role": "user",
                                    "content": "[SYSTEM: You filed an issue without using the think tool first. "
                                    "Per MANDATORY THINK before DECIDE, you must call think() to evaluate "
                                    "your candidate before filing. Use think now to validate this was the right choice.]",
                                })
                            # Cycle 33: require --label in-progress on gh issue create.
                            # Cycle 41: use flag-specific regex (body text can contain "in-progress").
                            if not re.search(r"--label[= ]in-progress", _cmd):
                                log.warning("CICD: gh issue create without --label in-progress — injecting reminder")
                                _issue_num_m = re.search(r'issues/(\d+)|#(\d+)|Issue\s+#(\d+)', result_str)
                                _issue_num = next((g for g in _issue_num_m.groups() if g), "?") if _issue_num_m else "?"
                                conversation_history.append({
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
                            _cicd_phase_state["decide"] = True
                            _cicd_think_used = False  # reset for next gate (verdict)
                            # Extract issue number from gh output
                            _issue_match = re.search(
                                r'issues/(\d+)|#(\d+)|Issue #(\d+)', result_str
                            )
                            if _issue_match:
                                _cicd_issue_number = next(
                                    g for g in _issue_match.groups() if g
                                )
                                log.info("CICD phase: issue #%s filed",
                                         _cicd_issue_number)
                        if "git worktree add" in _cmd and "exit=0" in result_str:
                            _cicd_phase_state["implement"] = True
                            _wt_path_match = re.search(r'git\s+worktree\s+add\s+(\S+)', _cmd)
                            if _wt_path_match:
                                _cicd_worktree_path = _wt_path_match.group(1)
                                log.info("CICD: worktree path captured: %s", _cicd_worktree_path)
                            _branch_match = re.search(r'-b\s+(\S+)', _cmd)
                            if _branch_match:
                                _cicd_branch = _branch_match.group(1)
                        # Cycle 82 capture-fix: when builder claims an EXISTING
                        # issue via `gh issue view N` or `gh issue edit N`, also
                        # capture the issue number so cycle 82's nudge can fire
                        # even on inherited/existing-issue cycles. Run 189 hit
                        # this gap: builder used `gh issue view 397`, which
                        # never set `_cicd_issue_number`, so cycle 82's elif
                        # condition `and _cicd_issue_number` was always False.
                        if (not _cicd_issue_number) and "exit=0" in result_str:
                            _existing_match = re.search(
                                r'^gh\s+issue\s+(?:view|edit|comment)\s+(\d+)\b',
                                _cmd.lstrip(),
                            )
                            if _existing_match:
                                _cicd_issue_number = _existing_match.group(1)
                                log.info("CICD phase: issue #%s claimed (existing)",
                                         _cicd_issue_number)
                        if "gh pr create" in _cmd and "exit=0" in result_str:
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
                            _pr_match = re.search(r'pull/(\d+)', result_str)
                            if _pr_match:
                                _cicd_pr_number = _pr_match.group(1)
                                log.info("CICD phase: PR #%s opened",
                                         _cicd_pr_number)
                            # Guard: PR body must contain `Closes #N` trailer
                            # so the linked issue auto-closes on merge. Missing
                            # trailer is a recurring builder failure that
                            # causes the reviewer to CLOSE the PR.
                            if not re.search(r'Closes\s+#\d+', _cmd, re.IGNORECASE) and _cicd_pr_number:
                                log.warning("CICD: gh pr create without `Closes #N` trailer — injecting reminder")
                                conversation_history.append({
                                    "role": "user",
                                    "content": (
                                        f"[SYSTEM: PR #{_cicd_pr_number} was created without a `Closes #<issue>` "
                                        f"trailer in the body. The reviewer will CLOSE this PR for the missing "
                                        f"trailer (per pre-merge check rule #4), wasting the cycle. Fix it NOW: "
                                        f"`gh pr edit {_cicd_pr_number} --body \"<existing body>\\n\\nCloses #<issue>\"` "
                                        f"— use the real issue number this cycle targets.]"
                                    ),
                                })
                        if ("gh pr review" in _cmd
                                and ("--request-changes" in _cmd
                                     or "--approve" in _cmd
                                     or "--comment" in _cmd)
                                and "exit=0" in result_str):
                            if not _has_reviewer_persisted:
                                log.info("Reviewer persistence detected (gh pr review) — completion signals now allowed")
                            _has_reviewer_persisted = True
                        if ("reviews.md" in _cmd
                                and (">>" in _cmd or "tee -a" in _cmd or "tee --append" in _cmd)
                                and "exit=0" in result_str):
                            if not _has_reviewer_persisted:
                                log.info("Reviewer persistence detected (reviews.md append) — completion signals now allowed")
                            _has_reviewer_persisted = True
                        if "gh pr review" in _cmd and "--approve" in _cmd:
                            if not _cicd_think_used:
                                log.warning("CICD: gh pr review --approve without think — injecting reminder")
                                conversation_history.append({
                                    "role": "user",
                                    "content": "[SYSTEM: You approved a PR without using the think tool first. "
                                    "Per MANDATORY THINK before VERDICT, call think() to check: "
                                    "did tests pass? was the metric measured? is the issue reference valid? "
                                    "is the diff in-scope? Proceed with merge only after thinking.]",
                                })
                            if ("exit=0" not in result_str
                                    and "approve your own pull request" in result_str.lower()):
                                log.warning("CICD: self-approve failed — injecting skip-approval reminder")
                                conversation_history.append({
                                    "role": "user",
                                    "content": "[SYSTEM: You cannot approve your own PR (same-account setup). "
                                    "SKIP the approval step entirely. Go directly to: "
                                    "`gh pr ready <N>` (separate command), then "
                                    "`gh pr merge <N> --squash` (separate command).]",
                                })
                        if "gh pr ready" in _cmd and "exit=0" in result_str:
                            _cicd_pr_ready_called = True
                            log.info("CICD: gh pr ready called")
                        if "gh issue view" in _cmd and ("exit=0" in result_str or "--json" in _cmd):
                            # Cycle 25: validate state + labels in the gh issue view result.
                            # Calling gh issue view is necessary but not sufficient — the
                            # issue must be OPEN and carry cicd + in-progress (or cicd-cycle-*)
                            # labels. If not, leave _cicd_issue_view_called=False so the
                            # pre-execute block (cycle 24) continues to block gh pr merge.
                            _premerge_ok = False
                            try:
                                import json as _json_mod
                                _json_body = result_str.split("exit=0", 1)[-1].strip()
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
                                        log.warning(
                                            "CICD: PRE-MERGE CHECK FAILED — state=%s labels=%s "
                                            "(need OPEN + cicd/in-progress[-bot-N] label)",
                                            _istate, _lnames,
                                        )
                                        conversation_history.append({
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
                                _cicd_issue_view_called = True
                                log.info("CICD: gh issue view called (PRE-MERGE CHECK satisfied)")
                        # Match `gh pr merge` as an actual top-level invocation — not inside
                        # heredoc/cat content where the string may appear as documentation.
                        # Matches at line start or after a shell separator (&&, ;, |, ||, \n).
                        if re.search(r"(?:^|&&\s*|;\s*|\|\|?\s*|\n\s*)gh\s+pr\s+merge\b", _cmd):
                            # Guard: PRE-MERGE CHECK — must view linked issue first (reviewer.md §4)
                            if not _cicd_issue_view_called:
                                log.warning("CICD: gh pr merge without PRE-MERGE CHECK — injecting reminder")
                                conversation_history.append({
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
                                log.warning("CICD: gh pr merge without --squash — injecting reminder")
                                conversation_history.append({
                                    "role": "user",
                                    "content": "[SYSTEM: You MUST use `gh pr merge --squash`. "
                                    "Never use --merge or --rebase. Never use --delete-branch. Retry with --squash only.]",
                                })
                            # Guard: must call `gh pr ready` first (draft PRs)
                            if not _cicd_pr_ready_called and "exit=0" not in result_str:
                                if "still a draft" in result_str.lower():
                                    log.warning("CICD: gh pr merge on draft — need gh pr ready first")
                                    conversation_history.append({
                                        "role": "user",
                                        "content": "[SYSTEM: The PR is still a draft. You must run "
                                        "`gh pr ready <N>` FIRST, then run `gh pr merge <N> --squash` "
                                        "as a SEPARATE command. Do NOT chain them. Do NOT add --delete-branch.]",
                                    })
                            if not _cicd_think_used and "exit=0" not in result_str:
                                log.warning("CICD: gh pr merge without think — injecting reminder")
                                conversation_history.append({
                                    "role": "user",
                                    "content": "[SYSTEM: You attempted to merge without using the think tool first. "
                                    "Per MANDATORY THINK before VERDICT, you must call think() to verify "
                                    "tests passed, metric was measured, and issue reference is valid.]",
                                })
                            # Cycle 34: detect || echo / || true which mask merge errors.
                            if re.search(r"gh\s+pr\s+merge\b.*\|\|\s*(echo|true|exit\s+0)", _cmd):
                                log.warning("CICD: gh pr merge with || suppressor — injecting reminder")
                                conversation_history.append({
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
                            if "exit=0" in result_str:
                                _cicd_phase_state["track"] = True
                        # Detect plan file writes via exec_command (cat/heredoc)
                        if ("improvements/" in _cmd
                                and ("cat >" in _cmd or "cat >>" in _cmd
                                     or "echo" in _cmd or "tee" in _cmd)
                                and "exit=0" in result_str):
                            _cicd_phase_state["plan"] = True
                            log.info("CICD phase: plan written via exec_command")
                        # Record cycle timestamp automatically
                        if _tracker:
                            try:
                                with open(_state_path("current-state.json"), encoding="utf-8", errors="replace") as _sf:
                                    _cycle = json.load(_sf).get("cycle", 0)
                            except Exception:
                                _cycle = 0
                            try:
                                timestamp = _tracker.auto_record(agent_id="e1")
                                log.debug("Cycle %s recorded: %s", _cycle, timestamp)
                            except Exception as e:
                                log.error("Failed to record cycle timestamp: %s", e)

                    log.debug("TOOL RESULT [%s]: %s", func_name, result_str)

                    # Track repeated tool results (errors and identical results)
                    _result_sig = (func_name, result_str[:100])
                    _recent_tool_errors.append(_result_sig)
                    _result_repeats = sum(1 for e in _recent_tool_errors if e == _result_sig)

                    # Past turn limit + same tool result 3 times = end cycle
                    if turn > _MAX_TURNS and _result_repeats >= 3:
                        log.warning("Overtime + repeated tool result (%s x%d) — ending cycle",
                                    func_name, _result_repeats)
                        _emit("on_overtime", "repeated_result")
                        return "done"

                    # Semantic result-loop detection: same tool returning same
                    # result despite different arguments.
                    # Skip empty or exit-only results (e.g. `echo >> file`,
                    # `git add`, `mkdir`) — they all hash identically and would
                    # false-positive after 3 consecutive silent commands.
                    # _EXIT_ONLY_RE is compiled at module scope.
                    _result_is_noise = (
                        not result_str.strip()
                        or bool(_EXIT_ONLY_RE.match(result_str.strip()))
                    )
                    if not _result_is_noise:
                        _res_hash = hashlib.md5(
                            result_str[:200].encode()
                        ).hexdigest()[:8]
                        _tool_result_key = (func_name, _res_hash)
                        _recent_tool_results.append(_tool_result_key)
                        if len(_recent_tool_results) > _RESULT_LOOP_WINDOW:
                            _recent_tool_results.pop(0)
                    _same_result_count = sum(
                        1 for k in _recent_tool_results[-6:]
                        if k == _tool_result_key
                    ) if not _result_is_noise else 0
                    if _same_result_count >= _RESULT_LOOP_THRESHOLD:
                        log.warning(
                            "Semantic result loop: %s returned same result %d times",
                            func_name, _same_result_count)
                        if func_name == "exec_command":
                            _cmd_preview = ""
                            if isinstance(func_args, dict):
                                _cmd_preview = str(func_args.get("command", ""))[:120]
                            _hint = (
                                f"SYSTEM: exec_command has returned the same output "
                                f"{_same_result_count} times (e.g. `{_cmd_preview}`). "
                                f"If you are re-verifying a check (tests, lint, gh status), "
                                f"the prior output is authoritative — do not re-run it. "
                                f"Move to the next step (commit, push, open PR) or take a "
                                f"materially different action."
                            )
                        else:
                            _hint = (
                                f"SYSTEM: The {func_name} tool has returned the "
                                f"same result {_same_result_count} times despite "
                                f"different arguments. Your approach is not working. "
                                f"Either try a completely different method, or accept "
                                f"the current state and move on to the next step."
                            )
                        conversation_history.append({
                            "role": "user",
                            "content": _hint,
                        })

                    if result_str.startswith("Error"):
                        consecutive = _result_repeats
                        if consecutive >= _REPEAT_THRESHOLD * 2:
                            # Model is stuck even after forced thinks — skip this step
                            log.warning("Hard bail: %s failed %d times — skipping",
                                        func_name, consecutive)
                            _emit("on_tool_skip", func_name, consecutive)
                            conversation_history.append({
                                "role": "user",
                                "content": (
                                    f"SYSTEM: The {func_name} tool has failed {consecutive} times "
                                    f"with the same error. This step is being SKIPPED. "
                                    f"Use exec_command with cat/heredoc to write files instead, "
                                    f"or move on to the next step."
                                ),
                            })
                            _recent_tool_errors[:] = [e for e in _recent_tool_errors if e[0] != func_name]
                            break
                        elif consecutive >= _REPEAT_THRESHOLD:
                            # Force a think call to break the loop
                            think_prompt = (
                                f"MANDATORY REFLECTION: I have called {func_name} "
                                f"{consecutive} times and gotten the same error each time.\n\n"
                                f"The error is: {result_str[:300]}\n\n"
                                f"My last arguments were: {json.dumps(func_args) if isinstance(func_args, dict) else str(func_args)}\n\n"
                                f"I MUST answer these questions:\n"
                                f"1. What exactly is the error telling me?\n"
                                f"2. What parameter am I missing or getting wrong?\n"
                                f"3. What is a DIFFERENT way to accomplish my goal "
                                f"without repeating the same call?\n"
                                f"4. Should I just skip this step and move on to "
                                f"CONSOLIDATE and PERSIST?"
                            )
                            log.warning("Loop detected: %s x%d — forcing think",
                                        func_name, consecutive)
                            _emit("on_forced_think", func_name, consecutive)
                            if "think" in MAP_FN:
                                think_result = MAP_FN["think"](prompt=think_prompt)
                                # Inject as assistant thought + tool response
                                think_id = f"forced_think_{turn}_{consecutive}"
                                conversation_history.append({
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
                                conversation_history.append({
                                    "role": "tool",
                                    "tool_call_id": think_id,
                                    "name": "think",
                                    "content": str(think_result),
                                })
                                log.info("FORCED THINK RESULT: %s", think_result)
                    else:
                        # Successful call — reset tracker for this tool
                        _recent_tool_errors[:] = [e for e in _recent_tool_errors if e[0] != func_name]
                    _emit("on_tool_result", func_name, func_args, result_str,
                          result_str.startswith("Error"))
        except CancelledError:
            _emit("on_cancelled", "tool_execution")
            log.info(
                "cancel.latency_ms latency_ms=%d site=tool_execution backend=%s",
                int((time.monotonic() - _tool_exec_t0) * 1000),
                _main_backend.kind,
            )
            log.info("CANCELLED during tool execution")
            if _async_summarizer:
                _async_summarizer.drain()
                _async_summarizer.harvest(summary_state)
            _save_checkpoint(conversation_history, summary_state, turn, initial_files)
            return "cancelled"

        # If ALL tool calls in this turn were garbled, retry the turn once
        # by removing the assistant message + error responses and re-requesting.
        if _garbled_count > 0 and _garbled_count >= len(tool_calls):
            _garble_retries = getattr(run_agent_single, '_garble_retries', 0)
            if _garble_retries < 1:
                log.warning("All %d tool call(s) garbled — retrying turn %d", _garbled_count, turn)
                # Remove the assistant message and all error tool responses from this turn
                # The assistant message is right before the tool responses
                _to_remove = 1 + _garbled_count  # assistant + tool error responses
                conversation_history[-_to_remove:] = []
                run_agent_single._garble_retries = _garble_retries + 1
                continue  # retry the same turn
            else:
                log.warning("Garble retry exhausted — proceeding with error responses")
                run_agent_single._garble_retries = 0
        else:
            run_agent_single._garble_retries = 0

        # Save checkpoint after each turn so -c can resume from here
        if _async_summarizer:
            _async_summarizer.harvest(summary_state)
        _save_checkpoint(conversation_history, summary_state, turn, initial_files)


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
                        choices=["llamacpp", "bedrock"], default=None,
                        help="Override the main backend kind (see plan/bedrock-integration.md).")
    parser.add_argument("--result-file", dest="result_file",
                        help="Write the final assistant response to this file (for subagent use).")
    parser.add_argument("--backend-summary", dest="backend_summary",
                        choices=["llamacpp", "bedrock"], default=None,
                        help="Override the summary backend kind.")
    parser.add_argument("prompt", nargs="*", help="Initial prompt")
    args = parser.parse_args()

    # Apply backend-kind overrides before any backend-dependent startup logic.
    _apply_backend_overrides(args.backend_main, args.backend_summary)

    global _NUDGE_ENABLED
    _NUDGE_ENABLED = args.nudge

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
    
    
if __name__ == "__main__":
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    else:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    import atexit
    # Always log bedrock session spend at exit — crashes (TimeoutError,
    # BedrockBudgetExceeded, etc.) would otherwise skip it. Safe no-op
    # when no bedrock backend is in use.
    atexit.register(_log_bedrock_session_spend, logging.getLogger("agent"))
    main()
