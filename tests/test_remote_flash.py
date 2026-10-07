"""Flashing the remote (stimengine/remote/flash.py): an update of the RADR writes the app partition only, so the
remote check's result (NVS) survives; the whole image only when the remote's partition table is not the image's.
esptool is replaced by a fake: nothing here opens a serial port."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from stimengine.remote import flash as F


def _entry(ptype: int, sub: int, off: int, size: int, label: str) -> bytes:
    return b"\xaa\x50" + bytes([ptype, sub]) + struct.pack("<II", off, size) + label.encode().ljust(16, b"\0") + \
        b"\0" * 4


def table(factory_size: int = 0x300000) -> bytes:
    """The RADR's partition table (radr/partitions.csv) as partitions.bin: 0xC00 bytes, 0xFF after the entries."""
    t = (_entry(1, 2, 0x9000, 0x5000, "nvs") + _entry(0, 0, 0x10000, factory_size, "factory") +
         _entry(1, 0x82, 0x310000, 0xCE0000, "spiffs") + _entry(1, 3, 0xFF0000, 0x10000, "coredump"))
    return t + b"\xff" * (0xC00 - len(t))


def merged(app: bytes = b"\xe9" + b"A" * 999, tbl: bytes | None = None) -> bytes:
    """What tools/package_m5.py makes: bootloader at 0x0, the table at 0x8000, the app at 0x10000, 0xFF between."""
    img = bytearray(b"\xff" * 0x10000)
    img[0:4] = b"\xe9BOOT"[:4]
    t = tbl or table()
    img[0x8000:0x8000 + len(t)] = t
    return bytes(img) + app


class FakeEsptool:
    """Stands in for `python -m esptool`: read-flash answers with the device's table, writes are recorded."""

    def __init__(self, device_table: bytes):
        self.device_table = device_table
        self.calls: list[list[str]] = []
        self.written: list[tuple[int, bytes]] = []

    def __call__(self, cmd, **_kw):
        args = cmd[cmd.index("--chip"):]
        assert args[:6] == ["--chip", "esp32s3", "--port", "COM3", "--baud", "921600"]
        self.calls.append(args[6:])
        op = args[6]
        if op == "read-flash":
            off, size, out = int(args[7], 16), int(args[8], 16), Path(args[9])
            assert (off, size) == (0x8000, 0xC00)
            out.write_bytes(self.device_table)
        elif op == "write-flash":
            self.written.append((int(args[7], 16), Path(args[8]).read_bytes()))

        class R:
            returncode = 0
        return R()


@pytest.fixture
def image(tmp_path) -> Path:
    p = tmp_path / "stim-remote-radr-test.bin"
    p.write_bytes(merged())
    return p


def test_an_update_writes_the_app_partition_only(image, monkeypatch):
    esp = FakeEsptool(table())
    monkeypatch.setattr(F.subprocess, "run", esp)
    assert F.flash("COM3", image, "app") == "app"
    assert [c[0] for c in esp.calls] == ["read-flash", "write-flash"]
    assert esp.written == [(0x10000, image.read_bytes()[0x10000:])]      # nothing below 0x10000: NVS (0x9000) stays


def test_another_partition_table_on_the_remote_gets_the_whole_image(image, monkeypatch):
    esp = FakeEsptool(table(factory_size=0x200000))                        # an older or foreign layout
    monkeypatch.setattr(F.subprocess, "run", esp)
    assert F.flash("COM3", image, "app") == "full"
    assert esp.written == [(0x0, image.read_bytes())]


def test_the_full_mode_writes_the_whole_image_without_reading(image, monkeypatch):
    esp = FakeEsptool(table())
    monkeypatch.setattr(F.subprocess, "run", esp)
    assert F.flash("COM3", image, "full") == "full"
    assert [c[0] for c in esp.calls] == ["write-flash"] and esp.written == [(0x0, image.read_bytes())]


def test_images_that_are_not_merged_or_have_no_app_are_refused(tmp_path, monkeypatch):
    esp = FakeEsptool(table())
    monkeypatch.setattr(F.subprocess, "run", esp)
    bare = tmp_path / "firmware.bin"
    bare.write_bytes(b"\xe9" + b"x" * 5000)                                # an app alone, not a merged image
    with pytest.raises(F.FlashError, match="not a merged image"):
        F.flash("COM3", bare, "app")
    noapp = tmp_path / "noapp.bin"
    noapp.write_bytes(merged(app=b"\x00" * 100))
    with pytest.raises(F.FlashError, match="no app"):
        F.flash("COM3", noapp, "app")
    big = tmp_path / "big.bin"
    big.write_bytes(merged(tbl=table(factory_size=0x1000), app=b"\xe9" * 0x2000))
    with pytest.raises(F.FlashError, match="larger than its partition"):
        F.flash("COM3", big, "app")
    assert esp.calls == []                                                 # refused before esptool ran
    assert F.main(["--port", "COM3", "--mode", "app", str(bare)]) == 1


def test_a_failing_esptool_fails_the_flash(image, monkeypatch):
    class Fail:
        returncode = 2
    monkeypatch.setattr(F.subprocess, "run", lambda cmd, **kw: Fail())
    with pytest.raises(F.FlashError, match="exit code 2"):
        F.flash("COM3", image, "app")
    assert F.main(["--port", "COM3", str(image)]) == 1
