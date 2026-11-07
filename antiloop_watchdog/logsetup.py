"""Event logging: one JSON object per line plus a human readable log.

Only notable events are written (detections, interventions, mode changes, ...). Normal
traffic never touches the disk. Prompts and model output are never logged; with
`logging.capture_text` enabled a short sample of the *repeated* sequence is added.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import threading
from pathlib import Path
from typing import List


def human_line(evt: dict) -> str:
    parts = [evt["ts"], f"{evt['event_type']:<19}"]
    if evt.get("session_id"):
        parts.append(f"session={evt['session_id']}")
    if evt.get("mode"):
        parts.append(f"mode={evt['mode']}")
    if evt.get("detection_type"):
        parts.append(f"detection={evt['detection_type']}")
    if evt.get("confidence") is not None:
        parts.append(f"conf={evt['confidence']:.2f}")
    if evt.get("repetitions"):
        parts.append(f"reps={evt['repetitions']}")
    if evt.get("tps") is not None:
        parts.append(f"tps={evt['tps']}")
    fp = evt.get("sequence_fingerprint") or evt.get("tool_fingerprint")
    if fp:
        parts.append(f"fp={fp}")
    if evt.get("intervention_action"):
        parts.append(f"action={evt['intervention_action']}")
    if evt.get("intervention_result"):
        parts.append(f"result={evt['intervention_result']}")
    if evt.get("wasted_tokens_est") is not None:
        parts.append(f"wasted~{evt['wasted_tokens_est']}tok")
    note = (evt.get("details") or {}).get("note")
    if note:
        parts.append(f"- {note}")
    return " ".join(parts)


class EventLog:
    def __init__(self, log_dir: Path, json_file: str, text_file: str, max_bytes: int, backups: int):
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._json = self._make("antiloop.json", self.dir / json_file, max_bytes, backups)
        self._text = self._make("antiloop.text", self.dir / text_file, max_bytes, backups)

    @staticmethod
    def _make(name: str, path: Path, max_bytes: int, backups: int) -> logging.Logger:
        lg = logging.getLogger(f"{name}.{path}")
        lg.setLevel(logging.INFO)
        lg.propagate = False
        for h in list(lg.handlers):
            lg.removeHandler(h)
            h.close()
        h = logging.handlers.RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(message)s"))
        lg.addHandler(h)
        return lg

    def write(self, evt: dict) -> None:
        with self._lock:
            self._json.info(json.dumps(evt, separators=(",", ":"), ensure_ascii=False))
            self._text.info(human_line(evt))

    def close(self) -> None:
        for lg in (self._json, self._text):
            for h in list(lg.handlers):
                h.close()
                lg.removeHandler(h)


class MemoryEventLog:
    """Used by tests / embedding: keeps events in a list."""

    def __init__(self) -> None:
        self.events: List[dict] = []

    def write(self, evt: dict) -> None:
        self.events.append(evt)

    def close(self) -> None:
        pass
