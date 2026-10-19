"""A Szünet gomb, a háttérmunka (ütemező), a figyelők, az értesítések és az okosabb webes keresés
tesztjei. Internet, modell és GPU nélkül futnak (hamis Ollama, hamis kereső, helyi tesztoldalak).

Futtatás:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import datetime as dt
import functools
import importlib.machinery
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_offline import FakeOllama, make_cfg, reply  # noqa: E402
from test_streaming import RecordingUI  # noqa: E402
from test_work import _QuietHandler  # noqa: E402

from jarvis_core import web  # noqa: E402
from jarvis_core.app import PAUSED_REPLY, JarvisApp, is_pause_request  # noqa: E402
from jarvis_core.config import SchedulerCfg, WebCfg  # noqa: E402
from jarvis_core.notices import NoticeBoard  # noqa: E402
from jarvis_core.scheduler import Hooks, Job, Preempted, Scheduler, within_hours  # noqa: E402
from jarvis_core.selling import POSTED  # noqa: E402
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
