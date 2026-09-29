"""ET-312 -> FOC-Stim V4 mapping never leaves the caps, and rides under the existing volume law."""
import random
import tomllib
from pathlib import Path

import pytest

from stimengine.et312 import ET312Engine, MappingConfig, map_frame
from stimengine.et312.engine import ChannelOutput, ET312Frame
from stimengine.et312.mapping import apply_to_engine, map_rate, map_width
from stimengine.math import limits
from stimengine.math._vendor import AxisType
from stimengine.math.frame import ModelSet, StimFrame, evaluate
from stimengine.math.volume import VolumeParts

CFG = tomllib.loads((Path(__file__).resolve().parents[1] / "config" / "engine.toml").read_text(encoding="utf-8"))


def ch(intensity=0.5, rate=100.0, width=130.0, gate=True, biphasic=True):
    return ChannelOutput(gate_on=gate, intensity=intensity, pulse_rate_hz=rate, pulse_width_us=width,
                         biphasic=biphasic, leading_polarity=1,
                         phase_asymmetry="et312_biphasic" if biphasic else "et312_monophasic",
                         raw_gate=7, raw_ramp=255, raw_intensity=255, raw_frequency=22, raw_width=130)


def frame(a, b):
    return ET312Frame(t=0.0, tick=0, mode=0x76, mode_name="waves", a=a, b=b, phase_mode="none", ma_value=0, ctrl_flags=0)


def test_config_bounds_intersect_limits():
    cfg = MappingConfig.from_config(CFG)
    assert cfg.rate_bounds == (1.0, 100.0)               # config P0 max 150 intersected with FOC 100
    assert cfg.width_bounds == (4.0, 10.0)
    assert cfg.carrier_bounds == (500.0, 2000.0)


@pytest.mark.parametrize("rate_map", ["compress", "clamp"])
def test_rate_never_exceeds_100hz(rate_map):
    cfg = MappingConfig.from_config(CFG, rate_map=rate_map)
    for r in (0.0, 15.0, 100.0, 150.0, 430.0, 1e6):
        assert 1.0 <= map_rate(r, cfg) <= 100.0
    assert map_rate(430.0, cfg) == 100.0
    if rate_map == "compress":
        assert map_rate(15.2, cfg) == pytest.approx(15.0, abs=0.2)
        assert map_rate(60.0, cfg) < map_rate(120.0, cfg) < map_rate(300.0, cfg)   # monotonic sweep survives


def test_width_maps_onto_burst_length_range():
    cfg = MappingConfig.from_config(CFG)
    assert map_width(50, cfg) == 4.0 and map_width(255, cfg) == 10.0
    assert 4.0 < map_width(130, cfg) < 10.0
    assert map_width(0, cfg) == 4.0 and map_width(1000, cfg) == 10.0


def test_geometry_fourphase_and_threephase():
    cfg4 = MappingConfig.from_config(CFG, geometry="fourphase")
    t = map_frame(frame(ch(0.6), ch(0.3)), cfg4)
    assert t.api_volume == pytest.approx(0.6) and (t.e1, t.e2) == (1.0, 1.0) and t.e3 == pytest.approx(0.5)
    t = map_frame(frame(ch(0.6), ch(0.3, gate=False)), cfg4)
    assert t.e3 == 0.0 and t.e4 == 0.0 and t.dominant == "a"
    cfg3 = MappingConfig.from_config(CFG, geometry="threephase")
    t = map_frame(frame(ch(0.6), ch(0.0)), cfg3)
    assert t.alpha == 1.0 and t.api_volume == pytest.approx(0.6)
    t = map_frame(frame(ch(0.0, gate=False), ch(0.4)), cfg3)
    assert t.alpha == -1.0 and t.api_volume == pytest.approx(0.4) and t.dominant == "b"
    t = map_frame(frame(ch(0.0, gate=False), ch(0.0, gate=False)), cfg3)
    assert t.alpha == 0.0 and t.api_volume == 0.0


def test_random_states_stay_inside_caps_and_amps_cap():
    """Any ET-312 state -> targets inside axis ranges; evaluated through the real volume chain the
    amps never exceed waveform_amplitude_amps even with master at 1."""
    models = ModelSet.from_config(CFG)
    cap = CFG["signal"]["waveform_amplitude_amps"]
    rnd = random.Random(0)
    for geometry in ("fourphase", "threephase"):
        cfg = MappingConfig.from_config(CFG, geometry=geometry, width_to_carrier=rnd.random() < 0.5)
        for _ in range(300):
            a = ch(rnd.uniform(-0.5, 1.5), rnd.uniform(0, 1000), rnd.uniform(0, 400), rnd.random() < 0.8)
            b = ch(rnd.uniform(-0.5, 1.5), rnd.uniform(0, 1000), rnd.uniform(0, 400), rnd.random() < 0.8)
            t = map_frame(frame(a, b), cfg)
            assert 0.0 <= t.api_volume <= 1.0
            assert 1.0 <= t.pulse_frequency <= limits.PulseFrequencyFOC.max
            assert 4.0 <= t.pulse_width <= 10.0
            assert CFG["signal"]["min_carrier_hz"] <= t.carrier <= CFG["signal"]["max_carrier_hz"]
            assert all(0.0 <= v <= 1.0 for v in (t.e1, t.e2, t.e3, t.e4))
            assert -1.0 <= t.alpha <= 1.0 and -1.0 <= t.beta <= 1.0
            sf = StimFrame(mode=t.mode, alpha=t.alpha, beta=t.beta, e1=t.e1, e2=t.e2, e3=t.e3, e4=t.e4,
                           volume=VolumeParts(master=1.0, api=t.api_volume),
                           carrier_frequency=t.carrier, pulse_frequency=t.pulse_frequency, pulse_width=t.pulse_width)
            out = evaluate(sf, models)
            assert out[AxisType.AXIS_WAVEFORM_AMPLITUDE_AMPS] <= cap + 1e-9
            assert out[AxisType.AXIS_WAVEFORM_AMPLITUDE_AMPS] <= limits.WaveformAmplitudeFOC.max


def test_live_modes_map_inside_caps():
    cfg = MappingConfig.from_config(CFG)
    for name in ("waves", "climb", "torment", "random2"):
        eng = ET312Engine(name, level_a=1.0, level_b=1.0, ma=1.0, power="high", seed=4)
        for f in eng.run(20):
            t = map_frame(f, cfg)
            assert 0.0 <= t.api_volume <= 1.0 and t.pulse_frequency <= 100.0 and t.pulse_width <= 10.0


class _FakeEngine:
    def __init__(self):
        self.calls = []

    def set_api_volume(self, level, source):
        self.calls.append(("api", level, source))

    def set_vector(self, *e, source):
        self.calls.append(("vector", e, source))

    def set_position(self, a, b, source):
        self.calls.append(("position", (a, b), source))

    def set_pulse(self, *, frequency, width, source):
        self.calls.append(("pulse", frequency, width, source))

    def set_carrier(self, hz, source):
        self.calls.append(("carrier", hz, source))

    def set_master(self, *a, **k):
        raise AssertionError("mapping must never touch master volume")


def test_apply_uses_only_api_volume_and_public_setters():
    fake = _FakeEngine()
    t = map_frame(frame(ch(0.6), ch(0.3)), MappingConfig.from_config(CFG))
    apply_to_engine(fake, t)
    kinds = [c[0] for c in fake.calls]
    assert kinds == ["api", "vector", "pulse", "carrier"]
    assert all(c[-1] == "et312" for c in fake.calls)
    assert fake.calls[0][1] == pytest.approx(0.6)
