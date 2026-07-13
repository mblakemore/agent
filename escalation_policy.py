"""escalation_policy — WHEN the fast driver should consult the advisor tier.

SPIKE (plan/spikes/). Pure function, no I/O, unit-testable. The counterpart to
consult_advisor.py (which is the HOW). This is the routing brain: it turns loop
signals into a go/no-go for the heavyweight GLM tier.

Design stance (mirrors how a good agent uses an advisor): escalate at high-stakes
FORKS, not on the median turn. Depth is worth minutes only for the rare hard
sub-problem. Everything here is biased toward NOT escalating, because a false
escalation costs minutes of GLM latency for nothing.

Two escalation MODES (see the spike md):
  - "advisor"  : bounded question → one GLM answer → driver keeps the loop.
                 DEFAULT. Cost capped to a single query.
  - "takeover" : GLM drives a sub-loop (via subagent with model=advisor).
                 UNBOUNDED latency — every turn pays 0.9 tok/s. Reserved for a
                 narrow, pre-identified class the fast model keeps failing.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LoopSignals:
    """Everything the router needs, harvested from the agent loop state."""
    # Model-initiated (strongest, cheapest signal — like choosing to ask).
    self_reported_stuck: bool = False       # driver emitted low-confidence / "stuck"
    explicit_tool_call: bool = False        # driver called consult_advisor itself

    # Structural failure (the strongest AUTOMATIC trigger).
    consecutive_gate_failures: int = 0      # same verify/test/DECIDE gate failing
    repeated_tool_error: int = 0            # same tool erroring in a row

    # Upfront complexity prior (soft — a hint, not a hard route).
    task_difficulty: str = "unknown"        # "E" | "M" | "H" | "unknown"

    # High-consequence, hard-to-reverse action about to happen.
    irreversible_action: Optional[str] = None   # e.g. "git push", "rm -rf", "plan commit"

    # Budget state (hard guards).
    advisor_calls_used: int = 0
    advisor_calls_cap: int = 3
    called_this_turn: bool = False          # cooldown: <=1 escalation per turn

    # Whether a compact brief can be produced under the prefill ceiling.
    brief_fits_budget: bool = True


@dataclass
class EscalationDecision:
    escalate: bool
    mode: str = "advisor"                    # "advisor" | "takeover"
    reason: str = ""
    triggers: list = field(default_factory=list)


# The narrow class where a bounded question is NOT enough and sustained
# multi-step GLM reasoning earns its latency. Keep this list SHORT.
_TAKEOVER_CLASS = {"H"}   # hardest issues, and only after repeated fast-model failure


def should_escalate(s: LoopSignals) -> EscalationDecision:
    """Return the escalation decision for the current loop state.

    Priority: hard budget guards first (never escalate if spent), then the
    trigger cascade from strongest/cheapest signal to weakest.
    """
    # --- hard guards: protect the latency budget ------------------------------
    if s.advisor_calls_used >= s.advisor_calls_cap:
        return EscalationDecision(False, reason="budget spent",
                                  triggers=["budget"])
    if s.called_this_turn:
        return EscalationDecision(False, reason="cooldown: already escalated this turn",
                                  triggers=["cooldown"])
    if not s.brief_fits_budget:
        # Can't compress under the prefill ceiling → a bounded advisor call would
        # blow GLM's ~3 pos/s prefill. Only a deliberate takeover is justified,
        # and only for the hardest class.
        if s.task_difficulty in _TAKEOVER_CLASS and s.consecutive_gate_failures >= 2:
            return EscalationDecision(True, "takeover",
                                      "context too large to distill; hard task "
                                      "failing repeatedly → deliberate takeover",
                                      ["no_brief", "H", "gate_fail"])
        return EscalationDecision(False, reason="context won't fit prefill budget",
                                  triggers=["no_brief"])

    triggers = []

    # --- trigger cascade (strongest → weakest) --------------------------------
    # 1. Explicit tool call: the driver asked. Always honor (budget permitting).
    if s.explicit_tool_call:
        triggers.append("explicit")

    # 2. Self-reported stuck: model-initiated, high signal.
    if s.self_reported_stuck:
        triggers.append("self_stuck")

    # 3. Structural failure: the strongest automatic trigger. Two strikes.
    if s.consecutive_gate_failures >= 2:
        triggers.append("gate_fail_x{}".format(s.consecutive_gate_failures))
    if s.repeated_tool_error >= 2:
        triggers.append("tool_error_x{}".format(s.repeated_tool_error))

    # 4. Irreversible action guard: consult once before a hard-to-undo step.
    if s.irreversible_action:
        triggers.append("irreversible:{}".format(s.irreversible_action))

    if not triggers:
        # NOTE: task_difficulty == "H" alone is deliberately NOT a trigger — an
        # upfront prior over-escalates. It only *promotes to takeover* once a
        # real failure signal is also present (below).
        return EscalationDecision(False, reason="no trigger; median turn",
                                  triggers=[])

    # --- mode selection -------------------------------------------------------
    # Promote to takeover only for the hardest class AND a genuine repeated
    # failure — never for a single explicit ask or an irreversible-action guard.
    hard_loop = (s.task_difficulty in _TAKEOVER_CLASS
                 and s.consecutive_gate_failures >= 2)
    mode = "takeover" if hard_loop else "advisor"

    return EscalationDecision(True, mode, "; ".join(triggers), triggers)


# --- smoke test (python3 escalation_policy.py) --------------------------------
if __name__ == "__main__":
    cases = [
        ("median turn", LoopSignals(), False, None),
        ("explicit ask", LoopSignals(explicit_tool_call=True), True, "advisor"),
        ("stuck", LoopSignals(self_reported_stuck=True), True, "advisor"),
        ("gate x2", LoopSignals(consecutive_gate_failures=2), True, "advisor"),
        ("gate x1 only", LoopSignals(consecutive_gate_failures=1), False, None),
        ("irreversible", LoopSignals(irreversible_action="git push"), True, "advisor"),
        ("H + gate x2 → takeover",
         LoopSignals(task_difficulty="H", consecutive_gate_failures=2), True, "takeover"),
        ("H alone, no failure → no escalate",
         LoopSignals(task_difficulty="H"), False, None),
        ("budget spent",
         LoopSignals(explicit_tool_call=True, advisor_calls_used=3), False, None),
        ("cooldown",
         LoopSignals(explicit_tool_call=True, called_this_turn=True), False, None),
        ("no brief + H + gate → takeover",
         LoopSignals(task_difficulty="H", consecutive_gate_failures=2,
                     brief_fits_budget=False), True, "takeover"),
        ("no brief, easy → no escalate",
         LoopSignals(self_reported_stuck=True, brief_fits_budget=False), False, None),
    ]
    ok = True
    for name, sig, want_esc, want_mode in cases:
        d = should_escalate(sig)
        pass_esc = d.escalate == want_esc
        pass_mode = (want_mode is None) or (d.mode == want_mode)
        status = "ok " if (pass_esc and pass_mode) else "FAIL"
        if status == "FAIL":
            ok = False
        print(f"[{status}] {name:38s} → escalate={d.escalate} mode={d.mode} "
              f"({d.reason})")
    print("\nALL PASS" if ok else "\nSOME FAILED")
    raise SystemExit(0 if ok else 1)
