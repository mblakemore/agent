"""Tests for BedrockChatAPI: network mocking and coverage.
"""

import json
import pytest
from unittest.mock import MagicMock, patch

from bedrock_api import BedrockChatAPI, _validate_conversation_ids

# ── Fixtures ──

@pytest.fixture
def api_config():
    return {
        "api_url": "https://api.example.com",
        "api_key": "test-key",
        "model": "claude-v4.5-sonnet",
    }

@pytest.fixture
def api(api_config):
    return BedrockChatAPI(api_config)

# ── Health & Usage ──

def test_health_success(api):
    resp = MagicMock()
    resp.status_code = 200
    with patch.object(api.session, "get", return_value=resp):
        assert api.health() is True

def test_health_failure(api):
    resp = MagicMock()
    resp.status_code = 500
    with patch.object(api.session, "get", return_value=resp):
        assert api.health() is False

def test_health_exception(api):
    with patch.object(api.session, "get", side_effect=Exception("Connection error")):
        assert api.health() is False

def test_get_token_usage_success(api):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"total_tokens": 100, "token_limit": 1000}
    with patch.object(api.session, "get", return_value=resp):
        out = api.get_token_usage()
    assert out == {"total_tokens": 100, "token_limit": 1000}

def test_get_token_usage_non_200(api):
    resp = MagicMock()
    resp.status_code = 404
    with patch.object(api.session, "get", return_value=resp):
        assert api.get_token_usage() is None

def test_get_token_usage_exception(api):
    with patch.object(api.session, "get", side_effect=Exception("Boom")):
        assert api.get_token_usage() is None

# ── Send ──

def test_send_success(api):
    resp = MagicMock()
    resp.status_code = 201
    resp.json.return_value = {"conversationId": "conv-123", "messageId": "msg-456"}
    with patch.object(api.session, "post", return_value=resp):
        conv_id, msg_id = api.send("Hello")
    assert conv_id == "conv-123"
    assert msg_id == "msg-456"

def test_send_error(api):
    resp = MagicMock()
    resp.status_code = 500
    resp.raise_for_status.side_effect = Exception("HTTP Error")
    with patch.object(api.session, "post", return_value=resp):
        with pytest.raises(Exception):
            api.send("Hello")

# ── Polling ──

def test_poll_immediate_success(api):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "lastMessageId": "msg-last",
        "messageMap": {"msg-last": {"role": "assistant", "content": [{"body": "hi"}]}}
    }
    with patch.object(api.session, "get", return_value=resp), \
         patch("time.sleep"):
        out = api.poll("conv-123")
    assert out["role"] == "assistant"

def test_poll_retry_429(api):
    # 429 -> 200
    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {
        "lastMessageId": "msg-last",
        "messageMap": {"msg-last": {"role": "assistant", "content": [{"body": "hi"}]}}
    }
    with patch.object(api.session, "get", side_effect=[resp_429, resp_200]), \
         patch("time.sleep"):
        out = api.poll("conv-123")
def test_send_with_options(api):
    # Targets line 90 and 92
    resp = MagicMock()
    resp.status_code = 201
    resp.json.return_value = {"conversationId": "conv-123", "messageId": "msg-456"}
    with patch.object(api.session, "post", return_value=resp):
        conv_id, msg_id = api.send("Hello", enable_reasoning=True, conversation_id="conv-existing")
    assert conv_id == "conv-123"
    assert msg_id == "msg-456"

def test_send_inference_params_included(api):
    resp = MagicMock()
    resp.status_code = 201
    resp.json.return_value = {"conversationId": "conv-123", "messageId": "msg-456"}
    with patch.object(api.session, "post", return_value=resp) as mock_post:
        api.send("Hello", inference_params={"maxTokens": 512})
    call_kwargs = mock_post.call_args
    payload = call_kwargs[1]["json"] if "json" in call_kwargs[1] else call_kwargs[0][1]
    assert payload["inferenceParams"] == {"maxTokens": 512}

def test_send_no_inference_params_omits_key(api):
    resp = MagicMock()
    resp.status_code = 201
    resp.json.return_value = {"conversationId": "conv-123", "messageId": "msg-456"}
    with patch.object(api.session, "post", return_value=resp) as mock_post:
        api.send("Hello", inference_params=None)
    call_kwargs = mock_post.call_args
    payload = call_kwargs[1]["json"] if "json" in call_kwargs[1] else call_kwargs[0][1]
    assert "inferenceParams" not in payload

def test_send_and_wait_forwards_inference_params(api):
    with patch.object(api, "send", return_value=("conv-123", "msg-456")) as mock_send, \
         patch.object(api, "poll", return_value={"role": "assistant"}):
        api.send_and_wait("Hello", inference_params={"maxTokens": 256})
    mock_send.assert_called_once()
    _, kwargs = mock_send.call_args
    assert kwargs.get("inference_params") == {"maxTokens": 256}

