"use strict";
// A Jarvis ablaka: kapcsolat, a menük (Beszélgetés, Munka, Kreatív) naplója, kezelőszervek, varázsszem.
// A Munka menü saját része a work.js-ben, a Kreatívé a creative.js-ben van; ez a fájl adja nekik a közös
// dolgokat (Jarvis.*).

const token = new URLSearchParams(location.search).get("t") || "";
const $ = (id) => document.getElementById(id);
// Távoli ablak: a telefonon vagy a laptopon nyitották meg (a gép a Tailscale-en át adja, lásd remote.py).
// Ilyenkor a fontos dolgokról értesítés jön.
const remote = !!document.querySelector('meta[name="jarvis-remote"]');
const inAndroid = /JarvisAndroid/.test(navigator.userAgent);   // az Android-alkalmazás maga értesít

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
let live = true;              // a visszajátszott régi események után igaz: csak az újakról jön értesítés
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

// Az élő események csatornája. A távoli ablak (telefon, laptop) háttérben bezárja (jarvisSleep), és ha újra előjön,
// újranyitja (jarvisWake): a gép újracsatlakozáskor visszajátssza, ami közben történt. A telefonon ez a rádiót és a
// Tailscale-t kíméli (a nyitott csatorna életjele is felébreszti); az értesítések közben is megjönnek (az alkalmazás
// háttérszolgáltatása, a laptopon a Web Push hozza őket).
let events = null;
let sleeping = false;

function connect() {
  if (events) events.close();
  events = new EventSource("/events?t=" + encodeURIComponent(token));
  events.onopen = () => { linkDown = false; };
  events.onmessage = (message) => {
    const event = JSON.parse(message.data);
    const handle = handlers[event.type] || extraHandlers[event.type];
    if (handle) handle(event);
  };
  events.onerror = () => {
    if (sleeping) return;
    linkDown = true;
    $("state").textContent = "Nincs kapcsolat";
    $("detail").textContent = "Fut még a Jarvis? Újracsatlakozom…";
  };
}

function jarvisSleep() {
  if (!remote || sleeping) return;
  sleeping = true;
  if (events) { events.close(); events = null; }
}

function jarvisWake() {
  clearTimeout(sleepTimer);
  if (!sleeping) return;
  sleeping = false;
  connect();
  pollGpus();
}
window.jarvisSleep = jarvisSleep;    // az Android-alkalmazás hívja, amikor háttérbe kerül, illetve előjön
window.jarvisWake = jarvisWake;
// A böngészőben (laptop, telefonos böngésző): ha az ablak HIDDEN_SLEEP ideig nem látszik, a csatorna bezárul.
const HIDDEN_SLEEP = 5 * 60 * 1000;
let sleepTimer = null;
if (remote) {
  document.addEventListener("visibilitychange", () => {
    clearTimeout(sleepTimer);
    if (document.hidden) sleepTimer = setTimeout(jarvisSleep, HIDDEN_SLEEP);
    else jarvisWake();
  });
}

// --- napló ------------------------------------------------------------------------

const channelOf = (event) => (logs[event.channel] ? event.channel : "chat");

function nearBottom(log) { return log.scrollHeight - log.scrollTop - log.clientHeight < 90; }

