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

from types import SimpleNamespace
from typing import Callable

import theme
from callbacks import safe_cb


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
        "  /model         — pick a different model from the server",
        "  /verbose       — toggle compact/full tool output",
        "  /tools [N|all] — show buffered tool calls (default: all; N = last N only)",
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


def _cmd_model(ctx: SimpleNamespace, args: str) -> None:
    _warn_extra_args(ctx, "/model", args)
    new_model = ctx.pick_model(ctx.config["llm"]["model"], ctx.base_url)
    if new_model:
        ctx.config["llm"]["model"] = new_model
        safe_cb(ctx.cb, "on_notice", "info",
                theme.c(theme.MINT,
                        f"Model set to {new_model} (summarizer keeps its original model)"))
        ctx.log.info("Model changed via /model: %s", new_model)


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


_COMMANDS: dict[str, Callable[[SimpleNamespace, str], None]] = {
    "/help": _cmd_help,
    "/clear": _cmd_clear,
    "/context": _cmd_context,
    "/model": _cmd_model,
    "/verbose": _cmd_verbose,
    "/tools": _cmd_tools,
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
