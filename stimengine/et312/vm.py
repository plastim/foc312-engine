"""ET-312B routine virtual machine: registers, program-block bytecode, modulators, gate, block timers.

This is a pure, deterministic re-implementation of the ErosTek ET-312B (firmware v1.6) *mode engine*,
derived from the public reverse-engineering work of the "buttshock" project:

  * https://github.com/teledildonics/buttshock-et312-firmware  (annotated disassembly
    `annotation/312-16-decrypted-combined.{hex,tags}`, hardware notes `doc/firmware.org`)
  * https://github.com/Orgasmscience/buttshock-protocol-docs   (`doc/et312-protocol.org`,
    `doc/protocol.txt`: the $4000.. memory map, register ranges, mode numbers)
  * ErosLink User Guide (bundled with `ErosLink_Installer.zip` in PlaStim's mk312 folder) for the
    human names of the same registers ("Rate: M.A.", "Frequency: Adv. Freq.", modules).
  * ET-312B User Guide v1.7 (mode descriptions, MA knob semantics per mode).

The annotated disassembly ships the flash *code* but blanks the mode data tables. The mode programs
themselves (0x2000-0x21c7 in the decrypted image) are ErosTek's and are NOT part of this source: fwdata.py
loads them at runtime from the user's own decrypted v1.6 image (the buttshock project's
`scripts/fw-utils.py` produces one), or from a JSON extracted from it. No firmware bytes are stored here.

What the firmware does (all offsets are RAM $4000+x; the VM mirrors that address map in `mem`):

  Tick base (verified 2026-09-26 against the annotated disassembly, buttshock-et312-firmware
  annotation/312-16-decrypted-combined): TCCR0 = 3 (clk/64, 0x1500) at 8 MHz (UBRRL = 25 for 19200 baud,
  0x1522) -> Timer0 overflows at 488.28 Hz; the ISR (0x0e5e) only sets r17 bit2.  Main (0x9c) counts the
  flags in $73 and runs the routine engine only when the count is odd (0xb0), so the ENGINE TICK is
  244.14 Hz (4.096 ms).  Each tick does BOTH channel pages: page A, then 0x29a sets r31 = 1 and 0x29c jumps
  back to 0xe2 for page B.  Per tick the 3-byte "routine timer" $88/$89/$8a increments; $8b increments every
  8th tick (30.5 Hz).  Modulator "select" bits 0-1 choose which counter a rate is compared against:
  1 = $88 (units of 4.1 ms), 2 = $8b (32.8 ms), 3 = $89 (1.049 s), 0 = no timer (static/follow mode).
  Every timed element (modulator 0x1d8, gate 0x14c, block timer 0xf8) stores last := counter + 1 when it
  fires, so a rate / time / period of N fires every N + 1 counter units (rate 0 every unit, rate 1 every
  2).  Before this was verified the VM stored last := counter and ran every timed element fast by
  (N + 1) / N: 2x at rate 1 (Orgasm's width sweep, Waves at MA fully CW).

  Per channel (A at $8c-$bf, B at $18c-$1bf) there are four 9-byte modulator blocks
  [value, min, max, rate, step, at_min, at_max, select, last] at $9c (mode-switch ramp),
  $a5 (intensity), $ae (frequency = pulse period), $b7 (pulse width), a gate block
  ($90 gate value, $98 on time, $99 off time, $9a select, $9b last) and a block timer
  ($94 last, $95 period, $96 select, $97 target block).

  Modulator step (every tick, per block):
    select&3 == 0: value := source, where select&0x0c is 0x04 = advanced parameter
      (RampLevel/Depth/Freq/Width for the four blocks), 0x08 = Multi-Adjust value, 0x0c = the
      other channel's value; 0 = leave alone.  bit4 (0x10) applies the per-block inversion
      value := (C - value) & 0xff with C = 0xcc/0xa4/0x09/0x40 (flash table at 0x1fec).
    select&3 != 0: rate := own rate | Adv second param (RampTime/Tempo/Effect/Pace, 0x20) |
      MA (0x40) | other channel's rate (0x60); bit7 inverts it.  When (timer - last) >= rate:
      last := timer + 1; min := source (same 0x0c selection as above, bit4 inversion);
      value += step (signed byte).  Hitting min/max clamps and runs the at_min/at_max action:
      0xfc = stop, 0xfd = wrap to the other end, 0xfe = toggle gate bits 1-2 (pulse shape)
      and reverse, 0xff = reverse direction, anything else = load that program block.
  Gate (0x10e-0x160, SBRC r26,0 at 0x118): select&3 timer; while ON (bit0 set) wait the on time $98
    (Effect if bit5, MA if bit6); while OFF wait the off time $99 (Tempo if bit2, MA if bit3; an off time
    of 0 turns the gate on again at once, so the output drops for one tick per on period); then toggle
    bit0, last := timer + 1.  (The VM had on and off swapped before 2026-09-26.)
  Block timer: when select&3 != 0 and (timer - last) >= period, last := timer + 1, load block `target`
    (channel B skips it if A has the same target).
  Program blocks: byte code executed once per channel pass selected by $85 (bit0 = A pass,
    bit1 = B pass; addresses $8c-$bf are redirected to the B page in the B pass):
      0x80|o v      : mem[$80+(o&0x3f)] = v   (bit6 of the opcode forces the B page)
      0x40|s|p  a   : s=0x00 ACC=mem[a], 0x04 mem[a]=ACC, 0x08 mem[a]>>=1, 0x0c mem[a]=random($8d..$8e)
      0x50|s|p  a v : s=0x00 +=, 0x04 &=, 0x08 |=, 0x0c ^=   (p = high address bits; page 2 = $20d = MA)
      0x20|n<<2|p a b..: n-byte block write
      0x70|p a      : if mem[a] == ACC: load block mem[$84]
      0x00-0x1f     : end of block

  Multi-Adjust: the pot is mapped linearly onto [mem[$87] (fully CCW) .. mem[$86] (fully CW)],
  each mode sets that range (protocol docs call $86 "high end" and $87 "low end").

Ambiguities are marked "AMBIGUOUS" in comments rather than guessed silently.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

TICK_HZ = 8_000_000 / 64 / 256 / 2          # 244.140625 Hz engine tick
TICK_S = 1.0 / TICK_HZ
SLOW_TICK_S = TICK_S * 8                     # select&3 == 2 units (32.8 ms)
SECOND_TICK_S = TICK_S * 256                 # select&3 == 3 units (1.049 s)

MOD_BLOCKS = (0x9C, 0xA5, 0xAE, 0xB7)        # ramp, intensity, frequency, width
MOD_NAMES = {0x9C: "ramp", 0xA5: "intensity", 0xAE: "frequency", 0xB7: "width"}
# flash table 0x1fec: value' = (b0 + b1 - value) & 0xff  (see module docstring)
INVERT_C = {0x9C: 0xCD + 0xFF, 0xA5: 0xA5 + 0xFF, 0xAE: 0x0F + 0xFA, 0xB7: 0x46 + 0xFA}
# advanced parameter pairs (first = value/min source, second = rate source) per block
ADV_FIRST = {0x9C: 0x1F8, 0xA5: 0x1FA, 0xAE: 0x1FC, 0xB7: 0x1FE}

# named registers (channel-relative; add 0x100 for channel B)
R = dict(
    ctrl_flags=0x83, next_block=0x84, chan_mask=0x85, ma_cw=0x86, ma_ccw=0x87,
    rt_lo=0x88, rt_mid=0x89, rt_hi=0x8A, rt_slow=0x8B, acc=0x8C, rnd_min=0x8D, rnd_max=0x8E,
    gate_val=0x90, blk_last=0x94, blk_period=0x95, blk_sel=0x96, blk_target=0x97,
    gate_on=0x98, gate_off=0x99, gate_sel=0x9A, gate_last=0x9B,
    ramp_val=0x9C, int_val=0xA5, int_min=0xA6, int_max=0xA7, int_rate=0xA8, int_step=0xA9,
    int_atmin=0xAA, int_atmax=0xAB, int_sel=0xAC,
    frq_val=0xAE, frq_min=0xAF, frq_max=0xB0, frq_rate=0xB1, frq_step=0xB2, frq_atmin=0xB3,
    frq_atmax=0xB4, frq_sel=0xB5,
    wid_val=0xB7, wid_min=0xB8, wid_max=0xB9, wid_rate=0xBA, wid_step=0xBB, wid_atmin=0xBC,
    wid_atmax=0xBD, wid_sel=0xBE,
    ma_value=0x20D,
    adv_ramp_level=0x1F8, adv_ramp_time=0x1F9, adv_depth=0x1FA, adv_tempo=0x1FB,
    adv_freq=0x1FC, adv_effect=0x1FD, adv_width=0x1FE, adv_pace=0x1FF,
    power_level=0x1F4, random1_mode=0x74, random1_next=0x75,
)

# Flash 0x1f9c..0x1fdb: the reset image of $80..$bf (channel B gets bytes 8.. at $188..$1bf).
DEFAULTS = bytes([
    0x00, 0x00, 0x02, 0x00, 0x00, 0x03, 0x0F, 0xFF,   # $80-$87  ctrl=0, mask=3, MA range CW=0x0f CCW=0xff
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x08, 0x00,   # $88-$8f  timers, acc, rnd 0..8
    0x06, 0x00, 0x00, 0x00, 0x00, 0xFF, 0x00, 0x00,   # $90-$97  gate_val=6 (biphasic, off), blk period ff sel 0
    0x3E, 0x3E, 0x00, 0x00,                           # $98-$9b  gate on/off 62, gate sel 0 (no gating)
    0x9C, 0x9C, 0xFF, 0x07, 0x01, 0xFC, 0xFC, 0x01, 0x00,   # $9c ramp: 156->255 step 1 every 7 ticks
    0xFF, 0xCD, 0xFF, 0x01, 0x01, 0xFF, 0xFF, 0x00, 0x00,   # $a5 intensity: 255, 205..255, static
    0x16, 0x09, 0x64, 0x01, 0x01, 0xFF, 0xFF, 0x08, 0x00,   # $ae frequency: 22, 9..100, follows MA
    0x82, 0x32, 0xC8, 0x01, 0x01, 0xFF, 0xFF, 0x04, 0x00,   # $b7 width: 130, 50..200, follows Adv Width
])
assert len(DEFAULTS) == 0x40

# Flash 0x1fdc..0x1feb: reset image of $1f0..$1ff (EEPROM normally overrides $1f3..).
ADV_DEFAULTS = bytes([0xC0, 0x2C, 0x20, 0x87, 0x02, 0x77, 0x76, 0x76,
                      0xE1, 0x14, 0xD7, 0x01, 0x19, 0x05, 0x82, 0x05])

Op = tuple


def decode_module(code: bytes) -> list[tuple]:
    """The firmware's program-block bytecode (built-in modes and ErosLink modules alike) -> VM op tuples.
    Stops at the first byte < 0x20 (end of block)."""
    ops: list[tuple] = []
    i = 0
    n = len(code)
    while i < n:
        b = code[i]
        if b < 0x20:
            break                                           # end of module
        if b >= 0x80:
            if i + 1 >= n:
                break
            ops.append(("set", 1 if b & 0x40 else 0, 0x80 | (b & 0x3F), code[i + 1]))
            i += 2
        elif b < 0x40:
            cnt = (b >> 2) & 7
            if i + 1 + cnt >= n:
                break
            ops.append(("blk", b & 3, code[i + 1], bytes(code[i + 2:i + 2 + cnt])))
            i += 2 + cnt
        elif b < 0x50:
            if i + 1 >= n:
                break
            kind = ("ldacc", "stacc", "shr", "rand")[(b >> 2) & 3]
            ops.append((kind, b & 3, code[i + 1]))
            i += 2
        elif b < 0x60:
            if i + 2 >= n:
                break
            kind = ("add", "and", "or", "xor")[(b >> 2) & 3]
            ops.append((kind, b & 3, code[i + 1], code[i + 2]))
            i += 3
        elif b >= 0x70:
            if i + 1 >= n:
                break
            ops.append(("ifacc", b & 3, code[i + 1]))
            i += 2
        else:
            raise ValueError(f"unknown ET-312 opcode {b:#04x} at {i}")
    return ops



def encode_ops(ops: Iterable[tuple]) -> bytes:
    """The inverse of decode_module: VM op tuples -> the firmware's bytecode, ending in 0x00. Used to hand the
    built-in blocks (loaded as op tuples by fwdata) to a bytecode engine such as the M5 remote's C core."""
    out = bytearray()
    for op in ops:
        kind = op[0]
        if kind == "set":
            _, page, addr, val = op
            if not 0x80 <= addr <= 0xBF or page not in (0, 1):
                raise ValueError(f"set op {op!r} has no single-byte form")
            out += bytes([0x80 | (0x40 if page else 0) | (addr & 0x3F), val & 0xFF])
        elif kind == "blk":
            _, page, addr, data = op
            if len(data) > 7:
                raise ValueError("block writes carry at most 7 bytes")
            out += bytes([0x20 | (len(data) << 2) | (page & 3), addr & 0xFF]) + bytes(data)
        elif kind in ("ldacc", "stacc", "shr", "rand"):
            out += bytes([0x40 | (("ldacc", "stacc", "shr", "rand").index(kind) << 2) | (op[1] & 3), op[2] & 0xFF])
        elif kind in ("add", "and", "or", "xor"):
            out += bytes([0x50 | (("add", "and", "or", "xor").index(kind) << 2) | (op[1] & 3), op[2] & 0xFF,
                          op[3] & 0xFF])
        elif kind == "ifacc":
            out += bytes([0x70 | (op[1] & 3), op[2] & 0xFF])
        elif kind == "nop":
            continue
        else:
            raise ValueError(f"unknown ET-312 op {op!r}")
    return bytes(out) + bytes([0])

