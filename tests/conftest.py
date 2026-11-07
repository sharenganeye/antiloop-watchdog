import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

from antiloop_watchdog.adapters import AdapterResult, HermesAdapter  # noqa: E402
from antiloop_watchdog.config import ConfigManager  # noqa: E402
from antiloop_watchdog.engine import WatchdogEngine  # noqa: E402
from antiloop_watchdog.logsetup import MemoryEventLog  # noqa: E402
from antiloop_watchdog.state import StateStore  # noqa: E402


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class RecordingAdapter(HermesAdapter):
    """Push-style test adapter that records every call (and can be told to fail)."""
    name = "recording"
    supports_redirect = True
    supports_interrupt = True
    push = True

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def redirect(self, session_id, directive):
        self.calls.append((session_id, "redirect", directive["message"]))
        return AdapterResult(False, "push_failed", "boom") if self.fail else AdapterResult(True, "pushed")

    def interrupt(self, session_id, directive):
        self.calls.append((session_id, "interrupt", directive["message"]))
        return AdapterResult(False, "push_failed", "boom") if self.fail else AdapterResult(True, "pushed")


@pytest.fixture
def make_engine(tmp_path):
    def _make(mode="observe", overrides=None, adapter=None, with_state=False):
        ov = {"default_mode": mode}
        for k, v in (overrides or {}).items():
            ov[k] = {**ov.get(k, {}), **v} if isinstance(v, dict) else v
        cm = ConfigManager(tmp_path / "config" / "cfg.json", base_dir=tmp_path, overrides=ov)
        clock = Clock()
        log = MemoryEventLog()
        state = StateStore(tmp_path / "state.json") if with_state else None
        eng = WatchdogEngine(cm, adapter, log, clock, state)
        return eng, clock, log
    return _make


@pytest.fixture
def recorder():
    return RecordingAdapter()


def events_of(log, etype=None, sid=None):
    return [e for e in log.events
            if (etype is None or e["event_type"] == etype) and (sid is None or e["session_id"] == sid)]
