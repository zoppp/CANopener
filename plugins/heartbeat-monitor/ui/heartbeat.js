/* Heartbeat monitor tab (example plugin UI). */
"use strict";
(function () {
  const CO = window.canopener;
  let el = null, timer = null, nodes = [], sel = null;
  const key = (n) => `${n.channel}:${n.node}`;

  async function refresh() {
    if (!el || el.hidden || sel.busy) return;        // busy: a mouse drag is selecting rows
    const d = await CO.api("/api/x/heartbeat-monitor/nodes").catch(() => null);
    if (!d) return;
    nodes = d.nodes;
    CO.$("#hb-body", el).innerHTML = nodes.map((n) => `<tr data-k="${CO.esc(key(n))}" class="${n.lost ? "err" : ""} ${sel.has(key(n)) ? "sel" : ""}"><td>${CO.esc(CO.chName(n.channel))}</td>
      <td class="r m"><b>${n.node}</b></td><td>${CO.esc(n.state)}${n.lost ? ` <span class="badge bad">lost</span>` : ""}</td>
      <td class="r m">${n.period_ms || ""}</td><td class="r m">${n.age_ms}</td><td class="r m">${n.count}</td><td class="r m">${n.boots}</td></tr>`).join("")
      || `<tr><td colspan="7" class="empty">No heartbeats yet (0x701-0x77F).</td></tr>`;
  }

  function menu(keys) {
    const sn = nodes.filter((n) => keys.includes(key(n)));
    if (!sn.length) return null;
    const ids = [...new Set(sn.map((n) => CO.idText(0x700 + n.node, 0)))];
    return [
      { label: sn.length > 1 ? `Copy ${sn.length} rows` : "Copy row", shortcut: "Ctrl+C", action: () => sel.copy() },
      { label: sn.length > 1 ? "Copy node IDs" : "Copy node ID", action: () => CO.copyText(sn.map((n) => n.node).join("\n"), "node ID") },
      { separator: true },
      { label: sn.length > 1 ? "Show only these heartbeats in the trace" : "Show only this heartbeat in the trace", action: () => { CO.traceFilter(ids, false); CO.show("trace"); } },
    ];
  }

  CO.registerTab({
    id: "heartbeat", title: "Heartbeats", order: 60,
    render(e) {
      el = e;
      el.innerHTML = `<div class="page"><div class="card"><div class="row"><h3 style="margin:0">CANopen heartbeats</h3>
        <span class="muted small">from the heartbeat-monitor plugin</span><span class="spacer"></span><button class="btn small" id="hb-clear">Clear</button></div>
        <table class="tbl" style="margin-top:8px"><thead><tr><th>Ch</th><th class="r">Node</th><th>NMT state</th><th class="r">Period ms</th>
        <th class="r">Last seen ms ago</th><th class="r">Count</th><th class="r">Boot-ups</th></tr></thead><tbody id="hb-body"></tbody></table></div></div>`;
      CO.$("#hb-clear", el).onclick = () => CO.api("/api/x/heartbeat-monitor/clear", {}).then(refresh);
      sel = CO.rowSelect({ el: CO.$("#hb-body", el).closest("table"), keys: () => nodes.map(key), menu });
    },
    onShow() { refresh(); clearInterval(timer); timer = setInterval(refresh, 500); },
    onHide() { clearInterval(timer); },
  });
})();
