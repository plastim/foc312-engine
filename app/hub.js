// Hub page: boxes, firmware, the remote, hotkeys. Talks to the hub server on this origin (/api/...). No libraries.
"use strict";

const $ = (id) => document.getElementById(id);
const POLL_MS = 3000, JOB_MS = 500;
const ACTION_NAMES = { stop: "STOP", level_up: "Both levels up", level_down: "Both levels down", a_up: "Level A up",
  a_down: "Level A down", b_up: "Level B up", b_down: "Level B down" };

let devices = [], engine = null, firmware = { box: [], remote: [] };
const jobs = {};                  // panel prefix -> {id, timer, state}

// ---------------------------------------------------------------- helpers
function showError(msg) {
  const e = $("error");
  if (msg) { e.textContent = msg; e.hidden = false; } else { e.hidden = true; }
}
async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  let r;
  try { r = await fetch(path, opts); } catch (err) { return { ok: false, status: 0, data: { error: "the hub server is not answering" } }; }
  let data = {};
  try { data = await r.json(); } catch { data = {}; }
  return { ok: r.ok, status: r.status, data };
}
function h(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v === true) n.setAttribute(k, "");
    else if (v !== false && v != null) n.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k != null) n.append(k.nodeType ? k : String(k));
  return n;
}
const KIND_NAME = { box: "FOC-Stim", remote: "M5 remote", unknown: "unknown" };
function devName(d) { return d.name || KIND_NAME[d.kind] || d.kind; }
function devLabel(d) {
  const fw = d.fw_label ? " · " + d.fw_label : d.kind === "unknown" ? " (press Detect)" : "";
  return `${d.port} — ${KIND_NAME[d.kind] || d.kind}${d.name && d.kind !== "remote" ? " " + d.name : ""}${fw}`;
}
// the firmware as a tag: PlaStim v7+ is ready; older fork or stock needs a flash
function fwTag(d) {
  if (d.kind === "remote") {
    const free = /free (\d+)/.exec(d.detail || "");
    if (free) return h("span", { class: "tag ok" }, `ready · ${(free[1] / 1048576).toFixed(1)} MB free for patterns`);
    if (/busy/.test(d.detail || "")) return h("span", { class: "tag warn" }, "running a session (stop it to load or flash)");
    return h("span", { class: "muted" }, d.detail || "");
  }
  if (d.fw_fork == null) return h("span", { class: "muted" }, d.kind === "unknown" ? "press Detect" : d.detail || "");
  if (d.fw_fork >= 7) return h("span", { class: "tag ok", title: d.detail }, d.fw_label + " \u2713");
  if (d.fw_fork >= 1) return h("span", { class: "tag warn", title: d.detail }, d.fw_label + " - update to v7+");
  return h("span", { class: "tag warn", title: d.detail }, d.fw_label + " - flash the PlaStim firmware");
}
function fillSelect(sel, items, label, value, empty) {
  if (document.activeElement === sel) return;      // never rebuild a list the user has open
  const keep = sel.value;
  sel.innerHTML = "";
  if (!items.length) { sel.append(h("option", { value: "" }, empty)); sel.disabled = true; return; }
  sel.disabled = false;
  for (const it of items) sel.append(h("option", { value: value(it), disabled: !!it.error }, label(it)));
  if ([...sel.options].some((o) => o.value === keep && !o.disabled)) sel.value = keep;
  else { const first = [...sel.options].find((o) => !o.disabled); if (first) sel.value = first.value; }
}

// ---------------------------------------------------------------- tabs
function showTab(name) {
  document.querySelectorAll('[role="tab"]').forEach((b) => {
    const on = b.dataset.tab === name;
    b.setAttribute("aria-selected", String(on));
    $("panel-" + b.dataset.tab).hidden = !on;
  });
  if (name === "settings") loadHotkeys();
  if (name === "boxes" || name === "remote") loadFirmware();
  if (name === "remote") { loadM5Settings(); loadPatterns(); loadEt312(); }
}
document.querySelectorAll('[role="tab"]').forEach((b) => b.addEventListener("click", () => {
  history.replaceState(null, "", "#" + b.dataset.tab);
  showTab(b.dataset.tab);
}));

