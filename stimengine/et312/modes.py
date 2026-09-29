"""ET-312B built-in modes as program blocks, plus the mode-select logic (Function_0x604).

Sources: see vm.py.  Block indices are the firmware's own (the lookup table at flash 0x1c3e; the
`Mode: ... (blocks x/y)` annotations in the buttshock tags file).  The blocks themselves are loaded at
runtime by fwdata.py from the user's own firmware image; they are not part of this source.
Register names: vm.R.

Mode numbers ($407b):  0x76 Waves .. 0x94 User7 (buttshock-protocol-docs "Box Modes" table).
"""
from __future__ import annotations

from .vm import ET312VM

# --- mode numbers -------------------------------------------------------------------------
WAVES, STROKE, CLIMB, COMBO, INTENSE, RHYTHM = 0x76, 0x77, 0x78, 0x79, 0x7A, 0x7B
AUDIO1, AUDIO2, AUDIO3, SPLIT, RANDOM1, RANDOM2 = 0x7C, 0x7D, 0x7E, 0x7F, 0x80, 0x81
TOGGLE, ORGASM, TORMENT, PHASE1, PHASE2, PHASE3 = 0x82, 0x83, 0x84, 0x85, 0x86, 0x87
USER1, USER2, USER3, USER4, USER5, USER6, USER7 = 0x88, 0x89, 0x90, 0x91, 0x92, 0x93, 0x94

MODE_NAMES = {
    WAVES: "waves", STROKE: "stroke", CLIMB: "climb", COMBO: "combo", INTENSE: "intense",
    RHYTHM: "rhythm", AUDIO1: "audio1", AUDIO2: "audio2", AUDIO3: "audio3", SPLIT: "split",
    RANDOM1: "random1", RANDOM2: "random2", TOGGLE: "toggle", ORGASM: "orgasm",
    TORMENT: "torment", PHASE1: "phase1", PHASE2: "phase2", PHASE3: "phase3",
    USER1: "user1", USER2: "user2", USER3: "user3", USER4: "user4", USER5: "user5",
    USER6: "user6", USER7: "user7",
}
# PlaStim variants of a built-in mode: the mode's own program runs unchanged; engine.py changes only WHEN the VM ticks
# (slows or pauses its clock at points found from the mode's own registers). Numbers in a range the ET-312 never uses.
CLIMB_SLOW, CLIMB_HOLD = 0xF0, 0xF1
VARIANTS = {CLIMB_SLOW: (CLIMB, "slow_finish"), CLIMB_HOLD: (CLIMB, "peak_hold")}
MODE_NAMES.update({CLIMB_SLOW: "climb_slow", CLIMB_HOLD: "climb_hold"})
MODE_BY_NAME = {v: k for k, v in MODE_NAMES.items()}
# what the emulator implements fully vs. what is a stub
IMPLEMENTED = ("waves", "stroke", "climb", "combo", "intense", "rhythm", "split", "random1",
               "random2", "toggle", "orgasm", "torment", "phase1", "phase2", "phase3")
STUBBED = ("audio1", "audio2", "audio3")      # audio input replaces the intensity value; no audio here
USER_MODES = ("user1", "user2", "user3", "user4", "user5", "user6", "user7")

# --- program blocks ------------------------------------------------------------------------
# The 36 blocks the built-in modes are made of are the ET-312B firmware's own (ErosTek's) and are not in this
# source: fwdata.py loads them from the user's firmware image (or a JSON extracted from it) at runtime. Only the
# two single-register blocks every mode start runs are defined here, so ErosLink routines and our own routines
# work without that data: block 0 = both gates off, block 1 = both gates on ($90 gate value 6 / 7).
CORE_BLOCKS: dict[int, list[tuple]] = {
    0: [('set', 0, 0x90, 0x06)],
    1: [('set', 0, 0x90, 0x07)],
}

# first block, second block (only when channel B is part of the pass)
MODE_BLOCKS = {
    WAVES: (11, 12), STROKE: (3, 4), CLIMB: (5, 8), COMBO: (13, 33), INTENSE: (14, 2),
    RHYTHM: (15, None), RANDOM2: (32, None), TOGGLE: (18, None), ORGASM: (24, None),
    TORMENT: (28, None), PHASE3: (22, None),
}

AUDIO_GATE = {AUDIO1: 0x47, AUDIO2: 0x47, AUDIO3: 0x67}   # gate value with bit6 "is an audio mode"


def select_mode(vm: ET312VM, mode: int, *, split: tuple[int, int] = (STROKE, WAVES),
                user_start: dict[int, int] | None = None) -> None:
    """Function_0x604 (select new mode): reset the channel blocks, gate on, load the mode's blocks.

    `split` = (mode for A, mode for B) used when mode == SPLIT (factory default Stroke/Waves).
    `user_start` maps USERn -> start block index (EEPROM start vectors $8018..)."""
    m = vm.mem
    vm.reset_defaults()
    vm.load_block(1)                     # gate on for both channels (mask defaults to 3)
    if m[0x74]:                          # Random1 supervisor has chosen a sub-mode
        mode = m[0x74]
    if mode == SPLIT:
        m[0x83] |= 0x10
        passes = [(split[0], 1), (split[1], 2)]
    else:
        m[0x83] &= ~0x10 & 0xFF
        passes = [(mode, 3)]
    for md, mask in passes:
        m[0x85] = mask
        _dispatch(vm, md, mask, user_start or {})
    m[0x85] = 3
    vm.update_ma()                       # the blocks may have changed the MA range


def _dispatch(vm: ET312VM, mode: int, mask: int, user_start: dict[int, int]) -> None:
    m = vm.mem
    if mode in MODE_BLOCKS:
        first, second = MODE_BLOCKS[mode]
        vm.load_block(first)
        if second is not None and mask & 2:
            vm.load_block(second)
        return
    if mode in (AUDIO1, AUDIO2, AUDIO3):
        if mode == AUDIO1:
            m[0x83] = 0x40               # mono
        gv = AUDIO_GATE[mode]
        if mask & 1:
            m[0x90] = gv
        if mask & 2:
            m[0x190] = gv
        vm.load_block(23 if mode != AUDIO3 else 34)
        if mode == AUDIO3:
            m[0x83] = 0x04
        return
    if mode == RANDOM1:
        m[0x74] = 1                      # supervisor picks a mode on its next check
        return
    if mode in (PHASE1, PHASE2):
        m[0x83] = 0x05                   # phase control 1 + 2 (A/B pulses interleaved)
        vm.load_block(20)
        if mask & 2:
            vm.load_block(21)
        if mode != PHASE1:
            vm.load_block(35)
        return
    if USER1 <= mode <= USER7:
        start = user_start.get(mode)
        if start is None:
            raise KeyError(f"no user routine registered for mode 0x{mode:02x}")
        vm.load_block(start)
        return
    raise ValueError(f"unknown ET-312 mode 0x{mode:02x}")
