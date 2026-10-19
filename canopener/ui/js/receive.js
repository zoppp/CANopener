/* Receive / Transmit tab: the per-ID receive list (top) and the transmit list (bottom), like PCAN-View. */
"use strict";
(function () {
  const CO = window.canopener, F = CO.F;
  const rows = new Map();          // key -> {r: row array, chg: [byte change times], seen}
  let sortKey = CO.prefs.get("rx.sort", "id"), sortDir = 1, frozen = false, rxSel = null, txRows = null;
  let shown = [], shownMap = new Map();          // the rows as last drawn: menus act on those (Freeze keeps them)
  let chFilter = "all", idFilter = "", showTx = CO.prefs.get("rx.showTx", false);
  let tabEl = null, txEntries = [];
  CO.receiveRows = rows;           // decoders / plugins may read it

  // row layout from the server: [key, ch, id, flags, dlc, data, prev, count, cycle, min, max, t_last]
  function applyFeed(msg) {
    if (!msg.rows) return;
    const live = new Set(CO.state.channels.map((c) => String(c.index)));
    for (const [ch, entry] of Object.entries(msg.rows)) {
      if (entry.full) for (const [k, v] of rows) if (String(v.r[1]) === ch) rows.delete(k);
      const now = performance.now();
      for (const r of entry.rows) {
        const old = rows.get(r[0]);
        const chg = old ? old.chg : [];
        if (old && old.r[5] !== r[5]) {
          for (let i = 0; i < r[5].length; i += 2) if (old.r[5].substr(i, 2) !== r[5].substr(i, 2)) chg[i / 2] = now;
        }
        rows.set(r[0], { r, chg });
      }
    }
    for (const [k, v] of rows) if (!live.has(String(v.r[1]))) rows.delete(k);
  }

  function matchId(id) {
    if (!idFilter) return true;
    return idFilter.split(",").some((part) => {
      part = part.trim(); if (!part) return false;
      const [a, b] = part.split("-").map(CO.parseHexId);
      return b === undefined || isNaN(b) ? id === a : id >= Math.min(a, b) && id <= Math.max(a, b);
    });
  }

  function sorted() {
    const list = [];
    for (const v of rows.values()) {
      const r = v.r;
      if (!showTx && r[3] & F.TX) continue;
      if (chFilter !== "all" && String(r[1]) !== chFilter) continue;
      if (!matchId(r[2])) continue;
      list.push(v);
    }
    const key = { id: (r) => r[1] * 1e10 + (r[3] & F.EXT ? 1e9 : 0) + r[2], count: (r) => r[7], cycle: (r) => r[8], dlc: (r) => r[4] }[sortKey] || ((r) => r[2]);
    list.sort((a, b) => (key(a.r) - key(b.r)) * sortDir);
    return list;
  }

  function dataHtml(v) {
    const r = v.r, hex = r[5], now = performance.now();
    if (r[3] & F.RTR) return `<span class="muted">remote request</span>`;
    let out = "";
    for (let i = 0; i < hex.length; i += 2) {
      const t = v.chg[i / 2];
      out += (i ? " " : "") + (t && now - t < 1500 ? `<span class="c">${hex.substr(i, 2)}</span>` : hex.substr(i, 2));
    }
    return out;
  }

  function renderRx() {
    if (!tabEl || tabEl.hidden || frozen || rxSel.busy) return;     // busy: a mouse drag is selecting rows
    const list = sorted();
    shown = list.map((v) => ({ r: v.r.slice() }));
    shownMap = new Map(shown.map((v) => [v.r[0], v]));
    const multi = CO.state.channels.length > 1;
    const dec = CO.decodedText && Object.keys(CO.decodedText).length > 0;     // filled by decode.js
    const body = list.map((v) => {
      const r = v.r;
      const cls = (rxSel.has(r[0]) ? "sel " : "") + (r[3] & F.ERR ? "err" : "");
      return `<tr data-k="${r[0]}" class="${cls}">${multi ? `<td>${CO.esc(CO.chName(r[1]))}</td>` : ""}` +
        `<td class="m ${r[3] & F.TX ? "tx" : ""}">${CO.idText(r[2], r[3])}${r[3] & F.TX ? " Tx" : ""}</td><td>${CO.typeText(r[3])}</td><td class="r m">${r[4]}</td>` +
        `<td class="m data">${dataHtml(v)}</td><td class="r m">${CO.fmtMs(r[8])}</td><td class="r m">${CO.fmtNum(r[7])}</td>` +
        `<td class="r m muted">${CO.fmtMs(r[9])}</td><td class="r m muted">${CO.fmtMs(r[10])}</td>` +
        (dec ? `<td class="dec" title="${CO.esc(CO.decodedText[r[0]] || "")}">${CO.esc(CO.decodedText[r[0]] || "")}</td>` : "") + `</tr>`;
    }).join("");
    const t = CO.$("#rx-table", tabEl);
    const head = `<tr>${multi ? "<th>Ch</th>" : ""}<th class="sort" data-s="id">ID</th><th>Type</th><th class="sort r" data-s="dlc">DLC</th><th>Data</th>` +
      `<th class="sort r" data-s="cycle" title="Last interval between two frames">Cycle ms</th><th class="sort r" data-s="count">Count</th><th class="r">Min ms</th><th class="r">Max ms</th>${dec ? "<th>Decoded</th>" : ""}</tr>`;
    if (t._head !== head) { t.tHead.innerHTML = head; t._head = head; }
    t.tBodies[0].innerHTML = body || "";
    CO.$("#rx-empty", tabEl).hidden = list.length > 0;
    CO.$("#rx-count", tabEl).textContent = `${list.length} id${list.length === 1 ? "" : "s"}`;
  }

  // ---------------------------------------------------------------- transmit list
  CO.onProject(() => CO.reloadTx());
  CO.reloadTx = async () => {
    const d = await CO.tryApi("/api/tx");
    if (d) { txEntries = d.entries; renderTx(); }
  };

  function renderTx() {
    if (!tabEl) return;
    const multi = CO.liveChannels().length > 1 || txEntries.some((e) => e.channel !== 0);
    const html = txEntries.map((e) => {
      const live = CO.state.txLive[e.id] || e;
      const flags = (e.ext ? F.EXT : 0) | (e.fd ? F.FD : 0) | (e.brs ? F.BRS : 0) | (e.rtr ? F.RTR : 0);
      const hex = CO.dataHex(e.data);                     // exactly what the server sends
      const data = e.rtr ? `<span class="muted">remote, DLC ${e.dlc}</span>` : CO.esc(hex === null ? e.data : CO.spaced(hex));
      const nbytes = hex === null ? "?" : hex.length / 2;
      return `<tr data-tx="${e.id}" class="${txRows.has(e.id) ? "sel" : ""}">
        <td><input type="checkbox" data-act="cyc" ${live.enabled ? "checked" : ""} ${e.period_ms > 0 ? "" : "disabled"} title="${e.period_ms > 0 ? "Send cyclically" : "Set a cycle time to send cyclically"}"></td>
        ${multi ? `<td>${CO.esc(CO.chName(e.channel))}</td>` : ""}
        <td class="m">${CO.idText(e.can_id, flags)}</td><td>${CO.typeText(flags)}</td><td class="r m">${e.rtr ? e.dlc : nbytes}</td>
        <td class="m">${data}</td><td class="r m">${e.period_ms > 0 ? e.period_ms : "manual"}</td>
        <td class="r m">${CO.fmtNum(live.count)}</td><td>${CO.esc(e.comment || "")}${live.error ? ` <span class="badge bad" title="${CO.esc(live.error)}">error</span>` : ""}</td>
        <td class="r"><button class="btn small" data-act="send" title="Send once (Space)">Send</button>
          <button class="btn small ghost" data-act="edit">Edit</button><button class="btn small ghost" data-act="del" title="Delete">&times;</button></td></tr>`;
    }).join("");
    const t = CO.$("#tx-table", tabEl);
    t.tHead.innerHTML = `<tr><th title="Cyclic">On</th>${multi ? "<th>Ch</th>" : ""}<th>ID</th><th>Type</th><th class="r">DLC</th><th>Data</th><th class="r">Cycle ms</th><th class="r">Count</th><th>Comment</th><th></th></tr>`;
    t.tBodies[0].innerHTML = html;
    CO.$("#tx-empty", tabEl).hidden = txEntries.length > 0;
  }

  function updateTxLive() {
    if (!tabEl || tabEl.hidden) return;
    for (const tr of CO.$$("#tx-table tbody tr", tabEl)) {
      const live = CO.state.txLive[+tr.dataset.tx];
      if (!live) continue;
      const cb = tr.querySelector("[data-act=cyc]");
      if (cb && cb !== document.activeElement) cb.checked = live.enabled;
      const cells = tr.children;
      const countCell = cells[cells.length - 3];
      const txt = CO.fmtNum(live.count);
      if (countCell.textContent !== txt) countCell.textContent = txt;
    }
  }

  CO.editTx = async (entry) => {
    const e = entry || { can_id: 0x100, data: "00 00 00 00 00 00 00 00", ext: false, fd: false, brs: false, rtr: false, dlc: 0, period_ms: 0, channel: (CO.liveChannels()[0] || { index: 0 }).index, comment: "" };
    const chans = CO.state.channels.length ? CO.state.channels : [{ index: 0, name: "CAN1" }];
    const res = await CO.modal({
      title: entry && entry.id ? "Edit transmit entry" : "New transmit entry",
      body: `<div class="grid3">
        <label class="field"><span>ID (hex)</span><input id="t-id" class="m" value="${CO.idText(e.can_id, e.ext ? F.EXT : 0)}"></label>
        <label class="field"><span>Channel</span><select id="t-ch">${chans.map((c) => `<option value="${c.index}" ${c.index === e.channel ? "selected" : ""}>${CO.esc(c.name || "CAN" + (c.index + 1))}</option>`).join("")}</select></label>
        <label class="field"><span>Cycle time ms (0 = manual)</span><input id="t-per" type="number" min="0" step="1" value="${e.period_ms || 0}"></label></div>
        <div class="row"><label class="row"><input type="checkbox" id="t-ext" ${e.ext ? "checked" : ""}> Extended (29-bit)</label>
          <label class="row"><input type="checkbox" id="t-fd" ${e.fd ? "checked" : ""}> CAN FD</label>
          <label class="row"><input type="checkbox" id="t-brs" ${e.brs ? "checked" : ""}> Bit rate switch</label>
          <label class="row"><input type="checkbox" id="t-rtr" ${e.rtr ? "checked" : ""}> Remote request</label></div>
        ${CO.dbcTx ? CO.dbcTx.html(e) : ""}
        <label class="field"><span>Data (hex bytes)</span><input id="t-data" class="m" value="${CO.esc(e.data || "")}" placeholder="11 22 33 44"></label>
        <label class="field" id="t-dlc-f"><span>Remote request DLC</span><input id="t-dlc" type="number" min="0" max="8" value="${e.dlc || 0}"></label>
        <label class="field"><span>Comment</span><input id="t-com" value="${CO.esc(e.comment || "")}"></label>`,
      buttons: [{ label: "Cancel", value: "cancel" }, { label: entry && entry.id ? "Save" : "Add", value: "ok", cls: "primary" }],
      onOpen: (f) => {
        const upd = () => { CO.$("#t-dlc-f", f).hidden = !CO.$("#t-rtr", f).checked; CO.$("#t-brs", f).disabled = !CO.$("#t-fd", f).checked; };
        f.addEventListener("change", upd); upd(); CO.$("#t-data", f).focus();
        if (CO.dbcTx) CO.dbcTx.open(f);
      },
      validate: (v, f) => isNaN(CO.parseHexId(CO.$("#t-id", f).value)) ? "ID must be hex" : "",
    });
    if (!res) return;
    const f = res.form, $ = (s) => CO.$(s, f);
    const body = { can_id: CO.$("#t-id", f).value.trim(), channel: +$("#t-ch").value, period_ms: +$("#t-per").value || 0,
      ext: $("#t-ext").checked, fd: $("#t-fd").checked, brs: $("#t-brs").checked, rtr: $("#t-rtr").checked,
      data: $("#t-data").value, dlc: +$("#t-dlc").value || 0, comment: $("#t-com").value };
    if (CO.dbcTx) Object.assign(body, CO.dbcTx.read(f));
    const ok = entry && entry.id ? await CO.tryApi(`/api/tx/${entry.id}`, body, null, "PATCH") : await CO.tryApi("/api/tx", body);
    if (ok) CO.reloadTx();
  };

  async function txAction(act, id, el) {
    const e = txEntries.find((x) => x.id === id);
    if (!e) return;
    if (act === "send") { await CO.tryApi(`/api/tx/${id}/send`, {}); }
    else if (act === "edit") CO.editTx(e);
    else if (act === "del") { if (await CO.tryApi(`/api/tx/${id}`, undefined, null, "DELETE")) CO.reloadTx(); }
    else if (act === "cyc") {
      const r = await CO.tryApi(`/api/tx/${id}`, { enabled: el.checked }, null, "PATCH");
      if (!r) el.checked = !el.checked; else CO.state.txLive[id] = { ...(CO.state.txLive[id] || {}), ...r };
    }
  }

  async function setCyclic(id, on) {
    const r = await CO.tryApi(`/api/tx/${id}`, { enabled: on }, null, "PATCH");
    if (r) { CO.state.txLive[id] = { ...(CO.state.txLive[id] || {}), ...r }; updateTxLive(); }
  }

  async function deleteTx(list) {
    if (list.length > 1 && !(await CO.confirm("Delete transmit entries", `Delete ${list.length} transmit entries?`, "Delete", true))) return;
    for (const e of list) if (!(await CO.tryApi(`/api/tx/${e.id}`, undefined, null, "DELETE"))) break;
    CO.reloadTx();
  }

  async function duplicateTx(e) {
    const copy = { ...e, enabled: false };
    delete copy.id; delete copy.count; delete copy.error;
    if (await CO.tryApi("/api/tx", copy)) CO.reloadTx();
  }

  const txFlags = (e) => (e.ext ? F.EXT : 0) | (e.fd ? F.FD : 0) | (e.brs ? F.BRS : 0) | (e.rtr ? F.RTR : 0);
  function txText(keys) {                                 // columns that CO.parseFrames reads back
    return ["Ch\tID\tType\tDLC\tData\tCycle ms\tComment"].concat(keys.map((id) => txEntries.find((e) => e.id === id)).filter(Boolean).map((e) => {
      const hex = CO.dataHex(e.data) || "";
      return [CO.chName(e.channel), CO.idText(e.can_id, txFlags(e)), CO.typeText(txFlags(e)), e.rtr ? e.dlc : hex.length / 2,
        e.rtr ? "remote request" : CO.spaced(hex), e.period_ms > 0 ? e.period_ms : "manual", (e.comment || "").replace(/\s+/g, " ")].join("\t");
    })).join("\n");
  }

  /* Pasted lines of "ID data" (or copied receive / trace / transmit rows) become manual transmit entries after a
     confirmation. Nothing is sent. */
  async function pasteFrames(text) {
    const { frames, bad } = CO.parseFrames(text);
    if (!frames.length) {
      CO.toast(bad.length ? `No frames in the pasted text (${bad.length} line${bad.length === 1 ? "" : "s"} not understood). Paste lines like 18FEF100 FF 80 20 00` : "Nothing to paste", "warn");
      return;
    }
    const MAX = 200, list = frames.slice(0, MAX);
    const known = (c) => c !== null && CO.state.channels.some((x) => x.index === c);
    const chans = CO.state.channels.length ? CO.state.channels : [{ index: 0, name: "CAN1" }];
    const dflt = (CO.liveChannels()[0] || chans[0]).index;
    const res = await CO.modal({
      title: `Add ${list.length} pasted frame${list.length === 1 ? "" : "s"} to the transmit list`,
      body: `<p class="small muted">They are added as manual entries (no cycle time). Nothing is sent until you click Send.</p>
        ${frames.length > MAX ? `<div class="notice warn">Only the first ${MAX} of ${frames.length} frames are added.</div>` : ""}
        ${bad.length ? `<div class="notice warn">${bad.length} line${bad.length === 1 ? "" : "s"} not understood, left out: <span class="mono small">${CO.esc(bad.slice(0, 3).join("  |  "))}${bad.length > 3 ? " ..." : ""}</span></div>` : ""}
        <label class="field"><span>Channel for frames without a known channel</span><select id="pf-ch">${chans.map((c) => `<option value="${c.index}" ${c.index === dflt ? "selected" : ""}>${CO.esc(c.name)}</option>`).join("")}</select></label>
        <div class="scroll" style="max-height:45vh"><table class="tbl compact"><thead><tr><th>Ch</th><th>ID</th><th>Type</th><th class="r">DLC</th><th>Data</th></tr></thead><tbody>
        ${list.map((f) => `<tr><td>${known(f.channel) ? CO.esc(CO.chName(f.channel)) : `<span class="muted">(channel above)</span>`}</td><td class="m">${CO.idText(f.can_id, txFlags(f))}</td>
          <td>${CO.typeText(txFlags(f))}</td><td class="r m">${f.dlc}</td><td class="m">${f.rtr ? "remote request" : CO.esc(f.data)}</td></tr>`).join("")}</tbody></table></div>`,
      buttons: [{ label: "Cancel", value: "cancel" }, { label: `Add ${list.length}`, value: "ok", cls: "primary" }],
    });
    if (!res) return;
    const ch = +CO.$("#pf-ch", res.form).value;
    let added = 0;
    for (const f of list) {
      if (!(await CO.tryApi("/api/tx", { ...f, channel: known(f.channel) ? f.channel : ch, period_ms: 0, enabled: false, comment: "" }))) break;
      added++;
    }
    if (added) CO.toast(`Added ${added} transmit entr${added === 1 ? "y" : "ies"}`, added === list.length ? "good" : "warn");
    CO.reloadTx();
  }
  const pasteItem = () => ({ label: "Paste frames", shortcut: "Ctrl+V", action: async () => {
    const t = await CO.readClipboard("the transmit list");
    if (t !== null) pasteFrames(t); else if (tabEl) CO.$("#tx-pane .scroll", tabEl).focus();
  } });

  function txMenu(keys, k) {
    const e = txEntries.find((x) => x.id === k);
    if (!e) return [{ label: "New entry...", action: () => CO.editTx() }, pasteItem(), { separator: true },
      { label: "Stop all cyclic", action: () => CO.tryApi("/api/tx/stop_all", {}) }];
    const sel = keys.map((id) => txEntries.find((x) => x.id === id)).filter(Boolean), n = sel.length;
    const live = CO.state.txLive[e.id] || e;
    return [
      { label: "Send once", shortcut: n === 1 ? "Space" : "", action: () => txAction("send", e.id) },
      { label: "Edit...", action: () => CO.editTx(e) },
      { label: "Duplicate", action: () => duplicateTx(e) },
      { label: live.enabled ? "Stop cyclic" : "Start cyclic", disabled: !(e.period_ms > 0), action: () => setCyclic(e.id, !live.enabled) },
      { label: n > 1 ? `Delete ${n} entries` : "Delete", action: () => deleteTx(sel) },
      { separator: true },
      { label: `Copy ${CO.rowsLabel(n)}`, shortcut: "Ctrl+C", action: () => txRows.copy() },
      pasteItem(),
      { label: "Select all", shortcut: "Ctrl+A", action: () => txRows.set(txEntries.map((x) => x.id)) },
    ];
  }

  /* Opens the transmit dialog filled with a frame (plugins may use it). Nothing is sent: the user adds the entry. */
  CO.frameToTx = (ch, id, flags, dlc, hex, cycleMs) => CO.editTx({ can_id: id, data: CO.spaced(hex), ext: !!(flags & F.EXT), fd: !!(flags & F.FD),
    brs: !!(flags & F.BRS), rtr: !!(flags & F.RTR), dlc, period_ms: cycleMs > 0 ? Math.round(cycleMs) : 0, channel: ch, comment: "" });
  const rowToTx = (r) => CO.frameToTx(r[1], r[2], r[3], r[4], r[5], r[8]);

  function copyToTx() {
    const v = rows.get(rxSel.anchor ?? rxSel.selected()[0]);
    if (!v) { CO.toast("Select a row in the receive list first", "warn"); return; }
    rowToTx(v.r);
  }

  function setFrozen(on) {
    frozen = on;
    const b = CO.$("#rx-freeze", tabEl);
    b.classList.toggle("on", frozen); b.textContent = frozen ? "Frozen" : "Freeze";
    renderRx();
  }

  function rxMenu(keys, k) {
    const freeze = { label: frozen ? "Unfreeze view" : "Freeze view", action: () => setFrozen(!frozen) };
    const sel = keys.map((x) => shownMap.get(x)).filter(Boolean).map((v) => v.r);
    if (!sel.length) return [freeze];
    const one = shownMap.get(k) ? shownMap.get(k).r : sel[0], n = sel.length;
    const ids = [...new Set(sel.map((r) => CO.idText(r[2], r[3])))], many = ids.length > 1;
    return [
      { label: `Copy ${CO.rowsLabel(n)}`, shortcut: "Ctrl+C", action: () => rxSel.copy() },
      { label: many ? "Copy IDs" : "Copy ID", action: () => CO.copyText(ids.join("\n"), many ? `${ids.length} IDs` : "ID") },
      { label: "Copy data (hex)", action: () => CO.copyText(sel.map((r) => CO.spaced(r[5])).join("\n"), "data") },
      { separator: true },
      { label: "Add to transmit list...", disabled: !!(one[3] & F.ERR), action: () => rowToTx(one) },
      { separator: true },
      { label: many ? "Show only these IDs in the trace" : "Show only this ID in the trace", action: () => { CO.traceFilter(ids, false); CO.show("trace"); } },
      { label: many ? "Hide these IDs in the trace" : "Hide this ID in the trace", action: () => { CO.traceFilter(ids, true); CO.show("trace"); } },
      { separator: true },
      freeze,
      { label: "Select all", shortcut: "Ctrl+A", action: () => rxSel.set(shown.map((v) => v.r[0])) },
    ];
  }

  // ---------------------------------------------------------------- tab
  CO.registerTab({
    id: "receive", title: "Receive / Transmit", order: 10,
    render(el) {
      tabEl = el;
      const ratio = CO.prefs.get("rx.split", 0.62);
      el.innerHTML = `
        <div class="toolbar">
          <select id="rx-ch" title="Channel"><option value="all">All channels</option></select>
          <input id="rx-id" class="m" placeholder="ID filter: 100-1FF, 7E5" style="width:200px">
          <label class="row small"><input type="checkbox" id="rx-tx" ${showTx ? "checked" : ""}> show Tx</label>
          <span class="sep"></span>
          <button class="btn" id="rx-freeze" title="Freeze the view (traffic keeps counting)">Freeze</button>
          <button class="btn" id="rx-clear" title="Clear the receive list and counters">Clear</button>
          <button class="btn" id="rx-copy" title="Copy the selected frame into the transmit list">To transmit</button>
          <span class="spacer"></span><span id="rx-count" class="muted small"></span>
        </div>
        <div class="split" id="rx-split">
          <div class="pane" id="rx-pane" style="flex:${ratio} 1 0">
            <div class="scroll"><table class="tbl" id="rx-table"><thead></thead><tbody></tbody></table>
              <div class="empty" id="rx-empty">No frames yet. Connect an adapter (+ Connect) - received frames show here, one row per ID.</div></div>
          </div>
          <div class="splitter" id="rx-splitter" title="Drag to resize"></div>
          <div class="pane" id="tx-pane" style="flex:${1 - ratio} 1 0">
            <div class="pane-head">Transmit <span class="muted small">double-click to edit, Space sends the selected row, right-click for more</span><span class="spacer"></span>
              <button class="btn small" id="tx-new">+ New</button><button class="btn small" id="tx-stop" title="Stop all cyclic entries">Stop all</button></div>
            <div class="scroll"><table class="tbl" id="tx-table"><thead></thead><tbody></tbody></table>
              <div class="empty" id="tx-empty">No transmit entries. + New adds one; "To transmit" copies a received frame.</div></div>
          </div>
        </div>`;
      const $ = (s) => CO.$(s, el);
      rxSel = CO.rowSelect({ el: $("#rx-pane .scroll"), keys: () => shown.map((v) => v.r[0]), menu: rxMenu, onDragEnd: renderRx });
      const txEl = $("#tx-pane .scroll");
      txRows = CO.rowSelect({ el: txEl, rows: "tr[data-tx]", key: (tr) => +tr.dataset.tx, keys: () => txEntries.map((e) => e.id), text: txText, menu: txMenu });
      // the paste event goes to the text selection or <body>, not to the focused list: route it by focus
      document.addEventListener("paste", (ev) => {
        const f = document.activeElement;
        if (el.hidden || !txEl.contains(f) || CO.isEditable(f)) return;
        ev.preventDefault();
        pasteFrames(ev.clipboardData.getData("text/plain"));
      });
      $("#rx-id").value = idFilter;
      $("#rx-ch").onchange = (e) => { chFilter = e.target.value; renderRx(); };
      $("#rx-id").oninput = (e) => { idFilter = e.target.value; renderRx(); };
      $("#rx-tx").onchange = (e) => { showTx = e.target.checked; CO.prefs.set("rx.showTx", showTx); renderRx(); };
      $("#rx-freeze").onclick = () => setFrozen(!frozen);
      // the server sends the cleared channel's table again (full refresh); other channels keep their rows
      $("#rx-clear").onclick = () => CO.tryApi("/api/channel/clear", { index: chFilter === "all" ? null : +chFilter });
      $("#rx-copy").onclick = copyToTx;
      $("#tx-new").onclick = () => CO.editTx();
      $("#tx-stop").onclick = async () => { await CO.tryApi("/api/tx/stop_all", {}); };
      $("#rx-table").addEventListener("click", (ev) => {
        const th = ev.target.closest("th[data-s]");
        if (th) { const k = th.dataset.s; sortDir = sortKey === k ? -sortDir : 1; sortKey = k; CO.prefs.set("rx.sort", k); renderRx(); return; }
      });
      $("#rx-table").addEventListener("dblclick", (ev) => { const tr = ev.target.closest("tr[data-k]"), v = tr && shownMap.get(tr.dataset.k); if (v) rowToTx(v.r); });
      $("#tx-table").addEventListener("click", (ev) => {
        const tr = ev.target.closest("tr[data-tx]"); if (!tr) return;
        const id = +tr.dataset.tx;
        const act = ev.target.closest("[data-act]");
        if (act && act.dataset.act !== "cyc") txAction(act.dataset.act, id, act);
      });
      $("#tx-table").addEventListener("change", (ev) => { const cb = ev.target.closest("[data-act=cyc]"); if (cb) txAction("cyc", +cb.closest("tr").dataset.tx, cb); });
      $("#tx-table").addEventListener("dblclick", (ev) => { const tr = ev.target.closest("tr[data-tx]"); if (tr && !ev.target.closest("button,input")) txAction("edit", +tr.dataset.tx); });
      // splitter
      const split = $("#rx-split"), sp = $("#rx-splitter");
      sp.onmousedown = (ev) => {
        ev.preventDefault();
        const rect = split.getBoundingClientRect();
        const move = (e) => { const r = Math.min(0.9, Math.max(0.15, (e.clientY - rect.top) / rect.height)); $("#rx-pane").style.flex = `${r} 1 0`; $("#tx-pane").style.flex = `${1 - r} 1 0`; CO.prefs.set("rx.split", r); };
        const up = () => { document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up); };
        document.addEventListener("mousemove", move); document.addEventListener("mouseup", up);
      };
      CO.reloadTx();
    },
    onShow() { renderRx(); renderTx(); },
  });

  document.addEventListener("keydown", (ev) => {
    if (ev.code !== "Space" || CO.current !== "receive" || !txRows || ev.repeat) return;
    if (ev.target.closest("input, textarea, select, button, dialog")) return;
    if (!tabEl || !CO.$("#tx-pane", tabEl).contains(document.activeElement)) return;   // the transmit list has the focus
    const ks = txRows.selected();
    if (ks.length !== 1) return;                          // Space sends one selected entry, never several
    ev.preventDefault();
    txAction("send", ks[0]);
  });

  let lastChans = "";
  CO.onFeed((msg) => {
    if (msg.type) return;
    applyFeed(msg);
    if (tabEl) {
      const chans = CO.state.channels.map((c) => c.index + ":" + c.name).join(",");
      if (chans !== lastChans) {
        lastChans = chans;
        const sel = CO.$("#rx-ch", tabEl);
        sel.innerHTML = `<option value="all">All channels</option>` + CO.state.channels.map((c) => `<option value="${c.index}">${CO.esc(c.name)}</option>`).join("");
        sel.value = CO.state.channels.some((c) => String(c.index) === chFilter) ? chFilter : "all";
        chFilter = sel.value;
        renderTx();
      }
    }
    renderRx();
    if (msg.tx) updateTxLive();
  });
})();
