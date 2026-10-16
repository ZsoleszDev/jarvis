"use strict";
// A Jarvis ablaka: kapcsolat, a menük (Beszélgetés, Munka, Kreatív) naplója, kezelőszervek, varázsszem.
// A Munka menü saját része a work.js-ben, a Kreatívé a creative.js-ben van; ez a fájl adja nekik a közös
// dolgokat (Jarvis.*).

const token = new URLSearchParams(location.search).get("t") || "";
const $ = (id) => document.getElementById(id);

const logs = { chat: $("log"), work: $("w-log"), creative: $("c-notes") };
const openSeg = { chat: null, work: null, creative: null };   // a most íródó Jarvis-üzenet menünként
const tools = { chat: new Map(), work: new Map(), creative: new Map() };
let current = { state: "starting", text: "Indulok", detail: "" };
let hintText = "";
let linkDown = false;
let pendingConfirm = null;
let mode = "chat";
let workAvailable = false;
let creativeAvailable = false;
let paused = false;
let standby = false;          // készenlét: szünet, amiből egy kérés magától felébreszti
let voiceAvailable = false;
const extraHandlers = {};
const helloHooks = [];

// --- kapcsolat a Jarvisszal ---------------------------------------------------------

function post(path, body) {
  return fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Jarvis-Token": token },
    body: JSON.stringify(body || {}),
  }).catch(() => null);
}

async function postJson(path, body) {
  const response = await post(path, body);
  if (!response) return { ok: false, error: "Nincs kapcsolat a Jarvisszal." };
  try { return await response.json(); } catch { return { ok: response.ok }; }
}

function connect() {
  const events = new EventSource("/events?t=" + encodeURIComponent(token));
  events.onopen = () => { linkDown = false; };
  events.onmessage = (message) => {
    const event = JSON.parse(message.data);
    const handle = handlers[event.type] || extraHandlers[event.type];
    if (handle) handle(event);
  };
  events.onerror = () => {
    linkDown = true;
    $("state").textContent = "Nincs kapcsolat";
    $("detail").textContent = "Fut még a Jarvis? Újracsatlakozom…";
  };
}

// --- napló ------------------------------------------------------------------------

const channelOf = (event) => (logs[event.channel] ? event.channel : "chat");

function nearBottom(log) { return log.scrollHeight - log.scrollTop - log.clientHeight < 90; }

function add(kind, text, channel = "chat") {
  const log = logs[channel];
  const stick = nearBottom(log);
  if (channel === "chat") $("empty").hidden = true;
  if (channel === "work" && log.hidden && kind !== "timing") {
    unread += 1;
    $("w-unread").textContent = unread + " új";
  }
  const el = document.createElement("div");
  el.className = "msg " + kind;
  el.textContent = text;
  log.appendChild(el);
  if (channel === "creative") tidyNotes(log, el, kind);
  else if (stick) log.scrollTop = log.scrollHeight;
  return el;
}

// A Kreatív menüben a Jarvis üzenetei (pl. ha a modell leállt) a történet alatt, röviden látszanak:
// legfeljebb három, a tájékoztatók maguktól eltűnnek, a hibák kattintásra.
function tidyNotes(log, el, kind) {
  const notes = log.querySelectorAll(".msg");
  for (let i = 0; i < notes.length - 3; i++) notes[i].remove();
  el.title = "Kattints rá, hogy eltűnjön";
  el.addEventListener("click", () => el.remove());
  if (!/error/.test(kind)) setTimeout(() => el.remove(), /warn/.test(kind) ? 30000 : 12000);
}

function clearLog(channel) {
  logs[channel].querySelectorAll(".msg").forEach((node) => node.remove());
  tools[channel].clear();
  openSeg[channel] = null;
  if (channel === "chat") $("empty").hidden = false;
}

function renderExamples(examples) {
  const box = $("examples");
  box.replaceChildren();
  for (const example of examples) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = example;
    button.addEventListener("click", () => post("/api/send", { text: example, mode: "chat" }));
    box.appendChild(button);
  }
}

