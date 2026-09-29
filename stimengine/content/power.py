"""Power — reactive axis lanes with a building ceiling (notes/power.md, 2026-08-25; lanes per PlaStim the same day).

Sits after Moves in `Engine._update_follow`: for every LANE whose mode is not `manual`, Power replaces that axis's
mapped target with its own value:

    mapping targets -> Moves.apply -> Power.apply (per active lane) -> slew/smooth -> frame

Lanes are keyed by axis: volume (0..1), carrier_hz (700..1600, 100 Hz grid), pulse_hz (30..150, 10 Hz grid),
pulse_width (4..20, 0.5 grid). Each lane has its own PowerParams in the axis's OWN units; internally everything is
a fraction u in 0..1 of the lane's RANGE (lo..hi), so the follower / build / extras are axis-agnostic:

- normalized energy `en`: per-scene running p10..p90 of the feature `E` (audio or motion; `energy` fallback),
  slow EMAs (tau 30 s) seeded from the first 5 s -> loud and quiet content both swing the full range;
- envelope follower `env` on `en` with attack/release pairs (speed: snappy/musical/slow);
- `max_now` starts at min + 10 % of range and BUILDS toward `max` at build_rate (% of range per minute);
- TEASE tiers (Amendment 2): floor = min; cruise = min + cruise_frac * (max_now - min); peak = max_now. The
  follower maps env^env_curve into min..cruise (only the loud tail nears cruise). Peak is reachable ONLY by a
  surge: a strong onset (en >= surge_thresh) jumps to max_now, holds peak_hold_s, then falls back to cruise with
  the release tau. Guards: peak_spacing_s refractory and a duty cap (peak_duty = fraction of the last 60 s spent
  nearer peak than cruise); when exceeded the surge threshold rises +0.05 per violation and decays back;
- extras: breakdown (music present but low -> fast fall to min), quiet (music gone -> slow fade to min), rim
  sidechain (+gain*r^2) and beat accent (+accent for accent_ms, downbeat only when bar_phase is available) — both
  bumps WITHIN cruise, never into peak territory; plateau (at max for plateau_min minutes -> step down, build
  again), per_show (each new scene raises `max` by N % of range). Build/plateau/per-show move max_now; cruise and
  peak follow.

DRIFT (Amendment 3, 2026-08-25 — PlaStim: "carrier is much safer and better to keep in one spot"; texture changes must
be rare, deliberate, held): the lane sits at `home` on its grid and, every `drift_hold_beats` beats (phase wraps of
beat_phase; without a trusted tempo 0.5 s per beat) — and with drift_on = "phrase" additionally only on a phrase
boundary when the features carry one — takes ONE grid step (drift_step steps), toward home with probability
home_pull when away from it (random direction at home). mood_bias nudges the direction by the long energy (L high
-> down, low -> up). peak_dip lowers the lane by peak_dip_amount while the VOLUME lane is in its peak hold
(`peaking`). Drift ignores loudness entirely; build/plateau do not apply (max_now = max); the output is always ON
the grid inside [min, max] (except the dip, clamped to the axis lo).

Safety: a lane's output is ALWAYS within [min, max_now] and within the axis LIMITS. Moves' dips / fade-fight run
before Power and so cannot raise it; master and caps clamp after, as for everything. `manual` returns None (the
mapped value passes through untouched).

Pure and unit-testable: no engine imports, no clock of its own (the engine passes `now`/`dt`).
"""
from __future__ import annotations

import copy
import math
import random
import threading
from collections import deque
from dataclasses import asdict, dataclass, fields
from typing import Any

from .mapping import LIMITS

MODES = ("manual", "sound", "build", "drift")
DRIFT_ON = ("phrase", "time")
DRIFT_FALLBACK_BEAT_S = 0.5      # no trusted tempo: assume 120 bpm
DRIFT_HOME = {"volume": 0.6, "carrier_hz": 1100.0, "pulse_hz": 70.0, "pulse_width": 6.0}
DRIFT_DIP = {"volume": 0.15, "carrier_hz": 200.0, "pulse_hz": 20.0, "pulse_width": 1.0}
DRIFT_GRID_VOLUME = 0.1          # the volume lane is continuous; drift steps it by 0.1
SPEEDS = {"snappy": (0.05, 0.6), "musical": (0.15, 2.0), "slow": (0.8, 6.0)}   # attack tau, release tau (s)
BUILD_RESETS = ("session", "scene")
# axis -> (lo, hi, grid). Lane min/max (and the output) snap to the grid; 0 = continuous.
AXES: dict[str, tuple[float, float, float]] = {
    "volume": (0.0, 1.0, 0.0),
    "carrier_hz": (700.0, 1600.0, 100.0),
    "pulse_hz": (30.0, 150.0, 10.0),
    "pulse_width": (4.0, 20.0, 0.5),
}
BUILD_START_FRAC = 0.10        # max_now starts at min + 10 % of range
QUIET_THRESH = 0.12
QUIET_AFTER_S = 1.0
BREAKDOWN_AFTER_S = 1.0
BREAKDOWN_FALL_S = 0.4
SURGE_EN = 0.75                # base surge threshold (normalized energy at the onset)
SURGE_THRESH_STEP = 0.05       # +per duty violation ...
SURGE_THRESH_DECAY_PER_S = 0.005   # ... and back toward SURGE_EN at this rate (0.05 per 10 s)
DUTY_WINDOW_S = 60.0
PEAK_ABOVE_FRAC = 0.5          # "above cruise" for the duty measure = nearer peak than cruise
PLATEAU_STEP_S = 3.0
NORM_TAU_S = 30.0
NORM_SEED_S = 5.0
ONSET_REFRACTORY_S = 0.15


