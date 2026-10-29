"""Az olvasóprofil (browser.READER_HOSTS, cfg.reader): a Vinteden a böngészés, a keresés és egy hirdetés átnézése egy
külön, soha be nem jelentkezett profilban megy (a saját ablakában), így a gyakori olvasás nem a fiókodhoz kötődik; a
bejelentkezést kívánó munka (feltöltés, üzenetek, ajánlat) a fő profilban marad. Valódi Chromiummal, helyi oldalakon:
a fő profil sütije (a „bejelentkezés”) az olvasóprofilban nincs ott, és fordítva."""
from __future__ import annotations

import functools
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_web_browser import HAS_CHROMIUM, _QuietHandler  # noqa: E402

from jarvis_core.browser import BrowserController  # noqa: E402
from jarvis_core.config import load_config  # noqa: E402

COOKIE_JS = "() => ({text: document.cookie, title: document.title})"
PAGES = {
    "login.html": "<!doctype html><title>Belépés</title><script>document.cookie = 'fiok=zsolt; path=/'</script>",
    "lista.html": ("<!doctype html><title>Lista</title><script>document.title = 'Lista ' + document.cookie</script>"
                   "<div class='item'><a href='/items/1.html'>Lego 42115</a> <span>25 000 Ft</span></div>"
                   "<div class='item'><a href='/items/2.html'>Lego 75192</a> <span>210 000 Ft</span></div>"),
    "captcha.html": "<!doctype html><title>Just a moment...</title><p>Checking your browser</p>",
}


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class ReaderProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        for name, html in PAGES.items():
            (Path(cls.dir.name) / name).write_text(html, encoding="utf-8")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_QuietHandler, directory=cls.dir.name))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.dir.cleanup()

    def setUp(self):
        self.profiles = tempfile.TemporaryDirectory()
        cfg = load_config(ROOT / "config.toml").browser
        cfg.headless, cfg.channel, cfg.pace = True, "", 0.0
        cfg.profile_dir = str(Path(self.profiles.name) / "fo")
        cfg.reader_profile_dir = str(Path(self.profiles.name) / "olvaso")
        self.cfg = cfg
        self.browser = BrowserController(cfg, allow_local=True, confirm=lambda prompt: True)
        self.browser.reader_hosts = ("127.0.0.1",)   # (a tesztben a helyi oldal a „Vinted”)

    def tearDown(self):
        self.browser.close()
        self.profiles.cleanup()

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/{name}"

    def test_reading_goes_to_a_profile_without_the_login(self):
        """A fő profilban „bejelentkezve” (süti); az olvasóprofil ezt nem látja, és a saját profilmappájában fut. A fő
        profil olvasása (look) továbbra is a bejelentkezett."""
        self.browser.open(self.url("login.html"))
        self.assertIn("fiok=zsolt", self.browser.look(self.url("lista.html"), COOKIE_JS)["text"])
        read = self.browser.read_page(self.url("lista.html"), COOKIE_JS)
        self.assertNotIn("fiok=zsolt", read["text"], "az olvasóprofilban nincs bejelentkezés")
        rows = self.browser.read_listings(self.url("lista.html"))
        self.assertEqual(sorted(r["price"] for r in rows), [25000, 210000])
        self.assertTrue((Path(self.profiles.name) / "olvaso").is_dir())
        self.assertIsNot(self.browser._reader_page, self.browser.page, "két külön ablak")
        self.assertIn("fiok=zsolt", self.browser.look(self.url("lista.html"), COOKIE_JS)["text"], "a fő megmaradt")

    def test_other_sites_and_the_old_mode_use_the_main_profile(self):
        """Ami nem olvasóoldal (pl. a Hardverapró), és ha az olvasóprofil ki van kapcsolva (reader = false), az a fő
        profilban megy, mint régen."""
        self.browser.open(self.url("login.html"))
        self.browser.reader_hosts = ("vinted.hu",)
        self.assertIn("fiok=zsolt", self.browser.read_page(self.url("lista.html"), COOKIE_JS)["text"])
        self.browser.reader_hosts = ("127.0.0.1",)
        self.cfg.reader = False
        self.assertIn("fiok=zsolt", self.browser.read_page(self.url("lista.html"), COOKIE_JS)["text"])
        self.assertIsNone(self.browser._reader_context, "el sem indult")

    def test_a_captcha_on_the_reader_is_seen(self):
        """A robot-ellenőrzés jelzése (challenged) az olvasóprofil lapjáról jön, ha ott olvasott utoljára; egy fő
        profilbeli lépés után megint a fő lapé."""
        self.assertEqual(self.browser.read_listings(self.url("captcha.html")), [])
        self.assertTrue(self.browser.challenged())
        self.browser.open(self.url("lista.html"))
        self.assertFalse(self.browser.challenged())

    def test_a_closed_reader_window_comes_back(self):
        """Ha az olvasó ablakot bezárják, a következő olvasás újranyitja; a böngésző leállításával az is leáll."""
        self.browser.read_page(self.url("lista.html"), COOKIE_JS)
        self.browser._reader_context.close()
        self.assertEqual(self.browser.read_page(self.url("lista.html"), COOKIE_JS)["title"].split()[0], "Lista")
        self.browser.close()
        self.assertIsNone(self.browser._reader_context)


if __name__ == "__main__":
    unittest.main()
