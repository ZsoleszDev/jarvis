"""Az emberi tempó tesztjei: a tempó véletlenje (humanize.py), a böngésző emberi mozdulatai egy helyi
oldalon (egérút, billentyűnkénti gépelés, gombnyomás), a modell nélküli lépéssorok szünetei, és a
két közzététel közötti várakozás ugyanazon az oldalon.

A böngészős részek valódi (láthatatlan) Chromiumot indítanak; ha a Playwright vagy a Chromium nincs
telepítve, ezek kimaradnak. Internet és modell nem kell.

Futtatás:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import functools
import math
import random
import statistics
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_offline import FakeOllama, make_cfg  # noqa: E402
from test_replay import ReplayHarness  # noqa: E402
from test_streaming import RecordingUI  # noqa: E402
from test_web_browser import HAS_CHROMIUM, _QuietHandler  # noqa: E402
from test_work import jpeg  # noqa: E402

from jarvis_core.app import JarvisApp  # noqa: E402
from jarvis_core.browser import BrowserController, same_value  # noqa: E402
from jarvis_core.config import load_config  # noqa: E402
from jarvis_core.humanize import LONG_TEXT, Pace  # noqa: E402
from jarvis_core.llm import Cancelled  # noqa: E402
from jarvis_core.replay import ReplayOutcome, Replayer  # noqa: E402
from jarvis_core.selling import POSTED  # noqa: E402

# --- a tempó véletlenje ------------------------------------------------------------------------------


class PaceTests(unittest.TestCase):
    def test_pauses_follow_the_switch_and_the_factor(self):
        slept: list[float] = []
        pace = Pace(True, 1.0, random.Random(1))
        pace.sleep = slept.append
        think = pace.think()
        self.assertTrue(0.5 <= think <= 1.5)
        self.assertEqual(slept, [think])
        read = pace.read()
        self.assertTrue(1.2 <= read <= 2.8)
        self.assertAlmostEqual(pace.waited, think + read)
        for off in (Pace(False, 1.0), Pace(True, 0.0)):   # kikapcsolva, vagy szünetek nélkül (tesztek)
            off.sleep = slept.append
            self.assertEqual((off.review(), off.pick_files(), off.press_ms()), (0.0, 0.0, 0.0))
            self.assertEqual(off.key_delays("abc"), [0.0, 0.0, 0.0])
            self.assertEqual(off.move_seconds((0, 0), (600, 400)), 0.0)
        self.assertEqual(len(slept), 2, "kikapcsolva nem vár")
        slower = Pace(True, 2.0, random.Random(1))
        slower.sleep = lambda _s: None
        self.assertTrue(3.0 <= slower.review() <= 7.0, "a pace szorzó nyújtja a szüneteket")

    def test_typing_rhythm_varies_like_a_person(self):
        pace = Pace(True, 1.0, random.Random(7))
        text = "Zara fekete póló, L-es méret. " * 12
        delays = pace.key_delays(text)
        self.assertEqual(len(delays), len(text))
        self.assertTrue(all(0 < d <= 1.5 for d in delays))
        self.assertTrue(0.08 < statistics.median(delays) < 0.2, statistics.median(delays))
        self.assertGreater(statistics.pstdev(delays), 0.03, "nem egyenletes, mint egy gépé")
        after_gap = [d for ch, d in zip(text, delays) if ch in " ,."]
        letters = [d for ch, d in zip(text, delays) if ch.isalpha()]
        self.assertGreater(statistics.mean(after_gap), statistics.mean(letters), "szóköz és írásjel után lassabb")
        self.assertTrue(any(d > 0.4 for d in delays), "néha megtorpan")

    def test_mouse_path_is_a_gentle_curve(self):
        start, end = (100.0, 100.0), (700.0, 400.0)
        dist = math.dist(start, end)
        deviations = []
        for seed in range(20):
            points = Pace(True, 1.0, random.Random(seed)).path(start, end)
            self.assertTrue(6 <= len(points) <= 28)
            self.assertEqual(points[-1], end, "pont oda ér, ahova kattint")
            steps = [math.dist(a, b) for a, b in zip([start, *points], points)]
            self.assertLess(steps[0], max(steps), "lassan indul")
            self.assertLess(steps[-1], max(steps), "lassan áll meg")
            # a legnagyobb eltérés az egyenestől (pont–egyenes távolság)
            (x0, y0), (x1, y1) = start, end
            deviations.append(max(abs((y1 - y0) * x - (x1 - x0) * y + x1 * y0 - y1 * x0) / dist
                                  for x, y in points))
        self.assertTrue(all(d <= 0.11 * dist for d in deviations), "enyhe ív, nem cikcakk")
        self.assertGreater(sum(d > 5 for d in deviations), 12, "többnyire nem nyílegyenes")
        self.assertEqual(len(Pace(True, 1.0).path((0, 0), (5, 0))), 6, "rövid úton is több lépés")

    def test_click_point_press_and_move_time(self):
        pace = Pace(True, 1.0, random.Random(5))
        box = {"x": 10.0, "y": 20.0, "width": 100.0, "height": 40.0}
        points = [pace.point_in(box) for _ in range(60)]
        self.assertTrue(all(40 <= x <= 80 and 32 <= y <= 48 for x, y in points), "a belsejére, nem a szélére")
        self.assertGreater(len({(round(x), round(y)) for x, y in points}), 30, "nem mindig ugyanoda")
        self.assertTrue(all(45 <= pace.press_ms() <= 120 for _ in range(20)))
        self.assertLess(pace.move_seconds((0, 0), (50, 0)), 0.3)
        self.assertLessEqual(pace.move_seconds((0, 0), (5000, 0)), 0.75, "egy mozdulat ritkán több fél mp-nél")


class SameValueTests(unittest.TestCase):
    def test_what_counts_as_typed(self):
        self.assertTrue(same_value("Zara  póló", "zara póló"))
        self.assertTrue(same_value("5 000 Ft", "5000"), "a mező tagolhat és pénznemet írhat mellé")
        self.assertTrue(same_value("5000", "5 000 Ft"))
        self.assertTrue(same_value("4,5", "4.5"))
        self.assertTrue(same_value("", ""))
        self.assertFalse(same_value("0500", "500"), "a régi 0 mögé került: nem jó")
        self.assertFalse(same_value("Régi címZara póló", "Zara póló"))
        self.assertFalse(same_value("Zara póló", "Zara"))
        self.assertFalse(same_value("régi szöveg", ""))


# --- a böngésző emberi mozdulatai egy helyi oldalon ---------------------------------------------------

TEMPO_PAGE = """<!doctype html><html lang="hu"><head><meta charset="utf-8"><title>Tempó</title></head><body>
<label>Cím <input id="title" name="title"></label>
<label>Ár <input id="price" name="price" value="0"
  onkeydown="if (event.ctrlKey && event.key.toLowerCase() === 'a') event.preventDefault()"></label>
