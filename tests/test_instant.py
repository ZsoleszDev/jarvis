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
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_deals import Clock, FakeBrowser, FakeLlm  # noqa: E402

from jarvis_core import deals as deals_module  # noqa: E402
from jarvis_core.deals import DealHunter  # noqa: E402
from jarvis_core.feeds import FeedError, SiteFetcher, fetch_image, parse_listings, parse_page, public_host  # noqa: E402
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

    def test_the_time_before_the_price_is_not_part_of_it(self):
        """A valódi Hardverapró-kártyán az idő az ár előtt is lehet: a „ma 12:54 997 Ft” ára 997 Ft, nem 54 997 (az ármezőből,
        és ha nincs ilyen, a szövegből is)."""
        cards = """<ul><li class="media"><a href="https://hardverapro.hu/apro/rtx_1/friss.html" title="RTX 3060">RTX 3060</a>
          <div class="uad-ultralight">ma 12:54</div><div class="uad-price">997 Ft</div></li>
          <li class="media"><a href="https://hardverapro.hu/apro/rtx_2/friss.html" title="RTX 3070">RTX 3070</a>
          <span>tegnap 9:45</span> <span>500 Ft</span></li>
          <li class="media"><a href="https://hardverapro.hu/apro/rtx_3/friss.html" title="RTX 3080">RTX 3080</a>
          <span>ma 13:05</span> <span>45 500 Ft</span></li></ul>"""
        rows = parse_listings(ha_page([cards]), "https://hardverapro.hu/aprok/hardver/index.html", "hardverapro")
        self.assertEqual([r["price"] for r in rows], [997, 500, 45500])

    def test_jofogas_cards_and_the_struck_old_price(self):
        page = "<html><body>" + jf_card(1, "iPhone 13 128GB", "185 000") + jf_card(2, "PS5 kontroller", "14 500") + \
            "</body></html>"
        rows = parse_listings(page, "https://www.jofogas.hu/magyarorszag?q=x", "jofogas")
        self.assertEqual([(r["text"].split(" 1")[0], r["price"]) for r in rows],
                         [("iPhone", 185000), ("PS5 kontroller", 14500)], "az áthúzott régi ár nem számít")
        self.assertTrue(rows[0]["url"].endswith("_120000001.htm"))

    def test_promoted_cards_are_marked(self):
        """A fizetett kiemelések jelölve: a kártya osztálya („highlighted”, „kiemelt”…), egy rövid címke („Kiemelt”,
        „Előresorolt”), vagy a kiemeltek blokkja (egy „Kiemelt hirdetések” fejléc). Ha az oldal nagyobbik része ilyen,
        az nem kiemelés (pl. minden kártya „premium” osztályú), és egyik sem jelölt."""
        top = '<div class="featured-block"><h4>Kiemelt hirdetések</h4><ul>' + ha_card(1, "Régi kártya", "90 000 Ft") + \
            "</ul></div>"
        badge = ha_card(2, "Felhúzott kártya", "80 000 Ft").replace('<div class="media-body">',
                                                                  '<div class="media-body"><span>Előresorolt</span>')
        marked = ha_card(3, "Kiemelt osztályú", "70 000 Ft").replace('class="media"', 'class="media uad-kiemelt"')
        plain = [ha_card(10 + n, f"Sima kártya {n}", "10 000 Ft") for n in range(4)]
        rows = parse_listings(ha_page([top, badge, marked] + plain), "https://hardverapro.hu/aprok/x/index.html",
                              "hardverapro")
        self.assertEqual([bool(r.get("promoted")) for r in rows], [True] * 3 + [False] * 4)
        everywhere = [card.replace('class="media"', 'class="media premium"') for card in plain]
        rows = parse_listings(ha_page(everywhere), "https://hardverapro.hu/aprok/x/index.html", "hardverapro")
        self.assertFalse(any(r.get("promoted") for r in rows))

    def test_the_newest_order_from_a_select_and_a_chosen_order_stays(self):
        from jarvis_core.feeds import chosen_sort, newest_link, parse
        page = """<html><body><form><select name="order"><option value="relevance" selected>Relevancia</option>
          <option value="refreshed">Frissítve</option><option value="time">Feladás ideje (legújabb elöl)</option></select>
          </form></body></html>"""
        self.assertEqual(newest_link(parse(page), "https://www.vatera.hu/listings/index.php?q=ps5&order=relevance"),
                         "https://www.vatera.hu/listings/index.php?q=ps5&order=time")
        self.assertTrue(chosen_sort("https://www.vatera.hu/listings/index.php?c=1&ob=ending"))
        self.assertFalse(chosen_sort("https://hardverapro.hu/aprok/keres.php?stext=rtx"))

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

    def test_listing_photos_for_the_check(self):
        """A hirdetés fotói (a képellenőrzéshez): a megosztáskor mutatott kép, majd a fő tartalom képei, a logók,
        ikonok, a kis képek és a rejtettek nélkül, teljes webcímmel."""
        page = parse_page("""<html><head><meta property="og:image" content="https://cdn.example/1-nagy.jpg"></head>
          <body><header><img src="/logo.png"></header><main>
          <img src="https://cdn.example/1-nagy.jpg"><img data-src="/kep/2.jpg" src="data:image/gif;base64,R0">
          <img src="/kep/ikon-avatar.jpg"><img src="/kep/kicsi.jpg" width="40" height="40">
          <img src="/kep/rejtett.jpg" style="display:none"><img src="/kep/3.jpg"><img src="/kep/4.jpg">
          <img src="/kep/5.jpg"></main></body></html>""", "https://hardverapro.hu/apro/a/friss.html")
        self.assertEqual(page["images"], ["https://cdn.example/1-nagy.jpg", "https://hardverapro.hu/kep/2.jpg",
                                          "https://hardverapro.hu/kep/3.jpg", "https://hardverapro.hu/kep/4.jpg"])

    def test_a_photo_is_only_fetched_from_a_public_address(self):
        class Photo:
            def __init__(self, kind="image/jpeg", status=200, body=b"\xff\xd8jpeg"):
                self.status_code, self.headers, self._body = status, {"Content-Type": kind}, body

            def iter_content(self, size):
                yield self._body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        asked = []
        get = lambda url, **kw: asked.append(url) or Photo()          # noqa: E731
        self.assertEqual(fetch_image("https://cdn.example/1.jpg", get), b"\xff\xd8jpeg")
        for url in ("http://127.0.0.1/a.jpg", "http://192.168.1.10/a.jpg", "http://localhost/a.jpg",
                    "file:///etc/passwd", "http://router.lan/a.jpg", "http://[::1]/a.jpg"):
            self.assertIsNone(fetch_image(url, get), url)
        self.assertEqual(asked, ["https://cdn.example/1.jpg"], "a helyi és belső címekhez nem is kérdez")
        self.assertIsNone(fetch_image("https://cdn.example/oldal", lambda url, **kw: Photo("text/html")), "csak kép")
        self.assertIsNone(fetch_image("https://cdn.example/1.jpg", lambda url, **kw: Photo(status=404)))
        self.assertTrue(public_host("8.8.8.8") and not public_host("10.0.0.1") and public_host("cdn.example"))


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
        self.assertGreaterEqual(second - first, 6, "két kérés között legalább 6 másodperc")
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

    def test_a_fresh_search_page_is_not_downloaded_twice(self):
        """A kereskedés köre a gyors oldalak találati oldalát nem kéri le újra, ha az azonnali figyelő pár perce már
        letöltötte (max_age); az azonnali figyelő mindig frisset kér."""
        self.net.pages["videokartya"] = (200, ha_page(HA_CARDS))
        url = "https://hardverapro.hu/aprok/hardver/videokartya/index.html"
        self.fetcher.listings(url, "hardverapro")
        self.assertEqual(len(self.fetcher.listings(url, "hardverapro", max_age=300)), 4)
        self.assertEqual(len(self.net.asked), 1, "a friss oldalt nem kérte le újra")
        self.clock.now += 301
        self.fetcher.listings(url, "hardverapro", max_age=300)
        self.assertEqual(len(self.net.asked), 2, "a régebbit igen")
        self.fetcher.listings(url, "hardverapro")
        self.assertEqual(len(self.net.asked), 3, "max_age nélkül mindig frisset")

    def test_it_slows_down_when_the_site_answers_slowly(self):
        """Ha az oldal lassan válaszol (terhelt, vagy fékez), a szünetek hosszabbak lesznek; a gyors válaszokkal
        fokozatosan visszaállnak."""
        self.net.pages["videokartya"] = (200, ha_page(HA_CARDS))
        url = "https://hardverapro.hu/aprok/hardver/videokartya/index.html"
        fetcher = SiteFetcher(clock=self.clock, sleep=lambda s: setattr(self.clock, "now", self.clock.now + s),
                              rng=random.Random(1), get=self.net, gap=(10, 10))
        with mock.patch("jarvis_core.feeds.SLOW_RESPONSE", 0.0):   # minden válasz „lassú”
            fetcher.listings(url, "hardverapro")
            fetcher.listings(url, "hardverapro")
        self.assertEqual(fetcher.status()["hardverapro"]["slow"], 2.25)
        start = self.clock.now
        fetcher.listings(url, "hardverapro")
        self.assertGreaterEqual(self.net.asked[-1][0] - self.net.asked[-2][0], 22, "a szünet 10 helyett 22,5 mp")
        self.assertLess(fetcher.status()["hardverapro"]["slow"], 2.25, "a gyors válasz után enyhít")
        self.assertGreater(self.clock.now, start)

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


