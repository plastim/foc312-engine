"""ET-312 VM: bytecode, timers, modulator state machine, gate, block timer, determinism."""
import random

import pytest

from stimengine.et312.vm import DEFAULTS, ET312VM, INVERT_C, TICK_HZ, TICK_S


def vm_with(blocks=None, seed=0):
    return ET312VM(blocks or {}, seed=seed)


def ticks(vm, n):
    for _ in range(n):
        vm.tick()


# ------------------------------------------------------------------ constants / defaults
def test_tick_base_is_244_hz():
    assert TICK_HZ == pytest.approx(244.140625)
    assert TICK_S == pytest.approx(0.004096)


def test_defaults_image_matches_protocol_docs():
    vm = vm_with()
    # $4086/$4087 MA range, $40a5.. intensity block, $40ae frequency (8-255, 8 fastest), $40b7 width
    assert vm.mem[0x85] == 3
    assert (vm.mem[0x86], vm.mem[0x87]) == (0x0F, 0xFF)
    assert vm.modulator(0, 0xA5)["min"] == 205 and vm.modulator(0, 0xA5)["max"] == 255
    assert vm.modulator(0, 0xAE)["value"] == 22 and vm.modulator(0, 0xAE)["select"] == 0x08
    assert vm.modulator(0, 0xB7)["value"] == 130 and vm.modulator(0, 0xB7)["select"] == 0x04
    assert vm.modulator(1, 0xB7) == vm.modulator(0, 0xB7)      # channel B gets the same image
    assert vm.mem[0x1FE] == 0x82 and vm.mem[0x1FA] == 0xD7      # Adv Width / Depth defaults


# ------------------------------------------------------------------ bytecode
def test_set_op_channel_redirect_and_explicit_b_page():
    vm = vm_with({0x80: [("set", 0, 0xA5, 0x99)], 0x81: [("set", 1, 0xA5, 0x77)], 0x82: [("set", 0, 0x86, 0x12)]})
    vm.load_block(0x80)                       # mask 3: A pass then B pass -> both channels
    assert vm.mem[0xA5] == 0x99 and vm.mem[0x1A5] == 0x99
    vm.mem[0x85] = 1
    vm.load_block(0x81)                       # bit6 forces the B page regardless of the pass
    assert vm.mem[0x1A5] == 0x77 and vm.mem[0xA5] == 0x99
    vm.mem[0x85] = 2                          # B-only pass: global registers (< $8c) are not redirected
    vm.load_block(0x82)
    assert vm.mem[0x86] == 0x12 and vm.mem[0x186] == 0


def test_mask_zero_executes_nothing_and_mask_change_mid_block():
    vm = vm_with({0x80: [("set", 0, 0x85, 0x02), ("set", 0, 0xA5, 0x11)]})
    vm.mem[0x85] = 0
    vm.load_block(0x80)
    assert vm.mem[0xA5] == 0xFF and vm.mem[0x1A5] == 0xFF
    vm.mem[0x85] = 3
    vm.load_block(0x80)                       # first op narrows the mask to B for the ops after it
    assert vm.mem[0x1A5] == 0x11 and vm.mem[0xA5] == 0xFF


def test_acc_arith_random_and_ifacc():
    vm = vm_with({
        0x80: [("ldacc", 2, 0x0D), ("stacc", 0, 0x95), ("add", 0, 0xA5, 0x02), ("and", 0, 0xA5, 0x0F),
               ("or", 0, 0xA5, 0x40), ("xor", 0, 0xA5, 0x01), ("shr", 0, 0xA5)],
        0x81: [("set", 0, 0x8D, 10), ("set", 0, 0x8E, 12), ("rand", 0, 0xA6)],
        0x82: [("set", 0, 0x84, 0x83), ("ldacc", 0, 0xA7), ("ifacc", 0, 0xA7)],
        0x83: [("set", 0, 0xB7, 0x33)],
    })
    vm.mem[0x85] = 1
    vm.mem[0x20D] = 42
    vm.load_block(0x80)
    assert vm.mem[0x95] == 42                                   # toggle-style "period = MA"
    expect = ((((0xFF + 2) & 0xFF) & 0x0F) | 0x40) ^ 0x01
    assert vm.mem[0xA5] == expect >> 1
    vm.load_block(0x81)
    assert 10 <= vm.mem[0xA6] <= 12
    vm.load_block(0x82)
    assert vm.pending_next
    vm.tick()                                                   # the conditional branch fires next tick
    assert vm.mem[0xB7] == 0x33 and not vm.pending_next


