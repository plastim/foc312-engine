"""The hub's M5 remote settings, pattern files, ET-312 extraction and status panel (stimengine/app): fakes and
temporary files only. Nothing here opens a serial port, starts an engine or loads a remote."""
from __future__ import annotations

import json
import tomllib
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest

from stimengine.app import m5settings, status
from stimengine.app.engine_proc import EngineProc
from stimengine.app.server import Hub
from stimengine.et312 import fwdata
from stimengine.remote import m5config, pack
from tests.test_app_backend import FakeEngine, client_for, run
from tests.test_et312_fwdata import _synthetic_image

ROOT = Path(__file__).resolve().parents[1]

M5_TOML = """[wifi]
ssid = "house"
password = "housepass1"

[direct]
enabled = true
ssid = "stim-remote"
password = "remotepass1"
channel = 6

[[box]]
name = "box 2"
mac = "02:ab:cd:00:00:02"
host = "10.0.0.51"
port = 55533
"""


@pytest.fixture
def m5files(tmp_path, monkeypatch):
    """Every file m5settings reads or writes, in a temporary folder."""
    cfg = tmp_path / "m5.toml"
    cfg.write_text(M5_TOML, encoding="utf-8")
    eng = tmp_path / "engine.toml"
    eng.write_text("[signal]\nwaveform_amplitude_amps = 0.15\n[safety]\nslow_start_s = 4.0\n", encoding="utf-8")
    monkeypatch.setattr(m5settings, "M5_TOML", cfg)
    monkeypatch.setattr(m5settings, "ENGINE_TOML", eng)
    monkeypatch.setattr(m5settings, "FOC312_STATE", tmp_path / "foc312-state.json")
    monkeypatch.setattr(m5settings, "LAST_LOAD", tmp_path / "m5-last-load.json")
    monkeypatch.setattr(m5settings, "BUILD_CONFIG", tmp_path / "build" / "config.json")
    return SimpleNamespace(cfg=cfg, eng=eng, tmp=tmp_path)


async def _req(hub, method, path, **kw):
    c = await client_for(hub)
    try:
        r = await c.request(method, path, **kw)
        return r.status, await r.json()
    finally:
        await c.close()


# ---- settings --------------------------------------------------------------------------------------------------------
def test_settings_are_shown_without_passwords(m5files):
    st, d = run(_req(Hub(engine=FakeEngine()), "GET", "/api/remote/settings"))
    assert st == 200
    assert "housepass1" not in json.dumps(d) and "remotepass1" not in json.dumps(d)
    assert d["wifi"] == {"ssid": "house", "has_password": True}
    assert d["direct"] == {"enabled": True, "ssid": "stim-remote", "has_password": True, "channel": 6}
    assert d["boxes"] == [{"name": "box 2", "mac": "02:ab:cd:00:00:02", "host": "10.0.0.51", "port": 55533}]
    assert d["remote_gets"]["wifi"] == {"mode": "direct", "ssid": "stim-remote", "channel": 6}   # no password
    assert d["remote_gets"]["safety"]["amps_cap"] == 0.15 and d["build_error"] is None and not d["missing"]


def test_saving_keeps_passwords_left_empty_and_writes_valid_toml(m5files):
    body = {"wifi": {"ssid": "house", "password": ""},
            "direct": {"enabled": True, "ssid": "stim-remote", "password": "", "channel": 11},
            "boxes": [{"name": "box 2", "mac": "02-AB-CD-00-00-02", "host": "", "port": 55533},
                      {"name": "", "mac": "", "host": ""},                          # an empty row: dropped
                      {"name": "box 1", "mac": "aa:bb:cc:dd:ee:01", "host": "10.0.0.50"}]}
    st, d = run(_req(Hub(engine=FakeEngine()), "PUT", "/api/remote/settings", json=body))
    assert st == 200 and d["ok"] and d["direct"]["channel"] == 11
    cfg = tomllib.loads(m5files.cfg.read_text(encoding="utf-8"))
    assert cfg["wifi"]["password"] == "housepass1" and cfg["direct"]["password"] == "remotepass1"   # kept
    assert [b["mac"] for b in cfg["box"]] == ["02:ab:cd:00:00:02", "aa:bb:cc:dd:ee:01"]              # normalised
    assert "housepass1" not in json.dumps(d)
    assert m5config.build({}, cfg)["wifi"]["channel"] == 11          # the file is what a load would use


