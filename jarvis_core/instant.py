"""Azonnali figyelő: amint új hirdetés kerül fel a Hardverapróra vagy a Jófogásra, kideríti, megéri-e megvenni, és csak
akkor szól, ha új ÉS megéri (a jó vételek többnyire a friss hirdetésekből jönnek; ezeken az oldalakon nincs is „szólj, ha
új jön” értesítés).

Megadod, mit figyeljen: egy keresőszót (pl. „RTX 3080”, mindkét oldalon), vagy egy találati oldal címét (pl. Hardverapró ›
Videókártyák). A Jarvis 2–15 percenként (alapból 3) letölti a találati oldal elejét, böngésző nélkül (feeds.SiteFetcher:
oldalanként egyszerre egy kérés, szünetekkel, óránként legfeljebb INSTANT_HOURLY), egy saját szálon: nem vár a
böngészőre, és arra sem, hogy épp beszélgetsz-e vele. Az első letöltésnél csak megjegyzi, ami már fent van; utána minden
új (vagy leárazott) hirdetést rögtön felmér (DealHunter.appraise): modell nélkül a felmért kereséseid és az árbázis
szokásos árához méri; amit így nem ismer, azt a modell nevezi meg, és ha kell, megnézi, mennyiért adják a másik oldalon;
a megérő jelölt oldalát átnézi (elkelt, hibás, gyanús?), és egy rövid modellkérdés után szól, a telefonra is (sürgős
értesítésként), az árral, a szokásos árral és a várható haszonnal. A vételi limit (Automata kereskedés: legfeljebb
ennyiért) fölötti hirdetést meg sem nézi. Ha épp dolgozol a Jarvisszal, a modellt nem zavarja: ami a modellre vár, az
utána jön (legfeljebb WAIT_MAX-ig).

Csak olvas: semmit nem vesz meg, nem ír senkinek. Az Automata kereskedés Indítás gombjával indul, a Leállítással és a
Szünettel áll le. Ha egy oldal tilt vagy robot-ellenőrzést kér, azon egy ideig nem próbálkozik (a SiteFetcher), és egyszer
szól.
"""
from __future__ import annotations

import json
import os
import random
import re
import secrets
import threading
import time
import urllib.parse
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .deals import DEALS_ACTION, DROP, MAX_EXCLUDE, DealError, deal_action, excluded, matches_query, parse_words
from .feeds import FAST_SITES, FeedError
from .selling import _WANTED_RE, ItemError, Platform, format_price, listing_key, parse_price
from .textutil import fold

MAX_ALERTS = 12
EVERY_CHOICES = (2, 3, 5, 10, 15)    # percben
DEFAULT_EVERY = 3
INSTANT_HOURLY = 60        # az azonnali figyelő oldalanként óránként legfeljebb ennyit kér (a többi a köröké)
MAX_SEEN = 500             # figyelőnként ennyi látott hirdetést jegyez meg
MAX_HITS = 60
MAX_ROWS = 30              # egy letöltésből ennyi hirdetést néz (az oldal eleje: a legújabbak)
MAX_WAITING = 60           # ennyi hirdetés várhat a modellre
WAIT_MAX = 1800            # ennyi ideig (másodperc); ami addig sem került sorra, az kimarad
TICK = 2.0
BOOK_SAVE = 600            # az árbázist legfeljebb ennyi másodpercenként írja ki (nagy fájl; a körök is mentik)
JITTER = (0.85, 1.15)


class InstantError(DealError):
    pass


