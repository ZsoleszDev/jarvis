"""Automata kereskedés (deals.py): a szokásos ár felmérése (egy modellhívás, jóváhagyással), utána modell nélküli
keresés ehhez az árhoz mérve, és csak a jelöltekre egy rövid modellkérdés."""
from __future__ import annotations

import random
import re
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_offline import make_cfg  # noqa: E402
from test_web_browser import HAS_CHROMIUM  # noqa: E402

from jarvis_core.browser import BrowserController, BrowserError  # noqa: E402
from jarvis_core.deals import (DealError, DealHunter, ModelLane, expected_profit, explore_top, highest_ok,  # noqa: E402
                               last_chance, left_text, offer_price, parse_auction)
from jarvis_core.journal import Journal  # noqa: E402
from jarvis_core.pricebook import PriceBook, product_key, usable  # noqa: E402
from jarvis_core.scheduler import Preempted  # noqa: E402
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
# Vatera: hamarosan záruló licitek (egy ismert keresésre illő, egy ismeretlen értékes, egy olcsó semmiség), egy
# 3 nap múlva záruló és egy fix áras; és amennyiért máshol adják őket.
AUCTIONS = [
    {"text": "PS5 DualSense kontroller · Hátralévő idő: 2 óra 10 perc · 3 licit", "price": 3500,
     "url": "https://www.vatera.hu/termek/1001", "auction": True},
    {"text": "Nintendo Switch OLED konzol · Hátralévő idő: 1 óra 5 perc · 7 licit", "price": 20000,
     "url": "https://www.vatera.hu/termek/1002", "auction": True},
    {"text": "Régi könyvcsomag · Hátralévő idő: 3 óra · 0 licit", "price": 1, "url": "https://www.vatera.hu/termek/1003",
     "auction": True},
    {"text": "Xbox kontroller · Hátralévő idő: 3 nap · 1 licit", "price": 1, "url": "https://www.vatera.hu/termek/1004",
     "auction": True},
    {"text": "Fix áras PS5 kontroller", "price": 9000, "url": "https://www.vatera.hu/termek/1005"},
]
SWITCH = [listing(60 + n, f"Nintendo Switch OLED fehér {n}", p, "www.jofogas.hu") for n, p in
          enumerate((100000, 95000, 105000, 98000, 102000))]
CONTROLLERS = [listing(70 + n, f"PS5 DualSense kontroller {n}", p, "www.jofogas.hu") for n, p in
               enumerate((14000, 13500, 15000, 13800, 14500))]
# Egy figyelt Hardverapró-kategória (videókártyák, a legújabb elöl): egy olcsó és egy szokásos árú RTX 3060 Ti (ezeket
# az árbázis ismeri), egy fejhallgató, aminek a címéből nem derül ki a típusa (a modell nevezi meg), és egy semmiség.
CATEGORY = "https://hardverapro.hu/aprok/hardver/videokartya/index.html"
PAGE = [listing(20, "Gigabyte RTX 3060 Ti Gaming OC", 70000), listing(21, "RTX 3060 Ti", 95000),
        listing(22, "Eladó zajszűrős Sony fejhallgató, alig használt", 30000), listing(23, "Laptop táska", 5000)]
HEADPHONES = [listing(80 + n, f"Sony WH-1000XM4 fejhallgató {n}", p, "www.jofogas.hu") for n, p in
              enumerate((70000, 68000, 72000, 69000, 71000))]
AIRPODS = [{**listing(90 + n, f"Apple AirPods Pro 2 {n}", p, "www.vinted.hu"), "platform": "vinted"} for n, p in
           enumerate((50000, 48000, 52000, 49000, 51000))]


class FakeBrowser:
    """A keresők találatai (by_url: ha a webcímben benne van a szó, azok a találatok), és egy-egy hirdetés oldala
    (pages: webcím → a lap szövege, vagy a lap adatai). Feljegyzi, melyik szálról hívták (a böngésző egy szálon fut)."""

    def __init__(self, results=None, fail=()):
        self.results, self.fail, self.urls, self.looked = list(results or RESULTS), set(fail), [], []
        self.pages: dict[str, object] = {}
        self.by_url: list[tuple[str, list[dict]]] = []
        self.threads: set[str] = set()
        self.challenge: set[str] = set()      # ezeken a címeken robot-ellenőrzést kér
        self._challenged = False

    def challenged(self) -> bool:
        return self._challenged

    def extract_listings(self, url: str) -> list[dict]:
        self.urls.append(url)
        self.threads.add(threading.current_thread().name)
        if any(f in url for f in self.fail):
            raise BrowserError("nem nyílt meg")
        self._challenged = any(c in url for c in self.challenge)
        if self._challenged:
            return []
        for word, results in self.by_url:
            if word in url:
                return [dict(r) for r in results]
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
    """A felmérésnél (similar) a sorok közül választ (pick: melyik sor számít, alapból mind), a jelöltnél igen/nem, a
    figyelt oldalaknál (items: a termék neve és hogy megéri-e) az explore szerint. Feljegyzi, melyik szálról hívták, és hány hívás futott egyszerre."""

    def __init__(self, ok: bool = True, pick=None):
        self.ok, self.pick, self.calls, self.judged = ok, pick, [], []
        self.suspicious = False               # a jelöltnél: átverésnek látszik
        self.explore: list[dict] = []
        self.threads: set[str] = set()
        self.running = self.most = 0
        self.delay = 0.0
        self.started = threading.Event()      # elindult egy modellhívás
        self._lock = threading.Lock()

    def chat_json(self, messages, schema, **kwargs):
        with self._lock:
            self.running += 1
            self.most = max(self.most, self.running)
            self.threads.add(threading.current_thread().name)
        self.started.set()
        try:
            time.sleep(self.delay)
            return self._answer(messages, schema)
        finally:
            with self._lock:
                self.running -= 1

    def _answer(self, messages, schema):
        self.calls.append(messages)
        if "items" in schema["properties"]:
            return {"items": self.explore, "reason": "Ezek megérhetik."}
        if "similar" in schema["properties"]:
            lines = [line for line in messages[1]["content"].splitlines() if re.match(r"\d+\. ", line)]
            self.judged.append(lines)
            return {"similar": [int(line.split(".")[0]) for line in lines if self.pick is None or self.pick(line)],
                    "reason": "A valódi kártyák."}
        if self.suspicious:
            return {"ok": True, "suspicious": True, "reason": "Előre utalást kér, és túl olcsó."}
        return {"ok": self.ok, "suspicious": False, "reason": "Ugyanaz a kártya." if self.ok else "Csak a doboza."}

    def checks(self) -> int:
        return len(self.calls) - len(self.judged)


class HunterCase(unittest.TestCase):
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
        hunter = DealHunter(Path(self.tmp.name) / sub, PLATFORMS, lambda: self.browser,
                            self.llm if llm == "default" else llm, clock=self.clock)
        hunter.set_day(0, 24)            # a tesztekben a gép mindig megy (az esti leállásnak külön teszt van)
        return hunter

    def notify(self, kind, title, text, **extra):
        self.notices.append((kind, title, text, extra))

    def ready(self, hunter: DealHunter | None = None, query: str = "RTX 3060 Ti", **values) -> dict:
        """Egy keresés kézzel megadott szokásos árral (felmérés nélkül), bekapcsolva."""
        hunter = hunter or self.hunter
        state = hunter.add_search(query, ["hardverapro"], **{"reference": 100000, **values})
        hunter.set_enabled(True)
        return state["searches"][-1]


