"""Azonnali figyelő: amint új hirdetés kerül fel a Hardverapróra vagy a Jófogásra, kideríti, megéri-e megvenni, és csak
akkor szól, ha új ÉS megéri (a jó vételek többnyire a friss hirdetésekből jönnek; ezeken az oldalakon nincs is „szólj, ha
új jön” értesítés).

Megadod, mit figyeljen: egy keresőszót (pl. „RTX 3080”, mindkét oldalon), vagy egy találati oldal címét (pl. Hardverapró ›
Videókártyák); a futó Keresések és a Figyelt oldalak hardveraprós és jófogásos része magától is ide kerül
(link_searches, ötpercenként). A Jarvis 2–15 percenként (alapból 3) letölti a találati oldal elejét, böngésző nélkül (feeds.SiteFetcher:
oldalanként egyszerre egy kérés, szünetekkel, óránként legfeljebb INSTANT_HOURLY), egy saját szálon: nem vár a
böngészőre, és arra sem, hogy épp beszélgetsz-e vele. Az első letöltésnél csak megjegyzi, ami már fent van; utána minden
új (vagy leárazott) hirdetést rögtön felmér (DealHunter.appraise): modell nélkül a felmért kereséseid és az árbázis
szokásos árához méri; amit így nem ismer, azt a modell nevezi meg, és ha kell, megnézi, mennyiért adják a másik oldalon;
a megérő jelölt oldalát átnézi (elkelt, hibás, gyanús?), és egy rövid modellkérdés után szól, a telefonra is (sürgős
értesítésként), az árral, a szokásos árral és a várható haszonnal. A vételi limit (Automata kereskedés: legfeljebb
ennyiért) fölötti hirdetést meg sem nézi. Ha épp dolgozol a Jarvisszal, a modellt nem zavarja: ami csak a modellel
ismerhető fel, az utána jön (legfeljebb WAIT_MAX-ig); a modell nélkül felmérhető megérőről rögtön szól, és a modell
ráérő időben utólag nézi meg (_followups). A letöltések a keretből egyenletesen oszlanak el (_floor), csúcsidőben a
leggyorsabb ütemben; az új hirdetések késését (a kártyán lévő feltöltési idő óta) feljegyzi (delay).

Csak olvas: semmit nem vesz meg, nem ír senkinek. Az Automata kereskedés Indítás gombjával indul, a Leállítással és a
Szünettel áll le. Ha egy oldal tilt vagy robot-ellenőrzést kér, azon egy ideig nem próbálkozik (a SiteFetcher), és egyszer
szól.
"""
from __future__ import annotations

import json
import os
import queue
import random
import re
import secrets
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .deals import (DEALS_ACTION, DROP, MAX_EXCLUDE, DealError, Deferred, deal_action, excluded, matches_query,
                    newest_first, parse_words, safety_text)
from .feeds import FAST_SITES, FeedError, Quota
from .pricebook import wearable
from .llm import background_calls
from .selling import _WANTED_RE, ItemError, Platform, format_price, listing_key, parse_price
from .textutil import fold

MAX_ALERTS = 12
EVERY_CHOICES = (2, 3, 5, 10, 15)    # percben
DEFAULT_EVERY = 3
INSTANT_HOURLY = 90        # az azonnali figyelő oldalanként óránként legfeljebb ennyit kér (a többi a köröké)
ROUND_RESERVE = 30         # a letöltő óránkénti keretéből ennyit mindig meghagy a köröknek (különben azok üresen futnak)
# A figyelő része a közös letöltő keretéből: minden tényleges kérése (a lista beállításáé is) ezzel a címkével számít, és a
# letöltő érvényesíti (feeds.Quota).
QUOTA = Quota("azonnali", cap=INSTANT_HOURLY, reserve=ROUND_RESERVE)
# A Keresések (Automata kereskedés) a Hardveraprón és a Jófogáson az azonnali figyelőben is futnak, ennyi percenként
# (a tanult ritmus és az óránkénti keret szerint): az új hirdetést így pár percen belül felméri, nem csak a keresés
# óránkénti körében. A felületen a figyelők között látszanak („a Keresésekből”); a keresés törlésével megszűnnek.
LINKED_EVERY = 5
LINK_CHECK = 30            # ennyi másodpercenként nézi meg, változtak-e a keresések
MAX_SEEN = 500             # figyelőnként ennyi látott hirdetést jegyez meg
MAX_HITS = 60
MAX_ROWS = 120             # egy letöltésből ennyi hirdetést néz: az oldal egészét (a Hardverapró 100-at mutat, és az
                           # elején többnyire a fizetett kiemelések vannak); az értékelést a várólista korlátozza
OVERFLOW_MIN = 10          # ha egy letöltésen legalább ennyi (nem kiemelt) hirdetés van, és az oldal alján lévő
OVERFLOW_TAIL = 5          # ennyi mind új, a következő oldalt is megnézi (a kiemeltek elöl mindig a régiek)
MAX_WAITING = 300          # ennyi hirdetés várhat (a modellre, a Szünet vagy a kéréskeret miatt; két teljes oldal is)
WAIT_MAX = 1800            # ennyi ideig (másodperc); ami addig sem került sorra (vagy nem fér el), azt a figyelő
                           # elfelejti: ha még fent van, a következő letöltésnél újra új hirdetésként jön (_forget)
TICK = 2.0
FOLLOWUP_EVERY = 20        # ráérő időben ennyi másodpercenként nézi, van-e utólag ellenőrizendő találat
# Előmelegítés (DealHunter.warm): ráérő időben, legfeljebb WARM_EVERY másodpercenként egy lépés (egy leírás, vagy csendes
# órában egy árkeresés), csak csúcsidőn kívül, ha a figyelő keretének kevesebb mint WARM_BELOW része ment el ebben az
# órában, és az oldal most rögtön kérhető (nem vár a szünetre: egy közben jövő új hirdetés értékelését nem késlelteti).
WARM_EVERY = 60
WARM_BELOW = 0.5
BOOK_SAVE = 600            # az árbázist legfeljebb ennyi másodpercenként írja ki (nagy fájl; a körök is mentik)
JITTER = (0.85, 1.15)
# Tanult ritmus: oldalanként feljegyzi, a nap melyik órájában mennyi új (illő) hirdetés jön (a régebbiek súlya minden új
# adatnál RHYTHM_DECAY-szeresére csökken). RHYTHM_MIN új hirdetés után a csúcsidőben (az óránkénti átlag PEAK-szerese
# fölött) feleannyi, a csendes órákban (QUIET-szerese alatt) kétszer annyi időnként néz (legfeljebb MAX_EVERY perc);
# az óránkénti keret (INSTANT_HOURLY) ugyanaz marad.
RHYTHM_MIN = 20
RHYTHM_DECAY = 0.99
PEAK, QUIET = 1.5, 0.5
MAX_EVERY = 30
# Egyenletes ütem a keretből: oldalanként a figyelők letöltései (a keret POLL_SHARE része; a többi a megérő hirdetések
# oldalára és az árkutatásra marad) egyenletesen oszlanak el az órán belül (_floor), különben az óra elején elfogyna a
# keret, és utána fél óráig semmit nem látna. Csúcsidőben a keret szerinti leggyorsabb ütemben néz (de legfeljebb
# PEAK_EVERY percenként), és ha egy letöltésen új hirdetés jött, a következő hamarabb jön (BURST-szörös idő múlva):
# az új hirdetések csoportosan jönnek.
POLL_SHARE = 0.75
PEAK_EVERY = 1
BURST = 0.5
# Késés: a Hardverapró és a Jófogás kártyáján ott a feltöltés ideje („ma 14:07”); az új hirdetésnél feljegyzi, mennyivel
# utána vette észre (oldalanként a legutóbbi MAX_DELAYS), a felületen az elmúlt DELAY_WINDOW középértéke látszik.
MAX_DELAYS = 300
DELAY_WINDOW = 6 * 3600
# Kiemelt hirdetések elöl: a Hardverapró egy kategóriájának elején az „Előresorolt” (fizetett) hirdetések akár egy fél
# oldalt is kitesznek, és mindig ugyanazok. A figyelő megjegyzi, hány ilyen áll az elején (lead), és a következő
# letöltésnél a cím offset= paraméterével átugorja őket, LEAD_MARGIN kiemeltet meghagyva jelzőnek: ha a letöltött oldal
# nem kiemelttel kezdődik, a kiemeltek elfogyhattak, és lehet, hogy egy új hirdetést is átugrott, ezért rögtön újra
# letölti az elejétől. Ha bemásolt címben offset= van, azt kezdő becslésnek veszi (a címből kiveszi, maga kezeli).
LEAD_SITES = ("hardverapro",)
LEAD_MARGIN = 5
# Közös friss lista: egy keresőszó nélküli figyelő (pl. Hardverapró › Hardver, a legújabb elöl) egy letöltéssel minden
# keresőszóra hoz friss hirdetést. Ha egy keresőszavas figyelő találatai rendre ebben is megvannak (legalább COVER_MIN,
# és legfeljebb COVER_MISS arányban jött olyan, ami csak a saját keresésében volt), azt csak COVERED_EVERY percenként
# tölti le külön (biztonsági hálónak), és a keretből COVERED_WEIGHT résszel számít: így a közös lista sűrűbben jöhet.
COVER_MIN = 5
COVER_MISS = 0.1
COVER_DECAY = 0.98
COVERED_EVERY = 15
COVERED_WEIGHT = 0.25
# A közös lista csak akkor fed le egy keresőszavas figyelőt, ha tényleg fut: be van kapcsolva, nem szünetel, nem tilt az
# oldal, és COVER_STALE másodpercen belül letöltött (különben a keresőszavas figyelő a saját ütemében néz).
COVER_STALE = 20 * 60
# Egy hirdetés a figyelő látottjai közt (seen): [mikor láttuk utoljára, ár, állapot, ki értékeli, mióta vár]. Az állapot
# SEEN_LISTED (letöltve: az első letöltésnél fent volt, vagy nem illik a figyelőhöz, nincs mit felmérni), SEEN_QUEUED
# (felmérésre vár: ez a figyelő, vagy a közös lista értékeli, owner), SEEN_DONE (felmérve). A régi, kételemű bejegyzés
# felmértnek számít. Ami PENDING_STALE-nél régebben vár (pl. a közös lista értékelése elmaradt), az újra új.
SEEN_LISTED, SEEN_QUEUED, SEEN_DONE = "l", "v", "e"
PENDING_STALE = WAIT_MAX + 600
_POSTED_RE = re.compile(r"\b(ma|tegnap),?\s+(\d{1,2})[:.](\d{2})\b")


