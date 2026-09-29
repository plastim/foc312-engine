"""USB serial devices: FOC-Stim boxes and M5 remotes. Both are ESP32-S3 USB serial bridges (303a:1001), so the kind
comes from asking: the remote's loader answers HELLO ("OK stim-remote 1 <free>", or "ERR busy" while armed); a box
answers the protobuf firmware-version request. Probing opens the port, so it only happens when asked, never on the
engine's port, and the result is kept per (port, serial number) until the device goes away. A detected kind is also
remembered by serial number in config/devices.json, so a device is recognised on the next start without probing.
Names: a box whose serial (its ESP32's MAC) matches a [[box]] mac in config/m5.toml gets that name.
"""
from __future__ import annotations

import asyncio
import json
import re
import tomllib
from pathlib import Path

from serial.tools import list_ports

ROOT = Path(__file__).resolve().parents[2]
KNOWN_FILE = ROOT / "config" / "devices.json"

REMOTE_WAIT_S = 2.0
BOX_WAIT_S = 3.0

_cache: dict[tuple[str, str], tuple[str, str]] = {}      # (port, serial) -> (kind, detail)


def ports() -> list[dict]:
    out = []
    for p in list_ports.comports():
        if p.vid is None:
            continue
        out.append({"port": p.device, "serial": p.serial_number or "", "vid": f"{p.vid:04x}",
                    "pid": f"{(p.pid or 0):04x}"})
    return sorted(out, key=lambda d: d["port"])


def probe_remote(port: str) -> str | None:
    """'stim-remote free N B' / 'stim-remote (busy: armed)', or None if it is not a remote."""
    from ..remote.loader import LoaderError, Remote, SerialLink
    try:
        link = SerialLink(port, timeout=1.0)
    except Exception:  # noqa: BLE001 - busy, gone, no permission
        return None
    try:
        free = Remote(link).hello(wait_s=REMOTE_WAIT_S)
        return f"stim-remote free {free} B"
    except LoaderError as exc:
        return "stim-remote (busy: armed)" if "busy" in str(exc) else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        link.close()


async def probe_box(port: str) -> str | None:
    """'1.3.2 (main) stim-engine biphasic-pairs v7', or None if no box answered."""
    from ..device import FocStimClient, SerialTransport
    client = FocStimClient(SerialTransport(port))
    try:
        await asyncio.wait_for(client.connect_and_handshake(start_imu=False), BOX_WAIT_S)
        t = client.telemetry
        comment = ""
        try:
            fw = await asyncio.wait_for(client.firmware_version(), BOX_WAIT_S)
            comment = fw.stm32_firmware_version_2.comment
        except Exception:  # noqa: BLE001
            pass
        return f"{t.firmware} {comment}".strip()
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


async def probe(port: str) -> tuple[str, str]:
    detail = await asyncio.get_running_loop().run_in_executor(None, probe_remote, port)
    if detail:
        return "remote", detail
    detail = await probe_box(port)
    if detail:
        return "box", detail
    return "unknown", ""


def _load_known() -> dict:
    try:
        return json.loads(KNOWN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_known(known: dict) -> None:
    try:
        KNOWN_FILE.write_text(json.dumps(known, indent=1), encoding="utf-8")
    except OSError:
        pass


def _box_names() -> dict[str, str]:
    """MAC (lower case) -> the box's name from config/m5.toml."""
    try:
        with open(ROOT / "config" / "m5.toml", "rb") as f:
            cfg = tomllib.load(f)
    except (OSError, ValueError):
        return {}
    return {str(b["mac"]).lower().replace("-", ":"): str(b.get("name") or "")
            for b in cfg.get("box") or [] if b.get("mac")}


def firmware_of(kind: str, detail: str) -> tuple[int | None, str]:
    """(fork version, label) of a box's firmware from its probe detail: fork vN, fork v1, or stock (0)."""
    if kind != "box" or not detail or detail == "engine connected":
        return None, ""
    m = re.search(r"stim-engine biphasic-pairs(?: v(\d+))?", detail)
    if m:
        v = int(m.group(1)) if m.group(1) else 1
        return v, f"PlaStim fork v{v}"
    ver = detail.split()[0] if detail.split() else ""
    return 0, f"stock {ver}".strip()


def known_kind(port: str) -> str:
    """What the device on `port` was detected as ('box', 'remote' or 'unknown'), without opening the port."""
    for d in ports():
        if d["port"].upper() == port.upper():
            if (d["port"], d["serial"]) in _cache:
                return _cache[(d["port"], d["serial"])][0]
            k = _load_known().get(d["serial"])
            return k["kind"] if k else "unknown"
    return "unknown"


async def scan(in_use: set[str], probe_mode: int = 0) -> list[dict]:
    """probe_mode 0: never open a port (cached kinds only); 1: probe ports not identified yet; 2: probe again."""
    devs = ports()
    present = {(d["port"], d["serial"]) for d in devs}
    for key in list(_cache):
        if key not in present:
            del _cache[key]
    known = _load_known()
    names = _box_names()
    changed = False
    for d in devs:
        key = (d["port"], d["serial"])
        d["in_use"] = d["port"].upper() in {p.upper() for p in in_use}
        # 1: ask whatever is not identified yet (an earlier failed ask, e.g. the port was busy, is asked again)
        if probe_mode and not d["in_use"] and (probe_mode == 2 or _cache.get(key, ("unknown",))[0] == "unknown"):
            _cache[key] = await probe(d["port"])
            if _cache[key][0] != "unknown" and d["serial"]:
                known[d["serial"]] = {"kind": _cache[key][0], "detail": _cache[key][1]}
                changed = True
        if key in _cache:
            d["kind"], d["detail"] = _cache[key]
        elif d["serial"] in known:                     # seen before: its kind without opening the port
            d["kind"], d["detail"] = known[d["serial"]]["kind"], known[d["serial"]].get("detail", "")
        else:
            d["kind"], d["detail"] = "unknown", ""
        d["name"] = names.get(d["serial"].lower(), "") if d["kind"] != "remote" else "M5 remote"
        d["fw_fork"], d["fw_label"] = firmware_of(d["kind"], d["detail"])
    if changed:
        _save_known(known)
    return devs
