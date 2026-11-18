import pytest
import requests
from unittest.mock import patch, MagicMock
import logging
from agent import ContextOverflowError
from llm_backend import LlamacppBackend

def test_stream_chat_success():
    with patch("llm_backend.requests.post") as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response
        
        b = LlamacppBackend({"base_url": "http://x", "model": "m"})
        log = logging.getLogger("test")
        
        response = b.stream_chat(log, json={})
        assert response == mock_response
        assert mock_post.call_count == 1

def test_stream_chat_500_retry_and_success():
    with patch("llm_backend.requests.post") as mock_post:
        mock_fail = MagicMock()
        mock_fail.status_code = 500
        
        mock_success = MagicMock()
        mock_success.status_code = 200
        
        mock_post.side_effect = [mock_fail, mock_success]
        
        b = LlamacppBackend({"base_url": "http://x", "model": "m"})
        log = logging.getLogger("test")
        
        with patch("llm_backend.time.sleep"):
            response = b.stream_chat(log, json={})
            
        assert response == mock_success
        assert mock_post.call_count == 2

def test_stream_chat_consecutive_500_overflow():
    with patch("llm_backend.requests.post") as mock_post:
        mock_fail = MagicMock()
        mock_fail.status_code = 500
        mock_post.return_value = mock_fail
        
        b = LlamacppBackend({"base_url": "http://x", "model": "m"})
        log = logging.getLogger("test")
        
        with patch("llm_backend.time.sleep"):
            with pytest.raises(ContextOverflowError):
                b.stream_chat(log, json={})
        
        assert mock_post.call_count == 3

def test_stream_chat_non_500_http_error():
    with patch("llm_backend.requests.post") as mock_post:
        mock_fail = MagicMock()
        mock_fail.status_code = 400
        mock_fail.raise_for_status.side_effect = requests.exceptions.HTTPError("400 Client Error")
        mock_post.return_value = mock_fail
        
        b = LlamacppBackend({"base_url": "http://x", "model": "m"})
        log = logging.getLogger("test")
        
        with pytest.raises(requests.exceptions.HTTPError):
            b.stream_chat(log, json={})
            
        assert mock_post.call_count == 1

def test_stream_chat_connection_error_retry():
    with patch("llm_backend.requests.post") as mock_post:
        mock_post.side_effect = [requests.exceptions.ConnectionError("Conn error"), MagicMock(status_code=200)]
        
        b = LlamacppBackend({"base_url": "http://x", "model": "m"})
        log = logging.getLogger("test")
        
        with patch("llm_backend.time.sleep"):
            response = b.stream_chat(log, json={})
            
        assert response.status_code == 200
        assert mock_post.call_count == 2

def test_stream_chat_502_retry_and_success():
    with patch("llm_backend.requests.post") as mock_post:
        mock_fail = MagicMock()
        mock_fail.status_code = 502
        
        mock_success = MagicMock()
        mock_success.status_code = 200
        
        mock_post.side_effect = [mock_fail, mock_success]
        
        b = LlamacppBackend({"base_url": "http://x", "model": "m"})
        log = logging.getLogger("test")
        
        with patch("llm_backend.time.sleep"):
            response = b.stream_chat(log, json={})
            
        assert response == mock_success
        assert mock_post.call_count == 2
