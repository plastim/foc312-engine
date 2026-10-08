"""PC side of the remote's USB loader (protocol: foc312-m5remote core/loader.h).

    link = SerialLink("COM20")          # the remote on USB
    r = Remote(link); r.hello(); r.put("patterns.bin", data); r.reload()

The remote refuses everything while its output is armed ("ERR busy"): loading never changes a running session.

Two kinds of remote speak it: the M5 remote (the ESP32-S3's own USB serial: any baud) and the remote on the RADR
hardware (its UART0 through a CP2102 USB-UART bridge at SERIAL_BAUD, no flow control). Firmware since the RADR build says which in
HELLO ("board=m5" / "board=radr") with its ESP32's MAC (Remote.board, Remote.mac): a CP2102's USB serial number is the
same on every unit, so the PC knows a RADR by that MAC.
"""
from __future__ import annotations

import queue
import subprocess
import threading
import time
import zlib


class LoaderError(RuntimeError):
    pass


# The loader's baud: the RADR's UART0 runs at this through its CP2102 (4x the old 115200: a pattern pack in well under
# a second). 921600 lost bytes on the RADR (no flow control, the UART's interrupt waiting out flash writes). The M5's
# USB serial ignores the baud, so one rate serves both.
SERIAL_BAUD = 115200
# A file whose transfer failed (a byte lost on the wire: no ACK; a damaged one: ERR crc) is sent again this often. The
# remote only replaces a file once all of it arrived with the right CRC, so a failed transfer leaves the old one.
PUT_RETRIES = 2
LOADER_TIMEOUT_S = 5.0          # the remote ends a transfer this long after its last byte (core/loader.h)
# what a transfer can fail with that sending it again cannot fix
_FINAL = ("busy", "name", "size", "space", "open")


class SerialLink:
    """The remote's USB serial port. DTR/RTS are held low (deasserted) so opening the port does not reset the ESP32:
    on the M5's USB serial they would, and on the RADR's CP2102 they drive EN and IO0 through the usual two-transistor
    auto-reset (both deasserted: neither)."""

    def __init__(self, port: str, timeout: float = 5.0, baud: int = SERIAL_BAUD) -> None:
        import serial

        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = baud
        self.ser.timeout = timeout
        self.ser.dtr = False
        self.ser.rts = False
        self.ser.open()
        self.ser.reset_input_buffer()

    def write(self, data: bytes) -> None:
        self.ser.write(data)
        self.ser.flush()

    def readline(self, timeout: float) -> str:
        self.ser.timeout = timeout
        line = self.ser.readline()
        if not line.endswith(b"\n"):
            raise LoaderError("no answer from the remote (timeout)")
        return line.decode("ascii", "replace").strip()

    def close(self) -> None:
        self.ser.close()


class PipeLink:
    """A subprocess speaking the protocol on stdin/stdout (the host simulator, remote/test/device_sim.c)."""

    def __init__(self, argv: list[str]) -> None:
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
        self.lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        for raw in iter(self.proc.stdout.readline, b""):
            self.lines.put(raw.decode("ascii", "replace").strip())

    def write(self, data: bytes) -> None:
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def readline(self, timeout: float) -> str:
        try:
            return self.lines.get(timeout=timeout)
        except queue.Empty:
            raise LoaderError("no answer from the remote (timeout)") from None

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.proc.kill()


