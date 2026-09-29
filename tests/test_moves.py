"""Moves layer (stimengine/content/moves.py, contract notes/moves.md): quantizer, edge_time controller, dips,
fade-fight, carrier grid, path=around, presets ([moves] table), plus the /moves and /cards API."""
from __future__ import annotations

import asyncio
import functools
import math
import random

import pytest

from stimengine.content.mapping import LIMITS, MappingSet, write_builtin_cards
from stimengine.content.moves import CARRIER_GRID, Moves, MovesParams, snap_carrier

DT = 1.0 / 60.0


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


def mk(**kw) -> Moves:
    return Moves(MovesParams().updated(kw), seed=7)


def run(m: Moves, ticks: int, targets_fn, feats_fn=lambda i: {}, t0: float = 0.0):
    out = []
    for i in range(ticks):
        now = t0 + i * DT
        out.append(m.apply(targets_fn(i), feats_fn(i), now, DT))
    return out


# ---- quantizer ------------------------------------------------------------------------------------------

def test_quantizer_snaps_to_step_and_holds_until_hold_ms():
    m = mk(compose=0.0, quant={"pulse_hz": {"step": 10.0, "hold_ms": 800.0, "on": "any"}})
    o = m.apply({"pulse_hz": 83.0}, {}, 0.0, DT)
    assert o["pulse_hz"] == 80.0
    # a new target inside the hold window is NOT taken
    o = m.apply({"pulse_hz": 97.0}, {}, 0.3, DT)
    assert o["pulse_hz"] == 80.0
    # after hold_ms it is (snapped)
    o = m.apply({"pulse_hz": 97.0}, {}, 0.9, DT)
    assert o["pulse_hz"] == 100.0
    assert m.readout()["last_move"]["axis"] == "pulse_hz"
    assert m.readout()["last_move"]["to"] == 100.0


def test_quantizer_on_event_only_changes_on_onset():
    m = mk(compose=0.0, quant={"pulse_hz": {"step": 10.0, "hold_ms": 100.0, "on": "onset"}})
    m.apply({"pulse_hz": 60.0}, {"E": 0.1}, 0.0, DT)
    # long after the hold, quiet features: still held
    for i in range(1, 120):
        o = m.apply({"pulse_hz": 110.0}, {"E": 0.1}, i * DT, DT)
    assert o["pulse_hz"] == 60.0
    # an onset (E rising edge) releases it
    o = m.apply({"pulse_hz": 110.0}, {"E": 0.6}, 2.0, DT)
    assert o["pulse_hz"] == 110.0
    # explicit cut also counts as an onset
    m2 = mk(compose=0.0, quant={"pulse_hz": {"step": 10.0, "hold_ms": 0.0, "on": "onset"}})
    m2.apply({"pulse_hz": 60.0}, {}, 0.0, DT)
    assert m2.apply({"pulse_hz": 90.0}, {}, 1.0, DT)["pulse_hz"] == 60.0
    assert m2.apply({"pulse_hz": 90.0}, {"cut": True}, 1.1, DT)["pulse_hz"] == 90.0


def test_quantizer_off_passes_through_and_disabled_is_identity():
    m = mk(compose=0.0, quant={"pulse_hz": {"step": 0.0}})
    assert m.apply({"pulse_hz": 83.0}, {}, 0.0, DT)["pulse_hz"] == 83.0
    m = mk(enabled=False)
    t = {"pulse_hz": 83.0, "carrier_hz": 1234.0, "volume": 0.7, "alpha": 0.2, "beta": 0.3}
    assert m.apply(t, {}, 0.0, DT) == t


# ---- carrier grid ---------------------------------------------------------------------------------------

