"""Jarvis "agya": betöltés, a kérések feldolgozása, beszéd és hallgatás.

A konzolos és a grafikus felület is ezt használja; csak a megjelenítés (UI) különbözik.
Minden kérés ugyanazon a háttérszálon (ágens-szál) fut egymás után, mert a böngésző-
vezérlés (Playwright) csak abból a szálból használható, amelyik elindította.

Megszakítás: a stop_event (Állj gomb, Ctrl+C) leállítja a felvételt, a modell válaszát és az
eszközhívások közötti továbblépést, a felolvasást pedig azonnal elhallgattatja.
"""
from __future__ import annotations

import contextlib
import gc
import itertools
import queue
import re
import secrets
import threading
import time
import traceback
import urllib.parse
from pathlib import PurePath
from typing import Any, Callable, Protocol

from . import gpu, memory, web
from .assistant import Assistant
from .config import Config, adopt_legacy_data, app_data, resolve_path
from .creative import CreativeDesk, StoryError
from .llm import FULL_GPU, Cancelled, OllamaClient, OllamaError, gpu_share, is_runner_crash
from .notices import NoticeBoard
from .ollama_log import (CACHE_SETTING, CHECKPOINT_SETTING, PARALLEL_SETTING, ServerLog, explain, find_cause,
                         pagefile_tip, server_parallel, user_setting)
from .browser import BrowserError
from .scheduler import Hooks, Job, Scheduler, standby_ok, within_hours
from .selling import NOT_POSTED, POSTED, ItemError, format_price, parse_price, price_verdict
from .textutil import fold
from .tools import Toolbox, ToolError, params
from .watches import WatchError, WatchList
from .work import (DELISTED_NOTE, PRICE_CHOICES, WORK_ONLY_TOOLS, WorkDesk, interpret_price_reply, load_work_prompt,
                   mmss)

MODES = ("chat", "work")
# Az Adás/Vétel feladatai, amik egy tárgyon dolgoznak (a kártyán látszik, hogy épp mi történik)
BUSY_ACTIONS = {"sell", "reply", "price", "post", "research", "delist", "sold", "approve", "upload"}
# A felhasználó kérései (a háttérmunka és a belső lépések nem ilyenek): ezek szakítják meg a háttérmunkát.
INTERACTIVE = {"text", "work", "creative"}
# Az eladás előkészítése (a fotók átnézése, az adatok, a visszakérdezés, az ár) böngésző nélkül megy: ezek a saját
# szálukon rögtön indulnak, a kereskedéssel párhuzamosan; ami utána böngészőt kér (árkutatás egy böngészős oldalon,
# a feltöltés), az elsőbbséggel kerül az ágens-szálra (a futó kereskedés a következő lépésénél átadja).
PREP_ACTIONS = {"sell", "approve", "reply", "price"}
# Egy feltöltés vagy más eladási lépés után ennyi másodperc múlva folytatódhat a kereskedés (a beszélgetés után a
# [scheduler] quiet_minutes); ha közben újabb feltöltés jön, a kereskedés úgyis átadja a böngészőt.
AFTER_WORK = 60.0

# Szünet beírva is („pihenj”); folytatni a gombbal vagy a telefonról lehet.
PAUSE_PHRASES = {"pihenj", "szunet", "tarts szunetet", "szunetelj", "engedd el a videokartyat",
                 "szabaditsd fel a videokartyat", "allits le mindent"}
PAUSED_REPLY = "Szünetelek, minden modell le van állítva. A Folytatás gombbal (vagy a telefonról) újraindulok."
WAKE_FAILED = "Most nem tudtam felébredni, mert a nyelvi modell nem töltődött be. Próbáld újra kicsit később."
# A modell leállása után ennyi másodperc múlva nézzük meg az Ollama naplójában, miért (addigra beleírta).
RESTART_LOG_DELAY = 5.0
# A Munka menü egy körének felső határa (token): ott a válasz egy-két mondat vagy egy eszközhívás (a hosszú
# hirdetésszöveg is belefér); egy beragadt válasz így sem foglalja percekig a modellt és a nyitott űrlapot.
WORK_MAX_TOKENS = 1024
# Készenlétben a háttérmunka egy-egy modellkérdése után a modell csak ennyi ideig marad a videókártyán.
STANDBY_KEEP_ALIVE = "5m"
# Amíg az automata kereskedés fut, a modell készenlétben is tovább marad betöltve: a friss hirdetések felmérésénél
# (azonnali figyelő, böngészés) így nem kell minden kérdés előtt 10–20 másodpercig betölteni.
TRADING_KEEP_ALIVE = "30m"
# Ha a modell egy része a processzorra került: ezekkel a KV-gyorsítótár-típusokkal a kontextus már kicsi.
SMALL_KV_TYPES = {"q8_0", "q4_0"}
KV_SETTING = "OLLAMA_KV_CACHE_TYPE"

# Az állapotok, ahogy a felületen megjelennek.
STATE_TEXT = {
    "starting": "Indulok",
    "idle": "Készen állok",
    "thinking": "Gondolkodom",
    "working": "Dolgozom",
    "paused": "Szünetelek",
    "standby": "Készenlétben",
    "writing": "Írok",
}
TOOL_ACTIVITY = {
    "web_search": "Keresek a weben", "read_webpage": "Olvasom az oldalt",
    "browser_open": "Böngészek", "browser_click": "Böngészek", "browser_type": "Böngészek",
    "browser_fill": "Kitöltöm az űrlapot", "browser_choose": "Választok a listából", "browser_do": "Böngészek",
    "browser_scroll": "Böngészek", "browser_back": "Böngészek", "browser_read": "Olvasom az oldalt",
    "browser_look": "Nézem az oldalt", "look_at_screen": "Nézem a képernyőt",
    "find_files": "Fájlokat keresek", "read_text_file": "Olvasom a fájlt", "open_file": "Megnyitom a fájlt",
    "open_app": "Elindítom a programot", "open_website": "Megnyitom az oldalt",
    "system_status": "Megnézem a gépet", "save_note": "Jegyzetelek", "set_timer": "Időzítőt állítok",
    "get_datetime": "Megnézem az időt", "run_powershell": "Parancsot futtatok",
    "item_list": "Átnézem a tárgyakat", "item_get": "Átnézem a tárgyat", "item_update": "Javítom a hirdetést",
    "item_research": "Árakat nézek", "item_post": "Feltöltöm a hirdetést",
    "browser_upload_photos": "Feltöltöm a fotókat", "item_mark": "Megjelölöm",
    "item_delist": "Leveszem a hirdetést", "web_research": "Több oldalt olvasok",
    "watch_add": "Figyelőt állítok", "watch_list": "Átnézem a figyelőket", "watch_remove": "Törlöm a figyelőt",
    "watch_check": "Megnézem a figyelőket", "messages_list": "Átnézem az üzeneteket",
    "message_draft": "Válaszvázlatot írok",
}
DEFAULT_SYSTEM_PROMPT = (
    "Te {name} vagy, egy magyarul beszélő személyi asszisztens. Röviden, természetesen "
    "válaszolj, markdown nélkül, és használd az eszközöket, ha a gépen kell valamit csinálni."
)


class UI(Protocol):
    """Amit a Jarvis a felülettől elvár (konzol vagy grafikus ablak)."""

    def state(self, state: str, detail: str = "") -> None: ...
    def user(self, text: str, via: str) -> None: ...
    def delta(self, text: str) -> None: ...
    def assistant_end(self, text: str, streamed: bool) -> None: ...
    def assistant_abort(self) -> None: ...
    def tool_start(self, call_id: int, name: str, args: dict[str, Any]) -> None: ...
    def tool_done(self, call_id: int, name: str, args: dict[str, Any], result: str) -> None: ...
    def notice(self, text: str) -> None: ...
    def info(self, text: str) -> None: ...
    def warn(self, text: str) -> None: ...
    def error(self, text: str) -> None: ...
    def timing(self, text: str) -> None: ...
    def confirm(self, prompt: str, stop_event: threading.Event) -> bool: ...


def secs(value: float) -> str:
    """Másodperc magyar tizedesvesszővel: 1,2 mp."""
    return f"{value:.1f}".replace(".", ",") + " mp"


def load_system_prompt(cfg: Config, ui: UI | None = None) -> str:
    path = resolve_path(cfg.assistant.system_prompt_file)
    try:
        template = path.read_text(encoding="utf-8")
    except OSError:
        if ui is not None:
            ui.warn(f"Nem található a rendszerprompt ({path}), az alapértelmezettet használom.")
        template = DEFAULT_SYSTEM_PROMPT
    return template.replace("{name}", cfg.assistant.name).strip()


def _words(text: str) -> str:
    """Az írásjelek és a megszólítás nélkül ("Jarvis, pihenj!" → "pihenj")."""
    words = re.sub(r"[^\w\s]", " ", fold(text)).split()
    if words and words[0] == "jarvis":
        words = words[1:]
    return " ".join(words)


def is_pause_request(text: str) -> bool:
    return _words(text) in PAUSE_PHRASES


def model_problem(exc: BaseException, short: bool = False) -> str:
    """Az Ollama-hiba érthetően: a naplóba (és röviden a hirdetés kártyájára)."""
    if is_runner_crash(exc):
        if short:
            return "A nyelvi modell (Ollama) közben leállt, újraindítás után is. Az Újra gombbal folytatom."
        return ("A nyelvi modell (Ollama) futás közben leállt, és újraindítás után is. Ez az Ollama hibája, nem "
                "a tiéd: próbáld újra kicsit később. Ha gyakran előfordul, lásd a README Hibaelhárítás részét.")
    return f"A nyelvi modell hibát adott: {exc}" if short else str(exc)


def _host(url: Any) -> str:
    text = str(url or "")
    parsed = urllib.parse.urlparse(text if "://" in text else "https://" + text)
    return parsed.hostname or text


def tool_label(name: str, args: dict[str, Any]) -> str:
    """Egy eszközhívás emberi nyelven (a felülethez)."""
    a = args or {}
    labels = {
        "web_search": lambda: f"Keresés: {a.get('query', '')}",
        "read_webpage": lambda: f"Oldal olvasása: {_host(a.get('url'))}",
        "open_website": lambda: f"Megnyitás a böngésződben: {_host(a.get('url'))}",
        "browser_open": lambda: f"Megnyitás: {_host(a.get('url'))}",
        "browser_click": lambda: f"Kattintás a(z) {a.get('element')}. elemre",
        "browser_type": lambda: f"Gépelés: „{a.get('text', '')}”",
        "browser_fill": lambda: f"Űrlap kitöltése ({len(a.get('fields') or [])} mező)",
        "browser_choose": lambda: f"Választás a(z) {a.get('element')}. listából: „{a.get('option', '')}”",
        "browser_do": lambda: (f"{len(a['actions'])} lépés egyszerre" if isinstance(a.get("actions"), list)
                               else "Több lépés egyszerre"),
        "browser_scroll": lambda: "Görgetés " + ("felfelé" if a.get("direction") == "up" else "lefelé"),
        "browser_back": lambda: "Vissza az előző oldalra",
        "browser_read": lambda: "Az oldal elolvasása",
        "browser_look": lambda: f"Ránézés az oldalra: {a.get('question', '')}",
        "look_at_screen": lambda: f"Ránézés a képernyőre: {a.get('question', '')}",
        "find_files": lambda: f"Fájlkeresés: {a.get('query') or a.get('extension') or ''}",
        "read_text_file": lambda: f"Fájl olvasása: {PurePath(str(a.get('path', ''))).name}",
        "open_file": lambda: f"Fájl megnyitása: {PurePath(str(a.get('path', ''))).name}",
        "open_app": lambda: f"Program indítása: {a.get('name', '')}",
        "save_note": lambda: "Jegyzet mentése",
        "set_timer": lambda: f"Időzítő: {a.get('minutes')} perc",
        "get_datetime": lambda: "Pontos idő",
        "system_status": lambda: "Gépállapot",
        "run_powershell": lambda: f"PowerShell: {a.get('command', '')}",
        "item_list": lambda: "A tárgyak listája",
        "item_get": lambda: f"Tárgy adatai: {a.get('item', '')}",
        "item_update": lambda: f"Hirdetés javítása: {a.get('item', '')}",
        "item_research": lambda: f"Árkutatás: {a.get('item', '')}",
        "item_post": lambda: f"Feltöltés indítása ({a.get('platform', '')}): {a.get('item', '')}",
        "item_delist": lambda: f"Levétel ({a.get('platform', '')}): {a.get('item', '')}",
        "web_research": lambda: f"Kutatás: {a.get('query', '')}",
        "watch_add": lambda: f"Figyelő: {a.get('query', '')}",
        "watch_list": lambda: "A figyelők listája",
        "watch_remove": lambda: f"Figyelő törlése: {a.get('watch', '')}",
        "watch_check": lambda: "A figyelők ellenőrzése",
        "browser_upload_photos": lambda: "Fotók feltöltése",
        "item_mark": lambda: f"Megjelölés: {a.get('status', '')}",
    }
    make = labels.get(name)
    return make() if make else name


