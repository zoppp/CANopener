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
  /* Transmit data text -> the bytes the server sends, as packed upper-case hex (null if it would refuse it).
     Mirrors core/transmit.py parse_data: "0x" goes, commas split, one-digit words are bytes, one long word is packed. */
  CO.dataHex = (text) => {
    const s = String(text || "").replace(/,/g, " ").replace(/0x/gi, "").trim();
    if (!s) return "";
    let parts = s.split(/\s+/);
    if (parts.length === 1 && parts[0].length > 2) {
      if (parts[0].length % 2) return null;
      parts = parts[0].match(/../g);
    }
    if (!parts.every((p) => /^[0-9a-f]+$/i.test(p) && parseInt(p, 16) <= 0xFF)) return null;
    return parts.map((p) => parseInt(p, 16).toString(16).toUpperCase().padStart(2, "0")).join("");
  };
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
  /* an open modal <dialog> sits in the top layer above everything in <body>, so while it is open
     #toasts lives inside it (and goes back to <body> when it closes) */
  const placeToasts = () => {
    const box = CO.$("#toasts"), dlg = CO.$("#modal");
    const host = dlg.open ? dlg : document.body;
    if (box.parentNode !== host) host.appendChild(box);
  };
  CO.toast = (msg, kind, ms) => {
    const el = document.createElement("div");
    el.className = "toast " + (kind || "");
    el.textContent = msg;
    CO.$("#toasts").appendChild(el);
    placeToasts();
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
      placeToasts();
      finish(value === "cancel" ? null : { value, form });
    };
    // one <dialog> serves every modal: the close event of the previous modal can arrive after this one opened
    dlg.onclose = () => { placeToasts(); if (!dlg.open) finish(null); };
    dlg.showModal();
    placeToasts();
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

  // ---------------------------------------------------------------- clipboard (also for plugins)
  CO.copyText = async (text, what) => {
    let ok = false;
    try { await navigator.clipboard.writeText(text); ok = true; } catch (e) {
      const focus = document.activeElement, ta = document.createElement("textarea");   // no clipboard permission
      ta.value = text; ta.style.cssText = "position:fixed;left:-9999px;top:0";
      (document.querySelector("dialog[open]") || document.body).appendChild(ta);
      ta.select();
      try { ok = document.execCommand("copy"); } catch (e2) { ok = false; }
      ta.remove();
      if (focus && focus.focus) focus.focus({ preventScroll: true });
    }
    CO.toast(ok ? "Copied" + (what ? " " + what : "") : "Copying failed", ok ? "good" : "bad", ok ? 1800 : 0);
    return ok;
  };
  /* Text of the clipboard for a menu "Paste" item, or null. Reading needs a permission the window may not have:
     then the user is told to press Ctrl+V (handle that with a `paste` event and ev.clipboardData). */
  CO.readClipboard = async (where) => {
    try { return await navigator.clipboard.readText(); } catch (e) {
      CO.toast(`Cannot read the clipboard from a menu here: click ${where || "the list"} and press Ctrl+V`, "warn");
      return null;
    }
  };
  /* Tab-separated text of table rows with a header line (pastes into Excel and text editors). Columns with an
     empty header (button columns) are left out. */
  CO.tableText = (trs) => {
    trs = trs.filter((tr) => tr && !tr.querySelector("td.empty"));
    if (!trs.length) return "";
    const table = trs[0].closest("table"), head = table && table.tHead && table.tHead.rows[0];
    const ths = head ? Array.from(head.cells) : [];
    const cell = (c) => c.textContent.replace(/\s+/g, " ").trim();
    const line = (cells) => cells.filter((c, i) => !ths.length || (ths[i] && cell(ths[i]))).map(cell).join("\t");
    return (ths.length ? [line(ths)] : []).concat(trs.map((tr) => line(Array.from(tr.cells)))).join("\n");
  };
  /* Frames from pasted text: lines of "ID data" (18FEF100 FF 80 20 00), candump (123#1122, 123#R, 123##1AABB,
     "(1.5) can0 123#11"), .trc lines (1.0, 1.1, 2.x) or rows copied from the receive list, trace or transmit list
     (tab separated, with their header line).
     Returns {frames: [{can_id, ext, fd, brs, rtr, dlc, data, channel}], bad: [line]}; channel is null if unknown. */
  CO.parseFrames = (text) => {
    const frames = [], bad = [];
    let cols = null;
    const chIndex = (s) => {
      if (s === undefined || s === null || s === "") return null;
      const c = CO.state.channels.find((x) => x.name === s);
      return c ? c.index : /^\d+$/.test(s) ? +s - 1 : null;
    };
    for (const raw of String(text || "").split(/\r?\n/)) {
      const line = raw.trim();
      if (!line || line.startsWith(";")) continue;
      const cells = raw.includes("\t") ? raw.split("\t").map((s) => s.trim()) : null;
      if (cells && cells.includes("ID") && cells.includes("Data")) { cols = {}; cells.forEach((c, i) => (cols[c] = i)); continue; }
      let id, data, type = "", ch = null, dlc = null, strictId = false;
      const cd = /^(?:\([\d.]+\)\s+\S+\s+)?([0-9a-f]{1,8})(##?)([0-9a-f]*|R\d?)$/i.exec(line);   // candump
      if (cells && cols) {
        id = (cells[cols.ID] || "").split(" ")[0]; data = cells[cols.Data] || ""; type = cells[cols.Type] || "";
        ch = chIndex(cells[cols.Ch]); dlc = cols.DLC !== undefined ? cells[cols.DLC] : null;
      } else if (cd) {
        id = cd[1];
        if (cd[3].toUpperCase().startsWith("R")) { type = "RTR"; dlc = cd[3].slice(1) || null; data = ""; }
        else if (cd[2] === "##") { type = (parseInt(cd[3][0] || "0", 16) & 1) ? "FD BRS" : "FD"; data = CO.spaced(cd[3].slice(1)); }
        else data = CO.spaced(cd[3]);
        if (cd[1].length > 3) type = "Ext " + type;
      } else {
        let t = line.split(/\s+/);
        strictId = true;                                   // free text: only spaced bytes, and an id with a digit
        if (/^\d+\)$/.test(t[0]) && /^(Rx|Tx)$/i.test(t[2] || "")) { t = t.slice(3); dlc = t[1]; t.splice(1, 1); }   // .trc 1.1: "1) 12.5 Rx 0123 8 11 22"
        else if (/^\d+\)$/.test(t[0])) { t = t.slice(2); dlc = t[1]; t.splice(1, 1); }   // .trc 1.0: "1) 1841 0123 8 11 22"
        else if (/^\d+$/.test(t[0]) && /^[\d.]+$/.test(t[1] || "") && /^(DT|FD|FB|FE|BI|RR)$/.test(t[2] || "")) {
          // .trc 2.x: "1 12.500 DT 0123 Rx 8 11 22"; 2.1 adds a bus column before the id and "-" after Rx/Tx
          const typ = t[2], dir = t.findIndex((x, i) => i > 3 && /^(Rx|Tx)$/.test(x));
          if (dir < 0) { bad.push(line); continue; }
          const after = t[dir + 1] === "-" ? dir + 2 : dir + 1;
          id = t[dir - 1]; dlc = t[after]; data = t.slice(after + 1).join(" ");
          type = (typ === "RR" ? "RTR" : typ === "FB" ? "FD BRS" : typ === "FD" ? "FD" : "") + (id.length > 4 ? " Ext" : "");
          t = null;
        } else if (t.length > 2 && /^([1-9]|1[0-5])$/.test(t[1]) && /^[0-9a-f]{2}$/i.test(t[2])) {
          // "123 8 11 22 ...": a DLC in front of the data (a byte would be written "08"); if the count disagrees
          // the line is refused below
          dlc = t[1]; t.splice(1, 1);
        }
        if (t) { id = t[0]; data = t.slice(1).join(" "); }
      }
      const idm = /^(?:0x)?([0-9a-f]{1,8})h?$/i.exec(id || "");
      if (!idm || /error/i.test(type) || /error/i.test(data)) { bad.push(line); continue; }
      // a word like "add" or "dead" is not taken for an id unless written as 0x... or ...h
      if (strictId && !/\d/.test(idm[1]) && !/^0x|h$/i.test(id)) { bad.push(line); continue; }
      const can_id = parseInt(idm[1], 16);
      const rtr = /RTR/.test(type) || /^(remote|RTR)/i.test(data);
      let bytes = [];
      if (!rtr) {
        const parts = data.trim() ? data.trim().split(/[\s,]+/).map((b) => b.replace(/^0x/i, "")) : [];
        if (parts.length === 1 && parts[0].length > 2 && parts[0].length % 2 === 0) bytes = parts[0].match(/../g);
        else bytes = parts;
        if (bytes.some((b) => !/^[0-9a-f]{2}$/i.test(b)) || bytes.length > 64) { bad.push(line); continue; }
      }
      if (can_id > 0x1FFFFFFF) { bad.push(line); continue; }
      const n = parseInt(dlc, 10);
      // a DLC that does not match the bytes means the line was not read right (FD DLC codes 9-15 are 12-64 bytes)
      const code = { 9: 12, 10: 16, 11: 20, 12: 24, 13: 32, 14: 48, 15: 64 }[n];
      if (!rtr && dlc !== null && dlc !== undefined && dlc !== "" && Number.isFinite(n) && n !== bytes.length && code !== bytes.length) {
        bad.push(line); continue;
      }
      frames.push({ can_id, ext: /Ext/.test(type) || idm[1].length > 4 || can_id > 0x7FF, fd: bytes.length > 8 || /FD/.test(type), brs: /BRS/.test(type), rtr,
        dlc: rtr ? (n >= 0 && n <= 8 ? n : 0) : bytes.length, data: bytes.join(" ").toUpperCase(), channel: ch });
    }
    return { frames, bad };
  };

  // ---------------------------------------------------------------- right-click menus (also for plugins)
  const NOT_TEXT = /^(checkbox|radio|button|submit|reset|file|color|range|image)$/;
  CO.isEditable = (t) => {
    if (!t || !t.closest) return false;
    const inp = t.closest("input");
    return !!(t.closest("textarea, [contenteditable]:not([contenteditable=false])") || (inp && !NOT_TEXT.test(inp.type)));
  };
  let menu = null;
  const closeMenu = () => {
    if (!menu) return;
    const m = menu; menu = null;
    m.el.remove();
    document.removeEventListener("mousedown", m.out, true);
    document.removeEventListener("wheel", closeMenu, true);
    window.removeEventListener("blur", closeMenu); window.removeEventListener("resize", closeMenu);
    if (m.focus && m.focus.isConnected && m.focus.focus) m.focus.focus({ preventScroll: true });
  };
  /* CO.contextMenu(event, items) shows a menu at the mouse. items: [{label, action(), disabled, shortcut}] or
     {separator: true}; falsy items are skipped. Arrow keys, Enter and Esc work; a click elsewhere, the mouse wheel,
     resizing or leaving the window closes it. */
  CO.contextMenu = (ev, items) => {
    ev.preventDefault();
    closeMenu();
    const list = [];
    for (const it of (items || []).filter(Boolean)) if (!it.separator || (list.length && !list[list.length - 1].separator)) list.push(it);
    while (list.length && list[list.length - 1].separator) list.pop();
    if (!list.length) return;
    const el = document.createElement("div");
    el.className = "ctx-menu"; el.tabIndex = -1; el.setAttribute("role", "menu");
    el.innerHTML = list.map((it, i) => it.separator ? `<div class="ctx-sep"></div>`
      : `<div class="ctx-item${it.disabled ? " dis" : ""}" role="menuitem" data-i="${i}"><span>${CO.esc(it.label)}</span><kbd>${CO.esc(it.shortcut || "")}</kbd></div>`).join("");
    // a modal <dialog> is drawn above everything else: a menu for its content must live inside it
    ((ev.target && ev.target.closest && ev.target.closest("dialog[open]")) || document.body).appendChild(el);
    const w = el.offsetWidth, h = el.offsetHeight;
    let x = ev.clientX, y = ev.clientY;
    if (x + w > innerWidth - 4) x = Math.max(4, x - w);
    if (y + h > innerHeight - 4) y = Math.max(4, Math.min(y - h, innerHeight - h - 4));
    el.style.left = x + "px"; el.style.top = y + "px";
    const enabled = CO.$$(".ctx-item:not(.dis)", el);
    let cur = -1;
    const mark = (i) => { cur = i; enabled.forEach((e, j) => e.classList.toggle("cur", j === i)); };
    const run = (node) => {
      const it = list[+node.dataset.i];
      closeMenu();
      try { const r = it.action && it.action(); if (r && r.catch) r.catch((e) => CO.toast(e.message, "bad")); }
      catch (e) { CO.toast(e.message, "bad"); console.error(e); }
    };
    el.addEventListener("mousemove", (e) => { const n = e.target.closest(".ctx-item:not(.dis)"); mark(n ? enabled.indexOf(n) : -1); });
    el.addEventListener("click", (e) => { const n = e.target.closest(".ctx-item:not(.dis)"); if (n) run(n); });
    el.addEventListener("contextmenu", (e) => e.preventDefault());
    el.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const n = enabled.length;
        if (n) mark(e.key === "ArrowDown" ? (cur + 1) % n : cur <= 0 ? n - 1 : cur - 1);
      } else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); if (cur >= 0) run(enabled[cur]); }
      else if (e.key === "Escape" || e.key === "Tab") { e.preventDefault(); closeMenu(); }
    });
    const out = (e) => {
      if (el.contains(e.target)) return;
      closeMenu();
      // the click that closes the menu must not also press the button it lands on (Send, Delete, ...)
      const eat = (c) => { c.preventDefault(); c.stopPropagation(); };
      document.addEventListener("click", eat, { capture: true, once: true });
      setTimeout(() => document.removeEventListener("click", eat, { capture: true }), 600);
    };
    menu = { el, out, focus: document.activeElement };
    document.addEventListener("mousedown", out, true);
    document.addEventListener("wheel", closeMenu, true);     // not "scroll": the live trace scrolls by itself
    window.addEventListener("blur", closeMenu); window.addEventListener("resize", closeMenu);
    el.focus({ preventScroll: true });
  };
  /* CO.onContextMenu(el, fn): fn(event) returns the items of the menu for that spot (or null for none).
     Text fields keep the native Cut / Copy / Paste menu; selected text gets a Copy item on top. */
  const selectionItem = (ev) => {
    const s = getSelection(), text = String(s || "");
    return text.trim() && s.containsNode(ev.target, true) ? [{ label: "Copy", shortcut: "Ctrl+C", action: () => CO.copyText(text) }, { separator: true }] : [];
  };
  CO.onContextMenu = (el, fn) => el.addEventListener("contextmenu", (ev) => {
    if (ev.defaultPrevented || CO.isEditable(ev.target)) return;
    const items = fn(ev);
    if (items) CO.contextMenu(ev, selectionItem(ev).concat(items));
  });
  document.addEventListener("contextmenu", (ev) => {
    if (ev.defaultPrevented || CO.isEditable(ev.target)) return;
    ev.preventDefault();                                   // never the browser's Back / Reload / Print menu
    const items = selectionItem(ev);
    if (items.length) CO.contextMenu(ev, items);
  });

  /* True while the user selects text in el (a text selection inside it, or the mouse button went down in it):
     live views skip re-rendering el then, so the selection is not destroyed. */
  let downIn = null;
  document.addEventListener("mousedown", (ev) => (downIn = ev.target), true);
  document.addEventListener("mouseup", () => (downIn = null), true);
  // a mouseup that never arrives (a native select list, drag and drop, leaving the window) must not keep views frozen
  for (const t of ["dragend", "pointercancel"]) document.addEventListener(t, () => (downIn = null), true);
  window.addEventListener("blur", () => (downIn = null));
  CO.selecting = (el) => {
    if (!el || !el.isConnected || el.offsetParent === null) return false;      // hidden views never wait
    if (downIn && el.contains(downIn)) return true;
    const s = getSelection();
    if (!s || s.isCollapsed || !String(s).trim() || !s.rangeCount) return false;
    // only a selection that starts or ends inside el (Ctrl+A on the whole page must not freeze every view)
    const r = s.getRangeAt(0);
    return el.contains(r.startContainer) || el.contains(r.endContainer);
  };

  // ---------------------------------------------------------------- row selection (also for plugins)
  /* CO.rowSelect({el, rows, key(tr), keys(), text(keys), menu(keys, rowKey, ev), onDragEnd()})
     el: the scrolling element around the rows (gets keyboard focus); rows: selector of a row (default tr[data-k]);
     key(tr): the row's stable key (default tr.dataset.k); keys(): every shown key in display order.
     Click selects, Shift+click a range, Ctrl+click toggles, drag selects a range (scrolls at the edges),
     Ctrl+A selects all, Esc clears, Ctrl+C copies text(keys) (default: the rows as shown, tab separated).
     The selection is a set of keys, so it survives re-rendering: render a row with class "sel" when has(key).
     busy is true while the mouse button is held: skip re-rendering then. */
  const scroller = (el) => {                              // el, or the nearest ancestor that scrolls
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      if (/(auto|scroll)/.test(getComputedStyle(n).overflowY) && n.scrollHeight > n.clientHeight) return n;
    }
    return document.scrollingElement || el;
  };
  CO.rowSelect = (o) => {
    const el = o.el, rowSel = o.rows || "tr[data-k]", keyOf = o.key || ((tr) => tr.dataset.k);
    const set = new Set();
    let anchor = null;
    if (!el.hasAttribute("tabindex")) el.tabIndex = 0;
    el.classList.add("rowsel");
    const range = (a, b) => {
      const ks = o.keys();
      let i = ks.indexOf(a), j = ks.indexOf(b);
      if (j < 0) return [];
      if (i < 0) i = j;
      return i <= j ? ks.slice(i, j + 1) : ks.slice(j, i + 1);
    };
    const rowAt = (x, y) => {
      const r = el.getBoundingClientRect();
      x = Math.min(Math.max(x, r.left + 4), r.right - 20); y = Math.min(Math.max(y, r.top + 2), r.bottom - 4);
      for (const dy of [0, 26]) {                          // 26: below a sticky header row
        const t = document.elementFromPoint(x, y + dy), tr = t && t.closest(rowSel);
        if (tr && el.contains(tr)) return tr;
      }
      return null;
    };
    const s = {
      busy: false,
      has: (k) => set.has(k),
      get size() { return set.size; },
      get anchor() { return set.has(anchor) ? anchor : null; },
      selected: () => { const ks = o.keys(); return set.size ? ks.filter((k) => set.has(k)) : []; },   // vanished rows drop out
      set(keys) { set.clear(); for (const k of keys) set.add(k); s.apply(); },
      clear() { set.clear(); anchor = null; s.apply(); },
      apply() { for (const tr of el.querySelectorAll(rowSel)) tr.classList.toggle("sel", set.has(keyOf(tr))); },
      rowsFor(keys) { const m = new Map(CO.$$(rowSel, el).map((tr) => [keyOf(tr), tr])); return keys.map((k) => m.get(k)).filter(Boolean); },
      text: (keys) => (o.text ? o.text(keys) : CO.tableText(s.rowsFor(keys))),
      copy() {
        const ks = s.selected();
        if (!ks.length) { CO.toast("Select rows first (click, Shift+click, drag, Ctrl+A)", "warn"); return; }
        CO.copyText(s.text(ks), ks.length === 1 ? "1 row" : `${ks.length} rows`);
      },
    };
    el.addEventListener("mousedown", (ev) => {
      if (ev.button !== 0) return;
      const tr = ev.target.closest(rowSel);
      if (!tr || !el.contains(tr) || ev.target.closest("button, input, select, textarea, a, label")) return;
      ev.preventDefault();                                // select rows, not text
      const ts = getSelection();
      if (ts && !ts.isCollapsed) ts.removeAllRanges();     // (preventDefault keeps an old text selection alive)
      el.focus({ preventScroll: true });
      const k = keyOf(tr);
      if (ev.ctrlKey || ev.metaKey) { if (set.has(k)) set.delete(k); else set.add(k); anchor = k; s.apply(); return; }
      if (ev.shiftKey && anchor !== null) { s.set(range(anchor, k)); return; }
      anchor = k; s.set([k]);
      s.busy = true;
      let x = ev.clientX, y = ev.clientY;
      const extend = () => { const t = rowAt(x, y); if (t) s.set(range(anchor, keyOf(t))); };
      const move = (e) => { x = e.clientX; y = e.clientY; extend(); };
      const sc = scroller(el);
      const timer = setInterval(() => {                    // scroll while the mouse is held above or below the list
        const r = sc.getBoundingClientRect(), top = Math.max(r.top, 0) + 26, bottom = Math.min(r.bottom, innerHeight) - 4;
        if (y < top) sc.scrollTop -= Math.min(200, Math.max(8, top - y));
        else if (y > bottom) sc.scrollTop += Math.min(200, Math.max(8, y - bottom));
        else return;
        extend();
      }, 40);
      const up = (e) => {
        move(e);                                           // a quick drag can end before its last mousemove
        s.busy = false; clearInterval(timer);
        document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up);
        if (o.onDragEnd) setTimeout(o.onDragEnd, 0);       // after click / dblclick: a re-render now would eat them
      };
      document.addEventListener("mousemove", move); document.addEventListener("mouseup", up);
    });
    el.addEventListener("keydown", (ev) => {
      if (CO.isEditable(ev.target) || ev.target.closest("select")) return;
      const ctrl = ev.ctrlKey || ev.metaKey, k = ev.key.toLowerCase();
      if (ctrl && k === "a") { ev.preventDefault(); s.set(o.keys()); }
      else if (ctrl && k === "c" && set.size) { ev.preventDefault(); s.copy(); }
      else if (ev.key === "Escape" && set.size) { ev.preventDefault(); s.clear(); }
    });
    if (o.menu) CO.onContextMenu(el, (ev) => {
      const tr = ev.target.closest(rowSel);
      const k = tr && el.contains(tr) ? keyOf(tr) : null;
      if (k !== null && !set.has(k)) { anchor = k; s.set([k]); }
      el.focus({ preventScroll: true });
      return o.menu(s.selected(), k, ev);
    });
    return s;
  };
  CO.rowsLabel = (n) => (n > 1 ? `${n} rows` : "row");

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