def test_carrier_snaps_to_grid_and_stays_in_band():
    lo, hi = CARRIER_GRID
    for hz in (500.0, 650.0, 700.0, 749.0, 751.0, 1140.0, 1599.0, 1600.0, 1999.0):
        for w in (0.0, 0.6, 1.0):
            v = snap_carrier(hz, w)
            assert lo <= v <= hi
            assert abs(v / 100.0 - round(v / 100.0)) < 1e-9, v
    assert snap_carrier(1140.0, 0.0) == 1100.0
    assert snap_carrier(1140.0, 0.6) < snap_carrier(1140.0, 0.0), "low weighting biases down"
    m = mk(compose=0.0, quant={"carrier_hz": {"on": "any", "hold_ms": 0.0}})
    rng = random.Random(3)
    for i in range(500):
        o = m.apply({"carrier_hz": rng.uniform(500.0, 2000.0)}, {}, i * DT, DT)
        assert lo <= o["carrier_hz"] <= hi and o["carrier_hz"] % 100.0 == 0.0
    # compose picks also live on the grid
    m = mk(compose=1.0, quant={"carrier_hz": {"on": "any", "hold_ms": 0.0}})
    for i in range(300):
        o = m.apply({"carrier_hz": 1300.0}, {}, i * DT, DT)
        assert lo <= o["carrier_hz"] <= hi and o["carrier_hz"] % 100.0 == 0.0


# ---- edge_time controller -------------------------------------------------------------------------------

@pytest.mark.parametrize("target", [0.3, 0.5, 0.7])
def test_edge_time_controller_converges_on_uniform_r(target):
    m = mk(compose=0.0, edge_time=target, dwell_ms=0.0, transit="slide", path="through")
    rng = random.Random(11)

    cur = {}

    def tgt(i):
        if i % 30 == 0:                      # a new uniform-r point every 0.5 s (a real target is not white noise)
            r = rng.random()
            th = rng.uniform(-math.pi, math.pi)
            cur.update({"alpha": r * math.cos(th), "beta": r * math.sin(th)})
        return dict(cur)

    run(m, int(300 / DT), tgt)
    ro = m.readout()
    assert abs(ro["edge_frac_60s"] - target) < 0.08, ro
    # uniform r spends 20% of the time at r >= 0.8 untouched; every target above that needs gamma < 1, and the
    # closed form (P(r^g >= 0.8) = 1 - 0.8^(1/g)) is an upper bound on gamma since transit time is off the rim
    g_closed = math.log(0.8) / math.log(1.0 - target)
    assert ro["gamma"] < 1.0 and ro["gamma"] <= g_closed * 1.15, (ro["gamma"], g_closed)


# ---- volume: dips and fade-fight only ever lower ---------------------------------------------------------

def test_dip_precedes_rise_and_never_exceeds_mapped_volume():
    m = mk(compose=0.0, dip_depth=0.25, dip_ms=300.0, dip_trigger=0.12, fade_fight_s=0.0)
    vols = [0.6] * 60 + [0.9] * 120
    outs = run(m, len(vols), lambda i: {"volume": vols[i]})
    for v, o in zip(vols, outs):
        assert o["volume"] <= v + 1e-12
    # the first ticks after the step are the dip (0.6 * 0.75 = 0.45), then the rise lands
    assert abs(outs[60]["volume"] - 0.45) < 1e-9
    assert abs(outs[70]["volume"] - 0.45) < 1e-9
    assert outs[60 + 20]["volume"] == 0.9   # 300 ms = 18 ticks
    assert m.readout()["dips"] == 1
    # small rises don't dip
    m = mk(compose=0.0, dip_depth=0.25, dip_ms=300.0, dip_trigger=0.12, fade_fight_s=0.0)
    m.apply({"volume": 0.6}, {}, 0.0, DT)
    assert m.apply({"volume": 0.68}, {}, DT, DT)["volume"] == 0.68
    assert m.readout()["dips"] == 0