class DealTests(HunterCase):
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
        self.browser.results = RESULTS + [listing(20, "RTX 3060 Ti doboz, üres", 45000),
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
        self.assertEqual([s["key"] for s in state["sites"]], ["vinted", "jofogas", "vatera", "hardverapro"],
                         "a Vaterán a fix áras hirdetések is")
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
        self.assertEqual(len(self.browser.urls), 4, "a többi oldalt is megnézte")
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
        """A hibásról és a gyanúsról nem szól (a modellt sem kérdezi); a gyanúsat a naplóba írja, hogy miért."""
        cases = {
            "Csak előre utalással, külföldön vagyok, postázom.": "átverésre utaló jel",
            "A kártya működik, de a hűtő ventilátora repedt.": None,
            "Szia, írj WhatsAppon: +36 30 123 4567": "az oldalon kívüli elérhetőséget ad meg",
            "Hibátlan. Az eladónak még nincs értékelése.": "az eladónak még nincs értékelése",
        }
        for n, (page, why) in enumerate(cases.items()):
            with self.subTest(page=page):
                self.notices.clear()
                self.llm.calls.clear()
                self.browser.pages = {CHEAP: page}
                hunter = self.make(str(n))
                self.ready(hunter)
                hunter.run(notify=self.notify)
                self.assertEqual((self.notices, self.llm.calls), ([], []), "nem szól róla, modell sem kell")
                self.assertEqual(hunter.state()["deals"], [], "a találatok közé sem kerül")
                said = [e["text"] for e in hunter.journal.entries() if e["text"].startswith("Gyanús")]
                if why is None:
                    self.assertEqual(said, [], "a hibás nem gyanús, csak nem éri meg")
                else:
                    [line] = said
                    self.assertIn("„Gigabyte RTX 3060 Ti Gaming OC” 55 000 Ft (Hardverapró)", line)
                    self.assertIn(why, line)

    def test_suspiciously_cheap_or_suspicious_to_the_model_is_never_sent(self):
        self.browser.results = NORMAL + [listing(10, "Gigabyte RTX 3060 Ti Gaming OC", 35000)]
        self.ready()
        self.hunter.run(notify=self.notify)
        self.assertEqual((self.notices, self.browser.looked), ([], []), "nem szól róla, az oldalát sem nyitja meg")
        self.assertEqual(self.hunter.state()["deals"], [])
        [line] = [e["text"] for e in self.hunter.journal.entries() if e["text"].startswith("Gyanús")]
        self.assertIn("gyanúsan olcsó: a szokásos ára (kb. 100 000 Ft) 35%-a", line)
        self.hunter.run(notify=self.notify, force=True)
        self.assertEqual(len([e for e in self.hunter.journal.entries() if e["text"].startswith("Gyanús")]), 1,
                         "látottnak jegyezte: egyszer írja be")
        # amit a modell gyanúsnak lát (előre utalás, túl szép, hogy igaz legyen…), arról sem szól
        self.browser.results = RESULTS
        self.llm.suspicious = True
        other = self.make("modell")
        self.ready(other)
        other.run(notify=self.notify)
        self.assertEqual((self.notices, other.state()["deals"]), ([], []))
        self.assertIn("a modell szerint gyanús: Előre utalást kér",
                      " ".join(e["text"] for e in other.journal.entries()))

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


    # --- ajánlat a Vinteden ---

    def test_a_bit_too_expensive_on_vinted_suggests_an_offer(self):
        self.browser.results = [listing(n, f"RTX 3060 Ti videókártya {n}", p, "www.vinted.hu") for n, p in
                                enumerate((95000, 100000, 105000, 98000, 110000))] + [
            listing(50, "MSI RTX 3060 Ti Ventus", 80000, "www.vinted.hu")]
        self.hunter.add_search("RTX 3060 Ti", ["vinted"], reference=100000)
        self.hunter.set_enabled(True)
        self.assertEqual(self.hunter.run(notify=self.notify), "1 keresés: 1 olcsó hirdetés.")
        [(_kind, title, text, extra)] = self.notices
        self.assertEqual(title, "Olcsó: MSI RTX 3060 Ti Ventus")
        self.assertIn("Ha 70 000 Ft-ért megkapod (ajánlattétel), kb. 18 500 Ft a haszon. Ajánlatot tegyek? Csak a "
                      "jóváhagyásod után küldöm el.", text)
        [deal] = self.hunter.state()["deals"]
        self.assertEqual((deal["offer"], deal["profit"]), (70000, 18500))
        self.assertEqual(extra["actions"][0], {"action": "offer", "deal": deal["id"], "label": "Ajánlatot teszek (70 000 Ft)"})
        other = self.make("jofogas")
        other.add_search("RTX 3060 Ti", ["jofogas"], reference=100000)
        other.set_enabled(True)
        self.notices.clear()
        other.run(notify=self.notify)
        self.assertEqual(self.notices, [], "ajánlatot csak a Vinteden lehet tenni")

    def test_the_highest_price_that_still_pays_and_the_offer(self):
        plain, profit = {"discount": 30}, {"discount": 30, "min_profit": 5000}
        self.assertEqual((highest_ok(plain, 100000), highest_ok(profit, 100000)), (70000, 83500))
        self.assertEqual(highest_ok({"discount": 30, "max_price": 50000}, 100000), 50000)
        self.assertIsNone(highest_ok({"discount": 30, "min_price": 80000}, 100000), "az ársávban nincs ilyen")
        for search in (plain, profit, {"discount": 10}, {"discount": 50, "min_profit": 1000}):
            for reference in (4000, 14000, 13333, 100000):
                top = highest_ok(search, reference)
                if top is None:
                    continue
                self.assertTrue(self.hunter._cheap(search, top, reference), (search, reference))
                self.assertFalse(self.hunter._cheap(search, top + 1, reference), (search, reference))
        self.assertEqual(offer_price(plain, 80000, 100000), 70000)
        self.assertIsNone(offer_price(plain, 95000, 100000), "26% engedmény: nem fogadná el")
        self.assertIsNone(offer_price(plain, 70000, 100000), "így is megéri: nem kell ajánlat")

    # --- Vatera-licitek ---

    def test_auction_text(self):
        self.assertEqual(parse_auction("PS5 kontroller · Hátralévő idő: 2 óra 10 perc · 3 licit"), {"left": 7800, "bids": 3})
        self.assertEqual(parse_auction("Licitek: 12, még 1 nap 5 óra"), {"left": 104400, "bids": 12})
        self.assertEqual(parse_auction("Azonnal megvásárolható"), {"left": None, "bids": None})
        self.assertEqual((left_text(7800), left_text(104400), left_text(None)), ("2 óra 10 perc", "1 nap 5 óra",
                                                                                "nem tudom, mikor"))

    def test_vatera_auctions_known_searches_and_explored_ones(self):
        self.browser.by_url = [("vatera.hu", AUCTIONS), ("Nintendo", SWITCH), ("PS5", CONTROLLERS)]
        self.hunter.add_search("PS5 kontroller", ["jofogas"], reference=14000)
        self.hunter.add_watch("https://www.vatera.hu/listings/index.php?c=123&ob=ending")
        self.llm.explore = [{"n": 1, "query": "Nintendo Switch OLED", "buy": True}]
        self.hunter.set_enabled(True)
        self.assertEqual(self.hunter.every_minutes(), 60.0)
        self.assertEqual(self.hunter.run(notify=self.notify), "2 keresés: 3 olcsó hirdetés.")
        notice = {title.split(" ·")[0]: text for _kind, title, text, _extra in self.notices}
        self.assertIn("Legfeljebb 9 800 Ft-ig érdemes licitálni", notice["Licit: PS5 DualSense kontroller"])
        self.assertIn("A szokásos ára 14 000 Ft", notice["Olcsó: Fix áras PS5 kontroller"], "a fix áras is számít")
        switch = notice["Licit: Nintendo Switch OLED konzol"]
        self.assertIn("1 óra 5 perc múlva zárul, most 20 000 Ft, 7 licit. Máshol kb. 100 000 Ft-ért adják. Legfeljebb "
                      "83 500 Ft-ig érdemes licitálni", switch)
        self.assertIn("Licitálni nem fogok helyetted.", switch)
        self.assertIn("Figyelt oldal: Vatera", switch)
        explored = [m for m in self.llm.calls if "Friss hirdetések egy figyelt oldalon" in m[1]["content"]]
        self.assertEqual(len(explored), 1, "a modell egyszer nézi át az oldalt")
        self.assertNotIn("Xbox", explored[0][1]["content"], "a 3 nap múlva záruló még nem érdekes")
        self.assertNotIn("PS5", explored[0][1]["content"], "ami illik egy keresésedre, ahhoz nem kell modell")
        self.notices.clear()
        self.hunter.run(notify=self.notify, force=True)
        self.assertEqual(self.notices, [], "egy licitről egyszer szól")

    def test_watch_validation_and_an_empty_page(self):
        for bad in ("", "https://example.com/aukcio"):
            with self.assertRaises(DealError):
                self.hunter.add_watch(bad)
        [watch] = self.hunter.add_watch("PS5", min_profit="8 000", hours=3)["watches"]
        self.assertEqual((watch["name"], watch["min_profit"], watch["hours"], watch["platform"]), ("PS5", 8000, 3, "vatera"))
        self.assertIn("vatera.hu/listings/index.php?q=PS5", watch["url"])
        self.browser.by_url = [("vatera.hu", [])]
        self.hunter.set_enabled(True)
        self.assertIn("Vatera (PS5): nem találtam hirdetést az oldalon", self.hunter.run(notify=self.notify))
        self.assertEqual(self.hunter.remove_watch(watch["id"])["watches"], [])

    def test_without_a_model_unknown_auctions_are_not_explored(self):
        self.browser.by_url = [("vatera.hu", AUCTIONS)]
        hunter = self.make("modell-nelkul", llm=None)
        hunter.add_watch("Nintendo")
        hunter.set_enabled(True)
        hunter.run(notify=self.notify)
        self.assertEqual(self.notices, [])
        self.assertEqual([u for u in self.browser.urls if "vatera" not in u], [], "máshol nem keresett")


class ExploreTests(HunterCase):
    """Felfedező: figyelt oldalak keresőszó nélkül, az árbázishoz mérve; az ismeretleneket a modell nevezi meg."""

    def setUp(self):
        super().setUp()
        self.hunter.book.add([{**r, "platform": "hardverapro"} for r in NORMAL])   # az árbázis már ismeri

    def test_a_category_page_without_a_search_word(self):
        self.browser.by_url = [("videokartya", PAGE), ("WH-1000XM4", HEADPHONES)]
        [watch] = self.hunter.add_watch(CATEGORY)["watches"]
        self.assertEqual((watch["name"], watch["platform"], watch["min_profit"]), ("Hardverapró: videokartya",
                                                                                   "hardverapro", 5000))
        self.llm.explore = [{"n": 1, "query": "Sony WH-1000XM4", "buy": True}]
        self.hunter.set_enabled(True)
        self.assertEqual(self.hunter.run(notify=self.notify), "1 keresés: 2 olcsó hirdetés.")
        notice = {title: text for _kind, title, text, _extra in self.notices}
        rtx = notice["Olcsó: Gigabyte RTX 3060 Ti Gaming OC"]
        # az oldal szokásos árú RTX-e is az árbázisba került (8 ár)
        self.assertIn("Máshol kb. 99 000 Ft-ért adják (8 hasonló hirdetés alapján), a várható haszon kb. 17 600 Ft. "
                      "Figyelt oldal: Hardverapró: videokartya.", rtx)
        sony = notice["Olcsó: Eladó zajszűrős Sony fejhallgató, alig használt"]
        self.assertIn("Máshol kb. 70 000 Ft-ért adják (5 hasonló hirdetés alapján), a várható haszon kb. 31 500 Ft.", sony)
        [explored] = [m[1]["content"] for m in self.llm.calls if "Friss hirdetések" in m[1]["content"]]
        self.assertNotIn("RTX", explored, "amit az árbázis ismer, ahhoz nem kell modell")
        self.assertIn("Laptop táska – 5 000 Ft", explored)
        self.assertEqual([u for u in self.browser.urls if "WH-1000XM4" in u and "hardverapro" in u], [],
                         "a viszonteladási árat a többi oldalon nézi")
        self.assertIsNotNone(self.hunter.book.reference("sony wh1000xm4"), "a máshol látott árak is az árbázisba kerülnek")
        self.notices.clear()
        self.hunter.run(notify=self.notify, force=True)
        self.assertEqual(self.notices, [], "egy hirdetésről egyszer szól")
        self.assertEqual(len([m for m in self.llm.calls if "Friss hirdetések" in m[1]["content"]]), 1)

    def test_known_products_need_no_model_and_the_named_ones_teach_the_book(self):
        self.browser.by_url = [("videokartya", PAGE)]
        for sub, profit, found in (("sok", 20000, []), ("eleg", 10000, ["Olcsó: Gigabyte RTX 3060 Ti Gaming OC"])):
            hunter = self.make(sub, llm=None)
            hunter.book.add([{**r, "platform": "hardverapro"} for r in NORMAL])
            hunter.add_watch(CATEGORY, min_profit=profit)
            hunter.set_enabled(True)
            self.notices.clear()
            hunter.run(notify=self.notify)
            self.assertEqual([t for _k, t, _x, _e in self.notices], found, f"{profit} Ft haszon kell, 17 600 jön ki")
        # a modell által megnevezett hirdetés ára a termék neve alatt kerül az árbázisba
        book = PriceBook(Path(self.tmp.name) / "tanul", self.clock)
        row = {"text": "Eladó zajszűrős Sony fejhallgató", "price": 70000, "url": "https://x.hu/apro/1", "platform": "x"}
        self.assertEqual(book.add([row]), 0)
        self.assertEqual(book.add([row], name="Sony WH-1000XM4"), 1)
        self.assertEqual(book.lookup("WH-1000XM4")[0]["samples"], 1)

    def test_vinted_pages_offers_and_fake_prone_brands(self):
        self.hunter.book.add(AIRPODS)
        page = [{**listing(95, "AirPods Pro 2 eladó", 45000, "www.vinted.hu")},
                {**listing(96, "Airpods pro 2 új", 25000, "www.vinted.hu")}]
        self.browser.by_url = [("vinted.hu/catalog", page)]
        [watch] = self.hunter.add_watch("https://www.vinted.hu/catalog?catalog[]=2994")["watches"]
        self.assertTrue(watch["url"].endswith("&order=newest_first"), "a legújabb elöl")
        self.hunter.set_enabled(True)
        self.hunter.run(notify=self.notify)
        deals = {d["price"]: d for d in self.hunter.state()["deals"]}
        self.assertEqual(deals[45000]["offer"], 38500)
        self.assertIn("Ajánlatot tegyek?", [t for _k, _t, t, _e in self.notices if "45 000" in t][0])
        self.assertNotIn(25000, deals, "a gyakran hamisított márka feltűnően olcsón gyanús: nem szól róla")
        self.assertIn("ezt a márkát gyakran hamisítják", " ".join(e["text"] for e in self.hunter.journal.entries()))
        self.assertNotIn("warnings", deals[45000])
        self.assertEqual(explore_top(50000, 5000), 38500)
        self.clock.now += 3600
        self.assertIn("Most egyik keresés sem esedékes", self.hunter.run(notify=self.notify),
                      "a Vinted-oldalt is legfeljebb kétóránként")

    def test_auctions_closing_while_the_computer_is_off_come_in_the_evening(self):
        day = (8, 22)
        at = lambda d, h, m=0: time.mktime((2027, 1, d, h, m, 0, 0, 0, -1))  # noqa: E731
        self.assertEqual(last_chance(at(15, 15), day), at(15, 15))
        self.assertEqual(last_chance(at(15, 23, 30), day), at(15, 22))
        self.assertEqual(last_chance(at(16, 7), day), at(15, 22))
        self.clock.now = at(15, 17)
        self.hunter.set_day(*day)
        auctions = [{"text": "Canon EOS 600D · Hátralévő idő: 7 óra", "price": 1, "url": "https://www.vatera.hu/termek/2001",
                     "auction": True},
                    {"text": "Nikon D3200 · Hátralévő idő: 15 óra", "price": 1, "url": "https://www.vatera.hu/termek/2002",
                     "auction": True}]
        self.browser.by_url = [("vatera.hu", auctions)]
        self.hunter.add_watch("https://www.vatera.hu/listings/index.php?c=1&ob=ending")
        self.hunter.set_enabled(True)
        self.hunter.run(notify=self.notify)
        [explored] = [m[1]["content"] for m in self.llm.calls if "Friss hirdetések" in m[1]["content"]]
        self.assertIn("Canon", explored, "éjfélkor zárul: még ma este szól")
        self.assertNotIn("Nikon", explored, "reggel 8-kor zárul: reggel szól")
        with self.assertRaises(DealError):
            self.hunter.set_day(22, 8)

    def test_old_listings_are_looked_at_again_for_the_selling_speed(self):
        self.clock.now += 3 * 86400
        self.browser.by_url = [("PS5", CONTROLLERS)]
        self.browser.pages[NORMAL[0]["url"]] = {"short": ["Eladva"]}
        self.ready(query="PS5 kontroller", reference=14000)
        self.hunter.run(notify=self.notify)
        self.assertEqual(self.browser.looked, [NORMAL[0]["url"]], "termékenként egy, a régebben látottakból")
        speed = self.hunter.book.speed("3060 rtx ti")
        self.assertEqual((speed["checked"], speed["sold"], speed["days"]), (1, 1, 3))
        [found] = self.hunter.lookup("RTX 3060")
        self.assertEqual((found["name"], found["reference"]["price"]), ("rtx 3060 ti", 100000))
        self.assertIn("gyorsan kel: 1/1 újranézett hirdetés eltűnt, kb. 3 nap alatt", found["speed_text"])
        self.browser.by_url.append(("RTX", [listing(30, "RTX 3060 Ti Founders", 60000)]))
        self.ready(reference=100000)
        self.hunter.run(notify=self.notify)
        self.assertIn("Eladási sebesség: gyorsan kel", self.notices[-1][2])


class PriceBookTests(unittest.TestCase):
    def test_product_keys_from_titles(self):
        cases = {
            "ASUS Dual RTX 3060 Ti 8GB videókártya": "3060 rtx ti", "Gigabyte GeForce RTX 3060 Ti Gaming OC": "3060 rtx ti",
            "iPhone 13 Pro Max 128GB": "13 iphone max pro", "PS5 DualSense kontroller": "kontroller ps5",
            "Sony WH-1000XM4 fejhallgató": "sony wh1000xm4", "LEGO Technic 42115 Lamborghini": "42115 lego",
            "Makita DHP482 akkus fúró": "dhp482 makita", "Nike Air Max 90 42-es": "90 air max nike",
            "PlayStation 5 Digital Edition": "digital ps5", "RTX 3060 95000 Ft": "3060 rtx",
            "RTX 3060 Ti 8 GB": "3060 rtx ti", "AirPods Pro 2": "2 airpods pro",
            "Xiaomi Redmi Note 10 Pro": "10 note pro redmi", "Xbox Series X 1TB": "seriesx xbox",
            "Xbox Series S": "seriess xbox", "Nintendo Switch OLED": "oled switch", "Ryzen 5 5600X": "5 5600x ryzen",
        }
        for title, key in cases.items():
            self.assertEqual(product_key(title)[0], key, title)
        # ami nem egy biztos termék: csak a család, a márka egy cipőmérettel, vagy több generáció egy néven
        for title in ("Eladó videókártya jó állapotban", "Zara kabát 38-as", "PS5 Spider-Man 2", "PS5", "Nike cipő 42",
                      "AirPods Pro", "Nintendo Switch", "Xbox One S"):
            self.assertIsNone(product_key(title), title)
        for title in ("iPhone 13 tok", "Töltő iPhone 13-hoz", "RTX 3060 Ti hibás, alkatrésznek", "Keresek RTX 3060-at",
                      "3 db RTX 3060 csomagban", "2x RTX 3060"):
            self.assertFalse(usable(title), title)

    def test_brands_learned_from_the_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = PriceBook(Path(tmp), Clock())
            self.assertIsNone(book.key("Anker PowerCore 20000 powerbank"))
            self.assertEqual([book.learn_name("Anker PowerCore 20000") for _ in range(3)], ["", "", "anker"])
            self.assertEqual(book.key("Anker PowerCore 20000 powerbank")[0], "20000 anker", "most már modell nélkül")
            self.assertEqual(book.learn_name("Eladó PowerCore 20000"), "", "az „eladó” nem márka")
            book.save()
            self.assertEqual(PriceBook(Path(tmp), Clock()).brands, {"anker"})

    def test_the_usual_price_needs_enough_close_prices(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            book = PriceBook(Path(tmp), clock)
            rows = [listing(n, f"RTX 3060 Ti videókártya {n}", p) for n, p in enumerate((95000, 100000, 105000, 98000))]
            book.add(rows)
            self.assertIsNone(book.reference("3060 rtx ti"), "négy ár még kevés")
            book.add([listing(9, "RTX 3060 Ti", 102000)])
            ref = book.reference("3060 rtx ti")
            self.assertEqual((ref["price"], ref["n"]), (100000, 5))
            self.assertEqual(book.reference("3060 rtx ti", exclude=rows[0]["url"]), None, "a jelölt maga nem számít")
            mixed = [listing(20 + n, f"Galaxy S21 telefon {n}", p) for n, p in enumerate((20000, 150000, 25000, 140000, 90000))]
            book.add(mixed)
            self.assertIsNone(book.reference("galaxy s21"), "nagyon szórnak: nem mond árat")
            book.add([listing(1, "RTX 3060 Ti videókártya", 99000)])
            self.assertEqual(len([s for s in book._keys["3060 rtx ti"]["samples"]]), 5, "ugyanaz a hirdetés egyszer")
            book.save()
            clock.now += 50 * 86400
            again = PriceBook(Path(tmp), clock)
            self.assertEqual(again.stats()["prices"], 10)
            self.assertIsNone(again.reference("3060 rtx ti"), "a régi árak már nem számítanak")


class BrowseTests(HunterCase):
    """Böngészés napközben: munkamenetek egy-egy oldalon, pihenők, oldalváltás, a modell kötegben, öntanulás, napló."""

    def setUp(self):
        super().setUp()
        self.hunter.rng = random.Random(3)
        self.hunter.book.add([{**r, "platform": "hardverapro"} for r in NORMAL])   # az árbázis ismeri az RTX-et
        self.browser.results = PAGE
        self.hunter.set_enabled(True)

    def explored(self) -> list[str]:
        return [m[1]["content"] for m in self.llm.calls if "Friss hirdetések" in m[1]["content"]]

    def journal(self) -> list[str]:
        return [e["text"] for e in self.hunter.journal.entries()]

    def test_sessions_rests_and_the_journal(self):
        self.assertFalse(self.hunter.available(), "a böngészés még nincs bekapcsolva")
        self.hunter.set_browse(on=True, sites=["hardverapro", "vinted"])
        self.assertEqual(self.hunter.every_minutes(), 1.0)
        self.assertTrue(self.hunter.available())
        text = self.hunter.run(notify=self.notify)
        self.assertTrue(text.startswith("Böngészés: Hardverapró: "), text)
        self.assertIn("hardverapro.hu/aprok/hardver/", self.browser.urls[0], "a felkapott kategóriák egyike")
        self.assertEqual([t for _k, t, _x, _e in self.notices], ["Olcsó: Gigabyte RTX 3060 Ti Gaming OC"])
        self.assertIn("Böngészés közben (Hardverapró: ", self.notices[0][2])
        self.assertEqual(self.explored(), [], "a modellt kötegben kérdezi, nem oldalanként")
        self.assertFalse(self.hunter.available(), "két oldal között nézelődik, ahogy egy ember")
        self.assertIn("Most: Hardverapró", self.hunter.note())
        self.clock.now += 151
        self.assertTrue(self.hunter.available())
        self.hunter.run(notify=self.notify)
        self.clock.now += 26 * 60
        summary = self.hunter.run(notify=self.notify)
        self.assertRegex(summary, r"^\d\d:\d\d–\d\d:\d\d Hardverapró: \d+ oldal \(.+\), \d+ hirdetés; jó találat: "
                                  r"„Gigabyte RTX 3060 Ti Gaming OC” 70 000 Ft \(Hardverapró, kb\. 17 600 Ft haszon\)\. "
                                  r"Utána: Vinted\.$")
        self.assertEqual(self.explored(), [], "két ismeretlenért nem tölti be a modellt: a következő munkamenetre várnak")
        self.assertEqual(len(self.hunter._pending), 2)
        self.assertEqual(self.journal()[0], summary)
        self.assertTrue(self.journal()[1].startswith("Jó találat: „Gigabyte RTX 3060 Ti Gaming OC”"))
        self.assertFalse(self.hunter.available(), "egy rövid szünet, és vált")
        self.assertEqual(self.hunter.note(), "Hamarosan: Vinted.")
        # a következő oldal (nem vár a Hardverapró pihenőjére): a Vinteden a kezdőoldallal kezd, és megnyit egy hirdetést
        feed = [listing(40, "Nike pulóver", 8000, "www.vinted.hu"), listing(41, "Zara kabát", 12000, "www.vinted.hu")]
        self.browser.by_url = [("vinted.hu", feed)]
        self.clock.now += 61
        self.assertTrue(self.hunter.available())
        self.hunter.run(notify=self.notify)
        self.assertEqual(self.browser.urls[-1], "https://www.vinted.hu/")
        self.assertIn(self.browser.looked[-1], {r["url"] for r in feed})
        self.assertEqual(len(self.hunter._pending), 4, "az ismeretlenek a következő kötegre várnak")
        self.assertTrue(any(r["text"].startswith("Hirdetés – ") for r in self.hunter._pending),
                        "a megnyitott hirdetés címével együtt")

    def test_unknown_listings_go_to_the_model_in_batches(self):
        many = [listing(200 + n, f"Eladó holmi {n}", 5000 + n, "hardverapro.hu") for n in range(25)]
        self.browser.results = many
        self.hunter.set_browse(on=True, sites=["hardverapro"])
        self.hunter.run(notify=self.notify)
        lines = lambda text: [line for line in text.splitlines() if re.match(r"\d+\. ", line)]  # noqa: E731
        self.assertEqual([len(lines(b)) for b in self.explored()], [20])
        self.clock.now += 151
        self.hunter.run(notify=self.notify)
        self.assertEqual(len(self.explored()), 1, "a már elküldöttek nem jönnek újra, a maradék még kevés")
        self.clock.now += 26 * 60
        self.hunter.run(notify=self.notify)
        self.assertEqual([len(lines(b)) for b in self.explored()], [20], "5 ismeretlenért nem tölti be a modellt")
        # egy licit nem várhat: a munkamenet végén akkor is a modellhez megy
        self.hunter._pending.append({**listing(300, "Canon EOS 600D · Hátralévő idő: 3 óra", 1, "www.vatera.hu"),
                                     "site": "Vatera", "platform": "vatera", "auction": True, "left": 10800})
        self.clock.now += 40 * 60
        self.hunter.run(notify=self.notify)
        self.clock.now += 26 * 60
        self.hunter.run(notify=self.notify)
        self.assertEqual(len(self.explored()), 2)
        self.assertIn("Canon EOS 600D", self.explored()[-1])

    def test_a_captcha_stops_that_site_for_the_day(self):
        self.browser.challenge = {"vinted.hu"}
        self.hunter.set_browse(on=True, sites=["vinted"])
        self.hunter.run(notify=self.notify)
        self.assertEqual([t for _k, t, _x, _e in self.notices], ["Vinted: robot-ellenőrzés"])
        self.assertIn("Vinted: robot-ellenőrzést kért, ma már nem nézem (nem próbálom megkerülni).", self.journal())
        self.assertEqual(self.browser.looked, [], "a captchát nem próbálja megoldani, és nem nyit meg több oldalt")
        self.clock.now += 3 * 3600
        self.assertFalse(self.hunter.available())
        self.clock.now += 86400
        self.browser.challenge = set()
        self.assertTrue(self.hunter.available(), "másnap újra")

    def test_search_now_starts_browsing_when_there_is_no_search(self):
        self.hunter.set_browse(on=True, sites=["hardverapro"])
        self.assertTrue(self.hunter.run(notify=self.notify, force=True).startswith("Böngészés: Hardverapró: "))
        self.assertIn("Most: Hardverapró", self.hunter.note())

    def test_only_the_site_just_browsed_rests(self):
        self.hunter.set_browse(on=True, sites=["hardverapro", "jofogas"])
        self.hunter.run(notify=self.notify)
        self.clock.now += 76
        self.assertTrue(self.hunter.available(), "két oldalbetöltés között legfeljebb 75 másodperc")
        self.clock.now += 26 * 60
        summary = self.hunter.run(notify=self.notify)
        self.assertTrue(summary.endswith(" Utána: Jófogás."), summary)
        self.clock.now += 61
        self.hunter.run(notify=self.notify)
        self.assertIn("jofogas.hu", self.browser.urls[-1], "rögtön a következő oldal, a Hardverapró közben pihen")
        self.clock.now += 26 * 60
        summary = self.hunter.run(notify=self.notify)
        self.assertTrue(summary.endswith(" Utána: Hardverapró."), "a Hardverapró közben kipihente magát")
        # ha csak egy oldal van bejelölve, az pihen egy kicsit, és a Jarvis addig vár
        self.hunter.set_browse(sites=["jofogas"])
        self.clock.now += 61
        self.assertFalse(self.hunter.available())
        self.assertRegex(self.hunter.note(), r"^Pihen \d\d:\d\d-ig, utána: Jófogás\.$")
        self.clock.now += 14 * 60
        self.assertTrue(self.hunter.available())

    def test_vinted_at_most_every_two_hours(self):
        self.browser.by_url = [("vinted.hu", [])]
        self.hunter.set_browse(on=True, sites=["vinted"])
        self.hunter.run(notify=self.notify)
        self.clock.now += 26 * 60
        self.hunter.run(notify=self.notify)             # a munkamenet vége
        self.clock.now += 40 * 60
        self.assertFalse(self.hunter.available(), "a pihenő után sem: a Vintedre két óra")
        self.assertRegex(self.hunter.note(), r"^Pihen \d\d:\d\d-ig, utána: Vinted\.$")
        self.clock.now += 81 * 60
        self.assertTrue(self.hunter.available(), "két órával az előző Vinted-munkamenet vége után")

    def test_only_while_the_computer_is_on(self):
        at = lambda d, h, m=0: time.mktime((2027, 1, d, h, m, 0, 0, 0, -1))  # noqa: E731
        self.hunter.set_day(8, 22)
        self.hunter.set_browse(on=True, sites=["hardverapro"])
        self.clock.now = at(15, 23)
        self.assertFalse(self.hunter.available())
        self.assertFalse(self.hunter.browsing(), "este a Jarvis készenlétében sem fut")
        self.assertEqual(self.hunter.note(), "Napközben böngészik (8–22 óra); most nem dolgozik.")
        self.clock.now = at(15, 21, 50)
        self.assertTrue(self.hunter.browsing(), "napközben a Jarvis készenlétében is fut")
        self.hunter.run(notify=self.notify)
        self.clock.now = at(15, 22, 1)
        self.assertTrue(self.hunter.available(), "este lezárja a munkamenetet")
        summary = self.hunter.run(notify=self.notify)
        self.assertNotIn("Pihenő", summary)
        self.assertFalse(self.hunter.available())
        self.clock.now = at(16, 8, 5)
        self.assertTrue(self.hunter.available(), "reggel folytatja")

    def test_it_learns_which_pages_pay_and_what_sells_fast(self):
        hunter = self.hunter
        hunter.set_browse(on=True, sites=["hardverapro"])
        with hunter._lock:
            hunter._data["lanes"]["hardverapro:videokartyak"] = {"pages": 10, "candidates": 3, "deals": 2}
            hunter._data["lanes"]["hardverapro:alaplapok"] = {"pages": 10}
        picks = Counter(hunter._pick_lane("hardverapro", [], False)["name"] for _ in range(300))
        self.assertGreater(picks["Hardverapró: Videókártyák"], 3 * picks["Hardverapró: Alaplapok"],
                           "amelyik többet hozott, azt gyakrabban nézi")
        self.assertGreater(picks["Hardverapró: Alaplapok"], 0, "a többit sem hagyja el")
        # a gyorsan kelő termékből saját indulóoldal lesz (naponta egyszer, az első munkamenet előtt)
        self.clock.now += 3 * 86400
        for row in NORMAL[:2]:
            hunter.book.revisited("3060 rtx ti", row["url"], gone=True)
        hunter.run(notify=self.notify)
        [lane] = [lane for lane in hunter.state()["lanes"] if lane["kind"] == "tanult"]
        self.assertEqual((lane["name"], lane["site"]), ("Hardverapró: rtx 3060 ti", "hardverapro"))
        self.assertIn("hardverapro.hu/aprok/keres.php?stext=rtx%203060%20ti", lane["url"])
        learned = [t for t in self.journal() if t.startswith("Tanultam:")]
        self.assertEqual(learned, ["Tanultam: mostantól figyelem: rtx 3060 ti (a Hardveraprón; gyorsan kel: 2/2 "
                                   "újranézett hirdetés eltűnt, kb. 3 nap alatt)."])
        # ami sokáig semmit nem hozott, kimarad (és nem tanulja újra rögtön)
        with hunter._lock:
            hunter._data["lanes"][lane["id"]]["pages"] = 20
            hunter._data["session"] = {}
        self.clock.now += 86400
        hunter.run(notify=self.notify)
        self.assertEqual([lane for lane in hunter.state()["lanes"] if lane["kind"] == "tanult"], [])
        self.assertIn("leállítottam", " ".join(self.journal()))

    def test_hidden_twice_is_not_reported_for_a_while(self):
        for n in range(2):
            self.hunter._remember({"id": f"d{n}", "url": f"https://hardverapro.hu/apro/{900 + n}.html",
                                   "text": "RTX 3060 Ti", "price": 70000, "site": "Hardverapró",
                                   "product_key": "3060 rtx ti"})
            self.hunter.dismiss(f"d{n}")
        self.assertIn("Az ilyen találatokat („rtx 3060 ti”) 2-szer elrejtetted: 30 napig nem szólok róluk.",
                      self.journal())
        self.hunter.set_browse(on=True, sites=["hardverapro"])
        self.hunter.run(notify=self.notify)
        self.assertEqual(self.notices, [], "az RTX-ről most nem szól")
        self.hunter._remember({"id": "b1", "url": "https://hardverapro.hu/apro/999.html", "text": "RTX 3060 Ti",
                               "price": 70000, "site": "Hardverapró", "product_key": "3060 rtx ti"})
        self.hunter.mark_bought("b1", "item1")
        self.assertFalse(self.hunter._muted("3060 rtx ti", self.clock()), "amit megvettél, arról újra szól")

    def test_a_search_waiting_for_approval_does_not_wake_the_job(self):
        self.hunter.add_search("RTX 3060 Ti", ["hardverapro"])
        self.browser.results = RESULTS
        self.assertTrue(self.hunter.available(), "a felmérés esedékes")
        self.hunter.run(notify=self.notify)
        self.clock.now += 4 * 3600
        self.assertFalse(self.hunter.available(), "jóváhagyásig nincs dolga (a felület se jelezzen munkát)")


class JournalTests(unittest.TestCase):
    def test_days_newest_first_and_two_weeks_kept(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp) / "naplo.json", clock)
            journal.add("Reggel: Hardverapró")
            clock.now += 3600
            journal.add("Délben: Vinted")
            self.assertEqual([e["text"] for e in journal.entries()], ["Délben: Vinted", "Reggel: Hardverapró"])
            first = journal.days()[0]
            for _ in range(20):
                clock.now += 86400
                journal.add("egy nap")
            again = Journal(Path(tmp) / "naplo.json", clock)
            self.assertEqual(len(again.days()), 14)
            self.assertNotIn(first, again.days())
            self.assertEqual(again.entries(first), [])


class LaneTests(unittest.TestCase):
    def test_the_lane_runs_tasks_one_by_one_and_hands_results_back(self):
        lane, got = ModelLane(), []
        try:
            for n in range(3):
                lane.submit(lambda n=n: (threading.current_thread().name, n), got.append)
            while lane.busy:
                lane.deliver(wait=0.5)
        finally:
            lane.close()
        self.assertEqual(got, [("jarvis-modell-sor", 0), ("jarvis-modell-sor", 1), ("jarvis-modell-sor", 2)])
        deadline = time.monotonic() + 2
        while lane.alive and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(lane.alive)

    def test_model_calls_one_at_a_time_and_the_browser_stays_on_its_thread(self):
        browser, llm = FakeBrowser(), FakeLlm()
        llm.delay = 0.05
        with tempfile.TemporaryDirectory() as tmp:
            hunter = DealHunter(Path(tmp), PLATFORMS, lambda: browser, llm)
            for _ in range(3):
                hunter.add_search("RTX 3060 Ti", ["hardverapro"])
            hunter.set_enabled(True)
            hunter.run()
            self.assertEqual(len(llm.judged), 3)
        self.assertEqual((llm.most, llm.threads), (1, {"jarvis-modell-sor"}), "egyszerre egy modellhívás, a modell-sorban")
        self.assertEqual(browser.threads, {threading.current_thread().name}, "a böngésző a hívó szálán marad")

    def test_a_preempted_round_stops_the_lane(self):
        browser, llm = FakeBrowser(), FakeLlm()
        llm.delay = 1.0              # tovább tart, mint a kör egy várakozása (0,3 mp)

        def check():
            if llm.started.is_set():  # a felhasználó közbeszólt, miközben a modell dolgozik
                raise Preempted()

        with tempfile.TemporaryDirectory() as tmp:
            hunter = DealHunter(Path(tmp), PLATFORMS, lambda: browser, llm)
            hunter.add_search("RTX 3060 Ti", ["hardverapro"])
            hunter.set_enabled(True)
            with self.assertRaises(Preempted):
                hunter.run(check=check)
            self.assertEqual(hunter.state()["searches"][0]["status"], "felmérés", "a félbemaradt felmérés újra jön")
        deadline = time.monotonic() + 3
        while any(t.name == "jarvis-modell-sor" for t in threading.enumerate()) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(any(t.name == "jarvis-modell-sor" for t in threading.enumerate()))


# --- ajánlattétel egy helyi „Vinted” hirdetésen, valódi böngészővel ---

OFFER_PAGE = """<!doctype html><html lang="hu"><head><meta charset="utf-8"><title>PS5 kontroller | Vinted</title></head>
<body><header>{header}</header><main><h1>PS5 DualSense kontroller</h1><p>16 000 Ft</p>
<button type="button">Vásárlás</button> {offer}
<div id="modal" role="dialog" hidden><label>Ajánlatod (Ft) <input id="amount" type="number"></label>
<button type="button" id="send">{send}</button></div><p id="out"></p></main>
<script>
const offer = document.getElementById('offer');
if (offer) offer.onclick = () => {{ modal.hidden = false; }};
send.onclick = () => {{
  fetch('/offer?amount=' + amount.value, {{method: 'POST'}});
  modal.hidden = true;
  out.textContent = 'Ajánlat elküldve: ' + amount.value + ' Ft';
}};
</script></body></html>"""
OFFER_BUTTON = '<button type="button" id="offer">Ajánlattétel</button>'
PAGES = {
    "/items/1-ps5": OFFER_PAGE.format(header="<a href='/member'>Profil</a>", offer=OFFER_BUTTON, send="Ajánlat küldése"),
    "/items/2-sima": OFFER_PAGE.format(header="<a href='/member'>Profil</a>", offer=OFFER_BUTTON, send="Ajánlatot teszek"),
    "/items/3-kint": OFFER_PAGE.format(header="<button type='button'>Bejelentkezés</button>", offer=OFFER_BUTTON,
                                       send="Ajánlat küldése"),
    "/items/4-nincs": OFFER_PAGE.format(header="<a href='/member'>Profil</a>", offer="", send="Ajánlat küldése"),
}


class OfferSite:
    def __init__(self):
        self.posts: list[str] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = PAGES.get(self.path.split("?")[0], "<h1>Nincs</h1>")
                data = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                site.posts.append(self.path.split("amount=")[-1])
                self.send_response(204)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}{path}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class OfferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = make_cfg(Path(cls.tmp.name))
        cfg.browser.headless = True
        cls.browser = BrowserController(cfg.browser, allow_local=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.site = OfferSite()
        self.dir = tempfile.TemporaryDirectory()
        self.hunter = DealHunter(Path(self.dir.name), PLATFORMS, lambda: self.browser, None)
        self.prompts: list[str] = []
        self.answer = True
        # mint az app: a böngésző kérdése elé a jóváhagyó ablak első sora (WorkDesk.confirm_context)
        self.browser.confirm = lambda prompt: self.confirm(f"{self.hunter.offering}\n\n{prompt}")

    def tearDown(self):
        self.site.close()
        self.dir.cleanup()

    def confirm(self, prompt: str) -> bool:
        self.prompts.append(prompt)
        return self.answer

    def deal(self, path: str, platform: str = "vinted") -> str:
        deal = {"id": f"d{len(self.prompts)}{path[-3:]}", "url": self.site.url(path), "text": "PS5 DualSense kontroller",
                "site": "Vinted", "platform": platform, "price": 16000, "median": 20000, "offer": 14000,
                "at": time.time()}
        self.hunter._remember(deal)
        return deal["id"]

    def test_the_offer_goes_only_with_your_approval_and_the_exact_amount(self):
        deal = self.deal("/items/1-ps5")
        self.answer = False
        self.assertEqual(self.hunter.make_offer(deal, None, confirm=self.confirm), "Rendben, nem tettem ajánlatot.")
        self.assertEqual(self.site.posts, [])
        self.assertIn("Ajánlat a Vinteden: 14 000 Ft – „PS5 DualSense kontroller” (most 16 000 Ft).", self.prompts[-1])
        self.assertIn("fizetni magától nem fogok", self.prompts[-1])
        self.answer = True
        result = self.hunter.make_offer(deal, "13 500", confirm=self.confirm)
        self.assertEqual(result, "Elküldtem az ajánlatot (13 500 Ft) a Vinteden. Ha az eladó elfogadja, a Vinted értesít, "
                                 "és akkor megveheted.")
        self.assertEqual(self.site.posts, ["13500"])
        self.assertEqual(self.hunter.get(deal)["offer_sent"]["price"], 13500)
        self.assertEqual(self.hunter.offering, "", "a jóváhagyó ablak első sora csak az ajánlattétel alatt")
        with self.assertRaisesRegex(DealError, "Erre már tettél ajánlatot"):
            self.hunter.make_offer(deal, None, confirm=self.confirm)

    def test_a_send_button_the_browser_would_not_ask_about_still_needs_approval(self):
        deal = self.deal("/items/2-sima")
        self.answer = False
        self.assertEqual(self.hunter.make_offer(deal, None, confirm=self.confirm), "Rendben, nem tettem ajánlatot.")
        self.assertIn('Jarvis elküldené (erre kattintana: "Ajánlatot teszek")', self.prompts[-1])
        self.assertEqual(self.site.posts, [])

    def test_logged_out_or_no_offer_button_stops_and_bad_amounts(self):
        with self.assertRaisesRegex(DealError, "Ki vagy jelentkezve"):
            self.hunter.make_offer(self.deal("/items/3-kint"), None, confirm=self.confirm)
        with self.assertRaisesRegex(DealError, "Nem találom a hirdetésen az Ajánlattétel gombot"):
            self.hunter.make_offer(self.deal("/items/4-nincs"), None, confirm=self.confirm)
        with self.assertRaisesRegex(DealError, "kevesebb a kért árnál"):
            self.hunter.make_offer(self.deal("/items/1-ps5"), 16000, confirm=self.confirm)
        with self.assertRaisesRegex(DealError, "csak a Vinteden"):
            self.hunter.make_offer(self.deal("/items/1-ps5", platform="jofogas"), None, confirm=self.confirm)
        self.assertEqual((self.site.posts, self.prompts), ([], []))


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


class StartAndLimitTests(HunterCase):
    def test_start_runs_everything_around_the_clock_until_stopped(self):
        """Indítás: minden keresés, a figyelt oldalak és a böngészés is, napszaktól függetlenül, amíg le nem állítod."""
        self.hunter.set_day(8, 20)
        state = self.hunter.start()
        self.assertEqual((state["running"], state["browse"]["on"], state["day"], state["started"]),
                         (True, True, [0, 24], self.clock.now))
        self.assertTrue(self.hunter.browsing() and self.hunter.in_day())
        self.clock.now += 600
        self.assertEqual(self.hunter.start()["started"], self.clock.now - 600, "a második Indítás nem kezd újra")
        state = self.hunter.stop()
        self.assertEqual((state["running"], state["browse"]["on"], state["started"]), (False, False, None))
        self.assertFalse(self.hunter.available())
        texts = [e["text"] for e in self.hunter.journal.entries()]
        self.assertIn("Elindítottad az automata kereskedést (keresések, figyelt oldalak, böngészés, azonnali figyelő).",
                      texts)
        self.assertIn("Leállítottad az automata kereskedést.", texts)

    def test_the_buy_limit_applies_to_searches_offers_and_auctions(self):
        """Legfeljebb ennyiért vegyen: a keresés a limit fölötti olcsót sem jelöli (az oldalát meg sem nyitja); a
        licitnél legfeljebb a limitig javasol; a Vinteden ajánlatot javasol, ha az belefér."""
        self.ready()
        with self.assertRaises(DealError):
            self.hunter.set_limits(max_buy="sok")
        self.assertEqual(self.hunter.set_limits(max_buy="50 000", min_profit="6000")["max_buy"], 50000)
        self.assertEqual(self.hunter.state()["browse"]["min_profit"], 6000)
        self.hunter.run(notify=self.notify)
        self.assertEqual(self.hunter.state()["deals"], [], "az 55 000 Ft-os Gigabyte a limit fölött van")
        self.assertEqual(self.browser.looked, [])
        source = {"id": "w", "name": "Vatera", "min_profit": 5000}
        reference = {"price": 100000, "n": 5, "name": "Nintendo Switch OLED"}
        auction = {"text": "Nintendo Switch OLED", "price": 20000, "url": "https://www.vatera.hu/termek/9", "site": "Vatera",
                   "platform": "vatera", "auction": True, "left": 3600}
        self.assertEqual(self.hunter._explore_deal(source, auction, reference)["max_bid"], 50000)
        vinted = {"text": "Nintendo Switch OLED", "price": 60000, "url": "https://www.vinted.hu/items/9", "site": "Vinted",
                  "platform": "vinted"}
        self.assertEqual(self.hunter._explore_deal(source, vinted, reference)["offer"], 50000,
                         "olcsó, de a limit fölött: ajánlattal belefér")
        self.assertIsNone(self.hunter._explore_deal(source, {**vinted, "platform": "jofogas", "url": "https://jofogas.hu/9"},
                                                    reference))
        self.assertIsNone(self.hunter.set_limits(max_buy="")["max_buy"], "üresen nincs limit")
        self.clock.now += 4 * 3600
        self.hunter.run(notify=self.notify, force=True)
        self.assertEqual([d["price"] for d in self.hunter.state()["deals"]], [55000])


class MoneyTests(HunterCase):
    """Pénzügyi keret: a havi keret (ha fogy, csak a maradékba beleférő ajánlat jön), és szól, ha a keret nagy része
    elment, ha elfogyott, vagy ha túl sok megvett tárgy vár eladásra."""

    def setUp(self):
        super().setUp()
        from jarvis_core.selling import ItemStore
        self.store = ItemStore(Path(self.tmp.name) / "eladas")
        self.hunter.items = self.store.all

    def test_budget_limits_the_deals_and_warns_once(self):
        now = self.clock.now
        self.store.create(title="RTX 3060 Ti", bought_price=40000, bought_shipping=1500, bought_at=now - 3600)
        self.store.create(title="Régi vétel", bought_price=90000, bought_at=now - 60 * 86400)   # másik hónap
        with self.assertRaises(DealError):
            self.hunter.set_money(budget="sok")
        state = self.hunter.set_money(budget="100 000")
        self.assertEqual((state["money"]["spent"], state["money"]["remaining"]), (41500, 58500))
        self.assertTrue(self.hunter._affordable(58000) and not self.hunter._affordable(59000),
                        "a havi keret maradéka is limit")
        self.ready()
        self.hunter.run(notify=self.notify)
        self.assertEqual([d["price"] for d in self.hunter.state()["deals"]], [55000], "az 55 000-es még belefér")
        self.store.create(title="PS5 kontroller", bought_price=40000, bought_at=now - 60)
        said = self.hunter.money_check(self.notify)
        self.assertEqual(len(said), 1)
        self.assertIn("A havi keret 82%-a elment: már csak 18 500 Ft maradt", said[0])
        self.assertEqual(self.notices[-1][3]["actions"], [{"action": "ledger", "label": "Vételeim"}])
        self.assertEqual(self.hunter.money_check(self.notify), [], "havonta egyszer")
        self.store.create(title="Makita", bought_price=20000, bought_at=now - 30)
        self.assertIn("Elfogyott a havi keret", self.hunter.money_check(self.notify)[0])
        self.assertFalse(self.hunter._affordable(1000), "elfogyott: semmi nem fér bele")

    def test_a_cancelled_purchase_is_removed_everywhere(self):
        """Visszamondva: a találat kikerül, a jó pontja visszavonódik, a havi keretbe nem számít bele, és erről a
        hirdetésről (akkor sem, ha leárazzák) többé nem szól."""
        self.hunter.set_money(budget="100 000")
        self.ready()
        self.hunter.run(notify=self.notify)
        [deal] = self.hunter.state()["deals"]
        item = self.store.create(title=deal["text"], bought_price=deal["price"], bought_at=self.clock.now)
        self.hunter.mark_bought(deal["id"], item.id)
        self.hunter.money_check(lambda *a, **k: None)      # a vétel után (a felület is így hívja)
        self.assertEqual((self.hunter.money_status()["spent"], self.hunter._budget_left()), (55000, 45000))
        key = deal["product_key"]
        self.assertEqual(self.hunter._data["feedback"][key]["bought"], 1)
        found = self.hunter.cancel_bought(item.id, item.name)
        self.store.delete(item.id)
        self.assertEqual(found["id"], deal["id"])
        self.assertEqual(self.hunter.state()["deals"], [], "a találatok közül is kikerült")
        self.assertEqual(self.hunter._data["feedback"][key]["bought"], 0)
        self.assertEqual((self.hunter.money_status()["spent"], self.hunter._budget_left()), (0, 100000),
                         "a havi keretből sem fogy")
        self.assertIn("Visszamondtad a vételt: „Gigabyte RTX 3060 Ti Gaming OC” (55 000 Ft)",
                      self.hunter.journal.entries()[0]["text"])
        self.notices.clear()
        self.browser.results = NORMAL + [listing(10, "Gigabyte RTX 3060 Ti Gaming OC", 50000)]   # leárazta
        self.clock.now += 3 * 3600
        self.hunter.run(notify=self.notify, force=True)
        self.assertEqual(self.notices, [], "a visszamondott hirdetésről többé nem szól")
        self.assertIsNone(self.hunter.cancel_bought("nincs-ilyen", "Kézzel felvett"), "kézzel felvett vétel")

    def test_too_many_unsold_items(self):
        now = self.clock.now
        for n in range(3):
            self.store.create(title=f"Tárgy {n}", bought_price=10000, bought_at=now - (10 - n) * 86400)
        sold = self.store.create(title="Eladott", bought_price=5000, sold_price=9000)
        self.store.mark(sold.id, sold=True)
        self.hunter.set_money(stock_count="2")
        self.assertTrue(self.hunter.money_status()["over_stock"])
        [said] = self.hunter.money_check(self.notify)
        self.assertIn("Felhalmozódtak a megvett tárgyak: 3 vár eladásra, 30 000 Ft van bennük", said)
        self.assertIn("a legrégebbiek: Tárgy 0 (10 napja), Tárgy 1 (9 napja), Tárgy 2 (8 napja)", said)
        self.assertEqual(self.hunter.money_check(self.notify), [], "naponta egyszer")
        self.clock.now += 86400
        self.assertEqual(len(self.hunter.money_check(self.notify)), 1, "másnap újra, ha még mindig így van")
        self.hunter.set_money(stock_count="", stock_value="40000")
        self.assertFalse(self.hunter.money_status()["over_stock"], "30 000 Ft még belefér")


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
        self.assertEqual(report["month"], {"profit": 40000, "count": 1, "revenue": 95000})
        self.assertEqual(report["total"], {"profit": 43000, "count": 2, "spent": 64000, "revenue": 107000, "roi": 67})
        self.assertEqual((report["open"]["count"], report["open"]["spent"], report["open"]["unknown"]), (1, 15000, 1),
                         "a Nike-nak nincs ára: nem tudni, mennyi haszon várható belőle")
        self.assertEqual(report["days"], 20, "átlagosan (10 + 30) / 2 nap alatt kelt el")
        self.assertEqual([r["name"] for r in report["rows"]], ["Nike Air Max", "RTX 3060 Ti", "PS5 kontroller"],
                         "elöl a meg nem eladott, aztán a legutóbb eladott")
        self.assertEqual([(m["profit"], m["count"]) for m in report["months"]], [(40000, 1), (3000, 1)])

    def test_costs_expected_profit_stages_and_todays_price(self):
        """A bekerülés a vételár, a posta és az egyéb költség; a haszon az eladás költségeivel (posta, díj) együtt. Amíg
        nem kelt el, a tervezett eladási árból (vagy a hirdetési árból) számolja a várható hasznot, és az árbázisból,
        hogy most mennyiért adják."""
        from jarvis_core.deals import profit_report
        from jarvis_core.selling import ItemStore
        with tempfile.TemporaryDirectory() as tmp:
            store = ItemStore(Path(tmp) / "eladas")
            book = PriceBook(Path(tmp))
            book.add([{"text": f"RTX 3060 Ti videókártya {n}", "price": 100000 + n * 1000,
                       "url": f"https://hardverapro.hu/apro/{100000 + n}.html"} for n in range(5)])
            now = time.time()
            road = store.create(title="RTX 3060 Ti Gaming OC", bought_price=55000, bought_shipping=1500,
                                bought_extra=2000, target_price=90000, in_transit=True, bought_at=now - 2 * 86400,
                                bought_from="Hardverapró")
            listed = store.create(title="PS5 kontroller", bought_price=9000, price=14000, sell_fee="500",
                                  targets=["vinted"])
            store.mark(listed.id, platform="vinted")
            sold = store.create(title="Nike Air Max 90", bought_price=15000, bought_shipping=1200, sold_price=24000,
                                sell_shipping=1500, sell_fee=800, bought_at=now - 5 * 86400)
            store.mark(sold.id, sold=True)
            guess = store.create(title="Valami", bought_price=5000, market_price=10000)
            with self.assertRaises(Exception):
                store.update(road.id, bought_shipping="sok")
            report = profit_report(store.all(), now, book=book)
        rows = {r["id"]: r for r in report["rows"]}
        self.assertEqual([r["stage"] for r in report["rows"]], ["úton", "nálad", "fent", "eladva"])
        self.assertEqual((rows[road.id]["cost"], rows[road.id]["estimate"], rows[road.id]["expected"],
                          rows[road.id]["roi"], rows[road.id]["days"]), (58500, 90000, 31500, 54, 2))
        self.assertEqual(rows[road.id]["market_now"]["price"], 102000, "most ennyiért adják (árbázis)")
        self.assertEqual((rows[listed.id]["estimate"], rows[listed.id]["expected"]), (14000, 4500),
                         "tervezett ár nélkül a hirdetési ár, mínusz a díj")
        self.assertEqual((rows[guess.id]["estimate"], rows[guess.id]["expected"]), (9000, 4000),
                         "se tervezett, se hirdetési ár: a szokásos ár 90%-a")
        self.assertEqual((rows[sold.id]["cost"], rows[sold.id]["profit"], rows[sold.id]["roi"]), (16200, 5500, 34))
        self.assertEqual(report["open"], {"count": 3, "spent": 72500, "expected": 40000, "value": 113000,
                                          "unknown": 0})
        self.assertEqual(report["stages"], {"úton": 1, "nálad": 1, "fent": 1, "eladva": 1})
        self.assertEqual(report["total"]["profit"], 5500)
        self.assertEqual(rows[road.id]["advice"], [], "kb. 92 000 Ft-ért kelne el: nem sokkal több, mint a 90 000")
        later = profit_report(store.all(), now + 8.5 * 86400, book=book)
        advice = {r["id"]: r["advice"] for r in later["rows"]}
        self.assertEqual(advice[road.id], ["10 napja úton van: kérdezd meg az eladót, vagy jelöld megérkezettnek."])
        self.assertEqual(advice[guess.id], ["8 napja nálad van, és még nincs fent sehol: tölts fel róla hirdetést."])
        self.assertEqual(advice[listed.id], [])
        store.update(road.id, target_price=80000)
        cheaper = {r["id"]: r["advice"] for r in profit_report(store.all(), now, book=book)["rows"]}
        self.assertEqual(cheaper[road.id], ["Most drágábban adják: kérhetsz többet (kb. 91 800 Ft-ért is elkelhet)."])


if __name__ == "__main__":
    unittest.main()
