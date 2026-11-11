"""Two HTTP faces: the Ingress UI (only Home Assistant's ingress proxy may talk to it) and the LAN
port the new machines use (every route carries a secret)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

from . import answers, build, move_in, netboot, proxydhcp, tftp
from .state import State

_LOGGER = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")
INGRESS_PEER = "172.30.32.2"
SUPERVISOR = "http://supervisor"


# ---------------------------------------------------------------- helpers


def _state(request: web.Request) -> State:
    return request.app["state"]


def _node_or_403(request: web.Request) -> str:
    state = _state(request)
    name = request.match_info["node"]
    if name not in state.nodes or state.node_secret(name) != request.match_info["secret"]:
        raise web.HTTPForbidden(text="unknown node or secret")
    return name


async def supervisor_defaults(state: State) -> dict[str, Any]:
    """Time zone and country from Home Assistant, the host's address for builder_url."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    out: dict[str, Any] = {}
    if not token:
        return out
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(f"{SUPERVISOR}/core/api/config") as r:
                if r.status == 200:
                    cfg = await r.json()
                    out["timezone"] = cfg.get("time_zone", "")
                    out["country"] = (cfg.get("country") or "us").lower()
            async with s.get(f"{SUPERVISOR}/network/info") as r:
                if r.status == 200:
                    info = (await r.json()).get("data", {})
                    for iface in info.get("interfaces", []):
                        addrs = iface.get("ipv4", {}).get("address") or []
                        if iface.get("primary") and addrs:
                            ip = addrs[0].split("/")[0]
                            out["host_ip"] = ip
                            gw = iface.get("ipv4", {}).get("gateway")
                            if gw:
                                out["gateway"] = gw
                            ns = iface.get("ipv4", {}).get("nameservers") or []
                            if ns:
                                out["dns"] = ns[0]
                            break
    except Exception as err:
        _LOGGER.debug("supervisor lookup failed: %s", err)
    return out


def apply_defaults(state: State, d: dict[str, Any]) -> None:
    s = state.settings
    changed = False
    if not s["timezone"] and d.get("timezone"):
        s["timezone"] = d["timezone"]
        changed = True
    if d.get("country") and s["country"] == "us" and d["country"] != "us":
        s["country"] = d["country"]
        changed = True
    if not s["builder_url"] and d.get("host_ip"):
        s["builder_url"] = f"http://{d['host_ip']}:8808"
        changed = True
    if not s["gateway"] and d.get("gateway"):
        s["gateway"] = d["gateway"]
        changed = True
    if not s["dns"] and d.get("dns", d.get("gateway")):
        s["dns"] = d.get("dns") or d["gateway"]
        changed = True
    if d.get("host_ip") and all(not n["ip"] for n in s["nodes"]):
        prefix = d["host_ip"].rsplit(".", 1)[0]
        for i, n in enumerate(s["nodes"]):
            n["ip"] = f"{prefix}.{11 + i}"
        changed = True
    if changed:
        state.save()


# ---------------------------------------------------------------- Ingress UI


@web.middleware
async def ingress_only(request: web.Request, handler):  # type: ignore[no-untyped-def]
    peer = request.remote or ""
    if peer not in (INGRESS_PEER, "127.0.0.1", "::1") and not request.app.get("dev"):
        raise web.HTTPForbidden(text="UI is reachable through Home Assistant only")
    return await handler(request)


async def ui_index(request: web.Request) -> web.Response:
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


async def ui_state(request: web.Request) -> web.Response:
    state = _state(request)
    pub = state.public()
    pub["iso_ready"] = build.iso_ready(state)
    pub["netboot_ready"] = netboot.ready(state)
    pub["tftp_error"] = getattr(state, "tftp_error", "")
    pub["dhcp_error"] = getattr(state, "dhcp_error", "")
    pub["free_mb"] = build.disk_free(state.downloads) >> 20
    pub["problems"] = state.validate()
    pub["now"] = time.time()
    return web.json_response(pub)


async def ui_settings(request: web.Request) -> web.Response:
    state = _state(request)
    body = await request.json()
    pw = body.pop("password", None) or None
    state.update_settings(body, pw)
    return web.json_response({"ok": True, "problems": state.validate()})


