"""Napló: mit csinált a Jarvis a nap folyamán (munka/kereskedes-naplo.json), röviden, időponttal.

Nem pontos jegyzőkönyv, hanem áttekintés: egy bejegyzés egy böngészési munkamenet összefoglalója („09:12–09:31
Hardverapró: 9 oldal, 214 hirdetés, 1 jó találat”), egy jó találat, egy pihenő, vagy amit megtanult. Napokra bontva,
az utolsó KEEP_DAYS nap marad meg.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

KEEP_DAYS = 14
MAX_PER_DAY = 300


def day_of(when: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(when))


class Journal:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.Lock()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        days = data.get("days") if isinstance(data, dict) else None
        self._days: dict[str, list[list[Any]]] = {
            str(day): [e for e in entries if isinstance(e, list) and len(e) == 2]
            for day, entries in (days or {}).items() if isinstance(entries, list)}

    def add(self, text: str, at: float | None = None) -> None:
        when = self.clock() if at is None else at
        text = " ".join(str(text).split())[:400]
        if not text:
            return
        with self._lock:
            entries = self._days.setdefault(day_of(when), [])
            entries.append([when, text])
            entries.sort(key=lambda e: e[0])
            del entries[:-MAX_PER_DAY]
            for old in sorted(self._days)[:-KEEP_DAYS]:
                self._days.pop(old, None)
            self._save()

    def entries(self, day: str = "") -> list[dict[str, Any]]:
        """Egy nap bejegyzései (alapból a mai), a legújabb elöl."""
        with self._lock:
            entries = list(self._days.get(day or day_of(self.clock()), []))
        return [{"at": at, "text": text} for at, text in reversed(entries)]

    def days(self) -> list[str]:
        """A napok, amelyekről van bejegyzés (a legújabb elöl)."""
        with self._lock:
            return sorted(self._days, reverse=True)

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"days": self._days}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass
