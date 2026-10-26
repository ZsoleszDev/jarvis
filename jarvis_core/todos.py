"""Teendőim: amit neked kell elvégezned, pl. egy eladott tárgy csomagjának feladása a Vinted adta kóddal, a megadott
helyen (messages.py: a vevői üzenetek olvasásakor, ha egy beszélgetésben „Eladva” és „Szállítási címke lekérése” van).
Az Elemzés fül Teendőim részén látszanak, a Kész gombbal jelölheted késznek őket. Amíg van elvégezetlen, minden este
(alapból 19:00-kor, [scheduler] todo_reminder) egy értesítés emlékeztet (reminder_due, a „todos” háttérmunka).

Egy teendő: {id, kind ("ship": csomagfeladás), at, title, item, item_name, platform, conversation, url, partner, code,
place, text, shot, done, done_at}. A text a címke oldalának szövege, ahogy a Jarvis látta, a shot a képe
(munka/teendok/<id>.jpg): ha a kódot nem sikerült kiolvasni (vagy QR-kódot adott a Vinted), ezekből látod."""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

KINDS = ("ship",)
MAX_TODOS = 200            # ennyit tart meg (ha több lenne, a legrégebben elvégzettek kiesnek)
MAX_SHOT = 4 * 1024 * 1024
TODO_ID_RE = re.compile(r"^[0-9a-f]{10}$")
_TIME_RE = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")
# A teendő mezői (szöveg) és a hosszuk felső határa.
FIELDS = {"title": 120, "item": 40, "item_name": 120, "platform": 20, "conversation": 20, "url": 300, "partner": 80,
          "code": 60, "place": 200, "text": 1500}


class TodoError(Exception):
    """Érthető hibaüzenet (a felület 400-zal adja vissza)."""


def parse_time(text: str) -> int | None:
    """"19:00" → 1140 (perc éjfél óta). Üres vagy hibás → None (nincs emlékeztető)."""
    match = _TIME_RE.match(text or "")
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    return hour * 60 + minute if hour < 24 and minute < 60 else None


