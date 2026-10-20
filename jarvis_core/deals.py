"""Automata kereskedés: alulárazott hirdetések keresése a Vinteden, a Jófogáson és a Hardverapróban.

Megadod, mit keresel (pl. „RTX 3060 Ti”, „PS5 kontroller”), és a Jarvis időnként, emberi tempóban megnézi az oldalak
keresőjét (csak olvas, semmire nem kattint, nem vesz és nem ír senkinek).

Szokásos ár: egy új keresésnél először felméri, mennyiért adják most a terméket. A találatokból modell nélkül kiszűri,
ami biztosan nem az (a keresés valamelyik szava hiányzik, kiegészítő, kereső-, csere- vagy csomaghirdetés, alkatrész,
a minimum ár alatti, vagy egy kizáró szó van benne), és a maradékból egyetlen modellhívás választja ki a valódi, működő,
egy darabos termékeket. Az árat nem a modell találja ki: a kiválasztott hirdetések középértéke lesz a javaslat. Erről
szól, és csak akkor keres vele, ha jóváhagytad (vagy kézzel megadod a szokásos árat; akkor felmérés sem kell). Ha a
felmérés közben talál jóval olcsóbbat, arról is szól.

Utána modell nélkül keres, ehhez a rögzített árhoz mérve: jelölt az a még nem látott (vagy azóta leárazott) hirdetés,
ami a megadott %-kal olcsóbb, vagy legalább a megadott forinttal hoz hasznot (a hirdetési ár 90%-áért eladva, a postával
együtt, SALE_RATIO, SHIPPING). A jelölt hirdetés oldalát megnyitja (csak olvasva), és modell nélkül megnézi: elkelt-e
már, van-e benne hibára utaló szó, vagy átverésre utaló jel (előre utalás, külföldről küldi, a Vinteden kívüli
elérhetőség). Csak az átment jelöltekre kérdez rá röviden a modellel. Ami átmegy, arról szól (kind: deal), a hirdetés
linkjével és a várható haszonnal. Ha a hirdetésekben napokig jóval más az ár, mint a jóváhagyott, szól, hogy mérje fel
újra.

Gyorsaság: a találatokat a legújabb elöl nézi. Keresésenként megadható, milyen sűrűn nézze (30 perc – 6 óra); a Vintedet
legfeljebb kétóránként (robot-ellenőrzés). Az egész egy kapcsolóval be- és kikapcsolható (a Munka menü Automata
kereskedés fülén); alapból ki van kapcsolva.
"""
from __future__ import annotations

import json
import math
import os
import queue
import re
import secrets
import threading
import time
import urllib.parse
from collections import Counter, deque
from functools import partial
from pathlib import Path
from typing import Any, Callable

from .browser import DECLINED, LISTING_PAGE_JS, BrowserError, is_risky
from .listings import GONE, SOLD, classify
from .replay import norm_text
from .selling import (_PARTS_RE, Item, ItemError, Platform, _shared, _tokens, format_price, listing_key,
                      parse_price, prefilter, price_stats, round_price)
from .textutil import fold
from .vinted import BUTTON_KINDS, consent_button, elem_key, logged_out, new_elements
from .web import UNTRUSTED_NOTE

# A Vaterán a keresés csak a fix áras („azonnal megvásárolható”) hirdetéseket nézi; a licitekre a licitfigyelő van.
DEAL_SITES = ("vinted", "jofogas", "vatera", "hardverapro")
RESALE_SITES = ("vinted", "jofogas", "hardverapro")   # egy licit viszonteladási árához ezeken keres
MAX_SEARCHES = 20
MAX_PER_SITE = 30          # egy keresés egy oldalán ennyi találatot néz meg (az első oldal)
MIN_PRICES = 5             # az ármozgás figyeléséhez legalább ennyi hasonló ár kell egy körben
DEFAULT_DISCOUNT = 30      # ennyi %-kal a szokásos ár (medián) alatt számít olcsónak
FLOOR = 0.15               # a medián 15%-a alatti ár többnyire nem valódi ár (1 Ft, „ár megegyezés szerint”)
MAX_CHECKS = 5             # egy körben legfeljebb ennyi jelöltet néz meg a modellel (a többi a következő körre marad)
MAX_DEALS = 60             # ennyi találatot őriz meg a listában
MAX_SEEN = 5000
EVERY_CHOICES = (30, 60, 180, 360)   # percben: milyen sűrűn nézi meg egy keresés találatait
DEFAULT_EVERY = 180
SITE_MIN_MINUTES = {"vinted": 120}   # a Vintedet legfeljebb kétóránként (ott a robot-ellenőrzés a kockázat)
# A legújabb hirdetések elöl: a Vinteden külön kérni kell, a Jófogáson és a Hardverapróban ez az alap.
NEWEST = {"vinted": "&order=newest_first"}
MAX_PAGES = 5              # egy körben legfeljebb ennyi jelölt hirdetés oldalát nyitja meg (a többi a következőre)
# A szokásos ár felmérése: legfeljebb ennyi találatot kap a modell, és legalább ennyi hasonlóból mond árat. Ha kevés
# a hasonló, egy nap múlva próbálja újra (addig kézzel megadhatod).
CALIBRATE_ROWS = 25
CALIBRATE_MIN = 4
CALIBRATE_RETRY = 86400
# Várható haszon: a hirdetési (kínálati) ár többnyire többet mond, mint amennyiért ténylegesen elkel (kb. 10%-kal), és
# a postázás (csomagautomata, 1–2 ezer Ft) is a tiéd.
SALE_RATIO = 0.9
SHIPPING = 1500
DROP = 0.05                # egy már látott hirdetés legalább ennyivel olcsóbb lett: leárazás, újra jelölt lehet
DRIFT = 0.2                # ha a hirdetésekben ennyivel más az ár, mint a jóváhagyott…
DRIFT_DAYS = 3             # …ennyi napig, szól, hogy mérje fel újra
MAX_EXCLUDE = 12
DEALS_ACTION = {"action": "deals", "label": "Megnézem"}   # az értesítés gombja: az Automata kereskedés fül
# Vinted-ajánlat: ha a kért ár kicsit több annál, ami még megéri, ajánlatot javasol; legfeljebb ennyivel a kért ár
# alatt (ennél nagyobb engedményt ritkán fogadnak el).
OFFER_CUT = 0.2
# Vatera-licitfigyelő: alapból óránként nézi, a zárás előtt legfeljebb ennyi órával szól (hogy legyen időd licitálni),
# legalább ennyi haszonnal; egy oldalról legfeljebb EXPLORE_ROWS ismeretlen licitet kap a modell, és ezekből legfeljebb
# EXPLORE_PICKS-nek nézi meg az árát máshol (RESALE_SITES).
MAX_WATCHES = 10
WATCH_EVERY = 60
WATCH_HOURS = 6
WATCH_PROFIT = 3000
EXPLORE_ROWS = 15
EXPLORE_PICKS = 3
_OFFER_OPEN_RE = re.compile(r"^(ajanlattetel|ajanlat tetele|ajanlatot teszek|ajanlat|make an offer|make offer|offer)$")
_OFFER_SEND_RE = re.compile(r"^(ajanlat kuldese|ajanlat elkuldese|ajanlatot kuld\w*|ajanlatot teszek|ajanlattetel|"
                            r"ajanlat tetele|kuldes|elkuldes|send offer|send|make offer|offer|ajanlok)$")
_OFFER_BOX_RE = re.compile(r"(ar|ajanlat|price|offer|osszeg|amount|ft)")
_PRICE_KINDS = {"input:number", "input:text", "input:tel", "textbox"}
PAGE_TEXT = 2000           # a hirdetés oldalának eleje (a cím, a leírás, az eladó); a lejjebb lévő ajánlók nem
# Átverésre utaló jel (ékezet nélkül): előre fizetés, külföldről küldés, pénzküldő szolgáltatás, ajándékkártya.
_SCAM_RE = re.compile(r"\b(csak (elore )?utal\w*|elore (kell )?utal\w*|utalas elore|elore fizet\w*|western union|"
                      r"moneygram|paypal (baratnak|friends)|kulfoldon (vagyok|elek|dolgozom)|kulfoldrol (kuldom|postazom)|"
                      r"ajandekkartya|gift ?card)\b")
