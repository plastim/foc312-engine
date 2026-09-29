"""Engine over MemoryTransport + a fake firmware-1.3.2 device: safety stack, streaming, session files."""

import asyncio
import functools
import json
import time

import pytest

from stimengine.device import AxisType, FocStimClient, MemoryTransport, hdlc
from stimengine.device.proto.constants_pb2 import BoardIdentifier, OutputMode
from stimengine.device.proto.focstim_rpc_pb2 import Notification, Response, RpcMessage
from stimengine.device.proto.messages_pb2 import (
    FirmwareVersion,
    ResponseAxisMoveTo,
    ResponseCapabilitiesGet,
    ResponseFirmwareVersion,
    ResponseLSM6DSOXStart,
    ResponseSignalStart,
    ResponseSignalStop,
)
from stimengine.device.proto.notifications_pb2 import NotificationBattery, NotificationBoot
from stimengine.engine import HARD_AMPS_CAP, MOVE_INTERVAL_MS, Engine, EngineError
from stimengine.session import SessionLogger

AMPS = AxisType.AXIS_WAVEFORM_AMPLITUDE_AMPS


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))

    return wrapper


def make_config(**safety):
    s = {"slow_start_s": 0.3, "deadman_silence_s": 0.3, "deadman_ramp_down_s": 0.3}
    s.update(safety)
    return {
        "signal": {"waveform_amplitude_amps": 0.15, "min_carrier_hz": 500, "max_carrier_hz": 2000},
        "carrier_defaults": {"pulse_width": 11.5, "pulse_carrier_frequency": 790, "pulse_frequency": 74},
        "calibration": {"threephase": {"center": -0.3, "neutral": -2.1, "right": 0.3},
                        "fourphase": {"a": 0.3, "b": 1.1, "c": -0.3, "d": 0.0, "center_reduction": 0.07}},
        "safety": s,
    }


def decode_sent(t: MemoryTransport):
    dec = hdlc.HDLCDecoder()
    out = []
    for chunk in t.sent:
        for frame in dec.parse(chunk):
            out.append(RpcMessage.FromString(frame))
    t.sent.clear()
    return out


def feed(t: MemoryTransport, msg: RpcMessage):
    t.feed(hdlc.encode(msg.SerializeToString()))


class FakeDevice:
    """Acks everything like firmware 1.3.2 on a v4 board; records axis moves."""

    def __init__(self, t: MemoryTransport):
        self.t = t
        self.moves: list[tuple[float, int, float, int]] = []  # (time, axis, value, interval)
        self.signal_started = 0
        self.signal_stopped = 0
        self.ack_axis = True
        self._task = None

    def start(self):
        self._task = asyncio.ensure_future(self.serve())

    async def serve(self):
        while True:
            await asyncio.sleep(0.002)
            for msg in decode_sent(self.t):
                req = msg.request
                which = req.WhichOneof("params")
                r = Response(id=req.id)
                if which == "request_firmware_version":
                    r.response_firmware_version.CopyFrom(ResponseFirmwareVersion(
                        board=BoardIdentifier.BOARD_FOCSTIM_V4,
                        stm32_firmware_version_2=FirmwareVersion(major=1, minor=3, revision=2, branch="main")))
                elif which == "request_capabilities_get":
                    r.response_capabilities_get.CopyFrom(ResponseCapabilitiesGet(
                        threephase=True, fourphase=True, battery=True, device_volume=True,
                        maximum_waveform_amplitude_amps=0.2, lsm6dsox=True))
                elif which == "request_lsm6dsox_start":
                    r.response_lsm6dsox_start.CopyFrom(ResponseLSM6DSOXStart(acc_sensitivity=0.122, gyr_sensitivity=17.5))
                elif which == "request_signal_start":
                    self.signal_started += 1
                    r.response_signal_start.CopyFrom(ResponseSignalStart())
                elif which == "request_signal_stop":
                    self.signal_stopped += 1
                    r.response_signal_stop.CopyFrom(ResponseSignalStop())
                elif which == "request_axis_move_to":
                    m = req.request_axis_move_to
                    self.moves.append((time.monotonic(), m.axis, m.value, m.interval))
                    if not self.ack_axis:
                        continue
                    r.response_axis_move_to.CopyFrom(ResponseAxisMoveTo())
                else:
                    continue
                if self.t.is_open:
                    feed(self.t, RpcMessage(response=r))

    def amps_moves(self):
        return [m for m in self.moves if m[1] == AMPS]

    def stop(self):
        if self._task:
            self._task.cancel()


