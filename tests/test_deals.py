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
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_offline import make_cfg  # noqa: E402
from test_web_browser import HAS_CHROMIUM  # noqa: E402

from jarvis_core import condition as conditions  # noqa: E402
from jarvis_core import deals as deals_module  # noqa: E402
from jarvis_core.browser import BrowserController, BrowserError  # noqa: E402
from jarvis_core.deals import (SUSPECT_RATIO, DealError, DealHunter, ModelLane, assess, buy_fee, careful_low,  # noqa: E402
                               expected_profit, explore_top, highest_ok, last_chance, left_text, market, matches_query,
                               newest_first, offer_price, parse_auction, profit_floor, safe_sale, safety_text)
from jarvis_core.journal import Journal  # noqa: E402
from jarvis_core.pricebook import PriceBook, pace, product_key, sized, unusable, usable, wearable  # noqa: E402
from jarvis_core import pricebook as pricebook_module  # noqa: E402
from jarvis_core.scheduler import Preempted  # noqa: E402
from jarvis_core.selling import PLATFORMS, listing_key  # noqa: E402


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
SWITCH_VINTED = [listing(65 + n, f"Nintendo Switch OLED {n}", p, "www.vinted.hu") for n, p in
                 enumerate((99000, 101000, 100000, 97000, 103000))]
CONTROLLERS = [listing(70 + n, f"PS5 DualSense kontroller {n}", p, "www.jofogas.hu") for n, p in
               enumerate((14000, 13500, 15000, 13800, 14500))]
# Egy figyelt Hardverapró-kategória (videókártyák, a legújabb elöl): egy olcsó és egy szokásos árú RTX 3060 Ti (ezeket
# az árbázis ismeri), egy fejhallgató, aminek a címéből nem derül ki a típusa (a modell nevezi meg), és egy semmiség.
CATEGORY = "https://hardverapro.hu/aprok/hardver/videokartya/index.html"
PAGE = [listing(20, "Gigabyte RTX 3060 Ti Gaming OC", 70000), listing(21, "RTX 3060 Ti", 95000),
        listing(22, "Eladó zajszűrős Sony fejhallgató, alig használt", 30000), listing(23, "Laptop táska", 5000)]
HEADPHONES = [listing(80 + n, f"Sony WH-1000XM4 fejhallgató {n}", p, "www.jofogas.hu") for n, p in
              enumerate((70000, 68000, 72000, 69000, 71000))]
HEADPHONES_VINTED = [listing(60 + n, f"Sony WH-1000XM4 {n}", p, "www.vinted.hu") for n, p in
                     enumerate((70500, 69500, 71500, 68500, 70000))]
