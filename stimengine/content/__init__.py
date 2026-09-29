"""Content-driven stim: *tracks* map scene time -> stim values; the engine plays them against the viewer's
playback beacon. CONTRACT shared by Fork H (tracks/analysis/engine integration) and Fork I (API/viewer).

Rules (non-negotiable): a track only SHAPES UNDER master/cap — it can never raise output above what PlaStim set.
Paused / no beacon -> volume fades to 0 (the beacon is the heartbeat). Seeks re-sample from the new position.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class TrackSample:
    volume: float | None = None          # 0..1 envelope multiplier on api volume; None = don't touch
    alpha: float | None = None           # -1..1 (3-phase position); None = leave to pattern/manual
    beta: float | None = None
    e: tuple[float, float, float, float] | None = None   # 4-phase intensities 0..1
    # The seven axes human-authored script kits modulate (62 kits on dh, 2026-08-20): alpha, beta, volume,
    # carrier frequency, pulse frequency, pulse width, pulse rise time. A motion track should be able to drive all
    # of them via a tunable mapping layer (config [content.motion.map]); None = leave that axis alone.
    carrier_hz: float | None = None       # 500..2000
    pulse_hz: float | None = None         # 0..150 (knob value; burst gap halves it at the device)
    pulse_width: float | None = None      # 4..20 cycles
    pulse_rise_ms: float | None = None    # 2..20
    features: dict | None = None          # raw motion features for tuning/UI: energy, stroke, tempo_hz, cut, ...


@dataclass
class TrackStatus:
    scene_id: str
    kind: str                            # "motion" | "script" | "phase" | "none"
    state: str                           # "idle" | "analyzing" | "ready" | "partial" | "error"
    progress: float = 0.0                # 0..1 analyzed fraction (motion), 1.0 for scripts
    duration_ms: int = 0
    ready_until_ms: int = 0              # samples valid up to here (progressive analysis)
    error: str | None = None
    meta: dict = field(default_factory=dict)


@runtime_checkable
class Track(Protocol):
    kind: str
    duration_ms: int
    @property
    def ready_until_ms(self) -> int: ...
    def sample(self, t_ms: float) -> TrackSample: ...
    def envelope_preview(self, n: int = 240) -> list[float]: ...   # volume envelope downsampled 0..1


@runtime_checkable
class TrackStore(Protocol):
    """Owns analysis + cache. `ensure` kicks off (or resumes) work and returns immediately."""
    def status(self, scene_id: str) -> TrackStatus: ...
    def ensure(self, scene: dict, prefer: str = "auto") -> TrackStatus: ...   # scene = Stash scene dict from stash.py
    def get(self, scene_id: str) -> Track | None: ...
    def cancel(self, scene_id: str) -> None: ...


# Engine-side API implemented by Fork H on Engine (Fork I codes against these names):
#   engine.follow_enable(on: bool, source: str = "api") -> None
#   engine.follow_status() -> dict   {"enabled": bool, "scene_id": str|None, "kind": str, "position_ms": int,
#                                     "playing": bool, "sample": {...last TrackSample...}, "beat_age_s": float}
#   engine.set_track(track: Track | None, scene_id: str | None) -> None
#   The engine's existing `last_beat` (from POST /beat) is the clock: position_ms + wall-clock extrapolation while playing.
