/* Nodes tab: CANopen scan, set node ID, bit rate, and the custom sequence mode for unknown devices.
   Every write is a plan: preview (exact frames) -> tick "checked" -> run. Everything is logged (logs/nodes.log). */
"use strict";
(function () {
  const CO = window.canopener;
  let tabEl = null, scan = null, channel = 0, jobLines = [], jobId = null;
  let steps = CO.prefs.get("nodes.steps", []);
  let vars = CO.prefs.get("nodes.vars", { OLD_ID: "", NEW_ID: "", BAUD: "" });
  const KBIT = [1000, 800, 500, 250, 125, 100, 50, 20, 10];
  const TYPES = ["u8", "u16", "u32", "i8", "i16", "i32", "u64", "i64", "f32", "str", "bytes"];

  const hex8 = (v) => (v === null || v === undefined ? "-" : v);
  const saveEditor = () => { CO.prefs.set("nodes.steps", steps); CO.prefs.set("nodes.vars", vars); };

  // ---------------------------------------------------------------- plan dialog
  CO.showPlan = async (plan) => {
    const m = plan.meta || {};
    const stepsHtml = plan.preview.map((p, i) => `<div class="plan-step"><b>${i + 1}.</b> ${CO.esc(p.text)}
      ${p.flags.length ? p.flags.map((f) => `<span class="badge ${f === "always" ? "accent" : ""}">${f}</span>`).join(" ") : ""}
      ${p.note ? `<span class="muted small">- ${CO.esc(p.note)}</span>` : ""}
      ${p.frames.length ? `<div class="frames">${p.frames.map((f) => `<div>0x${f.id.toString(16).toUpperCase().padStart(3, "0")}&nbsp;&nbsp;${CO.esc(f.data || "(no data)")}${f.note && f.note !== "raw" ? ` <span class="muted">(${CO.esc(f.note)})</span>` : ""}</div>`).join("")}</div>` : ""}
      ${p.expect ? `<div class="small muted">expects: ${CO.esc(p.expect)}</div>` : ""}</div>`).join("");
    const res = await CO.modal({
      title: `Preview: ${plan.title}`, wide: true,
      body: `<div class="kv"><span>Channel</span><span>${CO.esc(CO.chName(plan.channel))}</span>
        ${m.recipe ? `<span>Recipe</span><span>${CO.esc(m.recipe)} (${CO.esc(m.method || "")})</span>` : m.method ? `<span>Method</span><span>${CO.esc(m.method)}</span>` : ""}
        ${m.source ? `<span>Source</span><span class="small">${CO.esc(m.source)}</span>` : ""}
        <span>Frames</span><span>${plan.frame_count} to send (plus replies)</span></div>
        ${m.note ? `<div class="notice small">${CO.esc(m.note)}</div>` : ""}
        ${plan.warnings.map((w) => `<div class="notice warn">${CO.esc(w)}</div>`).join("")}
        <div>${stepsHtml}</div>
        <p class="small muted">Steps marked <span class="badge accent">always</span> also run after a failure (cleanup). The whole run is logged to logs\\nodes.log.</p>
        <label class="row"><input type="checkbox" id="pl-ok"> I checked the frames above and want to send them</label>`,
      buttons: [{ label: "Cancel", value: "cancel" }, { label: "Run", value: "run", cls: "danger solid" }],
      onOpen: (f) => { const b = CO.$('button[value="run"]', f); b.disabled = true; CO.$("#pl-ok", f).onchange = (e) => (b.disabled = !e.target.checked); },
    });
    if (!res) return null;
    const job = await CO.tryApi("/api/nodes/run", { token: plan.token, title: plan.title });
    if (job) { renderJob(job); CO.show("nodes"); }
    return job;
  };

  async function previewAndRun(path, body) {
    const plan = await CO.tryApi(path, { channel, ...body });
    if (plan) await CO.showPlan(plan);
  }

  // ---------------------------------------------------------------- scan
  async function doScan() {
    const secs = +CO.$("#nd-secs", tabEl).value || 2.5;
    const job = await CO.tryApi("/api/nodes/scan", { channel, seconds: secs });
    if (job) renderJob(job);
  }

  function renderScan() {
    if (!tabEl) return;
    const el = CO.$("#nd-nodes", tabEl);
    if (!scan || !scan.nodes) { el.innerHTML = `<div class="empty">Scan to list the CANopen nodes on this channel.</div>`; CO.$("#nd-other", tabEl).innerHTML = ""; return; }
    el.innerHTML = scan.nodes.length ? `<table class="tbl"><thead><tr><th class="r">ID</th><th>Name</th><th>Vendor</th><th>Product</th><th>Revision</th><th>Serial</th><th>Seen by</th><th>NMT</th><th>Recipe</th><th></th></tr></thead><tbody>` +
      scan.nodes.map((n) => `<tr data-node="${n.node_id}"><td class="r m"><b>${n.node_id}</b></td><td>${CO.esc(n.device_name || "-")}</td>
        <td class="m">${hex8(n.vendor_id)}</td><td class="m">${hex8(n.product_code)}</td><td class="m">${hex8(n.revision_number)}</td><td class="m">${hex8(n.serial_number)}</td>
        <td>${CO.esc((n.seen_by || []).join(", "))}</td><td>${n.nmt_state === null || n.nmt_state === undefined ? "-" : ({ 0: "boot", 4: "stopped", 5: "operational", 127: "pre-op" }[n.nmt_state] || n.nmt_state)}</td>
        <td title="${CO.esc(n.recipe_note || "")}">${CO.esc(n.recipe || "")} <span class="muted small">${CO.esc(n.recipe_method || "")}</span></td>
        <td class="r"><button class="btn small" data-act="setid" ${n.recipe_method === "readonly" ? "disabled title=\"read-only device\"" : ""}>Set ID&hellip;</button>
          <button class="btn small" data-act="baud" ${n.baud === "none" ? "disabled title=\"no bit-rate recipe; use Custom\"" : ""}>Bit rate&hellip;</button>
          <button class="btn small ghost" data-act="custom" title="Use this node in the custom sequence editor">Custom</button></td></tr>`).join("") + `</tbody></table>`
      : `<div class="empty">No CANopen node answered. ${scan.raw_ids.length ? "There is other traffic on the bus (below)." : "No traffic at all - check wiring, termination and bit rate."}</div>`;
    CO.$("#nd-other", tabEl).innerHTML = `<div class="small muted" style="margin-top:6px">
      ${scan.raw_ids.length} raw 11-bit id(s) seen${scan.raw_ext_ids.length ? `, ${scan.raw_ext_ids.length} extended id(s)` : ""}.
      ${scan.pdo_only_ids.length ? `PDO-range traffic without a CANopen node behind it: ${scan.pdo_only_ids.join(", ")}.` : ""}
      <details><summary>raw ids</summary><span class="mono">${scan.raw_ids.join(" ")} ${scan.raw_ext_ids.join(" ")}</span></details></div>`;
  }

  async function setIdDialog(node) {
    const n = scan.nodes.find((x) => x.node_id === node);
    const rec = await CO.api("/api/nodes/recipes");
    const names = rec.recipes.filter((r) => r.method !== "readonly" && r.key !== "default").map((r) => r.name || r.key);
    const res = await CO.modal({ title: `Set node ID of node ${node}`, body: `
      <div class="small muted">${CO.esc(n.device_name || "")} vendor ${CO.esc(n.vendor_id || "?")} - recipe <b>${CO.esc(n.recipe)}</b> (${CO.esc(n.recipe_method)})</div>
      ${n.recipe_note ? `<div class="notice small">${CO.esc(n.recipe_note)}</div>` : ""}
      <div class="grid3"><label class="field"><span>New node ID (1..127)</span><input id="s-new" type="number" min="1" max="127" value="${node === 127 ? 1 : node + 1}"></label>
      <label class="field"><span>Method</span><select id="s-meth"><option value="">automatic (by vendor id)</option><option value="lss">LSS (CiA 305)</option>
        ${names.map((x) => `<option value="sdo:${CO.esc(x)}">recipe ${CO.esc(x)}</option>`).join("")}</select></label>
      <label class="field"><span>Verify timeout s</span><input id="s-ver" type="number" min="0.5" step="0.5" value="5"></label></div>
      <p class="small muted">Next you see every frame that will be sent, and the conflict check against all ids seen on the bus.</p>`,
      buttons: [{ label: "Cancel", value: "cancel" }, { label: "Preview", value: "ok", cls: "primary" }] });
    if (!res) return;
    const f = res.form;
    await previewAndRun("/api/nodes/plan/set_id", { old: node, new: +CO.$("#s-new", f).value, method: CO.$("#s-meth", f).value || null, verify_timeout: +CO.$("#s-ver", f).value });
  }

  async function baudDialog(node) {
    const ch = CO.state.channels.find((c) => c.index === channel);
    const cur = ch ? ch.config.bitrate / 1000 : 250;
    const res = await CO.modal({ title: node === null ? "Change the bit rate of every LSS device" : `Change the bit rate of node ${node}`, body: `
      <div class="grid3"><label class="field"><span>New bit rate</span><select id="b-k">${KBIT.map((k) => `<option value="${k}" ${k === (cur === 250 ? 500 : 250) ? "selected" : ""}>${k} kbit/s</option>`).join("")}</select></label>
      <label class="field"><span>LSS switch delay ms</span><input id="b-d" type="number" min="10" value="500"></label>
      <label class="field"><span>Verify timeout s</span><input id="b-v" type="number" min="0.5" step="0.5" value="5"></label></div>
      <p class="small muted">The bus runs at ${cur} kbit/s now. After the change the adapter reopens at the new rate and waits for traffic.</p>`,
      buttons: [{ label: "Cancel", value: "cancel" }, { label: "Preview", value: "ok", cls: "primary" }] });
    if (!res) return;
    const f = res.form;
    await previewAndRun("/api/nodes/plan/baud", { node, kbit: +CO.$("#b-k", f).value, switch_delay_ms: +CO.$("#b-d", f).value, verify_timeout: +CO.$("#b-v", f).value });
  }

  // ---------------------------------------------------------------- job log
  function renderJob(job) {
    if (!tabEl || !job) return;
    if (job.id !== jobId) { jobId = job.id; jobLines = []; }
    // job.lines are lines[start:]; placing them by index makes repeats (a websocket reconnect resends from 0,
    // the POST reply and the feed overlap) harmless
    const lines = job.lines || [], start = (job.line_count ?? lines.length) - lines.length;
    jobLines = jobLines.slice(0, start).concat(lines);
    const el = CO.$("#nd-job", tabEl);
    el.hidden = false;
    const state = job.state === "running" ? `<span class="badge accent">running</span>` : job.state === "done" ? `<span class="badge good">done</span>` : `<span class="badge bad">failed</span>`;
    CO.$("#nd-job-head", tabEl).innerHTML = `${CO.esc(job.title)} ${state} ${job.error ? `<span class="small" style="color:var(--bad)">${CO.esc(job.error)}</span>` : ""}`;
    CO.$("#nd-stop", tabEl).hidden = job.state !== "running";
    const log = CO.$("#nd-joblog", tabEl);
    log.textContent = jobLines.join("\n");
    log.scrollTop = log.scrollHeight;
    if (job.state !== "running" && job.result) {
      const r = job.result;
      if (job.kind === "scan" && r.nodes) { scan = r; renderScan(); }
      else if (r.message) CO.$("#nd-result", tabEl).innerHTML = `<div class="notice ${r.verified || r.power_cycle_required || r.ok ? "good" : "warn"}">${CO.esc(r.message)}</div>`;
      else if (r.steps) CO.$("#nd-result", tabEl).innerHTML = `<div class="notice ${r.ok ? "good" : "bad"}">${r.ok ? "Sequence done" : CO.esc(r.error)}</div>` +
        `<table class="tbl compact">${r.steps.map((s) => `<tr><td>${s.index}</td><td>${CO.esc(s.text)}</td><td><span class="badge ${s.status === "ok" ? "good" : s.status === "failed" ? "bad" : s.status === "warn" ? "warn" : ""}">${s.status}</span></td><td class="m">${CO.esc(s.detail)}</td></tr>`).join("")}</table>`;
    } else if (job.state === "running") CO.$("#nd-result", tabEl).innerHTML = "";
  }

  // ---------------------------------------------------------------- custom sequence editor
  const TEMPLATES = {
    "sdo-id": { title: "SDO: write node id, store, reset", vars: { OLD_ID: "", NEW_ID: "" }, steps: [
      { op: "sdo_write", node: "$OLD_ID", index: "0x2100", sub: 0, type: "u8", value: "$NEW_ID", note: "set the device's node-id object (check its manual/EDS)" },
      { op: "store", node: "$OLD_ID", target: "0x1010:01" },
      { op: "nmt", command: "reset_node", node: "$OLD_ID" },
      { op: "wait_node", node: "$NEW_ID", timeout_s: 5, optional: true }] },
    "lss-global-id": { title: "LSS: give the only device on the bus a node id", vars: { NEW_ID: "" }, steps: [
      { op: "lss", command: "switch_global", state: "configuration", note: "only ONE LSS device may be connected" },
      { op: "lss", command: "configure_node_id", value: "$NEW_ID" },
      { op: "lss", command: "store" },
      { op: "lss", command: "switch_global", state: "waiting", always: true },
      { op: "nmt", command: "reset_comm", node: 0 },
      { op: "wait_node", node: "$NEW_ID", timeout_s: 5, optional: true }] },
    "lss-baud": { title: "LSS: bit rate of every LSS device", vars: { BAUD: "" }, steps: [
      { op: "lss", command: "switch_global", state: "configuration" },
      { op: "lss", command: "configure_bit_timing", kbit: "$BAUD" },
      { op: "lss", command: "store" },
      { op: "lss", command: "activate_bit_timing", delay_ms: 500 },
      { op: "delay", ms: 1200, nostop: true },
      { op: "reopen", kbit: "$BAUD", nostop: true },
      { op: "lss", command: "switch_global", state: "waiting", always: true },
      { op: "wait_traffic", timeout_s: 5, optional: true }] },
    "identity": { title: "Read identity and name", vars: { OLD_ID: "" }, steps: [
      { op: "sdo_read", node: "$OLD_ID", index: "0x1018", sub: 1, type: "u32" }, { op: "sdo_read", node: "$OLD_ID", index: "0x1018", sub: 2, type: "u32" },
      { op: "sdo_read", node: "$OLD_ID", index: "0x1018", sub: 3, type: "u32" }, { op: "sdo_read", node: "$OLD_ID", index: "0x1018", sub: 4, type: "u32" },
      { op: "sdo_read", node: "$OLD_ID", index: "0x1008", sub: 0, type: "str" }] },
    "nmt-start": { title: "NMT: start all nodes", vars: {}, steps: [{ op: "nmt", command: "start", node: 0 }] },
    "nmt-preop": { title: "NMT: all nodes to pre-operational", vars: {}, steps: [{ op: "nmt", command: "preop", node: 0 }] },
  };
  const FIELDS = {
    sdo_write: [["node", "node", "$OLD_ID"], ["index", "index", "0x2100"], ["sub", "sub", 0], ["type", "type", "u8"], ["value", "value", "$NEW_ID", "wide"]],
    sdo_read: [["node", "node", "$OLD_ID"], ["index", "index", "0x1000"], ["sub", "sub", 0], ["type", "type", "u32"], ["expect", "expect (opt.)", ""]],
    store: [["node", "node", "$OLD_ID"], ["target", "target", "0x1010:01"]],
    nmt: [["command", "command", "reset_node"], ["node", "node (0 = all)", 0]],
    raw: [["id", "id (hex)", "0x000"], ["data", "data", "", "wide"], ["ext", "ext", false], ["fd", "FD", false]],
    lss: [["command", "command", "switch_global"]],
    delay: [["ms", "ms", 100]],
    reopen: [["kbit", "kbit/s", "$BAUD"]],
    wait_node: [["node", "node", "$NEW_ID"], ["timeout_s", "timeout s", 5]],
    wait_traffic: [["timeout_s", "timeout s", 5]],
  };
  const LSS_FIELDS = { switch_global: [["state", "state", "waiting"]], switch_selective: [["vendor", "vendor", "$VENDOR"], ["product", "product", "$PRODUCT"], ["revision", "revision", "$REVISION"], ["serial", "serial", "$SERIAL"]],
    configure_node_id: [["value", "node id", "$NEW_ID"]], configure_bit_timing: [["kbit", "kbit/s", "$BAUD"]], activate_bit_timing: [["delay_ms", "delay ms", 500]], store: [], inquire_node_id: [] };

  function fieldInput(i, key, label, dflt, cls, step) {
    const v = step[key] ?? dflt;
    if (key === "type") return `<select data-i="${i}" data-k="type" title="${label}">${TYPES.map((t) => `<option ${t === v ? "selected" : ""}>${t}</option>`).join("")}</select>`;
    if (key === "command" && step.op === "nmt") return `<select data-i="${i}" data-k="command">${["start", "stop", "preop", "reset_node", "reset_comm"].map((t) => `<option ${t === v ? "selected" : ""}>${t}</option>`).join("")}</select>`;
    if (key === "command" && step.op === "lss") return `<select data-i="${i}" data-k="command">${Object.keys(LSS_FIELDS).map((t) => `<option ${t === v ? "selected" : ""}>${t}</option>`).join("")}</select>`;
    if (key === "state") return `<select data-i="${i}" data-k="state">${["waiting", "configuration"].map((t) => `<option ${t === v ? "selected" : ""}>${t}</option>`).join("")}</select>`;
    if (typeof dflt === "boolean") return `<label class="row small"><input type="checkbox" data-i="${i}" data-k="${key}" ${v ? "checked" : ""}>${label}</label>`;
    return `<input data-i="${i}" data-k="${key}" class="m ${cls || ""}" placeholder="${CO.esc(label)}" title="${CO.esc(label)}" value="${CO.esc(v)}">`;
  }

  function renderEditor() {
    if (!tabEl) return;
    const varsEl = CO.$("#cs-vars", tabEl);
    varsEl.innerHTML = Object.entries(vars).map(([k, v]) => `<label class="field"><span>$${CO.esc(k)}</span><input data-var="${CO.esc(k)}" class="m" style="width:90px" value="${CO.esc(v)}"></label>`).join("") +
      `<button type="button" class="btn small" id="cs-addvar" style="align-self:flex-end">+ variable</button>`;
    const list = CO.$("#cs-steps", tabEl);
    list.innerHTML = steps.length ? steps.map((s, i) => {
      let fields = (FIELDS[s.op] || []).map(([k, l, d, c]) => fieldInput(i, k, l, d, c, s)).join("");
      if (s.op === "lss") fields += (LSS_FIELDS[s.command || "switch_global"] || []).map(([k, l, d]) => fieldInput(i, k, l, d, "", s)).join("");
      return `<div class="step"><span class="muted">${i + 1}.</span>
        <select data-i="${i}" data-k="op">${Object.keys(FIELDS).map((o) => `<option ${o === s.op ? "selected" : ""}>${o}</option>`).join("")}</select>
        <div class="args">${fields}
          <label class="row small" title="Also run after a failure (cleanup)"><input type="checkbox" data-i="${i}" data-k="always" ${s.always ? "checked" : ""}>always</label>
          <label class="row small" title="A failure is only a warning"><input type="checkbox" data-i="${i}" data-k="optional" ${s.optional ? "checked" : ""}>optional</label></div>
        <span><button type="button" class="btn small ghost" data-mv="${i}:-1" title="Up">&uarr;</button><button type="button" class="btn small ghost" data-mv="${i}:1" title="Down">&darr;</button><button type="button" class="btn small ghost" data-del="${i}" title="Remove">&times;</button></span></div>`;
    }).join("") : `<div class="empty">No steps. Pick a template or add steps.</div>`;
  }

  function editorEvents() {
    const root = CO.$("#cs", tabEl);
    root.addEventListener("input", (ev) => {
      const t = ev.target;
      if (t.dataset.var !== undefined) { vars[t.dataset.var] = t.value; saveEditor(); return; }
      if (t.dataset.i === undefined || t.tagName === "SELECT" || t.type === "checkbox") return;
      const s = steps[+t.dataset.i], k = t.dataset.k;
      s[k] = /^-?\d+$/.test(t.value) && !["data", "value", "expect", "id", "index"].includes(k) ? +t.value : t.value;
      saveEditor();
    });
    root.addEventListener("change", (ev) => {
      const t = ev.target;
      if (t.dataset.i === undefined) return;
      const s = steps[+t.dataset.i], k = t.dataset.k;
      if (t.type === "checkbox") { if (t.checked) s[k] = true; else delete s[k]; }
      else if (t.tagName === "SELECT") {
        if (k === "op") { const keep = { op: t.value }; (FIELDS[t.value] || []).forEach(([fk, , d]) => (keep[fk] = d)); steps[+t.dataset.i] = keep; }
        else s[k] = t.value;
        renderEditor();
      }
      saveEditor();
    });
    root.addEventListener("click", (ev) => {
      const mv = ev.target.closest("[data-mv]"), del = ev.target.closest("[data-del]");
      if (mv) { const [i, d] = mv.dataset.mv.split(":").map(Number); const j = i + d; if (j >= 0 && j < steps.length) { [steps[i], steps[j]] = [steps[j], steps[i]]; saveEditor(); renderEditor(); } }
      if (del) { steps.splice(+del.dataset.del, 1); saveEditor(); renderEditor(); }
      if (ev.target.id === "cs-addvar") {
        const name = (prompt("Variable name (letters, e.g. INDEX)") || "").toUpperCase().replace(/[^A-Z0-9_]/g, "");
        if (name) { vars[name] = ""; saveEditor(); renderEditor(); }
      }
    });
  }

  function useNode(node) {
    const n = scan && scan.nodes.find((x) => x.node_id === node);
    vars.OLD_ID = node;
    if (n) { for (const [k, f] of [["VENDOR", "vendor_id"], ["PRODUCT", "product_code"], ["REVISION", "revision_number"], ["SERIAL", "serial_number"]]) if (n[f]) vars[k] = n[f]; }
    saveEditor(); renderEditor();
    CO.$("#cs", tabEl).scrollIntoView({ behavior: "smooth" });
    CO.toast(`$OLD_ID = ${node}` + (n && n.vendor_id ? `, identity filled in` : ""), "good");
  }

  async function saveRecipe() {
    if (!steps.length) { CO.toast("Nothing to save", "warn"); return; }
    const node = scan && scan.nodes.find((x) => x.node_id === +vars.OLD_ID);
    const res = await CO.modal({ title: "Save as recipe", body: `
      <p class="small muted">The sequence becomes the recipe for every device with this vendor id (0x1018:01), stored in the user recipes file. Set ID / Bit rate then use it automatically. Keep $OLD_ID / $NEW_ID / $BAUD placeholders in the steps.</p>
      <div class="grid2"><label class="field"><span>Vendor id (hex)</span><input id="r-v" class="m" value="${CO.esc(node ? node.vendor_id : vars.VENDOR || "")}"></label>
      <label class="field"><span>Name</span><input id="r-n" value="${CO.esc(node && node.device_name ? node.device_name.toLowerCase().replace(/\W+/g, "-") : "")}"></label>
      <label class="field"><span>Used for</span><select id="r-k"><option value="set_id">changing the node ID</option><option value="baud">changing the bit rate</option></select></label>
      <label class="field"><span>New id takes effect</span><select id="r-e"><option value="immediate">immediately / after the reset in the steps</option><option value="power_cycle">after a power cycle</option></select></label></div>
      <label class="field"><span>Note</span><input id="r-note" placeholder="where the mechanism comes from (manual section, EDS, trace)"></label>`,
      buttons: [{ label: "Cancel", value: "cancel" }, { label: "Save recipe", value: "ok", cls: "primary" }] });
    if (!res) return;
    const f = res.form;
    const r = await CO.tryApi("/api/nodes/recipes/save", { vendor_id: CO.$("#r-v", f).value, name: CO.$("#r-n", f).value.trim(), kind: CO.$("#r-k", f).value,
      effect: CO.$("#r-e", f).value, steps, note: CO.$("#r-note", f).value });
    if (r) CO.toast(`Saved to ${r.path}`, "good");
  }

  // ---------------------------------------------------------------- tab
  CO.registerTab({
    id: "nodes", title: "Nodes (CANopen)", order: 50,
    render(el) {
      tabEl = el;
      el.innerHTML = `<div class="page">
        <div class="row"><span>Channel</span><span id="nd-chsel"></span>
          <label class="row small">listen <input id="nd-secs" type="number" min="0.5" step="0.5" value="2.5" style="width:60px"> s</label>
          <button class="btn primary" id="nd-scan">Scan</button>
          <button class="btn" id="nd-allbaud" title="LSS: switch every LSS device to another bit rate">Bit rate of all LSS devices&hellip;</button>
          <span class="spacer"></span><button class="btn ghost" id="nd-log">Session log&hellip;</button></div>
        <div class="card" style="margin-top:10px"><div id="nd-nodes"></div><div id="nd-other"></div></div>
        <div class="card" id="nd-job" style="margin-top:10px" hidden><div class="row"><b id="nd-job-head"></b><span class="spacer"></span><button class="btn small danger" id="nd-stop">Stop</button></div>
          <div class="joblog" id="nd-joblog" style="margin-top:6px"></div><div id="nd-result" style="margin-top:6px"></div></div>
        <div class="card" id="cs" style="margin-top:10px"><div class="row"><h3 style="margin:0">Custom sequence</h3><span class="muted small">for devices without a recipe: SDO steps, raw frames, raw LSS, delays. Preview shows every frame.</span></div>
          <div class="row" style="margin-top:8px"><select id="cs-tpl"><option value="">Start from a template&hellip;</option>${Object.entries(TEMPLATES).map(([k, t]) => `<option value="${k}">${CO.esc(t.title)}</option>`).join("")}</select>
            <button class="btn small" id="cs-add">+ step</button><button class="btn small" id="cs-clear">Clear</button>
            <span class="spacer"></span><button class="btn small" id="cs-export">Export JSON</button><label class="btn small">Import JSON<input type="file" id="cs-import" accept=".json" hidden></label></div>
          <div class="row" id="cs-vars" style="margin-top:8px"></div>
          <div class="steps-editor" id="cs-steps" style="margin-top:8px"></div>
          <div class="row end" style="margin-top:10px"><button class="btn" id="cs-save">Save as recipe&hellip;</button><button class="btn primary" id="cs-run">Preview &amp; run&hellip;</button></div></div></div>`;
      const $ = (s) => CO.$(s, el);
      $("#nd-scan").onclick = doScan;
      $("#nd-allbaud").onclick = () => baudDialog(null);
      $("#nd-stop").onclick = () => CO.tryApi("/api/nodes/stop", {});
      $("#nd-log").onclick = async () => { const d = await CO.api("/api/nodes/log"); CO.modal({ title: "Node session log", wide: true, body: `<div class="small muted mono">${CO.esc(d.path || "")}</div><div class="joblog" style="max-height:60vh">${CO.esc(d.lines.join("\n"))}</div>` }); };
      $("#nd-nodes").addEventListener("click", (ev) => {
        const b = ev.target.closest("[data-act]"); if (!b) return;
        const node = +b.closest("tr").dataset.node;
        if (b.dataset.act === "setid") setIdDialog(node); else if (b.dataset.act === "baud") baudDialog(node); else useNode(node);
      });
      $("#cs-tpl").onchange = (e) => { const t = TEMPLATES[e.target.value]; if (!t) return; steps = JSON.parse(JSON.stringify(t.steps)); vars = { ...t.vars, ...Object.fromEntries(Object.entries(vars).filter(([k]) => k in t.vars && vars[k] !== "")) }; e.target.value = ""; saveEditor(); renderEditor(); };
      $("#cs-add").onclick = () => { steps.push({ op: "sdo_write", node: "$OLD_ID", index: "0x2100", sub: 0, type: "u8", value: "$NEW_ID" }); saveEditor(); renderEditor(); };
      $("#cs-clear").onclick = () => { steps = []; saveEditor(); renderEditor(); };
      $("#cs-run").onclick = () => previewAndRun("/api/nodes/plan/custom", { steps, vars, title: "Custom sequence" });
      $("#cs-save").onclick = saveRecipe;
      $("#cs-export").onclick = () => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([JSON.stringify({ vars, steps }, null, 2)], { type: "application/json" })); a.download = "sequence.json"; a.click(); };
      $("#cs-import").onchange = async (e) => { const f = e.target.files[0]; if (!f) return; try { const d = JSON.parse(await f.text()); steps = d.steps || []; vars = d.vars || vars; saveEditor(); renderEditor(); } catch (err) { CO.toast("Not a sequence file: " + err.message, "bad"); } e.target.value = ""; };
      editorEvents();
      renderScan(); renderEditor();
    },
    async onShow() {
      const live = CO.liveChannels();
      if (live.length && !live.some((c) => c.index === channel)) channel = live[0].index;
      CO.$("#nd-chsel", tabEl).innerHTML = CO.channelSelect(channel);
      const sel = CO.$("#nd-chsel select", tabEl);
      if (sel) sel.onchange = async (e) => { channel = +e.target.value; scan = await CO.api(`/api/nodes/scan?channel=${channel}`); if (!scan.nodes) scan = null; renderScan(); };
      const d = await CO.api(`/api/nodes/scan?channel=${channel}`).catch(() => null);
      scan = d && d.nodes ? d : null;
      renderScan();
      if (CO.state.job) { const j = await CO.api("/api/nodes/job").catch(() => null); if (j && j.id) renderJob(j); }
    },
  });

  CO.onFeed((msg) => {
    if (msg.type || !msg.job) return;
    renderJob(msg.job);
    if (msg.job.state !== "running" && msg.job.id === jobId && !msg.job._toasted) {
      // one toast per finished job
      if (CO._lastJobToast !== msg.job.id) { CO._lastJobToast = msg.job.id; CO.toast(`${msg.job.title}: ${msg.job.state}${msg.job.error ? " - " + msg.job.error : ""}`, msg.job.state === "done" ? "good" : "bad"); }
    }
  });
})();
