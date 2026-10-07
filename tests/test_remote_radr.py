"""The remote on the RADR hardware (foc312-m5remote core/radr.c): its controls on the remote's controller and its
first-start check, on the host. The controller is the M5's (ctrl.c, tests/test_remote_ctrl.py): the same rate limits,
restart block and safety stack; these tests are about what each knob and button does here."""
import ctypes
import os
import subprocess
import sys

import pytest

from stimengine.paths import BUILD_DIR, REMOTE_DIR
from tests.test_remote_core import CORE, _zig_available
from tests.test_remote_ctrl import PACK

pytestmark = pytest.mark.skipif(not _zig_available(), reason="needs the ziglang package (pip install ziglang)")

SRC = [CORE / n for n in ("pyrand.c", "et312.c", "safety.c", "foc312.c", "pack.c", "ctrl.c", "radr.c")] + \
      [REMOTE_DIR / "test" / n for n in ("ffi_ctrl.c", "ffi_radr.c")]
LIB = BUILD_DIR / "remote" / ("remote_radr.dll" if os.name == "nt" else "remote_radr.so")

RUN, PATTERNS, OPTIONS = 0, 1, 2
KL, KR = 0, 1
L_SHOULDER, R_SHOULDER, UNDER_L, CENTRE, UNDER_R = range(5)
FOCUS_A, FOCUS_B, FOCUS_MA = range(3)
(OPT_WIRES1, OPT_POL1, OPT_WIRES2, OPT_POL2, OPT_PAD1, OPT_PAD2, OPT_PAD3, OPT_PAD4, OPT_SWAP, OPT_SHAPE,
 OPT_SKIP, OPT_BOX, OPT_DEVICE, OPT_BACK) = range(14)
CHECK_KNOBS, CHECK_BUTTONS, CHECK_SCREEN, CHECK_DONE = range(4)


@pytest.fixture(scope="module")
def lib():
    headers = [CORE / n for n in ("et312.h", "pyrand.h", "safety.h", "foc312.h", "pack.h", "ctrl.h", "radr.h")]
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
    d.rc_tick.argtypes = [ctypes.c_double]
    d.rc_link.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_double]
    d.rc_hide_options.argtypes = [ctypes.c_uint]
    d.rc_hide_default_mask.restype = ctypes.c_uint
    d.rc_device_seq.restype = ctypes.c_uint
    d.rc_volume.restype = ctypes.c_double
    d.rc_control_names.argtypes = [ctypes.c_char_p] * 4
    for f in ("rc_status", "rc_option", "rk_button_name"):
        getattr(d, f).restype = ctypes.c_char_p
    d.rr_knob.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_double]
    d.rr_button.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_double]
    d.rr_focus_seq.restype = ctypes.c_uint
    # the RADR build's names in the controller's messages (the board sets them once at start)
    d.rc_control_names(b"the right button", b"the left knob", b"the right knob: A, B", b"the centre button")
    return d


class Radr:
    """The RADR build as its board sets it up: every option shown, the remote check among them."""

    def __init__(self, d, routes=None):
        self.d, self.t = d, 0.0
        r0, r1 = routes or (12, 34)
        assert d.rc_init(PACK, len(PACK), 2, 0.0, 1 if routes else 0, -1, r0, r1, 0) == 0
        d.rc_hide_options(d.rc_hide_default_mask() & ~(1 << OPT_DEVICE))
        d.rr_init()

    def run(self, seconds, dt=1 / 60):
        for _ in range(int(round(seconds / dt))):
            self.t += dt
            self.d.rc_tick(self.t)

    def knob(self, k, n):
        self.d.rr_knob(k, n, self.t)

    def press(self, b):
        self.d.rr_button(b, 1, self.t)
        self.d.rr_button(b, 0, self.t)

    def down(self, b):
        self.d.rr_button(b, 1, self.t)

    def up(self, b):
        self.d.rr_button(b, 0, self.t)

    def status(self):
        return self.d.rc_status().decode()

    def pick(self, index):
        self.press(UNDER_R)                                 # the list
        assert self.d.rc_screen() == PATTERNS
        self.knob(KL, index - self.d.rc_cursor())
        self.press(UNDER_R)                                 # selects (the right button)

    def armed(self, level=40):
        self.d.rc_link(1, b"", self.t)
        self.pick(0)
        self.knob(KR, level)                                # A (the focus at start)
        self.press(R_SHOULDER)
        self.knob(KR, level)                                # B
        self.press(L_SHOULDER)
        self.run(level / 10 + 0.5)
        self.press(CENTRE)
        assert self.d.rc_armed()
        return self


