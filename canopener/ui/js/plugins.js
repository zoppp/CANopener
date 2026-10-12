/* Plugins tab: what loaded, from where, and what failed. Also loads every plugin's UI scripts. */
"use strict";
(function () {
  const CO = window.canopener;
  let info = null;

  CO.loadPlugins = async () => {
    info = await CO.api("/api/plugins").catch(() => ({ extensions: [], errors: [], folders: [] }));
    for (const ext of info.extensions) {
      for (const src of ext.ui_scripts) {
        try { await CO.loadScript(src); } catch (e) { info.errors.push({ plugin: ext.id, error: e.message }); }
      }
    }
  };

  CO.registerTab({
    id: "plugins", title: "Plugins", order: 900,
    render(el) {
      el.innerHTML = `<div class="page"><div class="card"><h3>Plugins</h3>
        <p class="small muted">A plugin is a folder with a manifest.json and a Python module: it can add tabs, REST routes, MCP tools for Claude and
        live frame consumers. Plugins run with full access - only install what you reviewed. Restart CANopener after adding one.
        See docs/PLUGINS.md.</p><div id="pl-list"></div></div></div>`;
    },
    onShow(el) {
      const list = CO.$("#pl-list", el);
      if (!info) { list.textContent = "Loading..."; return; }
      list.innerHTML = `<table class="tbl"><thead><tr><th>Plugin</th><th>Version</th><th>Description</th><th>Folder</th></tr></thead><tbody>
        ${info.extensions.map((e) => `<tr><td><b>${CO.esc(e.title)}</b> <span class="muted small">${CO.esc(e.id)}</span></td><td>${CO.esc(e.version)}</td><td>${CO.esc(e.description)}</td><td class="small mono">${CO.esc(e.folder)}</td></tr>`).join("")
          || `<tr><td colspan="4" class="empty">No plugins loaded.</td></tr>`}</tbody></table>
        ${info.errors.map((x) => `<div class="notice bad"><b>${CO.esc(x.plugin)}</b>: ${CO.esc(x.error)}${x.trace ? `<div class="small mono">${CO.esc(x.trace)}</div>` : ""}</div>`).join("")}
        <p class="small muted">Plugin folders searched (first wins): ${info.folders.map((f) => `<span class="mono">${CO.esc(f)}</span>`).join(", ")}</p>`;
    },
  });
})();
