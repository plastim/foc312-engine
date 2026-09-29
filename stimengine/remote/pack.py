"""Pattern packs for the M5 remote: one binary file (`patterns.bin` on the remote) holding every pattern it can play.

Built on this PC from the user's own data and never distributed: the ET-312 built-in blocks come from the user's
firmware data (fwdata.py), ErosLink routines from the local cache / routine folder. The C parser
(remote/core/pack.c) validates every length and offset before anything plays; this module holds the builder and
a reference parser the tests compare it with.

Layout (little-endian):
    header   "FP31"  u16 version (1)  u16 flags (bit0 = built-in blocks present)  u16 entry count  u16 reserved
    builtin  if flags bit0: 36 x (u16 length, bytecode)            blocks 0..35 of the ET-312 built-in modes
    entries  count x:
               u8 kind (1 = built-in mode, 2 = routine)  u8 group  u8 name length  name (ASCII, <= 40)
               kind 1: u8 mode number
               kind 2: u8 start module  u8 module count  count x (u8 module number >= 0x80, u16 length, bytecode)
    trailer  u32 CRC-32 (zlib) of everything before it

Groups (what the remote's pattern list shows): 0 built-in modes, 1 ErosLink, 2 ErosLink examples, 3 your routines,
4 our routines.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from ..et312 import modes as M
from ..et312.foc312 import BUILTIN_MODES, BUILTIN_VARIANTS
from ..et312.vm import decode_module, encode_ops

MAGIC = b"FP31"
VERSION = 1
FLAG_BUILTIN = 1
KIND_BUILTIN, KIND_ROUTINE = 1, 2
GROUP_BUILTIN, GROUP_EROSLINK, GROUP_EXAMPLES, GROUP_YOURS, GROUP_OURS = 0, 1, 2, 3, 4
GROUP_NAMES = {GROUP_BUILTIN: "Built-in modes", GROUP_EROSLINK: "ErosLink", GROUP_EXAMPLES: "ErosLink examples",
               GROUP_YOURS: "Your routines", GROUP_OURS: "Our routines"}
NAME_MAX = 40
N_BUILTIN_BLOCKS = 36


class PackError(ValueError):
    pass


@dataclass
class Entry:
    name: str
    group: int
    mode: int | None = None                                   # built-in: its mode number
    start: int | None = None                                  # routine: entry module
    modules: dict[int, bytes] = field(default_factory=dict)   # routine: module number -> bytecode

    @property
    def kind(self) -> int:
        return KIND_BUILTIN if self.mode is not None else KIND_ROUTINE


@dataclass
class Pack:
    entries: list[Entry]
    builtin_blocks: dict[int, bytes] | None = None             # 0..35 -> bytecode


def display_name(name: str) -> str:
    """ASCII the remote's font can show, at most NAME_MAX characters."""
    s = "".join(c if 32 <= ord(c) < 127 else "?" for c in (name or "").strip()) or "?"
    return s[:NAME_MAX]


def _check_bytecode(code: bytes, what: str) -> None:
    ops = decode_module(code)          # raises on an opcode the VM doesn't know
    if not ops and code[:1] and code[0] >= 0x20:
        raise PackError(f"{what}: bytecode does not decode")


def build(pack: Pack) -> bytes:
    out = bytearray()
    flags = FLAG_BUILTIN if pack.builtin_blocks else 0
    out += MAGIC + struct.pack("<HHHH", VERSION, flags, len(pack.entries), 0)
    if flags & FLAG_BUILTIN:
        if sorted(pack.builtin_blocks) != list(range(N_BUILTIN_BLOCKS)):
            raise PackError(f"built-in blocks must be 0..{N_BUILTIN_BLOCKS - 1}")
        for i in range(N_BUILTIN_BLOCKS):
            code = pack.builtin_blocks[i]
            _check_bytecode(code, f"built-in block {i}")
            out += struct.pack("<H", len(code)) + code
    for e in pack.entries:
        name = display_name(e.name).encode("ascii")
        out += struct.pack("<BBB", e.kind, e.group, len(name)) + name
        if e.kind == KIND_BUILTIN:
            if not flags & FLAG_BUILTIN:
                raise PackError(f"{e.name}: a built-in mode needs the built-in blocks in the pack")
            out += struct.pack("<B", e.mode)
            continue
        if e.start not in e.modules:
            raise PackError(f"{e.name}: start module 0x{e.start:02x} is not among its modules")
        if not 1 <= len(e.modules) <= 64:
            raise PackError(f"{e.name}: {len(e.modules)} modules")
        out += struct.pack("<BB", e.start, len(e.modules))
        for num, code in sorted(e.modules.items()):
            if not 0x80 <= num <= 0xFF:
                raise PackError(f"{e.name}: module number 0x{num:02x} is outside the user range")
            _check_bytecode(code, f"{e.name} module 0x{num:02x}")
            out += struct.pack("<BH", num, len(code)) + code
    out += struct.pack("<I", zlib.crc32(bytes(out)) & 0xFFFFFFFF)
    return bytes(out)


