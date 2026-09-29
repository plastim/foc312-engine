"""Compact Markdown report for one engine session.

    python -m stimengine.tools.session_report <session_dir|latest> [--md out.md] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

from ..analysis.session import ELECTRODES, Series, Session, bucketize, load_session, sparkline

ROOT = Path(__file__).resolve().parents[2]
BUCKETS = 60


def _f(v, nd=3, unit="") -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "-"
    return f"{v:.{nd}f}{unit}"


def _stat_row(name: str, st: dict, nd=3, unit="") -> str:
    if not st or not st.get("n"):
        return f"| {name} | 0 | - | - | - | - |"
    return (f"| {name} | {st['n']} | {_f(st['min'], nd, unit)} | {_f(st['mean'], nd, unit)} | "
            f"{_f(st['max'], nd, unit)} | {_f(st['last'], nd, unit)} |")


def _spark(label: str, s: Series, dur: float, nd=3, unit="", how="mean") -> str:
    b = bucketize(s, dur, BUCKETS, how=how)
    fin = b[np.isfinite(b)]
    if fin.size == 0:
        return f"`{label:<14}` (no data)"
    lo, hi = float(fin.min()), float(fin.max())
    if hi - lo <= 1e-3 * max(abs(hi), 1e-12):  # flat within 0.1 %: don't amplify float noise into a curve
        lo = hi
    return f"`{label:<14}` `{sparkline(b, lo, hi)}` {_f(lo, nd, unit)} .. {_f(hi, nd, unit)}"


def render_markdown(sess: Session) -> str:
    s = sess.summary()
    dur = sess.duration_s
    out: list[str] = []
    out.append(f"# Session {sess.dir.name}")
    out.append("")
    out.append(f"- **when:** {s['started']} -> {s['ended'] or 'open'}  ({s['duration_s']} s)")
    out.append(f"- **transport:** {s['transport']}  **mode:** {s['mode']}  **firmware:** {s['firmware']}  "
               f"**board:** {s['board'] or '?'}")
    end = s["end_reason"]
    flag = " **FAULT**" if s["faulted"] else ""
    out.append(f"- **end:** {end}{flag}")
    out.append(f"- **max amps commanded:** {_f(s['max_amps_commanded'], 4, ' A')}  "
               f"**updates sent:** {s['updates_sent'] if s['updates_sent'] is not None else '-'}  "
               f"**max axis latency:** {_f(s['max_latency_ms'], 1, ' ms')}")
    contact = ", ".join(f"{e.upper()}={s['contact'][e]}" for e in ELECTRODES)
    out.append(f"- **contact (from skin R):** {contact}")
    if "latency_ms" in s:
        L = s["latency_ms"]
        out.append(f"- **axis-move latency:** p50 {_f(L['p50'], 1)} / p90 {_f(L['p90'], 1)} / p99 {_f(L['p99'], 1)} / "
                   f"max {_f(L['max'], 1)} ms  over 50/100/250/500 ms: "
                   + "/".join(str(L["over"][k]) for k in ("50", "100", "250", "500")))
    out.append("")

    out.append("## Events")
    out.append("")
    ev = s["events"]
    if not ev:
        out.append("_none_")
    else:
        out.append("| t (s) | event | source | detail |")
        out.append("|---:|---|---|---|")
        for e in ev:
            det = ", ".join(f"{k}={v}" for k, v in e["detail"].items()) if e["detail"] else ""
            out.append(f"| {e['t']:.2f} | {e['kind']} | {e['source'] or ''} | {det} |")
    out.append("")

    out.append("## Telemetry")
    out.append("")
    out.append("| series | n | min | mean | max | last |")
    out.append("|---|---:|---:|---:|---:|---:|")
    out.append(_stat_row("amps commanded (A)", s["amps_commanded"], 4))
    for e in ELECTRODES:
        out.append(_stat_row(f"rms current {e.upper()} (A)", s["rms_current"][e], 5))
    for e in ELECTRODES:
        out.append(_stat_row(f"skin R {e.upper()} (ohm)", s["skin_resistance"][e], 1))
    for e in ELECTRODES:
        out.append(_stat_row(f"output R {e.upper()} (ohm)", s["output_resistance"][e], 2))
    out.append(_stat_row("actual pulse (Hz)", s["actual_pulse_hz"], 1))
    out.append(_stat_row("v_drive (V)", s["v_drive"], 2))
    out.append(_stat_row("battery (V)", s["battery_v"], 3))
    out.append(_stat_row("battery SoC", s["battery_soc"], 2))
    out.append(_stat_row("device volume", s["device_volume"], 2))
    out.append(_stat_row("stm32 temp (C)", s["temp_stm32"], 1))
    out.append("")

    out.append(f"## Sparklines ({BUCKETS} buckets over {dur:.0f} s)")
    out.append("")
    out.append(_spark("amps cmd", sess.amps_commanded(), dur, 4, " A", how="max"))
    rms = sess.rms_currents()
    rms_all = Series(
        np.concatenate([rms[e].t for e in ELECTRODES]) if any(len(rms[e]) for e in ELECTRODES) else np.array([]),
        np.concatenate([rms[e].v for e in ELECTRODES]) if any(len(rms[e]) for e in ELECTRODES) else np.array([]),
    )
    out.append(_spark("rms (max ch)", rms_all, dur, 5, " A", how="max"))
    out.append(_spark("pulse Hz", sess.actual_pulse_frequency(), dur, 1, " Hz"))
    out.append(_spark("v_drive", sess.v_drive(), dur, 2, " V"))
    skin = sess.skin_resistance()
    for e in ELECTRODES:
        out.append(_spark(f"skin R {e.upper()}", skin[e], dur, 0, " ohm"))
    out.append(_spark("battery V", sess.battery_voltage(), dur, 3, " V"))
    out.append("")

    out.append("## Notifications")
    out.append("")
    out.append("| kind | count | rate (/s) | max gap (s) |")
    out.append("|---|---:|---:|---:|")
    for k, r in s["notifications"].items():
        out.append(f"| {k} | {r['count']} | {r['rate_hz']:.1f} | {_f(r['max_gap_s'], 2)} |")
    out.append("")
    out.append("## Commands")
    out.append("")
    out.append(", ".join(f"{k}: {n}" for k, n in s["commands"].items()) or "_none_")
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="engine session report")
    ap.add_argument("session", help="session dir or 'latest'")
    ap.add_argument("--md", help="write Markdown to this file")
    ap.add_argument("--json", help="write the summary dict as JSON to this file")
    ap.add_argument("--root", default=str(ROOT / "sessions"), help="sessions root for 'latest'")
    args = ap.parse_args(argv)
    try:
        sess = load_session(args.session, root=args.root)
    except FileNotFoundError as exc:
        print(f"not found: {exc}", file=sys.stderr)
        return 2
    md = render_markdown(sess)
    if args.md:
        Path(args.md).write_text(md, encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(sess.summary(), indent=1, default=str), encoding="utf-8")
    if not args.md:
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # sparkline glyphs on Windows consoles
        except Exception:  # noqa: BLE001
            pass
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
