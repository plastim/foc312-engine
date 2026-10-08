"""The USB loader: stimengine/remote/loader.py (PC) against remote/core/loader.c (the remote), through a pipe to
the host simulator (remote/test/device_sim.c: the real loader and pack reader, files in a folder)."""
import os
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from stimengine.remote import pack as P
from stimengine.remote.loader import LoaderError, PipeLink, Remote
from tests.test_remote_core import CORE, ROOT, _zig_available
from stimengine.paths import BUILD_DIR, REMOTE_DIR
from tests.test_remote_pack import _synthetic_pack

pytestmark = pytest.mark.skipif(not _zig_available(), reason="needs the ziglang package (pip install ziglang)")

SRC = [CORE / "pyrand.c", CORE / "et312.c", CORE / "pack.c", CORE / "loader.c",
       REMOTE_DIR / "test" / "device_sim.c"]
EXE = BUILD_DIR / "remote" / ("device_sim.exe" if os.name == "nt" else "device_sim")


@pytest.fixture(scope="module")
def sim_exe() -> Path:
    headers = [CORE / n for n in ("et312.h", "pyrand.h", "pack.h", "loader.h")]
    newest = max(p.stat().st_mtime for p in SRC + headers)
    if not EXE.exists() or EXE.stat().st_mtime < newest:
        EXE.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "ziglang", "cc", "-std=c99", "-O2", "-Wall", "-Wextra", "-Werror",
               f"-I{CORE}", *map(str, SRC), "-o", str(EXE)]
        if os.name != "nt":
            cmd.append("-lm")
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    return EXE


@pytest.fixture
def remote(sim_exe, tmp_path):
    link = PipeLink([str(sim_exe), str(tmp_path)])
    yield Remote(link, timeout=5.0), tmp_path
    link.close()


PACK = P.build(_synthetic_pack())
CONFIG = b'{"format": "stim-remote config v1"}'


def test_full_load(remote):
    r, folder = remote
    assert r.hello() > 0
    r.put("patterns.bin", PACK)
    r.put("config.json", CONFIG)
    assert r.list() == {"patterns.bin": len(PACK), "config.json": len(CONFIG)}
    r.reload()
    assert (folder / "patterns.bin").read_bytes() == PACK
    assert (folder / "config.json").read_bytes() == CONFIG
    assert not list(folder.glob(".*.part"))


