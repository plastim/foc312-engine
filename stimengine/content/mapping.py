"""Feature -> axis mapping layer for motion tracks — LIVE-tunable ("adjust the feel in depth").

One `MappingSet` = global macros + one `AxisMapping` per output axis. Tracks call `targets(features)` at
sample time; the ENGINE applies each axis's `slew_s` (rate limit) and `smooth_s` (EMA) at 60 Hz, so an
`update()` mid-playback changes targets atomically and the slew/smooth absorb the jump (no glitch).
Presets live in config/feel/<name>.toml.

Features (per sample, from tracks.py):
  energy        frame-diff top-5% energy (raw)
  energy_short  ~0.3 s EMA of energy
  energy_long   ~20 s EMA of energy ("scene intensity")
  stroke        0..1 leaky vertical-flow integrator
  hflow         horizontal flow mean (px/sample, signed)
  tempo_hz      stroke zero-crossing rate over ~3 s window
  cut           hard cut flag (bool)
"""
from __future__ import annotations

import copy
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

# Same limits as stimengine/control/tcode_axes.py defaults (and restim.ini) — the engine clamps again.
LIMITS: dict[str, tuple[float, float]] = {
    "volume": (0.0, 1.0),
    "alpha": (-1.0, 1.0),
    "beta": (-1.0, 1.0),
    "carrier_hz": (500.0, 2000.0),
    "pulse_hz": (0.0, 150.0),
    "pulse_width": (4.0, 20.0),
    "pulse_rise_ms": (2.0, 20.0),
}
FEATURES = ("energy", "energy_short", "energy_long", "stroke", "hflow", "vflow", "tempo_hz", "cut",
            "E", "M", "L", "phi", "drive", "max_ml",
            # orbit: phi is RADIANS (0..2pi) and must not be mapped through a 0..1 window (it saturated beta at +1
            # on PlaStim's first run). orbit_x/orbit_y = r*cos/sin(phi) with r = 0.3 + 0.5*M, in -1..1; "side" = the
            # per-source side-to-side feature (orbit for music/audio, horizontal flow for real scenes).
            "orbit_x", "orbit_y", "r", "side",
            # audio namespace (content/audio.py) — selectable as axis inputs directly
            "audio.energy", "audio.onset", "audio.tempo_hz", "audio.beat_phase", "audio.energy_10s",
            "audio.energy_60s", "audio.transition",
            # shape layer inputs (content/shape.py): band onsets + musical structure
            "onset_low", "onset_high", "bar_phase", "phrase_index",
            "audio.onset_low", "audio.onset_high", "audio.bar_phase", "audio.phrase_index")
AXES = tuple(LIMITS.keys())
SOURCES = ("auto", "motion", "audio")
# Cards are MOTION ONLY (notes/power.md Amendment 2): the axes / macros a card file carries and a card load touches.
CARD_AXES = ("alpha", "beta")
CARD_MACROS = ("position_scale", "variety", "shape_bias", "tempo_scale")


def _clip(v: float, lo: float, hi: float) -> float:
    if lo > hi:
        lo, hi = hi, lo
    return float(min(hi, max(lo, v)))


