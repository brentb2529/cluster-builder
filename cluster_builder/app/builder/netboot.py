"""Network boot (PXE): the boxes fetch iPXE from this add-on over TFTP, iPXE asks /boot/<mac> what to do, and a
machine assigned to a node gets the Proxmox auto-installer (kernel, initrd and the built ISO) over HTTP.
Every other machine is told to start from its own disk, so a node left on "network first" is safe."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state import State

KERNEL_FILE = "pve-kernel"
INITRD_FILE = "pve-initrd.img"
IPXE_DIR = Path(os.environ.get("CB_IPXE_DIR", "/usr/lib/ipxe"))
LOADERS = {  # what the router's "boot file name" may point at -> file in the ipxe package
    "ipxe.efi": "ipxe.efi",  # x86-64 UEFI (the usual mini PC)
    "ipxe-arm64.efi": "ipxe-arm64.efi",
    "snponly.efi": "snponly.efi",
    "undionly.kpxe": "undionly.kpxe",  # legacy BIOS
}
UNKNOWN_TRIES = 40  # x 15 s: a machine nobody assigns gives up after ten minutes
DEFAULT_CMDLINE = "ro ramdisk_size=16777216 rw quiet splash=silent proxmox-start-auto-installer"


def norm_mac(mac: str) -> str:
    return mac.strip().lower().replace("-", ":")


def auto_cmdline(grub_cfg: str) -> str:
    """The kernel command line of the ISO's 'Automated' menu entry, so the network boot runs the same installer."""
    for m in re.finditer(r"^\s*linux\s+(\S+)\s+(.*)$", grub_cfg, re.M):
        if "proxmox-start-auto-installer" in m.group(2):
            return m.group(2).strip()
    return DEFAULT_CMDLINE


def autoexec(state: State) -> str:
    """iPXE loads this from the TFTP server it came from, right after the firmware hands over to it."""
    base = state.settings["builder_url"].rstrip("/")
    return (
        "#!ipxe\n"
        "# Cluster Builder: ask the add-on whether this machine installs or starts from its own disk\n"
        "dhcp || dhcp\n"
        f"chain --replace {base}/boot/${{net0/mac}} || exit\n"
    )


def tftp_file(state: State, name: str) -> bytes | None:
    if name == "autoexec.ipxe":
        return autoexec(state).encode()
    src = LOADERS.get(name)
    if src is None:
        return None
    p = IPXE_DIR / src
    return p.read_bytes() if p.is_file() else None


def ready(state: State) -> bool:
    nb = state.build.get("netboot") or {}
    return bool(nb) and all((state.downloads / f).is_file() for f in (KERNEL_FILE, INITRD_FILE))


def boot_script(state: State, mac: str, tries: int, remote: str) -> str:
    """What iPXE on the machine with *mac* should do now."""
    mac = norm_mac(mac)
    base = state.settings["builder_url"].rstrip("/")
    token = state.secrets["answer_token"]
    what, name = state.netboot_decision(mac, remote)
    if what == "local":
        return f"#!ipxe\necho Cluster Builder: {name} is installed, starting it from its own disk\nsleep 2\nexit\n"
    if what == "install":
        if not ready(state):
            return _again(
                base, mac, tries, f"{name} is assigned to this machine but there is no build yet: press Build"
            )
        nb = state.build["netboot"]
        state.nodes[name]["netboot_ip"] = remote
        state.log(
            name,
            "netboot",
            f"{mac} is loading the installer over the network (1.7 GB, usually a minute), then starts it",
        )
        from . import build  # local import: build imports state

        return (
            "#!ipxe\n"
            f"echo Cluster Builder: installing Proxmox on {name} ({mac}); this wipes its disk\n"
            f"kernel {base}/files/{token}/{KERNEL_FILE} {nb['cmdline']}\n"
            f"initrd {base}/files/{token}/{INITRD_FILE}\n"
            f"initrd {base}/iso/{token}/{build.ISO_NAME} /proxmox.iso\n"
            "boot || shell\n"
        )
    return _again(
        base, mac, tries, f"this machine ({mac}) is not one of the nodes yet: assign it on the Cluster Builder page"
    )


def _again(base: str, mac: str, tries: int, why: str) -> str:
    if tries >= UNKNOWN_TRIES:
        return f"#!ipxe\necho Cluster Builder: {why}; giving up, starting from disk\nsleep 3\nexit\n"
    return (
        "#!ipxe\n"
        f"echo Cluster Builder: {why}\n"
        f"echo Checking again in 15 seconds (try {tries + 1} of {UNKNOWN_TRIES})\n"
        "sleep 15\n"
        f"chain --replace {base}/boot/{mac}?n={tries + 1} || exit\n"
    )
