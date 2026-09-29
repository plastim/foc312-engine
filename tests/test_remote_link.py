"""The remote's box link (remote/core/boxlink.c) against stimengine's simulated FOC-Stim (device/sim.py), in one
process: the C link is a shared library driven through ctypes; the simulated box runs on the real asyncio code."""
import asyncio
import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from stimengine.device import hdlc
from stimengine.device.proto.focstim_rpc_pb2 import Notification, Response, RpcMessage
from stimengine.device.proto.notifications_pb2 import NotificationBoot, NotificationDebugString
from stimengine.device.sim import SimDevice
from stimengine.device.transport import MemoryTransport
from tests.test_remote_core import CORE, ROOT, _zig_available
from stimengine.paths import BUILD_DIR, REMOTE_DIR

pytestmark = pytest.mark.skipif(not _zig_available(), reason="needs the ziglang package (pip install ziglang)")

SRC = [CORE / "hdlc.c", CORE / "boxlink.c", CORE / "safety.c", *sorted((CORE / "proto").glob("*.pb.c")),
       CORE / "nanopb" / "pb_common.c", CORE / "nanopb" / "pb_decode.c", CORE / "nanopb" / "pb_encode.c",
       REMOTE_DIR / "test" / "ffi_link.c"]
LIB = BUILD_DIR / "remote" / ("remote_link.dll" if os.name == "nt" else "remote_link.so")
IDLE, HS_FW, HS_CAPS, STARTING, RUNNING, FAULT = range(6)
WRITE_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_uint8), ctypes.c_int)


class Telemetry(ctypes.Structure):
    _fields_ = [("rms", ctypes.c_float * 4), ("peak", ctypes.c_float * 4), ("peak_cmd", ctypes.c_float),
                ("output_power", ctypes.c_float), ("output_power_skin", ctypes.c_float),
                ("out_r", ctypes.c_float * 4), ("out_x", ctypes.c_float * 4), ("skin_r", ctypes.c_float * 4),
                ("skin_x", ctypes.c_float * 4), ("device_volume", ctypes.c_float), ("volume_locked", ctypes.c_bool),
                ("battery_v", ctypes.c_float), ("battery_soc", ctypes.c_float), ("battery_charge_w", ctypes.c_float),
                ("wall_power", ctypes.c_bool), ("pulse_rate", ctypes.c_float), ("v_drive", ctypes.c_float),
                ("transformer_util", ctypes.c_float), ("voltage_util", ctypes.c_float),
                ("sigma", ctypes.c_float * 2), ("guard", ctypes.c_float), ("qnet", ctypes.c_float * 2),
                ("currents_ms", ctypes.c_uint32), ("hold", ctypes.c_float), ("pk", ctypes.c_float * 2),
                ("rho", ctypes.c_float * 2), ("r_est", ctypes.c_float * 2)]


def _lib() -> ctypes.CDLL:
    headers = [CORE / "boxlink.h", CORE / "hdlc.h", CORE / "safety.h"]
    newest = max(p.stat().st_mtime for p in SRC + headers)
    if not LIB.exists() or LIB.stat().st_mtime < newest:
        LIB.parent.mkdir(parents=True, exist_ok=True)
        inc = [f"-I{CORE}", f"-I{CORE / 'nanopb'}", f"-I{CORE / 'proto'}"]
        cmd = [sys.executable, "-m", "ziglang", "cc", "-std=c99", "-O2", "-Wall", "-Wextra", "-Werror", "-shared",
               *inc, *map(str, SRC), "-o", str(LIB)]
        if os.name != "nt":
            cmd += ["-fPIC", "-lm"]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    d = ctypes.CDLL(str(LIB))
    for name in ("bl_fault", "bl_fw_version", "bl_fw_comment", "bl_trip"):
        getattr(d, name).restype = ctypes.c_char_p
    d.bl_max_amps.restype = ctypes.c_float
    d.bl_tele.restype = ctypes.POINTER(Telemetry)
    d.bl_init.argtypes = [WRITE_CB, ctypes.c_float]
    d.bl_feed.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_uint32]
    return d