def test_inversion_table_matches_stroke_depth_default():
    # Stroke: intensity min = (0xa4 - Depth) & 0xff -> factory Depth 0xd7 gives the 205 documented minimum
    assert (INVERT_C[0xA5] - 0xD7) & 0xFF == 205
    assert (INVERT_C[0xAE] - 10) & 0xFF == 255                  # frequency inversion flips the range


# ------------------------------------------------------------------ timers
def test_timer_selects_and_counters():
    vm = vm_with()
    ticks(vm, 7)
    assert vm.mem[0x88] == 7 and vm.mem[0x8B] == 1             # slow counter ticks every 8th tick
    ticks(vm, 249)
    assert vm.mem[0x88] == 0 and vm.mem[0x89] == 1 and vm.mem[0x8B] == 32
    assert vm._timer(1) == (True, 0)
    assert vm._timer(3) == (True, 1)                            # only on the wrap tick
    vm.tick()
    assert vm._timer(3)[0] is False and vm._timer(2)[0] is False
    assert vm._timer(0)[0] is False


# ------------------------------------------------------------------ modulators
def set_mod(vm, z, **kw):
    keys = ("value", "min", "max", "rate", "step", "at_min", "at_max", "select", "last")
    for k, v in kw.items():
        vm.mem[z + keys.index(k)] = v


def test_ramp_reverses_at_ends_and_rate_units():
    vm = vm_with()
    set_mod(vm, 0xA5, value=250, min=240, max=255, rate=3, step=2, at_min=0xFF, at_max=0xFF, select=1, last=0)
    seen = []
    for _ in range(60):
        vm.tick()
        seen.append(vm.mem[0xA5])
    # firmware stores last := timer + 1 (0x1d8), so rate 3 steps every 4 ticks: first at timer 3 (last was 0),
    # then 7, 11, 15: 252, 254, 255 (clamp + reverse), 253 ... 240 (clamp + reverse) ...
    assert seen[2] == 252 and seen[6] == 254 and seen[10] == 255 and seen[14] == 253
    assert seen[3] == seen[5] == 252
    assert min(seen) == 240 and max(seen) == 255
    assert vm.mem[0xA9] in (2, 0xFE)


def test_slow_timer_rate_is_32ms_units():
    vm = vm_with()
    set_mod(vm, 0xB7, value=100, min=50, max=200, rate=2, step=1, at_min=0xFC, at_max=0xFC, select=2, last=0)
    changes = []
    for i in range(1, 201):
        before = vm.mem[0xB7]
        vm.tick()
        if vm.mem[0xB7] != before:
            changes.append(i)
    # timer select 2: checked only on ticks where rt_lo&7==7 ($8b just incremented); rate 2 -> a step every
    # 2 + 1 slow counts = 24 ticks (last := $8b + 1): $8b = 2 at tick 15, then 5 at 39, 8 at 63
    assert changes[:3] == [15, 39, 63]


def test_wrap_and_stop_actions():
    vm = vm_with()
    set_mod(vm, 0xAE, value=98, min=9, max=100, rate=0, step=1, at_min=0xFC, at_max=0xFD, select=1, last=0)
    vals = []
    for _ in range(5):
        vm.tick()
        vals.append(vm.mem[0xAE])
    assert vals == [99, 9, 10, 11, 12]                         # 0xfd at max -> wrap to min
    set_mod(vm, 0xAE, value=10, step=0xFF, at_min=0xFC)
    for _ in range(5):
        vm.tick()
    assert vm.mem[0xAE] == 9                                    # 0xfc at min -> stop there