function add(kind, text, channel = "chat") {
  const log = logs[channel];
  const stick = nearBottom(log);
  if (channel === "chat" && kind === "you") $("empty").hidden = true;   // a csempék az első kérdésedig látszanak
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

function setState(event) {
  current = event;
  document.body.dataset.state = event.state;
  $("state").textContent = event.text;
  let detail = event.detail || "";
  if (!detail && event.state === "idle") detail = hintText;
  if (!detail && event.state === "standby") detail = "Írj, és felébredek (10–20 másodperc)";
  $("detail").textContent = detail;
  // Az Állj a Küldés mellett csak akkor látszik, amikor Jarvis épp dolgozik (Esc is leállítja).
  $("stop").hidden = ["idle", "starting", "error", "paused", "standby"].includes(event.state);
}

const handlers = {
  hello(e) {
    if (e.replay) { clearLog("chat"); clearLog("work"); live = false; }
    $("name").textContent = e.name || "Jarvis";
    $("meta").textContent = e.model || "";
    setPaused(!!e.paused, !!e.standby);
    hintText = "Írd be, mit szeretnél";
    workAvailable = !!e.work;
    $("tab-work").hidden = !workAvailable;
    if (!workAvailable && mode === "work") setMode("chat");
    creativeAvailable = !!e.creative;
    $("tab-creative").hidden = !creativeAvailable;
    if (!creativeAvailable && mode === "creative") setMode("chat");
    if (current.state === "idle") setState(current);
    loadNotices();
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
  assistant(e) {
    const c = channelOf(e);
    openSeg[c] = null;
    add("jarvis", e.text, c);
    if (live && document.hidden) systemNotify("Jarvis", e.text, "jarvis-reply");
  },
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
    live = true;              // a visszajátszás végén mindig jön egy állapot
    if (e.state === "paused" || e.state === "standby") setPaused(true, e.state === "standby");
  },
  notification(e) {
    const notice = e.notice || {};
    systemNotify(notice.title || "Jarvis", notice.text || "", "notice-" + (notice.id || ""));
    addNotice(notice);
  },
  confirm(e) {
    pendingConfirm = e.id;
    $("confirm-text").textContent = e.prompt;
    if (!$("confirm").open) $("confirm").showModal();
    $("deny").focus();
    if (document.hidden) systemNotify("Jóváhagyás kell", e.prompt, "confirm-" + e.id);
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

// pairing: az épp érvényes párosító kód (code, és ha a gép címe ismert, a QR-kódja: qr), vagy null.
async function renderPhone(pairing) {
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
  const where = info.address
    ? "<p>A gép címe, ezt add meg a telefonon és a laptopon:</p>" +
      `<p class="address"><code>${escHtml(info.address)}</code></p>` +
      '<p class="muted">Telefon: a Jarvis alkalmazásban (ha még nincs meg, nyisd meg ezt a címet a telefon ' +
      "böngészőjében: ott a letöltés linkje). Laptop: nyisd meg az Edge-ben, és telepítsd alkalmazásként.</p>"
    : '<p class="muted">A telefon és a laptop a Tailscale-en át éri el a Jarvist. A gépen egyszer futtasd: ' +
      `<code>tailscale serve --bg ${escHtml(info.port)}</code>, és az alkalmazásban a gép Tailscale-címét add ` +
      "meg (pl. <code>https://gep-neve.tailnet.ts.net</code>).</p>";
  body.innerHTML = `
    ${where}
    ${pairing ? pairingBlock(pairing)
           : '<div class="row"><button type="button" id="phone-pair" class="primary">Új párosító kód</button></div>'}
    <h3 class="section-title">Párosított eszközök</h3>
    ${devices.length ? `<ul class="devices">${devices.map((d) =>
      `<li><span>${escHtml(d.name)}<br><small class="muted">párosítva: ${escHtml(date(d.created))},
       utoljára: ${escHtml(date(d.last_seen))}</small></span>
       <button type="button" class="small" data-revoke="${escHtml(d.id)}">Törlés</button></li>`).join("")}</ul>`
      : '<p class="muted">Még nincs párosított eszköz.</p>'}`;
}

// A QR-kódot (a gép állítja elő, remote.pair_link + qr.svg) a telefon kamerája olvassa be: megnyitja a gép címét a
// kóddal, és ott (vagy az Android-alkalmazásban) egy gombnyomással párosít.
function pairingBlock(pairing) {
  const code = `<div class="pair-code" aria-live="polite">${escHtml(pairing.code)}</div>`;
  if (!pairing.qr) return `<p>Írd be ezt a kódot a telefonon vagy a laptopon (5 percig érvényes):</p>${code}`;
  return `<div class="pair-qr">${pairing.qr}</div>` +
    "<p>Olvasd be a telefon kamerájával (5 percig érvényes). Laptopon vagy kézzel ezt a kódot írd be:</p>" + code;
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
    renderPhone(result);
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

// --- Automatikus munkák: a figyelők és a háttérmunka ---------------------------------------------
// Itt látod, mit néz meg Jarvis magától, időnként; amire nincs szükséged, törölheted vagy kikapcsolhatod.

let jobsInfo = null;

function everyText(minutes) {
  if (minutes % 1440 === 0) return minutes === 1440 ? "naponta" : `${minutes / 1440} naponta`;
  if (minutes % 60 === 0) return minutes === 60 ? "óránként" : `${minutes / 60} óránként`;
  return `${Math.round(minutes)} percenként`;
}

const shortDate = (t) => new Date(t * 1000).toLocaleString("hu-HU",
  { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

function watchState(watch) {
  if (watch.error) return "legutóbb nem sikerült: " + watch.error;
  if (!watch.checks) return "még nem néztem meg";
  const hits = (watch.hits || []).length;
  return `utoljára ${shortDate(watch.last_check)} · ` + (hits ? `${hits} új találat eddig` : "még nem volt új találat");
}

function jobState(job) {
  if (!job.every_minutes) return "a config.toml-ban ki van kapcsolva";
  if (!job.on) return "kikapcsolva: magától nem nézi meg";
  const parts = [job.note || everyText(job.every_minutes)];
  if (job.last_run) parts.push("utoljára " + shortDate(job.last_run));
  if (!job.available) parts.push("most nincs mit néznie");
  return parts.join(" · ");
}

async function renderJobs(info) {
  if (!info) {
    try {
      const response = await fetch("/api/jobs?t=" + encodeURIComponent(token));
      info = await response.json();
    } catch { info = {}; }
  }
  const body = $("jobs-body");
  jobsInfo = info;
  if (!info.ok || !info.ready) {
    body.innerHTML = '<p class="muted">A háttérmunka még indul; nézz vissza pár másodperc múlva.</p>';
    return;
  }
  const watches = info.watches || [];
  const jobs = info.jobs || [];
  const none = info.search ? "A beszélgetésben kérhetsz egyet, pl. „szólj, ha lesz új Python-állás Debrecenben”."
    : "A figyelőkhöz webes keresés kell (pip install ddgs).";
  // (a párbeszédablak bekezdései megtartják a sortörést, ezért egy sorban)
  body.innerHTML = `
    <p class="muted">Amit Jarvis magától, időnként megnéz, és szól, ha talál valamit. Amire nincs szükséged, töröld, vagy kapcsold ki.</p>
    ${info.enabled ? "" : '<p class="muted">A háttérmunka most ki van kapcsolva a config.toml-ban ([scheduler] enabled).</p>'}
    <h3 class="section-title">Figyelők</h3>
    ${watches.length ? `<ul class="devices">${watches.map((w) => `
      <li><span>${escHtml(w.label)}<br><small class="muted">${escHtml(watchState(w))}</small></span>
        <button type="button" class="small" data-watch="${escHtml(w.id)}">Törlés</button></li>`).join("")}</ul>
      ${watches.length > 1 ? '<div class="row"><button type="button" class="small quiet" id="watch-all">Mind törlése</button></div>' : ""}`
      : `<p class="muted">Nincs figyelőd. ${none}</p>`}
    <h3 class="section-title">Időnként magától</h3>
    <div class="jobs">${jobs.map((j) => `
      <label class="check"><input type="checkbox" data-job="${escHtml(j.key)}"${j.on ? " checked" : ""}${j.every_minutes ? "" : " disabled"}>
        <span>${escHtml(j.title)}<br><small class="muted">${escHtml(jobState(j))}</small></span></label>`).join("")}</div>`;
}

$("jobs").addEventListener("click", () => {
  renderJobs();
  $("jobs-dialog").showModal();
});
$("jobs-close").addEventListener("click", () => $("jobs-dialog").close());
$("jobs-body").addEventListener("click", async (event) => {
  const one = event.target.closest("[data-watch]");
  if (!one && !event.target.closest("#watch-all")) return;
  const watches = (jobsInfo && jobsInfo.watches) || [];
  const ids = one ? [one.dataset.watch] : watches.map((w) => w.id);
  const watch = watches.find((w) => w.id === ids[0]);
  const sure = one
    ? await ask("Figyelő törlése", `${watch ? watch.label : "Ezt a figyelőt"} többé nem nézem meg.`, "Törlés")
    : await ask("Minden figyelő törlése", `${ids.length} figyelőt törlök; ezeket többé nem nézem meg.`, "Mind törlése");
  if (!sure) return;
  const result = await postJson("/api/jobs/watch-delete", { ids });
  if (!result.ok) { toast(result.error || "Nem sikerült."); renderJobs(); return; }
  toast(ids.length > 1 ? "Figyelők törölve" : "Figyelő törölve");
  renderJobs(result);
  $("jobs-close").focus();
});
$("jobs-body").addEventListener("change", async (event) => {
  const box = event.target.closest("[data-job]");
  if (!box) return;
  const result = await postJson("/api/jobs/toggle", { key: box.dataset.job, on: box.checked });
  if (!result.ok) { toast(result.error || "Nem sikerült."); box.checked = !box.checked; return; }
  jobsInfo = result;
  const job = (result.jobs || []).find((j) => j.key === box.dataset.job);
  if (job) {
    box.checked = job.on;
    box.closest("label").querySelector("small").textContent = jobState(job);
  }
});

// --- Értesítések: amiről Jarvis a háttérben szólt ---------------------------------------------------
// Pl. egy elkeltnek látszó hirdetés: a gombja („Eladva itt, vedd le a többiről”) a hirdetés Eladva ablakát nyitja
// meg, előre kiválasztva; a levétel ott is csak a Rendben gombra indul. A laptop push-értesítésének és az
// Android-alkalmazás értesítésének gombja ugyanide visz (noticeAction).

let noticeList = [];
let noticesUnread = 0;
const NOTICE_ACTIONS = ["sold", "login", "captcha", "deals", "offer", "deal", "ledger", "todos", "item", "review"];

function setNoticesUnread(count) {
  noticesUnread = Math.max(0, count);
  $("notices-unread").textContent = noticesUnread ? String(noticesUnread) : "";
  $("notices").classList.toggle("on", noticesUnread > 0);
}

async function loadNotices() {
  try {
    const response = await fetch("/api/notices?t=" + encodeURIComponent(token));
    const data = await response.json();
    if (!data.ok) return;
    noticeList = data.notifications || [];
    setNoticesUnread(data.unread || 0);
  } catch { /* a Jarvis még indul: a következő "hello" újra lekéri */ }
}

function addNotice(notice) {
  if (!notice.id || noticeList.some((n) => n.id === notice.id)) return;
  window.dispatchEvent(new CustomEvent("jarvis-notice", { detail: notice }));   // pl. a Munka menü frissíti a listáját
  noticeList = [notice, ...noticeList].slice(0, 50);
  if (!notice.read) setNoticesUnread(noticesUnread + 1);   // a nem fontos (quiet) olvasottként jön: nem jelez
  if ($("notices-dialog").open) renderNotices();
}

const noticeButtons = (notice) => (notice.actions || []).filter((a) => NOTICE_ACTIONS.includes(a.action));

function renderNotices() {
  const body = $("notices-body");
  if (!noticeList.length) {
    body.innerHTML = '<p class="muted">Még nincs értesítés. Itt látod, ha Jarvis a háttérben talál valamit (pl. egy ' +
      "figyelő új találatát, vagy egy elkeltnek látszó hirdetést).</p>";
    return;
  }
  // a később helyesbített (pl. utólag mégsem jó) értesítés áthúzva, a helyesbítés címével
  body.innerHTML = `<ul class="notices">${noticeList.map((n, i) => `
    <li${n.read ? "" : ' class="new"'}><span>${n.superseded ? `<s>${escHtml(n.title)}</s>` : `<b>${escHtml(n.title)}</b>`}
      <small class="muted">${n.at ? escHtml(shortDate(n.at)) : ""}</small></span>
      ${n.superseded ? `<small class="warn">Helyesbítve: ${escHtml(n.superseded)}</small>` : ""}
      <span class="text">${escHtml(n.text)}</span>
      ${noticeButtons(n).length ? `<span class="row">${noticeButtons(n).map((a, j) =>
        `<button type="button" class="small" data-notice="${i}" data-act="${j}">${escHtml(a.label)}</button>`).join("")}</span>` : ""}
    </li>`).join("")}</ul>`;
}

$("notices").addEventListener("click", async () => {
  await loadNotices();
  renderNotices();
  $("notices-dialog").showModal();
  $("notices-close").focus();
  const ids = noticeList.filter((n) => !n.read).map((n) => n.id);
  if (!ids.length) return;
  const result = await postJson("/api/notices/read", { ids });
  if (!result.ok) return;
  for (const n of noticeList) if (ids.includes(n.id)) n.read = true;
  setNoticesUnread(noticesUnread - (result.marked ?? ids.length));
});
$("notices-close").addEventListener("click", () => $("notices-dialog").close());
$("notices-body").addEventListener("click", (event) => {
  const button = event.target.closest("[data-notice]");
  if (!button) return;
  const notice = noticeList[Number(button.dataset.notice)];
  const wanted = notice && noticeButtons(notice)[Number(button.dataset.act)];
  if (!wanted) return;
  $("notices-dialog").close();
  noticeAction({ ...wanted, item: notice.item || "" });
});

// Egy értesítés gombja: a Munka menü (work.js) megnyitja hozzá a lapot. Magától semmit nem jelöl meg és nem vesz le.
function noticeAction(wanted) {
  if (!wanted || !NOTICE_ACTIONS.includes(wanted.action)) return;
  if (wanted.action === "captcha") { openCaptcha(); return; }
  if (!workAvailable) { toast("A Munka menü most nem érhető el a gépen."); return; }
  setMode("work");
  window.dispatchEvent(new CustomEvent("jarvis-notice-action", { detail: wanted }));
}

// Captcha a telefonon: a Jarvis böngészőjének képe (pár másodpercenként frissül), és amire böksz, oda kattint a
// gépen. Ez a te kattintásod: a captchát te oldod meg, a Jarvis magától soha nem kattint rá.
let captchaTimer = null;
function captchaRefresh() {
  $("captcha-screen").src = "/api/browser/screen?t=" + encodeURIComponent(token) + "&n=" + Date.now();
}
function openCaptcha() {
  captchaRefresh();
  clearInterval(captchaTimer);
  captchaTimer = setInterval(captchaRefresh, 2000);
  $("captcha-dialog").showModal();
}
function closeCaptcha() {
  clearInterval(captchaTimer);
  captchaTimer = null;
  if ($("captcha-dialog").open) $("captcha-dialog").close();
}
$("captcha-screen").addEventListener("error", () => toast("A böngésző képe most nem érhető el (lehet, hogy a Jarvis épp dolgozik)."));
$("captcha-screen").addEventListener("click", async (event) => {
  const box = event.target.getBoundingClientRect();
  const result = await postJson("/api/browser/tap", { x: (event.clientX - box.left) / box.width,
                                                       y: (event.clientY - box.top) / box.height });
  if (!result.ok) { toast(result.error || "Nem sikerült a kattintás."); return; }
  captchaRefresh();
  if (result.captcha === false) toast("A captcha eltűnt. A Kész után az Újra gombbal folytatom.");
});
$("captcha-close").addEventListener("click", closeCaptcha);
$("captcha-dialog").addEventListener("close", closeCaptcha);

// A push-értesítés gombja bezárt ablaknál ezzel a címmel nyitja meg az ablakot: /#act=sold&item=…&where=…&delist=1
// (remote.py, service worker). Nyitott ablakban a service worker üzenetben szól.
let hashAction = null;
if (/^#act=/.test(location.hash)) {
  const wanted = new URLSearchParams(location.hash.slice(1));
  hashAction = { action: wanted.get("act"), item: wanted.get("item") || "", where: wanted.get("where") || "",
                 platform: wanted.get("platform") || "", delist: wanted.get("delist") !== "0", deal: wanted.get("deal") || "" };
  history.replaceState(null, "", location.pathname + location.search);
}
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.addEventListener("message", (event) => {
    if (event.data && event.data.type === "jarvis-action") noticeAction(event.data.action);
  });
}

// Rendszerértesítés, ha az ablak épp nem látszik (a laptopon; az Android-alkalmazás maga értesít).
const notifiable = remote && !inAndroid && "Notification" in window;

function systemNotify(title, body, tag) {
  if (!notifiable || Notification.permission !== "granted" || !document.hidden) return;
  const options = { body: String(body || "").slice(0, 240), tag, icon: "/icon-192.png" };
  const worker = navigator.serviceWorker;
  if (worker && worker.controller) {
    worker.ready.then((registration) => registration.showNotification(title, options)).catch(() => {});
  } else {
    try { new Notification(title, options); } catch { /* pl. a böngésző nem engedi */ }
  }
}

// Web Push: a gép akkor is értesít, ha az ablak be van zárva (webpush.py). A feliratkozás az eszköz kulcsával
// megy a gépre; a böngésző push-szolgáltatása (Edge-nél a Microsofté, Chrome-nál a Google-é) csak titkosítva
// kapja meg, amit a gép küld.
const pushable = notifiable && "serviceWorker" in navigator && "PushManager" in window;

function keyBytes(text) {
  const raw = atob(text.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (text.length % 4)) % 4));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

function sameKey(subscription, key) {
  const current = subscription.options && subscription.options.applicationServerKey;
  if (!current) return false;
  const a = new Uint8Array(current);
  return a.length === key.length && a.every((value, i) => value === key[i]);
}

async function enablePush(explicit) {
  if (!pushable || Notification.permission !== "granted") return;
  const auth = { Authorization: "Bearer " + token };
  try {
    const info = await (await fetch("/api/v1/push", { headers: auth })).json();
    if (!info.ok || !info.key) {
      if (explicit && info.reason) toast(info.reason);
      return;
    }
    const key = keyBytes(info.key);
    const registration = await navigator.serviceWorker.ready;
    let subscription = await registration.pushManager.getSubscription();
    if (subscription && !sameKey(subscription, key)) {     // a gép kulcsa változott: új feliratkozás kell
      await subscription.unsubscribe();
      subscription = null;
    }
    if (!subscription) {
      subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
    }
    const response = await fetch("/api/v1/push/subscribe", {
      method: "POST", headers: { ...auth, "Content-Type": "application/json" },
      body: JSON.stringify({ subscription: subscription.toJSON() }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) { if (explicit) toast(result.error || "A gép nem fogadta el a feliratkozást."); return; }
    if (explicit) toast("Kész: a Jarvis akkor is szól, ha az ablak be van zárva.");
  } catch {
    if (explicit) toast("Ez a böngésző most nem tud értesítést fogadni bezárt ablaknál; nyitott ablaknál szólok.");
  }
}

if (remote) {
  $("phone").hidden = true;   // új eszközt csak a gép előtt ülve lehet párosítani
  if (!inAndroid && "serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  if (notifiable && Notification.permission === "default") {
    $("notify").hidden = false;
    $("notify").addEventListener("click", async () => {
      const answer = await Notification.requestPermission().catch(() => "denied");
      $("notify").hidden = true;
      if (answer === "granted") enablePush(true);
    });
  }
  enablePush(false);          // ha már engedélyezted: a feliratkozás frissítése (pl. a gép új kulcsa után)
}
$("stop").addEventListener("click", () => post("/api/stop"));
// A Munka menüben a kérdezősor alapból rejtve (a kártyákon úgyis válaszolhatsz): a gombbal nyílik.
$("ask-toggle").addEventListener("click", () => {
  const open = !document.body.classList.contains("ask-open");
  document.body.classList.toggle("ask-open", open);
  $("ask-toggle").setAttribute("aria-expanded", String(open));
  if (open) $("text").focus();
});
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
// Itt a jel az, amit Jarvis éppen csinál.

const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
const eye = { angle: 118, glow: 0.2 };
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
    case "thinking": return [72 + wave(420, 30), 0.45];
    case "writing": return [48 + wave(300, 18), 0.52];
    case "working": return [58 + wave(220, 26), 0.5];
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
  shadow.setAttribute("d", wedge(eye.angle));
  disk.style.opacity = (0.32 + eye.glow).toFixed(3);
  glass.style.filter = `drop-shadow(0 0 ${(5 + eye.glow * 22).toFixed(1)}px rgba(255, 255, 255, ${(eye.glow * 0.6).toFixed(3)}))`;
  // A GPU-sávok gördülése: másodpercenként kb. 12 kép elég, hogy folyamatosnak lássék.
  if (gpuStrips.size && !reduceMotion && t - gpuDrawn > 80) {
    gpuDrawn = t;
    drawGpus();
  }
  requestAnimationFrame(frame);
}

// --- videókártyák: kártyánként egy vékony sáv a fejlécben ------------------------------------------
// Az utolsó perc terhelése gördülő görbeként, mint a Windows Feladatkezelőjében. A terhelést a fényerő
// mutatja, nem szín: minél jobban dolgozik a kártya, annál világosabb a vonal. Fölé víve az egeret (vagy
// billentyűzettel ráállva) látszanak a pontos értékek: terhelés, VRAM, hőmérséklet.

const GPU_SPAN = 60;                 // ennyi másodpercet mutat egy sáv
const gpuStrips = new Map();         // a kártya sorszáma → {el, canvas, gpu}
let gpuInterval = 1;                 // a mérés üteme másodpercben (a Jarvis mondja meg)
let gpuClock = 0;                    // a Jarvis órája mínusz a böngészőé, másodpercben
let gpuTipFor = null;                // a sáv, amelyiknek a részletei épp látszanak
let gpuTimer = null;
let gpuDrawn = 0;

const decimal = (value) => value.toFixed(1).replace(".", ",");

function vramText(usedMb, totalMb) {
  // A kártya névleges mérete (16 GB), ha a driver szerinti csak kicsit kevesebb (15,9).
  const total = totalMb / 1024;
  const nominal = Math.round(total);
  const shown = nominal && Math.abs(total - nominal) <= nominal * 0.03 ? String(nominal) : decimal(total);
  return decimal(usedMb / 1024) + " / " + shown + " GB";
}

function gpuText(gpu) {
  const last = gpu.samples[gpu.samples.length - 1];
  const parts = [gpu.name];
  if (last) {
    const [, load, used, temp] = last;
    parts.push(load === null ? "terhelés: nem mérhető" : "terhelés " + load + "%");
    parts.push("VRAM " + vramText(used, gpu.total_mb));
    if (temp !== null) parts.push(temp + " °C");
  }
  return parts.join(" · ");
}

function showGpuTip() {
  const tip = $("gpu-tip");
  if (!gpuTipFor) { tip.hidden = true; return; }
  tip.textContent = gpuText(gpuTipFor.gpu);
  tip.hidden = false;
  const box = gpuTipFor.el.getBoundingClientRect();
  const left = Math.min(Math.max(8, box.left + box.width / 2 - tip.offsetWidth / 2), innerWidth - tip.offsetWidth - 8);
  tip.style.left = left + "px";
  tip.style.top = box.bottom + 4 + "px";
}

function gpuStrip(gpu) {
  let strip = gpuStrips.get(gpu.index);
  if (!strip) {
    const el = document.createElement("div");
    el.className = "gpu";
    el.tabIndex = 0;
    el.setAttribute("role", "img");
    el.setAttribute("aria-describedby", "gpu-tip");
    const canvas = document.createElement("canvas");
    el.appendChild(canvas);
    $("gpus").appendChild(el);
    strip = { el, canvas, gpu };
    gpuStrips.set(gpu.index, strip);
    const show = () => { gpuTipFor = strip; showGpuTip(); };
    const hide = () => { if (gpuTipFor === strip) { gpuTipFor = null; showGpuTip(); } };
    el.addEventListener("mouseenter", show);
    el.addEventListener("focus", show);
    el.addEventListener("mouseleave", hide);
    el.addEventListener("blur", hide);
  }
  strip.gpu = gpu;
  strip.el.setAttribute("aria-label", gpu.name + " terhelése az utolsó percben");
  return strip;
}

function renderGpus(info) {
  gpuInterval = info.interval || 1;
  gpuClock = (info.now || Date.now() / 1000) - Date.now() / 1000;
  const seen = new Set();
  for (const gpu of info.gpus || []) {
    gpuStrip(gpu);
    seen.add(gpu.index);
  }
  for (const [index, strip] of gpuStrips) {
    if (seen.has(index)) continue;
    strip.el.remove();
    gpuStrips.delete(index);
    if (gpuTipFor === strip) gpuTipFor = null;
  }
  $("gpus").hidden = gpuStrips.size === 0;
  showGpuTip();
  if (reduceMotion) drawGpus();
}

async function pollGpus() {
  if (document.hidden || gpuTimer === null || sleeping) return;
  let info = null;
  try {
    const response = await fetch("/api/gpus?t=" + encodeURIComponent(token));
    if (response.ok) info = await response.json();
  } catch { info = null; }
  if (!info) return;                 // pl. épp újraindul a Jarvis: a következő körben újra
  if (!info.available) {             // nincs NVIDIA-kártya, vagy ki van kapcsolva ([gui] gpu_bars)
    clearInterval(gpuTimer);
    gpuTimer = null;
    $("gpus").hidden = true;
    return;
  }
  renderGpus(info);
}

function drawGpus() {
  // Egy ütemmel lemaradva rajzol, így a görbe vége nem ugrál, amikor megjön a következő mérés.
  const now = Date.now() / 1000 + gpuClock - gpuInterval;
  for (const strip of gpuStrips.values()) drawGpu(strip, now);
}

function drawGpu(strip, now) {
  const canvas = strip.canvas;
  const ratio = window.devicePixelRatio || 1;
  const width = Math.round(canvas.clientWidth * ratio);
  const height = Math.round(canvas.clientHeight * ratio);
  if (!width || !height) return;
  if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, width, height);
  // halvány alapvonal: üresjáratban is látszik, hogy ott a kártya
  ctx.fillStyle = "rgba(255, 255, 255, 0.12)";
  ctx.fillRect(0, height - ratio, width, ratio);
  const xOf = (t) => width - ((now - t) / GPU_SPAN) * width;
  const yOf = (load) => height - ratio / 2 - load * (height - ratio);
  const points = strip.gpu.samples.filter((s) => s[1] !== null).map((s) => [xOf(s[0]), s[1] / 100]);
  if (!points.length) return;
  const last = points[points.length - 1];
  if (last && last[0] < width && last[0] > width - (3 * gpuInterval / GPU_SPAN) * width) {
    points.push([width, last[1]]);   // az utolsó érték a sáv végéig
  }
  // A mérési hézagoknál (pl. amíg az ablak a háttérben volt) a görbe megszakad.
  const gap = (4 * gpuInterval / GPU_SPAN) * width;
  const runs = [];
  let run = [];
  for (const point of points) {
    if (run.length && point[0] - run[run.length - 1][0] > gap) { runs.push(run); run = []; }
    run.push(point);
  }
  runs.push(run);
  // A fényerő egy vízszintes színátmenet: mintánként világosabb, ahol a kártya jobban dolgozott.
  const fill = ctx.createLinearGradient(0, 0, width, 0);
  const line = ctx.createLinearGradient(0, 0, width, 0);
  for (const [x, load] of points) {
    const at = Math.min(1, Math.max(0, x / width));
    fill.addColorStop(at, `rgba(255, 255, 255, ${(0.05 + 0.25 * load).toFixed(3)})`);
    line.addColorStop(at, `rgba(255, 255, 255, ${(0.3 + 0.7 * load).toFixed(3)})`);
  }
  ctx.beginPath();
  for (const part of runs.filter((r) => r.length > 1)) {
    ctx.moveTo(part[0][0], height);
    for (const [x, load] of part) ctx.lineTo(x, yOf(load));
    ctx.lineTo(part[part.length - 1][0], height);
    ctx.closePath();
  }
  ctx.fillStyle = fill;
  ctx.fill();
  ctx.beginPath();
  for (const part of runs.filter((r) => r.length > 1)) {
    ctx.moveTo(part[0][0], yOf(part[0][1]));
    for (const [x, load] of part.slice(1)) ctx.lineTo(x, yOf(load));
  }
  ctx.strokeStyle = line;
  ctx.lineWidth = ratio;
  ctx.lineJoin = "round";
  ctx.stroke();
}

// a távoli ablakban (telefon, laptop) ritkábban: minden lekérdezés adatforgalom, és felébreszti a Tailscale-t
gpuTimer = setInterval(pollGpus, remote ? 5000 : 1000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) pollGpus(); });

// --- az Android-alkalmazás kérései ----------------------------------------------------------
// Hangutasítás (parancsikon, gyorsbeállítás csempe, az értesítés gombja), megosztott szöveg vagy fotók, új hirdetés,
// és egy elkeltnek látszó hirdetés értesítésének gombja (a hirdetés Eladva ablaka).
// Az alkalmazás tárolja őket; mi kérdezzük le (indításkor, és amikor az alkalmazás szól: window.JarvisPending).

function takePending() {
  if (!inAndroid || !window.JarvisApp || !window.JarvisApp.pending) return;
  let wanted = {};
  try { wanted = JSON.parse(window.JarvisApp.pending()); } catch { wanted = {}; }
  if (wanted.text) {
    setMode("chat");
    $("text").value = wanted.text;
    $("text").focus();
  }
  if (wanted.photos || wanted.listing) {
    if (workAvailable) {
      setMode("work");
      window.dispatchEvent(new CustomEvent("jarvis-share", { detail: wanted }));
    } else {
      toast("A Munka menü most nem érhető el a gépen.");
    }
  }
  if (wanted.sold) noticeAction({ action: "sold", ...wanted.sold });   // az értesítés „Eladva itt…” gombja
  if (wanted.deal) noticeAction({ action: "deal", deal: wanted.deal });   // egy megérő hirdetés értesítése: a találat ablaka
  // egy tárgy értesítése: a kérdése (item), vagy a Pro feltöltés Nézd át ablaka (review)
  if (wanted.item && wanted.item.id) noticeAction({ action: wanted.item.open === "review" ? "review" : "item", item: wanted.item.id });
}
window.JarvisPending = takePending;

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
pollGpus();
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
helloHooks.push(() => takePending());   // a menü visszaállítása után: egy megosztás oda visz, ahova kell
helloHooks.push(() => {                 // a push-értesítés gombja (a cím #act= része), egyszer
  if (hashAction && workAvailable) { noticeAction(hashAction); hashAction = null; }
});
$("text").focus();
