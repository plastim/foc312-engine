"""USB serial devices: FOC-Stim boxes and the remotes. Boxes and the M5 remote are ESP32-S3 USB serial bridges
(303a:1001), so the kind comes from asking: the remote's loader answers HELLO ("OK stim-remote 1 <free>", or "ERR busy"
while armed); a box answers the protobuf firmware-version request. Probing opens the port, so it only happens when
asked, never on the engine's port, and the result is kept per (port, serial number) until the device goes away. A
detected kind is also remembered by serial number in config/devices.json, so a device is recognised on the next start
without probing. Names: a box whose serial (its ESP32's MAC) matches a [[box]] mac in config/m5.toml gets that name.

The remote on the RADR hardware is behind a CP2102 USB-UART (10c4:ea60): only the remote's HELLO is tried there (a box
is never behind one, and the box probe's handshake is not for a UART), at the loader's 921600 baud with DTR / RTS held
low (the CP2102 drives the ESP32's EN and IO0 through its auto-reset). A CP2102's serial number is the same on every
unit ("0001"), so such a device is remembered by the ESP32 MAC its HELLO says ("mac:<mac>" in devices.json, with the
port it was last seen on); without a probe, a CP2102 on that port is taken to be it. Firmware since the RADR build
also says its board ("board=m5" / "board=radr"): the hub flashes only images for that board. A CP2102 that does not
answer as a remote may be a RADR still on its own firmware: it is offered for a first flash ("radr_candidate"), which
the hub does only when asked for explicitly.
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
CP210X_VID, CP2102_PID = "10c4", "ea60"                  # Silicon Labs CP210x: the RADR's USB-UART
BOARD_NAMES = {"m5": "M5 remote", "radr": "RADR remote"}

_cache: dict[tuple[str, str], tuple[str, str]] = {}      # (port, serial) -> (kind, detail)
_ident: dict[tuple[str, str], dict] = {}                 # (port, serial) -> the remote's {"board", "mac"} (HELLO)


def ports() -> list[dict]:
    out = []
    for p in list_ports.comports():
        if p.vid is None:
            continue
        out.append({"port": p.device, "serial": p.serial_number or "", "vid": f"{p.vid:04x}",
                    "pid": f"{(p.pid or 0):04x}"})
    return sorted(out, key=lambda d: d["port"])


def generic_serial(d: dict) -> bool:
    """A USB-UART bridge: its serial number is the bridge's (the same on every CP2102), not the device's."""
    return d.get("vid") == CP210X_VID


def is_cp2102(d: dict) -> bool:
    return d.get("vid") == CP210X_VID and d.get("pid") == CP2102_PID


def probe_remote(port: str) -> tuple[str, dict] | None:
    """('stim-remote free N B' | 'stim-remote (busy: armed)', {"board", "mac"} as it says them), or None if it is not
    a remote. An armed remote refuses HELLO: its board and MAC are then not known from this probe."""
    from ..remote.loader import LoaderError, Remote, SerialLink
    try:
        link = SerialLink(port, timeout=1.0)
    except Exception:  # noqa: BLE001 - busy, gone, no permission
        return None
    r = Remote(link)
    try:
        free = r.hello(wait_s=REMOTE_WAIT_S)
        return f"stim-remote free {free} B", dict(r.ident)
    except LoaderError as exc:
        return ("stim-remote (busy: armed)", {}) if "busy" in str(exc) else None
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


async def probe(port: str) -> tuple[str, str, dict]:
    """(kind, detail, the remote's {"board", "mac"}): the remote's HELLO first, then (not behind a CP210x) the box's."""
    got = await asyncio.get_running_loop().run_in_executor(None, probe_remote, port)
    if got:
        return "remote", got[0], got[1]
    if any(d["port"].upper() == port.upper() and generic_serial(d) for d in ports()):
        return "unknown", "", {}
    detail = await probe_box(port)
    if detail:
        return "box", detail, {}
    return "unknown", "", {}


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
        return v, f"PlaStim firmware v{v}"
    ver = detail.split()[0] if detail.split() else ""
    return 0, f"stock {ver}".strip()


def _known_key(d: dict, mac: str = "") -> str:
    """Where devices.json keeps a device: its USB serial number, or for a USB-UART bridge (whose serial is the
    bridge's) the ESP32's MAC from HELLO ("" if not known)."""
    if generic_serial(d):
        return f"mac:{mac.lower()}" if mac else ""
    return d["serial"]


def _known_for(d: dict, known: dict) -> tuple[str, dict]:
    """(key, entry) remembered for this device without asking it: by serial; a USB-UART bridge by the port and USB ids
    it was last seen on (("", {}) if none)."""
    if not generic_serial(d):
        return (d["serial"], known[d["serial"]]) if d["serial"] in known else ("", {})
    usb = f"{d['vid']}:{d['pid']}"
    for k, v in known.items():
        if k.startswith("mac:") and str(v.get("port", "")).upper() == d["port"].upper() and v.get("usb") == usb:
            return k, v
    return "", {}


def record_flash(port: str, label: str) -> None:
    """Remember what was just flashed onto the device on `port` (the remote doesn't report its own version)."""
    for d in ports():
        if d["port"].upper() == port.upper():
            known = _load_known()
            key = _known_key(d, _ident.get((d["port"], d["serial"]), {}).get("mac", "")) or _known_for(d, known)[0]
            if not key:
                return                                 # (a first flash of a RADR: its MAC is learnt at the next Detect)
            known.setdefault(key, {"kind": "remote", "detail": ""})["flashed"] = label
            _save_known(known)
            return


def known_kind(port: str) -> str:
    """What the device on `port` was detected as ('box', 'remote' or 'unknown'), without opening the port."""
    for d in ports():
        if d["port"].upper() == port.upper():
            if (d["port"], d["serial"]) in _cache:
                return _cache[(d["port"], d["serial"])][0]
            k = _known_for(d, _load_known())[1]
            return k["kind"] if k else "unknown"
    return "unknown"


def _board_of(d: dict, kind: str, ident: dict, entry: dict) -> str:
    """The remote's board: what it said, else what was remembered, else by its USB (firmware from before the board
    words ran only on the M5; a CP2102 is the RADR's)."""
    if kind != "remote":
        return ""
    return ident.get("board") or entry.get("board") or ("radr" if is_cp2102(d) else "m5")


def remote_board(port: str) -> str:
    """The board of the remote on `port` ('m5' / 'radr'; '' if it is not a detected remote), without opening it."""
    for d in ports():
        if d["port"].upper() == port.upper():
            key = (d["port"], d["serial"])
            if key in _cache:
                return _board_of(d, _cache[key][0], _ident.get(key, {}), {})
            entry = _known_for(d, _load_known())[1]
            return _board_of(d, entry.get("kind", "unknown"), {}, entry)
    return ""


def radr_candidate(port: str) -> bool:
    """A CP2102 on `port` that has not answered as a remote (nor been remembered as anything): a RADR still on its
    own firmware, perhaps. Only a first flash asked for explicitly goes to such a port."""
    for d in ports():
        if d["port"].upper() == port.upper():
            return is_cp2102(d) and known_kind(port) == "unknown"
    return False


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
            got = await probe(d["port"])
            _cache[key] = (got[0], got[1])
            _ident[key] = dict(got[2]) if len(got) > 2 and got[2] else {}
            kkey = _known_key(d, _ident[key].get("mac", ""))
            if _cache[key][0] != "unknown" and kkey:
                entry = {"kind": _cache[key][0], "detail": _cache[key][1]}
                entry.update({k: v for k, v in _ident[key].items() if k in ("board", "mac")})
                if generic_serial(d):                  # a bridge: where it was seen, so it is known without asking
                    entry.update(port=d["port"], usb=f"{d['vid']}:{d['pid']}")
                    for v in known.values():           # (another device seen on this port before: not any more)
                        if str(v.get("port", "")).upper() == d["port"].upper() and v.get("usb") == entry["usb"]:
                            v.pop("port", None)
                known[kkey] = entry
                changed = True
        kkey, entry = _known_for(d, known)
        ident = _ident.get(key, {}) if key in _cache else {}
        if key in _cache:
            d["kind"], d["detail"] = _cache[key]
            if _cache[key][0] != "unknown":
                entry = known.get(_known_key(d, ident.get("mac", "")), entry)
        elif entry:                                    # seen before: its kind without opening the port
            d["kind"], d["detail"] = entry["kind"], entry.get("detail", "")
        else:
            d["kind"], d["detail"] = "unknown", ""
        d["board"] = _board_of(d, d["kind"], ident, entry)
        d["mac"] = ident.get("mac") or entry.get("mac", "")
        d["radr_candidate"] = d["kind"] == "unknown" and is_cp2102(d)
        if d["kind"] == "remote":
            d["name"] = BOARD_NAMES.get(d["board"], "remote")
        else:
            d["name"] = names.get(d["serial"].lower(), "")
        d["fw_fork"], d["fw_label"] = firmware_of(d["kind"], d["detail"])
        if d["kind"] == "remote" and entry.get("flashed"):
            d["fw_label"] = entry["flashed"]                       # the remote can't say; what this PC flashed
    if changed:
        _save_known(known)
    return devs
