"""Oracle tests: ported 4-phase model == vendored FOCStimFourphaseAlgorithm."""
import itertools

import numpy as np
import pytest

from tests.test_math_support import assert_same, p, ported_fourphase, vendored_fourphase

LEVELS = [-0.2, 0.0, 0.25, 0.5, 0.75, 1.0, 1.3]     # outside 0..1 must clip
CARRIERS = [300.0, 500.0, 790.0, 1000.0, 1500.0, 2000.0, 2500.0]
PULSE_FREQS = [0.0, 1.0, 10.0, 50.0, 74.0, 100.0, 150.0]
PULSE_WIDTHS = [4.0, 6.0, 8.0, 10.0, 11.5]
TAUS = [100.0, 200.0, 355.0, 500.0, 1000.0]

COUNT = {"n": 0}


def _check(**over):
    d = p(**over)
    assert_same(vendored_fourphase(d), ported_fourphase(d))
    COUNT["n"] += 1


@pytest.mark.parametrize("e1,e2,e3,e4", list(itertools.product(LEVELS, LEVELS, [0.0, 0.5, 1.0], [0.0, 1.0])))
def test_intensity_grid(e1, e2, e3, e4):
    _check(e1=e1, e2=e2, e3=e3, e4=e4, master=0.7)


@pytest.mark.parametrize("carrier,tau", list(itertools.product(CARRIERS, TAUS)))
def test_carrier_tau_sweep(carrier, tau):
    _check(carrier_frequency=carrier, tau_us=tau, e1=1.0, e3=0.5)


@pytest.mark.parametrize("pf,pw,bg,pfa", list(itertools.product(
    PULSE_FREQS, PULSE_WIDTHS, [False, True], [False, True])))
def test_pulse_sweep(pf, pw, bg, pfa):
    _check(pulse_frequency=pf, pulse_width=pw, enable_burst_gap=bg,
           enable_pulse_frequency_adjustment=pfa, carrier_frequency=1000.0, e2=1.0)


@pytest.mark.parametrize("master,api,inactivity,external", [
    (0.0, 1.0, 1.0, 1.0), (1.0, 1.0, 1.0, 1.0), (0.3, 0.5, 0.9, 0.7),
    (1.5, 1.0, 1.0, 1.0), (0.5, -0.2, 1.0, 1.0), (0.5, 1.0, 0.0, 1.0), (0.5, 1.0, 1.0, 2.0),
])
def test_volume_parts(master, api, inactivity, external):
    _check(master=master, api=api, inactivity=inactivity, external=external, e1=1.0, e4=0.3)


@pytest.mark.parametrize("a,b,c,d,cr", [
    (0.0, 0.0, 0.0, 0.0, 0.07), (0.3, 1.1, -0.3, 0.0, 0.07), (-1.0, -2.0, 0.0, -0.5, 0.0), (0.0, 0.0, 0.0, 0.0, 0.2),
])
def test_calibration_passthrough(a, b, c, d, cr):
    _check(cal_a=a, cal_b=b, cal_c=c, cal_d=d, cal_center_reduction=cr, e1=0.5, e2=0.5, e3=0.5, e4=0.5)


def test_not_playing_zeroes_amps_only():
    d = p(playing=False, master=1.0, e1=0.5, e2=0.5)
    up, po = vendored_fourphase(d), ported_fourphase(d)
    assert_same(up, po)
    from stimengine.math._vendor import AxisType
    assert po[AxisType.AXIS_WAVEFORM_AMPLITUDE_AMPS] == 0.0
    assert po[AxisType.AXIS_ELECTRODE_1_POWER] == 0.5


class _Sensor:
    def __init__(self, factor, de):
        self.factor, self.de = factor, de

    def process(self, d):
        d["volume"] = d["volume"] * self.factor
        d["e1"] = d["e1"] + self.de
        d["e4"] = d["e4"] - self.de


@pytest.mark.parametrize("factor,de", [(0.5, 0.0), (2.0, 0.0), (0.0, 0.3), (1.0, -0.5), (3.0, 1.0)])
def test_sensor_hook_reduce_only(factor, de):
    d = p(master=0.9, e1=0.2, e2=0.1, e4=0.8)
    up = vendored_fourphase(d, sensor=_Sensor(factor, de))
    po = ported_fourphase(d, sensor=_Sensor(factor, de))
    assert_same(up, po)
    base = ported_fourphase(d)
    from stimengine.math._vendor import AxisType
    assert po[AxisType.AXIS_WAVEFORM_AMPLITUDE_AMPS] <= base[AxisType.AXIS_WAVEFORM_AMPLITUDE_AMPS] + 1e-12


def test_zz_report_count():
    print(f"\n[fourphase oracle comparisons: {COUNT['n']}]")
