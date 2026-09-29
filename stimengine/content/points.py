"""Point map — which spots on the pad feel good (PlaStim 2026-09-05: "certain points feel better than others").

A small fixed set of candidate positions on the alpha/beta disc (8 rim spokes, 8 mid-ring, centre), each with a
weight in 0..1. Neutral is 0.5. Two things read the map:

  * Moves' position picks (compose rim points, beat_lock step/random) sample candidates in proportion to weight
    (with an `explore` floor so a point can come back if it starts feeling better), instead of uniformly.
  * Tune mode (Moves) walks every candidate in a shuffled cycle, holding each for `tune_hold_ms`, so a rating is
    unambiguous.

Ratings arrive as +1 / -1 for the point that was active `latency_s` before the key press (the engine keeps a
short position history and asks `nearest()`); weights step by RATE_STEP. Optional `decay_s` (default 0 = off)
drifts weights back toward neutral so a map can be made to forget; off by default because a rated map that quietly
fades inside a session felt wrong (it is per placement and persisted — reset it deliberately instead).

Pure: no engine imports, no clock of its own. Persistence is JSON per electrode placement under
config/points/<placement>.json (moving one pad changes the whole map, so maps are named by placement).
"""
from __future__ import annotations

import json
import math
import random
import threading
from pathlib import Path
from typing import Any

NEUTRAL = 0.5
RATE_STEP = 0.15
W_MIN, W_MAX = 0.0, 1.0
RIM_R = 1.0
MID_R = 0.6
SPOKES = 8
NEAREST_MAX_D = 0.32         # a position further than this from every candidate credits nobody
PLACEMENT_RE = r"^[A-Za-z0-9_-]{1,40}$"


def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _cart(r: float, th: float) -> tuple[float, float]:
    return r * math.cos(th), r * math.sin(th)


def candidate_positions() -> dict[str, tuple[float, float]]:
    """name -> (alpha, beta). rim0..rim7 at r=1.0 (0 deg = +alpha, counter-clockwise), mid0..mid7 at 0.6, center."""
    out: dict[str, tuple[float, float]] = {}
    for k in range(SPOKES):
        th = k * 2 * math.pi / SPOKES
        out[f"rim{k}"] = _cart(RIM_R, th)
    for k in range(SPOKES):
        th = k * 2 * math.pi / SPOKES
        out[f"mid{k}"] = _cart(MID_R, th)
    out["center"] = (0.0, 0.0)
    return out


CANDIDATES = candidate_positions()
NAMES = tuple(CANDIDATES.keys())


