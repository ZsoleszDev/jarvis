"""Begyakorolt lépések: amit Jarvis egyszer a modellel végigcsinált (egy hirdetés feltöltése vagy
levétele egy oldalon), azt legközelebb modell nélkül, gyorsan visszajátssza.

Felvétel: a böngésző minden sikeres lépését (megnyitás, kattintás, gépelés, fotó) feljegyezzük.
Ha a feltöltés sikerült, a lépéssorból „recept” lesz:
- a beírt szövegek helyére a tárgy mezői kerülnek (cím, leírás, ár…),
- a választásoknál (márka, méret, állapot, szín…) az, hogy melyik értékhez mire kellett kattintani;
  ha a felirat maga az érték (pl. „L” a méretnél), akkor bármelyik értékre működik.
A recept oldalanként külön van; feltöltésnél tárgyfajtánként és kategóriánként is, mert a hirdetési
űrlap a kategóriától függően más, és a kategória kiválasztása is ettől függ.

Visszajátszás: ugyanazokat az elemeket keresi meg (felirat, mezőnév és sorrend alapján), és a mostani
tárgy adataival tölti ki. A közzététel előtt ugyanúgy jóváhagyást kér. Ha valami nem stimmel (pl. az
oldal megváltozott, vagy egy választásnál olyan érték jön, amit még nem tanult meg), a közzététel
ELŐTT megáll, és a modell csinálja végig, közben újratanulja. A közzététel után soha nem kezdi újra
(nehogy kétszer kerüljön fel), legfeljebb megnézeti a modellel, sikerült-e.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .textutil import fold

VERSION = 1
DEFAULT_TIMEOUT = 6.0      # ennyi ideig várja, hogy egy elem megjelenjen (a tesztek rövidebbre veszik)
FIELD_LABELS = {"title": "Cím", "description": "Leírás", "price": "Ár", "brand": "Márka", "size": "Méret",
                "condition": "Állapot", "color": "Szín", "material": "Anyag", "category": "Kategória",
                "location": "Átvétel helye", "shipping": "Szállítás", "parcel_size": "Csomagméret",
                "product": "Mi ez"}
# Szövegmezők: a beírt szöveg ezek egyike (a tárgy adataiból).
VALUE_FIELDS = ("title", "description", "price", "brand", "size", "color", "material", "location",
                "shipping", "product", "category", "condition")
# Választások: ezeknél az érték a tárgytól függ, és kattintással kell kiválasztani.
CHOICE_FIELDS = ("category", "brand", "size", "condition", "color", "material", "parcel_size", "shipping",
                 "location")
# Melyik mezőé egy felirat (ékezet nélkül, szó elején); a sorrend számít (a "csomagméret" nem "méret").
FIELD_WORDS = (
    ("parcel_size", ("csomagmeret", "csomag merete", "package size", "parcel size")),
    ("category", ("kategoria", "alkategoria", "fokategoria", "category", "rovat")),
    ("brand", ("marka", "brand", "gyarto")),
    ("size", ("meret", "size")),
    ("condition", ("allapot", "condition", "minoseg")),
    ("color", ("szin", "color", "colour")),
    ("material", ("anyag", "material")),
    ("shipping", ("szallitas", "kezbesites", "shipping", "delivery")),
    ("location", ("telepules", "varos", "helyseg", "iranyitoszam", "atvetel helye", "location")),
)
OPENER_KINDS = {"combobox", "button", "input:button", "link", "textbox", "input:text", "searchbox",
                "input:search", "summary", "tab", "listbox"}
CHOICE_KINDS = {"radio", "input:radio", "option", "menuitem", "menuitemradio", "checkbox", "input:checkbox",
                "switch"}
CONSENT_RE = re.compile(r"\b(elfogad|accept|cookie|suti|egyetert|hozzajarul|rendben|ertem|bezar|close|agree|"
                        r"allow|osszes|mindet|nem most|kesobb)")
CONDITION_WORDS = {"uj", "uj cimkevel", "uj cimke nelkul", "cimkevel", "bontatlan", "hasznalt", "hibas",
                   "felujitott", "kivalo", "nagyon jo", "jo", "elfogadhato", "kielegito", "serult", "megkimelt",
                   "alkatresznek", "hasznalt kivalo", "hasznalt jo", "hasznalt serult"}
COLOR_WORDS = {"fekete", "feher", "szurke", "piros", "kek", "zold", "sarga", "barna", "bezs", "lila",
               "rozsaszin", "narancs", "narancssarga", "arany", "ezust", "sotetkek", "vilagoskek", "khaki",
               "bordo", "turkiz", "tarka", "tobbszinu", "krem", "menta", "korall"}
SIZE_RE = re.compile(r"^(xxs|xs|s|m|l|xl|xxl|xxxl|[2-6]xl|\d{2}([.,]5)?|\d{2} ?[-/] ?\d{2}|egy meret|one size)$")


def norm_text(text: str) -> str:
    """Összehasonlításhoz: ékezet és írásjelek nélkül, egy szóközzel."""
    return " ".join(re.sub(r"[^\w\s]", " ", fold(text or "")).split())


def vkey(value: str) -> str:
    """Egy választás értékének kulcsa a memóriában (pl. „Nagyon jó” → „nagyon jo”)."""
    return norm_text(value)[:80]


def slug(text: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", fold(text or "")))[:60] or "x"


def url_shape(url: str) -> str:
    """A webcím „alakja”: gép + út, a számos részek helyén #. A sikeres feltöltés utáni oldal
    (pl. /items/123-zara-polo) így minden tárgynál ugyanaz: /items/#."""
    try:
        parts = urllib.parse.urlsplit(url or "")
    except ValueError:
        return ""
    segments = ["#" if re.search(r"\d", seg) else seg for seg in parts.path.split("/") if seg]
    return (parts.hostname or "") + "/" + "/".join(segments)


