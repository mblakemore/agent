"""Tests for the telemetry skeleton (issue #399).

These tests validate the public API contract of ``telemetry.py``:

* lazy import — ``opentelemetry`` MUST NOT be loaded when the env flag is unset
* graceful missing-deps handling — ``init()`` returns False, no exception
* enabled path — meters are created on the meter object
* verbose-only meters appear only when verbose is enabled
* recording helpers are no-ops when telemetry is disabled
"""

from __future__ import annotations

import builtins
import importlib
import logging
import os
import sys
import unittest
from unittest import mock


# Ensure the worktree root (containing ``telemetry.py``) is on sys.path. When
# pytest is invoked from the repo root this is already true; this guard makes
# the file robust to direct ``python -m unittest`` invocations as well.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


_OT_PREFIXES = ("opentelemetry",)


def _purge_opentelemetry_modules() -> None:
    """Remove any cached ``opentelemetry.*`` entries from ``sys.modules``."""
    for name in list(sys.modules):
        if any(name == p or name.startswith(p + ".") for p in _OT_PREFIXES):
            del sys.modules[name]


def _purge_telemetry_module() -> None:
    sys.modules.pop("telemetry", None)


def _clean_env() -> dict:
    """Return an environ patch dict that clears all AGENTPY_TELEMETRY* vars."""
    out = {}
    for key in list(os.environ):
        if key.startswith("AGENTPY_TELEMETRY"):
            out[key] = ""  # mock.patch.dict with clear-style; we use clear=False below
    return out


class _BaseTelemetryTest(unittest.TestCase):
    def setUp(self) -> None:
        # Snapshot env, scrub all AGENTPY_TELEMETRY* keys, and force a fresh
        # ``import telemetry`` so module-level state is clean per test.
        self._env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        for key in [k for k in os.environ if k.startswith("AGENTPY_TELEMETRY")]:
            del os.environ[key]
        # Also scrub the OTel temporality preference so init() can set it
        # deterministically.
        os.environ.pop("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", None)
        _purge_telemetry_module()
        _purge_opentelemetry_modules()

    def tearDown(self) -> None:
        # Best-effort shutdown of any provider this test instantiated.
        tele = sys.modules.get("telemetry")
        if tele is not None:
            try:
                tele.shutdown()
            except Exception:
                pass
        _purge_telemetry_module()
        _purge_opentelemetry_modules()
        self._env_patch.stop()


class TestLazyImport(_BaseTelemetryTest):
    def test_import_does_not_pull_opentelemetry(self) -> None:
        """Plain ``import telemetry`` MUST NOT pull in opentelemetry."""
        import telemetry  # noqa: F401

        for name in sys.modules:
            self.assertFalse(
                name == "opentelemetry" or name.startswith("opentelemetry."),
                f"opentelemetry leaked into sys.modules via plain import: {name}",
            )

    def test_init_off_returns_false_and_keeps_lazy(self) -> None:
        """With AGENTPY_TELEMETRY unset, ``init()`` returns False and stays lazy."""
        import telemetry

        self.assertFalse(telemetry.init())
        self.assertFalse(telemetry.verbose_enabled())
        for name in sys.modules:
            self.assertFalse(
                name == "opentelemetry" or name.startswith("opentelemetry."),
                f"opentelemetry imported when telemetry was disabled: {name}",
            )