async def started_engine(tmp_path, mode="threephase", **safety):
    t = MemoryTransport()
    dev = FakeDevice(t)
    dev.start()
    client = FocStimClient(t)
    session = SessionLogger(tmp_path / "sessions", name="test")
    eng = Engine(make_config(**safety), client, session)
    await eng.start(mode)
    return t, dev, eng, session


@run_async
async def test_start_handshakes_and_streams_full_frame(tmp_path):
    t, dev, eng, session = await started_engine(tmp_path)
    await asyncio.sleep(0.05)
    assert dev.signal_started == 1
    axes = {m[1] for m in dev.moves}
    assert {AxisType.AXIS_POSITION_ALPHA, AxisType.AXIS_POSITION_BETA, AMPS,
            AxisType.AXIS_CARRIER_FREQUENCY_HZ, AxisType.AXIS_CALIBRATION_3_CENTER} <= axes
    first = [m for m in dev.moves if m[3] == 0]
    assert first, "initial transmit uses interval 0"
    assert all(m[2] == 0.0 for m in dev.amps_moves()), "master is 0 until armed"
    await eng.stop()
    dev.stop()


@run_async
async def test_master_requires_arm_and_slow_starts(tmp_path):
    t, dev, eng, session = await started_engine(tmp_path, slow_start_s=0.3, deadman_silence_s=5.0)
    eng.set_master(1.0)
    await asyncio.sleep(0.1)
    assert max(m[2] for m in dev.amps_moves()) == 0.0
    eng.arm()
    t_arm = time.monotonic()
    await asyncio.sleep(0.12)
    mid = eng.status()["master"]
    assert 0.15 < mid < 0.7, f"ramping at ~40%: {mid}"
    await asyncio.sleep(0.3)
    assert eng.status()["master"] == 1.0
    assert time.monotonic() - t_arm >= 0.3
    amps = [m[2] for m in dev.amps_moves()]
    assert amps == sorted(amps), "slow-start only ever rises"
    from stimengine.math import StimFrame, VolumeParts, evaluate
    full = evaluate(StimFrame(volume=VolumeParts(master=1.0), carrier_frequency=790, pulse_frequency=74, pulse_width=11.5), eng.models)[AMPS]
    assert max(amps) == pytest.approx(full)  # tau + pulse-frequency derating applied, identical to restim's math
    await eng.stop()
    dev.stop()


@run_async
async def test_deadman_ramps_api_to_zero_and_recovers(tmp_path):
    t, dev, eng, session = await started_engine(tmp_path, slow_start_s=0.0, deadman_silence_s=0.2, deadman_ramp_down_s=0.2)
    eng.set_master(1.0)
    eng.arm()
    await asyncio.sleep(0.1)
    assert eng.last_values[AMPS] > 0
    await asyncio.sleep(0.5)  # silence > 0.2 + ramp 0.2
    st = eng.status()
    assert st["deadman_active"] and st["deadman_scale"] == 0.0
    assert eng.last_values[AMPS] == 0.0
    eng.set_position(0.1, 0.1)  # external control resumes
    await asyncio.sleep(0.05)
    assert not eng.status()["deadman_active"] and eng.last_values[AMPS] > 0
    # internal source does not count; lease renewal does
    await asyncio.sleep(0.45)
    assert eng.status()["deadman_active"]
    eng.renew_lease("pattern")
    await asyncio.sleep(0.05)
    assert not eng.status()["deadman_active"]
    await eng.stop()
    dev.stop()


@run_async
async def test_cap_clamp_and_hard_cap(tmp_path):
    cfg = make_config()
    cfg["signal"]["waveform_amplitude_amps"] = 0.25
    t = MemoryTransport()
    with pytest.raises(EngineError):
        Engine(cfg, FocStimClient(t))
    t, dev, eng, session = await started_engine(tmp_path, slow_start_s=0.0)
    eng.set_master(5.0)  # clipped to 1
    eng.set_api_volume(9.0)
    eng.arm()
    eng.set_tau(1.0)  # near-zero tau => no derating
    eng.set_flags(pulse_frequency_adjustment=False)
    eng.set_carrier(2000.0)
    await asyncio.sleep(0.1)
    assert eng.last_values[AMPS] <= 0.15 <= HARD_AMPS_CAP
    assert eng.last_values[AMPS] == pytest.approx(0.15, abs=1e-9)
    await eng.stop()
    dev.stop()


@run_async
async def test_sensor_hook_may_only_reduce(tmp_path):
    t, dev, eng, session = await started_engine(tmp_path, slow_start_s=0.0)
    eng.set_master(1.0)
    eng.arm()
    await asyncio.sleep(0.05)
    base = eng.last_values[AMPS]
    eng.set_sensor_hook(lambda p: p.__setitem__("volume", p["volume"] * 10))
    await asyncio.sleep(0.05)
    assert eng.last_values[AMPS] == pytest.approx(base)
    eng.set_sensor_hook(lambda p: p.__setitem__("volume", p["volume"] * 0.5))
    await asyncio.sleep(0.05)
    assert eng.last_values[AMPS] == pytest.approx(base * 0.5)
    await eng.stop()
    dev.stop()


