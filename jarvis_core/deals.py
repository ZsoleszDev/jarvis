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
import os
import re
import secrets
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from .browser import BrowserError
from .listings import GONE, SOLD, classify
from .selling import (_PARTS_RE, Item, ItemError, Platform, _shared, _tokens, format_price, listing_key,
                      parse_price, prefilter, price_stats, round_price)
from .textutil import fold
from .web import UNTRUSTED_NOTE

DEAL_SITES = ("vinted", "jofogas", "hardverapro")
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
CHECK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["ok", "reason"],
}


class DealError(Exception):
    pass


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
                    "deals": sorted((dict(d) for d in deals), key=lambda d: -float(d.get("at") or 0)),
                    "sites": [{"key": k, "name": p.name} for k, p in self.platforms.items()],
                    "browser": self.browser is not None, "sale_ratio": SALE_RATIO, "shipping": SHIPPING}

    def available(self) -> bool:
        with self._lock:
            return self._data["enabled"] and bool(self._data["searches"]) and self.browser is not None

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
            if not self._data["enabled"] or not self._data["searches"]:
                return 0.0
            return float(min(int(s.get("every") or DEFAULT_EVERY) for s in self._data["searches"]))

    def _due(self, search: dict[str, Any], key: str, now: float) -> bool:
        every = max(int(search.get("every") or DEFAULT_EVERY), SITE_MIN_MINUTES.get(key, 0))
        last = float(self._data["last"].get(f"{search['id']}:{key}") or 0)
        return now - last >= every * 60 - 90     # másfél perc ráhagyás: az ütemező percre pontosan nem indít

    def run(self, check: Callable[[], None] = lambda: None,
            notify: Callable[..., None] = lambda *a, **k: None, force: bool = False) -> str:
        """Egy kör: az esedékes keresések (force: mind) oldalain az első találati oldal, a legújabb elöl, modell
        nélkül. Ha egy keresésnek még nincs szokásos ára, felméri (egy modellhívás), különben ahhoz méri a
        hirdetéseket. A jelöltek oldalát megnyitja (csak olvasva), és a megmaradtakra egy rövid modellkérdés.
        Visszaadja, mit talált."""
        with self._lock:
            if not self._data["enabled"]:
                return "Az automata kereskedés ki van kapcsolva."
            searches = json.loads(json.dumps(self._data["searches"]))
        if self.browser is None:
            return "Az automata kereskedéshez Jarvis saját böngészője kell."
        now = self.clock()
        found, done, notes, errors = 0, 0, [], []
        budget = {"pages": MAX_PAGES, "checks": MAX_CHECKS, "blocked": False}
        for search in searches:
            check()
            sites = [key for key in search.get("sites") or list(self.platforms) if key in self.platforms
                     and (force or self._due(search, key, now))]
            if not sites:
                continue
            status, proposal = search_status(search), search.get("proposal") or {}
            if status == "javaslat":           # jóváhagyásig ezzel nem keres (az oldalakat sem tölti be)
                notes.append(f"„{search['query']}”: a szokásos ár ({format_price(proposal['price'])}) jóváhagyásra vár")
                continue
            if status == "kevés" and now - float(proposal.get("at") or 0) < CALIBRATE_RETRY:
                notes.append(f"„{search['query']}”: kevés hasonló hirdetés volt, add meg kézzel a szokásos árat")
                continue
            done += 1
            rows, problems = self._collect(search, sites, check)
            errors += problems
            if not rows:
                continue
            kept = self._relevant(search, rows)
            if status == "fut":
                candidates = self._hunt(search, kept)
                self._drift(search, kept, notify)
            else:
                candidates, note = self._calibrate(search, kept, notify)
                notes.append(note)
            for deal in candidates:
                check()
                verdict = self._process(search, deal, budget)
                if verdict is None:
                    break                      # ebben a körben nem fér bele: a következő körben újra jelölt
                if not verdict:
                    continue
                found += 1
                self._remember(deal)
                notify("deal", f"Olcsó: {deal['text'][:60]}", self._deal_text(search, deal), url=deal["url"],
                       actions=[DEALS_ACTION])
        extra = [n for n in notes if n] + errors
        if not done:
            return "Most egyik keresés sem esedékes" + (f" ({'; '.join(extra[:4])})" if extra else "") + "."
        text = f"{done} keresés: {found} olcsó hirdetés" if found else f"{done} keresés: nincs új olcsó hirdetés"
        if extra:
            text += f" ({'; '.join(extra[:4])})"
        return text + "."

    @staticmethod
    def _deal_text(search: dict[str, Any], deal: dict[str, Any]) -> str:
        drop = (f" (leárazta: {format_price(deal['drop'])} → {format_price(deal['price'])})"
                if deal.get("drop") else "")
        when = " A felmérés közben találtam." if deal.get("calibration") else ""
        warn = "".join(f" Figyelj: {w}." for w in deal.get("warnings") or [])
        return (f"{deal['site']}: „{deal['text'][:80]}” {format_price(deal['price'])}{drop}. A szokásos ára "
                f"{format_price(deal['median'])}, a várható haszon kb. {format_price(deal['profit'])}. Keresés: "
                f"„{search['query']}”.{when}{warn}")

    def _collect(self, search: dict[str, Any], sites: list[str],
                 check: Callable[[], None]) -> tuple[list[dict[str, Any]], list[str]]:
        rows, errors = [], []
        query = urllib.parse.quote(search["query"])
        browser = self.browser()
        for key in sites:
            site = self.platforms[key]
            check()
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

    def _cheap(self, search: dict[str, Any], price: int, reference: int) -> bool:
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

    def _deal(self, search: dict[str, Any], row: dict[str, Any], reference: int, **extra: Any) -> dict[str, Any]:
        return {**row, "id": secrets.token_hex(5), "search": search["id"], "query": search["query"],
                "median": reference, "margin": reference - row["price"],
                "profit": expected_profit(reference, row["price"]), "at": self.clock(),
                **{k: v for k, v in extra.items() if v}}

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
                if self._cheap(search, row["price"], reference):
                    out.append(self._deal(search, row, reference, drop=old if drop else None))
                else:
                    seen[key], prices[key] = now, row["price"]
            self._prune()
            self._save()
        return sorted(out, key=lambda d: -d["profit"])

    def _calibrate(self, search: dict[str, Any], kept: list[dict[str, Any]],
                   notify: Callable[..., None]) -> tuple[list[dict[str, Any]], str]:
        """A szokásos ár felmérése (keresésenként egyszer): a modell kiválasztja a valódi, működő, egy darabos
        termékeket, és ezek középértéke a javaslat. Erről szól; jóváhagyásig ezzel nem keres. Ha a kiválasztottak
        között jóval olcsóbb is van, az jelölt (felmérés közben talált)."""
        now = self.clock()
        query = search["query"]
        proposal = search.get("proposal") or {}     # egy korábbi, kevés hasonlóval végződött felmérés (lásd run)
        rows = kept[:CALIBRATE_ROWS]
        picked = self._judge(search, rows) if rows and self.llm is not None else None
        if picked is None:
            picked = rows                     # modell nélkül (vagy ha nem válaszolt): az előszűrt találatok
        stats = price_stats([r["price"] for r in picked])
        if not stats or stats["n"] < CALIBRATE_MIN:
            self._set(search["id"], proposal={"price": None, "n": len(picked), "at": now})
            if not proposal:                  # egyszer szól, nem minden próbálkozáskor
                notify("deal", f"Szokásos ár kell: {query}",
                       f"„{query}”: kevés hasonló hirdetést találtam ({len(picked)}), ebből nem mondok szokásos árat. "
                       "Add meg kézzel az Automata kereskedés fülön, vagy adj meg gyakoribb terméket.",
                       actions=[DEALS_ACTION])
            return [], f"„{query}”: kevés hasonló hirdetés ({len(picked)}), add meg kézzel a szokásos árat"
        price = round_price(stats["median"])
        proposal = {"price": price, "p25": stats["p25"], "p75": stats["p75"], "n": stats["n"], "at": now,
                    "rows": [{"text": r["text"][:120], "price": r["price"], "url": r["url"], "site": r["site"]}
                             for r in sorted(picked, key=lambda r: r["price"])[:15]]}
        self._set(search["id"], proposal=proposal)
        search["proposal"] = proposal
        notify("deal", f"Szokásos ár: {query}",
               f"„{query}”: szerintem most kb. {format_price(price)} ({stats['n']} hasonló hirdetés alapján, a többség "
               f"{format_price(stats['p25'])}–{format_price(stats['p75'])}). Ha jó, fogadd el az Automata kereskedés "
               "fülön, vagy írd át; addig ezzel nem keresek.", actions=[DEALS_ACTION])
        with self._lock:
            deals = self._deal_keys()
        candidates = [self._deal(search, row, price, calibration=True) for row in picked
                      if row["url"] and listing_key(row["url"]) not in deals and self._cheap(search, row["price"], price)]
        return (sorted(candidates, key=lambda d: -d["profit"]),
                f"„{query}”: szokásos ár javaslat {format_price(price)}, jóváhagyásra vár")

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

    def _process(self, search: dict[str, Any], deal: dict[str, Any], budget: dict[str, Any]) -> bool | None:
        """Egy jelölt: a hirdetés oldala (modell nélkül), majd egy rövid modellkérdés. True: szól róla; False: kiesett;
        None: ebben a körben már nem fér bele (nem jegyzi látottnak, a következő körben újra jelölt)."""
        if budget["blocked"]:
            deal.setdefault("warnings", []).append("a hirdetés oldalát nem néztem meg (robot-ellenőrzés)")
        else:
            if budget["pages"] <= 0:
                return None
            budget["pages"] -= 1
            verdict = self._inspect(deal)
            if verdict == "captcha":
                budget["blocked"] = True       # ebben a körben több hirdetést nem nyit meg
            elif verdict:
                self._mark(deal)               # elkelt, hibás vagy gyanús: nem szól róla
                return False
        if self.llm is not None:
            if budget["checks"] <= 0:
                return None
            budget["checks"] -= 1
            if self._confirm(search, deal) is False:
                self._mark(deal)
                return False
        self._mark(deal)
        return True

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
