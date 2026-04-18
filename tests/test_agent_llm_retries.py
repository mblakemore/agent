import pytest
import requests
from unittest.mock import patch, MagicMock
from agent import ContextOverflowError

def test_llm_request_success():
    with patch("requests.post") as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response
        
        from agent import _llm_request
        import logging
        log = logging.getLogger("test")
        kwargs = {"json": {}}
        
        response = _llm_request(log, **kwargs)
        assert response == mock_response
        assert mock_post.call_count == 1

def test_llm_request_500_retry_and_success():
    with patch("requests.post") as mock_post:
        mock_fail = MagicMock()
        mock_fail.status_code = 500
        
        mock_success = MagicMock()
        mock_success.status_code = 200
        
        mock_post.side_effect = [mock_fail, mock_success]
        
        from agent import _llm_request
        import logging
        log = logging.getLogger("test")
        kwargs = {"json": {}}
        
        with patch("time.sleep"):
            response = _llm_request(log, **kwargs)
            
        assert response == mock_success
        assert mock_post.call_count == 2

def test_llm_request_consecutive_500_overflow():
    with patch("requests.post") as mock_post:
        mock_fail = MagicMock()
        mock_fail.status_code = 500
        mock_post.return_value = mock_fail
        
        from agent import _llm_request
        import logging
        log = logging.getLogger("test")
        kwargs = {"json": {}}
        
        with patch("time.sleep"):
            with pytest.raises(ContextOverflowError):
                _llm_request(log, **kwargs)
        
        assert mock_post.call_count == 3

def test_llm_request_non_500_http_error():
    with patch("requests.post") as mock_post:
        mock_fail = MagicMock()
        mock_fail.status_code = 400
        mock_fail.raise_for_status.side_effect = requests.exceptions.HTTPError("400 Client Error")
        mock_post.return_value = mock_fail
        
        from agent import _llm_request
        import logging
        log = logging.getLogger("test")
        kwargs = {"json": {}}
        
        with pytest.raises(requests.exceptions.HTTPError):
            _llm_request(log, **kwargs)
            
        assert mock_post.call_count == 1

def test_llm_request_connection_error_retry():
    with patch("requests.post") as mock_post:
        mock_post.side_effect = [requests.exceptions.ConnectionError("Conn error"), MagicMock(status_code=200)]
        
        from agent import _llm_request
        import logging
        log = logging.getLogger("test")
        kwargs = {"json": {}}
        
        with patch("time.sleep"):
            response = _llm_request(log, **kwargs)
            
        assert response.status_code == 200
        assert mock_post.call_count == 2
