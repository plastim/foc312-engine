"""The output cap as a hub setting: config/engine.toml [signal] waveform_amplitude_amps (the most any level can reach,
before level x master x the box's own knob). Never above the box's own maximum (engine.HARD_AMPS_CAP, 0.2 A), which
the engine also refuses at start. The file is edited in place (one number), so its comments and layout are kept.
Takes effect the next time the engine connects; the M5 remote gets it with its next Load."""
from __future__ import annotations

import os
import re
import tomllib

from ..engine import HARD_AMPS_CAP
from . import m5settings

MIN_AMPS = 0.02
DEFAULT_AMPS = 0.15
_LINE = re.compile(r"(?m)^([ \t]*waveform_amplitude_amps[ \t]*=[ \t]*)([0-9]*\.?[0-9]+)")


def read() -> float:
    try:
        with open(m5settings.ENGINE_TOML, "rb") as f:
            cfg = tomllib.load(f)
    except (OSError, ValueError):
        return DEFAULT_AMPS
    return float(cfg.get("signal", {}).get("waveform_amplitude_amps", DEFAULT_AMPS))


def write(amps: float) -> float:
    """Set the cap (amps at the body). Refuses anything outside MIN_AMPS..HARD_AMPS_CAP; returns the value written."""
    amps = round(float(amps), 3)
    if not (MIN_AMPS <= amps <= HARD_AMPS_CAP):
        raise ValueError(f"the cap must be {MIN_AMPS * 1000:.0f} to {HARD_AMPS_CAP * 1000:.0f} mA")
    path = m5settings.ENGINE_TOML
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if _LINE.search(text):
        new = _LINE.sub(lambda m: f"{m.group(1)}{amps}", text, count=1)
    elif re.search(r"(?m)^\[signal\][ \t]*$", text):
        new = re.sub(r"(?m)^(\[signal\][ \t]*\n)", rf"\g<1>waveform_amplitude_amps = {amps}\n", text, count=1)
    else:
        new = text + ("" if text.endswith("\n") or not text else "\n") + f"\n[signal]\nwaveform_amplitude_amps = {amps}\n"
    parsed = tomllib.loads(new)                                   # never write a file the engine could not read
    if float(parsed.get("signal", {}).get("waveform_amplitude_amps", -1)) != amps:
        raise ValueError("could not set the cap in config/engine.toml")
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(new, encoding="utf-8")
    os.replace(tmp, path)
    return amps
