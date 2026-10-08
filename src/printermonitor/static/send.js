"use strict";
// "Send a file" dialog: 3D preview, model slicing, sending to one or more printers.

const send = { file: null, token: null, viewer: null, sliceJob: null, sendJob: null, catalogs: new Map(), prices: null };
const MODEL_EXTS = ["stl", "3mf", "obj", "step", "stp"];
const extOf = (name) => name.split(".").pop().toLowerCase();

window.openSend = function openSend(file = null, preselect = []) {
  resetSend();
  renderSendPrinters(preselect);
  $("#send-dlg").showModal();
  if (file) handleSendFile(file);
};

function resetSend() {
  send.viewer?.dispose();
  Object.assign(send, { file: null, token: null, viewer: null, sliceJob: null, sendJob: null });
  $("#send-viewer").innerHTML = `<div class="viewer-empty">
    <label class="drop big" id="send-drop">
      <input type="file" id="send-file" accept=".gcode,.stl,.3mf,.obj,.step,.stp" hidden>
      <span class="drop-title">Drop an .stl, .3mf or .gcode</span>
      <span class="drop-sub">or click to choose a file</span>
    </label></div>`;
  $("#send-meta").innerHTML = "";
  $("#send-slice").hidden = true;
  $("#send-status").innerHTML = "";
  $("#send-go").hidden = false;
  $("#send-reset").hidden = true;
  bindDrop();
  updateSendButton();
}

function bindDrop() {
  const drop = $("#send-drop");
  if (!drop) return;
  $("#send-file").onchange = (e) => e.target.files[0] && handleSendFile(e.target.files[0]);
  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("hover"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("hover"); }));
  drop.addEventListener("drop", (e) => e.dataTransfer.files[0] && handleSendFile(e.dataTransfer.files[0]));
}

function renderSendPrinters(preselect = []) {
  const start = $("#send-start").checked;
  const list = [...printers.values()];
  const free = list.filter((p) => p.online && !BUSY.has(p.state));
  const selected = new Set(preselect.length ? preselect : free.length === 1 ? [free[0].id] : []);
  $("#send-printers").innerHTML = list.map((p) => {
    const busy = BUSY.has(p.state);
    const disabled = !p.online || (start && busy);
    const why = !p.online ? "offline" : busy ? `${STATE_LABEL[p.state].toLowerCase()}${p.data?.printLeftTime ? ", available in " + dur(p.data.printLeftTime) : ""}` : STATE_LABEL[p.state].toLowerCase();
    return `<label class="sp ${disabled ? "off" : ""}">
      <input type="checkbox" value="${esc(p.id)}" ${selected.has(p.id) && !disabled ? "checked" : ""} ${disabled ? "disabled" : ""}>
      <img src="${modelUrl(p)}" alt="" onerror="this.style.visibility='hidden'">
      <span><b>${esc(p.name)}</b><small>${esc(why)}</small></span>
    </label>`;
  }).join("") || `<p class="muted small">No printers.</p>`;
  updateSendButton();
}

function selectedPrinters() {
  return $$("#send-printers input:checked").map((i) => i.value);
}

function updateSendButton() {
  const n = selectedPrinters().length;
  const ready = !!(send.token || (send.file && extOf(send.file.name) === "gcode"));
  const btn = $("#send-go");
  btn.disabled = !ready || !n || !!send.sendJob;
  btn.textContent = !ready ? (send.file ? "Slice the model first" : "Choose a file")
    : !n ? "Choose a printer"
    : `${$("#send-start").checked ? "Print" : "Send"} to ${n} printer${n > 1 ? "s" : ""}`;
}

async function viewerReady() {
  for (let i = 0; i < 50 && !window.PMViewer; i++) await new Promise((r) => setTimeout(r, 100));
  if (!window.PMViewer) throw new Error("3D preview unavailable");
  const box = $("#send-viewer");
  box.innerHTML = "";
  send.viewer?.dispose();
  send.viewer = new window.PMViewer(box, {});
  return send.viewer;
}

