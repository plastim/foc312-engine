"""foc312: route codes, pattern catalogue, the fork-firmware axis stream, stock-mode greying, deadman, HTTP."""

import asyncio
import functools
import sys
import time
import types
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from stimengine.control.foc312_api import Foc312API
from stimengine.device import FocStimClient, MemoryTransport
from stimengine.device import fork as F
from stimengine.device.proto.constants_pb2 import BoardIdentifier
from stimengine.device.proto.focstim_rpc_pb2 import Response, RpcMessage
from stimengine.device.proto.messages_pb2 import (
    FirmwareVersion,
    ResponseAxisMoveTo,
    ResponseCapabilitiesGet,
    ResponseFirmwareVersion,
    ResponseLSM6DSOXStart,
    ResponseSignalStart,
    ResponseSignalStop,
)
from stimengine.engine import Engine, EngineError
from stimengine.et312 import foc312 as FOC
from stimengine.et312.foc312 import BUILTIN_MODES, Foc312Error, Foc312Runner, pattern_catalog
from stimengine.session import SessionLogger
from tests.test_engine_core import decode_sent, feed, make_config


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


# ---------------------------------------------------------------- route codes

@pytest.mark.parametrize("code,expect", [(12, 12), ("41", 41), ("2-3", 23), ((3, 4), 34), (21, 21)])
def test_route_valid(code, expect):
    assert F.validate_route(code) == expect


@pytest.mark.parametrize("code", [11, 22, 45, 50, 9, 123, "ab", "", None, (1, 1), (0, 2)])
def test_route_invalid(code):
    with pytest.raises(F.RouteError):
        F.validate_route(code)


def test_reverse_swaps_digits():
    assert F.reverse_route(12) == 21
    assert F.reverse_route(41) == 14
    assert F.reverse_route(F.reverse_route(23)) == 23


# ---------------------------------------------------------------- catalogue

@pytest.mark.needs_et312_data
def test_catalog_has_all_18_builtins_in_box_order(tmp_path, monkeypatch):
    monkeypatch.setattr(FOC, "_elk_module", lambda: None)
    groups, _, err = pattern_catalog(tmp_path)
    assert err is None
    assert [g["label"] for g in groups] == ["Built-in modes"]
    items = groups[0]["items"]
    box = [i for i in items if i["note"] != "PlaStim variant"]
    assert [i["id"] for i in box] == [f"builtin:{k}" for k in (
        "waves", "stroke", "climb", "combo", "intense", "rhythm", "audio1", "audio2", "audio3", "split",
        "random1", "random2", "toggle", "orgasm", "torment", "phase1", "phase2", "phase3")]
    assert len(box) == 18
    ids = [i["id"] for i in items]                     # the PlaStim variants follow the mode they vary
    assert ids[ids.index("builtin:climb") + 1:ids.index("builtin:climb") + 3] == ["builtin:climb_slow",
                                                                                   "builtin:climb_hold"]
    names = [i["name"] for i in box]
    assert names[0] == "Waves" and names[10] == "Random 1" and names[17] == "Phase 3"
    assert all("(no audio input)" in n for n in names[6:9])
    assert not any(i["id"].startswith("builtin:user") for i in items)
    assert not any(i["disabled"] for i in items)     # audio stubs run safely (steady intensity at the level)


@pytest.mark.needs_et312_data
def test_every_builtin_selects_and_runs(monkeypatch):
    run = Foc312Runner()
    run.set_levels(0.5, 0.5)
    for key, _ in BUILTIN_MODES:
        run.set_pattern(f"builtin:{key}")
        assert run.advance(dt=0.2) > 0
        for ch in ("a", "b"):
            v = run.state()[ch]
            assert 0.0 <= v["intensity"] <= 1.0
            assert F.FREQ_RANGE[0] <= v["rate_hz"] <= F.FREQ_RANGE[1]


def _fake_elk(routines):
    mod = types.SimpleNamespace(list_routines=lambda folder=None, **kw: routines, load=lambda path: "waves")
    return lambda: mod


@pytest.mark.needs_et312_data
def test_catalog_three_groups_when_importer_marks_bundled(tmp_path, monkeypatch):
    monkeypatch.setattr(FOC, "_elk_module", _fake_elk([
        {"name": "Mine", "path": str(tmp_path / "mine.elk"), "description": "x"},
        {"name": "Official", "path": str(tmp_path / "off.elk"), "bundled": True},
        {"name": "Official 2", "path": str(tmp_path / "off2.elk"), "source": "eroslink"},
    ]))
    groups, elk_by_id, _ = pattern_catalog(tmp_path)
    assert [g["label"] for g in groups] == ["ErosLink (bundled)", "Built-in modes", "Your routines"]
    assert [i["name"] for i in groups[0]["items"]] == ["Official", "Official 2"]
    assert [i["name"] for i in groups[2]["items"]] == ["Mine"]
    assert len(elk_by_id) == 3


