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
        # a szokásos ár (a hirdetés saját ára nélkül: önmagához nem mérhető) 100 000 Ft: óvatosan 90 000-ért adhatod el,
        # a posta (1 500) nélkül 88 500; hogy a haszon a vételár legalább 10%-a legyen (különben kockázatos), legfeljebb
        # 80 454-et érdemes ajánlani
        self.assertEqual((candidate["url"], candidate["days"], candidate["offer"], candidate["cut"]), (OLD, 10, 80400, 0.15))
        self.hunter.set_enabled(True)
        self.assertTrue(self.hunter.available(), "az árbázis átnézése esedékes")
        self.assertEqual(self.hunter.run(notify=self.notify), "1 alkuképes hirdetés.")
        self.assertEqual(self.browser.looked, [OLD])
        self.assertEqual(self.llm.checks(), 1, "egy rövid modellkérdés")
        [(kind, title, text, extra)] = self.notices
        self.assertEqual((kind, title), ("haggle", "Alku: ASUS RTX 3060 Ti Dual eladó"))
        self.assertIn("95 000 Ft (10 napja fent; a hirdetésben: sürgős, költözés miatt)", text)
        self.assertIn("Ha 80 400 Ft-ért odaadja (15%-kal kevesebb), kb. 8 100 Ft a haszon.", text)
        self.assertNotIn("urgent", extra, "nem riaszt, mint egy megérő találat")
        self.assertNotIn("deal", extra, "a telefon nem a megérő hirdetés riasztását indítja")
        self.assertEqual(extra["actions"][0]["action"], "deal")
        [deal] = self.hunter.state()["deals"]
        self.assertEqual((deal["id"], deal["haggle"]["offer"], deal["title"]), (extra["haggle"], 80400,
                                                                                "ASUS RTX 3060 Ti Dual eladó"))
        self.assertEqual(deal["haggle"]["flex"], ["sürgős", "költözés miatt"])
        self.assertEqual((deal["profit"], deal["safety"]["level"]), (8100, "jó"))
        self.assertTrue(any(e["text"].startswith("Alkuképes: „ASUS RTX 3060 Ti Dual eladó” 95 000 Ft (Hardverapró), 10 "
                                                 "napja fent: ajánlj 80 400 Ft-ot")
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


LITE = "https://hardverapro.hu/apro/nintendo_switch_lite_szurke/900500.html"


class HaggleCostTests(HunterCase):
    """Az alku ugyanazokon az ellenőrzéseken megy át, mint a többi jelölt (_reprice): az ajánlott összegre, az állapot
    szerinti teljes költséggel. A Switch Lite szokásos ára 59 500 Ft (óvatosan 53 550-ért adható el): a javasolt
    ajánlat 47 000 Ft, ami hibátlan állapotban kb. 5 050 Ft haszon."""

    def setUp(self):
        super().setUp()
        self.hunter.book.add([{"text": "Nintendo Switch Lite szürke", "price": 55000, "platform": "hardverapro", "url": LITE}])
        self.clock.now += 10 * DAY
        comps = ((58000, "hardverapro"), (59000, "jofogas"), (59500, "hardverapro"), (60000, "jofogas"),
                 (61000, "hardverapro"))
        self.hunter.book.add([{"text": "Nintendo Switch Lite szürke", "price": 55000, "platform": "hardverapro", "url": LITE}]
                             + [{"text": f"Nintendo Switch Lite {color}", "price": p, "platform": site,
                                 "url": f"https://{'hardverapro.hu/apro' if site == 'hardverapro' else 'www.jofogas.hu/b'}"
                                        f"/lite_{n}/90060{n}.html"}
                                for n, ((p, site), color) in enumerate(zip(comps, ("kék", "türkiz", "korall", "sárga",
                                                                                   "fekete")))])
        self.page("Nintendo Switch Lite. A kijelzője repedt (világosabb folt is van rajta), töltő nélkül adom. "
                  "Egyébként működik. Eladó: Ancsa (8 értékelés)")
        self.hunter.set_enabled(True)

    def page(self, text: str) -> None:
        self.browser.pages[LITE] = {"title": "Nintendo Switch Lite szürke - HardverApró", "text": text}

    def haggles(self) -> list[tuple]:
        return [n for n in self.notices if n[0] == "haggle"]

    def test_the_offer_is_counted_with_the_repair_and_the_missing_charger(self):
        """Reprodukált hiba: 47 000 Ft-os ajánlatnál +5 050 Ft hasznot jelzett, pedig felismerte a repedt kijelzőt és a
        hiányzó töltőt; a megadott 15 000 Ft javítás és 3 000 Ft töltő kimaradt. Ezekkel −12 950 Ft: nem ajánl alkut
        (egy kisebb ajánlat is 25%-nál nagyobb engedmény lenne), és a naplóban ott a teljes számolás."""
        [candidate] = self.hunter.haggle_candidates()
        self.assertEqual((candidate["offer"], candidate["median"]), (47000, 59500))
        self.hunter.add_cost("kijelző", "Switch Lite", 15000)
        self.hunter.add_cost("töltő", "", 3000)
        self.hunter.run(notify=self.notify)
        self.assertEqual(self.haggles(), [], "nem ajánl alkut")
        self.assertEqual(self.hunter.state()["deals"], [])
        [line] = [e["text"] for e in self.hunter.journal.entries() if e["text"].startswith("Alkuval sem éri meg")]
        self.assertIn("47 000 Ft-ért a teljes költséggel (levonva: posta 1 500 Ft, kijelző javítása 15 000 Ft, töltő "
                      "pótlása 3 000 Ft) kb. -12 950 Ft lenne a haszon", line)

    def test_a_smaller_offer_when_it_still_pays_with_the_costs(self):
        """Ha csak a töltő hiányzik (3 000 Ft), egy kisebb ajánlat még megéri: azt javasolja, a haszon a teljes
        költséggel számolva (és az értesítésben is látszik, mit vont le)."""
        self.page("Nintendo Switch Lite. Hibátlanul működik, a kijelzője ép, töltő nélkül adom. Eladó: Ancsa (8 értékelés)")
        self.hunter.add_cost("töltő", "", 3000)
        self.hunter.run(notify=self.notify)
        [(_kind, _title, text, _extra)] = self.haggles()
        [deal] = self.hunter.state()["deals"]
        self.assertEqual(deal["haggle"]["offer"], 44000)
        self.assertEqual(deal["profit"], 53550 - 44000 - 1500 - 3000)
        self.assertEqual([c["what"] for c in deal["cost_parts"]], ["posta", "töltő pótlása"])
        self.assertIn("Ha 44 000 Ft-ért odaadja (20%-kal kevesebb), kb. 5 050 Ft a haszon (a posta mellett levonva: "
                      "töltő pótlása kb. 3 000 Ft).", text)

    def test_an_unknown_repair_cost_is_uncertain_not_zero(self):
        """Ha a kijelző javításának költségét nem adtad meg (és nincs hasonló hibájú összehasonlító), az alku
        bizonytalan: nem jön alku-értesítés pozitív haszonnal, hanem az Érdemes megnézni listába kerül, az okkal (a
        hiányzó költség nem nulla)."""
        self.hunter.add_cost("töltő", "", 3000)
        self.hunter.run(notify=self.notify)
        self.assertEqual(self.haggles(), [])
        self.assertEqual(self.hunter.state()["deals"], [])
        [tip] = self.hunter.state()["tips"]
        self.assertTrue(tip["unverified"])
        self.assertIn("alkuval, de a leírása szerint hibás a kijelzője, és nincs hozzá hasonló hibájú összehasonlító "
                      "vagy megadott javítási költség", tip["why"])


if __name__ == "__main__":
    unittest.main()
