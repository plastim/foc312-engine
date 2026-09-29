"""TP-5 sweep: start the signal, run a deterministic parameter sweep, log everything, stop cleanly.

    python -m stimengine.tools.sweep --serial COM13 --mode threephase
    python -m stimengine.tools.sweep --tcp 192.168.1.50 --mode fourphase --master 0.0

Master volume defaults to 0.0 and must be raised explicitly. The engine's caps (config + 0.2 A hard cap)
apply regardless. Electrodes must be connected to NOTHING during TP-5. Ctrl-C = volume 0 + signal_stop.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import signal
import sys
import tomllib
from pathlib import Path

from ..device import FocStimClient, SerialTransport, TcpTransport
from ..engine import Engine, EngineError
from ..session import SessionLogger

SWEEP_S = 60.0
PRINT_EVERY_S = 1.0
CONTROL_HZ = 30

ROOT = Path(__file__).resolve().parents[2]


def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def fmt4(v) -> str:
    return "-" if v is None else "/".join(f"{x:.3f}" for x in v)


def fmtz(z) -> str:
    return "-" if z is None else "/".join(f"{x:.0f}" for x in z.real())


def telemetry_line(t_s: float, eng: Engine) -> str:
    t = eng.client.telemetry
    st = eng.status()
    return (
        f"t={t_s:5.1f}s amps={st['amps_commanded'] if st['amps_commanded'] is not None else 0:.4f} "
        f"rmsA-D={fmt4(t.rms)} pulse={t.actual_pulse_frequency if t.actual_pulse_frequency is not None else float('nan'):.1f}Hz "
        f"vdrive={t.v_drive if t.v_drive is not None else float('nan'):.2f} "
        f"skinR={fmtz(t.skin_impedance)} outR={fmtz(t.output_impedance)} "
        f"bat={t.battery_voltage if t.battery_voltage is not None else float('nan'):.2f}V "
        f"pend={st['pending']} sent={st['updates_sent']}"
    )


def sweep_values(t_s: float, mode: str) -> dict:
    """Deterministic 60 s program: geometry loops, carrier/pulse/width ramp once across the sweep."""
    p = min(1.0, max(0.0, t_s / SWEEP_S))
    out = {
        "carrier": 500.0 + 500.0 * p,     # 500 -> 1000 Hz
        "pulse_freq": 20.0 + 130.0 * p,   # 20 -> 150 Hz
        "pulse_width": 4.0 + 6.0 * p,     # 4 -> 10 cycles
    }
    if mode == "threephase":
        ang = 2 * math.pi * (t_s / 10.0)  # one circle every 10 s, radius 0.8
        out["alpha"], out["beta"] = 0.8 * math.cos(ang), 0.8 * math.sin(ang)
    else:
        idx = int(t_s / 2.5) % 4          # round-robin, 2.5 s per electrode, soft crossfade
        frac = (t_s % 2.5) / 2.5
        e = [0.0, 0.0, 0.0, 0.0]
        e[idx] = 1.0 - 0.5 * frac
        e[(idx + 1) % 4] = 0.5 * frac
        out["e"] = e
    return out


async def run(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    if args.master > 0 and cfg["signal"]["waveform_amplitude_amps"] > 0.2:
        print("refusing: config amps cap > 0.2", file=sys.stderr)
        return 2
    transport = SerialTransport(args.serial) if args.serial else TcpTransport(args.tcp, args.port)
    client = FocStimClient(transport)
    session = SessionLogger(ROOT / "sessions")
    eng = Engine(cfg, client, session)

    stop_requested = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_sigint(*_a):
        print("\nCtrl-C: zeroing and stopping...", file=sys.stderr)
        stop_requested.set()

    try:
        loop.add_signal_handler(signal.SIGINT, on_sigint)
    except NotImplementedError:  # Windows
        signal.signal(signal.SIGINT, lambda *_a: loop.call_soon_threadsafe(stop_requested.set))

    print(f"connecting via {transport.name}, mode={args.mode}, master={args.master} ...")
    try:
        await eng.start(args.mode)
    except Exception as exc:  # noqa: BLE001
        print(f"start failed: {exc}", file=sys.stderr)
        session.close(f"start failed: {exc}")
        return 2
    t = client.telemetry
    print(f"firmware {t.firmware} board {t.board} caps 3ph={t.threephase} 4ph={t.fourphase} max={t.max_waveform_amps}A")
    print(f"signal started. session: {session.dir}")

    eng.set_master(args.master, source="sweep")
    eng.arm()
    t0 = loop.time()
    next_print = 0.0
    rc = 0
    try:
        while not stop_requested.is_set():
            el = loop.time() - t0
            if el >= SWEEP_S:
                break
            if eng.faulted:
                print(f"FAULT: {eng.fault_reason}", file=sys.stderr)
                rc = 3
                break
            v = sweep_values(el, args.mode)
            eng.set_carrier(v["carrier"], source="sweep")
            eng.set_pulse(frequency=v["pulse_freq"], width=v["pulse_width"], source="sweep")
            if args.mode == "threephase":
                eng.set_position(v["alpha"], v["beta"], source="sweep")
            else:
                eng.set_vector(*v["e"], source="sweep")
            eng.renew_lease("sweep")
            if el >= next_print:
                print(telemetry_line(el, eng))
                next_print += PRINT_EVERY_S
            await asyncio.sleep(1.0 / CONTROL_HZ)
    finally:
        await eng.stop("sweep complete" if rc == 0 and not stop_requested.is_set() else "interrupted")
        print(f"stopped. updates_sent={eng.updates_sent} max_latency={eng.max_latency_s*1000:.1f}ms")
        print(f"session dir: {session.dir}")
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="engine TP-5 sweep (signal ON; electrodes connected to nothing)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--serial", metavar="COMx")
    g.add_argument("--tcp", metavar="HOST")
    ap.add_argument("--port", type=int, default=55533)
    ap.add_argument("--mode", choices=["threephase", "fourphase"], required=True)
    ap.add_argument("--master", type=float, default=0.0, help="master volume 0..1 (default 0.0)")
    ap.add_argument("--config", default=str(ROOT / "config" / "engine.toml"))
    ap.add_argument("-v", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.v else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if not 0.0 <= args.master <= 1.0:
        ap.error("--master must be 0..1")
    try:
        return asyncio.run(run(args))
    except EngineError as exc:
        print(f"engine error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
