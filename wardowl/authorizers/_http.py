"""HTTP for the authorizers, with the standard library only.

* Connections are kept alive and reused, one per thread, so a busy retriever does not open a TCP
  (and TLS) connection for every permission check. A connection made before `fork()` is never used
  in the child process: parent and child would share one socket and could read each other's answers.
* `timeout` bounds a whole call: connecting, sending, every read of the answer, the pause before a
  retry and the retry itself. An answer larger than `max_response_bytes` (10 MiB by default) is
  refused.
* A request that fails in a way that is safe to repeat is retried `retries` times (default once):
  a refused or reset connection, a response that never arrived, or HTTP 502, 503 or 504. Every
  request is a query (OPA's data API, OpenFGA's checks and object lists), so repeating one has no
  side effects. Timeouts are not retried, and neither are other HTTP errors or invalid answers,
  which would only repeat.
* A connection the server closed while it sat idle is replaced and the request sent again, once,
  without counting as a retry.
* An optional `CircuitBreaker` stops calling a service that keeps failing, for a cooldown.
* Redirects are never followed: a permission service has no reason to send one.
* When a proxy applies to the host (the standard `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY`
  variables), requests go through urllib, which handles proxies, without connection reuse.
* `ssl_context` is refused for an http:// url, where it would not be used. An `Authorization`
  header is refused over http:// unless the url names this machine (`localhost`, `127.0.0.1`,
  `::1`) and no proxy applies, or `allow_insecure=True` is passed.

Whatever still fails raises `AuthorizationError`, so retrieval fails closed.
"""

from __future__ import annotations

import http.client
import io
import ipaddress
import json
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import SplitResult, urlsplit

from wardowl.authz import AuthorizationError

__all__ = ["DEFAULT_MAX_RESPONSE_BYTES", "CircuitBreaker", "JsonClient"]

_RETRY_STATUS = frozenset({502, 503, 504})

DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
"""The largest answer a permission service may send, unless `max_response_bytes` says otherwise."""

_deadline = threading.local()  # `at`: when the request in flight on this thread must be answered


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


class _TooLarge(Exception):
    """The answer is larger than `max_response_bytes`."""


