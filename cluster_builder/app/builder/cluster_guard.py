"""Set the Cluster Guard integration up in this Home Assistant, with the token the first node made for it.

The first node runs the first boot as root on the cluster, so it can make Cluster Guard its own Proxmox user, role
and API token (the same ones Cluster Guard would make from the root password) and hand them to the add-on. The
add-on then walks Cluster Guard's config flow through the Supervisor's Core API proxy: host, token, which VM is
Home Assistant. Done before the move-in backup is taken, so the restored Home Assistant arrives on the cluster
already watching it, and nobody types the root password into anything."""

from __future__ import annotations

import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

import aiohttp

from . import supervisor

if TYPE_CHECKING:
    from .state import State

_LOGGER = logging.getLogger(__name__)
DOMAIN = "cluster_guard"
FLOW = "/core/api/config/config_entries/flow"
ENTRIES = "/core/api/config/config_entries/entry"
PORT = 8006
PHASES = [
    ("", "Not started"),
    ("waiting", "Waiting for the first node to make Cluster Guard's login"),
    ("setting_up", "Setting Cluster Guard up in this Home Assistant"),
    ("done", "Cluster Guard is set up"),
    ("missing", "Cluster Guard is not installed here"),
    ("failed", "Cluster Guard could not be set up"),
]


def _log(state: State, phase: str, msg: str = "") -> None:
    state.d["cluster_guard"].update({"phase": phase, "msg": msg, "updated": time.time()})
    _LOGGER.info("cluster guard %s: %s", phase, msg)
    state.save()


async def installed(session: aiohttp.ClientSession) -> bool | None:
    """Whether the Cluster Guard integration is installed in this Home Assistant (None: could not tell).
    Core has no list of installable integrations over REST, so start its flow and drop it again."""
    if not supervisor.available():
        return None
    try:
        status, body = await supervisor.core_api(session, "POST", FLOW, json={"handler": DOMAIN})
    except (supervisor.SupervisorError, aiohttp.ClientError):
        return None
    if status == 400 and "handler" in str(body).lower():
        return False
    if status != 200 or not isinstance(body, dict):
        return None
    if body.get("flow_id"):
        with contextlib.suppress(supervisor.SupervisorError, aiohttp.ClientError):
            await supervisor.core_api(session, "DELETE", f"{FLOW}/{body['flow_id']}")
    return True


async def set_up(state: State, session: aiohttp.ClientSession) -> bool:
    """Create (or replace) Cluster Guard's config entry for this cluster. False when it could not be done;
    the move-in goes on either way and step 5 says what is left to do by hand."""
    cg = state.secrets.get("cluster_guard") or {}
    if not cg.get("token_id") or not cg.get("secret"):
        _log(state, "waiting", "the first node has not made Cluster Guard's login yet")
        return False
    host = state.settings["nodes"][0].get("ip") or cg.get("host") or ""
    name = state.settings.get("cluster_name") or ""
    _log(state, "setting_up", f"Cluster Guard in this Home Assistant, watching the cluster through {host}")
    try:
        # an entry for this cluster from an earlier build would make the flow abort as "already configured",
        # and its token is gone with the old cluster anyway
        status, entries = await supervisor.core_api(session, "GET", f"{ENTRIES}?domain={DOMAIN}")
        if status == 200 and isinstance(entries, list):
            for e in entries:
                if isinstance(e, dict) and e.get("title") == f"Cluster {name}" and e.get("entry_id"):
                    await supervisor.core_api(session, "DELETE", f"{ENTRIES}/{e['entry_id']}")
        status, step = await supervisor.core_api(session, "POST", FLOW, json={"handler": DOMAIN})
        if status == 400:
            _log(state, "missing", "Cluster Guard is not installed in this Home Assistant (install it from HACS)")
            return False
        step = _check(status, step, "user")
        step = _check(
            *await supervisor.core_api(
                session,
                "POST",
                f"{FLOW}/{step['flow_id']}",
                json={"host": host, "port": PORT, "method": "token", "verify_ssl": False},
            ),
            "token",
        )
        step = _check(
            *await supervisor.core_api(
                session,
                "POST",
                f"{FLOW}/{step['flow_id']}",
                json={"token_id": cg["token_id"], "token_secret": cg["secret"]},
            ),
            "ha_vm",
        )
        vmid = str(state.settings["vm"].get("vmid") or cg.get("vmid") or "0")
        status, final = await supervisor.core_api(session, "POST", f"{FLOW}/{step['flow_id']}", json={"ha_vmid": vmid})
        if status != 200 or not isinstance(final, dict) or final.get("type") != "create_entry":
            raise RuntimeError(f"the last step answered {status}: {str(final)[:200]}")
    except (supervisor.SupervisorError, aiohttp.ClientError, RuntimeError, KeyError, TypeError) as err:
        _log(state, "failed", f"{err}; add Cluster Guard by hand with a node's address and the root password")
        return False
    _log(state, "done", f"Cluster Guard watches the cluster through {host} (its own login, no root password kept)")
    return True


def _check(status: int, body: Any, expect: str) -> dict[str, Any]:
    """The flow's answer must be the next form; anything else is said in full so the page can show it."""
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"Core answered {status}: {str(body)[:200]}")
    if body.get("type") == "abort":
        raise RuntimeError(f"Cluster Guard's set-up stopped: {body.get('reason')}")
    if body.get("errors"):
        raise RuntimeError(f"Cluster Guard refused the {body.get('step_id')} step: {body['errors']}")
    if body.get("step_id") != expect:
        raise RuntimeError(f"Cluster Guard asked for {body.get('step_id')} instead of {expect}")
    return body
