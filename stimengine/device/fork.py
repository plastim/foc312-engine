"""stim-engine fork firmware (firmware/, FOC-Stim 1.3.2 + OUTPUT_BIPHASIC_PAIRS): ids and route codes.

The host protobufs are generated from stock v1.66 restim and do not know the fork's enum values; proto3 enums
are open, so the raw ints below go over the wire unchanged. Source of truth: firmware/proto/focstim/constants.proto
and firmware/NOTES.md §8.

Route codes (AXIS_BIPHASIC_A/B_ROUTE): two digits, first electrode then second, 1-based: 12, 23, 41 ... The
first digit is cathodic in the leading phase when the polarity axis is < 0.5. The host always sends polarity 0
and expresses a lead swap as a digit swap (12 -> 21), so there is exactly one way to say each pulse.
"""

from __future__ import annotations

import re

FORK_COMMENT = "stim-engine biphasic-pairs"      # FirmwareVersion.comment of the fork (config_version.h): base
# v1: exactly FORK_COMMENT (20 us width grid, half-sine only). v2: FORK_COMMENT + " v2" (fractional widths, pulse
# shapes). Any other suffix after the base is a fork of unknown version: treated with v1 capabilities.
FORK_COMMENT_V2 = FORK_COMMENT + " v2"

OUTPUT_BIPHASIC_PAIRS = 5

AXIS_BIPHASIC_A_AMPLITUDE_AMPS = 60
AXIS_BIPHASIC_B_AMPLITUDE_AMPS = 61
AXIS_BIPHASIC_A_PULSE_FREQUENCY_HZ = 62
AXIS_BIPHASIC_B_PULSE_FREQUENCY_HZ = 63
AXIS_BIPHASIC_A_PHASE_WIDTH_US = 64
AXIS_BIPHASIC_B_PHASE_WIDTH_US = 65
AXIS_BIPHASIC_INTERPHASE_GAP_US = 66
AXIS_BIPHASIC_A_POLARITY = 67
AXIS_BIPHASIC_B_POLARITY = 68
AXIS_BIPHASIC_A_ASYMMETRY = 69
AXIS_BIPHASIC_B_ASYMMETRY = 70
AXIS_BIPHASIC_A_ROUTE = 71
AXIS_BIPHASIC_B_ROUTE = 72
AXIS_BIPHASIC_A_SHAPE = 73      # v2 only: 0 rounded (half-sine), 1 square, 2 soft square (trapezoid, 20 us edges)
AXIS_BIPHASIC_B_SHAPE = 74

AMP_AXES = (AXIS_BIPHASIC_A_AMPLITUDE_AMPS, AXIS_BIPHASIC_B_AMPLITUDE_AMPS)
FREQ_AXES = (AXIS_BIPHASIC_A_PULSE_FREQUENCY_HZ, AXIS_BIPHASIC_B_PULSE_FREQUENCY_HZ)
WIDTH_AXES = (AXIS_BIPHASIC_A_PHASE_WIDTH_US, AXIS_BIPHASIC_B_PHASE_WIDTH_US)
POLARITY_AXES = (AXIS_BIPHASIC_A_POLARITY, AXIS_BIPHASIC_B_POLARITY)
ASYM_AXES = (AXIS_BIPHASIC_A_ASYMMETRY, AXIS_BIPHASIC_B_ASYMMETRY)
ROUTE_AXES = (AXIS_BIPHASIC_A_ROUTE, AXIS_BIPHASIC_B_ROUTE)
SHAPE_AXES = (AXIS_BIPHASIC_A_SHAPE, AXIS_BIPHASIC_B_SHAPE)
# switch in one step, never interpolated (the firmware also uses the target, NOTES.md §8 "Live routing")
IMMEDIATE_AXES = frozenset(ROUTE_AXES + POLARITY_AXES + SHAPE_AXES)

