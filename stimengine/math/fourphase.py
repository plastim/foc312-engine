"""4-phase (e1..e4 direct intensity) model for FOC-Stim, ported from restim v1.66.

In 4-phase mode there is no position abstraction on the host at all: four
per-electrode intensities (0..1, clipped) go straight to the device as
AXIS_ELECTRODE_n_POWER, together with per-electrode calibration offsets (dB,
normalised so the loudest is 0) and a "reduction in center" factor. Everything
about how those four numbers become currents lives in the firmware.

restim.ini mapping (v1.66 keys): [calibration_four] a/b/c/d/center_reduction.
An older ini may carry a `center` key and no `d`; v1.66 ignores `center` and
defaults `d` to 0.0 and `center_reduction` to 0.07.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ._vendor import AxisType
from .volume import PulseChain, SafetyLimits, VolumeParts, finalize_amps, pulse_chain, reduce_only


@dataclass(frozen=True)
class FourPhaseCalibration:
    a: float = 0.0                 # dB -> AXIS_CALIBRATION_4_A
    b: float = 0.0                 # dB -> AXIS_CALIBRATION_4_B
    c: float = 0.0                 # dB -> AXIS_CALIBRATION_4_C
    d: float = 0.0                 # dB -> AXIS_CALIBRATION_4_D
    center_reduction: float = 0.07 # fraction -> AXIS_CALIBRATION_4_REDUCTION_IN_CENTER

    def normalized(self) -> "FourPhaseCalibration":
        """What the GUI does on load: shift so the loudest electrode is 0 dB."""
        values = np.array([self.a, self.b, self.c, self.d], dtype=float)
        values = values - np.max(values)
        return FourPhaseCalibration(*map(float, values), self.center_reduction)


def clip_intensities(e1, e2, e3, e4) -> tuple[float, float, float, float]:
    """stim_math/fourphase_intensity.py FourPhaseIntensity.get_position"""
    return (float(np.clip(e1, 0, 1)), float(np.clip(e2, 0, 1)),
            float(np.clip(e3, 0, 1)), float(np.clip(e4, 0, 1)))


class FourPhaseModel:
    def __init__(self, calibration: FourPhaseCalibration | None = None):
        self.calibration = calibration or FourPhaseCalibration()

    def compute(self, *, e1: float, e2: float, e3: float, e4: float, volume: VolumeParts,
                carrier_frequency: float, pulse_frequency: float, pulse_width: float,
                pulse_rise_time: float, pulse_interval_random: float, tau_us: float,
                enable_burst_gap: bool, enable_pulse_frequency_adjustment: bool,
                safety: SafetyLimits, playing: bool = True,
                sensor=None) -> dict:
        """Exactly what FOCStimFourphaseAlgorithm.parameter_dict() returns.

        `sensor`, if given, is a callable(dict) -> None mutating
        {'volume','e1','e2','e3','e4'} in place; volume may only go down.
        """
        chain: PulseChain = pulse_chain(
            volume, carrier_frequency=carrier_frequency, pulse_frequency=pulse_frequency,
            pulse_width=pulse_width, tau_us=tau_us, enable_burst_gap=enable_burst_gap,
            enable_pulse_frequency_adjustment=enable_pulse_frequency_adjustment, safety=safety)
        vol = chain.volume

        a, b, c, d = clip_intensities(e1, e2, e3, e4)

        if sensor is not None:
            params = {'volume': vol, 'e1': a, 'e2': b, 'e3': c, 'e4': d}
            sensor(params)
            vol = reduce_only(vol, params['volume'])
            a, b, c, d = params['e1'], params['e2'], params['e3'], params['e4']

        amps = finalize_amps(vol, playing, safety)
        cal = self.calibration

        return {
            AxisType.AXIS_ELECTRODE_1_POWER: a,
            AxisType.AXIS_ELECTRODE_2_POWER: b,
            AxisType.AXIS_ELECTRODE_3_POWER: c,
            AxisType.AXIS_ELECTRODE_4_POWER: d,
            AxisType.AXIS_WAVEFORM_AMPLITUDE_AMPS: amps,
            AxisType.AXIS_CARRIER_FREQUENCY_HZ: chain.carrier_frequency,
            AxisType.AXIS_PULSE_FREQUENCY_HZ: chain.pulse_frequency,
            AxisType.AXIS_PULSE_WIDTH_IN_CYCLES: pulse_width,
            AxisType.AXIS_PULSE_RISE_TIME_CYCLES: pulse_rise_time,
            AxisType.AXIS_PULSE_INTERVAL_RANDOM_PERCENT: pulse_interval_random,
            AxisType.AXIS_CALIBRATION_4_A: cal.a,
            AxisType.AXIS_CALIBRATION_4_B: cal.b,
            AxisType.AXIS_CALIBRATION_4_C: cal.c,
            AxisType.AXIS_CALIBRATION_4_D: cal.d,
            AxisType.AXIS_CALIBRATION_4_REDUCTION_IN_CENTER: cal.center_reduction,
        }