def field_for(text: str) -> str:
    """Melyik mező választója ez a felirat vagy mezőnév (üres, ha egyiké sem)."""
    folded = fold(text or "")
    for name, words in FIELD_WORDS:
        if any(re.search(r"(?<![a-z])" + re.escape(word), folded) for word in words):
            return name
    return ""


def match_field(text: str, values: dict[str, str], candidates: tuple[str, ...] = VALUE_FIELDS) -> str:
    """Melyik mező értéke ez a beírt szöveg (pontosan, vagy kicsit átfogalmazva). Üres, ha egyiké sem."""
    wanted = norm_text(text)
    if not wanted:
        return ""
    digits = re.sub(r"\D", "", text or "")
    if "price" in candidates and values.get("price") and digits == values["price"] and len(wanted) <= 14:
        return "price"
    for name in candidates:
        if name != "price" and values.get(name) and norm_text(values[name]) == wanted:
            return name
    best, best_score = "", 0.0
    for name in candidates:
        value = norm_text(values.get(name, ""))
        if name == "price" or len(value) < 4:
            continue
        if value in wanted or wanted in value:
            score = min(len(value), len(wanted)) / max(len(value), len(wanted))
        else:
            a, b = set(wanted.split()), set(value.split())
            score = len(a & b) / max(1, len(a | b))
        if score > best_score:
            best, best_score = name, score
    return best if best_score >= 0.5 else ""


def value_field_of_label(label: str, values: dict[str, str]) -> str:
    """Ha egy kattintott felirat pontosan egy választós mező értéke (pl. „L” = a méret), melyiké."""
    wanted = norm_text(label)
    if not wanted:
        return ""
    for name in ("size", "condition", "color", "brand", "material", "parcel_size"):
        if values.get(name) and norm_text(values[name]) == wanted:
            return name
    return ""


def guess_choice_field(label: str, el: dict[str, Any], values: dict[str, str]) -> str:
    """Egy rádiógomb vagy lehetőség valószínűleg melyik mezőé (mezőnév vagy szókincs alapján)."""
    by_name = field_for(el.get("field", ""))
    if by_name in CHOICE_FIELDS:
        return by_name
    wanted = norm_text(label)
    if wanted in CONDITION_WORDS and values.get("condition"):
        return "condition"
    if SIZE_RE.match(wanted) and values.get("size"):
        return "size"
    if wanted in COLOR_WORDS and values.get("color"):
        return "color"
    return ""


def item_values(item: Any, settings: dict[str, Any]) -> dict[str, str]:
    """A tárgy adatai szövegként, ahogy a hirdetésbe kerülnek (az Alapadatokkal kiegészítve)."""
    values = {name: str(getattr(item, name, "") or "") for name in
              ("title", "description", "brand", "size", "condition", "color", "material", "category", "product")}
    values["price"] = str(int(item.price)) if getattr(item, "price", None) else ""
    values["location"] = str(getattr(item, "location", "") or settings.get("city", "") or "")
    values["shipping"] = str(getattr(item, "shipping", "") or settings.get("shipping", "") or "")
    values["parcel_size"] = str(settings.get("parcel_size") or "kicsi")
    values["kind"] = str(getattr(item, "kind", "") or "")
    return values