function setState(event) {
  current = event;
  document.body.dataset.state = event.state;
  $("state").textContent = event.text;
  let detail = event.detail || "";
  if (detail === "folytatás") detail = "Ha van még kérdésed, mondhatod";
  if (!detail && event.state === "idle") detail = hintText;
  if (!detail && event.state === "standby") detail = "Írj, és felébredek (10–20 másodperc)";
  $("detail").textContent = detail;
  $("stop").classList.toggle("active", !["idle", "starting", "error", "paused", "standby"].includes(event.state));
}

const handlers = {
  hello(e) {
    if (e.replay) { clearLog("chat"); clearLog("work"); }
    $("name").textContent = e.name || "Jarvis";
    const parts = [];
    if (e.model) parts.push(e.model);
    if (e.stt) parts.push("beszédfelismerés: " + e.stt);
    $("meta").textContent = parts.join(", ");
    voiceAvailable = !!e.voice;
    setPaused(!!e.paused, !!e.standby);
    hintText = e.wake_word ? "Szólíts meg így: „" + e.wake_word + "”"
             : e.voice ? "Nyomd meg a Hallgass meg gombot, vagy írj" : "Írd be, mit szeretnél";
    renderExamples(e.examples || []);
    workAvailable = !!e.work;
    $("tab-work").hidden = !workAvailable;
    if (!workAvailable && mode === "work") setMode("chat");
    creativeAvailable = !!e.creative;
    $("tab-creative").hidden = !creativeAvailable;
    if (!creativeAvailable && mode === "creative") setMode("chat");
    if (current.state === "idle") setState(current);
    for (const hook of helloHooks) hook(e);
  },
  user(e) { const c = channelOf(e); openSeg[c] = null; add("you", e.text, c); },
  delta(e) {
    const c = channelOf(e);
    const log = logs[c];
    const stick = nearBottom(log);
    if (!openSeg[c]) openSeg[c] = add("jarvis", "", c);
    openSeg[c].textContent += e.text;
    if (stick) log.scrollTop = log.scrollHeight;
  },
  segment_end(e) { openSeg[channelOf(e)] = null; },
  assistant(e) { const c = channelOf(e); openSeg[c] = null; add("jarvis", e.text, c); },
  tool(e) {
    const c = channelOf(e);
    let el = tools[c].get(e.id);
    if (!el) {
      openSeg[c] = null;
      el = add("tool", "", c);
      // A .sep nem látszik, de másoláskor elválasztja a lépést az eredményétől (a rácsban álló
      // elemeket a böngésző különben egybeírja: "www.alza.huAz oldal hibát adott").
      el.innerHTML = '<span class="mark" aria-hidden="true"></span><span class="label"></span>'
        + '<span class="summary"><span class="sep" aria-hidden="true"> — </span><span class="text"></span></span>';
      tools[c].set(e.id, el);
    }
    el.classList.toggle("done", e.status === "done");
    el.classList.toggle("failed", e.status === "failed");
    el.querySelector(".label").textContent = e.label;
    el.querySelector(".summary").hidden = !e.summary;
    el.querySelector(".summary .text").textContent = e.summary || "";
  },
  info(e) {
    // indulás közben a lépések a varázsszem alatt jelennek meg, nem a naplóban
    if (current.state === "starting") { $("detail").textContent = e.text; return; }
    add("note", e.text, channelOf(e));
  },
  warn(e) { add("note warn", e.text, channelOf(e)); },
  error(e) { add("note error", e.text, channelOf(e)); },
  timing(e) { add("timing", e.text, channelOf(e)); },
  state(e) {
    setState(e);
    if (e.state === "paused" || e.state === "standby") setPaused(true, e.state === "standby");
  },
  level(e) { eye.level = Math.max(eye.level, e.value); },
  confirm(e) {
    pendingConfirm = e.id;
    $("confirm-text").textContent = e.prompt;
    if (!$("confirm").open) $("confirm").showModal();
    $("deny").focus();
  },
  confirm_done(e) {
    if (pendingConfirm === e.id) {
      pendingConfirm = null;
      if ($("confirm").open) $("confirm").close();
    }
  },
};

// --- menük ------------------------------------------------------------------------

const PLACEHOLDER = {
  chat: "Írd ide a kérdésed",
  work: "Válaszolj Jarvisnak, vagy kérdezz a hirdetéseidről",
};

