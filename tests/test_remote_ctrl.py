"""The M5 remote's controller (remote/core/ctrl.c): the remote's own rules from notes/m5-remote.md, on the host."""
import ctypes
import os
import subprocess
import sys

import pytest

from stimengine.remote import pack as P
from tests.test_remote_core import CORE, ROOT, _synthetic_modules, _zig_available
from stimengine.paths import BUILD_DIR, REMOTE_DIR

pytestmark = pytest.mark.skipif(not _zig_available(), reason="needs the ziglang package (pip install ziglang)")

SRC = [CORE / n for n in ("pyrand.c", "et312.c", "safety.c", "foc312.c", "pack.c", "ctrl.c")] + \
      [REMOTE_DIR / "test" / "ffi_ctrl.c"]
LIB = BUILD_DIR / "remote" / ("remote_ctrl.dll" if os.name == "nt" else "remote_ctrl.so")
RUN, PATTERNS, OPTIONS = 0, 1, 2
K1, K2, K3, K4 = 0, 1, 2, 3                    # run screen: master, MA, level 1, level 2
(OPT_WIRES1, OPT_POL1, OPT_WIRES2, OPT_POL2, OPT_PAD1, OPT_PAD2, OPT_PAD3, OPT_PAD4, OPT_SWAP, OPT_SHAPE,
 OPT_SKIP, OPT_BOX, OPT_DEVICE, OPT_BACK) = range(14)


@pytest.fixture(scope="module")
def lib():
    headers = [CORE / n for n in ("et312.h", "pyrand.h", "safety.h", "foc312.h", "pack.h", "ctrl.h")]
    newest = max(p.stat().st_mtime for p in SRC + headers)
    if not LIB.exists() or LIB.stat().st_mtime < newest:
        LIB.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "ziglang", "cc", "-std=c99", "-O2", "-Wall", "-Wextra", "-Werror", "-shared",
               f"-I{CORE}", *map(str, SRC), "-o", str(LIB)]
        if os.name != "nt":
            cmd += ["-fPIC", "-lm"]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    d = ctypes.CDLL(str(LIB))
    d.rc_init.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_double, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, ctypes.c_int]
    d.rc_knob.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_double]
    for f in ("rc_push1", "rc_push4", "rc_button", "rc_tick"):
        getattr(d, f).argtypes = [ctypes.c_double]
    d.rc_link.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_double]
    for f in ("rc_amps", "rc_foc_level", "rc_volume"):
        getattr(d, f).restype = ctypes.c_double
    d.rc_status.restype = ctypes.c_char_p
    d.rc_option.restype = ctypes.c_char_p
    return d


PACK = P.build(P.Pack([P.Entry("Ours A", P.GROUP_OURS, start=0x80, modules=_synthetic_modules()),
                       P.Entry("Ours B", P.GROUP_OURS, start=0x80, modules={0x80: _synthetic_modules()[0x81]})]))


class Remote:
    def __init__(self, d, saved=None, n_boxes=2):
        self.d, self.t = d, 0.0
        pattern, r0, r1, box = saved or (-1, 12, 34, 0)
        assert d.rc_init(PACK, len(PACK), n_boxes, 0.0, 1 if saved else 0, pattern, r0, r1, box) == 0

    def run(self, seconds, dt=1 / 60):
        for _ in range(int(round(seconds / dt))):
            self.t += dt
            self.d.rc_tick(self.t)

    def knob(self, k, n):
        self.d.rc_knob(k, n, self.t)

    def push1(self):
        self.d.rc_push1(self.t)

    def push4(self):
        self.d.rc_push4(self.t)

    def button(self):
        self.d.rc_button(self.t)

    def link(self, running=True, fault=b""):
        self.d.rc_link(1 if running else 0, fault, self.t)

    def status(self):
        return self.d.rc_status().decode()

    def max_amps(self, seconds):
        """Peak output per channel over a window (the ET-312's gate and modulation move it every tick)."""
        mx = [0.0, 0.0]
        for _ in range(int(round(seconds * 60))):
            self.run(1 / 60)
            mx = [max(mx[0], self.d.rc_amps(0)), max(mx[1], self.d.rc_amps(1))]
        return mx

    def pick(self, index):
        self.push1()
        self.knob(K1, index - self.d.rc_cursor())
        self.push1()


