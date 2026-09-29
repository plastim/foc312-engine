"""Oracle tests: ported 3-phase model == vendored FOCStimThreephaseAlgorithm."""
import itertools

import numpy as np
import pytest

from tests.test_math_support import assert_same, p, ported_threephase, vendored_threephase

ALPHAS = np.linspace(-1.2, 1.2, 9)          # includes outside-unit-circle values
BETAS = np.linspace(-1.2, 1.2, 9)
CARRIERS = [300.0, 500.0, 790.0, 1000.0, 1500.0, 2000.0, 2500.0]   # 300/2500 hit the clamp
PULSE_FREQS = [0.0, 1.0, 10.0, 50.0, 74.0, 100.0, 150.0]
PULSE_WIDTHS = [4.0, 6.0, 8.0, 10.0, 11.5]
TAUS = [100.0, 200.0, 355.0, 500.0, 1000.0]

COUNT = {"n": 0}


def _check(**over):
    d = p(**over)
    assert_same(vendored_threephase(d), ported_threephase(d))
    COUNT["n"] += 1


@pytest.mark.parametrize("alpha,beta", list(itertools.product(ALPHAS, BETAS)))
def test_position_grid(alpha, beta):
    _check(alpha=float(alpha), beta=float(beta))


@pytest.mark.parametrize("carrier,tau", list(itertools.product(CARRIERS, TAUS)))
def test_carrier_tau_sweep(carrier, tau):
    _check(carrier_frequency=carrier, tau_us=tau)


@pytest.mark.parametrize("pf,pw,bg,pfa", list(itertools.product(
    PULSE_FREQS, PULSE_WIDTHS, [False, True], [False, True])))
def test_pulse_sweep(pf, pw, bg, pfa):
    _check(pulse_frequency=pf, pulse_width=pw, enable_burst_gap=bg,
           enable_pulse_frequency_adjustment=pfa, carrier_frequency=1000.0)


@pytest.mark.parametrize("master,api,inactivity,external", [
    (0.0, 1.0, 1.0, 1.0), (1.0, 1.0, 1.0, 1.0), (0.3, 0.5, 0.9, 0.7),
    (1.5, 1.0, 1.0, 1.0), (0.5, -0.2, 1.0, 1.0), (0.5, 1.0, 0.0, 1.0), (0.5, 1.0, 1.0, 2.0),
])
def test_volume_parts(master, api, inactivity, external):
    _check(master=master, api=api, inactivity=inactivity, external=external, alpha=0.3, beta=-0.4)


@pytest.mark.parametrize("amps", [0.01, 0.05, 0.15, 0.2])
def test_amps_cap(amps):
    _check(amps=amps, master=1.0)


@pytest.mark.parametrize("min_c,max_c", [(500.0, 2000.0), (300.0, 1000.0), (700.0, 1200.0), (1000.0, 1000.0)])
def test_safety_carrier_window(min_c, max_c):
    for carrier in CARRIERS:
        _check(min_carrier=min_c, max_carrier=max_c, carrier_frequency=carrier)


def test_not_playing_zeroes_amps_only():
    d = p(playing=False, master=1.0, alpha=0.5, beta=0.5)
    up, po = vendored_threephase(d), ported_threephase(d)
    assert_same(up, po)
    from stimengine.math._vendor import AxisType
    assert po[AxisType.AXIS_WAVEFORM_AMPLITUDE_AMPS] == 0.0
    assert po[AxisType.AXIS_POSITION_ALPHA] != 0.0


@pytest.mark.parametrize("rotation,mirror,top,bottom,left,right", [
    (0.0, False, 1.0, -1.0, -1.0, 1.0),
    (90.0, False, 1.0, -1.0, -1.0, 1.0),
    (37.5, True, 1.0, -1.0, -1.0, 1.0),
    (180.0, False, 0.5, -0.5, -0.8, 0.2),
    (270.0, True, 0.9, 0.1, -0.3, 0.3),
    (360.0, False, 1.0, 1.0, -1.0, 1.0),   # degenerate limits
])
def test_transform_enabled(rotation, mirror, top, bottom, left, right):
    for alpha, beta in [(0.0, 0.0), (0.5, 0.5), (-1.0, 0.2), (1.2, -1.2), (0.1, -0.9)]:
        _check(transform_enabled=True, rotation=rotation, mirror=mirror, top=top, bottom=bottom,
               left=left, right=right, alpha=alpha, beta=beta)


@pytest.mark.parametrize("start,length,invert", [
    (0.0, 90.0, False), (45.0, 180.0, True), (-30.0, 60.0, False), (200.0, 300.0, True), (10.0, 0.0, False),
])
def test_map_to_edge(start, length, invert):
    for alpha, beta in [(0.0, 0.0), (0.5, 0.5), (-1.0, 0.2), (1.2, -1.2), (0.1, -0.9)]:
        _check(map_to_edge_enabled=True, m2e_start=start, m2e_length=length, m2e_invert=invert,
               alpha=alpha, beta=beta)


def test_transform_then_map_to_edge():
    _check(transform_enabled=True, rotation=30.0, mirror=True, map_to_edge_enabled=True,
           m2e_start=20.0, m2e_length=120.0, alpha=0.4, beta=-0.6)


@pytest.mark.parametrize("neutral,right,center", [
    (0.0, 0.0, -0.604), (-2.1, 0.3, -0.3), (1.5, -1.5, 0.0), (0.7, 0.7, -3.0),
])
def test_calibration_passthrough(neutral, right, center):
    _check(cal_neutral=neutral, cal_right=right, cal_center=center)


class _Sensor:
    """Mimics a restim sensor node: tries to RAISE volume (must be clipped) and shifts position."""
    def __init__(self, factor, dalpha):
        self.factor, self.dalpha = factor, dalpha

    def process(self, d):
        d["volume"] = d["volume"] * self.factor
        d["alpha"] = d["alpha"] + self.dalpha


@pytest.mark.parametrize("factor,dalpha", [(0.5, 0.0), (2.0, 0.0), (0.0, 0.3), (1.0, -0.5), (3.0, 1.0)])
def test_sensor_hook_reduce_only(factor, dalpha):
    d = p(master=0.9, alpha=0.2, beta=0.1)
    up = vendored_threephase(d, sensor=_Sensor(factor, dalpha))
    po = ported_threephase(d, sensor=_Sensor(factor, dalpha))
    assert_same(up, po)
    base = ported_threephase(d)
    from stimengine.math._vendor import AxisType
    assert po[AxisType.AXIS_WAVEFORM_AMPLITUDE_AMPS] <= base[AxisType.AXIS_WAVEFORM_AMPLITUDE_AMPS] + 1e-12


def test_zz_report_count():
    # last alphabetically in this module; prints how many oracle comparisons ran here
    print(f"\n[threephase oracle comparisons: {COUNT['n']}]")
