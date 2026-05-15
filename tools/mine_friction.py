#!/usr/bin/env python3
"""
mine_friction.py — Phase 1 friction dataset miner.

Parses agent.py session logs (lyla, c0rtana, CICD builder/reviewer) and
generates training examples in Unsloth ShareGPT format for the Gemma 4 31B
tool-grammar LoRA.

Friction categories mined:
  T5.18 — action='write' on existing file with high line similarity (should be edit)
  T4.11 — action='write' drops top-level JSON keys (schema-warning)
  J.01  — action='write' on a .jsonl file (should be append)
  D.01  — same file read 2+ times in a 10-turn window (redundant read)
  H.01  — exec_command heredoc write instead of file(action='write') tool

Usage:
  python3 tools/mine_friction.py \
      --logs /droid/repos/lyla/.agent/history \
               /droid/repos/c0rtana/.agent/history \
               /droid/repos/agent/logs \
      --out /droid/repos/beewatcher/agent-friction-v1/phase1_examples.jsonl \
      --limit 200
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class Turn:
    role: str           # "system" | "user" | "assistant" | "tool_result"
    func: Optional[str] = None
    args: Optional[dict] = None
    result: Optional[str] = None
    text: Optional[str] = None  # assistant prose
    raw_line: int = 0


@dataclass
class FrictionEvent:
    category: str       # T5.18 | T4.11 | J.01 | D.01 | H.01
    bad_turn: Turn      # the turn exhibiting friction
    context: list       # preceding turns (max 8)
    ideal_call: str     # corrected tool call text to use as training target
    file_path: str = ""
    dropped_keys: list = field(default_factory=list)
    source: str = ""    # which log file


# ---------------------------------------------------------------------------
# Log parsers
# ---------------------------------------------------------------------------

# Baseline / lyla / c0rtana format:
#   HH:MM:SS DEBUG TOOL CALL: file({"action": "write", ...}) [id=...]
#   HH:MM:SS DEBUG TOOL RESULT [file]: ...

TC_RE = re.compile(
    r"DEBUG TOOL CALL:\s+(\w+)\((\{.*\})\)\s+\[id=\S+\]"
)
TR_RE = re.compile(
    r"DEBUG TOOL RESULT \[(\w+)\]:\s*(.*)"
)
ASST_RE = re.compile(r"DEBUG ASSISTANT:\s+(.*)")

# CICD builder format:
#   [whitespace]-> func(args...)
#   [whitespace]    Result: ...

CICD_TC_RE = re.compile(r"^\s+->\s+(\w+)\((.*)\)$")
CICD_TR_RE = re.compile(r"^\s+Result:\s+(.*)")


def _try_parse_json(s: str) -> Optional[dict]:
    try:
        return json.loads(s)
    except Exception:
        return None


def parse_baseline_log(path: Path) -> list[Turn]:
    """Parse lyla/.agent/history or agent/baseline logs."""
    turns = []
    lines = path.read_text(errors="replace").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = TC_RE.search(line)
        if m:
            func, args_str = m.group(1), m.group(2)
            args = _try_parse_json(args_str) or {}
            # grab multi-line result that follows
            result_lines = []
            j = i + 1
            while j < len(lines) and not TR_RE.search(lines[j]) and not TC_RE.search(lines[j]):
                j += 1
            if j < len(lines):
                rm = TR_RE.search(lines[j])
                if rm:
                    result_lines.append(rm.group(2))
                    k = j + 1
                    while k < len(lines) and not TC_RE.search(lines[k]) and not TR_RE.search(lines[k]) and not re.search(r"DEBUG (TOOL|ASSISTANT|Context|Turn)", lines[k]):
                        result_lines.append(lines[k])
                        k += 1
            result = "\n".join(result_lines)
            turns.append(Turn("tool_call", func=func, args=args, result=result, raw_line=i))
            i = j + 1
            continue
        am = ASST_RE.search(line)
        if am:
            turns.append(Turn("assistant", text=am.group(1), raw_line=i))
        i += 1
    return turns


def parse_cicd_log(path: Path) -> list[Turn]:
    """Parse CICD builder/reviewer logs."""
    turns = []
    lines = path.read_text(errors="replace").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = CICD_TC_RE.match(line)
        if m:
            func = m.group(1)
            args_str = m.group(2).strip()
            args = _try_parse_json(args_str) or {}
            result_lines = []
            j = i + 1
            while j < len(lines):
                rm = CICD_TR_RE.match(lines[j])
                if rm:
                    result_lines.append(rm.group(1))
                    k = j + 1
                    while k < len(lines) and not CICD_TC_RE.match(lines[k]) and not re.match(r"^\s+->", lines[k]) and not re.match(r"INFO:|A:\s*$", lines[k]):
                        result_lines.append(lines[k].strip())
                        k += 1
                    j = k
                    break
                j += 1
            result = "\n".join(result_lines)
            turns.append(Turn("tool_call", func=func, args=args, result=result, raw_line=i))
            i = j
            continue
        # assistant prose blocks
        if re.match(r"^A:\s*$", line):
            i += 1
            prose = []
            while i < len(lines) and not re.match(r"^\s+->|^INFO:|^A:\s*$", lines[i]):
                prose.append(lines[i])
                i += 1
            if prose:
                turns.append(Turn("assistant", text="\n".join(prose), raw_line=i))
            continue
        i += 1
    return turns


def load_log(path: Path) -> list[Turn]:
    try:
        if path.name.startswith("cicd-") or path.name.startswith("cicd_"):
            return parse_cicd_log(path)
        else:
            return parse_baseline_log(path)
    except Exception as e:
        print(f"  WARN: failed to parse {path.name}: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Friction detectors
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are an agent.py autonomous agent. "
    "You have these tools: file(action, path, ...), exec_command(command, cwd), "
    "think(prompt, depth), find_symbol(name, path), search_files(pattern, path), "
    "task_tracker(action, ...), web_fetch(url). "
    "For surgical changes to existing files, use file(action='edit', path=..., "
    "old_string=..., new_string=...) rather than rewriting the whole file. "
    "For append-only JSONL files use file(action='append'). "
    "Preserve all top-level keys when updating JSON state files."
)


def _tc_json(func: str, args: dict) -> str:
    """Render a tool call as the model's output format."""
    return f"```tool_call\n{json.dumps({'name': func, 'arguments': args}, indent=2)}\n```"