class PointMap:
    """Thread-safe weights per candidate + sampling. `version` bumps on every change (UI refresh hint)."""

    def __init__(self, placement: str = "default", explore: float = 0.1, decay_s: float = 0.0,
                 seed: int | None = None) -> None:
        self._lock = threading.Lock()
        self.placement = placement
        self.explore = _clip(float(explore), 0.0, 1.0)
        self.decay_s = max(0.0, float(decay_s))
        self.enabled = True
        self._rng = random.Random(seed)
        self.version = 0
        self.dirty = False
        self._reset_weights()

    def _reset_weights(self) -> None:
        self.weights: dict[str, float] = {n: NEUTRAL for n in NAMES}
        self.yes: dict[str, int] = {n: 0 for n in NAMES}
        self.no: dict[str, int] = {n: 0 for n in NAMES}
        self.last_rating: dict | None = None
        self._last_decay: float | None = None

    # ---- geometry ---------------------------------------------------------------------------------
    @staticmethod
    def pos(name: str) -> tuple[float, float]:
        return CANDIDATES[name]

    @staticmethod
    def nearest(alpha: float, beta: float, max_d: float = NEAREST_MAX_D) -> str | None:
        best, bd = None, 1e9
        for n, (a, b) in CANDIDATES.items():
            d = math.hypot(a - alpha, b - beta)
            if d < bd:
                best, bd = n, d
        return best if bd <= max_d else None

    # ---- ratings ----------------------------------------------------------------------------------
    def rate(self, name: str, direction: int, now: float | None = None) -> float:
        if name not in CANDIDATES:
            raise KeyError(name)
        d = 1 if int(direction) >= 0 else -1
        with self._lock:
            w = _clip(self.weights[name] + RATE_STEP * d, W_MIN, W_MAX)
            self.weights[name] = w
            (self.yes if d > 0 else self.no)[name] += 1
            self.last_rating = {"point": name, "dir": d, "weight": round(w, 3), "t": now}
            self.version += 1
            self.dirty = True
            return w

    def set_weight(self, name: str, w: float) -> float:
        if name not in CANDIDATES:
            raise KeyError(name)
        with self._lock:
            self.weights[name] = _clip(float(w), W_MIN, W_MAX)
            self.version += 1
            self.dirty = True
            return self.weights[name]

    def reset(self) -> None:
        with self._lock:
            self._reset_weights()
            self.version += 1
            self.dirty = True

    def tick(self, now: float) -> None:
        """Drift weights toward neutral with time constant decay_s (call ~once a second or per tick; cheap)."""
        if self.decay_s <= 0:
            self._last_decay = now
            return
        with self._lock:
            if self._last_decay is None:
                self._last_decay = now
                return
            dt = max(0.0, min(now - self._last_decay, 60.0))
            self._last_decay = now
            if dt <= 0:
                return
            k = 1.0 - math.exp(-dt / self.decay_s)
            changed = False
            for n, w in self.weights.items():
                if abs(w - NEUTRAL) > 1e-4:
                    self.weights[n] = w + (NEUTRAL - w) * k
                    changed = True
            if changed:
                self.dirty = True

    # ---- sampling ---------------------------------------------------------------------------------
    def pick(self, exclude: str | None = None, rim_only: bool = False) -> str:
        """Weighted pick; with probability `explore` uniform. `exclude` avoids an immediate repeat."""
        with self._lock:
            names = [n for n in NAMES if n != exclude and (not rim_only or n.startswith("rim"))]
            if not names:
                names = list(NAMES)
            if self._rng.random() < self.explore:
                return self._rng.choice(names)
            ws = [self.weights[n] for n in names]
            if sum(ws) <= 1e-9:
                return self._rng.choice(names)
            return self._rng.choices(names, weights=ws)[0]

    def top(self, k: int = 2) -> list[str]:
        with self._lock:
            return sorted(NAMES, key=lambda n: -self.weights[n])[:max(1, int(k))]

    def shuffled(self) -> list[str]:
        names = list(NAMES)
        self._rng.shuffle(names)
        return names

    # ---- (de)serialisation -----------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            pts = {}
            for n in NAMES:
                a, b = CANDIDATES[n]
                pts[n] = {"alpha": round(a, 4), "beta": round(b, 4), "weight": round(self.weights[n], 3),
                          "yes": self.yes[n], "no": self.no[n]}
            return {"placement": self.placement, "explore": self.explore, "decay_s": self.decay_s,
                    "enabled": self.enabled, "version": self.version, "points": pts,
                    "last_rating": self.last_rating, "top": sorted(NAMES, key=lambda n: -self.weights[n])[:3]}

    def readout(self) -> dict[str, Any]:
        """Compact form for the status stream: weights only."""
        with self._lock:
            return {"placement": self.placement, "enabled": self.enabled, "version": self.version,
                    "weights": {n: round(w, 3) for n, w in self.weights.items()}, "last_rating": self.last_rating}

    def update(self, partial: dict[str, Any]) -> dict[str, Any]:
        """Partial update: explore, decay_s, enabled, weights {name: w}."""
        with self._lock:
            if "explore" in partial and partial["explore"] is not None:
                self.explore = _clip(float(partial["explore"]), 0.0, 1.0)
            if "decay_s" in partial and partial["decay_s"] is not None:
                self.decay_s = max(0.0, float(partial["decay_s"]))
            if "enabled" in partial and partial["enabled"] is not None:
                self.enabled = bool(partial["enabled"])
            for n, w in (partial.get("weights") or {}).items():
                if n in CANDIDATES and w is not None:
                    self.weights[n] = _clip(float(w), W_MIN, W_MAX)
            self.version += 1
            self.dirty = True
        return self.to_dict()

    def save(self, directory: Path | str) -> Path:
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{self.placement}.json"
        with self._lock:
            body = {"placement": self.placement, "explore": self.explore, "decay_s": self.decay_s,
                    "weights": {n: round(w, 4) for n, w in self.weights.items()},
                    "yes": dict(self.yes), "no": dict(self.no)}
            self.dirty = False
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(p)
        return p

    def load(self, directory: Path | str, placement: str | None = None) -> bool:
        """Load <placement>.json if present (else keep neutral weights). Returns True if a file was read."""
        if placement is not None:
            self.placement = placement
        p = Path(directory) / f"{self.placement}.json"
        with self._lock:
            self._reset_weights()
            self.version += 1
            self.dirty = False
            if not p.exists():
                return False
            try:
                body = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001  (a corrupt map file is not fatal: start neutral)
                return False
            for n, w in (body.get("weights") or {}).items():
                if n in CANDIDATES:
                    self.weights[n] = _clip(float(w), W_MIN, W_MAX)
            for n, c in (body.get("yes") or {}).items():
                if n in CANDIDATES:
                    self.yes[n] = int(c)
            for n, c in (body.get("no") or {}).items():
                if n in CANDIDATES:
                    self.no[n] = int(c)
            if "explore" in body:
                self.explore = _clip(float(body["explore"]), 0.0, 1.0)
            if "decay_s" in body:
                self.decay_s = max(0.0, float(body["decay_s"]))
            return True