RTX_JOFOGAS = [listing(50 + n, f"RTX 3060 Ti {n}", p, "www.jofogas.hu") for n, p in enumerate((97000, 101000, 99000))]
# az árbázisban két oldalról (Vinted, Jófogás): biztos ár
AIRPODS = [{**listing(90 + n, f"Apple AirPods Pro 2 {n}", p, f"www.{site}.hu"), "platform": site} for n, (p, site) in
           enumerate(zip((50000, 48000, 52000, 49000, 51000), ("vinted", "jofogas") * 3))]


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
        self.pings: list[tuple] = []
        self.haggle_tips: list[tuple] = []
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
        # a gyors jelzés („Ígéretes új hirdetés, ellenőrzés folyamatban”: notice.pending) és az alku-tipp („Alkuval
        # megérheti”) külön; a notices a többi (a megérő ajánlat, a bizonytalan tippek, a javaslatok)
        target = self.pings if extra.get("pending") else self.haggle_tips if title.startswith("Alkuval megérheti") \
            else self.notices
        target.append((kind, title, text, extra))

    def vetted(self, hunter: DealHunter, rows: list[dict], issues: tuple = ()) -> None:
        """Ezeknek az összehasonlítóknak a leírását már elolvasta (allapotok.json): hibátlanok, vagy issues."""
        for row in rows:
            hunter._vetted[listing_key(row["url"])] = {"at": self.clock.now, "issues": list(issues),
                                                       "state": "hibás" if issues else "hibátlan"}

    @staticmethod
    def page_checked(deal: dict) -> dict:
        """Egy jelölt, aminek az adatlapját már megnézte (a végső minősítéshez, _verdict)."""
        return {**deal, "page_checked": True, "details": "RTX 3060 Ti. Hibátlan, dobozában. Eladó: Peti (12 értékelés)"}

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

    def test_the_day_in_numbers(self):
        """A mai számok: hány hirdetést látott, hány volt új, mi miért esett ki, hány jelölt és jó találat; egy hirdetés egy
        nap egy számban egyszer számít (a következő kör nem duplázza)."""
        self.ready()
        self.hunter.run(notify=self.notify)
        counts = self.hunter.state()["funnel"]["counts"]
        self.assertEqual((counts["seen"], counts["other"], counts["new"], counts["not_cheap"], counts["candidates"],
                          counts["found"]), (11, 3, 8, 7, 1, 1),
                         "11 hirdetés: a kereső, a hibás és a táska nem a keresett termék; 8 új, ebből 7 nem elég olcsó")
        self.hunter.run(notify=self.notify, force=True)
        self.assertEqual(self.hunter.state()["funnel"]["counts"]["seen"], 11, "ugyanaz a hirdetés egyszer")
        self.clock.now += 86400
        self.assertEqual(self.hunter.state()["funnel"]["counts"], {}, "másnap újra nulláról")

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

    def test_a_fast_alert_rings_before_the_model_and_the_model_checks_right_after(self):
        """Gyors riasztás (alapból be): az adatlap és az újraszámolás után rögtön csenget („a modell még nem nézte
        meg”), és a kör modell-sora rögtön utána megnézi: ha rendben, ellenőrzött lesz (nem szól újra); ha a modell
        szerint nem ez, „Mégsem: …” jön (nem riaszt), és a találatnál is látszik. Kikapcsolva a modell a csengetés előtt
        kérdez."""
        self.assertTrue(self.hunter.state()["fast_alert"])
        self.ready()
        self.hunter.run(notify=self.notify)
        [(_kind, title, text, extra)] = self.notices
        self.assertEqual(title, "Olcsó: Gigabyte RTX 3060 Ti Gaming OC")
        self.assertTrue(extra["urgent"])
        self.assertIn("de a modell még nem nézte meg (gyors riasztás: a modell utána nézi meg)", text)
        [deal] = self.hunter.state()["deals"]
        self.assertEqual((deal["verified"], deal.get("model_pending"), deal.get("unchecked"), deal["note"]),
                         (True, None, None, "Ugyanaz a kártya."), "a kör modell-sora utána megnézte")
        self.llm.ok = False
        other = self.make("nem-az")
        self.ready(other)
        self.notices.clear()
        other.run(notify=self.notify)
        self.assertEqual([n[1] for n in self.notices], ["Olcsó: Gigabyte RTX 3060 Ti Gaming OC",
                                                        "Mégsem: Gigabyte RTX 3060 Ti Gaming OC"])
        self.assertFalse({"urgent", "deal"} & set(self.notices[1][3]), "a „Mégsem” nem riaszt")
        [deal] = other.state()["deals"]
        self.assertIn("Csak a doboza", deal["model_no"])
        slow = self.make("lassu")
        self.assertFalse(slow.set_limits(fast_alert=False)["fast_alert"])
        self.ready(slow)
        self.notices.clear()
        slow.run(notify=self.notify)
        self.assertEqual(self.notices, [], "kikapcsolva a modell előbb nemet mondott")

    def test_the_model_can_say_no_and_without_a_model_it_still_rings(self):
        self.llm.ok = False
        self.hunter.set_fast_alert(False)      # a modell a csengetés előtt kérdez
        self.ready()
        self.hunter.run(notify=self.notify)
        self.assertEqual(self.notices, [])
        other = self.make("masik", llm=None)
        self.ready(other)
        other.run(notify=self.notify)
        [notice] = self.notices
        self.assertEqual(notice[1], "Olcsó: Gigabyte RTX 3060 Ti Gaming OC",
                         "az adatlap és az újraszámolás után modell nélkül is sürgősen szól")
        self.assertTrue(notice[3]["urgent"])
        self.assertIn("a modell még nem nézte meg (nincs beállítva modell)", notice[2])
        self.assertNotIn("Amint lehet, a modell is megnézi", notice[2], "modell nélkül nincs utólagos ellenőrzés")
        [deal] = other.state()["deals"]
        self.assertEqual((deal["verified"], deal.get("model_pending"), deal["safety"]["level"]), (False, None, "jó"),
                         "ellenőrizetlen: legfeljebb „jó”")
        self.assertEqual(other.state()["tips"], [])
        self.assertEqual(len(self.pings), 2, "a gyors jelzés mindkettőnél ment (az ellenőrzés előtt)")

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

    def test_a_slightly_expensive_fresh_listing_is_a_haggle_tip(self):
        """Ami most kicsit drága, de legfeljebb 15% alkuval megérne, arról egy „Érdemes megnézni” tipp jön („Alkuval
        megérheti”, a javasolt ajánlattal, rövid pittyenéssel: notice.tip); a jóval drágábbról nem. Ezt a kör nem méri fel
        újra (neked szól), és az alkuképes hirdetések kapcsolójával ki is kapcsolható."""
        self.browser.results = NORMAL + [listing(30, "ASUS RTX 3060 Ti Dual", 78000),
                                         listing(31, "MSI RTX 3060 Ti Ventus", 90000)]
        self.ready()
        self.hunter.run(notify=self.notify)
        self.assertEqual(self.notices, [], "egyik sem elég olcsó")
        [(_kind, title, text, extra)] = self.haggle_tips
        self.assertEqual(title, "Alkuval megérheti: ASUS RTX 3060 Ti Dual")
        self.assertIn("78 000 Ft. Most kicsit drága, de ha kb. 70 000 Ft-ért (10%-kal olcsóbban) megkapod, kb. 18 500 Ft "
                      "haszon lehet benne", text)
        self.assertTrue(extra["tip"])
        self.assertNotIn("urgent", extra)
        [tip] = self.hunter.state()["tips"]
        self.assertEqual((tip["haggle"], tip["profit"], tip["usual"]), ({"offer": 70000, "cut": 0.1}, 18500, 100000))
        self.assertEqual(self.hunter.state()["funnel"]["counts"]["haggle_tips"], 1)
        game = deals_module._Round(lambda: None, self.notify, None)
        self.hunter._step_tips(game)
        self.assertEqual(list(game.work), [], "az alku-tippet nem méri fel újra")
        other = self.make("alku-ki")
        other.set_haggle(on=False)
        self.ready(other)
        self.haggle_tips.clear()
        other.run(notify=self.notify)
        self.assertEqual(self.haggle_tips, [], "kikapcsolva nincs alku-tipp")

    def test_tips_go_out_after_every_step(self):
        """A tippek értesítése nem a kör végén megy ki (egy böngészési kör sokáig tart), hanem minden lépés után."""
        sent = []
        game = deals_module._Round(lambda: None, self.notify, None)
        game.flush = lambda: sent.append(len(game.work))
        game.next([lambda: None, lambda: None])
        game.loop()
        self.assertEqual(sent, [1, 0])

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
        [(_kind, title, text, extra)] = self.notices
        self.assertEqual(title, "Változott az ár? RTX 3060 Ti")
        self.assertTrue(extra["quiet"], "csak javaslat: a telefonra nem megy, a listában ott van")
        self.assertIn("inkább 100 000 Ft körül", text)
        self.hunter.run(notify=self.notify, force=True)
        self.assertEqual(len(self.notices), 1, "egyszer szól")
        self.assertEqual(self.hunter.state()["searches"][0]["market"]["median"], 100000)

    def test_a_much_lower_current_price_level_is_used_for_a_search(self):
        """A jóváhagyott szokásos ár (100 000) régi: most a hirdetések többsége kb. 60 000. Egy 55 000-es hirdetés a régi
        árhoz mérve nagy haszon lenne, a mostanihoz mérve nem éri meg: nem szól róla. Ha a mostanihoz mérve is megéri,
        szól, és megmondja, mihez mérte."""
        now = [listing(50 + n, f"RTX 3060 Ti videókártya {n + 20}", 60000 + n * 500) for n in range(8)]
        self.browser.results = now + [listing(70, "MSI RTX 3060 Ti Ventus", 55000)]
        self.ready()
        self.assertEqual(self.hunter.run(notify=self.notify), "1 keresés: nincs új olcsó hirdetés.")
        self.browser.results.append(listing(71, "Gigabyte RTX 3060 Ti Eagle", 35000))
        self.assertEqual(self.hunter.run(notify=self.notify, force=True), "1 keresés: 1 olcsó hirdetés.")
        [(_kind, _title, text, _extra)] = [n for n in self.notices if n[1].startswith("Olcsó")]
        self.assertIn("a mostani árhoz mértem", text)
        self.assertIn("kevesebbért, mint a jóváhagyott 100 000 Ft", text)

    def test_only_eight_candidates_per_round_the_rest_next_time(self):
        # (a szokásos árú hirdetések vannak többen: ha az olcsók lennének, az lenne a mostani ár, lásd
        # test_a_much_lower_current_price_level_is_used_for_a_search)
        more = [listing(60 + n, f"RTX 3060 Ti videókártya {n + 7}", 95000 + n * 1000) for n in range(6)]
        self.browser.results = NORMAL + more + [listing(40 + n, f"RTX 3060 Ti Gamer {n}", 50000 + n * 100)
                                                for n in range(10)]
        self.ready()
        self.assertEqual(self.hunter.run(notify=self.notify), "1 keresés: 8 olcsó hirdetés.")
        self.assertEqual(len(self.browser.looked), 8)
        self.assertEqual(self.hunter.run(notify=self.notify, force=True), "1 keresés: 2 olcsó hirdetés.")
        self.assertEqual(len(self.notices), 10, "a maradék a következő körben, és egyik sem kétszer")

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
        other.set_fast_alert(False)            # a modell a csengetés előtt kérdez
        self.ready(other)
        other.run(notify=self.notify)
        self.assertEqual((self.notices, other.state()["deals"]), ([], []))
        self.assertIn("a modell szerint gyanús: Előre utalást kér",
                      " ".join(e["text"] for e in other.journal.entries()))
        # gyors riasztásnál előbb szól, de ha a modell utólag gyanúsnak látja: „Mégsem”, és lekerül a találatok közül
        fast = self.make("gyors")
        self.ready(fast)
        fast.run(notify=self.notify)
        self.assertEqual([n[1] for n in self.notices], ["Olcsó: Gigabyte RTX 3060 Ti Gaming OC",
                                                        "Mégsem: Gigabyte RTX 3060 Ti Gaming OC"])
        self.assertIn("a modell szerint gyanús: Előre utalást kér", self.notices[1][2])
        self.assertEqual(fast.state()["deals"], [])
        self.assertTrue(any(e["text"].startswith("Gyanús, utólag levettem") for e in fast.journal.entries()))

    def test_the_model_sees_the_listing_photos_too(self):
        """A végső modellkérdés a hirdetés első két fotóját is megkapja (letöltve, kicsinyítve): a sérülés, a letöltött
        kép és a más termék a fotón látszik. Ha a modell nem tud képet, fotók nélkül kérdez."""
        import io
        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGB", (900, 700), (40, 40, 40)).save(buffer, "JPEG")
        asked = []
        self.browser.pages = {CHEAP: {"text": "Gigabyte RTX 3060 Ti Gaming OC. Hibátlan. Eladó: Peti (12 értékelés)",
                                      "images": [f"https://kep.example/{n}.jpg" for n in (1, 2, 3)]}}
        self.hunter.image_get = lambda url: asked.append(url) or buffer.getvalue()
        self.ready()
        self.hunter.run(notify=self.notify)
        [check] = [m for m in self.llm.calls if "images" in m[1]]
        self.assertEqual((len(check[1]["images"]), asked), (2, ["https://kep.example/1.jpg", "https://kep.example/2.jpg"]))
        self.assertIn("A hirdetés fotói (2 db) mellékelve.", check[1]["content"])
        self.assertIn("ha a fotó letöltött (webshop- vagy katalógus-) képnek látszik", check[0]["content"])
        self.assertEqual(len(self.notices), 1)
        [deal] = self.hunter.state()["deals"]
        self.assertEqual(deal["photos_checked"], 2)
        self.assertNotIn("images", deal, "a fotók címe csak a modellkérdéshez kellett")
        # egy modell, ami nem tud képet: fotók nélkül kérdez, és ugyanúgy szól
        original = self.llm.chat_json

        def blind(messages, schema, **kwargs):
            if any("images" in m for m in messages):
                raise RuntimeError("ez a modell nem lát képet")
            return original(messages, schema, **kwargs)

        self.llm.chat_json = blind
        self.notices.clear()
        other = self.make("vak")
        other.image_get = lambda url: buffer.getvalue()
        self.ready(other)
        other.run(notify=self.notify)
        self.assertEqual(len(self.notices), 1)
        self.assertNotIn("photos_checked", other.state()["deals"][0])

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
        self.assertEqual(notice[1], "Ellenőrizetlen: Gigabyte RTX 3060 Ti Gaming OC", "az adatlap nélkül nem sürgős")
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
        # a Vinted díjával (280 + 5%) együtt legfeljebb 70 000: 66 400 + 3 600; a haszon 90 000 − 66 400 − 1 500 − 3 600
        self.assertIn("Ha 66 400 Ft-ért megkapod (ajánlattétel), kb. 18 500 Ft a haszon. Ajánlatot tegyek? Csak a "
                      "jóváhagyásod után küldöm el.", text)
        self.assertIn("Levonva: vevővédelmi díj kb. 3 600 Ft.", text)
        [deal] = self.hunter.state()["deals"]
        self.assertEqual((deal["offer"], deal["profit"], deal["costs"]), (66400, 18500, 1500 + 3600))
        self.assertEqual(extra["actions"][0], {"action": "offer", "deal": deal["id"], "label": "Ajánlatot teszek (66 400 Ft)"})
        other = self.make("jofogas")
        other.add_search("RTX 3060 Ti", ["jofogas"], reference=100000)
        other.set_enabled(True)
        self.notices.clear()
        other.run(notify=self.notify)
        self.assertEqual(self.notices, [], "ajánlatot csak a Vinteden lehet tenni")

    def test_the_vinted_fee_matches_the_live_cards(self):
        """A Vinted díja (az ár 5%-a + 280 Ft): egy élő vinted.hu-s találati oldal öt kártyáján a díjjal növelt ár
        pontosan ennyi volt (2026. október). Máshol nincs vevői díj."""
        for price, total in ((105000, 110530), (111004, 116834), (122313, 128709), (143401, 150851), (2531, 2938)):
            self.assertEqual(price + buy_fee("vinted", price), total, price)
        self.assertEqual(buy_fee("hardverapro", 50000), 0)

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
        self.assertEqual(offer_price(plain, 80000, 100000, "vinted"), 66400, "a Vinteden a díjjal együtt")
        self.assertEqual(highest_ok(plain, 100000, "vinted"), 66400)
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
        self.browser.by_url = [("vatera.hu", AUCTIONS), ("jofogas.hu/magyarorszag?q=Switch", SWITCH),
                               ("vinted.hu/catalog?search_text=Switch", SWITCH_VINTED), ("PS5", CONTROLLERS)]
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
        self.assertIn("Xbox", explored[0][1]["content"], "a 3 nap múlva záruló is: ha megéri, a licitlistára kerül")
        self.assertNotIn("PS5", explored[0][1]["content"], "ami illik egy keresésedre, ahhoz nem kell modell")
        self.notices.clear()
        self.hunter.run(notify=self.notify, force=True)
        self.assertEqual(self.notices, [], "egy licitről egyszer szól")

    def test_a_far_auction_goes_to_the_auction_list_and_comes_back_before_closing(self):
        """Egy friss, 1 Ft-ról induló, még napokig futó licit, ami megéri: nem vész el (eddig a kör kihagyta), hanem a
        licitlistára kerül egy csendes értesítéssel, a javasolt legmagasabb ajánlattal (a Vaterán ez most beállítható).
        Ha egy oldalon újra látja, a mostani ára frissül; ha az már a javasolt összeg fölött van, lekerül. A zárás előtt
        (a figyelő órái szerint) a kör megnézi a licit oldalát, és szól (Licit: …); utána lekerül a listáról."""
        far = {"text": "PS5 DualSense kontroller fehér · Hátralévő idő: 2 nap · 0 licit", "price": 1,
               "url": "https://www.vatera.hu/termek/3001", "auction": True}
        gone = {"text": "PS5 DualSense kontroller fekete · Hátralévő idő: 3 nap · 0 licit", "price": 1,
                "url": "https://www.vatera.hu/termek/3002", "auction": True}
        self.browser.by_url = [("vatera.hu", [far, gone])]
        self.hunter.add_search("PS5 kontroller", ["jofogas"], reference=14000)
        self.hunter.add_watch("https://www.vatera.hu/listings/index.php?c=123&ob=ending")
        self.hunter.set_enabled(True)
        self.hunter.run(notify=self.notify)
        self.assertEqual(len(self.notices), 2, "licitenként egy csendes értesítés")
        _kind, title, text, extra = self.notices[0]
        self.assertTrue(title.startswith("Licit: PS5 DualSense kontroller fehér") and title.endswith("(legfeljebb 9 800 Ft)"),
                        title)
        self.assertTrue(extra["quiet"], "csendesen: még napokig fut")
        self.assertIn("legfeljebb 9 800 Ft-ig érdemes licitálni", text)
        self.assertIn("legmagasabb ajánlatként beállíthatod", text)
        auctions = {a["url"]: a for a in self.hunter.state()["auctions"]}
        self.assertEqual(set(auctions), {far["url"], gone["url"]})
        self.assertEqual((auctions[far["url"]]["max_bid"], auctions[far["url"]]["ends"]), (9800, self.clock.now + 2 * 86400))
        self.assertEqual(self.hunter.state()["deals"], [], "még nem találat")
        self.assertFalse(self.hunter._auctions_due(self.clock.now))
        # egy órával később újra látja: a fehér most 5 000 Ft (marad), a fekete 12 000 Ft (a javasolt fölött: lekerül)
        self.browser.by_url = [("vatera.hu", [{**far, "price": 5000}, {**gone, "price": 12000}])]
        self.clock.now += 3600
        self.hunter.run(notify=self.notify, force=True)
        auctions = {a["url"]: a for a in self.hunter.state()["auctions"]}
        self.assertEqual(list(auctions), [far["url"]])
        self.assertEqual(auctions[far["url"]]["price"], 5000)
        self.assertEqual(len(self.notices), 2, "egy licitről egyszer szól csendesen")
        # a zárás előtt (6 órán belül): a licit oldala, és szól
        self.browser.by_url = [("vatera.hu", [])]
        self.clock.now = auctions[far["url"]]["ends"] - 5 * 3600
        self.assertTrue(self.hunter._auctions_due(self.clock.now))
        self.hunter.run(notify=self.notify)
        self.assertIn(far["url"], self.browser.looked)
        [(title, text, extra)] = [(t, x, e) for _k, t, x, e in self.notices[2:]]
        self.assertTrue(title.startswith("Licit: PS5 DualSense kontroller fehér"))
        self.assertIn("5 óra múlva zárul, legutóbb 5 000 Ft", text)
        self.assertIn("Legfeljebb 9 800 Ft-ig érdemes licitálni", text)
        self.assertTrue(extra["urgent"])
        self.assertEqual(self.hunter.state()["auctions"], [], "a zárás előtti szólás után lekerül")
        self.assertEqual([d["url"] for d in self.hunter.state()["deals"]], [far["url"]])

    def test_an_auction_is_only_recommended_after_its_page_is_checked(self):
        """A még napokig futó licit gyorsan a listára kerül, de ellenőrizetlenként: licithatárt és értesítést csak az
        adatlapja után ad. Ha az adatlap szerint hibás („hibás, nem működik, alkatrésznek”), lekerül, és nem szól; ha
        a kör keretébe már nem fér az adatlap, ellenőrizetlen marad, és egy későbbi kör nézi meg (akkor szól)."""
        broken = {"text": "PS5 DualSense kontroller · Hátralévő idő: 2 nap", "price": 1,
                  "url": "https://www.vatera.hu/termek/3301", "auction": True}
        good = {"text": "PS5 DualSense kontroller fehér · Hátralévő idő: 2 nap", "price": 1,
                "url": "https://www.vatera.hu/termek/3302", "auction": True}
        self.browser.by_url = [("vatera.hu", [broken, good])]
        self.browser.pages[broken["url"]] = {"title": "PS5 DualSense kontroller",
                                             "text": "PS5 DualSense kontroller, hibás, nem működik, alkatrésznek."}
        self.hunter.add_search("PS5 kontroller", ["jofogas"], reference=14000)
        self.hunter.add_watch("https://www.vatera.hu/listings/index.php?c=123&ob=ending")
        self.hunter.set_enabled(True)
        with mock.patch.object(deals_module, "MAX_PAGES", 1):   # csak az első adatlapja fér bele
            self.hunter.run(notify=self.notify)
        self.assertEqual(self.notices, [], "ellenőrzés nélkül nem javasol licithatárt")
        [listed] = self.hunter.state()["auctions"]
        self.assertEqual((listed["url"], listed["checked"]), (good["url"], False))
        journal = [e["text"] for e in self.hunter.journal.entries()]
        self.assertTrue(any("Lekerült a licitlistáról: „PS5 DualSense kontroller" in t and "hibás" in t for t in journal))
        self.assertTrue(any(t.startswith("Licitlistára tettem, még ellenőrizetlenül") for t in journal))
        self.assertNotIn(good["url"], self.browser.looked)
        self.clock.now += deals_module.BUSY_RETRY
        self.assertTrue(self.hunter.available(), "az ellenőrizetlen adatlapja esedékes")
        self.hunter.run(notify=self.notify)
        [(title, text, extra)] = [(t, x, e) for _k, t, x, e in self.notices]
        self.assertTrue(title.endswith("(legfeljebb 9 800 Ft)") and extra["quiet"])
        self.assertIn("Az adatlapját megnéztem", text)
        self.assertTrue(self.hunter.state()["auctions"][0]["checked"])

    def test_the_last_check_before_closing_uses_the_current_price_and_settings(self):
        """A zárás előtti ellenőrzés az adatlapról olvassa a mostani árat, és a licithatárt a mostani beállításokkal
        számolja újra: a már 12 000 Ft-nál tartó licitről (9 800 Ft-os határ) nem szól; ha közben a vételi limitet
        3 000 Ft-ra csökkentetted, legfeljebb annyiig javasol, és ami már afölött van, arról nem szól."""
        rows = [{"text": f"PS5 DualSense kontroller {name} · Hátralévő idő: 2 nap", "price": 1,
                 "url": f"https://www.vatera.hu/termek/34{n}", "auction": True}
                for n, name in enumerate(("fehér", "fekete", "kék"))]
        self.browser.by_url = [("vatera.hu", rows)]
        self.hunter.add_search("PS5 kontroller", ["jofogas"], reference=14000)
        self.hunter.add_watch("https://www.vatera.hu/listings/index.php?c=123&ob=ending")
        self.hunter.set_enabled(True)
        self.hunter.run(notify=self.notify)
        self.assertEqual(len(self.notices), 3)
        self.assertEqual({a["max_bid"] for a in self.hunter.state()["auctions"]}, {9800})
        for row, current in zip(rows, ("12 000", "2 000", "4 000")):
            self.browser.pages[row["url"]] = {"title": row["text"].split(" ·")[0],
                                              "text": f"PS5 kontroller, hibátlan. Aktuális ár: {current} Ft. 3 licit."}
        self.browser.by_url = [("vatera.hu", [])]
        self.clock.now += 2 * 86400 - 5 * 3600
        self.hunter.set_limits(max_buy=3000)
        self.hunter.run(notify=self.notify)
        urgent = [(t, x) for _k, t, x, e in self.notices[3:] if e.get("urgent")]
        self.assertEqual([t.split(" ·")[0] for t, _x in urgent], ["Licit: PS5 DualSense kontroller fekete"])
        text = urgent[0][1]
        self.assertIn("most 2 000 Ft", text)
        self.assertIn("Legfeljebb 3 000 Ft-ig érdemes licitálni", text, "a mostani vételi limittel")
        self.assertEqual(self.hunter.state()["auctions"], [], "mindhárom lekerült")
        journal = " ".join(e["text"] for e in self.hunter.journal.entries())
        self.assertIn("„PS5 DualSense kontroller fehér · Hátralévő idő: 2 nap” most 12 000 Ft", journal)
        self.assertIn("most 4 000 Ft, a mostani beállításokkal legfeljebb 3 000 Ft-ig érné meg", journal)

    def test_an_auction_can_be_taken_off_the_list(self):
        far = {"text": "PS5 DualSense kontroller · Hátralévő idő: 2 nap", "price": 1,
               "url": "https://www.vatera.hu/termek/3101", "auction": True}
        self.browser.by_url = [("vatera.hu", [far])]
        self.hunter.add_search("PS5 kontroller", ["jofogas"], reference=14000)
        self.hunter.add_watch("https://www.vatera.hu/listings/index.php?c=123&ob=ending")
        self.hunter.set_enabled(True)
        self.hunter.run(notify=self.notify)
        [auction] = self.hunter.state()["auctions"]
        self.assertEqual(self.hunter.dismiss_auction(auction["id"])["auctions"], [])
        self.clock.now += 2 * 86400 - 3600
        self.hunter.run(notify=self.notify)
        self.assertEqual(len(self.notices), 1, "a levett licitről a zárás előtt sem szól")

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
        """Egy figyelt oldal: amit az árbázis ismer, ahhoz méri; amit nem, a modell nevezi meg, és a többi oldalon nézi
        meg az árát. Mindkettőnél csak akkor szól, ha az ár biztos: legalább két oldalról (az árbázisban csak a
        Hardverapró árai voltak, ezért a Jófogáson is megnézte)."""
        self.browser.results = []
        self.browser.by_url = [("videokartya", PAGE), ("jofogas.hu/magyarorszag?q=Sony", HEADPHONES),
                               ("vinted.hu/catalog?search_text=Sony", HEADPHONES_VINTED),
                               ("jofogas.hu/magyarorszag?q=", RTX_JOFOGAS)]
        [watch] = self.hunter.add_watch(CATEGORY)["watches"]
        self.assertEqual((watch["name"], watch["platform"], watch["min_profit"]), ("Hardverapró: videokartya",
                                                                                   "hardverapro", 5000))
        self.llm.explore = [{"n": 1, "query": "Sony WH-1000XM4", "buy": True}]
        self.hunter.set_enabled(True)
        self.assertEqual(self.hunter.run(notify=self.notify), "1 keresés: 2 olcsó hirdetés.")
        notice = {title: text for _kind, title, text, _extra in self.notices}
        rtx = notice["Olcsó: Gigabyte RTX 3060 Ti Gaming OC"]
        # az oldal szokásos árú RTX-e is az árbázisba került: 8 szorosan együtt lévő ár egy oldalról már biztos
        self.assertIn("Máshol kb. 99 000 Ft-ért adják (8 hasonló hirdetés alapján, Hardverapró), a "
                      "várható haszon kb. 17 600 Ft.", rtx)
        # a becslés: a gyors eladás sávja, a javasolt legmagasabb vételár (minden költséggel), a megbízhatóság, és hogy az
        # eladás költségei nincsenek megadva (nem csendben nulla)
        self.assertIn("legfeljebb 82 600 Ft-ért érdemes megvenni (minden költséggel); a becslés megbízhatósága magas; "
                      "nincs megadva: csomagolás, eladási díj, egyéb eladási költség, tartalék (0 Ft-tal számoltam).", rtx)
        self.assertIn("Változat: videókártya: típus rtx 3060 ti (a címből: „RTX 3060 Ti”)", rtx)
        self.assertIn("Figyelt oldal: Hardverapró: videokartya.", rtx)
        sony = notice["Olcsó: Eladó zajszűrős Sony fejhallgató, alig használt"]
        self.assertIn("Máshol kb. 70 000 Ft-ért adják (10 hasonló hirdetés alapján, Jófogás és Vinted), a várható haszon "
                      "kb. 31 500 Ft.", sony)
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

    def test_a_cheaper_listing_found_while_checking_the_price_becomes_a_candidate(self):
        """A kör árkutatása (a figyelt oldal hirdetésének ára máshol) közben talált olcsó hirdetés is jelölt: a többihez
        mérve, saját maga nélkül megéri, ezért a hirdetés oldala és a modellkérdés után szól róla is, az eredetivel együtt
        (a nagyobb haszon előbb); a jelölt mellett ott van, mihez mérte (az eredeti is, mert a címe szerint ugyanaz)."""
        cheap = listing(89, "Sony WH-1000XM4 fekete", 45000, "www.jofogas.hu")
        self.browser.results = []
        self.browser.by_url = [("videokartya", PAGE), ("jofogas.hu/magyarorszag?q=Sony", HEADPHONES + [cheap]),
                               ("vinted.hu/catalog?search_text=Sony", HEADPHONES_VINTED),
                               ("jofogas.hu/magyarorszag?q=", RTX_JOFOGAS)]
        self.hunter.add_watch(CATEGORY)
        self.llm.explore = [{"n": 1, "query": "Sony WH-1000XM4", "buy": True}]
        self.hunter.set_enabled(True)
        self.hunter.run(notify=self.notify)
        titles = [t for _k, t, _x, _e in self.notices]
        self.assertIn("Olcsó: Sony WH-1000XM4 fekete", titles, "az árkutatás közben talált is jelölt lett")
        self.assertLess(titles.index("Olcsó: Eladó zajszűrős Sony fejhallgató, alig használt"),
                        titles.index("Olcsó: Sony WH-1000XM4 fekete"), "a nagyobb haszon előbb")
        deal = next(d for d in self.hunter.state()["deals"] if d["url"] == cheap["url"])
        self.assertTrue(deal["spawned"])
        self.assertIn("árkutatás közben", deal["explore"])
        self.assertIn("Máshol kb.", next(x for _k, t, x, _e in self.notices if t == "Olcsó: Sony WH-1000XM4 fekete"))
        used = [e["u"] for e in deal["basis"]["used"]]
        self.assertNotIn(cheap["url"], used, "saját maga nélkül")
        self.assertEqual(len(used), 10, "a Jófogás és a Vinted 5-5 szokásos ára (az eredeti címe nem kulcsos: nincs köztük)")
        looked = list(self.browser.looked)
        self.assertEqual(looked.count(cheap["url"]), 1)
        self.assertEqual(self.hunter._peers({"url": "https://hardverapro.hu/apro/1.html", "text": "x", "price": 1},
                                            {"group": [], "peers": [{"u": cheap["url"], "p": 45000, "s": "jofogas",
                                                                     "row": cheap}]},
                                            lambda peer, ref: (1.0, peer)), [], "egy hirdetést egyszer vesz jelöltnek")

    def test_a_found_listing_that_does_not_fit_the_round_comes_next(self):
        """Ha az árkutatás közben talált jelölt a kör keretébe már nem fér bele, nem vész el: a következő kör ezzel kezd
        (egyszer nézi meg). Ami túl sokáig várt (CARRY_TTL), lekerül, és egy későbbi árkutatásnál újra jelölt lehet."""
        cheap = listing(89, "Sony WH-1000XM4 fekete", 45000, "www.jofogas.hu")
        self.browser.results = []
        self.browser.by_url = [("videokartya", PAGE), ("jofogas.hu/magyarorszag?q=Sony", HEADPHONES + [cheap]),
                               ("vinted.hu/catalog?search_text=Sony", HEADPHONES_VINTED),
                               ("jofogas.hu/magyarorszag?q=", RTX_JOFOGAS)]
        self.hunter.add_watch(CATEGORY)
        self.llm.explore = [{"n": 1, "query": "Sony WH-1000XM4", "buy": True}]
        self.hunter.set_enabled(True)
        # az összehasonlítók leírását már elolvasta (allapotok.json): nem kell rájuk oldal a kör keretéből
        self.vetted(self.hunter, HEADPHONES + HEADPHONES_VINTED + NORMAL + RTX_JOFOGAS + PAGE + [cheap])
        with mock.patch.object(deals_module, "MAX_PAGES", 2):   # az eredeti még belefér, a talált már nem
            self.hunter.run(notify=self.notify)
        titles = lambda: [t for _k, t, _x, _e in self.notices]  # noqa: E731
        self.assertIn("Olcsó: Eladó zajszűrős Sony fejhallgató, alig használt", titles())
        self.assertNotIn("Olcsó: Sony WH-1000XM4 fekete", titles())
        self.assertEqual(list(self.hunter._carry), [listing_key(cheap["url"])])
        self.hunter.run(notify=self.notify, force=True)
        self.assertIn("Olcsó: Sony WH-1000XM4 fekete", titles(), "a következő kör ezzel kezdett")
        self.assertEqual(self.browser.looked.count(cheap["url"]), 1)
        self.assertEqual(self.hunter._carry, {})
        # ami túl sokáig várt, lekerül (és újra jelölt lehet)
        late = {**listing(90, "Sony WH-1000XM4 ezüst", 46000, "www.jofogas.hu"), "id": "x1", "site": "Jófogás",
                "platform": "jofogas", "median": 70000, "profit": 15000, "spawned": True}
        self.hunter._spawned[listing_key(late["url"])] = self.clock.now
        self.hunter._carry_over({"id": "w", "query": "Sony WH-1000XM4"}, late)
        self.clock.now += deals_module.CARRY_TTL + 60
        self.hunter.run(notify=self.notify, force=True)
        self.assertEqual((self.hunter._carry, listing_key(late["url"]) in self.hunter._spawned), ({}, False))
        self.assertNotIn(late["url"], self.browser.looked)

    def test_the_fresh_state_of_a_listing_overrides_its_stored_price(self):
        """Ha egy korábban eltárolt hirdetés friss címében megjelenik, hogy hibás, a régi ára sem számít tovább: nem lesz
        egyszerre beszámítva és kizárva, és a többiből (ha kevés) nem lesz biztos ár. Az árbázisból is kikerül."""
        stored = [{**listing(300 + n, f"RTX 3070 videókártya {n}", 140000 + n * 1000), "platform": "hardverapro"}
                  for n in range(6)]
        others = [{**listing(320 + n, f"RTX 3070 Gaming {n}", 150000 + n * 1000, "www.jofogas.hu"),
                   "platform": "jofogas"} for n in range(2)]
        self.hunter.book.add(stored + others)
        fresh = [{**row, "title": f"RTX 3070 videókártya {n} hibás", "text": f"RTX 3070 videókártya {n} hibás"}
                 for n, row in enumerate(stored)]
        row = {**listing(399, "RTX 3070", 80000), "platform": "hardverapro"}
        found = self.hunter._market_of(row, "RTX 3070", fresh)
        used = {e["u"] for e in found["basis"]["used"]}
        out = {e["u"]: e["why"] for e in found["basis"]["out"]}
        self.assertFalse(used & set(out), "egy hirdetés nem lehet egyszerre beszámítva és kizárva")
        self.assertEqual({r["url"] for r in stored} - set(out), set(), "a hibássá vált hirdetések kizárva")
        self.assertEqual(set(out.values()), {"alkatrész vagy hibás"})
        self.assertEqual((used, found["n"], found["sure"]), ({r["url"] for r in others}, 2, False),
                         "a megmaradt kettőből nem biztos az ár")
        self.hunter.book.add(fresh)            # az árbázisban is: a friss állapot felülírja a régit
        reference = self.hunter.book.reference(self.hunter.book.key("RTX 3070")[0])
        self.assertTrue(reference is None or reference["n"] == 2)
        moved = {**others[0], "title": "RTX 3080 Gaming 0"}   # a címe szerint most másik termék: a régi helyéről kikerül
        self.hunter.book.add([moved])
        self.assertEqual([s["url"] for s in self.hunter.book.samples(self.hunter.book.key("RTX 3070")[0])],
                         [others[1]["url"]])
        self.assertEqual([s["url"] for s in self.hunter.book.samples(self.hunter.book.key("RTX 3080")[0])],
                         [others[0]["url"]])

    def test_a_lower_market_level_wins_over_older_prices(self):
        """Ha a most talált hirdetések jóval olcsóbbak a korábban látottaknál, lejjebb ment az árszint: azokhoz mér (és
        ezt meg is mondja), a régebbi árak a kizártak között („régebbi ár”). Így egy sok hasonlóan olcsó hirdetés között
        az egyik nem látszik kivételes ajánlatnak."""
        self.hunter.book.add([{**listing(200 + n, f"Makita DHP482 {n}", 40000 + n * 500), "platform": "hardverapro"}
                              for n in range(5)])
        fresh = [{**listing(210 + n, f"Makita DHP482 fúró {n}", 30000 + n * 500, "www.jofogas.hu"), "platform": "jofogas"}
                 for n in range(4)]
        row = {**listing(220, "Makita DHP482", 25000, "www.jofogas.hu"), "platform": "jofogas"}
        found = self.hunter._market_of(row, "Makita DHP482", fresh)
        self.assertEqual((found["price"], found["n"]), (30800, 4))
        self.assertEqual(found["basis"]["note"], "a mostani hirdetések olcsóbbak a korábban látottaknál: azokhoz mértem")
        self.assertEqual({e["why"] for e in found["basis"]["out"]}, {"régebbi ár (azóta lejjebb ment az árszint)"})

    def test_clothing_is_left_out_of_trading(self):
        """Ruhával nem kereskedik: a figyelt oldal ruháját és cipőjét kihagyja (a modellhez sem kerül, a mai számokban:
        „ruha, cipő”), amit a modell ruhának nevez meg, az is kimarad, és ruhára nem alkuszik; a kifejezett keresésed
        (pl. egy cipőre) ettől még megy."""
        for text in ("Nike Air Max 90 42-es", "Carhartt dzseki L", "Babaruha csomag", "Levi's 501 farmer", "Női csizma",
                     "H&M felső"):
            self.assertTrue(wearable(text), text)
        for text in ("RTX 3060 Ti", "Laptop táska", "Bosch ruhaszárító", "VW Polo", "Boss DS-1 pedál", "AirPods Pro 2",
                     "HMD Nokia telefon"):
            self.assertFalse(wearable(text), text)
        clothes = [listing(60, "Nike Air Max 90 42-es", 15000), listing(61, "Carhartt dzseki L", 9000),
                   listing(62, "Retró kütyü", 9000)]
        self.browser.results = []
        self.browser.by_url = [("videokartya", PAGE + clothes)]
        self.hunter.add_watch(CATEGORY)
        self.hunter.set_enabled(True)
        self.llm.explore = [{"n": 3, "query": "Zara blézer", "buy": True}]
        self.hunter.run(notify=self.notify)
        [(system, explored)] = [(m[0]["content"], m[1]["content"]) for m in self.llm.calls
                                if "Friss hirdetések" in m[1]["content"]]
        self.assertNotIn("Nike", explored)
        self.assertNotIn("Carhartt", explored)
        self.assertIn("A ruhát, a cipőt és a divatárut hagyd ki", system)
        self.assertEqual(self.hunter.funnel()["counts"]["clothing"], 3, "a két ruha, és amit a modell blézernek nevezett")
        self.assertFalse([u for u in self.browser.urls if "Zara" in u], "a ruhának nevezett árát sem keresi máshol")
        shoes = self.make("cipo")
        shoes.add_search("Nike Air Max 90", ["hardverapro"], reference=40000)
        shoes.set_enabled(True)
        self.browser.by_url = [("Nike", [listing(70, "Nike Air Max 90 42-es", 27000)])]
        shoes.run(notify=self.notify)
        self.assertIn("Olcsó: Nike Air Max 90 42-es", [t for _k, t, _x, _e in self.notices], "a kifejezett keresés megy")
        # alku: ugyanolyan régóta fent lévő, kicsit drága hirdetések; a fúrógépre szól, a cipőre nem
        for name, base in (("Nike Air Max 90", 80), ("Makita DHP482", 90)):
            rows = [{**listing(base + n, f"{name} {n}", p), "platform": "hardverapro"}
                    for n, p in enumerate((40000, 41000, 39000, 42000, 40500, 39500, 31000))]
            shoes.book.add(rows)
        self.clock.now += 8 * 86400
        for name, base in (("Nike Air Max 90", 80), ("Makita DHP482", 90)):
            shoes.book.add([{**listing(base + n, f"{name} {n}", p), "platform": "hardverapro"}
                            for n, p in enumerate((40000, 41000, 39000, 42000, 40500, 39500, 31000))])
        found = {c["key"] for c in shoes.haggle_candidates()}
        self.assertTrue([k for k in found if "makita" in k], "a fúrógépre alkudna (a teszt helyes)")
        self.assertFalse([k for k in found if "nike" in k], "ruhára nem alkuszik")

    def test_promoted_listings_come_after_the_rest(self):
        """A fizetett kiemelések (az oldal elején, többnyire régiek) a modellkeret végére kerülnek: egy oldalról
        EXPLORE_ROWS (20) ismeretlen jut a modellhez, ebből előbb a többi."""
        promoted = [{"text": f"Kiemelt bolti tárgy {n}", "price": 5000, "promoted": True,
                     "url": f"https://hardverapro.hu/apro/kiemelt_{n}/friss.html"} for n in range(5)]
        regular = [{"text": f"Ismeretlen tárgy {n}", "price": 5000,
                    "url": f"https://hardverapro.hu/apro/ismeretlen_{n}/friss.html"} for n in range(20)]
        self.browser.results = []
        self.browser.by_url = [("videokartya", promoted + regular)]
        self.hunter.add_watch(CATEGORY)
        self.hunter.set_enabled(True)
        self.hunter.run(notify=self.notify)
        [explored] = [m[1]["content"] for m in self.llm.calls if "Friss hirdetések" in m[1]["content"]]
        self.assertIn("Ismeretlen tárgy 19", explored)
        self.assertNotIn("Kiemelt bolti tárgy", explored, "a kiemeltekre most nem jutott hely")

    def test_a_price_seen_on_one_site_only_is_checked_elsewhere_first(self):
        """Az árbázis csak a Hardverapró 5 árát ismeri (nem elég biztos): mielőtt szólna, a többi oldalon is megnézi.
        Ha ott nincs hasonló, nem szól (a naplóba beírja, miért); ha van, és ott is jóval drágább, szól."""
        self.browser.results = []
        self.browser.by_url = [("videokartya", PAGE)]
        self.hunter = self.make("kevés")
        self.hunter.book.add([{**r, "platform": "hardverapro"} for r in NORMAL[:4]])
        self.hunter.add_watch(CATEGORY)
        self.hunter.set_enabled(True)
        self.hunter.run(notify=self.notify)
        [(kind, title, text, extra)] = self.notices
        self.assertEqual((kind, title), ("deal", "Érdemes megnézni: Gigabyte RTX 3060 Ti Gaming OC"),
                         "nem megérő találat, csak egy csendes tipp")
        self.assertIn("Bizonytalan ár: csak 5 hasonló hirdetés (Hardverapró) szerint kb. 98 000 Ft-ért adják", text)
        self.assertNotIn("urgent", extra)
        [tip] = self.hunter.state()["tips"]
        self.assertEqual((tip["price"], tip["n"], tip["key"]), (70000, 5, "3060 rtx ti"))
        self.assertEqual((extra["tip"], extra.get("quiet")), (tip["id"], None),
                         "a telefonon a tipp saját, nagyon rövid pittyenésével szól (notice.tip)")
        self.notices.clear()
        self.assertTrue(any("jofogas.hu/magyarorszag?q=RTX%203060%20Ti" in u for u in self.browser.urls),
                        "a Jófogáson is megnézte, a cím szavaival (ahogy a hirdetésben írták)")
        self.assertFalse(any("hardverapro.hu" in u and "RTX" in u for u in self.browser.urls), "ahol már ismeri, ott nem")
        [line] = [e["text"] for e in self.hunter.journal.entries() if e["text"].startswith("Nem biztos")]
        self.assertIn("„Gigabyte RTX 3060 Ti Gaming OC” 70 000 Ft", line)
        self.assertIn("csak 5 hasonló árat találtam (Hardverapró)", line)
        other = self.make("masik")
        other.book.add([{**r, "platform": "hardverapro"} for r in NORMAL[:4]])
        self.browser.by_url = [("videokartya", PAGE), ("jofogas.hu/magyarorszag?q=", RTX_JOFOGAS)]
        other.add_watch(CATEGORY)
        other.set_enabled(True)
        other.run(notify=self.notify)
        self.assertEqual([t for _k, t, _x, _e in self.notices], ["Olcsó: Gigabyte RTX 3060 Ti Gaming OC"])
        self.assertIn("8 hasonló hirdetés alapján, Hardverapró és Jófogás", self.notices[0][2])

    def test_a_tip_becomes_a_deal_when_enough_prices_come(self):
        """Egy bizonytalan árú tipp nem vész el: amikor az árbázisban összegyűlik elég hasonló ár, a következő kör
        letöltés nélkül újra felméri, és ha még mindig megéri, rendes megérő találat lesz belőle (a hirdetés oldala, a
        modellkérdés); a tipp lekerül a listáról. Az elrejtett tipp nem jön vissza."""
        self.browser.results = []
        self.browser.by_url = [("videokartya", PAGE)]
        self.hunter = self.make("tipp")
        self.hunter.book.add([{**r, "platform": "hardverapro"} for r in NORMAL[:4]])
        self.hunter.add_watch(CATEGORY)
        self.hunter.set_enabled(True)
        self.hunter.run(notify=self.notify)
        self.assertEqual(len(self.hunter.state()["tips"]), 1)
        self.notices.clear()
        self.hunter.book.add([{**r, "platform": "jofogas"} for r in RTX_JOFOGAS])
        self.clock.now += 3600
        self.hunter.run(notify=self.notify, force=True)
        self.assertEqual([t for _k, t, _x, _e in self.notices], ["Olcsó: Gigabyte RTX 3060 Ti Gaming OC"])
        self.assertEqual(self.hunter.state()["tips"], [])
        other = self.make("rejtett")
        other.book.add([{**r, "platform": "hardverapro"} for r in NORMAL[:4]])
        other.add_watch(CATEGORY)
        other.set_enabled(True)
        other.run(notify=self.notify)
        [tip] = other.state()["tips"]
        self.assertEqual(other.dismiss_tip(tip["id"])["tips"], [])

    def test_a_tip_waits_when_the_round_has_no_page_left(self):
        """Ha egy tipp ára közben biztos lett, de a körben már nem nyitható meg több hirdetés (elfogyott a keret), a
        tipp nem vész el: a listán marad, és a következő körben kerül sorra (eddig törlődött, és többé nem jött elő)."""
        self.browser.results = []
        self.browser.by_url = [("videokartya", PAGE)]
        self.hunter = self.make("keret")
        self.hunter.book.add([{**r, "platform": "hardverapro"} for r in NORMAL[:4]])
        self.hunter.add_watch(CATEGORY)
        self.hunter.set_enabled(True)
        self.hunter.run(notify=self.notify)
        self.notices.clear()
        self.hunter.book.add([{**r, "platform": "jofogas"} for r in RTX_JOFOGAS])
        self.clock.now += 3600
        with mock.patch.object(deals_module, "MAX_PAGES", 0):
            self.hunter.run(notify=self.notify, force=True)
        self.assertEqual(self.notices, [])
        self.assertEqual(len(self.hunter.state()["tips"]), 1, "megmaradt, a következő körben jön")
        self.clock.now += 3600
        self.hunter.run(notify=self.notify, force=True)
        self.assertEqual([t for _k, t, _x, _e in self.notices], ["Olcsó: Gigabyte RTX 3060 Ti Gaming OC"])
        self.assertEqual(self.hunter.state()["tips"], [], "az ellenőrzés lezárult: lekerült")

    def test_two_lanes_never_make_two_deals_of_one_tip(self):
        """A gyors sor és a böngésző sora egyszerre nézi a tippeket: ugyanazt csak az egyik foglalhatja le (a kiválasztás
        és a lefoglalás egy zárolt lépés; eddig mindkettő szabadnak láthatta, és két találat, két értesítés lett
        belőle). Mentéskor is: egy hirdetésből (webcím) egy találat, és csak egyszer szól róla."""
        self.browser.results = []
        self.browser.by_url = [("videokartya", PAGE)]
        hunter = self.hunter = self.make("ket-sor")
        hunter.book.add([{**r, "platform": "hardverapro"} for r in NORMAL[:4]])
        hunter.add_watch(CATEGORY)
        hunter.set_enabled(True)
        hunter.run(notify=self.notify)
        [tip] = hunter.state()["tips"]
        self.notices.clear()
        hunter.book.add([{**r, "platform": "jofogas"} for r in RTX_JOFOGAS])
        first = deals_module._Round(lambda: None, self.notify, None)
        second = deals_module._Round(lambda: None, self.notify, None)
        explore = hunter._explore_deal

        def racing(*args, **kwargs):
            hunter._explore_deal = explore     # a másik sor épp most, az első kiválasztása és foglalása között
            hunter._step_tips(second)
            return explore(*args, **kwargs)

        hunter._explore_deal = racing
        hunter._step_tips(first)
        self.assertEqual((len(first.tips), len(second.tips)), (0, 1), "csak az egyik sor foglalta le")
        self.assertEqual((len(first.work), len(second.work)), (0, 1), "és csak annak lett belőle jelölt")
        row = {"site": tip["site"], "platform": tip["platform"], "text": tip["title"], "title": tip["title"],
               "price": int(tip["price"]), "url": tip["url"]}
        search = {"id": "tipp", "query": tip["query"]}
        deal = self.page_checked(hunter._deal(search, row, 98000))
        hunter._conclude(first, search, dict(deal), True)
        hunter._conclude(second, search, {**deal, "id": "masik"}, True)
        self.assertEqual([t for _k, t, _x, _e in self.notices], ["Olcsó: Gigabyte RTX 3060 Ti Gaming OC"], "egyszer szól")
        self.assertEqual([d["id"] for d in hunter.state()["deals"] if d["url"] == tip["url"]], [deal["id"]],
                         "egy találat a hirdetésből")

    def test_profit_counts_with_a_careful_sale_price(self):
        """A haszon óvatos: a szokásos ár 90%-a, de legfeljebb a hasonlók alsó negyede (ha szórnak az árak, a vevő az
        olcsóbbakat találja meg előbb). Biztos ár: legalább 5 hasonló, két oldalról (vagy egy oldalról legalább 10)."""
        self.assertEqual(safe_sale(100000), 90000)
        self.assertEqual(safe_sale(100000, low=80000), 80000)
        self.assertEqual((expected_profit(100000, 60000), expected_profit(100000, 60000, low=80000)), (28500, 18500))
        self.assertEqual(explore_top(100000, 5000, low=80000), 73500)
        one_site = market([(p, "hardverapro") for p in (80000, 90000, 100000, 110000, 120000)])
        self.assertEqual((one_site["price"], one_site["p25"], one_site["n"], one_site["sure"]), (100000, 90000, 5, False))
        two_sites = market(list(zip((80000, 90000, 100000, 110000, 120000), ("hardverapro", "jofogas") * 3)))
        self.assertEqual((two_sites["sites"], two_sites["sure"]), (["hardverapro", "jofogas"], True))
        self.assertTrue(market([(100000 + n * 1000, "vinted") for n in range(10)])["sure"], "egy oldalról, de sok ár")
        tight = [(p, "hardverapro") for p in (60000, 62000, 61000, 64000, 65000, 63000)]
        self.assertTrue(market(tight)["sure"], "egy oldalról 6 ár, ha szorosan együtt vannak")
        self.assertFalse(market(tight[:5])["sure"], "5 egy oldalról kevés")
        self.assertFalse(market([(p, "hardverapro") for p in (40000, 50000, 60000, 70000, 80000, 90000)])["sure"],
                         "6 ár, de szórnak")
        self.assertFalse(market(list(zip((40000, 60000, 100000, 140000, 160000), ("vinted", "jofogas") * 3)))["sure"],
                         "nagyon szórnak az árak")
        self.assertIsNone(market([]))

    def test_how_safe_an_investment_is(self):
        """Nyugodt szívvel megvehető-e: a haszon a befektetett pénzhez képest, milyen gyorsan kel az ilyen, és mennyire
        szórnak az árak."""
        base = {"price": 50000, "profit": 15000, "similar": 12, "sites": ["Hardverapró", "Jófogás"], "verified": True}
        safe = assess(base)
        self.assertEqual((safe["level"], safe["good"]), ("biztos", ["kb. 30% becsült haszon", "12 hasonló, 2 oldalon"]))
        self.assertEqual(safety_text(safe), "Alacsony kockázat: kb. 30% becsült haszon, 12 hasonló, 2 oldalon.",
                         "nem ígéret: a haszon becsült")
        self.assertEqual(assess({**base, "profit": 7000})["level"], "jó", "14%: jó, de nem biztos")
        thin = assess({**base, "profit": 4000})
        self.assertEqual((thin["level"], thin["why"]), ("kockázatos", ["kicsi a haszon a befektetett pénzhez képest (8%)"]))
        self.assertEqual(safety_text(thin), "Kockázatos: kicsi a haszon a befektetett pénzhez képest (8%).")
        self.assertEqual(assess({**base, "pace": "lassú"})["level"], "kockázatos", "lassan kel, és csak 30% haszon")
        slow_but_rich = assess({**base, "profit": 20000, "pace": "lassú"})
        self.assertEqual((slow_but_rich["level"], slow_but_rich["why"]),
                         ("jó", ["lassan kel el (a hasonlók sokáig fent maradnak)"]))
        self.assertEqual(assess({**base, "spread": 0.6})["level"], "jó", "nagyon szórnak az árak")
        self.assertIn("gyorsan kel el", assess({**base, "pace": "gyors"})["good"])
        self.assertIn("4 eltűnt (talán elkelt) hasonló alapján is",
                      assess({**base, "sold": {"n": 4, "median": 60000}})["good"])
        self.assertEqual(assess({"price": 9000, "profit": 40000, "auction": True, "max_bid": 30000, "verified": True})["level"],
                         "biztos", "a licit mostani árán nem számol haszonrátát")
        self.assertEqual(assess({**base, "verified": False})["level"], "jó", "ellenőrizetlen: a magas haszon magában kevés")
        self.assertEqual(assess({**base, "offer": 45000, "profit": 4000})["level"], "kockázatos", "az ajánlat árán")

    def test_sold_prices_and_how_fast_it_sells(self):
        """Az újranézéskor eltűnt (elkelt) hirdetések ára: ha legalább 3 van, az óvatos eladási ár legfeljebb ennyi; és
        hogy milyen gyorsan kel az ilyen."""
        found = market([(p, s, gone) for p, s, gone in ((100000, "hardverapro", False), (105000, "jofogas", False),
                                                       (98000, "hardverapro", False), (110000, "jofogas", False),
                                                       (82000, "hardverapro", True), (85000, "jofogas", True),
                                                       (80000, "hardverapro", True))])
        self.assertEqual((found["sold"], found["p25"]), ({"n": 3, "median": 82000}, 83500))
        self.assertEqual(careful_low(found), 82000, "az elkeltek ára az alacsonyabb")
        self.assertEqual(careful_low({"p25": 70000, "sold": {"n": 3, "median": 82000}}), 70000)
        self.assertNotIn("sold", market([(100000, "a", True), (100000, "b", True), (100000, "a", False)] * 1))
        self.assertEqual(pace({"checked": 4, "sold": 3, "days": 4, "slow": 0}), "gyors")
        self.assertEqual(pace({"checked": 4, "sold": 0, "days": None, "slow": 3}), "lassú")
        self.assertEqual(pace({"checked": 4, "sold": 2, "days": 12, "slow": 1}), "")
        self.assertEqual(pace({"checked": 1, "sold": 1, "days": 2, "slow": 0}), "", "egyből még nem tudni")
        book = PriceBook(Path(self.tmp.name) / "elkelt", self.clock)
        book.add([{"text": f"RTX 3060 Ti videókártya {n}", "price": p, "url": f"https://x.hu/{n}", "platform": site}
                  for n, (p, site) in enumerate(zip((95000, 100000, 105000, 98000, 102000, 80000, 82000, 81000),
                                                    ("hardverapro", "jofogas") * 4))])
        for n in (5, 6, 7):
            book.revisited("3060 rtx ti", f"https://x.hu/{n}", gone=True)
        self.assertEqual(book.reference("3060 rtx ti")["sold"], {"n": 3, "median": 81000})

    def test_a_risky_find_is_skipped_unless_you_want_those_too(self):
        """Lassan kelő termék kis haszonnal: alapból nem szól róla (a naplóba írja, miért); ha a kockázatosakat is
        kéred, szól, de megmondja, mi a kockázat."""
        def ready(hunter):
            hunter.book.add([{**listing(40 + n, f"RTX 3060 Ti videókártya {n}", p, f"www.{site}.hu"), "platform": site}
                             for n, (p, site) in enumerate(zip((95000, 100000, 105000, 98000, 110000, 92000),
                                                               ("hardverapro", "jofogas") * 3))])
            self.clock.now += 15 * 86400                       # három két hét után is fent volt: lassan kel
            for n in (0, 1, 2):
                hunter.book.revisited("3060 rtx ti", f"https://www.{('hardverapro', 'jofogas')[n % 2]}.hu/apro/{100040 + n}.html",
                                      gone=False)
            hunter.add_watch(CATEGORY)
            hunter.set_enabled(True)

        self.browser.by_url = [("videokartya", [listing(20, "Gigabyte RTX 3060 Ti Gaming OC", 70000)])]
        ready(self.hunter)
        self.hunter.run(notify=self.notify)
        self.assertEqual(self.notices, [])
        [line] = [e["text"] for e in self.hunter.journal.entries() if e["text"].startswith("Kockázatos")]
        self.assertIn("„Gigabyte RTX 3060 Ti Gaming OC” 70 000 Ft", line)
        self.assertIn("lassan kel el", line)
        self.assertTrue(self.hunter.state()["safe_only"], "alapból csak a nyugodt szívvel megvehetőkről szól")
        other = self.make("mind")
        other.set_limits(safe_only=False)
        ready(other)
        other.run(notify=self.notify)
        [(_kind, title, text, _extra)] = self.notices
        self.assertEqual(title, "Olcsó: Gigabyte RTX 3060 Ti Gaming OC")
        self.assertIn("Kockázatos: lassan kel el (a hasonlók sokáig fent maradnak).", text)
        self.assertEqual(other.state()["deals"][0]["safety"]["level"], "kockázatos")
        self.assertFalse(other.state()["safe_only"])

    def test_known_products_need_no_model_and_the_named_ones_teach_the_book(self):
        self.browser.by_url = [("videokartya", PAGE)]
        for sub, profit, found in (("sok", 20000, []), ("eleg", 10000, ["Olcsó: Gigabyte RTX 3060 Ti Gaming OC"])):
            hunter = self.make(sub)            # a modell csak a végső ellenőrzéshez kell (az ismert terméket nem nevezi meg)
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
        self.assertEqual(deals[45000]["offer"], 36400, "a Vinted díjával együtt legfeljebb 38 500")
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
        self.assertIn("Nikon", explored, "reggel 8-kor zárul: ha megéri, a licitlistára kerül, és reggel szól")
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


