"""The current cap as a hub setting: config/engine.toml [signal] waveform_amplitude_amps, never above the box's 0.2 A."""
from __future__ import annotations

import tomllib

import pytest

from stimengine.app import capsetting, m5settings
from stimengine.app.server import Hub
from tests.test_app_backend import FakeEngine, run
from tests.test_app_hub_remote_status import _req

ENGINE_TOML = """# the engine's config
[device]
port = "COM1"

[signal]
mode = "threephase"         # "threephase" | "fourphase"
waveform_amplitude_amps = 0.15   # hard cap; board max 0.2
min_carrier_hz = 500
"""


@pytest.fixture
def eng(tmp_path, monkeypatch):
    p = tmp_path / "engine.toml"
    p.write_text(ENGINE_TOML, encoding="utf-8")
    monkeypatch.setattr(m5settings, "ENGINE_TOML", p)
    return p


def test_read_and_write_keep_the_rest_of_the_file(eng):
    assert capsetting.read() == 0.15
    assert capsetting.write(0.2) == 0.2
    text = eng.read_text(encoding="utf-8")
    assert "waveform_amplitude_amps = 0.2   # hard cap; board max 0.2" in text      # the comment is kept
    assert text.replace("= 0.2 ", "= 0.15 ", 1) == ENGINE_TOML                       # nothing else changed
    assert tomllib.loads(text)["signal"]["waveform_amplitude_amps"] == 0.2


@pytest.mark.parametrize("bad", [0.21, 0.5, 0.0, 0.01, -0.1])
def test_never_above_the_box_maximum_or_below_the_minimum(eng, bad):
    with pytest.raises(ValueError):
        capsetting.write(bad)
    assert eng.read_text(encoding="utf-8") == ENGINE_TOML


def test_a_file_without_the_line_gets_it_under_signal(tmp_path, monkeypatch):
    p = tmp_path / "engine.toml"
    p.write_text("[device]\nport = 'COM1'\n\n[signal]\nmode = 'threephase'\n", encoding="utf-8")
    monkeypatch.setattr(m5settings, "ENGINE_TOML", p)
    capsetting.write(0.18)
    assert tomllib.loads(p.read_text(encoding="utf-8"))["signal"] == {"waveform_amplitude_amps": 0.18,
                                                                       "mode": "threephase"}


def test_the_hub_api(eng):
    st, d = run(_req(Hub(engine=FakeEngine()), "GET", "/api/settings/cap"))
    assert st == 200 and d["amps"] == 0.15 and d["max"] == 0.2
    st, d = run(_req(Hub(engine=FakeEngine()), "PUT", "/api/settings/cap", json={"amps": 0.2}))
    assert st == 200 and d["ok"] and d["amps"] == 0.2 and "next time the engine connects" in d["note"]
    st, d = run(_req(Hub(engine=FakeEngine()), "PUT", "/api/settings/cap", json={"amps": 0.3}))
    assert st == 400 and "200 mA" in d["error"]
    assert capsetting.read() == 0.2