@pytest.mark.needs_et312_data
def test_catalog_two_groups_without_bundled_field(tmp_path, monkeypatch):
    monkeypatch.setattr(FOC, "_elk_module", _fake_elk([{"name": "Mine", "path": str(tmp_path / "m.elk")}]))
    groups, _, _ = pattern_catalog(tmp_path)
    assert [g["label"] for g in groups] == ["Built-in modes", "Your routines"]


@pytest.mark.needs_et312_data
def test_elk_selection_goes_through_importer(tmp_path, monkeypatch):
    monkeypatch.setattr(FOC, "_elk_module", _fake_elk([{"name": "Mine", "path": str(tmp_path / "m.elk")}]))
    run = Foc312Runner(config={"et312": {"elk_dir": str(tmp_path)}})
    groups = run.catalog()[0]
    assert [g["label"] for g in groups] == ["Built-in modes", "Your routines", "My patterns"]   # My patterns: empty
    rid = groups[1]["items"][0]["id"]
    run.set_pattern(rid)
    assert run.pattern["name"] == "Mine"
    with pytest.raises(Foc312Error):
        run.set_pattern("elk:doesnotexist")
    with pytest.raises(Foc312Error):
        run.set_pattern("C:/Windows/evil.elk")


def test_levels_are_independent():
    run = Foc312Runner()
    run.set_levels(a=0.7)
    assert run.levels == [0.7, 0.0]
    run.set_levels(b=0.2)
    assert run.levels == [0.7, 0.2]
    assert "link" not in run.state()


# ---------------------------------------------------------------- a recording fake box

class FakeBox:
    """Acks like firmware 1.3.2 on a V4; `fork=True` reports the fork v1 version comment, `fork=N` (N >= 2) vN's.
    Records axis moves."""

    def __init__(self, t: MemoryTransport, fork: bool):
        self.t, self.fork = t, fork
        self.moves: list[tuple[float, int, float, int]] = []
        self.modes: list[int] = []
        self._task = None

    def start(self):
        self._task = asyncio.ensure_future(self.serve())

    def stop(self):
        if self._task:
            self._task.cancel()

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
                        stm32_firmware_version_2=FirmwareVersion(
                            major=1, minor=3, revision=2, branch="main",
                            comment=(f"{F.FORK_COMMENT} v{int(self.fork)}" if self.fork is not True and int(self.fork) >= 2
                                     else F.FORK_COMMENT) if self.fork else "")))
                elif which == "request_capabilities_get":
                    r.response_capabilities_get.CopyFrom(ResponseCapabilitiesGet(
                        threephase=True, fourphase=True, battery=True, device_volume=True,
                        maximum_waveform_amplitude_amps=0.2, lsm6dsox=True))
                elif which == "request_lsm6dsox_start":
                    r.response_lsm6dsox_start.CopyFrom(ResponseLSM6DSOXStart(acc_sensitivity=0.1, gyr_sensitivity=1))
                elif which == "request_signal_start":
                    self.modes.append(req.request_signal_start.mode)
                    r.response_signal_start.CopyFrom(ResponseSignalStart())
                elif which == "request_signal_stop":
                    r.response_signal_stop.CopyFrom(ResponseSignalStop())
                elif which == "request_axis_move_to":
                    m = req.request_axis_move_to
                    self.moves.append((time.monotonic(), int(m.axis), m.value, m.interval))
                    r.response_axis_move_to.CopyFrom(ResponseAxisMoveTo())
                else:
                    continue
                if self.t.is_open:
                    feed(self.t, RpcMessage(response=r))

    def axis(self, axis):
        return [m for m in self.moves if m[1] == axis]


async def box_engine(tmp_path, fork: bool, mode="fourphase", **safety):
    t = MemoryTransport()
    box = FakeBox(t, fork)
    box.start()
    eng = Engine(make_config(**safety), FocStimClient(t), SessionLogger(tmp_path / "sessions", name="t"))
    await eng.start(mode)
    return box, eng


async def pump(run: Foc312Runner, seconds: float, hb: bool = True):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if hb:
            run.heartbeat()
        run.advance()
        await asyncio.sleep(0.02)


# ---------------------------------------------------------------- fork firmware stream

@run_async
async def test_engine_refuses_biphasic_on_stock_firmware(tmp_path):
    t = MemoryTransport()
    box = FakeBox(t, fork=False)
    box.start()
    eng = Engine(make_config(), FocStimClient(t), SessionLogger(tmp_path / "s", name="t"))
    with pytest.raises(EngineError, match="fork firmware"):
        await eng.start("biphasic")
    box.stop()


