"""ET-312 frame -> FOC-Stim V4 (stock firmware 1.3.2) control targets.

Kept separate from the mode engine so the same ET312Frame can later drive custom firmware or the
multi-contact box, which can render the per-channel pulse parameters literally.

What the V4 can do with stock firmware: one star-connected field, sine bursts of >= 3 carrier
cycles at 300-2000 Hz carrier, pulse rate <= 100 Hz, symmetric pulses.  So this mapping is an
approximation of the *modulation* the ET-312 modes produce, not of its pulses:

  intensity A/B -> api_volume + geometry.  The two ET-312 channels are two isolated electrode
      pairs; the V4 has one field.  `geometry="fourphase"` puts channel A on electrodes 1+2 and
      channel B on 3+4 (AXIS_ELECTRODE_n_POWER = gated intensity) - current still flows between
      all active electrodes, so "A only" is a pair, "A and B" is a four-electrode field.
      `geometry="threephase"` collapses A/B into a position: alpha = (A - B)/(A + B), beta fixed,
      so A-only sits at one rim, B-only at the other, both in the middle.  api_volume is the
      larger of the two gated intensities (fourphase) or their max (threephase); everything
      passes through the existing volume law (master x api x inactivity x watchdog), never
      around it.
  pulse rate -> AXIS_PULSE_FREQUENCY_HZ of the dominant channel (higher gated intensity).  The
      ET-312 makes 15-430 Hz; the V4 caps at 100 Hz (limits.PulseFrequencyFOC).  `rate_map=
      "compress"` (default) maps 15..430 Hz logarithmically onto rate_lo..rate_hi (15..100) so a
      Climb sweep stays a sweep; "clamp" keeps true rates and clips at 100 Hz.
  pulse width -> AXIS_PULSE_WIDTH_IN_CYCLES.  An ET-312 pulse (2 x 50-200 us) is shorter than one
      V4 carrier cycle, so the width register is mapped linearly onto the configured burst-length
      range (config P1 axis, 4..10 cycles): wider ET pulse = longer burst = more charge, same
      perceptual direction.  Optionally `width_to_carrier=True` also moves the carrier from
      carrier_hi (narrow pulse) to carrier_lo (wide pulse) inside the configured band.
  leading_polarity / phase_asymmetry / biphasic -> ignored (symmetric sine bursts).
  gate -> intensity 0 while the gate is closed (the V4 has no per-pulse gate; the volume law's
      slow-start only limits increases, so a gate opening is rate-limited by it, a closing is not).
  phase modes -> "linked" is already folded into the frame; "interleaved" is ignored.

Caps: every target is clipped to the engine's configured axis ranges (config/engine.toml
[tcode.axes] and [signal]) intersected with stim_math limits; api_volume is 0..1.  The mapping never
touches master volume or waveform_amplitude_amps.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..math import limits
from .engine import ET312Frame

ET312_RATE_MIN_HZ = 1e6 / (256 * 255 + 2 * 255)     # slowest the box makes (~15.2 Hz)
ET312_RATE_MAX_HZ = 1e6 / (256 * 9 + 2 * 50)        # fastest after the max(9,F) clamp (~415 Hz)
ET312_WIDTH_MIN = 50
ET312_WIDTH_MAX = 255


@dataclass(frozen=True)
class MappingConfig:
    geometry: str = "fourphase"          # "fourphase" | "threephase"
    rate_map: str = "compress"           # "compress" | "clamp"
    rate_lo: float = 15.0                # compress: output range (Hz), clipped to the axis range
    rate_hi: float = 100.0
    pulse_width_lo: float = 4.0          # burst length range (carrier cycles), from config P1
    pulse_width_hi: float = 10.0
    carrier_hz: float = 790.0            # static carrier unless width_to_carrier
    width_to_carrier: bool = False
    carrier_lo: float = 600.0
    carrier_hi: float = 1500.0
    beta: float = 0.0                    # threephase: fixed beta
    min_carrier_hz: float = 500.0        # caps (config [signal])
    max_carrier_hz: float = 2000.0
    max_pulse_frequency_hz: float = 150.0   # config P0 axis max; intersected with limits (100)

    @classmethod
    def from_config(cls, cfg: dict, **overrides) -> "MappingConfig":
        sig = cfg.get("signal", {})
        axes = cfg.get("tcode", {}).get("axes", {})
        cd = cfg.get("carrier_defaults", {})
        p0 = axes.get("P0", {})
        p1 = axes.get("P1", {})
        kw = dict(
            pulse_width_lo=float(p1.get("min", 4.0)), pulse_width_hi=float(p1.get("max", 10.0)),
            carrier_hz=float(cd.get("pulse_carrier_frequency", 790.0)),
            min_carrier_hz=float(sig.get("min_carrier_hz", 500.0)),
            max_carrier_hz=float(sig.get("max_carrier_hz", 2000.0)),
            max_pulse_frequency_hz=float(p0.get("max", 150.0)),
        )
        kw.update(overrides)
        return cls(**kw)

    # hard bounds after intersecting with stim_math limits
    @property
    def rate_bounds(self) -> tuple[float, float]:
        hi = min(self.max_pulse_frequency_hz, float(limits.PulseFrequencyFOC.max))
        return float(limits.PulseFrequencyFOC.min), hi

    @property
    def width_bounds(self) -> tuple[float, float]:
        lo = max(self.pulse_width_lo, float(limits.PulseWidth.min))
        hi = min(self.pulse_width_hi, float(limits.PulseWidth.max))
        return lo, max(lo, hi)

    @property
    def carrier_bounds(self) -> tuple[float, float]:
        lo = max(self.min_carrier_hz, float(limits.CarrierFrequencyFOC.min))
        hi = min(self.max_carrier_hz, float(limits.CarrierFrequencyFOC.max))
        return lo, max(lo, hi)


@dataclass(frozen=True)
class V4Targets:
    """What gets written to the engine (all already inside the caps)."""
    mode: str                       # "fourphase" | "threephase"
    api_volume: float               # 0..1, under master
    e1: float = 0.0
    e2: float = 0.0
    e3: float = 0.0
    e4: float = 0.0
    alpha: float = 0.0
    beta: float = 0.0
    pulse_frequency: float = 50.0
    pulse_width: float = 6.0
    carrier: float = 790.0
    dominant: str = "a"

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _clip(v: float, lo: float, hi: float) -> float:
    return float(min(hi, max(lo, v)))


def _lerp(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    if x1 == x0:
        return y0
    t = _clip((x - x0) / (x1 - x0), 0.0, 1.0)
    return y0 + t * (y1 - y0)


def map_rate(rate_hz: float, cfg: MappingConfig) -> float:
    lo, hi = cfg.rate_bounds
    if cfg.rate_map == "compress":
        a, b = math.log(ET312_RATE_MIN_HZ), math.log(ET312_RATE_MAX_HZ)
        t = _clip((math.log(max(rate_hz, 1e-3)) - a) / (b - a), 0.0, 1.0)
        out_lo, out_hi = _clip(cfg.rate_lo, lo, hi), _clip(cfg.rate_hi, lo, hi)
        v = out_lo + t * (out_hi - out_lo)
    else:
        v = rate_hz
    return _clip(v, lo, hi)


def map_width(width_us: float, cfg: MappingConfig) -> float:
    lo, hi = cfg.width_bounds
    v = _lerp(width_us, ET312_WIDTH_MIN, ET312_WIDTH_MAX, cfg.pulse_width_lo, cfg.pulse_width_hi)
    return _clip(v, lo, hi)


def map_carrier(width_us: float, cfg: MappingConfig) -> float:
    lo, hi = cfg.carrier_bounds
    if cfg.width_to_carrier:
        v = _lerp(width_us, ET312_WIDTH_MIN, ET312_WIDTH_MAX, cfg.carrier_hi, cfg.carrier_lo)
    else:
        v = cfg.carrier_hz
    return _clip(v, lo, hi)


def map_frame(frame: ET312Frame, cfg: MappingConfig | None = None) -> V4Targets:
    cfg = cfg or MappingConfig()
    ia = _clip(frame.a.effective, 0.0, 1.0)
    ib = _clip(frame.b.effective, 0.0, 1.0)
    dom = frame.a if ia >= ib else frame.b
    dominant = "a" if ia >= ib else "b"
    pf = map_rate(dom.pulse_rate_hz, cfg)
    pw = map_width(dom.pulse_width_us, cfg)
    carrier = map_carrier(dom.pulse_width_us, cfg)
    if cfg.geometry == "fourphase":
        vol = max(ia, ib)
        # per-electrode intensities are relative to the volume so the louder channel is at 1.0
        ea = ia / vol if vol > 0 else 0.0
        eb = ib / vol if vol > 0 else 0.0
        return V4Targets(mode="fourphase", api_volume=_clip(vol, 0, 1),
                         e1=_clip(ea, 0, 1), e2=_clip(ea, 0, 1), e3=_clip(eb, 0, 1), e4=_clip(eb, 0, 1),
                         pulse_frequency=pf, pulse_width=pw, carrier=carrier, dominant=dominant)
    if cfg.geometry == "threephase":
        s = ia + ib
        alpha = (ia - ib) / s if s > 0 else 0.0
        return V4Targets(mode="threephase", api_volume=_clip(max(ia, ib), 0, 1),
                         alpha=_clip(alpha, -1, 1), beta=_clip(cfg.beta, -1, 1),
                         pulse_frequency=pf, pulse_width=pw, carrier=carrier, dominant=dominant)
    raise ValueError(f"unknown geometry {cfg.geometry!r}")


def apply_to_engine(engine, targets: V4Targets, source: str = "et312") -> None:
    """Write targets through the stim-engine's public setters (which feed the safety stack).

    This is the only place the emulator touches the live engine; it sets api_volume (never master),
    geometry, pulse and carrier.  It does not arm, start or renew leases."""
    engine.set_api_volume(targets.api_volume, source=source)
    if targets.mode == "fourphase":
        engine.set_vector(targets.e1, targets.e2, targets.e3, targets.e4, source=source)
    else:
        engine.set_position(targets.alpha, targets.beta, source=source)
    engine.set_pulse(frequency=targets.pulse_frequency, width=targets.pulse_width, source=source)
    engine.set_carrier(targets.carrier, source=source)


__all__ = ["MappingConfig", "V4Targets", "map_frame", "map_rate", "map_width", "map_carrier",
           "apply_to_engine", "ET312_RATE_MIN_HZ", "ET312_RATE_MAX_HZ"]
