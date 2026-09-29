"""Engine daemon: device link + Engine + control API + the player.

    python -m stimengine.tools.serve --serial COM13 --mode threephase
    python -m stimengine.tools.serve --tcp 192.168.1.50 --mode fourphase
    python -m stimengine.tools.serve --serial COM13 --mode threephase --no-signal   # link + API only
    python -m stimengine.tools.serve --sim --mode threephase                          # no box: simulated device
    python -m stimengine.tools.serve --sim-fork --mode fourphase                      # no box: simulated FORK firmware

The player (the ET-312 emulator page, player/) is served by this same process on 127.0.0.1:8322 ([foc312] port /
enabled in config/engine.toml): one process owns the box, the safety stack stays in the engine.

Starts with master volume 0 and NOT armed; arming is an explicit API call (POST /arm). TCP connects retry
with backoff for ~30 s because the box's ESP32 sleeps between sessions and can miss the first attempt.
Ctrl-C: zero -> signal_stop -> close.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import tomllib
from pathlib import Path

from ..control.api import ControlAPI
from ..device import FocStimClient, MemoryTransport, SerialTransport, TcpTransport
from ..device.sim import SimDevice
from ..engine import Engine, EngineError
from ..session import SessionLogger

logger = logging.getLogger("engine.serve")
ROOT = Path(__file__).resolve().parents[2]
RETRY_TOTAL_S = 30.0
RETRY_DELAYS = (1.0, 2.0, 3.0, 5.0, 8.0, 11.0)


def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def make_transport(args: argparse.Namespace):
    if getattr(args, "sim", False) or getattr(args, "sim_fork", False):
        return MemoryTransport()
    if args.serial:
        return SerialTransport(args.serial)
    return TcpTransport(args.tcp, args.port)


async def start_engine_with_retry(args: argparse.Namespace, cfg: dict, session: SessionLogger):
    """Build transport+client+engine and start; TCP retries with backoff, serial tries once."""
    delays = RETRY_DELAYS if args.tcp else ()
    attempt = 0
    while True:
        transport = make_transport(args)
        client = FocStimClient(transport)
        eng = Engine(cfg, client, session)
        if isinstance(transport, MemoryTransport):
            sim = SimDevice(transport, fork=bool(getattr(args, "sim_fork", False)),
                            fork_version=int(getattr(args, "sim_fork_version", 2) or 2))
            sim.start()
            eng.sim = sim  # handle so the fake stops with the engine
        try:
            if args.no_signal:
                await client.connect_and_handshake()
            else:
                await eng.start(args.mode)
            return eng
        except Exception as exc:  # noqa: BLE001
            if attempt >= len(delays):
                raise
            delay = delays[attempt]
            attempt += 1
            logger.warning("connect failed (%s); retry %d in %.0fs", exc, attempt, delay)
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(delay)


async def run(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    from ..et312 import fwdata
    fwdata.set_default(fwdata.load(cfg))          # ET-312 built-in modes: the user's own firmware data, if any
    fw = fwdata.default()
    print("ET-312 built-in modes: " + (f"from {fw.source}" if fw else "not available (no firmware data; see et312/fwdata.py)"))
    session = SessionLogger(ROOT / "sessions")
    stop_requested = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_sigint(*_a):
        print("\nCtrl-C: zeroing and stopping...", file=sys.stderr)
        loop.call_soon_threadsafe(stop_requested.set)

    try:
        loop.add_signal_handler(signal.SIGINT, on_sigint)
    except NotImplementedError:  # Windows
        signal.signal(signal.SIGINT, on_sigint)
        # the app hub (stimengine.app) stops its engine child with CTRL_BREAK (a child in its own process group gets
        # no Ctrl-C): the same clean shutdown
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, on_sigint)

    link = ("SIMULATED box (no hardware)" if args.sim else "SIMULATED fork-firmware box (no hardware)" if args.sim_fork
            else ("serial " + args.serial if args.serial else "tcp " + args.tcp))
    print(f"connecting ({link}) mode={args.mode} ...")
    try:
        eng = await start_engine_with_retry(args, cfg, session)
    except Exception as exc:  # noqa: BLE001
        print(f"start failed: {exc}", file=sys.stderr)
        session.close(f"start failed: {exc}")
        return 2
    t = eng.client.telemetry
    print(f"firmware {t.firmware} board {t.board} 3ph={t.threephase} 4ph={t.fourphase} max={t.max_waveform_amps}A")
    print("signal: " + ("NOT started (--no-signal)" if args.no_signal else f"started, mode={args.mode}, master=0, NOT armed"))

    api = ControlAPI(eng, cfg)
    await api.start()
    print(f"API http://{api.bind}:{api.port}  (GET /status, POST /arm, /volume, /pattern, /lease ...)")
    foc312 = None
    if (cfg.get("foc312") or {}).get("enabled", True):
        try:
            from ..control.foc312_api import Foc312API
            foc312 = Foc312API(eng, cfg, pattern_runner=api.runner)
            await foc312.start()
            print(f"player http://{foc312.bind}:{foc312.port}/")
        except Exception as exc:  # noqa: BLE001 - the ET-312 app is optional; the engine keeps running
            logger.warning("foc312 not started: %s", exc)
            foc312 = None

    print(f"session: {session.dir}")

    rc = 0
    try:
        while not stop_requested.is_set():
            if eng.faulted:
                print(f"FAULT: {eng.fault_reason}", file=sys.stderr)
                rc = 3
                break
            if not args.no_signal and not eng.running:
                print("engine stopped (via API)")
                break
            await asyncio.sleep(0.25)
    finally:
        if foc312 is not None:
            try:
                await foc312.stop()
            except Exception:  # noqa: BLE001
                pass
        await api.stop()
        if eng.running or eng.client.is_open:
            await eng.stop("interrupted" if stop_requested.is_set() else "shutdown")
        elif session.dir:
            session.close("shutdown")
        if getattr(eng, "sim", None) is not None:
            eng.sim.stop()
        print(f"stopped. updates_sent={eng.updates_sent} max_latency={eng.max_latency_s*1000:.1f}ms")
        print(f"session dir: {session.dir}")
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="engine daemon (link + control API)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--serial", metavar="COMx")
    g.add_argument("--tcp", metavar="HOST")
    g.add_argument("--sim", action="store_true", help="no hardware: simulated box (viewer/API dev)")
    g.add_argument("--sim-fork", action="store_true", help="no hardware: simulated box running the fork firmware")
    ap.add_argument("--sim-fork-version", type=int, choices=[1, 2], default=2,
                    help="with --sim-fork: 2 = fractional widths + pulse shapes (default), 1 = the first fork build")
    ap.add_argument("--port", type=int, default=55533)
    ap.add_argument("--mode", choices=["threephase", "fourphase", "biphasic"], default="threephase",
                    help="biphasic = fork firmware OUTPUT_BIPHASIC_PAIRS (foc312 switches modes itself)")
    ap.add_argument("--no-signal", action="store_true", help="open the link and API but do not start the signal")
    ap.add_argument("--config", default=str(ROOT / "config" / "engine.toml"))
    ap.add_argument("-v", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.v else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        return asyncio.run(run(args))
    except EngineError as exc:
        print(f"engine error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
