"""Point map (stimengine/content/points.py): weights, ratings, sampling, persistence; Moves tune mode; the engine's
latency-compensated rating credit; the /points API."""
from __future__ import annotations

import asyncio
import functools
import math

import pytest

from stimengine.content.moves import Moves, MovesParams
from stimengine.content.points import CANDIDATES, NAMES, NEUTRAL, RATE_STEP, PointMap

DT = 1.0 / 60.0


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


# ---- PointMap ----------------------------------------------------------------------------------------------

def test_candidates_geometry():
    assert len(NAMES) == 17 and "center" in NAMES
    assert all(math.hypot(*CANDIDATES[f"rim{k}"]) == pytest.approx(1.0) for k in range(8))
    assert all(math.hypot(*CANDIDATES[f"mid{k}"]) == pytest.approx(0.6) for k in range(8))
    assert PointMap.nearest(0.98, 0.02) == "rim0"
    assert PointMap.nearest(0.0, 0.62) == "mid2"
    assert PointMap.nearest(0.05, -0.05) == "center"
    assert PointMap.nearest(0.5, 0.5) is None or math.hypot(0.5, 0.5) < 1.0   # (0.5,0.5) is ~0.13 from mid1 -> credited


def test_rate_steps_clamps_counts_and_reset():
    pm = PointMap(seed=1)
    assert pm.rate("rim3", +1, now=1.0) == pytest.approx(NEUTRAL + RATE_STEP)
    for _ in range(10):
        pm.rate("rim3", +1)
    assert pm.weights["rim3"] == 1.0 and pm.yes["rim3"] == 11
    for _ in range(20):
        pm.rate("mid5", -1)
    assert pm.weights["mid5"] == 0.0 and pm.no["mid5"] == 20
    assert pm.top(2)[0] == "rim3" and pm.last_rating["point"] == "mid5"
    with pytest.raises(KeyError):
        pm.rate("nope", 1)
    v = pm.version
    pm.reset()
    assert pm.version > v and all(w == NEUTRAL for w in pm.weights.values()) and pm.yes["rim3"] == 0


def test_pick_follows_weights_and_explore_floor():
    pm = PointMap(seed=3, explore=0.0)
    for n in NAMES:
        pm.set_weight(n, 0.0)
    pm.set_weight("rim1", 1.0)
    pm.set_weight("rim5", 1.0)
    picks = [pm.pick() for _ in range(200)]
    assert set(picks) == {"rim1", "rim5"}
    assert all(pm.pick(exclude="rim1") == "rim5" for _ in range(20))
    assert all(pm.pick(rim_only=True).startswith("rim") for _ in range(50))
    pm.explore = 1.0                                     # pure exploration: everything comes back
    assert len(set(pm.pick() for _ in range(400))) == len(NAMES)
    pm2 = PointMap(seed=3, explore=0.0)                  # all-zero weights degrade to uniform, never crash
    for n in NAMES:
        pm2.set_weight(n, 0.0)
    assert pm2.pick() in NAMES


def test_decay_drifts_toward_neutral_only_when_enabled():
    pm = PointMap(decay_s=100.0)
    pm.rate("rim0", +1)
    pm.rate("rim4", -1)
    pm.tick(0.0)
    pm.tick(50.0)
    assert NEUTRAL < pm.weights["rim0"] < NEUTRAL + RATE_STEP
    assert NEUTRAL - RATE_STEP < pm.weights["rim4"] < NEUTRAL
    hi = pm.weights["rim0"]
    pm.decay_s = 0.0
    pm.tick(1000.0)
    assert pm.weights["rim0"] == hi


def test_save_load_roundtrip_per_placement(tmp_path):
    pm = PointMap(placement="ring-a", seed=1)
    pm.rate("rim2", +1); pm.rate("rim2", +1); pm.rate("center", -1)
    p = pm.save(tmp_path)
    assert p.name == "ring-a.json" and not pm.dirty
    pm2 = PointMap()
    assert pm2.load(tmp_path, placement="ring-a") is True
    assert pm2.weights["rim2"] == pytest.approx(pm.weights["rim2"]) and pm2.no["center"] == 1
    assert pm2.load(tmp_path, placement="ring-b") is False        # unknown placement -> neutral, no error
    assert all(w == NEUTRAL for w in pm2.weights.values()) and pm2.placement == "ring-b"
    d = pm2.to_dict()
    assert d["placement"] == "ring-b" and len(d["points"]) == 17 and d["points"]["rim0"]["alpha"] == 1.0


# ---- Moves: tune mode + map-weighted picks -----------------------------------------------------------------

def _walk(m: Moves, seconds: float, onset_every: float | None = None):
    out = []
    n = int(seconds / DT)
    for i in range(n):
        now = i * DT
        onset = 1.0 if (onset_every and i % int(round(onset_every / DT)) == 0) else 0.0
        o = m.apply({"alpha": 0.0, "beta": 0.0, "volume": 0.5}, {"onset": onset, "energy": 0.5}, now, DT)
        out.append((now, o["alpha"], o["beta"]))
    return out


