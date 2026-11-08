"""P1 second follow-up: the defects supervision found in the first patch."""
import importlib.util
import socket
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "integration" / "hermes_hook.py"


def load_hook(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("p1c_hook", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "BREAKER_FILE", tmp_path / ".down")
    monkeypatch.setattr(mod, "BREAK_DIR", tmp_path / "state")
    monkeypatch.setattr(mod, "TOKEN_FILE", tmp_path / "token")
    (tmp_path / "token").write_text("tok", encoding="utf-8")
    return mod


class Peer:
    def __init__(self, behaviour):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, args=(behaviour,), daemon=True).start()

    def _serve(self, behaviour):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                conn.recv(65536)
                behaviour(conn)
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


# ---------------------------------------------------------------- B2 slot ownership
class FakeRequest:
    """Minimal stand-in for a socket, so process_request can be driven directly."""

    def __init__(self):
        self.sent = b""
        self.closed = False

    def sendall(self, data):
        self.sent += data

    def close(self):
        self.closed = True

    def shutdown(self, how):
        pass

    def settimeout(self, value):
        self.timeout = value

    def recv(self, n):
        return b""

    def makefile(self, *a, **k):
        raise AssertionError("a rejected connection must never reach the handler")


def test_b2_rejected_connection_must_not_release_an_active_slot(tmp_path):
    from antiloop_watchdog.api import ApiServer

    class Dummy:
        pass

    srv = ApiServer.__new__(ApiServer)          # no socket, no bind: only the slot machinery
    srv._slots = threading.BoundedSemaphore(1)
    srv._owned = set()
    srv._lock = threading.Lock()
    srv._double_release = 0
    srv._slots.acquire()                         # an active worker owns the only slot
    req = FakeRequest()

    srv.process_request(req, ("127.0.0.1", 0))   # must be refused, must NOT free the slot

    assert b"503" in req.sent, "overload must answer 503"
    assert req.closed, "the refused connection must be closed"
    assert srv._slots.acquire(blocking=False) is False, \
        "the refusal released a slot that belonged to an active worker"


def test_b2_slot_is_released_exactly_once(tmp_path):
    from antiloop_watchdog.api import ApiServer

    srv = ApiServer.__new__(ApiServer)
    srv._slots = threading.BoundedSemaphore(2)
    srv._owned = set()
    srv._lock = threading.Lock()
    srv._double_release = 0
    req = FakeRequest()
    srv._slots.acquire()                         # simulate the successful acquire
    srv._owned.add(id(req))
    srv.shutdown_request(req)                    # releases once
    srv.shutdown_request(req)                    # idempotent: must not release twice
    assert srv._slots._value == 2, "double shutdown must not over-release"


# ---------------------------------------------------------------- 503 -> breaker
def test_b2_503_is_treated_as_unavailable(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)

    def unavailable(conn):
        conn.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")

    peer = Peer(unavailable)
    monkeypatch.setattr(hook, "BASE_URL", "http://127.0.0.1:%d" % peer.port)
    hook.BREAKER_FILE.unlink(missing_ok=True)
    assert hook._call("POST", "/events", {}) is None
    assert hook._breaker_open() is True, "503 means the watchdog could not serve us: open the breaker"
    peer.close()


def test_b2_ordinary_4xx_does_not_trip_the_breaker(tmp_path, monkeypatch):
    hook = load_hook(tmp_path, monkeypatch)

    def refused(conn):
        conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 2\r\n\r\n{}")

    peer = Peer(refused)
    monkeypatch.setattr(hook, "BASE_URL", "http://127.0.0.1:%d" % peer.port)
    hook.BREAKER_FILE.unlink(missing_ok=True)
    assert hook._call("POST", "/events", {}) is None
    assert hook._breaker_open() is False, "a refused call is not a transport failure"
    peer.close()


# ---------------------------------------------------------------- A11 undecodable
def test_a11_undecodable_body_raises_typed_error(tmp_path):
    import pytest
    from antiloop_watchdog import client as client_mod

    def bad_utf8(conn):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n\xff\xfe\x00\x01")

    peer = Peer(bad_utf8)
    c = client_mod.WatchdogClient(base_url="http://127.0.0.1:%d" % peer.port, token="t")
    try:
        with pytest.raises(client_mod.WatchdogUnavailable):
            c.sessions()
    finally:
        peer.close()
