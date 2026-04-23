"""Security review tests (plan § 18.75 checklist, task 2.9).

- `api_key` redaction: dumping ``_config`` via ``log.debug`` must not
  emit the literal key value.
- Config file world-readable warning: at startup, if ``config.json``
  contains a non-empty ``api_key`` and the file is world-readable, agent
  must log a WARN ``"config.json is world-readable; chmod 600 config.json"``.
"""

import json
import logging
import os

import agent


def test_redact_api_keys_masks_nested_key():
    cfg = {
        "backends": {
            "main": {"kind": "bedrock", "api_key": "SENTINEL_VALUE_123"},
            "summary": {"kind": "llamacpp"},
        }
    }
    redacted = agent._redact_api_keys(cfg)
    # Dump to str (as log.debug('%s', ...) would).
    dumped = json.dumps(redacted)
    assert "SENTINEL_VALUE_123" not in dumped
    assert "***REDACTED***" in dumped
    assert redacted["backends"]["summary"]["kind"] == "llamacpp"


def test_redact_api_keys_passes_through_non_dicts():
    assert agent._redact_api_keys(None) is None
    assert agent._redact_api_keys("string") == "string"


def test_redact_empty_api_key_unchanged():
    """Empty ``api_key`` values shouldn't emit ``***REDACTED***`` — nothing
    to hide.
    """
    cfg = {"backends": {"main": {"api_key": ""}}}
    redacted = agent._redact_api_keys(cfg)
    assert redacted["backends"]["main"]["api_key"] == ""


def test_log_debug_config_does_not_leak_sentinel(caplog):
    """If an operator logs ``_config`` at DEBUG, the sentinel api_key must
    never appear in the caplog output (per § 18.75 item 3).
    """
    cfg = {"backends": {"main": {"api_key": "SENTINEL_VALUE_123"}}}
    log = logging.getLogger("test_redaction")
    with caplog.at_level(logging.DEBUG, logger="test_redaction"):
        log.debug("config: %s", agent._redact_api_keys(cfg))
    for r in caplog.records:
        assert "SENTINEL_VALUE_123" not in r.getMessage()


def test_world_readable_config_warns(tmp_path, caplog, monkeypatch):
    """Config file with wider-than-0o600 perms AND a non-empty api_key →
    WARN log line."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps({"backends": {"main": {"kind": "bedrock", "api_key": "abc123"}}})
    )
    # Set world-readable perms (0o644).
    os.chmod(str(cfg_file), 0o644)

    user_config = json.loads(cfg_file.read_text())

    with caplog.at_level(logging.WARNING, logger="agent"):
        agent._warn_if_world_readable_with_key(cfg_file, user_config)

    assert any(
        "world-readable" in r.message and "chmod 600" in r.message
        for r in caplog.records
    )


def test_mode_0600_no_warning(tmp_path, caplog):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps({"backends": {"main": {"kind": "bedrock", "api_key": "abc123"}}})
    )
    os.chmod(str(cfg_file), 0o600)

    user_config = json.loads(cfg_file.read_text())
    with caplog.at_level(logging.WARNING, logger="agent"):
        agent._warn_if_world_readable_with_key(cfg_file, user_config)

    assert not any("world-readable" in r.message for r in caplog.records)


def test_no_warning_when_no_api_key(tmp_path, caplog):
    """Legacy llamacpp-only config shouldn't trigger the warning even if
    world-readable — there's nothing to protect.
    """
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"backends": {"main": {"kind": "llamacpp"}}}))
    os.chmod(str(cfg_file), 0o644)

    user_config = json.loads(cfg_file.read_text())
    with caplog.at_level(logging.WARNING, logger="agent"):
        agent._warn_if_world_readable_with_key(cfg_file, user_config)

    assert not any("world-readable" in r.message for r in caplog.records)
