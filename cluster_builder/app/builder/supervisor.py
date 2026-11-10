"""The Supervisor API from inside the add-on: take a backup of this Home Assistant for the move."""

from __future__ import annotations

import asyncio
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
        body = await r.json(content_type=None)
        if r.status >= 400 or (isinstance(body, dict) and body.get("result") == "error"):
            msg = body.get("message") if isinstance(body, dict) else ""
            raise SupervisorError(f"{method} {path}: {msg or r.status}")
        return body.get("data", {}) if isinstance(body, dict) else {}


async def make_backup(
    session: aiohttp.ClientSession, password: str, dest: Path, progress: Callable[[str, float], None]
) -> dict[str, Any]:
    """A backup of everything here except this add-on (its downloads would bloat it), encrypted with the
    one-time move-in password; downloaded to dest and then deleted from the Supervisor."""
    addons = [a["slug"] for a in (await _api(session, "GET", "/addons")).get("addons", [])]
    addons = [a for a in addons if not a.endswith(OWN_SLUG_SUFFIX)]
    job = await _api(
        session,
        "POST",
        "/backups/new/partial",
        json={
            "name": "Cluster Builder move-in",
            "password": password,
            "compressed": True,
            "homeassistant": True,
            "addons": addons,
            "folders": FOLDERS,
            "background": True,
        },
    )
    job_id = job.get("job_id")
    if not job_id:
        raise SupervisorError("the Supervisor did not start the backup")
    slug = ""
    for _ in range(720):  # up to an hour
        j = await _api(session, "GET", f"/jobs/{job_id}")
        if j.get("errors"):
            raise SupervisorError("backup failed: " + "; ".join(str(e.get("message", e)) for e in j["errors"]))
        if j.get("done"):
            slug = str(j.get("reference") or "")
            break
        progress("backing up this Home Assistant for the move", float(j.get("progress") or 0) / 100)
        await asyncio.sleep(5)
    if not slug:
        raise SupervisorError("the backup took more than an hour")
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
