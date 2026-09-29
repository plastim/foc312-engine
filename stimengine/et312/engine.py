"""ET312Engine: knobs + mode -> per-tick, per-channel pulse parameters.  Pure and deterministic.

Output model (per channel, per 4.096 ms tick):
  intensity       0..1 amplitude fraction (level knob x mode-switch ramp x intensity modulation)
  pulse_rate_hz   pulses per second
  pulse_width_us  width of ONE phase of the biphasic pulse (the box's "width" register, microseconds)
  gate_on         output enabled (gate bit0; Mute control flag clears it)
  leading_polarity +1/-1: which half of the output transformer primary carries the (first) phase.
                  +1 = the "first" half (q5 on channel A, q7 on B), -1 = the other (q6 / q8).
  biphasic        both halves, back to back; False = one half only (monophasic) or alternating
  alternating     gate bit3: one half per pulse, the half alternating pulse to pulse
  phase_asymmetry "et312_biphasic" | "et312_monophasic" | "et312_alternating"
                  The manual defines the width value as "the width of each half of the bipolar pulse in
                  microseconds"; a monophasic pulse's return swing is the transformer's own flyback.

Gate register $4090 / $4190, pulse bits (verified in the disassembly of 312-16, teledildonics/
buttshock-et312-firmware annotation/312-16-decrypted-combined.hex, 2026-09-26):
  Main at 0x2a2-0x2c6 (B: 0x2ca-0x2f2) copies the gate's bits 1-2 into r16 bits 0-1 (r16 = (gate & 6) >> 1)
  whenever they change and all four FETs are off; if bit3 is set it loads r16 = 01 instead.
  Timer1_CMP_A (0xe92) and its pulse-start path (0xf34-0xf70), channel A:
    r16 bit1 (= gate bit2) set -> the pulse starts on the "first" half (0xf44: q5, or q6 if gate bit4), and
      when that phase ends (0xeaa) r16 bit0 (= gate bit1) decides whether the other half follows at once
      (0xf64: biphasic) or the pulse ends (0xec6: monophasic on the first half);
    else r16 bit0 (= gate bit1) set -> a single phase on the OTHER half (0xf54 -> 0xf64): monophasic,
      opposite polarity;
    neither -> no pulse.  Gate bit3 toggles r16 bits 0-1 after every pulse (0xec6-0xecc): alternating halves.
  So bits 2,1 = 11 biphasic, 10 monophasic "first" half, 01 monophasic "other" half, 00 silent; bit4 swaps
  which half is "first" (0xe94, 0xf46).  Stroke's gate 0x05 <-> 0x03 (at_min/at_max action 0xfe toggles bits
  1-2) therefore alternates the POLARITY of a monophasic pulse at every ramp reversal; it is never biphasic.
  (Before 2026-09-26 the emulator read bit1 alone as "biphasic" and played Stroke as mono <-> bi.)
  Channel B is the same with Timer1_CMP_B (0xf8a), q7/q8, and r16 bits 4-5.

Pulse timing (Timer1 at 1 MHz, same handlers):
  period = 256*(F + 1 + [$204]) + phases*W us   (F = max(9, frequency reg), W = max(50, width reg),
  phases = 2 biphasic, 1 otherwise).  Every pulse, mono or bi, ends in the 0xece gap of 256*(1 + [$204]) us,
  and the two idle steps add [$209] + [$20b] = 256*F (0x1ace-0x1ae2).  [$204] is written once at startup
  (0x57c) and assumed 0.  So F=9, W=50 gives ~376 Hz biphasic and F=255 ~15 Hz; the manual's "15-330 Hz"
  agrees at the low end.  (Before 2026-09-26 the biphasic period omitted the 256 us gap.)

Amplitude (Function_0x15b8 + the SPI/DAC writer):
  code = (pot/(5-power_level) + offset) * ramp/256 * intensity/256, offset 55 (A) / 43 (B)
  and the DAC gets ~code.  The +offset with the pot at zero suggests the FET drive starts conducting
  around code == offset, so `level_model="deadzone"` (default) reports
  amplitude = clip((code - offset) / (255/(5-power))), which makes the intensity modulation depth
  depend on the knob position as it does on the box.  `level_model="linear"` reports
  knob * ramp/255 * intensity/255 instead.  AMBIGUOUS: the FET transfer curve is not documented.

Random1 (Function at 0x474): every 0.524 s check; when due, pick a mode in Waves..Rhythm and a
  duration of 20..120 units (10.5..63 s), re-run mode select (which restarts the 156->255 mode
  ramp), and replace the MA pot reading with a random raw value 140..184.  AMBIGUOUS: as decoded,
  that raw value is x3 and clipped to 255 before complementing, so MA is pinned to the mode's
  fully-CW value; the manual says "MA value set to a random value".  Default follows the code
  (`random1_ma="firmware"`); `random1_ma="uniform"` follows the manual.

Sources: see vm.py.  Audio modes are stubbed (audio level would replace the intensity value).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterator

from . import fwdata
from . import modes as M
from .vm import ET312VM, TICK_HZ, TICK_S, R

POWER_LEVELS = {"low": 0, "normal": 1, "high": 2}
LEVEL_OFFSET = {0: 55, 1: 43}                     # RAM $6b / $6c ("some power offset", protocol.txt)
MASTER_MSB_TICKS = 128                            # $406a increments every 256 Timer0 overflows = 128 ticks


@dataclass(frozen=True)
class AdvancedParams:
    """The Advanced menu (flash defaults; the display shows them scaled 0-99)."""
    ramp_level: int = 0xE1
    ramp_time: int = 0x14
    depth: int = 0xD7
    tempo: int = 0x01
    freq: int = 0x19
    effect: int = 0x05
    width: int = 0x82
    pace: int = 0x05


@dataclass(frozen=True)
class ChannelOutput:
    gate_on: bool
    intensity: float
    pulse_rate_hz: float
    pulse_width_us: float
    biphasic: bool
    leading_polarity: int
    phase_asymmetry: str
    raw_gate: int
    raw_ramp: int
    raw_intensity: int
    raw_frequency: int
    raw_width: int
    alternating: bool = False

    @property
    def effective(self) -> float:
        """intensity if the gate is open, else 0."""
        return self.intensity if self.gate_on else 0.0


@dataclass(frozen=True)
class ET312Frame:
    t: float
    tick: int
    mode: int
    mode_name: str
    a: ChannelOutput
    b: ChannelOutput
    phase_mode: str           # "none" | "interleaved" (Phase 1/2) | "linked" (Phase 3: B mirrors A)
    ma_value: int
    ctrl_flags: int


class BuiltinModesUnavailable(ValueError):
    """A built-in mode was asked for, but no ET-312 firmware data is loaded (fwdata.py)."""


class ET312Engine:
    def __init__(self, mode: int | str = "waves", *, level_a: float = 0.5, level_b: float = 0.5,
                 ma: float = 0.5, power: str = "normal", advanced: AdvancedParams | None = None,
                 split: tuple[int | str, int | str] = (M.STROKE, M.WAVES), seed: int = 0,
                 level_model: str = "deadzone", random1_ma: str = "firmware",
                 user_blocks: dict[int, list] | None = None,
                 user_start: dict[int, int] | None = None,
                 firmware: fwdata.FirmwareData | None = None,
                 skip_mode_ramp: bool = False) -> None:
        if level_model not in ("deadzone", "linear"):
            raise ValueError("level_model must be 'deadzone' or 'linear'")
        if random1_ma not in ("firmware", "uniform"):
            raise ValueError("random1_ma must be 'firmware' or 'uniform'")
        # the built-in modes' blocks come from the user's own firmware data (fwdata.py); without it only ErosLink /
        # our own routines can play, and a built-in mode request raises BuiltinModesUnavailable
        fw = firmware if firmware is not None else fwdata.default()
        self.builtins_available = fw is not None
        self.firmware_source = fw.source if fw is not None else None
        blocks = dict(M.CORE_BLOCKS)
        if fw is not None:
            blocks.update(fw.blocks)
        if user_blocks:
            for k in user_blocks:
                if k < 0x80:
                    raise ValueError("user blocks must use indices >= 0x80")
            blocks.update(user_blocks)
        self.vm = ET312VM(blocks, seed=seed)
        self.level_model = level_model
        # False (default) = the box: a mode change restarts the level ramp (156 -> 255, one step per 8 ticks,
        # about 3.2 s). True starts every mode at full ramp, for comparing patterns without the ramp in the way.
        self.skip_mode_ramp = bool(skip_mode_ramp)
        self.random1_ma = random1_ma
        self.user_start = {self._mode_num(k): v for k, v in (user_start or {}).items()}
        self.split = (self._mode_num(split[0]), self._mode_num(split[1]))
        self.power = POWER_LEVELS[power] if isinstance(power, str) else int(power)
        self.level_a = float(level_a)
        self.level_b = float(level_b)
        self.advanced = advanced or AdvancedParams()
        self.vm.set_advanced(**self.advanced.__dict__)
        self.vm.mem[R["power_level"]] = self.power
        self.set_ma(ma)
        self.mode = 0
        self.routine = None                   # the loaded ErosLink routine while it plays as User1
        self._master_msb = 0
        if mode is not None and not (hasattr(mode, "modules") and hasattr(mode, "start")):
            self._mode_num(mode)              # an unknown name is an error whether or not the data is loaded
        if mode is None or (not self.builtins_available and self._is_builtin(mode)):
            self._silent()                    # no pattern yet (or no built-in data): gates off, nothing plays
        else:
            self.set_mode(mode)

    def _silent(self) -> None:
        self.mode = 0
        self.routine = None
        self.vm.reset_defaults()
        self.vm.load_block(0)

    @staticmethod
    def _is_builtin(mode) -> bool:
        if hasattr(mode, "modules") and hasattr(mode, "start"):
            return False
        try:
            num = ET312Engine._mode_num(mode)
        except ValueError:
            return True
        return not M.USER1 <= num <= M.USER7

    # ------------------------------------------------------------- knobs / mode
    @staticmethod
    def _mode_num(mode: int | str) -> int:
        if isinstance(mode, str):
            try:
                return M.MODE_BY_NAME[mode.lower()]
            except KeyError:
                raise ValueError(f"unknown ET-312 mode name {mode!r}") from None
        return int(mode)

    def set_mode(self, mode) -> None:
        """A built-in mode (number or name), or a compiled ErosLink routine (elk.load()): its modules
        are installed as the box's user program blocks and it runs as User1, as ErosLink's upload did."""
        if hasattr(mode, "modules") and hasattr(mode, "start"):
            self._install_routine(mode)
            mode = M.USER1
        elif not self.builtins_available and self._is_builtin(mode):
            raise BuiltinModesUnavailable(
                "the ET-312 built-in modes need your own firmware data (see stimengine/et312/fwdata.py)")
        elif not (isinstance(mode, int) and M.USER1 <= mode <= M.USER7):
            self.routine = None
        self.mode = self._mode_num(mode)
        self.vm.mem[0x74] = 0
        self.vm.ma_override_r2 = None
        M.select_mode(self.vm, self.mode, split=self.split, user_start=self.user_start)
        self._after_select()
        if self.mode == M.RANDOM1:
            self._random1_pick()

    def _after_select(self) -> None:
        if self.skip_mode_ramp:
            m = self.vm.mem
            for base in (0, 0x100):
                m[base + 0x9C] = m[base + 0x9E]      # ramp value := ramp max (it holds there: at_max = stop)

    def _install_routine(self, routine) -> None:
        blocks = self.vm.blocks
        for k in [k for k in blocks if k >= 0x80]:
            del blocks[k]
        from .vm import decode_module
        for num, code in routine.modules.items():
            blocks[num] = decode_module(code)
        self.user_start[M.USER1] = routine.start
        self.routine = routine

    def set_levels(self, a: float | None = None, b: float | None = None) -> None:
        if a is not None:
            self.level_a = min(1.0, max(0.0, float(a)))
        if b is not None:
            self.level_b = min(1.0, max(0.0, float(b)))

    def set_ma(self, fraction: float) -> None:
        """Multi-Adjust knob, 0 = fully counter-clockwise, 1 = fully clockwise."""
        self.vm.ma_knob = min(1.0, max(0.0, float(fraction)))
        self.vm.update_ma()

    def set_advanced(self, **kw: int) -> None:
        self.advanced = replace(self.advanced, **kw)
        self.vm.set_advanced(**kw)

    def start_ramp(self) -> None:
        """Front-panel "Start Ramp Up" (CallTable_33): the mode-switch ramp block restarts from
        Adv RampLevel, stepping once per Adv RampTime seconds (timer select 3, source RampLevel)."""
        m = self.vm.mem
        for base in (0, 0x100):
            m[base + 0xA4] = 0
            m[base + 0xA3] = 0x27
            m[base + 0x9C] = m[R["adv_ramp_level"]]

    # ------------------------------------------------------------- stepping
    def step(self) -> ET312Frame:
        self.vm.tick()
        if self.vm.tick_count % MASTER_MSB_TICKS == 0:
            self._master_msb = (self._master_msb + 1) & 0xFF
            if self.mode == M.RANDOM1 and self._master_msb == self.vm.mem[0x75]:
                self._random1_pick()
        return self.frame()

    def run(self, seconds: float) -> Iterator[ET312Frame]:
        for _ in range(int(round(seconds * TICK_HZ))):
            yield self.step()

    def _random1_pick(self) -> None:
        vm, m = self.vm, self.vm.mem
        m[0x8D], m[0x8E] = 0x76, 0x7B
        m[0x74] = vm.random_between()
        m[0x8D], m[0x8E] = 20, 120
        m[0x75] = (self._master_msb + vm.random_between()) & 0xFF
        m[0x8D], m[0x8E] = 140, 184
        raw = vm.random_between()
        if self.random1_ma == "firmware":
            vm.ma_override_r2 = 255 - min(255, 3 * raw)        # == 0 for every raw in 140..184
        else:
            vm.ma_override_r2 = vm.rng.randint(0, 255)
        M.select_mode(vm, M.RANDOM1, split=self.split, user_start=self.user_start)
        self._after_select()

    # ------------------------------------------------------------- outputs
    def frame(self) -> ET312Frame:
        vm, m = self.vm, self.vm.mem
        ctrl = m[0x83]
        a = self._channel(0, self.level_a, ctrl)
        b = self._channel(1, self.level_b, ctrl)
        phase_mode = "none"
        if ctrl & 0x08:
            # Phase 3: channel B's own timer never fires; A's handler drives B's transistors too.
            phase_mode = "linked"
            b = replace(b, gate_on=a.gate_on, pulse_rate_hz=a.pulse_rate_hz,
                        pulse_width_us=a.pulse_width_us, biphasic=a.biphasic,
                        phase_asymmetry=a.phase_asymmetry)
        elif ctrl & 0x04:
            phase_mode = "interleaved"
        mode = m[0x74] if self.mode == M.RANDOM1 and m[0x74] > 1 else self.mode
        return ET312Frame(
            t=vm.tick_count * TICK_S, tick=vm.tick_count, mode=mode,
            mode_name=(self.routine.name if self.routine is not None and mode == M.USER1
                       else M.MODE_NAMES.get(mode, f"0x{mode:02x}")), a=a, b=b,
            phase_mode=phase_mode, ma_value=vm.ma_value, ctrl_flags=ctrl)

    def _channel(self, ch: int, knob: float, ctrl: int) -> ChannelOutput:
        m = self.vm.mem
        base = ch << 8
        gv = m[base + 0x90]
        ramp = m[base + 0x9C]
        inten = m[base + 0xA5]
        f = max(9, m[base + 0xAE])          # CallTable_29 clamps
        w = max(50, m[base + 0xB7])
        pol, biphasic, alternating, pulses = gate_pulse(gv)
        period_us = 256 * (f + 1) + (2 if biphasic else 1) * w
        amp = self._amplitude(ch, knob, ramp, inten)
        gate_on = bool(gv & 1) and not (ctrl & 0x02) and pulses
        return ChannelOutput(
            gate_on=gate_on, intensity=amp, pulse_rate_hz=1e6 / period_us, pulse_width_us=float(w),
            biphasic=biphasic, leading_polarity=pol, alternating=alternating,
            phase_asymmetry=("et312_biphasic" if biphasic else "et312_alternating" if alternating
                             else "et312_monophasic"),
            raw_gate=gv, raw_ramp=ramp, raw_intensity=inten, raw_frequency=m[base + 0xAE],
            raw_width=m[base + 0xB7])

    def _amplitude(self, ch: int, knob: float, ramp: int, inten: int) -> float:
        scale = (ramp / 255.0) * (inten / 255.0)
        if self.level_model == "linear":
            return _clip01(knob * scale)
        k = 255.0 / (5 - self.power)
        off = LEVEL_OFFSET[ch]
        code = (knob * k + off) * scale
        return _clip01((code - off) / k)


def gate_pulse(gv: int) -> tuple[int, bool, bool, bool]:
    """Gate value -> (leading polarity, biphasic, alternating, any pulse). See the module docstring for the
    disassembly trace. Polarity +1 = the "first" half (q5 / q7), -1 = the other; bit4 swaps them."""
    first = -1 if gv & 0x10 else 1
    if gv & 0x08:                         # alternating halves; after a reload the first pulse is on the other half
        return -first, False, True, True
    halves = (gv >> 1) & 3                # bit1 = gate bit2 ("first" half), bit0 = gate bit1 (the other half)
    if halves == 3:
        return first, True, False, True
    if halves == 2:
        return first, False, False, True
    if halves == 1:
        return -first, False, False, True
    return first, False, False, False


def _clip01(v: float) -> float:
    return float(min(1.0, max(0.0, v)))


__all__ = ["AdvancedParams", "ChannelOutput", "ET312Frame", "ET312Engine", "TICK_HZ", "TICK_S"]
