"""Sources on the bus, finished-job items, and the `wait` tool.

The load-bearing guarantees: nothing changes for a loop that tracks nothing (drain stays
a no-op); a finished background job puts one item on the bus by itself; `wait` blocks
inside the tool call, returns everything ready, names what is still running, and is
interrupted by typed input without losing it.
"""

import threading
import time

import pytest

import agent as _agent
import monitor_bus
import tools.exec_command as ec
from tools.wait import fn as wait_fn
from tools.subagent import fn as subagent_fn


@pytest.fixture(autouse=True)
def _clean_bus():
    def _reset():
        monitor_bus.stop_all()
        ec.cleanup_temp_sessions()
        for _ in range(12):      # a killed session's exit item lands a moment later
            time.sleep(0.05)
            monitor_bus.drain()
    _reset()
    monitor_bus.configure(session_events=True, gate={}, judge=None)
    yield
    _reset()


def _sid(out):
    return out.split("[session: ")[1].split("]")[0]


# ── blast radius ──────────────────────────────────────────────────────────
def test_drain_is_still_a_noop_with_nothing_tracked():
    history = [{"role": "user", "content": "hi"}]
    assert _agent._drain_monitor_injections(history) is False
    assert history == [{"role": "user", "content": "hi"}]
    assert monitor_bus.list_sources() == []


# ── task 1: finished jobs are bus items ───────────────────────────────────
def test_a_detached_session_is_tracked_and_its_exit_lands_on_the_bus(monkeypatch):
    monkeypatch.setattr(ec, "_DETACH_AFTER_S", 0.2)
    out = ec.fn("echo start; sleep 0.6; echo end", timeout=30, new_session=True)
    sid = _sid(out)
    assert "wait()" in out
    src = monitor_bus.source(sid)
    assert src is not None and src.kind == "session" and not src.done
    deadline = time.time() + 5
    items = []
    while time.time() < deadline and not items:
        items = monitor_bus.drain()
        time.sleep(0.05)
    assert len(items) == 1
    assert items[0].startswith(f"[session: {sid}] exit=0") and "end" in items[0]
    assert monitor_bus.source(sid).done


def test_a_background_true_session_reports_its_exit_too():
    out = ec.fn("echo bg-done", timeout=30, background=True, new_session=True)
    sid = _sid(out)
    deadline = time.time() + 5
    items = []
    while time.time() < deadline and not items:
        items = monitor_bus.drain()
        time.sleep(0.05)
    assert items and f"[session: {sid}] exit=0" in items[0] and "bg-done" in items[0]


def test_session_events_off_keeps_the_bus_quiet():
    monitor_bus.configure(session_events=False)
    ec.fn("echo quiet", timeout=30, background=True, new_session=True)
    time.sleep(0.8)
    assert monitor_bus.drain() == []


def test_the_finished_item_carries_a_tail_not_the_whole_output(monkeypatch):
    monkeypatch.setattr(ec, "_FINISHED_TAIL", 40)
    ec.fn("python3 -c \"print('x' * 500); print('LAST')\"", timeout=30, background=True,
          new_session=True)
    deadline = time.time() + 5
    items = []
    while time.time() < deadline and not items:
        items = monitor_bus.drain()
        time.sleep(0.05)
    assert items and "LAST" in items[0] and items[0].count("x") <= 40


# ── task 3: wait ──────────────────────────────────────────────────────────
def test_wait_returns_the_first_finished_job_and_names_the_rest():
    a = _sid(ec.fn("sleep 0.3; echo A", timeout=30, background=True, new_session=True))
    b = _sid(ec.fn("sleep 3; echo B", timeout=30, background=True, new_session=True))
    t0 = time.time()
    out = wait_fn(max_s=10)
    assert time.time() - t0 < 2.5, out
    assert "[wait] 1 item" in out and f"[session: {a}] exit=0" in out and "A" in out
    assert "still running" in out and b in out
    assert monitor_bus.source(a) is None          # forgotten once returned
    assert monitor_bus.source(b) is not None


