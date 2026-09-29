"""After an engine FAULT (a box trip latches the box until it is power-cycled) the hub waits for the box to answer
again, then restarts the engine on the same port; a Disconnect stops the waiting; a clean exit never reconnects."""
from __future__ import annotations

import sys
import time

import pytest

from stimengine.app import engine_proc as EP


class Script:
    """A stand-in engine: each start runs the next script (exit code, or None = keep running)."""
    def __init__(self, *rcs):
        self.rcs, self.starts = list(rcs), []

    def __call__(self, port):
        self.starts.append(port)
        rc = self.rcs.pop(0) if self.rcs else None
        code = "import time; time.sleep(30)" if rc is None else f"import sys; print('engine'); sys.exit({rc})"
        return [sys.executable, "-c", code]


def wait_for(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(EP, "RECONNECT_EVERY_S", 0.1)


def test_a_fault_waits_for_the_box_then_restarts(fast, monkeypatch):
    answers = iter([False, False, False, True])                 # silent until "power-cycled"
    monkeypatch.setattr(EP.EngineProc, "_box_answers", lambda self, port: next(answers, True))
    script = Script(EP.FAULT_RC, None)
    ep = EP.EngineProc(command=script, stop_url=None)
    ep.connect("COM17")
    assert wait_for(lambda: ep.status()["reconnecting"]), "waiting after the fault"
    assert "power-cycle" in ep.status()["reconnect_note"] or "off and on" in ep.status()["reconnect_note"]
    assert wait_for(lambda: len(script.starts) == 2 and ep.running()), "restarted once the box answered"
    assert script.starts == ["COM17", "COM17"] and not ep.status()["reconnecting"]
    ep.disconnect()


def test_disconnect_stops_the_waiting(fast, monkeypatch):
    monkeypatch.setattr(EP.EngineProc, "_box_answers", lambda self, port: False)
    script = Script(EP.FAULT_RC, None)
    ep = EP.EngineProc(command=script, stop_url=None)
    ep.connect("COM17")
    assert wait_for(lambda: ep.status()["reconnecting"])
    ep.disconnect()
    monkeypatch.setattr(EP.EngineProc, "_box_answers", lambda self, port: True)
    time.sleep(0.5)
    assert script.starts == ["COM17"] and not ep.running(), "no restart after Disconnect"


def test_a_clean_exit_or_a_start_failure_never_reconnects(fast, monkeypatch):
    monkeypatch.setattr(EP.EngineProc, "_box_answers", lambda self, port: True)
    for rc in (0, 2):
        script = Script(rc)
        ep = EP.EngineProc(command=script, stop_url=None)
        ep.connect("COM17")
        assert wait_for(lambda: not ep.running())
        time.sleep(0.4)
        assert script.starts == ["COM17"] and not ep.status()["reconnecting"]
