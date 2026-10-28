"""Értesítések: amit Jarvis a háttérben talált (új találat egy figyelőben, régóta fent lévő
hirdetés, egy elkeltnek vagy eltűntnek látszó hirdetés, egy vevő új üzenete), és amiről a telefonon is
tudnod kell (kész vagy elakadt feltöltés és levétel, egy kérdés a tárgyról). Megmaradnak újraindítás
után is, és a telefonos felület innen kérdezi le őket, olvasottnak is innen jelöli.

Egy értesítés a szövegén túl hivatkozhat egy tárgyra (item), egy oldalra (platform), és felajánlhat
műveleteket (actions): pl. az elkeltnek látszó hirdetésnél [{"action": "sold", "where": "vinted",
"delist": true, "label": "Eladva itt, vedd le a többiről"}]. A művelet a felületen a meglévő Eladva-út
(/work/items/{id}/sold): az értesítés gombja (a gép Értesítések ablakában, a laptop push-értesítésében és az
Android-alkalmazásban) csak megnyitja a hirdetés Eladva ablakát, előre kiválasztva; a levétel ott is a te
kattintásodra indul. Ha a háttérellenőrzés bejelentkezést vagy robot-ellenőrzést talált: [{"action": "login",
"platform": "vinted", "label": "Bejelentkezés a böngészőmben"}], ez megnyitja az oldalt Jarvis böngészőjében.

A kevésbé fontos értesítés (quiet: pl. egy oldal robot-ellenőrzést kért, és holnap magától folytatja, vagy egy
emlékeztető) csak az Értesítések listájába kerül, olvasottként: a telefonra és a laptopra nem megy, és nem olvassa fel.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

KINDS = ("watch", "stale", "sold", "delist", "post", "question", "message", "deal", "haggle", "error", "work",
         "todo", "info")


class NoticeBoard:
    def __init__(self, path: Path, limit: int = 150,
                 publish: Callable[[dict[str, Any]], None] | None = None):
        self.path = Path(path)
        self.limit = max(10, int(limit))
        self.publish = publish
        self._lock = threading.Lock()
        self._items: list[dict[str, Any]] = self._load()

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [n for n in data if isinstance(n, dict) and n.get("id")] if isinstance(data, list) else []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._items, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, kind: str, title: str, text: str, **extra: Any) -> dict[str, Any]:
        """Egy értesítés. corrects (egy találat azonosítója): ez a korábbi, arról a találatról szóló (csengető)
        értesítés helyesbítése: azok olvasottak lesznek, és áthúzva látszanak (superseded: a helyesbítés címe); a telefon
        a csengetést is leállítja."""
        notice = {"id": secrets.token_hex(5), "at": time.time(), "kind": kind if kind in KINDS else "info",
                  "title": str(title)[:120], "text": str(text)[:1500], "read": False}
        notice.update({k: v for k, v in extra.items() if v not in (None, "", [])})
        if notice.get("quiet"):
            notice["read"] = True         # nem fontos: a listában megvan, de nem jelez (és nem megy a telefonra)
        with self._lock:
            if notice.get("corrects"):
                for old in self._items:
                    if old.get("deal") == notice["corrects"] and not old.get("superseded"):
                        old.update(superseded=notice["title"], read=True)
            self._items.append(notice)
            del self._items[:-self.limit]
            try:
                self._save()
            except OSError:
                pass
        if self.publish is not None:
            try:
                self.publish(dict(notice))
            except Exception:  # noqa: BLE001 - a felület hibája ne vesszen el az értesítéssel
                pass
        return notice

    def list(self, unread_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = [dict(n) for n in self._items if not unread_only or not n.get("read")]
        return list(reversed(items))[:max(1, int(limit))]

    def unread_count(self) -> int:
        with self._lock:
            return sum(1 for n in self._items if not n.get("read"))

    def mark_read(self, ids: list[str] | None = None) -> int:
        """Olvasottnak jelöl (ids nélkül mindet; üres listával egyet sem). Visszaadja, hányat."""
        if ids is not None and not ids:
            return 0
        wanted = set(ids or [])
        changed = 0
        with self._lock:
            for notice in self._items:
                if not notice.get("read") and (not wanted or notice["id"] in wanted):
                    notice["read"] = True
                    changed += 1
            if changed:
                try:
                    self._save()
                except OSError:
                    pass
        return changed
