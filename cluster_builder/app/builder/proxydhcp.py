"""ProxyDHCP: tells network-booting machines where their boot loader is, without touching the router.

A machine set to boot from the network broadcasts a DHCP DISCOVER tagged "PXEClient". The router's DHCP server
hands out the address as usual; this server answers the same DISCOVER with an offer that carries no address,
only the boot server (this add-on) and the boot file (iPXE for the machine's firmware type). PXE firmware merges
the two, fetches iPXE over TFTP from here and the rest is netboot.py. Nothing else on the network gets a reply:
only DISCOVERs with the PXEClient class on port 67, and PXE boot-server REQUESTs on port 4011, are answered."""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from collections.abc import Callable
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

PORT = 67
PXE_PORT = 4011
CLIENT_PORT = 68
MAGIC = b"\x63\x82\x53\x63"
DISCOVER, OFFER, REQUEST, ACK = 1, 2, 3, 5
# client system architecture (option 93, RFC 4578 / IANA) -> boot loader served by netboot.py
ARCH_LOADER = {
    0: "undionly.kpxe",  # legacy BIOS
    6: "ipxe.efi",  # 32-bit UEFI: ipxe.efi still does not run, but it is the closest we have
    7: "ipxe.efi",  # x86-64 UEFI (the usual mini PC)
    9: "ipxe.efi",
    11: "ipxe-arm64.efi",
}
IPXE_SCRIPT = "autoexec.ipxe"


@dataclass
class Request:
    xid: bytes
    flags: int
    ciaddr: bytes
    giaddr: bytes
    chaddr: bytes  # 16 bytes, hlen of them used
    hlen: int
    msg_type: int
    vendor_class: bytes
    arch: int | None
    uuid: bytes | None
    user_class: bytes | None

    @property
    def mac(self) -> str:
        return ":".join(f"{b:02x}" for b in self.chaddr[: self.hlen])

    @property
    def is_pxe(self) -> bool:
        return self.vendor_class.startswith(b"PXEClient")


def parse(data: bytes) -> Request | None:
    """A BOOTREQUEST with the DHCP magic, or None for anything else (short, a reply, not DHCP)."""
    if len(data) < 240 or data[0] != 1 or data[236:240] != MAGIC:
        return None
    hlen = min(data[2], 16)
    opts: dict[int, bytes] = {}
    i = 240
    while i < len(data):
        code = data[i]
        if code == 255:
            break
        if code == 0:
            i += 1
            continue
        if i + 1 >= len(data):
            break
        n = data[i + 1]
        opts[code] = opts.get(code, b"") + data[i + 2 : i + 2 + n]  # a repeated option is concatenated
        i += 2 + n
    if len(opts.get(53, b"")) != 1:
        return None
    arch = struct.unpack("!H", opts[93][:2])[0] if len(opts.get(93, b"")) >= 2 else None
    return Request(
        xid=data[4:8],
        flags=struct.unpack("!H", data[10:12])[0],
        ciaddr=data[12:16],
        giaddr=data[24:28],
        chaddr=data[28:44],
        hlen=hlen,
        msg_type=opts[53][0],
        vendor_class=opts.get(60, b""),
        arch=arch,
        uuid=opts.get(97),
        user_class=opts.get(77),
    )


def loader_for(req: Request) -> str | None:
    """Which file the machine should fetch over TFTP; None when its firmware type is not one we have a loader for."""
    if req.user_class and b"iPXE" in req.user_class:
        return IPXE_SCRIPT  # iPXE itself asking (after it loaded): hand it our script, not itself again
    if req.arch is None:
        return ARCH_LOADER[0]  # old BIOS clients do not say
    return ARCH_LOADER.get(req.arch)


def reply(req: Request, msg_type: int, server_ip: str, loader: str) -> bytes:
    """A BOOTREPLY with no address: boot server and file only, which is what ProxyDHCP is for."""
    sip = socket.inet_aton(server_ip)
    head = struct.pack("!BBBB4sHH", 2, 1, req.hlen, 0, req.xid, 0, req.flags)
    head += req.ciaddr + b"\0" * 4 + sip + req.giaddr + req.chaddr  # ciaddr, yiaddr=0, siaddr=us, giaddr, chaddr
    head += b"\0" * 64  # sname
    head += loader.encode().ljust(128, b"\0")  # file
    opts = bytes([53, 1, msg_type]) + bytes([54, 4]) + sip + bytes([60, 9]) + b"PXEClient"
    opts += bytes([67, len(loader)]) + loader.encode()  # some firmware reads the file name from here, not the header
    if req.uuid is not None:
        opts += bytes([97, len(req.uuid)]) + req.uuid
    # PXE vendor options: discovery control bit 3 = "use the boot file in this offer, skip boot-server discovery"
    pxe = bytes([6, 1, 8, 255])
    opts += bytes([43, len(pxe)]) + pxe + bytes([255])
    return head + MAGIC + opts


class ProxyDHCP(asyncio.DatagramProtocol):
    def __init__(self, server_ip: Callable[[], str | None], seen: Callable[[str, str], None] | None, port: int):
        self.server_ip = server_ip
        self.seen = seen
        self.port = port
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        req = parse(data)
        if req is None or not req.is_pxe or self.transport is None:
            return
        wanted = DISCOVER if self.port == PORT else REQUEST
        if req.msg_type != wanted:
            return
        server_ip = self.server_ip()
        loader = loader_for(req)
        if not server_ip or loader is None:
            if loader is None:
                _LOGGER.info(
                    "Network boot request from %s with firmware type %s: no boot loader for it", req.mac, req.arch
                )
            return
        if self.seen:
            self.seen(req.mac, loader)
        out = reply(req, OFFER if wanted == DISCOVER else ACK, server_ip, loader)
        if req.giaddr != b"\0" * 4:
            dest = (socket.inet_ntoa(req.giaddr), PORT)
        elif self.port == PXE_PORT and addr[0] not in ("0.0.0.0", ""):
            dest = (addr[0], addr[1])
        else:
            dest = ("255.255.255.255", CLIENT_PORT)  # the machine has no address yet (or wants a broadcast)
        try:
            self.transport.sendto(out, dest)
        except OSError as err:
            _LOGGER.warning("Could not answer network boot request from %s: %s", req.mac, err)


def _socket(port: int, host: str) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind((host, port))
    return s


async def start(
    server_ip: Callable[[], str | None],
    seen: Callable[[str, str], None] | None = None,
    ports: tuple[int, int] = (PORT, PXE_PORT),
    host: str = "0.0.0.0",
) -> list[asyncio.DatagramTransport]:
    """Listen on the DHCP and PXE boot-server ports; raises OSError when a port is taken."""
    loop = asyncio.get_running_loop()
    transports: list[asyncio.DatagramTransport] = []
    try:
        for port, logical in zip(ports, (PORT, PXE_PORT), strict=True):
            t, _ = await loop.create_datagram_endpoint(
                lambda logical=logical: ProxyDHCP(server_ip, seen, logical), sock=_socket(port, host)
            )
            transports.append(t)
    except OSError:
        for t in transports:
            t.close()
        raise
    return transports
