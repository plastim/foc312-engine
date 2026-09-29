"""The M5 remote's C core (remote/core) must behave exactly like the Python engine (stimengine/et312).

Both run the same script (commands documented in remote/test/golden_runner.c) and every ticked frame must match:
the VM's whole memory (FNV-1a hash) and every output value, floats compared bit for bit. Long runs compare a digest
of every line plus every 500th line; a mismatch re-runs in full to report the first differing tick.

Nothing derived from ErosTek is stored here: the reference output is regenerated from the Python engine at test
time. Public checkouts run the synthetic routines below; with the user's own ET-312 data (fwdata.py) all 18
built-in modes run too, and with ErosLink files on the machine a sample of real routines.
"""
import hashlib
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from stimengine.et312 import elk, fwdata
from stimengine.et312 import modes as M
from stimengine.et312.engine import ET312Engine
from stimengine.et312.vm import decode_module, encode_ops

ROOT = Path(__file__).resolve().parents[1]
from stimengine.paths import BUILD_DIR, REMOTE_DIR  # noqa: E402

CORE = REMOTE_DIR / "core"
if not (CORE / "et312.c").exists():
    pytest.skip("needs the foc312-m5remote project next to this one (its core/)", allow_module_level=True)
RUNNER_SRC = [CORE / "pyrand.c", CORE / "et312.c", REMOTE_DIR / "test" / "golden_runner.c"]
EXE = BUILD_DIR / "remote" / ("golden_runner.exe" if os.name == "nt" else "golden_runner")


def _zig_available() -> bool:
    try:
        import ziglang  # noqa: F401
        return True
    except ImportError:
        return False


pytestmark = pytest.mark.skipif(not _zig_available(), reason="needs the ziglang package (pip install ziglang)")


@pytest.fixture(scope="module")
def runner() -> Path:
    newest = max(p.stat().st_mtime for p in RUNNER_SRC + [CORE / "et312.h", CORE / "pyrand.h"])
    if not EXE.exists() or EXE.stat().st_mtime < newest:
        EXE.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "ziglang", "cc", "-std=c99", "-O2", "-Wall", "-Wextra", "-Werror",
               f"-I{CORE}", *map(str, RUNNER_SRC), "-o", str(EXE)]
        if os.name != "nt":
            cmd.append("-lm")
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    return EXE


# ---------------------------------------------------------------- the Python side of the script protocol

def _bits(x: float) -> str:
    return struct.pack(">d", float(x)).hex()


def _unbits(h: str) -> float:
    return struct.unpack(">d", bytes.fromhex(h))[0]


def _fnv(data: bytes) -> int:
    h = 2166136261
    for b in data:
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h


def _chan(c) -> str:
    return (f" {int(c.gate_on)} {_bits(c.intensity)} {_bits(c.pulse_rate_hz)} {_bits(c.pulse_width_us)}"
            f" {int(c.biphasic)} {c.leading_polarity} {int(c.alternating)} {c.raw_gate} {c.raw_ramp}"
            f" {c.raw_intensity} {c.raw_frequency} {c.raw_width}")


PHASE = {"none": 0, "interleaved": 1, "linked": 2}


