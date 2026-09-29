"""Pattern runner: internal motion generator that drives the Engine at ~30 Hz under an external lease.

Safety model (do not weaken): the runner is an *internal* source. It only calls `engine.renew_lease("pattern")`
while an external party (the API caller: Claude, the viewer) holds a live lease granted via `grant_lease(seconds)`.
When that lease lapses the runner keeps computing geometry but stops renewing, so the engine's deadman ramps the
volume to zero. "Claude went quiet" therefore fades to nothing within deadman_silence_s + deadman_ramp_down_s.

3-phase patterns (alpha/beta): circle, stroke, figure8, random_walk, hold
4-phase patterns (e1..e4):     round_robin, wave, all_equal
Optional volume envelope: ramp master from a -> b over N seconds (reductions obey the same linear ramp; the
engine's own slow-start still bounds increases).
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from ..engine import Engine

logger = logging.getLogger("engine.patterns")

PATTERN_HZ = 30
THREEPHASE_PATTERNS = ("circle", "stroke", "figure8", "random_walk", "hold")
FOURPHASE_PATTERNS = ("round_robin", "wave", "all_equal")
ALL_PATTERNS = THREEPHASE_PATTERNS + FOURPHASE_PATTERNS
SOURCE = "internal"  # must be in engine.INTERNAL_SOURCES: pattern writes are NOT control input; only the lease is


def _clip(v: float, lo: float, hi: float) -> float:
    return float(max(lo, min(hi, v)))


@dataclass
class PatternParams:
    name: str = "hold"
    rate_hz: float = 0.2          # cycles per second of the geometry
    amplitude: float = 0.5        # 0..1 radius / depth
    center: tuple[float, float] = (0.0, 0.0)   # alpha, beta (3-phase)
    floor: float = 0.0            # 4-phase: minimum intensity for idle electrodes
    seed: int = 0                 # random_walk reproducibility

    @classmethod
    def from_dict(cls, d: dict[str, Any], base: "PatternParams | None" = None) -> "PatternParams":
        p = PatternParams(**asdict(base)) if base else PatternParams()
        if "name" in d:
            name = str(d["name"])
            if name not in ALL_PATTERNS:
                raise ValueError(f"unknown pattern {name!r}; choose from {', '.join(ALL_PATTERNS)}")
            p.name = name
        if "rate_hz" in d:
            p.rate_hz = _clip(float(d["rate_hz"]), 0.0, 5.0)
        if "amplitude" in d:
            p.amplitude = _clip(float(d["amplitude"]), 0.0, 1.0)
        if "center" in d:
            c = d["center"]
            if not isinstance(c, (list, tuple)) or len(c) != 2:
                raise ValueError("center must be [alpha, beta]")
            p.center = (_clip(float(c[0]), -1.0, 1.0), _clip(float(c[1]), -1.0, 1.0))
        if "floor" in d:
            p.floor = _clip(float(d["floor"]), 0.0, 1.0)
        if "seed" in d:
            p.seed = int(d["seed"])
        return p


@dataclass
class Envelope:
    """Linear volume ramp a -> b over seconds, started at t0 (runner clock)."""

    start: float
    end: float
    seconds: float
    t0: float

    def value(self, now: float) -> float:
        if self.seconds <= 0:
            return self.end
        p = _clip((now - self.t0) / self.seconds, 0.0, 1.0)
        return self.start + (self.end - self.start) * p

    def done(self, now: float) -> bool:
        return now - self.t0 >= self.seconds


# ---- geometry ------------------------------------------------------------------------------------------------

def _unit_disc(alpha: float, beta: float) -> tuple[float, float]:
    r = math.hypot(alpha, beta)
    if r > 1.0:
        alpha, beta = alpha / r, beta / r
    return alpha, beta


class _Walker:
    """Smooth bounded random walk inside the amplitude disc around center (Ornstein-Uhlenbeck-ish)."""

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.x = 0.0
        self.y = 0.0
        self.vx = 0.0
        self.vy = 0.0

    def step(self, dt: float, rate_hz: float) -> tuple[float, float]:
        k = max(0.05, rate_hz) * 2 * math.pi
        sigma = k * 0.6
        self.vx += (-k * self.x - 0.8 * self.vx) * dt + sigma * self.rng.gauss(0, 1) * math.sqrt(dt)
        self.vy += (-k * self.y - 0.8 * self.vy) * dt + sigma * self.rng.gauss(0, 1) * math.sqrt(dt)
        self.x = _clip(self.x + self.vx * dt, -1.0, 1.0)
        self.y = _clip(self.y + self.vy * dt, -1.0, 1.0)
        return self.x, self.y


def threephase_point(p: PatternParams, phase: float, walker: _Walker | None, dt: float) -> tuple[float, float]:
    """phase = cycles elapsed (float). Returns (alpha, beta) inside the unit disc."""
    a0, b0 = p.center
    r = p.amplitude
    th = 2 * math.pi * phase
    if p.name == "circle":
        a, b = a0 + r * math.cos(th), b0 + r * math.sin(th)
    elif p.name == "stroke":
        a, b = a0, b0 + r * math.sin(th)
    elif p.name == "figure8":
        a, b = a0 + r * math.sin(th), b0 + r * math.sin(2 * th) / 2
    elif p.name == "random_walk":
        assert walker is not None
        x, y = walker.step(dt, p.rate_hz)
        a, b = a0 + r * x, b0 + r * y
    else:  # hold
        a, b = a0, b0
    return _unit_disc(a, b)


def fourphase_vector(p: PatternParams, phase: float) -> tuple[float, float, float, float]:
    """phase = cycles elapsed. Returns e1..e4 in 0..1 (amplitude scales the active level above floor)."""
    lo = p.floor
    hi = _clip(lo + p.amplitude * (1.0 - lo), 0.0, 1.0)
    if p.name == "round_robin":
        pos = (phase % 1.0) * 4.0
        idx = int(pos) % 4
        frac = pos - int(pos)
        e = [lo] * 4
        e[idx] = hi - (hi - lo) * frac
        e[(idx + 1) % 4] = lo + (hi - lo) * frac
        return tuple(e)  # type: ignore[return-value]
    if p.name == "wave":
        th = 2 * math.pi * phase
        return tuple(lo + (hi - lo) * (0.5 + 0.5 * math.sin(th - i * math.pi / 2)) for i in range(4))  # type: ignore[return-value]
    # all_equal
    return (hi, hi, hi, hi)


# ---- runner --------------------------------------------------------------------------------------------------

class PatternRunner:
    """Drives `engine` with the current PatternParams at PATTERN_HZ while an external lease is alive."""

    def __init__(self, engine: Engine, clock: Callable[[], float] = time.monotonic) -> None:
        self.engine = engine
        self.clock = clock
        self.params = PatternParams()
        self.running = False
        self.lease_until: float = 0.0
        self.lease_source: str | None = None
        self.envelope: Envelope | None = None
        self._task: asyncio.Task | None = None
        self._phase = 0.0
        self._last = 0.0
        self._walker: _Walker | None = None
        self.ticks = 0
        self.renewals = 0
        # external position/vector input (T-code, API) takes the axes: stop ourselves
        engine.on_external_position.append(self._on_external_position)

    def _on_external_position(self, source: str) -> None:
        if self.running:
            logger.info("pattern stop: external position input from %s", source)
            self.stop()

    # lease -------------------------------------------------------------------------------------------------

    def grant_lease(self, seconds: float, source: str = "api") -> float:
        seconds = _clip(float(seconds), 0.0, 600.0)
        self.lease_until = self.clock() + seconds
        self.lease_source = source
        return seconds

    def revoke_lease(self) -> None:
        self.lease_until = 0.0

    @property
    def lease_remaining(self) -> float:
        return max(0.0, self.lease_until - self.clock())

    @property
    def lease_alive(self) -> bool:
        return self.lease_remaining > 0.0

    # control -----------------------------------------------------------------------------------------------

    def start(self, params: dict[str, Any] | PatternParams | None = None) -> PatternParams:
        if params is not None:
            self.update(params)
        if self.running:
            return self.params
        mode = self.engine.mode or "threephase"
        self._check_mode(mode)
        self.running = True
        self._phase = 0.0
        self._last = self.clock()
        self._walker = _Walker(self.params.seed)
        self._task = asyncio.ensure_future(self._run())
        logger.info("pattern start: %s", asdict(self.params))
        return self.params

    def update(self, params: dict[str, Any] | PatternParams) -> PatternParams:
        new = params if isinstance(params, PatternParams) else PatternParams.from_dict(params, base=self.params)
        if self.running:
            self._check_mode(self.engine.mode or "threephase", new.name)
        if new.seed != self.params.seed:
            self._walker = _Walker(new.seed)
        self.params = new
        return self.params

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        logger.info("pattern stop")

    def set_envelope(self, start: float | None, end: float, seconds: float) -> Envelope:
        s = self.engine.status()["master_target"] if start is None else _clip(start, 0.0, 1.0)
        self.envelope = Envelope(s, _clip(end, 0.0, 1.0), max(0.0, float(seconds)), self.clock())
        return self.envelope

    def state(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "params": asdict(self.params),
            "lease_remaining_s": round(self.lease_remaining, 2),
            "lease_source": self.lease_source,
            "envelope": None if self.envelope is None else asdict(self.envelope),
            "ticks": self.ticks,
            "renewals": self.renewals,
        }

    # internals ---------------------------------------------------------------------------------------------

    def _check_mode(self, mode: str, name: str | None = None) -> None:
        name = name or self.params.name
        ok = THREEPHASE_PATTERNS if mode == "threephase" else FOURPHASE_PATTERNS
        if name not in ok:
            raise ValueError(f"pattern {name!r} is not a {mode} pattern (choose from {', '.join(ok)})")

    async def _run(self) -> None:
        try:
            while self.running:
                self.tick()
                await asyncio.sleep(1.0 / PATTERN_HZ)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("pattern loop died")
            self.running = False

    def tick(self) -> None:
        """One step; public for tests (deterministic with an injected clock)."""
        now = self.clock()
        dt = max(0.0, min(now - self._last, 0.25))
        self._last = now
        self._phase += self.params.rate_hz * dt
        self.ticks += 1
        eng = self.engine
        if not eng.running or eng.faulted:
            return
        mode = eng.mode or "threephase"
        if mode == "threephase":
            a, b = threephase_point(self.params, self._phase, self._walker, dt)
            eng.set_position(a, b, source=SOURCE)
        else:
            eng.set_vector(*fourphase_vector(self.params, self._phase), source=SOURCE)
        if self.envelope is not None:
            eng.set_master(self.envelope.value(now), source=SOURCE)
            if self.envelope.done(now):
                self.envelope = None
        if self.lease_alive:
            eng.renew_lease(SOURCE)
            self.renewals += 1
