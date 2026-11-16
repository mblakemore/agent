"""run_agent_interactive is a thin wrapper over _Session: steps run in order, a step
returning _END ends the session, and the wrapper's signature is the class's."""
import inspect

import agent


def test_steps_order():
    assert [s.__name__ for s in agent._Session._STEPS] == [
        "_boot", "_backends", "_announce", "_summarizer", "_resume", "_front_end",
        "_first_prompt", "_live_loop", "_blocking_loop", "_shutdown"]


def test_run_stops_at_end():
    seen = []
    def a(self): seen.append("a")
    def b(self): seen.append("b"); return agent._END
    def c(self): seen.append("c")
    s = agent._Session.__new__(agent._Session)
    s._STEPS = (a, b, c)
    assert s.run() is None and seen == ["a", "b"]


def test_wrapper_signature_matches_the_class():
    sig = inspect.signature(agent.run_agent_interactive)
    assert list(sig.parameters) == ["initial_prompt", "auto", "continue_mode", "cb", "tui", "verbose", "result_file"]
    init = [p for p in inspect.signature(agent._Session.__init__).parameters if p != "self"]
    assert init == list(sig.parameters)
    assert sig.parameters["cb"].kind is inspect.Parameter.KEYWORD_ONLY
