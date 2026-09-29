"""Moves layer — makes changes FELT (notes/moves.md, 2026-08-25).

Sits between the mapping's `targets` and the engine's slew/smooth in `Engine._follow_apply`:

    mapping targets -> Moves.apply(targets, features, now, dt) -> slew/smooth -> frame

Why: small continuous changes are not felt (Weber ~5-15% JND plus adaptation on slow ramps); position spends too
long in the diffuse center instead of the localized rim. So Moves (1) quantizes pulse/carrier/volume to felt steps
and only lets them change on events (onset/beat/cut) after a hold, (2) remaps the radius r' = r^gamma with gamma
adapted online so a target fraction of the time is spent on the rim, (3) dwells on arrival at the rim (still /
shimmer / drift), (4) transits rim->rim along the rim (path=around) or by jumps on events, (5) dips before a volume
rise and fights adaptation (fade-fight) — both only ever LOWER volume, and (6) can compose its own values (rim
spokes, grid values) blended with the mapped ones.

Pure and unit-testable: no engine imports, no clock of its own (the engine passes `now`/`dt`). Everything is
clamped to mapping LIMITS, and the volume out never exceeds the mapping's volume for the tick (plus the user's
explicit step offset, which is a deliberate act equivalent to editing the mapping).
"""
from __future__ import annotations

import copy
import math
import random
import threading
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from .mapping import LIMITS
from .points import PointMap

RIM_R = 0.8                    # "on the rim" = r >= RIM_R (contract: edge_time counts r >= 0.8)
CARRIER_GRID = (700.0, 1600.0)  # felt carrier band, 100 Hz grid inside it
PULSE_BAND = (50.0, 135.0)      # compose picks pulse grid values inside this band
SPOKES = 8                      # compose rim points
EDGE_MEAS_TAU_S = 5.0           # controller measurement EMA
EDGE_READ_TAU_S = 60.0          # readout EMA ("edge_frac_60s")
GAMMA_TAU_S = 20.0              # integral controller: a 0.25 fraction error moves log(gamma) by 1 per tau
GAMMA_LIMITS = (0.05, 4.0)
SLIDE_RATE = 6.0                # pad units/s for Moves' own position dynamics (fast vs the mapping's 0.2 s EMA)
ANGULAR_AROUND_MIN = 0.35       # rad: rim->rim moves shorter than this just slide straight
ONSET_REFRACTORY_S = 0.15
ON_MODES = ("any", "onset", "beat", "cut")
DWELL_STYLES = ("still", "shimmer", "drift")
TRANSITS = ("slide", "jump", "mix")
BEAT_LOCKS = ("off", "step", "bounce", "orbit", "random")
RANDOM_MIN_TURN = math.pi / 2   # random: every jump lands >= 90 deg from the last point
BEAT_DIVS = (0.5, 1.0, 2.0, 4.0)
PATHS = ("through", "around")
QUANT_AXES = ("pulse_hz", "carrier_hz", "volume", "alpha", "beta")
STEP_AXES = ("pulse_hz", "carrier_hz", "volume", "edge_time")
STEP_SIZE = {"pulse_hz": 10.0, "carrier_hz": 100.0, "volume": 0.10, "edge_time": 0.25}


def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


@dataclass
class QuantSpec:
    step: float = 0.0        # 0 = off (pass-through)
    hold_ms: float = 800.0   # minimum time between changes
    on: str = "onset"        # when a change is allowed: any | onset | beat | cut

    def validated(self) -> "QuantSpec":
        return QuantSpec(max(0.0, float(self.step)), max(0.0, float(self.hold_ms)),
                         self.on if self.on in ON_MODES else "onset")


def _default_quant() -> dict[str, QuantSpec]:
    return {
        "pulse_hz": QuantSpec(10.0, 800.0, "onset"),
        "carrier_hz": QuantSpec(100.0, 800.0, "onset"),
        "volume": QuantSpec(0.0, 800.0, "onset"),
        "alpha": QuantSpec(0.0, 800.0, "onset"),
        "beta": QuantSpec(0.0, 800.0, "onset"),
    }


