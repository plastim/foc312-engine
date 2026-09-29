"""Analysis loader/summarizers + Markdown report on a synthesized session dir."""

import json
from pathlib import Path

import numpy as np

from stimengine.analysis.session import (
    OPEN_SKIN_OHM,
    Session,
    bucketize,
    latest_session_dir,
    load_session,
    percentiles,
    sparkline,
)
from stimengine.tools import session_report


def _write(p: Path, recs: list[dict], truncate_tail: bool = False) -> None:
    lines = [json.dumps(r, separators=(",", ":")) for r in recs]
    text = "\n".join(lines) + "\n"
    if truncate_tail:
        text += '{"t": 99.0, "kind": "batt'  # half-written last line (fault mid-write)
    p.write_text(text, encoding="utf-8")


def make_session(root: Path, name: str = "20260820T000000Z", contact: bool = False) -> Path:
    d = root / name
    d.mkdir(parents=True)
    meta = {
        "started": "2026-08-20T00:00:00+00:00",
        "ended": "2026-08-20T00:01:00+00:00",
        "summary": {"max_amps_commanded": 0.05, "commands": 5, "notifications": 0, "duration_s": 60.0},
        "firmware": "1.3.2 (main)",
        "board": "BOARD_FOCSTIM_V4",
        "transport": "serial COM13",
        "mode": "threephase",
        "end_reason": "fault: link closed",
        "faulted": True,
        "fault_reason": "link closed",
        "updates_sent": 1234,
        "max_latency_s": 0.0495,
    }
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    cmds = [
        {"t": 0.1, "ts": "x", "kind": "amps", "source": "engine", "amps": 0.0, "interval_ms": 0},
        {"t": 0.2, "ts": "x", "kind": "signal_start", "source": "engine", "mode": "threephase"},
        {"t": 0.3, "ts": "x", "kind": "arm", "source": "engine"},
        {"t": 10.0, "ts": "x", "kind": "amps", "source": "engine", "amps": 0.05, "interval_ms": 30},
        {"t": 30.0, "ts": "x", "kind": "deadman_start", "source": "engine", "silent_s": 2.0},
        {"t": 40.0, "ts": "x", "kind": "latency", "source": "soak", "latency_ms": 12.0},
        {"t": 41.0, "ts": "x", "kind": "latency", "source": "soak", "latency_ms": 80.0},
        {"t": 59.9, "ts": "x", "kind": "fault", "source": "engine", "reason": "link closed"},
    ]
    _write(d / "commands.jsonl", cmds)
    tele = []
    for i in range(60):
        t = float(i)
        tele.append({"t": t, "ts": "x", "kind": "battery", "data": {"battery_voltage": 4.1 + 0.001 * i, "battery_soc": 1.0}})
        skin = 120.0 + i if contact else OPEN_SKIN_OHM
        tele.append({"t": t + 0.2, "ts": "x", "kind": "skin_resistance",
                     "data": {f"resistance_{e}": skin for e in "abcd"}})
        tele.append({"t": t + 0.3, "ts": "x", "kind": "signal_stats",
                     "data": {"actual_pulse_frequency": 20.0 + i * 0.5, "v_drive": 0.0}})
        tele.append({"t": t + 0.4, "ts": "x", "kind": "currents",
                     "data": {"rms_a": 0.001 * i, "rms_b": 0.0, "rms_c": 0.0, "rms_d": 0.0}})
        tele.append({"t": t + 0.5, "ts": "x", "kind": "system_stats", "data": {"focstimv3": {"temp_stm32": 30.0 + i * 0.1}}})
    tele.append({"t": 0.05, "ts": "x", "kind": "debug_string", "data": {"message": "Comms lost? Stopping."}})
    _write(d / "telemetry.jsonl", tele, truncate_tail=True)
    return d