def page_html(text: str) -> tuple[int, str]:
    return 200, f"<html><body><main><p>{text}</p></main></body></html>"


class InstantTests(InstantCase):
    def test_tells_only_when_a_new_listing_is_worth_buying(self):
        """Az első letöltés csak megjegyzi, ami fent van (a régi olcsóról sem szól); utána az új hirdetések közül csak a
        megérőről szól: a hirdetés oldalát is átnézi, és egy rövid modellkérdés után."""
        self.hunter.add_search("RTX 3060 Ti", ["hardverapro"], reference=100000)
        self.hunter.set_enabled(True)
        self.watch.add("RTX 3060 Ti", ["hardverapro"])
        self.watch.set_enabled(True)
        self.results(HA_CARDS[:2])               # a 55 000-es Gigabyte már fent volt: arról nem szól
        self.assertEqual(self.watch.tick(), 1)
        self.assertEqual(self.notices, [])
        self.assertIn("innentől minden újat felmérek", self.hunter.journal.entries()[0]["text"])
        self.assertEqual(self.watch.tick(), 0, "még nem esedékes")
        self.later()
        self.results([ha_card(9, "Zotac RTX 3060 Ti Twin Edge", "99 000 Ft"), ha_card(7, "Palit RTX 3060 Ti Dual", "52 000 Ft"),
                      ha_card(8, "EVGA RTX 3060 Ti XC", "50 000 Ft")] + HA_CARDS[:2])
        self.net.pages["_8/friss"] = page_html("Nem indul, javításra szorul.")
        self.net.pages["_7/friss"] = page_html("Hibátlan, dobozában, számlával.")
        self.assertEqual(self.watch.tick(), 1)
        [(kind, title, text, extra)] = self.notices
        self.assertEqual((kind, title), ("deal", "Megéri: Palit RTX 3060 Ti Dual"),
                         "a drága Zotacról és a hibás EVGA-ról nem szól")
        self.assertIn("Hardverapró: „Palit RTX 3060 Ti Dual” 52 000 Ft, most került fel. A szokásos ára kb. 100 000 Ft", text)
        self.assertIn("kb. 36 500 Ft haszon lehet. Biztos befektetés: 70% haszon. Ugyanaz a kártya.", text,
                      "mennyire biztos, és a modell is megnézte")
        self.assertTrue(extra["urgent"] and extra["url"].endswith("_7/friss.html"))
        [hit] = self.watch.state()["hits"]
        deals = self.hunter.state()["deals"]
        self.assertEqual(([d["text"][:22] for d in deals], hit["deal"]), (["Palit RTX 3060 Ti Dual"], deals[0]["id"]),
                         "a találatok közé is bekerül (Megvettem gomb)")
        self.assertEqual(self.browser.urls + self.browser.looked, [], "böngésző nélkül")
        self.later()
        self.watch.tick()
        self.assertEqual(len(self.notices), 1, "ugyanarról kétszer nem szól")

    def test_the_model_names_what_it_does_not_know_and_checks_the_price_elsewhere(self):
        """Amit az árbázis a címből nem ismer fel, azt a modell nevezi meg (egy hívás az egészre). Ha így ismert, ahhoz
        méri; ha nem, megnézi, mennyiért adják a többi oldalon (akkor is, ha a modell szerint nem érné meg)."""
        self.hunter.book.add([{"text": f"Sony WH-1000XM4 fejhallgató {n}", "price": 70000 + n * 500,
                               "url": f"https://www.jofogas.hu/b/sony_{n}.htm", "platform": ("jofogas", "hardverapro")[n % 2]}
                              for n in range(5)])   # két oldalról: biztos ár, máshol nem kell megnézni
        self.watch.add("https://hardverapro.hu/aprok/hang/fejhallgato/index.html")
        self.watch.set_enabled(True)
        self.results([ha_card(1, "Régi fülhallgató", "2 000 Ft")], "fejhallgato/index")
        self.watch.tick()
        self.later()
        self.results([ha_card(31, "Eladó zajszűrős Sony fejhallgató, alig használt", "30 000 Ft"),
                      ha_card(32, "Bose fejhallgató szép állapotban", "40 000 Ft"),
                      ha_card(33, "Valami kábelcsomag", "3 000 Ft"), ha_card(1, "Régi fülhallgató", "2 000 Ft")],
                     "fejhallgato/index")
        self.net.pages["_31/friss"] = page_html("Hibátlan, dobozában.")
        self.net.pages["_32/friss"] = page_html("Szép állapot, működik.")
        others = "".join(jf_card(40 + n, f"Bose QuietComfort 45 fejhallgató {n}", f"{78 + n} 000") for n in range(5))
        self.net.pages["q=Bose"] = (200, f"<html><body>{others}</body></html>")
        self.net.pages["stext=Bose"] = (200, ha_page([ha_card(50, "Bose QuietComfort 45 fejhallgató", "80 000 Ft")]))
        self.hunter.llm.explore = [{"n": 1, "query": "Sony WH-1000XM4", "buy": False},
                                   {"n": 2, "query": "Bose QuietComfort 45", "buy": False}]   # a modell nem tudja
        self.watch.tick()
        titles = [n[1] for n in self.notices]
        self.assertEqual(titles, ["Megéri: Eladó zajszűrős Sony fejhallgató, alig használt",
                                  "Megéri: Bose fejhallgató szép állapotban"])
        self.assertIn("A szokásos ára kb. 71 000 Ft, 5 hasonló alapján, Hardverapró és Jófogás", self.notices[0][2],
                      "az árbázisból")
        self.assertIn("A szokásos ára kb. 80 000 Ft, 6 hasonló alapján, Hardverapró és Jófogás", self.notices[1][2],
                      "a két oldal áraiból")
        explore_calls = [c for c in self.hunter.llm.calls if "Friss hirdetések" in c[1]["content"]]
        self.assertEqual(len(explore_calls), 1, "egy modellhívás a megnevezésre")
        self.assertNotIn("kábelcsomag", explore_calls[0][1]["content"], "a csomag a modellhez sem jut el")
        searched = [url for _t, url in self.net.asked if "Bose" in url]
        self.assertEqual(len(searched), 2, "a Bose árát mindkét oldalon megnézte (böngésző nélkül), a Sonyét nem kellett")

    def test_a_price_seen_on_one_site_only_is_checked_on_the_other_one(self):
        """Az árbázis a Sonyt csak a Jófogásról ismeri (5 ár): mielőtt szólna, a két oldalon is megnézi (egyszerre).
        Ha így sincs elég hasonló, nem szól (a naplóba beírja); ha van, és ott is jóval drágább, szól."""
        self.hunter.book.add([{"text": f"Sony WH-1000XM4 fejhallgató {n}", "price": 70000 + n * 500,
                               "url": f"https://www.jofogas.hu/b/sony_{n}.htm", "platform": "jofogas"} for n in range(5)])
        self.watch.add("https://hardverapro.hu/aprok/hang/fejhallgato/index.html")
        self.watch.set_enabled(True)
        self.results([ha_card(1, "Régi fülhallgató", "2 000 Ft")], "fejhallgato/index")
        self.watch.tick()
        self.later()
        self.results([ha_card(31, "Sony WH-1000XM4 fejhallgató", "30 000 Ft"), ha_card(1, "Régi fülhallgató", "2 000 Ft")],
                     "fejhallgato/index")
        self.net.pages["_31/friss"] = page_html("Hibátlan, dobozában.")
        self.watch.tick()
        self.assertEqual(self.notices, [])
        searched = [url for _t, url in self.net.asked if "sony" in url.lower() and "friss" not in url]
        self.assertEqual(len(searched), 2, "mindkét oldalon megnézte")
        self.assertIn("csak 5 hasonló árat találtam (Jófogás)",
                      " ".join(e["text"] for e in self.hunter.journal.entries() if e["text"].startswith("Nem biztos")))
        self.later()
        self.results([ha_card(32, "Sony WH-1000XM4 fejhallgató fekete", "31 000 Ft"),
                      ha_card(31, "Sony WH-1000XM4 fejhallgató", "30 000 Ft")], "fejhallgato/index")
        self.net.pages["_32/friss"] = page_html("Hibátlan, dobozában.")
        self.net.pages["stext=Sony"] = (200, ha_page([ha_card(60 + n, f"Sony WH-1000XM4 {n}", f"{69 + n} 000 Ft")
                                                      for n in range(3)]))
        self.watch.tick()
        [(_kind, title, text, _extra)] = self.notices
        self.assertEqual(title, "Megéri: Sony WH-1000XM4 fejhallgató fekete")
        self.assertIn("A szokásos ára kb. 70 800 Ft, 8 hasonló alapján, Hardverapró és Jófogás", text)

    def test_candidates_beyond_the_round_are_kept_for_later(self):
        """Az ár-ellenőrzésre váró jelöltekből egy körben EXPLORE_PICKS (3) kerül sorra; a többi nem vész el (eddig a
        figyelő látottnak jegyezte, és többé nem jött elő), hanem a következő körben jön."""
        for n in range(1, 6):
            self.hunter.book.add([{"text": f"Makita DHP48{n} fúró {k}", "price": 40000 + k * 500,
                                   "url": f"https://www.jofogas.hu/b/makita{n}_{k}.htm", "platform": "jofogas"}
                                  for k in range(5)])
        self.watch.add("https://hardverapro.hu/aprok/szerszam/index.html")
        self.watch.set_enabled(True)
        self.results([ha_card(1, "Régi fúró", "2 000 Ft")], "szerszam/index")
        self.watch.tick()
        self.later()
        self.results([ha_card(40 + n, f"Makita DHP48{n} fúró", "20 000 Ft") for n in range(1, 6)], "szerszam/index")
        self.watch.tick()

        def unsure() -> int:
            return len([e for e in self.hunter.journal.entries() if e["text"].startswith("Nem biztos")])

        self.assertEqual((unsure(), self.watch.state()["waiting"]), (3, 2), "három most, kettő vár")
        self.watch.tick()
        self.assertEqual((unsure(), self.watch.state()["waiting"]), (5, 0), "a maradék is sorra került")

    def test_a_full_page_of_new_listings_reads_the_next_page_too(self):
        """Ha két letöltés között több új hirdetés jött, mint amennyi egy oldalon elfér (csupa új az oldal), a következő
        oldalt is megnézi, ahogy az oldal linkeli (a paraméterét nem kell kitalálni); ha van ismert hirdetés az oldalon,
        nem."""
        url = "https://hardverapro.hu/aprok/hardver/videokartya/index.html"
        alert = self.watch.add(url)["alerts"][0]
        self.watch.set_enabled(True)
        self.results(HA_CARDS[:1], "videokartya/index.html")
        self.watch.tick()
        self.later()
        first = ha_page([ha_card(100 + n, f"Videókártya {n}", "1 000 Ft") for n in range(12)]).replace(
            "<head>", '<head><link rel="next" href="/aprok/hardver/videokartya/index.html?offset=12">')
        second = ha_page([ha_card(200 + n, f"Videókártya {n}", "1 000 Ft") for n in range(3)] + HA_CARDS[:1])
        self.net.pages = {"index.html?offset=12": (200, second), "videokartya/index.html": (200, first)}
        self.watch.tick()
        self.assertEqual(len([u for _t, u in self.net.asked if u.endswith("?offset=12")]), 1, "a következő oldal is")
        seen = self.watch._data["seen"][alert["id"]]
        self.assertEqual(len(seen), 1 + 12 + 3, "a második oldal hirdetéseit is látottnak jegyezte")
        self.later()
        self.net.pages = {"videokartya/index.html": (200, first)}
        asked = len(self.net.asked)
        self.watch.tick()
        self.assertEqual(len(self.net.asked), asked + 1, "ismert hirdetések: nem lapoz tovább")

    def test_promoted_listings_do_not_hide_a_full_page_of_new_ones(self):
        """A kiemelt hirdetések (elöl, mindig ugyanazok, tehát ismertek) nem akadályozzák, hogy észrevegye: az oldal alja
        is csupa új, lehet, hogy lemaradt valami (eddig csak a teljesen új oldal számított, ilyen pedig a kiemeltek miatt
        nem volt). Az új kiemelt hirdetés a többi új után kerül sorra."""
        url = "https://hardverapro.hu/aprok/hardver/videokartya/index.html"
        self.watch.add(url)
        self.watch.set_enabled(True)
        top = [ha_card(10 + n, f"Kiemelt kártya {n}", "90 000 Ft").replace('class="media"', 'class="media highlighted"')
               for n in range(3)]
        self.results(top + HA_CARDS[:1], "videokartya/index.html")
        self.watch.tick()
        self.later()
        promo = ha_card(19, "Új kiemelt kártya", "90 000 Ft").replace('class="media"', 'class="media highlighted"')
        first = ha_page([promo] + top + [ha_card(100 + n, f"Videókártya {n}", "1 000 Ft") for n in range(12)]).replace(
            "<head>", '<head><link rel="next" href="/aprok/hardver/videokartya/index.html?offset=16">')
        second = ha_page(top + [ha_card(200 + n, f"Videókártya {n}", "1 000 Ft") for n in range(3)] + HA_CARDS[:1])
        self.net.pages = {"index.html?offset=16": (200, second), "videokartya/index.html": (200, first)}
        fresh = []
        self.hunter.appraise = lambda rows, *args, **kwargs: fresh.extend(rows) or []
        self.watch.tick()
        self.assertEqual(len([u for _t, u in self.net.asked if u.endswith("?offset=16")]), 1, "a következő oldal is")
        self.assertEqual(fresh[-1]["title"], "Új kiemelt kártya", "a kiemelt a többi új után")

    def test_the_whole_page_is_read(self):
        """A Hardverapró egy oldalon 100 hirdetést mutat (az elején többnyire fizetett kiemelésekkel): az egész oldalt nézi,
        nem csak az elejét; egy új hirdetés a 80. helyen is sorra kerül (eddig csak az első 30 számított)."""
        url = "https://hardverapro.hu/aprok/hardver/videokartya/index.html"
        alert = self.watch.add(url)["alerts"][0]
        self.watch.set_enabled(True)
        cards = [ha_card(300 + n, f"Videókártya {n}", f"{20 + n} 000 Ft") for n in range(100)]
        self.results(cards, "videokartya/index.html")
        self.watch.tick()
        self.assertEqual(len(self.watch._data["seen"][alert["id"]]), 100, "az első letöltés az egész oldalt megjegyzi")
        self.later()
        self.results(cards[:79] + [ha_card(999, "Új videókártya", "50 000 Ft")] + cards[79:99], "videokartya/index.html")
        fresh = []
        self.hunter.appraise = lambda rows, *args, **kwargs: fresh.extend(rows) or []
        self.watch.tick()
        self.assertEqual([r["title"] for r in fresh], ["Új videókártya"])

    def test_the_newest_first_order_when_the_page_offers_it(self):
        """Ha az oldal kínál „legújabb elöl” rendezést (egy linkkel, ugyanazon az oldalon), onnantól azzal tölt; a váltás
        után egyszer megint csak megjegyzi az oldalt (különben a más sorrend miatt minden újnak látszana)."""
        url = "https://hardverapro.hu/aprok/hardver/videokartya/index.html"
        alert = self.watch.add(url)["alerts"][0]
        self.watch.set_enabled(True)
        sorting = '<div class="sort"><a href="?order=frissitve">Frissítettek</a> <a href="?order=uj">Legújabbak</a></div>'
        bumped = ha_page([sorting] + [ha_card(400 + n, f"Kiemelt kártya {n}", "90 000 Ft") for n in range(3)])
        newest = ha_page([sorting] + [ha_card(500 + n, f"Friss kártya {n}", "40 000 Ft") for n in range(3)])
        self.net.pages = {"index.html?order=uj": (200, newest), "videokartya/index.html": (200, bumped)}
        self.watch.tick()
        self.later()
        self.watch.tick()
        self.assertTrue(self.net.asked[-1][1].endswith("index.html?order=uj"), "a legújabb szerint rendezve")
        self.assertIn("a legújabb hirdetések szerint rendezve nézem", self.hunter.journal.entries()[0]["text"])
        self.assertEqual(self.notices, [], "a sorrend váltása nem új hirdetés")
        self.assertEqual(len(self.watch._data["seen"][alert["id"]]), 6)

    def test_the_evaluation_waits_while_paused(self):
        """Szünet alatt (vagy ha a háttérmunka tiltva van) az értékelő szál nem dolgozik: a feladat megmarad, nem kérdezi
        a modellt, nem nyitja meg a hirdetést, és nem szól; utána jön. Egy szüneteltetett figyelő hirdetései a
        várakozók közé kerülnek, és a folytatásakor jönnek."""
        self.hunter.add_search("RTX 3060 Ti", ["hardverapro"], reference=100000)
        self.hunter.set_enabled(True)
        self.watch.add("RTX 3060 Ti", ["hardverapro"])
        self.watch.set_enabled(True)
        self.results(HA_CARDS[:2])
        self.watch.tick()
        self.later()
        self.results([ha_card(7, "Palit RTX 3060 Ti Dual", "52 000 Ft")] + HA_CARDS[:2])
        self.net.pages["_7/friss"] = page_html("Hibátlan.")
        alert = self.watch.state()["alerts"][0]
        allowed = {"now": False}
        self.watch.allowed = lambda: allowed["now"]
        self.watch.start_worker()
        try:
            self.watch.poll(alert, "hardverapro")
            time.sleep(0.5)
            self.assertEqual((self.notices, self.hunter.llm.calls), ([], []))
            self.assertFalse([u for _t, u in self.net.asked if u.endswith("_7/friss.html")], "az oldalát sem nyitotta meg")
            allowed["now"] = True
            deadline = time.monotonic() + 5
            while not self.notices and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual([n[1] for n in self.notices], ["Megéri: Palit RTX 3060 Ti Dual"], "a Szünet után jön")

            self.notices.clear()
            self.later()
            self.results([ha_card(8, "Zotac RTX 3060 Ti", "50 000 Ft")] + HA_CARDS[:2])
            self.net.pages["_8/friss"] = page_html("Hibátlan.")
            self.watch.pause(alert["id"], True)
            self.watch.poll(alert, "hardverapro")
            deadline = time.monotonic() + 5
            while not self.watch.state()["waiting"] and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual((self.watch.state()["waiting"], self.notices), (1, []), "a szüneteltetett figyelőé vár")
            self.watch.pause(alert["id"], False)
            self.watch.tick()
            deadline = time.monotonic() + 5
            while not self.notices and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual([n[1] for n in self.notices], ["Megéri: Zotac RTX 3060 Ti"], "a folytatásakor jön")
        finally:
            self.watch.stop()

    def test_the_evaluation_runs_beside_the_downloads(self):
        """Az új hirdetések értékelése (a hirdetés oldala, a modellkérdés) a saját szálán megy: a letöltés szála csak
        beteszi őket, és mehet tovább."""
        self.hunter.add_search("RTX 3060 Ti", ["hardverapro"], reference=100000)
        self.hunter.set_enabled(True)
        self.watch.add("RTX 3060 Ti", ["hardverapro"])
        self.watch.set_enabled(True)
        self.results(HA_CARDS[:2])
        self.watch.tick()
        self.later()
        self.results([ha_card(7, "Palit RTX 3060 Ti Dual", "52 000 Ft")] + HA_CARDS[:2])
        self.net.pages["_7/friss"] = page_html("Hibátlan.")
        self.watch.start_worker()
        try:
            self.assertEqual(self.watch.tick(), 1)
            deadline = time.monotonic() + 5
            while not self.notices and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual([n[1] for n in self.notices], ["Megéri: Palit RTX 3060 Ti Dual"])
            page = [thread for thread, url in self.net.by_thread if url.endswith("_7/friss.html")]
            self.assertEqual(page, ["jarvis-azonnali-ertekeles"], "a hirdetés oldalát az értékelő szál nézte meg")
        finally:
            self.watch.stop()

    def test_waits_for_the_model_while_you_use_jarvis(self):
        """Amíg dolgozol a Jarvisszal, a modellt nem kérdezi: a modell nélkül is felmérhető megérőről rögtön szól (a
        rövid modellkérdés nélkül), a többi vár, és utána jön."""
        self.hunter.add_search("RTX 3060 Ti", ["hardverapro"], reference=100000)
        self.hunter.set_enabled(True)
        self.watch.add("https://hardverapro.hu/aprok/hardver/videokartya/index.html")
        self.watch.set_enabled(True)
        self.results(HA_CARDS[:1], "videokartya")
        self.watch.tick()
        self.later()
        self.watch.model_ok = lambda: False
        self.results([ha_card(7, "Palit RTX 3060 Ti Dual", "52 000 Ft"), ha_card(31, "Zajszűrős fejhallgató", "30 000 Ft")]
                     + HA_CARDS[:1], "videokartya")
        self.net.pages["_7/friss"] = page_html("Hibátlan.")
        self.net.pages["_31/friss"] = page_html("Hibátlan.")
        self.watch.tick()
        self.assertEqual([n[1] for n in self.notices], ["Megéri: Palit RTX 3060 Ti Dual"])
        self.assertNotIn("Ugyanaz a kártya", self.notices[0][2], "most nem kérdezte a modellt")
        self.assertEqual(self.watch.state()["waiting"], 1)
        self.assertEqual(self.hunter.llm.calls, [])
        self.hunter.book.add([{"text": f"Sony WH-1000XM4 {n}", "price": 70000, "url": f"https://x.hu/s{n}",
                               "platform": ("jofogas", "hardverapro")[n % 2]} for n in range(5)])
        self.hunter.llm.explore = [{"n": 1, "query": "Sony WH-1000XM4", "buy": False}]
        self.watch.model_ok = lambda: True
        self.watch.tick()
        self.assertEqual(self.watch.state()["waiting"], 0)
        self.assertEqual(self.notices[-1][1], "Megéri: Zajszűrős fejhallgató")

    def test_suspicious_new_listings_are_never_sent(self):
        """A gyanúsan olcsóról (az oldalát sem tölti le), az átverésre utalóról és az értékelés nélküli eladóéról nem
        szól, a találatok közé sem kerül; a naplóba beírja, miért hagyta ki."""
        self.hunter.add_search("RTX 3060 Ti", ["hardverapro"], reference=100000)
        self.hunter.set_enabled(True)
        self.watch.add("RTX 3060 Ti", ["hardverapro"])
        self.watch.set_enabled(True)
        self.results(HA_CARDS[:1])
        self.watch.tick()
        self.later()
        self.results([ha_card(5, "Zotac RTX 3060 Ti Twin Edge", "25 000 Ft"), ha_card(6, "PNY RTX 3060 Ti", "50 000 Ft"),
                      ha_card(7, "Palit RTX 3060 Ti Dual", "52 000 Ft")] + HA_CARDS[:1])
        self.net.pages["_6/friss"] = page_html("Hibátlan. Csak előre utalással, külföldről postázom.")
        self.net.pages["_7/friss"] = page_html("Hibátlan, írj Telegramon.")
        self.watch.tick()
        self.assertEqual(self.notices, [])
        self.assertEqual((self.watch.state()["hits"], self.hunter.state()["deals"]), ([], []))
        self.assertNotIn("_5/friss", " ".join(url for _t, url in self.net.asked), "a gyanúsan olcsó oldalát sem nézi")
        said = [e["text"] for e in self.hunter.journal.entries() if e["text"].startswith("Gyanús")]
        self.assertEqual(len(said), 3)
        for title, why in (("Zotac", "gyanúsan olcsó"), ("PNY", "átverésre utaló jel"),
                           ("Palit", "az oldalon kívüli elérhetőséget ad meg")):
            self.assertIn(why, next(line for line in said if title in line))

    def test_the_buy_limit(self):
        """A vételi limit fölött nem szól, és a modellhez sem küldi."""
        self.hunter.add_search("RTX 3060 Ti", ["hardverapro"], reference=100000)
        self.hunter.set_enabled(True)
        self.hunter.set_limits(max_buy="50 000")
        self.watch.add("https://hardverapro.hu/aprok/hardver/videokartya/index.html")
        self.watch.set_enabled(True)
        self.results(HA_CARDS[:1], "videokartya")
        self.watch.tick()
        self.later()
        self.results([ha_card(7, "Palit RTX 3060 Ti Dual", "52 000 Ft"), ha_card(8, "PNY RTX 3060 Ti", "48 000 Ft"),
                      ha_card(31, "Profi stúdió fejhallgató", "150 000 Ft")] + HA_CARDS[:1], "videokartya")
        self.net.pages["_8/friss"] = page_html("Hibátlan.")
        self.watch.tick()
        self.assertEqual([n[1] for n in self.notices], ["Megéri: PNY RTX 3060 Ti"], "az 52 000-es már a limit fölött")
        self.assertEqual([c for c in self.hunter.llm.calls if "Friss hirdetések" in c[1]["content"]], [],
                         "a drága ismeretlen a modellhez sem jut el")

    def test_a_price_drop_can_make_it_worth_it(self):
        self.hunter.add_search("RTX 3080", ["hardverapro"], reference=230000)
        self.hunter.set_enabled(True)
        self.watch.add("RTX 3080", ["hardverapro"])
        self.watch.set_enabled(True)
        self.results(HA_CARDS[3:])
        self.watch.tick()
        self.later()
        self.watch.tick()
        self.assertEqual(self.notices, [], "185 000 Ft még nem éri meg")
        self.later()
        self.results([ha_card(4, "MSI RTX 3080 Ventus", "155 000 Ft")])
        self.watch.tick()
        self.assertIn("leárazta: 185 000 Ft → 155 000 Ft", self.notices[0][2])

    def test_it_looks_more_often_when_many_new_listings_come(self):
        """Tanult ritmus: amikor (a nap melyik órájában) sok új hirdetés szokott jönni, sűrűbben néz; a csendes
        órákban ritkábban; amíg kevés az adat, a beállított gyakorisággal."""
        alert = {"id": "x", "every": 3}
        hour = time.localtime(self.clock.now).tm_hour
        self.assertTrue(3 * 60 * 0.85 <= self.watch._interval(alert, "hardverapro", self.clock.now) <= 3 * 60 * 1.15,
                        "még nincs adat: 3 percenként")
        self.assertEqual(self.watch._factor("hardverapro", self.clock.now), 1.0)
        rhythm = [1.0] * 24
        rhythm[hour] = 30.0                                     # ebben az órában jön a legtöbb
        self.watch._data["rhythm"]["hardverapro"] = rhythm
        self.assertEqual(self.watch._factor("hardverapro", self.clock.now), 0.5)
        self.assertLessEqual(self.watch._interval(alert, "hardverapro", self.clock.now), 2 * 60 * 1.15,
                             "csúcsidőben sűrűbben (de legalább 2 percenként)")
        self.assertEqual({s["key"]: s["rhythm"] for s in self.watch.state()["sites"]}["hardverapro"],
                         "csúcsidő: sűrűbben nézem")
        rhythm = [2.0] * 24
        rhythm[hour] = rhythm[hour - 1] = rhythm[(hour + 1) % 24] = 0.0
        self.watch._data["rhythm"]["hardverapro"] = rhythm
        self.assertGreaterEqual(self.watch._interval(alert, "hardverapro", self.clock.now), 6 * 60 * 0.85,
                                "csendes órában ritkábban")
        # a letöltésekből tanul: az új, illő hirdetések az aktuális órához kerülnek
        self.watch._data["rhythm"].clear()
        self.hunter.add_search("RTX 3060 Ti", ["hardverapro"], reference=100000)
        self.hunter.set_enabled(True)
        self.watch.add("RTX 3060 Ti", ["hardverapro"])
        self.watch.set_enabled(True)
        self.results(HA_CARDS[:2])
        self.watch.tick()
        self.assertNotIn("hardverapro", self.watch._data["rhythm"], "az első letöltés csak megjegyez")
        self.later()
        self.results([ha_card(9, "Zotac RTX 3060 Ti Twin Edge", "99 000 Ft"), ha_card(8, "EVGA RTX 3060 Ti XC", "98 000 Ft")]
                     + HA_CARDS[:2])
        self.watch.tick()
        self.assertEqual(self.watch._data["rhythm"]["hardverapro"][time.localtime(self.clock.now).tm_hour], 2.0)

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
        self.assertTrue(self.notices[0][3]["quiet"], "utána magától újra próbálja: nem fontos értesítés")
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