function setMode(next, remember = true) {
  mode = (next === "work" && workAvailable) || (next === "creative" && creativeAvailable) ? next : "chat";
  document.body.dataset.mode = mode;
  $("log").hidden = mode !== "chat";
  $("work").hidden = mode !== "work";
  $("creative").hidden = mode !== "creative";
  $("tab-chat").setAttribute("aria-selected", String(mode === "chat"));
  $("tab-work").setAttribute("aria-selected", String(mode === "work"));
  $("tab-creative").setAttribute("aria-selected", String(mode === "creative"));
  $("text").placeholder = PLACEHOLDER[mode] || PLACEHOLDER.chat;
  post("/api/mode", { mode });
  if (remember) { try { localStorage.setItem("jarvis-mode", mode); } catch { /* nem baj */ } }
  window.dispatchEvent(new CustomEvent("jarvis-mode", { detail: mode }));
}

$("tab-chat").addEventListener("click", () => setMode("chat"));
$("tab-work").addEventListener("click", () => setMode("work"));
$("tab-creative").addEventListener("click", () => setMode("creative"));

// A Munka menü naplója: széles ablakban oldalsáv, keskenyben alul; összecsukható.
let unread = 0;
function setLogOpen(open, remember = true) {
  $("w-logtoggle").setAttribute("aria-expanded", String(open));
  logs.work.hidden = !open;
  $("work").classList.toggle("log-closed", !open);
  if (open) {
    unread = 0;
    $("w-unread").textContent = "";
    logs.work.scrollTop = logs.work.scrollHeight;
  }
  if (remember) { try { localStorage.setItem("jarvis-worklog", open ? "1" : "0"); } catch { /* nem baj */ } }
}
$("w-logtoggle").addEventListener("click", () =>
  setLogOpen($("w-logtoggle").getAttribute("aria-expanded") !== "true"));
let savedLog = null;
try { savedLog = localStorage.getItem("jarvis-worklog"); } catch { /* nem baj */ }
setLogOpen(savedLog ? savedLog === "1" : matchMedia("(min-width: 900px)").matches, false);

// --- kezelőszervek ----------------------------------------------------------------

$("ask").addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = $("text").value.trim();
  if (!text) return;
  if (paused && !standby) {
    const go = await ask("Szünetelek", "Minden modell le van állítva. Folytassam (betöltöm őket, 10–20 másodperc), " +
      "és utána válaszolok?", "Folytatás");
    if (!go) return;
    await post("/api/resume");
  } else if (standby) {
    toast("Felébredek, és válaszolok (10–20 másodperc)…");
  }
  post("/api/send", { text, mode });
  $("text").value = "";
});

// --- Szünet: minden modell leáll, a videókártya és a memória felszabadul ---------------------

function setPaused(value, isStandby = false) {
  paused = value;
  standby = value && isStandby;
  $("pause").textContent = standby ? "Ébresztés" : paused ? "Folytatás" : "Szünet";
  $("pause").classList.toggle("on", paused);
  $("pause").title = standby ? "Készenlétben vagyok: betölti a modelleket (10–20 másodperc). Ha írsz, magamtól is felébredek."
    : paused ? "Újra betölti a modelleket (10–20 másodperc)"
    : "Minden modellt leállít, bezárja a böngészőt, és felszabadítja a videókártyát és a memóriát (pl. játékhoz)";
  $("listen").disabled = paused || !voiceAvailable;
}
$("pause").addEventListener("click", () => {
  if (current.state === "starting" && !paused) return;
  post(paused ? "/api/resume" : "/api/pause");
  if (!paused) { $("state").textContent = "Leállítom a modelleket"; }
});

// Egy kérdés igen/nem válasszal (a Munka menü is ezt használja).
function ask(title, text, yes = "Rendben") {
  return new Promise((resolve) => {
    const dialog = $("ask-dialog");
    $("ask-title").textContent = title;
    $("ask-text").textContent = text;
    $("ask-yes").textContent = yes;
    const finish = (value) => {
      $("ask-yes").removeEventListener("click", onYes);
      $("ask-no").removeEventListener("click", onNo);
      dialog.removeEventListener("cancel", onCancel);
      dialog.close();
      resolve(value);
    };
    const onYes = () => finish(true);
    const onNo = () => finish(false);
    const onCancel = (event) => { event.preventDefault(); finish(false); };
    $("ask-yes").addEventListener("click", onYes);
    $("ask-no").addEventListener("click", onNo);
    dialog.addEventListener("cancel", onCancel);
    dialog.showModal();
    $("ask-no").focus();
  });
}

