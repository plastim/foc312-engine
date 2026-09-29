"""Dry-run preview: render N seconds of an ET-312 mode's A/B parameter streams to a PNG.

    py -3.13 -m stimengine.et312.preview waves [--seconds 60] [--ma 0.5] [--level-a 0.6] [--level-b 0.6]
                                         [--seed 0] [--advanced depth=215,tempo=1] [--out cache/et312/waves.png]
                                         [--geometry fourphase|threephase] [--csv]

No device, no server, no third-party plotting library (the venv has neither matplotlib nor PIL):
a tiny rasterizer + PNG encoder.  Default output dir is cache/et312/ (gitignored).

Panels, top to bottom: A intensity (gate-shaded), A pulse rate, A pulse width, B same three,
then the V4 mapping: api_volume and pulse_frequency actually sent.
"""
from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

from .engine import AdvancedParams, ET312Engine, ET312Frame
from .mapping import MappingConfig, map_frame
from .modes import IMPLEMENTED, STUBBED

# ---------------------------------------------------------------- tiny raster canvas
_FONT = {  # 3x5 glyphs, rows top->bottom, 1 = pixel
    "A": "010101111101101", "B": "110101110101110", "C": "011100100100011", "D": "110101101101110",
    "E": "111100110100111", "F": "111100110100100", "G": "011100101101011", "H": "101101111101101",
    "I": "111010010010111", "J": "001001001101010", "K": "101101110101101", "L": "100100100100111",
    "M": "101111111101101", "N": "110101101101101", "O": "010101101101010", "P": "110101110100100",
    "Q": "010101101110011", "R": "110101110101101", "S": "011100010001110", "T": "111010010010010",
    "U": "101101101101011", "V": "101101101101010", "W": "101101111111101", "X": "101101010101101",
    "Y": "101101010010010", "Z": "111001010100111", "0": "010101101101010", "1": "010110010010111",
    "2": "110001010100111", "3": "110001010001110", "4": "101101111001001", "5": "111100110001110",
    "6": "011100110101010", "7": "111001010010010", "8": "010101010101010", "9": "010101011001110",
    " ": "000000000000000", ".": "000000000000010", "/": "001001010100100", "-": "000000111000000",
    ":": "000010000010000", "%": "101001010100101", "=": "000111000111000", "(": "010100100100010",
    ")": "010001001001010", "_": "000000000000111", "+": "000010111010000",
}


