"""The system prompt and the per-turn runtime note (agent.py's request assembly).

Prompt-cache discipline, measured on a live run: the leading system message used to begin
with a second-resolution clock, so the first tokens of every request differed and a
caching server matched a near-empty prefix on every turn. The head of the request must
be byte-stable turn to turn, so `split` keeps only lines that do not change within a run
in the system message (the user's own prompt, the working directory, the tool and skill
catalogs, the tool-selection guidance) and everything per-turn (the clock, the phase
checkpoint, the nudges) rides as a trailing user-role note. Strict chat templates reject a
non-leading system message, so the note is a user turn. On a caching backend the note is
stored in history once per turn (`persist_runtime_note`) so the next request extends this
one exactly, and the working directory leaves the head too, so runs that start within the
cache TTL share the cached system prompt.
"""
from lead import RUNTIME_NOTE_HEAD
import skills as _skills
import tools as _registry

NOTE_HEAD = "[Runtime note — from the framework, not the user]"   # the unstored trailer's head
TOOL_HINTS = (
    "Tool-selection guidance: when modifying an EXISTING file, "
    "prefer file(action='edit', path=..., old_string=..., "
    "new_string=...) over exec_command 'cat > f <<EOF'. "
    "Surgical edits are atomic, validate that old_string "
    "exists exactly once, and won't lose neighbouring content. "
    "Use heredoc writes only when creating a NEW file or "
    "rewriting one in full."
)
MODEL_GUARD_CWD = " — use this for all file paths, not /home/user."


def restart_line(rs):
    """The sentence a resumed session sees once after a restart in place."""
    return ("The agent process restarted (by %s%s) and this session resumed from its "
            "checkpoint: same history, tools and skills; monitors armed with persist "
            "came back, others did not." % (
                "the restart tool" if rs.get("by") == "tool" else "/restart at the keyboard",
                f": {rs.get('reason')}" if rs.get("reason") else ""))


def inertia_line(read_only_turns):
    """The action-inertia nudge: reading without writing for several turns."""
    return (f"[SYSTEM NOTICE: You have spent {read_only_turns} consecutive turns "
            f"reading files without writing, editing, or executing anything. "
            f"If you have identified what needs to change, apply the fix now "
            f"rather than reading more files.]")


def per_turn_lines(now_iso, cwd, *, model_guards=True, catalog_line=None, skill_catalog=None,
                   skill_suggestion=None, restart=None, phase_line=None, cicd_line=None,
                   tool_hints=False, read_only_turns=0, read_only_threshold=5, repeat_msg=None):
    """Every line a request carries besides the history, in order. The clock first (the
    framework owns the clock, the model owns the work), then the working directory, the
    catalogs, the skill suggestion, the restart notice, the phase and CICD checkpoints,
    the tool-selection guidance, and the two stall nudges."""
    lines = [
        f"Current time (UTC, ISO8601): {now_iso}",
        f"Working directory: {cwd}" + (MODEL_GUARD_CWD if model_guards else ""),
    ]
    if catalog_line:
        lines.append(catalog_line)            # static: the same bytes every request
    if skill_catalog:
        lines.append(_skills.INSTRUCTIONS)    # static too
        lines.append(skill_catalog)
    if skill_suggestion:
        lines.append(skill_suggestion)
    if restart:
        lines.append(restart_line(restart))
    if phase_line:
        lines.append(phase_line)
    if cicd_line:
        lines.append(cicd_line)
    if tool_hints:
        lines.append(TOOL_HINTS)
    if read_only_turns >= read_only_threshold:
        lines.append(inertia_line(read_only_turns))
    if repeat_msg:
        lines.append(repeat_msg)
    return lines


def is_static(line):
    """A line that does not change within a run belongs in the cached head."""
    return (line.startswith("Working directory:") or line.startswith("Tool-selection guidance:")
            or line.startswith(_registry.CATALOG_PREFIX)
            or line.startswith(_skills.CATALOG_PREFIX) or line is _skills.INSTRUCTIONS)


def split(system_lines, *, caching, user_prompt=None):
    """(static_lines, dynamic_lines). The user's own system prompt leads the static part:
    fixed within a run unless /system changes it. On a caching backend the working
    directory is dynamic too (nothing run-specific before the first user turn)."""
    static = [ln for ln in system_lines if is_static(ln)]
    if caching:
        static = [ln for ln in static if not ln.startswith("Working directory:")]
    dynamic = [ln for ln in system_lines if ln not in static]
    if isinstance(user_prompt, str) and user_prompt.strip():
        static.insert(0, user_prompt.strip())
    return static, dynamic


def outgoing(static_lines, messages_to_send, dynamic_lines):
    """The request body: the system message, the history, the unstored trailing note."""
    return ([{"role": "system", "content": "\n\n".join(static_lines)}] if static_lines else []) \
        + messages_to_send \
        + ([{"role": "user", "content": NOTE_HEAD + "\n" + "\n\n".join(dynamic_lines)}] if dynamic_lines else [])


def persist_runtime_note(conversation_history, messages_to_send, lines, turn):
    """P4.1: append this turn's runtime note to the newest history message (a user turn
    or a tool result — never sent before, so editing it costs no cache), or add it as
    a user turn after an assistant one. Idempotent within a turn: a retried request
    (overflow reduction, stall) re-sends the same note rather than stacking another."""
    if not lines or not conversation_history:
        return
    note = RUNTIME_NOTE_HEAD + "\n" + "\n\n".join(lines)
    last = conversation_history[-1]
    if last.get("_note_turn") == turn:
        return
    # A message noted on an EARLIER turn has already been sent; editing it would
    # break the prefix, so the new note gets its own message.
    if (last.get("role") in ("user", "tool") and isinstance(last.get("content"), str)
            and last.get("_note_turn") is None):
        last["content"] = (last["content"] + "\n\n" + note) if last["content"] else note
        last.pop("_tokens", None)          # M7: token_utils caches counts per message
        last["_note_turn"] = turn
        return
    msg = {"role": "user", "content": note, "_note_turn": turn}
    conversation_history.append(msg)
    if messages_to_send is not None:
        messages_to_send.append(msg)


def append_turn_text(conversation_history, messages_to_send, text):
    """Add text for the model at a turn boundary the way the runtime note does: onto
    the newest message when it is a user turn or a tool result not yet sent, else as a
    user turn of its own (never two user turns in a row)."""
    last = conversation_history[-1] if conversation_history else None
    if (last is not None and last.get("role") in ("user", "tool")
            and isinstance(last.get("content"), str) and last.get("_note_turn") is None):
        last["content"] = (last["content"] + "\n\n" + text) if last["content"] else text
        last.pop("_tokens", None)
        return last
    msg = {"role": "user", "content": text}
    conversation_history.append(msg)
    if messages_to_send is not None:
        messages_to_send.append(msg)
    return msg
