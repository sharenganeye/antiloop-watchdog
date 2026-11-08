"""P1 third follow-up: the concurrency and error-path tests supervision required."""
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class FakeRequest:
    def __init__(self, fail_send=False):
        self.sent = b""
        self.closed = False
        self.fail_send = fail_send

    def sendall(self, data):
        if self.fail_send:
            raise RuntimeError("socket already gone")
        self.sent += data

    def close(self):
        self.closed = True

    def shutdown(self, how):
        pass

    def settimeout(self, value):
        self.timeout = value

    def recv(self, n):
        return b""


def fresh_server(max_workers=2):
    from antiloop_watchdog.api import ApiServer

    srv = ApiServer.__new__(ApiServer)          # no bind: only the slot machinery
    srv._slots = threading.BoundedSemaphore(max_workers)
    srv._owned = set()
    srv._lock = threading.Lock()
    srv._double_release = 0
    srv.max_workers = max_workers
    return srv


def take_slot(srv):
    """Occupy one slot the way a live worker would."""
    assert srv._slots.acquire(timeout=1)
    req = FakeRequest()
    with srv._lock:
        srv._owned.add(id(req))
    return req


# ---------------------------------------------------------------- 1. duplicate shutdown
def test_concurrent_duplicate_shutdown_releases_once():
    srv = fresh_server(2)
    req = take_slot(srv)
    barrier = threading.Barrier(8)

    def shutdown():
        barrier.wait()
        for _ in range(5):
            srv.shutdown_request(req)

    threads = [threading.Thread(target=shutdown) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert srv._slots._value == 2, "the slot must come back exactly once, not once per caller"
    assert srv._double_release == 0, "a double release must never be swallowed"
    assert not srv._owned


# ---------------------------------------------------------------- 2. cleanup race
def test_exception_cleanup_racing_with_shutdown_releases_once():
    for _ in range(50):                       # a race needs repetition to be caught
        srv = fresh_server(2)
        req = take_slot(srv)
        barrier = threading.Barrier(2)

        def cleanup():
            barrier.wait()
            srv._release_slot(req)

        def shutdown():
            barrier.wait()
            srv.shutdown_request(req)

        t1, t2 = threading.Thread(target=cleanup), threading.Thread(target=shutdown)
        t1.start(); t2.start(); t1.join(); t2.join()
        assert srv._slots._value == 2, "both paths released, or neither"
        assert srv._double_release == 0, "a double release must never be swallowed"
        assert not srv._owned


# ---------------------------------------------------------------- 3. failing 503 send
def test_rejected_connection_is_closed_even_when_send_fails():
    srv = fresh_server(1)
    srv._slots.acquire()                      # capacity exhausted
    req = FakeRequest(fail_send=True)

    srv.process_request(req, ("127.0.0.1", 0))

    assert req.closed, "the socket must be closed even though sendall() raised"
    assert srv._slots._value == 0, "a refused connection must not touch the semaphore"
    srv._slots.release()


# ---------------------------------------------------------------- 4. capacity bound
def test_capacity_never_exceeds_max_workers_while_others_wait():
    srv = fresh_server(2)
    held = [take_slot(srv) for _ in range(2)]   # both workers busy
    refused = [FakeRequest() for _ in range(6)]

    for req in refused:
        srv.process_request(req, ("127.0.0.1", 0))

    assert all(b"503" in r.sent for r in refused), "over capacity must be refused"
    assert srv._slots._value == 0, "no refusal may free capacity"
    assert srv._slots.acquire(blocking=False) is False, "capacity must still be exhausted"

    for req in held:
        srv.shutdown_request(req)
    assert srv._slots._value == 2, "capacity returns only when the workers finish"
