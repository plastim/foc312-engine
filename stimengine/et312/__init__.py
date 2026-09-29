"""ET-312B / MK-312BT mode-engine emulation (pure, deterministic; no device I/O).

engine.py  ET312Engine: knobs + mode -> per-tick per-channel pulse parameters (ET312Frame)
vm.py      the routine VM (registers, bytecode, modulators, gate, timers) - firmware-faithful
modes.py   the built-in mode programs and mode-select logic
mapping.py ET312Frame -> FOC-Stim V4 (stock firmware) control targets, under the safety stack
preview.py `py -3.13 -m stimengine.et312.preview <mode>` renders 60 s of A/B streams to a PNG
elk.py     ErosLink .elk routines -> ET-312 program modules (ErosLink's compiler, byte-exact); ELK-FORMAT.md
javaser.py minimal Java-serialization reader used by elk.py
eroslink_cache.py  extracts the ErosLink CD's own routines from ErosLink_Installer.zip into a local cache
"""
from .engine import AdvancedParams, ChannelOutput, ET312Engine, ET312Frame
from .mapping import MappingConfig, V4Targets, map_frame
from .modes import IMPLEMENTED, MODE_BY_NAME, MODE_NAMES, STUBBED
from .vm import ET312VM, TICK_HZ, TICK_S

__all__ = [
    "AdvancedParams", "ChannelOutput", "ET312Engine", "ET312Frame", "ET312VM",
    "MappingConfig", "V4Targets", "map_frame", "IMPLEMENTED", "STUBBED",
    "MODE_BY_NAME", "MODE_NAMES", "TICK_HZ", "TICK_S",
]
