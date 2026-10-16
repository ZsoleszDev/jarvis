"use strict";
// Kreatív: közös történetmesélés (Kaland, mint az AI Dungeon) és beszélgetés egy karakterrel (mint a
// Character.AI). A történetek és az emlékezetük a Jarvisnál vannak; ez az oldal megjeleníti őket, élőben
// mutatja az írást, és elküldi a köreidet (Jarvis.* az app.js-ből).
(() => {
  const J = window.Jarvis;
  const { $ } = J;
  const withToken = (path) => path + (path.includes("?") ? "&" : "?") + "t=" + encodeURIComponent(J.token);

  let stories = [];
  let templates = { adventure: [], character: [], default_name: "Alex" };
  let defaultLang = "en";
  let story = null;                 // a megnyitott történet
  let view = "library";             // library | create | reader
  let kind = "do";                  // Tett, Mondás vagy Történet
  let streaming = null;             // { entry, retry, text }: amit épp ír
  let editing = null;               // { id, draft }: amit épp szerkesztesz
  let memoryNew = null;             // az új emlék piszkozata (null = nincs nyitva)
  let lastStats = "";
  let loaded = false;
  const create = { mode: "adventure", lang: "en", template: "fantasy", choice: "", fields: {} };

  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const store = (key, value) => { try { localStorage.setItem(key, value); } catch { /* nem baj */ } };
  const recall = (key) => { try { return localStorage.getItem(key); } catch { return null; } };
  const num = (value) => Math.round(Number(value) || 0).toLocaleString("hu-HU");
  const date = (t) => (t ? new Date(t * 1000).toLocaleString("hu-HU", { month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit" }) : "");
  const nearBottom = (el) => el.scrollHeight - el.scrollTop - el.clientHeight < 120;
  const busy = () => !!(story && story.status && story.status.busy);
  const charName = () => (story && story.character.name) || "?";
  const userName = () => (story && story.persona.name) || templates.default_name;

  const PLACEHOLDER = {
    en: { do: "What do you do?", say: "What do you say?", story: "Write what happens next…", user: "Message {name}…" },
    hu: { do: "Mit teszel?", say: "Mit mondasz?", story: "Írd le, mi történik…", user: "Üzenet {name} számára…" },
  };

  // --- adatok -----------------------------------------------------------------------------------

  async function getJson(path) {
    try {
      const response = await fetch(withToken(path));
      return await response.json();
    } catch {
      return { ok: false, error: "Nincs kapcsolat a Jarvisszal." };
    }
  }

  async function loadLibrary() {
    const data = await getJson("/api/creative/stories");
    if (!data.ok) return;
    stories = data.stories || [];
    templates = data.templates || templates;
    defaultLang = data.language || "en";
    if (!loaded) {
      loaded = true;
      create.lang = recall("jarvis-creative-lang") || defaultLang;
      const last = recall("jarvis-creative-story");
      if (last && stories.some((s) => s.id === last)) { await openStory(last); return; }
    } else if (story) {
      const fresh = await getJson("/api/creative/story?id=" + story.id);   // újracsatlakozás után
      if (fresh.ok) { applyStory(fresh.story); return; }
      story = null;
    }
    show(story ? "reader" : view === "create" ? "create" : "library");
  }

  async function openStory(id) {
    const data = await getJson("/api/creative/story?id=" + encodeURIComponent(id));
    if (!data.ok) { J.toast(data.error || "Nem sikerült megnyitni."); show("library"); return; }
    store("jarvis-creative-story", id);
    story = null;
    show("reader");                 // előbb látsszon, hogy a végére lehessen görgetni
    applyStory(data.story, true);
    $("c-text").focus();
  }

  function applyStory(next, toBottom = false) {
    const same = story && story.id === next.id;
    // A kérésre adott válasz lassabban is megérkezhet, mint a közben jött események: a régebbi ne írja felül
    // se a történetet (rev), se az állapotát (status_seq: épp ír, sorra vár).
    if (same) {
      if ((next.rev || 0) < (story.rev || 0)) return;
      if ((next.status_seq || 0) < (story.status_seq || 0)) {
        next.status = story.status;
        next.status_seq = story.status_seq;
      }
    }
    story = next;
    if (!same) {
      editing = null;
      streaming = null;
      memoryNew = null;
      lastStats = "";
      buildMemory();
    }
    renderHeader();
    renderStory(toBottom || !same);
    fillMemory();
    updateControls();
  }

  async function post(path, body, fallback = "Nem sikerült.") {
    const result = await J.postJson(path, body);
    if (!result.ok) { J.toast(result.error || fallback); return null; }
    if (result.story && story && result.story.id === story.id) applyStory(result.story);
    return result;
  }

  // A modell kell hozzá: ha a Jarvis szünetel, előbb megkérdezi; készenlétből magától felébred.
  async function ensureAwake() {
    if (J.paused && !J.standby) {
      const go = await J.ask("Szünetelek", "Minden modell le van állítva. Folytassam (betöltöm őket, 10–20 " +
        "másodperc), és utána írok?", "Folytatás");
      if (!go) return false;
      await J.post("/api/resume");
    } else if (J.standby) {
      J.toast("Felébredek, és írok (10–20 másodperc)…");
    }
    return true;
  }

  // --- nézetek ----------------------------------------------------------------------------------

  function show(next) {
    view = next;
    $("c-library").hidden = next !== "library";
    $("c-create").hidden = next !== "create";
    $("c-reader").hidden = next !== "reader";
    if (next === "library") { story = null; store("jarvis-creative-story", ""); renderLibrary(); }
    if (next === "create") renderCreate();
  }

  function renderLibrary() {
    const list = $("c-list");
    if (!stories.length) {
      list.innerHTML = `<p class="c-empty">Még nincs történeted. Kezdj egyet: válassz egy kiindulópontot,
        vagy írd le pár szóban az ötletedet, és Jarvis megírja az elejét.</p>`;
      return;
    }
    list.innerHTML = stories.map((s) => `
      <button type="button" class="c-book" data-open="${esc(s.id)}">
        <span class="c-book-title">${esc(s.title)}</span>
        <span class="c-book-meta">${s.mode === "character" ? "Karakter" : "Kaland"} · ${s.language === "hu" ? "magyar" : "angol"}
          · ${esc(date(s.updated))}</span>
        ${s.snippet ? `<span class="c-book-snippet">${esc(s.snippet)}</span>` : ""}
      </button>`).join("");
  }

  // --- új történet ------------------------------------------------------------------------------

  function chosenTemplate() {
    return (templates[create.mode] || []).find((t) => t.key === create.template) || null;
  }

  function field(name, label, { area = false, rows = 3, placeholder = "", value = "" } = {}) {
    const current = create.fields[name] ?? value;
    const control = area
      ? `<textarea id="cf-${name}" data-cf="${name}" rows="${rows}" placeholder="${esc(placeholder)}">${esc(current)}</textarea>`
      : `<input id="cf-${name}" data-cf="${name}" placeholder="${esc(placeholder)}" value="${esc(current)}">`;
    return `<div class="field"><label for="cf-${name}">${esc(label)}</label>${control}</div>`;
  }

  function renderCreate() {
    const adv = create.mode === "adventure";
    const list = templates[create.mode] || [];
    if (!["idea", "custom"].includes(create.template) && !list.some((t) => t.key === create.template)) {
      create.template = list.length ? list[0].key : "custom";
    }
    const tpl = chosenTemplate();
    const cards = list.map((t) => `
      <button type="button" class="c-tpl" data-tpl="${esc(t.key)}" aria-pressed="${create.template === t.key}">
        <strong>${esc(t.label)}</strong><span>${esc(t.blurb)}</span><small>${esc(t.titles[create.lang] || "")}</small>
      </button>`).join("") + `
      <button type="button" class="c-tpl" data-tpl="idea" aria-pressed="${create.template === "idea"}">
        <strong>Ötletből</strong><span>Írd le pár szóban, miről szóljon, és Jarvis megtervezi
        ${adv ? "a világot, a szereplőket és a kezdést" : "a karaktert és az első üzenetét"}.</span></button>
      <button type="button" class="c-tpl" data-tpl="custom" aria-pressed="${create.template === "custom"}">
        <strong>Saját</strong><span>${adv ? "Te adod meg a világot és a kezdést." : "Te írod meg a karaktert."}</span>
      </button>`;
    const nameLabel = adv ? "A neved a történetben" : "A te neved";
    let fields = "";
    if (tpl) {
      fields = field("name", nameLabel, { value: templates.default_name });
      if (tpl.choice) {
        const pick = create.choice || tpl.choice.options[0].key;
        fields += `<fieldset class="c-choice"><legend>${esc(tpl.choice.label)}</legend><div class="checks">${
          tpl.choice.options.map((o) => `<label class="check"><input type="radio" name="c-choice" value="${esc(o.key)}"
            ${o.key === pick ? "checked" : ""}> ${esc(o.label)}</label>`).join("")}</div></fieldset>`;
      }
    } else if (create.template === "idea") {
      fields = field("idea", "Miről szóljon?", { area: true, rows: 3, placeholder: adv
        ? "Pl. kalózok egy repülő szigetvilágban, és egy elátkozott térkép"
        : "Pl. egy morcos öreg sárkány, aki egy könyvesboltot üzemeltet" }) + field("name", nameLabel,
        { value: templates.default_name });
    } else if (adv) {
      fields = field("title", "Cím", { placeholder: "Pl. A sólyom árnyéka" }) +
        field("name", nameLabel, { value: templates.default_name }) +
        field("about", "Ki vagy? (nem kötelező)", { area: true, rows: 2, placeholder: "Pl. kiégett zsoldos, aki tartozik egy szívességgel" }) +
        field("plot", "A lényeg: világ, helyzet, szabályok", { area: true, rows: 4,
          placeholder: "Ezt a mesélő mindig látja. Pl. egy esős kikötőváros 1926-ban, ahol…" }) +
        field("opening", "A kezdés (ha üresen hagyod, a mesélő írja meg)", { area: true, rows: 4 });
    } else {
      fields = field("char", "A karakter neve") +
        field("charabout", "Milyen? (személyiség, külső, háttér, beszédmód)", { area: true, rows: 5 }) +
        field("greeting", "Az első üzenete (ha üresen hagyod, ő írja meg)", { area: true, rows: 3,
          placeholder: "A cselekvést csillagok közé: *felnéz a könyvből* Á, egy látogató!" }) +
        field("plot", "A helyzet (nem kötelező)", { area: true, rows: 2, placeholder: "Hol és hogyan találkoztok?" }) +
        field("examples", "Példa párbeszéd (nem kötelező)", { area: true, rows: 3 }) +
        field("name", nameLabel, { value: templates.default_name });
    }
    $("c-create").innerHTML = `
      <div class="c-create-head"><button type="button" class="quiet small" data-act="create-cancel">‹ Történetek</button>
        <h2>Új történet</h2></div>
      <div class="c-seg" role="radiogroup" aria-label="Típus">
        <button type="button" role="radio" data-cmode="adventure" aria-checked="${adv}">
          <strong>Kaland</strong><span>Te döntöd el, mit teszel; a mesélő elmondja, mi történik. Mint az AI Dungeon.</span></button>
        <button type="button" role="radio" data-cmode="character" aria-checked="${!adv}">
          <strong>Karakter</strong><span>Beszélgetés egy szereplővel, aki a saját hangján válaszol. Mint a Character.AI.</span></button>
      </div>
      <fieldset class="c-langs"><legend>Nyelv</legend><div class="checks">
        <label class="check"><input type="radio" name="c-newlang" value="en" ${create.lang === "en" ? "checked" : ""}> English</label>
        <label class="check"><input type="radio" name="c-newlang" value="hu" ${create.lang === "hu" ? "checked" : ""}> Magyar</label>
        <span class="muted">Angolul ír a legjobban a modell; később is átállíthatod.</span></div></fieldset>
      <fieldset class="c-starts"><legend>Kiindulás</legend><div class="c-templates">${cards}</div></fieldset>
      <div class="c-fields">${fields}</div>
      <div class="row"><button type="submit" class="primary">Kezdés</button>
        <button type="button" data-act="create-cancel">Mégse</button>
        <span id="c-create-msg" class="muted" role="status"></span></div>`;
  }

  async function submitCreate() {
    const f = (name) => (create.fields[name] ?? ($("cf-" + name) ? $("cf-" + name).value : "")).trim();
    const choice = document.querySelector('input[name="c-choice"]:checked');
    const payload = {
      mode: create.mode, language: create.lang, template: create.template, choice: choice ? choice.value : "",
      persona: { name: f("name") || templates.default_name, about: f("about") },
      title: f("title"), plot: f("plot"), opening: f("opening"), idea: f("idea"),
      character: { name: f("char"), about: f("charabout"), greeting: f("greeting"), examples: f("examples") },
    };
    const writes = create.template === "idea" || (create.template === "custom" &&
      (create.mode === "adventure" ? !payload.opening : !payload.character.greeting));
    if (writes && !(await ensureAwake())) return;
    $("c-create-msg").textContent = "Elkészítem…";
    const result = await J.postJson("/api/creative/new", payload);
    $("c-create-msg").textContent = "";
    if (!result.ok) { J.toast(result.error || "Nem sikerült."); return; }
    create.fields = {};
    store("jarvis-creative-lang", create.lang);
    store("jarvis-creative-story", result.story.id);
    story = null;
    show("reader");
    applyStory(result.story, true);
    $("c-text").focus();
  }

  $("c-create").addEventListener("click", (event) => {
    const mode = event.target.closest("[data-cmode]");   // (a body-nak is van data-mode-ja)
    const tpl = event.target.closest("[data-tpl]");
    const act = event.target.closest("[data-act]");
    if (mode) { create.mode = mode.dataset.cmode; create.choice = ""; renderCreate(); }
    else if (tpl) { create.template = tpl.dataset.tpl; create.choice = ""; renderCreate(); }
    else if (act && act.dataset.act === "create-cancel") show(story ? "reader" : "library");
  });
  $("c-create").addEventListener("input", (event) => {
    if (event.target.dataset.cf) create.fields[event.target.dataset.cf] = event.target.value;
  });
  $("c-create").addEventListener("change", (event) => {
    if (event.target.name === "c-newlang") { create.lang = event.target.value; renderCreate(); }
    if (event.target.name === "c-choice") create.choice = event.target.value;
  });
  $("c-create").addEventListener("submit", (event) => { event.preventDefault(); submitCreate(); });

  // --- a történet -------------------------------------------------------------------------------

  function renderHeader() {
    $("c-title").textContent = story.title || "Névtelen történet";
    $("c-chips").innerHTML = `<span class="chip">${story.mode === "character" ? "Karakter" : "Kaland"}</span>` +
      `<span class="chip">${story.language === "hu" ? "HU" : "EN"}</span>`;
    $("c-reader").classList.toggle("char", story.mode === "character");
    $("c-kinds").hidden = story.mode !== "adventure";
    $("c-export").href = withToken("/api/creative/export?id=" + story.id);
    setPlaceholder();
  }

  function setPlaceholder() {
    if (!story) return;
    const words = PLACEHOLDER[story.language] || PLACEHOLDER.en;
    const key = story.mode === "character" ? "user" : kind;
    $("c-text").placeholder = words[key].replace("{name}", charName());
  }

  // Bekezdések, a *cselekvés* dőlten (mint a Character.AI-ban).
  function richText(text) {
    return String(text || "").trim().split(/\n{2,}/).map((para) => "<p>" +
      esc(para).replace(/\*([^*\n]+)\*/g, "<em>$1</em>").replace(/\n/g, "<br>") + "</p>").join("");
  }

  function entryHtml(e) {
    const ai = e.kind === "ai";
    const classes = ["c-entry", ai ? "c-ai" : "c-you", "k-" + e.kind, e.mem ? "mem" : ""].join(" ").trim();
    const name = story.mode === "character" ? `<div class="c-name">${esc(ai ? charName() : userName())}</div>` : "";
    let body;
    if (editing && editing.id === e.id) {
      const rows = Math.min(16, Math.max(3, editing.draft.split("\n").length + Math.ceil(editing.draft.length / 90)));
      body = `<textarea class="c-edit" data-edit="${esc(e.id)}" rows="${rows}" aria-label="A rész szövege">${esc(editing.draft)}</textarea>
        <div class="row tight"><button type="button" class="small primary" data-act="edit-save" data-id="${esc(e.id)}">Mentés</button>
        <button type="button" class="small" data-act="edit-cancel">Mégse</button></div>`;
    } else if (e.kind === "do" || e.kind === "say") {
      body = `<div class="c-text turn">${esc(e.text.replace(/^>\s*/, ""))}</div>`;
    } else {
      body = `<div class="c-text">${richText(e.text)}</div>`;
    }
    const tools = [];
    if (ai && e.alts && e.alts.length > 1) {
      tools.push(`<span class="c-alts" aria-label="Változatok"><button type="button" class="small quiet" data-act="alt"
        data-id="${esc(e.id)}" data-index="${e.alt - 1}" ${e.alt === 0 ? "disabled" : ""} aria-label="Előző változat">‹</button>
        <span>${e.alt + 1}/${e.alts.length}</span><button type="button" class="small quiet" data-act="alt" data-id="${esc(e.id)}"
        data-index="${e.alt + 1}" ${e.alt >= e.alts.length - 1 ? "disabled" : ""} aria-label="Következő változat">›</button></span>`);
    }
    if (!(editing && editing.id === e.id)) {
      tools.push(`<button type="button" class="small quiet" data-act="edit" data-id="${esc(e.id)}">Szerkesztés</button>`);
      tools.push(`<button type="button" class="small quiet" data-act="erase" data-id="${esc(e.id)}">Törlés</button>`);
    }
    return `<div class="${classes}" data-id="${esc(e.id)}">${name}${body}<div class="c-tools">${tools.join("")}</div></div>`;
  }

  function streamHtml() {
    const name = story.mode === "character" ? `<div class="c-name">${esc(charName())}</div>` : "";
    return `<div class="c-entry c-ai writing" data-stream>${name}<div class="c-text">${esc(streaming.text.replace(/^\s+/, ""))}</div></div>`;
  }

  function renderStory(toBottom = false) {
    const box = $("c-story");
    const stick = toBottom || nearBottom(box);
    // ha épp egy részt szerkesztesz, az újrarajzolás után is ott maradjon a kurzor
    const active = document.activeElement;
    const caret = active && active.classList.contains("c-edit") ? active.selectionStart : null;
    const html = [];
    story.entries.forEach((e, i) => {
      if (i > 0 && story.entries[i - 1].mem && !e.mem) {
        html.push(`<div class="c-divider" role="separator" title="A fenti részeket a modell már csak az Emlékezetben lévő összefoglalóból ismeri">eddig emlékként</div>`);
      }
      html.push(streaming && streaming.retry && e.id === streaming.entry ? streamHtml() : entryHtml(e));
    });
    if (streaming && !streaming.retry) html.push(streamHtml());
    if (!story.entries.length && !streaming) {
      html.push(`<p class="c-waiting">${busy() ? (story.mode === "character" ? "Mindjárt ír…" : "Írom a kezdést…")
        : "Még üres. Írd meg az első kört, vagy nyomd meg a Folytasd gombot, és a mesélő kezdi."}</p>`);
    }
    box.innerHTML = html.join("");
    if (stick) box.scrollTop = box.scrollHeight;
    const draft = box.querySelector("textarea.c-edit");
    if (draft && caret !== null) {
      draft.focus();
      draft.selectionStart = draft.selectionEnd = Math.min(caret, draft.value.length);
    }
  }

  function updateControls() {
    if (!story) return;
    const working = busy();
    const entries = story.entries;
    const lastAi = entries.length > 0 && entries[entries.length - 1].kind === "ai";
    $("c-send").textContent = working ? "Állj" : "Küldés";
    $("c-send").classList.toggle("stop", working);
    $("c-send").title = working ? "Megállítja az írást (amit addig írt, megmarad)" : "Enter; üresen: Folytasd";
    $("c-continue").disabled = working;
    $("c-retry").disabled = working || !lastAi;
    $("c-undo").disabled = working || !entries.length;
    $("c-redo").disabled = working || !story.redo;
    $("c-reader").classList.toggle("busy", working);
    $("c-story").setAttribute("aria-busy", String(working));
    $("c-status").textContent = working ? (story.status.text || "Írok…") : lastStats;
    for (const el of $("c-memory").querySelectorAll('[data-act="summarize"]')) el.disabled = working;
  }

  async function send(action, extra = {}) {
    if (!story) return null;
    if (!["undo", "redo", "stop"].includes(action) && !(await ensureAwake())) return null;
    return post("/api/creative/act", { id: story.id, action, ...extra });
  }

  $("c-compose").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!story) return;
    if (busy()) { J.post("/api/creative/act", { id: story.id, action: "stop" }); return; }
    const text = $("c-text").value;
    const result = await send(text.trim() ? "send" : "continue",
      { text, kind: story.mode === "adventure" ? kind : "user" });
    if (result) {
      $("c-text").value = "";
      autosize();
      const box = $("c-story");
      box.scrollTop = box.scrollHeight;
    }
  });

  function autosize() {
    const el = $("c-text");
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight + 2, Math.round(window.innerHeight * 0.3)) + "px";
  }
  $("c-text").addEventListener("input", autosize);
  $("c-text").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!busy()) $("c-compose").requestSubmit();
    }
  });

  $("c-kinds").addEventListener("click", (event) => {
    const button = event.target.closest("[data-kind]");
    if (!button) return;
    kind = button.dataset.kind;
    for (const b of $("c-kinds").querySelectorAll("[data-kind]")) b.setAttribute("aria-checked", String(b === button));
    setPlaceholder();
    $("c-text").focus();
  });
  $("c-continue").addEventListener("click", () => send("continue"));
  $("c-retry").addEventListener("click", () => send("retry"));
  $("c-undo").addEventListener("click", () => send("undo"));
  $("c-redo").addEventListener("click", () => send("redo"));
  $("c-back").addEventListener("click", () => show("library"));
  $("c-new").addEventListener("click", () => { create.fields = {}; show("create"); });
  $("c-list").addEventListener("click", (event) => {
    const book = event.target.closest("[data-open]");
    if (book) openStory(book.dataset.open);
  });

  $("c-story").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-act]");
    if (!button || !story) return;
    const id = button.dataset.id;
    const act = button.dataset.act;
    if (act === "alt") {
      post("/api/creative/entry", { id: story.id, entry: id, op: "alt", index: Number(button.dataset.index) });
    } else if (act === "edit") {
      const entry = story.entries.find((e) => e.id === id);
      if (!entry) return;
      editing = { id, draft: entry.text };
      renderStory();
      const draft = $("c-story").querySelector("textarea.c-edit");
      if (draft) { draft.focus(); draft.selectionStart = draft.selectionEnd = draft.value.length; }
    } else if (act === "edit-cancel") {
      editing = null;
      renderStory();
    } else if (act === "edit-save") {
      const draft = editing ? editing.draft : "";
      const result = await post("/api/creative/entry", { id: story.id, entry: id, op: "edit", text: draft });
      if (result) { editing = null; renderStory(); }
    } else if (act === "erase") {
      if (await J.ask("Rész törlése", "Biztosan törlöd ezt a részt a történetből?", "Törlés")) {
        post("/api/creative/entry", { id: story.id, entry: id, op: "delete" });
      }
    }
  });
  $("c-story").addEventListener("input", (event) => {
    if (event.target.dataset.edit && editing) editing.draft = event.target.value;
  });

  // --- emlékezet --------------------------------------------------------------------------------

  function buildMemory() {
    const adv = story.mode === "adventure";
    $("c-memory").innerHTML = `
      <div class="c-mem-head"><h3>Emlékezet</h3>
        <button type="button" class="small quiet" data-act="mem-close">Bezárás</button></div>
      <div id="c-meter" class="c-meter"></div>
      <details class="c-sec" open><summary>${adv ? "Lényeg" : "Helyzet"}</summary>
        <p class="hint">${adv ? "Amit a mesélő mindig lát: a világ, a helyzet, a fontos tények."
          : "Hol és hogyan zajlik a beszélgetés. Ezt mindig látja."}</p>
        <textarea data-field="plot" rows="5" aria-label="${adv ? "Lényeg" : "Helyzet"}"></textarea></details>
      <details class="c-sec"><summary>Szerzői megjegyzés</summary>
        <p class="hint">Stílus és hangulat. Közvetlenül a válasza előtt látja, ezért erősen hat.</p>
        <textarea data-field="note" rows="2" aria-label="Szerzői megjegyzés" placeholder="Pl. Sötét, feszült hangulat, rövid mondatok."></textarea></details>
      <details class="c-sec"><summary>${adv ? "A szereplőd" : "Szereplők"}</summary>${adv ? `
        <div class="field"><label for="cm-pname">Név</label><input id="cm-pname" data-field="persona.name"></div>
        <div class="field"><label for="cm-pabout">Ki vagy?</label><textarea id="cm-pabout" data-field="persona.about" rows="3"></textarea></div>` : `
        <div class="field"><label for="cm-cname">A karakter neve</label><input id="cm-cname" data-field="character.name"></div>
        <div class="field"><label for="cm-cabout">Milyen? (személyiség, külső, háttér, beszédmód)</label>
          <textarea id="cm-cabout" data-field="character.about" rows="7"></textarea></div>
        <div class="field"><label for="cm-cex">Példa párbeszéd</label><textarea id="cm-cex" data-field="character.examples" rows="4"></textarea></div>
        <div class="field"><label for="cm-pname">A te neved</label><input id="cm-pname" data-field="persona.name"></div>
        <div class="field"><label for="cm-pabout">Rólad (amit tudhat rólad)</label><textarea id="cm-pabout" data-field="persona.about" rows="2"></textarea></div>`}
      </details>
      <details class="c-sec"><summary>Világkártyák <span class="count" id="c-cards-count"></span></summary>
        <p class="hint">Szereplők, helyek, tárgyak. Akkor kerül ${adv ? "a mesélő" : "a karakter"} elé, ha a kulcsszava előfordul
          az utolsó részekben; a „Mindig” jelölésűt mindig látja.</p>
        <div id="c-cards" class="c-cards"></div>
        <button type="button" class="small" data-act="card-add">Új kártya</button></details>
      <details class="c-sec" open><summary>Emlékek <span class="count" id="c-mem-count"></span></summary>
        <p class="hint">Ha a történet hosszú lesz, Jarvis összefoglalja a régebbi részeket, és onnantól ezeket látja
          helyettük. Bármelyiket átírhatod; amit törölsz, azt elfelejti.</p>
        <div id="c-summary"></div>
        <div id="c-memories" class="c-memories"></div>
        <div id="c-memory-new"></div>
        <div class="row tight">
          <button type="button" class="small" data-act="summarize">Összefoglalás most</button>
          <button type="button" class="small" data-act="memory-add">Új emlék</button>
          <button type="button" class="small quiet" data-act="memory-clear">Mind törlése</button></div></details>
      <details class="c-sec"><summary>Beállítások</summary>
        <div class="field"><label for="cm-title">Cím</label><input id="cm-title" data-field="title"></div>
        <fieldset class="field"><legend>Nyelv</legend><div class="checks">
          <label class="check"><input type="radio" name="cm-lang" value="en" data-field="language"> English</label>
          <label class="check"><input type="radio" name="cm-lang" value="hu" data-field="language"> Magyar</label></div></fieldset>
        <div class="field"><label for="cm-temp">Kreativitás: <b id="cm-temp-label"></b></label>
          <input id="cm-temp" type="range" min="0.3" max="1.5" step="0.05" data-field="settings.creativity"></div>
        <fieldset class="field"><legend>Válaszhossz</legend><div class="checks">
          <label class="check"><input type="radio" name="cm-len" value="short" data-field="settings.length"> Rövid</label>
          <label class="check"><input type="radio" name="cm-len" value="medium" data-field="settings.length"> Közepes</label>
          <label class="check"><input type="radio" name="cm-len" value="long" data-field="settings.length"> Hosszú</label></div></fieldset>
        <div class="field"><label for="cm-instr">Egyedi utasítások (pl. „Legyen benne több humor”)</label>
          <textarea id="cm-instr" data-field="instructions" rows="3"></textarea></div></details>
      <div class="c-danger"><a id="c-export" class="button small" href="#" download>Letöltés (.md)</a>
        <button type="button" class="small delete" data-act="delete">Történet törlése</button></div>`;
    $("c-export").href = withToken("/api/creative/export?id=" + story.id);
  }

  const valueAt = (path) => path.split(".").reduce((obj, key) => (obj == null ? obj : obj[key]), story);
  const tempWord = (t) => (t < 0.6 ? "visszafogott" : t < 0.85 ? "kiegyensúlyozott" : t < 1.1 ? "kreatív" : "merész");

  function fillMemory() {
    if (!story) return;
    renderMeter();
    renderSummary();
    renderCards();
    renderMemories();
    for (const el of $("c-memory").querySelectorAll("[data-field]")) {
      if (el === document.activeElement || el.dataset.dirty) continue;
      const value = valueAt(el.dataset.field);
      if (el.type === "radio") el.checked = String(value) === el.value;
      else el.value = value ?? "";
    }
    const temp = Number(story.settings.creativity);
    $("cm-temp-label").textContent = `${tempWord(temp)} (${temp.toFixed(2).replace(".", ",")})`;
    $("c-cards-count").textContent = story.cards.length ? `(${story.cards.length})` : "";
    const memories = story.memories.length + (story.summary ? 1 : 0);
    $("c-mem-count").textContent = memories ? `(${memories})` : "";
  }

  function renderMeter() {
    const u = story.usage;
    if (!u) return;
    const pct = (value) => Math.max(0, Math.min(100, (value / u.context) * 100)).toFixed(1);
    const total = u.system + u.memory + u.verbatim;
    $("c-meter").innerHTML = `
      <div class="c-track" role="img" aria-label="A kontextus foglaltsága: ${pct(total)} százalék">
        <span class="seg sys" style="width:${pct(u.system)}%"></span><span class="seg mem" style="width:${pct(u.memory)}%"></span><span class="seg verb" style="width:${pct(u.verbatim)}%"></span>
      </div>
      <p class="hint">A modell most kb. ${num(total)} tokent lát a ${num(u.context)}-ből: utasítások és háttér ${num(u.system)},
        emlékek ${num(u.memory)}, szó szerint ${num(u.verbatim)}. ${u.remembered
          ? `${num(u.remembered)} régebbi rész már emlékként él tovább.`
          : `Ha a szó szerinti rész eléri a kb. ${num(u.limit)}-et, a régebbit összefoglalja.`}</p>`;
  }

  function renderSummary() {
    const box = $("c-summary");
    if (box.contains(document.activeElement)) return;
    box.innerHTML = story.summary ? `<div class="field"><label for="cm-summary">Az eddigi történet (a régi emlékek összesűrítve)</label>
      <textarea id="cm-summary" data-field="summary" rows="6"></textarea></div>` : "";
  }

  function renderCards() {
    const box = $("c-cards");
    if (box.contains(document.activeElement)) return;   // épp szerkeszted: a mentés után frissül
    box.innerHTML = story.cards.map((c) => `
      <div class="c-card" data-card="${esc(c.id)}">
        <div class="row tight"><input class="c-card-name" data-cfield="name" value="${esc(c.name)}" aria-label="A kártya neve" placeholder="Név">
          <label class="check small"><input type="checkbox" data-cfield="always" ${c.always ? "checked" : ""}> Mindig</label>
          <button type="button" class="small quiet" data-act="card-delete" data-card="${esc(c.id)}">Törlés</button></div>
        <input data-cfield="keys" value="${esc((c.keys || []).join(", "))}" aria-label="Kulcsszavak" placeholder="Kulcsszavak, vesszővel elválasztva">
        <textarea data-cfield="text" rows="${Math.min(8, 2 + Math.floor(c.text.length / 70))}" aria-label="Leírás" placeholder="Leírás">${esc(c.text)}</textarea>
      </div>`).join("") || `<p class="muted">Még nincs kártya.</p>`;
  }

  function renderMemories() {
    const box = $("c-memories");
    if (!box.contains(document.activeElement)) {
      box.innerHTML = story.memories.map((m, i) => `
        <div class="c-memo" data-memory="${esc(m.id)}">
          <div class="c-memo-head"><span>${m.auto ? `${i + 1}. emlék${m.count ? ` · ${m.count} részből` : ""}` : "Saját emlék"}</span>
            <button type="button" class="small quiet" data-act="memory-delete" data-memory="${esc(m.id)}">Elfelejt</button></div>
          <textarea data-mfield="text" rows="${Math.min(10, 2 + Math.floor(m.text.length / 70))}" aria-label="Az emlék szövege">${esc(m.text)}</textarea>
        </div>`).join("") || (story.summary ? "" : `<p class="muted">Még nincs emlék. Akkor jön létre, amikor a történet
          már túl hosszú ahhoz, hogy szó szerint lássa, vagy ha te írsz egyet.</p>`);
    }
    const draft = $("c-memory-new");
    if (memoryNew === null) draft.innerHTML = "";
    else if (!draft.firstChild) {
      draft.innerHTML = `<div class="c-memo"><textarea id="cm-newmem" rows="3" placeholder="Pl. Mara megígérte, hogy elvisz a fővárosba."></textarea>
        <div class="row tight"><button type="button" class="small primary" data-act="memory-save">Mentés</button>
        <button type="button" class="small" data-act="memory-cancel">Mégse</button></div></div>`;
      $("cm-newmem").value = memoryNew;
      $("cm-newmem").focus();
    }
  }

  function fieldsFor(path, value) {
    const parts = path.split(".");
    const fields = {};
    let target = fields;
    parts.slice(0, -1).forEach((key) => { target = target[key] = {}; });
    target[parts[parts.length - 1]] = value;
    return fields;
  }

  $("c-memory").addEventListener("input", (event) => {
    const el = event.target;
    if (el.dataset.field) el.dataset.dirty = "1";
    if (el.id === "cm-newmem") memoryNew = el.value;
    if (el.dataset.field === "settings.creativity") {
      $("cm-temp-label").textContent = `${tempWord(Number(el.value))} (${Number(el.value).toFixed(2).replace(".", ",")})`;
    }
  });

  $("c-memory").addEventListener("change", async (event) => {
    const el = event.target;
    if (!story) return;
    if (el.dataset.field) {
      let value = el.type === "range" ? Number(el.value) : el.value;
      if (el.type === "radio" && !el.checked) return;
      const result = await J.postJson("/api/creative/update", { id: story.id, fields: fieldsFor(el.dataset.field, value) });
      delete el.dataset.dirty;
      if (!result.ok) { J.toast(result.error || "Nem sikerült menteni."); return; }
      applyStory(result.story);
      if (el.dataset.field === "language") J.toast(value === "hu" ? "Mostantól magyarul írok." : "From now on I write in English.");
      return;
    }
    const card = el.closest("[data-card]");
    if (card && el.dataset.cfield) {
      const value = el.type === "checkbox" ? el.checked
        : el.dataset.cfield === "keys" ? el.value.split(",").map((k) => k.trim()).filter(Boolean) : el.value;
      await post("/api/creative/card", { id: story.id, op: "edit", card: { id: card.dataset.card, [el.dataset.cfield]: value } });
      return;
    }
    const memo = el.closest("[data-memory]");
    if (memo && el.dataset.mfield) {
      if (!el.value.trim()) { J.toast("Üres emléket nem menthetek; ha nem kell, felejtsd el."); return; }
      await post("/api/creative/memory", { id: story.id, op: "edit", memory: memo.dataset.memory, text: el.value });
    }
  });

  $("c-memory").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-act]");
    if (!button || !story) return;
    const act = button.dataset.act;
    if (act === "mem-close") setMemoryOpen(false);
    else if (act === "card-add") {
      await post("/api/creative/card", { id: story.id, op: "add", card: { name: "Új kártya", keys: [], text: "" } });
      const names = $("c-cards").querySelectorAll(".c-card-name");
      if (names.length) { names[names.length - 1].focus(); names[names.length - 1].select(); }
    } else if (act === "card-delete") {
      if (await J.ask("Kártya törlése", "Biztosan törlöd ezt a világkártyát?", "Törlés")) {
        post("/api/creative/card", { id: story.id, op: "delete", card: { id: button.dataset.card } });
      }
    } else if (act === "memory-add") {
      memoryNew = memoryNew ?? "";
      renderMemories();
    } else if (act === "memory-cancel") {
      memoryNew = null;
      renderMemories();
    } else if (act === "memory-save") {
      const text = ($("cm-newmem") || {}).value || "";
      if (!text.trim()) { J.toast("Írd be, mire emlékezzen."); return; }
      if (await post("/api/creative/memory", { id: story.id, op: "add", text })) { memoryNew = null; renderMemories(); }
    } else if (act === "memory-delete") {
      if (await J.ask("Elfelejtsem?", "Ezt az emléket törlöm, és onnantól a modell nem tud róla (a történet szövege " +
        "megmarad).", "Elfelejtem")) {
        post("/api/creative/memory", { id: story.id, op: "delete", memory: button.dataset.memory });
      }
    } else if (act === "memory-clear") {
      if (await J.ask("Minden emlék törlése", "Az összes emléket és az eddigi történet összefoglalóját törlöm. A régi " +
        "részekről a modell onnantól nem tud (a történet szövege megmarad).", "Mind törlése")) {
        post("/api/creative/memory", { id: story.id, op: "clear" });
      }
    } else if (act === "summarize") {
      send("summarize");
    } else if (act === "delete") {
      if (await J.ask("Történet törlése", `Biztosan törlöd: „${story.title}”? A szövege és az emlékei is elvesznek.`, "Törlés")) {
        const result = await J.postJson("/api/creative/delete", { id: story.id });
        if (!result.ok) { J.toast(result.error || "Nem sikerült törölni."); return; }
        setMemoryOpen(false);
        stories = stories.filter((s) => s.id !== story.id);
        show("library");
      }
    }
  });

  function setMemoryOpen(open) {
    $("c-memory").hidden = !open;
    $("c-memtoggle").setAttribute("aria-expanded", String(open));
    $("c-reader").classList.toggle("mem-open", open);
    store("jarvis-creative-memory", open ? "1" : "0");
  }
  $("c-memtoggle").addEventListener("click", () => setMemoryOpen($("c-memory").hidden));
  setMemoryOpen(recall("jarvis-creative-memory") === "1" && matchMedia("(min-width: 1000px)").matches);

  // --- élő írás és változások a Jarvistól -------------------------------------------------------

  J.on("creative_list", (event) => {
    stories = event.stories || [];
    if (view === "library") renderLibrary();
  });
  J.on("creative_story", (event) => {
    if (story && event.story.id === story.id) applyStory(event.story);
  });
  J.on("creative_busy", (event) => {
    if (!story || event.id !== story.id || (event.seq || 0) < (story.status_seq || 0)) return;
    story.status_seq = event.seq || story.status_seq;
    story.status = { busy: event.busy, text: event.text, entry: event.entry };
    if (!event.busy && streaming) { streaming = null; renderStory(); }
    if (!story.entries.length) renderStory();
    updateControls();
  });
  J.on("creative_begin", (event) => {
    if (!story || event.id !== story.id) return;
    streaming = { entry: event.entry, retry: !!event.retry, text: "" };
    renderStory(true);
  });
  J.on("creative_delta", (event) => {
    if (!story || event.id !== story.id || !streaming || event.entry !== streaming.entry) return;
    streaming.text += event.text;
    const box = $("c-story");
    const stick = nearBottom(box);
    const el = box.querySelector("[data-stream] .c-text");
    if (el) el.textContent = streaming.text.replace(/^\s+/, "");
    if (stick) box.scrollTop = box.scrollHeight;
  });
  J.on("creative_end", (event) => {
    if (!story || event.id !== story.id) return;
    streaming = null;
    const s = event.stats || {};
    lastStats = s.seconds ? `${String(s.seconds).replace(".", ",")} mp${s.tps ? `, ~${Math.round(s.tps)} token/mp` : ""}` : "";
    if (event.cancelled) lastStats = "Megállítva" + (lastStats ? ` (${lastStats})` : "");
    if (!event.kept) renderStory();
  });

  J.onHello((event) => { if (event.creative) loadLibrary(); });
  window.addEventListener("jarvis-mode", (event) => {
    if (event.detail !== "creative") return;
    if (view === "reader") $("c-text").focus();
  });
})();
