"""Bounded, direct HTTPS fetching with DNS pinning and redirect validation."""
from __future__ import annotations

import http.client
import ipaddress
import json
import math
import queue
import socket
import ssl
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


class NetworkError(RuntimeError):
    """A deliberately URL-free network failure, safe to show in the UI."""


class DiscoveryCancelled(RuntimeError):
    """The user cancelled discovery or a download."""


class RetryDeferred(NetworkError):
    """A server retry interval exceeds the bounded automatic-wait budget."""

    def __init__(self, host, retry_at, status):
        super().__init__(
            f"Remote server returned HTTP {status}; Retry-After exceeds the automatic wait budget. "
            "Retry explicitly later."
        )
        self.host, self.retry_at = host, retry_at


class _TransientNetworkError(NetworkError):
    pass


def _check_cancelled(cancelled):
    if cancelled and cancelled():
        raise DiscoveryCancelled("Operation cancelled.")


def _public_ip(address):
    ip = ipaddress.ip_address(address)
    return ip.is_global and not (
        ip.is_multicast or ip.is_unspecified or ip.is_reserved
        or getattr(ip, "ipv4_mapped", None) is not None
        or (ip.version == 6 and any(ip in network for network in (
            ipaddress.ip_network("64:ff9b::/96"),
            ipaddress.ip_network("64:ff9b:1::/48"),
            ipaddress.ip_network("2001::/32"),
            ipaddress.ip_network("2002::/16"),
        )))
    )


def _validated_host(url):
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https" or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.port not in (None, 443)
            or "\\" in url or any(ord(c) < 33 or ord(c) == 127 for c in url)
        ):
            raise ValueError
        host = parts.hostname.rstrip(".").encode("idna").decode("ascii")
        if "%" in host or host == "localhost" or host.endswith(".localhost"):
            raise ValueError
        return parts, host
    except (ValueError, UnicodeError):
        raise NetworkError("Only public HTTPS URLs on port 443 are allowed.") from None