def test_boot_state(lib):
    r = Remote(lib)
    assert (r.d.rc_level(0), r.d.rc_level(1), r.d.rc_master()) == (0, 0, 100)
    assert not r.d.rc_armed() and r.d.rc_screen() == RUN and r.d.rc_pattern() == -1
    assert "pick a pattern" in r.status()


def test_start_needs_a_link_and_a_pattern(lib):
    r = Remote(lib)
    r.button()
    assert not r.d.rc_armed() and "no box" in r.status()
    r.link()
    r.button()
    assert not r.d.rc_armed() and "pick a pattern" in r.status()
    r.pick(0)
    assert r.d.rc_pattern() == 0 and r.d.rc_screen() == RUN
    r.button()
    assert r.d.rc_armed()


def _armed_remote(lib, level=40):
    r = Remote(lib)
    r.link()
    r.pick(0)
    r.knob(K2, level)
    r.knob(K3, level)
    r.run(level / 10 + 0.5)                   # levels rise at 10 steps/s
    r.button()
    return r


def test_slow_start_then_output(lib):
    r = _armed_remote(lib)
    r.run(0.5)
    early = r.d.rc_volume()
    r.run(4.0)
    assert 0 < early < 0.2 and r.d.rc_volume() == pytest.approx(1.0)
    assert r.max_amps(3.0)[0] > 0.01


def test_stop_always_wins_and_never_toggles_back_on(lib):
    r = _armed_remote(lib)
    r.run(1.0)
    r.button()
    assert not r.d.rc_armed() and r.d.rc_volume() == 0.0
    r.run(0.3)
    r.button()                                # a quick second press does not restart
    assert not r.d.rc_armed()
    r.run(1.0)
    r.button()                                # a deliberate press later does
    assert r.d.rc_armed()


def test_levels_rise_at_most_10_steps_a_second_and_fall_at_once(lib):
    r = Remote(lib)
    r.knob(K2, 60)                        # a fast spin
    assert r.d.rc_level_target(0) == 60 and r.d.rc_level(0) == 0
    r.run(1.0)
    assert 9 <= r.d.rc_level(0) <= 11
    r.run(6.0)
    assert r.d.rc_level(0) == 60
    r.knob(K2, -45)
    assert r.d.rc_level(0) == 15              # down: instant, before the next tick
    r.knob(K1, -80)
    assert r.d.rc_master() == 20
    r.knob(K1, 80)
    r.run(1.0)
    assert 29 <= r.d.rc_master() <= 31


def test_levels_stay_with_the_wires_through_a_swap(lib):
    r = Remote(lib)
    r.knob(K2, 50)
    r.knob(K3, 20)
    r.run(6.0)
    assert (r.d.rc_foc_level(0), r.d.rc_foc_level(1)) == (0.5, 0.2)
    assert (r.d.rc_foc_route(0), r.d.rc_foc_route(1)) == (12, 34)
    r.push4()
    r.knob(K1, OPT_SWAP)
    r.push1()
    r.run(0.1)
    # channel A's pattern now plays on position 2's wires (34) at position 2's level; the knobs still own the wires
    assert (r.d.rc_foc_route(0), r.d.rc_foc_route(1)) == (34, 12)
    assert (r.d.rc_foc_level(0), r.d.rc_foc_level(1)) == (0.2, 0.5)
    assert (r.d.rc_level(0), r.d.rc_level(1)) == (50, 20)


def test_a_fault_stops_and_zeroes_the_levels(lib):
    r = _armed_remote(lib)
    r.run(1.0)
    r.link(False, b"the box rebooted (a trip or a power cycle)")
    assert not r.d.rc_armed() and r.d.rc_volume() == 0.0
    assert (r.d.rc_level(0), r.d.rc_level(1), r.d.rc_level_target(0)) == (0, 0, 0)
    assert "rebooted" in r.status()
    r.run(2.0)
    r.button()
    assert not r.d.rc_armed()                 # the link is not running
    r.link(True)
    r.button()
    assert r.d.rc_armed()                     # reconnected: starts again, from level 0


