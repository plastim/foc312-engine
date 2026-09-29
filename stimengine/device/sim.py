"""Simulated FOC-Stim v4 (firmware 1.3.2) on a MemoryTransport — for running the engine/API/viewer with no box.

    python -m stimengine.tools.serve --sim --mode threephase

Acks the protocol like the real device and streams telemetry at ~10 Hz. Currents and skin impedance follow the
commanded amps axis (impedance shows the 294 Ω placeholder below ~5 mA, like the firmware), pulse rate tracks the
pulse-frequency axis at half rate (burst gap), battery slowly drains. The simulated hardware knob is at 1.0 so the
whole commanded level shows up in the numbers. `fork=True` impersonates the stim-engine fork firmware (version
comment + OUTPUT_BIPHASIC_PAIRS, currents on the two routed electrodes of each channel); `fork_version` picks v2
(default: fractional widths, pulse shapes) or v1 (the first flashed build). NOTHING here touches hardware.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time

from . import fork as F
from . import hdlc
from .proto.constants_pb2 import AxisType, BoardIdentifier
from .proto.focstim_rpc_pb2 import Notification, Response, RpcMessage
from .proto.messages_pb2 import (
    FirmwareVersion,
    ResponseAxisMoveTo,
    ResponseAxisSet,
    ResponseCapabilitiesGet,
    ResponseFirmwareVersion,
    ResponseLSM6DSOXStart,
    ResponseLSM6DSOXStop,
    ResponseLockDeviceVolume,
    ResponseSignalStart,
    ResponseSignalStop,
    ResponseTimestampGet,
    ResponseTimestampSet,
    ResponseWifiIPGet,
)
from .proto.notifications_pb2 import (
    NotificationBattery,
    NotificationCurrents,
    NotificationDeviceVolume,
    NotificationOutputResistance,
    NotificationSignalStats,
    NotificationSkinResistance,
    NotificationSystemStats,
    SystemStatsFocstimV3,
)
from .transport import MemoryTransport

logger = logging.getLogger("engine.sim")

PLACEHOLDER_OHMS = 294.0
MEASURE_THRESHOLD_A = 0.005


class SimDevice:
    """Fake firmware behind a MemoryTransport. Call start() before the client connects (it polls the transport)."""

    def __init__(self, transport: MemoryTransport, knob: float = 1.0, fork: bool = False, fork_version: int = 2) -> None:
        self.t = transport
        self.knob = knob
        self.fork = fork
        self.fork_version = fork_version       # when fork: 1 = v1 comment, 2 = v2 (fractional widths, pulse shapes)
        self.axes: dict[int, float] = {
            AxisType.AXIS_WAVEFORM_AMPLITUDE_AMPS: 0.0,
            AxisType.AXIS_CARRIER_FREQUENCY_HZ: 790.0,
            AxisType.AXIS_PULSE_FREQUENCY_HZ: 74.0,
            AxisType.AXIS_POSITION_ALPHA: 0.0,
            AxisType.AXIS_POSITION_BETA: 0.0,
        }
        self.playing = False
        self.mode = 0
        self.soc = 0.87
        self.t0 = time.monotonic()
        self._dec = hdlc.HDLCDecoder()
        self._tasks: list[asyncio.Task] = []
        # per-electrode tissue "character" so the four channels differ like real pads
        self.tissue = [random.uniform(280, 340) for _ in range(4)]

    # ---- wiring -------------------------------------------------------------------------------------------
    def start(self) -> None:
        self._tasks = [asyncio.ensure_future(self._serve()), asyncio.ensure_future(self._telemetry())]

    def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks = []

    def _emit(self, n: Notification) -> None:
        if self.t.is_open:
            self.t.feed(hdlc.encode(RpcMessage(notification=n).SerializeToString()))

    def _fork_comment(self) -> str:
        if not self.fork:
            return ""
        return F.FORK_COMMENT_V2 if self.fork_version >= 2 else F.FORK_COMMENT

    # ---- request handling ----------------------------------------------------------------------------------
    async def _serve(self) -> None:
        while True:
            await asyncio.sleep(0.002)
            if not self.t.sent:
                continue
            chunks = list(self.t.sent)
            self.t.sent.clear()
            for chunk in chunks:
                for frame in self._dec.parse(chunk):
                    try:
                        msg = RpcMessage.FromString(frame)
                    except Exception:  # noqa: BLE001
                        continue
                    if msg.WhichOneof("message") != "request":
                        continue
                    r = self._handle(msg.request)
                    if r is not None and self.t.is_open:
                        self.t.feed(hdlc.encode(RpcMessage(response=r).SerializeToString()))

    def _handle(self, req) -> Response | None:  # noqa: C901 - flat dispatch
        which = req.WhichOneof("params")
        r = Response(id=req.id)
        if which == "request_firmware_version":
            r.response_firmware_version.CopyFrom(ResponseFirmwareVersion(
                board=BoardIdentifier.BOARD_FOCSTIM_V4,
                stm32_firmware_version_2=FirmwareVersion(major=1, minor=3, revision=2, branch="main",
                                                         comment=self._fork_comment())))
        elif which == "request_capabilities_get":
            r.response_capabilities_get.CopyFrom(ResponseCapabilitiesGet(
                threephase=True, fourphase=True, battery=True, device_volume=True,
                maximum_waveform_amplitude_amps=0.2, lsm6dsox=True))
        elif which == "request_lsm6dsox_start":
            r.response_lsm6dsox_start.CopyFrom(ResponseLSM6DSOXStart(acc_sensitivity=0.122, gyr_sensitivity=17.5))
        elif which == "request_lsm6dsox_stop":
            r.response_lsm6dsox_stop.CopyFrom(ResponseLSM6DSOXStop())
        elif which == "request_signal_start":
            self.playing = True
            self.mode = req.request_signal_start.mode
            r.response_signal_start.CopyFrom(ResponseSignalStart())
        elif which == "request_signal_stop":
            self.playing = False
            r.response_signal_stop.CopyFrom(ResponseSignalStop())
        elif which == "request_axis_move_to":
            m = req.request_axis_move_to
            self.axes[m.axis] = m.value
            r.response_axis_move_to.CopyFrom(ResponseAxisMoveTo())
        elif which == "request_axis_set":
            m = req.request_axis_set
            self.axes[m.axis] = m.value
            r.response_axis_set.CopyFrom(ResponseAxisSet())
        elif which == "request_timestamp_set":
            r.response_timestamp_set.CopyFrom(ResponseTimestampSet())
        elif which == "request_timestamp_get":
            r.response_timestamp_get.CopyFrom(ResponseTimestampGet())
        elif which == "request_lock_device_volume":
            r.response_lock_device_volume.CopyFrom(ResponseLockDeviceVolume())
        elif which == "request_wifi_ip_get":
            r.response_wifi_ip_get.CopyFrom(ResponseWifiIPGet())
        else:
            logger.debug("sim: unhandled request %s", which)
            return None
        return r

    # ---- telemetry ------------------------------------------------------------------------------------------
    def _channel_amps(self) -> list[float]:
        """Per-electrode RMS amps from commanded amps, position and mode (3-phase leaves D at 0)."""
        if self.playing and self.mode == F.OUTPUT_BIPHASIC_PAIRS:
            out = [0.0, 0.0, 0.0, 0.0]
            for amp_axis, route_axis, default in zip(F.AMP_AXES, F.ROUTE_AXES, F.DEFAULT_ROUTES):
                a = self.axes.get(amp_axis, 0.0) * self.knob
                try:
                    x, y = F.parse_route(int(round(self.axes.get(route_axis, default))))
                except F.RouteError:
                    x, y = F.parse_route(default)
                for e in (x, y):
                    out[e - 1] += a * 0.05      # short pulses: RMS well below peak
            return [max(0.0, v + random.gauss(0, 0.00003)) for v in out]
        amps = self.axes.get(AxisType.AXIS_WAVEFORM_AMPLITUDE_AMPS, 0.0) * self.knob
        if not self.playing or amps <= 0:
            return [abs(random.gauss(0, 0.00005)) for _ in range(4)]
        a = self.axes.get(AxisType.AXIS_POSITION_ALPHA, 0.0)
        b = self.axes.get(AxisType.AXIS_POSITION_BETA, 0.0)
        four = self._four_phase()
        if four:
            w = [1 + 0.5 * a, 1 + 0.5 * b, 1 - 0.5 * a, 1 - 0.5 * b]
        else:
            w = [1 + 0.5 * a, 1 - 0.25 * a + 0.43 * b, 1 - 0.25 * a - 0.43 * b, 0.0]
        s = sum(w) or 1.0
        # RMS is far below peak on the real box (short pulses); ~4% of commanded peak matched the bench numbers
        return [max(0.0, amps * 0.04 * 3 * wi / s + random.gauss(0, 0.00003)) for wi in w]

    def _four_phase(self) -> bool:
        try:
            from .proto.constants_pb2 import OutputMode

            return self.mode in (OutputMode.OUTPUT_FOURPHASE, getattr(OutputMode, "OUTPUT_FOURPHASE_INDIVIDUAL_ELECTRODES", -1))
        except Exception:  # noqa: BLE001
            return False

    async def _telemetry(self) -> None:
        tick = 0
        while True:
            await asyncio.sleep(0.1)
            tick += 1
            now = time.monotonic() - self.t0
            ch = self._channel_amps()
            amps = self.axes.get(AxisType.AXIS_WAVEFORM_AMPLITUDE_AMPS, 0.0) * self.knob
            n = Notification()
            n.notification_currents.CopyFrom(NotificationCurrents(
                rms_a=ch[0], rms_b=ch[1], rms_c=ch[2], rms_d=ch[3],
                peak_a=ch[0] * 3.3, peak_b=ch[1] * 3.3, peak_c=ch[2] * 3.3, peak_d=ch[3] * 3.3,
                peak_cmd=amps, output_power=sum(c * c for c in ch) * 300, output_power_skin=sum(c * c for c in ch) * 250))
            self._emit(n)

            if tick % 5 == 0:
                measuring = self.playing and amps > MEASURE_THRESHOLD_A
                four = self._four_phase()
                sk = NotificationSkinResistance()
                orr = NotificationOutputResistance()
                for i, name in enumerate("abcd"):
                    if measuring and (i < 3 or four):
                        # tissue is non-linear: drops a bit with level, wanders slowly
                        z = self.tissue[i] * (1 - 0.6 * min(amps, 0.1)) + 8 * math.sin(now / 7 + i) + random.gauss(0, 2)
                    else:
                        z = PLACEHOLDER_OHMS
                    setattr(sk, f"resistance_{name}", z)
                    setattr(sk, f"reluctance_{name}", -12.0 if measuring else 0.0)
                    setattr(orr, f"resistance_{name}", z + 45.0)
                    setattr(orr, f"reluctance_{name}", -30.0)
                n = Notification()
                n.notification_skin_resistance.CopyFrom(sk)
                self._emit(n)
                n = Notification()
                n.notification_output_resistance.CopyFrom(orr)
                self._emit(n)

                pf = self.axes.get(AxisType.AXIS_PULSE_FREQUENCY_HZ, 74.0)
                n = Notification()
                n.notification_signal_stats.CopyFrom(NotificationSignalStats(
                    actual_pulse_frequency=pf / 2 if self.playing else 0.0,
                    v_drive=amps * 12.5 if self.playing else 0.0,
                    transformer_utilization=min(1.0, amps * 4), voltage_utilization=min(1.0, amps * 3)))
                self._emit(n)

            if tick % 10 == 0:
                n = Notification()
                n.notification_device_volume.CopyFrom(NotificationDeviceVolume(volume=self.knob, locked=False))
                self._emit(n)
                n = Notification()
                n.notification_system_stats.CopyFrom(NotificationSystemStats(focstimv3=SystemStatsFocstimV3(
                    temp_stm32=38.0 + 40 * min(amps, 0.1) + random.gauss(0, 0.2), v_sys_min=3.9, v_sys_max=4.1, v_ref=3.3,
                    v_boost_min=11.8, v_boost_max=12.2, boost_duty_cycle=0.3)))
                self._emit(n)

            if tick % 50 == 0:
                self.soc = max(0.05, self.soc - 0.0005 - amps * 0.002)
                n = Notification()
                n.notification_battery.CopyFrom(NotificationBattery(
                    battery_voltage=3.5 + 0.7 * self.soc, battery_charge_rate_watt=-0.4 - amps * 8,
                    battery_soc=self.soc, wall_power_present=False, chip_temperature=31.0))
                self._emit(n)


__all__ = ["SimDevice"]
