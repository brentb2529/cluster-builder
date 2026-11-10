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
    const set = (name, v) => { const el = form.elements[name]; if (el && el !== document.activeElement) el.value = v ?? ""; };
    for (const k of ["cluster_name", "domain", "gateway", "dns", "prefix", "timezone", "builder_url", "iso_url", "image_url", "disk_filter", "country", "keyboard", "extra_firstboot"]) set(k, s[k]);
    for (const k of ["memory", "cores", "disk_gb", "mac", "qm_args"]) set("vm_" + k, s.vm[k]);
    form.elements.vm_enabled.checked = !!s.vm.enabled;
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
    const body = { nodes, vm: { enabled: f.vm_enabled.checked, memory: +f.vm_memory.value, cores: +f.vm_cores.value, disk_gb: +f.vm_disk_gb.value, qm_args: f.vm_qm_args.value, mac: f.vm_mac.value.trim().toUpperCase() } };
    for (const k of ["cluster_name", "domain", "gateway", "dns", "timezone", "builder_url", "iso_url", "image_url", "disk_filter", "country", "keyboard", "extra_firstboot"]) body[k] = f[k].value.trim();
    body.prefix = +f.prefix.value;
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
  const phaseText = (p) => (state.phases.find((x) => x[0] === p) || [p, p])[1];

  const renderNodes = (s) => {
    const active = new Set(["installing", "hello", "network", "packages", "cluster_created", "waiting_cluster", "joining", "joined", "image", "vm_created", "replicating"]);
    $("#node-cards").innerHTML = s.nodes.map((ns) => {
      const n = s.nodes_state[ns.name] || {};
      const cls = n.phase === "done" ? "done" : n.phase === "failed" ? "failed" : active.has(n.phase) ? "active" : "";
      const m = n.machine;
      const stale = active.has(n.phase) && n.phase !== "installed" && s.now - (n.updated || 0) > 1800;
      return `<div class="node ${cls}">
        <h4><span>${esc(ns.name)} <small class="msg">${esc(ns.ip)}</small></span><small class="msg">${ago(n.updated)}</small></h4>
        <span class="phase">${esc(phaseText(n.phase))}</span>
        <span class="msg">${esc(n.msg)}${stale ? " (no news for a while: check the machine's screen)" : ""}</span>
        ${m ? `<div class="machine">${esc([m.vendor, m.product, m.serial].filter(Boolean).join(" ") || m.macs[0])}</div>` : ""}
        <details><summary class="msg">log</summary><pre>${(n.log || []).slice(-40).map((l) => `${new Date(l[0] * 1000).toLocaleTimeString()} ${esc(l[1])} ${esc(l[2])}`).join("\n")}</pre></details>
        <div class="tools">
          ${n.installed && !n.allow_reinstall ? `<button data-act="allow_reinstall" data-node="${esc(ns.name)}">Allow reinstall</button>` : ""}
          ${m && confirmNode !== ns.name ? `<button data-act="reset" data-node="${esc(ns.name)}">Forget this machine</button>` : ""}
          ${m && confirmNode === ns.name ? `<span class="confirm">Forget the machine assigned to ${esc(ns.name)}? Only if you are reinstalling it.
            <button class="danger" data-act="reset-yes" data-node="${esc(ns.name)}">Yes, forget it</button> <button data-act="reset-no" data-node="${esc(ns.name)}">Cancel</button></span>` : ""}
          <a class="msg" href="api/preview/${esc(ns.name)}" target="_blank" rel="noopener">what it gets</a>
        </div></div>`;
    }).join("");
    document.querySelectorAll("#node-cards button").forEach((b) => b.addEventListener("click", async () => {
      const act = b.dataset.act;
      if (act === "reset") { confirmNode = b.dataset.node; render(state); return; }
      if (act === "reset-no") { confirmNode = null; render(state); return; }
      confirmNode = null;
      await api(`api/node/${b.dataset.node}/${act === "reset-yes" ? "reset" : act}`, {}); refresh();
    }));
  };

  const render = (s) => {
    state = s;
    const st = { ...s.settings, nodes_state: s.nodes, phases: s.phases, now: s.now };
    fill(s.settings);
    renderNodes(st);
    $("#free").textContent = `${(s.free_mb / 1024).toFixed(1)} GB`;
    $("#problems").innerHTML = s.problems.map((p) => `<li>${esc(p)}</li>`).join("");
    const b = s.build;
    $("#build").disabled = b.status === "running" || s.problems.length > 0;
    $("#build").textContent = s.iso_ready ? "Build again" : "Build";
    $("#build-status").textContent = b.status === "running" ? b.step : b.status === "done" ? `Ready (built ${ago(b.built_at)})` : b.status === "error" ? "Failed" : "";
    $("#build-bar").style.width = `${Math.round((b.status === "running" ? b.progress : b.status === "done" ? 1 : 0) * 100)}%`;
    if (b.status === "error") { $("#build-error").textContent = b.error; $("#build-error").hidden = false; }
    const dl = $("#dl");
    dl.classList.toggle("disabled", !s.iso_ready);
    dl.href = s.iso_ready ? `download/${s.iso_name}` : "#";
    $("#iso-url").textContent = s.iso_ready ? `${s.settings.builder_url.replace(/\/$/, "")}/iso/${s.iso_token}/${s.iso_name}` : "(after the build)";
    $("#pve-url").textContent = `https://${s.settings.nodes[0].ip || "<node address>"}:8006`;
  };

  const refresh = async () => { try { render(await api("api/state")); } catch (err) { console.error(err); } };
  refresh();
  setInterval(refresh, 3000);
})();