class Harness:
    def __init__(self, *, fork=True, cap=0.15):
        self.d = _lib()
        self.t = MemoryTransport()
        self.sim = SimDevice(self.t, fork=fork, fork_version=2)
        self.t0 = time.monotonic()
        self.writes = 0
        self._cb = WRITE_CB(self._write)           # keep a reference while the C side holds it
        self.d.bl_init(self._cb, ctypes.c_float(cap))

    def now(self) -> int:
        return int((time.monotonic() - self.t0) * 1000) & 0xFFFFFFFF

    def _write(self, ptr, n) -> int:
        if not self.t.is_open:
            return 0
        self.t.write(ctypes.string_at(ptr, n))
        self.writes += 1
        return 1

    def _on_bytes(self, data: bytes) -> None:
        self.d.bl_feed(data, len(data), self.now())

    async def connect(self) -> None:
        await self.t.connect(self._on_bytes)
        self.sim.start()
        self.d.bl_connected(self.now())

    def state(self) -> int:
        return self.d.bl_state()

    def fault(self) -> str:
        return self.d.bl_fault().decode()

    def send(self, amps=(0.0, 0.0), rate=(50.0, 50.0), width=(150.0, 150.0), asym=(1.0, 1.0), route=(12, 34),
             shape=(0, 0)) -> int:
        D2, I2 = ctypes.c_double * 2, ctypes.c_int * 2
        return self.d.bl_send(D2(*amps), D2(*rate), D2(*width), D2(*asym), I2(*route), I2(*shape), self.now())

    async def run(self, seconds: float, until=None, **values) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.d.bl_poll(self.now())
            if self.state() == RUNNING and values:
                self.send(**values)
            if until is not None and until():
                return
            await asyncio.sleep(1 / 60)

    def play(self, on: bool) -> None:
        self.d.bl_set_playing(1 if on else 0, self.now())

    def inject(self, notification: Notification) -> None:
        self.t.feed(hdlc.encode(RpcMessage(notification=notification).SerializeToString()))

    def close(self) -> None:
        self.sim.stop()


def _run(coro):
    return asyncio.run(coro)


def test_handshake_then_values_reach_the_box():
    async def go():
        h = Harness()
        await h.connect()
        await h.run(2.0, until=lambda: h.state() in (RUNNING, FAULT))
        assert h.state() == RUNNING, h.fault()
        assert h.d.bl_fork_version() == 2 and abs(h.d.bl_max_amps() - 0.2) < 1e-6
        assert h.d.bl_fw_comment().decode().startswith("stim-engine biphasic-pairs")
        assert not h.sim.playing                       # connected and ready, but the box says Idle until a start
        h.play(True)
        await h.run(0.3, until=lambda: h.d.bl_playing())
        assert h.sim.playing and h.sim.mode == 5 and h.d.bl_playing()
        v = dict(amps=(0.05, 0.03), rate=(80.0, 60.0), width=(170.0, 130.0), asym=(1.0, 3.0), route=(21, 34),
                 shape=(2, 0))
        await h.run(0.4, **v)
        ax = h.sim.axes
        assert abs(ax[60] - 0.05) < 1e-6 and abs(ax[61] - 0.03) < 1e-6
        assert ax[62] == 80.0 and ax[65] == 130.0 and ax[70] == 3.0 and ax[71] == 21 and ax[73] == 2
        h.d.bl_stop(h.now())
        await h.run(0.2)
        assert not h.sim.playing and h.sim.axes[60] == 0.0
        h.close()
    _run(go())


def test_everything_is_resent_every_second_as_the_keepalive():
    async def go():
        h = Harness()
        await h.connect()
        await h.run(2.0, until=lambda: h.state() == RUNNING)
        v = dict(amps=(0.02, 0.0))
        await h.run(0.3, **v)
        before = h.writes
        await h.run(2.2, **v)                          # nothing changes: only the 1 s refreshes go out
        sent = h.writes - before
        assert 2 * 15 <= sent <= 3 * 15, sent
        assert h.state() == RUNNING
        h.close()
    _run(go())


