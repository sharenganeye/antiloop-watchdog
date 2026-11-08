"""Local HTTP API.

Security model: loopback bind only (enforced in service.py), bearer-token authentication on
everything except GET /health, Host-header allow-list (DNS-rebinding protection), JSON-only
bodies with a size cap, strict input validation, and no endpoint that can stop the gateway,
llama.cpp or any process.
"""
from __future__ import annotations

import hmac
import json
import re
import socket
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit

from .config import VERSION
from .schema import (WatchdogError, ValidationError, validate_session_id)

SID = r"([^/]+)"


def _q_int(query: dict, key: str, default: int, lo: int, hi: int) -> int:
    raw = query.get(key, [None])[0]
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ValidationError(f"query parameter '{key}' must be an integer")
    if v < lo or v > hi:
        raise ValidationError(f"query parameter '{key}' must be in [{lo}, {hi}]")
    return v


def _need_obj(body) -> dict:
    if not isinstance(body, dict):
        raise ValidationError("a JSON object body is required")
    return body


def _sid(raw: str, allow_reserved: bool = True) -> str:
    return validate_session_id(unquote(raw), allow_reserved=allow_reserved)


# --------------------------------------------------------------------------- handlers
# each handler: (service, path_groups, query, body) -> (status, payload)

def h_health(svc, g, q, b):
    return 200, {"status": "ok", "service": "antiloop-watchdog", "version": VERSION,
                 "uptime_s": round(svc.engine.clock() - svc.engine.started, 1)}


def h_status(svc, g, q, b):
    return 200, svc.engine.status()


def h_metrics(svc, g, q, b):
    return 200, svc.engine.metrics()


def h_sessions(svc, g, q, b):
    return 200, {"sessions": svc.engine.list_sessions()}


def h_session(svc, g, q, b):
    return 200, svc.engine.session_detail(_sid(g[0]))


def h_events_get(svc, g, q, b):
    since = _q_int(q, "since_id", 0, 0, 2 ** 62)
    limit = _q_int(q, "limit", 100, 1, 1000)
    sid = q.get("session_id", [None])[0]
    if sid is not None:
        validate_session_id(sid, allow_reserved=True)
    etype = q.get("event_type", [None])[0]
    return 200, svc.engine.events_since(since, sid, etype, limit)


def h_events_post(svc, g, q, b):
    max_batch = svc.cm.data["api"]["max_batch_events"]
    if isinstance(b, dict) and "events" in b:
        events = b["events"]
        if not isinstance(events, list) or not events or len(events) > max_batch:
            raise ValidationError(f"'events' must be a list of 1..{max_batch} events")
        return 200, svc.engine.ingest_batch(events)
    res = svc.engine.ingest(_need_obj(b))
    return 200, res


def h_config_get(svc, g, q, b):
    return 200, {"config": svc.cm.public(), "mutable_keys": svc.cm.mutable_keys()}


def h_config_post(svc, g, q, b):
    new = svc.cm.patch(_need_obj(b))
    with svc.engine.lock:
        svc.engine.emit("config_changed", details={"keys": sorted(b.keys())})
    return 200, {"config": new}


def h_mode(svc, g, q, b):
    body = _need_obj(b)
    if "mode" not in body:
        raise ValidationError("'mode' is required")
    return 200, svc.engine.set_mode(_sid(g[0]), body["mode"])


def h_reset(svc, g, q, b):
    return 200, svc.engine.reset_session(_sid(g[0]))


def h_intervene(svc, g, q, b):
    body = _need_obj(b)
    if "action" not in body:
        raise ValidationError("'action' is required")
    unknown = set(body) - {"action", "message"}
    if unknown:
        raise ValidationError(f"unknown fields: {sorted(unknown)}")
    return 200, {"directive": svc.engine.intervene(_sid(g[0]), body["action"], body.get("message"))}