@pytest.mark.needs_et312_data
@run_async
async def test_fork_axis_stream(tmp_path):
    box, eng = await box_engine(tmp_path, fork=True, slow_start_s=0.2, deadman_silence_s=5.0)
    run = Foc312Runner(eng, make_config())
    await run.set_output("fork")
    assert eng.mode == "biphasic" and box.modes[-1] == F.OUTPUT_BIPHASIC_PAIRS
    run.set_levels(0.8, 0.6)
    run.arm()
    await pump(run, 0.5)
    run.set_route(0, 23)
    run.set_route(1, 41)
    await pump(run, 0.3)
    run.reverse(0)                                           # 23 -> 32
    await pump(run, 0.3)

    for ax in F.ROUTE_AXES + F.POLARITY_AXES:
        moves = box.axis(ax)
        assert moves, f"axis {ax} was sent"
        assert all(m[3] == 0 for m in moves), f"axis {ax} always interval 0"
    assert all(m[2] == 0.0 for ax in F.POLARITY_AXES for m in box.axis(ax)), "polarity stays 0"
    a_routes = [int(m[2]) for m in box.axis(F.AXIS_BIPHASIC_A_ROUTE)]
    b_routes = [int(m[2]) for m in box.axis(F.AXIS_BIPHASIC_B_ROUTE)]
    assert a_routes[0] == 12 and 23 in a_routes and a_routes[-1] == 32
    assert b_routes[0] == 34 and b_routes[-1] == 41
    amps = [m[2] for ax in F.AMP_AXES for m in box.axis(ax)]
    assert max(amps) > 0.0
    assert max(amps) <= eng.safety.amps_cap + 1e-9
    widths = [m[2] for ax in F.WIDTH_AXES for m in box.axis(ax)]
    freqs = [m[2] for ax in F.FREQ_AXES for m in box.axis(ax)]
    assert all(F.WIDTH_RANGE[0] <= w <= F.WIDTH_RANGE[1] for w in widths)
    assert all(F.FREQ_RANGE[0] <= f <= F.FREQ_RANGE[1] for f in freqs)

    run.stop_output()
    await asyncio.sleep(0.1)
    assert all(eng.last_values[a] == 0.0 for a in F.AMP_AXES), "STOP zeroes both channels"
    await eng.stop()
    box.stop()


@run_async
async def test_fork_pulse_shape_is_logged(tmp_path):
    # the trip report is read against what was playing: width / shape / route / asymmetry / rate go to commands.jsonl
    import json
    box, eng = await box_engine(tmp_path, fork=True, slow_start_s=0.2, deadman_silence_s=5.0)
    run = Foc312Runner(eng, make_config())
    await run.set_output("fork")
    run.set_levels(0.5, 0.5)
    run.arm()
    await pump(run, 0.3)
    run.set_route(0, 23)
    await pump(run, 0.3)
    await eng.stop()
    box.stop()
    recs = [json.loads(line) for f in (tmp_path / "sessions").rglob("commands.jsonl") for line in f.open()]
    pulse = [r for r in recs if r.get("kind") == "pulse"]
    assert pulse, "pulse-shape records logged"
    a = [r for r in pulse if r["channel"] == "a"]
    assert any("width_us" in r and "asymmetry" in r and "rate_hz" in r for r in a)   # (shape: fork v2+ only)
    assert [int(r["route"]) for r in a if "route" in r][-1] == 23

@pytest.mark.needs_et312_data
@run_async
async def test_setup_survives_an_engine_restart_with_levels_zero_and_disarmed(tmp_path):
    # after a trip + power cycle the supervisor restarts the engine: same pattern/output/routes/shape/ma, levels 0,
    # NOT armed (PlaStim, 2026-09-27)
    state = tmp_path / "foc312-state.json"
    box, eng = await box_engine(tmp_path, fork=True, slow_start_s=0.2, deadman_silence_s=5.0)
    run = Foc312Runner(eng, make_config(), state_path=state)
    await run.start()
    await run.set_output("fork")
    run.set_pattern("builtin:stroke")
    run.set_route(0, 21)
    run.set_ma(0.7)
    run.set_levels(0.6, 0.4)
    run.arm()
    run._save_state()
    await run.stop()
    await eng.stop()
    box.stop()

    box2, eng2 = await box_engine(tmp_path, fork=True, slow_start_s=0.2, deadman_silence_s=5.0)
    run2 = Foc312Runner(eng2, make_config(), state_path=state)
    await run2.start()
    await run2.restore_setup()
    try:
        assert run2.pattern["id"] == "builtin:stroke"
        assert run2.output == "fork" and eng2.mode == "biphasic"
        assert run2.routes[0] == 21
        assert abs(run2.ma - 0.7) < 1e-9
        assert run2.levels == [0.0, 0.0]
        assert not eng2.armed
    finally:
        await run2.stop()
        await eng2.stop()
        box2.stop()


def test_restore_without_a_box_skips_the_output(tmp_path):
    import json
    state = tmp_path / "foc312-state.json"
    state.write_text(json.dumps({"pads": [True, True, False, False],
                                 "setup": {"pattern": "builtin:waves", "output": "fork", "routes": [12, 34],
                                           "shape": "soft", "ma": 0.3, "power": "low"}}))
    run = Foc312Runner(None, make_config(), state_path=state)
    asyncio.run(run.restore_setup())
    assert run.output == "preview" and run.power == "low" and run.pads == [True, True, False, False]
    assert run.levels == [0.0, 0.0]


