"""Automata kereskedés (deals.py): olcsó hirdetések keresése, modell nélkül, csak a jelöltekre egy kérdés."""
from __future__ import annotations

import sys
import tempfile
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
    def __init__(self, results=None, fail=()):
        self.results, self.fail, self.urls = results or RESULTS, set(fail), []

    def extract_listings(self, url: str) -> list[dict]:
        self.urls.append(url)
        if any(f in url for f in self.fail):
            raise BrowserError("nem nyílt meg")
        return [dict(r) for r in self.results]


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
        self.hunter = self.make()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self) -> DealHunter:
        return DealHunter(Path(self.tmp.name), PLATFORMS, lambda: self.browser, self.llm)

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
        self.notices.clear()
        self.assertEqual(self.hunter.run(notify=self.notify), "1 keresés: nincs új olcsó hirdetés.")
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
        self.assertIn("Vinted: nem nyílt meg", result)
        self.assertEqual(len(self.browser.urls), 3, "a többi oldalt is megnézte")
        self.assertEqual(len(self.notices), 1, "ugyanaz a hirdetés több oldalon is: egyszer")


if __name__ == "__main__":
    unittest.main()
