"use strict";
// Munka → Adás: kipipálod, hova kerüljön, hozzáadod a fotókat, leírod, mit adsz el, és Jarvis
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
  const STATUS_CLASS = { "vázlat": "vazlat", "kérdés": "kerdes", "árdöntés": "kerdes", "átnézésre vár": "kerdes",
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
      todosOpen = data.todos_open || 0;
      ledgerTab();
      if (!messagesOn && filter === "msgs") filter = "all";
      renderCompose();
      renderSetup();
      render();
      markLoaded();
      if (messagesOn) loadMessages();
    } catch { /* a Jarvis még indul: a következő "hello" újra betölti */ }
  }

  // Az Elemzés fül felirata: mellette az elvégezetlen teendők száma (pl. egy csomag feladása).
  let todosOpen = 0;
  function ledgerTab() {
    $("job-ledger").innerHTML = "Elemzés" + (todosOpen ? `<span class="count">${todosOpen}</span>` : "");
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

  // Átírtad-e a mezőt a megjelenése óta (a böngésző az egysoros mezőből a sortörést kiveszi: az még nem átírás).
  const typedValue = (el) => el.value !== (el.tagName === "TEXTAREA" ? el.defaultValue : el.defaultValue.replace(/[\r\n]/g, ""));

  const update = async (id, fields) => {
    const result = await J.postJson("/api/work/update", { id, fields });
    if (result.ok) {
      driftNotes.set(id, result.drift_note || "");
      settle(id, fields);
    }
    if (!remember(result, "Nem sikerült menteni.")) load();
  };

  // A tárgy lapjának egy mezője, amikor kilépsz belőle (change). Amíg a mentés tart, a mezőben a beírt érték marad
  // (az újrarajzolás sem veszi el); ha sikerült, már a Jarvisnál tárolt érték látszik (pl. „15 000” helyett 15000).
  // Ha nem sikerült, a beírt érték bent marad, hogy javíthasd.
  const saving = new Map();               // data-keep -> a mentés alatt álló érték
  async function saveField(id, el) {
    const key = el.dataset.keep;
    const value = el.value;
    if (key) saving.set(key, value);
    try {
      await update(id, { [el.dataset.field]: value });
    } finally {
      if (key && saving.get(key) === value) saving.delete(key);
    }
  }
  function settle(id, fields) {
    const box = $("w-detail");
    if (box.dataset.item !== id) return;
    for (const [name, value] of Object.entries(fields)) {
      const el = box.querySelector(`[data-field="${CSS.escape(name)}"][data-keep]`);
      if (el && el.value === String(value)) el.defaultValue = el.value;
    }
  }

  async function action(name, id, extra = {}) {
    const result = await J.postJson("/api/work/action", { action: name, ids: id ? [id] : [], ...extra });
    if (!result.ok) J.toast(result.error || "Nem sikerült elindítani.");
    return result.ok;
  }

  async function upload(id, files, alt = false) {
    files = files.filter((f) => /^image\//.test(f.type) || /\.(jpe?g|png|webp|heic|heif)$/i.test(f.name));
    for (let i = 0; i < files.length; i++) {
      uploading.set(id, `${alt ? "A 2. képsor fotói" : "Fotók feltöltése"} ${i + 1}/${files.length}…`);
      render();
      try {
        const response = await fetch(`/api/work/items/${id}/photo${alt ? "?set=alt" : ""}`, {
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

  // Egyszerű: fotók és egy-két mondat. Pro: elég a fotó (telefonon a kamerával is), az adatokat és az árat Jarvis
  // olvassa ki, a leírás csak megjegyzés (nem kötelező), és feltöltés előtt mindent megmutat (Nézd át).
  let composeMode = recall("jarvis-compose-mode") === "pro" ? "pro" : "simple";
  const touchPhone = /JarvisAndroid/.test(navigator.userAgent) || window.matchMedia("(pointer: coarse)").matches;
  function applyMode() {
    const pro = composeMode === "pro";
    $("w-mode-simple").setAttribute("aria-selected", String(!pro));
    $("w-mode-pro").setAttribute("aria-selected", String(pro));
    $("w-pro-help").hidden = !pro;
    $("w-camera-row").hidden = !(pro && touchPhone);
    $("w-research-row").hidden = pro;            // Pro: az árat a hasonlókból úgyis megnézi
    $("w-prompt-label").textContent = pro ? "Megjegyzés (nem kötelező)" : "Mit adsz el?";
    $("w-prompt").placeholder = pro ? "Pl. egy kis folt van az ujján; dobozzal együtt adom"
      : "Pl. Zara L-es fekete póló, egyszer hordott, 500 Ft";
  }
  for (const [id, mode] of [["w-mode-simple", "simple"], ["w-mode-pro", "pro"]]) {
    $(id).addEventListener("click", () => { composeMode = mode; store("jarvis-compose-mode", mode); applyMode(); });
  }
  $("w-camera-btn").addEventListener("click", () => $("w-camera").click());
  $("w-camera").addEventListener("change", () => {
    const files = [...$("w-camera").files];
    $("w-camera").value = "";
    if (files.length) addComposePhotos(files);
  });

  function renderCompose() {
    let saved = [];
    try { saved = JSON.parse(recall("jarvis-sites") || "[]"); } catch { saved = []; }
    const current = $("w-sites").children.length ? selectedSites() : saved;
    $("w-sites").innerHTML = platforms.map((p) =>
      `<label class="check"><input type="checkbox" value="${esc(p.key)}"${current.includes(p.key) ? " checked" : ""}>` +
      ` ${esc(p.name)}</label>`).join("");
    $("w-research").checked = recall("jarvis-research") === "1";
    $("w-research").disabled = !researchOn;
    applyMode();
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
    const pro = composeMode === "pro";
    const say = (text) => { $("w-compose-msg").textContent = text; };
    if (!targets.length) return say("Pipáld be, hova töltsem fel.");
    if (!composePhotos.length) return say("Adj hozzá legalább egy fotót.");
    if (!prompt && !pro) return say("Írd le röviden, mit adsz el.");
    if (!browserOn) return say("A feltöltéshez Jarvis saját böngészője kell (Playwright).");
    if (!(await acknowledge(targets))) return;
    $("w-go").disabled = true;
    say("Küldöm a fotókat…");
    try {
      const created = await J.postJson("/api/work/new",
        { targets, prompt, pro, research_wanted: !pro && $("w-research").checked });
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
        J.toast(pro ? "Átnézem a fotókat. Ha valami nem látszik, rákérdezek; feltöltés előtt mindent megmutatok."
          : "Jarvis dolgozik rajta. Ha kérdése van, a hirdetésnél látod.");
      }
    } finally {
      $("w-go").disabled = false;
    }
  }

  // --- Pro feltöltés: Nézd át (a fotókról kiolvasott adatok és az ár, átírhatóan) --------------------------------

  let reviewOpen = null;
  const GENDERS = ["", "női", "férfi", "gyerek", "uniszex"];
  function openReview(id) {
    const item = items.get(id);
    if (!item) { J.toast("Ez a hirdetés már nincs meg."); return; }
    reviewOpen = id;
    const text = (name, label, extra = "") => `<div class="field"><label for="r-${name}">${label}</label>` +
      `<input id="r-${name}" data-rfield="${name}" value="${esc(item[name] ?? "")}" ${extra}></div>`;
    const choose = (name, label, options) => {
      const value = item[name] || "";
      const all = options.includes(value) ? options : [...options, value];
      return `<div class="field"><label for="r-${name}">${label}</label><select id="r-${name}" data-rfield="${name}">` +
        all.map((o) => `<option value="${esc(o)}"${o === value ? " selected" : ""}>${esc(o || "–")}</option>`).join("") +
        "</select></div>";
    };
    const clothing = ["ruha", "cipő", "kiegészítő"].includes(item.kind);
    $("review-photos").innerHTML = item.photos.slice(0, 8)
      .map((n) => `<img src="${photoUrl(item.id, esc(n), true)}" alt="">`).join("");
    const basis = (item.pending || {}).basis;
    $("review-basis").textContent = basis ? `Az ár: ${basis}.` : "";
    $("review-fields").innerHTML = [
      `<div class="field wide"><label for="r-title">Cím</label><input id="r-title" data-rfield="title" value="${esc(item.title)}"></div>`,
      text("price", "Ár (Ft)", 'type="number" inputmode="numeric" min="1"'),
      choose("condition", "Állapot", ["", ...conditions]),
      text("brand", "Márka"), text("size", "Méret"), text("color", "Szín"),
      clothing ? choose("gender", "Kinek", GENDERS) : "",
      text("material", "Anyag"),
      `<div class="field wide"><label for="r-category">Kategória</label><input id="r-category" data-rfield="category" value="${esc(item.category)}"></div>`,
      `<div class="field wide"><label for="r-description">Leírás</label><textarea id="r-description" data-rfield="description" rows="4">${esc(item.description)}</textarea></div>`,
    ].join("");
    $("review-msg").textContent = "";
    if (!$("review-dialog").open) $("review-dialog").showModal();
  }
  // Csak amit az ablakban átírtál (a megnyitáskori értékhez képest): ami közben máshol (pl. a telefonon) változott,
  // azt nem írja vissza a régire.
  const changedHere = (el) => (el.tagName === "SELECT"
    ? el.value !== ([...el.options].find((o) => o.defaultSelected) || el.options[0] || { value: "" }).value
    : typedValue(el));
  async function saveReview() {
    const item = items.get(reviewOpen);
    if (!item) return false;
    const fields = {};
    for (const el of $("review-fields").querySelectorAll("[data-rfield]")) {
      if (changedHere(el)) fields[el.dataset.rfield] = el.value;
    }
    if (!Object.keys(fields).length) return true;
    const result = await J.postJson("/api/work/update", { id: item.id, fields });
    if (!result.ok) { $("review-msg").textContent = result.error || "Nem sikerült menteni."; return false; }
    items.set(result.item.id, result.item);
    render();
    return true;
  }
  $("review-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!(await saveReview())) return;
    const item = items.get(reviewOpen);
    if (!(await acknowledge(item.targets))) return;
    const result = await J.postJson("/api/work/action", { action: "approve", ids: [item.id] });
    if (!result.ok) { $("review-msg").textContent = result.error || "Nem sikerült elindítani."; return; }
    $("review-dialog").close();
    J.toast("Mehet: feltöltöm. A közzététel előtt még egyszer rákérdezek.");
  });
  $("review-save").addEventListener("click", async () => {
    if (await saveReview()) { $("review-dialog").close(); J.toast("Mentve. Ha jó, az Átnézem gombbal mehet a feltöltés."); }
  });
  $("review-close").addEventListener("click", () => $("review-dialog").close());

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

  // Újrarajzoláskor ne vesszen el, amit épp gépelsz (pl. egy válasz a kártyán). Csak az marad meg, amit a legutóbbi
  // rajzolás óta átírtál (és még nincs elmentve); a többi mezőben a friss adat látszik (pl. amit közben Jarvis írt át,
  // vagy a telefonon írtál át). A kulcsok a tárgyhoz kötöttek (data-keep="f-<tárgy>-title"), így egy tárgy mezőjébe
  // sosem kerülhet egy másikéból.
  function keepTyping(box, draw) {
    const typed = new Map([...box.querySelectorAll("[data-keep]")].filter(typedValue)
      .map((el) => [el.dataset.keep, el.value]));
    const active = document.activeElement;
    const focus = active && box.contains(active) && active.dataset.keep
      ? { key: active.dataset.keep, start: active.selectionStart, end: active.selectionEnd } : null;
    const scroll = area.scrollTop;
    draw();
    for (const el of box.querySelectorAll("[data-keep]")) {
      if (typed.has(el.dataset.keep)) el.value = typed.get(el.dataset.keep);
    }
    if (focus) {
      const el = box.querySelector(`[data-keep="${CSS.escape(focus.key)}"]`);
      if (el) {
        el.focus();
        try { el.setSelectionRange(focus.start, focus.end); } catch { /* select */ }
      }
    }
    area.scrollTop = scroll;
  }

  // Egy tárgy lapja mindig a tárgy saját adataival nyílik meg. Amíg ugyanaz a tárgy látszik, az újrarajzolás megtartja
  // a félbehagyott gépelést; ha a lap másik tárgyra vált vagy bezárul, előbb elmenti (a saját tárgyába), amit a
  // mezőibe beírtál, de még nem mentődött (pl. gépelés közben egy értesítésre bököttél), aztán kiürül. A lap tudja,
  // kié (data-item): a mezői, gombjai mindig ahhoz a tárgyhoz tartoznak, akkor is, ha közben máshová léptél.
  const detailItem = () => ($("w-detail").hidden ? null : $("w-detail").dataset.item || null);
  function showDetail(item) {
    const box = $("w-detail");
    const same = !box.hidden && box.dataset.item === item.id;
    if (!same) leaveDetail();
    box.hidden = false;
    if (same) keepTyping(box, () => renderDetail(item));
    else renderDetail(item);
  }
  function leaveDetail() {
    const box = $("w-detail");
    const id = box.dataset.item;
    box.hidden = true;
    if (!id) return;
    const unsaved = {};
    for (const el of box.querySelectorAll("[data-field][data-keep]")) {
      if (typedValue(el) && saving.get(el.dataset.keep) !== el.value) unsaved[el.dataset.field] = el.value;
    }
    box.innerHTML = "";
    delete box.dataset.item;
    if (Object.keys(unsaved).length && items.has(id)) update(id, unsaved);
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
  // a megvett találat (a tárgy azonosítójával): Megvetted ✓, és a Visszamondva gomb (cancelBought)
  const boughtOf = (dealId) => (deals.deals.find((d) => d.id === dealId && d.bought) || {}).bought || "";
  const cancelButton = (itemId, name) => `<small class="muted">Megvetted ✓</small>
    <button type="button" class="small quiet" data-cancel-bought="${esc(itemId)}" data-name="${esc(name)}">Visszamondva</button>`;
  let buying = null;      // a találat, aminél épp a vételárat írod (Megvettem)
  async function loadDeals() {
    try {
      const response = await fetch("/api/work/deals?t=" + encodeURIComponent(token));
      if (response.ok) deals = await response.json();
    } catch { /* a következő megnyitáskor újra */ }
    renderHome();
    if (job === "deals") render();
  }
  async function dealAction(body) {
    const result = await J.postJson("/api/work/deals", body);
    if (!result.ok) { J.toast(result.error || "Nem sikerült."); return false; }
    if (result.searches) deals = { ...deals, ...result };
    renderHome();
    render();
    return result;
  }

  // --- a Beszélgetés eleje: csempék (a kereskedés indítása, egy új tárgy, a találatok); az első üzenet után eltűnnek ---
  function renderHome() {
    const box = $("home-tiles");
    const running = !!deals.running;
    const fresh = (deals.deals || []).filter((d) => !d.bought).length;
    box.innerHTML = `
      <button type="button" class="hometile${running ? " on" : ""}" id="tile-trade" ${deals.browser || deals.instant ? "" : "disabled"}>
        <span class="hometile-title">${running ? "A kereskedés fut" : "Kereskedés indítása"}</span>
        <span class="hometile-sub">${running ? `${deals.started ? `${esc(hhmmOf(deals.started))} óta · ` : ""}${deals.turbo ? "Turbó · " : ""}bökj rá a leállításhoz`
          : "Keresem a megérő hirdetéseket, és szólok"}</span></button>
      <button type="button" class="hometile" id="tile-new">
        <span class="hometile-title">Új tárgy közzététele</span>
        <span class="hometile-sub">Fotók, és feltöltöm (Pro: elég a fotó)</span></button>
      ${fresh ? `<button type="button" class="hometile" id="tile-hits">
        <span class="hometile-title">Találatok (${fresh})</span>
        <span class="hometile-sub">A legutóbbi megérő hirdetések</span></button>` : ""}`;
    box.hidden = false;
    $("empty-hint").textContent = "Vagy kérdezz bármit lent.";
    $("tile-trade").addEventListener("click", async () => {
      const start = !deals.running;
      if (await dealAction({ action: start ? "start" : "stop" }))
        J.toast(start ? "Elindítottam: keresek, és ha valami megéri, szólok." : "Leállítottam.");
    });
    $("tile-new").addEventListener("click", () => {
      openId = null;
      J.setMode("work");
      setJob("sales");
      composeTouched = true;
      $("w-compose").classList.remove("folded");
      $("w-compose").scrollIntoView({ block: "start" });
    });
    if ($("tile-hits")) $("tile-hits").addEventListener("click", () => { J.setMode("work"); setJob("deals"); });
  }
  function setJob(name) {
    job = name;
    $("job-sales").setAttribute("aria-selected", String(name === "sales"));
    $("job-deals").setAttribute("aria-selected", String(name === "deals"));
    $("job-ledger").setAttribute("aria-selected", String(name === "ledger"));
    if (name === "deals") loadDeals();       // mindig frissen (közben a telefonról is változhatott)
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
      ? `<span class="row"><input id="d-ref" data-keep="ref-${esc(s.id)}" type="number" min="1" value="${esc(s.reference || (s.proposal && s.proposal.price) || "")}"
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
  // Az Automata kereskedés blokkjai összecsukhatók: alapból csak az Indítás és a legutóbbi találatok látszanak; amit
  // kinyitsz, nyitva marad (a lista frissülésekor és a következő megnyitáskor is).
  const openSections = new Set((recall("jarvis-deal-open") || "").split(",").filter(Boolean));
  const section = (key, title, body) => `<details class="fold" data-fold="${key}"${openSections.has(key) ? " open" : ""}>
      <summary>${title}</summary><div class="fold-body">${body}</div></details>`;
  // másodperc → „45 mp”, „2,5 perc” (az azonnali figyelő ütemének és késésének kiírásához)
  const secondsText = (sec) => sec < 90 ? `${Math.round(sec)} mp` : `${(sec / 60).toFixed(1).replace(".", ",").replace(",0", "")} perc`;
  function instantBox() {
    const I = deals.instant;
    if (!I) return "";
    const siteLine = (I.sites || []).map((s) => `${esc(s.name)}: ${s.blocked_until ? `tilt (${esc(s.reason)}), ${esc(hhmmOf(s.blocked_until))}-ig szünetel`
      : s.browser_only ? "most nem olvasható böngésző nélkül (a böngésző nézi)" : `kb. ${s.per_hour} kérés óránként (keret: ${I.hourly})${s.slow > 1.05 ? ", lassít (lassan válaszol)" : ""}${s.rhythm ? `, ${esc(s.rhythm)}` : ""}`
      + (s.every_s ? `, egy figyelő két letöltése között legalább ${esc(secondsText(s.every_s))}` : "")
      + (s.delay ? `, észlelés a feltöltés után: kb. ${esc(secondsText(s.delay.median))} (${esc(s.delay.n)} új alapján)` : "")}`).join(" · ");
    const alerts = (I.alerts || []).map((a) => {
      const sites = (a.sites || []).map((k) => (I.sites.find((s) => s.key === k) || { name: k }).name).join(", ");
      const range = a.min_price || a.max_price ? ` · ${a.min_price ? price(a.min_price) : "0"}–${a.max_price ? price(a.max_price) : "bármennyi"}` : "";
      const last = Math.max(0, ...Object.values(a.last || {}));
      return `<li><span><b>${esc(a.name)}</b>
          <small class="muted">${esc(sites)}${esc(range)}${(a.exclude || []).length ? ` · kivéve: ${esc(a.exclude.join(", "))}` : ""} · ${a.every} percenként</small>
          ${(a.covered || []).length ? '<small class="muted">A közös friss listából jönnek a találatai: külön csak negyedóránként nézem.</small>' : ""}
          ${Math.max(0, ...Object.values(a.lead || {})) > 0 ? `<small class="muted">Az elején álló ${esc(Math.max(...Object.values(a.lead)))} kiemelt hirdetést nem töltöm le.</small>` : ""}
          <small class="muted">${a.off ? "szünetel" : last ? `utoljára ${esc(ago(last))}` : "még nem nézte"}${a.found ? ` · eddig ${a.found} új` : ""}${a.found_at ? ` (legutóbb ${esc(ago(a.found_at))})` : ""}</small>
          ${Object.values(a.errors || {}).map((e) => `<small class="warn">${esc(e)}</small>`).join("")}
          ${a.url ? `<small><a href="${esc(a.url)}" target="_blank" rel="noopener noreferrer">az oldal</a></small>` : ""}</span>
        ${a.search ? `<small class="muted">${a.origin === "watches" ? "a Figyelt oldalakból" : "a Keresésekből"}</small>` : `<span class="row"><button type="button" class="small quiet" data-ipause="${esc(a.id)}" data-off="${a.off ? "" : "1"}">${a.off ? "Folytatás" : "Szünet"}</button>
          <button type="button" class="small quiet" data-iremove="${esc(a.id)}">Törlés</button></span>`}</li>`;
    }).join("");
    const hits = (I.hits || []).slice(0, 25).map((h) => `<li><span><a href="${esc(h.url)}" target="_blank" rel="noopener noreferrer">${esc(h.title || h.text)}</a>
        <small class="muted">${esc(h.site)} · <b>${esc(price(h.price))}</b>${h.drop ? ` (leárazta: ${esc(price(h.drop))})` : ""} · ${esc(ago(h.at))} · ${esc(h.alert_name)}</small>
        ${h.reference ? `<small class="${h.cheap ? "" : "muted"}">${h.cheap ? "<b>Megéri:</b> " : ""}szokásos ár kb. ${esc(price(h.reference))}${h.profit > 0 ? ` · kb. ${esc(price(h.profit))} becsült haszon` : ""}${h.cheap && h.max_buy !== undefined && h.max_buy !== null ? ` · legfeljebb ${esc(price(h.max_buy))}-ért vedd meg` : ""}${h.reliability && h.reliability.level !== "megadott" ? ` · a becslés megbízhatósága ${esc(h.reliability.level)}` : ""}</small>` : ""}
        ${h.withdrawn ? `<small class="warn">${esc(h.withdrawn)}</small>`
          : h.model_no ? `<small class="warn">Utólag mégsem jó: ${esc(h.model_no)}</small>`
          : h.verified ? '<small class="muted">Ellenőrizve: az adatlapja megnyílt, a leírása és az állapota alapján számoltam, a modell is átnézte</small>'
          : h.unchecked ? `<small class="warn">A modell még nem nézte meg${h.model_pending ? " (amint lehet, megnézi)" : ""}</small>` : ""}
        ${(h.warnings || []).filter((w) => !String(w).startsWith("utólag:")).map((w) => `<small class="warn">Figyelj: ${esc(w)}</small>`).join("")}</span>
        ${h.deal ? `<span class="row">${boughtOf(h.deal) ? cancelButton(boughtOf(h.deal), h.title || h.text)
          : `<button type="button" class="small" data-deal-buy="${esc(h.deal)}">Megvettem</button>`}</span>` : ""}</li>`).join("");
    const presets = (I.presets || []).map((p) => `<button type="button" class="small" data-ipreset="${esc(p.url)}">${esc(p.name)}</button>`).join("");
    return `${I.enabled && siteLine ? `<p><small>${siteLine}</small></p>` : ""}
      ${I.enabled && I.waiting ? `<p><small>${I.waiting} friss hirdetés vár a modellre (amíg a Jarvist használod).</small></p>` : ""}
      <ul class="dealist">${alerts || '<li class="muted">Még nincs azonnali figyelő.</li>'}</ul>
      <form id="di-add" class="compose" autocomplete="off">
        <input id="di-target" placeholder="Mit figyeljek? Pl. RTX 3080, vagy egy oldal címe">
        <div class="checks">${(I.sites || []).map((s) => `<label class="check"><input type="checkbox" name="di-site" value="${esc(s.key)}" checked> ${esc(s.name)}</label>`).join("")}</div>
        <div class="row">
          <label>Nézze <select id="di-every">${(I.every_choices || [2, 3, 5, 10, 15]).map((m) => `<option value="${m}"${m === 3 ? " selected" : ""}>${m} percenként</option>`).join("")}</select></label>
        </div>
        <div class="row">
          <label>Ár: legalább <input id="di-min" type="number" inputmode="numeric" min="0" placeholder="0" style="width: 6em"> Ft</label>
          <label>legfeljebb <input id="di-max" type="number" inputmode="numeric" min="0" placeholder="bármennyi" style="width: 7em"> Ft</label>
        </div>
        <label class="field-label">Kizáró szavak <input id="di-exclude" placeholder="pl. hibás, tok, doboz"></label>
        <div class="row"><button type="submit" class="primary">Figyelő hozzáadása</button></div>
        ${presets ? `<div class="row presets"><small class="muted">Gyorsan (Hardverapró):</small>${presets}</div>` : ""}
      </form>
      ${hits ? `<details class="setup" data-fold="fresh"${openSections.has("fresh") ? " open" : ""}><summary>Friss hirdetések (${(I.hits || []).length})</summary><ul class="dealist">${hits}</ul></details>` : ""}`;
  }
  const hhmmOf = (t) => new Date(t * 1000).toLocaleTimeString("hu-HU", { hour: "2-digit", minute: "2-digit" });
  const whenOf = (t) => new Date(t * 1000).toLocaleString("hu-HU", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

  // --- egy megérő találat ablaka (értesítésből vagy a listából) ---------------------------------------------------
  // Mennyibe kerül, mennyiért mennek most, mennyit kereshetsz vele; a gomb kimásolja az eladónak szóló üzenetet, és
  // megnyitja a hirdetést (a telefonon a hirdetési oldal alkalmazásában vagy a böngészőben), ahol rögtön elküldheted.
  // Nyugodt szívvel megvehető-e (deals.assess): biztos, jó (figyelmeztetéssel) vagy kockázatos.
  // (A „biztos” szint sem ígéret: a haszon hirdetési árakból becsült.)
  const SAFETY = { "biztos": "Alacsony kockázat", "jó": "Jó vétel", "kockázatos": "Kockázatos" };
  function safetyLine(s) {
    const details = s.level === "biztos" ? s.good : (s.why || []).length ? s.why : s.good;
    return `<b>${esc(SAFETY[s.level] || s.level)}</b>${(details || []).length ? `: ${esc(details.join("; "))}` : ""}`;
  }
  // A gyors eladási becslés (deals.safe_sale): a hirdetési árak mediánjának 90%-a, de legfeljebb a hasonlók alsó
  // negyede (és ha tudjuk, az eltűnt, talán elkelt hasonlók ára); a sávja a becslés megbízhatóságától függ
  // (deals.reliability, sale_range). A becslés megbízhatósága: mi szól mellette, mi ellene.
  const RELIABILITY = { "magas": "magas", "közepes": "közepes", "alacsony": "alacsony", "megadott": "a te megadott árad" };
  function reliabilityLine(r) {
    const why = (r.why || []).length ? `<br><small class="warn">${esc(r.why.join("; "))}</small>` : "";
    const good = (r.good || []).length ? ` <small class="muted">(${esc(r.good.join(", "))})</small>` : "";
    return `<b>${esc(RELIABILITY[r.level] || r.level)}</b>${good}${why}`;
  }
  const span = (pair) => (pair || []).length === 2 ? `${price(pair[0])} – ${price(pair[1])}` : "";
  // Az eladás költségei a beállításokból (deals.set_fees): az üres „nincs megadva”, a 0 megadott nulla.
  const feeValue = (name) => { const v = (deals.fees || {})[name]; return v === null || v === undefined ? "" : esc(String(v)); };
  const feesUnset = () => Object.keys(deals.fee_names || {}).filter((name) => (deals.fees || {})[name] === null).length;
  // Amit a hirdetés leírásából tudunk az állapotáról, és ami nem derül ki (az ismeretlen nem hibátlan).
  const ASPECTS = [["display", "kijelző"], ["battery", "akkumulátor"], ["function", "működés"], ["accessories", "tartozékok"], ["warranty", "garancia"]];
  function unknownAspects(d) {
    const cond = d.condition || {};
    if (!Object.keys(cond).length) return [];
    return ASPECTS.filter(([k]) => !cond[k] || !cond[k].state || cond[k].state === "ismeretlen").map(([, name]) => name);
  }
  // A mai számok (deals.funnel): mennyit látott, mennyi volt új, hány jelölt és jó találat; a Naplóban az is, mi miért
  // esett ki (ebből látszik, miért nincs találat egy napon).
  const FUNNEL_MAIN = [["seen", "hirdetés"], ["new", "új"], ["candidates", "jelölt"], ["found", "jó találat"], ["haggles", "alku"],
                       ["haggle_tips", "alkuval megérheti"],
                       ["auction", "licitlistára"]];
  const FUNNEL_OUT = [["not_cheap", "nem elég olcsó"], ["other", "nem a keresett termék vagy nem eladó"],
                      ["over_limit", "a vételi limit fölött"], ["unknown", "ismeretlen termék"], ["unsure", "bizonytalan ár (Érdemes megnézni)"],
                      ["unverified", "ellenőrizetlen (Érdemes megnézni)"],
                      ["suspicious", "gyanús"], ["risky", "kockázatos"], ["gone", "elkelt"], ["defect", "hibás"],
                      ["model_no", "a modell szerint nem az"], ["muted", "elrejtett termék"],
                      ["bundle", "csomag (a csomagbontásnak)"], ["clothing", "ruha, cipő (azzal nem kereskedem)"]];
  // ráérő időben (deals.warm): az összehasonlítók előre elolvasott leírásai és a kevés árú termékek árkeresései
  const FUNNEL_SPARE = [["warm_reads", "előre elolvasott leírás"], ["warm_searches", "árkeresés"]];
  const thousands = (n) => Number(n).toLocaleString("hu-HU");
  function funnelLine(full) {
    const c = (deals.funnel || {}).counts || {};
    const main = FUNNEL_MAIN.filter(([k]) => c[k] || k === "seen").map(([k, label]) => `${thousands(c[k] || 0)} ${label}`);
    if (!full) return c.seen ? `Ma: ${main.join(" · ")}` : "";
    const out = FUNNEL_OUT.filter(([k]) => c[k]).map(([k, label]) => `${thousands(c[k])} ${label}`);
    const spare = FUNNEL_SPARE.filter(([k]) => c[k]).map(([k, label]) => `${thousands(c[k])} ${label}`);
    return c.seen ? `Ma: ${main.join(" · ")}${out.length ? `. Kiesett: ${out.join(", ")}.` : ""}`
      + (spare.length ? ` Ráérő időben: ${spare.join(", ")}.` : "") : "Ma még nem néztem hirdetést.";
  }
  // Az eladónak szóló üzenet: egy alkuképes hirdetésnél az ajánlat (az {ár} helyén a javasolt összeg), különben az
  // „Megvan még?”.
  function messageFor(d) {
    if (d && d.haggle) return ((deals.haggle || {}).message || "").replaceAll("{ár}", price(d.haggle.offer));
    return deals.message || "";
  }
  // Alkuképes hirdetés: mennyiért van fent, mióta (és leárazták-e), mennyit ajánlj, és akkor mennyi a haszon.
  function haggleFacts(d) {
    const h = d.haggle;
    const since = h.days >= 1 ? `${h.days} napja fent` : "nemrég került fel";
    const cut = h.cuts && h.p0 > d.price ? ` · már leárazta: ${esc(price(h.p0))} → ${esc(price(d.price))}` : "";
    const rows = [["Kért ár", `<b>${esc(price(d.price))}</b> · ${esc(d.site)} · ${esc(since)}${cut}`],
                  ["Ajánlj", `<b>${esc(price(h.offer))}</b>-ot (${esc(Math.round(h.cut * 100))}%-kal kevesebb)`],
                  ["Becsült haszon", `<b>kb. ${esc(price(d.profit))}</b>${(d.profit_range || []).length === 2 ? ` <small class="muted">(${esc(span(d.profit_range))})</small>` : ""}, ha ennyiért megkapod (minden költséggel)`],
                  ["Hirdetési ár (medián)", `kb. ${esc(price(d.median))}${d.similar ? ` (${esc(d.similar)} hasonló alapján)` : ""}`]];
    costRows(d, rows);
    if ((h.flex || []).length) rows.push(["A hirdetésben", esc(h.flex.join(", "))]);
    if (d.reliability) rows.push(["A becslés megbízhatósága", reliabilityLine(d.reliability)]);
    if (d.safety) rows.push(["Kockázat", safetyLine(d.safety)]);
    return `<p class="muted">Alkuképes hirdetés · ${esc(ago(d.at))}</p>
      <dl class="facts">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>
      ${d.note ? `<p class="muted">${esc(d.note)}</p>` : ""}
      ${(d.warnings || []).map((w) => `<p class="warn">Figyelj: ${esc(w)}</p>`).join("")}
      <p class="muted">Ezt másolom ki neked:</p><pre class="msgpreview">${esc(messageFor(d))}</pre>`;
  }
  // Egy csomag tételei: darabszám, név, darabár, és honnan (mostani hirdetésekből vagy a modell becsléséből).
  function bundleLines(d) {
    const min = (deals.bundles || {}).piece_min || 1500;
    return `<ul class="dealrows">${d.bundle.lines.map((l) => `<li>${l.count > 1 ? `${esc(l.count)} db ` : ""}${esc(l.name)}:
      ${l.unit ? `kb. ${esc(price(l.unit))}${l.count > 1 ? "/db" : ""}` : "nem ér semmit"}
      <small class="muted">${l.source === "piac" ? `(${l.n ? `${esc(l.n)} hirdetés alapján` : "hirdetésekből"})` : "(becslés)"}${l.unit && l.unit < min ? " · egyben a többi apróval" : ""}</small></li>`).join("")}</ul>`;
  }
  function bundleFacts(d) {
    const b = d.bundle;
    const rows = [["Ára", `<b>${esc(price(d.price))}</b> · ${esc(d.site)} · csomag`],
                  ["Mi van benne", bundleLines(d)],
                  ["Darabonként eladva", `kb. ${esc(price(b.value))} (${esc(b.listings)} hirdetés${b.leftover ? `; ${esc(b.small)} apróság egyben` : ""})`],
                  ["Becsült haszon", `<b>kb. ${esc(price(d.profit))}</b> (a posta és darabonként a csomagolás benne van${b.selling ? `; az eladás költségei összesen kb. ${esc(price(b.selling))}` : ""})`]];
    if (d.price_scope) rows.push(["Az ára", `${esc(d.price_scope.scope)}${d.price_scope.quote ? ` <small class="muted">(„${esc(d.price_scope.quote)}”)</small>` : ""}`]);
    if ((d.fees_unset || []).length) rows.push(["Nincs megadva", `<span class="warn">${esc(d.fees_unset.join(", "))}</span> <small class="muted">(0 Ft-tal számoltam; Eladási költségek)</small>`]);
    if (d.safety) rows.push(["Kockázat", safetyLine(d.safety)]);
    return `<p class="muted">Megtaláltam: ${esc(ago(d.at))}${d.explore ? ` · ${esc(d.explore)}` : ""}</p>
      <dl class="facts">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>
      ${d.note ? `<p class="muted">${esc(d.note)}${d.photos_checked ? " (a hirdetés fotói alapján is)" : ""}</p>` : ""}
      ${(d.warnings || []).map((w) => `<p class="warn">Figyelj: ${esc(w)}</p>`).join("")}
      <p class="muted">Ezt másolom ki neked:</p><pre class="msgpreview">${esc(messageFor(d))}</pre>`;
  }
  // Mihez mérte: a beszámított hirdetések (a cím, az ár, az oldal, a link), és a kizártak az okkal (pl. más változat,
  // kilógó ár, „-tól” ár): így látszik, ha egy drágább változathoz mérte volna.
  const SOURCE = { "most": "most találtam", "árbázis": "korábban láttam", "felmérés": "a felmérésből" };
  function basisRow(e) {
    const link = safeUrl(e.u) ? `<a href="${esc(e.u)}" target="_blank" rel="noopener noreferrer">${esc(e.t || "a hirdetés")}</a>` : esc(e.t || "?");
    const tags = [e.s, SOURCE[e.src] || "", e.v ? `eladó: ${e.v}` : "", e.read ? "a leírásából" : "",
                  e.gone ? "azóta eltűnt (talán elkelt)" : ""].filter(Boolean);
    const approx = e.approx ? `<br><small class="muted">közelítő: ${esc(e.approx)}</small>` : "";
    return `<li>${link}: <b>${esc(price(e.p))}</b> <small class="muted">${esc(tags.join(" · "))}</small>${approx}${e.why ? `<br><small class="warn">${esc(e.why)}</small>` : ""}</li>`;
  }
  function basisBlock(d) {
    const b = d.basis;
    if (!b || !((b.used || []).length || (b.out || []).length || b.note)) return "";
    const nUsed = b.n_used || (b.used || []).length, nOut = b.n_out || (b.out || []).length;
    return `<details class="basis"><summary>Mihez mértem? ${esc(nUsed)} hirdetés${nOut ? ` · ${esc(nOut)} kizárva` : ""}</summary>
      ${b.note ? `<p class="muted">${esc(b.note)}</p>` : ""}
      ${(b.used || []).length ? `<ul class="dealrows">${b.used.map(basisRow).join("")}</ul>${nUsed > b.used.length ? `<p class="muted">…és még ${esc(nUsed - b.used.length)}.</p>` : ""}` : ""}
      ${(b.out || []).length ? `<p class="muted">Kizártam (ezek nem számítottak bele):</p><ul class="dealrows">${b.out.map(basisRow).join("")}</ul>` : ""}
    </details>`;
  }
  // Az eladó többi hirdetése (egy jó találatnál): mi éri meg még (külön is eladható-e), együtt mennyi a haszon (egy
  // posta, a Vinteden a vevővédelmi díj a végösszegre egyszer), mennyi munka meghirdetni, és mennyit ajánlj együtt.
  function sellerBlock(d) {
    const s = d.seller;
    if (!s || !((s.items || []).length || s.unknown)) return "";
    const items = (s.items || []).map((i) => `<li>${safeUrl(i.u) ? `<a href="${esc(i.u)}" target="_blank" rel="noopener noreferrer">${esc(i.t)}</a>` : esc(i.t)}:
      <b>${esc(price(i.p))}</b> <small class="${i.pick ? "muted" : "warn"}">${i.pick ? `külön is eladható, kb. ${esc(price(i.margin))} haszon` : esc(i.why || "")}${i.n ? ` · ${esc(i.n)} hasonló alapján` : ""}${i.pace === "gyors" ? " · gyorsan kel" : ""}</small></li>`).join("");
    const fee = s.fee ? `; Vinted vevővédelmi díj kb. ${esc(price(s.fee))} a végösszegre egyszer` : "";
    const offer = s.offer ? ` Közös vételre ajánlj kb. ${esc(price(s.offer))}-ot${s.vinted ? " (csomagajánlat)" : ""}.` : "";
    const sums = s.picked
      ? `<p>Együtt <b>${esc(price(s.total))}</b> (egy posta: ${esc(price(s.shipping))}${fee}): kb. <b>${esc(price(s.profit))}</b> haszon (csak ez: kb. ${esc(price(s.single))}; külön-külön véve: kb. ${esc(price(s.separately))}). Munka: ${esc(s.listings)} hirdetés, kb. ${esc(s.minutes)} perc.${offer}</p>`
      : `<p class="muted">Külön egyik sem éri meg biztosan.</p>`;
    return `<details class="basis seller"${s.worth ? " open" : ""}><summary>Az eladó többi hirdetése: ${s.picked ? `még ${esc(s.picked)} darab megérheti` : "nincs köztük megérő"}</summary>
      ${items ? `<ul class="dealrows">${items}</ul>` : ""}${s.unknown ? `<p class="muted">${esc(s.unknown)} hirdetésének az árát nem ismerem.</p>` : ""}${sums}
      ${safeUrl(s.url) ? `<p><a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">Az eladó hirdetései</a></p>` : ""}</details>`;
  }
  // A költségek tételesen (deals._deal, _reprice): posta, a Vinteden a vevővédelmi díj, az állapot szerinti pótlás,
  // javítás, az eladás költségei (csomagolás, eladási díj, tartalék), és ami nincs megadva (nem csendben nulla).
  function costRows(d, rows) {
    const parts = (d.cost_parts || []).length ? d.cost_parts : (d.costs ? [{ what: "posta", amount: d.costs }] : []);
    if (parts.length && !d.auction) rows.push(["Költség", parts.map((c) => `${esc(c.what)} kb. ${esc(price(c.amount))}`).join(" · ")
      + (parts.length > 1 ? ` <small class="muted">(összesen kb. ${esc(price(d.costs))})</small>` : "")]);
    if ((d.unpriced || []).length) rows.push(["Hiányzó költség", `<span class="warn">${esc(d.unpriced.join(", "))}: nincs megadva a javítás vagy a pótlás ára (Pótlás és javítás), így a haszon bizonytalan</span>`]);
    if ((d.fees_unset || []).length) rows.push(["Nincs megadva", `<span class="warn">${esc(d.fees_unset.join(", "))}</span> <small class="muted">(0 Ft-tal számoltam; Eladási költségek)</small>`]);
  }
  function dealFacts(d) {
    if (d.bundle) return bundleFacts(d);
    if (d.haggle) return haggleFacts(d);
    const sale = d.resale || (d.median ? Math.round(Math.min(d.median * (deals.sale_ratio || 0.9), d.low || Infinity) / 100) * 100 : null);
    const where = (d.sites || []).length ? `, ${d.sites.join(" és ")}` : "";
    const rows = [["Ára", `<b>${esc(price(d.price))}</b>${d.drop ? ` (leárazta: ${esc(price(d.drop))} → ${esc(price(d.price))})` : ""} · ${esc(d.site)}${d.auction ? " (licit)" : ""}`]];
    if (d.identity) rows.push(["Változat", `${esc(d.identity)}${d.variant_note ? `<br><small class="warn">${esc(d.variant_note)}</small>` : ""}`]);
    else if (d.variant_note) rows.push(["Változat", `<small class="warn">${esc(d.variant_note)}</small>`]);
    rows.push([d.auction ? "Máshol ennyiért adják" : "Hirdetési ár (medián)", `kb. ${esc(price(d.median))}${d.similar ? ` (${esc(d.similar)} hasonló alapján${esc(where)})` : ""} <small class="muted">(kínálati ár: ennyiért hirdetik, nem ennyiért kel el)</small>`]);
    if ((d.band || []).length === 2) rows.push(["Kínálati ársáv", `${esc(price(d.band[0]))} – ${esc(price(d.band[1]))} <small class="muted">(a hasonlók többsége)</small>`]);
    if (sale && !d.auction) rows.push(["Gyors eladási becslés", `kb. <b>${esc(price(sale))}</b>${(d.resale_range || []).length === 2 ? ` <small class="muted">(${esc(span(d.resale_range))})</small>` : ""} <small class="muted">(a medián 90%-a, de legfeljebb a hasonlók alsó negyede${d.competitor ? "; egy olcsóbb, ugyanilyen hirdetés miatt lejjebb" : ""})</small>`]);
    if (d.competitor) rows.push(["Versenytárs", `${safeUrl(d.competitor.u) ? `<a href="${esc(d.competitor.u)}" target="_blank" rel="noopener noreferrer">${esc(d.competitor.t || "egy hirdetés")}</a>` : esc(d.competitor.t || "egy hirdetés")}: ${esc(price(d.competitor.p))}${d.competitor.up ? ` <small class="muted">(${esc(d.competitor.up)} napja fent, a leírása szerint hibátlan)</small>` : ""}`]);
    if (d.sale_mode && !d.auction) rows.push(["Hogyan adnád el", esc(d.sale_mode)]);
    costRows(d, rows);
    if (d.condition_text) rows.push(["Állapot", `${esc(d.condition_text)}${d.verified ? "" : ' <small class="muted">(a leírás szerint)</small>'}`]);
    const unknown = unknownAspects(d);
    if (unknown.length) rows.push(["Nem derül ki", `${esc(unknown.join(", "))} <small class="muted">(az ismeretlen nem hibátlan)</small>`]);
    if (d.cond_band) rows.push(["Hasonló hibájúak", `kb. ${esc(price(d.cond_band.price))} (${esc(d.cond_band.n)} hirdetés: ${esc(d.cond_band.issues.join(", "))}); a hibátlanok kb. ${esc(price(d.cond_band.clean))}`]);
    rows.push(["Ellenőrizve", d.verified ? "igen: az adatlapja megnyílt, a leírása és az állapota alapján újraszámoltam, és a modell is átnézte <small class=\"muted\">(a haszon becslés, nem garancia)</small>"
      : d.model_no ? `<span class="warn">utólag mégsem jó: ${esc(d.model_no)}</span>`
      : d.unchecked ? `<span class="warn">az adatlapja és az állapota alapján igen, de a modell még nem nézte meg (${esc(d.unchecked)})${d.model_pending ? "; amint lehet, megnézi" : ""}</span>`
      : '<span class="warn">nem (nézd meg magad)</span>']);
    if (d.auction) rows.push(["Meddig licitálj", `legfeljebb <b>${esc(price(d.max_bid))}</b>-ig${d.left ? ` · ${esc(leftText(d))}` : ""}`]);
    else if (d.offer) rows.push(["Ajánlattal", `ha ${esc(price(d.offer))}-ért megkapod, kb. <b>${esc(price(d.profit))}</b> a haszon`]);
    else rows.push(["Becsült haszon", `<b>kb. ${esc(price(d.profit))}</b>${(d.profit_range || []).length === 2 ? ` <small class="muted">(${esc(span(d.profit_range))})</small>` : ""}${sale ? ` (ha kb. ${esc(price(sale))}-ért eladod, minden költség után)` : ""}`]);
    if (!d.auction && d.max_buy !== undefined && d.max_buy !== null) rows.push(["Legfeljebb ennyiért vedd meg", `<b>${esc(price(d.max_buy))}</b> <small class="muted">(ennyiért még meglenne a beállított haszon, minden költséggel)</small>`]);
    else if (!d.auction && (d.unpriced || []).length) rows.push(["Legfeljebb ennyiért vedd meg", '<span class="warn">nem tudni (hiányzik egy javítási vagy pótlási költség)</span>']);
    if (d.reliability) rows.push(["A becslés megbízhatósága", reliabilityLine(d.reliability)]);
    if (d.sold) rows.push(["Eltűnt (talán elkelt) hasonlók", `kb. ${esc(price(d.sold.median))} (${esc(d.sold.n)} db alapján) <small class="muted">(ennyiért hirdették; nem tudni, mennyiért keltek el)</small>`]);
    if (d.speed) rows.push(["Eladási sebesség", esc(d.speed)]);
    if (d.safety) rows.push(["Kockázat", safetyLine(d.safety)]);
    return `<p class="muted">Megtaláltam: ${esc(ago(d.at))}${d.explore ? ` · ${esc(d.explore)}` : ""}</p>
      <dl class="facts">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>
      ${sellerBlock(d)}${basisBlock(d)}
      ${d.note ? `<p class="muted">${esc(d.note)}${d.photos_checked ? " (a hirdetés fotói alapján is)" : ""}</p>` : ""}
      ${(d.warnings || []).map((w) => `<p class="warn">Figyelj: ${esc(w)}</p>`).join("")}
      <p class="muted">Ezt másolom ki neked:</p><pre class="msgpreview">${esc(messageFor(d))}</pre>`;
  }
  let dealOpen = null;
  async function openDeal(id) {
    setJob("deals");
    await loadDeals();
    const d = (deals.deals || []).find((x) => x.id === id);
    if (!d) { J.toast("Ez a találat már nincs meg (lehet, hogy elrejtetted)."); return; }
    dealOpen = d.id;
    $("deal-title").textContent = d.title || d.text;
    $("deal-body").innerHTML = dealFacts(d);
    $("deal-go").href = safeUrl(d.url) || "#";
    $("deal-bought").hidden = !!d.bought;
    if (!$("deal-dialog").open) $("deal-dialog").showModal();
  }
  // Szöveg a vágólapra: az Android-alkalmazásban a saját hídján (a WebView-ban ez a biztos), máshol a böngészőével.
  function copyText(text, done = "Kimásoltam az üzenetet.") {
    try {
      if (window.JarvisApp && window.JarvisApp.copy) { window.JarvisApp.copy(text); return; }
    } catch { /* tovább a böngészőével */ }
    const fallback = () => {
      const box = document.createElement("textarea");
      box.value = text;
      box.style.position = "fixed";
      box.style.opacity = "0";
      document.body.appendChild(box);
      box.select();
      try { document.execCommand("copy"); J.toast(done); } catch { J.toast("Nem sikerült kimásolni."); }
      box.remove();
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(() => J.toast(done), fallback);
    } else {
      fallback();
    }
  }
  $("deal-go").addEventListener("click", (event) => {
    if ($("deal-go").getAttribute("href") === "#") { event.preventDefault(); return; }
    copyText(messageFor((deals.deals || []).find((x) => x.id === dealOpen)));   // a hirdetés (a link) utána magától megnyílik
    $("deal-dialog").close();
  });
  // Szakértői tipp: a kérdés (a hirdetés adataival és linkjével) a vágólapra, és megnyílik a ChatGPT (az
  // Android-alkalmazásban a ChatGPT alkalmazás, ha telepítve van), ahol csak be kell illeszteni.
  function expertPrompt(d) {
    if (d.haggle) {
      return "Találtam egy hirdetést, ami egy ideje fent van, és kicsit drága; ajánlatot tennék rá, és utána továbbadnám. " +
        "Szerinted reális az ajánlat, és megérné-e?\n\n" +
        `${d.title || d.text}\nKért ár: ${price(d.price)} (${d.site}), ${d.haggle.days} napja fent\n` +
        `Ajánlat: ${price(d.haggle.offer)}\nA hasonlók most kb. ${price(d.median)}-ért mennek.\n` + d.url;
    }
    if (d.bundle) {
      return "Találtam egy csomagot, amit darabonként eladva többet érne, mint amennyibe kerül. Megnéznéd, hogy tényleg " +
        "jó vétel lenne-e, és reálisak-e a darabárak?\n\n" +
        `${d.title || d.text}\nÁra: ${price(d.price)} (${d.site})\nSzerintem ez van benne:\n` +
        d.bundle.lines.map((l) => `- ${l.count > 1 ? `${l.count} db ` : ""}${l.name}: kb. ${price(l.unit)}${l.count > 1 ? "/db" : ""}`).join("\n") +
        `\nDarabonként eladva kb. ${price(d.bundle.value)}, a haszon kb. ${price(d.profit)}.\n` + d.url;
    }
    // a kockázatok is (figyelmeztetések, a modell megjegyzése, a biztonsági értékelés, az állapot), és az összehasonlító
    // hirdetések linkjei: ezekkel ellenőrizhető, mihez mértem
    const lines = [`${d.title || d.text}`, `Ára: ${price(d.price)} (${d.site})`];
    if (d.identity) lines.push(`Változat (a hirdetésből): ${d.identity}`);
    if (d.variant_note) lines.push(`Figyelem: ${d.variant_note}.`);
    if (d.median) lines.push(`A hasonlókat most kb. ${price(d.median)}-ért hirdetik${d.similar ? ` (${d.similar} hirdetés alapján)` : ""}; ez kínálati ár, nem eladási ár.`);
    if ((d.resale_range || []).length === 2) lines.push(`Gyors eladásnál szerintem kb. ${price(d.resale_range[0])}–${price(d.resale_range[1])}.`);
    if (d.max_buy !== undefined && d.max_buy !== null) lines.push(`Legfeljebb ${price(d.max_buy)}-ért venném meg (minden költséggel).`);
    if (d.reliability && d.reliability.level !== "megadott") lines.push(`A becslés megbízhatósága: ${d.reliability.level}${(d.reliability.why || []).length ? ` (${d.reliability.why.join("; ")})` : ""}.`);
    if ((d.fees_unset || []).length) lines.push(`Nem számoltam vele (nincs megadva): ${d.fees_unset.join(", ")}.`);
    if (d.cond_band) lines.push(`Ez a hasonló hibájúak ára (${d.cond_band.issues.join(", ")}); a hibátlanok kb. ${price(d.cond_band.clean)}.`);
    const costs = (d.cost_parts || []).map((c) => `${c.what} kb. ${price(c.amount)}`);
    if (d.profit !== undefined) lines.push(`Várható haszon kb. ${price(d.profit)}${costs.length ? ` (levonva: ${costs.join(", ")})` : ""}.`);
    const cond = d.condition || {};
    const aspects = [["display", "kijelző"], ["battery", "akkumulátor"], ["function", "működés"], ["accessories", "tartozékok"], ["warranty", "garancia"]];
    const known = aspects.filter(([k]) => cond[k] && cond[k].state && cond[k].state !== "ismeretlen");
    if (known.length) {
      lines.push("Állapot a leírás szerint:");
      for (const [k, name] of known) {
        const state = k === "accessories" && cond[k].state === "hiányos" ? `hiányzik: ${(cond.missing || []).join(", ") || "egyéb"}` : cond[k].state;
        lines.push(`- ${name}: ${state}${cond[k].evidence ? ` („${cond[k].evidence}”)` : ""}`);
      }
      const unknown = aspects.filter(([k]) => !cond[k] || !cond[k].state || cond[k].state === "ismeretlen").map(([, name]) => name);
      if (unknown.length) lines.push(`- nem derül ki: ${unknown.join(", ")}`);
    } else if (d.condition_text) {
      lines.push(`Állapot: ${d.condition_text}`);
    }
    const risks = [...((d.safety || {}).why || []), ...(d.warnings || [])];
    if (d.safety) lines.push(`Az én értékelésem: ${SAFETY[d.safety.level] || d.safety.level}.`);
    if (risks.length) lines.push("Kockázatok:", ...risks.map((w) => `- ${w}`));
    if (d.note) lines.push(`A modell megjegyzése: ${d.note}`);
    if (!d.verified) lines.push("Az adatlapját és az állapotát nem tudtam teljesen ellenőrizni.");
    const used = ((d.basis || {}).used || []).filter((e) => e.u).slice(0, 5);
    if (used.length) lines.push("Ezekhez mértem:", ...used.map((e) => `- ${e.t || "hirdetés"}: ${price(e.p)}${e.s ? ` (${e.s})` : ""} ${e.u}`));
    const out = ((d.basis || {}).out || []).filter((e) => e.u && e.why).slice(0, 3);
    if (out.length) lines.push("Kizártam (az okkal):", ...out.map((e) => `- ${e.t || "hirdetés"}: ${price(e.p)} – ${e.why} ${e.u}`));
    return "Találtam egy jónak tűnő árut, ami jó befektetés lehetne, ha vétel után kicsit drágábban továbbárusítanám. " +
      "Megnéznéd, hogy tényleg jó vétel lenne-e, és mit kockáztatok vele?\n\n" + lines.join("\n") + `\n\nA hirdetés: ${d.url}`;
  }
  $("deal-expert").addEventListener("click", (event) => {
    const d = (deals.deals || []).find((x) => x.id === dealOpen);
    if (!d) { event.preventDefault(); return; }
    copyText(expertPrompt(d), "Kimásoltam a kérdést: a ChatGPT-ben csak illeszd be.");
    try {
      if (window.JarvisApp && window.JarvisApp.openChatGpt) { event.preventDefault(); window.JarvisApp.openChatGpt(); }
    } catch { /* a link nyitja meg */ }
    $("deal-dialog").close();
  });
  $("deal-close").addEventListener("click", () => $("deal-dialog").close());
  $("deal-bought").addEventListener("click", () => {
    $("deal-dialog").close();
    buying = dealOpen;
    setJob("deals");
    renderDeals();
    if ($("d-paid")) { $("d-paid").scrollIntoView({ block: "center" }); $("d-paid").focus(); }
  });

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
      <p class="profit"><b>${esc(signed(p.month.profit))}</b> ebben a hónapban (${p.month.count} eladás) ·
        összesen <b>${esc(signed(p.total.profit))}</b> (${p.total.count} eladás${p.days !== null ? `, átlagosan ${p.days} nap alatt` : ""})
        ${p.open.count ? ` · bent van ${esc(price(p.open.spent))} (${p.open.count} tárgy még nem kelt el)` : ""}</p>
      <div class="row"><button type="button" class="small" data-job="ledger">Elemzés</button></div>` : "";
    // Megvettem: a vételár (alapból a hirdetés ára), a posta, és hogy postán jön-e (akkor „úton” az Elemzés fülön)
    const buyButton = (d) => d.bought ? cancelButton(d.bought, d.text)
      : buying === d.id ? `<span class="row buyform">
          <label>Vételár <input id="d-paid" data-keep="paid-${esc(d.id)}" type="number" inputmode="numeric" min="1" value="${esc(d.haggle ? d.haggle.offer : d.price)}" aria-label="Vételár (Ft)"></label>
          <label>Posta <input id="d-ship" data-keep="ship-${esc(d.id)}" type="number" inputmode="numeric" min="0" placeholder="0" aria-label="Posta (Ft)"></label>
          <label class="check"><input id="d-transit" type="checkbox" checked> postán jön</label>
          <button type="button" class="small primary" data-deal-paid="${esc(d.id)}">Mentés</button></span>`
      : `<button type="button" class="small" data-deal-buy="${esc(d.id)}">Megvettem</button>`;
    const offerBox = (d) => d.offer_sent ? `<small>Ajánlat elküldve: <b>${esc(price(d.offer_sent.price))}</b> – ha az eladó
          elfogadja, a Vinted értesít.</small>`
      : d.offer ? `<span class="row"><input id="d-offer-${esc(d.id)}" data-keep="offer-${esc(d.id)}" type="number" min="1" value="${esc(d.offer)}" style="width: 6em"
          aria-label="Ajánlat (Ft)"> Ft <button type="button" class="small primary" data-offer="${esc(d.id)}">Ajánlatot teszek</button></span>` : "";
    const haggleCard = (d) => `<small class="muted">${esc(d.site)} · <b class="tag">alku</b> · kért ár ${esc(price(d.price))},
        ajánlj <b>${esc(price(d.haggle.offer))}</b>-ot (${esc(Math.round(d.haggle.cut * 100))}%-kal kevesebb) · ${d.haggle.days >= 1 ? `${esc(d.haggle.days)} napja fent` : "friss"}${d.haggle.cuts ? " · már leárazta" : ""}
        · akkor kb. <b>${esc(price(d.profit))}</b> haszon</small>`;
    const bundleCard = (d) => `<small class="muted">${esc(d.site)} · csomag · <b>${esc(price(d.price))}</b>, darabonként eladva kb. ${esc(price(d.bundle.value))}
        (${esc(d.bundle.listings)} hirdetés) · várható haszon kb. <b>${esc(price(d.profit))}</b>${d.explore ? " · " + esc(d.explore) : ""}</small>
        <details class="setup"><summary>Mi van benne (${d.bundle.lines.length} tétel)</summary>${bundleLines(d)}</details>`;
    const found = deals.deals.map((d) => `<li><span><a href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">${esc(d.haggle ? d.title || d.text : d.text)}</a>
        ${d.bundle ? bundleCard(d) : d.haggle ? haggleCard(d) : `<small class="muted">${esc(d.site)}${d.auction ? " (licit)" : ""} · <b>${esc(price(d.price))}</b>${d.drop ? ` (leárazta: ${esc(price(d.drop))})` : ""},
        ${d.auction ? "máshol" : "szokásos ár"} ${esc(price(d.median))}${d.auction ? ` · ${esc(leftText(d))} · legfeljebb <b>${esc(price(d.max_bid))}</b>-ig érdemes licitálni`
          : d.offer ? ` · ajánlattal (${esc(price(d.offer))}) kb. <b>${esc(price(d.profit))}</b> haszon`
          : d.profit !== undefined ? ` · várható haszon kb. <b>${esc(price(d.profit))}</b>` : ` (kb. ${esc(price(d.margin))}-tal olcsóbb)`}
        ${d.calibration ? " · a felmérés közben" : ""}${d.explore ? " · " + esc(d.explore) : ""}${d.note ? " · " + esc(d.note) : ""}</small>`}
        ${d.seller && d.seller.worth ? `<small><b class="tag">+${esc(d.seller.picked)} az eladótól</b> együtt kb. <b>${esc(price(d.seller.profit))}</b> haszon (Részletek)</small>` : ""}
        ${d.speed ? `<small class="muted">Eladási sebesség: ${esc(d.speed)}</small>` : ""}
        ${d.safety ? `<small class="${d.safety.level === "kockázatos" ? "warn" : "muted"}">${safetyLine(d.safety)}</small>` : ""}
        ${d.model_no ? `<small class="warn">Utólag mégsem jó: ${esc(d.model_no)}</small>`
          : d.unchecked && !d.verified ? `<small class="warn">A modell még nem nézte meg${d.model_pending ? " (amint lehet, megnézi)" : ""}</small>` : ""}
        ${(d.warnings || []).map((w) => `<small class="warn">Figyelj: ${esc(w)}</small>`).join("")}
        ${offerBox(d)}</span>
        <span class="row"><button type="button" class="small" data-deal-open="${esc(d.id)}">Részletek</button>${buyButton(d)}
        <button type="button" class="small quiet" data-deal-dismiss="${esc(d.id)}">Elrejtés</button></span></li>`).join("");
    // Érdemes megnézni (deals.tips): olcsónak látszott, de kevés a hasonló ára; a Jarvis a körökben újra felméri
    // a licitlista (Vatera): megéri beszállni, a javasolt legmagasabb ajánlat most beállítható; a zárás előtt még szól
    const auctions = (deals.auctions || []).map((a) => `<li><span><a href="${esc(a.url)}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a>
        <small class="muted">${esc(a.site)} · legutóbb <b>${esc(price(a.price))}</b> · zárás: ${esc(whenOf(a.ends))}</small>
        ${a.checked ? `<small>legfeljebb <b>${esc(price(a.max_bid))}</b>-ig érdemes licitálni <span class="muted">(máshol kb. ${esc(price(a.median))})</span></small>`
          : `<small class="warn">ellenőrizetlen: az adatlapját még nem néztem meg, ezért licithatárt még nem mondok</small>
             <small class="muted">máshol kb. ${esc(price(a.median))}</small>`}</span>
        <span class="row"><button type="button" class="small quiet" data-auction-dismiss="${esc(a.id)}">Elrejtés</button></span></li>`).join("");
    // az ellenőrizetlen (deals._unverified): az ára biztos, de az adatlapja, a modell vagy az állapota miatt nem ellenőrzött
    const tips = (deals.tips || []).map((t) => `<li><span><a href="${esc(t.url)}" target="_blank" rel="noopener noreferrer">${esc(t.title)}</a>
        ${t.haggle ? `<small class="muted">${esc(t.site)} · <b>${esc(price(t.price))}</b> · <b class="tag">alkuval</b>
          ajánlj kb. <b>${esc(price(t.haggle.offer))}</b>-ot (${esc(Math.round(t.haggle.cut * 100))}%-kal kevesebb) · kb. <b>${esc(price(t.profit))}</b> haszon
          · a hasonlók kb. ${esc(price(t.usual))}</small>`
        : t.unverified ? `<small class="muted">${esc(t.site)} · <b>${esc(price(t.price))}</b> · <b class="tag">ellenőrizetlen</b>
          a hasonlók kb. ${esc(price(t.usual))} · kb. <b>${esc(price(t.profit))}</b> haszon lehet benne</small>
          <small class="warn">${esc(t.why || "")}</small>${t.condition_text ? `<small class="muted">Állapot: ${esc(t.condition_text)}</small>` : ""}`
        : `<small class="muted">${esc(t.site)} · <b>${esc(price(t.price))}</b> · <b class="tag">bizonytalan ár</b> csak ${esc(t.n)} hasonló
          (${esc((t.sites || []).join(", ") || "?")}) szerint kb. ${esc(price(t.usual))} · kb. <b>${esc(price(t.profit))}</b> haszon lehet benne</small>`}</span>
        <span class="row"><button type="button" class="small quiet" data-tip-dismiss="${esc(t.id)}">Elrejtés</button></span></li>`).join("");
    const book = deals.book || { products: 0, prices: 0, ready: 0 };
    const lookupRows = lookupResults === null ? "" : `<ul class="dealist">${lookupResults.map((r) => `<li><span><b>${esc(r.name)}</b>
        <small class="muted">${r.reference ? `kb. <b>${esc(price(r.reference.price))}</b> (${r.reference.n} ár, a többség
          ${esc(price(r.reference.p25))}–${esc(price(r.reference.p75))})` : `még nincs szokásos ár (${r.samples} ár, vagy nagyon szórnak)`}</small>
        ${r.speed_text ? `<small class="muted">${esc(r.speed_text)}</small>` : ""}</span></li>`).join("") || '<li class="muted">Ilyet még nem láttam.</li>'}</ul>`;
    const running = !!deals.running;
    const b = deals.browse || { on: false, sites: [], min_profit: 5000, status: "" };
    // az indulóoldalak: a beépítettek (kategóriák, a legújabb elöl) és a sajátok ki-be kapcsolhatók, a saját és a tanult
    // törölhető (a tanultat nem veszi fel újra)
    const laneRows = (deals.lanes || []).map((l) => `<li${l.on === false ? ' class="muted"' : ""}><span><b>${esc(l.name)}</b>${l.kind === "tanult" ? ' <small class="warn">tanult</small>' : ""}${l.kind === "sajat" ? ' <small class="tag">saját</small>' : ""}${l.on === false ? ' <small class="muted">kikapcsolva</small>' : ""}
        <small class="muted">${l.pages} oldal · ${l.candidates} jelölt · ${l.deals} jó találat${l.bought ? ` · ${l.bought} megvett` : ""}</small>
        <small><a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">az oldal</a></small></span>
        <span class="row">${l.kind !== "tanult" ? `<button type="button" class="small quiet" data-lane-switch="${esc(l.id)}" data-on="${l.on === false ? "1" : ""}">${l.on === false ? "Bekapcsolás" : "Kikapcsolás"}</button>` : ""}${l.kind !== "page" && l.kind !== "feed" ? `<button type="button" class="small quiet" data-lane-remove="${esc(l.id)}">Törlés</button>` : ""}</span></li>`).join("");
    const hhmm = (t) => new Date(t * 1000).toLocaleTimeString("hu-HU", { hour: "2-digit", minute: "2-digit" });
    const entries = (journalDay ? journalEntries : deals.journal) || [];
    const journalRows = entries.map((e) => `<li><span><small class="muted">${esc(hhmm(e.at))}</small> ${esc(e.text)}</span></li>`).join("")
      || '<li class="muted">Erre a napra még nincs bejegyzés.</li>';
    const dayOptions = (deals.journal_days || []).map((d) => `<option value="${esc(d)}"${d === journalDay ? " selected" : ""}>${esc(d)}</option>`).join("");
    const edit = deals.searches.find((s) => s.id === editingSearch);
    const v = (key, fallback = "") => esc(edit && edit[key] != null ? edit[key] : fallback);
    const searchKey = `search-${edit ? esc(edit.id) : "new"}`;   // a data-keep kulcsok: melyik keresés űrlapja
    const checked = (key) => !edit || (edit.sites || []).includes(key) ? "checked" : "";
    const alertCount = ((deals.instant || {}).alerts || []).length;
    const bd = deals.bundles || { on: false, min_profit: 8000, searches: [], waiting: 0, presets: [] };
    const bundleSearches = bd.searches.map((s) => `<li><span><b>${esc(s.query)}</b>
        <small class="muted">${esc((s.sites || []).map(siteName).join(", "))} · ${esc(every(s.every || 60))}${s.max_price ? ` · legfeljebb ${esc(price(s.max_price))}` : ""}</small></span>
        <button type="button" class="small quiet" data-bundle-remove="${esc(s.id)}">Törlés</button></li>`).join("");
    box.innerHTML = `
      <div class="runbar">
        <button type="button" id="d-run-toggle" class="${running ? "" : "primary"}" ${deals.browser || deals.instant ? "" : "disabled"}>${running ? "Leállítás" : "Indítás"}</button>
        <button type="button" id="d-turbo" class="turbo${deals.turbo ? " on" : ""}" aria-pressed="${deals.turbo ? "true" : "false"}"
          title="Gyakrabban kérdezem a modellt, hogy hamarabb kiderüljön, mi éri meg">Turbó${deals.turbo ? " be" : ""}</button>
        <span class="runstate"><span><b>${running ? "Fut" : "Áll"}</b>${running && deals.started ? ` · ${esc(hhmmOf(deals.started))} óta` : ""}</span>
          ${running && b.status ? `<small>${esc(b.status)}</small>` : ""}</span>
      </div>
      ${deals.turbo ? `<p class="turbonote"><small>Turbó: minden oldal ismeretlen hirdetéseit rögtön megnevezem a modellel, és
        amíg nem dolgozol velem, a modell mindkét helyét a kereskedés használja. A weboldalaktól nem kérek többet.</small></p>` : ""}
      ${deals.browser ? "" : '<p class="warn"><small>A Vintedhez és a Vaterához Jarvis saját böngészője kell.</small></p>'}
      ${deals.money && deals.money.budget ? `<p><small>Havi keret: ${esc(price(deals.money.spent) || "0 Ft")} / ${esc(price(deals.money.budget))}
        (${deals.money.remaining ? `${esc(price(deals.money.remaining))} maradt` : "elfogyott"})</small></p>` : ""}
      ${((deals.money || {}).warnings || []).map((w) => `<p class="warn"><small>${esc(w)}</small></p>`).join("")}
      ${funnelLine(false) ? `<p class="funnel"><small class="muted">${esc(funnelLine(false))}</small></p>` : ""}
      <h3 class="section-title">Legutóbbi találatok</h3>
      <ul class="dealist">${found || '<li class="muted">Még nincs találat.</li>'}</ul>
      ${tips ? `<h3 class="section-title">Érdemes megnézni <small class="muted">bizonytalan ár, ellenőrizetlen vagy alkuval</small></h3>
      <p><small class="muted">Olcsónak látszik, de kevés hasonló árat találtam, vagy nem tudtam ellenőrizni (az adatlapját, az
        állapotát), ezért nem biztos, hogy megéri; vagy kicsit drága, de egy kis alkuval megérné. Nézd meg magad; ha közben
        kiderül, újra felmérem, és ha megéri, szólok.</small></p>
      <ul class="dealist">${tips}</ul>` : ""}
      ${auctions ? `<h3 class="section-title">Licitlista <small class="muted">Vatera</small></h3>
      <p><small class="muted">Ezeknél a licit még nem zárul hamarosan, de megérheti beszállni. Ha az adatlapját már megnéztem, a
        javasolt összeget a Vaterán most legmagasabb ajánlatként beállíthatod (addig licitál helyetted). A zárás előtt még
        egyszer megnézem (a mostani árát és a mostani beállításaidat), és szólok.</small></p>
      <ul class="dealist">${auctions}</ul>` : ""}
      ${section("settings", "Beállítások", `
        <div class="fields limits">
          <div class="field"><label for="d-maxbuy">Max. vételár (Ft)</label>
            <input id="d-maxbuy" data-keep="d-maxbuy" type="number" inputmode="numeric" min="0" placeholder="bármennyi" value="${esc(deals.max_buy ?? "")}"></div>
          <div class="field"><label for="d-minprofit">Min. haszon (Ft)</label>
            <input id="d-minprofit" data-keep="d-minprofit" type="number" inputmode="numeric" min="0" value="${esc(b.min_profit)}"></div>
        </div>
        <label class="check"><input id="d-safeonly" type="checkbox" ${deals.safe_only !== false ? "checked" : ""}>
          Csak a nyugodt szívvel megvehetőkről szólj</label>
        <label class="check"><input id="d-fastalert" type="checkbox" ${deals.fast_alert !== false ? "checked" : ""}>
          Gyors riasztás: az adatlap után rögtön szólj, a modell utána nézze meg</label>
        <div class="row"><button type="button" class="small" id="d-limits">Mentés</button></div>
        <label class="check"><input id="d-haggle" type="checkbox" ${(deals.haggle || {}).on !== false ? "checked" : ""}>
          Alkuképes hirdetések is (régóta fent lévő vagy friss, kicsit drága)</label>
        <label class="field-label" for="d-haggle-message">Ajánlat az eladónak ({ár} = a javasolt összeg)</label>
        <textarea id="d-haggle-message" data-keep="d-haggle-message" rows="3">${esc((deals.haggle || {}).message || "")}</textarea>
        <div class="row"><button type="button" class="small" id="d-haggle-save">Mentés</button></div>
        <label class="field-label" for="d-message">Üzenet az eladónak</label>
        <textarea id="d-message" data-keep="d-message" rows="3">${esc(deals.message || "")}</textarea>
        <div class="row"><button type="button" class="small" id="d-message-save">Mentés</button></div>`)}
      ${section("costs", `Pótlás és javítás (${(deals.costs || []).length})`, `
        <p><small class="muted">Ha egy jelöltnek a leírása szerint hiányzik a töltője, vagy hibás a kijelzője, ennyit vonok le a
          hasznából. Ha nincs megadva, és hasonló hibájú hirdetést sem találok, nem szólok róla sürgősen (ellenőrizetlen marad).
          A termék üresen mindenre vonatkozik; megadva csak arra (és az erősebb).</small></p>
        <ul class="dealist">${(deals.costs || []).map((c) => `<li><span><b>${esc(c.part)}</b>${c.product ? ` <small class="muted">${esc(c.product)}</small>` : ""}
          <small>kb. ${esc(price(c.amount))}</small></span>
          <button type="button" class="small quiet" data-cost-remove="${esc(c.id)}">Törlés</button></li>`).join("") || '<li class="muted">Még nincs megadva.</li>'}</ul>
        <form id="d-cost" class="compose" autocomplete="off">
          <div class="row">
            <label>Mi <input id="d-cost-part" list="d-cost-parts" placeholder="pl. töltő, kijelző" style="width: 9em"></label>
            <datalist id="d-cost-parts">${(deals.cost_parts || []).map((x) => `<option value="${esc(x)}">`).join("")}</datalist>
            <label>Termék <input id="d-cost-product" placeholder="üresen: mind" style="width: 10em"></label>
            <label><input id="d-cost-amount" type="number" inputmode="numeric" min="1" style="width: 6em"> Ft</label>
          </div>
          <div class="row"><button type="submit" class="small">Hozzáadás</button></div>
        </form>`)}
      ${section("fees", `Eladási költségek${feesUnset() ? ` (${feesUnset()} nincs megadva)` : ""}`, `
        <p><small class="muted">Minden találat hasznából levonom (a posta és a Vinteden a vevővédelmi díj mellett). Ami üres,
          az nincs megadva: 0 Ft-tal számolom, de a találatnál látszik, hogy nincs benne; a 0 megadott nulla.</small></p>
        <form id="d-fees" class="compose" autocomplete="off">
          <div class="row">
            <label>Csomagolás <input id="d-fee-packaging" data-keep="d-fee-packaging" type="number" inputmode="numeric" min="0" style="width: 6em" value="${feeValue("packaging")}"> Ft</label>
            <label>Eladási díj <input id="d-fee-sell_pct" data-keep="d-fee-sell_pct" type="number" inputmode="decimal" min="0" step="0.1" style="width: 5em" value="${feeValue("sell_pct")}"> %</label>
          </div>
          <div class="row">
            <label>Egyéb eladási költség <input id="d-fee-sell_fixed" data-keep="d-fee-sell_fixed" type="number" inputmode="numeric" min="0" style="width: 6em" value="${feeValue("sell_fixed")}"> Ft</label>
            <label>Tartalék <input id="d-fee-reserve_pct" data-keep="d-fee-reserve_pct" type="number" inputmode="decimal" min="0" step="0.1" style="width: 5em" value="${feeValue("reserve_pct")}"> %</label>
          </div>
          <div class="row"><button type="submit" class="small">Mentés</button></div>
        </form>`)}
      ${section("searches", `Keresések (${deals.searches.length})`, `
        <ul class="dealist">${searches || '<li class="muted">Még nincs keresés.</li>'}</ul>
        <form id="d-add" class="compose" autocomplete="off">
          ${edit ? `<p><b>Szerkesztés: ${esc(edit.query)}</b></p>` : ""}
          <input id="d-query" data-keep="${searchKey}-query" placeholder="Mit keressek? Pl. RTX 3060 Ti, PS5 kontroller" value="${v("query")}">
          <div class="checks">${deals.sites.map((s) => `<label class="check"><input type="checkbox" name="d-site" value="${esc(s.key)}" ${checked(s.key)}> ${esc(s.name)}</label>`).join("")}</div>
          <div class="row">
            <label>Legalább <input id="d-discount" data-keep="${searchKey}-discount" type="number" min="10" max="80" value="${v("discount", 30)}" style="width: 4em">%-kal olcsóbb,</label>
            <label>vagy legalább <input id="d-profit" data-keep="${searchKey}-profit" type="number" min="0" placeholder="–" value="${v("min_profit")}" style="width: 6em"> Ft haszon</label>
          </div>
          <div class="row">
            <label>Ár: legalább <input id="d-min" data-keep="${searchKey}-min" type="number" min="0" placeholder="0" value="${v("min_price")}" style="width: 6em"> Ft</label>
            <label>legfeljebb <input id="d-max" data-keep="${searchKey}-max" type="number" min="0" placeholder="bármennyi" value="${v("max_price")}" style="width: 7em"> Ft</label>
          </div>
          <label class="field-label">Kizáró szavak <input id="d-exclude" data-keep="${searchKey}-exclude" placeholder="pl. tok, töltő, játék, doboz" value="${esc(edit ? (edit.exclude || []).join(", ") : "")}"></label>
          ${edit ? "" : `<label class="field-label">Szokásos ár, ha tudod <input id="d-ref-new" data-keep="${searchKey}-ref" type="number" min="1" placeholder="üresen felmérem" style="width: 9em"> Ft</label>`}
          <div class="row">
            <label>Nézze <select id="d-every">${[30, 60, 180, 360].map((m) => `<option value="${m}"${m === Number(v("every", 180)) ? " selected" : ""}>${every(m)}</option>`).join("")}</select></label>
          </div>
          <div class="row"><button type="submit" class="primary">${edit ? "Mentés" : "Hozzáadás"}</button>
            ${edit ? '<button type="button" id="d-cancel">Mégse</button>' : '<button type="button" id="d-run">Keresés most</button>'}</div>
        </form>`)}
      ${section("bundles", `Csomagbontás (${bd.on ? "be" : "ki"})`, `
        <label class="check"><input id="db-on" type="checkbox" ${bd.on ? "checked" : ""}> Csomagok felmérése darabonként</label>
        <div class="row"><label>Legalább <input id="db-profit" data-keep="db-profit" type="number" inputmode="numeric" min="0" value="${esc(bd.min_profit)}" style="width: 6em"> Ft haszon</label>
          <button type="button" class="small" id="db-save">Mentés</button></div>
        ${bd.on && bd.waiting ? `<p><small>${esc(bd.waiting)} csomag vár felmérésre.</small></p>` : ""}
        <ul class="dealist">${bundleSearches || '<li class="muted">Még nincs csomagkeresés.</li>'}</ul>
        <form id="db-add" class="compose" autocomplete="off">
          <input id="db-query" placeholder="Pl. lego ömlesztve, videojáték csomag (ruhával nem kereskedem)">
          <div class="checks">${deals.sites.map((s) => `<label class="check"><input type="checkbox" name="db-site" value="${esc(s.key)}" checked> ${esc(s.name)}</label>`).join("")}</div>
          <div class="row">
            <label>Legfeljebb <input id="db-max" type="number" inputmode="numeric" min="0" placeholder="bármennyi" style="width: 7em"> Ft</label>
            <label>Nézze <select id="db-every">${[30, 60, 180, 360].map((m) => `<option value="${m}"${m === 60 ? " selected" : ""}>${every(m)}</option>`).join("")}</select></label>
          </div>
          <div class="row"><button type="submit" class="primary">Csomagkeresés hozzáadása</button></div>
          ${bd.presets.length ? `<div class="row presets"><small class="muted">Gyorsan:</small>${bd.presets.map((pr, i) => `<button type="button" class="small" data-bundle-preset="${i}">${esc(pr.name)}</button>`).join("")}</div>` : ""}
        </form>`)}
      ${deals.instant ? section("instant", `Azonnali figyelő (${alertCount})`, instantBox()) : ""}
      ${section("browse", "Böngészés", `
        <div class="checks">${deals.sites.map((s) => `<label class="check"><input type="checkbox" name="d-bsite" value="${esc(s.key)}" ${(b.sites || []).includes(s.key) ? "checked" : ""}> ${esc(s.name)}</label>`).join("")}</div>
        <div class="row"><button type="button" class="small" id="d-bsave">Oldalak mentése</button></div>
        ${(b.glance_sites || []).some((k) => (b.sites || []).includes(k)) ? `<div class="row"><label>A Vinted legújabb hirdetései:
          <select id="d-bglance">${(b.glance_choices || [0, 10, 15, 20, 30, 60]).map((m) => `<option value="${m}"${m === (b.glance ?? 20) ? " selected" : ""}>${m ? `${m} percenként egy oldal${m === 20 ? " (alapból)" : ""}` : "kétóránként, hosszabb munkamenetben"}</option>`).join("")}</select></label></div>
        <p><small class="muted">Ahogy egy ember frissíti: egy oldal a legújabbakkal, a többi oldal munkája közé ékelve. Sűrűbben
          hamarabb látom az újat, de a Vinted robot-ellenőrzése is hamarabb jöhet (akkor aznap már nem nézem).</small></p>` : ""}
        <details class="setup" data-fold="lanes"${openSections.has("lanes") ? " open" : ""}><summary>Indulóoldalak, és mit hoztak eddig</summary>
          <p><small class="muted">Alapból egész kategóriákat nézek, a legújabb hirdetésekkel elöl: mindent, ami újonnan felkerül.
            Bármelyiket kikapcsolhatod, és felvehetsz sajátot (egy oldal címét vagy egy keresőszót).</small></p>
          <ul class="dealist">${laneRows || '<li class="muted">Nincs indulóoldal.</li>'}</ul>
          <form id="d-lane" class="compose" autocomplete="off">
            <input id="dl-target" placeholder="Keresőszó (pl. Lego), vagy egy kategória vagy találati oldal címe">
            <div class="row"><label>Hol: <select id="dl-site"><option value="">a bejelölt oldalakon</option>${deals.sites.map((s) => `<option value="${esc(s.key)}">${esc(s.name)}</option>`).join("")}</select></label>
              <button type="submit" class="primary">Indulóoldal hozzáadása</button></div>
            ${(deals.lane_presets || []).length ? `<div class="row presets"><small class="muted">Gyorsan:</small>${deals.lane_presets.map((p) => `<button type="button" class="small" data-lane-preset="${esc(p)}">${esc(p)}</button>`).join("")}</div>` : ""}
          </form>
          <label class="check"><input type="checkbox" id="d-blearn" ${b.learn === false ? "" : "checked"}> Öntanulás: a gyorsan kelő termékekből magam is felveszek indulóoldalt</label>
        </details>`)}
      ${section("watches", `Figyelt oldalak (${(deals.watches || []).length})`, `
        <ul class="dealist">${(deals.watches || []).map((w) => `<li><span><b>${esc(w.name)}</b>
            <small class="muted">legalább ${esc(price(w.min_profit))} haszon${(w.platform || "vatera") === "vatera" ? ` · licitnél a zárás előtt legfeljebb ${w.hours} órával` : ""} · ${esc(every(w.every))}</small>
            <small><a href="${esc(w.url)}" target="_blank" rel="noopener noreferrer">az oldal</a></small></span>
            <button type="button" class="small quiet" data-watch-remove="${esc(w.id)}">Törlés</button></li>`).join("") || '<li class="muted">Még nincs figyelt oldal.</li>'}</ul>
        <form id="d-watch" class="compose" autocomplete="off">
          <input id="dw-target" placeholder="Egy találati oldal címe (legújabb elöl), vagy Vatera-keresőszó">
          <div class="row">
            <label>Legalább <input id="dw-profit" type="number" min="0" value="5000" style="width: 6em"> Ft haszon,</label>
            <label>licitnél a zárás előtt legfeljebb <input id="dw-hours" type="number" min="1" max="72" value="6" style="width: 4em"> órával</label>
          </div>
          <div class="row"><label>Nézze <select id="dw-every">${[30, 60, 180, 360].map((m) => `<option value="${m}"${m === 60 ? " selected" : ""}>${every(m)}</option>`).join("")}</select></label></div>
          <div class="row"><button type="submit" class="primary">Oldal hozzáadása</button></div>
        </form>`)}
      ${profitBox ? section("profit", "Haszon", profitBox) : ""}
      ${section("lookup", "Mennyit ér?", `
        <p><small class="muted">Árbázis: ${book.products} termék, ${book.prices} ár (${book.ready} termékre van szokásos ár)</small></p>
        <form id="d-lookup" class="compose" autocomplete="off">
          <div class="row"><input id="dl-query" placeholder="Pl. RTX 3060 Ti, iPhone 13, Makita DHP482" value="${esc(lookupQuery)}">
            <button type="submit">Megnézem</button></div>
        </form>
        ${lookupRows}`)}
      ${section("journal", "Napló", `
        <p><small class="muted">${esc(funnelLine(true))}</small></p>
        <div class="row"><label>Nap <select id="d-jday"><option value="">Ma</option>${dayOptions}</select></label></div>
        <ul class="dealist">${journalRows}</ul>`)}`;
    for (const fold of box.querySelectorAll("details[data-fold]")) fold.addEventListener("toggle", () => {
      if (fold.open) openSections.add(fold.dataset.fold); else openSections.delete(fold.dataset.fold);
      store("jarvis-deal-open", [...openSections].join(","));
    });
    $("d-run-toggle").addEventListener("click", async () => {
      const start = !running;
      if (await dealAction({ action: start ? "start" : "stop" }))
        J.toast(start ? "Elindítottam: keresek, és ha valami megéri, szólok." : "Leállítottam.");
    });
    $("d-turbo").addEventListener("click", async () => {
      const on = !deals.turbo;
      if (await dealAction({ action: "turbo", on }))
        J.toast(on ? "Turbó bekapcsolva: gyakrabban kérdezem a modellt." : "Turbó kikapcsolva.");
    });
    $("d-limits").addEventListener("click", async () => {
      if (await dealAction({ action: "limits", max_buy: $("d-maxbuy").value, min_profit: $("d-minprofit").value,
                             safe_only: $("d-safeonly").checked, fast_alert: $("d-fastalert").checked }))
        J.toast($("d-maxbuy").value ? `Rendben: ${price($("d-maxbuy").value)} fölött nem keresek.` : "Rendben: bármilyen árban keresek.");
    });
    if ($("di-add")) {
      $("di-add").addEventListener("submit", async (e) => {
        e.preventDefault();
        const sites = [...box.querySelectorAll('input[name="di-site"]:checked')].map((i) => i.value);
        if (await dealAction({ action: "instant_add", target: $("di-target").value, sites,
                               min_price: $("di-min").value, max_price: $("di-max").value, exclude: $("di-exclude").value,
                               every: $("di-every").value }))
          J.toast(running ? "Rendben: megjegyzem, ami már fent van, és ha új jön, ami megéri, szólok."
            : "Felvettem. Az Indítás gombbal indul.");
      });
      for (const b of box.querySelectorAll("[data-ipreset]")) b.addEventListener("click", async () => {
        if (await dealAction({ action: "instant_add", target: b.dataset.ipreset })) J.toast("Felvettem: ha új jön, ami megéri, szólok.");
      });
      for (const b of box.querySelectorAll("[data-iremove]")) b.addEventListener("click", () => dealAction({ action: "instant_remove", id: b.dataset.iremove }));
      for (const b of box.querySelectorAll("[data-ipause]")) b.addEventListener("click", () => dealAction({ action: "instant_pause", id: b.dataset.ipause, off: !!b.dataset.off }));
    }
    $("db-save").addEventListener("click", async () => {
      const on = $("db-on").checked;
      if (await dealAction({ action: "bundles", on, min_profit: $("db-profit").value }))
        J.toast(on ? "Rendben: a csomagokat darabonként is felmérem, és ha megéri, szólok." : "Kikapcsoltam a csomagbontást.");
    });
    $("db-add").addEventListener("submit", async (e) => {
      e.preventDefault();
      const sites = [...box.querySelectorAll('input[name="db-site"]:checked')].map((i) => i.value);
      if (await dealAction({ action: "bundle_add", query: $("db-query").value, sites, max_price: $("db-max").value,
                             every: $("db-every").value }))
        J.toast(running ? "Felvettem: a csomagokat darabonként felmérem, és ha megéri, szólok." : "Felvettem. Az Indítás gombbal indul.");
    });
    for (const b of box.querySelectorAll("[data-bundle-preset]")) b.addEventListener("click", async () => {
      const pr = bd.presets[Number(b.dataset.bundlePreset)];
      if (pr && await dealAction({ action: "bundle_add", query: pr.query, sites: pr.sites })) J.toast(`Felvettem: ${pr.name}.`);
    });
    for (const b of box.querySelectorAll("[data-bundle-remove]")) b.addEventListener("click", () => dealAction({ action: "bundle_remove", id: b.dataset.bundleRemove }));
    $("d-bsave").addEventListener("click", async () => {
      const sites = [...box.querySelectorAll('input[name="d-bsite"]:checked')].map((i) => i.value);
      if (await dealAction({ action: "browse", sites })) J.toast("Mentve.");
    });
    $("d-lane").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (await dealAction({ action: "lane_add", target: $("dl-target").value, site: $("dl-site").value }))
        J.toast("Felvettem: a böngészés ezt is nézi.");
    });
    for (const b of box.querySelectorAll("[data-lane-preset]")) b.addEventListener("click", async () => {
      if (await dealAction({ action: "lane_add", target: b.dataset.lanePreset })) J.toast(`Felvettem: ${b.dataset.lanePreset}.`);
    });
    for (const b of box.querySelectorAll("[data-lane-switch]")) b.addEventListener("click", () =>
      dealAction({ action: "lane_switch", id: b.dataset.laneSwitch, on: !!b.dataset.on }));
    for (const b of box.querySelectorAll("[data-lane-remove]")) b.addEventListener("click", () =>
      dealAction({ action: "lane_remove", id: b.dataset.laneRemove }));
    $("d-blearn").addEventListener("change", (e) => dealAction({ action: "browse", learn: e.target.checked }));
    if ($("d-bglance")) $("d-bglance").addEventListener("change", async (e) => {
      if (await dealAction({ action: "browse", glance: Number(e.target.value) })) J.toast("Mentve.");
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
      editingSearch = b.dataset.dealEdit;
      openSections.add("searches");
      renderDeals();
      $("d-add").scrollIntoView({ block: "start" });
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
    for (const b of box.querySelectorAll("[data-tip-dismiss]")) b.addEventListener("click", () => dealAction({ action: "tip_dismiss", id: b.dataset.tipDismiss }));
    for (const b of box.querySelectorAll("[data-cost-remove]")) b.addEventListener("click", () => dealAction({ action: "cost_remove", id: b.dataset.costRemove }));
    const costForm = box.querySelector("#d-cost");
    if (costForm) costForm.addEventListener("submit", (e) => {
      e.preventDefault();
      dealAction({ action: "cost_add", part: box.querySelector("#d-cost-part").value, product: box.querySelector("#d-cost-product").value,
                   amount: box.querySelector("#d-cost-amount").value });
    });
    const feeForm = box.querySelector("#d-fees");
    if (feeForm) feeForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const fees = {};
      for (const name of Object.keys(deals.fee_names || {})) {
        const input = box.querySelector(`#d-fee-${name}`);
        if (input) fees[name] = input.value.trim();
      }
      dealAction({ action: "fees", fees });
    });
    for (const b of box.querySelectorAll("[data-auction-dismiss]")) b.addEventListener("click", () => dealAction({ action: "auction_dismiss", id: b.dataset.auctionDismiss }));
    for (const b of box.querySelectorAll("[data-deal-open]")) b.addEventListener("click", () => openDeal(b.dataset.dealOpen));
    $("d-haggle-save").addEventListener("click", async () => {
      const on = $("d-haggle").checked;
      if (await dealAction({ action: "haggle", on, message: $("d-haggle-message").value }))
        J.toast(on ? "Mentve: a régóta fent lévő, kicsit drága hirdetésekre ajánlatot javaslok." : "Kikapcsoltam az alkuképes hirdetéseket.");
    });
    $("d-message-save").addEventListener("click", async () => {
      if (await dealAction({ action: "message", text: $("d-message").value })) J.toast("Mentve: ezt másolom ki az eladónak.");
    });
    for (const b of box.querySelectorAll("[data-deal-buy]")) b.addEventListener("click", () => {
      buying = b.dataset.dealBuy;
      renderDeals();
      if (!$("d-paid")) { J.toast("Ez a találat már nincs a listában."); return; }
      $("d-paid").scrollIntoView({ block: "center" });
      $("d-paid").focus();
    });
    for (const b of box.querySelectorAll("[data-cancel-bought]")) b.addEventListener("click", () => cancelBought(b.dataset.cancelBought, b.dataset.name));
    for (const b of box.querySelectorAll("[data-deal-paid]")) b.addEventListener("click", async () => {
      if (b.disabled) return;
      b.disabled = true;      // a válaszig: egy dupla kattintás ne küldje kétszer (a gép is csak egyszer könyveli)
      let result;
      try {
        result = await dealAction({ action: "bought", id: b.dataset.dealPaid, price: $("d-paid").value,
                                    shipping: $("d-ship").value, in_transit: $("d-transit").checked });
      } finally {
        b.disabled = false;
      }
      if (!result) return;
      buying = null;
      J.toast(result.again ? "Ezt már felvetted az Elemzés fülre (nem könyveltem még egyszer)."
        : (result.items || []).length > 1
        ? `Felvettem: a csomag ${result.items.length} darabja külön tárgy lett az eladandók között (a vételár szétosztva). Pipáld be, hova töltsem fel.`
        : "Felvettem az Elemzés fülre és az eladandók közé. Pipáld be, hova töltsem fel.");
      openId = result.item;
      await load();
      setJob("sales");
    });
    for (const b of box.querySelectorAll("[data-open]")) b.addEventListener("click", () => { openId = b.dataset.open; setJob("sales"); });
    for (const b of box.querySelectorAll("[data-job]")) b.addEventListener("click", () => setJob(b.dataset.job));
  }

  // Megvetted, de visszamondtad (vagy az eladó visszamondta): a tárgy mindenhonnan törlődik (Elemzés, eladandók,
  // találatok, havi keret), és erről a hirdetésről többé nem szólok.
  async function cancelBought(id, name) {
    const ok = await ask("Visszamondva", `Visszamondtad a vételt: ${name}?\nTörlöm az Elemzés fülről, az eladandók és a ` +
      "találatok közül, a havi keretbe sem számít bele, és erről a hirdetésről többé nem szólok. Ha egy csomag darabja, " +
      "a csomag többi darabját is. (A munka mappán belül a _torolt mappába kerül.)", "Visszamondva");
    if (!ok) return;
    const result = await J.postJson("/api/work/ledger", { action: "cancel", id });
    if (!result.ok) { J.toast(result.error || "Nem sikerült."); return; }
    ledger = result;
    for (const gone of result.removed || [id]) {     // egy csomagnál a darabjai is
      items.delete(gone);
      if (openId === gone) openId = null;
      if (ledgerOpen === gone) ledgerOpen = null;
    }
    J.toast((result.removed || []).length > 1 ? `Visszamondva: a csomag mind a ${result.removed.length} darabját töröltem.`
      : "Visszamondva: mindenhonnan töröltem.");
    await loadDeals();
    render();
  }

  // --- Elemzés: a Teendőim (pl. egy eladott tárgy feladási kódja), a megvett és az eladott tárgyak költségei (vételár,
  // posta, egyéb), mennyiért adnád tovább, és a haszon (a saját holmid eladása is) ---
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
    if (ledger) {
      todosOpen = (ledger.todos || []).filter((t) => !t.done).length;
      ledgerTab();
    }
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
    todosOpen = (result.todos || []).filter((t) => !t.done).length;
    ledgerTab();
    render();
    return result;
  }

  function ledgerCard(r) {
    const sold = r.stage === "eladva";
    const own = !!r.own;          // a saját holmid (nem vetted): az eladási ára a haszna
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
          <span><small>Bekerülés</small>${own ? "<b>saját</b><small>nem vetted</small>"
            : `<b>${esc(price(r.cost))}</b>${extra.length ? `<small>${esc(price(r.bought_price))} + ${esc(extra.join(" + "))}</small>` : ""}`}</span>
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
          ${own ? '<span class="chip">saját holmi</span>' : ""}
          ${r.stage === "úton" ? `<button type="button" class="small" data-larrived="${esc(r.id)}">Megérkezett</button>` : ""}
          <button type="button" class="small quiet" data-open="${esc(r.id)}">A tárgy lapja</button>
          ${sold ? "" : `<button type="button" class="small quiet" data-cancel-bought="${esc(r.id)}" data-name="${esc(r.name)}">Visszamondva</button>`}
        </div>
      </div>
      ${ledgerOpen === r.id ? ledgerForm(r) : ""}</article>`;
  }

  // Egy tárgy részletei. A data-keep kulcsok a tárgyhoz kötöttek: az újrarajzolás csak a saját, még nem mentett
  // gépelését tartja meg (keepTyping), és a Mentés is csak az átírt mezőket küldi.
  function ledgerForm(r) {
    const keep = (name) => `data-keep="l-${esc(r.id)}-${name}"`;
    const num = (name, label, value, hint = "") => `<div class="field"><label for="l-${name}">${label}</label>
      <input id="l-${name}" data-lfield="${name}" ${keep(name)} type="number" inputmode="numeric" min="0" value="${esc(value ?? "")}" placeholder="${esc(hint)}"></div>`;
    const sold = r.stage === "eladva";
    const sites = (ledger.sites || []).map((s) => `<option value="${esc(s)}">`).join("");
    if (r.own) {      // a saját holmid: csak az eladás adatai (ha mégis vetted valamennyiért, a tárgy lapján add meg)
      return `<form class="lform" data-lsave="${esc(r.id)}" data-own="1" autocomplete="off">
      <div class="fields">
        ${num("sold_price", "Eladási ár (Ft)", r.sold_price)}
        <div class="field"><label for="l-sold_at">Mikor kelt el</label>
          <input id="l-sold_at" data-lfield="sold_at" ${keep("sold_at")} data-day="${esc(dayInput(r.sold_at))}" type="date" value="${esc(dayInput(r.sold_at))}"></div>
        ${num("sell_shipping", "Posta az eladáskor (Ft)", r.sell_shipping, "ha te fizeted")}
        ${num("sell_fee", "Eladási díj (Ft)", r.sell_fee, "jutalék, kiemelés")}
        <div class="field wide"><label for="l-trade_note">Megjegyzés</label>
          <textarea id="l-trade_note" data-lfield="trade_note" ${keep("trade_note")} rows="2" placeholder="pl. a vevő neve, a csomagszám">${esc(r.trade_note)}</textarea></div>
      </div>
      <div class="row"><button type="submit" class="primary small">Mentés</button>
        <button type="button" class="small" data-lclose>Mégse</button></div>
    </form>`;
    }
    return `<form class="lform" data-lsave="${esc(r.id)}" autocomplete="off">
      <div class="fields">
        ${num("bought_price", "Vételár (Ft)", r.bought_price)}
        ${num("bought_shipping", "Posta a vételkor (Ft)", r.bought_shipping, "0")}
        ${num("bought_extra", "Egyéb költség (Ft)", r.bought_extra, "javítás, tisztítás…")}
        <div class="field"><label for="l-bought_from">Honnan</label>
          <input id="l-bought_from" data-lfield="bought_from" ${keep("bought_from")} list="l-sites" value="${esc(r.bought_from)}" placeholder="pl. Hardverapró"></div>
        <div class="field"><label for="l-bought_at">Mikor vetted</label>
          <input id="l-bought_at" data-lfield="bought_at" ${keep("bought_at")} data-day="${esc(dayInput(r.bought_at))}" type="date" value="${esc(dayInput(r.bought_at))}"></div>
        ${num("market_price", "Szokásos ára (Ft)", r.market_price, r.market_now ? String(r.market_now.price) : "")}
        ${num("target_price", "Mennyiért adnád tovább (Ft)", r.target_price, r.price ? String(r.price) : "")}
        ${num("sell_shipping", "Posta az eladáskor (Ft)", r.sell_shipping, "ha te fizeted")}
        ${num("sell_fee", "Eladási díj (Ft)", r.sell_fee, "jutalék, kiemelés")}
        ${sold ? num("sold_price", "Eladási ár (Ft)", r.sold_price) : ""}
        ${sold ? `<div class="field"><label for="l-sold_at">Mikor kelt el</label>
          <input id="l-sold_at" data-lfield="sold_at" ${keep("sold_at")} data-day="${esc(dayInput(r.sold_at))}" type="date" value="${esc(dayInput(r.sold_at))}"></div>` : ""}
        <div class="field wide"><label for="l-trade_note">Megjegyzés</label>
          <textarea id="l-trade_note" data-lfield="trade_note" ${keep("trade_note")} rows="2" placeholder="pl. csomagszám, az eladó neve, egy apró hiba">${esc(r.trade_note)}</textarea></div>
      </div>
      <datalist id="l-sites">${sites}</datalist>
      ${sold ? "" : `<label class="check"><input type="checkbox" data-lfield="in_transit" ${r.in_transit ? "checked" : ""}> Még úton van (postán jön)</label>`}
      <div class="row"><button type="submit" class="primary small">Mentés</button>
        <button type="button" class="small" data-lclose>Mégse</button></div>
    </form>`;
  }

  // Teendőim: egy eladott tárgy csomagjának feladása (a Vinted adta kód, a hely, a vevő); ha a kódot nem sikerült
  // kiolvasni, a címke oldalának képe és szövege. Kész gombbal jelölöd, ha feladtad (este 19:00-kor emlékeztetek).
  function todoCard(t) {
    const link = safeUrl(t.url) ? `<a href="${esc(t.url)}" target="_blank" rel="noopener noreferrer">a beszélgetés</a>` : "";
    const shot = t.shot ? `<a href="/api/work/todo-shot/${esc(t.id)}?t=${encodeURIComponent(token)}" target="_blank" rel="noopener">a címke képe</a>` : "";
    const facts = [t.partner ? `vevő: ${esc(t.partner)}` : "", t.at ? esc(new Date(t.at * 1000).toLocaleDateString("hu-HU")) : "", link, shot];
    return `<article class="todo${t.done ? " done" : ""}" data-todo="${esc(t.id)}">
      <div class="lhead"><b class="title">${esc(t.item_name || t.title)}</b>
        <span class="chip ${t.done ? "eladva" : "kerdes"}">${t.done ? "Kész" : "Feladásra vár"}</span></div>
      ${t.code ? `<div class="tcode"><small>Feladási kód</small><b>${esc(t.code)}</b>
          <button type="button" class="small" data-todo-copy="${esc(t.code)}">Másolás</button></div>`
        : `<p class="warn">A feladási kódot nem tudtam kiolvasni: nézd meg ${shot ? "a címke képén" : "a Vinteden"}${link && shot ? " vagy a beszélgetésben" : ""}.</p>`}
      ${t.place ? `<p class="tplace"><small>Hol</small> ${esc(t.place)}</p>` : ""}
      <small class="muted">${facts.filter(Boolean).join(" · ")}</small>
      ${t.text && !t.code ? `<details><summary>Amit a címke oldalán láttam</summary><p class="ttext">${esc(t.text)}</p></details>` : ""}
      <div class="row">
        <button type="button" class="small${t.done ? "" : " primary"}" data-todo-done="${esc(t.id)}" data-done="${t.done ? "0" : "1"}">${t.done ? "Mégsem kész" : "Kész, feladtam"}</button>
        ${t.done ? `<button type="button" class="small quiet" data-todo-remove="${esc(t.id)}">Törlés</button>` : ""}
      </div></article>`;
  }

  function todosBox(L) {
    const todos = L.todos || [];
    const waiting = todos.filter((t) => !t.done);
    const done = todos.filter((t) => t.done);
    return `<section id="l-todos" class="todos" aria-labelledby="l-todos-title">
      <h2 id="l-todos-title" class="section-title">Teendőim${waiting.length ? `<span class="count">${waiting.length}</span>` : ""}</h2>
      ${waiting.map(todoCard).join("") || `<p class="muted">Nincs elvégzendő. Ha a Vinteden eladsz valamit, a csomag feladási
        kódját és helyét ide írom.</p>`}
      ${done.length ? `<details class="setup"><summary>Elvégezve (${done.length})</summary>${done.slice(0, 20).map(todoCard).join("")}</details>` : ""}
    </section>`;
  }

  // A havi keret (mennyi ment el, mennyi maradt), a figyelmeztetések, és a beállításuk.
  function moneyBox(M) {
    const lim = M.limits || {};
    const used = M.budget ? Math.min(100, Math.round(M.spent * 100 / M.budget)) : 0;
    const bar = M.budget ? `<div class="budget"><div class="row"><b>Havi keret</b><span>${esc(price(M.spent) || "0 Ft")} / ${esc(price(M.budget))}</span>
        <small class="muted">${M.remaining ? `${esc(price(M.remaining))} maradt` : "elfogyott"}</small></div>
        <span class="mbar"><i style="width: ${used}%"${used >= 100 ? ' class="loss"' : ""}></i></span></div>` : "";
    const num = (id, label, value, min) => `<div class="field"><label for="${id}">${label}</label>
      <input id="${id}" data-keep="${id}" type="number" inputmode="numeric" min="${min}" placeholder="nincs" value="${esc(value ?? "")}"></div>`;
    return `${bar}${(M.warnings || []).map((w) => `<p class="warn">${esc(w)}</p>`).join("")}
      <details class="setup"><summary>Keret és figyelmeztetés</summary>
        <div class="fields">
          ${num("lm-budget", "Havi keret (Ft)", lim.budget, 0)}
          ${num("lm-count", "Szóljon, ha ennél több tárgy vár eladásra", lim.stock_count, 1)}
          ${num("lm-value", "… vagy ennél több pénz van bennük (Ft)", lim.stock_value, 0)}
        </div>
        <p class="muted">A havi keretbe az ebben a hónapban vett tárgyak vételára, postája és egyéb költsége számít. Ha
          fogy, csak a maradékba beleférő ajánlatokról szólok; amikor a 80%-a elment, és amikor elfogyott, szólok. Ha túl sok
          megvett tárgy vár eladásra, naponta egyszer szólok (és rögtön, amikor egy újabb vételt felveszel).</p>
        <div class="row"><button type="button" class="small" id="lm-save">Mentés</button></div></details>`;
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
    box.innerHTML = `${todosBox(L)}
      <h2 class="section-title">Haszon</h2>
      <div class="tiles">
        <div class="tile"><small>Ebben a hónapban</small><b>${esc(signed(L.month.profit))}</b><small>${L.month.count} eladás</small></div>
        <div class="tile"><small>Összesen</small><b>${esc(signed(L.total.profit))}</b>
          <small>${L.total.count} eladás${L.total.own ? ` (${L.total.own} saját holmi)` : ""}${L.total.roi !== null ? ` · ${L.total.roi}%` : ""}${L.days !== null ? ` · átl. ${L.days} nap` : ""}</small></div>
        <div class="tile"><small>Bent van</small><b>${esc(price(L.open.spent) || "0 Ft")}</b>
          <small>${L.open.count} tárgy${counts["úton"] ? ` · ${counts["úton"]} úton` : ""}</small></div>
        <div class="tile"><small>Várható haszon</small><b>${esc(signed(L.open.expected))}</b>
          <small>${L.open.unknown ? `${L.open.unknown} tárgynál nincs ár` : "ha a tervezett áron elkelnek"}</small></div>
      </div>
      ${moneyBox(L.money || {})}
      ${monthRows ? `<details class="setup"><summary>Havonta</summary><ul class="months">${monthRows}</ul></details>` : ""}
      <div class="row lbar">
        <div class="filters" role="group" aria-label="Szűrés">${LEDGER_FILTERS.map(([key, label]) => {
          const n = key === "all" ? L.rows.length : (counts[key] || 0);
          return `<button type="button" data-lfilter="${key}" aria-pressed="${ledgerFilter === key}">${label}<span class="count">${n}</span></button>`;
        }).join("")}</div>
        <button type="button" class="small${ledgerAdding ? "" : " primary"}" id="l-add-toggle">${ledgerAdding ? "Mégse" : "+ Új vétel"}</button>
      </div>
      ${ledgerAdding ? `<form id="l-add" class="compose" autocomplete="off">
        <input id="la-name" data-keep="la-name" placeholder="Mit vettél? Pl. RTX 3060 Ti, Nike Air Max 90">
        <div class="fields">
          <div class="field"><label for="la-price">Vételár (Ft)</label><input id="la-price" data-keep="la-price" type="number" inputmode="numeric" min="0"></div>
          <div class="field"><label for="la-ship">Posta (Ft)</label><input id="la-ship" data-keep="la-ship" type="number" inputmode="numeric" min="0" placeholder="0"></div>
          <div class="field"><label for="la-from">Honnan</label><input id="la-from" data-keep="la-from" list="la-sites" placeholder="pl. Jófogás, bolhapiac"></div>
          <div class="field"><label for="la-target">Mennyiért adnád tovább (Ft)</label><input id="la-target" data-keep="la-target" type="number" inputmode="numeric" min="0"></div>
        </div>
        <datalist id="la-sites">${sites}</datalist>
        <label class="check"><input id="la-transit" type="checkbox"> Még úton van (postán jön)</label>
        <div class="row"><button type="submit" class="primary">Felveszem</button></div>
      </form>` : ""}
      <div class="ledger-list">${rows.map(ledgerCard).join("") || `<p class="muted">${L.rows.length ? "Ebben a csoportban nincs tárgy."
        : "Még nincs megvett vagy eladott tárgy. Az Automata kereskedés találatainál a Megvettem gombbal, vagy itt az Új vétellel veheted fel; bármelyik tárgy lapján a Vételár is ide teszi. Amit a Jarvis eladott, az is ide kerül."}</p>`}</div>
      <p class="muted">A haszon: az eladási ár, mínusz az eladás költségei (posta, díj), mínusz amibe került (vételár, posta,
        egyéb). A saját holmid (amit nem vettél, pl. a ruháid), amit a Jarvis töltött fel, és elkelt, szintén ide kerül:
        ott az eladási ár a haszon (a megtérülésbe nem számít). Amíg nem kelt el, a várható haszon a tervezett eladási
        árral (ha nincs, a hirdetési árral) számol. A „most kb.” ár az árbázisból jön: ennyiért hirdetik most a
        hasonlókat.</p>`;
    for (const b of box.querySelectorAll("[data-lfilter]")) b.addEventListener("click", () => { ledgerFilter = b.dataset.lfilter; renderLedger(); });
    for (const b of box.querySelectorAll("[data-todo-copy]")) b.addEventListener("click", () => copyText(b.dataset.todoCopy, "Kimásoltam a kódot."));
    for (const b of box.querySelectorAll("[data-todo-done]")) b.addEventListener("click", async () => {
      const done = b.dataset.done === "1";
      if (await ledgerAction({ action: "todo_done", id: b.dataset.todoDone, done })) J.toast(done ? "Kész: elvégezve." : "Visszatettem a teendők közé.");
    });
    for (const b of box.querySelectorAll("[data-todo-remove]")) b.addEventListener("click", async () => {
      if (await ask("Teendő törlése", "Törlöd ezt az elvégzett teendőt?", "Törlés")) await ledgerAction({ action: "todo_remove", id: b.dataset.todoRemove });
    });
    $("lm-save").addEventListener("click", async () => {
      if (await ledgerAction({ action: "money", budget: $("lm-budget").value, stock_count: $("lm-count").value,
                               stock_value: $("lm-value").value })) J.toast("Mentve.");
    });
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
    for (const b of box.querySelectorAll("[data-cancel-bought]")) b.addEventListener("click", () => cancelBought(b.dataset.cancelBought, b.dataset.name));
    for (const form of box.querySelectorAll("[data-lsave]")) form.addEventListener("submit", async (e) => {
      e.preventDefault();
      // csak amit átírtál: ami közben máshol (pl. a telefonon) változott, azt nem írja vissza a régire
      const fields = {};
      for (const input of form.querySelectorAll("[data-lfield]")) {
        const name = input.dataset.lfield;
        if (input.type === "checkbox") {
          if (input.checked !== input.defaultChecked) fields[name] = input.checked;
        } else if (input.type === "date") {
          // csak ha átírtad a napot (különben a napon belüli időpont maradna el)
          if (input.value !== input.dataset.day) fields[name] = input.value ? new Date(`${input.value}T12:00:00`).getTime() / 1000 : 0;
        } else if (typedValue(input)) fields[name] = input.value;
      }
      const bought = form.querySelector('[data-lfield="bought_price"]');
      if (bought && !bought.value.trim()) { J.toast("A vételár kell (különben kikerül az Elemzésből)."); return; }
      if (!Object.keys(fields).length) { ledgerOpen = null; renderLedger(); return; }    // nem írtál át semmit
      if (await ledgerAction({ action: "update", id: form.dataset.lsave, fields })) { ledgerOpen = null; renderLedger(); J.toast("Mentve."); }
    });
  }

  function render() {
    foldCompose();
    $("w-deals").hidden = job !== "deals";
    $("w-ledger").hidden = job !== "ledger";
    $("w-filters").hidden = job !== "sales";
    if (job === "deals") {
      $("w-msgs").hidden = $("w-list-view").hidden = true;
      leaveDetail();
      // gépelés közben nem rajzolja újra (egy üzenet szövegében sem); máskor igen, de a beírt, még nem mentett érték marad
      if (!$("w-deals").contains(document.activeElement) || !document.activeElement.matches("input, textarea, select")) {
        keepTyping($("w-deals"), renderDeals);
      }
      return;
    }
    if (job === "ledger") {
      $("w-msgs").hidden = $("w-list-view").hidden = true;
      leaveDetail();
      // gépelés közben nem rajzolja újra; máskor igen, de a beírt, még nem mentett érték marad
      if (!$("w-ledger").contains(document.activeElement) || !document.activeElement.matches("input, textarea, select")) {
        keepTyping($("w-ledger"), renderLedger);
      }
      return;
    }
    renderFilters();
    $("w-msgs").hidden = filter !== "msgs";
    if (filter === "msgs") {
      openId = null;
      $("w-list-view").hidden = true;
      leaveDetail();
      if (convOpen && !convs.has(convOpen)) convOpen = null;
      keepTyping($("w-msgs"), renderMessages);
      return;
    }
    if (openId && items.has(openId)) {
      $("w-list-view").hidden = true;
      showDetail(items.get(openId));
    } else {
      openId = null;
      leaveDetail();
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
    $("w-list").innerHTML = (filter === "sold" && list.length ? soldSummary(list) : "") + (list.map(card).join("") ||
      `<p class="muted">${items.size ? "Nincs ilyen hirdetés." : "Még nincs hirdetésed. Fent kezdheted."}</p>`);
  }

  // Az Eladva szűrő tetején: hány eladás, mennyi a bevétel és a haszon (a saját holmid eladási ára is haszon; amit
  // megvettél, annál a bekerülés levonódik), összesen és ebben a hónapban; a részletek az Elemzés fülön.
  function soldSummary(list) {
    const now = new Date();
    let count = 0, revenue = 0, profit = 0, month = 0;
    for (const item of list) {
      if (item.sold_price === null || item.sold_price === undefined) continue;
      const cost = (item.bought_price || 0) + (item.bought_shipping || 0) + (item.bought_extra || 0);
      const gain = item.sold_price - (item.sell_shipping || 0) - (item.sell_fee || 0) - cost;
      const when = item.sold_at ? new Date(item.sold_at * 1000) : null;
      count += 1;
      revenue += item.sold_price;
      profit += gain;
      if (when && when.getFullYear() === now.getFullYear() && when.getMonth() === now.getMonth()) month += gain;
    }
    return `<div class="soldsum" role="status"><span><b>${count}</b> eladás · <b>${esc(price(revenue) || "0 Ft")}</b> bevétel ·
      haszon <b>${esc(signed(profit))}</b> · ebben a hónapban <b>${esc(signed(month))}</b></span>
      <button type="button" class="small" data-job="ledger">Elemzés</button></div>`;
  }
  const soldDay = (item) => (item.sold_at ? new Date(item.sold_at * 1000).toLocaleDateString("hu-HU") : "");

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
    if (p.kind === "review") {     // Pro: kész az adatlap az árral
      return `<div class="ask"><p>Kész az adatlap az árral (${esc(price(item.price))}): nézd át, és ha jó, mehet a feltöltés.</p>
        <div class="row"><button type="button" class="small primary" data-review="${item.id}">Átnézem</button>
        <button type="button" class="small quiet" data-cancel="${item.id}">Mégse</button></div></div>`;
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
    const facts = (item.sold ? [`eladva ${price(item.sold_price ?? item.price)}`.trim(), soldDay(item)]
      : [price(item.price), online(item)]).filter(Boolean).join(" · ");
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

  // A tárgy lapjának egy mezője; a data-keep kulcsa a tárgyhoz kötött (keepTyping).
  function field(item, name, label, value, extra = "") {
    return `<div class="field"><label for="f-${name}">${label}</label>` +
           `<input id="f-${name}" data-field="${name}" data-keep="f-${esc(item.id)}-${name}" value="${esc(value)}" ${extra}></div>`;
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

  // Automatikus frissítés (refresh.py): pár naponta újra a friss hirdetések közé kerül. A Vinteden levétel és
  // újrafeltöltés (felrakásonként a másik képsorral), a Hardveraprón az ingyenes UP; a Jófogáson a szabályzata miatt nem.
  const REFRESH_HOW = { vinted: "a Vinteden leveszem és újra feltöltöm (a kedvelései és megtekintései ezzel elvesznek)",
    hardverapro: "a Hardveraprón az ingyenes UP gombbal előre hozom" };
  const REFRESH_NOT = { jofogas: "a Jófogáson nem: a szabályzata szerint törlés után 14 napig nem adható fel újra",
    vatera: "a Vaterán nem" };
  const REFRESH_DAYS = [1, 2, 3, 4, 5, 6, 7, 10, 14, 21, 30];

  function refreshBlock(item) {
    const r = item.refresh || {};
    const days = r.days || 7;
    const hour = r.hour ?? 19;
    const how = item.targets.map((k) => REFRESH_HOW[k] || REFRESH_NOT[k]).filter(Boolean);
    const can = item.targets.some((k) => REFRESH_HOW[k]);
    const live = item.targets.some((k) => REFRESH_HOW[k] && siteState(item, k) === POSTED) || (r.repost || []).length;
    const alt = item.photos_alt || [];
    const repost = (r.repost || []).map((k) => (platforms.find((p) => p.key === k) || { name: k }).name);
    const status = [
      r.on && item.refresh_next ? `Legközelebb ${item.refresh_next} (amikor épp szabad a böngésző).` : "",
      repost.length ? `Az újrafeltöltés még hátravan: ${repost.join(", ")}.` : "",
      item.refresh_last ? `Utoljára: ${item.refresh_last}.` : "",
      r.note ? r.note + "." : "",
      !r.on && (r.fails || 0) >= 3 ? "Kikapcsoltam, mert többször egymás után nem sikerült." : "",
    ].filter(Boolean).join(" ");
    const options = [...new Set([...REFRESH_DAYS, days])].sort((a, b) => a - b);
    return `<section aria-labelledby="h-refresh" class="refresh">
        <h3 id="h-refresh">Frissítés</h3>
        <p class="muted">Pár naponta újra a friss hirdetések közé teszem, mert azokat nézik a legtöbben${how.length ? ": " + esc(how.join("; ")) : ""}.</p>
        ${can ? `<div class="row">
          <label class="check"><input type="checkbox" id="r-on" data-refresh${r.on ? " checked" : ""}> Automatikusan</label>
          <label>Hány naponta <select id="r-days" data-refresh>${options.map((d) => `<option value="${d}"${d === days ? " selected" : ""}>${d === 1 ? "naponta" : d + " naponta"}</option>`).join("")}</select></label>
          <label>Hány óra körül <select id="r-hour" data-refresh>${[...Array(24).keys()].map((h) => `<option value="${h}"${h === hour ? " selected" : ""}>${h} óra</option>`).join("")}</select></label>
          ${browserOn && live ? '<button type="button" class="small" data-action="refreshnow" title="Most rögtön, nem várja meg az időpontot">Most frissítsd</button>' : ""}
        </div>
        ${status ? `<p class="muted" id="r-status">${esc(status)}</p>` : ""}
        <p class="muted">A Vinteden felrakásonként váltom a két képsort, így az újra feltöltött hirdetés újnak is látszik
          (más szögből, más háttér előtt készült fotók a legjobbak; a túl gyakori, egyforma képes újrafeltöltést a Vinted
          kiszűrheti, hetente-kéthetente a biztonságos).</p>
        <h4>1. képsor (az alap)${r.set === 1 && alt.length ? "" : " · most ez van fent"}</h4>
        <div class="photos mini">${item.photos.map((name, i) => `<div class="photo"><img src="${photoUrl(item.id, esc(name), true)}" alt="Az alap ${i + 1}. fotója"></div>`).join("") || '<p class="muted">Még nincs fotó (fent, a Fotóknál adhatsz hozzá).</p>'}</div>
        <h4>2. képsor${r.set === 1 && alt.length ? " · most ez van fent" : ""}</h4>
        <div class="photos">
          ${alt.map((name, i) => `<div class="photo${i === 0 ? " first" : ""}"><img src="${photoUrl(item.id, esc(name), true)}" alt="A 2. képsor ${i + 1}. fotója">
            ${i === 0 ? '<span class="tag">borító</span>' : ""}
            <div class="tools"><span></span><button type="button" data-action="removephoto" data-name="${esc(name)}" aria-label="A 2. képsor ${i + 1}. fotójának törlése">✕</button></div></div>`).join("")}
          ${alt.length < maxPhotos ? '<button type="button" class="addphoto" data-action="pickalt">+ Fotó<br>a 2. képsorba</button>' : ""}
        </div>` : ""}
      </section>`;
  }

  function renderDetail(item) {
    const busy = uploading.get(item.id) || item.busy;
    $("w-detail").dataset.item = item.id;
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
      ${refreshBlock(item)}
      <section aria-labelledby="h-listing">
        <h3 id="h-listing">A hirdetés</h3>
        <p class="muted">A leírásod: ${esc(item.prompt) || "–"}</p>
        <div class="fields">
          <div class="field wide"><label for="f-title">Cím</label>
            <input id="f-title" data-field="title" data-keep="f-${esc(item.id)}-title" value="${esc(item.title)}" maxlength="100"></div>
          <div class="field wide"><label for="f-description">Leírás</label>
            <textarea id="f-description" data-field="description" data-keep="f-${esc(item.id)}-description" rows="4">${esc(item.description)}</textarea></div>
          ${field(item, "price", "Ár (Ft)", item.price ?? "", 'inputmode="numeric"')}
          ${messagesOn ? field(item, "min_price", "Alkunál legalább (Ft)", item.min_price ?? "",
            `inputmode="numeric" placeholder="${esc(price(item.price) || "az ár")}"`) : ""}
          ${field(item, "bought_price", "Vételár (Ft)", item.bought_price ?? "", 'inputmode="numeric" placeholder="ha vetted"')}
          ${item.sold ? field(item, "sold_price", "Eladási ár (Ft)", item.sold_price ?? "", 'inputmode="numeric"') : ""}
          ${item.sold && item.bought_price != null && item.sold_price != null
            ? `<div class="field"><label>Haszon</label><b>${esc(signed(item.sold_price - (item.sell_shipping || 0) - (item.sell_fee || 0)
                - item.bought_price - (item.bought_shipping || 0) - (item.bought_extra || 0)))}</b></div>` : ""}
          ${item.bought_price != null ? `<div class="field"><label>Vétel részletei</label>
            <button type="button" class="small" data-action="ledger">Elemzés fül</button></div>` : ""}
          ${conditionField(item.condition)}
          ${field(item, "brand", "Márka", item.brand)}
          ${field(item, "size", "Méret", item.size)}
          ${field(item, "color", "Szín", item.color)}
          ${field(item, "category", "Kategória", item.category)}
          ${field(item, "location", "Átvétel helye", item.location, `placeholder="${esc(settings.city || "az Alapadatokból")}"`)}
          ${field(item, "shipping", "Szállítás", item.shipping, `placeholder="${esc(settings.shipping || "az Alapadatokból")}"`)}
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
        <textarea id="m-draft" data-draft-for="${esc(conv.id)}" data-keep="draft-${esc(conv.id)}-${conv.draft_at || 0}" rows="4" aria-label="Válasz a vevőnek"
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
    if (wanted.action === "item" || wanted.action === "review") {   // egy tárgy kérdése, vagy a Pro Nézd át ablaka
      if (!items.has(wanted.item)) await load();
      const item = items.get(wanted.item);
      if (!item) { J.toast("Ez a hirdetés már nincs meg."); return; }
      openId = item.id;
      setJob("sales");
      area.scrollTop = 0;
      if (wanted.action === "review") openReview(item.id);
      return;
    }
    if (wanted.action === "deal") { await openDeal(wanted.deal); return; }   // egy megérő hirdetés: a találat ablaka
    if (wanted.action === "ledger") { setJob("ledger"); return; }   // a havi keret vagy a felhalmozódott tárgyak
    if (wanted.action === "todos") {        // egy eladás feladási kódja, vagy az esti emlékeztető: a Teendőim
      setJob("ledger");
      area.scrollTop = 0;
      return;
    }
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
    const id = event.target.id === "m-draft" && event.target.dataset.draftFor;   // a vázlat a saját beszélgetéséé
    if (!id) return;
    const result = await J.postJson("/api/work/messages/save", { id, draft: event.target.value });
    if (result.ok) convs.set(result.conversation.id, result.conversation);
  });

  async function onCardsClick(event) {
    const goto = event.target.closest("[data-job]");     // az Eladva összesítőjének gombja: az Elemzés fül
    if (goto) { setJob(goto.dataset.job); return; }
    const open = event.target.closest("[data-open]");
    if (open) {
      openId = open.dataset.open;
      render();
      area.scrollTop = 0;
      return;
    }
    const review = event.target.closest("[data-review]");
    if (review) { openReview(review.dataset.review); return; }
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
    const item = items.get($("w-detail").dataset.item);    // a lap tárgya (a gomb is az övé)
    if (!item) return;
    if (event.target.closest("[data-cancel]") || event.target.closest("[data-price]") || event.target.closest("[data-review]")) {
      onCardsClick(event);
      return;
    }
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const name = button.dataset.action;
    if (name === "back") { driftNotes.delete(item.id); openId = null; render(); }
    else if (name === "pickphotos") { pickFor = item.id; $("w-file").click(); }
    else if (name === "pickalt") { pickFor = "alt:" + item.id; $("w-file").click(); }
    else if (name === "refreshnow") {
      if (await action("refresh", item.id)) J.toast("Rendben: amint szabad a böngésző, frissítem.");
    }
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
    const item = items.get($("w-detail").dataset.item);    // a lap tárgya: a mező is az övé
    if (!item) return;
    if (el.dataset.target) changeTargets(item, el.dataset.target, el.checked);
    else if (el.dataset.field) saveField(item.id, el);
    else if (el.dataset.refresh !== undefined) saveRefresh(item);
  });

  async function saveRefresh(item) {
    const box = $("w-detail");
    const result = await J.postJson("/api/work/refresh", { id: item.id, on: box.querySelector("#r-on").checked,
      days: box.querySelector("#r-days").value, hour: box.querySelector("#r-hour").value });
    if (remember(result, "Nem sikerült menteni.") && result.item.refresh.on && result.item.refresh_next)
      J.toast(`Rendben: legközelebb ${result.item.refresh_next} frissítem.`);
  }

  // A fájlválasztó a lapon kívül van, így akkor sem vész el, ha közben frissül a lap.
  $("w-file").addEventListener("change", () => {
    const files = [...$("w-file").files];
    $("w-file").value = "";
    const target = pickFor;
    pickFor = null;
    if (!files.length) return;
    if (target === "compose") addComposePhotos(files);
    else if (target && target.startsWith("alt:") && items.has(target.slice(4))) upload(target.slice(4), files, true);
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
    const shown = detailItem();         // csak a látható lap tárgyához (nem a háttérben nyitva maradthoz)
    if (shown && items.has(shown)) upload(shown, files);
    else if (!$("w-list-view").hidden) addComposePhotos(files);
    else J.toast("Az Adás fülön húzd a fotót egy hirdetés lapjára, vagy az új hirdetéshez.");
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
  // Egy másik ablakból (pl. a telefonról) elindítottad, leállítottad vagy átírtad: itt is frissül.
  J.on("work_deals", () => { if (job === "deals" || J.mode === "chat") loadDeals(); });
  // A Teendőim változott (egy eladás feladási kódja, vagy egy másik ablakban késznek jelölted): a fül felirata, és ha
  // látszik, az Elemzés is frissül.
  J.on("work_todos", (event) => {
    todosOpen = event.open || 0;
    ledgerTab();
    if (job === "ledger") loadLedger();
  });
  // Amíg a fül látszik, félpercenként frissül az állapot („Most: Hardverapró…”), és ha a gép újraindult.
  setInterval(() => { if (job === "deals" && !document.hidden) loadDeals(); }, 30000);
  J.onHello((event) => {
    if (event.work) { load(); loadDeals(); }
    else { $("home-tiles").hidden = true; $("empty-hint").textContent = "Kérdezz bármit."; }
  });
})();
