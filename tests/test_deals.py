"""Automata kereskedés (deals.py): a szokásos ár felmérése (egy modellhívás, jóváhagyással), utána modell nélküli
keresés ehhez az árhoz mérve, és csak a jelöltekre egy rövid modellkérdés."""
from __future__ import annotations

import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis_core.browser import BrowserError  # noqa: E402
from jarvis_core.deals import DealError, DealHunter, expected_profit  # noqa: E402
from jarvis_core.selling import PLATFORMS  # noqa: E402


def listing(n: int, text: str, price: int, site: str = "hardverapro.hu") -> dict:
    return {"text": text, "price": price, "url": f"https://{site}/apro/{100000 + n}.html"}


# Egy találati oldal: hét szokásos árú RTX 3060 Ti (92–110 ezer), egy olcsó, egy kereső, egy hibás és egy más termék.
NORMAL = [listing(n, f"RTX 3060 Ti videókártya {n}", p) for n, p in
          enumerate((95000, 100000, 105000, 98000, 110000, 92000, 102000))]
RESULTS = NORMAL + [
    listing(10, "Gigabyte RTX 3060 Ti Gaming OC", 55000),
    listing(11, "Keresek RTX 3060 Ti kártyát", 40000),
    listing(12, "RTX 3060 Ti hibás, alkatrésznek", 30000),
    listing(13, "Laptop táska", 5000),
]
CHEAP = "https://hardverapro.hu/apro/100010.html"


class FakeBrowser:
    """A keresők találatai, és egy-egy hirdetés oldala (pages: webcím → a lap szövege, vagy a lap adatai)."""

    def __init__(self, results=None, fail=()):
        self.results, self.fail, self.urls, self.looked = list(results or RESULTS), set(fail), [], []
        self.pages: dict[str, object] = {}

    def extract_listings(self, url: str) -> list[dict]:
        self.urls.append(url)
        if any(f in url for f in self.fail):
            raise BrowserError("nem nyílt meg")
        return [dict(r) for r in self.results]

    def look(self, url: str) -> dict:
        self.looked.append(url)
        page = self.pages.get(url, "RTX 3060 Ti. Hibátlan, dobozában. Eladó: Peti (12 értékelés)")
        if isinstance(page, dict):
            return {"url": url, "title": "", "short": [], "labels": [], "captcha": False, "text": "", **page}
        return {"url": url, "title": "Hirdetés", "text": page, "short": [], "labels": [], "captcha": False}


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


