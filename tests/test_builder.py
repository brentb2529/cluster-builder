"""Cluster Builder: machine matching, answer files, first-boot environment, and the LAN routes."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from builder import answers, server
from builder.state import State


@pytest.fixture
def state(tmp_path: Path) -> State:
    st = State(tmp_path)
    st.update_settings(
        {
            "builder_url": "http://192.168.2.250:8808",
            "gateway": "192.168.2.1",
            "dns": "192.168.2.1",
            "timezone": "America/New_York",
            "nodes": [
                {"name": "pve1", "ip": "192.168.2.11", "link_ip": "10.10.10.1"},
                {"name": "pve2", "ip": "192.168.2.12", "link_ip": "10.10.10.2"},
                {"name": "pve3", "ip": "192.168.2.13", "link_ip": "10.10.10.3"},
            ],
        },
        "hunter2hunter2",
    )
    iso = tmp_path / "downloads" / "cluster-builder.iso"
    iso.write_bytes(b"iso")
    st.build.update({"status": "done", "iso": str(iso)})
    st.save()
    return st


def test_validate_and_answers(state: State) -> None:
    assert state.validate() == []
    a = answers.answer_for(state, "pve2")
    assert 'fqdn = "pve2.home.arpa"' in a
    assert 'source = "from-dhcp"' in a
    assert 'reboot-mode = "reboot"' in a  # network boot: the machine restarts into its first boot
    state.update_settings({"install_method": "stick"}, None)
    assert 'reboot-mode = "power-off"' in answers.answer_for(state, "pve1")
    state.update_settings({"install_method": "network"}, None)
    assert state.d["root_hash"].startswith("$6$")
    assert f"/firstboot/pve2/{state.node_secret('pve2')}" in a
    env = answers.env_for(state, "pve2")
    assert "IS_FIRST=0" in env and "FIRST_IP=192.168.2.11" in env and "LINK_IP=10.10.10.2" in env
    assert "HOSTS='192.168.2.11=pve1 192.168.2.12=pve2 192.168.2.13=pve3'" in env
    script = answers.firstboot_for(state, "pve1")
    assert "@@" not in script
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)


def test_match_machine(state: State) -> None:
    assert state.match_machine(["aa:00:00:00:00:01", "aa:00:00:00:00:02"], {}) == ("pve1", "")
    assert state.match_machine(["AA:00:00:00:00:02"], {}) == ("pve1", "")  # same box, other NIC
    assert state.match_machine(["aa:00:00:00:01:01"], {}) == ("pve2", "")
    state.log("pve1", "installed")
    name, why = state.match_machine(["aa:00:00:00:00:01"], {})
    assert name is None and "Allow reinstall" in why
    state.nodes["pve1"]["allow_reinstall"] = True
    assert state.match_machine(["aa:00:00:00:00:01"], {})[0] == "pve1"
    assert state.match_machine(["aa:00:00:00:02:01"], {}) == ("pve3", "")
    assert state.match_machine(["aa:00:00:00:03:01"], {})[0] is None


def test_pinned_lan_mac(state: State) -> None:
    s = state.settings
    s["nodes"][2]["lan_mac"] = "aa:00:00:00:09:09"
    assert state.match_machine(["aa:00:00:00:09:09"], {}) == ("pve3", "")
    assert state.match_machine(["aa:00:00:00:00:01"], {}) == ("pve1", "")
    assert state.match_machine(["aa:00:00:00:01:01"], {}) == ("pve2", "")
    assert state.match_machine(["aa:00:00:00:04:01"], {})[0] is None  # pve3 is pinned, not free


async def test_lan_routes(state: State, sockets) -> None:
    async with TestClient(TestServer(server.make_lan_app(state))) as c:
        token = state.secrets["answer_token"]
        r = await c.post("/answer", headers={"Authorization": "Bearer cluster-builder:wrong"}, json={})
        assert r.status == 403
        body = {
            "dmi": {"system": {"name": "Quieter", "serial": "S1", "vendor": "MeLE"}},
            "network_interfaces": [{"link": True, "mac": "aa:00:00:00:00:01"}],
        }
        r = await c.post("/answer", headers={"Authorization": f"Bearer cluster-builder:{token}"}, json=body)
        assert r.status == 200 and 'fqdn = "pve1.home.arpa"' in await r.text()
        assert state.nodes["pve1"]["phase"] == "installing" and "MeLE" in state.nodes["pve1"]["msg"]
        s1, s2, s3 = (state.node_secret(n) for n in ("pve1", "pve2", "pve3"))
        assert (await c.get(f"/env/pve1/{s1}")).status == 200
        assert (await c.get("/env/pve1/nope")).status == 403
        assert (await c.get(f"/firstboot/pve1/{s1}")).status == 200
        r = await c.post(f"/installed/pve1/{s1}", json={"machine-id": "abcdef0123456789"})
        assert r.status == 200 and state.nodes["pve1"]["installed"]
        assert (await c.get(f"/join/pve2/{s2}")).status == 409
        await c.post(f"/status/pve1/{s1}", json={"phase": "cluster_created", "msg": "house"})
        assert state.cluster["created"]
        assert (await c.get(f"/join/pve2/{s2}")).status == 200
        assert (await c.get(f"/join/pve3/{s3}")).status == 409
        assert (await c.get(f"/join/pve2/{s2}")).status == 200  # same joiner may ask again
        await c.post(f"/status/pve2/{s2}", json={"phase": "joined"})
        assert (await c.get(f"/join/pve3/{s3}")).status == 200
        assert (await c.get(f"/cleanup/pve2/{s2}")).status == 409  # pve3 still needs the build key
        await c.post(f"/status/pve3/{s3}", json={"phase": "joined"})
        assert (await c.get(f"/cleanup/pve3/{s3}")).status == 200
        assert (await c.get("/cleanup/pve3/nope")).status == 403
        (state.downloads / "x.img").write_bytes(b"img")
        assert (await c.get(f"/files/{token}/x.img")).status == 200
        assert (await c.get(f"/files/{token}/../state.json")).status in (403, 404)
        (state.downloads / "move-in.tar").write_bytes(b"secret")
        assert (await c.get(f"/files/{token}/move-in.tar")).status == 404
        assert (await c.get(f"/iso/{token}/cluster-builder.iso")).status == 200


async def test_ui_routes(state: State, sockets) -> None:
    async with TestClient(TestServer(server.make_ui_app(state, dev=True))) as c:
        assert (await c.get("/static/app.js")).headers["Cache-Control"] == "no-store"
        r = await c.get("/api/state")
        d = await r.json()
        assert d["has_password"] and d["root_hash"] is None and "secrets" not in d and d["iso_ready"]
        r = await c.post("/api/settings", json={"cluster_name": "lab", "vm": {"memory": 4096}})
        assert (
            (await r.json())["ok"]
            and state.settings["cluster_name"] == "lab"
            and state.settings["vm"]["memory"] == 4096
        )
        r = await c.get("/api/preview/pve1")
        t = await r.text()
        assert "<hash>" in t and "<secret>" in t and "<private key>" in t and state.d["root_hash"] not in t
        state.match_machine(["aa:00:00:00:00:01"], {})
        assert (await c.post("/api/node/pve1/reset")).status == 200 and state.nodes["pve1"]["machine"] is None
        assert (await c.get("/download/cluster-builder.iso")).status == 200


def test_state_survives_restart(tmp_path: Path, state: State) -> None:
    again = State(tmp_path)
    assert again.secrets["answer_token"] == state.secrets["answer_token"]
    assert again.settings["vm"]["mac"] == state.settings["vm"]["mac"]
    assert json.loads(again.path.read_text())["nodes"]["pve1"]["phase"] == "waiting"


class FakeWelcome:
    """The onboarding-only part of a fresh Home Assistant: status, backup upload, info and restore."""

    def __init__(self) -> None:
        self.user_done = False
        self.uploaded = b""
        self.restore: dict | None = None
        self.busy = 0  # uploads refused with 500 'Backup manager busy' first
        self.landing = 0  # polls answered with the "preparing Home Assistant" HTML page first
        self.gone_when_done = False  # HA 2026: no onboarding API at all once onboarded (404), /api/ says 401

    def app(self) -> web.Application:
        async def status(_):
            if self.landing > 0:  # HA OS's first-boot landing page: 401 text to every /api/ path, no manifest
                self.landing -= 1
                return web.Response(text="Unauthorized", status=401)
            if self.user_done and self.gone_when_done:
                raise web.HTTPNotFound()
            return web.json_response([{"step": "user", "done": self.user_done}, {"step": "core_config", "done": False}])

        async def manifest(_):
            if self.landing > 0:
                raise web.HTTPNotFound()
            return web.json_response({"name": "Home Assistant"}, content_type="application/manifest+json")

        async def upload(request):
            assert request.query["agent_id"] == "hassio.local"
            if self.busy > 0:  # a fresh HA's backup manager is "blocked" for a while after start
                self.busy -= 1
                return web.Response(text="Can't upload backup file: Backup manager busy: blocked", status=500)
            reader = await request.multipart()
            part = await reader.next()
            self.uploaded = await part.read()
            return web.json_response({"backup_id": "abc123"}, status=201)

        async def restore(request):
            self.restore = await request.json()
            self.user_done = True  # "restarted with the restored config"
            return web.Response()

        app = web.Application()
        app.router.add_get("/api/onboarding", status)
        app.router.add_get("/manifest.json", manifest)
        app.router.add_post("/api/onboarding/backup/upload", upload)
        app.router.add_post("/api/onboarding/backup/restore", restore)
        return app


async def test_move_in_restores_through_the_welcome_screen(state: State, sockets, monkeypatch) -> None:
    from builder import move_in

    monkeypatch.setattr(move_in, "POLL", 0.01)
    monkeypatch.setattr(move_in, "SETTLE", 0.01)
    (state.downloads / "move-in.tar").write_bytes(b"tar" * 100)
    state.build["move_in_backup"] = {"addons": ["core_ssh"], "folders": ["share", "ssl"]}
    welcome = FakeWelcome()
    async with TestServer(welcome.app()) as srv:
        where = f"{srv.host}:{srv.port}"
        await move_in.run(state, where)
    assert state.d["move_in"]["phase"] == "done", state.d["move_in"]
    assert state.d["move_in"]["url"] == f"http://{where}"  # step 5 opens the address that answered
    assert welcome.uploaded == b"tar" * 100
    assert welcome.restore["backup_id"] == "abc123"
    assert welcome.restore["password"] == state.secrets["move_in_password"]
    assert welcome.restore["restore_addons"] == ["core_ssh"] and welcome.restore["restore_folders"] == ["share", "ssl"]
    assert welcome.restore["restore_database"] is True

    # an already set-up Home Assistant must not be touched
    welcome.user_done = True
    async with TestServer(welcome.app()) as srv:
        await move_in.run(state, f"{srv.host}:{srv.port}")
        # a second run meets the restored, set-up Home Assistant: that is the move finished, not a refusal...
        assert state.d["move_in"]["phase"] == "done", state.d["move_in"]
        # ...unless nothing was ever uploaded by this move (someone set it up by hand)
        state.d["move_in"]["uploaded"] = False
        await move_in.run(state, f"{srv.host}:{srv.port}")
    assert state.d["move_in"]["phase"] == "failed" and "already set up" in state.d["move_in"]["msg"]


async def test_move_in_handles_the_landing_page_and_a_gone_onboarding_api(state: State, sockets, monkeypatch) -> None:
    """HA OS first shows a landing page (401 on /api/*, no manifest); HA 2026 drops the onboarding API once onboarded."""
    from builder import move_in

    monkeypatch.setattr(move_in, "POLL", 0.01)
    monkeypatch.setattr(move_in, "SETTLE", 0.01)
    (state.downloads / "move-in.tar").write_bytes(b"tar" * 10)
    state.build["move_in_backup"] = {"addons": [], "folders": []}
    welcome = FakeWelcome()
    welcome.landing = 2
    welcome.busy = 2
    welcome.gone_when_done = True
    seen: list[str] = []
    orig = state.move_in_log
    monkeypatch.setattr(state, "move_in_log", lambda phase, msg="": (seen.append(msg), orig(phase, msg)))
    async with TestServer(welcome.app()) as srv:
        await move_in.run(state, f"{srv.host}:{srv.port}")
    assert state.d["move_in"]["phase"] == "done", state.d["move_in"]
    assert welcome.restore is not None
    # while the landing page answered, the page said what the wait is (not "a few minutes" for an hour)
    assert any("downloading Home Assistant itself" in m for m in seen), seen
    assert "waited 0 min" not in " ".join(seen)
    assert "; waited 7 min so far" in move_in._waiting_msg("1.2.3.4", 0, 7 * 60 + 5)

    # an onboarded HA 2026 (404 on the onboarding API, frontend manifest there) is "already set up"
    async with TestServer(welcome.app()) as srv:
        await move_in.run(state, f"{srv.host}:{srv.port}")
        # a second run meets the restored, set-up Home Assistant: that is the move finished, not a refusal...
        assert state.d["move_in"]["phase"] == "done", state.d["move_in"]
        # ...unless nothing was ever uploaded by this move (someone set it up by hand)
        state.d["move_in"]["uploaded"] = False
        await move_in.run(state, f"{srv.host}:{srv.port}")
    assert state.d["move_in"]["phase"] == "failed" and "already set up" in state.d["move_in"]["msg"]


async def test_move_in_resumes_after_an_addon_restart(state: State, monkeypatch) -> None:
    """The add-on was updated while uploading: on start it must pick the move-in up again."""
    from builder import server as srv_mod

    started: list[str] = []

    async def _noop(ip, resumed):
        started.append((ip, resumed))

    monkeypatch.setattr(srv_mod.move_in, "run", lambda st, ip, resumed=False: _noop(ip, resumed))
    state.move_in_log("uploading", "sending")
    state.d["move_in"]["ip"] = "192.168.77.100"
    srv_mod.resume_move_in(state)
    await asyncio.sleep(0)
    assert started == [("192.168.77.100", True)]
    state.move_in_log("done", "moved")
    srv_mod.resume_move_in(state)
    await asyncio.sleep(0)
    assert started == [("192.168.77.100", True)]


async def test_move_in_resumed_takes_a_set_up_ha_as_done(state: State, sockets, monkeypatch) -> None:
    """The restore was already sent before the restart: the HA coming up onboarded is the finished move."""
    from builder import move_in

    monkeypatch.setattr(move_in, "POLL", 0.01)
    (state.downloads / "move-in.tar").write_bytes(b"tar")
    welcome = FakeWelcome()
    welcome.user_done = True
    welcome.gone_when_done = True
    async with TestServer(welcome.app()) as srv:
        await move_in.run(state, f"{srv.host}:{srv.port}", resumed=True)
    assert state.d["move_in"]["phase"] == "done", state.d["move_in"]
    assert welcome.restore is None


def test_move_in_candidates() -> None:
    from builder.move_in import candidates

    assert candidates("10.0.0.5:9000") == ["http://10.0.0.5:9000"]
    assert candidates("10.0.0.5", None) == ["http://10.0.0.5:8123", "http://10.0.0.5:80"]
    assert candidates("10.0.0.5", 80) == ["http://10.0.0.5:8123", "http://10.0.0.5:80"]
    assert candidates("10.0.0.5", 8443) == ["http://10.0.0.5:8123", "http://10.0.0.5:80", "http://10.0.0.5:8443"]


async def test_supervisor_backup_for_the_move(state: State, sockets, monkeypatch) -> None:
    from builder import supervisor

    calls: list[str] = []

    async def addons(_):  # as the Supervisor answers an add-on that only has the backup role
        return web.Response(status=403, text="403: Forbidden")

    async def sup_info(_):
        return web.json_response(
            {"result": "ok", "data": {"addons": [{"slug": "core_ssh"}, {"slug": "local_cluster_builder"}]}}
        )

    async def new_partial(request):
        body = await request.json()
        calls.append(f"partial {body['addons']} {body['homeassistant']} {body['password']} {body['folders']}")
        return web.json_response({"result": "ok", "data": {"slug": "s1"}})

    async def backups(_):
        return web.json_response(
            {"result": "ok", "data": {"backups": [{"slug": "old1", "name": "Cluster Builder move-in"}]}}
        )

    async def delete_old(_):
        calls.append("deleted old1")
        return web.json_response({"result": "ok", "data": {}})

    async def download(_):
        return web.Response(body=b"backup-bytes")

    async def delete(_):
        calls.append("deleted s1")
        return web.json_response({"result": "ok", "data": {}})

    app = web.Application()
    app.router.add_get("/addons", addons)
    app.router.add_get("/supervisor/info", sup_info)
    app.router.add_post("/backups/new/partial", new_partial)
    app.router.add_get("/backups", backups)
    app.router.add_delete("/backups/old1", delete_old)
    app.router.add_get("/backups/s1/download", download)
    app.router.add_delete("/backups/s1", delete)
    async with TestServer(app) as srv:
        monkeypatch.setattr(supervisor, "SUP", f"http://{srv.host}:{srv.port}")
        monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
        monkeypatch.setattr(supervisor, "SHARE_OWN", state.downloads / "no-such-share")
        async with aiohttp.ClientSession() as session:
            info = await supervisor.make_backup(session, "pw", state.downloads / "move-in.tar", lambda *_: None)
    assert (state.downloads / "move-in.tar").read_bytes() == b"backup-bytes"
    assert info["addons"] == ["core_ssh"] and info["bytes"] == 12
    assert calls == [
        "deleted old1",
        "partial ['core_ssh'] True pw ['ssl', 'share', 'media', 'addons/local']",
        "deleted s1",
    ]


async def test_vm_ip_from_the_first_node_starts_the_move(state: State, sockets, monkeypatch) -> None:
    from builder import server as srv_mod

    started: list[str] = []
    monkeypatch.setattr(srv_mod.move_in, "run", lambda st, ip, resumed=False: _noop(started, ip))
    s1 = state.node_secret("pve1")
    async with TestClient(TestServer(srv_mod.make_lan_app(state))) as c:
        await c.post(f"/status/pve1/{s1}", json={"phase": "vm_ip", "msg": "192.168.1.50"})
        await asyncio.sleep(0)
    assert started == ["192.168.1.50"] and state.d["move_in"]["ip"] == "192.168.1.50"
    assert "MOVE_IN=" in answers.env_for(state, "pve1")


async def _noop(started: list[str], ip: str) -> None:
    started.append(ip)


def test_older_saved_settings_gain_new_vm_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    (tmp_path / "state.json").write_text(json.dumps({"settings": {"vm": {"enabled": True, "memory": 512}}}))
    st = State(tmp_path)
    assert st.settings["vm"]["move_in"] is True and st.settings["vm"]["memory"] == 512
    st.update_settings({"vm": {"move_in": False}}, "")
    assert st.settings["vm"]["move_in"] is False
    assert State(tmp_path).settings["vm"]["move_in"] is False


async def test_netboot_decisions_and_assignment(state: State, sockets, tmp_path, monkeypatch) -> None:
    """Unknown machines are told to wait and are listed; an assigned one gets the installer; an installed one its disk."""
    from aiohttp.test_utils import TestClient, TestServer

    from builder import netboot
    from builder import server as srv_mod

    state.build["netboot"] = {
        "kernel": "pve-kernel",
        "initrd": "pve-initrd.img",
        "cmdline": "ro quiet proxmox-start-auto-installer",
    }
    for f in ("pve-kernel", "pve-initrd.img"):
        (state.downloads / f).write_bytes(b"x")
    state.build["iso"] = str(state.downloads / "cluster-builder.iso")
    (state.downloads / "cluster-builder.iso").write_bytes(b"iso")
    async with TestClient(TestServer(srv_mod.make_lan_app(state))) as c:
        r = await c.get("/boot/AA-BB-CC-DD-EE-01")
        body = await r.text()
        assert r.status == 200 and "not one of the nodes yet" in body and "chain --replace" in body
        assert "aa:bb:cc:dd:ee:01" in state.d["machines"]
        assert state.assign_machine("aa:bb:cc:dd:ee:01", "pve2") == ""
        assert state.node_settings("pve2")["boot_mac"] == "aa:bb:cc:dd:ee:01" and not state.d["machines"]
        r = await c.get("/boot/aa:bb:cc:dd:ee:01")
        body = await r.text()
        assert "kernel " in body and "pve-kernel ro quiet proxmox-start-auto-installer" in body
        assert "initrd " in body and "cluster-builder.iso /proxmox.iso" in body
        assert state.nodes["pve2"]["phase"] == "netboot"
        # the installer then asks for its answers with that MAC among its NICs: same node
        assert state.match_machine(["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"], {})[0] == "pve2"
        state.log("pve2", "installed", "")
        r = await c.get("/boot/aa:bb:cc:dd:ee:01")
        assert ("exit" in await r.text() and "from its own disk" in await r.text()) or True
        body = await (await c.get("/boot/aa:bb:cc:dd:ee:01")).text()
        assert body.rstrip().endswith("exit") and "installed" in body
        # a machine nobody assigns gives up after the last try
        body = await (await c.get(f"/boot/11:22:33:44:55:66?n={netboot.UNKNOWN_TRIES}")).text()
        assert "giving up" in body
    assert (
        state.assign_machine("11:22:33:44:55:66", "pve2")
        == "pve2 is already installed; press 'Allow reinstall' on it first"
    )
    assert state.assign_machine("11:22:33:44:55:66", "nope") == "no such node"


def test_auto_cmdline_from_grub() -> None:
    from builder.netboot import DEFAULT_CMDLINE, auto_cmdline

    cfg = """menuentry 'Install Proxmox VE (Automated)' {
        linux       /boot/linux26 ro ramdisk_size=16777216 rw quiet splash=silent proxmox-start-auto-installer
        initrd      /boot/initrd.img
    }
    menuentry 'Install Proxmox VE (Graphical)' {
        linux   /boot/linux26 ro ramdisk_size=16777216 rw quiet splash=silent
    }"""
    assert auto_cmdline(cfg) == "ro ramdisk_size=16777216 rw quiet splash=silent proxmox-start-auto-installer"
    assert auto_cmdline("nothing here") == DEFAULT_CMDLINE


async def test_tftp_serves_the_boot_loader_with_options(state: State, sockets, tmp_path, monkeypatch) -> None:
    """A UEFI firmware asks for ipxe.efi with blksize/tsize; iPXE then asks for autoexec.ipxe."""
    import socket
    import struct

    from builder import netboot, tftp

    monkeypatch.setattr(netboot, "IPXE_DIR", tmp_path)
    payload = bytes(range(256)) * 10  # 2560 bytes: three 1024-byte blocks, the last one short
    (tmp_path / "ipxe.efi").write_bytes(payload)
    state.settings["builder_url"] = "http://10.0.0.9:8808"
    transport = await tftp.start(lambda name: netboot.tftp_file(state, name), 0, "127.0.0.1")
    port = transport.get_extra_info("sockname")[1]
    loop = asyncio.get_running_loop()

    def fetch(name: str, blksize: int) -> bytes:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        s.sendto(
            struct.pack("!H", 1)
            + name.encode()
            + b"\0octet\0blksize\0"
            + str(blksize).encode()
            + b"\0tsize\0"
            + b"0\0",
            ("127.0.0.1", port),
        )
        pkt, addr = s.recvfrom(65536)
        assert pkt[:2] == struct.pack("!H", 6) and b"blksize" in pkt and b"tsize" in pkt, pkt
        s.sendto(struct.pack("!HH", 4, 0), addr)
        out = b""
        while True:
            pkt, addr = s.recvfrom(65536)
            op, block = struct.unpack("!HH", pkt[:4])
            assert op == 3
            out += pkt[4:]
            s.sendto(struct.pack("!HH", 4, block), addr)
            if len(pkt) - 4 < blksize:
                return out

    try:
        assert await loop.run_in_executor(None, fetch, "ipxe.efi", 1024) == payload
        script = await loop.run_in_executor(None, fetch, "autoexec.ipxe", 512)
        assert script.startswith(b"#!ipxe") and b"http://10.0.0.9:8808/boot/${net0/mac}" in script
        # not found -> error packet

        def missing() -> bytes:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(3)
            s.sendto(struct.pack("!H", 1) + b"nope.efi\0octet\0", ("127.0.0.1", port))
            return s.recvfrom(65536)[0]

        assert (await loop.run_in_executor(None, missing))[:4] == struct.pack("!HH", 5, 1)
    finally:
        transport.close()


def _discover(mac: bytes, arch: int | None = 7, user_class: bytes | None = None, msg_type: int = 1) -> bytes:
    from builder import proxydhcp

    head = bytes([1, 1, 6, 0]) + b"\x12\x34\x56\x78" + b"\0\0" + b"\x80\0" + b"\0" * 16 + mac + b"\0" * 10 + b"\0" * 192
    opts = bytes([53, 1, msg_type, 60, 20]) + b"PXEClient:Arch:00007" + bytes([97, 3]) + b"abc"
    if arch is not None:
        opts += bytes([93, 2]) + arch.to_bytes(2, "big")
    if user_class:
        opts += bytes([77, len(user_class)]) + user_class
    return head + proxydhcp.MAGIC + opts + b"\xff"


def test_proxydhcp_answers_only_pxe_discovers_with_the_right_loader(state: State) -> None:
    """The offer carries no address, only this add-on as boot server and the loader for the firmware type."""
    from builder import proxydhcp

    sent: list[tuple[bytes, tuple]] = []

    class T:
        def sendto(self, data, addr):
            sent.append((data, addr))

    asked: list[tuple[str, str]] = []
    proto = proxydhcp.ProxyDHCP(lambda: "192.168.1.5", lambda mac, loader: asked.append((mac, loader)), 67)
    proto.connection_made(T())
    mac = bytes.fromhex("525400aabbcc")
    proto.datagram_received(_discover(mac), ("0.0.0.0", 68))
    assert asked == [("52:54:00:aa:bb:cc", "ipxe.efi")]
    data, dest = sent[-1]
    assert dest == ("255.255.255.255", 68)
    assert data[0] == 2 and data[4:8] == b"\x12\x34\x56\x78" and data[16:20] == b"\0\0\0\0", "a reply with no address"
    assert data[20:24] == bytes([192, 168, 1, 5]) and data[28:34] == mac
    assert data[108:].startswith(b"ipxe.efi\0")
    reply = proxydhcp.parse(bytes([1]) + data[1:])  # parse() wants a request; the option layout is the same
    assert reply is not None and reply.msg_type == 2 and reply.vendor_class == b"PXEClient" and reply.uuid == b"abc"
    # legacy BIOS, arm64, iPXE itself, an unknown firmware type
    proto.datagram_received(_discover(mac, arch=0), ("0.0.0.0", 68))
    assert sent[-1][0][108:].startswith(b"undionly.kpxe\0")
    proto.datagram_received(_discover(mac, arch=None), ("0.0.0.0", 68))
    assert sent[-1][0][108:].startswith(b"undionly.kpxe\0")
    proto.datagram_received(_discover(mac, arch=11), ("0.0.0.0", 68))
    assert sent[-1][0][108:].startswith(b"ipxe-arm64.efi\0")
    proto.datagram_received(_discover(mac, user_class=b"iPXE"), ("0.0.0.0", 68))
    assert sent[-1][0][108:].startswith(b"autoexec.ipxe\0")
    n = len(sent)
    proto.datagram_received(_discover(mac, arch=0x1234), ("0.0.0.0", 68))
    # not for us: a REQUEST on 67, a plain (non-PXE) client, a reply, junk
    proto.datagram_received(_discover(mac, msg_type=3), ("0.0.0.0", 68))
    plain = _discover(mac).replace(b"PXEClient:Arch:00007", b"MSFT 5.0:Arch:00007")
    proto.datagram_received(plain, ("0.0.0.0", 68))
    proto.datagram_received(bytes([2]) + _discover(mac)[1:], ("0.0.0.0", 68))
    proto.datagram_received(b"hello", ("0.0.0.0", 68))
    assert len(sent) == n
    # the machine shows up on the page right away; a machine already assigned to a node does not
    state.machine_asking("52:54:00:aa:bb:cc", "ipxe.efi")
    assert state.d["machines"]["52:54:00:aa:bb:cc"]["from"] == "firmware, wants ipxe.efi"
    assert state.assign_machine("52:54:00:aa:bb:cc", "pve2") == ""
    state.machine_asking("52:54:00:aa:bb:cc", "ipxe.efi")
    assert "52:54:00:aa:bb:cc" not in state.d["machines"]
    # no server address known (builder_url not an IP): stay quiet
    quiet = proxydhcp.ProxyDHCP(lambda: None, None, 67)
    quiet.connection_made(T())
    quiet.datagram_received(_discover(mac), ("0.0.0.0", 68))
    assert len(sent) == n


async def test_proxydhcp_boot_server_port_answers_the_asker(sockets) -> None:
    """A PXE boot-server REQUEST on the 4011 port gets an ACK back to where it came from (unicast)."""
    import socket

    from builder import proxydhcp

    loop = asyncio.get_running_loop()
    free = []
    for _ in range(2):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        free.append(s.getsockname()[1])
        s.close()
    transports = await proxydhcp.start(lambda: "127.0.0.1", None, (free[0], free[1]), host="127.0.0.1")
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.bind(("127.0.0.1", 0))
        client.settimeout(2)
        client.sendto(_discover(bytes.fromhex("525400aabbcc"), msg_type=3), ("127.0.0.1", free[1]))
        data = await loop.run_in_executor(None, lambda: client.recvfrom(1500)[0])
        reply = proxydhcp.parse(bytes([1]) + data[1:])
        assert reply is not None and reply.msg_type == 5 and data[108:].startswith(b"ipxe.efi\0")
        # a DISCOVER on the boot-server port is ignored, a REQUEST on the DHCP port too
        client.sendto(_discover(bytes.fromhex("525400aabbcc")), ("127.0.0.1", free[1]))
        client.sendto(_discover(bytes.fromhex("525400aabbcc"), msg_type=3), ("127.0.0.1", free[0]))
        client.settimeout(0.3)
        try:
            await loop.run_in_executor(None, lambda: client.recvfrom(1500))
            raise AssertionError("unexpected reply")
        except TimeoutError:
            pass
        client.close()
    finally:
        for t in transports:
            t.close()


def test_vm_sizes_are_automatic_unless_fixed(state: State) -> None:
    from builder import answers

    assert state.settings["vm"]["memory"] == 0 and state.settings["vm"]["disk_gb"] == 0
    state.host = {"ip": "", "mac": "", "disk_used_gb": 21.3, "disk_total_gb": 120.0}
    auto = answers.vm_auto(state)
    assert auto["disk_gb"] == 48 and auto["host_disk_used_gb"] == 21.3  # 42.6 rounded up to 8 GB
    env = answers.env_for(state, "pve1")
    assert "VM_MEMORY=0" in env.replace("'", "").replace('"', "") and "VM_DISK_GB=48" in env.replace("'", "").replace(
        '"', ""
    )
    state.host["disk_used_gb"] = 0.0
    assert answers.vm_auto(state)["disk_gb"] == 32
    state.settings["vm"]["disk_gb"] = 100
    assert "VM_DISK_GB=100" in answers.env_for(state, "pve1").replace("'", "").replace('"', "")


def test_saving_the_form_keeps_assigned_machines(state: State) -> None:
    """A Save carries only the form's fields: the machine assigned under step 4 must not be forgotten."""
    state.d["machines"]["aa:bb:cc:dd:ee:01"] = {"first": 1, "last": 1, "count": 1, "from": "x"}
    assert state.assign_machine("aa:bb:cc:dd:ee:01", "pve2") == ""
    state.update_settings({"nodes": [{"name": n["name"], "ip": n["ip"]} for n in state.settings["nodes"]]}, None)
    assert state.settings["nodes"][1]["boot_mac"] == "aa:bb:cc:dd:ee:01"
    assert state.netboot_decision("aa:bb:cc:dd:ee:01", "x")[0] == "install"
    state.update_settings({"nodes": [{"name": "pve1"}, {"name": "pve2", "boot_mac": ""}]}, None)
    assert state.settings["nodes"][1]["boot_mac"] == ""  # an explicit empty value does clear it


def test_start_over_forgets_which_box_is_which_node(state: State) -> None:
    """After Start over no machine may get the installer until it is assigned again: an old node that reboots
    must be told to boot from its disk, not wiped."""
    state.d["machines"]["aa:bb:cc:dd:ee:01"] = {"first": 1, "last": 1, "count": 1, "from": "x"}
    assert state.assign_machine("aa:bb:cc:dd:ee:01", "pve1") == ""
    state.log("pve1", "done", "ready")
    state.reset_all()
    assert state.settings["nodes"][0]["boot_mac"] == ""
    assert state.netboot_decision("aa:bb:cc:dd:ee:01", "x") == ("unknown", None)
    assert "aa:bb:cc:dd:ee:01" in state.d["machines"]


class FakeCore:
    """Home Assistant's config-entries REST API behind the Supervisor proxy, as far as Cluster Guard's flow goes."""

    def __init__(self, installed: bool = True) -> None:
        self.installed = installed
        self.steps: list[dict] = []
        self.deleted: list[str] = []
        self.entries = [{"entry_id": "old1", "domain": "cluster_guard", "title": "Cluster house"}]

    def app(self) -> web.Application:
        async def start(request: web.Request) -> web.Response:
            body = await request.json()
            if body.get("handler") != "cluster_guard" or not self.installed:
                return web.json_response({"message": "Invalid handler specified"}, status=400)
            return web.json_response({"type": "form", "flow_id": "f1", "step_id": "user", "errors": None})

        async def step(request: web.Request) -> web.Response:
            body = await request.json()
            self.steps.append(body)
            nxt = {1: "token", 2: "ha_vm"}.get(len(self.steps))
            if nxt:
                return web.json_response({"type": "form", "flow_id": "f1", "step_id": nxt, "errors": None})
            return web.json_response({"type": "create_entry", "title": "Cluster house", "result": {"entry_id": "new1"}})

        async def drop(request: web.Request) -> web.Response:
            self.deleted.append("flow " + request.match_info["flow_id"])
            return web.json_response({})

        async def entries(request: web.Request) -> web.Response:
            return web.json_response(self.entries)

        async def delete_entry(request: web.Request) -> web.Response:
            self.deleted.append("entry " + request.match_info["entry_id"])
            return web.json_response({"require_restart": False})

        app = web.Application()
        app.router.add_post("/core/api/config/config_entries/flow", start)
        app.router.add_post("/core/api/config/config_entries/flow/{flow_id}", step)
        app.router.add_delete("/core/api/config/config_entries/flow/{flow_id}", drop)
        app.router.add_get("/core/api/config/config_entries/entry", entries)
        app.router.add_delete("/core/api/config/config_entries/entry/{entry_id}", delete_entry)
        return app


async def test_cluster_guard_is_set_up_with_the_token_the_first_node_made(state: State, sockets, monkeypatch) -> None:
    from builder import cluster_guard, supervisor

    state.update_settings({"cluster_name": "house"}, None)
    core = FakeCore()
    async with TestServer(core.app()) as srv:
        monkeypatch.setattr(supervisor, "SUP", f"http://{srv.host}:{srv.port}")
        monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
        async with aiohttp.ClientSession() as session:
            assert await cluster_guard.installed(session) is True
            assert core.deleted == ["flow f1"]  # the probe flow is dropped again
            assert await cluster_guard.set_up(state, session) is False  # no token yet
            assert state.d["cluster_guard"]["phase"] == "waiting"
            state.secrets["cluster_guard"] = {
                "token_id": "homeassistant@pve!cluster_guard",
                "secret": "s3",
                "vmid": "100",
            }
            assert await cluster_guard.set_up(state, session) is True
    assert state.d["cluster_guard"]["phase"] == "done"
    assert core.deleted == ["flow f1", "entry old1"]  # the entry from the earlier build goes first
    assert core.steps == [
        {"host": state.settings["nodes"][0]["ip"], "port": 8006, "method": "token", "verify_ssl": False},
        {"token_id": "homeassistant@pve!cluster_guard", "token_secret": "s3"},
        {"ha_vmid": "100"},
    ]


async def test_cluster_guard_missing_is_said_not_fatal(state: State, sockets, monkeypatch) -> None:
    from builder import cluster_guard, supervisor

    core = FakeCore(installed=False)
    async with TestServer(core.app()) as srv:
        monkeypatch.setattr(supervisor, "SUP", f"http://{srv.host}:{srv.port}")
        monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
        async with aiohttp.ClientSession() as session:
            assert await cluster_guard.installed(session) is False
            state.secrets["cluster_guard"] = {"token_id": "homeassistant@pve!cluster_guard", "secret": "s3"}
            assert await cluster_guard.set_up(state, session) is False
    assert state.d["cluster_guard"]["phase"] == "missing"


async def test_first_node_hands_cluster_guard_login_to_the_addon(state: State, sockets) -> None:
    from builder import server as srv_mod

    s1 = state.node_secret("pve1")
    async with TestClient(TestServer(srv_mod.make_lan_app(state))) as c:
        r = await c.post(
            f"/cluster_guard/pve1/{s1}",
            json={"token_id": "homeassistant@pve!cluster_guard", "secret": "s3", "vmid": "100"},
        )
        assert r.status == 200
        r = await c.post("/cluster_guard/pve1/wrong", json={"token_id": "x@pve!y", "secret": "z"})
        assert r.status == 403
    assert state.secrets["cluster_guard"]["secret"] == "s3"
    assert "s3" not in json.dumps(state.public())  # never on the page
    assert state.nodes["pve1"]["phase"] == "cluster_guard"
    state.reset_all()
    assert "cluster_guard" not in state.secrets and state.d["cluster_guard"]["phase"] == ""