class SellerTests(HunterCase):
    def test_a_bundle_from_the_same_seller(self):
        """Az eladó többi hirdetése (seller_bundle): ami külön is eladható haszonnal (biztos áron, nem lassan kelő), azt
        hozzáveszi; a Vinteden egy posta és a vevővédelmi díj (kb. fix + 5%) a végösszegre egyszer; darabonként a
        csomagolás és a meghirdetés; összeveti a csak ezt vennéd és a külön-külön vennéd esettel; a ruha, a csomag, a
        kiegészítő, a hibás kimarad, az ismeretlen árú csak számít."""
        self.hunter.book.add(AIRPODS)
        deal = {"id": "d1", "url": "https://www.vinted.hu/items/1-switch", "platform": "vinted", "site": "Vinted",
                "title": "Nintendo Switch OLED", "text": "Nintendo Switch OLED", "price": 60000, "median": 100000,
                "low": 90000, "resale": 90000, "profit": 28500, "at": self.clock(),
                "seller_url": "https://www.vinted.hu/member/5-anna"}
        self.hunter._remember(deal)
        rows = [{**listing(1, "Nintendo Switch OLED", 60000, "www.vinted.hu"), "url": deal["url"]},
                listing(2, "Apple AirPods Pro 2 fehér", 30000, "www.vinted.hu"),
                listing(3, "Apple AirPods Pro 2 doboz nélkül", 46000, "www.vinted.hu"),
                listing(4, "Nike pulóver", 5000, "www.vinted.hu"), listing(5, "3 db póló", 3000, "www.vinted.hu"),
                listing(6, "AirPods Pro 2 tok", 2000, "www.vinted.hu"), listing(7, "Apple AirPods Pro 2 hibás", 9000,
                                                                               "www.vinted.hu"),
                listing(8, "Retró kütyü X100", 3000, "www.vinted.hu")]
        summary = self.hunter.seller_bundle(deal, rows)
        fee = lambda amount: round(280 + 0.05 * amount)  # noqa: E731
        self.assertEqual([(i["t"], i["pick"], i.get("why", "")) for i in summary["items"]],
                         [("Apple AirPods Pro 2 fehér", True, ""),
                          ("Apple AirPods Pro 2 doboz nélkül", False, "külön nem éri meg")])
        self.assertEqual((summary["total"], summary["fee"], summary["unknown"]), (90000, fee(90000), 1))
        self.assertEqual(summary["single"], 90000 - 60000 - 1500 - fee(60000) - 300)
        self.assertEqual(summary["profit"], 90000 + 45000 - 90000 - 1500 - fee(90000) - 2 * 300)
        self.assertEqual(summary["separately"], summary["single"] + 45000 - 30000 - 1500 - fee(30000) - 300)
        self.assertGreater(summary["profit"], summary["separately"], "a közös vétel jobb, mint külön-külön")
        self.assertEqual((summary["worth"], summary["listings"], summary["minutes"], summary["offer"]),
                         (True, 2, 20, 81000))
        self.assertEqual(self.hunter.get("d1")["seller"]["profit"], summary["profit"], "a találatba is beírja")
        text = self.hunter.seller_text(deal, summary)
        self.assertIn("a Vinted vevővédelmi díja kb. 4 780 Ft a végösszegre egyszer", text)
        self.assertIn("Közös vételre ajánlj kb. 81 000 Ft-ot (csomagajánlat).", text)
        self.assertIsNone(self.hunter.seller_bundle(deal, rows[:1]), "nincs más hirdetése")

    def test_every_extra_item_passes_the_buy_checks(self):
        """A közös vétel minden darabjára ugyanazok a vételi ellenőrzések, mint egy találatra: az 1 Ft-os (nem valódi
        ár), a gyanúsan olcsó, a feltűnően olcsó, gyakran hamisított márka és a vételi limit fölötti darab nem kerül bele,
        és a közös végösszegnek is bele kell férnie a limitbe (a legjobbak jönnek, amíg belefér)."""
        self.hunter.book.add(AIRPODS + [{**r, "platform": "hardverapro"} for r in NORMAL])
        self.hunter.set_limits(max_buy=55000)
        deal = {"id": "d2", "url": "https://hardverapro.hu/apro/sony_1/friss.html", "platform": "hardverapro",
                "site": "Hardverapró", "title": "Sony WH-1000XM4", "text": "Sony WH-1000XM4", "price": 20000,
                "median": 70000, "low": 63000, "resale": 63000, "profit": 41200, "at": self.clock()}
        self.hunter._remember(deal)
        rows = [listing(21, "Gigabyte RTX 3060 Ti", 1), listing(22, "MSI RTX 3060 Ti", 35000),
                listing(23, "Apple AirPods Pro 2 fekete", 27000), listing(24, "ASUS RTX 3060 Ti", 60000),
                listing(25, "Apple AirPods Pro 2 fehér", 30000), listing(26, "Apple AirPods Pro 2 ezüst", 31000)]
        summary = self.hunter.seller_bundle(deal, rows)
        why = {i["t"]: (i["pick"], i.get("why", "")) for i in summary["items"]}
        self.assertEqual(why, {
            "Gigabyte RTX 3060 Ti": (False, "nem valódi ár (pl. 1 Ft vagy „ár megegyezés szerint”)"),
            "MSI RTX 3060 Ti": (False, "gyanúsan olcsó (átverés, hamisítvány vagy hibás lehet)"),
            "Apple AirPods Pro 2 fekete": (False, "gyakran hamisított márka, feltűnően olcsón"),
            "ASUS RTX 3060 Ti": (False, "a vételi limit fölött"),
            "Apple AirPods Pro 2 fehér": (True, ""),
            "Apple AirPods Pro 2 ezüst": (False, "együtt már nem fér a vételi limitbe")})
        self.assertEqual((summary["picked"], summary["total"]), (1, 50000), "a végösszeg is a limiten belül")

    def test_the_round_looks_at_the_sellers_other_listings(self):
        """A kör egy jó találata után (egy oldalbetöltés a kör keretéből) az eladó többi hirdetése is: együtt jobban
        megéri, ezért csendesen szól."""
        self.hunter.book.add(AIRPODS)
        self.hunter.add_search("RTX 3060 Ti", ["hardverapro"], reference=100000)
        self.hunter.set_enabled(True)
        row = listing(10, "Gigabyte RTX 3060 Ti Gaming OC", 55000)
        seller = "https://hardverapro.hu/aprok/hirdeto/peti/index.html"
        self.browser.results = [row]
        self.browser.pages[row["url"]] = {"title": "Gigabyte RTX 3060 Ti Gaming OC", "seller": seller,
                                          "text": "RTX 3060 Ti. Hibátlan, dobozában. Eladó: Peti (12 értékelés)"}
        self.browser.by_url = [("hirdeto/peti", [row, listing(11, "Apple AirPods Pro 2 fehér", 30000)])]
        self.hunter.run(notify=self.notify)
        self.assertEqual([t for _k, t, _x, _e in self.notices],
                         ["Olcsó: Gigabyte RTX 3060 Ti Gaming OC", "Ugyanettől az eladótól: Gigabyte RTX 3060 Ti Gaming OC"])
        self.assertTrue(self.notices[1][3]["quiet"])
        self.assertIn(seller, self.browser.urls)
        self.assertEqual(self.hunter.state()["deals"][0]["seller"]["picked"], 1)


class PriceBookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_really_comparable_prices_count(self):
        """Csak az számít összehasonlítónak (és kerül az árbázisba), ami egy működő, egy darab, meghatározott termék ára;
        a kizárásnak oka van (a találat ablakában látszik). A készlet változat: a 2×4 GB nem a 2×8 GB."""
        for text, why in (("RTX 3060 Ti 90 000 Ft-tól", "„-tól” ár: nem egy meghatározott darab ára"),
                          ("25e-től RTX kártyák", "„-tól” ár: nem egy meghatározott darab ára"),
                          ("RTX 3060 / 3070 / 3080 videókártyák", "több termék egy hirdetésben"),
                          ("Ryzen 5 5600X + B550 alaplap", "több termék egy hirdetésben"),
                          ("RTX 3070 és RTX 3080 eladó", "több termék egy hirdetésben"),
                          ("2 db RTX 3060", "csomag vagy több darab egy áron"),
                          ("RTX 3060 hibás", "alkatrész vagy hibás"), ("Keresek RTX 3060-at", "kereső- vagy cserehirdetés"),
                          ("iPhone 13 tok", "kiegészítő, nem maga a termék")):
            self.assertEqual(unusable(text), why, text)
        for text in ("RTX 3060 Ti", "2019-től használt Makita DHP482", "Hétfőtől átvehető RTX 3070",
                     "Kingston Fury Beast 16GB (2x8GB) DDR4 3200MHz", "RTX 3060 Ti 12GB GDDR6", "PS5 + 2 játék"):
            self.assertEqual(unusable(text), "", text)
        # egy termék több szava egynek számít (az i5 és a 10400F, a DDR4 és a CL16), és a felsorolás típusjel nélküli
        # része (dobozos, RGB, AM4, GDDR6, 256 bit) nem külön termék: ezek szabályos, egy darabos hirdetések
        for text in ("Intel Core i5-10400F, dobozos", "Corsair Vengeance LPX 16GB (2x8GB) DDR4 3200 CL16, RGB",
                     "16GB DDR4 3200MHz CL16 / 2x8GB", "G.Skill 32GB (2x16GB) DDR5 6000MHz CL30, fekete",
                     "Kingston Fury Beast 16GB DDR4 3200 MHz (2x8GB) fekete és piros", "Ryzen 5 5600X, AM4, dobozos",
                     "ASUS TUF RTX 4070 Ti Super 16GB GDDR6X, OC", "MSI RTX 3060 Ti Gaming X Trio 8G, GDDR6, 256 bit",
                     "RTX 4060 (2024)", "Asus ROG Strix B550-F Gaming, AM4", "Intel Core i5 10400F, i5-10400F processzor"):
            self.assertEqual(unusable(text), "", text)
        for text in ("Lego 42115, Lego 42116", "i5-12400F + B660M + 16GB DDR4", "GTX 1060 6GB / RX 580 8GB",
                     "i7 4790K, Z97, 16GB"):
            self.assertEqual(unusable(text), "több termék egy hirdetésben", text)
        self.assertNotEqual(product_key("Kingston Fury 2x4GB DDR4")[0], product_key("Kingston Fury 2x8GB DDR4")[0])
        self.assertEqual(product_key("Kingston Fury 16GB (2x8GB) DDR4")[0], product_key("Kingston Fury 2x8GB DDR4")[0])
        book = PriceBook(Path(self.tmp.name), self.clock)
        self.assertEqual(book.add([{"text": "RTX 3060 Ti 90 000 Ft-tól", "price": 90000, "url": "https://x.hu/1"},
                                   {"text": "RTX 3060 / 3070 / 3080", "price": 90000, "url": "https://x.hu/2"}]), 0)
        book.add([{"text": f"RTX 3060 Ti {n}", "price": 95000 + n * 1000, "url": f"https://x.hu/{10 + n}",
                   "platform": "hardverapro"} for n in range(6)] +
                 [{"text": "RTX 3060 Ti olcsón", "price": 30000, "url": "https://x.hu/30", "platform": "hardverapro"}])
        reference = book.reference(book.key("RTX 3060 Ti")[0])
        self.assertEqual(([e["p"] for e in reference["basis"]["used"]][:2], reference["basis"]["out"]),
                         ([95000, 96000], [{"t": "RTX 3060 Ti olcsón", "p": 30000, "s": "hardverapro",
                                            "u": "https://x.hu/30", "src": "árbázis", "seen": int(self.clock.now),
                                            "why": "kilógó ár (kimaradt a szokásos árból)", "outlier": "olcsó",
                                            "up": 0.0}]),
                         "mihez mérte, és mi maradt ki (a címével; a kilógóan olcsó: mióta van fent)")

    def test_the_title_gives_the_key_and_the_search_word(self):
        """A böngésző nélkül olvasott kártya szövegében a város, a dátum és az eladó neve is ott van („eladó31”): a
        termékkulcs a hirdetés címéből jön. A más oldalon való ellenőrzés keresőszava a cím kulcsszavai, ahogy írták."""
        with tempfile.TemporaryDirectory() as tmp:
            book = PriceBook(Path(tmp), Clock())
            card = {"text": "Sony WH-1000XM4 fejhallgató Budapest 30 000 Ft ma 14:01 eladó31 (12 értékelés)",
                    "title": "Sony WH-1000XM4 fejhallgató", "price": 30000, "url": "https://hardverapro.hu/apro/1.html",
                    "platform": "hardverapro"}
            self.assertEqual(book.add([card]), 1)
            self.assertEqual(book.samples("sony wh1000xm4")[0]["site"], "hardverapro")
            self.assertEqual(book.query("Sony WH-1000XM4 fejhallgató fekete"), "Sony WH-1000XM4")
            self.assertEqual(book.query("Gigabyte GeForce RTX 3060 Ti Gaming OC"), "RTX 3060 Ti")
            self.assertEqual(book.query("Régi fülhallgató"), "")

    def test_one_product_one_key(self):
        """Ugyanaz a termék, akárhogy írták, egy kulcs (különben a hasonló árai szétesnek, és sosem lesz biztos ára):
        a processzor kötőjellel, anélkül és a márkával; a videókártya a kártyagyártó típusnevével (G1, Gaming X); a
        memóriakészlet „2x8 GB”, „(2x8GB)” alakban. A laptop és a gép a kártyájával más termék."""
        groups = (
            ("Intel Core i5-10400F", "Intel Core i5 10400F processzor", "i5-10400F", "Eladó i5 10400f CPU",
             "Intel i5-10400F 6 mag"),
            ("Gigabyte GTX 1080 G1 Gaming", "MSI GTX 1080 Gaming X", "Nvidia GeForce GTX 1080", "GTX1080"),
            ("MSI GeForce GTX 1660 Ti Ventus XS 6GB", "Asus GTX 1660 Ti 6GB"),
            ("Corsair Vengeance 2x8 GB", "Corsair Vengeance 2x8GB", "Corsair Vengeance 16GB (2x8GB)"),
            ("AMD Ryzen 5 5600X", "Ryzen 5600X"),
        )
        for titles in groups:
            self.assertEqual(len({product_key(t)[0] for t in titles}), 1, titles)
        self.assertNotEqual(product_key("Asus TUF F15 laptop RTX 3060")[0], product_key("Asus RTX 3060")[0])
        self.assertNotEqual(product_key("Gamer PC RTX 3060 i5 12400F")[0], product_key("RTX 3060")[0])
        self.assertNotEqual(product_key("Lenovo Legion 5 RTX 3060")[0], product_key("RTX 3060")[0],
                            "a laptopcsalád neve is laptop")
        self.assertNotEqual(product_key("GTX 1660 Ti")[0], product_key("GTX 1660")[0])

    def test_an_older_price_book_is_rekeyed(self):
        """A régi (más kulcsokkal mentett) árbázis betöltéskor az új kulcsokra kerül: az egybeeső termékek árai
        összeadódnak (ugyanaz a hirdetés egyszer)."""
        import json
        root = Path(self.tmp.name)
        now = self.clock()
        keys = {"i510400f intel": {"name": "intel i510400f", "samples": [
                    {"p": 30000 + n * 500, "url": f"https://hardverapro.hu/apro/a{n}.html", "site": "hardverapro",
                     "at": now, "last": now} for n in range(3)]},
                "10400f i5": {"name": "i5 10400f", "samples": [
                    {"p": 31000 + n * 500, "url": f"https://hardverapro.hu/apro/b{n}.html", "site": "hardverapro",
                     "at": now, "last": now} for n in range(3)] + [
                    {"p": 30000, "url": "https://hardverapro.hu/apro/a0.html", "site": "hardverapro", "at": now,
                     "last": now - 10}]}}
        (root / "arbazis.json").write_text(json.dumps({"keys": keys}), encoding="utf-8")
        # egy régi árbázisban a „4G” még nem számított méretnek: a 4 GB-os RX 580 a kapacitás nélküliek közé került
        keys["580 rx"] = {"name": "rx 580", "samples": [
            {"p": 30000 + n * 500, "url": f"https://hardverapro.hu/apro/c{n}.html", "site": "hardverapro", "at": now,
             "last": now, "t": "Sapphire RX 580 Nitro+"} for n in range(3)] + [
            {"p": 21000, "url": "https://hardverapro.hu/apro/c9.html", "site": "hardverapro", "at": now, "last": now,
             "t": "Sapphire RX580 4G Nitro"}]}
        (root / "arbazis.json").write_text(json.dumps({"keys": keys}), encoding="utf-8")
        book = PriceBook(root, clock=self.clock)
        self.assertEqual(len(book.samples("10400f i5")), 6)
        self.assertEqual(book.reference("10400f i5")["n"], 6)
        self.assertEqual([s["p"] for s in book.samples("4gb 580 rx")], [21000], "a saját címe szerinti változatához")
        self.assertEqual(sorted(s["p"] for s in book.samples("580 rx")), [30000, 30500, 31000])
        book.save()
        self.assertEqual(json.loads((root / "arbazis.json").read_text(encoding="utf-8"))["kv"], pricebook_module.KEY_VERSION)

    def test_a_single_capacity_only_for_known_models(self):
        """A kapacitás nélküli címek csak az ismerten egyetlen kapacitással gyártott típusnál (a GTX 1080 mind 8 GB-os)
        számítanak a változathoz. Az árbázis tartalma ezt nem bizonyítja: az RTX 5060 Ti 8 és 16 GB-os is, így ott hat
        kapacitás nélküli hirdetésből nem lehet egy 8 GB-os kártya „biztos” ára (eddig 180 200 Ft lett belőle)."""
        book = PriceBook(Path(self.tmp.name), clock=self.clock)
        book.add([{"text": f"GTX 1080 {'8GB' if n < 2 else 'Gaming X'}", "price": 60000 + n * 1000,
                   "url": f"https://hardverapro.hu/apro/g{n}.html", "platform": "hardverapro"} for n in range(6)])
        self.assertEqual(book.reference("1080 8gb gtx")["n"], 6)
        self.assertEqual(book.reference("1080 gtx")["n"], 6)
        book.add([{"text": "GTX 1080 16GB", "price": 90000, "url": f"https://hardverapro.hu/apro/h{n}.html",
                   "platform": "hardverapro"} for n in range(5)])
        self.assertEqual(book.reference("1080 8gb gtx")["n"], 6, "a GTX 1080 nem létezik 16 GB-tal: az zaj")
        book.add([{"text": "RTX 5060 Ti", "price": 180000 + n * 100, "url": f"https://hardverapro.hu/apro/r{n}.html",
                   "platform": "hardverapro"} for n in range(6)])
        book.add([{"text": "RTX 5060 Ti 8GB", "price": 120000, "url": "https://hardverapro.hu/apro/r8.html",
                   "platform": "hardverapro"}])
        self.assertIsNone(book.reference("5060 8gb rtx ti"), "egyetlen 8 GB-os ár van: nincs szokásos ár")

    def test_product_keys_from_titles(self):
        cases = {
            "ASUS Dual RTX 3060 Ti 8GB videókártya": "3060 8gb rtx ti", "Gigabyte GeForce RTX 3060 Ti Gaming OC": "3060 rtx ti",
            "iPhone 13 Pro Max 128GB": "128gb 13 iphone max pro", "PS5 DualSense kontroller": "kontroller ps5",
            "Sony WH-1000XM4 fejhallgató": "sony wh1000xm4", "LEGO Technic 42115 Lamborghini": "42115 lego",
            "Makita DHP482 akkus fúró": "dhp482 makita", "Nike Air Max 90 42-es": "90 air max nike",
            "PlayStation 5 Digital Edition": "digital ps5", "RTX 3060 95000 Ft": "3060 rtx",
            "RTX 3060 Ti 8 GB": "3060 8gb rtx ti", "AirPods Pro 2": "2 airpods pro",
            "Xiaomi Redmi Note 10 Pro": "10 note pro redmi", "Xbox Series X 1TB": "1tb seriesx xbox",
            # a kapacitás más változat, más ár: külön kulcs (a „16 GB” két szóban és az „1000GB” is)
            "RTX 5060 Ti 16 GB": "16gb 5060 rtx ti", "Samsung 970 EVO Plus 1000GB": "1tb 970 plus samsung",
            "Corsair Vengeance LPX 16GB DDR4 3200MHz": "16gb ddr4",
            "MacBook Air M1 8GB 256GB": "256gb 8gb air m1 macbook",
            "Xbox Series S": "seriess xbox", "Nintendo Switch OLED": "oled switch", "Ryzen 5 5600X": "5600x ryzen",
        }
        for title, key in cases.items():
            self.assertEqual(product_key(title)[0], key, title)
        # ami nem egy biztos termék: csak a család, a márka egy cipőmérettel, vagy több generáció egy néven
        for title in ("Eladó videókártya jó állapotban", "Zara kabát 38-as", "PS5 Spider-Man 2", "PS5", "Nike cipő 42",
                      "AirPods Pro", "Nintendo Switch", "Xbox One S", "Corsair DDR4 8GB 16GB 32GB"):
            self.assertIsNone(product_key(title), title)
        for title in ("iPhone 13 tok", "Töltő iPhone 13-hoz", "RTX 3060 Ti hibás, alkatrésznek", "Keresek RTX 3060-at",
                      "3 db RTX 3060 csomagban", "2x RTX 3060"):
            self.assertFalse(usable(title), title)

    def test_a_title_without_capacity_is_priced_as_the_cheapest_variant(self):
        """Ha a címből nem derül ki, 8 vagy 16 GB-os-e a kártya, a legolcsóbb változat szokásos árával számol (így egy
        8 GB-os nem látszik olcsó 16 GB-osnak); a keresőszó a kapacitással megy tovább."""
        with tempfile.TemporaryDirectory() as tmp:
            book = PriceBook(Path(tmp), Clock())
            book.add([listing(n, f"RTX 5060 Ti 8GB {n}", p) for n, p in enumerate((110000, 112000, 108000, 111000, 109000))])
            self.assertEqual(book.reference("5060 rtx ti")["price"], 110000, "csak egy változat van")
            book.add([listing(10 + n, f"RTX 5060 Ti 16 GB {n}", p)
                      for n, p in enumerate((150000, 152000, 148000, 151000, 149000))])
            self.assertEqual((book.reference("16gb 5060 rtx ti")["price"], book.reference("5060 8gb rtx ti")["price"]),
                             (150000, 110000), "a két változat külön")
            plain = book.reference("5060 rtx ti")
            self.assertEqual((plain["price"], plain["name"]), (110000, "rtx 5060 ti 8gb"), "a legolcsóbb változat")
            self.assertEqual(book.variants("16gb 5060 rtx ti"), [])
            self.assertEqual(book.query("Gigabyte RTX 5060 Ti 16 GB Gaming OC"), "RTX 5060 Ti 16GB")
            book.add([listing(1, "RTX 5060 Ti 8GB", 99000)])
            [cut] = [s for s in book.samples("5060 8gb rtx ti") if s["url"].endswith("100001.html")]
            self.assertEqual((cut["cuts"], cut["p0"], cut["p"]), (1, 112000, 99000), "leárazta")

    def test_a_cheap_product_needs_a_smaller_profit(self):
        """Egy 10 000 Ft-os terméknél az 5 000 Ft-os min. haszonnal legfeljebb 2 500 Ft-ért érné meg, de 4 000 Ft alatt
        már gyanúsan olcsó: így semmi nem ment át. Olcsó terméknél a min. haszon a szokásos ár negyede."""
        self.assertEqual((profit_floor(10000, 5000), profit_floor(40000, 5000), profit_floor(2000, 5000)),
                         (2500, 5000, 1000))
        for reference in (10000, 15000, 20000, 60000):
            self.assertGreater(explore_top(reference, 5000), reference * SUSPECT_RATIO, reference)
        self.assertEqual(explore_top(10000, 5000), 5000)

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
        # (a munkamenetek a Vinteden is: a friss lista kikapcsolva, lásd GlanceTests)
        self.hunter.set_browse(on=True, sites=["hardverapro", "vinted"], glance=0)
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
        # a következő oldal (nem vár a Hardverapró pihenőjére): a Vinteden az Elektronika kategória legújabbjait nézi
        # (a kezdőoldal és a ruhamárkák helyett), és ami mégis ruha, azt kihagyja (azzal nem kereskedik)
        feed = [listing(40, "Nike pulóver", 8000, "www.vinted.hu"), listing(41, "Zara kabát", 12000, "www.vinted.hu"),
                listing(42, "Retró kütyü X100", 9000, "www.vinted.hu")]
        self.browser.by_url = [("vinted.hu", feed)]
        self.clock.now += 61
        self.assertTrue(self.hunter.available())
        self.hunter.run(notify=self.notify)
        self.assertEqual(self.browser.urls[-1], "https://www.vinted.hu/catalog?catalog[]=2994&order=newest_first")
        pending = [r["text"] for r in self.hunter._pending]
        self.assertEqual(len(pending), 3, "az ismeretlen kütyü a következő kötegre vár")
        self.assertFalse([t for t in pending if "Nike" in t or "Zara" in t], "a ruhát kihagyta")
        self.assertEqual(self.hunter.funnel()["counts"]["clothing"], 2)

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

    def test_turbo_names_the_unknowns_after_every_page(self):
        """Turbó (az Indítás melletti gomb): az ismeretleneket minden oldal után megnevezteti (nem várja meg a 20-at), és
        a munkamenet végén a maradékot is; a weboldalaktól nem kér többet. Megmarad a következő indításra is;
        kikapcsolva a régi kötegelés jön vissza."""
        self.assertFalse(self.hunter.state()["turbo"], "alapból ki van kapcsolva")
        self.hunter.set_browse(on=True, sites=["hardverapro", "vinted"], glance=0)   # (Vinted-munkamenet)
        self.hunter.run(notify=self.notify)
        self.assertEqual((self.explored(), len(self.hunter._pending)), ([], 2), "Turbó nélkül a kötegre vár")
        self.assertTrue(self.hunter.set_turbo(True)["turbo"])
        self.assertIn("Bekapcsoltad a Turbót", self.journal()[0])
        self.assertTrue(self.make().turbo, "megmarad")
        loaded = len(self.browser.urls)
        self.clock.now += 26 * 60
        self.hunter.run(notify=self.notify)    # a munkamenet vége: Turbóban a két maradék is a modellhez megy
        self.assertEqual(len(self.explored()), 1)
        self.assertIn("Laptop táska", self.explored()[0])
        self.assertEqual(self.hunter._pending, [])
        self.assertEqual(len(self.browser.urls), loaded, "ehhez nem töltött le semmit")
        self.browser.by_url = [("vinted.hu", [listing(42, "Retró kütyü X100", 9000, "www.vinted.hu")])]
        self.clock.now += 61
        self.hunter.run(notify=self.notify)    # a következő oldal (Vinted): egyetlen ismeretlen, rögtön a modellhez
        self.assertEqual(len(self.explored()), 2)
        self.assertIn("Retró kütyü X100", self.explored()[-1])
        self.hunter.set_turbo(False)
        self.assertIn("Kikapcsoltad a Turbót", self.journal()[0])
        self.browser.by_url = [("vinted.hu", [listing(43, "Retró kütyü X200", 9500, "www.vinted.hu")])]
        self.clock.now += 121
        self.hunter.run(notify=self.notify)
        self.assertEqual(len(self.explored()), 2, "kikapcsolva újra kötegben")
        self.assertEqual(len(self.hunter._pending), 1)

    def test_a_captcha_stops_that_site_for_the_day(self):
        self.browser.challenge = {"vinted.hu"}
        self.hunter.set_browse(on=True, sites=["vinted"])
        self.hunter.run(notify=self.notify)
        self.assertEqual([t for _k, t, _x, _e in self.notices], ["Vinted: robot-ellenőrzés"])
        self.assertTrue(self.notices[0][3]["quiet"], "holnap magától folytatja: nem fontos értesítés")
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

    def test_vinted_at_most_every_two_hours_without_the_fresh_list(self):
        """A friss lista nélkül (glance=0, a régi mód) a Vinteden munkamenetek, és két óra pihenő közöttük."""
        self.browser.by_url = [("vinted.hu", [])]
        self.hunter.set_browse(on=True, sites=["vinted"], glance=0)
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

    def test_by_default_it_looks_at_everything_new_not_at_chosen_products(self):
        """Alapból egész kategóriák, a legújabb hirdetésekkel elöl: nincs beégetett termék (pl. iPhone) a böngészésben."""
        lanes = self.hunter.state()["lanes"]
        self.assertEqual({lane["site"] for lane in lanes}, {"hardverapro", "jofogas", "vinted", "vatera"})
        for lane in lanes:
            self.assertEqual((lane["kind"], lane["on"]), ("page", True), lane["name"])
            self.assertNotRegex(lane["url"], r"[?&](q|search_text|stext)=", "kategória, nem keresőszó")
        urls = {lane["site"]: lane["url"] for lane in lanes}
        self.assertEqual(urls["vinted"], "https://www.vinted.hu/catalog?catalog[]=2994&order=newest_first")
        self.assertEqual(urls["jofogas"], "https://www.jofogas.hu/magyarorszag/muszaki-cikkek-elektronika?sp=3")
        self.assertIn("ob=5&obd=2", urls["vatera"], "a Vaterán is a legújabb elöl")

    def test_start_pages_can_be_switched_off_added_and_removed(self):
        hunter = self.hunter
        hunter.set_browse(on=True, sites=["hardverapro", "jofogas"])
        names = lambda: {lane["name"]: lane for lane in hunter.state()["lanes"]}  # noqa: E731
        # egy beépített kikapcsolva: a listában marad, de a böngészés nem nézi
        state = hunter.lane_switch("hardverapro:minden_hardver", False)
        self.assertFalse(names()["Hardverapró: Minden hardver"]["on"])
        picks = {hunter._pick_lane("hardverapro", [], False)["name"] for _ in range(200)}
        self.assertNotIn("Hardverapró: Minden hardver", picks)
        hunter.lane_switch("hardverapro:minden_hardver", True)
        self.assertTrue(names()["Hardverapró: Minden hardver"]["on"])
        # saját: egy keresőszó a bejelölt oldalakon (a legújabb elöl), egy oldal címe a sajátján
        state = hunter.lane_add("Lego")
        own = [lane for lane in state["lanes"] if lane["kind"] == "sajat"]
        self.assertEqual([(lane["name"], lane["on"]) for lane in own], [("Hardverapró: Lego", True), ("Jófogás: Lego", True)])
        self.assertTrue(names()["Jófogás: Lego"]["url"].endswith("?q=Lego&sp=3"))
        state = hunter.lane_add("https://www.vinted.hu/catalog?search_text=nintendo%20switch")
        lane = names()["Vinted: nintendo switch"]
        self.assertEqual(lane["url"], "https://www.vinted.hu/catalog?search_text=nintendo%20switch&order=newest_first")
        with self.assertRaises(DealError):
            hunter.lane_add("Lego", "jofogas")          # már megvan
        with self.assertRaises(DealError):
            hunter.lane_add("https://example.com/lista")
        with self.assertRaises(DealError):
            hunter.lane_remove("hardverapro:videokartyak")   # a beépítettet kikapcsolni lehet
        hunter.lane_switch(names()["Jófogás: Lego"]["id"], False)
        self.assertFalse(names()["Jófogás: Lego"]["on"])
        hunter.lane_remove(names()["Jófogás: Lego"]["id"])
        self.assertNotIn("Jófogás: Lego", names())
        self.assertIn("Új indulóoldal: Hardverapró: Lego, Jófogás: Lego.", self.journal())
        self.assertIn("Kikapcsoltad ezt az indulóoldalt: Hardverapró: Minden hardver.", self.journal())

    def test_a_removed_learned_page_is_not_learned_again_and_learning_can_be_off(self):
        hunter = self.hunter
        hunter.set_browse(on=True, sites=["hardverapro"], learn=False)
        self.clock.now += 3 * 86400
        for row in NORMAL[:2]:
            hunter.book.revisited("3060 rtx ti", row["url"], gone=True)
        hunter.run(notify=self.notify)
        self.assertEqual([lane for lane in hunter.state()["lanes"] if lane["kind"] == "tanult"], [],
                         "kikapcsolt öntanulással nem vesz fel magától")
        self.assertFalse(hunter.state()["browse"]["learn"])
        hunter.set_browse(learn=True)
        with hunter._lock:
            hunter._data["sites_state"]["learned"] = ""
            hunter._data["session"] = {}
        self.clock.now += 86400
        hunter.run(notify=self.notify)
        [lane] = [lane for lane in hunter.state()["lanes"] if lane["kind"] == "tanult"]
        hunter.lane_remove(lane["id"])
        self.assertEqual([lane for lane in hunter.state()["lanes"] if lane["kind"] == "tanult"], [])
        self.assertIn("Törölted ezt az indulóoldalt: Hardverapró: rtx 3060 ti (nem veszem fel újra magamtól).",
                      self.journal())
        for _ in range(2):                       # később sem tanulja meg újra (az egy hónapos némítás után sem)
            with hunter._lock:
                hunter._data["session"] = {}
            self.clock.now += 40 * 86400
            for row in NORMAL[:2]:
                hunter.book.revisited("3060 rtx ti", row["url"], gone=True)
            hunter.run(notify=self.notify)
        self.assertEqual([lane for lane in hunter.state()["lanes"] if lane["kind"] == "tanult"], [])

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
            self.hunter.make_offer(self.deal("/items/5-jofogas", platform="jofogas"), None, confirm=self.confirm)
        self.assertEqual((self.site.posts, self.prompts), ([], []))