@pytest.mark.needs_et312_data
def test_skip_mode_ramp_starts_patterns_at_full_level(tmp_path):
    run = Foc312Runner(None, make_config(), state_path=tmp_path / "s.json")
    run.set_pattern("builtin:stroke")
    assert run.et.vm.mem[0x9C] < run.et.vm.mem[0x9E]          # the box: a mode change restarts the ramp
    run.set_skip_mode_ramp(True)
    run.set_pattern("builtin:climb")
    m = run.et.vm.mem
    assert m[0x9C] == m[0x9E] and m[0x19C] == m[0x19E]        # full ramp at once, both channels
    run._save_state()
    again = Foc312Runner(None, make_config(), state_path=tmp_path / "s.json")
    import asyncio as _a
    _a.run(again.restore_setup())
    assert again.skip_mode_ramp and again.et.skip_mode_ramp

@run_async
async def test_fork_output_needs_arm(tmp_path):
    box, eng = await box_engine(tmp_path, fork=True, deadman_silence_s=5.0)
    run = Foc312Runner(eng, make_config())
    await run.set_output("fork")
    run.set_levels(1.0, 1.0)
    await pump(run, 0.3)
    assert all(m[2] == 0.0 for ax in F.AMP_AXES for m in box.axis(ax)), "nothing flows until ARM"
    await eng.stop()
    box.stop()


@pytest.mark.needs_et312_data
@run_async
async def test_deadman_when_the_page_goes_quiet(tmp_path):
    box, eng = await box_engine(tmp_path, fork=True, slow_start_s=0.1, deadman_silence_s=0.3, deadman_ramp_down_s=0.3)
    run = Foc312Runner(eng, make_config())
    await run.set_output("fork")
    run.set_pattern("builtin:intense")
    run.set_levels(1.0, 1.0)
    run.arm()
    await pump(run, 0.4, hb=True)
    assert max(eng.last_values[a] for a in F.AMP_AXES) > 0.0
    await pump(run, 1.0, hb=False)                  # runner keeps writing (source internal); nobody heartbeats
    assert eng.status()["deadman_active"]
    assert all(eng.last_values[a] == 0.0 for a in F.AMP_AXES), "faded to zero"
    await pump(run, 0.2, hb=True)                   # page back: deadman clears
    assert not eng.status()["deadman_active"]
    await eng.stop()
    box.stop()


# ---------------------------------------------------------------- stock firmware: not played

@run_async
async def test_stock_firmware_is_not_played(tmp_path):
    box, eng = await box_engine(tmp_path, fork=False, deadman_silence_s=5.0)
    run = Foc312Runner(eng, make_config())
    assert run.outputs_available() == {"preview": True, "fork": False}
    with pytest.raises(Foc312Error, match="fork firmware"):
        await run.set_output("fork")
    with pytest.raises(Foc312Error, match="output must be"):
        await run.set_output("stock")
    with pytest.raises(Foc312Error, match="fork firmware"):
        run.arm()
    await run.start()
    await asyncio.sleep(0.1)
    assert run.output == "preview", "a stock box is never picked"
    await run.stop()
    await eng.stop()
    box.stop()


@run_async
async def test_a_fork_box_is_picked_by_itself(tmp_path):
    box, eng = await box_engine(tmp_path, fork=True, deadman_silence_s=5.0)
    run = Foc312Runner(eng, make_config())
    assert run.output == "preview"
    await run.start()
    for _ in range(50):
        if run.output == "fork":
            break
        await asyncio.sleep(0.02)
    assert run.output == "fork" and not eng.status()["armed"]
    await run.stop()
    await eng.stop()
    box.stop()


def test_preview_has_no_device_and_cannot_arm():
    run = Foc312Runner()
    assert run.outputs_available() == {"preview": True, "fork": False}
    assert run.capabilities()["routing"] is True
    with pytest.raises(Foc312Error):
        run.arm()
    assert run.state()["knob"]["value"] is None


# ---------------------------------------------------------------- HTTP / WS API

