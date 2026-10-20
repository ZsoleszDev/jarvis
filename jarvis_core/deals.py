"""Automata kereskedés: alulárazott hirdetések keresése a Vinteden, a Jófogáson és a Hardverapróban.

Megadod, mit keresel (pl. „RTX 3060 Ti”, „Nike Air Max 42”), és a Jarvis időnként, emberi tempóban megnézi az oldalak
keresőjét (csak olvas, semmire nem kattint, nem vesz és nem ír senkinek). A találatokból modell nélkül kiszűri a
kereső-, csere- és csomaghirdetést, az alkatrészt és a más terméket (selling.prefilter), a többiből kiszámolja a szokásos
árat (selling.price_stats), és ami jóval olcsóbb ennél, azt jelöltnek veszi. Modellt csak ezekre a kevés jelöltre hív
(egy rövid igen/nem kérdés: tényleg ugyanaz a termék, működik, egy darab?), a gyorsaság és az áramfogyasztás miatt.
Ami átmegy, arról szól (kind: deal), a hirdetés linkjével és a becsült haszonnal. Egy hirdetésről egyszer szól.

Gyorsaság: a találatokat a legújabb elöl nézi, és csak a még nem látott hirdetésekből lesz jelölt. Keresésenként
megadható, milyen sűrűn nézze (30 perc – 6 óra); a Vintedet legfeljebb kétóránként (robot-ellenőrzés).
Mielőtt a modellt kérdezné, a jelölt hirdetés oldalát is megnyitja (csak olvasva), és modell nélkül megnézi: elkelt-e
már, van-e benne hibára utaló szó, vagy átverésre utaló jel (előre utalás, külföldről küldi, a Vinteden kívüli
elérhetőség). Ami gyanús vagy hibás, arról nem szól; ha az eladónak még nincs értékelése, szól, de figyelmeztet.

Az egész egy kapcsolóval be- és kikapcsolható (a Munka menü Automata kereskedés fülén); alapból ki van kapcsolva.
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
from .selling import (_PARTS_RE, Item, Platform, format_price, listing_key, prefilter, price_stats)
from .textutil import fold
from .web import UNTRUSTED_NOTE

DEAL_SITES = ("vinted", "jofogas", "hardverapro")
MAX_SEARCHES = 20
MAX_PER_SITE = 30          # egy keresés egy oldalán ennyi találatot néz meg (az első oldal)
MIN_PRICES = 5             # ennél kevesebb hasonló árból nem mond szokásos árat (bizonytalan lenne)
DEFAULT_DISCOUNT = 30      # ennyi %-kal a szokásos ár (medián) alatt számít olcsónak
FLOOR = 0.15               # a medián 15%-a alatti ár többnyire nem valódi ár (1 Ft, „ár megegyezés szerint”)
MAX_CHECKS = 5             # egy körben legfeljebb ennyi jelöltet néz meg a modellel
MAX_DEALS = 60             # ennyi találatot őriz meg a listában
MAX_SEEN = 5000
EVERY_CHOICES = (30, 60, 180, 360)   # percben: milyen sűrűn nézi meg egy keresés találatait
DEFAULT_EVERY = 180
SITE_MIN_MINUTES = {"vinted": 120}   # a Vintedet legfeljebb kétóránként (ott a robot-ellenőrzés a kockázat)
# A legújabb hirdetések elöl: a Vinteden külön kérni kell, a Jófogáson és a Hardverapróban ez az alap.
NEWEST = {"vinted": "&order=newest_first"}
MAX_PAGES = 5              # egy körben legfeljebb ennyi jelölt hirdetés oldalát nyitja meg
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
_NO_REVIEWS_RE = re.compile(r"nincs(enek)? (meg )?ertekeles\w*|meg nincs ertekeles\w*|no reviews yet|\b0 ertekeles")

CHECK_SYSTEM = (
    "Egy használtcikk-kereskedőnek segítesz. Megkapod, mit keres, és egy hirdetés címét, szövegét és árát. "
    "Döntsd el, megéri-e megnézni: ok=true, ha ez tényleg a keresett termék (nem kiegészítő, nem alkatrész, nem "
    "más típus), egy darab, eladó (nem keres vagy cserél), és nem hibás vagy hiányos. reason: egy rövid mondat "
    "magyarul. " + UNTRUSTED_NOTE)
CHECK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["ok", "reason"],
}


class DealError(Exception):
    pass


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
        return {"enabled": bool(data.get("enabled")),
                "searches": [s for s in data.get("searches") or [] if isinstance(s, dict) and s.get("query")],
                "deals": [d for d in data.get("deals") or [] if isinstance(d, dict) and d.get("url")],
                "seen": {str(k): float(v) for k, v in (data.get("seen") or {}).items()
                         if isinstance(v, (int, float))},
                "last": {str(k): float(v) for k, v in (data.get("last") or {}).items()
                         if isinstance(v, (int, float))}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def state(self) -> dict[str, Any]:
        """A felületnek: a kapcsoló, a keresések és a talált hirdetések (a legújabb elöl)."""
        with self._lock:
            deals = [d for d in self._data["deals"] if not d.get("dismissed")]
            return {"enabled": self._data["enabled"], "searches": [dict(s) for s in self._data["searches"]],
                    "deals": sorted((dict(d) for d in deals), key=lambda d: -float(d.get("at") or 0)),
                    "sites": [{"key": k, "name": p.name} for k, p in self.platforms.items()],
                    "browser": self.browser is not None}

    def available(self) -> bool:
        with self._lock:
            return self._data["enabled"] and bool(self._data["searches"]) and self.browser is not None

    def set_enabled(self, on: bool) -> dict[str, Any]:
        with self._lock:
            self._data["enabled"] = bool(on)
            self._save()
        return self.state()

    def add_search(self, query: str, sites: list[str] | None = None, max_price: Any = None,
                   discount: Any = None, every: Any = None) -> dict[str, Any]:
        query = " ".join(str(query or "").split())[:80]
        if not query:
            raise DealError("Írd be, mit keressek (pl. „RTX 3060 Ti”).")
        chosen = [s for s in (sites or list(self.platforms)) if s in self.platforms] or list(self.platforms)
        try:
            limit = int(max_price) if max_price not in (None, "") else None
            off = int(discount) if discount not in (None, "") else DEFAULT_DISCOUNT
            minutes = int(every) if every not in (None, "") else int(self.default_every or DEFAULT_EVERY)
        except (TypeError, ValueError):
            raise DealError("A legmagasabb ár és a kedvezmény egész szám legyen.") from None
        if limit is not None and limit <= 0:
            limit = None
        off = min(max(off, 10), 80)
        minutes = min(EVERY_CHOICES, key=lambda choice: abs(choice - minutes))
        with self._lock:
            if len(self._data["searches"]) >= MAX_SEARCHES:
                raise DealError(f"Legfeljebb {MAX_SEARCHES} keresés lehet.")
            self._data["searches"].append({"id": secrets.token_hex(4), "query": query, "sites": chosen,
                                           "max_price": limit, "discount": off, "every": minutes})
            self._save()
        return self.state()

    def remove_search(self, search_id: str) -> dict[str, Any]:
        with self._lock:
            self._data["searches"] = [s for s in self._data["searches"] if s.get("id") != search_id]
            self._save()
        return self.state()

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
        nélkül; a jelöltek oldalát megnyitja (csak olvasva), és a megmaradtakra egy rövid modellkérdés.
        Visszaadja, mit talált."""
        with self._lock:
            if not self._data["enabled"]:
                return "Az automata kereskedés ki van kapcsolva."
            searches = [dict(s) for s in self._data["searches"]]
        if self.browser is None:
            return "Az automata kereskedéshez Jarvis saját böngészője kell."
        found, errors, checked, opened, blocked = 0, [], 0, 0, False
        now = self.clock()
        done = 0
        for search in searches:
            check()
            sites = [key for key in search.get("sites") or list(self.platforms) if key in self.platforms
                     and (force or self._due(search, key, now))]
            if not sites:
                continue
            done += 1
            rows, problems = self._collect(search, sites, check)
            errors += problems
            for deal in self._candidates(search, rows):
                check()
                if opened < MAX_PAGES and not blocked:
                    opened += 1
                    verdict = self._inspect(deal)
                    if verdict == "captcha":
                        blocked = True         # ebben a körben több hirdetést nem nyit meg
                    elif verdict:
                        continue               # elkelt, hibás vagy gyanús: nem szól róla
                if checked < MAX_CHECKS and self.llm is not None:
                    checked += 1
                    if self._confirm(search, deal) is False:
                        continue
                found += 1
                self._remember(deal)
                warn = "".join(f" Figyelj: {w}." for w in deal.get("warnings") or [])
                notify("deal", f"Olcsó: {deal['text'][:60]}",
                       f"{deal['site']}: {format_price(deal['price'])}, a hasonlók szokásos ára "
                       f"{format_price(deal['median'])} (kb. {format_price(deal['margin'])}-tal olcsóbb). "
                       f"Keresés: „{search['query']}”.{warn}", url=deal["url"])
        if not done:
            return "Most egyik keresés sem esedékes."
        text = f"{done} keresés: {found} olcsó hirdetés" if found else f"{done} keresés: nincs új olcsó hirdetés"
        if errors:
            text += f" ({'; '.join(errors[:3])})"
        return text + "."

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
            for entry in listings[:MAX_PER_SITE]:
                if entry.get("auction"):
                    continue          # a licit csak a mostani ajánlat, nem eladási ár
                rows.append({"site": site.name, "platform": key, "text": str(entry.get("text") or "")[:180],
                             "price": int(entry["price"]), "url": str(entry.get("url") or "")})
        return rows, errors

    def _candidates(self, search: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Modell nélkül: a hasonló hirdetések szokásos ára, és ami ennél jóval olcsóbb, még nem láttuk."""
        probe = Item(id="kereses", created=0.0, updated=0.0, title=search["query"], search_query=search["query"])
        # az azonos című, hasonló árú hirdetések itt nem ismétlések (egy népszerű termékből sok van): a webcím dönt
        kept, _dropped = prefilter(probe, rows, limit=len(rows) or 1, dedupe=False)
        unique: dict[str, dict[str, Any]] = {}
        for row in kept:
            unique.setdefault(listing_key(row["url"]) if row["url"] else row["text"], row)
        kept = list(unique.values())
        stats = price_stats([r["price"] for r in kept])
        if not stats or stats["n"] < MIN_PRICES:
            return []
        median = stats["median"]
        ceiling = median * (100 - int(search.get("discount") or DEFAULT_DISCOUNT)) / 100
        limit = search.get("max_price")
        now = self.clock()
        out = []
        with self._lock:
            seen = self._data["seen"]
            for row in kept:
                key = listing_key(row["url"]) if row["url"] else ""
                if not key or key in seen:
                    continue
                seen[key] = now
                if not median * FLOOR <= row["price"] <= ceiling or (limit and row["price"] > limit):
                    continue
                out.append({**row, "id": secrets.token_hex(5), "search": search["id"], "query": search["query"],
                            "median": median, "margin": median - row["price"], "at": now})
            if len(seen) > MAX_SEEN:       # a legrégebbiek kiesnek
                for key, _at in sorted(seen.items(), key=lambda kv: kv[1])[:len(seen) - MAX_SEEN]:
                    seen.pop(key, None)
            self._save()
        return sorted(out, key=lambda d: -d["margin"])

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
                                                f"{deal['text']}\nÁr: {format_price(deal['price'])} (a hasonlóké "
                                                f"általában {format_price(deal['median'])})"
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
