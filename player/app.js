// The player page (the ET-312 emulator; served by stimengine/control/foc312_api.py): talks to the engine process
// on this port (WS /ws). No libraries.
"use strict";

// the FOC-Stim's electrode lead colours: 1 red, 2 blue, 3 yellow, 4 green; the number dark on yellow
const EL_COLOR = ["#d62828", "#1f6fe0", "#f2c200", "#2a9d3f"];
const EL_INK = ["#fff", "#fff", "#1b1b1b", "#fff"];

const $ = (id) => document.getElementById(id);
const HIST_S = 10;
const RATE_LO = 15, RATE_HI = 415;
const SOCKET_X = [50, 150, 250, 350], SOCKET_Y = 95, SOCKET_R = 22;

let ws = null, wsOpen = false, seq = 0, st = null;
let hist = [];                    // [t, ia, ib, ra, rb]
let editCh = "a", pendingSocket = null;
const localHold = {};             // slider id -> time until which server values are ignored
let lastFrameT = performance.now(), dashOff = [0, 0];

// ---------------------------------------------------------------- helpers
function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function pct(v) { return v == null ? "—" : Math.round(v * 100) + "%"; }
function parseRoute(code) {
  const s = String(code).replace(/[-,\s]/g, "");
  if (!/^[1-4]{2}$/.test(s) || s[0] === s[1]) return null;
  return [Number(s[0]), Number(s[1])];
}
function showError(msg) {
  const e = $("error");
  if (msg) { e.textContent = msg; e.hidden = false; } else { e.hidden = true; }
}

// ---------------------------------------------------------------- link
function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => { wsOpen = true; setConn(true); };
  ws.onclose = () => { wsOpen = false; setConn(false); setTimeout(connect, 1500); };
  ws.onerror = () => {};
  ws.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.type === "state") onState(m.state, m.full);
    else if (m.type === "result" && m.ok === false) showError(m.error);
    else if (m.type === "result") showError(null);
  };
}
function setConn(on) {
  const c = $("conn");
  c.textContent = on ? "live" : "offline";
  // the engine went away (a box trip stops it): the hub restarts it once the box is power-cycled, and this page
  // reconnects by itself with the same setup, levels 0 and not armed
  if (!on) showError("Engine offline. If the box tripped: switch it off and on; it reconnects by itself, then ARM again.");
  else showError(null);
  c.className = "pill " + (on ? "on" : "off");
  if (pipWin && st) updatePip(st);   // the pop-out's dot follows the link too (only called after the script ran)
}
function send(cmd, fields = {}) {
  if (!wsOpen) { showError("not connected to the engine"); return; }
  ws.send(JSON.stringify({ cmd, seq: ++seq, ...fields }));
}
// the page heartbeat IS the deadman's control input: stop sending and the engine ramps to zero
setInterval(() => { if (wsOpen) ws.send(JSON.stringify({ cmd: "hb" })); }, 500);

function throttle(fn, ms) {
  let t = 0, timer = null, lastArgs = null;
  return (...args) => {
    lastArgs = args;
    const now = Date.now();
    if (now - t >= ms) { t = now; fn(...args); }
    else if (!timer) timer = setTimeout(() => { timer = null; t = Date.now(); fn(...lastArgs); }, ms - (now - t));
  };
}