class LinkedSearchTests(InstantCase):
    def test_a_search_is_watched_every_few_minutes_on_the_fast_sites(self):
        """A Keresések a Hardveraprón és a Jófogáson az azonnali figyelőben is futnak (ugyanazzal a szóval, ársávval,
        kizáró szavakkal): az új, megérő hirdetésről pár percen belül szól, nem csak a keresés óránkénti körében. Ha
        egy saját figyelő már ugyanazt nézi, nem kéri le kétszer; a keresés törlésével a figyelés is megszűnik."""
        state = self.hunter.add_search("RTX 3060 Ti", ["hardverapro", "vinted"], reference=100000, max_price=90000,
                                       exclude="hibás")
        search = state["searches"][0]
        self.hunter.set_enabled(True)
        self.watch.set_enabled(True)
        self.results(HA_CARDS[:2])
        self.assertEqual(self.watch.tick(), 1)
        [linked] = self.watch.state()["alerts"]
        self.assertEqual((linked["search"], linked["query"], linked["sites"], linked["max_price"], linked["exclude"]),
                         (search["id"], "RTX 3060 Ti", ["hardverapro"], 90000, ["hibás"]))
        self.assertEqual(self.notices, [], "elsőre csak megjegyzi, ami már fent van")
        self.later(6)
        self.results([ha_card(7, "Palit RTX 3060 Ti Dual", "52 000 Ft"), ha_card(8, "MSI RTX 3060 Ti hibás", "40 000 Ft")]
                     + HA_CARDS[:2])
        self.net.pages["_7/friss"] = page_html("Hibátlan, dobozában.")
        self.watch.tick()
        self.assertEqual([n[1] for n in self.notices], ["Megéri: Palit RTX 3060 Ti Dual"], "a hibásat ki is zárta")
        self.assertIn("RTX 3060 Ti", self.notices[0][2])
        with self.assertRaises(InstantError):
            self.watch.add("RTX 3060 Ti", ["hardverapro"])         # a keresés miatt már figyeli
        self.hunter.remove_search(search["id"])
        self.watch.link_searches()
        self.assertEqual(self.watch.state()["alerts"], [], "a keresés törlésével a figyelés is megszűnt")
        # ha egy saját figyelő már ugyanazt nézi, a keresésből nem lesz még egy (nem kéri le kétszer)
        self.watch.add("RTX 3060 Ti", ["hardverapro"])
        self.hunter.add_search("RTX 3060 Ti", ["hardverapro"], reference=100000)
        self.watch.link_searches()
        self.assertEqual([a.get("search") for a in self.watch.state()["alerts"]], [None])


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