@dataclass
class AxisMapping:
    enabled: bool = False
    feature: str = "energy"
    in_min: float = 0.0        # feature window -> 0
    in_max: float = 1.0        # feature window -> 1
    out_min: float = 0.0       # axis value at 0 (clamped to LIMITS)
    out_max: float = 1.0       # axis value at 1
    gain: float = 1.0          # multiplies the normalized input (before curve)
    curve: float = 1.0         # exponent 0.3..3 on normalized input (1 = linear)
    invert: bool = False       # 1-x before the curve
    slew_s: float = 0.0        # engine: seconds to traverse |out_max-out_min|; 0 = instant
    smooth_s: float = 0.0      # engine: EMA time constant on the output; 0 = none
    floor: float | None = None  # minimum output while following (axis units); None = no floor
    source: str = "auto"        # which track feeds this axis: auto = the store's active source, or motion/audio

    def normalized(self, x: float) -> float:
        span = self.in_max - self.in_min
        n = (x - self.in_min) / span if abs(span) > 1e-9 else (1.0 if x >= self.in_max else 0.0)
        n = _clip(n * self.gain, 0.0, 1.0)
        if self.invert:
            n = 1.0 - n
        c = _clip(self.curve, 0.3, 3.0)
        return n ** c if c != 1.0 else n

    def map(self, x: float, axis: str, out_scale: float = 1.0) -> float:
        n = self.normalized(x)
        if axis in ("alpha", "beta"):
            v = (self.out_min + (self.out_max - self.out_min) * n) * out_scale   # bipolar: scale amplitude about 0
        else:
            out_max = self.out_min + (self.out_max - self.out_min) * out_scale  # unipolar: scale the top end
            v = self.out_min + (out_max - self.out_min) * n
        if self.floor is not None:
            v = max(v, self.floor) if self.out_max >= self.out_min else min(v, self.floor)
        return _clip(v, *LIMITS[axis])

    def validated(self, axis: str) -> "AxisMapping":
        lo, hi = LIMITS[axis]
        a = copy.copy(self)
        a.feature = a.feature if a.feature in FEATURES else "energy"
        a.out_min = _clip(float(a.out_min), lo, hi)
        a.out_max = _clip(float(a.out_max), lo, hi)
        a.gain = _clip(float(a.gain), 0.0, 10.0)
        a.curve = _clip(float(a.curve), 0.3, 3.0)
        a.slew_s = max(0.0, float(a.slew_s))
        a.smooth_s = max(0.0, float(a.smooth_s))
        if a.floor is not None:
            a.floor = _clip(float(a.floor), lo, hi)
        a.source = a.source if a.source in SOURCES else "auto"
        return a


def _defaults() -> dict[str, AxisMapping]:
    """Preset "kits": curves mined from the 62 human-authored script kits on dh (notes/script-kit-analysis.md).
    Inputs: E = scene-normalized energy, M = 10 s EMA of E, L = 60 s EMA of E, T = stroke tempo (Hz),
    S = stroke 0..1, phi = oscillator phase advancing at T Hz. The five parameter axes are SLOW envelopes
    (always ramped, never stepped) and volume is NEVER zeroed (kit band 0.52-0.95, median 0.80)."""
    return {
        # volume = 0.55 + 0.40*M, 10 s ramp-in (kits: tau ~90 s; we start faster and let PlaStim tune)
        "volume":        AxisMapping(True, "M", 0.0, 1.0, 0.55, 0.95, 1.0, 1.0, False, 0.0, 10.0, 0.55),
        # alpha = 2S - 1 (position tau 0.2 s)
        "alpha":         AxisMapping(True, "stroke", 0.0, 1.0, -1.0, 1.0, 1.0, 1.0, False, 0.0, 0.2, None),
        # beta = side-to-side: "side" = r*sin(phi) (r = 0.3 + 0.5*M) for music/audio, horizontal flow for real
        # scenes (sources.py decides per active source). Input window -1..1 -> out -1..1 (bipolar, scaled about 0).
        "beta":          AxisMapping(True, "side", -1.0, 1.0, -1.0, 1.0, 1.0, 1.0, False, 0.0, 0.2, None),
        # pulse_frequency = 50 + 85*(0.6*min(T/2.5,1) + 0.4*M), tau ~10 s  (feature "drive" = that blend)
        "pulse_hz":      AxisMapping(False, "drive", 0.0, 1.0, 50.0, 135.0, 1.0, 1.0, False, 0.0, 10.0, None),
        # pulse_width = 5.0 + 3.5*M (low confidence), tau ~30 s
        "pulse_width":   AxisMapping(False, "M", 0.0, 1.0, 5.0, 8.5, 1.0, 1.0, False, 0.0, 30.0, None),
        # pulse_rise_ms = 11 - 8*max(M, L), tau 30-60 s (intense = sharper)
        "pulse_rise_ms": AxisMapping(False, "max_ml", 0.0, 1.0, 11.0, 3.0, 1.0, 1.0, False, 0.0, 45.0, None),
        # carrier: PlaStim's own preference overrides the kit stats (kits ran 1130-1935 Hz, median 1550):
        # "usable band 600-1500 Hz, sweet spot ~1140 Hz, lower values sometimes feel really good, above ~1200 the
        #  sensation changes little" -> 600..1500, curve 0.74 puts the input midpoint at ~1140 Hz; slew <= 30 Hz/s
        # (slew_s is over the 1500 Hz LIMITS span -> 50 s). Disabled by default; static 790 Hz stays the non-follow value.
        "carrier_hz":    AxisMapping(False, "L", 0.0, 1.0, 600.0, 1500.0, 1.0, 0.74, False, 50.0, 30.0, None),
    }