// --- Telefon: párosítás és a párosított eszközök ----------------------------------------------

const escHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let codeTimer = null;

async function renderPhone(code) {
  let info = {};
  try {
    const response = await fetch("/api/phone?t=" + encodeURIComponent(token));
    info = await response.json();
  } catch { info = {}; }
  const body = $("phone-body");
  if (!info.enabled) {
    body.innerHTML = '<p class="muted">A telefonos kapcsolat ki van kapcsolva. Bekapcsolás: config.toml, [api] enabled = true.</p>';
    return;
  }
  const devices = info.devices || [];
  const date = (t) => (t ? new Date(t * 1000).toLocaleString("hu-HU") : "–");
  body.innerHTML = `
    <p class="muted">A telefon a Tailscale-en át éri el a Jarvist. A gépen egyszer futtasd:
      <code>tailscale serve --bg ${escHtml(info.port)}</code>, és az appban a gép Tailscale-címét add meg
      (pl. <code>https://gep-neve.tailnet.ts.net</code>).</p>
    ${code ? `<p>Írd be ezt a kódot az appban (5 percig érvényes):</p><div class="pair-code" aria-live="polite">${escHtml(code)}</div>`
           : '<div class="row"><button type="button" id="phone-pair" class="primary">Új párosító kód</button></div>'}
    <h3 class="section-title">Párosított eszközök</h3>
    ${devices.length ? `<ul class="devices">${devices.map((d) =>
      `<li><span>${escHtml(d.name)}<br><small class="muted">párosítva: ${escHtml(date(d.created))},
       utoljára: ${escHtml(date(d.last_seen))}</small></span>
       <button type="button" class="small" data-revoke="${escHtml(d.id)}">Törlés</button></li>`).join("")}</ul>`
      : '<p class="muted">Még nincs párosított eszköz.</p>'}`;
}

$("phone").addEventListener("click", () => {
  renderPhone(null);
  $("phone-dialog").showModal();
});
$("phone-close").addEventListener("click", () => { clearTimeout(codeTimer); $("phone-dialog").close(); });
$("phone-body").addEventListener("click", async (event) => {
  if (event.target.closest("#phone-pair")) {
    const result = await postJson("/api/phone/code", {});
    if (!result.ok) { toast(result.error || "Nem sikerült."); return; }
    renderPhone(result.code);
    clearTimeout(codeTimer);
    codeTimer = setTimeout(() => renderPhone(null), (result.seconds || 300) * 1000);
    return;
  }
  const revoke = event.target.closest("[data-revoke]");
  if (revoke && await ask("Eszköz törlése", "Ez az eszköz többé nem éri el a Jarvist (újra párosítani kell).", "Törlés")) {
    await postJson("/api/phone/revoke", { id: revoke.dataset.revoke });
    renderPhone(null);
    $("phone-dialog").open || $("phone-dialog").showModal();
  }
});
$("listen").addEventListener("click", () => post("/api/listen"));
$("stop").addEventListener("click", () => post("/api/stop"));
$("reset").addEventListener("click", () => { post("/api/reset", { mode }); clearLog(mode); });

function answerConfirm(approved) {
  if (pendingConfirm === null) return;
  post("/api/confirm", { id: pendingConfirm, approved });
  pendingConfirm = null;
  $("confirm").close();
}
$("allow").addEventListener("click", () => answerConfirm(true));
$("deny").addEventListener("click", () => answerConfirm(false));
$("confirm").addEventListener("cancel", (event) => { event.preventDefault(); answerConfirm(false); });

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !document.querySelector("dialog[open]")) post("/api/stop");
});

let toastTimer = null;
function toast(text) {
  let el = document.querySelector(".toast");
  if (!el) {
    el = document.createElement("div");
    el.className = "toast";
    el.setAttribute("role", "status");
    document.body.appendChild(el);
  }
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2200);
}