def _left(deadline: float) -> float:
    """The seconds left before `deadline`; `TimeoutError` when none are."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("the deadline passed")
    return left


class _DeadlineIO(io.RawIOBase):
    """A response's socket file: every read waits only for the time left before the deadline, so a
    server that sends its answer slowly cannot stretch a call past `timeout`."""

    def __init__(self, sock: socket.socket, deadline: float) -> None:
        super().__init__()
        self._raw = sock.makefile("rb", 0)  # counted among the socket's files, as a response's own is
        self._socket = sock
        self._deadline = deadline

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int | None:
        self._socket.settimeout(_left(self._deadline))
        return self._raw.readinto(b)

    def close(self) -> None:
        if not self.closed:
            self._raw.close()
        super().close()


class _Response(http.client.HTTPResponse):
    """An HTTP response read under the deadline of the request in flight on this thread."""

    def __init__(self, sock: socket.socket, *args: Any, **kwargs: Any) -> None:
        super().__init__(sock, *args, **kwargs)
        deadline: float | None = getattr(_deadline, "at", None)
        if deadline is not None:
            plain = self.fp  # nothing has been read from it yet
            self.fp = io.BufferedReader(_DeadlineIO(sock, deadline))
            plain.close()


class _HTTPConnection(http.client.HTTPConnection):
    response_class = _Response


class _HTTPSConnection(http.client.HTTPSConnection):
    response_class = _Response


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(_HTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, context: ssl.SSLContext) -> None:
        super().__init__(context=context)
        self._tls = context

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(_HTTPSConnection, req, context=self._tls)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # urllib then raises HTTPError for the 3xx response


def _proxy_applies(parts: SplitResult) -> bool:
    return parts.scheme in urllib.request.getproxies() and not urllib.request.proxy_bypass(parts.hostname or "")


def _loopback(host: str) -> bool:
    """True when `host` names this machine, so a request to it never crosses a network."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        allow_insecure: bool = False,
    ) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"the {service} url must start with http:// or https:// and name a host")
        if retries < 0:
            raise ValueError("retries must be zero or more")
        if timeout <= 0 or max_response_bytes < 1:
            raise ValueError("timeout and max_response_bytes must be positive")
        if ssl_context is not None and parts.scheme != "https":
            raise ValueError(f"ssl_context is only used with https://, but the {service} url is {parts.scheme}://")
        self._proxied = _proxy_applies(parts)
        cleartext = parts.scheme == "http" and (self._proxied or not _loopback(parts.hostname or ""))
        if cleartext and not allow_insecure and any(h.lower() == "authorization" for h in headers):
            raise ValueError(
                f"the {service} url is http://, so the Authorization header would cross the network unencrypted: "
                "use https://, or pass allow_insecure=True if the network is trusted"
            )
        self.url = url.rstrip("/")
        self.service = service
        self.timeout = timeout
        self.retries = retries
        self.breaker = breaker
        self.max_response_bytes = max_response_bytes
        self.headers = {"Content-Type": "application/json", **headers}
        self._parts = parts
        self._prefix = parts.path.rstrip("/")
        self._ssl = ssl_context or (ssl.create_default_context() if parts.scheme == "https" else None)
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
        deadline = time.monotonic() + self.timeout
        reachable = False  # recorded on every way out, so a probe can never leave the breaker stuck
        try:
            attempt = 0
            while True:
                last: BaseException | None = None  # the exception behind the failure, kept as its cause
                try:
                    status, raw = self._send(path, body, deadline)
                except TimeoutError as exc:
                    raise AuthorizationError(f"{what} timed out after {self.timeout} s") from exc
                except ssl.SSLCertVerificationError as exc:
                    raise AuthorizationError(f"{what}: TLS certificate verification failed: {exc}") from exc
                except _TooLarge as exc:
                    reachable = True  # the service answered; the answer is what is wrong
                    raise AuthorizationError(f"{what} answered with more than {self.max_response_bytes} bytes") from exc
                except (OSError, http.client.HTTPException) as exc:
                    failure, last = f"{type(exc).__name__}: {exc}", exc
                else:
                    if status not in _RETRY_STATUS:
                        reachable = status < 500
                        return self._decode(status, raw, what, lines)
                    failure = f"HTTP {status}"
                pause = min(0.05 * 2**attempt, 1.0)
                if attempt >= self.retries or time.monotonic() + pause >= deadline:
                    raise AuthorizationError(f"{what} failed: {failure}") from last
                attempt += 1
                time.sleep(pause)
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

    def _read(self, response: http.client.HTTPResponse) -> bytes:
        """The whole body; `_TooLarge` when it is over `max_response_bytes`, before reading it when
        the length is announced."""
        limit = self.max_response_bytes
        if response.length is not None:
            if response.length > limit:
                raise _TooLarge(response.length)
            return response.read()  # raises IncompleteRead if the body is cut short
        raw = response.read(limit + 1)  # chunked, or ended by closing the connection
        if len(raw) > limit:
            raise _TooLarge(len(raw))
        return raw

    # ---- transports ------------------------------------------------------------------------

    def _send(self, path: str, body: bytes, deadline: float) -> tuple[int, bytes]:
        _deadline.at = deadline
        try:
            if self._proxied:
                return self._send_urllib(path, body, deadline)
            held: tuple[int, http.client.HTTPConnection] | None = getattr(self._local, "conn", None)
            if held is not None:
                self._local.conn = None
                pid, conn = held
                if pid != os.getpid():
                    conn.close()  # made before fork(): this process's copy of the parent's socket
                else:
                    try:
                        return self._exchange(conn, path, body, deadline)
                    except (ConnectionError, http.client.BadStatusLine):
                        pass  # the server closed the idle connection; send once more on a new one
            return self._exchange(self._connect(), path, body, deadline)
        finally:
            _deadline.at = None

    def _connect(self) -> http.client.HTTPConnection:
        host, port = self._parts.hostname or "", self._parts.port
        if self._parts.scheme == "https":
            return _HTTPSConnection(host, port, context=self._ssl)
        return _HTTPConnection(host, port)

    def _exchange(self, conn: http.client.HTTPConnection, path: str, body: bytes, deadline: float) -> tuple[int, bytes]:
        try:
            sock = conn.sock
            if sock is None:
                conn.timeout = _left(deadline)
                conn.connect()
                sock = conn.sock
            sock.settimeout(_left(deadline))
            conn.request("POST", self._prefix + path, body=body, headers=self.headers)
            response = conn.getresponse()  # read under the deadline, by _Response
            raw = self._read(response)
        except BaseException:
            conn.close()
            raise
        if response.will_close:
            conn.close()
        else:
            self._local.conn = (os.getpid(), conn)
        return response.status, raw

    def _send_urllib(self, path: str, body: bytes, deadline: float) -> tuple[int, bytes]:
        handlers: list[Any] = [_NoRedirect(), _HTTPHandler()]
        if self._ssl is not None:
            handlers.append(_HTTPSHandler(self._ssl))
        opener = urllib.request.build_opener(*handlers)
        # The scheme is restricted to http(s) in __init__, so this cannot open file:// or others.
        request = urllib.request.Request(self.url + path, data=body, headers=self.headers, method="POST")  # noqa: S310
        try:
            with opener.open(request, timeout=_left(deadline)) as response:
                return response.status, self._read(response)
        except urllib.error.HTTPError as exc:
            exc.close()
            return exc.code, b""  # only a 200 answer's body is read
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError(str(exc.reason)) from exc
            raise
