/* Sniffer tab (sniffer plugin): changing bits per id, notch, byte guesses, candidate signals -> quick signals. */
"use strict";
(function () {
  const CO = window.canopener;
  const API = "/api/x/sniffer";
  const MAX_ROWS = 300;
  let el = null, timer = null, busy = false, frozen = false, sel = null, detail = null, lastRows = [];
  let detailAt = 0, overDetail = false;          // the detail panel refreshes slowly, and not under the pointer
  let rowSig = "", pointerDown = false;           // the grid is rebuilt only when its rows change; not mid-click
  const opts = { channel: "", ids: "", only_new: false, hide_static: false, sort: "id" };
  const TAG = { cnt: ["CNT", "counter (changes by the same step every frame)"], cnt4: ["CNT4", "4-bit counter in the low nibble"],
    chk: ["CHK", "checksum (XOR or sum of the other bytes)"], const: ["const", "never changed"] };

  const STYLE = `
    .sn-tbl td { vertical-align: middle; }
    .sn-bytes { display: flex; gap: 6px; flex-wrap: wrap; }
    .sn-byte { display: flex; flex-direction: column; align-items: center; gap: 2px; min-width: 76px; }
    .sn-hex { font-family: var(--mono); font-size: 12.5px; }
    .sn-bits { display: flex; gap: 1px; }
    .sn-bits i { width: 8px; height: 10px; border-radius: 2px; background: var(--bg3); display: block; }
    .sn-bits i.h1 { background: color-mix(in srgb, var(--warn) 12%, var(--bg3)); } .sn-bits i.h2 { background: color-mix(in srgb, var(--warn) 22%, var(--bg3)); }
    .sn-bits i.h3 { background: color-mix(in srgb, var(--warn) 32%, var(--bg3)); } .sn-bits i.h4 { background: color-mix(in srgb, var(--warn) 42%, var(--bg3)); }
    .sn-bits i.h5 { background: color-mix(in srgb, var(--warn) 52%, var(--bg3)); } .sn-bits i.h6 { background: color-mix(in srgb, var(--warn) 64%, var(--bg3)); }
    .sn-bits i.h7 { background: color-mix(in srgb, var(--warn) 76%, var(--bg3)); } .sn-bits i.h8 { background: color-mix(in srgb, var(--warn) 88%, var(--bg3)); }
    .sn-bits i.h9 { background: var(--warn); }
    .sn-bits i.n { background-image: repeating-linear-gradient(45deg, transparent 0 2px, var(--line2) 2px 3px); opacity: .45; }
    .sn-bits i.w { outline: 2px solid var(--accent); outline-offset: -1px; }
    .sn-tag { font-size: 10px; color: var(--fg3); height: 12px; line-height: 12px; }
    .sn-tag.cnt, .sn-tag.cnt4, .sn-tag.chk { color: var(--accent); }
    .sn-help { font-size: 12.5px; color: var(--fg2); }`;

  const hexId = (r) => (r.ext ? r.id.toString(16).toUpperCase().padStart(8, "0") : r.id.toString(16).toUpperCase().padStart(3, "0"));
  const big = (h) => { try { return BigInt("0x" + (h || "0")); } catch (e) { return 0n; } };

  // ---------------------------------------------------------------- rendering
  function bytesHtml(r) {
    const notch = big(r.notch), nw = big(r.new), n = r.data.length / 2;
    let out = "";
    for (let i = 0; i < n; i++) {
      let bits = "";
      for (let b = 7; b >= 0; b--) {
        const k = i * 8 + b, bit = 1n << BigInt(k);
        const cls = (notch & bit ? "n " : "") + (nw & bit ? "w " : "") + "h" + (r.heat[k] || "0");
        bits += `<i class="${cls}"></i>`;
      }
      const tag = r.tags[i] || "";
      out += `<div class="sn-byte" title="byte ${i}: bits ${i * 8 + 7}..${i * 8} (bit 7 left)"><span class="sn-hex">${r.data.substr(i * 2, 2)}</span><span class="sn-bits">${bits}</span>
        <span class="sn-tag ${tag}" title="${CO.esc(TAG[tag] ? TAG[tag][1] : "")}">${TAG[tag] ? TAG[tag][0] : ""}</span></div>`;
    }
    return `<div class="sn-bytes">${out}</div>`;
  }

  function sortRows(rows) {
    if (opts.sort === "recent") return rows.slice().sort((a, b) => (a.age_ms ?? 1e12) - (b.age_ms ?? 1e12));
    if (opts.sort === "count") return rows.slice().sort((a, b) => b.count - a.count);
    return rows;
  }

  function render(st) {
    const multi = new Set(st.ids.map((r) => r.channel)).size > 1;
    const rows = sortRows(st.ids);
    lastRows = rows;
    CO.$("#sn-status", el).textContent = `${CO.fmtNum(st.frames)} frames, ${rows.length} id(s)` + (st.dropped ? `, ${CO.fmtNum(st.dropped)} dropped` : "");
    const nb = CO.$("#sn-notch-info", el);
    nb.innerHTML = st.notched
      ? `<span class="badge accent">notched ${st.notched_s} s ago</span> <span class="sn-help">Now do one thing on the machine (press a pedal, flip a switch, move a joystick): the bits that follow it get a blue outline.</span>`
      : `<span class="sn-help">Lit bits changed recently (they fade over 10 s). <b>Notch</b> hides everything that has changed so far, so the next action stands out.</span>`;
    const only = CO.$("#sn-only", el);
    only.disabled = !st.notched;
    if (!st.notched && only.checked) { only.checked = false; opts.only_new = false; }       // no notch, nothing is "new"
    const shown = rows.slice(0, MAX_ROWS);
    const sig = (multi ? "m|" : "|") + shown.map((r) => `${r.key}/${r.data.length}`).join(",");
    if (sig !== rowSig) {                       // other rows: rebuild the table
      rowSig = sig;
      CO.$("#sn-body", el).innerHTML = shown.map((r) => `<tr data-key="${CO.esc(r.key)}" class="${sel === r.key ? "sel" : ""}" style="cursor:pointer">
        ${multi ? `<td>${CO.esc(CO.chName(r.channel))}</td>` : ""}<td class="m">${idCell(r)}</td>
        <td class="r m">${CO.fmtNum(r.count)}</td><td class="r m">${r.period_ms ? CO.fmtMs(r.period_ms) : ""}</td><td>${bytesHtml(r)}</td></tr>`).join("")
        || `<tr><td colspan="5" class="empty">${st.frames ? "No id matches the filters." : "Waiting for frames: connect a channel (the simulator works too)."}</td></tr>`;
      CO.$("#sn-head", el).innerHTML = `<tr>${multi ? "<th>Ch</th>" : ""}<th>ID</th><th class="r">Count</th><th class="r">Cycle ms</th><th>Bytes (bit 7 left, bit 0 right)</th></tr>`;
    } else {                                    // the same rows: update their cells in place
      const trs = CO.$$("#sn-body tr[data-key]", el);
      shown.forEach((r, n) => updateRow(trs[n], r, multi));
    }
    CO.$("#sn-more", el).textContent = (rows.length > MAX_ROWS ? `${rows.length - MAX_ROWS} more id(s) not shown: narrow the filter. ` : "") + (st.capped || "");
  }

  function idCell(r) { return `<b>${hexId(r)}</b>${r.new_id ? ` <span class="badge accent" title="first seen after the notch">new id</span>` : ""}`; }

  function updateRow(tr, r, multi) {
    if (!tr) return;
    tr.classList.toggle("sel", sel === r.key);
    const cells = tr.cells, o = multi ? 1 : 0;
    const id = idCell(r);
    if (cells[o].dataset.v !== id) { cells[o].innerHTML = id; cells[o].dataset.v = id; }
    cells[o + 1].textContent = CO.fmtNum(r.count);
    cells[o + 2].textContent = r.period_ms ? CO.fmtMs(r.period_ms) : "";
    const notch = big(r.notch), nw = big(r.new);
    CO.$$(".sn-byte", cells[o + 3]).forEach((box, i) => {
      box.firstElementChild.textContent = r.data.substr(i * 2, 2);
      const bits = box.children[1].children;
      for (let b = 7; b >= 0; b--) {
        const k = i * 8 + b, bit = 1n << BigInt(k);
        const cls = (notch & bit ? "n " : "") + (nw & bit ? "w " : "") + "h" + (r.heat[k] || "0");
        const node = bits[7 - b];
        if (node.className !== cls) node.className = cls;
      }
      const tag = r.tags[i] || "", t = box.children[2];
      if (t.dataset.v !== tag) { t.dataset.v = tag; t.className = "sn-tag " + tag; t.textContent = TAG[tag] ? TAG[tag][0] : ""; t.title = TAG[tag] ? TAG[tag][1] : ""; }
    });
  }

  function renderDetail() {
    const box = CO.$("#sn-detail", el);
    if (!sel || !detail) { box.hidden = true; return; }
    box.hidden = false;
    const r = detail;                           // the selected id, even when the filters hide its row
    const head = `${hexId(r)}${r.ext ? " (29-bit)" : ""}`;
    const bytes = detail.bytes.map((b) => `<tr><td class="r m">${b.byte}</td><td class="m">${b.value.toString(16).toUpperCase().padStart(2, "0")} <span class="muted">${b.value}</span></td>
      <td class="r m">${b.min}</td><td class="r m">${b.max}</td><td class="r m">${b.distinct}</td><td class="r m">${CO.fmtNum(b.changes)}</td>
      <td>${TAG[b.tag] ? `<span class="badge" title="${CO.esc(TAG[b.tag][1])}">${TAG[b.tag][0]}</span>` : ""}</td></tr>`).join("");
    const cands = detail.candidates.map((c, k) => {
      const end = c.byte_order === "little" ? c.start_bit + c.length - 1 : null;
      const where = c.byte_order === "little" ? `bits ${c.start_bit}-${end}` : `start bit ${c.start_bit} (Motorola)`;
      return `<tr><td class="m">${where}</td><td class="r m">${c.length}</td><td>${c.byte_order === "little" ? "Intel" : "Motorola"}</td>
        <td class="r m">${c.value ?? ""}</td><td class="m" title="over the last ${c.frames} frame(s) where this id's signal bits changed">${c.min ?? ""} .. ${c.max ?? ""}</td><td class="r m">${c.distinct}</td>
        <td>${c.tags.map((t) => `<span class="badge">${TAG[t] ? TAG[t][0] : CO.esc(t)}</span>`).join(" ")}</td>
        <td><button class="btn small" data-cand="${k}" data-act="quick">Quick signal&hellip;</button> <button class="btn small ghost" data-cand="${k}" data-act="dbc">DBC line</button></td></tr>`;
    }).join("");
    box.innerHTML = `<div class="row"><h3 style="margin:0">${CO.esc(head)}</h3><span class="muted small">${CO.fmtNum(detail.count)} frames, data changed in ${CO.fmtNum(detail.changes)}</span>
        <span class="spacer"></span><button class="btn small ghost" data-act="close">Close</button></div>
      <div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:8px"><div style="overflow-x:auto"><table class="tbl compact"><thead><tr><th class="r">Byte</th><th>Now</th><th class="r">Min</th><th class="r">Max</th><th class="r">Values</th><th class="r">Changes</th><th>Guess</th></tr></thead><tbody>${bytes}</tbody></table></div>
      <div style="flex:1;min-width:320px;overflow-x:auto"><table class="tbl compact"><thead><tr><th>Candidate</th><th class="r">Bits</th><th>Order</th><th class="r">Now</th><th>Seen (recent)</th><th class="r">Values</th><th></th><th></th></tr></thead>
        <tbody>${cands || `<tr><td colspan="8" class="empty">No changing bits yet${r.armed ? " since the notch" : ""}.</td></tr>`}</tbody></table>
        <p class="small muted">Candidates are the bit ranges that changed${r.armed ? " after the notch" : ""} (bytes guessed as counters or checksums left out), in Intel and Motorola bit order, and byte-aligned. "Seen" covers the last frames in which they changed. Check a candidate, then make it a quick signal to plot it on a dashboard.</p></div></div>`;
  }

  // ---------------------------------------------------------------- data
  async function refresh() {
    if (!el || el.hidden || busy || frozen || pointerDown) return;
    busy = true;
    try {
      const q = new URLSearchParams({ ids: opts.ids, only_new: opts.only_new, hide_static: opts.hide_static });
      if (opts.channel !== "") q.set("channel", opts.channel);
      const st = await CO.api(`${API}/state?${q}`);
      render(st);
      CO.$("#sn-filter", el).style.borderColor = "";
      if (sel && (!detail || (!overDetail && Date.now() - detailAt > 1500))) {
        detail = await CO.api(`${API}/detail?key=${encodeURIComponent(sel)}`).catch(() => null);
        detailAt = Date.now();
        renderDetail();
      }
    } catch (e) {
      if (e.status === 400) CO.$("#sn-filter", el).style.borderColor = "var(--bad)";
    } finally { busy = false; }
  }

  function candidate(k) { return detail && detail.candidates[+k]; }
  function sigName(c) { return `id${hexId(detail)}_${c.byte_order === "little" ? "le" : "be"}${c.start_bit}_${c.length}`; }

  async function quickSignal(c) {
    const r = detail;
    if (!r) return;
    const cur = await CO.api("/api/raw_signals").catch(() => ({ signals: [] }));
    const res = await CO.modal({ title: "Make a quick signal", body: `<p class="small muted">Quick signals are plotted on dashboards (Dashboards &gt; Quick signals lists and edits them).</p>
      <div class="grid2"><label class="field"><span>Name</span><input id="qs-n" value="${CO.esc(sigName(c))}"></label>
      <label class="field"><span>Unit</span><input id="qs-u" placeholder="%, rpm, bar"></label>
      <label class="field"><span>Start bit</span><input id="qs-s" type="number" min="0" max="511" value="${c.start_bit}"></label>
      <label class="field"><span>Length (bits)</span><input id="qs-l" type="number" min="1" max="64" value="${c.length}"></label>
      <label class="field"><span>Byte order</span><select id="qs-o"><option value="little" ${c.byte_order === "little" ? "selected" : ""}>Intel (little endian)</option><option value="big" ${c.byte_order === "big" ? "selected" : ""}>Motorola (big endian)</option></select></label>
      <label class="field"><span>Signed</span><select id="qs-g"><option value="0">no</option><option value="1">yes</option></select></label>
      <label class="field"><span>Factor</span><input id="qs-f" value="1"></label><label class="field"><span>Offset</span><input id="qs-off" value="0"></label></div>`,
      buttons: [{ label: "Cancel", value: "cancel" }, { label: "Add quick signal", value: "ok", cls: "primary" }],
      validate: (v, f) => {
        const $ = (x) => CO.$(x, f), num = (x) => x.trim() !== "" && Number.isFinite(Number(x));
        if (!/^[A-Za-z0-9_.-]{1,60}$/.test($("#qs-n").value.trim())) return "Name: letters, digits, _ . - (no spaces)";
        if (!(+$("#qs-l").value >= 1 && +$("#qs-l").value <= 64)) return "Length: 1..64 bits";
        if (!(+$("#qs-s").value >= 0 && +$("#qs-s").value <= 511)) return "Start bit: 0..511";
        if (!num($("#qs-f").value) || !num($("#qs-off").value)) return "Factor and offset must be numbers";
        return "";
      } });
    if (!res) return;
    const f = res.form, $ = (s) => CO.$(s, f);
    const sig = { name: $("#qs-n").value.trim(), channel: r.channel, can_id: hexId(r), extended: r.ext, start_bit: +$("#qs-s").value, length: +$("#qs-l").value,
      byte_order: $("#qs-o").value, signed: $("#qs-g").value === "1", factor: $("#qs-f").value, offset: $("#qs-off").value, unit: $("#qs-u").value };
    if ((cur.signals || []).some((x) => x.name === sig.name) && !(await CO.confirm("Replace the quick signal?", `<p>A quick signal named ${CO.esc(sig.name)} exists. Replace it?</p>`, "Replace", true))) return;
    const list = (cur.signals || []).filter((x) => x.name !== sig.name).concat([sig]);
    const ok = await CO.tryApi("/api/raw_signals", { signals: list });
    if (ok) CO.toast(`Quick signal ${sig.name} added: plot it from Dashboards (raw/${sig.name})`, "good");
  }

  function dbcLine(c) {
    const r = detail;
    if (!r) return;
    const name = sigName(c), dlc = r.dlc;
    const id = r.ext ? (r.id | 0x80000000) >>> 0 : r.id;
    const text = `BO_ ${id} MSG_${hexId(r)}: ${dlc} Vector__XXX\n SG_ ${name} : ${c.start_bit}|${c.length}@${c.byte_order === "little" ? 1 : 0}+ (1,0) [${c.min ?? 0}|${c.max ?? 0}] "" Vector__XXX`;
    CO.modal({ title: "DBC lines", body: `<p class="small muted">Paste into a DBC file and fix the name, factor, offset, unit and range (the range is the raw values seen recently).</p>
      <textarea id="dbc-t" rows="4" style="width:100%" class="mono" readonly>${CO.esc(text)}</textarea>`,
      buttons: [{ label: "Copy", value: "copy" }, { label: "Close", value: "close", cls: "primary" }] })
      .then((x) => { if (x && x.value === "copy") navigator.clipboard?.writeText(text).then(() => CO.toast("Copied", "good"), () => CO.toast("Copy failed: select the text instead", "bad")); });
  }

  // ---------------------------------------------------------------- right-click menus
  const rowText = (r) => `${hexId(r)}\t${CO.spaced(r.data)}\t${r.tags.map((t) => (TAG[t] ? TAG[t][0] : "-")).join(" ")}`;
  function rowMenu(ev) {
    const tr = ev.target.closest("tr[data-key]");
    const r = tr && lastRows.find((x) => x.key === tr.dataset.key);
    const notch = { label: "Notch (hide what changed so far)", action: () => CO.$("#sn-notch", el).click() };
    if (!r) return [notch];
    const id = hexId(r), flags = r.ext ? CO.F.EXT : 0;
    return [
      { label: "Details and candidates", action: () => { sel = r.key; detail = null; renderDetail(); refresh(); } },
      { separator: true },
      { label: "Copy ID", action: () => CO.copyText(id, "ID") },
      { label: "Copy data (hex)", action: () => CO.copyText(CO.spaced(r.data), "data") },
      { label: "Copy row (ID, data, guesses)", action: () => CO.copyText("ID\tData\tGuesses\n" + rowText(r), "row") },
      { label: "Copy all rows shown", action: () => CO.copyText("ID\tData\tGuesses\n" + lastRows.map(rowText).join("\n"), `${lastRows.length} rows`) },
      { separator: true },
      CO.traceFilter && { label: "Show only this ID in the trace", action: () => { CO.traceFilter([id], false); CO.show("trace"); } },
      CO.traceFilter && { label: "Hide this ID in the trace", action: () => { CO.traceFilter([id], true); CO.show("trace"); } },
      CO.frameToTx && { label: "Add to transmit list...", action: () => CO.frameToTx(r.channel, r.id, flags, r.data.length / 2, r.data, 0) },
      { separator: true },
      notch,
    ];
  }
  function candMenu(ev) {
    const b = ev.target.closest("tr")?.querySelector("[data-cand]");
    const c = b && candidate(b.dataset.cand);
    if (!c) return null;
    const range = c.byte_order === "little" ? `${c.start_bit}|${c.length}@1+` : `${c.start_bit}|${c.length}@0+`;
    return [
      { label: "Make a quick signal...", action: () => quickSignal(c) },
      { label: "DBC lines...", action: () => dbcLine(c) },
      { separator: true },
      { label: `Copy bit range (${range})`, action: () => CO.copyText(range, "bit range") },
      { label: "Copy value now", disabled: c.value === null || c.value === undefined, action: () => CO.copyText(String(c.value), "value") },
    ];
  }

  // ---------------------------------------------------------------- tab
  CO.registerTab({
    id: "sniffer", title: "Sniffer", order: 58,
    render(e) {
      el = e;
      if (!document.getElementById("sn-style")) { const st = document.createElement("style"); st.id = "sn-style"; st.textContent = STYLE; document.head.appendChild(st); }
      el.innerHTML = `<div class="page">
        <div class="row"><span id="sn-ch"></span>
          <input id="sn-filter" class="m" placeholder="ids: 0CF, 18FEF100, 700-77F" style="width:220px" title="hex ids or ranges">
          <label class="row small"><input type="checkbox" id="sn-only"> only new since the notch</label>
          <label class="row small"><input type="checkbox" id="sn-static"> hide ids that never change</label>
          <select id="sn-sort" title="Sort"><option value="id">by id</option><option value="recent">changed most recently</option><option value="count">busiest</option></select>
          <span class="spacer"></span>
          <button class="btn primary" id="sn-notch" title="Hide every bit that has changed so far">Notch</button>
          <button class="btn" id="sn-clear" title="Show every bit again">Clear notch</button>
          <button class="btn" id="sn-freeze" title="Stop updating the view (the analysis goes on)">Freeze</button>
          <button class="btn ghost" id="sn-reset" title="Forget everything seen so far">Reset</button></div>
        <div class="row" style="margin-top:6px"><span id="sn-notch-info"></span><span class="spacer"></span><span class="muted small" id="sn-status"></span></div>
        <div class="card" style="margin-top:8px;overflow-x:auto"><table class="tbl compact sn-tbl"><thead id="sn-head"></thead><tbody id="sn-body"></tbody></table>
          <div class="muted small" id="sn-more"></div></div>
        <div class="card" id="sn-detail" style="margin-top:10px" hidden></div>
        <p class="small muted">The sniffer only reads frames, it never sends anything. Own frames (Tx) are left out.</p></div>`;
      const $ = (s) => CO.$(s, el);
      el.addEventListener("mousedown", () => (pointerDown = true));
      window.addEventListener("mouseup", () => (pointerDown = false));
      $("#sn-filter").oninput = (ev) => { opts.ids = ev.target.value; refresh(); };
      $("#sn-only").onchange = (ev) => { opts.only_new = ev.target.checked; refresh(); };
      $("#sn-static").onchange = (ev) => { opts.hide_static = ev.target.checked; refresh(); };
      $("#sn-sort").onchange = (ev) => { opts.sort = ev.target.value; refresh(); };
      $("#sn-notch").onclick = async () => { const r = await CO.tryApi(`${API}/notch`, {}); if (r) { CO.toast(`${r.hidden_bits} changing bit(s) hidden: now do one thing on the machine`, "good"); refresh(); } };
      $("#sn-clear").onclick = async () => { await CO.tryApi(`${API}/notch/clear`, {}); opts.only_new = false; $("#sn-only").checked = false; refresh(); };
      $("#sn-reset").onclick = async () => { if (await CO.confirm("Reset the sniffer?", "<p>Forgets every id, bit change and guess seen so far.</p>", "Reset")) { await CO.tryApi(`${API}/reset`, {}); sel = null; detail = null; rowSig = ""; renderDetail(); refresh(); } };
      $("#sn-freeze").onclick = (ev) => { frozen = !frozen; ev.target.classList.toggle("on", frozen); ev.target.textContent = frozen ? "Frozen" : "Freeze"; if (!frozen) refresh(); };
      $("#sn-body").addEventListener("click", (ev) => { const tr = ev.target.closest("tr[data-key]"); if (!tr) return; sel = tr.dataset.key === sel ? null : tr.dataset.key; detail = null; renderDetail(); pointerDown = false; refresh(); });
      if (CO.onContextMenu) { CO.onContextMenu($("#sn-body"), rowMenu); CO.onContextMenu($("#sn-detail"), candMenu); }
      $("#sn-detail").onmouseenter = () => (overDetail = true);
      $("#sn-detail").onmouseleave = () => (overDetail = false);
      $("#sn-detail").addEventListener("click", (ev) => {
        const b = ev.target.closest("[data-act]"); if (!b) return;
        if (b.dataset.act === "close") { sel = null; detail = null; renderDetail(); return; }
        const c = candidate(b.dataset.cand); if (!c) return;
        if (b.dataset.act === "quick") quickSignal(c); else dbcLine(c);
      });
    },
    onShow() {
      const live = CO.liveChannels();
      if (opts.channel !== "" && !live.some((c) => String(c.index) === opts.channel)) opts.channel = "";   // that channel is gone
      CO.$("#sn-ch", el).innerHTML = `<select id="sn-chsel" title="Channel"><option value="">all channels</option>${live.map((c) => `<option value="${c.index}" ${String(c.index) === opts.channel ? "selected" : ""}>${CO.esc(c.name)}</option>`).join("")}</select>`;
      CO.$("#sn-chsel", el).onchange = (ev) => { opts.channel = ev.target.value; refresh(); };
      refresh(); clearInterval(timer); timer = setInterval(refresh, 300);
    },
    onHide() { clearInterval(timer); },
  });
})();
