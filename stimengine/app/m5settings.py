"""The M5 remote's settings for the hub: config/m5.toml shown without its passwords, edited from the page, validated
the way a load would (m5config.build), and written back; plus a record of the last load onto the remote, so the page
can say whether the remote still has the settings shown here.

config/m5.toml is gitignored (it holds Wi-Fi passwords). The page never receives a password: it sees whether one is
set, and an empty password field on save keeps the stored one.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import tomllib
from pathlib import Path

from ..remote import m5config

ROOT = m5config.ROOT
M5_TOML = ROOT / "config" / "m5.toml"
ENGINE_TOML = ROOT / "config" / "engine.toml"
FOC312_STATE = ROOT / "config" / "foc312-state.json"
LAST_LOAD = ROOT / "config" / "m5-last-load.json"
BUILD_CONFIG = ROOT / "build" / "m5" / "config.json"      # what `stimengine.remote load` last built

DIRECT_DEFAULTS = {"ssid": "stim-remote", "channel": 6}


def _read(path: Path | None = None) -> dict | None:
    path = path or M5_TOML
    if not path.exists():
        return None
    with open(path, "rb") as f:
        return tomllib.load(f)


def _engine_cfg() -> dict:
    try:
        with open(ENGINE_TOML, "rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError):
        return {}


def _foc312_state() -> dict | None:
    try:
        return json.loads(FOC312_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def view(path: Path | None = None) -> dict:
    """The settings as the page shows them (no passwords), what a load would send, and the last load."""
    raw = _read(path)
    missing = raw is None
    raw = raw or {}
    wifi = raw.get("wifi") or {}
    d = raw.get("direct") or {}
    out = {
        "missing": missing,
        "wifi": {"ssid": str(wifi.get("ssid", "")), "has_password": bool(wifi.get("password"))},
        # m5config: a [direct] section counts as enabled unless it says enabled = false
        "direct": {"enabled": bool(d) and bool(d.get("enabled", True)),
                   "ssid": str(d.get("ssid", DIRECT_DEFAULTS["ssid"])),
                   "has_password": bool(d.get("password")),
                   "channel": int(d.get("channel", DIRECT_DEFAULTS["channel"]))},
        "boxes": [{"name": str(b.get("name", "")), "mac": str(b.get("mac", "")), "host": str(b.get("host", "")),
                   "port": int(b.get("port", 55533))} for b in raw.get("box") or []],
    }
    try:
        built = m5config.build(_engine_cfg(), raw, _foc312_state())
        built["wifi"].pop("password", None)
        out["remote_gets"], out["build_error"] = built, None
    except m5config.ConfigError as exc:
        out["remote_gets"], out["build_error"] = None, str(exc)
    out["last_load"] = last_load(path)
    return out


def _merge(body: dict, current: dict) -> dict:
    """The page's form -> an m5.toml dict. Empty password = keep the stored one."""
    cw = current.get("wifi") or {}
    cd = current.get("direct") or {}
    w = body.get("wifi") or {}
    d = body.get("direct") or {}
    new: dict = {}
    ssid = str(w.get("ssid", cw.get("ssid", ""))).strip()
    pw = str(w.get("password") or cw.get("password") or "")
    if ssid or pw:
        new["wifi"] = {"ssid": ssid, "password": pw}
    new["direct"] = {
        "enabled": bool(d.get("enabled", False)),
        "ssid": str(d.get("ssid", cd.get("ssid", DIRECT_DEFAULTS["ssid"]))).strip(),
        "password": str(d.get("password") or cd.get("password") or ""),
        "channel": int(d.get("channel", cd.get("channel", DIRECT_DEFAULTS["channel"]))),
    }
    boxes = []
    for b in body.get("boxes") or []:
        name, mac, host = (str(b.get(k, "") or "").strip() for k in ("name", "mac", "host"))
        if not (name or mac or host):
            continue                                   # an empty row the user added and left
        box: dict = {"name": name or host or mac}
        if mac:
            box["mac"] = m5config._mac(mac, box["name"])      # aa:bb:cc:dd:ee:ff, or ConfigError
        if host:
            box["host"] = host
        box["port"] = int(b.get("port") or 55533)
        boxes.append(box)
    new["box"] = boxes
    return new