@dataclass
class MovesParams:
    enabled: bool = True
    quant: dict[str, QuantSpec] = field(default_factory=_default_quant)
    carrier_low_weight: float = 0.6   # 0 = uniform over the grid, 1 = strongly prefer the low end
    edge_time: float = 0.5            # target fraction of time with r >= RIM_R
    dwell_ms: float = 600.0
    dwell_style: str = "shimmer"      # still | shimmer | drift
    transit: str = "mix"              # slide | jump | mix
    jump_ratio: float = 0.5           # mix: probability that an event jumps (else slides)
    path: str = "around"              # through | around (rim->rim moves follow the rim)
    dip_depth: float = 0.25           # before a volume rise >= dip_trigger, dip by this fraction ...
    dip_ms: float = 300.0             # ... for this long
    dip_trigger: float = 0.12
    fade_fight_s: float = 45.0        # 0 = off; after this long within +-5% of one volume: -8% over 2 s, back over 3 s
    compose: float = 0.5              # 0 = features drive values; 1 = features only decide WHEN, Moves picks WHAT
    # "motion to the beat, if there is a beat" (PlaStim 2026-08-25). Only engages while the source reports a trusted
    # tempo (tempo_hz > 0 with a beat_phase); otherwise the card's normal motion runs.
    beat_lock: str = "off"            # off | step (new rim point per beat, hold between) | bounce (opposite rim
                                      # points alternate = a stroke per beat, axis turns each bar) | orbit (one
                                      # rim revolution per beat) | random (a random rim point >= 90 deg away per
                                      # beat — or per onset when there is no trusted beat)
    beat_div: float = 1.0             # beats per move: 0.5 = twice per beat, 1, 2, 4
    bar_accent: bool = True           # downbeat: step/bounce hit the CENTER for a moment, orbit gets a bigger radius
    # Point map (content/points.py, PlaStim 2026-09-05): rim/centre picks weighted by which spots feel good.
    point_map: bool = True            # compose picks + beat step/random sample candidates by weight (else uniform)
    tune: bool = False                # tune mode: walk every candidate, hold each tune_hold_ms, ignore beats
    tune_hold_ms: float = 2500.0      # minimum hold per candidate; moves on the next onset after that (1.5x = overdue)

    def validated(self) -> "MovesParams":
        p = copy.copy(self)
        p.enabled = bool(p.enabled)
        q = _default_quant()
        for k, v in (p.quant or {}).items():
            if k in q:
                q[k] = (v if isinstance(v, QuantSpec) else QuantSpec(**{kk: vv for kk, vv in dict(v).items()
                                                                     if kk in ("step", "hold_ms", "on")})).validated()
        p.quant = q
        p.carrier_low_weight = _clip(float(p.carrier_low_weight), 0.0, 1.0)
        p.edge_time = _clip(float(p.edge_time), 0.0, 1.0)
        p.dwell_ms = max(0.0, float(p.dwell_ms))
        p.dwell_style = p.dwell_style if p.dwell_style in DWELL_STYLES else "shimmer"
        p.transit = p.transit if p.transit in TRANSITS else "mix"
        p.jump_ratio = _clip(float(p.jump_ratio), 0.0, 1.0)
        p.path = p.path if p.path in PATHS else "around"
        p.dip_depth = _clip(float(p.dip_depth), 0.0, 0.9)
        p.dip_ms = max(0.0, float(p.dip_ms))
        p.dip_trigger = _clip(float(p.dip_trigger), 0.0, 1.0)
        p.fade_fight_s = max(0.0, float(p.fade_fight_s))
        p.compose = _clip(float(p.compose), 0.0, 1.0)
        p.beat_lock = p.beat_lock if p.beat_lock in BEAT_LOCKS else "off"
        try:
            bd = float(p.beat_div)
        except (TypeError, ValueError):
            bd = 1.0
        p.beat_div = min(BEAT_DIVS, key=lambda x: abs(x - bd))
        p.bar_accent = bool(p.bar_accent)
        p.point_map = bool(p.point_map)
        p.tune = bool(p.tune)
        p.tune_hold_ms = _clip(float(p.tune_hold_ms), 500.0, 20000.0)
        return p

    def to_dict(self) -> dict:
        d = asdict(self)
        d["quant"] = {k: asdict(v) for k, v in self.quant.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict | None) -> "MovesParams":
        return cls().updated(d or {})

    def updated(self, partial: dict) -> "MovesParams":
        """Partial update (same shape as to_dict); unknown keys ignored. Returns a NEW validated params."""
        p = copy.copy(self)
        p.quant = {k: copy.copy(v) for k, v in self.quant.items()}
        names = {f.name for f in fields(MovesParams)}
        for k, v in (partial or {}).items():
            if k == "quant" and isinstance(v, dict):
                for axis, spec in v.items():
                    if axis in p.quant and isinstance(spec, dict):
                        q = copy.copy(p.quant[axis])
                        for kk, vv in spec.items():
                            if kk in ("step", "hold_ms", "on") and vv is not None:
                                setattr(q, kk, vv)
                        p.quant[axis] = q
            elif k in names and k != "quant" and v is not None:
                setattr(p, k, v)
        return p.validated()


def snap_carrier(hz: float, low_weight: float = 0.0) -> float:
    """Snap a carrier request onto the 700..1600 / 100 Hz grid, biased toward the low end by `low_weight`."""
    lo, hi = CARRIER_GRID
    n = _clip((float(hz) - lo) / (hi - lo), 0.0, 1.0)
    if low_weight > 0:
        n = n ** (1.0 + 2.0 * low_weight)      # w=0.6 -> n^2.2: the input midpoint lands at ~920 Hz
    return lo + round(n * (hi - lo) / 100.0) * 100.0


def _quantize(v: float, step: float) -> float:
    return round(v / step) * step if step > 0 else v


def _polar(a: float, b: float) -> tuple[float, float]:
    return math.hypot(a, b), math.atan2(b, a)


def _cart(r: float, th: float) -> tuple[float, float]:
    return r * math.cos(th), r * math.sin(th)


def _wrap(th: float) -> float:
    return (th + math.pi) % (2 * math.pi) - math.pi