class _Stop(Exception):
    """A figyelő leáll (kilépés)."""


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
        # A JarvisApp állítja be: értesítés (kind, title, text, spoken=…, **extra), és hogy most dolgozhat-e (nincs
        # Szünet, a háttérmunka nincs kikapcsolva).
        self.notify: Callable[..., None] = lambda *a, **k: None
        self.allowed: Callable[[], bool] = lambda: True
        # Kérdezheti-e most a modellt (nem, amíg épp dolgozol a Jarvisszal: addig a hirdetés vár, lásd _waiting).
        self.model_ok: Callable[[], bool] = lambda: True
        self._waiting: list[tuple[str, str, dict[str, Any], float]] = []   # (figyelő, oldal, hirdetés, mióta vár)
        self._lock = threading.RLock()
        self._next: dict[str, float] = {}           # figyelő:oldal → mikor jön legközelebb (a memóriában)
        self._recent: dict[str, deque] = {k: deque() for k in self.platforms}
        self._told_blocked: dict[str, float] = {}   # oldal → mikor szóltunk, hogy tilt (naponta egyszer)
        self._book_saved = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
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
                "alerts": [a for a in data.get("alerts") or [] if isinstance(a, dict) and a.get("id")],
                "seen": {str(k): v for k, v in seen.items() if isinstance(v, dict)},
                "hits": [h for h in data.get("hits") or [] if isinstance(h, dict) and h.get("url")]}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def state(self) -> dict[str, Any]:
        """A felületnek: be van-e kapcsolva, a figyelők (mikor nézte, hányat talált), a friss hirdetések, az oldalak
        állapota (tilt-e), és hány kérés megy így óránként."""
        with self._lock:
            alerts = json.loads(json.dumps(self._data["alerts"]))
            hits = [dict(h) for h in self._data["hits"]]
            enabled = self._data["enabled"]
        status = self.fetcher.status() if self.fetcher is not None else {}
        load = self._load_per_hour(alerts)
        return {"enabled": enabled, "alerts": alerts, "hits": hits, "every_choices": list(EVERY_CHOICES),
                "sites": [{"key": k, "name": p.name, **status.get(k, {}), "per_hour": load.get(k, 0)}
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
        return [{"name": name, "url": target} for name, target in BROWSE_LANES.get("hardverapro", [])
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
            if len(self._data["alerts"]) >= MAX_ALERTS:
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

    def stop(self) -> None:
        """Leállítja a szálat, és megvárja (legfeljebb pár másodpercig), hogy a mostani letöltését befejezze."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
        try:
            self.hunter.book.save()
        except OSError:
            pass

    def _loop(self) -> None:
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

    def active(self) -> bool:
        """Most dolgozhat-e: be van kapcsolva, van figyelő, nincs Szünet, és a gép üzemideje van."""
        with self._lock:
            if not self._data["enabled"] or not any(not a.get("off") for a in self._data["alerts"]):
                return False
        try:
            return bool(self.allowed()) and self.hunter.in_day()
        except Exception:  # noqa: BLE001
            return False

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
                        at = last + int(alert.get("every") or DEFAULT_EVERY) * 60 * self.rng.uniform(*JITTER)
                        self._next[f"{alert['id']}:{site}"] = at
                    if at <= now:
                        out.append((at, alert, site))
        out.sort(key=lambda entry: entry[0])
        return [(json.loads(json.dumps(alert)), site) for _at, alert, site in out]

    def tick(self) -> int:
        """Egy kör: oldalanként legfeljebb egy esedékes figyelő (ha az oldal most kérhető). Visszaadja, hányat nézett."""
        if not self.active():
            return 0
        self._retry_waiting()
        now = self.clock()
        done, used = 0, set()
        for alert, site in self.due(now):
            if site in used or not self._has_budget(site, now) or not self.fetcher.ready(site):
                continue
            used.add(site)
            self.poll(alert, site)
            done += 1
        return done

    def _has_budget(self, site: str, now: float) -> bool:
        recent = self._recent.setdefault(site, deque())
        while recent and now - recent[0] > 3600:
            recent.popleft()
        return len(recent) < INSTANT_HOURLY

    def _url(self, alert: dict[str, Any], site: str) -> str:
        if alert.get("url"):
            return str(alert["url"])
        return self.platforms[site].search_url.format(q=urllib.parse.quote(alert["query"]))

    def poll(self, alert: dict[str, Any], site: str) -> list[dict[str, Any]]:
        """Egy figyelő egy oldala: letölti a találatok elejét, és szól az új (vagy leárazott) hirdetésekről. Az első
        letöltésnél csak megjegyzi, ami már fent van. Visszaadja az új, illő hirdetéseket."""
        now = self.clock()
        key = f"{alert['id']}:{site}"
        with self._lock:
            self._next[key] = now + int(alert.get("every") or DEFAULT_EVERY) * 60 * self.rng.uniform(*JITTER)
            self._recent.setdefault(site, deque()).append(now)
        name = self.platforms[site].name
        try:
            rows = self.fetcher.listings(self._url(alert, site), site, self._check)
        except FeedError as exc:
            self._failed(alert, site, exc)
            return []
        rows = [{"site": name, "platform": site, "text": str(r.get("text") or "")[:180], "price": int(r["price"]),
                 "url": str(r["url"]), "title": str(r.get("title") or r.get("text") or "")[:120]}
                for r in rows[:MAX_ROWS] if r.get("url") and not r.get("auction")]
        self.hunter.book.add(rows)            # az árbázis is tanul belőle
        if now - self._book_saved >= BOOK_SAVE:
            self._book_saved = now
            self.hunter.book.save()
        fresh: list[dict[str, Any]] = []
        with self._lock:
            stored = next((a for a in self._data["alerts"] if a.get("id") == alert["id"]), None)
            if stored is None:                # közben törölted
                return []
            seen = self._data["seen"].setdefault(alert["id"], {})
            first = site not in (stored.get("baseline") or [])
            for row in rows:
                ident = listing_key(row["url"])
                old = seen.get(ident)
                seen[ident] = [now, row["price"]]
                if first:
                    continue
                drop = isinstance(old, list) and len(old) > 1 and row["price"] <= int(old[1]) * (1 - DROP)
                if (old is None or drop) and self._fits(stored, row):
                    fresh.append({**row, "drop": int(old[1]) if drop else None})
            if len(seen) > MAX_SEEN:
                for ident, _value in sorted(seen.items(), key=lambda kv: float(kv[1][0]))[:len(seen) - MAX_SEEN]:
                    seen.pop(ident, None)
            stored.setdefault("last", {})[site] = now
            stored["polls"] = int(stored.get("polls") or 0) + 1
            (stored.get("errors") or {}).pop(site, None)
            if first:
                stored["baseline"] = sorted(set(stored.get("baseline") or []) | {site})
            self._save()
        if first:
            self.hunter.journal.add(f"Azonnali figyelő: {alert['name']} ({name}): elindult, {len(rows)} hirdetést már "
                                    "ismerek; innentől minden újat felmérek, és ha megéri, szólok.")
            return []
        return self._report(alert, site, fresh)

    @staticmethod
    def _fits(alert: dict[str, Any], row: dict[str, Any]) -> bool:
        """Illik-e a figyelőhöz: a keresőszó (ha van) minden szava benne van, nincs benne kizáró szó, az ársávban van,
        és nem kereső hirdetés („Keresek…”)."""
        if _WANTED_RE.search(fold(row["text"])):
            return False
        if alert.get("query") and not matches_query(alert["query"], row["text"]):
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
                    "utána újra próbálom; addig a szokásos (böngészős) keresés marad.", actions=[DEALS_ACTION])

    def _report(self, alert: dict[str, Any], site: str, fresh: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Az új hirdetések felmérése (DealHunter.appraise): amelyik megéri, arról rögtön szól (ahogy kiderül), és a friss
        hirdetések közé teszi; ami a modellre vár, az a várakozók közé kerül. Visszaadja a megérőket."""
        source = f"azonnali figyelő: {alert['name']}"
        hits: list[dict[str, Any]] = []

        def found(deal: dict[str, Any]) -> None:
            hit = {"id": secrets.token_hex(5), "alert": alert["id"], "alert_name": alert["name"], "at": self.clock(),
                   "deal": deal["id"], "cheap": True,
                   **{k: deal.get(k) for k in ("site", "platform", "text", "title", "price", "url", "drop", "note")},
                   "reference": deal["median"], "profit": deal["profit"], "n": deal.get("similar"),
                   "warnings": deal.get("warnings") or []}
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

        waiting = self.hunter.appraise(fresh, source, self._look, self._search, self.model_ok, found)
        if waiting:
            now = self.clock()
            with self._lock:
                queued = {row["url"] for _a, _s, row, _t in self._waiting}
                self._waiting += [(alert["id"], site, row, now) for row in waiting if row["url"] not in queued]
                del self._waiting[:-MAX_WAITING]
        return hits

    def _retry_waiting(self) -> None:
        """A modellre váró friss hirdetések, amint lehet kérdezni (amíg nem túl régiek)."""
        if not self._waiting or not self.model_ok():
            return
        now = self.clock()
        with self._lock:
            waiting = [entry for entry in self._waiting if now - entry[3] < WAIT_MAX]
            self._waiting = []
            alerts = {a["id"]: json.loads(json.dumps(a)) for a in self._data["alerts"]}
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for alert_id, site, row, _at in waiting:
            if alert_id in alerts:
                groups.setdefault((alert_id, site), []).append(row)
        for (alert_id, site), rows in groups.items():
            self._report(alerts[alert_id], site, rows)

    def _look(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """Egy hirdetés oldala böngésző nélkül (None, ha most nem sikerült)."""
        try:
            return self.fetcher.page(row["url"], row.get("platform", ""), self._check)
        except FeedError:
            return None

    def _search(self, query: str) -> list[dict[str, Any]]:
        """Mennyiért adják máshol: a keresőszó találatai a gyors oldalakon (böngésző nélkül)."""
        out: list[dict[str, Any]] = []
        for site, platform in self.platforms.items():
            try:
                rows = self.fetcher.listings(platform.search_url.format(q=urllib.parse.quote(query)), site, self._check)
            except FeedError:
                continue
            out += [{"site": platform.name, "platform": site, "text": str(r.get("text") or "")[:180],
                     "price": int(r["price"]), "url": str(r["url"])} for r in rows[:MAX_ROWS]
                    if r.get("url") and not r.get("auction")]
        return out

    def _tell(self, alert: dict[str, Any], hit: dict[str, Any]) -> None:
        warn = "".join(f" Figyelj: {w}." for w in hit.get("warnings") or [])
        checked = f" {hit['note']}" if hit.get("note") else ""
        self.notify("deal", "Megéri: " + hit["title"][:60], f"{self._line(hit)}.{checked}{warn} ({alert['name']})",
                    spoken=f"Megérő új hirdetés: {hit['title'][:50]}, {format_price(hit['price'])}.",
                    url=hit["url"], actions=[deal_action(hit["deal"])], deal=hit["deal"], urgent=True)

    @staticmethod
    def _line(hit: dict[str, Any]) -> str:
        text = f"{hit['site']}: „{hit['title'][:80]}” {format_price(hit['price'])}, most került fel"
        if hit.get("drop"):
            text += f" (leárazta: {format_price(hit['drop'])} → {format_price(hit['price'])})"
        if hit.get("reference"):
            basis = f", {hit['n']} hasonló alapján" if hit.get("n") else ""
            text += f". A szokásos ára kb. {format_price(hit['reference'])}{basis}"
            if hit.get("profit", 0) > 0:
                text += f", kb. {format_price(hit['profit'])} haszon lehet"
        return text
