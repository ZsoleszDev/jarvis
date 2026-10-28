"""Az automatikus frissítés (refresh.py): mikor esedékes, a második képsor, a jóváhagyás nélküli gombok köre, a
hirdetésben kézzel átírt ár átvétele, és a Hardverapró ingyenes UP gombja (egy helyi „Hardverapró” oldalon).

A Vinted levétele és újrafeltöltése egy helyi „Vinted” oldalon: test_vinted.VintedRefreshTests.

Futtatás a projekt mappájából:
    python -m unittest tests.test_refresh -v
"""
from __future__ import annotations

import datetime as dt
import functools
import random
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_offline import FakeOllama, make_cfg  # noqa: E402
from test_web_browser import HAS_CHROMIUM  # noqa: E402
from test_work import _QuietHandler, jpeg  # noqa: E402

from jarvis_core import refresh  # noqa: E402
from jarvis_core.browser import BrowserController  # noqa: E402
from jarvis_core.listings import LIVE, ListingChecker  # noqa: E402
from jarvis_core.llm import OllamaClient  # noqa: E402
from jarvis_core.selling import NOT_POSTED, POSTED, ItemError, ItemStore, Platform  # noqa: E402
from jarvis_core.tools import Toolbox  # noqa: E402
from jarvis_core.work import WorkDesk  # noqa: E402

DAY = 86400


