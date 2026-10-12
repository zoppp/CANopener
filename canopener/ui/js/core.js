/* CANopener UI core: API calls, the live feed (WebSocket), tabs, dialogs, toasts.
   window.canopener (alias CO) is also the plugin API - see docs/PLUGINS.md. */
"use strict";
(function () {
  const F = { EXT: 1, RTR: 2, ERR: 4, FD: 8, BRS: 16, ESI: 32, TX: 64 };
  const CO = {
    F,
    state: { channels: [], about: {}, settings: {}, backends: [], bitrates: [], dataBitrates: [],
             recording: null, replay: null, job: null, txLive: {}, events: [], connected: false,
             signals: {} },   // decoded signals by key, latest value (see canopener/decode/signals.py)
    tabs: [], current: null,
  };
  window.canopener = window.CO = CO;

  // ---------------------------------------------------------------- helpers
  CO.esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  CO.$ = (sel, root) => (root || document).querySelector(sel);
  CO.$$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  CO.idText = (id, flags) => (flags & F.EXT ? id.toString(16).toUpperCase().padStart(8, "0")
                                             : id.toString(16).toUpperCase().padStart(3, "0"));
  CO.typeText = (flags) => {
    if (flags & F.ERR) return "Error";
    if (flags & F.RTR) return flags & F.EXT ? "Ext RTR" : "RTR";
    let t = flags & F.EXT ? "Ext" : "Std";
    if (flags & F.FD) t = (flags & F.EXT ? "Ext " : "") + (flags & F.BRS ? "FD BRS" : "FD");
    return t;
  };
  CO.spaced = (hex) => (hex || "").replace(/(..)(?=.)/g, "$1 ");
  CO.fmtMs = (v) => (v === undefined || v === null || v === 0 ? "" : v >= 1000 ? (v / 1000).toFixed(2) + " s" : v.toFixed(1));
  CO.fmtNum = (v) => (v ?? 0).toLocaleString();
  CO.fmtBytes = (n) => n > 1e6 ? (n / 1e6).toFixed(1) + " MB" : n > 1e3 ? (n / 1e3).toFixed(0) + " kB" : n + " B";
  CO.fmtTime = (ts) => { const d = new Date(ts * 1000); return d.toLocaleTimeString([], { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0"); };
  CO.chName = (i) => { const c = CO.state.channels.find((c) => c.index === i); return c ? c.name : "CAN" + (i + 1); };
  CO.liveChannels = () => CO.state.channels.filter((c) => c.state === "open" || c.state === "error");
  CO.parseHexId = (s) => { s = String(s || "").trim().replace(/^0x/i, "").replace(/h$/i, ""); if (!/^[0-9a-f]+$/i.test(s)) return NaN; return parseInt(s, 16); };

  // ---------------------------------------------------------------- prefs (per PC, localStorage)
  CO.prefs = {
    get(key, dflt) { try { const v = localStorage.getItem("co." + key); return v === null ? dflt : JSON.parse(v); } catch (e) { return dflt; } },
    set(key, val) { try { localStorage.setItem("co." + key, JSON.stringify(val)); } catch (e) { /* private mode */ } },
  };

  // ---------------------------------------------------------------- API
  CO.api = async (path, body, method) => {
    const opt = { method: method || (body === undefined ? "GET" : "POST"), headers: {} };
    if (body !== undefined && !(body instanceof FormData)) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    else if (body instanceof FormData) opt.body = body;
    const r = await fetch(path, opt);
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (!r.ok) { const err = new Error((data && (data.error || data.detail)) || r.statusText); err.status = r.status; err.data = data; throw err; }
    return data;
  };
  CO.tryApi = async (path, body, okMsg, method) => {
    try { const d = await CO.api(path, body, method); if (okMsg) CO.toast(okMsg, "good"); return d; }
    catch (e) { CO.toast(e.message, "bad"); return null; }
  };

  // ---------------------------------------------------------------- toasts
  CO.toast = (msg, kind, ms) => {
    const el = document.createElement("div");
    el.className = "toast " + (kind || "");
    el.textContent = msg;
    CO.$("#toasts").appendChild(el);
    setTimeout(() => el.remove(), ms || (kind === "bad" ? 7000 : 3500));
  };

  // ---------------------------------------------------------------- dialogs
  /* CO.modal({title, body: html, buttons: [{label, value, cls, disabled}], onOpen(form), wide})
     resolves with {value, form} when a button is clicked or null when cancelled (Esc). */
  CO.modal = (opt) => new Promise((resolve) => {
    const dlg = CO.$("#modal"), form = CO.$("#modal-form");
    const buttons = opt.buttons || [{ label: "Close", value: "close" }];
    form.innerHTML = `<div class="dlg-head">${CO.esc(opt.title || "")}</div><div class="dlg-body">${opt.body || ""}</div>
      <div class="dlg-foot">${buttons.map((b, i) => `<button class="btn ${b.cls || ""}" value="${CO.esc(b.value)}" data-i="${i}" ${b.disabled ? "disabled" : ""}>${CO.esc(b.label)}</button>`).join("")}</div>`;
    dlg.style.width = opt.wide ? "min(1100px, 96vw)" : "";
    let done = false;
    const finish = (v) => { if (done) return; done = true; resolve(v); };
    form.onsubmit = (ev) => {
      ev.preventDefault();
      const btn = ev.submitter;
      const value = btn ? btn.value : null;
      if (opt.validate && value && value !== "cancel" && value !== "close") {
        const msg = opt.validate(value, form);
        if (msg) { CO.toast(msg, "bad"); return; }
      }
      dlg.close();
      finish(value === "cancel" ? null : { value, form });
    };
    // one <dialog> serves every modal: the close event of the previous modal can arrive after this one opened
    dlg.onclose = () => { if (!dlg.open) finish(null); };
    dlg.showModal();
    if (opt.onOpen) opt.onOpen(form);
  });
  CO.confirm = async (title, html, okLabel, danger) => {
    const r = await CO.modal({ title, body: html, buttons: [{ label: "Cancel", value: "cancel" }, { label: okLabel || "OK", value: "ok", cls: danger ? "danger solid" : "primary" }] });
    return !!r;
  };

  // ---------------------------------------------------------------- tabs (also for plugins)
  /* CO.registerTab({id, title, render(el), onShow(el), onHide(el), order}) */
  CO.registerTab = (tab) => {
    tab.order = tab.order ?? 100;
    CO.tabs = CO.tabs.filter((t) => t.id !== tab.id).concat([tab]).sort((a, b) => a.order - b.order);
    if (CO._started) CO.renderTabs();
  };
  CO.renderTabs = () => {
    const nav = CO.$("#tabs");
    nav.innerHTML = CO.tabs.map((t) => `<button data-tab="${CO.esc(t.id)}" class="${CO.current === t.id ? "active" : ""}">${CO.esc(t.title)}</button>`).join("");
    nav.onclick = (ev) => { const b = ev.target.closest("button[data-tab]"); if (b) CO.show(b.dataset.tab); };
  };
  CO.show = (id) => {
    const tab = CO.tabs.find((t) => t.id === id) || CO.tabs[0];
    if (!tab) return;
    const prev = CO.tabs.find((t) => t.id === CO.current);
    if (prev && prev.onHide) { try { prev.onHide(prev._el); } catch (e) { console.error(e); } }
    CO.current = tab.id;
    CO.prefs.set("tab", tab.id);
    const view = CO.$("#view");
    view.querySelectorAll(":scope > .tabview").forEach((el) => (el.hidden = true));
    if (!tab._el) {
      tab._el = document.createElement("div");
      tab._el.className = "tabview pane";
      tab._el.style.flex = "1";
      tab._el.style.minHeight = "0";
      view.appendChild(tab._el);
      try { tab.render(tab._el); } catch (e) { tab._el.innerHTML = `<div class="notice bad">${CO.esc(e.message)}</div>`; console.error(e); }
    }
    tab._el.hidden = false;
    if (tab.onShow) {                                   // a broken plugin tab must not stop the app
      try { const r = tab.onShow(tab._el); if (r && r.catch) r.catch((e) => console.error(e)); } catch (e) { console.error(e); }
    }
    CO.renderTabs();
  };

  // ---------------------------------------------------------------- live feed
  const feedListeners = [];
  CO.onFeed = (fn) => feedListeners.push(fn);
  let ws = null, wsBackoff = 500;
  CO.wsSend = (obj) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); };
  CO.connectFeed = () => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => { wsBackoff = 500; CO.state.connected = true; feedListeners.forEach((fn) => { try { fn({ type: "open" }); } catch (e) { console.error(e); } }); };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      CO.state.channels = msg.channels || [];
      if ("recording" in msg) CO.state.recording = msg.recording;
      if ("replay" in msg) CO.state.replay = msg.replay;
      if (msg.tx) msg.tx.forEach((t) => (CO.state.txLive[t.id] = t));
      if (msg.job) CO.state.job = msg.job;
      if (msg.signals_reset) CO.state.signals = {};
      if (msg.signals) for (const sg of msg.signals) CO.state.signals[sg.key] = sg;   // {key, ts, value}
      for (const fn of feedListeners) { try { fn(msg); } catch (e) { console.error(e); } }
    };
    ws.onclose = () => {
      CO.state.connected = false;
      feedListeners.forEach((fn) => { try { fn({ type: "closed" }); } catch (e) { /* */ } });
      setTimeout(CO.connectFeed, wsBackoff);
      wsBackoff = Math.min(wsBackoff * 2, 5000);
    };
  };

  // ---------------------------------------------------------------- project loaded (also for plugins)
  const projectListeners = [];
  CO.onProject = (fn) => projectListeners.push(fn);
  CO.emitProject = (info) => projectListeners.forEach((fn) => { try { const r = fn(info); if (r && r.catch) r.catch(console.error); } catch (e) { console.error(e); } });

  // ---------------------------------------------------------------- plugin helpers
  CO.loadScript = (src) => new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src; s.onload = resolve; s.onerror = () => reject(new Error("cannot load " + src));
    document.body.appendChild(s);
  });
  CO.channelSelect = (selected, opts) => {
    const chans = (opts && opts.includeFiles) ? CO.state.channels : CO.liveChannels();
    if (!chans.length) return `<select disabled><option>no channel</option></select>`;
    return `<select class="ch-select">${chans.map((c) => `<option value="${c.index}" ${c.index === selected ? "selected" : ""}>${CO.esc(c.name)}</option>`).join("")}</select>`;
  };
})();
