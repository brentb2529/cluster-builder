# Cluster Builder (Home Assistant add-on)

Boxes can boot the installer **over the network** (iPXE from the add-on, no stick) or from one USB stick.

Build a three-node Proxmox VE cluster for Home Assistant from one USB stick and one web page: no
command line. Each machine boots from the stick, installs itself, joins the cluster, and the first one
creates a Home Assistant OS VM protected by replication and automatic failover. Pair it with
[Cluster Guard](https://github.com/brentb2529/cluster-guard) to watch the cluster from Home Assistant.

[![Add repository to my Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fbrentb2529%2Fcluster-builder)

Settings > Add-ons > Add-on store > three dots > Repositories > add this URL, then install
**Cluster Builder**. Full instructions are in the add-on's Documentation tab (`cluster_builder/DOCS.md`).

## Development

The add-on is a small aiohttp app (`cluster_builder/app/builder`). Run it on a workstation:

```bash
cd cluster_builder/app && CB_UI_PORT=18099 CB_LAN_PORT=18808 python3 -m builder --dev
```

then open http://localhost:18099. `tests/` run with pytest (`pip install aiohttp pytest pytest-asyncio`).
`firstboot.sh` is the script every node runs once; `answers.py` writes the installer answer files.
