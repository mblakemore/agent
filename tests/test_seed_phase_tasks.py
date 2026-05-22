"""Regression tests for agent._seed_phase_tasks (AC5/AC6/AC7 of issue #1028).

AC5 — default seeding produces ephemeral tasks (no `persistent` field stored).
AC6 — preferences.seed_tasks_persistent=True ⇒ seeded tasks carry persistent=True.
AC7 — with the persistent flag on, re-seeding is idempotent: a phase description
       already present as an open task is skipped, not duplicated.
"""
import json
import logging
from pathlib import Path

import pytest

import agent
import tools.task_tracker as _tt_mod
from tools.task_tracker import _TASKS_FILE, fn as _tt_fn


PHASE_DESCS = ["PERCEIVE", "REFLECT", "DECIDE", "ACT", "CONSOLIDATE", "PERSIST"]


def setup_function(function):
    p = Path(_TASKS_FILE)
    if p.exists():
        p.unlink()


def teardown_function(function):
    p = Path(_TASKS_FILE)
    if p.exists():
        p.unlink()


def _load_tasks():
    p = Path(_TASKS_FILE)
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def test_seed_default_is_ephemeral():
    """AC5: without seed_tasks_persistent, seeded tasks have no persistent key."""
    config = {"preferences": {"initial_tasks": PHASE_DESCS}}
    log = logging.getLogger("test_seed_default_is_ephemeral")

    agent._seed_phase_tasks(config, log)

    tasks = _load_tasks()
    assert len(tasks) == len(PHASE_DESCS), f"expected {len(PHASE_DESCS)} tasks, got {len(tasks)}"
    for t in tasks:
        # AC2 stores key only when persistent=True. Default ⇒ key absent.
        assert "persistent" not in t, (
            f"task {t!r} unexpectedly has persistent key — default seeding "
            "should be ephemeral"
        )


def test_seed_persistent_config_marks_tasks_persistent():
    """AC6: preferences.seed_tasks_persistent=True ⇒ tasks have persistent=True."""
    config = {
        "preferences": {
            "initial_tasks": PHASE_DESCS,
            "seed_tasks_persistent": True,
        }
    }
    log = logging.getLogger("test_seed_persistent_config_marks_tasks_persistent")

    agent._seed_phase_tasks(config, log)

    tasks = _load_tasks()
    assert len(tasks) == len(PHASE_DESCS)
    for t in tasks:
        assert t.get("persistent") is True, (
            f"task {t!r} should be persistent when seed_tasks_persistent=True"
        )


def test_seed_persistent_is_idempotent():
    """AC7: with seed_tasks_persistent=True, re-seeding skips existing descriptions."""
    config = {
        "preferences": {
            "initial_tasks": PHASE_DESCS,
            "seed_tasks_persistent": True,
        }
    }
    log = logging.getLogger("test_seed_persistent_is_idempotent")

    agent._seed_phase_tasks(config, log)
    first_pass = _load_tasks()
    assert len(first_pass) == len(PHASE_DESCS)

    # Second seed pass while the same phase tasks are still open. No new task
    # should be created for any description that already exists open.
    agent._seed_phase_tasks(config, log)
    second_pass = _load_tasks()

    assert len(second_pass) == len(first_pass), (
        f"re-seeding duplicated tasks: {len(first_pass)} → {len(second_pass)}"
    )
    descs = [t["description"] for t in second_pass]
    assert descs == [t["description"] for t in first_pass]


def test_seed_persistent_idempotent_adds_only_missing():
    """AC7 variant: with one phase open, re-seed should add the others, not dup it."""
    config = {
        "preferences": {
            "initial_tasks": PHASE_DESCS,
            "seed_tasks_persistent": True,
        }
    }
    log = logging.getLogger("test_seed_persistent_idempotent_adds_only_missing")

    # Pre-create just one phase task to simulate a resumed session.
    _tt_fn("add", description=PHASE_DESCS[0], persistent=True)
    assert len(_load_tasks()) == 1

    agent._seed_phase_tasks(config, log)
    tasks = _load_tasks()

    # All 6 should now be present, the first not duplicated.
    descs = [t["description"] for t in tasks]
    assert descs.count(PHASE_DESCS[0]) == 1, f"duplicated first phase: {descs}"
    for d in PHASE_DESCS:
        assert d in descs, f"missing phase {d!r} after seed"
    assert len(tasks) == len(PHASE_DESCS)
