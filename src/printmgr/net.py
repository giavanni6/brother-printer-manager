"""Tiny HTTP helper. Redirects are NOT followed automatically — callers decide.

Every failure surfaces as an OSError subclass (http.client.HTTPException is not one), and
`timeout` bounds the WHOLE request — connect, headers and body — so a device that dribbles
bytes cannot stall a caller. (DNS is not bounded; the app addresses the printer by IP.)
"""

from __future__ import annotations

import http.client
import socket
import ssl
import threading
import time
from typing import NamedTuple
from urllib.parse import urlsplit

# The printer serves HTTPS with a self-signed certificate and we address it by LAN IP,
# so certificate verification cannot succeed. This context is only ever used for the printer.
_PRINTER_TLS = ssl.create_default_context()
_PRINTER_TLS.check_hostname = False
_PRINTER_TLS.verify_mode = ssl.CERT_NONE

MAX_BODY = 64 * 1024 * 1024  # a 600 dpi color letter page is ~10 MB


class Response(NamedTuple):
    status: int
    headers: dict[str, str]  # lower-cased names
    body: bytes


def _has_body(method: str, status: int) -> bool:
    return method != "HEAD" and status not in (204, 304) and not 100 <= status < 200


def request(
    method: str,
    url: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> Response:
    deadline = time.monotonic() + timeout
    expired = threading.Event()
    conn: http.client.HTTPConnection | None = None

    def abort() -> None:
        # Watchdog for the phases a per-read timeout cannot bound (slow headers): shutting the
        # socket down from here makes the blocked read in the caller's thread fail at once.
        expired.set()
        sock = conn.sock if conn is not None else None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    watchdog = threading.Timer(timeout, abort)
    watchdog.daemon = True
    try:
        try:
            parts = urlsplit(url)
            host = parts.hostname or ""
            port = parts.port  # ValueError on a malformed port
            if parts.scheme == "https":
                conn = http.client.HTTPSConnection(host, port or 443, timeout=timeout, context=_PRINTER_TLS)
            else:
                conn = http.client.HTTPConnection(host, port or 80, timeout=timeout)
        except (ValueError, http.client.InvalidURL) as e:
            raise ConnectionError(f"bad URL {url!r}: {e}") from None
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        watchdog.start()
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        chunks: list[bytes] = []
        size = 0
        while _has_body(method, resp.status):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{method} {url} exceeded {timeout:g}s")
            if conn.sock is not None:
                conn.sock.settimeout(remaining)
            chunk = resp.read1(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_BODY:
                raise ConnectionError(f"{method} {url}: response larger than {MAX_BODY} bytes")
            chunks.append(chunk)
        expected = resp_headers.get("content-length", "")
        # read1() returns b"" on an early close instead of raising: catch a truncated body here,
        # or a scan cut off by a Wi-Fi drop would be saved as a corrupt image.
        if _has_body(method, resp.status) and expected.isdigit() and int(expected) != size:
            raise ConnectionError(f"{method} {url}: connection closed after {size} of {expected} bytes")
        return Response(resp.status, resp_headers, b"".join(chunks))
    except http.client.HTTPException as e:  # IncompleteRead, BadStatusLine, ...
        if expired.is_set():
            raise TimeoutError(f"{method} {url} exceeded {timeout:g}s") from None
        raise ConnectionError(f"{method} {url}: {type(e).__name__}: {e}") from None
    except OSError:
        if expired.is_set():
            raise TimeoutError(f"{method} {url} exceeded {timeout:g}s") from None
        raise
    finally:
        watchdog.cancel()
        if conn is not None:
            conn.close()
