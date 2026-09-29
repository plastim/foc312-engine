"""Where this project's sibling projects live (each its own repository, next to this one under Projects):

    foc312            the FOC-Stim firmware fork (source, simulator, bench scripts, release images)
    foc312-m5remote   the M5 remote (its firmware, the portable C core, host test tools, release images)

Override with $FOC312_FIRMWARE_DIR / $FOC312_M5REMOTE_DIR when they are somewhere else.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _sibling(name: str, env: str) -> Path:
    return Path(os.environ.get(env) or ROOT.parent / name)


FIRMWARE_DIR = _sibling("foc312", "FOC312_FIRMWARE_DIR")
REMOTE_DIR = _sibling("foc312-m5remote", "FOC312_M5REMOTE_DIR")
BUILD_DIR = ROOT / "build"          # local build output (gitignored): the remote's pattern pack, host test tools
