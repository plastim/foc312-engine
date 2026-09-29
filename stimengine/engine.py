"""Engine: target state + safety stack + the 60 Hz axis streamer to one FOC-Stim.

Mirrors restim's proto_device loop (dirty axes only, interval 30 ms, full refresh every 1 s, throttle when
>20 requests are outstanding, a timed-out update is fatal) and adds the safety stack from notes/plan.md:

  master starts at 0 and needs arm()      slow-start ramp on arm        deadman on control silence
  hard amps cap (config, never > 0.2 A)   sensor hook may only reduce   fault -> stop loop, signal_stop, log
"""

from __future__ import annotations

import asyncio
import time as _time
from collections import deque
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from .device import AxisType, DeviceError, DeviceRebooted, FocStimClient, OutputMode
from .device import fork as F
from .math import ModelSet, StimFrame, VolumeParts, evaluate
from .session import SessionLogger

logger = logging.getLogger("engine")

TICK_HZ = 60
TICK_S = 1.0 / TICK_HZ
REFRESH_S = 1.0
MOVE_INTERVAL_MS = 17          # axis glide = one 60 Hz tick: as short as it goes while still continuous (PlaStim, 2026-09-28)
SHAPE_FADE_DOWN_S = 0.02       # fork v2 pulse-shape change: fade out on the old shape (about one pulse) ...
SHAPE_FADE_UP_S = 0.04         # ... then in on the new one; was 0.15 + 0.30 s until the v6 peak guard (PlaStim, 2026-09-28:
                               # the long dip made A/B comparing hard)
MAX_PENDING = 20
HARD_AMPS_CAP = 0.2
# fork pulse-shape axes logged to commands.jsonl as "pulse" records when they change: axis -> (channel, field)
_BP_LOG_AXES = {ax: ("ab"[i], name) for name, axes in (("width_us", F.WIDTH_AXES), ("shape", F.SHAPE_AXES),
                                                       ("route", F.ROUTE_AXES), ("asymmetry", F.ASYM_AXES),
                                                       ("rate_hz", F.FREQ_AXES)) for i, ax in enumerate(axes)}
FOLLOW_BEAT_STALE_S = 3.0
FOLLOW_FADE_IN_S = 0.5
FOLLOW_FADE_OUT_S = 1.0
FOLLOW_POSITION_YIELD_S = 2.0
POINTS_RATE_LATENCY_S = 0.6    # a rating credits the point active this long before the press (reaction time)
DEADMAN_LOG_EVERY_S = 30.0     # the deadman_start/clear commands are always logged; the WARNING is rate-limited
FOLLOW_LIMITS = {"volume": (0.0, 1.0), "alpha": (-1.0, 1.0), "beta": (-1.0, 1.0), "carrier_hz": (500.0, 2000.0),
                 "pulse_hz": (0.0, 150.0), "pulse_width": (4.0, 20.0), "pulse_rise_ms": (2.0, 20.0)}
ZERO_ACK_TIMEOUT_S = 1.0

MODES = {
    "threephase": OutputMode.OUTPUT_THREEPHASE,
    "fourphase": OutputMode.OUTPUT_FOURPHASE_INDIVIDUAL_ELECTRODES,
    "biphasic": F.OUTPUT_BIPHASIC_PAIRS,     # fork firmware only (device/fork.py, firmware/NOTES.md §8)
}

# set_* calls from these sources do NOT count as control input for the deadman.
INTERNAL_SOURCES = {"internal", "engine"}


class EngineError(Exception):
    """Engine refused or failed an operation."""


@dataclass
class SafetyConfig:
    slow_start_s: float = 4.0
    deadman_silence_s: float = 2.0
    deadman_ramp_down_s: float = 3.0
    amps_cap: float = 0.15

    @classmethod
    def from_config(cls, cfg: dict) -> "SafetyConfig":
        s = cfg.get("safety", {})
        amps = float(cfg.get("signal", {}).get("waveform_amplitude_amps", 0.15))
        if amps > HARD_AMPS_CAP:
            raise EngineError(f"waveform_amplitude_amps {amps} exceeds hard cap {HARD_AMPS_CAP}")
        return cls(
            slow_start_s=float(s.get("slow_start_s", 4.0)),
            deadman_silence_s=float(s.get("deadman_silence_s", 2.0)),
            deadman_ramp_down_s=float(s.get("deadman_ramp_down_s", 3.0)),
            amps_cap=amps,
        )