@pytest.mark.needs_et312_data
@run_async
async def test_http_api_preview(monkeypatch):
    monkeypatch.setattr(FOC, "_elk_module", lambda: None)
    api = Foc312API(None, {})
    client = TestClient(TestServer(api.app))
    await client.start_server()
    try:
        r = await client.get("/patterns")
        d = await r.json()
        assert len([i for i in d["groups"][0]["items"] if i["note"] != "PlaStim variant"]) == 18
        r = await client.post("/cmd", json={"cmd": "route", "ch": "a", "code": 22})
        assert r.status == 400 and "different" in (await r.json())["error"]
        r = await client.post("/cmd", json={"cmd": "routes", "a": 23, "b": 41})
        assert r.status == 200
        r = await client.post("/cmd", json={"cmd": "reverse", "ch": "b"})
        assert (await r.json())["code"] == 14
        r = await client.post("/cmd", json={"cmd": "arm"})
        assert r.status == 400
        r = await client.post("/cmd", json={"cmd": "output", "mode": "fork"})
        assert r.status == 400
        r = await client.get("/state")
        s = await r.json()
        assert s["routes"] == [23, 14] and s["output"] == "preview" and len(s["advanced_ranges"]) == 8
        r = await client.get("/")
        assert r.status == 200 and "Player" in await r.text()
        ws = await client.ws_connect("/ws")
        first = await ws.receive_json()
        assert first["type"] == "state" and first["full"]
        await ws.send_json({"cmd": "levels", "a": 0.3, "seq": 1})
        for _ in range(10):
            m = await ws.receive_json()
            if m["type"] == "result":
                assert m["ok"]
                break
        await ws.close()
    finally:
        await client.close()
        await api.runner.stop()


# ---------------------------------------------------------------- fork firmware v2: fractional widths, pulse shapes

def test_fork_version_detection():
    assert F.fork_version_of_comment("") == 0
    assert F.fork_version_of_comment(None) == 0
    assert F.fork_version_of_comment("something else") == 0
    assert F.fork_version_of_comment("stim-engine biphasic-pairs") == 1
    assert F.fork_version_of_comment("stim-engine biphasic-pairs v2") == 2
    assert F.fork_version_of_comment("stim-engine biphasic-pairs v9-dev") == 1   # unknown suffix: v1 capabilities
    assert F.fork_version_of_comment("stim-engine biphasic-pairs v3") == 3        # v3: v2 host features (>= 2)


def test_shape_charge_helpers():
    w = 150.0                                           # 7.5 samples
    assert F.phase_charge(F.SHAPE_SQUARE, w) == pytest.approx(7.5)
    assert F.phase_charge(F.SHAPE_ROUNDED, w) == pytest.approx(2 * 7.5 / 3.141592653589793)
    assert F.phase_charge(F.SHAPE_SOFT, w) == pytest.approx(6.5)
    assert F.phase_charge(F.SHAPE_SOFT, 30.0) == pytest.approx(0.75)        # triangle under 40 us: w/2
    for shape in F.SHAPES:
        for width in (40.0, 55.5, 150.0, 400.0):
            q = F.phase_charge(shape, width) * F.shape_charge_factor(shape, width)
            assert q == pytest.approx(F.phase_charge(F.SHAPE_ROUNDED, width))
    assert F.shape_id("Square") == 1 and F.shape_id("soft square") == 2 and F.shape_id(0) == 0
    with pytest.raises(ValueError):
        F.shape_id("zigzag")


@run_async
async def test_fork_v1_stream_is_unchanged(tmp_path):
    box, eng = await box_engine(tmp_path, fork=True, mode="biphasic", slow_start_s=0.05, deadman_silence_s=5.0)
    assert eng.fork_version == 1 and eng.status()["fork_version"] == 1
    eng.set_biphasic(0, intensity=0.5, width_us=55.0, shape="square")       # shape ignored on v1
    eng.set_master(1.0)
    eng.arm()
    await asyncio.sleep(0.3)
    widths = [m[2] for m in box.axis(F.AXIS_BIPHASIC_A_PHASE_WIDTH_US)]
    assert widths and all(w % 20 == 0 for w in widths), "v1: widths on the 20 us grid"
    assert widths[-1] == 60.0
    for ax in F.SHAPE_AXES:
        assert not box.axis(ax), "v1 never gets shape axes"
    amps = eng.last_values[F.AXIS_BIPHASIC_A_AMPLITUDE_AMPS]
    assert amps == pytest.approx(0.5 * eng.safety.amps_cap * 55.0 / 60.0, rel=1e-3)
    await eng.stop()
    box.stop()


@run_async
async def test_fork_v2_unsnapped_widths_shapes_and_charge_matching(tmp_path):
    box, eng = await box_engine(tmp_path, fork=2, mode="biphasic", slow_start_s=0.05, deadman_silence_s=5.0)
    assert eng.fork_version == 2
    eng.set_biphasic(0, intensity=0.4, width_us=55.3)
    eng.set_master(1.0)
    eng.arm()
    await asyncio.sleep(0.3)
    assert box.axis(F.AXIS_BIPHASIC_A_PHASE_WIDTH_US)[-1][2] == pytest.approx(55.3, abs=1e-4)
    cap = eng.safety.amps_cap
    q_amps = {}
    for shape in ("rounded", "square", "soft"):
        eng.set_biphasic(0, shape=shape)
        await asyncio.sleep(0.6)                    # past the shape-change fade (0.15 s out + 0.3 s in)
        amps = eng.last_values[F.AXIS_BIPHASIC_A_AMPLITUDE_AMPS]
        q_amps[shape] = amps * F.phase_charge(F.shape_id(shape), 55.3)
        assert box.axis(F.AXIS_BIPHASIC_A_SHAPE)[-1][2] == float(F.shape_id(shape))
    assert q_amps["rounded"] == pytest.approx(0.4 * cap * F.phase_charge(0, 55.3), rel=1e-3)
    assert q_amps["square"] == pytest.approx(q_amps["rounded"], rel=1e-3), "equal charge across shapes"
    assert q_amps["soft"] == pytest.approx(q_amps["rounded"], rel=1e-3)
    for ax in F.SHAPE_AXES:
        moves = box.axis(ax)
        assert moves and all(m[3] == 0 for m in moves), "shape switches in one step (interval 0)"
    # the factor never lifts a pulse above the cap: rounded -> soft at a narrow width wants > 1x, it clips
    eng.set_biphasic(0, intensity=1.0, width_us=40.0, shape="soft")
    await asyncio.sleep(0.6)
    assert F.shape_charge_factor(F.SHAPE_SOFT, 40.0) > 1.0
    assert eng.last_values[F.AXIS_BIPHASIC_A_AMPLITUDE_AMPS] == pytest.approx(cap)
    await eng.stop()
    box.stop()