def test_send_and_wait_conv_forwards_inference_params(api):
    with patch.object(api, "send", return_value=("conv-123", "msg-456")) as mock_send, \
         patch.object(api, "poll", return_value={"role": "assistant"}):
        api.send_and_wait_conv("Hello", inference_params={"maxTokens": 128})
    mock_send.assert_called_once()
    _, kwargs = mock_send.call_args
    assert kwargs.get("inference_params") == {"maxTokens": 128}

def test_send_inference_params_with_other_options(api):
    resp = MagicMock()
    resp.status_code = 201
    resp.json.return_value = {"conversationId": "conv-123", "messageId": "msg-456"}
    with patch.object(api.session, "post", return_value=resp) as mock_post:
        api.send("Hello", enable_reasoning=True, conversation_id="conv-existing",
                 inference_params={"maxTokens": 1024, "topP": 0.9})
    call_kwargs = mock_post.call_args
    payload = call_kwargs[1]["json"] if "json" in call_kwargs[1] else call_kwargs[0][1]
    assert payload["inferenceParams"] == {"maxTokens": 1024, "topP": 0.9}
    assert payload["conversationId"] == "conv-existing"
    assert payload["enableReasoning"] is True

def test_poll_with_cancel_check(api):
    # Targets line 106
    cancel_mock = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "lastMessageId": "msg-last",
        "messageMap": {"msg-last": {"role": "assistant", "content": [{"body": "hi"}]}}
    }
    with patch.object(api.session, "get", return_value=resp), \
         patch("time.sleep"):
        api.poll("conv-123", cancel_check=cancel_mock)
    cancel_mock.assert_called()

def test_poll_message_with_cancel_check(api):
    # Targets line 134
    cancel_mock = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"message": {"role": "assistant", "content": [{"body": "hi"}]}}
    with patch.object(api.session, "get", return_value=resp), \
         patch("time.sleep"):
        api.poll_message("conv-123", "msg-456", cancel_check=cancel_mock)
    cancel_mock.assert_called()

def test_poll_timeout(api):
    # Always 429 or something that doesn't return assistant
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"lastMessageId": None}

    # Set a very short timeout for the test
    api.poll_timeout = 0.1
    with patch.object(api.session, "get", return_value=resp), \
         patch("time.sleep"):
        with pytest.raises(TimeoutError):
            api.poll("conv-123")

def test_poll_message_success(api):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"message": {"role": "assistant", "content": [{"body": "hi"}]}}
    with patch.object(api.session, "get", return_value=resp), \
         patch("time.sleep"):
        out = api.poll_message("conv-123", "msg-456")
    assert out["role"] == "assistant"

def test_poll_message_retry_404_429(api):
    # 404 -> 429 -> 200
    resp_404 = MagicMock()
    resp_404.status_code = 404
    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {"message": {"role": "assistant", "content": [{"body": "hi"}]}}

    with patch.object(api.session, "get", side_effect=[resp_404, resp_429, resp_200]), \
         patch("time.sleep"):
        out = api.poll_message("conv-123", "msg-456")
    assert out["role"] == "assistant"

def test_poll_message_timeout(api):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"message": {"role": "user"}}

    api.poll_timeout = 0.1
    with patch.object(api.session, "get", return_value=resp), \
         patch("time.sleep"):
        with pytest.raises(TimeoutError):
            api.poll_message("conv-123", "msg-456")

# ── Conversation & Orchestration ──

def test_get_conversation_success(api):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"conversationId": "conv-123", "messageMap": {}}
    with patch.object(api.session, "get", return_value=resp):
        out = api.get_conversation("conv-123")
    assert out["conversationId"] == "conv-123"

def test_send_and_wait_orchestration(api):
    # Mock send and poll to avoid network logic in these tests
    with patch.object(api, "send", return_value=("conv-123", "msg-456")), \
         patch.object(api, "poll", return_value={"role": "assistant"}):
        out = api.send_and_wait("Hello")
    assert out["role"] == "assistant"

def test_send_and_wait_conv_orchestration(api):
    # Test both branches of send_and_wait_conv: conv_id provided and not provided
    with patch.object(api, "send", return_value=("conv-123", "msg-456")), \
         patch.object(api, "poll", return_value={"role": "assistant"}), \
         patch.object(api, "poll_message", return_value={"role": "assistant"}):

        # No conv_id
        out1, cid1 = api.send_and_wait_conv("Hello")
        assert out1["role"] == "assistant"
        assert cid1 == "conv-123"

        # With conv_id
        out2, cid2 = api.send_and_wait_conv("Hello", conversation_id="conv-existing")
        assert out2["role"] == "assistant"
        assert cid2 == "conv-123"

# ── Extraction ──

def test_extract_text(api):
    msg = {
        "content": [
            {"contentType": "text", "body": "Hello "},
            {"contentType": "reasoning", "body": "thinking..."},
            {"contentType": "text", "body": "World!"},
        ]
    }
    assert api.extract_text(msg) == "Hello \nWorld!"