def _q(v: str) -> str:
    """A TOML basic string (JSON's escapes are valid TOML; non-ASCII stays as UTF-8)."""
    return json.dumps(str(v), ensure_ascii=False)


def to_toml(cfg: dict) -> str:
    lines = ["# The M5 remote's Wi-Fi and boxes, written by the PC app. Gitignored: it holds Wi-Fi passwords.",
             "# Format: config/m5.example.toml. The remote gets these with the next Load.", ""]
    w = cfg.get("wifi")
    if w:
        lines += ["[wifi]", f"ssid = {_q(w.get('ssid', ''))}", f"password = {_q(w.get('password', ''))}", ""]
    d = cfg.get("direct")
    if d:
        lines += ["[direct]", f"enabled = {'true' if d.get('enabled') else 'false'}", f"ssid = {_q(d.get('ssid', ''))}",
                  f"password = {_q(d.get('password', ''))}", f"channel = {int(d.get('channel', 6))}", ""]
    for b in cfg.get("box") or []:
        lines.append("[[box]]")
        lines.append(f"name = {_q(b.get('name', ''))}")
        if b.get("mac"):
            lines.append(f"mac = {_q(b['mac'])}")
        if b.get("host"):
            lines.append(f"host = {_q(b['host'])}")
        lines += [f"port = {int(b.get('port', 55533))}", ""]
    return "\n".join(lines)


def update(body: dict, path: Path | None = None) -> dict:
    """Validate (as a load would) and write config/m5.toml; ConfigError if the remote could not use it."""
    path = path or M5_TOML
    new = _merge(body, _read(path) or {})
    m5config.build(_engine_cfg(), new, _foc312_state())      # raises ConfigError: nothing is written
    text = to_toml(new)
    tomllib.loads(text)                                     # our own writer must round-trip
    tmp = path.with_suffix(".toml.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return view(path)


# ---- the last load onto the remote ------------------------------------------------------------------------------

LOAD_LINE = re.compile(r"patterns\.bin: (\d+) patterns, (\d+) bytes(?: \(([^)]*)\))?")


def parse_load_log(lines: list[str]) -> dict | None:
    """What a finished `stimengine.remote load` printed: pattern count, pack size, count per group. None unless it
    ended with the remote's confirmation ('loaded; ...')."""
    if not any(line.startswith("loaded;") for line in lines):
        return None
    rec: dict = {"patterns": None, "pack_bytes": None, "groups": {}}
    for line in lines:
        m = LOAD_LINE.search(line)
        if m:
            rec["patterns"], rec["pack_bytes"] = int(m.group(1)), int(m.group(2))
            for part in (m.group(3) or "").split(","):
                name, _, n = part.strip().rpartition(" ")
                if name and n.isdigit():
                    rec["groups"][name] = int(n)
    return rec


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record_job(job) -> None:
    """Job finish hook for 'remote-load' (jobs.JobManager.on_done)."""
    if job.state != "ok":
        return
    rec = parse_load_log(list(job.lines))
    if rec is None:
        return
    rec["time"] = time.time()
    rec["settings_sha256"] = _sha(BUILD_CONFIG.read_bytes()) if BUILD_CONFIG.exists() else None
    LAST_LOAD.write_text(json.dumps(rec, indent=1), encoding="utf-8")


def last_load(path: Path | None = None) -> dict | None:
    """The last load, and whether the settings a load would send now are still the ones the remote got."""
    try:
        rec = json.loads(LAST_LOAD.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        now = m5config.build(_engine_cfg(), _read(path) or {}, _foc312_state())
        rec["settings_match"] = (_sha(m5config.to_bytes(now)) == rec.get("settings_sha256")
                                 if rec.get("settings_sha256") else None)
    except m5config.ConfigError:
        rec["settings_match"] = False
    return rec