def _context_turns(turns: list[Turn], idx: int, window: int = 8) -> list[Turn]:
    start = max(0, idx - window)
    return turns[start:idx]


def detect_t518(turns: list[Turn], source: str) -> list[FrictionEvent]:
    """T5.18: action='write' on existing file (should be edit).

    Two detection paths:
    1. Tool result contains [suggestion]/similar_rewrite nudge text.
    2. Write on a file that was explicitly read earlier in context (file existed),
       content is substantial (>3 lines), and the file is not .jsonl (J.01 handles those).
    """
    events = []
    # Build cumulative read-set for path 2
    files_seen: set[str] = set()
    for i, t in enumerate(turns):
        if t.func == "file" and t.args.get("action") == "read":
            files_seen.add(t.args.get("path", ""))
            continue
        if t.func != "file" or t.args.get("action") != "write":
            continue
        fp = t.args.get("path", "unknown")
        content = t.args.get("content", "")
        result = t.result or ""

        nudge_fired = "[suggestion]" in result or "similar_rewrite" in result
        prior_read = fp in files_seen
        substantial = len(content.splitlines()) > 3

        if not (nudge_fired or (prior_read and substantial)):
            continue
        if fp.endswith(".jsonl"):
            continue  # J.01 handles these

        ideal = _tc_json("file", {
            "action": "edit",
            "path": fp,
            "old_string": "<existing line(s) to replace>",
            "new_string": "<replacement line(s)>",
        })
        events.append(FrictionEvent(
            category="T5.18",
            bad_turn=t,
            context=_context_turns(turns, i),
            ideal_call=ideal,
            file_path=fp,
            source=source,
        ))
    return events


def detect_t411(turns: list[Turn], source: str) -> list[FrictionEvent]:
    """T4.11: action='write' on JSON file drops top-level keys."""
    events = []
    for i, t in enumerate(turns):
        if t.func != "file":
            continue
        if t.args.get("action") != "write":
            continue
        result = t.result or ""
        if "[schema-warning]" not in result:
            continue
        fp = t.args.get("path", "unknown")
        # Extract dropped keys from warning text
        m = re.search(r"dropped top-level keys.*?:\s*\[([^\]]+)\]", result)
        dropped = []
        if m:
            dropped = [k.strip().strip("'\"") for k in m.group(1).split(",")]
        written_content = t.args.get("content", "")
        # Ideal: use edit instead of write, preserving keys
        ideal = _tc_json("file", {
            "action": "edit",
            "path": fp,
            "old_string": "<field_to_change>: <old_value>",
            "new_string": "<field_to_change>: <new_value>",
        })
        events.append(FrictionEvent(
            category="T4.11",
            bad_turn=t,
            context=_context_turns(turns, i),
            ideal_call=ideal,
            file_path=fp,
            dropped_keys=dropped,
            source=source,
        ))
    return events


