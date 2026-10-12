/* Trace tab: every frame in time order (virtual scrolling), pause, filter, save. */
"use strict";
(function () {
  const CO = window.canopener, F = CO.F;
  const ROW = 22;
  let buf = [];                     // [ch, ts, id, flags, dlc, hex] or {gap: n}
  let total = 0, skipped = 0, paused = false, follow = true, tabEl = null, timeMode = CO.prefs.get("tr.time", "rel");
  let maxRows = CO.prefs.get("tr.max", 200000), t0 = null, dirty = false;
  let filter = CO.prefs.get("tr.filter", { ids: "", exclude: false, dir: "all", data: "" });
  CO.traceBuffer = () => buf;

  function sendFilter() {
    CO.wsSend({ type: "trace", paused, filter: { ...filter, channels: filter.ch !== undefined && filter.ch !== "all" ? [+filter.ch] : null } });
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
      if (!Array.isArray(r)) { html += `<div class="trace-row muted"><span></span><span>&hellip;</span><span></span><span></span><span>${r.gap ? `paused` : ""}</span></div>`; continue; }
      const fl = r[3], cls = fl & F.ERR ? "err" : fl & F.TX ? "tx" : "";
      const data = fl & F.RTR ? "remote request" : fl & F.ERR ? "error frame " + CO.spaced(r[5]) : CO.spaced(r[5]);
      html += `<div class="trace-row ${cls}"><span>${i + 1}</span><span>${timeText(r, i)}</span><span>${multi ? CO.esc(CO.chName(r[0])) : r[0] + 1}</span>` +
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
          buf = []; t0 = null; skipped = 0; sendFilter(); schedule();
        }, 300);
      };
      ["#tr-ids", "#tr-data"].forEach((s) => ($(s).oninput = onFilter));
      ["#tr-ex", "#tr-dir", "#tr-ch"].forEach((s) => ($(s).onchange = onFilter));
      $("#tr-time").onchange = (e) => { timeMode = e.target.value; CO.prefs.set("tr.time", timeMode); schedule(); };
      $("#tr-pause").onclick = (e) => {
        paused = !paused;
        e.target.classList.toggle("on", paused); e.target.textContent = paused ? "Paused - resume" : "Pause";
        if (paused) buf.push({ gap: 1 });
        sendFilter(); schedule();
      };
      $("#tr-clear").onclick = () => { buf = []; t0 = null; skipped = 0; schedule(); };
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
      wrap.addEventListener("scroll", () => {
        const atBottom = wrap.scrollTop + wrap.clientHeight >= wrap.scrollHeight - ROW * 2;
        follow = atBottom;
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
    if (paused) return;
    skipped += msg.skipped || 0;
    append(msg.trace || []);
  });

  /* Load frames of a file view channel into the trace (called by the Record tab). */
  CO.loadTraceFrames = (frames) => { buf = frames.slice(-maxRows); t0 = buf.length ? buf[0][1] : null; follow = false; schedule();
    if (tabEl) setTimeout(() => { CO.$("#tr-wrap", tabEl).scrollTop = 0; }, 0); };
})();
