import agent
import summarize


def test_model_summaries_capped_per_pass(monkeypatch):
    calls = []
    monkeypatch.setattr(summarize, "summary_request", lambda p, log=None: calls.append(p) or "s")
    hist = []
    for i in range(8):
        hist.append({"role": "tool", "name": "exec_command", "content": f"line {i}\n" * 100})
    import logging
    summarize.compress_repeated_tool_results(hist, "exec_command", logging.getLogger("t"))
    assert len(calls) == summarize.TOOL_RESULT_COMPRESS_MODEL_MAX
    assert all(m["content"].startswith("[compressed") for m in hist[:-1])
    assert not hist[-1]["content"].startswith("[compressed")