class TodoList:
    """A teendők (munka/teendok.json). Több szálról is használható (felület, telefon, ágens-szál)."""

    def __init__(self, root: Path, on_change: Callable[[int], None] | None = None,
                 clock: Callable[[], float] = time.time):
        self.path = Path(root) / "teendok.json"
        self.shots = Path(root) / "teendok"
        self.on_change = on_change            # a felületnek: változott (hány elvégezetlen van)
        self.clock = clock
        self._lock = threading.RLock()
        self._todos: list[dict[str, Any]] = []
        self._reminded = ""                  # melyik napon emlékeztetett utoljára (ÉÉÉÉ-HH-NN)
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        todos = data.get("todos")
        if isinstance(todos, list):
            self._todos = [t for t in todos if isinstance(t, dict) and TODO_ID_RE.match(str(t.get("id") or ""))]
        self._reminded = str(data.get("reminded") or "")

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"todos": self._todos, "reminded": self._reminded}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def _changed(self) -> None:
        self._save()
        if self.on_change is not None:
            try:
                self.on_change(sum(1 for t in self._todos if not t.get("done")))
            except Exception:  # noqa: BLE001 - a felület hibája ne rontsa el a mentést
                pass

    def _get(self, todo_id: str) -> dict[str, Any]:
        for todo in self._todos:
            if todo["id"] == todo_id:
                return todo
        raise TodoError("Nincs ilyen teendő (talán már törölted).")

    @staticmethod
    def _clean(values: dict[str, Any]) -> dict[str, str]:
        return {key: " ".join(str(value or "").split())[:FIELDS[key]] if key != "text"
                else str(value or "").strip()[:FIELDS[key]] for key, value in values.items() if key in FIELDS}

    # --- lekérdezés ---

    def all(self) -> list[dict[str, Any]]:
        """Elöl az elvégezetlenek (a legrégebbi elöl: azt kell előbb feladni), utána az elvégzettek (a legutóbbi elöl)."""
        with self._lock:
            todos = copy.deepcopy(self._todos)
        waiting = sorted((t for t in todos if not t.get("done")), key=lambda t: float(t.get("at") or 0))
        done = sorted((t for t in todos if t.get("done")), key=lambda t: -float(t.get("done_at") or 0))
        return waiting + done

    def waiting(self) -> list[dict[str, Any]]:
        return [t for t in self.all() if not t.get("done")]

    def find(self, **match: Any) -> dict[str, Any] | None:
        """Az első teendő, aminél minden megadott mező egyezik (pl. conversation=…), vagy None."""
        with self._lock:
            for todo in self._todos:
                if all(todo.get(key) == value for key, value in match.items()):
                    return copy.deepcopy(todo)
        return None

    def shot_file(self, todo_id: str) -> Path:
        """A teendő képe (a címke oldala); TodoError, ha nincs."""
        if not TODO_ID_RE.match(todo_id or ""):
            raise TodoError("Nincs ilyen teendő.")
        with self._lock:
            name = str(self._get(todo_id).get("shot") or "")
        path = self.shots / name
        if not name or not path.is_file():
            raise TodoError("Ehhez a teendőhöz nincs kép.")
        return path

    # --- módosítás ---

    def add(self, kind: str, title: str, **values: Any) -> dict[str, Any]:
        if kind not in KINDS:
            raise TodoError(f"Ismeretlen teendő: {kind}")
        todo = {"id": secrets.token_hex(5), "kind": kind, "at": self.clock(), "done": False, "done_at": 0.0,
                "shot": "", **{key: "" for key in FIELDS}, **self._clean({**values, "title": title})}
        with self._lock:
            self._todos.append(todo)
            if len(self._todos) > MAX_TODOS:
                done = sorted((t for t in self._todos if t.get("done")), key=lambda t: float(t.get("done_at") or 0))
                for old in done[:len(self._todos) - MAX_TODOS]:
                    self._todos.remove(old)
                    self._drop_shot(old)
            self._changed()
            return copy.deepcopy(todo)

    def update(self, todo_id: str, **values: Any) -> dict[str, Any]:
        with self._lock:
            todo = self._get(todo_id)
            todo.update(self._clean(values))
            self._changed()
            return copy.deepcopy(todo)

    def set_done(self, todo_id: str, done: bool = True) -> dict[str, Any]:
        """Kész (vagy mégsem kész)."""
        with self._lock:
            todo = self._get(todo_id)
            todo["done"] = bool(done)
            todo["done_at"] = self.clock() if done else 0.0
            self._changed()
            return copy.deepcopy(todo)

    def remove(self, todo_id: str) -> None:
        with self._lock:
            todo = self._get(todo_id)
            self._todos.remove(todo)
            self._drop_shot(todo)
            self._changed()

    def save_shot(self, todo_id: str, data: bytes) -> str:
        """A címke oldalának képe (JPEG) a teendőhöz; visszaadja a fájl nevét."""
        if not data or len(data) > MAX_SHOT:
            raise TodoError("A kép üres vagy túl nagy.")
        with self._lock:
            todo = self._get(todo_id)
            self.shots.mkdir(parents=True, exist_ok=True)
            name = f"{todo_id}.jpg"
            (self.shots / name).write_bytes(data)
            todo["shot"] = name
            self._changed()
            return name

    def _drop_shot(self, todo: dict[str, Any]) -> None:
        if todo.get("shot"):
            try:
                (self.shots / str(todo["shot"])).unlink()
            except OSError:
                pass

    # --- esti emlékeztető ---

    def reminder_due(self, at: str, now: float | None = None) -> bool:
        """Kell-e most emlékeztetni: van elvégezetlen teendő, a nap már elérte az `at` időpontot (pl. "19:00"), és ma
        még nem emlékeztetett."""
        minute = parse_time(at)
        if minute is None:
            return False
        moment = dt.datetime.fromtimestamp(self.clock() if now is None else now)
        with self._lock:
            if self._reminded == moment.strftime("%Y-%m-%d"):
                return False
            waiting = any(not t.get("done") for t in self._todos)
        return waiting and moment.hour * 60 + moment.minute >= minute

    def reminded(self, now: float | None = None) -> None:
        with self._lock:
            self._reminded = dt.datetime.fromtimestamp(self.clock() if now is None else now).strftime("%Y-%m-%d")
            self._save()

    def reminder_text(self) -> str:
        """Az esti emlékeztető szövege: hány teendő van, és melyek (a kóddal és a hellyel)."""
        waiting = self.waiting()
        lines = []
        for todo in waiting[:4]:
            details = ", ".join(part for part in (f"kód: {todo['code']}" if todo.get("code") else "",
                                                  todo.get("place") or "") if part)
            lines.append(f"{todo.get('item_name') or todo['title']}" + (f" ({details})" if details else ""))
        more = f" és még {len(waiting) - 4}" if len(waiting) > 4 else ""
        count = "egy teendőd" if len(waiting) == 1 else f"{len(waiting)} teendőd"
        return (f"Még {count} van: csomagfeladás – {'; '.join(lines)}{more}. Ha megvan, az Elemzés fül Teendőim részén "
                "jelöld késznek.")
