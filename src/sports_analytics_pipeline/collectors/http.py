"""Small, testable HTTP layer shared by our read-only collectors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import time
from typing import Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


SECRET_QUERY_KEYS = {"apikey", "api_key", "key", "token", "access_token"}
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class CollectorHttpError(RuntimeError):
    """A safe HTTP error that never includes secret query values."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]
    received_at: datetime

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CollectorHttpError("The source returned invalid JSON", status=self.status) from exc


class HttpTransport(Protocol):
    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        """Make one read-only GET request."""

    def post_json(
        self,
        url: str,
        *,
        payload: object,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        """Make one read-only JSON query to a protected adapter."""


def safe_url(url: str) -> str:
    """Remove credentials from a URL before it reaches an error or log."""
    parts = urlsplit(url)
    safe_query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        safe_query.append((key, "[REDACTED]" if key.casefold() in SECRET_QUERY_KEYS else value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(safe_query), ""))


class UrllibTransport:
    def __init__(
        self,
        *,
        timeout_seconds: int = 20,
        attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")
        if attempts < 1:
            raise ValueError("attempts must be positive")
        self.timeout_seconds = timeout_seconds
        self.attempts = attempts
        self._sleep = sleep

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        query = urlencode(params or {})
        parts = urlsplit(url)
        combined_query = "&".join(item for item in (parts.query, query) if item)
        request_url = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, combined_query, parts.fragment)
        )
        safe_request_url = safe_url(request_url)
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "SportsAnalyticsPipeline/0.2",
            **dict(headers or {}),
        }

        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            request = Request(request_url, headers=request_headers, method="GET")
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    return HttpResponse(
                        status=int(response.status),
                        body=response.read(),
                        headers={key.casefold(): value for key, value in response.headers.items()},
                        received_at=datetime.now(timezone.utc),
                    )
            except HTTPError as exc:
                last_error = exc
                if exc.code not in RETRYABLE_STATUS_CODES or attempt == self.attempts:
                    raise CollectorHttpError(
                        f"GET {safe_request_url} failed with HTTP {exc.code}",
                        status=exc.code,
                    ) from exc
            except (URLError, TimeoutError) as exc:
                last_error = exc
                if attempt == self.attempts:
                    raise CollectorHttpError(
                        f"GET {safe_request_url} failed after {self.attempts} attempts"
                    ) from exc

            self._sleep(min(2 ** (attempt - 1), 4))

        raise CollectorHttpError(f"GET {safe_request_url} failed") from last_error

    def post_json(
        self,
        url: str,
        *,
        payload: object,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        """POST event context to an adapter without exposing it in a URL or log."""
        safe_request_url = safe_url(url)
        request_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "SportsAnalyticsPipeline/0.4",
            **dict(headers or {}),
        }
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            request = Request(url, data=body, headers=request_headers, method="POST")
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    return HttpResponse(
                        status=int(response.status),
                        body=response.read(),
                        headers={key.casefold(): value for key, value in response.headers.items()},
                        received_at=datetime.now(timezone.utc),
                    )
            except HTTPError as exc:
                last_error = exc
                if exc.code not in RETRYABLE_STATUS_CODES or attempt == self.attempts:
                    raise CollectorHttpError(
                        f"POST {safe_request_url} failed with HTTP {exc.code}",
                        status=exc.code,
                    ) from exc
            except (URLError, TimeoutError) as exc:
                last_error = exc
                if attempt == self.attempts:
                    raise CollectorHttpError(
                        f"POST {safe_request_url} failed after {self.attempts} attempts"
                    ) from exc

            self._sleep(min(2 ** (attempt - 1), 4))

        raise CollectorHttpError(f"POST {safe_request_url} failed") from last_error
