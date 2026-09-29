"""ET-312 modes against documented behaviour (manual v1.7, ErosLink guide, buttshock annotations)."""
import pytest

from stimengine.et312 import ET312Engine, IMPLEMENTED, MODE_BY_NAME, STUBBED, TICK_HZ
from stimengine.et312 import modes as M


def run(name, seconds, **kw):
    eng = ET312Engine(name, **kw)
    return eng, list(eng.run(seconds))


def transitions(frames, key):
    out = []
    prev = None
    for f in frames:
        v = key(f)
        if prev is not None and v != prev:
            out.append((f.t, v))
        prev = v
    return out


@pytest.mark.needs_et312_data
@pytest.mark.parametrize("name", IMPLEMENTED + STUBBED)
def test_every_mode_runs_and_stays_in_range(name):
    eng, fr = run(name, 30, seed=1)
    assert len(fr) == int(round(30 * TICK_HZ))
    for f in fr:
        for ch in (f.a, f.b):
            assert 0.0 <= ch.intensity <= 1.0
            assert 50 <= ch.pulse_width_us <= 255
            assert 15.0 <= ch.pulse_rate_hz <= 420.0
            assert ch.leading_polarity in (1, -1)
            assert ch.phase_asymmetry in ("et312_biphasic", "et312_monophasic")
    assert fr[-1].mode_name in M.MODE_NAMES.values()


@pytest.mark.needs_et312_data
def test_mode_switch_ramp_soft_starts_every_mode():
    eng, fr = run("waves", 4)
    assert fr[0].a.raw_ramp == 156 and fr[6].a.raw_ramp == 157 and fr[-1].a.raw_ramp == 255
    # 99 steps, one per 7 + 1 ticks (last := timer + 1) -> ~3.24 s to full
    full = next(f.t for f in fr if f.a.raw_ramp == 255)
    assert 3.15 < full < 3.3
    assert fr[0].a.intensity < fr[-1].a.intensity


@pytest.mark.needs_et312_data
def test_determinism():
    _, x = run("random2", 20, seed=11)
    _, y = run("random2", 20, seed=11)
    assert x == y
    _, z = run("random2", 20, seed=12)
    assert z != x


@pytest.mark.needs_et312_data
def test_waves_channels_differ_and_ma_sets_rate():
    eng, fr = run("waves", 40, ma=0.5)
    ra = {round(f.a.pulse_rate_hz) for f in fr}
    rb = {round(f.b.pulse_rate_hz) for f in fr}
    assert max(ra) > 350 and min(ra) < 32           # A frequency 9..128: 1e6 / (256*(F+1) + 2W) -> ~376..30 Hz
    assert min(rb) > 58                             # B frequency max 64 -> >= 60 Hz
    wa = [f.a.raw_width for f in fr]
    assert min(wa) == 50 and max(wa) == 200         # width sweeps its full range
    slow = len(transitions(run("waves", 20, ma=0.0)[1], lambda f: f.a.raw_width))
    fast = len(transitions(run("waves", 20, ma=1.0)[1], lambda f: f.a.raw_width))
    assert fast > 20 * slow                         # MA clockwise = faster ("Rate: M.A.")


@pytest.mark.needs_et312_data
def test_stroke_intensity_ramps_between_depth_min_and_max_and_flips_pulse_polarity():
    eng, fr = run("stroke", 6, ma=1.0)
    steady = fr[800:]
    ints = [f.a.raw_intensity for f in steady]
    assert min(ints) == 205 and max(ints) == 255    # min = (0xa4 - Depth) with factory Depth
    # gate 0x05 <-> 0x03: a MONOPHASIC pulse whose half (polarity) flips at every ramp reversal; never biphasic
    assert not any(f.a.biphasic for f in steady)
    assert all(f.a.phase_asymmetry == "et312_monophasic" and f.a.gate_on for f in steady)
    flips = transitions(steady, lambda f: f.a.leading_polarity)
    assert len(flips) > 10                          # push/pull: the driven half toggles at every reversal
    # each flip happens where the intensity ramp reverses (at its min or max)
    flip_idx = [i for i in range(1, len(steady)) if steady[i].a.leading_polarity != steady[i - 1].a.leading_polarity]
    for i in flip_idx[:6]:
        assert steady[i].a.raw_intensity in (204, 205, 206, 254, 255, 256)
    assert all(f.a.pulse_width_us == 255 for f in steady) and all(f.b.pulse_width_us == 216 for f in steady)
    # Depth deeper -> lower minimum
    eng2, fr2 = run("stroke", 6, ma=1.0, advanced=__import__("stimengine.et312", fromlist=["AdvancedParams"]).AdvancedParams(depth=0xF0))
    assert min(f.a.raw_intensity for f in fr2[800:]) == (0xA4 - 0xF0) & 0xFF