def parse(data: bytes) -> Pack:
    """Reference parser (the C one in remote/core/pack.c must agree)."""
    if len(data) < 16 or data[:4] != MAGIC:
        raise PackError("not a pattern pack")
    if zlib.crc32(data[:-4]) & 0xFFFFFFFF != struct.unpack("<I", data[-4:])[0]:
        raise PackError("CRC mismatch")
    version, flags, count, _ = struct.unpack("<HHHH", data[4:12])
    if version != VERSION:
        raise PackError(f"pack version {version}")
    body, pos = data[:-4], 12

    def take(n: int) -> bytes:
        nonlocal pos
        if pos + n > len(body):
            raise PackError("truncated")
        chunk = body[pos:pos + n]
        pos += n
        return chunk

    builtin = None
    if flags & FLAG_BUILTIN:
        builtin = {}
        for i in range(N_BUILTIN_BLOCKS):
            (n,) = struct.unpack("<H", take(2))
            builtin[i] = take(n)
    entries = []
    for _ in range(count):
        kind, group, nlen = take(3)
        name = take(nlen).decode("ascii")
        if kind == KIND_BUILTIN:
            entries.append(Entry(name, group, mode=take(1)[0]))
        elif kind == KIND_ROUTINE:
            start, nmod = take(2)
            mods = {}
            for _ in range(nmod):
                num = take(1)[0]
                (n,) = struct.unpack("<H", take(2))
                mods[num] = take(n)
            entries.append(Entry(name, group, start=start, modules=mods))
        else:
            raise PackError(f"entry kind {kind}")
    if pos != len(body):
        raise PackError("trailing bytes")
    return Pack(entries, builtin)


# ---- what goes into the pack ----------------------------------------------------------------------------------

def collect(*, firmware=None, elk_dir: str | Path | None = None, ours_dir: str | Path | None = None,
            include_eroslink: bool = True) -> tuple[Pack, list[str]]:
    """Every pattern this PC can play, as a pack; plus notes on anything left out (and why)."""
    from ..et312 import elk, fwdata

    notes: list[str] = []
    entries: list[Entry] = []
    fw = firmware if firmware is not None else fwdata.default()
    builtin_blocks = None
    if fw is not None:
        builtin_blocks = {k: encode_ops(v) for k, v in fw.blocks.items() if k < N_BUILTIN_BLOCKS}
        for key, name in BUILTIN_MODES:
            entries.append(Entry(name, GROUP_BUILTIN, mode=M.MODE_BY_NAME[key]))
            for vkey, vname, after, _ in BUILTIN_VARIANTS:     # PlaStim variants follow the mode they vary
                if after == key:
                    entries.append(Entry(vname, GROUP_BUILTIN, mode=M.MODE_BY_NAME[vkey]))
    else:
        notes.append("no ET-312 firmware file extracted: the 18 built-in modes are left out (hub, M5 remote tab: Extract)")

    def add_routines(listing: list[dict], group_of) -> None:
        for r in listing:
            if r.get("error"):
                notes.append(f"{Path(r['file']).name}: unreadable ({r['error']})")
                continue
            try:
                cr = elk.load(r["path"])
            except Exception as exc:  # noqa: BLE001 - one bad routine must not stop the rest
                notes.append(f"{r['name']}: not compiled ({exc})")
                continue
            if cr.unresolved:
                notes.append(f"{r['name']}: left out (a module reference doesn't resolve, as in ErosLink)")
                continue
            entries.append(Entry(r["name"], group_of(r), start=cr.start, modules=dict(cr.modules)))

    if include_eroslink:
        listing = elk.list_routines(elk_dir)
        add_routines(listing, lambda r: {"bundled": GROUP_EROSLINK, "designer": GROUP_EXAMPLES}.get(
            r.get("source"), GROUP_YOURS))
    if ours_dir and Path(ours_dir).is_dir():
        add_routines(elk.list_routines(ours_dir, include_bundled=False), lambda r: GROUP_OURS)
    order = {GROUP_BUILTIN: 1, GROUP_EROSLINK: 0, GROUP_EXAMPLES: 2, GROUP_YOURS: 3, GROUP_OURS: 4}
    entries.sort(key=lambda e: order[e.group])       # stable: each group keeps its own order
    return Pack(entries, builtin_blocks), notes
