"""PlaStim variants of Climb: the mode's own program, with the VM's clock slowed (last 10 % of each climb, x3) or
paused (25 % of each climb's time at its top). Checked against plain Climb's own timing, at two MA settings."""
from __future__ import annotations

import pytest

from stimengine.et312 import ET312Engine
from stimengine.et312 import modes as M
from stimengine.et312.engine import TICK_HZ


def _climbs(mode: str, ma: float, seconds: float = 90.0) -> list[tuple[float, float]]:
    """(climb duration s, time at the top rate s) for each complete climb."""
    e = ET312Engine(mode, ma=ma, skip_mode_ramp=True)
    out, start, prev, top_t, top = [], 0.0, None, 0.0, None
    for f in e.run(seconds):
        r = f.a.pulse_rate_hz
        if prev is not None and r < prev * 0.5:                  # the drop: the next climb starts
            out.append((f.t - start, top_t))
            start, top_t, top = f.t, 0.0, None
        if top is None or r > top + 1e-9:
            top, top_t = r, 0.0
        elif abs(r - top) < 1e-9:
            top_t += 1 / TICK_HZ
        prev = r
    return out


@pytest.mark.needs_et312_data
@pytest.mark.parametrize("ma", [0.5, 0.8])
def test_slow_finish_stretches_the_last_tenth_three_times(ma):
    plain, slow = _climbs("climb", ma), _climbs("climb_slow", ma)
    assert len(slow) >= 1
    for (p, _), (s, _) in zip(plain, slow):
        assert s == pytest.approx(p + 2 * 0.10 * p, rel=0.03)   # the last 10 % takes 3x: +20 % of the climb


@pytest.mark.needs_et312_data
@pytest.mark.parametrize("ma", [0.5, 0.8])
def test_peak_hold_holds_the_top_for_a_quarter_of_the_climb(ma):
    plain, hold = _climbs("climb", ma), _climbs("climb_hold", ma)
    assert len(hold) >= 1
    for (p, ptop), (h, htop) in zip(plain, hold):
        assert h == pytest.approx(1.25 * p, rel=0.02)
        assert htop - ptop == pytest.approx(0.25 * p, rel=0.03, abs=0.02)


@pytest.mark.needs_et312_data
def test_variants_only_change_timing_not_the_climb():
    """Same rates visited, same width and strength: the variant plays Climb's own values."""
    def seen(mode):
        e = ET312Engine(mode, ma=0.8, skip_mode_ramp=True)
        fr = list(e.run(40))
        return {round(f.a.pulse_rate_hz, 3) for f in fr}, {(f.a.pulse_width_us, round(f.a.intensity, 4)) for f in fr}
    rates, other = seen("climb")
    for mode in ("climb_slow", "climb_hold"):
        r, o = seen(mode)
        assert r <= rates and o == other


@pytest.mark.needs_et312_data
def test_variant_frames_carry_their_own_name_and_steady_time():
    e = ET312Engine("climb_hold", ma=0.8, skip_mode_ramp=True)
    fr = list(e.run(30))
    assert fr[-1].mode == M.CLIMB_HOLD and fr[-1].mode_name == "climb_hold"
    assert [f.tick for f in fr] == list(range(1, len(fr) + 1))   # time runs on while the VM is paused


@pytest.mark.needs_et312_data
def test_the_remote_pack_carries_the_variants_after_climb():
    from stimengine.remote import pack as P
    pk, _ = P.collect()
    names = [e.name for e in pk.entries if e.kind == P.KIND_BUILTIN]
    i = names.index("Climb")
    assert names[i + 1:i + 3] == ["Climb (slow finish)", "Climb (peak hold)"]
    modes = {e.name: e.mode for e in pk.entries if e.kind == P.KIND_BUILTIN}
    assert modes["Climb (slow finish)"] == M.CLIMB_SLOW and modes["Climb (peak hold)"] == M.CLIMB_HOLD