def test_dips_and_fade_fight_skipped_when_power_drives_volume():
    mv = Moves()
    mv.update({"dip_depth": 0.3, "dip_ms": 300.0, "dip_trigger": 0.1, "fade_fight_s": 1.0})
    t, dt = 0.0, 1 / 60
    mv.apply({"volume": 0.5}, {"E": 0.5}, t, dt, power_volume=True)
    for _ in range(120):                       # a big rise with Power owning volume: no dip, no fade-fight
        t += dt
        out = mv.apply({"volume": 0.9}, {"E": 0.5}, t, dt, power_volume=True)
        assert out["volume"] == 0.9
    assert mv.readout()["dips"] == 0
    mv2 = Moves()
    mv2.update({"dip_depth": 0.3, "dip_ms": 300.0, "dip_trigger": 0.1})
    mv2.apply({"volume": 0.5}, {"E": 0.5}, 0.0, dt)
    out = mv2.apply({"volume": 0.9}, {"E": 0.5}, dt, dt)   # the mapping drives volume: the dip happens
    assert out["volume"] < 0.5 and mv2.readout()["dips"] == 1


def test_fade_fight_only_reduces_and_returns():
    m = mk(compose=0.0, dip_depth=0.0, fade_fight_s=3.0)
    outs = run(m, int(9.0 / DT), lambda i: {"volume": 0.8})     # fires at 3 s, back by 8 s
    vals = [o["volume"] for o in outs]
    assert all(v <= 0.8 + 1e-12 for v in vals)
    assert min(vals) < 0.8 * 0.93, "eased down ~8%"
    assert abs(vals[-1] - 0.8) < 1e-9, "and came back"
    lo_i = vals.index(min(vals))
    assert 3.0 < lo_i * DT < 6.0
    assert vals[lo_i] < vals[lo_i + 60] < 0.8 + 1e-12   # returning over 3 s
    assert m.stats["fades"] == 1
    # never fires when the volume keeps moving
    m = mk(compose=0.0, dip_depth=0.0, fade_fight_s=3.0)
    outs = run(m, int(10 / DT), lambda i: {"volume": 0.5 + 0.3 * ((i // 30) % 2)})
    assert m.stats.get("fades", 0) == 0


def test_volume_never_above_mapping_random_stream():
    m = mk()
    rng = random.Random(5)
    for i in range(3000):
        v = rng.random()
        o = m.apply({"volume": v, "alpha": rng.uniform(-1, 1), "beta": rng.uniform(-1, 1), "pulse_hz": rng.uniform(0, 150),
                     "carrier_hz": rng.uniform(500, 2000)},
                    {"E": rng.random(), "cut": rng.random() < 0.01}, i * DT, DT)
        assert o["volume"] <= v + 1e-12
        for ax in ("volume", "alpha", "beta", "pulse_hz", "carrier_hz"):
            lo, hi = LIMITS[ax]
            assert lo - 1e-9 <= o[ax] <= hi + 1e-9, (ax, o[ax])


# ---- position: path=around -------------------------------------------------------------------------------

def test_path_around_never_crosses_center_between_rim_points():
    m = mk(compose=0.0, edge_time=0.5, dwell_ms=0.0, transit="slide", path="around", dip_depth=0.0)
    m.gamma = 1.0
    # settle on the rim at angle 0, then ask for the opposite rim point
    for i in range(30):
        m.apply({"alpha": 1.0, "beta": 0.0}, {}, i * DT, DT)
    rs = []
    for i in range(30, 200):
        o = m.apply({"alpha": -1.0, "beta": 0.05}, {}, i * DT, DT)
        rs.append(math.hypot(o["alpha"], o["beta"]))
    assert min(rs) >= 0.5, min(rs)
    assert m.apply({"alpha": -1.0, "beta": 0.05}, {}, 200 * DT, DT)["alpha"] < -0.9, "arrived"
    # path=through does go through the middle
    m = mk(compose=0.0, dwell_ms=0.0, transit="slide", path="through", dip_depth=0.0)
    m.gamma = 1.0
    for i in range(30):
        m.apply({"alpha": 1.0, "beta": 0.0}, {}, i * DT, DT)
    rs = [math.hypot(*(lambda o: (o["alpha"], o["beta"]))(m.apply({"alpha": -1.0, "beta": 0.0}, {}, i * DT, DT)))
          for i in range(30, 200)]
    assert min(rs) < 0.2


def test_jump_transit_holds_until_event_and_dwell_parks():
    m = mk(compose=0.0, transit="jump", dwell_ms=500.0, dwell_style="still", dip_depth=0.0)
    m.gamma = 1.0
    m.apply({"alpha": 1.0, "beta": 0.0}, {"E": 0.1}, 0.0, DT)
    o = m.apply({"alpha": 0.0, "beta": 1.0}, {"E": 0.1}, 1.0, DT)
    assert o["alpha"] == pytest.approx(1.0) and o["beta"] == pytest.approx(0.0), "held: no event"
    o = m.apply({"alpha": 0.0, "beta": 1.0}, {"E": 0.9}, 2.0, DT)
    assert o["beta"] == pytest.approx(1.0), "jumped on the onset"
    assert m.readout()["jumps"] == 1 and m.readout()["dwelling"]


# ---- step / demo -----------------------------------------------------------------------------------------

def test_step_and_demo_offsets():
    m = mk(compose=0.0, quant={"pulse_hz": {"on": "any", "hold_ms": 0.0}})
    assert m.step("pulse_hz", +1) == 10.0
    assert m.step("pulse_hz", +1) == 20.0
    assert m.step("pulse_hz", -1) == 10.0
    assert m.apply({"pulse_hz": 80.0}, {}, 0.0, DT)["pulse_hz"] == 90.0
    assert m.step("edge_time", +1) == 0.75
    assert m.step("edge_time", +1) == 1.0
    with pytest.raises(ValueError):
        m.step("pulse_width", 1)
    m.start_demo("carrier_hz", 10.0, 1000.0, seconds=6.0)
    ax, v = m.demo_value(10.0)
    assert ax == "carrier_hz" and v == 1000.0
    ax, v = m.demo_value(12.0)
    assert v == 700.0            # min at 1/3
    ax, v = m.demo_value(14.0)
    assert v == 1600.0           # max at 2/3
    ax, v = m.demo_value(16.5)
    assert v == 1000.0 and not m.demo_running


# ---- presets carry [moves] -------------------------------------------------------------------------------

def test_preset_roundtrip_moves_table(tmp_path):
    ms = MappingSet(name="x")
    ms.moves = Moves()
    ms.moves.update({"edge_time": 0.7, "dwell_style": "drift", "quant": {"pulse_hz": {"hold_ms": 3000.0, "on": "beat"}}})
    ms.moves.step("pulse_hz", +1)
    ms.save_preset("x", tmp_path)
    text = (tmp_path / "x.toml").read_text()
    assert "[moves]" in text and "[moves.quant.pulse_hz]" in text
    ms2 = MappingSet(name="y")
    ms2.moves = Moves()
    ms2.load_preset("x", tmp_path)
    d = ms2.moves.to_dict()
    assert d["edge_time"] == 0.7 and d["dwell_style"] == "drift"
    assert d["quant"]["pulse_hz"]["hold_ms"] == 3000.0 and d["quant"]["pulse_hz"]["on"] == "beat"
    assert d["offsets"]["pulse_hz"] == 0.0, "a preset load clears step offsets"


def test_builtin_cards_are_far_apart(tmp_path):
    paths = write_builtin_cards(tmp_path)
    assert sorted(p.stem for p in paths) == ["corners", "drive", "jumpy", "random", "slow-burn", "wash"]
    import tomllib
    cards = {p.stem: tomllib.loads(p.read_text()) for p in paths}
    assert all(c["card"] is True and c["blurb"] for c in cards.values())
    assert cards["wash"]["moves"]["edge_time"] < 0.2 < 0.6 < cards["corners"]["moves"]["edge_time"]
    assert cards["jumpy"]["moves"]["transit"] == "jump" and cards["jumpy"]["moves"]["compose"] >= 0.9
    assert cards["slow-burn"]["moves"]["quant"]["pulse_hz"]["hold_ms"] == 3000.0
    assert cards["drive"]["moves"]["quant"]["volume"]["step"] == 0.1
    assert cards["random"]["moves"]["beat_lock"] == "random"
    for c in cards.values():   # motion only (notes/power.md Amendment 2)
        assert set(c["axes"]) == {"alpha", "beta"}
        assert "intensity" not in c["macros"] and "power" not in c


# ---- engine + API ----------------------------------------------------------------------------------------

@run_async
async def test_engine_step_and_demo_when_follow_idle(tmp_path):
    from tests.test_engine_core import started_engine
    t, dev, eng, session = await started_engine(tmp_path)
    eng.set_pulse(frequency=80.0)
    eng.set_carrier(1000.0)
    assert eng.moves_step("pulse_hz", +1) == 90.0
    assert eng.frame.pulse_frequency == 90.0
    assert eng.moves_step("carrier_hz", -1) == 900.0
    assert eng.frame.carrier_frequency == 900.0
    v0 = eng.status()["api_volume"]
    v1 = eng.moves_step("volume", -1)
    assert v1 == pytest.approx(v0 - 0.10) and eng.status()["api_volume"] == pytest.approx(v1)
    assert eng.moves_step("edge_time", -1) == 0.25
    assert eng.status()["follow"]["moves"]["edge_time"] == 0.25
    eng.moves_demo("carrier_hz", seconds=2.0)
    seen = []
    for _ in range(50):
        await asyncio.sleep(0.05)
        seen.append(eng.frame.carrier_frequency)
    assert min(seen) <= 800.0 and max(seen) >= 1500.0, seen      # swung min -> max ...
    assert eng.frame.carrier_frequency == 900.0 and not eng.moves.demo_running   # ... and back to current
    assert all(v % 100.0 == 0.0 for v in seen), "demo stays on the carrier grid"
    await eng.stop()
    dev.stop()




def _beat_stream(mv: Moves, seconds: float, tempo: float = 2.0, dt: float = 1 / 60, locked: bool = True):
    t, out = 0.0, []
    for _ in range(int(seconds / dt)):
        t += dt
        ph = (t * tempo) % 1.0
        f = {"tempo_hz": tempo if locked else 0.0, "E": 0.5, "bar_phase": ((int(t * tempo) % 4) + ph) / 4}
        if locked:
            f["beat_phase"] = ph
        o = mv.apply({"alpha": 0.1, "beta": 0.1, "volume": 0.7}, f, t, dt)
        out.append((t, o["alpha"], o["beta"]))
    return out


def test_beat_lock_step_moves_on_beats_and_falls_back():
    mv = Moves()
    mv.update({"beat_lock": "step", "beat_div": 1.0, "bar_accent": False})
    out = _beat_stream(mv, 6.0)
    r = mv.readout()["beat"]
    assert r["locked"] and abs(r["bpm"] - 120.0) < 0.1
    assert 10 <= r["moves"] <= 13                       # one move per beat at 120 bpm for 6 s
    rims = [math.hypot(a, b) for t, a, b in out if abs((t * 2.0) % 1.0 - 0.5) < 0.02]
    assert rims and min(rims) > 0.9                     # mid-beat the dot sits ON the rim
    mv2 = Moves(); mv2.update({"beat_lock": "step"})
    _beat_stream(mv2, 2.0, locked=False)
    assert mv2.readout()["beat"]["locked"] is False     # no trusted tempo -> card motion


def test_beat_lock_bounce_alternates_sides():
    mv = Moves()
    mv.update({"beat_lock": "bounce", "beat_div": 1.0, "bar_accent": False})
    out = _beat_stream(mv, 4.0)
    mids = [(a, b) for t, a, b in out if abs((t * 2.0) % 1.0 - 0.6) < 0.01]
    for (a1, b1), (a2, b2) in zip(mids, mids[1:]):
        assert a1 * a2 + b1 * b2 < -0.5                 # opposite sides beat to beat


def test_beat_lock_div_halves_move_rate():
    mv = Moves(); mv.update({"beat_lock": "step", "beat_div": 2.0, "bar_accent": False})
    _beat_stream(mv, 6.0)
    assert 5 <= mv.readout()["beat"]["moves"] <= 7


def _angle(a: float, b: float) -> float:
    return math.degrees(math.atan2(b, a))


def _turn(a1: float, a2: float) -> float:
    return abs((a2 - a1 + 180.0) % 360.0 - 180.0)


def test_beat_lock_random_jumps_per_beat_at_least_90_degrees_apart():
    mv = Moves(seed=7)
    mv.update({"beat_lock": "random", "beat_div": 1.0, "dwell_style": "still", "bar_accent": False})
    out = _beat_stream(mv, 8.0)                          # 120 bpm
    r = mv.readout()["beat"]
    assert r["locked"] and abs(r["bpm"] - 120.0) < 0.1
    assert 14 <= r["moves"] <= 17                        # one jump per beat
    mids = [(a, b) for t, a, b in out if abs((t * 2.0) % 1.0 - 0.6) < 0.01]
    assert all(math.hypot(a, b) > 0.95 for a, b in mids)   # rim points (r = 1)
    angs = [_angle(a, b) for a, b in mids]
    turns = [_turn(x, y) for x, y in zip(angs, angs[1:])]
    assert turns and all(tn >= 89.0 for tn in turns), turns
    assert len(set(round(x) for x in angs)) > 4          # random, not a fixed spoke pattern
    # beat_div 2: half the jumps
    mv2 = Moves(seed=3)
    mv2.update({"beat_lock": "random", "beat_div": 2.0, "dwell_style": "still", "bar_accent": False})
    _beat_stream(mv2, 8.0)
    assert 7 <= mv2.readout()["beat"]["moves"] <= 9


def test_beat_lock_random_uses_onsets_when_there_is_no_beat():
    mv = Moves(seed=5)
    mv.update({"beat_lock": "random", "dwell_style": "still", "bar_accent": False})
    t, dt, prev, jumps_at = 0.0, 1 / 60, None, []
    for i in range(int(6.0 / dt)):
        t += dt
        E = 0.9 if (i % 60) < 6 else 0.2                 # an E rising edge (onset) once per second, no tempo
        o = mv.apply({"alpha": 0.1, "beta": 0.1}, {"E": E, "tempo_hz": 0.0}, t, dt)
        cur = (round(o["alpha"], 3), round(o["beta"], 3))
        if prev is not None and math.hypot(cur[0] - prev[0], cur[1] - prev[1]) > 0.5:
            jumps_at.append(t)
        prev = cur
    r = mv.readout()["beat"]
    assert r["locked"] is False and "onsets" in (r["why"] or "")
    assert 5 <= r["moves"] <= 7, r                       # one jump per onset
    assert all(abs(t % 1.0) < 0.15 or abs(t % 1.0 - 1.0) < 0.15 for t in jumps_at), jumps_at


def test_quantizer_overdue_release_without_events():
    mv = Moves()
    mv.update({"quant": {"carrier_hz": {"step": 100, "hold_ms": 800, "on": "onset"}}})
    t, dt = 0.0, 1 / 60
    mv.apply({"carrier_hz": 700.0}, {"E": 0.5}, t, dt)
    for _ in range(int(2.0 / dt)):                      # 2 s: still held (no onset, not overdue yet)
        t += dt
        out = mv.apply({"carrier_hz": 900.0}, {"E": 0.5}, t, dt)
    assert out["carrier_hz"] == 700.0
    for _ in range(int(2.0 / dt)):                      # >= 3.2 s total: overdue -> released
        t += dt
        out = mv.apply({"carrier_hz": 900.0}, {"E": 0.5}, t, dt)
    assert out["carrier_hz"] == 900.0
    assert mv.readout()["last_move"]["why"] == "overdue"


def test_clear_offsets_drops_held_values():
    mv = Moves()
    mv.apply({"carrier_hz": 700.0}, {"E": 0.5}, 0.0, 1 / 60)
    mv.clear_offsets()
    out = mv.apply({"carrier_hz": 900.0}, {"E": 0.5}, 0.1, 1 / 60)
    assert out["carrier_hz"] == 900.0