class JarvisApp:
    STANDBY_CHECK_SECONDS = 20.0     # ilyen sűrűn nézi meg, visszaállhat-e készenlétbe

    def __init__(self, cfg: Config, ui: UI):
        self.cfg = cfg
        self.ui = ui
        self.name = cfg.assistant.name
        self.llm = OllamaClient(cfg.llm)
        self.stop_event = threading.Event()
        self.llm.stop_event = self.stop_event
        self.llm.on_runner_restart = self._on_runner_restart
        self.llm.fit_memory = app_data(cfg) / "modell_illesztes.json"
        # Az Ollama naplója: ha a modell leáll, ebből derül ki, miért (lásd _explain_restart).
        self.server_log = ServerLog()
        self.model_restarts: list[dict[str, str]] = []   # a leállások okai (a hibacsomagba is bekerülnek)
        self._restart_advice: set[str] = set()           # amelyik okról már részletesen szóltunk
        self.exit_requested = threading.Event()
        self.toolbox: Toolbox | None = None
        self.assistant: Assistant | None = None          # a Beszélgetés menü (bármit kérhetsz)
        self.work: WorkDesk | None = None                # a Munka menü tárgyai és eszközei
        self.work_assistant: Assistant | None = None     # a Munka menü saját beszélgetése
        self.creative: CreativeDesk | None = None        # a Kreatív menü (történetek, karakterek)
        self.mode = "chat"                               # melyik menü van elöl (a hangos kérésekhez)
        self.model_loaded = False
        self.context_length: int | None = None
        self.requests: queue.Queue = queue.Queue()
        self._outstanding = 0
        self._outstanding_lock = threading.Lock()
        self._agent: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._phase, self._detail = "starting", ""
        self._shown_state: tuple[str, str] | None = None
        self._tool_ids = itertools.count(1)
        self._current_tool_id = 0
        # Szünet: minden modell leállítva, a videókártya és a memória szabad (Szünet gomb).
        self.paused = False
        # Készenlét: szünet, amiből egy kérés magától felébreszti (a Szünet gombos szünetből nem).
        # standby_mode: készenlétben indult (gépindításkor); tétlenség után oda tér vissza.
        self.standby = False
        self.standby_mode = False
        self._closing = threading.Event()
        # egy kérésen belül oldalanként és módonként egy hibacsomag
        self._dossiers_made: set[tuple[str, str, str, str]] = set()
        # A háttérmunka csak akkor indul, ha pár perce nem beszélgettetek (time.time() szerint).
        self.last_activity = time.time()
        self.last_work = 0.0                             # az utolsó eladási lépés vége (AFTER_WORK)
        self.last_prep = 0.0                             # az utolsó eladás-előkészítés vége (a készenléthez)
        self.preempt = threading.Event()                 # a felhasználó közbeszólt: a háttérmunka álljon meg
        # Az eladás előkészítése a saját szálán (PREP_ACTIONS): a sor, a szál, és hogy épp melyik tárgyon dolgozik.
        self._prep_requests: queue.Queue = queue.Queue()
        self._prep: threading.Thread | None = None
        self._prep_stop = threading.Event()              # az Állj gomb ezt is megállítja
        self.prepping = ""
        self._handoffs: dict[str, dict[str, Any]] = {}  # tárgy → amit az előkészítés után az ágens-szál folytat
        self._current_kind = ""                          # amin az ágens-szál épp dolgozik
        self._done_events: dict[str, threading.Event] = {}
        self._done_lock = threading.Lock()              # a telefonos szálak is hozzányúlnak
        self.notices: NoticeBoard | None = None
        self.watches: WatchList | None = None
        self.scheduler: Scheduler | None = None

    @property
    def busy(self) -> bool:
        return self._outstanding > 0

    def describe(self) -> dict[str, Any]:
        """A felület fejlécéhez: mi fut és hogyan."""
        return {
            "name": self.name,
            "model": self.cfg.llm.model,
            "work": self.work is not None and self.work_assistant is not None,
            "creative": self.creative is not None,
            "paused": self.paused,
            "standby": self.paused and self.standby,
            "api": bool(self.cfg.api.enabled),
        }

    def _hello(self) -> None:
        """A felület (és a telefon) megtudja, mi változott (pl. szünet után nincs hang)."""
        hello = getattr(self.ui, "hello", None)
        if callable(hello):
            try:
                hello(self.describe())
            except Exception:  # noqa: BLE001 - a felület hibája ne állítsa meg a Jarvist
                pass

    # --- indulás -------------------------------------------------------------------

    def load_model(self) -> bool:
        """Az indulás első lépése: Ollama-ellenőrzés és a nyelvi modell betöltése.

        A grafikus felület ezt még az ablak megnyitása előtt hívja, mert a böngészőablak is
        foglal videómemóriát, és az Ollama a betöltés pillanatában szabad memóriához igazodik.
        """
        if self.model_loaded:
            return True
        self._set_phase("starting")
        if not getattr(self, "_warned", False):   # a beállítási figyelmeztetések csak egyszer
            self._warned = True
            for warning in self.cfg.warnings:
                self.ui.warn(warning)
        self.ui.info(f"{self.name} indul...")
        try:
            version = self.llm.ensure_ready()
            self.ui.info(f"Ollama {version}, modell: {self.cfg.llm.model}")
            self.ui.info("Nyelvi modell betöltése a videókártyára...")
            t0 = time.perf_counter()
            status = self.llm.load_fitted(log=self.ui.info)
            self.model_loaded = True
            self.server_log.mark()    # egy leállás okát innentől keressük a naplóban
            self.ui.info(f"  kész ({secs(time.perf_counter() - t0)})")
        except OllamaError as exc:
            self.ui.error(str(exc))
            self._set_phase("error")
            return False
        self._report_model_placement(status)
        self._check_pagefile()
        return True

    def _check_parallel(self) -> None:
        """Hány modellhívás mehet egyszerre: a [llm] parallel, de legfeljebb amennyit a futó Ollama bír
        (OLLAMA_NUM_PARALLEL, alapból egy). Ha többet szeretnél, mint amennyit az Ollama enged, megmondja, hogyan."""
        wanted = max(1, int(self.cfg.llm.parallel or 1))
        try:
            proc = memory.ollama_process()
        except Exception:  # noqa: BLE001 - csak tanács: ha nem derül ki, az Ollama alapbeállításával számol
            proc = memory.OllamaProcess()
        server, note = server_parallel(proc, user_setting(PARALLEL_SETTING))
        slots = min(wanted, server)
        self.llm.gate.set_slots(slots)
        if slots > 1:
            self.ui.info(f"Egyszerre {slots} modellhívás mehet: a feltöltés és az automata kereskedés párhuzamosan "
                         "kérdezheti a modellt (a feltöltés elsőbbséget kap).")
        elif wanted > 1:
            self.ui.info(note or (
                "Egyszerre egy modellhívás megy, mert az Ollama így van beállítva; a feltöltés és a beszélgetés így is "
                "előbb jön a kereskedésnél. Hogy egyszerre kettő menjen: PowerShellben "
                f"„setx {PARALLEL_SETTING} 2”, majd lépj ki az Ollamából a tálcán, indítsd újra, és utána a Jarvist is "
                "(a kontextus memóriája így kétszer kell; ha nem fér el, a Jarvis kisebb kontextussal tölti be)."))

    def _check_pagefile(self) -> None:
        """Windows: ha nincs lapozófájl, vagy kicsi, a nagy modell, a böngésző és a többi program mellett könnyen
        elfogy a memória (a modell és a böngésző leáll). Ezt előre megmondjuk, egyszer."""
        if getattr(self, "_pagefile_checked", False):
            return
        self._pagefile_checked = True
        try:
            tip = pagefile_tip(memory.MemoryReport(pagefile=memory.pagefile_setting()))
        except Exception:  # noqa: BLE001 - csak tanács
            return
        if tip:
            self.ui.warn(tip)

    def start(self, standby: bool = False) -> bool:
        """Betölt mindent. Hamissal tér vissza, ha a nyelvi modell nem indítható.

        `standby`: készenléti indulás (pl. a gép bekapcsolásakor): a nyelvi modell nem töltődik be, csak az
        első kérésnél; addig a videókártya szabad."""
        if standby:
            self.standby_mode = True
        elif not self.load_model():
            return False

        # A böngészőprofil (bejelentkezések) és a Munka menü adatai a projektmappán kívül vannak,
        # hogy egy új verzió kicsomagolása után is megmaradjanak; a régieket egyszer áthozzuk.
        adopt_legacy_data(self.cfg.browser.profile_dir, "browser_profile", self.ui.info)
        if self.cfg.work.enabled:
            adopt_legacy_data(self.cfg.work.dir, "munka", self.ui.info)
        self._check_parallel()
        self.toolbox = Toolbox(self.cfg, notify=self.notify, confirm=self.confirm, llm=self.llm)
        for folder in self.toolbox.missing_folders:
            self.ui.warn(f"Nem létező mappa a beállításokban (kihagyva): {folder}")
        for missing in self.toolbox.unavailable:
            self.ui.warn(f"Nem elérhető: {missing}")
        self._start_work()
        self._start_creative()
        self._start_background()
        chat_tools = set(self.toolbox.names()) - WORK_ONLY_TOOLS
        self.ui.info("Eszközök: " + ", ".join(name for name in self.toolbox.names() if name in chat_tools))
        self.assistant = self._make_assistant(load_system_prompt(self.cfg, self.ui), chat_tools)
        if self.work is not None:
            self.work_assistant = self._make_assistant(
                load_work_prompt(self.cfg), self.work.agent_tools, max_rounds=int(self.cfg.work.max_tool_rounds),
                options={"temperature": float(self.cfg.work.temperature), "num_predict": WORK_MAX_TOKENS})
        if standby:
            self.paused = self.standby = True
            self.llm.keep_alive_override = self._standby_keep_alive
            for warning in self.cfg.warnings:
                self.ui.warn(warning)
            self._warned = True
            self.ui.info("Készenlétben indultam: a modellek az első kérésnél töltődnek be (10–20 másodperc).")
        return True

    def _sync_context(self) -> None:
        """A beszélgetések a valódi (betöltéskor kialkudott) kontextusmérettel számoljanak: Folytatás
        után ez más lehet, mint induláskor volt (pl. ha közben más is foglalja a videókártyát)."""
        if self.context_length:
            for assistant in (self.assistant, self.work_assistant):
                if assistant is not None:
                    assistant.context_tokens = self.context_length

    # --- háttérmunka, figyelők, értesítések -------------------------------------------------------

    def _start_background(self) -> None:
        """Értesítések, figyelők és az ütemező (a háttérmunka csak a start_agent után indul)."""
        base = app_data(self.cfg)
        self.notices = NoticeBoard(base / "ertesitesek.json", publish=getattr(self.ui, "notification", None))
        self.watches = WatchList(
            base / "figyelok.json",
            lambda query, site, recency: web.search(query, self.cfg.web, site=site, recency=recency,
                                                    max_results=10))
        self._register_watch_tools()
        hooks = Hooks(paused=lambda: self.paused, busy=lambda: self.busy,
                      last_activity=self._quiet_since, submit=self.submit_job,
                      preempted=self.preempt.is_set, notify=self.notify_event,
                      standby=lambda: self.paused and self.standby)
        self.scheduler = Scheduler(self.cfg.scheduler, base / "hatter.json", hooks)
        search_ready = self.toolbox is not None and self.toolbox.has("web_research")
        # A figyelők és a régóta fent lévő hirdetések modell nélkül futnak, ezért készenlétben is.
        self.scheduler.register(Job("watches", "Figyelők", lambda: float(self.cfg.scheduler.watch_minutes),
                                    self.watches.run, available=lambda: search_ready and bool(self.watches.all()),
                                    needs_models=False))
        if self.work is not None:
            self.scheduler.register(Job("stale", "Régóta fent lévő hirdetések",
                                        lambda: 24 * 60.0 if int(self.cfg.scheduler.stale_days) > 0 else 0,
                                        self._stale_job, needs_models=False))
        checker = self.work.checker if self.work is not None else None
        if checker is not None:
            # Modell nélkül, de a böngésző kell hozzá: készenlétben (bezárt böngészővel) nem fut.
            self.scheduler.register(Job("listings", "Fent lévő hirdetések ellenőrzése",
                                        lambda: 24 * 60.0 if int(self.cfg.scheduler.listing_checks) > 0 else 0,
                                        self._listings_job, available=lambda: bool(checker.due()),
                                        needs_models=False, needs_browser=True))
        messages = self.work.messages if self.work is not None else None
        if messages is not None and messages.platforms and messages.browser is not None:
            # A vevői üzenetek olvasása: modell nélkül, csak olvasva, de a böngésző kell hozzá (készenlétben nem fut).
            self.scheduler.register(Job("messages", "Vevői üzenetek", self._message_minutes, self._messages_job,
                                        available=messages.available, needs_models=False, needs_browser=True))
        instant = self.work.instant if self.work is not None else None
        if instant is not None:
            # Azonnali figyelő (Hardverapró, Jófogás): saját szálon, böngésző és modell nélkül; a Szünet leállítja, a
            # készenlét nem, és a beszélgetésre sem vár (csak a felolvasás vár, ha épp dolgozol vele).
            instant.notify = self._quick_notify
            instant.allowed = self._instant_allowed
            # amíg a modellkapu nem enged háttérhívást (pl. egy feltöltés vagy beszélgetés kérdezi a modellt, és csak
            # egy hívás mehet egyszerre), a modellre váró hirdetés vár, és a figyelő közben nézi tovább az oldalakat
            instant.model_ok = self.llm.gate.background_ok
            instant.start()
        trading = self.work.deals if self.work is not None else None
        if trading is not None:
            trading.journal.on_add = self._trading_note   # a Munka menü naplójába (és a konzolra) is
            # Párhuzamos böngészés: a Hardverapró és a Jófogás böngésző nélkül, a saját szálán, amíg a böngésző a
            # Vintedet és a Vaterát nézi (a Szünet ezt is megállítja, a készenlét nem).
            trading.start_fast_lane(self._instant_allowed, self.llm.gate.background_ok, self._quick_notify)
        deals = self.work.deals if self.work is not None else None
        if deals is not None and deals.browser is not None:
            # Olcsó hirdetések keresése: modell nélkül (csak a kevés jelöltre egy rövid kérdés), a böngésző kell hozzá.
            # Keresésenként más gyakoriság lehet: a munka a leggyakoribbhoz igazodik, és csak az esedékeseket nézi meg.
            self.scheduler.register(Job("deals", "Automata kereskedés",
                                        lambda: deals.every_minutes() if float(self.cfg.scheduler.deal_minutes or 0) > 0
                                        else 0.0,
                                        lambda ctx: deals.run(check=ctx.check, notify=ctx.notify, force=ctx.forced),
                                        available=deals.available, needs_models=False, needs_browser=True,
                                        note=deals.note, standby_ok=deals.browsing))

    def _register_watch_tools(self) -> None:
        box, watches = self.toolbox, self.watches
        if box is None or watches is None or not box.has("web_research"):
            return   # a figyelőkhöz webes keresés kell (pip install ddgs)
        every = max(1, round(float(self.cfg.scheduler.watch_minutes)))

        def add(query: str, site: str = "", recency: str = "hét") -> str:
            try:
                watch = watches.add(query, site, recency or "hét")
            except WatchError as exc:
                raise ToolError(str(exc)) from None
            return (f"Figyelő beállítva ({watch.id}): {watch.describe()}. Kb. {every} percenként megnézem, "
                    "amikor ráérek, és szólok, ha új találat jelenik meg.")

        def remove(watch: str) -> str:
            try:
                return f"Figyelő törölve: {watches.remove(watch).describe()}"
            except WatchError as exc:
                raise ToolError(str(exc)) from None

        def check(watch: str = "") -> str:
            try:
                targets = [watches.find(watch)] if watch else watches.all()
            except WatchError as exc:
                raise ToolError(str(exc)) from None
            if not targets:
                return watches.summary()
            lines = []
            for target in targets:
                try:
                    new = watches.check(target)
                except Exception as exc:  # noqa: BLE001
                    lines.append(f"{target.describe()}: a keresés nem sikerült ({exc}).")
                    continue
                if target.checks == 1:
                    lines.append(f"{target.describe()}: most néztem meg először, a jelenlegi találatokat megjegyeztem.")
                elif new:
                    lines.append(f"{target.describe()}: {len(new)} új: " +
                                 "; ".join(f"{n['title']} ({n['url']})" for n in new[:5]))
                else:
                    lines.append(f"{target.describe()}: nincs új találat.")
            return web.UNTRUSTED_NOTE + "\n" + "\n".join(lines)

        recency = {"type": "string", "enum": ["", "nap", "hét", "hónap", "év"],
                   "description": "Csak ilyen friss találatok számítsanak (alapból: hét)."}
        box.register("watch_add",
                     "Figyelőt állít be: a háttérben időnként újra keres, és szól, ha új találat jelenik meg "
                     "(pl. „szólj, ha lesz új Python-állás Debrecenben”, „figyeld az RTX 3080-as hirdetéseket”).",
                     params({"query": {"type": "string", "description": "A keresés, pl. 'python fejlesztő állás Debrecen'."},
                             "site": {"type": "string", "description": "Opcionális: csak ezen az oldalon, pl. 'profession.hu'."},
                             "recency": recency}, ["query"]), add)
        box.register("watch_list", "Felsorolja a figyelőket és a legutóbbi új találataikat.", params(),
                     watches.summary)
        box.register("watch_remove", "Töröl egy figyelőt (az azonosítójával vagy a keresés egy részével).",
                     params({"watch": {"type": "string", "description": "Az azonosító vagy a keresés része."}},
                            ["watch"]), remove)
        box.register("watch_check", "Most rögtön megnézi a figyelőket (vagy egyet), és elmondja, van-e új.",
                     params({"watch": {"type": "string", "description": "Opcionális: csak ezt a figyelőt."}}),
                     check, ephemeral=True)

    def _stale_job(self, ctx: Any) -> str:
        """Naponta egyszer: szól, ha egy hirdetés régóta fent van, de még nem kelt el (és ha a havi keret fogy, vagy túl
        sok megvett tárgy vár eladásra)."""
        assert self.work is not None
        self.work.deals.money_check(ctx.notify)
        days = int(self.cfg.scheduler.stale_days)
        stale = self.work.stale_items(days)
        if not stale:
            return "Nincs régóta álló hirdetés."
        names = ", ".join(f"{i.name} ({format_price(i.price)})" for i in stale[:5])
        more = f" és még {len(stale) - 5}" if len(stale) > 5 else ""
        ctx.notify("stale", "Régóta fent lévő hirdetések",
                   f"Ezek legalább {days} napja fent vannak, de még nem keltek el: {names}{more}. "
                   "Ha olcsóbbra vennéd valamelyiket, szólj.", items=[i.id for i in stale], quiet=True)
        self.work.mark_reminded([i.id for i in stale])
        return f"{len(stale)} régóta fent lévő hirdetés."

    def _listings_job(self, ctx: Any) -> str:
        """Naponta: néhány fent lévő hirdetés oldala (csak olvasva): elkelt-e vagy eltűnt-e valahol."""
        assert self.work is not None and self.work.checker is not None
        return self.work.checker.run(int(self.cfg.scheduler.listing_checks), check=ctx.check, notify=ctx.notify)

    def _message_minutes(self) -> float:
        if int(self.cfg.scheduler.message_reads) <= 0:
            return 0.0
        return float(self.cfg.scheduler.message_minutes or 0)

    def _messages_job(self, ctx: Any) -> str:
        """Időnként: a vevői üzenetek (csak olvasva, modell nélkül); ha új jött, szól."""
        assert self.work is not None
        return self.work.messages.run(int(self.cfg.scheduler.message_reads), check=ctx.check, notify=ctx.notify)

    def call_browser(self, fn: Callable[[Any], Any], timeout: float = 10.0) -> Any:
        """A Jarvis böngészőjén futtat egy rövid hívást az ágens szálán (bármelyik szálról hívható). Ha a Jarvis épp
        dolgozik, és nem kerül sorra időben, TimeoutError."""
        if self.toolbox is None or self.work is None or not self.work.browser_ready:
            raise BrowserError("A Jarvis böngészője nem fut.")
        call = {"fn": fn, "done": threading.Event(), "result": None, "error": None}
        self._submit(("browser", call))
        if not call["done"].wait(timeout):
            raise TimeoutError("A Jarvis most mással dolgozik; próbáld újra pár másodperc múlva.")
        if call["error"] is not None:
            raise call["error"]
        return call["result"]

    def _browser_call(self, call: dict[str, Any]) -> None:
        try:
            call["result"] = call["fn"](self.toolbox.browser)
        except Exception as exc:  # noqa: BLE001 - a hívó kapja meg
            call["error"] = exc
        finally:
            with self._outstanding_lock:
                self._outstanding -= 1
            call["done"].set()

    def _captcha_notice(self, reason: str) -> None:
        """A captcha miatt megállt feltöltés vagy levétel: értesítés a telefonra is, a „Megoldom a telefonon” gombbal
        (a Jarvis böngészőjének képe, és amire ott böksz, oda kattint; a captchát te oldod meg, nem a Jarvis)."""
        if "captcha" in reason:
            self._tell("error", "Captcha a Vinteden", reason,
                       actions=[{"action": "captcha", "label": "Megoldom a telefonon"}])

    def _tell(self, kind: str, title: str, text: str, **extra: Any) -> None:
        """Értesítés a telefonra is (a naplóba nem kerül újra): pl. hogy egy feltöltés kész, vagy hogy Jarvis
        kérdez valamit egy tárgyról. Ha a feladatot a telefonról adtad, így ott is megtudod."""
        if self.notices is not None:
            try:
                self.notices.add(kind, title, text, **extra)
            except Exception:  # noqa: BLE001 - az értesítés hibája ne rontsa el a munkát
                pass

    def notify_event(self, kind: str, title: str, text: str, **extra: Any) -> None:
        """A háttérmunka üzenete: értesítés (a telefonnak is) és a naplóba: a figyelőké a Beszélgetésbe, a többi
        (kereskedés, hirdetések, vevői üzenetek) a Munka menü naplójába."""
        if self.notices is not None:
            self.notices.add(kind, title, text, **extra)
        with self._log_channel("chat" if kind == "watch" else "work"):
            self.ui.notice(text)

    def _quiet_since(self) -> float:
        """Mióta csendes a Jarvis a háttérmunkának: a beszélgetés után [scheduler] quiet_minutes kell, egy eladási
        lépés (feltöltés, Pro adatlap) után csak AFTER_WORK másodperc; utána a kereskedés folytatódhat."""
        quiet = float(self.cfg.scheduler.quiet_minutes or 0) * 60
        return max(self.last_activity, self.last_work - max(0.0, quiet - AFTER_WORK))

    def _trading_note(self, text: str) -> None:
        """A kereskedés naplójának egy sora a Munka menü naplójába (bármelyik szálról): a Beszélgetés tiszta marad."""
        with self._log_channel("work"):
            self.ui.info(f"Kereskedés: {text}")

    def _standby_keep_alive(self) -> str:
        """Készenlétben ennyi ideig marad betöltve a modell egy kérdés után: amíg az automata kereskedés fut, tovább."""
        trading = self.work.deals if self.work is not None else None
        return TRADING_KEEP_ALIVE if trading is not None and trading.state_running() else STANDBY_KEEP_ALIVE

    def _instant_allowed(self) -> bool:
        """Az azonnali figyelő mehet-e most: a háttérmunka be van kapcsolva, nincs Szünet (a készenlét nem számít), az
        aktív órákban vagyunk ([scheduler] active_hours), és nem függesztetted fel a háttérmunkát."""
        now = time.time()
        if not self.cfg.scheduler.enabled or (self.paused and not self.standby):
            return False
        if not within_hours(self.cfg.scheduler.active_hours, now):
            return False
        scheduler = self.scheduler
        return scheduler is None or float(scheduler.state.get("snoozed_until") or 0) <= now

    def _quick_notify(self, kind: str, title: str, text: str, **extra: Any) -> None:
        """Az azonnali figyelő és a gyors sor értesítése (a saját szálukon): a Munka menü naplójába és a telefonra is."""
        if self.notices is not None:
            self.notices.add(kind, title, text, **extra)
        with self._log_channel("work"):
            self.ui.notice(text)

    @contextlib.contextmanager
    def _log_channel(self, mode: str):
        """Ezen a szálon az üzenetek ebbe a menübe kerüljenek (a felület szálanként tudja: own_channel), a többi szál
        beszélgetése marad, ahol volt. A konzolos felületnek nincs menüje: ott mindegy."""
        own = getattr(type(self.ui), "own_channel", None)
        if callable(own):
            with own(self.ui, mode):
                yield
        else:
            yield

    def _make_assistant(self, prompt: str, tools: set[str], max_rounds: int | None = None,
                        options: dict[str, Any] | None = None) -> Assistant:
        assistant = Assistant(self.cfg, self.llm, self.toolbox, prompt, on_tool_call=self._on_tool_done,
                              on_tool_start=self._on_tool_start, tool_names=tools, max_rounds=max_rounds,
                              options=options)
        assistant.stop_event = self.stop_event
        if self.context_length:
            assistant.context_tokens = self.context_length
        return assistant

    def _start_work(self) -> None:
        """A Munka menü (eladás): a tárgyak betöltése és az eszközei."""
        if not self.cfg.work.enabled:
            return
        try:
            self.work = WorkDesk(self.cfg, self.llm, self.toolbox,
                                 on_change=getattr(self.ui, "work_item", None),
                                 on_remove=getattr(self.ui, "work_removed", None),
                                 confirm=self.confirm, check_stop=self._check_stop,
                                 on_message=getattr(self.ui, "work_message", None))
            self.work.gap_waiter = self._respect_gap   # a közzététel előtti szünet a naplóban és az állapotsorban
        except Exception as exc:  # noqa: BLE001 - a Beszélgetés menü enélkül is működjön
            self.ui.warn(f"A Munka menü nem indult el ({exc}).")
            self.work = None

    def _start_creative(self) -> None:
        """A Kreatív menü: a történetek az adatmappában vannak, a modell csak íráskor kell."""
        if not self.cfg.creative.enabled:
            return
        try:
            self.creative = CreativeDesk(self.llm, app_data(self.cfg) / "kreativ",
                                         publish=getattr(self.ui, "creative_event", None),
                                         language=str(self.cfg.creative.language or "en"),
                                         default_ctx=int(self.cfg.llm.num_ctx or 8192))
        except Exception as exc:  # noqa: BLE001 - a többi menü enélkül is működjön
            self.ui.warn(f"A Kreatív menü nem indult el ({exc}).")
            self.creative = None

    def _on_runner_restart(self, detail: str) -> None:
        """Az Ollama modellfolyamata leállt; a kliens egyszer újrapróbálja (közben az Ollama újraindítja).
        Pár másodperc múlva az Ollama naplójából kiderül, miért (_explain_restart)."""
        self.ui.warn("A nyelvi modell (Ollama) futás közben leállt. Újraindítom, és folytatom ott, ahol "
                     "tartottam (20–40 másodperc)…")
        self._set_phase("working", "Újraindítom a nyelvi modellt")
        timer = threading.Timer(RESTART_LOG_DELAY, self._explain_restart, args=(detail,))
        timer.daemon = True
        timer.start()

    def _explain_restart(self, detail: str) -> None:
        """Megnézi az Ollama naplójában, miért állt le a modell, és megmondja, mit lehet tenni ellene: okonként
        egyszer részletesen (a javítással együtt), utána csak röviden."""
        if self._closing.is_set():
            return
        try:
            cause = find_cause(self.server_log.read_new())
            self.server_log.mark()
            kind = cause.kind if cause else "unknown"
            # Ha a gép memóriája fogyott el (vagy nem tudjuk, mi): mennyi a határ, ki foglalja, és tényleg él-e
            # a futó Ollamában a beállítás.
            report = memory.report() if kind in ("ram", "unknown") else None
            ollama = memory.ollama_process() if kind == "ram" else None
            self.model_restarts.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "error": detail[:300],
                                        "cause": cause.kind if cause else "", "log": cause.line if cause else "",
                                        "memory": report.public() if report is not None else None})
            del self.model_restarts[:-10]
            repeated = kind in self._restart_advice
            self._restart_advice.add(kind)
            if repeated and cause is None:
                return
            user = {CACHE_SETTING: user_setting(CACHE_SETTING), CHECKPOINT_SETTING: user_setting(CHECKPOINT_SETTING)}
            for level, text in explain(cause, self.server_log.path, user, report, ollama, repeated):
                (self.ui.info if level == "info" else self.ui.warn)(text)
            if kind == "vram" and not repeated:
                for line in gpu.memory_lines():
                    self.ui.warn("  " + line)
        except Exception:  # noqa: BLE001 - csak magyarázat: ha nem sikerül, a munka attól még megy tovább
            pass

    def _report_model_placement(self, status: dict[str, Any] | None) -> None:
        """Szól, ha a modell egy része a processzorra került, és megjegyzi a valódi kontextusméretet."""
        configured = int(self.cfg.llm.num_ctx or 0)
        fitted = self.llm.num_ctx
        context = int((status or {}).get("context_length") or fitted or 0)
        if context:
            self.context_length = context
        share = gpu_share(status)
        if share is not None and share < FULL_GPU:
            cpu = max(1, round((1 - share) * 100))
            shrunk = f" (pedig már {fitted} tokenre csökkentettem a kontextust)" \
                if fitted and fitted < configured else ""
            gpus = gpu.query_gpus()
            free_mb = sum(g.free_mb for g in gpus)
            placed = status or {}
            cpu_mb = max(0, int(placed.get("size") or 0) - int(placed.get("size_vram") or 0)) // 2**20
            # A legolcsóbb javítás: a kisebb KV-gyorsítótár kb. a felére veszi a kontextus memóriáját.
            kv_tip = "" if (user_setting(KV_SETTING) or "").strip().lower() in SMALL_KV_TYPES else (
                f"Először ezt próbáld: „setx {KV_SETTING} q8_0” PowerShellben (a kontextus memóriája kb. a "
                "felére csökken, alig érezhető minőségvesztéssel), majd lépj ki az Ollamából a tálcán, és indítsd "
                "újra, utána a Jarvist is. ")
            if gpus and cpu_mb and free_mb >= cpu_mb + 512:
                # Elférne, de az Ollama (llama-server) tartalékot hagy a kártyákon: nem más programok miatt van.
                self.ui.warn(f"A modell kb. {cpu}%-a a processzoron fut{shrunk}, ezért lassabban válaszolok, "
                             f"pedig a videókártyákon van még kb. {gpu.gb(free_mb)} GB szabad hely: az Ollama "
                             f"tartalékot hagy rajtuk. {kv_tip}Így is működik; ha gyorsítanál rajta, lásd a README "
                             "Hibaelhárítás részét (LLAMA_ARG_FIT_TARGET).")
            else:
                self.ui.warn(f"A modell kb. {cpu}%-a a processzoron fut{shrunk}, ezért lassabban válaszolok. "
                             f"{kv_tip}Valószínűleg más programok is foglalják a videókártyák memóriáját "
                             "(böngésző, játék, videó). Zárd be őket, és indítsd újra a Jarvist. Ellenőrzés: ollama ps")
            for line in gpu.memory_lines(gpus):
                self.ui.warn("  " + line)
        elif fitted and fitted < configured:
            self.ui.info(f"  Így már teljesen a videókártyán fut, {fitted} tokenes kontextussal. Ezt megjegyzem, "
                         f"legközelebb egyből így töltöm be (vagy írd a config.toml-ba: num_ctx = {fitted}).")
        if context and context < 8192 and self.cfg.browser.enabled:
            self.ui.warn(f"A kontextus csak {context} token, ez böngészéshez kevés: legalább 8192 kell "
                         "([llm] num_ctx a config.toml-ban).")

    def start_agent(self) -> None:
        self._agent = threading.Thread(target=self._agent_loop, name="jarvis-agent", daemon=True)
        self._agent.start()
        if self.work is not None:
            self._prep = threading.Thread(target=self._prep_loop, name="jarvis-eladas", daemon=True)
            self._prep.start()
        self._set_phase(self._rest_phase())
        if self.scheduler is not None:
            self.scheduler.start()
        if self.standby_mode and float(self.cfg.standby.idle_minutes or 0) > 0:
            threading.Thread(target=self._standby_loop, name="jarvis-standby", daemon=True).start()
        self._warm_up()

    def _rest_phase(self) -> str:
        """Az állapot, amikor épp nem dolgozik: készen áll, szünetel, vagy készenlétben vár."""
        if self.paused:
            return "standby" if self.standby else "paused"
        return "idle"

    def _standby_loop(self) -> None:
        """Készenléti indulásnál: ha egy ideje nem használod, visszaáll készenlétbe (a modellek leállnak)."""
        while not self._closing.wait(self.STANDBY_CHECK_SECONDS):
            idle = float(self.cfg.standby.idle_minutes or 0) * 60
            last = max(self.last_activity, self.last_work, self.last_prep)
            if (idle > 0 and not self.paused and not self.busy and not self.prepping
                    and self._prep_requests.empty() and time.time() - last >= idle):
                self._submit(("standby", None))

    def _warm_up(self, mode: str | None = None) -> None:
        """Előre feldolgoztatja egy menü beszélgetésének elejét (rendszerprompt, eszközök, előzmények), egy külön
        szálon, így az első kérdésnél már csak magát a kérdést kell. Ha közben kérdezel, az Ollama sorba veszi.

        `mode`: a most kezelt kérés menüje (alapból az elöl lévő menü). A kérés eleje szó szerint ugyanaz, mint
        az igazié (Assistant.warm_request). Ha már vár a sorban egy kérésed, nem melegít: az úgyis feldolgozza a
        saját elejét, a melegítés csak feltartaná."""
        if not self.cfg.llm.warm_up or not self.model_loaded or self._interactive_queued():
            return
        mode = self._mode(mode or self.mode)
        if mode == "chat" and self.assistant is None:
            return
        messages, schemas, options = self._assistant_for(mode).warm_request()
        client = OllamaClient(self.cfg.llm)       # saját kapcsolat, hogy ne akadjon össze a többivel
        client.num_ctx = self.llm.num_ctx
        client._think_supported = self.llm._think_supported

        def run() -> None:
            try:
                client.warm(messages, schemas or None, options)
            except Exception:  # noqa: BLE001 - csak gyorsítás; ha nem megy, nem baj
                pass

        threading.Thread(target=run, name="jarvis-warmup", daemon=True).start()

    def _interactive_queued(self) -> bool:
        """Vár-e a sorban a felhasználó egy kérése."""
        with self.requests.mutex:
            return any(item[0] in INTERACTIVE for item in self.requests.queue)

    def greet(self) -> None:
        self.ui.notice(self.cfg.assistant.greeting)

    def shutdown(self) -> None:
        self._closing.set()
        self.stop_event.set()
        if self.scheduler is not None:
            self.scheduler.stop()
        if self.work is not None and getattr(self.work, "instant", None) is not None:
            self.work.instant.stop()
        if self.work is not None:
            self.work.deals.stop_fast_lane()
        if self._prep is not None:
            self._prep_requests.put(None)
        if self._agent is not None and self._agent.is_alive():
            self.requests.put(("quit", None))
            self._agent.join(15)
        elif self.toolbox is not None:
            self.toolbox.close()
        if self.cfg.llm.unload_on_exit and self.model_loaded:
            try:
                self.llm.unload()
                self.ui.info("A nyelvi modell kiürítve a videókártyáról.")
            except OllamaError:
                pass

    # --- kérések (bármelyik szálról hívható) ------------------------------------------

    def _submit(self, item: tuple[str, Any]) -> None:
        if item[0] in INTERACTIVE:
            if item[0] != "work":       # az eladási lépés nem beszélgetés: utána hamar folytatódhat a kereskedés
                self.last_activity = time.time()
            self.preempt.set()          # ha épp háttérmunka fut, a következő lépésnél abbahagyja
        with self._outstanding_lock:
            self._outstanding += 1
        self.requests.put(item)

    def new_request_id(self) -> str:
        """Egy kérés azonosítója: a telefon ezzel várja meg és találja meg a választ."""
        request_id = secrets.token_hex(6)
        with self._done_lock:
            self._done_events[request_id] = threading.Event()
        return request_id

    def wait_request(self, request_id: str, timeout: float) -> bool:
        """Megvárja, hogy a Jarvis végezzen a kéréssel (True), vagy lejárjon az idő (False)."""
        with self._done_lock:
            event = self._done_events.get(request_id)
        if event is None:
            return True
        done = event.wait(max(0.0, float(timeout)))
        if done:
            with self._done_lock:
                self._done_events.pop(request_id, None)
        return done

    def _finish_request(self, request_id: str | None) -> None:
        if not request_id:
            return
        done = getattr(self.ui, "request_done", None)
        if callable(done):
            try:
                done(request_id)
            except Exception:  # noqa: BLE001
                pass
        with self._done_lock:
            event = self._done_events.get(request_id)
            if event is not None:
                event.set()
            if len(self._done_events) > 200:   # akik nem várták meg, azok jelzése se gyűljön a végtelenségig
                for key in [k for k, e in self._done_events.items() if e.is_set()][:100]:
                    self._done_events.pop(key, None)

    def submit_text(self, text: str, mode: str = "chat", request_id: str | None = None, via: str = "text") -> None:
        text = (text or "").strip()
        if text:
            self._submit(("text", {"text": text, "mode": self._mode(mode), "request_id": request_id, "via": via}))

    def submit_reset(self, mode: str = "chat") -> None:
        self._submit(("reset", self._mode(mode)))

    def submit_work(self, action: str, ids: list[str] | None = None, platform: str = "", **extra: Any) -> None:
        """Az Adás/Vétel gombjai (eladás, válasz, árdöntés, újra, bejelentkezés): az ágens-szálon futnak."""
        ids = [str(i) for i in ids or []]
        payload = {"action": action, "ids": ids, "platform": platform, **extra}
        if action in PREP_ACTIONS and self._prep is not None and self.work is not None and not self.paused:
            # a saját szálán, rögtön (a kereskedés közben is); a böngészős része utána elsőbbséggel az ágens-szálon
            if ids and (self.prepping or not self._prep_requests.empty()):
                self.work.store.set_busy(ids[0], "Sorra vár…")
            self._prep_requests.put(payload)
            return
        if self.work is not None and ids and action in BUSY_ACTIONS and self.busy:
            self.work.store.set_busy(ids[0], "Sorra vár…")   # amíg Jarvis mással dolgozik
        self._submit(("work", payload))

    def submit_creative(self, action: str, story_id: str) -> None:
        """A Kreatív menü modellmunkája (írás, másik változat, ötletből történet, összefoglalás): az
        ágens-szálon fut, sorban a többi kéréssel, mert a modell egyszerre egy dolgot csinál."""
        if self.creative is None:
            return
        self.creative.queue(story_id)
        self._submit(("creative", {"action": action, "id": story_id}))

    def submit_job(self, key: str) -> None:
        """Háttérmunka (az ütemező hívja, ha Jarvis ráér)."""
        self._submit(("job", {"key": key, "forced": False}))

    def run_job_now(self, key: str) -> None:
        """Háttérmunka azonnal (a telefonról vagy a felületről kérve), a csendes időszakot nem várja meg."""
        self._submit(("job", {"key": key, "forced": True}))

    def submit_pause(self) -> None:
        """Szünet: abbahagy mindent, és leállít minden modellt, hogy a videókártya, a memória és a
        processzor felszabaduljon (pl. játékhoz). Folytatni a submit_resume-mal lehet."""
        self.stop_current()
        self._drop_queued()
        self._submit(("pause", None))

    def submit_resume(self) -> None:
        self._submit(("resume", None))

    def _drop_queued(self) -> None:
        """A sorban várakozó kéréseket eldobja (a Szünet előtt): a már futót a stop_event állítja le."""
        while True:
            try:
                waiting = self._prep_requests.get_nowait()
            except queue.Empty:
                break
            if waiting is None:
                self._prep_requests.put(None)
                break
            for item_id in waiting.get("ids") or []:
                self._clear_busy(item_id)
        dropped = []
        while True:
            try:
                item = self.requests.get_nowait()
            except queue.Empty:
                break
            if item[0] == "quit":
                self.requests.put(item)
                break
            dropped.append(item)
            with self._outstanding_lock:
                self._outstanding -= 1
        for kind, payload in dropped:
            if kind == "job" and self.scheduler is not None:
                self.scheduler.dropped()
            if kind == "creative" and self.creative is not None:
                self.creative.finished(str((payload or {}).get("id") or ""))
            if kind == "work" and self.work is not None:
                for item_id in (payload or {}).get("ids") or []:
                    self._clear_busy(item_id)
            if isinstance(payload, dict):
                self._finish_request(payload.get("request_id"))

    def _clear_busy(self, item_id: str) -> None:
        if self.work is not None:
            try:
                self.work.store.set_busy(item_id, "")
            except ItemError:
                pass

    def set_mode(self, mode: str) -> None:
        """Melyik menü van elöl: a hangos kérések (Hey Jarvis) oda mennek. Ha a Munka menüre váltasz, és Jarvis
        épp nem dolgozik, előre feldolgoztatja a Munka menü elejét (a menük eleje más, az Ollama egyet tart meg)."""
        mode = self._mode(mode)
        changed, self.mode = mode != self.mode, mode
        if changed and mode == "work" and not self.busy and not self.paused:
            self._warm_up("work")

    def _mode(self, mode: str) -> str:
        return mode if mode == "work" and self.work_assistant is not None else "chat"

    def _assistant_for(self, mode: str) -> Assistant:
        if mode == "work" and self.work_assistant is not None:
            return self.work_assistant
        assert self.assistant is not None
        return self.assistant

    @contextlib.contextmanager
    def _channel(self, mode: str):
        """A felület tudja, melyik menü naplójába kerüljenek az üzenetek. Az eladás-előkészítő szálon csak a saját
        üzenetei mennek oda (a felület szálanként is tudja: own_channel), az ágens-szál beszélgetése marad, ahol volt."""
        own = getattr(type(self.ui), "own_channel", None)
        if threading.current_thread() is self._prep and callable(own):
            with own(self.ui, mode):
                yield
            return
        previous = getattr(self.ui, "channel", None)
        if isinstance(previous, str):
            self.ui.channel = mode
        try:
            yield
        finally:
            if isinstance(previous, str):
                self.ui.channel = previous

    def stop_current(self) -> None:
        """Állj: megszakítja a felvételt, a modellt, a következő lépést és a beszédet."""
        self.stop_event.set()
        self._prep_stop.set()

    def _check_stop(self) -> None:
        """A modell nélküli lépéssorok (visszajátszás) is megállnak az Állj gombra."""
        if self.stop_event.is_set():
            raise Cancelled()

    # --- ágens-szál ------------------------------------------------------------------

    def _agent_loop(self) -> None:
        try:
            while True:
                kind, payload = self.requests.get()
                if kind == "quit":
                    break
                if kind == "browser":     # egy gyors böngészőhívás (captcha a telefonon), a többi állapothoz nem nyúl
                    self._browser_call(payload)
                    continue
                self.stop_event.clear()
                self._current_kind = kind
                if kind in INTERACTIVE:
                    self.preempt.clear()      # a kérésed most sorra került
                request_id = payload.get("request_id") if isinstance(payload, dict) else None
                self._dossiers_made.clear()
                if hasattr(self.ui, "request_id"):
                    self.ui.request_id = request_id   # a felület ezzel jelöli meg a kérés üzeneteit
                # a Kreatív menü üzenetei (pl. ha az Ollama leáll) a Kreatív menüben jelenjenek meg
                channel = contextlib.ExitStack()
                if kind == "creative":
                    channel.enter_context(self._channel("creative"))
                if kind in INTERACTIVE:   # a kérésed modellhívásai előbb jönnek a kereskedésnél (llm.ModelGate)
                    channel.enter_context(self.llm.gate.session())
                try:
                    if self.paused and self.standby and kind in INTERACTIVE and not self._asks_pause(kind, payload):
                        self._wake_up(self._request_mode(kind, payload))   # készenlétből felébreszti (10–20 mp)
                    if self.paused and kind in INTERACTIVE and self._asks_pause(kind, payload):
                        self.ui.user(payload["text"], payload.get("via", "text"))
                        self._do_pause()           # készenlétből Szünet: felébredés nélkül
                    elif self.paused and kind in INTERACTIVE:
                        self.ui.notice(WAKE_FAILED if self.standby else PAUSED_REPLY)
                        if kind == "work":    # pl. az előkészítés után átadott feltöltés: a kártyán ne várjon tovább
                            for item_id in payload.get("ids") or []:
                                self._clear_busy(item_id)
                    elif self.paused and kind == "job" and not self._light_job(payload):
                        if self.scheduler is not None:
                            self.scheduler.dropped()
                    elif kind == "standby":
                        if not self.paused and self.requests.empty():
                            self._do_pause(standby=True)
                    elif kind == "text":
                        if isinstance(payload, tuple):   # régi alak: (szöveg, menü)
                            payload = {"text": payload[0], "mode": payload[1]}
                        if is_pause_request(payload["text"]):
                            self.ui.user(payload["text"], payload.get("via", "text"))
                            self._do_pause()
                        else:
                            self.respond(payload["text"], via=payload.get("via", "text"),
                                         mode=payload.get("mode", "chat"))
                    elif kind == "reset":
                        mode = payload or "chat"
                        self._assistant_for(mode).reset()
                        with self._channel(mode):
                            self.ui.info("Új beszélgetés.")
                    elif kind == "work":
                        self.run_work(payload)
                    elif kind == "creative":
                        self.run_creative(payload)
                    elif kind == "job":
                        self._run_job(payload)
                    elif kind == "pause":
                        self._do_pause()
                    elif kind == "resume":
                        self._do_resume()
                except Cancelled:
                    self.ui.info("Megszakítva.")
                except OllamaError as exc:
                    self.ui.error(model_problem(exc))
                except Exception as exc:  # noqa: BLE001 - egy hiba ne állítsa le a Jarvist
                    self.ui.error(f"Váratlan hiba: {type(exc).__name__}: {exc}")
                    traceback.print_exc()
                finally:
                    with self._outstanding_lock:
                        self._outstanding -= 1
                    self._current_kind = ""
                    if self.work is not None and kind not in ("job",):
                        self.work.end_request()
                    if kind == "creative" and self.creative is not None:
                        self.creative.finished(str(payload.get("id") or ""))
                    channel.close()
                    if kind == "work":
                        self.last_work = time.time()       # utána AFTER_WORK múlva folytatódhat a kereskedés
                    elif kind in INTERACTIVE:
                        self.last_activity = time.time()   # a csendes időszak a kérés végétől számít
                    self._set_phase(self._rest_phase())
                    if hasattr(self.ui, "request_id"):
                        self.ui.request_id = None
                    self._finish_request(request_id)
        finally:
            if self.toolbox is not None:
                self.toolbox.close()  # a böngészőt ugyanabban a szálban kell bezárni

    @staticmethod
    def _asks_pause(kind: str, payload: Any) -> bool:
        """Szünetet kér-e a beírt szöveg („pihenj”)."""
        return kind == "text" and isinstance(payload, dict) and is_pause_request(str(payload.get("text") or ""))

    def _light_job(self, payload: Any) -> bool:
        """Készenlétben is lefuthat-e (a modell és böngésző nélküli háttérmunkák: figyelők, régi hirdetések; és amelyik
        kéri, pl. a napközbeni böngészés: a böngésző ilyenkor újraindul, a modell csak röviden töltődik be)."""
        if not self.standby or self.scheduler is None or not isinstance(payload, dict):
            return False
        job = self.scheduler.jobs.get(str(payload.get("key") or ""))
        return job is not None and (not job.needs_models and not job.needs_browser or standby_ok(job))

    def _wake_up(self, mode: str | None = None) -> None:
        """Készenlétből: betölti a modelleket, és utána jöhet a kérés (`mode`: a kérés menüje, annak az elejét
        melegíti elő)."""
        self.ui.info("Felébredek: betöltöm a modelleket (10–20 másodperc)...")
        self._do_resume(announce=False, mode=mode)

    def _request_mode(self, kind: str, payload: Any) -> str:
        """Melyik menü beszélgetése válaszol a kérésre (a Kreatív menünek nincs ilyen)."""
        if kind in ("work", "creative"):
            return kind
        if kind == "text" and isinstance(payload, dict):
            return self._mode(str(payload.get("mode") or "chat"))
        return self.mode

    def _run_job(self, payload: dict[str, Any]) -> None:
        if self.scheduler is None:
            return
        key = str(payload.get("key") or "")
        job = self.scheduler.jobs.get(key)
        self._set_phase("working", f"Háttérben: {job.title if job else key}")
        result = self.scheduler.run(key, forced=bool(payload.get("forced")))
        if payload.get("forced"):
            self.ui.info(f"{job.title if job else key}: {result}")

    # --- Szünet és Folytatás ---------------------------------------------------------------

    def _do_pause(self, standby: bool = False) -> None:
        """A nyelvi modell leáll (az Ollamában minden betöltött), és a böngésző is bezárul. A Jarvis ablaka (és a
        telefonos kapcsolat) megmarad, így a Folytatás gombbal vissza lehet hozni.

        `standby`: készenlét (tétlenség után magától): ebből egy kérés felébreszti. A Szünet gombos
        szünetből nem ébred fel magától (pl. mert épp játszol a gépen)."""
        if self.paused:
            if not standby and self.standby:
                self.standby = False
                self._set_phase(self._rest_phase())
                self.ui.notice("Szünetre váltottam: amíg a Folytatás gombot meg nem nyomod, nem ébredek fel.")
                self._hello()
            else:
                self.ui.info("Már szünetelek.")
            return
        self._set_phase("working", "Leállítom a modelleket")
        before = {g.index: g.used_mb for g in gpu.query_gpus()}
        freed = []
        if self.toolbox is not None and self.toolbox.close_browser():
            freed.append("böngésző")
        try:
            unloaded = self.llm.unload_all()
            if unloaded:
                freed.append("nyelvi modell" + (f" ({', '.join(unloaded)})" if len(unloaded) > 1 else ""))
        except OllamaError as exc:
            self.ui.warn(f"A nyelvi modellt nem sikerült kiüríteni ({exc}).")
        self.model_loaded = False
        self.paused = True
        self.standby = standby
        # Készenlétben a háttérmunka (pl. a napközbeni böngészés) néha kérdezi a modellt: utána ne maradjon a
        # videókártyán (a beállított keep_alive helyett csak STANDBY_KEEP_ALIVE ideig).
        self.llm.keep_alive_override = self._standby_keep_alive if standby else None
        if self.work is not None:
            self.work.current = None
        gc.collect()
        self._set_phase(self._rest_phase())
        if standby:
            self.ui.info("Egy ideje nem használtál, készenlétbe álltam (leállítottam: " +
                         (", ".join(freed) or "semmi nem futott") + "). Ha írsz, felébredek.")
        else:
            self.ui.notice("Szünetelek. Leállítottam: " + (", ".join(freed) or "nem futott semmi") +
                           ". A Folytatás gombbal (vagy a telefonról) újraindulok.")
        after = gpu.query_gpus()
        if before and after:
            gained = sum(max(0, before.get(g.index, g.used_mb) - g.used_mb) for g in after)
            lines = gpu.memory_lines(after)
            self.ui.info(f"Felszabadult kb. {gpu.gb(gained)} GB videómemória. " + " | ".join(lines))
        self._hello()

    def _do_resume(self, announce: bool = True, mode: str | None = None) -> None:
        if not self.paused:
            self.ui.info("Nem szünetelek, minden fut.")
            return
        if announce:
            self.ui.info("Folytatom: betöltöm a modelleket...")
        self.llm.keep_alive_override = None      # ébren a beállítás szerint marad a videókártyán
        if not self.load_model():
            if self.standby:
                self.llm.keep_alive_override = self._standby_keep_alive
            self._set_phase(self._rest_phase())
            self.ui.error("Nem sikerült betölteni a nyelvi modellt, ezért továbbra is szünetelek.")
            return
        self._sync_context()
        self.paused = False
        self.standby = False
        self.last_activity = time.time()
        self._set_phase("idle")
        if mode != "creative":      # a Kreatív menü saját, eszköz nélküli kéréseket küld: ott nincs mit előre
            self._warm_up(mode)
        self._hello()
        if announce:
            self.ui.notice("Újra itt vagyok.")

    # --- jóváhagyás, értesítés ------------------------------------------------------------------

    def notify(self, message: str) -> None:
        """Időzítő vagy más háttéresemény üzenete (másik szálról is jöhet)."""
        self.ui.notice(message)

    def confirm(self, prompt: str) -> bool:
        self._set_phase("working", "Jóváhagyásra várok")
        context = self.work.confirm_context() if self.work is not None else ""
        if context:   # feltöltés közben: melyik oldal, melyik tárgy, mennyiért
            prompt = f"{context}\n\n{prompt}"
        approved = self.ui.confirm(prompt, self.stop_event)
        if approved and context and self.work.current is not None:
            self.work.approved = True
        return approved

    # --- válasz ------------------------------------------------------------------------------

    def answer(self, text: str, via: str = "text", mode: str = "chat", shown: str | None = None,
               tools: set[str] | None = None) -> None:
        """Megválaszolja a kérést a megfelelő menü beszélgetésében (Beszélgetés vagy Munka).

        `shown`: a naplóban ez jelenjen meg a kérés helyett (a Jarvis saját, belső kéréseinél).
        `tools`: ebben a kérésben csak ezek az eszközök (pl. egy feltöltésnél); alapból a menü összes eszköze.
        """
        with self._channel(mode):
            self._answer(self._assistant_for(mode), text, via, shown, tools)

    def _answer(self, assistant: Assistant, text: str, via: str, shown: str | None = None,
                tools: set[str] | None = None) -> None:
        """A válasz kiírása már menet közben indul (a modell válaszának minden darabja rögtön megjelenik)."""
        if shown is None:
            self.ui.user(text, via)
        else:
            self.ui.info(shown)
        self._set_phase("thinking")
        try:
            reply = assistant.ask(text, on_text=self.ui.delta if self.cfg.assistant.stream else None,
                                  tool_names=tools)
        except BaseException:
            self.ui.assistant_abort()
            raise
        self.ui.assistant_end(reply, assistant.last_reply_streamed)
        if self.cfg.assistant.show_timings:
            stats = assistant.last_stats
            speed = stats.tokens_per_second()
            parts = [f"válasz: {secs(stats.llm_seconds)}"]
            if stats.tool_calls:
                parts.append(f"{stats.tool_calls} eszközhívás")
            if speed:
                parts.append(f"~{speed:.0f} token/mp")
            self.ui.timing(", ".join(parts))

    # --- Munka menü ------------------------------------------------------------------------

    def respond(self, text: str, via: str = "text", mode: str = "chat") -> None:
        """A felhasználó szövege. Ha a Munka menüben Jarvis épp visszakérdezett,
        ez a válasz arra (és megy tovább az eladás); különben a menü beszélgetése válaszol."""
        if mode == "work" and self.work is not None and self.work_assistant is not None:
            pending = self.work.pending_item()
            if pending is not None:
                self.run_work({"action": "reply", "ids": [pending.id], "text": text, "via": via})
                return
        self.answer(text, via=via, mode=mode)

    def run_work(self, payload: dict[str, Any], browser: bool = True) -> None:
        """Az Adás/Vétel feladatai az ágens-szálon: eladás, válasz, árdöntés, újra, árkutatás, belépés.
        browser=False: az eladás-előkészítő szálon (PREP_ACTIONS): a böngészős részt (árkutatás egy böngészős oldalon,
        feltöltés) nem itt csinálja, hanem az ágens-szálra adja (_handoffs, _prep_loop)."""
        if self.work is None or self.work_assistant is None:
            self.ui.error("A Munka menü nincs bekapcsolva.")
            return
        action = str(payload.get("action") or "")
        ids = list(payload.get("ids") or [])
        item_id = ids[0] if ids else ""
        with self._channel("work"):
            try:
                if action == "sell":
                    self._sell(item_id, browser)
                elif action == "approve":      # Pro: átnézted az adatokat (Nézd át), mehet a feltöltés
                    self.work.approve(item_id)
                    self._sell(item_id, browser)
                elif action == "upload":       # az előkészítés kész (eladás-előkészítő szál): a feltöltés jön
                    if not self.work.store.get(item_id).pending:
                        self._post_all(item_id)
                elif action == "reply":
                    self._reply(item_id, str(payload.get("text") or ""), str(payload.get("via") or "text"), browser)
                elif action == "price":
                    choice = payload.get("choice")
                    if choice not in PRICE_CHOICES:
                        choice = parse_price(choice) or "keep"
                    self.work.decide_price(item_id, choice)
                    if not self._price_question(item_id):
                        self._sell(item_id, browser)
                elif action == "cancel":
                    self.work.store.set_pending(item_id, None)
                    self.ui.info("Rendben, ezt most hagyom.")
                elif action == "post":
                    key = str(payload.get("platform") or "")
                    ok = self._post_one(item_id, key)
                    site = self.work.platforms.get(key)
                    name = self.work.store.get(item_id).name
                    self._tell("post", f"Feltöltés: {name}",
                               f"{name}: " + (f"fent van ({site.name if site else key})." if ok else
                                              f"nem sikerült ({site.name if site else key}); a kártyán látod, miért."),
                               item=item_id)
                elif action == "research":
                    self._research(item_id)
                elif action == "login":
                    self._login(str(payload.get("platform") or ""))
                elif action == "delist":
                    platforms = [str(p) for p in payload.get("platforms") or [] if p]
                    if payload.get("platform"):
                        platforms.append(str(payload["platform"]))
                    self._delist(item_id, platforms or None, confirmed=True)
                elif action == "sold":
                    self._sold(item_id, str(payload.get("where") or ""), bool(payload.get("delist")))
                elif action == "message_draft":
                    self._message_draft(str(payload.get("conversation") or ""))
                elif action == "message_send":
                    self._message_send(str(payload.get("conversation") or ""), str(payload.get("text") or ""))
                elif action == "deal_offer":
                    self._deal_offer(str(payload.get("deal") or ""), payload.get("price"))
                else:
                    self.ui.error(f"Ismeretlen feladat: {action}")
            except ItemError as exc:
                self.ui.error(str(exc))
                self._tell("error", "Munka: hiba", str(exc), item=item_id)
            except OllamaError as exc:   # a Munka naplójába kerüljön, ne a Beszélgetésbe
                text = model_problem(exc)
                self.ui.error(text)
                self._tell("error", "Munka: hiba", text, item=item_id)
            finally:
                if item_id and action in BUSY_ACTIONS:
                    self.work.store.set_busy(item_id, "")

    def run_creative(self, payload: dict[str, Any]) -> None:
        """A Kreatív menü feladata az ágens-szálon (a felület a Kreatív menüben mutatja, mi történik)."""
        if self.creative is None:
            return
        self._set_phase("writing", "")
        try:
            self.creative.run(payload, self.stop_event, show=lambda text: self._set_phase("writing", text))
        except Cancelled:
            self.ui.info("Megállítottam.")
        except StoryError as exc:
            self.ui.error(str(exc))
        except OllamaError as exc:
            self.ui.error(model_problem(exc))

    def _progress(self, item_id: str) -> Callable[[str], None]:
        def show(detail: str) -> None:
            try:
                name = self.work.store.get(item_id).name
            except ItemError:
                name = ""
            self._set_phase("working", f"{name}: {detail}" if name else detail)
        return show

    def _sell(self, item_id: str, browser: bool = True) -> None:
        """Egy eladás: adatok a leírásból → visszakérdezés vagy árdöntés → feltöltés oldalanként. browser=False (az
        eladás-előkészítő szálon): ha a következő lépéshez böngésző kell (árkutatás egy böngészős oldalon, vagy már a
        feltöltés), azt az ágens-szál folytatja (_handoffs)."""
        assert self.work is not None
        if not self.work.browser_ready:
            raise ItemError("A feltöltéshez Jarvis saját böngészője kell (pip install playwright, "
                            "majd: playwright install chromium; [browser] enabled = true).")
        store = self.work.store
        store.set_busy(item_id, "Értelmezem a leírást…")
        try:
            step = self.work.prepare(item_id, self._progress(item_id) if browser else self._card_progress(item_id),
                                     browser=browser)
        finally:
            store.set_busy(item_id, "")
        if step.kind == "browser":     # a böngésző kell (árkutatás): az ágens-szálon folytatja, elsőbbséggel
            self._handoffs[item_id] = {"action": "sell", "ids": [item_id]}
            return
        if step.kind in ("ask", "price"):
            self.ui.notice(step.text)
            self._tell("question", f"Kérdés: {store.get(item_id).name}", step.text, item=item_id, open="item",
                       actions=[{"action": "item", "label": "Megnyitom"}])
            return
        if step.kind == "review":      # Pro: a fotókról kiolvasott adatok és az ár, átírhatóan; a Mehet után tölt fel
            name = store.get(item_id).name
            self.ui.notice(step.text)
            self._tell("question", f"Nézd át: {name}", step.text, item=item_id, open="review",
                       actions=[{"action": "review", "label": "Átnézem"}])
            return
        if step.text:
            self.ui.notice(step.text)
        if step.note:      # a fotókról: csak szól, a feltöltés megy tovább
            self.ui.notice(step.note)
        if not browser:
            self._handoffs[item_id] = {"action": "upload", "ids": [item_id]}
            return
        self._post_all(item_id)

    def _card_progress(self, item_id: str) -> Callable[[str], None]:
        """Az eladás-előkészítő szál haladása a tárgy kártyáján (az állapotsor az ágens-szálé: a kereskedésé)."""
        def show(detail: str) -> None:
            text = detail[:1].upper() + detail[1:]
            self.work.store.set_busy(item_id, text if text.endswith("…") else text + "…")
        return show

    def _prep_loop(self) -> None:
        """Az eladás-előkészítő szál: a PREP_ACTIONS kérések egymás után, a kereskedéssel párhuzamosan (a modellhívásai
        elsőbbségiek: llm.ModelGate). Amit utána böngésző kell, az ágens-szál sorába kerül (az a futó kereskedést
        megszakítja), a kártyán közben látszik, hogy a böngészőre vár."""
        assert self.work is not None
        while True:
            payload = self._prep_requests.get()
            if payload is None:
                return
            ids = list(payload.get("ids") or [])
            item_id = ids[0] if ids else ""
            if self.paused:               # közben Szünet vagy készenlét jött: az ágens-szál intézi (felébred, ha lehet)
                self._submit(("work", payload))
                continue
            self.prepping = item_id or "?"
            self._prep_stop.clear()
            try:
                with self.llm.gate.session(), self.llm.stopped_by(self._prep_stop):
                    self.run_work(payload, browser=False)
            except Cancelled:
                with self._channel("work"):
                    self.ui.info("Megszakítva.")
            except Exception as exc:  # noqa: BLE001 - egy hiba ne állítsa le az előkészítést
                with self._channel("work"):
                    self.ui.error(f"Váratlan hiba: {type(exc).__name__}: {exc}")
                traceback.print_exc()
            finally:
                self.prepping = ""
                self.last_prep = time.time()
            handoff = self._handoffs.pop(item_id, None)
            if handoff is not None and self.work is not None:
                self.work.store.set_busy(item_id, "Feltöltésre vár (a kereskedés átadja a böngészőt)…"
                                         if handoff["action"] == "upload" else "Árkutatásra vár (a böngészőben)…")
                self._submit(("work", handoff))

    def _reply(self, item_id: str, text: str, via: str, browser: bool = True) -> None:
        """Válasz Jarvis kérdésére: kiegészítő adat vagy árdöntés."""
        assert self.work is not None
        item = self.work.store.get(item_id)
        self.ui.user(text, via)
        if item.pending.get("kind") == "price":
            confirming = bool(item.pending.get("confirm"))
            choice = interpret_price_reply(text, confirming=confirming)
            if choice is None:
                suggested = item.pending.get("suggested")
                question = "Akkor mennyi legyen?" if confirming else "Nem értettem."
                question += " Maradjon a te árad"
                question += f" ({format_price(item.price)})" if confirming and item.price else ""
                question += f", vagy legyen a javasolt ({format_price(suggested)})?" if suggested else "?"
                if confirming:   # a „3 Ft-ra gondoltál?” kérdésre nemet mondott: újra az eredeti kérdés
                    rest = {k: v for k, v in item.pending.items() if k != "confirm"}
                    self.work.store.set_pending(item_id, {**rest, "question": question})
                self.ui.notice(question)
                return
            self.work.decide_price(item_id, choice)
            if self._price_question(item_id):
                return
        else:
            self.work.answer(item_id, text)
        self._sell(item_id, browser)

    def _price_question(self, item_id: str) -> bool:
        """Ha az árdöntés után még kérdés maradt (pl. „3 Ft-ra gondoltál?”), felteszi. Igaz, ha kérdezett."""
        assert self.work is not None
        item = self.work.store.get(item_id)
        if item.pending.get("kind") != "price":
            return False
        question = str(item.pending.get("question") or "")
        self.ui.notice(question)
        self._tell("question", f"Kérdés: {item.name}", question, item=item_id, open="item",
                   actions=[{"action": "item", "label": "Megnyitom"}])
        return True

    def _post_all(self, item_id: str) -> None:
        """Sorban feltölti minden kiválasztott oldalra, ahol még nincs fent."""
        assert self.work is not None
        targets = self.work.targets_left(item_id)
        if not targets:
            text = f"{self.work.store.get(item_id).name}: már fent van mindenhol, ahova kérted."
            self.ui.notice(text)
            return
        done, failed = [], []
        while targets:
            if self.stop_event.is_set():
                raise Cancelled()
            # Ahol épp nem kell kivárni a közzétételek közti szünetet, az jön előbb (különben a kiválasztás sorrendje).
            key = min(targets, key=self.work.gap_remaining)
            targets.remove(key)
            name = self.work.platforms[key].name
            (done if self._post_one(item_id, key) else failed).append(name)
        item = self.work.store.get(item_id)
        parts = []
        if done:
            parts.append(f"Fent van: {', '.join(done)}.")
        if failed:
            parts.append(f"Nem sikerült: {', '.join(failed)}. A kártyán látod, miért; az Újra gombbal "
                         "megpróbálom még egyszer.")
        if parts:
            summary = f"{item.name}: " + " ".join(parts)
            self.ui.notice(summary)
            self._tell("post", f"Feltöltés: {item.name}", summary, item=item_id)

    def _replay(self, item_id: str, key: str, task: str) -> bool | None:
        """A begyakorolt lépések visszajátszása modell nélkül.

        Visszaad: True (sikerült), False (nem sikerült, vagy nem engedélyezted), vagy None, ha a
        közzététel ELŐTT, még az űrlap kitöltése előtt elakadt (akkor a modell csinálja végig, és közben
        újratanulja). Ha az űrlapot már részben kitöltötte, a modell a nyitott oldalon folytatja."""
        assert self.work is not None and self.work_assistant is not None
        work = self.work
        item = work.store.get(item_id)
        site = work.platforms[key]
        verb = "Feltöltés" if task == "post" else "Levétel"
        self.ui.info(f"{verb} begyakorolt lépésekkel: {item.name} → {site.name}")
        work.store.set_busy(item_id, f"{verb} (begyakorolt): {site.name}…")
        work.current = (item_id, key)
        work.approved = False
        started = time.perf_counter()
        try:
            outcome = work.replay(item_id, key, task, progress=self._progress(item_id))
        except Cancelled:
            work.current = None
            work.delisting = None
            raise
        if outcome.status in ("no_plan", "miss"):
            work.current = None
            if outcome.status == "miss":
                self._dossier(item_id, key, task, outcome.reason, "begyakorolt lépések", url=outcome.url,
                              kitoltes=outcome.log)
            self.ui.info(f"A begyakorolt lépések most nem jók ({outcome.reason}), végiggondolom.")
            return None
        if outcome.status == "partial":
            # Félúton akadt el (pl. egy elem nincs meg, és a javítás sem sikerült): a fotók és a kész mezők
            # maradnak, a modell a nyitott űrlapon folytatja, nem kezdi elölről.
            self._dossier(item_id, key, task, outcome.reason, "begyakorolt lépések", url=outcome.url,
                          kitoltes=outcome.log)
            self.ui.info(f"A begyakorolt lépések nem vittek végig ({outcome.reason}); a hiányzót a modell pótolja.")
            try:
                page = self.toolbox.browser.snapshot()
            except BrowserError as exc:
                page = f"(Az oldalnézet nem készült el: {exc})"
            return self._model_post(item_id, key, work.continue_request(item, key, outcome, page),
                                    shown=f"Folytatás: {item.name} → {site.name}")
        if outcome.status == "declined":
            if task == "post":
                return work.finish_post(item_id, key, "Nem engedélyezted a közzétételt.")
            return work.finish_delist(item_id, key, "nem engedélyezted.")
        if outcome.status == "done":
            if task == "post":
                work.store.mark(item_id, key, POSTED, url=outcome.url if outcome.url.startswith("http") else "")
                ok = work.finish_post(item_id, key)
            else:
                work.store.mark(item_id, key, NOT_POSTED, note=DELISTED_NOTE)
                ok = work.finish_delist(item_id, key)
            if self.cfg.assistant.show_timings:
                self.ui.timing(f"{verb.lower()} modell nélkül: {secs(time.perf_counter() - started)}")
            return ok
        # Bizonytalan: a közzététel (törlés) megtörtént, de nem látszik egyértelműen az eredmény.
        # Újrakezdeni nem szabad (kétszer kerülne fel), ezért a modell megnézi az oldalt.
        self._dossier(item_id, key, task, outcome.reason, "begyakorolt lépések (bizonytalan)", url=outcome.url,
                      kitoltes=outcome.log)
        return self._check_after(item_id, key, task, outcome)

    def _check_after(self, item_id: str, key: str, task: str, outcome: Any,
                     how: str = "begyakorolt lépésekkel") -> bool:
        """Ha a modell nélküli feltöltés vagy levétel után nem egyértelmű az eredmény, a modell megnézi az oldalt."""
        assert self.work is not None and self.work_assistant is not None
        work = self.work
        item = work.store.get(item_id)
        site = work.platforms[key]
        self.work_assistant.reset()
        last = ""
        cancelled = False
        work.checking = (item_id, key)   # közzététel után: újra feltölteni nem szabad (kétszer kerülne fel)
        try:
            self.answer(work.verify_request(item, key, task, outcome, how), via="auto", mode="work",
                        shown=f"Ellenőrzés: {item.name} → {site.name}", tools=work.task_tools)
            last = self._last_work_reply()
        except Cancelled:
            cancelled = True
            raise
        except OllamaError as exc:
            last = model_problem(exc, short=True)
            raise
        finally:
            work.checking = None
            ok = work.finish_post(item_id, key, last) if task == "post" else work.finish_delist(item_id, key, last)
            if not ok and not cancelled:
                self._dossier(item_id, key, task, last or "az ellenőrzés nem sikerült", "modell (ellenőrzés)")
        return ok

    def _post_one(self, item_id: str, key: str) -> bool:
        """Egy oldalra tölti fel: a Vintedre a Jarvis maga tölti ki (vinted.py); ha már begyakorolta, modell
        nélkül; különben a Munka menü beszélgetésével (a böngészőben, a végén jóváhagyással), és közben
        megtanulja."""
        assert self.work is not None and self.work_assistant is not None
        if key not in self.work.platforms:
            raise ItemError(f"Ismeretlen hirdetési oldal: {key}")
        # A közzétételek közti szünetet nem itt várja ki, hanem a közzététel gombja előtt (WorkDesk._before_publish):
        # így az űrlap a szünet alatt töltődik ki.
        self.work.begin_trail()
        if self.work.can_autofill(item_id, key):
            result = self._autofill(item_id, key)
            if result is not None:
                return result
        if self.work.can_replay(item_id, key, "post"):
            result = self._replay(item_id, key, "post")
            if result is not None:
                return result
        item = self.work.store.get(item_id)
        # Az első lépést (item_post: az űrlap megnyitása) a Jarvis maga végzi el, a modell már azzal kezd.
        return self._model_post(item_id, key, lambda: self.work.post_request(item, key),
                                shown=f"Feltöltés: {item.name} → {self.work.platforms[key].name}")

    def _model_post(self, item_id: str, key: str, request: str | Callable[[], str], shown: str,
                    guard: bool = False) -> bool:
        """A feltöltést (vagy a félig kitöltött űrlap befejezését) a modell csinálja a Munka menü beszélgetésében.
        `request`: a modell első üzenete, vagy ami elkészíti (ha ez ToolError-t dob, pl. mert hiányzik a cím, az
        úgy jelenik meg, mintha a modell mondta volna). `guard`: a közzététel már megtörtént (jóváhagytad), ezért
        újra feltölteni (item_post) nem szabad."""
        assert self.work is not None and self.work_assistant is not None
        self.work.store.set_busy(item_id, f"Feltöltés: {self.work.platforms[key].name}…")
        # Minden feltöltés tiszta lappal indul: rövidebb kérés, gyorsabb válasz.
        self.work_assistant.reset()
        last = ""
        cancelled = False
        failure: OllamaError | None = None
        if guard:
            self.work.checking = (item_id, key)
        try:
            try:
                self.answer(request() if callable(request) else request, via="auto", mode="work", shown=shown,
                            tools=self.work.task_tools)
                last = self._last_work_reply()
            except ToolError as exc:
                last = self._tool_refusal(str(exc), shown)
            except OllamaError as exc:
                last, failure = model_problem(exc, short=True), exc
            if self.work.unmarked_publish(item_id, key) and not guard:   # (a guard-os kérés maga is ellenőrzés)
                last = self._check_publish(item_id, key) or last
        except Cancelled:
            last = "Megszakítottad. Az Újra gombbal folytatom."
            cancelled = True
            raise
        finally:
            self.work.checking = None
            ok = self.work.finish_post(item_id, key, last)
            if not ok and not cancelled:
                self._dossier(item_id, key, "post", last or "nem sikerült végigcsinálni", "modell")
        if failure is not None and not ok:
            raise failure
        return ok

    def _autofill(self, item_id: str, key: str) -> bool | None:
        """A Vinted űrlapjának kitöltése modell nélkül (a modell csak a kategóriát választja ki, lásd vinted.py).

        Visszaad: True (fent van), False (nem sikerült, nem engedélyezted, vagy be kell jelentkezni), vagy None,
        ha az űrlapot fel sem ismerte (akkor a begyakorolt lépések vagy a modell csinálja elölről). Ha közben
        elakad, vagy a közzététel után nem egyértelmű az eredmény, a modell a nyitott űrlapon folytatja."""
        assert self.work is not None and self.work_assistant is not None
        work = self.work
        item = work.store.get(item_id)
        site = work.platforms[key]
        self.ui.info(f"Feltöltés modell nélkül: {item.name} → {site.name}")
        work.store.set_busy(item_id, f"Feltöltés (modell nélkül): {site.name}…")
        work.current = (item_id, key)
        work.approved = False
        started = time.perf_counter()
        try:
            outcome = work.autofill(item_id, key, progress=self._progress(item_id))
        except Cancelled:
            work.current = None
            raise
        mode = "Vinted-kitöltő"
        if outcome.status == "miss":
            work.current = None
            self._dossier(item_id, key, "post", outcome.reason, mode, url=outcome.url, kitoltes=outcome.log)
            self.ui.info(f"A modell nélküli kitöltés most nem megy ({outcome.reason}), végiggondolom.")
            return None
        if outcome.status == "declined":
            return work.finish_post(item_id, key, "Nem engedélyezted a közzétételt.")
        if outcome.status == "blocked":
            self.ui.notice(outcome.reason)
            self._captcha_notice(outcome.reason)
            return work.finish_post(item_id, key, outcome.reason)
        if outcome.status == "done":
            work.store.mark(item_id, key, POSTED, url=outcome.url if outcome.url.startswith("http") else "")
            ok = work.finish_post(item_id, key)
            if ok:
                work.remember_choices(outcome)
            if self.cfg.assistant.show_timings:
                self.ui.timing(f"feltöltés modell nélkül: {secs(time.perf_counter() - started)}")
            return ok
        # Elakadt (partial), vagy a közzététel után nem egyértelmű (uncertain): a modell a nyitott űrlapon folytatja.
        self._dossier(item_id, key, "post", outcome.reason, mode, url=outcome.url, kitoltes=outcome.log)
        if outcome.published:
            self.ui.info(f"A közzététel után nem egyértelmű, sikerült-e ({outcome.reason}); megnézem.")
        else:
            self.ui.info(f"A kitöltés nem ment végig ({outcome.reason}); a hiányzót a modell pótolja.")
        try:
            page = self.toolbox.browser.snapshot()
        except BrowserError as exc:
            page = f"(Az oldalnézet nem készült el: {exc})"
        verb = "Ellenőrzés" if outcome.published else "Folytatás"
        ok = self._model_post(item_id, key, work.continue_request(item, key, outcome, page),
                              shown=f"{verb}: {item.name} → {site.name}", guard=outcome.published)
        if ok:
            work.remember_choices(outcome)
        return ok

    def _tool_refusal(self, text: str, shown: str) -> str:
        """Ha a Jarvis által elvégzett első lépés (item_post, item_delist) nem megy (pl. hiányzik a cím), az úgy
        jelenik meg, mintha a modell mondta volna: a naplóban, felolvasva, és a kártyán ez lesz a megjegyzés."""
        self.ui.info(shown)
        self.ui.assistant_end(text, False)
        return text

    def _last_work_reply(self) -> str:
        """A Munka menü beszélgetésében a modell utolsó szöveges válasza."""
        history = self.work_assistant.history if self.work_assistant is not None else []
        return next((m.get("content", "") for m in reversed(history) if m.get("role") == "assistant"), "")

    def _check_publish(self, item_id: str, key: str) -> str:
        """A közzétételt jóváhagytad, de a tárgy nincs megjelölve (pl. a modell válasza félbemaradt, vagy
        elfelejtette): a modell még egyszer megnézi az oldalt, és ha fent van, megjelöli. Újra feltölteni nem
        szabad, mert kétszer kerülne fel. Visszaadja az ellenőrzés válaszát (üres, ha az sem sikerült)."""
        assert self.work is not None
        work = self.work
        item = work.store.get(item_id)
        site = work.platforms[key].name
        work.checking = (item_id, key)
        try:
            # Ugyanabban a beszélgetésben: a modell tudja, mit csinált, és a hibacsomagba is minden belekerül.
            self.answer(work.check_request(item, key), via="auto", mode="work",
                        shown=f"Ellenőrzés: {item.name} → {site}", tools=work.task_tools)
        except OllamaError as exc:
            self.ui.warn(f"Az ellenőrzés nem sikerült: {model_problem(exc, short=True)}")
            return ""
        finally:
            work.checking = None
        return self._last_work_reply()

    def _respect_gap(self, item_id: str, key: str) -> None:
        """Emberi tempó: ugyanazon az oldalon két közzététel (vagy levétel) között pár perc teljen el
        ([work] publish_gap_minutes). Addig vár (az Állj gomb megszakítja). A WorkDesk hívja a közzététel (törlés)
        gombja előtt, amikor az űrlap már ki van töltve."""
        assert self.work is not None
        remaining = self.work.gap_remaining(key)
        if remaining <= 0:
            return
        site = self.work.platforms[key].name
        self.ui.info(f"{site}: nemrég tettem közzé valamit, ezért {mmss(remaining)} múlva folytatom, hogy "
                     "ne legyen gyanúsan gyors.")
        self.work.wait_gap(item_id, key, show=lambda text: self._set_phase("working", text))

    def _dossier(self, item_id: str, key: str, task: str, reason: str, mode: str, **extra: Any) -> str:
        """Hibacsomag a javításhoz (dossier.py), és a hirdetés sorában a link hozzá.
        Ha a közzétételt te nem engedélyezted, az nem hiba: akkor nem készül."""
        work = self.work
        if work is None or (item_id, key, task, mode) in self._dossiers_made:
            return ""
        if any(event.get("do") == "declined" for event in work.trail):
            return ""
        try:
            item = work.store.get(item_id)
            browser = self.toolbox.browser if self.toolbox is not None and work.browser_ready else None
            history = list(self.work_assistant.history) if mode.startswith("modell") and self.work_assistant else []
            if self.model_restarts:
                extra.setdefault("model_restarts", self.model_restarts[-5:])   # a nyelvi modell leállásai
            name = work.dossiers.create(task=task, platform=key, item=item, reason=reason, mode=mode,
                                        browser=browser, steps=list(work.trail), history=history, extra=extra)
            work.store.attach_dossier(item_id, key, name)
        except Exception as exc:  # noqa: BLE001 - a csomag hiánya ne rontsa el a feltöltést
            self.ui.warn(f"A hibacsomag nem készült el ({type(exc).__name__}: {exc}).")
            return ""
        self._dossiers_made.add((item_id, key, task, mode))
        self.ui.info(f"Hibacsomag a javításhoz: {work.dossiers.root / name}")
        return name

    def _sold(self, item_id: str, where: str = "", delist: bool = False) -> None:
        """Eladva: megjelöli, és ha kérted, leveszi a többi oldalról (ahol nem ott kelt el)."""
        assert self.work is not None
        store = self.work.store
        item = store.mark(item_id, sold=True)
        if where in self.work.platforms and item.platforms.get(where, {}).get("status") == POSTED:
            item = store.mark(item_id, where, POSTED, note="Itt kelt el.")
        others = [k for k in self.work.posted_on(item) if k != where]
        if not others:
            text = f"{item.name}: megjelöltem, hogy elkelt."
            self.ui.notice(text)
            return
        if delist:
            self._delist(item_id, others, confirmed=True)
            return
        names = ", ".join(self.work.platforms[k].name for k in others)
        text = f"{item.name}: megjelöltem, hogy elkelt. Még fent van itt: {names}."
        self.ui.notice(text)

    def _delist(self, item_id: str, keys: list[str] | None = None, confirmed: bool = False) -> None:
        """Leveszi a hirdetést a megadott oldalakról (alapból mindenhonnan, ahol fent van)."""
        assert self.work is not None
        item = self.work.store.get(item_id)
        posted = self.work.posted_on(item)
        keys = [k for k in dict.fromkeys(keys or posted) if k in posted]
        if not keys:
            text = f"{item.name}: a nyilvántartásom szerint sehol nincs fent, nincs mit levenni."
            self.ui.notice(text)
            return
        names = ", ".join(self.work.platforms[k].name for k in keys)
        if not confirmed and not self.confirm(f"Jarvis levenné a(z) „{item.name}” hirdetést innen: {names}."):
            self.ui.notice("Rendben, nem veszem le.")
            return
        done, failed = [], []
        for key in keys:
            if self.stop_event.is_set():
                raise Cancelled()
            (done if self._delist_one(item_id, key) else failed).append(self.work.platforms[key].name)
        parts = []
        if done:
            parts.append(f"Levettem: {', '.join(done)}.")
        if failed:
            parts.append(f"Nem sikerült: {', '.join(failed)}; a hirdetés lapján látod, miért.")
        text = f"{item.name}: " + " ".join(parts)
        self.ui.notice(text)
        if self.notices is not None:
            self.notices.add("delist", f"Levétel: {item.name}", text, item=item_id)

    def _delist_one(self, item_id: str, key: str) -> bool:
        """Egy oldalról levétel: begyakorolt lépésekkel, ha lehet, különben a modell csinálja (és tanul)."""
        assert self.work is not None and self.work_assistant is not None
        work = self.work
        work.begin_trail()                # (a szünetet a törlés gombja előtt várja ki)
        work.begin_delist(item_id, key)   # a levételt már jóváhagytad: a törlés gombjaira nem kérdez újra
        site = work.platforms[key]
        if work.can_autodelist(item_id, key):
            result = self._autodelist(item_id, key)
            if result is not None:
                return result
            work.begin_delist(item_id, key)
        item = work.store.get(item_id)        # a levevő közben megtalálhatta a hirdetés webcímét
        if work.can_replay(item_id, key, "delist"):
            result = self._replay(item_id, key, "delist")
            if result is not None:
                return result
            work.begin_delist(item_id, key)
        last = ""
        cancelled = False
        shown = f"Levétel: {item.name} → {site.name}"
        try:
            work.store.set_busy(item_id, f"Levétel: {site.name}…")
            self.work_assistant.reset()
            # Az első lépést (item_delist: a hirdetés megnyitása) a Jarvis maga végzi el, a modell már azzal kezd.
            self.answer(work.delist_request(item, key), via="auto", mode="work", shown=shown, tools=work.task_tools)
            last = self._last_work_reply()
        except ToolError as exc:
            last = self._tool_refusal(str(exc), shown)
        except Cancelled:
            last = "Megszakítottad."
            cancelled = True
            raise
        except OllamaError as exc:
            last = model_problem(exc, short=True)
            raise
        finally:
            ok = work.finish_delist(item_id, key, last)
            if not ok and not cancelled:
                self._dossier(item_id, key, "delist", last or "nem sikerült végigcsinálni", "modell")
        return ok

    def _autodelist(self, item_id: str, key: str) -> bool | None:
        """A Vinted-levétel modell nélkül (vinted.py): a hirdetés oldalán a Törlés és a megerősítés, emberi tempóban.

        Visszaad: True (levéve, vagy már nem volt fent), False (nem sikerült, nem engedélyezted, vagy be kell
        jelentkezni), vagy None, ha nem találta meg a hirdetést vagy a Törlés gombját: akkor a begyakorolt lépések
        vagy a modell csinálja (a megtalált webcímmel). Ha a törlés után nem egyértelmű, a modell megnézi."""
        assert self.work is not None and self.work_assistant is not None
        work = self.work
        item = work.store.get(item_id)
        site = work.platforms[key]
        self.ui.info(f"Levétel modell nélkül: {item.name} → {site.name}")
        work.store.set_busy(item_id, f"Levétel (modell nélkül): {site.name}…")
        work.current = (item_id, key)
        started = time.perf_counter()
        try:
            outcome = work.autodelist(item_id, key, progress=self._progress(item_id))
        except Cancelled:
            work.current = None
            work.delisting = None
            raise
        mode = "Vinted-levétel"
        if outcome.status in ("done", "gone"):
            note = DELISTED_NOTE if outcome.status == "done" else f"{DELISTED_NOTE} (már nem volt fent)"
            work.store.mark(item_id, key, NOT_POSTED, note=note)
            ok = work.finish_delist(item_id, key)
            if self.cfg.assistant.show_timings:
                self.ui.timing(f"levétel modell nélkül: {secs(time.perf_counter() - started)}")
            return ok
        if outcome.url:          # megtalálta a hirdetést: a következő lépés (vagy a modell) egyből oda megy
            work.store.mark(item_id, key, POSTED, url=outcome.url)
        if outcome.status == "declined":
            return work.finish_delist(item_id, key, "nem engedélyezted.")
        if outcome.status == "blocked":
            self.ui.notice(outcome.reason)
            self._captcha_notice(outcome.reason)
            return work.finish_delist(item_id, key, outcome.reason)
        if outcome.status == "miss":
            work.current = None
            self._dossier(item_id, key, "delist", outcome.reason, mode, url=outcome.url, kitoltes=outcome.log)
            self.ui.info(f"A modell nélküli levétel most nem megy ({outcome.reason}), végiggondolom.")
            return None
        self._dossier(item_id, key, "delist", outcome.reason, f"{mode} (bizonytalan)", url=outcome.url,
                      kitoltes=outcome.log)
        return self._check_after(item_id, key, "delist", outcome, how="modell nélkül")

    def _message_draft(self, conv_id: str) -> None:
        """Válaszvázlat egy vevő üzenetére (a felület Válaszvázlat gombja). Csak vázlat: elküldeni a Küldés gombbal lehet."""
        assert self.work is not None
        self._set_phase("working", "Válaszvázlatot írok")
        conv = self.work.messages.draft(conv_id)
        text = f"Válaszvázlat ({conv['partner'] or 'vevő'}): „{conv['draft']}”"
        if conv["draft_note"]:
            text += " " + conv["draft_note"]
        self.ui.notice(text + " Az Üzenetek résznél átírhatod, és a Küldés gombbal küldheted el.")

    def _message_send(self, conv_id: str, text: str) -> None:
        """A felület Küldés gombja: beírja a választ a beszélgetésbe, és a küldés gombja előtt jóváhagyást kér (a
        kérdésben pontosan az a szöveg áll, ami elmegy)."""
        assert self.work is not None
        messages = self.work.messages
        partner = messages.store.get(conv_id)["partner"] or "vevő"
        result = messages.send(conv_id, text, progress=lambda detail: self._set_phase("working", f"Üzenet: {detail}"))
        self.ui.notice(result)
        self._tell("message", f"Üzenet: {partner}", result, conversation=conv_id)

    def _deal_offer(self, deal_id: str, price: Any) -> None:
        """Automata kereskedés: ajánlattétel a Vinteden (a felület vagy az értesítés gombja). A küldés előtt
        jóváhagyást kér, pontosan az összeggel; fizetni nem fizet."""
        assert self.work is not None
        result = self.work.deals.make_offer(deal_id, price, confirm=self.confirm,
                                            progress=lambda detail: self._set_phase("working", f"Ajánlat: {detail}"))
        self.ui.notice(result)
        self._tell("deal", "Ajánlat a Vinteden", result)

    def _research(self, item_id: str) -> None:
        """Árkutatás külön kérésre (csak ajánlás, nem állítja meg az eladást)."""
        assert self.work is not None
        store = self.work.store
        store.set_busy(item_id, "Árkutatás…")
        try:
            item = self.work.research(item_id, self._progress(item_id))
        finally:
            store.set_busy(item_id, "")
        text = price_verdict(item).text
        self.ui.notice(text)

    def _login(self, platform: str) -> None:
        """Megnyitja az oldalt Jarvis böngészőjében, hogy egyszer bejelentkezhess."""
        assert self.work is not None
        site = self.work.platforms.get(platform)
        if site is None:
            raise ItemError(f"Ismeretlen hirdetési oldal: {platform}")
        if not self.work.browser_ready:
            raise ItemError("Jarvis saját böngészője nincs bekapcsolva.")
        try:
            self.toolbox.browser.open(site.home)
        except BrowserError as exc:
            raise ItemError(str(exc)) from None
        text = (f"Megnyitottam a(z) {site.name} oldalt a böngészőmben. Jelentkezz be, és pipáld be, hogy "
                "maradj bejelentkezve; utána a bejelentkezés megmarad.")
        self.ui.notice(text)

    def _shown_args(self, args: dict[str, Any]) -> dict[str, Any]:
        """A felületen a tárgy neve jelenjen meg, ne az azonosítója."""
        if self.work is None or not isinstance(args, dict) or not args.get("item"):
            return args
        try:
            return {**args, "item": self.work.store.find(str(args["item"])).name}
        except ItemError:
            return args

    # --- eszközök és állapot ------------------------------------------------------------

    def _on_tool_start(self, name: str, args: dict[str, Any]) -> None:
        self._current_tool_id = next(self._tool_ids)
        self.ui.tool_start(self._current_tool_id, name, self._shown_args(args))
        self._set_phase("working", TOOL_ACTIVITY.get(name, "Dolgozom"))

    def _on_tool_done(self, name: str, args: dict[str, Any], result: str) -> None:
        self.ui.tool_done(self._current_tool_id, name, self._shown_args(args), result)
        self._set_phase("thinking")

    def _set_phase(self, phase: str, detail: str = "") -> None:
        with self._state_lock:
            self._phase, self._detail = phase, detail
        self._refresh_state()

    def _refresh_state(self) -> None:
        with self._state_lock:
            state = (self._phase, self._detail)
            if state == self._shown_state:
                return
            self._shown_state = state
        self.ui.state(*state)