@pytest.mark.parametrize("change, message", [
    ({"direct": {"enabled": True, "ssid": "stim-remote", "password": "short", "channel": 6}}, "8..63"),
    ({"boxes": [{"name": "x", "mac": "not-a-mac"}]}, "not aa:bb"),
    ({"direct": {"enabled": False}, "wifi": {"ssid": "house"}, "boxes": [{"name": "x", "mac": "aa:bb:cc:dd:ee:ff"}]},
     "has no host"),                                                  # house Wi-Fi: a box needs its address
])
def test_settings_the_remote_could_not_use_are_refused(m5files, change, message):
    before = m5files.cfg.read_text(encoding="utf-8")
    body = {"wifi": {"ssid": "house"}, "direct": {"enabled": True, "ssid": "stim-remote", "channel": 6},
            "boxes": [{"name": "box 2", "mac": "02:ab:cd:00:00:02"}], **change}
    st, d = run(_req(Hub(engine=FakeEngine()), "PUT", "/api/remote/settings", json=body))
    assert st == 400 and message in d["error"]
    assert m5files.cfg.read_text(encoding="utf-8") == before          # nothing written


def test_no_settings_file_yet(m5files):
    m5files.cfg.unlink()
    d = m5settings.view()
    assert d["missing"] and d["boxes"] == [] and d["build_error"] and not d["direct"]["enabled"]


# ---- last load ---------------------------------------------------------------------------------------------------------
LOAD_LOG = ["patterns.bin: 195 patterns, 16846 bytes (ErosLink 12, Built-in modes 18, ErosLink examples 70, "
            "Your routines 95)", "config.json: 517 bytes -> remote/build/m5", "patterns.bin: 16846/16846",
            "loaded; the remote reloaded its patterns and settings"]


def test_load_log_is_parsed():
    rec = m5settings.parse_load_log(LOAD_LOG)
    assert rec == {"patterns": 195, "pack_bytes": 16846,
                   "groups": {"ErosLink": 12, "Built-in modes": 18, "ErosLink examples": 70, "Your routines": 95}}
    assert m5settings.parse_load_log(LOAD_LOG[:-1]) is None           # not confirmed by the remote: no record


def test_a_finished_load_is_recorded_and_compared(m5files):
    built = m5config.build(m5settings._engine_cfg(), m5settings._read(), None)
    m5settings.BUILD_CONFIG.parent.mkdir(parents=True)
    m5settings.BUILD_CONFIG.write_bytes(m5config.to_bytes(built))    # what the load job's build wrote
    m5settings.record_job(SimpleNamespace(state="ok", lines=LOAD_LOG))
    ll = m5settings.last_load()
    assert ll["patterns"] == 195 and ll["settings_match"] is True
    m5settings.record_job(SimpleNamespace(state="failed", lines=LOAD_LOG + ["boom"]))    # a failed load: kept as was
    assert m5settings.last_load()["patterns"] == 195
    m5settings.update({"wifi": {"ssid": "house"}, "direct": {"enabled": True, "ssid": "stim-remote", "channel": 3},
                       "boxes": [{"name": "box 2", "mac": "02:ab:cd:00:00:02"}]})
    assert m5settings.last_load()["settings_match"] is False          # changed since: load again


def test_the_load_job_kind_has_the_hook():
    hub = Hub(engine=FakeEngine())
    assert hub.jobs.on_done["remote-load"] is m5settings.record_job