// --- a varázsszem ----------------------------------------------------------------------
// A régi rádiók hangolószeme: a sötét cikk annál keskenyebb, minél erősebb a jel.
// Itt a jel az, amit Jarvis éppen csinál; hallgatás közben a hangod ereje.

const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
const eye = { angle: 118, glow: 0.2, level: 0 };
const shadow = $("shadow");
const disk = $("disk");
const glass = $("glass");

(function drawTicks() {
  const ticks = $("ticks");
  for (let i = 0; i < 36; i++) {
    const a = (i * 10 - 90) * Math.PI / 180;
    const inner = i % 3 === 0 ? 70 : 73;
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", (100 + inner * Math.cos(a)).toFixed(2));
    line.setAttribute("y1", (100 + inner * Math.sin(a)).toFixed(2));
    line.setAttribute("x2", (100 + 77 * Math.cos(a)).toFixed(2));
    line.setAttribute("y2", (100 + 77 * Math.sin(a)).toFixed(2));
    ticks.appendChild(line);
  }
})();

function wedge(angle) {
  if (angle < 1) return "";
  const r = 63, c = 100;
  const a0 = (-90 - angle / 2) * Math.PI / 180;
  const a1 = (-90 + angle / 2) * Math.PI / 180;
  const large = angle > 180 ? 1 : 0;
  return `M${c},${c} L${(c + r * Math.cos(a0)).toFixed(2)},${(c + r * Math.sin(a0)).toFixed(2)} ` +
         `A${r},${r} 0 ${large} 1 ${(c + r * Math.cos(a1)).toFixed(2)},${(c + r * Math.sin(a1)).toFixed(2)} Z`;
}

function target(t) {
  const wave = (period, amp) => (reduceMotion ? 0 : amp * Math.sin(t / period));
  if (linkDown) return [170, 0.06];
  switch (current.state) {
    case "listening": return [Math.max(10, 118 - 112 * eye.level), 0.55];
    case "transcribing": return [55, 0.5];
    case "thinking": return [72 + wave(420, 30), 0.45];
    case "writing": return [48 + wave(300, 18), 0.52];
    case "working": return [58 + wave(220, 26), 0.5];
    case "speaking": return [28 + (reduceMotion ? 0 : 22 * Math.abs(Math.sin(t / 130))), 0.62];
    case "starting": return [96 + wave(700, 34), 0.18];
    case "paused": return [176, 0.04];
    case "standby": return [160 + wave(2400, 8), 0.07];
    case "error": return [170, 0.08];
    default: return [118, 0.22];
  }
}

function frame(t) {
  const [angle, glow] = target(t);
  const ease = reduceMotion ? 1 : 0.16;
  eye.angle += (angle - eye.angle) * ease;
  eye.glow += (glow - eye.glow) * (reduceMotion ? 1 : 0.08);
  eye.level *= 0.88;
  shadow.setAttribute("d", wedge(eye.angle));
  disk.style.opacity = (0.32 + eye.glow).toFixed(3);
  glass.style.filter = `drop-shadow(0 0 ${(5 + eye.glow * 22).toFixed(1)}px rgba(255, 255, 255, ${(eye.glow * 0.6).toFixed(3)}))`;
  requestAnimationFrame(frame);
}

// --- a Munka menünek (work.js) ----------------------------------------------------------

window.Jarvis = {
  token, $, post, postJson, add, toast, ask,
  get mode() { return mode; },
  get paused() { return paused; },
  get standby() { return standby; },
  setMode,
  on(type, fn) { extraHandlers[type] = fn; },
  onHello(fn) { helloHooks.push(fn); },
};

requestAnimationFrame(frame);
connect();
// A legutóbb használt menü nyíljon meg (amint a Munka menü elérhető).
let savedMode = null;
try { savedMode = localStorage.getItem("jarvis-mode"); } catch { /* nem baj */ }
helloHooks.push((e) => {
  if (savedMode === "work" && e.work) {
    if (mode !== "work") setMode("work", false);
    savedMode = null;
  } else if (savedMode === "creative" && e.creative) {
    if (mode !== "creative") setMode("creative", false);
    savedMode = null;
  }
});
$("text").focus();