<label>Leírás <textarea id="desc" name="desc"></textarea></label>
<button type="button" id="save" onclick="document.getElementById('out').textContent = 'Mentve'">Mentés</button>
<button type="button" id="pub" onclick="document.getElementById('out').textContent = 'Közzétéve'">Közzététel</button>
<label>Fotók <input type="file" id="files" accept="image/*" multiple></label>
<p id="out"></p>
<div style="height:2600px"></div>
<button type="button" id="far" onclick="document.getElementById('out').textContent = 'Lent'">Lent</button>
<div style="height:600px"></div>
<script>
window.log = {keydown: {}, input: {}, moves: 0, downs: [], ups: [], clicks: [], wheels: 0};
addEventListener('wheel', () => { log.wheels += 1; }, {passive: true});
const id = (e) => e.target.id || '-';
document.addEventListener('keydown', (e) => { log.keydown[id(e)] = (log.keydown[id(e)] || 0) + 1; }, true);
document.addEventListener('input', (e) => { log.input[id(e)] = (log.input[id(e)] || 0) + 1; }, true);
document.addEventListener('mousemove', () => { log.moves += 1; }, true);
document.addEventListener('mousedown', (e) => log.downs.push(performance.now()), true);
document.addEventListener('mouseup', (e) => log.ups.push(performance.now()), true);
document.addEventListener('click', (e) => log.clicks.push([e.offsetX, e.offsetY, id(e), e.isTrusted]), true);
</script></body></html>"""


class TempoSite:
    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        (Path(self.dir.name) / "tempo.html").write_text(TEMPO_PAGE, encoding="utf-8")
        self.photo = Path(self.dir.name) / "foto.jpg"
        self.photo.write_bytes(jpeg())
        handler = functools.partial(_QuietHandler, directory=self.dir.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/tempo.html"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class HumanBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = TempoSite()
        cls.profile = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.site.close()
        cls.profile.cleanup()

    def setUp(self):
        self.asked: list[str] = []
        self.browsers: list[BrowserController] = []

    def tearDown(self):
        for browser in self.browsers:
            browser.close()

    def make(self, human: bool = True, pace: float = 0.0) -> BrowserController:
        cfg = load_config(ROOT / "config.toml").browser
        cfg.headless = True
        cfg.profile_dir = self.profile.name
        cfg.human_pace = human
        cfg.pace = pace
        browser = BrowserController(cfg, allow_local=True, confirm=lambda prompt: self.asked.append(prompt) or True)
        self.browsers.append(browser)
        browser.open(self.site.url)
        return browser

    @staticmethod
    def find(browser: BrowserController, label: str) -> int:
        return next(i for i, e in browser.elements.items() if e.get("label") == label)

    @staticmethod
    def log(browser: BrowserController) -> dict:
        return browser.page.evaluate("() => window.log")

    def test_typing_is_key_by_key_and_long_text_is_pasted(self):
        browser = self.make()
        text = "Zara póló M"
        browser.type_text(self.find(browser, "Cím"), text)
        long_text = "Egyszer hordtam, szinte új, füstmentes, kisállatmentes otthonból. Postázom is."
        self.assertGreater(len(long_text), LONG_TEXT)
        browser.type_text(self.find(browser, "Leírás"), long_text)
        log = self.log(browser)
        self.assertEqual(log["input"]["title"], len(text), "minden karakter külön leütés")
        us_layout = sum(ch.isascii() for ch in text)   # az ékezetest a Playwright billentyű nélkül írja
        self.assertGreaterEqual(log["keydown"]["title"], us_layout)
        self.assertEqual(log["input"]["desc"], 1, "a hosszú szöveget egyben illeszti be")
        self.assertLessEqual(log["keydown"].get("desc", 0), 2, "csak a Ctrl+A")
        self.assertEqual(browser.page.input_value("#title"), text)
        self.assertEqual(browser.page.input_value("#desc"), long_text)

    def test_old_value_left_in_the_field_is_fixed(self):
        browser = self.make()
        browser.type_text(self.find(browser, "Ár"), "500")   # itt a Ctrl+A nem jelöli ki a régi „0”-t
        self.assertEqual(browser.page.input_value("#price"), "500", "nem „0500”")

    def test_the_mouse_travels_before_a_click(self):
        browser = self.make()
        for _ in range(3):
            browser.snapshot()
            browser.click(self.find(browser, "Mentés"))
        log = self.log(browser)
        self.assertGreaterEqual(log["moves"], 3 * 6, "minden kattintás előtt odamegy az egér")
        clicks = [c for c in log["clicks"] if c[2] == "save"]
        self.assertEqual(len(clicks), 3)
        self.assertTrue(all(c[3] for c in clicks), "valódi (trusted) kattintás")
        self.assertGreater(len({(c[0], c[1]) for c in clicks}), 1, "nem mindig pont ugyanoda")
        self.assertEqual(browser.page.inner_text("#out"), "Mentve")

    def test_far_elements_are_reached_with_the_wheel(self):
        browser = self.make()
        browser._human_click(browser.page, browser.page.locator("#far"))
        log = self.log(browser)
        self.assertGreaterEqual(log["wheels"], 5, "az egérgörgővel tekert oda, nem ugrott")
        self.assertGreater(browser.page.evaluate("scrollY"), 1500)
        self.assertEqual(browser.page.inner_text("#out"), "Lent")
        self.assertEqual(log["clicks"][-1][2], "far")

    def test_search_results_get_a_glance(self):
        browser = self.make()
        self.assertEqual(browser.extract_listings(self.site.url), [], "ezen az oldalon nincs ár")
        self.assertGreaterEqual(self.log(browser)["wheels"], 2, "átfutja a találatokat, ahogy az ember")

    def test_without_human_pace_it_fills_directly(self):
        browser = self.make(human=False)
        browser.type_text(self.find(browser, "Cím"), "Zara póló M")
        browser.click(self.find(browser, "Mentés"))
        log = self.log(browser)
        self.assertEqual(log["keydown"].get("title", 0), 0, "egyben tölti ki")
        self.assertEqual(log["input"]["title"], 1)
        self.assertLess(log["moves"], 6, "nincs egérút")

    def test_model_free_mode_pauses_like_a_person(self):
        browser = self.make(pace=1.0)
        slept: list[float] = []
        browser._sleep = slept.append            # a valódi várakozás helyett feljegyezzük
        browser.pace.sleep = slept.append
        named: list[str] = []
        for name in ("think", "read", "review", "pick_files"):
            original = getattr(browser.pace, name)
            setattr(browser.pace, name, lambda n=name, o=original: named.append(n) or o())

        browser.open(self.site.url)
        browser.click(self.find(browser, "Mentés"))
        browser.type_text(self.find(browser, "Cím"), "Zara")
        self.assertEqual(named, [], "a modell amúgy is lassú: ilyenkor nincs külön gondolkodási szünet")
        self.assertTrue(slept, "de a gépelés és az egér akkor is emberi tempójú")

        browser.auto = True                       # modell nélküli lépéssor (begyakorolt lépések)
        browser.open(self.site.url)
        browser.click(self.find(browser, "Mentés"))
        browser.type_text(self.find(browser, "Cím"), "Zara")
        browser.upload_files([str(self.site.photo)], self.find(browser, "Fotók"))
        browser.click(self.find(browser, "Közzététel"))
        self.assertEqual(named, ["read", "think", "think", "pick_files", "review"])
        self.assertIn("Közzététel", self.asked[-1], "a jóváhagyás attól még megmarad")
        self.assertEqual(browser.page.inner_text("#out"), "Közzétéve")
        self.assertTrue(all(0 < s <= 3.5 for s in slept))
        log = self.log(browser)
        presses = [up - down for down, up in zip(log["downs"], log["ups"])]
        self.assertTrue(presses and min(presses) >= 35, f"a gomb egy pillanatig lenyomva: {presses}")

    def test_real_pauses_keep_the_page_alive(self):
        browser = self.make(pace=1.0)
        started = time.monotonic()
        browser._sleep(0.3)      # a böngésző eseményei közben is feldolgozódnak (pl. fájlválasztó)
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        self.assertEqual(browser.page.title(), "Tempó")
        browser._sleep(0)
        browser.close()
        started = time.monotonic()
        browser._sleep(0.1)      # bezárt böngészőnél sima várakozás
        self.assertGreaterEqual(time.monotonic() - started, 0.08)

    def test_replay_switches_to_model_free_mode_and_back(self):
        class FakeBrowser:
            auto = False

        seen = []
        replayer = Replayer(FakeBrowser(), {"steps": []}, {}, [])
        def run(self, _url):
            seen.append(self.browser.auto)
            return ReplayOutcome("done", "")

        with mock.patch.object(Replayer, "_run", run):
            replayer.run("https://pelda.hu/uj")
        self.assertEqual(seen, [True])
        self.assertFalse(replayer.browser.auto)

        def stopped(_self, _url):
            raise Cancelled()

        with mock.patch.object(Replayer, "_run", stopped), self.assertRaises(Cancelled):
            replayer.run("https://pelda.hu/uj")
        self.assertFalse(replayer.browser.auto, "megszakítás után is visszaáll")


# --- két közzététel között ugyanazon az oldalon -------------------------------------------------------

class PublishGapTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.fake.vram_fit = lambda ctx: 1.0
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        self.ui = RecordingUI()
        self.app = JarvisApp(self.cfg, self.ui, use_voice=False, use_speech=False, use_wake_word=False)
        self.assertTrue(self.app.start())
        self.work = self.app.work
        self.item = self.work.store.create(targets=["vinted"], prompt="póló")

    def tearDown(self):
        self.app.shutdown()
        self.fake.close()
        self.tmp.cleanup()

    def test_gap_is_per_site_and_a_bit_random(self):
        self.work.note_publish("vinted", now=1000.0)
        self.assertEqual(self.work.gap_remaining("vinted", now=1000.0), 0.0, "a tesztekben 0 perc a szünet")
        self.cfg.work.publish_gap_minutes = 2.0
        gaps = set()
        for _ in range(10):
            self.work.note_publish("vinted", now=1000.0)
            gap = self.work.gap_remaining("vinted", now=1000.0)
            self.assertTrue(84 <= gap <= 156, gap)
            gaps.add(round(gap, 3))
        self.assertGreater(len(gaps), 1, "nem mindig ugyanannyi")
        self.assertEqual(self.work.gap_remaining("jofogas", now=1000.0), 0.0, "másik oldalon nem kell várni")
        self.assertEqual(self.work.gap_remaining("vinted", now=1200.0), 0.0)

    def test_waits_and_can_be_stopped(self):
        self.work._next_allowed["vinted"] = time.time() + 0.6
        started = time.monotonic()
        self.app._respect_gap(self.item.id, "vinted")
        self.assertGreaterEqual(time.monotonic() - started, 0.5)
        self.assertTrue(any("nemrég tettem közzé" in e[1] for e in self.ui.of("info")))
        self.assertIn(("state", "working", "Vinted: várok még 0:05"), self.ui.events)

        self.work._next_allowed["vinted"] = time.time() + 60
        threading.Timer(0.2, self.app.stop_event.set).start()
        started = time.monotonic()
        with self.assertRaises(Cancelled):
            self.app._respect_gap(self.item.id, "vinted")
        self.assertLess(time.monotonic() - started, 2.0, "az Állj gomb rögtön megállítja")


    def test_a_post_started_from_the_chat_waits_too(self):
        store = self.work.store
        store.add_photo(self.item.id, jpeg())
        store.update(self.item.id, title="Zara póló", description="Egyszer hordtam.", price=500)
        self.work._next_allowed["vinted"] = time.time() + 0.5
        args = {"item": self.item.id, "platform": "vinted"}
        with mock.patch.object(self.app.toolbox.browser, "open", return_value="Oldal: Új hirdetés") as opened:
            started = time.monotonic()
            result = self.app.toolbox.call("item_post", args)
            self.assertGreaterEqual(time.monotonic() - started, 0.4, "a modell eszköze is kivárja")
            self.assertIn("Oldal: Új hirdetés", result)
            opened.assert_called_once()
            self.work._next_allowed["vinted"] = time.time() + 60
            self.app.stop_event.set()
            started = time.monotonic()
            self.assertEqual(self.app.toolbox.call("item_post", args), "HIBA: Megszakítottad.")
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(opened.call_count, 1, "megszakítva meg sem nyitja az oldalt")


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class HumanReplayFlowTests(ReplayHarness, unittest.TestCase):
    def test_replay_is_human_paced_and_waits_after_the_last_publish(self):
        first = self.item()
        self.learn_posting(first)
        self.assertEqual(self.store.get(first.id).platforms["vinted"]["status"], POSTED)
        self.assertEqual(self.work.gap_remaining("vinted"), 0.0)

        # A második tárgy előtt: a közzététel után 0,6 perc (±30%) szünet, és a begyakorolt lépések
        # emberi szünetekkel (a várakozást itt csak feljegyezzük).
        self.app.cfg.work.publish_gap_minutes = 0.01
        self.work.note_publish("vinted")
        browser = self.app.toolbox.browser
        browser.pace.factor = 1.0
        slept: list[float] = []
        browser._sleep = slept.append
        browser.pace.sleep = slept.append
        named: list[str] = []
        for name in ("think", "read", "review", "pick_files"):
            original = getattr(browser.pace, name)
            setattr(browser.pace, name, lambda n=name, o=original: named.append(n) or o())
        second = self.item(title="H&M fehér póló, M", price=700, brand="H&M", condition="Jó", size="M")
        calls_before = len(self.fake.chat_bodies())
        started = time.monotonic()
        self.app.run_work({"action": "post", "ids": [second.id], "platform": "vinted"})
        self.assertGreaterEqual(time.monotonic() - started, 0.35, "megvárta a szünetet")
        self.assertTrue(any("nemrég tettem közzé" in e[1] for e in self.ui.of("info")))
        self.assertEqual(len(self.fake.chat_bodies()), calls_before, "modell nélkül ment")
        self.assertEqual(self.store.get(second.id).platforms["vinted"]["status"], POSTED)
        self.assertEqual(named[0], "read", "az űrlap megnyitása után körülnéz")
        self.assertIn("pick_files", named)
        self.assertGreaterEqual(named.count("think"), 5)
        self.assertEqual(named[-1], "review", "a közzététel előtt átnézi")
        self.assertGreater(sum(slept), 5.0, "egy ember sem tölt ki egy hirdetést pár másodperc alatt")
        self.assertFalse(browser.auto, "utána a modell lépései már nem kapnak külön szünetet")
        self.assertGreater(self.work.gap_remaining("vinted"), 0.0, "a következő is vár majd")


if __name__ == "__main__":
    unittest.main(verbosity=2)
