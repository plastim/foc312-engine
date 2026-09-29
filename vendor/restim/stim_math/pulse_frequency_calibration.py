import numpy as np

class PulseFrequencyCalibration:
    @staticmethod
    def scale(pulse_frequency):
        return (PulseFrequencyCalibration.normalized_intensity(0) /
                PulseFrequencyCalibration.normalized_intensity(pulse_frequency))

    @staticmethod
    def normalized_intensity(pulse_frequency):
        """
        I collected figures of relative intensity at 1000, 1500 and 2000hz for pulse rates
        between 5 and 100hz. This formula gives the normalized intensity relative to a baseline
        of 50Hz.
        Data for 1000, 1500 and 2000Hz was extremely close, so I averaged them.
        10hz ~= 0.92
        50Hz = 1
        100Hz ~= 1.05
        :param pulse_frequency:
        :return:
        """
        pulse_frequency = np.clip(pulse_frequency, 0, 175)
        return .9115 + .00203 * pulse_frequency - 0.00000576 * pulse_frequency ** 2
