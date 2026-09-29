"""ErosLink .elk routine importer: file -> ingredients -> triggers -> ET-312 program modules -> VM blocks.

ErosLink (ErosTek, 2003) never interprets a routine itself.  It *compiles* each routine into ET-312
"program modules" - the same bytecode the box's built-in modes are made of - and uploads them into
the box's user-mode memory; the box then runs them as User1..User7.  Our VM (vm.py) executes that
bytecode, so importing a routine means doing what ErosLink did, in three stages:

  1. decode   .elk = Java serialization of an ErosLink "Context" holding Routine objects, each a
              list of Ingredients (Channel, Ramp, Multi-A Ramp, Multi-A, Multi-A Gate, Gate, Time
              Goto, Set Value, Raw, External Trigger, Gate From).  javaser.py reads the stream;
              the per-class readers below mirror each class's readObject().
  2. lower    every ingredient's genLowLevel(): one "Trigger" (a module description: which
              registers of which channel to change) plus "ParameterSets" (one per modulator it
              ramps).  Names are routine-name-prefixed; triggers chain ("and also do") and
              reference each other at ramp ends ("and then").
  3. compile  ET312.Module / TriggerModule / ModuleSet: triggers -> (address, value) pairs ->
              sorted, de-duplicated, encoded as bytecode; module references resolved to module
              numbers (EEPROM vectors 0x80.. / 0xa0..), exactly as "Store in box" would.

Every rule here was ported from ErosLink's own classes and is checked byte-for-byte against
ErosLink itself (its jar run on an emulated box, see ELK-FORMAT.md and tests/test_et312_elk.py):
all 245 routines in the installer and in PlaStim's collection compile to identical module bytes.
Quirks are kept on purpose (they are what the box ran), and marked "ErosLink quirk".

Bundled routines (the ones on the ErosLink CD) are read at runtime from a local cache outside the
repo; `eroslink_cache.py` rebuilds it from ErosLink_Installer.zip.  No ErosLink code or routine
data is stored in this repository.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

from . import javaser
from .javaser import JavaObject
from .vm import decode_module  # noqa: F401 - the firmware's module format; re-exported for callers

PKG = "com.erostek.eroslink."

# --------------------------------------------------------------------------------------------
# enums (ErosLink "typesafe enum" classes serialise their short code)
RESTART, REVERSE, EXECUTE, HOLD_VALUE, REVERSE_FLIP = 1, 2, 3, 4, 5            # TriggerAction
FROM_RAND, FROM_STORE, DIV2, DIV4, DIV8, SET_VALUE, ADD_VALUE, AND_VALUE, OR_VALUE, XOR_VALUE = range(2, 12)
VF_NATIVE, VF_ADVANCED, VF_MULTI_ADJUST, VF_OTHER_CHANNEL = 1, 2, 3, 4          # ValueFrom
CH_A, CH_B, CH_BOTH = 1, 2, 3                                                   # ChannelIngredient.Channel
FROM_SET, FROM_MULTI_A, FROM_ADV = 1, 2, 3                                      # GateFromIngredient.From


def _i8(v: int) -> int:
    """Java (byte) cast."""
    v &= 0xFF
    return v - 256 if v >= 128 else v


def _jint(x: float) -> int:
    """Java (int) cast of a double: truncate toward zero, saturate (NaN -> 0)."""
    if x != x:
        return 0
    if x >= 2**31 - 1:
        return 2**31 - 1
    if x <= -2**31:
        return -2**31
    return int(x)


def _java_decode(tok: str) -> int:
    """java.lang.Integer.decode()."""
    s = tok
    neg = False
    if s[:1] in "+-":
        neg = s[0] == "-"
        s = s[1:]
    if s[:2].lower() == "0x":
        v = int(s[2:], 16)
    elif s[:1] == "#":
        v = int(s[1:], 16)
    elif len(s) > 1 and s[0] == "0":
        v = int(s[1:], 8)
    else:
        v = int(s, 10)
    return -v if neg else v


# --------------------------------------------------------------------------------------------
# stage 1: decoded routine model

@dataclass
class Ingredient:
    kind: str                      # class name without package, e.g. "RampIngredient"
    name: str                      # instance name (unique within the routine)
    also_do: str | None            # "and also do" ingredient name (chain), "<Nothing Else>" = none
    p: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        args = ", ".join(f"{k}={v}" for k, v in self.p.items())
        return f"{self.kind[:-10] if self.kind.endswith('Ingredient') else self.kind} {self.name!r} ({args}) also-do {self.also_do!r}"


@dataclass
class RoutineDef:
    name: str
    description: str
    ingredients: list[Ingredient]


@dataclass
class ContextFile:
    routines: list[RoutineDef]
    presets: list[tuple[str, list[str]]]


def _enum_code(obj: Any) -> int:
    if obj is None:
        return 0
    it = obj.items()
    it.read_string()          # version
    return it.read_short()


def _read_abstract(obj: JavaObject) -> tuple[str, str | None]:
    it = obj.items(PKG + "AbstractIngredient")
    ver = it.read_string()
    name = it.read_string()
    also = it.read_string()
    if ver != "V1-20011109":
        it.read_bool()        # use default background colour
        it.read_int()         # background colour
    return name, also


def _read_ramp(obj: JavaObject) -> dict[str, Any]:
    it = obj.items(PKG + "RampIngredient")
    ver = it.read_string()
    p = dict(start=it.read_double(), end=it.read_double(), time=it.read_double(), and_then=it.read_string(),
             intensity=it.read_bool(), frequency=it.read_bool(), width=it.read_bool())
    p["full_range"] = True if ver == "V1-20011107" else it.read_bool()
    return p


def _read_ingredient(obj: JavaObject) -> Ingredient:
    kind = obj.class_name.rsplit(".", 1)[-1]
    name, also = _read_abstract(obj)
    own = obj.items()
    p: dict[str, Any] = {}
    if kind == "ChannelIngredient":
        own.read_string()
        p["channel"] = _enum_code(own.read_object())
    elif kind == "RampIngredient":
        p = _read_ramp(obj)
    elif kind == "MultiARampIngredient":
        p = _read_ramp(obj)
        own.read_string()
        p.update(ma_intensity=own.read_bool(), ma_frequency=own.read_bool(), ma_width=own.read_bool(),
                 ma_affects_min=own.read_bool(), other_bound=own.read_double())
    elif kind == "MultiAIngredient":
        ver = own.read_string()
        p = dict(start=own.read_double(), end=own.read_double(), intensity=own.read_bool(),
                 frequency=own.read_bool(), width=own.read_bool())
        p["full_range"] = True if ver == "V1-20011231" else own.read_bool()
    elif kind == "MultiAGateIngredient":
        own.read_string()
        p = dict(min_time=own.read_double(), max_time=own.read_double(), on_time=own.read_bool(),
                 off_time=own.read_bool())
    elif kind == "GateIngredient":
        own.read_string()
        p = dict(on_time=own.read_double(), off_time=own.read_double())
    elif kind == "TimeGotoIngredient":
        own.read_string()
        p = dict(time=own.read_double(), and_then=own.read_string())
    elif kind == "SetValueIngredient":
        ver = own.read_string()
        v = own.read_double()
        p = dict(value=min(100.0, max(0.0, v)), intensity=own.read_bool(), frequency=own.read_bool(),
                 width=own.read_bool())
        cancel = [True, True, True]
        vfrom = [VF_NATIVE, VF_NATIVE, VF_NATIVE]
        full = True
        if ver == "V1-20011107":
            pass
        elif ver == "V2-20030226":
            full = own.read_bool()
        elif ver == "V3-20030328":
            full = own.read_bool()
            cancel = [own.read_bool()] * 3
        elif ver == "V4-20030423":
            full = own.read_bool()
            cancel = [own.read_bool()] * 3
            vfrom = [_enum_code(own.read_object()) for _ in range(3)]
            own.read_bool()
        else:
            full = own.read_bool()
            cancel = [own.read_bool() for _ in range(3)]
            vfrom = [_enum_code(own.read_object()) for _ in range(3)]
            own.read_bool()
        p.update(full_range=full, cancel=cancel, value_from=vfrom)
    elif kind == "RawIngredient":
        own.read_string()
        s = own.read_string()
        if s is not None:
            # ErosLink stores the raw text lightly obfuscated: per-position char offsets, then reversed
            chars = []
            for i, ch in enumerate(s):
                c = ord(ch)
                if i % 2 == 0:
                    c -= 24 if i > 6 else 23
                else:
                    c -= 32 if i > 8 else 34
                chars.append(chr(c & 0xFFFF))
            s = "".join(chars)[::-1]
        p = dict(raw=s)
    elif kind == "ExtTriggerIngredient":
        own.read_string()
        p = dict(and_then=own.read_string())
    elif kind == "GateFromIngredient":
        own.read_string()
        p = dict(on_from=_enum_code(own.read_object()), off_from=_enum_code(own.read_object()))
    else:
        raise ValueError(f"unsupported ErosLink ingredient {obj.class_name}")
    return Ingredient(kind, name, also, p)


def read_context(data: bytes) -> ContextFile:
    """Decode a whole .elk file (ErosLink Context.load)."""
    it = javaser.Items(javaser.parse(data))
    ver = it.read_string()
    if ver not in ("Context V1-20020725", "Context V2-20030519"):
        raise ValueError(f"not an ErosLink routine file (header {ver!r})")
    for _ in range(3):
        it.read_string()                       # ErosLinkHelper.loadSerial: serial / licence strings
    it.read_string()
    if ver != "Context V1-20020725":
        if it.read_bool():
            for _ in range(4):
                it.read_byte()
    it.read_string()                           # save date
    lists = []
    for _ in range(4):                         # triggers, parameter sets, routines, presets
        n = it.read_int()
        lists.append([it.read_object() for _ in range(n)])
    routines = []
    for obj in lists[2]:
        r = obj.items()
        r.read_string()
        name = r.read_string()
        desc = r.read_string() or ""
        n = r.read_int()
        ings = [_read_ingredient(r.read_object()) for _ in range(n)]
        routines.append(RoutineDef(name, desc, ings))
    presets = []
    for obj in lists[3]:
        r = obj.items()
        r.read_string()
        pname = r.read_string()
        n = r.read_int()
        presets.append((pname, [r.read_string() for _ in range(n)]))
    return ContextFile(routines, presets)


# --------------------------------------------------------------------------------------------
# stage 2: low-level triggers / parameter sets (ErosLink genLowLevel)

@dataclass
class Value:
    value: int = 255
    change: bool = False
    action: int = SET_VALUE
    to_store_pre: bool = False
    to_store_post: bool = False


def _v(n: int) -> Value:
    return Value(value=n, change=True)


@dataclass
class TPS:
    """TriggerParameterSet: which ParameterSet to load into a modulator, and what its ends do."""
    ps_change: bool = False
    ps_name: str | None = None
    min_change: bool = False
    min_action: int = RESTART
    min_exec: str | None = None
    max_change: bool = False
    max_action: int = RESTART
    max_exec: str | None = None


@dataclass
class ParameterSet:
    name: str = "Change Me"
    initial: Value = field(default_factory=Value)
    min: Value = field(default_factory=Value)
    max: Value = field(default_factory=Value)
    incdec: Value = field(default_factory=lambda: Value(value=0, change=False))
    value_change: bool = False
    rate_change: bool = False
    value_mask: int = 0
    rate_mask: int = 0
    timer: Value = field(default_factory=Value)
    timer_mask: int = 0
    timer_change: bool = False


@dataclass
class Trigger:
    name: str = "Change Me"
    channels_change: bool = True
    affect_a: bool = True
    affect_b: bool = False
    chain: bool = False
    chain_name: str | None = None
    ma_min: int = 1
    ma_min_change: bool = False
    ma_max: int = 10
    ma_max_change: bool = False
    rnd_min: int = 0
    rnd_min_change: bool = False
    rnd_max: int = 8
    rnd_max_change: bool = False
    intensity_ramp: TPS = field(default_factory=TPS)
    intensity_mod: TPS = field(default_factory=TPS)
    freq_ramp: TPS = field(default_factory=TPS)
    width_ramp: TPS = field(default_factory=TPS)
    output_enable: bool = False
    output_change: bool = False
    output_positive: bool = True
    output_negative: bool = True
    output_alternate: bool = False
    output_invert: bool = False
    output_audio_gate: bool = False
    tl_time: Value = field(default_factory=Value)
    tl_unit: int = 3
    tl_unit_change: bool = False
    tl_exec_change: bool = False
    tl_exec: str | None = None
    gate_unit_change: bool = False
    gate_unit: int = 0
    gate_on_from_change: bool = False
    gate_on_from: int = 0
    gate_on_always: bool = False
    gate_on_time: Value = field(default_factory=Value)
    gate_off_from_change: bool = False
    gate_off_from: int = 0
    gate_off_always: bool = False
    gate_off_time: Value = field(default_factory=Value)
    ctrl_change: bool = False
    ctrl_mask: int = 0
    ext_enable_change: bool = False
    ext_enable: bool = False
    ext_change: bool = False
    ext_name: str | None = None
    raw: bool = False
    raw_data: list | None = None       # ints (signed bytes) and str (trigger references)


def _trigger_action(s: str) -> int:
    u = s.upper()
    if u == "<REVERSE>":
        return REVERSE
    if u in ("<RESTART>", "<START OVER>"):
        return RESTART
    if u == "<HOLD>":
        return HOLD_VALUE
    if u in ("<REVERSE FLIP>", "<REVERSE FLIP END>", "<REVERSE FLIP BOTH>"):
        return REVERSE_FLIP
    return EXECUTE


class _Lowering:
    def __init__(self, routine: str) -> None:
        self.r = routine
        self.triggers: list[Trigger] = []
        self.psets: list[ParameterSet] = []

    def common(self, ing: Ingredient, t: Trigger) -> None:
        t.name = self.r + ing.name
        if ing.also_do is not None:
            t.chain = True
            t.chain_name = self.r + ing.also_do
        else:
            t.chain = False

    # ---- RampIngredient._$26519: one ramping parameter set
    def _ramp_tps(self, ing: Ingredient, what: str, a: int, b: int, ma_val: bool, ma_rate: bool,
                  other: float, st: dict) -> TPS:
        p = ing.p
        if a == b:
            if a == 0:
                b += 1
            else:
                a -= 1
        tps = TPS()
        ps = ParameterSet()
        ps.initial = _v(a)
        ps.name = self.r + what + ing.name
        tps.ps_change = True
        tps.ps_name = ps.name
        ps.timer_change = True
        ps.timer_mask = 1
        steps = 1
        per = p["time"] * 250 / abs(b - a)
        per_other = other * 250 / abs(b - a)
        t = per
        while t < 1.0 and steps < 3:
            steps += 1
            t = per * steps
        err = abs(t - _jint(t + 0.5))
        best = steps
        if p["time"] < 1.024:
            while steps < 3:
                steps += 1
                t = per * steps
                e2 = abs(t - _jint(t + 0.5))
                if e2 < err:
                    err = e2
                    best = steps
        t = per * best
        t_other = per_other * best
        if t > 255:
            ps.timer_mask = 2
            t /= 8.0
            t_other /= 8.0
        if t > 255:
            ps.timer_mask = 3
            t /= 32.0
            t_other /= 32.0
        ps.timer = _v(1 if t < 1 else 255 if t > 255 else _jint(t))
        st["primary"] = _jint(t)
        st["bound"] = 1 if t_other < 1 else 255 if t_other > 255 else _jint(t_other)
        ps.value_change = True
        ps.value_mask = 8 if ma_val else 0
        ps.rate_change = True
        ps.rate_mask = 64 if ma_rate else 0
        then = p["and_then"]
        act = _trigger_action(then)
        if a <= b:
            ps.incdec = _v(best)
            ps.min = _v(a)
            ps.max = _v(b)
            tps.max_change = True
            tps.max_action = act
            if act == EXECUTE:
                tps.max_exec = self.r + then
                tps.min_change = True
                tps.min_action = HOLD_VALUE
            elif act == REVERSE:
                tps.min_change = True
                tps.min_action = REVERSE
            elif act == REVERSE_FLIP:
                tps.min_change = True
                tps.min_action = REVERSE_FLIP if then.upper() == "<REVERSE FLIP BOTH>" else REVERSE
        else:
            ps.incdec = _v(-best)
            ps.min = _v(b)
            ps.max = _v(a)
            tps.min_change = True
            tps.min_action = act
            if act == EXECUTE:
                tps.min_exec = self.r + then
                tps.max_change = True
                tps.max_action = HOLD_VALUE
            elif act == REVERSE:
                tps.max_change = True
                tps.max_action = REVERSE
            elif act == REVERSE_FLIP:
                tps.max_change = True
                tps.max_action = REVERSE_FLIP if then.upper() == "<REVERSE FLIP BOTH>" else REVERSE
        self.psets.append(ps)
        return tps

    def ramp(self, ing: Ingredient, ma: tuple[bool, bool, bool, bool, bool, bool] = (False,) * 6,
             other: float = 0.0) -> None:
        mi, mf, mw, ri, rf, rw = ma
        p = ing.p
        if other < 0:
            other = 0.0
        t = Trigger()
        self.common(ing, t)
        lo, hi = 0, 255
        st: dict = {}

        def bound(val_a: int, val_b: int, val_o: int) -> None:
            nonlocal lo, hi
            vmin, vmax = min(val_a, val_b), max(val_a, val_b)
            if val_o < vmin:
                lo = max(lo, val_o)
                hi = min(hi, vmax - 1)
            else:
                lo = max(lo, vmin)
                hi = min(hi, min(val_o, vmax - 1))

        def rate_bound() -> None:
            nonlocal lo, hi
            if other < p["time"]:
                lo = max(lo, st["bound"])
                hi = st["primary"]
            else:
                hi = min(hi, st["bound"])
                lo = st["primary"]

        oc = 100.0 if other > 100.0 else other
        if p["intensity"]:
            base = 0 if p["full_range"] else 127
            a = _jint(p["start"] * (255 - base) / 100) + base
            b = _jint(p["end"] * (255 - base) / 100) + base
            if mi and not ri and not rf and not rw:
                bound(a, b, _jint(oc * (255 - base) / 100) + base)
            t.intensity_mod = self._ramp_tps(ing, "int", a, b, mi, ri, other, st)
            if ri:
                rate_bound()
        if p["frequency"]:
            a = _jint((100 - p["start"]) * 247 / 100) + 8
            b = _jint((100 - p["end"]) * 247 / 100) + 8
            if mf and not ri and not rf and not rw:
                bound(a, b, _jint((100 - oc) * 247 / 100) + 8)
            t.freq_ramp = self._ramp_tps(ing, "freq", a, b, mf, rf, other, st)
            if rf:
                rate_bound()
        if p["width"]:
            base = 50 if p["full_range"] else 70
            a = _jint(p["start"] * (255 - base) / 100) + base
            b = _jint(p["end"] * (255 - base) / 100) + base
            if mw and not ri and not rf and not rw:
                bound(a, b, _jint(oc * (255 - base) / 100) + base)
            t.width_ramp = self._ramp_tps(ing, "width", a, b, mw, rw, other, st)
            if rw:
                rate_bound()
        if any(ma) and lo < hi:
            t.ma_min, t.ma_max = min(lo, hi), max(lo, hi)
            t.ma_min_change = t.ma_max_change = True
        self.triggers.append(t)

    def lower(self, ing: Ingredient) -> None:
        k, p = ing.kind, ing.p
        if k == "ChannelIngredient":
            t = Trigger()
            self.common(ing, t)
            t.raw = True
            t.raw_data = [-123, {CH_B: 2, CH_BOTH: 3}.get(p["channel"], 1)]     # $85 = channel mask
            self.triggers.append(t)
        elif k == "RampIngredient":
            self.ramp(ing)
        elif k == "MultiARampIngredient":
            if p["ma_affects_min"]:
                self.ramp(ing, (p["ma_intensity"], p["ma_frequency"], p["ma_width"], False, False, False),
                          100.0 if p["other_bound"] > 100.0 else p["other_bound"])
            else:
                self.ramp(ing, (False, False, False, p["ma_intensity"], p["ma_frequency"], p["ma_width"]),
                          p["other_bound"])
        elif k == "MultiAIngredient":
            t = Trigger()
            self.common(ing, t)
            tps = TPS()
            ps = ParameterSet()
            base = 8
            if p["intensity"]:
                base = max(base, 0 if p["full_range"] else 127)
            if p["width"]:
                base = max(base, 50 if p["full_range"] else 70)
            if p["frequency"]:
                base = max(base, 8)
            a = _jint(p["start"] * (255.0 - base) / 100.0) + base
            b = _jint(p["end"] * (255.0 - base) / 100.0) + base
            if a > b:
                if b == 0:
                    b += 1
                a = b - 1
            t.ma_min, t.ma_min_change, t.ma_max, t.ma_max_change = a, True, b, True
            ps.name = self.r + ing.name
            tps.ps_change = True
            tps.ps_name = ps.name
            ps.value_change = True
            ps.value_mask = 8
            if p["intensity"]:
                t.intensity_mod = tps
            if p["frequency"]:
                t.freq_ramp = tps
            if p["width"]:
                t.width_ramp = tps
            self.triggers.append(t)
            self.psets.append(ps)
        elif k == "MultiAGateIngredient":
            t = Trigger()
            self.common(ing, t)
            u1, n1 = 1, _jint(p["min_time"] * 250)
            if n1 > 255:
                u1, n1 = 2, _jint(n1 / 8.0)
            if n1 > 255:
                u1, n1 = 3, _jint(n1 / 32.0)
            u2, n2 = 1, _jint(p["max_time"] * 250)
            if n2 > 255:
                u2, n2 = 2, _jint(n2 / 8.0)
            if n2 > 255:
                u2, n2 = 3, _jint(n2 / 32.0)
            if n1 != 0 and u1 == 3 and u2 != 3:
                n2 = max(1, _jint(n2 / 32.0 + 0.5)) if u2 == 2 else 1
                u2 = 3
            elif n1 != 0 and n2 != 0 and u2 == 3 and u1 != 3:
                n1 = max(1, _jint(n1 / 32.0 + 0.5)) if u1 == 2 else 1
                u1 = 3
            elif n1 != 0 and u1 == 2 and u2 != 2:
                n2 = max(1, _jint(n2 / 32.0 + 0.5))      # ErosLink quirk: /32 for a 4 ms -> 32 ms unit change
                u2 = 2
            elif n1 != 0 and n2 != 0 and u2 == 2 and u1 != 2:
                n1 = max(1, _jint(n1 / 32.0 + 0.5))
                u1 = 2
            if n1 > n2:
                if n2 == 0:
                    n2 += 1
                n1 = n2 - 1
            t.gate_unit_change, t.gate_unit = True, u1
            t.ma_min, t.ma_min_change, t.ma_max, t.ma_max_change = n1, True, n2, True
            t.gate_on_from_change, t.gate_on_from = True, 64 if p["on_time"] else 0
            t.gate_off_from_change, t.gate_off_from = True, 8 if p["off_time"] else 0
            self.triggers.append(t)
        elif k == "GateIngredient":
            t = Trigger()
            self.common(ing, t)
            t.gate_on_always = p["off_time"] == 0          # ErosLink quirk: on-always follows the OFF time
            t.gate_off_always = p["on_time"] == 0
            t.gate_on_from_change, t.gate_on_from = True, 0
            t.gate_off_from_change, t.gate_off_from = True, 0
            t.gate_unit_change = True
            u1, on = 1, p["on_time"] * 250
            if on > 255:
                u1, on = 2, on / 8.0
            if on > 255:
                u1, on = 3, on / 32.0
            u2, off = 1, p["off_time"] * 250
            if off > 255:
                u2, off = 2, off / 8.0
            if off > 255:
                u2, off = 3, off / 32.0
            if u1 == 3 and u2 != 3:
                if off != 0:
                    off = max(1.0, float(_jint(off / 32.0 + 0.5))) if u2 == 2 else 1.0
                u2 = 3
            elif u2 == 3 and u1 != 3:
                if off != 0:                                 # ErosLink quirk: tests off, rescales on
                    on = max(1.0, float(_jint(on / 32.0 + 0.5))) if u1 == 2 else 1.0
                u1 = 3
            elif u1 == 2 and u2 != 2:
                if off != 0:
                    off = max(1.0, float(_jint(off / 32.0 + 0.5)))
                u2 = 2
            elif u2 == 2 and u1 != 2:
                if on != 0:
                    on = max(1.0, float(_jint(on / 32.0 + 0.5)))
                u1 = 2
            t.gate_unit = u1
            t.gate_on_time = _v(1 if on < 1 else 255 if on > 255 else _jint(on))
            t.gate_off_time = _v(1 if off < 1 else 255 if on > 255 else _jint(off))   # ErosLink quirk: tests on
            self.triggers.append(t)
        elif k == "TimeGotoIngredient":
            t = Trigger()
            self.common(ing, t)
            t.affect_a, t.affect_b = False, True
            t.tl_exec_change = True
            then = p["and_then"]
            t.tl_exec = self.r + (ing.name if _trigger_action(then) != EXECUTE else then)
            t.tl_unit_change = True
            if then.upper() == "<NOTHING ELSE>":
                t.tl_unit = 0
            else:
                t.tl_unit = 1
                x = p["time"] * 250
                y = x
                if x > 255:
                    t.tl_unit = 2
                    y = x / 8.0
                if y > 255:
                    t.tl_unit = 3
                    y /= 32.0
                t.tl_time = _v(1 if y < 1 else 255 if y > 255 else _jint(y))
            self.triggers.append(t)
        elif k == "SetValueIngredient":
            t = Trigger()
            self.common(ing, t)
            val = p["value"]

            def sv(what: str, n: int, cancel: bool, vf: int) -> TPS:
                tps = TPS()
                ps = ParameterSet()
                ps.name = self.r + what + ing.name
                tps.ps_change, tps.ps_name = True, ps.name
                ps.value_change = True
                if vf == VF_ADVANCED:
                    ps.value_mask = 4
                elif vf == VF_OTHER_CHANNEL:
                    ps.value_mask = 12
                elif vf == VF_MULTI_ADJUST:
                    ps.value_mask = 8
                else:
                    ps.initial = _v(n)
                    ps.value_mask = 0
                if cancel:
                    ps.rate_change, ps.rate_mask = True, 0
                    ps.incdec = _v(0)
                    ps.timer_change, ps.timer_mask = True, 0
                    tps.max_change = tps.min_change = True
                    tps.max_action = tps.min_action = HOLD_VALUE
                self.psets.append(ps)
                return tps

            if p["intensity"]:
                base = 0 if p["full_range"] else 127
                t.intensity_mod = sv("int", _jint(val * (255 - base) / 100) + base, p["cancel"][0], p["value_from"][0])
            if p["frequency"]:
                t.freq_ramp = sv("freq", _jint((100 - val) * 247 / 100) + 8, p["cancel"][1], p["value_from"][1])
            if p["width"]:
                base = 50 if p["full_range"] else 70
                t.width_ramp = sv("width", _jint(val * (255 - base) / 100) + base, p["cancel"][2], p["value_from"][2])
            self.triggers.append(t)
        elif k == "RawIngredient":
            t = Trigger()
            self.common(ing, t)
            t.raw = True
            data: list = []
            s = (p["raw"] or "").strip().replace("\t", " ").replace("\n", " ").replace("\r", " ")
            while s:
                i = s.find(" ")
                if i >= 0:
                    tok, s = s[:i], s[i:].strip()
                else:
                    tok, s = s, ""
                if tok[0] == "*":
                    data.append(self.r + tok[1:])
                else:
                    data.append(_i8(_java_decode(tok)))
            t.raw_data = data
            self.triggers.append(t)
        elif k == "ExtTriggerIngredient":
            t = Trigger()
            self.common(ing, t)
            t.ext_change = t.ext_enable = t.ext_enable_change = True
            then = p["and_then"]
            t.ext_name = self.r + (ing.name if _trigger_action(then) != EXECUTE else then)
            t.ctrl_change, t.ctrl_mask = True, 2
            self.triggers.append(t)
        elif k == "GateFromIngredient":
            t = Trigger()
            self.common(ing, t)
            t.gate_on_from_change = t.gate_off_from_change = True
            t.gate_on_from = {FROM_MULTI_A: 64, FROM_ADV: 32}.get(p["on_from"], 0)
            if p["off_from"] == FROM_MULTI_A:
                t.gate_off_from = 8
            elif p["on_from"] == FROM_ADV:                  # ErosLink quirk: tests on_from
                t.gate_off_from = 4
            else:
                t.gate_off_from = 0
            self.triggers.append(t)
        else:
            raise ValueError(f"unsupported ingredient {k}")


def lower_routine(r: RoutineDef) -> tuple[list[Trigger], list[ParameterSet], str | None]:
    """All ingredients -> (triggers, parameter sets, start trigger name), as WriteRoutinesPanel does."""
    lw = _Lowering(r.name)
    start = None
    for i, ing in enumerate(r.ingredients):
        lw.lower(ing)
        if i == 0 and start is None:
            start = r.name + ing.name
        if ing.name == "1":
            start = r.name + ing.name
    return lw.triggers, lw.psets, start


# --------------------------------------------------------------------------------------------
# stage 3: module compiler (ET312.Module / TriggerModule / ModuleSet.Memory)

@dataclass(eq=False)
class _Pair:
    addr: int
    value: Any                         # int (signed byte), str (trigger reference) or None
    mask: int = 0xFF
    kind: str = "MemPair"
    opcode: int = 0
    sort_pri: int = 0
    sort_group: int = 0
    dont_sort: bool = False
    boundary: bool = False             # "useless write boundary": de-duplication never looks past it


_OPCODES = {"ToStore": 64, "FromStore": 68, "Div2": 72, "Rand": 76, "Add": 80, "And": 84, "Or": 88, "Xor": 92}


def _op_pair(kind: str, addr: int, value: Any = None, post: bool = False) -> _Pair:
    p = _Pair(addr, value, kind=kind, opcode=_OPCODES[kind])
    if kind == "ToStore":
        p.sort_pri = 1000000 if post else -1000000
        p.boundary = True
    elif kind == "FromStore":
        p.sort_pri = 1
        p.boundary = True
    elif kind == "Rand":
        p.dont_sort = True
    else:
        p.sort_pri = 1
    return p


def _cmp(a: _Pair, b: _Pair) -> int:
    """ET312.MemPair.compareTo (not a total order: a dont-sort pair equals everything)."""
    if a.dont_sort or b.dont_sort:
        return 0
    for x, y in ((a.sort_group, b.sort_group), (a.sort_pri, b.sort_pri), (a.addr, b.addr)):
        if x < y:
            return -1
        if x > y:
            return 1
    return 0


def _java_sort(lst: list) -> None:
    """java.util.Collections.sort as in the JRE 1.4 ErosLink shipped with (legacy merge sort).
    The comparator is not a total order, so the exact algorithm matters."""
    aux = lst[:]

    def ms(src: list, dest: list, low: int, high: int, off: int) -> None:
        length = high - low
        if length < 7:
            for i in range(low, high):
                j = i
                while j > low and _cmp(dest[j - 1], dest[j]) > 0:
                    dest[j], dest[j - 1] = dest[j - 1], dest[j]
                    j -= 1
            return
        dlow, dhigh = low, high
        low += off
        high += off
        mid = (low + high) >> 1
        ms(dest, src, low, mid, -off)
        ms(dest, src, mid, high, -off)
        if _cmp(src[mid - 1], src[mid]) <= 0:
            dest[dlow:dhigh] = src[low:high]
            return
        p, q = low, mid
        for i in range(dlow, dhigh):
            if q >= high or (p < mid and _cmp(src[p], src[q]) <= 0):
                dest[i] = src[p]
                p += 1
            else:
                dest[i] = src[q]
                q += 1

    ms(aux, lst, 0, len(lst), 0)


class _Infinite(Exception):
    pass


@dataclass
class _TModule:
    name: str
    pairs: list
    code: bytes = b""
    unresolved: bool = False
    vector: int = 0


class _Compiler:
    def __init__(self, triggers: list[Trigger], psets: list[ParameterSet]) -> None:
        self.triggers = triggers
        self.psets = psets
        self.modules: list[_TModule] = []
        self.in_progress: list[Trigger] = []

    def find_trigger(self, name: str | None) -> Trigger | None:
        for t in self.triggers:
            if name is not None and t.name == name:
                return t
        return None

    def find_ps(self, name: str | None) -> ParameterSet | None:
        for p in self.psets:
            if name is not None and p.name == name:
                return p
        return None

    def known(self, name: str) -> bool:
        return any(m.name == name for m in self.modules) or any(t.name == name for t in self.in_progress)

    def ref(self, name: str | None) -> None:
        if name is not None and not self.known(name):
            t = self.find_trigger(name)
            if t is not None:
                self.module(t)

    # ---- ET312.Module(Trigger)
    def module(self, trig: Trigger) -> None:
        if any(t is trig for t in self.in_progress):
            raise _Infinite()
        self.in_progress.append(trig)
        pairs: list = []
        state = {"group": 0, "rnd_min": None, "rnd_max": None}
        cur, first = trig, None
        while cur is not None and cur.chain:
            if first is None:
                first = cur
            elif cur is first:
                raise _Infinite()
            self.process(cur, pairs, state)
            nxt = self.find_trigger(cur.chain_name)
            self.dedupe(pairs)
            cur = nxt
        if not trig.chain:
            self.process(trig, pairs, state)
        self.modules.append(_TModule(trig.name, pairs))
        self.in_progress.remove(trig)

    def put(self, pairs: list, addr: int, v: Any, a: bool, b: bool, mask: int | None = None) -> None:
        if a:
            pairs.append(_Pair(addr, v, mask if mask is not None else 0xFF))
        if b:
            pairs.append(_Pair(addr + 256, v, mask if mask is not None else 0xFF))

    def op(self, pairs: list, kind: str, addr: int, a: bool, b: bool, v: Any = None, post: bool = False) -> None:
        if a:
            pairs.append(_op_pair(kind, addr, v, post))
        if b:
            pairs.append(_op_pair(kind, addr + 256, v, post))

    def process(self, t: Trigger, pairs: list, state: dict) -> None:
        if t.raw:
            for item in t.raw_data or []:
                p = _Pair(0, item, kind="RawLowLevel", dont_sort=True, boundary=True)
                pairs.append(p)
                if isinstance(item, str) and not self.known(item):
                    tt = self.find_trigger(item)
                    if tt is not None:
                        self.module(tt)
            return
        a, b = t.affect_a, t.affect_b
        lo = 0
        if t.ma_min_change:
            lo = min(t.ma_min, 254)
            self.put(pairs, 134, _i8(lo), True, False)
        if t.ma_max_change:
            hi = t.ma_max
            if hi <= lo:
                hi = lo + 1
            self.put(pairs, 135, _i8(hi), True, False)
        if t.rnd_min_change:
            self.put(pairs, 141, _i8(t.rnd_min), a, b)
            state["rnd_min"] = t.rnd_min
        if t.rnd_max_change:
            self.put(pairs, 142, _i8(t.rnd_max), a, b)
            state["rnd_max"] = t.rnd_max
        if t.ctrl_change and t.ext_enable_change:
            self.put(pairs, 131, _i8(t.ctrl_mask | 2 if t.ext_enable else t.ctrl_mask), True, False)
        if t.ext_change:
            self.put(pairs, 143, t.ext_name, True, False)
            self.ref(t.ext_name)
        if t.output_change:
            g = 0
            g |= 1 if t.output_enable else 0
            g |= 4 if t.output_positive else 0
            g |= 2 if t.output_negative else 0
            g |= 8 if t.output_alternate else 0
            g |= 16 if t.output_invert else 0
            g |= 32 if t.output_audio_gate else 0
            self.put(pairs, 144, _i8(g), a, b)
        # time limit (block timer)
        if t.tl_unit_change:
            self.put(pairs, 150, _i8(t.tl_unit), a, b)
        if t.tl_time.change:
            self.value(pairs, t.tl_time, 149, a, b, 0, state)
        if t.tl_exec_change:
            self.put(pairs, 151, t.tl_exec, a, b)
            self.ref(t.tl_exec)
        # gate
        m = v = 0
        if t.gate_unit_change:
            m |= 3
            v |= t.gate_unit
        if t.gate_on_from_change:
            m |= 96
            v |= t.gate_on_from
        if t.gate_off_from_change:
            m |= 12
            v |= t.gate_off_from
        if m:
            self.put(pairs, 154, _i8(v), a, b, None if m == 111 else m)
        if t.gate_on_time.change:
            gv = replace(t.gate_on_time)
            if t.gate_on_always:
                gv.value, gv.action = 0, SET_VALUE
            elif gv.value == 0 and gv.action == SET_VALUE:
                gv.value = 1
            self.value(pairs, gv, 152, a, b, 0, state)
        if t.gate_off_time.change:
            gv = replace(t.gate_off_time)
            if t.gate_off_always:
                gv.value, gv.action = 0, SET_VALUE
            elif gv.value == 0 and gv.action == SET_VALUE:
                gv.value = 1
            self.value(pairs, gv, 153, a, b, 0, state)
        self.tps(pairs, t.intensity_ramp, 156, a, b, 0, state)
        self.tps(pairs, t.intensity_mod, 165, a, b, 0, state)
        self.tps(pairs, t.freq_ramp, 174, a, b, 8, state)
        self.tps(pairs, t.width_ramp, 183, a, b, 50, state)
        for p in pairs:
            if p.kind == "RawLowLevel":
                state["group"] += 1
                p.sort_group = state["group"]
                state["group"] += 1
                p.dont_sort = False
            else:
                p.sort_group = state["group"]
        _java_sort(pairs)

    def value(self, pairs: list, v: Value, addr: int, a: bool, b: bool, legal_min: int, state: dict) -> None:
        bv = _i8(v.value)
        u = bv & 0xFF
        act = v.action
        if act == SET_VALUE:
            if u < legal_min:
                bv = _i8(legal_min)
            self.put(pairs, addr, bv, a, b)
        elif act == FROM_RAND:
            rmin, rmax = state["rnd_min"], state["rnd_max"]
            if legal_min < 1 or (rmin is not None and rmax is not None and rmin >= legal_min and rmax >= legal_min):
                self.op(pairs, "Rand", addr, a, b)
        elif act == OR_VALUE:
            if legal_min <= u:
                self.op(pairs, "Or", addr, a, b, bv)
        elif legal_min <= 0:
            if act == FROM_STORE:
                self.op(pairs, "FromStore", addr, a, b)
            elif act in (DIV8, DIV4, DIV2):
                for _ in range({DIV8: 3, DIV4: 2, DIV2: 1}[act]):
                    self.op(pairs, "Div2", addr, a, b)
            elif act == ADD_VALUE:
                self.op(pairs, "Add", addr, a, b, bv)
            elif act == AND_VALUE:
                self.op(pairs, "And", addr, a, b, bv)
            elif act == XOR_VALUE:
                self.op(pairs, "Xor", addr, a, b, bv)

    def pset(self, pairs: list, ps: ParameterSet, base: int, a: bool, b: bool, legal_min: int, state: dict) -> None:
        for off, v in ((0, ps.initial), (1, ps.min), (2, ps.max), (4, ps.incdec)):
            if v.to_store_pre:
                self.op(pairs, "ToStore", base + off, a, b, post=False)
        for off, v, lm in ((0, ps.initial, legal_min), (1, ps.min, legal_min), (2, ps.max, legal_min),
                           (4, ps.incdec, -128)):
            if v.change:
                self.value(pairs, v, base + off, a, b, lm, state)
        val = mask = 0
        if ps.value_change:
            mask |= 0x1C
            val |= ps.value_mask
        if ps.rate_change:
            mask |= 0xE0
            val |= ps.rate_mask
        if ps.timer_change:
            mask |= 0x03
            val |= ps.timer_mask
        if mask:
            self.put(pairs, base + 7, _i8(val), a, b, None if mask == 0xFF else mask)
        if ps.timer.change:
            self.value(pairs, ps.timer, base + 3, a, b, 0, state)
        for off, v in ((0, ps.initial), (1, ps.min), (2, ps.max), (4, ps.incdec)):
            if v.to_store_post:
                self.op(pairs, "ToStore", base + off, a, b, post=True)

    def tps(self, pairs: list, tp: TPS, base: int, a: bool, b: bool, legal_min: int, state: dict) -> None:
        if tp.ps_change:
            ps = self.find_ps(tp.ps_name)
            if ps is not None:
                self.pset(pairs, ps, base, a, b, legal_min, state)
        for chg, act, name, off in ((tp.min_change, tp.min_action, tp.min_exec, 5),
                                    (tp.max_change, tp.max_action, tp.max_exec, 6)):
            if not chg:
                continue
            if act == RESTART:
                self.put(pairs, base + off, -3, a, b)
            elif act == REVERSE:
                self.put(pairs, base + off, -1, a, b)
            elif act == EXECUTE:
                self.put(pairs, base + off, name, a, b)
                self.ref(name)
            elif act == REVERSE_FLIP:
                self.put(pairs, base + off, -2, a, b)
            else:
                self.put(pairs, base + off, -4, a, b)

    @staticmethod
    def dedupe(pairs: list) -> None:
        """Module._$21405: drop a write that a later write to the same register fully covers."""
        i = 0
        while i < len(pairs):
            p = pairs[i]
            removed = False
            if not p.boundary:
                acc = 0
                for q in pairs[i + 1:]:
                    if q.boundary or q.addr in (133, 141, 142):
                        break
                    if p.addr == q.addr and (p.mask & (q.mask | acc)) == p.mask:
                        del pairs[i]
                        removed = True
                        break
                    if p.addr == q.addr:
                        acc |= q.mask
            if not removed:
                i += 1

    # ---- TriggerModule: pairs -> bytecode
    @staticmethod
    def encode(mod: _TModule) -> None:
        out: list[int] = []
        mod.unresolved = False
        pairs = mod.pairs

        def val(v: Any) -> int:
            if isinstance(v, int):
                return v & 0xFF
            mod.unresolved = True
            return 0

        def op3(sub: int, addr: int, v: Any) -> None:
            out.extend([(0x50 | (sub & 12)) | ((addr & 0x300) >> 8), addr & 0xFF, val(v)])

        def short(p: _Pair) -> None:
            if 0x80 <= p.addr <= 0xBF:
                out.append(p.addr)
            elif 0x180 <= p.addr <= 0x1BF:
                out.append(p.addr - 192)
            else:
                out.append(0)
            out.append(val(p.value))

        i = 0
        while i < len(pairs):
            p = pairs[i]
            step = 1
            if p.opcode:
                # ErosLink quirk: every opcode pair is written as 2 bytes, even add/and/or/xor, whose
                # firmware form takes a third (value) byte.  No ingredient produces those four.
                out.extend([(p.opcode | ((p.addr & 0x300) >> 8)) & 0xFF, p.addr & 0xFF])
            elif p.kind == "RawLowLevel":
                out.append(val(p.value))
            elif p.mask != 0xFF:
                inv = (~p.mask) & 0xFF
                if inv != 0xFF:
                    op3(4, p.addr, inv)                          # AND with ~mask
                if isinstance(p.value, int):
                    o = p.value & p.mask
                    if o != 0:
                        op3(8, p.addr, o)                        # OR in the masked bits
                else:
                    op3(8, p.addr, None)
            else:
                n = 1
                while i + n < len(pairs):
                    q0, q1 = pairs[i + n - 1], pairs[i + n]
                    if q0.addr != q1.addr - 1 or q0.mask != q1.mask or q0.kind != q1.kind:
                        break
                    n += 1
                step = n
                if n < 2:
                    short(p)
                else:
                    cnt = 0
                    hdr = len(out)
                    for k in range(n):
                        q = pairs[i + k]
                        if cnt == 0:
                            if k == n - 1 and (0x80 <= q.addr <= 0xBF or 0x180 <= q.addr <= 0x1BF):
                                short(q)
                                break
                            hdr = len(out)
                            out.extend([0x20 | ((q.addr >> 8) & 3), q.addr & 0xFF])
                        out.append(val(q.value))
                        cnt += 1
                        if cnt > 5 or (i + k + 1 < len(pairs) and pairs[i + k + 1].addr - 1 != q.addr):
                            out[hdr] |= (cnt & 7) << 2
                            cnt = 0
                    if cnt:
                        out[hdr] |= (cnt & 7) << 2
            i += step
        out.append(0)
        mod.code = bytes(out)


# EEPROM layout ErosLink writes into ("Store in box"): two banks of module space + vector tables
_BANKS = (
    dict(vec_base=0x80, table=0x8020, table_end=0x803F, data=0x8040, data_end=0x80FF),
    dict(vec_base=0xA0, table=0x8100, table_end=0x811F, data=0x8120, data_end=0x81FF),
)


@dataclass
class CompiledRoutine:
    name: str
    description: str
    start: int                          # module number of the entry module
    modules: dict[int, bytes]           # module number -> bytecode (0-terminated)
    module_names: dict[int, str]
    fits_in_box: bool                   # False: needs more than the 416 bytes of EEPROM module space
    unresolved: bool                    # a module reference could not be resolved (ErosLink: "gen" failure)
    source: str = ""
    path: str = ""
    index: int = 0
    ingredients: list[Ingredient] = field(default_factory=list)

    def blocks(self) -> dict[int, list[tuple]]:
        return {k: decode_module(v) for k, v in self.modules.items()}

    @property
    def size(self) -> int:
        return sum(len(v) for v in self.modules.values())


def compile_routine(r: RoutineDef) -> CompiledRoutine:
    triggers, psets, start_name = lower_routine(r)
    c = _Compiler(triggers, psets)
    start_trig = c.find_trigger(start_name)
    if start_trig is None:
        raise ValueError(f"routine {r.name!r}: no start trigger")
    try:
        c.module(start_trig)
    except _Infinite:
        raise ValueError(f"routine {r.name!r}: modules form an and-then loop ErosLink refuses") from None
    for m in c.modules:
        _Compiler.encode(m)
    # allocate (last-created first, as ModuleSet.resolve_and_generate does)
    bank, addr, slot, fits = 0, _BANKS[0]["data"], 0, True
    for m in reversed(c.modules):
        n = len(m.code)
        while True:
            if bank == 2:
                # out of box memory (ErosLink: OutOfDeviceMemory); keep numbering past the box (0xc0..)
                # so the emulator can still run it
                fits = False
                m.vector = 0xC0 + slot
                slot += 1
                break
            bk = _BANKS[bank]
            if addr + n - 1 <= bk["data_end"] and bk["table"] + slot <= bk["table_end"]:
                m.vector = bk["vec_base"] + slot
                addr += n
                slot += 1
                break
            bank, slot = bank + 1, 0
            addr = _BANKS[bank]["data"] if bank < 2 else 0
    vec = {m.name: m.vector for m in c.modules}
    for m in c.modules:
        for p in m.pairs:
            if isinstance(p.value, str) and p.value in vec:
                p.value = _i8(vec[p.value])
    unresolved = False
    for m in c.modules:
        _Compiler.encode(m)
        unresolved |= m.unresolved
    return CompiledRoutine(
        name=r.name, description=r.description, start=vec[start_trig.name],
        modules={m.vector: m.code for m in c.modules}, module_names={m.vector: m.name for m in c.modules},
        fits_in_box=fits, unresolved=unresolved, ingredients=list(r.ingredients))


# --------------------------------------------------------------------------------------------
# files, cache, listing

def default_cache_dir() -> Path:
    """$STIM_ENGINE_EROSLINK_CACHE, else ~/.stim-engine/eroslink.  Not %LOCALAPPDATA%: the Microsoft Store
    Python this project runs on silently redirects new folders there into its own package cache, so the
    files would not be where the path says (and other programs could not see them)."""
    env = os.environ.get("STIM_ENGINE_EROSLINK_CACHE")
    if env:
        return Path(env)
    return Path.home() / ".stim-engine" / "eroslink"


def read_file(path: str | os.PathLike) -> ContextFile:
    return read_context(Path(path).read_bytes())


def _source_of(path: Path, cache: Path) -> str:
    try:
        rel = path.resolve().relative_to(cache.resolve())
    except (ValueError, OSError):
        return "user"
    return rel.parts[0] if rel.parts and rel.parts[0] in ("bundled", "designer") else "user"


def list_routines(folder: str | os.PathLike | None = None, *, cache_dir: str | os.PathLike | None = None,
                  include_bundled: bool = True) -> list[dict]:
    """Routines available to the UI: the ErosLink CD's own first, then `folder` (non-recursive).

    Each entry is a dict:
      name, description   as ErosLink shows them
      source              "bundled"  - the CD's main routines (routines/*.elk, the ones PlaStim rates best)
                          "designer" - the CD's designer examples (routines/designer/*.elk)
                          "user"     - files in `folder`
      bundled             True only for source == "bundled"
      path                what load() takes.  A file can hold several routines; then path is
                          "<file>#<index>" so every entry has a distinct path
      file, index         the .elk file and the routine's position in it
      id                  "<source>/<file name>#<index>"
    Files with identical bytes are listed once (the CD copy wins), so a folder holding copies of CD
    routines doesn't double them.  A file that can't be read is listed with an "error" message and
    index None (load() would raise for it)."""
    cache = Path(cache_dir) if cache_dir is not None else default_cache_dir()
    dirs: list[tuple[str, Path]] = []
    if include_bundled:
        dirs += [("bundled", cache / "bundled"), ("designer", cache / "designer")]
    if folder:                   # None or "" = no folder of your own (Path("") would be the current folder)
        dirs.append(("user", Path(folder)))
    out: list[dict] = []
    seen: set[str] = set()
    for source, d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.elk"), key=lambda q: q.name.lower()):
            data = f.read_bytes()
            h = hashlib.sha1(data).hexdigest()
            if h in seen:
                continue
            seen.add(h)
            base = dict(source=source, bundled=source == "bundled", file=str(f))
            try:
                ctx = read_context(data)
            except Exception as e:                      # noqa: BLE001 - one bad file must not hide the rest
                out.append(dict(base, name=f.stem, description="", path=str(f), index=None,
                                id=f"{source}/{f.name}", error=str(e)))
                continue
            many = len(ctx.routines) > 1
            for i, r in enumerate(ctx.routines):
                out.append(dict(base, name=r.name, description=r.description, index=i,
                                path=f"{f}#{i}" if many else str(f), id=f"{source}/{f.name}#{i}",
                                file_routines=len(ctx.routines)))
    return out


def _split_ref(path: str | os.PathLike) -> tuple[Path, int | None]:
    s = str(path)
    if "#" in s:
        head, _, tail = s.rpartition("#")
        if tail.isdigit():
            return Path(head), int(tail)
    return Path(s), None


def load(path: str | os.PathLike, index: int | str | None = None) -> CompiledRoutine:
    """Compile one routine for ET312Engine.set_mode().  `path` is a .elk file or a list_routines()
    "path" ("<file>#<index>"); `index` (position or routine name) overrides the suffix; default 0."""
    p, ref_index = _split_ref(path)
    if index is None:
        index = ref_index if ref_index is not None else 0
    ctx = read_file(p)
    if isinstance(index, str):
        matches = [i for i, r in enumerate(ctx.routines) if r.name == index]
        if not matches:
            raise KeyError(f"{p.name}: no routine named {index!r}")
        index = matches[0]
    if not ctx.routines:
        raise ValueError(f"{p.name}: the file holds no routines")
    r = ctx.routines[index]
    cr = compile_routine(r)
    cr.path, cr.index = str(p), index
    cr.source = _source_of(p, default_cache_dir())
    return cr


__all__ = ["CompiledRoutine", "ContextFile", "Ingredient", "RoutineDef", "compile_routine", "decode_module",
           "default_cache_dir", "list_routines", "load", "lower_routine", "read_context", "read_file"]