// ---------------------------------------------------------------- state -> DOM
function onState(s, full) {
  st = s;
  if (full && s.hist) hist = s.hist.slice();
  else if (s.sample && (!hist.length || s.sample[0] > hist[hist.length - 1][0])) hist.push(s.sample);
  if (hist.length && hist[0][0] > s.t + 0.5) hist = [];            // runner restarted
  while (hist.length && hist[0][0] < s.t - HIST_S - 0.2) hist.shift();
  if (full && s.advanced_ranges) buildAdvanced(s.advanced_ranges);

  // the box: plays only on the PlaStim firmware (the runner picks it by itself once the box is connected)
  const eng = s.engine, pill = $("boxPill");
  const live = !!(eng && eng.running && eng.link);
  pill.textContent = s.output === "fork" ? "PlaStim firmware ✓" : live ? "box needs the PlaStim firmware" : "no box";
  pill.className = "pill " + (s.output === "fork" ? "on" : live ? "bad" : "off");
  pill.title = s.output === "fork" ? "" : live ? "flash it on the hub's Boxes tab" : "connect a box in the hub";
  const arm = $("arm");
  arm.disabled = s.output === "preview" || !eng || !eng.running;
  arm.classList.toggle("armed", !!(eng && eng.armed));
  arm.textContent = eng && eng.armed ? `ARMED ${pct(eng.master)}` : "ARM";

  // status line
  const parts = [];
  if (!eng) parts.push("<b>preview only</b> (no engine link)");
  else {
    parts.push(`link <b>${eng.link || "closed"}</b>`);
    if (eng.firmware) parts.push(`fw <b>${eng.firmware}${eng.fork_firmware ? " fork" : ""}</b>`);
    parts.push(`mode <b>${eng.mode || "—"}</b>`);
    parts.push(`master <b>${pct(eng.master)}</b>`);
    if (eng.deadman_active) parts.push(`<span class="warn">deadman ${pct(eng.deadman_scale)}</span>`);
    if (eng.faulted) parts.push(`<span class="bad">FAULT: ${eng.fault_reason}</span>`);
    // the current the wires get, measured by the box (as the M5 remote shows it); what was asked, small, after it
    const ma = (a) => (a == null ? "—" : (a * 1000).toFixed(1) + " mA");
    const meas = eng.measured || [];
    parts.push(`current <b class="ch-a">A ${ma(meas[0])}</b> <b class="ch-b">B ${ma(meas[1])}</b>`);
    parts.push(`<small class="muted">asked ${(eng.amps || []).map(ma).join(" / ")}</small>`);
  }
  $("status").innerHTML = parts.join(" · ");
  if (s.error) showError(s.error);

  // pattern
  const sel = $("pattern");
  if (document.activeElement !== sel && sel.value !== s.pattern.id) sel.value = s.pattern.id;
  // Random 1/2 pick other modes: show which one is playing; otherwise the dropdown already says it
  $("modeName").textContent = s.pattern.id.startsWith("builtin:random") ? `now playing: ${s.mode_name}` : "";
  $("phaseMode").textContent = s.phase_mode !== "none" ? `phase: ${s.phase_mode}` : "";
  syncSlider("ma", s.ma, "maOut");
  syncSlider("levelA", s.levels[0], "levelAOut");
  syncSlider("levelB", s.levels[1], "levelBOut");
  if (s.master_set !== undefined) syncSlider("master", s.master_set, "masterOut");
  $("masterNote").textContent = boxVolText(s);
  $("knob").textContent = s.knob.value != null ? pct(s.knob.value) + (s.knob.locked ? " (locked)" : "") : s.knob.note.replace(/^knob: /, "");

  // routing
  const card = $("routingCard");
  card.classList.toggle("disabled", !s.caps.routing);
  $("routingNote").textContent = s.caps.routing ? "" : s.caps.note;
  $("shapeRow").classList.toggle("disabled", !s.caps.pulse_shape);
  $("shapeNote").textContent = s.caps.pulse_shape ? "" : s.caps.pulse_shape_note;
  const isTaper = String(s.shape || "").startsWith("taper");
  document.querySelectorAll(".shapes button").forEach((b) => b.setAttribute("aria-checked",
    String(b.dataset.shape === s.shape || (b.dataset.shape === "taper" && isTaper))));
  if (isTaper && document.activeElement !== $("taperAmt")) {
    $("taperAmt").value = String(parseInt(s.shape.slice(5), 10) || 0);
    $("taperOut").textContent = $("taperAmt").value + "%";
  }
  // triangle / taper: fork v7 only (the engine refuses them on an older box, which would misplay them)
  document.querySelectorAll(".shapes .v7").forEach((el) => {
    el.classList.toggle("disabled", !s.caps.shapes_v7);
    el.querySelectorAll("input").forEach((i) => { i.disabled = !s.caps.shapes_v7; });
    if (el.tagName === "BUTTON") el.disabled = !s.caps.shapes_v7;
  });
  const pads = s.pads || [true, true, true, true];
  document.querySelectorAll(".padbtns button").forEach((b) => b.setAttribute("aria-pressed", String(!!pads[Number(b.dataset.pad) - 1])));
  $("blockedNote").textContent = ["a", "b"].filter((ch) => s[ch].blocked)
    .map((ch) => `${ch.toUpperCase()}: ${s[ch].blocked} (silent)`).join(" · ");
  $("routeA").textContent = s.routes[0];
  $("routeB").textContent = s.routes[1];
  document.querySelectorAll(".chsel button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.ch === editCh)));

  // numbers
  for (const [ch, id] of [["a", "numsA"], ["b", "numsB"]]) {
    const c = s[ch];
    const rate = c.rate_hz_et312 > 400 ? `${c.rate_hz} Hz (312: ${c.rate_hz_et312})` : `${c.rate_hz} Hz`;
    $(id).textContent = `${rate} · ${c.width_us} µs · ${c.biphasic ? "biphasic" : "monophasic"} · asym ${c.asymmetry} · ` +
      (c.blocked ? "silent: no pad" : c.gate ? `${pct(c.intensity)}` : "gate off");
  }
  const eInfo = eng ? `cap ${eng.amps_cap} A, api ${pct(eng.api)}` : "not connected";
  $("engineInfo").textContent = eInfo;
  if (full || !$("power").matches(":focus")) $("power").value = s.power;
  $("skipRamp").checked = !!s.skip_mode_ramp;
  if (s.advanced) for (const [k, v] of Object.entries(s.advanced)) syncSlider("adv_" + k, v, "adv_" + k + "Out", true);
  if (pipWin) updatePip(s);
}

function syncSlider(id, value, outId, raw = false) {
  const el = $(id);
  if (!el) return;
  if ((localHold[id] || 0) > Date.now()) return;
  el.value = raw ? value : Math.round(value * 1000);
  $(outId).textContent = raw ? value : pct(value);
}

// ---------------------------------------------------------------- controls
function bindSlider(id, outId, onSend) {
  const el = $(id), sendT = throttle(onSend, 50);
  el.addEventListener("input", () => {
    localHold[id] = Date.now() + 700;
    const v = Number(el.value) / 1000;
    $(outId).textContent = pct(v);
    sendT(v);
  });
}
bindSlider("ma", "maOut", (v) => send("ma", { value: v }));
bindSlider("levelA", "levelAOut", (v) => send("levels", { a: v }));
bindSlider("levelB", "levelBOut", (v) => send("levels", { b: v }));
bindSlider("master", "masterOut", (v) => send("master", { value: v }));

$("pattern").addEventListener("change", (e) => send("pattern", { id: e.target.value }));
$("power").addEventListener("change", (e) => send("power", { level: e.target.value }));
$("ramp").addEventListener("click", () => send("ramp"));
$("skipRamp").addEventListener("change", (e) => send("skip_ramp", { on: e.target.checked }));
$("arm").addEventListener("click", () => send("arm"));
$("stop").addEventListener("click", () => send("stop"));
document.querySelectorAll(".chsel button").forEach((b) => b.addEventListener("click", () => {
  editCh = b.dataset.ch; pendingSocket = null; routeHint();
  document.querySelectorAll(".chsel button").forEach((x) => x.setAttribute("aria-checked", String(x === b)));
}));
document.querySelectorAll(".shapes button").forEach((b) => b.addEventListener("click", () =>
  send("shape", { value: b.dataset.shape === "taper" ? "taper" + $("taperAmt").value : b.dataset.shape })));
$("taperAmt").addEventListener("input", () => {
  $("taperOut").textContent = $("taperAmt").value + "%";
  if ($("taperBtn").getAttribute("aria-checked") === "true") send("shape", { value: "taper" + $("taperAmt").value });
});
$("revA").addEventListener("click", () => send("reverse", { ch: "a" }));
$("swapAB").addEventListener("click", () => send("swap"));
$("revB").addEventListener("click", () => send("reverse", { ch: "b" }));
document.querySelectorAll(".padbtns button").forEach((b) => b.addEventListener("click", () => {
  send("pads", { pad: Number(b.dataset.pad), on: b.getAttribute("aria-pressed") !== "true" });
}));
document.querySelectorAll(".presets button").forEach((b) => b.addEventListener("click", () => {
  const [a, bb] = b.dataset.preset.split(",");
  send("routes", { a: Number(a), b: Number(bb) });
}));

function buildAdvanced(ranges) {
  const box = $("advSliders");
  if (box.dataset.built) return;
  box.dataset.built = "1";
  const names = { ramp_level: "Ramp level", ramp_time: "Ramp time", depth: "Depth", tempo: "Tempo",
    freq: "Frequency", effect: "Effect", width: "Width", pace: "Pace" };
  for (const [k, [lo, hi]] of Object.entries(ranges)) {
    const lab = document.createElement("label");
    lab.className = "slider";
    lab.innerHTML = `<span class="lbl">${names[k] || k}</span><input id="adv_${k}" type="range" min="${lo}" max="${hi}" step="1">` +
      `<output id="adv_${k}Out"></output>`;
    box.appendChild(lab);
    const el = lab.querySelector("input");
    const sendT = throttle((v) => send("advanced", { [k]: v }), 80);
    el.addEventListener("input", () => {
      localHold["adv_" + k] = Date.now() + 700;
      $("adv_" + k + "Out").textContent = el.value;
      sendT(Number(el.value));
    });
  }
}

async function loadPatterns() {
  try {
    const r = await fetch("/patterns");
    const d = await r.json();
    const sel = $("pattern");
    sel.innerHTML = "";
    for (const g of d.groups) {
      const og = document.createElement("optgroup");
      og.label = g.label;
      if (!g.items.length) {
        const o = document.createElement("option");
        o.disabled = true; o.textContent = "(none found)";
        og.appendChild(o);
      }
      for (const it of g.items) {
        const o = document.createElement("option");
        o.value = it.id; o.textContent = it.name; o.disabled = !!it.disabled;
        if (it.description) o.title = it.description;
        og.appendChild(o);
      }
      sel.appendChild(og);
    }
    if (st) sel.value = st.pattern.id;
    if (d.error) showError(d.error);
  } catch (e) { showError("could not load the pattern list"); }
}

// ---------------------------------------------------------------- routing diagram
const SVGNS = "http://www.w3.org/2000/svg";
function el(tag, attrs, parent) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (parent) parent.appendChild(n);
  return n;
}
const dg = $("diagram");
const linkEls = {};
function buildDiagram() {
  dg.innerHTML = "";
  for (const ch of ["a", "b"]) {
    const g = el("g", {}, dg);
    linkEls[ch] = {
      path: el("path", { fill: "none", "stroke-linecap": "round", "stroke-dasharray": "10 8" }, g),
      under: el("path", { fill: "none", "stroke-linecap": "round", opacity: "0.18" }, g),
      lead: el("path", {}, g),
      flash: el("circle", { r: SOCKET_R + 6, fill: "none", "stroke-width": 3 }, g),
      label: el("text", { "text-anchor": "middle", "font-size": 13, "font-weight": 700 }, g),
    };
    g.insertBefore(linkEls[ch].under, linkEls[ch].path);
  }
  for (let i = 0; i < 4; i++) {
    const g = el("g", { class: "socket", style: "cursor:pointer" }, dg);
    const c = el("circle", { cx: SOCKET_X[i], cy: SOCKET_Y, r: SOCKET_R, "stroke-width": 2 }, g);
    const t = el("text", { x: SOCKET_X[i], y: SOCKET_Y + 6, "text-anchor": "middle", "font-size": 18, "font-weight": 700 }, g);
    t.textContent = String(i + 1);
    g.addEventListener("click", () => tapSocket(i + 1));
    linkEls["s" + i] = c; linkEls["t" + i] = t;
  }
}
function arcPath(x1, x2, up) {
  const span = Math.abs(x2 - x1);
  const h = 22 + span * 0.22;
  const y = up ? SOCKET_Y - SOCKET_R : SOCKET_Y + SOCKET_R;
  const cy = up ? y - h * 2 : y + h * 2;
  return { d: `M ${x1} ${y} Q ${(x1 + x2) / 2} ${cy} ${x2} ${y}`, apexY: up ? y - h : y + h };
}
function tapSocket(n) {
  if (!st || !st.caps.routing) return;
  if (pendingSocket == null) { pendingSocket = n; routeHint(); drawDiagram(0); return; }
  if (pendingSocket === n) { pendingSocket = null; routeHint(); return; }
  const code = pendingSocket * 10 + n;
  pendingSocket = null;
  routeHint();
  if (parseRoute(code)) send("route", { ch: editCh, code });
}
function routeHint() {
  const ch = editCh.toUpperCase();
  $("routeHint").textContent = pendingSocket == null
    ? `Channel ${ch}: tap the leading socket, then the other end.`
    : `Channel ${ch}: lead = ${pendingSocket}. Now tap the other end (tap ${pendingSocket} again to cancel).`;
}

