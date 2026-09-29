"""Axis exclusivity: an external position/vector input stops a running pattern (external wins)."""

from stimengine.control.patterns import PatternRunner
from tests.test_engine_core import started_engine
from tests.test_patterns_basic import run_async


@run_async
async def test_external_position_stops_pattern(tmp_path):
    t, dev, eng, session = await started_engine(tmp_path, deadman_silence_s=0.2, deadman_ramp_down_s=0.2)
    clock = [100.0]
    runner = PatternRunner(eng, clock=lambda: clock[0])
    runner._last = clock[0]
    runner.update({"name": "circle", "rate_hz": 1.0, "amplitude": 0.5})
    runner.running = True
    runner.grant_lease(5.0, source="test")
    for _ in range(3):
        clock[0] += 1 / 30
        runner.tick()
    assert runner.running

    # internal writes (the pattern's own) must NOT stop it
    eng.set_position(0.1, 0.1, source="internal")
    assert runner.running

    # an external write (T-code / API) takes the axes
    eng.set_position(0.3, -0.2, source="tcode")
    assert not runner.running
    assert (eng.frame.alpha, eng.frame.beta) == (0.3, -0.2)

    # same for vectors
    runner.running = True
    eng.set_vector(1, 0, 0, 0, source="api:claude")
    assert not runner.running

    await eng.stop()
    dev.stop()
