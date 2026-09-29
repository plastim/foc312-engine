"""Oracle tests for the leaf calibration functions, the engine.toml fixture, and StimFrame."""
import itertools

import numpy as np
import pytest

from tests.test_math_support import (
    TOL, assert_same, load_engine_toml, p, ported_fourphase, ported_threephase,
    vendored_fourphase, vendored_threephase,
)

import stim_math.burst_gap as up_bg  # vendored
import stim_math.limits as up_limits
from stim_math.pulse_frequency_calibration import PulseFrequencyCalibration as UpPFC
from stim_math.tau_calibration import TauCalibration as UpTau

from stimengine.math import ModelSet, StimFrame, VolumeParts, evaluate, limits, to_axis_moves
from stimengine.math import burst_gap, pulse_frequency_calibration, tau_calibration
from stimengine.math._vendor import AxisType
from stimengine.math.threephase import center_calib_to_reduction, center_reduction_to_calib


@pytest.mark.parametrize("max_f,f,tau", list(itertools.product(
    [1000.0, 2000.0], np.linspace(300, 2000, 18), [1e-4, 355e-6, 1e-3])))
def test_tau_derating(max_f, f, tau):
    assert abs(UpTau.derating_factor(max_f, f, tau) - tau_calibration.derating_factor(max_f, f, tau)) <= TOL


@pytest.mark.parametrize("pf", list(np.linspace(-10, 200, 43)))
def test_pulse_frequency_scale(pf):
    assert abs(UpPFC.scale(pf) - pulse_frequency_calibration.scale(pf)) <= TOL
    assert abs(UpPFC.normalized_intensity(pf) - pulse_frequency_calibration.normalized_intensity(pf)) <= TOL


@pytest.mark.parametrize("carrier,pf,pw", list(itertools.product(
    [0.0, 300.0, 790.0, 2000.0], [0.0, 0.05, 1.0, 74.0, 300.0], [3.0, 11.5, 100.0])))
def test_burst_gap_both_directions(carrier, pf, pw):
    assert abs(up_bg.burst_gap_frequency_to_pulse_frequency(carrier, pf, pw)
               - burst_gap.burst_gap_frequency_to_pulse_frequency(carrier, pf, pw)) <= TOL
    assert abs(up_bg.pulse_frequency_to_burst_gap_frequency(carrier, pf, pw)
               - burst_gap.pulse_frequency_to_burst_gap_frequency(carrier, pf, pw)) <= TOL


def test_limits_match_upstream():
    assert (limits.CarrierFrequencyFOC.min, limits.CarrierFrequencyFOC.max) == \
        (up_limits.CarrierFrequencyFOC.min, up_limits.CarrierFrequencyFOC.max)
    assert (limits.WaveformAmplitudeFOC.min, limits.WaveformAmplitudeFOC.max) == \
        (up_limits.WaveformAmpltiudeFOC.min, up_limits.WaveformAmpltiudeFOC.max)
    assert (limits.PulseFrequencyFOC.min, limits.PulseFrequencyFOC.max) == \
        (up_limits.PulseFrequencyFOC.min, up_limits.PulseFrequencyFOC.max)


@pytest.mark.parametrize("db", [-3.0, -0.604, -0.3, 0.0])
def test_center_reduction_roundtrip(db):
    assert abs(center_reduction_to_calib(center_calib_to_reduction(db)) - db) <= 1e-9


# ---- engine.toml fixture -------------------------------------------------------

def test_engine_toml_threephase_matches_upstream():
    cfg = load_engine_toml()
    cal = cfg["calibration"]["threephase"]
    sig = cfg["signal"]
    cd = cfg["carrier_defaults"]
    d = p(cal_neutral=cal["neutral"], cal_right=cal["right"], cal_center=cal["center"],
          min_carrier=sig["min_carrier_hz"], max_carrier=sig["max_carrier_hz"],
          amps=sig["waveform_amplitude_amps"], carrier_frequency=cd["pulse_carrier_frequency"],
          pulse_frequency=cd["pulse_frequency"], pulse_width=cd["pulse_width"],
          alpha=0.35, beta=-0.2, master=0.6)
    assert_same(vendored_threephase(d), ported_threephase(d))

    models = ModelSet.from_config(cfg)
    frame = StimFrame(mode="threephase", alpha=0.35, beta=-0.2, volume=VolumeParts(master=0.6),
                      carrier_frequency=cd["pulse_carrier_frequency"], pulse_frequency=cd["pulse_frequency"],
                      pulse_width=cd["pulse_width"], pulse_rise_time=d["pulse_rise_time"],
                      pulse_interval_random=d["pulse_interval_random"], tau_us=d["tau_us"])
    assert_same(vendored_threephase(d), evaluate(frame, models))