@pytest.mark.needs_et312_data
def test_climb_sweeps_frequency_in_three_blocks_at_different_rates():
    eng, fr = run("climb", 10, ma=1.0)
    loads = [b for _, b in eng.vm.trace.loads]
    assert loads[:3] == [1, 5, 8]
    assert loads[3:9] == [6, 7, 5, 6, 7, 5]         # A: block 5 -> 6 -> 7 -> 5 ...
    rates = [f.a.pulse_rate_hz for f in fr]
    assert min(rates) < 16 and max(rates) > 350     # F 9..255 at W 130: 1e6 / (256*(F+1) + 260) = 355..15 Hz
    # pulse rate rises within a block (frequency register descends) and drops back at the block change
    t6 = eng.vm.trace.loads[3][0]
    assert fr[t6 - 2].a.pulse_rate_hz > fr[t6 + 2].a.pulse_rate_hz
    # step doubles per block: block durations shrink 248 -> 125 -> 63 ticks
    d = [eng.vm.trace.loads[i + 1][0] - eng.vm.trace.loads[i][0] for i in range(2, 5)]
    assert d[0] > 1.8 * d[1] > 1.8 * d[2]
    # MA counter-clockwise slows the climb
    eng2, _ = run("climb", 10, ma=0.5)
    assert len(eng2.vm.trace.loads) - 3 < (len(eng.vm.trace.loads) - 3) / 4   # block changes after the 3 at start


@pytest.mark.needs_et312_data
def test_combo_gate_from_ma_and_frequency_width_sweep():
    eng, fr = run("combo", 30, ma=0.5)
    on = sum(f.a.gate_on for f in fr) / len(fr)
    assert 0.45 < on < 0.55                         # on and off time both = MA
    tr = transitions(fr, lambda f: f.a.gate_on)
    period = tr[2][0] - tr[0][0]
    assert 2 * 32 * 0.0328 * 0.9 < period < 2 * 32 * 0.0328 * 1.1   # 2 x MA(32) slow-timer units
    assert min(f.a.raw_frequency for f in fr) == 9 and max(f.a.raw_frequency for f in fr) == 100
    assert min(f.a.raw_width for f in fr) == 130 and max(f.a.raw_width for f in fr) == 200


@pytest.mark.needs_et312_data
def test_intense_a_continuous_b_gated_and_ma_sets_pulse_rate():
    eng, fr = run("intense", 4, ma=1.0)
    assert all(f.a.gate_on for f in fr)
    tr = transitions(fr, lambda f: f.b.gate_on)
    gaps = [round(tr[i + 1][0] - tr[i][0], 3) for i in range(len(tr) - 1)]
    assert all(abs(g - 63 / TICK_HZ) < 0.005 for g in gaps)     # 63 ticks on / 63 off
    assert fr[-1].a.raw_frequency == 9                          # MA CW -> frequency 9 (fastest)
    eng2, fr2 = run("intense", 1, ma=0.0)
    assert fr2[-1].a.raw_frequency == 255


@pytest.mark.needs_et312_data
def test_rhythm_gate_from_ma_width_alternates_intensity_creeps():
    eng, fr = run("rhythm", 20, ma=0.5)
    widths = {f.a.raw_width for f in fr[300:]}
    assert widths == {70, 180}
    wt = transitions(fr[300:], lambda f: f.a.raw_width)
    assert 0.95 < wt[1][0] - wt[0][0] < 1.1                     # 31 slow-timer units ~ 1.02 s
    ints = [f.a.raw_intensity for f in fr]
    assert ints[100] == 224 and ints[-1] > 224                  # +1 every other block
    gt = transitions(fr[300:1300], lambda f: f.a.gate_on)
    assert len(gt) > 20


@pytest.mark.needs_et312_data
def test_toggle_alternates_channels_with_ma_period():
    eng, fr = run("toggle", 12, ma=0.5)
    assert not any(f.a.gate_on and f.b.gate_on for f in fr)
    assert any(f.a.gate_on for f in fr) and any(f.b.gate_on for f in fr)
    tr = transitions(fr, lambda f: f.a.gate_on)
    assert 1.9 < tr[1][0] - tr[0][0] < 2.2                      # MA 63 x 32.8 ms
    eng2, fr2 = run("toggle", 12, ma=0.9)
    tr2 = transitions(fr2, lambda f: f.a.gate_on)
    assert tr2[1][0] - tr2[0][0] < 0.6 * (tr[1][0] - tr[0][0])