def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _snap(x: float, grid: float) -> float:
    return math.floor(x / grid + 0.5) * grid if grid > 0 else x   # half-up (round() is banker's: 850 -> 800)


def _fnum(v: Any, default: float = 0.0) -> float | None:
    if v is None:
        return None
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return default


@dataclass
class PowerParams:
    """One lane's live-tunable params, in the AXIS's own units for min/max (volume: fractions of master)."""
    mode: str = "manual"                 # manual (lane off) | sound | build
    min: float = 0.5
    max: float = 1.0
    build_rate_pct_per_min: float = 2.0  # % of the lane RANGE per minute; 0 = no build (max_now = max at once)
    build_reset: str = "session"         # session | scene
    speed: str = "musical"               # snappy | musical | slow
    quiet_fade_s: float = 8.0
    quiet_thresh: float = QUIET_THRESH
    surge: bool = True                   # peaks enabled (a strong onset may reach max_now)
    cruise_frac: float = 0.7             # cruise = min + cruise_frac * (max_now - min): the follower's ceiling
    env_curve: float = 1.6               # follower output = env ** env_curve (only the loud tail nears cruise)
    peak_hold_s: float = 0.6             # a peak holds max_now this long, then releases to cruise
    peak_spacing_s: float = 4.0          # refractory between peaks
    peak_duty: float = 0.12              # cap: fraction of the last 60 s above cruise before the threshold rises
    breakdown: bool = True
    breakdown_thresh: float = 0.25
    rim_sidechain: bool = True
    rim_gain: float = 0.10               # fraction of range per r^2
    beat_accent: bool = True
    accent: float = 0.10                 # fraction of range
    accent_ms: float = 120.0
    plateau: bool = True
    plateau_min: float = 4.0
    plateau_drop: float = 0.10           # fraction of range
    per_show_pct: float = 3.0            # % of range added to max per new scene
    # DRIFT (Amendment 3) — per-axis defaults for home / peak_dip_amount come from for_axis()
    home: float = 0.6                    # where the lane sits; on the lane grid, inside [min, max]
    drift_hold_beats: float = 8.0        # beats between moves (0.5 s per beat without a trusted tempo)
    drift_on: str = "phrase"             # phrase: move only on a phrase boundary (else every hold) | time
    drift_step: float = 1.0              # grid steps per move
    home_pull: float = 0.6               # probability a move goes toward home when away from it
    mood_bias: bool = False              # L high -> bias down, low -> bias up
    peak_dip: bool = False               # lower the lane while the volume lane is peaking
    peak_dip_amount: float = 0.15        # axis units

    def validated(self, axis: str = "volume") -> "PowerParams":
        lo, hi, grid = AXES.get(axis, AXES["volume"])
        p = copy.copy(self)
        p.mode = p.mode if p.mode in MODES else "manual"
        p.min = _clip(_snap(float(p.min), grid), lo, hi)
        p.max = _clip(_snap(float(p.max), grid), lo, hi)
        if p.max < p.min:
            p.max = p.min
        p.build_rate_pct_per_min = _clip(float(p.build_rate_pct_per_min), 0.0, 20.0)
        p.build_reset = p.build_reset if p.build_reset in BUILD_RESETS else "session"
        p.speed = p.speed if p.speed in SPEEDS else "musical"
        p.quiet_fade_s = _clip(float(p.quiet_fade_s), 0.1, 120.0)
        p.quiet_thresh = _clip(float(p.quiet_thresh), 0.0, 1.0)
        p.surge = bool(p.surge)
        p.cruise_frac = _clip(float(p.cruise_frac), 0.0, 1.0)
        p.env_curve = _clip(float(p.env_curve), 0.2, 5.0)
        p.peak_hold_s = _clip(float(p.peak_hold_s), 0.0, 30.0)
        p.peak_spacing_s = _clip(float(p.peak_spacing_s), 0.0, 120.0)
        p.peak_duty = _clip(float(p.peak_duty), 0.0, 1.0)
        p.breakdown = bool(p.breakdown)
        p.breakdown_thresh = _clip(float(p.breakdown_thresh), 0.0, 1.0)
        p.rim_sidechain = bool(p.rim_sidechain)
        p.rim_gain = _clip(float(p.rim_gain), 0.0, 1.0)
        p.beat_accent = bool(p.beat_accent)
        p.accent = _clip(float(p.accent), 0.0, 1.0)
        p.accent_ms = _clip(float(p.accent_ms), 0.0, 2000.0)
        p.plateau = bool(p.plateau)
        p.plateau_min = _clip(float(p.plateau_min), 0.1, 120.0)
        p.plateau_drop = _clip(float(p.plateau_drop), 0.0, 1.0)
        p.per_show_pct = _clip(float(p.per_show_pct), 0.0, 50.0)
        dgrid = grid if grid > 0 else DRIFT_GRID_VOLUME
        p.home = _clip(_snap(float(p.home), dgrid), p.min, p.max)
        p.drift_hold_beats = _clip(float(p.drift_hold_beats), 1.0, 64.0)
        p.drift_on = p.drift_on if p.drift_on in DRIFT_ON else "phrase"
        p.drift_step = float(max(1, min(8, int(round(float(p.drift_step))))))
        p.home_pull = _clip(float(p.home_pull), 0.0, 1.0)
        p.mood_bias = bool(p.mood_bias)
        p.peak_dip = bool(p.peak_dip)
        p.peak_dip_amount = _clip(float(p.peak_dip_amount), 0.0, hi - lo)
        return p

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def for_axis(cls, axis: str) -> "PowerParams":
        lo, hi, _ = AXES.get(axis, AXES["volume"])
        home = DRIFT_HOME.get(axis, 0.6)
        dip = DRIFT_DIP.get(axis, 0.15)
        if axis == "volume":
            return cls(home=home, peak_dip_amount=dip)
        return cls(min=lo, max=hi, home=home, peak_dip_amount=dip).validated(axis)

    def updated(self, partial: dict, axis: str = "volume") -> "PowerParams":
        """Partial update (same shape as to_dict); unknown keys ignored. Returns a NEW validated params."""
        p = copy.copy(self)
        names = {f.name for f in fields(PowerParams)}
        for k, v in (partial or {}).items():
            if k == "surge_hold_s" and v is not None:      # pre-Amendment-2 presets
                k = "peak_hold_s"
            if k == "drift_hold_s" and v is not None:      # first drift draft (seconds): 0.5 s per beat
                k, v = "drift_hold_beats", float(v) / DRIFT_FALLBACK_BEAT_S
            if k in names and v is not None:
                setattr(p, k, v)
        return p.validated(axis)


