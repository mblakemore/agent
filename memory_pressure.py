"""Memory pressure: the process's own footprint, and tool output too large for the
window (agent.py's `_check_memory_watermark`, `_release_memory`, the spill).

Python's allocator keeps freed memory as fragmented arenas, so a long session's RSS
climbs without a live leak; `release_memory` runs gc and glibc's malloc_trim at the end
of each turn and logs what came back. `check_memory_watermark` reads VmRSS once per turn:
over MEM_WARN_MB it reports pressure, over MEM_HARD_MB it ends the session cleanly
through agent.py's hook (a spend summary, then the memory exit code) before the OOM
killer can take the terminal with it. The spill writes an oversized tool result or
prompt to .agent/spill/ and leaves a reference with a head and tail excerpt in its
place; `spill_oversized_tool_results` is the first stage of context-overflow recovery.
"""
import ctypes
import gc
import os
import re
import sys
import time

DEPS = {"hard_limit_exit": lambda log, why: sys.exit(13)}


def configure(**deps):
    unknown = set(deps) - set(DEPS)
    if unknown:
        raise KeyError(f"unknown memory_pressure dependency: {sorted(unknown)}")
    DEPS.update(deps)


#
# Python's default allocator (pymalloc) does NOT return freed memory to the
# OS well — repeated alloc/free of large objects (like per-turn tokenizer
# encodes) fragments the heap and keeps RSS high even when Python has
# garbage-collected the objects themselves. Long-running agent sessions hit
# OOM because of this fragmentation, not because of a live leak.
#
# `release_memory()` runs at a natural seam (end of each turn) to:
#   1. `gc.collect()` — finalize any Python garbage now
#   2. `libc.malloc_trim(0)` — ask glibc to return unused arenas to the OS
# and logs `mem.trim released=N vmrss_mb=M` so we can verify it's working.
#
# `check_memory_watermark()` reads /proc/self/status once per turn:
#   - VmRSS > MEM_WARN_MB → log mem.watermark (caller may force summary)
#   - VmRSS > MEM_HARD_MB → log mem.hard_limit, exit(2) cleanly (no OOM,
#                             no tmux death, no systemd scope cleanup)

MEM_WARN_MB = int(os.environ.get("AGENT_MEM_WARN_MB", 8192))
MEM_HARD_MB = int(os.environ.get("AGENT_MEM_HARD_MB", 12288))

try:
    _libc = ctypes.CDLL("libc.so.6")
    MALLOC_TRIM_AVAILABLE = True
except (OSError, AttributeError):
    _libc = None
    MALLOC_TRIM_AVAILABLE = False


