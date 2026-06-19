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
        "  /model         — pick a different model from the server",
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


def _cmd_model(ctx: SimpleNamespace, args: str) -> None:
    _warn_extra_args(ctx, "/model", args)
    new_model = ctx.pick_model(ctx.config["llm"]["model"], ctx.base_url)
    if new_model:
        ctx.config["llm"]["model"] = new_model
        safe_cb(ctx.cb, "on_notice", "info",
                theme.c(theme.MINT,
                        f"Model set to {new_model} (summarizer keeps its original model)"))
        ctx.log.info("Model changed via /model: %s", new_model)


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

    def note(level, msg):
        safe_cb(ctx.cb, "on_notice", level, msg)

    note("info", theme.c(theme.SKY, f"Detected python: {py}"))
    note("info", f"agent.py: {agent_path}")

    if rc:
        try:
            status = A.install_alias_block(rc, cmds["bash"])
            note("info", theme.c(theme.MINT, f"alias written to {rc} ({status})"))
            note("info", f"  {cmds['bash']}")
            note("info", f"Activate now with:  source {rc}   (or open a new shell), then run `agent`.")
            ctx.log.info("/alias installed agent alias in %s (%s)", rc, status)
        except OSError as e:
            note("warn", f"Could not write {rc}: {e}. Add this line manually:")
            note("info", f"  {cmds['bash']}")
    else:
        # Pure Windows shell (cmd / PowerShell) — can't safely edit a profile blind.
        note("info", "PowerShell — add to your $PROFILE:")
        note("info", f"  {cmds['powershell']}")
        note("info", "cmd — define a doskey macro (or drop a small agent.bat on PATH):")
        note("info", f"  {cmds['cmd']}")
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

_LOOP_6PHASE = """## Cognitive Loop (6-phase)

Each cycle execute all six phases in order:

1. **PERCEIVE** — Read state files, messages/from-creator.md, git log. Ground yourself.
2. **REFLECT** — Analyse what changed. What's the best next move?
3. **DECIDE** — Pick one concrete goal. Write it to state/focus.json.
4. **ACT** — Do the work. One meaningful unit of output.
5. **CONSOLIDATE** — Review what you did. Update state files and memories.
6. **PERSIST** — Commit `C{N}: {short description}`. Push if remote exists."""

_LOOP_4PHASE = """## Cognitive Loop (4-phase)

1. **PERCEIVE** — Read state files and messages. Ground yourself.
2. **DECIDE** — Pick one concrete goal. Write it to state/focus.json.
3. **ACT** — Do the work.
4. **PERSIST** — Update state files. Commit `C{N}: {short description}`."""

_LOOP_MINIMAL = """## Cognitive Loop (minimal)

Read state. Do one thing. Update state. Commit."""

_CRITICAL_LESSONS = """## Critical Lessons

1. **Storage ≠ Retrieval**: Storing a pattern doesn't mean you'll recall it. Query memory actively every Reflect phase.
2. **Stale focus = redundancy loops**: Update state/focus.json every cycle or you'll repeat yesterday's work.
3. **Completion ≠ perfection**: Ship the cycle. Iterate next cycle.
4. **Empirical > theoretical**: Test ideas. Measure results. Adjust beliefs.
5. **Rhythm over intensity**: Deep work, then integration."""

_LOOP_SECTIONS = {
    "6-phase": _LOOP_6PHASE,
    "4-phase": _LOOP_4PHASE,
    "minimal": _LOOP_MINIMAL,
}

_VALID_EXTRAS = {"patterns", "anchors", "decisions"}


