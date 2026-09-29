"""Soak: latency/stability characterization of the link, NO body contact.

    python -m stimengine.tools.soak --serial COM13 --mode threephase --minutes 10
    python -m stimengine.tools.soak --tcp 192.168.1.50 --mode fourphase --minutes 5 [--master 0.0]

Runs a slow, gentle pattern at master 0.0 (explicit --master to raise; refuses config caps > 0.2 A) and
measures per-axis-move round-trip latency (p50/p90/p99/max, counts over 50/100/250/500 ms), pending-queue
depth, notification gaps per kind, and link drops/faults. Prints a per-minute line, a summary, and writes
soak.json into the session dir. Latency samples are also logged as 'latency' command records so
session_report can show them. Electrodes must be connected to NOTHING. Ctrl-C = volume 0 + signal_stop.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import signal
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

import numpy as np

from ..analysis.session import percentiles
from ..device import FocStimClient, SerialTransport, TcpTransport
from ..engine import Engine, EngineError
from ..session import SessionLogger

ROOT = Path(__file__).resolve().parents[2]
CONTROL_HZ = 30
SAMPLE_HZ = 20  # pending-depth sampling
THRESHOLDS_MS = (50, 100, 250, 500)


class SoakStats:
    """Accumulates latency, pending depth and notification-gap statistics."""

    def __init__(self) -> None:
        self.latency_ms: list[float] = []
        self.minute_latency_ms: list[float] = []
        self.pending: list[int] = []
        self.minute_pending: list[int] = []
        self.last_seen: dict[str, float] = {}
        self.max_gap: dict[str, float] = {}
        self.notifications = 0
        self.minute_notifications = 0
        self.link_drops = 0
        self.faults: list[str] = []
        self.minutes: list[dict[str, Any]] = []

    def on_latency(self, axis: int, lat_s: float) -> None:
        ms = lat_s * 1000.0
        self.latency_ms.append(ms)
        self.minute_latency_ms.append(ms)

    def on_notification(self, kind: str, now: float) -> None:
        self.notifications += 1
        self.minute_notifications += 1
        prev = self.last_seen.get(kind)
        if prev is not None:
            gap = now - prev
            if gap > self.max_gap.get(kind, 0.0):
                self.max_gap[kind] = gap
        self.last_seen[kind] = now

    def sample_pending(self, depth: int) -> None:
        self.pending.append(depth)
        self.minute_pending.append(depth)

    def close_minute(self, minute: int) -> dict[str, Any]:
        lat = percentiles(self.minute_latency_ms, THRESHOLDS_MS)
        pend = np.asarray(self.minute_pending or [0])
        row = {
            "minute": minute,
            "latency": lat,
            "pending_mean": float(pend.mean()),
            "pending_max": int(pend.max()),
            "notifications": self.minute_notifications,
        }
        self.minutes.append(row)
        self.minute_latency_ms = []
        self.minute_pending = []
        self.minute_notifications = 0
        return row

    def summary(self) -> dict[str, Any]:
        pend = np.asarray(self.pending or [0])
        return {
            "latency": percentiles(self.latency_ms, THRESHOLDS_MS),
            "pending": {"mean": float(pend.mean()), "p90": float(np.percentile(pend, 90)), "max": int(pend.max())},
            "notifications": self.notifications,
            "max_gap_s": {k: round(v, 3) for k, v in sorted(self.max_gap.items(), key=lambda kv: -kv[1])},
            "link_drops": self.link_drops,
            "faults": list(self.faults),
            "minutes": self.minutes,
        }


def fmt_lat(L: dict[str, Any]) -> str:
    if not L.get("n"):
        return "lat: n=0"
    over = "/".join(str(L["over"][str(t)]) for t in THRESHOLDS_MS)
    return (f"lat n={L['n']} p50={L['p50']:.1f} p90={L['p90']:.1f} p99={L['p99']:.1f} "
            f"max={L['max']:.1f}ms over50/100/250/500={over}")


def pattern_values(t_s: float, mode: str) -> dict:
    """Slow gentle circle (one lap / 20 s, r=0.5) or a slow 4-phase crossfade."""
    if mode == "threephase":
        ang = 2 * math.pi * (t_s / 20.0)
        return {"alpha": 0.5 * math.cos(ang), "beta": 0.5 * math.sin(ang)}
    idx = int(t_s / 5.0) % 4
    frac = (t_s % 5.0) / 5.0
    e = [0.0, 0.0, 0.0, 0.0]
    e[idx] = 1.0 - 0.5 * frac
    e[(idx + 1) % 4] = 0.5 * frac
    return {"e": e}


def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


async def run(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    if cfg["signal"]["waveform_amplitude_amps"] > 0.2:
        print("refusing: config amps cap > 0.2 A", file=sys.stderr)
        return 2
    transport = SerialTransport(args.serial) if args.serial else TcpTransport(args.tcp, args.port)
    client = FocStimClient(transport)
    session = SessionLogger(ROOT / "sessions")
    eng = Engine(cfg, client, session)
    stats = SoakStats()
    eng.on_move_latency.append(stats.on_latency)
    loop = asyncio.get_running_loop()

    def on_any(kind: str, _msg: Any = None) -> None:
        stats.on_notification(kind, loop.time())

    try:
        client.on("any", on_any)
    except Exception:  # noqa: BLE001 - best effort; gaps just won't be tracked
        logging.getLogger("soak").warning("could not subscribe to notifications; gaps not tracked")

    stop_requested = asyncio.Event()

    def on_sigint(*_a):
        print("\nCtrl-C: zeroing and stopping...", file=sys.stderr)
        stop_requested.set()

    try:
        loop.add_signal_handler(signal.SIGINT, on_sigint)
    except NotImplementedError:  # Windows
        signal.signal(signal.SIGINT, lambda *_a: loop.call_soon_threadsafe(stop_requested.set))

    print(f"soak via {transport.name}, mode={args.mode}, minutes={args.minutes}, master={args.master}")
    try:
        await eng.start(args.mode)
    except Exception as exc:  # noqa: BLE001
        print(f"start failed: {exc}", file=sys.stderr)
        session.close(f"start failed: {exc}")
        return 2
    t = client.telemetry
    print(f"firmware {t.firmware} board {t.board}; session {session.dir}")
    eng.set_master(args.master, source="soak")
    eng.arm()

    t0 = loop.time()
    total_s = args.minutes * 60.0
    next_sample = 0.0
    next_log_flush = 0.0
    minute = 0
    rc = 0
    lat_logged = 0
    try:
        while not stop_requested.is_set():
            el = loop.time() - t0
            if el >= total_s:
                break
            if eng.faulted:
                stats.faults.append(str(eng.fault_reason))
                print(f"FAULT: {eng.fault_reason}", file=sys.stderr)
                rc = 3
                break
            if not client.is_open:
                stats.link_drops += 1
                print("link dropped", file=sys.stderr)
                rc = 3
                break
            v = pattern_values(el, args.mode)
            if args.mode == "threephase":
                eng.set_position(v["alpha"], v["beta"], source="soak")
            else:
                eng.set_vector(*v["e"], source="soak")
            eng.renew_lease("soak")
            if el >= next_sample:
                stats.sample_pending(client.pending_count)
                next_sample += 1.0 / SAMPLE_HZ
            if el >= next_log_flush:
                # persist new latency samples to the session so session_report can analyse them
                for ms in stats.latency_ms[lat_logged:]:
                    session.log_command("latency", source="soak", latency_ms=round(ms, 2))
                lat_logged = len(stats.latency_ms)
                next_log_flush += 5.0
            if el >= (minute + 1) * 60.0:
                minute += 1
                row = stats.close_minute(minute)
                print(f"min {minute:3d}: {fmt_lat(row['latency'])} pend mean={row['pending_mean']:.1f} "
                      f"max={row['pending_max']} notif={row['notifications']}")
            await asyncio.sleep(1.0 / CONTROL_HZ)
    finally:
        for ms in stats.latency_ms[lat_logged:]:
            session.log_command("latency", source="soak", latency_ms=round(ms, 2))
        if stats.minute_latency_ms or stats.minute_pending:
            stats.close_minute(minute + 1)
        summary = stats.summary()
        summary.update({
            "transport": transport.name, "mode": args.mode, "master": args.master,
            "planned_minutes": args.minutes, "elapsed_s": round(loop.time() - t0, 1),
            "updates_sent": eng.updates_sent, "engine_max_latency_ms": round(eng.max_latency_s * 1000, 1),
            "interrupted": stop_requested.is_set(), "rc": rc,
        })
        await eng.stop("soak complete" if rc == 0 and not stop_requested.is_set() else "interrupted")
        try:
            (session.dir / "soak.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
        except OSError as exc:
            print(f"could not write soak.json: {exc}", file=sys.stderr)
        print("\n== soak summary ==")
        print(fmt_lat(summary["latency"]))
        p = summary["pending"]
        print(f"pending: mean={p['mean']:.2f} p90={p['p90']:.0f} max={p['max']}")
        gaps = list(summary["max_gap_s"].items())[:6]
        print("longest notification gaps: " + ", ".join(f"{k}={v:.2f}s" for k, v in gaps))
        print(f"link drops={summary['link_drops']} faults={summary['faults']} updates_sent={eng.updates_sent}")
        print(f"session dir: {session.dir}")
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="engine soak (signal ON at master 0; electrodes connected to nothing)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--serial", metavar="COMx")
    g.add_argument("--tcp", metavar="HOST")
    ap.add_argument("--port", type=int, default=55533)
    ap.add_argument("--mode", choices=["threephase", "fourphase"], required=True)
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--master", type=float, default=0.0, help="master volume 0..1 (default 0.0)")
    ap.add_argument("--config", default=str(ROOT / "config" / "engine.toml"))
    ap.add_argument("-v", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.v else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if not 0.0 <= args.master <= 1.0:
        ap.error("--master must be 0..1")
    if args.minutes <= 0:
        ap.error("--minutes must be > 0")
    try:
        return asyncio.run(run(args))
    except EngineError as exc:
        print(f"engine error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