RHYTHM_TEXT = {0.5: "csúcsidő: sűrűbben nézem", 2.0: "csendes óra: ritkábban nézem"}


def split_offset(url: str) -> tuple[str, int]:
    """A cím offset= paraméter nélkül, és az értéke (0, ha nincs)."""
    parts = urllib.parse.urlsplit(url)
    pieces = [piece for piece in parts.query.split("&") if piece]
    found = [piece for piece in pieces if piece.split("=", 1)[0] == "offset"]
    if not found:
        return url, 0                          # (a cím változatlan: a kódolását sem írja át)
    value = found[-1].split("=", 1)[1] if "=" in found[-1] else ""
    query = "&".join(piece for piece in pieces if piece not in found)
    return urllib.parse.urlunsplit(parts._replace(query=query)), int(value) if value.isdigit() else 0


def with_offset(url: str, offset: int) -> str:
    """A cím offset= paraméterrel (0-nál nélküle)."""
    base, _old = split_offset(url)
    if offset <= 0:
        return base
    parts = urllib.parse.urlsplit(base)
    return urllib.parse.urlunsplit(parts._replace(query="&".join(q for q in (parts.query, f"offset={offset}") if q)))


def leading_promoted(rows: list[dict[str, Any]]) -> int:
    """Hány kiemelt hirdetés áll a lista elején."""
    return next((n for n, row in enumerate(rows) if not row.get("promoted")), len(rows))


def posted_ago(text: str, now: float) -> float | None:
    """Mennyi ideje került fel a hirdetés (másodperc), a kártyán lévő időből („ma 14:07”, „tegnap 23:50”, helyi idő);
    None, ha nincs benne, vagy nem hihető (a jövőben, vagy 12 óránál régebben)."""
    match = _POSTED_RE.search(fold(text or ""))
    if not match:
        return None
    hour, minute = int(match.group(2)), int(match.group(3))
    if hour > 23 or minute > 59:
        return None
    local = time.localtime(now)
    posted = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, hour, minute, 0, 0, 0, -1))
    if match.group(1) == "tegnap":
        posted -= 86400
    ago = now - posted
    return max(0.0, ago) if -120 <= ago <= 12 * 3600 else None


class InstantError(DealError):
    pass


class _Stop(Exception):
    """A figyelő leáll (kilépés)."""


class _Off(Exception):
    """Közben leállítottad a figyelőt (Automata kereskedés: Leállítás), vagy törölted ezt a figyelőjét: a futó értékelés
    hátralévő része elmarad (se letöltés, se modellkérdés, se értesítés)."""