class PowerLane:
    """One axis. Not thread-safe on its own — `Power` holds the lock."""

    def __init__(self, axis: str, params: PowerParams | None = None) -> None:
        if axis not in AXES:
            raise ValueError(f"axis must be one of {tuple(AXES)}")
        self.axis = axis
        self.lo, self.hi, self.grid = AXES[axis]
        self.params = (params or PowerParams.for_axis(axis)).validated(axis)
        self.reset_all()

    # ---- unit helpers ----------------------------------------------------------------------------
    def _u(self, x: float) -> float:            # units -> fraction of range
        return (x - self.lo) / (self.hi - self.lo)

    def _x(self, u: float) -> float:            # fraction of range -> units
        return self.lo + u * (self.hi - self.lo)

    # ---- state ------------------------------------------------------------------------------------
    def reset_all(self) -> None:
        self._now = 0.0
        self.env = 0.0
        self.en = 0.0
        self.out: float | None = None
        self.state = "idle"
        self._p10: float | None = None
        self._p90: float | None = None
        self._seed: list[float] = []
        self._seed_t0: float | None = None
        self._prev_onset = 0.0
        self._prev_E: float | None = None
        self._E_ema: float | None = None
        self._last_onset_t = -1e9
        self._prev_phase: float | None = None
        self._prev_bar: float | None = None
        self._low_since: float | None = None
        self._surge_until = -1e9
        self._last_peak_t: float | None = None
        self._peak_env = 0.0                        # 1 during the hold, then releases toward 0 (peak -> cruise)
        self.surge_thresh = SURGE_EN
        self._duty: deque[tuple[float, float, bool]] = deque()   # (t, dt, above cruise) over the last 60 s
        self._duty_above = 0.0
        self._duty_total = 0.0
        self.cruise = self.params.min
        self._accent_until = -1e9
        self._at_max_since: float | None = None
        self._plateau_until = -1e9
        self._plateau_from = 0.0
        self._plateau_to = 0.0
        self.stats = {"surges": 0, "breakdowns": 0, "accents": 0, "plateaus": 0, "duty_violations": 0}
        self.peaking = False                        # volume lane: inside a peak hold (drift lanes dip on it)
        self.peaking_dip = False                    # drift lane: currently dipped for the volume lane's peak
        self.rng = random.Random()
        self.reset_drift()
        self.reset_build()

    def reset_drift(self) -> None:
        """Drift back to home; the hold starts over."""
        self.drift_pos = self.params.home
        self.drift_moves = 0
        self._drift_last_move_t: float | None = None
        self._drift_t0: float | None = None
        self._drift_beats = 0
        self._drift_prev_phase: float | None = None
        self._drift_prev_phrase: int | None = None
        self._drift_tempo = 0.0

    def reset_build(self) -> None:
        """Restart the build: max_now back to min + 10 % of range (or max when the rate is 0)."""
        p = self.params
        self.max_now = p.max if p.build_rate_pct_per_min <= 0 else min(p.max, self._x(self._u(p.min) + BUILD_START_FRAC))
        self._at_max_since = None
        self._plateau_until = -1e9

    def reset_scene(self) -> None:
        """Per-scene normalization restarts (the next scene may be a different loudness)."""
        self._p10 = self._p90 = None
        self._seed = []
        self._seed_t0 = None
        self._low_since = None

    def set_params(self, params: PowerParams) -> None:
        old = self.params
        self.params = params.validated(self.axis)
        p = self.params
        if p.build_rate_pct_per_min <= 0:
            self.max_now = p.max
        elif old.max != p.max or old.min != p.min or self.max_now > p.max:
            # a moved range keeps the build's PROGRESS (fraction of the way from min to max)
            span_old = max(1e-9, old.max - old.min)
            frac = _clip((self.max_now - old.min) / span_old, BUILD_START_FRAC, 1.0)
            self.max_now = min(p.max, max(p.min, p.min + frac * (p.max - p.min)))
            if self.max_now < p.max:
                self._at_max_since = None
        if old.mode == "manual" and p.mode != "manual":
            self.state = "following"
        if p.mode == "drift":
            self.max_now = p.max                       # build / plateau do not apply in drift
            if old.mode != "drift" or old.home != p.home:
                self.reset_drift()
            self.drift_pos = _clip(self.drift_pos, p.min, p.max)
            self.state = "drift"
        if p.mode == "manual":
            self.state = "idle"
            self.out = None
            self.peaking = False

    # ---- energy normalization --------------------------------------------------------------------
    def _normalize(self, E: float, now: float, dt: float) -> float:
        if self._seed_t0 is None:
            self._seed_t0 = now
        if self._p10 is None or now - self._seed_t0 < NORM_SEED_S:
            self._seed.append(E)
            s = sorted(self._seed)
            n = len(s)
            self._p10 = s[max(0, min(n - 1, int(0.10 * (n - 1))))]
            self._p90 = s[max(0, min(n - 1, int(round(0.90 * (n - 1)))))]
        else:
            spread = max(0.02, self._p90 - self._p10)
            k = min(1.0, dt / NORM_TAU_S) * spread      # a steady input moves a percentile one spread per ~tau
            self._p10 += k * (0.10 - (1.0 if E < self._p10 else 0.0))
            self._p90 += k * (0.90 - (1.0 if E < self._p90 else 0.0))
            if self._p90 < self._p10 + 0.02:
                self._p90 = self._p10 + 0.02
        span = self._p90 - self._p10
        if span < 1e-6:
            return 0.0 if E <= self._p10 else 1.0
        return _clip((E - self._p10) / span, 0.0, 1.0)

    # ---- events -----------------------------------------------------------------------------------
    def _events(self, f: dict, E: float | None, now: float, dt: float) -> tuple[bool, bool]:
        """(onset, accent-beat). Accent beat = downbeat when bar_phase/downbeat exist, else every beat."""
        onset = False
        o = _fnum(f.get("onset", f.get("audio.onset")))
        if o is not None:
            onset = o >= 1.0 and self._prev_onset < 1.0
            self._prev_onset = o
        if E is not None:
            if self._E_ema is None:
                self._E_ema = E
            thr = self._E_ema + 0.05
            if self._prev_E is not None and self._prev_E < thr <= E:
                onset = True
            self._prev_E = E
            self._E_ema += min(1.0, dt / 2.0) * (E - self._E_ema)
        if f.get("cut") or f.get("transition"):
            onset = True
        if onset and now - self._last_onset_t < ONSET_REFRACTORY_S:
            onset = False
        if onset:
            self._last_onset_t = now
        beat = bool(f.get("beat"))
        phase = _fnum(f.get("beat_phase", f.get("audio.beat_phase")))
        if phase is None:
            phi = _fnum(f.get("phi"))
            phase = (phi / (2 * math.pi)) % 1.0 if phi is not None else None
        if phase is not None:
            if self._prev_phase is not None and phase < self._prev_phase - 0.5:
                beat = True
            self._prev_phase = phase
        bar = _fnum(f.get("bar_phase", f.get("audio.bar_phase")))
        downbeat = bool(f.get("downbeat"))
        if bar is not None:
            if self._prev_bar is not None and bar < self._prev_bar - 0.5:
                downbeat = True
            self._prev_bar = bar
            return onset, downbeat
        if "downbeat" in f:
            return onset, downbeat
        return onset, beat

    # ---- build / plateau --------------------------------------------------------------------------
    def _build(self, now: float, dt: float) -> None:
        p = self.params
        if p.build_rate_pct_per_min <= 0:
            self.max_now = p.max
            return
        if now < self._plateau_until:
            k = 1.0 - (self._plateau_until - now) / PLATEAU_STEP_S
            self.max_now = self._plateau_from + (self._plateau_to - self._plateau_from) * _clip(k, 0.0, 1.0)
            return
        if self.max_now < p.max:
            rate = p.build_rate_pct_per_min / 100.0 * (self.hi - self.lo) / 60.0    # units/s
            self.max_now = min(p.max, self.max_now + rate * dt)
            self._at_max_since = None if self.max_now < p.max else now
            return
        if self._at_max_since is None:
            self._at_max_since = now
        elif p.plateau and now - self._at_max_since >= p.plateau_min * 60.0 and p.plateau_drop > 0:
            self._plateau_from = self.max_now
            self._plateau_to = max(p.min, self.max_now - p.plateau_drop * (self.hi - self.lo))
            self._plateau_until = now + PLATEAU_STEP_S
            self._at_max_since = None
            self.stats["plateaus"] += 1

    # ---- the per-tick entry point ----------------------------------------------------------------
    def apply(self, mapped: float | None, features: dict | None, position: tuple[float, float] | None,
              now: float, dt: float, peaking: bool = False) -> float | None:
        self._now = now
        p = self.params
        self.peaking_dip = False
        if p.mode == "manual":
            self.state = "idle"
            self.out = None
            self.peaking = False
            return None
        if p.mode == "drift":
            self.peaking = False
            return self._drift(features or {}, now, dt, peaking)
        f = features or {}
        E = _fnum(f.get("E", f.get("energy", f.get("audio.energy"))))
        onset, accent_beat = self._events(f, E, now, dt)
        if E is not None:
            self.en = self._normalize(E, now, dt)
        en = self.en
        attack, release = SPEEDS[p.speed]
        # ---- low-energy states: quiet (music gone) vs breakdown (music present, low) ----
        hyst = 0.08 if self.state in ("quiet", "breakdown") else 0.0   # leave a low state only clearly above it
        low_q = en < p.quiet_thresh + hyst
        low_b = en < p.breakdown_thresh + hyst
        if low_q or (p.breakdown and low_b):
            if self._low_since is None:
                self._low_since = now
        else:
            self._low_since = None
        quiet = low_q and self._low_since is not None and now - self._low_since > QUIET_AFTER_S
        breakdown = (not quiet and p.breakdown and low_b and self._low_since is not None
                     and now - self._low_since > BREAKDOWN_AFTER_S)
        prev_state = self.state
        # ---- duty cap: fraction of the last 60 s spent nearer peak than cruise; the threshold decays back ----
        self.surge_thresh = max(SURGE_EN, self.surge_thresh - SURGE_THRESH_DECAY_PER_S * dt)
        while self._duty and now - self._duty[0][0] > DUTY_WINDOW_S:
            _, odt, oabove = self._duty.popleft()
            self._duty_total -= odt
            if oabove:
                self._duty_above -= odt
        duty = (self._duty_above / self._duty_total) if self._duty_total > 1e-9 else 0.0
        # ---- peak (surge): strong onset -> jump to max_now, hold, release to cruise; spacing + duty guards ----
        if p.surge and onset and now >= self._surge_until and en >= SURGE_EN:
            spaced = self._last_peak_t is None or now - self._last_peak_t >= p.peak_spacing_s
            if spaced and duty > p.peak_duty:
                self.surge_thresh = min(1.5, self.surge_thresh + SURGE_THRESH_STEP)   # +0.05 per violation
                self.stats["duty_violations"] += 1
            if spaced and en >= self.surge_thresh:
                self._surge_until = now + p.peak_hold_s
                self._last_peak_t = now
                self._peak_env = 1.0
                self.stats["surges"] += 1
        surging = now < self._surge_until
        self.peaking = surging
        # ---- envelope follower ----
        if surging:
            self.env = 1.0
            state = "surge"
        else:
            if quiet:
                target, fall = 0.0, p.quiet_fade_s
                state = "quiet"
            elif breakdown:
                target, fall = 0.0, BREAKDOWN_FALL_S
                state = "breakdown"
            else:
                target, fall = en, release
                state = "following"
            tau = attack if target > self.env else fall
            self.env += (1.0 - math.exp(-dt / max(1e-6, tau))) * (target - self.env)
            self.env = _clip(self.env, 0.0, 1.0)
        if state == "breakdown" and prev_state != "breakdown":
            self.stats["breakdowns"] += 1
        # ---- build / plateau ----
        self._build(now, dt)
        if now < self._plateau_until:
            state = "plateau"
        # ---- compose the output in range units: floor..cruise from the follower, peak only from a surge ----
        min_u, maxn_u = self._u(p.min), self._u(self.max_now)
        cruise_u = min_u + p.cruise_frac * (maxn_u - min_u)
        u = min_u + (self.env ** p.env_curve) * (cruise_u - min_u)
        if p.rim_sidechain and position is not None and p.rim_gain > 0:
            r = min(1.0, math.hypot(float(position[0]), float(position[1])))
            u += p.rim_gain * r * r
        if p.beat_accent and accent_beat and p.accent > 0 and p.accent_ms > 0:
            self._accent_until = now + p.accent_ms / 1000.0
            self.stats["accents"] += 1
        if now < self._accent_until:
            u += p.accent
        u = min(u, cruise_u)                                   # bumps live WITHIN cruise
        if not surging and self._peak_env > 0.0:
            self._peak_env *= math.exp(-dt / max(1e-6, release))   # peak -> cruise with the release tau
            if self._peak_env < 0.01:
                self._peak_env = 0.0
        if self._peak_env > 0.0:
            u = max(u, cruise_u + self._peak_env * (maxn_u - cruise_u))
        above = self._peak_env > PEAK_ABOVE_FRAC
        self._duty.append((now, dt, above))
        self._duty_total += dt
        if above:
            self._duty_above += dt
        self.cruise = _clip(self._x(cruise_u), p.min, self.max_now)
        x = _snap(self._x(_clip(u, min_u, maxn_u)), self.grid)
        ceiling = self.max_now
        if self.grid > 0:
            if abs(_snap(ceiling, self.grid) - ceiling) < self.grid * 1e-6:
                ceiling = _snap(ceiling, self.grid)          # float noise: 899.9999 is 900
            if x > ceiling:
                x = math.floor(ceiling / self.grid) * self.grid   # stay on the grid AND under the ceiling
        x = _clip(x, p.min, ceiling)
        x = _clip(x, *LIMITS.get(self.axis, (self.lo, self.hi)))
        self.state = state
        self.out = x
        return x

    # ---- drift (Amendment 3) ----------------------------------------------------------------------
    @property
    def drift_grid(self) -> float:
        return self.grid if self.grid > 0 else DRIFT_GRID_VOLUME

    def _drift_hold_left(self) -> tuple[float, float | None]:
        """(beats remaining, seconds remaining) until a move is allowed; seconds only in the no-tempo fallback."""
        p = self.params
        if self._drift_tempo > 0:
            return max(0.0, p.drift_hold_beats - self._drift_beats), None
        t0 = self._drift_last_move_t if self._drift_last_move_t is not None else self._drift_t0
        if t0 is None:
            t0 = self._now
        left = max(0.0, p.drift_hold_beats * DRIFT_FALLBACK_BEAT_S - (self._now - t0))
        return left / DRIFT_FALLBACK_BEAT_S, left

    def _drift(self, f: dict, now: float, dt: float, peaking: bool) -> float:
        p = self.params
        if self._drift_t0 is None:
            self._drift_t0 = now
        # beats: phase wraps of beat_phase / audio.beat_phase / phi (same lookup as moves.py)
        try:
            self._drift_tempo = float(f.get("tempo_hz", f.get("audio.tempo_hz", 0.0)) or 0.0)
        except (TypeError, ValueError):
            self._drift_tempo = 0.0
        phase = _fnum(f.get("beat_phase", f.get("audio.beat_phase")))
        if phase is None:
            phi = _fnum(f.get("phi"))
            phase = (phi / (2 * math.pi)) % 1.0 if phi is not None else None
        if phase is not None and phase > 1.5:
            phase = (phase / (2 * math.pi)) % 1.0
        if phase is not None:
            if self._drift_prev_phase is not None and phase < self._drift_prev_phase - 0.5:
                self._drift_beats += 1
            self._drift_prev_phase = phase
        # phrase boundary: phrase_index / audio.phrase_index changed
        ph = f.get("phrase_index", f.get("audio.phrase_index"))
        boundary = False
        has_phrase = ph is not None
        if has_phrase:
            try:
                ph = int(ph)
            except (TypeError, ValueError):
                ph, has_phrase = None, False
        if has_phrase:
            boundary = self._drift_prev_phrase is not None and ph != self._drift_prev_phrase
            self._drift_prev_phrase = ph
        beats_left, _secs_left = self._drift_hold_left()
        held = beats_left <= 0.0
        if p.drift_on == "phrase" and has_phrase and self._drift_tempo > 0:
            allowed = held and boundary            # music: only on a phrase boundary after the hold
        else:
            allowed = held                         # time mode, or no music: every hold
        if allowed:
            self._drift_move(f)
            self._drift_last_move_t = now
            self._drift_beats = 0
        grid = self.drift_grid
        x = _snap(_clip(self.drift_pos, p.min, p.max), grid)
        self.drift_pos = x
        state = "drift"
        if p.peak_dip and peaking and p.peak_dip_amount > 0:
            x = max(self.lo, _snap(x - p.peak_dip_amount, grid))
            self.peaking_dip = True
            state = "dip"
        x = _clip(x, *LIMITS.get(self.axis, (self.lo, self.hi)))
        self.max_now = p.max
        self.state = state
        self.out = x
        return x

    def _drift_move(self, f: dict) -> None:
        p = self.params
        grid = self.drift_grid
        pos, home = self.drift_pos, p.home
        r = self.rng.random()
        if abs(pos - home) < grid * 0.5:
            d = 1 if r < 0.5 else -1                       # at home: random direction
        else:
            toward = 1 if home > pos else -1
            d = toward if r < p.home_pull else -toward
        if p.mood_bias:
            L = _fnum(f.get("energy_long", f.get("L", f.get("audio.energy_60s"))))
            if L is not None:
                if L >= 0.6 and self.rng.random() < 0.75:
                    d = -1
                elif L <= 0.4 and self.rng.random() < 0.75:
                    d = 1
        step = p.drift_step * grid
        new = _clip(_snap(pos + d * step, grid), p.min, p.max)
        if abs(new - pos) < grid * 1e-6:                   # against min/max: step the other way
            new = _clip(_snap(pos - d * step, grid), p.min, p.max)
        if abs(new - pos) >= grid * 1e-6:
            self.drift_moves += 1
        self.drift_pos = new

    # ---- scene hooks ------------------------------------------------------------------------------
    def on_new_scene(self, first: bool) -> None:
        p = self.params
        self.reset_scene()
        if first:
            return
        if p.per_show_pct > 0:
            new_max = _clip(_snap(p.max + p.per_show_pct / 100.0 * (self.hi - self.lo), self.grid), p.min, self.hi)
            self.params = copy.copy(p)
            self.params.max = new_max
            if p.build_rate_pct_per_min <= 0:
                self.max_now = new_max
            elif self.max_now >= p.max and new_max > p.max:
                self._at_max_since = None      # the build has somewhere to go again
        if p.build_reset == "scene":
            self.reset_build()

    # ---- readout ----------------------------------------------------------------------------------
    def readout(self) -> dict:
        p = self.params
        rate = p.build_rate_pct_per_min / 100.0 * (self.hi - self.lo)      # units per minute
        remaining = p.max - self.max_now
        if p.mode in ("manual", "drift"):
            mins = None if p.mode == "manual" else 0.0
        elif remaining <= 1e-9 or rate <= 0:
            mins = 0.0
        else:
            mins = round(remaining / rate, 2)
        beats_left: float | None = None
        secs_left: float | None = None
        if p.mode == "drift":
            b, sec = self._drift_hold_left()
            beats_left = round(b, 2)
            secs_left = round(sec, 2) if sec is not None else None
        return {
            "axis": self.axis,
            "mode": p.mode,
            "active": p.mode != "manual",
            "out": (round(self.out, 4) if self.out is not None else None),
            "min": p.min, "max": p.max, "max_now": round(self.max_now, 4),
            "lo": self.lo, "hi": self.hi, "grid": self.grid,
            "build_rate_pct_per_min": p.build_rate_pct_per_min,
            "en": round(self.en, 3), "env": round(self.env, 3),
            "state": self.state,
            "minutes_to_max": mins,
            "surges": self.stats["surges"], "breakdowns": self.stats["breakdowns"], "accents": self.stats["accents"],
            "plateaus": self.stats["plateaus"],
            # TEASE tiers (Amendment 2)
            "cruise": round(self.cruise, 4),
            "peaks": self.stats["surges"],
            "last_peak_s": (round(max(0.0, self._now - self._last_peak_t), 2) if self._last_peak_t is not None else None),
            "peak_duty_60s": round((self._duty_above / self._duty_total) if self._duty_total > 1e-9 else 0.0, 3),
            "surge_thresh": round(self.surge_thresh, 3),
            "peak_hold_s": p.peak_hold_s, "peak_spacing_s": p.peak_spacing_s, "peak_duty": p.peak_duty,
            "cruise_frac": p.cruise_frac,
            "peaking": bool(self.peaking),
            # DRIFT (Amendment 3)
            "home": p.home, "drift_hold_beats": p.drift_hold_beats, "drift_on": p.drift_on,
            "drift_step": p.drift_step, "home_pull": p.home_pull, "mood_bias": p.mood_bias,
            "peak_dip": p.peak_dip, "peak_dip_amount": p.peak_dip_amount,
            "next_move_beats": beats_left, "next_move_s": secs_left,
            "moves": self.drift_moves,
            "last_move_s": (round(max(0.0, self._now - self._drift_last_move_t), 2)
                            if self._drift_last_move_t is not None else None),
            "peaking_dip": bool(self.peaking_dip),
        }


