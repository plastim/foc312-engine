import numpy as np

def burst_gap_frequency_to_pulse_frequency(carrier_frequency, burst_gap_frequency, pulse_width):
    """
    Convert the burst gap frequency, which is 1/burst_gap_duration, to the pulse frequency
    :param carrier_frequency:
    :param burst_gap_frequency:
    :param pulse_width:
    :return:
    """
    active_duration = pulse_width/np.clip(carrier_frequency, 1, None)
    gap_duration = 1/np.clip(burst_gap_frequency, .1, None)
    duration = np.clip(active_duration + gap_duration, 0.001, None)
    return 1/duration

def pulse_frequency_to_burst_gap_frequency(carrier_frequency, pulse_frequency, pulse_width):
    duration = 1/np.clip(pulse_frequency, 0.1, None)
    active_duration = pulse_width/np.clip(carrier_frequency, 1, None)
    gap_duration = np.clip(duration - active_duration, .001, None)
    return 1/gap_duration
