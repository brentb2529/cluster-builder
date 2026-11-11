"""The Build button: download the Proxmox ISO and the HA OS image, write the answer files, make the
one USB-stick image that serves all nodes."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

import aiohttp

from . import answers, netboot, supervisor
from .state import HAOS_IMAGE_FALLBACK, HAOS_RELEASES_API, State

_LOGGER = logging.getLogger(__name__)
ISO_NAME = "cluster-builder.iso"
PVE_ISO_FILE = "proxmox-ve.iso"
IMAGE_FILE = "haos.qcow2.xz"
ASSISTANT = "proxmox-auto-install-assistant"
SHARE = Path(
    os.environ.get("CB_SHARE", "/share/cluster_builder")
)  # files put here by hand are used instead of downloads


def provided(name: str) -> Path | None:
    p = SHARE / name
    return p if p.is_file() and p.stat().st_size > 0 else None


class BuildError(Exception):
    pass


async def _download(session: aiohttp.ClientSession, url: str, dest: Path, state: State, label: str) -> None:
    """Resume-free streaming download with progress; keeps an existing complete file."""
    if provided(dest.name):
        state.build["step"] = f"{label}: using the file in {SHARE}"
        state.save()
        return
    marker = dest.with_suffix(dest.suffix + ".url")
    if dest.exists() and marker.exists() and marker.read_text().strip() == url:
        state.build["step"] = f"{label}: already downloaded"
        state.save()
        return
    tmp = dest.with_suffix(dest.suffix + ".part")
    state.build["step"] = f"{label}: downloading"
    state.build["progress"] = 0.0
    state.save()
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=None, sock_read=120)) as resp:
        if resp.status != 200:
            raise BuildError(f"{label}: HTTP {resp.status} from {url}")
        total = resp.content_length or 0
        done = 0
        last = 0.0
        with tmp.open("wb") as f:
            async for chunk in resp.content.iter_chunked(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total and time.time() - last > 1:
                    state.build["progress"] = done / total
                    state.build["step"] = f"{label}: downloading {done >> 20} of {total >> 20} MB"
                    state.save()
                    last = time.time()
    tmp.replace(dest)
    marker.write_text(url)
    state.build["progress"] = 1.0
    state.save()


async def _latest_haos_url(session: aiohttp.ClientSession) -> str:
    try:
        async with session.get(HAOS_RELEASES_API, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status == 200:
                rel = await resp.json()
                for a in rel.get("assets", []):
                    if re.fullmatch(r"haos_generic-x86-64-[\d.]+\.qcow2\.xz", a.get("name", "")):
                        return a["browser_download_url"]
    except Exception:
        _LOGGER.warning("Could not look up the latest HA OS release; using %s", HAOS_IMAGE_FALLBACK)
    return HAOS_IMAGE_FALLBACK


async def _run(*cmd: str) -> str:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    text = out.decode(errors="replace")
    if proc.returncode != 0:
        raise BuildError(f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} failed:\n{text[-2000:]}")
    return text


async def run_build(state: State) -> None:
    b = state.build
    b.update({"status": "running", "step": "starting", "progress": 0.0, "error": ""})
    state.save()
    try:
        errs = state.validate()
        if errs:
            raise BuildError(" ".join(errs))
        s = state.settings
        base = s["builder_url"].rstrip("/")
        token = state.secrets["answer_token"]
        async with aiohttp.ClientSession(headers={"User-Agent": "cluster-builder"}) as session:
            if s["vm"]["enabled"] and s["vm"].get("move_in"):
                b["step"] = "backing up this Home Assistant for the move"
                b["move_in_backup"] = None
                state.save()

                def _bp(step: str, frac: float) -> None:
                    b["step"], b["progress"] = step, frac
                    state.save()

                b["move_in_backup"] = await supervisor.make_backup(
                    session, state.secrets["move_in_password"], state.downloads / supervisor.BACKUP_FILE, _bp
                )
            if s["vm"]["enabled"]:
                url = s["image_url"] or await _latest_haos_url(session)
                if url.startswith(base + "/files/"):
                    # a file placed in the downloads folder by hand (lab use)
                    b["image_served_url"] = url
                else:
                    await _download(session, url, state.downloads / IMAGE_FILE, state, "Home Assistant OS image")
                    b["image_served_url"] = f"{base}/files/{token}/{IMAGE_FILE}"
                b["image"] = url
            await _download(session, s["iso_url"], state.downloads / PVE_ISO_FILE, state, "Proxmox installer")

        b["step"] = "checking the answer files"
        b["progress"] = 0.0
        state.save()
        with tempfile.TemporaryDirectory() as d:
            for name in state.node_names():
                p = Path(d) / f"{name}.toml"
                p.write_text(answers.answer_for(state, name))
                await _run(ASSISTANT, "validate-answer", str(p))
                answers.firstboot_for(state, name)  # template must render
        b["step"] = "writing the USB stick image (a few minutes)"
        state.save()
        out = state.downloads / ISO_NAME
        tmp = state.downloads / "cluster-builder.tmp.iso"
        tmp.unlink(missing_ok=True)
        log = await _run(
            ASSISTANT,
            "prepare-iso",
            str(provided(PVE_ISO_FILE) or state.downloads / PVE_ISO_FILE),
            "--fetch-from",
            "http",
            "--url",
            f"{base}/answer",
            "--answer-auth-token",
            f"cluster-builder:{token}",
            "--output",
            str(tmp),
        )
        _LOGGER.info("prepare-iso: %s", log.strip()[-800:])
        if not tmp.exists():
            raise BuildError(f"the installer image was not written:\n{log[-1500:]}")
        tmp.replace(out)
        b["netboot"] = None
        if s.get("install_method") == "network":
            b["step"] = "taking the kernel and initrd out of it for network boot"
            state.save()
            b["netboot"] = await netboot_files(out, state.downloads)
        b.update({"status": "done", "step": "ready", "progress": 1.0, "iso": str(out), "built_at": time.time()})
        state.save()
    except Exception as err:
        _LOGGER.exception("build failed")
        b.update({"status": "error", "error": str(err)})
        state.save()


async def netboot_files(iso: Path, downloads: Path) -> dict[str, str]:
    """Kernel, initrd and the auto-installer's command line from the built ISO (iPXE feeds the ISO itself to
    the installer as /proxmox.iso, which is where its init looks for it)."""
    kernel, initrd = downloads / netboot.KERNEL_FILE, downloads / netboot.INITRD_FILE
    with tempfile.TemporaryDirectory() as d:
        grub = Path(d) / "grub.cfg"
        for f in (kernel, initrd):
            f.unlink(missing_ok=True)
        await _run(
            "xorriso",
            "-osirrox",
            "on",
            "-indev",
            str(iso),
            "-extract",
            "/boot/linux26",
            str(kernel),
            "-extract",
            "/boot/initrd.img",
            str(initrd),
            "-extract",
            "/boot/grub/grub.cfg",
            str(grub),
        )
        cmdline = netboot.auto_cmdline(grub.read_text(errors="replace")) if grub.exists() else netboot.DEFAULT_CMDLINE
    for f in (kernel, initrd):
        if not f.is_file() or f.stat().st_size == 0:
            raise BuildError(f"could not take {f.name} out of the installer image")
        f.chmod(0o644)
    return {"kernel": kernel.name, "initrd": initrd.name, "cmdline": cmdline}


def disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


def iso_ready(state: State) -> bool:
    p = Path(state.build.get("iso") or "")
    return bool(state.build.get("iso")) and p.exists() and os.path.getsize(p) > 0