class FakeLlm:
    """A felmérésnél (similar) a sorok közül választ (pick: melyik sor számít, alapból mind), a jelöltnél igen/nem."""

    def __init__(self, ok: bool = True, pick=None):
        self.ok, self.pick, self.calls, self.judged = ok, pick, [], []

    def chat_json(self, messages, schema, **kwargs):
        self.calls.append(messages)
        if "similar" in schema["properties"]:
            lines = [line for line in messages[1]["content"].splitlines() if re.match(r"\d+\. ", line)]
            self.judged.append(lines)
            return {"similar": [int(line.split(".")[0]) for line in lines if self.pick is None or self.pick(line)],
                    "reason": "A valódi kártyák."}
        return {"ok": self.ok, "reason": "Ugyanaz a kártya." if self.ok else "Csak a doboza."}

    def checks(self) -> int:
        return len(self.calls) - len(self.judged)


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

    def make(self, sub: str = "", llm="default") -> DealHunter:
        return DealHunter(Path(self.tmp.name) / sub, PLATFORMS, lambda: self.browser,
                          self.llm if llm == "default" else llm, clock=self.clock)

    def notify(self, kind, title, text, **extra):
        self.notices.append((kind, title, text, extra))

    def ready(self, hunter: DealHunter | None = None, query: str = "RTX 3060 Ti", **values) -> dict:
        """Egy keresés kézzel megadott szokásos árral (felmérés nélkül), bekapcsolva."""
        hunter = hunter or self.hunter
        state = hunter.add_search(query, ["hardverapro"], **{"reference": 100000, **values})
        hunter.set_enabled(True)
        return state["searches"][-1]

    # --- felmérés és jóváhagyás ---

    def test_off_by_default_and_does_nothing(self):
        self.hunter.add_search("RTX 3060 Ti", ["hardverapro"])
        self.assertFalse(self.hunter.available())
        self.assertEqual(self.hunter.run(notify=self.notify), "Az automata kereskedés ki van kapcsolva.")
        self.assertEqual(self.browser.urls, [])

    def test_the_usual_price_is_surveyed_once_and_waits_for_approval(self):
        [search] = self.hunter.add_search("RTX 3060 Ti", ["hardverapro"])["searches"]
        self.assertEqual(search["status"], "felmérés")
        self.hunter.set_enabled(True)
        result = self.hunter.run(notify=self.notify)
        self.assertEqual(result, "1 keresés: 1 olcsó hirdetés („RTX 3060 Ti”: szokásos ár javaslat 100 000 Ft, "
                                 "jóváhagyásra vár).")
        [lines] = self.llm.judged
        self.assertEqual(len(lines), 8, "a modell csak az előszűrt találatokat kapja (kereső, hibás, más termék nem)")
        [proposal_notice, deal_notice] = self.notices
        self.assertIn("szerintem most kb. 100 000 Ft (7 hasonló hirdetés alapján, a többség", proposal_notice[2])
        self.assertEqual(proposal_notice[3]["actions"], [{"action": "deals", "label": "Megnézem"}])
        self.assertIn("A felmérés közben találtam.", deal_notice[2], "felmérés közben is szól a jóval olcsóbbról")
        self.assertIn("várható haszon kb. 33 500 Ft", deal_notice[2])
        [search] = self.hunter.state()["searches"]
        self.assertEqual((search["status"], search["proposal"]["price"], search["proposal"]["n"]), ("javaslat", 100000, 7))
        self.assertEqual(len(search["proposal"]["rows"]), 8, "a felületen látszik, miből számolt")

        self.browser.urls.clear()
        self.assertEqual(self.hunter.run(notify=self.notify, force=True),
                         "Most egyik keresés sem esedékes („RTX 3060 Ti”: a szokásos ár (100 000 Ft) jóváhagyásra vár).")
        self.assertEqual(self.browser.urls, [], "jóváhagyásig nem keres")
        with self.assertRaises(DealError):
            self.hunter.accept("nincs-ilyen")
        [search] = self.hunter.accept(search["id"])["searches"]
        self.assertEqual((search["status"], search["reference"], search["ref_source"], search["basis"]["n"]),
                         ("fut", 100000, "felmérés", 7))

        self.notices.clear()
        self.browser.results.append(listing(14, "MSI RTX 3060 Ti Ventus", 60000))
        self.assertEqual(self.hunter.run(notify=self.notify, force=True), "1 keresés: 1 olcsó hirdetés.")
        [(_kind, title, text, _extra)] = self.notices
        self.assertIn("MSI RTX 3060 Ti Ventus", title)
        self.assertIn("A szokásos ára 100 000 Ft, a várható haszon kb. 28 500 Ft.", text)
        self.assertEqual(len(self.llm.judged), 1, "a felmérés egyszer fut")
        self.notices.clear()
        self.assertEqual(self.hunter.run(notify=self.notify, force=True), "1 keresés: nincs új olcsó hirdetés.")
        self.assertEqual(self.notices, [], "egy hirdetésről egyszer szól")

    def test_a_manual_price_needs_no_survey(self):
        self.ready()
        self.assertEqual(self.hunter.run(notify=self.notify), "1 keresés: 1 olcsó hirdetés.")
        self.assertEqual((len(self.llm.judged), self.llm.checks()), (0, 1), "csak a jelöltre egy kérdés")
        self.assertEqual(self.browser.looked, [CHEAP], "csak a jelölt oldalát nyitotta meg")
        self.assertIn("Hibátlan, dobozában", self.llm.calls[0][1]["content"], "a modell a hirdetés leírását is látja")
        [deal] = self.hunter.state()["deals"]
        self.assertEqual((deal["price"], deal["median"], deal["profit"]), (55000, 100000, 33500))
        self.assertTrue(deal["checked"])
        self.assertEqual(self.make().state()["deals"][0]["id"], deal["id"], "a gépen megmarad")

    def test_the_price_can_be_written_over_and_surveyed_again(self):
        search = self.ready()
        [search] = self.hunter.accept(search["id"], "120 000")["searches"]
        self.assertEqual((search["reference"], search["ref_source"]), (120000, "kézi"))
        with self.assertRaises(DealError):
            self.hunter.accept(search["id"], "valamennyi")
        [search] = self.hunter.recalibrate(search["id"])["searches"]
        self.assertEqual(search["status"], "felmérés")
        self.hunter.run(notify=self.notify)
        self.assertEqual(len(self.llm.judged), 1, "a következő körben újra felméri")

    def test_too_few_similar_listings_ask_for_a_manual_price_once(self):
        self.browser.results = NORMAL[:3] + RESULTS[7:8]
        self.llm.pick = lambda line: "Gigabyte" not in line
        self.hunter.add_search("RTX 3060 Ti", ["hardverapro"])
        self.hunter.set_enabled(True)
        result = self.hunter.run(notify=self.notify)
        self.assertIn("kevés hasonló hirdetés (3), add meg kézzel a szokásos árat", result)
        [(_kind, title, text, _extra)] = self.notices
        self.assertEqual(title, "Szokásos ár kell: RTX 3060 Ti")
        self.assertEqual(self.hunter.state()["searches"][0]["status"], "kevés")
        self.browser.urls.clear()
        self.assertIn("kevés hasonló hirdetés volt", self.hunter.run(notify=self.notify, force=True))
        self.assertEqual(self.browser.urls, [], "egy napig nem próbálja újra")
        self.clock.now += 86400 + 60
        self.hunter.run(notify=self.notify, force=True)
        self.assertEqual((len(self.llm.judged), len(self.notices)), (2, 1), "egy nap múlva újra próbálja, de nem szól újra")

    def test_without_a_model_the_survey_uses_the_filtered_prices(self):
        hunter = self.make("modell-nelkul", llm=None)
        hunter.add_search("RTX 3060 Ti", ["hardverapro"])
        hunter.set_enabled(True)
        hunter.run(notify=self.notify)
        self.assertEqual(hunter.state()["searches"][0]["proposal"]["price"], 100000)

    # --- a modell nélküli keresés ---

    def test_the_model_can_say_no_and_without_a_model_it_still_tells(self):
        self.llm.ok = False
        self.ready()
        self.hunter.run(notify=self.notify)
        self.assertEqual(self.notices, [])
        other = self.make("masik", llm=None)
        self.ready(other)
        other.run(notify=self.notify)
        self.assertEqual(len(self.notices), 1, "modell nélkül is szól (nem ellenőrzött)")

    def test_minimum_price_and_excluded_words(self):
        self.browser.results = RESULTS + [listing(20, "RTX 3060 Ti doboz, üres", 20000),
                                          listing(21, "RTX 3060 Ti backplate", 9000)]
        self.ready(min_price=15000, exclude="doboz, backplate")
        self.hunter.run(notify=self.notify)
        self.assertEqual([n[1] for n in self.notices], ["Olcsó: Gigabyte RTX 3060 Ti Gaming OC"])
        other = self.make("szuro-nelkul")
        self.ready(other)
        other.run(notify=self.notify)
        self.assertEqual(len(self.notices), 3, "szűrők nélkül a doboz és a backplate is jelölt lett volna")
        with self.assertRaises(DealError):
            self.hunter.add_search("RTX 3060 Ti", min_price=90000, max_price=50000)

    def test_percent_or_profit(self):
        self.browser.results = NORMAL + [listing(30, "ASUS RTX 3060 Ti Dual", 80000)]
        self.ready()
        self.hunter.run(notify=self.notify)
        self.assertEqual(self.notices, [], "20%-kal olcsóbb: a 30%-os határon belül, haszon nincs megadva")
        other = self.make("haszon")
        self.ready(other, min_profit=5000)
        other.run(notify=self.notify)
        [(_kind, _title, text, _extra)] = self.notices
        self.assertIn("a várható haszon kb. 8 500 Ft", text, "100 000 × 0,9 − 80 000 − 1 500 posta")
        self.assertEqual(expected_profit(4000, 2700), -600)
        self.assertFalse(self.hunter._cheap({"discount": 30}, 2700, 4000), "33%-kal olcsóbb, de nem hoz hasznot")

    def test_a_price_drop_makes_a_seen_listing_a_candidate_again(self):
        self.ready()
        self.hunter.run(notify=self.notify)
        self.notices.clear()
        self.browser.results[0] = listing(0, "RTX 3060 Ti videókártya 0", 62000)
        self.assertEqual(self.hunter.run(notify=self.notify, force=True), "1 keresés: 1 olcsó hirdetés.")
        [(_kind, _title, text, _extra)] = self.notices
        self.assertIn("62 000 Ft (leárazta: 95 000 Ft → 62 000 Ft)", text)

    def test_a_lasting_price_change_is_reported_once(self):
        self.ready(reference=150000, discount=80)
        self.hunter.run(notify=self.notify)
        self.clock.now += 86400
        self.hunter.run(notify=self.notify, force=True)
        self.assertEqual(self.notices, [], "egy nap még nem elég")
        self.clock.now += 2 * 86400 + 60
        self.hunter.run(notify=self.notify, force=True)
        [(_kind, title, text, _extra)] = self.notices
        self.assertEqual(title, "Változott az ár? RTX 3060 Ti")
        self.assertIn("inkább 100 000 Ft körül", text)
        self.hunter.run(notify=self.notify, force=True)
        self.assertEqual(len(self.notices), 1, "egyszer szól")
        self.assertEqual(self.hunter.state()["searches"][0]["market"]["median"], 100000)

    def test_only_five_candidates_per_round_the_rest_next_time(self):
        self.browser.results = NORMAL + [listing(40 + n, f"RTX 3060 Ti Gamer {n}", 50000 + n * 100) for n in range(7)]
        self.ready()
        self.assertEqual(self.hunter.run(notify=self.notify), "1 keresés: 5 olcsó hirdetés.")
        self.assertEqual(len(self.browser.looked), 5)
        self.assertEqual(self.hunter.run(notify=self.notify, force=True), "1 keresés: 2 olcsó hirdetés.")
        self.assertEqual(len(self.notices), 7, "a maradék a következő körben, és egyik sem kétszer")

    def test_searches_validate_update_and_a_broken_site_does_not_stop_the_rest(self):
        with self.assertRaises(DealError):
            self.hunter.add_search("  ")
        state = self.hunter.add_search("Nike Air Max 42", ["vinted", "nincs ilyen"], discount=95, exclude="Gyerek; gyerek")
        [search] = state["searches"]
        self.assertEqual((search["sites"], search["discount"], search["exclude"]), (["vinted"], 80, ["gyerek"]))
        self.assertEqual([s["key"] for s in state["sites"]], ["vinted", "jofogas", "hardverapro"])
        self.hunter.accept(search["id"], 30000)
        [search] = self.hunter.update_search(search["id"], min_price="10000", every=60)["searches"]
        self.assertEqual((search["min_price"], search["every"], search["reference"]), (10000, 60, 30000))
        [search] = self.hunter.update_search(search["id"], query="Nike Air Force 42")["searches"]
        self.assertEqual(search["status"], "felmérés", "más termék: a szokásos árat újra fel kell mérni")
        self.hunter.remove_search(search["id"])
        self.hunter.add_search("RTX 3060 Ti", reference=100000)
        self.hunter.set_enabled(True)
        self.browser.fail = {"vinted"}
        result = self.hunter.run(notify=self.notify)
        self.assertIn("order=newest_first", self.browser.urls[0], "a Vinteden a legújabb elöl")
        self.assertIn("Vinted: nem nyílt meg", result)
        self.assertEqual(len(self.browser.urls), 3, "a többi oldalt is megnézte")
        self.assertEqual(len(self.notices), 1, "ugyanaz a hirdetés több oldalon is: egyszer")
        self.browser.results = []
        self.assertIn("Hardverapró: nem találtam árat a találati oldalon", self.hunter.run(notify=self.notify, force=True))

    def test_each_search_runs_at_its_own_pace_and_vinted_at_most_every_two_hours(self):
        self.hunter.add_search("RTX 3060 Ti", ["hardverapro"], every=30, reference=100000)
        self.hunter.add_search("Nike Air Max 42", ["vinted"], every=30, reference=30000)
        self.hunter.add_search("PS5 kontroller", ["jofogas"], every=360, reference=14000)
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

    # --- a jelölt hirdetés oldala ---

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
                hunter = self.make(str(n))
                self.ready(hunter)
                hunter.run(notify=self.notify)
                if warning is None:
                    self.assertEqual((self.notices, self.llm.calls), ([], []), "gyanús vagy hibás: modell sem kell")
                else:
                    [notice] = self.notices
                    self.assertIn(f"Figyelj: {warning}.", notice[2])
                    self.assertEqual(hunter.state()["deals"][0]["warnings"], [warning])

    def test_sold_listing_is_skipped_and_a_captcha_stops_the_page_reads(self):
        self.browser.pages = {CHEAP: {"short": ["Eladva"], "text": "Gigabyte RTX 3060 Ti"}}
        self.ready()
        self.hunter.run(notify=self.notify)
        self.assertEqual(self.notices, [], "már elkelt")
        self.browser.pages = {CHEAP: {"captcha": True}}
        other = self.make("c")
        self.ready(other)
        other.run(notify=self.notify)
        [notice] = self.notices
        self.assertIn("nem néztem meg (robot-ellenőrzés)", notice[2], "captchánál szól, de jelzi, hogy nem nézte meg")


