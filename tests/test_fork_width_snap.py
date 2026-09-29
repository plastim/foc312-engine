"""Fork firmware width grid: sent widths are on the 20 us grid and the phase charge stays continuous."""
from stimengine.device import fork as F


def test_snap_matches_firmware_rounding():
    assert F.snap_width(50) == (60.0, 50 / 60)       # int(2.5 + .5) = 3 samples
    assert F.snap_width(49.9)[0] == 40.0
    assert F.snap_width(10)[0] == 40.0 and F.snap_width(1000)[0] == 400.0
    for w in range(40, 401):
        played, _ = F.snap_width(w)
        assert played % 20 == 0 and 40 <= played <= 400


def test_charge_is_continuous_across_a_sweep():
    # a smooth 50 -> 120 us sweep (ET-312 Waves) must not jump in charge (amplitude x played width)
    prev = None
    for tenth in range(500, 1201):
        w = tenth / 10
        played, k = F.snap_width(w)
        q = k * played                    # relative charge per phase at unit intensity
        assert abs(q - w) < 1e-9
        if prev is not None:
            assert abs(q - prev) < 0.11   # 0.1 us steps in, <= 0.1 us-equivalent change out: no jumps
        prev = q
