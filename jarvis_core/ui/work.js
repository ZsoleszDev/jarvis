"use strict";
// Munka → Adás/Vétel: kipipálod, hova kerüljön, hozzáadod a fotókat, leírod, mit adsz el, és Jarvis
// megcsinálja. Ha valami hiányzik, a kártyán (és a naplóban) visszakérdez. Minden változás a
// Jarvisnál történik; ez az oldal csak megjeleníti és kéri (Jarvis.* az app.js-ből).
(() => {
  const J = window.Jarvis;
  const { $, token } = J;
  const items = new Map();
  const uploading = new Map();          // tárgy -> "Fotók feltöltése 2/5…"
  let platforms = [];
  let needs = [];
  let settings = {};
  let parcelSizes = [];
  let acknowledged = new Set();
  let researchOn = false;
  let browserOn = false;
  let conditions = [];
  let maxPhotos = 20;
  let filter = "all";
  let openId = null;
  let composePhotos = [];               // a még fel nem küldött hirdetés fotói (File)
  let pickFor = null;                   // a fájlválasztó kinek választ: "compose" vagy egy tárgy
  const area = $("w-area");

  const POSTED = "feltöltve";
  const FAILED = "hiba";
  const STATUS_CLASS = { "vázlat": "vazlat", "kérdés": "kerdes", "árdöntés": "kerdes",
                         "részben fent": "kesz", "fent": "feltoltve", "eladva": "eladva" };
  const FILTERS = [["all", "Mind"], ["todo", "Teendő"], ["up", "Fent van"], ["sold", "Eladva"]];

  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const price = (value) => (value === null || value === undefined || value === "") ? ""
    : String(Math.round(Number(value))).replace(/\B(?=(\d{3})+(?!\d))/g, " ") + " Ft";
  const photoUrl = (id, name, thumb) =>
    `/api/work/photo/${id}/${name}?t=${encodeURIComponent(token)}${thumb ? "&thumb=1" : ""}`;
  const safeUrl = (url) => (/^https?:\/\//i.test(url || "") ? url : "");
  const siteName = (key) => (platforms.find((p) => p.key === key) || { name: key }).name;
  const siteState = (item, key) => (item.platforms[key] || {}).status || "";
  const store = (key, value) => { try { localStorage.setItem(key, value); } catch { /* nem baj */ } };
  const recall = (key) => { try { return localStorage.getItem(key); } catch { return null; } };

  function matches(item) {
    if (filter === "sold") return item.sold;
    if (filter === "up") return !item.sold && (item.status === "fent" || item.status === "részben fent");
    if (filter === "todo") {
      return !item.sold && (item.status !== "fent" ||
        item.targets.some((t) => siteState(item, t) === FAILED));
    }
    return true;
  }

  // --- adatok -----------------------------------------------------------------------

  async function load() {
    try {
      const response = await fetch("/api/work/items?t=" + encodeURIComponent(token));
      if (!response.ok) return;
      const data = await response.json();
      items.clear();
      for (const item of data.items || []) items.set(item.id, item);
      platforms = data.platforms || [];
      needs = data.needs || [];
      settings = data.settings || {};
      parcelSizes = data.parcel_sizes || [];
      acknowledged = new Set(data.acknowledged || []);
      researchOn = !!data.research;
      browserOn = !!data.browser;
      conditions = data.conditions || [];
      maxPhotos = data.max_photos || 20;
      renderCompose();
      renderSetup();
      render();
    } catch { /* a Jarvis még indul: a következő "hello" újra betölti */ }
  }

  function remember(result, fallback) {
    if (result && result.ok && result.item) {
      items.set(result.item.id, result.item);
      render();
      return true;
    }
    J.toast((result && result.error) || fallback);
    return false;
  }

  const update = async (id, fields) => {
    if (!remember(await J.postJson("/api/work/update", { id, fields }), "Nem sikerült menteni.")) load();
  };

  async function action(name, id, extra = {}) {
    const result = await J.postJson("/api/work/action", { action: name, ids: id ? [id] : [], ...extra });
    if (!result.ok) J.toast(result.error || "Nem sikerült elindítani.");
    return result.ok;
  }

  async function upload(id, files) {
    files = files.filter((f) => /^image\//.test(f.type) || /\.(jpe?g|png|webp|heic|heif)$/i.test(f.name));
    for (let i = 0; i < files.length; i++) {
      uploading.set(id, `Fotók feltöltése ${i + 1}/${files.length}…`);
      render();
      try {
        const response = await fetch(`/api/work/items/${id}/photo`, {
          method: "POST",
          headers: { "Content-Type": "application/octet-stream", "X-Jarvis-Token": token },
          body: files[i],
        });
        const result = await response.json().catch(() => ({}));
        if (response.ok && result.ok) items.set(id, result.item);
        else J.toast(result.error || "Nem sikerült feltölteni: " + files[i].name);
      } catch {
        J.toast("Nem sikerült feltölteni: " + files[i].name);
      }
    }
    uploading.delete(id);
    render();
  }

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

  // Az oldalak szabályai tilthatják a gépi kitöltést: oldalanként egyszer szólunk, és megjegyezzük.
  async function acknowledge(keys) {
    const unknown = keys.filter((k) => !acknowledged.has(k));
    if (!unknown.length) return true;
    const names = unknown.map(siteName).join(", ");
    const ok = await ask("Feltöltés Jarvisszal",
      `A(z) ${names} szabályai (mint a legtöbb hirdetési oldalé) tiltják a botokat. Ha kiszúrják, ` +
      "korlátozhatják a fiókodat.\n\nJarvis a saját böngészőablakában, a szemed előtt tölti ki az " +
      "űrlapot, és a közzététel előtt mindig rákérdez.", "Értem, mehet");
    if (!ok) return false;
    for (const key of unknown) {
      const result = await J.postJson("/api/work/ack", { platform: key });
      if (result.ok) acknowledged = new Set(result.acknowledged || [...acknowledged, key]);
    }
    return true;
  }

  // --- új hirdetés ------------------------------------------------------------------------

  function selectedSites() {
    return [...document.querySelectorAll('#w-sites input[type="checkbox"]:checked')].map((el) => el.value);
  }

  function renderCompose() {
    let saved = [];
    try { saved = JSON.parse(recall("jarvis-sites") || "[]"); } catch { saved = []; }
    const current = $("w-sites").children.length ? selectedSites() : saved;
    $("w-sites").innerHTML = platforms.map((p) =>
      `<label class="check"><input type="checkbox" value="${esc(p.key)}"${current.includes(p.key) ? " checked" : ""}>` +
      ` ${esc(p.name)}</label>`).join("");
    $("w-research").checked = recall("jarvis-research") === "1";
    $("w-research").disabled = !researchOn;
    renderNeeds();
    renderComposePhotos();
  }

  function renderNeeds() {
    const chosen = selectedSites();
    if (!chosen.length) {
      $("w-needs").innerHTML = '<p class="muted">Pipáld be, hova kerüljön; megmutatom, mit kell megadnod hozzá.</p>';
      return;
    }
    const common = needs.filter((n) => !n.sites.length).map((n) => n.label);
    // Az azonos oldalakra vonatkozó igények egy sorba kerülnek (pl. „Vinted, ha ruha…: márka, méret”).
    const groups = new Map();
    const fromSettings = [];
    for (const need of needs.filter((n) => n.sites.some((s) => chosen.includes(s)))) {
      const where = need.sites.filter((s) => chosen.includes(s)).map(siteName).join(", ");
      if (need.setting) {
        const value = settings[need.setting];
        fromSettings.push(`${where}: ${need.label} – ` + (value ? `az Alapadatokból: ${value.replace(/\.+$/, "")}.`
          : "írd a leírásba, vagy add meg egyszer az Alapadatoknál."));
        continue;
      }
      const key = where + (need.clothing_only ? ", ha ruha, cipő vagy kiegészítő" : "");
      groups.set(key, [...(groups.get(key) || []), need.label]);
    }
    const lines = [`Mindenhova: ${common.join(", ")}, és legalább egy fotó.`,
                   ...[...groups].map(([where, labels]) => `${where}: ${labels.join(", ")}.`),
                   ...fromSettings];
    $("w-needs").innerHTML = '<p class="needs-title">Ehhez kell:</p><ul>' +
      lines.map((l) => `<li>${esc(l)}</li>`).join("") + "</ul>";
  }

  function renderComposePhotos() {
    const box = $("w-new-photos");
    for (const img of box.querySelectorAll("img")) URL.revokeObjectURL(img.src);
    box.innerHTML = composePhotos.map((file, i) =>
      `<div class="photo"><img src="${URL.createObjectURL(file)}" alt="${i + 1}. fotó">` +
      `<div class="tools"><span></span><button type="button" data-unpick="${i}" aria-label="${i + 1}. fotó elvétele">✕</button></div></div>`
    ).join("") + (composePhotos.length < maxPhotos
      ? '<button type="button" class="addphoto" data-pick="compose">+ Fotók<br>vagy húzd ide</button>' : "");
  }

  async function go(event) {
    event.preventDefault();
    const targets = selectedSites();
    const prompt = $("w-prompt").value.trim();
    const say = (text) => { $("w-compose-msg").textContent = text; };
    if (!targets.length) return say("Pipáld be, hova töltsem fel.");
    if (!composePhotos.length) return say("Adj hozzá legalább egy fotót.");
    if (!prompt) return say("Írd le röviden, mit adsz el.");
    if (!browserOn) return say("A feltöltéshez Jarvis saját böngészője kell (Playwright).");
    if (!(await acknowledge(targets))) return;
    $("w-go").disabled = true;
    say("Küldöm a fotókat…");
    try {
      const created = await J.postJson("/api/work/new",
        { targets, prompt, research_wanted: $("w-research").checked });
      if (!created.ok) { say(created.error || "Nem sikerült."); return; }
      items.set(created.item.id, created.item);
      const files = composePhotos;
      composePhotos = [];
      $("w-prompt").value = "";
      renderComposePhotos();
      render();
      await upload(created.item.id, files);
      if (await action("sell", created.item.id)) {
        say("");
        J.toast("Jarvis dolgozik rajta. Ha kérdése van, a hirdetésnél látod.");
      }
    } finally {
      $("w-go").disabled = false;
    }
  }

  // --- alapadatok és bejelentkezés --------------------------------------------------------------

  function renderSetup() {
    const parcel = ["", ...parcelSizes].map((s) =>
      `<option value="${esc(s)}"${(settings.parcel_size || "") === s ? " selected" : ""}>${esc(s || "–")}</option>`).join("");
    $("w-setup-body").innerHTML = `
      <p class="muted">Ezeket elég egyszer megadni; Jarvis minden hirdetéshez innen veszi, ha a leírásban nem írsz mást.</p>
      <div class="fields">
        <div class="field"><label for="s-city">Város (az átvétel helye)</label>
          <input id="s-city" data-setting="city" value="${esc(settings.city)}" placeholder="pl. Budapest XI."></div>
        <div class="field"><label for="s-parcel">Vinted csomagméret</label>
          <select id="s-parcel" data-setting="parcel_size">${parcel}</select></div>
        <div class="field wide"><label for="s-shipping">Szállítás (módok és díjak)</label>
          <input id="s-shipping" data-setting="shipping" value="${esc(settings.shipping)}"
            placeholder="pl. személyes átvétel, Foxpost 1290 Ft, MPL 1990 Ft, előre utalással"></div>
      </div>
      <div class="row"><button type="button" id="s-save">Mentés</button></div>
      <p class="muted">Bejelentkezés: a gomb megnyitja az oldalt Jarvis böngészőjében. Ott lépj be, és pipáld be,
        hogy maradj bejelentkezve. Elég egyszer, a Jarvis frissítése után is megmarad.</p>
      <div class="row">${platforms.map((p) =>
        `<button type="button" class="small" data-login="${esc(p.key)}"${browserOn ? "" : " disabled"}>${esc(p.name)}</button>`).join("")}</div>`;
  }

  async function saveSettings() {
    const values = {};
    for (const el of $("w-setup-body").querySelectorAll("[data-setting]")) values[el.dataset.setting] = el.value;
    const result = await J.postJson("/api/work/settings", { settings: values });
    if (!result.ok) { J.toast(result.error || "Nem sikerült menteni."); return; }
    settings = result.settings;
    renderNeeds();
    J.toast("Elmentettem.");
  }

  // --- a hirdetések -------------------------------------------------------------------------------

  // A Jarvis felől érkező változásokat összegyűjtjük, és egyszerre rajzoljuk újra. Ha épp egy
  // tárgy lapja van nyitva, egy másik tárgy változása miatt nem rajzoljuk újra (csak a számlálókat).
  let queued = false;
  const dirty = new Set();
  function scheduleRender(id) {
    dirty.add(id);
    if (queued) return;
    queued = true;
    setTimeout(() => {
      queued = false;
      const touchesOpen = !openId || dirty.has(openId) || !items.has(openId);
      dirty.clear();
      if (touchesOpen) render();
      else renderFilters();
    }, 30);
  }

  // Újrarajzoláskor ne vesszen el, amit épp gépelsz (pl. egy válasz a kártyán).
  function keepTyping(box, draw) {
    const typed = new Map([...box.querySelectorAll("[data-keep]")].map((el) => [el.dataset.keep, el.value]));
    const active = document.activeElement;
    const focus = active && box.contains(active) && active.dataset.keep
      ? { key: active.dataset.keep, start: active.selectionStart, end: active.selectionEnd } : null;
    const scroll = area.scrollTop;
    draw();
    for (const el of box.querySelectorAll("[data-keep]")) {
      if (typed.has(el.dataset.keep) && typed.get(el.dataset.keep)) el.value = typed.get(el.dataset.keep);
    }
    if (focus) {
      const el = box.querySelector(`[data-keep="${focus.key}"]`);
      if (el) {
        el.focus();
        try { el.setSelectionRange(focus.start, focus.end); } catch { /* select */ }
      }
    }
    area.scrollTop = scroll;
  }

  function render() {
    renderFilters();
    if (openId && items.has(openId)) {
      $("w-list-view").hidden = true;
      $("w-detail").hidden = false;
      keepTyping($("w-detail"), () => renderDetail(items.get(openId)));
    } else {
      openId = null;
      $("w-detail").hidden = true;
      $("w-list-view").hidden = false;
      keepTyping($("w-list"), renderList);
    }
  }

  function renderFilters() {
    const counts = {};
    for (const [key] of FILTERS) {
      const saved = filter;
      filter = key;
      counts[key] = [...items.values()].filter(matches).length;
      filter = saved;
    }
    $("w-filters").innerHTML = FILTERS.map(([key, label]) =>
      `<button type="button" data-filter="${key}" aria-pressed="${filter === key}">${label}` +
      `<span class="count">${counts[key]}</span></button>`).join("");
  }

  function renderList() {
    const list = [...items.values()].sort((a, b) => b.created - a.created).filter(matches);
    $("w-list").innerHTML = list.map(card).join("") ||
      `<p class="muted">${items.size ? "Nincs ilyen hirdetés." : "Még nincs hirdetésed. Fent kezdheted."}</p>`;
  }

  function siteChips(item) {
    return item.targets.map((key) => {
      const state = siteState(item, key);
      const mark = state === POSTED ? "✓" : state === FAILED ? "✕" : "…";
      const cls = state === POSTED ? "on" : state === FAILED ? "bad" : "";
      return `<span class="site ${cls}" title="${esc((item.platforms[key] || {}).note || state || "még nincs fent")}">` +
             `${esc(siteName(key))} ${mark}</span>`;
    }).join("");
  }

  function pendingBlock(item) {
    const p = item.pending || {};
    if (p.kind === "info") {
      return `<form class="ask" data-reply="${item.id}">
        <p>${esc(p.question)}</p>
        <div class="withcopy"><input data-keep="reply-${item.id}" aria-label="Válasz Jarvisnak" placeholder="Válasz">
          <button type="submit" class="small primary">Küldés</button>
          <button type="button" class="small quiet" data-cancel="${item.id}">Mégse</button></div></form>`;
    }
    if (p.kind === "price") {
      return `<div class="ask"><p>${esc(p.question)}</p><div class="row">
        <button type="button" class="small" data-price="keep" data-id="${item.id}">Marad: ${esc(price(item.price))}</button>
        <button type="button" class="small primary" data-price="suggested" data-id="${item.id}">Legyen: ${esc(price(p.suggested))}</button>
      </div></div>`;
    }
    return "";
  }

  function card(item) {
    const thumb = item.photos.length
      ? `<img class="thumb" src="${photoUrl(item.id, esc(item.photos[0]), true)}" alt="">`
      : '<span class="nothumb">nincs fotó</span>';
    const busy = uploading.get(item.id) || item.busy;
    return `<div class="card${item.sold ? " sold" : ""}" data-id="${item.id}">
      <button type="button" class="cardthumb" data-open="${item.id}" aria-label="${esc(item.name)} részletei">${thumb}</button>
      <div class="body">
        <div class="row tight"><span class="chip ${STATUS_CLASS[item.status] || ""}">${esc(item.status)}</span>
          <span class="facts">${esc(price(item.price))}</span></div>
        <button type="button" class="title linklike" data-open="${item.id}">${esc(item.name)}</button>
        <div class="sites">${siteChips(item)}</div>
        ${busy ? `<span class="busy">${esc(busy)}</span>` : ""}
        ${pendingBlock(item)}
      </div></div>`;
  }

  function field(name, label, value, extra = "") {
    return `<div class="field"><label for="f-${name}">${label}</label>` +
           `<input id="f-${name}" data-field="${name}" data-keep="f-${name}" value="${esc(value)}" ${extra}></div>`;
  }

  function conditionField(value) {
    const options = ["", ...conditions];
    if (value && !options.includes(value)) options.push(value);
    return `<div class="field"><label for="f-condition">Állapot</label><select id="f-condition" data-field="condition">` +
      options.map((o) => `<option value="${esc(o)}"${o === value ? " selected" : ""}>${esc(o || "–")}</option>`).join("") +
      "</select></div>";
  }

  function photo(item, name, index) {
    return `<div class="photo${index === 0 ? " first" : ""}">
      <img src="${photoUrl(item.id, esc(name), true)}" alt="${index + 1}. fotó">
      ${index === 0 ? '<span class="tag">borító</span>' : ""}
      <div class="tools">
        ${index > 0 ? `<button type="button" data-action="cover" data-name="${esc(name)}" title="Legyen ez az első kép">Borító</button>` : "<span></span>"}
        <button type="button" data-action="removephoto" data-name="${esc(name)}" aria-label="${index + 1}. fotó törlése">✕</button>
      </div></div>`;
  }

  function researchBlock(item) {
    const r = item.research || {};
    if (!r.at) return "";
    const similar = r.similar || [];
    let html = item.verdict ? `<p class="verdict">${esc(item.verdict)}</p>` : "";
    if (r.reason) html += `<p class="muted">${esc(r.reason)}</p>`;
    html += `<p class="muted">Keresés: „${esc(r.query)}”, ${r.count || 0} találat` +
            (similar.length ? `, ebből ${similar.length} hasonló:` : ".") + "</p>";
    if (similar.length) {
      html += "<ul>" + similar.map((s) => {
        const text = `${esc(s.site)}: ${esc((s.text || "").slice(0, 90))}`;
        const link = safeUrl(s.url) ? `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${text}</a>` : text;
        return `<li>${link} – ${esc(price(s.price))}</li>`;
      }).join("") + "</ul>";
    }
    for (const error of r.errors || []) html += `<p class="muted">${esc(error)}</p>`;
    return `<section aria-labelledby="h-research"><h3 id="h-research">Árkutatás</h3>` +
           `<div class="research">${html}</div></section>`;
  }

  function platformRow(item, p) {
    const state = item.platforms[p.key] || {};
    const chosen = item.targets.includes(p.key);
    const delisted = state.status === "nincs fent" && (state.note || "").startsWith("Levéve");
    const label = state.status === POSTED ? "Fent van" : state.status === FAILED ? "Nem sikerült"
      : delisted ? "Levéve" : chosen ? "Még nincs fent" : "";
    const when = state.at && state.status ? " (" + new Date(state.at * 1000).toLocaleDateString("hu-HU") + ")" : "";
    const note = state.note && state.status !== "nincs fent" ? state.note : "";
    return `<div class="platform">
      <label class="check pname"><input type="checkbox" data-target="${esc(p.key)}"${chosen ? " checked" : ""}> ${esc(p.name)}</label>
      <span class="pstatus${state.status === POSTED ? " on" : state.status === FAILED ? " bad" : ""}">${esc(label + (label ? when : ""))}</span>
      ${state.status === POSTED && safeUrl(state.url) ? `<a class="button small" href="${esc(state.url)}" target="_blank" rel="noopener noreferrer">Megnyitás</a>` : ""}
      ${chosen && !item.sold && state.status !== POSTED && browserOn ? `<button type="button" class="small" data-action="post" data-platform="${esc(p.key)}">${state.status === FAILED ? "Újra" : "Feltöltés"}</button>` : ""}
      ${state.status === POSTED && browserOn ? `<button type="button" class="small" data-action="delist" data-platform="${esc(p.key)}" title="Jarvis leveszi a hirdetést erről az oldalról">Levétel</button>` : ""}
      ${chosen ? `<button type="button" class="small quiet" data-action="toggle" data-platform="${esc(p.key)}" title="Csak a nyilvántartást írja át">${state.status === POSTED ? "Már levettem" : "Fent van"}</button>` : ""}
      ${state.dossier ? `<button type="button" class="small quiet" data-action="dossier" data-platform="${esc(p.key)}" title="Képernyőkép, oldalnézet és a lépések abból a pillanatból, amikor elakadt">Hibacsomag</button>` : ""}
      ${note ? `<span class="pnote">${esc(note)}</span>` : ""}
    </div>`;
  }

  function renderDetail(item) {
    const busy = uploading.get(item.id) || item.busy;
    $("w-detail").innerHTML = `
      <header>
        <button type="button" class="quiet small" data-action="back">← Hirdetéseim</button>
        <h2>${esc(item.name)}</h2>
        <span class="chip ${STATUS_CLASS[item.status] || ""}">${esc(item.status)}</span>
        ${busy ? `<span class="busy">${esc(busy)}</span>` : ""}
      </header>
      ${pendingBlock(item)}
      <section aria-labelledby="h-post">
        <h3 id="h-post">Hova kerüljön</h3>
        <div>${platforms.map((p) => platformRow(item, p)).join("")}</div>
        <div class="row">
          ${browserOn && item.targets.some((t) => siteState(item, t) !== POSTED)
            ? '<button type="button" class="primary" data-action="sell">Feltöltés a hiányzó helyekre</button>' : ""}
          ${researchOn ? '<button type="button" data-action="research">Mennyit ér? (árkutatás)</button>' : ""}
        </div>
      </section>
      <section aria-labelledby="h-photos">
        <h3 id="h-photos">Fotók</h3>
        <div class="photos">
          ${item.photos.map((name, i) => photo(item, name, i)).join("")}
          ${item.photos.length < maxPhotos ? `<button type="button" class="addphoto" data-action="pickphotos">
            + Fotó<br>vagy húzd ide</button>` : ""}
        </div>
      </section>
      <section aria-labelledby="h-listing">
        <h3 id="h-listing">A hirdetés</h3>
        <p class="muted">A leírásod: ${esc(item.prompt) || "–"}</p>
        <div class="fields">
          <div class="field wide"><label for="f-title">Cím</label>
            <input id="f-title" data-field="title" data-keep="f-title" value="${esc(item.title)}" maxlength="100"></div>
          <div class="field wide"><label for="f-description">Leírás</label>
            <textarea id="f-description" data-field="description" data-keep="f-description" rows="4">${esc(item.description)}</textarea></div>
          ${field("price", "Ár (Ft)", item.price ?? "", 'inputmode="numeric"')}
          ${conditionField(item.condition)}
          ${field("brand", "Márka", item.brand)}
          ${field("size", "Méret", item.size)}
          ${field("color", "Szín", item.color)}
          ${field("category", "Kategória", item.category)}
          ${field("location", "Átvétel helye", item.location, `placeholder="${esc(settings.city || "az Alapadatokból")}"`)}
          ${field("shipping", "Szállítás", item.shipping, `placeholder="${esc(settings.shipping || "az Alapadatokból")}"`)}
        </div>
        <p class="muted">Amit itt átírsz, azt Jarvis már nem írja felül.</p>
      </section>
      ${researchBlock(item)}
      <div class="danger">
        <button type="button" data-action="sold">${item.sold ? "Mégsem kelt el" : "Eladva"}</button>
        <button type="button" class="delete" data-action="delete">Törlés</button>
      </div>`;
  }

  // --- műveletek -------------------------------------------------------------------------

  async function toggle(item, key) {
    const status = siteState(item, key) === POSTED ? "nincs fent" : POSTED;
    remember(await J.postJson("/api/work/mark", { id: item.id, platform: key, status }), "Nem sikerült.");
  }

  // Eladva: hol kelt el, és vegye-e le Jarvis a többi oldalról (ahol nem ott kelt el).
  function askSold(item, posted) {
    return new Promise((resolve) => {
      const dialog = $("sold-dialog");
      $("sold-text").textContent = `${item.name}: hol kelt el?`;
      $("sold-where").innerHTML = [...posted.map((k) => [k, siteName(k)]), ["", "Máshol vagy személyesen"]]
        .map(([key, name], i) => `<label class="check"><input type="radio" name="sold-where" value="${esc(key)}"` +
          `${i === posted.length ? " checked" : ""}> ${esc(name)}</label>`).join("");
      $("sold-delist").checked = true;
      const finish = (value) => {
        $("sold-yes").removeEventListener("click", onYes);
        $("sold-no").removeEventListener("click", onNo);
        dialog.removeEventListener("cancel", onCancel);
        dialog.close();
        resolve(value);
      };
      const onYes = () => {
        const where = (dialog.querySelector('input[name="sold-where"]:checked') || { value: "" }).value;
        finish({ where, delist: $("sold-delist").checked });
      };
      const onNo = () => finish(null);
      const onCancel = (event) => { event.preventDefault(); finish(null); };
      $("sold-yes").addEventListener("click", onYes);
      $("sold-no").addEventListener("click", onNo);
      dialog.addEventListener("cancel", onCancel);
      dialog.showModal();
      $("sold-yes").focus();
    });
  }

  async function sold(item) {
    if (item.sold) {   // mégsem kelt el
      remember(await J.postJson("/api/work/mark", { id: item.id, sold: false }), "Nem sikerült.");
      return;
    }
    const posted = Object.keys(item.platforms).filter((k) => siteState(item, k) === POSTED);
    if (!posted.length) {
      remember(await J.postJson("/api/work/mark", { id: item.id, sold: true }), "Nem sikerült.");
      return;
    }
    const answer = await askSold(item, posted);
    if (!answer) return;
    const result = await J.postJson("/api/work/mark", { id: item.id, sold: true });
    if (!remember(result, "Nem sikerült.")) return;
    if (answer.where) {
      await J.postJson("/api/work/mark", { id: item.id, platform: answer.where, status: POSTED, note: "Itt kelt el." });
    }
    const others = posted.filter((k) => k !== answer.where);
    if (!others.length) return;
    if (answer.delist && browserOn) {
      if (await action("delist", item.id, { platforms: others })) {
        J.toast("Jarvis leveszi innen: " + others.map(siteName).join(", "));
      }
    } else {
      J.toast("Ne felejtsd el levenni innen: " + others.map(siteName).join(", "));
    }
  }

  async function delistOne(item, key) {
    const ok = await ask("Levétel", `Jarvis leveszi a(z) „${item.name}” hirdetést innen: ${siteName(key)}. ` +
      "A törlés gombjaira ezen az oldalon nem kérdez rá újra.", "Leveszem");
    if (ok) action("delist", item.id, { platforms: [key] });
  }

  async function remove(item) {
    const ok = await ask("Hirdetés törlése",
      `Biztosan törlöd: ${item.name}?\n(Az oldalakról ez nem veszi le. A munka mappán belül a _torolt mappába kerül.)`, "Törlés");
    if (!ok) return;
    const result = await J.postJson("/api/work/delete", { id: item.id });
    if (!result.ok) { J.toast(result.error || "Nem sikerült."); return; }
    items.delete(item.id);
    openId = null;
    render();
  }

  async function changeTargets(item, key, on) {
    const targets = on ? [...item.targets, key] : item.targets.filter((t) => t !== key);
    if (on && !(await acknowledge([key]))) { render(); return; }
    update(item.id, { targets });
  }

  // --- események -----------------------------------------------------------------------------

  // Az Android-alkalmazásból (app.js, takePending): a galériából megosztott fotók az új hirdetéshez, vagy az
  // „Új hirdetés” parancsikon. A fotókat az alkalmazás a /__jarvis_shared/ címen adja (a telefonról, nem a gépről).
  window.addEventListener("jarvis-share", async (event) => {
    const wanted = event.detail || {};
    openId = null;
    render();
    $("w-compose").scrollIntoView({ block: "start" });
    const files = [];
    for (let i = 0; i < (wanted.photos || 0); i++) {
      try {
        const response = await fetch(`/__jarvis_shared/${wanted.batch}/${i}`);
        if (!response.ok) continue;
        const blob = await response.blob();
        let name = `foto-${i + 1}.jpg`;
        try { name = decodeURIComponent(response.headers.get("X-Jarvis-Name") || name); } catch { /* marad */ }
        files.push(new File([blob], name, { type: blob.type || "image/jpeg" }));
      } catch { /* ezt nem sikerült beolvasni */ }
    }
    $("w-prompt").focus();
    if (!wanted.photos) return;
    if (!files.length) { J.toast("Nem sikerült beolvasni a megosztott fotókat."); return; }
    composePhotos = [...composePhotos, ...files].slice(0, maxPhotos);
    renderComposePhotos();
    J.toast(`${files.length} fotó a megosztásból: írd le, mit adsz el, és pipáld be, hova kerüljön.`);
  });

  $("w-compose").addEventListener("submit", go);
  $("w-sites").addEventListener("change", () => {
    store("jarvis-sites", JSON.stringify(selectedSites()));
    renderNeeds();
  });
  $("w-research").addEventListener("change", () => store("jarvis-research", $("w-research").checked ? "1" : "0"));
  $("w-new-photos").addEventListener("click", (event) => {
    const unpick = event.target.closest("[data-unpick]");
    if (unpick) {
      composePhotos.splice(Number(unpick.dataset.unpick), 1);
      renderComposePhotos();
    } else if (event.target.closest("[data-pick]")) {
      pickFor = "compose";
      $("w-file").click();
    }
  });

  $("w-setup-body").addEventListener("click", (event) => {
    if (event.target.closest("#s-save")) saveSettings();
    const login = event.target.closest("[data-login]");
    if (login) {
      action("login", null, { platform: login.dataset.login });
      J.toast("Megnyitom a Jarvis böngészőjében. Jelentkezz be, és pipáld be, hogy maradj bejelentkezve.");
    }
  });

  $("w-filters").addEventListener("click", (event) => {
    const button = event.target.closest("[data-filter]");
    if (!button) return;
    filter = button.dataset.filter;
    openId = null;
    render();
  });

  async function onCardsClick(event) {
    const open = event.target.closest("[data-open]");
    if (open) {
      openId = open.dataset.open;
      render();
      area.scrollTop = 0;
      return;
    }
    const cancel = event.target.closest("[data-cancel]");
    if (cancel) { action("cancel", cancel.dataset.cancel); return; }
    const decide = event.target.closest("[data-price]");
    if (decide) action("price", decide.dataset.id, { choice: decide.dataset.price });
  }

  function onReply(event) {
    const form = event.target.closest("form[data-reply]");
    if (!form) return;
    event.preventDefault();
    const input = form.querySelector("input");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    action("reply", form.dataset.reply, { text });
  }

  $("w-list").addEventListener("click", onCardsClick);
  $("w-list").addEventListener("submit", onReply);
  $("w-detail").addEventListener("submit", onReply);

  $("w-detail").addEventListener("click", async (event) => {
    const item = items.get(openId);
    if (!item) return;
    if (event.target.closest("[data-cancel]") || event.target.closest("[data-price]")) {
      onCardsClick(event);
      return;
    }
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const name = button.dataset.action;
    if (name === "back") { openId = null; render(); }
    else if (name === "pickphotos") { pickFor = item.id; $("w-file").click(); }
    else if (name === "sell") {
      if (await acknowledge(item.targets)) action("sell", item.id);
    }
    else if (name === "research") action("research", item.id);
    else if (name === "post") {
      if (await acknowledge([button.dataset.platform])) action("post", item.id, { platform: button.dataset.platform });
    }
    else if (name === "toggle") toggle(item, button.dataset.platform);
    else if (name === "delist") delistOne(item, button.dataset.platform);
    else if (name === "dossier") {
      const result = await J.postJson("/api/work/dossier", { id: item.id, platform: button.dataset.platform });
      if (!result.ok) J.toast(result.error || "Nem sikerült megnyitni.");
      else if (!result.opened) J.toast("A hibacsomag itt van: " + result.path);
    }
    else if (name === "cover") {
      remember(await J.postJson("/api/work/photos", { id: item.id, order: [button.dataset.name] }), "Nem sikerült.");
    }
    else if (name === "removephoto") {
      if (await ask("Fotó törlése", "Biztosan törlöd ezt a fotót?", "Törlés")) {
        remember(await J.postJson("/api/work/photos", { id: item.id, remove: button.dataset.name }), "Nem sikerült.");
      }
    }
    else if (name === "sold") sold(item);
    else if (name === "delete") remove(item);
  });

  $("w-detail").addEventListener("change", (event) => {
    const el = event.target;
    const item = items.get(openId);
    if (!item) return;
    if (el.dataset.target) changeTargets(item, el.dataset.target, el.checked);
    else if (el.dataset.field) update(item.id, { [el.dataset.field]: el.value });
  });

  // A fájlválasztó a lapon kívül van, így akkor sem vész el, ha közben frissül a lap.
  $("w-file").addEventListener("change", () => {
    const files = [...$("w-file").files];
    $("w-file").value = "";
    const target = pickFor;
    pickFor = null;
    if (!files.length) return;
    if (target === "compose") addComposePhotos(files);
    else if (target && items.has(target)) upload(target, files);
  });

  function addComposePhotos(files) {
    const images = files.filter((f) => /^image\//.test(f.type) || /\.(jpe?g|png|webp|heic|heif)$/i.test(f.name));
    if (!images.length) { J.toast("Csak képet tudok hozzáadni."); return; }
    composePhotos = [...composePhotos, ...images].slice(0, maxPhotos);
    renderComposePhotos();
  }

  // Fotók behúzása: az új hirdetéshez, vagy ha egy hirdetés lapja van nyitva, ahhoz.
  const hasFiles = (event) => [...((event.dataTransfer && event.dataTransfer.types) || [])].includes("Files");
  let depth = 0;
  area.addEventListener("dragenter", (event) => {
    if (!hasFiles(event)) return;
    depth += 1;
    area.classList.add("dragging");
  });
  area.addEventListener("dragleave", () => {
    depth = Math.max(0, depth - 1);
    if (!depth) area.classList.remove("dragging");
  });
  area.addEventListener("drop", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    depth = 0;
    area.classList.remove("dragging");
    const files = [...event.dataTransfer.files];
    if (openId) upload(openId, files);
    else addComposePhotos(files);
  });
  // máshová ejtve a kép ne nyíljon meg az ablakban a Jarvis helyett
  window.addEventListener("dragover", (event) => { if (hasFiles(event)) event.preventDefault(); });
  window.addEventListener("drop", (event) => { if (hasFiles(event)) event.preventDefault(); });

  J.on("work_item", (event) => { items.set(event.item.id, event.item); scheduleRender(event.item.id); });
  J.on("work_removed", (event) => {
    items.delete(event.id);
    if (openId === event.id) openId = null;
    scheduleRender(event.id);
  });
  J.onHello((event) => { if (event.work) load(); });
})();
