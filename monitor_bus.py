"""Background monitor bus — generic watchers that inject into the agent loop.

A *monitor* runs a shell command on a fixed interval inside a daemon thread.
When the command emits non-empty stdout, that output is queued on a shared
bus. The agentic loop drains the bus at each turn-complete boundary and feeds
queued items into the conversation as new user turns — so a monitor can pop
new work in "as the agent continues".

Design guarantees:
  * Generic: a monitor watches ANY shell command. No coupling to any data
    source — the caller supplies the command.
  * Daemon threads: monitors die with the process. An -a cycle that ends still
    exits promptly; a monitor never extends process lifetime, and stop_all()
    never block-joins (joining a monitor mid-subprocess would stall exit).
  * Bounded: per-poll subprocess timeout <= interval; output length capped.

Armed via the `monitor` tool (tools/monitor.py); drained by agent.py.
"""

import logging
import os
import queue as _queue
import signal
import subprocess
import threading

_log = logging.getLogger("agent")

_MAX_OUTPUT_CHARS = 4000    # cap one injection to protect the context window
_DEFAULT_INTERVAL = 15.0
_MIN_INTERVAL = 2.0
_MAX_MONITORS = 8           # sanity ceiling on concurrent monitors


def _kill_group(proc):
    """SIGKILL the process GROUP of proc, then reap. Never raises.

    Killing only the direct child leaks grandchildren: `bash -c 'tail -f x |
    grep y'` SIGKILLed at the bash level leaves tail running forever (verified
    live 2026-08-07 — two survivors from one timed-out poll). Because each
    poll starts its own session (start_new_session=True below), the group id
    is the child pid and killpg takes the whole pipeline down."""
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=2)
    except Exception:
        pass


def _run_once(command, timeout, register=None):
    """Run command via `bash -c` in its OWN PROCESS GROUP. Return stripped
    stdout on success (may be ''), or None on non-zero exit / timeout / error.
    Never raises.

    `register` (optional callable) receives the live Popen (and then None when
    the poll ends) so the owning monitor can group-kill an in-flight child at
    stop()/exit — otherwise 'exit' orphans whatever the poll was running and a
    persistent-style command survives the agent (the 15-day stale process
    class, 2026-08-07)."""
    p = None
    try:
        p = subprocess.Popen(["bash", "-c", command],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)
        if register:
            register(p)
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _log.debug("monitor cmd timed out after %ss — killing group", timeout)
        _kill_group(p)
        return None
    except Exception as e:
        _log.debug("monitor cmd errored: %s", e)
        _kill_group(p)
        return None
    finally:
        if register:
            register(None)
    if p.returncode != 0:
        _log.debug("monitor cmd exit=%s: %s", p.returncode,
                   ((err or "")[-200:]).strip())
        return None
    return (out or "").strip()


class _Monitor:
    def __init__(self, label, command, interval, cmd_timeout, prefix, dedup):
        self.label = label
        self.command = command
        self.interval = interval
        self.cmd_timeout = cmd_timeout
        self.prefix = prefix
        self.dedup = dedup
        self._last = None
        self._stop = threading.Event()
        self._proc = None                     # live Popen while a poll runs
        self._proc_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run, name=f"monitor:{label}", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        """Signal the loop AND group-kill any in-flight child. Without the
        kill, 'exit' (or a monitor replace) orphans the running poll — a
        daemon thread dies with the process but its subprocess does not."""
        self._stop.set()
        with self._proc_lock:
            proc, self._proc = self._proc, None
        _kill_group(proc)

    def _register(self, proc):
        with self._proc_lock:
            self._proc = proc

    def _run(self):
        while not self._stop.is_set():
            out = _run_once(self.command, self.cmd_timeout, register=self._register)
            if self._stop.is_set():
                break  # stopped during the poll — don't inject a straggler
            if out and not (self.dedup and out == self._last):
                self._last = out
                if len(out) > _MAX_OUTPUT_CHARS:
                    out = out[:_MAX_OUTPUT_CHARS] + "\n…[truncated]"
                _BUS._put(self.prefix + out)
            # Interruptible sleep — returns immediately when stop() is called.
            self._stop.wait(self.interval)


