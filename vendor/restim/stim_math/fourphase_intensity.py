import numpy as np

from stim_math.audio_gen.params import FourphaseIntensityParams

class FourPhaseIntensity:
    def __init__(self, position: FourphaseIntensityParams):
        self.position_params = position

    def get_position(self, command_timeline):
        a = np.clip(self.position_params.a.interpolate(command_timeline), 0, 1)
        b = np.clip(self.position_params.b.interpolate(command_timeline), 0, 1)
        c = np.clip(self.position_params.c.interpolate(command_timeline), 0, 1)
        d = np.clip(self.position_params.d.interpolate(command_timeline), 0, 1)

        return a, b, c, d
