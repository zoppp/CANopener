/* Statistics tab: counters per channel, bus load / frame rate history, busiest IDs. */
"use strict";
(function () {
  const CO = window.canopener, F = CO.F;
  const hist = {};                 // channel index -> [[t, load, fps], ...]
  let tabEl = null, lastSample = 0;

  function sample() {
    const now = Date.now();
    if (now - lastSample < 1000) return;
    lastSample = now;
    for (const c of CO.state.channels) {
      if (c.state === "file") continue;
      const h = hist[c.index] || (hist[c.index] = []);
      h.push([now, c.load || 0, c.fps || 0]);
      if (h.length > 600) h.shift();
    }
  }

  function drawChart(canvas, h) {
    const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth, ht = canvas.clientHeight;
    if (!w) return;
    canvas.width = w * dpr; canvas.height = ht * dpr;
    const g = canvas.getContext("2d");
    g.scale(dpr, dpr);
    const css = getComputedStyle(document.documentElement);
    const line = css.getPropertyValue("--line"), fg3 = css.getPropertyValue("--fg3");
    g.clearRect(0, 0, w, ht);
    g.strokeStyle = line; g.lineWidth = 1; g.font = "11px Segoe UI, sans-serif"; g.fillStyle = fg3;
    for (const p of [0, 25, 50, 75, 100]) { const y = ht - 4 - (ht - 18) * p / 100; g.beginPath(); g.moveTo(32, y); g.lineTo(w - 4, y); g.stroke(); g.fillText(p + "%", 2, y + 4); }
    if (h.length < 2) return;
    const t1 = h[h.length - 1][0], t0 = t1 - 600e3, x = (t) => 32 + (w - 36) * (t - t0) / 600e3;
    const maxFps = Math.max(10, ...h.map((p) => p[2]));
    const plot = (idx, color, scale) => { g.strokeStyle = color; g.lineWidth = 1.6; g.beginPath();
      h.forEach((p, i) => { const y = ht - 4 - (ht - 18) * Math.min(1, p[idx] / scale); i ? g.lineTo(x(p[0]), y) : g.moveTo(x(p[0]), y); }); g.stroke(); };
    plot(2, css.getPropertyValue("--fg3"), maxFps);
    plot(1, css.getPropertyValue("--accent"), 100);
    g.fillStyle = css.getPropertyValue("--accent"); g.fillText("bus load", w - 140, 12);
    g.fillStyle = fg3; g.fillText(`frames/s (max ${Math.round(maxFps)})`, w - 90, 12);
  }

  function render() {
    if (!tabEl || tabEl.hidden) return;
    const chans = CO.state.channels;
    const cards = CO.$("#st-cards", tabEl);
    if (!chans.length) { cards.innerHTML = `<div class="empty">Connect an adapter to see bus statistics.</div>`; CO.$("#st-top", tabEl).innerHTML = ""; return; }
    const sig = chans.map((c) => c.index).join(",");
    if (cards._sig !== sig) {
      cards._sig = sig;
      cards.innerHTML = chans.map((c) => `<div class="card" data-ch="${c.index}"><h3>${CO.esc(c.name)} <span class="muted small" data-f="desc"></span></h3>
        <div class="kv" data-f="kv"></div>${c.state === "file" ? "" : `<canvas class="chart" style="margin-top:10px"></canvas>`}</div>`).join("");
    }
    for (const c of chans) {
      const card = CO.$(`[data-ch="${c.index}"]`, cards); if (!card) continue;
      const cfg = c.config || {};
      CO.$("[data-f=desc]", card).textContent = c.state === "file" ? "file view" : `${c.backend} ${cfg.channel ?? ""} - ${cfg.bitrate / 1000} kbit/s${cfg.fd ? ` / FD ${cfg.data_bitrate / 1e6} M` : ""}${cfg.listen_only ? " - listen-only" : ""}`;
      const up = c.t_open ? Math.round(Date.now() / 1000 - c.t_open) : 0;
      CO.$("[data-f=kv]", card).innerHTML = `
        <span>State</span><span>${CO.esc(c.state)}${c.bus_state ? " / " + CO.esc(c.bus_state) : ""}${c.error ? ` <span class="badge bad">${CO.esc(c.error)}</span>` : ""}</span>
        <span>Bus load</span><span>${(c.load || 0).toFixed(1)} % (peak ${(c.peak_load || 0).toFixed(1)} %)${c.hw_load !== undefined ? `, adapter reports ${c.hw_load} %` : ""}</span>
        <span>Frames/s</span><span>${CO.fmtNum(Math.round(c.fps))} (peak ${CO.fmtNum(Math.round(c.peak_fps))})</span>
        <span>Received</span><span>${CO.fmtNum(c.rx)}</span><span>Transmitted</span><span>${CO.fmtNum(c.tx)}</span>
        <span>Error frames</span><span>${CO.fmtNum(c.err_frames)}${c.eps ? ` (${c.eps}/s)` : ""}</span>
        <span>Receive / send errors</span><span>${c.rx_errors} / ${c.tx_errors}${c.hw_overruns ? `, ${c.hw_overruns} adapter overruns` : ""}</span>
        <span>Distinct IDs</span><span>${c.ids}</span>
        <span>Connected</span><span>${c.state === "file" ? "-" : `${Math.floor(up / 60)} min ${up % 60} s`}</span>
        ${c.taps_dropped ? `<span>Plugin frames dropped</span><span>${c.taps_dropped}</span>` : ""}`;
      const cv = CO.$("canvas", card); if (cv) drawChart(cv, hist[c.index] || []);
    }
    // busiest ids from the receive list
    // average rate over the whole time an id was seen; ids seen only a few times (SDO, NMT) are not "busy"
    const rows = [...(CO.receiveRows || new Map()).values()].map((v) => v.r).filter((r) => r[12] > 0 && r[7] >= 5 && !(r[3] & F.ERR));
    rows.sort((a, b) => a[12] - b[12]);
    CO.$("#st-top", tabEl).innerHTML = rows.slice(0, 20).map((r) => `<tr><td>${CO.esc(CO.chName(r[1]))}</td><td class="m">${CO.idText(r[2], r[3])}${r[3] & F.TX ? " Tx" : ""}</td>
      <td class="r m">${(1000 / r[12]).toFixed(1)}</td><td class="r m">${CO.fmtMs(r[12])}</td><td class="r m">${CO.fmtMs(r[9])} - ${CO.fmtMs(r[10])}</td><td class="r m">${CO.fmtNum(r[7])}</td></tr>`).join("")
      || `<tr><td colspan="6" class="empty">No periodic traffic yet.</td></tr>`;
  }

  CO.registerTab({
    id: "stats", title: "Statistics", order: 40,
    render(el) {
      tabEl = el;
      el.innerHTML = `<div class="page"><div class="cards" id="st-cards"></div>
        <div class="card" style="margin-top:12px"><h3>Busiest IDs</h3>
        <table class="tbl compact"><thead><tr><th>Ch</th><th>ID</th><th class="r">Frames/s</th><th class="r">Avg cycle ms</th><th class="r">Min - max ms</th><th class="r">Count</th></tr></thead>
        <tbody id="st-top"></tbody></table></div></div>`;
    },
    onShow: render,
  });

  let n = 0;
  CO.onFeed((msg) => { if (msg.type) return; sample(); if (++n % 5 === 0) render(); });
})();
