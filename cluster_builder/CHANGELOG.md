# Changelog

## 0.1.2

- Move-in: the add-on looks for the new Home Assistant on port 8123 **and** port 80 (Home Assistant OS 18
  serves it on 80), tells the landing page shown during the first boot from a running Home Assistant, and knows
  a restored Home Assistant 2026 by its missing onboarding API. Both waits are an hour now.
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