def flow_key(platform: str, task: str, kind: str = "", category: str = "") -> str:
    if task == "post":
        return f"{platform}-post-{slug(kind or 'egyeb')}-{slug(category or 'nincs')}"
    return f"{platform}-{task}"


# --- felvétel --------------------------------------------------------------------------------------

class FlowRecorder:
    """Egy feltöltés vagy levétel lépései, ahogy a modell végigcsinálta."""

    def __init__(self, platform: str, task: str, key: str, start_url: str, domains: tuple[str, ...]):
        self.platform = platform
        self.task = task
        self.key = key
        self.start_url = start_url
        self.domains = tuple(domains)
        self.events: list[dict[str, Any]] = []
        self.dirty = ""
        self.started = time.time()

    def _on_site(self, url: str) -> bool:
        host = urllib.parse.urlsplit(url or "").hostname or ""
        return not self.domains or any(host == d or host.endswith("." + d) for d in self.domains)

    def add(self, event: dict[str, Any]) -> None:
        if len(self.events) >= 120:
            self.dirty = self.dirty or "túl sok lépés"
            return
        self.events.append(dict(event))

    @property
    def published(self) -> bool:
        return any(e.get("do") == "click" and e.get("risky") for e in self.events)

    def build(self, values: dict[str, str]) -> tuple[dict[str, Any] | None, str]:
        """A felvételből recept (vagy None és az ok, ha nem elég tiszta ahhoz, hogy megjegyezzük)."""
        if self.dirty:
            return None, self.dirty
        steps: list[dict[str, Any]] = []
        memory: dict[str, dict[str, list[dict[str, Any]]]] = {}
        group: dict[str, Any] | None = None
        risky_seen = False
        form_url = ""
        item_words = {w for w in norm_text(values.get("title", "")).split() if len(w) >= 4}

        def close() -> None:
            nonlocal group
            if group is None:
                return
            actions = group["actions"]
            name = group["field"]
            if actions or group.get("opener"):
                value_bound = [a for a in actions if a.get("from_value")]
                generic = bool(value_bound) and len(value_bound) == len(actions)
                key = "*" if generic else vkey(values.get(name, ""))
                memory.setdefault(name, {})[key] = actions
                steps.append({"do": "choice", "field": name, "opener": group.get("opener")})
            group = None

        for ev in self.events:
            do = ev.get("do")
            if do == "scroll":
                continue
            if do == "back":
                return None, "közben visszalépett, ez nem tiszta lépéssor"
            if do == "declined":
                return None, "egy lépést nem engedélyeztél"
            if do == "goto":
                close()
                url = ev.get("url", "")
                if not steps and url_shape(url) == url_shape(self.start_url):
                    continue   # a kiindulás (a hirdetésfeladás vagy a hirdetés oldala)
                if not self._on_site(ev.get("final") or url):
                    return None, "más oldalra is átment"
                if self.task == "delist":
                    return None, "a levételhez más oldalra is el kellett mennie"
                steps.append({"do": "goto", "url": url})
                continue
            el = dict(ev.get("el") or {})
            label = el.get("label", "")
            if do == "upload":
                close()
                steps.append({"do": "upload", "el": el or None})
                continue
            if do == "type":
                text = ev.get("text", "")
                by_label = field_for(label) or field_for(el.get("field", ""))
                submit = bool(ev.get("submit"))
                if el.get("select"):
                    close()
                    by_value = match_field(text, values, CHOICE_FIELDS)
                    name = by_label if by_label in CHOICE_FIELDS else by_value
                    if name:
                        act = ({"do": "type", "el": el, "from_value": True} if by_value == name
                               else {"do": "type", "el": el, "text": text})
                        group = {"field": name, "opener": None, "actions": [act]}
                        close()
                    else:
                        steps.append({"do": "type", "el": el, "text": text})   # állandó, pl. „Fix áras”
                    continue
                if group is not None and (ev.get("search") or not by_label or by_label == group["field"]):
                    current = values.get(group["field"], "")
                    if current and norm_text(text) == norm_text(current):
                        group["actions"].append({"do": "type", "el": el, "from_value": True, "submit": submit})
                    else:
                        group["actions"].append({"do": "type", "el": el, "text": text, "submit": submit})
                    continue
                close()
                mapped = match_field(text, values)
                if mapped:
                    steps.append({"do": "type", "el": el, "value": mapped, "submit": submit})
                    continue
                if by_label in CHOICE_FIELDS:
                    current = values.get(by_label, "")
                    act = ({"do": "type", "el": el, "from_value": True, "submit": submit}
                           if current and norm_text(text) == norm_text(current)
                           else {"do": "type", "el": el, "text": text, "submit": submit})
                    group = {"field": by_label, "opener": None, "actions": [act]}
                    continue
                if ev.get("search"):
                    steps.append({"do": "type", "el": el, "text": text, "submit": submit})
                    continue
                return None, f"olyan szöveget írt be, amit nem tudok a tárgy adataihoz kötni („{text[:40]}”)"
            if do == "click":
                if ev.get("chooser"):
                    continue   # a fotógomb: a fájlokat a következő (feltöltés) lépés adja át
                folded_label = norm_text(label)
                if len(item_words) >= 2 and len(item_words & set(folded_label.split())) >= 2:
                    return None, "a tárgy saját hirdetésére kattintott, ez minden tárgynál más"
                if ev.get("risky"):
                    close()
                    steps.append({"do": "click", "el": el, "risky": True})
                    if not risky_seen:
                        form_url = ev.get("url", "")   # az oldal, ahonnan a közzététel (törlés) indult
                    risky_seen = True
                    continue
                opener = field_for(label) or field_for(el.get("field", ""))
                as_value = value_field_of_label(label, values)
                guessed = guess_choice_field(label, el, values) if el.get("kind") in CHOICE_KINDS else ""
                other = as_value or guessed or (opener if opener in CHOICE_FIELDS else "")
                if group is not None and other and other != group["field"]:
                    close()     # ez már egy másik mező választása (pl. a kategória után az állapot)
                if opener in CHOICE_FIELDS and el.get("kind") in OPENER_KINDS and not as_value:
                    close()
                    group = {"field": opener, "opener": el, "actions": []}
                    continue
                if group is not None:
                    current = values.get(group["field"], "")
                    if current and norm_text(label) == norm_text(current):
                        group["actions"].append({"do": "click", "el": el, "from_value": True})
                    else:
                        group["actions"].append({"do": "click", "el": el})
                    continue
                if as_value:
                    memory.setdefault(as_value, {})["*"] = [{"do": "click", "el": el, "from_value": True}]
                    steps.append({"do": "choice", "field": as_value, "opener": None})
                    continue
                if el.get("kind") in CHOICE_KINDS:
                    if guessed:
                        memory.setdefault(guessed, {})[vkey(values.get(guessed, ""))] = [{"do": "click", "el": el}]
                        steps.append({"do": "choice", "field": guessed, "opener": None})
                        continue
                steps.append({"do": "click", "el": el, "optional": bool(CONSENT_RE.search(fold(label)))})
                continue
        close()
        if not risky_seen:
            return None, ("nem látszott a közzététel" if self.task == "post" else "nem látszott a törlés")
        # Siker: ahová a végén jutott (pl. a kész hirdetés oldala), ha az más, mint ahonnan indult.
        final_url = next((e.get("url_after") or e.get("final") or "" for e in reversed(self.events)
                          if e.get("url_after") or e.get("final")), "")
        success = ({"url": url_shape(final_url)}
                   if final_url and url_shape(final_url) != url_shape(form_url) else {})
        recording = {"version": VERSION, "key": self.key, "platform": self.platform, "task": self.task,
                     "recorded_at": time.time(), "uses": 0, "fails": 0, "steps": steps, "memory": memory,
                     "success": success, "choices": {name: values.get(name, "") for name in memory}}
        return recording, ""


