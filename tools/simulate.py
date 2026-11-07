#!/usr/bin/env python3
"""Synthetic load generator + resource meter for the watchdog (spawns its own isolated instance).

Phases: idle -> normal load (N sessions at ~50 tok/s each: streamed text, speed blocks, tool calls)
-> loop (one session repeats a paragraph) -> burst (events as fast as possible).
Reports CPU % (share of ONE core, from the OS process counters) and RSS of the service process.
Linux/WSL: /proc.  Windows: needs `pip install psutil` in the project venv (optional).

    python tools/simulate.py --duration 60 --sessions 2 --out reports/resource_measurement.json
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import random
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_r = random.Random(99)
SYL = "ka to ri ven mol sa du pen lo xi ar bel cor fen gu hal ix jor kes lum nor ot pra qui rus sel tam ul vi wex yar zon".split()
VOCAB = sorted({"".join(_r.choice(SYL) for _ in range(_r.randint(1, 3))) for _ in range(4000)})


def words(rng, n):
    return " ".join(rng.choice(VOCAB) for _ in range(n)) + ". "


class Proc:
    def __init__(self, pid):
        self.pid = pid
        try:
            import psutil  # type: ignore
            self.ps = psutil.Process(pid)
        except Exception:
            self.ps = None
        self.hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

    def _win(self):
        """ctypes handle + prototypes for GetProcessTimes/GetProcessMemoryInfo (Windows only)."""
        if getattr(self, "_h", None) is None:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi

            # Without explicit prototypes ctypes truncates the 64-bit HANDLE to a C int
            # and GetProcessTimes silently fails (returned 0 CPU seconds before the fix).
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                                 ctypes.POINTER(wintypes.FILETIME),
                                                 ctypes.POINTER(wintypes.FILETIME),
                                                 ctypes.POINTER(wintypes.FILETIME))

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE,
                                                   ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                                                   wintypes.DWORD)
            self._h, self._pmc = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, self.pid), PROCESS_MEMORY_COUNTERS

    def _tree(self):
        """Self + descendants: the venv python.exe is a launcher stub that re-execs the real
        interpreter, so measuring only self.pid reports the idle launcher (0.0 CPU, 4 MB)."""
        if self.ps is None:
            return []
        try:
            return [self.ps] + self.ps.children(recursive=True)
        except Exception:
            return [self.ps]

    def cpu_seconds(self):
        if self.ps is not None:
            total = 0.0
            for p in self._tree():
                try:
                    t = p.cpu_times()
                    total += t.user + t.system
                except Exception:
                    continue
            return total
        if os.name == "nt":
            return self._win_cpu()
        with open(f"/proc/{self.pid}/stat") as fh:
            f = fh.read().rsplit(")", 1)[1].split()
        return (int(f[11]) + int(f[12])) / self.hz

    def _win_cpu(self):
        import ctypes
        from ctypes import wintypes

        self._win()
        creation, exit_, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not ctypes.windll.kernel32.GetProcessTimes(
                self._h, ctypes.byref(creation), ctypes.byref(exit_),
                ctypes.byref(kernel), ctypes.byref(user)):
            return 0.0
        def _secs(ft):
            return (ft.dwHighDateTime << 32 | ft.dwLowDateTime) / 10_000_000
        return _secs(kernel) + _secs(user)

    def rss_mb(self):
        if self.ps is not None:
            total = 0
            for p in self._tree():
                try:
                    total += p.memory_info().rss
                except Exception:
                    continue
            return total / 1048576
        if os.name == "nt":
            import ctypes

            self._win()
            pmc = self._pmc()
            pmc.cb = ctypes.sizeof(self._pmc)
            if not ctypes.windll.psapi.GetProcessMemoryInfo(
                    self._h, ctypes.byref(pmc), ctypes.sizeof(pmc)):
                return None
            return pmc.WorkingSetSize / 1048576
        with open(f"/proc/{self.pid}/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
        return None


class Client:
    def __init__(self, port, token):
        self.c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        self.h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.n = 0

    def post(self, ev):
        self.c.request("POST", "/events", body=json.dumps(ev), headers=self.h)
        r = self.c.getresponse()
        r.read()
        self.n += 1
        return r.status


def phase(name, seconds, step, proc, report, rss_samples):
    c0, t0, n0 = proc.cpu_seconds(), time.monotonic(), step.sent
    end = t0 + seconds
    while time.monotonic() < end:
        step.tick()
        rss_samples.append(proc.rss_mb())
    dt = time.monotonic() - t0
    cpu = (proc.cpu_seconds() - c0) / dt * 100
    report[name] = {"seconds": round(dt, 1), "events_sent": step.sent - n0,
                    "events_per_s": round((step.sent - n0) / dt, 1), "service_cpu_percent_of_one_core": round(cpu, 2),
                    "rss_mb_end": round(rss_samples[-1], 1) if rss_samples and rss_samples[-1] else None}


class Idle:
    sent = 0
    def tick(self): time.sleep(0.5)


class Normal:
    """N sessions, each ~50 tok/s: text every 0.4 s (~20 tokens), speed block every 5 s, tool call every 4 s."""
    def __init__(self, clients, sessions):
        self.cl, self.sessions, self.sent, self.rng = clients, sessions, 0, random.Random(5)
        self.t, self.i = time.monotonic(), 0

    def tick(self):
        self.i += 1
        for k, sid in enumerate(self.sessions):
            c = self.cl[k]
            c.post({"type": "text_delta", "session_id": sid, "kind": "reasoning", "text": words(self.rng, 16)})
            self.sent += 1
            if self.i % 12 == 0:
                c.post({"type": "generation", "session_id": sid, "phase": "thinking", "tokens": 250, "duration_s": 5.0})
                self.sent += 1
            if self.i % 10 == 0:
                c.post({"type": "tool_call", "session_id": sid, "tool": "read_file",
                        "args": {"path": f"/src/f{self.i}_{k}.py"}, "status": "ok", "result_text": f"content {self.i} {k}"})
                self.sent += 1
        time.sleep(max(0.0, 0.4 - 0.02))


class Loop(Normal):
    para = None
    def tick(self):
        if Loop.para is None:
            Loop.para = words(random.Random(7), 90)
        sid = self.sessions[0]
        self.cl[0].post({"type": "text_delta", "session_id": sid, "kind": "reasoning", "text": Loop.para})
        self.sent += 1
        super_sessions = self.sessions[1:]
        for k, s in enumerate(super_sessions, start=1):
            self.cl[k].post({"type": "text_delta", "session_id": s, "kind": "reasoning", "text": words(self.rng, 16)})
            self.sent += 1
        time.sleep(0.4)


class Burst:
    def __init__(self, clients, sessions):
        self.cl, self.sessions, self.sent, self.rng = clients, sessions, 0, random.Random(8)
    def tick(self):
        for k, sid in enumerate(self.sessions):
            self.cl[k].post({"type": "text_delta", "session_id": sid, "kind": "answer", "text": words(self.rng, 16)})
            self.sent += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=60.0, help="seconds for the normal-load phase")
    ap.add_argument("--sessions", type=int, default=2)
    ap.add_argument("--out", default=str(ROOT / "reports" / "resource_measurement.json"))
    a = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="antiloop-sim-"))
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    cfg = tmp / "config" / "w.json"; cfg.parent.mkdir()
    cfg.write_text(json.dumps({"api": {"port": port}, "default_mode": "warn"}))
    proc = subprocess.Popen([sys.executable, "-m", "antiloop_watchdog", "--config", str(cfg), "--base-dir", str(tmp)],
                            cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        tok_path = tmp / "state" / "token"
        for _ in range(100):
            if tok_path.exists():
                try:
                    c = http.client.HTTPConnection("127.0.0.1", port, timeout=1); c.request("GET", "/health"); c.getresponse().read(); break
                except OSError:
                    pass
            time.sleep(0.1)
        token = tok_path.read_text().strip()
        time.sleep(1.0)
        p = Proc(proc.pid)
        sessions = [f"sim-{i}" for i in range(a.sessions)]
        clients = [Client(port, token) for _ in sessions]
        report = {"python": sys.version.split()[0], "platform": sys.platform, "cpu_count": os.cpu_count(),
                  "sessions": a.sessions, "rss_mb_after_start": round(p.rss_mb(), 1)}
        rss = []
        phase("idle", 15, Idle(), p, report, rss)
        phase("normal_load", a.duration, Normal(clients, sessions), p, report, rss)
        phase("loop_in_session_0", 10, Loop(clients, sessions), p, report, rss)
        phase("burst_max_rate", 10, Burst(clients, sessions), p, report, rss)
        report["rss_mb_peak"] = round(max(x for x in rss if x), 1)
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/status", headers={"Authorization": f"Bearer {token}"})
        st = json.loads(c.getresponse().read())
        report["service_counters"] = {k: v for k, v in st["counters"].items()
                                      if k.startswith(("detection_", "events_", "interventions_"))}
        report["note"] = ("CPU is the service process only (not the load generator), as a share of one core. "
                          "Measured on the machine named in 'platform'; re-run on the target machine.")
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
    finally:
        if os.name == "nt":
            proc.terminate()          # Windows: no SIGINT for subprocesses
        else:
            proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
