"""P1 fourth follow-up: the three gaps supervision found in the code review.

A1 chunked completion, B2 handler timeout, and a real overload test.
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
    spec = importlib.util.spec_from_file_location("p1d_hook", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "BREAKER_FILE", tmp_path / ".down")
    monkeypatch.setattr(mod, "BREAK_DIR", tmp_path / "state")
    monkeypatch.setattr(mod, "TOKEN_FILE", tmp_path / "token")
    (tmp_path / "token").write_text("tok", encoding="utf-8")
    return mod


class Peer:
    """A socket peer that sends exactly what the test gives it, then closes."""

    def __init__(self, raw):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, args=(raw,), daemon=True).start()

    def _serve(self, raw):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                conn.recv(65536)
                conn.sendall(raw)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def call_against(hook, monkeypatch, raw):
    peer = Peer(raw)
    monkeypatch.setattr(hook, "BASE_URL", "http://127.0.0.1:%d" % peer.port)
    hook.BREAKER_FILE.unlink(missing_ok=True)
    out = hook._call("POST", "/events", {})
    state = hook._breaker_open()
    peer.close()
    return out, state


# ------------------------------------------------------------- A1 chunked
def test_a1_complete_chunked_body_is_accepted(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)
    raw = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
           b"7\r\n{\"ok\": \r\n4\r\ntrue\r\n1\r\n}\r\n0\r\n\r\n")
    out, breaker = call_against(hook, monkeypatch, raw)
    assert out == {"ok": True}
    assert breaker is False


def test_a1_truncated_chunked_body_trips_the_breaker(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)
    # valid JSON in the bytes that arrived, but the terminator never comes
    raw = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
           b"7\r\n{\"ok\": \r\ntrue")
    out, breaker = call_against(hook, monkeypatch, raw)
    assert out is None, "a body without the 0-chunk must never be reported as a reply"
    assert breaker is True


def test_a1_chunked_body_cut_mid_chunk_trips_the_breaker(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)
    raw = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
           b"20\r\n{\"ok\": true}\r\n")           # declares 32 bytes, delivers 13, no terminator
    out, breaker = call_against(hook, monkeypatch, raw)
    assert out is None
    assert breaker is True


# ------------------------------------------------------------- B2 timeout
def test_b2_stalled_client_is_dropped_by_the_handler_timeout(tmp_path, monkeypatch):
    from antiloop_watchdog import api as api_mod
    from antiloop_watchdog.service import Service

    monkeypatch.setattr(api_mod.ApiServer, "handler_timeout", 1.0)
    svc = Service(config_path=str(tmp_path / "cfg" / "w.json"), base_dir=str(tmp_path),
                  overrides={"api": {"port": 0}}, token="t" * 40).start()
    try:
        sock = socket.create_connection(("127.0.0.1", svc.port), timeout=10)
        t0 = time.time()
        sock.sendall(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n")   # headers never finished
        sock.settimeout(8)
        try:
            data = sock.recv(1024)
        except socket.timeout:
            pytest.fail("the server kept a stalled connection open past the handler timeout")
        waited = time.time() - t0
        sock.close()
        assert data == b"", "the server must close the stalled connection"
        assert waited < 5, "closed after %.1f s, expected about 1 s" % waited
    finally:
        svc.stop()


# ------------------------------------------------------------- B2 real overload
def test_b2_overload_under_real_concurrency_returns_503(tmp_path, monkeypatch):
    """Hold every worker slot on a live server, then hit it concurrently."""
    from antiloop_watchdog import api as api_mod
    from antiloop_watchdog.service import Service

    monkeypatch.setattr(api_mod.ApiServer, "max_workers", 2)
    monkeypatch.setattr(api_mod.ApiServer, "slot_wait", 0.5)
    svc = Service(config_path=str(tmp_path / "cfg" / "w.json"), base_dir=str(tmp_path),
                  overrides={"api": {"port": 0}}, token="t" * 40).start()
    try:
        api = svc.server
        assert hasattr(api, "_slots"), "cannot reach the running server's semaphore"

        held = []
        for _ in range(2):
            assert api._slots.acquire(timeout=2)
            held.append(True)

        codes, lock = [], threading.Lock()

        def probe():
            try:
                c = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=10)
                c.request("GET", "/health")
                r = c.getresponse()
                r.read()
                code = r.status
                c.close()
            except Exception:
                code = 0
            with lock:
                codes.append(code)

        threads = [threading.Thread(target=probe) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]

        assert codes.count(503) == 6, "with every worker busy, all six must be refused: %s" % codes
        for _ in held:
            api._slots.release()

        # capacity back: a normal request must succeed again
        c = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=10)
        c.request("GET", "/health")
        r = c.getresponse()
        body = r.read()
        c.close()
        assert r.status == 200 and b"antiloop-watchdog" in body
    finally:
        svc.stop()
