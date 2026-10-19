"""Vinted: a hirdetésfeladás kitöltése modell nélkül; a modell csak egy-egy rövid kérdésre felel.

Eddig a Vinted űrlapját a modell töltötte ki lépésenként: minden kattintás egy modellkör volt egy 1–2 ezer
tokenes oldalnézettel, egy feltöltés 25–40 kör, és a kategóriafában, a márkakeresőben vagy a méreteknél könnyen
eltévedt. Itt a kód tudja, hol mi van:
- a fotókat, a címet, a leírást és az árat beírja,
- az állapotot, a színt és a csomagméretet a Vinted saját lehetőségei közül választja ki (a magyar és az angol
  felületen is: „Nagyon jó” = „Very good”, „fekete” = „Black”),
- a méretet és a márkát a lista feliratai alapján keresi meg („40” = „EU 40” vagy „L / 40 / 12”),
- a modellt csak ott kérdezi, ahol tényleg dönteni kell: a kategóriafa egy-egy szintjén (pl. Women › Clothing ›
  Jeans), vagy ha a márka, a méret vagy a szín nem egyértelmű. Ezek kis, számozott feleletválasztós kérdések
  (pár száz token, egy-két másodperc), nem egy egész oldal.

Amit a modell egyszer kiválasztott (egy tárgyfajta kategóriaútját), azt a sikeres feltöltés után megjegyzi, és
legközelebb modell nélkül választja. A közzététel előtt ugyanúgy jóváhagyást kér, és a kérdésben látod, mit
választott. Ha valamit nem talál, vagy a Vinted nem fogadja el a hirdetést, a modell a félig kitöltött űrlapon
folytatja (nem kezdi elölről); ha be kell jelentkezni, vagy robot-ellenőrzés jön, megáll, és szól.

A böngésző közben „modell nélküli” üzemmódban van, mint a begyakorolt lépéseknél: emberi tempóban dolgozik
(lásd humanize.py).
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus, urljoin, urlsplit

from .browser import BrowserError, is_risky
from .llm import OllamaError
from .replay import field_for, norm_text, url_shape, vkey
from .textutil import fold

MAX_LEVELS = 6             # a kategóriafa (vagy egy méretválasztó) legfeljebb ennyi szint
MAX_ASK_OPTIONS = 60       # a modell legfeljebb ennyi lehetőséget lát egy kérdésben
ASK_MAX_TOKENS = 16        # a válasz egy szám ({"choice": 3}): ennél hosszabb már csak beragadt válasz lehet
PUBLISH_WAIT = 25.0        # a közzététel után ennyi ideig várja, hogy a Vinted továbbengedjen
LABELS = {"photos": "Fotók", "title": "Cím", "description": "Leírás", "category": "Kategória", "brand": "Márka",
          "size": "Méret", "condition": "Állapot", "color": "Szín", "price": "Ár", "parcel_size": "Csomagméret"}

# A mezők a Vinted űrlapján: a mezőnév (name, id) szavai, és a felirat (magyarul vagy angolul).
FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "title": (("title",), ("title", "cim")),
    "description": (("description",), ("description", "describe your item", "leiras", "ird le")),
    "category": (("category",), ("category", "kategoria")),
    "brand": (("brand",), ("brand", "search brands", "marka", "markak keresese")),
    "size": (("size",), ("size", "meret")),
    "condition": (("condition", "status"), ("condition", "allapot")),
    "color": (("color", "colour"), ("colour", "colours", "color", "colors", "szin", "szinek")),
    "price": (("price",), ("price", "ar")),
}
TEXT_KINDS = {"input:text", "input:search", "searchbox", "textbox", "combobox", "textarea", "input:number",
              "input:tel"}
BUTTON_KINDS = {"button", "input:submit", "input:button"}
# Ami egy lenyitott listában választható (az onclick-es div, li is).
OPTION_KINDS = {"button", "radio", "input:radio", "checkbox", "input:checkbox", "option", "menuitem",
                "menuitemradio", "menuitemcheckbox", "switch", "link", "input:button", "div", "li", "span",
                "label", "td", "p"}
# Ezek után a választás kész (a gomb viszont lehet egy újabb szint, pl. Women → Clothing).
FINAL_KINDS = {"radio", "input:radio", "checkbox", "input:checkbox", "option", "menuitemradio", "menuitemcheckbox"}
# Egy lista vezérlőgombjai, nem lehetőségek (vissza, bezárás, keresés…). Csak a teljes felirat: a „Save the Duck”
# márka attól még márka.
_CONTROL_RE = re.compile(r"^(vissza|back|bezar|bezaras|close|megse|cancel|torles|clear|clear all|kesz|done|ok|"
                         r"rendben|alkalmaz|apply|mentes|save|tovabb|next|elozo|previous|kereses|search|"
                         r"(back to|vissza a|vissza az) .*)$")
_DONE_RE = re.compile(r"^(kesz|done|ok|rendben|alkalmaz|apply|bezar|bezaras|close)$")
# Csak a süti-ablak gombja: egy „Elfogadom” a feltételek alatt NEM ez (azt a felhasználónak kell elfogadnia).
_CONSENT_RE = re.compile(r"^(accept all|accept all cookies|allow all|allow all cookies|az osszes elfogadasa|"
                         r"osszes elfogadasa|minden suti elfogadasa|sutik elfogadasa|az osszes suti elfogadasa)$")
_LOGIN_RE = re.compile(r"\b(bejelentkez|belepes|regisztr|log ?in|sign ?in|sign ?up)")
_LOGIN_URL_RE = re.compile(r"(signup|sign_up|login|log_in|session|/auth|oauth)", re.I)
_PUBLISH_WORDS = ("feltoltes", "feltoltom", "upload", "kozzetetel", "kozzeteszem", "hirdetes feladasa",
                  "meghirdetem", "list item", "list it")
_PHOTO_RE = re.compile(r"(foto|kep|photo|image|picture)")
_CUSTOM_BRAND_RE = re.compile(r"\b(hasznal|use|hozzaad|add|letrehoz|create|uj marka|new brand)")

# Az állapotaink a Vinteden: a lehetőség azonosítója (condition-N) és a felirat eleje magyarul és angolul.
# A „Hibás”-nak a Vinteden nincs párja: a legalacsonyabb (Satisfactory / Kielégítő), a hibát a leírás mondja el.
CONDITIONS: dict[str, tuple[int, tuple[str, ...]]] = {
    "Új, címkével": (6, ("new with tags", "uj cimkevel")),
    "Új": (1, ("new without tags", "uj cimke nelkul")),
    "Nagyon jó": (2, ("very good", "nagyon jo")),
    "Jó": (3, ("good", "jo")),
    "Elfogadható": (4, ("satisfactory", "kielegito", "elfogadhato")),
    "Hibás": (4, ("satisfactory", "kielegito", "elfogadhato")),
}
# A színek magyarul (ékezet nélkül) és a Vinted angol felirata.
COLORS = {
    "fekete": "black", "feher": "white", "szurke": "grey", "barna": "brown", "bezs": "beige", "rozsaszin": "pink",
    "lila": "purple", "piros": "red", "voros": "red", "sarga": "yellow", "kek": "blue", "zold": "green",
    "narancs": "orange", "narancssarga": "orange", "ezust": "silver", "arany": "gold", "tobbszinu": "multi",
    "tarka": "multi", "szines": "multi", "khaki": "khaki", "turkiz": "turquoise", "krem": "cream",
    "barack": "apricot", "korall": "coral", "bordo": "burgundy", "rozsa": "rose", "orgona": "lilac",
    "levendula": "lilac", "vilagoskek": "light blue", "sotetkek": "navy", "tengereszkek": "navy",
    "sotetzold": "dark green", "mustar": "mustard", "menta": "mint", "gray": "grey",
}
# A csomagméret: package-size-N, és a felirat első szava.
PARCELS = {"kicsi": (1, ("small", "kicsi", "kis")), "kozepes": (2, ("medium", "kozepes")),
           "nagy": (3, ("large", "nagy"))}
# A kategóriaút gyakori magyar szavai és a Vinted angol felirata (a többit a modell dönti el).
CATEGORY_WORDS = {
    "no": "women", "nok": "women", "noi": "women", "ferfi": "men", "ferfiak": "men", "gyerek": "kids",
    "gyerekek": "kids", "gyermek": "kids", "ruha": "clothing", "ruhak": "clothing", "ruhazat": "clothing",
    "cipo": "shoes", "cipok": "shoes", "taska": "bags", "taskak": "bags", "kiegeszito": "accessories",
    "kiegeszitok": "accessories", "otthon": "home", "elektronika": "electronics", "szorakozas": "entertainment",
    "sport": "sport", "szepseg": "beauty",
}
# A méret szavai: betűméret, szám (fél méret is), W30-féle; a magyar rag („M-es”, „38-as”) nem számít.
_SIZE_TOKEN_RE = re.compile(r"[a-z]*\d+(?:[.,]5)?|[a-z]+")
_SIZE_SUFFIXES = {"es", "as", "os", "is", "us", "meret", "meretu", "size"}

ASK_SYSTEM = (
    "Egy eladó helyett hirdetést töltesz fel a Vintedre, és egy lenyíló listából kell választanod. Megkapod a "
    "tárgy adatait, a mezőt, és a lehetőségeket számozva, ahogy az oldalon szerepelnek (lehet, hogy angolul). "
    "Azt az egyet válaszd, amelyik a tárgyhoz illik; a kategóriáknál azt az ágat, amelyik alá a tárgy tartozik "
    "(nézd a nemét, a fajtáját és a leírását is). Ha a 0 is a lehetőségek között van, és egyik sem illik, a 0-t "
    "válaszd. Csak a lehetőség számát add meg."
)


class Missing(Exception):
    """Egy mezőt nem sikerült kitölteni (az ok a modellnek és a hibacsomagnak szól)."""


@dataclass
class FillOutcome:
    status: str                   # done | uncertain | declined | partial | blocked | miss
    reason: str = ""
    url: str = ""
    published: bool = False       # megtörtént-e a közzététel kattintása (jóváhagytad)
    filled: list[str] = field(default_factory=list)   # mi van már kitöltve (ha a modellnek kell folytatnia)
    learned: dict[str, dict[str, list[dict[str, str]]]] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)      # a döntések (a hibacsomagba)
    profile: str = ""             # a Vinted-profilod címe, ha útközben kiderült (a levételhez jól jön)


# --- segédek (böngésző nélkül is tesztelhetők) --------------------------------------------------------

def words(text: str) -> list[str]:
    return norm_text(text).split()


def starts(label: str, phrase: str) -> bool:
    """A felirat ezzel a szóval (szavakkal) kezdődik-e (ékezet és írásjel nélkül)."""
    head = words(phrase)
    return bool(head) and words(label)[:len(head)] == head


def short(label: str, limit: int = 40) -> str:
    """Egy felirat röviden (a Vinted az állapotnál a magyarázatot is a feliratba teszi)."""
    text = " ".join((label or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return (cut or text[:limit]) + "…"


def elem_key(el: dict[str, Any]) -> tuple[str, str, str]:
    """Egy elem azonosítása két letapogatás között (a szám közben megváltozik)."""
    return el.get("kind", ""), norm_text(el.get("label", ""))[:60], el.get("field", "")


def field_words(text: str) -> list[str]:
    """A mezőnév szavai: a „package-size-1” → package, size, 1."""
    return [w for w in re.split(r"[\s_\-.:/\[\]]+", (text or "").lower()) if w]


def find_field(elements: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """Egy mező (cím, kategória, márka…) a letapogatott elemek közül: előbb a mezőnév (a Vinteden pl.
    name="title" id="title"), aztán a felirat alapján. A más mezőhöz tartozót (a „package-size” nem a méret)
    és a fejléc keresőjét kihagyja."""
    keys, labels = FIELDS[name]
    best, best_score = None, 0.0
    for el in elements:
        if el.get("chrome") or el.get("disabled") or el.get("sensitive"):
            continue
        kind = el.get("kind", "")
        if kind not in TEXT_KINDS and kind not in BUTTON_KINDS and not kind.startswith("input:"):
            continue
        if kind in ("input:radio", "input:checkbox", "input:file", "input:password"):
            continue
        names = field_words(el.get("field", ""))
        if names and names[-1].isdigit():
            continue      # egy lista lehetősége (condition-2, catalog-1904, package-size-1), nem a mező
        owner = field_for(el.get("field", ""))
        if owner and owner != name and (owner in FIELDS or owner == "parcel_size"):
            continue
        score = 0.0
        if any(word in keys for word in names):
            score = 3
        else:
            label = norm_text(el.get("label", ""))
            if any(label == norm_text(alias) for alias in labels):
                score = 2
            elif name in ("description", "brand") and any(starts(label, alias) for alias in labels[1:]):
                score = 1
        if score and kind in TEXT_KINDS:
            score += 0.5      # a szövegmező (vagy a lenyíló lista mezője) erősebb, mint egy ugyanígy nevezett gomb
        if score > best_score:
            best, best_score = el, score
    return best


def is_option(el: dict[str, Any], name: str = "") -> bool:
    """Választható lehetőség-e egy lenyitott listában (a mező, a vezérlőgomb és a fejléc nem)."""
    if el.get("chrome") or el.get("disabled") or el.get("kind", "") not in OPTION_KINDS:
        return False
    label = norm_text(el.get("label", ""))
    if not label or _CONTROL_RE.match(label):
        return False
    owner = field_for(el.get("field", ""))
    return not (owner and owner != name and (owner in FIELDS or owner == "parcel_size"))


def is_writable(el: dict[str, Any] | None) -> bool:
    return bool(el) and el.get("kind", "") in TEXT_KINDS and not el.get("readonly")


def same_option(options: list[dict[str, Any]], step: dict[str, str]) -> dict[str, Any] | None:
    """Egy megjegyzett lépés lehetősége: ugyanaz a mezőnév (pl. catalog-1845), vagy ugyanaz a felirat."""
    if step.get("field"):
        for option in options:
            if option.get("field") == step["field"]:
                return option
    wanted = norm_text(step.get("label", ""))
    return next((o for o in options if wanted and norm_text(o.get("label", "")) == wanted), None)


def condition_option(options: list[dict[str, Any]], value: str) -> tuple[dict[str, Any] | None, str]:
    """Az állapot lehetősége (condition-N, vagy a felirat eleje) és a felirat rövid alakja („Very good”)."""
    entry = CONDITIONS.get(value)
    if entry is None:
        return None, ""
    number, phrases = entry
    for option in options:
        if field_words(option.get("field", ""))[-2:] in (["condition", str(number)], ["status", str(number)]):
            phrase = next((p for p in phrases if starts(option["label"], p)), "")
            return option, " ".join(option["label"].split()[:len(phrase.split())]) if phrase else short(option["label"])
    for phrase in phrases:
        for option in options:
            if starts(option.get("label", ""), phrase):
                return option, " ".join(option["label"].split()[:len(phrase.split())])
    return None, ""


def color_values(value: str) -> list[str]:
    """A tárgy színei külön (a Vinteden legfeljebb kettő): „fekete-fehér” → fekete, fehér."""
    parts = re.split(r"\s*(?:,|/|&|\+|;|-|\bes\b|\band\b)\s*", fold(value or "").lower())
    return [p for p in (norm_text(part) for part in parts) if p][:2]


def color_option(options: list[dict[str, Any]], color: str) -> dict[str, Any] | None:
    """Egy szín lehetősége magyarul vagy angolul („kék” = „Kék” vagy „Blue”; „világos kék” = „Light blue”)."""
    color = norm_text(color)
    joined = color.replace(" ", "")
    wanted = [w for w in dict.fromkeys((color, joined, COLORS.get(color, ""), COLORS.get(joined, ""))) if w]
    for want in wanted:
        for option in options:
            if norm_text(option.get("label", "")) == want:
                return option
    for want in wanted:
        hits = [o for o in options if starts(o.get("label", ""), want)]
        if len(hits) == 1:
            return hits[0]
    return None


def size_tokens(text: str) -> list[str]:
    tokens = [t.replace(",", ".") for t in _SIZE_TOKEN_RE.findall(fold(text or "").lower())]
    useful = [t for t in tokens if t not in _SIZE_SUFFIXES]
    return useful or tokens


def size_options(options: list[dict[str, Any]], value: str) -> list[dict[str, Any]]:
    """A méretnek megfelelő lehetőségek: amelyik feliratában minden szava benne van („40” → „EU 40”,
    „L” → „L / 40 / 12”). Ha van pontosan egyező felirat, csak az."""
    wanted = size_tokens(value)
    if not wanted:
        return []
    hits = [o for o in options if set(wanted) <= set(size_tokens(o.get("label", "")))]
    exact = [o for o in hits if size_tokens(o.get("label", "")) == wanted]
    return exact or hits


def brand_option(options: list[dict[str, Any]], brand: str) -> dict[str, Any] | None:
    """A márka a keresés találatai közül: pontos egyezés, vagy ha csak egy kezdődik vele."""
    wanted = norm_text(brand)
    if not wanted:
        return None
    exact = [o for o in options if norm_text(o.get("label", "")) == wanted]
    if exact:
        return exact[0]
    prefix = [o for o in options if starts(o.get("label", ""), wanted)
              and not _CUSTOM_BRAND_RE.search(norm_text(o.get("label", "")))]
    return prefix[0] if len(prefix) == 1 else None


def custom_brand_option(options: list[dict[str, Any]], brand: str) -> dict[str, Any] | None:
    """Ha a Vinted nem ismeri a márkát: a „Use “X” as brand”-féle lehetőség, ha van."""
    wanted = norm_text(brand)
    for option in options:
        label = norm_text(option.get("label", ""))
        if wanted and wanted in label and _CUSTOM_BRAND_RE.search(label):
            return option
    return None


def parcel_option(elements: list[dict[str, Any]], size: str) -> tuple[dict[str, Any] | None, str]:
    """A csomagméret lehetősége (package-size-N, vagy a felirat első szava) és a rövid felirata."""
    number, phrases = PARCELS.get(norm_text(size), PARCELS["kicsi"])
    group = [e for e in elements if not e.get("chrome") and not e.get("disabled")
             and e.get("kind", "") in OPTION_KINDS and field_for(e.get("field", "")) == "parcel_size"]
    for option in group:
        if field_words(option.get("field", ""))[-1:] == [str(number)]:
            return option, option["label"].split()[0] if option.get("label") else ""
    for phrase in phrases:
        for option in group:
            if starts(option.get("label", ""), phrase):
                return option, option["label"].split()[0]
    return None, ""


def category_words(category: str) -> set[str]:
    """A tárgy kategóriaútjának szavai és az angol megfelelőjük (Nők > Ruhák > Pólók → nok, women, ruhak…)."""
    out: set[str] = set()
    for part in re.split(r"[>›»/|,]", category or ""):
        part = norm_text(part)
        if part:
            out.add(part)
            if part in CATEGORY_WORDS:
                out.add(CATEGORY_WORDS[part])
    return out


def category_option(options: list[dict[str, Any]], wanted: set[str]) -> dict[str, Any] | None:
    """Ha pontosan egy lehetőség felirata egyezik a kategóriaút egy szavával, az (különben a modell dönt)."""
    hits = [o for o in options if norm_text(o.get("label", "")) in wanted]
    return hits[0] if len(hits) == 1 else None


def publish_button(elements: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A közzététel gombja („Feltöltés”, „Upload”); a fotófeltöltő és a piszkozat mentése nem az."""
    loose = None
    for el in elements:
        if el.get("chrome") or el.get("disabled") or el.get("kind", "") not in BUTTON_KINDS:
            continue
        label = norm_text(el.get("label", ""))
        if not label or _PHOTO_RE.search(label) or not is_risky(el.get("label", "")):
            continue
        if label in _PUBLISH_WORDS:
            return el
        if loose is None and any(starts(label, word) for word in _PUBLISH_WORDS):
            loose = el
    return loose