class InstantWatch:
    """Az azonnali figyelők (munka/azonnali.json) és a háttérszál, ami letölti őket."""

    def __init__(self, root: Path, platforms: dict[str, Platform], fetcher: Any, hunter: Any,
                 clock: Callable[[], float] = time.time, rng: random.Random | None = None):
        self.path = Path(root) / "azonnali.json"
        self.platforms = {k: p for k, p in platforms.items() if k in FAST_SITES and p.search_url}
        self.fetcher = fetcher
        self.hunter = hunter
        self.clock = clock
        self.rng = rng or random.Random()
        # A JarvisApp állítja be: értesítés (kind, title, text, **extra), és hogy most dolgozhat-e (nincs
        # Szünet, a háttérmunka nincs kikapcsolva).
        self.notify: Callable[..., None] = lambda *a, **k: None
        self.allowed: Callable[[], bool] = lambda: True
        # Kérdezheti-e most a modellt (nem, amíg épp dolgozol a Jarvisszal: addig a hirdetés vár, lásd _waiting).
        self.model_ok: Callable[[], bool] = lambda: True
        # (figyelő, oldal, hirdetés, mióta vár, melyik oldal kerete kell hozzá: üres, ha egyiké sem, pl. a modellre vár)
        self._waiting: list[tuple[str, str, dict[str, Any], float, tuple[str, ...]]] = []
        self._lock = threading.RLock()
        self._next: dict[str, float] = {}           # figyelő:oldal → mikor jön legközelebb (a memóriában)
        self._told_blocked: dict[str, float] = {}   # oldal → mikor szóltunk, hogy tilt (naponta egyszer)
        self._book_saved = 0.0
        self._linked_at = float("-inf")        # mikor nézte meg utoljára a kereséseket (link_searches)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Az új hirdetések értékelése (árbázis, modell, a hirdetés oldala) a saját szálán, hogy egy lassú értékelés ne
        # késleltesse a következő letöltést (start; a tesztekben, ha nem indul el, a letöltés szálán megy).
        self._jobs: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._followed_at = float("-inf")     # mikor nézte meg utoljára az utólag ellenőrizendőket (_followups)
        self._warmed_at = float("-inf")       # mikor melegített elő utoljára (_warm)
        self._data = self._load()
        if hasattr(hunter, "listen"):
            hunter.listen(self._deal_changed)   # egy találat utólagos változása a friss hirdetések közt is látszik

    # --- tárolás ---

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        seen = data.get("seen") if isinstance(data.get("seen"), dict) else {}
        return {"enabled": bool(data.get("enabled")),
                "alerts": [a for a in data.get("alerts") or [] if isinstance(a, dict) and a.get("id")],
                "seen": {str(k): v for k, v in seen.items() if isinstance(v, dict)},
                "hits": [h for h in data.get("hits") or [] if isinstance(h, dict) and h.get("url")],
                "delays": {str(k): [list(x)[:2] for x in v if isinstance(x, (list, tuple)) and len(x) >= 2][-MAX_DELAYS:]
                           for k, v in (data.get("delays") or {}).items() if isinstance(v, list)},
                "rhythm": {str(k): [float(x) for x in v][:24] for k, v in (data.get("rhythm") or {}).items()
                           if isinstance(v, list) and len(v) == 24}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def link_searches(self) -> None:
        """A futó Keresések (DealHunter.running_searches) és a Figyelt oldalak (running_watches) Hardverapró- és
        Jófogás-része figyelőként: ami új, felveszi (a keresésnél ugyanazzal a keresőszóval, ársávval és kizáró
        szavakkal, a figyelt oldalnál a címével), ami megváltozott, frissíti, a törölt keresését vagy oldalát kiveszi.
        Az első letöltésnél, mint minden figyelő, csak megjegyzi, ami már fent van."""
        self._linked_at = self.clock()
        try:
            searches = self.hunter.running_searches()
        except AttributeError:
            return
        try:
            watches = self.hunter.running_watches()
        except AttributeError:
            watches = []
        with self._lock:     # amit egy saját figyelőd már ugyanezzel a szóval (vagy címmel) néz, azt nem kéri le kétszer
            own = [a for a in self._data["alerts"] if not a.get("search")]
            covered = {(fold(a.get("query") or ""), site) for a in own if not a.get("url") for site in a.get("sites") or []}
            pages = {self._url(a, site) for a in own if a.get("url") for site in a.get("sites") or []
                     if site in self.platforms}
        wanted: dict[str, dict[str, Any]] = {}
        for search in searches:
            sites = [k for k in search.get("sites") or [] if k in self.platforms
                     and (fold(str(search.get("query") or "")), k) not in covered]
            if sites and search.get("query"):
                wanted["k" + str(search["id"])] = {
                    "query": str(search["query"])[:80], "url": "", "sites": sites, "name": str(search["query"])[:80],
                    "max_price": search.get("max_price") or None, "min_price": search.get("min_price") or None,
                    "exclude": list(search.get("exclude") or [])[:MAX_EXCLUDE], "every": LINKED_EVERY,
                    "search": str(search["id"])}
        for watch in watches:                # a figyelt oldal (pl. Hardverapró › Videókártyák) is pár percenként
            site, url = str(watch.get("platform") or ""), str(watch.get("url") or "")
            if site in self.platforms and url and newest_first(url, site) not in pages:
                wanted["f" + str(watch["id"])] = {
                    "query": "", "url": url, "sites": [site], "name": str(watch.get("name") or url)[:80],
                    "max_price": None, "min_price": None, "exclude": [], "every": LINKED_EVERY,
                    "search": "f" + str(watch["id"]), "origin": "watches"}
        with self._lock:
            alerts, changed = [], False
            for alert in self._data["alerts"]:
                if not alert.get("search"):
                    alerts.append(alert)
                    continue
                spec = wanted.pop(alert["id"], None)
                if spec is None:                       # a keresést (vagy a figyelt oldalt) törölted, vagy újra felméri
                    self._data["seen"].pop(alert["id"], None)
                    changed = True
                    continue
                if any(alert.get(key) != value for key, value in spec.items()):
                    if alert.get("query") != spec["query"] or alert.get("url") != spec["url"] \
                            or set(alert.get("sites") or []) != set(spec["sites"]):
                        alert["baseline"] = []         # más keresés: előbb megint csak megjegyzi, ami fent van
                    alert.update(spec)
                    changed = True
                alerts.append(alert)
            for ident, spec in wanted.items():
                alerts.append({"id": ident, "created": self.clock(), **spec})
                changed = True
            if changed:
                self._data["alerts"] = alerts
                self._save()

    def state(self) -> dict[str, Any]:
        """A felületnek: be van-e kapcsolva, a figyelők (mikor nézte, hányat talált), a friss hirdetések, az oldalak
        állapota (tilt-e), és hány kérés megy így óránként."""
        with self._lock:
            alerts = json.loads(json.dumps(self._data["alerts"]))
            for alert in alerts:                # amelyik oldalon a közös lista hozza a találatait (ritkábban nézi)
                alert["covered"] = [k for k in alert.get("sites") or [] if self._covered(alert, k)]
            hits = [dict(h) for h in self._data["hits"]]
            enabled = self._data["enabled"]
        status = self.fetcher.status() if self.fetcher is not None else {}
        load = self._load_per_hour(alerts)
        return {"enabled": enabled, "alerts": alerts, "hits": hits, "every_choices": list(EVERY_CHOICES),
                "sites": [{"key": k, "name": p.name, **status.get(k, {}), "per_hour": load.get(k, 0),
                           "rhythm": RHYTHM_TEXT.get(self._factor(k, self.clock()), ""), "delay": self.delay(k),
                           "every_s": round(self._floor(k))}
                          for k, p in self.platforms.items()],
                "hourly": INSTANT_HOURLY, "presets": self.presets(), "working": self.working(),
                "waiting": len(self._waiting)}

    def working(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def presets(self) -> list[dict[str, str]]:
        """Gyors hozzáadás: a Hardverapró felkapott kategóriái (csak ami megéri)."""
        from .deals import BROWSE_LANES
        if "hardverapro" not in self.platforms:
            return []
        every = [{"name": "Minden hardver (közös friss lista)", "url": "https://hardverapro.hu/aprok/hardver/index.html"}]
        return every + [{"name": name, "url": target} for name, target in BROWSE_LANES.get("hardverapro", [])
                        if target.startswith("http")]

    @staticmethod
    def _load_per_hour(alerts: list[dict[str, Any]]) -> dict[str, int]:
        out: dict[str, int] = {}
        for alert in alerts:
            if alert.get("off"):
                continue
            for site in alert.get("sites") or []:
                out[site] = out.get(site, 0) + round(60 / max(1, int(alert.get("every") or DEFAULT_EVERY)))
        return out

    # --- beállítás ---

    def set_enabled(self, on: bool) -> dict[str, Any]:
        with self._lock:
            self._data["enabled"] = bool(on)
            self._next.clear()                 # bekapcsoláskor rögtön kezd
            if not on:
                self._waiting = []
            self._save()
        return self.state()                    # a naplóba az Indítás és a Leállítás ír (DealHunter.start, stop)

    def add(self, target: str, sites: Any = None, max_price: Any = None, min_price: Any = None,
            exclude: Any = None, every: Any = None) -> dict[str, Any]:
        """Új figyelő: egy keresőszó (a megadott oldalakon), vagy egy Hardverapró- vagy Jófogás-találati oldal címe. Csak
        az új és megérő hirdetésekről szól."""
        target = " ".join(str(target or "").split())[:300]
        if not target:
            raise InstantError("Írd be, mit figyeljek (pl. „RTX 3080”), vagy másolj be egy találati oldalt.")
        alert: dict[str, Any] = {"id": secrets.token_hex(4), "created": self.clock()}
        if re.match(r"https?://", target):
            host = (urllib.parse.urlsplit(target).hostname or "").removeprefix("www.")
            platform = next((k for k, p in self.platforms.items()
                             if any(host == d or host.endswith("." + d) for d in p.domains)), None)
            if platform is None:
                names = " vagy a ".join(p.name for p in self.platforms.values())
                raise InstantError(f"Azonnal csak a {names} találati oldalát tudom figyelni (a Vinted maga is szól az "
                                   "újakról).")
            parts = urllib.parse.urlsplit(target)
            segments = [seg for seg in parts.path.split("/") if seg and not seg.startswith("index")
                        and not seg.endswith((".php", ".html"))]
            query = urllib.parse.parse_qs(parts.query)
            label = " ".join((query.get("q") or query.get("stext") or
                              [urllib.parse.unquote(segments[-1]).replace("-", " ").replace("_", " ")
                               if segments else ""])[0].split())
            alert.update(url=target, sites=[platform], query="",
                         name=self.platforms[platform].name + (f": {label[:40]}" if label else ""))
        else:
            chosen = [k for k in self.platforms if k in (sites if isinstance(sites, (list, tuple)) else [])] or \
                list(self.platforms)
            alert.update(query=target[:80], url="", sites=chosen, name=target[:80])
        try:
            alert["max_price"] = parse_price(max_price) or None
            alert["min_price"] = parse_price(min_price) or None
        except ItemError:
            raise InstantError("Az ár egy szám legyen, forintban.") from None
        if alert["min_price"] and alert["max_price"] and alert["min_price"] > alert["max_price"]:
            raise InstantError("A legalacsonyabb ár nagyobb, mint a legmagasabb.")
        alert["exclude"] = parse_words(exclude or [])[:MAX_EXCLUDE]
        try:
            minutes = int(every) if every not in (None, "") else DEFAULT_EVERY
        except (TypeError, ValueError):
            minutes = DEFAULT_EVERY
        alert["every"] = min(EVERY_CHOICES, key=lambda choice: abs(choice - minutes))
        with self._lock:
            if any(a.get("url") == alert["url"] and fold(a.get("query") or "") == fold(alert["query"])
                   and set(a.get("sites") or []) == set(alert["sites"]) for a in self._data["alerts"]):
                raise InstantError("Ezt már figyelem.")
            if sum(1 for a in self._data["alerts"] if not a.get("search")) >= MAX_ALERTS:
                raise InstantError(f"Legfeljebb {MAX_ALERTS} azonnali figyelő lehet.")
            self._data["alerts"].append(alert)
            self._save()
        return self.state()

    def remove(self, alert_id: str) -> dict[str, Any]:
        with self._lock:
            self._data["alerts"] = [a for a in self._data["alerts"] if a.get("id") != alert_id]
            self._data["seen"].pop(alert_id, None)
            self._save()
        return self.state()

    def _deal_changed(self, deal_id: str, deal: dict[str, Any] | None) -> None:
        """Egy találat utólag megváltozott (DealHunter.listen: az utólagos modellellenőrzés után ellenőrzött lett, mégsem
        jó, vagy levette, mert gyanús): a friss hirdetések közt is ez látszik, és amit elutasított vagy veszteségesnek
        bizonyult, az nem marad „Megéri”."""
        with self._lock:
            changed = False
            for hit in self._data["hits"]:
                if hit.get("deal") != deal_id:
                    continue
                changed = True
                if deal is None:
                    hit.update(cheap=False, withdrawn="utólag levettem: a modell szerint gyanús", model_pending=False,
                               verified=False)
                    continue
                hit.update(verified=bool(deal.get("verified")), unchecked=deal.get("unchecked") or "",
                           model_pending=bool(deal.get("model_pending")), profit=deal.get("profit", hit.get("profit")),
                           reference=deal.get("median", hit.get("reference")), safety=deal.get("safety"),
                           warnings=deal.get("warnings") or [])
                for key in ("condition_text", "cost_parts", "cond_band", "model_no"):
                    if deal.get(key):
                        hit[key] = deal[key]
                    else:
                        hit.pop(key, None)
                if deal.get("model_no") or int(deal.get("profit") or 0) <= 0:
                    hit["cheap"] = False       # mégsem jó, vagy a teljes költséggel nem hoz hasznot: nem „Megéri”
            if changed:
                self._save()

    def forget_deal(self, deal_id: str) -> None:
        """Visszamondtad a vételt: a friss hirdetések közül is kikerül (a hirdetést látottnak tudja, nem szól újra)."""
        with self._lock:
            hits = [h for h in self._data["hits"] if h.get("deal") != deal_id]
            if len(hits) != len(self._data["hits"]):
                self._data["hits"] = hits
                self._save()

    def pause(self, alert_id: str, off: bool) -> dict[str, Any]:
        """Egy figyelő szüneteltetése (a beállításai és amit már látott, megmarad)."""
        with self._lock:
            for alert in self._data["alerts"]:
                if alert.get("id") == alert_id:
                    alert["off"] = bool(off)
                    if not off:
                        for site in alert.get("sites") or []:
                            self._next.pop(f"{alert_id}:{site}", None)
            self._save()
        return self.state()

    # --- a háttérszál ---

    def start(self) -> None:
        if self._thread is None and self.fetcher is not None and self.platforms:
            self._thread = threading.Thread(target=self._loop, name="jarvis-azonnali", daemon=True)
            self._thread.start()
            self.start_worker()

    def start_worker(self) -> None:
        """Az értékelő szál: a letöltés szála csak beteszi az új hirdetéseket, ez méri fel őket (DealHunter.appraise)."""
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._evaluate, name="jarvis-azonnali-ertekeles", daemon=True)
            self._worker.start()

    def _evaluate(self) -> None:
        """A sorból egyenként: leállítva (vagy ha a figyelőt törölted) elmarad; ha ezt a figyelőt szünetelteted, a
        várakozók közé kerül (a folytatásakor jön, WAIT_MAX-ig); Szünet alatt, kikapcsolt háttérmunkával vagy az
        üzemidőn kívül megtartja, és vár (nem kérdezi a modellt, nem tölt le, nem szól)."""
        with background_calls():       # a modellhívásai háttérhívások: a feltöltés és a beszélgetés előbb jön
            job = None
            while not self._stop.is_set():
                if job is None:
                    try:
                        job = self._jobs.get(timeout=0.5)
                    except queue.Empty:
                        self._followups()
                        self._warm()
                        continue
                _alert, site, rows = job
                with self._lock:
                    enabled = self._data["enabled"]
                    stored = next((a for a in self._data["alerts"] if a.get("id") == job[0]["id"]), None)
                    stored = json.loads(json.dumps(stored)) if stored is not None else None
                if not enabled or stored is None:
                    job = None                 # leállítottad, vagy törölted a figyelőt: elmarad
                    continue
                if stored.get("off"):
                    self._park(stored["id"], site, rows)
                    job = None
                    continue
                if not self._may_work():
                    self._stop.wait(1.0)       # Szünet: a feladat megmarad, utána jön
                    continue
                job = None
                try:
                    self._report(stored, site, rows)
                except _Stop:
                    return
                except Exception:  # noqa: BLE001 - az értékelés egy hiba miatt ne álljon le
                    pass

    def _followups(self, force: bool = False) -> None:
        """Ráérő időben (üres sornál, legfeljebb FOLLOWUP_EVERY másodpercenként; force: rögtön, egy köteg után): amit
        ellenőrzés nélkül jelzett (gyors riasztás, vagy a modell foglalt volt), azt most megnézeti
        (DealHunter.followups), ha a modell kérdezhető."""
        now = self.clock()
        if (not force and now - self._followed_at < FOLLOWUP_EVERY) or not hasattr(self.hunter, "followups"):
            return
        self._followed_at = now
        try:
            if self._on() and self._may_work() and self.model_ok():
                self.hunter.followups(self.model_ok, self.notify)
        except Exception:  # noqa: BLE001 - egy hiba miatt ne álljon le az értékelés
            pass

    def _spare(self, site: str, now: float) -> bool:
        """Van-e most kimaradó keret ezen az oldalon az előmelegítéshez: nincs csúcsidő, a figyelő keretének kevesebb mint
        WARM_BELOW része ment el ebben az órában, és az oldal most rögtön kérhető."""
        if self._factor(site, now) < 1 or not self._has_budget(site, now):
            return False
        used = getattr(self.fetcher, "used", None)
        if used is not None and used(site, QUOTA.tag) >= INSTANT_HOURLY * WARM_BELOW:
            return False
        ready = getattr(self.fetcher, "ready", None)
        return ready is None or bool(ready(site))

    def _warm(self, force: bool = False) -> str:
        """Ráérő időben (üres sornál, legfeljebb WARM_EVERY másodpercenként) egy előmelegítő lépés (DealHunter.warm): a
        gyakran felbukkanó termékek összehasonlítóinak egy leírása, vagy csendes órában (mindkét oldal ráér) egy
        árkeresés a kevés árú termékre. Visszaadja, mit csinált."""
        now = self.clock()
        if (not force and now - self._warmed_at < WARM_EVERY) or not hasattr(self.hunter, "warm"):
            return ""
        self._warmed_at = now
        try:
            if not (self._on() and self._may_work()):
                return ""
            spare = [site for site in self.platforms if self._spare(site, now)]
            if not spare:
                return ""
            quiet = len(spare) == len(self.platforms) and all(self._factor(site, now) > 1 for site in self.platforms)

            def read(row: dict[str, Any]) -> dict[str, Any] | None:
                site = str(row.get("platform") or "")
                if site not in spare or not self._spare(site, self.clock()):
                    raise Deferred("kéréskeret", sites=[site])     # most nem: a következő alkalommal
                return self._look(row)

            return self.hunter.warm(read, self._search if quiet else None, readable=lambda site: site in spare)
        except Exception:  # noqa: BLE001 - (Szünet, keret, letöltési hiba, kilépés: a szál ciklusa nézi) ráér
            return ""

    def _may_work(self) -> bool:
        """Most dolgozhat-e (nincs Szünet, a háttérmunka nincs kikapcsolva vagy felfüggesztve, a gép üzemideje van)."""
        try:
            return bool(self.allowed()) and self.hunter.in_day()
        except Exception:  # noqa: BLE001
            return False

    def _park(self, alert_id: str, site: str, rows: list[dict[str, Any]]) -> None:
        """Egy szüneteltetett figyelő hirdetései a várakozók közé (a folytatásakor jönnek, legfeljebb WAIT_MAX-ig)."""
        self._keep_waiting(alert_id, site, rows)

    def _keep_waiting(self, alert_id: str, site: str, rows: list[dict[str, Any]]) -> None:
        """Hirdetések a várakozók közé (legfeljebb MAX_WAITING; a régebbiek esnek ki, és azokat a figyelő elfelejti).
        A hirdetés wait_for mezője (DealHunter.appraise): melyik oldal kerete kell a folytatásához."""
        now = self.clock()
        with self._lock:
            queued = {entry[2]["url"] for entry in self._waiting}
            for row in rows:
                if row["url"] not in queued:
                    needs = tuple(row.get("wait_for") or ())
                    self._waiting.append((alert_id, site, {k: v for k, v in row.items() if k != "wait_for"}, now,
                                          needs))
            overflow = self._waiting[:max(0, len(self._waiting) - MAX_WAITING)]
            del self._waiting[:len(overflow)]
        self._forget(overflow)

    def _forget(self, entries: list[tuple[str, str, dict[str, Any], float, tuple[str, ...]]]) -> None:
        """Amire nem került sor (túl sok várt, vagy túl régóta): a figyelő nem jegyzi látottnak, így ha még fent van, a
        következő letöltésnél újra új hirdetésként jön (nem vész el)."""
        if not entries:
            return
        with self._lock:
            for entry in entries:
                ident = listing_key(entry[2]["url"])
                (self._data["seen"].get(entry[0]) or {}).pop(ident, None)
                for seen in self._data["seen"].values():      # akiknek a közös lista mérte volna fel
                    old = seen.get(ident)
                    if isinstance(old, list) and len(old) > 3 and old[2] == SEEN_QUEUED and old[3] == entry[0]:
                        seen.pop(ident, None)
            self._save()

    def _go(self, alert_id: str | None = None) -> None:
        """Az értékelés közben (DealHunter.appraise: minden hirdetés letöltése, modellkérdése és a szólás előtt): ha
        közben leállítottad a figyelőt (vagy törölted ezt a figyelőjét, `alert_id`), a hátralévő rész elmarad (_Off); ha
        ezt a figyelőt szünetelteted, Szünetben, kikapcsolt háttérmunkával vagy az üzemidőn kívül megáll (Deferred: a
        hátralévő hirdetések megmaradnak, és a folytatáskor jönnek); kilépéskor kilép."""
        self._check()
        if not self._on(alert_id):
            raise _Off()
        if alert_id is not None and self._paused(alert_id):
            raise Deferred("szünet")
        if not self._may_work():
            raise Deferred("szünet")

    def _on(self, alert_id: str | None = None) -> bool:
        """Be van-e kapcsolva a figyelő (az Indítás után, a Leállításig), és megvan-e még ez a figyelője."""
        with self._lock:
            if not self._data["enabled"]:
                return False
            return alert_id is None or any(a.get("id") == alert_id for a in self._data["alerts"])

    def _paused(self, alert_id: str) -> bool:
        with self._lock:
            return any(a.get("id") == alert_id and a.get("off") for a in self._data["alerts"])

    def _room(self, sites: list[str] | tuple[str, ...]) -> bool:
        """Van-e hely ezeken az oldalakon a figyelő keretében (_has_budget); a kérést a letöltő is ellenőrzi (QUOTA)."""
        now = self.clock()
        return all(self._has_budget(site, now) for site in sites)

    def stop(self) -> None:
        """Leállítja a szálat, és megvárja (legfeljebb pár másodpercig), hogy a mostani letöltését befejezze."""
        self._stop.set()
        for thread in (self._thread, self._worker):
            if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=5)
        try:
            self.hunter.book.save()
        except OSError:
            pass

    def _loop(self) -> None:
        with background_calls():       # a modellhívásai háttérhívások: a feltöltés és a beszélgetés előbb jön
            self._watch()

    def _watch(self) -> None:
        while not self._stop.wait(TICK):
            try:
                self.tick()
            except _Stop:
                return
            except Exception:  # noqa: BLE001 - a figyelő soha ne álljon le egy hiba miatt
                pass

    def _check(self) -> None:
        if self._stop.is_set():
            raise _Stop()

    def _check_on(self) -> None:
        """A letöltő ezt hívja a kérés előtti várakozás közben: a kilépés és a Leállítás is megállítja (_Off)."""
        self._check()
        if not self._on():
            raise _Off()

    def active(self) -> bool:
        """Most dolgozhat-e: be van kapcsolva, van figyelő, nincs Szünet, és a gép üzemideje van."""
        with self._lock:
            if not self._data["enabled"] or not any(not a.get("off") for a in self._data["alerts"]):
                return False
        try:
            return bool(self.allowed()) and self.hunter.in_day()
        except Exception:  # noqa: BLE001
            return False

    def _factor(self, site: str, now: float) -> float:
        """A tanult ritmus szerint mennyiszer a beállított gyakoriság most: 0,5 (csúcsidő: sűrűbben), 2 (csendes óra:
        ritkábban), vagy 1 (átlagos, vagy még kevés az adat)."""
        with self._lock:
            counts = list(self._data["rhythm"].get(site) or [])
        if len(counts) != 24 or sum(counts) < RHYTHM_MIN:
            return 1.0
        hour = time.localtime(now).tm_hour
        here = (counts[hour - 1] + 2 * counts[hour] + counts[(hour + 1) % 24]) / 4   # a szomszéd órák is számítanak
        ratio = here / (sum(counts) / 24)
        return 0.5 if ratio >= PEAK else 2.0 if ratio <= QUIET else 1.0

    def _interval(self, alert: dict[str, Any], site: str, now: float) -> float:
        """A következő letöltésig (másodperc): a beállított gyakoriság a tanult ritmus szerint, de nem sűrűbben, mint
        ahogy a keret egyenletesen elosztva engedi (_floor); csúcsidőben a keret szerinti leggyorsabb ütem (legalább
        PEAK_EVERY perc). Egy kis véletlennel."""
        factor = self._factor(site, now)
        floor = self._floor(site)
        if factor < 1:
            seconds = max(floor, PEAK_EVERY * 60)
        else:
            minutes = min(max(int(alert.get("every") or DEFAULT_EVERY) * factor, EVERY_CHOICES[0]), MAX_EVERY)
            seconds = max(minutes * 60, floor)
        if self._covered(alert, site):         # a közös lista hozza a találatait: külön csak biztonsági hálónak
            seconds = max(seconds, COVERED_EVERY * 60)
        return seconds * self.rng.uniform(*JITTER)

    def _floor(self, site: str) -> float:
        """A legrövidebb idő két letöltés között egy figyelőnél (másodperc): az oldal figyelői (a bekapcsoltak) a keret
        POLL_SHARE részén egyenletesen osztoznak (a közös listából lefedett keresőszavas figyelő COVERED_WEIGHT
        résszel)."""
        with self._lock:
            pairs = sum(COVERED_WEIGHT if self._covered(a, site) else 1.0 for a in self._data["alerts"]
                        if not a.get("off") and site in (a.get("sites") or []))
        return max(1.0, pairs) * 3600 / max(1.0, INSTANT_HOURLY * POLL_SHARE)

    def _learn(self, site: str, fresh: int, now: float) -> None:
        """A ritmushoz: ebben az órában ennyi új (illő) hirdetés jött ezen az oldalon (a hívó menti)."""
        counts = self._data["rhythm"].setdefault(site, [0.0] * 24)
        for hour in range(24):
            counts[hour] = round(counts[hour] * RHYTHM_DECAY, 3)
        counts[time.localtime(now).tm_hour] += fresh

    def due(self, now: float | None = None) -> list[tuple[dict[str, Any], str]]:
        """Az esedékes (figyelő, oldal) párok, a legrégebben esedékes elöl."""
        now = self.clock() if now is None else now
        out = []
        with self._lock:
            for alert in self._data["alerts"]:
                if alert.get("off"):
                    continue
                for site in alert.get("sites") or []:
                    if site not in self.platforms:
                        continue
                    at = self._next.get(f"{alert['id']}:{site}")
                    if at is None:
                        last = float((alert.get("last") or {}).get(site) or 0)
                        at = last + self._interval(alert, site, now)
                        self._next[f"{alert['id']}:{site}"] = at
                    if at <= now:
                        out.append((at, alert, site))
        out.sort(key=lambda entry: entry[0])
        return [(json.loads(json.dumps(alert)), site) for _at, alert, site in out]

    def tick(self) -> int:
        """Egy kör: az esedékes figyelők, amíg az oldal kérhető (a letöltő a kérések között szünetet tart, így oldalanként
        többnyire egy jut egy körre). Visszaadja, hányat nézett."""
        if self.clock() - self._linked_at >= LINK_CHECK:
            self.link_searches()
        if not self.active():
            return 0
        self._retry_waiting()
        now = self.clock()
        done = 0
        for alert, site in self.due(now):
            if not self._has_budget(site, now) or not self.fetcher.ready(site):
                continue
            try:
                self.poll(alert, site)
            except _Off:                       # közben leállítottad: a többi figyelő sem jön
                return done
            done += 1
        return done

    def _has_budget(self, site: str, now: float | None = None) -> bool:
        """Kérhet-e most a figyelő ettől az oldaltól: a saját óránkénti kerete (INSTANT_HOURLY), és a letöltő közös
        keretéből (a körök kérései is ide számítanak) ROUND_RESERVE mindig a köröké marad. A tényleges kéréseket a
        közös letöltő számolja és korlátozza (QUOTA: a lista beállítása miatti plusz kéréseket is)."""
        used = getattr(self.fetcher, "used", None)
        if used is None:
            return True
        hourly = int(getattr(self.fetcher, "hourly", 0) or 0)
        return used(site, QUOTA.tag) < INSTANT_HOURLY and (not hourly or used(site) < hourly - ROUND_RESERVE)

    def _url(self, alert: dict[str, Any], site: str) -> str:
        """A figyelt találati oldal címe, a legújabb hirdetésekkel elöl (deals.NEWEST; a Jófogáson, ha nem adtál meg
        más rendezést)."""
        return newest_first(self._plain_url(alert, site), site)

    def _plain_url(self, alert: dict[str, Any], site: str) -> str:
        if alert.get("url"):
            return str(alert["url"])
        return self.platforms[site].search_url.format(q=urllib.parse.quote(alert["query"]))

    def poll(self, alert: dict[str, Any], site: str) -> list[dict[str, Any]]:
        """Egy figyelő egy oldala: letölti a találatok elejét, és szól az új (vagy leárazott) hirdetésekről. Az első
        letöltésnél csak megjegyzi, ami már fent van. Ha az oldalon csupa új hirdetés van (két letöltés között több
        jött, mint amennyi elfér rajta: lehet, hogy lemaradt valami), a következő oldalt is megnézi (ahogy az oldal
        linkeli). Visszaadja a megérőket; ha az értékelés a saját szálán megy (start), azok később jönnek, és üres."""
        now = self.clock()
        key = f"{alert['id']}:{site}"
        interval = self._interval(alert, site, now)
        with self._lock:
            self._next[key] = now + interval
        base = self._url(alert, site)
        try:
            rows, url = self._fetch_rows(alert, site, base)
        except FeedError as exc:
            self._failed(alert, site, exc)
            return []
        resorted = self.fetcher.sorted_url(url) if hasattr(self.fetcher, "sorted_url") else ""
        # a Hardveraprón a sorrend nem a címben van: a lista beállítása (feeds.SiteFetcher.view) is a cím része itt
        view = self.fetcher.view(site) if hasattr(self.fetcher, "view") else ""
        result = self._absorb(alert, site, rows, now, count=True, resorted=resorted,
                              used=f"{base}#{view}" if view else base, plain=split_offset(self._plain_url(alert, site))[0])
        if result is None:                    # közben törölted
            return []
        first, fresh, overflow = result
        if fresh and not first:                # új jött: a következő letöltés hamarabb (csoportosan jönnek)
            with self._lock:
                self._next[key] = now + max(interval * BURST, self._floor(site), PEAK_EVERY * 60)
        if overflow:
            more = self._next_rows(url, site, now)
            extra = self._absorb(alert, site, more, now, count=False) if more else None
            if extra is not None:
                fresh += extra[1]
        fresh.sort(key=lambda row: bool(row.get("promoted")))         # a kiemeltek a többi új után kerülnek sorra
        if first:
            self.hunter.journal.add(f"Azonnali figyelő: {alert['name']} ({self.platforms[site].name}): elindult, "
                                    f"{len(rows)} hirdetést már ismerek; innentől minden újat felmérek, és ha megéri, "
                                    "szólok." if first != "resort" else
                                    f"Azonnali figyelő: {alert['name']} ({self.platforms[site].name}): "
                                    + (f"a lista beállítása: {view}" if view else
                                       "a legújabb hirdetések szerint rendezve nézem")
                                    + f"; {len(rows)} hirdetést már ismerek.")
            return []
        self.hunter.tally("new", fresh)
        if self._worker is not None and self._worker.is_alive():
            if fresh:
                self._jobs.put((alert, site, fresh))   # az értékelés a saját szálán: a letöltés közben mehet tovább
            return []
        return self._report(alert, site, fresh)

    def _fetch_rows(self, alert: dict[str, Any], site: str, base: str) -> tuple[list[dict[str, Any]], str]:
        """A figyelő oldalának letöltése; ahol lehet (LEAD_SITES), az elején álló kiemelt hirdetések nélkül (lásd
        LEAD_MARGIN). (a hirdetések, a letöltött cím). FeedError, ha nem sikerült."""
        base, given = split_offset(base)
        if site not in LEAD_SITES:
            return self._rows(self.fetcher.listings(base, site, self._check_on, quota=QUOTA), site), base
        lead = int((alert.get("lead") or {}).get(site, given) or 0)
        offset = max(0, lead - LEAD_MARGIN)
        url = with_offset(base, offset)
        rows = self._rows(self.fetcher.listings(url, site, self._check_on, quota=QUOTA), site)
        if offset and rows and not rows[0].get("promoted"):
            # nem kiemelttel kezdődik: a kiemeltek elfogyhattak, és egy új hirdetés az átugrott részbe eshetett
            offset, url = 0, base
            if self._has_budget(site):
                rows = self._rows(self.fetcher.listings(base, site, self._check_on, quota=QUOTA), site)
            else:
                rows = []
        if rows:
            lead = offset + leading_promoted(rows)
        elif offset == 0:
            lead = 0
        with self._lock:
            stored = next((a for a in self._data["alerts"] if a.get("id") == alert["id"]), None)
            if stored is not None:
                stored.setdefault("lead", {})[site] = lead     # (az _absorb menti)
        return rows, url

    def _rows(self, rows: list[dict[str, Any]], site: str) -> list[dict[str, Any]]:
        """A letöltött hirdetések (az elejükből MAX_ROWS, a licitek nélkül); az árbázis is tanul belőlük."""
        name = self.platforms[site].name
        rows = [{"site": name, "platform": site, "text": str(r.get("text") or "")[:180], "price": int(r["price"]),
                 "url": str(r["url"]), "title": str(r.get("title") or r.get("text") or "")[:120],
                 **({"promoted": True} if r.get("promoted") else {}),
                 **({"seller": str(r["seller"])[:40]} if r.get("seller") else {})}
                for r in rows[:MAX_ROWS] if r.get("url") and not r.get("auction")]
        self.hunter.book.add(rows)            # az árbázis is tanul belőle
        self.hunter.tally("seen", rows)
        now = self.clock()
        if now - self._book_saved >= BOOK_SAVE:
            self._book_saved = now
            self.hunter.book.save()
        return rows

    def _absorb(self, alert: dict[str, Any], site: str, rows: list[dict[str, Any]], now: float,
                count: bool, resorted: str = "", used: str = "",
                plain: str = "") -> tuple[bool | str, list[dict[str, Any]], bool] | None:
        """A letöltött hirdetések a figyelő látottjai közé; visszaadja: első letöltés-e (akkor csak megjegyzi őket; „resort”,
        ha a rendezés váltása miatt), az új (vagy leárazott), illő hirdetéseket, és hogy csupa új volt-e az oldal
        (overflow: lehet, hogy lemaradt valami). count: ez a figyelő egy letöltése (nem a következő oldal): a számlálók
        és a ritmus ebből tanul. resorted: ha a letöltő a „legújabb elöl” rendezésre vált (sorted_url), a következő
        letöltés megint csak megjegyzi az oldalt (különben a más sorrend miatt minden újnak látszana). used: a letöltött
        cím; ha más, mint a legutóbbi (pl. egy frissítés óta a legújabb elöl rendezéssel, plain: a régi, rendezés
        nélküli cím), ez a letöltés is csak megjegyzi az oldalt. None, ha a figyelőt közben törölted."""
        fresh: list[dict[str, Any]] = []
        with self._lock:
            stored = next((a for a in self._data["alerts"] if a.get("id") == alert["id"]), None)
            if stored is None:
                return None
            seen = self._data["seen"].setdefault(alert["id"], {})
            first: bool | str = site not in (stored.get("baseline") or [])
            if first and site in (stored.get("resorted") or []):
                first = "resort"
            if used and count:
                urls = stored.setdefault("urls", {})
                if not first and urls.get(site, plain or used) != used:
                    first = "resort"                  # más címről (más sorrendben) jött: most csak megjegyzi
                urls[site] = used
            fresh_rows: list[bool] = []               # a nem kiemelt hirdetések: újak-e (az oldal sorrendjében)
            # ahol az elején álló kiemelteket átugorja (LEAD_SITES), egy (eddig nem látott) kiemelt sem új hirdetés
            skips = site in LEAD_SITES and bool((stored.get("lead") or {}).get(site))
            for row in rows:
                ident = listing_key(row["url"])
                old = seen.get(ident)
                if not row.get("promoted"):
                    fresh_rows.append(old is None)
                if first:
                    seen[ident] = [now, row["price"], SEEN_LISTED]
                    continue
                drop = isinstance(old, list) and len(old) > 1 and row["price"] <= int(old[1]) * (1 - DROP)
                if row.get("promoted") and skips:
                    seen[ident] = self._seen_again(old, now, row["price"])
                    continue                          # a kiemeltek blokkját átugorja: ezek nem új hirdetések
                # új, ami még nem jött; és ami felmérésre várt, de az elmaradt (a közös lista leállt, szünetel, elfelejtette)
                unseen = old is None or self._stale_pending(old, now)
                if (unseen or drop) and self._fits(stored, row):
                    fresh.append({**row, "drop": int(old[1]) if drop else None})
                    seen[ident] = [now, row["price"], SEEN_QUEUED, stored["id"], now]
                elif unseen:
                    seen[ident] = [now, row["price"], SEEN_LISTED]   # nem illik ehhez a figyelőhöz: csak letöltve
                else:
                    seen[ident] = self._seen_again(old, now, row["price"])
            if not first and not stored.get("query"):
                self._share(stored, site, fresh, now)
            elif not first and stored.get("query") and count:
                self._covered_note(stored, site, misses=len(fresh))
            if len(seen) > MAX_SEEN:
                for ident, _value in sorted(seen.items(), key=lambda kv: float(kv[1][0]))[:len(seen) - MAX_SEEN]:
                    seen.pop(ident, None)
            if count:
                stored.setdefault("last", {})[site] = now
                stored["polls"] = int(stored.get("polls") or 0) + 1
                (stored.get("errors") or {}).pop(site, None)
            if first:
                stored["baseline"] = sorted(set(stored.get("baseline") or []) | {site})
                stored["resorted"] = [s for s in stored.get("resorted") or [] if s != site]
            elif fresh:
                self._learn(site, len(fresh), now)
                self._note_delays(site, [row for row in fresh if not row.get("drop") and not row.get("promoted")], now)
            if count and resorted and (stored.get("sorted") or {}).get(site) != resorted:
                stored.setdefault("sorted", {})[site] = resorted      # a következő letöltéstől más a sorrend
                stored["baseline"] = [s for s in stored.get("baseline") or [] if s != site]
                stored["resorted"] = sorted(set(stored.get("resorted") or []) | {site})
            self._save()
        # ha az oldal alja is csupa új (két letöltés között több jött, mint amennyi elfér rajta), lehet, hogy lemaradt
        # valami; a kiemelt hirdetések (elöl, többnyire régiek) ebbe nem számítanak
        overflow = not first and len(fresh_rows) >= OVERFLOW_MIN and all(fresh_rows[-OVERFLOW_TAIL:])
        return first, fresh, overflow

    def _share(self, feed: dict[str, Any], site: str, fresh: list[dict[str, Any]], now: float) -> None:
        """Egy közös lista (keresőszó nélküli figyelő) friss hirdetései a keresőszavas figyelőknek is: csak az, amit a
        közös lista tényleg felmér (fresh: illik a saját szűrőjéhez, pl. az ársávjához); amelyik keresőszavas figyelőhöz
        is illik, annál „felmérésre vár, a közös lista értékeli” lesz (SEEN_QUEUED, owner), így a saját letöltése nem
        méri fel még egyszer, és a lefedettségébe beszámít (_covered). Amit a közös lista nem mér fel (pl. drágább a
        szűrőjénél), azt a keresőszavas figyelő maga méri fel. A ruhára szóló keresőszavast kihagyja (azt a közös lista
        nem méri fel). A hívó tartja a zárat, és ment."""
        for alert in self._data["alerts"]:
            if alert is feed or not alert.get("query") or alert.get("off") or site not in (alert.get("sites") or []) \
                    or site not in (alert.get("baseline") or []) or wearable(str(alert["query"])):
                continue
            seen = self._data["seen"].setdefault(alert["id"], {})
            hits = 0
            for row in fresh:
                ident = listing_key(row["url"])
                old = seen.get(ident)
                if (old is None or self._stale_pending(old, now) or row.get("drop")) and not row.get("promoted") \
                        and self._fits(alert, row):
                    seen[ident] = [now, row["price"], SEEN_QUEUED, feed["id"], now]
                    hits += 1
            if hits:
                self._covered_note(alert, site, hits=hits)

    @staticmethod
    def _seen_again(old: Any, now: float, price: int) -> list[Any]:
        """Egy már ismert hirdetés újra a letöltésben: az ideje és az ára friss, az állapota marad."""
        state = list(old[2:]) if isinstance(old, list) and len(old) > 2 else [SEEN_DONE]
        return [now, price, *state]

    def _stale_pending(self, old: Any, now: float) -> bool:
        """Felmérésre várt, de az elmaradt: PENDING_STALE óta vár, vagy aki felméri, már nincs meg, szünetel, vagy a
        figyelő le van állítva (a hívó tartja a zárat)."""
        if not (isinstance(old, list) and len(old) > 3 and old[2] == SEEN_QUEUED):
            return False
        since = float(old[4]) if len(old) > 4 else float(old[0])
        owner = next((a for a in self._data["alerts"] if a.get("id") == old[3]), None)
        return now - since >= PENDING_STALE or owner is None or bool(owner.get("off"))

    def _mark_done(self, alert_id: str, rows: list[dict[str, Any]]) -> None:
        """Ezeket a hirdetéseket felmérte (ez a figyelő, vagy a közös lista a keresőszavasoknak is): SEEN_DONE."""
        idents = {listing_key(row["url"]) for row in rows}
        with self._lock:
            changed = False
            for owner_id, seen in self._data["seen"].items():
                for ident in idents:
                    entry = seen.get(ident)
                    if isinstance(entry, list) and len(entry) > 3 and entry[2] == SEEN_QUEUED and \
                            (entry[3] == alert_id or owner_id == alert_id):
                        seen[ident] = [entry[0], entry[1], SEEN_DONE]
                        changed = True
            if changed:
                self._save()

    @staticmethod
    def _covered_note(alert: dict[str, Any], site: str, hits: int = 0, misses: int = 0) -> None:
        """A lefedettség: hány friss találata jött a közös listából (hits), és hány csak a saját letöltéséből (misses);
        a régebbiek súlya minden új adatnál COVER_DECAY-szeresére csökken."""
        if not hits and not misses:
            return
        cover = alert.setdefault("cover", {}).setdefault(site, [0.0, 0.0])
        cover[0] = round(cover[0] * COVER_DECAY + hits, 3)
        cover[1] = round(cover[1] * COVER_DECAY + misses, 3)

    def _covered(self, alert: dict[str, Any], site: str) -> bool:
        """Egy keresőszavas figyelő friss találatai rendre a közös listából jönnek-e (lásd COVER_MIN), és az a közös
        lista most is fut (COVER_STALE): egy szünetelő, tiltott vagy rég nem letöltött közös lista nem lassítja a
        saját keresést."""
        hits, misses = ((alert.get("cover") or {}).get(site) or [0.0, 0.0])[:2]
        if not (alert.get("query") and hits >= COVER_MIN and misses <= COVER_MISS * (hits + misses)):
            return False
        now = self.clock()
        with self._lock:
            return bool(self._data["enabled"]) and any(
                not feed.get("query") and not feed.get("off") and site in (feed.get("sites") or [])
                and site in (feed.get("baseline") or []) and not (feed.get("errors") or {}).get(site)
                and now - float((feed.get("last") or {}).get(site) or 0) <= COVER_STALE
                for feed in self._data["alerts"])

    def _note_delays(self, site: str, rows: list[dict[str, Any]], now: float) -> None:
        """Az új hirdetések késése (a kártyán lévő feltöltési idő óta, posted_ago), oldalanként a legutóbbi MAX_DELAYS (a
        hívó menti)."""
        delays = self._data.setdefault("delays", {}).setdefault(site, [])
        for row in rows:
            ago = posted_ago(str(row.get("text") or ""), now)
            if ago is not None:
                delays.append([round(now), round(ago)])
        del delays[:-MAX_DELAYS]

    def delay(self, site: str, now: float | None = None) -> dict[str, Any] | None:
        """Mennyivel a feltöltés után vette észre az új hirdetéseket ezen az oldalon az elmúlt DELAY_WINDOW-ban: {median,
        n} másodpercben; None, ha nincs adat."""
        now = self.clock() if now is None else now
        with self._lock:
            recent = sorted(ago for at, ago in (self._data.get("delays") or {}).get(site) or []
                            if now - float(at) <= DELAY_WINDOW)
        if not recent:
            return None
        return {"median": recent[len(recent) // 2], "n": len(recent)}

    def _next_rows(self, url: str, site: str, now: float) -> list[dict[str, Any]]:
        """A találatok következő oldala (ha az oldal linkeli, és belefér az óránkénti keretbe); üres, ha nem sikerült."""
        following = self.fetcher.next_page(url) if hasattr(self.fetcher, "next_page") else ""
        if not following or not self._has_budget(site, now):
            return []
        try:
            return self._rows(self.fetcher.listings(following, site, self._check_on, quota=QUOTA), site)
        except FeedError:
            return []

    @staticmethod
    def _fits(alert: dict[str, Any], row: dict[str, Any]) -> bool:
        """Illik-e a figyelőhöz: a keresőszó (ha van) minden szava benne van, nincs benne kizáró szó, az ársávban van,
        és nem kereső hirdetés („Keresek…”). Egy keresőszó nélküli (oldalcímes) figyelőnél a ruha és a cipő kimarad:
        azzal nem kereskedik (a ruhára szóló keresőszavad ettől még megy)."""
        if _WANTED_RE.search(fold(row["text"])):
            return False
        if alert.get("query") and not matches_query(alert["query"], row["text"]):
            return False
        if not alert.get("query") and wearable(str(row.get("title") or row["text"])):
            return False
        if excluded(alert.get("exclude") or [], row["text"]):
            return False
        price = int(row["price"])
        return not ((alert.get("min_price") and price < int(alert["min_price"]))
                    or (alert.get("max_price") and price > int(alert["max_price"])))

    def _failed(self, alert: dict[str, Any], site: str, exc: FeedError) -> None:
        name = self.platforms[site].name
        with self._lock:
            for stored in self._data["alerts"]:
                if stored.get("id") == alert["id"]:
                    stored.setdefault("errors", {})[site] = f"{name}: {exc}"[:200]
            self._save()
        if exc.kind != "blocked":
            return
        now = self.clock()
        if now - self._told_blocked.get(site, 0.0) < 86400:
            return
        self._told_blocked[site] = now
        until = self.fetcher.blocked_until(site) if hasattr(self.fetcher, "blocked_until") else 0
        when = time.strftime("%H:%M", time.localtime(until)) if until > now else "egy ideig"
        self.hunter.journal.add(f"Azonnali figyelő: a {name} tilt vagy robot-ellenőrzést kér; {when}-ig nem kérdezem "
                                "(nem próbálom megkerülni).")
        self.notify("deal", f"{name}: az azonnali figyelő szünetel",
                    f"A {name} tiltja vagy ellenőrzi a böngésző nélküli letöltést. {when.capitalize()}-ig nem kérdezem, "
                    "utána újra próbálom; addig a szokásos (böngészős) keresés marad.", actions=[DEALS_ACTION],
                    quiet=True)

    def _report(self, alert: dict[str, Any], site: str, fresh: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Az új hirdetések felmérése (DealHunter.appraise): amelyik megéri, arról rögtön szól (ahogy kiderül), és a friss
        hirdetések közé teszi; ami a modellre vár, az a várakozók közé kerül. Visszaadja a megérőket."""
        source = f"azonnali figyelő: {alert['name']}"
        hits: list[dict[str, Any]] = []

        def found(deal: dict[str, Any]) -> None:
            if not self._on(alert["id"]):      # közben leállítottad (vagy törölted a figyelőt): nem szól
                raise _Off()
            hit = {"id": secrets.token_hex(5), "alert": alert["id"], "alert_name": alert["name"], "at": self.clock(),
                   "deal": deal["id"], "cheap": True,
                   **{k: deal.get(k) for k in ("site", "platform", "text", "title", "price", "url", "drop", "note")},
                   "reference": deal["median"], "profit": deal["profit"], "n": deal.get("similar"),
                   "sites": deal.get("sites") or [], "safety": deal.get("safety"),
                   "warnings": deal.get("warnings") or [], "verified": bool(deal.get("verified")),
                   "unchecked": deal.get("unchecked") or "", "model_pending": bool(deal.get("model_pending")),
                   **{k: deal[k] for k in ("condition_text", "cost_parts", "cond_band") if deal.get(k)}}
            hit["title"] = hit["title"] or hit["text"]
            hits.append(hit)
            with self._lock:
                self._data["hits"] = ([hit] + self._data["hits"])[:MAX_HITS]
                for stored in self._data["alerts"]:
                    if stored.get("id") == alert["id"]:
                        stored["found"] = int(stored.get("found") or 0) + 1
                        stored["found_at"] = self.clock()
                self._save()
            self._tell(alert, hit)
            self._seller(deal)                 # az eladó többi hirdetése: közös vétellel jobban megéri?

        def ping(*args: Any, **extra: Any) -> None:
            if self._on(alert["id"]):          # a gyors jelzés („Ígéretes …, ellenőrzés folyamatban”), ha még fut
                self.notify(*args, **extra)

        # go: Szünetben (vagy ha a kéréskeret elfogyott: _look, _search) az értékelés megáll, a többi megmarad; ha
        # közben leállítottad a figyelőt, a hátralévő rész elmarad (minden letöltés, modellkérdés és szólás előtt nézi)
        try:
            waiting = self.hunter.appraise(fresh, source, self._look, self._search, self.model_ok, found,
                                           go=lambda: self._go(alert["id"]),
                                           wear=wearable(str(alert.get("query") or "")), ping=ping)
        except _Off:
            return hits
        if hasattr(self.hunter, "flush_tips"):
            self.hunter.flush_tips(self.notify)     # a bizonytalan árú tippek („Érdemes megnézni”)
        later = {listing_key(row["url"]) for row in waiting or []}
        self._mark_done(alert["id"], [row for row in fresh if listing_key(row["url"]) not in later])
        self._followups(force=True)                 # a gyors riasztással jelzetteket a modell most nézi meg
        if waiting:
            self._keep_waiting(alert["id"], site, waiting)
        return hits

    def _retry_waiting(self) -> None:
        """A váró friss hirdetések (a modellre, a Szünet vagy a kéréskeret miatt), amint lehet: a modell kérdezhető, és
        mindegyik gyors oldalon van hely a figyelő keretében (az értékelés mindkettőn kereshet). Ami WAIT_MAX-nál
        régebben vár, azt a figyelő elfelejti (_forget: ha még fent van, a következő letöltésnél újra jön)."""
        if not self._waiting:
            return
        now = self.clock()
        with self._lock:
            alerts = {a["id"]: json.loads(json.dumps(a)) for a in self._data["alerts"]}
            expired = [entry for entry in self._waiting if now - entry[3] >= WAIT_MAX]
            self._waiting = [entry for entry in self._waiting if now - entry[3] < WAIT_MAX]
        self._forget(expired)
        if not self.model_ok():
            return
        with self._lock:
            # a szüneteltetett figyelő hirdetései várnak (a folytatásáig, WAIT_MAX-ig), és az is, amelyikhez szükséges
            # oldal kerete még nem szabadult fel (csak a sajátja számít: egy másik oldalé nem akasztja meg)
            ready = [entry for entry in self._waiting
                     if not (alerts.get(entry[0]) or {}).get("off") and self._room(entry[4])]
            taken = {id(entry) for entry in ready}
            self._waiting = [entry for entry in self._waiting if id(entry) not in taken]
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for alert_id, site, row, _at, _needs in ready:
            if alert_id in alerts:
                groups.setdefault((alert_id, site), []).append(row)
        threaded = self._worker is not None and self._worker.is_alive()
        for (alert_id, site), rows in groups.items():
            if threaded:
                self._jobs.put((alerts[alert_id], site, rows))
            else:
                self._report(alerts[alert_id], site, rows)

    def _look(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """Egy hirdetés oldala böngésző nélkül (None, ha most nem sikerült). A figyelő keretéből megy: ha nincs hely
        (vagy Szünet van), Deferred (a hirdetés vár). A jelölt adatlapja (row urgent) a letöltő sorában elsőbbséggel."""
        self._go()
        site = str(row.get("platform") or "")
        if not self._room([site]):
            raise Deferred("kéréskeret", sites=[site])
        try:
            return self.fetcher.page(row["url"], site, self._check_on, quota=QUOTA, urgent=bool(row.get("urgent")))
        except FeedError as exc:
            if exc.kind == "busy":                 # a letöltő szerint sincs hely: a hirdetés vár
                raise Deferred("kéréskeret", sites=[site]) from None
            return None

    def _search(self, query: str) -> list[dict[str, Any]]:
        """Mennyiért adják máshol: a keresőszó találatai a gyors oldalakon (böngésző nélkül), az oldalak egyszerre (a
        letöltő oldalanként egy kérést enged, a szünetekkel). A figyelő keretéből megy (mindkét oldalon): ha nincs hely
        (vagy Szünet van), Deferred (a hirdetés vár)."""
        self._go()
        sites = list(self.platforms)
        if not self._room(sites):
            raise Deferred("kéréskeret", sites=sites)

        def one(site: str, platform: Platform) -> list[dict[str, Any]]:
            try:
                rows = self.fetcher.listings(platform.search_url.format(q=urllib.parse.quote(query)), site,
                                             self._check_on, quota=QUOTA)
            except FeedError as exc:
                if exc.kind == "busy":
                    raise
                return []
            return [{"site": platform.name, "platform": site, "text": str(r.get("text") or "")[:180],
                     "price": int(r["price"]), "url": str(r["url"]),
                     **({"title": str(r["title"])[:160]} if r.get("title") else {}),
                     **({"seller": str(r["seller"])[:40]} if r.get("seller") else {})} for r in rows[:MAX_ROWS]
                    if r.get("url") and not r.get("auction")]

        with ThreadPoolExecutor(max_workers=max(1, len(self.platforms)), thread_name_prefix="jarvis-ar") as pool:
            jobs = [pool.submit(one, site, platform) for site, platform in self.platforms.items()]
            try:
                return [row for job in jobs for row in job.result()]
            except FeedError:                      # a letöltő szerint nincs hely: a hirdetés vár
                raise Deferred("kéréskeret", sites=sites) from None

    def _seller(self, deal: dict[str, Any]) -> None:
        """Egy jó találat eladójának többi hirdetése (a hirdetés oldaláról: seller_url), a figyelő kéréskeretéből: ha
        ezekből is megéri még valami, és együtt több a haszon (DealHunter.seller_bundle), csendesen szól róla."""
        url, site = str(deal.get("seller_url") or ""), str(deal.get("platform") or "")
        if not url or site not in self.platforms or deal.get("auction") or not self._room([site]):
            return
        try:
            listings = self.fetcher.listings(url, site, self._check_on, quota=QUOTA)
        except FeedError:
            return
        name = self.platforms[site].name
        rows = [{"site": name, "platform": site, "text": str(r.get("text") or "")[:180], "price": int(r["price"]),
                 "url": str(r["url"]), **({"title": str(r["title"])[:160]} if r.get("title") else {})}
                for r in listings[:MAX_ROWS] if r.get("url") and not r.get("auction")]
        self.hunter.book.add(rows)
        summary = self.hunter.seller_bundle(deal, rows)
        if summary and summary["worth"]:
            self.notify("deal", f"Ugyanettől az eladótól: {str(deal.get('title') or deal['text'])[:50]}",
                        self.hunter.seller_text(deal, summary), url=url, actions=[deal_action(deal["id"])],
                        deal=deal["id"], quiet=True)

    def _tell(self, alert: dict[str, Any], hit: dict[str, Any]) -> None:
        warn = "".join(f" Figyelj: {w}." for w in hit.get("warnings") or [])
        checked = f" {hit['note']}" if hit.get("note") else ""
        self.notify("deal", "Megéri: " + hit["title"][:60], f"{self._line(hit)}.{checked}{warn} ({alert['name']})",
                    url=hit["url"], actions=[deal_action(hit["deal"])], deal=hit["deal"], urgent=True)

    @staticmethod
    def _line(hit: dict[str, Any]) -> str:
        text = f"{hit['site']}: „{hit['title'][:80]}” {format_price(hit['price'])}, most került fel"
        if hit.get("drop"):
            text += f" (leárazta: {format_price(hit['drop'])} → {format_price(hit['price'])})"
        if hit.get("reference"):
            where = f", {' és '.join(hit['sites'])}" if hit.get("sites") else ""
            basis = f", {hit['n']} hasonló alapján{where}" if hit.get("n") else ""
            text += f". A szokásos ára kb. {format_price(hit['reference'])}{basis}"
            if hit.get("profit", 0) > 0:
                text += f", kb. {format_price(hit['profit'])} haszon lehet"
        if hit.get("condition_text"):
            text += f". Állapot: {hit['condition_text']}"
        extra = [part for part in hit.get("cost_parts") or [] if part.get("what") != "posta"]
        if extra:
            text += ". Levonva: " + ", ".join(f"{part['what']} kb. {format_price(part['amount'])}" for part in extra)
        if hit.get("verified"):
            text += ". Ellenőriztem: az adatlapja és az állapota alapján is megéri"
        elif hit.get("unchecked"):
            text += (f". Az adatlapja és az állapota alapján megéri, de a modell még nem nézte meg ({hit['unchecked']})"
                     + ("; amint lehet, megnézi, és ha mégsem jó, szólok" if hit.get("model_pending") else ""))
        if hit.get("safety"):
            text += f". {safety_text(hit['safety']).rstrip('.')}"
        return text