def test_many_chunks_arrive_intact(remote):
    r, folder = remote
    blob = bytes((i * 7919 + 13) & 0xFF for i in range(70_000))       # 69 chunks, a partial last one
    seen = []
    r.put("big.bin", blob, progress=lambda s, n: seen.append(s))
    assert (folder / "big.bin").read_bytes() == blob
    assert seen[-1] == len(blob) and len(seen) == -(-len(blob) // 1024)


def test_a_bad_crc_keeps_the_old_file(remote):
    r, folder = remote
    r.put("patterns.bin", PACK)
    link = r.link
    link.write(f"PUT patterns.bin 5 {zlib.crc32(b'hello') ^ 1:08x}\n".encode())
    assert link.readline(5).startswith("READY")
    link.write(b"hello")
    assert link.readline(5) == "ACK 5"
    assert link.readline(5) == "ERR crc"
    assert (folder / "patterns.bin").read_bytes() == PACK
    assert not list(folder.glob(".*.part"))


def test_busy_refuses_everything(remote):
    r, folder = remote
    r.put("patterns.bin", PACK)
    (folder / "BUSY").write_bytes(b"")
    for call in (r.hello, lambda: r.put("patterns.bin", b"x"), r.reload, r.list, lambda: r.delete("patterns.bin")):
        with pytest.raises(LoaderError, match="busy"):
            call()
    assert (folder / "patterns.bin").read_bytes() == PACK


@pytest.mark.parametrize("name", ["../evil", "Upper.bin", "", ".hidden", "a" * 40, "sp ace"])
def test_bad_names_are_refused(remote, name):
    r, _ = remote
    r.link.write(f"PUT {name} 3 00000000\n".encode())
    ans = r.link.readline(5)
    assert ans.startswith("ERR "), ans          # refused (a space splits the command: "ERR size")


def test_oversize_is_refused(remote):
    r, _ = remote
    with pytest.raises(LoaderError, match="size"):
        r._cmd(f"PUT big.bin {600 * 1024} 00000000")


def test_reload_rejects_a_damaged_pack(remote):
    r, folder = remote
    r.put("config.json", CONFIG)
    bad = PACK[:30] + bytes([PACK[30] ^ 0x40]) + PACK[31:]
    r.put("patterns.bin", bad)
    with pytest.raises(LoaderError, match="CRC mismatch"):
        r.reload()


def test_delete_and_missing(remote):
    r, folder = remote
    r.put("config.json", CONFIG)
    r.delete("config.json")
    assert not (folder / "config.json").exists()
    with pytest.raises(LoaderError, match="missing"):
        r.delete("config.json")
    with pytest.raises(LoaderError, match="no patterns.bin"):
        r.reload()


def test_hello_gives_up_on_a_device_that_never_stops_talking():
    """A FOC-Stim streams telemetry all the time: readline never times out, and HELLO must still give up."""
    import time as _t

    from stimengine.remote.loader import LoaderError, Remote

    class Chatty:
        def write(self, data):
            pass

        def readline(self, timeout):
            _t.sleep(0.01)
            return "~\x03garbage"

        def close(self):
            pass

    t0 = _t.monotonic()
    with pytest.raises(LoaderError, match="not a stim remote"):
        Remote(Chatty()).hello(wait_s=0.5)
    assert _t.monotonic() - t0 < 2.0


# ---- what the remote runs on: HELLO's board and MAC words (the RADR build next to the M5) -------------------------
@pytest.mark.parametrize("mode, board, mac", [(None, "m5", "02:00:00:00:00:05"), ("radr", "radr", "02:00:00:00:00:0a"),
                                              ("noident", "", "")])
def test_hello_says_the_board_and_the_mac(sim_exe, tmp_path, mode, board, mac):
    link = PipeLink([str(sim_exe), str(tmp_path)] + ([mode] if mode else []))
    try:
        r = Remote(link, timeout=5.0)
        assert r.hello() > 0                         # the free bytes as before: an older PC reads only that
        assert (r.board, r.mac) == (board, mac)      # "": firmware from before the board words (only the M5 had such)
        r.put("patterns.bin", PACK)                  # the same loader on both boards: patterns and settings
        r.put("config.json", CONFIG)
        r.reload()
        assert (tmp_path / "patterns.bin").read_bytes() == PACK
    finally:
        link.close()


def test_the_loader_runs_at_460800_with_dtr_and_rts_low(monkeypatch):
    """The RADR's UART0 behind its CP2102 runs at 460800; DTR / RTS stay deasserted (EN and IO0 untouched). The M5's
    USB serial ignores the baud."""
    import serial

    from stimengine.remote import loader as L

    opened = {}

    class FakeSerial:
        def __init__(self):
            self.port = None

        def open(self):
            opened.update(port=self.port, baud=self.baudrate, dtr=self.dtr, rts=self.rts)

        def reset_input_buffer(self):
            pass

    monkeypatch.setattr(serial, "Serial", FakeSerial)
    L.SerialLink("COM3")
    assert opened == {"port": "COM3", "baud": 460800, "dtr": False, "rts": False} and L.SERIAL_BAUD == 460800


# ---- a UART without flow control (the RADR's CP2102): a lost or damaged byte, and the PC sending the file again ----
@pytest.fixture
def quick(sim_exe, tmp_path):
    link = PipeLink([str(sim_exe), str(tmp_path), "radr"])
    yield Remote(link, timeout=1.0), tmp_path           # (a lost byte shows as a missing ACK: 1 s, not 5)
    link.close()


@pytest.mark.parametrize("fault, at", [("DROP", 5000), ("FLIP", 3000), ("DROP", 3071), ("FLIP", 0)])
def test_a_lost_or_damaged_byte_is_sent_again(quick, fault, at):
    r, folder = quick
    assert r.hello() > 0 and r.board == "radr"
    blob = bytes((i * 7919 + 13) & 0xFF for i in range(6000))
    (folder / fault).write_text(str(at))
    r.put("patterns.bin", blob)                          # the first try fails (no ACK / ERR crc), the second goes through
    assert not (folder / fault).exists() and (folder / "patterns.bin").read_bytes() == blob
    assert not list(folder.glob(".*.part"))
    assert r.list() == {"patterns.bin": len(blob)}       # and the remote answers normally afterwards


def test_a_failed_transfer_keeps_the_old_file(quick):
    r, folder = quick
    r.hello()
    r.put("patterns.bin", PACK)
    (folder / "DROP").write_text("100")
    with pytest.raises(LoaderError, match="timeout"):
        r.put("patterns.bin", b"\x01" * 3000, retries=0)
    r._unstick("no answer from the remote (timeout)")
    assert (folder / "patterns.bin").read_bytes() == PACK and not list(folder.glob(".*.part"))
    r.put("config.json", CONFIG)
    r.reload()                                           # the old patterns, still a working remote
    for final in ("ERR busy",):                          # a refusal is not sent again
        (folder / "BUSY").write_bytes(b"")
        with pytest.raises(LoaderError, match="busy"):
            r.put("patterns.bin", PACK)
        (folder / "BUSY").unlink()
