"""PC app system-wide hotkeys (stimengine/app/hotkeys.py): parsing, registration status, dispatch, platforms."""
import queue
import sys
import threading

import pytest

from stimengine.app import hotkeys as H


def test_parse_keys():
    assert H.parse_keys("Pause") == (0, 0x13)
    assert H.parse_keys("Ctrl+Alt+Up") == (H.MOD_CONTROL | H.MOD_ALT, 0x26)
    assert H.parse_keys("ctrl + shift + F24") == (H.MOD_CONTROL | H.MOD_SHIFT, 0x87)
    assert H.parse_keys("Alt+PageDown") == (H.MOD_ALT, 0x22)
    assert H.parse_keys("Win+q") == (H.MOD_WIN, ord("Q"))
    for bad in ("", "Hyper+Up", "Ctrl+Banana", "Ctrl+"):
        with pytest.raises(ValueError):
            H.parse_keys(bad)


class FakeRegistrar:
    """Registers everything except the combos in `taken`; hands out queued hotkey ids like GetMessage."""

    def __init__(self, taken=()):
        self.taken = set(taken)
        self.active = {}
        self.q: queue.Queue = queue.Queue()

    def register(self, hk_id, mods, vk):
        if (mods & ~H.MOD_NOREPEAT, vk) in self.taken:
            return "taken by another app"
        self.active[hk_id] = (mods, vk)
        return None

    def unregister(self, hk_id):
        self.active.pop(hk_id, None)

    def thread_id(self):
        return 1

    def ensure_queue(self):
        pass

    def next_hotkey(self):
        return self.q.get()

    def quit(self, thread_id):
        self.q.put(None)
        return True


def test_defaults_register_and_up_keys_do_not_repeat():
    reg = FakeRegistrar()
    hk = H.Hotkeys(lambda a: None, registrar=reg, platform="win32")
    hk._register_all(reg)
    st = hk.status()
    assert st["supported"] and all(b["registered"] for b in st["bindings"])
    assert {b["action"] for b in st["bindings"]} == set(H.ACTIONS)
    by_action = {b["action"]: b for b in hk.bindings}
    assert by_action["a_up"]["mods"] & H.MOD_NOREPEAT and by_action["level_up"]["mods"] & H.MOD_NOREPEAT
    assert not by_action["stop"]["mods"] & H.MOD_NOREPEAT          # holding Pause keeps stopping
    assert not by_action["b_down"]["mods"] & H.MOD_NOREPEAT
    hk._unregister_all(reg)
    assert not reg.active


def test_a_taken_key_fails_alone_and_bad_bindings_are_reported():
    reg = FakeRegistrar(taken={H.parse_keys("Ctrl+Alt+Up")})
    hk = H.Hotkeys(lambda a: None, bindings=[("Ctrl+Alt+Up", "level_up"), ("Pause", "stop"),
                                             ("Ctrl+Nope", "stop"), ("F13", "explode")],
                   registrar=reg, platform="win32")
    hk._register_all(reg)
    b = hk.status()["bindings"]
    assert (b[0]["registered"], b[0]["error"]) == (False, "taken by another app")
    assert b[1]["registered"] and b[1]["error"] is None
    assert not b[2]["registered"] and "unknown key" in b[2]["error"]
    assert not b[3]["registered"] and "unknown action" in b[3]["error"]


def test_the_thread_dispatches_survives_a_bad_callback_and_stops():
    reg = FakeRegistrar()
    got = []
    done = threading.Event()

    def action(a):
        got.append(a)
        if a == "a_up":
            raise RuntimeError("boom")                              # must not end the loop
        if a == "stop":
            done.set()

    hk = H.Hotkeys(action, registrar=reg, platform="win32")
    hk.start()
    ids = {b["action"]: b["id"] for b in hk.bindings}
    for a in ("a_up", "b_down", "stop"):
        reg.q.put(ids[a])
    reg.q.put(-1)                                                   # some other message: ignored
    assert done.wait(2.0)
    hk.stop()
    assert got == ["a_up", "b_down", "stop"]
    assert not hk.status()["running"] and not reg.active


def test_not_supported_off_windows():
    called = []
    hk = H.Hotkeys(called.append, platform="darwin")
    hk.start()
    hk.stop()
    st = hk.status()
    assert not st["supported"] and not st["running"] and "Accessibility" in st["note"]
    assert all(not b["registered"] for b in st["bindings"])
    assert all(b["error"] == "not supported on this platform" for b in st["bindings"])


@pytest.mark.skipif(sys.platform != "win32", reason="real RegisterHotKey needs Windows")
def test_real_registration_on_windows():
    # an unusual combo nobody binds; registered, reported, unregistered (no key injection)
    hk = H.Hotkeys(lambda a: None, bindings=[("Ctrl+Alt+Shift+F24", "stop")])
    hk.start()
    try:
        b = hk.status()["bindings"][0]
        assert b["registered"], b["error"]
    finally:
        hk.stop()
    assert not hk.status()["running"]
    # and it can be registered again after stop (so it really was released)
    hk2 = H.Hotkeys(lambda a: None, bindings=[("Ctrl+Alt+Shift+F24", "stop")])
    hk2.start()
    try:
        assert hk2.status()["bindings"][0]["registered"]
    finally:
        hk2.stop()
