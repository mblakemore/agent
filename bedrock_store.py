"""Bedrock credential store with health-check rotation.

See issue #405 (CICD: bedrock credential store with health-check rotation
+ key management CLI).

The store replaces the single ``BEDROCK_API_URL`` / ``BEDROCK_API_KEY``
env-var pair with a JSON file holding multiple URL/key pairs. Each entry
carries health and daily-spend metadata so ``BedrockBackend`` can pick
the best ``up`` entry at session start and rotate to a sibling when one
gateway saturates.

Design notes (issue #405 acceptance criteria 1, 8, 9):

- Default path: ``~/.config/agent/bedrock_creds.json``. Override via
  ``AGENT_BEDROCK_STORE``.
- Atomic writes: ``write_store`` writes ``<path>.tmp`` then ``os.replace``.
- Concurrent-process safety: ``with_locked_store(...)`` takes an
  ``fcntl.flock(LOCK_EX)`` with a 2-second timeout. If we can't lock, the
  caller logs a warning and skips the mutation rather than blocking the
  request path (criterion 9).
- File mode is forced to ``0o600`` on the first write since keys are
  sensitive (criterion 1).
- Selection: filter by ``status == "up"``, pick lowest ``daily_spend_usd``
  with ``last_checked`` ascending as the tie-break so stale entries get
  re-tested (criterion 3).

This module is import-safe with no side effects, no logging at import
time, and only stdlib + ``requests`` (for the health probe) at runtime.
``BedrockBackend.__init__`` consults this store via
``select_credentials()`` with env-var fallback (criterion 2).
"""

from __future__ import annotations

import errno
try:
    import fcntl  # POSIX advisory file locks
except ImportError:  # Windows / non-POSIX
    fcntl = None
try:
    import msvcrt  # Windows file locking
except ImportError:  # non-Windows
    msvcrt = None
import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


DEFAULT_STORE_PATH = "~/.config/agent/bedrock_creds.json"
ENV_OVERRIDE = "AGENT_BEDROCK_STORE"
LOCK_TIMEOUT_SECONDS = 2.0
HEALTH_CHECK_TIMEOUT_SECONDS = 5.0

# Status values used in the store. ``unknown`` is the transient state for
# a freshly added entry before the first health check completes.
STATUS_UP = "up"
STATUS_DOWN = "down"
STATUS_UNKNOWN = "unknown"