class CapacityTests(HunterCase):
    def test_a_different_capacity_is_not_the_same_product(self):
        """A négy szónál hosszabb keresésben egy szó hiányozhat, de a kapacitás nem: egy 500 GB-os SSD ára nem jöhet az 1
        TB-osokéból (eddig öt 1 TB-os hirdetésből „biztos” árat mondott)."""
        query = "Samsung 970 EVO Plus 500GB"
        self.assertFalse(matches_query(query, "Samsung 970 EVO Plus 1TB"))
        self.assertTrue(matches_query(query, "Samsung 970 EVO Plus 500 GB NVMe"))
        self.assertTrue(matches_query(query, "Samsung 970 EVO Plus"), "a kapacitás nélküli hirdetés keresésnek jó")
        self.assertFalse(matches_query(query, "Samsung 970 EVO Plus", strict_size=True), "árösszehasonlításnak nem")
        self.assertTrue(matches_query("Samsung 970 EVO Plus", "Samsung 970 EVO Plus 1TB"))
        row = {"text": "Samsung 970 EVO Plus 500GB", "price": 12000, "url": "https://hardverapro.hu/apro/ssd/1.html"}
        terabytes = [{**listing(60 + n, f"Samsung 970 EVO Plus 1TB {n}", p, site), "platform": platform}
                     for n, (p, site, platform) in enumerate(((30000, "hardverapro.hu", "hardverapro"),
                                                              (31000, "www.jofogas.hu", "jofogas"),
                                                              (29000, "hardverapro.hu", "hardverapro"),
                                                              (30500, "www.jofogas.hu", "jofogas"),
                                                              (29500, "hardverapro.hu", "hardverapro")))]
        self.assertIsNone(self.hunter._market_of(row, query, terabytes), "az 1 TB-osok nem számítanak")

    def test_without_capacity_the_cheapest_variant_counts(self):
        """Ha a keresőszóban nincs kapacitás, a változatok külön számítanak, és a legolcsóbb (legalább három árral) dönt:
        egy ismeretlen kapacitású hirdetés nem látszik olcsónak a drágább változathoz mérve."""
        rows = [{**listing(70 + n, f"Samsung 970 EVO Plus {size} {n}", p), "platform": "hardverapro"}
                for n, (size, p) in enumerate((("500GB", 20000), ("500GB", 21000), ("500GB", 19500),
                                               ("1TB", 34000), ("1TB", 35000), ("1TB", 33000), ("1TB", 36000),
                                               ("2TB", 9000)))]
        row = {"text": "Samsung 970 EVO Plus", "price": 15000, "url": "https://hardverapro.hu/apro/ssd/2.html"}
        found = self.hunter._market_of(row, "Samsung 970 EVO Plus", rows)
        self.assertEqual((found["price"], found["n"]), (20000, 3), "az 500 GB-osok (a 2 TB-os egyetlen ára kevés)")