def test_tune_walks_every_candidate_before_repeating_and_holds():
    m = Moves(MovesParams().updated({"tune": True, "tune_hold_ms": 1000.0}), seed=5)
    out = _walk(m, 30.0)                           # no onsets -> moves at 1.5 x hold = 1.5 s each
    names = []
    for t, a, b in out:
        n = PointMap.nearest(a, b, max_d=0.01)
        assert n is not None, "tune sits exactly on a candidate"
        if not names or names[-1] != n:
            names.append(n)
    assert len(names) >= 18 and set(names[:17]) == set(NAMES), names   # first cycle covers all 17, no repeat
    st = m.readout()["tune"]
    assert st["on"] and st["cycles"] >= 1 and st["point"] in NAMES and st["total"] == 17
    # hold: every visit lasted >= hold_ms
    visits = []
    for t, a, b in out:
        n = PointMap.nearest(a, b, max_d=0.01)
        if not visits or visits[-1][0] != n:
            visits.append([n, t, t])
        else:
            visits[-1][2] = t
    assert all((v[2] - v[1]) >= 1.0 - DT for v in visits[:-1])


def test_tune_advances_on_onset_after_hold_and_works_when_moves_disabled():
    m = Moves(MovesParams().updated({"tune": True, "tune_hold_ms": 1000.0, "enabled": False}), seed=5)
    out = _walk(m, 12.0, onset_every=0.4)          # onsets every 0.4 s: moves at the first onset >= 1.0 s -> 1.2 s
    changes = [t for (t, a, b), (t2, a2, b2) in zip(out, out[1:]) if (a, b) != (a2, b2)]
    gaps = [y - x for x, y in zip(changes, changes[1:])]
    assert gaps and all(abs(g - 1.2) < 0.05 for g in gaps), gaps
    assert m.readout()["last_move"]["why"] == "tune"


def test_map_weights_steer_compose_and_beat_picks():
    m = Moves(MovesParams().updated({"compose": 1.0, "quant": {"alpha": {"on": "any", "hold_ms": 0.0}},
                                      "transit": "jump", "dwell_ms": 0.0}), seed=2)
    m.points.explore = 0.0
    for n in NAMES:
        m.points.set_weight(n, 0.0)
    m.points.set_weight("rim6", 1.0)
    m.points.set_weight("mid2", 1.0)
    seen = set()
    for i in range(300):
        o = m.apply({"alpha": 0.3, "beta": 0.3}, {"onset": 1.0 if i % 10 == 0 else 0.0}, i * DT, DT)
        n = PointMap.nearest(o["alpha"], o["beta"], max_d=0.05)
        if n:
            seen.add(n)
    assert seen and seen <= {"rim6", "mid2"}, seen
    # beat step: only rim spokes >= 90 deg away, weighted -> with a single hot spoke it alternates hot/anything
    m2 = Moves(MovesParams().updated({"beat_lock": "step", "bar_accent": False}), seed=2)
    m2.points.explore = 0.0
    for n in NAMES:
        m2.points.set_weight(n, 0.0)
    m2.points.set_weight("rim0", 1.0)
    m2.points.set_weight("rim4", 1.0)
    pts = set()
    for i in range(600):
        now = i * DT
        f = {"tempo_hz": 2.0, "beat_phase": (now * 2.0) % 1.0, "beat": 1.0 if abs((now * 2.0) % 1.0) < DT else 0.0}
        o = m2.apply({"alpha": 0.0, "beta": 0.0}, f, now, DT)
        if abs((now * 2.0) % 1.0 - 0.6) < 0.01:
            pts.add(PointMap.nearest(o["alpha"], o["beta"], max_d=0.1))
    assert pts <= {"rim0", "rim4"}, pts
    # point_map off -> legacy uniform behaviour still runs
    m3 = Moves(MovesParams().updated({"beat_lock": "step", "point_map": False}), seed=2)
    assert m3.readout()["point_map"] is False


# ---- engine: latency-compensated credit ---------------------------------------------------------------------

@run_async
async def test_engine_rating_credits_point_active_latency_ago(tmp_path):
    from tests.test_engine_core import started_engine
    t, dev, eng, session = await started_engine(tmp_path)
    eng.set_position(1.0, 0.0)                       # rim0
    await asyncio.sleep(0.8)
    eng.set_position(-1.0, 0.0)                      # rim4 now
    await asyncio.sleep(0.15)
    r = eng.points_rate(+1)                          # 0.6 s ago we were still on rim0
    assert r["ok"] and r["point"] == "rim0" and r["weight"] == pytest.approx(NEUTRAL + RATE_STEP)
    r2 = eng.points_rate(-1, latency_s=0.0)          # right now: rim4
    assert r2["ok"] and r2["point"] == "rim4" and r2["weight"] == pytest.approx(NEUTRAL - RATE_STEP)
    eng.set_position(0.5, 0.5)                       # between mid1 (0.42,0.42) and rim1 (0.71,0.71) -> nearest mid1
    await asyncio.sleep(0.1)
    assert eng.points_rate(+1, latency_s=0.0)["point"] == "mid1"
    st = eng.status()["follow"]
    assert st["points"]["weights"]["rim0"] == pytest.approx(NEUTRAL + RATE_STEP) and st["active_point"] == "mid1"
    # tune with follow idle: the engine writes the walk to the frame
    eng.moves.update({"tune": True, "tune_hold_ms": 500.0})
    await asyncio.sleep(2.2)                         # external position yields for 2 s first
    seen = set()
    for _ in range(40):
        await asyncio.sleep(0.05)
        seen.add(PointMap.nearest(eng.frame.alpha, eng.frame.beta, max_d=0.01))
    assert None not in seen and len(seen) >= 2, seen
    await eng.stop()
    dev.stop()


# ---- API ---------------------------------------------------------------------------------------------------

