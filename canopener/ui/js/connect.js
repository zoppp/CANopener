/* Header + status bar: connect dialog, channel chips, bus load, event log, projects, theme, about. */
"use strict";
(function () {
  const CO = window.canopener;

  function fmtRate(b) { return b >= 1e6 ? (b / 1e6) + "M" : b >= 1000 ? (b / 1000).toFixed(b % 1000 ? 1 : 0) + "k" : String(b); }

  // ---------------------------------------------------------------- connect dialog
  CO.openConnect = async (index) => {
    const last = (CO.state.settings.connections || []).find((c) => index === undefined ? true : c.index === index) || {};
    const used = new Set(CO.state.channels.map((c) => c.index));
    let slot = index; if (slot === undefined) { slot = 0; while (used.has(slot)) slot++; }
    const backends = CO.state.backends;
    const body = `
      <div class="row"><label class="field" style="flex:1"><span>Detected adapters</span>
        <select id="c-dev"><option value="">Searching&hellip;</option></select></label>
        <button type="button" class="btn" id="c-refresh" style="align-self:flex-end">Refresh</button></div>
      <div class="grid3">
        <label class="field"><span>Interface</span><select id="c-if">${backends.map((b) => `<option value="${b.name}">${CO.esc(b.title)}</option>`).join("")}</select></label>
        <label class="field"><span>Channel</span><input id="c-ch" placeholder="default"></label>
        <label class="field"><span>Bit rate</span><select id="c-br">${CO.state.bitrates.map((b) => `<option value="${b}">${fmtRate(b)}bit/s</option>`).join("")}</select></label>
      </div>
      <div class="grid3">
        <label class="row"><input type="checkbox" id="c-fd"> CAN FD</label>
        <label class="field"><span>FD data bit rate</span><select id="c-dbr">${CO.state.dataBitrates.map((b) => `<option value="${b}">${fmtRate(b)}bit/s</option>`).join("")}</select></label>
        <label class="row" title="The adapter never ACKs or transmits: safe on a live machine"><input type="checkbox" id="c-lo"> Listen-only</label>
      </div>
      <label class="row" id="c-sim-row" title="Simulated CANopen nodes (they answer SDO/LSS and really change their id), PDOs and J1939 traffic"><input type="checkbox" id="c-sim" checked> Start simulated devices on this virtual bus (to try things without hardware)</label>
      <div id="c-note" class="small muted"></div>
      <div class="small muted">Opens as <b>CAN${slot + 1}</b>. Your choices are remembered.</div>`;
    const res = await CO.modal({
      title: index === undefined ? "Connect an adapter" : `Reconnect CAN${index + 1}`, body,
      buttons: [{ label: "Cancel", value: "cancel" }, { label: "Connect", value: "ok", cls: "primary" }],
      onOpen: (f) => {
        const $ = (s) => CO.$(s, f);
        const ifSel = $("#c-if"), note = $("#c-note");
        const setVals = (c) => {
          if (c.interface) ifSel.value = c.interface;
          $("#c-ch").value = c.channel ?? "";
          if (c.bitrate) $("#c-br").value = c.bitrate;
          $("#c-fd").checked = !!c.fd;
          if (c.data_bitrate) $("#c-dbr").value = c.data_bitrate;
          $("#c-lo").checked = !!c.listen_only;
          update();
        };
        const update = () => {
          const b = backends.find((x) => x.name === ifSel.value) || {};
          $("#c-fd").disabled = !b.fd; if (!b.fd) $("#c-fd").checked = false;
          $("#c-dbr").disabled = !$("#c-fd").checked;
          $("#c-lo").disabled = !b.listen_only && b.name !== "virtual"; if ($("#c-lo").disabled) $("#c-lo").checked = false;
          $("#c-br").disabled = b.name === "socketcan";
          $("#c-ch").placeholder = "default: " + b.default_channel;
          note.textContent = b.needs ? "Needs: " + b.needs : "";
          $("#c-sim-row").hidden = b.name !== "virtual";
        };
        ifSel.onchange = update; $("#c-fd").onchange = update;
        setVals({ interface: "virtual", bitrate: 250000, data_bitrate: 2000000, ...last });
        const devSel = $("#c-dev");
        const loadDevices = async () => {
          devSel.innerHTML = `<option value="">Searching&hellip;</option>`;
          const d = await CO.tryApi("/api/devices");
          const devs = (d && d.devices) || [];
          devSel.innerHTML = `<option value="">${devs.length ? "Pick a detected adapter&hellip;" : "None found"}</option>` +
            devs.map((x, i) => `<option value="${i}">${CO.esc(x.label)}${x.detail ? " - " + CO.esc(x.detail) : ""}</option>`).join("");
          devSel.onchange = () => { const x = devs[+devSel.value]; if (x) { ifSel.value = x.interface; $("#c-ch").value = x.channel; update(); } };
        };
        $("#c-refresh").onclick = loadDevices;
        loadDevices();
      },
    });
    if (!res) return;
    const f = res.form, $ = (s) => CO.$(s, f);
    const body2 = { index: slot, interface: $("#c-if").value, channel: $("#c-ch").value.trim() || null,
      bitrate: +$("#c-br").value, fd: $("#c-fd").checked, data_bitrate: +$("#c-dbr").value, listen_only: $("#c-lo").checked };
    if (body2.interface === "virtual" && $("#c-sim").checked) await CO.tryApi("/api/sim/start", { channel: body2.channel || "canopener" });
    const st = await CO.tryApi("/api/connect", body2);
    if (st) {
      CO.toast(`${st.name} connected`, "good");
      CO.state.settings = await CO.api("/api/settings");
    }
  };

  CO.disconnect = async (index) => { await CO.tryApi("/api/disconnect", { index }); };

  // ---------------------------------------------------------------- chips + status bar
  function renderChips() {
    const el = CO.$("#chips");
    const html = CO.state.channels.map((c) => {
      const cfg = c.config || {};
      const desc = c.state === "file" ? "file" : `${CO.esc(c.backend)} ${CO.esc(cfg.channel ?? "")} ${fmtRate(cfg.bitrate)}${cfg.fd ? " FD" : ""}${cfg.listen_only ? " LO" : ""}`;
      const title = c.error ? CO.esc(c.error) : "";
      return `<span class="chip" title="${title}"><span class="dot ${c.state}"></span><b>${CO.esc(c.name)}</b><span class="muted">${desc}</span>` +
        `<button class="x" data-dc="${c.index}" title="${c.state === "file" ? "Close" : "Disconnect"}">&times;</button></span>`;
    }).join("");
    if (el._html !== html) { el.innerHTML = html; el._html = html; }
  }
  CO.$("#chips").addEventListener("click", (ev) => { const b = ev.target.closest("[data-dc]"); if (b) CO.disconnect(+b.dataset.dc); });

  function renderStatus() {
    const el = CO.$("#status-channels");
    if (!CO.state.connected) { el.innerHTML = `<span style="color:var(--bad)">Server not reachable - reconnecting&hellip;</span>`; return; }
    if (!CO.state.channels.length) { el.textContent = "Not connected - click + Connect"; return; }
    el.innerHTML = CO.state.channels.map((c) => {
      if (c.state === "file") return `<span><b>${CO.esc(c.name)}</b> file view, ${CO.fmtNum(c.rx + c.tx)} frames</span>`;
      const load = Math.min(100, c.load || 0);
      const cls = load > 80 ? "vhi" : load > 50 ? "hi" : "";
      const errs = c.err_frames + c.rx_errors + c.tx_errors;
      return `<span><b>${CO.esc(c.name)}</b> <span class="load"><i class="${cls}" style="width:${load}%"></i></span>${load.toFixed(1)}% ` +
        `${CO.fmtNum(Math.round(c.fps))} fr/s ` +
        (c.bus_state && c.bus_state !== "ACTIVE" ? `<span class="badge warn">${CO.esc(c.bus_state)}</span> ` : "") +
        (errs ? `<span class="badge bad" title="error frames / receive errors / send errors">${c.err_frames}/${c.rx_errors}/${c.tx_errors} err</span>` : "") +
        (c.state === "error" ? ` <span class="badge bad" title="${CO.esc(c.error)}">adapter error</span>` : "") + `</span>`;
    }).join("");
  }

  function renderRec() {
    const el = CO.$("#rec-indicator");
    const r = CO.state.recording;
    if (r && !r.stopped) {
      el.hidden = false;
      el.textContent = `REC ${r.written.toLocaleString()} frames ${r.duration}s`;
    } else el.hidden = true;
  }

  // ---------------------------------------------------------------- events drawer
  let unseen = 0;
  function addEvents(evs) {
    const list = CO.$("#events-list");
    for (const [seq, ts, level, text] of evs) {
      CO.state.events.push([seq, ts, level, text]);
      const div = document.createElement("div");
      div.className = level;
      div.textContent = `${CO.fmtTime(ts)}  ${text}`;
      list.appendChild(div);
      if (level === "warn" || level === "error") CO.toast(text, "warn");
    }
    while (list.children.length > 500) list.firstChild.remove();
    list.scrollTop = list.scrollHeight;
    if (CO.$("#events").hidden) { unseen += evs.length; const b = CO.$("#event-badge"); b.hidden = false; b.textContent = unseen; }
  }
  CO.$("#btn-events").onclick = () => { const d = CO.$("#events"); d.hidden = !d.hidden; if (!d.hidden) { unseen = 0; CO.$("#event-badge").hidden = true; } };
  CO.$("#btn-events-close").onclick = () => (CO.$("#events").hidden = true);

  CO.onFeed((msg) => {
    if (msg.type) { renderStatus(); return; }
    renderChips(); renderStatus(); renderRec();
    if (msg.events) addEvents(msg.events);
  });

  // ---------------------------------------------------------------- theme, about, projects
  function applyTheme(t) { document.documentElement.dataset.theme = t; CO.prefs.set("theme", t); }
  applyTheme(CO.prefs.get("theme", "dark"));
  CO.$("#btn-theme").onclick = () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  CO.$("#btn-connect").onclick = () => CO.openConnect();
  document.addEventListener("keydown", (ev) => { if (ev.ctrlKey && ev.key.toLowerCase() === "k") { ev.preventDefault(); CO.openConnect(); } });

  CO.$("#btn-about").onclick = async () => {
    const a = await CO.api("/api/about");
    CO.modal({ title: "About CANopener", body: `<div class="kv">
      <span>Version</span><span>${CO.esc(a.version)}${a.commit ? ` (${CO.esc(a.commit)}${a.dirty ? ", modified" : ""})` : ""}</span>
      <span>Built</span><span>${CO.esc(a.built || "from source")}</span>
      <span>App folder</span><span class="mono">${CO.esc(a.app_dir)}</span>
      <span>MCP (Claude)</span><span class="mono">${location.origin}/mcp/</span>
      <span>Plugins</span><span>${a.extensions.length} loaded${a.extension_errors.length ? `, ${a.extension_errors.length} failed` : ""}</span>
      <span>Licence</span><span>MIT</span></div>
      <p class="small muted">Open-source CAN toolbox: receive, trace, transmit, record &amp; replay, statistics, CANopen node commissioning.
      Recordings and logs are in the app folder (recordings\\, logs\\).</p>
      <p><a class="btn primary" href="/guide/user-guide.html" target="_blank" rel="noopener">User guide</a></p>` });
  };

  CO.$("#btn-project").onclick = async () => {
    const d = await CO.api("/api/projects");
    const list = d.projects.map((p) => `<option value="${CO.esc(p.name)}">${CO.esc(p.name)}</option>`).join("");
    const res = await CO.modal({ title: "Projects", body: `
      <p class="small muted">A project keeps the transmit list, the adapter connections, the decoders (DBC, EDS, J1939), the dashboards and the quick signals.</p>
      <label class="field"><span>Save current setup as</span><input id="p-name" value="${CO.esc(d.current || "")}" placeholder="e.g. test rig 2"></label>
      <label class="field"><span>Or open</span><select id="p-open" ${d.projects.length ? "" : "disabled"}>${list || "<option>no saved projects</option>"}</select></label>`,
      buttons: [{ label: "Cancel", value: "cancel" }, { label: "Open", value: "open", disabled: !d.projects.length }, { label: "Save", value: "save", cls: "primary" }] });
    if (!res) return;
    if (res.value === "save") {
      const r = await CO.tryApi("/api/projects/save", { name: CO.$("#p-name", res.form).value.trim(), ui: CO.collectUi ? CO.collectUi() : {} });
      if (r) CO.toast(`Saved project ${r.name}`, "good");
    } else {
      const r = await CO.tryApi("/api/projects/load", { name: CO.$("#p-open", res.form).value });
      if (!r) return;
      CO.toast(`Opened ${r.name}: ${r.tx} transmit entries`, "good");
      CO.emitProject(r);                              // transmit list, dashboards, decoders reload
      if (r.connections.length && !CO.state.channels.length &&
          await CO.confirm("Connect?", `Connect the project's adapters now?<br>${r.connections.map((c) => CO.esc(`CAN${c.index + 1}: ${c.interface} ${c.channel ?? ""} ${c.bitrate / 1000} kbit/s`)).join("<br>")}`, "Connect")) {
        for (const c of r.connections) await CO.tryApi("/api/connect", c);
      }
    }
  };
})();
