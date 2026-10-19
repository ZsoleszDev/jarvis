"""A Szünet gomb, a háttérmunka (ütemező), a figyelők, az értesítések és az okosabb webes keresés
tesztjei. Internet, modell és GPU nélkül futnak (hamis Ollama, hamis kereső, helyi tesztoldalak).

Futtatás:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import datetime as dt
import functools
import importlib.machinery
import random
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_offline import FakeOllama, is_warm_up, make_cfg, reply  # noqa: E402
from test_streaming import RecordingUI  # noqa: E402
from test_web_browser import HAS_CHROMIUM  # noqa: E402
from test_work import _QuietHandler  # noqa: E402

from jarvis_core import web  # noqa: E402
from jarvis_core.app import PAUSED_REPLY, JarvisApp, is_pause_request  # noqa: E402
from jarvis_core.browser import BrowserController  # noqa: E402
from jarvis_core.config import SchedulerCfg, WebCfg  # noqa: E402
from jarvis_core.listings import BLOCKED, GONE, LIVE, SOLD, ListingChecker, classify  # noqa: E402
from jarvis_core.notices import NoticeBoard  # noqa: E402
from jarvis_core.scheduler import Hooks, Job, Preempted, Scheduler, within_hours  # noqa: E402
from jarvis_core.selling import POSTED, ItemStore, Platform  # noqa: E402
from jarvis_core.watches import WatchError, WatchList, normalize_url  # noqa: E402


# --- hamis DuckDuckGo-kereső (a ddgs csomag helyett) ----------------------------------------------

class FakeDDGS:
    """A ddgs.DDGS helyett: előre megadott találatok, és feljegyzi, mit kerestek."""
    results: list[list[dict]] = []
    calls: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    def text(self, query, **options):
        FakeDDGS.calls.append({"query": query, **options})
        return FakeDDGS.results.pop(0) if FakeDDGS.results else []


def install_fake_ddgs() -> mock._patch:
    module = types.ModuleType("ddgs")
    module.DDGS = FakeDDGS
    module.__spec__ = importlib.machinery.ModuleSpec("ddgs", None)
    FakeDDGS.results, FakeDDGS.calls = [], []
    return mock.patch.dict(sys.modules, {"ddgs": module})


def hit(n: int, host: str = "allas.hu") -> dict:
    return {"title": f"Állás {n}", "href": f"https://{host}/allas/{n}?utm_source=x", "body": f"Leírás {n}"}


# --- ütemező -------------------------------------------------------------------------------------

class Clock:
    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.flags = {"paused": False, "busy": False, "preempted": False}
        self.last_activity = self.clock.now - 3600
        self.submitted: list[str] = []
        self.runs: list[str] = []
        self.cfg = SchedulerCfg(quiet_minutes=5, watch_minutes=60)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, job=None) -> Scheduler:
        hooks = Hooks(paused=lambda: self.flags["paused"], busy=lambda: self.flags["busy"],
                      last_activity=lambda: self.last_activity, submit=self.submitted.append,
                      preempted=lambda: self.flags["preempted"])
        scheduler = Scheduler(self.cfg, Path(self.tmp.name) / "hatter.json", hooks, clock=self.clock)
        scheduler.register(job or Job("watches", "Figyelők", lambda: 60.0, self.job))
        return scheduler

    def job(self, ctx) -> str:
        ctx.check()
        self.runs.append("watches")
        return "kész"

    def test_runs_only_when_jarvis_is_free(self):
        scheduler = self.make()
        for flag, reason in (("paused", "szünet"), ("busy", "Jarvis dolgozik")):
            self.flags[flag] = True
            self.assertEqual(scheduler.blocked_reason(), reason)
            self.assertIsNone(scheduler.tick())
            self.flags[flag] = False
        self.last_activity = self.clock.now - 60          # egy perce beszélgettünk
        self.assertEqual(scheduler.blocked_reason(), "nemrég beszélgettünk")
        self.assertIsNone(scheduler.tick())
        self.last_activity = self.clock.now - 6 * 60
        self.assertEqual(scheduler.tick(), "watches")
        self.assertIsNone(scheduler.tick(), "amíg el nem indul, nem teszi sorba újra")
        self.assertEqual(scheduler.run("watches"), "kész")
        self.assertEqual(self.submitted, ["watches"])
        self.assertIsNone(scheduler.tick(), "egy órán belül nem esedékes újra")
        self.clock.now += 61 * 60
        self.last_activity = self.clock.now - 3600
        self.assertEqual(scheduler.tick(), "watches")

    def test_postponed_not_skipped_and_yields_to_you(self):
        scheduler = self.make()
        self.flags["busy"] = True                      # pl. játszol vagy beszélgetünk: elhalasztja
        self.clock.now += 3 * 3600
        self.assertIsNone(scheduler.tick())
        self.flags["busy"] = False
        self.assertEqual(scheduler.tick(), "watches", "amint szabad, sorra kerül")
        self.flags["preempted"] = True                 # közben megszólítottad
        self.assertIn("Később", scheduler.run("watches"))
        self.assertEqual(self.runs, [])
        self.flags["preempted"] = False
        scheduler.run("watches")
        self.assertEqual(self.runs, ["watches"])

    def test_preempted_in_the_middle_stays_due(self):
        def job(ctx):
            self.flags["preempted"] = True            # a munka közben jön a kérésed
            ctx.check()
            return "nem ér ide"

        scheduler = self.make(Job("watches", "Figyelők", lambda: 60.0, job))
        self.assertIn("Félbehagytam", scheduler.run("watches"))
        self.flags["preempted"] = False
        self.assertTrue(scheduler.due(scheduler.jobs["watches"], self.clock.now), "később folytatja")

    def test_errors_wait_for_the_next_turn_and_state_survives_restart(self):
        def job(ctx):
            raise RuntimeError("a kereső korlátoz")

        scheduler = self.make(Job("watches", "Figyelők", lambda: 60.0, job))
        self.assertIn("a kereső korlátoz", scheduler.run("watches"))
        self.assertFalse(scheduler.due(scheduler.jobs["watches"], self.clock.now), "nem próbálkozik percenként")
        until = scheduler.snooze(30)
        self.assertIn("felfüggesztve", scheduler.blocked_reason())
        again = self.make()
        self.assertEqual(again.state["jobs"]["watches"]["error"], "RuntimeError: a kereső korlátoz")
        self.assertEqual(again.state["snoozed_until"], until)
        again.snooze(0)
        self.assertEqual(again.blocked_reason(), "")
        status = again.status()
        self.assertTrue(status["free"])
        self.assertEqual(status["jobs"][0]["title"], "Figyelők")

    def test_a_job_switched_off_stays_off(self):
        scheduler = self.make()
        self.assertFalse(scheduler.set_enabled("nincs ilyen", False))
        self.assertTrue(scheduler.set_enabled("watches", False))      # az Automatikus munkák ablakból
        self.clock.now += 3 * 3600
        self.assertFalse(scheduler.due(scheduler.jobs["watches"], self.clock.now))
        self.assertIsNone(scheduler.tick(), "kikapcsolva magától nem fut")
        job = scheduler.status()["jobs"][0]
        self.assertFalse(job["on"])
        self.assertFalse(job["enabled"])
        self.assertIsNone(job["next_run"])
        again = self.make()                                            # újraindítás után is így marad
        self.assertFalse(again.is_on("watches"))
        self.assertIsNone(again.tick())
        self.assertEqual(again.run("watches", forced=True), "kész", "kérésre azért lefut")
        again.set_enabled("watches", True)
        self.clock.now += 61 * 60
        self.assertEqual(again.tick(), "watches")
        self.assertTrue(self.make().status()["jobs"][0]["on"])

    def test_active_hours(self):
        def at(hour: int) -> float:
            return dt.datetime(2026, 10, 15, hour, 30).timestamp()

        self.assertTrue(within_hours("08:00-20:00", at(12)))
        self.assertFalse(within_hours("08:00-20:00", at(21)))
        self.assertTrue(within_hours("22:00-06:00", at(23)), "éjfélen átnyúló időszak")
        self.assertFalse(within_hours("22:00-06:00", at(12)))
        self.assertTrue(within_hours("", at(3)), "üres: bármikor")
        self.assertTrue(within_hours("valami", at(3)), "hibás beállítás: bármikor")
        self.cfg.active_hours = "08:00-20:00"
        self.clock.now = at(21)
        self.last_activity = self.clock.now - 3600
        self.assertIn("aktív órákon kívül", self.make().blocked_reason())


# --- figyelők és értesítések ------------------------------------------------------------------------

class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.results: list[list[dict]] = []
        self.searches: list[tuple] = []
        self.watches = WatchList(Path(self.tmp.name) / "figyelok.json", self.search)

    def tearDown(self):
        self.tmp.cleanup()

    def search(self, query, site, recency):
        self.searches.append((query, site, recency))
        return [{"title": h["title"], "url": h["href"], "snippet": h["body"]} for h in self.results.pop(0)]

    def test_first_check_remembers_then_only_new_ones_count(self):
        watch = self.watches.add("python fejlesztő állás Debrecen", "https://www.profession.hu/allasok", "hét")
        self.assertEqual((watch.site, watch.recency), ("profession.hu", "hét"))
        self.results = [[hit(1), hit(2)], [hit(2), hit(1), hit(3)], [hit(3)]]
        self.assertEqual(self.watches.check(watch), [], "az első futáskor csak megjegyzi")
        new = self.watches.check(watch)
        self.assertEqual([n["title"] for n in new], ["Állás 3"])
        self.assertEqual(self.watches.check(watch), [])
        self.assertEqual(self.searches[0], ("python fejlesztő állás Debrecen", "profession.hu", "hét"))
        reloaded = WatchList(Path(self.tmp.name) / "figyelok.json", self.search)
        self.assertEqual(len(reloaded.all()[0].hits), 1, "megmarad újraindítás után is")
        self.assertIn("Állás 3", reloaded.summary())

    def test_add_validates_and_dedupes(self):
        first = self.watches.add("RTX 3080", "hardverapro.hu")
        self.assertEqual(self.watches.add("rtx 3080", "hardverapro.hu", "hónap").id, first.id)
        self.assertEqual(self.watches.find(first.id).recency, "hónap")
        with self.assertRaises(WatchError):
            self.watches.add("   ")
        with self.assertRaises(WatchError):
            self.watches.add("valami", "nem egy oldal")
        self.assertEqual(self.watches.remove("rtx").id, first.id)
        self.assertEqual(self.watches.all(), [])
        self.assertEqual(normalize_url("https://A.hu/x/?utm_source=y&id=2#top"), "https://a.hu/x?id=2")

    def test_background_run_notifies_and_stops_when_you_speak(self):
        a = self.watches.add("első")
        b = self.watches.add("második")
        for watch in (a, b):     # mindkettő már látott egyszer
            self.results.append([hit(1)])
            self.watches.check(watch)
        notes = []
        checks = {"n": 0}

        class Ctx:
            def check(self):
                checks["n"] += 1
                if checks["n"] > 1:
                    raise Preempted()

            def notify(self, kind, title, text, **extra):
                notes.append((kind, title, text, extra))

        self.results = [[hit(1), hit(5)]]
        with self.assertRaises(Preempted):
            self.watches.run(Ctx())
        self.assertEqual(len(notes), 1, "az első figyelő még lefutott, a második már nem")
        self.assertEqual(notes[0][0], "watch")
        self.assertIn("Állás 5", notes[0][2])
        self.assertEqual(notes[0][3]["url"], "https://allas.hu/allas/5?utm_source=x")


class NoticeTests(unittest.TestCase):
    def test_add_list_read_and_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            published = []
            board = NoticeBoard(Path(tmp) / "e.json", publish=published.append)
            first = board.add("watch", "Új találat", "Új állás", url="https://x.hu")
            board.add("stale", "Régi", "Régóta fent")
            self.assertEqual(published[0]["id"], first["id"])
            self.assertEqual([n["title"] for n in board.list()], ["Régi", "Új találat"], "a legújabb elöl")
            self.assertEqual(board.unread_count(), 2)
            self.assertEqual(board.mark_read([first["id"]]), 1)
            self.assertEqual([n["title"] for n in NoticeBoard(Path(tmp) / "e.json").list(unread_only=True)],
                             ["Régi"])

    def test_sold_notice_keeps_its_item_and_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = NoticeBoard(Path(tmp) / "e.json")
            actions = [{"action": "sold", "where": "vinted", "delist": True, "label": "Eladva itt, vedd le a többiről"}]
            board.add("sold", "Elkelt? Farmer", "A Vinteden eladottnak látszik.", item="a1b2c3d4", platform="vinted",
                      actions=actions)
            notice = NoticeBoard(Path(tmp) / "e.json").list()[0]
            self.assertEqual((notice["kind"], notice["item"], notice["platform"]), ("sold", "a1b2c3d4", "vinted"))
            self.assertEqual(notice["actions"], actions)


# --- fent lévő hirdetések háttérellenőrzése (elkelt-e, eltűnt-e) ---------------------------------------------

def listing_page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang='hu'><head><meta charset='utf-8'><title>{title}</title></head>"
            f"<body><header><a href='/'>Főoldal</a></header><main>{body}</main></body></html>")


LISTINGS = {
    "/items/1001-zara-blezer": (200, listing_page(
        "Zara blézer | Vinted",
        "<h1>Zara blézer</h1><p>4 500 Ft</p><button>Megjelölés eladottként</button><button>Mark as sold</button>"
        "<a href='/items/7-masik'><span>Eladva</span></a>"
        "<p>Leírás: a párja már eladva, ez az utolsó darab, egyszer hordott.</p>")),
    "/items/2001-zara-blezer": (200, listing_page("Zara blézer - Jófogás", "<h1>Zara blézer</h1><p>4 500 Ft</p>")),
    "/items/1002-mango-kabat": (200, listing_page(
        "Mango kabát | Vinted", "<h1>Mango kabát</h1><div class='badge'>Eladva</div><p>9 000 Ft</p>")),
    "/items/2002-mango-kabat": (200, listing_page("Mango kabát - Jófogás", "<h1>Mango kabát</h1><p>9 000 Ft</p>")),
    "/items/1003-hm-polo": (404, listing_page("Az oldal nem található", "<h1>Hoppá! Ez az oldal nem létezik.</h1>")),
    "/items/1004-levis-farmer": (302, "/catalog"),
    "/catalog": (200, listing_page("Katalógus", "<h1>Katalógus</h1><p>Sok minden.</p>")),
    "/items/1005-nike-cipo": (200, listing_page("Nike cipő", "<iframe src='/captcha/frame'></iframe>")),
    "/items/1006-adidas-polo": (302, "/member/login?next=/items/1006"),
    "/member/login": (200, listing_page("Belépés", "<h1>Lépj be</h1>")),
}


class ListingSite:
    """Helyi „hirdetési oldal”: fent lévő, eladott, törölt (404), máshova irányító és captchás hirdetés."""

    def __init__(self):
        self.hits: list[str] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                site.hits.append(path)
                status, body = LISTINGS.get(path, (404, listing_page("Nincs", "<h1>Nem található</h1>")))
                if status == 302:
                    self.send_response(302)
                    self.send_header("Location", body)
                    self.end_headers()
                    return
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}{path}"

    def visits(self) -> list[str]:
        return [h for h in self.hits if h.startswith("/items/")]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class ListingClassifyTests(unittest.TestCase):
    URL = "https://www.vinted.hu/items/4455667-zara-blezer"

    def test_states_from_the_page(self):
        live = {"url": self.URL + "?referrer=catalog", "title": "Zara blézer", "text": "Zara blézer 4 500 Ft",
                "short": ["Zara blézer", "4 500 Ft"], "labels": ["Megjelölés eladottként", "Profilom"]}
        self.assertEqual(classify(live, self.URL, "Zara blézer", "vinted"), (LIVE, ""))
        self.assertEqual(classify({**live, "short": ["Sold"]}, self.URL, "Zara blézer", "vinted")[0], SOLD)
        self.assertEqual(classify({**live, "short": ["Foglalt"]}, self.URL)[0], SOLD)
        self.assertEqual(classify({**live, "short": ["Sold by Kata"]}, self.URL)[0], LIVE)
        moved = {**live, "url": "https://www.vinted.hu/catalog?search_text=zara"}
        self.assertEqual(classify(moved, self.URL), (GONE, "a hirdetés címe máshova irányít"))
        missing = {**live, "title": "Az oldal nem található", "text": "Hoppá"}
        self.assertEqual(classify(missing, self.URL, "Zara blézer")[0], GONE)
        own = {**live, "title": "Zara blézer, nem található hiba", "text": ""}
        self.assertEqual(classify(own, self.URL, "Zara blézer")[0], LIVE, "a saját címe van az oldalon: fent van")
        self.assertEqual(classify({**live, "captcha": True}, self.URL)[0], BLOCKED)
        self.assertEqual(classify({**live, "labels": ["Sign up | Log in", "Log in"]}, self.URL, "", "vinted"),
                         (BLOCKED, "ki vagy jelentkezve"))
        self.assertEqual(classify({**live, "url": "https://www.jofogas.hu/belepes?next=x"},
                                  "https://www.jofogas.hu/zara_123456.htm")[0], BLOCKED)


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class ListingCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = ListingSite()
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = make_cfg(Path(cls.tmp.name))
        cfg.browser.headless = True
        cls.browser = BrowserController(cfg.browser, allow_local=True)
        cls.actions: list[dict] = []
        cls.browser.on_action = cls.actions.append

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.site.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.clock = Clock()
        names = {"vinted": "Vinted", "jofogas": "Jófogás", "vatera": "Vatera", "hardverapro": "Hardverapró"}
        self.platforms = {key: Platform(key, name, "", "", "", ("127.0.0.1",), "") for key, name in names.items()}
        self.store = ItemStore(Path(self.dir.name), platform_keys=list(self.platforms))
        self.checker = ListingChecker(self.store, self.platforms, lambda: self.browser, clock=self.clock,
                                      rng=random.Random(7))
        self.notices: list[tuple] = []
        self.site.hits.clear()
        self.actions.clear()

    def tearDown(self):
        self.dir.cleanup()

    def notify(self, kind, title, text, **extra):
        self.notices.append((kind, title, text, extra))

    def posted(self, title: str, where: dict[str, str]) -> str:
        item = self.store.create(targets=list(where), title=title, price=4500)
        for key, path in where.items():
            self.store.mark(item.id, key, POSTED, url=self.site.url(path))
            self.store.get(item.id).platforms[key]["at"] = self.clock.now - 2 * 86400
        return item.id

    def test_live_sold_and_gone_once_a_day_without_clicking(self):
        live = self.posted("Zara blézer", {"vinted": "/items/1001-zara-blezer", "jofogas": "/items/2001-zara-blezer"})
        sold = self.posted("Mango kabát", {"vinted": "/items/1002-mango-kabat", "jofogas": "/items/2002-mango-kabat"})
        gone = self.posted("H&M póló", {"jofogas": "/items/1003-hm-polo"})
        moved = self.posted("Levi's farmer", {"hardverapro": "/items/1004-levis-farmer"})
        fresh = self.store.create(targets=["vatera"], title="Friss")
        self.store.mark(fresh.id, "vatera", POSTED, url=self.site.url("/items/1001-zara-blezer"))
        self.store.get(fresh.id).platforms["vatera"]["at"] = self.clock.now - 3600
        self.assertEqual(len(self.checker.due()), 6, "a ma feltöltöttet még nem nézi meg")
        result = self.checker.run(10, notify=self.notify)
        self.assertEqual(result, "6 hirdetést néztem meg: 3 fent van, 1 elkeltnek látszik, 2 már nincs fent.")
        states = {(i.id, k): e.get("check") for i in self.store.all() for k, e in i.platforms.items()}
        self.assertEqual(states[(live, "vinted")], LIVE, "a gomb és a hosszú leírás „eladva” szava nem számít")
        self.assertEqual((states[(sold, "vinted")], states[(sold, "jofogas")]), (SOLD, LIVE))
        self.assertEqual((states[(gone, "jofogas")], states[(moved, "hardverapro")]), (GONE, GONE))
        self.assertIsNone(states[(fresh.id, "vatera")])
        kinds = sorted((n[3]["item"], n[3]["platform"], n[3]["state"]) for n in self.notices)
        self.assertEqual(kinds, sorted([(sold, "vinted", SOLD), (gone, "jofogas", GONE), (moved, "hardverapro", GONE)]))
        self.assertTrue(all(n[0] == "sold" for n in self.notices))
        sold_notice = next(n for n in self.notices if n[3]["item"] == sold)
        self.assertIn("a Vinteden eladottnak látszik", sold_notice[2])
        self.assertIn("innen is leveszem: Jófogás", sold_notice[2])
        self.assertEqual(sold_notice[3]["actions"], [{"action": "sold", "where": "vinted", "delist": True,
                                                      "label": "Eladva itt, vedd le a többiről"}])
        gone_notice = next(n for n in self.notices if n[3]["item"] == gone)
        self.assertEqual(gone_notice[3]["actions"][0]["label"], "Eladva itt")
        self.assertEqual([a for a in self.actions if a.get("do") != "goto"], [], "semmire nem kattintott")
        self.assertEqual(len(self.site.visits()), 6)

        self.assertEqual(self.checker.run(10, notify=self.notify), "Nincs ma megnézendő hirdetés.")
        self.assertEqual(len(self.site.visits()), 6, "naponta legfeljebb egyszer")
        self.clock.now += 86400 + 1
        self.checker.run(10, notify=self.notify)
        self.assertEqual(len(self.notices), 3, "ugyanarról nem szól újra")
        self.assertEqual(len(self.site.visits()), 13, "egy nap múlva újra (a friss is)")

    def test_a_captcha_or_a_login_stops_the_whole_run(self):
        for path in ("/items/1005-nike-cipo", "/items/1006-adidas-polo"):
            self.notices.clear()
            self.site.hits.clear()
            first = self.posted("Nike cipő", {"vinted": path})
            second = self.posted("Puma cipő", {"jofogas": path})
            result = self.checker.run(5, notify=self.notify)
            self.assertTrue(result.startswith("Abbahagytam"), result)
            self.assertEqual(len(self.site.visits()), 1, "az első akadálynál abbahagyja")
            self.assertEqual([n[0] for n in self.notices], ["error"])
            self.assertIn("holnap újra megpróbálom", self.notices[0][2])
            for item_id in (first, second):
                self.assertNotIn("checked_at", next(iter(self.store.get(item_id).platforms.values())))
            for item_id in (first, second):
                self.store.mark(item_id, sold=True)

    def test_interrupt_between_listings(self):
        self.posted("Zara blézer", {"vinted": "/items/1001-zara-blezer", "jofogas": "/items/2001-zara-blezer"})
        calls = []

        def check():
            calls.append(1)
            if len(calls) == 2:
                raise Preempted()

        with self.assertRaises(Preempted):
            self.checker.run(5, check=check, notify=self.notify)
        self.assertEqual(len(self.site.visits()), 1)
        self.assertEqual(len(self.checker.due()), 1, "a megnézett kimarad, a másik esedékes marad")


# --- Szünet és Folytatás ----------------------------------------------------------------------------

class PauseTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.fake.vram_fit = lambda ctx: 1.0
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        self.cfg.assistant.show_timings = False
        self.ui = RecordingUI()
        self.app = JarvisApp(self.cfg, self.ui, use_voice=False, use_speech=False, use_wake_word=False)
        self.assertTrue(self.app.start())

    def tearDown(self):
        self.app.shutdown()
        self.fake.close()
        self.tmp.cleanup()

    def wait_idle(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        while self.app.busy and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.app.busy)

    def unloads(self) -> list[str]:
        return [b["model"] for m, p, b in self.fake.requests if p == "/api/generate" and b.get("keep_alive") == 0]

    def test_pause_frees_every_model_and_resume_brings_them_back(self):
        self.app.start_agent()
        self.wait_idle()
        self.fake.ps = {"models": [{"name": "qwen3.8:27b"}, {"name": "llama3.2:3b"}]}
        self.app.submit_pause()
        self.wait_idle()
        self.assertTrue(self.app.paused)
        self.assertEqual(sorted(self.unloads()), ["llama3.2:3b", "qwen3.8:27b"], "az Ollamában minden modell kiürül")
        self.assertIn("Szünetelek. Leállítottam:", [e[1] for e in self.ui.of("notice")][-1])
        self.assertIn(("state", "paused", ""), self.ui.events)
        self.assertEqual(self.app.scheduler.blocked_reason(), "szünet", "szünet alatt háttérmunka sincs")
        self.assertTrue(self.app.describe()["paused"])

        self.app.submit_text("Mennyi az idő?")
        self.wait_idle()
        self.assertEqual([e[1] for e in self.ui.of("notice")][-1], PAUSED_REPLY)
        self.assertEqual(self.fake.chat_bodies(), [], "szünetben nem kérdezi a modellt")

        loads_before = sum(1 for m, p, b in self.fake.requests if p == "/api/generate" and b.get("keep_alive") != 0)
        self.app.submit_resume()
        self.wait_idle()
        self.assertFalse(self.app.paused)
        loads = sum(1 for m, p, b in self.fake.requests if p == "/api/generate" and b.get("keep_alive") != 0)
        self.assertEqual(loads, loads_before + 1, "a nyelvi modellt újra betölti")
        self.assertEqual([e[1] for e in self.ui.of("notice")][-1], "Újra itt vagyok.")
        self.fake.chat_script = [reply("Fél kettő van.")]
        self.app.submit_text("Mennyi az idő?")
        self.wait_idle()
        self.assertEqual([e[1] for e in self.ui.of("assistant_end")][-1], "Fél kettő van.")

    def test_pause_drops_waiting_requests(self):
        self.app._outstanding = 0
        self.app.submit_text("első")
        self.app.submit_text("második")
        self.app.submit_pause()
        self.assertEqual(self.app.requests.qsize(), 1, "csak a szünet maradt a sorban")
        self.assertEqual(self.app._outstanding, 1)

    def test_a_dropped_background_job_does_not_block_the_scheduler(self):
        self.app.scheduler.submitted = "watches"          # a munka a sorban vár…
        self.app._submit(("job", {"key": "watches", "forced": False}))
        self.app.submit_pause()                            # …de jön a Szünet, és eldobja
        self.assertIsNone(self.app.scheduler.submitted, "később újra sorra kerülhet")

    def test_voice_and_typed_pause_phrases(self):
        for text in ("Pihenj!", "Jarvis, szünet.", "engedd el a videókártyát"):
            self.assertTrue(is_pause_request(text), text)
        for text in ("pihenj egy kicsit és mondd meg az időt", "Mi a szünet a futballban?"):
            self.assertFalse(is_pause_request(text), text)

    def test_warm_up_prepares_the_chat_prompt(self):
        self.app.start_agent()
        deadline = time.monotonic() + 5
        while self.fake.warm_ups == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.fake.warm_ups, 1)
        body = next(b for m, p, b in self.fake.requests if p == "/api/chat")
        self.assertEqual(body["messages"][0]["content"], self.app.assistant.system_message(),
                         "ugyanúgy kezdődik, mint az igazi kérések (a dátummal együtt)")
        self.assertEqual(len(body["tools"]), len(self.app.toolbox.schemas(self.app.assistant.tool_names)),
                         "ugyanazokkal az eszközökkel, mint a valódi kérdések, különben nem közös az eleje")

    def warm_bodies(self, count: int, timeout: float = 5.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while self.fake.warm_ups < count and time.monotonic() < deadline:
            time.sleep(0.02)
        return [b for m, p, b in self.fake.requests if p == "/api/chat" and is_warm_up(b)]

    def test_switching_to_the_work_menu_warms_its_prompt(self):
        # A Munka menü eleje más, mint a Beszélgetésé: ha átváltasz, és Jarvis épp nem dolgozik, előre
        # feldolgoztatja, szó szerint úgy, ahogy az első igazi kérés kezdődik.
        self.app.set_mode("work")
        [warm] = self.warm_bodies(1)
        self.assertEqual(warm["messages"][0]["content"], self.app.work_assistant.system_message())
        self.fake.chat_script = [reply("Két tárgyad van.")]
        self.app.answer("Hány tárgyam van?", mode="work")
        real = self.fake.chat_bodies()[-1]
        self.assertEqual(warm["messages"][0], real["messages"][0])
        self.assertEqual(warm["tools"], real["tools"], "ugyanazok az eszközök, ugyanabban a sorrendben")
        self.assertEqual((warm["think"], warm["options"]["num_ctx"], warm["options"]["temperature"]),
                         (real["think"], real["options"]["num_ctx"], real["options"]["temperature"]))
        self.app.set_mode("work")                      # nem váltott menüt: nem melegít újra
        self.app.set_mode("chat")
        time.sleep(0.2)
        self.assertEqual(self.fake.warm_ups, 1)

    def test_warm_up_follows_the_request_and_waits_for_nobody(self):
        self.fake.chat_script = [reply("Szia!")]
        self.app.answer("Szia", mode="chat")
        self.app._warm_up("chat")
        [warm] = self.warm_bodies(1)
        real = self.fake.chat_bodies()[-1]
        self.assertEqual(warm["messages"][:-1], [*real["messages"], self.app.assistant.history[-1]],
                         "az eddigi beszélgetés is benne van: a következő kérdésnél csak az új rész kell")
        # ha már vár a sorban egy kérésed, a melegítés csak feltartaná
        self.app.requests.put(("text", {"text": "Szia", "mode": "chat"}))
        self.app._warm_up("chat")
        self.app.set_mode("work")
        time.sleep(0.2)
        self.assertEqual(self.fake.warm_ups, 1)
        self.assertEqual(self.app._request_mode("text", {"text": "x", "mode": "work"}), "work")
        self.assertEqual(self.app._request_mode("work", {}), "work")
        self.assertEqual(self.app._request_mode("voice", "wake"), "work", "a hang az elöl lévő menübe megy")

    def test_stale_listings_are_mentioned_once_a_week(self):
        work = self.app.work
        old = work.store.create(targets=["vinted"], prompt="farmer")
        work.store.update(old.id, title="Farmer", price=5000)
        work.store.mark(old.id, "vinted", POSTED)
        work.store.get(old.id).platforms["vinted"]["posted_at"] = time.time() - 20 * 86400
        fresh = work.store.create(targets=["vinted"], prompt="póló")
        work.store.mark(fresh.id, "vinted", POSTED)
        self.assertEqual([i.id for i in work.stale_items(14)], [old.id])
        result = self.app.scheduler.run("stale", forced=True)
        self.assertIn("1 régóta fent lévő", result)
        notice = self.app.notices.list()[0]
        self.assertEqual(notice["kind"], "stale")
        self.assertIn("Farmer (5 000 Ft)", notice["text"])
        self.assertEqual(work.stale_items(14), [], "egy hétig nem szól újra")

    def test_listing_check_job_needs_the_browser_and_a_posted_listing(self):
        job = self.app.scheduler.jobs["listings"]
        self.assertTrue(job.needs_browser and not job.needs_models)
        self.assertFalse(job.available(), "nincs fent lévő hirdetés")
        store = self.app.work.store
        item = store.create(targets=["vinted"], title="Farmer", price=5000)
        store.mark(item.id, "vinted", POSTED, url="https://www.vinted.hu/items/1234567-farmer")
        store.get(item.id).platforms["vinted"]["at"] = time.time() - 20 * 86400
        self.assertTrue(job.available())
        self.assertEqual(job.every_minutes(), 24 * 60.0)
        self.assertEqual([i.id for i in self.app.work.stale_items(14)], [item.id])
        store.set_check(item.id, "vinted", SOLD)
        self.assertEqual(self.app.work.stale_items(14), [], "erről már szólt a háttérellenőrzés")
        self.cfg.scheduler.listing_checks = 0
        self.assertEqual(job.every_minutes(), 0, "kikapcsolható")


# --- okosabb keresés: oldalra és frissességre szűrve, több oldal egy lépésben ------------------------------

PAGES = {
    "a.html": "<html><head><title>Python állások</title></head><body><nav>Menü</nav>"
              "<p>Ez egy hosszú bevezető a cégről, ami nem a kérdésről szól, csak kitöltő szöveg itt.</p>"
              "<p>Python fejlesztő állás Debrecenben, bruttó 900 ezer forint, hibrid munkavégzés.</p></body></html>",
    "b.html": "<html><head><title>Másik oldal</title></head><body>"
              "<p>Debreceni Python fejlesztői pozíció junioroknak, teljes munkaidőben, azonnali kezdéssel.</p>"
              "</body></html>",
}


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.patch = install_fake_ddgs()
        self.patch.start()
        self.dir = tempfile.TemporaryDirectory()
        for name, content in PAGES.items():
            (Path(self.dir.name) / name).write_text(content, encoding="utf-8")
        handler = functools.partial(_QuietHandler, directory=self.dir.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.cfg = WebCfg(allow_local=True, timeout=5)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()
        self.patch.stop()

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/{name}"

    def test_site_and_recency_filters(self):
        FakeDDGS.results = [[hit(1)]]
        results = web.search("python állás", self.cfg, site="https://www.profession.hu/x", recency="hét",
                             max_results=8)
        self.assertEqual(FakeDDGS.calls[0]["query"], "python állás site:profession.hu")
        self.assertEqual((FakeDDGS.calls[0]["timelimit"], FakeDDGS.calls[0]["max_results"]), ("w", 8))
        text = web.format_results("python állás", results, "profession.hu", "hét")
        self.assertIn("(profession.hu, az elmúlt hét)", text)
        self.assertEqual(web.recency_code("Hónap"), "m")
        self.assertIsNone(web.recency_code("tegnapelőtt"))
        self.assertEqual(web.clean_site("nem oldal"), "")

    def test_research_reads_several_pages_at_once(self):
        FakeDDGS.results = [[{"title": "A", "href": self.url("a.html"), "body": "a"},
                             {"title": "B", "href": self.url("b.html"), "body": "b"},
                             {"title": "B2", "href": self.url("b.html?masodik"), "body": "b"},
                             {"title": "Hiányzó", "href": self.url("nincs.html").replace("127.0.0.1", "localhost"),
                              "body": "c"}]]
        text = web.research("python fejlesztő állás Debrecen", self.cfg, pages=3, per_page_chars=200)
        self.assertIn("Kutatás: „python fejlesztő állás Debrecen” (3 oldal)", text)
        self.assertIn("bruttó 900 ezer forint", text, "a kérdéshez illő bekezdés")
        self.assertNotIn("hosszú bevezető", text, "a lényegtelen bevezetőt kihagyja")
        self.assertIn("Debreceni Python fejlesztői pozíció", text, "állások ~ állás: a toldalék nem számít")
        self.assertIn("Nem sikerült letölteni: " + self.url("nincs.html").replace("127.0.0.1", "localhost"), text)
        self.assertNotIn("masodik", text, "ugyanarról az oldalról legfeljebb kettőt: több forrás legyen")

    def test_tools_and_watch_tools_in_the_chat(self):
        fake = FakeOllama()
        fake.vram_fit = lambda ctx: 1.0
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(Path(tmp), fake.url)
            app = JarvisApp(cfg, RecordingUI(), use_voice=False, use_speech=False, use_wake_word=False)
            try:
                self.assertTrue(app.start())
                names = set(app.toolbox.names())
                self.assertTrue({"web_research", "watch_add", "watch_list", "watch_remove", "watch_check"} <= names)
                self.assertIn("watch_add", app.assistant.tool_names, "a Beszélgetésben is")
                result = app.toolbox.call("watch_add", {"query": "python állás Debrecen", "site": "profession.hu"})
                self.assertIn("Figyelő beállítva", result)
                FakeDDGS.results = [[hit(1)], [hit(1), hit(2)]]
                self.assertIn("most néztem meg először", app.toolbox.call("watch_check", {}))
                self.assertIn("1 új: Állás 2", app.toolbox.call("watch_check", {}))
                self.assertIn("python állás Debrecen", app.toolbox.call("watch_list", {}))
                self.assertTrue(app.scheduler.jobs["watches"].available())
            finally:
                app.shutdown()
                fake.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