class MatchTests(unittest.TestCase):
    def test_every_word_of_the_search_and_no_accessories(self):
        from jarvis_core.deals import matches_query
        cases = {
            "Sony DualSense PS5 kontroller fehér": True,
            "PlayStation 5 DualSense controller": True,
            "PS5 FIFA 24 játék": False,
            "PS4 kontroller": False,
            "PS5 kontroller töltő állomás": False,
            "PS5 kontrollerhez szilikon tok": False,
        }
        for text, expected in cases.items():
            self.assertEqual(matches_query("Ps5 kontroller", text), expected, text)
        self.assertFalse(matches_query("RTX 3060 Ti", "RTX 3060 12GB"))

    def test_a_game_in_the_results_does_not_reach_the_survey(self):
        controllers = [listing(n, f"PS5 DualSense kontroller {n}", p, "www.vinted.hu") for n, p in
                       enumerate((14000, 13500, 15000, 13800, 14500, 13000))]
        games = [listing(20 + n, f"PS5 játék {name}", 5000, "www.vinted.hu") for n, name in enumerate(("FIFA", "GTA"))]
        browser, llm = FakeBrowser(controllers + games), FakeLlm()
        with tempfile.TemporaryDirectory() as tmp:
            hunter = DealHunter(Path(tmp), PLATFORMS, lambda: browser, llm)
            hunter.add_search("Ps5 kontroller", ["vinted"])
            hunter.set_enabled(True)
            hunter.run()
            self.assertEqual(len(llm.judged[0]), 6, "a játékok a modellhez sem jutnak el")
            self.assertEqual(hunter.state()["searches"][0]["proposal"]["price"], 13900)
            self.assertEqual(browser.looked, [])


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