def test_toggle_action_flips_pulse_shape_bits():
    vm = vm_with()
    vm.mem[0x90] = 0x05                                         # Stroke's gate value: monophasic
    set_mod(vm, 0xA5, value=254, min=205, max=255, rate=0, step=1, at_min=0xFE, at_max=0xFE, select=1, last=0)
    vm.tick()
    assert vm.mem[0x90] == 0x03 and vm.mem[0xA9] == 0xFF        # bits 1-2 flipped, direction reversed


def test_at_min_jump_loads_block_and_channel_b_dedupes():
    vm = vm_with({5: [("set", 0, 0xB7, 0x55)], 6: [("set", 0, 0xB7, 0x66)]})
    for base in (0, 0x100):
        set_mod(vm, base + 0xAE, value=10, min=9, max=255, rate=0, step=0xFF, at_min=6, at_max=0xFF, select=1, last=0)
    ticks(vm, 2)                                                # 10 -> 9 (clamp is inclusive) -> below min
    assert [b for _, b in vm.trace.loads] == [6]                # A jumped, B skipped the duplicate
    vm = vm_with({5: [("set", 0, 0xB7, 0x55)], 6: [("set", 0, 0xB7, 0x66)]})
    set_mod(vm, 0xAE, value=10, min=9, max=255, rate=0, step=0xFF, at_min=6, at_max=0xFF, select=1, last=0)
    set_mod(vm, 0x1AE, value=10, min=9, max=255, rate=0, step=0xFF, at_min=5, at_max=0xFF, select=1, last=0)
    ticks(vm, 2)
    assert [b for _, b in vm.trace.loads] == [6, 5]


def test_static_follow_sources_and_other_channel():
    vm = vm_with()
    vm.mem[0x20D] = 77
    set_mod(vm, 0xAE, select=0x08)                              # frequency follows MA
    set_mod(vm, 0xB7, select=0x04)                              # width follows Adv Width
    set_mod(vm, 0x1AE, select=0x0C)                             # B frequency follows A's value
    vm.mem[0x1FE] = 0x90
    vm.tick()
    assert vm.mem[0xAE] == 77 and vm.mem[0xB7] == 0x90 and vm.mem[0x1AE] == 77
    set_mod(vm, 0xAE, select=0x18)                              # MA, inverted
    vm.tick()
    assert vm.mem[0xAE] == (INVERT_C[0xAE] - vm.mem[0x20D]) & 0xFF


def test_timed_min_follows_source_and_rate_from_ma():
    vm = vm_with()
    vm.mem[0x86], vm.mem[0x87] = 0x00, 0x20                     # Stroke's MA range, knob at CW -> MA 0
    vm.ma_knob = 1.0
    vm.update_ma()
    assert vm.mem[0x20D] == 0
    set_mod(vm, 0xA5, value=255, min=128, max=255, rate=9, step=2, at_min=0xFE, at_max=0xFE, select=0x55, last=0)
    vm.mem[0x1FA] = 0xD7
    vm.tick()
    assert vm.mem[0xA6] == 205                                  # min refreshed from inverted Depth
    assert vm.mem[0xA5] == 255 and vm.mem[0xA9] == 0xFE         # clamp + reverse on the first step (rate = MA = 0)
    vm.tick()
    assert vm.mem[0xA5] == 253


