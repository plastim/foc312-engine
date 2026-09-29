"""Local HTTP + WebSocket control API (aiohttp): the engine's control surface for tools, the hub and Claude.

Binds 127.0.0.1:<api.http_port> by default (config `api.bind` to change). JSON only.
Every write is tagged source="api:<source>" (external), so it counts as control input for the engine's deadman.
Volume increases are always ramped (never stepped); decreases may be immediate.

  GET  /status             engine + pattern + lease + link + session
  POST /arm | /disarm | /stop
  POST /volume    {"level":0..1, "ramp_s":float, "source":"claude"}
  POST /position  {"alpha","beta"}          POST /vector {"e1".."e4"}
  POST /carrier   {"hz"}                    POST /pulse  {"frequency","width","rise_time","interval_random"}
  POST /pattern   {"name","rate_hz","amplitude","center":[a,b],"floor","envelope":{"to":x,"seconds":n}}
  DELETE /pattern
  POST /lease     {"seconds":N,"source":"claude"}    GET /lease
  GET  /telemetry
  POST /signal    {"on":bool}   device signal on/off (STOP really stops the box playing)
  WS   /ws        streams {"status":..,"telemetry":..} at ~12 Hz; accepts {"cmd": "<route>", ...fields}

The Stash viewer's endpoints (content follow, tracks, feel mapping, moves, points, power, cards, the Stash proxy,
the viewer page) stay with that project (stim-engine); foc312-engine is the PlaStim product.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import math
import time
from typing import Any

from aiohttp import WSMsgType, web

from ..engine import Engine, EngineError
from .origin import origin_guard
from .patterns import ALL_PATTERNS, PatternRunner

logger = logging.getLogger("engine.api")

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8321
WS_HZ = 12.0
MIN_RAMP_UP_S = 1.0       # a volume increase is never faster than this
DEFAULT_RAMP_S = 3.0
MAX_LEASE_S = 600.0
MAX_CARRIER_HZ = 2000.0
MIN_CARRIER_HZ = 500.0

class BadRequest(web.HTTPBadRequest):
    def __init__(self, msg: str) -> None:
        super().__init__(text=json.dumps({"error": msg}), content_type="application/json")


def _num(d: dict, key: str, lo: float, hi: float, required: bool = True) -> float | None:
    if key not in d or d[key] is None:
        if required:
            raise BadRequest(f"missing {key!r}")
        return None
    try:
        v = float(d[key])
    except (TypeError, ValueError):
        raise BadRequest(f"{key!r} must be a number") from None
    if math.isnan(v) or math.isinf(v):
        raise BadRequest(f"{key!r} must be finite")
    return max(lo, min(hi, v))


def _telemetry_dict(t: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in dataclasses.fields(t):
        v = getattr(t, f.name)
        if f.name == "last_update":
            continue
        if hasattr(v, "real") and callable(getattr(v, "real", None)) and not isinstance(v, (int, float)):
            v = list(v.real())
        elif isinstance(v, tuple):
            v = list(v)
        out[f.name] = v
    last = max(t.last_update.values(), default=None)
    out["age_s"] = None if last is None else round(time.monotonic() - last, 3)
    return out


async def _maybe_await(x):
    if asyncio.iscoroutine(x) or isinstance(x, asyncio.Future):
        return await x
    return x


class ControlAPI:
    """Holds the engine + pattern runner; serves HTTP/WS. `await start()` / `await stop()`."""

    def __init__(self, engine: Engine, config: dict | None = None, runner: PatternRunner | None = None) -> None:
        self.engine = engine
        api_cfg = (config or {}).get("api", {})
        self.bind = str(api_cfg.get("bind", DEFAULT_BIND))
        self.port = int(api_cfg.get("http_port", DEFAULT_PORT))
        self.runner = runner or PatternRunner(engine)
        engine.pattern_runner = self.runner
        self.app = web.Application(middlewares=[origin_guard()])   # local pages only (origin.py)
        self._routes()
        self._runner_site: web.AppRunner | None = None
        self._ramp_task: asyncio.Task | None = None
        self._ws_clients: set[web.WebSocketResponse] = set()

    # ---- lifecycle ----------------------------------------------------------------------------------------

    async def start(self) -> None:
        self._runner_site = web.AppRunner(self.app, access_log=None)
        await self._runner_site.setup()
        site = web.TCPSite(self._runner_site, self.bind, self.port)
        await site.start()
        logger.info("control API on http://%s:%d", self.bind, self.port)

    async def stop(self) -> None:
        if self._ramp_task and not self._ramp_task.done():
            self._ramp_task.cancel()
        self.runner.stop()
        for ws in list(self._ws_clients):
            await ws.close()
        if self._runner_site:
            await self._runner_site.cleanup()
            self._runner_site = None

    # ---- routing ------------------------------------------------------------------------------------------

    def _routes(self) -> None:
        r = self.app.router
        r.add_get("/status", self.h_status)
        r.add_post("/arm", self.h_arm)
        r.add_post("/disarm", self.h_disarm)
        r.add_post("/stop", self.h_stop)
        r.add_post("/volume", self.h_volume)
        r.add_post("/position", self.h_position)
        r.add_post("/vector", self.h_vector)
        r.add_post("/carrier", self.h_carrier)
        r.add_post("/pulse", self.h_pulse)
        r.add_post("/pattern", self.h_pattern)
        r.add_delete("/pattern", self.h_pattern_stop)
        r.add_post("/lease", self.h_lease)
        r.add_get("/lease", self.h_lease_get)
        r.add_get("/telemetry", self.h_telemetry)
        r.add_post("/signal", self.h_signal)
        r.add_get("/ws", self.h_ws)

    @staticmethod
    async def _body(req: web.Request) -> dict[str, Any]:
        if not req.can_read_body:
            return {}
        try:
            data = await req.json()
        except json.JSONDecodeError:
            raise BadRequest("body must be JSON") from None
        if not isinstance(data, dict):
            raise BadRequest("body must be a JSON object")
        return data

    @staticmethod
    def _src(d: dict[str, Any]) -> str:
        s = str(d.get("source") or "client")[:32]
        return f"api:{s}"

    # ---- the shared command core (HTTP and WS both land here) -----------------------------------------

    def status(self) -> dict[str, Any]:
        st = self.engine.status()
        st["pattern"] = self.runner.state()
        st["lease_remaining_s"] = round(self.runner.lease_remaining, 2)
        st["patterns_available"] = list(ALL_PATTERNS)
        return st

    async def _signal(self, on: bool, src: str) -> dict[str, Any]:
        fn = getattr(self.engine, "signal_on" if on else "signal_off", None)
        if not callable(fn):
            raise web.HTTPNotImplemented(text=json.dumps({"error": "engine has no signal on/off yet"}),
                                         content_type="application/json")
        if on:
            await _maybe_await(fn())
        else:
            self._cancel_ramp()
            self.runner.stop()
            await _maybe_await(fn(f"api stop ({src})"))
        st = self.engine.status()
        return {"ok": True, "signal_on": st.get("signal_on", st.get("running")), "armed": st.get("armed")}

    async def command(self, name: str, d: dict[str, Any]) -> dict[str, Any]:
        eng = self.engine
        src = self._src(d)
        try:
            if name == "status":
                return self.status()
            if name == "arm":
                eng.arm()
                eng.renew_lease(src)
                return {"ok": True, "armed": eng.armed}
            if name == "disarm":
                self._cancel_ramp()
                eng.disarm()
                return {"ok": True, "armed": eng.armed}
            if name == "stop":
                self._cancel_ramp()
                self.runner.stop()
                await eng.stop("api stop")
                return {"ok": True, "running": eng.running}
            if name == "volume":
                level = _num(d, "level", 0.0, 1.0)
                ramp = _num(d, "ramp_s", 0.0, 600.0, required=False)
                return self._volume(level, ramp, src)
            if name == "position":
                a = _num(d, "alpha", -1.0, 1.0)
                b = _num(d, "beta", -1.0, 1.0)
                eng.set_position(a, b, source=src)
                return {"ok": True, "alpha": a, "beta": b}
            if name == "vector":
                e = [_num(d, k, 0.0, 1.0) for k in ("e1", "e2", "e3", "e4")]
                eng.set_vector(*e, source=src)
                return {"ok": True, "e": e}
            if name == "carrier":
                hz = _num(d, "hz", MIN_CARRIER_HZ, MAX_CARRIER_HZ)
                eng.set_carrier(hz, source=src)
                return {"ok": True, "hz": hz}
            if name == "pulse":
                kw = {
                    "frequency": _num(d, "frequency", 1.0, 150.0, required=False),
                    "width": _num(d, "width", 1.0, 20.0, required=False),
                    "rise_time": _num(d, "rise_time", 0.0, 50.0, required=False),
                    "interval_random": _num(d, "interval_random", 0.0, 1.0, required=False),
                }
                if all(v is None for v in kw.values()):
                    raise BadRequest("give at least one of frequency, width, rise_time, interval_random")
                eng.set_pulse(source=src, **kw)
                return {"ok": True, **{k: v for k, v in kw.items() if v is not None}}
            if name == "pattern":
                return self._pattern(d, src)
            if name == "pattern_stop":
                self.runner.stop()
                return {"ok": True, "pattern": self.runner.state()}
            if name == "lease":
                secs = _num(d, "seconds", 0.0, MAX_LEASE_S)
                granted = self.runner.grant_lease(secs, source=src)
                eng.renew_lease(src)
                return {"ok": True, "seconds": granted, "lease_remaining_s": round(self.runner.lease_remaining, 2)}
            if name == "lease_get":
                return {"lease_remaining_s": round(self.runner.lease_remaining, 2), "source": self.runner.lease_source}
            if name == "telemetry":
                return _telemetry_dict(eng.client.telemetry)
            if name == "signal":
                if "on" not in d:
                    raise BadRequest("missing 'on'")
                return await self._signal(bool(d["on"]), src)
        except EngineError as exc:
            raise web.HTTPConflict(text=json.dumps({"error": str(exc)}), content_type="application/json") from None
        except ValueError as exc:
            raise BadRequest(str(exc)) from None
        raise BadRequest(f"unknown command {name!r}")

    def _volume(self, level: float, ramp: float | None, src: str) -> dict[str, Any]:
        eng = self.engine
        self._cancel_ramp()
        current = float(eng.status()["master_target"])
        if level <= current:
            eng.set_master(level, source=src)  # reductions may be immediate
            return {"ok": True, "level": level, "ramp_s": 0.0}
        ramp_s = DEFAULT_RAMP_S if ramp is None else max(MIN_RAMP_UP_S, ramp)
        self._ramp_task = asyncio.ensure_future(self._ramp(current, level, ramp_s, src))
        return {"ok": True, "level": level, "ramp_s": ramp_s}

    async def _ramp(self, start: float, end: float, seconds: float, src: str) -> None:
        t0 = time.monotonic()
        self.engine.renew_lease(src)  # the command itself is control input; the ramp's steps are not
        try:
            while True:
                p = min(1.0, (time.monotonic() - t0) / seconds)
                # "internal": a long ramp must not keep the deadman alive after its caller has gone quiet
                self.engine.set_master(start + (end - start) * p, source="internal")
                if p >= 1.0:
                    return
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            pass

    def _cancel_ramp(self) -> None:
        if self._ramp_task and not self._ramp_task.done():
            self._ramp_task.cancel()
        self._ramp_task = None

    def _pattern(self, d: dict[str, Any], src: str) -> dict[str, Any]:
        params = {k: v for k, v in d.items() if k in ("name", "rate_hz", "amplitude", "center", "floor", "seed")}
        env = d.get("envelope")
        if self.runner.running:
            self.runner.update(params)
        else:
            self.runner.start(params)
        if env is not None:
            if not isinstance(env, dict):
                raise BadRequest("envelope must be an object {to, seconds, from?}")
            to = _num(env, "to", 0.0, 1.0)
            secs = _num(env, "seconds", 0.0, 600.0)
            frm = _num(env, "from", 0.0, 1.0, required=False)
            if frm is None:
                frm = float(self.engine.status()["master_target"])
            if to > frm:
                secs = max(MIN_RAMP_UP_S, secs)
            self.runner.set_envelope(frm, to, secs)
        self.engine.renew_lease(src)
        return {"ok": True, "pattern": self.runner.state()}

    # ---- HTTP handlers ------------------------------------------------------------------------------------

    async def _json(self, name: str, req: web.Request, with_body: bool = True) -> web.Response:
        d = await self._body(req) if with_body else {}
        return web.json_response(await self.command(name, d))

    async def h_status(self, req):   return web.json_response(self.status())
    async def h_arm(self, req):      return await self._json("arm", req)
    async def h_disarm(self, req):   return await self._json("disarm", req)
    async def h_stop(self, req):     return await self._json("stop", req)
    async def h_volume(self, req):   return await self._json("volume", req)
    async def h_position(self, req): return await self._json("position", req)
    async def h_vector(self, req):   return await self._json("vector", req)
    async def h_carrier(self, req):  return await self._json("carrier", req)
    async def h_pulse(self, req):    return await self._json("pulse", req)
    async def h_pattern(self, req):  return await self._json("pattern", req)
    async def h_pattern_stop(self, req): return await self._json("pattern_stop", req, with_body=False)
    async def h_lease(self, req):    return await self._json("lease", req)
    async def h_lease_get(self, req): return web.json_response(await self.command("lease_get", {}))
    async def h_telemetry(self, req): return web.json_response(await self.command("telemetry", {}))

    async def h_signal(self, req):      return await self._json("signal", req)

    # ---- WebSocket ----------------------------------------------------------------------------------------

    async def h_ws(self, req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=10)
        await ws.prepare(req)
        self._ws_clients.add(ws)

        async def pump() -> None:
            try:
                while not ws.closed:
                    await ws.send_json({"status": self.status(), "telemetry": _telemetry_dict(self.engine.client.telemetry)})
                    await asyncio.sleep(1.0 / WS_HZ)
            except (ConnectionResetError, asyncio.CancelledError):
                pass

        pump_task = asyncio.ensure_future(pump())
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    d = json.loads(msg.data)
                    cmd = str(d.pop("cmd"))
                    rid = d.pop("id", None)
                    result = await self.command(cmd, d)
                    await ws.send_json({"id": rid, "cmd": cmd, "result": result})
                except web.HTTPException as exc:
                    await ws.send_json({"id": None, "error": json.loads(exc.text or '{"error":"bad request"}')["error"],
                                        "status": exc.status})
                except (KeyError, json.JSONDecodeError, TypeError) as exc:
                    await ws.send_json({"error": f"bad message: {exc}"})
        finally:
            pump_task.cancel()
            self._ws_clients.discard(ws)
        return ws
