"""The Play tab's status panel: what the engine and the box are doing, at a glance.

The hub asks the engine's own APIs (control API :8321 /status and /telemetry, foc312 :8322 /state) with a short
timeout and folds the answers into one small summary. The engine may be off, starting or gone: every field is
optional, and the page shows a dash for what is missing. The box's last trip report comes from the engine's log
(EngineProc.last_trip): a tripped box stops answering, so its report is only ever there.
"""
from __future__ import annotations

import asyncio
import re

import aiohttp

API = "http://127.0.0.1:8321"
FOC312 = "http://127.0.0.1:8322"
TIMEOUT_S = 1.0
STALE_S = 3.0          # no telemetry from the box for this long: the link is probably gone

# log lines that say nothing useful to a person watching the box
_NOISE = re.compile(r"aiohttp\.access|engine\.content\.worker|analyzer worker|\"(GET|POST) /|^\s*$")
_NUMBERS_ONLY = re.compile(r"device:\s*[-\d.\s|]+$")        # the firmware's timing tables after a trip


def filter_log(lines: list[str], keep: int = 40) -> list[str]:
    return [ln for ln in lines if not _NOISE.search(ln) and not _NUMBERS_ONLY.search(ln)][-keep:]


async def _get(session: aiohttp.ClientSession, url: str) -> dict | None:
    try:
        async with session.get(url) as r:
            if r.status != 200:
                return None
            d = await r.json(content_type=None)
            return d if isinstance(d, dict) else None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None


async def fetch() -> tuple[dict | None, dict | None, dict | None]:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S)) as s:
        return await asyncio.gather(_get(s, API + "/status"), _get(s, API + "/telemetry"), _get(s, FOC312 + "/state"))


def _pct(x) -> int | None:
    try:
        return round(float(x) * 100)
    except (TypeError, ValueError):
        return None


def _channel_peak_ma(peak: list | None, route) -> float | None:
    """The current a channel actually gets: the box's measured peak on the electrodes of its route. On a shared
    electrode the other one of the pair is this channel's alone, so the smaller of the two."""
    try:
        code = int(route)
        a, b = code // 10 - 1, code % 10 - 1
        return round(min(float(peak[a]), float(peak[b])) * 1000, 1)
    except (TypeError, ValueError, IndexError):
        return None


def summarize(status: dict | None, tele: dict | None, foc: dict | None, *, running: bool, port: str | None,
              trip: dict | None) -> dict:
    status, tele, foc = status or {}, tele or {}, foc or {}
    out: dict = {"running": running, "port": port, "reachable": bool(status or foc), "warnings": [], "trip": trip}
    fw = tele.get("firmware")
    if fw and status.get("fork_firmware"):
        fw = f"{fw} · PlaStim fork v{status.get('fork_version')}"
    elif fw:
        fw = f"{fw} · stock"
    out["firmware"] = fw
    out["link"] = status.get("link")
    age = tele.get("age_s")
    out["telemetry_age_s"] = round(float(age), 1) if isinstance(age, (int, float)) else None
    armed = status.get("armed")
    if status.get("faulted"):
        out["state"] = "fault"
    elif armed is not None:
        out["state"] = "running" if armed else "stopped"
    else:
        out["state"] = None
    out["fault"] = status.get("fault_reason")
    out["output"] = foc.get("output")
    out["pattern"] = (foc.get("pattern") or {}).get("name")
    levels = foc.get("levels") or [None, None]
    out["level_a"], out["level_b"] = _pct(levels[0]), _pct(levels[1] if len(levels) > 1 else None)
    out["master"] = _pct(status.get("master"))
    out["ma"] = _pct(foc.get("ma"))
    routes = foc.get("routes") or [12, 34]
    peak = tele.get("peak")
    out["peak_ma_a"] = _channel_peak_ma(peak, routes[0]) if peak else None
    out["peak_ma_b"] = _channel_peak_ma(peak, routes[1]) if peak and len(routes) > 1 else None
    out["routes"] = routes
    out["battery"] = _pct(tele.get("battery_soc"))
    out["charging"] = tele.get("wall_power_present")
    rate = tele.get("actual_pulse_frequency")
    out["pulse_hz"] = round(float(rate), 1) if isinstance(rate, (int, float)) else None
    out["box_knob"] = _pct(tele.get("device_volume"))
    w = out["warnings"]
    if status.get("faulted"):
        w.append(f"fault: {status.get('fault_reason') or 'the engine stopped the output'}")
    if armed and status.get("deadman_active"):
        w.append("no control input: the output is ramping down (open the player, or keep it open)")
    if out["box_knob"] == 0:
        w.append("the box's own knob is at zero: nothing will play until it is turned up")
    if out["telemetry_age_s"] is not None and out["telemetry_age_s"] > STALE_S:
        w.append(f"no data from the box for {out['telemetry_age_s']:.0f} s: is it switched on and connected?")
    if armed and out["output"] == "preview":
        w.append("the player's output is Preview: nothing reaches the box (choose Fork fw)")
    if running and not out["reachable"]:
        w.append("the engine is starting (or not answering yet)")
    return out