@run_async
async def test_runner_shape_control_needs_v2(tmp_path):
    box, eng = await box_engine(tmp_path, fork=True, deadman_silence_s=5.0)
    run = Foc312Runner(eng, make_config())
    assert run.set_shape("square") == "square"                       # preview: animation only
    await run.set_output("fork")
    assert run.capabilities()["pulse_shape"] is False
    with pytest.raises(Foc312Error, match="v2"):
        run.set_shape("soft")
    await eng.stop()
    box.stop()

    box, eng = await box_engine(tmp_path, fork=2, deadman_silence_s=5.0)
    run = Foc312Runner(eng, make_config())
    await run.set_output("fork")
    assert run.capabilities()["pulse_shape"] is True
    assert run.set_shape("soft") == "soft"
    assert run.state()["shape"] == "soft" and run.state()["a"]["shape"] == "soft"
    assert eng.biphasic[0]["shape"] == 2 and eng.biphasic[1]["shape"] == 2
    assert run.engine_view()["fork_version"] == 2
    await eng.stop()
    box.stop()


@run_async
async def test_routes_preset_34_12(tmp_path):
    html = (Path(__file__).resolve().parents[1] / "player" / "index.html").read_text(encoding="utf-8")
    assert 'data-preset="34,12"' in html
    api = Foc312API(None, {})
    res = await api.apply({"cmd": "routes", "a": 34, "b": 12})
    assert res["ok"] and api.runner.routes == [34, 12]


@run_async
async def test_v2_shape_change_fades_out_and_in(tmp_path):
    """A shape change never jumps: the old shape fades to 0, the new shape is sent at ~0 and fades back in."""
    box, eng = await box_engine(tmp_path, fork=2, mode="biphasic", slow_start_s=0.05, deadman_silence_s=5.0)
    eng.set_biphasic(0, intensity=0.4, width_us=150.0)
    eng.set_master(1.0)
    eng.arm()
    await asyncio.sleep(0.4)
    full = eng.last_values[F.AXIS_BIPHASIC_A_AMPLITUDE_AMPS]
    n0 = len(box.axis(F.AXIS_BIPHASIC_A_AMPLITUDE_AMPS))
    eng.set_biphasic(0, shape="square")
    await asyncio.sleep(0.7)
    amps = [m[2] for m in box.axis(F.AXIS_BIPHASIC_A_AMPLITUDE_AMPS)[n0:]]
    shapes = box.axis(F.AXIS_BIPHASIC_A_SHAPE)
    assert min(amps) < 0.1 * full, "fades (nearly) to zero across the switch"
    target = 0.4 * eng.safety.amps_cap * F.shape_charge_factor(F.SHAPE_SQUARE, 150.0)
    assert amps[-1] == pytest.approx(target, rel=1e-3), "back to the charge-matched level"
    assert max(amps) <= full + 1e-9, "never above the pre-switch level"
    # the square shape is only sent once the old shape has faded out
    sq = [m for m in shapes if m[2] == float(F.SHAPE_SQUARE)]
    assert sq, "square was sent"
    await eng.stop()
    box.stop()


# ---------------------------------------------------------------- connected-pads guard

