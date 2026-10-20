"""A Munka menü. Az első munka az Adás/Vétel: Jarvis itt az eladásra szánt holmikon dolgozik.

Egy eladás menete (a JarvisApp az ágens-szálon futtatja, lásd app.run_work):
  1. a leírásodból egy modellhívással kiolvassa a hirdetés adatait (ListingExtractor; ugyanabból a
     leírásból csak egyszer),
  2. ha a kiválasztott oldalakhoz hiányzik valami, visszakérdez, és megvárja a válaszod (abból csak a
     hiányzó adatokat olvassa ki),
  3. ha kérted, megnézi, mennyiért kínálnak hasonlót; ha az árad sokkal drágább, megkérdezi,
     maradjon-e (csak ajánlás, a döntés a tiéd),
  4. sorban feltölti minden kiválasztott oldalra a saját böngészőjében (ehhez a Munka menü
     saját beszélgetése és eszközei kellenek), a közzététel előtt mindig rákérdez. Ugyanazon az
     oldalon két közzététel között pár perc szünetet tart: ezt a közzététel gombja előtt várja ki,
     az űrlapot addigra kitölti, és amelyik oldalon épp nem kell várni, az jön előbb. Ha egy fotó
     homályos, sötét vagy majdnem ugyanaz, mint egy másik (photos.py), az elején szól, de nem áll meg.
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable

from .assistant import KEEP_ABOVE
from .browser import BrowserError
from .config import Config, data_path, resolve_path
from .dossier import Dossiers
from .listings import CHECK_WORDS, GONE, SOLD, ListingChecker
from .deals import DealHunter
from .messages import MessageDesk
from .replay import (FlowRecorder, FlowStore, Replayer, ReplayOutcome, entry_for, entry_problem, flow_key, item_values,
                     url_shape)
from .selling import (CONDITIONS, MAX_PRICE, NEEDS, NOT_POSTED, ON_SITE, PARCEL_SIZES, PLATFORMS, POSTED, FAILED,
                      RESEARCH_MAX_AGE, Item, ItemError, ItemStore, ListingExtractor, Platform, PriceResearcher,
                      format_price, missing_needs, price_verdict, question_for, research_fresh)
from .textutil import fold, hu_amounts
from .tools import Toolbox, ToolError, params
from .vinted import ChoiceMemory, FillOutcome, VintedDelister, VintedFiller

# A Munka menü beszélgetése ezeket az eszközöket látja; a böngésző ugyanaz, mint a Beszélgetésé.
SHARED_TOOLS = {"get_datetime", "web_search", "read_webpage", "browser_open", "browser_click",
                "browser_type", "browser_fill", "browser_choose", "browser_do", "browser_scroll", "browser_back",
                "browser_read", "browser_look"}
WORK_ONLY_TOOLS = {"item_list", "item_get", "item_update", "item_research", "item_post", "item_mark",
                   "item_delist", "browser_upload_photos", "messages_list", "message_draft"}
# Egy modellel végzett feltöltés, levétel vagy ellenőrzés csak ezeket látja (és a böngésző minden eszközét, a
# browser_ kezdetűeket): kevesebb eszközleírás = rövidebb prompt, és a modell nem téved el (pl. egy keresésbe
# a kitöltés közepén). A feladat végéig ugyanaz a lista, így a prompt eleje az Ollama gyorsítótárában marad.
TASK_TOOLS = {"item_get", "item_mark", "item_post", "item_delist"}
DELISTED_NOTE = "Levéve"
# Levétel közben (amit már jóváhagytál) ezekre a gombokra nem kérdez újra, de fizetősre soha nem kattint.
_DELIST_OK_RE = re.compile(r"\b(torol|torl|delete|remove|eltavolit|inaktiv|lezar|befejez|visszavon|eladva|"
                           r"eladott|eladtam|sold|megerosit|confirm|igen|yes|ok|rendben|levetel|leveszem|levesz)")
_PAY_RE = re.compile(r"(fizet|vasarl|megrendel|checkout|penztar|buy|purchase|pay|utal|kiemel|elore ?sorol|"
                     r"elorehoz|elofizet)")
DELIST_SCOPE_SECONDS = 15 * 60
FIELD_NAMES = {"product": "mi ez", "title": "cím", "description": "leírás", "category": "kategória",
               "brand": "márka", "size": "méret", "condition": "állapot", "color": "szín",
               "material": "anyag", "price": "ár", "location": "átvétel helye", "shipping": "szállítás"}
SETTINGS = {"city": 80, "shipping": 300, "parcel_size": 20}
# A tárgy adatai, amelyek szavaival a böngésző egy hosszú lenyíló listából elöl mutatja az egyezőket (hints).
HINT_FIELDS = ("category", "product", "brand", "size", "condition", "color", "material", "kind", "location")

DEFAULT_WORK_PROMPT = (
    "Te {name} vagy, és most a Munka menüben a felhasználó holmijainak eladásában segítesz: "
    "hirdetési oldalakra töltesz fel, és a hirdetéseit kezeled. Röviden, magyarul válaszolj."
)


def mmss(seconds: float) -> str:
    """Hátralévő idő percben és másodpercben: 1:05."""
    seconds = max(0, int(math.ceil(seconds)))
    return f"{seconds // 60}:{seconds % 60:02d}"


def load_work_prompt(cfg: Config) -> str:
    try:
        template = resolve_path(cfg.work.prompt_file).read_text(encoding="utf-8")
    except OSError:
        template = DEFAULT_WORK_PROMPT
    return template.replace("{name}", cfg.assistant.name).strip()


def research_summary(research: dict[str, Any], now: float | None = None) -> str:
    if not research:
        return "Árkutatás: még nem volt."
    now = time.time() if now is None else now
    age = now - float(research.get("at") or now)
    head = f"Árkutatás („{research.get('query', '')}”)"
    if age >= RESEARCH_MAX_AGE:
        head += f", {int(age // 86400)} napos, elavult (árazás előtt újra megnézem)"
    similar = research.get("similar") or []
    if not similar:
        errors = "; ".join(research.get("errors") or [])
        text = f"{head}: nem találtam elég hasonló hirdetést."
        return text + (f" ({errors})" if errors else "")
    sites = sorted({entry["site"] for entry in similar})
    if research.get("p25") is not None and research.get("p75") is not None:
        band = f"többnyire {format_price(research['p25'])} és {format_price(research['p75'])} között"
    else:
        band = f"{format_price(research.get('min'))} és {format_price(research.get('max'))} között"
    text = (f"{head}: {len(similar)} hasonló hirdetés ({', '.join(sites)}), {band}, "
            f"középérték {format_price(research.get('median'))}. "
            f"Javasolt ár: {format_price(research.get('suggested'))}.")
    if research.get("confidence") == "bizonytalan":
        text += " Kevés vagy nagyon eltérő ár alapján, ezért csak tájékoztató."
    if research.get("reason"):
        text += " " + str(research["reason"])
    return text


# „Legyen a javasolt”, „oké, mehet”, „vidd lejjebb”: a javasolt ár (gyenge jel: a többi felülírja)
_SUGGESTED_WORDS = {"legyen", "rendben", "oke", "ok", "mehet", "lejjebb", "csokkentsd", "olcsobb", "olcsobban"}
# Egyértelműen a javasolt ár („a javasolt”, „az ajánlott”, „a tiéd”); egy előtte álló „ne” megfordítja
_SUGGESTED_STEMS = ("javasol", "javaslat", "ajanlott", "ajanlas", "tied")
# Egyértelműen a mostani ár („maradjon”, „az enyém”, „hagyd”); egy előtte álló „ne” megfordítja
_KEEP_STEMS = ("marad", "enyem", "sajatom", "hagyd")
# Bizonytalan válasz: inkább újra kérdez, mint hogy tippeljen („nem tudom”, „mindegy”, „hmm”)
_UNSURE_RE = re.compile(r"\b(nem tudom|nem tudok|nem tom|nemtom|nem biztos|mindegy|fogalmam sincs|passz|h+m+)\b")
# A tagadás ezeken átnyúlik: „ne legyen a javasolt”
_NEGATION_SKIP = {"a", "az", "legyen", "is", "inkabb"}
# Az árdöntés gombjai a felületen és a telefonon: marad, legyen a javasolt, vagy „Igen” a visszakérdezett összegre
# („3 Ft-ra gondoltál?”); minden más egy összeg (WorkDesk.decide_price).
PRICE_CHOICES = ("keep", "suggested", "confirmed")
# Visszakérdezés után („3 Ft-ra gondoltál?”): igen
_YES_WORDS = {"igen", "aha", "persze", "pontosan", "ja", "jaja", "stimmel", "helyes", "nana", "ugy", "ugyvan", "jo",
              "oke", "ok", "rendben", "mehet"}


def interpret_price_reply(text: str, confirming: bool = False) -> str | int | None:
    """Árdöntésre adott (írt vagy mondott) válasz: "keep" (maradjon), "suggested" (legyen a javasolt), egy összeg,
    vagy None (nem egyértelmű: újra kell kérdezni). `confirming`: Jarvis épp egy furcsa összegre kérdezett vissza
    („3 Ft-ra gondoltál?”); ilyenkor az igen "confirmed", a puszta nem pedig None (mondja meg újra).

    A kérdés: „Maradjon a te árad, vagy legyen a javasolt?”. A tagadás csak arra vonatkozik, ami utána jön:
    „ne maradjon, legyen a javasolt” a javasolt, „ne a javasolt legyen” a mostani ár."""
    amounts = [a for a in hu_amounts(text) if 0 < a <= MAX_PRICE]
    if amounts:
        return amounts[-1]   # „ne 150 ezer legyen, hanem 120 ezer”: az utolsó a kért
    folded = _UNSURE_RE.sub(" ", fold(text or ""))
    keep = suggest = weak = 0
    bare_no = yes = False
    for clause in re.split(r"[,;.!?]|\b(?:hanem|de|vagy)\b", folded):
        negated = False
        for token in re.findall(r"\w+", clause):
            if token in ("ne", "nem"):
                negated = True
                continue
            if negated and token in _NEGATION_SKIP:
                continue
            if token.startswith(_KEEP_STEMS):
                keep, suggest = (keep, suggest + 1) if negated else (keep + 1, suggest)
            elif token.startswith(_SUGGESTED_STEMS):
                keep, suggest = (keep + 1, suggest) if negated else (keep, suggest + 1)
            elif negated:
                bare_no = True       # „nem kell”: a változtatásra mond nemet
            else:
                weak += token in _SUGGESTED_WORDS
                yes = yes or token in _YES_WORDS
            negated = False
        bare_no = bare_no or negated     # „nem, jó így”
    if keep or suggest:
        if keep and suggest:
            return None      # „maradjon, vagy legyen a javasolt”: mindkettő, nem dönthetünk helyette
        return "keep" if keep else "suggested"
    if confirming:
        return "confirmed" if yes and not bare_no else None
    if bare_no:
        return "keep"
    return "suggested" if weak else None


@dataclass
class Step:
    kind: str          # "ask" (visszakérdez), "price" (árdöntésre vár), "post" (mehet a feltöltés)
    text: str = ""
    note: str = ""     # a fotókról, a feltöltés elején (csak jelzés: a feltöltés ettől megy tovább)


class WorkDesk:
    """Az eladásra szánt tárgyak, és amit a Jarvis velük csinál."""

    def __init__(self, cfg: Config, llm: Any, toolbox: Toolbox,
                 on_change: Callable[[dict[str, Any]], None] | None = None,
                 on_remove: Callable[[str], None] | None = None,
                 confirm: Callable[[str], bool] | None = None,
                 platforms: dict[str, Platform] | None = None,
                 check_stop: Callable[[], None] | None = None,
                 on_message: Callable[[dict[str, Any]], None] | None = None):
        self.cfg = cfg
        self.llm = llm
        self.toolbox = toolbox
        self.confirm = confirm or (lambda _prompt: False)
        self.check_stop = check_stop or (lambda: None)   # Állj gomb: Cancelled kivételt dob
        catalog = platforms or PLATFORMS
        self.platforms = {key: catalog[key] for key in cfg.work.platforms if key in catalog}
        self.root = data_path(cfg.work.dir, "munka")
        self.store = ItemStore(self.root / "eladas", max_photos=cfg.work.max_photos,
                               on_change=on_change, on_remove=on_remove, platform_keys=list(self.platforms))
        self.extractor = ListingExtractor(llm, self.store)
        self.browser_ready = toolbox.has("browser_open")
        sites = [catalog[key] for key in cfg.work.research_sites if key in catalog]
        self.researcher = (PriceResearcher(llm, self.store, lambda: toolbox.browser, sites)
                           if self.browser_ready and sites else None)
        # A fent lévő hirdetések háttérellenőrzése (elkelt-e, eltűnt-e): modell nélkül, csak olvasva.
        self.checker = (ListingChecker(self.store, self.platforms, lambda: toolbox.browser)
                        if self.browser_ready else None)
        self._current: tuple[str, str] | None = None   # (tárgy, oldal), amit épp feltölt vagy levesz (current)
        self._answered: dict[str, list[str]] = {}     # tárgy → amire a legutóbb visszakérdezett és válaszoltál
        self.approved = False    # feltöltés közben jóváhagytál-e egy kattintást (pl. a közzétételt)
        # (tárgy, oldal), amiről a modell épp megnézi, fent van-e: ilyenkor újra feltölteni nem szabad.
        self.checking: tuple[str, str] | None = None
        # (tárgy, oldal), amit a Vinted-levevő ebben a kérésben már megpróbált: az item_delist nem kezdi újra.
        self.autodelisted: tuple[str, str] | None = None
        self._settings_path = self.root / "beallitasok.json"
        self.settings = self._load_settings()
        # Vevői üzenetek (messages.py): olvasás modell nélkül, válaszvázlat kérésre, küldés csak a te jóváhagyásoddal.
        self.messages = MessageDesk(self.root, self.platforms, self.store,
                                    (lambda: toolbox.browser) if self.browser_ready else None, llm,
                                    settings=lambda: self.settings, confirm=lambda prompt: self.confirm(prompt),
                                    check_stop=lambda: self.check_stop(), on_change=on_message)
        # Automata kereskedés (deals.py): alulárazott hirdetések keresése, modell nélkül (csak a jelöltekre egy kérdés).
        self.deals = DealHunter(self.root, self.platforms, (lambda: toolbox.browser) if self.browser_ready else None,
                                llm)
        # Begyakorolt lépések: a modell sikeres feltöltéseiből és levételeiből tanul (replay.py).
        self.flows = FlowStore(self.root / "folyamatok")
        # A Vinted-kitöltő (vinted.py) választásai, pl. egy tárgyfajta kategóriaútja: legközelebb modell nélkül.
        self.choices = ChoiceMemory(self.root / "valasztasok" / "vinted.json")
        # Hibacsomagok (dossier.py): ha egy feltöltés vagy levétel elakad, minden a javításhoz.
        self.dossiers = Dossiers(self.root / "hibak")
        self.trail: list[dict[str, Any]] = []      # a mostani feltöltés/levétel böngészős lépései
        # Emberi tempó: oldal → mikortól jöhet a következő közzététel vagy levétel. A szünetet a közzététel
        # gombja előtt várja ki (_before_publish), így az űrlap addigra ki van töltve.
        self._next_allowed: dict[str, float] = {}
        self.published_in: tuple[str, str] | None = None   # (tárgy, oldal): ebben a menetben már volt közzététel
        # A szünet kijelzése (a JarvisApp állítja be: napló, állapotsor); alapból csak a tárgy kártyáján látszik.
        self.gap_waiter: Callable[[str, str], Any] | None = None
        self.recorder: FlowRecorder | None = None
        # A jóváhagyó ablakba: mivel töltötte ki (begyakorolt lépések vagy a Vinted-kitöltő), és mit választott.
        self.preview_title = ""
        self.replay_preview: list[str] = []
        # Jóváhagyott levétel: (tárgy, oldal, meddig); addig a törlés gombjaira nem kérdez újra.
        self.delisting: tuple[str, str, float] | None = None
        if self.browser_ready:
            browser = toolbox.browser
            browser.on_action = self._on_browser_action
            browser.approve_hook = self._preapproved
            browser.before_publish = self._before_publish
        self._register(toolbox)

    @property
    def agent_tools(self) -> set[str]:
        return (SHARED_TOOLS | WORK_ONLY_TOOLS) & set(self.toolbox.names())

    @property
    def task_tools(self) -> set[str]:
        """A modellel végzett feltöltés, levétel és ellenőrzés eszközei (lásd TASK_TOOLS)."""
        return {name for name in self.toolbox.names() if name.startswith("browser_") or name in TASK_TOOLS}

    def describe(self) -> dict[str, Any]:
        """A felületnek: milyen oldalak vannak, mit kérnek, és mik az alapadataid."""
        return {
            "platforms": [{"key": p.key, "name": p.name, "research": bool(p.search_url)}
                          for p in self.platforms.values()],
            "needs": [{"label": n.label, "sites": list(n.sites), "clothing_only": n.clothing_only,
                       "setting": n.setting} for n in NEEDS],
            "acknowledged": list(self.settings.get("acknowledged", [])),
            "settings": {key: self.settings.get(key, "") for key in SETTINGS},
            "parcel_sizes": PARCEL_SIZES,
            "research": self.researcher is not None,
            "browser": self.browser_ready,
            "conditions": CONDITIONS,
            "max_photos": self.store.max_photos,
            "messages": bool(self.messages.platforms),
        }

    # --- beállítások (alapadatok) ---------------------------------------------------------------

    def _load_settings(self) -> dict[str, Any]:
        try:
            data = json.loads(self._settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_settings(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._settings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.settings, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self._settings_path)

    def save_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        for key, limit in SETTINGS.items():
            if key in values:
                text = " ".join(str(values[key] or "").split())[:limit]
                if key == "parcel_size" and text and text not in PARCEL_SIZES:
                    raise ItemError(f"A csomagméret ezek egyike lehet: {', '.join(PARCEL_SIZES)}")
                self.settings[key] = text
        self._save_settings()
        return {key: self.settings.get(key, "") for key in SETTINGS}

    def acknowledge(self, platform: str) -> None:
        """A felhasználó tudomásul vette, hogy az oldal szabályai tilthatják a gépi kitöltést."""
        if platform not in self.platforms:
            raise ItemError(f"Ismeretlen oldal: {platform}")
        self.settings["acknowledged"] = sorted(set(self.settings.get("acknowledged", [])) | {platform})
        self._save_settings()

    # --- az eladás lépései (az ágens-szálon) ------------------------------------------------------

    def prepare(self, item_id: str, progress: Callable[[str], None]) -> Step:
        """Adatok a leírásból, hiányzó adatok, árellenőrzés. Megmondja, mi a következő lépés (feltöltésnél a
        fotókról egy rövid jelzéssel, ha valamelyikkel gond van)."""
        item = self.store.get(item_id)
        if not item.targets:
            raise ItemError("Pipáld be, hova töltsem fel.")
        if not item.photos:
            raise ItemError("Ehhez a tárgyhoz még nincs fotó.")
        answered = self._answered.pop(item_id, None)
        if not self.extractor.up_to_date(item):   # ugyanabból a leírásból nem olvassa ki újra
            progress("a leírásod értelmezése")
            # a visszakérdezésre adott válaszból csak a hiányzó adatok (rövidebb séma, gyorsabb válasz)
            only = answered if answered and item.title and self.extractor.appended(item) else None
            item = self.extractor.extract(item_id, only=only)
        missing = missing_needs(item, self.settings)
        if missing:
            question = question_for(item, missing)
            self.store.set_pending(item_id, {"kind": "info", "question": question})
            return Step("ask", question)
        # Az egy hétnél régebbi árkutatást az árazás előtt frissíti (azóta változhattak az árak).
        if item.research_wanted and not research_fresh(item) and self.researcher is not None:
            try:
                item = self.researcher.research(item_id, lambda detail: progress("árkutatás – " + detail),
                                                only=item.targets)
            except ItemError as exc:   # az árkutatás csak ajánlás: ha nem megy, attól még feltöltjük
                self.store.set_pending(item_id, None)
                return Step("post", f"Az árkutatás nem sikerült ({exc}), marad az árad.", self.photo_note(item_id))
            verdict = price_verdict(item)
            if verdict.pause:
                self.store.set_pending(item_id, {"kind": "price", "question": verdict.text,
                                                 "suggested": verdict.suggested})
                return Step("price", verdict.text)
            self.store.set_pending(item_id, None)
            return Step("post", verdict.text, self.photo_note(item_id))
        self.store.set_pending(item_id, None)
        return Step("post", note=self.photo_note(item_id))

    def photo_note(self, item_id: str) -> str:
        """A fotókról egy rövid jelzés a feltöltés elejére (homályos, sötét, majdnem ugyanaz, egy másik tárgy
        fotója), vagy üres, ha mind rendben van. A feltöltést nem állítja meg. Egy régebbi tárgynál előbb megméri
        a fotókat (az ágens-szálon, csak ennek a tárgynak a fotóit)."""
        note = self.store.ensure_checks(item_id).photo_report()["photo_note"]
        return f"{note} Ettől még feltöltöm." if note else ""

    def answer(self, item_id: str, text: str) -> Item:
        """A visszakérdezésre adott válasz a leírás mellé kerül; a következő menet már ezzel számol (és a
        válaszból csak azt olvassa ki, amire rákérdezett)."""
        item = self.store.get(item_id)
        if item.pending.get("kind") == "info":
            self._answered[item_id] = [need.field for need, _ in missing_needs(item, self.settings)]
        self.store.update(item_id, prompt=(item.prompt + "\n" + text.strip()).strip())
        return self.store.set_pending(item_id, None)

    def decide_price(self, item_id: str, choice: str | int) -> Item:
        """Árdöntés: "keep" = marad az ár, "suggested" = a javasolt, egy konkrét összeg, vagy "confirmed" = a
        visszakérdezett összeg. Ha az összeg gyanús (pl. a félrehallott „3 és fél ezer” 3 Ft lenne), nem írja át,
        hanem visszakérdez: a kérdés a tárgy függő kérdése lesz („3 Ft-ra gondoltál?”)."""
        item = self.store.get(item_id)
        if choice == "confirmed":
            choice = item.pending.get("confirm") or "keep"
        elif choice == "suggested":
            choice = item.pending.get("suggested") or item.research.get("suggested") or "keep"
        elif choice != "keep" and choice != item.pending.get("confirm") and self.odd_price(item, int(choice)):
            # (ha ugyanazt az összeget mondja vagy írja be újra, az is igen)
            question = (f"{format_price(choice)}-ra gondoltál? Ha igen, mondd, hogy igen; ha nem, mondd meg újra, "
                        "mennyi legyen.")
            return self.store.set_pending(item_id, {**item.pending, "kind": "price", "question": question,
                                                    "confirm": int(choice)})
        if choice != "keep":
            self.store.update(item_id, user=True, price=choice)
        return self.store.set_pending(item_id, None)

    @staticmethod
    def odd_price(item: Item, amount: int) -> bool:
        """Gyanús összeg: a mostani és a javasolt ár 20%-ánál is kevesebb, vagy a háromszorosánál is több."""
        references = [r for r in (item.price, item.pending.get("suggested"), item.research.get("suggested")) if r]
        return bool(references) and not any(0.2 * r <= amount <= 3 * r for r in references)

    def pending_item(self) -> Item | None:
        """A legutóbb visszakérdezett tárgy (a Munka menüben írt válasz ehhez tartozik)."""
        waiting = [i for i in self.store.all() if i.pending.get("kind")]
        return max(waiting, key=lambda i: i.updated) if waiting else None

    def targets_left(self, item_id: str) -> list[str]:
        item = self.store.get(item_id)
        return [t for t in item.targets if t in self.platforms
                and item.platforms.get(t, {}).get("status") != POSTED]

    def post_request(self, item: Item, key: str) -> str:
        """A modell első üzenete egy feltöltéshez. Az első lépés mindig ugyanaz (item_post: az űrlap megnyitása, a
        hirdetés adatai, a tippek), ezt a Jarvis maga végzi el, és az eredményével kezd a modell: egy modellkör
        (a még hideg prompt feldolgozása) megspórolva. ToolError, ha nem lehet (pl. hiányzik a cím)."""
        site = self.platforms[key]
        opened = self.tool_post(item.id, key)
        return (f"Töltsd fel a(z) „{item.name}” tárgyat (azonosító: {item.id}) a(z) {site.name} oldalra "
                f"(platform: {site.key}). Az item_post lépést már elvégeztem, a hirdetésfeladás nyitva van a "
                "böngésződben (az item_post eszközt ne használd, mert elölről kezdené). Töltsd ki a hirdetés "
                f"adataival, tedd közzé, és a végén jelöld meg az item_mark eszközzel.\n\n{opened}")

    def unmarked_publish(self, item_id: str, key: str) -> bool:
        """A közzétételt jóváhagytad, de a tárgy nincs megjelölve feltöltöttnek (pl. a modell válasza
        félbemaradt). Ilyenkor meg kell nézni, fent van-e: az Újra gomb különben másodszor is feltöltené."""
        if not self.approved:
            return False
        try:
            item = self.store.get(item_id)
        except ItemError:
            return False
        return item.platforms.get(key, {}).get("status") != POSTED

    def check_request(self, item: Item, key: str) -> str:
        """A modell nézze meg, fent van-e a hirdetés, amit közzétett, de nem jelölt meg."""
        site = self.platforms[key]
        return (f"A(z) „{item.name}” hirdetés (azonosító: {item.id}) közzétételét a felhasználó jóváhagyta a(z) "
                f"{site.name} oldalon, de a tárgy még nincs megjelölve. Nézd meg a böngészőben nyitott oldalt "
                "(browser_read vagy browser_look). Ha a hirdetés fent van (pl. az oldal szerint feltöltve, vagy ott "
                "van a hirdetéseid között), most jelöld meg az item_mark eszközzel (status: feltöltve, platform: "
                f"{key}, és ha látod, a hirdetés webcíme). Ha hibaüzenet van (pl. hiányzó kötelező mező), javítsd ki "
                "a hirdetés adataival (item_get), és tedd közzé újra. Ha nem tudod eldönteni, ne jelöld meg. Az "
                "item_post eszközt ne használd: új hirdetést kezdene, és a tárgy kétszer kerülne fel. Röviden mondd "
                "el az eredményt.")

    def finish_post(self, item_id: str, key: str, last_reply: str = "") -> bool:
        """Egy oldal feltöltése után: ha nem jelölte meg feltöltöttnek, hibának jelöljük."""
        self.current = None
        if self.recorder is not None and self.recorder.task == "post":
            self.recorder = None     # befejezetlen felvétel: nem jegyezzük meg
        approved, self.approved = self.approved, False
        try:
            item = self.store.get(item_id)
        except ItemError:
            return False
        self.store.set_busy(item_id, "")
        if item.platforms.get(key, {}).get("status") == POSTED:
            return True
        note = last_reply or "Nem sikerült végigcsinálni."
        if approved:   # a közzétételt jóváhagytad, csak a megjelölés maradt el
            note = ("Lehet, hogy mégis fent van (jóváhagytad a közzétételt). Nézd meg az oldalon, és ha fent "
                    "van, nyomd meg a „Fent van” gombot. " + (last_reply or ""))
        self.store.mark(item_id, key, FAILED, note=note.strip())
        return False

    def confirm_context(self) -> str:
        """A jóváhagyó ablak első sora feltöltés közben: melyik oldal, melyik tárgy, mennyiért.
        Visszajátszásnál alatta az is, mit választott ki (kategória, állapot, méret…), hogy ránézésre
        ellenőrizhesd, mielőtt jóváhagyod. Üzenet küldésekor: kinek, melyik oldalon, és pontosan mi megy el."""
        if self.current is None:
            return self.messages.sending
        try:
            item = self.store.get(self.current[0])
        except ItemError:
            return ""
        site = self.platforms.get(self.current[1])
        text = f"{site.name if site else self.current[1]}: {item.name} – {format_price(item.price)}"
        if self.replay_preview:
            text += f"\n{self.preview_title or 'Begyakorolt lépésekkel töltöttem ki.'} " + "; ".join(self.replay_preview)
        return text

    # --- felvétel és visszajátszás (lásd replay.py) ---------------------------------------------------

    def end_request(self) -> None:
        """Egy kérés vége: a felvétel és a levétel jóváhagyása nem nyúlhat át a következő kérésre
        (különben egy későbbi, más célú böngészés is a recept része lehetne, vagy a törlés gombjai
        kérdés nélkül maradnának)."""
        self.recorder = None
        self.delisting = None
        self.replay_preview = []
        self.preview_title = ""
        self.checking = None
        self.autodelisted = None
        if self.browser_ready:
            self.toolbox.browser.hints = []     # a tárgy szavai csak a feladat alatt (lásd current)
        self.messages.sending = ""

    def _on_browser_action(self, event: dict[str, Any]) -> None:
        if self.recorder is not None:
            self.recorder.add(event)
        if self.current is not None:
            self.trail.append(dict(event))
            del self.trail[:-200]
            # Közzététel (vagy törlés): egy kockázatos gomb. Egy kockázatos jelölőnégyzet (pl. a feltételek
            # elfogadása, check) nem az: utána még jön a közzététel gombja, és az előtt kell a szünet.
            if event.get("do") == "click" and event.get("risky") and not event.get("check"):
                self.note_publish(self.current[1])
                self.published_in = self.current

    def begin_trail(self) -> None:
        """Egy feltöltés vagy levétel kezdete: innen jegyezzük a lépéseket a hibacsomaghoz."""
        self.trail = []
        self.published_in = None

    # --- emberi tempó: két közzététel között ugyanazon az oldalon ------------------------------------

    def note_publish(self, key: str, now: float | None = None) -> None:
        """Megtörtént egy közzététel (vagy törlés) ezen az oldalon: a következő csak pár perc múlva."""
        gap = float(self.cfg.work.publish_gap_minutes or 0) * 60
        if gap > 0:
            now = time.time() if now is None else now
            self._next_allowed[key] = now + gap * random.uniform(0.7, 1.3)

    def gap_remaining(self, key: str, now: float | None = None) -> float:
        """Hány másodpercet kell még várni a következő közzétételig ezen az oldalon."""
        now = time.time() if now is None else now
        return max(0.0, self._next_allowed.get(key, 0.0) - now)

    def wait_gap(self, item_id: str, key: str, show: Callable[[str], None] | None = None) -> float:
        """Ha ezen az oldalon nemrég volt közzététel (vagy levétel), megvárja a szünet végét; közben a
        tárgy kártyáján látszik, mennyi van még hátra. Az Állj gomb megszakítja (Cancelled).
        Visszaadja, hány másodpercet várt."""
        if self.gap_remaining(key) <= 0:
            return 0.0
        site = self.platforms[key].name if key in self.platforms else key
        started, shown = time.monotonic(), ""
        while True:
            remaining = self.gap_remaining(key)
            if remaining <= 0:
                break
            self.check_stop()
            text = f"{site}: várok még {mmss(math.ceil(remaining / 5) * 5)}"
            if text != shown:
                shown = text
                try:
                    self.store.set_busy(item_id, text + "…")
                except ItemError:
                    pass
                if show is not None:
                    show(text)
            time.sleep(min(1.0, remaining))
        return time.monotonic() - started

    def _before_publish(self, label: str, url: str) -> None:
        """A böngésző hívja a közzététel (vagy törlés) kattintása előtt, még a jóváhagyó kérdés előtt: ha ezen az
        oldalon nemrég volt közzététel, itt várja ki a szünetet (Állj: Cancelled). Így az űrlap kitöltése a szünetre
        esik, nem utána. Feltöltésen vagy levételen kívül (pl. a Beszélgetés böngészésénél) nem vár, és egy menetben
        csak az első ilyen kattintásnál (a megerősítő gomb vagy a hiba utáni újraküldés már nem vár)."""
        if self.current is None or self.published_in == self.current:
            return
        item_id, key = self.current
        if self.gap_remaining(key) <= 0:
            return
        try:
            busy = self.store.get(item_id).busy
        except ItemError:
            busy = ""
        if self.gap_waiter is not None:
            self.gap_waiter(item_id, key)
        else:
            self.wait_gap(item_id, key)
        if busy:
            self.store.set_busy(item_id, busy)    # a visszaszámlálás helyett újra az, amin dolgozik

    @property
    def current(self) -> tuple[str, str] | None:
        """(tárgy, oldal), amit épp feltölt vagy levesz."""
        return self._current

    @current.setter
    def current(self, value: tuple[str, str] | None) -> None:
        # Amíg egy feltöltés vagy levétel tart, az oldalnézet a hosszú lenyíló listákból elöl a tárgy szavaival
        # egyező lehetőségeket mutatja (BrowserController.hints); utána, pl. a Beszélgetés böngészésénél, már nem.
        self._current = value
        if self.browser_ready:
            self.toolbox.browser.hints = self.item_hints(value[0]) if value else []

    def item_hints(self, item_id: str) -> list[str]:
        """A tárgy szavai a böngészőnek: kategória, mi ez, márka, méret, állapot, szín, anyag, fajta, település."""
        try:
            item = self.store.get(item_id)
        except ItemError:
            return []
        values = item_values(item, self.settings)
        return [values[name] for name in HINT_FIELDS if values.get(name)]

    @property
    def recorder(self) -> FlowRecorder | None:
        """A modell lépéseinek felvétele, amíg tart (lásd start_recording)."""
        return self._recorder

    @recorder.setter
    def recorder(self, value: FlowRecorder | None) -> None:
        # A böngésző csak felvétel közben számolja ki, hányadik egy elem a hasonlók között (ORDINAL_JS).
        self._recorder = value
        if self.browser_ready:
            self.toolbox.browser.recording = value is not None

    def start_recording(self, item: Item, key: str, task: str, start_url: str) -> None:
        """A modell lépéseinek felvétele (a sikeres végén megjegyezzük őket)."""
        self.recorder = None
        if not self.cfg.work.replay or key not in self.platforms:
            return
        self.recorder = FlowRecorder(key, task, flow_key(key, task, item.kind, item.category), start_url,
                                     self.platforms[key].domains)

    def finish_recording(self, item_id: str, key: str, task: str) -> str:
        """Ha a felvétel tiszta volt, receptet készít belőle. Visszaadja, mit mondjon róla."""
        recorder = self.recorder
        if recorder is None or recorder.platform != key or recorder.task != task:
            return ""
        self.recorder = None
        try:
            item = self.store.get(item_id)
        except ItemError:
            return ""
        if task == "post":
            self.learn_entry(key, recorder.entry_url())   # akkor is, ha a lépéssor nem elég tiszta a recepthez
        browser = self.toolbox.browser if self.browser_ready else None
        if browser is not None and browser.running:
            # Ahol most áll (a modell már megnézte az eredményt): a sikeres oldal címe és címsora.
            try:
                title, heading = browser.headline()
                recorder.landing = {"url": browser.url, "title": title, "heading": heading}
            except BrowserError:
                pass
        recording, reason = recorder.build(item_values(item, self.settings))
        if recording is None:
            return f" (A lépéseket most nem jegyeztem meg: {reason}.)"
        try:
            self.flows.save(recording)
        except OSError:
            return ""
        return " Megjegyeztem a lépéseket: legközelebb ezen az oldalon modell nélkül, gyorsabban megy."

    def _flow_key(self, item: Item, key: str, task: str) -> str:
        return flow_key(key, task, item.kind, item.category)

    def entry_url(self, key: str) -> str:
        """Ahol ezen az oldalon legutóbb a hirdetésfeladás űrlapja kezdődött (üres, ha még nem tudjuk)."""
        entry = (self.settings.get("entry_urls") or {}).get(key, "")
        site = self.platforms.get(key)
        return entry_for(entry, site.domains) if isinstance(entry, str) and site is not None else ""

    def learn_entry(self, key: str, url: str) -> None:
        """Sikeres feltöltés után megjegyzi, hol kezdődött az űrlap (a webcím útja, kérdőjeles rész nélkül, csak
        az oldal saját címein): legközelebb közvetlenül azt nyitja meg, nem a főoldalról keresi."""
        site = self.platforms.get(key)
        entry = entry_for(url, site.domains) if site is not None else ""
        if site is not None and entry and url_shape(entry) == url_shape(site.new_url):
            self.forget_entry(key)    # ott kezdődik, ahol eddig is: nincs mit külön megjegyezni
            return
        if not entry or entry == self.entry_url(key):
            return
        self.settings["entry_urls"] = {**(self.settings.get("entry_urls") or {}), key: entry}
        try:
            self._save_settings()
        except OSError:
            pass

    def forget_entry(self, key: str) -> None:
        """A megjegyzett űrlapcím már nem jó (nincs meg, vagy máshová visz): legközelebb a kezdőoldalról."""
        entries = dict(self.settings.get("entry_urls") or {})
        if entries.pop(key, None) is None:
            return
        self.settings["entry_urls"] = entries
        try:
            self._save_settings()
        except OSError:
            pass

    def can_replay(self, item_id: str, key: str, task: str) -> bool:
        if not (self.cfg.work.replay and self.browser_ready) or key not in self.platforms:
            return False
        try:
            item = self.store.get(item_id)
        except ItemError:
            return False
        if task == "delist" and not item.platforms.get(key, {}).get("url"):
            return False
        return self.flows.get(self._flow_key(item, key, task)) is not None

    def replay(self, item_id: str, key: str, task: str,
               progress: Callable[[str], None] | None = None) -> ReplayOutcome:
        """Visszajátssza a begyakorolt feltöltést vagy levételt (modell nélkül)."""
        item = self.store.get(item_id)
        site = self.platforms[key]
        flow = self._flow_key(item, key, task)
        recording = self.flows.get(flow)
        if recording is None:
            return ReplayOutcome("no_plan", "nincs begyakorolt lépéssor")
        photos = self.photos_for(item, key) if task == "post" else []
        # A modell csak egy-egy rövid, kötött kérdésre kell (eltűnt elem, még nem látott érték), a lépésekhez nem.
        replayer = Replayer(self.toolbox.browser, recording, item_values(item, self.settings), photos,
                            check=self.check_stop, progress=progress, llm=self.llm)
        problem = replayer.plan_problem()
        if problem:
            return ReplayOutcome("no_plan", problem)
        start = site.new_url if task == "post" else item.platforms.get(key, {}).get("url", "")
        if not start:
            return ReplayOutcome("no_plan", "nem tudom a hirdetés webcímét")
        self.recorder = None
        self.begin_trail()
        self.replay_preview = replayer.shown     # menet közben pontosodik (pl. egy új értéknél mire kattintott)
        try:
            outcome = replayer.run(start)
        finally:
            self.replay_preview = []
        if outcome.status == "done":
            if task == "post":
                self.learn_entry(key, replayer.form_page)
            if replayer.changed:
                # Javított lépés vagy új érték: a recept így marad, legközelebb már kérdés nélkül megy.
                try:
                    self.flows.save(recording)
                except OSError:
                    pass
            self.flows.update_stats(flow, ok=True)
        elif outcome.status in ("miss", "partial"):
            self.flows.update_stats(flow, ok=False)
        return outcome

    def verify_request(self, item: Item, key: str, task: str, outcome: ReplayOutcome | FillOutcome,
                       how: str = "begyakorolt lépésekkel") -> str:
        """A modell nézze meg, sikerült-e (ha a visszajátszás vagy a modell nélküli levétel után nem egyértelmű)."""
        site = self.platforms[key]
        if task == "post":
            return (f"A(z) „{item.name}” hirdetést (azonosító: {item.id}) begyakorolt lépésekkel kitöltöttem a(z) "
                    f"{site.name} oldalon, és a felhasználó jóváhagyta a közzétételt, de nem biztos, hogy sikerült "
                    f"({outcome.reason}). Nézd meg az oldalt (browser_read vagy browser_look). Ha fent van, jelöld meg "
                    f"az item_mark eszközzel (status: feltöltve, platform: {key}, és ha látod, a hirdetés webcíme). "
                    "Ha hibaüzenet van (pl. hiányzó kötelező mező), javítsd ki a hirdetés adataival (item_get), és "
                    "tedd közzé újra. Ha nem tudod eldönteni, ne jelöld meg. Röviden mondd el az eredményt.")
        return (f"A(z) „{item.name}” hirdetést (azonosító: {item.id}) {how} vettem le a(z) "
                f"{site.name} oldalról, de nem biztos, hogy sikerült ({outcome.reason}). Nézd meg az oldalt. Ha a "
                "hirdetés már nem él (törölve, inaktív vagy eladottként jelölve), jelöld meg az item_mark eszközzel "
                f"(status: nincs fent, platform: {key}). Ha még fent van, fejezd be a levételt. Röviden mondd el "
                "az eredményt.")

    # --- a Vinted-kitöltő (lásd vinted.py) -------------------------------------------------------------

    def can_autofill(self, item_id: str, key: str) -> bool:
        """A Vinted űrlapját a Jarvis maga tölti ki ([work] autofill); a modell csak a kategóriát választja ki."""
        if not (self.cfg.work.autofill and self.browser_ready) or key != "vinted" or key not in self.platforms:
            return False
        try:
            item = self.store.get(item_id)
        except ItemError:
            return False
        return bool(item.title and item.description and item.price and item.photos)

    def autofill(self, item_id: str, key: str, progress: Callable[[str], None] | None = None) -> FillOutcome:
        """Kitölti (és jóváhagyással közzéteszi) a hirdetést a Vinted-kitöltővel."""
        item = self.store.get(item_id)
        filler = VintedFiller(self.toolbox.browser, self.llm, item_values(item, self.settings),
                              self.photos_for(item, key), memory=self.choices,
                              check=self.check_stop, progress=progress)
        self.recorder = None
        self.begin_trail()
        self.preview_title = "Modell nélkül töltöttem ki."
        self.replay_preview = filler.summary      # a közzététel kérdésében látod, mit választott
        try:
            outcome = filler.run(self.platforms[key].new_url)
        finally:
            self.replay_preview = []
            self.preview_title = ""
        self.remember_profile(outcome.profile)
        return outcome

    def remember_profile(self, url: str) -> None:
        """A Vinted-profilod címe (a feltöltés után a Vinted oda visz): ha egy hirdetés webcíme nincs meg, a
        levételnél ott keresi."""
        if url and self.settings.get("vinted_profile") != url:
            self.settings["vinted_profile"] = url
            try:
                self._save_settings()
            except OSError:
                pass

    # --- a Vinted-levétel (lásd vinted.py) ------------------------------------------------------------

    def can_autodelist(self, item_id: str, key: str) -> bool:
        """A Vintedről a Jarvis maga veszi le a hirdetést ([work] autofill): Törlés, megerősítés, modell nélkül."""
        if not (self.cfg.work.autofill and self.browser_ready) or key != "vinted" or key not in self.platforms:
            return False
        try:
            item = self.store.get(item_id)
        except ItemError:
            return False
        return bool(item.title or item.platforms.get(key, {}).get("url"))

    def autodelist(self, item_id: str, key: str, progress: Callable[[str], None] | None = None) -> FillOutcome:
        """Leveszi a hirdetést a Vinted-levevővel (a levételt már jóváhagytad: begin_delist)."""
        item = self.store.get(item_id)
        site = self.platforms[key]
        delister = VintedDelister(self.toolbox.browser, item.title or item.name,
                                  url=item.platforms.get(key, {}).get("url", ""),
                                  profile=str(self.settings.get("vinted_profile") or ""), search_url=site.search_url,
                                  check=self.check_stop, progress=progress)
        self.recorder = None
        self.begin_trail()
        self.autodelisted = (item_id, key)
        outcome = delister.run()
        self.remember_profile(outcome.profile)
        return outcome

    def _after_autodelist(self, found: Item, platform: str, outcome: FillOutcome) -> str | None:
        """Az item_delist eszközben (a beszélgetésből kért levételnél) a Vinted-levevő eredménye: a kész válasz, ha
        a modellnek nincs több dolga; None, ha folytatnia kell (a megtalált hirdetésnél)."""
        site = self.platforms[platform]
        if outcome.status in ("done", "gone"):
            note = DELISTED_NOTE if outcome.status == "done" else f"{DELISTED_NOTE} (már nem volt fent)"
            self.store.mark(found.id, platform, NOT_POSTED, note=note)
            self.store.set_busy(found.id, "")
            self.delisting = self.current = None
            if outcome.status == "gone":
                return f"A hirdetés már nem volt fent a(z) {site.name} oldalon. Megjelöltem: nincs fent."
            return f"Levettem a(z) {site.name} oldalról (modell nélkül). Megjelöltem: nincs fent."
        if outcome.url:          # megtalálta a hirdetést: onnan folytatódik
            self.store.mark(found.id, platform, POSTED, url=outcome.url)
        if outcome.status == "declined":
            self.store.set_busy(found.id, "")
            return "A felhasználó nem engedélyezte a levételt."
        if outcome.status == "blocked":
            self.store.set_busy(found.id, "")
            return f"Nem tudom levenni: {outcome.reason} Állj meg, és mondd el ezt a felhasználónak."
        return None

    def remember_choices(self, outcome: FillOutcome) -> None:
        """Sikeres feltöltés után: amit a modell kiválasztott (pl. a kategóriautat), legközelebb modell nélkül."""
        try:
            self.choices.update(outcome.learned)
        except OSError:
            pass

    def continue_request(self, item: Item, key: str, outcome: FillOutcome | ReplayOutcome, page: str) -> str:
        """A Vinted-kitöltő (vagy az elakadt visszajátszás) után a modell a nyitott űrlapon folytatja (nem kezdi
        elölről)."""
        site = self.platforms[key]
        mark = (f"jelöld meg az item_mark eszközzel (status: feltöltve, platform: {key}, és ha látod, a hirdetés "
                "webcíme)")
        if outcome.published:
            task = (f"Kitöltöttem a(z) {site.name} hirdetési űrlapját, és a felhasználó jóváhagyta a közzétételt, de nem "
                    f"biztos, hogy sikerült ({outcome.reason}). Nézd meg az oldalt. Ha a hirdetés fent van, rögtön {mark}. "
                    "Ha az oldal azt írja, hogy valami hiányzik, pótold a hirdetés adataival, és tedd közzé újra (a rendszer "
                    "megint jóváhagyást kér). Ha nem tudod eldönteni, fent van-e, ne jelöld meg.")
        else:
            task = (f"Elkezdtem kitölteni a(z) {site.name} hirdetési űrlapját, ami nyitva van a böngésződben. Kész: "
                    f"{', '.join(outcome.filled) or 'még semmi'}. Nem sikerült: {outcome.reason}. Ezeket pótold a hirdetés "
                    "adataival, a kész mezőkhöz ne nyúlj. Utána kattints a közzététel gombjára (a rendszer jóváhagyást "
                    f"kér), és ha fent van, rögtön {mark}.")
        return (f"A(z) „{item.name}” hirdetés (azonosító: {item.id}). {task} Az item_post eszközt ne használd, mert "
                "elölről kezdené. Márkát, méretet, színt vagy állapotot ne találj ki: ha valami nincs meg a hirdetés "
                "adatai között, állj meg, és mondd meg, mi kell. Ha bejelentkezés vagy robot-ellenőrzés kell, állj meg, és "
                "kérd meg a felhasználót. A végén röviden mondd el az eredményt.\n\n"
                f"A hirdetés adatai ({item.id}):\n{self.listing_block(item, key)}\n\nTippek ({site.name}): {site.hints}"
                + KEEP_ABOVE + page)

    # --- levétel (eladás után) ----------------------------------------------------------------------

    def posted_on(self, item: Item) -> list[str]:
        return [k for k in self.platforms if item.platforms.get(k, {}).get("status") == POSTED]

    def begin_delist(self, item_id: str, key: str) -> None:
        """A levételt jóváhagytad: ennek a tárgynak ezen az oldalon a törlés-gombjaira nem kérdez újra."""
        self.delisting = (item_id, key, time.time() + DELIST_SCOPE_SECONDS)
        self.current = (item_id, key)

    def _delist_scope(self, item_id: str, key: str) -> bool:
        if self.delisting is None:
            return False
        scope_item, scope_key, until = self.delisting
        if time.time() > until:
            self.delisting = None
            return False
        return (scope_item, scope_key) == (item_id, key)

    def _preapproved(self, label: str, url: str) -> bool:
        """Levétel közben: a törlés és a megerősítés gombja előre jóvá van hagyva (fizetős soha)."""
        if self.delisting is None or not self._delist_scope(*self.delisting[:2]):
            return False
        site = self.platforms.get(self.delisting[1])
        host = urllib.parse.urlsplit(url or "").hostname or ""
        if site is None or not any(host == d or host.endswith("." + d) for d in site.domains):
            return False
        folded = fold(label or "")
        return bool(_DELIST_OK_RE.search(folded)) and not _PAY_RE.search(folded)

    def delist_request(self, item: Item, key: str) -> str:
        """A modell első üzenete egy levételhez: az item_delist első lépését (a hirdetés megnyitása) a Jarvis maga
        végzi el, a modell ott folytatja. A modell nélküli levételt és a begyakorolt lépéseket a JarvisApp ekkorra
        már megpróbálta, ezeket nem kezdi újra. ToolError, ha nem lehet (pl. nincs fent)."""
        site = self.platforms[key]
        opened = self.open_delist(self._item(item.id), key, auto=False)
        return (f"Vedd le a(z) „{item.name}” hirdetést (azonosító: {item.id}) a(z) {site.name} oldalról "
                f"(platform: {key}), mert eladtuk. A levételt a felhasználó már jóváhagyta. Az item_delist lépést már "
                "elvégeztem (ne használd újra), az eredménye lent. A végén jelöld meg az item_mark eszközzel "
                f"(status: nincs fent).\n\n{opened}")

    def finish_delist(self, item_id: str, key: str, last_reply: str = "") -> bool:
        """Egy oldalról levétel után: ha nem jelölte meg levettnek, a hirdetés fent marad, megjegyzéssel."""
        self.current = None
        self.delisting = None
        self.autodelisted = None
        if self.recorder is not None and self.recorder.task == "delist":
            self.recorder = None
        try:
            item = self.store.get(item_id)
        except ItemError:
            return False
        self.store.set_busy(item_id, "")
        if item.platforms.get(key, {}).get("status") == NOT_POSTED:
            return True
        note = "A levétel nem sikerült: " + (last_reply or "nem sikerült végigcsinálni.")
        if item.platforms.get(key, {}).get("status") == POSTED:
            self.store.mark(item_id, key, POSTED, note=note[:300])
        return False

    # --- régóta fent lévő hirdetések (háttérmunka) ----------------------------------------------------------

    def stale_items(self, days: int, now: float | None = None) -> list[Item]:
        """Ami legalább `days` napja fent van, nem kelt el, és egy hete nem szóltunk róla. Amiről a
        háttérellenőrzés már szólt, hogy valahol elkeltnek vagy eltűntnek látszik, arról itt nem szól újra."""
        now = time.time() if now is None else now
        out = []
        for item in self.store.all():
            if item.sold or any(e.get("status") == POSTED and e.get("check") in (SOLD, GONE)
                                for e in item.platforms.values()):
                continue
            since = item.online_since()      # a felkerülés ideje (egy újbóli jelölés nem nullázza)
            if since is None or now - since < days * 86400:
                continue
            if item.reminded and now - item.reminded < 7 * 86400:
                continue
            out.append(item)
        return out

    def mark_reminded(self, ids: list[str]) -> None:
        for item_id in ids:
            try:
                self.store.set_reminded(item_id)
            except ItemError:
                pass

    def listing_block(self, item: Item, key: str = "") -> str:
        """A hirdetés adatai a feltöltéshez (az Alapadatokkal kiegészítve)."""
        lines = [f"Cím: {item.title}", f"Leírás: {item.description}",
                 f"Ár: {item.price} (forint)" if item.price else "Ár: nincs megadva"]
        for label, value in (("Mi ez", item.product), ("Kategória", item.category), ("Márka", item.brand),
                             ("Méret", item.size), ("Állapot", item.condition), ("Szín", item.color),
                             ("Anyag", item.material),
                             ("Átvétel helye (város)", item.location or self.settings.get("city", "")),
                             ("Szállítás", item.shipping or self.settings.get("shipping", ""))):
            if value:
                lines.append(f"{label}: {value}")
        if key == "vinted":
            lines.append(f"Csomagméret: {self.settings.get('parcel_size') or 'kicsi'}")
        site = self.platforms.get(key)
        if site is not None and len(item.photos) > site.max_photos:
            lines.append(f"Fotók: {site.max_photos} db (a(z) {site.name} legfeljebb ennyit vesz fel, ezért "
                         f"{len(item.photos)} helyett az első {site.max_photos}; a browser_upload_photos tölti fel "
                         "őket)")
        else:
            lines.append(f"Fotók: {len(item.photos)} db (a browser_upload_photos tölti fel őket)")
        return "\n".join(lines)

    def photos_for(self, item: Item, key: str = "") -> list[str]:
        """A tárgy fotói ehhez az oldalhoz: legfeljebb annyi, amennyit az oldal felvesz (a borítókép az első)."""
        paths = [str(p) for p in self.store.photo_paths(item.id)]
        site = self.platforms.get(key)
        return paths[:site.max_photos] if site is not None else paths

    # --- eszközök a Munka menü beszélgetéséhez -------------------------------------------------

    def _register(self, box: Toolbox) -> None:
        item = {"type": "string", "description": "A tárgy azonosítója (az item_list eszközből)."}
        text = {"type": "string"}
        platform = {"type": "string", "enum": list(self.platforms), "description": "A hirdetési oldal."}
        box.register(
            "item_list",
            "Felsorolja az eladásra szánt tárgyakat: azonosító, név, ár, állapot, és melyik oldalon van fent.",
            params({"status": {"type": "string",
                               "enum": ["", "vázlat", "kérdés", "árdöntés", "részben fent", "fent", "eladva"],
                               "description": "Opcionális: csak az ilyen állapotúak."}}),
            self.tool_list,
        )
        box.register(
            "item_get",
            "Egy tárgy minden adata: a hirdetés szövege, az eladó leírása, az árkutatás eredménye, "
            "és hol van fent.",
            params({"item": item}, ["item"]), self.tool_get,
        )
        box.register(
            "item_update",
            "Módosítja egy tárgy hirdetését vagy árát a felhasználó kérésére. Csak azt add meg, amin "
            "változtatni kell.",
            params({"item": item, "title": text, "description": text, "category": text, "brand": text,
                    "size": text, "condition": {"type": "string", "enum": CONDITIONS},
                    "color": text, "material": text, "location": text, "shipping": text,
                    "price": {"type": "integer", "description": "Ár forintban, pl. 4500."}}, ["item"]),
            self.tool_update,
        )
        if self.researcher is not None:
            box.register(
                "item_research",
                "Megnézi a hirdetési oldalakon, mennyiért kínálnak hasonlót, és ajánlást ad az árra "
                "(az árat nem írja át). Fél-egy percig tart.",
                params({"item": item}, ["item"]), self.tool_research,
            )
        if self.browser_ready and self.platforms:
            box.register(
                "item_post",
                "Elkezdi feltölteni a tárgyat egy hirdetési oldalra: a saját böngésződben megnyitja a "
                "hirdetésfeladást, és visszaadja a hirdetés adatait, a kitöltési tippeket és az oldalnézetet.",
                params({"item": item, "platform": platform}, ["item", "platform"]),
                self.tool_post, ephemeral=True,
            )
            box.register(
                "browser_upload_photos",
                "Feltölti a tárgy fotóit a böngészőben nyitott hirdetési űrlapra. Elem nélkül az oldal "
                "képfeltöltő mezőjét keresi meg; ha az nem megy, add meg a fotófeltöltő gomb számát. "
                "Fájlt máshogy nem tudsz feltölteni.",
                params({"element": {"type": "integer", "description": "Opcionális: a fotófeltöltő gomb száma."},
                        "item": {"type": "string",
                                 "description": "Opcionális: a tárgy azonosítója (alapból amit épp feltöltesz)."}}),
                self.tool_upload_photos, ephemeral=True,
            )
        if self.browser_ready and self.platforms:
            box.register(
                "item_delist",
                "Leveszi a tárgy hirdetését egy oldalról (pl. mert eladtuk): ha már begyakorolta, magától "
                "megcsinálja, különben megnyitja a hirdetést, és te fejezed be. Csak a felhasználó kérésére.",
                params({"item": item, "platform": platform}, ["item", "platform"]),
                self.tool_delist, ephemeral=True,
            )
        if self.messages.platforms:
            conversation = {"type": "string", "description": "A beszélgetés azonosítója (a messages_list eszközből) "
                                                             "vagy a vevő nevének része."}
            box.register(
                "messages_list",
                "Felsorolja a vevői üzeneteket (amiket a háttérben elolvastam): a beszélgetéseket a vevő nevével, a "
                "tárggyal és az utolsó üzenettel, vagy egy beszélgetés üzeneteit. Elküldeni nem tud semmit.",
                params({"conversation": {**conversation, "description": "Opcionális: csak ennek a beszélgetésnek az "
                                                                         "üzenetei."},
                        "unread": {"type": "boolean", "description": "Opcionális: csak az újak és az olvasatlanok."}}),
                self.messages.tool_list,
            )
            box.register(
                "message_draft",
                "Válaszvázlatot ír egy vevő üzenetére a tárgy adataiból (a felületen átírható). Elküldeni nem küldi el: "
                "azt csak a felhasználó teheti meg a felületen, a Küldés gombbal.",
                params({"conversation": conversation}, ["conversation"]), self.messages.tool_draft,
            )
        box.register(
            "item_mark",
            "Megjelöli egy tárgy állapotát: feltöltve egy oldalra (a hirdetés webcímével, ha látod), "
            "levéve egy oldalról („nincs fent”), vagy eladva.",
            params({"item": item, "status": {"type": "string", "enum": [POSTED, NOT_POSTED, "eladva"]},
                    "platform": {**platform, "description": "Melyik oldalon (eladvánál nem kell)."},
                    "url": {"type": "string", "description": "Opcionális: a hirdetés webcíme."}},
                   ["item", "status"]),
            self.tool_mark,
        )

    def _item(self, ref: str) -> Item:
        try:
            return self.store.find(ref)
        except ItemError as exc:
            raise ToolError(str(exc)) from None

    def _site_name(self, key: str) -> str:
        site = self.platforms.get(key) or PLATFORMS.get(key)
        return site.name if site else key

    def _line(self, item: Item) -> str:
        parts = [item.id, item.name, format_price(item.price), item.status]
        states = []
        drift = {d["platform"]: d for d in item.drift()}
        for key in item.targets:
            entry = item.platforms.get(key, {})
            state = f"{self._site_name(key)}: {entry.get('status') or 'még nincs fent'}"
            if "price" in drift.get(key, {}):
                state += f" (ott még {format_price(drift[key]['price'])})"
            if entry.get("status") == POSTED and entry.get("check") in CHECK_WORDS:
                state += f" ({CHECK_WORDS[entry['check']]})"
            states.append(state)
        if states:
            parts.append(", ".join(states))
        if item.busy:
            parts.append("épp dolgozom rajta")
        return " | ".join(parts)

    def tool_list(self, status: str = "") -> str:
        items = [i for i in self.store.all() if not status or i.status == status]
        if not items:
            return ("Nincs ilyen állapotú tárgy." if status else
                    "Még nincs tárgy. A felhasználó az Adás/Vétel fülön adhat hozzá (oldalak, fotók, leírás).")
        return "\n".join([f"{len(items)} tárgy:"] + [self._line(i) for i in items])

    def tool_get(self, item: str) -> str:
        found = self._item(item)
        lines = [self._line(found), f"Az eladó leírása: {found.prompt or '–'}", self.listing_block(found)]
        for key, entry in found.platforms.items():
            if entry.get("note"):
                lines.append(f"{self._site_name(key)} megjegyzés: {entry['note']}")
        lines.append(research_summary(found.research))
        return "\n".join(lines)

    def tool_update(self, item: str, **values: Any) -> str:
        found = self._item(item)
        values = {k: v for k, v in values.items() if v is not None}
        if not values:
            raise ToolError("Nem adtál meg módosítandó adatot.")
        try:
            updated = self.store.update(found.id, user=True, **values)
        except ItemError as exc:
            raise ToolError(str(exc)) from None
        names = ", ".join(FIELD_NAMES.get(k, k) for k in values)
        note = self.drift_note(updated, set(values))
        return f"Módosítva ({found.name}): {names}" + (f". {note}" if note else "")

    def drift_note(self, item: Item, changed: set[str]) -> str:
        """Ha a fent lévő hirdetésekben még a régi ár vagy cím van, őszintén megmondja, hogy csak itt írta át:
        a hirdetéseket magától nem szerkeszti. Üres, ha a most átírt mezők sehol nem térnek el. A modell
        (item_update) és a felület is ezt kapja, amikor kézzel átírod (gui /api/work/update, api PATCH)."""
        parts = []
        for entry in item.drift():
            old = []
            if "price" in changed and "price" in entry:
                old.append(format_price(entry["price"]))
            if "title" in changed and "title" in entry:
                old.append(f"a régi cím („{entry['title']}”)")
            if old:
                where = ON_SITE.get(entry["platform"]) or f"a(z) {self._site_name(entry['platform'])} oldalon"
                parts.append(f"{where} még " + " és ".join(old))
        if not parts:
            return ""
        return ("Csak itt írtam át; " + ", ".join(parts) + ". A fent lévő hirdetést magamtól nem szerkesztem: "
                "ott kézzel kell átírni (vagy levenni, és újra feltölteni).")

    def tool_research(self, item: str) -> str:
        found = self._item(item)
        self.store.set_busy(found.id, "Árkutatás…")
        try:
            found = self.researcher.research(found.id, only=found.targets)
        except ItemError as exc:
            raise ToolError(str(exc)) from None
        finally:
            self.store.set_busy(found.id, "")
        return research_summary(found.research) + "\n" + price_verdict(found).text

    def _open_entry(self, key: str) -> tuple[str, str]:
        """A modell feltöltésénél: ha tudjuk, hol kezdődik az űrlap, közvetlenül azt nyitja meg. Visszaad: (a
        kiindulás webcíme, az oldalnézet); az oldalnézet üres, ha a megszokott kezdőoldalt kell megnyitni (nincs
        megjegyzett cím, bejelentkezést kér, vagy az oldal már nincs meg: akkor el is felejti)."""
        site = self.platforms[key]
        entry = self.entry_url(key)
        if not entry or url_shape(entry) == url_shape(site.new_url):
            return site.new_url, ""
        browser = self.toolbox.browser
        try:
            page = browser.open(entry)
            problem = entry_problem(browser, entry)
        except BrowserError:
            page, problem = "", "gone"
        if not problem:
            return entry, page
        if problem == "gone":
            self.forget_entry(key)
        return site.new_url, ""

    def tool_post(self, item: str, platform: str) -> str:
        found = self._item(item)
        site = self.platforms.get(platform)
        if site is None:
            raise ToolError(f"Ismeretlen oldal: {platform}. Lehet: {', '.join(self.platforms)}")
        missing = [name for name, ok in (("cím", found.title), ("leírás", found.description),
                                         ("ár", found.price), ("fotó", found.photos)) if not ok]
        if missing:
            raise ToolError(f"A feltöltéshez még hiányzik: {', '.join(missing)}. Kérdezd meg a felhasználót.")
        if self.checking == (found.id, platform):
            raise ToolError("Ezt a hirdetést már közzétetted (a felhasználó jóváhagyta), újra ne töltsd fel, mert "
                            "kétszer kerülne fel. Nézd meg a nyitott oldalt, és ha fent van, jelöld meg (item_mark).")
        # (a közzétételek közti szünetet nem itt, hanem a közzététel gombja előtt várja ki: _before_publish)
        self.store.clear_note(found.id, platform)   # az előző próbálkozás megjegyzése már nem érvényes
        self.current = (found.id, platform)
        self.approved = False
        self.begin_trail()
        self.store.set_busy(found.id, f"Feltöltés: {site.name}…")
        start, page = self._open_entry(platform)
        self.start_recording(found, platform, "post", start)
        if not page:
            try:
                page = self.toolbox.browser.open(site.new_url)
            except BrowserError as exc:
                self.recorder = None
                raise ToolError(str(exc)) from None
        where = ("Közvetlenül a hirdetésfeladás oldalát nyitottam meg (legutóbb itt kezdődött az űrlap): a "
                 "hirdetésfeladás gombját nem kell megkeresned.\n" if start != site.new_url else "")
        return (f"A hirdetés adatai ({found.id}):\n{self.listing_block(found, platform)}\n\n"
                f"Tippek ({site.name}): {site.hints}\n{where}"
                "A végső feltöltés előtt a rendszer jóváhagyást kér a felhasználótól. Ha bejelentkezés "
                "vagy robot-ellenőrzés kell, állj meg, és kérd meg a felhasználót." + KEEP_ABOVE + page)

    def tool_upload_photos(self, element: int | None = None, item: str = "") -> str:
        ref = item or (self.current[0] if self.current else "")
        if not ref:
            raise ToolError("Melyik tárgy fotóit töltsem fel? Add meg az azonosítóját.")
        found = self._item(ref)
        browser = self.toolbox.browser
        host = browser.current_host()
        sites = [site.key for site in self.platforms.values()
                 if any(host == domain or host.endswith("." + domain) for domain in site.domains)]
        known = bool(sites)
        # az oldal fotólimitje: amit épp feltöltesz, különben amelyiken a böngésző áll
        key = self.current[1] if self.current and (self.current[1] in sites or not sites) else next(iter(sites), "")
        paths = self.photos_for(found, key)
        if not paths:
            raise ToolError("Ennek a tárgynak nincs fotója.")
        if not known and not self.confirm(
                f"Jarvis feltöltené a(z) „{found.name}” {len(paths)} fotóját ide: {host or 'ismeretlen oldal'}"):
            return "A felhasználó nem engedélyezte a fotók feltöltését."
        try:
            return browser.upload_files(paths, element)
        except BrowserError as exc:
            raise ToolError(str(exc)) from None

    def tool_mark(self, item: str, status: str, platform: str = "", url: str = "") -> str:
        found = self._item(item)
        delisted = status == NOT_POSTED and bool(platform) and self._delist_scope(found.id, platform)
        try:
            if status == "eladva":
                found = self.store.mark(found.id, sold=True)
            else:
                if not platform:
                    raise ToolError("Melyik oldalon? Add meg a platform paramétert.")
                found = self.store.mark(found.id, platform, status, url, note=DELISTED_NOTE if delisted else "")
        except ItemError as exc:
            raise ToolError(str(exc)) from None
        text = f"Megjelölve: {found.name} – {status}"
        if platform:
            text += f" ({self._site_name(platform)})"
        learned = ""
        recorder = self.recorder
        if platform and recorder is not None and recorder.platform == platform:
            task = {POSTED: "post", NOT_POSTED: "delist"}.get(status, "")
            if task == recorder.task and recorder.published:
                learned = self.finish_recording(found.id, platform, task)
        if status == NOT_POSTED and platform and self.delisting and self.delisting[1] == platform:
            self.delisting = None
        if status == "eladva":
            still = [self._site_name(k) for k, v in found.platforms.items() if v.get("status") == POSTED]
            if still:
                text += (". Ne felejtsd el levenni innen: " + ", ".join(still) + " (kérdezd meg, levegyem-e; "
                         "ha igen, az item_delist eszközzel leveszem)")
        return text + "." + learned

    def tool_delist(self, item: str, platform: str) -> str:
        return self.open_delist(self._item(item), platform)

    def open_delist(self, found: Item, platform: str, auto: bool = True) -> str:
        """Az item_delist eszköz: ha lehet, modell nélkül vagy begyakorolt lépésekkel leveszi (`auto`), különben
        megnyitja a hirdetést, és a modell fejezi be."""
        site = self.platforms.get(platform)
        if site is None:
            raise ToolError(f"Ismeretlen oldal: {platform}. Lehet: {', '.join(self.platforms)}")
        entry = found.platforms.get(platform) or {}
        if entry.get("status") != POSTED:
            raise ToolError(f"A nyilvántartásom szerint a(z) {site.name} oldalon nincs fent ez a hirdetés.")
        if not self._delist_scope(found.id, platform):
            # nem a Jarvis indította (pl. a beszélgetésből jön): egyszer megkérdezzük
            if not self.confirm(f"Jarvis levenné a(z) „{found.name}” hirdetést innen: {site.name}."):
                return "A felhasználó nem engedélyezte a levételt."
            self.begin_delist(found.id, platform)
        self.current = (found.id, platform)      # (a szünetet a törlés gombja előtt várja ki)
        self.begin_trail()
        self.store.set_busy(found.id, f"Levétel: {site.name}…")
        prefix, clicked = "", False
        if auto and self.can_autodelist(found.id, platform) and self.autodelisted != (found.id, platform):
            delisted = self.autodelist(found.id, platform)
            answer = self._after_autodelist(found, platform, delisted)
            if answer is not None:
                return answer
            found = self.store.get(found.id)
            entry = found.platforms.get(platform) or {}
            clicked = delisted.status == "uncertain"     # a Törlésre már rákattintott: csak meg kell nézni
            prefix = (f"A Törlésre rákattintottam, de nem biztos, hogy sikerült ({delisted.reason}). Nézd meg az "
                      "oldalt: ha a hirdetés már nincs fent, csak jelöld meg; ha még fent van, fejezd be.\n" if clicked
                      else f"A modell nélküli levétel nem ment végig ({delisted.reason}); folytasd te.\n")
        if auto and not clicked and self.can_replay(found.id, platform, "delist"):
            outcome = self.replay(found.id, platform, "delist")
            if outcome.status == "done":
                self.store.mark(found.id, platform, NOT_POSTED, note=DELISTED_NOTE)
                self.store.set_busy(found.id, "")
                self.delisting = self.current = None
                return f"Levettem a(z) {site.name} oldalról (begyakorolt lépésekkel). Megjelöltem: nincs fent."
            if outcome.status == "declined":
                self.store.set_busy(found.id, "")
                return "A felhasználó nem engedélyezte a levételt."
            prefix += f"A begyakorolt lépések nem vitték végig ({outcome.reason}); folytasd te.\n"
        url = entry.get("url", "")
        if url:
            self.start_recording(found, platform, "delist", url)
        try:
            page = self.toolbox.browser.open(url or site.home)
        except BrowserError as exc:
            self.recorder = None
            raise ToolError(str(exc)) from None
        where = (f"Megnyitottam a hirdetést: {url}" if url else
                 f"A hirdetés webcímét nem tudom, a(z) {site.name} főoldalát nyitottam meg: keresd meg a saját "
                 "hirdetéseid között (Hirdetéseim, Profilom).")
        return (f"{prefix}Levétel ({site.name}): {found.name} (azonosító: {found.id}). {where}\n"
                f"Tippek: {site.delist_hints}\n"
                "Ha bejelentkezés vagy robot-ellenőrzés kell, állj meg, és kérd meg a felhasználót. Ha kész, jelöld "
                f"meg: item_mark (status: nincs fent, platform: {platform})." + KEEP_ABOVE + page)
