"""OpenHost hybrid gate-proxy for FitPub.

Sits between the OpenHost router and the services bundled in this
container. FitPub is a *federated* app with its own JWT account
system, so — unlike openhost-vscode's auto-login proxy — most traffic
must pass through untouched: remote fediverse servers hit webfinger /
nodeinfo / actor / inbox endpoints anonymously, and human visitors use
FitPub's own login. The manifest therefore declares
``public_paths = ["/"]`` and this proxy enforces the *owner-only*
carve-outs itself:

  * ``/admin``     → forwarded to FitPub only when the OpenHost router
                     stamped ``X-OpenHost-Is-Owner: true`` (FitPub's own
                     ADMIN-role JWT check still applies behind it —
                     this is defense in depth plus surface-hiding).
  * ``/actuator``  → owner-only; the proxy injects the generated HTTP
                     Basic credentials so the owner can open
                     /actuator/health in a browser without ever seeing
                     the password.
  * ``/mailpit``   → owner-only; routed to the bundled MailPit UI
                     (registration verification codes land here).
  * ``/api/debug`` → 404 for everyone. FitPub gates this prefix to
                     loopback ``getRemoteAddr()``, but its prod profile
                     trusts ``X-Forwarded-For`` — denying at the edge
                     removes any header-spoofing path. The intended
                     workflow (curl from inside the container, straight
                     to 127.0.0.1:8081) still works because that route
                     bypasses this proxy entirely.
  * ``/healthz``   → answered by the proxy: it probes FitPub's
                     Basic-Auth'd ``/actuator/health`` with the
                     generated credentials and maps the result to a
                     bare 200/503, so the OpenHost router can health
                     check without credentials.

Everything else — federation endpoints, web UI, JWT APIs — is a plain
pass-through to FitPub on 127.0.0.1:8081.

FitPub itself uses no WebSockets (notifications are Web Push), but
MailPit's UI streams updates over one, so the bidirectional WS
forwarding from openhost-vscode/auth_proxy.py is retained and routed
by the same prefix rules.

Defense in depth: ALWAYS strip client-supplied ``X-OpenHost-Is-Owner``
/ ``X-OpenHost-User`` before forwarding upstream, and default
``X-Forwarded-Proto`` / ``X-Forwarded-Host`` when the router didn't
set them (FitPub's prod profile builds absolute URLs from forwarded
headers).

CRITICAL: the original ``Host`` header is forwarded verbatim (never
rewritten to the loopback upstream address). ActivityPub HTTP
Signatures sign the ``host`` header, and FitPub's
HttpSignatureValidator rebuilds the signing string from the raw
request headers — a rewritten Host makes every signed inbox delivery
(Accept, Create, ...) fail verification with 401, which presents as
"following works but no workouts ever arrive".

Implementation adapted from openhost-vscode/auth_proxy.py.
"""

from __future__ import annotations

import base64
import http.client
import logging
import os
import selectors
import socket
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import AbstractSet, Iterable

OWNER_HEADER_NAME = "X-OpenHost-Is-Owner"
USER_HEADER_NAME = "X-OpenHost-User"

# Prefixes (segment-aware) that require the router's owner stamp.
OWNER_ONLY_PREFIXES = ("/admin", "/actuator", "/mailpit")
# Prefixes hidden from everyone (see module docstring).
DENIED_PREFIXES = ("/api/debug",)
# Prefixes routed to MailPit instead of FitPub.
MAILPIT_PREFIXES = ("/mailpit",)

HEALTHZ_PATH = "/healthz"
FITPUB_HEALTH_PATH = "/actuator/health"

HOP_BY_HOP_HEADERS = frozenset(
    h.lower()
    for h in (
        "Connection",
        "Keep-Alive",
        "Proxy-Authenticate",
        "Proxy-Authorization",
        "TE",
        "Trailer",
        "Transfer-Encoding",
        "Upgrade",
        "Host",
        "Content-Length",
    )
)

ALWAYS_STRIP_HEADERS = frozenset(
    h.lower() for h in (OWNER_HEADER_NAME, USER_HEADER_NAME)
)

CLIENT_READ_TIMEOUT_SECONDS = 60

# 256 MiB body cap. FitPub's default per-file upload limit is 50 MB
# (FITPUB_FILE_UPLOAD_MAX_SIZE) and batch imports send several files
# per multipart request; 256 MiB leaves generous headroom.
MAX_BODY_BYTES = 256 * 1024 * 1024