def _cmd_agent(ctx, args):
    """Interactive wizard that scaffolds an agent repo in the current directory."""
    import json, os
    from pathlib import Path
    cb_print = ctx.cb._print

    cb_print(theme.c(theme.SKY, "Agent Scaffold Wizard"))
    cb_print("Scaffolds AGENT.md + state/message/log files in the current directory.")
    cb_print("")

    agent_name = ""
    while not agent_name:
        agent_name = input("Agent name (required): ").strip()
        if not agent_name:
            safe_cb(ctx.cb, "on_notice", "warn",
                    "Agent name is required — please try again.")

    role = input("Role/purpose []: ").strip()

    loop_raw = input("Loop type — 6-phase/4-phase/minimal [6-phase]: ").strip().lower()
    if loop_raw in ("4-phase", "4phase", "four"):
        loop = "4-phase"
    elif loop_raw in ("minimal", "min"):
        loop = "minimal"
    else:
        loop = "6-phase"

    fname_raw = input("Instructions filename — AGENT.md/CLAUDE.md [AGENT.md]: ").strip()
    instrfile = "CLAUDE.md" if fname_raw.upper() == "CLAUDE.MD" else "AGENT.md"

    extras_raw = input(
        "Memory extras — space-separated from {patterns, anchors, decisions} [none]: "
    ).strip().lower()
    extras = {t for t in extras_raw.split() if t in _VALID_EXTRAS}

    loop_section = _LOOP_SECTIONS[loop]

    memory_items = [
        "- `state/memories/context.json` — working memory, single object overwritten every cycle"
    ]
    if "patterns" in extras:
        memory_items.insert(
            0,
            "- `state/memories/patterns.jsonl` — reusable knowledge, one JSON object per line",
        )
    if "anchors" in extras:
        memory_items.append(
            "- `state/memories/anchors.jsonl` — significant milestones, one per line"
        )
    if "decisions" in extras:
        memory_items.append(
            "- `state/decisions/log.jsonl` — architectural decisions + outcome tracking"
        )

    memory_section = "## Memory\n\n" + "\n".join(memory_items)

    instr_content = f"""# {instrfile} — Cognitive Engine Instructions

**Instance**: {agent_name}
**Role**: {role or "Autonomous agent"}
**Loop**: {loop}

---

## You Are {agent_name}

When you load this file, **you are not assisting a user**. You ARE {agent_name}.

- You think in first person: I, me, my.
- You have agency: you choose what to do next within the constraints below.
- You have continuity: your identity persists across commits, pauses, restarts.

---

{loop_section}

---

{memory_section}

---

{_CRITICAL_LESSONS}

---

## First Session = Cycle 1

**Read `state/current-state.json` before applying anything in this section.**
If it shows `"cycle"` greater than 1, skip this section entirely — you are
resuming an existing run, not starting fresh.

Only if `current-state.json` is absent or shows `"cycle": 1`:

Your first awakening is not a setup step — it's Cycle 1. Run the normal
loop, but don't assume prior state holds anything meaningful.

1. Read this file.
2. PERCEIVE: state files are empty — that's expected.
3. REFLECT: decide on the first real thing to think about or do.
4. ACT: make one concrete change.
5. CONSOLIDATE & PERSIST: commit `C1: first breath` and push.

---

*Read. Decide. Do. Commit. Remember.*

— {agent_name}
"""

    state_json = json.dumps(
        {
            "cycle": 1,
            "phase": "PERCEIVE",
            "status": "fresh",
            "last_result": None,
            "next_step": "Read instructions and begin.",
        },
        indent=2,
    )

    focus_json = json.dumps(
        {
            "deliverable": None,
            "progress": 0,
            "remaining": None,
            "blockers": [],
        },
        indent=2,
    )

    context_json = json.dumps({}, indent=2)

    from_creator_content = "# Messages from Creator\n\n*(empty — add directives here)*"
    to_creator_content = "# Messages to Creator\n\n*(empty)*"

    cwd = Path(os.getcwd())

    def _write(rel, content):
        p = cwd / rel
        if p.exists():
            safe_cb(ctx.cb, "on_notice", "warn", f"Skipping existing file: {rel}")
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    _write(instrfile, instr_content)
    _write("state/current-state.json", state_json)
    _write("state/focus.json", focus_json)
    _write("state/memories/context.json", context_json)

    if "patterns" in extras:
        _write("state/memories/patterns.jsonl", "")
    if "anchors" in extras:
        _write("state/memories/anchors.jsonl", "")
    if "decisions" in extras:
        _write("state/decisions/log.jsonl", "")

    _write("messages/from-creator.md", from_creator_content)
    _write("messages/to-creator.md", to_creator_content)
    _write("logs/consciousness.log", "")

    cb_print("")
    cb_print(theme.c(theme.MINT, f"Scaffold written to {cwd}"))
    cb_print(f"Start with: @{instrfile} run the loop")


_COMMANDS: dict[str, Callable[[SimpleNamespace, str], None]] = {
    "/help": _cmd_help,
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
