"""foc312: the ET-312 mode engine driven live, for the foc312 web app (foc312/, served on :8322).

One Foc312Runner per engine process. It steps an ET312Engine at the box's own 244 Hz tick (catching up in
~20 ms slices), keeps a 10 s history for the page's strips, and writes the frames to one of three outputs:

  preview  nothing leaves the process (the default; works with no device at all)
  stock    stock FOC-Stim firmware: mapping.map_frame -> apply_to_engine (sine bursts; no routing / polarity /
           pulse shape, those controls are refused with "needs fork firmware")
  fork     the stim-engine fork firmware, OUTPUT_BIPHASIC_PAIRS: per channel intensity, rate, width, asymmetry and
           route through Engine.set_biphasic (device/fork.py, firmware/NOTES.md §8)

Safety: the runner only ever writes with source="internal", so its 50 Hz writes never feed the engine's deadman.
The page's heartbeat (heartbeat()) is the control input; if the page goes quiet for deadman_silence_s the engine
ramps everything to 0. Every amp still goes through master x api x deadman x caps in the engine; arm() is the only
way master rises, and it slow-starts. The V4's hardware knob multiplies again in firmware, after all of this.

Connected pads: the page says which of the four electrodes actually have a pad on them (default all four;
persisted in config/foc312-state.json when the API gives a state path). A channel whose route uses an unconnected
electrode is sent at intensity 0 (the route itself is still sent). Reduce-only. Why: on an open route the fork
firmware's model raises the drive voltage until the sensed (magnetizing) current passes the per-sample e-stop
limit and the box latches until a power cycle (2026-09-26, pads on 1-2, preset 34/12). When the pad comes back
the channel ramps in over UNBLOCK_RAMP_S instead of jumping to its level.

Routes: the page's route codes are what the user sees (12 / 34 default; 21 = same pair, electrode 2 leads). The
fork's polarity axis is always 0. If an ET-312 mode itself sets the gate's polarity bit (no built-in mode does),
the effective route sent is the digit-swapped one, shown as such.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import deque
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable

from ..device import fork as F
from . import fwdata
from . import modes as M
from .engine import AdvancedParams, BuiltinModesUnavailable, ET312Engine, ET312Frame
from .mapping import MappingConfig, apply_to_engine, map_frame
from .vm import TICK_HZ

logger = logging.getLogger("engine.foc312")

DEFAULT_ELK_DIR = ""          # your own .elk folder: [et312] elk_dir in config/engine.toml (none by default)
OUTPUTS = ("preview", "stock", "fork")
LOOP_S = 0.02                 # wake every 20 ms, run the ticks that are due (244 Hz / 50 Hz ~ 5 per wake)
HIST_S = 10.0
HIST_DT = 0.05                # 20 Hz history samples -> 200 per 10 s strip
MAX_CATCHUP_S = 0.25
UNBLOCK_RAMP_S = 1.0          # a channel whose pads come back ramps 0 -> 1 over this, never jumps
SETUP_SAVE_S = 1.0            # pattern/output/routes/shape/ma changes are saved at most this often

# The box's own order and names (ET-312 mode numbers 0x76..0x87). User 1-7 are empty slots; .elk routines
# fill that role here.
BUILTIN_MODES: tuple[tuple[str, str], ...] = (
    ("waves", "Waves"), ("stroke", "Stroke"), ("climb", "Climb"), ("combo", "Combo"), ("intense", "Intense"),
    ("rhythm", "Rhythm"), ("audio1", "Audio 1"), ("audio2", "Audio 2"), ("audio3", "Audio 3"), ("split", "Split"),
    ("random1", "Random 1"), ("random2", "Random 2"), ("toggle", "Toggle"), ("orgasm", "Orgasm"),
    ("torment", "Torment"), ("phase1", "Phase 1"), ("phase2", "Phase 2"), ("phase3", "Phase 3"),
)
AUDIO_NOTE = "(no audio input)"

# Advanced menu, raw register ranges (buttshock protocol docs, "Advanced parameters"); defaults = AdvancedParams
ADVANCED_RANGES: dict[str, tuple[int, int]] = {
    "ramp_level": (0xDB, 0xFF), "ramp_time": (0x01, 0x78), "depth": (0xA5, 0xEC), "tempo": (0x01, 0x64),
    "freq": (0x01, 0xFF), "effect": (0x01, 0x64), "width": (0x46, 0xC8), "pace": (0x01, 0x64),
}
POWER_LEVELS = ("low", "normal", "high")


class Foc312Error(Exception):
    """A request the runner refuses (message is shown on the page)."""


# ---- pattern catalogue -------------------------------------------------------------------------------------

def _elk_module():
    try:
        from . import elk  # written by the .elk importer fork; optional
    except Exception:  # noqa: BLE001 - missing or broken importer: built-ins only
        return None
    return elk


def _is_bundled(r: dict) -> bool:
    return bool(r.get("bundled")) or str(r.get("source", "")).lower() in ("bundled", "eroslink")


def pattern_catalog(elk_dir: str | Path | None) -> tuple[list[dict], dict[str, dict], str | None]:
    """(groups, elk_by_id, error). Groups in order: "ErosLink (bundled)" (only when the importer marks entries
    bundled), "Built-in modes", "ErosLink examples" (source="designer"), "Your routines".
    Each item: {id, name, description, disabled, note}."""
    builtins = []
    for key, name in BUILTIN_MODES:
        audio = key in M.STUBBED
        builtins.append({"id": f"builtin:{key}", "name": f"{name} {AUDIO_NOTE}" if audio else name,
                         "description": ("audio modes need the box's audio input; here the intensity holds steady"
                                         if audio else ""),
                         "disabled": False, "note": AUDIO_NOTE if audio else ""})
    elk_by_id: dict[str, dict] = {}
    bundled, designer, user = [], [], []
    err = None
    mod = _elk_module()
    if mod is not None and hasattr(mod, "list_routines") and elk_dir:
        try:
            routines = list(mod.list_routines(str(elk_dir)) or [])
        except Exception as exc:  # noqa: BLE001
            routines, err = [], f".elk list failed: {exc}"
        for r in routines:
            if not isinstance(r, dict) or not r.get("path"):
                continue
            rid = "elk:" + hashlib.sha1(str(r["path"]).encode("utf-8")).hexdigest()[:12]
            item = {"id": rid, "name": str(r.get("name") or Path(str(r["path"])).stem),
                    "description": str(r.get("description") or ""), "disabled": False, "note": ""}
            elk_by_id[rid] = dict(r)
            if _is_bundled(r):
                bundled.append(item)
            elif str(r.get("source", "")).lower() == "designer":
                designer.append(item)
            else:
                user.append(item)
    groups = []
    if bundled:
        groups.append({"label": "ErosLink (bundled)", "items": bundled})
    if fwdata.default() is not None:        # ErosTek's modes: only with the user's own firmware data (fwdata.py)
        groups.append({"label": "Built-in modes", "items": builtins})
    if designer:
        groups.append({"label": "ErosLink examples", "items": designer})
    if user or mod is not None:
        groups.append({"label": "Your routines", "items": user})
    return groups, elk_by_id, err


# ---- runner ------------------------------------------------------------------------------------------------

class Foc312Runner:
    def __init__(self, engine=None, config: dict | None = None, *, pattern_runner=None,
                 clock: Callable[[], float] = time.monotonic, seed: int = 0,
                 state_path: str | Path | None = None) -> None:
        cfg = config or {}
        et = cfg.get("et312") or {}
        self.engine = engine
        self.pattern_runner = pattern_runner
        self.clock = clock
        self.elk_dir = str(et.get("elk_dir", DEFAULT_ELK_DIR))
        self.monophasic_asymmetry = float(min(4.0, max(1.0, float(et.get("monophasic_asymmetry", 3.0)))))
        self.skip_mode_ramp = bool(et.get("skip_mode_ramp", False))   # see ET312Engine.skip_mode_ramp
        self.mapping_cfg = MappingConfig.from_config(cfg) if cfg else MappingConfig()
        self.levels = [0.0, 0.0]            # knobs start at zero, like the box; always independent (PlaStim: both
                                            # together = the V4's hardware knob, the master volume)
        self.ma = 0.5
        self.power = "normal"
        self.advanced = AdvancedParams()
        self.routes = list(F.DEFAULT_ROUTES)
        self.shape = F.SHAPE_ROUNDED        # pulse shape, both channels (fork v2; charge-matched in the engine)
        self.pads = [True, True, True, True]    # which electrodes have a pad on them (persisted)
        self._pad_gain = [1.0, 1.0]         # per channel: 0 while blocked, ramps back to 1 over UNBLOCK_RAMP_S
        self._gain_t: float | None = None
        self.state_path = Path(state_path) if state_path else None
        self._load_state()
        self.output = "preview"
        self.et = ET312Engine("waves", level_a=0.0, level_b=0.0, ma=self.ma, power=self.power, seed=seed,
                              skip_mode_ramp=self.skip_mode_ramp)
        # without the ET-312 firmware data there is no Waves to start on: no pattern until one is picked (silent)
        self.pattern = ({"id": "builtin:waves", "name": "Waves"} if self.et.builtins_available
                        else {"id": None, "name": "(pick a routine)"})
        self.frame: ET312Frame = self.et.frame()
        self.hist: deque = deque(maxlen=int(HIST_S / HIST_DT) + 2)
        self.last_error: str | None = None
        self.last_heartbeat: float | None = None
        self._catalog: tuple[list[dict], dict[str, dict], str | None] | None = None
        self._acc = 0.0
        self._last = None
        self._last_hist = -1e9
        self._task: asyncio.Task | None = None
        self._running = False
        self._setup_dirty = False           # pattern/output/routes/shape/ma/power/advanced changed: save (throttled)
        self._setup_saved_t = -1e9

    # ---- lifecycle ------------------------------------------------------------------------------------------
    async def start(self) -> None:
        if self._task is None:
            self._running = True
            self._last = self.clock()
            self._task = asyncio.create_task(self._run(), name="foc312-runner")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except BaseException:  # noqa: BLE001
                pass
            self._task = None
        if self.output != "preview" and self._engine_live():
            self.engine.disarm()

    async def _run(self) -> None:
        while self._running:
            try:
                self.advance()
            except Exception:  # noqa: BLE001 - keep animating; a write failure faults the engine, not us
                logger.exception("foc312 step failed")
            if self._setup_dirty and self.clock() - self._setup_saved_t >= SETUP_SAVE_S:
                self._save_state()
            await asyncio.sleep(LOOP_S)

    def advance(self, dt: float | None = None) -> int:
        """Run the ticks due since the last call (or `dt` seconds' worth). Returns ticks run."""
        now = self.clock()
        if dt is None:
            dt = 0.0 if self._last is None else now - self._last
        self._last = now
        self._acc += max(0.0, min(dt, MAX_CATCHUP_S))
        n = int(self._acc * TICK_HZ)
        self._acc -= n / TICK_HZ
        for _ in range(n):
            self.frame = self.et.step()
        if n:
            self._after_frame(now)
        return n

    def _after_frame(self, now: float) -> None:
        self._update_pad_gain(now)
        f = self.frame
        if now - self._last_hist >= HIST_DT:
            self._last_hist = now
            self.hist.append((round(f.t, 3), round(f.a.effective, 4), round(f.b.effective, 4),
                              round(f.a.pulse_rate_hz, 1), round(f.b.pulse_rate_hz, 1)))
        self._write_outputs()

    # ---- outputs --------------------------------------------------------------------------------------------
    def _engine_live(self) -> bool:
        e = self.engine
        return bool(e is not None and e.running and e.client.is_open and not e.faulted)

    def effective_route(self, ch: int) -> int:
        """The route actually sent: the user's route, digit-swapped if the ET-312 mode set its polarity bit."""
        c = self.frame.a if ch == 0 else self.frame.b
        r = self.routes[ch]
        return F.reverse_route(r) if c.leading_polarity < 0 else r

    # ---- connected pads -------------------------------------------------------------------------------------
    def blocked(self, ch: int) -> str | None:
        """Why channel `ch` is silenced by the pad guard, or None. Its route's electrodes must all have pads."""
        missing = [d for d in F.parse_route(self.routes[ch]) if not self.pads[d - 1]]
        if not missing:
            return None
        return f"pad {missing[0]} not connected" if len(missing) == 1 else             "pads " + " and ".join(str(d) for d in missing) + " not connected"

    def _update_pad_gain(self, now: float) -> None:
        dt = 0.0 if self._gain_t is None else max(0.0, now - self._gain_t)
        self._gain_t = now
        for ch in (0, 1):
            if self.blocked(ch):
                self._pad_gain[ch] = 0.0
            else:
                self._pad_gain[ch] = min(1.0, self._pad_gain[ch] + dt / UNBLOCK_RAMP_S)

    def set_pads(self, pads) -> list[bool]:
        """pads: four truthy values for electrodes 1-4. Blocking takes effect at once; unblocking ramps in."""
        vals = list(pads)
        if len(vals) != 4:
            raise Foc312Error("pads needs four values (electrodes 1-4)")
        self.pads = [bool(v) for v in vals]
        for ch in (0, 1):
            if self.blocked(ch):
                self._pad_gain[ch] = 0.0
        self._save_state()
        self._write_outputs()
        return list(self.pads)

    def set_pad(self, n: int, on: bool) -> list[bool]:
        n = int(n)
        if not 1 <= n <= 4:
            raise Foc312Error("pad must be 1-4")
        pads = list(self.pads)
        pads[n - 1] = bool(on)
        return self.set_pads(pads)

    def _load_state(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            st = json.loads(self.state_path.read_text(encoding="utf-8"))
            pads = st.get("pads")
            if isinstance(pads, list) and len(pads) == 4:
                self.pads = [bool(v) for v in pads]
                self._pad_gain = [0.0 if self.blocked(ch) else 1.0 for ch in (0, 1)]
            setup = st.get("setup")
            self._saved_setup = setup if isinstance(setup, dict) else None
        except Exception as exc:  # noqa: BLE001 - a bad file means defaults, never a crash
            logger.warning("foc312 state unreadable (%s); all pads assumed connected", exc)

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"pads": self.pads, "setup": self.setup()}), encoding="utf-8")
            tmp.replace(self.state_path)
            self._setup_dirty = False
            self._setup_saved_t = self.clock()
        except Exception as exc:  # noqa: BLE001
            logger.warning("foc312 state not saved: %s", exc)

    # ---- setup that survives an engine restart (never the levels) --------------------------------------------
    def setup(self) -> dict[str, Any]:
        """What the page was set to, minus the levels: an engine restart (after a trip + power cycle) comes back
        with the same pattern, output, routes and shape, levels at 0 and NOT armed, like the box after a power
        cycle."""
        return {"pattern": self.pattern["id"], "output": self.output, "routes": list(self.routes),
                "shape": F.SHAPES[self.shape], "ma": self.ma, "power": self.power, "skip_mode_ramp": self.skip_mode_ramp,
                "advanced": asdict(self.advanced)}

    def _setup_changed(self) -> None:
        self._setup_dirty = True

    async def restore_setup(self) -> None:
        """Re-apply the saved setup (see setup()). Levels stay 0 and nothing is armed. Each piece is best-effort:
        a routine that has gone away or an output the box can't do is skipped, never fatal."""
        st = getattr(self, "_saved_setup", None)
        if not st:
            return
        steps = [("pattern", lambda v: self.set_pattern(v)), ("ma", lambda v: self.set_ma(v)),
                 ("power", lambda v: self.set_power(v)),
                 ("skip_mode_ramp", lambda v: self.set_skip_mode_ramp(v)),
                 ("advanced", lambda v: self.set_advanced(**v) if isinstance(v, dict) else None)]
        for key, fn in steps:
            if st.get(key) is not None:
                try:
                    fn(st[key])
                except Exception as exc:  # noqa: BLE001
                    logger.warning("foc312: saved %s not restored (%s)", key, exc)
        try:
            routes = st.get("routes")
            if isinstance(routes, list) and len(routes) == 2:
                self.routes = [F.validate_route(r) for r in routes]
            if st.get("shape") is not None:
                self.shape = F.shape_id(st["shape"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("foc312: saved routes/shape not restored (%s)", exc)
        out = st.get("output")
        if out in ("stock", "fork"):
            try:
                await self.set_output(out)        # disarmed; master stays 0 until the page's ARM
            except Exception as exc:  # noqa: BLE001
                logger.warning("foc312: saved output %s not restored (%s)", out, exc)
        self.set_levels(0.0, 0.0)
        self._setup_dirty = False
        logger.info("foc312: restored setup %s (levels 0, not armed)", self.setup())

    def channel_view(self, ch: int) -> dict[str, Any]:
        c = self.frame.a if ch == 0 else self.frame.b
        return {
            "intensity": round(c.effective, 4), "gate": c.gate_on, "level": self.levels[ch],
            "rate_hz": round(min(F.FREQ_RANGE[1], max(F.FREQ_RANGE[0], c.pulse_rate_hz)), 1),
            "rate_hz_et312": round(c.pulse_rate_hz, 1),
            "width_us": round(min(F.WIDTH_RANGE[1], max(F.WIDTH_RANGE[0], c.pulse_width_us))),
            "biphasic": c.biphasic,
            # monophasic = the asymmetric pulse, its lead polarity carried by route_sent; alternating halves
            # (gate bit3) have no per-pulse equivalent on the fork, so they play symmetric (charge-balanced pairs)
            "asymmetry": 1.0 if (c.biphasic or c.alternating) else self.monophasic_asymmetry,
            "route": self.routes[ch], "route_sent": self.effective_route(ch),
            "mode_polarity_swap": c.leading_polarity < 0,
            "shape": F.SHAPES[self.shape],
            "blocked": self.blocked(ch), "pad_gain": round(self._pad_gain[ch], 3),
        }

    def _write_outputs(self) -> None:
        if self.output == "preview" or not self._engine_live():
            return
        eng = self.engine
        if self.output == "fork" and eng.mode == "biphasic":
            for i in (0, 1):
                v = self.channel_view(i)
                if v["blocked"]:
                    self._pad_gain[i] = 0.0          # immediate: a route onto a missing pad is silent at once
                gain = 0.0 if v["blocked"] else self._pad_gain[i]
                eng.set_biphasic(i, intensity=v["intensity"] * gain, rate_hz=v["rate_hz"], width_us=v["width_us"],
                                 asymmetry=v["asymmetry"], route=v["route_sent"], shape=self.shape,
                                 source="internal")
        elif self.output == "stock" and eng.mode in ("threephase", "fourphase"):
            apply_to_engine(eng, map_frame(self.frame, replace(self.mapping_cfg, geometry=eng.mode)), source="internal")

    def outputs_available(self) -> dict[str, bool]:
        live = self._engine_live()
        return {"preview": True, "stock": live, "fork": live and bool(self.engine.fork_firmware)}

    async def set_output(self, mode: str) -> None:
        if mode not in OUTPUTS:
            raise Foc312Error(f"output must be one of {OUTPUTS}")
        eng = self.engine
        if mode == "preview":
            if self._engine_live():
                eng.disarm()
            self.output = "preview"
            self._setup_changed()
            return
        if not self._engine_live():
            raise Foc312Error("no device link: run the engine with a box (or --sim / --sim-fork)")
        if mode == "fork" and not eng.fork_firmware:
            raise Foc312Error("needs fork firmware (not flashed on this box)")
        eng.disarm()
        self._quiet_others()
        want = "biphasic" if mode == "fork" else (eng.mode if eng.mode in ("threephase", "fourphase") else "fourphase")
        if eng.mode != want or not eng.signal_on_flag:
            if eng.signal_on_flag:
                await eng.signal_off("foc312 output switch")
            await eng.signal_on(want)
        if mode == "fork":
            eng.set_api_volume(1.0, source="internal")   # under master, which is 0 until arm() slow-starts it
        self.output = mode
        self._write_outputs()
        self._setup_changed()

    def _quiet_others(self) -> None:
        """foc312 takes the engine: stop a running pattern and content follow so two writers never mix."""
        pr = self.pattern_runner or getattr(self.engine, "pattern_runner", None)
        if pr is not None and getattr(pr, "running", False):
            pr.stop()
        try:
            self.engine.follow_enable(False, source="foc312")
        except Exception:  # noqa: BLE001
            pass

    def arm(self) -> None:
        if self.output == "preview":
            raise Foc312Error("preview: nothing to arm (pick Stock or Fork output)")
        if not self._engine_live():
            raise Foc312Error("no device link")
        eng = self.engine
        eng.set_master(1.0, source="foc312")
        eng.arm()                                      # master ramps 0 -> 1 over safety.slow_start_s

    def stop_output(self) -> None:
        """STOP: immediate zero (disarm). The signal stays on; the pattern keeps animating."""
        if self.engine is not None and self.engine.running:
            self.engine.disarm()

    def heartbeat(self) -> None:
        self.last_heartbeat = self.clock()
        if self.engine is not None and self.engine.running:
            self.engine.renew_lease("foc312")

    # ---- knobs / pattern ------------------------------------------------------------------------------------
    def catalog(self, refresh: bool = False):
        if self._catalog is None or refresh:
            self._catalog = pattern_catalog(self.elk_dir)
        return self._catalog

    def set_pattern(self, pid: str) -> None:
        pid = str(pid)
        if pid.startswith("builtin:"):
            key = pid.split(":", 1)[1]
            names = dict(BUILTIN_MODES)
            if key not in names:
                raise Foc312Error(f"unknown mode {key!r}")
            try:
                self.et.set_mode(key)
            except BuiltinModesUnavailable as exc:
                raise Foc312Error(str(exc)) from None
            self.pattern = {"id": pid, "name": names[key]}
            self._setup_changed()
            return
        if pid.startswith("elk:"):
            _, elk_by_id, _ = self.catalog()
            r = elk_by_id.get(pid) or self.catalog(refresh=True)[1].get(pid)
            if r is None:
                raise Foc312Error("unknown routine (list changed? reload the page)")
            mod = _elk_module()
            if mod is None or not hasattr(mod, "load"):
                raise Foc312Error(".elk importer not available")
            try:
                prog = mod.load(r["path"])
                self.et.set_mode(prog)
            except Foc312Error:
                raise
            except Exception as exc:  # noqa: BLE001
                raise Foc312Error(f"could not load {r.get('name') or r['path']}: {exc}") from exc
            self.pattern = {"id": pid, "name": str(r.get("name") or Path(str(r["path"])).stem)}
            self._setup_changed()
            return
        raise Foc312Error("pattern id must be builtin:<mode> or elk:<id>")

    def set_levels(self, a: float | None = None, b: float | None = None) -> None:
        if a is not None:
            self.levels[0] = min(1.0, max(0.0, float(a)))
        if b is not None:
            self.levels[1] = min(1.0, max(0.0, float(b)))
        self.et.set_levels(self.levels[0], self.levels[1])

    def set_ma(self, v: float) -> None:
        self.ma = min(1.0, max(0.0, float(v)))
        self.et.set_ma(self.ma)
        self._setup_changed()

    def set_power(self, level: str) -> None:
        if level not in POWER_LEVELS:
            raise Foc312Error(f"power must be one of {POWER_LEVELS}")
        self.power = level
        self.et.power = POWER_LEVELS.index(level)
        self.et.vm.mem[_power_reg()] = self.et.power
        self._setup_changed()

    def set_advanced(self, **kw) -> None:
        clean = {}
        for k, v in kw.items():
            if k not in ADVANCED_RANGES:
                raise Foc312Error(f"unknown advanced parameter {k!r}")
            lo, hi = ADVANCED_RANGES[k]
            clean[k] = int(min(hi, max(lo, round(float(v)))))
        self.advanced = replace(self.advanced, **clean)
        self.et.set_advanced(**clean)
        self._setup_changed()

    def set_skip_mode_ramp(self, on: bool) -> None:
        """Start every mode at full ramp (compare patterns) instead of the box's ~3 s ramp. Takes effect at the
        next pattern or mode change; the current ramp is left alone."""
        self.skip_mode_ramp = bool(on)
        self.et.skip_mode_ramp = self.skip_mode_ramp
        self._setup_changed()

    def start_ramp(self) -> None:
        self.et.start_ramp()

    def set_route(self, ch: int, code) -> int:
        if self.output == "stock":
            raise Foc312Error("routing needs fork firmware (stock firmware drives one field)")
        self.routes[ch] = F.validate_route(code)
        self._write_outputs()
        self._setup_changed()
        return self.routes[ch]

    def reverse(self, ch: int) -> int:
        return self.set_route(ch, F.reverse_route(self.routes[ch]))

    def _fork_v2(self) -> bool:
        return self._engine_live() and int(getattr(self.engine, "fork_version", 0) or 0) >= 2

    def set_shape(self, shape) -> str:
        """Pulse shape for both channels: rounded / square / soft. Preview animates it; on the box it needs fork
        v2 (the engine charge-matches it to rounded, so a switch never jumps the phase charge)."""
        try:
            sid = F.shape_id(shape)
        except ValueError as exc:
            raise Foc312Error(str(exc)) from None
        if self.output != "preview" and not (self.output == "fork" and self._fork_v2()):
            raise Foc312Error("pulse shapes need fork firmware v2")
        need = F.shape_min_fork(sid)
        if self.output == "fork" and int(getattr(self.engine, "fork_version", 0) or 0) < need:
            raise Foc312Error(f"the {F.SHAPES[sid]} shape needs fork firmware v{need}")
        self.shape = sid
        self._write_outputs()
        self._setup_changed()
        return F.SHAPES[sid]

    # ---- state for the page ---------------------------------------------------------------------------------
    def capabilities(self) -> dict[str, Any]:
        fork_like = self.output in ("preview", "fork")
        pulse_shape = self.output == "preview" or (self.output == "fork" and self._fork_v2())
        return {"routing": fork_like, "polarity": fork_like, "shape": fork_like,
                "note": "" if fork_like else "needs fork firmware",
                "pulse_shape": pulse_shape, "pulse_shape_note": "" if pulse_shape else "needs fork firmware v2",
                # triangle and taper play only on fork v7+ (an older box would play them as soft square)
                "shapes_v7": self.output == "preview" or (self.output == "fork" and self._engine_live()
                                                          and int(getattr(self.engine, "fork_version", 0) or 0) >= 7)}

    def knob(self) -> dict[str, Any]:
        if not self._engine_live():
            return {"value": None, "locked": None, "note": "knob: set on device" if self.engine else "knob: preview"}
        t = self.engine.client.telemetry
        if t.device_volume is None:
            return {"value": None, "locked": None, "note": "knob: set on device"}
        return {"value": round(float(t.device_volume), 3), "locked": t.device_volume_locked, "note": ""}

    def engine_view(self) -> dict[str, Any] | None:
        e = self.engine
        if e is None:
            return None
        st = {"running": e.running, "link": e.client.transport.name if e.client.is_open else None,
              "mode": e.mode, "signal_on": e.signal_on_flag, "armed": e.armed, "master": round(e._master_now, 3),
              "api": round(e._api_target, 3), "deadman_active": e._deadman_active,
              "deadman_scale": round(e._deadman_scale, 3), "faulted": e.faulted, "fault_reason": e.fault_reason,
              "fork_firmware": bool(e.fork_firmware), "fork_version": int(getattr(e, "fork_version", 0) or 0),
              "firmware": e.client.telemetry.firmware,
              "amps_cap": e.safety.amps_cap}
        if e.mode == "biphasic":
            st["amps"] = [e.last_values.get(a) for a in F.AMP_AXES]
        else:
            st["amps"] = [e.last_values.get(11)]
        return st

    def state(self, full: bool = False) -> dict[str, Any]:
        f = self.frame
        out = {
            "t": round(f.t, 3), "output": self.output, "outputs_available": self.outputs_available(),
            "caps": self.capabilities(), "pattern": dict(self.pattern), "mode_name": f.mode_name,
            "phase_mode": f.phase_mode, "levels": list(self.levels), "ma": self.ma,
            "power": self.power, "advanced": asdict(self.advanced), "routes": list(self.routes),
            "skip_mode_ramp": self.skip_mode_ramp,
            "shape": F.SHAPES[self.shape], "pads": list(self.pads),
            "a": self.channel_view(0), "b": self.channel_view(1), "knob": self.knob(),
            "engine": self.engine_view(), "error": self.last_error,
            "sample": list(self.hist[-1]) if self.hist else None,
        }
        if full:
            out["hist"] = [list(h) for h in self.hist]
            out["advanced_ranges"] = {k: list(v) for k, v in ADVANCED_RANGES.items()}
            out["hist_s"] = HIST_S
        return out


def _power_reg() -> int:
    from .vm import R
    return R["power_level"]


__all__ = ["Foc312Runner", "Foc312Error", "pattern_catalog", "BUILTIN_MODES", "OUTPUTS", "ADVANCED_RANGES"]
