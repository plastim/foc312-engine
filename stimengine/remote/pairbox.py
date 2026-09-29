"""Point a FOC-Stim's Wi-Fi at the M5 remote's own network, or back at the house Wi-Fi, over the box's USB.

    py -3.13 -m stimengine.remote pair-box --box-port COM17            # join the remote's network ([direct])
    py -3.13 -m stimengine.remote pair-box --box-port COM17 --house    # back to the house Wi-Fi ([wifi])

The box keeps one network in its ESP32's flash. It never starts the signal. The box's ESP32 firmware (diglet48's
FOC-Stim-esp32) gives up on a network after two retries, so on the remote's network: switch the remote on first,
and power-cycle the box if the remote restarts.
"""
from __future__ import annotations

import asyncio

from ..device.client import DeviceError, FocStimClient
from ..device.transport import SerialTransport
from . import m5config


def target_network(m5_cfg: dict, house: bool) -> tuple[str, str]:
    if not house:
        d = m5config.direct_network(m5_cfg)
        if not d:
            raise m5config.ConfigError("config/m5.toml has no [direct] section (the remote's own network)")
        return d["ssid"], d["password"]
    w = m5_cfg.get("wifi") or {}
    if not w.get("ssid"):
        raise m5config.ConfigError("config/m5.toml: [wifi] ssid is missing")
    return str(w["ssid"]), str(w.get("password", ""))


async def pair(port: str, ssid: str, password: str, wait_s: float = 30.0, log=print) -> str:
    client = FocStimClient(SerialTransport(port))
    await client.connect_and_handshake(start_imu=False)
    try:
        log(f"box on {port}: firmware {client.telemetry.firmware}; was on Wi-Fi {await client.wifi_ip_get()}")
        try:
            await client.wifi_parameters_set(ssid.encode(), password.encode())
        except DeviceError as exc:
            # the firmware answers ERROR_UNKNOWN even when it worked (a TODO in stock 1.3.x, kept in the fork)
            if "ERROR_UNKNOWN" not in str(exc):
                raise
        log(f"sent network {ssid!r}; waiting for the box to join ...")
        ip = "0.0.0.0"
        for _ in range(int(wait_s)):
            await asyncio.sleep(1.0)
            ip = await client.wifi_ip_get()
            if ip != "0.0.0.0":
                break
        return ip
    finally:
        await client.close()


def run(port: str, house: bool) -> int:
    ssid, password = target_network(m5config.load_m5(), house)
    ip = asyncio.run(pair(port, ssid, password))
    if ip == "0.0.0.0":
        print("the box has not joined yet" + ("" if house else " - is the remote on and loaded with the [direct] "
                                                                "config? then power-cycle the box"))
        return 1
    print(f"the box joined {ssid!r} at {ip}")
    return 0