def test_telemetry_is_parsed():
    async def go():
        h = Harness()
        await h.connect()
        await h.run(2.0, until=lambda: h.state() == RUNNING)
        h.play(True)
        await h.run(1.5, amps=(0.05, 0.04))
        t = h.d.bl_tele().contents
        assert t.currents_ms > 0 and max(t.peak) > 0 and max(t.skin_r) > 0
        # the heartbeat: every valid frame counts, and the last one is recent
        assert h.d.bl_rx_frames() > 10 and h.now() - h.d.bl_last_rx_ms() < 500
        # round trip of axis updates: measured, and small against the simulated box
        h.d.bl_rtt_avg.restype = ctypes.c_float
        assert 0 < h.d.bl_rtt_avg() < 100 and 0 < h.d.bl_rtt_max() < 200
        # the simulator sends battery every 5 s: inject known values (also checks the struct layout end to end)
        from stimengine.device.proto.notifications_pb2 import NotificationBattery, NotificationDeviceVolume
        from stimengine.device.proto.notifications_pb2 import NotificationDebugTeleplot
        h.inject(Notification(notification_battery=NotificationBattery(
            battery_voltage=4.1, battery_soc=0.8, battery_charge_rate_watt=-0.5, wall_power_present=True)))
        h.inject(Notification(notification_device_volume=NotificationDeviceVolume(volume=0.37, locked=True)))
        h.inject(Notification(notification_debug_teleplot=NotificationDebugTeleplot(id="bp_sigma_b", value=0.42)))
        h.inject(Notification(notification_debug_teleplot=NotificationDebugTeleplot(id="bp_guard", value=1966)))
        for tid, val in (("bp_hold", 812.0), ("bp_pk_b", 1.42), ("bp_rho_a", 0.93), ("bp_r_b", 21.5)):   # fork v7
            h.inject(Notification(notification_debug_teleplot=NotificationDebugTeleplot(id=tid, value=val)))
        t = h.d.bl_tele().contents
        assert abs(t.battery_v - 4.1) < 1e-6 and abs(t.battery_soc - 0.8) < 1e-6 and t.wall_power
        assert abs(t.device_volume - 0.37) < 1e-6 and t.volume_locked
        assert abs(t.sigma[1] - 0.42) < 1e-6 and t.guard == 1966.0
        assert t.hold == 812.0 and abs(t.pk[1] - 1.42) < 1e-6 and abs(t.rho[0] - 0.93) < 1e-6 and t.r_est[1] == 21.5
        h.close()
    _run(go())


def test_a_reboot_is_a_fault():
    async def go():
        h = Harness()
        await h.connect()
        await h.run(2.0, until=lambda: h.state() == RUNNING)
        h.inject(Notification(notification_boot=NotificationBoot()))
        assert h.state() == FAULT and "rebooted" in h.fault()
        h.close()
    _run(go())


def test_a_silent_box_is_a_fault():
    async def go():
        h = Harness()
        await h.connect()
        await h.run(2.0, until=lambda: h.state() == RUNNING)
        h.sim.stop()                                   # the box stops answering (switched off, or latched)
        t0 = time.monotonic()
        await h.run(5.0, until=lambda: h.state() == FAULT, amps=(0.03, 0.0))
        took = time.monotonic() - t0
        assert h.state() == FAULT and "went silent" in h.fault()
        assert took < 2.2, took                        # 1.5 s of silence, not the 4 s update timeout
    _run(go())


def test_stock_firmware_is_refused():
    async def go():
        h = Harness(fork=False)
        await h.connect()
        await h.run(2.0, until=lambda: h.state() in (RUNNING, FAULT))
        assert h.state() == FAULT and "not the fork firmware" in h.fault()
        assert not h.sim.playing
        h.close()
    _run(go())


def test_a_cap_above_the_box_maximum_is_refused():
    async def go():
        h = Harness(cap=0.25)
        await h.connect()
        await h.run(2.0, until=lambda: h.state() in (RUNNING, FAULT))
        assert h.state() == FAULT and "exceeds the box maximum" in h.fault()
        assert not h.sim.playing
        h.close()
    _run(go())


def test_an_error_response_is_a_fault(monkeypatch):
    async def go():
        h = Harness()
        orig = h.sim._handle

        def refuse_start(req):
            if req.WhichOneof("params") == "request_signal_start":
                r = Response(id=req.id)
                r.error.code = 1
                return r
            return orig(req)

        h.sim._handle = refuse_start
        await h.connect()
        await h.run(2.0, until=lambda: h.state() in (RUNNING, FAULT))
        assert h.state() == RUNNING
        h.play(True)
        await h.run(1.0, until=lambda: h.state() == FAULT)
        assert h.state() == FAULT and "signal start refused" in h.fault()
        h.close()
    _run(go())