def consent_button(elements: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A süti-ablak elfogadó gombja (Vinted: OneTrust, „Accept all” / „Az összes elfogadása”). Mást (pl. a
    felhasználási feltételek elfogadását) nem fogad el helyetted."""
    for el in elements:
        if el.get("kind", "") not in BUTTON_KINDS or el.get("disabled"):
            continue
        if "onetrust-accept" in el.get("field", "") or _CONSENT_RE.match(norm_text(el.get("label", ""))):
            return el
    return None


def login_needed(url: str, elements: list[dict[str, Any]]) -> bool:
    """Bejelentkezést kér-e az oldal (a hirdetésfeladás helyett)."""
    if _LOGIN_URL_RE.search(url or ""):
        return True
    texts = " ".join(norm_text(e.get("label", "")) for e in elements if not e.get("chrome"))
    return bool(_LOGIN_RE.search(texts))


def choice_key(values: dict[str, str]) -> str:
    """A kategóriaút megjegyzésének kulcsa: a tárgy fajtája, a kategóriája és hogy mi ez, a szín, a márka és a
    méret nélkül (a „fekete póló” és a „fehér póló” ugyanoda kerül, a „skinny farmer” és a „hosszú farmer” nem)."""
    drop = set(words(values.get("brand", ""))) | set(size_tokens(values.get("size", ""))) | _SIZE_SUFFIXES
    drop |= set(COLORS) | {w for name in COLORS.values() for w in name.split()}
    product = " ".join(w for w in words(values.get("product", "")) if w not in drop)
    return "|".join((vkey(values.get("kind", "")), vkey(values.get("category", "")), vkey(product)))


# --- a megjegyzett választások ---------------------------------------------------------------------------

class ChoiceMemory:
    """Amit a kitöltő egyszer kiválasztott (pl. egy tárgyfajta kategóriaútja), mezőnként és kulcsonként."""

    VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) and data.get("version") == self.VERSION else {}

    def get(self, name: str, key: str) -> list[dict[str, str]] | None:
        steps = (self._load().get(name) or {}).get(key)
        return steps if isinstance(steps, list) and steps else None

    def update(self, learned: dict[str, dict[str, list[dict[str, str]]]]) -> None:
        if not learned:
            return
        data = self._load() or {"version": self.VERSION}
        for name, entries in learned.items():
            data.setdefault(name, {}).update(entries)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


# --- a kitöltő ------------------------------------------------------------------------------------------------

class VintedFiller:
    def __init__(self, browser: Any, llm: Any, values: dict[str, str], photos: list[str],
                 memory: ChoiceMemory | None = None, check: Callable[[], None] | None = None,
                 progress: Callable[[str], None] | None = None, publish_wait: float | None = None):
        self.browser = browser
        self.llm = llm
        self.values = values
        self.photos = photos
        self.memory = memory
        self.check = check or (lambda: None)
        self.progress = progress or (lambda _text: None)
        self.publish_wait = PUBLISH_WAIT if publish_wait is None else publish_wait
        self.summary: list[str] = []     # a jóváhagyó ablakba: „Kategória: Women > Clothing > Jeans”
        self.filled: list[str] = []      # mi sikerült (ha a modellnek kell folytatnia)
        self.missing: list[str] = []     # mi nem sikerült, és miért
        self.log: list[str] = []
        self.learned: dict[str, dict[str, list[dict[str, str]]]] = {}

    # --- a menet ---

    def run(self, start_url: str) -> FillOutcome:
        """Kitölti és (jóváhagyással) közzéteszi a hirdetést. Közben a böngésző emberi tempóban dolgozik."""
        previous = getattr(self.browser, "auto", False)
        self.browser.auto = True
        try:
            return self._run(start_url)
        finally:
            self.browser.auto = previous

    def _outcome(self, status: str, reason: str = "", published: bool = False, url: str | None = None) -> FillOutcome:
        return FillOutcome(status, reason, url=self.browser.url if url is None else url, published=published,
                           filled=list(self.filled), learned=self.learned, log=list(self.log))

    def _run(self, start_url: str) -> FillOutcome:
        try:
            self.progress("a hirdetési űrlap megnyitása")
            self.browser.open(start_url)
            if self.browser.challenged():
                return self._outcome("blocked", "A Vinted robot-ellenőrzést (captcha) kér. Oldd meg a böngészőablakomban, "
                                                "utána az Újra gombbal folytatom.")
            elements = self.browser.scan()
            consent = consent_button(elements)
            if consent is not None:
                self._click(consent)
                elements = self.browser.scan()
        except (BrowserError, Missing) as exc:
            return self._outcome("miss", f"nem nyílt meg a hirdetésfeladás ({str(exc).splitlines()[0][:160]})")
        if find_field(elements, "title") is None:
            if login_needed(self.browser.url, elements):
                return self._outcome("blocked", "Be kell jelentkezned a Vintedre a böngészőablakomban (Alapadatok és "
                                                "bejelentkezés › Vinted), utána az Újra gombbal folytatom.")
            return self._outcome("miss", "nem ismerem fel a hirdetési űrlapot (nem találom a cím mezőt)")
        try:
            self._step("photos", self._photos)
            self._step("title", lambda: self._text("title"))
            self._step("description", lambda: self._text("description"))
            self._step("category", self._category)
            self._step("brand", self._brand)
            self._step("size", self._size)
            self._step("condition", self._condition)
            self._step("color", self._color)
            self._step("price", lambda: self._text("price"))
            self._step("parcel_size", self._parcel)
            button = publish_button(self.browser.scan()) if not self.missing else None
        except Exception as exc:  # noqa: BLE001 - a Cancelled (Állj gomb) menjen tovább, a többit a modell rendezi
            if type(exc).__name__ == "Cancelled":
                raise
            return self._outcome("partial", "; ".join([*self.missing, str(exc).splitlines()[0][:160] if str(exc)
                                                       else type(exc).__name__]))
        if self.missing:
            return self._outcome("partial", "; ".join(self.missing))
        if button is None:
            return self._outcome("partial", "nem találom a közzététel gombját")
        return self._publish(button)

    def _step(self, name: str, action: Callable[[], None]) -> None:
        self.check()
        self.progress(LABELS[name].lower())
        try:
            action()
        except Missing as exc:
            self._miss(name, str(exc))
        except BrowserError as exc:
            self._miss(name, str(exc).splitlines()[0][:160])
        except OllamaError as exc:
            self._miss(name, f"a nyelvi modell nem válaszolt ({str(exc).splitlines()[0][:120]})")

    def _miss(self, name: str, reason: str) -> None:
        self.missing.append(f"{LABELS[name]}: {reason}")
        self.log.append(f"{LABELS[name]}: NEM SIKERÜLT – {reason}")
        try:
            self.browser.press("Escape")   # ha nyitva maradt egy lista, ne takarja a többi mezőt
        except BrowserError:
            pass

    def _click(self, el: dict[str, Any]) -> None:
        """Kattintás egy lehetőségre vagy mezőre (ha a felirata miatt jóváhagyást kért, és nem engedted: hiba)."""
        result = self.browser.click(el["id"])
        if result.startswith("A felhasználó nem engedélyezte"):
            raise Missing(f"nem engedélyezted ezt a kattintást: „{short(el.get('label', ''))}”")

    def _done(self, name: str, shown: str = "", choice: bool = False) -> None:
        text = f"{LABELS[name]}: {shown}" if shown else LABELS[name]
        self.filled.append(text.lower() if not shown else f"{LABELS[name].lower()}: {shown}")
        if choice:
            self.summary.append(text)
        self.log.append(text)

    # --- szövegmezők és fotók ---

    def _photos(self) -> None:
        if not self.photos:
            raise Missing("nincs fotó")
        self.browser.upload_files(self.photos)
        self._done("photos", f"{len(self.photos)} db")

    def _text(self, name: str) -> None:
        value = self.values.get(name, "")
        if not value:
            raise Missing("nincs megadva")
        el = find_field(self.browser.scan(), name)
        if el is None:
            raise Missing("nem találom a mezőt")
        self.browser.type_text(el["id"], value)
        self._done(name)

    # --- választások ---

    def _options(self, elements: list[dict[str, Any]], base: set[tuple[str, str, str]], name: str) -> list[dict[str, Any]]:
        """A lista lehetőségei: ami a kinyitás óta jelent meg. Ha egy felirat kétszer is szerepel (pl. a sor és a
        benne lévő gomb), csak egyszer, a pontosabbal (választógomb, aztán gomb)."""
        found = [e for e in elements if elem_key(e) not in base and is_option(e, name)]

        def rank(el: dict[str, Any]) -> int:
            return 0 if el.get("kind") in FINAL_KINDS else 1 if el.get("kind") in BUTTON_KINDS else 2

        best: dict[str, dict[str, Any]] = {}
        for el in found:
            label = norm_text(el.get("label", ""))
            if label not in best or rank(el) < rank(best[label]):
                best[label] = el
        return [el for el in found if best.get(norm_text(el.get("label", ""))) is el]

    def _open(self, name: str) -> tuple[set[tuple[str, str, str]], list[dict[str, Any]], dict[str, Any]] | None:
        """Kinyitja a mező listáját. Visszaadja, mi látszott előtte, az új lehetőségeket és a mezőt (vagy None-t,
        ha ilyen mező nincs az űrlapon, pl. a táskánál a méret)."""
        before = self.browser.scan()
        opener = find_field(before, name)
        if opener is None:
            return None
        base = {elem_key(e) for e in before}
        self._click(opener)
        after = self.browser.scan()
        options = self._options(after, base, name)
        if not options:
            # Lehet, hogy a lista már nyitva volt, és a kattintás bezárta: még egyszer.
            again = find_field(after, name)
            if again is not None:
                base = {elem_key(e) for e in after}
                self._click(again)
                options = self._options(self.browser.scan(), base, name)
        return base, options, opener

    def _close(self, shown: list[dict[str, Any]], base: set[tuple[str, str, str]]) -> None:
        """Ha a lista nyitva maradt (pl. több színt is lehet választani), bezárja: a lista „Kész” gombjával
        (csak az a gomb, ami a listával jelent meg), vagy Esc-cel."""
        keys = {elem_key(o) for o in shown}
        for _ in range(2):
            elements = self.browser.scan()
            if not any(elem_key(e) in keys for e in elements):
                return
            done = next((e for e in elements if e.get("kind") in BUTTON_KINDS and elem_key(e) not in base
                         and not e.get("chrome") and _DONE_RE.match(norm_text(e.get("label", "")))), None)
            if done is not None:
                self._click(done)
            else:
                self.browser.press("Escape")

    def _choose(self, name: str, decide: Callable[[list[dict[str, Any]], list[str]], dict[str, Any] | None],
                remembered: list[dict[str, str]] | None = None, required: bool = True) -> list[dict[str, Any]]:
        """Kinyitja a mező listáját, és szintenként kiválaszt egy lehetőséget, amíg a választás véget nem ér
        (választógomb, a mező felvette az értéket, vagy nem nyílt újabb szint). `decide(lehetőségek, eddigi út)`
        adja meg a lehetőséget; a `remembered` lépései (egy korábbi választás) előrébb valók."""
        opened = self._open(name)
        if opened is None:
            if required:
                raise Missing("nem találom a mezőt")
            return []
        base, options, opener = opened
        if not options:
            raise Missing("nem nyílt ki a lista")
        chosen: list[dict[str, Any]] = []
        for level in range(MAX_LEVELS):
            self.check()
            option = same_option(options, remembered[level]) if remembered and level < len(remembered) else None
            if option is not None:
                self.log.append(f"{LABELS[name]} ({level + 1}. szint): {option['label']} (megjegyzett)")
            else:
                option = decide(options, [c["label"] for c in chosen])
            if option is None:
                labels = ", ".join(short(o.get("label", ""), 30) for o in options[:8])
                path = " > ".join(c["label"] for c in chosen)
                raise Missing(f"nem tudtam választani{' (eddig: ' + path + ')' if path else ''}; a lehetőségek: {labels}")
            self._click(option)
            chosen.append(option)
            shown = options
            if option.get("kind") in FINAL_KINDS:
                break
            after = self.browser.scan()
            current = find_field(after, name)
            if current is not None and (current.get("value") or "") != (opener.get("value") or "") \
                    and not current.get("expanded"):
                break      # a mező felvette az értéket: kész
            taken = {norm_text(c["label"]) for c in chosen}
            options = [o for o in self._options(after, base, name) if norm_text(o["label"]) not in taken]
            if not options:
                break
        else:
            raise Missing("túl sok szint")
        self._close(shown, base)
        return chosen

    def _ask(self, what: str, options: list[dict[str, Any]], path: list[str], value: str = "",
             allow_none: bool = True) -> dict[str, Any] | None:
        """Egy rövid, számozott kérdés a modellnek: melyik lehetőség illik a tárgyhoz (a válasz kötött: szám)."""
        if self.llm is None or not options:
            return None
        shown = options[:MAX_ASK_OPTIONS]
        v = self.values
        about = [f"A tárgy: {v.get('title', '')}", f"Mi ez: {v.get('product', '')} ({v.get('kind') or 'egyéb'})"]
        if v.get("category"):
            about.append(f"Az eladó szerint a kategória: {v['category']}")
        extra = "; ".join(f"{LABELS[n]}: {v[n]}" for n in ("brand", "size", "color") if v.get(n))
        if extra:
            about.append(extra)
        if v.get("description"):
            about.append(f"Leírás: {v['description'][:300]}")
        lines = [f"{n}. {short(o.get('label', ''), 90)}" for n, o in enumerate(shown, 1)]
        if allow_none:
            lines.append("0. egyik sem")
        question = f"Mező: {what}" + (f" (eddig kiválasztva: {' > '.join(path)})" if path else "")
        if value:
            question += f". A keresett érték: {value}"
        messages = [{"role": "system", "content": ASK_SYSTEM},
                    {"role": "user", "content": "\n".join(about) + f"\n\n{question}\nLehetőségek:\n" + "\n".join(lines)}]
        numbers = list(range(0 if allow_none else 1, len(shown) + 1))
        schema = {"type": "object", "properties": {"choice": {"type": "integer", "enum": numbers}},
                  "required": ["choice"]}
        data = self.llm.chat_json(messages, schema, temperature=0.0, max_tokens=ASK_MAX_TOKENS)
        try:
            number = int(data.get("choice"))
        except (TypeError, ValueError):
            number = 0
        if not 1 <= number <= len(shown):
            self.log.append(f"{what}: a modell szerint egyik sem illik ({value or ' > '.join(path)})")
            return None
        self.log.append(f"{what}{' (' + str(len(path) + 1) + '. szint)' if not value else ''}: "
                        f"{shown[number - 1]['label']} (a modell választotta)")
        return shown[number - 1]

    def _category(self) -> None:
        wanted = category_words(self.values.get("category", ""))
        key = choice_key(self.values)
        remembered = self.memory.get("category", key) if self.memory is not None else None

        def decide(options: list[dict[str, Any]], path: list[str]) -> dict[str, Any] | None:
            option = category_option(options, wanted)
            if option is not None:
                self.log.append(f"Kategória ({len(path) + 1}. szint): {option['label']} (a tárgy kategóriájából)")
                return option
            return self._ask("Kategória", options, path, allow_none=False)

        chosen = self._choose("category", decide, remembered)
        self.learned.setdefault("category", {})[key] = [{"label": c["label"], "field": c.get("field", "")}
                                                         for c in chosen]
        self._done("category", " > ".join(c["label"] for c in chosen), choice=True)

    def _condition(self) -> None:
        value = self.values.get("condition", "")
        if not value:
            raise Missing("nincs megadva")
        shown = [""]

        def decide(options: list[dict[str, Any]], path: list[str]) -> dict[str, Any] | None:
            option, shown[0] = condition_option(options, value)
            if option is None:
                option = self._ask("Állapot", options, path, value)
                shown[0] = short(option["label"], 24) if option else ""
            return option

        self._choose("condition", decide)
        self._done("condition", shown[0], choice=True)

    def _size(self) -> None:
        value = self.values.get("size", "")
        if not value:
            return

        def decide(options: list[dict[str, Any]], path: list[str]) -> dict[str, Any] | None:
            hits = size_options(options, value)
            if len(hits) == 1:
                self.log.append(f"Méret: {hits[0]['label']} (a „{value}” méretből)")
                return hits[0]
            # Több is illik (pl. „40” a „L / 40 / 12” és az „EU 40” is), vagy egyik sem, mert előbb a méretrendszert
            # kell kiválasztani (pl. EU): a modell dönt.
            return self._ask("Méret", hits or options, path, value, allow_none=not hits)

        chosen = self._choose("size", decide, required=False)
        if chosen:
            self._done("size", chosen[-1]["label"], choice=True)

    def _brand(self) -> None:
        brand = self.values.get("brand", "").strip()
        elements = self.browser.scan()
        el = find_field(elements, "brand")
        if el is None or not brand:
            return    # ennél a kategóriánál nincs márka, vagy nem adtad meg (ha kötelező, a Vinted szól)
        base = {elem_key(e) for e in elements}
        search = el if is_writable(el) else None
        if search is None:     # a mező csak kinyit egy listát, abban van a kereső
            self._click(el)
            search = next((e for e in self.browser.scan() if elem_key(e) not in base and is_writable(e)), None)
        if search is not None:
            self.browser.type_text(search["id"], brand)
        options = self._options(self.browser.scan(), base, "brand")
        option = brand_option(options, brand)
        if option is not None:
            self.log.append(f"Márka: {option['label']} (a keresés találata)")
        elif options:
            option = self._ask("Márka", [o for o in options if not _CUSTOM_BRAND_RE.search(norm_text(o["label"]))]
                               or options, [], brand)
        if option is None:
            option = custom_brand_option(options, brand)
        if option is None:
            raise Missing(f"„{brand}”: nem találom a Vinted márkái között")
        self._click(option)
        self._close(options, base)
        self._done("brand", short(option["label"]), choice=True)

    def _color(self) -> None:
        colors = color_values(self.values.get("color", ""))
        if not colors:
            return
        opened = self._open("color")
        if opened is None:
            return    # ennél a kategóriánál nincs szín
        base, options, _opener = opened
        picked: list[str] = []
        shown = list(options)
        for color in colors:
            self.check()
            if not options:
                break
            option = color_option(options, color) or self._ask("Szín", options, [], color)
            if option is None:
                self.log.append(f"Szín: „{color}” nincs a lehetőségek között")
                continue
            if not option.get("checked"):
                self._click(option)
            picked.append(option["label"])
            options = self._options(self.browser.scan(), base, "color")
            shown += options
        self._close(shown, base)
        if not picked:
            raise Missing(f"„{self.values.get('color', '')}”: nincs ilyen szín a listában")
        self._done("color", ", ".join(short(p, 20) for p in picked), choice=True)

    def _parcel(self) -> None:
        option, shown = parcel_option(self.browser.scan(), self.values.get("parcel_size", "kicsi"))
        if option is None:
            return    # nincs csomagméret-választó (a Vinted maga állítja be)
        if not option.get("checked"):
            self._click(option)
        self._done("parcel_size", shown, choice=True)

    # --- közzététel ---

    def _publish(self, button: dict[str, Any]) -> FillOutcome:
        """A közzététel gombja (jóváhagyással). Ha a kattintás közben bármi hiba jön, bizonytalannak vesszük: lehet,
        hogy már fent van, és újrakezdeni nem szabad (kétszer kerülne fel)."""
        before_errors = set(self.browser.form_errors())
        form_url = self.browser.url
        self.check()
        self.progress("közzététel (jóváhagyásra vár)")
        try:
            result = self.browser.click(button["id"])
        except BrowserError as exc:
            return self._outcome("uncertain", f"a közzététel gombjánál hiba volt ({str(exc).splitlines()[0][:160]})",
                                 published=True)
        if result.startswith("A felhasználó nem engedélyezte"):
            return self._outcome("declined", "nem engedélyezted a közzétételt")
        self.log.append(f"Közzététel: „{button.get('label', '')}”")
        return self._await_result(form_url, before_errors)

    def _listing_url(self, landing: str) -> str:
        """A közzététel után a Vinted a profilodra (vagy a hirdetésre) visz. A hirdetés saját címe kell (azzal
        egyből le lehet venni): ha a profilon ott a linkje, azt adjuk vissza, különben a céloldalt."""
        if ITEM_PATH_RE.search(urlsplit(landing).path):
            return landing
        for attempt in range(3):            # a profil hirdetései kicsit később töltődhetnek be
            try:
                link = listing_link(self.browser.scan(), self.values.get("title", ""))
            except BrowserError:
                break
            if link is not None:
                return urljoin(self.browser.url or landing, link["href"])
            if attempt < 2:
                time.sleep(0.7)
        return landing

    def _await_result(self, form_url: str, before_errors: set[str]) -> FillOutcome:
        """A közzététel után: a Vinted továbbenged (a hirdetés vagy a profil oldalára), vagy hibát ír ki."""
        started = time.monotonic()
        shape = url_shape(form_url)
        last: list[str] = []
        while True:
            url = self.browser.url
            if url and url_shape(url) != shape:
                outcome = self._outcome("done", published=True, url=self._listing_url(url))
                outcome.profile = profile_url(url)
                return outcome
            if time.monotonic() - started >= 1.5:
                errors = [e for e in self.browser.form_errors() if e not in before_errors]
                if errors and errors == last:    # két egymás utáni nézésnél is ott van: nem csak egy villanás
                    return self._outcome("uncertain", "a Vinted ezt írta ki: " + "; ".join(errors)[:300],
                                         published=True, url=url)
                last = errors
            if time.monotonic() - started >= self.publish_wait:
                return self._outcome("uncertain", "nem látszik, hogy a Vinted elfogadta", published=True, url=url)
            time.sleep(0.5)


# --- levétel (eladás után) --------------------------------------------------------------------------------
# Eddig a modell vette le lépésenként: a hirdetés címe nélkül a főoldalról indult, a profilodon kereste, és a
# jobb oldali gombokat képről próbálta megtalálni; percekig tartott, és nem mindig sikerült. Itt a kód tudja,
# hol mi van: a hirdetés oldalán a Törlés (Delete) gomb, utána a megerősítés, végül megnézi, hogy eltűnt-e.

DELETE_WAIT = 15.0          # a megerősítés után ennyi ideig várja, hogy a hirdetés eltűnjön
MAX_CANDIDATES = 3          # a keresőben legfeljebb ennyi azonos című hirdetést nyit meg (melyik a tiéd)
PROFILE_SCROLLS = 3         # a profilodon ennyit görget lefelé, ha a hirdetés nem látszik (a rács később tölt)
ITEM_PATH_RE = re.compile(r"/items/(\d+)")
MEMBER_PATH_RE = re.compile(r"/member/(\d+)")
_DELETE_RE = re.compile(r"^(delete|delete item|delete listing|torles|torlom|hirdetes torlese|a hirdetes torlese|"
                        r"termek torlese|tetel torlese)$")
# A megerősítő ablak gombja (a „Mégse” nem): előbb a törlés szavával, aztán az általános megerősítés.
_CONFIRM_DELETE_RE = re.compile(r"^(confirm and delete|yes delete|yes delete it|delete|delete item|torles|torlom|"
                                r"igen torlom|igen torles|megerosites es torles|torles megerositese)$")
_CONFIRM_RE = re.compile(r"^(confirm|megerosites|megerositem|igen|yes|ok|rendben)$")
_GONE_RE = re.compile(r"(page not found|page doesn t exist|not found|nem talalhato|nem letezik|no longer available|"
                      r"mar nem elerheto|has been deleted|was deleted|torolve|toroltek|toroltuk)")
_LOGGED_OUT_RE = re.compile(r"^(log in|login|sign up|sign up log in|sign in|bejelentkezes|belepes|regisztracio|"
                            r"regisztralj|csatlakozz)$")


def item_slug(title: str) -> str:
    """A hirdetés címe úgy, ahogy a Vinted a webcímbe teszi: „Hosszú farmer 40-es méret” → hosszu-farmer-40-es-meret."""
    return re.sub(r"[^a-z0-9]+", "-", fold(title or "")).strip("-")


def profile_url(url: str) -> str:
    """A Vinted-profil címe (a /member/<szám> rész, a ?… nélkül), ha ez egy profiloldal; különben ""."""
    parts = urlsplit(url or "")
    found = MEMBER_PATH_RE.search(parts.path)
    return f"{parts.scheme}://{parts.netloc}{parts.path}" if found and parts.scheme in ("http", "https") else ""


def listing_link(elements: list[dict[str, Any]], title: str) -> dict[str, Any] | None:
    """Egy hirdetéslista (a profilod vagy a keresés) eleme, ami erre a hirdetésre visz: /items/<szám>-… link, és a
    felirata a hirdetés címével kezdődik („Hosszú farmer 40-es méret, brand: …”), vagy a webcímében ez a cím áll."""
    return next(iter(listing_links(elements, title)), None)


def listing_links(elements: list[dict[str, Any]], title: str) -> list[dict[str, Any]]:
    slug = item_slug(title)
    if not slug:
        return []
    found, seen = [], set()
    for el in elements:
        href = str(el.get("href") or "")
        path = urlsplit(href).path
        match = ITEM_PATH_RE.search(path)
        if not match or match.group(1) in seen:
            continue
        tail = path[match.end():].strip("/-")
        if starts(el.get("label", ""), title) or tail == slug or tail.startswith(slug + "-"):
            seen.add(match.group(1))
            found.append(el)
    return found


def delete_button(elements: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A hirdetés oldalán a Törlés (Delete) gomb: csak a saját hirdetéseden van ilyen."""
    for el in elements:
        if el.get("chrome") or el.get("disabled") or el.get("kind", "") not in BUTTON_KINDS | {"link"}:
            continue
        if _DELETE_RE.match(norm_text(el.get("label", ""))):
            return el
    return None


def seller_profile(elements: list[dict[str, Any]], base: str) -> str:
    """A hirdetés oldalán az eladó profiljának címe (a fejlécen kívüli /member/<szám> link), ha egyértelmű. A saját
    hirdetéseden ez a te profilod: legközelebb ott keresi a hirdetéseidet."""
    members = {}
    for el in elements:
        href = str(el.get("href") or "")
        found = MEMBER_PATH_RE.search(urlsplit(href).path)
        if found and not el.get("chrome"):
            members.setdefault(found.group(1), href)
    return profile_url(urljoin(base, next(iter(members.values())))) if len(members) == 1 else ""


def logged_out(elements: list[dict[str, Any]]) -> bool:
    """Kijelentkezett oldal: a fejlécben „Log in” / „Bejelentkezés” gomb van."""
    return any(_LOGGED_OUT_RE.match(norm_text(el.get("label", ""))) for el in elements)


def new_elements(before: Counter, elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ami egy kattintás óta jelent meg (pl. a megerősítő ablak gombjai). Ha egy felirat már előtte is ott volt
    (a „Delete” gomb az oldalon és az ablakban is), a későbbi előfordulás az új: az ablak a lap végére kerül."""
    seen: Counter = Counter()
    fresh = []
    for el in elements:
        key = elem_key(el)
        seen[key] += 1
        if seen[key] > before.get(key, 0):
            fresh.append(el)
    return fresh


def confirm_button(fresh: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A megerősítő ablak gombja a most megjelent elemek közül: a törlés szavával, vagy az általános igen."""
    buttons = [e for e in fresh if e.get("kind", "") in BUTTON_KINDS and not e.get("disabled")]
    for pattern in (_CONFIRM_DELETE_RE, _CONFIRM_RE):
        for el in buttons:
            if pattern.match(norm_text(el.get("label", ""))):
                return el
    return None


class _Stop(Exception):
    """A levétel itt megáll (pl. bejelentkezés vagy robot-ellenőrzés kell); a kimenetel benne van."""

    def __init__(self, outcome: FillOutcome):
        super().__init__(outcome.reason)
        self.outcome = outcome


class VintedDelister:
    """Egy hirdetés levétele a Vintedről modell nélkül, emberi tempóban (a böngésző közben „auto” módban van).

    A hirdetés oldala: a megjegyzett webcím; ha az a profilodé (a feltöltés után a Vinted oda visz), vagy nincs,
    a profilodon, végül a Vinted keresőjében keresi meg (a keresőben a tiéd az, amelyiken ott a Törlés gomb).
    Utána a Törlés, a megerősítés, és megnézi, hogy a hirdetés eltűnt-e. A törlés jóvá van hagyva, mert a
    levételt már jóváhagytad (work.begin_delist); fizetős gombra soha nem kattint."""

    def __init__(self, browser: Any, title: str, url: str = "", profile: str = "", search_url: str = "",
                 check: Callable[[], None] | None = None, progress: Callable[[str], None] | None = None,
                 wait: float | None = None):
        self.browser = browser
        self.title = title
        self.url = url or ""
        self.profile = profile_url(url) or profile or ""
        self.search_url = search_url
        self.check = check or (lambda: None)
        self.progress = progress or (lambda _text: None)
        self.wait = DELETE_WAIT if wait is None else wait
        self.found = ""             # a hirdetés webcíme, ha megtalálta (legközelebb egyből oda megy)
        self.log: list[str] = []

    def run(self) -> FillOutcome:
        previous = getattr(self.browser, "auto", False)
        self.browser.auto = True
        try:
            return self._run()
        except _Stop as stop:
            return stop.outcome
        finally:
            self.browser.auto = previous

    def _outcome(self, status: str, reason: str = "", published: bool = False) -> FillOutcome:
        return FillOutcome(status, reason, url=self.found, published=published, log=list(self.log),
                           profile=self.profile)

    def _run(self) -> FillOutcome:
        try:
            self.progress("a hirdetés megkeresése")
            button = self._find()
            if button is None:
                return self._outcome("miss", "nem találom a hirdetést (se a webcímén, se a profilodon, se a keresőben)")
            return self._delete(button)
        except BrowserError as exc:
            return self._outcome("miss", f"a böngészőben hiba volt ({str(exc).splitlines()[0][:160]})")

    # --- a hirdetés megkeresése ---

    def _open(self, url: str) -> tuple[str, list[dict[str, Any]]]:
        """Megnyit egy oldalt (a süti-ablakot elfogadja); az oldal szövege és a letapogatott elemek."""
        self.check()
        text = self.browser.open(url)
        if self.browser.challenged():
            raise _Stop(self._outcome("blocked", "A Vinted robot-ellenőrzést (captcha) kér. Oldd meg a "
                                                 "böngészőablakomban, utána az Újra gombbal folytatom."))
        elements = self.browser.scan()
        consent = consent_button(elements)
        if consent is not None:
            self.browser.click(consent["id"])
            elements = self.browser.scan()
        return text, elements

    def _item_page(self, url: str, known: bool = False) -> dict[str, Any] | None:
        """Megnyitja a hirdetés oldalát; a Törlés gombja, ha ez a te hirdetésed. A megjegyzett webcímnél (known)
        az is kiderül, ha már nincs fent, vagy ha be kell jelentkezni."""
        text, elements = self._open(url)
        for attempt in range(3):            # a gombok a lap betöltése után kicsit később jelenhetnek meg
            button = delete_button(elements)
            if button is not None:
                self.found = self.browser.url.split("?")[0]
                self.log.append(f"A hirdetés oldala: {self.found}")
                self.profile = self.profile or seller_profile(elements, self.browser.url)
                return button
            if attempt < 2:
                time.sleep(0.6)
                elements = self.browser.scan()
        if logged_out(elements):
            raise _Stop(self._outcome("blocked", "Be kell jelentkezned a Vintedre a böngészőablakomban (Alapadatok és "
                                                 "bejelentkezés › Vinted), utána az Újra gombbal folytatom."))
        if known and self._gone(url, text):
            self.log.append(f"A hirdetés már nincs fent: {url}")
            raise _Stop(self._outcome("gone", "a hirdetés már nem volt fent a Vinteden"))
        return None

    def _gone(self, url: str, text: str) -> bool:
        """A hirdetés oldala már nem létezik: a Vinted máshova irányított, vagy „nem található” oldalt mutat."""
        wanted = ITEM_PATH_RE.search(urlsplit(url).path)
        now = ITEM_PATH_RE.search(urlsplit(self.browser.url).path)
        if wanted and (now is None or now.group(1) != wanted.group(1)):
            return True
        heading, visible = self._page_text(text)
        return bool(_GONE_RE.search(norm_text(heading + " " + visible[:600]))) and not starts(heading, self.title)

    @staticmethod
    def _page_text(text: str) -> tuple[str, str]:
        """Az oldalnézetből (browser.open) az oldal címe és a látható szöveg (a kezelhető elemek nélkül)."""
        heading, visible, inside = "", [], False
        for line in (text or "").splitlines():
            if line.startswith("Oldal: ") and not heading:
                heading = line[len("Oldal: "):].strip()
            elif line.startswith("Látható szöveg:"):
                inside = True
            elif line.startswith("Kezelhető elemek") or line.startswith("Nincs kezelhető elem"):
                break
            elif inside:
                visible.append(line)
        return heading, " ".join(visible)

    def _find(self) -> dict[str, Any] | None:
        if self.url and ITEM_PATH_RE.search(urlsplit(self.url).path):
            self.progress("a hirdetés megnyitása")
            button = self._item_page(self.url, known=True)
            if button is not None:
                return button
            # Fent van, és be vagy jelentkezve, csak a Törlés gombot nem ismerem fel (pl. új elrendezés): máshol
            # nem keresem, mert egy azonos című másik hirdetésedet törölhetném; a lépéseket a modell végzi.
            self.found = self.url.split("?")[0]
            raise _Stop(self._outcome("miss", "a hirdetés oldalán nem találom a Törlés gombot"))
        if self.profile:
            self.progress("a hirdetés keresése a profilodon")
            button = self._from_profile(self.profile)
            if button is not None:
                return button
        if self.search_url and self.title:
            self.progress("a hirdetés keresése a Vinteden")
            return self._from_search()
        return None

    def _from_profile(self, url: str) -> dict[str, Any] | None:
        _text, elements = self._open(url)
        for scroll in range(PROFILE_SCROLLS + 1):
            link = listing_link(elements, self.title)
            if link is not None:
                self.log.append(f"A profilodon: „{short(link.get('label', ''), 60)}”")
                return self._item_page(urljoin(self.browser.url, link["href"]))
            if scroll < PROFILE_SCROLLS:
                self.browser.scroll("down")
                elements = self.browser.scan()
        self.log.append("A profilodon nem látszik a hirdetés.")
        return None

    def _from_search(self) -> dict[str, Any] | None:
        query = self.search_url.replace("{q}", quote_plus(self.title))
        _text, elements = self._open(query)
        base = self.browser.url
        links = [urljoin(base, el["href"]) for el in listing_links(elements, self.title)][:MAX_CANDIDATES]
        self.log.append(f"A keresőben {len(links)} azonos című hirdetés.")
        for link in links:
            button = self._item_page(link)
            if button is not None:          # a tiéd az, amelyiken ott a Törlés gomb
                return button
        return None

    # --- a törlés ---

    def _delete(self, button: dict[str, Any]) -> FillOutcome:
        item_url = self.found or self.browser.url
        before = Counter(elem_key(e) for e in self.browser.scan())
        self.check()
        self.progress("törlés")
        result = self.browser.click(button["id"])
        if result.startswith("A felhasználó nem engedélyezte"):
            return self._outcome("declined", "nem engedélyezted a törlést")
        self.log.append(f"Törlés: „{button.get('label', '')}”")
        confirm = None
        deadline = time.monotonic() + 4.0
        while confirm is None and time.monotonic() < deadline:
            if not ITEM_PATH_RE.search(urlsplit(self.browser.url).path):
                break                       # megerősítés nélkül törölte, és továbbvitt
            confirm = confirm_button(new_elements(before, self.browser.scan()))
            if confirm is None:
                time.sleep(0.4)
        if confirm is not None:
            self.progress("a törlés megerősítése")
            result = self.browser.click(confirm["id"])
            if result.startswith("A felhasználó nem engedélyezte"):
                return self._outcome("declined", "nem engedélyezted a törlést")
            self.log.append(f"Megerősítés: „{confirm.get('label', '')}”")
        return self._await_gone(item_url)

    def _await_gone(self, item_url: str) -> FillOutcome:
        """A törlés után: a Vinted továbbvisz (pl. a profilodra), vagy eltűnik a hirdetés. Ha nem egyértelmű,
        még egyszer megnyitja a hirdetést: ha már nincs ott, kész; ha még ott van a Törlés gomb, bizonytalan."""
        wanted = ITEM_PATH_RE.search(urlsplit(item_url).path)
        started = time.monotonic()
        while time.monotonic() - started < self.wait:
            now = ITEM_PATH_RE.search(urlsplit(self.browser.url).path)
            if wanted and (now is None or now.group(1) != wanted.group(1)):
                self.log.append(f"Törölve, továbbvitt: {self.browser.url}")
                return self._outcome("done", published=True)
            if delete_button(self.browser.scan()) is None and time.monotonic() - started >= 1.5:
                break
            time.sleep(0.5)
        text = self.browser.open(item_url)
        if self._gone(item_url, text) or (delete_button(self.browser.scan()) is None
                                          and not starts(self._page_text(text)[0], self.title)):
            self.log.append("Újra megnyitva: a hirdetés már nincs fent.")
            return self._outcome("done", published=True)
        return self._outcome("uncertain", "a törlés után is látszik a hirdetés", published=True)