def test_the_left_knob_is_master_and_the_right_knob_follows_the_focus(lib):
    r = Radr(lib)
    assert r.d.rr_focus() == FOCUS_A and r.d.rc_screen() == RUN
    r.knob(KL, -30)
    assert r.d.rc_master() == 70                            # down: at once
    r.knob(KR, 20)
    assert (r.d.rc_level_target(0), r.d.rc_level_target(1), r.d.rc_ma()) == (20, 0, 50)
    r.press(R_SHOULDER)
    assert r.d.rr_focus() == FOCUS_B
    r.knob(KR, 30)
    assert (r.d.rc_level_target(0), r.d.rc_level_target(1), r.d.rc_ma()) == (20, 30, 50)
    r.press(R_SHOULDER)
    assert r.d.rr_focus() == FOCUS_MA
    r.knob(KR, -10)
    assert (r.d.rc_level_target(0), r.d.rc_level_target(1), r.d.rc_ma()) == (20, 30, 40)
    for focus in (FOCUS_A, FOCUS_B, FOCUS_MA):              # the left knob is the master whatever the focus
        for _ in range(3):
            if r.d.rr_focus() != focus:
                r.press(R_SHOULDER)
        assert r.d.rr_focus() == focus
        r.knob(KL, -5)
    assert r.d.rc_master() == 55


def test_the_shoulders_move_the_focus_wrapping_round_and_each_change_ticks_once(lib):
    r = Radr(lib)
    seq = r.d.rr_focus_seq()
    r.press(L_SHOULDER)                                     # at A: wraps to MA
    assert r.d.rr_focus() == FOCUS_MA and r.d.rr_focus_seq() == seq + 1
    r.press(R_SHOULDER)                                     # MA: wraps to A
    assert r.d.rr_focus() == FOCUS_A and r.d.rr_focus_seq() == seq + 2
    r.press(R_SHOULDER)
    r.press(R_SHOULDER)
    assert r.d.rr_focus() == FOCUS_MA and r.d.rr_focus_seq() == seq + 4
    r.down(R_SHOULDER)                                      # it acts on the press (MA -> A) ...
    assert r.d.rr_focus() == FOCUS_A and r.d.rr_focus_seq() == seq + 5
    r.up(R_SHOULDER)                                        # ... and the release does nothing
    assert r.d.rr_focus() == FOCUS_A and r.d.rr_focus_seq() == seq + 5
    r.press(L_SHOULDER)
    assert r.d.rr_focus() == FOCUS_MA and r.d.rr_focus_seq() == seq + 6


def test_the_rate_limits_are_the_controllers(lib):
    r = Radr(lib)
    r.knob(KR, 60)                                          # a fast spin of the right knob (A)
    assert r.d.rc_level_target(0) == 60 and r.d.rc_level(0) == 0
    r.run(1.0)
    assert 9 <= r.d.rc_level(0) <= 11
    r.knob(KR, -55)
    assert r.d.rc_level(0) <= 5                             # down: at once
    r.knob(KL, -90)
    r.knob(KL, 90)
    r.run(1.0)
    assert 19 <= r.d.rc_master() <= 21


