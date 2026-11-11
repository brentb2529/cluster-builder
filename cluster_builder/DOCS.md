# Cluster Builder

Turns three empty mini PCs into a Proxmox VE cluster that runs Home Assistant OS in a VM protected by
replication and automatic failover. You fill in one page, write one USB stick, and boot each machine
from it. The add-on answers the Proxmox installer's questions over your network, then tells each node
what to do on its first boot: fix the network, join the cluster, create the Home Assistant VM, set up
the one-minute copies. The page shows every node's progress.

## What you need

- Three small PCs with an NVMe drive each, two network ports preferred (one for the house, one for a
  private switch between the three). 16 GB RAM is comfortable.
- Three free addresses on your network, outside the router's DHCP range.
- One USB stick of 4 GB or more.
- Internet, for the Proxmox installer (1.7 GB) and the Home Assistant OS image (0.4 GB).

## Steps

1. **Install this add-on** on the Home Assistant you have today and open it from the sidebar.
2. **Fill in the page.** Most fields are pre-filled from your Home Assistant: time zone, router, DNS,
   suggested node addresses. Choose a root password for the nodes (you will type it once into Cluster
   Guard later). The page makes up a MAC address for the new Home Assistant VM (it is not one of the new
   machines' and not this Home Assistant's; nothing to look up): give it a DHCP reservation in your router so
   the new Home Assistant comes up at an address you know. It cannot have this machine's address while both
   run; after the move, when the old machine is off, you can hand the old address to the VM's MAC. Save.
3. **Build.** Downloads the installer and the OS image and writes the stick image. A few minutes.
4. **Write the stick** with Raspberry Pi Imager (Use custom) or Etcher. Everything on the stick is erased.
5. **Boot the machines.** Over the network (the default): nothing to set on the router, the add-on answers the
   machines' network-boot requests itself next to the router's DHCP (ProxyDHCP; if your router already has a
   network-boot/PXE option pointing elsewhere, clear it or point it at this Home Assistant and the file `ipxe.efi`).
   In each box's firmware once: turn Secure Boot off (the boot loader is not signed), enable network boot
   and put it before the disk (the box needs 8 GB of memory or more: the installer and its 1.7 GB image run from memory, 4 GB panics), then turn them
   on. Each
   box appears under step 4 as it asks to boot; say which node it is and it installs itself, restarts and finishes
   alone. With a stick instead: cable the box, boot it from the stick, it installs and **turns off**; remove the
   stick, turn it on again. Either way the first machine to install becomes the first node and gets the Home
   Assistant VM. A machine you never assign, and every node after its install, is told to start from its own disk.
6. When all three say **Done**, open `https://<first node>:8006` (user `root`), see three nodes, then
   open `http://homeassistant.local:8123` (plain `http://homeassistant.local` on Home
   Assistant OS 18) and restore your backup on the Welcome screen (or let the move-in do it, step 5). Add the
   Cluster Guard integration in the restored Home Assistant.

The stick erases whatever machine boots from it. It only answers machines this page has not installed
yet, and a machine that was already installed is refused until you press *Allow reinstall* on it.
Keep it out of machines you care about anyway.

## Moving this Home Assistant onto the cluster

Tick **Move this Home Assistant onto it** (step 1, on by default on Home Assistant OS). Build then first takes a
full backup of this Home Assistant (everything except this add-on), encrypted with a one-time key the add-on
keeps. When the first node has created the VM and Home Assistant OS shows its Welcome screen, the add-on sends
that backup in through the Welcome screen's own restore and waits for the restart. Step 5 then says **shut this
machine down**: for a few minutes two identical Home Assistants exist, and only one of them should talk to your
devices. USB radios do not follow a backup; that is what Radio Guard's restore onto the new adapters is for.

If the first node cannot see the VM's address (no guest agent yet, odd network), step 5 lets you type it in and
try again. Untick the option to get the plain Welcome screen and restore a backup by hand.
The new Home Assistant keeps the uploaded move-in backup in its backup list (a few MB); delete it there
whenever you like.

## How it works

- The stick is the official Proxmox ISO prepared with `proxmox-auto-install-assistant`, pointed at this
  add-on's port 8808 with a token. The installer sends its network cards' addresses; the add-on assigns
  the next free node and replies with that node's answer file (from DHCP, ZFS on the NVMe, root password
  hash, the add-on's SSH key, power off when done).
- After the install, Proxmox runs the first-boot script it fetched from the add-on: static address on
  the port that got DHCP, the other port as the private cluster link, no-subscription repositories,
  updates; the first node creates the cluster, the others take turns joining (over SSH with the add-on's
  key; the last node to join removes it from the whole cluster); the first node creates the Home Assistant VM from the official
  image as a high-availability resource, and every other node adds a one-minute replication job for it.
- Every step is reported back to the add-on, which is what the page shows. Each node also keeps
  `/var/log/cluster-builder.log`.

## Advanced

- **Address the machines use to reach this add-on**: pre-filled with this Home Assistant's address.
  Change it only if the new machines sit on another network.
- **Cluster address / LAN port MAC / Link port MAC**: for setups where the cluster talks on a different
  network than the node's management address, or when you want to pin which port is which.
- **Extra commands at first boot**: a bash script run on every node after the network and packages are
  done, before the cluster is formed. The node's name is its hostname.
- **Extra VM options**: appended to `qm create`. The default is `--machine q35 --cpu host`.
- **No internet, or a different installer**: put `proxmox-ve.iso` and `haos.qcow2.xz` in the `share/cluster_builder`
  folder (Samba or the File editor) and the build uses them instead of downloading. Files there are also served to
  the nodes at `<add-on address>/files/<token>/<name>`, so the Home Assistant OS image field can point at one.

## If something goes wrong

- A node shows *Failed*: read its log on the page, or on the node at `/var/log/cluster-builder.log`.
  Press *Forget this machine*, boot it from the stick again.
- A machine boots from the stick but nothing appears here: it could not reach this add-on. Check that it
  got an address from your router and that port 8808 of this Home Assistant is open on your network.
- The machine does not boot from the stick: use the boot menu key (F7 on MeLE Quieter, F11 or F12 on
  most others) and choose the USB stick.

## Options

`log_level`: how much the add-on writes to its log.

## Reinstalling a node later

The build key is removed from the cluster once the last node has joined, so a node you reinstall after that
(with **Allow reinstall** on its card) installs fine but cannot join: its log says "unable to copy ssh ID ...
Permission denied". Until the add-on handles this itself, put the key back on a working node first: copy the
key shown under *Advanced* on the page into `/etc/pve/priv/authorized_keys` on any node (one line), remove the
old node from the cluster (`pvecm delnode <name>`) and delete its replication job, then let the reinstalled
node reboot into its first boot. The last node to join removes the key again.
