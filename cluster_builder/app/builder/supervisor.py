"""The Supervisor API from inside the add-on: take a backup of this Home Assistant for the move."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)
SUP = os.environ.get("CB_SUPERVISOR_URL", "http://supervisor")
BACKUP_FILE = "move-in.tar"
BACKUP_NAME = "Cluster Builder move-in"
FOLDERS = ["ssl", "share", "media", "addons/local"]
OWN_SLUG_SUFFIX = "cluster_builder"


class SupervisorError(Exception):
    pass


def available() -> bool:
    return bool(os.environ.get("SUPERVISOR_TOKEN"))


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}"}


async def _api(session: aiohttp.ClientSession, method: str, path: str, **kw: Any) -> dict[str, Any]:
    async with session.request(method, SUP + path, headers=_headers(), **kw) as r:
        text = await r.text()
        try:
            body = json.loads(text)
        except ValueError:
            body = {}
        if r.status >= 400 or (isinstance(body, dict) and body.get("result") == "error"):
            msg = body.get("message") if isinstance(body, dict) else ""
            raise SupervisorError(f"Supervisor {method} {path}: {msg or text[:120] or r.status}")
        return body.get("data", {}) if isinstance(body, dict) else {}


async def _installed_addons(session: aiohttp.ClientSession) -> list[str]:
    """Slugs of the installed add-ons; /addons needs a bigger role than backup, /supervisor/info does not."""
    for path, key in (("/addons", "addons"), ("/supervisor/info", "addons")):
        try:
            return [str(a["slug"]) for a in (await _api(session, "GET", path)).get(key, []) if a.get("slug")]
        except SupervisorError as err:
            _LOGGER.info("%s; trying the next way to list add-ons", err)
    raise SupervisorError("could not list the installed add-ons")


async def make_backup(
    session: aiohttp.ClientSession, password: str, dest: Path, progress: Callable[[str, float], None]
) -> dict[str, Any]:
    """A backup of everything here except this add-on (its downloads would bloat it), encrypted with the
    one-time move-in password; downloaded to dest and then deleted from the Supervisor."""
    addons = [a for a in await _installed_addons(session) if not a.endswith(OWN_SLUG_SUFFIX)]
    # a stray one from an interrupted build is only clutter
    for b in (await _api(session, "GET", "/backups")).get("backups", []):
        if b.get("name") == BACKUP_NAME and b.get("slug"):
            await _api(session, "DELETE", f"/backups/{b['slug']}")
    progress("backing up this Home Assistant for the move (a few minutes)", 0.0)
    # in the foreground: the backup role may create backups but not read the job that would track one
    made = await _api(
        session,
        "POST",
        "/backups/new/partial",
        json={
            "name": BACKUP_NAME,
            "password": password,
            "compressed": True,
            "homeassistant": True,
            "addons": addons,
            "folders": FOLDERS,
        },
        timeout=aiohttp.ClientTimeout(total=3 * 3600),
    )
    slug = str(made.get("slug") or "")
    if not slug:
        raise SupervisorError("the Supervisor did not make the backup")
    progress("downloading the backup", 0.0)
    tmp = dest.with_suffix(".part")
    async with session.get(f"{SUP}/backups/{slug}/download", headers=_headers()) as r:
        if r.status != 200:
            raise SupervisorError(f"could not download backup {slug}: {r.status}")
        with tmp.open("wb") as f:
            async for chunk in r.content.iter_chunked(1 << 20):
                f.write(chunk)
    tmp.replace(dest)
    try:
        await _api(session, "DELETE", f"/backups/{slug}")
    except SupervisorError as err:  # it is only clutter at this point
        _LOGGER.warning("could not delete backup %s: %s", slug, err)
    return {"addons": addons, "folders": FOLDERS, "bytes": dest.stat().st_size, "made_at": time.time()}