@pytest.mark.needs_et312_data
def test_orgasm_width_builds_slowly_and_chains_blocks():
    eng, fr = run("orgasm", 60, seed=2)
    w = [f.a.raw_width for f in fr]
    assert w[0] == 54 and max(w) == 200                          # +4 per tick to 200, then -1 per tick
    assert min(w[-5000:]) > min(w[:1200])                        # the floor creeps up +2 per cycle
    blocks = {b for _, b in eng.vm.trace.loads}
    assert {24, 25, 26, 27} <= blocks


@pytest.mark.needs_et312_data
def test_torment_is_off_most_of_the_time_with_rising_bursts():
    eng, fr = run("torment", 120, seed=3)
    on_a = sum(f.a.gate_on for f in fr) / len(fr)
    on_b = sum(f.b.gate_on for f in fr) / len(fr)
    assert on_a < 0.6 and on_b < 0.6 and (on_a > 0 or on_b > 0)
    assert not fr[5].a.gate_on and not fr[5].b.gate_on          # starts silent
    # within a burst the intensity register rises from 176
    burst = [f for f in fr if f.a.gate_on]
    if burst:
        assert burst[0].a.raw_intensity == 176 and max(f.a.raw_intensity for f in burst) > 200
    assert 28 in {b for _, b in eng.vm.trace.loads}


@pytest.mark.needs_et312_data
def test_random1_switches_between_first_six_modes():
    eng, fr = run("random1", 150, seed=5)
    names = [n for _, n in transitions(fr, lambda f: f.mode_name)] + [fr[0].mode_name]
    assert all(n in ("waves", "stroke", "climb", "combo", "intense", "rhythm") for n in names)
    assert len(set(names)) >= 2
    gaps = [b - a for (a, _), (b, _) in zip(transitions(fr, lambda f: f.mode_name), transitions(fr, lambda f: f.mode_name)[1:])]
    assert all(9 < g < 64 for g in gaps)                        # 20..120 x 0.524 s


@pytest.mark.needs_et312_data
def test_random2_randomises_rates_per_seed_and_reloads():
    e1, f1 = run("random2", 40, seed=1)
    e2, f2 = run("random2", 40, seed=2)
    assert e1.vm.modulator(0, 0xA5)["rate"] in range(1, 5)
    assert f1 != f2
    assert [b for _, b in e1.vm.trace.loads].count(32) >= 2      # reloaded every 5..31 s


@pytest.mark.needs_et312_data
def test_split_runs_stroke_on_a_and_waves_on_b():
    eng, fr = run("split", 10, ma=0.5)
    assert {f.a.raw_width for f in fr[100:]} == {255}           # Stroke A
    assert len({f.b.raw_width for f in fr}) > 20                # Waves B sweeps
    assert eng.vm.mem[0x83] & 0x10


@pytest.mark.needs_et312_data
def test_phase_modes_flags():
    _, f1 = run("phase1", 1)
    # block 20 sets width 125, block 21 (loaded under mask 3) then sets 121 on both channels
    assert f1[-1].phase_mode == "interleaved" and f1[-1].a.pulse_width_us == 121 and f1[-1].b.pulse_width_us == 121
    _, f2 = run("phase2", 3, ma=1.0)
    assert len({f.a.raw_intensity for f in f2}) > 5             # Phase 2 varies intensity
    _, f3 = run("phase3", 1)
    assert f3[-1].phase_mode == "linked"
    assert f3[-1].b.pulse_rate_hz == f3[-1].a.pulse_rate_hz and f3[-1].b.gate_on == f3[-1].a.gate_on


@pytest.mark.needs_et312_data
def test_polarity_flag_is_carried():
    eng = ET312Engine("waves")
    eng.vm.mem[0x90] |= 0x10
    f = eng.step()
    assert f.a.leading_polarity == -1 and f.b.leading_polarity == 1


@pytest.mark.needs_et312_data
def test_level_models_and_power():
    e = ET312Engine("waves", level_a=1.0, level_b=0.0)
    for _ in range(800):
        f = e.step()
    assert f.a.intensity == pytest.approx(1.0) and f.b.intensity == 0.0
    e.set_levels(a=0.5)
    e.vm.mem[0xA5] = 205
    f = e.step()
    dz = f.a.intensity
    lin = ET312Engine("waves", level_a=0.5, level_model="linear")
    for _ in range(800):
        lin.step()
    lin.vm.mem[0xA5] = 205
    assert lin.step().a.intensity == pytest.approx(0.5 * 205 / 255)
    assert dz < 0.5 * 205 / 255                                  # dead-zone model modulates deeper


