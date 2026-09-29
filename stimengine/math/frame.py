"""StimFrame: one host-side control snapshot -> the axis/value pairs the device gets.

This is the seam between "control" (patterns, T-code, Claude) and "device"
(Fork A's axis_move_to sender). A frame carries plain floats only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ._vendor import AxisType  # TODO(Fork A): stimengine.device.proto.constants_pb2
from .fourphase import FourPhaseCalibration, FourPhaseModel
from .threephase import ThreePhaseCalibration, ThreePhaseModel, ThreePhaseTransform
from .volume import SafetyLimits, VolumeParts

Mode = Literal["threephase", "fourphase"]


@dataclass
class StimFrame:
    mode: Mode = "threephase"
    # 3-phase position (ignored in fourphase)
    alpha: float = 0.0
    beta: float = 0.0
    # 4-phase intensities (ignored in threephase)
    e1: float = 0.0
    e2: float = 0.0
    e3: float = 0.0
    e4: float = 0.0
    # shared
    volume: VolumeParts = field(default_factory=VolumeParts)
    carrier_frequency: float = 790.0
    pulse_frequency: float = 74.0
    pulse_width: float = 11.5
    pulse_rise_time: float = 5.0
    pulse_interval_random: float = 0.0
    tau_us: float = 355.0
    enable_burst_gap: bool = True
    enable_pulse_frequency_adjustment: bool = True
    playing: bool = True


@dataclass
class ModelSet:
    """Everything static-ish that a frame is evaluated against."""
    safety: SafetyLimits
    threephase: ThreePhaseModel = field(default_factory=ThreePhaseModel)
    fourphase: FourPhaseModel = field(default_factory=FourPhaseModel)

    @classmethod
    def from_config(cls, cfg: dict) -> "ModelSet":
        """Build from a parsed config/engine.toml dict."""
        sig = cfg.get("signal", {})
        cal3 = cfg.get("calibration", {}).get("threephase", {})
        cal4 = cfg.get("calibration", {}).get("fourphase", {})
        safety = SafetyLimits(
            minimum_carrier_frequency=float(sig.get("min_carrier_hz", 500)),
            maximum_carrier_frequency=float(sig.get("max_carrier_hz", 2000)),
            waveform_amplitude_amps=float(sig.get("waveform_amplitude_amps", 0.15)),
        )
        safety.validate()
        three = ThreePhaseModel(
            ThreePhaseCalibration(
                neutral=float(cal3.get("neutral", 0.0)),
                right=float(cal3.get("right", 0.0)),
                center=float(cal3.get("center", -0.604)),
            ),
            ThreePhaseTransform(),
        )
        four = FourPhaseModel(FourPhaseCalibration(
            a=float(cal4.get("a", 0.0)), b=float(cal4.get("b", 0.0)),
            c=float(cal4.get("c", 0.0)), d=float(cal4.get("d", 0.0)),
            center_reduction=float(cal4.get("center_reduction", 0.07)),
        ))
        return cls(safety=safety, threephase=three, fourphase=four)


def evaluate(frame: StimFrame, models: ModelSet, sensor=None) -> dict:
    """Frame -> {AxisType: value}, identical to restim's parameter_dict() for the mode."""
    common = dict(
        volume=frame.volume, carrier_frequency=frame.carrier_frequency,
        pulse_frequency=frame.pulse_frequency, pulse_width=frame.pulse_width,
        pulse_rise_time=frame.pulse_rise_time, pulse_interval_random=frame.pulse_interval_random,
        tau_us=frame.tau_us, enable_burst_gap=frame.enable_burst_gap,
        enable_pulse_frequency_adjustment=frame.enable_pulse_frequency_adjustment,
        safety=models.safety, playing=frame.playing, sensor=sensor,
    )
    if frame.mode == "threephase":
        return models.threephase.compute(alpha=frame.alpha, beta=frame.beta, **common)
    if frame.mode == "fourphase":
        return models.fourphase.compute(e1=frame.e1, e2=frame.e2, e3=frame.e3, e4=frame.e4, **common)
    raise ValueError(f"unknown mode {frame.mode!r}")


def to_axis_moves(frame: StimFrame, models: ModelSet, sensor=None) -> list[tuple[AxisType, float]]:
    """Ordered (AxisType, value) pairs, in upstream's dict order, for axis_move_to."""
    return [(axis, float(value)) for axis, value in evaluate(frame, models, sensor).items()]