class _Bus:
    def __init__(self):
        self._q = _queue.Queue()
        self._monitors = {}
        self._lock = threading.Lock()

    def arm(self, label, command, interval=_DEFAULT_INTERVAL, cmd_timeout=None,
            prefix="", dedup=True):
        label = str(label or "").strip()
        command = str(command or "").strip()
        if not label:
            return {"ok": False, "error": "label required"}
        if not command:
            return {"ok": False, "error": "command required"}
        try:
            interval = float(interval)
        except Exception:
            interval = _DEFAULT_INTERVAL
        interval = max(_MIN_INTERVAL, interval)
        try:
            cmd_timeout = float(cmd_timeout) if cmd_timeout is not None else min(10.0, interval)
        except Exception:
            cmd_timeout = min(10.0, interval)
        # C4107 discipline: a hung poll can never wedge the loop past one interval.
        cmd_timeout = min(cmd_timeout, interval)
        with self._lock:
            existing = self._monitors.get(label)
            if existing is None and len(self._monitors) >= _MAX_MONITORS:
                return {"ok": False, "error": f"too many monitors (max {_MAX_MONITORS})"}
            if existing is not None:
                existing.stop()   # replace: stop the old one, start a fresh one
            m = _Monitor(label, command, interval, cmd_timeout, prefix, bool(dedup))
            self._monitors[label] = m
            m.start()
        return {"ok": True, "label": label, "interval": interval,
                "replaced": existing is not None}

    def disarm(self, label):
        label = str(label or "").strip()
        with self._lock:
            m = self._monitors.pop(label, None)
        if m is None:
            return {"ok": False, "error": f"no monitor '{label}'"}
        m.stop()
        return {"ok": True, "label": label}

    def list_active(self):
        with self._lock:
            return [{"label": m.label, "command": m.command, "interval": m.interval}
                    for m in self._monitors.values()]

    def _put(self, text, kind="monitor"):
        # Items are (kind, text) tuples so consumers can distinguish a typed
        # user line (needs slash/exit/file-ref handling) from a monitor ping
        # (injected raw). drain() flattens to text for the mid-burst path,
        # which treats both identically; get_blocking() preserves the tuple.
        self._q.put((kind, text))
        _fire_notifier()   # wake an idle interactive prompt, if one registered

    def put_user(self, text):
        """Enqueue a typed user line (provenance 'user')."""
        self._put(text, kind="user")

    def get_blocking(self, timeout=None):
        """Block for the next (kind, text) item; return None on timeout.
        Used by the concurrent-input main loop as its idle wait."""
        try:
            return self._q.get(block=True, timeout=timeout)
        except _queue.Empty:
            return None

    def has_pending(self):
        # Best-effort: is anything queued right now? Used by the interactive
        # loop to drain before blocking on input (closes the arrived-just-
        # before-prompt race).
        return not self._q.empty()

    def drain(self):
        """Return queued injection TEXTS (FIFO), emptying the queue. Flattens
        (kind, text) tuples to text — the mid-burst path treats a typed user
        line and a monitor ping identically. Non-blocking; safe every turn."""
        out = []
        while True:
            try:
                item = self._q.get_nowait()
            except _queue.Empty:
                break
            out.append(item[1] if isinstance(item, tuple) else item)
        return out

    def stop_all(self):
        """Signal every monitor to stop. Does NOT join — daemon threads die
        with the process; joining one mid-subprocess would stall exit."""
        with self._lock:
            monitors = list(self._monitors.values())
            self._monitors.clear()
        for m in monitors:
            m.stop()


_BUS = _Bus()

# Optional notifier: a zero-arg callback fired (best-effort, from the monitor
# thread) whenever new output is queued. The interactive front-end registers
# one so an idle prompt can wake immediately instead of waiting for the next
# turn. Default None -> no-op (so -a / non-interactive paths are unaffected).
_notifier = None
_notifier_lock = threading.Lock()


def set_notifier(fn):
    """Register a zero-arg callback fired when output is queued (or None)."""
    global _notifier
    with _notifier_lock:
        _notifier = fn


def clear_notifier():
    set_notifier(None)


def _fire_notifier():
    with _notifier_lock:
        fn = _notifier
    if fn is not None:
        try:
            fn()
        except Exception:
            pass


def has_pending():
    return _BUS.has_pending()


def put_user(text):
    """Enqueue a typed user line (concurrent-input producer)."""
    return _BUS.put_user(text)


def get_blocking(timeout=None):
    """Block for the next (kind, text) item; None on timeout."""
    return _BUS.get_blocking(timeout=timeout)


# Module-level convenience API (agent.py + tools/monitor.py call these).
def arm(label, command, **kw):
    return _BUS.arm(label, command, **kw)


def disarm(label):
    return _BUS.disarm(label)


def list_active():
    return _BUS.list_active()


def drain():
    return _BUS.drain()


def stop_all():
    return _BUS.stop_all()