class TwoLaneTests(InstantCase):
    """Párhuzamos böngészés: a Hardverapró és a Jófogás a gyors sorban (böngésző nélkül, saját szálon), közben a böngésző
    a Vintedet és a Vaterát nézi; mindkét sornak saját munkamenete van."""

    def setUp(self):
        super().setUp()
        self.hunter.parallel = True            # amit a start_fast_lane állít be (a szálat itt kézzel léptetjük)
        self.hunter.start()
        self.net.pages["hardverapro.hu/aprok/hardver"] = (200, ha_page(HA_CARDS))
        self.net.pages["jofogas.hu/magyarorszag"] = (200, "<html><body>" + jf_card(1, "iPhone 13 128GB", "185 000")
                                                     + "</body></html>")
        self.browser.results = [{"text": "Nike Air Max 90", "price": 20000, "url": "https://www.vinted.hu/items/1"}]

    def test_both_lanes_work_at_the_same_time(self):
        self.hunter.run()                      # a böngésző sora (az ágens-szálon)
        self.assertIn("Böngészés:", self.hunter.run_fast())
        self.assertTrue(self.browser.urls and all("vinted" in url for url in self.browser.urls),
                        "a böngésző a Vintedet nézi, a Hardverapróhoz nem nyúl")
        self.assertTrue(any("hardverapro.hu/aprok/hardver" in url for _t, url in self.net.asked))
        status = self.hunter.state()["browse"]["status"]
        self.assertIn("Most: Hardverapró", status)
        self.assertIn("Most: Vinted", status)
        self.assertEqual(self.hunter.run_fast(), "", "a gyors sor is tart szünetet két oldal között")

    def test_the_fast_lane_never_touches_the_browser(self):
        """Ha a gyors oldal most nem olvasható böngésző nélkül (pl. az óránkénti keret miatt), a gyors sor kihagyja, nem a
        böngészőhöz nyúl (az a másik szálé); ha egy oldal tilt, azt a böngésző sora veszi át."""
        self.fetcher.hourly = 1
        self.hunter.run_fast()
        self.clock.now += 120
        self.hunter.run_fast()
        self.assertEqual(self.browser.urls, [])
        self.net.pages["hardverapro.hu/aprok/hardver"] = (403, "tiltva")
        self.net.pages["jofogas.hu/magyarorszag"] = (403, "tiltva")
        self.fetcher.hourly = 90
        self.clock.now += 3600
        for _ in range(6):                     # a régi munkamenet lezárul, pihenő, aztán egy új kezdődik
            self.clock.now += 120
            self.hunter.run_fast()
        blocked = [site for site in ("hardverapro", "jofogas") if not self.fetcher.usable(site)]
        self.assertTrue(blocked)
        self.assertEqual({self.hunter._lane_of(site) for site in blocked}, {deals_module.LANE_BROWSER},
                         "a tiltó oldalt a böngésző sora veszi át")
        self.assertEqual(self.browser.urls, [], "a gyors sor akkor sem nyúlt a böngészőhöz")

    def test_the_thread(self):
        with mock.patch.object(deals_module, "FAST_TICK", 0.05):
            self.hunter.start_fast_lane(lambda: True, lambda: True, lambda *a, **k: None)
            try:
                deadline = time.monotonic() + 10
                while not self.net.asked and time.monotonic() < deadline:
                    time.sleep(0.05)
            finally:
                self.hunter.stop_fast_lane()
        self.assertIn("jarvis-gyors-sor", self.net.threads)