def _resolve(host, deadline, cancelled):
    # A daemon prevents a stuck system resolver from blocking cancellation/shutdown.
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        except OSError:
            result.put(None)

    threading.Thread(target=resolve, daemon=True).start()
    while True:
        _check_cancelled(cancelled)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NetworkError("DNS resolution timed out.")
        try:
            addresses = result.get(timeout=min(0.1, remaining))
            break
        except queue.Empty:
            pass
    if not addresses:
        raise NetworkError("DNS resolution failed.")
    try:
        if any(not _public_ip(item[4][0]) for item in addresses):
            raise NetworkError("The destination resolves to a non-public address.")
    except ValueError:
        raise NetworkError("Invalid destination address.") from None
    return addresses[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self):
        family, socktype, proto, _, sockaddr = self._address
        raw = socket.socket(family, socktype, proto)
        try:
            raw.settimeout(self.timeout)
            raw.connect(sockaddr)
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _request_once(url, max_bytes, timeout, headers, cancelled):
    parts, host = _validated_host(url)
    deadline = time.monotonic() + timeout
    address = _resolve(host, deadline, cancelled)
    _check_cancelled(cancelled)
    connection = _PinnedHTTPSConnection(
        host, address, min(2.0 if cancelled else timeout, max(0.01, deadline - time.monotonic()))
    )
    try:
        path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        if response.status != 200:
            return response.status, response_headers, b""
        length = response_headers.get("content-length")
        if length:
            try:
                if int(length) < 0 or int(length) > max_bytes:
                    raise NetworkError("Response exceeds the size limit.")
            except ValueError:
                raise NetworkError("Invalid response size.") from None
        if response_headers.get("content-encoding", "identity").lower() not in ("identity", ""):
            raise NetworkError("Compressed responses are not supported.")
        chunks, size = [], 0
        while True:
            _check_cancelled(cancelled)
            if time.monotonic() >= deadline:
                raise NetworkError("Response timed out.")
            chunk = response.read1(min(65536, max_bytes - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise NetworkError("Response exceeds the size limit.")
            chunks.append(chunk)
        _check_cancelled(cancelled)
        if length and size != int(length):
            raise NetworkError("The response was truncated.")
        return response.status, response_headers, b"".join(chunks)
    except (OSError, http.client.HTTPException, ValueError):
        _check_cancelled(cancelled)
        raise _TransientNetworkError("HTTPS request failed or timed out.") from None
    finally:
        connection.close()


def _backoff(value, attempt):
    delay = 0.5 * (2 ** attempt)
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                delay = parsedate_to_datetime(value).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                pass
    return max(0.0, min(5.0, delay))


def _pause(seconds, cancelled):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        _check_cancelled(cancelled)
        time.sleep(min(0.1, max(0, until - time.monotonic())))
    _check_cancelled(cancelled)


def safe_fetch(
    url: str,
    max_bytes: int,
    timeout: float = 20,
    headers: dict | None = None,
    cancelled: Callable[[], bool] | None = None,
    strict_retry_after: bool = False,
    on_retry: Callable[[str], None] | None = None,
    before_request: Callable[[str], None] | None = None,
) -> tuple[bytes, str, str]:
    """Fetch public HTTPS only; limits apply even without Content-Length.

    All DNS answers must be public. Each connection pins a validated address
    while certificate verification and SNI retain the original hostname.
    Environment proxies are never consulted. At most five redirects and two
    transient HTTP retries are followed. Errors never include URLs or secrets.

    ``timeout`` is a per-request budget, not an overall retry/redirect deadline.
    DNS waits honor that budget and poll cancellation every 100 ms; an OS DNS
    lookup itself cannot be forcibly stopped and may finish on its daemon thread
    after the caller returns. Socket operations use at most a two-second timeout
    when a cancellation callback is supplied. Retry delays are capped at five
    seconds and also poll cancellation every 100 ms.

    Opt-in library callers use ``strict_retry_after`` to defer rather than retry
    earlier than a longer server interval, ``before_request`` to enforce persisted
    host cooldowns on every redirect, and ``on_retry`` to retain retry notices.
    """
    if not isinstance(max_bytes, int) or max_bytes <= 0 or timeout <= 0:
        raise ValueError("Positive max_bytes and timeout are required.")
    _validated_host(url)
    request_headers = {"User-Agent": "BunaScreening/1.0", "Accept-Encoding": "identity"}
    for key, value in (headers or {}).items():
        if key.lower() not in {"host", "connection", "accept-encoding", "proxy-authorization"}:
            request_headers[key] = value
    redirects, retries = 0, 0
    while True:
        _check_cancelled(cancelled)
        if before_request:
            before_request(url)
        try:
            status, response_headers, data = _request_once(
                url, max_bytes, timeout, request_headers, cancelled
            )
        except _TransientNetworkError:
            if retries >= 2:
                raise
            if on_retry:
                on_retry("HTTPS request failed or timed out; retrying.")
            _pause(_backoff(None, retries), cancelled)
            retries += 1
            continue
        if status == 200:
            return data, response_headers.get("content-type", ""), url
        if status in {301, 302, 303, 307, 308}:
            if redirects >= 5 or not response_headers.get("location"):
                raise NetworkError("Redirect limit reached or invalid redirect.")
            next_url = urljoin(url, response_headers["location"])
            _, next_host = _validated_host(next_url)
            _, host = _validated_host(url)
            if next_host != host:
                request_headers = {
                    key: value for key, value in request_headers.items()
                    if key.lower() in {"accept", "user-agent", "accept-encoding"}
                }
                parts = urlsplit(next_url)
                # Never forward credentials encoded in a redirect query.
                query = urlencode([
                    (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                    if key.lower() not in {
                        "api_key", "apikey", "key", "token", "access_token", "secret",
                        "authorization", "password",
                    }
                ])
                next_url = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
            url, redirects = next_url, redirects + 1
            continue
        if status in {429, 500, 502, 503, 504}:
            if strict_retry_after and response_headers.get("retry-after"):
                value = response_headers["retry-after"]
                try:
                    delay = float(value)
                except ValueError:
                    try:
                        delay = parsedate_to_datetime(value).timestamp() - time.time()
                    except (ValueError, TypeError, OverflowError):
                        delay = 0
                if math.isfinite(delay) and delay > 5:
                    raise RetryDeferred(urlsplit(url).hostname, time.time() + delay, status)
            if retries < 2:
                if on_retry:
                    on_retry(f"Remote server returned HTTP {status}; retrying after backoff.")
                _pause(_backoff(response_headers.get("retry-after"), retries), cancelled)
                retries += 1
                continue
        raise NetworkError(f"Remote server returned HTTP {status}.")


def safe_json(url: str, max_bytes: int = 8_000_000, **kwargs):
    """Fetch a bounded JSON document using the same SSRF protections."""
    data, _, _ = safe_fetch(url, max_bytes=max_bytes, **kwargs)
    try:
        return json.loads(data)
    except (ValueError, UnicodeError):
        raise NetworkError("Remote server returned invalid JSON.") from None
