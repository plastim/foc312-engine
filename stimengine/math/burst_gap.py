"""Burst-gap <-> pulse-frequency conversion, ported from restim v1.66.

With burst gap enabled the user-facing "pulse frequency" is reinterpreted as
1 / (silent gap between bursts). The actual pulse rate sent to the device then
depends on how long each burst is (pulse_width carrier cycles at the carrier
frequency), so a wider or lower-carrier pulse lowers the real repetition rate.
"""
import numpy as np


def burst_gap_frequency_to_pulse_frequency(carrier_frequency, burst_gap_frequency, pulse_width):
    """Convert burst-gap frequency (1/gap_duration) to the pulse repetition frequency."""
    active_duration = pulse_width / np.clip(carrier_frequency, 1, None)
    gap_duration = 1 / np.clip(burst_gap_frequency, .1, None)
    duration = np.clip(active_duration + gap_duration, 0.001, None)
    return 1 / duration


def pulse_frequency_to_burst_gap_frequency(carrier_frequency, pulse_frequency, pulse_width):
    duration = 1 / np.clip(pulse_frequency, 0.1, None)
    active_duration = pulse_width / np.clip(carrier_frequency, 1, None)
    gap_duration = np.clip(duration - active_duration, .001, None)
    return 1 / gap_duration
