"""_gate_judge: the summary model's one-shot answer for the gate, the skill pick and F3."""
import agent


class _Stub:
    def __init__(self):
        self.calls = []

    def complete(self, *, prompt, gen_params=None, cancel_check=None, timeout=120):
        self.calls.append({"prompt": prompt, "gen_params": gen_params, "timeout": timeout})
        return "wake"


class _PromptOnly:
    def __init__(self):
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        return "hold"


def test_judge_asks_for_a_plain_deterministic_completion(monkeypatch):
    stub = _Stub()
    monkeypatch.setattr(agent, "_summary_backend", stub)
    assert agent._gate_judge("is this worth a wake?", timeout=7) == "wake"
    assert agent._gate_judge("pick", timeout=3, max_tokens=350) == "wake"
    assert [c["gen_params"] for c in stub.calls] == [
        {"max_tokens": 200, "temperature": 0, "thinking": "off"},
        {"max_tokens": 350, "temperature": 0, "thinking": "off"}]
    assert [c["timeout"] for c in stub.calls] == [7, 3]


def test_judge_falls_back_to_a_prompt_only_backend(monkeypatch):
    stub = _PromptOnly()
    monkeypatch.setattr(agent, "_summary_backend", stub)
    assert agent._gate_judge("is this worth a wake?", timeout=7) == "hold"
    assert stub.prompts == ["is this worth a wake?"]
