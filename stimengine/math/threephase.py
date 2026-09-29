"""3-phase (alpha/beta) model for FOC-Stim, ported from restim v1.66.

What the host actually does in 3-phase mode:
  * clamps (alpha, beta) into the unit circle,
  * optionally applies a user "transform" (rotate / mirror / rectangular limits)
    or a "map to edge" (collapse the 2-D position onto an arc of the circle),
  * sends alpha/beta AND the three calibration numbers raw to the device.
The position -> per-electrode current vectoring happens in the firmware.
(`stim_math/transforms.py` ab_to_e123 is the *audio* path and is not used here.)

Calibration wire format (upstream naming is confusing, kept faithfully):
  params.calibrate.center  -> AXIS_CALIBRATION_3_CENTER   (dB, <= 0; "center reduction")
  params.calibrate.neutral -> AXIS_CALIBRATION_3_UP       (up/down bias, GUI "neutral")
  params.calibrate.right   -> AXIS_CALIBRATION_3_LEFT     (left/right bias, GUI "right")
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ._vendor import AxisType
from .volume import PulseChain, SafetyLimits, VolumeParts, finalize_amps, pulse_chain, reduce_only


def _norm(x, y):
    return np.linalg.norm((x, y), axis=0)


# ---- calibration helpers (from qt_ui/three_phase_settings_widget.py) ---------

def center_calib_to_reduction(calib_db: float) -> float:
    """dB -> percent reduction (what the GUI shows)."""
    power = 10 ** (calib_db / 10)
    return (1 - power) * 100


def center_reduction_to_calib(reduction_percent: float) -> float:
    """percent reduction -> dB (what is sent to the device)."""
    power = 1 - reduction_percent / 100
    return float(np.log10(power) * 10)


@dataclass(frozen=True)
class ThreePhaseCalibration:
    """Values as *sent* (restim.ini [hw_calibration] neutral/right/center)."""
    neutral: float = 0.0     # up/down   -> AXIS_CALIBRATION_3_UP
    right: float = 0.0       # left/right-> AXIS_CALIBRATION_3_LEFT
    center: float = -0.604   # dB        -> AXIS_CALIBRATION_3_CENTER (upstream default)


@dataclass(frozen=True)
class ThreePhaseTransform:
    """restim.ini [threephase_transform]; all disabled = identity."""
    transform_enabled: bool = False
    rotation_degrees: float = 0.0
    mirror: bool = False
    top_limit: float = 1.0
    bottom_limit: float = -1.0
    left_limit: float = -1.0
    right_limit: float = 1.0
    map_to_edge_enabled: bool = False
    map_to_edge_start: float = 0.0
    map_to_edge_length: float = 0.0
    map_to_edge_invert: bool = False


# ---- coordinate transforms (stim_math/threephase_coordinate_transform.py) -----

class CoordinateTransform:
    def __init__(self, rotation, mirror, top, bottom, left, right):
        mirror_matrix = np.eye(3)
        if mirror:
            mirror_matrix[1, 1] = -1

        rotation_in_rad = np.deg2rad(rotation)
        rotation_in_rad *= -1          # rotate clockwise
        cos, sin = np.cos(rotation_in_rad), np.sin(rotation_in_rad)
        rotation_matrix = np.array([[cos, -sin, 0], [sin, cos, 0], [0, 0, 1]])

        alpha_center = (top + bottom) / 2
        beta_center = -(left + right) / 2
        alpha_scale = (top - bottom) / 2
        beta_scale = (right - left) / 2
        limits_matrix = np.array([
            [alpha_scale, 0, alpha_center],
            [0, beta_scale, beta_center],
            [0, 0, 1]]
        )
        self.matrix = limits_matrix @ rotation_matrix @ mirror_matrix
        self.mirror = mirror

    def transform(self, alpha, beta):
        a, b, _ = self.matrix @ [alpha, beta, np.ones_like(alpha)]
        return a, b

    def inverse_transform(self, alpha, beta):
        try:
            matrix = np.linalg.inv(self.matrix)
        except np.linalg.LinAlgError:
            matrix = np.eye(3)
            if self.mirror:
                matrix[1, 1] = -1
        a, b, _ = matrix @ [alpha, beta, np.ones_like(alpha)]
        return a, b


class MapToEdgeTransform:
    def __init__(self, start, length, invert):
        self.start = start
        self.end = start + length
        if invert:
            self.start, self.end = self.end, self.start

    def transform(self, alpha, beta):
        angle = self.start + (alpha * -0.5 + 0.5) * (self.end - self.start)
        return np.cos(np.deg2rad(-angle)), np.sin(np.deg2rad(-angle))


# ---- the model ---------------------------------------------------------------

class ThreePhaseModel:
    def __init__(self, calibration: ThreePhaseCalibration | None = None,
                 transform: ThreePhaseTransform | None = None):
        self.calibration = calibration or ThreePhaseCalibration()
        self.transform = transform or ThreePhaseTransform()

    def transform_position(self, alpha: float, beta: float) -> tuple[float, float]:
        """stim_math/threephase_position.py ThreePhasePosition.transform_position"""
        alpha = np.asarray(alpha, dtype=float)
        beta = np.asarray(beta, dtype=float)
        t = self.transform

        norm = np.clip(_norm(alpha, beta), 1.0, None)
        alpha = alpha / norm
        beta = beta / norm

        if t.transform_enabled:
            ct = CoordinateTransform(t.rotation_degrees, t.mirror, t.top_limit,
                                     t.bottom_limit, t.left_limit, t.right_limit)
            alpha, beta = ct.transform(alpha, beta)
            norm = np.clip(_norm(alpha, beta), 1.0, None)
            alpha = alpha / norm
            beta = beta / norm
        if t.map_to_edge_enabled:
            mt = MapToEdgeTransform(t.map_to_edge_start, t.map_to_edge_length, t.map_to_edge_invert)
            alpha, beta = mt.transform(alpha, beta)
            norm = np.clip(_norm(alpha, beta), 1.0, None)
            alpha = alpha / norm
            beta = beta / norm

        return float(alpha), float(beta)

    def compute(self, *, alpha: float, beta: float, volume: VolumeParts,
                carrier_frequency: float, pulse_frequency: float, pulse_width: float,
                pulse_rise_time: float, pulse_interval_random: float, tau_us: float,
                enable_burst_gap: bool, enable_pulse_frequency_adjustment: bool,
                safety: SafetyLimits, playing: bool = True,
                sensor=None) -> dict:
        """Exactly what FOCStimThreephaseAlgorithm.parameter_dict() returns.

        `sensor`, if given, is a callable(dict) -> None mutating
        {'volume','alpha','beta'} in place; volume may only go down.
        """
        chain: PulseChain = pulse_chain(
            volume, carrier_frequency=carrier_frequency, pulse_frequency=pulse_frequency,
            pulse_width=pulse_width, tau_us=tau_us, enable_burst_gap=enable_burst_gap,
            enable_pulse_frequency_adjustment=enable_pulse_frequency_adjustment, safety=safety)
        vol = chain.volume

        if sensor is not None:
            d = {'volume': vol, 'alpha': alpha, 'beta': beta}
            sensor(d)
            vol = reduce_only(vol, d['volume'])
            alpha, beta = d['alpha'], d['beta']

        alpha, beta = self.transform_position(alpha, beta)
        amps = finalize_amps(vol, playing, safety)

        return {
            AxisType.AXIS_POSITION_ALPHA: alpha,
            AxisType.AXIS_POSITION_BETA: beta,
            AxisType.AXIS_WAVEFORM_AMPLITUDE_AMPS: amps,
            AxisType.AXIS_CARRIER_FREQUENCY_HZ: chain.carrier_frequency,
            AxisType.AXIS_PULSE_FREQUENCY_HZ: chain.pulse_frequency,
            AxisType.AXIS_PULSE_WIDTH_IN_CYCLES: pulse_width,
            AxisType.AXIS_PULSE_RISE_TIME_CYCLES: pulse_rise_time,
            AxisType.AXIS_PULSE_INTERVAL_RANDOM_PERCENT: pulse_interval_random,
            AxisType.AXIS_CALIBRATION_3_CENTER: self.calibration.center,
            AxisType.AXIS_CALIBRATION_3_UP: self.calibration.neutral,
            AxisType.AXIS_CALIBRATION_3_LEFT: self.calibration.right,
        }
