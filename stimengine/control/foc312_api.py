"""foc312 HTTP + WebSocket API on its own port (default 127.0.0.1:8322), same process as the engine.

Separate app from the video viewer (:8321), same engine: one process owns the box and the safety stack.

  GET  /                 the player page (player/index.html); /static/* its files
  GET  /state            full state incl. 10 s history          GET /patterns   grouped pattern list
  POST /cmd              {"cmd": <name>, ...}  (the same commands the WS accepts)
  WS   /ws               pushes state at ~20 Hz; accepts {"cmd": ...}; {"cmd": "hb"} is the page heartbeat

Commands: hb | pattern {id} | levels {a?, b?} | ma {value} | power {level} | advanced {k: v..} | ramp |
          route {ch: "a"|"b", code} | reverse {ch} | routes {a, b} | output {mode: preview|stock|fork} | arm | stop
Every command except hb also counts as a heartbeat. The heartbeat is the deadman's control input: if the page
goes quiet for the engine's deadman_silence_s, the engine ramps the output to zero.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from .origin import origin_guard

from ..device import fork as F
from ..et312.foc312 import Foc312Error, Foc312Runner

logger = logging.getLogger("engine.foc312.api")

# the player page ("foc312" now names the firmware fork; the Python names here stay foc312: internal)
PLAYER_DIR = Path(__file__).resolve().parents[2] / "player"
FOC312_DIR = PLAYER_DIR        # the old name, kept for callers
STATE_PATH = Path(__file__).resolve().parents[2] / "config" / "foc312-state.json"   # connected pads (gitignored)
DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8322
WS_HZ = 20.0


def _num01(d: dict, key: str) -> float | None:
    if key not in d or d[key] is None:
        return None
    try:
        v = float(d[key])
    except (TypeError, ValueError):
        raise Foc312Error(f"{key!r} must be a number") from None
    if math.isnan(v) or math.isinf(v):
        raise Foc312Error(f"{key!r} must be finite")
    return min(1.0, max(0.0, v))


def _ch(d: dict) -> int:
    c = str(d.get("ch", "")).lower()
    if c not in ("a", "b"):
        raise Foc312Error("ch must be 'a' or 'b'")
    return 0 if c == "a" else 1


class Foc312API:
    def __init__(self, engine=None, config: dict | None = None, *, pattern_runner=None,
                 runner: Foc312Runner | None = None) -> None:
        cfg = (config or {}).get("foc312") or {}
        self.bind = str(cfg.get("bind", DEFAULT_BIND))
        self.port = int(cfg.get("port", DEFAULT_PORT))
        state_path = cfg.get("state_path", STATE_PATH)
        self.runner = runner or Foc312Runner(engine, config, pattern_runner=pattern_runner,
                                             state_path=state_path or None)
        self.app = web.Application(middlewares=[origin_guard()])   # local pages only (origin.py)
        self._routes()
        self._site_runner: web.AppRunner | None = None
        self._ws_clients: set[web.WebSocketResponse] = set()
        self._push_task: asyncio.Task | None = None

    # ---- lifecycle ------------------------------------------------------------------------------------------
    async def start(self) -> None:
        await self.runner.start()
        await self.runner.restore_setup()   # after a restart: same pattern/output/routes/shape; levels 0, not armed
        self._site_runner = web.AppRunner(self.app, access_log=None)
        await self._site_runner.setup()
        await web.TCPSite(self._site_runner, self.bind, self.port).start()
        self._push_task = asyncio.create_task(self._push_loop(), name="foc312-ws-push")
        logger.info("foc312 on http://%s:%d/", self.bind, self.port)

    async def stop(self) -> None:
        if self._push_task is not None:
            self._push_task.cancel()
            self._push_task = None
        for ws in list(self._ws_clients):
            await ws.close()
        await self.runner.stop()
        if self._site_runner is not None:
            await self._site_runner.cleanup()
            self._site_runner = None

    def _routes(self) -> None:
        r = self.app.router
        r.add_get("/", self.h_index)
        r.add_get("/state", self.h_state)
        r.add_get("/patterns", self.h_patterns)
        r.add_post("/cmd", self.h_cmd)
        r.add_get("/ws", self.h_ws)
        if FOC312_DIR.is_dir():
            r.add_static("/static/", FOC312_DIR, show_index=False)
        self.app.on_response_prepare.append(self._no_cache)

    @staticmethod
    async def _no_cache(req: web.Request, resp: web.StreamResponse) -> None:
        if req.path == "/" or req.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"

    # ---- commands -------------------------------------------------------------------------------------------
    async def apply(self, d: dict[str, Any]) -> dict[str, Any]:
        """Run one command; returns {"ok": True, ...} or raises Foc312Error / fork.RouteError."""
        run = self.runner
        cmd = str(d.get("cmd", ""))
        run.heartbeat()                          # any command from the page is a live operator
        if cmd == "hb":
            return {"ok": True}
        if cmd == "pattern":
            run.set_pattern(str(d.get("id", "")))
        elif cmd == "levels":
            run.set_levels(_num01(d, "a"), _num01(d, "b"))
        elif cmd == "master":
            run.set_master(_num01(d, "value"))
        elif cmd == "ma":
            v = _num01(d, "value")
            if v is None:
                raise Foc312Error("missing 'value'")
            run.set_ma(v)
        elif cmd == "power":
            run.set_power(str(d.get("level", "")))
        elif cmd == "advanced":
            run.set_advanced(**{k: v for k, v in d.items() if k != "cmd"})
        elif cmd == "ramp":
            run.start_ramp()
        elif cmd == "skip_ramp":
            run.set_skip_mode_ramp(bool(d.get("on")))
        elif cmd == "route":
            return {"ok": True, "code": run.set_route(_ch(d), d.get("code"))}
        elif cmd == "routes":
            a, b = F.validate_route(d.get("a")), F.validate_route(d.get("b"))
            run.set_route(0, a)
            run.set_route(1, b)
        elif cmd == "swap":
            return {"ok": True, "routes": run.swap()}
        elif cmd == "reverse":
            return {"ok": True, "code": run.reverse(_ch(d))}
        elif cmd == "pads":
            if "pad" in d:
                return {"ok": True, "pads": run.set_pad(d.get("pad"), bool(d.get("on")))}
            return {"ok": True, "pads": run.set_pads(d.get("pads") or [])}
        elif cmd == "shape":
            return {"ok": True, "shape": run.set_shape(d.get("value", ""))}
        elif cmd == "output":
            await run.set_output(str(d.get("mode", "")))
        elif cmd == "arm":
            run.arm()
        elif cmd == "stop":
            run.stop_output()
        else:
            raise Foc312Error(f"unknown command {cmd!r}")
        return {"ok": True}

    async def _apply_safe(self, d: dict[str, Any]) -> tuple[dict[str, Any], int]:
        try:
            res = await self.apply(d)
            if d.get("cmd") != "hb":
                self.runner.last_error = None
            return res, 200
        except (Foc312Error, F.RouteError) as exc:
            self.runner.last_error = str(exc)
            return {"ok": False, "error": str(exc)}, 400
        except Exception as exc:  # noqa: BLE001 - engine refusals (EngineError) etc.
            logger.warning("foc312 command %r failed: %s", d.get("cmd"), exc)
            self.runner.last_error = str(exc)
            return {"ok": False, "error": str(exc)}, 409

    # ---- handlers -------------------------------------------------------------------------------------------
    async def h_index(self, req: web.Request) -> web.StreamResponse:
        idx = FOC312_DIR / "index.html"
        if not idx.exists():
            return web.Response(status=404, text="player/index.html missing")
        return web.FileResponse(idx)

    async def h_state(self, req: web.Request) -> web.Response:
        return web.json_response(self.runner.state(full=True))

    async def h_patterns(self, req: web.Request) -> web.Response:
        groups, _, err = self.runner.catalog(refresh=req.query.get("refresh") == "1")
        return web.json_response({"groups": groups, "error": err, "elk_dir": self.runner.elk_dir})

    async def h_cmd(self, req: web.Request) -> web.Response:
        try:
            d = await req.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "body must be JSON"}, status=400)
        if not isinstance(d, dict):
            return web.json_response({"ok": False, "error": "body must be an object"}, status=400)
        res, status = await self._apply_safe(d)
        return web.json_response(res, status=status)

    async def h_ws(self, req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=10.0)
        await ws.prepare(req)
        self._ws_clients.add(ws)
        try:
            await ws.send_json({"type": "state", "full": True, "state": self.runner.state(full=True)})
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    d = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(d, dict):
                    continue
                res, _ = await self._apply_safe(d)
                if d.get("cmd") != "hb":
                    await ws.send_json({"type": "result", "cmd": d.get("cmd"), "seq": d.get("seq"), **res})
        finally:
            self._ws_clients.discard(ws)
        return ws

    async def _push_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0 / WS_HZ)
            if not self._ws_clients:
                continue
            payload = {"type": "state", "full": False, "state": self.runner.state()}
            for ws in list(self._ws_clients):
                try:
                    await ws.send_json(payload)
                except Exception:  # noqa: BLE001
                    self._ws_clients.discard(ws)


__all__ = ["Foc312API", "FOC312_DIR", "DEFAULT_PORT"]