def local(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return dt.datetime(year, month, day, hour, minute).timestamp()


class ScheduleTests(unittest.TestCase):
    def test_next_time_is_days_later_around_the_hour(self):
        base = local(2026, 10, 28, 21, 3)
        for seed in range(20):
            when = refresh.next_time(base, 7, 19, base, random.Random(seed))
            self.assertGreaterEqual(when, local(2026, 11, 4, 19))
            self.assertLess(when, local(2026, 11, 4, 19, refresh.JITTER_MINUTES), "az óra után legfeljebb 45 perccel")
        # ha már régóta fent van (a napok elteltek), a legközelebbi ilyen óra: ma, ha még nem múlt el, különben holnap
        long_ago = local(2026, 9, 1, 12)
        self.assertGreaterEqual(refresh.next_time(long_ago, 7, 22, local(2026, 10, 28, 21), random.Random(1)),
                                local(2026, 10, 28, 22))
        self.assertGreaterEqual(refresh.next_time(long_ago, 7, 8, local(2026, 10, 28, 21), random.Random(1)),
                                local(2026, 10, 29, 8))

    def test_when_text(self):
        now = local(2026, 10, 28, 21)
        self.assertEqual(refresh.when_text(local(2026, 10, 28, 22, 10), now), "ma 22 óra körül")
        self.assertEqual(refresh.when_text(local(2026, 10, 29, 19, 30), now), "holnap 19 óra körül")
        self.assertEqual(refresh.when_text(local(2026, 11, 4, 19, 5), now), "nov. 4. 19 óra körül")

    def test_where_and_how(self):
        self.assertEqual((refresh.method("vinted"), refresh.method("hardverapro")), ("relist", "up"))
        self.assertEqual((refresh.method("jofogas"), refresh.method("vatera")), ("", ""))
        self.assertIn("14 napig", refresh.UNSUPPORTED["jofogas"])

    def test_the_free_up_button_never_the_paid_one(self):
        def el(label, kind="button"):
            return {"id": 1, "kind": kind, "label": label}
        self.assertIsNone(refresh.up_button([el("Előresorolás"), el("Kiemelés vásárlása"), el("Szerkesztés")]))
        self.assertEqual(refresh.up_button([el("Előresorolás"), el("UP", "link")])["label"], "UP")
        self.assertEqual(refresh.up_button([el("UP (7)")])["label"], "UP (7)")
        self.assertIsNone(refresh.up_button([el("UP", "checkbox")]))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ItemStore(Path(self.tmp.name), max_photos=3)
        self.now = local(2026, 10, 28, 21)

    def tearDown(self):
        self.tmp.cleanup()

    def posted(self, key="vinted", price=5000):
        item = self.store.create(targets=[key], prompt="Zara kabát", title="Zara kabát", price=price)
        self.store.add_photo(item.id, jpeg())
        self.store.mark(item.id, key, POSTED, url=f"https://www.vinted.hu/items/{1000 + len(self.store.all())}-x")
        self.store.get(item.id).platforms[key]["posted_at"] = self.now - 3 * DAY
        return self.store.get(item.id)

    def test_settings_validation_and_the_first_time(self):
        item = self.posted()
        for bad in ({"days": 0}, {"days": 61}, {"days": "sok"}, {"hour": 24}, {"hour": "este"}):
            with self.assertRaises(ItemError):
                self.store.set_refresh(item.id, on=True, now=self.now, **bad)
        state = self.store.set_refresh(item.id, on=True, now=self.now).refresh
        self.assertEqual((state["days"], state["hour"], state["on"]), (refresh.DEFAULT_DAYS, refresh.DEFAULT_HOUR, True))
        # a felkerülés után 7 nappal (3 napja van fent: 4 nap múlva), 19 óra körül
        self.assertGreaterEqual(state["next"], local(2026, 11, 1, 19))
        self.assertLess(state["next"], local(2026, 11, 1, 20))
        state = self.store.set_refresh(item.id, days=2, hour=8, now=self.now).refresh
        self.assertGreaterEqual(state["next"], local(2026, 10, 29, 8), "a 2 nap már letelt: holnap reggel")
        self.assertLess(state["next"], local(2026, 10, 29, 9))
        self.assertFalse(self.store.set_refresh(item.id, on=False, now=self.now).refresh["on"])

    def test_due_only_when_on_posted_on_a_supported_site_and_not_sold(self):
        vinted, jofogas, sold = self.posted(), self.posted("jofogas"), self.posted()
        for item in (vinted, jofogas, sold):
            self.store.set_refresh(item.id, on=True, days=1, hour=0, now=self.now)
        self.store.mark(sold.id, sold=True)
        later = self.now + 3 * DAY
        self.assertEqual([i.id for i in self.store.refresh_due(later)], [vinted.id], "a Jófogáson nem frissít")
        self.assertEqual(self.store.refresh_due(self.now), [], "még nem esedékes")
        # ha levette, de az újrafeltöltés elmaradt, akkor is esedékes (nincs fent sehol)
        self.store.refresh_begin(vinted.id, ["vinted"])
        self.store.mark(vinted.id, "vinted", NOT_POSTED)
        self.assertEqual([i.id for i in self.store.refresh_due(later)], [vinted.id])

    def test_results_failures_and_retry(self):
        item = self.posted()
        self.store.set_refresh(item.id, on=True, days=7, hour=19, now=self.now)
        state = self.store.refresh_end(item.id, True, "kész", now=self.now).refresh
        self.assertEqual((state["last"], state["count"], state["fails"]), (self.now, 1, 0))
        self.assertGreaterEqual(state["next"], local(2026, 11, 4, 19))
        # ha levette, de az újrafeltöltés elmaradt: fél óra múlva újra
        self.store.refresh_begin(item.id, ["vinted"])
        state = self.store.refresh_end(item.id, False, "nem sikerült", now=self.now).refresh
        self.assertEqual((state["fails"], state["next"]), (1, self.now + refresh.REPOST_RETRY))
        # más hiba: holnap ilyenkor
        self.store.refresh_reposted(item.id, "vinted")
        state = self.store.refresh_end(item.id, False, "nem nyílt meg", now=self.now).refresh
        self.assertGreaterEqual(state["next"], local(2026, 10, 29, 19))
        self.assertTrue(state["on"])
        state = self.store.refresh_end(item.id, False, "nem nyílt meg", now=self.now).refresh
        self.assertEqual((state["fails"], state["on"]), (refresh.MAX_FAILS, False), "háromszor egymás után: kikapcsol")

    def test_the_second_photo_set(self):
        item = self.posted()
        self.store.add_photo(item.id, jpeg(color=(200, 0, 0)), alt=True)
        item = self.store.add_photo(item.id, jpeg(color=(0, 200, 0)), alt=True)
        self.assertEqual((item.photos, item.photos_alt), (["01.jpg"], ["02.jpg", "03.jpg"]), "a számozás folytatódik")
        self.assertTrue(self.store.photo_file(item.id, "03.jpg").exists())
        self.assertEqual(item.live_photos(), ["01.jpg"])
        self.assertEqual(self.store.refresh_next_set(item.id), 1)
        self.assertEqual(self.store.get(item.id).live_photos(), ["02.jpg", "03.jpg"])
        self.assertEqual(self.store.refresh_next_set(item.id), 0, "felrakásonként váltakozik")
        self.store.add_photo(item.id, jpeg(color=(0, 0, 200)), alt=True)
        with self.assertRaises(ItemError):
            self.store.add_photo(item.id, jpeg(), alt=True)    # legfeljebb max_photos egy képsorban
        item = self.store.remove_photo(item.id, "02.jpg")
        self.assertEqual(item.photos_alt, ["03.jpg", "04.jpg"])
        reloaded = ItemStore(Path(self.tmp.name), max_photos=3).get(item.id)
        self.assertEqual((reloaded.photos, reloaded.photos_alt), (["01.jpg"], ["03.jpg", "04.jpg"]))
        self.assertIn("03.jpg", reloaded.photo_checks, "a második képsor mérései is megmaradnak")

    def test_the_listing_age_survives_a_refresh(self):
        item = self.posted()
        since = item.online_since()
        self.store.refresh_begin(item.id, ["vinted"])
        self.store.mark(item.id, "vinted", NOT_POSTED)
        self.store.mark(item.id, "vinted", POSTED, url="https://www.vinted.hu/items/2000-x")   # az új hirdetés
        self.assertEqual(self.store.get(item.id).online_since(), since)

    def test_a_price_changed_on_the_site_is_taken_over(self):
        item = self.posted(price=5000)
        self.assertIsNone(self.store.adopt_live_price(item.id, "vinted", 5000), "nem változott")
        self.assertEqual(self.store.adopt_live_price(item.id, "vinted", 4500), 5000)
        item = self.store.get(item.id)
        self.assertEqual((item.price, item.platforms["vinted"]["posted_price"]), (4500, 4500))
        self.assertIn("price", item.edited)
        # ha itt is átírtad (és még nem töltötte fel újra), az a tiéd: nem írjuk felül
        self.store.update(item.id, user=True, price=4000)
        self.assertIsNone(self.store.adopt_live_price(item.id, "vinted", 3900))
        self.assertEqual(self.store.get(item.id).price, 4000)
        self.assertIsNone(self.store.adopt_live_price(item.id, "jofogas", 100), "ott nincs fent")


class PreapprovalTests(unittest.TestCase):
    """Frissítés közben a törlés és a közzététel gombjaira nem kérdez, de csak annál a tárgynál és azon az oldalon,
    és fizetősre soha."""

    def setUp(self):
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        cfg.browser.enabled = False
        self.desk = WorkDesk(cfg, OllamaClient(cfg.llm), Toolbox(cfg))

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def test_scope(self):
        desk = self.desk
        url = "https://www.vinted.hu/items/new"
        desk.current = ("a1b2c3d4", "vinted")
        self.assertFalse(desk._preapproved("Upload", url), "frissítés nélkül kérdez")
        desk.begin_refresh("a1b2c3d4", "vinted")
        for label in ("Upload", "Feltöltés", "Közzététel", "Delete", "Igen, törlöm"):
            self.assertTrue(desk._preapproved(label, url), label)
        for label in ("Kiemelés vásárlása", "Előresorolás", "Fizetés"):
            self.assertFalse(desk._preapproved(label, url), label)
        self.assertFalse(desk._preapproved("Upload", "https://www.jofogas.hu/hirdetes"), "másik oldalon nem")
        desk.current = ("ffffffff", "vinted")
        self.assertFalse(desk._preapproved("Upload", url), "másik tárgynál nem")
        desk.current = ("a1b2c3d4", "vinted")
        desk.end_refresh()
        self.assertFalse(desk._preapproved("Upload", url))


# --- egy helyi „Hardverapró”: a hirdetésed oldala az ingyenes UP gombbal --------------------------------------

def ha_page(body: str) -> str:
    return ("<!doctype html><html lang='hu'><head><meta charset='utf-8'><title>RTX 3060 Ti - HardverApró</title>"
            "</head><body><main><h1>RTX 3060 Ti</h1><p>90 000 Ft</p>" + body + "<p id='msg'></p></main></body></html>")


UP_SCRIPT = """<script>
document.getElementById('up').onclick = (e) => {
  e.preventDefault();
  const n = Number(localStorage.getItem('up') || 0);
  if (n >= %d) { document.getElementById('msg').textContent = 'Elfogyott az ingyenes UP mára.'; return; }
  localStorage.setItem('up', String(n + 1));
  document.getElementById('msg').textContent = 'A hirdetésed a lista elejére került.';
};
</script>"""

HA_PAGES = {
    "apro/rtx_3060_ti/index.html": ha_page("<div><a href='#' id='up'>UP</a> <button type='button'>Előresorolás</button> "
                                           "<a href='/szerk'>Szerkesztés</a></div>" + UP_SCRIPT % 8),
    "apro/rtx_3060_ti_elfogyott/index.html": ha_page("<div><a href='#' id='up'>UP</a></div>" + UP_SCRIPT % 0),
    "apro/rtx_3060_ti_fizetos/index.html": ha_page("<div><button type='button'>Előresorolás</button></div>"),
    "apro/rtx_3060_ti_eladva/index.html": ha_page("<span>Eladva</span>"),
}


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class HardverUpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        for name, content in HA_PAGES.items():
            path = Path(cls.dir.name) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_QuietHandler, directory=cls.dir.name))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.dir.cleanup()

    def setUp(self):
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        cfg.browser.headless = True
        cfg.web.allow_local = True
        cfg.work.platforms = ["hardverapro"]
        cfg.work.research_sites = []
        self.desk = WorkDesk(cfg, OllamaClient(cfg.llm), Toolbox(cfg))
        self.desk.platforms["hardverapro"] = Platform("hardverapro", "Hardverapró", "", "", "", ("127.0.0.1",), "")
        self.store = self.desk.store

    def tearDown(self):
        self.desk.toolbox.close()
        self.fake.close()
        self.tmp.cleanup()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/{path}"

    def bump(self, path: str) -> tuple[bool, str]:
        item = self.store.create(targets=["hardverapro"], prompt="RTX 3060 Ti", title="RTX 3060 Ti", price=90000)
        self.store.mark(item.id, "hardverapro", POSTED, url=self.url(path))
        return self.desk.bump(item.id, "hardverapro")

    def test_the_free_up_button(self):
        self.assertEqual(self.bump("apro/rtx_3060_ti/"), (True, "előre hoztam az ingyenes UP gombbal"))
        browser = self.desk.toolbox.browser
        self.assertEqual(browser.page.evaluate("() => localStorage.getItem('up')"), "1")
        self.assertFalse(browser.auto, "utána a böngésző újra a modellé")

    def test_no_free_up_left_today(self):
        ok, note = self.bump("apro/rtx_3060_ti_elfogyott/")
        self.assertFalse(ok)
        self.assertIn("ma már nincs ingyenes UP", note)

    def test_never_the_paid_bump_and_not_on_a_sold_listing(self):
        ok, note = self.bump("apro/rtx_3060_ti_fizetos/")
        self.assertFalse(ok)
        self.assertIn("nem találtam az ingyenes UP gombot", note)
        ok, note = self.bump("apro/rtx_3060_ti_eladva/")
        self.assertFalse(ok)
        self.assertIn("nem hoztam előre", note)


