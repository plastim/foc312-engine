"""The engine as a child process: `stimengine.tools.serve` for the box on one serial port (foc312 on :8322, the
control API on :8321). Connecting starts it; disconnecting stops it the way Ctrl-C does (zero, signal stop, close),
which releases the port, e.g. for flashing. Only one engine: one process owns a box.

After a FAULT (a box trip latches the box until it is power-cycled) the engine exits; this then asks the box every few
seconds whether it answers (a cheap handshake, no engine, no session) and restarts the engine on the same port once
it does. The player comes back with its setup (foc312-state.json): same pattern and routes, levels 0, NOT armed; the
user arms again. Disconnect stops the waiting."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Callable

from .jobs import ROOT

STOP_WAIT_S = 5.0
FAULT_RC = 3                        # serve's exit code after an engine fault (a box trip, a lost link)
RECONNECT_EVERY_S = 3.0             # after a fault: ask the box every few seconds whether it answers again
RECONNECT_WINDOW_S = 15 * 60        # and give up after this long
API_STOP_URL = "http://127.0.0.1:8321/stop"      # the engine's own clean exit (as the supervisor stops it)


def serve_command(port: str) -> list[str]:
    """How the engine normally runs (notes/handoff-foc312.md): four-phase at start; foc312 switches to the fork's
    biphasic-pairs mode itself when its output is set to the box."""
    return [sys.executable, "-m", "stimengine.tools.serve", "--serial", port, "--mode", "fourphase"]


def _has_console() -> bool:
    if sys.platform != "win32":
        return True
    import ctypes
    return bool(ctypes.windll.kernel32.GetConsoleWindow())


class EngineProc:
    def __init__(self, command: Callable[[str], list[str]] = serve_command,
                 stop_url: str | None = API_STOP_URL) -> None:
        self.command = command
        self.stop_url = stop_url
        self.proc: subprocess.Popen | None = None
        self.port: str | None = None
        self.log: deque[str] = deque(maxlen=200)
        self.last_trip: dict | None = None      # the box's last over-current trip report (see _watch)
        self.wanted_port: str | None = None     # the port the user connected (None after Disconnect)
        self.reconnect_note: str | None = None  # while waiting for the box after a fault
        self._lock = threading.Lock()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def owns(self, port: str | None) -> bool:
        return bool(port) and self.running() and (self.port or "").upper() == str(port).upper()

    def connect(self, port: str) -> None:
        with self._lock:
            if self.running():
                raise RuntimeError(f"the engine is already running on {self.port}: disconnect first")
            self.wanted_port = port
            self._spawn(port, fresh=True)

    def _spawn(self, port: str, fresh: bool) -> None:
        """Start serve on `port` (the lock is held). fresh: a user Connect (clears the log and the trip report)."""
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        # its own process group, so a CTRL_BREAK reaches only the engine (on POSIX: its own session, SIGINT).
        # CTRL_BREAK needs a shared console, so no console window of its own unless the hub has none either
        if sys.platform == "win32":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | (0 if _has_console() else subprocess.CREATE_NO_WINDOW)
            kw: dict = {"creationflags": flags}
        else:
            kw = {"start_new_session": True}
        if fresh:
            self.log.clear()
            self.last_trip = None
        self.proc = subprocess.Popen(self.command(port), cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                                     encoding="utf-8", errors="replace", bufsize=1, **kw)
        self.port = port
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            self.log.append(line)
            self._watch(line)
        rc = proc.wait()
        self.log.append(f"[engine exited, rc {rc}]")
        port = self.wanted_port
        if rc == FAULT_RC and port and proc is self.proc:
            threading.Thread(target=self._reconnect, args=(port,), daemon=True).start()

    def _box_answers(self, port: str) -> bool:
        import asyncio
        from .devices import probe_box
        try:
            return asyncio.run(probe_box(port)) is not None
        except Exception:  # noqa: BLE001 - the port is gone while the box is off, busy, ...
            return False

    def _reconnect(self, port: str) -> None:
        """After a fault: wait for the box to answer again (it is latched until power-cycled), then restart."""
        deadline = time.monotonic() + RECONNECT_WINDOW_S
        self.reconnect_note = "the box stopped (a trip?): switch it off and on; the engine reconnects by itself"
        self.log.append(f"[waiting for the box on {port}: power-cycle it to reconnect]")
        try:
            while time.monotonic() < deadline:
                time.sleep(RECONNECT_EVERY_S)
                if self.wanted_port != port or self.running():
                    return                                  # Disconnect, or someone connected meanwhile
                if not self._box_answers(port):
                    continue
                with self._lock:
                    if self.wanted_port != port or self.running():
                        return
                    self.log.append(f"[the box answers again on {port}: restarting the engine]")
                    self._spawn(port, fresh=False)
                return
            self.log.append("[gave up waiting for the box: press Connect when it is back]")
            self.wanted_port = None
        finally:
            self.reconnect_note = None

    def _watch(self, line: str) -> None:
        """Keep the box's trip report. The firmware sends it as debug strings, which the engine logs as
        'device: biphasic: current limit exceeded ...' then three 'device: biphasic trip: ...' lines; the box then
        stops answering until it is power-cycled, so the report is only ever in this log."""
        i = line.find("device: ")
        msg = line[i + 8:].strip() if i >= 0 else ""
        if "current limit exceeded" in msg:
            self.last_trip = {"time": time.time(), "lines": [msg]}
        elif msg.startswith("biphasic trip:") and self.last_trip is not None and len(self.last_trip["lines"]) < 6:
            self.last_trip["lines"].append(msg)

    def _api_stop(self) -> bool:
        if not self.stop_url:
            return False
        import urllib.request
        try:
            req = urllib.request.Request(self.stop_url, data=b"{}", headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=2).read()
            return True
        except Exception:  # noqa: BLE001 - still starting, or its API is not up
            return False

    def _signal_stop(self, proc: subprocess.Popen) -> None:
        try:
            if sys.platform == "win32":
                os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(proc.pid, signal.SIGINT)
        except OSError:
            pass

    def disconnect(self) -> int | None:
        """Stop the engine cleanly (zero, signal stop, close): its API's /stop, else CTRL_BREAK / SIGINT (serve
        treats both like Ctrl-C); terminate it only if it has not exited within STOP_WAIT_S."""
        with self._lock:
            self.wanted_port = None                  # no reconnecting after a Disconnect
            proc = self.proc
            if proc is None:
                return None
            if proc.poll() is None:
                stopped = False
                if self._api_stop():
                    try:
                        proc.wait(timeout=STOP_WAIT_S)
                        stopped = True
                    except subprocess.TimeoutExpired:
                        pass
                if not stopped:
                    self._signal_stop(proc)
                    try:
                        proc.wait(timeout=STOP_WAIT_S)
                    except subprocess.TimeoutExpired:
                        self.log.append("[engine did not stop in time: terminating]")
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()
            self.port = None
            return proc.returncode

    def status(self) -> dict:
        running = self.running()
        return {"running": running, "port": self.port if running else None,
                "reconnecting": self.reconnect_note is not None and not running, "reconnect_note": self.reconnect_note,
                "pid": self.proc.pid if running and self.proc else None,
                "foc312_url": "http://127.0.0.1:8322/", "api_url": "http://127.0.0.1:8321/",
                "log": list(self.log)[-50:]}
