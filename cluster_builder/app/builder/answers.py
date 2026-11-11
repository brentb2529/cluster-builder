"""Answer files for the Proxmox auto-installer, and the first-boot environment each node receives."""

from __future__ import annotations

import base64
import shlex
from pathlib import Path
from typing import Any

from .state import State

TEMPLATE = Path(__file__).with_name("firstboot.sh").read_text()


def _url(state: State) -> str:
    return state.settings["builder_url"].rstrip("/")


def toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def answer_for(state: State, name: str) -> str:
    """The TOML the installer gets for node *name*. Network from DHCP (the first boot makes it static),
    ZFS on the filtered disk, power off when done, first-boot script and webhook from this add-on."""
    s = state.settings
    base = _url(state)
    sec = state.node_secret(name)
    pub = state.secrets.get("ssh_public", "")
    lines = [
        "[global]",
        f"keyboard = {toml_str(s['keyboard'] or 'en-us')}",
        f"country = {toml_str(s['country'] or 'us')}",
        f"fqdn = {toml_str(name + '.' + s['domain'])}",
        f"mailto = {toml_str('root@' + s['domain'])}",
        f"timezone = {toml_str(s['timezone'])}",
        f"root-password-hashed = {toml_str(state.d['root_hash'])}",
        f"root-ssh-keys = [{toml_str(pub)}]",
        "reboot-on-error = false",
        # a network-booted machine just restarts into its first boot; a stick must be pulled first
        f"reboot-mode = {toml_str('reboot' if s.get('install_method') == 'network' else 'power-off')}",
        "",
        "[network]",
        'source = "from-dhcp"',
        "",
        "[disk-setup]",
        'filesystem = "zfs"',
        'zfs.raid = "raid0"',
        f"filter.DEVNAME = {toml_str(s['disk_filter'] or '/dev/nvme*')}",
        "",
        "[first-boot]",
        'source = "from-url"',
        f"url = {toml_str(f'{base}/firstboot/{name}/{sec}')}",
        'ordering = "fully-up"',
        "",
        "[post-installation-webhook]",
        f"url = {toml_str(f'{base}/installed/{name}/{sec}')}",
        "",
    ]
    return "\n".join(lines)


def firstboot_for(state: State, name: str) -> str:
    sec = state.node_secret(name)
    return TEMPLATE.replace("@@URL@@", _url(state)).replace("@@NODE@@", name).replace("@@SECRET@@", sec)


def env_for(state: State, name: str) -> str:
    """Shell-sourceable environment for the first-boot script."""
    s = state.settings
    nodes = s["nodes"]
    me = state.node_settings(name) or {}
    first = nodes[0]
    idx = [n["name"] for n in nodes].index(name)
    vm = s["vm"]

    def ip_of(n: dict[str, Any]) -> str:
        return n.get("cluster_ip") or n["ip"]

    env: dict[str, Any] = {
        "NODE": name,
        "NODE_INDEX": idx,
        "DOMAIN": s["domain"],
        "CLUSTER": s["cluster_name"],
        "MGMT_IP": me["ip"],
        "PREFIX": s["prefix"],
        "GATEWAY": s["gateway"],
        "DNS": s["dns"],
        "CLUSTER_IP": ip_of(me),
        "LAN_MAC": me.get("lan_mac", ""),
        "LINK_IP": me.get("link_ip", ""),
        "LINK_MAC": me.get("link_mac", ""),
        "FIRST_NODE": first["name"],
        "FIRST_IP": ip_of(first),
        "HOSTS": " ".join(f"{ip_of(n)}={n['name']}" for n in nodes),
        "IS_FIRST": "1" if idx == 0 else "0",
        "SSH_KEY_B64": base64.b64encode(state.secrets.get("ssh_private", "").encode()).decode(),
        "SSH_PUB": state.secrets.get("ssh_public", ""),
        "VM_ENABLED": "1" if vm["enabled"] else "0",
        "VMID": vm["vmid"],
        "VM_NAME": vm["name"],
        "VM_MEMORY": vm["memory"],
        "VM_CORES": vm["cores"],
        "VM_DISK_GB": vm["disk_gb"],
        "VM_MAC": vm["mac"],
        "VM_QM_ARGS": vm["qm_args"],
        "IMAGE_URL": state.build.get("image_served_url", ""),
        "MOVE_IN": "1" if vm.get("move_in") and state.build.get("move_in_backup") else "0",
        "EXTRA_B64": base64.b64encode(s.get("extra_firstboot", "").encode()).decode(),
    }
    return "".join(f"{k}={shlex.quote(str(v))}\n" for k, v in env.items())
