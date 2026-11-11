/* Cluster Builder UI: one page, polls api/state every 3 s. All URLs are relative so it works behind Ingress. */
(() => {
  const $ = (s) => document.querySelector(s);
  const form = $("#settings");
  let state = null;
  let dirty = false;

  const api = async (path, body) => {
    const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  };

  const nodeRow = (n, i) => `<tr>
    <td><input name="n${i}_name" value="${n.name}" pattern="[A-Za-z0-9-]+" required></td>
    <td><input name="n${i}_ip" value="${n.ip}" placeholder="192.168.2.1${i + 1}" required></td>
    <td><input name="n${i}_link_ip" value="${n.link_ip}"></td></tr>`;
  const nodeAdv = (n, i) => `<tr>
    <td>${n.name}</td>
    <td><input name="n${i}_cluster_ip" value="${n.cluster_ip}"></td>
    <td><input name="n${i}_lan_mac" value="${n.lan_mac}" class="mono"></td>
    <td><input name="n${i}_link_mac" value="${n.link_mac}" class="mono"></td></tr>`;

  const fill = (s) => {
    if (dirty) return; // the user is editing: no field is touched until Save (or the page is reloaded)
    const set = (name, v) => { const el = form.elements[name]; if (el && el !== document.activeElement) el.value = v ?? ""; };
    for (const k of ["cluster_name", "domain", "gateway", "dns", "prefix", "timezone", "builder_url", "iso_url", "image_url", "disk_filter", "country", "keyboard", "extra_firstboot"]) set(k, s[k]);
    for (const k of ["memory", "cores", "disk_gb", "mac", "qm_args"]) set("vm_" + k, s.vm[k]);
    form.elements.vm_enabled.checked = !!s.vm.enabled;
    form.elements.vm_move_in.checked = !!s.vm.move_in;
    if (!form.contains(document.activeElement)) form.elements.install_method.value = s.install_method || "network";
    if (!form.contains(document.activeElement) || !dirty) {
      $("#nodes-rows").innerHTML = s.nodes.map(nodeRow).join("");
      $("#nodes-adv").innerHTML = s.nodes.map(nodeAdv).join("");
    }
  };

  const read = () => {
    const f = form.elements;
    const nodes = [0, 1, 2].map((i) => ({
      name: f[`n${i}_name`].value, ip: f[`n${i}_ip`].value, link_ip: f[`n${i}_link_ip`].value,
      cluster_ip: f[`n${i}_cluster_ip`].value, lan_mac: f[`n${i}_lan_mac`].value, link_mac: f[`n${i}_link_mac`].value,
    }));
    const body = { nodes, vm: { enabled: f.vm_enabled.checked, move_in: f.vm_move_in.checked, memory: +f.vm_memory.value, cores: +f.vm_cores.value, disk_gb: +f.vm_disk_gb.value, qm_args: f.vm_qm_args.value, mac: f.vm_mac.value.trim().toUpperCase() } };
    for (const k of ["cluster_name", "domain", "gateway", "dns", "timezone", "builder_url", "iso_url", "image_url", "disk_filter", "country", "keyboard", "extra_firstboot"]) body[k] = f[k].value.trim();
    body.prefix = +f.prefix.value;
    body.install_method = f.install_method.value || "network";
    if (f.password.value) body.password = f.password.value;
    return body;
  };

  form.addEventListener("input", () => { dirty = true; });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = form.elements;
    if (f.password.value && f.password.value !== f.password2.value) { $("#save-msg").textContent = "The two passwords differ."; return; }
    try {
      const r = await api("api/settings", read());
      f.password.value = ""; f.password2.value = ""; dirty = false;
      $("#save-msg").textContent = r.problems.length ? "Saved. " + r.problems.join(" ") : "Saved.";
      refresh();
    } catch (err) { $("#save-msg").textContent = "Not saved: " + err.message; }
  });

  $("#build").addEventListener("click", async () => {
    $("#build-error").hidden = true;
    try { await api("api/build", {}); refresh(); } catch (err) { $("#build-error").textContent = err.message; $("#build-error").hidden = false; }
  });
  // Confirmations are in-page (a native confirm() looks like a pop-up from "an embedded page" inside Home Assistant).
  const resetAllAsk = (show) => { $("#reset-all").hidden = show; $("#reset-all-confirm").hidden = !show; };
  $("#reset-all").addEventListener("click", () => resetAllAsk(true));
  $("#reset-all-no").addEventListener("click", () => resetAllAsk(false));
  $("#reset-all-yes").addEventListener("click", async () => { resetAllAsk(false); await api("api/reset", {}); refresh(); });
  let confirmNode = null;  // node whose "Forget this machine" is awaiting a yes

  const ago = (t) => { if (!t) return ""; const s = Math.round(state.now - t); return s < 90 ? `${s} s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`; };
  const esc = (x) => String(x ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const phaseText = (p) => {
    if (p === "installed" && state.settings.install_method === "network") return "Installed, restarting into its first boot";
    return (state.phases.find((x) => x[0] === p) || [p, p])[1];
  };

  // The lists are rebuilt only when their content changes, not on every 3 s poll: a rebuild destroys the buttons,
  // and a click that lands during one goes nowhere. Times ("asked 14 s ago") are refreshed in place.
  const sigs = {};
  const changed = (key, sig) => { if (sigs[key] === sig) return false; sigs[key] = sig; return true; };
  const tickAgo = () => document.querySelectorAll("[data-ago]").forEach((el) => { el.textContent = ago(+el.dataset.ago); });
  const tickMachines = (s) => document.querySelectorAll("#machines [data-mac-info]").forEach((el) => {
    const m = (s.machines || {})[el.dataset.macInfo]; if (!m) return;
    el.querySelector("[data-ago]").dataset.ago = m.last || 0;
    el.querySelector("[data-count]").textContent = `${m.count} time${m.count === 1 ? "" : "s"}`;
  });
  const renderMachines = (s) => {
    const macs = Object.keys(s.machines || {}).sort((a, b) => (s.machines[a].first || 0) - (s.machines[b].first || 0));
    const assigned = s.settings.nodes.filter((n) => n.boot_mac).length;
    const sig = JSON.stringify([macs, macs.map((m) => s.machines[m].from), s.settings.nodes.map((n) => n.name), s.settings.install_method, assigned]);
    if (!changed("machines", sig)) { tickMachines(s); tickAgo(); return; }
    $("#machines").innerHTML = macs.map((mac) => {
      const m = s.machines[mac];
      return `<div class="machine-row" data-mac-info="${esc(mac)}"><span><b class="mono">${esc(mac)}</b> <span class="msg">asked <span data-ago="${m.last || 0}">${esc(ago(m.last))}</span>${m.from ? ` from ${esc(m.from)}` : ""}, <span data-count>${m.count} time${m.count === 1 ? "" : "s"}</span></span></span>
        <span>${s.settings.nodes.map((n) => `<button data-mac="${esc(mac)}" data-node="${esc(n.name)}">This is ${esc(n.name)}</button>`).join(" ")}
        <button data-mac="${esc(mac)}" data-forget="1">Not one of mine</button></span></div>`;
    }).join("") || `<p class="msg">${s.settings.install_method !== "network" ? "" : assigned === 0 ? "No machine has asked to boot yet." : assigned < s.settings.nodes.length ? "Waiting for the other machines to ask." : ""}</p>`;
  };
  $("#machines").addEventListener("click", async (ev) => {
    const b = ev.target.closest("button"); if (!b || !b.dataset.mac) return;
    try {
      if (b.dataset.forget) await api(`api/machine/${encodeURIComponent(b.dataset.mac)}/forget`, {});
      else await api(`api/machine/${encodeURIComponent(b.dataset.mac)}/assign`, { node: b.dataset.node });
      refresh();
    } catch (err) { $("#build-error").textContent = err.message; $("#build-error").hidden = false; }
  });

  const renderNodes = (s) => {
    const active = new Set(["netboot", "installing", "hello", "network", "packages", "cluster_created", "waiting_cluster", "joining", "joined", "image", "vm_created", "replicating"]);
    const stuckKind = (n) => {
      const quiet = s.now - (n.updated || 0);
      if (!active.has(n.phase) || n.phase === "installed") return "";
      return n.phase === "netboot" && quiet > 300 ? "netboot" : n.phase === "installing" && quiet > 600 ? "installing" : quiet > 1800 ? "quiet" : "";
    };
    const sig = JSON.stringify([confirmNode, s.nodes.map((ns) => { const n = s.nodes_state[ns.name] || {}; return [ns.name, ns.ip, ns.boot_mac, n.phase, n.msg, n.machine, n.installed, n.allow_reinstall, (n.log || []).length, stuckKind(n)]; })]);
    if (!changed("nodes", sig)) { tickAgo(); return; }
    $("#node-cards").innerHTML = s.nodes.map((ns) => {
      const n = s.nodes_state[ns.name] || {};
      const cls = n.phase === "done" ? "done" : n.phase === "failed" ? "failed" : active.has(n.phase) ? "active" : "";
      const m = n.machine;
      // a machine that fails stays silent (the installer only reports success, a crashed box reports nothing):
      // say what probably happened, by phase, once it has been quiet too long
      const when = `<span data-ago="${n.updated || 0}">${esc(ago(n.updated))}</span>`;
      const kind = stuckKind(n);
      const stuck = kind === "netboot" ? `It took the installer ${when} and never asked for its answers. Look at its screen: a kernel panic or a restart loop means the box has too little memory for the network install (8 GB needed); a normal Proxmox login means it started from its disk instead (put network boot first in its firmware). Fix that and turn it on again: it gets the installer again by itself.` :
        kind === "installing" ? `The installer stopped reporting ${when}. Look at the machine's screen for the error; after fixing it, turn the box on again and it installs again.` :
        kind === "quiet" ? `No news ${when}: check the machine's screen.` : "";
      const steps = state.phases.filter((p) => !["waiting", "failed"].includes(p[0]));
      const stepNo = n.phase === "done" ? steps.length : Math.max(0, steps.findIndex((p) => p[0] === n.phase) + 1);
      const pct = n.phase === "failed" ? 100 : Math.round((stepNo / steps.length) * 100);
      return `<div class="node ${cls} ${stuck ? "stuck" : ""}">
        <h4><span>${esc(ns.name)} <small class="msg">${esc(ns.ip)}</small></span><small class="msg" data-ago="${n.updated || 0}">${esc(ago(n.updated))}</small></h4>
        <span class="phase">${esc(phaseText(n.phase))}</span>
        ${stepNo ? `<div class="bar" title="step ${stepNo} of ${steps.length}"><i style="width:${pct}%"></i></div><small class="msg">step ${stepNo} of ${steps.length}</small>` : ""}
        <span class="msg">${esc(n.msg)}</span>${stuck ? `<p class="error">${stuck}</p>` : ""}
        ${m ? `<div class="machine">${esc([m.vendor, m.product, m.serial].filter(Boolean).join(" ") || m.macs[0])}</div>` : ns.boot_mac ? `<div class="machine">boots from the network as <span class="mono">${esc(ns.boot_mac)}</span></div>` : ""}
        <details><summary class="msg">log</summary><pre>${(n.log || []).slice(-40).map((l) => `${new Date(l[0] * 1000).toLocaleTimeString()} ${esc(l[1])} ${esc(l[2])}`).join("\n")}</pre></details>
        <div class="tools">
          ${n.installed && !n.allow_reinstall ? `<button data-act="allow_reinstall" data-node="${esc(ns.name)}">Allow reinstall</button>` : ""}
          ${m && confirmNode !== ns.name ? `<button data-act="reset" data-node="${esc(ns.name)}">Forget this machine</button>` : ""}
          ${m && confirmNode === ns.name ? `<span class="confirm">Forget the machine assigned to ${esc(ns.name)}? Only if you are reinstalling it.
            <button class="danger" data-act="reset-yes" data-node="${esc(ns.name)}">Yes, forget it</button> <button data-act="reset-no" data-node="${esc(ns.name)}">Cancel</button></span>` : ""}
          <a class="msg" href="api/preview/${esc(ns.name)}" target="_blank" rel="noopener">what it gets</a>
        </div></div>`;
    }).join("");
  };
  $("#node-cards").addEventListener("click", async (ev) => {
    const b = ev.target.closest("button"); if (!b || !b.dataset.act) return;
    const act = b.dataset.act;
    if (act === "reset") { confirmNode = b.dataset.node; render(state); return; }
    if (act === "reset-no") { confirmNode = null; render(state); return; }
    confirmNode = null;
    await api(`api/node/${b.dataset.node}/${act === "reset-yes" ? "reset" : act}`, {}); refresh();
  });

  const render = (s) => {
    state = s;
    const st = { ...s.settings, nodes_state: s.nodes, phases: s.phases, now: s.now };
    fill(s.settings);
    renderNodes(st);
    const net = s.settings.install_method === "network";
    $("#step3-net").hidden = !net;
    $("#step3").hidden = net;
    $("#net-howto").hidden = !net;
    $("#stick-howto").hidden = net;
    if (net) renderMachines(s);
    const host = s.host || {};
    const va = s.vm_auto || {};
    $("#vm-disk-auto").textContent = va.host_disk_used_gb ? `${va.disk_gb} GB (twice the ${va.host_disk_used_gb} GB this Home Assistant uses now)` : `${va.disk_gb || 32} GB`;
    $("#host-where").textContent = host.ip ? `now at ${host.ip}${host.mac ? ", MAC " + host.mac : ""}` : "this machine";
    $("#net-server").textContent = (s.settings.builder_url.match(/\/\/([^/:]+)/) || [])[1] || "<this Home Assistant's address>";
    $("#tftp-error").hidden = !(net && s.tftp_error);
    $("#tftp-error").textContent = s.tftp_error ? `The boot-loader port (UDP 69) could not be opened: ${s.tftp_error}. Network boot will not work until the add-on can use it; the stick still does.` : "";
    $("#dhcp-error").hidden = !(net && s.dhcp_error);
    $("#dhcp-error").textContent = s.dhcp_error ? `The add-on cannot answer network-boot requests itself (UDP 67/4011: ${s.dhcp_error}). Set your router's network-boot (PXE) option to server ${$("#net-server").textContent}, file ipxe.efi instead (UniFi: Settings > Networks > your network > DHCP > Network Boot; other routers call these DHCP options 66 and 67).` : "";
    $("#free").textContent = `${(s.free_mb / 1024).toFixed(1)} GB`;
    $("#problems").innerHTML = s.problems.map((p) => `<li>${esc(p)}</li>`).join("");
    const b = s.build;
    $("#build").disabled = b.status === "running" || s.problems.length > 0;
    $("#build").textContent = s.iso_ready ? "Build again" : "Build";
    $("#build-status").textContent = b.status === "running" ? b.step : b.status === "done" ? `Ready (built ${ago(b.built_at)})${s.settings.install_method === "network" && !s.netboot_ready ? " for a stick only: Build again for network boot" : ""}` : b.status === "error" ? "Failed" : "";
    $("#build-bar").style.width = `${Math.round((b.status === "running" ? b.progress : b.status === "done" ? 1 : 0) * 100)}%`;
    if (b.status === "error") { $("#build-error").textContent = b.error; $("#build-error").hidden = false; }
    const dl = $("#dl");
    dl.classList.toggle("disabled", !s.iso_ready);
    dl.href = s.iso_ready ? `download/${s.iso_name}` : "#";
    $("#iso-url").textContent = s.iso_ready ? `${s.settings.builder_url.replace(/\/$/, "")}/iso/${s.iso_token}/${s.iso_name}` : "(after the build)";
    $("#ssh-public").textContent = s.ssh_public || "";
    $("#pve-url").textContent = `https://${s.settings.nodes[0].ip || "<node address>"}:8006`;
    form.elements.vm_move_in.disabled = !s.supervisor;
    $("#movein-hint").hidden = !s.supervisor;
    $("#cg-missing").hidden = !(s.supervisor && s.settings.vm.move_in && s.cluster_guard_installed === false);
    const mi = s.move_in || {};
    const moving = !!(s.settings.vm.move_in && s.supervisor);
    $("#movein").hidden = !moving;
    $("#movein-manual").hidden = moving;
    if (moving) {
      const text = (s.move_in_phases.find((x) => x[0] === (mi.phase || "")) || ["", mi.phase])[1];
      $("#movein-phase").textContent = mi.phase ? text : (s.cluster.vm_created ? "Waiting for the first node to report the VM's address" : "Starts by itself once the first node has created the VM");
      $("#movein-msg").textContent = mi.msg || "";
      $("#movein-done").hidden = mi.phase !== "done";
      const cg = s.cluster_guard || {};
      const cgText = ((s.cluster_guard_phases || []).find((x) => x[0] === (cg.phase || "")) || ["", cg.phase])[1];
      $("#cg-line").textContent = cg.phase ? `${cgText}: ${cg.msg || ""}` : "";
      $("#movein-cg").textContent = cg.phase === "done" ? ", and Cluster Guard watches the cluster from it" : "";
      $("#movein-cg-manual").textContent = cg.phase === "done" ? "" : " Add the Cluster Guard integration there with a node's address and the root password, once.";
      $("#movein-url").textContent = mi.url || (mi.ip ? (mi.ip.includes(":") ? `http://${mi.ip}` : `http://${mi.ip}:8123`) : "");
      $("#movein-retry").hidden = !(mi.phase === "failed" || (s.cluster.vm_created && !mi.phase));
      if (document.activeElement !== $("#movein-ip") && !$("#movein-ip").value) $("#movein-ip").value = mi.ip || "";
    }
  };
  $("#movein-go").addEventListener("click", async () => {
    try { await api("api/move_in", { ip: $("#movein-ip").value.trim() }); refresh(); } catch (err) { $("#movein-msg").textContent = err.message; }
  });

  const refresh = async () => { try { render(await api("api/state")); } catch (err) { console.error(err); } };
  refresh();
  setInterval(refresh, 3000);
})();
