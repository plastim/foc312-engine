"""Pattern geometry + lease semantics (deterministic clock, no hardware)."""

import asyncio
import functools
import math

import pytest

from stimengine.control.patterns import (
    PatternParams,
    PatternRunner,
    fourphase_vector,
    threephase_point,
)
from tests.test_engine_core import started_engine


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))

    return wrapper


def test_circle_geometry():
    p = PatternParams(name="circle", amplitude=0.8, center=(0.0, 0.0))
    pts = [threephase_point(p, ph, None, 0.03) for ph in (0.0, 0.25, 0.5, 0.75)]
    assert pts[0] == pytest.approx((0.8, 0.0))
    assert pts[1] == pytest.approx((0.0, 0.8), abs=1e-9)
    assert pts[2] == pytest.approx((-0.8, 0.0), abs=1e-9)
    for a, b in pts:
        assert math.hypot(a, b) <= 1.0 + 1e-9


def test_offcenter_stays_in_disc():
    p = PatternParams(name="circle", amplitude=1.0, center=(0.9, 0.9))
    for ph in [i / 50 for i in range(50)]:
        a, b = threephase_point(p, ph, None, 0.03)
        assert math.hypot(a, b) <= 1.0 + 1e-9


def test_stroke_and_figure8_and_hold():
    s = PatternParams(name="stroke", amplitude=0.5)
    assert threephase_point(s, 0.25, None, 0.03) == pytest.approx((0.0, 0.5))
    f = PatternParams(name="figure8", amplitude=0.5)
    a, b = threephase_point(f, 0.125, None, 0.03)
    assert 0 < a < 0.5 and 0 < b <= 0.25
    h = PatternParams(name="hold", center=(0.2, -0.3))
    assert threephase_point(h, 7.3, None, 0.03) == pytest.approx((0.2, -0.3))


def test_fourphase_vectors():
    rr = PatternParams(name="round_robin", amplitude=1.0)
    e = fourphase_vector(rr, 0.0)
    assert e == pytest.approx((1.0, 0.0, 0.0, 0.0))
    e = fourphase_vector(rr, 0.125)  # halfway through the first crossfade
    assert e == pytest.approx((0.5, 0.5, 0.0, 0.0))
    w = PatternParams(name="wave", amplitude=1.0, floor=0.2)
    for ph in (0.0, 0.3, 0.77):
        v = fourphase_vector(w, ph)
        assert all(0.2 - 1e-9 <= x <= 1.0 + 1e-9 for x in v)
    eq = PatternParams(name="all_equal", amplitude=0.5)
    assert fourphase_vector(eq, 3.0) == pytest.approx((0.5,) * 4)


def test_params_validation():
    with pytest.raises(ValueError):
        PatternParams.from_dict({"name": "spiral"})
    p = PatternParams.from_dict({"rate_hz": 99, "amplitude": 7, "center": [2, -2]})
    assert p.rate_hz == 5.0 and p.amplitude == 1.0 and p.center == (1.0, -1.0)
    with pytest.raises(ValueError):
        PatternParams.from_dict({"center": [1]})


@run_async
async def test_runner_renews_only_under_lease(tmp_path):
    t, dev, eng, session = await started_engine(tmp_path, deadman_silence_s=0.2, deadman_ramp_down_s=0.2)
    clock = [100.0]
    runner = PatternRunner(eng, clock=lambda: clock[0])
    runner._last = clock[0]
    runner.update({"name": "circle", "rate_hz": 1.0, "amplitude": 0.5})
    runner.running = True  # drive tick() by hand instead of the task

    # no lease: geometry moves, no renewals
    before = eng.frame.alpha
    for _ in range(5):
        clock[0] += 1 / 30
        runner.tick()
    assert runner.renewals == 0
    assert eng.frame.alpha != before or eng.frame.beta != 0.0

    runner.grant_lease(1.0, source="test")
    for _ in range(5):
        clock[0] += 1 / 30
        runner.tick()
    assert runner.renewals == 5
    assert runner.lease_alive

    clock[0] += 2.0  # lease lapses
    runner.tick()
    assert not runner.lease_alive
    assert runner.renewals == 5, "no renewals after the lease lapsed"

    # wrong-mode pattern refused
    with pytest.raises(ValueError):
        runner.update({"name": "round_robin"})

    runner.running = False
    await eng.stop()
    dev.stop()


@run_async
async def test_runner_task_and_envelope(tmp_path):
    t, dev, eng, session = await started_engine(tmp_path)
    runner = PatternRunner(eng)
    runner.start({"name": "stroke", "rate_hz": 2.0, "amplitude": 0.7})
    runner.grant_lease(5.0)
    eng.arm()
    runner.set_envelope(0.0, 0.2, 0.15)
    await asyncio.sleep(0.35)
    assert runner.ticks >= 5
    assert runner.renewals >= 5
    assert eng.status()["master_target"] == pytest.approx(0.2)
    assert runner.envelope is None  # consumed
    runner.stop()
    assert not runner.running
    await eng.stop()
    dev.stop()