// ---------------------------------------------------------------- engine
async function refreshEngine() {
  const r = await api("/api/engine");
  if (!r.ok) { showError(r.data.error || "engine status unavailable"); return; }
  engine = r.data;
  const run = !!engine.running;
  const pill = $("enginePill");
  pill.textContent = run ? `engine on ${engine.port || "?"}`
    : engine.reconnecting ? "waiting for the box: power-cycle it" : "engine off";
  pill.title = engine.reconnecting ? (engine.reconnect_note || "") : "";
  pill.className = "pill " + (run ? "on" : "off");
  $("hubStop").disabled = !run;
  const link = $("openPlayer");
  if (engine.foc312_url) link.href = engine.foc312_url;
  link.classList.toggle("disabled", !run);
  $("playStatus").textContent = run
    ? `The engine is driving the box on ${engine.port}${engine.pid ? " (process " + engine.pid + ")" : ""}. Open the player to play.`
    : "The engine is not running. Connect it to a box below or on the Boxes tab.";
  $("engineWhere").textContent = run ? `· ${engine.port}` : "";
  renderDevices();                                    // the engine log itself: refreshStatus (filtered)
}
async function connectEngine(port) {
  const r = await api("/api/engine/connect", { port });
  if (!r.ok) showError(r.data.error || "could not start the engine"); else showError(null);
  refreshEngine();
}
async function disconnectEngine() {
  const r = await api("/api/engine/disconnect", {});
  if (!r.ok) showError(r.data.error || "could not stop the engine"); else showError(null);
  refreshEngine();
}
// STOP from here: the player's own command, sent straight to the engine's player port. A plain-text POST is a
// "simple" request, so the browser sends it without a CORS preflight (the reply is not readable, and not needed).
$("hubStop").addEventListener("click", () => {
  const base = (engine && engine.foc312_url) || "http://127.0.0.1:8322/";
  fetch(base.replace(/\/?$/, "/") + "cmd", { method: "POST", mode: "no-cors", body: JSON.stringify({ cmd: "stop" }) })
    .catch(() => showError("STOP could not reach the engine: use the player's STOP or the box knob"));
});

// ---------------------------------------------------------------- devices
async function refreshDevices(probe = false) {
  const r = await api("/api/devices" + (probe ? "?probe=1" : ""));
  if (!r.ok) { showError(r.data.error || "device list unavailable"); return; }
  devices = r.data.devices || [];
  renderDevices();
}
function engineOn(port) { return !!(engine && engine.running && engine.port === port); }
function renderDevices() {
  const rows = $("deviceRows");
  rows.innerHTML = "";
  if (!devices.length) {
    rows.append(h("tr", {}, h("td", { colspan: 5, class: "muted" }, "No box or remote on USB. Plug one in (a data cable, not charge-only).")));
  }
  const running = !!(engine && engine.running);
  for (const d of devices) {
    const acts = [];
    if (d.kind !== "remote") {
      if (engineOn(d.port)) acts.push(h("button", { onclick: disconnectEngine }, "Disconnect"));
      else acts.push(h("button", { class: "primary", disabled: running || d.in_use,
        title: running ? "the engine is driving another box: disconnect it first" : "start the engine on this box",
        onclick: () => connectEngine(d.port) }, "Connect engine"));
      acts.push(h("button", { disabled: d.in_use || engineOn(d.port), title: "choose this box in the firmware panel",
        onclick: () => { $("boxPort").value = d.port; showTab("boxes"); $("boxFlashCard").scrollIntoView({ behavior: "smooth" }); } },
        "Firmware…"));
    } else {
      acts.push(h("button", { onclick: () => { $("remotePort").value = d.port; history.replaceState(null, "", "#remote"); showTab("remote"); } }, "M5 remote…"));
    }
    const status = engineOn(d.port) ? h("span", { class: "tag use" }, "engine")
      : d.in_use ? h("span", { class: "tag warn", title: "another program has this port open" }, "in use") : h("span", { class: "muted" }, "free");
    rows.append(h("tr", {},
      h("td", {}, h("span", { class: "badge " + d.kind }, KIND_NAME[d.kind] || d.kind),
        d.name && d.kind !== "remote" ? h("span", { class: "devname" }, d.name) : null),
      h("td", {}, [fwTag(d), d.serial ? h("div", { class: "mono muted" }, d.serial) : null]),
      h("td", { class: "mono" }, d.port),
      h("td", {}, status),
      h("td", { class: "act" }, acts)));
  }
  updateSteps(running);
  // selects
  const boxish = devices.filter((d) => d.kind === "box" || d.kind === "unknown");
  fillSelect($("boxPort"), boxish, devLabel, (d) => d.port, "no box on USB");
  fillSelect($("pairPort"), boxish, devLabel, (d) => d.port, "no box on USB");
  // only a device detected as the M5 remote: loading or flashing the remote on a box's port is refused by the hub
  fillSelect($("remotePort"), devices.filter((d) => d.kind === "remote"), devLabel, (d) => d.port,
    devices.some((d) => d.kind === "unknown") ? "no M5 remote detected yet: press Detect" : "no M5 remote on USB");
  updateBoxFlash();
  updateRemoteButtons();
}
// Detect: ask each free device what it is (the answer is remembered by the hub); both buttons share it
async function detect() {
  for (const id of ["identify", "detectHere"]) { $(id).disabled = true; $(id).textContent = "Detecting…"; }
  await refreshDevices(true);
  for (const id of ["identify", "detectHere"]) { $(id).disabled = false; $(id).textContent = "Detect"; }
}
$("identify").addEventListener("click", detect);
$("detectHere").addEventListener("click", detect);
document.querySelectorAll("[data-goto]").forEach((a) => a.addEventListener("click", (ev) => {
  ev.preventDefault();
  history.replaceState(null, "", "#" + a.dataset.goto);
  showTab(a.dataset.goto);
}));

