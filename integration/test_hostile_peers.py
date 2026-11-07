"""Hostile-stub tests for the hook HTTP client (Claude's review item 2)."""
import importlib.util, json, socket, threading, time
from pathlib import Path

HOOK = Path(r"C:/Users/zizor/Desktop/Antiloop/anti-loop-watchdog/integration/hermes_hook.py")
spec = importlib.util.spec_from_file_location("hook_under_test", HOOK)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

fails = 0


def check(name, cond, extra=""):
    global fails
    print(("  PASS  " if cond else "  FAIL  ") + name + ((" | " + str(extra)) if extra else ""))
    if not cond:
        fails += 1


class Stub:
    """One-shot socket server with a scripted (mis)behaviour."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.t = threading.Thread(target=self._serve, daemon=True)
        self.t.start()

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        try:
            conn.recv(8192)
            self.behaviour(conn)
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


def b_garbage(conn):
    conn.sendall(b"NOT-HTTP-AT-ALL\r\n\r\n\x00\x01\x02")


def b_truncated(conn):
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n{\"ok\": tr")


def b_chunked(conn):
    body = json.dumps({"status": "ok"}).encode()
    conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                 + ("%x\r\n" % len(body)).encode() + body + b"\r\n0\r\n\r\n")


def b_oversized(conn):
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5000000\r\n\r\n")
    chunk = b"x" * 65536
    for _ in range(80):
        try:
            conn.sendall(chunk)
        except OSError:
            return


def b_never_ending(conn):
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 999999\r\n\r\n")
    try:
        while True:
            conn.sendall(b"y")
            time.sleep(0.2)
    except OSError:
        return


def b_close(conn):
    pass


TESTS = [("garbage", b_garbage), ("truncated body", b_truncated), ("chunked", b_chunked),
         ("oversized", b_oversized), ("never-ending dribble", b_never_ending), ("closes at once", b_close)]

print("=== hostile peers (client must return, not hang, not raise) ===")
for name, behaviour in TESTS:
    stub = Stub(behaviour)
    m.BASE_URL = "http://127.0.0.1:%d" % stub.port
    time.sleep(0.05)
    t0 = time.perf_counter()
    try:
        out = m._call("POST", "/events", {"type": "tool_call", "session_id": "stub"})
        err = None
    except Exception as exc:                                   # noqa: BLE001
        out, err = None, repr(exc)
    elapsed = time.perf_counter() - t0
    stub.close()
    check(f"{name}: returned inside the deadline", elapsed < m.TOTAL_S + 0.4, "%.2f s" % elapsed)
    check(f"{name}: no exception", err is None, err or "")
    if name == "chunked":
        check("chunked body is decoded", isinstance(out, dict) and out.get("status") == "ok", out)

print("\n=== real watchdog still answers ===")
m.BASE_URL = "http://127.0.0.1:8791"
m.BREAKER_FILE.unlink(missing_ok=True)
t0 = time.perf_counter()
out = m._call("GET", "/status")
elapsed = (time.perf_counter() - t0) * 1000
check("GET /status returns a dict", isinstance(out, dict) and out.get("service") == "antiloop-watchdog")
check("round trip under 100 ms", elapsed < 100, "%.1f ms" % elapsed)

print("\n=== breaker file is written atomically ===")
m.BREAKER_FILE.unlink(missing_ok=True)
m._breaker_trip()
check("breaker file exists after trip", m.BREAKER_FILE.exists())
check("no temp file left behind", not list(m.BREAKER_FILE.parent.glob("*.tmp*")))
m._breaker_reset()

print("\nRESULT:", "PASS" if fails == 0 else "FAIL (%d)" % fails)
raise SystemExit(0 if fails == 0 else 1)
