"""Bounded retries for read-only provider calls.

Only transient failures are retried: connection errors, timeouts, HTTP 429 and 5xx. Authentication and request
errors (other 4xx) fail at once. The number of attempts is always finite and the wait grows exponentially.
Error messages never include headers, tokens or query strings (API keys travel in them).
"""
from __future__ import annotations

import time
from typing import Any, Callable

import requests

RETRY_STATUS = (429, 500, 502, 503, 504)


class ProviderError(RuntimeError):
    pass


def request_with_retry(session: Any, method: str, url: str, *, timeout: float, max_retries: int = 3, backoff: float = 1.0,
                       sleep: Callable[[float], None] = time.sleep, retry_status: tuple[int, ...] = RETRY_STATUS,
                       label: str = "request", **kwargs) -> Any:
    """Call session.<method>(url, ...) up to 1 + max_retries times. Returns the last response (status may be >= 400)."""
    if max_retries < 0:
        raise ValueError("max_retries must be >= 0")
    call = getattr(session, method.lower())
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            res = call(url, timeout=timeout, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            res = None
        if res is not None and res.status_code not in retry_status:
            return res
        if attempt < max_retries:
            sleep(backoff * (2 ** attempt))
    if res is not None:
        return res
    raise ProviderError(f"{label}: network error after {max_retries + 1} attempts ({type(last_exc).__name__})")
