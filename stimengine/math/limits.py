"""Hard limits, ported verbatim from restim v1.66 `stim_math/limits.py`.

Only the FOC-relevant classes are carried over. Values are upstream's; the
engine's *configured* caps in config/engine.toml must sit inside these.
"""


class PulseFrequency:
    min = 1
    max = 300


class PulseWidth:
    min = 3
    max = 100


class PulseRiseTime:
    min = 2
    max = 100


class CarrierFrequencyFOC:
    min = 300   # Hz
    max = 2000


class WaveformAmplitudeFOC:
    min = 0.01  # Amperes
    max = 0.20


# upstream typo kept as an alias so ported call sites read the same
WaveformAmpltiudeFOC = WaveformAmplitudeFOC


class PulseFrequencyFOC:
    min = 1
    max = 100
