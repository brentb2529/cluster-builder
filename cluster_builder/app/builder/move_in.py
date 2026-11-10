"""Move this Home Assistant into the new VM: restore the move-in backup through the Welcome screen's
own (unauthenticated, onboarding-only) API, then tell the person to shut the old machine down."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import aiohttp

from .supervisor import BACKUP_FILE

if TYPE_CHECKING:
    from .state import State

_LOGGER = logging.getLogger(__name__)
AGENT = "hassio.local"
POLL = 15.0
SETTLE = 30.0
WAIT_HA = 45 * 60
WAIT_RESTORE = 30 * 60
RESUMABLE = ("waiting_ha", "uploading", "restoring")


def base_url(ip: str) -> str:
    return f"http://{ip}" if ":" in ip else f"http://{ip}:8123"


def _user_done(steps: object) -> bool:
    return isinstance(steps, list) and any(
        isinstance(s, dict) and s.get("step") == "user" and s.get("done") for s in steps
    )


async def _onboarding(session: aiohttp.ClientSession, base: str) -> tuple[int, object]:
    """(status, body) of the Welcome screen's status call; (0, None) while it does not answer."""
    try:
        async with session.get(f"{base}/api/onboarding") as r:
            body = await r.json(content_type=None) if r.status == 200 else None
            return r.status, body
    except (TimeoutError, aiohttp.ClientError):
        return 0, None


async def run(state: State, ip: str) -> None:
    log = state.move_in_log
    base = base_url(ip)
    backup = state.downloads / BACKUP_FILE
    if not backup.exists():
        log("failed", "there is no move-in backup here: Build again with the move-in option ticked")
        return
    try:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=900)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            log(
                "waiting_ha", f"waiting for Home Assistant OS to come up at {base} (its first boot takes a few minutes)"
            )
            deadline = time.time() + WAIT_HA
            while True:
                status, steps = await _onboarding(session, base)
                if status == 200 and not _user_done(steps):
                    break
                if status in (200, 401):
                    log("failed", f"the Home Assistant at {base} is already set up, so it cannot take your backup")
                    return
                if time.time() > deadline:
                    log("failed", f"Home Assistant did not answer at {base} within 45 minutes")
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
                status, steps = await _onboarding(session, base)
                if status == 401 or (status == 200 and _user_done(steps)):
                    log("done", f"Your Home Assistant now runs on the cluster at {base}")
                    return
                await asyncio.sleep(POLL)
            log("failed", f"the restore did not finish within 30 minutes; open {base} to see where it is")
    except Exception as err:
        _LOGGER.exception("move-in failed")
        log("failed", str(err))
