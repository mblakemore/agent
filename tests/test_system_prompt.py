"""system_prompt.py: the per-turn lines, the static/dynamic split and the request body."""
import skills
import system_prompt as sp
import tools


def test_per_turn_lines_order_and_optional_parts():
    lines = sp.per_turn_lines(
        "2026-01-01T00:00:00Z", "/w", catalog_line=tools.CATALOG_PREFIX + " a, b",
        skill_catalog=skills.CATALOG_PREFIX + " s", skill_suggestion="[skill: s]",
        restart={"by": "tool", "reason": "r"}, phase_line="PHASE 1", cicd_line="CICD c",
        tool_hints=True, read_only_turns=5, read_only_threshold=5, repeat_msg="again")
    assert lines[0] == "Current time (UTC, ISO8601): 2026-01-01T00:00:00Z"
    assert lines[1] == "Working directory: /w" + sp.MODEL_GUARD_CWD
    assert lines[2].startswith(tools.CATALOG_PREFIX)
    assert lines[3] is skills.INSTRUCTIONS and lines[4].startswith(skills.CATALOG_PREFIX)
    assert lines[5] == "[skill: s]"
    assert "restarted (by the restart tool: r)" in lines[6]
    assert lines[7:9] == ["PHASE 1", "CICD c"]
    assert lines[9] == sp.TOOL_HINTS
    assert "5 consecutive turns" in lines[10]
    assert lines[11] == "again"


def test_per_turn_lines_minimal_and_no_guards():
    assert sp.per_turn_lines("t", "/w", model_guards=False) == [
        "Current time (UTC, ISO8601): t", "Working directory: /w"]
    # below the threshold no inertia nudge; an empty catalog adds nothing
    assert len(sp.per_turn_lines("t", "/w", catalog_line="", skill_catalog="", read_only_turns=4)) == 2


def test_split_keeps_only_run_stable_lines_in_the_head():
    lines = sp.per_turn_lines("t", "/w", catalog_line=tools.CATALOG_PREFIX + " a",
                              skill_catalog=skills.CATALOG_PREFIX + " s", tool_hints=True,
                              phase_line="PHASE", repeat_msg="again")
    static, dynamic = sp.split(lines, caching=False, user_prompt="  be brief ")
    assert static[0] == "be brief"
    assert static[1].startswith("Working directory:")
    assert skills.INSTRUCTIONS in static and sp.TOOL_HINTS in static
    assert dynamic == ["Current time (UTC, ISO8601): t", "PHASE", "again"]
    # a caching backend keeps nothing run-specific in the head
    static_c, dynamic_c = sp.split(lines, caching=True)
    assert not any(ln.startswith("Working directory:") for ln in static_c)
    assert dynamic_c[1] == "Working directory: /w" + sp.MODEL_GUARD_CWD
    assert static_c[0].startswith(tools.CATALOG_PREFIX)


def test_outgoing_shape():
    hist = [{"role": "user", "content": "hi"}]
    out = sp.outgoing(["S1", "S2"], hist, ["D1", "D2"])
    assert out[0] == {"role": "system", "content": "S1\n\nS2"}
    assert out[1] is hist[0]
    assert out[2] == {"role": "user", "content": sp.NOTE_HEAD + "\nD1\n\nD2"}
    assert sp.outgoing([], hist, []) == hist


def test_persist_runtime_note_is_idempotent_per_turn():
    hist = [{"role": "user", "content": "task"}]
    sp.persist_runtime_note(hist, None, ["clock 1"], turn=1)
    sp.persist_runtime_note(hist, None, ["clock 1"], turn=1)
    assert hist[0]["content"] == "task\n\n" + sp.RUNTIME_NOTE_HEAD + "\nclock 1"
    sent = []
    sp.persist_runtime_note(hist, sent, ["clock 2"], turn=2)     # noted earlier: a new message
    assert len(hist) == 2 and hist[1]["_note_turn"] == 2 and sent == [hist[1]]
