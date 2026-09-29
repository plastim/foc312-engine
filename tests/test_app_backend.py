"""The PC app hub's backend (stimengine/app): API with fakes only. Nothing here opens a serial port or flashes."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from stimengine.app import devices, firmware, jobs as jobs_mod
from stimengine.app.engine_proc import EngineProc
from stimengine.app.jobs import JobManager
from stimengine.app.server import Hub


def run(coro):
    return asyncio.run(coro)


async def client_for(hub: Hub) -> TestClient:
    c = TestClient(TestServer(hub.app))
    await c.start_server()
    return c


def fake_ports(monkeypatch, entries):
    ports = [SimpleNamespace(device=d, serial_number=s, vid=v, pid=0x1001) for d, s, v in entries]
    monkeypatch.setattr(devices.list_ports, "comports", lambda: ports)
    devices._cache.clear()


@pytest.fixture
def manifests(tmp_path, monkeypatch):
    """A box and a remote manifest in tmp_path: one good image each, one with a wrong hash, one missing file."""
    box_dir, rem_dir = tmp_path / "box", tmp_path / "rem"
    box_dir.mkdir()
    rem_dir.mkdir()
    (box_dir / "good.hex").write_bytes(b":00000001FF\n")
    (box_dir / "bad.hex").write_bytes(b"tampered")
    (rem_dir / "r.bin").write_bytes(b"\xe9" * 64)
    good = hashlib.sha256((box_dir / "good.hex").read_bytes()).hexdigest()
    rsha = hashlib.sha256((rem_dir / "r.bin").read_bytes()).hexdigest()
    (box_dir / "manifest.json").write_text(json.dumps({"images": [
        {"id": "good", "name": "Good", "version": "v7", "file": "good.hex", "sha256": good, "recommended": True},
        {"id": "bad", "name": "Bad", "file": "bad.hex", "sha256": "00" * 32},
        {"id": "gone", "name": "Gone", "file": "gone.hex", "sha256": "11" * 32}]}))
    (rem_dir / "manifest.json").write_text(json.dumps({"images": [
        {"id": "r1", "name": "Remote", "file": "r.bin", "sha256": rsha}]}))
    monkeypatch.setattr(firmware, "BOX_MANIFEST", box_dir / "manifest.json")
    monkeypatch.setattr(firmware, "REMOTE_MANIFEST", rem_dir / "manifest.json")
    return box_dir, rem_dir


class FakeEngine(EngineProc):
    """Owns a port without a process."""
    def __init__(self, port=None):
        super().__init__()
        self._fake_port = port

    def running(self):
        return self._fake_port is not None

    @property
    def port(self):
        return self._fake_port

    @port.setter
    def port(self, v):
        pass


class RecordingJobs(JobManager):
    """Records the command instead of running it."""
    def __init__(self):
        super().__init__()
        self.cmds = []

    def start(self, kind, cmd, flash=False):
        self.cmds.append((kind, cmd, flash))
        return SimpleNamespace(id=len(self.cmds))


# ---- devices ---------------------------------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def isolated_devices(monkeypatch, tmp_path):
    # never read or write the real config: remembered kinds and box names come from here
    monkeypatch.setattr(devices, "KNOWN_FILE", tmp_path / "devices.json")
    monkeypatch.setattr(devices, "_box_names", lambda: {"02:ab:cd:00:00:02": "box 2"})
    devices._cache.clear()


def test_devices_are_listed_without_opening_ports(monkeypatch):
    fake_ports(monkeypatch, [("COM17", "02:AB:CD:00:00:02", 0x303A), ("COM3", None, None), ("COM18", "02:AB", 0x303A)])
    monkeypatch.setattr(devices, "probe", lambda port: (_ for _ in ()).throw(AssertionError("probed")))

    async def go():
        c = await client_for(Hub(engine=FakeEngine("COM17")))
        try:
            d = await (await c.get("/api/devices")).json()
        finally:
            await c.close()
        return d["devices"]
    devs = run(go())
    assert [d["port"] for d in devs] == ["COM17", "COM18"]            # a port without a VID is not a USB device
    assert devs[0] == {"port": "COM17", "serial": "02:AB:CD:00:00:02", "vid": "303a", "pid": "1001",
                       "in_use": True, "kind": "box", "detail": "engine connected", "name": "box 2",
                       "fw_fork": None, "fw_label": ""}
    assert devs[1]["kind"] == "unknown" and not devs[1]["in_use"]


def test_probing_is_cached_and_skips_the_engines_port(monkeypatch):
    fake_ports(monkeypatch, [("COM17", "A", 0x303A), ("COM18", "B", 0x303A)])
    probed = []

    async def fake_probe(port):
        probed.append(port)
        return "remote", "stim-remote free 3506176 B"
    monkeypatch.setattr(devices, "probe", fake_probe)

    async def go():
        c = await client_for(Hub(engine=FakeEngine("COM17")))
        try:
            first = (await (await c.get("/api/devices?probe=1")).json())["devices"]
            await c.get("/api/devices?probe=1")                         # cached: not again
            await c.get("/api/devices?probe=2")                         # forced: again
        finally:
            await c.close()
        return first
    first = run(go())
    assert probed == ["COM18", "COM18"]
    assert first[1]["kind"] == "remote" and first[1]["detail"].startswith("stim-remote")


# ---- firmware ----------------------------------------------------------------------------------------------------------
def test_firmware_listing_checks_hashes(manifests):
    lst = firmware.listing()
    box = {e["id"]: e for e in lst["box"]}
    assert "error" not in box["good"] and box["good"]["recommended"] and box["good"]["version"] == "v7"
    assert "does not match" in box["bad"]["error"]
    assert box["gone"]["error"] == "file missing"
    assert lst["remote"][0]["id"] == "r1" and "error" not in lst["remote"][0]
    with pytest.raises(ValueError):
        firmware.find("box", "bad")
    with pytest.raises(ValueError):
        firmware.find("box", "nope")


@pytest.mark.skipif(not firmware.BOX_MANIFEST.exists(), reason="no local release builds (a public checkout)")
def test_the_real_manifests_verify():
    lst = firmware.listing()
    assert lst["box"] and all("error" not in e for e in lst["box"]), lst["box"]
    assert sum(e["recommended"] for e in lst["box"]) == 1


# ---- flashing: refusals and the commands -----------------------------------------------------------------------------
def post(hub, path, body):
    async def go():
        c = await client_for(hub)
        try:
            r = await c.post(path, json=body)
            return r.status, await r.json()
        finally:
            await c.close()
    return run(go())


def test_box_flash_refusals(manifests):
    jobs = RecordingJobs()
    ok = {"port": "COM17", "image": "good", "confirm": True, "remote_off": True}
    assert post(Hub(engine=FakeEngine(), jobs=jobs), "/api/flash/box", {**ok, "confirm": False})[0] == 400
    st, d = post(Hub(engine=FakeEngine(), jobs=jobs), "/api/flash/box", {**ok, "remote_off": False})
    assert st == 400 and "remote" in d["error"]
    st, d = post(Hub(engine=FakeEngine("COM17"), jobs=jobs), "/api/flash/box", ok)
    assert st == 409 and "disconnect" in d["error"]
    assert post(Hub(engine=FakeEngine(), jobs=jobs), "/api/flash/box", {**ok, "image": "bad"})[0] == 400
    assert post(Hub(engine=FakeEngine(), jobs=jobs), "/api/flash/box", {**ok, "image": "gone"})[0] == 400
    assert jobs.cmds == []
    st, d = post(Hub(engine=FakeEngine("COM9"), jobs=jobs), "/api/flash/box", ok)   # engine on another box: fine
    assert st == 200 and d["job"] == 1
    kind, cmd, flash = jobs.cmds[0]
    box_dir, _ = manifests
    assert kind == "flash-box" and flash
    assert cmd[1].endswith("flash_focstim.py") and cmd[2] == str(box_dir / "good.hex")
    assert cmd[3:] == ["--sha256", firmware.find("box", "good")[0]["sha256"], "--port", "COM17"]


def test_remote_flash_load_and_pair(manifests, monkeypatch):
    kinds = {"COM18": "remote", "COM17": "box"}
    monkeypatch.setattr(devices, "known_kind", lambda port: kinds.get(port, "unknown"))
    jobs = RecordingJobs()
    st, d = post(Hub(engine=FakeEngine(), jobs=jobs), "/api/flash/remote", {"port": "COM18", "image": "r1"})
    assert st == 400                                                     # no confirm
    st, d = post(Hub(engine=FakeEngine("COM18"), jobs=jobs), "/api/flash/remote",
                 {"port": "COM18", "image": "r1", "confirm": True})
    assert st == 409
    st, d = post(Hub(engine=FakeEngine(), jobs=jobs), "/api/flash/remote",
                 {"port": "COM18", "image": "r1", "confirm": True})
    assert st == 200
    assert jobs.cmds[-1][1][1:] == ["-m", "esptool", "--chip", "esp32s3", "--port", "COM18", "--baud", "921600",
                                    "write-flash", "0x0", str(manifests[1] / "r.bin")]
    assert post(Hub(engine=FakeEngine(), jobs=jobs), "/api/remote/load", {"port": "COM18"})[0] == 200
    assert jobs.cmds[-1][1][1:] == ["-m", "stimengine.remote", "load", "--port", "COM18"] and not jobs.cmds[-1][2]
    assert post(Hub(engine=FakeEngine("COM17"), jobs=jobs), "/api/remote/pair", {"box_port": "COM17"})[0] == 409
    assert post(Hub(engine=FakeEngine(), jobs=jobs), "/api/remote/pair", {"box_port": "COM17", "house": True})[0] == 200
    assert jobs.cmds[-1][1][1:] == ["-m", "stimengine.remote", "pair-box", "--box-port", "COM17", "--house"]
    # remote actions only on a port detected as the M5 remote (esptool would flash a box's ESP32 just the same)
    n = len(jobs.cmds)
    for path, body in (("/api/flash/remote", {"port": "COM17", "image": "r1", "confirm": True}),
                       ("/api/remote/load", {"port": "COM17"}), ("/api/remote/load", {"port": "COM99"})):
        st, d = post(Hub(engine=FakeEngine(), jobs=jobs), path, body)
        assert st == 400 and ("not the M5 remote" in d["error"] or "press Detect" in d["error"]), (path, body, d)
    assert post(Hub(engine=FakeEngine(), jobs=jobs), "/api/remote/pair", {"box_port": "COM18"})[0] == 400
    assert post(Hub(engine=FakeEngine(), jobs=jobs), "/api/flash/box",
                {"port": "COM18", "image": "b1", "confirm": True, "remote_off": True})[0] == 400
    assert len(jobs.cmds) == n                                           # nothing started


# ---- jobs --------------------------------------------------------------------------------------------------------------
def test_job_runs_and_reports_its_output():
    jm = JobManager()
    job = jm.start("test", [sys.executable, "-c", "print('hi'); import sys; print('err', file=sys.stderr)"], flash=True)
    with pytest.raises(jobs_mod.JobBusy):
        if job.running():
            jm.start("second", [sys.executable, "-c", "pass"], flash=True)
        else:
            raise jobs_mod.JobBusy("finished too fast to test; counts as busy-refusal")
    for _ in range(100):
        if not job.running():
            break
        time.sleep(0.05)
    v = job.view()
    assert v["state"] == "ok" and v["rc"] == 0 and "hi" in v["log"] and "err" in v["log"]
    bad = jm.start("fail", [sys.executable, "-c", "raise SystemExit(3)"])
    for _ in range(100):
        if not bad.running():
            break
        time.sleep(0.05)
    assert bad.view()["state"] == "failed" and bad.rc == 3


def test_jobs_api():
    hub = Hub(engine=FakeEngine())
    job = hub.jobs.start("test", [sys.executable, "-c", "print('hi')"])
    for _ in range(100):
        if not job.running():
            break
        time.sleep(0.05)

    async def go():
        c = await client_for(hub)
        try:
            one = await (await c.get(f"/api/jobs/{job.id}")).json()
            all_ = await (await c.get("/api/jobs")).json()
            missing = (await c.get("/api/jobs/999")).status
        finally:
            await c.close()
        return one, all_, missing
    one, all_, missing = run(go())
    assert one["state"] == "ok" and one["log"] == ["hi"] and one["kind"] == "test"
    assert all_["jobs"][0]["id"] == job.id and missing == 404


# ---- the engine child ---------------------------------------------------------------------------------------------------
CHILD = """
import http.server, sys, threading
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok": true}')
        print('clean stop', flush=True)
        threading.Thread(target=srv.shutdown).start()
    def log_message(self, *a):
        pass