# A Vinteden kívüli elérhetőség: ott a fizetés és a vevővédelem az oldalon megy, a máshová hívás átverés jele.
_OFFSITE_RE = re.compile(r"\b(whatsapp\w*|telegram\w*|viber\w*)\b|[\w.+-]+@[\w-]+\.[a-z]{2,}")
# Hibára utaló szó a leírásban (a „nem hibás”, „nem törött” nem számít).
_DEFECT_RE = re.compile(r"(?<!nem )\b(repedt|repedes\w*|beegett|beeges|nem kapcsol be|nem indul|javitasra szorul|"
                        r"hianyos|lyukas|szakadt|foltos)\b")
# Ugyanannak a dolognak más szavai (ékezet nélkül): a keresés minden szavának meg kell lennie a hirdetésben, de
# „kontroller” helyett „DualSense”, „videókártya” helyett „VGA” is jó.
SYNONYMS = {
    "kontroller": ("controller", "kontroler", "dualsense", "dualshock", "joystick", "gamepad"),
    "controller": ("kontroller", "kontroler", "dualsense", "dualshock", "joystick", "gamepad"),
    "videokartya": ("vga", "gpu", "grafikus"), "vga": ("videokartya", "gpu"),
    "ps5": ("playstation",), "ps4": ("playstation",), "laptop": ("notebook",), "notebook": ("laptop",),
    "telefon": ("mobil", "mobiltelefon", "okostelefon"), "fulhallgato": ("headset", "fejhallgato"),
    "fejhallgato": ("headset", "fulhallgato"),
}
# Kiegészítő a keresett dologhoz (töltő, tok, kábel…), vagy „-hoz/-hez” alak („kontrollerhez”): nem maga a termék.
_ACCESSORY_RE = re.compile(r"\b(tolto\w*|dokkolo\w*|tok|tokja|vedotok\w*|borito\w*|skin|matrica\w*|kabel\w*|"
                           r"allvany\w*|tarto|szilikon\w*|grip\w*|csere ?gomb\w*|ujjvedo\w*)\b")
_NO_REVIEWS_RE = re.compile(r"nincs(enek)? (meg )?ertekeles\w*|meg nincs ertekeles\w*|no reviews yet|\b0 ertekeles")

CHECK_SYSTEM = (
    "Egy használtcikk-kereskedőnek segítesz. Megkapod, mit keres, és egy hirdetés címét, szövegét és árát. "
    "Döntsd el, megéri-e megnézni: ok=true, ha ez tényleg a keresett termék (nem kiegészítő, nem alkatrész, nem "
    "más típus), egy darab, eladó (nem keres vagy cserél), és nem hibás vagy hiányos. reason: egy rövid mondat "
    "magyarul. " + UNTRUSTED_NOTE)
JUDGE_SYSTEM = (
    "Egy használtcikk-kereskedőnek segítesz felmérni, mennyiért adják most a keresett terméket. Megkapod a keresett "
    "terméket és hirdetéseket (számozva, árral). Válaszd ki azokat, amelyek pontosan a keresett termék: ugyanaz a "
    "típus, egy darab, működő, eladó. Ne válaszd a kiegészítőt (tok, töltő, kábel), a más típust vagy kiadást, az "
    "alkatrészt, a hibásat, a csomagot és a keresőhirdetést. Árat ne becsülj, csak a sorszámokat add meg. reason: egy "
    "rövid mondat magyarul. " + UNTRUSTED_NOTE)
JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"similar": {"type": "array", "items": {"type": "integer"}}, "reason": {"type": "string"}},
    "required": ["similar", "reason"],
}
EXPLORE_SYSTEM = (
    "Egy használtcikk-kereskedőnek segítesz a Vatera licitjei között. Megkapod a hamarosan záruló licitek címét, mostani "
    "árát és hátralévő idejét (számozva). Válaszd ki legfeljebb hármat, ami egy viszonteladónak megérhet: ismert "
    "márkájú, keresett, működőnek látszó termék, aminek a mostani ára jóval a szokásos ára alatt lehet. Mindegyikhez "
    "adj egy rövid keresőszót (márka és típus, 2–4 szó), amivel máshol megnézhetem, mennyiért adják. Ha egyik sem "
    "ilyen, üres lista. reason: egy rövid mondat magyarul. " + UNTRUSTED_NOTE)
EXPLORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"picks": {"type": "array", "items": {"type": "object", "properties": {
        "n": {"type": "integer"}, "query": {"type": "string"}}, "required": ["n", "query"]}},
        "reason": {"type": "string"}},
    "required": ["picks", "reason"],
}
CHECK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["ok", "reason"],
}


class DealError(ItemError):
    pass


_LEFT_UNITS = {"nap": 86400, "ora": 3600, "perc": 60}


def parse_auction(text: str) -> dict[str, int | None]:
    """Egy licit hátralévő ideje (másodperc) és a licitek száma a kártya szövegéből („2 nap 3 óra”, „5 óra 12 perc”,
    „3 licit”, „Licitek: 3”); amit nem talál, None."""
    folded = fold(text)
    parts = re.findall(r"(\d+)\s*(nap|ora|perc)\b", folded)
    bids = re.search(r"(\d+)\s*licit|licit(?:ek)?(?: szama)?:?\s*(\d+)", folded)
    return {"left": sum(int(n) * _LEFT_UNITS[unit] for n, unit in parts) if parts else None,
            "bids": int(bids.group(1) or bids.group(2)) if bids else None}


def left_text(seconds: int | None) -> str:
    """„2 nap 3 óra”, „5 óra 12 perc”, „40 perc”."""
    if seconds is None:
        return "nem tudom, mikor"
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = [f"{days} nap"] if days else []
    if hours:
        parts.append(f"{hours} óra")
    if minutes and not days:
        parts.append(f"{minutes} perc")
    return " ".join(parts) or "kevesebb mint egy perc"


def pays(search: dict[str, Any], price: int, reference: int) -> bool:
    """Megéri-e: a szokásos árnál a megadott %-kal olcsóbb, vagy legalább a megadott haszon jön ki belőle (és
    egyáltalán hoz hasznot); nem gyanúsan olcsó (FLOOR), és a megadott ársávban van."""
    if price < reference * FLOOR:
        return False
    if (search.get("min_price") and price < search["min_price"]) or \
            (search.get("max_price") and price > search["max_price"]):
        return False
    profit = expected_profit(reference, price)
    if profit <= 0:
        return False
    by_percent = price <= reference * (100 - int(search.get("discount") or DEFAULT_DISCOUNT)) / 100
    return by_percent or bool(search.get("min_profit") and profit >= int(search["min_profit"]))


def highest_ok(search: dict[str, Any], reference: int) -> int | None:
    """A legmagasabb ár, ami ennél a keresésnél még megéri (pays): ennyit érdemes ajánlani vagy licitálni. None, ha a
    megadott ársávban nincs ilyen. Az ár csökkenésével csak jobb lesz, ezért felezéssel keresi meg a határt."""
    low = max(math.ceil(reference * FLOOR), int(search.get("min_price") or 0), 1)
    high = int(search.get("max_price") or reference)
    if high < low or not pays(search, low, reference):
        return None
    while low < high:
        middle = (low + high + 1) // 2
        if pays(search, middle, reference):
            low = middle
        else:
            high = middle - 1
    return low


def offer_price(search: dict[str, Any], price: int, reference: int) -> int | None:
    """Vinted-ajánlat: ha a kért ár kicsit több annál, ami még megéri (highest_ok), de legfeljebb OFFER_CUT-tal, ennyit
    ajánlj (kerek százasra lefelé). None, ha a kért ár már most is megéri, vagy túl nagy engedmény kellene."""
    top = highest_ok(search, reference)
    if top is None or price <= top:
        return None
    offer = top // 100 * 100
    return offer if offer > 0 and offer >= price * (1 - OFFER_CUT) else None


class ModelLane:
    """Modell-sor: egy háttérszál, ami a modellfeladatokat egyenként végzi (egyszerre mindig csak egy modellhívás), amíg a
    böngésző a következő oldalt tölti. Az eredményt nem ő dolgozza fel: a kész feladat folytatása (then) a böngésző
    szálán fut le (deliver), mert onnan kell továbbmenni (pl. egy hirdetés oldalát megnyitni, értesíteni)."""

    def __init__(self) -> None:
        self._tasks: queue.Queue = queue.Queue()
        self._done: queue.Queue = queue.Queue()
        self._pending = 0
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="jarvis-modell-sor", daemon=True)
        self._thread.start()

    def submit(self, call: Callable[[], Any], then: Callable[[Any], None]) -> None:
        with self._lock:
            self._pending += 1
        self._tasks.put((call, then))

    def _loop(self) -> None:
        while not self._closed.is_set():
            try:
                call, then = self._tasks.get(timeout=0.2)
            except queue.Empty:
                continue
            if self._closed.is_set():
                break
            try:
                result = call()
            except Exception:  # noqa: BLE001 - a hívások maguk kezelik a hibát; ami mégis kijön, az „nincs válasz”
                result = None
            self._done.put((then, result))

    def deliver(self, wait: float = 0.0) -> bool:
        """Egy kész modellfeladat folytatása a hívó (böngésző) szálán. Visszaadja, volt-e ilyen."""
        try:
            then, result = self._done.get(timeout=wait) if wait else self._done.get_nowait()
        except queue.Empty:
            return False
        with self._lock:
            self._pending -= 1
        then(result)
        return True

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._pending > 0

    def close(self) -> None:
        """A kör vége (vagy megszakadt): a szál a mostani hívása után kilép, a maradék feladat elmarad."""
        self._closed.set()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()