@dataclass
class VMTrace:
    """Optional log of block loads for tests/debugging."""
    loads: list[tuple[int, int]] = field(default_factory=list)   # (tick, block)


class ET312VM:
    """Register file + bytecode + modulation engine.  Knobs are plain attributes.

    `blocks` maps block index -> op list (see modes.py).  User routines (ErosLink / EEPROM) use
    the same bytecode; register them under indices >= 0x80.
    """

    def __init__(self, blocks: dict[int, Sequence[Op]], *, seed: int = 0,
                 rng: random.Random | None = None) -> None:
        self.mem = bytearray(0x300)
        self.blocks = dict(blocks)
        self.rng = rng or random.Random(seed)
        self.ma_knob = 0.5            # 0 = fully CCW, 1 = fully CW
        self.ma_override_r2: int | None = None   # Random1 replaces the pot reading (see engine)
        self.tick_count = 0
        self.pending_next = False
        self.trace = VMTrace()
        self.reset_defaults()

    # ------------------------------------------------------------------ setup
    def reset_defaults(self) -> None:
        """CallTable_24: copy the flash image over $80..$bf and $188..$1bf, $1f0..$1ff."""
        self.mem[0x80:0xC0] = DEFAULTS
        self.mem[0x188:0x1C0] = DEFAULTS[8:]
        # $1f0.. is only reset at power-on in the firmware; we reset it once at construction
        if not any(self.mem[0x1F0:0x200]):
            self.mem[0x1F0:0x200] = ADV_DEFAULTS
        self.pending_next = False
        self.update_ma()

    def set_advanced(self, **kw: int) -> None:
        """Advanced-menu parameters by name: ramp_level, ramp_time, depth, tempo, freq, effect, width, pace."""
        for k, v in kw.items():
            self.mem[R["adv_" + k]] = int(v) & 0xFF

    # ------------------------------------------------------------- bytecode
    def _resolve(self, page: int, addr: int, t: int) -> int:
        full = ((page & 3) << 8) | (addr & 0xFF)
        if 0x8C <= full < 0xC0 and t:
            full += 0x100
        return full

    def load_block(self, idx: int) -> None:
        """CallTable_22: run every op of a block (each op under the current $85 channel mask)."""
        ops = self.blocks.get(idx)
        if ops is None:
            raise KeyError(f"ET-312 program block {idx} is not defined")
        self.trace.loads.append((self.tick_count, idx))
        for op in ops:
            self._exec(op)

    def _exec(self, op: Op) -> None:
        """CallTable_30: pass for channel A if mask bit0, then channel B if mask bit1."""
        mask = self.mem[0x85]
        if mask == 0:
            return
        t = 0 if mask & 1 else 1
        while True:
            self._exec_pass(op, t)
            if t == 1 or not (self.mem[0x85] & 2):
                break
            t = 1

    def _exec_pass(self, op: Op, t: int) -> None:
        m = self.mem
        kind = op[0]
        if kind == "set":
            _, page, addr, val = op
            m[self._resolve(page, addr, t)] = val & 0xFF
        elif kind == "ldacc":
            m[0x8C + (t << 8)] = m[self._resolve(op[1], op[2], t)]
        elif kind == "stacc":
            m[self._resolve(op[1], op[2], t)] = m[0x8C + (t << 8)]
        elif kind == "shr":
            a = self._resolve(op[1], op[2], t)
            m[a] >>= 1
        elif kind == "rand":
            m[self._resolve(op[1], op[2], t)] = self.random_between()
        elif kind in ("add", "and", "or", "xor"):
            a = self._resolve(op[1], op[2], t)
            v = op[3] & 0xFF
            if kind == "add":
                m[a] = (m[a] + v) & 0xFF
            elif kind == "and":
                m[a] &= v
            elif kind == "or":
                m[a] |= v
            else:
                m[a] ^= v
        elif kind == "blk":
            a = self._resolve(op[1], op[2], t)
            data = bytes(op[3])
            m[a:a + len(data)] = data
        elif kind == "ifacc":
            if m[self._resolve(op[1], op[2], t)] == m[0x8C + (t << 8)]:
                self.pending_next = True
        elif kind == "nop":
            pass
        else:
            raise ValueError(f"unknown ET-312 op {op!r}")

    def random_between(self) -> int:
        """r26_is_random_between_mem0x8D_and_mem0x8E: uniform integer in [$8d, $8e] (inclusive).
        The firmware's PRNG mixes ADC noise; we use a seeded random.Random for determinism."""
        lo, hi = self.mem[0x8D], self.mem[0x8E]
        if hi < lo:               # firmware: error handler ("Failure 15")
            raise ValueError(f"random range {lo}..{hi} is inverted")
        return self.rng.randint(lo, hi)

    # ------------------------------------------------------------- knobs
    def update_ma(self) -> None:
        """Function_0x15fc/0x1650: MA value = mem[$86] + r2 * (mem[$87]-mem[$86]) / 255, r2 = 255 at fully CCW.

        The pot's bias voltage is averaged, x3 and clipped, then complemented -> r2; a knob fraction
        f (1 = CW) gives r2 = 255*(1-f).  Integer math kept (128x scaling, 32640 = 255*128)."""
        m = self.mem
        hi, lo = m[0x86], m[0x87]
        r2 = self.ma_override_r2 if self.ma_override_r2 is not None else int(round(255 * (1.0 - self.ma_knob)))
        r2 = max(0, min(255, r2))
        rng = (lo - hi) & 0xFF
        if rng == 0:
            # AMBIGUOUS: firmware divides by zero here -> "Failure 15" halt. No built-in mode does it.
            v = hi
        else:
            q = 32640 // rng
            v = (r2 * 128) // q + hi
            v = min(255, v)
        m[0x20D] = v

    @property
    def ma_value(self) -> int:
        return self.mem[0x20D]

    # ------------------------------------------------------------- timers
    def _timer(self, tsel: int) -> tuple[bool, int]:
        """set_routine_timer: (this tick counts for this timer select, current counter value)."""
        m = self.mem
        tsel &= 3
        if tsel == 1:
            return True, m[0x88]
        if tsel == 2:
            return (m[0x88] & 7) == 7, m[0x8B]
        if tsel == 3:
            return m[0x88] == 0, m[0x89]
        return False, 0

    def tick(self) -> None:
        """One 244.14 Hz engine tick (the even half of the Timer0 overflow handler + Main)."""
        m = self.mem
        self.tick_count += 1
        m[0x88] = (m[0x88] + 1) & 0xFF
        if m[0x88] == 0:
            m[0x89] = (m[0x89] + 1) & 0xFF
            if m[0x89] == 0:
                m[0x8A] = (m[0x8A] + 1) & 0xFF
        if (m[0x88] & 7) == 7:
            m[0x8B] = (m[0x8B] + 1) & 0xFF
        for page in (0, 1):
            self._block_timer(page)
            self._gate(page)
            for z in MOD_BLOCKS:
                self._modulate(page, z)
        if self.pending_next:              # r15 bit1: conditional branch requested by an IF op
            self.pending_next = False
            self.load_block(m[0x84])
        self.update_ma()

    # ------------------------------------------------------------- per-page engines
    def _block_timer(self, page: int) -> None:
        m = self.mem
        base = page << 8
        sel = m[base + 0x96]
        if sel & 3 == 0:
            return
        ok, now = self._timer(sel)
        if not ok:
            return
        if (now - m[base + 0x94]) & 0xFF < m[base + 0x95]:
            return
        m[base + 0x94] = (now + 1) & 0xFF   # firmware 0xf8: r0++ before the store -> period = N + 1 units
        target = m[base + 0x97]
        if page == 1 and target == m[0x97]:
            return                      # channel A already loaded the same block
        self.load_block(target)

    def _gate(self, page: int) -> None:
        m = self.mem
        base = page << 8
        sel = m[base + 0x9A]
        if sel & 3 == 0:
            return
        gv = m[base + 0x90]
        # firmware 0x116-0x13a (SBRC r26,0): while OFF (bit0 clear) wait the off time $99 (Tempo if bit2, MA if
        # bit3), and an off time of 0 turns the gate straight back on; while ON wait the on time $98 (Effect if
        # bit5, MA if bit6)
        if not gv & 1:
            t = m[base + 0x99]
            if sel & 0x04:
                t = m[0x1FB]            # Advanced Tempo
            if sel & 0x08:
                t = m[0x20D]            # MA
            if t == 0:
                m[base + 0x90] = gv | 1  # off time 0: on again at once (last is not updated)
                return
        else:
            t = m[base + 0x98]
            if sel & 0x20:
                t = m[0x1FD]            # Advanced Effect
            if sel & 0x40:
                t = m[0x20D]            # MA
        ok, now = self._timer(sel)
        if not ok:
            return
        if (now - m[base + 0x9B]) & 0xFF < t:
            return
        m[base + 0x9B] = (now + 1) & 0xFF   # firmware 0x14c: r0++ before the store -> period = t + 1 units
        m[base + 0x90] = gv ^ 1

    def _source(self, page: int, z: int, sel: int, offset: int) -> int | None:
        """Value/min source selected by select bits 2-3 (offset 0 = value, 1 = min of the other channel)."""
        m = self.mem
        src = sel & 0x0C
        if src == 0x0C:
            v = m[((1 - page) << 8) + z + offset]
        elif src == 0x08:
            v = m[0x20D]
        elif src == 0x04:
            v = m[ADV_FIRST[z]]
        else:
            return None
        if sel & 0x10:
            v = (INVERT_C[z] - v) & 0xFF
        return v

    def _modulate(self, page: int, z: int) -> None:
        m = self.mem
        base = page << 8
        bz = base + z
        sel = m[bz + 7]
        tsel = sel & 3
        if tsel == 0:
            v = self._source(page, z, sel, 0)
            if v is not None:
                m[bz] = v
            return
        rs = sel & 0x60
        if rs == 0x60:
            rate = m[((1 - page) << 8) + z + 3]
        elif rs == 0x40:
            rate = m[0x20D]
        elif rs == 0x20:
            rate = m[ADV_FIRST[z] + 1]
        else:
            rate = m[bz + 3]
        if sel & 0x80:
            rate = (INVERT_C[z] - rate) & 0xFF
        ok, now = self._timer(tsel)
        if not ok:
            return
        if (now - m[bz + 8]) & 0xFF < rate:
            return
        m[bz + 8] = (now + 1) & 0xFF        # firmware 0x1d8: r0++ before the store -> one step per rate + 1 units
        mn = self._source(page, z, sel, 1)
        if mn is None:
            mn = m[bz + 1]
        m[bz + 1] = mn
        val, step, mx = m[bz], m[bz + 4], m[bz + 2]
        new = val + step
        if step >= 0x80:                       # negative step
            if new < 0x100:                    # borrowed below zero
                self._at_min(page, z)
                return
            new &= 0xFF
            if new < mn:
                self._at_min(page, z)
                return
            if new >= mx:
                self._at_max(page, z)
                return
        else:
            if new >= 0x100 or new >= mx:
                self._at_max(page, z)
                return
        m[bz] = new

    def _at_min(self, page: int, z: int) -> None:
        m = self.mem
        bz = (page << 8) + z
        m[bz] = m[bz + 1]
        a = m[bz + 5]
        if a == 0xFC:
            return
        if a == 0xFD:
            m[bz] = m[bz + 2]
            return
        self._action(page, z, a)

    def _at_max(self, page: int, z: int) -> None:
        m = self.mem
        bz = (page << 8) + z
        m[bz] = m[bz + 2]
        a = m[bz + 6]
        if a == 0xFC:
            return
        if a == 0xFD:
            m[bz] = m[bz + 1]
            return
        self._action(page, z, a)

    def _action(self, page: int, z: int, a: int) -> None:
        m = self.mem
        bz = (page << 8) + z
        if a == 0xFE:
            m[(page << 8) + 0x90] ^= 6         # flip pulse-shape bits (mono <-> biphasic)
            a = 0xFF
        if a == 0xFF:
            m[bz + 4] = (-m[bz + 4]) & 0xFF    # reverse direction
            return
        if page == 1 and (a == m[z + 5] or a == m[z + 6]):
            return                             # A's block already loaded this one
        self.load_block(a)

    # ------------------------------------------------------------- helpers for the engine
    def reg(self, name: str, channel: int = 0) -> int:
        a = R[name]
        if channel and a < 0x100:
            a += 0x100
        return self.mem[a]

    def set_reg(self, name: str, value: int, channel: int = 0) -> None:
        a = R[name]
        if channel and a < 0x100:
            a += 0x100
        self.mem[a] = value & 0xFF

    def modulator(self, channel: int, z: int) -> dict[str, int]:
        b = (channel << 8) + z
        keys = ("value", "min", "max", "rate", "step", "at_min", "at_max", "select", "last")
        return {k: self.mem[b + i] for i, k in enumerate(keys)}
