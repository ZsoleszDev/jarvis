"""Háttérmunka: amit Jarvis magától, időnként elvégez (pl. a figyelők átnézése).

Csak akkor dolgozik, amikor ráér:
- nincs Szünet (a Szünet gomb mindent leállít, a háttérmunkát is); készenlétben csak a modell és
  böngésző nélküli munkák futnak (pl. a figyelők),
- épp nem dolgozik másvalamin, és az utolsó beszélgetés óta eltelt pár perc (quiet_minutes),
- az aktív órákon belül vagyunk (ha be van állítva), és nem függesztetted fel.

Ha nem ér rá, a munka nem marad ki, csak eltolódik: amint újra ráér, sorra kerül. Ha közben
megszólítod, vagy kérsz valamit, a háttérmunka a következő lépésnél abbahagyja, és később
folytatja. Egyszerre mindig csak egy munka fut, ugyanazon az ágens-szálon, mint minden más.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import SchedulerCfg

TICK_SECONDS = 20.0
_HOURS_RE = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*[-–]\s*(\d{1,2})[:.](\d{2})\s*$")


class Preempted(Exception):
    """A felhasználó közbeszólt (vagy Szünet jött): a háttérmunka később folytatódik."""


@dataclass
class Hooks:
    """Amit az ütemező a Jarvisról tudni akar (így a Jarvis nélkül is tesztelhető)."""
    paused: Callable[[], bool]
    busy: Callable[[], bool]
    last_activity: Callable[[], float]          # time.time() szerinti időpont
    submit: Callable[[str], None]               # a munkát az ágens-szál sorába teszi
    preempted: Callable[[], bool]               # közbeszólt-e azóta a felhasználó
    notify: Callable[..., None] = lambda *a, **k: None
    standby: Callable[[], bool] = lambda: False  # készenlét: szünet, de a könnyű munkák mehetnek


@dataclass
class Job:
    key: str
    title: str
    every_minutes: Callable[[], float]          # 0 vagy kevesebb = ki van kapcsolva
    run: Callable[["JobContext"], str]
    available: Callable[[], bool] = lambda: True
    needs_models: bool = True                   # kell-e hozzá a nyelvi modell (készenlétben nem fut)
    needs_browser: bool = False                 # kell-e hozzá a böngésző (készenlétben az sincs: nem fut)


@dataclass
class JobContext:
    scheduler: "Scheduler"
    job: Job
    forced: bool = False
    notes: list[str] = field(default_factory=list)

    def check(self) -> None:
        """A munka lépései között: ha a felhasználó közbeszólt, abbahagyjuk (később folytatjuk)."""
        if self.scheduler.should_yield():
            raise Preempted()

    def notify(self, kind: str, title: str, text: str, **extra: Any) -> None:
        self.scheduler.hooks.notify(kind, title, text, **extra)


def parse_hours(text: str) -> tuple[int, int] | None:
    """"08:00-20:00" → (480, 1200) percben. Üres vagy hibás → None (bármikor)."""
    match = _HOURS_RE.match(text or "")
    if not match:
        return None
    h1, m1, h2, m2 = (int(x) for x in match.groups())
    if not (0 <= h1 <= 24 and 0 <= h2 <= 24 and 0 <= m1 < 60 and 0 <= m2 < 60):
        return None
    return h1 * 60 + m1, h2 * 60 + m2


def within_hours(text: str, when: float) -> bool:
    span = parse_hours(text)
    if span is None:
        return True
    start, end = span
    now = dt.datetime.fromtimestamp(when)
    minute = now.hour * 60 + now.minute
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end      # éjfélen átnyúló, pl. 22:00-06:00


class Scheduler:
    def __init__(self, cfg: SchedulerCfg, state_path: Path, hooks: Hooks,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.path = Path(state_path)
        self.hooks = hooks
        self.clock = clock
        self.jobs: dict[str, Job] = {}
        self.running: str | None = None       # épp futó munka
        self.submitted: str | None = None     # a sorba tett, még el nem indult munka
        self.submitted_at = 0.0
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()     # a felület, a telefon és az ágens-szál is menthet
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state: dict[str, Any] = self._load()

    # --- állapot -------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("jobs", {})
        data.setdefault("snoozed_until", 0)
        return data

    def _save(self) -> None:
        with self._save_lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError:
                pass

    def register(self, job: Job) -> None:
        self.jobs[job.key] = job

    def _job_state(self, key: str) -> dict[str, Any]:
        return self.state["jobs"].setdefault(key, {})

    def is_on(self, key: str) -> bool:
        """Nincs-e kikapcsolva a felületen (Automatikus munkák)."""
        return not self._job_state(key).get("off")

    def set_enabled(self, key: str, on: bool) -> bool:
        """Be- vagy kikapcsol egy háttérmunkát; a következő indításkor is így marad. Hamis, ha nincs ilyen."""
        if key not in self.jobs:
            return False
        with self._lock:
            state = self._job_state(key)
            if on:
                state.pop("off", None)
            else:
                state["off"] = True
        self._save()
        return True

    # --- mikor ér rá ----------------------------------------------------------------

    def blocked_reason(self, now: float | None = None) -> str:
        """Miért nem indíthat most háttérmunkát (üres szöveg: ráér)."""
        now = self.clock() if now is None else now
        if not self.cfg.enabled:
            return "ki van kapcsolva"
        if self.hooks.paused() and not self.hooks.standby():
            return "szünet"
        until = float(self.state.get("snoozed_until") or 0)
        if until > now:
            return "felfüggesztve " + dt.datetime.fromtimestamp(until).strftime("%H:%M") + "-ig"
        if not within_hours(self.cfg.active_hours, now):
            return f"aktív órákon kívül ({self.cfg.active_hours})"
        if self.hooks.busy():
            return "Jarvis dolgozik"
        quiet = float(self.cfg.quiet_minutes) * 60 - (now - float(self.hooks.last_activity()))
        if quiet > 0:
            return "nemrég beszélgettünk"
        return ""

    def should_yield(self) -> bool:
        return self.hooks.preempted() or (self.hooks.paused() and not self.hooks.standby())

    def _allowed(self, job: Job) -> bool:
        """Futhat-e most ez a munka (készenlétben csak a modell és böngésző nélküliek)."""
        if not self.hooks.paused():
            return True
        return self.hooks.standby() and not job.needs_models and not job.needs_browser

    def due(self, job: Job, now: float) -> bool:
        every = float(job.every_minutes() or 0)
        if every <= 0 or not self.is_on(job.key) or not job.available() or not self._allowed(job):
            return False
        last = float(self._job_state(job.key).get("last_run") or 0)
        return now - last >= every * 60

    def next_due(self, now: float | None = None) -> Job | None:
        """A legrégebben esedékes munka (ha több is van)."""
        now = self.clock() if now is None else now
        due = [j for j in self.jobs.values() if self.due(j, now)]
        if not due:
            return None
        return min(due, key=lambda j: float(self._job_state(j.key).get("last_run") or 0))

    # --- futtatás ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="jarvis-scheduler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - az ütemező soha ne álljon le
                pass

    def tick(self, now: float | None = None) -> str | None:
        """Ha ráér és van esedékes munka, sorba teszi. Visszaadja a munka kulcsát (vagy None-t)."""
        now = self.clock() if now is None else now
        with self._lock:
            if self.submitted and now - self.submitted_at > 15 * 60:
                self.submitted = None     # biztonsági háló: ha a sorból kiesett (pl. Szünet), ne akadjon el
            if self.running or self.submitted:
                return None
            if self.blocked_reason(now):
                return None
            job = self.next_due(now)
            if job is None:
                return None
            self.submitted = job.key
            self.submitted_at = now
        self.hooks.submit(job.key)
        return job.key

    def dropped(self) -> None:
        """A sorba tett munka nem fog lefutni (pl. Szünet jött): a következő szabad percben újra jöhet."""
        with self._lock:
            self.submitted = None

    def run(self, key: str, forced: bool = False) -> str:
        """Lefuttat egy munkát (az ágens-szálon). Visszaadja a rövid eredményt."""
        with self._lock:
            self.submitted = None
            job = self.jobs.get(key)
            if job is None:
                return f"Nincs ilyen háttérmunka: {key}"
            if not self._allowed(job):
                return "Szünetelek, ezért most nem dolgozom."
            if not forced and self.should_yield():
                return "Később: épp közbeszóltál."
            self.running = key
        ctx = JobContext(self, job, forced=forced)
        state = self._job_state(key)
        try:
            result = job.run(ctx) or ""
            state.update({"last_run": self.clock(), "result": result[:300], "error": ""})
        except Preempted:
            state["yielded_at"] = self.clock()   # esedékes marad, a következő szabad percben folytatja
            result = "Félbehagytam, mert közbeszóltál; később folytatom."
        except Exception as exc:  # noqa: BLE001 - egy hibás munka miatt ne álljon le semmi
            # A hibás munka se próbálkozzon percenként: a következő esedékes időpontban újra.
            state.update({"last_run": self.clock(), "error": f"{type(exc).__name__}: {exc}"[:300]})
            result = f"Hiba: {exc}"
        finally:
            with self._lock:
                self.running = None
            self._save()
        return result

    # --- felfüggesztés és állapot ---------------------------------------------------------

    def snooze(self, minutes: float) -> float:
        """Ennyi percig nincs háttérmunka (0: újra mehet). Visszaadja, meddig."""
        until = self.clock() + max(0.0, float(minutes)) * 60 if minutes and minutes > 0 else 0.0
        self.state["snoozed_until"] = until
        self._save()
        return until

    def status(self) -> dict[str, Any]:
        now = self.clock()
        jobs = []
        for job in self.jobs.values():
            state = self._job_state(job.key)
            every = float(job.every_minutes() or 0)
            last = float(state.get("last_run") or 0)
            on = not state.get("off")
            jobs.append({
                "key": job.key, "title": job.title, "every_minutes": every,
                "enabled": every > 0 and on and job.available(), "on": on, "available": bool(job.available()),
                "last_run": last or None,
                "next_run": (last + every * 60) if every > 0 and on and last else None,
                "result": state.get("result", ""), "error": state.get("error", ""),
                "running": self.running == job.key,
            })
        blocked = self.blocked_reason(now)
        return {"enabled": bool(self.cfg.enabled), "blocked": blocked, "free": not blocked,
                "snoozed_until": float(self.state.get("snoozed_until") or 0) or None,
                "running": self.running, "jobs": jobs}
