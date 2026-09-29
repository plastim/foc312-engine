"""Pure-Python port of restim v1.66's FOC-Stim math (no Qt, no hardware).

Oracle: vendor/restim/. Tests: tests/test_math_*.py. Map: NOTES.md.
"""
from .fourphase import FourPhaseCalibration, FourPhaseModel
from .frame import ModelSet, StimFrame, evaluate, to_axis_moves
from .threephase import ThreePhaseCalibration, ThreePhaseModel, ThreePhaseTransform
from .volume import SafetyLimits, VolumeParts

__all__ = [
    "FourPhaseCalibration", "FourPhaseModel", "ModelSet", "StimFrame", "evaluate",
    "to_axis_moves", "ThreePhaseCalibration", "ThreePhaseModel", "ThreePhaseTransform",
    "SafetyLimits", "VolumeParts",
]