class Power:
    """All lanes; live-tunable, thread-safe. `apply()` per tick replaces the active lanes' targets."""

    def __init__(self, params: dict[str, PowerParams] | None = None) -> None:
        self._lock = threading.Lock()
        params = dict(params or {})
        # Amendment 2: the volume lane boots in `sound` (the mapping never owns volume unless the user says so)
        params.setdefault("volume", PowerParams(mode="sound"))
        # Amendment 3: the carrier lane boots in `drift` at home 1100 ("keep it in one spot" out of the box)
        params.setdefault("carrier_hz", PowerParams.for_axis("carrier_hz").updated({"mode": "drift"}, "carrier_hz"))
        self.lanes: dict[str, PowerLane] = {ax: PowerLane(ax, params.get(ax)) for ax in AXES}
        self.version = 0
        self._scene: str | None = None
        self._seen_scene = False

    # ---- params -----------------------------------------------------------------------------------
    def lane_dict(self, axis: str) -> dict:
        lane = self.lanes[axis]
        d = lane.params.to_dict()
        d["axis"] = axis
        d["lo"], d["hi"], d["grid"] = lane.lo, lane.hi, lane.grid
        d["max_now"] = round(lane.max_now, 4)
        return d

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "lanes": {ax: self.lane_dict(ax) for ax in AXES},
                "axes": list(AXES),
                "active": [ax for ax, ln in self.lanes.items() if ln.params.mode != "manual"],
                "options": {"mode": list(MODES), "speed": list(SPEEDS), "build_reset": list(BUILD_RESETS),
                            "drift_on": list(DRIFT_ON), "speeds": {k: list(v) for k, v in SPEEDS.items()}},
                "version": self.version,
            }

    def params_dict(self) -> dict[str, dict]:
        """Lane params only (what presets save): {axis: PowerParams.to_dict()}."""
        with self._lock:
            return {ax: ln.params.to_dict() for ax, ln in self.lanes.items()}

    def update(self, partial: dict, axis: str = "volume", bump: bool = True) -> dict:
        axis = str(partial.get("axis") or axis or "volume") if isinstance(partial, dict) else axis
        if axis not in AXES:
            raise ValueError(f"axis must be one of {tuple(AXES)}")
        body = {k: v for k, v in (partial or {}).items() if k != "axis"}
        with self._lock:
            lane = self.lanes[axis]
            lane.set_params(lane.params.updated(body, axis))
            if bump:
                self.version += 1
        return self.to_dict()

    def update_all(self, lanes: dict[str, dict], bump: bool = True) -> dict:
        """Presets: {axis: partial} for any subset of lanes."""
        for ax, d in (lanes or {}).items():
            if ax in AXES and isinstance(d, dict):
                self.update(d, axis=ax, bump=False)
        if bump:
            with self._lock:
                self.version += 1
        return self.to_dict()

    def reset(self, axis: str | None = None) -> dict:
        """Restart the build now (one lane, or all)."""
        with self._lock:
            for ax, ln in self.lanes.items():
                if axis is None or ax == axis:
                    ln.reset_build()
        return self.to_dict()

    def active(self, axis: str) -> bool:
        ln = self.lanes.get(axis)
        return ln is not None and ln.params.mode != "manual"

    @property
    def any_active(self) -> bool:
        return any(ln.params.mode != "manual" for ln in self.lanes.values())

    # ---- scene hook -------------------------------------------------------------------------------
    def on_scene(self, scene_id: str | None) -> None:
        """Engine: a new scene starts following. Same id again is not a new scene; None clears."""
        with self._lock:
            if scene_id is None or scene_id == self._scene:
                return
            first = not self._seen_scene
            self._scene = scene_id
            self._seen_scene = True
            for ln in self.lanes.values():
                ln.on_new_scene(first)

    # ---- per tick ---------------------------------------------------------------------------------
    def apply_lane(self, axis: str, mapped: float | None, features: dict | None,
                   position: tuple[float, float] | None, now: float, dt: float) -> float | None:
        with self._lock:
            return self.lanes[axis].apply(mapped, features, position, now, dt, peaking=self.lanes["volume"].peaking)

    def apply(self, targets: dict, features: dict | None, position: tuple[float, float] | None,
              now: float, dt: float) -> tuple[dict, list[str]]:
        """Replace the active lanes' targets. Returns (new targets, axes Power drove)."""
        out = dict(targets)
        drove: list[str] = []
        with self._lock:
            vol = self.lanes["volume"]
            for ax, ln in self.lanes.items():
                if ln.params.mode == "manual":
                    continue
                # the volume lane runs first (dict order), so its peak hold is fresh for the drift lanes' dip
                v = ln.apply(out.get(ax), features, position, now, dt, peaking=vol.peaking)
                if v is not None:
                    out[ax] = v
                    drove.append(ax)
        return out, drove

    def readout(self) -> dict:
        with self._lock:
            return {ax: ln.readout() for ax, ln in self.lanes.items()}


# ---- preset TOML helpers (the [power.<axis>] tables in config/feel/<name>.toml) -------------------------

def power_toml(lanes: dict[str, dict]) -> str:
    lines: list[str] = []
    for ax, d in lanes.items():
        lines += ["", f"[power.{ax}]"]
        for k, v in d.items():
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, str):
                lines.append(f'{k} = "{v}"')
            else:
                lines.append(f"{k} = {float(v)}")
    return "\n".join(lines) + "\n"
