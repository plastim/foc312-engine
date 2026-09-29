"""Stage 2 of the M5 remote's C core: foc312's fork view + the engine's safety stack (remote/core/{foc312,safety}).

The full pipeline runs on a simulated clock in both languages: ET-312 ticks -> foc312 channel view (pads guard,
route, shape) -> the safety stack (arm / slow start, deadman, volume law, shape fade, cap) -> the values sent to the
box. The Python side is stimengine's own Foc312Runner + Engine, driven by hand with a stub link.

Python rounds a few display values (intensity to 4 places, rate to 0.1 Hz) with decimal rounding that portable C
matches only away from exact decimal ties, so floats are compared within tolerances far below anything felt (amps
25 uA, rate 0.11 Hz) and at most 5 % of values may differ in the last bits; widths, asymmetry, routes, shapes and
the safety state (volume, master, deadman, pad gains) must match exactly.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from stimengine.et312 import fwdata
from stimengine.et312 import modes as M
from stimengine.et312.engine import ET312Engine
from stimengine.et312.vm import decode_module, encode_ops
from tests.test_remote_core import CORE, ROOT, _bits, _synthetic_modules, _unbits, _zig_available, block_lines
from stimengine.paths import BUILD_DIR, REMOTE_DIR

pytestmark = pytest.mark.skipif(not _zig_available(), reason="needs the ziglang package (pip install ziglang)")

SRC = [CORE / "pyrand.c", CORE / "et312.c", CORE / "safety.c", CORE / "foc312.c",
       REMOTE_DIR / "test" / "golden_pipeline.c"]
EXE = BUILD_DIR / "remote" / ("golden_pipeline.exe" if os.name == "nt" else "golden_pipeline")
# amps A/B, rate A/B, width A/B, asym A/B, route A/B, shape A/B, volume, master, deadman, pad gain A/B
TOL = [25e-6, 25e-6, 0.11, 0.11, 0, 0, 0, 0, 0, 0, 0, 0, 1e-12, 1e-12, 1e-12, 1e-12, 1e-12]


@pytest.fixture(scope="module")
def pipeline() -> Path:
    headers = [CORE / n for n in ("et312.h", "pyrand.h", "safety.h", "foc312.h")]
    newest = max(p.stat().st_mtime for p in SRC + headers)
    if not EXE.exists() or EXE.stat().st_mtime < newest:
        EXE.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "ziglang", "cc", "-std=c99", "-O2", "-Wall", "-Wextra", "-Werror",
               f"-I{CORE}", *map(str, SRC), "-o", str(EXE)]
        if os.name != "nt":
            cmd.append("-lm")
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    return EXE


class _Done:
    def add_done_callback(self, fn):
        pass


class _StubClient:
    """What Engine touches of the device client while driven by hand: an open link that accepts writes."""
    is_open = True
    pending_count = 0
    closed_reason = None

    def axis_move_to_nowait(self, *a, **k):
        return _Done()

    def on(self, kind, callback):
        pass


def python_pipeline(script: str) -> list[list[float]]:
    from stimengine.device import fork as F
    from stimengine.engine import TICK_S, Engine
    from stimengine.et312.foc312 import Foc312Runner
    from tests.test_engine_core import make_config

    class _ForkEngine(Engine):
        fork_version = 6
        fork_firmware = True

    clock = [0.0]

    def clk() -> float:
        return clock[0]

    seed, blocks = 0, {}
    eng = run = None
    rows: list[list[float]] = []

    def fresh_et():
        et = ET312Engine(None, level_a=0.0, level_b=0.0, ma=0.5, seed=seed, firmware=fwdata.FirmwareData({}, "s"))
        et.vm.blocks.update(blocks)
        et.builtins_available = all(i in et.vm.blocks for i in range(2, 36))
        return et

    for raw in script.splitlines():
        p = raw.split()
        if not p:
            continue
        cmd, args = p[0], p[1:]
        if cmd == "seed":
            seed = int(args[0])
        elif cmd == "block":
            blocks[int(args[0], 0)] = decode_module(bytes.fromhex(args[1]))
        elif cmd == "safety":
            slow, dead, ramp, cap = (_unbits(a) for a in args)
            cfg = make_config(slow_start_s=slow, deadman_silence_s=dead, deadman_ramp_down_s=ramp)
            cfg["signal"]["waveform_amplitude_amps"] = cap
            eng = _ForkEngine(cfg, _StubClient(), None, clock=clk)
            eng.running, eng.signal_on_flag, eng.mode = True, True, "biphasic"
            run = Foc312Runner(eng, cfg, clock=clk, seed=seed)
            run.et = fresh_et()
            run.frame = run.et.frame()
        elif cmd == "mode":
            run.et.set_mode(int(args[0], 0))
            run.frame = run.et.frame()
        elif cmd == "routine":
            run.et.user_start[M.USER1] = int(args[0], 0)
            run.et.set_mode(M.USER1)
            run.frame = run.et.frame()
        elif cmd == "output_on":
            run.output = "fork"
        elif cmd == "levels":
            run.set_levels(_unbits(args[0]), _unbits(args[1]))
        elif cmd == "ma":
            run.set_ma(_unbits(args[0]))
        elif cmd == "master":
            eng.set_master(_unbits(args[0]), source="m5")
        elif cmd == "arm":
            eng.arm()
        elif cmd == "disarm":
            eng.disarm()
        elif cmd == "hb":
            eng.renew_lease("m5")
        elif cmd == "route":
            run.set_route(int(args[0]), int(args[1]))
        elif cmd == "pads":
            run.set_pads([c == "1" for c in args[0]])
        elif cmd == "shape":
            run.set_shape(int(args[0]))
        elif cmd == "mono":
            run.monophasic_asymmetry = _unbits(args[0])
        elif cmd == "loop":
            n, dt = int(args[0]), _unbits(args[1])
            for _ in range(n):
                clock[0] += dt
                run.advance()
                now = clock[0]
                edt = TICK_S if eng._last_tick == 0.0 else max(0.0, min(now - eng._last_tick, 0.25))
                eng._last_tick = now
                eng._update_master(edt)
                eng._update_deadman(now, edt)
                v = eng._biphasic_values()
                rows.append([v[F.AMP_AXES[0]], v[F.AMP_AXES[1]], v[F.FREQ_AXES[0]], v[F.FREQ_AXES[1]],
                             v[F.WIDTH_AXES[0]], v[F.WIDTH_AXES[1]], v[F.ASYM_AXES[0]], v[F.ASYM_AXES[1]],
                             v[F.ROUTE_AXES[0]], v[F.ROUTE_AXES[1]], v[F.SHAPE_AXES[0]], v[F.SHAPE_AXES[1]],
                             eng._volume_law(), eng._master_now, eng._deadman_scale,
                             run._pad_gain[0], run._pad_gain[1]])
    return rows


def c_pipeline(exe: Path, script: str) -> list[list[float]]:
    r = subprocess.run([str(exe)], input=script, capture_output=True, text=True, check=True)
    return [[float(x) for x in ln.split()] for ln in r.stdout.splitlines() if ln.strip()]


def assert_pipeline_same(exe: Path, script: str, label: str) -> None:
    py, c = python_pipeline(script), c_pipeline(exe, script)
    assert len(py) == len(c), f"{label}: {len(py)} python rows vs {len(c)} c rows"
    inexact = total = 0
    for i, (a, b) in enumerate(zip(py, c)):
        for j, (x, y) in enumerate(zip(a, b)):
            total += 1
            if abs(float(x) - y) > TOL[j]:
                pytest.fail(f"{label}: row {i} column {j}: python {x!r} vs c {y!r}\n  py {a}\n  c  {b}")
            inexact += float(x) != y
    assert inexact <= total // 20, f"{label}: {inexact} of {total} values not bit-identical"
    # the pipeline must actually have produced output (a silent run would compare trivially)
    assert max(row[0] + row[1] for row in py) > 0.01, f"{label}: no output reached the box"


def _session(dt60: str) -> str:
    """Arm + slow start, levels, route and pad changes, shapes, monophasic asymmetry, deadman, disarm, jitter."""
    s = f"output_on\nlevels {_bits(0.8)} {_bits(0.6)}\nma {_bits(0.4)}\nmaster {_bits(1.0)}\narm\n"
    s += f"loop 400 {dt60}\nhb\nroute 0 21\nloop 60 {dt60}\nroute 1 23\nshape 2\nloop 90 {dt60}\nhb\n"
    s += f"pads 1011\nloop 30 {dt60}\npads 1111\nloop 90 {dt60}\nhb\nmono {_bits(2.5)}\nshape 1\nloop 60 {dt60}\n"
    s += f"master {_bits(0.3)}\nloop 20 {dt60}\nmaster {_bits(0.9)}\nloop 400 {dt60}\n"          # silence: deadman
    s += f"hb\nloop 30 {_bits(0.021)}\ndisarm\nloop 20 {dt60}\nhb\narm\nloop 100 {_bits(0.013)}\n"
    s += f"levels {_bits(0.2)} {_bits(1.0)}\nshape 0\nhb\nloop 120 {dt60}\n"
    return s


def test_pipeline_synthetic_routine(pipeline):
    s = "seed 9\n" + block_lines(_synthetic_modules())
    s += f"safety {_bits(4.0)} {_bits(2.0)} {_bits(3.0)} {_bits(0.15)}\nroutine 0x80\n"
    assert_pipeline_same(pipeline, s + _session(_bits(1 / 60)), "pipeline synthetic")


@pytest.mark.needs_et312_data
@pytest.mark.parametrize("name", ["waves", "stroke", "climb", "toggle", "orgasm", "phase3", "split", "random1"])
def test_pipeline_builtin_mode(pipeline, name):
    fw = fwdata.default()
    blocks = {k: encode_ops(v) for k, v in fw.blocks.items()}
    s = "seed 11\n" + block_lines(blocks)
    s += f"safety {_bits(2.0)} {_bits(1.5)} {_bits(2.0)} {_bits(0.2)}\nmode {M.MODE_BY_NAME[name]}\n"
    assert_pipeline_same(pipeline, s + _session(_bits(1 / 60)), f"pipeline {name}")
