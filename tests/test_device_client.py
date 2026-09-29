"""FocStimClient over an in-memory transport with a fake device built from the vendored protobufs."""

import asyncio
import functools

import pytest

from stimengine.device import (
    AxisType,
    DeviceError,
    DeviceRebooted,
    FocStimClient,
    MemoryTransport,
    OutputMode,
)
from stimengine.device import hdlc
from stimengine.device.proto.constants_pb2 import BoardIdentifier, Errors
from stimengine.device.proto.focstim_rpc_pb2 import Error, Notification, Response, RpcMessage
from stimengine.device.proto.messages_pb2 import (
    FirmwareVersion,
    ResponseAxisMoveTo,
    ResponseCapabilitiesGet,
    ResponseFirmwareVersion,
    ResponseLSM6DSOXStart,
    ResponseWifiIPGet,
)
from stimengine.device.proto.notifications_pb2 import (
    NotificationBattery,
    NotificationBoot,
    NotificationDebugString,
    NotificationSkinResistance,
)

def run_async(fn):
    """Run an async test in a fresh event loop (no pytest-asyncio dependency)."""

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))

    return wrapper



def decode_sent(transport: MemoryTransport) -> list[RpcMessage]:
    dec = hdlc.HDLCDecoder()
    out = []
    for chunk in transport.sent:
        for frame in dec.parse(chunk):
            out.append(RpcMessage.FromString(frame))
    transport.sent.clear()
    return out


def feed(transport: MemoryTransport, msg: RpcMessage) -> None:
    transport.feed(hdlc.encode(msg.SerializeToString()))


def respond(transport: MemoryTransport, request_id: int, **fields) -> None:
    feed(transport, RpcMessage(response=Response(id=request_id, **fields)))


class FakeDevice:
    """Answers requests the way firmware 1.3.2 on a v4 board would."""

    def __init__(self, transport: MemoryTransport, branch: str = "main", minor: int = 3):
        self.t = transport
        self.branch = branch
        self.minor = minor
        self.seen = []

    async def serve(self, n: int):
        served = 0
        while served < n:
            await asyncio.sleep(0)
            for msg in decode_sent(self.t):
                req = msg.request
                self.seen.append(req)
                which = req.WhichOneof("params")
                if which == "request_firmware_version":
                    respond(
                        self.t,
                        req.id,
                        response_firmware_version=ResponseFirmwareVersion(
                            board=BoardIdentifier.BOARD_FOCSTIM_V4,
                            stm32_firmware_version_2=FirmwareVersion(major=1, minor=self.minor, revision=2, branch=self.branch),
                        ),
                    )
                elif which == "request_capabilities_get":
                    respond(
                        self.t,
                        req.id,
                        response_capabilities_get=ResponseCapabilitiesGet(
                            threephase=True, fourphase=True, battery=True, device_volume=True,
                            maximum_waveform_amplitude_amps=0.2, lsm6dsox=True,
                        ),
                    )
                elif which == "request_lsm6dsox_start":
                    respond(self.t, req.id, response_lsm6dsox_start=ResponseLSM6DSOXStart(acc_sensitivity=0.122, gyr_sensitivity=17.5))
                elif which == "request_axis_move_to":
                    respond(self.t, req.id, response_axis_move_to=ResponseAxisMoveTo())
                elif which == "request_wifi_ip_get":
                    respond(self.t, req.id, response_wifi_ip_get=ResponseWifiIPGet(ip=(192 << 24) | (168 << 16) | (1 << 8) | 50))  # big-endian, verified on hardware
                elif which == "request_signal_start":
                    respond(self.t, req.id, error=Error(code=Errors.ERROR_POWER_NOT_PRESENT))
                served += 1


async def connected_client():
    t = MemoryTransport()
    c = FocStimClient(t)
    await c.connect()
    return t, c


@run_async
async def test_request_response_roundtrip():
    t, c = await connected_client()
    fut = asyncio.ensure_future(c.firmware_version())
    await asyncio.sleep(0)
    [msg] = decode_sent(t)
    assert msg.request.WhichOneof("params") == "request_firmware_version"
    assert 1 <= msg.request.id < 4096
    respond(
        t, msg.request.id,
        response_firmware_version=ResponseFirmwareVersion(stm32_firmware_version_2=FirmwareVersion(major=1, minor=3, revision=2, branch="main")),
    )
    fw = await asyncio.wait_for(fut, 1)
    assert (fw.stm32_firmware_version_2.major, fw.stm32_firmware_version_2.minor) == (1, 3)
    assert c.pending_count == 0