// ---------------------------------------------------------------- getting started (Play tab)
function step(id, done, todo) {
  $(id).classList.toggle("done", !!done);
  $(id).classList.toggle("todo", !done && !!todo);
}
function updateSteps(running) {
  const boxes = devices.filter((d) => d.kind === "box");
  const unknown = devices.filter((d) => d.kind === "unknown");
  const s1 = $("stepPlugState");
  s1.innerHTML = "";
  if (boxes.length) boxes.forEach((d) => s1.append(h("span", { class: "tag ok" }, `${devName(d)} on ${d.port}`), " "));
  else if (unknown.length) s1.append(`Found ${unknown.length} USB device${unknown.length > 1 ? "s" : ""} not identified yet: press Detect.`);
  else s1.append("Nothing found yet.");
  step("stepPlug", boxes.length, true);
  const s2 = $("stepFwState");
  s2.innerHTML = "";
  for (const d of boxes) s2.append(h("span", {}, devName(d) + ": "), fwTag(d), " ");
  const ready = boxes.filter((d) => (d.fw_fork || 0) >= 7);
  step("stepFw", ready.length, boxes.length);
  const q = $("quickBoxes");
  q.innerHTML = "";
  if (running) q.append(h("span", { class: "tag ok" }, `Connected to ${engine.port}`), " ",
    h("button", { onclick: disconnectEngine }, "Disconnect"));
  else if (!boxes.length && !unknown.length) q.append("Plug a box in first.");
  else for (const d of [...boxes, ...unknown]) q.append(h("button", { class: "primary", disabled: d.in_use,
    onclick: () => connectEngine(d.port) }, `Connect ${devName(d)} (${d.port})`), " ");
  step("stepConnect", running, boxes.length);
  $("openPlayer2").classList.toggle("disabled", !running);
  if (engine && engine.foc312_url) $("openPlayer2").href = engine.foc312_url;
  step("stepPlay", false, running);
}

// ---------------------------------------------------------------- firmware images
async function loadFirmware() {
  const r = await api("/api/firmware");
  if (!r.ok) { showError(r.data.error || "firmware list unavailable"); return; }
  firmware = { box: r.data.box || [], remote: r.data.remote || [] };
  const order = (a, b) => (b.recommended ? 1 : 0) - (a.recommended ? 1 : 0);
  const src = (i) => i.source === "release" ? " [PlaStim release \u2713]" : i.source === "stock" ? " [stock, diglet48]"
    : i.source === "local" ? " [local build]" : "";
  const lbl = (i) => `${i.name}${i.version ? " " + i.version : ""}${i.recommended ? " (recommended)" : ""}${src(i)}${i.error ? " — " + i.error : ""}`;
  fillSelect($("boxImage"), firmware.box.slice().sort(order), lbl, (i) => i.id, "no box images found");
  fillSelect($("remoteImage"), firmware.remote.slice().sort(order), lbl, (i) => i.id, "no remote images found");
  imageInfo("box"); imageInfo("remote");
  updateBoxFlash(); updateRemoteButtons();
}
function image(kind) { return firmware[kind].find((i) => i.id === $(kind + "Image").value); }
function imageInfo(kind) {
  const i = image(kind), box = $(kind + "ImageInfo");
  box.innerHTML = "";
  if (!i) return;
  box.append(h("div", {}, h("b", {}, i.name), i.version ? ` · ${i.version}` : "", i.file ? ` · ${i.file}` : ""),
    i.sha256 ? h("div", { class: "mono" }, "SHA-256 " + i.sha256) : null,
    i.notes ? h("div", {}, i.notes) : null,
    i.source === "release" ? h("div", { class: "tag ok" }, "signed by PlaStim \u2713") : null,
    i.source === "stock" ? h("div", { class: "muted" }, "the original FOC-Stim firmware (diglet48), a known build") : null);
}

