"""HDLC-style framing used on the FOC-Stim link (byte-compatible with upstream restim).

Frame = 0x7E | escaped(payload) | escaped(crc16-x25 LE) | 0x7E.  Escape byte 0x7D, XOR 0x20.
"""

from __future__ import annotations

import struct

import crc

FRAME_BOUNDARY = 0x7E
ESCAPE = 0x7D
_CRC = crc.Calculator(crc.Crc16.X25)


def crc16(payload: bytes) -> int:
    """CRC-16/X-25 as used by the firmware."""
    return _CRC.checksum(payload)


def encode(payload: bytes) -> bytes:
    """Wrap a payload in an HDLC frame."""
    if len(payload) > 65536:
        raise ValueError("maximum payload length is 65536")
    body = payload + struct.pack("<H", crc16(payload))
    out = bytearray([FRAME_BOUNDARY])
    for c in body:
        if c in (FRAME_BOUNDARY, ESCAPE):
            out.append(ESCAPE)
            out.append(c ^ 0x20)
        else:
            out.append(c)
    out.append(FRAME_BOUNDARY)
    return bytes(out)


class HDLCDecoder:
    """Incremental frame parser. Feed bytes, get complete CRC-valid payloads back."""

    def __init__(self, max_len: int = 1024) -> None:
        self._max_len = max_len
        self._escape_next = False
        self._consuming = False  # False until the first boundary marker is seen
        self._pending = bytearray()

    def parse(self, data: bytes) -> list[bytes]:
        frames: list[bytes] = []
        for c in data:
            if c == FRAME_BOUNDARY:
                if len(self._pending) >= 2:
                    payload = bytes(self._pending[:-2])
                    packet_crc = struct.unpack("<H", self._pending[-2:])[0]
                    if crc16(payload) == packet_crc:
                        frames.append(payload)
                self._reset(consuming=True)
            elif c == ESCAPE:
                self._escape_next = True
            else:
                if self._escape_next:
                    c ^= 0x20
                    self._escape_next = False
                if self._consuming:
                    self._pending.append(c)
                if len(self._pending) > self._max_len:
                    self._reset(consuming=False)
        return frames

    def _reset(self, consuming: bool) -> None:
        self._escape_next = False
        self._pending.clear()
        self._consuming = consuming
