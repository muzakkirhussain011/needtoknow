"""HTTP for the authorizers, with the standard library only.

* Connections are kept alive and reused, one per thread, so a busy retriever does not open a TCP
  (and TLS) connection for every permission check.
* A request that fails in a way that is safe to repeat is retried `retries` times (default once):
  a refused or reset connection, a response that never arrived, or HTTP 502, 503 or 504. Every
  request is a query (OPA's data API, OpenFGA's checks and object lists), so repeating one has no
  side effects. Timeouts are not retried, because that would double the worst-case latency, and
  neither are other HTTP errors or invalid answers, which would only repeat.
* A connection the server closed while it sat idle is replaced and the request sent again, once,
  without counting as a retry.
* An optional `CircuitBreaker` stops calling a service that keeps failing, for a cooldown.
* Redirects are never followed: a permission service has no reason to send one.
* When a proxy applies to the host (the standard `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY`
  variables), requests go through urllib, which handles proxies, without connection reuse.

Whatever still fails raises `AuthorizationError`, so retrieval fails closed.
"""

from __future__ import annotations

import http.client
import json
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import SplitResult, urlsplit

from needtoknow.authz import AuthorizationError

__all__ = ["CircuitBreaker", "JsonClient"]

_RETRY_STATUS = frozenset({502, 503, 504})


class CircuitBreaker:
    """Pauses requests to a permission service that keeps failing.

    After `failures` consecutive failed requests the breaker opens, and every call raises
    `AuthorizationError` at once, without contacting the service, for `reset_after` seconds. Then
    one request is let through as a probe: if it succeeds the breaker closes, if it fails the
    breaker opens again. A failure is a request that got no HTTP response, or a 5xx one; any other
    response shows that the service is up. Retrieval fails closed while the breaker is open, as it
    would if every request failed, but a struggling service is not flooded with requests.

    One breaker can be shared by several authorizers that call the same service.
    """

    def __init__(
        self, failures: int = 5, reset_after: float = 30.0, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if failures < 1 or reset_after <= 0:
            raise ValueError("failures and reset_after must be positive")
        self.failures = failures
        self.reset_after = reset_after
        self._clock = clock
        self._lock = threading.Lock()
        self._consecutive = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def is_open(self) -> bool:
        """True while requests are paused, for monitoring."""
        with self._lock:
            return self._opened_at is not None

    def allow(self) -> None:
        """Raise `AuthorizationError` while open; after the cooldown, let one probe through."""
        with self._lock:
            if self._opened_at is None:
                return
            if self._probing or self._clock() - self._opened_at < self.reset_after:
                raise AuthorizationError("the permission service keeps failing; requests are paused (circuit open)")
            self._probing = True

    def record(self, ok: bool) -> None:
        """Record the outcome of a request that `allow` let through."""
        with self._lock:
            probing, self._probing = self._probing, False
            if ok:
                self._consecutive = 0
                self._opened_at = None
                return
            self._consecutive += 1
            if probing or self._consecutive >= self.failures:
                self._opened_at = self._clock()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # urllib then raises HTTPError for the 3xx response


def _proxy_applies(parts: SplitResult) -> bool:
    return parts.scheme in urllib.request.getproxies() and not urllib.request.proxy_bypass(parts.hostname or "")


class JsonClient:
    """POSTs JSON to one service and returns the decoded answer of a 200 response."""

    def __init__(
        self,
        url: str,
        *,
        service: str,
        timeout: float,
        headers: Mapping[str, str],
        retries: int = 1,
        breaker: CircuitBreaker | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"the {service} url must start with http:// or https:// and name a host")
        if retries < 0:
            raise ValueError("retries must be zero or more")
        self.url = url.rstrip("/")
        self.service = service
        self.timeout = timeout
        self.retries = retries
        self.breaker = breaker
        self.headers = {"Content-Type": "application/json", **headers}
        self._parts = parts
        self._prefix = parts.path.rstrip("/")
        self._ssl = ssl_context or (ssl.create_default_context() if parts.scheme == "https" else None)
        self._proxied = _proxy_applies(parts)
        self._local = threading.local()

    def post_json(self, path: str, payload: Any, what: str, *, lines: bool = False) -> Any:
        """POST `payload` and return the decoded answer; with `lines`, a list with one decoded
        value per line of a newline-delimited JSON answer (a streamed response)."""
        try:
            body = json.dumps(payload).encode()
        except (TypeError, ValueError) as exc:
            raise AuthorizationError(f"{what}: the input is not JSON-serialisable: {exc}") from exc
        if self.breaker is not None:
            self.breaker.allow()
        reachable = False  # recorded on every way out, so a probe can never leave the breaker stuck
        try:
            attempt = 0
            while True:
                try:
                    status, raw = self._send(path, body)
                except TimeoutError as exc:
                    raise AuthorizationError(f"{what} timed out after {self.timeout} s") from exc
                except ssl.SSLCertVerificationError as exc:
                    raise AuthorizationError(f"{what}: TLS certificate verification failed: {exc}") from exc
                except (OSError, http.client.HTTPException) as exc:
                    failure = f"{type(exc).__name__}: {exc}"
                else:
                    if status not in _RETRY_STATUS:
                        reachable = status < 500
                        return self._decode(status, raw, what, lines)
                    failure = f"HTTP {status}"
                if attempt >= self.retries:
                    raise AuthorizationError(f"{what} failed: {failure}")
                attempt += 1
                time.sleep(min(0.05 * 2 ** (attempt - 1), 1.0))
        finally:
            if self.breaker is not None:
                self.breaker.record(reachable)

    @staticmethod
    def _decode(status: int, raw: bytes, what: str, lines: bool = False) -> Any:
        if status != 200:
            raise AuthorizationError(f"{what} returned HTTP {status}")
        try:
            if lines:
                return [json.loads(line) for line in raw.splitlines() if line.strip()]
            return json.loads(raw)
        except ValueError as exc:
            raise AuthorizationError(f"{what} returned invalid JSON") from exc

    # ---- transports ------------------------------------------------------------------------

    def _send(self, path: str, body: bytes) -> tuple[int, bytes]:
        if self._proxied:
            return self._send_urllib(path, body)
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                return self._exchange(conn, path, body)
            except (ConnectionError, http.client.BadStatusLine):
                pass  # the server closed the idle connection; send once more on a new one
        return self._exchange(self._connect(), path, body)

    def _connect(self) -> http.client.HTTPConnection:
        host, port = self._parts.hostname or "", self._parts.port
        if self._parts.scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=self.timeout, context=self._ssl)
        return http.client.HTTPConnection(host, port, timeout=self.timeout)

    def _exchange(self, conn: http.client.HTTPConnection, path: str, body: bytes) -> tuple[int, bytes]:
        self._local.conn = None
        try:
            conn.request("POST", self._prefix + path, body=body, headers=self.headers)
            response = conn.getresponse()
            raw = response.read()
        except BaseException:
            conn.close()
            raise
        if response.will_close:
            conn.close()
        else:
            self._local.conn = conn
        return response.status, raw

    def _send_urllib(self, path: str, body: bytes) -> tuple[int, bytes]:
        handlers: list[Any] = [_NoRedirect()]
        if self._ssl is not None:
            handlers.append(urllib.request.HTTPSHandler(context=self._ssl))
        opener = urllib.request.build_opener(*handlers)
        # The scheme is restricted to http(s) in __init__, so this cannot open file:// or others.
        request = urllib.request.Request(self.url + path, data=body, headers=self.headers, method="POST")  # noqa: S310
        try:
            with opener.open(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError(str(exc.reason)) from exc
            raise
