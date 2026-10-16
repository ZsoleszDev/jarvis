"""Emberi tempó a Jarvis böngészőjében.

A hirdetési oldalak robotszűrői a gépies viselkedést figyelik: a pillanatok alatt kitöltött űrlapot,
a billentyűleütések nélkül megjelenő szöveget, az egérmozgás nélküli kattintást. Ezért a Jarvis
böngészője úgy kezeli az oldalt, ahogy egy ember:

- kattintás előtt az egér odamegy az elemhez (enyhe ívben, lassan indulva és megállva, nem pont a
  közepére), és a gomb lenyomása is tart egy pillanatig;
- a rövid szöveget (cím, ár, márka) billentyűnként gépeli, változó tempóban; a hosszút (leírás)
  beilleszti, ahogy az ember a jegyzeteiből másolja;
- a lejjebb lévő mezőhöz az egérgörgővel teker oda, több kisebb „kattanásban” (az oldal nem ugrik);
- a modell nélküli lépéssoroknál (begyakorolt lépések, árkutatás) gondolkodási és olvasási szüneteket
  is tart: ott nincs modell, ami magától lassítana, és egy ember sem tölt ki egy űrlapot 3 másodperc
  alatt.

A szünetek hossza a [browser] pace beállítással szorozható (0 = szünetek nélkül, pl. a tesztekben).
"""
from __future__ import annotations

import math
import random
from typing import Callable

LONG_TEXT = 60          # ennél hosszabb (vagy többsoros) szöveget beilleszt, nem gépel
TYPING_MEDIAN = 0.11    # másodperc két leütés között (kb. 8–9 leütés/mp, egy gyakorlott gépelő)


class Pace:
    """Az emberi tempó paraméterei és véletlenje. A böngésző adja meg, hogyan kell várni (sleep)."""

    def __init__(self, enabled: bool = True, factor: float = 1.0, rng: random.Random | None = None):
        self.enabled = bool(enabled)
        self.factor = max(0.0, float(factor))
        self.rng = rng or random.Random()
        self.sleep: Callable[[float], None] = lambda _seconds: None
        self.waited = 0.0       # összesen mennyit várt (napló és tesztek)

    @property
    def slow(self) -> bool:
        """Tart-e tényleges szüneteket (a tesztekben a pace = 0: emberi mozdulatok, de várakozás nélkül)."""
        return self.enabled and self.factor > 0

    def wait(self, low: float, high: float) -> float:
        """Véletlen szünet low és high másodperc között (a szorzóval). Visszaadja, mennyit várt."""
        if not self.slow:
            return 0.0
        seconds = self.rng.uniform(low, high) * self.factor
        if seconds > 0:
            self.sleep(seconds)
            self.waited += seconds
        return seconds

    # --- szünetek a lépések között (a modell nélküli lépéssoroknál) ---------------------------------

    def think(self) -> float:
        """Mielőtt a következő mezőhöz vagy gombhoz nyúl."""
        return self.wait(0.5, 1.5)

    def read(self) -> float:
        """Egy új oldal után körülnéz."""
        return self.wait(1.2, 2.8)

    def pick_files(self) -> float:
        """A fotók kiválasztása a fájlválasztóban."""
        return self.wait(1.5, 3.5)

    def review(self) -> float:
        """A közzététel (vagy törlés) előtt átnézi, amit kitöltött."""
        return self.wait(1.5, 3.5)

    # --- billentyűzet és egér -----------------------------------------------------------------------

    def key_delays(self, text: str) -> list[float]:
        """Szünet minden leütés után (másodperc): változó tempó, szóköz és írásjel után kicsit hosszabb,
        és néha egy rövid megtorpanás, ahogy az ember a következő szón gondolkodik."""
        delays = []
        for ch in text:
            delay = self.rng.lognormvariate(math.log(TYPING_MEDIAN), 0.35)
            if ch in " ,.;:!?-/()":
                delay += self.rng.uniform(0.04, 0.18)
            if self.rng.random() < 0.04:
                delay += self.rng.uniform(0.3, 0.8)
            delays.append(min(delay, 1.5) * self.factor if self.slow else 0.0)
        return delays

    def press_ms(self) -> float:
        """Meddig van lenyomva az egérgomb egy kattintásnál (ezredmásodperc)."""
        return self.rng.uniform(45, 120) * self.factor if self.slow else 0.0

    def point_in(self, box: dict[str, float]) -> tuple[float, float]:
        """Kattintási pont egy elemen belül: nem pont a közepe, de nem is a széle."""
        return (box["x"] + box["width"] * self.rng.uniform(0.3, 0.7),
                box["y"] + box["height"] * self.rng.uniform(0.3, 0.7))

    def path(self, start: tuple[float, float], end: tuple[float, float]) -> list[tuple[float, float]]:
        """Az egér útja két pont között: enyhe ív (másodfokú Bézier-görbe), lassan indulva és megállva."""
        (x0, y0), (x1, y1) = start, end
        dist = math.hypot(x1 - x0, y1 - y0)
        steps = max(6, min(28, int(dist / 28)))
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        nx, ny = -(y1 - y0), (x1 - x0)
        norm = math.hypot(nx, ny) or 1.0
        bend = self.rng.uniform(-0.2, 0.2) * dist
        cx, cy = mx + nx / norm * bend, my + ny / norm * bend
        points = []
        for i in range(1, steps + 1):
            t = i / steps
            t = t * t * (3 - 2 * t)
            points.append(((1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t ** 2 * x1,
                           (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t ** 2 * y1))
        return points

    def move_seconds(self, start: tuple[float, float], end: tuple[float, float]) -> float:
        """Mennyi ideig tart az egér útja (a távolsággal nő, de egy mozdulat ritkán több fél másodpercnél)."""
        if not self.slow:
            return 0.0
        dist = math.hypot(end[0] - start[0], end[1] - start[1])
        return min(0.75, 0.12 + dist / 1600 + self.rng.uniform(0.0, 0.12)) * self.factor
