"""Power lanes (stimengine/content/power.py, contract notes/power.md): envelope follower, build, quiet vs breakdown,
surge, safety bounds, normalization, plateau, per-show / scene reset, carrier lane on the grid, presets
([power.<axis>] tables, cards never carry them), engine wiring, and the /power API."""
from __future__ import annotations

import asyncio
import functools
import math

import pytest

from stimengine.content.mapping import LIMITS, MappingSet, write_builtin_cards
from stimengine.content.moves import Moves
from stimengine.content.power import AXES, Power, PowerLane, PowerParams

DT = 1.0 / 60.0


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


def lane(axis: str = "volume", **kw) -> PowerLane:
    p = PowerParams.for_axis(axis).updated(dict(mode="build", **kw), axis)
    return PowerLane(axis, p)


def quiet_lane(axis: str = "volume", **kw) -> PowerLane:
    """A lane with the extras off (pure follower + build) unless overridden."""
    base = dict(surge=False, breakdown=False, rim_sidechain=False, beat_accent=False, plateau=False, per_show_pct=0.0)
    base.update(kw)
    return lane(axis, **base)


def seed(ln: PowerLane, t0: float = 0.0, seconds: float = 6.0, lo: float = 0.2, hi: float = 0.8) -> float:
    """Feed a square wave of energy lo/hi so p10..p90 ~ lo..hi; returns the time after seeding."""
    n = int(seconds / DT)
    for i in range(n):
        E = hi if (i // 15) % 2 else lo
        ln.apply(None, {"E": E}, (0.0, 0.0), t0 + i * DT, DT)
    return t0 + n * DT


def run(ln: PowerLane, t0: float, seconds: float, feats_fn, pos=(0.0, 0.0)) -> list[float]:
    out = []
    n = int(seconds / DT)
    for i in range(n):
        now = t0 + i * DT
        out.append(ln.apply(None, feats_fn(i, now), pos, now, DT))
    return out


# ---- follower -------------------------------------------------------------------------------------------

def test_manual_returns_none():
    ln = PowerLane("volume")
    assert ln.apply(0.7, {"E": 0.9}, (0, 0), 0.0, DT) is None
    assert ln.readout()["state"] == "idle" and ln.readout()["out"] is None


def test_envelope_rises_with_attack_and_falls_with_release():
    ln = quiet_lane(speed="musical", build_rate_pct_per_min=0.0, min=0.0, max=1.0)
    t = seed(ln)
    # step to loud: env reaches ~63% of the way in one attack tau (0.15 s)
    ln.env = 0.0
    run(ln, t, 0.15, lambda i, now: {"E": 0.8})
    assert 0.5 < ln.env < 0.8
    run(ln, t + 0.15, 1.5, lambda i, now: {"E": 0.8})
    assert ln.env > 0.95
    # step to mid (not below breakdown/quiet): falls with release tau 2.0 -> after 2 s ~ 63% of the drop
    t2 = t + 1.65
    run(ln, t2, 2.0, lambda i, now: {"E": 0.5})
    assert 0.55 < ln.env < 0.75
    run(ln, t2 + 2.0, 10.0, lambda i, now: {"E": 0.5})
    assert ln.env == pytest.approx(0.5, abs=0.05)


def test_build_reaches_max_at_stated_rate():
    ln = quiet_lane(min=0.5, max=1.0, build_rate_pct_per_min=10.0)   # 10 %/min of range 1.0: 0.6 -> 1.0 in 4 min
    assert ln.max_now == pytest.approx(0.6)
    r = ln.readout()
    assert r["minutes_to_max"] == pytest.approx(4.0, rel=0.01)
    t = seed(ln)
    run(ln, t, 60.0 * 4.0 * 0.95 - 6.0, lambda i, now: {"E": 0.8})
    assert ln.max_now < 1.0
    run(ln, t + 60.0 * 4.0 * 0.95 - 6.0, 60.0 * 4.0 * 0.10, lambda i, now: {"E": 0.8})
    assert ln.max_now == pytest.approx(1.0)


def test_build_rate_zero_means_max_at_once():
    ln = quiet_lane(min=0.3, max=0.9, build_rate_pct_per_min=0.0)
    assert ln.max_now == 0.9


def test_quiet_fade_is_slow_and_breakdown_is_fast():
    slow = quiet_lane(build_rate_pct_per_min=0.0, min=0.0, max=1.0, quiet_fade_s=8.0)
    fast = quiet_lane(build_rate_pct_per_min=0.0, min=0.0, max=1.0, breakdown=True, breakdown_thresh=0.25)
    for ln in (slow, fast):
        t = seed(ln)
        run(ln, t, 2.0, lambda i, now: {"E": 0.8})
        assert ln.env > 0.95
    # music gone (E far below p10 -> en 0 < quiet_thresh): slow lane fades on quiet_fade_s
    t = 8.0
    run(slow, t, 1.0 + 8.0, lambda i, now: {"E": 0.0})
    assert slow.readout()["state"] == "quiet"
    assert 0.15 < slow.env < 0.5, slow.env            # 1 s normal release, then one quiet tau (8 s): ~e^-1.5
    # music present but low (en ~ 0.22 between quiet 0.12 and breakdown 0.25): fast lane drops in 0.4 s
    run(fast, t, 1.0 + 1.2, lambda i, now: {"E": 0.33})
    assert fast.readout()["state"] == "breakdown"
    assert fast.env < 0.1
    assert fast.readout()["breakdowns"] == 1
    # returns with the attack
    run(fast, t + 2.2, 1.0, lambda i, now: {"E": 0.8})
    assert fast.env > 0.9


def test_surge_jumps_to_max_now_and_holds_then_releases():
    ln = quiet_lane(build_rate_pct_per_min=5.0, min=0.2, max=1.0, surge=True, peak_hold_s=1.0, speed="slow")
    t = seed(ln)
    run(ln, t, 30.0, lambda i, now: {"E": 0.5})         # settle mid (slow taus)
    assert ln.env < 0.7
    t += 30.0
    n0 = ln.readout()["surges"]                          # the seed's square wave already surged a few times
    # an onset (onset strength rising through 1.0) at high normalized energy
    out = run(ln, t, 0.5, lambda i, now: {"E": 0.9, "onset": 2.0 if i == 0 else 0.0})
    assert ln.readout()["surges"] == n0 + 1 and ln.readout()["state"] == "surge"
    assert all(v == pytest.approx(ln.max_now, abs=0.05) for v in out[1:])
    assert out[-1] <= ln.max_now
    # after the hold, drop the energy: releases with the slow release (6 s), i.e. still high at +0.5 s
    run(ln, t + 0.5, 1.0, lambda i, now: {"E": 0.5})
    assert ln.readout()["state"] == "following"
    assert ln.env > 0.85


def test_output_never_exceeds_max_now_nor_drops_below_min():
    ln = lane("volume", min=0.4, max=0.9, build_rate_pct_per_min=15.0, rim_gain=0.5, accent=0.5)
    t = seed(ln)
    import random
    rng = random.Random(3)
    for i in range(60 * 60):
        now = t + i * DT
        f = {"E": rng.random(), "onset": rng.choice([0.0, 0.0, 2.0]), "beat_phase": (i / 20.0) % 1.0,
             "cut": rng.random() < 0.01}
        v = ln.apply(None, f, (rng.uniform(-1, 1), rng.uniform(-1, 1)), now, DT)
        assert 0.4 - 1e-9 <= v <= ln.max_now + 1e-9, (i, v, ln.max_now)
        assert 0.4 <= ln.max_now <= 0.9 + 1e-9


def test_normalization_maps_p10_p90_to_0_1():
    ln = quiet_lane(build_rate_pct_per_min=0.0, min=0.0, max=1.0)
    import random
    rng = random.Random(11)
    samples = [0.3 + 0.2 * rng.random() for _ in range(60 * 40)]   # a quiet-ish scene: E in 0.3..0.5
    for i, E in enumerate(samples):
        ln.apply(None, {"E": E}, (0, 0), i * DT, DT)
    s = sorted(samples)
    p10, p90 = s[int(0.1 * len(s))], s[int(0.9 * len(s))]
    assert ln._p10 == pytest.approx(p10, abs=0.03) and ln._p90 == pytest.approx(p90, abs=0.03)
    t = len(samples) * DT
    ln.apply(None, {"E": p90}, (0, 0), t, DT)
    assert ln.en > 0.9
    ln.apply(None, {"E": p10}, (0, 0), t + DT, DT)
    assert ln.en < 0.1
    # a LOUD scene swings the same full range after on_new_scene (normalization restarts)
    ln.on_new_scene(first=False)
    loud = [0.8 + 0.15 * rng.random() for _ in range(60 * 20)]
    for i, E in enumerate(loud):
        ln.apply(None, {"E": E}, (0, 0), t + 1.0 + i * DT, DT)
    ln.apply(None, {"E": 0.94}, (0, 0), t + 100.0, DT)
    assert ln.en > 0.85


def test_plateau_steps_down_only_after_plateau_min_at_max():
    ln = quiet_lane(min=0.5, max=1.0, build_rate_pct_per_min=20.0, plateau=True, plateau_min=1.0, plateau_drop=0.10)
    t = seed(ln)
    # 20 %/min: 0.6 -> 1.0 in 2 min (the seed's 6 s count: at max from t ~ 120 s)
    run(ln, t, 125.0, lambda i, now: {"E": 0.8})
    assert ln.max_now == pytest.approx(1.0) and ln.readout()["plateaus"] == 0
    t += 125.0
    run(ln, t, 40.0, lambda i, now: {"E": 0.8})            # ~51 s at max: not yet
    assert ln.max_now == pytest.approx(1.0) and ln.readout()["plateaus"] == 0
    t += 40.0
    run(ln, t, 15.0, lambda i, now: {"E": 0.8})            # past 60 s: step down 0.10 over 3 s
    assert ln.readout()["plateaus"] == 1
    assert ln.max_now < 1.0
    t += 15.0
    run(ln, t, 5.0, lambda i, now: {"E": 0.8})
    assert ln.max_now > 0.9                                # ... and the build climbs again


def test_per_show_raises_max_and_scene_reset_restarts_build():
    ln = quiet_lane(min=0.5, max=0.8, build_rate_pct_per_min=20.0, per_show_pct=5.0, build_reset="scene")
    t = seed(ln)
    run(ln, t, 60.0, lambda i, now: {"E": 0.8})
    assert ln.max_now == pytest.approx(0.8)
    ln.on_new_scene(first=False)
    assert ln.params.max == pytest.approx(0.85)
    assert ln.max_now == pytest.approx(0.6)                # build restarted at min + 0.10
    # session reset keeps the build; per-show caps at hi
    ln2 = quiet_lane(min=0.5, max=0.98, build_rate_pct_per_min=0.0, per_show_pct=5.0, build_reset="session")
    ln2.on_new_scene(first=False)
    assert ln2.params.max == 1.0 and ln2.max_now == 1.0
    # the FIRST scene of a session does not raise
    ln3 = quiet_lane(min=0.5, max=0.8, per_show_pct=5.0)
    ln3.on_new_scene(first=True)
    assert ln3.params.max == 0.8


def test_beat_accent_prefers_downbeat_and_rim_sidechain_adds():
    ln = quiet_lane(build_rate_pct_per_min=0.0, min=0.0, max=1.0, beat_accent=True, accent=0.2, accent_ms=100.0,
                    rim_sidechain=True, rim_gain=0.1)
    t = seed(ln)
    run(ln, t, 3.0, lambda i, now: {"E": 0.5})
    base = ln.apply(None, {"E": 0.5, "beat_phase": 0.9, "bar_phase": 0.5}, (0, 0), t + 3.0, DT)
    v = ln.apply(None, {"E": 0.5, "beat_phase": 0.1, "bar_phase": 0.6}, (0, 0), t + 3.0 + DT, DT)   # beat, not downbeat
    assert ln.readout()["accents"] == 0 and v == pytest.approx(base, abs=0.02)
    v = ln.apply(None, {"E": 0.5, "beat_phase": 0.1, "bar_phase": 0.05}, (0, 0), t + 3.0 + 2 * DT, DT)   # downbeat
    assert ln.readout()["accents"] == 1 and v == pytest.approx(base + 0.2, abs=0.03)
    run(ln, t + 4.0, 1.0, lambda i, now: {"E": 0.5})
    center = ln.apply(None, {"E": 0.5}, (0.0, 0.0), t + 5.0, DT)
    rim = ln.apply(None, {"E": 0.5}, (1.0, 0.0), t + 5.0 + DT, DT)
    assert rim == pytest.approx(center + 0.1, abs=0.02)


# ---- carrier lane ------------------------------------------------------------------------------------------

def test_carrier_lane_snaps_to_grid_and_stays_within_range():
    ln = quiet_lane("carrier_hz", min=750.0, max=1440.0, build_rate_pct_per_min=30.0, cruise_frac=1.0)
    assert (ln.params.min, ln.params.max) == (800.0, 1400.0)          # snapped to the 100 Hz grid (half-up)
    assert ln.params.build_rate_pct_per_min == 20.0                     # contract: 0..20 %/min
    assert ln.max_now == pytest.approx(800.0 + 0.10 * 900.0)            # +10 % of the 700..1600 range
    t = seed(ln)
    outs = run(ln, t, 190.0, lambda i, now: {"E": 0.8 if (i // 30) % 2 else 0.3})   # 510 Hz at 180 Hz/min ~ 170 s
    assert all(800.0 <= v <= 1400.0 for v in outs)
    assert all(v % 100.0 == 0.0 for v in outs), "a carrier lane only ever emits grid values (felt steps)"
    assert max(outs) == 1400.0 and min(outs) < 1000.0
    assert ln.readout()["minutes_to_max"] == 0.0
    # validation clamps to the lane range regardless of what the request says
    p = PowerParams.for_axis("pulse_hz").updated({"min": 5.0, "max": 500.0}, "pulse_hz")
    assert (p.min, p.max) == (30.0, 150.0)
    p = PowerParams.for_axis("pulse_width").updated({"min": 6.3, "max": 30.0}, "pulse_width")
    assert (p.min, p.max) == (6.5, 20.0)


def test_power_container_applies_only_active_lanes_and_resets():
    pw = Power()
    assert pw.any_active and pw.to_dict()["active"] == ["volume", "carrier_hz"]   # volume boots "sound", carrier "drift"
    pw.update({"mode": "manual"})
    pw.update({"axis": "carrier_hz", "mode": "manual"})
    assert not pw.any_active
    tg, drove = pw.apply({"volume": 0.3, "carrier_hz": 900.0}, {"E": 0.5}, (0, 0), 0.0, DT)
    assert tg == {"volume": 0.3, "carrier_hz": 900.0} and drove == []
    pw.update({"mode": "sound", "min": 0.4, "max": 0.9})                    # default axis: volume
    pw.update({"axis": "carrier_hz", "mode": "build", "min": 800.0, "max": 1200.0, "build_rate_pct_per_min": 0.0})
    assert pw.to_dict()["active"] == ["volume", "carrier_hz"]
    for i in range(300):
        tg, drove = pw.apply({"volume": 0.3, "carrier_hz": 900.0, "pulse_hz": 70.0}, {"E": 0.9 if i % 2 else 0.2},
                             (0, 0), i * DT, DT)
    assert drove == ["volume", "carrier_hz"]
    assert 0.4 <= tg["volume"] <= 0.9 and 800.0 <= tg["carrier_hz"] <= 1200.0 and tg["pulse_hz"] == 70.0
    r = pw.readout()
    assert r["volume"]["state"] != "idle" and r["pulse_hz"]["state"] == "idle"
    with pytest.raises(ValueError):
        pw.update({"mode": "sound"}, axis="alpha")
    # reset restarts one lane's build
    pw.update({"axis": "volume", "build_rate_pct_per_min": 10.0})
    pw.lanes["volume"].max_now = 0.9
    pw.reset("carrier_hz")
    assert pw.lanes["volume"].max_now == 0.9
    pw.reset()
    assert pw.lanes["volume"].max_now == pytest.approx(0.5)
    # on_scene: same id twice is one scene
    pw.update({"axis": "volume", "per_show_pct": 10.0})
    pw.on_scene("a"); pw.on_scene("a"); pw.on_scene("b")
    assert pw.lanes["volume"].params.max == pytest.approx(1.0)


# ---- presets --------------------------------------------------------------------------------------------

def test_preset_roundtrip_power_tables(tmp_path):
    ms = MappingSet()
    ms.moves = Moves()
    ms.power = Power()
    ms.power.update({"mode": "build", "min": 0.3, "max": 0.7, "speed": "slow", "surge": False})
    ms.power.update({"axis": "carrier_hz", "mode": "sound", "min": 900.0, "max": 1300.0})
    ms.save_preset("pw", tmp_path)
    text = (tmp_path / "pw.toml").read_text()
    assert "[power.volume]" in text and "[power.carrier_hz]" in text and "[moves]" in text
    ms2 = MappingSet()
    ms2.power = Power()
    ms2.load_preset("pw", tmp_path)
    v = ms2.power.lanes["volume"].params
    assert (v.mode, v.min, v.max, v.speed, v.surge) == ("build", 0.3, 0.7, "slow", False)
    c = ms2.power.lanes["carrier_hz"].params
    assert (c.mode, c.min, c.max) == ("sound", 900.0, 1300.0)
    assert ms2.power.lanes["pulse_hz"].params.mode == "manual"
    # a preset WITHOUT [power] leaves the lanes alone
    (tmp_path / "plain.toml").write_text('name = "plain"\n[macros]\n')
    ms2.load_preset("plain", tmp_path)
    assert ms2.power.lanes["volume"].params.mode == "build"


def test_builtin_cards_do_not_carry_power(tmp_path):
    paths = write_builtin_cards(tmp_path)
    import tomllib
    for p in paths:
        d = tomllib.loads(p.read_text())
        assert "power" not in d
        assert set(d["axes"]) == {"alpha", "beta"}, "cards are motion only (Amendment 2)"
        assert "intensity" not in d["macros"]


# ---- TEASE tiers (notes/power.md Amendment 2) ------------------------------------------------------------

def tease_lane(**kw) -> PowerLane:
    base = dict(build_rate_pct_per_min=0.0, min=0.0, max=1.0, speed="snappy", breakdown=False, rim_sidechain=False,
                beat_accent=False, plateau=False, per_show_pct=0.0, surge=True)
    base.update(kw)
    return lane("volume", **base)


def test_sustained_loud_input_never_exceeds_cruise():
    ln = tease_lane()
    t = seed(ln)
    run(ln, t, 3.0, lambda i, now: {"E": 0.95})          # the step in may surge once; let it hold + release
    outs = run(ln, t + 3.0, 20.0, lambda i, now: {"E": 0.95})
    r = ln.readout()
    assert r["cruise"] == pytest.approx(0.7, abs=1e-6)     # min + 0.7 * (max_now - min)
    assert max(outs) <= r["cruise"] + 1e-9
    assert max(outs) > 0.6, "the loud tail sits near cruise"
    # the curve: mid energy lands well under cruise (en 0.5 -> 0.5**1.6 * 0.7 ~ 0.23)
    run(ln, t + 23.0, 10.0, lambda i, now: {"E": 0.5})
    assert ln.out < 0.35


def test_strong_onset_peaks_for_peak_hold_then_returns_to_cruise():
    ln = tease_lane(peak_hold_s=0.6, peak_spacing_s=4.0)
    t = seed(ln)
    run(ln, t, 10.0, lambda i, now: {"E": 0.95})          # settle at cruise
    t += 10.0
    n0 = ln.readout()["peaks"]
    outs = run(ln, t, 0.6, lambda i, now: {"E": 1.0, "onset": 2.0 if i == 0 else 0.0})
    assert ln.readout()["peaks"] == n0 + 1 and ln.readout()["state"] == "surge"
    assert all(v == pytest.approx(ln.max_now, abs=0.02) for v in outs[1:]), "held at max_now for peak_hold_s"
    assert ln.readout()["last_peak_s"] == pytest.approx(0.6, abs=0.05)
    # snappy release 0.6 s: back to <= cruise within a few tau
    run(ln, t + 0.6, 3.0, lambda i, now: {"E": 0.95})
    assert ln.out <= ln.readout()["cruise"] + 1e-9 and ln.readout()["state"] == "following"


def test_peaks_respect_spacing():
    ln = tease_lane(peak_hold_s=0.3, peak_spacing_s=4.0)
    t = seed(ln)
    run(ln, t, 10.0, lambda i, now: {"E": 0.95})
    t += 10.0
    n0 = ln.readout()["peaks"]
    # a strong onset every second for 10 s -> at most one peak per 4 s
    run(ln, t, 10.0, lambda i, now: {"E": 1.0, "onset": 2.0 if i % 60 == 0 else 0.0})
    assert ln.readout()["peaks"] - n0 == 3            # t=0, 4, 8


def test_duty_cap_raises_threshold_under_wall_to_wall_hits():
    ln = tease_lane(peak_hold_s=0.6, peak_spacing_s=4.0, peak_duty=0.12, speed="musical")
    t = seed(ln)
    run(ln, t, 10.0, lambda i, now: {"E": 0.95})
    t += 10.0
    n0 = ln.readout()["peaks"]
    assert ln.readout()["surge_thresh"] == pytest.approx(0.75)
    run(ln, t, 60.0, lambda i, now: {"E": 1.0, "onset": 2.0 if i % 30 == 0 else 0.0})   # hits every 0.5 s
    r = ln.readout()
    assert r["surge_thresh"] > 0.75, "duty exceeded -> the threshold rose"
    assert r["peak_duty_60s"] > 0.0
    assert r["peaks"] - n0 < 15, "spacing alone would allow 15; the duty cap made peaks rarer"
    # every output is within [min, max_now]
    assert 0.0 <= ln.out <= ln.max_now + 1e-9


def test_accents_and_rim_never_exceed_cruise():
    ln = tease_lane(beat_accent=True, accent=0.5, accent_ms=200.0, rim_sidechain=True, rim_gain=0.5)
    t = seed(ln)
    run(ln, t, 10.0, lambda i, now: {"E": 0.95})
    cruise = ln.readout()["cruise"]
    outs = run(ln, t + 10.0, 5.0, lambda i, now: {"E": 0.95, "beat_phase": (i / 30.0) % 1.0}, pos=(1.0, 0.0))
    assert ln.readout()["accents"] > 0
    assert max(outs) <= cruise + 1e-9


# ---- engine + API ----------------------------------------------------------------------------------------



# ---- drift (Amendment 3) ---------------------------------------------------------------------------------

def drift_lane(axis: str = "carrier_hz", **kw) -> PowerLane:
    p = PowerParams.for_axis(axis).updated(dict(mode="drift", **kw), axis)
    ln = PowerLane(axis, p)
    ln.rng.seed(7)
    return ln


def beat_feats(i: int, tempo_hz: float = 2.0, phrase: int = 0) -> dict:
    return {"tempo_hz": tempo_hz, "beat_phase": (i * DT * tempo_hz) % 1.0, "phrase_index": phrase, "E": 0.9 if i % 2 else 0.1}


def test_drift_holds_home_for_the_hold_with_no_moves():
    ln = drift_lane()                                    # hold 8 beats; no tempo -> 0.5 s per beat = 4 s
    out = run(ln, 0.0, 3.9, lambda i, now: {"E": 0.9 if i % 2 else 0.1})   # loudness is ignored
    assert set(out) == {1100.0}
    r = ln.readout()
    assert r["mode"] == "drift" and r["home"] == 1100.0 and r["moves"] == 0 and r["state"] == "drift"
    assert r["next_move_s"] is not None and 0.0 < r["next_move_s"] <= 0.2 and r["next_move_beats"] <= 0.4
    assert r["max_now"] == 1600.0 and r["min"] == 700.0 and r["max"] == 1600.0   # full grid; build does not apply
    # no music at all -> the time fallback moves it after the hold
    out = run(ln, 3.9, 0.5, lambda i, now: {})
    assert ln.drift_moves == 1 and abs(out[-1] - 1100.0) == 100.0


def test_drift_moves_one_grid_step_on_a_phrase_boundary_after_the_hold():
    ln = drift_lane()
    n = int(10.0 / DT)                                   # 10 s at 120 bpm = 20 beats, phrase never changes
    for i in range(n):
        assert ln.apply(None, beat_feats(i), (0, 0), i * DT, DT) == 1100.0
    assert ln.drift_moves == 0 and ln.readout()["next_move_beats"] == 0.0 and ln.readout()["next_move_s"] is None
    v = ln.apply(None, beat_feats(n, phrase=1), (0, 0), n * DT, DT)   # phrase boundary -> ONE grid step
    assert abs(v - 1100.0) == 100.0 and ln.drift_moves == 1
    r = ln.readout()
    assert r["moves"] == 1 and r["last_move_s"] == 0.0 and r["next_move_beats"] == 8.0
    # another boundary inside the hold does not move it
    for i in range(n + 1, n + 60):
        assert ln.apply(None, beat_feats(i, phrase=2), (0, 0), i * DT, DT) == v
    assert ln.drift_moves == 1


def test_drift_never_leaves_min_max_and_stays_on_grid():
    ln = drift_lane(min=900.0, max=1300.0, drift_on="time", drift_hold_beats=1.0, home_pull=0.5)
    out = run(ln, 0.0, 120.0, lambda i, now: {})         # a move every 0.5 s: a seeded random walk
    assert all(900.0 <= x <= 1300.0 and x % 100 == 0 for x in out)
    assert ln.drift_moves > 100 and len({900.0, 1300.0} & set(out)) == 2   # it visits both bounds and bounces


def test_drift_home_pull_walks_back_one_step_per_move():
    ln = drift_lane(drift_on="time", drift_hold_beats=2.0, home_pull=1.0)
    ln.drift_pos = 1400.0                                # started away from home
    out = run(ln, 0.0, 3.2, lambda i, now: {})           # a move every 1 s
    seen = []
    for x in out:
        if not seen or seen[-1] != x:
            seen.append(x)
    assert seen[:4] == [1400.0, 1300.0, 1200.0, 1100.0]


def test_peak_dip_lowers_the_carrier_lane_while_volume_peaks_and_restores():
    pw = Power()
    pw.update({"axis": "carrier_hz", "peak_dip": True})
    vol = pw.lanes["volume"]
    t = seed(vol)
    pw.lanes["carrier_hz"].rng.seed(1)
    tg, drove = pw.apply({"carrier_hz": 1000.0}, {"E": 0.5}, (0, 0), t, DT)
    assert tg["carrier_hz"] == 1100.0 and "carrier_hz" in drove
    ro = pw.readout()
    assert ro["volume"]["peaking"] is False and ro["carrier_hz"]["peaking_dip"] is False
    vol._surge_until = t + 0.6                           # the volume lane is in its peak hold
    tg, _ = pw.apply({"carrier_hz": 1000.0}, {"E": 0.5}, (0, 0), t + DT, DT)
    ro = pw.readout()
    assert ro["volume"]["peaking"] is True and ro["volume"]["state"] == "surge"
    assert tg["carrier_hz"] == 900.0 and ro["carrier_hz"]["peaking_dip"] is True and ro["carrier_hz"]["state"] == "dip"
    tg, _ = pw.apply({"carrier_hz": 1000.0}, {"E": 0.5}, (0, 0), t + 1.0, DT)
    ro = pw.readout()
    assert tg["carrier_hz"] == 1100.0 and ro["carrier_hz"]["peaking_dip"] is False and ro["volume"]["peaking"] is False
    # the dip never goes under the axis lo
    pw.update({"axis": "carrier_hz", "home": 700.0, "min": 700.0})
    vol._surge_until = t + 5.0
    tg, _ = pw.apply({}, {"E": 0.5}, (0, 0), t + 2.0, DT)
    assert tg["carrier_hz"] == 700.0


def test_drift_mode_roundtrips_through_a_preset(tmp_path):
    ms = MappingSet()
    ms.moves = Moves()
    ms.power = Power()
    ms.power.update({"axis": "pulse_hz", "mode": "drift", "home": 80.0, "drift_hold_beats": 16.0, "drift_on": "time",
                     "home_pull": 0.9, "mood_bias": True, "peak_dip": True, "peak_dip_amount": 30.0, "drift_step": 2})
    ms.save_preset("dr", tmp_path)
    text = (tmp_path / "dr.toml").read_text()
    assert 'mode = "drift"' in text and "drift_hold_beats = 16.0" in text
    ms2 = MappingSet()
    ms2.power = Power()
    ms2.power.update({"axis": "pulse_hz", "mode": "manual"})
    ms2.load_preset("dr", tmp_path)
    p = ms2.power.lanes["pulse_hz"].params
    assert (p.mode, p.home, p.drift_hold_beats, p.drift_on, p.home_pull, p.mood_bias, p.peak_dip, p.peak_dip_amount,
            p.drift_step) == ("drift", 80.0, 16.0, "time", 0.9, True, True, 30.0, 2.0)
    assert ms2.power.lanes["pulse_hz"].drift_pos == 80.0
    # the first draft's seconds field still loads (0.5 s per beat)
    ms2.power.update({"axis": "pulse_hz", "drift_hold_s": 30.0})
    assert ms2.power.lanes["pulse_hz"].params.drift_hold_beats == 60.0