def detect_j01(turns: list[Turn], source: str) -> list[FrictionEvent]:
    """J.01: action='write' on a .jsonl file (should be append)."""
    events = []
    for i, t in enumerate(turns):
        if t.func != "file":
            continue
        if t.args.get("action") != "write":
            continue
        fp = t.args.get("path", "")
        if not fp.endswith(".jsonl"):
            continue
        # Exclude new-file writes (no prior read in context)
        ctx = _context_turns(turns, i, window=10)
        prior_read = any(
            c.func == "file" and c.args.get("action") == "read" and c.args.get("path") == fp
            for c in ctx
        )
        if not prior_read:
            continue  # might be a legitimate new-file write
        content = t.args.get("content", "")
        ideal = _tc_json("file", {
            "action": "append",
            "path": fp,
            "content": content.rstrip("\n") + "\n",
        })
        events.append(FrictionEvent(
            category="J.01",
            bad_turn=t,
            context=ctx,
            ideal_call=ideal,
            file_path=fp,
            source=source,
        ))
    return events


def detect_d01(turns: list[Turn], source: str) -> list[FrictionEvent]:
    """D.01: same file read 2+ times in a 10-turn window."""
    events = []
    window = 10
    for i, t in enumerate(turns):
        if t.func != "file" or t.args.get("action") != "read":
            continue
        fp = t.args.get("path", "")
        ctx = _context_turns(turns, i, window=window)
        prior_reads = [
            c for c in ctx
            if c.func == "file" and c.args.get("action") == "read" and c.args.get("path") == fp
        ]
        if not prior_reads:
            continue
        # Ideal: skip the redundant read, reference earlier result
        ideal = (
            f"I already read `{fp}` earlier in this cycle. "
            f"The content is cached — I will proceed using what I already know."
        )
        events.append(FrictionEvent(
            category="D.01",
            bad_turn=t,
            context=ctx,
            ideal_call=ideal,
            file_path=fp,
            source=source,
        ))
    return events


HEREDOC_RE = re.compile(r"cat\s+(?:>>|>)\s+\S+\s+<<\s*['\"]?EOF")

def detect_h01(turns: list[Turn], source: str) -> list[FrictionEvent]:
    """H.01: exec_command heredoc write (should use file tool instead)."""
    events = []
    for i, t in enumerate(turns):
        if t.func != "exec_command":
            continue
        cmd = t.args.get("command", "")
        if not HEREDOC_RE.search(cmd):
            continue
        # Extract target path from the heredoc command
        pm = re.search(r"cat\s+(?:>>|>)\s+(\S+)\s+<<", cmd)
        fp = pm.group(1) if pm else "unknown"
        action = "append" if ">>" in cmd else "write"
        ideal = _tc_json("file", {
            "action": action,
            "path": fp,
            "content": "<content that was in the heredoc>",
        })
        events.append(FrictionEvent(
            category="H.01",
            bad_turn=t,
            context=_context_turns(turns, i),
            ideal_call=ideal,
            file_path=fp,
            source=source,
        ))
    return events


# ---------------------------------------------------------------------------
# Example generators
# ---------------------------------------------------------------------------

def _make_user_task(event: FrictionEvent) -> str:
    fp = event.file_path or "a file"
    cat = event.category
    if cat == "T5.18":
        return (
            f"`{fp}` exists on disk. Make a targeted change to it — "
            f"update only the field that needs to change, leaving everything else intact."
        )
    if cat == "T4.11":
        keys = ", ".join(f"`{k}`" for k in event.dropped_keys) if event.dropped_keys else "existing keys"
        return (
            f"Update `{fp}`. Only the value you're changing needs to differ — "
            f"preserve {keys} and all other top-level fields exactly as they are."
        )
    if cat == "J.01":
        return (
            f"`{fp}` is an append-only JSONL log. Add a new entry to it "
            f"without touching any existing lines."
        )
    if cat == "D.01":
        return (
            f"Continue working with `{fp}`. You read it earlier this cycle "
            f"and already have the content — use what you already know."
        )
    if cat == "H.01":
        return (
            f"Write content to `{fp}`. Use the file() tool directly — "
            f"shell heredocs are fragile and bypass write guards."
        )
    return f"Perform a targeted update on `{fp}`."


