"""System-wide hotkeys for the PC app: STOP and the levels reachable while another window (a video player, Stash)
has focus.

Windows only. The keys are registered with RegisterHotKey on one dedicated thread that runs its own GetMessage loop
(hotkeys belong to the thread that registered them). Elsewhere `supported` is False and everything is a no-op:
macOS needs the user to grant the app Accessibility permission before it may watch keys system-wide, and Wayland
blocks global hotkeys by design (X11 could do it; not built). The focused player page has its own keys either way.

Default bindings (low-collision: Pause is otherwise unused; Ctrl+Alt+arrows are rarely bound by apps):

    Pause                  stop       (repeats allowed: holding it keeps stopping)
    Ctrl+Alt+Up / Down     both levels up / down
    Ctrl+Alt+Right / Left  level A up / down
    Ctrl+Alt+PageUp / Down level B up / down

UP actions are registered with MOD_NOREPEAT: holding the key does not keep raising a level (one step per press),
the same rule as the player page (up is rate-limited, down and STOP never are). A key another app already owns fails
on its own and the others still work; status() says which.

The `action` callback runs on the hotkey thread: keep it quick (hand the work to the asyncio loop with
loop.call_soon_threadsafe); an exception in it is logged and the loop carries on.
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Callable

logger = logging.getLogger(__name__)

ACTIONS = ("stop", "level_up", "level_down", "a_up", "a_down", "b_up", "b_down")
UP_ACTIONS = frozenset({"level_up", "a_up", "b_up"})

DEFAULT_BINDINGS: list[tuple[str, str]] = [
    ("Pause", "stop"),
    ("Ctrl+Alt+Up", "level_up"),
    ("Ctrl+Alt+Down", "level_down"),
    ("Ctrl+Alt+Right", "a_up"),
    ("Ctrl+Alt+Left", "a_down"),
    ("Ctrl+Alt+PageUp", "b_up"),
    ("Ctrl+Alt+PageDown", "b_down"),
]

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
_VK = {"pause": 0x13, "break": 0x13, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "pageup": 0x21,
       "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22, "home": 0x24, "end": 0x23, "insert": 0x2D, "delete": 0x2E,
       "space": 0x20, "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "scrolllock": 0x91}
_VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})              # F1 = 0x70 .. F24 = 0x87
_VK.update({chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)})
_VK.update({str(d): 0x30 + d for d in range(10)})

WM_HOTKEY, WM_QUIT, PM_NOREMOVE = 0x0312, 0x0012, 0x0000


def parse_keys(text: str) -> tuple[int, int]:
    """'Ctrl+Alt+PageUp' -> (modifiers, virtual-key code). ValueError on anything unknown."""
    parts = [p.strip().lower() for p in str(text).split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key combination")
    mods = 0
    for p in parts[:-1]:
        if p not in _MODS:
            raise ValueError(f"unknown modifier {p!r} in {text!r}")
        mods |= _MODS[p]
    key = parts[-1]
    if key not in _VK:
        raise ValueError(f"unknown key {parts[-1]!r} in {text!r}")
    return mods, _VK[key]


class _Win32Registrar:
    """The real thing: user32 RegisterHotKey / UnregisterHotKey, called on the hotkey thread."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ct, self.wt = ctypes, wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self.user32.RegisterHotKey.restype = wintypes.BOOL
        self.user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.UnregisterHotKey.restype = wintypes.BOOL
        self.user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        self.user32.GetMessageW.restype = wintypes.BOOL
        self.user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                                             wintypes.UINT, wintypes.UINT]
        self.user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.PostThreadMessageW.restype = wintypes.BOOL
        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    def register(self, hk_id: int, mods: int, vk: int) -> str | None:
        if self.user32.RegisterHotKey(None, hk_id, mods, vk):
            return None
        err = self.ct.get_last_error()
        return "taken by another app" if err == 1409 else f"RegisterHotKey failed (error {err})"

    def unregister(self, hk_id: int) -> None:
        self.user32.UnregisterHotKey(None, hk_id)

    def thread_id(self) -> int:
        return int(self.kernel32.GetCurrentThreadId())

    def ensure_queue(self) -> None:
        msg = self.wt.MSG()
        self.user32.PeekMessageW(self.ct.byref(msg), None, 0, 0, PM_NOREMOVE)   # creates this thread's queue

    def next_hotkey(self) -> int | None:
        """Block for the next message; the hotkey id for WM_HOTKEY, -1 for anything else, None on WM_QUIT."""
        msg = self.wt.MSG()
        r = self.user32.GetMessageW(self.ct.byref(msg), None, 0, 0)
        if r == 0 or r == -1:
            return None
        return int(msg.wParam) if msg.message == WM_HOTKEY else -1

    def quit(self, thread_id: int) -> bool:
        return bool(self.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0))


