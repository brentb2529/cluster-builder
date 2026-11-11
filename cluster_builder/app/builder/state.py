"""Everything the add-on remembers, in one JSON file under /data.

Settings are what the person typed; secrets are generated once (answer token, per-node secrets, the
cluster SSH key); nodes track each machine from "waiting" to "done"; build tracks the ISO work.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from . import supervisor

_LOGGER = logging.getLogger(__name__)

PVE_ISO_URL = "https://enterprise.proxmox.com/iso/proxmox-ve_9.2-1.iso"
HAOS_RELEASES_API = "https://api.github.com/repos/home-assistant/operating-system/releases/latest"
HAOS_IMAGE_FALLBACK = (
    "https://github.com/home-assistant/operating-system/releases/download/18.4/haos_generic-x86-64-18.4.qcow2.xz"
)

PHASES = [
    ("waiting", "Waiting for a machine to boot the installer"),
    ("netboot", "Booting the installer over the network"),
    ("installing", "Installing Proxmox"),
    ("installed", "Installed. Remove the stick when the machine turns itself off, then turn it on"),
    ("hello", "First boot started"),
    ("network", "Network configured"),
    ("packages", "Packages updated"),
    ("cluster_created", "Cluster created"),
    ("waiting_cluster", "Waiting for the first node to create the cluster"),
    ("joining", "Joining the cluster"),
    ("joined", "Joined the cluster"),
    ("image", "Downloading the Home Assistant OS image"),
    ("vm_created", "Home Assistant VM created and protected"),
    ("vm_wait", "Waiting for the Home Assistant VM to come up on the network"),
    ("vm_ip", "Home Assistant VM is on the network"),
    ("replicating", "Copy of the Home Assistant VM set up on this node"),
    ("done", "Done"),
    ("failed", "Failed"),
]
PHASE_TEXT = dict(PHASES)
MOVE_IN_PHASES = [
    ("", "Not started"),
    ("waiting_vm", "Waiting for the first node to report the VM's address"),
    ("waiting_ha", "Waiting for Home Assistant OS to boot in the VM"),
    ("uploading", "Sending your backup to the new Home Assistant"),
    ("restoring", "The new Home Assistant is restoring your backup"),
    ("done", "Moved in"),
    ("failed", "Failed"),
]


def default_settings() -> dict[str, Any]:
    return {
        "install_method": "network",  # or "stick"
        "cluster_name": "house",
        "domain": "home.arpa",
        "builder_url": "",
        "iso_url": PVE_ISO_URL,
        "image_url": "",  # empty = latest HA OS generic x86-64 at build time
        "prefix": 24,
        "gateway": "",
        "dns": "",
        "timezone": "",
        "country": "us",
        "keyboard": "en-us",
        "disk_filter": "/dev/nvme*",
        "link_prefix": "10.10.10.",
        "nodes": [
            {
                "name": "pve1",
                "ip": "",
                "cluster_ip": "",
                "link_ip": "10.10.10.1",
                "lan_mac": "",
                "link_mac": "",
                "boot_mac": "",
            },
            {
                "name": "pve2",
                "ip": "",
                "cluster_ip": "",
                "link_ip": "10.10.10.2",
                "lan_mac": "",
                "link_mac": "",
                "boot_mac": "",
            },
            {
                "name": "pve3",
                "ip": "",
                "cluster_ip": "",
                "link_ip": "10.10.10.3",
                "lan_mac": "",
                "link_mac": "",
                "boot_mac": "",
            },
        ],
        "vm": {
            "move_in": supervisor.available(),
            "enabled": True,
            "vmid": 100,
            "name": "homeassistant",
            "memory": 0,  # 0 = sized on the first node: half its memory, 2 to 8 GB
            "cores": 0,  # 0 = half the node's cores
            "disk_gb": 0,  # 0 = twice what this Home Assistant uses now, at least 32 GB
            "mac": "",
            "qm_args": "--machine q35 --cpu host",
        },
        "extra_firstboot": "",
    }


def random_mac() -> str:
    """A locally administered MAC in Proxmox's own prefix, so DHCP reservations survive rebuilds."""
    return "BC:24:11:" + ":".join(f"{b:02X}" for b in secrets.token_bytes(3))


def hash_password(password: str) -> str:
    out = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=password + "\n", capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "openssl passwd failed")
    return out.stdout.strip()


