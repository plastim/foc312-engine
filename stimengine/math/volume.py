"""Restim's volume law and the shared pulse-parameter chain (v1.66 semantics).

Order of operations is upstream's and is load-bearing for the oracle tests:

  1. volume  = clip(master) * clip(api) * clip(inactivity) * clip(external)
  2. carrier = clip(carrier, min_f, max_f)      # safety limits intersected with FOC hard limits
     volume *= clip(tau_derating(max_f, carrier, tau), 0, 1)
  3. if burst_gap:  pulse_freq = burst_gap -> pulse_freq(carrier, pulse_freq, pulse_width)
  4. if pf_adjust:  volume *= clip(pulse_frequency_scale(pulse_freq), 0, 1)
  5. (sensor hook: may only lower volume)       # engine-side, see reduce_only()
  6. if not playing: volume = 0
  7. amps = volume * waveform_amplitude_amps
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import limits
from .burst_gap import burst_gap_frequency_to_pulse_frequency
from .pulse_frequency_calibration import scale as pulse_frequency_scale
from .tau_calibration import derating_factor


@dataclass(frozen=True)
class VolumeParts:
    master: float = 0.0
    api: float = 1.0
    inactivity: float = 1.0
    external: float = 1.0


@dataclass(frozen=True)
class SafetyLimits:
    """Mirror of upstream SafetyParamsFOC (the device-wizard values)."""
    minimum_carrier_frequency: float
    maximum_carrier_frequency: float
    waveform_amplitude_amps: float

    def validate(self) -> None:
        eps = 0.0001
        if not (limits.WaveformAmplitudeFOC.min - eps <= self.waveform_amplitude_amps
                <= limits.WaveformAmplitudeFOC.max + eps):
            raise ValueError(
                f"waveform_amplitude_amps {self.waveform_amplitude_amps} outside "
                f"[{limits.WaveformAmplitudeFOC.min}, {limits.WaveformAmplitudeFOC.max}]")


def base_volume(parts: VolumeParts) -> float:
    return float(
        np.clip(parts.master, 0, 1)
        * np.clip(parts.api, 0, 1)
        * np.clip(parts.inactivity, 0, 1)
        * np.clip(parts.external, 0, 1)
    )


def carrier_bounds(safety: SafetyLimits) -> tuple[float, float]:
    """(min, max) carrier after intersecting FOC hard limits with the safety limits."""
    maximum = float(np.clip(limits.CarrierFrequencyFOC.max,
                            safety.minimum_carrier_frequency, safety.maximum_carrier_frequency))
    minimum = float(np.clip(limits.CarrierFrequencyFOC.min,
                            safety.minimum_carrier_frequency, safety.maximum_carrier_frequency))
    return minimum, maximum


@dataclass(frozen=True)
class PulseChain:
    volume: float            # 0..1 after all derating, before amps scaling
    carrier_frequency: float
    pulse_frequency: float   # as sent to the device (burst-gap converted if enabled)
    pulse_width: float


def pulse_chain(parts: VolumeParts, *, carrier_frequency: float, pulse_frequency: float,
                pulse_width: float, tau_us: float, enable_burst_gap: bool,
                enable_pulse_frequency_adjustment: bool, safety: SafetyLimits) -> PulseChain:
    volume = base_volume(parts)
    minimum_frequency, maximum_frequency = carrier_bounds(safety)
    tau = tau_us * 1e-6

    carrier_frequency = float(np.clip(carrier_frequency, minimum_frequency, maximum_frequency))
    volume *= float(np.clip(derating_factor(maximum_frequency, carrier_frequency, tau), 0, 1))

    if enable_burst_gap:
        pulse_frequency = float(burst_gap_frequency_to_pulse_frequency(
            carrier_frequency, pulse_frequency, pulse_width))

    if enable_pulse_frequency_adjustment:
        volume *= float(np.clip(pulse_frequency_scale(pulse_frequency), 0, 1))

    return PulseChain(volume, carrier_frequency, float(pulse_frequency), float(pulse_width))


def reduce_only(original: float, proposed: float) -> float:
    """Upstream's sensor-node rule: a sensor may only lower volume, never raise it."""
    return float(np.clip(proposed, 0, original))


def finalize_amps(volume: float, playing: bool, safety: SafetyLimits) -> float:
    if not playing:
        volume *= 0
    return volume * safety.waveform_amplitude_amps