async function handleSendFile(file) {
  const ext = extOf(file.name);
  if (ext !== "gcode" && !MODEL_EXTS.includes(ext)) { toast("Supported formats: .stl, .3mf, .obj, .step, .gcode", true); return; }
  send.file = file;
  send.token = null;
  $("#send-meta").innerHTML = `<span class="muted">${esc(file.name)} · ${(file.size / 1048576).toFixed(1)} MB</span>`;
  try {
    if (ext === "gcode") {
      $("#send-slice").hidden = true;
      const v = await viewerReady();
      await v.loadGcode(file);
      const text = await file.slice(Math.max(0, file.size - 300_000)).text() + await file.slice(0, 65536).text();
      showGcodeMeta(parseMeta(text), v.info);
    } else {
      $("#send-slice").hidden = false;
      loadCatalogs();
      if (ext === "stl") {
        const v = await viewerReady();
        await v.loadSTL(file);
        const s = v.info?.size;
        $("#send-meta").innerHTML = `<span>${esc(file.name)}</span>${s ? ` · ${s.x.toFixed(1)} × ${s.y.toFixed(1)} × ${s.z.toFixed(1)} mm` : ""}${v.info?.fitsBed === false ? ` · <span class="perr inline-err">does not fit on the bed</span>` : ""}`;
      } else {
        $("#send-viewer").innerHTML = `<div class="viewer-empty"><p class="muted">No preview before slicing for .${ext} files: slice it to see the result.</p></div>`;
      }
    }
  } catch (e) {
    toast("Preview failed: " + e.message, true);
  }
  updateSendButton();
}

function parseMeta(text) {
  const find = (re) => { const m = text.match(re); return m ? m[1].trim() : null; };
  const g = find(/^;\s*(?:total )?filament used \[g\]\s*[=:]\s*([\d.]+)/mi);
  const t = find(/^;\s*estimated printing time(?: \(normal mode\))?\s*[=:]\s*([^\n]+)/mi);
  let secs = 0;
  for (const [, v, u] of (t || "").matchAll(/(\d+)\s*([dhms])/g)) secs += Number(v) * { d: 86400, h: 3600, m: 60, s: 1 }[u];
  return { filament_g: g ? Number(g) : null, est_time_s: secs || null, material: find(/^;\s*filament_type\s*[=:]\s*([^\n;]+)/mi) };
}

async function showGcodeMeta(meta, info) {
  if (!send.prices) { try { send.prices = (await api("/api/settings")).filaments; } catch { send.prices = {}; } }
  const fam = Object.keys(send.prices).sort((a, b) => b.length - a.length).find((k) => (meta.material || "PLA").toUpperCase().includes(k)) || "PLA";
  const price = send.prices[fam]?.price_kg;
  const cost = meta.filament_g && price ? meta.filament_g * price / 1000 : null;
  $("#send-meta").innerHTML = [
    `<span>${esc(send.file?.name || "")}</span>`,
    meta.est_time_s && `⏱ ${dur(meta.est_time_s)}`,
    meta.filament_g && `${meta.filament_g.toFixed(1)} g${meta.material ? " of " + esc(meta.material) : ""}`,
    cost && `≈ ${cost.toLocaleString(undefined, { style: "currency", currency: "EUR" })} of filament`,
    info?.layers && `${info.layers} layers`,
  ].filter(Boolean).join(" · ");
}

// ---------- slicing ----------
async function loadCatalogs() {
  const models = [...new Set([...printers.values()].map((p) => p.model).filter(Boolean))];
  if (!models.length) models.push("Creality Hi");
  const sel = $("#send-model");
  sel.innerHTML = models.map((m) => `<option>${esc(m)}</option>`).join("");
  const firstSel = printers.get(selectedPrinters()[0]);
  if (firstSel?.model) sel.value = firstSel.model;
  await fillCatalog();
}

async function fillCatalog() {
  const model = $("#send-model").value;
  let st = send.catalogs.get(model);
  if (!st) {
    try { st = await api(`/api/slicer?model=${encodeURIComponent(model)}`); } catch (e) { toast(e.message, true); return; }
    if (st.available) send.catalogs.set(model, st);
  }
  const missing = $("#send-slice-missing");
  const c = st.catalog;
  if (!st.available || !c?.machine) {
    missing.hidden = false;
    missing.innerHTML = !st.available
      ? `OrcaSlicer is not installed. <a href="#settings" onclick="document.querySelector('#send-dlg').close()">${st.can_install === false ? "See Settings" : "Install it from Settings"}</a>, or send a .gcode.`
      : `No OrcaSlicer profiles for "${esc(model)}".`;
    $("#send-slice-btn").disabled = true;
    $("#send-process").innerHTML = $("#send-filament").innerHTML = "";
    return;
  }
  missing.hidden = true;
  $("#send-slice-btn").disabled = false;
  const remember = (k, v) => { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } };
  const recall = (k) => { try { return localStorage.getItem(k); } catch { return null; } };
  $("#send-process").innerHTML = c.processes.map((p) => `<option>${esc(p)}</option>`).join("");
  $("#send-filament").innerHTML = c.filaments.map((f) => `<option>${esc(f)}</option>`).join("");
  $("#send-process").value = recall("pm.process") || c.processes.find((p) => p.startsWith("0.20mm Standard")) || c.processes[0];
  if (!$("#send-process").value) $("#send-process").value = c.processes[0];
  // default filament: the one loaded in the selected printer, otherwise the last one used
  const loaded = filaments(printers.get(selectedPrinters()[0])?.data || {})[0];
  const guess = loaded && c.filaments.find((f) => f.toUpperCase().startsWith(`CREALITY GENERIC ${String(loaded.type).toUpperCase()} `));
  $("#send-filament").value = guess || recall("pm.filament") || c.filaments.find((f) => f.startsWith("Creality Generic PLA @")) || c.filaments[0];
  if (!$("#send-filament").value) $("#send-filament").value = c.filaments[0];
  $("#send-process").onchange = () => remember("pm.process", $("#send-process").value);
  $("#send-filament").onchange = () => remember("pm.filament", $("#send-filament").value);
}
$("#send-model").onchange = fillCatalog;

