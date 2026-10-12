/* Trace tab: every frame in time order (virtual scrolling), pause, filter, save. */
"use strict";
(function () {
  const CO = window.canopener, F = CO.F;
  const ROW = 22;
  let buf = [];                     // [ch, ts, id, flags, dlc, hex] or {gap: n}
  let total = 0, skipped = 0, paused = false, follow = true, tabEl = null, timeMode = CO.prefs.get("tr.time", "rel");
  let maxRows = CO.prefs.get("tr.max", 200000), t0 = null, dirty = false;
  let base = 0, sel = null;         // base: rows dropped from the front since the last reset; row key = base + index
  let filter = CO.prefs.get("tr.filter", { ids: "", exclude: false, dir: "all", data: "" });
  CO.traceBuffer = () => buf;

  let filterGen = 0;                // frames made by an older filter (still on their way) are dropped
  function sendFilter() {
    filterGen += 1;
    CO.wsSend({ type: "trace", paused, gen: filterGen, filter: { ...filter, channels: filter.ch !== undefined && filter.ch !== "all" ? [+filter.ch] : null } });
  }

  function timeText(row, i) {
    const ts = row[1];
    if (timeMode === "abs") return CO.fmtTime(ts);
    if (timeMode === "delta") {
      let j = i - 1; while (j >= 0 && !Array.isArray(buf[j])) j--;
      return j >= 0 ? ((ts - buf[j][1]) * 1000).toFixed(3) : "0.000";
    }
    return (ts - (t0 ?? ts)).toFixed(6);
  }

  const dataText = (r) => (r[3] & F.RTR ? "remote request" : r[3] & F.ERR ? "error frame " + CO.spaced(r[5]) : CO.spaced(r[5]));
  const allKeys = () => Array.from({ length: buf.length }, (_, i) => base + i);
  const framesOf = (keys) => keys.map((p) => [buf[p - base], p - base]).filter(([r]) => Array.isArray(r));

  function reset() { buf = []; t0 = null; skipped = 0; base = 0; if (sel) sel.clear(); }

  function traceText(keys) {                // the columns the trace shows
    const multi = CO.state.channels.length > 1;
    const head = ["#", { rel: "Time (s)", delta: "Delta (ms)", abs: "Clock" }[timeMode], "Ch", "Dir", "ID", "Type", "DLC", "Data"].join("\t");
    return [head].concat(framesOf(keys).map(([r, i]) => [i + 1, timeText(r, i), multi ? CO.chName(r[0]) : r[0] + 1, r[3] & F.TX ? "Tx" : "Rx",
      CO.idText(r[2], r[3]), CO.typeText(r[3]), r[4], dataText(r)].join("\t"))).join("\n");
  }

  function trcText(keys) {                  // rows like a PCAN-View .trc v1.1 file (canopener/core/trc.py), error frames left out
    return framesOf(keys).filter(([r]) => !(r[3] & F.ERR)).map(([r, i]) => {
      const id = r[2].toString(16).toUpperCase().padStart(r[3] & F.EXT ? 8 : 4, "0");
      return `${String(i + 1).padStart(6)})${((r[1] - (t0 ?? r[1])) * 1000).toFixed(1).padStart(12)}  ${r[3] & F.TX ? "Tx" : "Rx"}${id.padStart(13)}  ${r[4]}  ${r[3] & F.RTR ? "RTR" : CO.spaced(r[5])}`;
    }).join("\n");
  }

  function togglePause() {
    paused = !paused;
    const b = CO.$("#tr-pause", tabEl);
    b.classList.toggle("on", paused); b.textContent = paused ? "Paused - resume" : "Pause";
    if (paused) buf.push({ gap: 1 });
    sendFilter(); schedule();
  }

  /* Show only (exclude false) or hide (exclude true) these hex IDs; [] clears the ID filter. Hiding adds to the IDs
     already hidden. Like any filter change it empties the view. */
  CO.traceFilter = (ids, exclude) => {
    const hidden = filter.exclude && (filter.ids || "").trim() ? filter.ids.split(",").map((x) => x.trim()).filter(Boolean) : [];
    const list = exclude ? [...new Set(hidden.concat(ids))] : ids;
    filter = { ...filter, ids: list.join(", "), exclude: !!exclude && list.length > 0 };
    if (tabEl) { CO.$("#tr-ids", tabEl).value = filter.ids; CO.$("#tr-ex", tabEl).checked = filter.exclude; }
    CO.prefs.set("tr.filter", filter);
    reset(); follow = true; sendFilter(); schedule();
  };

  function traceMenu(keys, k) {
    const pause = { label: paused ? "Resume the live trace" : "Pause", action: togglePause };
    const rs = framesOf(keys).map(([r]) => r);
    if (!rs.length) return [pause];
    const one = Array.isArray(buf[k - base]) ? buf[k - base] : rs[0], n = rs.length;
    const ids = [...new Set(rs.map((r) => CO.idText(r[2], r[3])))], many = ids.length > 1;
    return [
      { label: `Copy ${CO.rowsLabel(n)}`, shortcut: "Ctrl+C", action: () => sel.copy() },
      { label: "Copy as .trc lines", action: () => CO.copyText(trcText(keys), n > 1 ? `${n} lines` : "1 line") },
      { label: many ? "Copy IDs" : "Copy ID", action: () => CO.copyText(ids.join("\n"), many ? `${ids.length} IDs` : "ID") },
      { label: "Copy data (hex)", action: () => CO.copyText(rs.map((r) => CO.spaced(r[5])).join("\n"), "data") },
      { separator: true },
      { label: many ? "Show only these IDs" : "Show only this ID", action: () => CO.traceFilter(ids, false) },
      { label: many ? "Hide these IDs" : "Hide this ID", action: () => CO.traceFilter(ids, true) },
      (filter.ids || "").trim() && { label: "Clear the ID filter", action: () => CO.traceFilter([], false) },
      { separator: true },
      { label: "Add to transmit list...", disabled: !!(one[3] & F.ERR), action: () => CO.frameToTx(one[0], one[2], one[3], one[4], one[5], 0) },
      { separator: true },
      pause,
      { label: "Select all", shortcut: "Ctrl+A", action: () => sel.set(allKeys()) },
    ];
  }

  function render() {
    dirty = false;
    if (!tabEl || tabEl.hidden) return;
    const wrap = CO.$("#tr-wrap", tabEl), rowsEl = CO.$("#tr-rows", tabEl), spacer = CO.$("#tr-spacer", tabEl);
    spacer.style.height = (buf.length * ROW + ROW) + "px";
    if (follow) wrap.scrollTop = wrap.scrollHeight;
    const first = Math.max(0, Math.floor(wrap.scrollTop / ROW) - 5);
    const count = Math.ceil(wrap.clientHeight / ROW) + 10;
    const multi = CO.state.channels.length > 1;
    let html = "";
    for (let i = first; i < Math.min(buf.length, first + count); i++) {
      const r = buf[i];
      const p = base + i, sc = sel.has(p) ? " sel" : "";
      if (!Array.isArray(r)) { html += `<div class="trace-row muted${sc}" data-p="${p}"><span></span><span>&hellip;</span><span></span><span></span><span>${r.gap ? `paused` : ""}</span></div>`; continue; }
      const fl = r[3], cls = fl & F.ERR ? "err" : fl & F.TX ? "tx" : "";
      const data = dataText(r);
      html += `<div class="trace-row ${cls}${sc}" data-p="${p}"><span>${i + 1}</span><span>${timeText(r, i)}</span><span>${multi ? CO.esc(CO.chName(r[0])) : r[0] + 1}</span>` +
        `<span>${fl & F.TX ? "Tx" : "Rx"}</span><span>${CO.idText(r[2], fl)}</span><span>${CO.typeText(fl)}</span><span>${r[4]}</span><span>${data}</span></div>`;
    }
    rowsEl.style.top = (ROW + first * ROW) + "px";
    rowsEl.innerHTML = html;
    CO.$("#tr-info", tabEl).textContent = `${buf.length.toLocaleString()} shown` + (skipped ? `, ${skipped.toLocaleString()} not shown live (too fast; all are in the save/recording)` : "");
    CO.$("#tr-latest", tabEl).hidden = follow;
  }
  const schedule = () => { if (!dirty) { dirty = true; requestAnimationFrame(render); } };

  function append(frames) {
    if (!frames.length) return;
    if (t0 === null) t0 = frames[0][1];
    for (const f of frames) buf.push(f);
    total += frames.length;
    if (buf.length > maxRows) {
      const drop = buf.length - maxRows;
      buf.splice(0, drop);
      base += drop;                 // selected rows that fell out of the buffer drop out of the selection by themselves
      if (!follow && tabEl) { const w = CO.$("#tr-wrap", tabEl); w.scrollTop = Math.max(0, w.scrollTop - drop * ROW); }
    }
    schedule();
  }

  CO.registerTab({
    id: "trace", title: "Trace", order: 20,
    render(el) {
      tabEl = el;
      el.innerHTML = `
        <div class="toolbar">
          <button class="btn" id="tr-pause" title="Pause the live trace (recording and counters keep running)">Pause</button>
          <button class="btn" id="tr-clear">Clear</button>
          <span class="sep"></span>
          <select id="tr-ch" title="Channel"><option value="all">All channels</option></select>
          <input id="tr-ids" class="m" placeholder="IDs: 100-1FF, 7E5" style="width:180px" title="Hex IDs and ranges, comma separated">
          <label class="row small" title="Hide these IDs instead of showing only them"><input type="checkbox" id="tr-ex"> exclude</label>
          <select id="tr-dir"><option value="all">Rx + Tx</option><option value="rx">Rx only</option><option value="tx">Tx only</option></select>
          <input id="tr-data" class="m" placeholder="data contains (hex)" style="width:150px">
          <span class="sep"></span>
          <select id="tr-time" title="Time column"><option value="rel">Time (s)</option><option value="delta">Delta (ms)</option><option value="abs">Clock</option></select>
          <button class="btn" id="tr-save" title="Save everything the trace buffer holds (all channels, unfiltered) to a file">Save&hellip;</button>
          <span class="spacer"></span><span id="tr-info" class="muted small"></span>
        </div>
        <div class="trace-wrap" id="tr-wrap">
          <div class="trace-head"><span>#</span><span>Time</span><span>Ch</span><span>Dir</span><span>ID</span><span>Type</span><span>DLC</span><span>Data</span></div>
          <div id="tr-spacer"></div><div class="trace-rows" id="tr-rows"></div>
        </div>
        <button class="btn primary" id="tr-latest" style="position:absolute;right:28px;bottom:40px" hidden>Jump to latest &darr;</button>`;
      el.style.position = "relative";
      const $ = (s) => CO.$(s, el);
      $("#tr-ids").value = filter.ids || ""; $("#tr-ex").checked = !!filter.exclude; $("#tr-dir").value = filter.dir || "all";
      $("#tr-data").value = filter.data || ""; $("#tr-time").value = timeMode;
      let ft = null;
      const onFilter = () => {
        filter = { ids: $("#tr-ids").value, exclude: $("#tr-ex").checked, dir: $("#tr-dir").value, data: $("#tr-data").value, ch: $("#tr-ch").value };
        clearTimeout(ft);
        ft = setTimeout(() => {
          CO.prefs.set("tr.filter", filter);
          reset(); sendFilter(); schedule();
        }, 300);
      };
      ["#tr-ids", "#tr-data"].forEach((s) => ($(s).oninput = onFilter));
      ["#tr-ex", "#tr-dir", "#tr-ch"].forEach((s) => ($(s).onchange = onFilter));
      $("#tr-time").onchange = (e) => { timeMode = e.target.value; CO.prefs.set("tr.time", timeMode); schedule(); };
      $("#tr-pause").onclick = togglePause;
      $("#tr-clear").onclick = () => { reset(); schedule(); };
      $("#tr-save").onclick = async () => {
        const res = await CO.modal({ title: "Save trace buffer", body: `
          <p class="small muted">Saves every frame the trace buffer holds on the server (all channels, without the filter) into the recordings folder. For long captures use Record instead.</p>
          <div class="grid2"><label class="field"><span>File name</span><input id="s-name" placeholder="automatic"></label>
          <label class="field"><span>Format</span><select id="s-fmt"><option>trc</option><option>asc</option><option>blf</option><option>log</option><option>csv</option></select></label></div>`,
          buttons: [{ label: "Cancel", value: "cancel" }, { label: "Save", value: "ok", cls: "primary" }] });
        if (!res) return;
        const r = await CO.tryApi("/api/trace/save", { name: CO.$("#s-name", res.form).value.trim(), format: CO.$("#s-fmt", res.form).value });
        if (r) CO.toast(`Saved ${r.frames.toLocaleString()} frames to ${r.name}`, "good");
      };
      const wrap = $("#tr-wrap");
      sel = CO.rowSelect({ el: wrap, rows: ".trace-row[data-p]", key: (d) => +d.dataset.p, keys: allKeys, text: traceText, menu: traceMenu });
      wrap.addEventListener("mousedown", (ev) => { if (ev.target.closest(".trace-row")) follow = false; });   // rows stop moving while you pick them
      wrap.addEventListener("scroll", () => {
        const atBottom = wrap.scrollTop + wrap.clientHeight >= wrap.scrollHeight - ROW * 2;
        follow = atBottom && !sel.busy;
        schedule();
      });
      $("#tr-latest").onclick = () => { follow = true; schedule(); };
    },
    onShow() { follow = true; schedule(); },
  });

  let lastChans = "";
  CO.onFeed((msg) => {
    if (msg.type === "open") { sendFilter(); return; }
    if (msg.type) return;
    if (tabEl) {
      const chans = CO.state.channels.map((c) => c.index + ":" + c.name).join(",");
      if (chans !== lastChans) {
        lastChans = chans;
        const sel = CO.$("#tr-ch", tabEl), cur = sel.value;
        sel.innerHTML = `<option value="all">All channels</option>` + CO.state.channels.map((c) => `<option value="${c.index}">${CO.esc(c.name)}</option>`).join("");
        sel.value = CO.state.channels.some((c) => String(c.index) === cur) ? cur : "all";
      }
    }
    if (paused || msg.trace_gen !== filterGen) return;
    skipped += msg.skipped || 0;
    append(msg.trace || []);
  });

  /* Load frames of a file view channel into the trace (called by the Record tab). */
  CO.loadTraceFrames = (frames) => { reset(); buf = frames.slice(-maxRows); t0 = buf.length ? buf[0][1] : null; follow = false; schedule();
    if (tabEl) setTimeout(() => { CO.$("#tr-wrap", tabEl).scrollTop = 0; }, 0); };
})();