class TestMissingDeps(_BaseTelemetryTest):
    def test_missing_deps_returns_false_and_logs_hint(self) -> None:
        """AGENTPY_TELEMETRY=1 with deps missing → returns False, no raise."""
        os.environ["AGENTPY_TELEMETRY"] = "1"

        # Simulate "opentelemetry not installed" by making any import of an
        # opentelemetry submodule raise ImportError. We do this by patching
        # ``builtins.__import__`` so that the lazy import inside ``init()``
        # fails the same way it would on a machine without the package.
        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "opentelemetry" or name.startswith("opentelemetry."):
                raise ImportError(f"No module named {name!r} (simulated)")
            return real_import(name, globals, locals, fromlist, level)

        import telemetry

        with mock.patch.object(builtins, "__import__", side_effect=fake_import):
            with self.assertLogs("telemetry", level="ERROR") as cm:
                result = telemetry.init()

        self.assertFalse(result)
        joined = "\n".join(cm.output)
        self.assertIn("opentelemetry-sdk", joined)
        self.assertIn("opentelemetry-exporter-otlp-proto-http", joined)
        self.assertFalse(telemetry.verbose_enabled())


@unittest.skipUnless(
    importlib.util.find_spec("opentelemetry") is not None,
    "opentelemetry not installed in this interpreter",
)
class TestEnabledBase(_BaseTelemetryTest):
    def test_init_enabled_returns_true_no_verbose(self) -> None:
        """AGENTPY_TELEMETRY=1 → init() returns True, verbose is False."""
        os.environ["AGENTPY_TELEMETRY"] = "1"

        import telemetry

        self.assertTrue(telemetry.init())
        self.assertFalse(telemetry.verbose_enabled())

        # Cumulative-temporality env must be set BEFORE the lazy import.
        self.assertEqual(
            os.environ.get("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"),
            "cumulative",
        )

        # Base meters must exist; verbose meters must NOT.
        self.assertIsNotNone(telemetry._cycles)
        self.assertIsNotNone(telemetry._tokens)
        self.assertIsNotNone(telemetry._errors)
        self.assertIsNotNone(telemetry._cycle_duration)
        self.assertIsNone(telemetry._turns)
        self.assertIsNone(telemetry._turn_duration)


@unittest.skipUnless(
    importlib.util.find_spec("opentelemetry") is not None,
    "opentelemetry not installed in this interpreter",
)
class TestEnabledVerbose(_BaseTelemetryTest):
    def test_verbose_creates_per_turn_meters(self) -> None:
        """AGENTPY_TELEMETRY=1 + AGENTPY_TELEMETRY_VERBOSE=1 → verbose meters exist."""
        os.environ["AGENTPY_TELEMETRY"] = "1"
        os.environ["AGENTPY_TELEMETRY_VERBOSE"] = "1"

        import telemetry

        self.assertTrue(telemetry.init())
        self.assertTrue(telemetry.verbose_enabled())

        self.assertIsNotNone(telemetry._turns)
        self.assertIsNotNone(telemetry._turn_duration)
        self.assertIsNotNone(telemetry._turn_tool_calls)
        self.assertIsNotNone(telemetry._turn_tokens)

        # Sanity: recording a turn does not raise.
        telemetry.record_turn(
            role="assistant",
            duration_s=1.5,
            tool_calls=2,
            in_tokens=100,
            out_tokens=50,
            model="sonnet",
        )


class TestDisabledNoOps(_BaseTelemetryTest):
    def test_record_helpers_are_no_ops_when_disabled(self) -> None:
        """With base disabled, record_* must not raise and must not import OTel."""
        import telemetry

        self.assertFalse(telemetry.init())

        # All record_* calls must be no-ops — no exception, no module import.
        telemetry.record_cycle("completed", 1.23)
        telemetry.record_cycle("failed", 0.5)
        telemetry.record_tokens("sonnet", "input", 1234)
        telemetry.record_tokens("sonnet", "output", 567)
        telemetry.record_error("ValueError")
        telemetry.record_turn(
            role="assistant",
            duration_s=1.0,
            tool_calls=0,
            in_tokens=0,
            out_tokens=0,
            model="sonnet",
        )
        telemetry.shutdown()  # also a no-op when no provider exists

        for name in sys.modules:
            self.assertFalse(
                name == "opentelemetry" or name.startswith("opentelemetry."),
                f"opentelemetry imported via record_* path: {name}",
            )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    unittest.main(verbosity=2)