class _Round:
    """Egy kör állapota: a böngésző szálán sorra kerülő lépések (work), a modell-sor, a keret (hány hirdetés oldalát
    nyithatja meg, hányszor kérdezheti a modellt), és az összefoglaló."""

    def __init__(self, check: Callable[[], None], notify: Callable[..., None], lane: ModelLane | None) -> None:
        self.check, self.notify, self.lane = check, notify, lane
        self.work: deque[Callable[[], None]] = deque()
        self.pages, self.checks, self.resale, self.blocked = MAX_PAGES, MAX_CHECKS, EXPLORE_PICKS, False
        self.found = self.done = 0
        self.notes: list[str] = []
        self.errors: list[str] = []

    def next(self, steps: list[Callable[[], None]]) -> None:
        """Ezek jönnek rögtön (pl. egy keresés jelöltjei a következő keresés előtt)."""
        self.work.extendleft(reversed(steps))

    def ask(self, call: Callable[[], Any], then: Callable[[Any], None]) -> None:
        """Modellfeladat a modell-sorba; modell nélkül rögtön a folytatás jön, „nincs válasz” (None) eredménnyel."""
        if self.lane is None:
            then(None)
        else:
            self.lane.submit(call, then)

    def loop(self) -> None:
        """A böngésző szála: a lépések sorban, közben a kész modellfeladatok folytatása; addig, amíg van mit tenni."""
        while True:
            self.check()
            if self.lane is not None and self.lane.deliver():
                continue
            if self.work:
                self.work.popleft()()
                continue
            if self.lane is not None and self.lane.busy:
                self.lane.deliver(wait=0.3)
                continue
            return


def matches_query(query: str, text: str) -> bool:
    """A hirdetés tényleg a keresett dolog-e: a keresés minden szava (vagy a rokon szava, SYNONYMS) benne van; négy
    szónál hosszabb keresésnél egy hiányozhat. Így a „PS5 kontroller” keresésre a PS5-játék nem jelölt."""
    wanted = _tokens(query)
    have = _tokens(text)
    if not wanted:
        return True
    hits = sum(1 for word in wanted if _shared({word, *SYNONYMS.get(word, ())}, have))
    if hits < (len(wanted) if len(wanted) <= 4 else len(wanted) - 1):
        return False
    folded = fold(query)
    accessory = [m.group(0) for m in _ACCESSORY_RE.finditer(fold(text)) if m.group(0) not in folded]
    for_it = [w for w in have if w.endswith(("hoz", "hez")) and any(w.startswith(q) and w != q for q in wanted)]
    return not accessory and not for_it


def expected_profit(reference: int, price: int) -> int:
    """Várható haszon, ha megveszed, és a szokásos áron hirdeted: a hirdetési árnál kicsit olcsóbban kel el
    (SALE_RATIO), és a postázás is a tiéd (SHIPPING)."""
    return int(round(reference * SALE_RATIO - price - SHIPPING))


def parse_words(value: Any) -> list[str]:
    """Kizáró szavak („tok, töltő, játék” vagy lista): kisbetűvel, ahogy beírtad, legfeljebb MAX_EXCLUDE (az
    ékezet csak az egyezésnél nem számít, lásd excluded)."""
    parts = re.split(r"[,;\n]+", value) if isinstance(value, str) else \
        [str(v) for v in value] if isinstance(value, (list, tuple)) else []
    out: list[str] = []
    for part in parts:
        word = " ".join(part.lower().split())[:30]
        if word and fold(word) not in {fold(w) for w in out}:
            out.append(word)
    return out[:MAX_EXCLUDE]


def excluded(words: list[str], text: str) -> bool:
    """Van-e a hirdetésben kizáró szó, ékezet nélkül is (a szó eleje számít: „tok” → „tokkal”, „tokot” is)."""
    folded = fold(text)
    return any(re.search(rf"\b{re.escape(fold(word))}", folded) for word in words)


def search_status(search: dict[str, Any]) -> str:
    """fut: van jóváhagyott szokásos ár; javaslat: jóváhagyásra vár; kevés: nem talált elég hasonlót (kézzel kell);
    felmérés: a következő körben felméri."""
    if search.get("reference"):
        return "fut"
    proposal = search.get("proposal") or {}
    if proposal.get("price"):
        return "javaslat"
    return "kevés" if proposal else "felmérés"