def test_a_box_still_playing_from_before_is_stopped_then_started():
    """After a remote reboot the box is still playing and refuses a start with error 4 (ERROR_ALREADY_PLAYING)."""
    async def go():
        h = Harness()
        orig = h.sim._handle
        starts = []

        def like_the_firmware(req):
            which = req.WhichOneof("params")
            if which == "request_signal_start":
                starts.append(h.sim.playing)
                if h.sim.playing:
                    r = Response(id=req.id)
                    r.error.code = 4
                    return r
            return orig(req)

        h.sim._handle = like_the_firmware
        h.sim.playing, h.sim.mode = True, 5          # left playing by an earlier connection
        await h.connect()
        await h.run(2.0, until=lambda: h.state() in (RUNNING, FAULT))
        assert h.state() == RUNNING, h.fault()
        assert not h.sim.playing and starts == []    # the handshake stopped it
        h.play(True)
        await h.run(0.5, until=lambda: h.d.bl_playing())
        assert starts == [False] and h.sim.playing   # so the start was accepted
        # and if the box somehow already plays, error 4 on a start is not a fault
        h.play(False)
        await h.run(0.3, until=lambda: not h.d.bl_playing())
        h.sim.playing = True
        h.play(True)
        await h.run(0.5, until=lambda: h.d.bl_playing())
        assert starts[-1] is True and h.state() == RUNNING and h.d.bl_playing()
        h.close()
    _run(go())


def test_the_box_stops_with_the_remote():
    """STOP on the remote stops the box's signal (its screen says Idle), with both channels zeroed first; start
    starts it again. Only changes are sent."""
    async def go():
        h = Harness()
        await h.connect()
        await h.run(2.0, until=lambda: h.state() == RUNNING)
        h.play(True)
        await h.run(0.5, until=lambda: h.sim.playing, amps=(0.04, 0.02))
        await h.run(0.3, amps=(0.04, 0.02))
        assert h.sim.playing and h.sim.axes[60] > 0
        before = h.writes
        h.play(False)
        h.play(False)                                 # repeated: nothing more goes out
        assert h.writes - before == 3                 # amps A 0, amps B 0, signal stop
        await h.run(0.3, until=lambda: not h.sim.playing)
        assert not h.sim.playing and h.sim.axes[60] == 0.0 and h.sim.axes[61] == 0.0 and not h.d.bl_playing()
        await h.run(2.0)                              # stopped and quiet is not "silent": the box keeps reporting
        assert h.state() == RUNNING
        h.play(True)
        await h.run(0.5, until=lambda: h.sim.playing)
        assert h.sim.playing and h.d.bl_playing()
        h.close()
    _run(go())


def test_the_trip_report_is_kept():
    async def go():
        h = Harness()
        await h.connect()
        await h.run(2.0, until=lambda: h.state() == RUNNING)
        lines = ["biphasic: current limit exceeded (limit 0.262 A primary)",
                 "biphasic trip: meas a 0.003 b -0.271 c 0.000 d 0.000 A primary at sample 4 of 13",
                 "biphasic trip: cmd peak 0.142 A primary, lead 87.1 us, return 87.1 us, shape 0, route 21",
                 "biphasic trip: r_est 31.95 ohm, sigma 0.00 (bin 1 + 0.92), v_drive 4.31 V, scale 1.00"]
        h.inject(Notification(notification_debug_string=NotificationDebugString(message="unrelated chatter")))
        for ln in lines:
            h.inject(Notification(notification_debug_string=NotificationDebugString(message=ln)))
        assert h.d.bl_ntrip() == 4
        assert [h.d.bl_trip(i).decode() for i in range(4)] == lines
        h.close()
    _run(go())


def test_the_trip_report_survives_the_reconnect_attempts():
    """A tripped box stops answering; the remote retries every 3 s. The report must still be there to read."""
    async def go():
        h = Harness()
        await h.connect()
        await h.run(2.0, until=lambda: h.state() == RUNNING)
        lines = ["biphasic: current limit exceeded (limit 0.304 A primary)",
                 "biphasic trip: meas a -0.309 b 0.001 c 0.000 d -0.000 A primary at sample 12 of 17",
                 "biphasic trip: cmd peak 0.184 A primary, lead 130.0 us, return 130.0 us, shape 2 (0.00), route 21",
                 "biphasic trip: r_est 15.79 ohm, sigma 0.44 (bin 2 + 1.00), v_drive 3.70 V, scale 1.00"]
        for ln in lines:
            h.inject(Notification(notification_debug_string=NotificationDebugString(message=ln)))
        assert h.d.bl_trip_seq() == 1
        h.d.bl_disconnected(b"the box stopped answering")
        h.d.bl_connected(h.now())                      # a reconnect attempt (the box is latched: it will time out)
        assert h.d.bl_ntrip() == 4 and [h.d.bl_trip(i).decode() for i in range(4)] == lines
        h.inject(Notification(notification_debug_string=NotificationDebugString(message=lines[0])))
        assert h.d.bl_trip_seq() == 2 and h.d.bl_ntrip() == 1   # a new report replaces the old one
        h.close()
    _run(go())
