"""Service lifecycle: builds everything, runs the HTTP server and the light 1 s tick thread."""
from __future__ import annotations

import os
import secrets
import signal
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .adapters import HermesAdapter, build_adapter
from .api import ApiServer
from .collector import LlamaLogTailer
from .config import ConfigManager
from .engine import WatchdogEngine
from .logsetup import EventLog
from .schema import ConfigError
from .state import StateStore
from .util import atomic_write_text

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def default_base_dir() -> Path:
    return Path(__file__).resolve().parent.parent


class Service:
    def __init__(self, config_path: Optional[str] = None, base_dir: Optional[str] = None,
                 overrides: Optional[dict] = None, clock: Callable[[], float] = time.time,
                 adapter: Optional[HermesAdapter] = None, event_log=None, token: Optional[str] = None):
        self.base_dir = Path(base_dir) if base_dir else default_base_dir()
        cfg_path = Path(config_path) if config_path else self.base_dir / "config" / "watchdog.config.json"
        self.cm = ConfigManager(cfg_path, base_dir=self.base_dir, overrides=overrides)
        host = self.cm.data["api"]["host"]
        if host not in LOOPBACK:
            raise ConfigError(f"api.host must be a loopback address (got {host!r}); remote binding is not supported")
        self.state_dir = self.cm.resolve(self.cm.data["paths"]["state_dir"])
        self.log_dir = self.cm.resolve(self.cm.data["paths"]["log_dir"])
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.token = self._load_token(token)
        lc = self.cm.data["logging"]
        self.event_log = event_log if event_log is not None else EventLog(
            self.log_dir, lc["json_file"], lc["text_file"], lc["max_bytes"], lc["backups"])
        if adapter is None:
            adapter, warn = build_adapter(self.cm.data)
            if warn:
                self.cm.warnings.append(warn)
        self.state = StateStore(self.state_dir / "state.json")
        self.engine = WatchdogEngine(self.cm, adapter, self.event_log, clock, self.state)
        self.tailer: Optional[LlamaLogTailer] = None
        cp = self.cm.data["collector"]["llama_log_path"]
        if cp:
            self.tailer = LlamaLogTailer(cp, self.engine.ingest_speed_sample,
                                         self.cm.data["collector"]["llama_log_from_start"])
        self.server: Optional[ApiServer] = None
        self.port: Optional[int] = None
        self._stop = threading.Event()
        self._threads: list = []
        self.pidfile = self.state_dir / "watchdog.pid"

    # ------------------------------------------------------------------ token

    def _load_token(self, explicit: Optional[str]) -> str:
        tok = explicit or os.environ.get("WATCHDOG_TOKEN", "")
        if tok:
            if len(tok) < 16:
                raise ConfigError("WATCHDOG_TOKEN must be at least 16 characters")
            return tok
        path = self.cm.resolve(self.cm.data["api"]["token_file"])
        try:
            tok = path.read_text(encoding="utf-8").strip()
            if len(tok) >= 16:
                return tok
        except OSError:
            pass
        tok = secrets.token_urlsafe(32)
        atomic_write_text(path, tok + "\n", mode=0o600)
        return tok

    @property
    def token_path(self) -> Path:
        return self.cm.resolve(self.cm.data["api"]["token_file"])

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> "Service":
        api = self.cm.data["api"]
        self.server = ApiServer(self, api["host"], api["port"])
        self.port = self.server.server_address[1]
        self._threads.append(self.server.start())
        t = threading.Thread(target=self._tick, name="antiloop-tick", daemon=True)
        t.start()
        self._threads.append(t)
        try:
            atomic_write_text(self.pidfile, str(os.getpid()) + "\n")
        except OSError:
            pass
        with self.engine.lock:
            self.engine.emit("service_started", details={
                "note": f"listening on {api['host']}:{self.port}", "adapter": self.engine.adapter.name,
                "default_mode": self.cm.data["default_mode"]})
        return self

    def _tick(self) -> None:
        hk = 0
        while not self._stop.is_set():
            interval = self.cm.data["collector"]["poll_interval_s"] if self.tailer else 5.0
            if self._stop.wait(interval):
                return
            try:
                if self.tailer:
                    self.tailer.poll()
                hk += interval
                if hk >= 5.0:
                    hk = 0
                    self.engine.housekeeping()
            except Exception:
                self.engine.counters["tick_errors"] += 1

    def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        with self.engine.lock:
            self.engine.emit("service_stopped")
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        self.engine.shutdown()
        try:
            if self.pidfile.exists() and self.pidfile.read_text().strip() == str(os.getpid()):
                self.pidfile.unlink()
        except OSError:
            pass
        self.event_log.close()

    def run_forever(self) -> None:
        done = threading.Event()

        def _handler(signum, frame):
            done.set()

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _handler)
                except (ValueError, OSError):
                    pass
        self.start()
        try:
            while not done.wait(0.5):
                pass
        finally:
            self.stop()
