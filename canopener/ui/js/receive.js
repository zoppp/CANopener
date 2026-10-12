/* Receive / Transmit tab: the per-ID receive list (top) and the transmit list (bottom), like PCAN-View. */
"use strict";
(function () {
  const CO = window.canopener, F = CO.F;
  const rows = new Map();          // key -> {r: row array, chg: [byte change times], seen}
  let sortKey = CO.prefs.get("rx.sort", "id"), sortDir = 1, selected = null, frozen = false;
  let chFilter = "all", idFilter = "", showTx = CO.prefs.get("rx.showTx", false);
  let tabEl = null, txEntries = [], txSel = null;
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
    if (!tabEl || tabEl.hidden || frozen) return;
    const list = sorted();
    const multi = CO.state.channels.length > 1;
    const dec = CO.decodedText && Object.keys(CO.decodedText).length > 0;     // filled by decode.js
    const body = list.map((v) => {
      const r = v.r;
      const cls = (r[0] === selected ? "sel " : "") + (r[3] & F.ERR ? "err" : "");
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
      const data = e.rtr ? `<span class="muted">remote, DLC ${e.dlc}</span>` : CO.esc(CO.spaced((e.data || "").replace(/[^0-9a-f]/gi, "").toUpperCase()));
      const nbytes = (e.data || "").replace(/[^0-9a-f]/gi, "").length / 2;
      return `<tr data-tx="${e.id}" class="${txSel === e.id ? "sel" : ""}">
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

  function copyToTx() {
    const v = rows.get(selected);
    if (!v) { CO.toast("Select a row in the receive list first", "warn"); return; }
    const r = v.r;
    CO.editTx({ can_id: r[2], data: CO.spaced(r[5]), ext: !!(r[3] & F.EXT), fd: !!(r[3] & F.FD), brs: !!(r[3] & F.BRS),
      rtr: !!(r[3] & F.RTR), dlc: r[4], period_ms: r[8] > 0 ? Math.round(r[8]) : 0, channel: r[1], comment: "" });
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
            <div class="pane-head">Transmit <span class="muted small">double-click to edit, Space sends the selected row</span><span class="spacer"></span>
              <button class="btn small" id="tx-new">+ New</button><button class="btn small" id="tx-stop" title="Stop all cyclic entries">Stop all</button></div>
            <div class="scroll"><table class="tbl" id="tx-table"><thead></thead><tbody></tbody></table>
              <div class="empty" id="tx-empty">No transmit entries. + New adds one; "To transmit" copies a received frame.</div></div>
          </div>
        </div>`;
      const $ = (s) => CO.$(s, el);
      $("#rx-id").value = idFilter;
      $("#rx-ch").onchange = (e) => { chFilter = e.target.value; renderRx(); };
      $("#rx-id").oninput = (e) => { idFilter = e.target.value; renderRx(); };
      $("#rx-tx").onchange = (e) => { showTx = e.target.checked; CO.prefs.set("rx.showTx", showTx); renderRx(); };
      $("#rx-freeze").onclick = (e) => { frozen = !frozen; e.target.classList.toggle("on", frozen); e.target.textContent = frozen ? "Frozen" : "Freeze"; renderRx(); };
      // the server sends the cleared channel's table again (full refresh); other channels keep their rows
      $("#rx-clear").onclick = () => CO.tryApi("/api/channel/clear", { index: chFilter === "all" ? null : +chFilter });
      $("#rx-copy").onclick = copyToTx;
      $("#tx-new").onclick = () => CO.editTx();
      $("#tx-stop").onclick = async () => { await CO.tryApi("/api/tx/stop_all", {}); };
      $("#rx-table").addEventListener("click", (ev) => {
        const th = ev.target.closest("th[data-s]");
        if (th) { const k = th.dataset.s; sortDir = sortKey === k ? -sortDir : 1; sortKey = k; CO.prefs.set("rx.sort", k); renderRx(); return; }
        const tr = ev.target.closest("tr[data-k]"); if (tr) { selected = tr.dataset.k; renderRx(); }
      });
      $("#rx-table").addEventListener("dblclick", (ev) => { const tr = ev.target.closest("tr[data-k]"); if (tr) { selected = tr.dataset.k; copyToTx(); } });
      $("#tx-table").addEventListener("click", (ev) => {
        const tr = ev.target.closest("tr[data-tx]"); if (!tr) return;
        const id = +tr.dataset.tx;
        const act = ev.target.closest("[data-act]");
        if (act && act.dataset.act !== "cyc") { txAction(act.dataset.act, id, act); return; }
        txSel = id; CO.$$("#tx-table tr", el).forEach((r) => r.classList.toggle("sel", +r.dataset.tx === id));
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
    if (ev.code !== "Space" || CO.current !== "receive" || txSel === null) return;
    if (ev.target.closest("input, textarea, select, button, dialog")) return;
    ev.preventDefault();
    txAction("send", txSel);
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
