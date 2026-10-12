/* Record & Replay tab. */
"use strict";
(function () {
  const CO = window.canopener;
  let tabEl = null, files = [], folder = "", sel = null;

  async function loadFiles() {
    const d = await CO.tryApi("/api/recordings");
    if (!d) return;
    files = d.files; folder = d.folder;
    renderFiles();
  }

  function renderFiles() {
    if (!tabEl) return;
    CO.$("#rp-folder", tabEl).textContent = folder;
    CO.$("#rp-files", tabEl).innerHTML = files.length ? files.map((f) => `<tr data-name="${CO.esc(f.name)}" class="${sel.has(f.name) ? "sel" : ""}">
      <td class="m">${CO.esc(f.name)}</td><td class="r">${CO.fmtBytes(f.bytes)}</td><td>${new Date(f.modified * 1000).toLocaleString()}</td>
      <td class="r"><button class="btn small" data-act="open" title="Load into the receive list and trace (offline)">Open</button>
        <button class="btn small" data-act="replay" title="Send onto a bus">Replay&hellip;</button>
        <a class="btn small ghost" href="/api/recordings/${encodeURIComponent(f.name)}" download>Download</a></td></tr>`).join("")
      : `<tr><td colspan="4" class="empty">No recordings yet.</td></tr>`;
  }

  function renderStatus() {
    if (!tabEl) return;
    const r = CO.state.recording, el = CO.$("#rec-status", tabEl);
    const live = CO.liveChannels();
    CO.$("#rec-start", tabEl).disabled = !!r || !live.length;
    CO.$("#rec-stop", tabEl).disabled = !r;
    if (CO.selecting(el)) { /* keep the text being selected */ } else if (r) {
      el.innerHTML = `<div class="notice ${r.error ? "bad" : "good"}"><b>Recording</b> to <span class="mono">${CO.esc(r.path)}</span><br>
        ${r.written.toLocaleString()} frames written, ${r.duration} s, ${CO.fmtBytes(r.bytes)}${r.pending > 1000 ? `, ${r.pending.toLocaleString()} queued` : ""}
        ${r.error ? `<br>Error: ${CO.esc(r.error)}` : ""}</div>`;
    } else el.innerHTML = live.length ? `<span class="muted">Not recording.</span>` : `<span class="muted">Connect an adapter to record.</span>`;
    const p = CO.state.replay, ps = CO.$("#rp-status", tabEl);
    if (CO.selecting(ps)) return;                   // also keeps Pause / Stop under the mouse while it is pressed
    if (p) {
      const running = p.state === "playing" || p.state === "paused";
      ps.innerHTML = `<div class="notice ${p.state === "error" ? "bad" : running ? "good" : ""}"><b>Replay ${CO.esc(p.state)}</b> ${CO.esc(p.path.split(/[\\/]/).pop())}:
        ${p.sent.toLocaleString()} of ${p.total.toLocaleString()} sent, ${p.position_s.toFixed(1)} s${p.loops ? `, loop ${p.loops}` : ""}
        ${p.errors ? `<br>${p.errors} send errors: ${CO.esc(p.last_error)}` : ""}</div>
        ${running ? `<div class="row"><button class="btn" data-rp="pause">${p.state === "paused" ? "Resume" : "Pause"}</button><button class="btn danger" data-rp="stop">Stop</button></div>` : ""}`;
    } else ps.innerHTML = "";
  }

  async function openFile(name) {
    const r = await CO.tryApi("/api/file/open", { name });
    if (!r) return;
    const all = [];
    for (const ch of r.channels) {
      const t = await CO.api(`/api/trace?channel=${ch}&count=200000`);
      all.push(...t.frames);
    }
    all.sort((a, b) => a[1] - b[1]);
    CO.loadTraceFrames(all);
    CO.toast(`Opened ${name}: ${all.length.toLocaleString()} frames. It shows as a file channel (close it with its x).`, "good");
    CO.show("receive");
  }

  const fullPath = (name) => folder + (folder.includes("\\") ? "\\" : "/") + name;
  function download(name) {
    const a = document.createElement("a");
    a.href = `/api/recordings/${encodeURIComponent(name)}`; a.download = name; a.click();
  }

  function filesMenu(keys, k) {
    if (k === null) return [{ label: "Refresh", action: loadFiles }];
    const n = keys.length;
    return [
      { label: "Open", action: () => openFile(k) },
      { label: "Replay...", action: () => replayDialog(k) },
      { label: "Download", action: () => download(k) },
      { separator: true },
      { label: n > 1 ? "Copy file names" : "Copy file name", action: () => CO.copyText(keys.join("\n"), n > 1 ? `${n} names` : "file name") },
      { label: n > 1 ? "Copy full paths" : "Copy full path", action: () => CO.copyText(keys.map(fullPath).join("\n"), n > 1 ? `${n} paths` : "path") },
      { label: `Copy ${CO.rowsLabel(n)}`, shortcut: "Ctrl+C", action: () => sel.copy() },
    ];
  }

  async function replayDialog(name) {
    const live = CO.liveChannels();
    if (!live.length) { CO.toast("Connect an adapter to replay onto", "warn"); return; }
    const res = await CO.modal({ title: `Replay ${name}`, body: `
      <div class="notice warn">Replay transmits every recorded frame onto the bus. Make sure that is safe for the machine.</div>
      <div class="grid3">
        <label class="field"><span>Bus 1 of the file onto</span>${CO.channelSelect(live[0].index)}</label>
        <label class="field"><span>Speed</span><select id="r-speed"><option value="0.5">0.5x</option><option value="1" selected>1x (recorded timing)</option><option value="2">2x</option><option value="10">10x</option></select></label>
        <label class="field"><span>Frames</span><select id="r-only"><option value="all">all</option><option value="rx">recorded Rx only</option><option value="tx">recorded Tx only</option></select></label></div>
      <label class="row"><input type="checkbox" id="r-loop"> Loop</label>`,
      buttons: [{ label: "Cancel", value: "cancel" }, { label: "Start replay", value: "ok", cls: "danger solid" }] });
    if (!res) return;
    const f = res.form;
    await CO.tryApi("/api/replay/start", { name, bus_map: { 1: +CO.$(".ch-select", f).value }, speed: +CO.$("#r-speed", f).value,
      loop: CO.$("#r-loop", f).checked, only: CO.$("#r-only", f).value }, "Replay started");
  }

  CO.registerTab({
    id: "record", title: "Record & Replay", order: 30,
    render(el) {
      tabEl = el;
      el.innerHTML = `<div class="page"><div class="cards">
        <div class="card"><h3>Record</h3>
          <div class="grid2"><label class="field"><span>File name</span><input id="rec-name" placeholder="automatic (date-time)"></label>
          <label class="field"><span>Format</span><select id="rec-fmt">
            <option value="trc">.trc PCAN-View</option><option value="blf">.blf Vector binary</option><option value="asc">.asc Vector ASCII</option>
            <option value="log">.log candump</option><option value="csv">.csv</option><option value="canbin">.canbin PLC format</option></select></label></div>
          <p class="small muted">Records every connected channel, Rx and Tx, without dropping frames (written by its own thread).</p>
          <div class="row"><button class="btn danger solid" id="rec-start">&#9679; Start recording</button><button class="btn" id="rec-stop">Stop</button></div>
          <div id="rec-status" style="margin-top:8px"></div></div>
        <div class="card"><h3>Replay</h3><p class="small muted">Pick a file below and click Replay. Frames are sent with their recorded timing.</p>
          <div id="rp-status"></div></div>
      </div>
      <div class="card" style="margin-top:12px"><div class="row"><h3 style="margin:0">Recordings</h3><span class="muted small mono" id="rp-folder"></span><span class="spacer"></span>
        <label class="btn small" title="Copy a .trc/.asc/.blf/.log/.csv/.canbin file into the recordings folder">Import file&hellip;<input type="file" id="rp-upload" hidden accept=".trc,.asc,.blf,.log,.csv,.canbin"></label>
        <button class="btn small" id="rp-refresh">Refresh</button></div>
        <table class="tbl" style="margin-top:8px"><thead><tr><th>File</th><th class="r">Size</th><th>Modified</th><th></th></tr></thead><tbody id="rp-files"></tbody></table></div></div>`;
      const $ = (s) => CO.$(s, el);
      sel = CO.rowSelect({ el: $("#rp-files").closest("table"), rows: "tr[data-name]", key: (tr) => tr.dataset.name, keys: () => files.map((f) => f.name), menu: filesMenu });
      $("#rec-start").onclick = async () => {
        const r = await CO.tryApi("/api/record/start", { name: $("#rec-name").value.trim(), format: $("#rec-fmt").value });
        if (r) { CO.state.recording = r; renderStatus(); }
      };
      $("#rec-stop").onclick = async () => { const r = await CO.tryApi("/api/record/stop", {}); if (r) { CO.toast(`Saved ${r.written.toLocaleString()} frames to ${r.path}`, "good"); CO.state.recording = null; renderStatus(); loadFiles(); } };
      $("#rp-refresh").onclick = loadFiles;
      $("#rp-upload").onchange = async (e) => {
        const file = e.target.files[0]; if (!file) return;
        const fd = new FormData(); fd.append("file", file);
        if (await CO.tryApi("/api/recordings/upload", fd, `Imported ${file.name}`)) loadFiles();
        e.target.value = "";
      };
      $("#rp-files").addEventListener("click", (ev) => {
        const b = ev.target.closest("[data-act]"); if (!b) return;
        const name = b.closest("tr").dataset.name;
        if (b.dataset.act === "open") openFile(name); else if (b.dataset.act === "replay") replayDialog(name);
      });
      $("#rp-status").addEventListener("click", async (ev) => {
        const b = ev.target.closest("[data-rp]"); if (!b) return;
        if (b.dataset.rp === "stop") await CO.tryApi("/api/replay/stop", {});
        else await CO.tryApi("/api/replay/pause", { paused: CO.state.replay.state !== "paused" });
      });
      loadFiles();
    },
    onShow() { loadFiles(); renderStatus(); },
  });

  let wasRecording = false;
  CO.onFeed((msg) => {
    if (msg.type) return;
    if (tabEl && !tabEl.hidden) renderStatus();
    const rec = !!CO.state.recording;
    if (wasRecording && !rec && tabEl) loadFiles();
    wasRecording = rec;
  });
})();
