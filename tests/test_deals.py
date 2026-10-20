"""Automata kereskedés (deals.py): olcsó hirdetések keresése, modell nélkül, csak a jelöltekre egy kérdés."""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis_core.browser import BrowserError  # noqa: E402
from jarvis_core.deals import DealError, DealHunter  # noqa: E402
from jarvis_core.selling import PLATFORMS  # noqa: E402


def listing(n: int, text: str, price: int, site: str = "hardverapro.hu") -> dict:
    return {"text": text, "price": price, "url": f"https://{site}/apro/{100000 + n}.html"}


# Egy találati oldal: hét szokásos árú RTX 3060 Ti (90–110 ezer), egy olcsó, egy kereső, egy hibás és egy más termék.
RESULTS = [listing(n, f"RTX 3060 Ti videókártya {n}", p) for n, p in
           enumerate((95000, 100000, 105000, 98000, 110000, 92000, 102000))] + [
    listing(10, "Gigabyte RTX 3060 Ti Gaming OC", 55000),
    listing(11, "Keresek RTX 3060 Ti kártyát", 40000),
    listing(12, "RTX 3060 Ti hibás, alkatrésznek", 30000),
    listing(13, "Laptop táska", 5000),
]


class FakeBrowser:
    """A keresők találatai, és egy-egy hirdetés oldala (pages: webcím → a lap szövege, vagy a lap adatai)."""

    def __init__(self, results=None, fail=()):
        self.results, self.fail, self.urls, self.looked = results or RESULTS, set(fail), [], []
        self.pages: dict[str, object] = {}

    def extract_listings(self, url: str) -> list[dict]:
        self.urls.append(url)
        if any(f in url for f in self.fail):
            raise BrowserError("nem nyílt meg")
        return [dict(r) for r in self.results]

    def look(self, url: str) -> dict:
        self.looked.append(url)
        page = self.pages.get(url, "Gigabyte RTX 3060 Ti Gaming OC. Hibátlan, dobozában. Eladó: Peti (12 értékelés)")
        if isinstance(page, dict):
            return {"url": url, "title": "", "short": [], "labels": [], "captcha": False, "text": "", **page}
        return {"url": url, "title": "Hirdetés", "text": page, "short": [], "labels": [], "captcha": False}


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


CHEAP = "https://hardverapro.hu/apro/100010.html"


class FakeLlm:
    def __init__(self, ok: bool = True):
        self.ok, self.calls = ok, []

    def chat_json(self, messages, schema, **kwargs):
        self.calls.append(messages)
        return {"ok": self.ok, "reason": "Ugyanaz a kártya." if self.ok else "Csak a doboza."}


class DealTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.browser = FakeBrowser()
        self.llm = FakeLlm()
        self.notices: list[tuple] = []
        self.clock = Clock()
        self.hunter = self.make()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self) -> DealHunter:
        return DealHunter(Path(self.tmp.name), PLATFORMS, lambda: self.browser, self.llm, clock=self.clock)

    def notify(self, kind, title, text, **extra):
        self.notices.append((kind, title, text, extra))

    def test_off_by_default_and_does_nothing(self):
        self.hunter.add_search("RTX 3060 Ti", ["hardverapro"])
        self.assertFalse(self.hunter.available())
        self.assertEqual(self.hunter.run(notify=self.notify), "Az automata kereskedés ki van kapcsolva.")
        self.assertEqual(self.browser.urls, [])

    def test_finds_the_cheap_one_and_asks_the_model_only_about_it(self):
        self.hunter.add_search("RTX 3060 Ti", ["hardverapro"])
        self.hunter.set_enabled(True)
        self.assertTrue(self.hunter.available())
        result = self.hunter.run(notify=self.notify)
        self.assertEqual(result, "1 keresés: 1 olcsó hirdetés.")
        self.assertEqual(len(self.llm.calls), 1, "modell csak a jelöltre (a kereső, a hibás és a más termék kiesik)")
        [(kind, title, text, extra)] = self.notices
        self.assertEqual(kind, "deal")
        self.assertIn("Gigabyte RTX 3060 Ti", title)
        self.assertIn("55 000 Ft", text)
        self.assertEqual(extra["url"], "https://hardverapro.hu/apro/100010.html")
        [deal] = self.hunter.state()["deals"]
        self.assertEqual((deal["price"], deal["median"], deal["margin"]), (55000, 100000, 45000))
        self.assertTrue(deal["checked"])
        self.assertEqual(self.make().state()["deals"][0]["id"], deal["id"], "a gépen megmarad")
        self.assertEqual(self.browser.looked, [CHEAP], "csak a jelölt oldalát nyitotta meg")
        self.assertIn("Hibátlan, dobozában", self.llm.calls[0][1]["content"], "a modell a hirdetés leírását is látja")
        self.notices.clear()
        self.assertEqual(self.hunter.run(notify=self.notify, force=True), "1 keresés: nincs új olcsó hirdetés.")
        self.assertEqual((self.notices, len(self.llm.calls)), ([], 1), "egy hirdetésről egyszer szól")

    def test_the_model_can_say_no_and_without_a_model_it_still_tells(self):
        self.llm.ok = False
        self.hunter.add_search("RTX 3060 Ti", ["hardverapro"])
        self.hunter.set_enabled(True)
        self.hunter.run(notify=self.notify)
        self.assertEqual(self.notices, [])
        other = DealHunter(Path(self.tmp.name) / "masik", PLATFORMS, lambda: self.browser, None)
        other.add_search("RTX 3060 Ti", ["hardverapro"])
        other.set_enabled(True)
        other.run(notify=self.notify)
        self.assertEqual(len(self.notices), 1, "modell nélkül is szól (nem ellenőrzött)")

    def test_max_price_discount_and_too_few_prices(self):
        self.hunter.add_search("RTX 3060 Ti", ["hardverapro"], max_price=50000)
        self.hunter.set_enabled(True)
        self.hunter.run(notify=self.notify)
        self.assertEqual(self.notices, [], "55 ezer a megadott 50 ezres határ fölött")
        self.browser.results = RESULTS[:3] + RESULTS[7:8]
        other = DealHunter(Path(self.tmp.name) / "keves", PLATFORMS, lambda: self.browser, self.llm)
        other.add_search("RTX 3060 Ti", ["hardverapro"], discount=20)
        other.set_enabled(True)
        other.run(notify=self.notify)
        self.assertEqual(self.notices, [], "kevés hasonló árból nem mond szokásos árat")

    def test_searches_validate_and_a_broken_site_does_not_stop_the_rest(self):
        with self.assertRaises(DealError):
            self.hunter.add_search("  ")
        state = self.hunter.add_search("Nike Air Max 42", ["vinted", "nincs ilyen"], discount=95)
        [search] = state["searches"]
        self.assertEqual((search["sites"], search["discount"]), (["vinted"], 80))
        self.assertEqual([s["key"] for s in state["sites"]], ["vinted", "jofogas", "hardverapro"])
        self.hunter.remove_search(search["id"])
        self.hunter.add_search("RTX 3060 Ti")
        self.hunter.set_enabled(True)
        self.browser.fail = {"vinted"}
        result = self.hunter.run(notify=self.notify)
        self.assertIn("order=newest_first", self.browser.urls[0], "a Vinteden a legújabb elöl")
        self.assertIn("Vinted: nem nyílt meg", result)
        self.assertEqual(len(self.browser.urls), 3, "a többi oldalt is megnézte")
        self.assertEqual(len(self.notices), 1, "ugyanaz a hirdetés több oldalon is: egyszer")


    def test_each_search_runs_at_its_own_pace_and_vinted_at_most_every_two_hours(self):
        self.hunter.add_search("RTX 3060 Ti", ["hardverapro"], every=30)
        self.hunter.add_search("Nike Air Max 42", ["vinted"], every=30)
        self.hunter.add_search("PS5 kontroller", ["jofogas"], every=360)
        self.assertEqual(self.hunter.every_minutes(), 0.0, "kikapcsolva nem fut")
        self.hunter.set_enabled(True)
        self.assertEqual(self.hunter.every_minutes(), 30.0, "a leggyakoribb keresés szerint")
        self.hunter.run(notify=self.notify)
        self.assertEqual(len(self.browser.urls), 3)
        self.assertEqual(self.hunter.run(notify=self.notify), "Most egyik keresés sem esedékes.")
        self.clock.now += 31 * 60
        self.browser.urls.clear()
        self.hunter.run(notify=self.notify)
        self.assertEqual([u.split("/")[2] for u in self.browser.urls], ["hardverapro.hu"],
                         "fél óra múlva csak a Hardverapró (a Vinted kétóránként, a harmadik hatóránként)")
        self.clock.now += 90 * 60
        self.browser.urls.clear()
        self.hunter.run(notify=self.notify)
        self.assertEqual(sorted(u.split("/")[2] for u in self.browser.urls), ["hardverapro.hu", "www.vinted.hu"])
        self.browser.urls.clear()
        self.hunter.run(notify=self.notify, force=True)
        self.assertEqual(len(self.browser.urls), 3, "Keresés most: mind")

    def test_the_listing_page_is_checked_before_the_model(self):
        cases = {
            "Csak előre utalással, külföldön vagyok, postázom.": None,
            "A kártya működik, de a hűtő ventilátora repedt.": None,
            "Szia, írj WhatsAppon: +36 30 123 4567": "az oldalon kívüli elérhetőséget ad meg",
            "Hibátlan. Az eladónak még nincs értékelése.": "az eladónak még nincs értékelése",
        }
        for n, (page, warning) in enumerate(cases.items()):
            with self.subTest(page=page):
                self.notices.clear()
                self.llm.calls.clear()
                self.browser.pages = {CHEAP: page}
                hunter = DealHunter(Path(self.tmp.name) / str(n), PLATFORMS, lambda: self.browser, self.llm)
                hunter.add_search("RTX 3060 Ti", ["hardverapro"])
                hunter.set_enabled(True)
                hunter.run(notify=self.notify)
                if warning is None:
                    self.assertEqual((self.notices, self.llm.calls), ([], []), "gyanús vagy hibás: modell sem kell")
                else:
                    [notice] = self.notices
                    self.assertIn(f"Figyelj: {warning}.", notice[2])
                    self.assertEqual(hunter.state()["deals"][0]["warnings"], [warning])

    def test_sold_listing_is_skipped_and_a_captcha_stops_the_page_reads(self):
        self.browser.pages = {CHEAP: {"short": ["Eladva"], "text": "Gigabyte RTX 3060 Ti"}}
        self.hunter.add_search("RTX 3060 Ti", ["hardverapro"])
        self.hunter.set_enabled(True)
        self.hunter.run(notify=self.notify)
        self.assertEqual(self.notices, [], "már elkelt")
        self.browser.pages = {CHEAP: {"captcha": True}}
        other = DealHunter(Path(self.tmp.name) / "c", PLATFORMS, lambda: self.browser, self.llm)
        other.add_search("RTX 3060 Ti", ["hardverapro"])
        other.set_enabled(True)
        other.run(notify=self.notify)
        [notice] = self.notices
        self.assertIn("nem néztem meg (robot-ellenőrzés)", notice[2], "captchánál szól, de jelzi, hogy nem nézte meg")



