/* Boot: settings, backends, plugins, tabs, live feed. */
"use strict";
(async function () {
  const CO = window.canopener;
  try {
    const [about, b, settings] = await Promise.all([CO.api("/api/about"), CO.api("/api/backends"), CO.api("/api/settings")]);
    CO.state.about = about;
    CO.state.backends = b.backends; CO.state.bitrates = b.bitrates; CO.state.dataBitrates = b.data_bitrates;
    CO.state.settings = settings;
    document.title = `CANopener ${about.version}`;
  } catch (e) {
    CO.toast("Cannot reach the CANopener server: " + e.message, "bad", 20000);
  }
  await CO.loadPlugins();
  CO._started = true;
  CO.renderTabs();
  CO.show(CO.prefs.get("tab", "receive"));
  CO.connectFeed();
})();
