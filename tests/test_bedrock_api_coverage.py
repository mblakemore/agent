import pytest
from unittest.mock import MagicMock, patch
from bedrock_api import BedrockChatAPI

def test_bedrock_api_health_ok():
    api = BedrockChatAPI({"api_url": "http://api.example.com", "api_key": "key"})
    resp = MagicMock()
    resp.status_code = 200
    with patch.object(api.session, "get", return_value=resp):
        assert api.health() is True

def test_bedrock_api_health_fail():
    api = BedrockChatAPI({"api_url": "http://api.example.com", "api_key": "key"})
    resp = MagicMock()
    resp.status_code = 500
    with patch.object(api.session, "get", return_value=resp):
        assert api.health() is False

def test_bedrock_api_health_exception():
    api = BedrockChatAPI({"api_url": "http://api.example.com", "api_key": "key"})
    with patch.object(api.session, "get", side_effect=Exception("boom")):
        assert api.health() is False

def test_bedrock_api_send_success():
    api = BedrockChatAPI({"api_url": "http://api.example.com", "api_key": "key"})
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"conversationId": "conv-123", "messageId": "msg-456"}
    with patch.object(api.session, "post", return_value=resp):
        conv_id, msg_id = api.send("hello")
        assert conv_id == "conv-123"
        assert msg_id == "msg-456"

def test_bedrock_api_send_with_params():
    api = BedrockChatAPI({"api_url": "http://api.example.com", "api_key": "key"})
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"conversationId": "conv-123", "messageId": "msg-456"}
    with patch.object(api.session, "post", return_value=resp):
        api.send("hello", enable_reasoning=True, conversation_id="conv-prev")
        # Verify payload contains reasoning and conversationId
        args, kwargs = api.session.post.call_args
        payload = kwargs['json']
        assert payload["enableReasoning"] is True
        assert payload["conversationId"] == "conv-prev"

def test_bedrock_api_poll_success():
    api = BedrockChatAPI({"api_url": "http://api.example.com", "api_key": "key", "poll_interval": 0.01})
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "lastMessageId": "msg-last",
        "messageMap": {
            "msg-last": {"role": "assistant", "content": [{"contentType": "text", "body": "hi"}]}
        }
    }
    with patch.object(api.session, "get", return_value=resp):
        msg = api.poll("conv-123")
        assert msg["role"] == "assistant"

def test_bedrock_api_poll_retry_429():
    api = BedrockChatAPI({"api_url": "http://api.example.com", "api_key": "key", "poll_interval": 0.01})
    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {
        "lastMessageId": "msg-last",
        "messageMap": {
            "msg-last": {"role": "assistant", "content": [{"contentType": "text", "body": "hi"}]}
        }
    }
    with patch.object(api.session, "get", side_effect=[resp_429, resp_200]):
        msg = api.poll("conv-123")
        assert msg["role"] == "assistant"

def test_bedrock_api_poll_timeout():
    api = BedrockChatAPI({"api_url": "http://api.example.com", "api_key": "key", "poll_interval": 0.01, "poll_timeout": 0.1})
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"lastMessageId": None} # never ready
    with patch.object(api.session, "get", return_value=resp):
        with pytest.raises(TimeoutError):
            api.poll("conv-123")

def test_bedrock_api_poll_message_success():
    api = BedrockChatAPI({"api_url": "http://api.example.com", "api_key": "key", "poll_interval": 0.01})
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"message": {"role": "assistant", "content": [{"contentType": "text", "body": "hi"}]}}
    with patch.object(api.session, "get", return_value=resp):
        msg = api.poll_message("conv-123", "msg-456")
        assert msg["role"] == "assistant"

def test_bedrock_api_poll_message_retry_404():
    api = BedrockChatAPI({"api_url": "http://api.example.com", "api_key": "key", "poll_interval": 0.01})
    resp_404 = MagicMock()
    resp_404.status_code = 404
    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {"message": {"role": "assistant", "content": [{"contentType": "text", "body": "hi"}]}}
    with patch.object(api.session, "get", side_effect=[resp_404, resp_200]):
        msg = api.poll_message("conv-123", "msg-456")
        assert msg["role"] == "assistant"