class Canvas:
    def __init__(self, w: int, h: int, bg=(255, 255, 255)) -> None:
        self.w, self.h = w, h
        self.px = bytearray(bg * (w * h))

    def set(self, x: int, y: int, c) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            i = 3 * (y * self.w + x)
            self.px[i:i + 3] = bytes(c)

    def rect(self, x0: int, y0: int, x1: int, y1: int, c) -> None:
        for y in range(max(0, y0), min(self.h, y1)):
            for x in range(max(0, x0), min(self.w, x1)):
                self.set(x, y, c)

    def line(self, x0: int, y0: int, x1: int, y1: int, c) -> None:
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = 1 if x0 < x1 else -1, 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            self.set(x0, y0, c)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def text(self, x: int, y: int, s: str, c, scale: int = 2) -> None:
        for ch in s.upper():
            g = _FONT.get(ch, _FONT[" "])
            for r in range(5):
                for col in range(3):
                    if g[r * 3 + col] == "1":
                        self.rect(x + col * scale, y + r * scale, x + (col + 1) * scale, y + (r + 1) * scale, c)
            x += 4 * scale

    def png(self) -> bytes:
        raw = b"".join(b"\x00" + bytes(self.px[y * self.w * 3:(y + 1) * self.w * 3]) for y in range(self.h))

        def chunk(tag: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


# ---------------------------------------------------------------- plotting
INK = (30, 30, 30)
GRID = (225, 225, 225)
GATE = (235, 245, 255)
COL_A = (200, 40, 40)
COL_B = (30, 90, 200)
COL_V = (20, 140, 60)


def _panel(cv: Canvas, x0: int, y0: int, w: int, h: int, label: str, xs: list[float], ys: list[float],
           lo: float, hi: float, color, gate: list[bool] | None = None, unit: str = "") -> None:
    if gate:
        n = len(gate)
        run_start = None
        for i in range(n + 1):
            on = gate[i] if i < n else False
            if on and run_start is None:
                run_start = i
            elif not on and run_start is not None:
                cv.rect(x0 + int(run_start * w / n), y0, x0 + int(i * w / n) + 1, y0 + h, GATE)
                run_start = None
    cv.rect(x0, y0, x0 + w, y0 + 1, GRID)
    cv.rect(x0, y0 + h - 1, x0 + w, y0 + h, GRID)
    for k in range(1, 4):
        cv.rect(x0, y0 + k * h // 4, x0 + w, y0 + k * h // 4 + 1, GRID)
    for k in range(1, 6):
        cv.rect(x0 + k * w // 6, y0, x0 + k * w // 6 + 1, y0 + h, GRID)
    span = (hi - lo) or 1.0
    px, py = None, None
    for x, y in zip(xs, ys):
        X = x0 + int(x * (w - 1))
        Y = y0 + h - 1 - int((min(hi, max(lo, y)) - lo) / span * (h - 1))
        if px is not None:
            cv.line(px, py, X, Y, color)
        px, py = X, Y
    cv.text(x0 + 4, y0 + 3, f"{label} {min(ys):.3g}..{max(ys):.3g}{unit}", INK, 2)


def render(frames: list[ET312Frame], targets: list, title: str, width: int = 1400) -> Canvas:
    rows = [
        ("A INTENSITY", [f.a.intensity for f in frames], 0.0, 1.0, COL_A, [f.a.gate_on for f in frames], ""),
        ("A PULSE RATE HZ", [f.a.pulse_rate_hz for f in frames], 0.0, 450.0, COL_A, None, ""),
        ("A WIDTH US", [f.a.pulse_width_us for f in frames], 0.0, 260.0, COL_A, None, ""),
        ("B INTENSITY", [f.b.intensity for f in frames], 0.0, 1.0, COL_B, [f.b.gate_on for f in frames], ""),
        ("B PULSE RATE HZ", [f.b.pulse_rate_hz for f in frames], 0.0, 450.0, COL_B, None, ""),
        ("B WIDTH US", [f.b.pulse_width_us for f in frames], 0.0, 260.0, COL_B, None, ""),
        ("V4 API VOLUME", [t.api_volume for t in targets], 0.0, 1.0, COL_V, None, ""),
        ("V4 PULSE FREQ HZ", [t.pulse_frequency for t in targets], 0.0, 110.0, COL_V, None, ""),
        ("V4 PULSE WIDTH CYC", [t.pulse_width for t in targets], 0.0, 12.0, COL_V, None, ""),
    ]
    ph, gap, top, left = 90, 6, 30, 10
    cv = Canvas(width, top + len(rows) * (ph + gap) + 24)
    cv.text(left, 8, title, INK, 2)
    n = len(frames)
    step = max(1, n // (width * 2))          # decimate for drawing speed; keep min/max structure
    xs = [i / (n - 1) for i in range(0, n, step)]
    y = top
    for label, ys, lo, hi, col, gate, unit in rows:
        _panel(cv, left, y, width - 2 * left, ph, label, xs, ys[::step], lo, hi, col,
               gate[::step] if gate else None, unit)
        y += ph + gap
    secs = frames[-1].t
    for k in range(7):
        cv.text(left + k * (width - 2 * left) // 6, y + 4, f"{secs * k / 6:.0f}S", INK, 2)
    return cv


# ---------------------------------------------------------------- CLI
def _parse_advanced(s: str | None) -> AdvancedParams:
    if not s:
        return AdvancedParams()
    kw = {}
    for part in s.split(","):
        k, v = part.split("=")
        kw[k.strip()] = int(v, 0)
    return AdvancedParams(**kw)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", help="mode name: " + " ".join(IMPLEMENTED) + " | stubs: " + " ".join(STUBBED))
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--ma", type=float, default=0.5, help="Multi-Adjust knob 0 (CCW) .. 1 (CW)")
    ap.add_argument("--level-a", type=float, default=0.6)
    ap.add_argument("--level-b", type=float, default=0.6)
    ap.add_argument("--power", default="normal", choices=["low", "normal", "high"])
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--advanced", default=None, help="e.g. depth=215,tempo=1,width=130")
    ap.add_argument("--level-model", default="deadzone", choices=["deadzone", "linear"])
    ap.add_argument("--geometry", default="fourphase", choices=["fourphase", "threephase"])
    ap.add_argument("--rate-map", default="compress", choices=["compress", "clamp"])
    ap.add_argument("--out", default=None, help="PNG path (default cache/et312/<mode>.png)")
    ap.add_argument("--csv", action="store_true", help="also write <out>.csv with every tick")
    args = ap.parse_args(argv)

    eng = ET312Engine(args.mode, level_a=args.level_a, level_b=args.level_b, ma=args.ma,
                      power=args.power, advanced=_parse_advanced(args.advanced), seed=args.seed,
                      level_model=args.level_model)
    cfg = MappingConfig(geometry=args.geometry, rate_map=args.rate_map)
    frames = list(eng.run(args.seconds))
    targets = [map_frame(f, cfg) for f in frames]

    out = Path(args.out) if args.out else Path("cache") / "et312" / f"{args.mode}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    title = (f"ET312 {args.mode.upper()} {args.seconds:.0f}S MA {args.ma:.2f} A {args.level_a:.2f} "
             f"B {args.level_b:.2f} SEED {args.seed} {args.geometry.upper()}")
    out.write_bytes(render(frames, targets, title).png())
    if args.csv:
        with open(out.with_suffix(".csv"), "w", encoding="utf-8") as fh:
            fh.write("t,mode,a_gate,a_int,a_rate,a_width,a_biphasic,b_gate,b_int,b_rate,b_width,b_biphasic,"
                     "ma,v4_volume,v4_pf,v4_pw,v4_carrier\n")
            for f, t in zip(frames, targets):
                fh.write(f"{f.t:.4f},{f.mode_name},{int(f.a.gate_on)},{f.a.intensity:.4f},{f.a.pulse_rate_hz:.2f},"
                         f"{f.a.pulse_width_us:.0f},{int(f.a.biphasic)},{int(f.b.gate_on)},{f.b.intensity:.4f},"
                         f"{f.b.pulse_rate_hz:.2f},{f.b.pulse_width_us:.0f},{int(f.b.biphasic)},{f.ma_value},"
                         f"{t.api_volume:.4f},{t.pulse_frequency:.2f},{t.pulse_width:.2f},{t.carrier:.0f}\n")

    def rng(sel):
        v = [sel(f) for f in frames]
        return f"{min(v):.3g}..{max(v):.3g}"

    ga = sum(f.a.gate_on for f in frames) / len(frames)
    gb = sum(f.b.gate_on for f in frames) / len(frames)
    print(f"{args.mode}: {len(frames)} ticks, {len(eng.vm.trace.loads)} block loads, MA value {frames[-1].ma_value}")
    print(f"  A: intensity {rng(lambda f: f.a.intensity)}  rate {rng(lambda f: f.a.pulse_rate_hz)} Hz  "
          f"width {rng(lambda f: f.a.pulse_width_us)} us  gate on {ga:.0%}")
    print(f"  B: intensity {rng(lambda f: f.b.intensity)}  rate {rng(lambda f: f.b.pulse_rate_hz)} Hz  "
          f"width {rng(lambda f: f.b.pulse_width_us)} us  gate on {gb:.0%}")
    print(f"  V4: api_volume {min(t.api_volume for t in targets):.3g}..{max(t.api_volume for t in targets):.3g}  "
          f"pulse_frequency {min(t.pulse_frequency for t in targets):.3g}..{max(t.pulse_frequency for t in targets):.3g} Hz")
    print(f"  wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