@dataclass
class Macros:
    intensity: float = 1.0        # scales volume out_max (0..1)
    tempo_scale: float = 1.0      # scales tempo feature into pulse_hz (0.25..4)
    position_scale: float = 1.0   # scales alpha/beta amplitude (0..1)
    variety: float = 0.5          # shape layer: how readily the position shape changes at phrase boundaries (0..1)
    shape_bias: float = 0.0       # shape layer: -1 prefer linear strokes/sway ... +1 prefer orbits

    def validated(self) -> "Macros":
        return Macros(_clip(self.intensity, 0.0, 1.0), _clip(self.tempo_scale, 0.25, 4.0), _clip(self.position_scale, 0.0, 1.0),
                      _clip(self.variety, 0.0, 1.0), _clip(self.shape_bias, -1.0, 1.0))


SHAPE_MODES = ("auto", "stroke", "sway", "orbit", "diagonal", "figure8")


class MappingSet:
    """Thread-safe, atomically updatable mapping. `version` bumps on every update (for UI/engine)."""

    def __init__(self, axes: dict[str, AxisMapping] | None = None, macros: Macros | None = None, name: str = "default") -> None:
        self._lock = threading.Lock()
        self.axes: dict[str, AxisMapping] = axes or _defaults()
        self.macros = macros or Macros()
        self.name = name
        self.version = 0
        self.shape_mode = "auto"       # shape layer override: auto (feature-driven) or a pinned canonical shape
        self.moves = None              # content.moves.Moves when attached: its params ride along in presets ([moves])
        self.power = None              # content.power.Power when attached: [power.<axis>] tables in presets (never cards)

    # -- evaluation -------------------------------------------------------------------------------
    @staticmethod
    def _derive(features: dict, tempo_scale: float) -> dict:
        f = dict(features)
        M = float(f.get("M", 0.0) or 0.0)
        L = float(f.get("L", 0.0) or 0.0)
        T = float(f.get("tempo_hz", 0.0) or 0.0) * tempo_scale
        f["max_ml"] = max(M, L)
        f["drive"] = 0.6 * min(T / 2.5, 1.0) + 0.4 * M
        f["tempo_hz"] = T
        if "orbit_y" not in f:
            import math as _m
            phi = float(f.get("phi", 0.0) or 0.0)
            r = 0.3 + 0.5 * M
            f["r"] = r
            f["orbit_x"] = r * _m.cos(phi)
            f["orbit_y"] = r * _m.sin(phi)
        f.setdefault("side", f["orbit_y"])
        return f

    def targets(self, features: dict, sources: dict[str, dict] | None = None, active: str = "motion") -> dict[str, float]:
        """Enabled axes -> target values (clamped). Volume is the envelope 0..1 (already intensity-scaled).

        `features` is the canonical feature dict of the ACTIVE source. `sources` may give per-source canonical
        dicts ({"motion": {...}, "audio": {...}}) so an axis with `source="motion"|"audio"` reads that one instead
        (per-axis source override); an axis with `source="auto"` reads `features`. A missing override source
        falls back to `features`. `_hold` is taken from the active features (cut / transition)."""
        with self._lock:
            axes = self.axes
            m = self.macros
            f = self._derive(features, m.tempo_scale)
            per: dict[str, dict] = {}
            for name, feats in (sources or {}).items():
                if feats:
                    per[name] = self._derive(feats, m.tempo_scale)
            out: dict[str, float] = {}
            for axis, a in axes.items():
                if not a.enabled:
                    continue
                fa = per.get(a.source, f) if a.source != "auto" else f
                M = float(fa.get("M", 0.0) or 0.0)
                if a.feature == "phi":
                    # legacy presets: "phi" meant the orbit; read the derived orbit_y (-1..1) with a -1..1 window
                    x = float(fa.get("orbit_y", 0.0) or 0.0)
                    a = copy.copy(a)
                    a.in_min, a.in_max = -1.0, 1.0
                else:
                    x = float(fa.get(a.feature, 0.0) or 0.0)
                scale = m.intensity if axis == "volume" else (m.position_scale if axis in ("alpha", "beta") else 1.0)
                out[axis] = a.map(x, axis, scale)
            # kit rule: a cut HOLDS (never zero) -> the engine keeps the previous applied values when cut is set
            out["_hold"] = 1.0 if f.get("cut") else 0.0
            return out

    def slew(self, axis: str) -> float:
        with self._lock:
            return self.axes[axis].slew_s if axis in self.axes else 0.0

    def smooth(self, axis: str) -> float:
        with self._lock:
            return self.axes[axis].smooth_s if axis in self.axes else 0.0

    # -- serialization ----------------------------------------------------------------------------
    def to_dict(self) -> dict:
        with self._lock:
            return {
                "name": self.name, "version": self.version,
                "macros": asdict(self.macros),
                "axes": {k: asdict(v) for k, v in self.axes.items()},
                "limits": {k: list(v) for k, v in LIMITS.items()},
                "features": list(FEATURES),
                "sources": list(SOURCES),
                "shape": {"mode": self.shape_mode, "modes": list(SHAPE_MODES)},
            }

    @classmethod
    def from_dict(cls, d: dict, name: str | None = None) -> "MappingSet":
        ms = cls(name=name or str(d.get("name") or "custom"))
        ms.update(d, bump=False)
        return ms

    def update(self, partial: dict, bump: bool = True) -> dict:
        """Apply a partial {macros:{...}, axes:{axis:{field:value}}} atomically. Unknown keys ignored."""
        names = {f.name for f in fields(AxisMapping)}
        with self._lock:
            new_axes = {k: copy.copy(v) for k, v in self.axes.items()}
            for axis, spec in (partial.get("axes") or {}).items():
                if axis not in new_axes or not isinstance(spec, dict):
                    continue
                a = new_axes[axis]
                for k, v in spec.items():
                    if k in names and v is not None or (k == "floor"):
                        setattr(a, k, v)
                new_axes[axis] = a.validated(axis)
            mac = copy.copy(self.macros)
            for k, v in (partial.get("macros") or {}).items():
                if hasattr(mac, k) and v is not None:
                    setattr(mac, k, float(v))
            self.axes = new_axes
            self.macros = mac.validated()
            shape = partial.get("shape")
            mode = shape.get("mode") if isinstance(shape, dict) else partial.get("shape_mode")
            if mode is not None:
                if mode not in SHAPE_MODES:
                    raise ValueError(f"shape mode must be one of {SHAPE_MODES}")
                self.shape_mode = str(mode)
            if "name" in partial and partial["name"]:
                self.name = str(partial["name"])
            if bump:
                self.version += 1
        return self.to_dict()

    # -- presets (TOML) ---------------------------------------------------------------------------
    @staticmethod
    def preset_dir(root: str | Path = "config/feel") -> Path:
        p = Path(root)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @classmethod
    def list_presets(cls, root: str | Path = "config/feel") -> list[str]:
        return sorted(p.stem for p in cls.preset_dir(root).glob("*.toml"))

    def save_preset(self, name: str, root: str | Path = "config/feel", extra: dict | None = None,
                    motion_only: bool = False) -> Path:
        """Write the preset TOML. `extra` adds top-level scalars (cards: card=true, blurb="..."). The attached
        Moves' params (self.moves, content/moves.py) are written as a [moves] table. `motion_only` (cards,
        notes/power.md Amendment 2): just the position axes + position macros, and no [power] tables."""
        name = "".join(ch for ch in name if ch.isalnum() or ch in "-_").strip("-_") or "preset"
        d = self.to_dict()
        if motion_only:
            d["macros"] = {k: v for k, v in d["macros"].items() if k in CARD_MACROS}
            d["axes"] = {k: v for k, v in d["axes"].items() if k in CARD_AXES}
        lines = [f'name = "{name}"', f'shape_mode = "{d["shape"]["mode"]}"']
        for k, v in (extra or {}).items():
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, str):
                lines.append(f'{k} = "{v.replace(chr(34), chr(39))}"')
            else:
                lines.append(f"{k} = {float(v)}")
        lines += ["", "[macros]"]
        for k, v in d["macros"].items():
            lines.append(f"{k} = {float(v)}")
        for axis, a in d["axes"].items():
            lines += ["", f"[axes.{axis}]"]
            for k, v in a.items():
                if v is None:
                    continue
                if isinstance(v, bool):
                    lines.append(f"{k} = {'true' if v else 'false'}")
                elif isinstance(v, str):
                    lines.append(f'{k} = "{v}"')
                else:
                    lines.append(f"{k} = {float(v)}")
        text = "\n".join(lines) + "\n"
        if self.moves is not None:
            from .moves import moves_toml
            text += moves_toml(self.moves.params)
        if self.power is not None and not motion_only:
            from .power import power_toml
            text += power_toml(self.power.params_dict())
        p = self.preset_dir(root) / f"{name}.toml"
        p.write_text(text, encoding="utf-8")
        with self._lock:
            self.name = name
        return p

    def load_preset(self, name: str, root: str | Path = "config/feel") -> dict:
        import tomllib
        p = self.preset_dir(root) / f"{name}.toml"
        d = tomllib.loads(p.read_text(encoding="utf-8"))
        # floor omitted in file => None
        for axis, spec in (d.get("axes") or {}).items():
            if isinstance(spec, dict) and "floor" not in spec:
                spec["floor"] = None
        d["name"] = name
        out = self.update(d)
        if self.moves is not None and isinstance(d.get("moves"), dict):
            self.moves.update(d["moves"])
            self.moves.clear_offsets()   # a preset/card change ends the Learn-panel step offsets
        if self.power is not None and isinstance(d.get("power"), dict):
            self.power.update_all({ax: v for ax, v in d["power"].items() if isinstance(v, dict)})
        return out

    @classmethod
    def from_config(cls, config: dict | None, moves=None) -> "MappingSet":
        """Startup: `[content.motion.map]` partial overrides, or `[content.motion] preset = "name"`."""
        ms = cls()
        ms.moves = moves
        mo = ((config or {}).get("content") or {}).get("motion") or {}
        preset = mo.get("preset")
        if preset:
            try:
                ms.load_preset(str(preset), mo.get("preset_dir", "config/feel"))
            except (OSError, ValueError):
                pass
        if mo.get("map"):
            ms.update({"axes": mo["map"], "macros": mo.get("macros") or {}}, bump=False)
        return ms


