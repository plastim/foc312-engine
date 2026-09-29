import numpy as np

from stim_math import trig
from stim_math.audio_gen.params import ThreephasePositionParams, ThreephasePositionTransformParams
from stim_math.threephase_coordinate_transform import ThreePhaseCoordinateTransform, \
    ThreePhaseCoordinateTransformMapToEdge


class ThreePhasePosition:
    def __init__(self, position: ThreephasePositionParams, transform: ThreephasePositionTransformParams):
        self.position_params = position
        self.transform_params = transform

    def get_position(self, command_timeline):
        alpha = self.position_params.alpha.interpolate(command_timeline)
        beta = self.position_params.beta.interpolate(command_timeline)
        return self.transform_position(alpha, beta)

    def transform_position(self, alpha, beta):

        # normalize (alpha, beta) to be within the unit circle.
        norm = np.clip(trig.norm(alpha, beta), 1.0, None)
        alpha /= norm
        beta /= norm

        # # mobius transform...
        # z = alpha + beta * 1j
        # a = self.params.focus_alpha.last_value() + self.params.focus_beta.last_value() * 1j
        # z = (z - a) / (1 - np.conj(a) * z)
        # alpha = z.real
        # beta = z.imag

        if self.transform_params.transform_enabled.last_value():
            transform = ThreePhaseCoordinateTransform(
                self.transform_params.transform_rotation_degrees.last_value(),
                self.transform_params.transform_mirror.last_value(),
                self.transform_params.transform_top_limit.last_value(),
                self.transform_params.transform_bottom_limit.last_value(),
                self.transform_params.transform_left_limit.last_value(),
                self.transform_params.transform_right_limit.last_value(),
            )
            alpha, beta = transform.transform(alpha, beta)
            norm = np.clip(trig.norm(alpha, beta), 1.0, None)
            alpha /= norm
            beta /= norm
        if self.transform_params.map_to_edge_enabled.last_value():
            transform = ThreePhaseCoordinateTransformMapToEdge(
                self.transform_params.map_to_edge_start.last_value(),
                self.transform_params.map_to_edge_length.last_value(),
                self.transform_params.map_to_edge_invert.last_value(),
            )
            alpha, beta = transform.transform(alpha, beta)
            norm = np.clip(trig.norm(alpha, beta), 1.0, None)
            alpha /= norm
            beta /= norm

        return alpha, beta