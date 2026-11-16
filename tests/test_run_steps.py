"""_Run: the turn loop as steps. Each step returns None to go on, _NEXT for the next turn
or the run's result; run() honours that protocol in step order."""
import inspect

import agent


def test_steps_are_the_turn_in_order():
    assert [f.__name__ for f in agent._Run._STEPS] == [
        "_begin_turn", "_request", "_stream", "_record_response",
        "_text_only", "_tool_guard", "_dispatch", "_end_turn"]
    for f in agent._Run._STEPS:
        assert getattr(agent._Run, f.__name__) is f


def test_run_protocol_next_turn_and_result():
    calls = []
    def step(name, answers):
        def f(self):
            calls.append(name)
            return answers.pop(0)
        return f
    r = agent._Run.__new__(agent._Run)
    r._STEPS = (step("a", [None, None]), step("b", [agent._NEXT, "done"]), step("c", [None]))
    assert agent._Run.run(r) == "done"
    # turn 1: a, b -> _NEXT (c skipped); turn 2: a, b -> "done"
    assert calls == ["a", "b", "a", "b"]


def test_thin_wrapper_keeps_the_signature():
    sig = inspect.signature(agent._run_agent_single_impl)
    assert list(sig.parameters)[:4] == ["conversation_history", "summary_state", "initial_files", "log"]
    assert sig.parameters["start_turn"].default == 0
    assert agent.run_agent_single.__wrapped__ is agent._run_agent_single_impl
    assert list(inspect.signature(agent._Run.__init__).parameters)[1:] == list(sig.parameters)


def test_dispatch_call_phases_and_break():
    """_dispatch runs one call at a time; _BREAK ends the batch, a result ends the run."""
    names = ["_parse_call", "_dedup_hit", "_gate_and_execute", "_finish_result", "_write_loop_check",
             "_record_result", "_exec_followups", "_result_loop_checks", "_after_result"]
    for n in names:
        assert callable(getattr(agent._Run, n))
    seen = []
    class _R(agent._Run):
        def __init__(self):
            self.tool_calls = ["a", "b", "c"]
            self._deadline_hard = self._refusal_hard = False
            self.log = agent.logging.getLogger("t")
        def _dispatch_call(self, tc):
            seen.append(tc)
            return agent._BREAK if tc == "b" else None
    assert _R()._dispatch() is None and seen == ["a", "b"]
    seen.clear()
    class _R2(_R):
        def _dispatch_call(self, tc):
            seen.append(tc)
            return "done" if tc == "a" else None
    assert _R2()._dispatch() == "done" and seen == ["a"]
