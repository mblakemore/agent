"""PhaseEngine — code-level phase machine for agent.py (WS1, wave 2).

Turns the six-phase cognitive loop (PERCEIVE → REFLECT → DECIDE → ACT →
CONSOLIDATE → PERSIST) from prompt prose + seeded task_tracker strings into
enforced structure. Design doc: plan/six-phase-upgrade-plan.md WS1;
brief: plan/briefs/ws1-phase-engine.md.

Opt-in and inert by default: agent.py only constructs an engine when config
contains ``cycle.profile`` (or an explicit ``cycle.phases`` list). Absent
config → no engine → behavior identical to before this module existed.

v1 enforcement policy (deliberately conservative — see brief; structure must
not break the consumers' real workflows, e.g. lyla runs `git log` in
PERCEIVE and memory-op node commands in CONSOLIDATE):
  - File-WRITE tool calls are blocked during PERCEIVE / REFLECT / DECIDE
    (write-before-read class; the fix-forward for friction T5.18/T4.11).
  - ``end_cycle`` is only allowed in PERSIST (or when no engine is active).
  - DECIDE entry has a verification gate: at least one ``think`` (or
    ``verify``) call must have happened since the cycle started, else the
    transition is soft-blocked with a redirect (one retry, then allowed —
    a gate that can deadlock a weak model is worse than none).
  - Everything else is allowed everywhere; per-phase ``allowed_tools`` in
    config tightens further for stricter setups (model-capability profiles).

Phase advancement is observational: the creatures already mark phases done
via ``task_tracker(action='done', description='PERCEIVE')`` — the engine
listens to executed tool calls and advances on those markers. It never
blocks forward progress on mis-ordered markers; it records and nudges.
"""

from __future__ import annotations

import re

SIX_PHASES = ["PERCEIVE", "REFLECT", "DECIDE", "ACT", "CONSOLIDATE", "PERSIST"]

# CICD's 8-phase vocabulary maps onto the 6-phase spine (PROBE folds into
# PERCEIVE; PLAN/IMPLEMENT/VERIFY are ACT sub-phases; TRACK is CONSOLIDATE).
CICD_TO_SIX = {
    "PERCEIVE": "PERCEIVE", "PROBE": "PERCEIVE", "REFLECT": "REFLECT",
    "DECIDE": "DECIDE", "PLAN": "ACT", "IMPLEMENT": "ACT", "VERIFY": "ACT",
    "TRACK": "CONSOLIDATE",
}

# Tool calls that mutate files. ("file" is action-dependent.)
_WRITE_TOOLS = {"write_file", "edit_file", "append_file"}
_FILE_WRITE_ACTIONS = {"write", "edit", "append", "insert", "replace", "delete"}
# Phases in which file writes are premature.
_READ_ONLY_PHASES = {"PERCEIVE", "REFLECT", "DECIDE"}
# Tools that satisfy the DECIDE verification gate.
_VERIFY_TOOLS = {"think", "verify"}

DEFAULT_PROFILES = {
    "creature-6phase": {"phases": [{"name": p} for p in SIX_PHASES]},
    # cicd-8phase is observational-only in v1: the CICD guards in agent.py
    # already police that loop; the engine just tracks/reports.
    "cicd-8phase": {"phases": [{"name": p} for p in CICD_TO_SIX],
                    "observe_only": True},
}


def _is_file_write(func_name, func_args):
    if func_name in _WRITE_TOOLS:
        return True
    if func_name == "file" and isinstance(func_args, dict):
        return func_args.get("action", "") in _FILE_WRITE_ACTIONS
    return False