// ---------------------------------------------------------------- firmware updates (GitHub releases, signed)
// Never automatic: "Check" asks GitHub, "Download" fetches and verifies one release; flashing stays the step above.
const upState = { box: null, remote: null };
function fmtTime(t) { return t ? new Date(t * 1000).toLocaleString() : "not checked yet"; }
function renderUpdates(kind) {
  const r = upState[kind], list = $(kind + "UpList");
  list.innerHTML = "";
  $(kind + "UpChecked").textContent = r ? "last checked " + fmtTime(r.checked_at) : "not checked yet";
  if (!r) return;
  if (r.error) list.append(h("div", { class: "muted" }, r.error));
  const rels = r.releases || [];
  const older = $(kind + "UpOlder").checked;
  $(kind + "UpOlderRow").hidden = rels.length < 2;
  rels.forEach((rel, n) => {
    if (n > 0 && !older) return;
    const dl = h("button", { class: n === 0 ? "primary" : "", disabled: !rel.signed || rel.downloaded,
      title: rel.signed ? "download and verify" : "this release is not signed: the app will not use it",
      onclick: () => download(kind, { tag: rel.tag }) }, rel.downloaded ? "Downloaded" : "Download");
    list.append(h("div", { class: "uprow" },
      h("div", {}, h("b", {}, rel.name || rel.tag), n === 0 ? h("span", { class: "tag ok" }, "latest") : null, " ",
        h("span", { class: "muted" }, (rel.published || "").slice(0, 10)), " ",
        rel.signed ? h("span", { class: "muted" }, "signed release") : h("span", { class: "tag warn" }, "not signed"),
        rel.notes ? h("div", { class: "muted upnotes" }, rel.notes) : null),
      dl));
  });
  if (!rels.length && !r.error) list.append(h("div", { class: "muted" }, "No releases yet."));
  const st = r.stock;
  if (st) {
    if (st.error) list.append(h("div", { class: "muted" }, `Stock firmware (diglet48): ${st.error}`));
    else list.append(h("div", { class: "uprow" },
      h("div", {}, h("b", {}, `Stock FOC-Stim ${st.tag}`), " ", h("span", { class: "muted" }, "(diglet48, the original)"),
        h("div", { class: "muted upnotes" }, st.note), st.url ? h("a", { href: st.url, target: "_blank", rel: "noopener" }, "release page") : null),
      st.known ? h("button", { disabled: st.downloaded, onclick: () => download(kind, { stock: st.tag }) },
        st.downloaded ? "Downloaded" : "Download") : null));
  }
}
async function checkUpdates(kind) {
  const b = $(kind + "UpCheck");
  b.disabled = true; b.textContent = "Checking…";
  const r = await api("/api/updates/check", { kind });
  b.disabled = false; b.textContent = "Check for updates";
  if (!r.ok) { showError(r.data.error || "update check failed"); return; }
  upState[kind] = r.data;
  renderUpdates(kind);
}
async function download(kind, what) {
  const r = await api("/api/updates/download", { kind, ...what });
  if (!r.ok) { showError(r.data.error || "download failed"); return; }
  showError(null);
  await loadFirmware();
  if (r.data.image && r.data.image.id) { $(kind + "Image").value = r.data.image.id; imageInfo(kind); }
  checkUpdates(kind);
}
for (const kind of ["box", "remote"]) {
  $(kind + "UpCheck").addEventListener("click", () => checkUpdates(kind));
  $(kind + "UpOlder").addEventListener("change", () => renderUpdates(kind));
}
api("/api/updates").then((r) => {          // the last check's result, without asking GitHub again
  if (!r.ok) return;
  for (const kind of ["box", "remote"]) if (r.data[kind]) { upState[kind] = r.data[kind]; renderUpdates(kind); }
});
$("boxImage").addEventListener("change", () => { imageInfo("box"); updateBoxFlash(); });
$("remoteImage").addEventListener("change", () => { imageInfo("remote"); updateRemoteButtons(); });

// ---------------------------------------------------------------- jobs (a log that fills while a flash / load runs)
function jobRunning() { return Object.values(jobs).some((j) => j.state === "running"); }
function showJob(prefix, id, title) {
  const j = jobs[prefix] = { id, state: "running", timer: null };
  $(prefix + "Job").hidden = false;
  $(prefix + "JobTitle").textContent = title;
  $(prefix + "JobLog").textContent = "";
  const tick = async () => {
    const r = await api("/api/jobs/" + encodeURIComponent(id));
    if (!r.ok) { setJobState(prefix, "failed", r.data.error || "job lost"); return; }
    const d = r.data, pre = $(prefix + "JobLog");
    const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
    const text = (d.log || []).join("\n");
    if (pre.textContent !== text) { pre.textContent = text; if (atEnd) pre.scrollTop = pre.scrollHeight; }
    if (d.state === "running") { j.timer = setTimeout(tick, JOB_MS); setJobState(prefix, "running"); return; }
    setJobState(prefix, d.state, d.state === "ok" ? "done" : `failed${d.rc != null ? " (exit " + d.rc + ")" : ""}`);
    refreshDevices(); refreshEngine();
    if (prefix === "load") loadM5Settings(false);       // the "last loaded" line
  };
  setJobState(prefix, "running");
  tick();
}
function setJobState(prefix, state, text) {
  const j = jobs[prefix];
  if (j) j.state = state;
  const s = $(prefix + "JobState");
  s.className = "jobstate " + state;
  s.textContent = text || (state === "running" ? "running…" : state);
  updateBoxFlash(); updateRemoteButtons();
}
async function startJob(prefix, path, body, title) {
  const r = await api(path, body);
  if (!r.ok || !r.data.job) {
    $(prefix + "Job").hidden = false;
    $(prefix + "JobTitle").textContent = title;
    $(prefix + "JobLog").textContent = r.data.error || `refused (${r.status})`;
    jobs[prefix] = { state: "failed" };
    setJobState(prefix, "failed", "refused");
    return false;
  }
  showError(null);
  showJob(prefix, r.data.job, title);
  return true;
}
async function resumeJobs() {                   // a reload during a flash picks its log back up
  const r = await api("/api/jobs");
  if (!r.ok) return;
  const list = Array.isArray(r.data) ? r.data : (r.data.jobs || []);
  for (const j of list) {
    if (j.state !== "running") continue;
    const k = String(j.kind || "");
    const prefix = k.includes("load") ? "load" : k.includes("pair") ? "pair"
      : k.includes("remote") ? "remote" : k.includes("box") || k.includes("flash") ? "box" : null;
    if (prefix && !jobs[prefix]) showJob(prefix, j.id, k);
  }
}

