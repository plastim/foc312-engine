"""Carrier-frequency derating, ported from restim v1.66 `stim_math/tau_calibration.py`.

Model: a nerve with membrane time constant tau responds to charge, and the
charge delivered per half-cycle of a current-controlled carrier falls as the
carrier frequency rises (shorter pulses). Upstream's relation is
    Q_threshold = Q0 * (1 + pw/tau)
which, for a carrier of frequency f (pulse width ~ 1/(2f)), reduces to the
ratio below. At `max_frequency` the factor is exactly 1; at lower carriers it is
< 1 so subjective intensity stays roughly constant across the carrier range.
"""


def derating_factor(max_frequency: float, frequency: float, tau: float) -> float:
    """
    :param max_frequency: carrier frequency at which derating = 1 (no derating)
    :param frequency:     carrier frequency of the pulse
    :param tau:           nerve time constant in seconds (~355e-6)
    :return: volume multiplier giving equal subjective intensity to a pulse at max carrier
    """
    return (frequency * tau + 0.5) / (max_frequency * tau + 0.5)


class TauCalibration:
    """Upstream-shaped namespace; prefer the module-level function."""
    derating_factor = staticmethod(derating_factor)