def write_builtin_presets(root: str | Path = "config/feel") -> list[Path]:
    """Ship kits (= default), gentle and driving (scaled variants of kits)."""
    out = []
    kits = MappingSet(name="kits")
    out.append(kits.save_preset("kits", root))
    out.append(MappingSet(name="default").save_preset("default", root))
    gentle = MappingSet(name="gentle")
    gentle.update({"macros": {"intensity": 0.7, "position_scale": 0.6},
                   "axes": {"volume": {"out_min": 0.45, "out_max": 0.80, "floor": 0.45, "smooth_s": 20.0},
                            "alpha": {"smooth_s": 0.4}, "beta": {"smooth_s": 0.4}}}, bump=False)
    out.append(gentle.save_preset("gentle", root))
    driving = MappingSet(name="driving")
    driving.update({"macros": {"intensity": 1.0, "tempo_scale": 1.3, "position_scale": 1.0},
                    "axes": {"volume": {"out_min": 0.60, "out_max": 1.0, "floor": 0.60, "smooth_s": 5.0},
                             "pulse_hz": {"enabled": True, "smooth_s": 5.0},
                             "pulse_width": {"enabled": True},
                             "pulse_rise_ms": {"enabled": True, "smooth_s": 20.0}}}, bump=False)
    out.append(driving.save_preset("driving", root))
    return out