function visRate(r) {   // real rates are 15-415 Hz; show a readable 2-12 Hz blink on a log scale
  const x = Math.log(Math.max(RATE_LO, r) / RATE_LO) / Math.log(RATE_HI / RATE_LO);
  return 2 + 10 * Math.min(1, Math.max(0, x));
}
function drawDiagram(dt) {
  if (!st) return;
  const ink = css("--ink"), sock = css("--socket"), sockInk = css("--socket-ink"), line = css("--line");
  const now = performance.now() / 1000;
  const used = new Set();
  ["a", "b"].forEach((ch, idx) => {
    const c = st[ch], L = linkEls[ch];
    const [x, y] = parseRoute(c.route_sent) || [idx * 2 + 1, idx * 2 + 2];
    used.add(x); used.add(y);
    const blocked = !!c.blocked;
    const color = css(blocked ? "--danger" : ch === "a" ? "--ch-a" : "--ch-b");
    const up = ch === "a";
    const p = arcPath(SOCKET_X[x - 1], SOCKET_X[y - 1], up);
    const I = blocked ? 0 : (c.gate ? c.intensity : 0) * (c.pad_gain ?? 1);
    for (const e of [L.path, L.under]) { e.setAttribute("d", p.d); e.setAttribute("stroke", color); }
    L.under.setAttribute("stroke-width", 4 + 6 * I);
    L.path.setAttribute("stroke-width", 3 + 6 * I);
    L.path.setAttribute("opacity", blocked ? "0.8" : c.gate ? (0.3 + 0.7 * I).toFixed(3) : "0.15");
    L.path.setAttribute("stroke-dasharray", blocked ? "3 7" : "10 8");
    if (!blocked) dashOff[idx] -= dt * (30 + 90 * Math.log10(Math.max(RATE_LO, c.rate_hz) / RATE_LO + 1));
    L.path.setAttribute("stroke-dashoffset", dashOff[idx].toFixed(1));
    // lead marker: triangle pointing into the leading socket
    const lx = SOCKET_X[x - 1], ly = up ? SOCKET_Y - SOCKET_R - 4 : SOCKET_Y + SOCKET_R + 4;
    const s = up ? -1 : 1;
    L.lead.setAttribute("d", `M ${lx - 8} ${ly + s * 12} L ${lx + 8} ${ly + s * 12} L ${lx} ${ly} Z`);
    L.lead.setAttribute("fill", color);
    // pulse flash at the lead socket, blinking at a readable rate, brightness = intensity
    const f = visRate(c.rate_hz);
    const b = I * (0.5 + 0.5 * Math.cos(2 * Math.PI * f * now));
    L.flash.setAttribute("cx", lx); L.flash.setAttribute("cy", SOCKET_Y);
    L.flash.setAttribute("stroke", color); L.flash.setAttribute("opacity", b.toFixed(3));
    L.label.setAttribute("x", (SOCKET_X[x - 1] + SOCKET_X[y - 1]) / 2);
    L.label.setAttribute("y", up ? p.apexY - 8 : p.apexY + 18);
    L.label.setAttribute("fill", color);
    L.label.textContent = blocked ? `${ch.toUpperCase()} ${x}→${y} · no pad`
      : `${ch.toUpperCase()} ${x}→${y} · ${Math.round(c.rate_hz)} Hz` + (c.mode_polarity_swap ? " (mode swap)" : "");
  });
  for (let i = 0; i < 4; i++) {
    const c = linkEls["s" + i];
    const pend = pendingSocket === i + 1;
    // each socket in its electrode's colour (the FOC-Stim's leads: 1 red, 2 blue, 3 yellow, 4 green)
    c.setAttribute("fill", pend ? css(editCh === "a" ? "--ch-a" : "--ch-b") : EL_COLOR[i] || sock);
    c.setAttribute("stroke", used.has(i + 1) ? ink : line);
    linkEls["t" + i].setAttribute("fill", pend ? "#fff" : EL_INK[i] || sockInk);
    const padOn = !st.pads || st.pads[i];              // no pad on this electrode: socket drawn dimmed
    c.setAttribute("opacity", padOn ? "1" : "0.35");
    c.setAttribute("stroke-dasharray", padOn ? "" : "4 4");
    linkEls["t" + i].setAttribute("opacity", padOn ? "1" : "0.4");
  }
}