def test_extract_reasoning_text_block(api):
    msg = {
        "content": [
            {"contentType": "reasoning", "text": "I am thinking..."},
        ]
    }
    assert api.extract_reasoning(msg) == "I am thinking..."

def test_extract_reasoning_thinking_log(api):
    msg = {
        "thinkingLog": "Log content here",
    }
    assert api.extract_reasoning(msg) == "Log content here"

def test_extract_reasoning_none(api):
    msg = {"content": []}
    assert api.extract_reasoning(msg) is None

# ── Model Listing ──

def test_list_models_success(api):
    # Mock a simplified openapi.json response
    spec = {
        "components": {
            "schemas": {
                "MessageInputWithoutMessageId": {
                    "properties": {
                        "model": {"enum": ["model-1", "model-2"]}
                    }
                }
            }
        }
    }
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = spec
    with patch.object(api.session, "get", return_value=resp):
        models = api.list_models()
    assert models == ["model-1", "model-2"]

def test_list_models_failure(api):
    with patch.object(api.session, "get", side_effect=Exception("Boom")):
        models = api.list_models()
    assert models == []


# ── ID validation (#851) ──

class TestValidateConversationIds:
    """Unit tests for the _validate_conversation_ids helper."""

    def test_valid_ids_pass(self):
        # Should not raise for normal string IDs.
        _validate_conversation_ids("conv-abc", "msg-xyz", {})

    def test_none_conv_id_raises(self):
        with pytest.raises(ValueError, match="conversationId"):
            _validate_conversation_ids(None, "msg-xyz", {})

    def test_none_msg_id_raises(self):
        with pytest.raises(ValueError, match="messageId"):
            _validate_conversation_ids("conv-abc", None, {})

    def test_integer_conv_id_raises(self):
        with pytest.raises(ValueError, match="conversationId"):
            _validate_conversation_ids(12345, "msg-xyz", {})

    def test_integer_msg_id_raises(self):
        with pytest.raises(ValueError, match="messageId"):
            _validate_conversation_ids("conv-abc", 67890, {})

    def test_empty_string_conv_id_raises(self):
        with pytest.raises(ValueError, match="conversationId"):
            _validate_conversation_ids("", "msg-xyz", {})

    def test_empty_string_msg_id_raises(self):
        with pytest.raises(ValueError, match="messageId"):
            _validate_conversation_ids("conv-abc", "", {})

    def test_error_message_includes_value(self):
        with pytest.raises(ValueError, match=r"None"):
            _validate_conversation_ids(None, "msg-xyz", {"conversationId": None})

    def test_error_message_includes_response(self):
        body = {"conversationId": None, "messageId": "msg-xyz"}
        with pytest.raises(ValueError, match="Full response"):
            _validate_conversation_ids(None, "msg-xyz", body)


class TestSendValidatesIds:
    """Integration: send() raises ValueError instead of returning bad IDs."""

    def test_send_raises_on_none_conv_id(self, api):
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"conversationId": None, "messageId": "msg-xyz"}
        with patch.object(api.session, "post", return_value=resp):
            with pytest.raises(ValueError, match="conversationId"):
                api.send("Hello")

    def test_send_raises_on_none_msg_id(self, api):
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"conversationId": "conv-abc", "messageId": None}
        with patch.object(api.session, "post", return_value=resp):
            with pytest.raises(ValueError, match="messageId"):
                api.send("Hello")

    def test_send_raises_on_integer_conv_id(self, api):
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"conversationId": 42, "messageId": "msg-xyz"}
        with patch.object(api.session, "post", return_value=resp):
            with pytest.raises(ValueError, match="conversationId"):
                api.send("Hello")

    def test_send_raises_on_missing_conv_id(self, api):
        # Key absent from response dict → data.get() returns None
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"messageId": "msg-xyz"}
        with patch.object(api.session, "post", return_value=resp):
            with pytest.raises(ValueError, match="conversationId"):
                api.send("Hello")

    def test_send_raises_on_missing_msg_id(self, api):
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"conversationId": "conv-abc"}
        with patch.object(api.session, "post", return_value=resp):
            with pytest.raises(ValueError, match="messageId"):
                api.send("Hello")

    def test_send_raises_on_empty_string_ids(self, api):
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"conversationId": "", "messageId": ""}
        with patch.object(api.session, "post", return_value=resp):
            with pytest.raises(ValueError, match="conversationId"):
                api.send("Hello")

    def test_send_valid_ids_still_returned(self, api):
        # Sanity-check: valid IDs still work after the guard is added.
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"conversationId": "conv-ok", "messageId": "msg-ok"}
        with patch.object(api.session, "post", return_value=resp):
            conv_id, msg_id = api.send("Hello")
        assert conv_id == "conv-ok"
        assert msg_id == "msg-ok"
