"""P1 fifth follow-up: the two source-level gaps supervision listed.

A1: cap-is-not-completion, chunk CRLF validation, trailer termination, close-delimited close.
B2: the overload response must survive a large or fragmented request, with a bounded drain.
"""
import http.client
import socket
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "integration" / "hermes_hook.py"


def load_hook(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("p1e_hook", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "BREAKER_FILE", tmp_path / ".down")
    monkeypatch.setattr(mod, "BREAK_DIR", tmp_path / "state")
    monkeypatch.setattr(mod, "TOKEN_FILE", tmp_path / "token")
    (tmp_path / "token").write_text("tok", encoding="utf-8")
    return mod


class Peer:
    def __init__(self, raw, keep_open=False, delay=0.0):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self._hold = []
        self._keep, self._delay = keep_open, delay
        threading.Thread(target=self._serve, args=(raw,), daemon=True).start()

    def _serve(self, raw):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                conn.recv(65536)
                if self._delay:
                    time.sleep(self._delay)
                conn.sendall(raw)
                if self._keep:
                    self._hold.append(conn)      # never closes: a peer that hangs
                    continue
            except OSError:
                pass
            finally:
                if not self._keep:
                    try:
                        conn.close()
                    except OSError:
                        pass

    def close(self):
        for c in self._hold:
            try:
                c.close()
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass


def call_against(hook, monkeypatch, raw, **kw):
    peer = Peer(raw, **kw)
    monkeypatch.setattr(hook, "BASE_URL", "http://127.0.0.1:%d" % peer.port)
    hook.BREAKER_FILE.unlink(missing_ok=True)
    out = hook._call("POST", "/events", {})
    state = hook._breaker_open()
    peer.close()
    return out, state


# ------------------------------------------------------------------ A1 strictness
def test_a1_cap_reached_without_end_is_not_a_completion(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)
    payload = b'{"pad": "' + b"x" * hook.MAX_RESPONSE + b'"}'
    raw = b"HTTP/1.1 200 OK\r\n\r\n" + payload        # close-delimited, never closed
    out, breaker = call_against(hook, monkeypatch, raw, keep_open=True)
    assert out is None, "hitting the response cap must never count as a complete reply"
    assert breaker is True


def test_a1_zero_chunk_without_trailer_termination_is_rejected(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)
    raw = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
           b"d\r\n{\"ok\": true}\r\n"                  # valid data chunk
           b"0\r\n")                                   # terminator line, then nothing
    out, breaker = call_against(hook, monkeypatch, raw)
    assert out is None
    assert breaker is True


def test_a1_chunk_data_without_crlf_delimiter_is_rejected(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)
    raw = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
           b"d\r\n{\"ok\": true}XX"                    # chunk data not followed by CRLF
           b"\r\n0\r\n\r\n")
    out, breaker = call_against(hook, monkeypatch, raw)
    assert out is None
    assert breaker is True


def test_a1_close_delimited_requires_an_observed_close(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)
    raw = b"HTTP/1.1 200 OK\r\n\r\n{\"ok\": true}"     # valid JSON, but the peer never closes
    out, breaker = call_against(hook, monkeypatch, raw, keep_open=True, delay=0.0)
    assert out is None, "a close-delimited body needs the close, not just the bytes"
    assert breaker is True


def test_a1_complete_close_delimited_is_accepted(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)
    raw = b"HTTP/1.1 200 OK\r\n\r\n{\"ok\": true}"
    out, breaker = call_against(hook, monkeypatch, raw)
    assert out == {"ok": True}
    assert breaker is False


# ------------------------------------------------------------------ B2 reliability
def _busy_server(tmp_path, monkeypatch, workers=1):
    from antiloop_watchdog import api as api_mod
    from antiloop_watchdog.service import Service

    monkeypatch.setattr(api_mod.ApiServer, "max_workers", workers)
    monkeypatch.setattr(api_mod.ApiServer, "slot_wait", 0.5)
    svc = Service(config_path=str(tmp_path / "cfg" / "w.json"), base_dir=str(tmp_path),
                  overrides={"api": {"port": 0}}, token="t" * 40).start()
    for _ in range(workers):
        assert svc.server._slots.acquire(timeout=2)
    return svc


def _read_status(sock, limit=65536):
    data = b""
    sock.settimeout(10)
    while len(data) < limit:
        try:
            part = sock.recv(4096)
        except socket.timeout:
            break
        if not part:
            break
        data += part
    return data


def test_b2_large_post_body_still_gets_a_complete_503(tmp_path, monkeypatch):
    svc = _busy_server(tmp_path, monkeypatch)
    try:
        body = b'{"x": "' + b"y" * (64 * 1024) + b'"}'
        req = (b"POST /events HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
               b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        sock = socket.create_connection(("127.0.0.1", svc.port), timeout=10)
        sock.sendall(req)
        data = _read_status(sock)
        sock.close()
        assert data.startswith(b"HTTP/1.1 503"), "a 64 KiB POST must still receive the 503: %r" % data[:80]
        assert b"Content-Length: 0" in data.split(b"\r\n\r\n")[0]
    finally:
        svc.stop()


def test_b2_fragmented_request_still_gets_a_complete_503(tmp_path, monkeypatch):
    svc = _busy_server(tmp_path, monkeypatch)
    try:
        sock = socket.create_connection(("127.0.0.1", svc.port), timeout=10)
        for piece in (b"POST /events HTTP/1.1\r\n", b"Host: 127.0.0.1\r\n",
                      b"Content-Type: application/json\r\n", b"Content-Length: 20\r\n", b"\r\n",
                      b'{"a":', b' 1}'):
            sock.sendall(piece)
            time.sleep(0.02)
        data = _read_status(sock)
        sock.close()
        assert data.startswith(b"HTTP/1.1 503"), "a fragmented request must still receive the 503"
    finally:
        svc.stop()


def test_b2_drain_is_bounded_and_the_server_keeps_serving(tmp_path, monkeypatch):
    svc = _busy_server(tmp_path, monkeypatch)
    try:
        flood = socket.create_connection(("127.0.0.1", svc.port), timeout=10)
        flood.sendall(b"POST /events HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 10000000\r\n\r\n")
        for _ in range(40):                   # keep pushing: the server must not wait for us
            try:
                flood.sendall(b"z" * 8192)
            except OSError:
                break
            time.sleep(0.01)

        t0 = time.time()
        # capacity is still held, but the accept loop must answer promptly with 503
        probe = socket.create_connection(("127.0.0.1", svc.port), timeout=5)
        probe.sendall(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        data = _read_status(probe)
        waited = time.time() - t0
        probe.close()
        flood.close()
        assert data.startswith(b"HTTP/1.1 503")
        assert waited < 3, "the accept loop was tied up for %.1f s" % waited
    finally:
        svc.stop()