# --- tárolás ------------------------------------------------------------------------------------------

class FlowStore:
    """A receptek: oldalanként (és feltöltésnél fajtánként, kategóriánként) egy JSON-fájl."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / (re.sub(r"[^a-z0-9-]", "", key)[:120] + ".json")

    def get(self, key: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self._path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("version") == VERSION and data.get("steps") else None

    def save(self, recording: dict[str, Any]) -> None:
        """Elmenti; ha már volt ilyen, a választások memóriáját összefésüli (az új az erősebb)."""
        old = self.get(recording["key"]) or {}
        memory = {name: dict(entries) for name, entries in (old.get("memory") or {}).items()}
        for name, entries in (recording.get("memory") or {}).items():
            memory.setdefault(name, {}).update(entries)
        merged = {**recording, "memory": memory, "uses": int(old.get("uses") or 0), "fails": 0}
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(recording["key"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def update_stats(self, key: str, ok: bool) -> None:
        """Sikeres visszajátszás után számol; két egymás utáni kudarc után elfelejti a receptet."""
        data = self.get(key)
        if data is None:
            return
        if ok:
            data["uses"] = int(data.get("uses") or 0) + 1
            data["fails"] = 0
        else:
            data["fails"] = int(data.get("fails") or 0) + 1
        if data["fails"] >= 2:
            self.forget(key)
            return
        path = self._path(key)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def forget(self, key: str) -> None:
        try:
            self._path(key).unlink()
        except OSError:
            pass

    def keys(self) -> list[str]:
        try:
            return sorted(p.stem for p in self.root.glob("*.json"))
        except OSError:
            return []


# --- visszajátszás --------------------------------------------------------------------------------------

class ReplayMiss(Exception):
    """A visszajátszás elakadt (nincs meg egy elem): a modellnek kell folytatnia."""


@dataclass
class ReplayOutcome:
    status: str                   # done | uncertain | declined | miss | no_plan
    reason: str = ""
    url: str = ""
    published: bool = False       # megtörtént-e a közzététel / törlés kattintása
    summary: list[str] = field(default_factory=list)


class Replayer:
    def __init__(self, browser: Any, recording: dict[str, Any], values: dict[str, str], photos: list[str],
                 check: Callable[[], None] | None = None, progress: Callable[[str], None] | None = None,
                 timeout: float | None = None):
        self.browser = browser
        self.recording = recording
        self.values = values
        self.photos = photos
        self.check = check or (lambda: None)
        self.progress = progress or (lambda _text: None)
        self.timeout = DEFAULT_TIMEOUT if timeout is None else timeout
        self.published = False
        self.summary: list[str] = []

    def _memory_for(self, name: str) -> tuple[list[dict[str, Any]] | None, str]:
        entries = (self.recording.get("memory") or {}).get(name) or {}
        value = self.values.get(name, "")
        if vkey(value) in entries:
            return entries[vkey(value)], value
        if value and "*" in entries:
            return entries["*"], value
        return None, value

    def plan_problem(self) -> str:
        """Üres, ha a recept ezzel a tárggyal visszajátszható; különben az ok."""
        for step in self.recording.get("steps") or []:
            if step.get("do") == "choice":
                actions, value = self._memory_for(step["field"])
                if actions is None:
                    label = FIELD_LABELS.get(step["field"], step["field"])
                    return f"{label}: „{value or 'üres'}” – ezt az értéket még nem tanultam meg ezen az oldalon"
            if step.get("do") == "type" and step.get("value") and not self.values.get(step["value"]) \
                    and step["value"] in ("title", "price"):
                return f"hiányzik: {FIELD_LABELS.get(step['value'], step['value'])}"
            if step.get("do") == "upload" and not self.photos:
                return "nincs fotó"
        return ""

    def preview(self) -> list[str]:
        """A választások, ahogy a jóváhagyó ablakban megjelennek (pl. „Állapot: Nagyon jó”)."""
        lines = []
        for step in self.recording.get("steps") or []:
            if step.get("do") == "choice":
                label = FIELD_LABELS.get(step["field"], step["field"])
                lines.append(f"{label}: {self.values.get(step['field']) or '–'}")
        return lines

    # --- lépések ---

    def _find(self, want: dict[str, Any], optional: bool = False) -> dict[str, Any] | None:
        found = self.browser.locate(want, timeout=1.5 if optional else self.timeout)
        if found is None and not optional:
            raise ReplayMiss(f"nem találom ezt: „{want.get('label') or want.get('field') or want.get('kind')}”")
        return found

    def _click(self, want: dict[str, Any], risky: bool = False, optional: bool = False) -> bool:
        found = self._find(want, optional=optional)
        if found is None:
            return True
        result = self.browser.click(found["id"])
        if result.startswith("A felhasználó nem engedélyezte"):
            return False
        if risky:
            self.published = True
        return True

    def _type(self, want: dict[str, Any], text: str, submit: bool = False) -> None:
        found = self._find(want)
        result = self.browser.type_text(found["id"], text, submit=submit)
        if result.startswith("A felhasználó nem engedélyezte"):
            raise ReplayMiss("nem engedélyezted az elküldést")

    def _act(self, act: dict[str, Any], value: str) -> None:
        want = dict(act.get("el") or {})
        if act["do"] == "click":
            if act.get("from_value"):
                want.update(label=value, loose=True, field="", href="", ordinal=0)
            if not self._click(want):
                raise ReplayMiss("nem engedélyezted")
        elif act["do"] == "type":
            self._type(want, value if act.get("from_value") else act.get("text", ""), bool(act.get("submit")))

    def run(self, start_url: str) -> ReplayOutcome:
        """Végigjátssza a receptet. Közzététel után semmit nem kezd újra, legfeljebb bizonytalan.

        Közben a böngésző „modell nélküli” üzemmódban van: emberi tempóban dolgozik (gondolkodási,
        olvasási szünetek, egérmozgás, gépelés; lásd humanize.py), nem pillanatok alatt."""
        previous = getattr(self.browser, "auto", False)
        if self.browser is not None:
            self.browser.auto = True
        try:
            return self._run(start_url)
        finally:
            if self.browser is not None:
                self.browser.auto = previous

    def _run(self, start_url: str) -> ReplayOutcome:
        try:
            self.progress("a hirdetési űrlap megnyitása" if self.recording.get("task") == "post"
                          else "a hirdetés megnyitása")
            self.browser.open(start_url)
            steps = self.recording.get("steps") or []
            for step in steps:
                self.check()
                do = step.get("do")
                if do == "goto":
                    self.browser.open(step["url"])
                elif do == "upload":
                    self.progress(f"fotók ({len(self.photos)} db)")
                    element = None
                    if step.get("el"):
                        found = self._find(step["el"], optional=True)
                        element = found["id"] if found else None
                    self.browser.upload_files(self.photos, element)
                elif do == "type":
                    if step.get("value"):
                        text = self.values.get(step["value"], "")
                        if not text:
                            continue   # ennél a tárgynál nincs ilyen adat (pl. márka)
                        self.progress(FIELD_LABELS.get(step["value"], step["value"]).lower())
                    else:
                        text = step.get("text", "")
                    self._type(step["el"], text, bool(step.get("submit")))
                elif do == "choice":
                    name = step["field"]
                    actions, value = self._memory_for(name)
                    if actions is None:
                        raise ReplayMiss(f"{FIELD_LABELS.get(name, name)}: új érték")
                    self.progress(f"{FIELD_LABELS.get(name, name).lower()}: {value or '–'}")
                    if step.get("opener"):
                        if not self._click(step["opener"]):
                            raise ReplayMiss("nem engedélyezted")
                    for act in actions:
                        self.check()
                        self._act(act, value)
                    self.summary.append(f"{FIELD_LABELS.get(name, name)}: {value or '–'}")
                elif do == "click":
                    if step.get("risky"):
                        self.progress("közzététel (jóváhagyásra vár)" if self.recording.get("task") == "post"
                                      else "levétel")
                    if not self._click(step["el"], risky=bool(step.get("risky")), optional=bool(step.get("optional"))):
                        return ReplayOutcome("declined", "nem engedélyezted", published=self.published,
                                             summary=self.summary)
        except ReplayMiss as exc:
            status = "uncertain" if self.published else "miss"
            return ReplayOutcome(status, str(exc), url=self.browser.url, published=self.published,
                                 summary=self.summary)
        except Exception as exc:  # noqa: BLE001 - pl. BrowserError: a modell veszi át (vagy ellenőriz)
            if type(exc).__name__ == "Cancelled":
                raise
            status = "uncertain" if self.published else "miss"
            return ReplayOutcome(status, str(exc).splitlines()[0][:200], url=self.browser.url,
                                 published=self.published, summary=self.summary)
        if not self.published:
            return ReplayOutcome("miss", "nem jutott el a közzétételig", url=self.browser.url, summary=self.summary)
        wanted = (self.recording.get("success") or {}).get("url")
        current = self.browser.url
        if wanted and url_shape(current) == wanted:
            return ReplayOutcome("done", url=current, published=True, summary=self.summary)
        return ReplayOutcome("uncertain", "nem biztos, hogy sikerült", url=current, published=True,
                             summary=self.summary)