def test_engine_toml_fourphase_matches_upstream():
    cfg = load_engine_toml()
    cal = cfg["calibration"]["fourphase"]
    sig = cfg["signal"]
    # v1.66 keys are a/b/c/d/center_reduction; the captured ini has a/b/c + legacy 'center'
    d = p(cal_a=cal["a"], cal_b=cal["b"], cal_c=cal["c"], cal_d=cal.get("d", 0.0),
          cal_center_reduction=cal.get("center_reduction", 0.07),
          min_carrier=sig["min_carrier_hz"], max_carrier=sig["max_carrier_hz"],
          amps=sig["waveform_amplitude_amps"], e1=1.0, e2=0.4, e3=0.0, e4=0.7, master=0.6)
    assert_same(vendored_fourphase(d), ported_fourphase(d))

    models = ModelSet.from_config(cfg)
    frame = StimFrame(mode="fourphase", e1=1.0, e2=0.4, e3=0.0, e4=0.7, volume=VolumeParts(master=0.6),
                      carrier_frequency=d["carrier_frequency"], pulse_frequency=d["pulse_frequency"],
                      pulse_width=d["pulse_width"], pulse_rise_time=d["pulse_rise_time"],
                      pulse_interval_random=d["pulse_interval_random"], tau_us=d["tau_us"])
    assert_same(vendored_fourphase(d), evaluate(frame, models))


# ---- StimFrame / to_axis_moves -------------------------------------------------

def test_to_axis_moves_order_and_types():
    models = ModelSet.from_config(load_engine_toml())
    moves = to_axis_moves(StimFrame(mode="threephase", alpha=0.1, beta=0.2, volume=VolumeParts(master=0.5)), models)
    assert [a for a, _ in moves] == [
        AxisType.AXIS_POSITION_ALPHA, AxisType.AXIS_POSITION_BETA, AxisType.AXIS_WAVEFORM_AMPLITUDE_AMPS,
        AxisType.AXIS_CARRIER_FREQUENCY_HZ, AxisType.AXIS_PULSE_FREQUENCY_HZ, AxisType.AXIS_PULSE_WIDTH_IN_CYCLES,
        AxisType.AXIS_PULSE_RISE_TIME_CYCLES, AxisType.AXIS_PULSE_INTERVAL_RANDOM_PERCENT,
        AxisType.AXIS_CALIBRATION_3_CENTER, AxisType.AXIS_CALIBRATION_3_UP, AxisType.AXIS_CALIBRATION_3_LEFT,
    ]
    assert all(isinstance(v, float) for _, v in moves)

    moves4 = to_axis_moves(StimFrame(mode="fourphase", e1=1.0, volume=VolumeParts(master=0.5)), models)
    assert moves4[0][0] == AxisType.AXIS_ELECTRODE_1_POWER
    assert moves4[-1][0] == AxisType.AXIS_CALIBRATION_4_REDUCTION_IN_CENTER
    assert len(moves4) == 15


def test_frame_default_volume_is_silent():
    models = ModelSet.from_config(load_engine_toml())
    out = evaluate(StimFrame(alpha=0.5, beta=0.5), models)
    assert out[AxisType.AXIS_WAVEFORM_AMPLITUDE_AMPS] == 0.0


def test_unknown_mode_rejected():
    models = ModelSet.from_config(load_engine_toml())
    with pytest.raises(ValueError):
        evaluate(StimFrame(mode="fivephase"), models)  # type: ignore[arg-type]