@pytest.mark.needs_et312_data
@run_async
async def test_route_on_unconnected_pad_is_silent_and_ramps_back(tmp_path):
    """Pads on 1-2 only, preset 34/12: A (3-4) must send 0 A while B (1-2) plays; reconnecting ramps A in."""
    box, eng = await box_engine(tmp_path, fork=2, slow_start_s=0.1, deadman_silence_s=5.0)
    run = Foc312Runner(eng, make_config())
    await run.set_output("fork")
    run.set_pattern("builtin:intense")
    run.set_pads([True, True, False, False])
    run.set_route(0, 34)                                     # accepted: presets still work
    run.set_route(1, 12)
    assert run.routes == [34, 12]
    assert run.blocked(0) == "pads 3 and 4 not connected" and run.blocked(1) is None
    run.set_levels(0.8, 0.8)
    run.arm()
    await pump(run, 0.8)
    a_amps = [m[2] for m in box.axis(F.AXIS_BIPHASIC_A_AMPLITUDE_AMPS)]
    assert a_amps and max(a_amps) == 0.0, "blocked channel never sends current"
    assert [int(m[2]) for m in box.axis(F.AXIS_BIPHASIC_A_ROUTE)][-1] == 34, "the route itself is still sent"
    assert max(m[2] for m in box.axis(F.AXIS_BIPHASIC_B_AMPLITUDE_AMPS)) > 0.0, "the other channel is unaffected"
    st = run.state()
    assert st["a"]["blocked"] and st["b"]["blocked"] is None and st["pads"] == [True, True, False, False]

    run.set_pad(3, True)
    assert run.blocked(0) == "pad 4 not connected"
    run.set_pad(4, True)
    assert run.blocked(0) is None
    await pump(run, 0.3)
    assert 0.0 < run._pad_gain[0] < 0.6, "ramps in, no jump to full level"
    await pump(run, 1.0)
    assert run._pad_gain[0] == 1.0
    assert max(m[2] for m in box.axis(F.AXIS_BIPHASIC_A_AMPLITUDE_AMPS)) > 0.0

    run.set_pad(3, False)                                     # blocking again is immediate
    assert run._pad_gain[0] == 0.0 and eng.biphasic[0]["intensity"] == 0.0
    await pump(run, 0.1)                                      # next engine transmit carries it
    assert eng.last_values[F.AXIS_BIPHASIC_A_AMPLITUDE_AMPS] == 0.0
    await eng.stop()
    box.stop()


def test_pads_persist_across_restarts(tmp_path):
    path = tmp_path / "foc312-state.json"
    run = Foc312Runner(None, state_path=path)
    assert run.pads == [True, True, True, True]
    run.set_pads([1, 1, 0, 0])
    assert path.exists()
    again = Foc312Runner(None, state_path=path)
    assert again.pads == [True, True, False, False]
    assert again._pad_gain == [1.0, 0.0], "B's default route 34 starts blocked"
    with pytest.raises(Foc312Error):
        again.set_pads([1, 1, 1])
    with pytest.raises(Foc312Error):
        again.set_pad(5, True)
    path.write_text("not json", encoding="utf-8")
    assert Foc312Runner(None, state_path=path).pads == [True, True, True, True], "bad file = defaults"


@pytest.mark.needs_et312_data
def test_stroke_plays_monophasic_with_alternating_lead_on_the_fork():
    """ET-312 Stroke = monophasic pulses whose driven half flips at each ramp reversal (gate 0x05 <-> 0x03).
    On the fork that is the asymmetric pulse on the SAME pads, lead electrode swapped by route digit order."""
    run = Foc312Runner()
    run.set_pattern("builtin:stroke")
    run.set_levels(0.5, 0.5)
    sent, asym = set(), set()
    for _ in range(8 * 50):                          # 8 s at 20 ms steps
        run.advance(0.02)
        v = run.channel_view(0)
        sent.add(v["route_sent"])
        asym.add(v["asymmetry"])
        assert v["biphasic"] is False
    assert sent == {12, 21}, "lead flips between electrode 1 and 2 on the same pair"
    assert asym == {run.monophasic_asymmetry}


# ---- fork v7 shapes: triangle and the continuous taper ---------------------------------------------------------
def test_v7_shape_charges_match_the_firmware_integrals():
    import sys
    from stimengine.paths import FIRMWARE_DIR
    if not (FIRMWARE_DIR / "sim" / "biphasic_rc_sim.py").exists():
        pytest.skip("needs the foc312 firmware project next to this one (its sim/)")
    sys.path.insert(0, str(FIRMWARE_DIR / "sim"))
    from biphasic_rc_sim import shape_integral       # the firmware's shape_integral, mirrored (tests/test_fork_lc_sim)
    for width in (40.0, 55.5, 130.0, 255.0, 400.0):
        w = width / 20.0
        assert F.phase_charge(F.SHAPE_TRIANGLE, width) == pytest.approx(shape_integral(3, w, w))
        for sid in range(F.SHAPE_TAPER_0, F.SHAPE_TAPER_1 + 1):
            assert F.phase_charge(sid, width) == pytest.approx(shape_integral(F.shape_axis_value(sid), w, w))
        # the taper's ends are the old shapes exactly
        assert F.phase_charge(F.SHAPE_TAPER_0, width) == pytest.approx(F.phase_charge(F.SHAPE_ROUNDED, width))
        assert F.phase_charge(F.SHAPE_TAPER_1, width) == pytest.approx(F.phase_charge(F.SHAPE_SQUARE, width))