SHAPE_ROUNDED, SHAPE_SQUARE, SHAPE_SOFT, SHAPE_TRIANGLE = 0, 1, 2, 3
# v7 taper: ids 40..50 = flat-top fraction 0.0..1.0 in tenths (40 = the half-sine, 50 = square); the firmware's shape
# axis takes id / 10 (4.0..5.0). Quarter-sine edges, flat in between: a continuous rounded -> square control.
SHAPE_TAPER_0, SHAPE_TAPER_1 = 40, 50
SHAPES = {SHAPE_ROUNDED: "rounded", SHAPE_SQUARE: "square", SHAPE_SOFT: "soft", SHAPE_TRIANGLE: "triangle",
          **{i: f"taper{(i - SHAPE_TAPER_0) * 10}" for i in range(SHAPE_TAPER_0, SHAPE_TAPER_1 + 1)}}
SHAPE_BY_NAME = {v: k for k, v in SHAPES.items()}


def shape_axis_value(shape: int) -> float:
    """What the firmware's shape axis gets: 0..3 as they are, a taper id as 4.0..5.0."""
    return float(shape) if shape < SHAPE_TAPER_0 else shape / 10.0


def shape_min_fork(shape: int) -> int:
    """Fork firmware version that plays `shape`. An older fork clamps an unknown shape to soft square (more charge
    than the host planned for), so a host must never send a shape to a box below this version."""
    return 7 if shape == SHAPE_TRIANGLE or shape >= SHAPE_TAPER_0 else 2

# firmware axis ranges (main_focstim_v4.cpp simple_axes)
FREQ_RANGE = (1.0, 400.0)
WIDTH_RANGE = (40.0, 400.0)
WIDTH_GRID_US = 20.0     # the firmware plays pulses on its 50 kHz PWM grid: widths snap to 20 us (2..20 samples)


def snap_width(width_us: float) -> tuple[float, float]:
    """(width the firmware will actually play, amplitude factor that keeps the phase CHARGE of the requested width).
    The firmware rounds the width to the 20 us grid, so a smoothly swept width (ET-312 Waves: 50 -> 120 us) would
    play as 40/60/80/100/120 us steps: +50 %, +33 %, +25 % jumps in charge that are felt as distinct steps. Scaling the
    amplitude by requested/played keeps the charge per phase continuous; for pulses this short (well under sensory
    chronaxie) charge is what sets the sensation. Mirrors firmware int(width * fs + 0.5) clamped to 2..20 samples."""
    n = min(20, max(2, int(float(width_us) / WIDTH_GRID_US + 0.5)))
    played = n * WIDTH_GRID_US
    return played, float(width_us) / played


def shape_id(shape) -> int:
    """0..3, 40..50 or "rounded"/"square"/"soft"/"triangle"/"taper50" (also "taper 50", "taper:0.5") -> an id."""
    if isinstance(shape, str) and not shape.strip().isdigit():
        key = shape.strip().lower().replace(" ", "").replace("_", "").replace("-", "")
        key = {"softsquare": "soft", "halfsine": "rounded", "sine": "rounded"}.get(key, key)
        if key.startswith("taper") and key not in SHAPE_BY_NAME:
            num = key[5:].lstrip(":")
            try:
                frac = float(num) if "." in num else float(num) / 100.0
            except ValueError:
                frac = -1.0
            if 0.0 <= frac <= 1.0:
                return SHAPE_TAPER_0 + int(round(frac * 10))
        if key not in SHAPE_BY_NAME:
            raise ValueError(f"shape {shape!r}: rounded, square, soft, triangle or taper0..taper100")
        return SHAPE_BY_NAME[key]
    n = int(shape)
    if n not in SHAPES:
        raise ValueError(f"shape {shape!r}: 0 rounded, 1 square, 2 soft, 3 triangle, 40..50 taper")
    return n


