"""Tiny persistent state: per-session mode overrides (written only when a mode changes)."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from .schema import MODES
from .util import atomic_write_text

MAX_ENTRIES = 256


class StateStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.session_modes: dict = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            modes = raw.get("session_modes", {})
            if isinstance(modes, dict):
                self.session_modes = {k: v for k, v in modes.items() if isinstance(k, str) and v in MODES}
        except (OSError, ValueError):
            self.session_modes = {}

    def get_mode(self, session_id: str) -> Optional[str]:
        return self.session_modes.get(session_id)

    def set_mode(self, session_id: str, mode: Optional[str]) -> None:
        with self._lock:
            if mode is None:
                self.session_modes.pop(session_id, None)
            else:
                self.session_modes.pop(session_id, None)       # re-insert -> most recent last
                self.session_modes[session_id] = mode
                while len(self.session_modes) > MAX_ENTRIES:
                    self.session_modes.pop(next(iter(self.session_modes)))
            self._save()

    def _save(self) -> None:
        try:
            atomic_write_text(self.path, json.dumps({"session_modes": self.session_modes}, indent=2) + "\n")
        except OSError:
            pass