// ---------------------------------------------------------------- canvases
function prep(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return [ctx, w, h];
}

function drawGlyph(canvas, c, color) {
  const [ctx, w, h] = prep(canvas);
  const mid = h / 2, pad = 10;
  const WIN = 1300;                                 // µs across the canvas
  const xs = (us) => pad + (us / WIN) * (w - 2 * pad);
  ctx.strokeStyle = css("--grid"); ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(pad, mid); ctx.lineTo(w - pad, mid); ctx.stroke();
  ctx.fillStyle = css("--muted"); ctx.font = "11px system-ui";
  if (!c.gate) { ctx.fillText("gate off", pad, 14); return; }
  const I = Math.max(0.04, c.intensity);
  const w1 = c.width_us, w2 = c.width_us * c.asymmetry, shape = c.shape || "rounded";
  // unit-peak phase shape and its charge (fork v2): rounded half-sine, square, soft square (20 µs edges);
  // fork v7: triangle, and taper N % (quarter-sine edges of (1 - N/100) / 2 of the width each, flat between)
  const flat = shape.startsWith("taper") ? Math.min(1, (parseInt(shape.slice(5), 10) || 0) / 100) : null;
  const f = (t, w) => {
    if (shape === "square") return 1;
    if (shape === "soft") { const r = Math.min(20, w / 2); return Math.min(1, t / r, (w - t) / r); }
    if (shape === "triangle") return 1 - Math.abs(2 * t / w - 1);
    if (flat !== null) {
      const r = (1 - flat) * w / 2;
      if (r <= 0) return 1;
      if (t < r) return Math.sin(Math.PI / 2 * t / r);
      if (t > w - r) return Math.sin(Math.PI / 2 * (w - t) / r);
      return 1;
    }
    return Math.sin(Math.PI * t / w);
  };
  const q = (w) => {
    if (shape === "square") return w;
    if (shape === "soft") return w - Math.min(20, w / 2);
    if (shape === "triangle") return w / 2;
    if (flat !== null) { const r = (1 - flat) * w / 2; return 4 * r / Math.PI + (w - 2 * r); }
    return 2 * w / Math.PI;
  };
  const a1 = (mid - 14) * I * (2 * w1 / Math.PI) / q(w1);   // charge-matched to rounded, as the engine sends it
  const a2 = a1 * q(w1) / q(w2);
  ctx.strokeStyle = color; ctx.lineWidth = 2;
  ctx.fillStyle = color + "33";
  ctx.beginPath(); ctx.moveTo(xs(0), mid);
  for (let k = 0; k <= 40; k++) { const u = (k / 40) * w1; ctx.lineTo(xs(u), mid + a1 * f(u, w1)); }
  for (let k = 0; k <= 60; k++) { const u = (k / 60) * w2; ctx.lineTo(xs(w1 + u), mid - a2 * f(u, w2)); }
  ctx.lineTo(xs(w1 + w2), mid);                     // square edges drop straight back to zero
  ctx.lineTo(xs(WIN), mid);
  ctx.fill(); ctx.stroke();
  const [x] = parseRoute(c.route_sent) || [0];
  ctx.fillStyle = css("--muted");
  ctx.fillText(`lead: E${x}`, pad, h - 4);
  const t = `${w1} µs + ${Math.round(w2)} µs · equal charge`;
  ctx.fillText(t, w - pad - ctx.measureText(t).width, 12);
}

