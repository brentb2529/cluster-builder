# Changelog

## 0.2.1

- Move-in: the restore is asked again when the new Home Assistant refuses it because it is frozen for a disk
  snapshot (the cluster replicates the VM every minute, and Home Assistant OS's guest agent freezes the Supervisor's
  backup manager for those seconds). It used to fail the move on that.
- While the move-in waits for the new Home Assistant, step 5 says what it sees at the VM's address: nothing yet,
  Home Assistant OS's landing page (it is downloading Home Assistant itself, about 1 GB, slow on a slow line), or
  Home Assistant starting; with the minutes waited. It used to say "takes a few minutes" for up to an hour.
- Cluster Guard is set up for you: the first node makes Cluster Guard its own Proxmox login (user, role, token) as root
  and hands it to the add-on, which sets the Cluster Guard integration up in this Home Assistant through the Supervisor
  before the move-in backup is taken. The restored Home Assistant arrives on the cluster already watching it; the last
  step is only "shut this machine down". The page says before Build if Cluster Guard is not installed here (HACS).
- The move-in backup is taken when the VM is up (fresh, and with Cluster Guard in it), not at Build; Build is faster.

- Fixed: the first node reported a VM disk smaller than it made (the Home Assistant OS image is 32 GB and a disk cannot shrink below it); the size now starts at the image's own size and the message says what the VM really got.
- Step 5 opens the address the move-in actually used (Home Assistant OS 18 serves on port 80) instead of always saying :8123, and no longer repeats "restoring your backup" twice. Step 4 says "No machine has asked to boot yet" only while no machine is assigned.
- Fixed: "Start over (forget all machines)" kept which box was which node, so an old node that rebooted after it would have been reinstalled without anyone clicking. It now forgets the assignments too; every machine is listed again and installed only once you say which node it is.
- Fixed: the move-in reported "Failed: Server disconnected" (or later "already set up") when the new Home
  Assistant restarted into the restore before answering the request; it now waits for the restored Home Assistant
  and reports done, and re-sends the backup only if the Welcome screen comes back.
- Fixed: the move-in gave up with "refused the backup: 404" when it caught the new Home Assistant in the seconds
  between its frontend and its onboarding API coming up; it now keeps trying through 401/404/5xx and dropped connections until its deadline.
- A node whose first boot failed runs it again (freshly fetched from the add-on) when it is turned on again,
  so the page's advice works and a fix in the add-on reaches it.
- Fixed: the first node could fail creating the VM with "cluster not ready - no quorum?": every other node's join
  restarts the cluster filesystem on it for a moment. It now waits for quorum and retries the creation (ten tries). A failed first boot reports the failing command's own last
  lines, not only the line number.
- Fixed: pressing Save in step 1 forgot which machine was assigned to which node (the nodes were then told
  "not one of the nodes yet" at their next network boot).
- Node cards show progress while a box loads and starts the network installer (kernel, files, image handed over,
  "starting the installer, 2 to 5 minutes"), a step-of-N bar, and green instead of orange while busy.
- Fixed: the machine list and node cards were rebuilt on every 3 s poll, so a click during a rebuild went nowhere
  and rows could move under the mouse (seen by hand: "This is pve3" needed three tries). They now update in
  place, rows keep their order, only the times tick.
- The Home Assistant VM is sized by the add-on: memory half of the first node's (2 to 8 GB) and cores half of its,
  decided on the node itself; disk twice what this Home Assistant uses today, at least 32 GB, never more than half
  the node's pool. Fixed sizes moved under Advanced (0 = automatic).
- A node that took the network installer and went silent (no answers request within 5 min) is called out on its
  card with the likely cause (too little memory, or it booted from disk) and what to do; same for an installer that
  stops reporting.
- Docs: a network-installed box needs 8 GB of memory, not 4 (the installer image is held in memory).
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
