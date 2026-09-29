"""Flash a FOC-Stim V4 .hex over the box's USB serial port (the same route as restim's Tools -> Firmware updater).

The ESP32-S3 on COM13 bridges USB to the STM32's USART. The tool asks the running firmware to jump to the STM32 ROM
bootloader (RequestDebugEnterBootloader), then speaks the ROM bootloader protocol (AN3155) through the bridge with
stm32loader at 115200 8E1: erase, write, read back and verify, then start the new image. It is a port of restim
v1.66 qt_ui/focstim_flash_dialog.py with extra refusals.

    python firmware/flash.py <file.hex> --sha256 <expected> [--port COM13]
    python firmware/flash.py <file.hex> --dry-run            # hash and address checks only, no serial

Rules (CLAUDE.md, notes/handoff-foc312.md): flash only on PlaStim's go for that flash; stop the engine and close restim
first (COM13 has one owner); keep firmware/release/focstim_v4_firmware_v1.3.2_stock.hex to restore; run the
one-resistor check before a body. If the bootloader can't be entered, hold the STM32 boot button while switching
the box on and run it again (the tool then finds the bootloader already active).

Needs: pyserial, intelhex, stm32loader from diglet48/stm32loader@c71b592 (branch feat/device-table, which knows the
G473, product id 0x469) -- the same stm32loader restim ships.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
import time
from pathlib import Path

BAUD = 115200
PRODUCT_ID_G473 = 0x469
FLASH_KB = 256
# restim's allowed ranges (both banks of the G473's 256 KB dual-bank flash)
ALLOWED = ((0x0800_0000, 0x0802_0000), (0x0804_0000, 0x0806_0000))
RESTORE_HEX = Path(__file__).resolve().parent / "release" / "focstim_v4_firmware_v1.3.2_stock.hex"


def log(msg: str) -> None:
    print(msg, flush=True)


def enter_bootloader_frame() -> bytes:
    """HDLC frame of RpcMessage{request{id 123, request_debug_enter_bootloader{}}} (== restim's literal bytes)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from stimengine.device import hdlc
    from stimengine.device.proto.focstim_rpc_pb2 import RpcMessage

    m = RpcMessage()
    m.request.id = 123
    m.request.request_debug_enter_bootloader.SetInParent()
    return hdlc.encode(m.SerializeToString())


def check_hex(path: Path, expected_sha: str | None):
    from intelhex import IntelHex

    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    log(f"hex {path} ({len(raw)} B), SHA-256 {sha}")
    if expected_sha is not None and sha.lower() != expected_sha.lower():
        raise SystemExit(f"REFUSED: SHA-256 mismatch, expected {expected_sha}")
    ih = IntelHex(str(path))
    ih.padding = 0x00
    segs = ih.segments(min_gap=128)
    total = 0
    for s, e in segs:
        ok = any(lo <= s <= hi and lo <= e <= hi for lo, hi in ALLOWED)
        log(f"  segment 0x{s:08x}-0x{e:08x} ({e - s} B){'' if ok else '  <- OUTSIDE the allowed flash ranges'}")
        if not ok:
            raise SystemExit("REFUSED: the image has an address range outside the FOC-Stim V4 flash")
        total += e - s
    if not segs or segs[0][0] != 0x0800_0000:
        raise SystemExit("REFUSED: the image does not start at 0x08000000 (no vector table there)")
    log(f"  {total} B to write")
    return ih, segs


def poke(ser, stm32) -> bool:
    for attempt in range(4):
        ser.reset_input_buffer()
        stm32.write(stm32.Command.SYNCHRONIZE)
        got = bytearray(ser.read())
        if got and got[0] in (stm32.Reply.ACK, stm32.Reply.NACK):
            log(f"  bootloader answered {bytes(got)!r}: active")
            return True
        if got and got[0] == ord("~"):
            log(f"  got an HDLC frame {bytes(got)!r}: the application is running, not the bootloader")
            return False
        log(f"  got {bytes(got)!r}, retrying")
    return False


def flash(port: str, ih, segs, backup: Path | None = None) -> None:
    import serial
    import stm32loader.bootloader as bl

    log(f"opening {port} at {BAUD} 8E1")
    try:
        ser = serial.Serial(port=port, baudrate=BAUD, bytesize=8, parity="E", stopbits=1,
                            xonxoff=False, rtscts=False, timeout=1)
    except serial.SerialException as e:
        raise SystemExit(f"cannot open {port}: {e}\n(stop the engine and close restim: COM13 has one owner)")
    # restim: on Windows, toggling RTS/DTR on close resets the ESP32 bridge; hold them low
    ser.setRTS(False)
    ser.setDTR(False)
    with ser:
        stm32 = bl.Stm32Bootloader(ser, verbosity=0, show_progress=False, device_family=None)
        log("talking to the bootloader...")
        active = poke(ser, stm32)
        if not active:
            log("asking the firmware to enter the bootloader")
            ser.write(enter_bootloader_frame())
            ser.reset_input_buffer()
            time.sleep(0.05)
            active = poke(ser, stm32)
        if not active:
            raise SystemExit("ERROR: bootloader not active. Hold the boot button while switching the box on, "
                             "then run this again. Nothing was written.")

        stm32.detect_device()
        kb = stm32.get_flash_size()
        log(f"device: {stm32.device}, flash {kb} KB")
        if stm32.device.product_id != PRODUCT_ID_G473 or kb != FLASH_KB:
            raise SystemExit(f"REFUSED: not a FOC-Stim V4 (want product 0x{PRODUCT_ID_G473:03x}, {FLASH_KB} KB). "
                             "Nothing was written.")

        if backup is not None:
            # read the whole 256 KB (both banks, the same ranges an image may use) before anything is erased: an exact
            # restore image of whatever this box ran (e.g. a stock version no longer published upstream)
            from intelhex import IntelHex
            out = IntelHex()
            for lo, hi in ALLOWED:
                log(f"backing up 0x{lo:08x}-0x{hi:08x} ...")
                try:
                    data = stm32.read_memory_data(lo, hi - lo)
                except Exception as err:  # noqa: BLE001 - e.g. read protection
                    raise SystemExit(f"REFUSED: backup read failed ({err}); nothing was erased")
                out.frombytes(bytes(data), offset=lo)
            backup.parent.mkdir(parents=True, exist_ok=True)
            out.write_hex_file(str(backup))
            log(f"backup written: {backup} (SHA-256 {hashlib.sha256(backup.read_bytes()).hexdigest()})")

        # The box's ESP32 forwards everything that arrives over Wi-Fi to the STM32 as well: a Wi-Fi client (the M5
        # remote reconnecting every few seconds) can drop stray bytes into the bootloader conversation, and one such
        # byte failed a whole flash (2026-09-28, "0x31 programming failed: 0x49"). So every block is retried on a
        # garbled answer, and a verify mismatch erases and writes again; nothing counts until the read-back matches.
        def retried(what, fn, tries=8):
            for attempt in range(tries):
                try:
                    return fn()
                except (bl.CommandError, bl.DataLengthError) as err:
                    if attempt == tries - 1:
                        raise
                    log(f"  {what}: {err}; retrying ({attempt + 1})")
                    time.sleep(0.3)
                    ser.reset_input_buffer()

        def write_all(s, data):
            for off in range(0, len(data), 256):
                block = data[off:off + 256]
                retried(f"write 0x{s + off:08x}", lambda a=s + off, b=block: stm32.write_memory(a, b))

        def read_all(s, n):
            out = bytearray()
            for off in range(0, n, 256):
                k = min(256, n - off)
                out += retried(f"read 0x{s + off:08x}", lambda a=s + off, kk=k: stm32.read_memory(a, kk))
            return out

        for attempt in range(3):
            log("erasing flash (from here until 'verified' the box has no valid firmware)...")
            retried("erase", stm32.extended_erase_memory)
            for s, e in segs:
                data = ih.tobinarray(s, e - 1)
                log(f"writing {len(data)} B at 0x{s:08x}")
                write_all(s, data)
            bad = None
            for s, e in segs:
                log(f"verifying 0x{s:08x}...")
                data = ih.tobinarray(s, e - 1)
                got = read_all(s, e - s)
                if bytes(got) != bytes(data):
                    bad = next(i for i, (x, y) in enumerate(zip(got, data)) if x != y)
                    log(f"  mismatch at 0x{s + bad:08x}; erasing and writing again")
                    break
            if bad is None:
                break
        else:
            raise SystemExit(f"VERIFY FAILED three times. The bootloader is still active: run this again "
                             f"(or flash {RESTORE_HEX.name} to restore stock).")
        log("verified; starting the new image")
        stm32.go(0x0800_0000)


async def read_version(port: str) -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from stimengine.device.client import FocStimClient
    from stimengine.device.transport import SerialTransport

    client = FocStimClient(SerialTransport(port))
    await client.connect()
    try:
        v = await client.firmware_version()
        f = v.stm32_firmware_version_2
        return f"{f.major}.{f.minor}.{f.revision} branch {f.branch!r} comment {f.comment!r}"
    finally:
        await client.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("hex", type=Path)
    ap.add_argument("--port", default="COM13")
    ap.add_argument("--sha256", help="expected SHA-256 of the .hex; required unless --dry-run")
    ap.add_argument("--dry-run", action="store_true", help="check the file only; do not open the port")
    ap.add_argument("--backup", type=Path, help="read the box's whole flash to this .hex before erasing")
    args = ap.parse_args()

    if not args.hex.is_file():
        raise SystemExit(f"no such file: {args.hex}")
    if not args.dry_run and not args.sha256:
        raise SystemExit("REFUSED: pass --sha256 <expected> (the hash recorded for this build) to flash")
    ih, segs = check_hex(args.hex, args.sha256)
    if not RESTORE_HEX.is_file():
        log(f"WARNING: the stock restore image {RESTORE_HEX} is missing")
    if args.dry_run:
        log("dry run: file checks passed; nothing sent")
        return

    flash(args.port, ih, segs, args.backup)
    for attempt in range(5):
        time.sleep(2)
        try:
            log(f"firmware now: {asyncio.run(read_version(args.port))}")
            return
        except Exception as e:  # the ESP32 bridge / STM32 may still be restarting
            log(f"  version read failed ({type(e).__name__}: {e}), retrying")
    log("could not read the version back: power-cycle the box and check with "
        "`python -m stimengine.device.probe --serial COM13 --seconds 3`")


if __name__ == "__main__":
    main()