def python_run(script: str) -> list[str]:
    """Execute the script on the Python engine; returns the printed lines (same rules as golden_runner.c)."""
    blocks: dict[int, list] = {}
    e = None
    out: list[str] = []
    digest, every, counter = 2166136261, 1, 0

    def new_engine(seed: int):
        eng = ET312Engine(None, seed=seed, firmware=fwdata.FirmwareData({}, "script"))
        eng.vm.blocks.update(blocks)
        eng.builtins_available = all(i in eng.vm.blocks for i in range(2, 36))
        return eng

    def emit(f):
        nonlocal digest, counter
        line = (f"{f.tick} {_fnv(bytes(e.vm.mem)):08x} {f.mode}{_chan(f.a)} |{_chan(f.b)}"
                f" {PHASE[f.phase_mode]} {f.ma_value} {f.ctrl_flags} 0\n")
        for ch in line.encode():
            digest = ((digest ^ ch) * 16777619) & 0xFFFFFFFF
        counter += 1
        if counter % every == 0:
            out.append(line.rstrip("\n"))

    e = new_engine(0)
    for raw in script.splitlines():
        parts = raw.split()
        if not parts:
            continue
        cmd, args = parts[0], parts[1:]
        if cmd == "seed":
            e = new_engine(int(args[0]))
        elif cmd == "power":
            e.power = int(args[0])
            e.vm.mem[0x1F4] = e.power
        elif cmd == "level_model":
            e.level_model = args[0]
        elif cmd == "skip_ramp":
            e.skip_mode_ramp = bool(int(args[0]))
        elif cmd == "random1":
            e.random1_ma = args[0]
        elif cmd == "split":
            e.split = (int(args[0], 0), int(args[1], 0))
        elif cmd == "block":
            idx = int(args[0], 0)
            blocks[idx] = decode_module(bytes.fromhex(args[1]))
            e.vm.blocks[idx] = blocks[idx]
            e.builtins_available = all(i in e.vm.blocks for i in range(2, 36))
        elif cmd == "clear_user_blocks":
            for k in [k for k in e.vm.blocks if k >= 0x80]:
                del e.vm.blocks[k]
            for k in [k for k in blocks if k >= 0x80]:
                del blocks[k]
        elif cmd == "mode":
            e.set_mode(int(args[0], 0))
        elif cmd == "routine":
            e.user_start[M.USER1] = int(args[0], 0)
            e.set_mode(M.USER1)
        elif cmd == "silent":
            e._silent()
        elif cmd == "start_ramp":
            e.start_ramp()
        elif cmd == "levels":
            e.set_levels(_unbits(args[0]), _unbits(args[1]))
        elif cmd == "ma":
            e.set_ma(_unbits(args[0]))
        elif cmd == "run":
            n, period = int(args[0]), int(args[1])
            for t in range(n):
                if period > 0:
                    e.set_ma((t % period) / period)
                emit(e.step())
        elif cmd == "frame":
            emit(e.frame())
        elif cmd == "every":
            every = max(1, int(args[0]))
        elif cmd == "digest":
            out.append(f"digest {digest:08x}")
    return out


def c_run(runner: Path, script: str) -> list[str]:
    r = subprocess.run([str(runner)], input=script, capture_output=True, text=True, check=True)
    return [ln.rstrip("\r") for ln in r.stdout.splitlines()]


def assert_same(runner: Path, script: str, label: str) -> None:
    py, c = python_run(script), c_run(runner, script)
    if py == c:
        return
    # locate the first differing tick with full output
    full = "every 1\n" + "\n".join(ln for ln in script.splitlines() if not ln.startswith("every"))
    py, c = python_run(full), c_run(runner, full)
    for i, (a, b) in enumerate(zip(py, c)):
        if a != b:
            pytest.fail(f"{label}: first difference at output line {i}\n  python: {a}\n  c:      {b}")
    pytest.fail(f"{label}: outputs differ in length ({len(py)} vs {len(c)})")


def block_lines(blocks: dict[int, bytes]) -> str:
    return "".join(f"block {k} {v.hex()}\n" for k, v in sorted(blocks.items()))


# ---------------------------------------------------------------- synthetic routines (ours; public)

def _synthetic_modules() -> dict[int, bytes]:
    """Two-channel routine that exercises every opcode, modulator source/rate option, the gate, the block timer,
    random ranges and the IF branch. Our own bytecode, not taken from any ErosTek program."""
    start = encode_ops([
        ("set", 0, 0x90, 0x07),                                   # gate on, biphasic
        ("blk", 0, 0xA5, bytes([0x80, 0x40, 0xF0, 0x02, 0x03, 0xFF, 0xFE])),   # intensity ramps, 0xfe at max
        ("set", 0, 0xAC, 0x01),                                   # ... on the fast timer
        ("blk", 0, 0xAE, bytes([0x40, 0x10, 0xC0, 0x05, 0xFC, 0x81, 0xFD])),   # frequency falls, at_min -> 0x81
        ("set", 0, 0xB5, 0x02),                                   # ... on the slow timer
        ("set", 0, 0xBE, 0x08),                                   # width follows MA (static)
        ("set", 0, 0x98, 0x10), ("set", 0, 0x99, 0x08), ("set", 0, 0x9A, 0x01),   # gating
        ("set", 0, 0x95, 0x03), ("set", 0, 0x96, 0x03), ("set", 0, 0x97, 0x82),   # block timer -> 0x82
        ("set", 0, 0x8D, 0x10), ("set", 0, 0x8E, 0x60), ("rand", 0, 0xB1),         # random rate
        ("set", 1, 0xB5, 0x0D | 0x10), ("set", 1, 0xAE, 0x30),    # B frequency: other channel's value, inverted
        ("set", 1, 0xAC, 0x61), ("set", 1, 0xA9, 0x02),           # B intensity rate = A's rate
        ("set", 1, 0xBE, 0xC2), ("set", 1, 0xBB, 0x01),           # B width: MA rate, inverted, slow timer
        ("ldacc", 2, 0x0D), ("stacc", 0, 0x9D), ("shr", 0, 0x9D), # ACC = MA; ramp min = MA / 2
        ("add", 0, 0x9F, 0x05), ("and", 0, 0xA0, 0xF7), ("or", 0, 0x9F, 0x01), ("xor", 0, 0xA0, 0x02),
        ("set", 0, 0x84, 0x83), ("ifacc", 0, 0x8C),               # IF (ACC == ACC) -> next tick load 0x83
    ])
    m81 = encode_ops([("xor", 0, 0x90, 0x06), ("set", 0, 0xB2, 0x04), ("set", 0, 0xB3, 0xFF),
                      ("set", 0, 0xB4, 0x80), ("set", 1, 0xA5, 0x60)])   # flip pulse bits, rise, reverse
    m80b = encode_ops([("set", 0, 0xAC, 0x21), ("rand", 1, 0xB1), ("add", 1, 0xA7, 0x10),
                       ("set", 0, 0x9A, 0x45), ("set", 0, 0xB7, 0xFC)])  # timers from Advanced, MA gate
    m83 = encode_ops([("set", 0, 0x83, 0x04), ("set", 0, 0x84, 0x00), ("set", 0, 0xBE, 0x04)])
    return {0x80: start, 0x81: m81, 0x82: m80b, 0x83: m83}


