/* EDS editor tab (eds-editor plugin): object tree, form, problems; device compare / create; DCF -> Nodes steps.
   The document is the JSON of plugins/eds-editor/edsdoc.py; the server checks and writes it. */
"use strict";
(function () {
  const CO = window.canopener;
  const API = "/api/x/eds-editor";
  const VAR_FIELDS = ["data_type", "access", "default", "low", "high", "pdo", "value", "denotation", "extra", "guess"];
  let el = null, meta = { templates: [], data_types: [], access: [], folder: "" };
  let doc = null, problems = [], sel = { info: true }, dirty = false, savedAs = "", filter = "";
  let checkTimer = null, draftTimer = null;
  const expanded = new Set();

  const hex4 = (i) => i.toString(16).toUpperCase().padStart(4, "0");
  const hex2 = (i) => i.toString(16).toUpperCase().padStart(2, "0");
  const where = (i, s) => (i === null || i === undefined ? "" : "0x" + hex4(i) + (s === null || s === undefined ? "" : ":" + hex2(s)));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const dtName = (c) => (meta.data_types.find((d) => d.code === c) || { name: "0x" + hex4(c || 0) }).name;
  const dtBits = (c) => (meta.data_types.find((d) => d.code === c) || { bits: 0 }).bits;
  const isMapping = (i) => (i >= 0x1600 && i <= 0x17ff) || (i >= 0x1a00 && i <= 0x1bff);
  const dcf = () => doc && doc.format === "dcf";
  const edsInt = (t) => {                      // enough of edsdoc.parse_int for the mapping view ($NODEID counts as 0)
    const s = String(t ?? "").replace(/\s+/g, "").replace(/\+?\$NODEID\+?/i, "");
    if (!s) return 0;
    const v = /^0x/i.test(s) ? parseInt(s, 16) : /^0[0-7]+$/.test(s) ? parseInt(s, 8) : parseInt(s, 10);
    return Number.isFinite(v) ? v : 0;
  };
  const refValue = (v) => (dcf() && String(v.value ?? "").trim() ? v.value : v.default);
  const findObj = (i) => doc.objects.find((o) => o.index === i);
  const findVar = (i, s) => { const o = findObj(i); if (!o) return null; if (!o.subs) return s ? null : o; return o.subs.find((x) => x.sub === s) || null; };
  const section = (name, create) => {
    let s = doc.sections.find(([n]) => n.toLowerCase() === name.toLowerCase());
    if (!s && create) { s = [name, {}]; doc.sections.push(s); }
    return s ? s[1] : null;
  };

  const STYLE = `
    .ee-main { flex: 1; display: flex; min-height: 0; border-bottom: 1px solid var(--line); }
    .ee-tree { width: 330px; min-width: 220px; border-right: 1px solid var(--line); display: flex; flex-direction: column; min-height: 0; }
    .ee-tree .list { overflow: auto; flex: 1; font-size: 12.5px; padding-bottom: 8px; }
    .ee-tree .grp { padding: 6px 10px 2px; color: var(--fg3); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
    .ee-node { padding: 2px 10px; cursor: pointer; display: flex; gap: 6px; align-items: center; white-space: nowrap; }
    .ee-node:hover { background: var(--bg3); } .ee-node.sel { background: var(--sel); }
    .ee-node .ix { font-family: var(--mono); color: var(--fg2); } .ee-node .nm { overflow: hidden; text-overflow: ellipsis; }
    .ee-node.sub { padding-left: 30px; } .ee-node .tw { width: 12px; color: var(--fg3); text-align: center; }
    .ee-dot { width: 7px; height: 7px; border-radius: 50%; flex: 0 0 7px; margin-left: auto; }
    .ee-dot.error { background: var(--bad); } .ee-dot.warn { background: var(--warn); }
    .ee-form { flex: 1; overflow: auto; padding: 12px 16px; min-width: 0; }
    .ee-form .grid3 { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 8px 12px; }
    .ee-form .field input, .ee-form .field select { width: 100%; } .ee-form .field input[type=checkbox] { width: auto; }
    .ee-probs { max-height: 26vh; overflow: auto; font-size: 12.5px; }
    .ee-probs div { padding: 2px 10px; display: flex; gap: 8px; cursor: pointer; } .ee-probs div:hover { background: var(--bg3); }
    .ee-probs .loc { font-family: var(--mono); color: var(--fg2); min-width: 86px; }
    .ee-bits { height: 8px; background: var(--bg3); border: 1px solid var(--line2); border-radius: 4px; overflow: hidden; margin: 4px 0 8px; }
    .ee-bits > div { height: 100%; background: var(--accent); } .ee-bits.over > div { background: var(--bad); }
    .ee-empty { padding: 30px; color: var(--fg2); max-width: 640px; }
    .ee-subs td input, .ee-subs td select { width: 100%; min-width: 60px; }
    .ee-frame { display: flex; height: 34px; border: 1px solid var(--line2); border-radius: 4px; overflow: hidden; font-size: 11px; margin-top: 4px; }
    .ee-frame.over { border-color: var(--bad); }
    .ee-frame .seg { display: flex; align-items: center; justify-content: center; min-width: 0; overflow: hidden; white-space: nowrap; padding: 0 3px; border-right: 1px solid var(--bg); }
    .ee-frame .free { background: repeating-linear-gradient(45deg, transparent 0 5px, var(--bg3) 5px 10px); color: var(--fg3); }
    .ee-bytes { display: flex; } .ee-bytes span { flex: 1; text-align: center; font-size: 10.5px; color: var(--fg3); border-left: 1px solid var(--line); }
    @media (max-width: 760px) { .ee-main { flex-direction: column; } .ee-tree { width: auto; max-height: 34vh; border-right: 0; border-bottom: 1px solid var(--line); } }`;

  // ---------------------------------------------------------------- state
  function setDoc(d, probs, isDirty) {
    doc = d; problems = probs || []; dirty = !!isDirty; savedAs = ""; sel = { info: true }; expanded.clear();
    saveDraft(); renderAll();
  }
  function changed(recheck = true) {
    dirty = true;
    renderHead();
    clearTimeout(draftTimer); draftTimer = setTimeout(saveDraft, 600);
    if (recheck) { clearTimeout(checkTimer); checkTimer = setTimeout(recheck_, 400); }
  }
  async function recheck_() {
    if (!doc) return;
    const r = await CO.api(API + "/check", { doc }).catch(() => null);
    if (r) { problems = r.problems; renderProblems(); renderTree(); renderFormProblems(); }
  }
  function saveDraft() { CO.prefs.set("eds.draft", doc ? { doc, savedAs, dirty } : null); }

  // ---------------------------------------------------------------- rendering
  function renderAll() { renderHead(); renderTree(); renderForm(); renderProblems(); }

  function renderHead() {
    const h = CO.$("#ee-file", el);
    if (!doc) { h.innerHTML = `<span class="muted">no file</span>`; return; }
    h.innerHTML = `<b>${CO.esc(doc.file_name || "untitled")}</b>${dirty ? ` <span class="badge warn" title="Not saved">changed</span>` : ""}`;
    CO.$("#ee-format", el).value = doc.format || "eds";
    CO.$$("[data-need-doc]", el).forEach((b) => (b.disabled = !doc));
  }

  function probsAt(i, s) { return problems.filter((p) => p.index === i && (s === undefined || p.sub === s)); }
  function dotFor(list) {
    if (list.some((p) => p.level === "error")) return `<span class="ee-dot error"></span>`;
    if (list.some((p) => p.level === "warn")) return `<span class="ee-dot warn"></span>`;
    return "";
  }

  function renderTree() {
    const list = CO.$("#ee-list", el);
    if (!doc) { list.innerHTML = ""; return; }
    const f = filter.toLowerCase();
    const match = (o) => !f || hex4(o.index).toLowerCase().includes(f.replace(/^0x/, "")) || (o.name || "").toLowerCase().includes(f) ||
      (o.subs || []).some((s) => (s.name || "").toLowerCase().includes(f));
    const groups = [["Communication (1000-1FFF)", (i) => i >= 0x1000 && i <= 0x1fff], ["Manufacturer (2000-5FFF)", (i) => i >= 0x2000 && i <= 0x5fff],
      ["Device profile (6000-9FFF)", (i) => i >= 0x6000 && i <= 0x9fff], ["Other", (i) => i < 0x1000 || i > 0x9fff]];
    let html = `<div class="ee-node ${sel.info ? "sel" : ""}" data-info="1"><span class="tw"></span><span class="nm"><b>File and device info</b></span>${dotFor(problems.filter((p) => p.index === null))}</div>
      <div class="ee-node ${sel.pdos ? "sel" : ""}" data-pdos="1"><span class="tw"></span><span class="nm"><b>PDOs</b> <span class="muted small">${pdoList().length}</span></span>${dotFor(problems.filter((p) => p.index >= 0x1400 && p.index <= 0x1bff))}</div>`;
    for (const [title, test] of groups) {
      const objs = doc.objects.filter((o) => test(o.index) && match(o));
      if (!objs.length) continue;
      html += `<div class="grp">${title}</div>`;
      for (const o of objs) {
        const open = expanded.has(o.index) || (f && o.subs);
        html += `<div class="ee-node ${!sel.info && sel.index === o.index && sel.sub === undefined ? "sel" : ""}" data-i="${o.index}">
          <span class="tw" data-tw="${o.index}">${o.subs ? (open ? "&#9662;" : "&#9656;") : ""}</span><span class="ix">${hex4(o.index)}</span><span class="nm">${CO.esc(o.name)}</span>${dotFor(probsAt(o.index))}</div>`;
        if (o.subs && open) for (const s of o.subs) {
          html += `<div class="ee-node sub ${sel.index === o.index && sel.sub === s.sub ? "sel" : ""}" data-i="${o.index}" data-s="${s.sub}"><span class="ix">${hex2(s.sub)}</span><span class="nm">${CO.esc(s.name)}</span>${dotFor(probsAt(o.index, s.sub))}</div>`;
        }
      }
    }
    list.innerHTML = html;
  }

  function renderProblems() {
    const box = CO.$("#ee-probs", el), head = CO.$("#ee-probs-head", el);
    const n = (lv) => problems.filter((p) => p.level === lv).length;
    head.innerHTML = doc ? `Problems <span class="badge ${n("error") ? "bad" : "good"}">${n("error")} error(s)</span> <span class="badge ${n("warn") ? "warn" : ""}">${n("warn")} warning(s)</span> <span class="muted small">${n("info")} note(s)</span>` : "Problems";
    box.innerHTML = problems.map((p, k) => `<div data-p="${k}"><span class="badge ${p.level === "error" ? "bad" : p.level === "warn" ? "warn" : ""}">${p.level}</span><span class="loc">${where(p.index, p.sub)}</span><span>${CO.esc(p.text)}</span></div>`).join("")
      || (doc ? `<div class="muted">No problems found.</div>` : "");
  }

  function selected() {
    if (!doc || sel.info || sel.pdos) return null;
    const o = findObj(sel.index);
    if (!o) return null;
    if (sel.sub === undefined) return { o, v: o.subs ? null : o };
    return { o, v: (o.subs || []).find((s) => s.sub === sel.sub) || null };
  }

  function renderFormProblems() {
    const box = CO.$("#ee-fprobs", el);
    if (!box) return;
    const cur = sel.pdos ? currentPdo() : null;
    const list = sel.info ? problems.filter((p) => p.index === null)
      : sel.pdos ? problems.filter((p) => cur && (p.index === cur.cb || p.index === cur.mb))
      : problems.filter((p) => p.index === sel.index && (sel.sub === undefined || p.sub === sel.sub || p.sub === null));
    box.innerHTML = list.map((p) => `<div class="notice ${p.level === "error" ? "bad" : p.level === "warn" ? "warn" : ""}">${p.sub !== null && p.sub !== undefined && sel.sub === undefined ? `<span class="mono">sub ${hex2(p.sub)}</span> ` : ""}${CO.esc(p.text)}</div>`).join("");
  }

  function input(f, label, value, extra) {
    return `<label class="field"><span>${label}</span><input data-f="${f}" class="${extra && extra.mono === false ? "" : "m"}" value="${CO.esc(value ?? "")}" ${extra && extra.ro ? "readonly" : ""} ${extra && extra.ph ? `placeholder="${CO.esc(extra.ph)}"` : ""}></label>`;
  }
  function varFields(v) {
    const types = meta.data_types.map((d) => `<option value="${d.code}" ${d.code === v.data_type ? "selected" : ""}>${d.name}</option>`).join("");
    const acc = meta.access.map((a) => `<option ${a === v.access ? "selected" : ""}>${a}</option>`).join("");
    const extra = Object.entries(v.extra || {});
    return `<div class="grid3" style="margin-top:8px">
      <label class="field"><span>Data type</span><select data-f="data_type">${types}${meta.data_types.some((d) => d.code === v.data_type) ? "" : `<option selected value="${v.data_type}">0x${hex4(v.data_type || 0)}</option>`}</select></label>
      <label class="field"><span>Access</span><select data-f="access">${acc}${meta.access.includes(v.access) ? "" : `<option selected>${CO.esc(v.access)}</option>`}</select></label>
      ${input("default", "Default value", v.default, { ph: "e.g. 0, 0x1F, $NODEID+0x180" })}
      ${input("low", "Low limit", v.low)}${input("high", "High limit", v.high)}
      <label class="field"><span>PDO mapping</span><label class="row small" style="height:28px"><input type="checkbox" data-f="pdo" ${v.pdo ? "checked" : ""}> can be mapped into a PDO</label></label>
      ${dcf() ? input("value", "DCF value (ParameterValue)", v.value, { ph: "empty = the default" }) + input("denotation", "Denotation", v.denotation, { mono: false }) : ""}
    </div>${extra.length ? `<div class="small muted" style="margin-top:8px">Other keys, written back unchanged: <span class="mono">${extra.map(([k, x]) => CO.esc(k + "=" + x)).join(", ")}</span></div>` : ""}`;
  }

  function renderForm() {
    const box = CO.$("#ee-form", el);
    if (!doc) {
      box.innerHTML = `<div class="ee-empty"><h3>EDS editor</h3><p>Create a new EDS from a CiA 301, 401 or 402 template, open an EDS, DCF or XDD file, or build one from a device on the bus (Create from device).</p>
        <p class="small">Device work only reads (SDO uploads). To write a DCF to a node, use <b>To Nodes</b>: the steps open in the Nodes tab, which shows every frame and asks before sending.</p>
        <div class="row"><button class="btn primary" data-act="new">New EDS&hellip;</button><button class="btn" data-act="open">Open&hellip;</button><button class="btn" data-act="probe">Create from device&hellip;</button></div></div>`;
      return;
    }
    if (sel.info) { box.innerHTML = infoForm(); renderFormProblems(); return; }
    if (sel.pdos) { box.innerHTML = pdoView(); renderFormProblems(); return; }
    const s = selected();
    if (!s || (sel.sub !== undefined && !s.v)) { sel = s ? { index: s.o.index } : { info: true }; return renderForm(); }
    const { o, v } = s;
    let html = `<div id="ee-fprobs"></div>`;
    if (sel.sub === undefined) {
      html += `<div class="row"><h3 style="margin:0">0x${hex4(o.index)} ${CO.esc(o.name)}</h3><span class="spacer"></span>
        ${o.index >= 0x1400 && o.index <= 0x1bff ? `<button class="btn small" data-act="topdo">Open in the PDO editor</button>` : ""}
        <button class="btn small danger" data-act="delobj">Delete object</button></div>
        <div class="grid3" style="margin-top:8px">${input("index", "Index (hex)", hex4(o.index))}${input("name", "Name", o.name, { mono: false })}
        <label class="field"><span>Object type</span><select data-f="object_type">${[[7, "VAR"], [8, "ARRAY"], [9, "RECORD"], [2, "DOMAIN"], [5, "DEFTYPE"], [6, "DEFSTRUCT"]].map(([c, n]) => `<option value="${c}" ${c === o.object_type ? "selected" : ""}>${n}</option>`).join("")}</select></label></div>`;
      if (v) html += varFields(v);
      if (o.subs) {
        if (isMapping(o.index)) html += mappingView(o);
        html += subsTable(o);
      }
    } else {
      html += `<div class="row"><h3 style="margin:0">0x${hex4(o.index)}:${hex2(v.sub)} ${CO.esc(v.name)}</h3><span class="muted small">in ${CO.esc(o.name)}</span><span class="spacer"></span>
        ${v.sub ? `<button class="btn small danger" data-act="delsub" data-sub="${v.sub}">Delete sub-index</button>` : ""}</div>
        <div class="grid3" style="margin-top:8px">${input("sub", "Sub-index", hex2(v.sub), { ro: true })}${input("name", "Name", v.name, { mono: false })}</div>${varFields(v)}`;
    }
    box.innerHTML = html;
    renderFormProblems();
  }

  function subsTable(o) {
    const types = (c) => meta.data_types.map((d) => `<option value="${d.code}" ${d.code === c ? "selected" : ""}>${d.name}</option>`).join("")
      + (meta.data_types.some((d) => d.code === c) ? "" : `<option selected value="${c}">0x${hex4(c || 0)}</option>`);
    const acc = (a) => meta.access.map((x) => `<option ${x === a ? "selected" : ""}>${x}</option>`).join("")
      + (meta.access.includes(a) ? "" : `<option selected>${CO.esc(a)}</option>`);
    return `<h3 style="margin:14px 0 6px">Sub-indices</h3><div style="overflow-x:auto"><table class="tbl compact ee-subs"><thead><tr><th>Sub</th><th>Name</th><th>Data type</th><th>Access</th><th>Default</th>${dcf() ? "<th>DCF value</th>" : ""}<th>PDO</th><th></th></tr></thead><tbody>
      ${o.subs.map((s) => `<tr data-sub="${s.sub}"><td class="m"><a href="#" data-go="${s.sub}">${hex2(s.sub)}</a></td>
        <td><input data-sf="name" value="${CO.esc(s.name)}"></td><td><select data-sf="data_type">${types(s.data_type)}</select></td><td><select data-sf="access">${acc(s.access)}</select></td>
        <td><input class="m" data-sf="default" value="${CO.esc(s.default)}"></td>${dcf() ? `<td><input class="m" data-sf="value" value="${CO.esc(s.value)}"></td>` : ""}
        <td><input type="checkbox" data-sf="pdo" ${s.pdo ? "checked" : ""}></td><td>${s.sub ? `<button class="btn small ghost" data-act="delsub" data-sub="${s.sub}" title="Delete">&times;</button>` : ""}</td></tr>`).join("")}
      </tbody></table></div><div class="row" style="margin-top:6px"><button class="btn small" data-act="addsub">+ sub-index</button></div>`;
  }

  function mappingView(o) {
    const rx = o.index < 0x1a00;
    const s0 = o.subs.find((s) => s.sub === 0);
    const count = Math.min(64, s0 ? edsInt(refValue(s0)) : 0);
    let bits = 0;
    const rows = [];
    for (let i = 1; i <= count; i++) {
      const e = o.subs.find((s) => s.sub === i);
      const v = e ? edsInt(refValue(e)) : 0;
      const ti = Math.floor(v / 65536), ts = (v >> 8) & 0xff, b = v & 0xff;
      bits += b;
      const t = findVar(ti, ts);
      rows.push(`<tr><td class="r m">${i}</td><td class="m">${where(ti, ts)}</td><td>${CO.esc(t ? t.name : ti >= 1 && ti <= 7 ? "dummy " + dtName(ti) : "missing")}</td><td class="r m">${b}</td>
        <td><button class="btn small ghost" data-map-up="${i}" title="Earlier" ${i === 1 ? "disabled" : ""}>&uarr;</button><button class="btn small ghost" data-map-del="${i}" title="Remove">&times;</button></td></tr>`);
    }
    const cands = [];
    for (const ob of doc.objects) for (const v of ob.subs || [ob]) {
      if (!v.pdo || (ob.subs && v.sub === 0)) continue;
      const ok = rx ? ["rw", "wo", "rww", "rwr"].includes(v.access) : ["ro", "rw", "rwr", "rww", "const"].includes(v.access);
      const b = dtBits(v.data_type);
      if (!ok || !b) continue;
      const sub = ob.subs ? v.sub : 0;
      cands.push(`<option value="${ob.index * 65536 + sub * 256 + b}">${where(ob.index, sub)} ${CO.esc(v.name)} (${dtName(v.data_type)})</option>`);
    }
    return `<h3 style="margin:14px 0 4px">${rx ? "RPDO" : "TPDO"} mapping <span class="muted small">(${dcf() ? "DCF values" : "defaults"})</span></h3>
      <div class="ee-bits ${bits > 64 ? "over" : ""}"><div style="width:${Math.min(100, bits * 100 / 64)}%"></div></div><div class="small muted">${bits} of 64 bits</div>
      <table class="tbl compact" style="margin-top:6px"><thead><tr><th class="r">#</th><th>Object</th><th>Name</th><th class="r">Bits</th><th></th></tr></thead><tbody>${rows.join("") || `<tr><td colspan="5" class="empty">Nothing mapped.</td></tr>`}</tbody></table>
      <div class="row" style="margin-top:6px"><select id="ee-map-add" style="max-width:420px"><option value="">Map an object&hellip;</option>${cands.join("")}</select><span class="small muted">${rx ? "writable" : "readable"}, PDO-mappable objects</span></div>`;
  }

  function infoForm() {
    const fi = section("FileInfo", true), di = section("DeviceInfo", true);
    const f = (sec, k, label, mono) => `<label class="field"><span>${label}</span><input data-sec="${sec}" data-k="${k}" class="${mono ? "m" : ""}" value="${CO.esc((sec === "FileInfo" ? fi : sec === "DeviceInfo" ? di : section(sec, true))[k] ?? "")}"></label>`;
    const bauds = [10, 20, 50, 125, 250, 500, 800, 1000].map((b) => `<label class="row small"><input type="checkbox" data-baud="${b}" ${String(di["BaudRate_" + b] ?? "0") === "1" ? "checked" : ""}>${b}k</label>`).join("");
    const others = doc.sections.map(([n]) => n).filter((n) => !["fileinfo", "deviceinfo", "dummyusage", "devicecomissioning"].includes(n.toLowerCase()));
    return `<div id="ee-fprobs"></div><h3 style="margin:0 0 8px">File</h3><div class="grid3">
        ${f("FileInfo", "FileName", "File name (set when saved)", true)}${f("FileInfo", "FileVersion", "File version", true)}${f("FileInfo", "FileRevision", "File revision", true)}
        ${f("FileInfo", "EDSVersion", "EDS version", true)}${f("FileInfo", "CreatedBy", "Created by")}${f("FileInfo", "ModifiedBy", "Modified by")}</div>
      <label class="field" style="margin-top:8px"><span>Description</span><input data-sec="FileInfo" data-k="Description" value="${CO.esc(fi.Description ?? "")}"></label>
      <h3 style="margin:16px 0 8px">Device</h3><div class="grid3">
        ${f("DeviceInfo", "VendorName", "Vendor name")}${f("DeviceInfo", "VendorNumber", "Vendor number", true)}${f("DeviceInfo", "ProductName", "Product name")}
        ${f("DeviceInfo", "ProductNumber", "Product number", true)}${f("DeviceInfo", "RevisionNumber", "Revision number", true)}${f("DeviceInfo", "OrderCode", "Order code")}
        ${f("DeviceInfo", "SimpleBootUpSlave", "Simple boot-up slave (0/1)", true)}${f("DeviceInfo", "SimpleBootUpMaster", "Simple boot-up master (0/1)", true)}${f("DeviceInfo", "LSS_Supported", "LSS supported (0/1)", true)}
        ${f("DeviceInfo", "Granularity", "Granularity", true)}</div>
      <div class="field" style="margin-top:8px"><span>Bit rates</span><div class="row">${bauds}</div></div>
      <div class="small muted" style="margin-top:4px">NrOfRXPDO / NrOfTXPDO are counted from the PDO objects when the file is saved.</div>
      ${dcf() ? `<h3 style="margin:16px 0 8px">Commissioning (DCF)</h3><div class="grid3">${f("DeviceComissioning", "NodeID", "Node id", true)}${f("DeviceComissioning", "Baudrate", "Bit rate (kbit/s)", true)}${f("DeviceComissioning", "NodeName", "Node name")}</div>` : ""}
      <label class="field" style="margin-top:16px"><span>Comments (one per line)</span><textarea id="ee-comments" rows="4" style="width:100%">${CO.esc((doc.comments || []).join("\n"))}</textarea></label>
      ${others.length ? `<div class="small muted" style="margin-top:8px">Other sections, written back unchanged: ${others.map(CO.esc).join(", ")}</div>` : ""}`;
  }

  // ---------------------------------------------------------------- PDO editor
  const COMM_SUBS = { 1: ["COB-ID", 7], 2: ["Transmission type", 5], 3: ["Inhibit time", 6], 5: ["Event timer", 6], 6: ["SYNC start value", 5] };
  const DUMMIES = [[1, 1], [2, 8], [3, 16], [4, 32], [5, 8], [6, 16], [7, 32]];      // dummy objects (data type index, bits)
  const RE_HEX = /^\s*(0x)?[0-9a-f]+\s*$/i;
  const RE_COB = /^\s*(\$NODEID\s*\+\s*)?(0x[0-9a-f]+|\d+)(\s*\+\s*\$NODEID)?\s*$/i;     // what a COB-ID may look like
  const strictHex = (t) => (RE_HEX.test(String(t)) ? parseInt(String(t).trim().replace(/^0x/i, ""), 16) : NaN);
  const entryError = (v) => (!Number.isFinite(v) || v < 0 || v > 0xffffffff ? "Entry: 8 hex digits, e.g. 0x60410010"
    : Math.floor(v / 65536) === 0 ? "Entry: the index (first 4 hex digits) must not be 0"
    : (v & 0xff) < 1 || (v & 0xff) > 64 ? "Entry: the length (last 2 hex digits) must be 1-64 bits" : "");
  function pdoList() {
    const out = [];
    if (!doc) return out;
    for (const [kind, cb, mb] of [["rpdo", 0x1400, 0x1600], ["tpdo", 0x1800, 0x1a00]]) {
      for (let n = 0; n < 512; n++) {
        const c = findObj(cb + n), m = findObj(mb + n);
        if (c || m) out.push({ kind, n: n + 1, c: c && c.subs ? c : null, m: m && m.subs ? m : null, cb: cb + n, mb: mb + n, label: `${kind.toUpperCase()} ${n + 1}` });
      }
    }
    return out;
  }
  function currentPdo() {                                   // null when the chosen PDO is gone
    const list = pdoList();
    if (sel.pdo) return list.find((p) => p.kind === sel.pdo.kind && p.n === sel.pdo.n) || null;
    return list[0] || null;
  }
  const hex8 = (v) => (v >>> 0).toString(16).toUpperCase().padStart(8, "0");
  const nodeOf = () => +(section("DeviceComissioning")?.NodeID || 0) || 0;
  function cobOf(text) {                                    // decode a COB-ID value (bit 31 invalid, 30 no RTR, 29 extended)
    const v = edsInt(text) >>> 0, ext = !!((v >>> 29) & 1);
    return { valid: !((v >>> 31) & 1), rtr: !((v >>> 30) & 1), ext, rel: /\$NODEID/i.test(String(text || "")), id: ext ? v & 0x1fffffff : v & 0x7ff };
  }
  function cobText(c) {                                     // ...and encode it back, keeping $NODEID when it follows the node id
    const v = ((c.valid ? 0 : 0x80000000) + (c.rtr ? 0 : 0x40000000) + (c.ext ? 0x20000000 : 0) + c.id) >>> 0;
    return c.rel ? `$NODEID+0x${v.toString(16).toUpperCase()}` : `0x${hex8(v)}`;
  }
  const ttText = (t) => t === "" || t === null || t === undefined || Number.isNaN(t) ? "" : t === 0 ? "synchronous, acyclic" : t >= 1 && t <= 240 ? (t === 1 ? "synchronous, every SYNC" : `synchronous, every ${t} SYNC`)
    : ({ 252: "RTR only, synchronous", 253: "RTR only, event-driven", 254: "event-driven (manufacturer)", 255: "event-driven (device profile)" }[t] || `reserved (${t})`);
  function pdoVal(o, sub) { const v = o && (o.subs || []).find((s) => s.sub === sub); return v ? refValue(v) : ""; }
  function setPdoVal(o, sub, text) {                        // only parameters the object has (see addPdoParam)
    const v = o.subs.find((s) => s.sub === sub);
    if (v) v[mapField()] = text;
  }
  async function addPdoParam(o, sub) {
    const [name, dt] = COMM_SUBS[sub];
    if (!(await CO.confirm(`Add the ${name.toLowerCase()}?`, `<p>0x${hex4(o.index)} has no sub-index ${sub} (${CO.esc(name)}). Adding it changes the device description: the file then says the device has it. Only add it if the device does.</p>`, "Add"))) return;
    o.subs.push({ name, data_type: dt, access: "rw", default: "0", low: "", high: "", pdo: false, value: "", denotation: "", extra: {}, sub });
    o.subs.sort((a, b) => a.sub - b.sub);
    const s0 = o.subs.find((s) => s.sub === 0);
    if (s0) {                                               // the highest sub-index only ever grows here
      const top = Math.max(...o.subs.map((s) => s.sub));
      if (edsInt(s0.default) < top) s0.default = String(top);
      if (String(s0.value ?? "").trim() && edsInt(s0.value) < top) s0.value = String(top);
    }
    renderAll(); changed();
  }

  function pdoView() {
    const list = pdoList(), cur = currentPdo(), nodeId = nodeOf();
    const rows = list.map((p) => {
      const cob = p.c ? cobOf(pdoVal(p.c, 1)) : null, tt = p.c ? pdoVal(p.c, 2) : "";
      const ents = p.m ? mapEntries(p.m) : [], bits = ents.reduce((a, e) => a + (e & 0xff), 0);
      const names = ents.map((e) => { const ti = Math.floor(e / 65536), t = findVar(ti, (e >> 8) & 0xff); return t ? t.name : ti >= 1 && ti <= 7 ? "dummy" : "?"; });
      const idTxt = !cob ? "" : !cob.rel ? "0x" + cob.id.toString(16).toUpperCase()
        : nodeId ? `0x${(cob.id + nodeId).toString(16).toUpperCase()} (node ${nodeId})` : `node id + 0x${cob.id.toString(16).toUpperCase()}`;
      return `<tr data-pdo-sel="${p.kind}:${p.n}" class="${cur && cur.kind === p.kind && cur.n === p.n ? "sel" : ""}" style="cursor:pointer">
        <td><b>${p.label}</b>${dotFor(problems.filter((x) => x.index === p.cb || x.index === p.mb))}</td>
        <td class="m">${cob ? CO.esc(idTxt) : "-"}</td><td>${cob ? (cob.valid ? `<span class="badge good">on</span>` : `<span class="badge">off</span>`) : ""}</td>
        <td>${CO.esc(ttText(tt === "" ? "" : edsInt(tt)))}</td><td class="r m ${bits > 64 ? "err" : ""}">${bits}</td><td class="dec" style="max-width:360px">${CO.esc(names.join(", "))}</td></tr>`;
    }).join("");
    let html = `<div id="ee-fprobs"></div><div class="row"><h3 style="margin:0">PDOs</h3><span class="muted small">${dcf() ? "DCF values" : "defaults"}${nodeId ? `, ids for node ${nodeId}` : ""}</span><span class="spacer"></span>
      <button class="btn small" data-act="pdoadd" data-kind="rpdo">+ RPDO</button><button class="btn small" data-act="pdoadd" data-kind="tpdo">+ TPDO</button>
      <button class="btn small" data-act="pdoread" title="Read the PDO objects of this file from a node (SDO reads only)">Read PDOs from node&hellip;</button></div>
      <div style="overflow-x:auto;margin-top:8px"><table class="tbl compact"><thead><tr><th>PDO</th><th>COB-ID</th><th></th><th>Transmission</th><th class="r">Bits</th><th>Mapped</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="6" class="empty">No PDOs. Add one with + RPDO or + TPDO.</td></tr>`}</tbody></table></div>`;
    if (cur) html += pdoEditor(cur, nodeId);
    else if (sel.pdo && list.length) html += `<div class="notice">${sel.pdo.kind.toUpperCase()} ${sel.pdo.n} is not in the file any more. Pick a PDO above.</div>`;
    return html;
  }

  function pdoEditor(p, nodeId) {
    const rx = p.kind === "rpdo";
    let html = `<div class="row" style="margin-top:16px"><h3 style="margin:0">${p.label}</h3><span class="muted small mono">0x${hex4(p.cb)} / 0x${hex4(p.mb)}</span><span class="spacer"></span>
      <button class="btn small danger" data-act="pdodel">Delete ${p.label}</button></div>`;
    if (p.c) {
      const raw = pdoVal(p.c, 1), cob = cobOf(raw), tt = pdoVal(p.c, 2), t = tt === "" ? "" : edsInt(tt);
      const cyc = t !== "" && t >= 1 && t <= 240;
      const has = (sub) => p.c.subs.some((s) => s.sub === sub);
      const opt = (v, label) => `<option value="${v}" ${String(cyc ? "cyc" : t) === String(v) ? "selected" : ""}>${label}</option>`;
      const param = (sub, key, label) => has(sub) ? `<label class="field"><span>${label}</span><input class="m" data-pc="${key}" value="${CO.esc(pdoVal(p.c, sub))}"></label>`
        : `<div class="field"><span>${label}</span><button class="btn small" data-act="pdoparam" data-sub="${sub}" title="The object has no sub-index ${sub}">+ add</button></div>`;
      html += `<h3 style="margin:12px 0 6px">Communication</h3><div class="grid3">
        <label class="field"><span>COB-ID (as stored)</span><input class="m" data-pc="cob" value="${CO.esc(raw)}"></label>
        <label class="field"><span>CAN id (hex)${cob.rel ? ", added to the node id" : ""}</span><input class="m" data-pc="id" value="${cob.id.toString(16).toUpperCase()}"></label>
        <div class="field"><span>Flags</span><div class="row small" style="min-height:28px">
          <label class="row"><input type="checkbox" data-pc="valid" ${cob.valid ? "checked" : ""}>enabled</label>
          <label class="row" title="COB-ID = node id + CAN id"><input type="checkbox" data-pc="rel" ${cob.rel ? "checked" : ""}>node relative</label>
          <label class="row"><input type="checkbox" data-pc="ext" ${cob.ext ? "checked" : ""}>29-bit</label>
          <label class="row"><input type="checkbox" data-pc="rtr" ${cob.rtr ? "checked" : ""}>RTR allowed</label></div></div>
        ${has(2) ? `<label class="field"><span>Transmission type</span><select data-pc="tt">${opt("", "-")}${opt(0, "0: synchronous, acyclic")}${opt("cyc", "1-240: every n-th SYNC")}${rx ? "" : opt(252, "252: RTR only, synchronous") + opt(253, "253: RTR only, event-driven")}${opt(254, "254: event-driven (manufacturer)")}${opt(255, "255: event-driven (device profile)")}${t !== "" && !cyc && ![0, 254, 255].concat(rx ? [] : [252, 253]).includes(t) ? opt(t, `${t}: ${ttText(t)}`) : ""}</select></label>` : param(2, "tt", "Transmission type")}
        ${cyc ? `<label class="field"><span>Every n-th SYNC (1-240)</span><input class="m" type="number" min="1" max="240" data-pc="ttn" value="${t}"></label>` : ""}
        ${rx ? "" : param(3, "inh", "Inhibit time (100 &micro;s units)")}
        ${param(5, "evt", rx ? "Event timer / deadline (ms)" : "Event timer (ms)")}
        ${rx ? "" : param(6, "sync", "SYNC start value")}
      </div><div class="small muted" style="margin-top:4px">${cob.valid ? "" : "Disabled (bit 31 set). "}${cob.rel ? `Id on the bus: node id + 0x${cob.id.toString(16).toUpperCase()}${nodeId ? ` = 0x${(cob.id + nodeId).toString(16).toUpperCase()} for node ${nodeId}` : ""}.` : ""}
        ${ttText(t) ? " Transmission: " + CO.esc(ttText(t)) + "." : ""}${!rx && pdoVal(p.c, 3) !== "" ? ` Inhibit ${(edsInt(pdoVal(p.c, 3)) / 10).toFixed(1)} ms.` : ""}</div>`;
    } else html += `<div class="notice warn">No communication object 0x${hex4(p.cb)} (or it is not a RECORD).</div>`;
    if (!p.m) return html + `<div class="notice warn">No mapping object 0x${hex4(p.mb)} (or it is not a RECORD).</div>`;
    const ents = mapEntries(p.m);
    let pos = 0;
    const segs = [], rows = [];
    ents.forEach((e, k) => {
      const ti = Math.floor(e / 65536), ts = (e >> 8) & 0xff, b = e & 0xff, t = findVar(ti, ts);
      const name = t ? t.name : ti >= 1 && ti <= 7 ? "dummy" : "missing object";
      const color = ti >= 1 && ti <= 7 ? "var(--bg3)" : `hsl(${(k * 67 + 200) % 360} 60% 50% / .45)`;
      segs.push(`<div class="seg" style="flex:${Math.max(b, 1)};background:${color}" title="${CO.esc(`${where(ti, ts)} ${name}, ${b} bits from bit ${pos}`)}">${CO.esc(name)}</div>`);
      rows.push(`<tr><td class="r m">${k + 1}</td><td class="m">${where(ti, ts)}</td><td>${CO.esc(name)}${t && dtBits(t.data_type) && dtBits(t.data_type) !== b ? ` <span class="badge bad">${dtName(t.data_type)} is ${dtBits(t.data_type)} bits</span>` : ""}</td>
        <td class="r m">${b}</td><td class="m">${Math.floor(pos / 8)}.${pos % 8}</td><td><input class="m" data-pe="${k}" value="0x${hex8(e)}" style="width:110px" title="index (16 bits), sub-index (8), length in bits (8)"></td>
        <td><button class="btn small ghost" data-map-up="${k + 1}" ${k ? "" : "disabled"} title="Earlier">&uarr;</button><button class="btn small ghost" data-map-dn="${k + 1}" ${k < ents.length - 1 ? "" : "disabled"} title="Later">&darr;</button><button class="btn small ghost" data-map-del="${k + 1}" title="Remove">&times;</button></td></tr>`);
      pos += b;
    });
    if (pos < 64) segs.push(`<div class="seg free" style="flex:${64 - pos}">${64 - pos} bits free</div>`);
    const cands = [];
    for (const ob of doc.objects) for (const v of ob.subs || [ob]) {
      if (!v.pdo || (ob.subs && v.sub === 0)) continue;
      const ok = rx ? ["rw", "wo", "rww"].includes(v.access) : ["ro", "rw", "rwr", "const"].includes(v.access);
      const b = dtBits(v.data_type);
      if (ok && b) { const sub = ob.subs ? v.sub : 0; cands.push(`<option value="${ob.index * 65536 + sub * 256 + b}">${where(ob.index, sub)} ${CO.esc(v.name)} (${dtName(v.data_type)})</option>`); }
    }
    return html + `<h3 style="margin:16px 0 6px">Mapping <span class="muted small">${pos} of 64 bits (${Math.ceil(pos / 8)} data bytes)</span></h3>
      <div class="ee-frame ${pos > 64 ? "over" : ""}">${segs.join("")}</div><div class="ee-bytes">${[0, 1, 2, 3, 4, 5, 6, 7].map((b) => `<span>byte ${b}</span>`).join("")}</div>
      <div style="overflow-x:auto"><table class="tbl compact" style="margin-top:8px"><thead><tr><th class="r">#</th><th>Object</th><th>Name</th><th class="r">Bits</th><th>Byte.bit</th><th>Entry (encoded)</th><th></th></tr></thead>
      <tbody>${rows.join("") || `<tr><td colspan="7" class="empty">Nothing mapped.</td></tr>`}</tbody></table></div>
      <div class="row" style="margin-top:6px"><select id="ee-pdo-add" style="max-width:420px"><option value="">Map an object&hellip;</option>${cands.join("")}</select>
        ${rx ? `<select id="ee-pdo-dummy" title="Bits the device ignores (RPDOs only)"><option value="">Gap (dummy)&hellip;</option>${DUMMIES.map(([i, b]) => `<option value="${i * 65536 + b}">${dtName(i)} (${b} bits)</option>`).join("")}</select>` : ""}
        <label class="row small">Entry <input class="m" id="ee-pdo-raw" placeholder="0x60410010" style="width:110px"></label><button class="btn small" data-act="pdoraw">Add</button></div>
      <p class="small muted">An entry is index (16 bits), sub-index (8 bits) and length in bits (8 bits): 0x60410010 maps 0x6041:00 with 16 bits. Edit the encoded entry to type one directly.</p>`;
  }

  function onPdoInput(ev) {
    const t = ev.target, p = currentPdo();
    if (!p || !t.dataset.pc || !p.c) return;
    const k = t.dataset.pc, refuse = (msg) => { CO.toast(msg, "bad"); renderForm(); };
    if (k === "cob") {                                      // typed: stored once it is a valid value
      const ok = RE_COB.test(t.value);
      t.style.borderColor = ok ? "" : "var(--bad)";
      if (!ok) return;
      setPdoVal(p.c, 1, t.value.trim());
    } else if (["valid", "rel", "ext", "rtr"].includes(k) || (k === "id" && ev.type === "change")) {
      const c = cobOf(pdoVal(p.c, 1)), nodeId = nodeOf();
      if (k === "id") {
        const v = strictHex(t.value);
        if (!Number.isFinite(v) || v > (c.ext ? 0x1fffffff : 0x7ff)) return refuse(`CAN id: hex up to ${c.ext ? "1FFFFFFF" : "7FF (tick 29-bit for more)"}`);
        c.id = v;
      } else if (k === "rel") {                             // the same id on the bus, written with or without the node id
        if (!nodeId) return refuse("Node relative changes how the id is written: set the node id first (File and device info, format DCF), or type the COB-ID");
        const id = t.checked ? c.id - nodeId : c.id + nodeId;
        if (id < 0 || id > (c.ext ? 0x1fffffff : 0x7ff)) return refuse(`The id 0x${c.id.toString(16).toUpperCase()} does not fit for node ${nodeId}`);
        c.id = id; c.rel = t.checked;
      } else if (k === "ext" && !t.checked && c.id > 0x7ff) {
        return refuse(`0x${c.id.toString(16).toUpperCase()} needs 29 bits: change the CAN id first`);
      } else if (k === "valid" && t.checked && c.id === 0 && !c.rel) {
        return refuse("Give the PDO a CAN id before enabling it (0x000 is the NMT id)");
      } else c[k] = t.checked;
      setPdoVal(p.c, 1, cobText(c));
    } else if (k === "tt") { if (t.value === "") return; setPdoVal(p.c, 2, t.value === "cyc" ? "1" : t.value); }
    else if (k === "ttn") setPdoVal(p.c, 2, String(Math.min(240, Math.max(1, +t.value || 1))));
    else if (k === "inh") setPdoVal(p.c, 3, t.value.trim());
    else if (k === "evt") setPdoVal(p.c, 5, t.value.trim());
    else if (k === "sync") setPdoVal(p.c, 6, t.value.trim());
    else return;
    changed();
    if (t.type === "checkbox" || t.tagName === "SELECT" || k === "id") renderForm();
  }
  function onPdoChange(ev) {
    const t = ev.target, p = currentPdo();
    if (!p) return;
    if ((t.id === "ee-pdo-add" || t.id === "ee-pdo-dummy") && t.value && p.m) { mapAdd(p.m, +t.value); return; }
    if (t.dataset.pe !== undefined && p.m) {
      const v = strictHex(t.value), err = entryError(v);
      if (err) { CO.toast(err, "bad"); renderForm(); return; }
      const e = mapEntries(p.m); e[+t.dataset.pe] = v; mapStore(p.m, e); return;
    }
    if (t.dataset.pc === "id") { onPdoInput(ev); return; }
    if (t.dataset.pc === "cob" && !RE_COB.test(t.value)) { CO.toast("COB-ID: a number such as 0x185, or $NODEID+0x180", "bad"); renderForm(); return; }
    if (t.dataset.pc && t.type !== "checkbox" && t.tagName !== "SELECT") renderForm();    // text fields: redraw once typed
  }
  async function pdoAdd(kind) {
    const r = await CO.tryApi(API + "/pdo/add", { doc, kind });
    if (!r) return;
    // only the new objects come back, so nothing typed meanwhile is lost
    if (r.objects.some((o) => findObj(o.index))) { CO.toast("The file changed meanwhile: try again", "bad"); return; }
    doc.objects.push(...r.objects); doc.objects.sort((a, b) => a.index - b.index);
    sel = { pdos: true, pdo: { kind, n: r.number } };
    renderAll(); changed();
    CO.toast(`${kind.toUpperCase()} ${r.number} added` + (r.number > 4 ? ": disabled until you give it a COB-ID" : ""), "good");
  }
  async function pdoDel() {
    const p = currentPdo();
    if (!p || !(await CO.confirm(`Delete ${p.label}`, `<p>Removes 0x${hex4(p.cb)} and 0x${hex4(p.mb)}.</p>`, "Delete", true))) return;
    doc.objects = doc.objects.filter((o) => o.index !== p.cb && o.index !== p.mb);
    sel = { pdos: true };
    renderAll(); changed();
  }
  function pdoOf(index) {
    for (const [kind, cb, mb] of [["rpdo", 0x1400, 0x1600], ["tpdo", 0x1800, 0x1a00]]) {
      if (index >= cb && index < cb + 512) return { kind, n: index - cb + 1 };
      if (index >= mb && index < mb + 512) return { kind, n: index - mb + 1 };
    }
    return null;
  }

  // ---------------------------------------------------------------- edits
  function onFormInput(ev) {
    if (sel.pdos) { onPdoInput(ev); return; }
    const t = ev.target;
    if (!doc) return;
    if (t.dataset.sec) { section(t.dataset.sec, true)[t.dataset.k] = t.value; changed(); return; }
    if (t.dataset.baud) { section("DeviceInfo", true)["BaudRate_" + t.dataset.baud] = t.checked ? "1" : "0"; changed(); return; }
    if (t.id === "ee-comments") { doc.comments = t.value.split("\n").filter((l, i, a) => l || i < a.length - 1); changed(false); return; }
    const s = selected();
    if (!s) return;
    if (t.dataset.sf) {                                         // subs table row
      const sub = s.o.subs.find((x) => x.sub === +t.closest("tr").dataset.sub);
      setField(sub, t.dataset.sf, t);
      if (ev.type === "change" || t.type === "checkbox") renderTree();
      changed();
      return;
    }
    const f = t.dataset.f;
    if (!f) return;
    if (f === "index") return;                                  // on change only
    if (f === "object_type") return;
    if (f === "name") { (s.v && sel.sub !== undefined ? s.v : s.o).name = t.value; renderTree(); changed(); return; }
    if (s.v) setField(s.v, f, t);
    changed();
  }
  function setField(v, f, t) {
    if (f === "pdo") v.pdo = t.checked;
    else if (f === "data_type") v.data_type = +t.value;
    else v[f] = t.value;
    const g = { data_type: "type", access: "access", default: "default" }[f];
    if (g && v.guess) v.guess = v.guess.filter((x) => x !== g);
  }

  function onFormChange(ev) {
    if (sel.pdos) { onPdoChange(ev); return; }
    const t = ev.target, s = selected();
    if (t.id === "ee-map-add" && t.value && s) { mapAdd(s.o, +t.value); return; }
    if (!s || !t.dataset.f) return;
    if (t.dataset.f === "index") {
      const v = parseInt(t.value.replace(/^0x/i, ""), 16);
      if (!(v > 0 && v <= 0xffff) || (v !== s.o.index && findObj(v))) { CO.toast(findObj(v) ? `0x${hex4(v)} already exists` : "Index: hex 0001-FFFF", "bad"); t.value = hex4(s.o.index); return; }
      if (s.o.guess) s.o.guess = s.o.guess.filter((x) => x !== "structure");
      s.o.index = v; sel.index = v;
      doc.objects.sort((a, b) => a.index - b.index);
      renderAll(); changed();
    } else if (t.dataset.f === "object_type") {
      setObjectType(s.o, +t.value);
    }
  }

  async function setObjectType(o, ot) {
    const structured = (x) => x === 8 || x === 9;
    if (structured(ot) && !o.subs) {
      const v = {}; VAR_FIELDS.forEach((k) => { if (k in o) { v[k] = o[k]; delete o[k]; } });
      o.subs = [{ name: "Highest sub-index supported", data_type: 5, access: "ro", default: "1", low: "", high: "", pdo: false, value: "", denotation: "", extra: {}, sub: 0 },
        { name: o.name, ...v, extra: v.extra || {}, sub: 1 }];
    } else if (!structured(ot) && o.subs) {
      if (!(await CO.confirm("Make it a VAR?", `<p>0x${hex4(o.index)} has ${o.subs.length} sub-indices. A VAR keeps only sub-index 1's settings.</p>`, "Make it a VAR"))) { renderForm(); return; }
      const s1 = o.subs.find((s) => s.sub === 1) || o.subs[0];
      VAR_FIELDS.forEach((k) => { if (k in s1) o[k] = s1[k]; });
      delete o.subs;
    }
    o.object_type = ot;
    if (o.guess) o.guess = o.guess.filter((x) => x !== "structure");
    renderAll(); changed();
  }

  function mapField() { return dcf() ? "value" : "default"; }
  function mapEntries(o) {
    const s0 = o.subs.find((s) => s.sub === 0);
    const n = Math.min(64, s0 ? edsInt(refValue(s0)) : 0);
    const out = [];
    for (let i = 1; i <= n; i++) { const e = o.subs.find((s) => s.sub === i); out.push(e ? edsInt(refValue(e)) : 0); }
    return out;
  }
  function mapStore(o, entries) {
    const f = mapField();
    const total = Math.max(entries.length, ...o.subs.map((s) => s.sub));
    for (let i = 1; i <= Math.min(64, Math.max(total, entries.length)); i++) {
      let e = o.subs.find((s) => s.sub === i);
      if (!e) { if (i > entries.length) continue; e = { name: `Application object ${i}`, data_type: 7, access: "rw", default: "0x00000000", low: "", high: "", pdo: false, value: "", denotation: "", extra: {}, sub: i }; o.subs.push(e); }
      e[f] = "0x" + (i <= entries.length ? entries[i - 1] : 0).toString(16).toUpperCase().padStart(8, "0");
    }
    o.subs.sort((a, b) => a.sub - b.sub);
    o.subs.find((s) => s.sub === 0)[f] = String(entries.length);
    renderAll(); changed();
  }
  function mapAdd(o, entry) {
    const e = mapEntries(o);
    if (e.length >= 64) return CO.toast("64 entries at most", "bad");
    const ti = Math.floor(entry / 65536);
    if (ti >= 1 && ti <= 7) section("DummyUsage", true)[`Dummy000${ti}`] = "1";     // the file must allow that dummy
    e.push(entry); mapStore(o, e);
  }

  async function addObject() {
    let next = 0x2000; while (findObj(next) && next < 0x5fff) next++;
    const types = meta.data_types.map((d) => `<option value="${d.code}" ${d.code === 7 ? "selected" : ""}>${d.name}</option>`).join("");
    const r = await CO.modal({ title: "Add object", body: `<div class="grid2">
        <label class="field"><span>Index (hex)</span><input id="ao-i" class="m" value="${hex4(next)}"></label>
        <label class="field"><span>Name</span><input id="ao-n" value="New object"></label>
        <label class="field"><span>Object type</span><select id="ao-t"><option value="7">VAR</option><option value="8">ARRAY</option><option value="9">RECORD</option></select></label>
        <label class="field"><span>Data type</span><select id="ao-d">${types}</select></label>
        <label class="field"><span>Access</span><select id="ao-a">${meta.access.map((a) => `<option ${a === "rw" ? "selected" : ""}>${a}</option>`).join("")}</select></label></div>`,
      buttons: [{ label: "Cancel", value: "cancel" }, { label: "Add", value: "ok", cls: "primary" }],
      validate: (v, f) => { const i = parseInt(CO.$("#ao-i", f).value.replace(/^0x/i, ""), 16); return !(i > 0 && i <= 0xffff) ? "Index: hex 0001-FFFF" : findObj(i) ? `0x${hex4(i)} already exists` : ""; } });
    if (!r) return;
    const f = r.form, i = parseInt(CO.$("#ao-i", f).value.replace(/^0x/i, ""), 16), ot = +CO.$("#ao-t", f).value;
    const v = { name: CO.$("#ao-n", f).value || "New object", data_type: +CO.$("#ao-d", f).value, access: CO.$("#ao-a", f).value, default: "0", low: "", high: "", pdo: false, value: "", denotation: "", extra: {} };
    const o = ot === 7 ? { index: i, object_type: 7, ...v } : { index: i, name: v.name, object_type: ot, extra: {}, subs: [
      { name: "Highest sub-index supported", data_type: 5, access: "ro", default: "1", low: "", high: "", pdo: false, value: "", denotation: "", extra: {}, sub: 0 }, { ...v, name: v.name + " 1", sub: 1 }] };
    doc.objects.push(o); doc.objects.sort((a, b) => a.index - b.index);
    sel = { index: i }; if (o.subs) expanded.add(i);
    renderAll(); changed();
  }

  function addSub(o) {
    const last = o.subs[o.subs.length - 1];
    const n = Math.max(...o.subs.map((s) => s.sub)) + 1;
    if (n > 0xfe) return CO.toast("Sub-index FE is the last one", "bad");
    o.subs.push({ ...JSON.parse(JSON.stringify(last.sub ? last : { data_type: 7, access: "rw", default: "0", low: "", high: "", pdo: false, value: "", denotation: "", extra: {} })), name: (o.name || "Sub") + " " + n, sub: n, guess: [] });
    const s0 = o.subs.find((s) => s.sub === 0);
    if (s0 && !isMapping(o.index) && o.index !== 0x1003) s0.default = String(n);
    renderAll(); changed();
  }
  function delSub(o, sub) {
    o.subs = o.subs.filter((s) => s.sub !== sub);
    const s0 = o.subs.find((s) => s.sub === 0);
    if (s0 && !isMapping(o.index) && o.index !== 0x1003) s0.default = String(Math.max(0, ...o.subs.map((s) => s.sub)));
    if (sel.sub === sub) delete sel.sub;
    renderAll(); changed();
  }

  // ---------------------------------------------------------------- file actions
  async function loadMeta() { if (!meta.data_types.length) meta = await CO.api(API + "/templates"); }
  async function discardOk() { return !doc || !dirty || CO.confirm("Discard changes?", `<p>${CO.esc(doc.file_name || "This file")} has changes that are not saved.</p>`, "Discard", true); }

  async function newDoc() {
    if (!(await discardOk())) return;
    const r = await CO.modal({ title: "New EDS", body: `<label class="field"><span>Start from</span><select id="nd-p">${meta.templates.map((t) => `<option value="${t.id}">${CO.esc(t.title)}</option>`).join("")}</select></label>
      <div class="grid2" style="margin-top:8px"><label class="field"><span>Vendor name</span><input id="nd-vn"></label><label class="field"><span>Vendor number (hex)</span><input id="nd-vi" class="m" placeholder="0x00000000"></label>
      <label class="field"><span>Product name</span><input id="nd-pn"></label><label class="field"><span>Product number (hex)</span><input id="nd-pi" class="m" placeholder="0x00000000"></label>
      <label class="field"><span>File name</span><input id="nd-f" value="device.eds"></label></div>`,
      buttons: [{ label: "Cancel", value: "cancel" }, { label: "Create", value: "ok", cls: "primary" }] });
    if (!r) return;
    const f = r.form;
    const d = await CO.tryApi(API + "/new", { profile: CO.$("#nd-p", f).value, vendor_name: CO.$("#nd-vn", f).value, vendor_number: CO.$("#nd-vi", f).value.trim(),
      product_name: CO.$("#nd-pn", f).value, product_number: CO.$("#nd-pi", f).value.trim(), file_name: CO.$("#nd-f", f).value });
    if (d) setDoc(d.doc, d.problems, true);
  }

  async function openDoc() {
    if (!(await discardOk())) return;
    const files = await CO.api(API + "/files").catch(() => ({ files: [] }));
    const list = files.files.map((x, k) => `<tr><td><a href="#" data-k="${k}">${CO.esc(x.name)}</a></td><td class="muted">${CO.esc(x.folder)}</td><td class="r muted">${CO.fmtBytes(x.size)}</td></tr>`).join("");
    let picked = null;
    const r = await CO.modal({ title: "Open EDS / DCF / XDD", wide: true, body: `
      ${list ? `<div style="max-height:40vh;overflow:auto"><table class="tbl compact"><thead><tr><th>File</th><th>Folder</th><th class="r">Size</th></tr></thead><tbody>${list}</tbody></table></div>` : `<p class="muted small">No files in ${CO.esc(files.folder || "the eds folder")} yet.</p>`}
      <label class="field" style="margin-top:10px"><span>Or a path on this PC</span><input id="op-path" class="m" placeholder="C:\\path\\device.eds"></label>
      <div class="row" style="margin-top:10px"><label class="btn">Upload a file&hellip;<input type="file" id="op-file" accept=".eds,.dcf,.xdd,.xdc" hidden></label><span class="small muted">EDS, DCF, XDD/XDC</span></div>`,
      buttons: [{ label: "Cancel", value: "cancel" }, { label: "Open path", value: "ok", cls: "primary" }],
      onOpen(form) {
        form.addEventListener("click", (ev) => { const a = ev.target.closest("a[data-k]"); if (!a) return; ev.preventDefault(); picked = files.files[+a.dataset.k].path; CO.$("#op-path", form).value = picked; form.requestSubmit(form.querySelector('button[value="ok"]')); });
        CO.$("#op-file", form).onchange = async (ev) => {
          const file = ev.target.files[0]; if (!file) return;
          const fd = new FormData(); fd.append("file", file);
          const d = await CO.tryApi(API + "/upload", fd);
          if (d) { CO.$("#modal").close(); setDoc(d.doc, d.problems); }
        };
      } });
    if (!r) return;
    const path = CO.$("#op-path", r.form).value.trim();
    if (!path) return;
    const d = await CO.tryApi(API + "/open", { path });
    if (d) setDoc(d.doc, d.problems);
  }

  async function saveDoc(asNew) {
    let name = savedAs, fmt = doc.format || "eds";
    if (asNew || !name) {
      const r = await CO.modal({ title: "Save", body: `<div class="grid2"><label class="field"><span>File name</span><input id="sv-n" value="${CO.esc((doc.file_name || "device.eds").replace(/\.(eds|dcf|xdd|xdc)$/i, ""))}"></label>
        <label class="field"><span>Format</span><select id="sv-f"><option value="eds" ${fmt === "eds" ? "selected" : ""}>EDS</option><option value="dcf" ${fmt === "dcf" ? "selected" : ""}>DCF (with values)</option></select></label></div>
        <p class="small muted">Saved in ${CO.esc(meta.folder)}. Use Download for a copy somewhere else.</p>`,
        buttons: [{ label: "Cancel", value: "cancel" }, { label: "Save", value: "ok", cls: "primary" }] });
      if (!r) return false;
      name = CO.$("#sv-n", r.form).value; fmt = CO.$("#sv-f", r.form).value;
    }
    let res;
    try { res = await CO.api(API + "/save", { doc, name, format: fmt, overwrite: !!savedAs && !asNew && savedAs.toLowerCase().endsWith("." + fmt) }); }
    catch (e) {
      if (e.status !== 409) { CO.toast(e.message, "bad"); return false; }
      if (!(await CO.confirm("Replace the file?", `<p>${CO.esc(e.message)}. Replace it?</p>`, "Replace", true))) return false;
      res = await CO.tryApi(API + "/save", { doc, name, format: fmt, overwrite: true });
      if (!res) return false;
    }
    doc.format = fmt; doc.file_name = res.name; savedAs = res.name; dirty = false;
    section("FileInfo", true).FileName = res.name;
    saveDraft(); renderAll();
    CO.toast(`Saved ${res.path}`, "good");
    return true;
  }

  async function download() {
    const r = await CO.tryApi(API + "/text", { doc, format: doc.format });
    if (!r) return;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([r.text], { type: "text/plain" }));
    a.download = r.name; a.click();
  }

  async function toDecoder() {
    const r = await CO.modal({ title: "Use in the CANopen decoder", body: `<p class="small muted">Saves the file (${CO.esc(doc.file_name)}) and assigns it to a node, so PDOs and SDOs of that node are decoded.</p>
      <div class="grid2"><label class="field"><span>Channel</span>${CO.channelSelect(CO.liveChannels()[0] ? CO.liveChannels()[0].index : 0, { includeFiles: true })}</label>
      <label class="field"><span>Node id</span><input id="dc-n" type="number" min="1" max="127" value="${CO.esc(section("DeviceComissioning")?.NodeID || "")}"></label></div>`,
      buttons: [{ label: "Cancel", value: "cancel" }, { label: "Save and assign", value: "ok", cls: "primary" }],
      validate: (v, f) => (+CO.$("#dc-n", f).value >= 1 && +CO.$("#dc-n", f).value <= 127 ? "" : "Node id 1..127") });
    if (!r) return;
    const ch = CO.$(".ch-select", r.form);
    const body = { doc, name: doc.file_name, format: doc.format, node: +CO.$("#dc-n", r.form).value, channel: ch ? +ch.value : null, overwrite: !!savedAs && savedAs === doc.file_name };
    let d = null;
    try { d = await CO.api(API + "/decoder", body); }
    catch (e) {
      if (e.status !== 409) { CO.toast(e.message, "bad"); return; }
      if (!(await CO.confirm("Replace the file?", `<p>${CO.esc(e.message)} in the eds folder. Replace it?</p>`, "Replace", true))) return;
      d = await CO.tryApi(API + "/decoder", { ...body, overwrite: true });
    }
    if (d) { savedAs = d.path.split(/[\\/]/).pop(); doc.file_name = savedAs; dirty = false; saveDraft(); renderHead(); CO.toast(`Node ${CO.$("#dc-n", r.form).value} decodes with ${savedAs}`, "good"); }
  }

  // ---------------------------------------------------------------- device (read only)
  function devicePicker(extra) {
    const live = CO.liveChannels();
    if (!live.length) { CO.toast("Connect a channel first (+ Connect)", "bad"); return null; }
    return `<div class="grid2"><label class="field"><span>Channel</span>${CO.channelSelect(live[0].index)}</label>
      <label class="field"><span>Node id</span><input id="dv-n" type="number" min="1" max="127" value="${CO.esc(CO.prefs.get("eds.node", ""))}"></label></div>${extra || ""}`;
  }
  async function runJob(path, body, title) {
    let job;
    try { job = await CO.api(path, body); } catch (e) { CO.toast(e.message, "bad"); return null; }
    let finished = false;
    CO.modal({ title, body: `<div class="small" id="ee-pt">Starting&hellip;</div><progress id="ee-pb" max="1" value="0" style="width:100%;margin-top:8px"></progress>
      <p class="small muted">Only reads (SDO uploads). The same job shows in the Nodes tab.</p>`, buttons: [{ label: "Stop", value: "stop", cls: "danger" }] })
      .then(() => { if (!finished) CO.api("/api/nodes/stop", {}).catch(() => {}); });
    let j = null;
    for (;;) {
      await sleep(300);
      j = await CO.api("/api/nodes/job").catch(() => null);
      const p = await CO.api(API + "/device/progress").catch(() => null);
      const pt = CO.$("#ee-pt"), pb = CO.$("#ee-pb");
      if (p && p.job === job.id && pt) {
        pt.textContent = `${p.done} of ${p.total}` + (p.kind === "eds-probe" ? ` indices, ${p.found} object(s) found` : " objects read");
        pb.max = Math.max(1, p.total); pb.value = p.done;
      }
      if (!j || j.id !== job.id || j.state !== "running") break;
    }
    finished = true;
    const dlg = CO.$("#modal"); if (dlg.open) dlg.close();
    if (!j || j.id !== job.id) { CO.toast("The job was replaced by another node job", "bad"); return null; }
    if (j.state !== "done") { CO.toast(`${title}: ${j.error || j.state}`, "bad"); return null; }
    return CO.api(`${API}/device/result?job=${job.id}`).catch((e) => { CO.toast(e.message, "bad"); return null; });
  }

  async function compare(only) {           // only: a filter on the objects to read (the PDO editor reads the PDO objects)
    const body = devicePicker(`<p class="small muted">Reads every readable ${only ? "PDO object" : "object"} of the file from the node and shows where the node differs. Nothing is written.</p>`);
    if (!body) return;
    const r = await CO.modal({ title: "Compare with a node / read its values", body, buttons: [{ label: "Cancel", value: "cancel" }, { label: "Read", value: "ok", cls: "primary" }],
      validate: (v, f) => (+CO.$("#dv-n", f).value >= 1 && +CO.$("#dv-n", f).value <= 127 ? "" : "Node id 1..127") });
    if (!r) return;
    const node = +CO.$("#dv-n", r.form).value; CO.prefs.set("eds.node", node);
    const part = typeof only === "function" ? { ...doc, objects: doc.objects.filter(only) } : doc;
    const res = await runJob(API + "/device/compare", { doc: part, node, channel: +CO.$(".ch-select", r.form).value }, `Reading node ${node}`);
    if (res) showCompare(res);
  }

  async function showCompare(res) {
    const cls = { same: "good", differs: "warn", missing: "bad", size: "bad", error: "bad", read: "" };
    const rowsHtml = (only) => res.rows.filter((x) => !only || x.status !== "same").map((x) => `<tr><td class="m">${where(x.index, x.sub)}</td><td>${CO.esc(x.name)}</td><td class="m">${CO.esc(x.file)}</td><td class="m">${CO.esc(x.device)}</td><td><span class="badge ${cls[x.status] || ""}">${x.status}</span></td></tr>`).join("") || `<tr><td colspan="5" class="empty">No differences.</td></tr>`;
    const counts = Object.entries(res.counts || {}).map(([k, v]) => `<span class="badge ${cls[k] || ""}">${v} ${k}</span>`).join(" ");
    const r = await CO.modal({ title: `Node ${res.node} vs ${doc.file_name}`, wide: true, body: `${res.stopped ? `<div class="notice warn">Ended early (${CO.esc(res.stopped)}): the rows below are what was read.</div>` : ""}<div class="row">${counts}<span class="spacer"></span><label class="row small"><input type="checkbox" id="cp-only" checked> only differences</label></div>
      <div style="max-height:52vh;overflow:auto;margin-top:8px"><table class="tbl compact"><thead><tr><th>Object</th><th>Name</th><th>File</th><th>Node</th><th>Status</th></tr></thead><tbody id="cp-rows">${rowsHtml(true)}</tbody></table></div>
      <p class="small muted">"Into DCF values" keeps the defaults and stores what the node has as DCF values (ParameterValue); "Into defaults" changes the defaults.</p>`,
      buttons: [{ label: "Close", value: "cancel" }, { label: "Node values into defaults", value: "default" }, { label: "Node values into DCF values", value: "value", cls: "primary" }],
      onOpen(form) { CO.$("#cp-only", form).onchange = (e) => (CO.$("#cp-rows", form).innerHTML = rowsHtml(e.target.checked)); } });
    if (!r) return;
    let n = 0;
    for (const x of res.rows) {
      if (!["differs", "read"].includes(x.status)) continue;
      const v = findVar(x.index, x.sub); if (!v) continue;
      v[r.value] = x.device; n++;
    }
    if (r.value === "value") doc.format = "dcf";
    renderAll(); changed();
    CO.toast(`${n} value(s) taken from node ${res.node}` + (r.value === "value" ? " (the file is now a DCF)" : ""), "good");
  }

  async function probe() {
    if (!(await discardOk())) return;
    const body = devicePicker(`<label class="field" style="margin-top:8px"><span>Index ranges (hex, optional)</span><input id="dv-r" class="m" placeholder="empty = 1000-9FFF (a few minutes); e.g. 1000-1FFF, 2000-20FF"></label>
      <p class="small muted">Reads every index of the ranges from the node (SDO uploads, nothing is written). Standard objects get their CiA names and types; for the others the data type is guessed from the reply size and listed under Problems to check.</p>`);
    if (!body) return;
    const r = await CO.modal({ title: "Create an EDS from a node", body, buttons: [{ label: "Cancel", value: "cancel" }, { label: "Start", value: "ok", cls: "primary" }],
      validate: (v, f) => (+CO.$("#dv-n", f).value >= 1 && +CO.$("#dv-n", f).value <= 127 ? "" : "Node id 1..127") });
    if (!r) return;
    const node = +CO.$("#dv-n", r.form).value; CO.prefs.set("eds.node", node);
    const res = await runJob(API + "/device/probe", { node, channel: +CO.$(".ch-select", r.form).value, ranges: CO.$("#dv-r", r.form).value }, `Probing node ${node}`);
    if (res) { setDoc(res.doc, res.problems, true); CO.toast(`${res.doc.objects.length} object(s) from node ${node}; check the warnings`, "good"); }
  }

  async function toNodes() {
    if (!dcf()) { doc.format = "dcf"; section("DeviceComissioning", true); renderAll(); changed(); CO.toast("Switched to DCF: To Nodes writes the DCF values", "good"); }
    const nid = section("DeviceComissioning")?.NodeID || CO.prefs.get("eds.node", "");
    const withValues = doc.objects.some((o) => (o.subs || [o]).some((v) => String(v.value ?? "").trim()));
    if (!withValues) { CO.toast("No DCF values to write: set values (format DCF) or read them from a node first", "warn"); return; }
    const r = await CO.modal({ title: "Write the DCF values with the Nodes tab", body: `<p class="small muted">Makes Nodes-tab custom steps that write the DCF values (SDO). PDOs are changed the CiA 301 way (disable, mapping, enable). Nothing is sent here: the Nodes tab previews every frame and asks first.</p>
      <div class="grid2"><label class="field"><span>Node id</span><input id="tn-n" type="number" min="1" max="127" value="${CO.esc(nid)}"></label></div>
      <div style="margin-top:8px"><label class="row small"><input type="checkbox" id="tn-c" checked> only values that differ from the default</label>
      <label class="row small"><input type="checkbox" id="tn-p" checked> NMT pre-operational first</label>
      <label class="row small"><input type="checkbox" id="tn-s" checked> store (0x1010:01 "save") at the end</label>
      <label class="row small"><input type="checkbox" id="tn-r"> reset the node at the end</label></div>`,
      buttons: [{ label: "Cancel", value: "cancel" }, { label: "Make steps", value: "ok", cls: "primary" }],
      validate: (v, f) => (+CO.$("#tn-n", f).value >= 1 && +CO.$("#tn-n", f).value <= 127 ? "" : "Node id 1..127") });
    if (!r) return;
    const f = r.form;
    const d = await CO.tryApi(API + "/dcf-steps", { doc: { ...doc, format: "dcf" }, node: +CO.$("#tn-n", f).value, only_changed: CO.$("#tn-c", f).checked,
      preop: CO.$("#tn-p", f).checked, store: CO.$("#tn-s", f).checked, reset: CO.$("#tn-r", f).checked });
    if (!d) return;
    if (!d.count) { CO.toast("Nothing to write" + (d.skipped.length ? `; ${d.skipped.length} value(s) are not writable` : ""), "warn"); return; }
    if (d.skipped.length && !(await CO.confirm("Some values are skipped", `<p>${d.count} SDO write(s). Skipped:</p><ul class="small">${d.skipped.map((s) => `<li>${CO.esc(s)}</li>`).join("")}</ul>`, "Continue"))) return;
    if (!CO.openCustomSequence) { CO.toast("This CANopener has no custom sequence editor", "bad"); return; }
    await CO.openCustomSequence({ steps: d.steps, vars: d.vars, title: `DCF ${doc.file_name || ""}`.trim() });
  }

  // ---------------------------------------------------------------- right-click menus
  const varLine = (i, sv, v) => [where(i, sv), v.name, dtName(v.data_type), v.access, v.default ?? "", v.value ?? "", v.pdo ? "1" : "0"].join("\t");
  const VAR_HEAD = "Object\tName\tData type\tAccess\tDefault\tDCF value\tPDO";
  function goTo(index, sub) {
    sel = sub === undefined || sub === null ? { index } : { index, sub };
    if (findObj(index)?.subs) expanded.add(index);
    renderTree(); renderForm();
  }
  function objectMenu(o, sub) {
    const v = sub === undefined ? (o.subs ? null : o) : o.subs.find((x) => x.sub === sub);
    const lines = sub !== undefined ? [varLine(o.index, sub, v)] : o.subs ? o.subs.map((x) => varLine(o.index, x.sub, x)) : [varLine(o.index, null, o)];
    const pdo = o.index >= 0x1400 && o.index <= 0x1bff ? pdoOf(o.index) : null;
    return [
      { label: "Copy index", action: () => CO.copyText(sub === undefined ? `0x${hex4(o.index)}` : where(o.index, sub), "index") },
      { label: "Copy name", action: () => CO.copyText(v ? v.name : o.name, "name") },
      v && { label: "Copy value", disabled: !String(refValue(v) ?? "").trim(), action: () => CO.copyText(String(refValue(v)), "value") },
      { label: o.subs && sub === undefined ? "Copy the object as rows" : "Copy as a row", action: () => CO.copyText(VAR_HEAD + "\n" + lines.join("\n"), CO.rowsLabel(lines.length)) },
      { separator: true },
      pdo && { label: "Open in the PDO editor", action: () => { sel = { pdos: true, pdo }; renderTree(); renderForm(); } },
      o.subs && sub === undefined && { label: "Add a sub-index", action: () => { goTo(o.index); addSub(o); } },
      sub !== undefined && sub !== 0 && { label: "Delete this sub-index", action: () => delSub(o, sub) },
      sub === undefined && { label: "Delete object...", action: async () => {
        if (await CO.confirm("Delete object", `<p>Delete 0x${hex4(o.index)} ${CO.esc(o.name)}?</p>`, "Delete", true)) {
          doc.objects = doc.objects.filter((x) => x !== o); sel = { info: true }; renderAll(); changed();
        } } },
    ];
  }
  function treeMenu(ev) {
    const n = ev.target.closest(".ee-node");
    if (!doc || !n || n.dataset.info || n.dataset.pdos) return doc ? [{ label: "Add an object...", action: addObject }] : null;
    const o = findObj(+n.dataset.i);
    return o ? objectMenu(o, n.dataset.s !== undefined ? +n.dataset.s : undefined) : null;
  }
  function probMenu(ev) {
    const d = ev.target.closest("[data-p]"), p = d && problems[+d.dataset.p];
    if (!p) return problems.length ? [{ label: "Copy all problems", action: () => CO.copyText(problems.map((x) => `${x.level}\t${where(x.index, x.sub)}\t${x.text}`).join("\n"), `${problems.length} problems`) }] : null;
    return [
      p.index !== null && findObj(p.index) && { label: "Go to the object", action: () => goTo(p.index, p.sub !== null && findVar(p.index, p.sub) && findObj(p.index).subs ? p.sub : undefined) },
      { label: "Copy problem", action: () => CO.copyText(`${p.level}\t${where(p.index, p.sub)}\t${p.text}`, "problem") },
      { label: "Copy all problems", action: () => CO.copyText(problems.map((x) => `${x.level}\t${where(x.index, x.sub)}\t${x.text}`).join("\n"), `${problems.length} problems`) },
    ];
  }
  function formMenu(ev) {
    if (!doc) return null;
    const subRow = ev.target.closest("tr[data-sub]"), pdoRow = ev.target.closest("tr[data-pdo-sel]"), s = selected();
    if (subRow && s) return objectMenu(s.o, +subRow.dataset.sub);
    if (pdoRow) {
      const [kind, num] = pdoRow.dataset.pdoSel.split(":"), p = pdoList().find((x) => x.kind === kind && x.n === +num);
      if (!p) return null;
      const cob = p.c ? pdoVal(p.c, 1) : "";
      return [
        { label: `Edit ${p.label}`, action: () => { sel = { pdos: true, pdo: { kind, n: +num } }; renderForm(); } },
        { label: "Copy COB-ID", disabled: !cob, action: () => CO.copyText(cob, "COB-ID") },
        { label: "Copy the mapping", disabled: !p.m, action: () => CO.copyText(mapEntries(p.m).map((e) => "0x" + hex8(e)).join("\n"), "mapping") },
        { separator: true },
        { label: `Delete ${p.label}...`, action: () => { sel = { pdos: true, pdo: { kind, n: +num } }; pdoDel(); } },
      ];
    }
    return null;
  }

  // ---------------------------------------------------------------- tab
  CO.registerTab({
    id: "eds", title: "EDS editor", order: 55,
    render(e) {
      el = e;
      if (!document.getElementById("ee-style")) { const st = document.createElement("style"); st.id = "ee-style"; st.textContent = STYLE; document.head.appendChild(st); }
      el.innerHTML = `<div class="pane" style="flex:1;height:100%">
        <div class="toolbar"><button class="btn small" data-act="new">New&hellip;</button><button class="btn small" data-act="open">Open&hellip;</button>
          <button class="btn small" data-act="save" data-need-doc>Save</button><button class="btn small" data-act="saveas" data-need-doc>Save as&hellip;</button><button class="btn small" data-act="download" data-need-doc>Download</button>
          <select id="ee-format" class="small" title="EDS describes the device; DCF also holds the values for one node" data-need-doc><option value="eds">EDS</option><option value="dcf">DCF</option></select>
          <span class="sep"></span><button class="btn small" data-act="decoder" data-need-doc title="Decode a node with this file">To decoder&hellip;</button>
          <button class="btn small" data-act="compare" data-need-doc title="Read the node and compare (reads only)">Compare with node&hellip;</button>
          <button class="btn small" data-act="probe" title="Build an EDS from what a node answers (reads only)">Create from node&hellip;</button>
          <button class="btn small" data-act="tonodes" data-need-doc title="Write the DCF values with the Nodes tab (preview and confirm there)">To Nodes&hellip;</button>
          <span class="spacer"></span><span id="ee-file"></span></div>
        <div class="ee-main"><div class="ee-tree"><div class="row" style="padding:6px 8px"><input id="ee-filter" placeholder="Filter: index or name" style="flex:1;min-width:0"><button class="btn small" data-act="addobj" data-need-doc title="Add an object">+</button></div><div class="list" id="ee-list"></div></div>
          <div class="ee-form" id="ee-form"></div></div>
        <div class="pane-head" id="ee-probs-head">Problems</div><div class="ee-probs" id="ee-probs"></div></div>`;
      el.addEventListener("click", (ev) => {
        const b = ev.target.closest("[data-act]");
        if (b && !b.disabled) {
          const act = b.dataset.act, s = selected();
          ({ new: newDoc, open: openDoc, save: () => saveDoc(false), saveas: () => saveDoc(true), download, decoder: toDecoder, compare, probe, tonodes: toNodes, addobj: addObject,
            addsub: () => s && addSub(s.o), delsub: () => s && delSub(s.o, +b.dataset.sub),
            pdoadd: () => pdoAdd(b.dataset.kind), pdodel: pdoDel, pdoparam: () => { const p = currentPdo(); if (p && p.c) addPdoParam(p.c, +b.dataset.sub); }, pdoread: () => compare((o) => o.index >= 0x1400 && o.index <= 0x1bff),
            pdoraw: () => {
              const p = currentPdo(), inp = CO.$("#ee-pdo-raw", el), v = strictHex(inp.value || ""), err = entryError(v);
              if (!p || !p.m) return;
              if (err) { CO.toast(err, "bad"); return; }
              mapAdd(p.m, v);
            },
            topdo: () => { if (s) { sel = { pdos: true, pdo: pdoOf(s.o.index) }; renderTree(); renderForm(); } },
            delobj: async () => { if (s && await CO.confirm("Delete object", `<p>Delete 0x${hex4(s.o.index)} ${CO.esc(s.o.name)}?</p>`, "Delete", true)) { doc.objects = doc.objects.filter((o) => o !== s.o); sel = { info: true }; renderAll(); changed(); } },
          }[act] || (() => {}))();
          return;
        }
        const tw = ev.target.closest("[data-tw]");
        if (tw && tw.textContent) { const i = +tw.dataset.tw; expanded.has(i) ? expanded.delete(i) : expanded.add(i); renderTree(); return; }
        const n = ev.target.closest(".ee-node");
        const pr = ev.target.closest("[data-pdo-sel]");
        if (pr) { const [kind, num] = pr.dataset.pdoSel.split(":"); sel = { pdos: true, pdo: { kind, n: +num } }; renderForm(); return; }
        if (n) { sel = n.dataset.info ? { info: true } : n.dataset.pdos ? { pdos: true, pdo: sel.pdo } : n.dataset.s !== undefined ? { index: +n.dataset.i, sub: +n.dataset.s } : { index: +n.dataset.i }; if (!sel.info && !sel.pdos && findObj(sel.index)?.subs) expanded.add(sel.index); renderTree(); renderForm(); return; }
        const p = ev.target.closest("[data-p]");
        if (p) { const pr = problems[+p.dataset.p]; if (pr.index === null) sel = { info: true }; else if (findObj(pr.index)) { sel = pr.sub === null || !findVar(pr.index, pr.sub) || !findObj(pr.index).subs ? { index: pr.index } : { index: pr.index, sub: pr.sub }; expanded.add(pr.index); } renderTree(); renderForm(); return; }
        const go = ev.target.closest("[data-go]");
        if (go) { ev.preventDefault(); sel = { index: sel.index, sub: +go.dataset.go }; renderTree(); renderForm(); return; }
        const mu = ev.target.closest("[data-map-up]"), mdn = ev.target.closest("[data-map-dn]"), md = ev.target.closest("[data-map-del]");
        const mo = sel.pdos ? currentPdo()?.m : selected()?.o;          // the mapping object shown
        if ((mu || mdn || md) && mo) {
          const e = mapEntries(mo);
          if (mu) { const i = +mu.dataset.mapUp - 1; [e[i - 1], e[i]] = [e[i], e[i - 1]]; }
          else if (mdn) { const i = +mdn.dataset.mapDn - 1; [e[i], e[i + 1]] = [e[i + 1], e[i]]; }
          else e.splice(+md.dataset.mapDel - 1, 1);
          mapStore(mo, e);
        }
      });
      const form = CO.$("#ee-form", el);
      if (CO.onContextMenu) {
        CO.onContextMenu(CO.$("#ee-list", el), treeMenu);
        CO.onContextMenu(CO.$("#ee-probs", el), probMenu);
        CO.onContextMenu(form, formMenu);
      }
      form.addEventListener("input", onFormInput);
      form.addEventListener("change", (ev) => {
        onFormChange(ev);
        const s = selected();          // the mapping view shows the entries: redraw it once an edit is done
        if (s && isMapping(s.o.index) && (ev.target.dataset.sf || ["default", "value"].includes(ev.target.dataset.f))) renderForm();
      });
      CO.$("#ee-filter", el).oninput = (ev) => { filter = ev.target.value.trim(); renderTree(); };
      CO.$("#ee-format", el).onchange = (ev) => { if (!doc) return; doc.format = ev.target.value; if (doc.format === "dcf") section("DeviceComissioning", true); renderForm(); changed(); };
      const draft = CO.prefs.get("eds.draft", null);
      if (draft && draft.doc) { doc = draft.doc; savedAs = draft.savedAs || ""; dirty = !!draft.dirty; }
      loadMeta().then(() => { renderAll(); if (doc) recheck_(); }).catch((e2) => CO.toast("EDS editor: " + e2.message, "bad"));
    },
  });
})();
