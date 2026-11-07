"""Small stdlib-only helpers."""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha(text: str, n: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:n]


def atomic_write_text(path, text: str, mode: Optional[int] = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def rss_bytes() -> Optional[int]:
    """Resident set size of this process, or None if it cannot be determined."""
    try:
        with open("/proc/self/status", "r", encoding="ascii", errors="replace") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        try:
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

            counters = PMC()
            counters.cb = ctypes.sizeof(PMC)
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi = ctypes.windll.psapi
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
            if psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
        except Exception:
            return None
    return None


class CpuMeter:
    """CPU usage of this process (percent of one core) since construction / last call."""

    def __init__(self) -> None:
        self._t0 = time.monotonic()
        self._c0 = time.process_time()
        self._lt = self._t0
        self._lc = self._c0

    def since_start(self) -> float:
        dt = time.monotonic() - self._t0
        return round(100.0 * (time.process_time() - self._c0) / dt, 3) if dt > 0 else 0.0

    def since_last(self) -> float:
        now, cpu = time.monotonic(), time.process_time()
        dt = now - self._lt
        val = round(100.0 * (cpu - self._lc) / dt, 3) if dt > 0 else 0.0
        self._lt, self._lc = now, cpu
        return val
