"""Böngésző nélküli olvasás (feeds.py) és az azonnali figyelő (instant.py): a Hardverapró és a Jófogás találati oldalai
sima letöltéssel, oldalanként egyszerre egy kéréssel és szünetekkel; a tiltás megjegyzése; az új hirdetések értesítése; és
hogy az automata kereskedés köre a gyors oldalakat a böngészővel párhuzamosan tölti le."""
from __future__ import annotations

import random
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_deals import Clock, FakeBrowser, FakeLlm  # noqa: E402

from jarvis_core.deals import DealHunter  # noqa: E402
from jarvis_core.feeds import FeedError, SiteFetcher, parse_listings, parse_page  # noqa: E402
from jarvis_core.instant import InstantError, InstantWatch  # noqa: E402
from jarvis_core.selling import PLATFORMS  # noqa: E402


def ha_card(n: int, title: str, price: str, city: str = "Budapest") -> str:
    """Egy Hardverapró-találat, ahogy a lista egy sorában van (kép, cím, település, ár, idő, eladó)."""
    url = f"https://hardverapro.hu/apro/{title.lower().replace(' ', '_')}_{n}/friss.html"
    return f"""<li class="media" data-uadid="{n}">
      <a class="uad-image" href="{url}" title="{title}"><img src="/k/{n}.jpg" alt=""></a>
      <div class="media-body">
        <div class="uad-col uad-col-title"><h1><a href="{url}">{title}</a></h1><div class="uad-cities">{city}</div></div>
        <div class="uad-col uad-col-info"><div class="uad-price">{price}</div><div class="uad-ultralight">ma 14:0{n % 10}</div></div>
        <div class="uad-col uad-col-seller"><a href="/tag/eladó{n}.html">eladó{n}</a> (12 értékelés)</div>
      </div></li>"""