# --- a háttérellenőrzés átveszi a Vinteden kézzel átírt árat ---------------------------------------------------

def vinted_item(price: str) -> str:
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Zara kabát | Vinted</title>"
            '<script type="application/ld+json">{"@type": "Product", "offers": {"@type": "Offer", '
            f'"price": "{price}", "priceCurrency": "HUF"}}}}</script></head><body><main><h1>Zara kabát</h1>'
            "<div data-testid='item-price'>" + price.split(".")[0] + " Ft</div></main></body></html>")


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class CheckerPriceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        pages = {"items/1001-zara-kabat/index.html": vinted_item("4500.0"),
                 "items/1002-zara-kabat/index.html": vinted_item("4500.0").replace(
                     "<script type", "<script data-x type").replace("data-testid='item-price'>4500 Ft",
                                                                    "data-testid='item-price'>4 500 Ft"),
                 "apro/zara_kabat/index.html": vinted_item("4500.0")}
        for name, content in pages.items():
            path = Path(self.dir.name) / "site" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          functools.partial(_QuietHandler, directory=str(Path(self.dir.name) / "site")))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        cfg = make_cfg(Path(self.dir.name))
        cfg.browser.headless = True
        self.browser = BrowserController(cfg.browser, allow_local=True)
        self.platforms = {key: Platform(key, name, "", "", "", ("127.0.0.1",), "")
                          for key, name in (("vinted", "Vinted"), ("hardverapro", "Hardverapró"))}
        self.store = ItemStore(Path(self.dir.name) / "items", platform_keys=list(self.platforms))
        self.checker = ListingChecker(self.store, self.platforms, lambda: self.browser)
        self.notices: list[tuple] = []

    def tearDown(self):
        self.browser.close()
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()

    def posted(self, key: str, path: str) -> str:
        item = self.store.create(targets=[key], title="Zara kabát", price=5000)
        self.store.mark(item.id, key, POSTED, url=f"http://127.0.0.1:{self.server.server_address[1]}/{path}")
        self.store.get(item.id).platforms[key]["at"] = time.time() - 2 * DAY
        return item.id

    def test_the_price_you_set_on_vinted_is_taken_over(self):
        structured = self.posted("vinted", "items/1001-zara-kabat/")
        shown = self.posted("vinted", "items/1002-zara-kabat/")      # (szerkesztett adat nélkül: az ármezőből)
        other = self.posted("hardverapro", "apro/zara_kabat/")       # a többi oldalon nem vesszük át
        self.checker.run(5, notify=lambda *a, **k: self.notices.append((a, k)))
        self.assertEqual([self.store.get(i).price for i in (structured, shown, other)], [4500, 4500, 5000])
        self.assertEqual(self.store.get(structured).platforms["vinted"]["check"], LIVE)
        texts = sorted(a[2] for a, _k in self.notices)
        self.assertEqual(len(texts), 2)
        self.assertIn("Zara kabát: a Vinteden 5 000 Ft helyett 4 500 Ft az ára (ott írtad át)", texts[0])


class JobTests(unittest.TestCase):
    """Az ütemező háttérmunkája: csak ha valami esedékes, és akkor egyszerre egyet."""

    def test_one_due_item_per_run(self):
        from jarvis_core.app import JarvisApp
        calls: list[str] = []
        store = types.SimpleNamespace(refresh_due=lambda: [types.SimpleNamespace(id="a1"), types.SimpleNamespace(id="b2")])
        app = types.SimpleNamespace(work=types.SimpleNamespace(store=store),
                                    _refresh=lambda item_id, notify: calls.append(item_id) or "kész")
        ctx = types.SimpleNamespace(check=lambda: None, notify=lambda *a, **k: None)
        self.assertEqual(JarvisApp._refresh_job(app, ctx), "kész")
        self.assertEqual(calls, ["a1"])
        app.work.store.refresh_due = lambda: []
        self.assertEqual(JarvisApp._refresh_job(app, ctx), "Nincs esedékes frissítés.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