# ------------------------------------------------------------------ gate
def test_gate_on_off_timing():
    vm = vm_with()
    vm.mem[0x90] = 0x07
    vm.mem[0x98], vm.mem[0x99], vm.mem[0x9A] = 4, 2, 1
    states = []
    for _ in range(14):
        vm.tick()
        states.append(vm.mem[0x90] & 1)
    # firmware: ON waits the on time $98, OFF waits the off time $99, each + 1 (last := timer + 1):
    # on until timer 4, off 4..6 (off time 2 -> 3 ticks), on 7..11 (on time 4 -> 5 ticks), off 12..14
    assert states == [1, 1, 1, 0, 0, 0, 1, 1, 1, 1, 1, 0, 0, 0]


def test_gate_off_time_zero_stays_on_and_ma_sources():
    vm = vm_with()
    vm.mem[0x90], vm.mem[0x98], vm.mem[0x99], vm.mem[0x9A] = 0x07, 5, 0, 1
    states = [(vm.tick(), vm.mem[0x90] & 1)[1] for _ in range(24)]
    # off time 0: the OFF branch turns the gate straight back on without touching last, so the output drops
    # for exactly one tick (4 ms) per on-time period: off at timer 5, 11, 17, 23 (on time 5, + 1)
    assert [i + 1 for i, st in enumerate(states) if not st] == [5, 11, 17, 23]
    vm.mem[0x9A] = 0x49                                         # Rhythm: both times from MA, timer 1
    vm.mem[0x86], vm.mem[0x87] = 1, 0x17
    vm.ma_knob = 1.0
    vm.update_ma()
    assert vm.mem[0x20D] == 1
    states = [(vm.tick(), vm.mem[0x90] & 1)[1] for _ in range(8)]
    assert states == [0, 0, 1, 1, 0, 0, 1, 1]                   # MA 1 -> each phase 1 + 1 ticks


# ------------------------------------------------------------------ block timer
def test_block_timer_loads_target_and_b_dedupes():
    vm = vm_with({0x10: [("set", 0, 0xB7, 0xB4)], 0x11: [("set", 0, 0xB7, 0x46)]})
    vm.mem[0x95], vm.mem[0x96], vm.mem[0x97] = 3, 1, 0x10
    vm.mem[0x195], vm.mem[0x196], vm.mem[0x197] = 3, 1, 0x10
    ticks(vm, 3)
    assert [b for _, b in vm.trace.loads] == [0x10]
    vm.mem[0x197] = 0x11
    ticks(vm, 3)
    assert [b for _, b in vm.trace.loads] == [0x10]              # period 3 + 1: last was stored as 4
    ticks(vm, 1)
    assert [b for _, b in vm.trace.loads] == [0x10, 0x10, 0x11]


# ------------------------------------------------------------------ MA + determinism
def test_ma_mapping_endpoints():
    vm = vm_with()
    vm.mem[0x86], vm.mem[0x87] = 0x0F, 0xFF                     # default range
    vm.ma_knob = 1.0; vm.update_ma(); assert vm.mem[0x20D] == 0x0F
    vm.ma_knob = 0.0; vm.update_ma(); assert vm.mem[0x20D] == 0xFF
    vm.ma_knob = 0.5; vm.update_ma(); assert 130 <= vm.mem[0x20D] <= 137
    vm.mem[0x86], vm.mem[0x87] = 0xCD, 0xD4                     # Phase 3: inverted small range
    vm.ma_knob = 0.0; vm.update_ma(); assert vm.mem[0x20D] == 0xD4
    vm.ma_knob = 1.0; vm.update_ma(); assert vm.mem[0x20D] == 0xCD


def test_random_is_seeded_and_in_range():
    a, b = vm_with(seed=7), vm_with(seed=7)
    for v in (a, b):
        v.mem[0x8D], v.mem[0x8E] = 5, 24
    xs = [a.random_between() for _ in range(50)]
    ys = [b.random_between() for _ in range(50)]
    assert xs == ys and all(5 <= x <= 24 for x in xs)
    a.mem[0x8D], a.mem[0x8E] = 30, 20
    with pytest.raises(ValueError):
        a.random_between()
