"""Pattern packs (stimengine/remote/pack.py -> remote/core/pack.c) and the remote's settings file.

The C reader must list exactly what Python built, reject every kind of damage before anything plays, and play each
entry exactly like the Python engine (same per-tick lines as tests/test_remote_core.py, compared by digest)."""
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from stimengine.et312 import fwdata
from stimengine.remote import m5config, pack as P
from tests.test_remote_core import CORE, ROOT, _bits, _synthetic_modules, _zig_available, block_lines, python_run
from stimengine.paths import BUILD_DIR, REMOTE_DIR

pytestmark = pytest.mark.skipif(not _zig_available(), reason="needs the ziglang package (pip install ziglang)")

SRC = [CORE / "pyrand.c", CORE / "et312.c", CORE / "pack.c", REMOTE_DIR / "test" / "pack_tool.c"]
EXE = BUILD_DIR / "remote" / ("pack_tool.exe" if os.name == "nt" else "pack_tool")


@pytest.fixture(scope="module")
def tool() -> Path:
    headers = [CORE / n for n in ("et312.h", "pyrand.h", "pack.h")]
    newest = max(p.stat().st_mtime for p in SRC + headers)
    if not EXE.exists() or EXE.stat().st_mtime < newest:
        EXE.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "ziglang", "cc", "-std=c99", "-O2", "-Wall", "-Wextra", "-Werror",
               f"-I{CORE}", *map(str, SRC), "-o", str(EXE)]
        if os.name != "nt":
            cmd.append("-lm")
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    return EXE


def run_tool(tool: Path, *args) -> list[str]:
    r = subprocess.run([str(tool), *map(str, args)], capture_output=True, text=True, check=True)
    return [ln.rstrip("\r") for ln in r.stdout.splitlines()]


def _synthetic_pack() -> P.Pack:
    mods = _synthetic_modules()
    # modules refer to each other by number (at_min / block timer / IF targets), so the second routine is its own
    other = {0x80: mods[0x81]}
    return P.Pack([
        P.Entry("Synéthetic one", P.GROUP_OURS, start=0x80, modules=mods),           # non-ASCII -> "?"
        P.Entry("x" * 60, P.GROUP_YOURS, start=0x80, modules=other),                       # long name trimmed
    ])


def _write(tmp_path: Path, data: bytes, name="p.bin") -> Path:
    f = tmp_path / name
    f.write_bytes(data)
    return f


def _python_listing(pk: P.Pack) -> list[str]:
    out = []
    for i, e in enumerate(pk.entries):
        out.append(f"{i} {e.kind} {e.group} {e.mode or 0} {e.start or 0} {len(e.modules)} {P.display_name(e.name)}")
    return out


def test_c_lists_what_python_built(tool, tmp_path):
    pk = _synthetic_pack()
    f = _write(tmp_path, P.build(pk))
    assert run_tool(tool, "list", f) == _python_listing(pk)
    back = P.parse(f.read_bytes())
    assert [e.name for e in back.entries] == ["Syn?thetic one", "x" * P.NAME_MAX]


def _reseal(body: bytes) -> bytes:
    return body + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF)


@pytest.mark.parametrize("damage, error", [
    ("flip", "CRC mismatch"), ("truncate", "CRC mismatch"), ("magic", "not a pattern pack"),
    ("module_low", "bad module"), ("bad_opcode", "invalid bytecode"), ("no_builtins", "built-in mode without"),
    ("start_missing", "start module missing"), ("trailing", "trailing bytes"), ("version", "version"),
])
def test_damage_is_rejected(tool, tmp_path, damage, error):
    good = P.build(_synthetic_pack())
    body = bytearray(good[:-4])
    if damage == "flip":
        data = bytes(good[:40]) + bytes([good[40] ^ 1]) + good[41:]
    elif damage == "truncate":
        data = good[:-9]
    elif damage == "magic":
        data = b"XP31" + good[4:]
    elif damage == "version":
        body[4] = 9
        data = _reseal(bytes(body))
    elif damage == "trailing":
        data = _reseal(bytes(body) + b"\x00")
    else:
        # a one-entry pack assembled by hand (the builder refuses these)
        name = b"bad"
        if damage == "no_builtins":
            entry = bytes([P.KIND_BUILTIN, 0, len(name)]) + name + bytes([0x76])
        else:
            num = 0x40 if damage == "module_low" else 0x80
            code = bytes([0x65, 0x00, 0x00]) if damage == "bad_opcode" else bytes([0x90, 0x07, 0x00])
            start = 0x81 if damage == "start_missing" else 0x80
            entry = bytes([P.KIND_ROUTINE, 3, len(name)]) + name + bytes([start, 1, num]) + struct.pack("<H", len(code)) + code
        data = _reseal(P.MAGIC + struct.pack("<HHHH", 1, 0, 1, 0) + entry)
    out = run_tool(tool, "list", _write(tmp_path, data))
    assert out and out[0].startswith("ERROR") and error in out[0], out


def test_builder_refuses_what_the_reader_would_reject():
    with pytest.raises(P.PackError):
        P.build(P.Pack([P.Entry("w", 0, mode=0x76)]))                              # built-in without blocks
    with pytest.raises(P.PackError):
        P.build(P.Pack([P.Entry("r", 3, start=0x81, modules={0x80: b"\x90\x07\x00"})]))   # start missing
    with pytest.raises(P.PackError):
        P.build(P.Pack([P.Entry("r", 3, start=0x40, modules={0x40: b"\x90\x07\x00"})]))   # not a user module
    with pytest.raises((P.PackError, ValueError)):
        P.build(P.Pack([P.Entry("r", 3, start=0x80, modules={0x80: b"\x65\x00\x00"})]))   # unknown opcode