def write_builtin_cards(root: str | Path = "config/feel/cards", base: str = "default-0820",
                        base_root: str | Path = "config/feel") -> list[Path]:
    """The Learn-panel cards (notes/moves.md) — deliberately far apart, tuned for distinguishability.
    Cards are MOTION ONLY (notes/power.md Amendment 2): position axes, shape, position macros and [moves]. Volume /
    pulse / carrier / width are owned by the Power lanes and the step buttons, never by a card. Each card starts
    from PlaStim's tuned preset (`base`) for the position axes."""
    from .moves import Moves, MovesParams

    def make(name: str) -> MappingSet:
        ms = MappingSet(name=name)
        ms.moves = Moves(MovesParams())
        try:
            ms.load_preset(base, base_root)
        except (OSError, ValueError):
            pass
        ms.name = name
        return ms

    def save(ms: MappingSet, name: str, blurb: str) -> Path:
        return ms.save_preset(name, root, {"card": True, "blurb": blurb}, motion_only=True)

    out = []
    # 1. Wash: center-heavy, slides, no jumps
    ms = make("wash")
    ms.moves.update({"edge_time": 0.15, "transit": "slide", "path": "through", "dwell_ms": 300.0, "dwell_style": "still",
                     "dip_depth": 0.0, "fade_fight_s": 0.0, "compose": 0.0, "beat_lock": "off",
                     "quant": {"pulse_hz": {"on": "any"}, "carrier_hz": {"on": "any"}, "volume": {"step": 0.0}}})
    out.append(save(ms, "wash", "Center-heavy wash. Slides, no jumps, no dips."))
    # 2. Corners: on the rim, around the rim, still dwell, jumps on onsets
    ms = make("corners")
    ms.moves.update({"edge_time": 0.7, "path": "around", "dwell_ms": 800.0, "dwell_style": "still", "transit": "jump",
                     "jump_ratio": 1.0, "compose": 0.5, "dip_depth": 0.15, "fade_fight_s": 0.0, "beat_lock": "off",
                     "quant": {"pulse_hz": {"on": "any"}, "carrier_hz": {"on": "any"}, "alpha": {"hold_ms": 800.0, "on": "onset"}}})
    out.append(save(ms, "corners", "Lives on the rim. Jumps corner to corner on onsets, parks 0.8 s."))
    # 3. Drive: mixed slides and jumps along the rim, shimmer dwell
    ms = make("drive")
    ms.moves.update({"edge_time": 0.5, "transit": "mix", "jump_ratio": 0.5, "path": "around", "dwell_ms": 500.0,
                     "dwell_style": "shimmer", "dip_depth": 0.25, "dip_ms": 300.0, "dip_trigger": 0.08,
                     "fade_fight_s": 0.0, "compose": 0.3, "carrier_low_weight": 0.6, "beat_lock": "off",
                     "quant": {"volume": {"step": 0.10, "hold_ms": 400.0, "on": "beat"},
                               "pulse_hz": {"step": 10.0, "hold_ms": 800.0, "on": "onset"},
                               "carrier_hz": {"step": 100.0, "hold_ms": 2000.0, "on": "onset"}}})
    out.append(save(ms, "drive", "Half slides, half jumps along the rim, shimmer dwell 0.5 s."))
    # 4. Slow burn: long holds, slow slides, drift dwell
    ms = make("slow-burn")
    ms.moves.update({"edge_time": 0.5, "transit": "slide", "path": "around", "dwell_ms": 1500.0, "dwell_style": "drift",
                     "dip_depth": 0.2, "dip_ms": 400.0, "fade_fight_s": 45.0, "compose": 0.2, "carrier_low_weight": 0.3,
                     "beat_lock": "off",
                     "quant": {"volume": {"step": 0.0, "hold_ms": 3000.0, "on": "any"},
                               "pulse_hz": {"step": 10.0, "hold_ms": 3000.0, "on": "any"},
                               "carrier_hz": {"step": 100.0, "hold_ms": 3000.0, "on": "any"}}})
    out.append(save(ms, "slow-burn", "Long holds (3 s), slow slides that drift along the rim."))
    # 5. Jumpy: jumps only, Moves composes the spots, shimmer dwell
    ms = make("jumpy")
    ms.update({"axes": {"alpha": {"smooth_s": 0.05}, "beta": {"smooth_s": 0.05}}}, bump=False)
    ms.moves.update({"edge_time": 0.6, "transit": "jump", "jump_ratio": 1.0, "compose": 0.9, "path": "around",
                     "dwell_ms": 400.0, "dwell_style": "shimmer", "dip_depth": 0.25, "dip_ms": 200.0, "fade_fight_s": 0.0,
                     "carrier_low_weight": 0.5, "beat_lock": "off",
                     "quant": {"pulse_hz": {"step": 10.0, "hold_ms": 300.0, "on": "onset"},
                               "carrier_hz": {"step": 100.0, "hold_ms": 4000.0, "on": "any"},
                               "alpha": {"hold_ms": 300.0, "on": "onset"}}})
    out.append(save(ms, "jumpy", "Jumps only, Moves picks the spots. Shimmer dwell."))
    # 6. Random: beat-locked; each beat (or onset when there is no trusted beat) jumps to a random rim point >= 90 deg away
    ms = make("random")
    ms.update({"axes": {"alpha": {"smooth_s": 0.05}, "beta": {"smooth_s": 0.05}}}, bump=False)
    ms.moves.update({"edge_time": 0.8, "transit": "jump", "jump_ratio": 1.0, "path": "around", "dwell_ms": 400.0,
                     "dwell_style": "shimmer", "dip_depth": 0.0, "fade_fight_s": 0.0, "compose": 0.5,
                     "beat_lock": "random", "beat_div": 1.0, "bar_accent": False})
    out.append(save(ms, "random", "Beat-locked. Every beat (every onset without a beat) jumps to a random rim point at least 90 deg away, shimmer between."))
    return out


# Backwards-compatible alias used by tracks.py
MotionMapping = MappingSet