# ---- pattern files -----------------------------------------------------------------------------------------------------
def test_pattern_files_are_counted_without_loading(monkeypatch):
    entries = [SimpleNamespace(group=pack.GROUP_BUILTIN)] * 18 + [SimpleNamespace(group=pack.GROUP_YOURS)] * 3
    monkeypatch.setattr(pack, "collect", lambda **kw: (SimpleNamespace(entries=entries), ["a.elk: unreadable"]))
    monkeypatch.setattr(fwdata, "load", lambda cfg=None: fwdata.FirmwareData({}, "test data"))
    st, d = run(_req(Hub(engine=FakeEngine()), "GET", "/api/remote/patterns"))
    assert st == 200 and d["total"] == 21 and d["notes"] == ["a.elk: unreadable"]
    counts = {g["name"]: g["count"] for g in d["groups"]}
    assert counts == {"Built-in modes": 18, "ErosLink": 0, "ErosLink examples": 0, "Your routines": 3, "Our routines": 0}
    assert d["builtin"]["available"] and d["builtin"]["source"] == "test data"
    assert set(d["eroslink_cache"]) == {"path", "exists", "files"}


# ---- ET-312 extraction -------------------------------------------------------------------------------------------------
@pytest.fixture
def user_data(tmp_path, monkeypatch):
    out = tmp_path / "et312-firmware-data.json"
    monkeypatch.setattr(fwdata, "USER_DATA", out)
    monkeypatch.setattr(fwdata, "_default", fwdata._default)          # the extract sets the process default
    monkeypatch.setattr(fwdata, "_default_loaded", fwdata._default_loaded)
    return out


def test_extract_from_an_image(user_data):
    st, d = run(_req(Hub(engine=FakeEngine()), "POST", "/api/et312/extract", data=_synthetic_image()))
    assert st == 200 and d["ok"] and d["blocks"] == fwdata.NBLOCKS and len(d["sha256"]) == 64
    assert user_data.exists() and len(fwdata.from_json(user_data).blocks) == fwdata.NBLOCKS
    assert [p.name for p in user_data.parent.iterdir()] == [user_data.name]   # only the JSON is kept, not the image


def test_extract_refuses_what_is_not_an_image(user_data):
    def hub():
        return Hub(engine=FakeEngine())                               # one per event loop
    st, d = run(_req(hub(), "POST", "/api/et312/extract", data=b"\x00" * 100))
    assert st == 400 and "not a decrypted ET-312B v1.6 image" in d["error"] and not user_data.exists()
    st, d = run(_req(hub(), "POST", "/api/et312/extract", data=b":00000001FF\n"))       # an empty Intel HEX file
    assert st == 400
    st, d = run(_req(hub(), "POST", "/api/et312/extract", data=b"\xff" * (300 * 1024)))
    assert st == 400 and "too big" in d["error"]


def test_extracted_data_is_found_by_the_engine(user_data, monkeypatch, tmp_path):
    img = tmp_path / "img.bin"
    img.write_bytes(_synthetic_image())
    fwdata.to_json(fwdata.from_image(img), user_data)
    monkeypatch.delenv("STIM_ENGINE_ET312_DATA", raising=False)
    monkeypatch.delenv("STIM_ENGINE_ET312_IMAGE", raising=False)
    monkeypatch.setattr(fwdata, "PRIVATE_DATA", tmp_path / "nothing.json")
    d = fwdata.load({})
    assert d is not None and user_data.name in d.source


# ---- status panel ------------------------------------------------------------------------------------------------------
STATUS = {"armed": True, "faulted": False, "fault_reason": None, "link": "serial", "master": 0.8, "deadman_active": False,
          "fork_firmware": True, "fork_version": 8}
TELE = {"firmware": "1.3.2 (main)", "age_s": 0.2, "peak": [0.0121, 0.0125, 0.0302, 0.0301],
        "battery_soc": 0.87, "wall_power_present": False, "actual_pulse_frequency": 163.2, "device_volume": 0.4}
FOC = {"output": "fork", "pattern": {"id": "builtin:waves", "name": "Waves"}, "levels": [0.25, 0.4], "ma": 0.5,
       "routes": [12, 34]}


def test_status_summary():
    s = status.summarize(STATUS, TELE, FOC, running=True, port="COM17", trip=None)
    assert s["firmware"] == "1.3.2 (main) · PlaStim fork v8" and s["state"] == "running"
    assert (s["level_a"], s["level_b"], s["master"], s["ma"]) == (25, 40, 80, 50)
    assert (s["peak_ma_a"], s["peak_ma_b"]) == (12.1, 30.1)       # the smaller electrode of each channel's pair
    assert (s["battery"], s["pulse_hz"], s["box_knob"]) == (87, 163.2, 40) and s["warnings"] == []


