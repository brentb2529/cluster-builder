#!/bin/bash
# Cluster Builder first boot for @@NODE@@. Runs once, as root, after Proxmox is installed and the
# network is up. Everything it does is reported to the add-on so you can watch it from Home Assistant.
# Log: /var/log/cluster-builder.log
B='@@URL@@'; N='@@NODE@@'; S='@@SECRET@@'
LOG=/var/log/cluster-builder.log
exec >>"$LOG" 2>&1
set -u -o pipefail

json_escape() { python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"; }
say() {
  echo "[$(date '+%F %T')] $1: ${2:-}"
  curl -fsS -m 10 -X POST -H 'Content-Type: application/json' \
    -d "{\"phase\":$(json_escape "$1"),\"msg\":$(json_escape "${2:-}")}" "$B/status/$N/$S" >/dev/null 2>&1 || true
}
fail() { say failed "$1"; exit 1; }
trap 'fail "line $LINENO: $BASH_COMMAND"' ERR
set -e

say hello "first boot started on $(hostname)"

# --- the builder's answers for this node (retry: the add-on may still be starting)
for i in $(seq 1 90); do
  curl -fsS -m 15 "$B/env/$N/$S" -o /run/cb.env && break
  sleep 10
done
[ -s /run/cb.env ] || fail "could not fetch the node settings from $B"
# shellcheck disable=SC1091
. /run/cb.env

# the cluster filesystem (pmxcfs) has its own quorum view, and that is what qm/pvesh go by. Right after
# "pvecm create" it can sit with no cluster at all (it was restarted before corosync was up) while corosync
# is already quorate; a restart of pve-cluster reconnects it.
wait_quorum() {
  for i in $(seq 1 60); do
    grep -q '"quorate": 1' /etc/pve/.members 2>/dev/null && return 0
    case "$i" in 10|30) pvecm status 2>/dev/null | grep -q "Quorate: *Yes" && systemctl restart pve-cluster ;; esac
    sleep 3
  done
  return 1
}
mac_if() { for d in /sys/class/net/*; do [ -e "$d/device" ] || continue; [ "$(cat "$d/address" 2>/dev/null)" = "$1" ] && basename "$d" && return 0; done; return 1; }

# --- network: the NIC the installer used (DHCP) becomes static vmbr0; cluster and link addresses on top
PORT=$(awk '/^iface vmbr0/{f=1} f && /bridge-ports/{print $2; exit}' /etc/network/interfaces)
[ -n "$PORT" ] || fail "no bridge port on vmbr0 in /etc/network/interfaces"
LAN_IF=""; LINK_IF=""
if [ -n "$LAN_MAC" ] && [ "$CLUSTER_IP" != "$MGMT_IP" ]; then LAN_IF=$(mac_if "$LAN_MAC" || true); fi
if [ -n "$LINK_IP" ]; then
  if [ -n "$LINK_MAC" ]; then LINK_IF=$(mac_if "$LINK_MAC" || true); fi
  if [ -z "$LINK_IF" ]; then
    for d in /sys/class/net/*; do
      i=$(basename "$d"); [ -e "$d/device" ] || continue
      [ "$i" = "$PORT" ] && continue; [ "$i" = "$LAN_IF" ] && continue
      [ "$(cat "$d/carrier" 2>/dev/null || echo 0)" = "1" ] && { LINK_IF=$i; break; }
    done
  fi
fi
{
  echo "auto lo"; echo "iface lo inet loopback"; echo
  echo "auto $PORT"; echo "iface $PORT inet manual"; echo
  echo "auto vmbr0"; echo "iface vmbr0 inet static"
  echo "    address $MGMT_IP/$PREFIX"; echo "    gateway $GATEWAY"
  echo "    bridge-ports $PORT"; echo "    bridge-stp off"; echo "    bridge-fd 0"
  echo "#management and guests (Cluster Builder)"; echo
  if [ -n "$LAN_IF" ]; then
    echo "auto $LAN_IF"; echo "iface $LAN_IF inet manual"; echo
    echo "auto vmbr1"; echo "iface vmbr1 inet static"
    echo "    address $CLUSTER_IP/$PREFIX"; echo "    bridge-ports $LAN_IF"; echo "    bridge-stp off"; echo "    bridge-fd 0"
    echo "#cluster LAN (Cluster Builder)"; echo
  fi
  if [ -n "$LINK_IF" ]; then
    echo "auto $LINK_IF"; echo "iface $LINK_IF inet static"; echo "    address $LINK_IP/24"
    echo "#private cluster link (Cluster Builder)"; echo
  fi
  echo "source /etc/network/interfaces.d/*"
} > /etc/network/interfaces.new
mv /etc/network/interfaces.new /etc/network/interfaces
printf 'search %s\nnameserver %s\n' "$DOMAIN" "$DNS" > /etc/resolv.conf
{
  echo "127.0.0.1 localhost.localdomain localhost"
  for h in $HOSTS; do echo "${h%%=*} ${h##*=}.$DOMAIN ${h##*=}"; done
  echo; echo "::1 ip6-localhost ip6-loopback"; echo "fe00::0 ip6-localnet"; echo "ff00::0 ip6-mcastprefix"
  echo "ff02::1 ip6-allnodes"; echo "ff02::2 ip6-allrouters"; echo "ff02::3 ip6-allhosts"
} > /etc/hosts
ifreload -a || systemctl restart networking
sleep 3
for i in $(seq 1 30); do curl -fsS -m 10 "$B/env/$N/$S" -o /dev/null && break; sleep 5; done
say network "vmbr0=$PORT $MGMT_IP/$PREFIX${LAN_IF:+, vmbr1=$LAN_IF $CLUSTER_IP}${LINK_IF:+, link=$LINK_IF $LINK_IP}"

# --- repositories: no-subscription on, enterprise off, everything current
rm -f /etc/apt/sources.list.d/pve-enterprise.sources /etc/apt/sources.list.d/pve-enterprise.list \
      /etc/apt/sources.list.d/ceph.sources /etc/apt/sources.list.d/ceph.list
cat > /etc/apt/sources.list.d/pve-no-subscription.sources <<SRC
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
SRC
export DEBIAN_FRONTEND=noninteractive
apt-get -qq update >/dev/null
apt-get -qq -y dist-upgrade >/dev/null
say packages "$(pveversion)"

# --- anything extra the person asked for (advanced)
if [ -n "$EXTRA_B64" ]; then
  echo "$EXTRA_B64" | base64 -d > /usr/local/sbin/cluster-builder-extra.sh
  chmod +x /usr/local/sbin/cluster-builder-extra.sh
  /usr/local/sbin/cluster-builder-extra.sh
fi

# --- the cluster: first node creates it, the others take turns joining
mkdir -p /root/.ssh; chmod 700 /root/.ssh
echo "$SSH_KEY_B64" | base64 -d > /root/.ssh/id_ed25519; chmod 600 /root/.ssh/id_ed25519
LINK_OPT=""; [ -n "$LINK_IF" ] && LINK_OPT="--link1 $LINK_IP"
if [ "$IS_FIRST" = 1 ]; then
  if ! pvecm status >/dev/null 2>&1; then
    # shellcheck disable=SC2086
    pvecm create "$CLUSTER" --link0 "$CLUSTER_IP" $LINK_OPT
  fi
  wait_quorum || fail "the new cluster did not become quorate"
  say cluster_created "cluster $CLUSTER on $CLUSTER_IP"
else
  say waiting_cluster
  while :; do
    code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$B/join/$N/$S" || echo 000)
    [ "$code" = 200 ] && break
    sleep 15
  done
  say joining "pvecm add $FIRST_IP"
  for i in $(seq 1 30); do
    ssh-keyscan -t ed25519 "$FIRST_IP" "$FIRST_NODE" 2>/dev/null > /tmp/kh && [ -s /tmp/kh ] && break; sleep 5
  done
  cat /tmp/kh >> /root/.ssh/known_hosts
  if ! pvecm status >/dev/null 2>&1; then
    rc=0
    # shellcheck disable=SC2086
    out=$(pvecm add "$FIRST_IP" --link0 "$CLUSTER_IP" $LINK_OPT --use_ssh 2>&1) || rc=$?
    echo "$out"
    if [ "$rc" != 0 ] || echo "$out" | grep -qiE 'unable to|permission denied|^error'; then
      fail "join failed: $(echo "$out" | grep -iE 'unable to|denied|error' | tail -1 | cut -c1-200)"
    fi
  fi
  wait_quorum || fail "no quorum after joining"
  say joined "$(pvecm status 2>/dev/null | grep -E 'Quorate' | tr -s ' ')"
fi
rm -f /root/.ssh/id_ed25519

# --- the Home Assistant VM: created on the first node, copied to every other node each minute
if [ "$VM_ENABLED" = 1 ]; then
  if [ "$IS_FIRST" = 1 ]; then
    if ! qm status "$VMID" >/dev/null 2>&1; then
      say image "$IMAGE_URL"
      IMG=/var/lib/vz/template/cluster-builder-vm.qcow2
      case "$IMAGE_URL" in
        *.xz) curl -fsSL --retry 5 "$IMAGE_URL" | xz -dc > "$IMG" ;;
        *) curl -fsSL --retry 5 "$IMAGE_URL" -o "$IMG" ;;
      esac
      VM_BRIDGE=vmbr0; [ -n "$LAN_IF" ] && VM_BRIDGE=vmbr1
      # shellcheck disable=SC2086
      qm create "$VMID" --name "$VM_NAME" --ostype l26 --memory "$VM_MEMORY" --cores "$VM_CORES" $VM_QM_ARGS \
        --bios ovmf --efidisk0 local-zfs:1,efitype=4m,pre-enrolled-keys=0 --scsihw virtio-scsi-single \
        --scsi0 "local-zfs:0,import-from=$IMG,discard=on,ssd=1" --boot order=scsi0 \
        --net0 "virtio=$VM_MAC,bridge=$VM_BRIDGE" --agent enabled=1 --onboot 1 --tablet 0
      qm resize "$VMID" scsi0 "${VM_DISK_GB}G" || true
      rm -f "$IMG"
    fi
    wait_quorum || true
    ha-manager config 2>/dev/null | grep -q "^vm:$VMID" || ha-manager add "vm:$VMID" --state started --max_restart 1 --max_relocate 1
    ha-manager set "vm:$VMID" --state started
    say vm_created "VM $VMID ($VM_NAME) is a failover resource; MAC $VM_MAC"
    if [ "${MOVE_IN:-0}" = 1 ]; then
      # the add-on restores the backup through the new Home Assistant's Welcome screen; it needs the VM's address.
      # Home Assistant OS runs the QEMU guest agent; until it is up, look the VM's MAC up on the bridge instead.
      say vm_wait "waiting for the Home Assistant VM to get an address"
      VMIP=""; mac_lc=$(echo "$VM_MAC" | tr 'A-F' 'a-f')
      for i in $(seq 1 160); do
        VMIP=$(qm agent "$VMID" network-get-interfaces 2>/dev/null | python3 -c '
import json, sys
for i in json.load(sys.stdin):
    for a in i.get("ip-addresses", []):
        ip = a.get("ip-address", "")
        if a.get("ip-address-type") == "ipv4" and not ip.startswith("127."):
            print(ip); raise SystemExit' 2>/dev/null || true)
        if [ -z "$VMIP" ]; then
          br_ip=$(ip -4 -o addr show "$VM_BRIDGE" | awk '{print $4}' | cut -d/ -f1 | head -1)
          if [ -n "$br_ip" ]; then
            for h in $(seq 1 254); do ping -c1 -W1 "${br_ip%.*}.$h" >/dev/null 2>&1 & done; wait
            VMIP=$(ip neigh show | awk -v m="$mac_lc" 'tolower($0) ~ "lladdr " m {print $1; exit}')
          fi
        fi
        [ -n "$VMIP" ] && break
        sleep 15
      done
      if [ -n "$VMIP" ]; then say vm_ip "$VMIP"; else say vm_wait "the VM got no address in 40 minutes; enter it on the page under Move in"; fi
    fi
  else
    for i in $(seq 1 240); do pvesh get /cluster/resources --type vm --output-format json 2>/dev/null | grep -q "\"vmid\":$VMID" && break; sleep 15; done
    JOB="$VMID-$NODE_INDEX"
    pvesh get /cluster/replication --output-format json 2>/dev/null | grep -q "\"id\":\"$JOB\"" \
      || pvesh create /cluster/replication --id "$JOB" --target "$N" --type local --schedule '*/1' --comment "Cluster Builder"
    say replicating "job $JOB every minute"
  fi
fi

# --- tidy: the builder's key was only for the joins. authorized_keys is cluster-wide once a node is in,
# so it must stay until the last node has joined; the add-on says when, and that node removes it for all.
code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$B/cleanup/$N/$S" || echo 000)
AK=/root/.ssh/authorized_keys
if [ "$code" = 200 ]; then
  if [ -f "$AK" ]; then grep -v ' cluster-builder$' "$AK" > /tmp/ak || true; cat /tmp/ak > "$AK"; rm -f /tmp/ak; fi
  say done "$(hostname) ready; build key removed from the cluster"
else
  say done "$(hostname) ready (the build key stays until the last node has joined)"
fi
