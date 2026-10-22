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
  const driftNotes = new Map();         // tárgy -> a legutóbbi kézi átírás után: a hirdetésben még a régi ár, cím
  // Vevői üzenetek: a beszélgetések (Jarvis olvassa el őket), a nyitott beszélgetés, és van-e saját böngésző.
  const convs = new Map();
  let messagesOn = false;
  let msgBrowser = false;
  let convOpen = null;
  const area = $("w-area");
  let markLoaded = null;
  const loaded = new Promise((resolve) => { markLoaded = resolve; });   // az első betöltés után

  const POSTED = "feltöltve";
  const FAILED = "hiba";
  const STATUS_CLASS = { "vázlat": "vazlat", "kérdés": "kerdes", "árdöntés": "kerdes",
                         "részben fent": "kesz", "fent": "feltoltve", "eladva": "eladva" };
  const FILTERS = [["all", "Mind"], ["todo", "Teendő"], ["up", "Fent van"], ["check", "Nézd meg"], ["sold", "Eladva"]];
  const CHECKED = { sold: "Elkeltnek látszik", gone: "Már nincs fent?" };   // a háttérellenőrzés jelzései
  // Az árkutatás előszűrése (selling.prefilter): miért maradt ki egy találat.
  const DROPPED = { "saját": "a sajátod", "kereső": "kereső vagy csere", "csomag": "csomag",
                    "alkatrész": "alkatrész vagy hibás", "más termék": "más termék", "ismétlődő": "ismétlődő",
                    "kevésbé illő": "kevésbé illő" };

  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const price = (value) => (value === null || value === undefined || value === "") ? ""
    : String(Math.round(Number(value))).replace(/\B(?=(\d{3})+(?!\d))/g, " ") + " Ft";
  const photoUrl = (id, name, thumb) =>
    `/api/work/photo/${id}/${name}?t=${encodeURIComponent(token)}${thumb ? "&thumb=1" : ""}`;
  const safeUrl = (url) => (/^https?:\/\//i.test(url || "") ? url : "");
  const az = (n) => (n === 1 || /^5\d{0,2}$/.test(String(n)) ? "az" : "a");    // „az 1.”, „az 5.”, „a 3.”
  // A fotóellenőrzés jelzése (homályos, sötét, ismétlődő…): „!” a kép sarkában, a részletek az egér alatt.
  const photoBadge = (text) => (text
    ? `<span class="warn" role="img" title="${esc(text)}" aria-label="${esc(text)}">!</span>` : "");
  const siteName = (key) => (platforms.find((p) => p.key === key) || { name: key }).name;
  const siteState = (item, key) => (item.platforms[key] || {}).status || "";
  const store = (key, value) => { try { localStorage.setItem(key, value); } catch { /* nem baj */ } };
  const recall = (key) => { try { return localStorage.getItem(key); } catch { return null; } };
  const shortDate = (t) => new Date(t * 1000).toLocaleString("hu-HU",
    { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  const online = (item) => (item.sold || item.days_online === null || item.days_online === undefined ? ""
    : item.days_online ? `${item.days_online} napja fent` : "ma került fel");

  // Amire érdemes ránézni: a háttérellenőrzés szerint valahol elkelt vagy eltűnt, vagy a fent lévő hirdetésben
  // még a régi ár vagy cím van (itt átírtad, ott nem).
  function flags(item) {
    if (item.sold) return [];
    const out = [];
    for (const [key, state] of Object.entries(item.platforms)) {
      if (state.status === POSTED && CHECKED[state.check]) out.push(`${siteName(key)}: ${CHECKED[state.check].toLowerCase()}`);
    }
    const drifted = new Map();           // "ár", "cím" vagy "ár és cím" -> az oldalak, ahol még a régi van fent
    for (const d of item.drift || []) {
      const what = d.price !== undefined && d.title ? "ár és cím" : d.price !== undefined ? "ár" : "cím";
      drifted.set(what, [...(drifted.get(what) || []), siteName(d.platform)]);
    }
    for (const [what, sites] of drifted) out.push(`A hirdetésben még a régi ${what}: ${sites.join(", ")}`);
    return out;
  }
  const isNew = (conv) => !!(conv.new || conv.unread);

  function matches(item) {
    if (filter === "sold") return item.sold;
    if (filter === "check") return flags(item).length > 0;
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
      messagesOn = !!data.messages;
      if (!messagesOn && filter === "msgs") filter = "all";
      renderCompose();
      renderSetup();
      render();
      markLoaded();
      if (messagesOn) loadMessages();
    } catch { /* a Jarvis még indul: a következő "hello" újra betölti */ }
  }

  async function loadMessages() {
    try {
      const response = await fetch("/api/work/messages?t=" + encodeURIComponent(token));
      if (!response.ok) return;
      const data = await response.json();
      convs.clear();
      for (const conv of data.conversations || []) convs.set(conv.id, conv);
      msgBrowser = !!data.browser;
      render();
    } catch { /* később újra betölti */ }
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
    const result = await J.postJson("/api/work/update", { id, fields });
    if (result.ok) driftNotes.set(id, result.drift_note || "");
    if (!remember(result, "Nem sikerült menteni.")) load();
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

  // Telefonon az „Új hirdetés” űrlap összecsukva kezdődik (ha van már tárgy), hogy a lista látsszon; a címére bökve nyílik.
  const phone = window.matchMedia("(max-width: 640px)");
  let composeTouched = false;
  function foldCompose() {
    if (!composeTouched) $("w-compose").classList.toggle("folded", phone.matches && items.size > 0);
  }
  $("w-compose-title").addEventListener("click", () => {
    composeTouched = true;
    $("w-compose").classList.toggle("folded");
  });

  // --- Automata kereskedés: olcsó hirdetések keresése (deals.py) ---
  let job = "sales";
  let deals = { enabled: false, searches: [], deals: [], sites: [], browser: false, profit: null };
  let buying = null;      // a találat, aminél épp a vételárat írod (Megvettem)
  async function loadDeals() {
    try {
      const response = await fetch("/api/work/deals?t=" + encodeURIComponent(token));
      if (response.ok) deals = await response.json();
    } catch { /* a következő megnyitáskor újra */ }
    if (job === "deals") render();
  }
  async function dealAction(body) {
    const result = await J.postJson("/api/work/deals", body);
    if (!result.ok) { J.toast(result.error || "Nem sikerült."); return false; }
    if (result.searches) deals = { ...deals, ...result };
    render();
    return result;
  }
  function setJob(name) {
    job = name;
    $("job-sales").setAttribute("aria-selected", String(name === "sales"));
    $("job-deals").setAttribute("aria-selected", String(name === "deals"));
    $("job-ledger").setAttribute("aria-selected", String(name === "ledger"));
    if (name === "deals") loadDeals();
    if (name === "ledger") loadLedger();
    render();
  }
  $("job-sales").addEventListener("click", () => setJob("sales"));
  $("job-deals").addEventListener("click", () => setJob("deals"));
  $("job-ledger").addEventListener("click", () => setJob("ledger"));

  const every = (m) => (m >= 60 ? `${m / 60} óránként` : `${m} percenként`);
  // Egy licit hátralévő ideje most (a találatkor látott idő, mínusz azóta eltelt): „2 óra 10 perc”.
  function leftText(d) {
    if (d.left === undefined || d.left === null) return "";
    const s = Math.max(0, d.left - (Date.now() / 1000 - (d.at || 0)));
    const days = Math.floor(s / 86400), hours = Math.floor((s % 86400) / 3600), minutes = Math.floor((s % 3600) / 60);
    if (s < 60) return "lezárult";
    return [days ? `${days} nap` : "", hours ? `${hours} óra` : "", !days && minutes ? `${minutes} perc` : ""].filter(Boolean).join(" ") + " múlva zárul";
  }
  let editingRef = null;      // a keresés, aminél épp kézzel írod a szokásos árat
  let editingSearch = null;   // a keresés, amit az űrlapon szerkesztesz (Mentés: módosítás, nem új)
  let lookupQuery = "";       // Mennyit ér? (az árbázisban)
  let lookupResults = null;   // az utolsó keresés eredménye (null: még nem kerestél)
  let journalDay = "";        // a napló melyik napja látszik ("" = ma)
  let journalEntries = null;  // egy korábbi nap bejegyzései (null: a mai, a deals.journal-ból)

  // Egy keresés állapota: a szokásos ár (jóváhagyva, javaslat, kevés hasonló, felmérés a következő körben).
  function searchState(s) {
    const refForm = editingRef === s.id
      ? `<span class="row"><input id="d-ref" type="number" min="1" value="${esc(s.reference || (s.proposal && s.proposal.price) || "")}"
           style="width: 7em" aria-label="Szokásos ár (Ft)"> Ft <button type="button" class="small primary" data-ref-save="${esc(s.id)}">Mentés</button></span>`
      : "";
    if (s.status === "fut") {
      const basis = s.basis && s.basis.n ? `felmérve ${s.basis.n} hirdetésből` : "kézzel";
      const market = s.market && s.market.since
        ? `<small class="warn">A hirdetésekben most inkább ${esc(price(s.market.median))} körül van.</small>` : "";
      return `<small>Szokásos ár: <b>${esc(price(s.reference))}</b> (${basis})</small>${market}
        <span class="row">${refForm || `<button type="button" class="small" data-ref-edit="${esc(s.id)}">Átírom</button>`}
        <button type="button" class="small quiet" data-recalibrate="${esc(s.id)}">Újra felmérem</button></span>`;
    }
    if (s.status === "javaslat") {
      const p = s.proposal;
      const rows = (p.rows || []).map((r) => `<li><a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">${esc(r.text)}</a>
        <small class="muted">${esc(r.site)} · ${esc(price(r.price))}</small></li>`).join("");
      return `<small class="warn">Javaslat: <b>${esc(price(p.price))}</b> (${p.n} hasonló hirdetés, a többség
          ${esc(price(p.p25))}–${esc(price(p.p75))}). Jóváhagyásig ezzel nem keresek.</small>
        <details><summary class="muted">Miből számoltam</summary><ul class="dealrows">${rows}</ul></details>
        <span class="row">${refForm || `<button type="button" class="small primary" data-accept="${esc(s.id)}">Elfogadom</button>
          <button type="button" class="small" data-ref-edit="${esc(s.id)}">Átírom</button>`}</span>`;
    }
    if (s.status === "kevés") {
      return `<small class="warn">Kevés hasonló hirdetést találtam (${s.proposal.n}): add meg kézzel a szokásos árat.</small>
        <span class="row">${refForm || `<button type="button" class="small primary" data-ref-edit="${esc(s.id)}">Megadom</button>`}</span>`;
    }
    return `<small class="muted">A szokásos árat a következő keresésnél mérem fel (egy modellhívás), és jóváhagyásra elküldöm.</small>
      <span class="row">${refForm || `<button type="button" class="small" data-ref-edit="${esc(s.id)}">Inkább megadom</button>`}</span>`;
  }

  function searchRules(s, siteName) {
    const parts = [s.sites.map(siteName).join(", "), `${s.discount}%-kal olcsóbb${s.min_profit ? ` vagy legalább ${price(s.min_profit)} haszon` : ""}`];
    if (s.min_price || s.max_price) parts.push(`${s.min_price ? price(s.min_price) : "0"}–${s.max_price ? price(s.max_price) : "bármennyi"}`);
    if ((s.exclude || []).length) parts.push(`kivéve: ${s.exclude.join(", ")}`);
    parts.push(every(s.every));
    return parts.join(" · ");
  }

  // „5 perce”, „2 órája”, „okt. 21.”: mikor (a friss hirdetéseknél és a figyelőknél)
  function ago(t) {
    if (!t) return "";
    const s = Date.now() / 1000 - t;
    if (s < 90) return "most";
    if (s < 3600) return `${Math.round(s / 60)} perce`;
    if (s < 86400) return `${Math.round(s / 3600)} órája`;
    return new Date(t * 1000).toLocaleDateString("hu-HU", { month: "short", day: "numeric" });
  }

  // Azonnali figyelő (instant.py): a Hardverapró és a Jófogás új hirdetései, böngésző nélkül, pár percenként.
  function instantBox() {
    const I = deals.instant;
    if (!I) return "";
    const siteLine = (I.sites || []).map((s) => `${esc(s.name)}: ${s.blocked_until ? `tilt (${esc(s.reason)}), ${esc(hhmmOf(s.blocked_until))}-ig szünetel`
      : s.browser_only ? "most nem olvasható böngésző nélkül (a böngésző nézi)" : `kb. ${s.per_hour} kérés óránként (keret: ${I.hourly})`}`).join(" · ");
    const alerts = (I.alerts || []).map((a) => {
      const sites = (a.sites || []).map((k) => (I.sites.find((s) => s.key === k) || { name: k }).name).join(", ");
      const range = a.min_price || a.max_price ? ` · ${a.min_price ? price(a.min_price) : "0"}–${a.max_price ? price(a.max_price) : "bármennyi"}` : "";
      const last = Math.max(0, ...Object.values(a.last || {}));
      return `<li><span><b>${esc(a.name)}</b>
          <small class="muted">${esc(sites)} · ${a.mode === "cheap" ? "csak ami megéri" : "minden új"}${esc(range)}${(a.exclude || []).length ? ` · kivéve: ${esc(a.exclude.join(", "))}` : ""} · ${a.every} percenként</small>
          <small class="muted">${a.off ? "szünetel" : last ? `utoljára ${esc(ago(last))}` : "még nem nézte"}${a.found ? ` · eddig ${a.found} új` : ""}${a.found_at ? ` (legutóbb ${esc(ago(a.found_at))})` : ""}</small>
          ${Object.values(a.errors || {}).map((e) => `<small class="warn">${esc(e)}</small>`).join("")}
          ${a.url ? `<small><a href="${esc(a.url)}" target="_blank" rel="noopener noreferrer">az oldal</a></small>` : ""}</span>
        <span class="row"><button type="button" class="small quiet" data-ipause="${esc(a.id)}" data-off="${a.off ? "" : "1"}">${a.off ? "Folytatás" : "Szünet"}</button>
          <button type="button" class="small quiet" data-iremove="${esc(a.id)}">Törlés</button></span></li>`;
    }).join("");
    const hits = (I.hits || []).slice(0, 25).map((h) => `<li><span><a href="${esc(h.url)}" target="_blank" rel="noopener noreferrer">${esc(h.title || h.text)}</a>
        <small class="muted">${esc(h.site)} · <b>${esc(price(h.price))}</b>${h.drop ? ` (leárazta: ${esc(price(h.drop))})` : ""} · ${esc(ago(h.at))} · ${esc(h.alert_name)}</small>
        ${h.reference ? `<small class="${h.cheap ? "" : "muted"}">${h.cheap ? "<b>Megéri:</b> " : ""}szokásos ár kb. ${esc(price(h.reference))}${h.profit > 0 ? ` · kb. ${esc(price(h.profit))} haszon` : ""}</small>` : ""}
        ${(h.warnings || []).map((w) => `<small class="warn">Figyelj: ${esc(w)}</small>`).join("")}</span>
        ${h.deal ? `<span class="row"><button type="button" class="small" data-deal-buy="${esc(h.deal)}">Megvettem</button></span>` : ""}</li>`).join("");
    const presets = (I.presets || []).map((p) => `<button type="button" class="small" data-ipreset="${esc(p.url)}">${esc(p.name)}</button>`).join("");
    return `<h3 class="section-title">Azonnali figyelő: Hardverapró, Jófogás</h3>
      <label class="check"><input type="checkbox" id="di-enabled" ${I.enabled ? "checked" : ""}> <span><b>Szóljon, amint új hirdetés kerül fel</b> (a fenti órákban)</span></label>
      <p class="muted">Ezeken az oldalakon nincs „értesíts, ha új jön”: pár percenként megnézem a keresésed legújabb hirdetéseit
        (böngésző nélkül, így gyors, és nem vár a többi munkára), és rögtön szólok, a telefonon is. Oldalanként egyszerre egy
        kérés, szünetekkel; ha egy oldal tiltana, ott egy ideig leállok. Csak olvasok: venni te veszel.</p>
      ${I.enabled && siteLine ? `<p><small>${siteLine}</small></p>` : ""}
      <ul class="dealist">${alerts || '<li class="muted">Még nincs azonnali figyelő.</li>'}</ul>
      <form id="di-add" class="compose" autocomplete="off">
        <input id="di-target" placeholder="Mit figyeljek? Pl. RTX 3080, vagy egy oldal címe">
        <div class="checks">${(I.sites || []).map((s) => `<label class="check"><input type="checkbox" name="di-site" value="${esc(s.key)}" checked> ${esc(s.name)}</label>`).join("")}</div>
        <div class="row">
          <label><select id="di-mode"><option value="all">Minden új hirdetésről szóljon</option><option value="cheap">Csak ha megéri (a szokásos ár alatt)</option></select></label>
          <label>Nézze <select id="di-every">${(I.every_choices || [2, 3, 5, 10, 15]).map((m) => `<option value="${m}"${m === 3 ? " selected" : ""}>${m} percenként</option>`).join("")}</select></label>
        </div>
        <div class="row">
          <label>Ár: legalább <input id="di-min" type="number" inputmode="numeric" min="0" placeholder="0" style="width: 6em"> Ft</label>
          <label>legfeljebb <input id="di-max" type="number" inputmode="numeric" min="0" placeholder="bármennyi" style="width: 7em"> Ft</label>
        </div>
        <label class="field-label">Kizáró szavak <input id="di-exclude" placeholder="pl. hibás, tok, doboz"></label>
        <div class="row"><button type="submit" class="primary">Figyelő hozzáadása</button></div>
        ${presets ? `<div class="row presets"><small class="muted">Gyorsan (Hardverapró, csak ami megéri):</small>${presets}</div>` : ""}
      </form>
      ${hits ? `<details class="setup" open><summary>Friss hirdetések (${(I.hits || []).length})</summary><ul class="dealist">${hits}</ul></details>` : ""}`;
  }
  const hhmmOf = (t) => new Date(t * 1000).toLocaleTimeString("hu-HU", { hour: "2-digit", minute: "2-digit" });

  // Vinted-ajánlat: a Jarvis megnyitja a hirdetést, beírja az összeget, és a küldés előtt rákérdez (pontosan ezzel).
  async function makeOffer(id, amount) {
    const result = await J.postJson("/api/work/deals", { action: "offer", id, price: amount });
    J.toast(result.ok ? "Elkezdem; a küldés előtt rákérdezek, pontosan ezzel az összeggel." : (result.error || "Nem sikerült."));
  }

  function renderDeals() {
    const box = $("w-deals");
    const siteName = (key) => (deals.sites.find((s) => s.key === key) || { name: key }).name;
    const searches = deals.searches.map((s) => `<li><span><b>${esc(s.query)}</b>
        <small class="muted">${esc(searchRules(s, siteName))}</small>
        ${searchState(s)}</span>
        <span class="row"><button type="button" class="small quiet" data-deal-edit="${esc(s.id)}">Szerkesztés</button>
        <button type="button" class="small quiet" data-deal-remove="${esc(s.id)}">Törlés</button></span></li>`).join("");
    const p = deals.profit;
    const profitBox = p && (p.rows.length) ? `
      <h3 class="section-title">Haszon</h3>
      <p class="profit"><b>${esc(signed(p.month.profit))}</b> ebben a hónapban (${p.month.count} eladás) ·
        összesen <b>${esc(signed(p.total.profit))}</b> (${p.total.count} eladás${p.days !== null ? `, átlagosan ${p.days} nap alatt` : ""})
        ${p.open.count ? ` · bent van ${esc(price(p.open.spent))} (${p.open.count} tárgy még nem kelt el)` : ""}</p>
      <div class="row"><button type="button" class="small" data-job="ledger">Vételeim részletesen</button></div>` : "";
    // Megvettem: a vételár (alapból a hirdetés ára), a posta, és hogy postán jön-e (akkor „úton” a Vételeim fülön)
    const buyButton = (d) => d.bought ? '<small class="muted">Megvetted ✓</small>'
      : buying === d.id ? `<span class="row buyform">
          <label>Vételár <input id="d-paid" type="number" inputmode="numeric" min="1" value="${esc(d.price)}" aria-label="Vételár (Ft)"></label>
          <label>Posta <input id="d-ship" type="number" inputmode="numeric" min="0" placeholder="0" aria-label="Posta (Ft)"></label>
          <label class="check"><input id="d-transit" type="checkbox" checked> postán jön</label>
          <button type="button" class="small primary" data-deal-paid="${esc(d.id)}">Mentés</button></span>`
      : `<button type="button" class="small" data-deal-buy="${esc(d.id)}">Megvettem</button>`;
    const offerBox = (d) => d.offer_sent ? `<small>Ajánlat elküldve: <b>${esc(price(d.offer_sent.price))}</b> – ha az eladó
          elfogadja, a Vinted értesít.</small>`
      : d.offer ? `<span class="row"><input id="d-offer-${esc(d.id)}" type="number" min="1" value="${esc(d.offer)}" style="width: 6em"
          aria-label="Ajánlat (Ft)"> Ft <button type="button" class="small primary" data-offer="${esc(d.id)}">Ajánlatot teszek</button></span>` : "";
    const found = deals.deals.map((d) => `<li><span><a href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">${esc(d.text)}</a>
        <small class="muted">${esc(d.site)}${d.auction ? " (licit)" : ""} · <b>${esc(price(d.price))}</b>${d.drop ? ` (leárazta: ${esc(price(d.drop))})` : ""},
        ${d.auction ? "máshol" : "szokásos ár"} ${esc(price(d.median))}${d.auction ? ` · ${esc(leftText(d))} · legfeljebb <b>${esc(price(d.max_bid))}</b>-ig érdemes licitálni`
          : d.offer ? ` · ajánlattal (${esc(price(d.offer))}) kb. <b>${esc(price(d.profit))}</b> haszon`
          : d.profit !== undefined ? ` · várható haszon kb. <b>${esc(price(d.profit))}</b>` : ` (kb. ${esc(price(d.margin))}-tal olcsóbb)`}
        ${d.calibration ? " · a felmérés közben" : ""}${d.explore ? " · " + esc(d.explore) : ""}${d.note ? " · " + esc(d.note) : ""}</small>
        ${d.speed ? `<small class="muted">Eladási sebesség: ${esc(d.speed)}</small>` : ""}
        ${(d.warnings || []).map((w) => `<small class="warn">Figyelj: ${esc(w)}</small>`).join("")}
        ${offerBox(d)}</span>
        <span class="row">${buyButton(d)}
        <button type="button" class="small quiet" data-deal-dismiss="${esc(d.id)}">Elrejtés</button></span></li>`).join("");
    const book = deals.book || { products: 0, prices: 0, ready: 0 };
    const lookupRows = lookupResults === null ? "" : `<ul class="dealist">${lookupResults.map((r) => `<li><span><b>${esc(r.name)}</b>
        <small class="muted">${r.reference ? `kb. <b>${esc(price(r.reference.price))}</b> (${r.reference.n} ár, a többség
          ${esc(price(r.reference.p25))}–${esc(price(r.reference.p75))})` : `még nincs szokásos ár (${r.samples} ár, vagy nagyon szórnak)`}</small>
        ${r.speed_text ? `<small class="muted">${esc(r.speed_text)}</small>` : ""}</span></li>`).join("") || '<li class="muted">Ilyet még nem láttam.</li>'}</ul>`;
    const day = deals.day || [7, 23];
    const b = deals.browse || { on: false, sites: [], min_profit: 5000, status: "" };
    const laneRows = (deals.lanes || []).map((l) => `<li><span><b>${esc(l.name)}</b>${l.kind === "tanult" ? ' <small class="warn">tanult</small>' : ""}
        <small class="muted">${l.pages} oldal · ${l.candidates} jelölt · ${l.deals} jó találat${l.bought ? ` · ${l.bought} megvett` : ""}</small></span></li>`).join("");
    const hhmm = (t) => new Date(t * 1000).toLocaleTimeString("hu-HU", { hour: "2-digit", minute: "2-digit" });
    const entries = (journalDay ? journalEntries : deals.journal) || [];
    const journalRows = entries.map((e) => `<li><span><small class="muted">${esc(hhmm(e.at))}</small> ${esc(e.text)}</span></li>`).join("")
      || '<li class="muted">Erre a napra még nincs bejegyzés.</li>';
    const dayOptions = (deals.journal_days || []).map((d) => `<option value="${esc(d)}"${d === journalDay ? " selected" : ""}>${esc(d)}</option>`).join("");
    const edit = deals.searches.find((s) => s.id === editingSearch);
    const v = (key, fallback = "") => esc(edit && edit[key] != null ? edit[key] : fallback);
    const checked = (key) => !edit || (edit.sites || []).includes(key) ? "checked" : "";
    box.innerHTML = `
      <label class="check"><input type="checkbox" id="d-enabled" ${deals.enabled ? "checked" : ""}> <b>Bekapcsolva</b></label>
      <div class="row"><label>A gép általában
        <input id="d-day-start" type="number" min="0" max="23" value="${esc(day[0])}" style="width: 3.5em"> órától
        <input id="d-day-end" type="number" min="1" max="24" value="${esc(day[1])}" style="width: 3.5em"> óráig megy</label>
        <button type="button" class="small" id="d-day">Mentés</button></div>
      ${instantBox()}
      <h3 class="section-title">Böngészés napközben</h3>
      <label class="check"><input type="checkbox" id="d-browse" ${b.on ? "checked" : ""}> <b>Napközben böngésszen</b> (a fenti órákban)</label>
      <p class="muted">Ahogy egy ember: kb. 20 percig nézelődik egy oldalon (fél-egy percenként egy oldal, a Vinteden
        lassabban; néha megnyit egy hirdetést), aztán rögtön a következő oldalra vált. Csak az az oldal pihen, amelyiken épp
        volt (legalább 10 percet, a Vinted két órát); ha minden bejelölt oldal pihen, ő is. A Vinteden a kezdőoldallal
        kezd, a Hardveraprón a felkapott kategóriákat nézi (videókártya, memória, SSD, processzor, alaplap). Ha egy oldal
        robot-ellenőrzést kér, aznap már nem nézi. A modellt csak kötegben kérdezi (energiatakarékos), és amint talál
        valamit, amiből haszon lehet, szól. Tanul is: amelyik oldal többet hoz, azt gyakrabban nézi, a gyorsan kelő
        termékeket külön figyeli, és amit kétszer elrejtesz, arról egy hónapig nem szól.</p>
      <div class="checks">${deals.sites.map((s) => `<label class="check"><input type="checkbox" name="d-bsite" value="${esc(s.key)}" ${(b.sites || []).includes(s.key) ? "checked" : ""}> ${esc(s.name)}</label>`).join("")}</div>
      <div class="row"><label>Legalább <input id="d-bprofit" type="number" min="0" value="${esc(b.min_profit)}" style="width: 6em"> Ft haszon</label>
        <button type="button" class="small" id="d-bsave">Mentés</button></div>
      ${b.on && b.status ? `<p><small>${esc(b.status)}</small></p>` : ""}
      <details class="setup"><summary>Indulóoldalak, és mit hoztak eddig</summary><ul class="dealist">${laneRows}</ul></details>
      <p class="muted">Egy új keresésnél először felmérem, mennyiért adják most (egy modellhívás), és ha jóváhagyod, ahhoz
        az árhoz mérem a legújabb hirdetéseket, modell nélkül. Ami jóval olcsóbb, annak az oldalát is átnézem (elkelt-e,
        hibás-e, gyanús-e), és csak utána kérdezem meg a modellt. Csak olvasok: semmit nem veszek meg, és nem írok senkinek.
        A Vintedet legfeljebb kétóránként nézem.
        ${deals.browser ? "" : "<b>Ehhez Jarvis saját böngészője kell.</b>"}</p>
      ${profitBox}
      <h3 class="section-title">Keresések</h3>
      <ul class="dealist">${searches || '<li class="muted">Még nincs keresés.</li>'}</ul>
      <form id="d-add" class="compose" autocomplete="off">
        ${edit ? `<p><b>Szerkesztés: ${esc(edit.query)}</b></p>` : ""}
        <input id="d-query" placeholder="Mit keressek? Pl. RTX 3060 Ti, PS5 kontroller" value="${v("query")}">
        <div class="checks">${deals.sites.map((s) => `<label class="check"><input type="checkbox" name="d-site" value="${esc(s.key)}" ${checked(s.key)}> ${esc(s.name)}</label>`).join("")}</div>
        <div class="row">
          <label>Legalább <input id="d-discount" type="number" min="10" max="80" value="${v("discount", 30)}" style="width: 4em">%-kal olcsóbb,</label>
          <label>vagy legalább <input id="d-profit" type="number" min="0" placeholder="–" value="${v("min_profit")}" style="width: 6em"> Ft haszon</label>
        </div>
        <div class="row">
          <label>Ár: legalább <input id="d-min" type="number" min="0" placeholder="0" value="${v("min_price")}" style="width: 6em"> Ft</label>
          <label>legfeljebb <input id="d-max" type="number" min="0" placeholder="bármennyi" value="${v("max_price")}" style="width: 7em"> Ft</label>
        </div>
        <label class="field-label">Kizáró szavak <input id="d-exclude" placeholder="pl. tok, töltő, játék, doboz" value="${esc(edit ? (edit.exclude || []).join(", ") : "")}"></label>
        ${edit ? "" : `<label class="field-label">Szokásos ár, ha tudod <input id="d-ref-new" type="number" min="1" placeholder="üresen felmérem" style="width: 9em"> Ft</label>`}
        <div class="row">
          <label>Nézze <select id="d-every">${[30, 60, 180, 360].map((m) => `<option value="${m}"${m === Number(v("every", 180)) ? " selected" : ""}>${every(m)}</option>`).join("")}</select></label>
        </div>
        <div class="row"><button type="submit" class="primary">${edit ? "Mentés" : "Hozzáadás"}</button>
          ${edit ? '<button type="button" id="d-cancel">Mégse</button>' : '<button type="button" id="d-run">Keresés most</button>'}</div>
      </form>
      <h3 class="section-title">Figyelt oldalak (bármi, ami megéri)</h3>
      <p class="muted">Itt nem kell megadnod, mit keresel. A böngészőben nyiss meg egy kategóriát (pl. Hardverapró ›
        Videókártyák, Jófogás › Konzolok, Vinted › Férfi cipők) a legújabb elöl, a Vaterán a „hamarosan lejár”
        rendezéssel, és másold be az oldal címét. A hirdetéseket az árbázishoz mérem (modell nélkül); amit nem ismerek
        fel, azt a modell nevezi meg, és ami megérhet, annak máshol is megnézem az árát. Licitálni és venni nem fogok
        helyetted: megmondom, meddig érdemes.</p>
      <ul class="dealist">${(deals.watches || []).map((w) => `<li><span><b>${esc(w.name)}</b>
          <small class="muted">legalább ${esc(price(w.min_profit))} haszon${(w.platform || "vatera") === "vatera" ? ` · licitnél a zárás előtt legfeljebb ${w.hours} órával` : ""} · ${esc(every(w.every))}</small>
          <small><a href="${esc(w.url)}" target="_blank" rel="noopener noreferrer">az oldal</a></small></span>
          <button type="button" class="small quiet" data-watch-remove="${esc(w.id)}">Törlés</button></li>`).join("") || '<li class="muted">Még nincs figyelt oldal.</li>'}</ul>
      <form id="d-watch" class="compose" autocomplete="off">
        <input id="dw-target" placeholder="Egy találati oldal címe (Vinted, Jófogás, Vatera, Hardverapró), vagy Vatera-keresőszó">
        <div class="row">
          <label>Legalább <input id="dw-profit" type="number" min="0" value="5000" style="width: 6em"> Ft haszon,</label>
          <label>licitnél a zárás előtt legfeljebb <input id="dw-hours" type="number" min="1" max="72" value="6" style="width: 4em"> órával</label>
        </div>
        <div class="row"><label>Nézze <select id="dw-every">${[30, 60, 180, 360].map((m) => `<option value="${m}"${m === 60 ? " selected" : ""}>${every(m)}</option>`).join("")}</select></label></div>
        <div class="row"><button type="submit" class="primary">Oldal hozzáadása</button></div>
      </form>
      <h3 class="section-title">Mennyit ér?</h3>
      <p class="muted">Árbázis: ${book.products} termék, ${book.prices} ár (${book.ready} termékre már van szokásos ár).
        Minden hirdetés ára bekerül, amit látok; modell és oldalbetöltés nélkül megmondja, mennyiért adják.</p>
      <form id="d-lookup" class="compose" autocomplete="off">
        <div class="row"><input id="dl-query" placeholder="Pl. RTX 3060 Ti, iPhone 13, Makita DHP482" value="${esc(lookupQuery)}">
          <button type="submit">Megnézem</button></div>
      </form>
      ${lookupRows}
      <h3 class="section-title">Olcsó hirdetések</h3>
      <ul class="dealist">${found || '<li class="muted">Még nincs találat.</li>'}</ul>
      <h3 class="section-title">Napló</h3>
      <div class="row"><label>Nap <select id="d-jday"><option value="">Ma</option>${dayOptions}</select></label></div>
      <ul class="dealist">${journalRows}</ul>`;
    $("d-enabled").addEventListener("change", (e) => dealAction({ action: "enable", on: e.target.checked }));
    if ($("di-enabled")) {
      $("di-enabled").addEventListener("change", async (e) => {
        if (await dealAction({ action: "instant_enable", on: e.target.checked }))
          J.toast(e.target.checked ? "Rendben: amint új hirdetés kerül fel, szólok." : "Kikapcsoltam az azonnali figyelőt.");
      });
      let modeTouched = false;
      $("di-mode").addEventListener("change", () => { modeTouched = true; });
      // egy egész oldal (kategória) új hirdetései: alapból csak ami megéri, különben túl sok lenne
      $("di-target").addEventListener("input", (e) => { if (!modeTouched) $("di-mode").value = /^https?:\/\//i.test(e.target.value.trim()) ? "cheap" : "all"; });
      $("di-add").addEventListener("submit", async (e) => {
        e.preventDefault();
        const sites = [...box.querySelectorAll('input[name="di-site"]:checked')].map((i) => i.value);
        if (await dealAction({ action: "instant_add", target: $("di-target").value, sites, mode: $("di-mode").value,
                               min_price: $("di-min").value, max_price: $("di-max").value, exclude: $("di-exclude").value,
                               every: $("di-every").value }))
          J.toast(deals.instant && deals.instant.enabled ? "Rendben: először megjegyzem, ami már fent van, utána minden újról szólok."
            : "Felvettem. Kapcsold be fent az azonnali figyelőt.");
      });
      for (const b of box.querySelectorAll("[data-ipreset]")) b.addEventListener("click", async () => {
        if (await dealAction({ action: "instant_add", target: b.dataset.ipreset, mode: "cheap" })) J.toast("Felvettem: csak arról szólok, ami megéri.");
      });
      for (const b of box.querySelectorAll("[data-iremove]")) b.addEventListener("click", () => dealAction({ action: "instant_remove", id: b.dataset.iremove }));
      for (const b of box.querySelectorAll("[data-ipause]")) b.addEventListener("click", () => dealAction({ action: "instant_pause", id: b.dataset.ipause, off: !!b.dataset.off }));
    }
    $("d-browse").addEventListener("change", async (e) => {
      if (await dealAction({ action: "browse", on: e.target.checked }))
        J.toast(e.target.checked ? "Rendben: napközben böngészem az oldalakat, és szólok, ha találok valamit." : "Kikapcsoltam a böngészést.");
    });
    $("d-bsave").addEventListener("click", async () => {
      const sites = [...box.querySelectorAll('input[name="d-bsite"]:checked')].map((i) => i.value);
      if (await dealAction({ action: "browse", sites, min_profit: $("d-bprofit").value })) J.toast("Mentve.");
    });
    $("d-jday").addEventListener("change", async (e) => {
      journalDay = e.target.value;
      journalEntries = null;
      if (journalDay) {
        const result = await J.postJson("/api/work/deals", { action: "journal", day: journalDay });
        journalEntries = result.ok ? result.entries : [];
      }
      renderDeals();
    });
    $("d-day").addEventListener("click", async () => {
      if (await dealAction({ action: "day", start: $("d-day-start").value, end: $("d-day-end").value }))
        J.toast("Rendben: az éjjel záruló licitekről még este szólok.");
    });
    $("d-lookup").addEventListener("submit", async (e) => {
      e.preventDefault();
      lookupQuery = $("dl-query").value;
      const result = await J.postJson("/api/work/deals", { action: "lookup", query: lookupQuery });
      if (!result.ok) { J.toast(result.error || "Nem sikerült."); return; }
      lookupResults = result.results;
      renderDeals();
    });
    $("d-add").addEventListener("submit", async (e) => {
      e.preventDefault();
      const sites = [...box.querySelectorAll('input[name="d-site"]:checked')].map((i) => i.value);
      const fields = { query: $("d-query").value, sites, discount: $("d-discount").value, min_profit: $("d-profit").value,
                       min_price: $("d-min").value, max_price: $("d-max").value, exclude: $("d-exclude").value,
                       every: $("d-every").value };
      const result = edit ? await dealAction({ action: "update", id: edit.id, ...fields })
        : await dealAction({ action: "add", ...fields, reference: $("d-ref-new").value });
      if (result) { editingSearch = null; renderDeals(); }
    });
    if ($("d-cancel")) $("d-cancel").addEventListener("click", () => { editingSearch = null; renderDeals(); });
    if ($("d-run")) $("d-run").addEventListener("click", async () => {
      const result = await J.postJson("/api/work/deals", { action: "run" });
      J.toast(result.ok ? "Keresem; ha találok valamit, szólok." : (result.error || "Nem sikerült."));
    });
    for (const b of box.querySelectorAll("[data-deal-edit]")) b.addEventListener("click", () => {
      editingSearch = b.dataset.dealEdit; renderDeals(); $("d-add").scrollIntoView({ block: "start" });
    });
    for (const b of box.querySelectorAll("[data-ref-edit]")) b.addEventListener("click", () => { editingRef = b.dataset.refEdit; renderDeals(); $("d-ref").focus(); });
    for (const b of box.querySelectorAll("[data-ref-save]")) b.addEventListener("click", async () => {
      if (await dealAction({ action: "accept", id: b.dataset.refSave, price: $("d-ref").value })) { editingRef = null; renderDeals(); }
    });
    for (const b of box.querySelectorAll("[data-accept]")) b.addEventListener("click", async () => {
      if (await dealAction({ action: "accept", id: b.dataset.accept })) J.toast("Rendben: ehhez az árhoz mérem a hirdetéseket.");
    });
    for (const b of box.querySelectorAll("[data-recalibrate]")) b.addEventListener("click", async () => {
      if (await dealAction({ action: "recalibrate", id: b.dataset.recalibrate })) J.toast("A következő keresésnél újra felmérem.");
    });
    for (const b of box.querySelectorAll("[data-deal-remove]")) b.addEventListener("click", () => dealAction({ action: "remove", id: b.dataset.dealRemove }));
    $("d-watch").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (await dealAction({ action: "watch_add", target: $("dw-target").value, min_profit: $("dw-profit").value,
                             hours: $("dw-hours").value, every: $("dw-every").value })) J.toast("Rendben, figyelem az oldalt.");
    });
    for (const b of box.querySelectorAll("[data-watch-remove]")) b.addEventListener("click", () => dealAction({ action: "watch_remove", id: b.dataset.watchRemove }));
    for (const b of box.querySelectorAll("[data-offer]")) b.addEventListener("click", () => makeOffer(b.dataset.offer, $(`d-offer-${b.dataset.offer}`).value));
    for (const b of box.querySelectorAll("[data-deal-dismiss]")) b.addEventListener("click", () => dealAction({ action: "dismiss", id: b.dataset.dealDismiss }));
    for (const b of box.querySelectorAll("[data-deal-buy]")) b.addEventListener("click", () => {
      buying = b.dataset.dealBuy;
      renderDeals();
      if (!$("d-paid")) { J.toast("Ez a találat már nincs a listában."); return; }
      $("d-paid").scrollIntoView({ block: "center" });
      $("d-paid").focus();
    });
    for (const b of box.querySelectorAll("[data-deal-paid]")) b.addEventListener("click", async () => {
      const result = await dealAction({ action: "bought", id: b.dataset.dealPaid, price: $("d-paid").value,
                                        shipping: $("d-ship").value, in_transit: $("d-transit").checked });
      if (!result) return;
      buying = null;
      J.toast("Felvettem a Vételeim közé és az eladandók közé. Pipáld be, hova töltsem fel.");
      openId = result.item;
      await load();
      setJob("sales");
    });
    for (const b of box.querySelectorAll("[data-open]")) b.addEventListener("click", () => { openId = b.dataset.open; setJob("sales"); });
    for (const b of box.querySelectorAll("[data-job]")) b.addEventListener("click", () => setJob(b.dataset.job));
  }

  // --- Vételeim: a megvett tárgyak költségei (vételár, posta, egyéb), mennyiért adnád tovább, és a haszon ---
  let ledger = null;          // a /api/work/ledger válasza (deals.profit_report)
  let ledgerFilter = "all";   // all, úton, nálad, fent, eladva
  let ledgerOpen = null;      // a tárgy, aminek a részleteit szerkeszted
  let ledgerAdding = false;   // az „Új vétel” űrlap nyitva
  let ledgerTimer = null;
  const LEDGER_FILTERS = [["all", "Mind"], ["úton", "Úton"], ["nálad", "Nálad"], ["fent", "Fent"], ["eladva", "Eladva"]];
  const STAGE_CHIP = { "úton": "kerdes", "nálad": "kesz", "fent": "feltoltve", "eladva": "eladva" };
  const STAGE_TEXT = { "úton": "Úton", "nálad": "Nálad", "fent": "Fent van", "eladva": "Eladva" };
  // +12 300 Ft / −4 000 Ft: a haszon előjellel (a veszteség is látsszon)
  function signed(value) {
    if (value === null || value === undefined) return "–";
    return (value > 0 ? "+" : value < 0 ? "−" : "") + price(Math.abs(value));
  }
  const dayInput = (t) => {
    if (!t) return "";
    const d = new Date(t * 1000);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  };
  async function loadLedger() {
    try {
      const response = await fetch("/api/work/ledger?t=" + encodeURIComponent(token));
      if (response.ok) ledger = await response.json();
    } catch { /* a következő megnyitáskor újra */ }
    if (job === "ledger") render();
  }
  // egy tárgy változott (pl. eladva, új ár): a fül összesítése is frissül (egyszerre csak egy kérés)
  function refreshLedger() {
    if (job !== "ledger") return;
    clearTimeout(ledgerTimer);
    ledgerTimer = setTimeout(loadLedger, 400);
  }
  async function ledgerAction(body) {
    const result = await J.postJson("/api/work/ledger", body);
    if (!result.ok) { J.toast(result.error || "Nem sikerült."); return false; }
    ledger = result;
    render();
    return result;
  }

  function ledgerCard(r) {
    const sold = r.stage === "eladva";
    const thumb = r.photo ? `<img class="thumb" src="${photoUrl(r.id, esc(r.photo), true)}" alt="">` : '<span class="nothumb">nincs fotó</span>';
    const extra = [r.bought_shipping ? `posta ${price(r.bought_shipping)}` : "", r.bought_extra ? `egyéb ${price(r.bought_extra)}` : ""].filter(Boolean);
    const sale = sold ? r.sold_price : r.estimate;
    const gain = sold ? r.profit : r.expected;
    const when = sold ? (r.sold_at ? `${r.days} nap alatt kelt el` : "") : (r.days ? `${r.days} napja vetted` : "ma vetted");
    const facts = [r.bought_from, when];
    const now = r.market_now ? `Most kb. <b>${esc(price(r.market_now.price))}</b>-ért adják (${r.market_now.n} ár)${r.market_now.speed ? ` · ${esc(r.market_now.speed)}` : ""}` : "";
    const sellCost = (r.sell_shipping || 0) + (r.sell_fee || 0);
    return `<article class="lcard${sold ? " sold" : ""}">
      <button type="button" class="cardthumb" data-lopen="${esc(r.id)}" aria-label="${esc(r.name)}: részletek">${thumb}</button>
      <div class="body">
        <div class="lhead"><button type="button" class="linklike title" data-lopen="${esc(r.id)}">${esc(r.name)}</button>
          <span class="chip ${STAGE_CHIP[r.stage]}">${STAGE_TEXT[r.stage]}</span></div>
        <div class="lnums">
          <span><small>Bekerülés</small><b>${esc(price(r.cost))}</b>${extra.length ? `<small>${esc(price(r.bought_price))} + ${esc(extra.join(" + "))}</small>` : ""}</span>
          <span><small>${sold ? "Eladva" : "Eladnád"}</small><b>${sale !== null && sale !== undefined ? esc(price(sale)) : "?"}</b>${sellCost ? `<small>költség ${esc(price(sellCost))}</small>` : ""}</span>
          <span><small>${sold ? "Haszon" : "Várható haszon"}</small><b class="${gain < 0 ? "loss" : ""}">${esc(signed(gain))}</b>${r.roi !== null ? `<small>${r.roi}%</small>` : ""}</span>
        </div>
        <small class="muted">${esc(facts.filter(Boolean).join(" · "))}${r.source && safeUrl(r.source) ? ` · <a href="${esc(r.source)}" target="_blank" rel="noopener noreferrer">a hirdetés</a>` : ""}</small>
        ${!sold && now ? `<small class="muted">${now}</small>` : ""}
        ${!sold && sale === null ? '<small class="warn">Add meg, mennyiért adnád tovább: abból számolom a várható hasznot.</small>' : ""}
        ${(r.advice || []).map((a) => `<small class="warn">${esc(a)}</small>`).join("")}
        ${r.trade_note ? `<small class="note">${esc(r.trade_note)}</small>` : ""}
        <div class="row">
          <button type="button" class="small" data-lopen="${esc(r.id)}" aria-expanded="${ledgerOpen === r.id}">${ledgerOpen === r.id ? "Bezárás" : "Részletek"}</button>
          ${r.stage === "úton" ? `<button type="button" class="small" data-larrived="${esc(r.id)}">Megérkezett</button>` : ""}
          <button type="button" class="small quiet" data-open="${esc(r.id)}">A tárgy lapja</button>
        </div>
      </div>
      ${ledgerOpen === r.id ? ledgerForm(r) : ""}</article>`;
  }

  function ledgerForm(r) {
    const num = (name, label, value, hint = "") => `<div class="field"><label for="l-${name}">${label}</label>
      <input id="l-${name}" data-lfield="${name}" type="number" inputmode="numeric" min="0" value="${esc(value ?? "")}" placeholder="${esc(hint)}"></div>`;
    const sold = r.stage === "eladva";
    const sites = (ledger.sites || []).map((s) => `<option value="${esc(s)}">`).join("");
    return `<form class="lform" data-lsave="${esc(r.id)}" autocomplete="off">
      <div class="fields">
        ${num("bought_price", "Vételár (Ft)", r.bought_price)}
        ${num("bought_shipping", "Posta a vételkor (Ft)", r.bought_shipping, "0")}
        ${num("bought_extra", "Egyéb költség (Ft)", r.bought_extra, "javítás, tisztítás…")}
        <div class="field"><label for="l-bought_from">Honnan</label>
          <input id="l-bought_from" data-lfield="bought_from" list="l-sites" value="${esc(r.bought_from)}" placeholder="pl. Hardverapró"></div>
        <div class="field"><label for="l-bought_at">Mikor vetted</label>
          <input id="l-bought_at" data-lfield="bought_at" data-day="${esc(dayInput(r.bought_at))}" type="date" value="${esc(dayInput(r.bought_at))}"></div>
        ${num("market_price", "Szokásos ára (Ft)", r.market_price, r.market_now ? String(r.market_now.price) : "")}
        ${num("target_price", "Mennyiért adnád tovább (Ft)", r.target_price, r.price ? String(r.price) : "")}
        ${num("sell_shipping", "Posta az eladáskor (Ft)", r.sell_shipping, "ha te fizeted")}
        ${num("sell_fee", "Eladási díj (Ft)", r.sell_fee, "jutalék, kiemelés")}
        ${sold ? num("sold_price", "Eladási ár (Ft)", r.sold_price) : ""}
        ${sold ? `<div class="field"><label for="l-sold_at">Mikor kelt el</label>
          <input id="l-sold_at" data-lfield="sold_at" data-day="${esc(dayInput(r.sold_at))}" type="date" value="${esc(dayInput(r.sold_at))}"></div>` : ""}
        <div class="field wide"><label for="l-trade_note">Megjegyzés</label>
          <textarea id="l-trade_note" data-lfield="trade_note" rows="2" placeholder="pl. csomagszám, az eladó neve, egy apró hiba">${esc(r.trade_note)}</textarea></div>
      </div>
      <datalist id="l-sites">${sites}</datalist>
      ${sold ? "" : `<label class="check"><input type="checkbox" data-lfield="in_transit" ${r.in_transit ? "checked" : ""}> Még úton van (postán jön)</label>`}
      <div class="row"><button type="submit" class="primary small">Mentés</button>
        <button type="button" class="small" data-lclose>Mégse</button></div>
    </form>`;
  }

  function renderLedger() {
    const box = $("w-ledger");
    if (!ledger) { box.innerHTML = '<p class="muted">Betöltöm…</p>'; return; }
    const L = ledger;
    const counts = L.stages || {};
    const rows = L.rows.filter((r) => ledgerFilter === "all" || r.stage === ledgerFilter);
    const months = (L.months || []);
    const best = Math.max(1, ...months.map((m) => Math.abs(m.profit)));
    const monthRows = months.map((m) => `<li><span class="mname">${esc(m.month.replace("-", ". "))}.</span>
        <span class="mbar"><i style="width: ${Math.round(Math.abs(m.profit) / best * 100)}%"${m.profit < 0 ? ' class="loss"' : ""}></i></span>
        <b>${esc(signed(m.profit))}</b><small class="muted">${m.count} eladás</small></li>`).join("");
    const sites = (L.sites || []).map((s) => `<option value="${esc(s)}">`).join("");
    box.innerHTML = `
      <div class="tiles">
        <div class="tile"><small>Ebben a hónapban</small><b>${esc(signed(L.month.profit))}</b><small>${L.month.count} eladás</small></div>
        <div class="tile"><small>Összesen</small><b>${esc(signed(L.total.profit))}</b>
          <small>${L.total.count} eladás${L.total.roi !== null ? ` · ${L.total.roi}%` : ""}${L.days !== null ? ` · átl. ${L.days} nap` : ""}</small></div>
        <div class="tile"><small>Bent van</small><b>${esc(price(L.open.spent) || "0 Ft")}</b>
          <small>${L.open.count} tárgy${counts["úton"] ? ` · ${counts["úton"]} úton` : ""}</small></div>
        <div class="tile"><small>Várható haszon</small><b>${esc(signed(L.open.expected))}</b>
          <small>${L.open.unknown ? `${L.open.unknown} tárgynál nincs ár` : "ha a tervezett áron elkelnek"}</small></div>
      </div>
      ${monthRows ? `<details class="setup"><summary>Havonta</summary><ul class="months">${monthRows}</ul></details>` : ""}
      <div class="row lbar">
        <div class="filters" role="group" aria-label="Szűrés">${LEDGER_FILTERS.map(([key, label]) => {
          const n = key === "all" ? L.rows.length : (counts[key] || 0);
          return `<button type="button" data-lfilter="${key}" aria-pressed="${ledgerFilter === key}">${label}<span class="count">${n}</span></button>`;
        }).join("")}</div>
        <button type="button" class="small${ledgerAdding ? "" : " primary"}" id="l-add-toggle">${ledgerAdding ? "Mégse" : "+ Új vétel"}</button>
      </div>
      ${ledgerAdding ? `<form id="l-add" class="compose" autocomplete="off">
        <input id="la-name" placeholder="Mit vettél? Pl. RTX 3060 Ti, Nike Air Max 90">
        <div class="fields">
          <div class="field"><label for="la-price">Vételár (Ft)</label><input id="la-price" type="number" inputmode="numeric" min="0"></div>
          <div class="field"><label for="la-ship">Posta (Ft)</label><input id="la-ship" type="number" inputmode="numeric" min="0" placeholder="0"></div>
          <div class="field"><label for="la-from">Honnan</label><input id="la-from" list="la-sites" placeholder="pl. Jófogás, bolhapiac"></div>
          <div class="field"><label for="la-target">Mennyiért adnád tovább (Ft)</label><input id="la-target" type="number" inputmode="numeric" min="0"></div>
        </div>
        <datalist id="la-sites">${sites}</datalist>
        <label class="check"><input id="la-transit" type="checkbox"> Még úton van (postán jön)</label>
        <div class="row"><button type="submit" class="primary">Felveszem</button></div>
      </form>` : ""}
      <div class="ledger-list">${rows.map(ledgerCard).join("") || `<p class="muted">${L.rows.length ? "Ebben a csoportban nincs tárgy."
        : "Még nincs megvett tárgy. Az Automata kereskedés találatainál a Megvettem gombbal, vagy itt az Új vétellel veheted fel; bármelyik tárgy lapján a Vételár is ide teszi."}</p>`}</div>
      <p class="muted">A haszon: az eladási ár, mínusz az eladás költségei (posta, díj), mínusz amibe került (vételár, posta,
        egyéb). Amíg nem kelt el, a várható haszon a tervezett eladási árral (ha nincs, a hirdetési árral) számol. A
        „most kb.” ár az árbázisból jön: ennyiért hirdetik most a hasonlókat.</p>`;
    for (const b of box.querySelectorAll("[data-lfilter]")) b.addEventListener("click", () => { ledgerFilter = b.dataset.lfilter; renderLedger(); });
    $("l-add-toggle").addEventListener("click", () => { ledgerAdding = !ledgerAdding; renderLedger(); if (ledgerAdding) $("la-name").focus(); });
    if ($("l-add")) $("l-add").addEventListener("submit", async (e) => {
      e.preventDefault();
      const result = await ledgerAction({ action: "add", name: $("la-name").value, price: $("la-price").value,
        bought_shipping: $("la-ship").value, bought_from: $("la-from").value, target_price: $("la-target").value,
        in_transit: $("la-transit").checked });
      if (result) { ledgerAdding = false; ledgerOpen = null; renderLedger(); J.toast("Felvettem; az eladandók között is ott van."); load(); }
    });
    for (const b of box.querySelectorAll("[data-lopen]")) b.addEventListener("click", () => {
      ledgerOpen = ledgerOpen === b.dataset.lopen ? null : b.dataset.lopen;
      renderLedger();
    });
    for (const b of box.querySelectorAll("[data-lclose]")) b.addEventListener("click", () => { ledgerOpen = null; renderLedger(); });
    for (const b of box.querySelectorAll("[data-larrived]")) b.addEventListener("click", async () => {
      if (await ledgerAction({ action: "update", id: b.dataset.larrived, fields: { in_transit: false } })) J.toast("Megérkezett: most már nálad van.");
    });
    for (const b of box.querySelectorAll("[data-open]")) b.addEventListener("click", () => { openId = b.dataset.open; setJob("sales"); });
    for (const form of box.querySelectorAll("[data-lsave]")) form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fields = {};
      for (const input of form.querySelectorAll("[data-lfield]")) {
        const name = input.dataset.lfield;
        if (input.type === "checkbox") fields[name] = input.checked;
        else if (input.type === "date") {
          // csak ha átírtad a napot (különben a napon belüli időpont maradna el)
          if (input.value !== input.dataset.day) fields[name] = input.value ? new Date(`${input.value}T12:00:00`).getTime() / 1000 : 0;
        } else fields[name] = input.value;
      }
      if (!String(fields.bought_price || "").trim()) { J.toast("A vételár kell (különben kikerül a Vételeim közül)."); return; }
      if (await ledgerAction({ action: "update", id: form.dataset.lsave, fields })) { ledgerOpen = null; renderLedger(); J.toast("Mentve."); }
    });
  }

  function render() {
    foldCompose();
    $("w-deals").hidden = job !== "deals";
    $("w-ledger").hidden = job !== "ledger";
    $("w-filters").hidden = job !== "sales";
    if (job === "deals") {
      $("w-msgs").hidden = $("w-list-view").hidden = $("w-detail").hidden = true;
      if (!$("w-deals").contains(document.activeElement) || !document.activeElement.matches("input")) renderDeals();
      return;
    }
    if (job === "ledger") {
      $("w-msgs").hidden = $("w-list-view").hidden = $("w-detail").hidden = true;
      if (!$("w-ledger").contains(document.activeElement) || !document.activeElement.matches("input, textarea, select")) renderLedger();
      return;
    }
    renderFilters();
    $("w-msgs").hidden = filter !== "msgs";
    if (filter === "msgs") {
      openId = null;
      $("w-list-view").hidden = true;
      $("w-detail").hidden = true;
      if (convOpen && !convs.has(convOpen)) convOpen = null;
      keepTyping($("w-msgs"), renderMessages);
      return;
    }
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
      `<span class="count">${counts[key]}</span></button>`).join("") + (messagesOn
      ? `<button type="button" data-filter="msgs" aria-pressed="${filter === "msgs"}">Üzenetek` +
        `<span class="count">${[...convs.values()].filter(isNew).length}</span></button>` : "");
  }

  function renderList() {
    const list = [...items.values()].sort((a, b) => b.created - a.created).filter(matches);
    $("w-list").innerHTML = list.map(card).join("") ||
      `<p class="muted">${items.size ? "Nincs ilyen hirdetés." : "Még nincs hirdetésed. Fent kezdheted."}</p>`;
  }

  function siteChips(item) {
    return item.targets.map((key) => {
      const state = siteState(item, key);
      const flagged = state === POSTED && !item.sold ? CHECKED[(item.platforms[key] || {}).check] : "";
      const mark = flagged ? "?" : state === POSTED ? "✓" : state === FAILED ? "✕" : "…";
      const cls = flagged || state === FAILED ? "bad" : state === POSTED ? "on" : "";
      return `<span class="site ${cls}" title="${esc(flagged || (item.platforms[key] || {}).note || state || "még nincs fent")}">` +
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
      // „3 Ft-ra gondoltál?”: az Igen a visszakérdezett összeg (a Marad és a Legyen ugyanúgy választható)
      const yes = p.confirm ? `<button type="button" class="small primary" data-price="confirmed" data-id="${item.id}">` +
        `Igen: ${esc(price(p.confirm))}</button>` : "";
      return `<div class="ask"><p>${esc(p.question)}</p><div class="row">${yes}
        <button type="button" class="small" data-price="keep" data-id="${item.id}">Marad: ${esc(price(item.price))}</button>
        ${p.suggested ? `<button type="button" class="small${p.confirm ? "" : " primary"}" data-price="suggested" data-id="${item.id}">Legyen: ${esc(price(p.suggested))}</button>` : ""}
      </div></div>`;
    }
    return "";
  }

  function card(item) {
    const thumb = item.photos.length
      ? `<img class="thumb" src="${photoUrl(item.id, esc(item.photos[0]), true)}" alt="">${photoBadge(item.photo_note)}`
      : '<span class="nothumb">nincs fotó</span>';
    const busy = uploading.get(item.id) || item.busy;
    const marks = flags(item);
    const facts = [price(item.price), online(item)].filter(Boolean).join(" · ");
    const label = item.name + " részletei" + (item.photo_note ? ". " + item.photo_note : "");
    return `<div class="card${item.sold ? " sold" : ""}" data-id="${item.id}">
      <button type="button" class="cardthumb" data-open="${item.id}" aria-label="${esc(label)}">${thumb}</button>
      <div class="body">
        <div class="row tight"><span class="chip ${STATUS_CLASS[item.status] || ""}">${esc(item.status)}</span>
          <span class="facts">${esc(facts)}</span></div>
        <button type="button" class="title linklike" data-open="${item.id}">${esc(item.name)}</button>
        <div class="sites">${siteChips(item)}</div>
        ${marks.length ? `<div class="flags">${marks.map((m) => `<span class="flag">${esc(m)}</span>`).join("")}</div>` : ""}
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
    const warnings = ((item.photo_warnings || {})[name] || []).map((w) => w.text).join(" ");
    return `<div class="photo${index === 0 ? " first" : ""}">
      <img src="${photoUrl(item.id, esc(name), true)}" alt="${index + 1}. fotó">
      ${index === 0 ? '<span class="tag">borító</span>' : ""}
      ${photoBadge(warnings)}
      <div class="tools">
        ${index > 0 ? `<button type="button" data-action="cover" data-name="${esc(name)}" title="Legyen ez az első kép">Borító</button>` : "<span></span>"}
        <button type="button" data-action="removephoto" data-name="${esc(name)}" aria-label="${index + 1}. fotó törlése">✕</button>
      </div></div>`;
  }

  // Miért maradt ki egy-egy találat a modell elől (pl. „3 kereső vagy csere, 2 más termék”); a régi kutatásnál "".
  function droppedText(r) {
    return Object.entries(r.dropped || {}).filter(([, n]) => n > 0).map(([why, n]) =>
      why === "kevésbé illő" && r.kept ? `${n} kevésbé illő (a ${r.kept} legjobban illő maradt)`
        : `${n} ${DROPPED[why] || why}`).join(", ");
  }

  // Az összefoglaló a fotók alatt; ha a borítóval gond van, a gombbal a javasolt kerül előre (csak kérésre).
  function photoNote(item) {
    if (!item.photo_note) return "";
    const cover = item.photos.indexOf(item.photo_cover) + 1;
    const button = cover > 1 ? ` <button type="button" class="small" data-action="cover" data-name="${esc(item.photo_cover)}">` +
      `Legyen ${az(cover)} ${cover}. a borító</button>` : "";
    return `<p class="photonote">${esc(item.photo_note)}${button}</p>`;
  }

  function researchBlock(item) {
    const r = item.research || {};
    if (!r.at) return "";
    const similar = r.similar || [];
    let html = item.verdict ? `<p class="verdict">${esc(item.verdict)}</p>` : "";
    if (r.reason) html += `<p class="muted">${esc(r.reason)}</p>`;
    const dropped = (r.count || 0) - (r.kept ?? (r.count || 0));
    const why = droppedText(r) || "kereső, csomag, alkatrész, a sajátod vagy más termék";
    html += `<p class="muted">Keresés: „${esc(r.query)}”, ${r.count || 0} találat` +
            (dropped > 0 ? ` (${dropped} kiszűrve: ${esc(why)})` : "") +
            (similar.length ? `, ebből ${similar.length} hasonló:` : ".") + "</p>";
    if (item.research_stale) html += '<p class="muted">Ez az árkutatás egy hétnél régebbi; árazás előtt frissítem.</p>';
    if (similar.length) {
      html += "<ul>" + similar.map((s) => {
        const text = `${esc(s.site)}: ${esc((s.text || "").slice(0, 90))}`;
        const link = safeUrl(s.url) ? `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${text}</a>` : text;
        const bid = s.auction ? ' <span class="auction" title="Licit: a mostani ajánlat, nem végleges ár">(licit)</span>' : "";
        return `<li>${link} – ${esc(price(s.price))}${bid}</li>`;
      }).join("") + "</ul>";
    }
    // a robusztus statisztika (selling.price_stats): a hasonlók alsó és felső negyede, és mennyire megbízható
    if (similar.length && r.p25 !== undefined && r.p75 !== undefined) {
      const band = r.p25 < r.p75 ? `${price(r.p25)} – ${price(r.p75)}` : `${price(r.median)} körül`;
      const basis = [r.n ? `${r.n} ár alapján` : "", r.confidence === "bizonytalan"
        ? "bizonytalan: kevés vagy nagyon eltérő ár" : r.confidence || ""].filter(Boolean).join(", ");
      html += `<p class="muted">A hasonlók középső fele: ${esc(band)}, középérték ${esc(price(r.median))}` +
              (basis ? ` (${esc(basis)})` : "") + "." + (r.outliers ? ` ${r.outliers} kilógó árat kihagytam.` : "") + "</p>";
    }
    for (const error of r.errors || []) html += `<p class="muted">${esc(error)}</p>`;
    return `<section aria-labelledby="h-research"><h3 id="h-research">Árkutatás</h3>` +
           `<div class="research">${html}</div></section>`;
  }

  // Mikor nézte meg a háttérellenőrzés ezt a hirdetést, és mit látott (ha nem nyílt meg, azt is).
  function checkNote(item, state) {
    if (state.status !== POSTED || item.sold || !state.checked_at) return "";
    const at = `Ellenőrizve: ${shortDate(state.checked_at)}`;
    if (CHECKED[state.check]) return `${at}, ${state.check_note || CHECKED[state.check].toLowerCase()}.`;
    if (state.check === "live" && !state.check_note) return `${at}, fent van.`;
    return `${at}, de nem nyílt meg${state.check_note ? ` (${state.check_note})` : ""}; holnap újra megnézem.`;
  }

  // A fent lévő hirdetésben még a régi ár vagy cím van: Jarvis nem írja át, ott kézzel kell.
  function driftText(item, drift) {
    if (!drift) return "";
    const parts = [];
    if (drift.price !== undefined) parts.push(`Ár eltér: a hirdetésben még ${price(drift.price)}, itt ${price(item.price) || "nincs ár"}.`);
    if (drift.title) parts.push(`Cím eltér: a hirdetésben még „${drift.title}”.`);
    return parts.join(" ") + " Jarvis a fent lévő hirdetést nem írja át; ott kézzel módosítsd.";
  }

  function platformRow(item, p) {
    const state = item.platforms[p.key] || {};
    const chosen = item.targets.includes(p.key);
    const delisted = state.status === "nincs fent" && (state.note || "").startsWith("Levéve");
    // a háttérellenőrzés szerint itt elkelt vagy eltűnt (csak jelzés: a döntés a tiéd)
    const flagged = state.status === POSTED && !item.sold && CHECKED[state.check] ? CHECKED[state.check] : "";
    const label = flagged ? flagged : state.status === POSTED ? "Fent van" : state.status === FAILED ? "Nem sikerült"
      : delisted ? "Levéve" : chosen ? "Még nincs fent" : "";
    const when = state.at && state.status ? " (" + new Date(state.at * 1000).toLocaleDateString("hu-HU") + ")" : "";
    const note = state.note && state.status !== "nincs fent" ? state.note : "";
    const drift = driftText(item, (item.drift || []).find((d) => d.platform === p.key));
    const checked = checkNote(item, state);
    const others = Object.keys(item.platforms).some((k) => k !== p.key && siteState(item, k) === POSTED);
    return `<div class="platform">
      <label class="check pname"><input type="checkbox" data-target="${esc(p.key)}"${chosen ? " checked" : ""}> ${esc(p.name)}</label>
      <span class="pstatus${flagged ? " bad" : state.status === POSTED ? " on" : state.status === FAILED ? " bad" : ""}">${esc(label + (label ? when : ""))}</span>
      ${flagged ? `<button type="button" class="small" data-action="soldhere" data-platform="${esc(p.key)}" title="Megjelöli eladottnak; a többi oldalról a jóváhagyásod után leveszi">${others ? "Eladva itt, vedd le a többiről" : "Eladva itt"}</button>` : ""}
      ${flagged ? `<button type="button" class="small quiet" data-action="dismiss" data-platform="${esc(p.key)}" title="A jelzés téves: a hirdetés fent van. Ennél a hirdetésnél ugyanezt a jelzést többé nem hozom fel.">Nem igaz, fent van</button>` : ""}
      ${state.status === POSTED && safeUrl(state.url) ? `<a class="button small" href="${esc(state.url)}" target="_blank" rel="noopener noreferrer">Megnyitás</a>` : ""}
      ${chosen && !item.sold && state.status !== POSTED && browserOn ? `<button type="button" class="small" data-action="post" data-platform="${esc(p.key)}">${state.status === FAILED ? "Újra" : "Feltöltés"}</button>` : ""}
      ${state.status === POSTED && browserOn ? `<button type="button" class="small" data-action="delist" data-platform="${esc(p.key)}" title="Jarvis leveszi a hirdetést erről az oldalról">Levétel</button>` : ""}
      ${chosen ? `<button type="button" class="small quiet" data-action="toggle" data-platform="${esc(p.key)}" title="Csak a nyilvántartást írja át">${state.status === POSTED ? "Már levettem" : "Fent van"}</button>` : ""}
      ${state.dossier ? `<button type="button" class="small quiet" data-action="dossier" data-platform="${esc(p.key)}" title="Képernyőkép, oldalnézet és a lépések abból a pillanatból, amikor elakadt">Hibacsomag</button>` : ""}
      ${note ? `<span class="pnote">${esc(note)}</span>` : ""}
      ${checked ? `<span class="pnote${flagged ? " bad" : ""}">${esc(checked)}</span>` : ""}
      ${drift ? `<span class="pnote">${esc(drift)}</span>` : ""}
    </div>`;
  }

  function renderDetail(item) {
    const busy = uploading.get(item.id) || item.busy;
    $("w-detail").innerHTML = `
      <header>
        <button type="button" class="quiet small" data-action="back">← Hirdetéseim</button>
        <h2>${esc(item.name)}</h2>
        <span class="chip ${STATUS_CLASS[item.status] || ""}">${esc(item.status)}</span>
        ${online(item) ? `<span class="muted">${esc(online(item))}</span>` : ""}
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
        ${photoNote(item)}
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
          ${messagesOn ? field("min_price", "Alkunál legalább (Ft)", item.min_price ?? "",
            `inputmode="numeric" placeholder="${esc(price(item.price) || "az ár")}"`) : ""}
          ${field("bought_price", "Vételár (Ft)", item.bought_price ?? "", 'inputmode="numeric" placeholder="ha vetted"')}
          ${item.sold ? field("sold_price", "Eladási ár (Ft)", item.sold_price ?? "", 'inputmode="numeric"') : ""}
          ${item.sold && item.bought_price != null && item.sold_price != null
            ? `<div class="field"><label>Haszon</label><b>${esc(signed(item.sold_price - (item.sell_shipping || 0) - (item.sell_fee || 0)
                - item.bought_price - (item.bought_shipping || 0) - (item.bought_extra || 0)))}</b></div>` : ""}
          ${item.bought_price != null ? `<div class="field"><label>Vétel részletei</label>
            <button type="button" class="small" data-action="ledger">Vételeim fül</button></div>` : ""}
          ${conditionField(item.condition)}
          ${field("brand", "Márka", item.brand)}
          ${field("size", "Méret", item.size)}
          ${field("color", "Szín", item.color)}
          ${field("category", "Kategória", item.category)}
          ${field("location", "Átvétel helye", item.location, `placeholder="${esc(settings.city || "az Alapadatokból")}"`)}
          ${field("shipping", "Szállítás", item.shipping, `placeholder="${esc(settings.shipping || "az Alapadatokból")}"`)}
        </div>
        ${(item.drift || []).length && driftNotes.get(item.id)
          ? `<p class="drift-note" role="status">${esc(driftNotes.get(item.id))}</p>` : ""}
        <p class="muted">Amit itt átírsz, azt Jarvis már nem írja felül.</p>
      </section>
      ${researchBlock(item)}
      <div class="danger">
        <button type="button" data-action="sold">${item.sold ? "Mégsem kelt el" : "Eladva"}</button>
        <button type="button" class="delete" data-action="delete">Törlés</button>
      </div>`;
  }

  // --- vevői üzenetek ---------------------------------------------------------------------------

  function renderMessages() {
    const conv = convOpen && convs.get(convOpen);
    $("w-msgs").innerHTML = conv ? conversation(conv) : conversationList();
  }

  function conversationList() {
    const list = [...convs.values()].sort((a, b) => (isNew(b) - isNew(a)) || (b.updated - a.updated));
    return `<div class="row msgs-head"><h2 class="section-title">Vevői üzenetek</h2>
        ${msgBrowser ? '<button type="button" class="small" data-msg="check">Frissítés most</button>' : ""}</div>
      <p class="muted">Jarvis időnként elolvassa az új üzeneteidet (csak olvas, semmit nem küld). Válaszvázlatot
        kérésre ír; elküldeni csak te tudod, a Küldés gombbal, és a küldés előtt még egyszer rákérdez.</p>
      ${list.length ? `<div class="convs">${list.map(conversationRow).join("")}</div>`
        : '<p class="muted">Még nincs elolvasott beszélgetés.</p>'}`;
  }

  function conversationRow(conv) {
    const fresh = conv.new ? `${conv.new} új` : conv.unread ? "olvasatlan" : "";
    return `<button type="button" class="conv${isNew(conv) ? " fresh" : ""}" data-conv="${esc(conv.id)}">
      <span class="who">${esc(conv.partner || "Vevő")}</span>
      ${fresh ? `<span class="chip kerdes">${esc(fresh)}</span>` : ""}
      <span class="where">${esc(conv.site)}${conv.item_name ? " · " + esc(conv.item_name) : ""}</span>
      <span class="snippet">${esc(conv.snippet)}</span></button>`;
  }

  function conversation(conv) {
    const bubbles = (conv.messages || []).map((m) =>
      `<div class="bubble ${m.from === "me" ? "me" : "them"}"><p>${esc(m.text)}</p>` +
      (m.time ? `<span class="when">${esc(m.time)}</span>` : "") + "</div>").join("");
    const fromBuyer = (conv.messages || []).some((m) => m.from !== "me");
    return `<header>
        <button type="button" class="quiet small" data-msg="back">← Üzenetek</button>
        <h2>${esc(conv.partner || "Vevő")}</h2>
        <span class="chip">${esc(conv.site)}</span>
        ${conv.item ? `<button type="button" class="small" data-msg="item">Tárgy: ${esc(conv.item_name)}</button>`
          : '<span class="muted">Nem tudom, melyik tárgyadról szól.</span>'}
        ${safeUrl(conv.url) ? `<a class="button small" href="${esc(conv.url)}" target="_blank" rel="noopener noreferrer">Megnyitás</a>` : ""}
      </header>
      <div class="bubbles">${bubbles || '<p class="muted">Ezt a beszélgetést még nem olvastam el.</p>'}</div>
      <section class="reply" aria-labelledby="h-reply">
        <h3 id="h-reply">Válasz</h3>
        ${conv.minimum ? `<p class="muted">Alkunál legalább ${esc(price(conv.minimum))}: ennél kevesebbet a vázlat nem fogad el
          (a tárgy lapján átírhatod).</p>` : ""}
        ${conv.draft_note ? `<p class="pnote">${esc(conv.draft_note)}</p>` : ""}
        <textarea id="m-draft" data-keep="draft-${esc(conv.id)}-${conv.draft_at || 0}" rows="4" aria-label="Válasz a vevőnek"
          placeholder="Írd ide a választ, vagy kérj vázlatot">${esc(conv.draft)}</textarea>
        <div class="row">
          <button type="button" data-msg="draft"${fromBuyer ? "" : " disabled"}>Válaszvázlat</button>
          <button type="button" class="primary" data-msg="send"${msgBrowser ? "" : " disabled"}>Küldés</button>
        </div>
        <p class="muted">A Küldésre Jarvis beírja a választ a beszélgetésbe, és a küldés előtt még egyszer rákérdez,
          pontosan ezzel a szöveggel.</p>
      </section>`;
  }

  function openConversation(id) {
    convOpen = id;
    const conv = convs.get(id);
    if (conv && conv.new) {   // megnézted: nincs benne új
      J.postJson("/api/work/messages/seen", { id }).then((result) => {
        if (result.ok) { convs.set(id, result.conversation); render(); }
      });
    }
    render();
    area.scrollTop = 0;
  }

  async function messageAction(name) {
    const conv = convs.get(convOpen);
    if (name === "back") { convOpen = null; render(); return; }
    if (name === "check") {
      const result = await J.postJson("/api/work/messages/check", {});
      J.toast(result.ok ? "Megnézem az üzeneteidet." : result.error || "Nem sikerült elindítani.");
      return;
    }
    if (!conv) return;
    if (name === "item") {
      filter = "all";
      openId = conv.item;
      convOpen = null;
      render();
      area.scrollTop = 0;
    } else if (name === "draft") {
      const result = await J.postJson("/api/work/messages/draft", { id: conv.id });
      J.toast(result.ok ? "Jarvis vázlatot ír…" : result.error || "Nem sikerült elindítani.");
    } else if (name === "send") {
      const text = $("m-draft").value.trim();
      if (!text) { J.toast("Írd be a választ, vagy kérj vázlatot."); return; }
      const result = await J.postJson("/api/work/messages/send", { id: conv.id, text });
      J.toast(result.ok ? "Jarvis beírja, és a küldés előtt rákérdez." : result.error || "Nem sikerült elindítani.");
    }
  }

  // --- műveletek -------------------------------------------------------------------------

  async function toggle(item, key) {
    const status = siteState(item, key) === POSTED ? "nincs fent" : POSTED;
    remember(await J.postJson("/api/work/mark", { id: item.id, platform: key, status }), "Nem sikerült.");
  }

  // Eladva: hol kelt el, és vegye-e le Jarvis a többi oldalról (ahol nem ott kelt el). `where`: előre
  // kiválasztott oldal (pl. ahol a háttérellenőrzés szerint elkelt); a döntés itt is a tiéd.
  function askSold(item, posted, where = "", delist = true) {
    return new Promise((resolve) => {
      const dialog = $("sold-dialog");
      const chosen = posted.includes(where) ? where : "";
      $("sold-text").textContent = `${item.name}: hol kelt el?`;
      $("sold-where").innerHTML = [...posted.map((k) => [k, siteName(k)]), ["", "Máshol vagy személyesen"]]
        .map(([key, name]) => `<label class="check"><input type="radio" name="sold-where" value="${esc(key)}"` +
          `${key === chosen ? " checked" : ""}> ${esc(name)}</label>`).join("");
      $("sold-delist").checked = delist;
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

  async function sold(item, where = "", delist = true) {
    if (item.sold) {   // mégsem kelt el
      remember(await J.postJson("/api/work/mark", { id: item.id, sold: false }), "Nem sikerült.");
      return;
    }
    const posted = Object.keys(item.platforms).filter((k) => siteState(item, k) === POSTED);
    if (!posted.length) {
      remember(await J.postJson("/api/work/mark", { id: item.id, sold: true }), "Nem sikerült.");
      return;
    }
    const answer = await askSold(item, posted, where, delist);
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
    composeTouched = true;
    $("w-compose").classList.remove("folded");
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

  // Egy értesítés gombja (app.js: az Értesítések ablak, a laptop push-értesítése, az Android-alkalmazás). Az
  // „Eladva itt, vedd le a többiről” a hirdetés Eladva ablakát nyitja meg, előre kiválasztva: a levétel csak a
  // Rendben gombra indul. A „Bejelentkezés a böngészőmben” megnyitja az oldalt Jarvis böngészőjében.
  window.addEventListener("jarvis-notice-action", async (event) => {
    const wanted = event.detail || {};
    await loaded;
    if (wanted.action === "deals") { setJob("deals"); return; }   // pl. a szokásos ár jóváhagyása
    if (wanted.action === "offer") { setJob("deals"); await makeOffer(wanted.deal, ""); return; }   // a javasolt összeggel
    if (wanted.action === "login") {
      if (!platforms.some((p) => p.key === wanted.platform)) return;
      if (await action("login", null, { platform: wanted.platform })) {
        J.toast("Megnyitom a Jarvis böngészőjében. Jelentkezz be, és pipáld be, hogy maradj bejelentkezve.");
      }
      return;
    }
    if (wanted.action !== "sold" || $("sold-dialog").open) return;
    const item = items.get(wanted.item);
    if (!item) { J.toast("Ez a hirdetés már nincs meg."); return; }
    openId = item.id;
    render();
    area.scrollTop = 0;
    if (item.sold) { J.toast("Ezt már eladottnak jelölted."); return; }
    if (!Object.values(item.platforms).some((state) => state.status === POSTED)) {
      J.toast("A nyilvántartás szerint már sehol nincs fent; ha elkelt, az Eladva gombbal jelöld meg.");
      return;
    }
    sold(item, String(wanted.where || ""), wanted.delist !== false);
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
    convOpen = null;
    render();
  });

  $("w-msgs").addEventListener("click", (event) => {
    const row = event.target.closest("[data-conv]");
    if (row) { openConversation(row.dataset.conv); return; }
    const button = event.target.closest("button[data-msg]");
    if (button) messageAction(button.dataset.msg);
  });
  // a kézzel átírt vázlat megmarad (a telefonon is ez látszik)
  $("w-msgs").addEventListener("change", async (event) => {
    if (event.target.id !== "m-draft" || !convOpen) return;
    const result = await J.postJson("/api/work/messages/save", { id: convOpen, draft: event.target.value });
    if (result.ok) convs.set(result.conversation.id, result.conversation);
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
    if (name === "back") { driftNotes.delete(item.id); openId = null; render(); }
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
    else if (name === "ledger") { ledgerOpen = item.id; setJob("ledger"); }
    else if (name === "soldhere") sold(item, button.dataset.platform);
    else if (name === "dismiss") {
      const result = await J.postJson("/api/work/dismiss", { id: item.id, platform: button.dataset.platform });
      if (remember(result, "Nem sikerült.")) J.toast("Rendben, fent van. Ezt a jelzést többé nem hozom fel.");
    }
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

  J.on("work_item", (event) => { items.set(event.item.id, event.item); scheduleRender(event.item.id); refreshLedger(); });
  J.on("work_removed", (event) => {
    items.delete(event.id);
    refreshLedger();
    if (openId === event.id) openId = null;
    scheduleRender(event.id);
  });
  J.on("work_message", (event) => {
    const conv = event.conversation;
    convs.set(conv.id, conv);
    if (filter === "msgs" && convOpen === conv.id && conv.new) openConversation(conv.id);
    else if (filter === "msgs") render();
    else renderFilters();
  });
  // Új olcsó hirdetés (a háttérben, vagy a Keresés most után): a nyitott lista frissül.
  window.addEventListener("jarvis-notice", (event) => {
    if (event.detail && event.detail.kind === "deal") loadDeals();
  });
  J.onHello((event) => { if (event.work) load(); });
})();
