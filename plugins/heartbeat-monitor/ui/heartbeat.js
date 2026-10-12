/* Heartbeat monitor tab (example plugin UI). */
"use strict";
(function () {
  const CO = window.canopener;
  let el = null, timer = null;

  async function refresh() {
    if (!el || el.hidden) return;
    const d = await CO.api("/api/x/heartbeat-monitor/nodes").catch(() => null);
    if (!d) return;
    CO.$("#hb-body", el).innerHTML = d.nodes.map((n) => `<tr class="${n.lost ? "err" : ""}"><td>${CO.esc(CO.chName(n.channel))}</td>
      <td class="r m"><b>${n.node}</b></td><td>${CO.esc(n.state)}${n.lost ? ` <span class="badge bad">lost</span>` : ""}</td>
      <td class="r m">${n.period_ms || ""}</td><td class="r m">${n.age_ms}</td><td class="r m">${n.count}</td><td class="r m">${n.boots}</td></tr>`).join("")
      || `<tr><td colspan="7" class="empty">No heartbeats yet (0x701-0x77F).</td></tr>`;
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
    },
    onShow() { refresh(); clearInterval(timer); timer = setInterval(refresh, 500); },
    onHide() { clearInterval(timer); },
  });
})();
