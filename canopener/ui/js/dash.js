/* Dashboards tab: named dashboards of live widgets (time plot, XY plot, gauge, numeric tile, bit lamp) on a
   12-column grid, and the Quick signals editor (signals cut out of raw frames, see canopener/dash/).
   Widgets read CO.state.signals (10 Hz feed); plots fetch /api/signals/history when they open. Every widget
   is destroyed when the tab hides or another dashboard is shown, so nothing keeps running in the background. */
"use strict";
(function () {
  const CO = window.canopener, $ = CO.$, esc = CO.esc;
  const COLS = 12, ROW_H = 40, GAP = 8;
  const WINDOWS = [10, 30, 60, 120, 300, 600];
  const PALETTE = ["#3b82f6", "#f97316", "#22c55e", "#e11d48", "#a855f7", "#14b8a6", "#eab308", "#94a3b8"];
  const TYPES = { plot: "Time plot", xy: "XY plot", gauge: "Gauge", number: "Numeric tile", lamp: "Bit lamp" };
  const SIZES = { plot: [6, 6], xy: [4, 6], gauge: [3, 4], number: [2, 2], lamp: [2, 2] };
  const FORMATS = [["auto", "auto"], ["0", "0"], ["1", "0.0"], ["2", "0.00"], ["3", "0.000"], ["hex", "hex"]];

  let tabEl = null, active = false, editing = false, saveTimer = null;
  let dashes = [], cur = null;      // all dashboards, the shown one
  let views = [];                   // live widgets of the shown dashboard: {w, el, update(msg), destroy()}
  let sigList = [], sigInfo = {};   // /api/signals: list and key -> info (unit, min, max)

  // ---------------------------------------------------------------- helpers
  const color = (c, dflt) => (/^#[0-9a-f]{6}$/i.test(c || "") ? c : dflt);
  const num = (v) => (v === "" || v === null || v === undefined || isNaN(+v) ? null : +v);
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const numeric = (v) => (typeof v === "boolean" ? (v ? 1 : 0) : typeof v === "number" && isFinite(v) ? v : null);
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const sigValue = (key) => { const s = CO.state.signals[key]; return s ? s.value : undefined; };
  const winOf = (w) => clamp(+w.window || 60, 10, 600);
  const newId = () => Math.random().toString(16).slice(2, 10);
  const canon = (v) => JSON.stringify(v, (k, x) => (x && typeof x === "object" && !Array.isArray(x)
    ? Object.fromEntries(Object.entries(x).sort()) : x));

  function fmtValue(v, format) {
    if (v === undefined || v === null) return "-";
    if (typeof v === "boolean") return v ? "true" : "false";
    if (typeof v !== "number") return String(v);
    if (format === "hex") return (v < 0 ? "-0x" : "0x") + Math.abs(Math.round(v)).toString(16).toUpperCase();
    if (format && format !== "auto") return v.toFixed(+format);
    return Number.isInteger(v) ? String(v) : String(+v.toPrecision(5));
  }

  function thresholdColor(list, v, dflt) {        // colour of the highest threshold at or below v
    let c = dflt;
    for (const t of (list || []).slice().sort((a, b) => a.value - b.value)) if (v >= t.value) c = color(t.color, c);
    return c;
  }

  async function loadSignals() {
    const d = await CO.api("/api/signals").catch(() => ({ signals: [] }));
    sigList = d.signals;
    sigInfo = Object.fromEntries(sigList.map((s) => [s.key, s]));
  }

  function place(el, w) {
    el.style.gridColumn = `${w.x + 1} / span ${w.w}`;
    el.style.gridRow = `${w.y + 1} / span ${w.h}`;
  }

  function plotAxes() {
    const fg = cssVar("--fg2"), line = cssVar("--line");
    const ax = { stroke: fg, grid: { stroke: line, width: 1 }, ticks: { stroke: line, width: 1 }, font: "11px Segoe UI, sans-serif" };
    return [ax, { ...ax, size: 56 }];
  }

  function sizePlot(u, body) {
    const leg = u.root.querySelector(".u-legend");
    u.setSize({ width: Math.max(80, body.clientWidth), height: Math.max(50, body.clientHeight - (leg ? leg.offsetHeight : 0) - 2) });
  }

  // ---------------------------------------------------------------- widgets
  function numberView(w, body) {
    body.innerHTML = `<div class="dash-num"><span class="v">-</span><span class="u"></span></div>`;
    const v = $(".v", body), u = $(".u", body);
    return {
      update() {
        const val = sigValue(w.key), n = numeric(val);
        const choice = n !== null && ((sigInfo[w.key] || {}).choices || {})[String(n)];   // DBC enum text
        v.textContent = choice || fmtValue(val, w.format);
        v.style.color = n === null ? "" : thresholdColor(w.thresholds, n, "");
        u.textContent = w.unit || (sigInfo[w.key] || {}).unit || "";
      },
    };
  }

  function lampView(w, body) {
    body.innerHTML = `<div class="dash-lamp"><i></i><span></span></div>`;
    const lamp = $("i", body), txt = $("span", body);
    return {
      update() {
        const val = sigValue(w.key);
        let on = null;
        if (val !== undefined && val !== null) {
          const n = numeric(val);
          if (n === null) on = !/^(0|false|off|)$/i.test(String(val).trim());
          else if (w.mode === "bit") on = ((BigInt.asUintN(64, BigInt(Math.trunc(n))) >> BigInt(clamp(+w.bit || 0, 0, 63))) & 1n) === 1n;
          else if (w.mode === "above") on = n >= (num(w.threshold) ?? 0.5);
          else on = n !== 0;
        }
        lamp.style.background = on === null ? "var(--bg3)" : on ? color(w.on_color, "#3fb950") : color(w.off_color, "#4b5563");
        txt.textContent = on === null ? "no value" : `${on ? "ON" : "OFF"}  ${fmtValue(val)}`;
      },
    };
  }

  function gaugeView(w, body) {
    const A0 = 150, SWEEP = 240;          // degrees, clockwise from 3 o'clock; centre (100,100)
    const pt = (deg, r) => [100 + r * Math.cos(deg * Math.PI / 180), 100 + r * Math.sin(deg * Math.PI / 180)];
    const arc = (f0, f1, r) => {
      const a = A0 + SWEEP * f0, b = A0 + SWEEP * f1, [x0, y0] = pt(a, r), [x1, y1] = pt(b, r);
      return `M${x0.toFixed(2)} ${y0.toFixed(2)} A${r} ${r} 0 ${b - a > 180 ? 1 : 0} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
    };
    body.innerHTML = `<svg class="dash-gauge" viewBox="0 0 200 165" preserveAspectRatio="xMidYMid meet">
      <path class="track" d="${arc(0, 1, 80)}"/><g class="bands"></g><path class="val" d=""/>
      <text class="gv" x="100" y="108">-</text><text class="gu" x="100" y="128"></text>
      <text class="gl" x="31" y="158"></text><text class="gl" x="169" y="158"></text></svg>`;
    const val = $(".val", body), gv = $(".gv", body), gu = $(".gu", body), labels = CO.$$(".gl", body), bands = $(".bands", body);
    let range = "";
    return {
      update() {
        const info = sigInfo[w.key] || {};
        const lo = num(w.min) ?? info.min ?? 0, hi0 = num(w.max) ?? info.max ?? 100, hi = hi0 > lo ? hi0 : lo + 1;
        const frac = (v) => clamp((v - lo) / (hi - lo), 0, 1);
        if (range !== `${lo}:${hi}`) {        // the threshold bands only change with the range
          range = `${lo}:${hi}`;
          labels[0].textContent = fmtValue(lo); labels[1].textContent = fmtValue(hi);
          const th = (w.thresholds || []).slice().sort((a, b) => a.value - b.value);
          bands.innerHTML = th.map((t, i) => {
            const f0 = frac(t.value), f1 = frac(i + 1 < th.length ? th[i + 1].value : hi);
            return f1 > f0 ? `<path d="${arc(f0, f1, 93)}" style="stroke:${color(t.color, "#d29922")}"/>` : "";
          }).join("");
        }
        const v = sigValue(w.key), n = numeric(v);
        gv.textContent = fmtValue(v, w.format);
        gu.textContent = w.unit || info.unit || "";
        const f = n === null ? 0 : frac(n);
        val.setAttribute("d", f > 0.002 ? arc(0, f, 80) : "");
        val.style.stroke = n === null ? "" : thresholdColor(w.thresholds, n, "var(--accent)");
      },
    };
  }

  function plotView(w, body, ctl) {
    const series = (w.series || []).filter((s) => s && s.key);
    const idx = Object.fromEntries(series.map((s, i) => [s.key, i]));
    const st = { xs: [], ys: series.map(() => []), now: Date.now() / 1000, paused: false, dead: false, hist: 0 };
    ctl.innerHTML = `<select data-c="win" title="Time window">${WINDOWS.map((s) => `<option value="${s}" ${s === winOf(w) ? "selected" : ""}>${s < 60 ? s + " s" : s / 60 + " min"}</option>`).join("")}</select>
      <button type="button" class="btn ghost" data-c="pause" title="Freeze the plot (drag across the plot to zoom; double-click to zoom out)">Pause</button>
      <button type="button" class="btn ghost" data-c="csv" title="Save the plotted values as CSV">CSV</button>`;
    if (!series.length) { body.innerHTML = `<div class="u-note">No signals: Edit layout, then the widget's settings</div>`; return {}; }

    function push(t, i, v) {               // rows of [t, v0, v1, ...]; a signal missing in a row is null
      const n = st.xs.length;
      if (n && t <= st.xs[n - 1]) {
        t = st.xs[n - 1];
        if (st.ys[i][n - 1] === null) { st.ys[i][n - 1] = v; return; }
      }
      st.xs.push(t);
      st.ys.forEach((y, j) => y.push(j === i ? v : null));
    }
    function trim() {
      const cutoff = st.now - winOf(w);
      let k = 0;
      while (k < st.xs.length && st.xs[k] < cutoff) k++;
      if (k) { st.xs.splice(0, k); st.ys.forEach((y) => y.splice(0, k)); }
    }
    const draw = () => st.u.setData([st.xs.slice(), ...st.ys.map((y) => y.slice())]);   // copies: a paused plot keeps its own
    const setPaused = (p) => { st.paused = p; $("[data-c=pause]", ctl).textContent = p ? "Resume" : "Pause"; $("[data-c=pause]", ctl).classList.toggle("on", p); if (!p) draw(); };

    st.u = new uPlot({
      width: 300, height: 150, axes: plotAxes(),
      scales: { x: { time: true, range: (u, mn, mx) => (st.paused ? [mn, mx] : [st.now - winOf(w), st.now]) } },
      series: [{}, ...series.map((s, i) => ({ label: s.key, stroke: color(s.color, PALETTE[i % PALETTE.length]), width: 1.5,
        spanGaps: true, points: { show: false } }))],
      cursor: { drag: { x: true, y: false, setScale: false } },
      hooks: { setSelect: [(u) => {                 // drag to zoom: pause and show the selected time span
        if (u.select.width > 3) {
          const mn = u.posToVal(u.select.left, "x"), mx = u.posToVal(u.select.left + u.select.width, "x");
          setPaused(true);
          u.setScale("x", { min: mn, max: mx });
        }
        u.setSelect({ left: 0, top: 0, width: 0, height: 0 }, false);
      }] },
    }, [[], ...series.map(() => [])], body);
    const ro = new ResizeObserver(() => sizePlot(st.u, body));
    ro.observe(body);

    async function loadHistory() {           // full-resolution past values, then the live rows that came since
      const token = ++st.hist;
      const res = await Promise.all(series.map((s) => CO.api(`/api/signals/history?key=${encodeURIComponent(s.key)}&seconds=${winOf(w)}&max_points=3000`)
        .catch(() => ({ points: [] }))));
      if (st.dead || token !== st.hist) return;
      const pts = [];
      res.forEach((r, i) => r.points.forEach(([t, v]) => pts.push([t, i, v])));
      pts.sort((a, b) => a[0] - b[0]);
      const tEnd = pts.length ? pts[pts.length - 1][0] : -Infinity, oldX = st.xs, oldY = st.ys;
      st.xs = []; st.ys = series.map(() => []);
      for (const [t, i, v] of pts) push(t, i, v);
      oldX.forEach((t, k) => { if (t > tEnd) oldY.forEach((y, i) => { if (y[k] !== null) push(t, i, y[k]); }); });
      st.now = Date.now() / 1000;
      trim();
      if (!st.paused) draw();
    }
    loadHistory();

    ctl.onchange = (ev) => { if (ev.target.dataset.c === "win") { w.window = +ev.target.value; saveSoon(); setPaused(false); loadHistory(); } };
    ctl.onclick = (ev) => {
      const c = ev.target.dataset.c;
      if (c === "pause") setPaused(!st.paused);
      if (c === "csv") {
        const data = st.u.data, lines = ["time,iso_time," + series.map((s) => `"${s.key.replace(/"/g, '""')}"`).join(",")];
        for (let k = 0; k < data[0].length; k++) {
          lines.push(`${data[0][k].toFixed(6)},${new Date(data[0][k] * 1000).toISOString()},` + data.slice(1).map((y) => (y[k] === null ? "" : y[k])).join(","));
        }
        const a = document.createElement("a");
        a.href = URL.createObjectURL(new Blob([lines.join("\r\n") + "\r\n"], { type: "text/csv" }));
        a.download = `${(w.title || "plot").replace(/[^\w.-]+/g, "_")}.csv`;
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 120000);   // the Save dialog is modal and may stay open
      }
    };

    return {
      update(msg) {
        st.now = msg.t || Date.now() / 1000;
        if (msg.signals) {
          const pts = [];
          for (const sg of msg.signals) {
            const i = idx[sg.key], v = numeric(sg.value);
            if (i !== undefined && v !== null) pts.push([sg.ts, i, v]);
          }
          pts.sort((a, b) => a[0] - b[0]);
          for (const [t, i, v] of pts) push(t, i, v);
        }
        trim();
        if (!st.paused) draw();
      },
      destroy() { st.dead = true; ro.disconnect(); st.u.destroy(); },
    };
  }

  function xyView(w, body, ctl) {
    const st = { pts: [], now: Date.now() / 1000, dead: false };     // [t, x, y]
    ctl.innerHTML = `<select data-c="win" title="Time window">${WINDOWS.map((s) => `<option value="${s}" ${s === winOf(w) ? "selected" : ""}>${s < 60 ? s + " s" : s / 60 + " min"}</option>`).join("")}</select>`;
    if (!w.x_key || !w.y_key) { body.innerHTML = `<div class="u-note">Pick an X and a Y signal: Edit layout, then the widget's settings</div>`; return {}; }
    const c = color(w.color, PALETTE[0]);
    st.u = new uPlot({
      width: 300, height: 150, axes: plotAxes(),
      scales: { x: { time: false } },
      series: [{ label: w.x_key }, { label: w.y_key, stroke: c, fill: c, paths: () => null, points: { show: true, size: 4, stroke: c, fill: c } }],
      cursor: { drag: { x: false, y: false } },
    }, [[], []], body);
    const ro = new ResizeObserver(() => sizePlot(st.u, body));
    ro.observe(body);
    const draw = () => {
      const s = st.pts.slice().sort((a, b) => a[1] - b[1]);
      st.u.setData([s.map((p) => p[1]), s.map((p) => p[2])]);
    };
    const trim = () => { const cutoff = st.now - winOf(w); let k = 0; while (k < st.pts.length && st.pts[k][0] < cutoff) k++; if (k) st.pts.splice(0, k); };

    async function loadHistory() {           // pair each Y value with the X value current at that time
      const q = (key) => CO.api(`/api/signals/history?key=${encodeURIComponent(key)}&seconds=${winOf(w)}&max_points=3000`).catch(() => ({ points: [] }));
      const [hx, hy] = await Promise.all([q(w.x_key), q(w.y_key)]);
      if (st.dead) return;
      const out = [];
      let j = -1;
      for (const [t, y] of hy.points) {
        while (j + 1 < hx.points.length && hx.points[j + 1][0] <= t) j++;
        if (j >= 0) out.push([t, hx.points[j][1], y]);
      }
      st.pts = out.concat(st.pts.filter((p) => !out.length || p[0] > out[out.length - 1][0]));
      st.now = Date.now() / 1000;
      trim();
      draw();
    }
    loadHistory();
    ctl.onchange = (ev) => { if (ev.target.dataset.c === "win") { w.window = +ev.target.value; saveSoon(); loadHistory(); } };

    return {
      update(msg) {
        st.now = msg.t || Date.now() / 1000;
        if (msg.signals && msg.signals.some((s) => s.key === w.x_key || s.key === w.y_key)) {
          const x = numeric(sigValue(w.x_key)), y = numeric(sigValue(w.y_key));
          if (x !== null && y !== null) st.pts.push([st.now, x, y]);
        }
        trim();
        draw();
      },
      destroy() { st.dead = true; ro.disconnect(); st.u.destroy(); },
    };
  }

  const VIEWS = { plot: plotView, xy: xyView, gauge: gaugeView, number: numberView, lamp: lampView };

  function makeView(w) {
    const el = document.createElement("div");
    el.className = "dash-w";
    el.dataset.id = w.id;
    el.innerHTML = `<div class="dash-wh"><span class="dash-wt">${esc(w.title || TYPES[w.type])}</span><span class="dash-wc"></span>
      <span class="dash-edit"><button type="button" class="btn ghost" data-act="cfg" title="Settings">&#9881;</button><button type="button" class="btn ghost" data-act="del" title="Remove">&times;</button></span></div>
      <div class="dash-wb"></div><div class="dash-rs" title="Resize"></div>`;
    place(el, w);
    $("#db-grid", tabEl).appendChild(el);
    const view = { w, el, update() {}, destroy() {} };
    try {
      Object.assign(view, VIEWS[w.type](w, $(".dash-wb", el), $(".dash-wc", el)));
      view.update({});
    } catch (e) {
      $(".dash-wb", el).innerHTML = `<div class="notice bad">${esc(e.message)}</div>`;
      console.error(e);
    }
    $(".dash-wh", el).onpointerdown = (ev) => { if (editing && !ev.target.closest("button, select, input")) drag(ev, view, "move"); };
    $(".dash-rs", el).onpointerdown = (ev) => drag(ev, view, "size");
    $(".dash-edit", el).onclick = (ev) => {
      const act = ev.target.closest("[data-act]")?.dataset.act;
      if (act === "cfg") configure(w);
      if (act === "del") { cur.widgets = cur.widgets.filter((x) => x !== w); saveSoon(); build(); }
    };
    return view;
  }

  function drag(ev, view, mode) {           // move or resize in whole grid cells, never onto another widget
    ev.preventDefault();
    const grid = $("#db-grid", tabEl), w = view.w, handle = ev.currentTarget;
    const cellW = (grid.clientWidth - GAP * (COLS - 1)) / COLS + GAP, cellH = ROW_H + GAP;
    const sx = ev.clientX, sy = ev.clientY, o = { x: w.x, y: w.y, w: w.w, h: w.h };
    const others = cur.widgets.filter((x) => x !== w);
    const free = (r) => !others.some((b) => r.x < b.x + b.w && b.x < r.x + r.w && r.y < b.y + b.h && b.y < r.y + r.h);
    handle.setPointerCapture(ev.pointerId);
    handle.onpointermove = (e) => {
      const dx = Math.round((e.clientX - sx) / cellW), dy = Math.round((e.clientY - sy) / cellH);
      const r = mode === "move" ? { x: clamp(o.x + dx, 0, COLS - o.w), y: Math.max(0, o.y + dy), w: o.w, h: o.h }
        : { x: o.x, y: o.y, w: clamp(o.w + dx, 1, COLS - o.x), h: clamp(o.h + dy, 1, 40) };
      if (!free(r)) return;
      Object.assign(w, r);
      place(view.el, w);
    };
    handle.onpointerup = handle.onpointercancel = () => {
      handle.onpointermove = handle.onpointerup = handle.onpointercancel = null;
      if (w.x !== o.x || w.y !== o.y || w.w !== o.w || w.h !== o.h) saveSoon();
    };
  }

  // ---------------------------------------------------------------- dashboard view
  function destroyViews() {
    for (const v of views) { try { v.destroy(); } catch (e) { console.error(e); } }
    views = [];
    if (tabEl) $("#db-grid", tabEl).innerHTML = "";
  }

  function build() {
    destroyViews();
    const grid = $("#db-grid", tabEl);
    grid.classList.toggle("editing", editing && !!cur);
    $("#db-edit", tabEl).classList.toggle("on", editing);
    $("#db-edit", tabEl).textContent = editing ? "Done" : "Edit layout";
    $("#db-add", tabEl).hidden = !editing || !cur;
    for (const id of ["db-rename", "db-delete", "db-edit"]) $("#" + id, tabEl).disabled = !cur;
    if (!cur) { grid.innerHTML = `<div class="dash-empty empty">No dashboard yet. <button class="btn primary" id="db-first">New dashboard</button></div>`; $("#db-first", grid).onclick = newDashboard; return; }
    if (!cur.widgets.length) grid.innerHTML = `<div class="dash-empty empty">${editing ? "Add a widget with + Widget." : "This dashboard is empty: click Edit layout, then + Widget."}</div>`;
    views = cur.widgets.map(makeView);
    loadSignals();                          // units and ranges for gauges/tiles; they show on the next tick
  }

  function renderPicker() {
    $("#db-pick", tabEl).innerHTML = dashes.length
      ? dashes.map((d) => `<option value="${esc(d.id)}" ${cur && d.id === cur.id ? "selected" : ""}>${esc(d.name)}</option>`).join("")
      : `<option value="">no dashboards</option>`;
  }

  function pickCurrent(id) {
    cur = dashes.find((d) => d.id === (id || (cur && cur.id) || CO.prefs.get("dash.current", ""))) || dashes[0] || null;
    if (cur) CO.prefs.set("dash.current", cur.id);
  }

  async function reload() {
    const d = await CO.tryApi("/api/dashboards");
    if (!d || !active) return;              // the tab may have been left meanwhile
    dashes = d.dashboards;
    pickCurrent();
    renderPicker();
    build();
  }

  const unsaved = new Set();
  function saveSoon() {                     // layout edits come in bursts: save each changed dashboard once
    if (cur) unsaved.add(cur);
    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
      saveTimer = null;
      const list = [...unsaved];
      unsaved.clear();
      for (const d of list) await CO.tryApi("/api/dashboards", d);
    }, 400);
  }

  async function askName(title, value) {
    const r = await CO.modal({ title, body: `<label class="field"><span>Name</span><input id="db-name" value="${esc(value || "")}" placeholder="e.g. Engine test rig"></label>`,
      buttons: [{ label: "Cancel", value: "cancel" }, { label: "OK", value: "ok", cls: "primary" }],
      onOpen: (f) => $("#db-name", f).focus() });
    return r ? $("#db-name", r.form).value.trim() : null;
  }

  async function newDashboard() {
    const name = await askName("New dashboard", "");
    if (!name) return;
    const d = await CO.tryApi("/api/dashboards", { name, widgets: [] });
    if (!d) return;
    dashes.push(d);
    pickCurrent(d.id);
    editing = true;
    renderPicker();
    build();
  }

  async function addWidget() {
    const r = await CO.modal({ title: "Add widget", body: `<p class="small muted">Pick the widget type; you choose its signals next.</p>`,
      buttons: [{ label: "Cancel", value: "cancel" }, ...Object.entries(TYPES).map(([t, label]) => ({ label, value: t, cls: "primary" }))] });
    if (!r || !TYPES[r.value]) return;
    const [ww, hh] = SIZES[r.value];
    const bottom = Math.max(0, ...cur.widgets.map((x) => x.y + x.h));
    const w = { id: newId(), type: r.value, title: "", x: 0, y: bottom, w: ww, h: hh,
      ...(r.value === "plot" ? { series: [], window: 60 } : r.value === "xy" ? { window: 60, color: PALETTE[0] } : {}) };
    await configure(w, true);
  }

  // ---------------------------------------------------------------- widget settings
  function pickerHtml() {
    return `<div class="dash-picker"><input type="search" placeholder="Search signals" class="dp-q"><div class="dash-plist"></div></div>`;
  }
  function wirePicker(root, onPick) {
    const q = $(".dp-q", root), list = $(".dash-plist", root);
    const show = () => {
      const s = q.value.trim().toLowerCase(), hits = sigList.filter((x) => x.key.toLowerCase().includes(s)).slice(0, 300);
      list.innerHTML = hits.map((x) => `<div class="dash-pitem" data-key="${esc(x.key)}"><span class="m">${esc(x.key)}</span><span class="muted small">${esc(fmtValue(x.value))} ${esc(x.unit)}</span></div>`).join("")
        || `<div class="muted small">${sigList.length ? "No signal matches." : "No signals yet: decoders and quick signals publish them while frames arrive. You can also type a key."}</div>`;
    };
    q.oninput = show;
    list.onclick = (ev) => { const it = ev.target.closest("[data-key]"); if (it) onPick(it.dataset.key); };
    show();
  }
  const thrRow = (t) => `<div class="row dash-thr"><input type="number" step="any" data-f="tv" value="${esc(t.value ?? "")}" style="width:110px" placeholder="from value">
    <input type="color" class="dash-color" data-f="tc" value="${color(t.color, "#d29922")}"><button type="button" class="btn ghost small" data-act="thr-del">&times;</button></div>`;
  const thrHtml = (list) => `<div class="field"><span>Thresholds (colour from this value up)</span><div class="dash-list" id="we-thr">${(list || []).map(thrRow).join("")}</div>
    <div><button type="button" class="btn small" data-act="thr-add">+ Threshold</button></div></div>`;
  const serRow = (s, i) => `<div class="row dash-ser" data-key="${esc(s.key)}"><span class="m" title="${esc(s.key)}">${esc(s.key)}</span>
    <input type="color" class="dash-color" data-f="sc" value="${color(s.color, PALETTE[i % PALETTE.length])}"><button type="button" class="btn ghost small" data-act="ser-del">&times;</button></div>`;
  const fmtSelect = (v) => `<label class="field"><span>Format</span><select id="we-fmt">${FORMATS.map(([k, l]) => `<option value="${k}" ${k === (v || "auto") ? "selected" : ""}>${l}</option>`).join("")}</select></label>`;
  const winSelect = (w) => `<label class="field"><span>Time window</span><select id="we-win">${WINDOWS.map((s) => `<option value="${s}" ${s === winOf(w) ? "selected" : ""}>${s < 60 ? s + " s" : s / 60 + " min"}</option>`).join("")}</select></label>`;
  const keyField = (id, label, v) => `<label class="field"><span>${label}</span><input id="${id}" class="m we-key" value="${esc(v || "")}" placeholder="pick below or type a key"></label>`;

  async function configure(w, isNew) {
    await loadSignals();
    const t = w.type;
    let body = `<label class="field"><span>Title</span><input id="we-title" value="${esc(w.title || "")}" placeholder="${esc(TYPES[t])}"></label>`;
    if (t === "plot") body += `<div class="field"><span>Signals</span><div class="dash-list" id="we-series">${(w.series || []).map(serRow).join("")}</div></div>
      <div class="small muted">Click a signal to add it:</div>${pickerHtml()}${winSelect(w)}`;
    if (t === "xy") body += `<div class="grid2">${keyField("we-x", "X signal", w.x_key)}${keyField("we-y", "Y signal", w.y_key)}</div>
      <div class="small muted">Click a signal to put it into the X or Y field you clicked last:</div>${pickerHtml()}
      <div class="grid2">${winSelect(w)}<label class="field"><span>Colour</span><input type="color" class="dash-color" id="we-color" value="${color(w.color, PALETTE[0])}"></label></div>`;
    if (t === "gauge" || t === "number" || t === "lamp") body += `${keyField("we-key", "Signal", w.key)}${pickerHtml()}`;
    if (t === "gauge") body += `<div class="grid3"><label class="field"><span>Min (empty: signal's)</span><input id="we-min" type="number" step="any" value="${esc(w.min ?? "")}"></label>
      <label class="field"><span>Max (empty: signal's)</span><input id="we-max" type="number" step="any" value="${esc(w.max ?? "")}"></label>
      <label class="field"><span>Unit (empty: signal's)</span><input id="we-unit" value="${esc(w.unit || "")}"></label></div>${fmtSelect(w.format)}${thrHtml(w.thresholds)}`;
    if (t === "number") body += `<div class="grid2"><label class="field"><span>Unit (empty: signal's)</span><input id="we-unit" value="${esc(w.unit || "")}"></label>${fmtSelect(w.format)}</div>${thrHtml(w.thresholds)}`;
    if (t === "lamp") body += `<div class="grid3"><label class="field"><span>On when</span><select id="we-mode">
        <option value="bool">value is not 0</option><option value="bit" ${w.mode === "bit" ? "selected" : ""}>a bit is set</option><option value="above" ${w.mode === "above" ? "selected" : ""}>value &ge; threshold</option></select></label>
      <label class="field"><span>Bit (0 = LSB)</span><input id="we-bit" type="number" min="0" max="63" value="${esc(w.bit ?? 0)}"></label>
      <label class="field"><span>Threshold</span><input id="we-th" type="number" step="any" value="${esc(w.threshold ?? 0.5)}"></label></div>
      <div class="row"><label>On colour <input type="color" class="dash-color" id="we-on" value="${color(w.on_color, "#3fb950")}"></label>
      <label>Off colour <input type="color" class="dash-color" id="we-off" value="${color(w.off_color, "#4b5563")}"></label></div>`;

    const r = await CO.modal({
      title: `${isNew ? "Add" : "Edit"} ${TYPES[t]}`, body, wide: false,
      buttons: [{ label: "Cancel", value: "cancel" }, { label: isNew ? "Add" : "Save", value: "ok", cls: "primary" }],
      onOpen(f) {
        let target = $(".we-key", f);
        CO.$$(".we-key", f).forEach((inp) => (inp.onfocus = () => (target = inp)));
        const picker = $(".dash-picker", f);
        if (picker) wirePicker(picker, (key) => {
          if (t === "plot") {
            const list = $("#we-series", f);
            if (!CO.$$(".dash-ser", list).some((row) => row.dataset.key === key)) list.insertAdjacentHTML("beforeend", serRow({ key }, list.children.length));
          } else if (target) target.value = key;
        });
        $(".dlg-body", f).addEventListener("click", (ev) => {     // the body is new for every dialog
          const act = ev.target.dataset.act;
          if (act === "thr-add") $("#we-thr", f).insertAdjacentHTML("beforeend", thrRow({}));
          if (act === "thr-del" || act === "ser-del") ev.target.closest(".row").remove();
        });
      },
    });
    if (!r) return false;
    const f = r.form, val = (id) => ($("#" + id, f) ? $("#" + id, f).value : undefined);
    w.title = val("we-title").trim();
    if (t === "plot") {
      w.series = CO.$$(".dash-ser", f).map((row) => ({ key: row.dataset.key, color: $("[data-f=sc]", row).value }));
      w.window = +val("we-win");
    }
    if (t === "xy") { w.x_key = val("we-x").trim(); w.y_key = val("we-y").trim(); w.window = +val("we-win"); w.color = val("we-color"); }
    if (val("we-key") !== undefined) w.key = val("we-key").trim();
    if (t === "gauge") { w.min = num(val("we-min")); w.max = num(val("we-max")); }
    if (t === "gauge" || t === "number") {
      w.unit = val("we-unit").trim();
      w.format = val("we-fmt");
      w.thresholds = CO.$$(".dash-thr", f).map((row) => ({ value: num($("[data-f=tv]", row).value), color: $("[data-f=tc]", row).value }))
        .filter((x) => x.value !== null);
    }
    if (t === "lamp") { w.mode = val("we-mode"); w.bit = clamp(Math.trunc(num(val("we-bit")) ?? 0), 0, 63); w.threshold = num(val("we-th")); w.on_color = val("we-on"); w.off_color = val("we-off"); }
    if (isNew) cur.widgets.push(w);
    saveSoon();
    build();
    return true;
  }

  // ---------------------------------------------------------------- quick signals editor
  const qRow = (d) => `<tr>
    <td><input data-f="name" class="m" value="${esc(d.name || "")}" style="width:110px" placeholder="EngineSpeed"></td>
    <td><select data-f="channel"><option value="">any</option>${[0, 1, 2, 3, 4, 5, 6, 7].map((i) => `<option value="${i}" ${d.channel === i ? "selected" : ""}>${esc(CO.chName(i))}</option>`).join("")}</select></td>
    <td><input data-f="can_id" class="m" value="${esc(d.can_id ?? "")}" style="width:84px" placeholder="hex"></td>
    <td><input type="checkbox" data-f="extended" ${d.extended ? "checked" : ""} title="29-bit id"></td>
    <td><input type="number" data-f="start_bit" min="0" max="511" value="${esc(d.start_bit ?? 0)}" style="width:56px"></td>
    <td><input type="number" data-f="length" min="1" max="64" value="${esc(d.length ?? 8)}" style="width:50px"></td>
    <td><select data-f="byte_order"><option value="little">Intel</option><option value="big" ${d.byte_order === "big" ? "selected" : ""}>Motorola</option></select></td>
    <td><input type="checkbox" data-f="signed" ${d.signed ? "checked" : ""}></td>
    <td><input type="number" step="any" data-f="factor" value="${esc(d.factor ?? 1)}" style="width:70px"></td>
    <td><input type="number" step="any" data-f="offset" value="${esc(d.offset ?? 0)}" style="width:62px"></td>
    <td><input data-f="unit" value="${esc(d.unit || "")}" style="width:52px"></td>
    <td><input type="number" step="any" data-f="min" value="${esc(d.min ?? "")}" style="width:62px"></td>
    <td><input type="number" step="any" data-f="max" value="${esc(d.max ?? "")}" style="width:62px"></td>
    <td class="pv muted">-</td>
    <td><button type="button" class="btn ghost small" data-act="q-del" title="Remove">&times;</button></td></tr>`;

  function readQuick(f) {
    return CO.$$("#q-rows tr", f).map((tr) => {
      const v = (k) => $(`[data-f=${k}]`, tr);
      return { name: v("name").value.trim(), channel: v("channel").value === "" ? null : +v("channel").value,
        can_id: v("can_id").value.trim(), extended: v("extended").checked, start_bit: num(v("start_bit").value) ?? 0,
        length: num(v("length").value) ?? 8, byte_order: v("byte_order").value, signed: v("signed").checked,
        factor: num(v("factor").value) ?? 1, offset: num(v("offset").value) ?? 0, unit: v("unit").value.trim(),
        min: num(v("min").value), max: num(v("max").value) };
    });
  }

  async function quickSignals(rows) {
    if (!rows) {
      const d = await CO.tryApi("/api/raw_signals");
      if (!d) return;
      rows = d.signals.map((s) => ({ ...s, can_id: CO.idText(s.can_id, s.extended ? CO.F.EXT : 0) }));
    }
    let timer = null, busy = false;
    const r = await CO.modal({
      title: "Quick signals", wide: true,
      body: `<p class="small muted">Values cut out of raw frames, without a DBC file. Each appears as signal <b>raw/&lt;name&gt;</b> for dashboards and Claude.
        Bit numbering as in DBC files: Intel (little endian) start bit = least significant bit; Motorola (big endian) start bit = most significant bit
        (bit 7 of byte 0 is the first bit on the wire). Value = raw &times; factor + offset.</p>
        <div class="scroll" style="max-height:52vh"><table class="tbl compact dash-q"><thead><tr><th>Name</th><th>Channel</th><th>ID</th><th>Ext</th><th>Start bit</th><th>Length</th>
        <th>Byte order</th><th>Signed</th><th>Factor</th><th>Offset</th><th>Unit</th><th>Min</th><th>Max</th><th>Now</th><th></th></tr></thead>
        <tbody id="q-rows">${rows.map(qRow).join("")}</tbody></table></div>
        <div><button type="button" class="btn small" data-act="q-add">+ Signal</button></div>`,
      buttons: [{ label: "Cancel", value: "cancel" }, { label: "Save", value: "save", cls: "primary" }],
      onOpen(f) {
        const dlgBody = $(".dlg-body", f);                // new for every dialog: the listeners go with it
        dlgBody.addEventListener("click", (ev) => {
          if (ev.target.dataset.act === "q-add") $("#q-rows", f).insertAdjacentHTML("beforeend", qRow({}));
          if (ev.target.dataset.act === "q-del") ev.target.closest("tr").remove();
        });
        dlgBody.addEventListener("input", (ev) => {           // an id above 7FF can only be a 29-bit id
          if (ev.target.dataset.f === "can_id" && CO.parseHexId(ev.target.value) > 0x7FF) $("[data-f=extended]", ev.target.closest("tr")).checked = true;
        });
        const preview = async () => {
          if (busy || !f.isConnected || !$("#q-rows", f)) return;
          busy = true;
          try {
            const defs = readQuick(f).map((d) => ({ ...d, name: "preview" }));
            const res = defs.length ? (await CO.api("/api/raw_signals/preview", { signals: defs })).results : [];
            CO.$$("#q-rows tr", f).forEach((tr, i) => {
              const p = res[i], cell = $(".pv", tr), unit = $("[data-f=unit]", tr).value;
              if (!p || !cell) return;
              cell.textContent = p.value === null ? "-" : `${fmtValue(p.value)} ${unit}`;
              cell.title = p.note || `${p.data} (${CO.chName(p.channel)})`;
            });
          } catch (e) { /* the editor may have closed */ }
          busy = false;
        };
        preview();
        timer = setInterval(preview, 500);
      },
    });
    clearInterval(timer);
    if (!r) return;
    const defs = readQuick(r.form);
    try {
      await CO.api("/api/raw_signals", { signals: defs });
      CO.toast(`Saved ${defs.length} quick signal${defs.length === 1 ? "" : "s"}`, "good");
    } catch (e) {
      CO.toast(e.message, "bad");
      quickSignals(defs);                   // reopen with the user's rows
    }
  }

  // ---------------------------------------------------------------- tab
  CO.registerTab({
    id: "dash", title: "Dashboards", order: 45,
    render(el) {
      tabEl = el;
      el.innerHTML = `<div class="toolbar">
          <select id="db-pick" title="Dashboard" style="min-width:160px"></select>
          <button class="btn small" id="db-new">New</button><button class="btn small" id="db-rename">Rename</button>
          <button class="btn small" id="db-delete">Delete</button><span class="sep"></span>
          <button class="btn small" id="db-edit" title="Add, move, resize and remove widgets">Edit layout</button>
          <button class="btn small primary" id="db-add" hidden>+ Widget</button>
          <div class="spacer"></div>
          <button class="btn small" id="db-quick" title="Define signals on raw frames, without a DBC file">Quick signals</button>
        </div>
        <div class="page"><div id="db-grid" class="dash-grid"></div></div>`;
      $("#db-pick", el).onchange = (ev) => { pickCurrent(ev.target.value); build(); };
      $("#db-new", el).onclick = newDashboard;
      $("#db-rename", el).onclick = async () => {
        const name = cur && await askName("Rename dashboard", cur.name);
        if (!name) return;
        if (await CO.tryApi("/api/dashboards", { ...cur, name })) { cur.name = name; renderPicker(); }
      };
      $("#db-delete", el).onclick = async () => {
        if (!cur || !await CO.confirm("Delete dashboard", `Delete <b>${esc(cur.name)}</b> and its widgets?`, "Delete", true)) return;
        if (await CO.tryApi(`/api/dashboards/${encodeURIComponent(cur.id)}`, undefined, null, "DELETE")) {
          dashes = dashes.filter((d) => d !== cur);
          cur = null;
          pickCurrent();
          renderPicker();
          build();
        }
      };
      $("#db-edit", el).onclick = () => { editing = !editing; build(); };
      $("#db-add", el).onclick = addWidget;
      $("#db-quick", el).onclick = () => quickSignals();
      // plots read the theme colours when they are created: rebuild on a theme switch
      new MutationObserver(() => { if (active) build(); }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    },
    onShow() { active = true; if (saveTimer) build(); else reload(); },
    onHide() { active = false; destroyViews(); },
  });

  CO.onFeed((msg) => {
    if (!active || msg.type) return;
    for (const v of views) { try { v.update(msg); } catch (e) { console.error(e); } }
  });

  // a project was opened: show the project's dashboards
  CO.onProject(async () => {
    clearTimeout(saveTimer); saveTimer = null; unsaved.clear();   // never write the old project's layout over it
    if (active) {
      const d = await CO.api("/api/dashboards").catch(() => null);
      if (d && canon(d.dashboards) !== canon(dashes)) reload();
    }
  });
})();
