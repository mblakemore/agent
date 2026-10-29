import time
import tools.exec_command as ec


def test_quick_command_unchanged():
    assert "exit=0" in ec.fn("echo hi") and "hi" in ec.fn("echo hi")


def test_slow_command_detaches_and_polls(monkeypatch):
    monkeypatch.setattr(ec, "_DETACH_AFTER_S", 0.3)
    out = ec.fn("echo start; sleep 1; echo end", timeout=30)
    assert "continuing in background" in out and "start" in out
    sid = out.split("[session: ")[1].split("]")[0]
    for _ in range(40):
        r = ec.fn(session_id=sid)
        if "finished" in r:
            break
        time.sleep(0.1)
    assert "exit=0" in r and "end" in r


def test_background_false_blocks(monkeypatch):
    monkeypatch.setattr(ec, "_DETACH_AFTER_S", 0.2)
    out = ec.fn("sleep 0.6; echo done", timeout=30, background=False)
    assert "exit=0" in out and "done" in out