def test_centre_starts_and_stops_on_the_press_on_every_screen(lib):
    for screen in (RUN, PATTERNS, OPTIONS):
        r = Radr(lib).armed()
        r.run(1.0)
        assert r.d.rc_volume() > 0
        if screen == PATTERNS:
            r.press(UNDER_R)
        elif screen == OPTIONS:
            r.press(UNDER_L)
        assert r.d.rc_screen() == screen and r.d.rc_armed()
        r.down(CENTRE)                                      # STOP on the press, before any release
        assert not r.d.rc_armed() and r.d.rc_volume() == 0.0
        r.up(CENTRE)
        r.run(0.3)
        r.press(CENTRE)                                     # a quick second press never turns it back on
        assert not r.d.rc_armed()


def test_start_needs_a_link_and_a_pattern_and_says_which_button(lib):
    r = Radr(lib)
    assert r.status() == "press the right button to pick a pattern"
    r.press(CENTRE)
    assert not r.d.rc_armed() and "no box" in r.status()
    r.d.rc_link(1, b"", r.t)
    r.press(CENTRE)
    assert not r.d.rc_armed() and r.status() == "pick a pattern first (the right button)"
    r.pick(1)
    assert r.d.rc_pattern() == 1 and r.d.rc_screen() == RUN
    r.press(CENTRE)
    assert r.d.rc_armed()


def test_the_list_scrolls_with_the_left_knob_jumps_with_the_right_picks_and_goes_back(lib):
    r = Radr(lib)
    r.press(UNDER_R)
    assert r.d.rc_screen() == PATTERNS and r.d.rr_right_knob() == 1      # KNOB_2: ten at a time
    r.knob(KL, 1)
    assert r.d.rc_cursor() == 1
    r.knob(KR, -1)                                          # (two patterns: ten back is the first)
    assert r.d.rc_cursor() == 0
    r.knob(KR, 1)
    assert r.d.rc_cursor() == 1                             # ... and ten on stops at the last
    r.press(UNDER_L)                                        # back (the left button): nothing picked
    assert r.d.rc_screen() == RUN and r.d.rc_pattern() == -1
    r.press(UNDER_R)
    r.knob(KL, 1)
    r.press(UNDER_R)                                        # selects (the right button)
    assert r.d.rc_screen() == RUN and r.d.rc_pattern() == 1


def test_options_open_with_under_left_select_with_under_right_and_back_with_under_left(lib):
    r = Radr(lib)
    r.press(UNDER_L)
    assert r.d.rc_screen() == OPTIONS and r.d.rc_cursor() == 0
    r.knob(KL, OPT_SWAP)
    assert r.d.rc_cursor() == OPT_SWAP
    r.press(UNDER_R)                                        # selects: the pairs trade places
    r.run(0.1)
    assert (r.d.rc_foc_route(0), r.d.rc_foc_route(1)) == (34, 12)
    assert r.d.rc_screen() == OPTIONS
    r.knob(KR, 1)                                           # the right knob (focus A): A's pair, as knob 2 on the M5
    assert r.d.rc_cursor() == OPT_WIRES1
    r.press(UNDER_L)                                        # back
    assert r.d.rc_screen() == RUN
    r.press(L_SHOULDER)                                     # on the run screen the shoulders move the focus: MA
    r.press(UNDER_L)                                        # Options again
    shape = r.d.rc_shape()
    r.knob(KR, 1)                                           # focus MA: the pulse shape, as knob 4
    assert r.d.rc_cursor() == OPT_SHAPE and r.d.rc_shape() != shape
    r.press(UNDER_L)                                        # back
    assert r.d.rc_screen() == RUN