def _now_iso() -> str:
    """UTC timestamp in RFC 3339-ish form (issue #405 example uses ``Z``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def store_path() -> Path:
    """Return the resolved store path, honouring ``AGENT_BEDROCK_STORE``."""
    raw = os.environ.get(ENV_OVERRIDE) or DEFAULT_STORE_PATH
    return Path(os.path.expanduser(raw))


def _empty_store() -> dict[str, Any]:
    return {"entries": []}


def _entry_defaults(name: str, url: str, key: str) -> dict[str, Any]:
    return {
        "name": name,
        "url": url.rstrip("/"),
        "key": key,
        "status": STATUS_UNKNOWN,
        "last_checked": None,
        "last_error": None,
        "daily_spend_usd": 0.0,
    }


def load_store(path: Path | None = None) -> dict[str, Any]:
    """Read the store file. Returns an empty store if missing or empty.

    Malformed JSON is treated as an empty store so a corrupted file
    cannot hard-block the agent — the env-var fallback path then takes
    over (criterion 2).
    """
    p = path or store_path()
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty_store()
    except OSError:
        return _empty_store()
    raw = raw.strip()
    if not raw:
        return _empty_store()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logging.getLogger("agent").warning(
            "bedrock_store.parse_failed path=%s", p
        )
        return _empty_store()
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return _empty_store()
    return data


def write_store(data: dict[str, Any], path: Path | None = None) -> None:
    """Atomically write the store. Forces ``0o600`` on the file.

    Writes to ``<path>.tmp`` then ``os.replace`` so a crash mid-write
    can't corrupt the live file (criterion 8).
    """
    p = path or store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    body = json.dumps(data, indent=2, sort_keys=False)
    # Open with explicit mode so the tmp file is also 0600 — no window
    # where a wider mode is visible to other users.
    fd = os.open(
        str(tmp),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, p)
    # Belt-and-braces: ensure the destination is 0600 too. ``os.replace``
    # preserves the source mode but a pre-existing file may have had a
    # different mode.
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _acquire_lock(lock_fd: int, timeout: float) -> bool:
    """Acquire an exclusive advisory lock on ``lock_fd``.

    POSIX uses ``fcntl.flock``; Windows uses ``msvcrt.locking``. Both
    auto-release when the fd is closed or the owning process dies, so a crash
    never leaves a stale lock (an ``O_EXCL`` lockfile would not have this
    property). Retries on contention until ``timeout`` seconds elapse; returns
    True if the lock was taken, False on timeout. If neither primitive is
    available, proceeds unlocked (single-process correctness only).
    """
    if fcntl is None and msvcrt is None:
        return True
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                os.lseek(lock_fd, 0, os.SEEK_SET)
                msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as e:
            # POSIX flock: EAGAIN/EACCES means another process holds it — retry.
            # Any other errno is a real error. Windows msvcrt raises OSError
            # (EDEADLOCK/EACCES) on contention, which we also treat as retry.
            if fcntl is not None and e.errno not in (errno.EAGAIN, errno.EACCES):
                raise
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def _release_lock(lock_fd: int) -> None:
    """Release a lock taken by :func:`_acquire_lock`. Best-effort."""
    try:
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        elif msvcrt is not None:
            os.lseek(lock_fd, 0, os.SEEK_SET)
            msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextmanager
def with_locked_store(
    path: Path | None = None,
    timeout: float = LOCK_TIMEOUT_SECONDS,
) -> Iterator[tuple[dict[str, Any], Path] | None]:
    """Context manager around the read-modify-write window.

    Yields ``(data, path)`` while holding an exclusive ``fcntl.flock``.
    If the lock can't be acquired within ``timeout`` seconds, yields
    ``None`` so the caller can log + skip the mutation (criterion 9).

    Lock is taken on a sidecar ``.lock`` file so we don't need the data
    file to exist yet (the very first ``add`` creates it).
    """
    p = path or store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_path = p.with_suffix(p.suffix + ".lock")
    lock_fd = os.open(
        str(lock_path),
        os.O_RDWR | os.O_CREAT,
        0o600,
    )
    locked = _acquire_lock(lock_fd, timeout)
    try:
        if not locked:
            yield None
            return
        data = load_store(p)
        yield (data, p)
    finally:
        if locked:
            _release_lock(lock_fd)
        os.close(lock_fd)


def find_entry(data: dict[str, Any], name: str) -> dict[str, Any] | None:
    for ent in data.get("entries", []):
        if ent.get("name") == name:
            return ent
    return None


def add_entry(
    data: dict[str, Any],
    *,
    name: str,
    url: str,
    key: str,
) -> dict[str, Any]:
    """Append a new entry. Raises ``ValueError`` on duplicate name."""
    if not name:
        raise ValueError("entry name is required")
    if find_entry(data, name) is not None:
        raise ValueError(f"entry {name!r} already exists")
    entry = _entry_defaults(name, url, key)
    data.setdefault("entries", []).append(entry)
    return entry


def remove_entry(data: dict[str, Any], name: str) -> bool:
    """Remove the named entry. Returns True if removed, False if missing."""
    entries = data.get("entries", [])
    for i, ent in enumerate(entries):
        if ent.get("name") == name:
            del entries[i]
            return True
    return False


def select_entry(data: dict[str, Any]) -> dict[str, Any] | None:
    """Pick the next entry to use.

    Filter ``status == "up"``; lowest ``daily_spend_usd`` wins;
    tie-break by oldest ``last_checked`` (None counts as oldest so a
    fresh-but-up entry beats a stale one with identical spend).
    """
    candidates = [
        e for e in data.get("entries", [])
        if e.get("status") == STATUS_UP
    ]
    if not candidates:
        return None

    def sort_key(e: dict[str, Any]) -> tuple[float, str]:
        spend = e.get("daily_spend_usd") or 0.0
        try:
            spend = float(spend)
        except (TypeError, ValueError):
            spend = 0.0
        # Empty string sorts before any real ISO timestamp, so None /
        # missing last_checked is treated as oldest (re-test it first).
        last = e.get("last_checked") or ""
        return (spend, last)

    candidates.sort(key=sort_key)
    return candidates[0]


def select_credentials(
    *,
    env_url: str | None = None,
    env_key: str | None = None,
    path: Path | None = None,
) -> tuple[str, str, str | None]:
    """Return ``(api_url, api_key, entry_name_or_None)`` for backend init.

    Resolution order (criterion 2 + 3):

    1. If the store has at least one ``up`` entry, use it.
    2. Else if env vars are set, return them with name ``None`` (the
       implicit ``env`` entry — back-compat path).
    3. Else raise ``LookupError`` so the caller can convert it to
       ``ConfigError`` (matching today's behaviour at
       ``llm_backend.py:526``).
    """
    data = load_store(path)
    entry = select_entry(data)
    if entry is not None:
        return (
            str(entry.get("url", "")),
            str(entry.get("key", "")),
            str(entry.get("name", "")),
        )
    if env_url and env_key:
        return env_url, env_key, None
    raise LookupError(
        "no bedrock credentials available — store has no 'up' entries "
        "and BEDROCK_API_URL/BEDROCK_API_KEY are unset"
    )


def mark_status(
    name: str,
    *,
    status: str,
    last_error: str | None = None,
    path: Path | None = None,
) -> bool:
    """Update an entry's status under flock. Returns True on success.

    Returns False when the lock can't be acquired or the entry name
    isn't in the store (criterion 9 — we'd rather miss a status update
    than block the request path).
    """
    log = logging.getLogger("agent")
    with with_locked_store(path) as locked:
        if locked is None:
            log.warning(
                "bedrock_store.lock_timeout op=mark_status name=%s", name
            )
            return False
        data, p = locked
        entry = find_entry(data, name)
        if entry is None:
            return False
        entry["status"] = status
        entry["last_checked"] = _now_iso()
        entry["last_error"] = last_error
        write_store(data, p)
        return True


def health_check(
    url: str,
    key: str,
    *,
    timeout: float = HEALTH_CHECK_TIMEOUT_SECONDS,
    model: str = "claude-v4.6-sonnet",
) -> tuple[bool, str | None]:
    """Probe a bedrock endpoint.

    Returns ``(ok, error_summary)``. Treats any 2xx as ``up`` and any
    other status / exception as ``down`` with a one-line summary
    (criterion 7). Tries ``GET {url}/health`` first; falls back to a
    1-token chat completion if ``/health`` returns 4xx (some gateways
    don't expose a public health probe but accept auth'd POSTs).
    """
    import requests  # local import — keeps module import side-effect-free

    headers = {
        "x-api-key": key,
        "Content-Type": "application/json",
        "Origin": "http://localhost:8000",
    }
    base = url.rstrip("/")
    try:
        resp = requests.get(
            f"{base}/health",
            headers=headers,
            timeout=timeout,
        )
        if 200 <= resp.status_code < 300:
            return True, None
        # 4xx on /health may just mean the endpoint isn't exposed; fall
        # through to a trivial chat probe before declaring down.
        if 400 <= resp.status_code < 500:
            try:
                probe = requests.post(
                    f"{base}/conversation",
                    headers=headers,
                    json={
                        "message": {
                            "content": [
                                {"contentType": "text", "body": "ping"}
                            ],
                            "model": model,
                        },
                    },
                    timeout=timeout,
                )
                if 200 <= probe.status_code < 300:
                    return True, None
                return False, f"HTTP {probe.status_code}"
            except requests.RequestException as e:
                return False, f"{type(e).__name__}: {str(e)[:80]}"
        return False, f"HTTP {resp.status_code}"
    except requests.RequestException as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"
    except Exception as e:  # pragma: no cover - defensive
        return False, f"{type(e).__name__}: {str(e)[:80]}"


def summarize_error(exc: BaseException) -> str:
    """One-line summary used for ``last_error`` on rotation."""
    msg = str(exc) or repr(exc)
    return f"{type(exc).__name__}: {msg[:160]}"


def is_stale(entry: dict[str, Any], stale_days: int, now: datetime | None = None) -> bool:
    """True if ``entry`` is down AND last_checked older than N days."""
    if entry.get("status") != STATUS_DOWN:
        return False
    last = entry.get("last_checked")
    if not last:
        # No timestamp — treat as stale so we don't keep dead-forever rows.
        return True
    try:
        ts = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    cur = now or datetime.now(timezone.utc)
    age_days = (cur - ts).total_seconds() / 86400.0
    return age_days >= stale_days
