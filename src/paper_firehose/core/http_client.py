"""Shared HTTP client with retry logic and rate limiting."""

import time
from typing import Optional, Dict, Any
import requests


class RetryableHTTPClient:
    """HTTP client with exponential backoff retry logic and rate limiting.

    Handles common failure scenarios (429, 500, 502, 503, 504) with exponential
    backoff, respects Retry-After headers, and enforces rate limiting.

    Args:
        rps: Maximum requests per second (default: 1.0)
        max_retries: Maximum number of retry attempts (default: 3)
        timeout: Request timeout in seconds (default: 15)
    """

    def __init__(self, rps: float = 1.0, max_retries: int = 3, timeout: int = 15):
        self.session = requests.Session()
        self.rps = rps
        self.max_retries = max_retries
        self.timeout = timeout
        self.min_interval = 1.0 / max(rps, 0.01)
        self.last_request_time = 0.0

    def _rate_limit(self):
        """Enforce rate limiting between requests."""
        now = time.time()
        elapsed = now - self.last_request_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self.last_request_time = time.time()

    def get_with_retry(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
        return_none_on_404: bool = True
    ) -> Optional[requests.Response]:
        """Make GET request with exponential backoff retry logic.

        Args:
            url: URL to fetch
            headers: Optional request headers
            params: Optional query parameters
            timeout: Optional timeout override (uses instance default if None)
            return_none_on_404: If True, return None on 404; if False, let it raise

        Returns:
            Response object on success, None on 404 (if return_none_on_404=True)

        Raises:
            requests.HTTPError: On non-retryable HTTP errors
            requests.RequestException: On network errors after retries exhausted
        """
        timeout = timeout or self.timeout

        for attempt in range(self.max_retries):
            try:
                self._rate_limit()
                r = self.session.get(url, headers=headers, params=params, timeout=timeout)

                # Handle 404 specially
                if r.status_code == 404:
                    if return_none_on_404:
                        return None
                    r.raise_for_status()

                # Retry on throttling/server errors with exponential backoff
                if r.status_code in (429, 500, 502, 503, 504):
                    wait = self._calculate_backoff_time(r, attempt)
                    time.sleep(wait)
                    continue

                # Raise on other HTTP errors
                r.raise_for_status()
                return r

            except requests.RequestException as e:
                # Network or parsing error → backoff and retry
                if attempt < self.max_retries - 1:
                    wait = min(8.0, 2.0 ** attempt)
                    time.sleep(wait)
                    continue
                # Last attempt failed, re-raise
                raise

        # Should not reach here, but just in case
        return None

    def _calculate_backoff_time(self, response: requests.Response, attempt: int) -> float:
        """Calculate backoff time, respecting Retry-After header if present."""
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                wait = float(retry_after)
                return max(wait, 1.0)  # At least 1 second
            except (ValueError, TypeError):
                pass
        # Exponential backoff: 1s, 2s, 4s, max 8s
        return min(8.0, 2.0 ** attempt)

    def close(self):
        """Close the underlying session."""
        self.session.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
