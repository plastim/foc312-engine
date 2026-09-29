"""Control API end-to-end: aiohttp test client -> ControlAPI -> real Engine over MemoryTransport + fake device."""

import asyncio
import functools
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from stimengine.control.api import ControlAPI
from stimengine.control.patterns import PatternRunner
from tests.test_engine_core import AMPS, started_engine


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))

    return wrapper


class Harness:
    def __init__(self, tmp_path, **safety):
        self.tmp_path = tmp_path
        self.safety = safety

    async def __aenter__(self):
        self.t, self.dev, self.eng, self.session = await started_engine(self.tmp_path, **self.safety)
        self.api = ControlAPI(self.eng, {"api": {"http_port": 0}})
        self.client = TestClient(TestServer(self.api.app))
        await self.client.start_server()
        return self

    async def __aexit__(self, *exc):
        await self.client.close()
        await self.api.stop()
        if self.eng.running or self.eng.client.is_open:
            await self.eng.stop()
        self.dev.stop()

    async def post(self, path, **body):
        r = await self.client.post(path, json=body)
        return r.status, await r.json()

    async def get(self, path):
        r = await self.client.get(path)
        return r.status, await r.json()


@run_async
async def test_status_and_validation(tmp_path):
    async with Harness(tmp_path) as h:
        st, body = await h.get("/status")
        assert st == 200 and body["running"] and not body["armed"]
        assert body["pattern"]["running"] is False and "circle" in body["patterns_available"]
        st, body = await h.post("/volume", level="loud")
        assert st == 400 and "error" in body
        st, body = await h.post("/position", alpha=0.1)
        assert st == 400
        st, body = await h.post("/pattern", name="spiral")
        assert st == 400 and "unknown pattern" in body["error"]
        st, body = await h.post("/carrier", hz=50)
        assert st == 200 and body["hz"] == 500.0  # clamped to the carrier floor
        st, body = await h.get("/telemetry")
        assert st == 200 and body["firmware"].startswith("1.3.2")


@run_async
async def test_arm_volume_ramps_never_steps_up(tmp_path):
    async with Harness(tmp_path, slow_start_s=0.05) as h:
        st, body = await h.post("/arm")
        assert st == 200 and body["armed"]
        st, body = await h.post("/volume", level=0.5, ramp_s=0.1)
        assert st == 200 and body["ramp_s"] >= 1.0, "increase is floored at MIN_RAMP_UP_S"
        await asyncio.sleep(0.3)
        target = h.eng.status()["master_target"]
        assert 0.0 < target < 0.5, f"still ramping, got {target}"
        # decrease is immediate
        st, body = await h.post("/volume", level=0.1)
        assert st == 200 and body["ramp_s"] == 0.0
        assert h.eng.status()["master_target"] == pytest.approx(0.1)
        st, body = await h.post("/disarm")
        assert st == 200 and not body["armed"]


@run_async
async def test_position_vector_pulse_tagged_external(tmp_path):
    async with Harness(tmp_path) as h:
        before = h.eng._last_control
        await asyncio.sleep(0.01)
        st, body = await h.post("/position", alpha=0.3, beta=-0.2, source="claude")
        assert st == 200 and h.eng.frame.alpha == 0.3 and h.eng.frame.beta == -0.2
        assert h.eng._last_control > before, "API writes count as external control input"
        st, body = await h.post("/pulse", frequency=60, width=8)
        assert st == 200 and h.eng.frame.pulse_frequency == 60.0 and h.eng.frame.pulse_width == 8.0
        st, body = await h.post("/pulse")
        assert st == 400
        st, body = await h.post("/vector", e1=1, e2=0.5, e3=0, e4=2)
        assert st == 200 and body["e"] == [1.0, 0.5, 0.0, 1.0]


@run_async
async def test_pattern_lease_and_deadman(tmp_path):
    async with Harness(tmp_path, slow_start_s=0.05, deadman_silence_s=0.25, deadman_ramp_down_s=0.15) as h:
        await h.post("/arm")
        await h.post("/volume", level=0.3, ramp_s=1.0)
        st, body = await h.post("/pattern", name="circle", rate_hz=1.0, amplitude=0.6)
        assert st == 200 and body["pattern"]["running"]
        st, body = await h.post("/lease", seconds=0.5, source="claude")
        assert st == 200 and body["lease_remaining_s"] > 0
        await asyncio.sleep(0.3)
        assert h.api.runner.renewals > 0
        assert not h.eng.status()["deadman_active"]
        # lease lapses -> runner stops renewing -> deadman ramps api volume to 0
        await asyncio.sleep(0.9)
        st_ = h.eng.status()
        assert st_["deadman_active"] and st_["deadman_scale"] == 0.0
        assert h.api.runner.state()["running"], "pattern keeps running; it just can't hold the volume up"
        # renewing the lease brings it back
        await h.post("/lease", seconds=2)
        await asyncio.sleep(0.1)
        assert not h.eng.status()["deadman_active"]
        r = await h.client.delete("/pattern")
        assert r.status == 200 and (await r.json())["pattern"]["running"] is False


@run_async
async def test_pattern_update_and_envelope(tmp_path):
    async with Harness(tmp_path, slow_start_s=0.05) as h:
        await h.post("/arm")
        st, body = await h.post("/pattern", name="stroke", rate_hz=0.5, amplitude=0.4,
                                envelope={"to": 0.2, "seconds": 0.1})
        assert st == 200 and body["pattern"]["envelope"] is not None
        assert body["pattern"]["envelope"]["seconds"] >= 1.0, "envelope increase floored at MIN_RAMP_UP_S"
        st, body = await h.post("/pattern", amplitude=0.9)
        assert st == 200 and body["pattern"]["params"]["amplitude"] == 0.9 and body["pattern"]["params"]["name"] == "stroke"
        st, body = await h.post("/pattern", name="round_robin")
        assert st == 400, "4-phase pattern refused in threephase mode"


@run_async
async def test_ws_commands_and_stream(tmp_path):
    async with Harness(tmp_path) as h:
        ws = await h.client.ws_connect("/ws")
        await ws.send_json({"cmd": "position", "alpha": 0.2, "beta": 0.1, "id": 7})
        got_reply = got_stream = False
        for _ in range(10):
            msg = json.loads((await ws.receive()).data)
            if msg.get("id") == 7:
                got_reply = msg["result"]["ok"]
            if "status" in msg and "telemetry" in msg:
                got_stream = True
            if got_reply and got_stream:
                break
        assert got_reply and got_stream
        await ws.send_json({"cmd": "volume", "level": "x"})
        for _ in range(10):
            msg = json.loads((await ws.receive()).data)
            if "error" in msg:
                assert msg["status"] == 400
                break
        else:
            pytest.fail("no error reply")
        await ws.close()


@run_async
async def test_stop_via_api(tmp_path):
    async with Harness(tmp_path) as h:
        st, body = await h.post("/stop")
        assert st == 200 and body["running"] is False
        assert h.dev.signal_stopped == 1
        st, body = await h.post("/arm")
        assert st == 409
