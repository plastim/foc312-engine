"""Soak statistics math + the engine's latency observer hook (no hardware)."""

import asyncio
import functools

from stimengine.tools.soak import SoakStats, fmt_lat, pattern_values
from tests.test_engine_core import started_engine


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))

    return wrapper


def test_soak_stats_math():
    s = SoakStats()
    for ms in (5, 10, 20, 60, 120, 300, 600):
        s.on_latency(1, ms / 1000.0)
    for depth in (0, 1, 3, 20):
        s.sample_pending(depth)
    s.on_notification("battery", 0.0)
    s.on_notification("battery", 1.0)
    s.on_notification("battery", 3.5)
    s.on_notification("imu", 0.0)
    row = s.close_minute(1)
    assert row["latency"]["n"] == 7 and row["latency"]["max"] == 600
    assert row["latency"]["over"] == {"50": 4, "100": 3, "250": 2, "500": 1}
    assert row["pending_max"] == 20 and row["notifications"] == 4
    # minute buffers reset, totals kept
    assert s.minute_latency_ms == [] and len(s.latency_ms) == 7
    summ = s.summary()
    assert summ["latency"]["p50"] == 60
    assert summ["pending"]["max"] == 20
    assert abs(summ["max_gap_s"]["battery"] - 2.5) < 1e-9 and "imu" not in summ["max_gap_s"]
    assert summ["link_drops"] == 0 and summ["faults"] == [] and len(summ["minutes"]) == 1
    assert "p99" in fmt_lat(summ["latency"]) and fmt_lat({"n": 0}) == "lat: n=0"


def test_pattern_values_bounded():
    for t in (0.0, 3.3, 12.5, 19.9, 47.0):
        v = pattern_values(t, "threephase")
        assert (v["alpha"] ** 2 + v["beta"] ** 2) ** 0.5 <= 0.5 + 1e-9
        e = pattern_values(t, "fourphase")["e"]
        assert len(e) == 4 and all(0.0 <= x <= 1.0 for x in e) and abs(sum(e) - 1.0) < 1e-9


@run_async
async def test_engine_latency_hook(tmp_path):
    t, dev, eng, session = await started_engine(tmp_path)
    seen = []
    eng.on_move_latency.append(lambda axis, lat: seen.append((axis, lat)))
    eng.set_position(0.2, 0.1, source="test")
    for _ in range(5):
        await asyncio.sleep(0.05)
        if seen:
            break
    assert seen, "latency observer never fired after an acked axis move"
    assert all(lat >= 0 for _, lat in seen)
    await eng.stop()
    dev.stop()