# Nintendo Switch Lite: hat összehasonlító az árbázisban, két oldalról (a címük szerint mind hibátlan); a 60 000-es
# leírása szerint repedt a kijelzője. A jelölt (egy figyelt Hardverapró-oldalon) kijelzőhibás, és töltő nélkül adják.
LITE = [{**listing(160 + n, f"Nintendo Switch Lite szürke {n}", p, f"www.{site}.hu"), "platform": site} for n, (p, site) in
        enumerate(zip((60000, 58000, 59000, 61000, 62000, 60500), ("jofogas", "hardverapro") * 3))]
LITE_PAGE = "https://hardverapro.hu/aprok/szoftver_jatek/konzol/index.html"
LITE_CHEAP = listing(150, "Nintendo Switch Lite", 26000)


class ConditionDealTests(HunterCase):
    """Részletes állapot (kijelző, akkumulátor, működés, tartozékok, garancia) bizonyítékkal; az összehasonlítók
    állapota a leírásuk szerint (eltárolva); az állapothoz igazított ár és a teljes költség; ellenőrzött vagy
    ellenőrizetlen ajánlat."""

    def setUp(self):
        super().setUp()
        self.hunter.set_fast_alert(False)         # (itt az ellenőrzött ajánlat a téma: a modell a csengetés előtt kérdez)
        self.browser.results = []                 # a többi oldalon nincs több találat
        self.browser.by_url = [("konzol", [LITE_CHEAP])]
        self.browser.pages = {
            LITE_CHEAP["url"]: "Nintendo Switch Lite. A kijelzője repedt, töltő nélkül adom. Egyébként hibátlanul működik.",
            LITE[0]["url"]: "Nintendo Switch Lite szürke. A kijelzője repedt, de működik. Töltővel.",
        }
        self.hunter.book.add(LITE)

    def test_a_display_defect_and_a_missing_charger_need_a_basis(self):
        """A címek szerint hat hibátlan Switch Lite: a szokásos ár csak azután biztos, hogy a legfontosabbak leírását is
        elolvasta (a repedt kijelzőjű kimarad a hibátlanok árából, az eredmény eltárolva). A jelölt kijelzőhibás és töltő
        nélküli: ezek tények, bizonyítékkal; a levonáshoz alap kell (hasonló hibájú összehasonlító, vagy megadott
        költség). Amíg nincs, nem sürgős találat, hanem ellenőrizetlen tipp; a gyors jelzés viszont rögtön ment. Ha
        megadod a pótlás és a javítás költségét, a következő kör újraszámolja, és ellenőrzötten szól, a levonásokkal."""
        hunter = self.hunter
        raw = hunter.book.reference(hunter.book.key("Nintendo Switch Lite")[0], exclude=LITE_CHEAP["url"])
        screened = hunter._screened(raw)
        self.assertEqual((screened["enough"], screened["sure"]), (True, False),
                         "elég ár van, de a leírásukat még nem olvasta el: nem biztos")
        hunter.add_watch(LITE_PAGE)
        hunter.set_enabled(True)
        hunter.run(notify=self.notify)
        self.assertEqual([p[1] for p in self.pings], ["Ígéretes új hirdetés: Nintendo Switch Lite"])
        [(_kind, title, text, extra)] = self.notices
        self.assertEqual(title, "Ellenőrizetlen: Nintendo Switch Lite")
        self.assertIn("hibás a kijelzője, és nincs hozzá hasonló hibájú összehasonlító vagy megadott javítási költség",
                      text)
        self.assertIn("nincs töltő, és nincs hozzá hasonló hiányú összehasonlító vagy megadott pótlási költség", text)
        self.assertNotIn("urgent", extra)
        self.assertEqual(hunter.state()["deals"], [])
        [tip] = hunter.state()["tips"]
        self.assertEqual((tip["hold"], tip["issues"], tip["unverified"]), ("állapot", ["kijelző", "töltő"], True))
        self.assertIn("kijelző: hibás („kijelzője repedt”)", tip["condition_text"])
        self.assertIn("tartozékok: hiányzik: töltő („töltő nélkül”)", tip["condition_text"])
        # az összehasonlítók közül a szokásos árhoz legközelebbi hármat olvasta el, eltárolva (újraindítás után is);
        # és a jelölt saját állapotát is (az adatlapjáról: egy későbbi összehasonlításnál ő is összehasonlító)
        vetted = DealHunter(Path(self.tmp.name), PLATFORMS, lambda: self.browser, self.llm, clock=self.clock)._vetted
        self.assertEqual(len([k for k in vetted if k != listing_key(LITE_CHEAP["url"])]), 3)
        self.assertEqual(vetted[listing_key(LITE_CHEAP["url"])]["state"], "hibás")
        self.assertEqual(vetted[listing_key(LITE[0]["url"])]["issues"], ["kijelző"])
        looked = list(self.browser.looked)
        hunter.run(notify=self.notify, force=True)
        self.assertEqual(self.browser.looked, looked, "költség nélkül újra sem nézi meg, és az eltárolt állapotot "
                                                      "sem olvassa el újra")
        # költségek: a töltő pótlása (minden termékre) és a kijelző javítása (csak a Switch Lite-ra)
        self.clock.now += 600
        hunter.add_cost("Töltő", "", 3000)
        state = hunter.add_cost("kijelző", "Switch Lite", "15 000")
        self.assertEqual([(c["part"], c["product"], c["amount"]) for c in state["costs"]],
                         [("töltő", "", 3000), ("kijelző", "Switch Lite", 15000)])
        self.notices.clear()
        hunter.run(notify=self.notify, force=True)
        [(_kind, title, text, extra)] = self.notices
        self.assertEqual(title, "Olcsó: Nintendo Switch Lite")
        self.assertTrue(extra["urgent"])
        self.assertIn("Ellenőrizve: az adatlapja megnyílt, a leírása és az állapota alapján újraszámoltam, és a modell "
                      "is átnézte. A haszon hirdetési árakból becsült, nem garancia.", text)
        self.assertIn("Levonva: kijelző javítása kb. 15 000 Ft, töltő pótlása kb. 3 000 Ft.", text)
        [deal] = hunter.state()["deals"]
        self.assertTrue(deal["verified"])
        self.assertEqual(deal["costs"], 1500 + 3000 + 15000)
        self.assertEqual(deal["profit"], deal["resale"] - 26000 - 1500 - 18000, "a posta, a pótlás és a javítás is")
        self.assertEqual(deal["safety"]["level"], "jó", "a javítás miatt nem „biztos”")
        self.assertIn("kijelző javítása levonva (kb. 15 000 Ft)", deal["safety"]["why"])
        excluded = [e for e in deal["basis"]["out"] if e["u"] == LITE[0]["url"]]
        self.assertEqual([e["why"] for e in excluded], ["a leírása szerint hibás a kijelzője"],
                         "a hibátlanok árából kimaradt, az okkal")
        self.assertEqual(len(self.pings), 1, "a gyors jelzés egyszer ment")
        self.assertEqual(hunter.state()["tips"], [])

    def test_similar_defects_set_the_price_and_nothing_counts_twice(self):
        """Ha legalább két összehasonlító ugyanígy hibás, az ő áruk a jelölt mércéje (a hiba már benne van): a megadott
        javítási költséget nem vonja le még egyszer; ami nincs lefedve (a töltő), azt igen. A nem működő kiesik; ha a
        költségek után már nem éri meg, az is."""
        hunter = self.hunter
        hunter.add_cost("kijelző", "", 15000)
        hunter.add_cost("töltő", "", 3000)
        cond = {"display": {"state": "hibás", "evidence": "repedt a kijelző"}, "missing": ["töltő"],
                "accessories": {"state": "hiányos", "evidence": "töltő nélkül"}, "issues": ["kijelző", "töltő"]}
        refs = [{"p": 40000, "u": "a", "issues": ["kijelző"]}, {"p": 42000, "u": "b", "issues": ["kijelző"]}]
        row = {**LITE_CHEAP, "site": "Hardverapró", "platform": "hardverapro", "title": "Nintendo Switch Lite"}
        deal = hunter._deal({"id": "w", "query": "Nintendo Switch Lite"}, row, 60000, low=59000, refs=refs,
                            floor=5000, condition=cond)
        self.assertEqual(hunter._reprice(deal), ("ok", ""))
        self.assertEqual(deal["cond_band"], {"issues": ["kijelző"], "n": 2, "price": 41000, "clean": 60000})
        self.assertEqual([p["what"] for p in deal["cost_parts"]], ["posta", "töltő pótlása"], "a kijelzőt nem kétszer")
        self.assertEqual(deal["profit"], safe_sale(41000, 40000) - 26000 - 1500 - 3000)
        broken = hunter._deal({"id": "w", "query": "x"}, row, 60000, condition={"function": {"state": "hibás"},
                                                                              "issues": ["működés"]})
        self.assertEqual(hunter._reprice(broken), ("hibás", "a leírása szerint nem működik"))
        dear = hunter._deal({"id": "w", "query": "x"}, {**row, "price": 40000}, 60000, low=59000, floor=5000,
                            condition=cond)
        state, why = hunter._reprice(dear)
        self.assertEqual(state, "nem éri meg")
        self.assertIn("ez már kevés", why)
        box = hunter._deal({"id": "w", "query": "x"}, row, 60000, condition=conditions.compact(
            conditions.from_text("Doboz nélkül, hibátlanul működik.")))
        self.assertEqual(hunter._reprice(box), ("ok", ""), "a doboz hiánya önmagában nem kér alapot")
        self.assertIn("doboz nélkül", assess({**box, "verified": True})["why"])

    def test_the_listing_page_keeps_known_defects_and_drops_the_rest(self):
        """Az adatlapon a felismert hiba (repedt kijelző, hiányzó töltő) tény, a jelölt marad (az ár igazodik hozzá); a nem
        működő, az alkatrésznek adott, és amelyiknél hibára utaló szó van, de nem tudni, mi a hiba, kiesik."""
        base = {"url": "https://hardverapro.hu/apro/1.html", "text": "Switch Lite", "platform": "hardverapro",
                "title": "Switch Lite"}
        for text, verdict in (("A kijelzője repedt, töltő nélkül.", ""), ("Nem kapcsol be.", "hibás"),
                              ("Csak alkatrésznek.", "hibás"), ("A hangszóró hibás.", "hibás"),
                              ("Hibátlan, dobozában.", "")):
            deal = dict(base)
            with self.subTest(text=text):
                self.assertEqual(self.hunter._inspect(deal, {"url": base["url"], "text": text, "title": ""}), verdict)
                self.assertTrue(deal["page_checked"])

    def test_the_model_condition_is_checked_and_merged(self):
        """A modell szempontonkénti állapotát az idézetei alapján ellenőrzi: a kitalált idézet ismeretlen lesz, a
        kulcsszavak szerinti rosszabb állapot nem íródik felül."""
        deal = {"site": "Hardverapró", "text": "Switch Lite", "title": "Switch Lite", "price": 26000, "median": 60000,
                "details": "Switch Lite. A kijelző karcos, töltő nincs hozzá. Garancia 2027-ig.", "url": "u"}

        class Model:
            def chat_json(self, messages, schema, **kwargs):
                self.schema = schema
                return {"ok": True, "suspicious": False, "reason": "Ugyanaz.",
                        "condition": {"display": {"state": "ép", "evidence": "a kijelző karcmentes"},
                                      "battery": {"state": "jó", "evidence": "az akku kiváló"},
                                      "function": {"state": "ismeretlen", "evidence": ""},
                                      "accessories": {"state": "hiányos", "evidence": "töltő nincs hozzá"},
                                      "warranty": {"state": "van", "evidence": "Garancia 2027-ig"},
                                      "missing": ["töltő"]}}

        self.hunter.llm = Model()
        self.assertTrue(self.hunter._confirm({"query": "Nintendo Switch Lite"}, deal))
        self.assertIn("condition", self.hunter.llm.schema["required"])
        cond = deal["condition"]
        self.assertEqual(cond["display"]["state"], "karcos", "a kitalált „karcmentes” helyett a leírás szerinti")
        self.assertEqual(cond["battery"]["state"], "ismeretlen", "az idézet nincs a hirdetésben")
        self.assertEqual((cond["warranty"]["state"], cond["missing"], cond["issues"]), ("van", ["töltő"], ["töltő"]))

    def test_costs_are_kept_and_the_specific_one_wins(self):
        hunter = self.hunter
        hunter.add_cost("akku", "", 8000)
        hunter.add_cost("akkumulátor", "Switch Lite", 12000)
        deal = {"title": "Nintendo Switch Lite szürke", "text": "", "query": ""}
        self.assertEqual(hunter._cost("akkumulátor", deal), 12000, "a termékre szabott az erősebb")
        self.assertEqual(hunter._cost("akkumulátor", {"title": "Steam Deck"}), 8000)
        self.assertIsNone(hunter._cost("töltő", deal))
        state = hunter.add_cost("akku", "", 9000)
        self.assertEqual(sorted((c["product"], c["amount"]) for c in state["costs"]), [("", 9000), ("Switch Lite", 12000)],
                         "ugyanarra a kettőre a régit lecseréli")
        self.assertIn("kijelző", state["cost_parts"])
        for bad in (("", "", 1000), ("töltő", "", "")):
            with self.assertRaises(DealError):
                hunter.add_cost(*bad)
        again = DealHunter(Path(self.tmp.name), PLATFORMS, lambda: self.browser, self.llm, clock=self.clock)
        self.assertEqual(len(again.state()["costs"]), 2)
        hunter.remove_cost(state["costs"][0]["id"])
        self.assertEqual(len(hunter.state()["costs"]), 1)


