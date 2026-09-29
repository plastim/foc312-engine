"""Shared harness for the math oracle tests (no tests in here).

Puts vendor/restim on sys.path, provides fake axes that satisfy restim's
AbstractAxis/AbstractMediaSync contracts with plain constants, and builds the
VENDORED algorithms from a flat dict of floats so each test can run upstream
and the port on identical inputs.
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor" / "restim"
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from device.focstim.fourphase_algorithm import FOCStimFourphaseAlgorithm  # noqa: E402
from device.focstim.threephase_algorithm import FOCStimThreephaseAlgorithm  # noqa: E402
from stim_math.audio_gen.params import (  # noqa: E402
    FOCStimParams, FourphaseCalibrationParams, FourphaseFOCStimParams, FourphaseIntensityParams,
    SafetyParamsFOC, ThreephaseCalibrationParams, ThreephasePositionParams,
    ThreephasePositionTransformParams, VolumeParams,
)
from stim_math.axis import AbstractAxis, AbstractMediaSync  # noqa: E402

from stimengine.math import (  # noqa: E402
    FourPhaseCalibration, FourPhaseModel, SafetyLimits, ThreePhaseCalibration, ThreePhaseModel,
    ThreePhaseTransform, VolumeParts,
)

TOL = 1e-9


class K(AbstractAxis):
    """Constant axis: interpolate(t) and last_value() both return the constant."""
    def __init__(self, v):
        self.v = v

    def interpolate(self, timestamp):
        return self.v

    def last_value(self):
        return self.v

    def add(self, value, interval=0.0):
        self.v = value


class Media(AbstractMediaSync):
    def __init__(self, playing=True):
        self.playing = playing

    def is_playing(self):
        return self.playing


def load_engine_toml() -> dict:
    with open(ROOT / "config" / "engine.toml", "rb") as f:
        return tomllib.load(f)


DEFAULTS = dict(
    master=0.8, api=1.0, inactivity=1.0, external=1.0,
    carrier_frequency=790.0, pulse_frequency=74.0, pulse_width=11.5,
    pulse_rise_time=5.0, pulse_interval_random=0.0, tau_us=355.0,
    enable_burst_gap=True, enable_pulse_frequency_adjustment=True,
    min_carrier=500.0, max_carrier=2000.0, amps=0.15, playing=True,
    # 3-phase
    alpha=0.0, beta=0.0, cal_neutral=0.0, cal_right=0.0, cal_center=-0.604,
    transform_enabled=False, rotation=0.0, mirror=False,
    top=1.0, bottom=-1.0, left=-1.0, right=1.0,
    map_to_edge_enabled=False, m2e_start=0.0, m2e_length=0.0, m2e_invert=False,
    # 4-phase
    e1=0.0, e2=0.0, e3=0.0, e4=0.0, cal_a=0.0, cal_b=0.0, cal_c=0.0, cal_d=0.0, cal_center_reduction=0.07,
)


def p(**over):
    d = dict(DEFAULTS)
    d.update(over)
    return d


def _volume(d):
    return VolumeParams(api=K(d["api"]), master=K(d["master"]),
                        inactivity=K(d["inactivity"]), external=K(d["external"]))


def _safety(d):
    return SafetyParamsFOC(d["min_carrier"], d["max_carrier"], d["amps"])


def vendored_threephase(d, sensor=None):
    params = FOCStimParams(
        position=ThreephasePositionParams(K(d["alpha"]), K(d["beta"])),
        transform=ThreephasePositionTransformParams(
            transform_enabled=K(d["transform_enabled"]),
            transform_rotation_degrees=K(d["rotation"]),
            transform_mirror=K(d["mirror"]),
            transform_top_limit=K(d["top"]), transform_bottom_limit=K(d["bottom"]),
            transform_left_limit=K(d["left"]), transform_right_limit=K(d["right"]),
            map_to_edge_enabled=K(d["map_to_edge_enabled"]),
            map_to_edge_start=K(d["m2e_start"]), map_to_edge_length=K(d["m2e_length"]),
            map_to_edge_invert=K(d["m2e_invert"]),
        ),
        calibrate=ThreephaseCalibrationParams(
            neutral=K(d["cal_neutral"]), right=K(d["cal_right"]), center=K(d["cal_center"])),
        volume=_volume(d),
        carrier_frequency=K(d["carrier_frequency"]), pulse_frequency=K(d["pulse_frequency"]),
        pulse_width=K(d["pulse_width"]), pulse_interval_random=K(d["pulse_interval_random"]),
        pulse_rise_time=K(d["pulse_rise_time"]), tau=K(d["tau_us"]),
        enable_pulse_frequency_adjustment=K(d["enable_pulse_frequency_adjustment"]),
        enable_burst_gap=K(d["enable_burst_gap"]),
    )
    alg = FOCStimThreephaseAlgorithm(Media(d["playing"]), params, _safety(d))
    if sensor is not None:
        alg.sensor_node = sensor
    return alg.parameter_dict()


def ported_threephase(d, sensor=None):
    model = ThreePhaseModel(
        ThreePhaseCalibration(neutral=d["cal_neutral"], right=d["cal_right"], center=d["cal_center"]),
        ThreePhaseTransform(
            transform_enabled=d["transform_enabled"], rotation_degrees=d["rotation"],
            mirror=d["mirror"], top_limit=d["top"], bottom_limit=d["bottom"],
            left_limit=d["left"], right_limit=d["right"],
            map_to_edge_enabled=d["map_to_edge_enabled"], map_to_edge_start=d["m2e_start"],
            map_to_edge_length=d["m2e_length"], map_to_edge_invert=d["m2e_invert"],
        ),
    )
    return model.compute(
        alpha=d["alpha"], beta=d["beta"],
        volume=VolumeParts(d["master"], d["api"], d["inactivity"], d["external"]),
        carrier_frequency=d["carrier_frequency"], pulse_frequency=d["pulse_frequency"],
        pulse_width=d["pulse_width"], pulse_rise_time=d["pulse_rise_time"],
        pulse_interval_random=d["pulse_interval_random"], tau_us=d["tau_us"],
        enable_burst_gap=d["enable_burst_gap"],
        enable_pulse_frequency_adjustment=d["enable_pulse_frequency_adjustment"],
        safety=SafetyLimits(d["min_carrier"], d["max_carrier"], d["amps"]),
        playing=d["playing"],
        sensor=(sensor.process if sensor is not None else None),
    )


def vendored_fourphase(d, sensor=None):
    params = FourphaseFOCStimParams(
        position=FourphaseIntensityParams(K(d["e1"]), K(d["e2"]), K(d["e3"]), K(d["e4"])),
        calibrate=FourphaseCalibrationParams(
            a=K(d["cal_a"]), b=K(d["cal_b"]), c=K(d["cal_c"]), d=K(d["cal_d"]),
            center_reduction=K(d["cal_center_reduction"])),
        volume=_volume(d),
        carrier_frequency=K(d["carrier_frequency"]), pulse_frequency=K(d["pulse_frequency"]),
        pulse_width=K(d["pulse_width"]), pulse_interval_random=K(d["pulse_interval_random"]),
        pulse_rise_time=K(d["pulse_rise_time"]), tau=K(d["tau_us"]),
        enable_pulse_frequency_adjustment=K(d["enable_pulse_frequency_adjustment"]),
        enable_burst_gap=K(d["enable_burst_gap"]),
    )
    alg = FOCStimFourphaseAlgorithm(Media(d["playing"]), params, _safety(d))
    if sensor is not None:
        alg.sensor_node = sensor
    return alg.parameter_dict()


def ported_fourphase(d, sensor=None):
    model = FourPhaseModel(FourPhaseCalibration(
        a=d["cal_a"], b=d["cal_b"], c=d["cal_c"], d=d["cal_d"],
        center_reduction=d["cal_center_reduction"]))
    return model.compute(
        e1=d["e1"], e2=d["e2"], e3=d["e3"], e4=d["e4"],
        volume=VolumeParts(d["master"], d["api"], d["inactivity"], d["external"]),
        carrier_frequency=d["carrier_frequency"], pulse_frequency=d["pulse_frequency"],
        pulse_width=d["pulse_width"], pulse_rise_time=d["pulse_rise_time"],
        pulse_interval_random=d["pulse_interval_random"], tau_us=d["tau_us"],
        enable_burst_gap=d["enable_burst_gap"],
        enable_pulse_frequency_adjustment=d["enable_pulse_frequency_adjustment"],
        safety=SafetyLimits(d["min_carrier"], d["max_carrier"], d["amps"]),
        playing=d["playing"],
        sensor=(sensor.process if sensor is not None else None),
    )


def assert_same(expected: dict, actual: dict):
    """Same keys, same order, every value within TOL."""
    assert list(expected.keys()) == list(actual.keys()), (list(expected.keys()), list(actual.keys()))
    for k in expected:
        e, a = float(expected[k]), float(actual[k])
        assert abs(e - a) <= TOL, f"{k}: upstream {e!r} != port {a!r}"