STREAM_CHUNK_BYTES = 64 * 1024
STREAM_TIMEOUT_SECONDS = 6 * 60 * 60
HEADER_LINE_CAP = 64 * 1024

logging.basicConfig(
    level=os.environ.get("AUTH_PROXY_LOG_LEVEL", "INFO"),
    format="[auth-proxy] %(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("auth_proxy")


def _parse_hostport(value: str, default_port: int) -> tuple[str, int]:
    host, _, port = value.partition(":")
    return host or "127.0.0.1", int(port) if port else default_port


def _strip_headers(
    headers: Iterable[tuple[str, str]], drop: AbstractSet[str]
) -> list[tuple[str, str]]:
    drop_lower = {h.lower() for h in drop}
    return [(k, v) for k, v in headers if k.lower() not in drop_lower]


def _matches_prefix(path: str, prefixes: Iterable[str]) -> bool:
    """Segment-aware prefix match: /admin matches /admin and /admin/x,
    but not /administrators."""
    for prefix in prefixes:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


class GateProxyHandler(BaseHTTPRequestHandler):
    fitpub_host: str = "127.0.0.1"
    fitpub_port: int = 8081
    mailpit_host: str = "127.0.0.1"
    mailpit_port: int = 8025
    actuator_auth: str | None = None  # pre-encoded "Basic ..." value
    forwarded_host: str = ""
    forwarded_proto: str = "https"

    def log_message(self, format: str, *args) -> None:  # noqa: A002, N802
        # Suppress noisy health-probe log lines.
        path = getattr(self, "path", "")
        if path.startswith(HEALTHZ_PATH):
            return
        log.info("%s - " + format, self.address_string(), *args)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch()

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch()

    def _safe_send_error(self, code: int, message: str) -> None:
        try:
            self.send_error(code, message)
        except OSError as exc:
            log.debug("client disconnected before error response: %s", exc)

    # -------------------------------------------------------------
    # Routing
    # -------------------------------------------------------------

    def _dispatch(self) -> None:
        try:
            self.connection.settimeout(CLIENT_READ_TIMEOUT_SECONDS)
        except OSError:
            pass

        path = urllib.parse.urlsplit(self.path).path

        if path == HEALTHZ_PATH:
            self._handle_healthz()
            return

        if _matches_prefix(path, DENIED_PREFIXES):
            # 404 (not 403): don't advertise that the prefix exists.
            self._safe_send_error(404, "Not Found")
            return

        is_owner = self.headers.get(OWNER_HEADER_NAME, "").lower() == "true"
        if _matches_prefix(path, OWNER_ONLY_PREFIXES) and not is_owner:
            log.info("denied non-owner request to %s", path)
            self._safe_send_error(403, "Owner access required")
            return

        if _matches_prefix(path, MAILPIT_PREFIXES):
            upstream = (self.mailpit_host, self.mailpit_port)
            extra_headers: list[tuple[str, str]] = []
        else:
            upstream = (self.fitpub_host, self.fitpub_port)
            extra_headers = []
            # Let the owner open /actuator/* in a browser: inject the
            # generated Basic credentials when none were supplied.
            if (
                _matches_prefix(path, ("/actuator",))
                and self.actuator_auth
                and not self.headers.get("Authorization")
            ):
                extra_headers.append(("Authorization", self.actuator_auth))

        if self._is_websocket_upgrade():
            self._proxy_websocket(upstream)
            return

        self._proxy(upstream, extra_headers)

    def _probe_fitpub(self, path: str, headers: dict[str, str]) -> int | None:
        try:
            conn = http.client.HTTPConnection(
                self.fitpub_host, self.fitpub_port, timeout=5
            )
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            resp.read(4096)
            conn.close()
            return resp.status
        except (OSError, http.client.HTTPException):
            return None

    def _handle_healthz(self) -> None:
        """Probe FitPub and answer bare 200/503 so the router needs no
        credentials.

        Preferred probe is the Basic-Auth'd /actuator/health — but the
        current FitPub *release* image predates the actuator security
        chain (it JWT-protects /actuator/** like everything else), so
        on 401/403/3xx we fall back to fetching the public homepage.
        When FitPub ships the actuator chain, the first probe simply
        starts winning.
        """
        status, body = 503, b'{"status":"DOWN"}'
        headers = {"Accept": "application/json"}
        if self.actuator_auth:
            headers["Authorization"] = self.actuator_auth
        probe = self._probe_fitpub(FITPUB_HEALTH_PATH, headers)
        if probe is not None and probe != 200:
            # Any 2xx/3xx counts: the release 302s "/" to /timeline.
            probe = self._probe_fitpub("/", {"Accept": "text/html"})
        if probe is not None and 200 <= probe < 400:
            status, body = 200, b'{"status":"UP"}'
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError as exc:
            log.debug("client disconnected during healthz: %s", exc)

    STARTING_HTML = (
        b"<!DOCTYPE html><html><head><title>FitPub is starting"
        b"&hellip;</title><meta http-equiv=\"refresh\" content=\"10\">"
        b"<style>body{font-family:sans-serif;display:flex;align-items:center;"
        b"justify-content:center;height:100vh;margin:0}div{text-align:center}"
        b"</style></head><body><div><h1>FitPub is starting&hellip;</h1>"
        b"<p>Database migrations can take a few minutes after an update."
        b"<br>This page refreshes automatically.</p></div></body></html>"
    )

    def _send_starting_page(self) -> None:
        wants_html = "text/html" in self.headers.get("Accept", "").lower()
        body = (
            self.STARTING_HTML
            if wants_html
            else b'{"status":"starting","retry_after":10}'
        )
        try:
            self.send_response(503, "Service Starting")
            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8" if wants_html else "application/json",
            )
            self.send_header("Retry-After", "10")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError as exc:
            log.debug("client disconnected during starting page: %s", exc)

    # -------------------------------------------------------------
    # Forwarded-header hygiene
    # -------------------------------------------------------------

    def _original_host(self) -> str:
        return self.headers.get("Host", "").strip()

    def _forward_headers(
        self, extra: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        cleaned = _strip_headers(
            self.headers.items(), HOP_BY_HOP_HEADERS | ALWAYS_STRIP_HEADERS
        )
        # Re-add the ORIGINAL Host (it is in HOP_BY_HOP_HEADERS so the
        # generic strip removed it) — HTTP signature verification needs
        # the exact value the sender signed. _proxy() uses
        # skip_host=True so http.client won't add a competing one.
        original_host = self._original_host()
        if original_host:
            cleaned.append(("Host", original_host))
        have = {k.lower() for k, _ in cleaned}
        # TLS terminates at OpenHost's edge; make sure FitPub sees the
        # right scheme + public host even if the router didn't say so.
        if "x-forwarded-proto" not in have:
            cleaned.append(("X-Forwarded-Proto", self.forwarded_proto))
        if "x-forwarded-host" not in have and (original_host or self.forwarded_host):
            cleaned.append(("X-Forwarded-Host", original_host or self.forwarded_host))
        cleaned.extend(extra)
        return cleaned

    # -------------------------------------------------------------
    # WebSocket pass-through (used by the MailPit UI's live stream)
    # -------------------------------------------------------------

    def _is_websocket_upgrade(self) -> bool:
        upgrade = self.headers.get("Upgrade", "").lower().strip()
        connection = self.headers.get("Connection", "").lower()
        connection_tokens = {t.strip() for t in connection.split(",")}
        return upgrade == "websocket" and "upgrade" in connection_tokens

    def _proxy_websocket(self, upstream: tuple[str, int]) -> None:
        upstream_host, upstream_port = upstream
        ws_drop = HOP_BY_HOP_HEADERS | ALWAYS_STRIP_HEADERS
        # Upgrade/Connection are hop-by-hop but must be re-sent for the
        # handshake; re-add them explicitly.
        cleaned = _strip_headers(self.headers.items(), ws_drop)
        cleaned.append(("Connection", "Upgrade"))
        cleaned.append(("Upgrade", "websocket"))
        original_host = self._original_host() or self.headers.get(
            "X-Forwarded-Host", ""
        ).strip()

        try:
            upstream_sock = socket.create_connection(
                (upstream_host, upstream_port), timeout=STREAM_TIMEOUT_SECONDS
            )
        except OSError as exc:
            log.warning("upstream connect failed (websocket): %s", exc)
            self._safe_send_error(502, "Bad Gateway")
            return

        try:
            upstream_sock.settimeout(STREAM_TIMEOUT_SECONDS)
            host_header = original_host or f"{upstream_host}:{upstream_port}"
            request_bytes = bytearray()
            request_bytes.extend(
                self._encode_header_bytes(f"{self.command} {self.path} HTTP/1.1\r\n")
            )
            request_bytes.extend(
                self._encode_header_bytes(f"Host: {host_header}\r\n")
            )
            for k, v in cleaned:
                request_bytes.extend(self._encode_header_bytes(f"{k}: {v}\r\n"))
            request_bytes.extend(b"\r\n")
            try:
                upstream_sock.sendall(bytes(request_bytes))
            except OSError as exc:
                log.warning("websocket request send failed: %s", exc)
                self._safe_send_error(502, "Bad Gateway")
                return

            response_buf = self._read_until_double_crlf(
                upstream_sock, max_bytes=HEADER_LINE_CAP
            )
            if response_buf is None:
                self._safe_send_error(502, "Bad Gateway")
                return
            head_bytes, tail_bytes = response_buf

            try:
                self.wfile.write(head_bytes)
                if tail_bytes:
                    self.wfile.write(tail_bytes)
                self.wfile.flush()
            except OSError as exc:
                log.debug("client disconnected during ws handshake: %s", exc)
                return

            if not head_bytes.startswith(b"HTTP/1.1 101"):
                first_line = head_bytes.split(b"\r\n", 1)[0].decode(
                    "latin-1", errors="replace"
                )
                log.info("upstream rejected websocket upgrade: %s", first_line)
                return

            self._websocket_pump(self.connection, upstream_sock)
        finally:
            try:
                upstream_sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                upstream_sock.close()
            except OSError:
                pass

    @staticmethod
    def _read_until_double_crlf(
        sock: socket.socket, max_bytes: int
    ) -> tuple[bytes, bytes] | None:
        buf = bytearray()
        while True:
            try:
                chunk = sock.recv(4096)
            except OSError as exc:
                log.info("websocket handshake recv failed: %s", exc)
                return None
            if not chunk:
                return None
            buf.extend(chunk)
            idx = buf.find(b"\r\n\r\n")
            if idx >= 0:
                head = bytes(buf[: idx + 4])
                tail = bytes(buf[idx + 4 :])
                return head, tail
            if len(buf) >= max_bytes:
                log.warning(
                    "websocket response head exceeds %d bytes; aborting", max_bytes
                )
                return None

    @staticmethod
    def _websocket_pump(
        client_sock: socket.socket, upstream_sock: socket.socket
    ) -> None:
        for s in (client_sock, upstream_sock):
            try:
                s.settimeout(None)
            except OSError:
                pass

        sel = selectors.DefaultSelector()
        try:
            sel.register(client_sock, selectors.EVENT_READ, "client")
            sel.register(upstream_sock, selectors.EVENT_READ, "upstream")
            while True:
                events = sel.select(timeout=STREAM_TIMEOUT_SECONDS)
                if not events:
                    log.info("websocket idle timeout; closing")
                    return
                for key, _ in events:
                    if key.data == "client":
                        src, dst = client_sock, upstream_sock
                        direction = "client->upstream"
                    else:
                        src, dst = upstream_sock, client_sock
                        direction = "upstream->client"
                    try:
                        chunk = src.recv(STREAM_CHUNK_BYTES)
                    except OSError as exc:
                        log.info("websocket %s recv failed: %s", direction, exc)
                        return
                    if not chunk:
                        log.debug("websocket %s EOF; closing", direction)
                        return
                    try:
                        dst.sendall(chunk)
                    except OSError as exc:
                        log.info("websocket %s sendall failed: %s", direction, exc)
                        return
        finally:
            try:
                sel.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("websocket selector close failed: %s", exc)

    @staticmethod
    def _encode_header_bytes(value: str) -> bytes:
        try:
            return value.encode("latin-1")
        except UnicodeEncodeError:
            log.warning("non-latin-1 header value, replacing offending bytes")
            return value.encode("latin-1", errors="replace")

    # -------------------------------------------------------------
    # Plain HTTP forwarding
    # -------------------------------------------------------------

    def _proxy(
        self, upstream: tuple[str, int], extra_headers: list[tuple[str, str]]
    ) -> None:
        upstream_host, upstream_port = upstream
        cleaned_headers = self._forward_headers(extra_headers)

        transfer_encoding = self.headers.get("Transfer-Encoding", "").lower().strip()
        if transfer_encoding and transfer_encoding != "identity":
            self._safe_send_error(501, "Transfer-Encoding not supported")
            return

        body: bytes | None = None
        content_length_header = self.headers.get("Content-Length")
        if content_length_header:
            try:
                length = int(content_length_header)
            except ValueError:
                self._safe_send_error(400, "invalid Content-Length")
                return
            if length < 0:
                self._safe_send_error(400, "negative Content-Length")
                return
            if length > MAX_BODY_BYTES:
                self._safe_send_error(413, "request body too large")
                return
            if length > 0:
                try:
                    body = self.rfile.read(length)
                except (OSError, TimeoutError) as exc:
                    log.info("client read error: %s", exc)
                    self._safe_send_error(400, "request body read failed")
                    return
                if len(body) != length:
                    self._safe_send_error(400, "incomplete request body")
                    return
            else:
                body = b""
        elif self.command in ("POST", "PUT", "PATCH", "DELETE"):
            body = b""

        conn = http.client.HTTPConnection(upstream_host, upstream_port, timeout=120)
        try:
            try:
                # skip_host: the original Host header travels in
                # cleaned_headers (signature verification needs it).
                conn.putrequest(
                    self.command, self.path, skip_host=True, skip_accept_encoding=True
                )
                for key, value in cleaned_headers:
                    conn.putheader(key, value)
                if body is not None:
                    conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(message_body=body)
                upstream_resp = conn.getresponse()
            except ConnectionRefusedError:
                # FitPub's JVM isn't listening yet — the first boot
                # after an image update runs Flyway migrations for
                # several minutes. Tell humans and machines to retry
                # rather than presenting a dead-looking 502.
                self._send_starting_page()
                return
            except (OSError, http.client.HTTPException) as exc:
                log.warning("upstream error: %s", exc)
                self._safe_send_error(502, "Bad Gateway")
                return

            try:
                payload = upstream_resp.read(MAX_BODY_BYTES + 1)
            except (OSError, http.client.HTTPException) as exc:
                log.warning("upstream read error: %s", exc)
                self._safe_send_error(502, "Bad Gateway")
                try:
                    upstream_resp.close()
                except Exception as close_exc:  # noqa: BLE001
                    log.debug("upstream.close() raised: %s", close_exc)
                return
            try:
                upstream_resp.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("upstream.close() raised (ignored): %s", exc)
            if len(payload) > MAX_BODY_BYTES:
                self._safe_send_error(502, "upstream response too large")
                return

            reason = upstream_resp.reason or ""
            try:
                self.send_response(upstream_resp.status, reason)
                for key, value in upstream_resp.getheaders():
                    if key.lower() in HOP_BY_HOP_HEADERS:
                        continue
                    self.send_header(key, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
            except OSError as exc:
                log.debug("client disconnected mid-response: %s", exc)
        finally:
            conn.close()


class IPv4ThreadingServer(ThreadingHTTPServer):
    address_family = socket.AF_INET
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    try:
        listen_port = int(os.environ.get("AUTH_PROXY_LISTEN_PORT", "8080"))
    except ValueError:
        log.error("AUTH_PROXY_LISTEN_PORT is not an integer")
        return 1

    fitpub_host, fitpub_port = _parse_hostport(
        os.environ.get("FITPUB_UPSTREAM", "127.0.0.1:8081"), 8081
    )
    mailpit_host, mailpit_port = _parse_hostport(
        os.environ.get("MAILPIT_UPSTREAM", "127.0.0.1:8025"), 8025
    )

    GateProxyHandler.fitpub_host = fitpub_host
    GateProxyHandler.fitpub_port = fitpub_port
    GateProxyHandler.mailpit_host = mailpit_host
    GateProxyHandler.mailpit_port = mailpit_port
    GateProxyHandler.forwarded_host = os.environ.get("FORWARDED_HOST", "").strip()
    GateProxyHandler.forwarded_proto = (
        os.environ.get("FORWARDED_PROTO", "https").strip() or "https"
    )

    actuator_user = os.environ.get("ACTUATOR_USERNAME", "").strip()
    actuator_password = os.environ.get("ACTUATOR_PASSWORD", "")
    if actuator_user and actuator_password:
        token = base64.b64encode(
            f"{actuator_user}:{actuator_password}".encode()
        ).decode("ascii")
        GateProxyHandler.actuator_auth = f"Basic {token}"
    else:
        log.warning("no actuator credentials; /healthz and /actuator disabled")

    try:
        server = IPv4ThreadingServer(("0.0.0.0", listen_port), GateProxyHandler)
    except OSError as exc:
        log.error("failed to bind 0.0.0.0:%d: %s", listen_port, exc)
        return 1
    log.info(
        "listening on 0.0.0.0:%d -> fitpub %s:%d, mailpit %s:%d",
        listen_port,
        fitpub_host,
        fitpub_port,
        mailpit_host,
        mailpit_port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