@pytest.mark.needs_et312_data
def test_start_ramp_and_user_routine():
    e = ET312Engine("waves", advanced=__import__("stimengine.et312", fromlist=["AdvancedParams"]).AdvancedParams(ramp_level=0x80))
    e.start_ramp()
    assert e.vm.mem[0x9C] == 0x80 and e.vm.mem[0xA3] == 0x27
    user = {0x80: [("set", 0, 0xBE, 0x00), ("set", 0, 0xB7, 0x40), ("set", 0, 0xB5, 0x00), ("set", 0, 0xAE, 0x20)]}
    u = ET312Engine("user1", user_blocks=user, user_start={"user1": 0x80})
    f = u.step()
    assert f.a.pulse_width_us == 64 and f.a.raw_frequency == 0x20
    with pytest.raises(KeyError):
        ET312Engine("user2", user_blocks=user, user_start={"user1": 0x80})


def test_mode_names_roundtrip():
    for name in IMPLEMENTED + STUBBED:
        assert M.MODE_NAMES[MODE_BY_NAME[name]] == name
    with pytest.raises(ValueError):
        ET312Engine("nope")


# ------------------------------------------------------------------ firmware timing (regression)
# The firmware stores last := timer + 1 when a modulator steps (0x1d8), so a rate-R modulator on the 244.14 Hz
# timer steps every R + 1 ticks, not R. PlaStim felt the difference on the real box ("Orgasm / Waves come faster"):
# at rate 1 the emulator ran exactly 2x fast.

@pytest.mark.needs_et312_data
def test_orgasm_width_sweep_takes_rate_plus_one_ticks_per_step():
    # Orgasm block 24: A width step 4 from 50 to max 200 (rate $ba = 1, timer 1), then block 25 sets step -1 and
    # min 52: the sweep back down is (200 - 52) steps at 1 + 1 ticks each = 296 ticks = 1.21 s
    eng, _ = run("orgasm", 4, ma=0.5)
    loads = eng.vm.trace.loads
    first = [t for t, b in loads if b == 25][0]
    after = [t for t, b in loads if t > first][0]
    assert first == pytest.approx(38 * 2, abs=2)          # ceil(150 / 4) = 38 steps x 2 ticks
    assert after - first == pytest.approx(148 * 2, abs=3)


@pytest.mark.needs_et312_data
def test_waves_width_period_at_ma_fully_clockwise():
    # Waves A: width rate = MA, MA range 1..64; fully CW -> MA 1 -> a step (of 2) every 2 ticks; 50..200 and back
    # is 2 x 75 steps x 2 ticks = 300 ticks + the two reversal steps ~ 1.24 s (the emulator used to give 0.62 s)
    _, fr = run("waves", 12, ma=1.0)
    tops = [f.t for a, f in zip(fr, fr[1:]) if a.a.raw_width < 200 <= f.a.raw_width]
    periods = [b - a for a, b in zip(tops, tops[1:])]
    assert periods and all(p == pytest.approx(1.24, abs=0.03) for p in periods)


@pytest.mark.needs_et312_data
def test_gate_pulse_bits_match_the_firmware_output_isr():
    """Gate bits 1-2 enable the two halves of the push-pull primary (disassembly: Main 0x2a2, Timer1_CMP_A 0xe92,
    pulse start 0xf34-0xf70); bit4 swaps which half is first; bit3 alternates halves pulse to pulse."""
    from stimengine.et312.engine import gate_pulse
    assert gate_pulse(0x07) == (1, True, False, True)        # both halves: biphasic, first half leads
    assert gate_pulse(0x05) == (1, False, False, True)       # bit2 only: monophasic on the first half
    assert gate_pulse(0x03) == (-1, False, False, True)      # bit1 only: monophasic on the OTHER half
    assert gate_pulse(0x17) == (-1, True, False, True)       # bit4 swaps the order
    assert gate_pulse(0x15) == (-1, False, False, True)
    assert gate_pulse(0x13) == (1, False, False, True)
    assert gate_pulse(0x01)[3] is False                      # neither half: no pulse
    assert gate_pulse(0x0f)[2] is True                       # bit3: alternating halves


@pytest.mark.needs_et312_data
def test_pulse_period_includes_the_gap_for_both_shapes():
    """period = 256*(F+1) + phases*W us (the 0xece gap follows every pulse)."""
    eng = ET312Engine("waves")
    m = eng.vm.mem
    eng.step()
    for gate, phases in ((0x07, 2), (0x05, 1), (0x03, 1)):
        m[0x90] = gate
        m[0xAE] = 20
        m[0xB7] = 100
        c = eng._channel(0, 0.5, 0)
        assert c.pulse_rate_hz == pytest.approx(1e6 / (256 * 21 + phases * 100))
    m[0x90] = 0x01                                           # gate on, no half enabled: silent
    assert eng._channel(0, 0.5, 0).gate_on is False
