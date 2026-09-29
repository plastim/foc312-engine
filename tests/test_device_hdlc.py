"""HDLC framing must be byte-identical to the vendored restim implementation."""

import importlib.util
import pathlib
import sys

import pytest

from stimengine.device import hdlc

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_vendored_hdlc():
    path = ROOT / "vendor" / "restim" / "device" / "focstim" / "hdlc.py"
    spec = importlib.util.spec_from_file_location("vendored_hdlc", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["vendored_hdlc"] = mod
    spec.loader.exec_module(mod)
    return mod.HDLC


VendoredHDLC = _load_vendored_hdlc()

PAYLOADS = [
    b"",
    b"\x00",
    b"hello",
    bytes(range(256)),
    b"\x7e\x7d\x7e\x7d" * 10,
    b"\x7d\x5e\x7d\x5d",  # looks escaped already
    bytes(1000),
]


@pytest.mark.parametrize("payload", PAYLOADS)
def test_encode_matches_vendored(payload):
    assert hdlc.encode(payload) == VendoredHDLC.encode(payload)


@pytest.mark.parametrize("payload", PAYLOADS)
def test_roundtrip_own_decoder(payload):
    dec = hdlc.HDLCDecoder(max_len=2048)
    assert dec.parse(hdlc.encode(payload)) == [payload]


@pytest.mark.parametrize("payload", PAYLOADS)
def test_vendored_decoder_accepts_ours(payload):
    vdec = VendoredHDLC(max_len=2048)
    assert vdec.parse(hdlc.encode(payload)) == [payload]


def test_streaming_in_chunks_and_garbage():
    frames = [b"one", b"two\x7e", b"three\x7d"]
    stream = b"\xff\x01garbage" + b"".join(hdlc.encode(f) for f in frames) + b"\x12partial"
    dec = hdlc.HDLCDecoder()
    got = []
    for i in range(0, len(stream), 3):
        got += dec.parse(stream[i : i + 3])
    assert got == frames


def test_bad_crc_dropped():
    frame = bytearray(hdlc.encode(b"payload"))
    frame[2] ^= 0x01  # corrupt a payload byte
    assert hdlc.HDLCDecoder().parse(bytes(frame)) == []


def test_overrun_resyncs():
    dec = hdlc.HDLCDecoder(max_len=8)
    assert dec.parse(hdlc.encode(bytes(20))) == []
    assert dec.parse(hdlc.encode(b"ok")) == [b"ok"]