def test_the_shoulders_select_and_go_back_in_the_list_and_select_or_step_the_pair_in_options(lib):
    r = Radr(lib, routes=(12, 41))
    r.press(UNDER_R)                                        # the list
    r.knob(KL, 1)
    r.press(R_SHOULDER)                                     # top-right: back, nothing picked
    assert r.d.rc_screen() == RUN and r.d.rc_pattern() == -1
    r.press(UNDER_R)
    r.knob(KL, 1)
    r.press(L_SHOULDER)                                     # top-left: selects
    assert r.d.rc_screen() == RUN and r.d.rc_pattern() == 1
    assert r.d.rr_focus() == FOCUS_A                        # neither moved the focus
    r.press(UNDER_L)                                        # Options
    r.press(R_SHOULDER)                                     # top-right: A (the focus) to its next pair
    r.run(0.2)
    assert r.d.rc_screen() == OPTIONS and (r.d.rc_foc_route(0), r.d.rc_foc_route(1)) == (34, 41)
    r.knob(KL, OPT_SWAP - r.d.rc_cursor())
    r.press(L_SHOULDER)                                     # top-left: selects the highlighted row (swap)
    r.run(0.2)
    assert r.d.rc_screen() == OPTIONS and (r.d.rc_foc_route(0), r.d.rc_foc_route(1)) == (41, 34)
    r.press(UNDER_L)
    r.press(R_SHOULDER)                                     # the run screen: focus B
    r.press(UNDER_L)
    r.press(R_SHOULDER)                                     # Options, focus B: B to its next pair (41 -> 13)
    r.run(0.2)
    assert r.d.rc_wire(0) == 34 and r.d.rc_wire(1) == 13


def test_every_option_and_the_check_are_offered(lib):
    r = Radr(lib)
    r.press(UNDER_L)
    seen = [r.d.rc_cursor()]
    for _ in range(20):
        r.knob(KL, 1)
        seen.append(r.d.rc_cursor())
    assert sorted(set(seen)) == list(range(OPT_BACK + 1)) and seen[-1] == OPT_BACK
    r.knob(KL, -1)
    assert r.d.rc_cursor() == OPT_DEVICE and r.d.rc_option(OPT_DEVICE).decode().startswith("Remote check")
    seq = r.d.rc_device_seq()
    r.press(UNDER_R)                                        # the check: asked of the board
    assert r.d.rc_device_seq() == seq + 1 and r.d.rc_screen() == RUN


def test_the_check_is_refused_while_output_flows(lib):
    r = Radr(lib).armed()
    r.press(UNDER_L)
    r.knob(KL, 30)
    r.knob(KL, -1)
    assert r.d.rc_cursor() == OPT_DEVICE
    seq = r.d.rc_device_seq()
    r.press(UNDER_R)
    assert r.d.rc_device_seq() == seq and r.d.rc_armed() and "stop the output" in r.status()
    r.press(CENTRE)                                         # STOP (still in Options, the check highlighted)
    assert not r.d.rc_armed() and r.d.rc_screen() == OPTIONS and r.d.rc_cursor() == OPT_DEVICE
    r.press(UNDER_R)
    assert r.d.rc_device_seq() == seq + 1


# ---- the first-start check --------------------------------------------------------------------------------------
def _turn(d, knob, raw, times):
    for _ in range(times):
        d.rk_knob(knob, raw)


def test_the_check_settles_each_knobs_direction_from_the_first_clockwise_turn(lib):
    d = lib
    d.rk_begin(1, 1, 0, 0)
    assert d.rk_step() == CHECK_KNOBS
    _turn(d, KL, -1, 2)                                     # turned clockwise, reads negative: this unit's left knob
    assert d.rk_settled(KL) and d.rk_dir(KL) == -1 and d.rk_cw(KL) == 2
    _turn(d, KL, -1, 1)
    _turn(d, KL, +1, 3)                                     # counter-clockwise
    assert d.rk_knob_ok(KL) and (d.rk_cw(KL), d.rk_ccw(KL)) == (3, 3)
    assert d.rk_step() == CHECK_KNOBS                       # the right knob still to do
    _turn(d, KR, 1, 1)
    _turn(d, KR, -1, 1)                                     # back before it settled: counted again
    assert not d.rk_settled(KR)
    _turn(d, KR, 1, 2)
    assert d.rk_settled(KR) and d.rk_dir(KR) == 1
    _turn(d, KR, 1, 1)
    _turn(d, KR, -1, 2)
    assert d.rk_step() == CHECK_KNOBS
    _turn(d, KR, -1, 1)
    assert d.rk_knob_ok(KR) and d.rk_step() == CHECK_BUTTONS


