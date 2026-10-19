"""A webes kutatás javításai: a gépi letöltést tiltó (403) oldalak a saját böngészővel, az ismételt
hívások kiszűrése, a mai dátum a promptban, és hogy a lépéskeret végén is legyen válasz.

Futtatás a projekt mappájából:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_offline import FakeOllama, make_cfg, reply, tool_reply  # noqa: E402
from test_web_browser import HAS_CHROMIUM  # noqa: E402

from jarvis_core import web  # noqa: E402
from jarvis_core.assistant import (REPEAT_NOTE, TOO_MANY_STEPS, WRAP_UP, Assistant, call_key,  # noqa: E402
                                   date_line)
from jarvis_core.browser import BrowserController, BrowserError, is_closed_error  # noqa: E402
from jarvis_core.config import load_config  # noqa: E402
from jarvis_core.llm import OllamaClient  # noqa: E402
from jarvis_core.tools import Toolbox  # noqa: E402

ARTICLE = ("<html><head><meta charset=\"utf-8\"><title>{title}</title></head><body><article><h1>{title}</h1>"
           "<p>{body} Ez egy elég hosszú bekezdés ahhoz, hogy a kinyerő főszövegnek tekintse, és ne "
           "gondolja, hogy az oldal JavaScripttel töltődik be. Még egy mondat a biztonság kedvéért, "
           "hogy kétszáz karakternél több legyen a szöveg.</p></article></body></html>")


class Site:
    """Helyi oldal: a /tiltott/ alatt 403 (mint egy robotvédelem), a /nincs/ alatt 404."""

    def __init__(self):
        self.hits: list[str] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                site.hits.append(self.path)
                if self.path.startswith("/tiltott"):
                    status, body = 403, "<html><body>Access denied</body></html>"
                elif self.path.startswith("/nincs"):
                    status, body = 404, "<html><body>Nincs ilyen</body></html>"
                else:
                    name = self.path.strip("/").split(".")[0] or "fooldal"
                    status, body = 200, ARTICLE.format(title=f"Oldal {name}", body=f"Ébresztőóra {name} 3990 Ft.")
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path: str, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.server.server_address[1]}/{path}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class FakeBrowser:
    """A saját böngésző helyett: feljegyzi, mit olvasott el, és szöveget ad vissza."""

    def __init__(self, fail: str = ""):
        self.read: list[str] = []
        self.fail = fail

    def fetch_text(self, url: str) -> tuple[str, str, str]:
        self.read.append(url)
        if self.fail:
            raise BrowserError(self.fail)
        return url, "Böngészővel olvasva", "Ébresztőóra, kapható 4990 Ft-ért, a böngészőben látszik az ár."

    def read_url(self, url: str) -> str:
        final, title, text = self.fetch_text(url)
        return web.format_page(title, final, text, 6000, js_hint=False)

    def close(self) -> None:
        pass

    @property
    def running(self) -> bool:
        return False


class ReadFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = Site()

    @classmethod
    def tearDownClass(cls):
        cls.site.close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name))
        self.cfg.web.allow_local = True
        self.box = Toolbox(self.cfg)
        self.browser = FakeBrowser()
        self.box._browser = self.browser
        self.site.hits.clear()

    def tearDown(self):
        self.box._browser = None
        self.tmp.cleanup()

    @unittest.skipUnless("browser_open" in Toolbox(load_config(ROOT / "config.toml")).names(),
                         "a Playwright nincs telepítve")
    def test_403_page_is_read_with_the_browser_and_remembered(self):
        result = self.box.call("read_webpage", {"url": self.site.url("tiltott/ora.html")})
        self.assertIn("Böngészővel olvasva", result)
        self.assertIn("a saját böngészőmmel olvastam el", result)
        self.assertEqual(self.browser.read, [self.site.url("tiltott/ora.html")])
        self.assertIn("127.0.0.1", self.box.blocked_hosts())
        # ugyanarról az oldalról a következőt egyből a böngésző olvassa (nincs újabb 403-as próbálkozás)
        hits = len(self.site.hits)
        self.box.call("read_webpage", {"url": self.site.url("masik.html")})
        self.assertEqual(len(self.site.hits), hits, "a sima letöltést meg sem próbálja")
        self.assertEqual(len(self.browser.read), 2)

    def test_missing_page_is_not_retried_with_the_browser(self):
        result = self.box.call("read_webpage", {"url": self.site.url("nincs/ora.html")})
        self.assertTrue(result.startswith("HIBA"))
        self.assertIn("404", result)
        self.assertIn("Ne próbáld újra", result)
        self.assertEqual(self.browser.read, [])

    def test_browser_failure_is_explained(self):
        self.box._browser = FakeBrowser(fail="captcha")
        result = self.box.call("read_webpage", {"url": self.site.url("tiltott/x.html")})
        self.assertTrue(result.startswith("HIBA"))
        self.assertIn("403", result)
        self.assertIn("A saját böngészőmmel sem sikerült: captcha", result)
        self.assertIn("válassz másik forrást", result)

    def test_without_browser_the_plain_error_stays(self):
        self.cfg.browser.enabled = False
        box = Toolbox(self.cfg)
        result = box.call("read_webpage", {"url": self.site.url("tiltott/x.html")})
        self.assertEqual(result, "HIBA: Az oldal hibát adott (403).")

    def test_error_kinds(self):
        with self.assertRaises(web.WebError) as blocked:
            web.fetch_page(self.site.url("tiltott/a.html"), self.cfg.web)
        self.assertEqual(blocked.exception.kind, "blocked")
        with self.assertRaises(web.WebError) as missing:
            web.fetch_page(self.site.url("nincs/a.html"), self.cfg.web)
        self.assertEqual(missing.exception.kind, "http")
        self.assertEqual(web.host_of("https://www.Alza.hu/ebreszto"), "alza.hu")

    @unittest.skipUnless("browser_open" in Toolbox(load_config(ROOT / "config.toml")).names(),
                         "a Playwright nincs telepítve")
    def test_research_replaces_blocked_pages_and_uses_the_browser_last(self):
        hits = [
            {"title": "Tiltott bolt", "url": self.site.url("tiltott/bolt.html"), "snippet": "ébresztőóra"},
            {"title": "A", "url": self.site.url("a.html", "localhost"), "snippet": "a"},
            {"title": "B", "url": self.site.url("b.html"), "snippet": "b"},
            {"title": "C", "url": self.site.url("c.html"), "snippet": "c"},
        ]
        with mock.patch.object(web, "search", return_value=hits):
            text = self.box.web_research("ébresztőóra ár", pages=2)   # (ddgs nélkül nincs regisztrálva)
        self.assertIn("Oldal a", text)
        self.assertIn("Oldal b", text, "a kieső oldal helyett a következő találatot olvasta el")
        self.assertNotIn("Oldal c", text)
        self.assertIn("127.0.0.1", self.box.blocked_hosts(), "megjegyezte, hogy nem enged be")
        self.assertEqual(self.browser.read, [], "elég oldal volt, a lassú böngészőre nem volt szükség")

        self.browser.read.clear()
        only_blocked = [hits[0], {"title": "D", "url": self.site.url("tiltott/d.html"), "snippet": "d"}]
        with mock.patch.object(web, "search", return_value=only_blocked):
            text = self.box.web_research("ébresztőóra ár", pages=2)
        self.assertIn("Böngészővel olvasva", text)
        self.assertEqual(len(self.browser.read), 2, "a tiltó oldalakat a böngésző olvasta el")


class AssistantResearchTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        self.llm = OllamaClient(self.cfg.llm)
        self.box = Toolbox(self.cfg)
        self.reads: list[str] = []

        def read(url: str) -> str:
            self.reads.append(url)
            return f"Oldal: {url}\nÉbresztőóra 3990 Ft."

        self.box._tools["read_webpage"].func = read
        self.assistant = Assistant(self.cfg, self.llm, self.box, "rendszer")

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def test_today_is_in_the_system_prompt(self):
        self.fake.chat_script = [reply("ok")]
        self.assistant.ask("szia")
        system = self.fake.chat_bodies()[0]["messages"][0]["content"]
        self.assertTrue(system.startswith("rendszer"))
        self.assertTrue(system.endswith(date_line()))
        self.assertRegex(date_line(), r"^Mai dátum: \d{4}\. [a-zá-ű]+ \d{1,2}\., [a-zá-ű]+\.$")

    def test_same_page_is_not_read_twice(self):
        self.fake.chat_script = [
            tool_reply("read_webpage", {"url": "https://www.alza.hu/ora"}),
            tool_reply("read_webpage", {"url": "alza.hu/ora/"}),
            reply("Az Alzán 3990 Ft."),
        ]
        self.assertEqual(self.assistant.ask("mennyi?"), "Az Alzán 3990 Ft.")
        self.assertEqual(self.reads, ["https://www.alza.hu/ora"])
        tool_results = [m["content"] for m in self.assistant.history if m["role"] == "tool"]
        self.assertEqual(tool_results[1], REPEAT_NOTE)
        self.assertEqual(call_key("web_search", {"query": "Ébresztőóra  ÁR"}),
                         call_key("web_search", {"query": "ebresztoora ar"}))
        self.assertIsNone(call_key("browser_open", {"url": "x"}), "a böngészős lépések állapotfüggők")

    def test_budget_warning_and_answer_at_the_end(self):
        self.cfg.assistant.max_tool_rounds = 4
        seen_final = {}

        def final(body):
            seen_final["tools"] = "tools" in body
            seen_final["last"] = body["messages"][-1]
            return 200, reply("A Kaufland 2990 forintos órája a legjobb választás.")

        self.fake.chat_script = [tool_reply("read_webpage", {"url": f"https://bolt{i}.hu"}) for i in range(4)]
        self.fake.chat_script.append(final)
        answer = self.assistant.ask("melyik ébresztőórát vegyem?")
        self.assertEqual(answer, "A Kaufland 2990 forintos órája a legjobb választás.")
        self.assertTrue(seen_final["tools"], "az utolsó körben is ugyanazok az eszközök (a gyorsítótár miatt)")
        self.assertEqual(seen_final["last"], {"role": "user", "content": WRAP_UP})
        tool_results = [m["content"] for m in self.assistant.history if m["role"] == "tool"]
        self.assertIn("[Még 3 lépésed maradt.", tool_results[0])
        self.assertIn("[Még 2 lépésed maradt.", tool_results[1])
        self.assertIn("[Még 1 lépésed maradt.", tool_results[2])
        self.assertNotIn("lépésed maradt", tool_results[3], "az utolsó után már a záró kérés jön")
        self.assertNotIn(WRAP_UP, [m.get("content") for m in self.assistant.history],
                         "a záró kérés nem marad az előzményekben")

    def test_tool_call_text_at_the_end_becomes_an_honest_summary(self):
        self.cfg.assistant.max_tool_rounds = 1
        self.fake.chat_script = [tool_reply("read_webpage", {"url": "https://www.alza.hu/ora"}),
                                 reply('<tool_call>{"name": "web_search", "arguments": {}}</tool_call>')]
        answer = self.assistant.ask("melyik órát vegyem?")
        self.assertTrue(answer.startswith(TOO_MANY_STEPS))
        self.assertIn("alza.hu", answer)
        self.assertNotIn("tool_call", answer)


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class BrowserReadTests(unittest.TestCase):
    PAGES = {
        "cikk.html": ARTICLE.format(title="Cikk", body="Az óra ára 3990 Ft."),
        # robotvédelmi köztes oldal, ami másfél másodperc múlva magától továbbenged
        "varj.html": ("<html><head><title>Just a moment...</title></head><body>Checking your browser"
                      "<script>setTimeout(() => location.replace('/cikk.html'), 1500)</script></body></html>"),
        "captcha.html": "<html><head><title>Captcha</title></head><body>Bizonyítsd, hogy nem vagy robot</body></html>",
    }

    @classmethod
    def setUpClass(cls):
        import functools
        from http.server import SimpleHTTPRequestHandler

        cls.dir = tempfile.TemporaryDirectory()
        for name, html in cls.PAGES.items():
            (Path(cls.dir.name) / name).write_text(html, encoding="utf-8")

        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=cls.dir.name))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.profile = tempfile.TemporaryDirectory()
        cfg = load_config(ROOT / "config.toml").browser
        cfg.headless = True
        cfg.profile_dir = cls.profile.name
        cfg.pace = 0.0
        cls.browser = BrowserController(cfg, allow_local=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.server.shutdown()
        cls.server.server_close()
        cls.dir.cleanup()
        cls.profile.cleanup()

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/{name}"

    def test_reads_in_a_separate_tab_and_keeps_the_active_page(self):
        self.browser.open(self.url("cikk.html?aktiv"))
        before = self.browser.url
        text = self.browser.read_url(self.url("cikk.html"))
        self.assertIn("Az óra ára 3990 Ft.", text)
        self.assertEqual(self.browser.url, before, "a modell lapja nem változott")
        self.assertEqual(len([p for p in self.browser._context.pages if not p.is_closed()]), 1,
                         "a külön lap bezárult")

    def test_waits_for_a_passing_challenge_but_does_not_solve_captchas(self):
        final, title, text = self.browser.fetch_text(self.url("varj.html"))
        self.assertTrue(final.endswith("/cikk.html"))
        self.assertIn("3990", text)
        with mock.patch("jarvis_core.browser.CHALLENGE_WAIT", 1.0), \
                self.assertRaisesRegex(BrowserError, "nem kerülöm meg"):
            self.browser.fetch_text(self.url("captcha.html"))

    def test_restarts_when_the_browser_was_closed_under_it(self):
        page = self.browser._ensure()

        def closed(*args, **kwargs):
            raise RuntimeError("Page.goto: Target page, context or browser has been closed")

        page.goto = closed   # mintha az ablakot épp bezárták volna
        view = self.browser.open(self.url("cikk.html"))
        self.assertIn("Oldal: Cikk", view)
        self.assertTrue(is_closed_error(RuntimeError("Target page, context or browser has been closed")))
        self.assertFalse(is_closed_error(RuntimeError("net::ERR_NAME_NOT_RESOLVED")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