def phase_charge(shape: int, width_us: float) -> float:
    """Charge of one unit-peak phase of `width_us`, in (unit amps) x (20 us samples) - the firmware's shapes:
    rounded (half-sine) 2w/pi, square w, soft square (20 us linear edges, a triangle under 40 us) w - min(1, w/2),
    v7 triangle w/2, v7 taper f: two quarter-sine edges of r = (1 - f) w / 2 each (charge 2r/pi) and w - 2r flat."""
    w = float(width_us) / WIDTH_GRID_US
    if shape == SHAPE_SQUARE:
        return w
    if shape == SHAPE_SOFT:
        return w - min(1.0, w / 2)
    if shape == SHAPE_TRIANGLE:
        return w / 2
    if shape >= SHAPE_TAPER_0:
        r = (1.0 - (shape - SHAPE_TAPER_0) / 10.0) * w / 2
        return 4.0 * r / 3.141592653589793 + (w - 2 * r)
    return 2.0 * w / 3.141592653589793


def shape_charge_factor(shape: int, width_us: float) -> float:
    """Amplitude factor that gives `shape` the same phase charge as the rounded pulse at the same level, so a
    shape switch never jumps the charge (rounded -> square at equal peak would be +57 %)."""
    return phase_charge(SHAPE_ROUNDED, width_us) / phase_charge(shape, width_us)


GAP_RANGE = (0.0, 200.0)
ASYM_RANGE = (1.0, 4.0)

DEFAULT_ROUTES = (12, 34)


class RouteError(ValueError):
    """Not a valid route code (two different electrode digits 1-4)."""


def parse_route(code) -> tuple[int, int]:
    """Route code -> (first, second) electrode, 1-based. Accepts 23, "23", "2-3", (2, 3)."""
    if isinstance(code, (tuple, list)) and len(code) == 2:
        x, y = code
    else:
        s = str(code).strip().replace("-", "").replace(",", "").replace(" ", "")
        if len(s) != 2 or not s.isdigit():
            raise RouteError(f"route {code!r}: two electrode digits 1-4, e.g. 12 or 41")
        x, y = int(s[0]), int(s[1])
    try:
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        raise RouteError(f"route {code!r}: electrodes must be numbers") from None
    if not (1 <= x <= 4 and 1 <= y <= 4):
        raise RouteError(f"route {code!r}: electrodes are 1-4")
    if x == y:
        raise RouteError(f"route {code!r}: needs two different electrodes")
    return x, y


def route_code(x: int, y: int) -> int:
    parse_route((x, y))
    return 10 * int(x) + int(y)


def validate_route(code) -> int:
    """Normalized int code, or RouteError."""
    return route_code(*parse_route(code))


def reverse_route(code) -> int:
    """Swap which end leads: 12 -> 21."""
    x, y = parse_route(code)
    return route_code(y, x)


def fork_version_of_comment(comment: str | None) -> int:
    """0 = not the fork; 1 = v1 (or an unknown fork suffix: v1 capabilities only); N for an exact " vN" suffix.
    v3 (width-aware impedance model, trip report) is host-compatible with v2: callers test ``>= 2``."""
    c = comment or ""
    if not c.startswith(FORK_COMMENT):
        return 0
    if c == FORK_COMMENT:
        return 1
    m = re.fullmatch(re.escape(FORK_COMMENT) + r" v(\d+)", c)
    return int(m.group(1)) if m and int(m.group(1)) >= 2 else 1


def fork_version(client) -> int:
    fw = getattr(client, "firmware", None)
    try:
        return fork_version_of_comment(fw.stm32_firmware_version_2.comment) if fw is not None else 0
    except AttributeError:
        return 0


def is_fork_firmware(client) -> bool:
    return fork_version(client) > 0


__all__ = [n for n in dir() if n.isupper() or n in (
    "RouteError", "parse_route", "route_code", "validate_route", "reverse_route", "is_fork_firmware", "fork_version",
    "fork_version_of_comment", "snap_width", "shape_id", "phase_charge", "shape_charge_factor", "shape_axis_value",
    "shape_min_fork")]