def read_vmrss_mb():
    """Read VmRSS from /proc/self/status and return MB. 0 on any error."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def release_memory(log):
    """Force a GC + malloc_trim and log the result.

    Cheap (~10ms typical) and idempotent. Intended to run at the end of each
    agent turn. Uses environment override ``AGENT_DISABLE_MEM_TRIM=1`` to
    skip for benchmarking / A-B testing the effect.
    """
    if os.environ.get("AGENT_DISABLE_MEM_TRIM") == "1":
        return
    gc.collect()
    released = 0
    if MALLOC_TRIM_AVAILABLE:
        try:
            released = int(_libc.malloc_trim(0))
        except Exception:
            released = 0
    rss_mb = read_vmrss_mb()
    log.info(
        "mem.trim released=%d vmrss_mb=%d trim_available=%s",
        released,
        rss_mb,
        MALLOC_TRIM_AVAILABLE,
    )


def check_memory_watermark(log):
    """Check current RSS against configured watermarks.

    Returns:
        ``"ok"``       — under ``MEM_WARN_MB``
        ``"pressure"`` — over ``MEM_WARN_MB`` (caller may take action)
        ``"abort"``    — over ``MEM_HARD_MB`` (process exits before return)
    """
    rss_mb = read_vmrss_mb()
    if rss_mb <= 0:
        return "ok"  # couldn't read — don't interfere
    if rss_mb > MEM_HARD_MB:
        log.error(
            "mem.hard_limit vmrss_mb=%d (limit %d) — exiting session cleanly "
            "before OOM killer destroys the tmux scope",
            rss_mb,
            MEM_HARD_MB,
        )
        # agent.py's hook: the spend summary, then the clean exit (EXIT_MEMORY).
        DEPS["hard_limit_exit"](log, f"vmrss_mb={rss_mb} over hard limit {MEM_HARD_MB}")
    if rss_mb > MEM_WARN_MB:
        log.warning(
            "mem.watermark vmrss_mb=%d (warn %d) — memory pressure",
            rss_mb,
            MEM_WARN_MB,
        )
        return "pressure"
    return "ok"


SPILL_DIR = os.path.join(".agent", "spill")
SPILL_MARKER = "[tool output spilled to file: "
SPILL_THRESHOLD = int(os.environ.get("AGENT_CTX_SPILL_THRESHOLD", "4000"))
SPILL_MIN_THRESHOLD = 1000   # successive spill passes halve toward this floor
SPILL_PREVIEW = 200          # chars of head and tail kept inline for orientation


def spill_tool_result(turn_idx, name, content, log, kind="tool"):
    """F3 (stage 2 of the spill arc): a SINGLE oversized payload — a tool result at the
    moment it is produced, or an inbound prompt/message (F3a) — is written to
    .agent/spill/<turn>-<name>.txt and replaced by a reference the model can act on:
    path, size, line count, head/tail excerpt, and the instruction to read it in slices.
    Same file convention as the overflow-time spill so downstream tooling sees one shape.
    Returns the replacement text; on a write failure returns None (caller falls back to
    the legacy in-place truncation — never die for want of a scratch file)."""
    try:
        os.makedirs(SPILL_DIR, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name))[:40] or kind
        fpath = os.path.join(SPILL_DIR, f"{int(turn_idx):04d}-{safe}-{int(time.time())}.txt")
        with open(fpath, "w", encoding="utf-8", errors="replace") as f:
            f.write(content)
    except OSError as e:
        log.warning("F3 spill: could not write %s (%s) — falling back to truncation", name, e)
        return None
    n_lines = content.count("\n") + 1
    return (f"{SPILL_MARKER}{fpath}] {len(content)} chars, {n_lines} lines — too large for the "
            f"context window, saved to that file. Read it in slices (read_file with a line range) or "
            f"search it; do NOT re-request the whole payload.\n"
            f"--- head ---\n{content[:SPILL_PREVIEW]}\n--- tail ---\n{content[-SPILL_PREVIEW:]}")


def spill_oversized_tool_results(conversation_history, log, threshold):
    """Context-overflow recovery, stage 1 (before message trimming): move bulk tool-result
    payloads out of the conversation and into files, leaving a reference + short preview.

    WHY (observed in a live deployment, 2026-08-24): a research worker died on
    'context overflow: still failing after 10 reductions' against a 196k-token window —
    the window wasn't small, the MESSAGES were huge: raw price histories echoed into
    role:'tool' contents. Trimming whole messages loses recent work; the payload the
    model usually needs is 'that data exists at this path', not the bytes themselves.
    Spilled files land under .agent/spill/ IN CWD, so the (cwd-jailed) file tool can
    re-read any spilled payload on demand.

    Only role:'tool' string contents above `threshold` chars are touched; already-spilled
    messages (marker prefix) are skipped, so the pass is idempotent. Returns the number
    of messages spilled; 0 means nothing left to spill at this threshold and the caller
    should fall through to message trimming.
    """
    spilled = 0
    for idx, m in enumerate(conversation_history):
        if m.get("role") != "tool":
            continue
        content = m.get("content")
        if not isinstance(content, str) or len(content) <= threshold:
            continue
        if content.startswith(SPILL_MARKER):
            continue
        try:
            os.makedirs(SPILL_DIR, exist_ok=True)
            fname = f"{idx:04d}-{m.get('name', 'tool')}-{int(time.time())}.txt"
            fpath = os.path.join(SPILL_DIR, fname)
            with open(fpath, "w", encoding="utf-8", errors="replace") as f:
                f.write(content)
        except OSError as e:
            # A failed spill must not eat the content — leave the message intact and
            # let the trimming fallback handle pressure the old way.
            log.warning("Context spill: could not write %s (%s) — leaving message inline",
                        m.get("name", "tool"), e)
            continue
        head = content[:SPILL_PREVIEW]
        tail = content[-SPILL_PREVIEW:]
        m["content"] = (
            f"{SPILL_MARKER}{fpath}]\n"
            f"[{len(content)} chars of '{m.get('name', 'tool')}' output moved out of context "
            f"to keep the conversation inside the window. Read the file if you need the "
            f"data again — do NOT re-run the tool just to re-see it.]\n"
            f"[first {SPILL_PREVIEW} chars]: {head}\n"
            f"[last {SPILL_PREVIEW} chars]: {tail}")
        spilled += 1
        log.warning("Context spill: %d chars of tool '%s' (msg %d) -> %s",
                    len(content), m.get("name", "tool"), idx, fpath)
    return spilled






