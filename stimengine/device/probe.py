"""Safe probe: connect to the box, handshake, print telemetry for a while, disconnect.

Never starts the signal. Usage:
    python -m stimengine.device.probe --serial COM13 [--seconds 20]
    python -m stimengine.device.probe --tcp 192.168.1.50 [--port 55533] [--seconds 20]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .client import DeviceError, FocStimClient, Telemetry
from .transport import SerialTransport, TcpTransport, TransportError


def _fmt(v: float | None, spec: str = ".2f", unit: str = "") -> str:
    return "-" if v is None else f"{v:{spec}}{unit}"


def telemetry_line(t: Telemetry) -> str:
    parts = []
    if t.battery_voltage is not None or t.battery_soc is not None:
        soc = "-" if t.battery_soc is None else f"{t.battery_soc * 100:.0f}%"
        wall = "" if t.wall_power_present is None else (" wall" if t.wall_power_present else " batt")
        parts.append(f"bat {_fmt(t.battery_voltage, '.2f', 'V')}/{soc}{wall}")
    if t.temp_stm32 is not None:
        parts.append(f"stm32 {_fmt(t.temp_stm32, '.1f', 'C')}")
    if t.v_sys_min is not None:
        parts.append(f"vsys {_fmt(t.v_sys_min)}-{_fmt(t.v_sys_max)}V")
    if t.v_boost_min is not None:
        parts.append(f"vboost {_fmt(t.v_boost_min)}-{_fmt(t.v_boost_max)}V")
    if t.device_volume is not None:
        lock = " (locked)" if t.device_volume_locked else ""
        parts.append(f"devvol {t.device_volume:.2f}{lock}")
    if t.skin_impedance is not None:
        a, b, c, d = t.skin_impedance.real()
        parts.append(f"skinR A{a:.0f} B{b:.0f} C{c:.0f} D{d:.0f}")
    if t.rms is not None:
        parts.append("rms " + " ".join(f"{x:.3f}" for x in t.rms))
    if t.actual_pulse_frequency is not None:
        parts.append(f"pulse {t.actual_pulse_frequency:.1f}Hz")
    if t.acc is not None:
        parts.append("acc " + " ".join(f"{x:+.1f}" for x in t.acc))
    if t.pressure is not None:
        parts.append(f"press {t.pressure:.2f}")
    if t.button is not None:
        parts.append(f"btn {t.button}")
    return "  ".join(parts) if parts else "(no telemetry yet)"


async def run(args: argparse.Namespace) -> int:
    if args.serial:
        transport = SerialTransport(args.serial)
    else:
        transport = TcpTransport(args.tcp, args.port)
    client = FocStimClient(transport)
    print(f"connecting via {transport.name} ...")
    try:
        await client.connect_and_handshake(start_imu=not args.no_imu)
    except (TransportError, DeviceError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    t = client.telemetry
    print(f"firmware {t.firmware}  board {t.board}")
    print(
        f"capabilities: threephase={t.threephase} fourphase={t.fourphase} battery={t.battery_capable} "
        f"device_volume={t.device_volume_capable} max_amps={t.max_waveform_amps} imu={t.imu_capable}"
    )
    if args.tcp is None:
        try:
            print(f"box wifi ip: {await client.wifi_ip_get()}")
        except DeviceError as exc:
            print(f"wifi ip: {exc}")
    print(f"listening {args.seconds}s (signal NOT started) ...")
    rc = 0
    try:
        for _ in range(args.seconds):
            await asyncio.sleep(1.0)
            if client.closed.is_set():
                print(f"link dropped: {client.closed_reason}", file=sys.stderr)
                rc = 3
                break
            print(telemetry_line(t))
    finally:
        await client.close()
        print(f"disconnected  (in {transport.bytes_in} B, out {transport.bytes_out} B)")
    return rc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="FOC-Stim probe (read-only; never starts the signal)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--serial", metavar="COMx", help="USB serial port, e.g. COM13")
    g.add_argument("--tcp", metavar="HOST", help="box WiFi address, e.g. 192.168.1.50")
    p.add_argument("--port", type=int, default=55533, help="TCP port (default 55533)")
    p.add_argument("--seconds", type=int, default=20, help="how long to listen (default 20)")
    p.add_argument("--no-imu", action="store_true", help="do not start the IMU stream")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
