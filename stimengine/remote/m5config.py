"""The M5 remote's settings file (`config.json` on the remote), built from this PC's configuration.

Caps and safety timings come from config/engine.toml, so the remote enforces exactly what the engine does - they
are set here, never on the remote (notes/m5-remote.md, safety 5). Wi-Fi and the list of boxes come from
config/m5.toml (gitignored: it holds the Wi-Fi password); config/m5.example.toml shows the format.
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

from ..engine import HARD_AMPS_CAP

ROOT = Path(__file__).resolve().parents[2]
FORMAT = "stim-remote config v1"


class ConfigError(ValueError):
    pass


def build(engine_cfg: dict, m5_cfg: dict, foc312_state: dict | None = None) -> dict:
    sig = engine_cfg.get("signal", {})
    saf = engine_cfg.get("safety", {})
    et = engine_cfg.get("et312", {})
    cap = float(sig.get("waveform_amplitude_amps", 0.15))
    if not 0 < cap <= HARD_AMPS_CAP:
        raise ConfigError(f"waveform_amplitude_amps {cap} is outside 0..{HARD_AMPS_CAP}")
    direct = direct_network(m5_cfg)
    if direct:
        wifi_out = {"mode": "direct", **direct}
    else:
        wifi = m5_cfg.get("wifi") or {}
        if not wifi.get("ssid"):
            raise ConfigError("config/m5.toml: [wifi] ssid is missing")
        wifi_out = {"mode": "house", "ssid": str(wifi["ssid"]), "password": str(wifi.get("password", ""))}
    boxes = []
    for b in m5_cfg.get("box") or []:
        # on the house network a box is found by its address; on the remote's own network by its Wi-Fi MAC (or,
        # with a single box and no MAC, as whichever box joined)
        if not direct and not b.get("host"):
            raise ConfigError(f"config/m5.toml: box {b.get('name', '?')!r} has no host")
        box = {"name": str(b.get("name") or b.get("host") or b.get("mac") or "box")[:24],
               "host": str(b.get("host", "")), "port": int(b.get("port", 55533))}
        if b.get("mac"):
            box["mac"] = _mac(b["mac"], box["name"])
        boxes.append(box)
    if not boxes:
        raise ConfigError("config/m5.toml: at least one [[box]] is needed")
    if direct and len(boxes) > 1 and not all("mac" in b for b in boxes):
        raise ConfigError("config/m5.toml: with [direct] and several boxes, every box needs its Wi-Fi mac")
    pads = (foc312_state or {}).get("pads") or [True, True, True, True]
    return {
        "format": FORMAT,
        "wifi": wifi_out,
        "boxes": boxes,
        "safety": {
            "amps_cap": cap,
            "slow_start_s": float(saf.get("slow_start_s", 4.0)),
            "deadman_silence_s": float(saf.get("deadman_silence_s", 2.0)),
            "deadman_ramp_down_s": float(saf.get("deadman_ramp_down_s", 3.0)),
        },
        "et312": {"monophasic_asymmetry": float(min(4.0, max(1.0, float(et.get("monophasic_asymmetry", 3.0)))))},
        "defaults": {"routes": [12, 34], "pads": [bool(p) for p in pads][:4], "shape": "rounded"},
    }


def direct_network(m5_cfg: dict) -> dict | None:
    """The remote's own network ([direct] in config/m5.toml), or None when the remote joins the house Wi-Fi.
    The boxes join it (`pair-box`): one hop through the air instead of two, and no busy house access point."""
    d = m5_cfg.get("direct")
    if not d or not d.get("enabled", True):
        return None
    ssid, pw, ch = str(d.get("ssid", "")), str(d.get("password", "")), int(d.get("channel", 6))
    if not 1 <= len(ssid.encode()) <= 32:
        raise ConfigError("config/m5.toml: [direct] ssid must be 1..32 bytes")
    if not 8 <= len(pw.encode()) <= 63:       # WPA2: the box refuses open networks
        raise ConfigError("config/m5.toml: [direct] password must be 8..63 characters")
    if not 1 <= ch <= 11:
        raise ConfigError("config/m5.toml: [direct] channel must be 1..11")
    return {"ssid": ssid, "password": pw, "channel": ch}


def _mac(text: str, name: str) -> str:
    parts = str(text).replace("-", ":").lower().split(":")
    if len(parts) != 6 or not all(len(p) == 2 and all(c in "0123456789abcdef" for c in p) for p in parts):
        raise ConfigError(f"config/m5.toml: box {name!r} mac {text!r} is not aa:bb:cc:dd:ee:ff")
    return ":".join(parts)


def load_m5(m5_toml: Path | None = None) -> dict:
    m5_toml = m5_toml or ROOT / "config" / "m5.toml"
    if not m5_toml.exists():
        raise ConfigError(f"{m5_toml} not found: copy config/m5.example.toml and fill in Wi-Fi and boxes")
    with open(m5_toml, "rb") as f:
        return tomllib.load(f)


def build_from_files(engine_toml: Path | None = None, m5_toml: Path | None = None) -> dict:
    engine_toml = engine_toml or ROOT / "config" / "engine.toml"
    m5_cfg = load_m5(m5_toml)
    with open(engine_toml, "rb") as f:
        engine_cfg = tomllib.load(f)
    state_path = ROOT / "config" / "foc312-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else None
    return build(engine_cfg, m5_cfg, state)


def to_bytes(cfg: dict) -> bytes:
    return json.dumps(cfg, indent=1).encode("utf-8")