class PhaseEngine:
    """Tracks and (for non-observe profiles) enforces the phase loop.

    Not thread-safe; one engine per run_agent_single invocation.
    """

    def __init__(self, profile=None, phases=None, log=None):
        if phases is None:
            prof = DEFAULT_PROFILES.get(profile or "")
            if prof is None:
                raise ValueError(f"unknown phase profile: {profile!r}")
            phases = prof["phases"]
            self.observe_only = bool(prof.get("observe_only"))
        else:
            self.observe_only = False
        self.profile = profile or "custom"
        self.phases = [dict(p) for p in phases]
        self.names = [str(p.get("name", "")).upper() for p in self.phases]
        if not self.names or any(not n for n in self.names):
            raise ValueError("phases must be a non-empty list of {name: ...}")
        self.idx = 0
        self.done = []
        self.log = log
        self.verify_calls = 0          # think/verify calls this cycle
        self.decide_gate_warned = False
        self.persisted = False         # primary-task-complete latch (WS1)
        self.blocked_count = 0

    # ------------------------------------------------------------ state

    @property
    def current(self):
        return self.names[self.idx] if self.idx < len(self.names) else self.names[-1]

    def checkpoint_line(self):
        done = ",".join(self.done) if self.done else "-"
        return (f"PHASE CHECKPOINT ({self.profile}): current={self.current} "
                f"({min(self.idx + 1, len(self.names))}/{len(self.names)}) | "
                f"done: {done} | persisted: {'yes' if self.persisted else 'no'}")

    def mark_persisted(self, source=""):
        """Primary-task-complete latch — set at write sites (git push,
        reviewer verdict, results file), never recomputed."""
        if not self.persisted and self.log:
            self.log.info("PhaseEngine: persisted-work latch set (%s)", source)
        self.persisted = True

    # ------------------------------------------------------------ gates

    def allow(self, func_name, func_args):
        """Pre-dispatch gate. Returns (allowed, message)."""
        if self.observe_only:
            return True, None

        phase = self.current

        # Per-phase explicit whitelist (config-tightened profiles).
        allowed_tools = self.phases[self.idx].get("allowed_tools")
        if allowed_tools and func_name not in allowed_tools:
            self.blocked_count += 1
            return False, (
                f"Error: tool '{func_name}' is not allowed during {phase} "
                f"(profile {self.profile}). Allowed now: "
                f"{', '.join(allowed_tools)}. The call was NOT executed."
            )

        # end_cycle only in PERSIST.
        if func_name == "end_cycle" and phase != "PERSIST":
            self.blocked_count += 1
            return False, (
                f"Error: end_cycle blocked — you are in {phase}, not PERSIST. "
                "Finish CONSOLIDATE (store patterns/learnings) and PERSIST "
                "(save state + git commit) first, marking each phase done via "
                "task_tracker. The call was NOT executed."
            )

        # No file writes in read/decide phases.
        if phase in _READ_ONLY_PHASES and _is_file_write(func_name, func_args):
            self.blocked_count += 1
            path = ""
            if isinstance(func_args, dict):
                path = func_args.get("path", "") or func_args.get("file_path", "")
            return False, (
                f"Error: file write to '{path}' blocked — you are in {phase}, "
                "a read/think phase. Writes belong in ACT (work products) or "
                "CONSOLIDATE/PERSIST (memory, state). If you have finished "
                f"{phase}, mark it done first: "
                f"task_tracker(action='done', description='{phase}'). "
                "The call was NOT executed."
            )

        return True, None

    def gate_decide_entry(self):
        """Verification gate (Elder C2174): entering DECIDE without any
        think/verify this cycle gets ONE soft-block redirect, then passes.
        Returns (allowed, message)."""
        if self.verify_calls > 0 or self.decide_gate_warned or self.observe_only:
            return True, None
        self.decide_gate_warned = True
        return False, (
            "[SYSTEM: Verification gate — you are entering DECIDE without any "
            "think/verify call this cycle. Spend 5-30s verifying the key "
            "assumption behind your decision (run the cheap check, or call "
            "think) BEFORE committing. Verify → Commit → Execute, not "
            "Assume → Commit → Discover.]"
        )

    # ------------------------------------------------------------ observe

    def observe(self, func_name, func_args, result_str=""):
        """Post-execution observation: advance phases, count verify calls.
        Returns an optional message to inject (e.g. DECIDE gate)."""
        if func_name in _VERIFY_TOOLS:
            self.verify_calls += 1
            return None

        if func_name != "task_tracker" or not isinstance(func_args, dict):
            return None
        if func_args.get("action") != "done":
            return None
        desc = str(func_args.get("description", "")).upper()
        # Which phase (if any) does this completion marker name?
        named = None
        for name in self.names:
            if re.search(r"\b" + re.escape(name) + r"\b", desc):
                named = name
                break
        if named is None:
            return None

        if named == self.current:
            self.done.append(named)
            self.idx = min(self.idx + 1, len(self.names) - 1)
            if self.log:
                self.log.info("PhaseEngine: %s done → %s", named, self.current)
            # Entering DECIDE fires the verification gate.
            if self.current == "DECIDE":
                ok, msg = self.gate_decide_entry()
                if not ok:
                    return msg
        else:
            # Out-of-order marker: record, don't fight (v1 policy).
            if named not in self.done:
                self.done.append(named)
            if self.log:
                self.log.info(
                    "PhaseEngine: out-of-order phase marker %s (current %s)",
                    named, self.current)
        return None


def build_phase_engine(config, log=None):
    """Factory: returns a PhaseEngine or None (inert) based on config.

    Config surface (all optional):
      {"cycle": {"profile": "creature-6phase"}}
      {"cycle": {"phases": [{"name": "...", "allowed_tools": [...]}, ...]}}
    """
    # Strict type validation: tests (and misconfigs) hand agent.py MagicMock
    # or otherwise non-dict configs whose .get() returns truthy junk — the
    # engine must stay inert on ANYTHING that isn't an explicit, well-formed
    # opt-in.
    if not isinstance(config, dict):
        return None
    cycle_cfg = config.get("cycle")
    if not isinstance(cycle_cfg, dict):
        return None
    phases = cycle_cfg.get("phases")
    profile = cycle_cfg.get("profile")
    if not (isinstance(phases, list) and phases
            and all(isinstance(p, dict) and p.get("name") for p in phases)):
        phases = None
    if not isinstance(profile, str):
        profile = None
    if not phases and not profile:
        return None
    try:
        return PhaseEngine(profile=profile, phases=phases, log=log)
    except ValueError as e:
        if log:
            log.warning("PhaseEngine disabled: %s", e)
        return None
