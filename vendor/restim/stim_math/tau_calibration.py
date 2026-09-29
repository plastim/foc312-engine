

class TauCalibration:
    @staticmethod
    def derating_factor(max_frequency, frequency, tau):
        """
        :param max_frequency:   carrier frequency at which derating = 1 (i.e. no derating)
        :param frequency:       carrier frequency of the pulse
        :param tau:             time constant of the nerves, ~355e-6
        :return:                volume of the pulse, such that it has equal subjective intensity as a pulse at max carrier frequency.
        """
        # this formula follows from Qt = Q0 * (1 + pw/tau)
        return (frequency * tau + 0.5) / (max_frequency * tau + 0.5)
