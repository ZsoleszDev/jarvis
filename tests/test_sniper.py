"""A villámfigyelő (sniper.py): a Vinted legújabb hirdetései a megadott keresőszavakra, szinte folyamatosan, a
beállított időszakban és ritmusban, és rögtön szól (saját értesítéssel), ha új jön, ami illik. A logika egy hamis olvasóval (a
találati oldal hirdetései, amiket a teszt ad), a böngésző egy valódi Chromiummal, egy Vinted-szerű helyi oldalon (a
valódi Vintedet a tesztek nem érhetik el)."""
from __future__ import annotations

import datetime as dt
import json
import random
import sys
import tempfile
import threading
import time
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_web_browser import HAS_CHROMIUM  # noqa: E402

from jarvis_core import sniper as sniper_mod  # noqa: E402
from jarvis_core.config import load_config  # noqa: E402
from jarvis_core.sniper import Sniper, SniperError, VintedReader, search_url, window_of  # noqa: E402


def at(hhmm: str, day: int = 5) -> float:
    """2026. november <day>. <hhmm> helyi idő szerint."""
    hour, minute = map(int, hhmm.split(":"))
    return dt.datetime(2026, 11, day, hour, minute).timestamp()


def row(number: int, title: str, price: int, **extra) -> dict:
    slug = "-".join(title.lower().split())
    return {"url": f"https://www.vinted.hu/items/{number}-{slug}", "title": title, "text": f"{title} | {price} Ft",
            "price": price, **extra}


class FakeReader:
    """A találati oldal: keresőszó → a hirdetései (a legújabb elöl); a captcha egy kapcsoló."""

    def __init__(self):
        self.pages: dict[str, list[dict]] = {}
        self.calls: list[str] = []
        self.captcha = False
        self.fail: Exception | None = None
        self.closed = 0
        self.running = True

    def read(self, url: str) -> dict:
        self.calls.append(url)
        if self.fail is not None:
            raise self.fail
        if self.captcha:
            return {"rows": [], "captcha": True}
        query = url.split("search_text=")[1].split("&")[0] if "search_text=" in url else ""
        return {"rows": list(self.pages.get(query, [])), "captcha": False}

    def close(self):
        self.closed += 1


class SniperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = [at("16:35")]
        self.reader = FakeReader()
        self.sniper = self.make()
        self.sent: list[tuple] = []

    def make(self) -> Sniper:
        sniper = Sniper(Path(self.tmp.name), self.reader, clock=lambda: self.now[0], rng=random.Random(1))
        sniper.notify = lambda kind, title, text, **extra: self.sent.append((kind, title, text, extra))
        return sniper

    def tearDown(self):
        self.tmp.cleanup()

    def snipes(self) -> list[tuple]:
        return [s for s in self.sent if s[0] == "snipe"]

    def test_first_load_remembers_then_a_new_cheap_listing_is_told_at_once(self):
        """Az első betöltés csak megjegyzi, ami fent van; utána az új, ár alatti hirdetésről rögtön szól (saját
        értesítés: snipe, a hirdetés címével, sürgős), az ár fölöttiről nem."""
        self.sniper.add("ps4", max_price="40 000")
        self.sniper.configure(enabled=True)
        self.reader.pages["ps4"] = [row(5000100, "Sony PlayStation 4 Slim 500GB", 30000)]
        wait = self.sniper.tick()
        self.assertEqual(self.snipes(), [], "ami már fent volt, arról nem szól")
        self.assertTrue(10 * 0.75 - 0.01 <= wait <= 15 * 1.25, wait)
        self.now[0] += 15
        self.reader.pages["ps4"] = [row(5000300, "PS4 Pro 1TB két kontrollerrel", 55000),
                                    row(5000200, "Sony PlayStation 4 slim 1TB", 35000, likes=4),
                                    *self.reader.pages["ps4"]]
        self.sniper.tick()
        [(kind, title, text, extra)] = self.snipes()
        self.assertEqual((extra["url"], extra["urgent"]), (row(5000200, "Sony PlayStation 4 slim 1TB", 1)["url"], True))
        self.assertEqual(extra["snipe"], "vinted.hu#5000200")
        self.assertIn("35 000 Ft", title)
        self.assertIn("Sony PlayStation 4 slim 1TB", text)
        self.assertIn("a Vinted-díjjal kb. 37 030 Ft", text)
        self.assertIn("15 mp-en belül", text)
        self.assertIn("Már 4-en kedvelték, siess", text)
        state = self.sniper.state()
        self.assertEqual([h["price"] for h in state["hits"]], [35000])
        self.assertEqual((state["queries"][0]["found"], state["hits_today"], state["loads_today"]), (1, 1, 2))
        self.now[0] += 15
        self.sniper.tick()
        self.assertEqual(len(self.snipes()), 1, "ugyanarról egyszer")

    def test_the_price_limit_can_be_switched_off_and_filters_keep_out_games_and_accessories(self):
        """Az árhatár kikapcsolható (az összeg megmarad); az alsó határ kiszűri a játékokat, a kizáró szó és a
        címegyezés a tartozékot; a címegyezés kikapcsolható."""
        self.sniper.add("ps4", max_price=40000, min_price=15000, exclude="hibás")
        query = self.sniper.state()["queries"][0]
        self.assertEqual((query["max_price"], query["max_on"], query["min_price"], query["exclude"]),
                         (40000, True, 15000, ["hibás"]))
        self.sniper.configure(enabled=True)
        self.sniper.tick()                     # (üres oldal: megjegyzi)
        self.now[0] += 15
        self.reader.pages["ps4"] = [row(6000001, "PS4 Pro 1TB", 60000), row(6000002, "FIFA 23 PS4", 3000),
                                    row(6000003, "PS4 slim hibás, nem kapcsol be", 12000 + 8000),
                                    row(6000004, "Töltőállomás PS4 kontrollerhez", 16000)]
        self.sniper.tick()
        self.assertEqual(self.snipes(), [], "drága, játék, hibás, tartozék: egyik sem")
        self.sniper.update(query["id"], max_on=False)
        self.assertEqual(self.sniper.state()["queries"][0]["max_price"], 40000, "az összeg megmarad")
        self.now[0] += 15
        self.reader.pages["ps4"] = [row(6000005, "PS4 Pro 1TB dobozában", 70000), *self.reader.pages["ps4"]]
        self.sniper.tick()
        self.assertEqual([s[3]["snipe"] for s in self.snipes()], ["vinted.hu#6000005"], "árhatár nélkül a drága is")
        self.sniper.update(query["id"], strict=False, min_price="")
        self.now[0] += 15
        self.reader.pages["ps4"] = [row(6000006, "Kontroller töltő PS4-hez", 4000), *self.reader.pages["ps4"]]
        self.sniper.tick()
        self.assertEqual(self.snipes()[-1][3]["snipe"], "vinted.hu#6000006", "címegyezés nélkül a tartozék is")
        with self.assertRaises(SniperError):
            self.sniper.add("xbox", max_on=True)       # árhatár összeg nélkül
        with self.assertRaises(SniperError):
            self.sniper.add("xbox", max_price=10000, min_price=20000)

    def test_a_price_drop_under_the_limit_is_told_once(self):
        self.sniper.add("ps4", max_price=40000)
        self.sniper.configure(enabled=True)
        self.reader.pages["ps4"] = [row(7000001, "PS4 Slim 500GB", 45000)]
        self.sniper.tick()
        self.now[0] += 15
        self.reader.pages["ps4"] = [row(7000001, "PS4 Slim 500GB", 38000)]
        self.sniper.tick()
        [(_kind, _title, text, extra)] = self.snipes()
        self.assertIn("leárazta: 45 000 Ft → 38 000 Ft", text)
        self.now[0] += 15
        self.reader.pages["ps4"] = [row(7000001, "PS4 Slim 500GB", 36000)]
        self.sniper.tick()
        self.assertEqual(len(self.snipes()), 1, "egy már jelzett hirdetésről nem szól újra")

    def test_keywords_take_turns_and_one_listing_is_told_once(self):
        """Több keresőszó felváltva (közben az időköz × jitter); ugyanaz a hirdetés két keresőszóra egyszer."""
        self.sniper.add("ps4")
        self.sniper.add("playstation 4")
        with self.assertRaises(SniperError):
            self.sniper.add("PS4")               # ugyanaz
        self.sniper.configure(enabled=True, interval=20)
        for _ in range(4):
            self.sniper.tick()
            self.now[0] += 20
        self.assertEqual([url.split("search_text=")[1].split("&")[0] for url in self.reader.calls],
                         ["playstation%204", "ps4", "playstation%204", "ps4"])
        self.assertTrue(all("order=newest_first" in url for url in self.reader.calls))
        listing = row(8000001, "Sony PlayStation 4 slim", 30000)
        self.reader.pages["ps4"] = self.reader.pages["playstation%204"] = [listing]
        self.sniper.tick()
        self.now[0] += 20
        self.sniper.tick()
        self.assertEqual(len(self.snipes()), 1)
        state = self.sniper.state()
        self.assertEqual((state["every"], state["per_query"]), (20, 40))

    def test_window_and_rhythm(self):
        """Naponta 16:30–19:30, 20 perc figyelés, 10 perc szünet; a 0 perc szünet végig figyel; az éjfélen átnyúló
        időszak; a Most is az időszakon kívül is; kikapcsolva és keresőszó nélkül nem figyel."""
        sniper = self.sniper
        self.assertEqual(sniper.plan(at("16:35"))["phase"], "off")
        sniper.configure(enabled=True)
        self.assertEqual(sniper.plan(at("16:35"))["phase"], "empty")
        sniper.add("ps4")
        plan = sniper.plan(at("16:35"))
        self.assertEqual((plan["phase"], plan["until"]), ("active", at("16:50")))
        plan = sniper.plan(at("16:55"))
        self.assertEqual((plan["phase"], plan["until"]), ("pause", at("17:00")))
        self.assertEqual(sniper.plan(at("17:05"))["phase"], "active")
        plan = sniper.plan(at("19:15"))
        self.assertEqual((plan["phase"], plan["until"]), ("active", at("19:20")))
        plan = sniper.plan(at("19:25"))
        self.assertEqual((plan["phase"], plan["until"]), ("pause", at("19:30")), "az időszak végén véget ér")
        plan = sniper.plan(at("19:40"))
        self.assertEqual((plan["phase"], plan["until"]), ("outside", at("16:30", day=6)))
        plan = sniper.plan(at("10:00"))
        self.assertEqual((plan["phase"], plan["until"]), ("outside", at("16:30")))
        sniper.configure(pause=0)
        plan = sniper.plan(at("16:55"))
        self.assertEqual((plan["phase"], plan["until"]), ("active", at("19:30")), "szünet nélkül végig")
        sniper.configure(start="22:00", end="2:00", pause=10, active=20)
        self.assertEqual((sniper._data["start"], sniper._data["end"]), ("22:00", "02:00"))
        self.assertEqual(sniper.plan(at("01:05"))["phase"], "active", "éjfél után is (a tegnapi időszak)")
        self.assertEqual(sniper.plan(at("02:05"))["phase"], "outside")
        self.now[0] = at("12:00")
        self.assertIn("22:00-kor kezdek", sniper.state()["status"])
        sniper.burst(True)
        plan = sniper.plan(at("12:00"))
        self.assertEqual((plan["phase"], plan["until"], plan["burst"]), ("active", at("12:20"), True))
        self.assertIn("Most is", sniper.state()["status"])
        sniper.burst(False)
        sniper.configure(enabled=False)
        self.assertEqual(sniper.plan(at("23:00"))["phase"], "off")
        sniper.burst(True)
        self.assertEqual(sniper.plan(at("12:05"))["phase"], "active", "a Most is kikapcsolva is megy")
        with self.assertRaises(SniperError):
            sniper.configure(start="25:00")

    def test_outside_the_window_nothing_is_loaded_and_the_window_closes(self):
        self.sniper.add("ps4")
        self.sniper.configure(enabled=True)
        self.now[0] = at("19:45")
        wait = self.sniper.tick()
        self.assertEqual((self.reader.calls, wait), ([], sniper_mod.IDLE_TICK))
        self.assertEqual(self.reader.closed, 1, "az időszak után bezárja az ablakát")
        self.now[0] = at("16:55")               # a szakaszok közti szünet: nyitva marad
        self.assertEqual(self.sniper.tick(), sniper_mod.IDLE_TICK)
        self.assertEqual((self.reader.calls, self.reader.closed), ([], 1))

    def test_the_next_day_starts_with_remembering_not_a_flood(self):
        """Egy hosszabb kihagyás után (pl. másnap az időszak elején) az első betöltés csak megjegyzi, ami fent van; a
        szakaszok közti szünetben jött újakról viszont szól."""
        self.sniper.add("ps4")
        self.sniper.configure(enabled=True)
        self.reader.pages["ps4"] = [row(9000001, "PS4 slim", 30000)]
        self.sniper.tick()
        self.now[0] = at("17:01")                     # a 10 perc szünet után
        self.reader.pages["ps4"] = [row(9000002, "PS4 Pro", 50000), *self.reader.pages["ps4"]]
        self.sniper.tick()
        self.assertEqual(len(self.snipes()), 1, "a szünetben jött új még friss")
        self.now[0] = at("16:31", day=6)
        self.reader.pages["ps4"] = [row(9000010 + n, f"PS4 {n}", 30000) for n in range(30)]
        self.sniper.tick()
        self.assertEqual(len(self.snipes()), 1, "másnap reggel: csak megjegyzi")

    def test_many_new_at_once_get_a_summary(self):
        self.sniper.add("ps4", strict=False)
        self.sniper.configure(enabled=True)
        self.sniper.tick()
        self.now[0] += 15
        self.reader.pages["ps4"] = [row(9100000 + n, f"PS4 {n}", 30000) for n in range(8)]
        self.sniper.tick()
        snipes = self.snipes()
        self.assertEqual(len(snipes), sniper_mod.NOTIFY_MAX + 1)
        self.assertIn("Még 3 új hirdetés", snipes[-1][1])

    def test_a_captcha_is_not_bypassed_it_waits_longer_and_longer_and_slows_down(self):
        self.sniper.add("ps4")
        self.sniper.configure(enabled=True, pause=0)
        self.reader.captcha = True
        self.sniper.tick()
        state = self.sniper.state()
        self.assertEqual((state["phase"], state["blocked_until"]), ("blocked", self.now[0] + 15 * 60))
        self.assertEqual(state["slow"], 1.5)
        self.assertIn("robot-ellenőrzést kért", state["status"])
        [(kind, title, text, _extra)] = self.sent
        self.assertEqual(kind, "error")
        self.assertIn("Nem kerülöm meg", text)
        calls = len(self.reader.calls)
        self.now[0] += 60
        self.sniper.tick()
        self.assertEqual(len(self.reader.calls), calls, "vár, nem tölt be")
        self.now[0] += 15 * 60
        self.sniper.tick()                          # újra captcha: most 30 perc, és csak egyszer szól naponta
        self.assertEqual(self.sniper.state()["blocked_until"], self.now[0] + 30 * 60)
        self.assertEqual(len(self.sent), 1)
        self.sniper.resume()                        # megoldottad az ablakában
        self.reader.captcha = False
        self.sniper.tick()
        self.assertEqual(self.sniper.state()["phase"], "active")
        self.assertEqual(self.sniper.state()["every"], round(15 * 1.5 * 1.5), "ma ritkábban frissít")

    def test_the_jarvis_pause_holds_it(self):
        self.sniper.add("ps4")
        self.sniper.configure(enabled=True)
        self.sniper.allowed = lambda: False
        self.sniper.tick()
        self.assertEqual((self.reader.calls, self.sniper.state()["phase"]), ([], "held"))
        self.now[0] += sniper_mod.HELD_CLOSE
        self.sniper.tick()
        self.assertEqual(self.reader.closed, 1, "a Szünetben bezárja az ablakát")

    def test_a_failing_page_is_retried_and_told_once(self):
        self.sniper.add("ps4")
        self.sniper.configure(enabled=True)
        self.reader.fail = SniperError("Nem töltődött be a Vinted (net::ERR_INTERNET_DISCONNECTED).")
        waits = [self.sniper.tick() for _ in range(sniper_mod.FAILS_LONG + 1)]
        self.assertEqual(waits, [sniper_mod.FAIL_WAIT] * (sniper_mod.FAILS_LONG - 1) + [sniper_mod.FAIL_LONG_WAIT] * 2)
        self.assertEqual([s[0] for s in self.sent], ["error"])
        self.assertIn("ERR_INTERNET_DISCONNECTED", self.sniper.state()["queries"][0]["error"])
        self.reader.fail = None
        self.sniper.tick()
        self.assertNotIn("error", self.sniper.state()["queries"][0])

    def test_links_settings_and_storage(self):
        """Egy Vinted-keresés linkje a szűrőivel (a rendezés a legújabb elöl, az első oldal); más oldal linkje nem jó; a
        beállítások és a keresőszavak megmaradnak; a próba-értesítés; legfeljebb MAX_QUERIES keresőszó."""
        link = "https://www.vinted.hu/catalog?search_text=ps4&catalog[]=3025&order=relevance&page=3&time=1730000000"
        self.sniper.add(link, max_price=30000)
        query = self.sniper.state()["queries"][0]
        self.assertEqual((query["text"], query["url"]), ("ps4", link))
        self.assertEqual(query["link"],
                         "https://www.vinted.hu/catalog?search_text=ps4&catalog[]=3025&order=newest_first")
        for bad in ("https://www.jofogas.hu/magyarorszag?q=ps4", "https://www.vinted.hu/items/123-ps4", ""):
            with self.assertRaises(SniperError):
                self.sniper.add(bad)
        self.sniper.configure(enabled=True, start="17:00", end="20:15", active=15, pause=5, interval=30)
        again = self.make()
        state = again.state()
        self.assertEqual((state["enabled"], state["start"], state["end"], state["active"], state["pause"],
                          state["interval"], len(state["queries"])), (True, "17:00", "20:15", 15, 5, 30, 1))
        self.assertEqual(again.configure(interval=1)["interval"], sniper_mod.MIN_INTERVAL, "legalább 10 mp")
        again.test()
        [(kind, title, _text, extra)] = self.snipes()
        self.assertEqual((kind, extra["url"], extra["urgent"]), ("snipe", sniper_mod.HOME, True))
        self.assertTrue(extra["snipe"].startswith("proba-"))
        for n in range(sniper_mod.MAX_QUERIES - 1):
            again.add(f"kulcsszo {n}")
        with self.assertRaises(SniperError):
            again.add("egy túl sok")
        again.remove(query["id"])
        self.assertEqual(len(again.state()["queries"]), sniper_mod.MAX_QUERIES - 1)
        self.assertEqual(search_url({"text": "ps4 slim"}), "https://www.vinted.hu/catalog?search_text=ps4%20slim"
                                                           "&order=newest_first")

    def test_window_helper_handles_a_whole_day(self):
        current, upcoming = window_of(at("03:00"), 8 * 60, 8 * 60)
        self.assertIsNotNone(current, "egyforma kezdet és vég: egész nap")
        self.assertEqual(upcoming, at("08:00"))