def test_wait_all_of_is_a_barrier():
    a = _sid(ec.fn("sleep 0.2; echo A", timeout=30, background=True, new_session=True))
    b = _sid(ec.fn("sleep 0.6; echo B", timeout=30, background=True, new_session=True))
    out = wait_fn(all_of=[a, b], max_s=10)
    assert "2 items" in out and "A" in out and "B" in out
    assert "nothing else is tracked" in out


def test_wait_any_of_ignores_other_sources():
    a = _sid(ec.fn("sleep 0.2; echo A", timeout=30, background=True, new_session=True))
    b = _sid(ec.fn("sleep 0.8; echo B", timeout=30, background=True, new_session=True))
    out = wait_fn(any_of=[b], max_s=10)
    # A's item arrived first and is carried along, but the wait ended on B.
    assert "B" in out and "A" in out and b in out
    assert monitor_bus.source(a) is None and monitor_bus.source(b) is None


def test_wait_times_out_with_the_running_list():
    b = _sid(ec.fn("sleep 2; echo B", timeout=30, background=True, new_session=True))
    out = wait_fn(max_s=0.4)
    assert out.startswith("[wait] nothing happened in 0s") and b in out


def test_typed_input_interrupts_wait_and_is_not_lost():
    ec.fn("sleep 3; echo late", timeout=30, background=True, new_session=True)
    threading.Timer(0.3, lambda: monitor_bus.put_user("hello?")).start()
    out = wait_fn(max_s=10)
    assert "[wait] interrupted after 0s by user input" in out
    assert monitor_bus.drain_items() == [("user", "hello?")]


def test_wait_is_cancelled_by_esc_esc():
    import cancel
    cancel.reset()
    threading.Timer(0.2, cancel.request_cancel).start()
    try:
        with pytest.raises(cancel.CancelledError):
            wait_fn(max_s=10)
    finally:
        cancel.reset()


def test_wait_rejects_unknown_sources_and_bad_bounds():
    assert wait_fn(any_of=["nope"], max_s=1).startswith("Error: unknown source")
    assert wait_fn(max_s="soon").startswith("Error")
    assert wait_fn(max_s=-1).startswith("Error")


def test_a_monitor_ping_ends_a_wait():
    monitor_bus.arm("tick", "echo tock", interval=2)
    out = wait_fn(max_s=5)
    assert "tock" in out and "watching: tick" in out


# ── background subagents ──────────────────────────────────────────────────
def test_a_background_subagent_is_a_source_whose_answer_arrives_on_the_bus(monkeypatch):
    import tools.subagent as sa
    monkeypatch.setattr(sa, "_run_subagent", lambda prompt, timeout: f"answer to {prompt}")
    out = subagent_fn("add 2 and 2", background=True)
    assert out.startswith("[subagent: sub-") and "wait()" in out
    sid = out.split("[subagent: ")[1].split("]")[0]
    assert monitor_bus.source(sid).kind == "subagent"
    got = wait_fn(any_of=[sid], max_s=5)
    assert f"[subagent: {sid}] finished" in got and "answer to add 2 and 2" in got


def test_a_foreground_subagent_still_blocks(monkeypatch):
    import tools.subagent as sa
    monkeypatch.setattr(sa, "_run_subagent", lambda prompt, timeout: "done")
    assert subagent_fn("x") == "done"
    assert monitor_bus.list_sources() == []


# ── file watches ──────────────────────────────────────────────────────────
def test_a_watched_file_reports_only_what_is_appended(tmp_path):
    log = tmp_path / "job.log"
    log.write_text("old line\n")
    r = monitor_bus.arm("joblog", "", path=str(log), interval=2)
    assert r["ok"]
    time.sleep(0.3)
    assert monitor_bus.drain() == []          # the existing content is not replayed
    with open(log, "a") as fh:
        fh.write("new line\n")
    out = wait_fn(max_s=6)
    assert "new line" in out and "old line" not in out
