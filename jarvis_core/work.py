"""A Munka menü. Az első munka az Adás/Vétel: Jarvis itt az eladásra szánt holmikon dolgozik.

Egy eladás menete (a JarvisApp az ágens-szálon futtatja, lásd app.run_work):
  1. a leírásodból egy modellhívással kiolvassa a hirdetés adatait (ListingExtractor),
  2. ha a kiválasztott oldalakhoz hiányzik valami, visszakérdez, és megvárja a válaszod,
  3. ha kérted, megnézi, mennyiért kínálnak hasonlót; ha az árad sokkal drágább, megkérdezi,
     maradjon-e (csak ajánlás, a döntés a tiéd),
  4. sorban feltölti minden kiválasztott oldalra a saját böngészőjében (ehhez a Munka menü
     saját beszélgetése és eszközei kellenek), a közzététel előtt mindig rákérdez.
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
from .llm import Cancelled
from .replay import FlowRecorder, FlowStore, Replayer, ReplayOutcome, flow_key, item_values
from .selling import (CONDITIONS, NEEDS, NOT_POSTED, PARCEL_SIZES, PLATFORMS, POSTED, FAILED, Item,
                      ItemError, ItemStore, ListingExtractor, Platform, PriceResearcher, format_price,
                      missing_needs, parse_price, price_verdict, question_for)
from .textutil import fold
from .tools import Toolbox, ToolError, params
from .vinted import ChoiceMemory, FillOutcome, VintedDelister, VintedFiller

# A Munka menü beszélgetése ezeket az eszközöket látja; a böngésző ugyanaz, mint a Beszélgetésé.
SHARED_TOOLS = {"get_datetime", "web_search", "read_webpage", "browser_open", "browser_click",
                "browser_type", "browser_fill", "browser_scroll", "browser_back", "browser_read",
                "browser_look"}
WORK_ONLY_TOOLS = {"item_list", "item_get", "item_update", "item_research", "item_post", "item_mark",
                   "item_delist", "browser_upload_photos"}
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


def research_summary(research: dict[str, Any]) -> str:
    if not research:
        return "Árkutatás: még nem volt."
    similar = research.get("similar") or []
    if not similar:
        errors = "; ".join(research.get("errors") or [])
        text = f"Árkutatás („{research.get('query', '')}”): nem találtam elég hasonló hirdetést."
        return text + (f" ({errors})" if errors else "")
    sites = sorted({entry["site"] for entry in similar})
    text = (f"Árkutatás („{research.get('query', '')}”): {len(similar)} hasonló hirdetés "
            f"({', '.join(sites)}), {format_price(research.get('min'))} és {format_price(research.get('max'))} "
            f"között, középérték {format_price(research.get('median'))}. "
            f"Javasolt ár: {format_price(research.get('suggested'))}.")
    if research.get("reason"):
        text += " " + str(research["reason"])
    return text


_AMOUNT_RE = re.compile(r"(\d[\d .]*\d|\d)\s*(ezer|e\b|k\b)?")
# „Legyen a javasolt”, „oké, mehet”, „vidd lejjebb”: a javasolt ár
_SUGGESTED_WORDS = {"legyen", "javasolt", "javasoltat", "javasoltra", "javaslat", "javaslatod", "rendben",
                    "oke", "ok", "mehet", "lejjebb", "csokkentsd", "olcsobb", "olcsobban"}


def interpret_price_reply(text: str) -> str | int | None:
    """Árdöntésre adott válasz: "keep" (maradjon), "suggested" (legyen a javasolt), szám, vagy None."""
    folded = fold(text or "")
    amounts = []
    for number, thousand in _AMOUNT_RE.findall(folded):
        try:
            value = parse_price(number)
        except ItemError:
            continue
        if value:
            amounts.append(value * 1000 if thousand and value < 10_000 else value)
    if amounts:
        return amounts[-1]   # „ne 150 ezer legyen, hanem 120 ezer”: az utolsó a kért
    tokens = re.findall(r"\w+", folded)
    if any(t.startswith("marad") for t in tokens) or {"ne", "nem"} & set(tokens):
        return "keep"        # a kérdés: „Maradjon a te árad, vagy legyen …?”; a nem a változtatásra nem
    if any(t in _SUGGESTED_WORDS for t in tokens):
        return "suggested"
    return None


@dataclass
class Step:
    kind: str          # "ask" (visszakérdez), "price" (árdöntésre vár), "post" (mehet a feltöltés)
    text: str = ""


class WorkDesk:
    """Az eladásra szánt tárgyak, és amit a Jarvis velük csinál."""

    def __init__(self, cfg: Config, llm: Any, toolbox: Toolbox,
                 on_change: Callable[[dict[str, Any]], None] | None = None,
                 on_remove: Callable[[str], None] | None = None,
                 confirm: Callable[[str], bool] | None = None,
                 platforms: dict[str, Platform] | None = None,
                 check_stop: Callable[[], None] | None = None):
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
        self.current: tuple[str, str] | None = None   # (tárgy, oldal), amit épp feltölt
        self.approved = False    # feltöltés közben jóváhagytál-e egy kattintást (pl. a közzétételt)
        # (tárgy, oldal), amiről a modell épp megnézi, fent van-e: ilyenkor újra feltölteni nem szabad.
        self.checking: tuple[str, str] | None = None
        # (tárgy, oldal), amit a Vinted-levevő ebben a kérésben már megpróbált: az item_delist nem kezdi újra.
        self.autodelisted: tuple[str, str] | None = None
        self._settings_path = self.root / "beallitasok.json"
        self.settings = self._load_settings()
        # Begyakorolt lépések: a modell sikeres feltöltéseiből és levételeiből tanul (replay.py).
        self.flows = FlowStore(self.root / "folyamatok")
        # A Vinted-kitöltő (vinted.py) választásai, pl. egy tárgyfajta kategóriaútja: legközelebb modell nélkül.
        self.choices = ChoiceMemory(self.root / "valasztasok" / "vinted.json")
        # Hibacsomagok (dossier.py): ha egy feltöltés vagy levétel elakad, minden a javításhoz.
        self.dossiers = Dossiers(self.root / "hibak")
        self.trail: list[dict[str, Any]] = []      # a mostani feltöltés/levétel böngészős lépései
        # Emberi tempó: oldal → mikortól jöhet a következő közzététel vagy levétel.
        self._next_allowed: dict[str, float] = {}
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
        self._register(toolbox)

    @property
    def agent_tools(self) -> set[str]:
        return (SHARED_TOOLS | WORK_ONLY_TOOLS) & set(self.toolbox.names())

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
        """Adatok a leírásból, hiányzó adatok, árellenőrzés. Megmondja, mi a következő lépés."""
        item = self.store.get(item_id)
        if not item.targets:
            raise ItemError("Pipáld be, hova töltsem fel.")
        if not item.photos:
            raise ItemError("Ehhez a tárgyhoz még nincs fotó.")
        progress("a leírásod értelmezése")
        item = self.extractor.extract(item_id)
        missing = missing_needs(item, self.settings)
        if missing:
            question = question_for(item, missing)
            self.store.set_pending(item_id, {"kind": "info", "question": question})
            return Step("ask", question)
        if item.research_wanted and not item.research.get("at") and self.researcher is not None:
            try:
                item = self.researcher.research(item_id, lambda detail: progress("árkutatás – " + detail),
                                                only=item.targets)
            except ItemError as exc:   # az árkutatás csak ajánlás: ha nem megy, attól még feltöltjük
                self.store.set_pending(item_id, None)
                return Step("post", f"Az árkutatás nem sikerült ({exc}), marad az árad.")
            verdict = price_verdict(item)
            if verdict.pause:
                self.store.set_pending(item_id, {"kind": "price", "question": verdict.text,
                                                 "suggested": verdict.suggested})
                return Step("price", verdict.text)
            self.store.set_pending(item_id, None)
            return Step("post", verdict.text)
        self.store.set_pending(item_id, None)
        return Step("post")

    def answer(self, item_id: str, text: str) -> Item:
        """A visszakérdezésre adott válasz a leírás mellé kerül; a következő menet már ezzel számol."""
        item = self.store.get(item_id)
        self.store.update(item_id, prompt=(item.prompt + "\n" + text.strip()).strip())
        return self.store.set_pending(item_id, None)

    def decide_price(self, item_id: str, choice: str | int) -> Item:
        """Árdöntés: "keep" = marad az ár, "suggested" = a javasolt, vagy egy konkrét összeg."""
        item = self.store.get(item_id)
        if choice == "suggested":
            choice = item.pending.get("suggested") or item.research.get("suggested") or "keep"
        if choice != "keep":
            self.store.update(item_id, user=True, price=choice)
        return self.store.set_pending(item_id, None)

    def pending_item(self) -> Item | None:
        """A legutóbb visszakérdezett tárgy (a Munka menüben írt válasz ehhez tartozik)."""
        waiting = [i for i in self.store.all() if i.pending.get("kind")]
        return max(waiting, key=lambda i: i.updated) if waiting else None

    def targets_left(self, item_id: str) -> list[str]:
        item = self.store.get(item_id)
        return [t for t in item.targets if t in self.platforms
                and item.platforms.get(t, {}).get("status") != POSTED]

    def post_request(self, item: Item, key: str) -> str:
        site = self.platforms[key]
        return (f"Töltsd fel a(z) „{item.name}” tárgyat (azonosító: {item.id}) a(z) {site.name} oldalra "
                f"(platform: {site.key}). Kezdd az item_post eszközzel, a végén jelöld meg az item_mark eszközzel.")

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
        ellenőrizhesd, mielőtt jóváhagyod."""
        if self.current is None:
            return ""
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

    def _on_browser_action(self, event: dict[str, Any]) -> None:
        if self.recorder is not None:
            self.recorder.add(event)
        if self.current is not None:
            self.trail.append(dict(event))
            del self.trail[:-200]
            if event.get("do") == "click" and event.get("risky"):
                self.note_publish(self.current[1])

    def begin_trail(self) -> None:
        """Egy feltöltés vagy levétel kezdete: innen jegyezzük a lépéseket a hibacsomaghoz."""
        self.trail = []

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

    def _tool_gap(self, item_id: str, key: str) -> None:
        """Ugyanez a modell eszközeiben (item_post, item_delist): a beszélgetésből indított feltöltés vagy
        levétel is tartja a szünetet."""
        try:
            self.wait_gap(item_id, key)
        except Cancelled:
            raise ToolError("Megszakítottad.") from None

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
        photos = [str(p) for p in self.store.photo_paths(item.id)] if task == "post" else []
        replayer = Replayer(self.toolbox.browser, recording, item_values(item, self.settings), photos,
                            check=self.check_stop, progress=progress)
        problem = replayer.plan_problem()
        if problem:
            return ReplayOutcome("no_plan", problem)
        start = site.new_url if task == "post" else item.platforms.get(key, {}).get("url", "")
        if not start:
            return ReplayOutcome("no_plan", "nem tudom a hirdetés webcímét")
        self.recorder = None
        self.begin_trail()
        self.replay_preview = replayer.preview()
        try:
            outcome = replayer.run(start)
        finally:
            self.replay_preview = []
        if outcome.status == "done":
            self.flows.update_stats(flow, ok=True)
        elif outcome.status == "miss":
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
                              [str(p) for p in self.store.photo_paths(item.id)], memory=self.choices,
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

    def continue_request(self, item: Item, key: str, outcome: FillOutcome, page: str) -> str:
        """A Vinted-kitöltő után a modell a nyitott űrlapon folytatja (nem kezdi elölről)."""
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
        site = self.platforms[key]
        return (f"Vedd le a(z) „{item.name}” hirdetést (azonosító: {item.id}) a(z) {site.name} oldalról "
                f"(platform: {key}), mert eladtuk. Kezdd az item_delist eszközzel, a végén jelöld meg az item_mark "
                "eszközzel (status: nincs fent). A levételt a felhasználó már jóváhagyta.")

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
        """Ami legalább `days` napja fent van, nem kelt el, és egy hete nem szóltunk róla."""
        now = time.time() if now is None else now
        out = []
        for item in self.store.all():
            if item.sold:
                continue
            posted = [float(e.get("at") or now) for e in item.platforms.values() if e.get("status") == POSTED]
            if not posted or now - min(posted) < days * 86400:
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
        lines.append(f"Fotók: {len(item.photos)} db (a browser_upload_photos tölti fel őket)")
        return "\n".join(lines)

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
        for key in item.targets:
            entry = item.platforms.get(key, {})
            states.append(f"{self._site_name(key)}: {entry.get('status') or 'még nincs fent'}")
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
            self.store.update(found.id, user=True, **values)
        except ItemError as exc:
            raise ToolError(str(exc)) from None
        return f"Módosítva ({found.name}): " + ", ".join(FIELD_NAMES.get(k, k) for k in values)

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
        self._tool_gap(found.id, platform)
        self.store.clear_note(found.id, platform)   # az előző próbálkozás megjegyzése már nem érvényes
        self.current = (found.id, platform)
        self.approved = False
        self.begin_trail()
        self.store.set_busy(found.id, f"Feltöltés: {site.name}…")
        self.start_recording(found, platform, "post", site.new_url)
        try:
            page = self.toolbox.browser.open(site.new_url)
        except BrowserError as exc:
            self.recorder = None
            raise ToolError(str(exc)) from None
        return (f"A hirdetés adatai ({found.id}):\n{self.listing_block(found, platform)}\n\n"
                f"Tippek ({site.name}): {site.hints}\n"
                "A végső feltöltés előtt a rendszer jóváhagyást kér a felhasználótól. Ha bejelentkezés "
                "vagy robot-ellenőrzés kell, állj meg, és kérd meg a felhasználót." + KEEP_ABOVE + page)

    def tool_upload_photos(self, element: int | None = None, item: str = "") -> str:
        ref = item or (self.current[0] if self.current else "")
        if not ref:
            raise ToolError("Melyik tárgy fotóit töltsem fel? Add meg az azonosítóját.")
        found = self._item(ref)
        paths = [str(p) for p in self.store.photo_paths(found.id)]
        if not paths:
            raise ToolError("Ennek a tárgynak nincs fotója.")
        browser = self.toolbox.browser
        host = browser.current_host()
        known = any(host == domain or host.endswith("." + domain)
                    for site in self.platforms.values() for domain in site.domains)
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
        found = self._item(item)
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
        self._tool_gap(found.id, platform)
        self.current = (found.id, platform)
        self.begin_trail()
        self.store.set_busy(found.id, f"Levétel: {site.name}…")
        prefix, clicked = "", False
        if self.can_autodelist(found.id, platform) and self.autodelisted != (found.id, platform):
            auto = self.autodelist(found.id, platform)
            answer = self._after_autodelist(found, platform, auto)
            if answer is not None:
                return answer
            found = self.store.get(found.id)
            entry = found.platforms.get(platform) or {}
            clicked = auto.status == "uncertain"     # a Törlésre már rákattintott: csak meg kell nézni
            prefix = (f"A Törlésre rákattintottam, de nem biztos, hogy sikerült ({auto.reason}). Nézd meg az "
                      "oldalt: ha a hirdetés már nincs fent, csak jelöld meg; ha még fent van, fejezd be.\n" if clicked
                      else f"A modell nélküli levétel nem ment végig ({auto.reason}); folytasd te.\n")
        if not clicked and self.can_replay(found.id, platform, "delist"):
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