// ---------------------------------------------------------------- box firmware
function boxWhyNot() {
  const port = $("boxPort").value, dev = devices.find((d) => d.port === port), img = image("box");
  if (jobRunning()) return "a job is running";
  if (!dev) return "no box selected";
  if (engineOn(port)) return "the engine is driving this box: disconnect it first";
  if (dev.in_use) return "another program has this port open";
  if (dev.kind === "remote") return "that is the remote, not a box";
  if (!img) return "no image selected";
  if (img.error) return img.error;
  if (!$("remoteOff").checked) return "tick the remote box first";
  return "";
}
function updateBoxFlash() {
  const why = boxWhyNot();
  $("boxFlashBtn").disabled = !!why;
  $("boxFlashWhy").textContent = why;
  if (why) $("boxConfirm").hidden = true;          // anything changed under a pending confirm: ask again
}
$("remoteOff").addEventListener("change", updateBoxFlash);
$("boxPort").addEventListener("change", updateBoxFlash);
$("boxFlashBtn").addEventListener("click", () => {
  if (boxWhyNot()) return;
  const port = $("boxPort").value, dev = devices.find((d) => d.port === port), img = image("box");
  $("boxConfirmText").innerHTML = "";
  $("boxConfirmText").append("Flash ", h("b", {}, `${img.name}${img.version ? " " + img.version : ""}`),
    " to the box on ", h("b", {}, port), dev.serial ? ` (${dev.serial})` : "", "?",
    img.sha256 ? h("div", { class: "mono" }, "SHA-256 " + img.sha256) : null,
    h("div", {}, "The box has no working firmware from the erase until the flash is verified (about a minute). Don't unplug it."));
  $("boxConfirm").hidden = false;
});
$("boxFlashCancel").addEventListener("click", () => { $("boxConfirm").hidden = true; });
$("boxFlashGo").addEventListener("click", async () => {
  $("boxConfirm").hidden = true;
  if (boxWhyNot()) return;
  const port = $("boxPort").value, img = image("box");
  await startJob("box", "/api/flash/box", { port, image: img.id, confirm: true, remote_off: true },
    `Flash ${img.name} → ${port}`);
  $("remoteOff").checked = false;             // asked again for the next flash
  updateBoxFlash();
});

// ---------------------------------------------------------------- remote
function remoteWhyNot(needImage) {
  const port = $("remotePort").value, dev = devices.find((d) => d.port === port);
  if (jobRunning()) return "a job is running";
  if (!dev) return "no remote on USB";
  if (dev.in_use) return "another program has this port open";
  if (dev.kind === "box") return "that is a box, not the remote";
  if (needImage) {
    const img = image("remote");
    if (!img) return "no image selected";
    if (img.error) return img.error;
  }
  return "";
}
function updateRemoteButtons() {
  const why = remoteWhyNot(true);
  $("remoteFlashBtn").disabled = !!why;
  $("remoteFlashWhy").textContent = why;
  $("remoteLoad").disabled = !!remoteWhyNot(false);
  const pairWhy = jobRunning() ? "a job is running" : !$("pairPort").value ? "no box on USB"
    : engineOn($("pairPort").value) ? "the engine is driving this box: disconnect it first" : "";
  $("pairRemote").disabled = !!pairWhy; $("pairHouse").disabled = !!pairWhy;
  $("pairRemote").title = $("pairHouse").title = pairWhy;
}
$("remotePort").addEventListener("change", updateRemoteButtons);
$("pairPort").addEventListener("change", updateRemoteButtons);
$("remoteLoad").addEventListener("click", () => {
  const port = $("remotePort").value;
  startJob("load", "/api/remote/load", { port }, `Load patterns & settings → ${port}`);
});
$("pairRemote").addEventListener("click", () => {
  const port = $("pairPort").value;
  startJob("pair", "/api/remote/pair", { box_port: port, house: false }, `${port} → the remote's Wi-Fi`);
});
$("pairHouse").addEventListener("click", () => {
  const port = $("pairPort").value;
  startJob("pair", "/api/remote/pair", { box_port: port, house: true }, `${port} → house Wi-Fi`);
});
$("remoteFlashBtn").addEventListener("click", () => {
  if (remoteWhyNot(true)) return;
  const port = $("remotePort").value, img = image("remote");
  $("remoteConfirmText").innerHTML = "";
  $("remoteConfirmText").append("Flash ", h("b", {}, `${img.name}${img.version ? " " + img.version : ""}`),
    " to the remote on ", h("b", {}, port), "?",
    img.sha256 ? h("div", { class: "mono" }, "SHA-256 " + img.sha256) : null,
    h("div", {}, "The remote must be stopped. It restarts when the flash is done."));
  $("remoteConfirm").hidden = false;
});
$("remoteFlashCancel").addEventListener("click", () => { $("remoteConfirm").hidden = true; });
$("remoteFlashGo").addEventListener("click", () => {
  $("remoteConfirm").hidden = true;
  if (remoteWhyNot(true)) return;
  const port = $("remotePort").value, img = image("remote");
  startJob("remote", "/api/flash/remote", { port, image: img.id, confirm: true }, `Flash ${img.name} → ${port}`);
});

