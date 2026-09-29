"""Asyncio RPC client for the FOC-Stim (protobuf over HDLC, serial or TCP).

Ported from restim's `device/focstim/proto_api.py` + the startup sequence in `proto_device.py`,
minus Qt. Requests are correlated by id with asyncio Futures; notifications fan out to callbacks
and into a `Telemetry` snapshot.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import numpy as np
from google.protobuf.message import DecodeError
from google.protobuf import text_format

from . import hdlc
from .proto.constants_pb2 import AxisType, OutputMode
from .proto.focstim_rpc_pb2 import Notification, Request, Response, RpcMessage
from .proto.messages_pb2 import (
    RequestAxisMoveTo,
    RequestAxisSet,
    RequestCapabilitiesGet,
    RequestFirmwareVersion,
    RequestLockDeviceVolume,
    RequestLSM6DSOXStart,
    RequestLSM6DSOXStop,
    RequestSignalStart,
    RequestSignalStop,
    RequestTimestampGet,
    RequestTimestampSet,
    RequestWifiIPGet,
    RequestWifiParametersSet,
    ResponseCapabilitiesGet,
    ResponseFirmwareVersion,
)
from .transport import BaseTransport, TransportError

logger = logging.getLogger("engine.device.client")

# Same compatibility gate restim applies.
FIRMWARE_MAJOR = 1
FIRMWARE_MIN_MINOR = 1
FIRMWARE_BRANCH = "main"

TIMEOUT_SETUP_S = 2.0
TIMEOUT_UPDATE_S = 4.0

LSM6DSOX_SAMPLERATE_HZ = 104
LSM6DSOX_ACC_FULLSCALE = 4
LSM6DSOX_GYR_FULLSCALE = 500
_MILLI_G_TO_MS2 = 0.001 * 9.80
_MILLI_DPS_TO_RADS = 1 / 360 / 1000 * (2 * np.pi)

# Notification oneof field name -> short event name used for callbacks.
NOTIFICATION_FIELDS: dict[str, str] = {
    "notification_boot": "boot",
    "notification_device_volume": "device_volume",
    "notification_currents": "currents",
    "notification_output_resistance": "output_resistance",
    "notification_skin_resistance": "skin_resistance",
    "notification_system_stats": "system_stats",
    "notification_signal_stats": "signal_stats",
    "notification_battery": "battery",
    "notification_lsm6dsox": "imu",
    "notification_pressure": "pressure",
    "notification_button_press": "button",
    "notification_debug_string": "debug_string",
    "notification_debug_as5311": "debug_as5311",
    "notification_debug_edging": "debug_edging",
    "notification_debug_teleplot": "debug_teleplot",
}

NotificationCallback = Callable[[Any], None | Awaitable[None]]


class DeviceError(Exception):
    """Protocol-level failure (timeout, firmware error response, incompatible firmware)."""


class DeviceRebooted(DeviceError):
    """The box sent a boot notification mid-session (restim treats this as fatal)."""


@dataclass
class Complex4:
    """Per-electrode complex impedance (resistance + j*reluctance), electrodes A-D."""

    a: complex = 0j
    b: complex = 0j
    c: complex = 0j
    d: complex = 0j

    def real(self) -> tuple[float, float, float, float]:
        return (self.a.real, self.b.real, self.c.real, self.d.real)


@dataclass
class Telemetry:
    """Latest value of everything the box reports. Timestamps are `time.monotonic()` at receipt."""

    firmware: str = ""
    board: str = ""
    threephase: bool = False
    fourphase: bool = False
    battery_capable: bool = False
    device_volume_capable: bool = False
    max_waveform_amps: float = 0.0
    imu_capable: bool = False

    device_volume: float | None = None
    device_volume_locked: bool | None = None

    rms: tuple[float, float, float, float] | None = None
    peak: tuple[float, float, float, float] | None = None
    peak_cmd: float | None = None
    output_power: float | None = None
    output_power_skin: float | None = None

    output_impedance: Complex4 | None = None
    skin_impedance: Complex4 | None = None

    temp_stm32: float | None = None
    temp_board: float | None = None
    v_bus: float | None = None
    v_sys_min: float | None = None
    v_sys_max: float | None = None
    v_boost_min: float | None = None
    v_boost_max: float | None = None
    boost_duty_cycle: float | None = None

    actual_pulse_frequency: float | None = None
    v_drive: float | None = None
    transformer_utilization: float | None = None
    voltage_utilization: float | None = None

    battery_voltage: float | None = None
    battery_charge_rate_watt: float | None = None
    battery_soc: float | None = None
    wall_power_present: bool | None = None
    battery_chip_temperature: float | None = None

    acc: tuple[float, float, float] | None = None  # m/s^2
    gyr: tuple[float, float, float] | None = None  # rad/s
    pressure: float | None = None
    button: str | None = None
    last_debug_string: str | None = None

    last_update: dict[str, float] = field(default_factory=dict)

    def touch(self, kind: str) -> None:
        self.last_update[kind] = time.monotonic()


class FocStimClient:
    """One FOC-Stim link. Create with a transport, `await connect_and_handshake()`, then use the typed requests."""

    def __init__(self, transport: BaseTransport) -> None:
        self.transport = transport
        self.telemetry = Telemetry()
        self._decoder = hdlc.HDLCDecoder()
        self._request_id = random.randint(1, 4096)
        self._pending: dict[int, asyncio.Future[Response]] = {}
        self._callbacks: dict[str, list[NotificationCallback]] = {k: [] for k in NOTIFICATION_FIELDS.values()}
        self._callbacks["any"] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self.closed_reason: Exception | None = None
        self.closed = asyncio.Event()
        self._acc_sensitivity = 0.0
        self._gyr_sensitivity = 0.0
        self.capabilities: ResponseCapabilitiesGet | None = None
        self.firmware: ResponseFirmwareVersion | None = None

    # ---- lifecycle -----------------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the transport only (no requests sent)."""
        self._loop = asyncio.get_running_loop()
        await self.transport.connect(self._on_bytes, self._on_transport_closed)
        logger.info("connection established (%s)", self.transport.name)

    async def connect_and_handshake(self, start_imu: bool = True) -> None:
        """Open the link and run restim's startup sequence up to (not including) signal start.

        firmware version (compat check) -> capabilities -> IMU stream start (if present and requested).
        """
        await self.connect()
        try:
            fw = await self.firmware_version()
            ver = fw.stm32_firmware_version_2
            self.telemetry.firmware = f"{ver.major}.{ver.minor}.{ver.revision} ({ver.branch})"
            self.telemetry.board = fw.DESCRIPTOR.fields_by_name["board"].enum_type.values_by_number[fw.board].name
            logger.info("firmware: %s", text_format.MessageToString(fw, as_one_line=True))
            if ver.branch != FIRMWARE_BRANCH:
                raise DeviceError(f"incompatible firmware branch {ver.branch!r}, expected {FIRMWARE_BRANCH!r}")
            if not (ver.major == FIRMWARE_MAJOR and ver.minor >= FIRMWARE_MIN_MINOR):
                raise DeviceError(
                    f"incompatible firmware {ver.major}.{ver.minor}.{ver.revision}, "
                    f"needs >= {FIRMWARE_MAJOR}.{FIRMWARE_MIN_MINOR}.0"
                )
            caps = await self.capabilities_get()
            logger.info("capabilities: %s", text_format.MessageToString(caps, as_one_line=True))
            t = self.telemetry
            t.threephase, t.fourphase = caps.threephase, caps.fourphase
            t.battery_capable, t.device_volume_capable = caps.battery, caps.device_volume
            t.max_waveform_amps, t.imu_capable = caps.maximum_waveform_amplitude_amps, caps.lsm6dsox
            if caps.lsm6dsox and start_imu:
                await self.lsm6dsox_start()
        except BaseException:
            await self.close()
            raise

    async def close(self, stop_signal: bool = False) -> None:
        """Close the link. With `stop_signal`, send signal_stop first (best effort)."""
        if stop_signal and self.transport.is_open:
            try:
                await self.signal_stop(timeout=1.0)
            except Exception:  # noqa: BLE001 - best effort on the way out
                pass
        self._cancel_pending(DeviceError("connection closed"))
        await self.transport.drain()
        await self.transport.close()
        self.closed.set()

    @property
    def is_open(self) -> bool:
        return self.transport.is_open

    # ---- notifications -------------------------------------------------------------------------

    def on(self, kind: str, callback: NotificationCallback) -> None:
        """Register a callback for a notification kind (see NOTIFICATION_FIELDS values) or 'any'."""
        if kind not in self._callbacks:
            raise KeyError(f"unknown notification kind {kind!r}")
        self._callbacks[kind].append(callback)

    def off(self, kind: str, callback: NotificationCallback) -> None:
        self._callbacks[kind].remove(callback)

    # ---- typed requests ------------------------------------------------------------------------

    async def firmware_version(self, timeout: float = TIMEOUT_SETUP_S) -> ResponseFirmwareVersion:
        r = await self._request(Request(request_firmware_version=RequestFirmwareVersion()), timeout)
        self.firmware = r.response_firmware_version
        return self.firmware

    async def capabilities_get(self, timeout: float = TIMEOUT_SETUP_S) -> ResponseCapabilitiesGet:
        r = await self._request(Request(request_capabilities_get=RequestCapabilitiesGet()), timeout)
        self.capabilities = r.response_capabilities_get
        return self.capabilities

    async def signal_start(self, mode: int, timeout: float = TIMEOUT_SETUP_S) -> Response:
        """Start output. `mode` is an OutputMode value (THREEPHASE or FOURPHASE_INDIVIDUAL_ELECTRODES)."""
        return await self._request(Request(request_signal_start=RequestSignalStart(mode=mode)), timeout)

    async def signal_stop(self, timeout: float = TIMEOUT_SETUP_S) -> Response:
        return await self._request(Request(request_signal_stop=RequestSignalStop()), timeout)

    async def axis_move_to(
        self, axis: int, value: float, interval_ms: int = 30, timeout: float = TIMEOUT_UPDATE_S
    ) -> Response:
        """Glide an axis to `value` over `interval_ms` (restim sends these at 60 Hz with interval 30)."""
        req = Request(request_axis_move_to=RequestAxisMoveTo(axis=axis, value=float(value), interval=int(interval_ms)))
        return await self._request(req, timeout)

    def axis_move_to_nowait(self, axis: int, value: float, interval_ms: int = 30) -> asyncio.Future[Response]:
        """Fire an axis update without awaiting; the returned future resolves on the device ack."""
        req = Request(request_axis_move_to=RequestAxisMoveTo(axis=axis, value=float(value), interval=int(interval_ms)))
        return self._send(req, TIMEOUT_UPDATE_S)

    async def axis_set(
        self, axis: int, value: float, timestamp_ms: int, clear: bool = False, timeout: float = TIMEOUT_UPDATE_S
    ) -> Response:
        """Timestamped axis set (buffered streaming mode; unused by restim, kept for synced playback work)."""
        req = Request(
            request_axis_set=RequestAxisSet(axis=axis, value=float(value), timestamp_ms=int(timestamp_ms), clear=clear)
        )
        return await self._request(req, timeout)

    async def timestamp_set(self, timestamp_ms: int | None = None, timeout: float = TIMEOUT_SETUP_S) -> Response:
        ts = int(time.time_ns() // 1_000_000) if timestamp_ms is None else int(timestamp_ms)
        return await self._request(Request(request_timestamp_set=RequestTimestampSet(timestamp_ms=ts)), timeout)

    async def timestamp_get(self, timeout: float = TIMEOUT_SETUP_S) -> Response:
        return await self._request(Request(request_timestamp_get=RequestTimestampGet()), timeout)

    async def lock_device_volume(self, locked: bool, timeout: float = TIMEOUT_SETUP_S) -> Response:
        return await self._request(Request(request_lock_device_volume=RequestLockDeviceVolume(lock=locked)), timeout)

    async def lsm6dsox_start(
        self,
        samplerate_hz: float = LSM6DSOX_SAMPLERATE_HZ,
        acc_fullscale: float = LSM6DSOX_ACC_FULLSCALE,
        gyr_fullscale: float = LSM6DSOX_GYR_FULLSCALE,
        timeout: float = TIMEOUT_SETUP_S,
    ) -> Response:
        req = Request(
            request_lsm6dsox_start=RequestLSM6DSOXStart(
                imu_samplerate=samplerate_hz, acc_fullscale=acc_fullscale, gyr_fullscale=gyr_fullscale
            )
        )
        r = await self._request(req, timeout)
        self._acc_sensitivity = r.response_lsm6dsox_start.acc_sensitivity * _MILLI_G_TO_MS2
        self._gyr_sensitivity = r.response_lsm6dsox_start.gyr_sensitivity * _MILLI_DPS_TO_RADS
        return r

    async def lsm6dsox_stop(self, timeout: float = TIMEOUT_SETUP_S) -> Response:
        return await self._request(Request(request_lsm6dsox_stop=RequestLSM6DSOXStop()), timeout)

    async def wifi_ip_get(self, timeout: float = TIMEOUT_SETUP_S) -> str:
        r = await self._request(Request(request_wifi_ip_get=RequestWifiIPGet()), timeout)
        ip = r.response_wifi_ip_get.ip
        return ".".join(str((ip >> shift) & 0xFF) for shift in (24, 16, 8, 0))  # firmware packs big-endian (verified on hardware 2026-08-20: box at 192.168.1.50)

    async def wifi_parameters_set(self, ssid: bytes, password: bytes, timeout: float = TIMEOUT_SETUP_S) -> Response:
        req = Request(request_wifi_parameters_set=RequestWifiParametersSet(ssid=ssid, password=password))
        return await self._request(req, timeout)

    # ---- internals -----------------------------------------------------------------------------

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def _next_id(self) -> int:
        self._request_id = (self._request_id + 1) % 4096
        if self._request_id == 0:
            self._request_id = 1
        return self._request_id

    def _send(self, request: Request, timeout: float) -> asyncio.Future[Response]:
        assert self._loop is not None, "connect() first"
        request.id = self._next_id()
        fut: asyncio.Future[Response] = self._loop.create_future()
        payload = RpcMessage(request=request).SerializeToString()
        try:
            self.transport.write(hdlc.encode(payload))
        except TransportError as exc:
            fut.set_exception(DeviceError(str(exc)))
            return fut
        self._pending[request.id] = fut
        handle = self._loop.call_later(timeout, self._on_timeout, request.id)
        fut.add_done_callback(lambda _f: handle.cancel())
        return fut

    async def _request(self, request: Request, timeout: float) -> Response:
        response = await self._send(request, timeout)
        if response.HasField("error"):
            name = response.error.DESCRIPTOR.fields_by_name["code"].enum_type.values_by_number[response.error.code].name
            raise DeviceError(f"device error {name}")
        return response

    def _on_timeout(self, request_id: int) -> None:
        fut = self._pending.pop(request_id, None)
        if fut is not None and not fut.done():
            fut.set_exception(DeviceError(f"request {request_id} timed out"))

    def _cancel_pending(self, error: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(error)
        self._pending.clear()

    def _on_transport_closed(self, error: Exception | None) -> None:
        self.closed_reason = error
        logger.error("connection closed: %s", error or "clean")
        self._cancel_pending(DeviceError(f"connection closed: {error}"))
        self.closed.set()

    def _on_bytes(self, data: bytes) -> None:
        for frame in self._decoder.parse(data):
            try:
                msg = RpcMessage.FromString(frame)
            except DecodeError:
                logger.error("protobuf decode error (%d bytes)", len(frame))
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: RpcMessage) -> None:
        if msg.HasField("response"):
            fut = self._pending.pop(msg.response.id, None)
            if fut is None:
                logger.warning("response for unknown request id %d", msg.response.id)
            elif not fut.done():
                fut.set_result(msg.response)
        elif msg.HasField("notification"):
            self._handle_notification(msg.notification)

    def _handle_notification(self, n: Notification) -> None:
        which = n.WhichOneof("notification")
        kind = NOTIFICATION_FIELDS.get(which or "")
        if kind is None:
            logger.warning("unhandled notification: %s", text_format.MessageToString(n, as_one_line=True))
            return
        body = getattr(n, which)
        self._update_telemetry(kind, body)
        calls = [(cb, (body,)) for cb in self._callbacks[kind]] + [(cb, (kind, body)) for cb in self._callbacks["any"]]
        for cb, args in calls:
            try:
                result = cb(*args)
                if inspect.isawaitable(result):
                    asyncio.ensure_future(result)
            except Exception:  # noqa: BLE001 - a bad callback must not kill the link
                logger.exception("notification callback failed")

    def _update_telemetry(self, kind: str, b: Any) -> None:  # noqa: C901 - flat field mapping
        t = self.telemetry
        if kind == "boot":
            logger.error("boot notification received - device rebooted")
            self._cancel_pending(DeviceRebooted("device rebooted"))
            self.closed_reason = DeviceRebooted("device rebooted")
        elif kind == "device_volume":
            t.device_volume, t.device_volume_locked = b.volume, b.locked
        elif kind == "currents":
            t.rms = (b.rms_a, b.rms_b, b.rms_c, b.rms_d)
            t.peak = (b.peak_a, b.peak_b, b.peak_c, b.peak_d)
            t.peak_cmd, t.output_power, t.output_power_skin = b.peak_cmd, b.output_power, b.output_power_skin
        elif kind in ("output_resistance", "skin_resistance"):
            z = Complex4(
                complex(b.resistance_a, b.reluctance_a),
                complex(b.resistance_b, b.reluctance_b),
                complex(b.resistance_c, b.reluctance_c),
                complex(b.resistance_d, b.reluctance_d),
            )
            if kind == "output_resistance":
                t.output_impedance = z
            else:
                t.skin_impedance = z
        elif kind == "system_stats":
            if b.HasField("esc1"):
                t.temp_stm32, t.temp_board, t.v_bus = b.esc1.temp_stm32, b.esc1.temp_board, b.esc1.v_bus
            elif b.HasField("focstimv3"):
                s = b.focstimv3
                t.temp_stm32 = s.temp_stm32
                t.v_sys_min, t.v_sys_max = s.v_sys_min, s.v_sys_max
                t.v_boost_min, t.v_boost_max, t.boost_duty_cycle = s.v_boost_min, s.v_boost_max, s.boost_duty_cycle
        elif kind == "signal_stats":
            t.actual_pulse_frequency, t.v_drive = b.actual_pulse_frequency, b.v_drive
            t.transformer_utilization, t.voltage_utilization = b.transformer_utilization, b.voltage_utilization
        elif kind == "battery":
            t.battery_voltage, t.battery_charge_rate_watt = b.battery_voltage, b.battery_charge_rate_watt
            t.battery_soc, t.wall_power_present = b.battery_soc, b.wall_power_present
            t.battery_chip_temperature = b.chip_temperature
        elif kind == "imu":
            if self._acc_sensitivity and self._gyr_sensitivity:
                t.acc = (b.acc_x * self._acc_sensitivity, b.acc_y * self._acc_sensitivity, b.acc_z * self._acc_sensitivity)
                t.gyr = (b.gyr_x * self._gyr_sensitivity, b.gyr_y * self._gyr_sensitivity, b.gyr_z * self._gyr_sensitivity)
        elif kind == "pressure":
            t.pressure = b.pressure
        elif kind == "button":
            t.button = b.DESCRIPTOR.fields_by_name["state"].enum_type.values_by_number[b.state].name
        elif kind == "debug_string":
            # Firmware narrates self-test and battery checks through these (restim logs them as warnings).
            t.last_debug_string = b.message
            logger.warning("device: %s", b.message)
        t.touch(kind)


__all__ = [
    "AxisType",
    "Complex4",
    "DeviceError",
    "DeviceRebooted",
    "FocStimClient",
    "NOTIFICATION_FIELDS",
    "OutputMode",
    "Telemetry",
]