async def ui_build(request: web.Request) -> web.Response:
    state = _state(request)
    if state.build["status"] == "running":
        return web.json_response({"ok": False, "error": "already building"}, status=409)
    errs = state.validate()
    if errs:
        return web.json_response({"ok": False, "error": " ".join(errs)}, status=400)
    request.app["build_task"] = asyncio.create_task(build.run_build(state))
    return web.json_response({"ok": True})


async def ui_node_action(request: web.Request) -> web.Response:
    state = _state(request)
    name = request.match_info["node"]
    action = request.match_info["action"]
    if name not in state.nodes:
        raise web.HTTPNotFound()
    if action == "reset":
        state.reset_node(name)
    elif action == "allow_reinstall":
        state.nodes[name]["allow_reinstall"] = True
        state.save()
    else:
        raise web.HTTPNotFound()
    return web.json_response({"ok": True})


async def ui_assign(request: web.Request) -> web.Response:
    """A machine that asked to network-boot becomes one of the nodes."""
    state = _state(request)
    body = await request.json()
    why = state.assign_machine(request.match_info["mac"], str(body.get("node", "")))
    if why:
        return web.json_response({"ok": False, "error": why}, status=400)
    return web.json_response({"ok": True})


async def ui_forget_machine(request: web.Request) -> web.Response:
    state = _state(request)
    state.d["machines"].pop(netboot.norm_mac(request.match_info["mac"]), None)
    state.save()
    return web.json_response({"ok": True})


async def ui_reset_all(request: web.Request) -> web.Response:
    _state(request).reset_all()
    return web.json_response({"ok": True})


async def ui_download(request: web.Request) -> web.StreamResponse:
    state = _state(request)
    if not build.iso_ready(state):
        raise web.HTTPNotFound(text="no stick image yet")
    return web.FileResponse(
        state.build["iso"], headers={"Content-Disposition": f'attachment; filename="{build.ISO_NAME}"'}
    )


