"""Move this Home Assistant into the new VM: restore the move-in backup through the Welcome screen's
own (unauthenticated, onboarding-only) API, then tell the person to shut the old machine down."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import aiohttp

from . import supervisor
from .supervisor import BACKUP_FILE

if TYPE_CHECKING:
    from .state import State

_LOGGER = logging.getLogger(__name__)
AGENT = "hassio.local"
POLL = 15.0
SETTLE = 30.0
WAIT_HA = 60 * 60
WAIT_RESTORE = 60 * 60
RESUMABLE = ("waiting_ha", "uploading", "restoring")
PORTS = (8123, 80)  # Home Assistant's classic port, and the port Home Assistant OS 18 serves it on


def candidates(ip: str, extra: int | None = None) -> list[str]:
    """Where a fresh Home Assistant may answer: a given host:port only, else the known ports (+ this HA's)."""
    if ":" in ip:
        return [f"http://{ip}"]
    ports: list[int] = []
    for p in (*PORTS, extra):
        if p and p not in ports:
            ports.append(p)
    return [f"http://{ip}:{p}" for p in ports]


def _user_done(steps: object) -> bool:
    return isinstance(steps, list) and any(
        isinstance(s, dict) and s.get("step") == "user" and s.get("done") for s in steps
    )


async def _onboarding(session: aiohttp.ClientSession, base: str) -> tuple[int, object]:
    """(status, steps) of the Welcome screen's status call; (0, None) while nothing (or a landing page) answers."""
    try:
        async with session.get(f"{base}/api/onboarding", allow_redirects=False) as r:
            if r.status != 200:
                return r.status, None
            try:
                return 200, await r.json(content_type=None)
            except ValueError:  # HTML from the "preparing Home Assistant" page
                return 0, None
    except (TimeoutError, aiohttp.ClientError):
        return 0, None


async def _core_up(session: aiohttp.ClientSession, base: str) -> bool:
    """A running Home Assistant answers /api/ with 401 when no token is sent."""
    try:
        async with session.get(f"{base}/api/", allow_redirects=False) as r:
            return r.status == 401
    except (TimeoutError, aiohttp.ClientError):
        return False


async def _set_up(session: aiohttp.ClientSession, base: str, status: int, steps: object) -> bool:
    """True when the Home Assistant at base is past onboarding (it then answers 404 or 401 there)."""
    if status == 200:
        return _user_done(steps)
    return status in (401, 404) and await _core_up(session, base)


async def run(state: State, ip: str) -> None:
    log = state.move_in_log
    backup = state.downloads / BACKUP_FILE
    if not backup.exists():
        log("failed", "there is no move-in backup here: Build again with the move-in option ticked")
        return
    try:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=900)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            bases = candidates(ip, await supervisor.core_port(session) if supervisor.available() else None)
            log("waiting_ha", f"waiting for Home Assistant OS to come up at {ip} (its first boot takes a few minutes)")
            deadline = time.time() + WAIT_HA
            base = ""
            while not base:
                for b in bases:
                    status, steps = await _onboarding(session, b)
                    if await _set_up(session, b, status, steps):
                        log("failed", f"the Home Assistant at {b} is already set up, so it cannot take your backup")
                        return
                    if status == 200 and isinstance(steps, list):
                        base = b
                        break
                if base:
                    break
                if time.time() > deadline:
                    log("failed", f"Home Assistant did not answer at {ip} within {WAIT_HA // 60} minutes")
                    return
                await asyncio.sleep(POLL)

            size_mb = backup.stat().st_size / 1e6
            log("uploading", f"sending your backup ({size_mb:.0f} MB) to {base}")
            with backup.open("rb") as f:
                data = aiohttp.FormData()
                data.add_field("file", f, filename=BACKUP_FILE, content_type="application/x-tar")
                async with session.post(
                    f"{base}/api/onboarding/backup/upload", params={"agent_id": AGENT}, data=data
                ) as r:
                    if r.status not in (200, 201):
                        log("failed", f"the new Home Assistant refused the backup: {r.status} {(await r.text())[:200]}")
                        return
                    backup_id = (await r.json(content_type=None))["backup_id"]
            info = state.build.get("move_in_backup") or {}
            body = {
                "backup_id": backup_id,
                "agent_id": AGENT,
                "password": state.secrets["move_in_password"],
                "restore_addons": info.get("addons", []),
                "restore_database": True,
                "restore_folders": info.get("folders", []),
            }
            async with session.post(f"{base}/api/onboarding/backup/restore", json=body) as r:
                if r.status != 200:
                    log("failed", f"the restore was refused: {r.status} {(await r.text())[:300]}")
                    return
            log("restoring", "the new Home Assistant is restoring your backup and restarting (several minutes)")
            await asyncio.sleep(SETTLE)
            deadline = time.time() + WAIT_RESTORE
            while time.time() < deadline:
                for b in bases:  # the restored Home Assistant may listen on this one's port
                    status, steps = await _onboarding(session, b)
                    if await _set_up(session, b, status, steps):
                        log("done", f"Your Home Assistant now runs on the cluster at {b}")
                        return
                await asyncio.sleep(POLL)
            log(
                "failed",
                f"the restore did not finish within {WAIT_RESTORE // 60} minutes; open {base} to see where it is",
            )
    except Exception as err:
        _LOGGER.exception("move-in failed")
        log("failed", str(err))
