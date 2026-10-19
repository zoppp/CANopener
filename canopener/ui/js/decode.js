/* Decode tab: DBC files and messages (click one: its signals, live); CANopen (EDS/XDD per node, NMT, EMCY log;
   click a node: its PDO objects, SDO traffic and object dictionary); J1939 (source addresses and address claims,
   click one: the PGNs it sends; transport-protocol messages, DM1). The detail panels poll the server every 500 ms.
   Also fills CO.decodedText (the receive table's Decoded column) and provides CO.dbcTx (the "DBC message" mode of
   the transmit dialog). Writes to nodes (SDO write, NMT) are plans shown with CO.showPlan before anything is sent. */
"use strict";
(function () {
  const CO = window.canopener;
  let tabEl = null, sub = CO.prefs.get("dec.sub", "dbc");
  let dbc = { files: [], messages: [] }, co = { nodes: [], nmt: [], sdo_servers: [], emcy: [] }, j1939 = { channels: [], ecus: [], messages: [], dm1: [], sources: [] };
  let sigFilter = "", od = [], odFilter = "", odOpen = false;
  let dbcLive = {}, dbcSel = null, dbcDet = null;          // DBC: last seen per message; the selected message
  let nodeSel = null, nodeDet = null;                      // CANopen: {node, assigned, channel, eds}; its detail
  let jSel = null, jDet = null;                            // J1939: {sa, ch}; its detail
  CO.decodedText = {};                     // receive-table row key "ch:key" -> decoded text

  const esc = CO.esc;
  const hex = (v, n) => "0x" + Number(v).toString(16).toUpperCase().padStart(n || 2, "0");
  const chLabel = (c) => (c === null || c === undefined ? "all" : CO.chName(c));
  const chOptions = () => `<option value="">All channels</option>` + CO.state.channels.map((c) => `<option value="${c.index}">${esc(c.name)}</option>`).join("");
  const idxSub = (i, s) => `${hex(i, 4)}:${Number(s).toString(16).toUpperCase().padStart(2, "0")}`;
  const valText = (v, text) => {                          // a decoded value, its value name first
    if (v === null || v === undefined) return "";
    const t = typeof v === "number" && !Number.isInteger(v) ? String(+v.toFixed(4)) : String(v);
    return text ? `${text} (${t})` : t;
  };
  const fmtAge = (s) => (s < 1 ? `${s.toFixed(1)} s` : s < 60 ? `${Math.round(s)} s` : s < 3600 ? `${Math.round(s / 60)} min` : `${Math.round(s / 3600)} h`);
  const fmtPeriod = (ms) => (!ms ? "" : ms < 1000 ? `${ms.toFixed(1)} ms` : `${(ms / 1000).toFixed(2)} s`);
  const stopped = (x) => x.count > 1 && x.age > Math.max(3 * x.period_ms / 1000, 1);
  const seen = (x) => (!x || !x.count ? `<span class="muted">not seen</span>`
    : `${fmtAge(x.age)} ago${stopped(x) ? ` <span class="badge warn" title="no frame for more than 3 periods">stopped</span>` : ""}`);
  const asHex = (v, type) => (typeof v === "number" && Number.isInteger(v) && /^u(16|32|64)$/.test(type) ? hex(v, 4) : v);   // like the Default column
  const range = (o) => (o.min === null && o.max === null ? "" : `${o.min ?? ""} .. ${o.max ?? ""}`);
  const table = (heads, rows) => `<table class="tbl compact" style="margin-top:4px"><thead><tr>${heads.map((h) => h.startsWith(">") ? `<th class="r">${h.slice(1)}</th>` : `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>`;
  const busy = () => CO.selecting(CO.$("#dec-body", tabEl)) || CO.$(".ctx-menu");
  const firstChannel = () => (CO.liveChannels()[0] || { index: 0 }).index;

  // ---------------------------------------------------------------- data
  async function loadDbc() { const d = await CO.tryApi("/api/decode/dbc"); if (d) dbc = d; }
  async function refresh() {
    if (!tabEl || tabEl.hidden || (document.activeElement && document.activeElement.matches("#dec-body input"))) return;
    if (busy()) return;                                   // keep a text selection / the open menu's rows
    if (sub === "canopen") { const d = await CO.tryApi("/api/decode/canopen"); if (d) { co = d; renderCanopen(); } }
    else if (sub === "j1939") { const d = await CO.tryApi("/api/decode/j1939"); if (d) { j1939 = d; renderJ1939(); } }
    else { const d = await CO.tryApi("/api/decode/dbc/live"); if (d) { dbcLive = d.messages; updateDbcLive(); } }
  }

  /* The open detail panel, every 500 ms: only its live parts are replaced (the rest of the page stays). */
  async function refreshDetail() {
    if (!tabEl || tabEl.hidden || !CO.state.connected) return;
    const s = sub === "canopen" ? nodeSel : sub === "dbc" ? dbcSel : jSel;
    if (!s) return;
    const url = sub === "canopen" ? `/api/decode/canopen/node?node=${s.node}&channel=${s.assigned ?? ""}`
      : sub === "dbc" ? `/api/decode/dbc/message?name=${encodeURIComponent(s)}` : `/api/decode/j1939/source?sa=${s.sa}&channel=${s.ch}`;
    let d;
    try { d = await CO.api(url); } catch (e) { return; }  // gone (DBC unloaded) or server restarting: keep the last
    if (s !== (sub === "canopen" ? nodeSel : sub === "dbc" ? dbcSel : jSel)) return;
    if (sub === "canopen") nodeDet = d; else if (sub === "dbc") dbcDet = d; else jDet = d;
    if (busy()) return;
    const set = (sel, html) => { const x = CO.$(sel, tabEl); if (x && x.innerHTML !== html) x.innerHTML = html; };
    if (sub === "canopen") {
      set("#np-pdo", pdoHtml()); set("#np-sdo", sdoHtml());
      for (const td of CO.$$("td[data-v]", tabEl)) { const h = odVal(td.dataset.v); if (td.innerHTML !== h) td.innerHTML = h; }
    } else if (sub === "dbc") set("#dbc-detail", dbcDetailHtml());
    else set("#j-detail", jDetailHtml());
  }
  async function upload(path, file, fields) {
    const fd = new FormData();
    fd.append("file", file);
    for (const [k, v] of Object.entries(fields)) fd.append(k, v);
    return CO.tryApi(path, fd);
  }

  // ---------------------------------------------------------------- DBC
  const idCell = (m) => `${CO.idText(m.id, m.ext ? CO.F.EXT : 0)}${m.j1939 ? ` <span class="badge">PGN ${m.pgn}</span>` : ""}`;

  function renderDbc() {
    const el = CO.$("#dec-body", tabEl);
    const files = dbc.files.length ? `<table class="tbl compact"><thead><tr><th>File</th><th>Channel</th><th class="r">Messages</th><th></th></tr></thead><tbody>` +
      dbc.files.map((f, i) => `<tr><td class="m">${esc(f.path)}</td><td>${esc(chLabel(f.channel))}</td><td class="r">${f.messages}</td>
        <td class="r"><button class="btn small ghost" data-rm="${i}" title="Unload">&times;</button></td></tr>`).join("") + `</tbody></table>`
      : `<div class="empty">No DBC loaded. Give a path or upload a file above.</div>`;
    const q = sigFilter.toLowerCase();
    const rows = [];
    for (const m of dbc.messages) {
      if (q && !m.name.toLowerCase().includes(q) && !m.signals.some((x) => x.name.toLowerCase().includes(q))) continue;
      if (rows.length >= 1000) break;
      rows.push(`<tr data-msg="${esc(m.name)}" class="pick${m.name === dbcSel ? " sel" : ""}" title="${esc(m.comment)}"><td>${esc(m.name)}</td><td class="m">${idCell(m)}</td>
        <td class="r">${m.signals.length}</td><td class="r m" data-lv="period"></td><td data-lv="seen"></td><td class="m" data-lv="data"></td></tr>`);
    }
    if (dbcSel && !dbc.messages.some((m) => m.name === dbcSel)) { dbcSel = null; dbcDet = null; }   // its DBC was unloaded
    el.innerHTML = `<div class="card">${files}</div>
      ${dbcSel ? `<div class="card" style="margin-top:10px" id="dbc-panel"><div class="row"><h3 style="margin:0">${esc(dbcSel)}</h3><span class="spacer"></span>
        <button class="btn small ghost" id="dbc-close">Close</button></div><div id="dbc-detail">${dbcDetailHtml()}</div></div>` : ""}
      <div class="card" style="margin-top:10px"><div class="row"><h3 style="margin:0">Messages</h3><span class="muted small">click one for its signals</span><span class="spacer"></span>
        <input id="dbc-filter" placeholder="filter: message or signal" value="${esc(sigFilter)}" style="width:240px"></div>
        ${table(["Message", "ID", ">Signals", ">Period", "Last seen", "Data"], rows)}${rows.length ? "" : `<div class="empty">No messages.</div>`}</div>`;
    CO.$("#dbc-filter", el).oninput = (e) => { sigFilter = e.target.value; const pos = e.target.selectionStart; renderDbc(); const f = CO.$("#dbc-filter", el); f.focus(); f.setSelectionRange(pos, pos); };
    if (CO.$("#dbc-close", el)) CO.$("#dbc-close", el).onclick = () => { dbcSel = null; dbcDet = null; renderDbc(); };
    el.querySelectorAll("[data-rm]").forEach((b) => (b.onclick = async () => {
      const f = dbc.files[+b.dataset.rm];
      if (await CO.tryApi("/api/decode/dbc/remove", { path: f.path, channel: f.channel })) { await loadDbc(); renderDbc(); }
    }));
    updateDbcLive();
  }

  function updateDbcLive() {                       // the message list's period / last seen / data, in place
    if (!tabEl || tabEl.hidden || sub !== "dbc") return;
    for (const tr of CO.$$("tr[data-msg]", tabEl)) {
      const lv = dbcLive[tr.dataset.msg];
      for (const td of CO.$$("td[data-lv]", tr)) {
        const h = td.dataset.lv === "period" ? (lv ? fmtPeriod(lv.period_ms) : "") : td.dataset.lv === "seen" ? seen(lv) : lv ? esc(lv.data) : "";
        if (td.innerHTML !== h) td.innerHTML = h;
      }
    }
  }

  function dbcDetailHtml() {
    const m = dbcDet;
    if (!m) return `<div class="empty">Loading&hellip;</div>`;
    const lv = m.live;
    const rows = m.signals.map((x) => `<tr title="${esc(x.comment)}"><td>${esc(x.name)}<span class="muted small">${x.mux ? " (mux)" : x.mux_ids.length ? ` (m${x.mux_ids.join(",")})` : ""}</span></td>
      <td class="m r">${esc(valText(x.value, x.text))}</td><td class="m r">${x.raw ?? ""}</td><td>${esc(x.unit)}</td><td class="muted small">${esc(range(x))}</td>
      <td class="muted small" style="white-space:normal">${esc(Object.entries(x.choices).map(([k, v]) => k + "=" + v).join(", "))}</td></tr>`);
    return `<div class="small">${idCell(m)} &middot; ${m.length} bytes${m.senders.length ? " &middot; sent by " + esc(m.senders.join(", ")) : ""} &middot; DBC for ${m.channel === null ? "all channels" : esc(CO.chName(m.channel))}
        ${lv ? ` &middot; ${fmtPeriod(lv.period_ms) || "one frame"} &middot; ${seen(lv)} on ${esc(CO.chName(lv.channel))}${lv.tx ? " (Tx)" : ""} &middot; <span class="m">${esc(lv.data)}</span>` : ` &middot; <span class="muted">not seen</span>`}</div>
      ${table(["Signal", ">Value", ">Raw", "Unit", "Range", "Value names"], rows)}
      ${m.signals.some((x) => x.mux_ids.length) ? `<p class="small muted">Multiplexed signals have a value only when the last frame carried them.</p>` : ""}`;
  }

  async function selectDbc(name) {
    dbcSel = dbcSel === name ? null : name; dbcDet = null;
    renderDbc();
    if (dbcSel) { await refreshDetail(); CO.$("#dbc-panel", tabEl)?.scrollIntoView({ block: "nearest" }); }
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
    for (const s of co.sdo_servers) if (!nodes.some((n) => n.node === s.node)) nodes.push({ node: s.node, channel: s.channel, path: "", pdos: [], hbOnly: true, sdoOnly: true });
    const servers = (node) => co.sdo_servers.filter((s) => s.node === node).map((s) => ` <span class="muted" title="extra SDO server: request / response COB-ID">SDO ${hex(s.request, 3)}/${hex(s.response, 3)}</span>`).join("");
    nodes.sort((a, b) => a.node - b.node);
    if (nodeSel && !nodes.some((n) => n.node === nodeSel.node && (n.channel ?? null) === nodeSel.assigned && !n.hbOnly === nodeSel.eds)) { nodeSel = null; nodeDet = null; }
    const nmtBtns = `<button class="btn small" data-nmt="start">Start</button><button class="btn small" data-nmt="stop">Stop</button>
      <button class="btn small" data-nmt="preop">Pre-op</button><button class="btn small" data-nmt="reset_node">Reset</button>`;
    const nodeRows = nodes.map((n) => `<tr data-node="${n.node}" data-ch="${n.channel ?? ""}" data-eds="${n.hbOnly ? "" : 1}" class="pick${nodeSel && nodeSel.node === n.node && nodeSel.assigned === (n.channel ?? null) ? " sel" : ""}">
      <td class="r m"><b>${n.node}</b></td><td>${esc(chLabel(n.channel))}</td>
      <td class="m small" title="${esc(n.path)}">${n.hbOnly ? `<span class="muted">no EDS (${n.sdoOnly ? "extra SDO server" : "heartbeat seen"})</span>` : esc(n.path.split(/[\\/]/).pop())}${servers(n.node)}</td>
      <td class="small">${n.pdos.map((p) => `${esc(p.name)} <span class="muted">${hex(p.cob_id, 3)}</span>`).join(", ")}${n.source === "device" ? ` <span class="badge accent">from device</span>` : ""}</td>
      <td>${esc(n.state || "-")}</td>
      <td class="r">${n.hbOnly ? "" : `<button class="btn small" data-act="pdo" title="Read the PDO mapping from the device (SDO reads)">Read PDO mapping</button>`}
        ${nmtBtns}${n.hbOnly ? "" : `<button class="btn small ghost" data-act="rm" title="Remove the EDS">&times;</button>`}</td></tr>`).join("");
    const emcy = co.emcy.slice().reverse().map((e) => `<tr><td class="m">${CO.fmtTime(e.ts)}</td><td>${esc(CO.chName(e.channel))}</td><td class="r">${e.node}</td>
      <td class="m">${esc(e.code_hex)}</td><td>${esc(e.text)}</td><td class="m">${hex(e.register)} <span class="muted small">${esc(e.register_bits.join(", "))}</span></td><td class="m">${esc(e.vendor)}</td></tr>`).join("");
    el.innerHTML = `<div class="card"><table class="tbl compact"><thead><tr><th class="r">Node</th><th>Channel</th><th>EDS / XDD</th><th>PDOs</th><th>NMT</th><th></th></tr></thead>
        <tbody>${nodeRows}</tbody></table>${nodes.length ? "" : `<div class="empty">Assign an EDS/XDD to a node id above. Nodes sending heartbeats show here too.</div>`}
        <p class="small muted">Click a node for its PDOs, SDO traffic and object dictionary. Start / Stop / Pre-op / Reset and SDO writes open a preview of the exact frames first.</p></div>
      ${nodePanel()}
      <div class="card" style="margin-top:10px"><div class="row"><h3 style="margin:0">EMCY log</h3><span class="spacer"></span><button class="btn small" id="emcy-clear">Clear</button></div>
        ${emcy ? `<table class="tbl compact" style="margin-top:6px"><thead><tr><th>Time</th><th>Ch</th><th class="r">Node</th><th>Code</th><th>Meaning</th><th>Error register</th><th>Vendor bytes</th></tr></thead><tbody>${emcy}</tbody></table>`
               : `<div class="empty">No emergency messages.</div>`}</div>`;
    const $ = (s) => CO.$(s, el);
    if ($("#np-close")) {
      $("#np-close").onclick = () => { nodeSel = null; nodeDet = null; renderCanopen(); };
      $("#sdo-clear").onclick = async () => { await CO.tryApi("/api/decode/clear", { what: "sdo" }); refreshDetail(); };
    }
    if ($("#np-od")) $("#np-od").ontoggle = (e) => (odOpen = e.target.open);
    if ($("#od-filter")) $("#od-filter").oninput = (e) => { odFilter = e.target.value; const pos = e.target.selectionStart; renderCanopen(); const f = CO.$("#od-filter", el); f.focus(); f.setSelectionRange(pos, pos); };
    $("#emcy-clear").onclick = async () => { await CO.tryApi("/api/decode/clear", {}); refresh(); };
  }

  // the node panel: PDOs (live), SDO log, object dictionary (collapsed at first)
  function nodePanel() {
    const s = nodeSel;
    if (!s) return "";
    const q = odFilter.toLowerCase();
    const odRows = s.eds ? od.filter((o) => !q || o.name.toLowerCase().includes(q) || hex(o.index, 4).toLowerCase().includes(q)).slice(0, 1000).map((o) => {
      const k = `${o.index}:${o.sub}`;
      return `<tr data-k="${k}"><td class="m">${hex(o.index, 4)}</td><td class="m">${hex(o.sub)}</td><td>${esc(o.name)}</td><td>${esc(o.type)}</td><td>${esc(o.access)}</td>
        <td class="m">${esc(typeof o.default === "number" && /^u(16|32|64)$/.test(o.type) ? hex(o.default, 4) : o.default ?? "")}</td><td class="m" data-v="${k}">${odVal(k)}</td>
        <td class="r"><button class="btn small" data-sdo="read">Read</button>${["ro", "const"].includes(o.access) ? "" : `<button class="btn small" data-sdo="write">Write&hellip;</button>`}</td></tr>`;
    }).join("") : "";
    return `<div class="card" style="margin-top:10px" id="node-panel"><div class="row"><h3 style="margin:0">Node ${s.node}</h3>
        <span class="muted small">${s.assigned === null ? "all channels" : "on " + esc(CO.chName(s.assigned))}</span><span class="spacer"></span>
        <button class="btn small ghost" id="np-close">Close</button></div>
      <div class="row" style="margin-top:8px"><b>PDOs</b>${s.eds ? "" : `<span class="muted small">no EDS: the frames on the predefined PDO ids, not decoded</span>`}</div>
      <div id="np-pdo">${pdoHtml()}</div>
      <div class="row" style="margin-top:10px"><b>SDO log</b><span class="muted small">transfers seen on the bus, from any master or from CANopener</span><span class="spacer"></span><button class="btn small" id="sdo-clear">Clear</button></div>
      <div id="np-sdo">${sdoHtml()}</div>
      ${s.eds ? `<details id="np-od" style="margin-top:10px" ${odOpen ? "open" : ""}><summary><b>Object dictionary</b> <span class="muted small">${od.length} objects; values from Read and from SDO traffic seen</span></summary>
        <div class="row" style="margin-top:4px"><span class="spacer"></span><input id="od-filter" placeholder="filter: name or index" value="${esc(odFilter)}" style="width:220px"></div>
        <table class="tbl compact" style="margin-top:6px"><thead><tr><th>Index</th><th>Sub</th><th>Name</th><th>Type</th><th>Access</th><th>Default</th><th>Value</th><th></th></tr></thead>
        <tbody id="od-rows">${odRows}</tbody></table></details>` : ""}</div>`;
  }

  function pdoHtml() {
    if (!nodeDet) return `<div class="empty">Loading&hellip;</div>`;
    if (!nodeDet.pdos.length) return `<div class="empty">${nodeSel.eds ? "The EDS describes no PDO." : "No frame on the predefined PDO ids (TPDO 0x180/0x280/0x380/0x480, RPDO 0x200/0x300/0x400/0x500 + node id) yet."}</div>`;
    const rows = [];
    for (const p of nodeDet.pdos) {
      const info = p.count ? [fmtPeriod(p.period_ms) || "one frame", seen(p), (p.tx ? "Tx" : "Rx") + (nodeSel.assigned === null ? " on " + esc(CO.chName(p.channel)) : ""),
        `<span class="m">${esc(p.data)}</span>`] : [seen(p)];
      rows.push(`<tr class="grp"><td colspan="7"><b>${esc(p.name)}</b> <span class="m">${hex(p.cob_id, 3)}</span>${p.source === "device" ? ` <span class="badge accent">mapping from device</span>` : ""}
        <span class="small">&middot; ${info.join(" &middot; ")}</span></td></tr>`);
      for (const f of p.fields) rows.push(`<tr><td class="m">${idxSub(f.index, f.sub)}</td><td class="m small">${f.bits === 1 ? `bit ${f.offset}` : `bits ${f.offset}..${f.offset + f.bits - 1}`}</td><td>${esc(f.name)}</td>
        <td class="m r">${esc(valText(f.value, f.text))}</td><td class="m r">${esc(f.raw ?? "")}</td><td>${esc(f.unit)}</td><td class="muted small">${esc(range(f))}</td></tr>`);
    }
    return nodeSel.eds ? table(["Index:Sub", "Position", "Name", ">Value", ">Raw", "Unit", "Range"], rows) : table(["PDO"], rows.map((r) => r.replace(' colspan="7"', "")));
  }

  function sdoHtml() {
    if (!nodeDet) return "";
    if (!nodeDet.sdo.length) return `<div class="empty">No SDO transfer seen yet.</div>`;
    const result = (e) => (e.result === "ok" ? "ok" : e.result === "abort"
      ? `<span class="badge bad">abort ${esc(e.abort_code)}</span> <span class="small">${esc(e.abort_text)} <span class="muted">(${esc(e.abort_by)})</span></span>`
      : `<span class="badge warn">${esc(e.result)}</span>`);
    const rows = nodeDet.sdo.map((e) => `<tr><td class="m">${CO.fmtTime(e.ts)}</td><td>${e.source === "bus" ? "bus" : `<span class="badge accent">CANopener</span>`}</td>
      <td>${esc(e.op)}${e.server ? ` <span class="muted small" title="extra SDO server">${esc(e.server)}</span>` : ""}</td><td class="m">${idxSub(e.index, e.sub)}</td><td>${esc(e.name)}</td><td class="m" style="white-space:normal" title="${esc(e.raw)}">${esc(valText(asHex(e.value, e.type), e.text))}</td>
      <td class="r">${e.size ?? ""}${e.segmented ? ` <span class="muted small">seg</span>` : ""}</td><td>${result(e)}</td><td class="r small">${e.ms}</td></tr>`);
    return `<div style="max-height:40vh;overflow:auto">${table(["Time", "Source", "", "Index:Sub", "Name", "Value", ">Bytes", "Result", ">ms"], rows)}</div>`;
  }

  function odVal(k) {                         // the Value column: last value read or seen, with its age and source
    const v = nodeDet && nodeDet.values[k];
    if (!v) return "";
    const [index, sub] = k.split(":").map(Number), o = od.find((x) => x.index === index && x.sub === sub);
    return `${esc(valText(asHex(v.value, o && o.type), v.text))} <span class="muted small">${fmtAge(v.age)} ago, ${v.source === "bus" ? "bus" : "CANopener"}${v.op === "write" ? " write" : ""}</span>`;
  }

  async function selectNode(tr) {
    const node = +tr.dataset.node, assigned = tr.dataset.ch === "" ? null : +tr.dataset.ch, eds = !!tr.dataset.eds;
    if (nodeSel && nodeSel.node === node && nodeSel.assigned === assigned) { nodeSel = null; nodeDet = null; renderCanopen(); return; }
    nodeSel = { node, assigned, eds, channel: nmtChannel(node, assigned) }; nodeDet = null; od = []; odFilter = "";
    const s = nodeSel;
    renderCanopen();
    if (eds) {
      const d = await CO.tryApi(`/api/decode/canopen/od?node=${node}&channel=${s.channel}`);
      if (d && nodeSel === s) od = d.objects;
    }
    await refreshDetail();
    if (nodeSel === s) { renderCanopen(); CO.$("#node-panel", tabEl)?.scrollIntoView({ block: "nearest" }); }
  }

  async function canopenAction(ev) {
    const b = ev.target.closest("button");
    if (!b) { const tr = ev.target.closest("tr[data-node]"); if (tr && !String(getSelection())) selectNode(tr); return; }
    if (b.dataset.sdo) { sdoAction(b.dataset.sdo, b.closest("tr").dataset.k); return; }
    const tr = b.closest("tr[data-node]"); if (!tr) return;
    const node = +tr.dataset.node, assigned = tr.dataset.ch === "" ? null : +tr.dataset.ch;
    const channel = nmtChannel(node, assigned);
    if (b.dataset.nmt) {
      const plan = await CO.tryApi("/api/decode/canopen/plan/nmt", { channel, node, command: b.dataset.nmt });
      if (plan) await CO.showPlan(plan);
    } else if (b.dataset.act === "pdo") {
      if (await CO.tryApi("/api/decode/canopen/pdo_mapping", { channel, node }, `PDO mapping of node ${node} read from the device`)) { await refresh(); refreshDetail(); }
    } else if (b.dataset.act === "rm") {
      if (await CO.tryApi("/api/decode/canopen/eds/remove", { node, channel: assigned })) { if (nodeSel && nodeSel.node === node) { nodeSel = null; nodeDet = null; } refresh(); }
    }
  }

  async function sdoAction(act, k) {
    const [index, sub] = k.split(":").map(Number);
    const o = od.find((x) => x.index === index && x.sub === sub);
    const body = { channel: nodeSel.channel, node: nodeSel.node, index, sub };
    if (act === "read") {
      if (await CO.tryApi("/api/decode/canopen/sdo_read", body)) refreshDetail();     // the SDO log has it (source CANopener)
      return;
    }
    const last = nodeDet && nodeDet.values[k];
    const res = await CO.modal({
      title: `Write node ${nodeSel.node} ${hex(index, 4)}:${hex(sub)} ${o ? o.name : ""}`,
      body: `<label class="field"><span>Value (${esc(o ? o.type : "")}${o && o.unit ? ", " + esc(o.unit) : ""})</span><input id="sdo-v" class="m" value="${esc(last ? last.value : o?.default ?? "")}"></label>
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

  // ---------------------------------------------------------------- EDS folders, assign dialog, auto-assign
  const idHex = (v) => (v === null || v === undefined ? "?" : hex(v, 8));
  const MATCH = { exact: ["good", "vendor + product + revision"], product: ["accent", "vendor + product"], vendor: ["", "vendor only"] };

  async function browse(kind, input, btn) {     // native picker over the app window; a browser has none
    const r = await CO.tryApi("/api/decode/browse", { kind });
    if (!r) return;
    if (!r.available) {                         // said in the dialog itself: a toast would sit under it
      btn.disabled = true; btn.title = `No ${kind} picker in a browser (only in the app window)`;
      input.placeholder = `type or paste the full ${kind} path`; input.focus(); return;
    }
    if (r.paths.length) input.value = r.paths[0];
  }

  async function foldersDialog() {
    const lib = await CO.tryApi("/api/decode/canopen/eds/folders");
    if (!lib) return;
    let folders = lib.folders.slice();
    const count = (f) => lib.files.filter((x) => x.path.toLowerCase().startsWith(f.toLowerCase())).length;
    const list = () => folders.length ? folders.map((f, i) => `<div class="row"><span class="m small" style="flex:1">${esc(f)}</span>
        <span class="muted small">${lib.folders.includes(f) ? count(f) + " file(s)" : "new"}</span><button type="button" class="btn small ghost" data-rmf="${i}">&times;</button></div>`).join("")
      : `<div class="empty">No EDS folders yet.</div>`;
    const res = await CO.modal({
      title: "EDS folders", wide: true,
      body: `<p class="small muted">The EDS / DCF / XDD files in these folders (subfolders too) are offered when you assign an EDS to a node,
          and Auto-assign picks the file whose vendor id and product code (object 0x1018) are the node's.</p>
        <div id="ef-list">${list()}</div>
        <div class="row" style="margin-top:8px"><input id="ef-path" class="m" placeholder="C:\\path\\to\\eds-folder" style="flex:1">
          <button type="button" class="btn" id="ef-browse">Browse&hellip;</button><button type="button" class="btn" id="ef-add">Add</button></div>
        ${lib.problems.map((p) => `<div class="notice warn small">${esc(p)}</div>`).join("")}`,
      buttons: [{ label: "Cancel", value: "cancel" }, { label: "Save", value: "save" }, { label: "Save and auto-assign", value: "auto", cls: "primary" }],
      onOpen: (f) => {
        const $ = (s) => CO.$(s, f), redraw = () => ($("#ef-list").innerHTML = list());
        const add = () => { const p = $("#ef-path").value.trim(); if (p && !folders.includes(p)) folders.push(p); $("#ef-path").value = ""; redraw(); };
        $("#ef-browse").onclick = async () => { await browse("folder", $("#ef-path"), $("#ef-browse")); if ($("#ef-path").value) add(); };
        $("#ef-add").onclick = add;
        $("#ef-path").onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } };
        $("#ef-list").onclick = (e) => { const b = e.target.closest("[data-rmf]"); if (b) { folders.splice(+b.dataset.rmf, 1); redraw(); } };
      },
    });
    if (!res) return;
    const pending = CO.$("#ef-path", res.form).value.trim();
    if (pending && !folders.includes(pending)) folders.push(pending);
    const r = await CO.tryApi("/api/decode/canopen/eds/folders", { folders });
    if (!r) return;
    CO.toast(`${r.files.length} EDS/XDD file(s) in ${r.folders.length} folder(s)`, r.problems.length ? "warn" : "good");
    for (const p of r.problems) CO.toast(p, "warn");
    if (res.value === "auto") await autoAssign();
  }

  // extra SDO servers: COB-ID pairs other than 0x600/0x580 + node, followed in the node's SDO log
  async function sdoServersDialog() {
    const list = () => co.sdo_servers.length ? `<table class="tbl compact"><thead><tr><th class="r">Node</th><th>Channel</th><th>Request</th><th>Response</th><th></th></tr></thead><tbody>
        ${co.sdo_servers.map((s, i) => `<tr><td class="r m">${s.node}</td><td>${esc(chLabel(s.channel))}</td><td class="m">${hex(s.request, 3)}</td><td class="m">${hex(s.response, 3)}</td>
          <td class="r"><button type="button" class="btn small ghost" data-rms="${i}">&times;</button></td></tr>`).join("")}</tbody></table>`
      : `<div class="empty">No extra SDO servers.</div>`;
    await CO.modal({
      title: "Extra SDO servers", wide: true,
      body: `<p class="small muted">SDO transfers are followed on 0x600 / 0x580 + node id. A node with a second SDO server (0x1201...) answers on other
          COB-IDs: add the pair here and its transfers show in that node's SDO log, decoded with its EDS. Nothing is sent.</p>
        <div id="ss-list">${list()}</div>
        <div class="row" style="margin-top:8px"><label class="row small">Node <input id="ss-node" type="number" min="1" max="127" value="${esc(CO.$("#co-node", tabEl).value)}" style="width:64px"></label>
          <select id="ss-ch">${chOptions()}</select>
          <label class="row small">Request <input id="ss-req" class="m" placeholder="627" style="width:70px"></label>
          <label class="row small">Response <input id="ss-resp" class="m" placeholder="5A7" style="width:70px"></label>
          <button type="button" class="btn" id="ss-add">Add</button></div>`,
      buttons: [{ label: "Close", value: "close", cls: "primary" }],
      onOpen: (f) => {
        const $ = (s) => CO.$(s, f);
        const reload = async () => { const d = await CO.tryApi("/api/decode/canopen"); if (d) co = d; $("#ss-list").innerHTML = list(); renderCanopen(); };
        $("#ss-add").onclick = async () => {
          const ch = $("#ss-ch").value, body = { node: +$("#ss-node").value, request: $("#ss-req").value.trim(), response: $("#ss-resp").value.trim(), channel: ch === "" ? null : +ch };
          if (!body.request || !body.response) { CO.toast("Give the request and response COB-IDs (hex)", "warn"); return; }
          if (await CO.tryApi("/api/decode/canopen/sdo_server", body)) { $("#ss-req").value = ""; $("#ss-resp").value = ""; reload(); }
        };
        $("#ss-list").onclick = async (e) => {
          const b = e.target.closest("[data-rms]"); if (!b) return;
          const s = co.sdo_servers[+b.dataset.rms];
          if (await CO.tryApi("/api/decode/canopen/sdo_server/remove", { node: s.node, request: s.request, channel: s.channel })) reload();
        };
      },
    });
  }

  async function autoAssign() {
    const r = await CO.tryApi("/api/decode/canopen/eds/auto", {});
    if (!r) return;
    if (!r.folders.length) { CO.toast("Add the folders with your EDS files first", "warn"); return foldersDialog(); }
    refresh();
    const rows = r.results.map((x) => `<tr><td class="r m">${x.node}</td><td>${esc(CO.chName(x.channel))}</td>
      <td><span class="badge ${x.result === "assigned" ? "good" : x.result === "has EDS" ? "" : "warn"}">${esc(x.result)}</span></td>
      <td class="small">${x.path ? `<span class="m" title="${esc(x.path)}">${esc(x.path.split(/[\\/]/).pop())}</span>` : ""}
        ${x.match === "product" ? ` <span class="muted">(revision differs or not given)</span>` : ""}
        ${x.files && x.files.length ? x.files.map((p) => esc(p.split(/[\\/]/).pop())).join(", ") + ` <span class="muted">- right-click the node to pick one</span>` : ""}
        ${x.identity && !x.path && !(x.files && x.files.length) ? `<span class="muted">${esc(`vendor ${idHex(x.identity.vendor)}, product ${idHex(x.identity.product)}`)}</span>` : ""}
        ${x.note ? `<span class="muted">${esc(x.note)}</span>` : ""}</td></tr>`).join("");
    await CO.modal({
      title: "Auto-assign EDS files", wide: true,
      body: `<p class="small muted">${r.files} file(s) in ${r.folders.length} EDS folder(s). Nodes come from heartbeats and the last scan (Nodes tab); their identity from that scan or an SDO read of 0x1018.</p>
        ${rows ? `<table class="tbl compact"><thead><tr><th class="r">Node</th><th>Channel</th><th>Result</th><th>File</th></tr></thead><tbody>${rows}</tbody></table>`
               : `<div class="empty">No node seen yet: a node shows up when its heartbeat is heard, or after a scan in the Nodes tab.</div>`}
        ${r.problems.map((p) => `<div class="notice warn small">${esc(p)}</div>`).join("")}`,
    });
  }

  /* The assign dialog of one node: the files of the EDS folders (the ones that fit the node's identity first),
     or any other file by path, Browse or Upload. Also used by the Nodes tab's row menu. */
  CO.edsAssignDialog = async (node, channel, assigned) => {
    const d = await CO.tryApi("/api/decode/canopen/eds/candidates", { node, channel });
    if (!d) return;
    const id = d.identity, ch = assigned === undefined ? channel : assigned;
    const rows = d.files.slice(0, 500).map((f) => `<tr data-path="${esc(f.path)}"><td><input type="radio" name="eds-pick" value="${esc(f.path)}" ${f.path === d.best ? "checked" : ""}></td>
      <td>${f.match ? `<span class="badge ${MATCH[f.match][0]}">${MATCH[f.match][1]}</span>` : ""}</td>
      <td class="m small" title="${esc(f.path)}">${esc(f.name)}</td><td>${esc(f.product_name || "")}</td>
      <td class="m small">${idHex(f.vendor)}</td><td class="m small">${idHex(f.product)}</td><td class="m small">${idHex(f.revision)}</td></tr>`).join("");
    let file = null;
    const res = await CO.modal({
      title: `Assign an EDS file to node ${node}`, wide: true,
      body: `<div class="small">${id.error ? `<div class="notice warn small">Identity unknown: ${esc(id.error)}</div>`
          : `Node ${node}: vendor <b class="m">${idHex(id.vendor)}</b>, product <b class="m">${idHex(id.product)}</b>, revision <b class="m">${idHex(id.revision)}</b> <span class="muted">(${esc(id.source)})</span>`}</div>
        ${d.folders.length ? `<div class="row" style="margin-top:8px"><b>Files in the EDS folders</b><span class="spacer"></span><input id="ea-filter" placeholder="filter" style="width:200px"></div>
          ${rows ? `<div style="max-height:45vh;overflow:auto"><table class="tbl compact"><thead><tr><th></th><th>Fits</th><th>File</th><th>Product</th><th>Vendor</th><th>Product code</th><th>Revision</th></tr></thead><tbody id="ea-rows">${rows}</tbody></table></div>`
                 : `<div class="empty">No EDS/XDD file in the EDS folders.</div>`}`
          : `<div class="notice small">No EDS folders yet: with <b>EDS folders</b> the files there are listed here, the ones that fit this node first.</div>`}
        ${d.problems.map((p) => `<div class="notice warn small">${esc(p)}</div>`).join("")}
        <div class="row" style="margin-top:8px"><span class="small">Other file</span><input id="ea-path" class="m" placeholder="C:\\path\\device.eds or .xdd" style="flex:1">
          <button type="button" class="btn" id="ea-browse">Browse&hellip;</button><label class="btn">Upload&hellip;<input type="file" id="ea-file" accept=".eds,.dcf,.xdd" hidden></label></div>
        <label class="field" style="margin-top:8px"><span>Channel</span><select id="ea-ch">${chOptions()}</select></label>`,
      buttons: [{ label: "Cancel", value: "cancel" }, { label: "EDS folders\u2026", value: "folders" }, { label: "Assign", value: "ok", cls: "primary" }],
      validate: (v, f) => v === "ok" && !file && !CO.$("#ea-path", f).value.trim() && !CO.$('input[name="eds-pick"]:checked', f) ? "Pick a file" : "",
      onOpen: (f) => {
        const $ = (s) => CO.$(s, f);
        $("#ea-ch").value = ch === null || ch === undefined ? "" : String(ch);
        $("#ea-browse").onclick = async () => { file = null; await browse("file", $("#ea-path"), $("#ea-browse")); };
        $("#ea-path").oninput = () => (file = null);
        $("#ea-file").onchange = (e) => { file = e.target.files[0] || null; if (file) $("#ea-path").value = file.name; };
        if ($("#ea-filter")) $("#ea-filter").oninput = (e) => { const q = e.target.value.toLowerCase(); CO.$$("#ea-rows tr", f).forEach((tr) => (tr.hidden = !!q && !tr.textContent.toLowerCase().includes(q))); };
        if ($("#ea-rows")) $("#ea-rows").onclick = (e) => { const tr = e.target.closest("tr[data-path]"); if (tr) { CO.$("input", tr).checked = true; $("#ea-path").value = ""; file = null; } };
      },
    });
    if (!res) return;
    if (res.value === "folders") { await foldersDialog(); return CO.edsAssignDialog(node, channel, assigned); }
    const f = res.form, chv = CO.$("#ea-ch", f).value, typed = CO.$("#ea-path", f).value.trim();
    const pick = CO.$('input[name="eds-pick"]:checked', f);
    const ok = file ? await upload("/api/decode/canopen/eds/upload", file, { node, channel: chv })
      : await CO.tryApi("/api/decode/canopen/eds", { node, path: typed || pick.value, channel: chv === "" ? null : +chv });
    if (ok) { CO.toast(`EDS assigned to node ${node}`, "good"); refresh(); }
  };

  // ---------------------------------------------------------------- J1939
  function renderJ1939() {
    const el = CO.$("#dec-body", tabEl);
    const chans = CO.state.channels.map((c) => `<label class="row small"><input type="checkbox" data-j1939="${c.index}" ${j1939.channels.includes(c.index) ? "checked" : ""}> ${esc(c.name)}</label>`).join("");
    // every source address: the ones with an address claim (NAME decoded) and the ones only seen sending
    const srcs = new Map();
    for (const e of j1939.ecus) srcs.set(`${e.channel}:${e.sa}`, { channel: e.channel, sa: e.sa, ecu: e });
    for (const x of j1939.sources) { const k = `${x.channel}:${x.sa}`; srcs.set(k, { ...(srcs.get(k) || { channel: x.channel, sa: x.sa }), seen: x }); }
    const list = [...srcs.values()].sort((a, b) => a.channel - b.channel || a.sa - b.sa);
    if (jSel && !srcs.has(`${jSel.ch}:${jSel.sa}`)) { jSel = null; jDet = null; }
    const ecus = list.map(({ channel, sa, ecu: e, seen: x }) => `<tr data-sa="${sa}" data-ch="${channel}" class="pick${jSel && jSel.sa === sa && jSel.ch === channel ? " sel" : ""}">
      <td>${esc(CO.chName(channel))}</td><td class="m">${hex(sa)}</td><td class="r">${x ? x.frames : ""}</td><td>${x ? fmtAge(x.age) + " ago" : ""}</td>
      ${e ? `<td class="m">${esc(e.name)}</td><td class="r m">${e.identity}</td><td class="r m">${e.manufacturer}</td><td class="r m">${e.function}</td><td class="r">${e.ecu_instance}</td><td class="r">${e.function_instance}</td>
        <td class="r">${e.vehicle_system}</td><td>${esc(e.industry || e.industry_group)}</td><td>${e.arbitrary_address ? "yes" : "no"}</td><td class="m">${CO.fmtTime(e.ts)}</td>`
        : `<td colspan="10" class="muted">no address claim seen</td>`}</tr>`).join("");
    const lamp = (n, v) => `<span class="badge ${v === 1 ? "warn" : ""}" title="${n}: ${["off", "on", "error", "n/a"][v] || v}">${n}</span>`;
    const dm1 = j1939.dm1.map((d) => `<tr><td>${esc(CO.chName(d.channel))}</td><td class="m">${hex(d.sa)}</td><td>${Object.entries(d.lamps).map(([n, v]) => lamp(n, v)).join(" ")}</td>
      <td class="m">${d.dtcs.length ? d.dtcs.map((x) => `SPN ${x.spn} FMI ${x.fmi} OC ${x.oc}`).map(esc).join("<br>") : `<span class="muted">no active DTC</span>`}</td><td class="m">${CO.fmtTime(d.ts)}</td></tr>`).join("");
    const msgs = j1939.messages.slice().reverse().map((m) => `<tr><td class="m">${CO.fmtTime(m.ts)}</td><td>${esc(CO.chName(m.channel))}</td><td class="m">${hex(m.sa)}</td><td class="m">${hex(m.da)}</td>
      <td class="r m">${m.pgn}</td><td>${esc(m.name)}</td><td>${esc(m.kind)}</td><td class="r">${m.size}</td><td class="m small" style="white-space:normal">${esc(m.decoded || m.data)}</td></tr>`).join("");
    el.innerHTML = `<div class="card"><div class="row"><b>J1939 decoding on</b> ${chans || `<span class="muted">no channel connected</span>`}</div>
        <p class="small muted">Splits 29-bit ids into priority / PGN / DA / SA, collects address claims, reassembles TP (RTS/CTS and BAM) and decodes DM1. A J1939 DBC (VFrameFormat J1939PG) decodes the signals by PGN.</p></div>
      <div class="card" style="margin-top:10px"><div class="row"><h3 style="margin:0">ECUs (source addresses)</h3><span class="muted small">click one for the PGNs it sends</span></div>
        ${ecus ? `<table class="tbl compact" style="margin-top:6px"><thead><tr><th>Ch</th><th>SA</th><th class="r">Frames</th><th>Last frame</th><th>NAME</th><th class="r">Identity</th><th class="r">Manufacturer</th><th class="r">Function</th><th class="r">ECU inst</th><th class="r">Fn inst</th><th class="r">Vehicle system</th><th>Industry</th><th>Arbitrary addr</th><th>Claim seen</th></tr></thead><tbody>${ecus}</tbody></table>`
               : `<div class="empty">No address claim or J1939 frame seen.</div>`}</div>
      ${jSel ? `<div class="card" style="margin-top:10px" id="j-panel"><div class="row"><h3 style="margin:0">SA ${hex(jSel.sa)}</h3><span class="muted small">on ${esc(CO.chName(jSel.ch))}</span><span class="spacer"></span>
        <button class="btn small ghost" id="j-close">Close</button></div><div id="j-detail">${jDetailHtml()}</div></div>` : ""}
      <div class="card" style="margin-top:10px"><h3>DM1 active diagnostic trouble codes</h3>${dm1 ? `<table class="tbl compact"><thead><tr><th>Ch</th><th>SA</th><th>Lamps</th><th>DTCs</th><th>Seen</th></tr></thead><tbody>${dm1}</tbody></table>` : `<div class="empty">No DM1 seen.</div>`}</div>
      <div class="card" style="margin-top:10px"><div class="row"><h3 style="margin:0">Transport protocol messages</h3><span class="spacer"></span><button class="btn small" id="tp-clear">Clear</button></div>
        ${msgs ? `<table class="tbl compact" style="margin-top:6px"><thead><tr><th>Time</th><th>Ch</th><th>SA</th><th>DA</th><th class="r">PGN</th><th>Name</th><th>Kind</th><th class="r">Bytes</th><th>Decoded / data</th></tr></thead><tbody>${msgs}</tbody></table>` : `<div class="empty">No reassembled message yet.</div>`}</div>`;
    el.querySelectorAll("[data-j1939]").forEach((cb) => (cb.onchange = async () => {
      if (!await CO.tryApi("/api/decode/j1939", { channel: +cb.dataset.j1939, enabled: cb.checked })) cb.checked = !cb.checked; else refresh();
    }));
    CO.$("#tp-clear", el).onclick = async () => { await CO.tryApi("/api/decode/clear", {}); refresh(); };
    if (CO.$("#j-close", el)) CO.$("#j-close", el).onclick = () => { jSel = null; jDet = null; renderJ1939(); };
  }

  function jDetailHtml() {
    const d = jDet;
    if (!d) return `<div class="empty">Loading&hellip;</div>`;
    const e = d.ecu;
    const head = e ? `NAME <span class="m">${esc(e.name)}</span> &middot; manufacturer ${e.manufacturer} &middot; function ${e.function} &middot; ${esc(e.industry || e.industry_group)}`
      : `<span class="muted">no address claim seen</span>`;
    const sig = (x) => `${esc(x.name)} = <b>${esc(valText(x.value, x.text))}</b>${x.unit ? " " + esc(x.unit) : ""}`;
    const rows = d.pgns.map((p) => `<tr><td class="r m">${p.pgn}</td><td>${esc(p.name)}</td><td class="r">${p.priority}</td><td class="m">${p.da === 0xFF ? "global" : hex(p.da)}</td>
      <td class="r m">${fmtPeriod(p.period_ms)}</td><td>${seen(p)}${p.tx ? ` <span class="muted small">Tx</span>` : ""}</td><td class="m">${esc(p.data)}</td>
      <td class="small" style="white-space:normal">${p.message ? `<span class="muted">${esc(p.message)}:</span> ${p.signals.map(sig).join(", ")}` : ""}</td></tr>`);
    const tp = d.tp.map((m) => `<tr><td class="m">${CO.fmtTime(m.ts)}</td><td class="m">${hex(m.da)}</td><td class="r m">${m.pgn}</td><td>${esc(m.name)}</td><td>${esc(m.kind)}</td>
      <td class="r">${m.size}</td><td class="m small" style="white-space:normal">${esc(m.decoded || m.data)}</td></tr>`);
    return `<div class="small">${head}</div>
      ${rows.length ? table([">PGN", "Name", ">Prio", "DA", ">Period", "Last seen", "Data (last frame)", "Decoded (J1939 DBC)"], rows) : `<div class="empty">No frame from this address on the J1939 channels.</div>`}
      ${tp.length ? `<div class="row" style="margin-top:10px"><b>Transport protocol messages</b></div>${table(["Time", "DA", ">PGN", "Name", "Kind", ">Bytes", "Decoded / data"], tp)}` : ""}`;
  }

  async function selectSource(tr) {
    const sa = +tr.dataset.sa, ch = +tr.dataset.ch;
    jSel = jSel && jSel.sa === sa && jSel.ch === ch ? null : { sa, ch }; jDet = null;
    renderJ1939();
    if (jSel) { await refreshDetail(); CO.$("#j-panel", tabEl)?.scrollIntoView({ block: "nearest" }); }
  }

  function bodyClick(ev) {                    // a click on a row selects it (opens its detail panel)
    if (sub === "canopen") { canopenAction(ev); return; }
    if (ev.target.closest("button, input, select, a") || String(getSelection())) return;
    const tr = ev.target.closest(sub === "dbc" ? "tr[data-msg]" : "tr[data-sa]");
    if (tr) { if (sub === "dbc") selectDbc(tr.dataset.msg); else selectSource(tr); }
  }

  function tableMenu(ev) {
    const td = ev.target.closest("td"), tr = td && td.closest("tbody tr");
    if (!tr || td.classList.contains("empty")) return null;
    const cell = td.querySelector("button") ? "" : td.textContent.replace(/\s+/g, " ").trim(), k = tr.dataset.k;
    const nodeTr = sub === "canopen" && tr.closest("tr[data-node]");
    const assigned = nodeTr && nodeTr.dataset.ch !== "" ? +nodeTr.dataset.ch : null;
    const odRow = sub === "canopen" && nodeSel && nodeSel.eds && k && tr.closest("#od-rows");
    return [
      nodeTr && { label: "Assign EDS file...", action: () => CO.edsAssignDialog(+nodeTr.dataset.node, nmtChannel(+nodeTr.dataset.node, assigned), assigned) },
      nodeTr && { separator: true },
      { label: "Copy value", disabled: !cell, action: () => CO.copyText(cell, "value") },
      { label: "Copy row", action: () => CO.copyText(CO.tableText([tr]), "1 row") },
      { label: "Copy table", action: () => { const trs = CO.$$("tr", tr.closest("tbody")); CO.copyText(CO.tableText(trs), `${trs.length} rows`); } },
      odRow && { separator: true },
      odRow && { label: "Read from the node (SDO)", action: () => sdoAction("read", k) },
    ];
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
            <button class="btn primary" id="co-assign">Assign</button><label class="btn">Upload&hellip;<input type="file" id="co-file" accept=".eds,.dcf,.xdd" hidden></label>
            <span class="sep"></span><button class="btn" id="co-folders" title="Folders searched for EDS/DCF/XDD files">EDS folders&hellip;</button>
            <button class="btn" id="co-auto" title="Assign the file of the EDS folders that fits each node without an EDS (vendor id + product code of 0x1018)">Auto-assign</button>
            <button class="btn" id="co-sdo" title="Extra SDO server COB-ID pairs of a node, followed in its SDO log">SDO servers&hellip;</button></span>
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
      $("#co-folders").onclick = foldersDialog;
      $("#co-auto").onclick = autoAssign;
      $("#co-sdo").onclick = sdoServersDialog;
      $("#co-file").onchange = async (e) => { const f = e.target.files[0]; if (f) await edsAssign(f); e.target.value = ""; };
      $("#dec-body").addEventListener("click", bodyClick);
      CO.onContextMenu($("#dec-body"), tableMenu);
    },
    async onShow() { await loadDbc(); renderSub(); },
  });

  // ---------------------------------------------------------------- polling
  setInterval(refresh, 1000);
  setInterval(refreshDetail, 500);
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