$("#send-slice-btn").onclick = async () => {
  if (!send.file) return;
  const btn = $("#send-slice-btn");
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span> Slicing…`;
  const fd = new FormData();
  fd.append("file", send.file);
  fd.append("machine", send.catalogs.get($("#send-model").value)?.catalog?.machine || "");
  fd.append("process", $("#send-process").value);
  fd.append("filament", $("#send-filament").value);
  try {
    // Thumbnails are rendered here: OrcaSlicer's headless slicing doesn't produce any.
    // Optional: if rendering fails or takes too long, slice without them.
    if (send.viewer && extOf(send.file.name) === "stl") {
      const within = (p, ms) => Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), ms))]);
      try {
        fd.append("thumb300", await within(send.viewer.snapshot(300), 5000));
        fd.append("thumb96", await within(send.viewer.snapshot(96), 5000));
      } catch (err) { console.warn("thumbnail not generated:", err.message); }
    }
    send.sliceJob = (await api("/api/slice", { method: "POST", body: fd })).id;
  } catch (e) {
    toast(e.message, true);
    btn.disabled = false;
    btn.textContent = "Slice";
  }
};

window.addEventListener("pm:job", async (e) => {
  const job = e.detail;
  if (job.id === send.sliceJob && ["done", "error"].includes(job.step)) {
    send.sliceJob = null;
    const btn = $("#send-slice-btn");
    btn.disabled = false;
    btn.textContent = "Re-slice";
    if (job.step === "error") { toast(job.error, true); return; }
    send.token = job.token;
    toast("Slicing complete ✓");
    try {
      const v = await viewerReady();
      await v.loadGcode(`/api/sliced/${job.token}`);
      showGcodeMeta(job.meta || {}, v.info);
    } catch (err) { toast("Preview failed: " + err.message, true); }
    updateSendButton();
  }
  if (job.id === send.sendJob) renderSendStatus(job);
});

// ---------- sending ----------
const SEND_STEP = { queued: "Queued", uploading: "Uploading…", starting: "Starting…", done: "OK", error: "Failed" };

function renderSendStatus(job) {
  $("#send-status").innerHTML = Object.values(job.targets || {}).map((t) => `
    <div class="jobrow" data-step="${esc(t.step)}">
      ${["done", "error"].includes(t.step) ? "" : `<span class="spinner"></span>`}
      <span class="fname">${esc(t.name)}</span><span class="step">${SEND_STEP[t.step] || esc(t.step)}</span>
      ${t.error ? `<pre>${esc(t.error)}</pre>` : ""}
    </div>`).join("");
  if (["done", "error"].includes(job.step)) {
    send.sendJob = null;
    $("#send-go").hidden = true;
    $("#send-reset").hidden = false;
    if (job.step === "done") toast(job.start ? "Print started ✓" : "File sent ✓");
  }
}

$("#send-go").onclick = async () => {
  const fd = new FormData();
  fd.append("printers", selectedPrinters().join(","));
  fd.append("start", $("#send-start").checked ? "true" : "false");
  if (send.token) fd.append("token", send.token);
  else fd.append("file", send.file);
  const ids = selectedPrinters();
  if ($("#send-start").checked && !confirm(`Start printing on ${ids.map((id) => printers.get(id)?.name).join(", ")}?\nThe bed must be clear.`)) return;
  try {
    const job = await api("/api/send", { method: "POST", body: fd });
    send.sendJob = job.id;
    renderSendStatus(job);
    updateSendButton();
  } catch (e) { toast(e.message, true); }
};

$("#send-printers").addEventListener("change", updateSendButton);
$("#send-start").onchange = () => renderSendPrinters(selectedPrinters());
$("#send-reset").onclick = () => { const keep = selectedPrinters(); resetSend(); renderSendPrinters(keep); };
$("#send-close").onclick = () => $("#send-dlg").close();
$("#send-dlg").addEventListener("close", () => { send.viewer?.dispose(); send.viewer = null; });
$("#send-btn").onclick = () => window.openSend(null, current ? [current] : []);
