"""Figyelők: mentett keresések, amiket Jarvis a háttérben, időnként újra lefuttat, és szól, ha új
találat jelent meg (pl. „szólj, ha lesz új Python-állás Debrecenben”).

Könnyű munka: egy webes keresés figyelőnként, böngésző és nyelvi modell nélkül. Az első
futáskor csak megjegyzi a meglévő találatokat; utána csak az újakról szól.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

from .textutil import fold
from .web import RECENCY_NAMES, clean_site, recency_code

MAX_SEEN = 400
MAX_HITS = 30
MAX_WATCHES = 30

SearchFn = Callable[[str, str, str], list[dict[str, str]]]   # (keresés, oldal, frissesség) → találatok


class WatchError(Exception):
    """Érthető hibaüzenet a figyelőkkel kapcsolatban."""


def normalize_url(url: str) -> str:
    """A követőparaméterek és a # rész nélkül, hogy ugyanaz a találat ne számítson újnak."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith(("utm_", "fbclid", "gclid"))]
    path = parts.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path,
                                    urllib.parse.urlencode(query), ""))


@dataclass
class Watch:
    id: str
    query: str
    site: str = ""
    recency: str = ""
    created: float = 0.0
    last_check: float = 0.0
    checks: int = 0
    seen: list[str] = field(default_factory=list)
    hits: list[dict[str, Any]] = field(default_factory=list)   # az utóbbi új találatok
    error: str = ""

    def describe(self) -> str:
        scope = []
        if self.site:
            scope.append(self.site)
        code = recency_code(self.recency)
        if code:
            scope.append(RECENCY_NAMES[code])
        return f"„{self.query}”" + (f" ({', '.join(scope)})" if scope else "")

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("seen", None)
        data["label"] = self.describe()
        return data


class WatchList:
    def __init__(self, path: Path, search: SearchFn, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.search = search
        self.clock = clock
        self._lock = threading.RLock()
        self._watches: dict[str, Watch] = self._load()

    def _load(self) -> dict[str, Watch]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        known = {f.name for f in fields(Watch)}
        out = {}
        for entry in data if isinstance(data, list) else []:
            if isinstance(entry, dict) and entry.get("id") and entry.get("query"):
                watch = Watch(**{k: v for k, v in entry.items() if k in known})
                out[watch.id] = watch
        return out

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(w) for w in self._watches.values()], ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    # --- kezelés ----------------------------------------------------------------------

    def all(self) -> list[Watch]:
        with self._lock:
            return sorted(self._watches.values(), key=lambda w: w.created)

    def add(self, query: str, site: str = "", recency: str = "het") -> Watch:
        query = " ".join((query or "").split())[:150]
        if not query:
            raise WatchError("Mit figyeljek? Adj meg egy keresést.")
        host = clean_site(site)
        if site and not host:
            raise WatchError(f"Ezt az oldalt nem értem: {site} (pl. profession.hu)")
        code = recency_code(recency)
        recency = {"d": "nap", "w": "hét", "m": "hónap", "y": "év"}.get(code or "", "")
        with self._lock:
            for watch in self._watches.values():
                if fold(watch.query) == fold(query) and watch.site == host:
                    watch.recency = recency
                    self._save()
                    return watch
            if len(self._watches) >= MAX_WATCHES:
                raise WatchError(f"Legfeljebb {MAX_WATCHES} figyelő lehet; előbb törölj egyet.")
            watch = Watch(id=secrets.token_hex(3), query=query, site=host, recency=recency,
                          created=self.clock())
            self._watches[watch.id] = watch
            self._save()
            return watch

    def find(self, ref: str) -> Watch:
        ref = (ref or "").strip()
        with self._lock:
            if ref in self._watches:
                return self._watches[ref]
            matches = [w for w in self._watches.values() if fold(ref) and fold(ref) in fold(w.query)]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise WatchError(f"Több figyelő is illik erre: {ref}. Az azonosítóval add meg.")
        raise WatchError(f"Nincs ilyen figyelő: {ref}")

    def remove(self, ref: str) -> Watch:
        watch = self.find(ref)
        with self._lock:
            self._watches.pop(watch.id, None)
            self._save()
        return watch

    # --- ellenőrzés -------------------------------------------------------------------------

    def check(self, watch: Watch) -> list[dict[str, str]]:
        """Lefuttatja a keresést; visszaadja az új találatokat (az első futáskor üres)."""
        try:
            results = self.search(watch.query, watch.site, watch.recency)
        except Exception as exc:  # noqa: BLE001 - pl. a kereső épp korlátoz
            with self._lock:
                watch.error = str(exc).splitlines()[0][:200]
                watch.last_check = self.clock()
                self._save()
            raise
        first = watch.checks == 0
        new = []
        with self._lock:
            seen = set(watch.seen)
            for item in results:
                key = normalize_url(item.get("url", ""))
                if not key or key in seen:
                    continue
                seen.add(key)
                watch.seen.append(key)
                if not first:
                    hit = {"title": item.get("title", ""), "url": item.get("url", ""),
                           "snippet": item.get("snippet", "")[:200], "at": self.clock()}
                    new.append(hit)
                    watch.hits.append(hit)
            del watch.seen[:-MAX_SEEN]
            del watch.hits[:-MAX_HITS]
            watch.checks += 1
            watch.last_check = self.clock()
            watch.error = ""
            self._save()
        return new

    def run(self, ctx: Any) -> str:
        """A háttérmunka: minden figyelő egyszer (a legrégebben nézett elöl)."""
        watches = sorted(self.all(), key=lambda w: w.last_check)
        if not watches:
            return "Nincs figyelő."
        found = errors = 0
        for watch in watches:
            ctx.check()   # ha közbeszóltál, itt abbahagyja
            try:
                new = self.check(watch)
            except Exception:  # noqa: BLE001 - a hiba a figyelőnél látszik, a többi megy tovább
                errors += 1
                continue
            if new:
                found += len(new)
                titles = "; ".join(n["title"] or n["url"] for n in new[:3])
                more = f" (és még {len(new) - 3})" if len(new) > 3 else ""
                ctx.notify("watch", f"Új találat: {watch.query}",
                           f"Új találat a(z) {watch.describe()} figyelőben: {titles}{more}.",
                           url=new[0]["url"], watch=watch.id)
        text = f"{len(watches)} figyelő átnézve, {found} új találat"
        return text + (f", {errors} hiba" if errors else "") + "."

    def summary(self) -> str:
        watches = self.all()
        if not watches:
            return "Nincs figyelőd. Ha kéred, beállítok egyet (pl. „szólj, ha lesz új Python-állás Debrecenben”)."
        lines = [f"{len(watches)} figyelő:"]
        for watch in watches:
            state = "még nem néztem meg" if not watch.checks else f"{len(watch.hits)} új találat eddig"
            if watch.error:
                state += f", legutóbb hiba: {watch.error}"
            lines.append(f"{watch.id} | {watch.describe()} | {state}")
            for hit in watch.hits[-3:]:
                lines.append(f"   - {hit['title']} {hit['url']}")
        return "\n".join(lines)