async def ui_move_in(request: web.Request) -> web.Response:
    """Start (or retry) the move-in, optionally with an address the node could not find."""
    state = _state(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    ip = str(body.get("ip") or state.d["move_in"].get("ip") or "").strip()
    if not ip:
        raise web.HTTPBadRequest(text="the VM's address is not known yet")
    state.d["move_in"]["ip"] = ip
    state.save()
    if not start_move_in(state, ip):
        raise web.HTTPConflict(text="a move-in is running")
    return web.json_response({"ok": True})


async def ui_preview(request: web.Request) -> web.Response:
    """What a node would get: answer file, first-boot script, environment (secrets blanked)."""
    state = _state(request)
    name = request.match_info["node"]
    if name not in state.nodes:
        raise web.HTTPNotFound()
    text = answers.answer_for(state, name).replace(state.d["root_hash"], "<hash>")
    sec = state.node_secret(name)
    env = answers.env_for(state, name)
    env = "\n".join(
        line if not line.startswith("SSH_KEY_B64") else "SSH_KEY_B64=<private key>" for line in env.splitlines()
    )
    return web.Response(
        text=f"# answer.toml\n{text}\n\n# first boot environment\n{env}".replace(sec, "<secret>"),
        content_type="text/plain",
    )


async def _no_store(request: web.Request, response: web.StreamResponse) -> None:
    """Never let the browser cache the page or its script: an add-on update must show the new UI at once."""
    response.headers["Cache-Control"] = "no-store"


def make_ui_app(state: State, dev: bool = False) -> web.Application:
    app = web.Application(middlewares=[ingress_only], client_max_size=1 << 20)
    app["state"] = state
    app.on_response_prepare.append(_no_store)
    app["dev"] = dev
    app.router.add_get("/", ui_index)
    app.router.add_get("/api/state", ui_state)
    app.router.add_post("/api/settings", ui_settings)
    app.router.add_post("/api/build", ui_build)
    app.router.add_post("/api/node/{node}/{action}", ui_node_action)
    app.router.add_post("/api/reset", ui_reset_all)
    app.router.add_post("/api/machine/{mac}/assign", ui_assign)
    app.router.add_post("/api/machine/{mac}/forget", ui_forget_machine)
    app.router.add_post("/api/move_in", ui_move_in)
    app.router.add_get("/api/preview/{node}", ui_preview)
    app.router.add_get("/download/" + build.ISO_NAME, ui_download)
    app.router.add_static("/static", STATIC)
    return app


# ---------------------------------------------------------------- the machines' port


async def lan_answer(request: web.Request) -> web.Response:
    """The installer asks who it is. It sends its NICs and DMI data with the token from the ISO."""
    state = _state(request)
    auth = request.headers.get("Authorization", "")
    if not auth.endswith(state.secrets["answer_token"]):
        _LOGGER.warning("answer request from %s with a wrong token", request.remote)
        raise web.HTTPForbidden(text="wrong token")
    try:
        body = await request.json()
    except Exception:
        body = {}
    macs = [str(i.get("mac", "")).lower() for i in body.get("network_interfaces", []) if i.get("mac")]
    dmi = body.get("dmi", {}) or {}
    system = dmi.get("system", {}) or {}
    info = {
        "product": str(system.get("name") or system.get("product_name") or ""),
        "serial": str(system.get("serial") or system.get("serial_number") or ""),
        "vendor": str(system.get("vendor") or system.get("manufacturer") or ""),
        "ifaces": len(macs),
        "from": request.remote,
    }
    if not build.iso_ready(state):
        raise web.HTTPServiceUnavailable(text="no build yet")
    name, why = state.match_machine(macs, info)
    if not name:
        _LOGGER.warning("refusing installer at %s: %s", request.remote, why)
        for n in state.node_names():
            state.nodes[n]["log"].append(
                [time.time(), "refused", f"a machine ({', '.join(macs[:2])}) asked and was refused: {why}"]
            )
        state.save()
        raise web.HTTPForbidden(text=why)
    m = state.nodes[name]["machine"] or info
    state.log(
        name,
        "installing",
        f"{m.get('vendor', '')} {m.get('product', '')} {m.get('serial', '')}".strip() or ", ".join(macs[:2]),
    )
    _LOGGER.info("installer at %s is %s", request.remote, name)
    return web.Response(text=answers.answer_for(state, name), content_type="application/toml")


async def lan_boot(request: web.Request) -> web.Response:
    """iPXE on a machine asks what to do (no secret: the answer is either the public installer or 'use your disk')."""
    state = _state(request)
    try:
        tries = int(request.query.get("n", "0"))
    except ValueError:
        tries = 0
    script = netboot.boot_script(state, request.match_info["mac"], tries, request.remote or "")
    return web.Response(text=script, content_type="text/plain")


async def lan_firstboot(request: web.Request) -> web.Response:
    name = _node_or_403(request)
    return web.Response(text=answers.firstboot_for(_state(request), name), content_type="text/x-shellscript")


async def lan_env(request: web.Request) -> web.Response:
    name = _node_or_403(request)
    return web.Response(text=answers.env_for(_state(request), name), content_type="text/plain")


async def lan_installed(request: web.Request) -> web.Response:
    name = _node_or_403(request)
    state = _state(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    state.log(name, "installed", f"machine-id {str(body.get('machine-id', ''))[:8]}".strip())
    return web.Response(text="ok")


async def lan_status(request: web.Request) -> web.Response:
    name = _node_or_403(request)
    state = _state(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    phase = str(body.get("phase", "")).strip()[:40] or "unknown"
    msg = str(body.get("msg", ""))[:500]
    state.log(name, phase, msg)
    if phase == "vm_ip" and msg:
        start_move_in(state, msg)
    return web.Response(text="ok")


def start_move_in(state: State, ip: str, resumed: bool = False) -> bool:
    """Run the move-in in the background (one at a time)."""
    task = getattr(state, "move_in_task", None)
    if task is not None and not task.done():
        return False
    state.move_in_task = asyncio.create_task(move_in.run(state, ip, resumed))  # type: ignore[attr-defined]
    return True


def resume_move_in(state: State) -> None:
    """After an add-on restart, pick up a move-in that was under way."""
    m = state.d["move_in"]
    if m.get("phase") in move_in.RESUMABLE and m.get("ip"):
        start_move_in(state, m["ip"], resumed=True)


async def lan_join(request: web.Request) -> web.Response:
    """A node may join when the cluster exists and nobody else is joining right now."""
    name = _node_or_403(request)
    state = _state(request)
    c = state.cluster
    if not c["created"]:
        raise web.HTTPConflict(text="cluster not created yet")
    stuck = c.get("joining") and time.time() - c.get("joining_since", 0) > 1800
    if c["joining"] in (None, name) or stuck:
        if c["joining"] != name:
            c["joining_since"] = time.time()
        c["joining"] = name
        state.save()
        return web.Response(text="go")
    raise web.HTTPConflict(text=f"{c['joining']} is joining")


async def lan_cleanup(request: web.Request) -> web.Response:
    """200 once every node has joined: the asking node may remove the build key for the whole cluster."""
    _node_or_403(request)
    if _state(request).all_joined():
        return web.Response(text="go")
    raise web.HTTPConflict(text="not every node has joined yet")


async def lan_file(request: web.Request) -> web.StreamResponse:
    state = _state(request)
    if request.match_info["token"] != state.secrets["answer_token"]:
        raise web.HTTPForbidden()
    name = request.match_info["name"]
    if "/" in name or name.startswith(".") or name.startswith("move-in"):  # the backup goes to the VM only
        raise web.HTTPNotFound()
    p = build.provided(name) or state.downloads / name
    if not p.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(p)


async def lan_iso(request: web.Request) -> web.StreamResponse:
    state = _state(request)
    if request.match_info["token"] != state.secrets["answer_token"] or not build.iso_ready(state):
        raise web.HTTPNotFound()
    return web.FileResponse(
        state.build["iso"], headers={"Content-Disposition": f'attachment; filename="{build.ISO_NAME}"'}
    )


def make_lan_app(state: State) -> web.Application:
    app = web.Application(client_max_size=1 << 20)
    app["state"] = state
    app.router.add_post("/answer", lan_answer)
    app.router.add_get("/firstboot/{node}/{secret}", lan_firstboot)
    app.router.add_get("/env/{node}/{secret}", lan_env)
    app.router.add_post("/installed/{node}/{secret}", lan_installed)
    app.router.add_post("/status/{node}/{secret}", lan_status)
    app.router.add_get("/join/{node}/{secret}", lan_join)
    app.router.add_get("/boot/{mac}", lan_boot)
    app.router.add_get("/files/{token}/{name}", lan_file)
    app.router.add_get("/cleanup/{node}/{secret}", lan_cleanup)
    app.router.add_get("/iso/{token}/" + build.ISO_NAME, lan_iso)
    return app


async def serve(state: State, ui_port: int = 8099, lan_port: int = 8808, dev: bool = False) -> None:
    apply_defaults(state, await supervisor_defaults(state))
    runners = []
    for app, port in ((make_ui_app(state, dev), ui_port), (make_lan_app(state), lan_port)):
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        runners.append(runner)
    _LOGGER.info("UI on :%s (ingress), machines on :%s", ui_port, lan_port)
    tftp_port = int(os.environ.get("CB_TFTP_PORT", "69"))
    try:
        await tftp.start(lambda name: netboot.tftp_file(state, name), tftp_port)
        state.tftp_error = ""  # type: ignore[attr-defined]
        have = [n for n in netboot.LOADERS if (netboot.IPXE_DIR / netboot.LOADERS[n]).is_file()]
        _LOGGER.info("TFTP (network boot loader) on :%s, serving %s", tftp_port, ", ".join(have) or "no loaders!")
    except OSError as err:
        state.tftp_error = str(err)  # type: ignore[attr-defined]
        _LOGGER.warning(
            "TFTP port %s not available (%s): network boot will not work, the stick still does", tftp_port, err
        )
    dhcp_ports = tuple(int(p) for p in os.environ.get("CB_DHCP_PORTS", "67,4011").split(","))
    try:
        await proxydhcp.start(lambda: _ipv4_host(state.settings["builder_url"]), state.machine_asking, dhcp_ports)
        state.dhcp_error = ""  # type: ignore[attr-defined]
        _LOGGER.info("ProxyDHCP (network boot answers) on UDP %s", "/".join(map(str, dhcp_ports)))
    except OSError as err:
        state.dhcp_error = str(err)  # type: ignore[attr-defined]
        _LOGGER.warning("ProxyDHCP port not available (%s): the router must point network boot at this add-on", err)
    resume_move_in(state)  # an add-on restart (update, rebuild) must not strand a move-in half way
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        for r in runners:
            await r.cleanup()


def _ipv4_host(url: str) -> str | None:
    """The add-on's own address as the machines reach it, if builder_url names an IPv4 address."""
    host = urlparse(url).hostname or ""
    try:
        socket.inet_aton(host)
    except OSError:
        return None
    return host if host.count(".") == 3 else None


def dumps(o: Any) -> str:
    return json.dumps(o)
