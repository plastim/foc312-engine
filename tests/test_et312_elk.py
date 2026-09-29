"""ErosLink .elk importer: Java-serialization reader, ingredient lowering, module compiler, VM wiring.

The compiler is checked against ErosLink itself: tests/data/elk_eroslink_oracle.json holds the sha1 of
ErosLink's own compile of every routine in the installer and in PlaStim's collection (keyed by .elk sha1).
Those tests read the files at runtime and skip when they are absent (no routine data in the repo)."""
from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import pytest

from stimengine.et312 import ET312Engine, elk, javaser
from stimengine.et312.elk import CH_BOTH, Ingredient, RoutineDef

def _user_elk_dir() -> Path | None:
    """This PC's own .elk folder, from [et312] elk_dir in config/engine.toml (none when unset)."""
    import tomllib
    with open(Path(__file__).resolve().parents[1] / "config" / "engine.toml", "rb") as f:
        d = tomllib.load(f).get("et312", {}).get("elk_dir", "")
    return Path(d) if d else None


USER_DIR = _user_elk_dir()
ORACLE = json.loads((Path(__file__).parent / "data" / "elk_eroslink_oracle.json").read_text(encoding="utf-8"))


def _elk_files() -> list[Path]:
    cache = elk.default_cache_dir()
    out = []
    for d in (cache / "bundled", cache / "designer", USER_DIR):
        if d is not None and d.is_dir():
            out += sorted(d.glob("*.elk"))
    return out


def _oracle_hash(cr: elk.CompiledRoutine) -> str:
    body = ";".join(f"{k:02x}:{cr.modules[k].hex(' ')}" for k in sorted(cr.modules))
    return hashlib.sha1(f"{cr.start:02x}|{body}".encode()).hexdigest()


# ------------------------------------------------------------------ javaser

def test_javaser_reads_strings_blockdata_and_backrefs():
    s = b"abc"
    data = (struct.pack(">HH", 0xACED, 5) + b"\x74" + struct.pack(">H", 3) + s
            + b"\x77\x05" + struct.pack(">i?", 7, True)
            + b"\x71" + struct.pack(">I", 0x7E0000))          # back-reference to "abc"
    top = javaser.parse(data)
    assert top[0] == "abc" and top[2] == "abc"
    it = javaser.Items(top)
    assert it.read_string() == "abc"
    assert it.read_int() == 7 and it.read_bool() is True
    assert it.read_string() == "abc"


def test_javaser_rejects_non_java_data():
    with pytest.raises(javaser.JavaSerError):
        javaser.parse(b"PK\x03\x04 not a java stream")


# ------------------------------------------------------------------ compiler (synthetic routine)

def _climb_like() -> RoutineDef:
    """Channel BOTH + three Multi-A frequency ramps chained slow -> med -> fast -> slow (the shape of the
    CD's designer "Climb").  Expected bytes are ErosLink's own output for that routine."""
    def mar(name, time, then, other):
        return Ingredient("MultiARampIngredient", name, "<Nothing Else>", dict(
            start=30.0, end=100.0, time=time, and_then=then, intensity=False, frequency=True, width=False,
            full_range=False, ma_intensity=False, ma_frequency=True, ma_width=False, ma_affects_min=False,
            other_bound=other))
    return RoutineDef("Climb", "", [
        Ingredient("ChannelIngredient", "1", "slow ramp", dict(channel=CH_BOTH)),
        mar("slow ramp", 2.0, "med ramp", 30.0),
        mar("med ramp", 1.0, "fast ramp", 15.0),
        mar("fast ramp", 0.5, "slow ramp", 7.5),
    ])


def test_compile_matches_eroslink_output_for_a_climb_routine():
    cr = elk.compile_routine(_climb_like())
    assert cr.start == 0x80 and cr.fits_in_box and not cr.unresolved
    h = {k: v.hex(" ") for k, v in cr.modules.items()}
    assert h == {
        0x80: "85 03 28 86 02 2b 38 ae b4 08 b4 02 ff 81 28 b4 fc 41 00",
        0x81: "28 86 02 2b 38 ae b4 08 b4 02 fe 82 28 b4 fc 41 00",
        0x82: "28 86 02 20 38 ae b4 08 b4 02 fd 83 28 b4 fc 41 00",
        0x83: "28 86 02 2b 38 ae b4 08 b4 02 ff 81 28 b4 fc 41 00",
    }


def test_decode_module_follows_the_firmware_bytecode():
    ops = elk.decode_module(bytes.fromhex("85 03 38 ae b4 08 b4 02 ff 81 54 be e3 58 b5 08 4c 95 00 99"))
    assert ops == [("set", 0, 0x85, 3), ("blk", 0, 0xAE, bytes.fromhex("b4 08 b4 02 ff 81")),
                   ("and", 0, 0xBE, 0xE3), ("or", 0, 0xB5, 0x08), ("rand", 0, 0x95)]     # stops at 00
    assert elk.decode_module(bytes.fromhex("c5 02")) == [("set", 1, 0x85, 2)]           # bit6 = B page


def test_engine_runs_a_compiled_routine_as_user1():
    cr = elk.compile_routine(_climb_like())
    e = ET312Engine(cr, ma=0.5)
    freqs = {f.a.raw_frequency for f in e.run(20)}
    assert e.frame().mode_name == "Climb"
    assert min(freqs) < 60 and max(freqs) >= 170          # the ramps sweep and restart (no ET-312 data needed)


@pytest.mark.needs_et312_data
def test_switching_from_a_routine_to_a_builtin_mode():
    e = ET312Engine(elk.compile_routine(_climb_like()), ma=0.5)
    e.run(5)
    e.set_mode("waves")
    assert e.frame().mode_name == "waves" and e.routine is None


# ------------------------------------------------------------------ real files (skipped when absent)

FILES = _elk_files()


@pytest.mark.skipif(not FILES, reason="no .elk files on this machine (cache or PlaStim's folder)")
def test_every_routine_compiles_exactly_like_eroslink():
    checked = 0
    for f in FILES:
        data = f.read_bytes()
        want = ORACLE["files"].get(hashlib.sha1(data).hexdigest())
        ctx = elk.read_context(data)
        for i, r in enumerate(ctx.routines):
            cr = elk.compile_routine(r)
            if want is not None:
                assert _oracle_hash(cr) == want[i], f"{f.name} #{i} {r.name!r}"
                checked += 1
    assert checked > 0


@pytest.mark.skipif(not FILES, reason="no .elk files on this machine (cache or PlaStim's folder)")
def test_every_routine_runs_in_the_emulator():
    for f in FILES:
        for i in range(len(elk.read_file(f).routines)):
            cr = elk.load(f, i)
            e = ET312Engine(cr, ma=0.5, seed=1)
            for _ in e.run(2):
                pass


@pytest.mark.skipif(not (elk.default_cache_dir() / "bundled").is_dir(), reason="ErosLink cache not built")
def test_list_routines_groups_the_bundled_set_first():
    rs = elk.list_routines(USER_DIR if USER_DIR is not None and USER_DIR.is_dir() else None)
    bundled = [r for r in rs if r["bundled"]]
    assert len(bundled) == 12 and all(r["source"] == "bundled" for r in bundled)
    assert rs[:12] == bundled
    assert len({r["path"] for r in rs}) == len(rs)             # multi-routine files get "#index" paths
    multi = next(r for r in rs if r.get("file_routines", 1) > 1)
    assert elk.load(multi["path"]).name == multi["name"]