class Hotkeys:
    def __init__(self, action: Callable[[str], None], bindings: list[tuple[str, str]] | None = None, *,
                 registrar=None, platform: str | None = None) -> None:
        self.action = action
        self.platform = platform or sys.platform
        self.supported = self.platform == "win32"
        self._registrar = registrar
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self.bindings: list[dict] = []
        for i, (keys, act) in enumerate(bindings if bindings is not None else DEFAULT_BINDINGS):
            b = {"id": 0xB000 + i, "keys": keys, "action": act, "registered": False, "error": None,
                 "mods": 0, "vk": 0}
            try:
                if act not in ACTIONS:
                    raise ValueError(f"unknown action {act!r}")
                b["mods"], b["vk"] = parse_keys(keys)
                if act in UP_ACTIONS:
                    b["mods"] |= MOD_NOREPEAT
            except ValueError as exc:
                b["error"] = str(exc)
            self.bindings.append(b)

    # ---- the parts the thread runs (also used directly by the tests) ----------------------------------------
    def _register_all(self, reg) -> None:
        for b in self.bindings:
            if b["vk"] == 0:
                continue                                   # a bad binding string: never tried, its error stays
            b["error"] = reg.register(b["id"], b["mods"], b["vk"])
            b["registered"] = b["error"] is None
            if not b["registered"]:
                logger.warning("hotkey %s (%s) not registered: %s", b["keys"], b["action"], b["error"])

    def _unregister_all(self, reg) -> None:
        for b in self.bindings:
            if b["registered"]:
                reg.unregister(b["id"])
                b["registered"] = False

    def _dispatch(self, hk_id: int) -> None:
        for b in self.bindings:
            if b["id"] == hk_id and b["registered"]:
                try:
                    self.action(b["action"])
                except Exception:  # noqa: BLE001 - one bad callback must not end the key loop
                    logger.exception("hotkey action %s failed", b["action"])
                return

    def _run(self) -> None:
        reg = self._registrar or _Win32Registrar()
        try:
            reg.ensure_queue()
            self._thread_id = reg.thread_id()
            self._register_all(reg)
        finally:
            self._ready.set()
        try:
            while True:
                hk = reg.next_hotkey()
                if hk is None:
                    break
                if hk >= 0:
                    self._dispatch(hk)
        finally:
            self._unregister_all(reg)

    # ---- public ---------------------------------------------------------------------------------------------
    def start(self) -> None:
        if not self.supported or self._thread is not None:
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(2.0)

    def stop(self) -> None:
        t = self._thread
        if t is None:
            return
        reg = self._registrar or _Win32Registrar()
        if self._thread_id:
            reg.quit(self._thread_id)
        t.join(2.0)
        self._thread = None
        self._thread_id = 0

    def status(self) -> dict:
        note = None
        if not self.supported:
            note = ("system-wide hotkeys are Windows-only for now (macOS needs Accessibility permission, Wayland "
                    "blocks them); the player page's own keys still work while it has focus")
        return {
            "supported": self.supported,
            "platform": self.platform,
            "running": self._thread is not None,
            "note": note,
            "bindings": [{"keys": b["keys"], "action": b["action"], "registered": b["registered"],
                          "error": b["error"] if self.supported or b["vk"] == 0 else "not supported on this platform"}
                         for b in self.bindings],
        }