class Remote:
    def __init__(self, link, timeout: float = 5.0) -> None:
        self.link = link
        self.timeout = timeout
        self.ident: dict[str, str] = {}            # HELLO's "board=..." and "mac=..." (firmware since the RADR build)

    def _cmd(self, line: str) -> str:
        self.link.write((line + "\n").encode("ascii"))
        return self._answer()

    def _answer(self) -> str:
        ans = self.link.readline(self.timeout)
        while ans.startswith("[") or not ans:      # a stray framework log line ("[ 10202][E][vfs_api...]"): skip
            ans = self.link.readline(self.timeout)
        if ans.startswith("ERR"):
            raise LoaderError(f"remote: {ans[4:] or ans}")
        return ans

    def hello(self, wait_s: float = 8.0) -> int:
        """Free bytes on the remote. Opening the USB port can reset the ESP32-S3, so boot chatter (the ROM banner)
        is skipped and HELLO is repeated until the remote answers or `wait_s` passes."""
        end = time.monotonic() + wait_s
        last = ""
        while time.monotonic() < end:
            self.link.write(b"\nHELLO\n")
            try:
                # the deadline holds inside too: a device that keeps talking (a FOC-Stim streams telemetry) never
                # lets readline time out, and loading pointed at a box hung here for good (2026-09-28)
                while time.monotonic() < end:
                    ans = self.link.readline(1.0)
                    if ans.startswith("ERR"):
                        raise LoaderError(f"remote: {ans[4:] or ans}")
                    parts = ans.split()
                    if parts[:3] == ["OK", "stim-remote", "1"]:
                        self._drain()
                        self.ident = {k: v for k, _eq, v in (w.partition("=") for w in parts[4:])
                                      if k in ("board", "mac") and v}
                        return int(parts[3])
                    if ans:
                        last = ans
            except LoaderError as exc:
                if "busy" in str(exc):
                    raise
        raise LoaderError(f"not a stim remote (last answer {last!r})")

    @property
    def board(self) -> str:
        """What the remote runs on: "m5", "radr", or "" (firmware from before it said; only the M5 had such)."""
        return self.ident.get("board", "")

    @property
    def mac(self) -> str:
        """Its ESP32's MAC, lower case with colons ("" if it did not say)."""
        return self.ident.get("mac", "").lower()

    def _drain(self) -> None:
        """Swallow the answers to any extra HELLOs sent while the remote was booting."""
        try:
            while True:
                self.link.readline(0.3)
        except LoaderError:
            pass

    def put(self, name: str, data: bytes, progress=None, retries: int = PUT_RETRIES) -> None:
        """Send a file. A transfer that fails on the way (a timeout, a CRC error, a wrong ACK) is sent again up to
        `retries` times, after the remote's half-finished transfer is ended (_unstick)."""
        for attempt in range(retries + 1):
            try:
                self._put_once(name, data, progress)
                if attempt:
                    print(f"{name}: sent again, attempt {attempt + 1}: ok")
                return
            except LoaderError as exc:
                if attempt == retries or any(w in str(exc) for w in _FINAL):
                    raise
                print(f"{name}: {exc}; sending it again")
                self._unstick(str(exc))

    def _unstick(self, why: str = "") -> None:
        """After a failed transfer: wait until the remote has ended it, then swallow what is still on its way. A
        CRC error (or a refused write) ended it there and then; after a lost byte the remote waits for the rest and
        gives up LOADER_TIMEOUT_S after the last byte ("ERR timeout"). Either way it drops the half-sent file and keeps
        the old one."""
        if not any(w in why for w in ("remote: crc", "remote: commit", "remote: write", "remote: timeout")):
            end = time.monotonic() + LOADER_TIMEOUT_S + 2.0
            while time.monotonic() < end:
                try:
                    line = self.link.readline(max(0.1, end - time.monotonic()))
                except LoaderError:
                    break
                if line.startswith("ERR timeout"):
                    break
        self._drain()

    def _put_once(self, name: str, data: bytes, progress=None) -> None:
        crc = zlib.crc32(data) & 0xFFFFFFFF
        ans = self._cmd(f"PUT {name} {len(data)} {crc:08x}")
        if not ans.startswith("READY "):
            raise LoaderError(f"unexpected answer {ans!r}")
        chunk = int(ans.split()[1])
        sent = 0
        while sent < len(data):
            part = data[sent:sent + chunk]
            self.link.write(part)
            sent += len(part)
            ack = self._answer()
            if ack != f"ACK {sent}":
                raise LoaderError(f"expected ACK {sent}, got {ack!r}")
            if progress:
                progress(sent, len(data))
        done = self._answer()
        if done != "OK":
            raise LoaderError(f"unexpected answer {done!r}")

    def list(self) -> dict[str, int]:
        self.link.write(b"LIST\n")
        out = {}
        while True:
            ans = self._answer()
            if ans == "END":
                return out
            _, name, size = ans.split()
            out[name] = int(size)

    def delete(self, name: str) -> None:
        self._cmd(f"DEL {name}")

    def reload(self) -> None:
        self._cmd("RELOAD")


def wait_for_port(port: str, seconds: float = 10.0) -> None:
    """After plugging the remote in, Windows needs a moment before the COM port opens."""
    import serial

    end = time.monotonic() + seconds
    while True:
        try:
            # DTR/RTS stay low: pyserial's defaults raise them, and on the ESP32-S3's USB serial that resets the chip
            probe = serial.Serial()
            probe.port = port
            probe.dtr = False
            probe.rts = False
            probe.open()
            probe.close()
            return
        except serial.SerialException:
            if time.monotonic() > end:
                raise
            time.sleep(0.3)