def h_directives(svc, g, q, b):
    status = q.get("status", [None])[0]
    if status is not None and status not in ("pending", "delivered", "acked", "expired", "superseded"):
        raise ValidationError("invalid status filter")
    peek = q.get("peek", ["0"])[0] in ("1", "true", "yes")
    return 200, {"directives": svc.engine.get_directives(_sid(g[0]), status, peek)}


def h_ack(svc, g, q, b):
    body = _need_obj(b)
    return 200, {"directive": svc.engine.ack_directive(_sid(g[0]), unquote(g[1]), body.get("result", "applied"))}


ROUTES = [
    ("GET", re.compile(r"^/health$"), h_health, False),
    ("GET", re.compile(r"^/status$"), h_status, True),
    ("GET", re.compile(r"^/metrics$"), h_metrics, True),
    ("GET", re.compile(r"^/sessions$"), h_sessions, True),
    ("GET", re.compile(rf"^/sessions/{SID}$"), h_session, True),
    ("GET", re.compile(r"^/events$"), h_events_get, True),
    ("POST", re.compile(r"^/events$"), h_events_post, True),
    ("GET", re.compile(r"^/config$"), h_config_get, True),
    ("POST", re.compile(r"^/config$"), h_config_post, True),
    ("POST", re.compile(rf"^/sessions/{SID}/mode$"), h_mode, True),
    ("POST", re.compile(rf"^/sessions/{SID}/reset$"), h_reset, True),
    ("POST", re.compile(rf"^/sessions/{SID}/intervene$"), h_intervene, True),
    ("GET", re.compile(rf"^/sessions/{SID}/directives$"), h_directives, True),
    ("POST", re.compile(rf"^/sessions/{SID}/directives/([^/]+)/ack$"), h_ack, True),
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AntiLoopWatchdog"
    sys_version = ""
    wbufsize = 65536                  # headers + body leave in one segment ...
    disable_nagle_algorithm = True    # ... and without Nagle/delayed-ACK stalls (~40 ms per request otherwise)

    def log_message(self, *args):  # no stderr noise, no disk writes
        pass

    # ---- plumbing
    def _send(self, status: int, payload, close: bool = False, raw: Optional[str] = None, ctype: str = "application/json") -> None:
        body = (raw if raw is not None else json.dumps(payload, separators=(",", ":"), ensure_ascii=False)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str, close: bool = False) -> None:
        self._send(status, {"error": {"code": code, "message": message}}, close=close)

    def _host_ok(self) -> bool:
        host = self.headers.get("Host")
        if host is None:
            return True
        return host.lower() in self.server.allowed_hosts

    def _authed(self) -> bool:
        token = self.server.service.token.encode()
        got = self.headers.get("X-Watchdog-Token")
        if got is None:
            auth = self.headers.get("Authorization", "")
            got = auth[7:] if auth.lower().startswith("bearer ") else ""
        return hmac.compare_digest(got.encode("utf-8", "replace"), token)

    def _read_body(self):
        length_raw = self.headers.get("Content-Length")
        if length_raw is None:
            return None
        try:
            length = int(length_raw)
        except ValueError:
            raise ValidationError("invalid Content-Length")
        if length < 0:
            raise ValidationError("invalid Content-Length")
        if length > self.server.service.cm.data["api"]["max_body_bytes"]:
            raise WatchdogError("request body too large", "payload_too_large")
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return None
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise WatchdogError("Content-Type must be application/json", "unsupported_media_type")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValidationError("body is not valid UTF-8 JSON", "invalid_json")

    def _route(self, method: str) -> None:
        svc = self.server.service
        try:
            parts = urlsplit(self.path)
            path, query = parts.path, parse_qs(parts.query)
            if not self._host_ok():
                return self._error(403, "forbidden_host", "Host header not allowed", close=True)
            matched = []
            for m, rx, fn, auth in ROUTES:
                mt = rx.match(path)
                if mt:
                    matched.append((m, mt, fn, auth))
            if not matched:
                return self._error(404, "not_found", "unknown endpoint", close=method == "POST")
            for m, mt, fn, auth in matched:
                if m != method:
                    continue
                if auth and not self._authed():
                    return self._error(401, "unauthorized", "missing or invalid token", close=True)
                body = self._read_body() if method == "POST" else None
                status, payload = fn(svc, mt.groups(), query, body)
                if (fn is h_metrics and (query.get("format", [""])[0] == "prometheus"
                                         or "text/plain" in (self.headers.get("Accept") or ""))):
                    return self._send(200, None, raw=svc.engine.prometheus(), ctype="text/plain")
                return self._send(status, payload)
            return self._error(405, "method_not_allowed", f"{method} not allowed here", close=method == "POST")
        except WatchdogError as exc:
            status = {"payload_too_large": 413, "unsupported_media_type": 415}.get(exc.code, exc.status)
            self._error(status, exc.code, exc.message, close=status in (413, 415))
        except Exception:  # never leak internals
            svc.engine.counters["internal_errors"] += 1
            self._error(500, "internal_error", "internal error", close=True)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _not_allowed(self):
        self._error(405, "method_not_allowed", "method not allowed", close=True)

    do_PUT = do_DELETE = do_PATCH = _not_allowed


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128      # the default of 5 turns a hook burst into resets
    handler_timeout = 10.0        # a stalled client must not pin a worker forever
    max_workers = 64              # bounded concurrency; excess connections get 503, not a thread
    drain_limit = 262144          # bytes of a refused request we are willing to read

    def __init__(self, service, host: str, port: int):
        self.service = service
        self._slots = threading.BoundedSemaphore(self.max_workers)
        self._owned = set()
        self._lock = threading.Lock()
        self._double_release = 0      # observable invariant, asserted by the tests
        super().__init__((host, port), Handler)
        real = self.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{real}", f"localhost:{real}", f"[::1]:{real}"}

    slot_wait = 0.25              # bounded wait for a worker slot before refusing

    def _release_slot(self, request) -> None:
        """Release a connection's slot exactly once, atomically.

        The ownership check, the removal and the release must be one critical section:
        the exception-cleanup path and shutdown_request can run concurrently for the same
        connection, and an unsynchronised check-then-release lets both of them release.
        """
        with self._lock:
            if id(request) in self._owned:
                self._owned.discard(id(request))
                try:
                    self._slots.release()
                except ValueError:            # BoundedSemaphore says this was a double release
                    self._double_release += 1  # counted, so the tests can assert it stays 0

    def process_request(self, request, client_address):
        if not self._slots.acquire(timeout=self.slot_wait):
            # Refuse WITHOUT touching the semaphore: this connection never owned a slot, and
            # releasing one here would hand an active worker's capacity to the next caller.
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                                b"Connection: close\r\n\r\n")
            except Exception:
                pass                          # the peer is gone; nothing to tell it
            try:
                request.shutdown(socket.SHUT_WR)   # FIN, so the client knows the response ended
            except Exception:
                pass
            # Drain the request so close() does not answer a half-read request with an RST.
            # Bounded twice: by time (slot_wait) and by bytes, so a flooder cannot pin the loop.
            budget_end = time.monotonic() + self.slot_wait
            drained = 0
            while drained < self.drain_limit:
                left = budget_end - time.monotonic()
                if left <= 0:
                    break
                try:
                    request.settimeout(left)
                    part = request.recv(65536)
                except (socket.timeout, OSError):
                    break
                if not part:
                    break
                drained += len(part)
            try:
                request.close()
            except Exception:
                pass
            return
        with self._lock:
            self._owned.add(id(request))
        try:
            request.settimeout(self.handler_timeout)   # a stalled client must not pin a worker
        except OSError:
            pass
        try:
            super().process_request(request, client_address)
        except Exception:
            self._release_slot(request)
            raise

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            self._release_slot(request)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.5},
                             name="antiloop-http", daemon=True)
        t.start()
        return t