def test_in_the_menus_no_knob_touches_the_output(lib):
    """Run screen: 1 master, 2 level A, 3 level B, 4 MA. Menus: the knobs are the menu's; master, MA and levels hold."""
    r = Remote(lib)
    r.knob(K2, 30)
    r.knob(K3, 20)
    ma, master = r.d.rc_ma(), r.d.rc_master()
    r.push1()                                  # patterns: 1 scrolls, 2 jumps ten, 3-4 do nothing
    assert r.d.rc_screen() == PATTERNS
    r.knob(K1, 1)
    assert r.d.rc_cursor() == 1
    r.knob(K2, 1)
    assert r.d.rc_cursor() == 1                # only two patterns: the jump stops at the end
    r.knob(K3, -30)
    r.knob(K4, 50)
    r.push4()                                  # back
    r.push4()                                  # options: 1 moves (press toggles), 4 cycles the pulse shape
    assert r.d.rc_screen() == OPTIONS
    r.knob(K1, OPT_SWAP)
    r.push1()
    assert r.d.rc_cursor() == OPT_SWAP and r.d.rc_option(OPT_SWAP).decode() == "Swap channels: on"
    r.knob(K4, 1)                              # from anywhere: the shape changes and the highlight moves to it
    assert r.d.rc_cursor() == OPT_SHAPE and r.d.rc_option(OPT_SHAPE).decode() == "Shape: soft square"
    assert (r.d.rc_ma(), r.d.rc_master()) == (ma, master)
    assert (r.d.rc_level_target(0), r.d.rc_level_target(1)) == (30, 20)
    r.push4()
    assert r.d.rc_screen() == RUN
    r.knob(K1, -10)
    r.knob(K4, 4)
    r.knob(K2, -5)
    assert (r.d.rc_master(), r.d.rc_ma(), r.d.rc_level_target(0)) == (master - 10, ma + 4, 25)


def test_options_polarity_wires_and_text(lib):
    r = Remote(lib)
    r.push4()
    r.knob(K1, OPT_POL1)
    r.push1()                                  # press toggles: polarity 1 -> reversed
    r.run(0.05)
    assert r.d.rc_foc_route(0) == 21
    assert r.d.rc_option(OPT_POL1).decode() == "Polarity 1: reversed"
    r.push1()                                  # again: normal
    assert r.d.rc_foc_route(0) == 12
    r.knob(K1, OPT_WIRES2 - OPT_POL1)
    r.push1()                                  # press: wires 2: 34 -> 23
    assert r.d.rc_wire(1) == 23 and r.d.rc_option(OPT_WIRES2).decode() == "Wires 2: 2-3"


def test_knobs_2_and_3_step_their_wires_through_every_pair_both_ways(lib):
    r = Remote(lib)
    r.push4()
    seen = []
    for _ in range(12):
        r.knob(K2, 1)
        r.run(0.02)
        seen.append(r.d.rc_foc_route(0))
    assert seen == [21, 34, 43, 23, 32, 41, 14, 13, 31, 24, 42, 12]
    assert r.d.rc_cursor() == OPT_WIRES1       # the picture highlights what is changing
    r.knob(K3, -1)                             # position 2, backwards from 34: 21
    assert r.d.rc_foc_route(1) == 21 and r.d.rc_cursor() == OPT_WIRES2
    assert r.d.rc_option(OPT_POL2).decode() == "Polarity 2: reversed"


def test_an_unconnected_pad_silences_its_channel(lib):
    r = _armed_remote(lib, level=60)
    r.run(5.0)
    assert r.max_amps(3.0)[0] > 0.01
    r.push4()
    r.knob(K1, OPT_PAD1)
    r.push1()                                  # pad 1 off: channel A (12) goes silent at once
    assert r.max_amps(3.0)[0] == 0.0
    r.push1()                                  # pad 1 back: A ramps back in
    assert r.max_amps(3.0)[0] > 0.01


def test_switching_boxes_stops_the_output(lib):
    r = _armed_remote(lib)
    r.push4()
    r.knob(K1, OPT_BOX)
    r.push1()
    assert r.d.rc_box() == 1 and not r.d.rc_armed()


def test_navigation_times_out_to_the_run_screen(lib):
    r = Remote(lib)
    r.push1()
    assert r.d.rc_screen() == PATTERNS
    r.run(21.0)
    assert r.d.rc_screen() == RUN