def test_bedrock_api_poll_message_retry_429():
    api = BedrockChatAPI({"api_url": "http://api.example.com", "api_key": "key", "poll_interval": 0.01})
    resp_404 = MagicMock()
    resp_404.status_code = 404
    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {"message": {"role": "assistant", "content": [{"contentType": "text", "body": "hi"}]}}
    with patch.object(api.session, "get", side_effect=[resp_404, resp_429, resp_200]):
        msg = api.poll_message("conv-123", "msg-456")
        assert msg["role"] == "assistant"

def test_bedrock_api_poll_message_timeout():
    api = BedrockChatAPI({"api_url": "http://api.example.com", "api_key": "key", "poll_interval": 0.01, "poll_timeout": 0.1})
    resp = MagicMock()
    resp.status_code = 404
    with patch.object(api.session, "get", return_value=resp):
        with pytest.raises(TimeoutError):
            api.poll_message("conv-123", "msg-456")

def test_bedrock_api_get_conversation_success():
    api = BedrockChatAPI({"api_url": "http://api.example.com", "api_key": "key"})
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"conv": "data"}
    with patch.object(api.session, "get", return_value=resp):
        conv = api.get_conversation("conv-123")
        assert conv == {"conv": "data"}

def test_bedrock_api_extract_text_success():
    api = BedrockChatAPI()
    msg = {
        "content": [
            {"contentType": "text", "body": "Hello"},
            {"contentType": "reasoning", "text": "Thinking..."},
            {"contentType": "text", "body": "World"},
        ]
    }
    assert api.extract_text(msg) == "Hello\nWorld"

def test_bedrock_api_extract_text_empty():
    api = BedrockChatAPI()
    msg = {"content": []}
    assert api.extract_text(msg) == ""

def test_bedrock_api_extract_reasoning_success():
    api = BedrockChatAPI()
    msg = {
        "content": [
            {"contentType": "reasoning", "text": "Thinking..."},
        ]
    }
    assert api.extract_reasoning(msg) == "Thinking..."

def test_bedrock_api_extract_reasoning_thinking_log():
    api = BedrockChatAPI()
    msg = {"thinkingLog": "Log content"}
    assert api.extract_reasoning(msg) == "Log content"

def test_bedrock_api_extract_reasoning_none():
    api = BedrockChatAPI()
    msg = {"content": []}
    assert api.extract_reasoning(msg) is None

def test_bedrock_api_list_models_success():
    api = BedrockChatAPI({"api_url": "http://api.example.com", "api_key": "key"})
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "components": {
            "schemas": {
                "MessageInputWithoutMessageId": {
                    "properties": {
                        "model": {"enum": ["model1", "model2"]}
                    }
                }
            }
        }
    }
    with patch.object(api.session, "get", return_value=resp):
        models = api.list_models()
        assert models == ["model1", "model2"]

def test_bedrock_api_list_models_fail():
    api = BedrockChatAPI({"api_url": "http://api.example.com", "api_key": "key"})
    with patch.object(api.session, "get", side_effect=Exception("boom")):
        models = api.list_models()
        assert models == []

def test_bedrock_api_send_and_wait_success():
    api = BedrockChatAPI({"api_url": "http://api.example.com", "api_key": "key", "poll_interval": 0.01})
    with patch.object(api, "send", return_value=("conv-123", "msg-456")):
        with patch.object(api, "poll", return_value={"role": "assistant", "body": "hi"}):
            res = api.send_and_wait("hello")
            assert res["role"] == "assistant"

def test_bedrock_api_send_and_wait_conv_success():
    api = BedrockChatAPI({"api_url": "http://api.example.com", "api_key": "key", "poll_interval": 0.01})
    with patch.object(api, "send", return_value=("conv-123", "msg-456")):
        with patch.object(api, "poll_message", return_value={"role": "assistant", "body": "hi"}):
            msg, conv_id = api.send_and_wait_conv("hello", conversation_id="conv-prev")
            assert msg["role"] == "assistant"
            assert conv_id == "conv-123"
