"""The hub's HTTP server (aiohttp, 127.0.0.1:8320): the page in app/ and the JSON API it drives.

    GET  /api/devices[?probe=1|2]   USB serial devices; probe=1 asks the ones not identified yet, 2 all of them
    GET  /api/firmware              flashable images (box, remote), hashes checked
    GET  /api/engine                the engine child (running, port, log)
    POST /api/engine/connect        {"port"}      start the engine on that box
    POST /api/engine/disconnect                   stop it (releases the port)
    POST /api/flash/box             {"port", "image", "confirm": true, "remote_off": true}
    POST /api/flash/remote          {"port", "image", "confirm": true}
    POST /api/remote/load           {"port"}      patterns + settings onto the remote
    POST /api/remote/pair           {"box_port", "house": bool}   a box's Wi-Fi: the remote's network or the house
    GET  /api/jobs, /api/jobs/{id}  job output
    GET  /api/hotkeys               global hotkeys (Windows)
    GET  /api/engine/status         what the engine and the box are doing (the Play tab's status panel)
    GET  /api/remote/settings       the M5 remote's settings (config/m5.toml, no passwords), what a load sends, last load
    PUT  /api/remote/settings       save them (validated as a load would; an empty password keeps the stored one)
    GET  /api/remote/patterns       the pattern files a load would put on the remote, per group
    GET  /api/et312                 whether the ET-312 built-in mode data is available, and from where
    POST /api/et312/extract         the user's own ET-312 v1.6 firmware image -> config/et312-firmware-data.json

Nothing that writes to a device runs on the port the engine owns: disconnect first.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from ..control.origin import origin_guard
from . import devices, firmware, m5settings, status, updates
from .engine_proc import EngineProc
from .jobs import ROOT, JobBusy, JobManager

logger = logging.getLogger(__name__)

APP_DIR = ROOT / "app"
DEFAULT_PORT = 8320
FOC312_CMD = "http://127.0.0.1:8322/cmd"
FOC312_STATE = "http://127.0.0.1:8322/state"
LEVEL_STEP = 0.01
EXTRACT_MAX_BYTES = 256 * 1024       # an ET-312 image is 16 KB (.bin) or ~45 KB (.hex)


def _err(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": msg}, status=status)


async def _body(req: web.Request) -> dict:
    try:
        d = await req.json()
    except Exception:  # noqa: BLE001
        return {}
    return d if isinstance(d, dict) else {}


class Hub:
    def __init__(self, engine: EngineProc | None = None, jobs: JobManager | None = None) -> None:
        self.engine = engine or EngineProc()
        self.jobs = jobs or JobManager()
        self.hotkeys = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.app = web.Application(middlewares=[origin_guard()])   # local pages only (control/origin.py)
        r = self.app.router
        r.add_get("/api/devices", self.h_devices)
        r.add_get("/api/firmware", self.h_firmware)
        r.add_get("/api/updates", self.h_updates)
        r.add_post("/api/updates/check", self.h_updates_check)
        r.add_post("/api/updates/download", self.h_updates_download)
        r.add_get("/api/engine", self.h_engine)
        r.add_post("/api/engine/connect", self.h_connect)
        r.add_post("/api/engine/disconnect", self.h_disconnect)
        r.add_post("/api/flash/box", self.h_flash_box)
        r.add_post("/api/flash/remote", self.h_flash_remote)
        r.add_post("/api/remote/load", self.h_remote_load)
        r.add_post("/api/remote/pair", self.h_remote_pair)
        r.add_get("/api/jobs", self.h_jobs)
        r.add_get("/api/jobs/{id}", self.h_job)
        r.add_get("/api/hotkeys", self.h_hotkeys)
        r.add_get("/api/settings/cap", self.h_cap)
        r.add_put("/api/settings/cap", self.h_cap_put)
        r.add_get("/api/engine/status", self.h_engine_status)
        r.add_get("/api/remote/settings", self.h_remote_settings)
        r.add_put("/api/remote/settings", self.h_remote_settings_put)
        r.add_get("/api/remote/patterns", self.h_remote_patterns)
        r.add_get("/api/et312", self.h_et312)
        r.add_post("/api/et312/extract", self.h_et312_extract)
        # a finished load records what the remote got, so the page can say whether it still matches
        self.jobs.on_done["remote-load"] = m5settings.record_job
        r.add_get("/", self.h_index)
        if APP_DIR.is_dir():
            r.add_static("/static/", APP_DIR, show_index=False)
        self.app.on_startup.append(self._startup)
        self.app.on_cleanup.append(self._cleanup)

    # ---- lifecycle -------------------------------------------------------------------------------------------
    async def _startup(self, _app: web.Application) -> None:
        self.loop = asyncio.get_running_loop()
        try:
            from .hotkeys import Hotkeys           # the front end's module; the hub runs without it
        except ImportError:
            return
        try:
            self.hotkeys = Hotkeys(self._hotkey)
            self.hotkeys.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning("global hotkeys not available: %s", exc)
            self.hotkeys = None

    async def _cleanup(self, _app: web.Application) -> None:
        if self.hotkeys is not None:
            try:
                self.hotkeys.stop()
            except Exception:  # noqa: BLE001
                pass
        if self.engine.running():
            await asyncio.get_running_loop().run_in_executor(None, self.engine.disconnect)

    # ---- hotkeys -> foc312 -----------------------------------------------------------------------------------
    def _hotkey(self, action: str) -> None:
        """Called from the hotkey thread: hand the action to the event loop."""
        if self.loop is not None:
            self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.hotkey_action(action)))

    async def hotkey_action(self, action: str) -> None:
        if not self.engine.running():
            return
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as s:
                if action == "stop":
                    await s.post(FOC312_CMD, json={"cmd": "stop"})
                    return
                delta = {"level_up": (1, 1), "level_down": (-1, -1), "a_up": (1, 0), "a_down": (-1, 0),
                         "b_up": (0, 1), "b_down": (0, -1)}.get(action)
                if delta is None:
                    return
                async with s.get(FOC312_STATE) as resp:
                    state = await resp.json()
                lv = state.get("levels") or [0.0, 0.0]
                a = min(1.0, max(0.0, float(lv[0]) + delta[0] * LEVEL_STEP))
                b = min(1.0, max(0.0, float(lv[1]) + delta[1] * LEVEL_STEP))
                await s.post(FOC312_CMD, json={"cmd": "levels", "a": a, "b": b})
        except Exception as exc:  # noqa: BLE001 - the engine may be starting or gone
            logger.info("hotkey %s not applied: %s", action, exc)

    # ---- handlers ---------------------------------------------------------------------------------------------
    async def h_index(self, _req: web.Request) -> web.StreamResponse:
        index = APP_DIR / "index.html"
        if index.exists():
            return web.FileResponse(index)
        return web.Response(text="app/index.html is missing", content_type="text/plain")

    async def h_devices(self, req: web.Request) -> web.Response:
        try:
            mode = int(req.query.get("probe", "0"))
        except ValueError:
            mode = 0
        in_use = {self.engine.port} if self.engine.running() and self.engine.port else set()
        devs = await devices.scan(in_use, mode)
        for d in devs:
            if d["in_use"] and d["kind"] == "unknown":
                d["kind"], d["detail"] = "box", "engine connected"
        return web.json_response({"devices": devs})

    async def h_firmware(self, _req: web.Request) -> web.Response:
        return web.json_response(firmware.listing())

    # ---- firmware updates (GitHub releases, signed by PlaStim): never automatic, downloading is not flashing ----
    async def h_updates(self, _req: web.Request) -> web.Response:
        u = firmware.updater()
        return web.json_response({"box": u.last.get("box"), "remote": u.last.get("remote")})

    async def h_updates_check(self, req: web.Request) -> web.Response:
        kind = str((await _body(req)).get("kind", ""))
        if kind not in updates.PRODUCTS:
            return _err("kind must be 'box' or 'remote'")
        res = await asyncio.get_running_loop().run_in_executor(None, firmware.updater().check, kind)
        return web.json_response(res)

    async def h_updates_download(self, req: web.Request) -> web.Response:
        d = await _body(req)
        kind = str(d.get("kind", ""))
        if kind not in updates.PRODUCTS:
            return _err("kind must be 'box' or 'remote'")
        u, loop = firmware.updater(), asyncio.get_running_loop()
        try:
            if d.get("stock"):
                if kind != "box":
                    return _err("stock firmware is for the box")
                e = await loop.run_in_executor(None, u.download_stock, str(d["stock"]))
            else:
                e = await loop.run_in_executor(None, u.download, kind, str(d.get("tag", "")))
        except updates.UpdateError as exc:
            return _err(str(exc))
        return web.json_response({"ok": True, "image": firmware._public(e)})

    async def h_engine(self, _req: web.Request) -> web.Response:
        return web.json_response(self.engine.status())

    async def h_connect(self, req: web.Request) -> web.Response:
        port = str((await _body(req)).get("port", "")).strip()
        if not port:
            return _err("missing 'port'")
        if any(j.flash and j.running() for j in self.jobs.jobs.values()):
            return _err("a flashing job is running", 409)
        try:
            self.engine.connect(port)
        except (RuntimeError, OSError) as exc:
            return _err(str(exc), 409)
        return web.json_response({"ok": True, **self.engine.status()})

    async def h_disconnect(self, _req: web.Request) -> web.Response:
        rc = await asyncio.get_running_loop().run_in_executor(None, self.engine.disconnect)
        return web.json_response({"ok": True, "rc": rc, **self.engine.status()})

    def _start(self, kind: str, cmd: list[str], flash: bool = False) -> web.Response:
        try:
            job = self.jobs.start(kind, cmd, flash=flash)
        except JobBusy as exc:
            return _err(str(exc), 409)
        return web.json_response({"ok": True, "job": job.id})

    def _port_free(self, port: str) -> str | None:
        if self.engine.owns(port):
            return f"the engine is connected to {port}: disconnect it first"
        return None

    async def h_flash_box(self, req: web.Request) -> web.Response:
        d = await _body(req)
        port = str(d.get("port", "")).strip()
        if not port:
            return _err("missing 'port'")
        if d.get("confirm") is not True:
            return _err("flashing needs \"confirm\": true")
        if d.get("remote_off") is not True:
            # the box's ESP32 passes Wi-Fi traffic to the STM32 bootloader too: a connected remote broke a flash
            return _err("switch the remote off (or out of range) first, then confirm \"remote_off\": true")
        if devices.known_kind(port) == "remote":
            return _err(f"{port} is the M5 remote, not a box")
        busy = self._port_free(port)
        if busy:
            return _err(busy, 409)
        try:
            im, path = firmware.find("box", str(d.get("image", "")))
        except ValueError as exc:
            return _err(str(exc))
        return self._start("flash-box", [sys.executable, str(ROOT / "tools" / "flash_focstim.py"), str(path),
                                         "--sha256", im["sha256"], "--port", port], flash=True)

    async def h_flash_remote(self, req: web.Request) -> web.Response:
        d = await _body(req)
        port = str(d.get("port", "")).strip()
        if not port:
            return _err("missing 'port'")
        if d.get("confirm") is not True:
            return _err("flashing needs \"confirm\": true")
        # esptool would happily flash a FOC-Stim's own ESP32: only a port detected as the M5 remote
        refuse = self._not_remote(port)
        if refuse:
            return _err(refuse)
        busy = self._port_free(port)
        if busy:
            return _err(busy, 409)
        try:
            _im, path = firmware.find("remote", str(d.get("image", "")))
        except ValueError as exc:
            return _err(str(exc))
        return self._start("flash-remote", [sys.executable, "-m", "esptool", "--chip", "esp32s3", "--port", port,
                                            "--baud", "921600", "write-flash", "0x0", str(path)], flash=True)

    async def h_remote_load(self, req: web.Request) -> web.Response:
        port = str((await _body(req)).get("port", "")).strip()
        if not port:
            return _err("missing 'port'")
        refuse = self._not_remote(port)
        if refuse:
            return _err(refuse)
        busy = self._port_free(port)
        if busy:
            return _err(busy, 409)
        return self._start("remote-load", [sys.executable, "-m", "stimengine.remote", "load", "--port", port])

    async def h_remote_pair(self, req: web.Request) -> web.Response:
        d = await _body(req)
        port = str(d.get("box_port", "")).strip()
        if not port:
            return _err("missing 'box_port'")
        if devices.known_kind(port) == "remote":
            return _err(f"{port} is the M5 remote, not a box")
        busy = self._port_free(port)
        if busy:
            return _err(busy, 409)
        cmd = [sys.executable, "-m", "stimengine.remote", "pair-box", "--box-port", port]
        if d.get("house"):
            cmd.append("--house")
        return self._start("remote-pair", cmd)

    @staticmethod
    def _not_remote(port: str) -> str | None:
        kind = devices.known_kind(port)
        if kind == "remote":
            return None
        if kind == "box":
            return f"{port} is a FOC-Stim box, not the M5 remote"
        return f"{port} has not been detected as the M5 remote yet: press Detect"

    async def h_jobs(self, _req: web.Request) -> web.Response:
        return web.json_response({"jobs": self.jobs.recent()})

    async def h_job(self, req: web.Request) -> web.Response:
        try:
            job = self.jobs.get(int(req.match_info["id"]))
        except ValueError:
            job = None
        if job is None:
            return _err("no such job", 404)
        return web.json_response(job.view())

    async def h_cap(self, _req: web.Request) -> web.Response:
        from . import capsetting
        return web.json_response({"amps": capsetting.read(), "min": capsetting.MIN_AMPS,
                                  "max": capsetting.HARD_AMPS_CAP, "engine_running": self.engine.running()})

    async def h_cap_put(self, req: web.Request) -> web.Response:
        from . import capsetting
        try:
            amps = capsetting.write((await _body(req)).get("amps"))
        except (TypeError, ValueError, OSError) as exc:
            return _err(str(exc))
        note = ("Saved. It applies the next time the engine connects (disconnect and connect now to use it)."
                if self.engine.running() else "Saved. It applies the next time the engine connects.")
        return web.json_response({"ok": True, "amps": amps, "note": note + " The M5 remote gets it with its next Load."})

    async def h_hotkeys(self, _req: web.Request) -> web.Response:
        if self.hotkeys is None:
            return web.json_response({"supported": False, "bindings": [], "note": "hotkeys module not running"})
        try:
            st: Any = self.hotkeys.status()
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"supported": False, "bindings": [], "note": str(exc)})
        return web.json_response(st)

    # ---- the Play tab's status panel ---------------------------------------------------------------------------
    async def h_engine_status(self, _req: web.Request) -> web.Response:
        running = self.engine.running()
        st, tele, foc = await status.fetch()
        out = status.summarize(st, tele, foc, running=running, port=self.engine.port if running else None,
                               trip=self.engine.last_trip)
        out["log"] = status.filter_log(list(self.engine.log))
        return web.json_response(out)

    # ---- the M5 remote's settings and pattern files ------------------------------------------------------------
    async def h_remote_settings(self, _req: web.Request) -> web.Response:
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, m5settings.view))

    async def h_remote_settings_put(self, req: web.Request) -> web.Response:
        body = await _body(req)
        try:
            view = await asyncio.get_running_loop().run_in_executor(None, m5settings.update, body)
        except (m5settings.m5config.ConfigError, ValueError) as exc:
            return _err(str(exc))
        return web.json_response({"ok": True, **view})

    @staticmethod
    def _engine_cfg() -> dict:
        try:
            with open(ROOT / "config" / "engine.toml", "rb") as f:
                return tomllib.load(f)
        except (OSError, ValueError):
            return {}

    def _patterns(self) -> dict:
        """What `stimengine.remote build` would pack, without building or loading anything."""
        from ..et312 import fwdata
        from ..et312.foc312 import DEFAULT_ELK_DIR
        from ..remote import pack
        cfg = self._engine_cfg()
        elk_dir = str((cfg.get("et312") or {}).get("elk_dir", DEFAULT_ELK_DIR))
        ours_dir = ROOT / "routines"
        data = fwdata.load(cfg)
        pk, notes = pack.collect(firmware=data, elk_dir=elk_dir, ours_dir=ours_dir)
        counts: dict[int, int] = {}
        for e in pk.entries:
            counts[e.group] = counts.get(e.group, 0) + 1
        groups = [{"id": g, "name": name, "count": counts.get(g, 0)} for g, name in pack.GROUP_NAMES.items()]
        from ..et312 import elk
        cache = Path(elk.default_cache_dir())      # ErosLink's own routines, extracted from its installer
        return {"groups": groups, "total": len(pk.entries), "notes": notes,
                "eroslink_cache": {"path": str(cache), "exists": cache.is_dir(),
                                   "files": len(list(cache.glob("*/*.elk"))) if cache.is_dir() else 0},
                "elk_dir": {"path": elk_dir, "exists": bool(elk_dir) and Path(elk_dir).is_dir()},
                "ours_dir": {"path": str(ours_dir), "exists": ours_dir.is_dir()},
                "builtin": self._et312_view(data)}

    async def h_remote_patterns(self, _req: web.Request) -> web.Response:
        try:
            out = await asyncio.get_running_loop().run_in_executor(None, self._patterns)
        except Exception as exc:  # noqa: BLE001 - a bad .elk file must not break the page
            return _err(f"could not read the pattern files: {exc}", 500)
        return web.json_response(out)

    # ---- the ET-312 built-in modes (the user's own firmware image) ---------------------------------------------
    @staticmethod
    def _et312_view(data) -> dict:
        from ..et312 import fwdata
        return {"available": data is not None, "source": data.source if data is not None else None,
                "blocks": len(data.blocks) if data is not None else 0, "user_file": str(fwdata.USER_DATA),
                "user_file_exists": fwdata.USER_DATA.exists()}

    async def h_et312(self, _req: web.Request) -> web.Response:
        from ..et312 import fwdata
        data = await asyncio.get_running_loop().run_in_executor(None, fwdata.load, self._engine_cfg())
        return web.json_response(self._et312_view(data))

    async def h_et312_extract(self, req: web.Request) -> web.Response:
        """The image as the raw body or as the first file of a multipart form; decoded here, written as JSON to
        config/et312-firmware-data.json (gitignored). The image itself is not kept."""
        from ..et312 import fwdata
        raw = b""
        if req.content_type.startswith("multipart/"):
            reader = await req.multipart()
            part = await reader.next()
            while part is not None and not getattr(part, "filename", None):
                part = await reader.next()
            if part is None:
                return _err("no file in the form")
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                raw += chunk
                if len(raw) > EXTRACT_MAX_BYTES:
                    return _err("that file is too big for an ET-312 firmware image")
        else:
            while True:                       # one read() may return only part of the body: read to the end
                chunk = await req.content.read(64 * 1024)
                if not chunk:
                    break
                raw += chunk
                if len(raw) > EXTRACT_MAX_BYTES:
                    return _err("that file is too big for an ET-312 firmware image")
        if not raw:
            return _err("no file received")
        suffix = ".hex" if raw.lstrip()[:1] == b":" else ".bin"          # Intel HEX is text starting with ':'

        def work():
            with tempfile.TemporaryDirectory() as d:
                img = Path(d) / ("et312-image" + suffix)
                img.write_bytes(raw)
                data = fwdata.from_image(img)
            fwdata.to_json(data, fwdata.USER_DATA)
            fwdata.set_default(data)           # this process (the pattern list) sees it at once
            return data

        try:
            data = await asyncio.get_running_loop().run_in_executor(None, work)
        except (fwdata.FirmwareDataError, ValueError, KeyError, IndexError) as exc:
            return _err(f"not an ET-312B v1.6 firmware image: {exc}")
        return web.json_response({"ok": True, "blocks": len(data.blocks), "sha256": hashlib.sha256(raw).hexdigest(),
                                  "path": str(fwdata.USER_DATA), "source": data.source})
