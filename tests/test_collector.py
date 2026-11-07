"""llama.cpp / Strata log tailer: incremental, speed-only, slot-scoped."""
from antiloop_watchdog.collector import LlamaLogTailer, parse_lines
from conftest import events_of

SAMPLE = """\
slot print_timing: id  0 | task 11 |
prompt eval time =    1500.00 ms /  2400 tokens (    0.62 ms per token,  1600.00 tokens per second)
       eval time =    5000.00 ms /   245 tokens (   20.41 ms per token,    49.00 tokens per second)
      total time =    6500.00 ms /  2645 tokens
slot print_timing: id  1 | task 12 |
       eval time =    2000.00 ms /     3 tokens (  666.00 ms per token,     1.50 tokens per second)
"""


def test_parse_lines_distinguishes_prefill_from_decode_and_tracks_slots():
    out, slot = parse_lines(SAMPLE.splitlines(), "unknown")
    assert out == [("0", "prefill", 2400, 1.5), ("0", "decode", 245, 5.0), ("1", "decode", 3, 2.0)]
    assert slot == "1"


def test_tailer_reads_only_appended_bytes_and_feeds_slot_sessions(tmp_path, make_engine, recorder):
    eng, clock, log = make_engine("automatic", adapter=recorder)
    f = tmp_path / "strata.log"
    f.write_text("old line: eval time = 1000.00 ms / 200 tokens\n")
    t = LlamaLogTailer(str(f), eng.ingest_speed_sample)
    assert t.poll() == 0                                       # attaches at the end, no replay
    with open(f, "a") as fh:
        fh.write(SAMPLE[:120])                                 # partial write ...
    assert t.poll() <= 1
    with open(f, "a") as fh:
        fh.write(SAMPLE[120:])
    t.poll()
    sess = {s["session_id"]: s for s in eng.list_sessions()}
    assert set(sess) == {"slot-0", "slot-1"}
    assert sess["slot-0"]["speed"]["accepted_blocks"] == 1 and sess["slot-0"]["speed"]["ignored_blocks"]["prefill"] == 1
    assert sess["slot-1"]["speed"]["ignored_blocks"]["tiny_block"] == 1
    assert sess["slot-0"]["automatic_eligible"] is False and recorder.calls == []


def test_tailer_survives_rotation_and_missing_file(tmp_path):
    got = []
    f = tmp_path / "x.log"
    t = LlamaLogTailer(str(f), lambda *a: got.append(a), from_start=True)
    assert t.poll() == 0                                       # file does not exist yet
    f.write_text("slot print_timing: id 0 | t |\n       eval time = 1000.00 ms / 50 tokens\n")
    assert t.poll() == 1
    f.write_text("slot print_timing: id 0 | t |\n       eval time = 2000.00 ms / 90 tokens\n")   # rotated (shorter)
    t.offset = 10**6
    assert t.poll() == 1 and got[-1][2] == 90