class ComparableTests(HunterCase):
    """A hasonló hirdetések a termékkulcs szerint: a modell elnevezésének márka- és típusnév-szavai (MSI, OC, Intel
    Core) nem zárják ki a többi kártyagyártó és az eltérően írt hirdetéseket (eddig ezért volt csak 2 ár egy GTX 1080-ra)."""

    def test_other_board_makers_and_spellings_count(self):
        rows = [{**listing(80 + n, title, p), "platform": "hardverapro"} for n, (title, p) in enumerate((
            ("Gigabyte GTX 1080 G1 Gaming", 62000), ("MSI GTX 1080 Gaming X 8GB", 65000), ("Nvidia GeForce GTX 1080", 60000),
            ("Asus Strix GTX 1080 OC", 64000), ("Zotac GTX1080 AMP", 61000), ("GTX 1080 Ti", 120000),
            ("GTX 1080 hűtő", 5000)))]
        row = {"text": "MSI GeForce GTX 1080 OC 8GB videókártya eredeti dobozával", "price": 43999,
               "url": "https://hardverapro.hu/apro/msi_1080/1.html"}
        found = self.hunter._market_of(row, "MSI GTX 1080 OC 8GB", rows)
        self.assertEqual((found["n"], found["price"]), (5, 62000), "a Ti és a hűtő nem számít")
        self.assertEqual(self.hunter._probe_text("MSI GeForce GTX 1080 OC 8GB"), "GTX 1080")
        cpus = [{**listing(90 + n, title, p), "platform": "hardverapro"} for n, (title, p) in enumerate((
            ("Intel Core i5 10400F processzor", 30000), ("i5-10400F", 29000), ("Eladó i5 10400f CPU", 31000),
            ("Intel i5-10400F dobozos", 30500), ("Core i5-10400F hűtővel", 32000)))]
        row = {"text": "Intel Core i5-10400F", "price": 12000, "url": "https://hardverapro.hu/apro/i5/1.html"}
        self.assertEqual(self.hunter._market_of(row, "Intel Core i5-10400F", cpus)["n"], 5)
        self.assertEqual(self.hunter._probe_text("Intel Core i5-10400F"), "i5-10400F")

    def test_capacity_less_listings_are_not_an_8gb_card(self):
        """Egy 8 GB-os RTX 5060 Ti-hoz a kapacitás nélküli hirdetések nem hasonlók (lehetnek 16 GB-osak is)."""
        rows = [{**listing(95 + n, f"RTX 5060 Ti {name}", 180000 + n * 100), "platform": "hardverapro"}
                for n, name in enumerate(("Dual", "Ventus", "Eagle", "Gaming", "Windforce", "Prime"))]
        rows.append({**listing(101, "RTX 5060 Ti 8GB Twin", 120000), "platform": "hardverapro"})
        row = {"text": "RTX 5060 Ti 8GB", "price": 110000, "url": "https://hardverapro.hu/apro/5060/1.html"}
        found = self.hunter._market_of(row, "RTX 5060 Ti 8GB", rows)
        self.assertTrue(found is None or found["n"] == 1, "csak az egy 8 GB-os számít")


