# Changelog

## 0.2.1

- A node that took the network installer and went silent (no answers request within 5 min) is called out on its
  card with the likely cause (too little memory, or it booted from disk) and what to do; same for an installer that
  stops reporting.
- Docs: a network-installed box needs 8 GB of memory, not 4 (the installer image lives in memory; 4 GB panics).
- Page: says whose MAC address the VM field is (made up for the new VM, nothing to look up) and shows this Home
  Assistant's own address and MAC next to it.
- Fixed: the page refreshed every field from the server every 3 s while you were still editing, so a changed
  checkbox (or a field you had moved on from) was quietly reverted before Save. Nothing is refilled while the form
  has unsaved edits.
- Network install needs **no router change**: the add-on answers the machines' network-boot requests itself
  (ProxyDHCP on UDP 67 and 4011, next to the router's own DHCP), so the router's network-boot option is only a
  fallback. The add-on now uses the host's network for that (the port list is gone). A machine is listed under
  step 4 as soon as its firmware asks, before the boot loader has even loaded.
- Docs: the network boot loader is unsigned, so Secure Boot must be off on each box (page and DOCS say so now).

## 0.2.0

- New: **install over the network**, no stick. The add-on serves the boot loader (iPXE) over TFTP and the Proxmox
  auto-installer over HTTP; point the router's network-boot option at this Home Assistant, enable network boot on
  each box once, and the boxes appear on the page as they ask. Say which node each one is and it installs and restarts
  by itself. Any machine you do not assign is told to start from its own disk, and so is every node after its
  install, so "network first" in the firmware is safe to leave on.
- Step 1 chooses between the network and a USB stick; the stick path is unchanged.
- The add-on needs UDP port 69 for the boot loader (shown on the page if it cannot open it).
- Known gap: a node reinstalled after all three joined cannot join on its own (the build key is gone); DOCS says
  what to do by hand until the add-on handles node replacement.

## 0.1.2

Verified on a real Home Assistant OS 18.4 VM in the lab: the backup was restored through the Welcome screen,
the add-ons came back started and the test owner's token still worked.

- Move-in: the add-on looks for the new Home Assistant on port 8123 **and** port 80 (Home Assistant OS 18
  serves it on 80), tells the landing page shown during the first boot from a running Home Assistant, and knows
  a restored Home Assistant 2026 by its missing onboarding API. Both waits are an hour now, and
  the upload is retried while the new Home Assistant's backup manager still says "busy" (seen on a real
  first boot). A move-in under way is picked up again after an add-on restart.
- Fixed: creating the Home Assistant VM could fail with "cluster not ready - no quorum?" right after the
  cluster was formed (the cluster filesystem had not connected to corosync). The first boot now waits for the
  filesystem's own quorum view and restarts it if needed.

## 0.1.1

- New: **Move this Home Assistant onto it**. Build takes a backup of this Home Assistant; once the new VM
  shows its Welcome screen the add-on restores it there and tells you when to shut the old machine down.
- Fixed: the first node removed the build key from the cluster before the other nodes had joined, so
  their joins failed. The key now stays until the last node is in, and that node removes it for all.
- A failed join is reported at once with its error, instead of a quorum timeout minutes later.
- "Start over" and "Forget this machine" confirm in the page instead of a browser pop-up.
- "Start over" also clears the join lock's timestamp.
- Add-on config no longer sets options that equal their defaults (add-on linter).

## 0.1.0

First release: one stick for three nodes, unattended Proxmox install, cluster formed on first boot,
Home Assistant OS VM created as a failover resource with one-minute replication, live progress page.
