"""Move this Home Assistant into the new VM: restore the move-in backup through the Welcome screen's
own (unauthenticated, onboarding-only) API, then tell the person to shut the old machine down."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import aiohttp

from . import cluster_guard, supervisor
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


def _waiting_msg(ip: str, answered: int, waited: float) -> str:
    """What the page says while the new Home Assistant is not ready: what answers at the VM's address tells
    where its first boot is, so a long wait is explained rather than "a few minutes" for an hour."""
    if answered == 401:  # HA OS's landing page, shown while Home Assistant itself is being downloaded
        what = (
            f"Home Assistant OS is up at {ip} and is downloading Home Assistant itself before its first start "
            "(about 1 GB: a few minutes on a fast line, much longer on a slow one)"
        )
    elif answered:  # Core is starting: it answers, but its onboarding API is not registered yet
        what = f"Home Assistant is starting at {ip} (its first start takes a few minutes)"
    else:
        what = f"waiting for Home Assistant OS to come up at {ip} (its first boot takes a few minutes)"
    minutes = int(waited // 60)
    return f"{what}; waited {minutes} min so far" if minutes >= 2 else what


async def _wait_restored(session: aiohttp.ClientSession, bases: list[str], deadline: float) -> str:
    """After a restore was sent: "done" once a set-up Home Assistant answers, "welcome" if the onboarding screen
    is back instead (the restore did not take), "timeout" otherwise."""
    quiet = 0
    while time.time() < deadline:
        for b in bases:  # the restored Home Assistant may listen on this one's port
            status, steps = await _onboarding(session, b)
            if await _set_up(session, b, status, steps):
                return "done"
            if status == 200 and isinstance(steps, list):
                quiet += 1
                if quiet >= 6:  # the Welcome screen, steadily, for a while: nothing is restoring
                    return "welcome"
                break
        else:
            quiet = 0
        await asyncio.sleep(POLL)
    return "timeout"


async def run(state: State, ip: str, resumed: bool = False) -> None:
    """resumed: the add-on restarted while a move was under way. The backup may already be restoring, so a
    Home Assistant that comes up set up is then the finished move, not someone else's."""
    log = state.move_in_log
    backup = state.downloads / BACKUP_FILE
    try:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=900)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if supervisor.available() and not resumed:
                # Cluster Guard first, so the backup carries it; then a fresh backup of this Home Assistant
                if state.d["cluster_guard"].get("phase") != "done":
                    await cluster_guard.set_up(state, session)
                log("backup", "taking a fresh backup of this Home Assistant for the move (a few minutes)")

                def _bp(step: str, frac: float) -> None:
                    log("backup", step)

                state.build["move_in_backup"] = await supervisor.make_backup(
                    session, state.secrets["move_in_password"], backup, _bp
                )
                state.save()
            if not backup.exists():
                log("failed", "there is no move-in backup here: tick the move-in option and Try the move again")
                return
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
            started = time.time()
            while not base:
                answered = 0
                for b in bases:
                    status, steps = await _onboarding(session, b)
                    answered = answered or status
                    if await _set_up(session, b, status, steps):
                        if resumed or state.d["move_in"].get("uploaded"):
                            state.d["move_in"]["url"] = b
                            log("done", f"Your Home Assistant now runs on the cluster at {b}")
                            return
                        log(
                            "failed",
                            f"the Home Assistant at {b} is already set up, so it cannot take your backup. If it was "
                            "restoring your backup the last time this page looked, that is your move finished: open "
                            f"{b} and check. Otherwise it was set up by hand.",
                        )
                        return
                    if status == 200 and isinstance(steps, list):
                        base = b
                        state.d["move_in"]["url"] = b  # the page sends you to the same address later
                        state.save()
                        break
                if base:
                    break
                if time.time() > deadline:
                    log(
                        "failed",
                        f"Home Assistant did not answer at {ip} within {(deadline and WAIT_RESTORE if resumed else WAIT_HA) // 60} minutes",
                    )
                    return
                if not resumed:
                    msg = _waiting_msg(ip, answered, time.time() - started)
                    if msg != state.d["move_in"].get("msg"):
                        log("waiting_ha", msg)
                await asyncio.sleep(POLL)

            size_mb = backup.stat().st_size / 1e6
            log("uploading", f"sending your backup ({size_mb:.0f} MB) to {base}")
            # right after its first start Home Assistant is not ready for this for a while: the landing page still
            # answers 401, Core answers 404 until its onboarding API is registered, then 500 "Backup manager busy"
            # for some minutes. Keep trying; a 4xx other than those is a real refusal.
            deadline = time.time() + WAIT_UPLOAD
            while True:
                status, text = 0, ""
                state.d["move_in"]["uploaded"] = True  # from here on, a set-up Home Assistant means the move worked
                state.save()
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
                    # it hung up on us: either still starting, or it took the backup and is restarting into the
                    # restore. Wait for it; upload again only if the Welcome screen comes back
                    text = f"{type(err).__name__}: {err}"
                    log(
                        "restoring",
                        f"the new Home Assistant hung up during the upload ({text.strip()}); waiting for it",
                    )
                    what = await _wait_restored(session, bases, time.time() + WAIT_RESTORE)
                    if what == "done":
                        log("done", f"Your Home Assistant now runs on the cluster at {base}")
                        return
                    if what == "timeout":
                        log(
                            "failed",
                            f"Home Assistant did not come back within {WAIT_RESTORE // 60} minutes; open {base}",
                        )
                        return
                    log("uploading", "the Welcome screen is back: sending the backup again")
                    continue
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
            try:
                async with session.post(f"{base}/api/onboarding/backup/restore", json=body) as r:
                    if r.status != 200:
                        log("failed", f"the restore was refused: {r.status} {(await r.text())[:300]}")
                        return
            except (TimeoutError, aiohttp.ClientError):
                pass  # it restarts into the restore before answering: that is the restore starting
            log("restoring", "it restarts while it restores, which takes several minutes")
            await asyncio.sleep(SETTLE)
            what = await _wait_restored(session, bases, time.time() + WAIT_RESTORE)
            if what == "done":
                log("done", f"Your Home Assistant now runs on the cluster at {base}")
            elif what == "welcome":
                log("failed", f"the restore did not take: {base} shows its Welcome screen again; Try the move again")
            else:
                log(
                    "failed",
                    f"the restore did not finish within {WAIT_RESTORE // 60} minutes; open {base} to see where it is",
                )
    except Exception as err:
        _LOGGER.exception("move-in failed")
        log("failed", str(err))
