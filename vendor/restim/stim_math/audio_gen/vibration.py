import numpy as np

from stim_math import limits, amplitude_modulation
from stim_math.audio_gen.params import VibrationParams
from stim_math.sine_generator import AngleGeneratorWithVaryingIPI


class VibrationAlgorithm:
    def __init__(self, vib_1: VibrationParams, vib_2: VibrationParams):
        self.vib_1 = vib_1
        self.vibration_1_angle = AngleGeneratorWithVaryingIPI()
        self.vib_2 = vib_2
        self.vibration_2_angle = AngleGeneratorWithVaryingIPI()

    def generate_vibration_signal(self, command_timeline, samplerate, n_samples: int):
        volume = 1

        volume *= self._calculate_modulation(
            command_timeline,
            samplerate, n_samples,
            self.vib_1, self.vibration_1_angle,
        )

        volume *= self._calculate_modulation(
            command_timeline,
            samplerate, n_samples,
            self.vib_2, self.vibration_2_angle,
        )

        return volume

    def generate_vibration_float(self, command_timeline, samplerate, n_samples):
        volume = self.generate_vibration_signal(command_timeline, samplerate, n_samples)
        try:
            return volume[0]
        except TypeError:
            return volume

    def _calculate_modulation(self, command_timeline, samplerate, n_samples, params: VibrationParams, angle_generator):
        is_enabled = params.enabled.last_value()
        modulation_frequency = params.frequency.interpolate(command_timeline)
        modulation_strength = params.strength.interpolate(command_timeline)
        modulation_left_right_bias = params.left_right_bias.interpolate(command_timeline)
        modulation_high_low_bias = params.high_low_bias.interpolate(command_timeline)
        modulation_random = params.high_low_bias.interpolate(command_timeline)

        if not is_enabled or modulation_frequency == 0:
            return 1

        modulation_frequency = np.clip(modulation_frequency,
                                       limits.ModulationFrequency.min,
                                       limits.ModulationFrequency.max)
        theta = angle_generator.generate(n_samples, modulation_frequency, samplerate, modulation_random)
        modulation = amplitude_modulation.SineModulation(
            theta,
            modulation_strength,
            modulation_left_right_bias,
            modulation_high_low_bias,
        )
        return modulation.get_modulation_signal()