// ---------------------------------------------------------------- hotkeys
// ---- the current cap (Settings) ----
let capSaved = null;
async function loadCap() {
  const r = (await api("/api/settings/cap")).data;
  if (!r || r.amps === undefined) return;
  capSaved = Math.round(r.amps * 1000);
  $("capRange").min = Math.round(r.min * 1000);
  $("capRange").max = Math.round(r.max * 1000);
  $("capRange").value = capSaved;
  capShow();
  $("capState").textContent = `now ${capSaved} mA`;
}
function capShow() {
  const v = Number($("capRange").value);
  $("capValue").textContent = `${v} mA`;
  $("capSave").disabled = v === capSaved;
  $("capConfirm").hidden = true;
  $("capMsg").textContent = v > capSaved ? `raising from ${capSaved} mA` : v < capSaved ? `lowering from ${capSaved} mA` : "";
}
async function capWrite() {
  const v = Number($("capRange").value);
  let r = null;
  try {
    r = await (await fetch("/api/settings/cap", { method: "PUT", headers: { "Content-Type": "application/json" },
                                                    body: JSON.stringify({ amps: v / 1000 }) })).json();
  } catch (e) { r = { error: String(e) }; }
  $("capConfirm").hidden = true;
  if (r && r.ok) { capSaved = Math.round(r.amps * 1000); capShow(); $("capState").textContent = `now ${capSaved} mA`; $("capMsg").textContent = r.note; }
  else $("capMsg").textContent = (r && r.error) || "not saved";
}
$("capRange").addEventListener("input", capShow);
$("capSave").addEventListener("click", () => {
  const v = Number($("capRange").value);
  if (v > capSaved) {               // raising asks once more; lowering is always fine
    $("capConfirm").hidden = false;
    $("capMsg").textContent = `Raise the cap from ${capSaved} to ${v} mA?`;
  } else capWrite();
});
$("capConfirm").addEventListener("click", capWrite);
loadCap();

async function loadHotkeys() {
  const r = await api("/api/hotkeys");
  const rows = $("hotkeyRows");
  rows.innerHTML = "";
  if (!r.ok) { rows.append(h("tr", {}, h("td", { colspan: 3, class: "muted" }, r.data.error || "unavailable"))); return; }
  const d = r.data;
  $("hotkeyState").textContent = d.supported ? (d.running ? "· active" : "· not running") : "· not available here";
  if (d.note) $("hotkeyNote").textContent = d.note;
  for (const b of d.bindings || []) {
    rows.append(h("tr", {},
      h("td", { class: "mono" }, b.keys),
      h("td", {}, ACTION_NAMES[b.action] || b.action),
      h("td", {}, b.registered ? h("span", { class: "tag use" }, "active")
        : h("span", { class: "tag bad", title: b.error || "" }, b.error || "off"))));
  }
}

// ---------------------------------------------------------------- start
showTab((location.hash || "#play").slice(1).replace(/[^a-z]/g, "") || "play");
if (!document.querySelector('[role="tab"][aria-selected="true"]')) showTab("play");
refreshEngine().then(() => refreshDevices(true));   // detect once on open: only devices not seen before are asked
loadFirmware();
resumeJobs();
setInterval(() => { refreshEngine(); refreshDevices(); }, POLL_MS);

// ---------------------------------------------------------------- Play tab: status panel
const dash = (v, unit = "") => (v == null || v === "" ? "—" : `${v}${unit}`);
function stat(k, v, cls) { return h("div", { class: "stat" }, h("div", { class: "k" }, k), h("div", { class: "v " + (cls || "") }, v)); }
async function refreshStatus() {
  const r = await api("/api/engine/status");
  if (!r.ok) return;
  const s = r.data;
  $("statusOff").hidden = !!(s.running || s.reachable);
  const grid = $("statusGrid");
  grid.innerHTML = "";
  if (s.running || s.reachable) {
    const state = s.state === "running" ? "RUNNING" : s.state === "stopped" ? "stopped" : s.state === "fault" ? "FAULT" : "—";
    const linkOk = s.telemetry_age_s != null && s.telemetry_age_s <= 3;
    const out = { preview: "Preview (not the box)", fork: "the box (PlaStim fw)", stock: "the box (stock fw)" }[s.output] || s.output;
    grid.append(
      stat("Output", state, s.state === "running" ? "good" : s.state === "fault" ? "bad" : ""),
      stat("Box", s.port ? `${s.port}${s.link && s.link !== "serial" ? " · " + s.link : ""}` : dash(s.link), linkOk ? "good" : "warn"),
      stat("Firmware", dash(s.firmware)),
      stat("Playing to", dash(out), s.output === "preview" ? "warn" : ""),
      stat("Pattern", dash(s.pattern)),
      stat(`Level A (${(s.routes || [])[0] ?? "—"})`, dash(s.level_a, " %")),
      stat(`Level B (${(s.routes || [])[1] ?? "—"})`, dash(s.level_b, " %")),
      stat("Master", dash(s.master, " %")),
      stat("MA", dash(s.ma, " %")),
      stat("Measured A", dash(s.peak_ma_a, " mA")),
      stat("Measured B", dash(s.peak_ma_b, " mA")),
      stat("Pulse rate", dash(s.pulse_hz, " Hz")),
      stat("Box knob", dash(s.box_knob, " %"), s.box_knob === 0 ? "warn" : ""),
      stat("Battery", s.battery == null ? "—" : `${s.battery} %${s.charging ? " (charging)" : ""}`,
        s.battery != null && s.battery < 20 ? "warn" : ""));
  }
  const w = $("statusWarnings");
  w.innerHTML = "";
  (s.warnings || []).forEach((t) => w.append(h("li", {}, t)));
  const trip = s.trip;
  $("tripPanel").hidden = !trip;
  if (trip) {
    $("tripWhen").textContent = "at " + new Date(trip.time * 1000).toLocaleTimeString();
    $("tripLines").textContent = (trip.lines || []).join("\n");
  }
  const log = (s.log || []).join("\n") || "—";
  const pre = $("engineLog"), atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
  if (pre.textContent !== log) { pre.textContent = log; if (atEnd) pre.scrollTop = pre.scrollHeight; }
}

