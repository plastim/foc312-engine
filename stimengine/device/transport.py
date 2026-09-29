"""Byte transports to the FOC-Stim: USB serial (115200) or TCP (WiFi, port 55533). Same stream either way."""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Callable, Optional

import serial_asyncio

logger = logging.getLogger("engine.device.transport")

BytesCallback = Callable[[bytes], None]
ClosedCallback = Callable[[Optional[Exception]], None]

SERIAL_BAUD = 115200
TCP_PORT = 55533
SERIAL_SETTLE_S = 0.1  # restim waits 100 ms after opening, then discards buffered input


class TransportError(Exception):
    """Raised when the link cannot be opened or dies."""


class BaseTransport:
    """Common read-loop plumbing. Subclasses implement `_open()` returning (reader, writer)."""

    name = "transport"

    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._on_bytes: BytesCallback | None = None
        self._on_closed: ClosedCallback | None = None
        self.bytes_in = 0
        self.bytes_out = 0

    @property
    def is_open(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self, on_bytes: BytesCallback, on_closed: ClosedCallback | None = None) -> None:
        """Open the link and start the read loop; `on_bytes` receives raw chunks as they arrive."""
        self._on_bytes = on_bytes
        self._on_closed = on_closed
        try:
            self._reader, self._writer = await self._open()
        except (OSError, asyncio.TimeoutError, ValueError) as exc:
            raise TransportError(f"{self.name}: could not open: {exc}") from exc
        await self._after_open()
        self._read_task = asyncio.create_task(self._read_loop(), name=f"{self.name}-read")

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        raise NotImplementedError

    async def _after_open(self) -> None:
        return None

    def write(self, data: bytes) -> None:
        if not self.is_open:
            raise TransportError(f"{self.name}: write on closed link")
        assert self._writer is not None
        self._writer.write(data)
        self.bytes_out += len(data)

    async def drain(self) -> None:
        if self._writer is not None:
            try:
                await self._writer.drain()
            except (OSError, ConnectionError):
                pass

    async def close(self) -> None:
        if self._read_task is not None and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except BaseException:  # noqa: BLE001 - cancelled or failed, either way we are done
                pass
        self._read_task = None
        if self._writer is not None:
            try:
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), timeout=1.0)
            except Exception:  # noqa: BLE001 - best-effort close
                pass
        self._writer = None
        self._reader = None

    async def _read_loop(self) -> None:
        assert self._reader is not None
        error: Exception | None = None
        try:
            while True:
                chunk = await self._reader.read(256)
                if not chunk:
                    error = TransportError(f"{self.name}: link closed by peer")
                    break
                self.bytes_in += len(chunk)
                if self._on_bytes is not None:
                    self._on_bytes(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface any read failure to the owner
            error = exc
        if self._on_closed is not None:
            self._on_closed(error)


class SerialTransport(BaseTransport):
    """USB serial link (pyserial-asyncio)."""

    def __init__(self, port: str, baud: int = SERIAL_BAUD) -> None:
        super().__init__()
        self.port = port
        self.baud = baud
        self.name = f"serial {port}"

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await serial_asyncio.open_serial_connection(url=self.port, baudrate=self.baud)

    async def _after_open(self) -> None:
        # Mirror restim: settle, then throw away whatever the box sent before we were listening.
        await asyncio.sleep(SERIAL_SETTLE_S)
        try:
            ser = self._writer.transport.serial  # type: ignore[union-attr]
            ser.reset_input_buffer()
        except Exception:  # noqa: BLE001 - not fatal
            pass


class TcpTransport(BaseTransport):
    """WiFi link: plain TCP to the box with Nagle disabled."""

    def __init__(self, host: str, port: int = TCP_PORT, connect_timeout: float = 5.0) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.name = f"tcp {host}:{port}"

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=self.connect_timeout
        )
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return reader, writer


class MemoryTransport(BaseTransport):
    """In-process transport for tests: `feed()` bytes from a fake device, inspect `sent`."""

    name = "memory"

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[bytes] = []
        self._open_flag = False

    @property
    def is_open(self) -> bool:
        return self._open_flag

    async def connect(self, on_bytes: BytesCallback, on_closed: ClosedCallback | None = None) -> None:
        self._on_bytes = on_bytes
        self._on_closed = on_closed
        self._open_flag = True

    def write(self, data: bytes) -> None:
        if not self._open_flag:
            raise TransportError("memory: write on closed link")
        self.sent.append(data)
        self.bytes_out += len(data)

    async def drain(self) -> None:
        return None

    async def close(self) -> None:
        self._open_flag = False

    def feed(self, data: bytes) -> None:
        """Simulate bytes arriving from the device."""
        self.bytes_in += len(data)
        if self._on_bytes is not None:
            self._on_bytes(data)

    def drop(self, error: Exception | None = None) -> None:
        """Simulate the link dying."""
        self._open_flag = False
        if self._on_closed is not None:
            self._on_closed(error)