class Engine:
    """One engine per device link. Construct, `await start(mode)`, drive with set_*/arm, `await stop()`."""

    def __init__(
        self,
        config: dict,
        client: FocStimClient,
        session: SessionLogger | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.client = client
        self.session = session
        self.clock = clock
        self.safety = SafetyConfig.from_config(config)
        self.models = ModelSet.from_config(config)
        self.frame = StimFrame()
        self._apply_carrier_defaults(config)

        self.mode: str | None = None
        self.armed = False
        self.running = False
        self.faulted = False
        self.fault_reason: str | None = None

        self._master_target = 0.0
        self._master_now = 0.0
        self._api_target = 1.0
        self._deadman_scale = 1.0
        self._deadman_active = False
        self._deadman_last_log = -1e9
        self._last_control = self.clock()
        self.on_external_position: list = []  # callbacks(source) — see _external_position
        self._sensor_hook: Callable[[dict], None] | None = None

        # ---- device signal state (signal_off/signal_on keep the link + session open) ----
        self.signal_on_flag = False

        # ---- content follow (tracks played against the viewer beacon) ----
        from stimengine.content.mapping import MappingSet  # local import keeps engine importable standalone
        self.last_beat: dict | None = None
        from stimengine.content.moves import Moves
        self.moves = Moves()                      # notes/moves.md: felt steps / rim time / dips, between mapping and slew
        from stimengine.content.points import PointMap
        self.points = PointMap()                  # content/points.py: which pad spots feel good (shared with Moves + API)
        self.moves.points = self.points
        self._pos_hist: deque = deque(maxlen=240) # (t, alpha, beta) per tick, ~4 s: ratings credit the point felt
                                                  # POINTS_RATE_LATENCY_S before the key press
        self.follow_mapping = MappingSet.from_config(config if isinstance(config, dict) else None, moves=self.moves)
        from stimengine.content.power import Power
        self.power = Power()                      # notes/power.md: reactive axis lanes; volume lane boots in "sound"
        self.follow_mapping.power = self.power    # attached AFTER from_config: presets may carry [power.<axis>]
        self._power_drove: list[str] = []
        self.card: str | None = None              # active card name (config/feel/cards), set by the API
        self._follow_applied = False
        self._track = None
        self._track_scene: str | None = None
        self._follow_enabled = False
        self._follow_last_sample: dict | None = None
        self._follow_position_ms = 0.0
        self._follow_playing = False
        self._follow_fade = 0.0
        self._follow_axis_now: dict[str, float] = {}
        self._follow_last_log = 0.0
        self._follow_last_playhead = 0.0
        self._last_external_position = 0.0
        self._follow_axes = {"volume": True, "position": True, "pulse": False, "width": False, "rise": False, "carrier": False}
        self.pattern_runner = None  # set by the control layer so follow can yield position to a running pattern
        # fork firmware OUTPUT_BIPHASIC_PAIRS targets, per channel (A, B). intensity is 0..1 of the amps cap and
        # goes through the same volume law as every other mode (master x api x deadman, sensor may only reduce).
        self.biphasic: list[dict[str, float]] = [
            {"intensity": 0.0, "rate_hz": 50.0, "width_us": 150.0, "asymmetry": 1.0, "route": float(r),
             "shape": float(F.SHAPE_ROUNDED), "shape_sent": float(F.SHAPE_ROUNDED), "shape_t0": -1e9,
             "shape_pending": 0.0, "shape_in_t0": -1e9}
            for r in F.DEFAULT_ROUTES]

        self._loop_task: asyncio.Task | None = None
        self._old_dict: dict[int, float] = {}
        self._last_refresh = self.clock()
        self._last_tick = 0.0
        self._stopping = False
        self.updates_sent = 0
        self.ticks = 0
        self.max_latency_s = 0.0
        self.on_move_latency: list = []  # callbacks(axis, latency_s) per acked axis move
        self.last_values: dict[int, float] = {}

        self.client.on("any", self._on_notification)
        self.client.on("boot", lambda _b: self._fault("device rebooted"))

    def _apply_carrier_defaults(self, cfg: dict) -> None:
        c = cfg.get("carrier_defaults", {})
        self.frame.carrier_frequency = float(c.get("pulse_carrier_frequency", self.frame.carrier_frequency))
        self.frame.pulse_frequency = float(c.get("pulse_frequency", self.frame.pulse_frequency))
        self.frame.pulse_width = float(c.get("pulse_width", self.frame.pulse_width))

    # ---- lifecycle -------------------------------------------------------------------------------

    @property
    def fork_firmware(self) -> bool:
        """The box runs the stim-engine fork (OUTPUT_BIPHASIC_PAIRS available)."""
        return F.is_fork_firmware(self.client)

    @property
    def fork_version(self) -> int:
        """0 = stock, 1 = fork v1 (20 us width grid, half-sine), 2 = fork v2 (fractional widths, pulse shapes)."""
        return F.fork_version(self.client)

    async def start(self, mode: str, start_imu: bool | None = None) -> None:
        """Handshake (firmware -> capabilities -> IMU) then signal_start(mode) and begin streaming."""
        if mode not in MODES:
            raise EngineError(f"unknown mode {mode!r}")
        if self.running:
            raise EngineError("already running")
        if start_imu is None:
            # PlaStim 2026-08-20: IMU off by default — 103 notifications/s flooded the ESP32 WiFi bridge
            # (suspected cause of 500 ms latency spikes) and nothing uses it yet.
            start_imu = bool(self.config.get("device", {}).get("start_imu", False)) if isinstance(getattr(self, "config", None), dict) else False
        await self.client.connect_and_handshake(start_imu=start_imu)
        t = self.client.telemetry
        if mode == "threephase" and not t.threephase:
            await self.client.close()
            raise EngineError("device does not support threephase")
        if mode == "fourphase" and not t.fourphase:
            await self.client.close()
            raise EngineError("device does not support fourphase")
        if mode == "biphasic" and not self.fork_firmware:
            await self.client.close()
            raise EngineError("biphasic needs the stim-engine fork firmware (not flashed on this box)")
        if t.max_waveform_amps and self.safety.amps_cap > t.max_waveform_amps + 1e-6:
            await self.client.close()
            raise EngineError(f"amps cap {self.safety.amps_cap} exceeds device max {t.max_waveform_amps}")
        self.mode = mode
        self.frame.mode = mode  # type: ignore[assignment]
        self._master_now = 0.0
        self._master_target = 0.0
        self.armed = False
        if self.session:
            self.session.set_meta(
                config=self.config,
                firmware=t.firmware,
                board=t.board,
                transport=self.client.transport.name,
                mode=mode,
                capabilities={
                    "threephase": t.threephase,
                    "fourphase": t.fourphase,
                    "max_waveform_amps": t.max_waveform_amps,
                    "battery": t.battery_capable,
                },
            )
        # Firmware keepalive: while playing, >4 s without an axis command stops the signal, and
        # signal_start does NOT reset that timer (only axis commands do). Restim therefore sends a
        # full frame (interval 0) BEFORE signal_start; doing it after loses the race (seen on
        # hardware 2026-08-20: "Comms lost? Stopping." within the same ms as signal_start).
        self._transmit(interval_ms=0, force=True)
        try:
            await self.client.signal_start(MODES[mode])
        except DeviceError:
            if self.session:
                self.session.close("signal_start failed")
            await self.client.close()
            raise
        self._log_cmd("signal_start", mode=mode)
        self.signal_on_flag = True
        self.running = True
        self._last_refresh = self.clock()
        self._last_control = self.clock()
        self._loop_task = asyncio.create_task(self._run(), name="engine-loop")

    async def signal_off(self, reason: str = "stop") -> None:
        """Make the DEVICE leave Playing: zero (acked) -> signal_stop. Link, loop and session stay open."""
        if not self.signal_on_flag:
            return
        self.armed = False
        self._master_target = 0.0
        self._master_now = 0.0
        self._follow_enabled = False
        self._follow_fade = 0.0
        await self._send_zero_now()
        self.signal_on_flag = False
        if self.client.is_open:
            try:
                await self.client.signal_stop(timeout=ZERO_ACK_TIMEOUT_S)
                self._log_cmd("signal_stop", reason=reason)
            except Exception as exc:  # noqa: BLE001
                logger.warning("signal_stop failed: %s", exc)
        self._log_cmd("signal_off", reason=reason)

    async def signal_on(self, mode: str | None = None) -> None:
        """Re-start the device signal after signal_off (no handshake): full frame at interval 0 BEFORE
        signal_start (firmware keepalive race), then resume streaming."""
        if self.faulted:
            raise EngineError("engine faulted; restart required")
        if not self.running or not self.client.is_open:
            raise EngineError("not running")
        if self.signal_on_flag:
            raise EngineError("signal already on")
        mode = mode or self.mode or "threephase"
        if mode not in MODES:
            raise EngineError(f"unknown mode {mode!r}")
        if mode == "biphasic" and not self.fork_firmware:
            raise EngineError("biphasic needs the stim-engine fork firmware (not flashed on this box)")
        self.mode = mode
        self.frame.mode = mode  # type: ignore[assignment]
        self.armed = False
        self._master_now = 0.0
        self._master_target = 0.0
        self._old_dict = {}
        self._transmit(interval_ms=0, force=True)
        await self.client.signal_start(MODES[mode])
        self._log_cmd("signal_start", mode=mode, note="signal_on")
        self.signal_on_flag = True
        self._last_control = self.clock()

    async def stop(self, reason: str = "stop") -> None:
        """Zero volume first, then signal_stop, close link and session."""
        if self._stopping:
            return
        self._stopping = True
        self.armed = False
        self._master_target = 0.0
        self._master_now = 0.0
        self.running = False
        task = self._loop_task
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001
                pass
        await self._send_zero_now()
        if self.client.is_open:
            try:
                await self.client.signal_stop(timeout=ZERO_ACK_TIMEOUT_S)
                self._log_cmd("signal_stop", reason=reason)
            except Exception as exc:  # noqa: BLE001
                logger.warning("signal_stop failed: %s", exc)
        await self.client.close()
        if self.session:
            self.session.close(
                reason,
                faulted=self.faulted,
                fault_reason=self.fault_reason,
                updates_sent=self.updates_sent,
                max_latency_s=self.max_latency_s,
            )
        self._stopping = False

    def arm(self) -> None:
        """Allow master volume > 0; master ramps up from 0 over slow_start_s."""
        if self.faulted:
            raise EngineError("engine faulted; restart required")
        if not self.running:
            raise EngineError("not running")
        if not self.signal_on_flag:
            raise EngineError("signal is off; call signal_on first")
        self.armed = True
        self._master_now = 0.0
        self._log_cmd("arm")

    def disarm(self) -> None:
        """Immediate zero, arming state cleared. Signal stays on (restim-style 'volume 0')."""
        self.armed = False
        self._master_now = 0.0
        self._log_cmd("disarm")
        if self.running:
            self._transmit(interval_ms=0)

    # ---- control inputs --------------------------------------------------------------------------

    def _touch(self, source: str) -> None:
        if source not in INTERNAL_SOURCES:
            self._last_control = self.clock()

    def renew_lease(self, source: str = "pattern") -> None:
        """Internal generators must call this periodically or the deadman fades them out."""
        self._last_control = self.clock()

    def set_master(self, level: float, source: str = "external") -> None:
        self._master_target = _clip01(level)
        self._touch(source)
        self._log_cmd("set_master", source=source, level=self._master_target)

    def set_api_volume(self, level: float, source: str = "external") -> None:
        self._api_target = _clip01(level)
        self._touch(source)
        self._log_cmd("set_api_volume", source=source, level=self._api_target)

    def set_position(self, alpha: float, beta: float, source: str = "external") -> None:
        self.frame.alpha, self.frame.beta = float(alpha), float(beta)
        self._touch(source)
        self._external_position(source)

    def set_vector(self, e1: float, e2: float, e3: float, e4: float, source: str = "external") -> None:
        self.frame.e1, self.frame.e2 = _clip01(e1), _clip01(e2)
        self.frame.e3, self.frame.e4 = _clip01(e3), _clip01(e4)
        self._touch(source)
        self._external_position(source)

    def _external_position(self, source: str) -> None:
        """Axis exclusivity: an external position/vector input takes the axes — internal generators
        (patterns) subscribed here stop themselves. External always wins."""
        if source in INTERNAL_SOURCES:
            return
        self._last_external_position = self.clock()
        for cb in list(self.on_external_position):
            try:
                cb(source)
            except Exception:  # never let a subscriber break a control write
                logger.exception("on_external_position subscriber failed")

    def set_carrier(self, hz: float, source: str = "external") -> None:
        self.frame.carrier_frequency = float(hz)
        self._touch(source)

    def set_pulse(
        self,
        *,
        frequency: float | None = None,
        width: float | None = None,
        rise_time: float | None = None,
        interval_random: float | None = None,
        source: str = "external",
    ) -> None:
        if frequency is not None:
            self.frame.pulse_frequency = float(frequency)
        if width is not None:
            self.frame.pulse_width = float(width)
        if rise_time is not None:
            self.frame.pulse_rise_time = float(rise_time)
        if interval_random is not None:
            self.frame.pulse_interval_random = float(interval_random)
        self._touch(source)

    def set_tau(self, tau_us: float, source: str = "external") -> None:
        self.frame.tau_us = float(tau_us)
        self._touch(source)

    def set_flags(
        self,
        *,
        burst_gap: bool | None = None,
        pulse_frequency_adjustment: bool | None = None,
        playing: bool | None = None,
        source: str = "external",
    ) -> None:
        if burst_gap is not None:
            self.frame.enable_burst_gap = burst_gap
        if pulse_frequency_adjustment is not None:
            self.frame.enable_pulse_frequency_adjustment = pulse_frequency_adjustment
        if playing is not None:
            self.frame.playing = playing
        self._touch(source)

    def set_biphasic(self, channel: int, *, intensity: float | None = None, rate_hz: float | None = None,
                     width_us: float | None = None, asymmetry: float | None = None, route=None,
                     shape=None, source: str = "external") -> None:
        """Fork-firmware per-channel pulse targets (channel 0 = A, 1 = B). Values are clipped to the firmware's
        ranges; route is a two-digit code (device/fork.py) and raises fork.RouteError if invalid. Only used while
        mode == "biphasic"; the amps actually sent are intensity x volume law x amps cap (see _biphasic_values)."""
        ch = self.biphasic[int(channel)]
        if route is not None:
            ch["route"] = float(F.validate_route(route))
        if shape is not None:
            new = float(F.shape_id(shape))              # only sent to fork v2; v1 always plays rounded
            if new != ch["shape"]:
                # a shape change fades out on the old shape, switches, and fades back in (_shape_fade)
                ch["shape"] = new
                ch["shape_t0"] = self.clock()
                ch["shape_pending"] = 1.0
        if intensity is not None:
            ch["intensity"] = _clip01(intensity)
        if rate_hz is not None:
            ch["rate_hz"] = float(min(F.FREQ_RANGE[1], max(F.FREQ_RANGE[0], float(rate_hz))))
        if width_us is not None:
            ch["width_us"] = float(min(F.WIDTH_RANGE[1], max(F.WIDTH_RANGE[0], float(width_us))))
        if asymmetry is not None:
            ch["asymmetry"] = float(min(F.ASYM_RANGE[1], max(F.ASYM_RANGE[0], float(asymmetry))))
        self._touch(source)

    def set_sensor_hook(self, hook: Callable[[dict], None] | None) -> None:
        """hook(params) may mutate position/intensity keys and may only LOWER params['volume']."""
        self._sensor_hook = hook

    # ---- status ----------------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        t = self.client.telemetry
        last = max(t.last_update.values(), default=None)
        return {
            "running": self.running,
            "signal_on": self.signal_on_flag,
            "follow": self.follow_status(),
            "armed": self.armed,
            "faulted": self.faulted,
            "fault_reason": self.fault_reason,
            "link": self.client.transport.name if self.client.is_open else None,
            "mode": self.mode,
            "master": round(self._master_now, 4),
            "master_target": self._master_target,
            "api_volume": self._api_target,
            "deadman_active": self._deadman_active,
            "deadman_scale": round(self._deadman_scale, 4),
            "frame": {
                "alpha": self.frame.alpha,
                "beta": self.frame.beta,
                "e": (self.frame.e1, self.frame.e2, self.frame.e3, self.frame.e4),
                "carrier_hz": self.frame.carrier_frequency,
                "pulse_hz": self.frame.pulse_frequency,
                "pulse_width": self.frame.pulse_width,
                "pulse_rise_ms": self.frame.pulse_rise_time,
                "tau_us": self.frame.tau_us,
            },
            "amps_commanded": self.last_values.get(AxisType.AXIS_WAVEFORM_AMPLITUDE_AMPS),
            "fork_firmware": self.fork_firmware,
            "fork_version": self.fork_version,
            "biphasic": ({"a": dict(self.biphasic[0]), "b": dict(self.biphasic[1]),
                          "amps": [self.last_values.get(a) for a in F.AMP_AXES]}
                         if self.mode == "biphasic" else None),
            "telemetry_age_s": None if last is None else round(time.monotonic() - last, 3),
            "updates_sent": self.updates_sent,
            "pending": self.client.pending_count,
            "session_dir": str(self.session.dir) if self.session else None,
        }

    # ---- the loop --------------------------------------------------------------------------------

    async def _run(self) -> None:
        try:
            while self.running:
                self._tick()
                await asyncio.sleep(TICK_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._fault(f"loop exception: {exc!r}")

    def _tick(self) -> None:
        now = self.clock()
        dt = TICK_S if self._last_tick == 0.0 else max(0.0, min(now - self._last_tick, 0.25))
        self._last_tick = now
        self.ticks += 1
        if not self.client.is_open:
            self._fault(f"link closed: {self.client.closed_reason or 'unknown'}")
            return
        self._update_master(dt)
        self._follow_applied = False
        self._update_follow(now, dt)
        if not self._follow_applied:
            self._update_moves_idle(now)
        self._pos_hist.append((now, self.frame.alpha, self.frame.beta))
        self.points.tick(now)
        self._update_deadman(now, dt)
        if now - self._last_refresh >= REFRESH_S:
            self._old_dict = {}
            self._last_refresh = now
        self._transmit(interval_ms=MOVE_INTERVAL_MS)
        if self.session:
            self.session.maybe_flush()

    def _update_master(self, dt: float) -> None:
        if not self.armed:
            self._master_now = 0.0
            return
        target = self._master_target
        if self._master_now < target:
            step = dt / self.safety.slow_start_s if self.safety.slow_start_s > 0 else 1.0
            self._master_now = min(target, self._master_now + step)
        else:
            self._master_now = target  # reductions are immediate

    # ---- content follow ----------------------------------------------------------------------------

    def set_track(self, track, scene_id: str | None) -> None:
        """Install a track (content/__init__ Track protocol) for the given scene; None clears."""
        self._track = track
        self._track_scene = scene_id
        if track is not None and hasattr(track, "mapping"):
            track.mapping = self.follow_mapping  # live-tunable: one shared MappingSet
        self._follow_axis_now = {}
        self._follow_last_sample = None
        if track is not None:
            self.power.on_scene(scene_id)         # per-scene normalization, per-show max, scene build reset
        self._log_cmd("track", scene_id=scene_id, track_kind=(getattr(track, "kind", None) if track is not None else None))

    def follow_enable(self, on: bool, source: str = "api", axes: dict | None = None) -> None:
        """axes: per-axis gates {volume, position, pulse, width, rise, carrier: bool} on top of the mapping."""
        if axes:
            for k, v in axes.items():
                if k in self._follow_axes:
                    self._follow_axes[k] = bool(v)
        on = bool(on)
        if on == self._follow_enabled:
            return
        self._follow_enabled = on
        if not on:
            self._follow_fade = 0.0
            self._follow_axis_now = {}
        self._log_cmd("follow_on" if on else "follow_off", source=source, scene_id=self._track_scene)

    def follow_status(self) -> dict:
        beat = self.last_beat or {}
        age = (_time.time() - float(beat["t"])) if beat.get("t") else None
        return {
            "enabled": self._follow_enabled,
            "scene_id": self._track_scene,
            "kind": (getattr(self._track, "kind", "none") if self._track is not None else "none"),
            "position_ms": int(self._follow_position_ms),
            "playing": self._follow_playing,
            "fade": round(self._follow_fade, 3),
            "ready_until_ms": (int(getattr(self._track, "ready_until_ms", 0) or 0) if self._track is not None else 0),
            "sample": self._follow_last_sample,
            "axes": dict(self._follow_axes),
            "beat_age_s": (round(age, 2) if age is not None else None),
            "mapping_version": self.follow_mapping.version,
            "mapping_name": self.follow_mapping.name,
            "card": self.card,
            "moves": self.moves.readout(),
            "points": self.points.readout(),
            "active_point": self.active_point(),
            "power": self.power.readout(),
            "volume_source": self._volume_source(),
            "power_drove": list(self._power_drove),
            **self._follow_source_info(),
            "shape": self._follow_shape_info(),
        }

    def _volume_source(self) -> str:
        """Who set the follow volume on the last applied tick: power | mapping | none."""
        if not self._follow_applied or not self._follow_enabled:
            return "none"
        if "volume" in self._power_drove:
            return "power"
        return "mapping" if "volume" in self._follow_axis_now else "none"

    def _follow_shape_info(self) -> dict | None:
        """Shape layer (content/shape.py) readout from the track, if it has one."""
        fn = getattr(self._track, "shape_info", None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                return None
        return None

    def _follow_source_info(self) -> dict:
        """Composite tracks (content/sources.py) report which analysis drives: source/mode/reason, tempo."""
        tr = self._track
        info = {"source": (getattr(tr, "kind", "none") if tr is not None else "none"), "source_mode": "auto",
                "source_reason": None, "tempo_bpm": 0.0, "tempo_stability": 0.0}
        fn = getattr(tr, "source_info", None)
        if callable(fn):
            try:
                info.update(fn())
            except Exception:  # noqa: BLE001
                pass
        return info

    def _follow_clock(self) -> tuple[float, bool]:
        """Scene position (ms) and playing flag from the last beat, extrapolated by wall clock."""
        beat = self.last_beat
        if not beat or beat.get("positionMs") is None:
            return self._follow_position_ms, False
        age = _time.time() - float(beat.get("t") or _time.time())
        playing = beat.get("state") == "playing" and age <= FOLLOW_BEAT_STALE_S
        # Stall detection: the page says "playing" but the playhead hasn't advanced across fresh beats
        # (buffering / stuck video) -> treat as paused so the dot doesn't run away from a frozen picture.
        bt, bpos = float(beat.get("t") or 0.0), float(beat["positionMs"])
        prev = getattr(self, "_beat_prev", None)
        if prev is None or bt != prev[0]:
            if prev is not None and abs(bpos - prev[1]) < 1.0 and beat.get("state") == "playing":
                self._beat_stall_n = getattr(self, "_beat_stall_n", 0) + 1
            else:
                self._beat_stall_n = 0
            self._beat_prev = (bt, bpos)
        if getattr(self, "_beat_stall_n", 0) >= 2:      # two consecutive non-advancing beats (~2 s)
            playing = False
        pos = bpos + (age * 1000.0 if playing else 0.0)
        return pos, playing

    def _pattern_running(self) -> bool:
        r = self.pattern_runner
        return bool(r is not None and getattr(r, "running", False))

    def _update_follow(self, now: float, dt: float) -> None:
        if not self._follow_enabled or self._track is None or not self.signal_on_flag:
            return
        pos, playing = self._follow_clock()
        self._follow_position_ms = pos
        self._follow_playing = playing
        beat = self.last_beat or {}
        beat_fresh = bool(beat.get("t")) and (_time.time() - float(beat["t"])) <= FOLLOW_BEAT_STALE_S
        if beat_fresh:
            # a live page is a live operator: a fresh beat (playing OR paused) keeps the deadman quiet.
            # Paused still fades the follow volume to 0 below — the two are independent.
            self._last_control = now
        if playing:
            self._follow_fade = min(1.0, self._follow_fade + dt / FOLLOW_FADE_IN_S)
        else:
            self._follow_fade = max(0.0, self._follow_fade - dt / FOLLOW_FADE_OUT_S)
        set_ph = getattr(self._track, "set_playhead", None)
        if callable(set_ph) and (now - self._follow_last_playhead) >= 0.5:
            self._follow_last_playhead = now
            try:
                set_ph(pos)          # analyzers seek to the playhead (content/segments.py)
            except Exception:  # noqa: BLE001
                pass
        try:
            smp = self._track.sample(pos)
        except Exception as exc:  # noqa: BLE001
            logger.warning("track sample failed: %r", exc)
            return
        g = self._follow_axes
        feats = getattr(smp, "features", None) or {}
        if feats.get("pending"):
            # not analyzed at this position yet (the analyzer is seeking here): hold what we have, tell the UI
            if "volume" in self._follow_axis_now:
                self._api_target = _clip01(self._follow_axis_now["volume"] * self._follow_fade)
            self._follow_last_sample = {"t_ms": int(pos), "pending": True, "volume": None, "alpha": None, "beta": None,
                                        "e": None, "carrier_hz": None, "pulse_hz": None, "pulse_width": None,
                                        "pulse_rise_ms": None, "applied": {}, "features": feats}
            return
        if feats.get("hold") and self._follow_axis_now:
            # kit rule: a hard cut HOLDS the previous values (never zero); just keep the fade alive
            if "volume" in self._follow_axis_now:
                self._api_target = _clip01(self._follow_axis_now["volume"] * self._follow_fade)
            return
        targets: dict[str, float] = {}
        if smp.volume is not None and g["volume"]:
            targets["volume"] = float(smp.volume)
        yield_position = (now - self._last_external_position) < FOLLOW_POSITION_YIELD_S or self._pattern_running()
        if not yield_position and g["position"]:
            if smp.alpha is not None:
                targets["alpha"] = float(smp.alpha)
            if smp.beta is not None:
                targets["beta"] = float(smp.beta)
        for name, gate in (("carrier_hz", "carrier"), ("pulse_hz", "pulse"), ("pulse_width", "width"), ("pulse_rise_ms", "rise")):
            v = getattr(smp, name, None)
            if v is not None and g[gate]:
                targets[name] = float(v)
        # Moves layer (notes/moves.md): felt steps, rim time, dwell/transit, dips/fade-fight — before slew/smooth.
        # It only shapes inside the mapping's values (volume never above the mapped volume for this tick).
        # When a Power lane drives volume, Moves' dips / fade-fight are skipped (Amendment 2: Power owns volume).
        try:
            targets = self.moves.apply(targets, feats, now, dt, power_volume=self.power.active("volume"))
        except Exception as exc:  # noqa: BLE001  (never let the shaping layer break the follow loop)
            logger.warning("moves.apply failed: %r", exc)
        # Power lanes (notes/power.md): an active lane REPLACES that axis's target (and bypasses its follow gate).
        # Each lane's value is inside [min, max_now] and the axis LIMITS; master/caps clamp after as always.
        self._power_drove = []
        if self.power.any_active:
            try:
                ab = (float(targets.get("alpha", self.frame.alpha)), float(targets.get("beta", self.frame.beta)))
                targets, self._power_drove = self.power.apply(targets, feats, ab, now, dt)
            except Exception as exc:  # noqa: BLE001
                logger.warning("power.apply failed: %r", exc)
        self._follow_applied = True
        current_axis = {"volume": 0.0, "alpha": self.frame.alpha, "beta": self.frame.beta,
                        "carrier_hz": self.frame.carrier_frequency, "pulse_hz": self.frame.pulse_frequency,
                        "pulse_width": self.frame.pulse_width, "pulse_rise_ms": self.frame.pulse_rise_time}
        applied: dict[str, float] = {}
        for name, tgt in targets.items():
            cur = self._follow_axis_now.get(name)
            if cur is None:
                cur = current_axis.get(name, tgt)   # start from where the axis IS (volume from silence)
            slew = self.follow_mapping.slew(name)
            sm = self.follow_mapping.smooth(name)
            if name in self._power_drove:
                # Power lanes have their own attack/release; the mapping's smooth/slew would blunt a 0.6 s peak
                # into nothing (PlaStim: "strong hits that feel great for a second"). Apply them raw. Drift lanes
                # (Amendment 3) take the same path on purpose: a move is a deliberate one-grid-step change.
                slew, sm = 0.0, 0.0
            if sm > 0:
                tgt = cur + min(1.0, dt / sm) * (tgt - cur)   # EMA toward target
            if slew > 0:
                lo, hi = FOLLOW_LIMITS.get(name, (0.0, 1.0))
                step = abs(hi - lo) * dt / slew
                cur = cur + max(-step, min(step, tgt - cur))
            else:
                cur = tgt
            self._follow_axis_now[name] = cur
            applied[name] = cur
        if "volume" in applied:
            self._api_target = _clip01(applied["volume"] * self._follow_fade)   # under master, never above
        if "alpha" in applied or "beta" in applied:
            self.frame.alpha = float(min(1.0, max(-1.0, applied.get("alpha", self.frame.alpha))))
            self.frame.beta = float(min(1.0, max(-1.0, applied.get("beta", self.frame.beta))))
            if self.mode == "fourphase":
                # 4-phase follow: position -> electrode weights (2-3 active, sum ~1.1), NOT round-robin
                e = position_to_weights(self.frame.alpha, self.frame.beta)
                self.frame.e1, self.frame.e2, self.frame.e3, self.frame.e4 = e
        if "carrier_hz" in applied:
            self.frame.carrier_frequency = float(min(2000.0, max(500.0, applied["carrier_hz"])))
        if "pulse_hz" in applied:
            self.frame.pulse_frequency = float(min(150.0, max(0.0, applied["pulse_hz"])))
        if "pulse_width" in applied:
            self.frame.pulse_width = float(min(20.0, max(4.0, applied["pulse_width"])))
        if "pulse_rise_ms" in applied:
            self.frame.pulse_rise_time = float(min(20.0, max(2.0, applied["pulse_rise_ms"])))
        self._follow_last_sample = {
            "t_ms": int(pos), "volume": smp.volume, "alpha": smp.alpha, "beta": smp.beta, "e": smp.e,
            "carrier_hz": smp.carrier_hz, "pulse_hz": smp.pulse_hz, "pulse_width": smp.pulse_width,
            "pulse_rise_ms": smp.pulse_rise_ms, "applied": {k: round(v, 4) for k, v in applied.items()},
            "features": getattr(smp, "features", None),
        }
        if now - self._follow_last_log >= 1.0:
            self._follow_last_log = now
            self._log_cmd("follow", source="internal", t_ms=int(pos), playing=playing,
                          fade=round(self._follow_fade, 3), applied={k: round(v, 4) for k, v in applied.items()})

    # ---- Moves layer: Learn-panel step/demo when follow is idle ---------------------------------------

    def _moves_current(self, axis: str) -> float:
        if axis == "volume":
            return float(self._follow_axis_now.get("volume", self._api_target)) if self._follow_applied else float(self._api_target)
        if axis == "pulse_hz":
            return float(self.frame.pulse_frequency)
        if axis == "carrier_hz":
            return float(self.frame.carrier_frequency)
        return float(self.moves.params.edge_time)

    def _moves_write(self, axis: str, value: float, source: str) -> float:
        """Write one Moves axis straight to the frame / api target through the same clamps the API uses."""
        if axis == "volume":
            self.set_api_volume(_clip01(value), source=source)
            return self._api_target
        if axis == "pulse_hz":
            v = float(min(150.0, max(1.0, value)))
            self.set_pulse(frequency=v, source=source)
            return v
        if axis == "carrier_hz":
            v = float(min(2000.0, max(500.0, value)))
            self.set_carrier(v, source=source)
            return v
        return value

    def moves_step(self, axis: str, direction: int, source: str = "api") -> float:
        """One felt step (pulse +-10, carrier +-100, volume +-0.10 of the api target, edge_time +-0.25).
        Follow running: offsets the mapped value (cleared on the next preset/card). Idle: writes the frame."""
        from stimengine.content.moves import STEP_AXES, STEP_SIZE, snap_carrier
        if axis not in STEP_AXES:
            raise ValueError(f"axis must be one of {STEP_AXES}")
        d = 1 if int(direction) >= 0 else -1
        cur = self._moves_current(axis)
        following = self._follow_applied and axis in self._follow_axis_now
        if axis == "edge_time" or following:
            off = self.moves.step(axis, d, current=cur)
            self._log_cmd("moves_step", source=source, axis=axis, dir=d, offset=off)
            if axis == "edge_time":
                return off
            return float(cur + d * STEP_SIZE[axis]) if axis != "carrier_hz" else snap_carrier(cur + d * STEP_SIZE[axis])
        if axis == "carrier_hz":
            v = snap_carrier(cur + d * STEP_SIZE[axis])
        elif axis == "pulse_hz":
            v = round((cur + d * STEP_SIZE[axis]) / 10.0) * 10.0
        else:
            v = round(cur + d * STEP_SIZE[axis], 3)
        v = self._moves_write(axis, v, source)
        self._log_cmd("moves_step", source=source, axis=axis, dir=d, value=v)
        return v

    def moves_demo(self, axis: str, source: str = "api", seconds: float = 6.0) -> dict:
        """Swing `axis` min -> max -> current over `seconds`; the tick applies it (idle: to the frame;
        following: Moves overrides that axis inside apply())."""
        cur = self._moves_current(axis)
        d = self.moves.start_demo(axis, self.clock(), cur, seconds)
        self._log_cmd("moves_demo", source=source, axis=axis, seconds=seconds)
        return d

    def _update_moves_idle(self, now: float) -> None:
        # tune mode with no follow running: the walk still advances (position only; volume is whatever it is)
        tp = self.moves.tune_position(now)
        if tp is not None and not self._pattern_running() and (now - self._last_external_position) >= FOLLOW_POSITION_YIELD_S:
            self._write_position(*tp)
        dv = self.moves.demo_value(now)
        if dv is None:
            return
        axis, value = dv
        if axis == "edge_time":
            return
        self._moves_write(axis, value, "internal")

    def _write_position(self, alpha: float, beta: float) -> None:
        self.frame.alpha = float(min(1.0, max(-1.0, alpha)))
        self.frame.beta = float(min(1.0, max(-1.0, beta)))
        if self.mode == "fourphase":
            self.frame.e1, self.frame.e2, self.frame.e3, self.frame.e4 = position_to_weights(self.frame.alpha, self.frame.beta)

    # ---- point map ratings (content/points.py) -------------------------------------------------------------
    def active_point(self) -> str | None:
        from stimengine.content.points import PointMap
        return PointMap.nearest(self.frame.alpha, self.frame.beta)

    def points_rate(self, direction: int, latency_s: float = POINTS_RATE_LATENCY_S, source: str = "api") -> dict:
        """Rate the candidate point that was active `latency_s` ago (reaction time) +1 / -1. Returns what was credited."""
        from stimengine.content.points import PointMap
        now = self.clock()
        t_target = now - max(0.0, float(latency_s))
        a, b = self.frame.alpha, self.frame.beta
        if self._pos_hist:
            t, a, b = min(self._pos_hist, key=lambda s: abs(s[0] - t_target))
        name = PointMap.nearest(a, b)
        d = 1 if int(direction) >= 0 else -1
        out = {"dir": d, "alpha": round(a, 3), "beta": round(b, 3), "latency_s": float(latency_s)}
        if name is None:
            out.update({"ok": False, "point": None, "reason": "position is not near a candidate point"})
            return out
        w = self.points.rate(name, d, now)
        self._touch(source)
        self._log_cmd("points_rate", source=source, point=name, dir=d, weight=round(w, 3), alpha=out["alpha"], beta=out["beta"])
        out.update({"ok": True, "point": name, "weight": round(w, 3)})
        return out

    def _update_deadman(self, now: float, dt: float) -> None:
        silent = now - self._last_control
        if silent > self.safety.deadman_silence_s:
            if not self._deadman_active:
                self._deadman_active = True
                if now - self._deadman_last_log >= DEADMAN_LOG_EVERY_S:
                    self._deadman_last_log = now
                    logger.warning("deadman: no control input for %.1fs, ramping api volume to 0", silent)
                self._log_cmd("deadman_start", silent_s=round(silent, 2))
            ramp = self.safety.deadman_ramp_down_s
            step = dt / ramp if ramp > 0 else 1.0
            self._deadman_scale = max(0.0, self._deadman_scale - step)
        elif self._deadman_active:
            self._deadman_active = False
            self._deadman_scale = 1.0
            self._log_cmd("deadman_clear")

    def _volume_law(self) -> float:
        """master x api x inactivity x watchdog(deadman), clipped 0..1; the sensor hook may only lower it."""
        v = _clip01(self._master_now * self._api_target * self._deadman_scale)
        if self._sensor_hook is not None:
            params = {"volume": v}
            try:
                self._sensor_hook(params)
                v = min(v, _clip01(params.get("volume", v)))
            except Exception:  # noqa: BLE001 - a broken sensor must not raise the level
                logger.exception("sensor hook failed")
        return v

    def _shape_fade(self, ch: dict[str, float], now: float) -> float:
        """Amplitude factor 0..1 around a shape change, and the shape to send. The old shape fades to zero over
        SHAPE_FADE_DOWN_S, the new one is sent at zero and fades in over SHAPE_FADE_UP_S. Only ever reduces.
        Why: switching rounded -> square on skin tripped the firmware over-current e-stop 70 ms after the switch
        (2026-09-26, TEST-LOG); no hard transition, and the pulse-to-pulse model re-settles at low level."""
        if ch["shape_pending"]:
            dt = now - ch["shape_t0"]
            if dt < SHAPE_FADE_DOWN_S:
                return max(0.0, 1.0 - dt / SHAPE_FADE_DOWN_S)
            # the switch goes out on a tick that sends 0, and the fade-in counts from that tick: with a fade about
            # one tick long, counting from shape_t0 let the new shape start at up to ~40 % (2026-09-28)
            ch["shape_sent"] = ch["shape"]
            ch["shape_pending"] = 0.0
            ch["shape_in_t0"] = now
            return 0.0
        return min(1.0, max(0.0, (now - ch["shape_in_t0"]) / SHAPE_FADE_UP_S))

    def _biphasic_values(self) -> dict[int, float]:
        vol = self._volume_law()
        cap = float(min(self.safety.amps_cap, HARD_AMPS_CAP))
        out: dict[int, float] = {F.AXIS_BIPHASIC_INTERPHASE_GAP_US: 0.0}
        v2 = self.fork_version >= 2
        now = self.clock()
        for i, ch in enumerate(self.biphasic):
            fade = 1.0
            if v2:
                # v2 plays fractional widths (charge = the true integral), so the width goes as asked. The shape is
                # charge-matched to the rounded pulse at the same level: a shape switch never jumps the charge
                width_sent = round(float(ch["width_us"]), 1)
                fade = self._shape_fade(ch, now)
                shape = int(ch["shape_sent"])
                if F.shape_min_fork(shape) > self.fork_version:
                    shape = F.SHAPE_ROUNDED       # an older fork would clamp it to soft square: more charge than planned
                charge_factor = F.shape_charge_factor(shape, width_sent)
                out[F.SHAPE_AXES[i]] = F.shape_axis_value(shape)
            else:
                # v1 snaps width to its 20 us grid; send the snapped width and hold the phase charge continuous
                # through the amplitude. v1 knows no shape axes and always plays rounded
                width_sent, charge_factor = F.snap_width(ch["width_us"])
            # never above the cap: a clipped pulse is weaker, not stronger
            amps = float(min(cap, max(0.0, ch["intensity"] * vol * cap * charge_factor))) * fade
            out[F.AMP_AXES[i]] = round(amps, 5)
            out[F.FREQ_AXES[i]] = round(ch["rate_hz"], 1)
            out[F.WIDTH_AXES[i]] = width_sent
            out[F.ASYM_AXES[i]] = round(ch["asymmetry"], 3)
            out[F.POLARITY_AXES[i]] = 0.0          # lead swaps are route digit swaps: never a double flip
            out[F.ROUTE_AXES[i]] = float(F.validate_route(int(ch["route"])))
        return out

    def _current_values(self) -> dict[int, float]:
        if self.mode == "biphasic":
            return self._biphasic_values()
        self.frame.volume = VolumeParts(
            master=self._master_now,
            api=self._api_target * self._deadman_scale,
            inactivity=1.0,
            external=1.0,
        )
        values = evaluate(self.frame, self.models, sensor=self._sensor_hook)
        axis = AxisType.AXIS_WAVEFORM_AMPLITUDE_AMPS
        if axis in values:
            values[axis] = float(min(values[axis], self.safety.amps_cap, HARD_AMPS_CAP))
        return values

    def _transmit(self, interval_ms: int, force: bool = False) -> None:
        if not self.client.is_open or self.faulted:
            return
        if not self.signal_on_flag and not force:
            return
        if not force and self.client.pending_count > MAX_PENDING:
            return
        new = self._current_values()
        self.last_values = new
        sent_at = self.clock()
        pulse_changes: dict[str, dict[str, float]] = {}
        for axis, value in new.items():
            if force or axis not in self._old_dict or self._old_dict[axis] != value:
                name = _BP_LOG_AXES.get(axis)
                if name is not None:   # width / shape / route / asymmetry / rate: what a trip report is read against
                    pulse_changes.setdefault(name[0], {})[name[1]] = value
                iv = 0 if axis in F.IMMEDIATE_AXES else interval_ms
                fut = self.client.axis_move_to_nowait(axis, value, iv)
                fut.add_done_callback(lambda f, a=axis, t0=sent_at: self._on_move_done(f, a, t0))
                self.updates_sent += 1
                if axis == AxisType.AXIS_WAVEFORM_AMPLITUDE_AMPS:
                    self._log_cmd("amps", amps=value, interval_ms=interval_ms)
                elif axis in F.AMP_AXES:
                    self._log_cmd("amps", channel="ab"[F.AMP_AXES.index(axis)], amps=value, interval_ms=iv)
        for ch, fields in pulse_changes.items():
            self._log_cmd("pulse", channel=ch, **fields)
        self._old_dict = new

    def _on_move_done(self, fut: asyncio.Future, axis: int, t0: float) -> None:
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is None:
            lat = self.clock() - t0
            if lat > self.max_latency_s:
                self.max_latency_s = lat
            for cb in self.on_move_latency:  # observers (soak tool); must not raise
                try:
                    cb(axis, lat)
                except Exception:  # noqa: BLE001
                    logger.exception("on_move_latency subscriber failed")
            return
        if self._stopping or not self.running:
            return
        if isinstance(exc, DeviceRebooted):
            self._fault("device rebooted")
        else:
            self._fault(f"axis {axis} update failed: {exc}")

    async def _send_zero_now(self) -> None:
        """Best-effort immediate amps=0 with ack wait (used on stop)."""
        if not self.client.is_open:
            return
        try:
            if self.mode == "biphasic":
                for axis in F.AMP_AXES:
                    await self.client.axis_move_to(axis, 0.0, 0, timeout=ZERO_ACK_TIMEOUT_S)
            await self.client.axis_move_to(AxisType.AXIS_WAVEFORM_AMPLITUDE_AMPS, 0.0, 0, timeout=ZERO_ACK_TIMEOUT_S)
            self._log_cmd("amps", amps=0.0, interval_ms=0, note="zero-on-stop")
        except Exception as exc:  # noqa: BLE001
            logger.warning("zero-on-stop not acked: %s", exc)

    def _fault(self, reason: str) -> None:
        if self.faulted:
            return
        self.faulted = True
        self.fault_reason = reason
        self.armed = False
        self._master_now = 0.0
        self.running = False
        logger.error("FAULT: %s", reason)
        self._log_cmd("fault", reason=reason)
        task = self._loop_task
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
        if self.client.is_open and not isinstance(self.client.closed_reason, DeviceRebooted):
            asyncio.ensure_future(self._fault_shutdown(reason))
        else:
            asyncio.ensure_future(self.client.close())
            if self.session:
                self.session.close(f"fault: {reason}", faulted=True, fault_reason=reason)

    async def _fault_shutdown(self, reason: str) -> None:
        try:
            await self.client.close(stop_signal=True)
        finally:
            if self.session:
                self.session.close(f"fault: {reason}", faulted=True, fault_reason=reason)

    # ---- helpers ---------------------------------------------------------------------------------

    def _on_notification(self, kind: str, body: Any) -> None:
        if self.session:
            self.session.log_telemetry(kind, body)

    def _log_cmd(self, kind: str, source: str = "engine", **fields: Any) -> None:
        if self.session:
            self.session.log_command(kind, source=source, **fields)


def position_to_weights(alpha: float, beta: float, total: float = 1.1) -> tuple[float, float, float, float]:
    """Map a unit-disc position onto four electrodes at 0/90/180/270 deg: w_i = max(0, 0.25 + 0.75*(p.u_i)),
    normalized to sum `total` (kit analysis: 2-3 electrodes active, weights sum ~1.1). Center -> all equal."""
    import math as _m
    r = min(1.0, _m.hypot(alpha, beta))
    if r < 1e-6:
        w = [1.0, 1.0, 1.0, 1.0]
    else:
        ux, uy = alpha / r, beta / r
        dots = (ux, uy, -ux, -uy)
        w = [max(0.0, 0.25 + 0.75 * d * r + 0.25 * (1 - r)) for d in dots]
    s = sum(w) or 1.0
    return tuple(float(min(1.0, x * total / s)) for x in w)  # type: ignore[return-value]


def _clip01(v: float) -> float:
    return float(min(1.0, max(0.0, float(v))))


__all__ = ["Engine", "EngineError", "SafetyConfig", "MODES", "HARD_AMPS_CAP"]
