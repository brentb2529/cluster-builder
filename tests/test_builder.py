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
    assert 'reboot-mode = "power-off"' in a
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
        await move_in.run(state, f"{srv.host}:{srv.port}")
    assert state.d["move_in"]["phase"] == "done", state.d["move_in"]
    assert welcome.uploaded == b"tar" * 100
    assert welcome.restore["backup_id"] == "abc123"
    assert welcome.restore["password"] == state.secrets["move_in_password"]
    assert welcome.restore["restore_addons"] == ["core_ssh"] and welcome.restore["restore_folders"] == ["share", "ssl"]
    assert welcome.restore["restore_database"] is True

    # an already set-up Home Assistant must not be touched
    welcome.user_done = True
    async with TestServer(welcome.app()) as srv:
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
    async with TestServer(welcome.app()) as srv:
        await move_in.run(state, f"{srv.host}:{srv.port}")
    assert state.d["move_in"]["phase"] == "done", state.d["move_in"]
    assert welcome.restore is not None

    # an onboarded HA 2026 (404 on the onboarding API, frontend manifest there) is "already set up"
    async with TestServer(welcome.app()) as srv:
        await move_in.run(state, f"{srv.host}:{srv.port}")
    assert state.d["move_in"]["phase"] == "failed" and "already set up" in state.d["move_in"]["msg"]


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
    monkeypatch.setattr(srv_mod.move_in, "run", lambda st, ip: _noop(started, ip))
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