def test_v7_shape_names_axis_values_and_versions():
    assert F.shape_id("triangle") == 3 and F.shape_id(3) == 3
    assert F.shape_id("taper50") == 45 and F.shape_id("Taper 50") == 45 and F.shape_id("taper:0.5") == 45
    assert F.shape_id("taper0") == 40 and F.shape_id("taper100") == 50 and F.shape_id(47) == 47
    assert F.SHAPES[45] == "taper50"
    assert F.shape_axis_value(3) == 3.0 and F.shape_axis_value(45) == 4.5 and F.shape_axis_value(2) == 2.0
    assert [F.shape_min_fork(s) for s in (0, 1, 2, 3, 40, 50)] == [2, 2, 2, 7, 7, 7]
    for bad in ("taper150", "taper:x", 4, 39, 51):
        with pytest.raises(ValueError):
            F.shape_id(bad)


@run_async
async def test_v7_shapes_never_reach_an_older_fork(tmp_path):
    # an older fork clamps an unknown shape to soft square: more charge than the host planned, so it plays rounded
    for fork, want in ((6, {"triangle": 0.0, "taper50": 0.0}), (7, {"triangle": 3.0, "taper50": 4.5})):
        box, eng = await box_engine(tmp_path, fork=fork, mode="biphasic", slow_start_s=0.05, deadman_silence_s=5.0)
        eng.set_biphasic(0, intensity=0.4, width_us=130.0)
        eng.set_master(1.0)
        eng.arm()
        await asyncio.sleep(0.3)
        cap = eng.safety.amps_cap
        for shape, axis_value in want.items():
            eng.set_biphasic(0, shape=shape)
            await asyncio.sleep(0.6)
            moves = box.axis(F.AXIS_BIPHASIC_A_SHAPE)
            assert (moves[-1][2] if moves else 0.0) == axis_value, (fork, shape)   # never sent = rounded
            played = 0 if axis_value == 0.0 else F.shape_id(shape)
            q = eng.last_values[F.AXIS_BIPHASIC_A_AMPLITUDE_AMPS] * F.phase_charge(played, 130.0)
            assert q == pytest.approx(0.4 * cap * F.phase_charge(0, 130.0), rel=1e-3), "equal charge"
        await eng.stop()
        box.stop()
        run = Foc312Runner(eng, make_config())
        if fork < 7:
            box, eng = await box_engine(tmp_path, fork=fork, deadman_silence_s=5.0)
            run = Foc312Runner(eng, make_config())
            await run.set_output("fork")
            with pytest.raises(Foc312Error, match="v7"):
                run.set_shape("triangle")
            assert run.set_shape("soft") == "soft"
            await eng.stop()
            box.stop()


# ---------------------------------------------------------------- the player's Master

@pytest.mark.needs_et312_data
@run_async
async def test_master_arm_ramps_to_it_lowering_is_instant_raising_slow_starts(tmp_path):
    box, eng = await box_engine(tmp_path, fork=True, slow_start_s=1.0, deadman_silence_s=5.0)
    run = Foc312Runner(eng, make_config())
    await run.set_output("fork")
    run.set_pattern("builtin:waves")
    run.set_levels(1.0, 1.0)
    run.set_master(0.5)
    assert eng._master_now == 0.0, "setting Master before ARM releases nothing"
    run.arm()
    await pump(run, 1.5, hb=True)
    assert eng._master_now == pytest.approx(0.5), "ARM ramps up to the player's Master, not to 100 %"
    run.set_master(0.2)
    await pump(run, 0.05, hb=True)
    assert eng._master_now == pytest.approx(0.2), "lowering Master is instant"
    run.set_master(1.0)
    await pump(run, 0.25, hb=True)
    assert 0.2 < eng._master_now < 0.6, "raising Master rises at the slow-start rate, not at once"
    assert run.state()["master_set"] == 1.0
    run.stop_output()
    await asyncio.sleep(0.05)
    assert eng._master_now == 0.0, "STOP still zeroes it"
    await eng.stop()
    box.stop()


@pytest.mark.needs_et312_data
def test_swap_trades_the_routes_and_each_wire_pair_keeps_its_level():
    run = Foc312Runner()
    run.set_levels(0.7, 0.2)
    run.routes = [13, 24]
    assert run.swap() == [24, 13]
    assert run.levels == [0.2, 0.7], "the level follows its wires"
    assert run.swap() == [13, 24] and run.levels == [0.7, 0.2]


@run_async
async def test_measured_current_per_channel_is_the_remotes_figure(tmp_path):
    import time as _t
    box, eng = await box_engine(tmp_path, fork=True, deadman_silence_s=5.0)
    run = Foc312Runner(eng, make_config())
    run.routes = [13, 24]
    t = eng.client.telemetry
    t.peak, t.currents_at = (0.050, 0.030, 0.020, 0.045), _t.monotonic()
    assert run.engine_view()["measured"] == [0.020, 0.030], "the smaller of each route's two electrodes"
    t.currents_at = _t.monotonic() - 5
    assert run.engine_view()["measured"] == [None, None], "stale -> unknown"
    await eng.stop()
    box.stop()
