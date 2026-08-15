"""
Slash-command dispatcher for the interactive agent.

Each command takes a context `ctx` (a SimpleNamespace of loop state and
callables) plus an `args` string (everything after the verb, stripped)
and returns True if the command was handled — the caller uses the
return value to decide whether to skip the normal user-input path.

Adding a new command is a matter of writing a handler and registering it
in `_COMMANDS`. Keep handlers short and free of business logic; anything
non-trivial belongs back in agent.py or a helper module.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import theme
from callbacks import safe_cb
from tools.task_tracker import get_tasks



def _warn_extra_args(ctx: SimpleNamespace, verb: str, args: str) -> None:
    if args:
        safe_cb(ctx.cb, "on_notice", "warn",
                f"{verb} takes no arguments — ignoring: {args!r}")


def _cmd_help(ctx: SimpleNamespace, args: str) -> None:
    _warn_extra_args(ctx, "/help", args)
    lines = [
        theme.c(theme.SKY, "Commands:"),
        "  /help          — show this message",
        "  /clear         — clear conversation history and start a fresh session log",
        "  /context       — show current context usage (bar + token counts)",
        "  /model [main|summary] [name] — set+persist the main/summary model",
        "  /setup [main|summary|advisor|test|calibrate] — configure endpoints, probe, calibrate",
        "  /agent         — scaffold an agent identity (AGENT.md + state tree) here",
        "  /alias         — install an `agent` shell alias for this checkout",
        "  /verbose       — toggle compact/full tool output",
        "  /tools [N|all] — show buffered tool calls (default: all; N = last N only)",
        "  /phase         — show current phase checkpoint",
        "  exit/quit      — end the session",
    ]
    ctx.cb._print("\n".join(lines))


def _cmd_clear(ctx: SimpleNamespace, args: str) -> None:
    _warn_extra_args(ctx, "/clear", args)
    ctx.conversation_history.clear()
    ctx.summary_state["text"] = ""
    ctx.summary_state["up_to"] = 0
    ctx.initial_files = None
    if ctx.async_summarizer:
        ctx.async_summarizer.reset()
    new_log, new_log_path, _ = ctx.setup_logger()
    ctx.log = new_log
    ctx.log_path = new_log_path
    ctx.refresh_cb_log(new_log)
    safe_cb(ctx.cb, "on_notice", "info",
            f"Conversation cleared. New session: {new_log_path}")


def _cmd_context(ctx: SimpleNamespace, args: str) -> None:
    _warn_extra_args(ctx, "/context", args)
    ctx.cb._print(ctx.render_context_bar(ctx.conversation_history, ctx.summary_state, ctx.ctx_size))


def _parse_model_args(args: str):
    """Parse ``/model`` args into ``(role, direct_name)``.

    ``""`` → main (bare /model keeps current muscle memory); ``"main"`` /
    ``"summary"`` → that role interactively; ``"main <name>"`` /
    ``"summary <name>"`` → set that role directly. Returns ``(None, None)``
    for an unrecognized role token (caller warns).
    """
    parts = args.split(None, 1)
    if not parts:
        return "main", None
    role = parts[0].lower()
    if role not in ("main", "summary"):
        return None, None
    direct = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    return role, direct


def _cmd_model(ctx: SimpleNamespace, args: str) -> None:
    role, direct = _parse_model_args(args)
    if role is None:
        safe_cb(ctx.cb, "on_notice", "warn",
                f"usage: /model [main|summary] [<name>] — got: {args!r}")
        return

    section = "llm" if role == "main" else "summary"
    current = ctx.config.get(section, {}).get("model", "")
    if role == "summary":
        base_url = getattr(ctx, "summary_base_url", None) or ctx.base_url
    else:
        base_url = ctx.base_url

    if direct:
        new_model = direct
    else:
        new_model = ctx.pick_model(current, base_url)
    if not new_model:
        return

    ctx.set_model(role, new_model)
    safe_cb(ctx.cb, "on_notice", "info",
            theme.c(theme.MINT,
                    f"{role} model set to {new_model} "
                    f"(persisted to ./.agent/config.json)"))
    ctx.log.info("Model changed via /model %s: %s", role, new_model)


def _cmd_alias(ctx: SimpleNamespace, args: str) -> None:
    """Detect the working python and install an `agent` shell alias mapping
    `<python> /path/to/agent.py` → `agent`."""
    _warn_extra_args(ctx, "/alias", args)
    import alias_setup as A

    py = A.detect_python_cmd()
    agent_path = A.agent_script_path()
    cmds = A.build_alias_commands(py, agent_path)
    kind = A.current_shell_kind()
    rc = A.rc_file_for(kind)

    # Literal-level helpers (not a single note(level, ...) closure): the
    # callbacks dispatch-arm AST guard requires on_notice to be called with a
    # literal level so it can verify every level arm is reachable.
    def info(msg):
        safe_cb(ctx.cb, "on_notice", "info", msg)

    def warn(msg):
        safe_cb(ctx.cb, "on_notice", "warn", msg)

    info(theme.c(theme.SKY, f"Detected python: {py}"))
    info(f"agent.py: {agent_path}")

    if rc:
        try:
            status = A.install_alias_block(rc, cmds["bash"])
            info(theme.c(theme.MINT, f"alias written to {rc} ({status})"))
            info(f"  {cmds['bash']}")
            info(f"Activate now with:  source {rc}   (or open a new shell), then run `agent`.")
            ctx.log.info("/alias installed agent alias in %s (%s)", rc, status)
        except OSError as e:
            warn(f"Could not write {rc}: {e}. Add this line manually:")
            info(f"  {cmds['bash']}")
    else:
        # Pure Windows shell (cmd / PowerShell) — can't safely edit a profile blind.
        info("PowerShell — add to your $PROFILE:")
        info(f"  {cmds['powershell']}")
        info("cmd — define a doskey macro (or drop a small agent.bat on PATH):")
        info(f"  {cmds['cmd']}")
        ctx.log.info("/alias printed Windows (cmd/PowerShell) alias instructions")


def _cmd_verbose(ctx: SimpleNamespace, args: str) -> None:
    _warn_extra_args(ctx, "/verbose", args)
    if hasattr(ctx.cb, "verbose"):
        ctx.cb.verbose = not getattr(ctx.cb, "verbose", False)
        state = "on" if ctx.cb.verbose else "off"
        safe_cb(ctx.cb, "on_notice", "info", f"verbose mode {state}")


def _cmd_tools(ctx: SimpleNamespace, args: str) -> None:
    if not hasattr(ctx.cb, "render_tools"):
        return
    limit = None
    if args:
        low = args.lower()
        if low == "all":
            limit = None
        else:
            try:
                n = int(args)
            except ValueError:
                safe_cb(ctx.cb, "on_notice", "warn",
                        f"usage: /tools [N|all] — got: {args!r}")
                return
            if n <= 0:
                safe_cb(ctx.cb, "on_notice", "warn",
                        f"/tools N requires a positive integer — got: {args!r}")
                return
            limit = n
    ctx.cb._print(ctx.cb.render_tools(limit=limit))


def _cmd_phase(ctx: SimpleNamespace, args: str) -> None:
    _warn_extra_args(ctx, "/phase", args)
    phases = ["PERCEIVE", "PROBE", "DECIDE", "PLAN", "IMPLEMENT", "VERIFY", "TRACK"]
    try:
        tasks = get_tasks()
    except Exception as e:
        ctx.cb._print(f"Error loading tasks: {e}")
        return
    
    results = []
    for phase in phases:
        is_done = any(
            phase.lower() in t.get("description", "").lower() 
            and t.get("status") == "done" 
            for t in tasks
        )
        results.append(f"{phase} {'✓' if is_done else '✗'}")
    
    ctx.cb._print("PHASE CHECKPOINT: " + " | ".join(results))



# ── /agent wizard ──────────────────────────────────────────────────────────


def _cmd_agent(ctx, args):
    """Scaffold an agent repo (AGENT.md + state tree) in the current
    directory — agent_scaffold owns the wizard; runs inside the terminal
    borrow like /model and /setup, so plain input() is safe."""
    _warn_extra_args(ctx, "/agent", args)
    import agent_scaffold as A

    cb_print = ctx.cb._print
    cb_print(theme.c(theme.SKY, "Agent Scaffold Wizard"))
    try:
        A.run_agent_wizard(print_fn=cb_print, input_fn=_borrow_input)
    except (KeyboardInterrupt, EOFError):
        safe_cb(ctx.cb, "on_notice", "info", "\n/agent aborted — nothing written")


def _borrow_input(prompt: str = "") -> str:
    """input() for wizards running inside the terminal borrow.

    Under patch_stdout, a promptless-newline prompt sits unflushed in the
    proxy while the prompt app is suspended — input()'s own prompt never
    appears and the wizard looks hung at its first question. Writing to
    sys.__stdout__ ALSO vanishes in the borrowed-terminal state (measured,
    QA 2026-08-15: input() was demonstrably waiting while neither channel
    showed a byte). The one channel proven to render is the proxy with a
    trailing newline — the same path every wizard print uses — so the
    question becomes its own line and the answer is typed on the next."""
    if prompt:
        print(prompt)
    return input("")


def _cmd_setup(ctx: SimpleNamespace, args: str) -> None:
    """Configure roles/endpoints and calibrate from live probes.

    Forms: `/setup` (full wizard) · `/setup main|summary` (one role) ·
    `/setup test` (probe only, change nothing) · `/setup calibrate`
    (re-derive context knobs from a fresh probe of the main endpoint).
    Runs inside the terminal borrow like /model, so plain input() is safe.
    """
    import setup_wizard as W

    arg = args.strip().lower()
    if arg not in ("", "test", "calibrate", "main", "summary", "advisor"):
        safe_cb(ctx.cb, "on_notice", "warn",
                f"usage: /setup [main|summary|advisor|test|calibrate] — got: {args!r}")
        return

    if arg == "test":
        W.run_probe_report(ctx.config)
        return

    cfg_path = Path(os.getcwd()) / ".agent" / "config.json"
    try:
        updates = W.run_wizard(cfg_path, ctx.config,
                               jump_to=(arg or None) if arg != "" else None,
                               input_fn=_borrow_input)
    except (KeyboardInterrupt, EOFError):
        safe_cb(ctx.cb, "on_notice", "info", "\n/setup aborted — nothing written")
        return
    if not updates:
        return
    apply_fn = getattr(ctx, "apply_setup", None)
    if apply_fn is not None:
        applied = apply_fn(updates)
        safe_cb(ctx.cb, "on_notice", "info",
                theme.c(theme.MINT,
                        f"setup applied live ({applied}) and persisted to {cfg_path}"))
    else:
        safe_cb(ctx.cb, "on_notice", "info",
                f"setup persisted to {cfg_path} — restart to apply")
    ctx.log.info("/setup wrote %s (sections: %s)", cfg_path, sorted(updates))


_COMMANDS: dict[str, Callable[[SimpleNamespace, str], None]] = {
    "/help": _cmd_help,
    "/setup": _cmd_setup,
    "/clear": _cmd_clear,
    "/context": _cmd_context,
    "/model": _cmd_model,
    "/alias": _cmd_alias,
    "/verbose": _cmd_verbose,
    "/tools": _cmd_tools,
    "/phase": _cmd_phase,
    "/agent": _cmd_agent,
}


def handle_command(line: str, ctx: SimpleNamespace) -> bool:
    """Dispatch a slash command. Returns True iff the input was a command.

    The stripped input is split on the first whitespace run into a verb
    (e.g. `/tools`) and an argument string (e.g. `40`). Handlers that
    accept no arguments warn when `args` is non-empty but still run, so
    typos like `/clear now` aren't silently swallowed.

    Unknown `/…` lines still count as commands (we print a hint and
    consume the input) so the user can't accidentally send a typo to the
    model as a prompt.
    """
    stripped = line.strip()
    if not stripped.startswith("/"):
        return False
    parts = stripped.split(None, 1)
    verb = parts[0]
    args = parts[1].strip() if len(parts) > 1 else ""
    handler = _COMMANDS.get(verb)
    if handler is None:
        safe_cb(ctx.cb, "on_notice", "warn",
                f"Unknown command: {verb} — type /help for the list")
        return True
    handler(ctx, args)
    return True