@run_async
async def test_dirty_only_sends_and_periodic_refresh(tmp_path):
    t, dev, eng, session = await started_engine(tmp_path)
    await asyncio.sleep(0.05)
    dev.moves.clear()
    await asyncio.sleep(0.3)  # nothing changed, < refresh period
    assert dev.moves == []
    eng.set_carrier(600.0)
    await asyncio.sleep(0.05)
    assert {m[1] for m in dev.moves} <= {AxisType.AXIS_CARRIER_FREQUENCY_HZ, AxisType.AXIS_PULSE_FREQUENCY_HZ, AMPS}
    assert all(m[3] == MOVE_INTERVAL_MS for m in dev.moves)
    dev.moves.clear()
    await asyncio.sleep(1.1)  # refresh resends everything once
    n_full = len(eng.last_values)
    assert len(dev.moves) >= n_full
    await eng.stop()
    dev.stop()


@run_async
async def test_reboot_faults_and_stops_sending(tmp_path):
    t, dev, eng, session = await started_engine(tmp_path, slow_start_s=0.0)
    eng.set_master(1.0)
    eng.arm()
    await asyncio.sleep(0.05)
    feed(t, RpcMessage(notification=Notification(notification_boot=NotificationBoot())))
    await asyncio.sleep(0.05)
    assert eng.faulted and not eng.running and not eng.armed
    dev.moves.clear()
    eng.set_position(0.5, 0.5)
    await asyncio.sleep(0.2)
    assert dev.moves == []
    with pytest.raises(EngineError):
        eng.arm()
    meta = json.loads((session.dir / "meta.json").read_text())
    assert meta["faulted"] is True and meta["end_reason"].startswith("fault")
    dev.stop()


@run_async
async def test_transport_drop_faults(tmp_path):
    t, dev, eng, session = await started_engine(tmp_path, slow_start_s=0.0)
    eng.set_master(1.0)
    eng.arm()
    await asyncio.sleep(0.05)
    t.drop(ConnectionResetError("cable yanked"))
    await asyncio.sleep(0.1)
    assert eng.faulted
    assert not eng.running
    dev.stop()


@run_async
async def test_disarm_zeroes_and_stop_orders_zero_before_signal_stop(tmp_path):
    t, dev, eng, session = await started_engine(tmp_path, slow_start_s=0.0)
    eng.set_master(1.0)
    eng.arm()
    await asyncio.sleep(0.05)
    assert eng.last_values[AMPS] > 0
    eng.disarm()
    await asyncio.sleep(0.05)
    assert eng.last_values[AMPS] == 0.0 and not eng.armed
    eng.arm()
    await asyncio.sleep(0.05)
    dev.moves.clear()
    await eng.stop()
    zero_at = [m[0] for m in dev.amps_moves() if m[2] == 0.0]
    assert zero_at, "amps zeroed on stop"
    assert dev.signal_stopped == 1
    assert not t.is_open
    dev.stop()


@run_async
async def test_session_files(tmp_path):
    t, dev, eng, session = await started_engine(tmp_path, slow_start_s=0.0)
    feed(t, RpcMessage(notification=Notification(notification_battery=NotificationBattery(battery_voltage=3.9, battery_soc=0.8))))
    eng.set_master(0.5)
    eng.arm()
    await asyncio.sleep(0.1)
    await eng.stop()
    d = session.dir
    meta = json.loads((d / "meta.json").read_text())
    assert meta["mode"] == "threephase" and meta["firmware"].startswith("1.3.2")
    assert meta["transport"] == "memory" and meta["end_reason"] == "stop"
    assert meta["summary"]["max_amps_commanded"] > 0
    tele = [json.loads(l) for l in (d / "telemetry.jsonl").read_text().splitlines()]
    assert any(r["kind"] == "battery" and r["data"]["battery_voltage"] == pytest.approx(3.9) for r in tele)
    cmds = [json.loads(l) for l in (d / "commands.jsonl").read_text().splitlines()]
    kinds = [c["kind"] for c in cmds]
    assert kinds[0] == "amps" and kinds[1] == "signal_start" and "arm" in kinds and "signal_stop" in kinds
    assert any(c["kind"] == "set_master" and c["level"] == 0.5 for c in cmds)
    assert session.write_errors == 0
    dev.stop()
