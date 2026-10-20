"""Automata kereskedés: alulárazott hirdetések keresése a Vinteden, a Jófogáson és a Hardverapróban.

Megadod, mit keresel (pl. „RTX 3060 Ti”, „Nike Air Max 42”), és a Jarvis időnként, emberi tempóban megnézi az oldalak
keresőjét (csak olvas, semmire nem kattint, nem vesz és nem ír senkinek). A találatokból modell nélkül kiszűri a
kereső-, csere- és csomaghirdetést, az alkatrészt és a más terméket (selling.prefilter), a többiből kiszámolja a szokásos
árat (selling.price_stats), és ami jóval olcsóbb ennél, azt jelöltnek veszi. Modellt csak ezekre a kevés jelöltre hív
(egy rövid igen/nem kérdés: tényleg ugyanaz a termék, működik, egy darab?), a gyorsaság és az áramfogyasztás miatt.
Ami átmegy, arról szól (kind: deal), a hirdetés linkjével és a becsült haszonnal. Egy hirdetésről egyszer szól.

Az egész egy kapcsolóval be- és kikapcsolható (a Munka menü Automata kereskedés fülén); alapból ki van kapcsolva.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from .browser import BrowserError
from .selling import (Item, Platform, format_price, listing_key, prefilter, price_stats)
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


class DealHunter:
    """A keresések, a kapcsoló és a talált olcsó hirdetések (munka/kereskedes.json)."""

    def __init__(self, root: Path, platforms: dict[str, Platform], browser: Callable[[], Any] | None,
                 llm: Any = None, clock: Callable[[], float] = time.time):
        self.path = Path(root) / "kereskedes.json"
        self.platforms = {k: p for k, p in platforms.items() if k in DEAL_SITES and p.search_url}
        self.browser = browser
        self.llm = llm
        self.clock = clock
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
                   discount: Any = None) -> dict[str, Any]:
        query = " ".join(str(query or "").split())[:80]
        if not query:
            raise DealError("Írd be, mit keressek (pl. „RTX 3060 Ti”).")
        chosen = [s for s in (sites or list(self.platforms)) if s in self.platforms] or list(self.platforms)
        try:
            limit = int(max_price) if max_price not in (None, "") else None
            off = int(discount) if discount not in (None, "") else DEFAULT_DISCOUNT
        except (TypeError, ValueError):
            raise DealError("A legmagasabb ár és a kedvezmény egész szám legyen.") from None
        if limit is not None and limit <= 0:
            limit = None
        off = min(max(off, 10), 80)
        with self._lock:
            if len(self._data["searches"]) >= MAX_SEARCHES:
                raise DealError(f"Legfeljebb {MAX_SEARCHES} keresés lehet.")
            self._data["searches"].append({"id": secrets.token_hex(4), "query": query, "sites": chosen,
                                           "max_price": limit, "discount": off})
            self._save()
        return self.state()

    def remove_search(self, search_id: str) -> dict[str, Any]:
        with self._lock:
            self._data["searches"] = [s for s in self._data["searches"] if s.get("id") != search_id]
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

    def run(self, check: Callable[[], None] = lambda: None,
            notify: Callable[..., None] = lambda *a, **k: None) -> str:
        """Egy kör: minden keresés minden oldalán az első találati oldal, modell nélkül; a jelöltekre egy rövid
        modellkérdés. Visszaadja, mit talált."""
        with self._lock:
            if not self._data["enabled"]:
                return "Az automata kereskedés ki van kapcsolva."
            searches = [dict(s) for s in self._data["searches"]]
        if self.browser is None:
            return "Az automata kereskedéshez Jarvis saját böngészője kell."
        found, errors, checked = 0, [], 0
        for search in searches:
            check()
            rows, problems = self._collect(search, check)
            errors += problems
            for deal in self._candidates(search, rows):
                check()
                if checked < MAX_CHECKS and self.llm is not None:
                    checked += 1
                    verdict = self._confirm(search, deal)
                    if verdict is False:
                        continue
                found += 1
                self._remember(deal)
                notify("deal", f"Olcsó: {deal['text'][:60]}",
                       f"{deal['site']}: {format_price(deal['price'])}, a hasonlók szokásos ára "
                       f"{format_price(deal['median'])} (kb. {format_price(deal['margin'])}-tal olcsóbb). "
                       f"Keresés: „{search['query']}”.", url=deal["url"])
        text = f"{len(searches)} keresés: {found} olcsó hirdetés" if found else f"{len(searches)} keresés: nincs új olcsó hirdetés"
        if errors:
            text += f" ({'; '.join(errors[:3])})"
        return text + "."

    def _collect(self, search: dict[str, Any], check: Callable[[], None]) -> tuple[list[dict[str, Any]], list[str]]:
        rows, errors = [], []
        query = urllib.parse.quote(search["query"])
        browser = self.browser()
        for key in search.get("sites") or list(self.platforms):
            site = self.platforms.get(key)
            if site is None:
                continue
            check()
            try:
                listings = browser.extract_listings(site.search_url.format(q=query))
            except BrowserError as exc:
                errors.append(f"{site.name}: {str(exc).splitlines()[0][:120]}")
                continue
            for entry in listings[:MAX_PER_SITE]:
                if entry.get("auction"):
                    continue          # a licit csak a mostani ajánlat, nem eladási ár
                rows.append({"site": site.name, "text": str(entry.get("text") or "")[:180],
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

    def _confirm(self, search: dict[str, Any], deal: dict[str, Any]) -> bool | None:
        """Egy rövid modellkérdés egy jelöltről. None: nem sikerült megkérdezni (ilyenkor szól, de nem ellenőrzött)."""
        messages = [{"role": "system", "content": CHECK_SYSTEM},
                    {"role": "user", "content": f"Keresett termék: {search['query']}\nHirdetés ({deal['site']}): "
                                                f"{deal['text']}\nÁr: {format_price(deal['price'])} (a hasonlóké "
                                                f"általában {format_price(deal['median'])})"}]
        try:
            data = self.llm.chat_json(messages, CHECK_SCHEMA, temperature=0.0, max_tokens=120)
        except Exception:  # noqa: BLE001 - a modell hibája ne állítsa le a keresést
            return None
        deal["checked"] = True
        deal["note"] = str(data.get("reason") or "")[:200]
        return bool(data.get("ok"))

    def _remember(self, deal: dict[str, Any]) -> None:
        with self._lock:
            self._data["deals"] = ([deal] + self._data["deals"])[:MAX_DEALS]
            self._save()