def test_load_and_summary(tmp_path):
    d = make_session(tmp_path)
    s = load_session(d)
    assert isinstance(s, Session)
    assert s.duration_s == 60.0
    assert s.transport == "serial COM13" and s.mode == "threephase" and s.firmware.startswith("1.3.2")
    assert s.end_reason.startswith("fault") and s.max_amps_commanded == 0.05
    # truncated tail tolerated: 5 kinds * 60 + 1 debug string
    assert sum(s.kinds().values()) == 301
    summ = s.summary()
    assert summ["contact"] == {"a": "open", "b": "open", "c": "open", "d": "open"}
    assert summ["actual_pulse_hz"]["min"] == 20.0 and summ["actual_pulse_hz"]["max"] == 49.5
    assert abs(summ["rms_current"]["a"]["max"] - 0.059) < 1e-12
    assert summ["battery_v"]["n"] == 60
    assert summ["temp_stm32"]["last"] > 35.0
    kinds = [e["kind"] for e in summ["events"]]
    assert kinds[0] == "firmware"  # debug string at t=0.05 sorts first
    assert "deadman_start" in kinds and kinds[-1] == "fault"
    assert summ["latency_ms"]["n"] == 2 and summ["latency_ms"]["max"] == 80.0
    assert summ["latency_ms"]["over"]["50"] == 1
    rates = summ["notifications"]
    assert rates["battery"]["count"] == 60 and abs(rates["battery"]["rate_hz"] - 1.0) < 1e-9
    assert abs(rates["battery"]["max_gap_s"] - 1.0) < 1e-9


def test_contact_detection(tmp_path):
    d = make_session(tmp_path, contact=True)
    s = load_session(d)
    assert s.summary()["contact"]["a"] == "contact"


def test_latest_and_missing(tmp_path):
    assert latest_session_dir(tmp_path / "nope") is None
    make_session(tmp_path, "20260820T000000Z")
    make_session(tmp_path, "20260820T000100Z")
    assert latest_session_dir(tmp_path).name == "20260820T000100Z"
    assert load_session("latest", root=tmp_path).dir.name == "20260820T000100Z"
    try:
        load_session(tmp_path / "missing")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError")


def test_helpers():
    p = percentiles([10, 20, 30, 400])
    assert p["n"] == 4 and p["max"] == 400 and p["over"]["250"] == 1 and p["over"]["500"] == 0
    assert percentiles([]) == {"n": 0}
    from stimengine.analysis.session import Series
    s = Series(np.array([0.0, 30.0, 59.0]), np.array([1.0, 2.0, 3.0]))
    b = bucketize(s, 60.0, 6)
    assert b[0] == 1.0 and b[3] == 2.0 and b[5] == 3.0 and np.isnan(b[1])
    sp = sparkline([0, 1, np.nan, 2])
    assert len(sp) == 4 and sp[2] == " " and sp[0] != sp[3]
    assert sparkline([np.nan, np.nan]) == "  "
    assert sparkline([5, 5, 5]) == "▁▁▁"


def test_markdown_report_and_cli(tmp_path, capsys):
    d = make_session(tmp_path)
    md = session_report.render_markdown(load_session(d))
    assert md.startswith("# Session 20260820T000000Z")
    assert "**FAULT**" in md and "Comms lost? Stopping." in md
    assert "| actual pulse (Hz) | 60 |" in md
    assert "skin R A" in md and "axis-move latency" in md
    out_md = tmp_path / "r.md"
    out_json = tmp_path / "r.json"
    rc = session_report.main([str(d), "--md", str(out_md), "--json", str(out_json)])
    assert rc == 0
    assert out_md.read_text(encoding="utf-8") == md
    j = json.loads(out_json.read_text(encoding="utf-8"))
    assert j["mode"] == "threephase" and j["contact"]["a"] == "open"
    assert session_report.main([str(tmp_path / "missing")]) == 2
    rc = session_report.main(["latest", "--root", str(tmp_path)])
    assert rc == 0
    assert "# Session" in capsys.readouterr().out
