"""Pulse-rate intensity compensation, ported from restim v1.66.

Upstream fitted a quadratic to perceived relative intensity vs pulse rate
(5-100 Hz, measured at 1000/1500/2000 Hz carriers and averaged, baseline 50 Hz):
    10 Hz ~ 0.92,  50 Hz = 1.00,  100 Hz ~ 1.05
`scale()` returns the multiplier that flattens that curve, normalised so a
pulse rate of 0 maps to 1.0.
"""
import numpy as np


def normalized_intensity(pulse_frequency: float) -> float:
    pulse_frequency = np.clip(pulse_frequency, 0, 175)
    return .9115 + .00203 * pulse_frequency - 0.00000576 * pulse_frequency ** 2


def scale(pulse_frequency: float) -> float:
    return normalized_intensity(0) / normalized_intensity(pulse_frequency)


class PulseFrequencyCalibration:
    """Upstream-shaped namespace; prefer the module-level functions."""
    scale = staticmethod(scale)
    normalized_intensity = staticmethod(normalized_intensity)