class ProfitTests(unittest.TestCase):
    def test_profit_month_total_days_and_money_still_in_items(self):
        from jarvis_core.deals import profit_report
        from jarvis_core.selling import ItemStore
        with tempfile.TemporaryDirectory() as tmp:
            store = ItemStore(Path(tmp))
            now = time.time()
            sold = store.create(title="RTX 3060 Ti", price=95000, bought_price=55000, bought_at=now - 10 * 86400)
            store.mark(sold.id, sold=True)
            self.assertEqual(store.get(sold.id).sold_price, 95000, "az Eladva jelöléskor az akkori ár")
            old = store.create(title="PS5 kontroller", bought_price=9000, sold_price=12000, bought_at=now - 90 * 86400)
            store.mark(old.id, sold=True)
            store.update(old.id, sold_at=now - 60 * 86400)
            store.create(title="Nike Air Max", bought_price=15000)
            store.create(title="Saját póló", price=2000)          # nem vett tárgy: nem számít
            report = profit_report(store.all(), now)
        self.assertEqual(report["month"], {"profit": 40000, "count": 1})
        self.assertEqual(report["total"], {"profit": 43000, "count": 2, "spent": 64000, "revenue": 107000})
        self.assertEqual(report["open"], {"count": 1, "spent": 15000})
        self.assertEqual(report["days"], 20, "átlagosan (10 + 30) / 2 nap alatt kelt el")
        self.assertEqual([r["name"] for r in report["rows"]], ["Nike Air Max", "PS5 kontroller", "RTX 3060 Ti"])


if __name__ == "__main__":
    unittest.main()