def _python_play_digest(pk: P.Pack, e: P.Entry, ticks: int) -> str:
    """What pack_tool play prints, computed by the Python engine through the same script protocol."""
    blocks = dict(pk.builtin_blocks or {})
    if e.kind == P.KIND_ROUTINE:
        blocks.update(e.modules)
    s = "seed 7\n" + block_lines(blocks) + f"levels {_bits(0.7)} {_bits(0.45)}\n"
    s += (f"mode {e.mode}\n" if e.kind == P.KIND_BUILTIN else f"routine {e.start}\n")
    s += f"every 100000000\nrun {ticks} 3000\ndigest\n"
    return python_run(s)[-1]


def test_played_from_the_pack_matches_the_python_engine(tool, tmp_path):
    pk = _synthetic_pack()
    f = _write(tmp_path, P.build(pk))
    for i, e in enumerate(pk.entries):
        c = run_tool(tool, "play", f, i, 3000)[0]
        assert c == _python_play_digest(pk, e, 3000) + " play 0", (e.name, c)


@pytest.mark.needs_et312_data
def test_this_pcs_full_pack(tool, tmp_path):
    """Everything this PC can play: the C reader lists all of it, and a sample plays exactly like Python."""
    import tomllib
    with open(Path(__file__).resolve().parents[1] / "config" / "engine.toml", "rb") as f:
        elk_dir = tomllib.load(f).get("et312", {}).get("elk_dir", "")
    pk, notes = P.collect(elk_dir=elk_dir)
    f = _write(tmp_path, P.build(pk))
    assert run_tool(tool, "list", f) == _python_listing(pk)
    builtins = [i for i, e in enumerate(pk.entries) if e.kind == P.KIND_BUILTIN]
    routines = [i for i, e in enumerate(pk.entries) if e.kind == P.KIND_ROUTINE]
    for i in builtins + routines[:: max(1, len(routines) // 25)]:
        c = run_tool(tool, "play", f, i, 2500)[0]
        assert c == _python_play_digest(pk, pk.entries[i], 2500) + " play 0", pk.entries[i].name


# ---------------------------------------------------------------- settings file

def _m5(**over):
    base = {"wifi": {"ssid": "net", "password": "pw"}, "box": [{"name": "box 1", "host": "192.168.1.51"}]}
    base.update(over)
    return base


def test_settings_carry_the_engines_caps():
    eng = {"signal": {"waveform_amplitude_amps": 0.18}, "safety": {"slow_start_s": 5, "deadman_silence_s": 1.5},
           "et312": {"monophasic_asymmetry": 2.5}}
    cfg = m5config.build(eng, _m5(), {"pads": [True, True, False, False]})
    assert cfg["safety"] == {"amps_cap": 0.18, "slow_start_s": 5.0, "deadman_silence_s": 1.5,
                             "deadman_ramp_down_s": 3.0}
    assert cfg["boxes"] == [{"name": "box 1", "host": "192.168.1.51", "port": 55533}]
    assert cfg["defaults"]["pads"] == [True, True, False, False]
    assert cfg["et312"]["monophasic_asymmetry"] == 2.5


@pytest.mark.parametrize("eng, m5", [
    ({"signal": {"waveform_amplitude_amps": 0.25}}, _m5()),          # above the hard cap
    ({}, _m5(wifi={})),                                               # no Wi-Fi
    ({}, _m5(box=[])),                                                # no box
    ({}, _m5(box=[{"name": "x"}])),                                   # box without a host
])
def test_settings_refuse_bad_input(eng, m5):
    with pytest.raises(m5config.ConfigError):
        m5config.build(eng, m5)


def test_house_mode_is_the_default():
    assert m5config.build({}, _m5())["wifi"] == {"mode": "house", "ssid": "net", "password": "pw"}


def test_direct_mode_runs_the_remotes_own_network():
    direct = {"ssid": "stim-remote", "password": "longenough", "channel": 11}
    cfg = m5config.build({}, _m5(direct=direct, box=[{"name": "box 2", "mac": "02-AB-CD-00-00-02"}]))
    assert cfg["wifi"] == {"mode": "direct", "ssid": "stim-remote", "password": "longenough", "channel": 11}
    assert cfg["boxes"] == [{"name": "box 2", "host": "", "port": 55533, "mac": "02:ab:cd:00:00:02"}]
    # switched off: back to the house network, and the box needs its host again
    off = _m5(direct={**direct, "enabled": False})
    assert m5config.build({}, off)["wifi"]["mode"] == "house"


@pytest.mark.parametrize("direct, box", [
    ({"ssid": "r", "password": "short"}, [{"name": "a"}]),                            # WPA2 needs 8+
    ({"ssid": "r", "password": "longenough", "channel": 13}, [{"name": "a"}]),        # 1..11 only
    ({"ssid": "", "password": "longenough"}, [{"name": "a"}]),                        # no name
    ({"ssid": "r", "password": "longenough"}, [{"name": "a", "mac": "02:ab:cd"}]),    # bad mac
    ({"ssid": "r", "password": "longenough"}, [{"name": "a"}, {"name": "b"}]),        # two boxes, no macs
])
def test_direct_mode_refuses_bad_input(direct, box):
    with pytest.raises(m5config.ConfigError):
        m5config.build({}, _m5(direct=direct, box=box))


def test_pair_box_targets():
    from stimengine.remote import pairbox

    m5 = _m5(direct={"ssid": "stim-remote", "password": "longenough"})
    assert pairbox.target_network(m5, house=False) == ("stim-remote", "longenough")
    assert pairbox.target_network(m5, house=True) == ("net", "pw")
    with pytest.raises(m5config.ConfigError):
        pairbox.target_network(_m5(), house=False)