srv = http.server.HTTPServer(("127.0.0.1", int(sys.argv[2])), H)
print('engine up on', sys.argv[1], flush=True)
srv.serve_forever()
"""


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_engine_connect_and_disconnect():
    api = _free_port()
    eng = EngineProc(command=lambda port: [sys.executable, "-c", CHILD, port, str(api)],
                     stop_url=f"http://127.0.0.1:{api}/stop")
    hub = Hub(engine=eng)

    async def go():
        c = await client_for(hub)
        try:
            assert (await c.post("/api/engine/connect", json={})).status == 400
            r = await (await c.post("/api/engine/connect", json={"port": "COM17"})).json()
            assert r["ok"] and r["running"] and r["port"] == "COM17"
            again = await c.post("/api/engine/connect", json={"port": "COM13"})
            assert again.status == 409                                  # one engine
            for _ in range(100):
                st = await (await c.get("/api/engine")).json()
                if any("engine up" in ln for ln in st["log"]):
                    break
                await asyncio.sleep(0.05)
            assert any("engine up on COM17" in ln for ln in st["log"])
            r = await (await c.post("/api/engine/disconnect")).json()
            assert r["ok"] and not r["running"] and r["port"] is None and r["rc"] == 0
        finally:
            await c.close()
    run(go())
    assert not eng.running()
    assert any("clean stop" in ln for ln in eng.log), list(eng.log)   # stopped through its API, not killed


def test_an_engine_that_ignores_stop_is_terminated():
    eng = EngineProc(command=lambda port: [sys.executable, "-c", "import time\nwhile True: time.sleep(0.05)"],
                     stop_url=None)
    import stimengine.app.engine_proc as ep
    old = ep.STOP_WAIT_S
    ep.STOP_WAIT_S = 0.5
    try:
        eng.connect("COM17")
        assert eng.running() and eng.owns("com17") and not eng.owns("COM18")
        eng.disconnect()
    finally:
        ep.STOP_WAIT_S = old
    assert not eng.running() and eng.status()["port"] is None


def test_hotkey_actions_are_ignored_without_an_engine():
    hub = Hub(engine=FakeEngine())
    run(hub.hotkey_action("stop"))                                       # no engine: nothing happens, no error


def test_a_detected_kind_is_remembered_and_firmware_is_labelled(monkeypatch):
    fake_ports(monkeypatch, [("COM17", "02:AB:CD:00:00:02", 0x303A), ("COM18", "02:AB", 0x303A)])

    async def fake_probe(port):
        return ("box", "1.3.2 (main) stim-engine biphasic-pairs v8") if port == "COM17" else ("remote", "stim-remote free 1 B")
    monkeypatch.setattr(devices, "probe", fake_probe)
    devs = run(devices.scan(set(), 1))
    assert devs[0]["name"] == "box 2" and devs[0]["fw_fork"] == 8 and devs[0]["fw_label"] == "PlaStim fork v8"
    assert devs[1]["kind"] == "remote" and devs[1]["name"] == "M5 remote"
    # the next start (no cache, no probing) still knows them, on whatever port they come back
    devices._cache.clear()
    fake_ports(monkeypatch, [("COM9", "02:AB:CD:00:00:02", 0x303A)])
    monkeypatch.setattr(devices, "probe", lambda port: (_ for _ in ()).throw(AssertionError("probed")))
    devs = run(devices.scan(set(), 0))
    assert devs[0]["kind"] == "box" and devs[0]["fw_fork"] == 8
    assert devices.firmware_of("box", "1.3.2 (main)") == (0, "stock 1.3.2")
