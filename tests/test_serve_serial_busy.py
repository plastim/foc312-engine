"""serve: a BUSY serial port (the hub's Detect asking the box at that moment, a box just plugged in) is retried for a
few seconds; any other serial failure still fails at once."""
from __future__ import annotations

import argparse
import asyncio

import pytest

from stimengine.tools import serve


class FakeClient:
    def __init__(self, transport):
        pass

    async def close(self):
        pass


def fake_engine(errors):
    class E:
        def __init__(self, cfg, client, session):
            pass

        async def start(self, mode):
            if errors:
                raise errors.pop(0)
    return E


def run(errors, monkeypatch):
    monkeypatch.setattr(serve, "make_transport", lambda args: object())
    monkeypatch.setattr(serve, "FocStimClient", FakeClient)
    monkeypatch.setattr(serve, "Engine", fake_engine(errors))
    monkeypatch.setattr(serve, "SERIAL_BUSY_DELAYS", (0.01, 0.01, 0.01))
    args = argparse.Namespace(tcp=None, serial="COM17", no_signal=False, mode="fourphase", sim=False, sim_fork=False)
    return asyncio.run(serve.start_engine_with_retry(args, {}, None))


BUSY = OSError("serial COM17: could not open: could not open port 'COM17': PermissionError(13, 'Access is denied.')")


def test_a_busy_port_is_retried_then_connects(monkeypatch):
    errors = [BUSY, BUSY]
    assert run(errors, monkeypatch) is not None and errors == []


def test_a_port_busy_for_too_long_still_fails(monkeypatch):
    with pytest.raises(OSError, match="denied"):
        run([BUSY] * 5, monkeypatch)


def test_other_serial_failures_fail_at_once(monkeypatch):
    errors = [RuntimeError("handshake timed out"), None]
    with pytest.raises(RuntimeError, match="handshake"):
        run(errors, monkeypatch)
    assert errors == [None], "not retried"
