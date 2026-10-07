"""Flash the remote's firmware with esptool, writing the app partition only when that keeps what the remote holds.

    py -3.13 -m stimengine.remote.flash --port COMx --mode app  IMAGE     an update (the RADR remote)
    py -3.13 -m stimengine.remote.flash --port COMx --mode full IMAGE     the whole image at 0x0

A release image is merged (tools/package_m5.py in foc312-m5remote): the bootloader at 0x0, the partition table at
0x8000, the app at its partition, with 0xFF filling the gaps. Written whole it also wipes NVS (0x9000), where the remote
on the RADR hardware keeps the result of its first-start remote check (each knob's direction, the screen's
orientation). An update therefore writes only the app partition, found in the image's own partition table, after
reading the table on the remote and checking that it is the image's: then nothing but the app changes (the remote
check, and the patterns and settings in LittleFS, stay). If the tables differ (an image with another flash layout),
the whole image is written instead, and the remote asks for its check again.

The port is the remote's USB serial (the RADR's CP2102: its DTR / RTS auto-reset puts the ESP32 in its bootloader).
"""
from __future__ import annotations

import argparse
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

TABLE_OFFSET, TABLE_SIZE = 0x8000, 0xC00
BAUD = 921600


class FlashError(RuntimeError):
    pass


def partitions(table: bytes) -> list[tuple[int, int, int, int, str]]:
    """(type, subtype, offset, size, label) per entry of an ESP-IDF partition table."""
    out = []
    for i in range(0, len(table) - 31, 32):
        e = table[i:i + 32]
        if e[:2] != b"\xaa\x50":
            break
        off, size = struct.unpack("<II", e[4:12])
        out.append((e[2], e[3], off, size, e[12:28].rstrip(b"\0").decode("ascii", "replace")))
    return out


def image_table(image: bytes) -> bytes:
    if len(image) < TABLE_OFFSET + TABLE_SIZE:
        raise FlashError("not a merged image: it ends before the partition table (0x8000)")
    table = image[TABLE_OFFSET:TABLE_OFFSET + TABLE_SIZE]
    if not partitions(table):
        raise FlashError("not a merged image: no partition table at 0x8000")
    return table


def app_offset(image: bytes) -> int:
    """Where the image's app partition starts (its first app partition), checked: the app is in the image."""
    apps = [p for p in partitions(image_table(image)) if p[0] == 0]
    if not apps:
        raise FlashError("the image's partition table has no app partition")
    off, size = apps[0][2], apps[0][3]
    if len(image) <= off or image[off] != 0xE9:
        raise FlashError(f"no app in the image at {off:#x}")
    if len(image) - off > size:
        raise FlashError(f"the image's app ({len(image) - off} B) is larger than its partition ({size} B)")
    return off


def esptool(port: str, *args: str) -> None:
    cmd = [sys.executable, "-m", "esptool", "--chip", "esp32s3", "--port", port, "--baud", str(BAUD), *args]
    print("$ esptool", " ".join(cmd[cmd.index("--chip"):]), flush=True)
    rc = subprocess.run(cmd).returncode
    if rc:
        raise FlashError(f"esptool failed (exit code {rc})")


def flash(port: str, image_path: Path, mode: str) -> str:
    """Flash `image_path`; returns what was written ("app" or "full")."""
    image = image_path.read_bytes()
    if mode == "full":
        image_table(image)
        esptool(port, "write-flash", "0x0", str(image_path))
        return "full"
    off = app_offset(image)
    with tempfile.TemporaryDirectory() as tmp:
        got = Path(tmp) / "table.bin"
        print("reading the remote's partition table", flush=True)
        esptool(port, "read-flash", hex(TABLE_OFFSET), hex(TABLE_SIZE), str(got))
        if got.read_bytes() != image_table(image):
            print("the remote's partition table is not this image's: writing the whole image (the remote asks for "
                  "its remote check again)", flush=True)
            esptool(port, "write-flash", "0x0", str(image_path))
            return "full"
        app = Path(tmp) / "app.bin"
        app.write_bytes(image[off:])
        print(f"writing the app only ({len(image) - off} B at {off:#x}): the remote check, patterns and settings "
              "stay", flush=True)
        esptool(port, "write-flash", hex(off), str(app))
    return "app"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True)
    ap.add_argument("--mode", choices=["app", "full"], default="app")
    ap.add_argument("image", type=Path)
    a = ap.parse_args(argv)
    try:
        flash(a.port, a.image, a.mode)
    except (FlashError, OSError) as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