def test_synthetic_routine_matches_tick_for_tick(runner):
    s = "seed 5\n" + block_lines(_synthetic_modules())
    s += f"levels {_bits(0.8)} {_bits(0.35)}\nroutine 0x80\nframe\nrun 3000 0\nrun 3000 1700\n"
    s += f"power 2\nlevel_model linear\nlevels {_bits(1.0)} {_bits(0.05)}\nrun 1500 311\nstart_ramp\nrun 800 0\n"
    s += "silent\nrun 200 0\nroutine 0x80\nrun 1000 97\n"
    assert_same(runner, s, "synthetic routine")


def test_climb_like_eroslink_routine_matches(runner):
    from tests.test_et312_elk import _climb_like
    cr = elk.compile_routine(_climb_like())
    s = "seed 1\n" + block_lines(cr.modules) + f"levels {_bits(0.6)} {_bits(0.9)}\nroutine {cr.start}\n"
    s += "every 50\nrun 6000 2400\ndigest\n"
    assert_same(runner, s, "climb-like routine")


# ---------------------------------------------------------------- the user's own data (skipped when absent)

@pytest.mark.needs_et312_data
@pytest.mark.parametrize("name", sorted(M.IMPLEMENTED + M.STUBBED + ("split", "climb_slow", "climb_hold")))
def test_builtin_mode_matches(runner, name):
    fw = fwdata.default()
    blocks = {k: encode_ops(v) for k, v in fw.blocks.items()}
    seed = int(hashlib.sha1(name.encode()).hexdigest()[:6], 16)
    mode = M.MODE_BY_NAME[name]
    s = f"seed {seed}\n" + block_lines(blocks) + f"split {M.STROKE} {M.WAVES}\n"
    s += f"levels {_bits(0.7)} {_bits(0.45)}\nmode {mode}\nevery 500\nrun 12000 4000\n"
    s += f"random1 uniform\nlevel_model linear\npower 0\nlevels {_bits(1.0)} {_bits(0.2)}\nmode {mode}\n"
    s += f"run 6000 1111\nstart_ramp\nma {_bits(0.93)}\nrun 3000 0\n"
    s += f"skip_ramp 1\nmode {mode}\nrun 2000 700\ndigest\n"
    assert_same(runner, s, name)


def _elk_sample(n: int = 30) -> list[Path]:
    from tests.test_et312_elk import _elk_files
    files = _elk_files()
    return files[:: max(1, len(files) // n)][:n]


@pytest.mark.skipif(not _elk_sample(), reason="no .elk files on this machine")
@pytest.mark.parametrize("path", _elk_sample(), ids=lambda p: p.stem)
def test_real_eroslink_routine_matches(runner, path):
    try:
        cr = elk.load(path)
    except Exception as exc:  # noqa: BLE001 - a file the importer can't read is not this test's subject
        pytest.skip(f"not loadable: {exc}")
    s = "seed 3\n" + block_lines(cr.modules) + f"levels {_bits(0.75)} {_bits(0.75)}\nroutine {cr.start}\n"
    s += "every 400\nrun 8000 3000\ndigest\n"
    assert_same(runner, s, path.stem)
