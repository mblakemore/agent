"""Conversation checkpoints: the file `-c` resumes from, and the `.agent/` directories
it and the session logs live in (agent.py's `_save_checkpoint` / `_load_checkpoint`).

The checkpoint holds the history (older thinking pruned, reads of the file itself
stripped), the summary state, the turn, the lead files by text and by name, the loaded
tools and skills, and the restart record. `load` restores the registries that other
modules keep (tools, skills, the lead, the restart state) before handing the history back.
`ensure_agent_dirs` makes the directories and, inside a git repo, keeps `.agent/` and
`config.json` out of commits; agent.py calls it once at import.
"""
import json
import logging
import os
import subprocess
import sys

import lead as _lead
import restart_state as _restart
import skills as _skills
import tools as _registry

CKPT_TURN = {"turn": 0}         # the turn the newest checkpoint carries (for a boundary save)

AGENT_DIR = os.path.join(os.getcwd(), ".agent")
STATE_DIR = os.path.join(AGENT_DIR, "state")
HISTORY_DIR = os.path.join(AGENT_DIR, "history")


def state_path(*parts):
    """Return a path inside .agent/state under the current working directory."""
    return os.path.join(STATE_DIR, *parts)


def in_git_repo(start=None) -> bool:
    """True if *start* (default cwd) is inside a git working tree."""
    d = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return True
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent


def ensure_gitignored(entries) -> None:
    """Best-effort: ensure each of *entries* is a line in ./.gitignore — but
    only when inside a git repo, so we never litter a .gitignore into a plain
    working directory. Matches whole lines (a substring check would be fooled
    by comments or longer paths).
    """
    if not in_git_repo():
        return
    gitignore = os.path.join(os.getcwd(), ".gitignore")
    try:
        existing = (
            open(gitignore, encoding="utf-8", errors="replace").read()
            if os.path.exists(gitignore) else ""
        )
        present = set(existing.splitlines())
        missing = [e for e in entries if e not in present]
        if not missing:
            return
        with open(gitignore, "a", encoding="utf-8") as f:
            if not existing:
                f.write("# agent runtime state & local config\n")
            elif not existing.endswith("\n"):
                f.write("\n")
            f.write("\n".join(missing) + "\n")
    except OSError:
        pass  # non-fatal; best-effort only


def warn_if_config_tracked():
    """Loud warning when a config file is TRACKED by git — .gitignore does
    not untrack, so a key-bearing config that was committed before the
    ignore line landed keeps committing forever. Warning only (never
    auto-untrack: rewriting the user's index is their call), with the
    exact fix command. stderr as well as the log: this is
    actively-leaking-keys severity, not a hygiene note.
    """
    if not in_git_repo():
        return
    try:
        out = subprocess.run(
            ["git", "ls-files", "--", ".agent/config.json", "config.json"],
            capture_output=True, text=True, timeout=5, cwd=os.getcwd(),
        ).stdout.strip()
    except Exception:
        return  # git absent/hung — the ignore-append above still ran
    for path in out.splitlines():
        msg = (f"{path} is TRACKED by git — .gitignore does not untrack it, "
               f"so a key-bearing config keeps committing. "
               f"Fix: git rm --cached {path}")
        logging.getLogger("agent").warning(msg)
        print(f"⚠ {msg}", file=sys.stderr)


