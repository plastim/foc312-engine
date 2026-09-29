"""Temporary bridge to the vendored restim protobuf enums.

TODO(Fork A): switch to `stimengine.device.proto.constants_pb2` once the pb2
files are re-homed under stimengine/device/proto/. Nothing else in
stimengine.math depends on the vendor tree.
"""
from __future__ import annotations

import sys
from pathlib import Path

_VENDOR = Path(__file__).resolve().parents[2] / "vendor" / "restim"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

from device.focstim.constants_pb2 import AxisType, OutputMode  # noqa: E402

__all__ = ["AxisType", "OutputMode"]
