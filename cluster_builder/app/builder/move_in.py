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
WAIT_UPLOAD = 15 * 60
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
    """A running Home Assistant serves its frontend's manifest; the first-boot landing page answers 404 there
    and 401 to every /api/ path, so it must not pass for a Home Assistant that is up."""
    try:
        async with session.get(f"{base}/manifest.json", allow_redirects=False) as r:
            return r.status == 200 and "json" in r.headers.get("Content-Type", "")
    except (TimeoutError, aiohttp.ClientError):
        return False


async def _set_up(session: aiohttp.ClientSession, base: str, status: int, steps: object) -> bool:
    """True when the Home Assistant at base is past onboarding: its steps say so, or (Home Assistant 2026)
    the onboarding API is gone (404) while the frontend is there."""
    if status == 200:
        return _user_done(steps)
    return status == 404 and await _core_up(session, base)


async def run(state: State, ip: str, resumed: bool = False) -> None:
    """resumed: the add-on restarted while a move was under way. The backup may already be restoring, so a
    Home Assistant that comes up set up is then the finished move, not someone else's."""
    log = state.move_in_log
    backup = state.downloads / BACKUP_FILE
    if not backup.exists():
        log("failed", "there is no move-in backup here: Build again with the move-in option ticked")
        return
    try:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=900)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            bases = candidates(ip, await supervisor.core_port(session) if supervisor.available() else None)
            if resumed:
                log("restoring", f"picking the move up again after a restart: checking the Home Assistant at {ip}")
                deadline = time.time() + WAIT_RESTORE
            else:
                log(
                    "waiting_ha",
                    f"waiting for Home Assistant OS to come up at {ip} (its first boot takes a few minutes)",
                )
                deadline = time.time() + WAIT_HA
            base = ""
            while not base:
                for b in bases:
                    status, steps = await _onboarding(session, b)
                    if await _set_up(session, b, status, steps):
                        if resumed:
                            log("done", f"Your Home Assistant now runs on the cluster at {b}")
                            return
                        log("failed", f"the Home Assistant at {b} is already set up, so it cannot take your backup")
                        return
                    if status == 200 and isinstance(steps, list):
                        base = b
                        break
                if base:
                    break
                if time.time() > deadline:
                    log(
                        "failed",
                        f"Home Assistant did not answer at {ip} within {(deadline and WAIT_RESTORE if resumed else WAIT_HA) // 60} minutes",
                    )
                    return
                await asyncio.sleep(POLL)

            size_mb = backup.stat().st_size / 1e6
            log("uploading", f"sending your backup ({size_mb:.0f} MB) to {base}")
            # right after its first start Home Assistant is not ready for this for a while: the landing page still
            # answers 401, Core answers 404 until its onboarding API is registered, then 500 "Backup manager busy"
            # for some minutes. Keep trying; a 4xx other than those is a real refusal.
            deadline = time.time() + WAIT_UPLOAD
            while True:
                status, text = 0, ""
                try:
                    with backup.open("rb") as f:
                        data = aiohttp.FormData()
                        data.add_field("file", f, filename=BACKUP_FILE, content_type="application/x-tar")
                        async with session.post(
                            f"{base}/api/onboarding/backup/upload", params={"agent_id": AGENT}, data=data
                        ) as r:
                            status = r.status
                            if status in (200, 201):
                                backup_id = (await r.json(content_type=None))["backup_id"]
                                break
                            text = (await r.text())[:200]
                except (TimeoutError, aiohttp.ClientError) as err:
                    text = f"{type(err).__name__}: {err}"  # it hung up on us mid-upload: still starting, try again
                if (status not in (0, 401, 404) and status < 500) or time.time() > deadline:
                    log("failed", f"the new Home Assistant refused the backup: {status} {text}")
                    return
                log("uploading", f"the new Home Assistant is still getting ready ({text.strip()}); trying again")
                await asyncio.sleep(POLL)
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