def ensure_agent_dirs():
    """Create .agent/state and .agent/history on first use, and — when inside a
    git repo — gitignore .agent/ and config.json so runtime state and the
    (possibly key-bearing) local config never land in commits.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(HISTORY_DIR, exist_ok=True)
    ensure_gitignored((".agent/", "config.json"))
    warn_if_config_tracked()


CHECKPOINT_PATH = state_path("conversation_checkpoint.json")


def strip_checkpoint_reads(conversation_history):
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


def prune_old_thinking(conversation_history):
    """A copy of the history in which only the newest assistant message keeps its stored
    thinking blocks, for the checkpoint file. The live list is untouched and the wire
    replays every block it still has (anthropic_wire.openai_to_anthropic), so within a
    run the prefix is stable; after a `-c` resume the older turns simply have no
    thinking, consistently from the first request on. Off with AGENT_PRUNE_OLD_THINKING=0."""
    from anthropic_wire import THINKING_KEY, prune_old_thinking_enabled
    if not prune_old_thinking_enabled():
        return conversation_history
    last = max((i for i, m in enumerate(conversation_history) if m.get("role") == "assistant"),
               default=None)
    out = []
    for i, msg in enumerate(conversation_history):
        if msg.get("role") == "assistant" and i != last and THINKING_KEY in msg:
            msg = {k: v for k, v in msg.items() if k != THINKING_KEY}
        out.append(msg)
    return out


def save(conversation_history, summary_state, turn, initial_files,
                     clean_exit=False):
    """Save conversation state so the session can be resumed with -c.

    clean_exit=True means the session ended normally (text-only stop or
    completion signal).  False means mid-turn (tool loop) or cancelled.
    -c uses this flag to pick an appropriate resume message.
    """
    try:
        checkpoint = {
            "conversation_history": prune_old_thinking(strip_checkpoint_reads(conversation_history)),
            "summary_state": summary_state,
            "turn": turn,
            "initial_files": initial_files,
            "initial_file_refs": list(_lead.LEAD["refs"]),
            "clean_exit": clean_exit,
            "loaded_tools": list(_registry.LOADED),
            "loaded_skills": list(_skills.LOADED),
            "restart": ({"by": _restart.STATE["by"], "reason": _restart.STATE["reason"], "at": _restart.STATE["at"],
                         "recent": list(_restart.STATE["recent"])} if _restart.STATE["requested"]
                        else {"recent": list(_restart.STATE["recent"])}),
        }
        CKPT_TURN["turn"] = turn
        os.makedirs(os.path.dirname(CHECKPOINT_PATH), exist_ok=True)
        with open(CHECKPOINT_PATH, "w", encoding="utf-8") as f:
            json.dump(checkpoint, f)
    except Exception:
        pass  # best-effort, don't crash the agent


def load():
    """Load a saved conversation checkpoint.
    Returns (history, summary, turn, files, clean_exit) or None."""
    if not os.path.exists(CHECKPOINT_PATH):
        return None
    try:
        with open(CHECKPOINT_PATH, encoding="utf-8", errors="replace") as f:
            cp = json.load(f)
        _registry.RESUME_LOADED[:] = [n for n in (cp.get("loaded_tools") or []) if isinstance(n, str)]
        # The skill text is in the history; the names are restored for the catalog and
        # the picker until the first window sync re-derives them.
        _skills.RESUME_LOADED[:] = [n for n in (cp.get("loaded_skills") or []) if isinstance(n, str)]
        _skills.LOADED[:] = [n for n in _skills.RESUME_LOADED if n in _skills.SKILLS]
        _skills.STATE["started"] = bool(cp.get("conversation_history"))
        _lead.LEAD["refs"] = [r for r in (cp.get("initial_file_refs") or []) if isinstance(r, str)]
        _rs = cp.get("restart") if isinstance(cp.get("restart"), dict) else {}
        _restart.STATE["recent"] = [t for t in (_rs.get("recent") or []) if isinstance(t, (int, float))]
        _restart.STATE["resumed"] = _rs if _rs.get("at") else None
        return (
            cp["conversation_history"],
            cp["summary_state"],
            cp.get("turn", 0),
            cp.get("initial_files"),
            cp.get("clean_exit", False),
        )
    except Exception:
        return None


def delete():
    """Remove checkpoint after a clean exit."""
    try:
        if os.path.exists(CHECKPOINT_PATH):
            os.remove(CHECKPOINT_PATH)
    except Exception:
        pass