def make_ssh_key() -> tuple[str, str]:
    with tempfile.TemporaryDirectory() as d:
        key = Path(d) / "key"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "cluster-builder", "-f", str(key)], check=True
        )
        return key.read_text(), key.with_suffix(".pub").read_text().strip()


class State:
    def __init__(self, data_dir: Path) -> None:
        self.dir = data_dir
        self.path = data_dir / "state.json"
        self.downloads = data_dir / "downloads"
        self.downloads.mkdir(parents=True, exist_ok=True)
        self.d: dict[str, Any] = {
            "settings": default_settings(),
            "secrets": {},
            "root_hash": "",
            "nodes": {},
            "build": {
                "status": "idle",
                "step": "",
                "progress": 0.0,
                "error": "",
                "iso": "",
                "image": "",
                "built_at": 0,
            },
            "cluster": {"created": False, "joining": None, "vm_created": False},
            "move_in": {"phase": "", "msg": "", "ip": "", "updated": 0},
        }
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text())
                for k in self.d:
                    if k in saved:
                        self.d[k] = saved[k] if k != "settings" else {**default_settings(), **saved[k]}
                # new settings keys (this version) under older saved settings
                self.d["settings"]["vm"] = {**default_settings()["vm"], **self.d["settings"].get("vm", {})}
                self.d["move_in"] = {**{"phase": "", "msg": "", "ip": "", "updated": 0}, **self.d.get("move_in", {})}
            except Exception:
                _LOGGER.exception("state.json unreadable; starting fresh")
        s = self.d["secrets"]
        changed = False
        if "answer_token" not in s:
            s["answer_token"] = secrets.token_urlsafe(24)
            changed = True
        if "move_in_password" not in s:
            s["move_in_password"] = secrets.token_urlsafe(18)
            changed = True
        if "ssh_private" not in s:
            try:
                s["ssh_private"], s["ssh_public"] = make_ssh_key()
                changed = True
            except Exception:
                _LOGGER.exception("ssh-keygen failed")
        if not self.d["settings"]["vm"].get("mac"):
            self.d["settings"]["vm"]["mac"] = random_mac()
            changed = True
        self._ensure_nodes()
        if changed:
            self.save()

    # ---- persistence

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, indent=1))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    @property
    def settings(self) -> dict[str, Any]:
        return self.d["settings"]

    @property
    def secrets(self) -> dict[str, Any]:
        return self.d["secrets"]

    @property
    def build(self) -> dict[str, Any]:
        return self.d["build"]

    @property
    def cluster(self) -> dict[str, Any]:
        return self.d["cluster"]

    @property
    def nodes(self) -> dict[str, Any]:
        return self.d["nodes"]

    def node_names(self) -> list[str]:
        return [n["name"] for n in self.settings["nodes"]]

    def node_settings(self, name: str) -> dict[str, Any] | None:
        return next((n for n in self.settings["nodes"] if n["name"] == name), None)

    def _ensure_nodes(self) -> None:
        for name in self.node_names():
            self.nodes.setdefault(
                name,
                {
                    "phase": "waiting",
                    "msg": "",
                    "machine": None,
                    "installed": False,
                    "allow_reinstall": False,
                    "log": [],
                    "updated": 0,
                },
            )
            self.secrets.setdefault("node_secrets", {}).setdefault(name, secrets.token_urlsafe(18))
        for name in list(self.nodes):
            if name not in self.node_names():
                del self.nodes[name]
        self.d.setdefault("machines", {})  # machines that asked to network-boot and are not assigned yet
        self.settings.setdefault("install_method", "network")
        for n in self.settings["nodes"]:
            n.setdefault("boot_mac", "")

    # ---- settings

    def update_settings(self, new: dict[str, Any], password: str | None) -> None:
        s = self.settings
        for k in default_settings():
            if k in new and k not in ("nodes", "vm"):
                s[k] = new[k]
        if "vm" in new:
            s["vm"] = {**s["vm"], **{k: v for k, v in new["vm"].items() if k in s["vm"]}}
            s["vm"]["move_in"] = bool(s["vm"].get("move_in"))
            if not s["vm"].get("mac"):
                s["vm"]["mac"] = random_mac()
        if "nodes" in new:
            nodes = []
            for i, n in enumerate(new["nodes"][:3]):
                name = str(n.get("name") or f"pve{i + 1}").strip()
                old = s["nodes"][i] if i < len(s["nodes"]) else {}
                # the form only carries what it shows: a machine assigned under step 4 (boot_mac) must survive a Save
                boot_mac = n["boot_mac"] if "boot_mac" in n else old.get("boot_mac", "")
                nodes.append(
                    {
                        "name": name,
                        "ip": str(n.get("ip", "")).strip(),
                        "cluster_ip": str(n.get("cluster_ip", "")).strip(),
                        "link_ip": str(n.get("link_ip", "")).strip(),
                        "lan_mac": str(n.get("lan_mac", "")).strip().lower(),
                        "link_mac": str(n.get("link_mac", "")).strip().lower(),
                        "boot_mac": str(boot_mac or "").strip().lower().replace("-", ":"),
                    }
                )
            s["nodes"] = nodes
        if s.get("install_method") not in ("network", "stick"):
            s["install_method"] = "network"
        if password:
            self.d["root_hash"] = hash_password(password)
        self._ensure_nodes()
        self.save()

    def validate(self) -> list[str]:
        """Human-readable reasons a build cannot start."""
        s = self.settings
        errs = []
        if not self.d["root_hash"]:
            errs.append("Choose a root password for the nodes.")
        if not s["builder_url"].startswith("http"):
            errs.append("The address the machines use to reach this add-on must start with http://.")
        if not s["gateway"]:
            errs.append("Enter the gateway (your router's address).")
        if not s["dns"]:
            errs.append("Enter a DNS server (usually the router's address).")
        if not s["timezone"]:
            errs.append("Enter the time zone.")
        names = [n["name"] for n in s["nodes"]]
        if len(set(names)) != len(names) or any(not n.replace("-", "").isalnum() for n in names):
            errs.append("Node names must be unique letters and digits.")
        for n in s["nodes"]:
            if not n["ip"]:
                errs.append(f"Enter an address for {n['name']}.")
        if s["vm"]["enabled"] and not s["image_url"] and self.build.get("status") != "running":
            pass  # resolved at build time
        if s["vm"].get("move_in") and not supervisor.available():
            errs.append(
                "Moving this Home Assistant in needs the Supervisor (Home Assistant OS or Supervised): untick it."
            )
        return errs

    # ---- nodes

    def node_secret(self, name: str) -> str:
        return self.secrets["node_secrets"][name]

    def log(self, name: str, phase: str, msg: str = "") -> None:
        node = self.nodes[name]
        node["phase"] = phase
        node["msg"] = msg
        node["updated"] = time.time()
        node["log"].append([time.time(), phase, msg])
        node["log"] = node["log"][-200:]
        if phase == "installed":
            node["installed"] = True
            node["allow_reinstall"] = False
        if phase == "installing":
            node["installed"] = False
            node["allow_reinstall"] = False
            node["joined"] = False
        if phase == "cluster_created":
            self.cluster["created"] = True
            node["joined"] = True
        if phase == "joined":
            node["joined"] = True
        if phase == "vm_created":
            self.cluster["vm_created"] = True
        if phase == "vm_ip":
            self.d["move_in"]["ip"] = msg
        if phase in ("joined", "failed", "done") and self.cluster.get("joining") == name:
            self.cluster["joining"] = None
        self.save()

    def reset_node(self, name: str) -> None:
        self.nodes[name] = {
            "phase": "waiting",
            "msg": "",
            "machine": None,
            "installed": False,
            "allow_reinstall": False,
            "joined": False,
            "log": [],
            "updated": 0,
        }
        if self.cluster.get("joining") == name:
            self.cluster["joining"] = None
        self.secrets["node_secrets"][name] = secrets.token_urlsafe(18)
        self.save()

    def reset_all(self) -> None:
        self.d["machines"] = {}
        for name in list(self.nodes):
            self.reset_node(name)
        self.cluster.update({"created": False, "joining": None, "joining_since": 0, "vm_created": False})
        self.d["move_in"] = {"phase": "", "msg": "", "ip": "", "updated": 0}
        self.save()

    def move_in_log(self, phase: str, msg: str = "") -> None:
        self.d["move_in"].update({"phase": phase, "msg": msg, "updated": time.time()})
        _LOGGER.info("move-in %s: %s", phase, msg)
        self.save()

    def all_joined(self) -> bool:
        """Every configured node is in the cluster (so the build key can go)."""
        return all(n.get("joined") for n in self.nodes.values())

    def match_machine(self, macs: list[str], info: dict[str, Any]) -> tuple[str | None, str]:
        """Which node a booting machine is. Known MACs win; else the first node still waiting."""
        macs = [m.lower() for m in macs]
        for name in self.node_names():
            node = self.nodes[name]
            if node["machine"] and set(node["machine"]["macs"]) & set(macs):
                if node["installed"] and not node["allow_reinstall"]:
                    return None, f"{name} is already installed; press 'Allow reinstall' on it first"
                return name, ""
        for name in self.node_names():
            ns = self.node_settings(name) or {}
            if (ns.get("lan_mac") and ns["lan_mac"] in macs) or (ns.get("boot_mac") and ns["boot_mac"] in macs):
                self.nodes[name]["machine"] = {"macs": macs, **info}
                return name, ""
        for name in self.node_names():
            node = self.nodes[name]
            ns = self.node_settings(name) or {}
            if node["machine"] is None and not ns.get("lan_mac"):
                node["machine"] = {"macs": macs, **info}
                return name, ""
        return None, "all nodes are already assigned to machines"

    # ---- network boot

    def netboot_decision(self, mac: str, remote: str) -> tuple[str, str | None]:
        """What a machine asking to network-boot gets: "install" / "local" for a known node, else "unknown"
        (and it is remembered so the page can offer to assign it)."""
        mac = mac.lower().replace("-", ":")
        name = self.node_for_mac(mac)
        if name is not None:
            node = self.nodes[name]
            if node["installed"] and not node["allow_reinstall"]:
                return "local", name
            return "install", name
        m = self.d["machines"].setdefault(mac, {"first": time.time(), "count": 0, "from": remote})
        m["last"] = time.time()
        m["count"] += 1
        m["from"] = remote
        self.save()
        return "unknown", None

    def node_for_mac(self, mac: str) -> str | None:
        for name in self.node_names():
            node = self.nodes[name]
            ns = self.node_settings(name) or {}
            known = set((node["machine"] or {}).get("macs", []))
            known |= {m for m in (ns.get("boot_mac"), ns.get("lan_mac")) if m}
            if mac in known:
                return name
        return None

    def node_loading_from(self, ip: str) -> str | None:
        """The node whose machine (at *ip*) is fetching the network installer right now."""
        for name, node in self.nodes.items():
            if node.get("netboot_ip") == ip and node.get("phase") == "netboot":
                return name
        return None

    def machine_asking(self, mac: str, loader: str) -> None:
        """A machine's firmware asked where to boot from (ProxyDHCP): list it right away, before iPXE has loaded,
        so the page shows something is happening. The count stays for /boot/<mac> calls."""
        mac = mac.lower().replace("-", ":")
        if self.node_for_mac(mac) is not None:
            return
        m = self.d["machines"].get(mac)
        if m is None:
            now = time.time()
            self.d["machines"][mac] = {"first": now, "last": now, "count": 0, "from": f"firmware, wants {loader}"}
            self.save()
        else:
            m["last"] = time.time()

    def assign_machine(self, mac: str, name: str) -> str:
        """Make the machine with *mac* node *name*; returns a reason when that is not allowed."""
        mac = mac.lower().replace("-", ":")
        ns = self.node_settings(name)
        if ns is None:
            return "no such node"
        node = self.nodes[name]
        if node["installed"] and not node["allow_reinstall"]:
            return f"{name} is already installed; press 'Allow reinstall' on it first"
        for other in self.settings["nodes"]:
            if other is not ns and other.get("boot_mac") == mac:
                other["boot_mac"] = ""
        ns["boot_mac"] = mac
        if node["machine"] and mac not in node["machine"]["macs"]:
            node["machine"] = None
        self.d["machines"].pop(mac, None)
        self.save()
        return ""

    # ---- what the UI may see (no secrets)

    def public(self) -> dict[str, Any]:
        out = copy.deepcopy({k: v for k, v in self.d.items() if k != "secrets"})
        out["has_password"] = bool(self.d["root_hash"])
        out["root_hash"] = None
        out["phases"] = PHASES
        out["ssh_public"] = self.secrets.get("ssh_public", "")
        out["move_in_phases"] = MOVE_IN_PHASES
        out["supervisor"] = supervisor.available()
        out["iso_name"] = "cluster-builder.iso"
        out["iso_token"] = self.secrets["answer_token"]
        return out
