/* Decode tab: DBC files with live signal values; CANopen (EDS/XDD per node, SDO browser, NMT, EMCY log);
   J1939 (address claims, transport-protocol messages, DM1). Also fills CO.decodedText (the receive table's
   Decoded column) and provides CO.dbcTx (the "DBC message" mode of the transmit dialog).
   Writes to nodes (SDO write, NMT) are plans shown with CO.showPlan before anything is sent. */
"use strict";
(function () {
  const CO = window.canopener;
  let tabEl = null, sub = CO.prefs.get("dec.sub", "dbc");
  let dbc = { files: [], messages: [] }, co = { nodes: [], nmt: [], emcy: [] }, j1939 = { channels: [], ecus: [], messages: [], dm1: [] };
  let sigFilter = "", odNode = null, od = [], odFilter = "";
  const odValues = {};
  CO.decodedText = {};                     // receive-table row key "ch:key" -> decoded text

  const esc = CO.esc;
  const hex = (v, n) => "0x" + Number(v).toString(16).toUpperCase().padStart(n || 2, "0");
  const chLabel = (c) => (c === null || c === undefined ? "all" : CO.chName(c));
  const chOptions = () => `<option value="">All channels</option>` + CO.state.channels.map((c) => `<option value="${c.index}">${esc(c.name)}</option>`).join("");
  const fmt = (v, choices) => {
    if (v === null || v === undefined) return "";
    if (choices && choices[String(v)] !== undefined) return choices[String(v)];
    return typeof v === "number" && !Number.isInteger(v) ? String(+v.toFixed(4)) : String(v);
  };
  const firstChannel = () => (CO.liveChannels()[0] || { index: 0 }).index;

  // ---------------------------------------------------------------- data
  async function loadDbc() { const d = await CO.tryApi("/api/decode/dbc"); if (d) dbc = d; }
  async function refresh() {
    if (!tabEl || tabEl.hidden || (document.activeElement && document.activeElement.matches("#dec-body input"))) return;
    if (sub === "canopen") { const d = await CO.tryApi("/api/decode/canopen"); if (d) { co = d; renderCanopen(); } }
    else if (sub === "j1939") { const d = await CO.tryApi("/api/decode/j1939"); if (d) { j1939 = d; renderJ1939(); } }
  }
  async function upload(path, file, fields) {
    const fd = new FormData();
    fd.append("file", file);
    for (const [k, v] of Object.entries(fields)) fd.append(k, v);
    return CO.tryApi(path, fd);
  }

  // ---------------------------------------------------------------- DBC
  function liveText(m, s) {
    if (!m.j1939) { const sg = CO.state.signals[`dbc/${m.name}/${s.name}`]; return sg ? fmt(sg.value, s.choices) : ""; }
    const tail = `/${m.pgn}.${s.name}`, out = [];                // J1939: one value per source address
    for (const [k, sg] of Object.entries(CO.state.signals)) if (k.startsWith("j1939/") && k.endsWith(tail)) out.push(`SA ${k.split("/")[1]}: ${fmt(sg.value, s.choices)}`);
    return out.join(", ");
  }

  function renderDbc() {
    const el = CO.$("#dec-body", tabEl);
    const files = dbc.files.length ? `<table class="tbl compact"><thead><tr><th>File</th><th>Channel</th><th class="r">Messages</th><th></th></tr></thead><tbody>` +
      dbc.files.map((f, i) => `<tr><td class="m">${esc(f.path)}</td><td>${esc(chLabel(f.channel))}</td><td class="r">${f.messages}</td>
        <td class="r"><button class="btn small ghost" data-rm="${i}" title="Unload">&times;</button></td></tr>`).join("") + `</tbody></table>`
      : `<div class="empty">No DBC loaded. Give a path or upload a file above.</div>`;
    const q = sigFilter.toLowerCase();
    const rows = [];
    for (const m of dbc.messages) {
      for (const s of m.signals) {
        if (q && !(m.name + "." + s.name).toLowerCase().includes(q)) continue;
        if (rows.length >= 1000) break;
        const mux = s.mux ? " (mux)" : s.mux_ids.length ? ` (m${s.mux_ids.join(",")})` : "";
        rows.push(`<tr><td>${esc(m.name)}</td><td class="m">${CO.idText(m.id, m.ext ? CO.F.EXT : 0)}${m.j1939 ? ` <span class="badge">PGN ${m.pgn}</span>` : ""}</td>
          <td title="${esc(s.comment)}">${esc(s.name)}<span class="muted small">${esc(mux)}</span></td>
          <td class="m r" data-m="${esc(m.name)}" data-s="${esc(s.name)}"></td><td>${esc(s.unit)}</td>
          <td class="muted small">${s.min ?? ""} .. ${s.max ?? ""}${Object.keys(s.choices).length ? " (" + esc(Object.entries(s.choices).map(([k, v]) => k + "=" + v).join(", ")) + ")" : ""}</td></tr>`);
      }
    }
    el.innerHTML = `<div class="card">${files}</div>
      <div class="card" style="margin-top:10px"><div class="row"><h3 style="margin:0">Signals</h3><span class="spacer"></span>
        <input id="dbc-filter" placeholder="filter: message or signal" value="${esc(sigFilter)}" style="width:240px"></div>
        <table class="tbl compact" style="margin-top:6px"><thead><tr><th>Message</th><th>ID</th><th>Signal</th><th class="r">Value</th><th>Unit</th><th>Range</th></tr></thead>
        <tbody>${rows.join("")}</tbody></table>${rows.length ? "" : `<div class="empty">No signals.</div>`}</div>`;
    CO.$("#dbc-filter", el).oninput = (e) => { sigFilter = e.target.value; const pos = e.target.selectionStart; renderDbc(); const f = CO.$("#dbc-filter", el); f.focus(); f.setSelectionRange(pos, pos); };
    el.querySelectorAll("[data-rm]").forEach((b) => (b.onclick = async () => {
      const f = dbc.files[+b.dataset.rm];
      if (await CO.tryApi("/api/decode/dbc/remove", { path: f.path, channel: f.channel })) { await loadDbc(); renderDbc(); }
    }));
    updateDbcValues();
  }

  function updateDbcValues() {
    if (!tabEl || tabEl.hidden || sub !== "dbc") return;
    const byName = new Map(dbc.messages.map((m) => [m.name, m]));
    for (const td of CO.$$("td[data-m]", tabEl)) {
      const m = byName.get(td.dataset.m); if (!m) continue;
      const s = m.signals.find((x) => x.name === td.dataset.s);
      const t = s ? liveText(m, s) : "";
      if (td.textContent !== t) td.textContent = t;
    }
  }

  async function dbcLoad() {
    const path = CO.$("#dbc-path", tabEl).value.trim();
    if (!path) { CO.toast("Give the full path of a .dbc file (or use Upload)", "warn"); return; }
    const ch = CO.$("#dbc-ch", tabEl).value;
    if (await CO.tryApi("/api/decode/dbc/load", { path, channel: ch === "" ? null : +ch }, "DBC loaded")) { await loadDbc(); renderDbc(); }
  }

  // ---------------------------------------------------------------- CANopen
  function nmtChannel(node, assigned) {
    if (assigned !== null && assigned !== undefined) return assigned;
    const hb = co.nmt.find((n) => n.node === node);
    return hb ? hb.channel : firstChannel();
  }

  function renderCanopen() {
    const el = CO.$("#dec-body", tabEl);
    const nodes = co.nodes.map((n) => ({ ...n, state: (co.nmt.find((h) => h.node === n.node && (n.channel === null || h.channel === n.channel)) || {}).state }));
    for (const h of co.nmt) if (!co.nodes.some((n) => n.node === h.node)) nodes.push({ node: h.node, channel: h.channel, path: "", pdos: [], state: h.state, hbOnly: true });
    nodes.sort((a, b) => a.node - b.node);
    const nmtBtns = `<button class="btn small" data-nmt="start">Start</button><button class="btn small" data-nmt="stop">Stop</button>
      <button class="btn small" data-nmt="preop">Pre-op</button><button class="btn small" data-nmt="reset_node">Reset</button>`;
    const nodeRows = nodes.map((n) => `<tr data-node="${n.node}" data-ch="${n.channel ?? ""}"><td class="r m"><b>${n.node}</b></td><td>${esc(chLabel(n.channel))}</td>
      <td class="m small" title="${esc(n.path)}">${n.hbOnly ? `<span class="muted">no EDS (heartbeat seen)</span>` : esc(n.path.split(/[\\/]/).pop())}</td>
      <td class="small">${n.pdos.map((p) => `${esc(p.name)} <span class="muted">${hex(p.cob_id, 3)}</span>`).join(", ")}${n.source === "device" ? ` <span class="badge accent">from device</span>` : ""}</td>
      <td>${esc(n.state || "-")}</td>
      <td class="r">${n.hbOnly ? "" : `<button class="btn small" data-act="od">Objects&hellip;</button><button class="btn small" data-act="pdo" title="Read the PDO mapping from the device (SDO reads)">Read PDO mapping</button>`}
        ${nmtBtns}${n.hbOnly ? "" : `<button class="btn small ghost" data-act="rm" title="Remove the EDS">&times;</button>`}</td></tr>`).join("");
    const q = odFilter.toLowerCase();
    const odRows = od.filter((o) => !q || o.name.toLowerCase().includes(q) || hex(o.index, 4).toLowerCase().includes(q)).slice(0, 1000).map((o) => {
      const k = `${o.index}:${o.sub}`;
      return `<tr data-k="${k}"><td class="m">${hex(o.index, 4)}</td><td class="m">${hex(o.sub)}</td><td>${esc(o.name)}</td><td>${esc(o.type)}</td><td>${esc(o.access)}</td>
        <td class="m">${esc(typeof o.default === "number" && /^u(16|32|64)$/.test(o.type) ? hex(o.default, 4) : o.default ?? "")}</td><td class="m">${esc(odValues[k] ?? "")}</td>
        <td class="r"><button class="btn small" data-sdo="read">Read</button>${["ro", "const"].includes(o.access) ? "" : `<button class="btn small" data-sdo="write">Write&hellip;</button>`}</td></tr>`;
    }).join("");
    const emcy = co.emcy.slice().reverse().map((e) => `<tr><td class="m">${CO.fmtTime(e.ts)}</td><td>${esc(CO.chName(e.channel))}</td><td class="r">${e.node}</td>
      <td class="m">${esc(e.code_hex)}</td><td>${esc(e.text)}</td><td class="m">${hex(e.register)} <span class="muted small">${esc(e.register_bits.join(", "))}</span></td><td class="m">${esc(e.vendor)}</td></tr>`).join("");
    el.innerHTML = `<div class="card"><table class="tbl compact"><thead><tr><th class="r">Node</th><th>Channel</th><th>EDS / XDD</th><th>PDOs</th><th>NMT</th><th></th></tr></thead>
        <tbody>${nodeRows}</tbody></table>${nodes.length ? "" : `<div class="empty">Assign an EDS/XDD to a node id above. Nodes sending heartbeats show here too.</div>`}
        <p class="small muted">Start / Stop / Pre-op / Reset and SDO writes open a preview of the exact frames first.</p></div>
      ${odNode ? `<div class="card" style="margin-top:10px"><div class="row"><h3 style="margin:0">Object dictionary of node ${odNode.node}</h3>
        <span class="muted small">on ${esc(CO.chName(odNode.channel))}</span><span class="spacer"></span>
        <input id="od-filter" placeholder="filter: name or index" value="${esc(odFilter)}" style="width:220px"><button class="btn small ghost" id="od-close">Close</button></div>
        <table class="tbl compact" style="margin-top:6px"><thead><tr><th>Index</th><th>Sub</th><th>Name</th><th>Type</th><th>Access</th><th>Default</th><th>Value</th><th></th></tr></thead>
        <tbody id="od-rows">${odRows}</tbody></table></div>` : ""}
      <div class="card" style="margin-top:10px"><div class="row"><h3 style="margin:0">EMCY log</h3><span class="spacer"></span><button class="btn small" id="emcy-clear">Clear</button></div>
        ${emcy ? `<table class="tbl compact" style="margin-top:6px"><thead><tr><th>Time</th><th>Ch</th><th class="r">Node</th><th>Code</th><th>Meaning</th><th>Error register</th><th>Vendor bytes</th></tr></thead><tbody>${emcy}</tbody></table>`
               : `<div class="empty">No emergency messages.</div>`}</div>`;
    const $ = (s) => CO.$(s, el);
    if ($("#od-filter")) {
      $("#od-filter").oninput = (e) => { odFilter = e.target.value; const pos = e.target.selectionStart; renderCanopen(); const f = CO.$("#od-filter", el); f.focus(); f.setSelectionRange(pos, pos); };
      $("#od-close").onclick = () => { odNode = null; renderCanopen(); };
    }
    $("#emcy-clear").onclick = async () => { await CO.tryApi("/api/decode/clear", {}); refresh(); };
  }

  async function canopenAction(ev) {
    const b = ev.target.closest("button"); if (!b) return;
    if (b.dataset.sdo) { sdoAction(b.dataset.sdo, b.closest("tr").dataset.k); return; }
    const tr = b.closest("tr[data-node]"); if (!tr) return;
    const node = +tr.dataset.node, assigned = tr.dataset.ch === "" ? null : +tr.dataset.ch;
    const channel = nmtChannel(node, assigned);
    if (b.dataset.nmt) {
      const plan = await CO.tryApi("/api/decode/canopen/plan/nmt", { channel, node, command: b.dataset.nmt });
      if (plan) await CO.showPlan(plan);
    } else if (b.dataset.act === "od") {
      const d = await CO.tryApi(`/api/decode/canopen/od?node=${node}&channel=${channel}`);
      if (d) { odNode = { node, channel }; od = d.objects; odFilter = ""; renderCanopen(); }
    } else if (b.dataset.act === "pdo") {
      if (await CO.tryApi("/api/decode/canopen/pdo_mapping", { channel, node }, `PDO mapping of node ${node} read from the device`)) refresh();
    } else if (b.dataset.act === "rm") {
      if (await CO.tryApi("/api/decode/canopen/eds/remove", { node, channel: assigned })) { if (odNode && odNode.node === node) odNode = null; refresh(); }
    }
  }

  async function sdoAction(act, k) {
    const [index, sub] = k.split(":").map(Number);
    const o = od.find((x) => x.index === index && x.sub === sub);
    const body = { channel: odNode.channel, node: odNode.node, index, sub };
    if (act === "read") {
      const r = await CO.tryApi("/api/decode/canopen/sdo_read", body);
      if (r) { odValues[k] = r.text ? `${r.value} (${r.text})` : String(r.value); renderCanopen(); }
      return;
    }
    const res = await CO.modal({
      title: `Write node ${odNode.node} ${hex(index, 4)}:${hex(sub)} ${o ? o.name : ""}`,
      body: `<label class="field"><span>Value (${esc(o ? o.type : "")}${o && o.unit ? ", " + esc(o.unit) : ""})</span><input id="sdo-v" class="m" value="${esc(odValues[k] ?? o?.default ?? "")}"></label>
        <p class="small muted">Next you see the exact SDO frames and confirm before anything is sent.</p>`,
      buttons: [{ label: "Cancel", value: "cancel" }, { label: "Preview", value: "ok", cls: "primary" }],
      onOpen: (f) => CO.$("#sdo-v", f).focus(),
    });
    if (!res) return;
    const plan = await CO.tryApi("/api/decode/canopen/plan/sdo_write", { ...body, value: CO.$("#sdo-v", res.form).value.trim() });
    if (plan) await CO.showPlan(plan);
  }

  async function edsAssign(file) {
    const node = +CO.$("#co-node", tabEl).value, ch = CO.$("#co-ch", tabEl).value;
    if (!(node >= 1 && node <= 127)) { CO.toast("Node id must be 1..127", "warn"); return; }
    let ok;
    if (file) ok = await upload("/api/decode/canopen/eds/upload", file, { node, channel: ch });
    else {
      const path = CO.$("#co-path", tabEl).value.trim();
      if (!path) { CO.toast("Give the full path of an .eds/.xdd file (or use Upload)", "warn"); return; }
      ok = await CO.tryApi("/api/decode/canopen/eds", { node, path, channel: ch === "" ? null : +ch });
    }
    if (ok) { CO.toast(`EDS assigned to node ${node}`, "good"); refresh(); }
  }

  // ---------------------------------------------------------------- J1939
  function renderJ1939() {
    const el = CO.$("#dec-body", tabEl);
    const chans = CO.state.channels.map((c) => `<label class="row small"><input type="checkbox" data-j1939="${c.index}" ${j1939.channels.includes(c.index) ? "checked" : ""}> ${esc(c.name)}</label>`).join("");
    const ecus = j1939.ecus.map((e) => `<tr><td>${esc(CO.chName(e.channel))}</td><td class="m">${hex(e.sa)}</td><td class="m">${esc(e.name)}</td><td class="r m">${e.identity}</td>
      <td class="r m">${e.manufacturer}</td><td class="r m">${e.function}</td><td class="r">${e.ecu_instance}</td><td class="r">${e.function_instance}</td>
      <td class="r">${e.vehicle_system}</td><td>${esc(e.industry || e.industry_group)}</td><td>${e.arbitrary_address ? "yes" : "no"}</td><td class="m">${CO.fmtTime(e.ts)}</td></tr>`).join("");
    const lamp = (n, v) => `<span class="badge ${v === 1 ? "warn" : ""}" title="${n}: ${["off", "on", "error", "n/a"][v] || v}">${n}</span>`;
    const dm1 = j1939.dm1.map((d) => `<tr><td>${esc(CO.chName(d.channel))}</td><td class="m">${hex(d.sa)}</td><td>${Object.entries(d.lamps).map(([n, v]) => lamp(n, v)).join(" ")}</td>
      <td class="m">${d.dtcs.length ? d.dtcs.map((x) => `SPN ${x.spn} FMI ${x.fmi} OC ${x.oc}`).map(esc).join("<br>") : `<span class="muted">no active DTC</span>`}</td><td class="m">${CO.fmtTime(d.ts)}</td></tr>`).join("");
    const msgs = j1939.messages.slice().reverse().map((m) => `<tr><td class="m">${CO.fmtTime(m.ts)}</td><td>${esc(CO.chName(m.channel))}</td><td class="m">${hex(m.sa)}</td><td class="m">${hex(m.da)}</td>
      <td class="r m">${m.pgn}</td><td>${esc(m.name)}</td><td>${esc(m.kind)}</td><td class="r">${m.size}</td><td class="m small" style="white-space:normal">${esc(m.decoded || m.data)}</td></tr>`).join("");
    el.innerHTML = `<div class="card"><div class="row"><b>J1939 decoding on</b> ${chans || `<span class="muted">no channel connected</span>`}</div>
        <p class="small muted">Splits 29-bit ids into priority / PGN / DA / SA, collects address claims, reassembles TP (RTS/CTS and BAM) and decodes DM1. A J1939 DBC (VFrameFormat J1939PG) decodes the signals by PGN.</p></div>
      <div class="card" style="margin-top:10px"><h3>ECUs (address claims)</h3>${ecus ? `<table class="tbl compact"><thead><tr><th>Ch</th><th>SA</th><th>NAME</th><th class="r">Identity</th><th class="r">Manufacturer</th><th class="r">Function</th><th class="r">ECU inst</th><th class="r">Fn inst</th><th class="r">Vehicle system</th><th>Industry</th><th>Arbitrary addr</th><th>Seen</th></tr></thead><tbody>${ecus}</tbody></table>` : `<div class="empty">No address claim seen.</div>`}</div>
      <div class="card" style="margin-top:10px"><h3>DM1 active diagnostic trouble codes</h3>${dm1 ? `<table class="tbl compact"><thead><tr><th>Ch</th><th>SA</th><th>Lamps</th><th>DTCs</th><th>Seen</th></tr></thead><tbody>${dm1}</tbody></table>` : `<div class="empty">No DM1 seen.</div>`}</div>
      <div class="card" style="margin-top:10px"><div class="row"><h3 style="margin:0">Transport protocol messages</h3><span class="spacer"></span><button class="btn small" id="tp-clear">Clear</button></div>
        ${msgs ? `<table class="tbl compact" style="margin-top:6px"><thead><tr><th>Time</th><th>Ch</th><th>SA</th><th>DA</th><th class="r">PGN</th><th>Name</th><th>Kind</th><th class="r">Bytes</th><th>Decoded / data</th></tr></thead><tbody>${msgs}</tbody></table>` : `<div class="empty">No reassembled message yet.</div>`}</div>`;
    el.querySelectorAll("[data-j1939]").forEach((cb) => (cb.onchange = async () => {
      if (!await CO.tryApi("/api/decode/j1939", { channel: +cb.dataset.j1939, enabled: cb.checked })) cb.checked = !cb.checked; else refresh();
    }));
    CO.$("#tp-clear", el).onclick = async () => { await CO.tryApi("/api/decode/clear", {}); refresh(); };
  }

  // ---------------------------------------------------------------- tab
  function renderSub() {
    CO.prefs.set("dec.sub", sub);
    CO.$$("[data-sub]", tabEl).forEach((b) => b.classList.toggle("on", b.dataset.sub === sub));
    CO.$$(".dec-tools", tabEl).forEach((t) => (t.hidden = t.dataset.for !== sub));
    CO.$$(".ch-opts", tabEl).forEach((s) => { const v = s.value; s.innerHTML = chOptions(); s.value = v; });
    if (sub === "dbc") renderDbc(); else if (sub === "canopen") renderCanopen(); else renderJ1939();
    refresh();
  }

  CO.registerTab({
    id: "decode", title: "Decode", order: 45,
    render(el) {
      tabEl = el;
      el.innerHTML = `<div class="toolbar">
          <button class="btn" data-sub="dbc">DBC</button><button class="btn" data-sub="canopen">CANopen</button><button class="btn" data-sub="j1939">J1939</button>
          <span class="sep"></span>
          <span class="row dec-tools" data-for="dbc"><input id="dbc-path" class="m" placeholder="C:\\path\\file.dbc" style="width:280px">
            <select id="dbc-ch" class="ch-opts"></select><button class="btn primary" id="dbc-load">Load</button>
            <label class="btn">Upload&hellip;<input type="file" id="dbc-file" accept=".dbc" hidden></label></span>
          <span class="row dec-tools" data-for="canopen"><label class="row small">Node <input id="co-node" type="number" min="1" max="127" value="1" style="width:64px"></label>
            <select id="co-ch" class="ch-opts"></select><input id="co-path" class="m" placeholder="C:\\path\\device.eds or .xdd" style="width:260px">
            <button class="btn primary" id="co-assign">Assign</button><label class="btn">Upload&hellip;<input type="file" id="co-file" accept=".eds,.dcf,.xdd" hidden></label></span>
        </div>
        <div class="page" id="dec-body"></div>`;
      const $ = (s) => CO.$(s, el);
      el.querySelector(".toolbar").addEventListener("click", (ev) => { const b = ev.target.closest("[data-sub]"); if (b) { sub = b.dataset.sub; renderSub(); } });
      $("#dbc-load").onclick = dbcLoad;
      $("#dbc-file").onchange = async (e) => {
        const f = e.target.files[0]; if (!f) return;
        if (await upload("/api/decode/dbc/upload", f, { channel: $("#dbc-ch").value })) { CO.toast("DBC loaded", "good"); await loadDbc(); renderDbc(); }
        e.target.value = "";
      };
      $("#co-assign").onclick = () => edsAssign(null);
      $("#co-file").onchange = async (e) => { const f = e.target.files[0]; if (f) await edsAssign(f); e.target.value = ""; };
      $("#dec-body").addEventListener("click", (ev) => { if (sub === "canopen") canopenAction(ev); });
    },
    async onShow() { await loadDbc(); renderSub(); },
  });

  // ---------------------------------------------------------------- feed + polling
  CO.onFeed((msg) => { if (!msg.type && msg.signals) updateDbcValues(); });
  setInterval(refresh, 1000);
  setInterval(async () => {                // the receive table's Decoded column
    if (CO.current !== "receive" || !CO.state.connected) return;
    try { CO.decodedText = (await CO.api("/api/decode/text")).text; } catch (e) { /* server restarting */ }
  }, 500);

  // ---------------------------------------------------------------- transmit dialog: DBC message mode
  let txEntry = null;
  CO.dbcTx = {
    html(e) {
      txEntry = e;
      if (!dbc.messages.length && !e.dbc_message) return "";
      return `<label class="field"><span>Data from</span><select id="t-dbc"><option value="">raw bytes (below)</option>
        ${dbc.messages.map((m) => `<option value="${esc(m.name)}" ${m.name === e.dbc_message ? "selected" : ""}>DBC message ${esc(m.name)} (${CO.idText(m.id, m.ext ? CO.F.EXT : 0)})</option>`).join("")}</select></label>
        <div id="t-sigs" class="grid3"></div>`;
    },
    open(f) {
      const sel = CO.$("#t-dbc", f); if (!sel) return;
      const upd = () => {
        const m = dbc.messages.find((x) => x.name === sel.value);
        for (const id of ["#t-id", "#t-data", "#t-ext", "#t-rtr"]) CO.$(id, f).disabled = !!m;
        const vals = m && m.name === txEntry.dbc_message ? txEntry.signals || {} : {};
        CO.$("#t-sigs", f).innerHTML = m ? m.signals.map((s) => {
          const v = vals[s.name] ?? s.offset;
          const label = `${esc(s.name)}${s.unit ? " [" + esc(s.unit) + "]" : ""}${s.mux_ids.length ? ` (mux ${s.mux_ids.join(",")})` : ""}`;
          const ch = Object.entries(s.choices);
          return `<label class="field"><span>${label}</span>${ch.length
            ? `<select data-sig="${esc(s.name)}">${ch.map(([k, t]) => `<option value="${esc(k)}" ${String(v) === k || v === t ? "selected" : ""}>${esc(t)} (${esc(k)})</option>`).join("")}</select>`
            : `<input data-sig="${esc(s.name)}" class="m" value="${esc(v)}" title="${s.min ?? ""} .. ${s.max ?? ""}">`}</label>`;
        }).join("") : "";
      };
      sel.onchange = upd; upd();
    },
    read(f) {
      const sel = CO.$("#t-dbc", f);
      if (!sel || !sel.value) return { dbc_message: "", signals: {} };
      const signals = {};
      for (const inp of CO.$$("[data-sig]", f)) { const v = inp.value.trim(); signals[inp.dataset.sig] = v !== "" && !isNaN(+v) ? +v : v; }
      return { dbc_message: sel.value, signals };
    },
  };
  loadDbc();
})();