def test_under_left_starts_the_knobs_over(lib):
    d = lib
    d.rk_begin(1, 1, 0, 0)
    _turn(d, KL, 1, 2)                                      # turned the wrong way first
    assert d.rk_settled(KL) and d.rk_dir(KL) == 1
    d.rk_button(UNDER_L, 1)
    d.rk_button(UNDER_L, 0)
    assert not d.rk_settled(KL) and d.rk_cw(KL) == 0 and d.rk_step() == CHECK_KNOBS
    _turn(d, KL, -1, 2)
    assert d.rk_dir(KL) == -1


def _knobs_done(d):
    _turn(d, KL, 1, 3)
    _turn(d, KL, -1, 3)
    _turn(d, KR, 1, 3)
    _turn(d, KR, -1, 3)
    assert d.rk_step() == CHECK_BUTTONS


def test_the_check_needs_every_button_then_confirms_the_screen(lib):
    d = lib
    d.rk_begin(1, 1, 0, 0)
    for b in range(5):                                      # buttons before the knobs are done count for nothing
        d.rk_button(b, 1)
        d.rk_button(b, 0)
    assert not any(d.rk_pressed(b) for b in range(5))
    _knobs_done(d)
    for b in (CENTRE, L_SHOULDER, UNDER_R, CENTRE, R_SHOULDER):
        d.rk_button(b, 1)
        d.rk_button(b, 0)
    assert d.rk_step() == CHECK_BUTTONS and not d.rk_pressed(UNDER_L)
    d.rk_button(UNDER_L, 1)
    assert d.rk_step() == CHECK_SCREEN and not d.rk_flip()   # the last button's press is not the screen's
    d.rk_button(UNDER_L, 0)
    d.rk_button(UNDER_L, 1)                                 # turn the screen 180 degrees, invert the colours
    d.rk_button(UNDER_R, 1)
    assert d.rk_flip() and d.rk_invert() and not d.rk_done()
    d.rk_button(UNDER_R, 1)
    assert not d.rk_invert()
    d.rk_button(CENTRE, 1)                                  # confirmed
    assert d.rk_done() and d.rk_flip()
    assert [d.rk_button_name(b).decode() for b in range(5)] == \
        ["left shoulder", "right shoulder", "under-left", "centre", "under-right"]


# ---- the builds' flash layouts ----------------------------------------------------------------------------------
def _ini(board: str) -> str:
    return (REMOTE_DIR / board / "platformio.ini").read_text(encoding="utf-8")


def test_the_m5_build_keeps_its_flash_layout_and_the_radr_has_its_own():
    if not (REMOTE_DIR / "radr" / "partitions.csv").exists():
        pytest.skip("needs the foc312-m5remote project next to this one")
    assert "board_build.partitions = default_16MB.csv" in _ini("m5")      # the M5: as every release so far
    rows = [ln.split(",") for ln in (REMOTE_DIR / "radr" / "partitions.csv").read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]
    table = {r[0].strip(): (r[1].strip(), r[2].strip(), int(r[3], 16), int(r[4], 16)) for r in rows}
    assert table["nvs"] == ("data", "nvs", 0x9000, 0x5000)               # the remote check's result
    assert table["factory"] == ("app", "factory", 0x10000, 0x300000)     # the app an update writes alone
    assert "otadata" not in table and not any(t[1].startswith("ota") for t in table.values())
    fs = table["spiffs"]
    assert fs[0] == "data" and fs[2] == 0x10000 + 0x300000 and fs[2] + fs[3] <= 0x1000000 - 0x10000
    assert "board_build.partitions = partitions.csv" in _ini("radr") and "-D BOARD_RADR=1" in _ini("radr")