@run_async
async def test_timeout_raises():
    t, c = await connected_client()
    with pytest.raises(DeviceError, match="timed out"):
        await c.capabilities_get(timeout=0.05)
    assert c.pending_count == 0


@run_async
async def test_error_response_raises():
    t, c = await connected_client()
    dev = FakeDevice(t)
    task = asyncio.ensure_future(c.signal_start(OutputMode.OUTPUT_THREEPHASE))
    await dev.serve(1)
    with pytest.raises(DeviceError, match="ERROR_POWER_NOT_PRESENT"):
        await asyncio.wait_for(task, 1)


@run_async
async def test_handshake_sequence_stops_before_signal_start():
    t, c = await connected_client()
    dev = FakeDevice(t)
    task = asyncio.ensure_future(c.connect_and_handshake())
    # connect() inside handshake re-opens the memory transport; serve 3 requests: fw, caps, imu
    await dev.serve(3)
    await asyncio.wait_for(task, 1)
    kinds = [r.WhichOneof("params") for r in dev.seen]
    assert kinds == ["request_firmware_version", "request_capabilities_get", "request_lsm6dsox_start"]
    assert "request_signal_start" not in kinds
    tel = c.telemetry
    assert tel.firmware == "1.3.2 (main)" and tel.board == "BOARD_FOCSTIM_V4"
    assert tel.threephase and tel.fourphase and tel.max_waveform_amps == pytest.approx(0.2)


@run_async
async def test_handshake_rejects_wrong_branch():
    t, c = await connected_client()
    dev = FakeDevice(t, branch="dev")
    task = asyncio.ensure_future(c.connect_and_handshake())
    await dev.serve(1)
    with pytest.raises(DeviceError, match="branch"):
        await asyncio.wait_for(task, 1)
    assert not t.is_open


@run_async
async def test_notifications_dispatch_and_telemetry():
    t, c = await connected_client()
    got = []
    anys = []
    c.on("battery", got.append)
    c.on("any", lambda kind, body: anys.append(kind))

    async def async_cb(body):
        got.append(("async", body.battery_soc))

    c.on("battery", async_cb)
    feed(t, RpcMessage(notification=Notification(notification_battery=NotificationBattery(battery_voltage=3.9, battery_soc=0.55, wall_power_present=True))))
    feed(t, RpcMessage(notification=Notification(notification_skin_resistance=NotificationSkinResistance(resistance_a=100, reluctance_a=5, resistance_b=200, resistance_c=300, resistance_d=400))))
    feed(t, RpcMessage(notification=Notification(notification_debug_string=NotificationDebugString(message="Battery detected!"))))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert got[0].battery_soc == pytest.approx(0.55)
    assert ("async", pytest.approx(0.55)) in got
    assert anys == ["battery", "skin_resistance", "debug_string"]
    tel = c.telemetry
    assert tel.battery_voltage == pytest.approx(3.9) and tel.wall_power_present is True
    assert tel.skin_impedance.real() == pytest.approx((100, 200, 300, 400))
    assert tel.skin_impedance.a == complex(100, 5)
    assert tel.last_debug_string == "Battery detected!"
    assert set(tel.last_update) == {"battery", "skin_resistance", "debug_string"}


@run_async
async def test_boot_notification_fails_pending():
    t, c = await connected_client()
    fut = asyncio.ensure_future(c.capabilities_get(timeout=5))
    await asyncio.sleep(0)
    feed(t, RpcMessage(notification=Notification(notification_boot=NotificationBoot())))
    with pytest.raises(DeviceRebooted):
        await asyncio.wait_for(fut, 1)


@run_async
async def test_axis_move_to_and_wifi_ip():
    t, c = await connected_client()
    dev = FakeDevice(t)
    task = asyncio.ensure_future(c.axis_move_to(AxisType.AXIS_POSITION_ALPHA, 0.25, 30))
    await dev.serve(1)
    await asyncio.wait_for(task, 1)
    req = dev.seen[-1].request_axis_move_to
    assert req.axis == AxisType.AXIS_POSITION_ALPHA and req.value == pytest.approx(0.25) and req.interval == 30
    task = asyncio.ensure_future(c.wifi_ip_get())
    await dev.serve(1)
    assert await asyncio.wait_for(task, 1) == "192.168.1.50"


@run_async
async def test_transport_drop_fails_pending_and_sets_closed():
    t, c = await connected_client()
    fut = asyncio.ensure_future(c.capabilities_get(timeout=5))
    await asyncio.sleep(0)
    t.drop(RuntimeError("cable yanked"))
    with pytest.raises(DeviceError, match="cable yanked"):
        await asyncio.wait_for(fut, 1)
    assert c.closed.is_set() and not c.is_open