function drawStrip(canvas, idx, color) {
  const [ctx, w, h] = prep(canvas);
  ctx.strokeStyle = css("--grid"); ctx.lineWidth = 1;
  for (const f of [0.25, 0.5, 0.75]) { ctx.beginPath(); ctx.moveTo(0, h * f); ctx.lineTo(w, h * f); ctx.stroke(); }
  if (!hist.length || !st) return;
  const t1 = st.t, t0 = t1 - HIST_S;
  const xs = (t) => ((t - t0) / HIST_S) * w;
  const yI = (v) => h - 2 - v * (h - 4);
  const yR = (r) => h - 2 - (Math.log(Math.max(RATE_LO, r) / RATE_LO) / Math.log(RATE_HI / RATE_LO)) * (h - 4);
  ctx.fillStyle = color + "55"; ctx.strokeStyle = color; ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.moveTo(xs(hist[0][0]), h);
  for (const s of hist) ctx.lineTo(xs(s[0]), yI(s[1 + idx]));
  ctx.lineTo(xs(hist[hist.length - 1][0]), h); ctx.closePath(); ctx.fill();
  ctx.strokeStyle = css("--ink"); ctx.lineWidth = 1.2;
  ctx.beginPath();
  hist.forEach((s, i) => { const x = xs(s[0]), y = yR(s[3 + idx]); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
  ctx.stroke();
}

function frame() {
  const now = performance.now(), dt = Math.min(0.1, (now - lastFrameT) / 1000);
  lastFrameT = now;
  if (st) {
    drawDiagram(dt);
    const ca = css("--ch-a"), cb = css("--ch-b");
    drawGlyph($("glyphA"), st.a, ca); drawGlyph($("glyphB"), st.b, cb);
    drawStrip($("stripA"), 0, ca); drawStrip($("stripB"), 1, cb);
  }
  requestAnimationFrame(frame);
}

buildDiagram();
routeHint();
loadPatterns();
connect();
requestAnimationFrame(frame);

// ---------------------------------------------------------------- hotkeys (2026-09-27)
// Q/A Level A up/down · W/S Level B · E/D both · R/F MA · 1 % per press, Shift = 5 % · X both levels -10 % ·
// Space or Esc = STOP. ARM stays a click, so a stray key can never start output. Holding an UP key repeats at most
// 10 steps a second; DOWN keys and STOP are never rate-limited.
const HOTKEYS = { q: ["levelA", 1], a: ["levelA", -1], w: ["levelB", 1], s: ["levelB", -1],
  e: ["both", 1], d: ["both", -1], r: ["ma", 1], f: ["ma", -1] };
let lastUpKey = 0;
function nudge(id, steps) {
  const el = $(id);
  el.value = Math.min(1000, Math.max(0, Number(el.value) + steps * 10));
  el.dispatchEvent(new Event("input"));
}
function keyFlash(text) {
  const h = $("hotkeys");
  if (!h) return;
  h.dataset.flash = text;
  h.classList.add("flash");
  clearTimeout(keyFlash.t);
  keyFlash.t = setTimeout(() => h.classList.remove("flash"), 400);
}
// one handler for this page and the pop-out window (attached to both documents)
function onHotkey(e) {
  const doc = e.currentTarget && e.currentTarget.body ? e.currentTarget : document;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === " " || k === "escape") {
    e.preventDefault();
    send("stop");
    keyFlash("STOP");
    return;
  }
  if (k === "x") {
    e.preventDefault();
    nudge("levelA", -10); nudge("levelB", -10);
    keyFlash("both −10 %");
    return;
  }
  const hk = HOTKEYS[k];
  if (!hk) return;
  e.preventDefault();
  if (doc.activeElement && doc.activeElement !== doc.body) doc.activeElement.blur();
  if (hk[1] > 0) {
    const now = Date.now();
    if (e.repeat && now - lastUpKey < 100) return;
    lastUpKey = now;
  }
  const steps = hk[1] * (e.shiftKey ? 5 : 1);
  if (hk[0] === "both") { nudge("levelA", steps); nudge("levelB", steps); } else nudge(hk[0], steps);
  keyFlash(`${k.toUpperCase()} ${steps > 0 ? "+" : ""}${steps} %`);
}
// a focused button would also "click" on Space: swallow it, Space is STOP only
function onHotkeyUp(e) { if (e.key === " ") e.preventDefault(); }
document.addEventListener("keydown", onHotkey);
document.addEventListener("keyup", onHotkeyUp);

// ---------------------------------------------------------------- pop-out panel (2026-09-28)
// A small always-on-top window (Document Picture-in-Picture, Chrome / Edge 116+) with STOP first, ARM, the two
// levels, MA, the pattern and the link state. It is the same page's JS: the same WebSocket, send() and state; the
// page keys work in it too. While it is open it also sends the heartbeat from its own timer: a pop-out usually means
// this tab is in the background, where the browser slows timers, and the heartbeat is the deadman's control input.
let pipWin = null;
const pipHold = {};
function pipSupported() { return "documentPictureInPicture" in window; }
function pipCopyStyles(doc) {
  for (const sheet of document.styleSheets) {
    try {
      const style = doc.createElement("style");
      style.textContent = [...sheet.cssRules].map((r) => r.cssText).join("\n");
      doc.head.appendChild(style);
    } catch (err) {                       // a cross-origin sheet: link it instead
      if (sheet.href) {
        const link = doc.createElement("link");
        link.rel = "stylesheet"; link.href = sheet.href;
        doc.head.appendChild(link);
      }
    }
  }
  for (const a of document.documentElement.attributes) {
    if (a.name.startsWith("data-")) doc.documentElement.setAttribute(a.name, a.value);
  }
}
// the box's own volume knob and what reaches the pads: Master x box knob (levels come on top per channel)
function boxVolText(s) {
  const k = s && s.knob && s.knob.value != null ? s.knob.value : null;
  const m = s && s.master_set != null ? s.master_set : null;
  if (k == null) return "box knob — (not reported)";
  return m == null ? `box knob ${pct(k)}` : `box knob ${pct(k)} → ${pct(m * k)} out`;
}
function pipSlider(doc, id, label, cls, onSend) {
  const lab = doc.createElement("label");
  lab.className = "slider " + cls;
  lab.innerHTML = `<span class="lbl">${label}</span><input type="range" min="0" max="1000" value="0"><output>0%</output>`;
  const input = lab.querySelector("input"), out = lab.querySelector("output");
  const sendT = throttle(onSend, 50);
  input.addEventListener("input", () => {
    pipHold[id] = Date.now() + 700;
    localHold[id] = Date.now() + 700;       // the main page's copy follows the server, not this drag
    const v = Number(input.value) / 1000;
    out.textContent = pct(v);
    sendT(v);
  });
  return lab;
}
async function openPip() {
  if (pipWin) { pipWin.focus(); return; }
  try {
    pipWin = await window.documentPictureInPicture.requestWindow({ width: 300, height: 450 });
  } catch (err) {
    showError("could not open the pop-out: " + err.message);
    return;
  }
  const doc = pipWin.document;
  doc.title = "Player";
  pipCopyStyles(doc);
  doc.body.className = "pip-body";
  const root = doc.createElement("div");
  root.className = "pip";
  root.innerHTML =
    `<button class="stop" data-pip="stop" title="STOP: immediate zero (disarm)">STOP</button>` +
    `<div class="pip-row"><button class="arm" data-pip="arm" title="ARM: master slow-starts from 0">ARM</button>` +
    `<span class="pip-state stopped" data-pip="state">STOPPED</span><span class="pip-dot" data-pip="dot" title="link"></span></div>` +
    `<div class="pip-pattern" data-pip="pattern">—</div>`;
  const rt = doc.createElement("div");
  rt.className = "pip-routes";
  rt.innerHTML = `<button class="rev ch-a" data-pip="revA" title="Reverse which end of A leads">⇄ A</button>` +
    `<button class="rev" data-pip="swap" title="Swap A and B (each wire pair keeps its level)">A ⇆ B</button>` +
    `<button class="rev ch-b" data-pip="revB" title="Reverse which end of B leads">⇄ B</button>`;
  root.appendChild(rt);
  root.appendChild(pipSlider(doc, "levelA", "A", "ch-a", (v) => send("levels", { a: v })));
  root.appendChild(pipSlider(doc, "levelB", "B", "ch-b", (v) => send("levels", { b: v })));
  root.appendChild(pipSlider(doc, "master", "Master", "master", (v) => send("master", { value: v })));
  const bv = doc.createElement("div");
  bv.className = "pip-boxvol";
  bv.dataset.pip = "boxvol";
  root.appendChild(bv);
  root.appendChild(pipSlider(doc, "ma", "MA", "", (v) => send("ma", { value: v })));
  const foot = doc.createElement("div");
  foot.className = "pip-foot";
  foot.textContent = "Space/Esc STOP · Q/A W/S E/D R/F";
  root.appendChild(foot);
  doc.body.appendChild(root);
  root.querySelector('[data-pip="stop"]').addEventListener("click", () => send("stop"));
  root.querySelector('[data-pip="arm"]').addEventListener("click", () => send("arm"));
  root.querySelector('[data-pip="revA"]').addEventListener("click", () => send("reverse", { ch: "a" }));
  root.querySelector('[data-pip="revB"]').addEventListener("click", () => send("reverse", { ch: "b" }));
  root.querySelector('[data-pip="swap"]').addEventListener("click", () => send("swap"));
  doc.addEventListener("keydown", onHotkey);
  doc.addEventListener("keyup", onHotkeyUp);
  const hb = pipWin.setInterval(() => { if (wsOpen) ws.send(JSON.stringify({ cmd: "hb" })); }, 500);
  pipWin.addEventListener("pagehide", () => { pipWin.clearInterval(hb); pipWin = null; });
  if (st) updatePip(st);
}
function updatePip(s) {
  if (!pipWin || !s) return;
  const doc = pipWin.document, q = (k) => doc.querySelector(`[data-pip="${k}"]`);
  const eng = s.engine, armed = !!(eng && eng.armed);
  const arm = q("arm");
  if (!arm) return;
  arm.disabled = s.output === "preview" || !eng || !eng.running;
  arm.classList.toggle("armed", armed);
  arm.textContent = armed ? `ARMED ${pct(eng.master)}` : "ARM";
  const state = q("state");
  state.textContent = eng && eng.faulted ? "FAULT" : armed ? "RUNNING" : "STOPPED";
  state.className = "pip-state " + (armed ? "running" : "stopped");
  q("dot").classList.toggle("on", wsOpen && !!eng && !!eng.link && !eng.faulted);   // link = transport name or null
  const sel = $("pattern"), opt = sel.options[sel.selectedIndex];
  q("pattern").innerHTML = "";
  const b = doc.createElement("b");
  b.textContent = opt ? opt.textContent : s.pattern.id;
  q("pattern").append("Pattern ", b);
  const sliders = doc.querySelectorAll(".pip .slider");
  q("boxvol").textContent = boxVolText(s);
  [["levelA", s.levels[0]], ["levelB", s.levels[1]], ["master", s.master_set ?? 1], ["ma", s.ma]].forEach(([id, v], i) => {
    if ((pipHold[id] || 0) > Date.now()) return;
    sliders[i].querySelector("input").value = Math.round(v * 1000);
    sliders[i].querySelector("output").textContent = pct(v);
  });
}
(function initPopout() {
  const btn = $("popout");
  if (!btn) return;
  if (!pipSupported()) {
    btn.disabled = true;
    const note = $("popoutNote");
    note.textContent = "pop-out needs Chrome or Edge";
    note.hidden = false;
    return;
  }
  btn.addEventListener("click", openPip);
})();
