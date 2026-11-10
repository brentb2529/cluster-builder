"""Cluster Builder: machine matching, answer files, first-boot environment, and the LAN routes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
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
        (state.downloads / "x.img").write_bytes(b"img")
        assert (await c.get(f"/files/{token}/x.img")).status == 200
        assert (await c.get(f"/files/{token}/../state.json")).status in (403, 404)
        assert (await c.get(f"/iso/{token}/cluster-builder.iso")).status == 200


async def test_ui_routes(state: State, sockets) -> None:
    async with TestClient(TestServer(server.make_ui_app(state, dev=True))) as c:
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
