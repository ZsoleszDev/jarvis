"""Villámfigyelő (Vinted): a megadott keresőszavakra (vagy Vinted-keresések linkjére) szinte folyamatosan frissíti a
Vinted legújabb hirdetéseit, és amint új hirdetés jön, ami illik (alapból: a beállított ár alatt), rögtön szól, a
többitől eltérő, saját hangjával (a telefonon a „Villámfigyelő” értesítési csatorna; rábökve a hirdetés nyílik meg, a
Vinted alkalmazásában vagy a böngészőben). Ugyanazt csinálja, mint aki délután a telefonján frissítget, hogy elsőként
lássa az olcsón feltett PS4-et.

Mikor: naponta a beállított időszakban (alapból DEFAULT_START–DEFAULT_END), azon belül szakaszosan: „active” percig
figyel, aztán „pause” perc szünet (alapból 20 és 10; a 0 perc szünet = végig figyel). A Most is gomb BURST_MINUTES
percre az időszakon kívül is elindítja (pl. kipróbálni). Figyelés közben „interval” másodpercenként (±25%, JITTER)
tölt be egy találati oldalt, a legújabbakkal elöl, a keresőszavakat felváltva: három keresőszónál mindegyik
3 × interval másodpercenként kerül sorra.

Hogyan: saját szálon, saját böngészőablakban és profilban (VintedReader, browser_profile_villam, soha be nem
jelentkezve): nem vár a Jarvis többi munkájára (feltöltés, vevői üzenetek, a kereskedés körei), és azok sem rá. A
modellt nem kérdezi, az árat nem kutatja (a lényeg a gyorsaság): a találati oldal kártyáiból dönt (browser.LISTINGS_JS). Egy
keresőszó első betöltése (és ha rég nézte, REBASE_AFTER: pl. másnap az időszak elején) csak megjegyzi, ami már fent
van; utána minden új hirdetésről szól, ami illik (fits): az ára legfeljebb a megadott (ha az árhatár be van
kapcsolva), legalább a megadott alsó határ (pl. a PS4-es játékok és tartozékok kiszűrésére), nincs benne kizáró szó, és
a címe szerint tényleg a keresett dolog (deals.matches_query: a tok, a töltő, a „PS4-hez” kontroller nem; ez
kikapcsolható). Ha egy már látott hirdetést leáraznak, és így illik, arról is szól. Ugyanarról a hirdetésről (két
keresőszó találata) csak egyszer.

Robotvédelem: ha a Vinted robot-ellenőrzést (captchát) kér, nem kerüli meg: egyre tovább vár (CAPTCHA_WAIT: 15, 30,
60 perc), a nap hátralévő részében lassabban frissít (SLOW_STEP-szer, legfeljebb SLOW_MAX-szor), és naponta egyszer
szól. A captchát a villámfigyelő ablakában te megoldhatod; a Folytatás most gomb nem vár tovább. Csak olvas: semmit nem
vesz meg, senkinek nem ír.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
import secrets
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from .browser import LISTINGS_JS, _short, is_closed_error, launch_options, page_challenged
from .deals import LIKES_HOT, DealError, buy_fee, excluded, matches_query, parse_words
from .selling import ItemError, format_price, listing_key, parse_price, reserved, vinted_titles
from .textutil import fold
from .todos import parse_time

CATALOG = "https://www.vinted.hu/catalog"
HOME = "https://www.vinted.hu/"
FILE = "villam.json"
PROFILE = "browser_profile_villam"
INTERVAL_CHOICES = (10, 15, 20, 30, 45, 60)    # két betöltés között (másodperc)
DEFAULT_INTERVAL = 15
MIN_INTERVAL, MAX_INTERVAL = 10, 300
ACTIVE_CHOICES = (5, 10, 15, 20, 30, 45, 60)    # percben
PAUSE_CHOICES = (0, 5, 10, 15, 20, 30)
DEFAULT_ACTIVE, DEFAULT_PAUSE = 20, 10
DEFAULT_START, DEFAULT_END = "16:30", "19:30"
BURST_MINUTES = 20
MAX_QUERIES = 8
MAX_SEEN = 400             # keresőszavanként ennyi látott hirdetést jegyez meg
MAX_HITS = 40
JITTER = (0.75, 1.25)
# Ha egy keresőszót ennél régebben nézett (de legalább a szünet + 15 perc), az első betöltés csak megjegyzi, ami fent
# van: másnap az időszak elején nem jön egyszerre egy egész oldalnyi „új” hirdetés. A szakaszok közti szünetben
# feltett hirdetésekről a következő szakasz elején szól (azok még frissek).
REBASE_AFTER = 45 * 60
CAPTCHA_WAIT = (15 * 60, 30 * 60, 60 * 60)
SLOW_STEP, SLOW_MAX = 1.5, 4.0
FAIL_WAIT = 30             # egy sikertelen betöltés után ennyit vár; FAILS_LONG egymás utáni hiba után FAIL_LONG_WAIT
FAILS_LONG, FAIL_LONG_WAIT = 4, 300
EMPTY_WARN = 3             # ennyi egymás utáni üres oldal után jelzi, hogy nem talál hirdetést (átalakították?)
IDLE_TICK = 30.0           # amíg nem figyel, legfeljebb ennyi másodpercenként néz rá az órára
HELD_CLOSE = 120           # a Jarvis szünetében ennyi másodperc után bezárja az ablakát
SAVE_EVERY = 30            # a látott hirdetéseket legfeljebb ennyi másodpercenként írja ki (találatnál rögtön)
NOTIFY_MAX = 5             # egy betöltés új találataiból ennyiről szól külön (a többiről egy összegzés)
LOAD_TIMEOUT = 25000       # ms
ITEMS_WAIT = 6000          # ms: ennyit vár a kártyákra a betöltés után
ITEM_LINK = 'a[href*="/items/"]'
_ITEM_ID = re.compile(r"/items/(\d+)")
_ORDER_DROP = {"order", "page", "time", "search_id"}
# a kártyák megjelenése (vagy a lap teljes betöltése, ha nincs találat)
_READY_JS = ("() => !!document.querySelector('" + ITEM_LINK.replace("'", "\\'") + "') || "
             "document.readyState === 'complete'")


class SniperError(DealError):
    """Érthető hibaüzenet (a felület 400-zal adja vissza)."""


def item_id(url: str) -> int:
    found = _ITEM_ID.search(urllib.parse.urlsplit(url or "").path)
    return int(found.group(1)) if found else 0


def _day_window(day: dt.date, start: int, end: int) -> tuple[float, float]:
    base = dt.datetime.combine(day, dt.time())
    length = (end - start) % (24 * 60) or 24 * 60     # az éjfélen átnyúló is; egyforma kezdet és vég: egész nap
    begin = base + dt.timedelta(minutes=start)
    return begin.timestamp(), (begin + dt.timedelta(minutes=length)).timestamp()


def window_of(now: float, start: int, end: int) -> tuple[tuple[float, float] | None, float]:
    """A mostani időszak (kezdet, vég), ha benne vagyunk (különben None), és a következő kezdete (helyi idő)."""
    today = dt.datetime.fromtimestamp(now).date()
    current: tuple[float, float] | None = None
    upcoming = float("inf")
    for offset in (-1, 0, 1, 2):
        begin, finish = _day_window(today + dt.timedelta(days=offset), start, end)
        if current is None and begin <= now < finish:
            current = (begin, finish)
        if begin > now:
            upcoming = min(upcoming, begin)
    return current, upcoming


def clock_text(when: float, now: float) -> str:
    """„17:20”, holnapra „holnap 16:30”."""
    moment = dt.datetime.fromtimestamp(when)
    days = (moment.date() - dt.datetime.fromtimestamp(now).date()).days
    hhmm = moment.strftime("%H:%M")
    return hhmm if days <= 0 else f"holnap {hhmm}" if days == 1 else moment.strftime("%m.%d. %H:%M")


def seconds_text(seconds: float) -> str:
    seconds = max(0, round(seconds))
    return f"{seconds} mp" if seconds < 90 else f"{round(seconds / 60)} perc"


def search_url(query: dict[str, Any], catalog: str = CATALOG) -> str:
    """A keresőszó (vagy a bemásolt keresés) találati oldala a legújabb hirdetésekkel elöl, az első oldal."""
    if query.get("url"):
        parts = urllib.parse.urlsplit(query["url"])
        pairs = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k not in _ORDER_DROP]
        query_text = urllib.parse.urlencode(pairs + [("order", "newest_first")], safe="[]")
        return urllib.parse.urlunsplit(parts._replace(query=query_text, fragment=""))
    return f"{catalog}?search_text={urllib.parse.quote(query['text'])}&order=newest_first"


class VintedReader:
    """A villámfigyelő saját böngészője (Playwright), a saját profiljában és ablakában: csak a villámfigyelő szálán
    használható (a Playwright szálhoz kötött). A képeket nem tölti le (gyorsabb, és a kártyák adata enélkül is ott
    van); a captchát nem kerüli meg, csak jelzi."""

    def __init__(self, cfg: Any, profile: Path, headless: bool | None = None):
        self.cfg = cfg
        self.profile = Path(profile)
        self.headless = headless
        self._pw: Any = None
        self._context: Any = None
        self._page: Any = None

    @property
    def running(self) -> bool:
        return self._context is not None

    def _open(self) -> Any:
        if self._context is not None:
            try:
                if self._page is None or self._page.is_closed():
                    pages = [p for p in self._context.pages if not p.is_closed()]
                    self._page = pages[0] if pages else self._context.new_page()
                return self._page
            except Exception:  # noqa: BLE001 - bezárták az ablakát: újraindítja
                self.close()
        if self._pw is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
        self.profile.mkdir(parents=True, exist_ok=True)
        options, attempts = launch_options(self.cfg, self.headless)
        options["args"] = [*options["args"], "--blink-settings=imagesEnabled=false"]
        last: Exception | None = None
        for extra in attempts:
            try:
                self._context = self._pw.chromium.launch_persistent_context(str(self.profile), **options, **extra)
                break
            except Exception as exc:  # noqa: BLE001 - a következőt próbálja
                last = exc
        if self._context is None:
            raise SniperError(f"A villámfigyelő böngészője nem indult: {_short(last)}")
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()
        return self._page

    def read(self, url: str) -> dict[str, Any]:
        """Egy találati oldal hirdetései: {"rows": [...], "captcha": bool}."""
        for attempt in range(2):
            page = self._open()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=LOAD_TIMEOUT)
                try:
                    page.wait_for_function(_READY_JS, timeout=ITEMS_WAIT)
                except Exception:  # noqa: BLE001 - lassú oldal: amit addig kirajzolt
                    pass
                if page_challenged(page):
                    return {"rows": [], "captcha": True}
                rows = page.evaluate(LISTINGS_JS) or []
                if not rows:                   # a kártyák a betöltés után jönnek (a lap szkriptje rajzolja)
                    try:
                        page.wait_for_selector(ITEM_LINK, timeout=ITEMS_WAIT)
                        rows = page.evaluate(LISTINGS_JS) or []
                    except Exception:  # noqa: BLE001 - tényleg nincs találat
                        pass
                rows = [r for r in rows if isinstance(r, dict) and r.get("price") and item_id(str(r.get("url")))]
                return {"rows": vinted_titles(rows), "captcha": False}
            except SniperError:
                raise
            except Exception as exc:  # noqa: BLE001
                if attempt or not is_closed_error(exc):
                    raise SniperError(f"Nem töltődött be a Vinted ({_short(exc)}).") from None
                self.close()
        raise SniperError("Nem töltődött be a Vinted.")

    def close(self) -> None:
        context, pw = self._context, self._pw
        self._context = self._page = self._pw = None
        for closer in (getattr(context, "close", None), getattr(pw, "stop", None)):
            if closer is not None:
                try:
                    closer()
                except Exception:  # noqa: BLE001 - már bezárult
                    pass


class Sniper:
    """A villámfigyelő beállításai és állapota (munka/villam.json), és a háttérszál, ami figyel."""

    def __init__(self, root: Path, reader: Any = None, catalog: str = CATALOG,
                 clock: Callable[[], float] = time.time, rng: random.Random | None = None):
        self.path = Path(root) / FILE
        self.reader = reader                   # VintedReader (a JarvisApp adja meg; a tesztekben egy helyi oldalé)
        self.catalog = catalog
        self.hosts = ((urllib.parse.urlsplit(catalog).hostname or "").removeprefix("www."),)
        self.clock = clock
        self.rng = rng or random.Random()
        # A JarvisApp állítja be: értesítés (kind, title, text, **extra), és hogy most dolgozhat-e (nincs Szünet).
        self.notify: Callable[..., None] = lambda *a, **k: None
        self.allowed: Callable[[], bool] = lambda: True
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._turn = 0
        self._saved_at = 0.0
        self._dirty = False
        self._held_since: float | None = None
        self._data = self._load()

    # --- tárolás ---

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        seen = data.get("seen") if isinstance(data.get("seen"), dict) else {}
        return {"enabled": bool(data.get("enabled")),
                "start": data.get("start") if parse_time(str(data.get("start") or "")) is not None else DEFAULT_START,
                "end": data.get("end") if parse_time(str(data.get("end") or "")) is not None else DEFAULT_END,
                "active": _int(data.get("active"), DEFAULT_ACTIVE, 1, 240),
                "pause": _int(data.get("pause"), DEFAULT_PAUSE, 0, 240),
                "interval": _int(data.get("interval"), DEFAULT_INTERVAL, MIN_INTERVAL, MAX_INTERVAL),
                "queries": [q for q in data.get("queries") or [] if isinstance(q, dict) and q.get("id")][:MAX_QUERIES],
                "seen": {str(k): v for k, v in seen.items() if isinstance(v, dict)},
                "hits": [h for h in data.get("hits") or [] if isinstance(h, dict) and h.get("url")][:MAX_HITS],
                "burst_until": float(data.get("burst_until") or 0),
                "blocked_until": float(data.get("blocked_until") or 0),
                "day": data.get("day") if isinstance(data.get("day"), dict) else {}}

    def _save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            self._saved_at, self._dirty = self.clock(), False

    def _changed(self) -> None:
        """A beállítás változott: mentés, és a szál rögtön újratervez (nem várja ki a mostani szünetét)."""
        self._save()
        self._wake.set()

    def _today(self, now: float) -> dict[str, Any]:
        """A mai nap számlálói (betöltések, találatok, captchák, lassítás); éjfélkor újrakezdődnek."""
        day = dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if self._data["day"].get("date") != day:
            self._data["day"] = {"date": day, "loads": 0, "hits": 0, "captchas": 0, "slow": 1.0, "told": []}
        return self._data["day"]

    # --- a felületnek ---

    def state(self) -> dict[str, Any]:
        now = self.clock()
        with self._lock:
            plan = self.plan(now)
            day = dict(self._today(now))
            queries = json.loads(json.dumps(self._data["queries"]))
            for query in queries:
                query["link"] = search_url(query, self.catalog)
            data = {k: self._data[k] for k in ("enabled", "start", "end", "active", "pause", "interval")}
            hits = [dict(h) for h in self._data["hits"]]
        running = sum(1 for q in queries if not q.get("off"))
        every = data["interval"] * float(day.get("slow") or 1.0)
        return {**data, "queries": queries, "hits": hits, "phase": plan["phase"], "until": plan.get("until"),
                "status": self._status(plan, now), "burst_until": self._data["burst_until"] if plan.get("burst") else 0,
                "blocked_until": self._data["blocked_until"] if self._data["blocked_until"] > now else 0,
                "loads_today": day.get("loads", 0), "hits_today": day.get("hits", 0), "slow": day.get("slow", 1.0),
                "load_ms": day.get("load_ms"), "every": round(every), "per_query": round(every * max(1, running)),
                "interval_choices": list(INTERVAL_CHOICES), "active_choices": list(ACTIVE_CHOICES),
                "pause_choices": list(PAUSE_CHOICES), "burst_minutes": BURST_MINUTES, "max_queries": MAX_QUERIES,
                "working": self.working()}

    def _status(self, plan: dict[str, Any], now: float) -> str:
        phase, until = plan["phase"], plan.get("until")
        if phase == "off":
            return "Ki van kapcsolva."
        if phase == "empty":
            return "Adj meg legalább egy keresőszót."
        if phase == "blocked":
            return (f"A Vinted robot-ellenőrzést kért: {clock_text(until, now)}-ig várok (a captchát az ablakában "
                    "megoldhatod, aztán Folytatás most).")
        if phase == "held":
            return "A Jarvis szünetel, ezért a villámfigyelő is."
        if phase == "active":
            if plan.get("burst"):
                return f"Figyelek (Most is): {clock_text(until, now)}-ig."
            pause = int(self._data["pause"])
            then = f", utána {pause} perc szünet" if pause and until < plan.get("window_end", until) else ""
            return f"Figyelek: {clock_text(until, now)}-ig{then}."
        if phase == "pause":
            return f"Szünet: {clock_text(until, now)}-kor folytatom ({self._data['active']} percig figyelek)."
        if until and until != float("inf"):
            return f"Legközelebb {clock_text(until, now)}-kor kezdek ({self._data['start']}–{self._data['end']})."
        return ""

    def working(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # --- beállítás ---

    def configure(self, enabled: Any = None, start: Any = None, end: Any = None, active: Any = None,
                  pause: Any = None, interval: Any = None) -> dict[str, Any]:
        with self._lock:
            data = self._data
            if start is not None or end is not None:
                begin, finish = str(start or data["start"]).strip(), str(end or data["end"]).strip()
                if parse_time(begin) is None or parse_time(finish) is None:
                    raise SniperError("Az időszak kezdete és vége óra:perc legyen, pl. 16:30 és 19:30.")
                data["start"], data["end"] = f"{parse_time(begin) // 60:02d}:{parse_time(begin) % 60:02d}", \
                    f"{parse_time(finish) // 60:02d}:{parse_time(finish) % 60:02d}"
            if active is not None:
                data["active"] = _int(active, data["active"], 1, 240)
            if pause is not None:
                data["pause"] = _int(pause, data["pause"], 0, 240)
            if interval is not None:
                data["interval"] = _int(interval, data["interval"], MIN_INTERVAL, MAX_INTERVAL)
            if enabled is not None:
                data["enabled"] = bool(enabled)
                if not enabled:
                    data["burst_until"] = 0.0
            self._changed()
        return self.state()

    def add(self, target: Any, max_price: Any = None, min_price: Any = None, exclude: Any = None,
            strict: Any = True, max_on: Any = None) -> dict[str, Any]:
        """Egy keresőszó (pl. „ps4”) vagy egy Vinted-keresés linkje (a szűrőivel, pl. kategória, márka)."""
        target = " ".join(str(target or "").split())[:200]
        if not target:
            raise SniperError("Adj meg egy keresőszót (pl. ps4), vagy egy Vinted-keresés linkjét.")
        query: dict[str, Any] = {"id": secrets.token_hex(4), "text": target, "url": "", "added": self.clock()}
        if re.match(r"https?://", target, re.IGNORECASE):
            parts = urllib.parse.urlsplit(target)
            host = (parts.hostname or "").lower().removeprefix("www.")
            if not any(host == h or host.endswith("." + h) for h in self.hosts) or \
                    not parts.path.rstrip("/").endswith("/catalog") and "/catalog/" not in parts.path:
                raise SniperError("Csak a Vinted egy keresésének vagy kategóriájának linkje jó (vinted.hu/catalog…), "
                                  "vagy egy keresőszó.")
            params = dict(urllib.parse.parse_qsl(parts.query))
            query.update(url=target, text=" ".join(params.get("search_text", "").split()))
        query.update(self._filters({"max_price": max_price, "min_price": min_price, "exclude": exclude,
                                    "strict": strict, "max_on": max_on}, {}))
        with self._lock:
            if len(self._data["queries"]) >= MAX_QUERIES:
                raise SniperError(f"Legfeljebb {MAX_QUERIES} keresőszót figyelek egyszerre (minél több, annál "
                                  "ritkábban kerül sorra egy-egy).")
            same = search_url(query, self.catalog)
            if any(search_url(q, self.catalog) == same or not q.get("url") and not query["url"]
                   and fold(q["text"]) == fold(query["text"]) for q in self._data["queries"]):
                raise SniperError("Ezt már figyelem.")
            self._data["queries"].append(query)
            self._changed()
        return self.state()

    def update(self, query_id: str, **fields: Any) -> dict[str, Any]:
        """Egy keresőszó szűrői (árhatár be/ki és az összege, alsó határ, kizáró szavak, címegyezés), vagy szünet."""
        with self._lock:
            query = self._query(query_id)
            if "off" in fields and fields["off"] is not None:
                query["off"] = bool(fields["off"])
            query.update(self._filters(fields, query))
            self._changed()
        return self.state()

    def remove(self, query_id: str) -> dict[str, Any]:
        with self._lock:
            self._query(query_id)
            self._data["queries"] = [q for q in self._data["queries"] if q["id"] != query_id]
            self._data["seen"].pop(query_id, None)
            self._changed()
        return self.state()

    def burst(self, on: bool = True) -> dict[str, Any]:
        """Most is: BURST_MINUTES percig az időszakon és a szakaszokon kívül is figyel (napi bekapcsolás nélkül is)."""
        with self._lock:
            if on and not any(not q.get("off") for q in self._data["queries"]):
                raise SniperError("Előbb adj meg egy keresőszót.")
            self._data["burst_until"] = self.clock() + BURST_MINUTES * 60 if on else 0.0
            self._changed()
        return self.state()

    def resume(self) -> dict[str, Any]:
        """Folytatás most: a robot-ellenőrzés utáni várakozás vége (pl. mert megoldottad a captchát az ablakában)."""
        with self._lock:
            self._data["blocked_until"] = 0.0
            self._changed()
        return self.state()

    def clear_hits(self) -> dict[str, Any]:
        with self._lock:
            self._data["hits"] = []
            self._changed()
        return self.state()

    def test(self) -> dict[str, Any]:
        """Próba-értesítés: így szól, ha talál (a telefonon is, a saját hangjával)."""
        self.notify("snipe", "⚡ Próba: PS4 Slim 500 GB · 35 000 Ft",
                    "Így szól a villámfigyelő, ha új hirdetést talál. Rábökve a hirdetés nyílik meg (a próbánál a "
                    "Vinted kezdőlapja).", url=HOME, snipe="proba-" + secrets.token_hex(3), urgent=True)
        return self.state()

    def _query(self, query_id: str) -> dict[str, Any]:
        for query in self._data["queries"]:
            if query["id"] == query_id:
                return query
        raise SniperError("Nincs ilyen keresőszó (lehet, hogy közben törölted).")

    @staticmethod
    def _filters(fields: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            if fields.get("max_price") is not None:
                out["max_price"] = parse_price(fields["max_price"]) or 0
            if fields.get("min_price") is not None:
                out["min_price"] = parse_price(fields["min_price"]) or 0
        except ItemError as exc:
            raise SniperError(f"Az ár: {exc}") from None
        if fields.get("exclude") is not None:
            out["exclude"] = parse_words(fields["exclude"])
        if fields.get("strict") is not None:
            out["strict"] = bool(fields["strict"])
        top = out.get("max_price", old.get("max_price") or 0)
        if fields.get("max_on") is not None:
            if fields["max_on"] and not top:
                raise SniperError("Előbb add meg, hány forint alatt szóljak.")
            out["max_on"] = bool(fields["max_on"])
        elif "max_price" in out:
            out["max_on"] = bool(top)          # egy megadott ár bekapcsolja az árhatárt, az üres kikapcsolja
        low = out.get("min_price", old.get("min_price") or 0)
        if top and low and low > top:
            raise SniperError("Az alsó határ nagyobb, mint a legfeljebb ár.")
        return out

    # --- mikor figyel ---

    def plan(self, now: float | None = None) -> dict[str, Any]:
        """Most mi a dolga: phase = off (kikapcsolva), empty (nincs keresőszó), outside (az időszakon kívül), pause (a
        szakaszok közti szünet), active (figyel), held (a Jarvis szünetel), blocked (robot-ellenőrzés után vár); until:
        meddig (illetve mikor kezd)."""
        now = self.clock() if now is None else now
        with self._lock:
            data = self._data
            burst = float(data["burst_until"] or 0) > now
            if not data["enabled"] and not burst:
                return {"phase": "off"}
            if not any(not q.get("off") for q in data["queries"]):
                return {"phase": "empty"}
            if burst:
                plan: dict[str, Any] = {"phase": "active", "until": float(data["burst_until"]), "burst": True}
            else:
                current, upcoming = window_of(now, parse_time(data["start"]) or 0, parse_time(data["end"]) or 0)
                if current is None:
                    return {"phase": "outside", "until": upcoming}
                begin, finish = current
                active, pause = int(data["active"]) * 60, int(data["pause"]) * 60
                plan = {"phase": "active", "until": finish, "window_end": finish}
                if pause > 0:
                    position = (now - begin) % (active + pause)
                    if position >= active:
                        return {"phase": "pause", "until": min(finish, now + active + pause - position),
                                "window_end": finish}
                    plan["until"] = min(finish, now + active - position)
            blocked = float(data["blocked_until"] or 0)
        if blocked > now:
            return {**plan, "phase": "blocked", "until": blocked}
        try:
            allowed = self.allowed()
        except Exception:  # noqa: BLE001
            allowed = True
        return plan if allowed else {**plan, "phase": "held"}

    # --- a háttérszál ---

    def start(self) -> None:
        if self.reader is not None and not self.working():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="jarvis-villam", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=15)
        if self._dirty:
            try:
                self._save()
            except OSError:
                pass

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    wait = self.tick()
                except Exception:  # noqa: BLE001 - a figyelő egy hiba miatt ne álljon le
                    wait = FAIL_WAIT
                self._wake.wait(max(0.2, wait))
                self._wake.clear()
        finally:
            self._close_reader()

    def _close_reader(self) -> None:
        if self.reader is not None and getattr(self.reader, "running", True):
            try:
                self.reader.close()
            except Exception:  # noqa: BLE001
                pass

    def tick(self) -> float:
        """Egy lépés: ha most figyelni kell, egy találati oldal (a soron következő keresőszóé). Visszaadja, hány
        másodperc múlva jöjjön a következő."""
        now = self.clock()
        plan = self.plan(now)
        phase = plan["phase"]
        if phase == "held":
            self._held_since = self._held_since or now
            if now - self._held_since >= HELD_CLOSE:
                self._close_reader()
            return IDLE_TICK / 3
        self._held_since = None
        if phase != "active":
            if phase in ("off", "empty", "outside"):
                self._close_reader()       # a szakaszok közti szünetben nyitva marad (nem ugrik fel újra az ablak)
            if self._dirty:
                self._save()
            until = plan.get("until")
            return max(0.5, min(IDLE_TICK, (until - now) if until else IDLE_TICK))
        query = self._next_query()
        if query is None:
            return IDLE_TICK
        url = search_url(query, self.catalog)
        started = time.monotonic()
        try:
            result = self.reader.read(url)
        except Exception as exc:  # noqa: BLE001 - SniperError, vagy egy váratlan böngészőhiba
            return self._failed(query, exc, now)
        took = time.monotonic() - started
        with self._lock:
            day = self._today(now)
            day["loads"] = int(day.get("loads", 0)) + 1
            day["load_ms"] = round(took * 1000)
        if result.get("captcha"):
            return self._captcha(now)
        self._absorb(query, list(result.get("rows") or []), self.clock())
        with self._lock:
            slow = float(self._today(now).get("slow") or 1.0)
            interval = float(self._data["interval"]) * slow
        return max(1.0, interval * self.rng.uniform(*JITTER) - took)

    def _next_query(self) -> dict[str, Any] | None:
        with self._lock:
            queries = [q for q in self._data["queries"] if not q.get("off")]
            if not queries:
                return None
            self._turn = (self._turn + 1) % len(queries)
            return queries[self._turn]

    def _failed(self, query: dict[str, Any], exc: Exception, now: float) -> float:
        text = str(exc) if isinstance(exc, SniperError) else f"Nem töltődött be a Vinted ({_short(exc)})."
        with self._lock:
            query["error"] = text
            query["fails"] = int(query.get("fails") or 0) + 1
            fails = query["fails"]
            self._dirty = True
        if fails == FAILS_LONG:
            self._tell_once("hiba", "Villámfigyelő: nem tölt be a Vinted",
                            f"{text} {FAIL_LONG_WAIT // 60} percenként újra próbálom.")
        return FAIL_LONG_WAIT if fails >= FAILS_LONG else FAIL_WAIT

    def _captcha(self, now: float) -> float:
        with self._lock:
            day = self._today(now)
            count = int(day.get("captchas", 0))
            wait = CAPTCHA_WAIT[min(count, len(CAPTCHA_WAIT) - 1)]
            day["captchas"] = count + 1
            day["slow"] = min(SLOW_MAX, float(day.get("slow") or 1.0) * SLOW_STEP)
            self._data["blocked_until"] = now + wait
            self._save()
        self._tell_once("captcha", "Villámfigyelő: a Vinted robot-ellenőrzést kért",
                        f"Nem kerülöm meg: {wait // 60} percig várok, és ma ritkábban frissítek. Ha a gépnél vagy, a "
                        "villámfigyelő ablakában megoldhatod, aztán az Automata kereskedés › Villámfigyelő › Folytatás "
                        "most gombbal rögtön folytatom.")
        return min(float(wait), IDLE_TICK)

    def _tell_once(self, key: str, title: str, text: str) -> None:
        """Egy hibáról vagy a robot-ellenőrzésről naponta egyszer szól."""
        now = self.clock()
        with self._lock:
            told = self._today(now).setdefault("told", [])
            if key in told:
                return
            told.append(key)
            self._dirty = True
        self.notify("error", title, text)

    # --- az új hirdetések ---

    @staticmethod
    def fits(query: dict[str, Any], row: dict[str, Any]) -> bool:
        """Illik-e a hirdetés a keresőszó szűrőihez (árhatár, alsó határ, kizáró szavak, a címe szerint ez-e)."""
        price = int(row.get("price") or 0)
        if price <= 0:
            return False
        if query.get("max_on") and query.get("max_price") and price > int(query["max_price"]):
            return False
        if query.get("min_price") and price < int(query["min_price"]):
            return False
        title = str(row.get("title") or row.get("text") or "")
        text = f"{title} {row.get('text') or ''}"
        if query.get("exclude") and excluded(query["exclude"], text):
            return False
        if reserved(text):
            return False
        if query.get("strict", True) and query.get("text") and not matches_query(query["text"], title):
            return False
        return True

    def _absorb(self, query: dict[str, Any], rows: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
        """Egy betöltés hirdetései: ami új (vagy leárazva most illik), arról szól. Az első betöltés (és egy hosszabb
        kihagyás után az első) csak megjegyzi, ami fent van."""
        with self._lock:
            qid = query["id"]
            seen: dict[str, Any] = self._data["seen"].setdefault(qid, {})
            last_ok = float(query.get("last_ok") or 0)
            rebase = max(REBASE_AFTER, (int(self._data["pause"]) + 15) * 60)
            # (egy üres első oldal után is: az első betöltés megjegyzi, hogy nincs fent semmi, és ami utána jön, az új)
            baseline = not last_ok or now - last_ok > rebase
            top = int(query.get("top") or 0)
            told = {h["key"] for h in self._data["hits"] if h.get("key")}
            hits: list[dict[str, Any]] = []
            for row in rows:
                url = str(row.get("url") or "")
                key = listing_key(url)
                price = int(row.get("price") or 0)
                if not key or price <= 0:
                    continue
                ok = not row.get("promoted") and self.fits(query, row)
                old = seen.pop(key, None)      # (újra a végére kerül: a legrégebben látott esik ki először)
                old = old if isinstance(old, list) and old else None
                old_price = int(old[0]) if old else 0
                was_told = bool(old and len(old) > 1 and old[1])
                fresh = old is None and not baseline
                drop = old is not None and not baseline and 0 < price < old_price
                alert = ok and (fresh or (drop and not was_told))
                if alert and key not in told:
                    told.add(key)
                    hits.append(self._hit(query, row, key, now, last_ok, drop=old_price if drop else 0,
                                          older=fresh and 0 < item_id(url) < top))
                seen[key] = [price, alert or was_told]
            while len(seen) > MAX_SEEN:
                seen.pop(next(iter(seen)))
            query["top"] = max([top] + [item_id(str(r.get("url") or "")) for r in rows])
            query["last_ok"] = now
            query["loads"] = int(query.get("loads") or 0) + 1
            query.pop("error", None)
            query["fails"] = 0
            query["empty"] = 0 if rows else int(query.get("empty") or 0) + 1
            empty = query["empty"]
            if hits:
                query["found"] = int(query.get("found") or 0) + len(hits)
                query["found_at"] = now
                self._data["hits"] = (hits + self._data["hits"])[:MAX_HITS]
                day = self._today(now)
                day["hits"] = int(day.get("hits", 0)) + len(hits)
            self._dirty = True
            if hits or now - self._saved_at >= SAVE_EVERY:
                self._save()
        if empty == EMPTY_WARN:
            self._tell_once(f"ures-{qid}", "Villámfigyelő: nem látok hirdetést",
                            f"A(z) „{query.get('text') or 'link'}” keresés oldalán {EMPTY_WARN}-szor egymás után nem "
                            "találtam hirdetést. Lehet, hogy tényleg nincs, vagy a Vinted átalakította az oldalt.")
        for hit in hits[:NOTIFY_MAX]:
            self._tell(hit)
        if len(hits) > NOTIFY_MAX:
            rest = hits[NOTIFY_MAX:]
            self.notify("snipe", f"⚡ Még {len(rest)} új hirdetés: {query.get('text') or 'link'}",
                        "; ".join(f"„{h['title'][:50]}” {format_price(h['price'])}" for h in rest[:8]),
                        url=search_url(query, self.catalog), snipe=rest[0]["key"], urgent=True)
        return hits

    @staticmethod
    def _hit(query: dict[str, Any], row: dict[str, Any], key: str, now: float, last_ok: float, drop: int = 0,
             older: bool = False) -> dict[str, Any]:
        price = int(row["price"])
        title = " ".join(str(row.get("title") or row.get("text") or "").split())[:160] or "Vinted-hirdetés"
        hit = {"key": key, "url": str(row["url"]), "title": title, "price": price,
               "total": price + buy_fee("vinted", price), "at": now, "query": query.get("text") or "link",
               "qid": query["id"], "within": round(now - last_ok) if last_ok else None}
        if drop:
            hit["drop"] = drop
        if older:
            hit["older"] = True
        if isinstance(row.get("likes"), int) and row["likes"] > 0:
            hit["likes"] = row["likes"]
        return hit

    def _tell(self, hit: dict[str, Any]) -> None:
        price = format_price(hit["price"])
        if hit.get("drop"):
            when = f"leárazta: {format_price(hit['drop'])} → {price}"
        elif hit.get("older"):
            when = "korábbi hirdetés, most került előre (előresorolták vagy átárazták)"
        elif hit.get("within"):
            when = f"az előző frissítés óta jött ({seconds_text(hit['within'])}-en belül)"
        else:
            when = "most jött"
        likes = f" Már {hit['likes']}-en kedvelték, siess." if hit.get("likes", 0) >= LIKES_HOT else ""
        text = (f"Vinted: „{hit['title']}” {price} (a Vinted-díjjal kb. {format_price(hit['total'])}), {when}. "
                f"Keresés: {hit['query']}.{likes} Rábökve megnyílik a hirdetés.")
        self.notify("snipe", f"⚡ {price} · {hit['title'][:70]}", text, url=hit["url"], snipe=hit["key"],
                    urgent=True)


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def make_sniper(root: Path, browser_cfg: Any, enabled: bool = True) -> Sniper | None:
    """A Munka menü villámfigyelője a saját böngészőjével (a [browser] beállításaival: ugyanaz a böngésző, külön
    profilban, sniper_profile_dir); None, ha nincs böngésző."""
    if not enabled:
        return None
    from .config import data_path
    profile = data_path(getattr(browser_cfg, "sniper_profile_dir", ""), PROFILE)
    headless = getattr(browser_cfg, "sniper_headless", None)
    return Sniper(root, VintedReader(browser_cfg, profile, headless=bool(headless) if headless else None))


__all__ = ["Sniper", "SniperError", "VintedReader", "make_sniper", "search_url", "window_of"]