class Moves:
    """Live-tunable, thread-safe params; per-tick `apply()`; `readout()` for the UI; `step()`/`demo()` offsets."""

    def __init__(self, params: MovesParams | None = None, seed: int | None = None) -> None:
        self._lock = threading.Lock()
        self.params = (params or MovesParams()).validated()
        self.version = 0
        self._rng = random.Random(seed)
        self.points = PointMap(seed=seed)     # the engine may swap in its own (shared with the API)
        self.reset()

    # ---- state ----------------------------------------------------------------------------------
    def reset(self) -> None:
        self._held: dict[str, float] = {}        # quantized value currently held per axis
        self._held_since: dict[str, float] = {}
        self._prev_E: float | None = None
        self._E_ema: float | None = None
        self._prev_onset = 0.0
        self._prev_phi: float | None = None
        self._last_onset_t = -1e9
        self._events: dict[str, bool] = {"onset": False, "beat": False, "cut": False, "any": True}
        self._features: dict = {}
        self.gamma = 1.0
        self._edge_meas = 0.5
        self._edge_read = 0.5
        self._edge_init = False
        self._pos: tuple[float, float] | None = None    # Moves' own output position
        self._dwell_until = -1e9
        self._dwell_anchor: tuple[float, float] | None = None
        self._was_rim = False
        self._moving = False
        self._center_until = -1e9
        self._chosen: dict[str, float] = {}      # compose picks (persist until the next event)
        self._chosen_pos: tuple[float, float] | None = None
        self._vol_base: float | None = None      # last volume we committed to (pre dip/fade shaping)
        self._dip_until = -1e9
        self._dip_level = 0.0
        self._dip_pending: float | None = None
        self._ff_anchor: float | None = None
        self._ff_since = 0.0
        self._ff_start = -1e9
        self.offsets: dict[str, float] = {k: 0.0 for k in STEP_AXES}
        self._demo: dict | None = None
        self.stats = {"dips": 0, "jumps": 0, "beats": 0}
        # beat lock state
        self._beat_locked = False
        self._beat_why: str | None = "off"
        self._beat_bpm = 0.0
        self._beat_count = 0            # beats seen (for beat_div > 1)
        self._beat_half_prev = 0.0      # beat_phase last tick (for beat_div 0.5: fire at the half-beat too)
        self._beat_spoke = 0            # step: current spoke index
        self._beat_axis = 0.0           # bounce: axis angle (radians), turns each bar
        self._beat_side = 1             # bounce: +1 / -1
        self._beat_target: tuple[float, float] | None = None
        self._beat_angle: float | None = None   # random: angle of the last rim point
        self._beat_center_until = 0.0
        self._prev_bar_phase: float | None = None
        self.last_move: dict | None = None
        # point map / tune state
        self._last_point: str | None = None     # last candidate a map pick landed on
        self._tune_order: list[str] = []
        self._tune_idx = -1
        self._tune_since = -1e9
        self._tune_cycles = 0

    # ---- params ---------------------------------------------------------------------------------
    def to_dict(self) -> dict:
        with self._lock:
            d = self.params.to_dict()
            d["version"] = self.version
            d["offsets"] = dict(self.offsets)
            d["options"] = {"on": list(ON_MODES), "dwell_style": list(DWELL_STYLES), "transit": list(TRANSITS),
                            "path": list(PATHS), "carrier_grid": list(CARRIER_GRID)}
            return d

    def update(self, partial: dict, bump: bool = True) -> dict:
        with self._lock:
            self.params = self.params.updated(partial)
            if bump:
                self.version += 1
        return self.to_dict()

    def set_params(self, params: MovesParams, bump: bool = True) -> None:
        with self._lock:
            self.params = params.validated()
            if bump:
                self.version += 1

    def clear_offsets(self) -> None:
        """Card/preset change: drop step offsets AND the quantizer's held values so the new card applies now."""
        with self._lock:
            self.offsets = {k: 0.0 for k in STEP_AXES}
            self._held = {}
            self._held_since = {}

    # ---- Learn panel: step / demo ---------------------------------------------------------------
    def step(self, axis: str, direction: int, current: float | None = None) -> float:
        """One felt step on `axis`. Adds to the running offset (applied on top of the mapped value) and returns
        the new offset — the caller (engine/API) resolves the absolute value. `current` (absolute) lets the
        offset be clamped so the result stays inside LIMITS."""
        if axis not in STEP_AXES:
            raise ValueError(f"axis must be one of {STEP_AXES}")
        d = 1 if int(direction) >= 0 else -1
        size = STEP_SIZE[axis]
        with self._lock:
            if axis == "edge_time":
                self.params = self.params.updated({"edge_time": self.params.edge_time + d * size})
                self.version += 1
                return self.params.edge_time
            off = self.offsets[axis] + d * size
            if current is not None:
                lo, hi = LIMITS[axis] if axis in LIMITS else (0.0, 1.0)
                if axis == "carrier_hz":
                    lo, hi = CARRIER_GRID
                off = _clip(off, lo - (current - self.offsets[axis]), hi - (current - self.offsets[axis]))
            self.offsets[axis] = off
            self.last_move = {"axis": axis, "from": None, "to": off, "t": None, "why": "step"}
            return off

    def start_demo(self, axis: str, now: float, current: float, seconds: float = 6.0) -> dict:
        """Swing `axis` min -> max -> current over `seconds` (pure schedule; the caller applies demo_value())."""
        if axis not in STEP_AXES:
            raise ValueError(f"axis must be one of {STEP_AXES}")
        lo, hi = {"pulse_hz": PULSE_BAND, "carrier_hz": CARRIER_GRID, "volume": (0.0, 1.0),
                  "edge_time": (0.0, 1.0)}[axis]
        with self._lock:
            self._demo = {"axis": axis, "t0": now, "seconds": float(seconds), "lo": lo, "hi": hi,
                          "current": float(current)}
            return dict(self._demo)

    def demo_value(self, now: float) -> tuple[str, float] | None:
        """(axis, value) the demo wants right now, or None when no demo is running (it clears itself)."""
        with self._lock:
            return self._demo_value(now)

    def _demo_value(self, now: float) -> tuple[str, float] | None:
        if True:
            d = self._demo
            if d is None:
                return None
            p = (now - d["t0"]) / d["seconds"]
            if p >= 1.0:
                self._demo = None
                return d["axis"], d["current"]
            lo, hi, cur = d["lo"], d["hi"], d["current"]
            if p < 1 / 3:
                v = cur + (lo - cur) * (p * 3)
            elif p < 2 / 3:
                v = lo + (hi - lo) * ((p - 1 / 3) * 3)
            else:
                v = hi + (cur - hi) * ((p - 2 / 3) * 3)
            if d["axis"] == "carrier_hz":
                v = snap_carrier(v)
            elif d["axis"] == "pulse_hz":
                v = _quantize(v, 10.0)
            return d["axis"], v

    @property
    def demo_running(self) -> bool:
        return self._demo is not None

    # ---- events ---------------------------------------------------------------------------------
    def _detect_events(self, f: dict, now: float, dt: float) -> dict[str, bool]:
        cut = bool(f.get("cut"))
        onset = False
        raw = f.get("onset")
        if raw is not None:
            try:
                o = float(raw or 0.0)
            except (TypeError, ValueError):
                o = 0.0
            onset = o >= 1.0 and self._prev_onset < 1.0      # audio onset strength (0..4), rising edge
            self._prev_onset = o
        E = f.get("E", f.get("energy_short"))
        if E is not None:
            try:
                E = float(E or 0.0)
            except (TypeError, ValueError):
                E = 0.0
            if self._E_ema is None:
                self._E_ema = E
            thr = self._E_ema + 0.05
            if self._prev_E is not None and self._prev_E < thr <= E:
                onset = True
            self._prev_E = E
            self._E_ema += min(1.0, dt / 2.0) * (E - self._E_ema)
        if onset and now - self._last_onset_t < ONSET_REFRACTORY_S:
            onset = False
        if onset:
            self._last_onset_t = now
        beat = bool(f.get("beat"))
        phi = f.get("beat_phase")
        if phi is None:
            phi = f.get("audio.beat_phase")
        if phi is None:
            phi = f.get("phi")
        if phi is not None:
            try:
                phi = float(phi or 0.0)
            except (TypeError, ValueError):
                phi = 0.0
            if self._prev_phi is not None and phi < self._prev_phi - 0.5 * (2 * math.pi if phi > 1.5 or self._prev_phi > 1.5 else 1.0):
                beat = True
            self._prev_phi = phi
        if cut:
            onset = True   # a cut is the strongest onset there is
        return {"onset": onset, "beat": beat, "cut": cut, "any": True}

    def _allowed(self, axis: str, now: float) -> bool:
        q = self.params.quant.get(axis) or QuantSpec()
        since = self._held_since.get(axis)
        if since is not None and (now - since) * 1000.0 < q.hold_ms:
            return False
        return bool(self._events.get(q.on, True))

    # ---- quantizer ------------------------------------------------------------------------------
    def _quant_axis(self, axis: str, target: float, now: float) -> float:
        q = self.params.quant.get(axis) or QuantSpec()
        if axis == "carrier_hz":
            # pure grid snap: the low-end bias is for Moves' OWN picks (_pick), never for an explicit request —
            # with the bias here a card asking for 900 came out as 700 (PlaStim 2026-08-25)
            want = snap_carrier(target, 0.0)
        elif q.step <= 0:
            return target
        else:
            want = _quantize(target, q.step)
        lo, hi = LIMITS[axis]
        want = _clip(want, lo, hi)
        held = self._held.get(axis)
        if held is None:
            self._held[axis] = want
            self._held_since[axis] = now
            return want
        if want != held:
            # "overdue": the mapping has wanted another step for a long time and no event ever came (motion source
            # with no onsets held the carrier at 700 while the card asked for 900 — PlaStim 2026-08-25). Let it through
            # after 4x the hold time (min 3 s) even without an event.
            since = self._held_since.get(axis, now)
            overdue = (now - since) >= max(3.0, 4.0 * q.hold_ms / 1000.0)
            if self._allowed(axis, now) or overdue:
                self.last_move = {"axis": axis, "from": round(held, 4), "to": round(want, 4), "t": now,
                                  "why": (q.on if not overdue or self._allowed(axis, now) else "overdue")}
                self._held[axis] = want
                self._held_since[axis] = now
        return self._held[axis]

    # ---- compose picks --------------------------------------------------------------------------
    def _pick(self, axis: str) -> float:
        if axis == "carrier_hz":
            lo, hi = CARRIER_GRID
            grid = [lo + 100.0 * i for i in range(int((hi - lo) / 100.0) + 1)]
            w = self.params.carrier_low_weight
            weights = [(1.0 - w) + w * (1.0 - i / (len(grid) - 1)) ** 2 * 2.0 for i in range(len(grid))]
            return self._rng.choices(grid, weights=weights)[0]
        if axis == "pulse_hz":
            lo, hi = PULSE_BAND
            return _quantize(self._rng.uniform(lo, hi), 10.0)
        if axis == "volume":
            return self._rng.uniform(0.0, 1.0)
        return 0.0

    def _pick_pos(self) -> tuple[float, float]:
        if self.params.point_map and self.points is not None and self.points.enabled:
            name = self.points.pick(exclude=self._last_point)
            self._last_point = name
            if name == "center":
                self._center_until = self._now + self.params.dwell_ms / 1000.0
            return self.points.pos(name)
        if self._rng.random() < 1.0 / SPOKES:
            self._center_until = self._now + self.params.dwell_ms / 1000.0
            return 0.0, 0.0
        k = self._rng.randrange(SPOKES)
        return _cart(1.0, k * 2 * math.pi / SPOKES)

    def _map_rim_spoke(self, from_angle: float | None) -> int | None:
        """Point-map pick among rim spokes >= 90 deg from `from_angle` (all 8 when None). None = map off."""
        if not (self.params.point_map and self.points is not None and self.points.enabled):
            return None
        names = []
        for k in range(SPOKES):
            th = k * 2 * math.pi / SPOKES
            if from_angle is None or abs(_wrap(th - from_angle)) >= RANDOM_MIN_TURN - 1e-6:
                names.append(f"rim{k}")
        ws = [self.points.weights[n] for n in names]
        if self._rng.random() < self.points.explore or sum(ws) <= 1e-9:
            name = self._rng.choice(names)
        else:
            name = self._rng.choices(names, weights=ws)[0]
        self._last_point = name
        return int(name[3:])

    # ---- tune mode: walk every candidate, hold each, move on the onset after the hold -----------
    def _tune_position(self, now: float, onset: bool) -> tuple[float, float]:
        p = self.params
        hold = p.tune_hold_ms / 1000.0
        held = now - self._tune_since
        advance = self._tune_idx < 0 or (held >= hold and (onset or held >= 1.5 * hold))
        if advance:
            if self._tune_idx < 0 or self._tune_idx + 1 >= len(self._tune_order):
                self._tune_order = self.points.shuffled() if self.points is not None else ["center"]
                if self._tune_idx >= 0:
                    self._tune_cycles += 1
                self._tune_idx = 0
            else:
                self._tune_idx += 1
            self._tune_since = now
            name = self._tune_order[self._tune_idx]
            tgt = self.points.pos(name) if self.points is not None else (0.0, 0.0)
            self.last_move = {"axis": "position", "from": list(self._pos or (0.0, 0.0)),
                              "to": [round(tgt[0], 3), round(tgt[1], 3)], "t": now, "why": "tune", "point": name}
            self._last_point = name
        name = self._tune_order[self._tune_idx]
        self._pos = self.points.pos(name) if self.points is not None else (0.0, 0.0)
        self._was_rim = math.hypot(*self._pos) >= RIM_R
        return self._pos

    def tune_position(self, now: float) -> tuple[float, float] | None:
        """Idle-path entry (no follow running): the tune walk still advances (on the 1.5x hold, no onsets)."""
        with self._lock:
            if not self.params.tune:
                return None
            self._now = now
            return self._tune_position(now, onset=False)

    def tune_state(self) -> dict:
        p = self.params
        name = self._tune_order[self._tune_idx] if 0 <= self._tune_idx < len(self._tune_order) else None
        now = getattr(self, "_now", 0.0)
        return {"on": p.tune, "hold_ms": p.tune_hold_ms, "point": name,
                "held_ms": int((now - self._tune_since) * 1000.0) if name else 0,
                "idx": self._tune_idx + 1 if name else 0, "total": len(self._tune_order), "cycles": self._tune_cycles}

    # ---- volume shaping (only ever lowers) ------------------------------------------------------
    def _shape_volume(self, v: float, now: float, dt: float) -> float:
        """Dip-before-rise and fade-fight. Returns a value <= v, always."""
        p = self.params
        base = self._vol_base
        if base is None:
            self._vol_base = v
            self._ff_anchor, self._ff_since = v, now
            return v
        out = v
        if self._dip_pending is not None:
            if now < self._dip_until:
                out = min(v, self._dip_level)          # dipping: hold the low level until dip_ms is up
            else:
                self._dip_pending = None
                self._vol_base = v                     # the rise lands now
        elif v >= base + p.dip_trigger and p.dip_depth > 0 and p.dip_ms > 0:
            self._dip_pending = v
            self._dip_level = base * (1.0 - p.dip_depth)
            self._dip_until = now + p.dip_ms / 1000.0
            self.stats["dips"] += 1
            self.last_move = {"axis": "volume", "from": round(base, 3), "to": round(v, 3), "t": now, "why": "dip"}
            out = min(v, self._dip_level)
        elif v < base:
            self._vol_base = v                         # drops are followed at once
        else:
            self._vol_base = base + min(1.0, dt / 2.0) * (v - base)   # small rises: follow slowly (no dip)
        # fade-fight: too long within +-5% of one level -> ease down 8% over 2 s, back over 3 s
        if p.fade_fight_s > 0:
            if self._ff_anchor is None or abs(v - self._ff_anchor) > 0.05 * max(self._ff_anchor, 1e-6):
                self._ff_anchor, self._ff_since = v, now
            elif now - self._ff_since >= p.fade_fight_s and now >= self._ff_start + 5.0:
                self._ff_start = now
                self._ff_since = now + 5.0          # re-arm from the END of this fade
                self.stats["fades"] = self.stats.get("fades", 0) + 1
            t = now - self._ff_start
            if 0.0 <= t < 5.0:
                depth = 0.08 * (t / 2.0 if t < 2.0 else 1.0 - (t - 2.0) / 3.0)
                out = min(out, v * (1.0 - depth))
        return min(out, v)

    # ---- position -------------------------------------------------------------------------------
    def _update_gamma(self, r_out: float, dt: float) -> None:
        p = self.params
        on_rim = 1.0 if r_out >= RIM_R else 0.0
        if not self._edge_init:
            self._edge_meas = self._edge_read = on_rim
            self._edge_init = True
        self._edge_meas += min(1.0, dt / EDGE_MEAS_TAU_S) * (on_rim - self._edge_meas)
        self._edge_read += min(1.0, dt / EDGE_READ_TAU_S) * (on_rim - self._edge_read)
        target = _clip(p.edge_time + self.offsets.get("edge_time", 0.0), 0.0, 1.0)
        lg = math.log(self.gamma) + 4.0 * (self._edge_meas - target) * dt / GAMMA_TAU_S
        self.gamma = _clip(math.exp(lg), *GAMMA_LIMITS)

    def _beat_position(self, f: dict, now: float, dt: float) -> tuple[float, float] | None:
        """Beat-locked motion; None when off or when there is no trusted beat (caller falls back to normal)."""
        p = self.params
        if p.beat_lock == "off":
            self._beat_locked = False
            self._beat_why = "off"
            return None
        # canonical dicts carry the audio PLL phase as "audio.beat_phase" (0..1) and/or "phi" (radians);
        # a plain "beat_phase" key is accepted too (tests / other sources)
        raw = f.get("beat_phase")
        if raw is None:
            raw = f.get("audio.beat_phase")
        if raw is None and f.get("phi") is not None:
            raw = (float(f.get("phi") or 0.0) / (2 * math.pi)) % 1.0
        try:
            tempo = float(f.get("tempo_hz", 0.0) or 0.0)
            phase = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            tempo, phase = 0.0, None
        if phase is not None and phase > 1.5:  # radians -> 0..1
            phase = (phase / (2 * math.pi)) % 1.0
        stale_ms = f.get("audio.stale_ms")
        why = None
        if tempo <= 0.0:
            why = "no tempo"
        elif phase is None:
            why = "no beat phase"
        elif stale_ms is not None and float(stale_ms or 0) > 2000:
            why = "audio stale"
        self._beat_why = why
        if why is not None and p.beat_lock != "random":
            self._beat_locked = False
            self._beat_bpm = 0.0
            return None
        div = p.beat_div
        # --- when does a move fire? ---
        fire = False
        if why is not None:
            # random with no trusted beat: every onset is a beat (the no-beat path still moves)
            self._beat_locked = False
            self._beat_bpm = 0.0
            self._beat_why = f"{why} (onsets)"
            fire = bool(self._events["onset"])
            bar, downbeat = None, False
        else:
            self._beat_locked = True
            self._beat_bpm = tempo * 60.0
            if self._events["beat"]:
                self._beat_count += 1
                if div >= 1.0 and self._beat_count % int(div) == 0:
                    fire = True
            if div < 1.0:
                if self._events["beat"] or (self._beat_half_prev < 0.5 <= phase):
                    fire = True
            self._beat_half_prev = phase
            bar = f.get("bar_phase")
            downbeat = False
            try:
                bar = float(bar) if bar is not None else None
            except (TypeError, ValueError):
                bar = None
            if bar is not None:
                if self._prev_bar_phase is not None and bar < self._prev_bar_phase - 0.5:
                    downbeat = True
                self._prev_bar_phase = bar
        if fire:
            self.stats["beats"] += 1
        # --- orbit: continuous, one rim revolution per `div` beats ---
        if p.beat_lock == "orbit":
            cyc = ((self._beat_count % max(1, int(div)) if div >= 1.0 else 0) + phase) / (div if div >= 1.0 else 1.0)
            if div < 1.0:
                cyc = (phase * 2.0) % 1.0
            r = 1.0 if (p.bar_accent and bar is not None and bar < 0.12) else 0.92
            self._pos = _cart(r, 2 * math.pi * cyc)
            return self._pos
        # --- step / bounce: jump on the beat, hold between ---
        if fire or self._beat_target is None:
            if p.beat_lock == "step":
                # advance >= 90 deg: every move is a clearly different place, never a neighbour (map-weighted
                # among the eligible spokes when the point map is on; else the fixed +3/+4 walk)
                mk = self._map_rim_spoke(self._beat_spoke * math.pi / 4 if self._beat_target is not None else None)
                if mk is not None:
                    self._beat_spoke = mk
                else:
                    self._beat_spoke = (self._beat_spoke + 3 + (1 if self._rng.random() < 0.3 else 0)) % 8
                self._beat_target = _cart(1.0, self._beat_spoke * math.pi / 4)
            elif p.beat_lock == "random":
                # a random rim point at least 90 deg from the last one (angle only; r = 1.0); with the point map on
                # it is a map-weighted rim spoke instead of a continuous angle
                mk = self._map_rim_spoke(self._beat_angle)
                if mk is not None:
                    ang = mk * 2 * math.pi / SPOKES
                elif self._beat_angle is None:
                    ang = self._rng.uniform(-math.pi, math.pi)
                else:
                    turn = self._rng.uniform(RANDOM_MIN_TURN, math.pi)
                    ang = _wrap(self._beat_angle + (turn if self._rng.random() < 0.5 else -turn))
                self._beat_angle = ang
                self._beat_target = _cart(1.0, ang)
            else:  # bounce
                self._beat_side = -self._beat_side
                self._beat_target = _cart(1.0, self._beat_axis + (0.0 if self._beat_side > 0 else math.pi))
            if fire:
                self.stats["jumps"] += 1
                self.last_move = {"axis": "position", "from": list(self._pos or (0.0, 0.0)),
                                  "to": [round(self._beat_target[0], 3), round(self._beat_target[1], 3)],
                                  "t": now, "why": "beat"}
        if downbeat:
            if p.beat_lock == "bounce":
                self._beat_axis = (self._beat_axis + math.pi / 4) % (2 * math.pi)   # the stroke turns each bar
            if p.bar_accent and p.beat_lock != "random":
                self._beat_center_until = now + min(0.25, 0.5 / max(tempo, 0.5))   # a short center hit
        if now < self._beat_center_until:
            self._pos = (0.0, 0.0)
            return self._pos
        tgt = self._beat_target
        if self._pos is None:
            self._pos = tgt
        # arrive fast (a jump), then shimmer in place if the card's dwell style says so
        cur = self._pos
        d = math.hypot(tgt[0] - cur[0], tgt[1] - cur[1])
        if d > 0.02:
            k = min(1.0, (SLIDE_RATE * 4.0) * dt / d)
            self._pos = (cur[0] + (tgt[0] - cur[0]) * k, cur[1] + (tgt[1] - cur[1]) * k)
        elif p.dwell_style == "shimmer":
            ar, ath = _polar(*tgt)
            self._pos = _cart(min(1.0, ar - 0.03 + 0.03 * math.sin(now * 2 * math.pi * 3.0)), ath + 0.05 * math.sin(now * 2 * math.pi * 2.3))
        elif p.dwell_style == "drift" and p.beat_lock == "random":
            ar, ath = _polar(*tgt)                       # creep along the rim until the next jump
            self._pos = _cart(ar, ath + 0.15 * (now - (self.last_move or {}).get("t", now)))
        else:
            self._pos = tgt
        self._was_rim = True
        return self._pos

    def _position(self, a: float, b: float, now: float, dt: float) -> tuple[float, float]:
        p = self.params
        if p.tune:
            return self._tune_position(now, onset=bool(self._events.get("onset")))
        bp = self._beat_position(self._features, now, dt)
        if bp is not None:
            return bp
        # 1. compose: on an event, maybe pick our own rim point
        q_on = p.quant["alpha"].on if "alpha" in p.quant else "onset"
        if p.compose > 0 and self._events.get(q_on, True) and (self._chosen_pos is None or self._allowed("alpha", now)):
            self._chosen_pos = self._pick_pos()
            self._held_since["alpha"] = now
        if self._chosen_pos is not None and p.compose > 0:
            a = a * (1 - p.compose) + self._chosen_pos[0] * p.compose
            b = b * (1 - p.compose) + self._chosen_pos[1] * p.compose
        # 2. radial remap
        r, th = _polar(a, b)
        r = min(1.0, r)
        r2 = r ** self.gamma if r > 0 else 0.0
        if self._events["cut"]:
            self._center_until = now + p.dwell_ms / 1000.0   # deliberate center event
        if now < self._center_until:
            r2 = 0.0
        ta, tb = _cart(r2, th)
        # 3. transit
        if self._pos is None:
            self._pos = (ta, tb)
        cur = self._pos
        jump = False
        if p.transit == "jump" or (p.transit == "mix" and p.jump_ratio > 0):
            if self._events["onset"] and (p.transit == "jump" or self._rng.random() < p.jump_ratio):
                jump = True
        if now < self._dwell_until and self._dwell_anchor is not None and not jump:
            ar, ath = _polar(*self._dwell_anchor)
            if p.dwell_style == "shimmer":
                ar = min(1.0, ar + 0.06 * math.sin(now * 2 * math.pi * 3.0))
                ath += 0.06 * math.sin(now * 2 * math.pi * 2.3)
            elif p.dwell_style == "drift":
                ath += 0.15 * (now - (self._dwell_until - p.dwell_ms / 1000.0))
            self._pos = _cart(ar, ath)
            return self._pos
        if jump:
            self._pos = (ta, tb)
            self.stats["jumps"] += 1
            self.last_move = {"axis": "position", "from": [round(cur[0], 3), round(cur[1], 3)],
                              "to": [round(ta, 3), round(tb, 3)], "t": now, "why": "jump"}
        elif p.transit == "jump":
            pass   # hold until the next event
        else:
            cr, cth = _polar(*cur)
            d = math.hypot(ta - cur[0], tb - cur[1])
            dth = _wrap(th - cth)
            if p.path == "around" and cr >= RIM_R and r2 >= RIM_R and abs(dth) > ANGULAR_AROUND_MIN:
                rate = SLIDE_RATE * dt / max(cr, RIM_R)
                nth = cth + _clip(dth, -rate, rate)
                nr = cr + _clip(r2 - cr, -SLIDE_RATE * dt, SLIDE_RATE * dt)
                self._pos = _cart(max(nr, RIM_R), nth)
            elif d <= SLIDE_RATE * dt or d < 1e-9:
                self._pos = (ta, tb)
            else:
                k = SLIDE_RATE * dt / d
                self._pos = (cur[0] + (ta - cur[0]) * k, cur[1] + (tb - cur[1]) * k)
        # 4. arrival at the rim -> dwell (entering the rim, landing a jump on it, or a rim slide coming to rest)
        pr = math.hypot(*self._pos)
        on_rim = pr >= RIM_R
        settled = math.hypot(ta - self._pos[0], tb - self._pos[1]) <= 0.05
        arrived = on_rim and (not self._was_rim or jump or (self._moving and settled))
        if arrived and p.dwell_ms > 0:
            self._dwell_until = now + p.dwell_ms / 1000.0
            self._dwell_anchor = self._pos
        self._moving = not settled
        self._was_rim = on_rim
        return self._pos

    # ---- the per-tick entry point ---------------------------------------------------------------
    def apply(self, targets: dict, features: dict | None, now: float, dt: float, power_volume: bool = False) -> dict:
        """targets: mapping output (axis -> value, plus `_hold`). Returns a new dict, same keys, shaped.
        `power_volume`: a Power lane will replace volume after us -> skip the dips / fade-fight (they only make
        sense when the mapping drives volume; Power has its own tease tiers)."""
        with self._lock:
            self._now = now
            p = self.params
            out = dict(targets)
            if not p.enabled:
                for ax in ("pulse_hz", "carrier_hz", "volume"):
                    if ax in out and self.offsets.get(ax):
                        out[ax] = _clip(out[ax] + self.offsets[ax], *LIMITS[ax])
                if "alpha" in out and "beta" in out:
                    if p.tune:
                        self._events = self._detect_events(features or {}, now, dt)
                        out["alpha"], out["beta"] = self._tune_position(now, onset=bool(self._events.get("onset")))
                    r, _ = _polar(out["alpha"], out["beta"])
                    self._update_gamma(min(1.0, r), dt)
                return out
            f = features or {}
            self._features = f
            self._events = self._detect_events(f, now, dt)
            def ev(ax: str) -> bool:
                q = p.quant.get(ax)
                return bool(self._events.get(q.on if q else "onset", True))
            # value axes: compose pick -> quantize -> step offset
            for ax in ("pulse_hz", "carrier_hz", "volume"):
                if ax not in out:
                    continue
                v = float(out[ax])
                if p.compose > 0 and ax != "volume":
                    if ev(ax) and (ax not in self._chosen or self._allowed(ax, now)):
                        self._chosen[ax] = self._pick(ax)
                    if ax in self._chosen:
                        v = v * (1 - p.compose) + self._chosen[ax] * p.compose
                v = self._quant_axis(ax, v, now)
                if ax == "volume":
                    mapped = _clip(float(out[ax]) + self.offsets["volume"], 0.0, 1.0)
                    v = min(_clip(v + self.offsets["volume"], 0.0, 1.0), mapped)
                    if not power_volume:
                        v = min(self._shape_volume(v, now, dt), mapped)
                else:
                    v = _clip(v + self.offsets[ax], *LIMITS[ax])
                    if ax == "carrier_hz" and self.offsets[ax]:
                        v = _clip(_quantize(v, 100.0), *LIMITS[ax])
                out[ax] = v
            demo = self._demo_value(now)
            if demo is not None and demo[0] in out:
                ax, dv = demo
                if ax == "volume":
                    out[ax] = min(_clip(dv, 0.0, 1.0), _clip(float(targets[ax]) + self.offsets["volume"], 0.0, 1.0))
                else:
                    out[ax] = _clip(dv, *LIMITS[ax])
            if "alpha" in out and "beta" in out:
                if demo is not None and demo[0] == "edge_time":
                    # show-me for edge time: drive gamma directly (the controller is far too slow for a 6 s demo)
                    self.gamma = math.exp(math.log(3.0) + (math.log(0.2) - math.log(3.0)) * _clip(demo[1], 0.0, 1.0))
                a, b = self._position(float(out["alpha"]), float(out["beta"]), now, dt)
                out["alpha"], out["beta"] = _clip(a, -1.0, 1.0), _clip(b, -1.0, 1.0)
                if demo is None or demo[0] != "edge_time":
                    self._update_gamma(math.hypot(a, b), dt)
            return out

    # ---- readout --------------------------------------------------------------------------------
    def readout(self) -> dict:
        with self._lock:
            now = getattr(self, "_now", 0.0)
            held = {ax: int((now - t) * 1000.0) for ax, t in self._held_since.items()}
            return {
                "enabled": self.params.enabled,
                "edge_frac_60s": round(self._edge_read, 3),
                "gamma": round(self.gamma, 3),
                "last_move": self.last_move,
                "held_ms": held,
                "held": {k: round(v, 3) for k, v in self._held.items()},
                "dips": self.stats["dips"],
                "jumps": self.stats["jumps"],
                "beat": {"mode": self.params.beat_lock, "div": self.params.beat_div, "locked": self._beat_locked,
                         "bpm": round(self._beat_bpm, 1), "moves": self.stats["beats"], "why": self._beat_why},
                "compose": self.params.compose,
                "edge_time": round(_clip(self.params.edge_time + self.offsets.get("edge_time", 0.0), 0.0, 1.0), 3),
                "offsets": dict(self.offsets),
                "dwelling": now < self._dwell_until,
                "tune": self.tune_state(),
                "point_map": self.params.point_map,
                "last_point": self._last_point,
                "demo": (dict(self._demo, axis=self._demo["axis"]) if self._demo else None),
                "version": self.version,
            }


# ---- preset TOML helpers (the [moves] table in config/feel/<name>.toml) ---------------------------------

def moves_toml(p: MovesParams) -> str:
    d = p.to_dict()
    lines = ["", "[moves]"]
    for k, v in d.items():
        if k == "quant":
            continue
        if isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, str):
            lines.append(f'{k} = "{v}"')
        else:
            lines.append(f"{k} = {float(v)}")
    for axis, q in d["quant"].items():
        lines += ["", f"[moves.quant.{axis}]", f"step = {float(q['step'])}", f"hold_ms = {float(q['hold_ms'])}",
                  f'on = "{q["on"]}"']
    return "\n".join(lines) + "\n"