def profit_report(items: list[Item], now: float | None = None) -> dict[str, Any]:
    """Haszonkövetés a vételárral megjelölt tárgyakból (Megvettem, vagy a tárgy lapján a Vételár): az eladottak haszna
    (eladási ár − vételár) ebben a hónapban és összesen, átlagosan hány nap alatt kelt el, és mennyi pénz van még
    bent a meg nem eladott tárgyakban."""
    now = time.time() if now is None else now
    month = time.localtime(now)[:2]
    rows, total, this_month, days = [], [0, 0, 0, 0], [0, 0], []   # haszon, darab, vételár, bevétel
    open_count, open_spent = 0, 0
    for item in items:
        if item.bought_price is None:
            continue
        row: dict[str, Any] = {"id": item.id, "name": item.name, "bought_price": item.bought_price,
                               "sold": item.sold, "sold_price": item.sold_price, "profit": None, "days": None}
        if item.sold and item.sold_price is not None:
            profit = item.sold_price - item.bought_price
            row["profit"] = profit
            total[0] += profit
            total[1] += 1
            total[2] += item.bought_price
            total[3] += item.sold_price
            if item.sold_at and item.bought_at:
                row["days"] = max(0, int((item.sold_at - item.bought_at) // 86400))
                days.append(row["days"])
            if item.sold_at and time.localtime(item.sold_at)[:2] == month:
                this_month[0] += profit
                this_month[1] += 1
        elif not item.sold:
            open_count += 1
            open_spent += item.bought_price
        rows.append(row)
    rows.sort(key=lambda r: (r["sold"], r["name"]))
    return {"month": {"profit": this_month[0], "count": this_month[1]},
            "total": {"profit": total[0], "count": total[1], "spent": total[2], "revenue": total[3]},
            "open": {"count": open_count, "spent": open_spent},
            "days": round(sum(days) / len(days)) if days else None, "rows": rows}


class DealHunter:
    """A keresések, a kapcsoló és a talált olcsó hirdetések (munka/kereskedes.json)."""

    def __init__(self, root: Path, platforms: dict[str, Platform], browser: Callable[[], Any] | None,
                 llm: Any = None, clock: Callable[[], float] = time.time, default_every: float = DEFAULT_EVERY):
        self.path = Path(root) / "kereskedes.json"
        self.platforms = {k: p for k, p in platforms.items() if k in DEAL_SITES and p.search_url}
        self.browser = browser
        self.llm = llm
        self.clock = clock
        self.default_every = default_every    # az új keresések gyakorisága ([scheduler] deal_minutes)
        self.offering = ""                    # ajánlattétel közben a jóváhagyó ablak első sora (WorkDesk.confirm_context)
        self._lock = threading.RLock()
        self._data = self._load()

    # --- tárolás ---

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        numbers = lambda key: {str(k): float(v) for k, v in (data.get(key) or {}).items()  # noqa: E731
                               if isinstance(v, (int, float))}
        return {"enabled": bool(data.get("enabled")),
                "searches": [s for s in data.get("searches") or [] if isinstance(s, dict) and s.get("query")],
                "watches": [w for w in data.get("watches") or [] if isinstance(w, dict) and w.get("url")],
                "deals": [d for d in data.get("deals") or [] if isinstance(d, dict) and d.get("url")],
                "seen": numbers("seen"),       # hirdetés → mikor néztük meg utoljára (egy keresés ehhez igazodik)
                "prices": {k: int(v) for k, v in numbers("prices").items()},   # hirdetés → az ára akkor (leárazás)
                "last": numbers("last")}       # keresés:oldal → mikor néztük meg (gyakoriság)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def state(self) -> dict[str, Any]:
        """A felületnek: a kapcsoló, a keresések (az állapotukkal) és a talált hirdetések (a legújabb elöl)."""
        with self._lock:
            deals = [d for d in self._data["deals"] if not d.get("dismissed")]
            searches = [{**json.loads(json.dumps(s)), "status": search_status(s)} for s in self._data["searches"]]
            return {"enabled": self._data["enabled"], "searches": searches,
                    "watches": json.loads(json.dumps(self._data["watches"])),
                    "deals": sorted((dict(d) for d in deals), key=lambda d: -float(d.get("at") or 0)),
                    "sites": [{"key": k, "name": p.name} for k, p in self.platforms.items()],
                    "browser": self.browser is not None, "sale_ratio": SALE_RATIO, "shipping": SHIPPING}

    def available(self) -> bool:
        with self._lock:
            return (self._data["enabled"] and bool(self._data["searches"] or self._data["watches"])
                    and self.browser is not None)

    def set_enabled(self, on: bool) -> dict[str, Any]:
        with self._lock:
            self._data["enabled"] = bool(on)
            self._save()
        return self.state()

    # --- keresések ---

    @staticmethod
    def _amount(value: Any, what: str) -> int | None:
        """Egy forintösszeg a felületről („52000”, „52 000 Ft”, „ötvenezer”); üresen None."""
        try:
            amount = parse_price(value)
        except ItemError:
            raise DealError(f"{what}: nem értem ezt az összeget.") from None
        return amount or None

    def _clean(self, fields: dict[str, Any]) -> dict[str, Any]:
        """A keresés beállításai ellenőrizve (a hozzáadáshoz és a módosításhoz)."""
        out: dict[str, Any] = {}
        if "query" in fields:
            out["query"] = " ".join(str(fields["query"] or "").split())[:80]
            if not out["query"]:
                raise DealError("Írd be, mit keressek (pl. „RTX 3060 Ti”).")
        if "sites" in fields:
            sites = fields["sites"] if isinstance(fields["sites"], (list, tuple)) else []
            out["sites"] = [s for s in sites if s in self.platforms] or list(self.platforms)
        for key, what in (("max_price", "A legmagasabb ár"), ("min_price", "A legalacsonyabb ár"),
                          ("min_profit", "A legkisebb haszon"), ("reference", "A szokásos ár")):
            if key in fields:
                out[key] = self._amount(fields[key], what)
        if "discount" in fields:
            try:
                off = int(fields["discount"]) if fields["discount"] not in (None, "") else DEFAULT_DISCOUNT
            except (TypeError, ValueError):
                raise DealError("A kedvezmény egész szám legyen (pl. 30).") from None
            out["discount"] = min(max(off, 10), 80)
        if "every" in fields:
            try:
                minutes = int(fields["every"]) if fields["every"] not in (None, "") else int(self.default_every)
            except (TypeError, ValueError):
                minutes = DEFAULT_EVERY
            out["every"] = min(EVERY_CHOICES, key=lambda choice: abs(choice - minutes))
        if "exclude" in fields:
            out["exclude"] = parse_words(fields["exclude"])
        return out

    def add_search(self, query: str, sites: list[str] | None = None, max_price: Any = None, discount: Any = None,
                   every: Any = None, min_price: Any = None, min_profit: Any = None, exclude: Any = None,
                   reference: Any = None) -> dict[str, Any]:
        """Új keresés. Ha megadod a szokásos árat (reference), azzal keres; különben a következő körben felméri."""
        search = self._clean({"query": query, "sites": sites or list(self.platforms), "max_price": max_price,
                              "discount": discount, "every": every, "min_price": min_price, "min_profit": min_profit,
                              "exclude": exclude or [], "reference": reference})
        self._check_range(search)
        if search.get("reference"):
            now = self.clock()
            search.update(ref_source="kézi", ref_at=now, checked_from=now)
        else:
            search.pop("reference", None)
        with self._lock:
            if len(self._data["searches"]) >= MAX_SEARCHES:
                raise DealError(f"Legfeljebb {MAX_SEARCHES} keresés lehet.")
            self._data["searches"].append({"id": secrets.token_hex(4), **search})
            self._save()
        return self.state()

    @staticmethod
    def _check_range(search: dict[str, Any]) -> None:
        low, high = search.get("min_price"), search.get("max_price")
        if low and high and low > high:
            raise DealError("A legalacsonyabb ár nagyobb, mint a legmagasabb.")

    def update_search(self, search_id: str, **fields: Any) -> dict[str, Any]:
        """Egy keresés beállításainak módosítása. A szokásos árat az accept állítja (vagy a recalibrate törli)."""
        fields.pop("reference", None)
        clean = self._clean(fields)
        with self._lock:
            search = self._search(search_id)
            merged = {**search, **clean}
            self._check_range(merged)
            if clean.get("query") and fold(clean["query"]) != fold(search["query"]):
                for key in ("reference", "ref_source", "ref_at", "checked_from", "basis", "proposal", "market"):
                    merged.pop(key, None)       # más termék: a szokásos árat újra fel kell mérni
            search.clear()
            search.update(merged)
            self._reset_due(search)
            self._save()
        return self.state()

    def accept(self, search_id: str, price: Any = None) -> dict[str, Any]:
        """A szokásos ár: a javaslat elfogadása, vagy (price) kézzel megadva. Innen ezzel keres; az első kör minden
        mostani hirdetést megnéz (nem csak az újakat)."""
        now = self.clock()
        with self._lock:
            search = self._search(search_id)
            amount = self._amount(price, "A szokásos ár") if price not in (None, "") else None
            if amount:
                search.update(reference=amount, ref_source="kézi", ref_at=now, checked_from=now)
                search.pop("basis", None)
            else:
                proposal = search.get("proposal") or {}
                if not proposal.get("price"):
                    raise DealError("Nincs még javaslat: add meg kézzel a szokásos árat.")
                search.update(reference=int(proposal["price"]), ref_source="felmérés", ref_at=now,
                              checked_from=float(proposal.get("at") or now),
                              basis={k: proposal.get(k) for k in ("n", "p25", "p75", "at")})
            search.pop("proposal", None)
            search.pop("market", None)
            self._reset_due(search)
            self._save()
        return self.state()

    def recalibrate(self, search_id: str) -> dict[str, Any]:
        """Újra felméri a szokásos árat (a következő körben); addig ezzel a kereséssel nem keres."""
        with self._lock:
            search = self._search(search_id)
            for key in ("reference", "ref_source", "ref_at", "checked_from", "basis", "proposal", "market"):
                search.pop(key, None)
            self._reset_due(search)
            self._save()
        return self.state()

    def remove_search(self, search_id: str) -> dict[str, Any]:
        with self._lock:
            self._data["searches"] = [s for s in self._data["searches"] if s.get("id") != search_id]
            self._save()
        return self.state()

    def add_watch(self, target: str, min_profit: Any = None, hours: Any = None, every: Any = None) -> dict[str, Any]:
        """Vatera-licitfigyelő: egy Vatera-találati oldal címe (pl. egy kategória „hamarosan lejár” rendezéssel), vagy
        keresőszó. Nem kell megadni, mit keresel: a hamarosan záruló licitekből a modell választ, és megnézi, máshol
        mennyiért adják."""
        target = " ".join(str(target or "").split())[:300]
        site = self.platforms.get("vatera")
        if not target or site is None:
            raise DealError("Add meg a Vatera-oldal címét (a böngésző címsorából), vagy egy keresőszót.")
        if re.match(r"https?://", target):
            host = (urllib.parse.urlsplit(target).hostname or "").removeprefix("www.")
            if host != "vatera.hu" and not host.endswith(".vatera.hu"):
                raise DealError("Ez nem Vatera-oldal; másold be a Vatera találati oldalának címét.")
            url, name = target, "Vatera-oldal"
        else:
            url, name = site.search_url.format(q=urllib.parse.quote(target)), target[:80]
        try:
            profit = self._amount(min_profit, "A legkisebb haszon") or WATCH_PROFIT
            window = min(max(int(hours or WATCH_HOURS), 1), 72)
            minutes = min(EVERY_CHOICES, key=lambda choice: abs(choice - int(every or WATCH_EVERY)))
        except (TypeError, ValueError):
            raise DealError("A zárásig hátralévő órák és a gyakoriság egész szám legyen.") from None
        with self._lock:
            if len(self._data["watches"]) >= MAX_WATCHES:
                raise DealError(f"Legfeljebb {MAX_WATCHES} licitfigyelő lehet.")
            self._data["watches"].append({"id": secrets.token_hex(4), "name": name, "url": url, "min_profit": profit,
                                          "hours": window, "every": minutes})
            self._save()
        return self.state()

    def remove_watch(self, watch_id: str) -> dict[str, Any]:
        with self._lock:
            self._data["watches"] = [w for w in self._data["watches"] if w.get("id") != watch_id]
            self._save()
        return self.state()

    def _search(self, search_id: str) -> dict[str, Any]:
        for search in self._data["searches"]:
            if search.get("id") == search_id:
                return search
        raise DealError("Ez a keresés már nincs meg.")

    def _set(self, search_id: str, **fields: Any) -> None:
        """Egy keresés mezői a kör közben (javaslat, ármozgás); ha közben törölted, nem csinál semmit."""
        with self._lock:
            for search in self._data["searches"]:
                if search.get("id") == search_id:
                    search.update(fields)
                    self._save()

    def _reset_due(self, search: dict[str, Any]) -> None:
        for key in self.platforms:
            self._data["last"].pop(f"{search['id']}:{key}", None)

    # --- találatok ---

    def get(self, deal_id: str) -> dict[str, Any]:
        with self._lock:
            for deal in self._data["deals"]:
                if deal.get("id") == deal_id:
                    return dict(deal)
        raise DealError("Ez a találat már nincs meg.")

    def mark_bought(self, deal_id: str, item_id: str) -> dict[str, Any]:
        """Megvetted: a találatból eladási tárgy lett (item_id); a listában jelölve marad."""
        with self._lock:
            for deal in self._data["deals"]:
                if deal.get("id") == deal_id:
                    deal["bought"] = item_id
            self._save()
        return self.state()

    def dismiss(self, deal_id: str) -> dict[str, Any]:
        with self._lock:
            for deal in self._data["deals"]:
                if deal.get("id") == deal_id:
                    deal["dismissed"] = True
            self._save()
        return self.state()

    # --- a keresés (háttérmunka) ---

    def every_minutes(self) -> float:
        """A háttérmunka gyakorisága: a leggyakoribb keresésé (a többi csak akkor fut, ha esedékes, lásd _due)."""
        with self._lock:
            jobs = [int(s.get("every") or DEFAULT_EVERY) for s in self._data["searches"]]
            jobs += [int(w.get("every") or WATCH_EVERY) for w in self._data["watches"]]
            if not self._data["enabled"] or not jobs:
                return 0.0
            return float(min(jobs))

    def _due(self, search: dict[str, Any], key: str, now: float) -> bool:
        every = max(int(search.get("every") or DEFAULT_EVERY), SITE_MIN_MINUTES.get(key, 0))
        last = float(self._data["last"].get(f"{search['id']}:{key}") or 0)
        return now - last >= every * 60 - 90     # másfél perc ráhagyás: az ütemező percre pontosan nem indít

    def run(self, check: Callable[[], None] = lambda: None,
            notify: Callable[..., None] = lambda *a, **k: None, force: bool = False) -> str:
        """Egy kör: az esedékes keresések (force: mind) oldalain az első találati oldal, a legújabb elöl, modell
        nélkül. Ha egy keresésnek még nincs szokásos ára, felméri (egy modellhívás), különben ahhoz méri a
        hirdetéseket. A Vatera-licitfigyelők a hamarosan záruló liciteket nézik. A jelöltek oldalát megnyitja (csak
        olvasva), és a megmaradtakra egy rövid modellkérdés. A modellhívások a modell-sorban mennek (ModelLane),
        egyenként, miközben a böngésző a következő oldalt tölti. Visszaadja, mit talált."""
        with self._lock:
            if not self._data["enabled"]:
                return "Az automata kereskedés ki van kapcsolva."
            searches = json.loads(json.dumps(self._data["searches"]))
            watches = json.loads(json.dumps(self._data["watches"]))
        if self.browser is None:
            return "Az automata kereskedéshez Jarvis saját böngészője kell."
        now = self.clock()
        game = _Round(check, notify, ModelLane() if self.llm is not None else None)
        for search in searches:
            sites = [key for key in search.get("sites") or list(self.platforms) if key in self.platforms
                     and (force or self._due(search, key, now))]
            if not sites:
                continue
            status, proposal = search_status(search), search.get("proposal") or {}
            if status == "javaslat":           # jóváhagyásig ezzel nem keres (az oldalakat sem tölti be)
                game.notes.append(f"„{search['query']}”: a szokásos ár ({format_price(proposal['price'])}) "
                                  "jóváhagyásra vár")
                continue
            if status == "kevés" and now - float(proposal.get("at") or 0) < CALIBRATE_RETRY:
                game.notes.append(f"„{search['query']}”: kevés hasonló hirdetés volt, add meg kézzel a szokásos árat")
                continue
            game.done += 1
            game.work.append(partial(self._step_search, game, search, sites, status))
        running = [s for s in searches if search_status(s) == "fut"]
        for watch in watches:
            if "vatera" in self.platforms and (force or self._due(watch, "vatera", now)):
                game.done += 1
                game.work.append(partial(self._step_watch, game, watch, running))
        try:
            game.loop()
        finally:
            if game.lane is not None:
                game.lane.close()
        extra = [n for n in game.notes if n] + game.errors
        if not game.done:
            return "Most egyik keresés sem esedékes" + (f" ({'; '.join(extra[:4])})" if extra else "") + "."
        text = (f"{game.done} keresés: {game.found} olcsó hirdetés" if game.found
                else f"{game.done} keresés: nincs új olcsó hirdetés")
        if extra:
            text += f" ({'; '.join(extra[:4])})"
        return text + "."

    def _step_search(self, game: _Round, search: dict[str, Any], sites: list[str], status: str) -> None:
        """Egy keresés: a találati oldalak, majd a szokásos árhoz mért jelöltek, vagy (ha még nincs) a felmérés."""
        rows, problems = self._collect(search, sites, game.check)
        game.errors += problems
        if not rows:
            return
        kept = self._relevant(search, rows)
        if status == "fut":
            game.next([partial(self._candidate, game, search, deal) for deal in self._hunt(search, kept)])
            self._drift(search, kept, game.notify)
            return
        rows = kept[:CALIBRATE_ROWS]
        if rows:
            game.ask(partial(self._judge, search, rows), partial(self._calibrated, game, search, rows))
        else:
            self._calibrated(game, search, rows, None)

    def _deal_text(self, search: dict[str, Any], deal: dict[str, Any]) -> str:
        warn = "".join(f" Figyelj: {w}." for w in deal.get("warnings") or [])
        title = f"„{deal['text'][:80]}”"
        if deal.get("auction"):
            bids = f", {deal['bids']} licit" if deal.get("bids") is not None else ""
            return (f"Vatera-licit: {title}, {left_text(deal.get('left'))} múlva zárul, most {format_price(deal['price'])}"
                    f"{bids}. Máshol kb. {format_price(deal['median'])}-ért adják. Legfeljebb "
                    f"{format_price(deal['max_bid'])}-ig érdemes licitálni (akkor is kb. "
                    f"{format_price(expected_profit(deal['median'], deal['max_bid']))} a haszon). Licitálni nem fogok "
                    f"helyetted.{warn}")
        if deal.get("offer"):
            return (f"Vinted: {title} {format_price(deal['price'])}, a szokásos ára {format_price(deal['median'])}. "
                    f"Ha {format_price(deal['offer'])}-ért megkapod (ajánlattétel), kb. {format_price(deal['profit'])} "
                    f"a haszon. Ajánlatot tegyek? Csak a jóváhagyásod után küldöm el. Keresés: „{search['query']}”.{warn}")
        drop = (f" (leárazta: {format_price(deal['drop'])} → {format_price(deal['price'])})"
                if deal.get("drop") else "")
        when = " A felmérés közben találtam." if deal.get("calibration") else ""
        return (f"{deal['site']}: {title} {format_price(deal['price'])}{drop}. A szokásos ára "
                f"{format_price(deal['median'])}, a várható haszon kb. {format_price(deal['profit'])}. Keresés: "
                f"„{search['query']}”.{when}{warn}")

    def _collect(self, search: dict[str, Any], sites: list[str], check: Callable[[], None],
                 track: bool = True) -> tuple[list[dict[str, Any]], list[str]]:
        rows, errors = [], []
        query = urllib.parse.quote(search["query"])
        browser = self.browser()
        for key in sites:
            site = self.platforms[key]
            check()
            if track:
                with self._lock:
                    self._data["last"][f"{search['id']}:{key}"] = self.clock()
            try:
                listings = browser.extract_listings(site.search_url.format(q=query) + NEWEST.get(key, ""))
            except BrowserError as exc:
                errors.append(f"{site.name}: {str(exc).splitlines()[0][:120]}")
                continue
            if not listings:
                errors.append(f"{site.name}: nem találtam árat a találati oldalon (nincs találat, ellenőrzést kér, "
                              "vagy megváltozott az oldal)")
            for entry in listings[:MAX_PER_SITE]:
                if entry.get("auction"):
                    continue          # a licit csak a mostani ajánlat, nem eladási ár
                rows.append({"site": site.name, "platform": key, "text": str(entry.get("text") or "")[:180],
                             "price": int(entry["price"]), "url": str(entry.get("url") or "")})
        return rows, errors

    def _relevant(self, search: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Modell nélkül, ami biztosan nem a keresett termék, kiesik: kereső-, csere- és csomaghirdetés, alkatrész
        (selling.prefilter), ha a keresés valamelyik szava hiányzik, kiegészítő (matches_query), egy kizáró szó van
        benne, vagy a minimum ár alatt van. Ugyanaz a hirdetés (webcím) egyszer."""
        probe = Item(id="kereses", created=0.0, updated=0.0, title=search["query"], search_query=search["query"])
        # az azonos című, hasonló árú hirdetések itt nem ismétlések (egy népszerű termékből sok van): a webcím dönt
        kept, _dropped = prefilter(probe, rows, limit=len(rows) or 1, dedupe=False)
        unique: dict[str, dict[str, Any]] = {}
        for row in kept:
            unique.setdefault(listing_key(row["url"]) if row["url"] else row["text"], row)
        words, low = search.get("exclude") or [], search.get("min_price")
        return [row for row in unique.values() if matches_query(search["query"], row["text"])
                and not excluded(words, row["text"]) and not (low and row["price"] < low)]

    @staticmethod
    def _cheap(search: dict[str, Any], price: int, reference: int) -> bool:
        return pays(search, price, reference)

    def _deal(self, search: dict[str, Any], row: dict[str, Any], reference: int, **extra: Any) -> dict[str, Any]:
        return {**row, "id": secrets.token_hex(5), "search": search["id"], "query": search["query"],
                "median": reference, "margin": reference - row["price"],
                "profit": expected_profit(reference, row["price"]), "at": self.clock(),
                **{k: v for k, v in extra.items() if v is not None and v is not False}}

    def _deal_keys(self) -> set[str]:
        return {listing_key(d["url"]) for d in self._data["deals"] if d.get("url")}

    def _hunt(self, search: dict[str, Any], kept: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Modell nélkül, a jóváhagyott szokásos árhoz mérve: a még nem látott (vagy azóta leárazott) hirdetések
        közül, ami megéri. A többit látottnak jegyzi, az árukkal (a leárazás észrevételéhez)."""
        reference = int(search["reference"])
        since = float(search.get("checked_from") or 0)
        now = self.clock()
        out = []
        with self._lock:
            seen, prices, deals = self._data["seen"], self._data["prices"], self._deal_keys()
            for row in kept:
                key = listing_key(row["url"]) if row["url"] else ""
                if not key:
                    continue
                old = prices.get(key)
                new = key not in seen or seen[key] < since
                drop = old is not None and row["price"] <= old * (1 - DROP)
                if not (new or drop) or (key in deals and not drop):
                    prices[key] = row["price"]
                    continue
                offer = offer_price(search, row["price"], reference) if row.get("platform") == "vinted" else None
                if self._cheap(search, row["price"], reference):
                    out.append(self._deal(search, row, reference, drop=old if drop else None))
                elif offer:            # kicsit drága: a Vinteden ajánlatot tehetsz (csak a jóváhagyásoddal)
                    out.append(self._deal(search, row, reference, offer=offer, profit=expected_profit(reference, offer)))
                else:
                    seen[key], prices[key] = now, row["price"]
            self._prune()
            self._save()
        return sorted(out, key=lambda d: -d["profit"])

    def _calibrated(self, game: _Round, search: dict[str, Any], rows: list[dict[str, Any]],
                    picked: list[dict[str, Any]] | None) -> None:
        """A szokásos ár felmérése (keresésenként egyszer), a modell választása (picked) után: a kiválasztott valódi,
        működő, egy darabos termékek középértéke a javaslat. Erről szól; jóváhagyásig ezzel nem keres. Ha a
        kiválasztottak között jóval olcsóbb is van, az jelölt (felmérés közben talált). Modell nélkül (vagy ha nem
        válaszolt) az előszűrt találatokból számol."""
        now = self.clock()
        query = search["query"]
        earlier = search.get("proposal") or {}     # egy korábbi, kevés hasonlóval végződött felmérés
        if picked is None:
            picked = rows
        stats = price_stats([r["price"] for r in picked])
        if not stats or stats["n"] < CALIBRATE_MIN:
            self._set(search["id"], proposal={"price": None, "n": len(picked), "at": now})
            if not earlier:                   # egyszer szól, nem minden próbálkozáskor
                game.notify("deal", f"Szokásos ár kell: {query}",
                            f"„{query}”: kevés hasonló hirdetést találtam ({len(picked)}), ebből nem mondok szokásos "
                            "árat. Add meg kézzel az Automata kereskedés fülön, vagy adj meg gyakoribb terméket.",
                            actions=[DEALS_ACTION])
            game.notes.append(f"„{query}”: kevés hasonló hirdetés ({len(picked)}), add meg kézzel a szokásos árat")
            return
        price = round_price(stats["median"])
        proposal = {"price": price, "p25": stats["p25"], "p75": stats["p75"], "n": stats["n"], "at": now,
                    "rows": [{"text": r["text"][:120], "price": r["price"], "url": r["url"], "site": r["site"]}
                             for r in sorted(picked, key=lambda r: r["price"])[:15]]}
        self._set(search["id"], proposal=proposal)
        search["proposal"] = proposal
        game.notify("deal", f"Szokásos ár: {query}",
                    f"„{query}”: szerintem most kb. {format_price(price)} ({stats['n']} hasonló hirdetés alapján, a "
                    f"többség {format_price(stats['p25'])}–{format_price(stats['p75'])}). Ha jó, fogadd el az Automata "
                    "kereskedés fülön, vagy írd át; addig ezzel nem keresek.", actions=[DEALS_ACTION])
        game.notes.append(f"„{query}”: szokásos ár javaslat {format_price(price)}, jóváhagyásra vár")
        with self._lock:
            deals = self._deal_keys()
        candidates = [self._deal(search, row, price, calibration=True) for row in picked
                      if row["url"] and listing_key(row["url"]) not in deals and self._cheap(search, row["price"], price)]
        game.next([partial(self._candidate, game, search, deal)
                   for deal in sorted(candidates, key=lambda d: -d["profit"])])

    def _judge(self, search: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        """Egy modellhívás: a találatok közül melyik pontosan a keresett termék. None: a modell nem válaszolt."""
        listing = "\n".join(f"{n}. [{r['site']}] {r['text']} – {format_price(r['price'])}"
                            for n, r in enumerate(rows, 1))
        messages = [{"role": "system", "content": JUDGE_SYSTEM},
                    {"role": "user", "content": f"Keresett termék: {search['query']}\n\nTalálatok:\n{listing}"}]
        try:
            data = self.llm.chat_json(messages, JUDGE_SCHEMA, temperature=0.0, max_tokens=300)
        except Exception:  # noqa: BLE001 - a modell hibája ne állítsa le a keresést
            return None
        picked: list[dict[str, Any]] = []
        for number in data.get("similar") or []:
            try:
                index = int(number) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(rows) and rows[index] not in picked:
                picked.append(rows[index])
        return picked

    def _drift(self, search: dict[str, Any], kept: list[dict[str, Any]], notify: Callable[..., None]) -> None:
        """Ármozgás: ha a hirdetésekben napok óta jóval más az ár, mint a jóváhagyott szokásos ár, egyszer szól."""
        stats = price_stats([r["price"] for r in kept])
        if not stats or stats["n"] < MIN_PRICES:
            return
        now, median, reference = self.clock(), stats["median"], int(search["reference"])
        market = dict(search.get("market") or {})
        off = abs(median - reference) > DRIFT * reference
        since = float(market.get("since") or now) if off else None
        market.update(median=median, at=now, since=since)
        if not off:
            market.pop("noted", None)
        elif now - since >= DRIFT_DAYS * 86400 and not market.get("noted"):
            notify("deal", f"Változott az ár? {search['query']}",
                   f"„{search['query']}”: szokásos árnak {format_price(reference)}-ot fogadtál el, de a hirdetésekben "
                   f"{DRIFT_DAYS} napja inkább {format_price(median)} körül van. Ha szeretnéd, mérd fel újra, vagy írd át "
                   "az Automata kereskedés fülön.", actions=[DEALS_ACTION])
            market["noted"] = now
        self._set(search["id"], market=market)

    def _candidate(self, game: _Round, search: dict[str, Any], deal: dict[str, Any]) -> None:
        """Egy jelölt: a hirdetés oldala (modell nélkül, a böngésző szálán), majd egy rövid modellkérdés a modell-sorban.
        Ha a kör keretébe már nem fér bele, nem jegyzi látottnak: a következő körben újra jelölt."""
        if game.blocked:
            deal.setdefault("warnings", []).append("a hirdetés oldalát nem néztem meg (robot-ellenőrzés)")
        else:
            if game.pages <= 0:
                return
            game.pages -= 1
            verdict = self._inspect(deal)
            if verdict == "captcha":
                game.blocked = True            # ebben a körben több hirdetést nem nyit meg
            elif verdict:
                self._mark(deal)               # elkelt, hibás vagy gyanús: nem szól róla
                return
        if game.lane is None:
            self._conclude(game, search, deal, None)
            return
        if game.checks <= 0:
            return
        game.checks -= 1
        game.ask(partial(self._confirm, search, deal), partial(self._conclude, game, search, deal))

    def _conclude(self, game: _Round, search: dict[str, Any], deal: dict[str, Any], ok: bool | None) -> None:
        """A modell válasza után (a böngésző szálán): ha nem mondott nemet, szól róla."""
        self._mark(deal)
        if ok is False:
            return
        game.found += 1
        self._remember(deal)
        actions = [DEALS_ACTION]
        if deal.get("offer"):
            actions = [{"action": "offer", "deal": deal["id"], "label": f"Ajánlatot teszek ({format_price(deal['offer'])})"},
                       DEALS_ACTION]
        title = f"Licit: {deal['text'][:60]}" if deal.get("auction") else f"Olcsó: {deal['text'][:60]}"
        game.notify("deal", title, self._deal_text(search, deal), url=deal["url"], actions=actions)

    # --- Vatera-licitfigyelő ---

    def _step_watch(self, game: _Round, watch: dict[str, Any], running: list[dict[str, Any]]) -> None:
        """A hamarosan záruló licitek egy Vatera-oldalon. Ami egy felmért keresésre illik, azt modell nélkül a szokásos
        árához méri; a többiből a modell választ (egy hívás), és azoknak máshol nézi meg az árát (_resale)."""
        game.check()
        with self._lock:
            self._data["last"][f"{watch['id']}:vatera"] = self.clock()
        try:
            listings = self.browser().extract_listings(watch["url"])
        except BrowserError as exc:
            game.errors.append(f"Vatera: {str(exc).splitlines()[0][:120]}")
            return
        window = int(watch.get("hours") or WATCH_HOURS) * 3600
        auctions = []
        with self._lock:
            seen = self._data["seen"]
            for entry in listings[:MAX_PER_SITE * 2]:
                if not entry.get("auction") or not entry.get("url"):
                    continue
                info = parse_auction(str(entry.get("text") or ""))
                if (info["left"] is not None and info["left"] > window) or listing_key(entry["url"]) in seen:
                    continue
                auctions.append({"site": "Vatera", "platform": "vatera", "text": str(entry.get("text") or "")[:180],
                                 "price": int(entry["price"]), "url": str(entry["url"]), "auction": True, **info})
        if not any(entry.get("auction") for entry in listings):
            game.errors.append(f"Vatera ({watch['name']}): nem találtam licitet az oldalon")
            return
        unknown, found = [], []
        for row in auctions:
            search = next((s for s in running if matches_query(s["query"], row["text"])
                           and not excluded(s.get("exclude") or [], row["text"])), None)
            if search is None:
                unknown.append(row)
                continue
            top = highest_ok(search, int(search["reference"]))
            if top and row["price"] < top:
                found.append((search, self._deal(search, row, int(search["reference"]), max_bid=top)))
            else:
                self._mark(row)
        game.next([partial(self._candidate, game, search, deal) for search, deal in found])
        batch = unknown[:EXPLORE_ROWS]
        if batch:
            game.ask(partial(self._explore, batch), partial(self._explored, game, watch, batch))

    def _explore(self, batch: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]] | None:
        """Egy modellhívás: a licitek közül melyik érhet meg egy viszonteladónak, és mi a keresőszava."""
        listing = "\n".join(f"{n}. {r['text']} – most {format_price(r['price'])}, {left_text(r.get('left'))} múlva zárul"
                            for n, r in enumerate(batch, 1))
        messages = [{"role": "system", "content": EXPLORE_SYSTEM},
                    {"role": "user", "content": f"Hamarosan záruló licitek:\n{listing}"}]
        try:
            data = self.llm.chat_json(messages, EXPLORE_SCHEMA, temperature=0.0, max_tokens=300)
        except Exception:  # noqa: BLE001 - a modell hibája ne állítsa le a keresést
            return None
        picks: list[tuple[dict[str, Any], str]] = []
        for pick in data.get("picks") or []:
            try:
                index = int(pick.get("n")) - 1
            except (AttributeError, TypeError, ValueError):
                continue
            query = " ".join(str(pick.get("query") or "").split())[:60]
            if 0 <= index < len(batch) and query and all(batch[index] is not row for row, _q in picks):
                picks.append((batch[index], query))
        return picks[:EXPLORE_PICKS]

    def _explored(self, game: _Round, watch: dict[str, Any], batch: list[dict[str, Any]],
                  picks: list[tuple[dict[str, Any], str]] | None) -> None:
        for row in batch:
            if picks is not None and all(row is not picked for picked, _q in picks):
                self._mark(row)                # egy licitet egyszer néz meg (ha a modell nem válaszolt, újra jön)
        steps = []
        for row, query in picks or []:
            if game.resale <= 0:
                break                          # a többi a következő körre marad (nem jegyeztük látottnak)
            game.resale -= 1
            steps.append(partial(self._resale, game, watch, row, query))
        game.next(steps)

    def _resale(self, game: _Round, watch: dict[str, Any], row: dict[str, Any], query: str) -> None:
        """Egy licit viszonteladási ára: a keresőszóval a többi oldalon (modell nélkül, a középérték). Ha a mostani
        licit jóval alatta van, jelölt, a javasolt legmagasabb ajánlattal."""
        self._mark(row)
        probe = {"id": watch["id"], "query": query}
        rows, _problems = self._collect(probe, [k for k in RESALE_SITES if k in self.platforms], game.check,
                                        track=False)
        kept = self._relevant(probe, rows)
        stats = price_stats([r["price"] for r in kept])
        if not stats or stats["n"] < CALIBRATE_MIN:
            return
        resale = stats["median"]
        max_bid = math.floor(resale * SALE_RATIO - SHIPPING - int(watch.get("min_profit") or WATCH_PROFIT))
        if row["price"] >= max_bid:
            return
        deal = self._deal(probe, row, resale, max_bid=max_bid, similar=stats["n"])
        game.next([partial(self._candidate, game, probe, deal)])

    # --- Vinted-ajánlat (csak a jóváhagyásod után) ---

    def make_offer(self, deal_id: str, amount: Any, confirm: Callable[[str], bool],
                   progress: Callable[[str], None] = lambda _text: None) -> str:
        """Ajánlattétel a Vinteden egy talált hirdetésre, emberi tempóban: megnyitja a hirdetést, az Ajánlattétel
        gombra kattint, beírja az összeget, és a küldés előtt jóváhagyást kér, pontosan ezzel az összeggel. Fizetni
        nem fizet: ha az eladó elfogadja, a Vinted értesít, és te veszed meg. Visszaadja a rövid eredményt; ha nem
        tudja megtenni, DealError."""
        deal = self.get(deal_id)
        if deal.get("platform") != "vinted":
            raise DealError("Ajánlatot csak a Vinteden tudok tenni.")
        if deal.get("offer_sent"):
            raise DealError(f"Erre már tettél ajánlatot ({format_price(deal['offer_sent']['price'])}); ha az eladó "
                            "elfogadja, a Vinted értesít.")
        offer = self._amount(amount if amount not in (None, "") else deal.get("offer"), "Az ajánlat")
        if not offer:
            raise DealError("Mennyit ajánljak?")
        if offer >= int(deal["price"]):
            raise DealError("Az ajánlat legyen kevesebb a kért árnál (különben vedd meg simán).")
        if self.browser is None:
            raise DealError("Az ajánlattételhez Jarvis saját böngészője kell.")
        browser = self.browser()
        previous = getattr(browser, "auto", False)
        browser.auto = True                    # modell nélküli lépéssor: emberi tempó, gondolkodási szünetekkel
        self.offering = (f"Ajánlat a Vinteden: {format_price(offer)} – „{deal['text'][:80]}” (most "
                         f"{format_price(deal['price'])}). Ha az eladó elfogadja, a Vinted szól, és ennyiért megveheted; "
                         "fizetni magától nem fogok.")
        try:
            return self._offer(browser, deal, offer, confirm, progress)
        finally:
            browser.auto = previous
            self.offering = ""

    def _offer(self, browser: Any, deal: dict[str, Any], offer: int, confirm: Callable[[str], bool],
               progress: Callable[[str], None]) -> str:
        try:
            progress("a hirdetés megnyitása")
            browser.open(deal["url"])
            if browser.challenged():
                raise DealError("A Vinted robot-ellenőrzést kér; oldd meg a böngészőablakomban, és próbáld újra.")
            elements = browser.scan()
            consent = consent_button(elements)
            if consent is not None:
                browser.click(consent["id"])
                elements = browser.scan()
            if logged_out(elements):
                raise DealError("Ki vagy jelentkezve a Vinteden: jelentkezz be a böngészőmben, és próbáld újra.")
            openers = [e for e in elements if e.get("kind", "") in BUTTON_KINDS | {"link"} and not e.get("disabled")
                       and _OFFER_OPEN_RE.match(norm_text(e.get("label", "")))]
            if len(openers) != 1:
                raise DealError("Nem találom a hirdetésen az Ajánlattétel gombot (lehet, hogy már elkelt, vagy az eladó "
                                "nem fogad ajánlatot), ezért nem tettem ajánlatot.")
            before = Counter(elem_key(e) for e in elements)
            progress("ajánlattétel")
            browser.click(openers[0]["id"])
            fresh = new_elements(before, browser.scan())
            boxes = [e for e in fresh if e.get("kind", "") in _PRICE_KINDS and not (e.get("disabled") or e.get("readonly")
                                                                                 or e.get("sensitive"))]
            labeled = [e for e in boxes if _OFFER_BOX_RE.search(norm_text(f"{e.get('label', '')} {e.get('field', '')}"))]
            box = (labeled or boxes)[0] if len(labeled or boxes) == 1 else None
            if box is None:
                browser.press("Escape")
                raise DealError("Nem találom az ajánlat összegének mezőjét, ezért nem tettem ajánlatot. Nézd meg a "
                                "böngészőablakomban.")
            browser.type_text(box["id"], str(offer))
            fresh = new_elements(before, browser.scan())
            buttons = [e for e in fresh if e.get("kind", "") in BUTTON_KINDS and not e.get("disabled")
                       and _OFFER_SEND_RE.match(norm_text(e.get("label", "")))]
            if len(buttons) != 1:
                browser.press("Escape")
                raise DealError("Nem találom az ajánlat küldés gombját, ezért nem tettem ajánlatot. Nézd meg a "
                                "böngészőablakomban.")
        except BrowserError as exc:
            raise DealError(f"Nem tettem ajánlatot: a böngészőben hiba volt ({str(exc).splitlines()[0][:160]}).") from None
        progress("küldés (jóváhagyásra vár)")
        label = buttons[0].get("label", "")
        if not (browser.cfg.confirm_risky and is_risky(label)):
            # A gomb felirata alapján a böngésző nem kérdezne rá (vagy ki van kapcsolva): ajánlat csak jóváhagyással.
            if not confirm(f'{self.offering}\n\nJarvis elküldené (erre kattintana: "{label}").'):
                browser.press("Escape")
                return "Rendben, nem tettem ajánlatot."
        try:
            result = browser.click(buttons[0]["id"])
        except BrowserError as exc:
            return (f"Az ajánlat küldésénél hiba volt ({str(exc).splitlines()[0][:160]}). Nézd meg a böngészőablakomban, "
                    "elment-e.")
        if result.startswith(DECLINED):
            browser.press("Escape")
            return "Rendben, nem tettem ajánlatot."
        with self._lock:
            for stored in self._data["deals"]:
                if stored.get("id") == deal["id"]:
                    stored["offer_sent"] = {"price": offer, "at": self.clock()}
            self._save()
        try:
            shown = str(browser.page_data(LISTING_PAGE_JS).get("text") or "")
        except BrowserError:
            shown = ""
        seen = str(offer) in re.sub(r"\D", "", shown) or format_price(offer).replace(" Ft", "") in shown
        return (f"Elküldtem az ajánlatot ({format_price(offer)}) a Vinteden. Ha az eladó elfogadja, a Vinted értesít, és "
                "akkor megveheted." + ("" if seen else " (Az oldalon nem láttam visszaigazolást; nézd meg a "
                                                     "böngészőablakomban.)"))

    def _mark(self, deal: dict[str, Any]) -> None:
        if not deal.get("url"):
            return
        with self._lock:
            key = listing_key(deal["url"])
            self._data["seen"][key], self._data["prices"][key] = self.clock(), int(deal["price"])
            self._prune()
            self._save()

    def _prune(self) -> None:
        seen = self._data["seen"]
        if len(seen) > MAX_SEEN:       # a legrégebbiek kiesnek
            for key, _at in sorted(seen.items(), key=lambda kv: kv[1])[:len(seen) - MAX_SEEN]:
                seen.pop(key, None)
        prices = self._data["prices"]
        for key in [k for k in prices if k not in seen][:max(0, len(prices) - MAX_SEEN)]:
            prices.pop(key, None)

    def _inspect(self, deal: dict[str, Any]) -> str:
        """A jelölt hirdetés oldala, modell nélkül, csak olvasva. Visszaadja, miért esik ki („” = maradhat, „captcha”:
        robot-ellenőrzés, ilyenkor maradhat, de nem néztük meg). A figyelmeztetéseket és a leírás elejét a jelölthöz
        teszi (a modellkérdéshez)."""
        try:
            page = self.browser().look(deal["url"])
        except BrowserError:
            deal.setdefault("warnings", []).append("a hirdetés oldalát nem tudtam megnyitni")
            return ""
        state, _detail = classify(page, deal["url"], deal["text"], deal.get("platform", ""))
        if page.get("captcha"):
            deal.setdefault("warnings", []).append("a hirdetés oldalát nem néztem meg (robot-ellenőrzés)")
            return "captcha"
        if state in (SOLD, GONE):
            return "elkelt"
        text = fold(str(page.get("text") or "")[:PAGE_TEXT])
        deal["details"] = " ".join(str(page.get("text") or "").split())[:PAGE_TEXT]
        if _SCAM_RE.search(text) or (deal.get("platform") == "vinted" and _OFFSITE_RE.search(text)):
            return "gyanús"
        if _PARTS_RE.search(text) or _DEFECT_RE.search(text):
            return "hibás"
        if _NO_REVIEWS_RE.search(text):
            deal.setdefault("warnings", []).append("az eladónak még nincs értékelése")
        if _OFFSITE_RE.search(text):
            deal.setdefault("warnings", []).append("az oldalon kívüli elérhetőséget ad meg")
        return ""

    def _confirm(self, search: dict[str, Any], deal: dict[str, Any]) -> bool | None:
        """Egy rövid modellkérdés egy jelöltről. None: nem sikerült megkérdezni (ilyenkor szól, de nem ellenőrzött)."""
        messages = [{"role": "system", "content": CHECK_SYSTEM},
                    {"role": "user", "content": f"Keresett termék: {search['query']}\nHirdetés ({deal['site']}): "
                                                f"{deal['text']}\nÁr: {format_price(deal['price'])} (a szokásos ára "
                                                f"{format_price(deal['median'])})"
                                                + (f"\nA hirdetés oldala: {deal['details'][:1200]}"
                                                   if deal.get("details") else "")}]
        try:
            data = self.llm.chat_json(messages, CHECK_SCHEMA, temperature=0.0, max_tokens=120)
        except Exception:  # noqa: BLE001 - a modell hibája ne állítsa le a keresést
            return None
        deal["checked"] = True
        deal["note"] = str(data.get("reason") or "")[:200]
        return bool(data.get("ok"))

    def _remember(self, deal: dict[str, Any]) -> None:
        deal = {k: v for k, v in deal.items() if k != "details"}   # a leírás csak a modellkérdéshez kellett
        with self._lock:
            self._data["deals"] = ([deal] + self._data["deals"])[:MAX_DEALS]
            self._save()
