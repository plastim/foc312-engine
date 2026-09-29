"""The ET-312B's built-in mode programs: loaded at runtime from the user's own data, never shipped in the source.

The box's 18 built-in modes (Waves, Stroke, Climb, ...) are 36 program blocks of bytecode in the ET-312B firmware
(flash 0x2000-0x21c7, indexed by the byte table at 0x1c3e: one word offset from 0x2000 per block). They are
ErosTek's, so stim-engine does not include them; the VM (vm.py) and the mode logic (modes.py) are our own
reimplementation and run whatever blocks they are given.

Where the blocks come from, first match wins:
  1. `[et312] firmware_data` in config/engine.toml, or $STIM_ENGINE_ET312_DATA: a JSON file written by this module;
  2. `[et312] firmware_image`, or $STIM_ENGINE_ET312_IMAGE: the user's own v1.6 firmware image (.bin, or
     Intel .hex), e.g. from the buttshock project's `scripts/fw-utils.py --downloadfw`;
  3. `config/et312-firmware-data.json`: what the PC app's "ET-312 built-in modes" card writes from an uploaded image
     (gitignored: it stays on this computer);
  4. `private/et312/firmware-data.json` in this checkout (the maintainer's copy; not part of a public release).
Without any of them the built-in modes are simply absent; ErosLink routines and our own routines still run.

    py -3.13 -m stimengine.et312.fwdata --image 312-16-dec.bin --out private/et312/firmware-data.json

The bytecode is the firmware's own module format, decoded by `vm.decode_module` into the VM's op tuples.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .vm import decode_module

logger = logging.getLogger("engine.et312.fwdata")

ROOT = Path(__file__).resolve().parents[2]
PRIVATE_DATA = ROOT / "private" / "et312" / "firmware-data.json"
USER_DATA = ROOT / "config" / "et312-firmware-data.json"      # written by the PC app's extract (stimengine/app)
PROG_BASE = 0x2000             # program blocks
PROG_END = 0x21C8
TABLE = 0x1C3E                 # one byte per block: word offset from PROG_BASE
NBLOCKS = 36
FORMAT = "stim-engine et312 firmware data v1"


class FirmwareDataError(ValueError):
    pass


@dataclass(frozen=True)
class FirmwareData:
    blocks: dict[int, list[tuple]]
    source: str                 # where it was loaded from (for status / logs)


# ---- the user's firmware image ---------------------------------------------------------------------------------

def _read_image(path: Path) -> bytes:
    raw = path.read_bytes()
    if path.suffix.lower() != ".hex":
        return raw
    mem: dict[int, int] = {}
    base = 0
    for line in raw.decode("ascii", "replace").splitlines():
        line = line.strip()
        if not line.startswith(":"):
            continue
        rec = bytes.fromhex(line[1:])
        n, addr, typ, data = rec[0], (rec[1] << 8) | rec[2], rec[3], rec[4:4 + rec[0]]
        if sum(rec) & 0xFF:
            raise FirmwareDataError(f"{path.name}: bad Intel HEX checksum")
        if typ == 0:
            for i, b in enumerate(data):
                mem[base + addr + i] = b
        elif typ == 2:
            base = int.from_bytes(data, "big") << 4
        elif typ == 4:
            base = int.from_bytes(data, "big") << 16
        elif typ == 1:
            break
    if not mem:
        return b""
    out = bytearray(b"\xff" * (max(mem) + 1))
    for a, b in mem.items():
        out[a] = b
    return bytes(out)


def from_image(path: str | Path) -> FirmwareData:
    """Decode the 36 program blocks from an ET-312B v1.6 firmware image (read from the user's own box)."""
    p = Path(path)
    img = _read_image(p)
    if len(img) < PROG_END:
        raise FirmwareDataError(f"{p.name}: {len(img)} bytes; not an ET-312B v1.6 firmware image (need >= {PROG_END})")
    blocks: dict[int, list[tuple]] = {}
    for idx in range(NBLOCKS):
        start = PROG_BASE + 2 * img[TABLE + idx]
        if not PROG_BASE <= start < PROG_END:
            raise FirmwareDataError(f"{p.name}: block {idx} points outside the program area")
        ops = decode_module(img[start:PROG_END])
        if not ops:
            raise FirmwareDataError(f"{p.name}: block {idx} is empty; wrong image or version?")
        blocks[idx] = ops
    return FirmwareData(blocks, f"image {p.name} (sha1 {hashlib.sha1(img).hexdigest()[:12]})")


# ---- JSON (what this module writes) ----------------------------------------------------------------------------

def _op_to_json(op: tuple) -> list:
    return [x.hex() if isinstance(x, (bytes, bytearray)) else x for x in op]


def _op_from_json(op: list) -> tuple:
    if op and op[0] == "blk":
        return ("blk", op[1], op[2], bytes.fromhex(op[3]))
    return tuple(op)


def to_json(data: FirmwareData, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {"format": FORMAT, "source": data.source,
           "blocks": {str(k): [_op_to_json(o) for o in v] for k, v in sorted(data.blocks.items())}}
    p.write_text(json.dumps(doc, indent=1), encoding="utf-8")


def from_json(path: str | Path) -> FirmwareData:
    p = Path(path)
    doc = json.loads(p.read_text(encoding="utf-8"))
    if doc.get("format") != FORMAT:
        raise FirmwareDataError(f"{p.name}: not a {FORMAT!r} file")
    blocks = {int(k): [_op_from_json(o) for o in v] for k, v in doc["blocks"].items()}
    if sorted(blocks) != list(range(NBLOCKS)):
        raise FirmwareDataError(f"{p.name}: expected blocks 0..{NBLOCKS - 1}")
    return FirmwareData(blocks, f"{p.name} ({doc.get('source', '?')})")


# ---- lookup ----------------------------------------------------------------------------------------------------

def load(config: dict | None = None) -> FirmwareData | None:
    """The built-in mode data from the first configured source (module docstring), or None."""
    et = (config or {}).get("et312") or {}
    candidates = [
        ("json", et.get("firmware_data") or os.environ.get("STIM_ENGINE_ET312_DATA")),
        ("image", et.get("firmware_image") or os.environ.get("STIM_ENGINE_ET312_IMAGE")),
        ("json", str(USER_DATA)),
        ("json", str(PRIVATE_DATA)),
    ]
    for kind, path in candidates:
        if not path or not Path(path).exists():
            continue
        try:
            return from_json(path) if kind == "json" else from_image(path)
        except (OSError, ValueError, KeyError) as exc:
            logger.warning("ET-312 built-in mode data not loaded from %s: %s", path, exc)
    return None


_default: FirmwareData | None = None
_default_loaded = False


def default() -> FirmwareData | None:
    """load() with no config, cached (the engine and foc312 share one copy)."""
    global _default, _default_loaded
    if not _default_loaded:
        _default, _default_loaded = load(), True
    return _default


def set_default(data: FirmwareData | None) -> None:
    """Install the data the rest of the process uses (serve does this from config; tests too)."""
    global _default, _default_loaded
    _default, _default_loaded = data, True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Extract the ET-312B built-in mode blocks from your own firmware image.")
    ap.add_argument("--image", required=True, help="your own ET-312B v1.6 firmware image (.bin or .hex)")
    ap.add_argument("--out", default=str(PRIVATE_DATA))
    args = ap.parse_args(argv)
    data = from_image(args.image)
    to_json(data, args.out)
    print(f"{len(data.blocks)} blocks from {data.source} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
