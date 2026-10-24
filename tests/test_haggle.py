"""Alkuképes hirdetések (deals.py): ami egy ideje fent van (vagy már leárazták), és csak kicsit drágább annál, ami még
megéri, arra ajánlatot javasol, egy másfajta értesítéssel (nem riaszt, a címe „Alku:”), a javasolt összeggel.

Futtatás a projekt mappájából:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_deals import HunterCase  # noqa: E402

from jarvis_core.deals import HAGGLE_EVERY, HAGGLE_PER_DAY  # noqa: E402

DAY = 86400
OLD = "https://hardverapro.hu/apro/asus_rtx_3060_ti_dual/900001.html"
OLD_PAGE = {"title": "ASUS RTX 3060 Ti Dual eladó - HardverApró",
            "text": "ASUS RTX 3060 Ti Dual. Hibátlan, dobozában. Költözés miatt sürgős. Eladó: Peti (12 értékelés)"}


def row(n: int, price: int, site: str = "hardverapro", url: str = "") -> dict:
    host = {"hardverapro": "hardverapro.hu/apro", "jofogas": "www.jofogas.hu/budapest", "vinted": "www.vinted.hu/items"}
    return {"text": f"RTX 3060 Ti videókártya {n}", "price": price, "platform": site,
            "url": url or f"https://{host[site]}/rtx_{n}/{800000 + n}.html"}


class HaggleCase(HunterCase):
    def setUp(self):
        super().setUp()
        # tíz napja fent van egy kicsit drága RTX 3060 Ti; a szokásos ára két oldalról kb. 100 000 Ft
        self.hunter.book.add([row(0, 95000, url=OLD)])
        self.clock.now += 10 * DAY
        self.market()
        self.browser.pages[OLD] = OLD_PAGE

    def market(self, extra: list | None = None) -> None:
        """A friss árak (ma is láttuk őket), a régi hirdetéssel együtt."""
        self.hunter.book.add([row(0, 95000, url=OLD)] + [row(n, p, site) for n, (p, site) in enumerate(
            ((98000, "hardverapro"), (100000, "jofogas"), (102000, "hardverapro"), (99000, "jofogas"),
             (101000, "hardverapro")), 1)] + (extra or []))

    def haggles(self) -> list[tuple]:
        return [n for n in self.notices if n[0] == "haggle"]


class HaggleTests(HaggleCase):
    def test_an_old_slightly_expensive_listing_gets_an_offer(self):
        """Tíz napja fent van, és csak kb. 12%-kal drágább annál, ami még megéri: megnyitja (fent van-e, fix áras-e),
        egy rövid modellkérdés után szól, de nem riasztással: a címe „Alku:”, és a találat ablakában az ajánlat."""
        [candidate] = self.hunter.haggle_candidates()
        # a szokásos ár 99 500 Ft: óvatosan 89 550-ért adhatod el, a posta (1 500) nélkül 88 050; hogy a haszon a vételár
        # legalább 10%-a legyen (különben kockázatos), legfeljebb 80 045-öt érdemes ajánlani
        self.assertEqual((candidate["url"], candidate["days"], candidate["offer"], candidate["cut"]), (OLD, 10, 80000, 0.16))
        self.hunter.set_enabled(True)
        self.assertTrue(self.hunter.available(), "az árbázis átnézése esedékes")
        self.assertEqual(self.hunter.run(notify=self.notify), "1 alkuképes hirdetés.")
        self.assertEqual(self.browser.looked, [OLD])
        self.assertEqual(self.llm.checks(), 1, "egy rövid modellkérdés")
        [(kind, title, text, extra)] = self.notices
        self.assertEqual((kind, title), ("haggle", "Alku: ASUS RTX 3060 Ti Dual eladó"))
        self.assertIn("95 000 Ft (10 napja fent; a hirdetésben: sürgős, költözés miatt)", text)
        self.assertIn("Ha 80 000 Ft-ért odaadja (16%-kal kevesebb), kb. 8 050 Ft a haszon.", text)
        self.assertNotIn("urgent", extra, "nem riaszt, mint egy megérő találat")
        self.assertNotIn("deal", extra, "a telefon nem a megérő hirdetés riasztását indítja")
        self.assertEqual(extra["actions"][0]["action"], "deal")
        [deal] = self.hunter.state()["deals"]
        self.assertEqual((deal["id"], deal["haggle"]["offer"], deal["title"]), (extra["haggle"], 80000,
                                                                                "ASUS RTX 3060 Ti Dual eladó"))
        self.assertEqual(deal["haggle"]["flex"], ["sürgős", "költözés miatt"])
        self.assertEqual((deal["profit"], deal["safety"]["level"]), (8050, "jó"))
        self.assertTrue(any(e["text"].startswith("Alkuképes: „ASUS RTX 3060 Ti Dual eladó” 95 000 Ft (Hardverapró), 10 "
                                                 "napja fent: ajánlj 80 000 Ft-ot")
                            for e in self.hunter.journal.entries()))
        self.notices.clear()
        self.assertFalse(self.hunter.available(), f"{HAGGLE_EVERY} percig nem nézi át újra")
        self.clock.now += HAGGLE_EVERY * 60
        self.hunter.run(notify=self.notify, force=True)
        self.assertEqual(self.notices, [], "egy hirdetésről egyszer szól")

    def test_which_listings_are_worth_an_offer(self):
        """Csak ami legalább egy hete fent van (vagy leárazták), legfeljebb 25%-kal drágább annál, ami megéri, és nem
        olcsó már most is (arról a többi út szól)."""
        fresh = row(20, 95000)
        self.hunter.book.add([fresh, row(21, 130000, url="https://hardverapro.hu/apro/x/900021.html")])
        urls = [c["url"] for c in self.hunter.haggle_candidates()]
        self.assertEqual(urls, [OLD], "a friss és a túl drága kimarad")
        self.hunter.book.add([{**fresh, "price": 90000}])      # leárazta: már friss is lehet
        [cut] = [c for c in self.hunter.haggle_candidates() if c["url"] == fresh["url"]]
        self.assertEqual((cut["cuts"], cut["p0"], cut["days"]), (1, 95000, 0))
        self.hunter.book.add([{**fresh, "price": 80000}])      # most már olcsó: arról a megérő találat szól
        self.assertNotIn(fresh["url"], [c["url"] for c in self.hunter.haggle_candidates()])
        self.clock.now += 3 * DAY                              # a régit két napja nem láttuk: lehet, hogy elkelt
        self.assertEqual(self.hunter.haggle_candidates(), [])

    def test_a_fixed_price_is_not_haggled(self):
        self.browser.pages[OLD] = {**OLD_PAGE, "text": "ASUS RTX 3060 Ti Dual. Fix ár, alku nincs. Eladó: Peti (12 értékelés)"}
        self.hunter.set_enabled(True)
        self.hunter.run(notify=self.notify)
        self.assertEqual(self.notices, [])
        self.assertTrue(any(e["text"].startswith("Fix áras, nem ajánlottam alkut") for e in self.hunter.journal.entries()))
        self.assertEqual(self.hunter.haggle_candidates(), [], "többé nem jön elő")

    def test_only_a_few_notifications_a_day_and_it_can_be_turned_off(self):
        with self.hunter._lock:
            self.hunter._data["haggle"]["told"] = {"day": self._today(), "n": HAGGLE_PER_DAY}
        self.hunter.set_enabled(True)
        self.hunter.run(notify=self.notify)
        [(_kind, _title, _text, extra)] = self.haggles()
        self.assertTrue(extra["quiet"], "a napi keret felett csak a listában és az Értesítések között")
        state = self.hunter.set_haggle(on=False, message="Szia! {ár}-ért odaadnád?")
        self.assertEqual((state["haggle"]["on"], state["haggle"]["message"]), (False, "Szia! {ár}-ért odaadnád?"))
        self.clock.now += HAGGLE_EVERY * 60
        self.assertFalse(self.hunter.available())
        self.assertEqual(self.hunter.set_haggle(message="")["haggle"]["message"].splitlines()[0], "Szia!",
                         "üresen az alapüzenet")

    def test_a_dismissed_offer_does_not_mute_the_product(self):
        self.hunter.set_enabled(True)
        self.hunter.run(notify=self.notify)
        [deal] = self.hunter.state()["deals"]
        self.hunter.dismiss(deal["id"])
        self.assertNotIn("3060 rtx ti", self.hunter._data["feedback"])

    def _today(self) -> str:
        from jarvis_core.journal import day_of
        return day_of(self.clock.now)


if __name__ == "__main__":
    unittest.main()