class NewestTests(HunterCase):
    """A legújabb hirdetések elöl: a Vinted, a Jófogás és a Vatera címében (NEWEST), ha nincs benne más rendezés."""

    def test_the_order_in_the_address(self):
        self.assertEqual(newest_first("https://www.vatera.hu/muszaki-cikk-es-mobil/index-c12082.html#", "vatera"),
                         "https://www.vatera.hu/muszaki-cikk-es-mobil/index-c12082.html?ob=5&obd=2")
        self.assertEqual(newest_first("https://www.jofogas.hu/magyarorszag/muszaki-cikkek-elektronika", "jofogas"),
                         "https://www.jofogas.hu/magyarorszag/muszaki-cikkek-elektronika?sp=3")
        for url, site in (("https://www.jofogas.hu/magyarorszag?q=ps5&sp=1", "jofogas"),
                          ("https://www.vatera.hu/listings/index.php?c=123&ob=ending", "vatera"),
                          ("https://hardverapro.hu/aprok/keres.php?stext=rtx", "hardverapro")):
            self.assertEqual(newest_first(url, site), url, "a választott rendezés marad; a Hardveraprón nincs ilyen")

    def test_searches_and_pasted_pages(self):
        self.hunter.add_search("PS5 kontroller", ["jofogas", "vatera"], reference=14000)
        self.hunter.set_enabled(True)
        self.hunter.run(notify=self.notify)
        self.assertTrue(any(u.endswith("jofogas.hu/magyarorszag?q=PS5%20kontroller&sp=3") for u in self.browser.urls))
        self.assertTrue(any(u.endswith("vatera.hu/listings/index.php?q=PS5%20kontroller&ob=5&obd=2")
                            for u in self.browser.urls))
        watches = self.hunter.add_watch("https://www.jofogas.hu/magyarorszag/konzol")["watches"]
        self.assertEqual(watches[-1]["url"], "https://www.jofogas.hu/magyarorszag/konzol?sp=3")
        watches = self.hunter.add_watch("https://www.vatera.hu/muszaki-cikk-es-mobil/index-c12082.html")["watches"]
        self.assertEqual(watches[-1]["url"], "https://www.vatera.hu/muszaki-cikk-es-mobil/index-c12082.html",
                         "a Vaterán a te rendezésed (a licitekhez a „hamarosan lejár” a jó)")


class SizeKindTests(unittest.TestCase):
    def test_ram_and_storage_are_told_apart(self):
        """A RAM és a tárhely nem keveredik a szűrésnél: ami a hirdetésben RAM-ként szerepel („4GB RAM”, „GDDR7”), az
        nem egy másik tárhely-változat (eddig kizárta az „iPhone 13 128GB” keresésből), és egy címben két méretnél
        („8GB 256GB”) a kisebb a RAM. Ami azonos fajtájú és más, az más termék."""
        self.assertTrue(matches_query("iPhone 13 128GB", "iPhone 13 128GB, 4GB RAM, kártyafüggetlen"))
        self.assertTrue(matches_query("MacBook Air M1 256GB", "MacBook Air M1 8GB RAM 256GB SSD"))
        self.assertTrue(matches_query("Galaxy S21 8GB RAM 128GB", "Samsung Galaxy S21 128GB 8GB"))
        self.assertTrue(matches_query("Kingston Fury 16GB RAM", "Kingston Fury Beast 16GB DDR4 3200"),
                        "a „RAM” szó csak a méretet írja le: nem kell a hirdetésbe")
        self.assertFalse(matches_query("Kingston Fury 16GB DDR5", "Kingston Fury Beast 16GB DDR4 3200"), "más típus")
        self.assertTrue(matches_query("MacBook Air M1 8GB 256GB", "MacBook Air M1 RAM: 8GB, SSD: 256GB"))
        self.assertFalse(matches_query("MacBook Air M1 8GB 256GB", "MacBook Air M1 16GB RAM 256GB SSD"), "más RAM")
        self.assertFalse(matches_query("MacBook Air M1 8GB 256GB", "MacBook Air M1 8GB 512GB"), "más tárhely")
        self.assertFalse(matches_query("RTX 5060 Ti 8GB", "RTX 5060 Ti 16GB GDDR7"), "más memória")
        self.assertFalse(matches_query("iPhone 13 128GB", "iPhone 13 256GB, 4GB RAM"), "más tárhely")
        self.assertFalse(matches_query("Samsung 970 EVO Plus 500GB", "Samsung 970 EVO Plus 1TB"))
        self.assertFalse(matches_query("iPhone 13 128GB", "iPhone 13 4GB RAM"),
                         "a tárhely nélküli hirdetés nem biztos, hogy ez a változat")


class SizeOrderTests(unittest.TestCase):
    def test_one_kind_word_labels_one_size(self):
        """Egy fajtát jelölő szó („RAM”, „SSD”) csak az egyik szomszédos méretet írja le: a „8GB RAM 256GB” címben a
        256 GB a tárhely (eddig ezt is memóriának vette, és a „MacBook Air M1 8GB 256GB” keresésből kizárta)."""
        self.assertEqual(sized("MacBook Air M1 8GB RAM 256GB"), [("8gb", "ram"), ("256gb", "storage")])
        self.assertEqual(sized("MacBook Air M1 256GB SSD 8GB"), [("256gb", "storage"), ("8gb", "ram")])
        self.assertEqual(sized("RAM: 16GB, SSD: 512GB"), [("16gb", "ram"), ("512gb", "storage")])
        self.assertEqual(sized("SSD 512GB RAM 16GB"), [("512gb", "storage"), ("16gb", "ram")])
        self.assertEqual(sized("16GB RAM 512GB SSD"), [("16gb", "ram"), ("512gb", "storage")])
        for title in ("MacBook Air M1 8GB RAM 256GB", "MacBook Air M1 256GB SSD 8GB", "MacBook Air M1 SSD 256GB RAM 8GB",
                      "MacBook Air M1 8GB RAM, 256GB SSD"):
            self.assertTrue(matches_query("MacBook Air M1 8GB 256GB", title), title)
        self.assertFalse(matches_query("MacBook Air M1 8GB 256GB", "MacBook Air M1 8GB RAM 512GB"), "más tárhely")
        self.assertFalse(matches_query("MacBook Air M1 8GB 256GB", "MacBook Air M1 16GB RAM 256GB"), "más RAM")


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
        self.assertEqual(self.notices[-1][3]["actions"], [{"action": "ledger", "label": "Elemzés"}])
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
        self.assertEqual(report["total"], {"profit": 43000, "count": 2, "spent": 64000, "revenue": 107000, "roi": 67,
                                           "own": 0, "own_revenue": 0})
        self.assertEqual((report["open"]["count"], report["open"]["spent"], report["open"]["unknown"]), (1, 15000, 1),
                         "a Nike-nak nincs ára: nem tudni, mennyi haszon várható belőle")
        self.assertEqual(report["days"], 20, "átlagosan (10 + 30) / 2 nap alatt kelt el")
        self.assertEqual([r["name"] for r in report["rows"]], ["Nike Air Max", "RTX 3060 Ti", "PS5 kontroller"],
                         "elöl a meg nem eladott, aztán a legutóbb eladott")
        self.assertEqual([(m["profit"], m["count"]) for m in report["months"]], [(40000, 1), (3000, 1)])

    def test_own_things_sold_by_jarvis_count_in_the_profit(self):
        """A saját holmid (nincs vételára, pl. a ruháid), amit a Jarvis töltött fel és elkelt: a bevétele a haszonba
        számít (ebben a hónapban, összesen, havonta), az eladás költségeivel együtt; a megtérülés (%) csak a megvett
        tárgyakból számol. Amíg nem kelt el, nem számít (nincs mit elszámolni)."""
        from jarvis_core.deals import profit_report
        from jarvis_core.selling import ItemStore
        with tempfile.TemporaryDirectory() as tmp:
            store = ItemStore(Path(tmp))
            now = time.time()
            blazer = store.create(title="Zara blézer", price=4500, targets=["vinted"])
            store.mark(blazer.id, "vinted")
            store.mark(blazer.id, sold=True)
            shoes = store.create(title="Nike cipő", price=8000, sell_shipping=1000)
            store.mark(shoes.id, sold=True)
            store.create(title="H&M póló", price=1500)          # még nem kelt el: nem számít
            card = store.create(title="RTX 3060 Ti", bought_price=55000, sold_price=95000, bought_at=now - 10 * 86400)
            store.mark(card.id, sold=True)
            report = profit_report(store.all(), now)
        rows = {r["id"]: r for r in report["rows"]}
        self.assertEqual(set(rows), {blazer.id, shoes.id, card.id})
        self.assertEqual((rows[blazer.id]["own"], rows[blazer.id]["stage"], rows[blazer.id]["cost"],
                          rows[blazer.id]["profit"], rows[blazer.id]["roi"]), (True, "eladva", 0, 4500, None))
        self.assertEqual(rows[shoes.id]["profit"], 7000, "az eladás postája levonódik")
        self.assertFalse(rows[card.id]["own"])
        self.assertEqual(report["total"], {"profit": 51500, "count": 3, "spent": 55000, "revenue": 107500, "roi": 73,
                                           "own": 2, "own_revenue": 12500})
        self.assertEqual(report["month"], {"profit": 51500, "count": 3, "revenue": 107500})
        self.assertEqual(report["stages"], {"eladva": 3})
        self.assertEqual(report["open"]["count"], 0, "a saját holmi nem pénz, ami bent van")

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
