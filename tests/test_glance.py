"""A Vinted friss listája (deals.GLANCE_*): a legújabb hirdetések alapból 20 percenként, egyetlen oldal, a többi oldal
munkamenetei közé ékelve, ahogy egy ember frissíti egész nap. Nem kér többet a Vintedtől, mint a régi, kétóránkénti
munkamenetek, de egy új hirdetés legfeljebb kb. 20 perc múlva kiderül (eddig akár 2,5 óra). A robot-ellenőrzésnél aznap
leáll; a gyakoriság a felületről állítható, és a régi munkamenetek is visszakapcsolhatók (0)."""
from __future__ import annotations

import random
import re
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_deals import NORMAL, PAGE, HunterCase, listing  # noqa: E402

from jarvis_core.deals import GLANCE_DEFAULT, DealError  # noqa: E402
from jarvis_core.journal import day_of  # noqa: E402

ELECTRONICS = "https://www.vinted.hu/catalog?catalog[]=2994&order=newest_first"


class GlanceTests(HunterCase):
    def setUp(self):
        super().setUp()
        self.hunter.rng = random.Random(3)
        self.hunter.book.add([{**r, "platform": "hardverapro"} for r in NORMAL])   # az árbázis ismeri az RTX-et
        self.browser.results = PAGE
        self.browser.by_url = [("vinted.hu", [listing(40, "Retró kütyü X100", 9000, "www.vinted.hu")])]
        self.loads: list[tuple[float, str]] = []
        extract = self.browser.extract_listings

        def timed(url: str) -> list[dict]:
            self.loads.append((self.clock.now, url))
            return extract(url)

        self.browser.extract_listings = timed
        self.hunter.set_enabled(True)

    def journal(self) -> list[str]:
        return [e["text"] for e in self.hunter.journal.entries()]

    def vinted(self) -> list[float]:
        return [at for at, url in self.loads if "vinted.hu/catalog" in url]

    def follow(self, minutes: int, sessions: bool = False) -> None:
        """Ennyi percig, ahogy az ütemező: percenként, ha van dolga (sessions: a Vinteden munkamenet is lehet)."""
        for _ in range(minutes):
            self.clock.now += 60
            if self.hunter.available():
                self.hunter.run(notify=self.notify)
                if not sessions:
                    self.assertNotEqual(self.hunter._data["session"].get("site"), "vinted",
                                        "a Vinteden nincs munkamenet")

    def test_the_newest_vinted_listings_every_twenty_minutes_between_the_other_sites(self):
        """Alapból 20 percenként (±20%) egy oldal a Vinted legújabbjaiból (az Elektronika kategória), a Hardverapró
        munkamenetei közé ékelve; a Vinteden nincs munkamenet, és három óra alatt nem több letöltés, mint régen (egy
        munkamenet 10 oldal volt, két óra szünettel), csak egyenletesen elosztva."""
        self.assertEqual(self.hunter.state()["browse"]["glance"], GLANCE_DEFAULT)
        self.hunter.set_browse(on=True, sites=["hardverapro", "vinted"])
        text = self.hunter.run(notify=self.notify)
        self.assertEqual(text, "Vinted, a legújabbak: Vinted: Elektronika: 1 hirdetés, 1 új.")
        self.assertEqual([url for _at, url in self.loads], [ELECTRONICS])
        self.assertRegex(self.hunter.note(), r"Vinted: a legújabbakat \d\d:\d\d-kor néztem, legközelebb kb\. \d\d:\d\d\.$")
        self.clock.now += 60
        self.assertTrue(self.hunter.run(notify=self.notify).startswith("Böngészés: Hardverapró: "),
                        "közben a többi oldal a szokásos munkamenetében")
        self.follow(180)
        looks = self.vinted()
        gaps = [(b - a) / 60 for a, b in zip(looks, looks[1:])]
        self.assertTrue(8 <= len(looks) <= 12, looks)
        self.assertTrue(all(15 <= gap <= 26 for gap in gaps), gaps)
        self.assertGreater(len([1 for _at, url in self.loads if "hardverapro.hu" in url]), 10, "a Hardverapró is ment")
        # az ismeretlen kütyü egyszer kerül a modellre váró sorba (a látottak nem jönnek újra)
        self.assertEqual([r["text"] for r in self.hunter._pending].count("Retró kütyü X100"), 1)
        # a naplóba óránként egy bejegyzés, nem mindegyik pillantásról külön
        hours = [t for t in self.journal() if ", a legújabbak: " in t]
        self.assertTrue(2 <= len(hours) <= 3, hours)
        self.assertRegex(hours[-1], r"^\d\d:\d\d–\d\d:\d\d Vinted, a legújabbak: [34] pillantás, \d+ hirdetés \(1 új\)\.$",
                         "az első órában a kütyü új volt")
        self.assertRegex(hours[0], r"Vinted, a legújabbak: [34] pillantás, \d+ hirdetés \(0 új\)\.$", "utána már nem")

    def test_a_cheap_new_vinted_listing_is_found_on_the_next_glance(self):
        """Egy új, olcsó hirdetés a következő pillantásnál kiderül (nem két óra múlva): az adatlapját megnézi, és szól."""
        self.hunter.set_browse(on=True, sites=["vinted"])
        self.hunter.run(notify=self.notify)
        self.assertEqual(self.notices, [])
        self.browser.by_url = [("vinted.hu", [listing(41, "Gigabyte RTX 3060 Ti Gaming OC", 60000, "www.vinted.hu"),
                                              listing(40, "Retró kütyü X100", 9000, "www.vinted.hu")])]
        self.follow(25)
        self.assertEqual(len(self.vinted()), 2, "a következő pillantás 16–24 perc múlva")
        self.assertIn("https://www.vinted.hu/apro/100041.html", self.browser.looked, "az adatlapját is megnézte")
        self.assertEqual([t for _k, t, _x, _e in self.notices], ["Olcsó: Gigabyte RTX 3060 Ti Gaming OC"])

    def test_several_vinted_pages_take_turns(self):
        """Több Vinted-indulóoldal felváltva (a legrégebben nézett jön), így a kérések száma ugyanannyi marad."""
        self.hunter.set_browse(on=True, sites=["vinted"])
        self.hunter.lane_add("Lego", "vinted")
        self.follow(70)
        urls = [url for _at, url in self.loads if "vinted.hu" in url]
        self.assertEqual(len(urls), 4)
        self.assertEqual([("Lego" in url) for url in urls], [False, True, False, True])
        self.assertIn("search_text=Lego", urls[1])

    def test_the_cadence_is_set_from_the_screen_and_kept(self):
        """A gyakoriság a felületről (10, 15, 20, 30, 60 perc, vagy 0: a régi munkamenetek), a naplóba is; az újraindítás
        után is megmarad. Ami nem ezek közül való, arra hibaüzenet."""
        self.hunter.set_browse(on=True, sites=["vinted"])
        self.hunter.run(notify=self.notify)
        first = self.vinted()[-1]
        state = self.hunter.set_browse(glance=10)
        self.assertEqual(state["browse"]["glance"], 10)
        self.assertEqual(self.journal()[0], "A Vinted legújabb hirdetéseit ezentúl 10 percenként nézem meg (egy oldal).")
        self.follow(12)
        self.assertEqual(self.vinted()[-1] - first, 600, "az új gyakoriság rögtön: a legutóbbitól 10 perc")
        self.assertEqual(self.make().state()["browse"]["glance"], 10, "megmarad")
        with self.assertRaises(DealError):
            self.hunter.set_browse(glance=7)
        state = self.hunter.set_browse(glance=0)
        self.assertEqual(self.journal()[0], "A Vintedet ezentúl kétóránként nézem, hosszabb munkamenetekben.")
        self.assertEqual(state["browse"]["glance_choices"], [0, 10, 15, 20, 30, 60])
        before = len(self.vinted())
        self.follow(15, sessions=True)
        self.assertEqual(self.hunter._data["session"].get("site"), "vinted", "a régi mód: munkamenet a Vinteden")
        self.assertGreater(len(self.vinted()), before)

    def test_a_captcha_stops_the_glances_for_the_day(self):
        """Ha a Vinted robot-ellenőrzést kér, aznap már nem néz rá (nem próbálja megkerülni), szól, és másnap folytatja."""
        self.browser.challenge = {"vinted.hu"}
        self.hunter.set_browse(on=True, sites=["vinted"])
        self.hunter.run(notify=self.notify)
        self.assertEqual([t for _k, t, _x, _e in self.notices], ["Vinted: robot-ellenőrzés"])
        self.assertIn("a legújabb hirdetéseit néztem", self.notices[0][2])
        self.assertEqual(self.hunter.note(), "Vinted: ma robot-ellenőrzést kért, holnap folytatom.")
        self.follow(120)
        self.assertEqual(len(self.vinted()), 1, "aznap nem próbálja újra")
        self.browser.challenge = set()
        self.clock.now += 86400
        self.assertTrue(self.hunter.available(), "másnap újra")

    def test_only_while_the_computer_is_on(self):
        """Csak a gép üzemidejében (a nap órái), mint a böngészés; az este félbemaradt óra reggel kerül a naplóba."""
        at = lambda d, h, m=0: time.mktime((2027, 1, d, h, m, 0, 0, 0, -1))  # noqa: E731
        self.hunter.set_day(8, 22)
        self.hunter.set_browse(on=True, sites=["vinted"])
        self.clock.now = at(15, 21, 30)
        self.hunter.run(notify=self.notify)
        self.follow(29)                        # 21:59-ig még egy pillantás
        evening = len(self.vinted())
        self.assertEqual(evening, 2)
        self.clock.now = at(15, 23)
        self.assertFalse(self.hunter.available(), "este nem")
        self.clock.now = at(16, 8, 1)
        self.assertTrue(self.hunter.available(), "reggel igen")
        self.hunter.run(notify=self.notify)
        evening = [e["text"] for e in self.hunter.journal.entries(day_of(at(15, 21)))]
        self.assertTrue(re.match(r"^21:30–21:\d\d Vinted, a legújabbak: 2 pillantás", evening[0]), evening)


if __name__ == "__main__":
    unittest.main()