def ha_page(cards: list[str]) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Videókártya - HardverApró</title></head><body>
      <header><nav><a href="/aprok/hardver/index.html">Hardver</a> <a href="/akcio.html">Akció: RTX 4070 199 990 Ft</a></nav></header>
      <div class="uad-list"><ul class="list-unstyled">{"".join(cards)}</ul></div>
      <footer><a href="/aszf.html">ÁSZF</a></footer></body></html>"""


def jf_card(n: int, title: str, price: str) -> str:
    url = f"https://www.jofogas.hu/budapest/{title.lower().replace(' ', '-')}_{120000000 + n}.htm"
    return f"""<div class="list-item"><div class="contentArea">
      <h3 class="item-title"><a class="subject" href="{url}">{title}</a></h3>
      <div class="priceBox"><h3 class="item-price"><span class="price-value">{price}</span> <span class="currency">Ft</span></h3>
        <s class="old-price">999 000 Ft</s></div>
      <section class="cityname">Budapest, XI. kerület</section></div></div>"""


HA_CARDS = [ha_card(1, "ASUS RTX 3060 Ti Dual", "95 000 Ft"), ha_card(2, "Gigabyte RTX 3060 Ti Gaming OC", "55 000 Ft"),
            ha_card(3, "Keresek RTX 3060 Ti kártyát", "60 000 Ft"), ha_card(4, "MSI RTX 3080 Ventus", "185 000 Ft")]


class ParseTests(unittest.TestCase):
    def test_hardverapro_cards_without_the_header_and_seller_links(self):
        rows = parse_listings(ha_page(HA_CARDS), "https://hardverapro.hu/aprok/hardver/videokartya/index.html",
                              "hardverapro")
        self.assertEqual([(r["price"], r["url"].split("/")[4]) for r in rows],
                         [(95000, "asus_rtx_3060_ti_dual_1"), (55000, "gigabyte_rtx_3060_ti_gaming_oc_2"),
                          (60000, "keresek_rtx_3060_ti_kártyát_3"), (185000, "msi_rtx_3080_ventus_4")])
        self.assertIn("ASUS RTX 3060 Ti Dual", rows[0]["text"])
        self.assertIn("Budapest", rows[0]["text"])
        self.assertNotIn("auction", rows[0])

    def test_jofogas_cards_and_the_struck_old_price(self):
        page = "<html><body>" + jf_card(1, "iPhone 13 128GB", "185 000") + jf_card(2, "PS5 kontroller", "14 500") + \
            "</body></html>"
        rows = parse_listings(page, "https://www.jofogas.hu/magyarorszag?q=x", "jofogas")
        self.assertEqual([(r["text"].split(" 1")[0], r["price"]) for r in rows],
                         [("iPhone", 185000), ("PS5 kontroller", 14500)], "az áthúzott régi ár nem számít")
        self.assertTrue(rows[0]["url"].endswith("_120000001.htm"))

    def test_structured_data_when_the_cards_have_no_price(self):
        page = """<html><head><script type="application/ld+json">{"@context": "https://schema.org", "@type": "ItemList",
          "itemListElement": [{"@type": "ListItem", "position": 1, "item": {"@type": "Product", "name": "Makita DHP482",
          "url": "/budapest/makita_1.htm", "offers": {"@type": "Offer", "price": "24990", "priceCurrency": "HUF"}}}]}
          </script></head><body><div id="app"></div></body></html>"""
        rows = parse_listings(page, "https://www.jofogas.hu/magyarorszag?q=makita", "jofogas")
        self.assertEqual(rows, [{"text": "Makita DHP482", "price": 24990,
                                 "url": "https://www.jofogas.hu/budapest/makita_1.htm"}])
        self.assertEqual(parse_listings("<html><body><p>Nincs találat</p></body></html>", "https://x.hu/", ""), [])

    def test_listing_page_text_without_menus_and_hidden_parts(self):
        page = parse_page("""<html><head><title>RTX 3060 Ti eladó - HardverApró</title></head><body>
          <nav>Bejelentkezés</nav><main><h1>RTX 3060 Ti</h1><span class="badge">Eladva</span>
          <p>Hibátlan, dobozában. Csak személyes átvétel.</p><div style="display: none">Western Union</div>
          <button>Üzenet</button><a href="/tag/x.html">x</a></main></body></html>""", "https://hardverapro.hu/apro/a/friss.html")
        self.assertEqual(page["title"], "RTX 3060 Ti eladó - HardverApró")
        self.assertIn("Hibátlan, dobozában.", page["text"])
        self.assertNotIn("Western Union", page["text"], "a rejtett rész nem látszik")
        self.assertNotIn("Bejelentkezés", page["text"])
        self.assertIn("Eladva", page["short"])
        self.assertEqual(page["labels"], ["x"])


class FakeResponse:
    def __init__(self, url: str, status: int, body: str):
        self.url, self.status_code, self.encoding, self._body = url, status, "utf-8", body.encode("utf-8")

    def iter_content(self, size):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeNet:
    """A letöltések: webcím-részlet → (állapot, HTML); feljegyzi, mikor és melyik szálról kérték."""

    def __init__(self, clock: Clock):
        self.clock, self.pages, self.asked, self.threads, self.by_thread = clock, {}, [], set(), []
        self.running = self.most = 0
        self.delay = 0.0
        self._lock = threading.Lock()

    def __call__(self, url, headers, timeout):
        with self._lock:
            self.running += 1
            self.most = max(self.most, self.running)
            self.asked.append((self.clock(), url))
            self.threads.add(threading.current_thread().name)
            self.by_thread.append((threading.current_thread().name, url))
        try:
            time.sleep(self.delay)
            for part, (status, body) in self.pages.items():
                if part in url:
                    return FakeResponse(url, status, body)
            return FakeResponse(url, 404, "nincs")
        finally:
            with self._lock:
                self.running -= 1


class FetcherTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.net = FakeNet(self.clock)
        self.slept: list[float] = []

        def sleep(seconds):
            self.slept.append(seconds)
            self.clock.now += seconds

        self.fetcher = SiteFetcher(clock=self.clock, sleep=sleep, rng=random.Random(1), get=self.net)

    def test_one_request_at_a_time_with_a_pause_between(self):
        self.net.pages["videokartya"] = (200, ha_page(HA_CARDS))
        url = "https://hardverapro.hu/aprok/hardver/videokartya/index.html"
        self.assertEqual(len(self.fetcher.listings(url, "hardverapro")), 4)
        self.assertTrue(self.fetcher.ready("hardverapro") is False, "a következő kérés csak a szünet után jöhet")
        self.fetcher.listings(url, "hardverapro")
        first, second = self.net.asked[0][0], self.net.asked[1][0]
        self.assertGreaterEqual(second - first, 8, "két kérés között legalább 8 másodperc")
        self.assertTrue(self.fetcher.ready("jofogas"), "a másik oldal közben mehet")
        with self.assertRaises(FeedError) as caught:
            self.fetcher.listings("https://www.vinted.hu/catalog", "vinted")
        self.assertEqual(caught.exception.kind, "off", "a Vintedet nem így olvassa")
        strict = SiteFetcher(get=self.net, gap=(0, 0), domains={"hardverapro": ("hardverapro.hu",)})
        for wrong in ("http://192.168.1.1/apro/x/friss.html", "https://hardverapro.hu.gonosz.example/apro/x.html",
                      "file:///etc/passwd"):
            with self.assertRaises(FeedError, msg=wrong) as caught:
                strict.page(wrong, "hardverapro")
            self.assertEqual(caught.exception.kind, "off", "csak a saját domainjére megy")
        self.assertEqual(len(self.net.asked), 2)

    def test_a_block_stops_that_site_for_a_while_and_says_why(self):
        self.net.pages["jofogas"] = (403, "<html><body>Access denied</body></html>")
        with self.assertRaises(FeedError) as caught:
            self.fetcher.listings("https://www.jofogas.hu/magyarorszag?q=ps5", "jofogas")
        self.assertEqual(caught.exception.kind, "blocked")
        self.assertFalse(self.fetcher.usable("jofogas"))
        self.assertTrue(self.fetcher.status()["jofogas"]["blocked_until"])
        with self.assertRaises(FeedError):
            self.fetcher.listings("https://www.jofogas.hu/magyarorszag?q=ps5", "jofogas")
        self.assertEqual(len(self.net.asked), 1, "tiltás alatt meg sem próbálja")
        self.clock.now += 3601
        self.net.pages["jofogas"] = (200, "<html><body>" + jf_card(1, "PS5 kontroller", "14 000") + "</body></html>")
        self.assertEqual(len(self.fetcher.listings("https://www.jofogas.hu/magyarorszag?q=ps5", "jofogas")), 1)
        self.net.pages["jofogas"] = (200, "<html><title>Just a moment...</title><body>Checking your browser</body></html>")
        with self.assertRaises(FeedError):
            self.fetcher.listings("https://www.jofogas.hu/magyarorszag?q=ps5", "jofogas")
        self.assertFalse(self.fetcher.usable("jofogas"), "a robot-ellenőrző köztes oldal is tiltás")

    def test_pages_without_listings_send_it_back_to_the_browser(self):
        self.net.pages["keres"] = (200, "<html><body><div id='app'></div></body></html>")
        for _ in range(3):
            with self.assertRaises(FeedError) as caught:
                self.fetcher.listings("https://hardverapro.hu/aprok/keres.php?stext=x", "hardverapro")
            self.assertEqual(caught.exception.kind, "empty")
        self.assertFalse(self.fetcher.usable("hardverapro"), "háromszor semmi: egy ideig a böngésző nézi")
        self.assertTrue(self.fetcher.status()["hardverapro"]["browser_only"])

    def test_hourly_limit(self):
        self.net.pages["videokartya"] = (200, ha_page(HA_CARDS))
        fetcher = SiteFetcher(clock=self.clock, sleep=lambda s: setattr(self.clock, "now", self.clock.now + s),
                              get=self.net, gap=(0, 0), hourly=3)
        for _ in range(3):
            fetcher.listings("https://hardverapro.hu/aprok/hardver/videokartya/index.html", "hardverapro")
        with self.assertRaises(FeedError) as caught:
            fetcher.listings("https://hardverapro.hu/aprok/hardver/videokartya/index.html", "hardverapro")
        self.assertEqual(caught.exception.kind, "busy")

    def test_a_real_download_from_a_local_server(self):
        body = ha_page(HA_CARDS).encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")      # karakterkészlet nélkül: az oldal mondja meg
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        fetcher = SiteFetcher(gap=(0, 0))
        try:
            rows = fetcher.listings(f"http://127.0.0.1:{server.server_port}/aprok/hardver/videokartya/index.html",
                                    "hardverapro")
        finally:
            fetcher.close()
            server.shutdown()
            server.server_close()
        self.assertEqual(len(rows), 4)
        self.assertIn("kártyát", rows[2]["url"], "az ékezetek is jók (a meta charset szerint)")


class InstantCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.net = FakeNet(self.clock)
        self.fetcher = SiteFetcher(clock=self.clock, sleep=lambda s: setattr(self.clock, "now", self.clock.now + s),
                                   rng=random.Random(2), get=self.net, gap=(0, 0))
        self.browser = FakeBrowser()
        self.hunter = DealHunter(Path(self.tmp.name), PLATFORMS, lambda: self.browser, FakeLlm(), clock=self.clock,
                                 fetcher=self.fetcher)
        self.hunter.set_day(0, 24)
        self.watch = InstantWatch(Path(self.tmp.name), PLATFORMS, self.fetcher, self.hunter, clock=self.clock,
                                  rng=random.Random(3))
        self.notices: list[tuple] = []
        self.watch.notify = lambda kind, title, text, **extra: self.notices.append((kind, title, text, extra))

    def tearDown(self):
        self.tmp.cleanup()

    def results(self, cards: list[str], part: str = "keres.php") -> None:
        self.net.pages[part] = (200, ha_page(cards))

    def later(self, minutes: float = 4) -> None:
        self.clock.now += minutes * 60


class InstantTests(InstantCase):
    def test_remembers_what_is_there_then_tells_about_each_new_listing(self):
        self.watch.add("RTX 3060 Ti", ["hardverapro"])
        self.watch.set_enabled(True)
        self.results(HA_CARDS[:2])
        self.assertEqual(self.watch.tick(), 1)
        self.assertEqual(self.notices, [], "az első letöltés csak megjegyzi, ami már fent van")
        self.assertIn("elindult, 2 hirdetést már ismerek", self.hunter.journal.entries()[0]["text"])
        self.assertEqual(self.watch.tick(), 0, "még nem esedékes")
        self.later()
        self.results([ha_card(9, "Zotac RTX 3060 Ti Twin Edge", "99 000 Ft"), HA_CARDS[2], HA_CARDS[3]] + HA_CARDS[:2])
        self.assertEqual(self.watch.tick(), 1)
        [(kind, title, text, extra)] = self.notices
        self.assertEqual((kind, title), ("deal", "Új: Zotac RTX 3060 Ti Twin Edge"), "a kártya címe, a zaj nélkül")
        self.assertIn("Hardverapró: „Zotac RTX 3060 Ti Twin Edge", text)
        self.assertIn("99 000 Ft", text)
        self.assertTrue(extra["urgent"] and extra["url"].endswith("_9/friss.html"))
        self.assertEqual(extra["spoken"], "Új hirdetés: Zotac RTX 3060 Ti Twin Edge, 99 000 Ft.")
        self.assertEqual(len(self.watch.state()["hits"]), 1, "a kereső hirdetés és az RTX 3080 nem illik")
        self.later()
        self.assertEqual(self.watch.tick(), 1)
        self.assertEqual(len(self.notices), 1, "ugyanarról kétszer nem szól")

    def test_only_worth_it_uses_the_usual_price_and_checks_the_listing_page(self):
        self.hunter.add_search("RTX 3060 Ti", ["hardverapro"], reference=100000)
        self.hunter.set_enabled(True)
        self.watch.add("https://hardverapro.hu/aprok/hardver/videokartya/index.html")
        self.assertEqual(self.watch.state()["alerts"][0]["mode"], "cheap", "egy egész kategória: csak ami megéri")
        self.watch.set_enabled(True)
        self.results(HA_CARDS[:1], "videokartya")
        self.watch.tick()
        self.later()
        cheap = ha_card(7, "Palit RTX 3060 Ti Dual", "52 000 Ft")
        broken = ha_card(8, "EVGA RTX 3060 Ti XC", "50 000 Ft")
        self.results([cheap, broken, ha_card(6, "Inno3D RTX 3060 Ti", "97 000 Ft")] + HA_CARDS[:1], "videokartya")
        self.net.pages["evga_rtx_3060_ti_xc_8"] = (200, "<html><body><main><p>Nem indul, javításra szorul.</p></main>"
                                                       "</body></html>")
        self.net.pages["palit_rtx_3060_ti_dual_7"] = (200, "<html><body><main><p>Hibátlan, dobozában.</p></main>"
                                                          "</body></html>")
        self.watch.tick()
        [(_kind, title, text, _extra)] = self.notices
        self.assertTrue(title.startswith("Megéri: Palit RTX 3060 Ti Dual"))
        self.assertIn("a szokásos ára kb. 100 000 Ft, kb. 36 500 Ft haszon lehet", text)
        deals = self.hunter.state()["deals"]
        self.assertEqual([d["text"][:22] for d in deals], ["Palit RTX 3060 Ti Dual"], "a hibás nem jelölt")
        self.assertTrue(deals[0]["quick"])
        self.assertEqual(self.watch.state()["hits"][0]["deal"], deals[0]["id"], "a Megvettem gombhoz")
        self.assertEqual(self.browser.urls + self.browser.looked, [], "böngésző nélkül")

    def test_many_new_at_once_come_in_one_notification(self):
        self.watch.add("RTX", ["hardverapro"])
        self.watch.set_enabled(True)
        self.results(HA_CARDS[:1])
        self.watch.tick()
        self.later()
        self.results([ha_card(10 + n, f"RTX 40{n}0 Founders", f"{150 + n} 000 Ft") for n in range(5)] + HA_CARDS[:1])
        self.watch.tick()
        [(_kind, title, text, extra)] = self.notices
        self.assertEqual(title, "5 új hirdetés: RTX")
        self.assertEqual(text.count("•"), 5)
        self.assertEqual(extra["spoken"], "5 új hirdetés: RTX.")

    def test_a_price_drop_is_news_too(self):
        self.watch.add("RTX 3080", ["hardverapro"])
        self.watch.set_enabled(True)
        self.results(HA_CARDS[3:])
        self.watch.tick()
        self.later()
        self.results([ha_card(4, "MSI RTX 3080 Ventus", "160 000 Ft")])
        self.watch.tick()
        self.assertIn("leárazta: 185 000 Ft → 160 000 Ft", self.notices[0][2])

    def test_off_paused_evening_and_limits(self):
        self.watch.add("RTX 3060 Ti", ["hardverapro"])
        self.results(HA_CARDS)
        self.assertEqual(self.watch.tick(), 0, "alapból ki van kapcsolva")
        self.watch.set_enabled(True)
        self.watch.allowed = lambda: False
        self.assertEqual(self.watch.tick(), 0, "Szünet")
        self.watch.allowed = lambda: True
        self.hunter.set_day(0, 1) if time.localtime(self.clock()).tm_hour >= 1 else self.hunter.set_day(23, 24)
        self.assertEqual(self.watch.tick(), 0, "a gép üzemidején kívül")
        self.hunter.set_day(0, 24)
        alert = self.watch.state()["alerts"][0]
        self.watch.pause(alert["id"], True)
        self.assertEqual(self.watch.tick(), 0, "a szüneteltetett figyelő nem fut")
        self.watch.pause(alert["id"], False)
        self.assertEqual(self.watch.tick(), 1)
        with self.assertRaises(InstantError):
            self.watch.add("https://www.vinted.hu/catalog?search_text=nike")
        with self.assertRaises(InstantError):
            self.watch.add("RTX", min_price="50000", max_price="10000")
        with self.assertRaises(InstantError):
            self.watch.add("rtx 3060 ti", ["hardverapro"])       # ugyanez már megvan
        self.assertEqual(self.watch.remove(alert["id"])["alerts"], [])

    def test_a_block_is_reported_once(self):
        self.watch.add("RTX 3060 Ti", ["hardverapro", "jofogas"])
        self.watch.set_enabled(True)
        self.results(HA_CARDS)
        self.net.pages["jofogas"] = (429, "Too many requests")
        self.watch.tick()
        self.assertEqual([n[1] for n in self.notices], ["Jófogás: az azonnali figyelő szünetel"])
        self.assertIn("Jófogás: Az oldal robot-ellenőrzést kér", self.watch.state()["alerts"][0]["errors"]["jofogas"])
        self.assertNotIn("hardverapro", self.watch.state()["alerts"][0]["errors"], "a Hardverapró közben ment")
        sites = {site["key"]: site for site in self.watch.state()["sites"]}
        self.assertTrue(sites["jofogas"]["blocked_until"] and not sites["hardverapro"]["blocked_until"])
        self.later(70)
        self.watch.tick()
        self.assertEqual(len(self.notices), 1, "naponta egyszer szól")

    def test_the_thread_runs_on_its_own(self):
        self.watch.add("RTX 3060 Ti", ["hardverapro"])
        self.watch.set_enabled(True)
        self.results(HA_CARDS)
        self.watch.start()
        try:
            deadline = time.monotonic() + 10
            while not self.net.asked and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            self.watch.stop()
        self.assertEqual(self.net.threads, {"jarvis-azonnali"})


class ParallelRoundTests(InstantCase):
    def test_fast_sites_load_in_the_background_while_the_browser_works(self):
        """A kör elején a Hardverapró és a Jófogás találatai a háttérben töltődnek le (böngésző nélkül), a böngésző közben
        a Vintedet nézi; a jelölt hirdetés oldala is böngésző nélkül jön."""
        self.net.delay = 0.2
        self.browser.results = [{"text": f"RTX 3060 Ti {n}", "price": 100000 + n, "url": f"https://www.vinted.hu/items/{n}"}
                                for n in range(3)]
        self.results(HA_CARDS[:2])
        self.net.pages["jofogas"] = (200, "<html><body>" + jf_card(1, "RTX 3060 Ti Eagle", "98 000") + "</body></html>")
        self.net.pages["gigabyte_rtx_3060_ti_gaming_oc_2"] = (200, "<html><body><main>Hibátlan.</main></body></html>")
        self.hunter.add_search("RTX 3060 Ti", ["vinted", "jofogas", "hardverapro"], reference=100000)
        self.hunter.set_enabled(True)
        notices = []
        result = self.hunter.run(notify=lambda *a, **k: notices.append(a))
        self.assertTrue(any("vinted" in url for url in self.browser.urls))
        self.assertFalse(any("hardverapro" in url or "jofogas" in url for url in self.browser.urls),
                         "a gyors oldalakat nem a böngésző töltötte le")
        searched = [(name.split("_")[0], url) for name, url in self.net.by_thread if "friss.html" not in url]
        self.assertEqual({name for name, _url in searched}, {"jarvis-gyors"}, "a találati oldalak a háttérben")
        self.assertEqual(len(searched), 2)
        self.assertEqual(self.browser.looked, [], "a jelölt oldala is böngésző nélkül")
        self.assertIn("1 olcsó hirdetés", result)
        self.assertIn("Olcsó: Gigabyte RTX 3060 Ti Gaming OC", notices[0][1])

    def test_falls_back_to_the_browser_when_the_site_blocks(self):
        self.net.pages["keres.php"] = (403, "tiltva")
        self.hunter.add_search("RTX 3060 Ti", ["hardverapro"], reference=100000)
        self.hunter.set_enabled(True)
        self.hunter.run()
        self.assertTrue(any("hardverapro" in url for url in self.browser.urls), "tiltásnál a böngésző nézi meg")
        self.assertFalse(self.fetcher.usable("hardverapro"))


if __name__ == "__main__":
    unittest.main()