def test_damaged_saved_settings_fall_back_to_safe_defaults(lib):
    r = Remote(lib, saved=(99, 55, 11, 7))     # pattern out of range, invalid routes, box out of range
    assert r.d.rc_pattern() == -1 and (r.d.rc_wire(0), r.d.rc_wire(1)) == (12, 34) and r.d.rc_box() == 0
    ok = Remote(lib, saved=(1, 23, 41, 1))
    assert ok.d.rc_pattern() == 1 and (ok.d.rc_wire(0), ok.d.rc_wire(1)) == (23, 41) and ok.d.rc_box() == 1
    assert (ok.d.rc_level(0), ok.d.rc_level(1)) == (0, 0)   # never the levels


def test_shapes_follow_the_box_firmware(lib):
    """Knob 4 in the options: triangle, rounded, taper 10..80 %, soft square; the v7 ones only with a v7 box, and an
    older box is sent rounded for a v7 shape it would misplay. The charge factors match device/fork.py."""
    from stimengine.device import fork as F
    lib.rc_set_fork.argtypes = [ctypes.c_int, ctypes.c_double]
    lib.rc_charge_factor.argtypes = [ctypes.c_int, ctypes.c_double]
    lib.rc_charge_factor.restype = ctypes.c_double
    for sid in list(F.SHAPES):
        for w in (40.0, 130.0, 255.0):
            assert lib.rc_charge_factor(sid, w) == pytest.approx(F.shape_charge_factor(sid, w), rel=1e-12)
    r = Remote(lib)
    r.push4()
    r.d.rc_set_fork(6, r.t)
    seen = []
    for _ in range(4):
        r.knob(K4, 1)
        seen.append(r.d.rc_shape())
    assert seen == [2, 0, 2, 0]                      # v6: rounded <-> soft square, round the list
    r.d.rc_set_fork(7, r.t)
    seen = []
    for _ in range(11):
        r.knob(K4, 1)
        seen.append(r.d.rc_shape())
    assert seen == [41, 42, 43, 44, 45, 46, 47, 48, 2, 3, 0]   # ... soft square, then round to triangle, rounded
    r.knob(K4, -1)
    assert r.d.rc_shape() == 3 and r.d.rc_option(OPT_SHAPE).decode() == "Shape: triangle"
    r.knob(K4, -1)                                   # backwards round the end too
    assert r.d.rc_option(OPT_SHAPE).decode() == "Shape: soft square"
    r.knob(K4, 1)
    r.knob(K4, 3)                                    # a quick turn: one step per detent
    assert r.d.rc_option(OPT_SHAPE).decode() == "Shape: taper 20%"
    r.run(0.2)
    assert r.d.rc_shape_sent(0) == 42                # a v7 box gets the taper
    r.d.rc_set_fork(6, r.t)                          # the box changes under it (another box, older firmware)
    r.run(0.2)
    assert r.d.rc_shape_sent(0) == 0                 # ... it is sent rounded
    assert "plays rounded" in r.d.rc_option(OPT_SHAPE).decode()


def test_the_m5s_options_are_as_before_the_remote_check_hidden(lib):
    """OPT_DEVICE (the RADR build's "Remote check") is hidden by default: on the M5 the highlight steps from Box to
    Back as it always did, the option does nothing and the messages still name the M5's knobs."""
    r = Remote(lib)
    assert r.status() == "press knob 1 to pick a pattern"
    r.push4()
    assert r.d.rc_screen() == OPTIONS and r.d.rc_cursor() == 0
    seen = [r.d.rc_cursor()]
    for _ in range(20):
        r.knob(K1, 1)
        seen.append(r.d.rc_cursor())
    assert sorted(set(seen)) == [o for o in range(OPT_BACK + 1) if o != OPT_DEVICE] and seen[-1] == OPT_BACK
    r.knob(K1, -1)
    assert r.d.rc_cursor() == OPT_BOX                    # back over the hidden one
    r.knob(K1, -30)
    assert r.d.rc_cursor() == 0                          # and it stops at the first, as before
    assert not lib.rc_option_shown(OPT_DEVICE) and lib.rc_device_seq() == 0
    r.link()
    r.button()
    assert r.status() == "pick a pattern first (knob 1)"