def test_status_warnings_and_missing_data():
    s = status.summarize({**STATUS, "deadman_active": True}, {**TELE, "device_volume": 0.0, "age_s": 9.0},
                         {**FOC, "output": "preview"}, running=True, port="COM17", trip=None)
    text = " ".join(s["warnings"])
    assert "ramping down" in text and "knob is at zero" in text and "no data from the box" in text and "Preview" in text
    off = status.summarize(None, None, None, running=False, port=None, trip=None)
    assert not off["reachable"] and off["state"] is None and off["level_a"] is None and off["warnings"] == []
    starting = status.summarize(None, None, None, running=True, port="COM17", trip=None)
    assert any("starting" in w for w in starting["warnings"])


def test_trip_report_is_caught_from_the_engine_log():
    e = EngineProc()
    lines = ["INFO engine: running",
             "WARNING engine.device.client: device: biphasic: current limit exceeded (limit 0.304 A primary)",
             "WARNING engine.device.client: device: biphasic trip: meas a -0.309 b 0.001 c 0.000 d -0.000 A primary",
             "WARNING engine.device.client: device: biphasic trip: cmd peak 0.184 A primary, lead 130.0 us, route 21",
             "WARNING engine.device.client: device: biphasic trip: r_est 15.79 ohm, sigma 0.44",
             "WARNING engine.device.client: device:          0          0          0          0"]
    for ln in lines:
        e._watch(ln)
    assert e.last_trip["lines"][0].startswith("biphasic: current limit exceeded") and len(e.last_trip["lines"]) == 4
    kept = status.filter_log(lines + ['INFO aiohttp.access: 127.0.0.1 "GET /state HTTP/1.1" 200',
                                      "INFO engine.content.worker: analyzer worker audio ready"])
    assert kept == lines[:5]                                          # timing tables and access lines dropped


def test_status_endpoint_folds_the_engine_apis(monkeypatch):
    async def fake_fetch():
        return STATUS, TELE, FOC
    monkeypatch.setattr(status, "fetch", fake_fetch)
    eng = FakeEngine("COM17")
    eng.last_trip = {"time": 1.0, "lines": ["biphasic: current limit exceeded (limit 0.304 A primary)"]}
    eng.log.extend(["INFO engine: started", 'INFO aiohttp.access: x "GET /state HTTP/1.1"'])
    st, d = run(_req(Hub(engine=eng), "GET", "/api/engine/status"))
    assert st == 200 and d["running"] and d["port"] == "COM17" and d["pattern"] == "Waves"
    assert d["trip"]["lines"] and d["log"] == ["INFO engine: started"]


# ---- page ------------------------------------------------------------------------------------------------------------
class _Ids(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.links, self.tabs = set(), [], {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.add(a["id"])
        if tag == "a" and a.get("href", "").startswith("https://"):
            self.links.append((a["href"], a.get("target"), a.get("rel")))


def test_the_page_has_the_new_parts():
    p = _Ids()
    html = (ROOT / "app" / "index.html").read_text(encoding="utf-8")
    p.feed(html)
    for i in ("m5Mode", "m5DirSsid", "m5DirPw", "m5DirCh", "m5HouseSsid", "m5HousePw", "m5Boxes", "m5AddBox", "m5Save",
              "m5LastLoad", "patRows", "et312Image", "et312Extract", "statusGrid", "tripPanel", "statusWarnings",
              "engineLog"):
        assert i in p.ids, i
    assert ">M5 remote</button>" in html
    assert ("https://www.patreon.com/plastim", "_blank", "noopener") in p.links
    assert ("https://plastim.net/", "_blank", "noopener") in p.links
    js = (ROOT / "app" / "hub.js").read_text(encoding="utf-8")
    for fn in ("refreshStatus", "loadM5Settings", "loadPatterns", "loadEt312"):
        assert f"function {fn}(" in js
