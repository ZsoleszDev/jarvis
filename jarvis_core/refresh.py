"""Automatikus frissítés: a fent lévő hirdetés pár naponta újra a lista elejére kerül, hogy a friss hirdetéseket
böngészők ismét lássák.

- Vinted: leveszi, és újra feltölti (új hirdetés lesz belőle); ha van második képsor, felrakásonként a másikkal, így az
  újra feltöltött hirdetés újnak is látszik. A kedvelések és a megtekintések ezzel elvesznek (az új hirdetésé nulláról
  indul). A Vinted szabályzata nem tiltja, de a túl gyakori, egyforma képekkel újra feltöltött hirdetéseket kiszűrheti:
  hetente-kéthetente a biztonságos, és a második képsor sokat segít.
- Hardverapró: az ingyenes „UP” gombbal (naponta 8 jár mindenkinek), levétel nélkül: a hirdetés marad, csak előre kerül.
- Jófogás: kimarad. A szabályzata (2.1) szerint egy törölt hirdetés terméke 14 napig nem adható fel újra; az előre
  sorolás ott fizetős.
- Vatera: kimarad (ott a licitek és a lejárat a saját rendszerük szerint mennek).

Mikor: a megadott naponként, a megadott óra körül (egy kis véletlennel, hogy ne mindig percre ugyanakkor), és csak
amikor a Jarvis ráér (nem dolgozol vele, és a böngésző szabad: az ütemező háttérmunkája). Ha valami nem sikerül, később
újra próbálja; ha a levétel után az újrafeltöltés maradt el, hamar (REPOST_RETRY), mert addig a hirdetés nincs fent.
Előtte a hirdetés oldalát elolvassa: ha ott (kézzel) lejjebb vitted az árát, azt átveszi (ezzel tölti fel újra), ha
pedig elkeltnek vagy eltűntnek látszik, nem tölti fel újra.
"""
from __future__ import annotations

import datetime as dt
import random
import re
from typing import Any

from .textutil import fold

# Hol mit jelent a frissítés: „relist” (levétel és újrafeltöltés), „up” (az oldal ingyenes előre hozó gombja).
METHODS = {"vinted": "relist", "hardverapro": "up"}
# Miért nem: a felületnek és a modellnek.
UNSUPPORTED = {
    "jofogas": "a Jófogás szabályzata szerint egy törölt hirdetés terméke 14 napig nem adható fel újra (az előre "
               "sorolás ott fizetős), ezért ott nem frissítek",
    "vatera": "a Vaterán nem frissítek (ott a lejárat és a licitek a saját rendjük szerint mennek)",
}
DAYS = (1, 60)            # hány naponta (legalább, legfeljebb)
DEFAULT_DAYS = 7
DEFAULT_HOUR = 19         # este: ekkor böngésznek a legtöbben
JITTER_MINUTES = 45       # a megadott óra után ennyi percen belül, véletlenszerűen („hány óra körül”)
REPOST_RETRY = 30 * 60    # ha levette, de az újrafeltöltés elmaradt: ennyi múlva újra (addig nincs fent)
MAX_FAILS = 3             # ennyi sikertelen próbálkozás után kikapcsolja, és szól
# A Hardverapró ingyenes előre hozó gombja („UP”, néha egy számlálóval: „UP (7)”); a fizetős előresorolás nem az.
_UP_RE = re.compile(r"^(up|up\s*\(\d+\)|\d+\s*up|felhoz\w*|hirdetes felhozasa)$")
_UP_PAID_RE = re.compile(r"(elore ?sorol|kiemel|vasarl|fizet|csomag|extra|ultra|plusz)")


def method(platform: str) -> str:
    """Hogyan frissít ezen az oldalon („relist”, „up”), vagy „”, ha itt nem."""
    return METHODS.get(platform, "")


def clamp_days(value: Any) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Hány naponta: egy egész szám {DAYS[0]} és {DAYS[1]} között.") from None
    if not DAYS[0] <= days <= DAYS[1]:
        raise ValueError(f"Hány naponta: {DAYS[0]} és {DAYS[1]} nap között.")
    return days


def clamp_hour(value: Any) -> int:
    try:
        hour = int(value)
    except (TypeError, ValueError):
        raise ValueError("Hány óra körül: egy egész óra 0 és 23 között.") from None
    if not 0 <= hour <= 23:
        raise ValueError("Hány óra körül: 0 és 23 között.")
    return hour


def at_hour(day: dt.date, hour: int, jitter: float) -> float:
    """A nap adott órája (helyi idő) plusz `jitter` perc, időbélyegként."""
    return dt.datetime.combine(day, dt.time(hour)).timestamp() + jitter * 60


def next_time(base: float, days: int, hour: int, now: float, rng: random.Random | None = None) -> float:
    """Mikor legközelebb: a `base` (az utolsó frissítés, vagy amikor felkerült) után `days` nappal, `hour` óra körül
    (0–JITTER_MINUTES perccel utána). Ha ez már elmúlt (pl. régóta fent van), a legközelebbi ilyen óra (ma, ha még nem
    múlt el, különben holnap)."""
    jitter = (rng or random).uniform(0, JITTER_MINUTES)
    when = at_hour(dt.datetime.fromtimestamp(base).date() + dt.timedelta(days=days), hour, jitter)
    if when > now:
        return when
    today = dt.datetime.fromtimestamp(now).date()
    when = at_hour(today, hour, jitter)
    return when if when > now else at_hour(today + dt.timedelta(days=1), hour, jitter)


def when_text(timestamp: float, now: float) -> str:
    """„ma 19 óra körül”, „holnap 19 óra körül”, „okt. 31. 19 óra körül” (a felületnek és a naplónak)."""
    moment = dt.datetime.fromtimestamp(timestamp)
    day = (moment.date() - dt.datetime.fromtimestamp(now).date()).days
    months = ("jan.", "febr.", "márc.", "ápr.", "máj.", "jún.", "júl.", "aug.", "szept.", "okt.", "nov.", "dec.")
    label = "ma" if day == 0 else "holnap" if day == 1 else f"{months[moment.month - 1]} {moment.day}."
    return f"{label} {moment.hour} óra körül"


def up_button(elements: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A Hardverapró ingyenes „UP” gombja a letapogatott elemek közül (a fizetős előresorolás nem az)."""
    for element in elements:
        label = " ".join(fold(str(element.get("label") or "")).split())
        if element.get("kind") in ("button", "link", "input:submit", "input:button") and _UP_RE.match(label) \
                and not _UP_PAID_RE.search(label):
            return element
    return None