def event_to_example(event: FrictionEvent) -> dict:
    """Convert a FrictionEvent to a Unsloth ShareGPT training example."""
    user_task = _make_user_task(event)

    # Build a minimal context from the friction window
    convo = [
        {
            "role": "system",
            "content": [{"type": "text", "text": SYSTEM_PROMPT}],
        },
        {
            "role": "user",
            "content": [{"type": "text", "text": user_task}],
        },
    ]

    # Inject up to 3 prior tool turns for context realism
    relevant_ctx = [
        c for c in event.context
        if c.func in ("file", "exec_command", "think") and c.result
    ][-3:]
    for c in relevant_ctx:
        convo.append({
            "role": "assistant",
            "content": [{"type": "text", "text": _tc_json(c.func, c.args or {})}],
        })
        convo.append({
            "role": "user",  # Gemma 4 chat template has no 'tool' role; tool results come back as user turns
            "content": [{"type": "text", "text": (c.result or "")[:500]}],
        })

    # The ideal final assistant turn
    convo.append({
        "role": "assistant",
        "content": [{"type": "text", "text": event.ideal_call}],
    })

    return {
        "conversations": convo,
        "_meta": {
            "category": event.category,
            "file_path": event.file_path,
            "source": os.path.basename(event.source),
        },
    }


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _example_key(ex: dict) -> str:
    """Deduplicate by (category, file_path, ideal_call snippet)."""
    meta = ex.get("_meta", {})
    ideal = ex["conversations"][-1]["content"][0]["text"][:120]
    return f"{meta.get('category')}|{meta.get('file_path')}|{ideal}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def collect_logs(roots: list[str]) -> list[Path]:
    logs = []
    for root in roots:
        p = Path(root)
        if p.is_file():
            logs.append(p)
        elif p.is_dir():
            for ext in ("*.log",):
                logs.extend(sorted(p.glob(ext)))
    return logs


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--logs", nargs="+", required=True,
                    help="Log files or directories to mine")
    ap.add_argument("--out", required=True,
                    help="Output JSONL path")
    ap.add_argument("--limit", type=int, default=200,
                    help="Max examples to emit (default 200)")
    ap.add_argument("--categories", nargs="*",
                    default=["T5.18", "T4.11", "J.01", "D.01", "H.01"],
                    help="Friction categories to include")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    detectors = {
        "T5.18": detect_t518,
        "T4.11": detect_t411,
        "J.01":  detect_j01,
        "D.01":  detect_d01,
        "H.01":  detect_h01,
    }

    log_paths = collect_logs(args.logs)
    print(f"Scanning {len(log_paths)} log files…")

    all_events: list[FrictionEvent] = []
    for lp in log_paths:
        turns = load_log(lp)
        if not turns:
            continue
        count_before = len(all_events)
        for cat in args.categories:
            if cat in detectors:
                found = detectors[cat](turns, str(lp))
                all_events.extend(found)
        n = len(all_events) - count_before
        if args.verbose and n:
            print(f"  {lp.name}: {n} events")

    print(f"Found {len(all_events)} raw friction events")

    # Convert to examples + deduplicate
    seen = set()
    examples = []
    for ev in all_events:
        ex = event_to_example(ev)
        key = _example_key(ex)
        if key in seen:
            continue
        seen.add(key)
        examples.append(ex)

    # Cap per category to keep mix balanced
    per_cat_limit = max(1, args.limit // len(args.categories))
    cat_counts: dict[str, int] = {}
    final = []
    for ex in examples:
        cat = ex["_meta"]["category"]
        cat_counts.setdefault(cat, 0)
        if cat_counts[cat] >= per_cat_limit:
            continue
        cat_counts[cat] += 1
        final.append(ex)
        if len(final) >= args.limit:
            break

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        for ex in final:
            fh.write(json.dumps(ex) + "\n")

    print(f"\nWrote {len(final)} examples to {out}")
    print("Category breakdown:")
    for cat, n in sorted(cat_counts.items()):
        print(f"  {cat}: {n}")


if __name__ == "__main__":
    main()
