"""A small read-only TFTP server (RFC 1350 with blksize/tsize options) for the network boot loader.

It answers from the one listening socket instead of a fresh port per transfer, so it works behind the
add-on's UDP port mapping. Only the few boot-loader files are served; everything else comes over HTTP."""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Callable
from typing import Any

_LOGGER = logging.getLogger(__name__)
RRQ, WRQ, DATA, ACK, ERROR, OACK = 1, 2, 3, 4, 5, 6
RETRIES = 5
TIMEOUT = 2.0

FileProvider = Callable[[str], bytes | None]


class _Transfer:
    def __init__(self, data: bytes, blksize: int) -> None:
        self.data = data
        self.blksize = blksize
        self.block = 0  # last block sent
        self.sent = 0
        self.retries = 0
        self.done = False
        self.timer: asyncio.TimerHandle | None = None

    def chunk(self, block: int) -> bytes:
        start = (block - 1) * self.blksize
        return self.data[start : start + self.blksize]


class TFTPServer(asyncio.DatagramProtocol):
    def __init__(self, provider: FileProvider) -> None:
        self.provider = provider
        self.transport: asyncio.DatagramTransport | None = None
        self.transfers: dict[tuple[str, int], _Transfer] = {}

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def _send(self, addr: tuple[str, int], pkt: bytes) -> None:
        if self.transport is not None:
            self.transport.sendto(pkt, addr)

    def _error(self, addr: tuple[str, int], code: int, msg: str) -> None:
        self._send(addr, struct.pack("!HH", ERROR, code) + msg.encode() + b"\0")

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < 4:
            return
        op = struct.unpack("!H", data[:2])[0]
        if op == RRQ:
            self._rrq(data[2:], addr)
        elif op == ACK:
            self._ack(struct.unpack("!H", data[2:4])[0], addr)
        elif op == WRQ:
            self._error(addr, 2, "read only")
        elif op == ERROR:
            self._drop(addr)

    def _rrq(self, body: bytes, addr: tuple[str, int]) -> None:
        parts = body.split(b"\0")
        if len(parts) < 2:
            return
        name = parts[0].decode(errors="replace").lstrip("/")
        opts: dict[str, str] = {}
        rest = [p.decode(errors="replace") for p in parts[2:] if p]
        for k, v in zip(rest[::2], rest[1::2], strict=False):
            opts[k.lower()] = v
        data = self.provider(name)
        if data is None:
            _LOGGER.info("tftp: %s asked for %s: not found", addr[0], name)
            self._error(addr, 1, "not found")
            return
        blksize = max(8, min(int(opts.get("blksize", 512) or 512), 65464))
        self._drop(addr)
        t = _Transfer(data, blksize)
        self.transfers[addr] = t
        _LOGGER.info("tftp: sending %s (%d bytes) to %s", name, len(data), addr[0])
        if opts:
            oack: list[bytes] = []
            if "blksize" in opts:
                oack += [b"blksize", str(blksize).encode()]
            if "tsize" in opts:
                oack += [b"tsize", str(len(data)).encode()]
            if oack:
                self._send(addr, struct.pack("!H", OACK) + b"\0".join(oack) + b"\0")
                self._arm(addr, t)
                return
        self._send_block(addr, t, 1)

    def _send_block(self, addr: tuple[str, int], t: _Transfer, block: int) -> None:
        chunk = t.chunk(block)
        t.block = block
        self._send(addr, struct.pack("!HH", DATA, block & 0xFFFF) + chunk)
        if len(chunk) < t.blksize:
            t.done = True
        self._arm(addr, t)

    def _arm(self, addr: tuple[str, int], t: _Transfer) -> None:
        if t.timer:
            t.timer.cancel()
        t.timer = asyncio.get_running_loop().call_later(TIMEOUT, self._timeout, addr)

    def _timeout(self, addr: tuple[str, int]) -> None:
        t = self.transfers.get(addr)
        if t is None:
            return
        if t.done or t.retries >= RETRIES:
            self._drop(addr)
            return
        t.retries += 1
        if t.block == 0:  # the OACK went unanswered
            self._send(addr, struct.pack("!H", OACK) + b"blksize\0" + str(t.blksize).encode() + b"\0")
            self._arm(addr, t)
        else:
            self._send_block(addr, t, t.block)

    def _ack(self, block: int, addr: tuple[str, int]) -> None:
        t = self.transfers.get(addr)
        if t is None:
            return
        if block == (t.block & 0xFFFF):
            t.retries = 0
            if t.done:
                self._drop(addr)
            else:
                self._send_block(addr, t, t.block + 1)

    def _drop(self, addr: tuple[str, int]) -> None:
        t = self.transfers.pop(addr, None)
        if t is not None and t.timer:
            t.timer.cancel()


async def start(provider: FileProvider, port: int = 69, host: str = "0.0.0.0") -> Any:
    """Listen for TFTP read requests; returns the transport (close() stops it)."""
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: TFTPServer(provider), local_addr=(host, port))
    return transport