class _Catalog(SimpleHTTPRequestHandler):
    """A Vinted-szerű találati oldal: a hirdetések (a teszt adja meg), vagy egy robot-ellenőrzés."""
    state: dict = {"rows": [], "captcha": False}

    def do_GET(self):  # noqa: N802
        if self.state["captcha"]:
            body = "<!doctype html><title>Just a moment...</title><p>Checking your browser</p>"
        else:
            cards = "".join(f"<div class='feed-grid__item'><div class='new-item-box'>"
                            f"<a href='/items/{n}-{t.replace(' ', '-')}'><h3>{t}</h3></a>"
                            f"<p class='price'>{p:,} Ft</p></div></div>".replace(",", " ")
                            for n, t, p in self.state["rows"])
            body = ("<!doctype html><html lang='hu'><title>Vinted</title><body>"
                    f"<div class='feed-grid'>{cards}</div></body></html>")
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class SniperBrowserTests(unittest.TestCase):
    """A villámfigyelő saját böngészője (VintedReader) egy valódi Chromiummal, a saját profiljában, a saját szálán."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Catalog)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.catalog = f"http://127.0.0.1:{cls.server.server_address[1]}/catalog"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Catalog.state = {"rows": [(5000001, "Sony PlayStation 4 Slim 500GB", 30000)], "captcha": False}
        self.tmp = tempfile.TemporaryDirectory()
        cfg = load_config(ROOT / "config.toml").browser
        cfg.headless, cfg.channel = True, ""
        self.reader = VintedReader(cfg, Path(self.tmp.name) / "villam")
        self.sniper = Sniper(Path(self.tmp.name), self.reader, catalog=self.catalog)
        self.sent: list[tuple] = []
        self.told = threading.Event()

        def notify(kind, title, text, **extra):
            self.sent.append((kind, title, text, extra))
            if kind == "snipe":
                self.told.set()
        self.sniper.notify = notify

    def tearDown(self):
        self.sniper.stop()
        self.reader.close()
        self.tmp.cleanup()

    def test_reads_the_cards_and_spots_a_captcha(self):
        """A kártyák (cím, ár, link) a betöltés után rögtön; a saját profilmappájában; a robot-ellenőrzést jelzi."""
        url = search_url({"text": "ps4"}, self.catalog)
        result = self.reader.read(url)
        self.assertEqual([(r["price"], r["title"]) for r in result["rows"]], [(30000, "Sony PlayStation 4 Slim 500GB")])
        self.assertFalse(result["captcha"])
        self.assertTrue((Path(self.tmp.name) / "villam").is_dir())
        _Catalog.state["captcha"] = True
        self.assertEqual(self.reader.read(url), {"rows": [], "captcha": True})

    def test_the_thread_tells_a_new_listing_within_seconds(self):
        """A háttérszál (a saját Playwrightjával): a Most is után az első frissítés megjegyzi, ami fent van, és amint
        új, ár alatti hirdetés kerül fel, pár másodpercen belül szól."""
        self.sniper.add("ps4", max_price=40000)
        with self.sniper._lock:
            self.sniper._data["interval"] = 1      # (a tesztben gyorsan; élesben legalább MIN_INTERVAL)
        self.sniper.burst(True)
        self.sniper.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not self.sniper.state()["loads_today"]:
            time.sleep(0.1)
        self.assertTrue(self.sniper.state()["loads_today"], "nem töltött be")
        self.assertEqual(self.sent, [])
        _Catalog.state["rows"] = [(5000002, "PS4 Pro 1TB", 39000), *_Catalog.state["rows"]]
        posted = time.monotonic()
        self.assertTrue(self.told.wait(20), "nem szólt")
        self.assertLess(time.monotonic() - posted, 10)
        [(kind, title, _text, extra)] = self.sent
        self.assertEqual((kind, extra["urgent"]), ("snipe", True))
        self.assertIn("39 000 Ft", title)
        self.assertTrue(extra["url"].endswith("/items/5000002-PS4-Pro-1TB"))
        self.sniper.stop()
        self.assertFalse(self.sniper.working())
        self.assertFalse(self.reader.running, "leálláskor a böngészője is bezárul")
        saved = json.loads((Path(self.tmp.name) / "villam.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["hits"][0]["price"], 39000)


if __name__ == "__main__":
    unittest.main()
