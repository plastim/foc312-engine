"""stimengine.tools.supervise: restart the engine after a fault / failed start, stop on a clean exit or the stop file."""
from stimengine.tools import supervise as S


class FakeChild:
    def __init__(self, rc, on_wait=None):
        self.rc, self.on_wait = rc, on_wait

    def wait(self, timeout=None):
        if self.on_wait:
            self.on_wait()
        return self.rc


def fake_popen(rcs, calls, on_wait=None):
    def popen(cmd, stdout=None, stderr=None):
        calls.append(cmd)
        return FakeChild(rcs.pop(0), on_wait)
    return popen


def test_restarts_after_fault_and_failed_start_until_clean_exit(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(S, "RESTART_DELAY_S", 0.0)
    monkeypatch.setattr(S.subprocess, "Popen", fake_popen([3, 2, 2, 0], calls))
    rc = S.main(["--logs", str(tmp_path), "--serial", "COM13", "--mode", "fourphase"])
    assert rc == 0
    assert len(calls) == 4                                   # trip, latched, latched, then a clean stop
    assert calls[0][-4:] == ["--serial", "COM13", "--mode", "fourphase"]
    err = (tmp_path / "serve-err.log").read_text()
    assert err.count("=== start") == 4 and "FAULT" in err and "not restarting" in err


def test_stop_file_prevents_the_restart(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(S, "RESTART_DELAY_S", 0.0)
    monkeypatch.setattr(S.subprocess, "Popen",
                        fake_popen([3, 3], calls, on_wait=lambda: (tmp_path / S.STOP_FILE).touch()))
    rc = S.main(["--logs", str(tmp_path), "--serial", "COM13"])
    assert rc == 3 and len(calls) == 1
