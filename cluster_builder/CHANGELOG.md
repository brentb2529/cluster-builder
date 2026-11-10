# Changelog

## 0.1.1

- Fixed: the first node removed the build key from the cluster before the other nodes had joined, so
  their joins failed. The key now stays until the last node is in, and that node removes it for all.
- A failed join is reported at once with its error, instead of a quorum timeout minutes later.
- "Start over" and "Forget this machine" confirm in the page instead of a browser pop-up.
- "Start over" also clears the join lock's timestamp.
- Add-on config no longer sets options that equal their defaults (add-on linter).

## 0.1.0

First release: one stick for three nodes, unattended Proxmox install, cluster formed on first boot,
Home Assistant OS VM created as a failover resource with one-minute replication, live progress page.