// ---------------------------------------------------------------- M5 remote: settings
let m5 = null;
function m5ModeNote() {
  $("m5ModeNote").textContent = $("m5Mode").value === "direct"
    ? "The remote makes its own network and each box joins it (Box Wi-Fi below): one hop, no house router in between, the least lag. Switch the remote on before the boxes."
    : "The remote and the boxes all join the house Wi-Fi; each box needs its house address. Simpler, but a busy house network adds lag.";
  $("m5DirectBox").classList.toggle("dim", $("m5Mode").value !== "direct");
}
function boxRow(b = {}) {
  const tr = h("tr", {},
    h("td", {}, h("input", { class: "bx-name", value: b.name || "", maxlength: 24, placeholder: "box 1" })),
    h("td", {}, h("input", { class: "bx-mac mono", value: b.mac || "", placeholder: "aa:bb:cc:dd:ee:ff" })),
    h("td", {}, h("input", { class: "bx-host", value: b.host || "", placeholder: "only for house Wi-Fi" })),
    h("td", { class: "act" }, h("button", { onclick: () => tr.remove(), title: "remove this box" }, "Remove")));
  tr.dataset.port = b.port || 55533;
  return tr;
}
function detectedBoxButtons() {
  const span = $("m5AddDetected");
  if (!span) return;
  span.innerHTML = "";
  const have = [...document.querySelectorAll("#m5Boxes .bx-mac")].map((i) => i.value.trim().toLowerCase());
  for (const d of devices.filter((d) => d.kind === "box" && d.serial)) {
    if (have.includes(d.serial.toLowerCase())) continue;
    span.append(h("button", { onclick: () => { $("m5Boxes").append(boxRow({ name: d.name || "", mac: d.serial.toLowerCase() })); detectedBoxButtons(); } },
      `Add ${d.name || "the FOC-Stim"} on ${d.port}`), " ");
  }
}
async function loadM5Settings(fillForm = true) {
  const r = await api("/api/remote/settings");
  if (!r.ok) { $("m5SetState").textContent = r.data.error || "settings unavailable"; return; }
  m5 = r.data;
  if (fillForm) {
    $("m5Mode").value = m5.direct.enabled ? "direct" : "house";
    $("m5DirSsid").value = m5.direct.ssid || "";
    $("m5DirCh").value = String(m5.direct.channel || 6);
    $("m5HouseSsid").value = m5.wifi.ssid || "";
    for (const [id, has] of [["m5DirPw", m5.direct.has_password], ["m5HousePw", m5.wifi.has_password]]) {
      $(id).value = "";
      $(id).placeholder = has ? "unchanged" : "not set";
    }
    const tb = $("m5Boxes");
    tb.innerHTML = "";
    (m5.boxes || []).forEach((b) => tb.append(boxRow(b)));
    m5ModeNote();
  }
  $("m5SetState").textContent = m5.missing ? "· not set up yet" : m5.build_error ? "· needs attention" : "";
  $("m5SaveMsg").textContent = m5.build_error || "";
  const saf = $("m5Safety");
  saf.innerHTML = "";
  const g = m5.remote_gets;
  if (g) saf.append(stat("Current cap", `${Math.round(g.safety.amps_cap * 1000)} mA`), stat("Slow start", `${g.safety.slow_start_s} s`),
    stat("Deadman after", `${g.safety.deadman_silence_s} s`), stat("Deadman ramp", `${g.safety.deadman_ramp_down_s} s`),
    stat("ET-312 asymmetry", `${g.et312.monophasic_asymmetry}`), stat("Pads connected", (g.defaults.pads || []).map((p, i) => p ? i + 1 : "–").join(" ")));
  const ll = m5.last_load, el = $("m5LastLoad");
  if (!ll) el.textContent = "Not loaded from this computer yet.";
  else {
    const when = new Date(ll.time * 1000).toLocaleString();
    const match = ll.settings_match === true ? "the remote has the settings shown above"
      : ll.settings_match === false ? "the settings have changed since: load again" : "";
    el.textContent = `Last loaded ${when}: ${ll.patterns ?? "?"} patterns${match ? "; " + match : ""}.`;
    el.className = "fine " + (ll.settings_match === false ? "warntext" : "");
  }
  detectedBoxButtons();
}
$("m5Mode").addEventListener("change", m5ModeNote);
$("m5AddBox").addEventListener("click", () => { $("m5Boxes").append(boxRow({})); });
$("m5Save").addEventListener("click", async () => {
  const boxes = [...$("m5Boxes").querySelectorAll("tr")].map((tr) => ({
    name: tr.querySelector(".bx-name").value.trim(), mac: tr.querySelector(".bx-mac").value.trim(),
    host: tr.querySelector(".bx-host").value.trim(), port: Number(tr.dataset.port) || 55533 }));
  const body = {
    wifi: { ssid: $("m5HouseSsid").value.trim(), password: $("m5HousePw").value },
    direct: { enabled: $("m5Mode").value === "direct", ssid: $("m5DirSsid").value.trim(), password: $("m5DirPw").value,
      channel: Number($("m5DirCh").value) },
    boxes };
  $("m5Save").disabled = true;
  const r = await fetch("/api/remote/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then(async (x) => ({ ok: x.ok, data: await x.json().catch(() => ({})) }))
    .catch(() => ({ ok: false, data: { error: "the hub server is not answering" } }));
  $("m5Save").disabled = false;
  if (!r.ok) { $("m5SaveMsg").textContent = r.data.error || "not saved"; $("m5SaveMsg").className = "warntext"; return; }
  $("m5SaveMsg").textContent = "Saved. Load patterns & settings to send them to the remote.";
  $("m5SaveMsg").className = "muted";
  loadM5Settings();
});

// ---------------------------------------------------------------- M5 remote: pattern files
const GROUP_HELP = {
  "Built-in modes": () => "The ET-312's own 18 modes. Not included: extract them from your own ET-312's firmware (card below).",
  "ErosLink": () => "The routines ErosTek shipped with ErosLink, from ErosLink's installer (see below).",
  "ErosLink examples": () => "ErosLink's designer examples, from the same installer.",
  "Your routines": (p) => [".elk files in your ErosLink folder: ", h("span", { class: "mono" }, p.elk_dir.path),
    p.elk_dir.exists ? "" : " (not found)", ". Put new .elk files there."],
  "Our routines": (p) => ["PlaStim's routines in this program's folder: ", h("span", { class: "mono" }, p.ours_dir.path),
    p.ours_dir.exists ? "" : " (empty so far)"],
};
async function loadPatterns() {
  const r = await api("/api/remote/patterns");
  const tb = $("patRows");
  if (!r.ok) { tb.innerHTML = ""; tb.append(h("tr", {}, h("td", { colspan: 3, class: "muted" }, r.data.error || "could not read the pattern files"))); return; }
  const p = r.data;
  tb.innerHTML = "";
  for (const g of p.groups) tb.append(h("tr", {}, h("td", {}, h("b", {}, g.name)),
    h("td", { class: "mono" }, String(g.count)), h("td", { class: "muted" }, (GROUP_HELP[g.name] || (() => ""))(p))));
  $("patTotal").textContent = `· ${p.total} in all`;
  $("patCache").textContent = p.eroslink_cache ? `${p.eroslink_cache.path} (${p.eroslink_cache.exists ? p.eroslink_cache.files + " files" : "not created yet"})` : "";
  const notes = $("patNotes");
  notes.innerHTML = "";
  (p.notes || []).forEach((n) => notes.append(h("li", {}, n)));
  $("patNotesBox").hidden = !(p.notes || []).length;
}

// ---------------------------------------------------------------- M5 remote: ET-312 built-in modes
async function loadEt312() {
  const r = await api("/api/et312");
  if (!r.ok) return;
  const d = r.data;
  $("et312State").textContent = d.available ? `· available (${d.blocks} programs)` : "· not available";
  $("et312State").className = d.available ? "oktext" : "muted";
  $("et312File").textContent = d.user_file;
}
$("et312Image").addEventListener("change", () => { $("et312Extract").disabled = !$("et312Image").files.length; });
$("et312Extract").addEventListener("click", async () => {
  const f = $("et312Image").files[0];
  if (!f) return;
  $("et312Extract").disabled = true;
  $("et312Msg").textContent = "Extracting…";
  const r = await fetch("/api/et312/extract", { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: f })
    .then(async (x) => ({ ok: x.ok, data: await x.json().catch(() => ({})) }))
    .catch(() => ({ ok: false, data: { error: "the hub server is not answering" } }));
  $("et312Extract").disabled = false;
  $("et312Msg").textContent = r.ok ? `Done: ${r.data.blocks} mode programs saved. Load patterns & settings to put them on the remote.`
    : (r.data.error || "extraction failed");
  $("et312Msg").className = r.ok ? "oktext" : "warntext";
  loadEt312(); loadPatterns();
});

refreshStatus();
setInterval(refreshStatus, 1500);
