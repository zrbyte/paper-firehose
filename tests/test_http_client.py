"""Tests for core.http_client.RetryableHTTPClient.

Exercises retry logic, rate limiting, backoff calculations, and error handling
using mocked HTTP responses to simulate real network conditions.
"""

import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import requests

from paper_firehose.core.http_client import RetryableHTTPClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(status_code=200, headers=None, json_data=None, text="OK"):
    """Build a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.text = text
    resp.json.return_value = json_data or {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    return resp


# ---------------------------------------------------------------------------
# Basic requests
# ---------------------------------------------------------------------------

class TestBasicRequests:
    def test_successful_get(self):
        client = RetryableHTTPClient()
        resp = _mock_response(200)
        with patch.object(client.session, "get", return_value=resp):
            result = client.get_with_retry("http://example.com/api")
        assert result.status_code == 200

    def test_404_returns_none_by_default(self):
        client = RetryableHTTPClient()
        resp = _mock_response(404)
        with patch.object(client.session, "get", return_value=resp):
            result = client.get_with_retry("http://example.com/missing")
        assert result is None

    def test_404_raises_when_configured(self):
        client = RetryableHTTPClient()
        resp = _mock_response(404)
        with patch.object(client.session, "get", return_value=resp):
            with pytest.raises(requests.HTTPError):
                client.get_with_retry("http://example.com/missing", return_none_on_404=False)

    def test_non_retryable_error_raises_immediately(self):
        client = RetryableHTTPClient(max_retries=3)
        resp = _mock_response(403)
        with patch.object(client.session, "get", return_value=resp):
            with pytest.raises(requests.HTTPError):
                client.get_with_retry("http://example.com/forbidden")


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------

class TestRetryLogic:
    @patch("paper_firehose.core.http_client.time.sleep")
    def test_retries_on_429(self, mock_sleep):
        client = RetryableHTTPClient(max_retries=3)
        throttled = _mock_response(429)
        success = _mock_response(200)
        with patch.object(client.session, "get", side_effect=[throttled, throttled, success]):
            result = client.get_with_retry("http://example.com/api")
        assert result.status_code == 200
        assert mock_sleep.call_count >= 2

    @patch("paper_firehose.core.http_client.time.sleep")
    def test_retries_on_503(self, mock_sleep):
        client = RetryableHTTPClient(max_retries=2)
        error_resp = _mock_response(503)
        success = _mock_response(200)
        with patch.object(client.session, "get", side_effect=[error_resp, success]):
            result = client.get_with_retry("http://example.com/api")
        assert result.status_code == 200

    @patch("paper_firehose.core.http_client.time.sleep")
    def test_retries_exhausted_returns_none(self, mock_sleep):
        client = RetryableHTTPClient(max_retries=2)
        error_resp = _mock_response(500)
        with patch.object(client.session, "get", return_value=error_resp):
            result = client.get_with_retry("http://example.com/api")
        assert result is None

    @patch("paper_firehose.core.http_client.time.sleep")
    def test_network_error_retries(self, mock_sleep):
        client = RetryableHTTPClient(max_retries=3)
        success = _mock_response(200)
        with patch.object(client.session, "get",
                          side_effect=[requests.ConnectionError("timeout"), success]):
            result = client.get_with_retry("http://example.com/api")
        assert result.status_code == 200

    @patch("paper_firehose.core.http_client.time.sleep")
    def test_network_error_exhausted_raises(self, mock_sleep):
        client = RetryableHTTPClient(max_retries=2)
        with patch.object(client.session, "get",
                          side_effect=requests.ConnectionError("refused")):
            with pytest.raises(requests.ConnectionError):
                client.get_with_retry("http://example.com/api")


# ---------------------------------------------------------------------------
# Backoff calculation
# ---------------------------------------------------------------------------

class TestBackoff:
    def test_exponential_backoff(self):
        client = RetryableHTTPClient()
        resp = _mock_response(429)
        assert client._calculate_backoff_time(resp, 0) == 1.0  # 2^0
        assert client._calculate_backoff_time(resp, 1) == 2.0  # 2^1
        assert client._calculate_backoff_time(resp, 2) == 4.0  # 2^2
        assert client._calculate_backoff_time(resp, 3) == 8.0  # 2^3 (max)
        assert client._calculate_backoff_time(resp, 10) == 8.0  # capped at 8

    def test_retry_after_header_respected(self):
        client = RetryableHTTPClient()
        resp = _mock_response(429, headers={"Retry-After": "5"})
        assert client._calculate_backoff_time(resp, 0) == 5.0

    def test_retry_after_minimum_one_second(self):
        client = RetryableHTTPClient()
        resp = _mock_response(429, headers={"Retry-After": "0.1"})
        assert client._calculate_backoff_time(resp, 0) == 1.0

    def test_retry_after_invalid_falls_back_to_exponential(self):
        client = RetryableHTTPClient()
        resp = _mock_response(429, headers={"Retry-After": "not-a-number"})
        assert client._calculate_backoff_time(resp, 0) == 1.0  # 2^0 fallback


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class TestRateLimiting:
    def test_min_interval_calculation(self):
        client = RetryableHTTPClient(rps=2.0)
        assert client.min_interval == 0.5

    def test_min_interval_guards_zero_rps(self):
        client = RetryableHTTPClient(rps=0.0)
        assert client.min_interval == 100.0  # 1/0.01

    @patch("paper_firehose.core.http_client.time.sleep")
    @patch("paper_firehose.core.http_client.time.time")
    def test_rate_limit_sleeps_when_too_fast(self, mock_time, mock_sleep):
        client = RetryableHTTPClient(rps=1.0)
        client.last_request_time = 100.0
        mock_time.return_value = 100.3  # only 0.3s elapsed, need 1.0s
        client._rate_limit()
        mock_sleep.assert_called_once()
        # Should sleep approximately 0.7s
        slept = mock_sleep.call_args[0][0]
        assert 0.6 < slept < 0.8


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------

class TestContextManager:
    def test_close_called(self):
        client = RetryableHTTPClient()
        with patch.object(client.session, "close") as mock_close:
            with client:
                pass
        mock_close.assert_called_once()
