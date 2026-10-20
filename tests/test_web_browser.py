"""A webes, böngészős és látás-eszközök tesztjei.

A böngészőtesztek valódi Chromiumot indítanak egy helyi tesztoldallal (internet nem kell).
Ha a Playwright vagy a Chromium nincs telepítve, ezek a tesztek kimaradnak.

Futtatás a projekt mappájából:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import base64
import functools
import importlib.machinery
import io
import json
import re
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_offline import FakeOllama, make_cfg, reply, tool_reply  # noqa: E402

from jarvis_core import assistant as assistant_module  # noqa: E402
from jarvis_core import web  # noqa: E402
from jarvis_core.assistant import STALE_NOTE, Assistant  # noqa: E402
from jarvis_core.browser import (ORDINAL_JS, SCAN_BASE, BrowserController, BrowserError,  # noqa: E402
                                 format_snapshot, is_risky, is_search_field, match_option, same_site,
                                 short_href, wanted_state)
from jarvis_core.config import load_config  # noqa: E402
from jarvis_core.llm import OllamaClient  # noqa: E402
from jarvis_core.tools import Toolbox, params  # noqa: E402

SITE = {
    "index.html": """<!doctype html><html lang="hu"><head><meta charset="utf-8"><title>Teszt Bolt</title></head>
<body>
<div id="cookie" style="position:fixed;bottom:0;left:0;right:0;background:#eee;padding:10px">
  Sütiket használunk. <button onclick="document.getElementById('cookie').remove()">Elfogadom</button></div>
<h1>Üdv a Teszt Boltban</h1>
<form action="/search.html" method="get"><input type="search" name="q" placeholder="Keresés a boltban">
  <button>Keresés</button></form>
<p id="akcio">Ma akciós a kávéfőző!</p>
<a href="/termek.html">Kávéfőző X200</a> | <a href="/termek.html" target="_blank">Megnyitás új lapon</a>
<form action="/koszonjuk.html"><label>Név <input name="nev"></label></form>
<label>Jelszó <input type="password" name="pw"></label>
<label><input type="checkbox" name="hirlevel"> Hírlevél</label>
<select name="meret"><option>Kicsi</option><option>Nagy</option></select>
<div contenteditable="true" aria-label="Megjegyzés" style="border:1px solid #ccc;min-height:20px"></div>
<button onclick="document.getElementById('akcio').textContent='Megrendelve!'">Megrendelés</button>
<button style="display:none">Rejtett gomb</button>
<div style="height:3000px"></div><p>Az oldal alja</p><a href="/termek.html">Alsó link</a>
</body></html>""",
    "search.html": """<!doctype html><html><head><meta charset="utf-8"><title>Keresési találatok</title></head>
<body><h1>Találatok</h1><ul>
<li><a href="/termek.html">Kávéfőző X200 – 39 990 Ft</a></li><li><a href="/termek.html">Kávéfőző Mini</a></li></ul>
<script>document.querySelector('h1').textContent += ': ' + new URLSearchParams(location.search).get('q');</script>
</body></html>""",
    "termek.html": """<!doctype html><html><head><meta charset="utf-8"><title>Kávéfőző X200</title></head>
<body><nav>Menü: Főoldal | Kapcsolat</nav><article><h1>Kávéfőző X200</h1>
<p>A Kávéfőző X200 egy 15 bar nyomású presszógép, beépített tejhabosítóval. Ára 39 990 Ft, a garancia 2 év.</p>
<p>Tulajdonságok: 1,5 literes víztartály, automatikus kikapcsolás, kivehető csepptálca.</p></article>
<script>var titok = "ez nem látszhat";</script><button>Kosárba</button></body></html>""",
    "koszonjuk.html": """<!doctype html><html><head><meta charset="utf-8"><title>Köszönjük</title></head>
<body><p>Elküldve.</p></body></html>""",
    "lapok.html": """<!doctype html><html><head><meta charset="utf-8"><title>Lapok</title></head>
<body><a href="/lapok.html" target="_blank">Következő lap</a></body></html>""",
    # Kockázatos jelölőnégyzet (a feltételek elfogadása) a közzététel előtt, és egy csak Esc-re bezáródó lista.
    "feltetelek.html": """<!doctype html><html lang="hu"><head><meta charset="utf-8"><title>Feltételek</title></head>
<body><label><input type="checkbox" id="elfogad"> Megerősítem, hogy elfogadom a feltételeket</label>
<button type="button" id="szin" aria-haspopup="listbox" onclick="szinek.hidden = !szinek.hidden">Színek</button>
<div id="szinek" hidden><input type="checkbox" id="piros" aria-label="Piros">
  <input type="checkbox" id="kek" aria-label="Kék"></div>
<button type="button" onclick="out.textContent = 'Feltöltve'">Feltöltés</button><p id="out"></p>
<script>document.addEventListener('keydown', (e) => { if (e.key === 'Escape') szinek.hidden = true; });</script>
</body></html>""",
    "urlap.html": None,   # lent (URLAP)
    "regi.html": None,  # cp1250 kódolású oldal, lent írjuk ki
    "kep.png": None,
}

_SIZES = ["Válassz", "XXS", "XS", "S", "M", "L", "XL", "XXL", "3XL", "4XL", "34", "36", "38", "40", "42", "44", "46",
          "48", "Egy méret", "Egyedi méret"] + [f"Gyerek {n}" for n in range(98, 153, 6)]
# Egy hirdetésfeladó űrlap a jellemző mezőfajtákkal: jelölőnégyzet, választógomb, kapcsoló, 30 elemű natív lista,
# saját lenyíló lista, kétszintű kategóriafa, kereshető márkamező, saját keresővel nyíló lista, nyitva maradó lista.
URLAP = """<!doctype html><html lang="hu"><head><meta charset="utf-8"><title>Új hirdetés</title></head><body>
<label>Cím <input id="cim"></label>
<p>Szín: <label><input type="checkbox" id="fekete"> Fekete</label>
<label><input type="checkbox" id="sotetkek"> Sötétkék</label>
Állapot: <label><input type="radio" name="allapot" id="uj"> Új</label>
<label><input type="radio" name="allapot" id="nagyonjo"> Nagyon jó</label></p>
<div role="switch" id="alku" aria-checked="false" tabindex="0" style="display:inline-block"
  onclick="this.setAttribute('aria-checked', String(this.getAttribute('aria-checked') !== 'true'))"
  >Alku lehetséges</div>
<label for="ruhameret">Ruhaméret</label>
<select id="ruhameret">""" + "".join(f"<option>{s}</option>" for s in _SIZES) + """</select>
<div role="combobox" id="meret" aria-label="Méret" aria-expanded="false" tabindex="0" style="display:inline-block"
  onclick="nyit('meretek', this)">Válassz</div>
<div role="listbox" id="meretek" hidden><div role="option" onclick="valaszt(this)">S</div>
  <div role="option" onclick="valaszt(this)">M</div><div role="option" onclick="valaszt(this)">L</div></div>
<button type="button" id="kat" aria-haspopup="true" onclick="nyit('fa', this)">Kategória</button>
<span id="kat-ertek"></span>
<div id="fa" hidden>
  <div id="fa-0"><button type="button" onclick="szint('fa-0', 'fa-nok')">Nők</button>
    <button type="button" onclick="szint('fa-0', 'fa-ferfi')">Férfiak</button></div>
  <div id="fa-nok" hidden><button type="button" onclick="szint('fa-nok', 'fa-nok-ruha')">Ruházat</button>
    <button type="button">Cipők</button></div>
  <div id="fa-ferfi" hidden><button type="button">Ruházat</button></div>
  <div id="fa-nok-ruha" hidden><div role="option" onclick="kat(this)">Pólók</div>
    <div role="option" onclick="kat(this)">Pólók és trikók</div><div role="option" onclick="kat(this)">Ingek</div></div>
</div>
<label>Márka <input id="marka" role="combobox" aria-autocomplete="list" autocomplete="off"></label>
<div role="listbox" id="markak"></div>
<button type="button" id="marka2" aria-haspopup="listbox" onclick="nyit('m2', this)">Márka listából</button>
<span id="marka2-ertek"></span>
<div id="m2" hidden><input type="search" placeholder="Márka keresése" oninput="szur(this.value)">
  <div id="m2-lista"></div></div>
<button type="button" id="szin" aria-haspopup="listbox" onclick="nyit('szinek', this)">Színek</button>
<div id="szinek" hidden><input type="checkbox" id="piros" aria-label="Piros">
  <input type="checkbox" id="kek" aria-label="Kék">
  <button type="button" onclick="szinek.hidden = true">Kész</button></div>
<p><button type="button" id="mentes" onclick="out.textContent = 'Mentve: ' + cim.value">Mentés</button>
<button type="button" onclick="out.textContent = 'Közzétéve'">Közzététel</button>
<button type="button" onclick="out.textContent = 'Kiemelve'">Kiemelés 990 Ft</button>
<button type="button" onclick="reszletek.showModal()">Részletek</button>
<button type="button" onclick="mentes.replaceWith(mentes.cloneNode(true))">Frissítés</button>
<a href="/termek.html">Másik oldal</a></p><p id="out"></p>
<dialog id="reszletek"><p>Részletek</p><button type="button" onclick="reszletek.close()">Bezárás</button></dialog>
<script>
function nyit(id, gomb) { const l = document.getElementById(id); l.hidden = !l.hidden;
  gomb.setAttribute('aria-expanded', String(!l.hidden)); }
function valaszt(o) { meret.textContent = o.textContent; meretek.hidden = true;
  meret.setAttribute('aria-expanded', 'false'); }
function szint(innen, ide) { document.getElementById(innen).hidden = true;
  document.getElementById(ide).hidden = false; }
function kat(o) { document.getElementById('kat-ertek').textContent = 'Nők > Ruházat > ' + o.textContent;
  fa.hidden = true; for (const id of ['fa-nok', 'fa-ferfi', 'fa-nok-ruha']) document.getElementById(id).hidden = true;
  document.getElementById('fa-0').hidden = false; }
const MARKAK = ['Zara', 'Zara Kids', 'H&M', 'Mango', 'Nike', 'Reserved'];
let kesleltet;
marka.addEventListener('input', () => { clearTimeout(kesleltet); kesleltet = setTimeout(() => {
  markak.innerHTML = '';
  const q = marka.value.toLowerCase();
  if (q) for (const m of MARKAK.filter((m) => m.toLowerCase().includes(q))) {
    const o = document.createElement('div'); o.setAttribute('role', 'option'); o.textContent = m;
    o.onclick = () => { marka.value = m; markak.innerHTML = ''; }; markak.appendChild(o); }
}, 300); });
function szur(q) { const lista = document.getElementById('m2-lista'); lista.innerHTML = '';
  const talalt = q ? MARKAK.filter((m) => m.toLowerCase().includes(q.toLowerCase())) : MARKAK.slice(0, 3);
  for (const m of talalt) {
    const o = document.createElement('div'); o.setAttribute('role', 'option'); o.textContent = m;
    o.onclick = () => { document.getElementById('marka2-ertek').textContent = m; m2.hidden = true; };
    lista.appendChild(o); } }
szur('');
</script></body></html>"""


class LocalSite:
    """Helyi tesztoldal a 127.0.0.1-en."""

    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        for name, content in SITE.items():
            if content is not None:
                (root / name).write_text(content, encoding="utf-8")
        (root / "regi.html").write_bytes(
            '<html><head><meta charset="windows-1250"><title>Régi oldal</title></head>'
            '<body><p>Árvíztűrő tükörfúrógép, ez egy régi kódolású magyar oldal.</p></body></html>'
            .encode("cp1250"))
        (root / "kep.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (root / "urlap.html").write_text(URLAP, encoding="utf-8")
        handler = functools.partial(_QuietHandler, directory=self.dir.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/{path}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001 - nincs letöltve a böngésző
        return False


HAS_CHROMIUM = chromium_available()


# --- web ----------------------------------------------------------------------------

class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = LocalSite()

    @classmethod
    def tearDownClass(cls):
        cls.site.close()

    def setUp(self):
        self.cfg = load_config(ROOT / "config.toml").web

    def test_normalize_url(self):
        self.assertEqual(web.normalize_url("idokep.hu"), "https://idokep.hu")
        for bad in ("file:///etc/passwd", "ftp://x.hu", "", "javascript:alert(1)//x"):
            with self.assertRaises(web.WebError, msg=bad):
                web.normalize_url(bad)
        for local in ("http://localhost:4200", "http://127.0.0.1", "192.168.1.1", "http://10.0.0.5/admin"):
            with self.assertRaises(web.WebError, msg=local):
                web.normalize_url(local)
        self.assertEqual(web.normalize_url("localhost:4200", allow_local=True), "https://localhost:4200")

    def test_search_with_fake_ddgs(self):
        class FakeDDGS:
            def __init__(self, timeout=5):
                pass

            def text(self, query, region, safesearch, max_results):
                assert region == "hu-hu"
                return [{"title": "Időkép", "href": "https://www.idokep.hu", "body": "Holnap 18 fok, napos idő."}]

        fake = types.ModuleType("ddgs")
        fake.DDGS = FakeDDGS
        with mock.patch.dict(sys.modules, {"ddgs": fake}):
            results = web.search("holnapi időjárás", self.cfg)
        text = web.format_results("holnapi időjárás", results)
        self.assertIn("1. Időkép", text)
        self.assertIn("https://www.idokep.hu", text)
        self.assertTrue(text.startswith(web.UNTRUSTED_NOTE))

    def test_search_failure_suggests_browser(self):
        class BrokenDDGS:
            def __init__(self, timeout=5):
                pass

            def text(self, *args, **kwargs):
                raise RuntimeError("ratelimit")

        fake = types.ModuleType("ddgs")
        fake.DDGS = BrokenDDGS
        with mock.patch.dict(sys.modules, {"ddgs": fake}), self.assertRaisesRegex(web.WebError, "bing.com"):
            web.search("valami", self.cfg)

    def test_extract_text_fallback(self):
        with mock.patch.dict(sys.modules, {"trafilatura": None}):  # mintha nem lenne telepítve
            title, text = web.extract_text(SITE["termek.html"].encode("utf-8"))
        self.assertEqual(title, "Kávéfőző X200")
        self.assertIn("15 bar nyomású", text)
        self.assertNotIn("ez nem látszhat", text)   # a script nem kerül bele
        self.assertNotIn("Menü", text)              # a navigáció sem

    def test_extract_text_uses_trafilatura(self):
        fake = types.ModuleType("trafilatura")
        fake.extract = lambda page, **kw: "Fő tartalom " * 30
        fake.html2txt = lambda page: ""
        with mock.patch.dict(sys.modules, {"trafilatura": fake}):
            _, text = web.extract_text("<html><title>T</title><p>x</p></html>")
        self.assertTrue(text.startswith("Fő tartalom"))

    def test_read_webpage_and_encodings(self):
        self.cfg.allow_local = True
        with mock.patch.dict(sys.modules, {"trafilatura": None}):
            page = web.read_webpage(self.site.url("termek.html"), self.cfg)
            old = web.read_webpage(self.site.url("regi.html"), self.cfg)
        self.assertIn("Oldal: Kávéfőző X200", page)
        self.assertIn("39 990 Ft", page)
        self.assertIn("Árvíztűrő tükörfúrógép", old)
        with self.assertRaisesRegex(web.WebError, "nem weboldal"):
            web.read_webpage(self.site.url("kep.png"), self.cfg)
        with self.assertRaisesRegex(web.WebError, "hibát adott"):
            web.read_webpage(self.site.url("nincs.html"), self.cfg)

    def test_read_webpage_blocks_local_by_default(self):
        self.cfg.allow_local = False
        with self.assertRaisesRegex(web.WebError, "allow_local"):
            web.read_webpage(self.site.url("termek.html"), self.cfg)

    def test_truncation(self):
        page = web.format_page("Cím", "https://x.hu", "a" * 500, 100)
        self.assertIn("Az első 100 karakter az 500-ból", page.replace("a 500", "az 500"))


class RiskTests(unittest.TestCase):
    def test_risky_words(self):
        for label in ("Megrendelés", "Fizetés", "Vásárlás most", "Törlés", "Küldés", "Elküldöm",
                      "Pay now", "Place order", "Közzététel", "Feliratkozás", "Regisztráció"):
            self.assertTrue(is_risky(label), label)
        for label in ("Elfogadom", "Kosárba", "Tovább", "Hírlevél", "Keresés", "Bezárás", "Kávéfőző X200"):
            self.assertFalse(is_risky(label), label)

    def test_search_field_detection(self):
        self.assertTrue(is_search_field({"kind": "input:search"}))
        self.assertTrue(is_search_field({"kind": "input:text", "label": "Keresés a boltban"}))
        self.assertTrue(is_search_field({"kind": "input:text", "label": "", "field": "q"}))
        self.assertFalse(is_search_field({"kind": "input:text", "label": "Név", "field": "nev"}))


# --- valódi böngésző -------------------------------------------------------------------

@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = LocalSite()
        cls.profile = tempfile.TemporaryDirectory()
        cfg = load_config(ROOT / "config.toml").browser
        cfg.headless = True
        cfg.profile_dir = cls.profile.name
        cfg.pace = 0.0   # emberi mozdulatok várakozás nélkül (a tempót a test_humanize nézi)
        cls.asked: list[str] = []
        cls.answer = False
        cls.browser = BrowserController(cfg, allow_local=True,
                                        confirm=lambda prompt: cls.asked.append(prompt) or cls.answer)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.site.close()
        cls.profile.cleanup()

    def setUp(self):
        type(self).answer = False
        self.asked.clear()
        self.view = self.browser.open(self.site.url("index.html"))

    def find(self, predicate) -> int:
        return next(i for i, e in self.browser.elements.items() if predicate(e))

    def test_snapshot_lists_elements(self):
        self.assertIn("Oldal: Teszt Bolt", self.view)
        self.assertIn('[1] gomb "Elfogadom"', self.view)
        self.assertIn('keresőmező "Keresés a boltban"', self.view)
        self.assertIn("(érzékeny adat, ezt nem töltöm ki)", self.view)
        self.assertIn('(kiválasztva: "Kicsi"; lehetőségek: Kicsi, Nagy)', self.view)
        self.assertIn("lejjebb is van még tartalom", self.view)
        self.assertNotIn("Rejtett gomb", self.view)
        self.assertIn("Ma akciós a kávéfőző!", self.view)

    def test_search_click_read_back(self):
        view = self.browser.type_text(self.find(lambda e: e["kind"] == "input:search"), "kávéfőző", submit=True)
        self.assertEqual(self.asked, [], "keresésnél nem kell jóváhagyás")
        self.assertIn("Találatok: kávéfőző", view)
        view = self.browser.click(self.find(lambda e: "X200" in e["label"]))
        self.assertIn("Oldal: Kávéfőző X200", view)
        article = self.browser.read()
        self.assertIn("15 bar nyomású", article)
        self.assertNotIn("Friss", article)
        view = self.browser.back()
        self.assertIn("Visszaléptem", view)
        self.assertIn("Keresési találatok", view)

    def test_password_is_never_typed(self):
        with self.assertRaisesRegex(BrowserError, "Jelszót"):
            self.browser.type_text(self.find(lambda e: e["kind"] == "input:password"), "titok")
        self.assertEqual(self.browser.page.input_value("input[name=pw]"), "")

    def test_risky_click_needs_confirmation(self):
        order = self.find(lambda e: e["label"] == "Megrendelés")
        self.assertIn("nem engedélyezte", self.browser.click(order))
        self.assertIn("Megrendelés", self.asked[0])
        self.assertEqual(self.browser.page.inner_text("#akcio"), "Ma akciós a kávéfőző!")
        type(self).answer = True
        self.browser.snapshot()
        self.browser.click(self.find(lambda e: e["label"] == "Megrendelés"))
        self.assertEqual(self.browser.page.inner_text("#akcio"), "Megrendelve!")

    def test_form_submit_needs_confirmation(self):
        name_field = self.find(lambda e: e["label"] == "Név")
        self.assertIn("nem engedélyezte", self.browser.type_text(name_field, "Teszt Elek", submit=True))
        self.assertIn("Teszt Elek", self.asked[0])
        type(self).answer = True
        self.browser.snapshot()
        view = self.browser.type_text(self.find(lambda e: e["label"] == "Név"), "Teszt Elek", submit=True)
        self.assertIn("Oldal: Köszönjük", view)

    def test_select_checkbox_contenteditable(self):
        view = self.browser.type_text(self.find(lambda e: e.get("select")), "Nagy")
        self.assertIn('kiválasztva: "Nagy"', view)
        view = self.browser.click(self.find(lambda e: e["label"] == "Hírlevél"))
        self.assertIn('"Hírlevél" [bejelölve]', view)
        self.browser.type_text(self.find(lambda e: e["label"] == "Megjegyzés"), "Kérem csomagolva")
        self.assertEqual(self.browser.page.inner_text("[aria-label=Megjegyzés]"), "Kérem csomagolva")

    def test_new_tab_is_followed(self):
        view = self.browser.click(self.find(lambda e: "új lapon" in e["label"]))
        self.assertIn("Új lapon nyílt meg.", view)
        self.assertIn("Oldal: Kávéfőző X200", view)

    def test_own_tabs_are_closed_but_not_the_users(self):
        context = self.browser._context
        first = self.browser.page = context.new_page()   # a felhasználó lapjai (kézzel nyitotta)
        by_hand = context.new_page()
        self.browser.open(self.site.url("lapok.html"))
        before = len([p for p in context.pages if not p.is_closed()])
        opened = []
        for _ in range(5):
            self.browser.click(self.find(lambda e: e["label"] == "Következő lap"))
            opened.append(self.browser.page)
        try:
            pages = [p for p in context.pages if not p.is_closed()]
            self.assertIs(self.browser.page, opened[-1], "a legújabb lapon dolgozik")
            self.assertLessEqual(len(pages), before + 2, "csak az aktív és az előző saját lap marad")
            self.assertTrue(all(p in pages for p in (first, by_hand, opened[-2])))
            self.assertTrue(all(p.is_closed() for p in opened[:-2]), "a régebbi saját lapokat bezárta")
            self.assertIsNone(getattr(first, "_jarvis_network", None), "a háttérbe került lapot nem figyeli")
            self.assertIsNotNone(getattr(opened[-1], "_jarvis_network", None))
        finally:
            by_hand.close()

    def test_model_free_steps_keep_the_scan_numbers(self):
        self.browser.auto = True       # modell nélküli lépéssor: nincs oldalnézet, amit senki nem olvasna
        try:
            elements = self.browser.scan()
            name = next(e for e in elements if e["label"] == "Név")
            news = next(e for e in elements if e["label"] == "Hírlevél")
            self.assertEqual(self.browser.type_text(name["id"], "Teszt Elek"), 'Beírtam: "Teszt Elek".')
            note = self.browser.click(news["id"])       # a letapogatás száma még érvényes
            self.assertEqual(note, 'Rákattintottam: "Hírlevél".')
            self.assertTrue(self.browser.page.is_checked("input[name=hirlevel]"))
            self.assertEqual(self.browser.page.input_value("input[name=nev]"), "Teszt Elek")
        finally:
            self.browser.auto = False

    def test_ordinals_are_counted_only_while_recording(self):
        page, scripts, events = self.browser.page, [], []
        original = page.evaluate
        page.evaluate = lambda script, *args, **kwargs: scripts.append(script) or original(script, *args, **kwargs)
        self.browser.on_action = events.append
        try:
            self.browser.click(self.find(lambda e: e["label"] == "Hírlevél"))
            self.assertNotIn(ORDINAL_JS, scripts, "felvétel nélkül nem járja be az egész lapot")
            self.browser.recording = True
            self.browser.click(self.find(lambda e: e["label"] == "Hírlevél"))
            self.assertIn(ORDINAL_JS, scripts)
            self.assertEqual([e["do"] for e in events], ["click", "click"])
        finally:
            del page.evaluate
            self.browser.on_action = None
            self.browser.recording = False

    def test_scroll_to_bottom(self):
        for _ in range(6):
            view = self.browser.scroll("down")
        self.assertIn("az oldal alján vagyunk", view)
        self.assertIn("Az oldal alja", view)
        self.assertIn("Felfelé görgettem.", self.browser.scroll("up"))

    def test_bad_element_numbers(self):
        with self.assertRaisesRegex(BrowserError, "Nincs 999"):
            self.browser.click(999)
        with self.assertRaisesRegex(BrowserError, "számával"):
            self.browser.click("gomb")

    def test_screenshot(self):
        data = base64.b64decode(self.browser.screenshot_b64())
        self.assertTrue(data.startswith(b"\xff\xd8"))  # JPEG

    def test_recovers_when_window_closed(self):
        self.browser._context.close()  # mintha a felhasználó bezárta volna az ablakot
        view = self.browser.open(self.site.url("termek.html"))
        self.assertIn("Oldal: Kávéfőző X200", view)

    def test_local_addresses_blocked_by_default(self):
        strict = BrowserController(self.browser.cfg, allow_local=False)
        with self.assertRaisesRegex(BrowserError, "allow_local"):
            strict.open(self.site.url("index.html"))
        self.assertFalse(strict.running, "tiltott címnél el sem indul a böngésző")


# --- az oldalnézet: tömör, állandó számokkal ----------------------------------------------------------

_BRANDS = ["Zara", "H&M", "Nike", "Adidas", "Mango", "Reserved", "Bershka", "Levi's", "Tommy Hilfiger", "Puma"]
_PRICES = [1500, 2500, 3990, 4500, 6000, 8990, 12000]
_COLORS = ["Fekete", "Barna", "Szürke", "Bézs", "Rózsaszín", "Lila", "Piros", "Sárga", "Kék", "Zöld", "Narancs",
           "Fehér"]


def _ft(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " Ft"


def catalog_page() -> str:
    """Vinted-szerű találati oldal 48 hirdetéssel: követőparaméteres linkek, sok menülink, szűrőgombok."""
    words = "noi ferfi gyerek otthon elektronika szorakozas sport kisallat markak akciok sugo blog rolunk karrier sajto"
    nav = "".join(f'<li><a href="/catalog/{i}-{w}?ref=nav&utm_source=header">{w.capitalize()}</a></li>'
                  for i, w in enumerate(words.split()))
    sub = "".join(f'<a href="/catalog/{1000 + i}-kat-{i}?ref=mega">Alkategória {i}</a>' for i in range(30))
    footer = "".join(f'<a href="/help/{i}-segitseg-cikk-{i}?locale=hu&ref=footer">Súgó cikk {i}</a>'
                     for i in range(35))
    filters = "".join(f'<button aria-haspopup="listbox" aria-expanded="false">{f}</button>'
                      for f in ("Kategória", "Méret", "Márka", "Állapot", "Szín", "Ár", "Anyag", "Rendezés"))
    cards = []
    for i in range(48):
        brand, price = _BRANDS[i % len(_BRANDS)], _PRICES[i % len(_PRICES)]
        slug = f"{7000000000 + i * 137}-{brand.lower().replace(' ', '-')}-noi-polo-meret-m-{i}"
        cards.append(
            f'<div class="card"><a href="/items/{slug}?referrer=catalog&homepage_session_id=abc123def456">'
            f'<img alt="{brand} női póló" src="data:,"></a>'
            f'<div><a href="/member/{100000 + i}-felhasznalo{i}?ref=item_box">felhasznalo{i}</a> '
            f'<button aria-label="Kedvencekhez adás">♡</button><span>{i % 40}</span></div>'
            f'<p>{brand}</p><p>M · Nagyon jó</p><p>{_ft(price)}</p>'
            f'<p>{_ft(int(price * 1.05 + 150))} vevővédelemmel</p></div>')
    return ("<!doctype html><html lang='hu'><head><meta charset='utf-8'><title>Női pólók | Vinted</title><style>"
            "body{font-family:sans-serif;margin:0} header{height:120px;overflow:hidden}"
            ".grid{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}"
            ".card{border:1px solid #ccc;padding:4px;height:330px}"
            ".card img{width:100%;height:220px;background:#eee;display:block}</style></head><body>"
            '<header><a href="/"><img alt="Vinted logó" src="data:,">Vinted</a>'
            '<form action="/catalog"><input type="search" name="search_text" placeholder="Keress márkákra"></form>'
            f'<nav><ul>{nav}</ul><div class="mega">{sub}</div></nav>'
            '<a href="/member/signup/select_type?ref_url=%2Fitems%2Fnew">Regisztrálj | Jelentkezz be</a>'
            '<a href="/items/new">Adj el most</a></header>'
            f"<main><h1>Női pólók</h1><div>{filters}</div><p>12 345 találat</p>"
            f"<div class='grid'>{''.join(cards)}</div>"
            "<nav aria-label='lapozás'><a href='?page=2'>2</a><a href='?page=2'>Következő</a></nav></main>"
            f"<footer>{footer}<p>© Vinted 2026. Minden jog fenntartva.</p></footer></body></html>")


def form_page() -> str:
    """Hosszú hirdetésfeladó űrlap: választógombok magyarázattal, jelölőnégyzetek egy cím alatt, 150 elemű lenyíló
    lista, kattintásra kinyíló márkalista, és jó három képernyővel lejjebb az ár és a Tovább gomb."""
    options = "".join(f"<option>{'Videokártya' if i == 120 else f'Kategória {i}'}</option>" for i in range(150))
    colors = "".join(f'<label><input type="checkbox" name="color" value="{c}"> {c}</label>' for c in _COLORS)
    return f"""<!doctype html><html lang="hu"><head><meta charset="utf-8"><title>Új hirdetés</title></head><body>
<header><a href="/">Főoldal</a> <nav><a href="/sugo?ref=nav">Súgó</a></nav></header>
<main><h1>Új hirdetés</h1>
<form novalidate onsubmit="event.preventDefault(); hiba()">
<p><label>Cím <input name="title" required></label></p>
<p><label>Leírás <textarea name="description"></textarea></label></p>
<p><label>Kategória <select name="category">{options}</select></label></p>
<p><label id="markal">Márka</label> <span role="combobox" aria-labelledby="markal" aria-expanded="false"
  aria-controls="markak" tabindex="0" onclick="nyit()">Válassz márkát</span></p><ul role="listbox" id="markak"></ul>
<fieldset><legend>Állapot</legend>
<label><input type="radio" name="allapot" value="uj" required> Új <small>Címkével, sosem használt darab.</small>
</label><label><input type="radio" name="allapot" value="jo" required> Jó, de használt <small>Látható kopás
nélkül.</small></label><label><input type="radio" name="allapot" value="ok" required> Elfogadható</label></fieldset>
<h3>Szín</h3><div>{colors}</div>
<div style="height:2400px"></div>
<p><label>Ár <input name="price" aria-required="true"></label></p>
<button type="submit">Tovább</button>
</form></main>
<script>
function nyit() {{
  document.querySelector('[role=combobox]').setAttribute('aria-expanded', 'true');
  document.getElementById('markak').innerHTML = ['Nike', 'Adidas', 'Zara'].map(
    (m) => '<li role="option" tabindex="-1">' + m + '</li>').join('');
}}
function hiba() {{
  document.querySelector('[name=price]').setAttribute('aria-invalid', 'true');
  const d = document.createElement('div');
  d.setAttribute('role', 'alert');
  d.textContent = 'Add meg az árat.';
  document.querySelector('h1').after(d);
}}
</script></body></html>"""


VIEW_PAGES = {
    "/katalogus.html": catalog_page(),
    "/urlap.html": form_page(),
    "/sok-gomb.html": ("<!doctype html><html><head><meta charset='utf-8'><title>Sok gomb</title></head><body><main>"
                       + "".join(f"<button>Gomb {i}</button>" for i in range(80)) + "</main></body></html>"),
    # React-szerű űrlap <form> nélkül: a küldőgomb is a „Lejjebb az űrlapon” részbe kerül, egy kedvencgomb nem.
    "/react-urlap.html": """<!doctype html><html><head><meta charset="utf-8"><title>Feladás</title></head><body><main>
<div><label>Cím <input name="title"></label></div>
<fieldset><legend>Szállítás</legend>
<label><input type="radio" name="mod" value="posta"> Posta</label><label><input type="radio" name="mod"> Foxpost</label>
<label><input type="radio" name="fizetes"> Előre utalás</label>
<label><input type="radio" name="fizetes"> Utánvét</label>
</fieldset>
<div style="height:2600px"></div>
<div><button>Kedvencekhez</button></div>
<div><button>Feltöltés</button></div>
</main></body></html>""",
    # Süti-ablak nyitott shadow DOM-ban (sok süti-kezelő így épül fel).
    "/arnyek.html": """<!doctype html><html><head><meta charset="utf-8"><title>Árnyék</title></head><body>
<p id="allapot">Nincs elfogadva.</p><div id="host"></div>
<script>
const root = document.getElementById('host').attachShadow({mode: 'open'});
root.innerHTML = '<p>Sütiket használunk (árnyékban).</p><button>Elfogadom</button>' +
  '<span id="em">E-mail cím</span><input aria-labelledby="em">';
root.querySelector('button').onclick = () => { document.getElementById('allapot').textContent = 'Elfogadva.'; };
</script></body></html>""",
}


class _PagesHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = VIEW_PAGES.get(self.path.split("?")[0], "").encode("utf-8")
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PageViewFormatTests(unittest.TestCase):
    """Az oldalnézet szövege rögzített adatokból (böngésző nélkül)."""

    def test_links_show_only_the_useful_part_of_their_address(self):
        page = "https://www.vinted.hu/catalog?search_text=polo"

        def short(href, url="", **extra):
            return short_href({"id": 1, "kind": "link", "href": href, "url": url, **extra}, page)

        item = "https://www.vinted.hu/items/1-polo?referrer=catalog&homepage_session_id=abc123"
        self.assertEqual(short("/items/1-polo?referrer=catalog&homepage_session_id=abc123", item), "/items/1-polo")
        search = "https://www.vinted.hu/catalog?search_text=nike polo&catalog[]=1904&time=1712&page=2"
        self.assertEqual(short("?x", search), "/catalog?search_text=nike+polo&catalog[]=1904&page=2")
        shop = "https://shop.example.com/p/123?utm_source=x"
        self.assertEqual(short(shop, shop), "shop.example.com/p/123", "más oldalra: a gépnévvel")
        self.assertEqual(short("/items/2-polo?ref=x"), "/items/2-polo", "a cím nélkül is feloldja")
        self.assertEqual(short("/k%C3%A1v%C3%A9", "https://www.vinted.hu/k%C3%A1v%C3%A9"), "/kávé")
        long = short("/a", "https://www.vinted.hu/items/" + "nagyon-hosszu-cim-" * 6)
        self.assertEqual(len(long), 60)
        self.assertTrue(long.endswith("…"))
        for href in ("#top", "javascript:void(0)", ""):
            self.assertEqual(short(href), "", href)
        self.assertEqual(short("/catalog/1-noi?ref=nav", chrome=True), "", "menülinknél nincs cím")

    def test_view_layout(self):
        data = {"title": "Új hirdetés", "url": "https://x.hu/uj", "text": "Új hirdetés", "errors": ["Add meg az árat."],
                "elements": [
                    {"id": 3, "kind": "input:text", "label": "Cím", "required": True, "inView": True},
                    {"id": 4, "kind": "combobox", "label": "Márka", "expanded": False, "inView": True},
                    {"id": 5, "kind": "input:radio", "label": "Új Címkével", "short": "Új", "group": "g0radio",
                     "question": "Állapot", "checked": False, "inView": True},
                    {"id": 6, "kind": "input:radio", "label": "Jó, de használt", "group": "g0radio",
                     "question": "Állapot", "checked": True, "inView": True, "new": True},
                    {"id": 7, "kind": "input:checkbox", "label": "Hírlevél", "group": "n1checkbox",
                     "question": "Értesítések", "checked": False, "inView": True},
                    {"id": 8, "kind": "link", "label": "Nike póló", "price": "6 000 Ft", "href": "/items/8?ref=x",
                     "url": "https://x.hu/items/8?ref=x", "inView": True},
                    {"id": 2, "kind": "select", "label": "Kategória", "select": True, "value": "Válassz",
                     "options": ["Videokártya", "Alaplap"], "more": 148, "inView": True},
                    {"id": 10, "kind": "button", "label": "Mentés", "disabled": True, "inView": False},
                ],
                "dropped": 5, "droppedChrome": 3,
                "below": [{"id": 12, "kind": "input:text", "label": "Ár", "invalid": True, "inView": False}]}
        lines = format_snapshot(data, 'Rákattintottam: "Tovább".').splitlines()
        # a közös forma: figyelmeztetés, a művelet eredménye, Oldal, Cím, utánuk a többi
        self.assertEqual(lines[:5], [web.UNTRUSTED_NOTE, 'Rákattintottam: "Tovább".', "Oldal: Új hirdetés",
                                     "Cím: https://x.hu/uj", "Hibaüzenetek: Add meg az árat."])
        self.assertIn("Kezelhető elemek (a számukkal hivatkozz rájuk; *: új az előző nézet óta):", lines)
        self.assertIn('[3] szövegmező "Cím" (kötelező)', lines)
        self.assertIn('[4] lenyíló lista "Márka" (csukva)', lines)
        self.assertIn('Állapot (választógombok): [5] Új, *[6] "Jó, de használt" ✓', lines)
        self.assertIn('[7] jelölőnégyzet "Hírlevél" (csoport: Értesítések) [nincs bejelölve]', lines)
        self.assertIn('[8] link "Nike póló · 6 000 Ft" -> /items/8', lines)
        self.assertIn('[2] lenyíló lista "Kategória" (kiválasztva: "Válassz"; lehetőségek: Videokártya, Alaplap '
                      '(+148 további; írd be a nevét))', lines)
        outside = lines.index("A látható részen kívül (görgetés nélkül is elérhetők):")
        self.assertEqual(lines[outside + 1], '[10] gomb "Mentés" (tiltva)')
        self.assertIn("(+5 elem nem fért ki, ebből 3 menüpont)", lines)
        below = lines.index("Lejjebb az űrlapon (görgetés nélkül is elérhetők):")
        self.assertEqual(lines[below + 1], '[12] szövegmező "Ár" (hibás)')
        self.assertNotIn("(a látható részen kívül)", "\n".join(lines))
        menu_only = format_snapshot({"url": "https://x.hu", "elements": [], "dropped": 4, "droppedChrome": 4})
        self.assertIn("(+4 menüpont nem fért ki)", menu_only)
        self.assertNotIn("Hibaüzenetek", menu_only)


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class PageViewTests(unittest.TestCase):
    """Az oldalnézet valódi böngészőben: tömör, a számok állandók, és semmi nem vész el belőle szó nélkül."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _PagesHandler)
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
        cls.profile.cleanup()

    def setUp(self):
        self.browser.hints = []

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}{path}"

    def number(self, label: str) -> int:
        return next(n for n, e in self.browser.elements.items() if e.get("label") == label and n < SCAN_BASE)

    def number_of(self, field: str) -> int:
        return next(n for n, e in self.browser.elements.items() if e.get("field") == field and n < SCAN_BASE)

    @staticmethod
    def line_of(view: str, number: int) -> str:
        return next(line for line in view.splitlines() if re.search(rf"(^|\s|\*)\[{number}\]", line))

    def test_catalog_view_is_compact_and_keeps_every_number(self):
        view = self.browser.open(self.url("/katalogus.html"))
        numbers = [n for n in self.browser.elements if n < SCAN_BASE]
        self.assertEqual(len(numbers), 60)
        for n in numbers:
            self.assertRegex(view, rf"\[{n}\]")
        # Régen 5 731 karakter volt (ennek a fele a linkek követőparaméterei); most legalább 30%-kal kevesebb.
        self.assertLess(len(view), 4000, view)
        for noise in ("referrer", "homepage_session_id", "utm_source", "ref=", "(a látható részen kívül)"):
            self.assertNotIn(noise, view)
        first = self.number("Zara női póló")
        self.assertIn("referrer=catalog", self.browser.elements[first]["href"], "a teljes cím megmarad")
        self.assertEqual(self.line_of(view, first),
                         f'[{first}] link "Zara női póló · 1 500 Ft" -> /items/7000000000-zara-noi-polo-meret-m-0')
        self.assertNotIn("Ft", self.line_of(view, self.number("felhasznalo0")), "az eladó linkje nem kap árat")
        self.assertEqual(self.line_of(view, self.number("Noi")), f'[{self.number("Noi")}] link "Noi"')
        self.assertIn('gomb "Kategória" (csukva)', view)
        self.assertIn("A látható részen kívül (görgetés nélkül is elérhetők):", view)
        self.assertIn("(+28 menüpont nem fért ki)", view)
        self.assertNotIn("Alkategória 25", view, "a ki nem fért menülinkek szövege sem foglal helyet")

    def test_long_form_groups_choices_and_lists_the_rest_below(self):
        view = self.browser.open(self.url("/urlap.html"))
        self.assertLess(len(view), 1175, "régen 1 678 karakter volt (két elemmel kevesebbel)")
        self.assertIn(f'[{self.number("Cím")}] szövegmező "Cím" (kötelező)', view)
        self.assertRegex(view, r'Állapot \(választógombok\) \(kötelező\): \[\d+\] Új, \[\d+\] "Jó, de használt", '
                               r'\[\d+\] Elfogadható\n')
        self.assertNotIn("sosem használt", view, "a választók magyarázata nem kerül a nézetbe")
        colors = next(line for line in view.splitlines() if line.startswith("Szín (jelölőnégyzetek): "))
        for color in _COLORS:
            self.assertIn(f"[{self.number(color)}] {color}", colors)
        self.assertIn(f'[{self.number("Márka")}] lenyíló lista "Márka" (csukva)', view)
        self.assertIn("(+138 további; írd be a nevét)", view)
        lines = view.splitlines()
        below = lines.index("Lejjebb az űrlapon (görgetés nélkül is elérhetők):")
        self.assertEqual(lines[below + 1:below + 3], [f'[{self.number("Ár")}] szövegmező "Ár" (kötelező)',
                                                      f'[{self.number("Tovább")}] gomb "Tovább"'])
        # egy csoport tagja a saját számával kattintható
        view = self.browser.click(self.number("Barna"))
        self.assertRegex(view, rf"\[{self.number('Barna')}\] Barna ✓")

    def test_far_submit_button_is_clickable_and_errors_lead_the_view(self):
        self.browser.open(self.url("/urlap.html"))
        price, cim = self.number("Ár"), self.number("Cím")
        view = self.browser.click(self.number("Tovább"))   # három képernyővel lejjebb: görgetés nélkül
        lines = view.splitlines()
        self.assertTrue(lines[3].startswith("Cím: "))
        self.assertEqual(lines[4], "Hibaüzenetek: Add meg az árat.")
        self.assertIn(f'[{price}] szövegmező "Ár" (kötelező) (hibás)', view, "ugyanaz a szám maradt")
        # a lap tetején maradt mező nincs a nézetben, de a száma (a DOM-ban) ugyanaz maradt
        self.assertEqual(self.browser.page.get_attribute("[name=title]", "data-jarvis-id"), str(cim))

    def test_long_select_shows_the_options_matching_the_hints(self):
        self.browser.open(self.url("/urlap.html"))
        self.browser.hints = ["Videokártya", "Nike"]
        view = self.browser.snapshot()
        # a körülölelő címkéből csak a saját szövege, a 150 lehetőség nélkül
        self.assertIn(f'[{self.number_of("category")}] lenyíló lista "Kategória" (kiválasztva: "Kategória 0"; '
                      "lehetőségek: Videokártya, Kategória 0, Kategória 1", view)
        self.assertIn("(+138 további; írd be a nevét)", view)
        self.browser.hints = []
        self.assertIn("lehetőségek: Kategória 0, Kategória 1", self.browser.snapshot(), "tipp nélkül a lista eleje")

    def test_numbers_stay_and_new_elements_are_marked(self):
        self.browser.open(self.url("/urlap.html"))
        cim, marka = self.number("Cím"), self.number("Márka")
        view = self.browser.click(marka)
        self.assertEqual((self.number("Cím"), self.number("Márka")), (cim, marka), "ugyanazok a számok")
        self.assertIn("Kezelhető elemek (a számukkal hivatkozz rájuk; *: új az előző nézet óta):", view)
        self.assertIn(f'[{marka}] lenyíló lista "Márka" (nyitva)', view)
        nike, adidas, zara = self.number("Nike"), self.number("Adidas"), self.number("Zara")
        self.assertIn(f"Márka (opciók): *[{nike}] Nike, *[{adidas}] Adidas, *[{zara}] Zara", view)
        self.assertNotIn(f"*[{cim}]", view)
        self.assertNotIn("*[", self.browser.snapshot(), "a következő nézetben már nem újak")
        self.assertNotIn("*[", self.browser.open(self.url("/urlap.html")), "új oldalon nincs csillag")

    def test_scan_and_locate_numbers_survive_a_page_view(self):
        self.browser.open(self.url("/urlap.html"))
        title = next(e for e in self.browser.scan() if e["field"].startswith("title"))
        self.assertGreaterEqual(title["id"], SCAN_BASE)
        view = self.browser.snapshot()
        self.assertNotIn(f"[{title['id']}]", view, "a letapogatás számai nem kerülnek a modell elé")
        self.browser.type_text(title["id"], "Zara póló")
        self.assertEqual(self.browser.page.input_value("[name=title]"), "Zara póló")
        found = self.browser.locate({"kind": "input:text", "label": "Ár", "field": "price"})
        self.browser.snapshot()
        self.browser.type_text(found["id"], "5000")
        self.assertEqual(self.browser.page.input_value("[name=price]"), "5000")
        self.assertEqual(self.browser.page.input_value("[name=title]"), "Zara póló")

    def test_numbers_restart_before_reaching_the_scan_range(self):
        self.browser.open(self.url("/urlap.html"))
        self.browser.page.evaluate("() => { window.__jarvisView.seq = 4980; }")
        self.browser.snapshot()
        numbers = [n for n in self.browser.elements if n < SCAN_BASE]
        self.assertEqual(min(numbers), 1)
        self.assertLess(max(numbers), 100)

    def test_a_copied_element_gets_its_own_number(self):
        self.browser.open(self.url("/katalogus.html"))
        self.browser.page.evaluate("() => { const card = document.querySelector('.card');"
                                   " card.parentNode.insertBefore(card.cloneNode(true), card); }")
        self.browser.snapshot()
        for n in self.browser.elements:
            self.assertEqual(self.browser.page.locator(f'[data-jarvis-id="{n}"]').count(), 1, n)

    def test_cap_reports_what_did_not_fit(self):
        view = self.browser.open(self.url("/sok-gomb.html"))
        self.assertIn("(+20 elem nem fért ki)", view)

    def test_open_shadow_dom_is_seen_and_usable(self):
        view = self.browser.open(self.url("/arnyek.html"))
        self.assertIn("Sütiket használunk (árnyékban).", view)
        self.assertIn(f'[{self.number("E-mail cím")}] szövegmező "E-mail cím"', view, "címke az árnyékban")
        view = self.browser.click(self.number("Elfogadom"))
        self.assertIn("Elfogadva.", view)

    def test_form_without_form_tag_lists_its_submit_button_below(self):
        view = self.browser.open(self.url("/react-urlap.html"))
        lines = view.splitlines()
        below = lines.index("Lejjebb az űrlapon (görgetés nélkül is elérhetők):")
        self.assertEqual(lines[below + 1:], [f'[{self.number("Feltöltés")}] gomb "Feltöltés"'])
        self.assertNotIn("Kedvencekhez", view, "a lejjebb lévő egyéb gomb nem kerül be")
        # egy dobozban két kérdés: a rádiógombok a nevük szerint külön sorba kerülnek
        self.assertRegex(view, r"\nSzállítás \(választógombok\): \[\d+\] Posta, \[\d+\] Foxpost\n")
        self.assertRegex(view, r"\nSzállítás \(választógombok\): \[\d+\] Előre utalás, \[\d+\] Utánvét\n")
        # a közzététel továbbra is jóváhagyást kér (itt nincs, aki jóváhagyja)
        self.assertTrue(self.browser.click(self.number("Feltöltés")).startswith("A felhasználó nem engedélyezte"))


# --- mezők a fajtájuk szerint, választás listából, több lépés egy körben ----------------------------------

class ChoiceMatchTests(unittest.TestCase):
    def test_option_matching_is_strict_about_ambiguity(self):
        sizes = ["Egy méret", "Egyedi méret", "M", "Gyerek 104", "Gyerek 110"]
        self.assertEqual(match_option(sizes, "m"), (2, [2]), "a pontos egyezés nyer")
        self.assertEqual(match_option(sizes, "egyedi")[0], 1, "egyetlen ezzel kezdődő")
        self.assertEqual(match_option(sizes, "104")[0], 3, "egyetlen, amelyben egy szó így kezdődik")
        self.assertEqual(match_option(sizes, "egy"), (None, [0, 1]), "nem egyértelmű: a jelöltek")
        self.assertEqual(match_option(["Női pólók", "Férfi ingek"], "pólók női")[0], 0, "a legtöbb közös szó")
        self.assertEqual(match_option(sizes, "XL"), (None, []))
        self.assertEqual(match_option(sizes, "L"), (None, []), "az L nem az XL része")

    def test_wanted_state_of_a_checkbox(self):
        for text in ("igen", "Igen", "x", "✓", "be", "true", "Fekete"):
            self.assertIs(wanted_state(text, "Fekete"), True, text)
        for text in ("nem", "ki", "false"):
            self.assertIs(wanted_state(text, "Fekete"), False, text)
        self.assertIsNone(wanted_state("Sötétkék", "Fekete"))

    def test_same_site(self):
        self.assertTrue(same_site("https://api.vinted.hu/x", "https://www.vinted.hu/items/new"))
        self.assertTrue(same_site("https://static.bbc.co.uk/a.js", "https://www.bbc.co.uk/"))
        self.assertFalse(same_site("https://securepubads.g.doubleclick.net/x", "https://www.jofogas.hu/"))
        self.assertFalse(same_site("http://localhost:9/x", "http://127.0.0.1:8/"))
        self.assertTrue(same_site("http://127.0.0.1:9/x", "http://127.0.0.1:8/"))


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class FormKindTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = LocalSite()
        cls.profile = tempfile.TemporaryDirectory()
        cfg = load_config(ROOT / "config.toml").browser
        cfg.headless = True
        cfg.profile_dir = cls.profile.name
        cfg.pace = 0.0
        cls.asked: list[str] = []
        cls.browser = BrowserController(cfg, allow_local=True, confirm=lambda prompt: cls.asked.append(prompt) or True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.site.close()
        cls.profile.cleanup()

    def setUp(self):
        self.asked.clear()
        self.view = self.browser.open(self.site.url("urlap.html"))

    def el(self, label: str) -> int:
        return next(i for i, e in self.browser.elements.items() if e["label"] == label)

    def state(self, script: str):
        return self.browser.page.evaluate(script)

    def test_fill_sets_each_field_by_its_kind(self):
        note = self.browser.fill_fields([
            {"element": self.el("Cím"), "text": "Zara póló"}, {"element": self.el("Fekete"), "text": "igen"},
            {"element": self.el("Sötétkék"), "text": "Sötétkék"},
            {"element": self.el("Nagyon jó"), "text": "Nagyon jó"},
            {"element": self.el("Ruhaméret"), "text": "egyedi"}, {"element": self.el("Alku lehetséges"), "text": "✓"}])
        self.assertIn('Kitöltöttem: "Cím", "Fekete" (bejelöltem), "Sötétkék" (bejelöltem), "Nagyon jó" (bejelöltem), '
                      '"Ruhaméret" (Egyedi méret), "Alku lehetséges" (bejelöltem).', note)
        self.assertNotIn("Nem sikerült", note)
        self.assertEqual(self.state("() => [cim.value, fekete.checked, sotetkek.checked, nagyonjo.checked, "
                                    "ruhameret.value, alku.getAttribute('aria-checked')]"),
                         ["Zara póló", True, True, True, "Egyedi méret", "true"])
        note = self.browser.type_text(self.el("Fekete"), "igen")
        self.assertIn('"Fekete": már be volt jelölve.', note, "nem kattint rá még egyszer (az kivenné)")
        self.browser.type_text(self.el("Fekete"), "nem")
        self.assertFalse(self.state("() => fekete.checked"))

    def test_nothing_is_typed_into_what_is_not_a_text_field(self):
        with self.assertRaisesRegex(BrowserError, "nem szövegmező.*browser_choose"):
            self.browser.type_text(self.el("Méret"), "M")
        self.assertEqual(self.state("() => meret.textContent"), "Válassz", "nem mondja, hogy beírta")
        with self.assertRaisesRegex(BrowserError, "igen.*nem"):
            self.browser.type_text(self.el("Fekete"), "Zara póló")
        self.assertFalse(self.state("() => fekete.checked"))

    def test_an_ambiguous_option_lists_the_candidates_at_once(self):
        started = time.monotonic()
        with self.assertRaisesRegex(BrowserError, "Több lehetőség.*„Egy méret”, „Egyedi méret”"):
            self.browser.type_text(self.el("Ruhaméret"), "egy")
        with self.assertRaisesRegex(BrowserError, "Nincs ilyen lehetőség.*„Válassz”"):
            self.browser.type_text(self.el("Ruhaméret"), "óriás")
        self.assertLess(time.monotonic() - started, 4, "nem vár a nem létező feliratra")
        self.assertIn('Kiválasztottam: "Gyerek 104"', self.browser.type_text(self.el("Ruhaméret"), "104"))

    def test_choose_from_a_custom_dropdown(self):
        view = self.browser.choose(self.el("Méret"), "m")
        self.assertIn('Kiválasztottam: "M" ("Méret").', view)
        self.assertIn("Oldal: Új hirdetés", view, "a végén egy friss oldalnézet")
        self.assertEqual(self.state("() => [meret.textContent, meretek.hidden]"), ["M", True])

    def test_choose_walks_a_tree_level_by_level(self):
        view = self.browser.choose(self.el("Kategória"), "Nők > Ruházat > Pólók")
        self.assertIn('Kiválasztottam: "Nők > Ruházat > Pólók" ("Kategória").', view)
        self.assertEqual(self.state("() => document.getElementById('kat-ertek').textContent"),
                         "Nők > Ruházat > Pólók")
        self.browser.snapshot()
        with self.assertRaisesRegex(BrowserError, "Több lehetőség.*„Pól”.*„Pólók”, „Pólók és trikók”"):
            self.browser.choose(self.el("Kategória"), "Nők > Ruházat > Pól")

    def test_choose_types_into_a_searchable_field_first(self):
        self.assertIn('Kiválasztottam: "Zara" ("Márka").', self.browser.choose(self.el("Márka"), "Zara"))
        self.assertEqual(self.state("() => [marka.value, markak.children.length]"), ["Zara", 0])
        self.browser.snapshot()
        self.assertIn('Kiválasztottam: "Mango"', self.browser.choose(self.el("Márka listából"), "Mango"),
                      "a lista saját keresőjébe írta, mert a nyitott listában nem volt")
        self.assertEqual(self.state("() => document.getElementById('marka2-ertek').textContent"), "Mango")

    def test_a_list_that_stays_open_is_closed(self):
        self.browser.choose(self.el("Színek"), "Kék")
        self.assertEqual(self.state("() => [kek.checked, piros.checked, szinek.hidden]"), [True, False, True],
                         "a „Kész” gombbal bezárta")

    def test_several_steps_in_one_round(self):
        view = self.browser.do_actions([
            {"do": "type", "element": self.el("Cím"), "text": "Zara póló"},
            {"do": "check", "element": self.el("Fekete"), "text": "igen"},
            {"do": "choose", "element": self.el("Méret"), "text": "L"},
            {"do": "click", "element": self.el("Mentés")}])
        self.assertIn("Elvégeztem mind a 4 lépést", view)
        self.assertEqual(view.count("Oldal: "), 1, "egyetlen oldalnézet a végén")
        self.assertEqual(self.state("() => [out.textContent, fekete.checked, meret.textContent]"),
                         ["Mentve: Zara póló", True, "L"])

    def test_steps_stop_when_the_page_changes(self):
        view = self.browser.do_actions([{"do": "click", "element": self.el("Másik oldal")},
                                        {"do": "type", "element": self.el("Cím"), "text": "Zara"}])
        self.assertIn("A 2 lépésből 1 lett kész", view)
        self.assertIn("Megálltam, mert megváltozott az oldal címe", view)
        self.assertIn('Kihagytam: 2. gépelés', view)
        self.assertIn("Oldal: Kávéfőző X200", view)

        self.browser.open(self.site.url("urlap.html"))
        view = self.browser.do_actions([{"do": "click", "element": self.el("Részletek")},
                                        {"do": "click", "element": self.el("Mentés")}])
        self.assertIn("felugró ablak nyílt meg", view)
        self.assertEqual(self.state("() => out.textContent"), "", "a Mentés kimaradt")

        self.browser.open(self.site.url("urlap.html"))
        view = self.browser.do_actions([{"do": "click", "element": self.el("Frissítés")},
                                        {"do": "click", "element": self.el("Mentés")}])
        self.assertIn("már nem az, ami az oldalnézetben volt", view, "az oldal újrarajzolta a gombot")
        self.assertEqual(self.state("() => out.textContent"), "")

    def test_a_risky_checkbox_is_not_a_publish_and_esc_is_recorded(self):
        events: list[dict] = []
        self.browser.on_action = events.append
        try:
            self.browser.open(self.site.url("feltetelek.html"))
            self.browser.type_text(self.el("Megerősítem, hogy elfogadom a feltételeket"), "igen")
            self.browser.choose(self.el("Színek"), "Kék")
            self.browser.click(self.el("Feltöltés"))
        finally:
            self.browser.on_action = None
        self.assertEqual(self.state("() => [elfogad.checked, kek.checked, szinek.hidden, out.textContent]"),
                         [True, True, True, "Feltöltve"])
        self.assertEqual(len(self.asked), 2, "a feltételek elfogadása és a közzététel is jóváhagyással")
        clicks = [e for e in events if e["do"] == "click"]
        terms, publish = clicks[0], clicks[-1]
        self.assertEqual((terms["el"]["kind"], terms["risky"], terms.get("check")), ("input:checkbox", True, True),
                         "a kockázatos jelölőnégyzet: jelölőnégyzetként, nem gombként")
        self.assertEqual((publish["el"]["label"], publish["risky"], publish.get("check")), ("Feltöltés", True, None))
        presses = [i for i, e in enumerate(events) if e["do"] == "press"]
        self.assertEqual([events[i] for i in presses], [{"do": "press", "key": "Escape"}],
                         "a nyitva maradt listát Esc-cel zárta be, és ez is a lépések között van")
        self.assertEqual(events[presses[0] - 1]["el"]["label"], "Kék")

    def test_a_risky_checkbox_clicked_directly_is_not_a_publish_either(self):
        events: list[dict] = []
        self.browser.on_action = events.append
        try:
            self.browser.open(self.site.url("feltetelek.html"))
            self.browser.click(self.el("Megerősítem, hogy elfogadom a feltételeket"))
        finally:
            self.browser.on_action = None
        self.assertTrue(self.state("() => elfogad.checked"))
        clicks = [e for e in events if e["do"] == "click"]
        self.assertEqual([(e["risky"], e.get("check")) for e in clicks], [(True, True)],
                         "browser_click-kel is jelölőnégyzetként jegyzi, nem a közzététel gombjaként")

    def test_steps_keep_the_approval_rules(self):
        with self.assertRaisesRegex(BrowserError, "csak utolsóként"):
            self.browser.do_actions([{"do": "click", "element": self.el("Közzététel")},
                                     {"do": "click", "element": self.el("Mentés")}])
        with self.assertRaisesRegex(BrowserError, "fizetős"):
            self.browser.do_actions([{"do": "click", "element": self.el("Kiemelés 990 Ft")}])
        with self.assertRaisesRegex(BrowserError, "legfeljebb 5"):
            self.browser.do_actions([{"do": "click", "element": self.el("Mentés")}] * 6)
        self.assertEqual(self.state("() => out.textContent"), "", "semmit nem csinált")
        self.assertEqual(self.asked, [])
        view = self.browser.do_actions([{"do": "type", "element": self.el("Cím"), "text": "Zara"},
                                        {"do": "click", "element": self.el("Közzététel")}])
        self.assertIn("Közzététel", self.asked[0], "a kockázatos lépés utolsóként jóváhagyással mehet")
        self.assertIn("Elvégeztem mind a 2 lépést", view)
        self.assertEqual(self.state("() => out.textContent"), "Közzétéve")


# --- eszközök a böngészővel, látás, tömörítés ----------------------------------------------

class FakeMss:
    """Hamis mss: 400x200-as, félig piros képernyő."""

    def __init__(self):
        self.monitors = [{"left": 0, "top": 0, "width": 400, "height": 200}] * 2

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def grab(self, monitor):
        width, height = monitor["width"], monitor["height"]
        row = b"\xff\x00\x00" * (width // 2) + b"\x00\x00\x00" * (width - width // 2)
        return types.SimpleNamespace(size=(width, height), rgb=row * height)


def fake_mss_module():
    module = types.ModuleType("mss")
    module.mss = FakeMss
    module.__spec__ = importlib.machinery.ModuleSpec("mss", None)
    return module


class VisionAndToolTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        self.cfg.screen.max_width = 200
        self.llm = OllamaClient(self.cfg.llm)

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def test_look_at_screen_sends_image_to_model(self):
        self.fake.chat_script = [reply("Egy piros hibaüzenet látszik: Fájl nem található.")]
        with mock.patch.dict(sys.modules, {"mss": fake_mss_module()}):
            box = Toolbox(self.cfg, llm=self.llm)
            self.assertIn("look_at_screen", box.names())
            result = box.call("look_at_screen", {"question": "Mi ez a hiba?"})
        self.assertIn("Fájl nem található", result)
        message = self.fake.chat_bodies()[0]["messages"][0]
        self.assertIn("Mi ez a hiba?", message["content"])
        from PIL import Image

        image = Image.open(io.BytesIO(base64.b64decode(message["images"][0])))
        self.assertEqual(image.size, (200, 100))   # kicsinyítve a max_width-re
        self.assertFalse(self.fake.chat_bodies()[0]["think"])

    def test_look_runs_inside_the_conversation(self):
        # A kép a futó beszélgetés végére kerül (ugyanaz a rendszerprompt, előzmény, eszközök), így az Ollama
        # gyorsítótárában a beszélgetés eleje megmarad; a kép nem kerül az előzmények közé.
        self.fake.chat_script = [tool_reply("look_at_screen", {"question": "Mi ez a hiba?"}),
                                 reply("Egy piros hibaüzenet: Fájl nem található."),
                                 reply("A Fájl nem található hibát látom.")]
        with mock.patch.dict(sys.modules, {"mss": fake_mss_module()}):
            box = Toolbox(self.cfg, llm=self.llm)
            assistant = Assistant(self.cfg, self.llm, box, "rendszer")
            self.assertEqual(assistant.ask("Mit látsz a képernyőn?"), "A Fájl nem található hibát látom.")
        first, look, last = self.fake.chat_bodies()
        self.assertEqual(look["messages"][:2], first["messages"])
        self.assertEqual(look["messages"][2], last["messages"][2], "az eszközhívás is közös előzmény")
        self.assertEqual(look["messages"][3]["role"], "tool")
        self.assertTrue(look["messages"][-1]["images"])
        self.assertIn("Mi ez a hiba?", look["messages"][-1]["content"])
        self.assertEqual(look["tools"], first["tools"])
        self.assertEqual(look["options"]["temperature"], first["options"]["temperature"])
        self.assertEqual(look["options"]["num_predict"], 400)
        self.assertEqual(look["think"], first["think"])
        self.assertIn("Fájl nem található", last["messages"][-1]["content"])
        self.assertFalse(any("images" in m for m in last["messages"]))
        self.assertFalse(any("images" in m for m in assistant.history))
        self.assertIsNone(box.conversation, "a kérés után nincs futó beszélgetés")

    def test_look_falls_back_to_a_separate_question(self):
        # Ha a beszélgetésben eszközt hívna szöveg helyett, külön (előzmények nélküli) kérdés lesz belőle.
        self.fake.chat_script = [tool_reply("look_at_screen", {"question": "Mi ez?"}),
                                 tool_reply("get_datetime", {}), reply("Egy piros ablak."), reply("Piros ablak.")]
        with mock.patch.dict(sys.modules, {"mss": fake_mss_module()}):
            box = Toolbox(self.cfg, llm=self.llm)
            assistant = Assistant(self.cfg, self.llm, box, "rendszer")
            self.assertEqual(assistant.ask("Mit látsz?"), "Piros ablak.")
        separate = self.fake.chat_bodies()[2]
        self.assertEqual(len(separate["messages"]), 1)
        self.assertNotIn("tools", separate)
        self.assertIn("Egy piros ablak.", assistant.history[2]["content"])
        self.assertEqual([m["role"] for m in assistant.history], ["user", "assistant", "tool", "assistant"])

    def test_look_is_separate_when_the_image_would_not_fit(self):
        # Ha a kép már nem férne el a beszélgetés kontextusában, külön (előzmények nélküli) kérdés lesz belőle.
        self.fake.chat_script = [tool_reply("look_at_screen", {"question": "Mi ez?"}), reply("Egy piros ablak."),
                                 reply("Piros ablak.")]
        with mock.patch.dict(sys.modules, {"mss": fake_mss_module()}), \
                mock.patch.object(assistant_module, "IMAGE_TOKENS", 10**6):
            box = Toolbox(self.cfg, llm=self.llm)
            assistant = Assistant(self.cfg, self.llm, box, "rendszer")
            self.assertEqual(assistant.ask("Mit látsz?"), "Piros ablak.")
        separate = self.fake.chat_bodies()[1]
        self.assertEqual(len(separate["messages"]), 1)
        self.assertTrue(separate["messages"][0]["images"])

    def test_no_vision_tools_without_model(self):
        with mock.patch.dict(sys.modules, {"mss": fake_mss_module()}):
            box = Toolbox(self.cfg)
        self.assertNotIn("look_at_screen", box.names())
        self.assertNotIn("browser_look", box.names())

    def test_wrong_monitor_is_reported(self):
        self.cfg.screen.monitor = 5
        with mock.patch.dict(sys.modules, {"mss": fake_mss_module()}):
            box = Toolbox(self.cfg, llm=self.llm)
            result = box.call("look_at_screen", {"question": "x"})
        self.assertIn("Nincs 5. monitor", result)

    def test_loaded_status(self):
        self.fake.ps = {"models": [{"name": "qwen3.8:27b", "size": 10, "size_vram": 10, "context_length": 16384}]}
        self.assertEqual(self.llm.loaded_status()["context_length"], 16384)
        self.fake.ps = {"models": []}
        self.assertIsNone(self.llm.loaded_status())

    def _paging_assistant(self, context_tokens: int) -> Assistant:
        box = Toolbox(self.cfg)
        pages = iter(["OLDAL-1 " * 50, "OLDAL-2 " * 50, "OLDAL-3 " * 50])
        box.register("page", "teszt oldalnézet", params(), lambda: next(pages), ephemeral=True)
        self.fake.chat_script = [tool_reply("page", {}), tool_reply("page", {}), tool_reply("page", {}),
                                 reply("kész")]
        assistant = Assistant(self.cfg, self.llm, box, "rendszer")
        assistant.context_tokens = context_tokens
        return assistant

    def test_old_page_views_compacted_when_context_fills(self):
        assistant = self._paging_assistant(context_tokens=900)
        self.assertEqual(assistant.ask("lapozz"), "kész")
        contents = [m["content"][:7] for m in assistant.history if m["role"] == "tool"]
        self.assertEqual(contents[0], STALE_NOTE[:7])
        self.assertEqual(contents[-1], "OLDAL-3")   # a legfrissebb oldal mindig teljes
        # az utolsó modellhívás a legelső oldalt már nem látta
        self.assertNotIn("OLDAL-1", str(self.fake.chat_bodies()[-1]["messages"]))

    def test_compaction_keeps_the_prompt_cache(self):
        # Bőven van hely, az előző oldalnézet mégis rögtön rövid sorra cserélődik, amint új jön: az az előző
        # kérés utolsó üzenete, így a kérések eleje szó szerint ismétlődik (a prompt-gyorsítótár megmarad),
        # és minden kérésben csak egy teljes oldalnézet van.
        from test_context import first_difference, page_view

        box = Toolbox(self.cfg)
        pages = iter([page_view(1), page_view(2), page_view(3)])
        box.register("page", "teszt oldalnézet", params(), lambda: next(pages), ephemeral=True)
        self.fake.chat_script = [tool_reply("page", {}), tool_reply("page", {}), tool_reply("page", {}),
                                 reply("kész")]
        assistant = Assistant(self.cfg, self.llm, box, "rendszer")
        assistant.context_tokens = 100_000
        self.assertEqual(assistant.ask("lapozz"), "kész")
        bodies = self.fake.chat_bodies()
        for earlier, later in zip(bodies, bodies[1:]):
            n = len(earlier["messages"])
            self.assertGreaterEqual(first_difference(earlier["messages"], later["messages"]), n - 1)
            self.assertEqual(sum("Kezelhető elemek" in m["content"] for m in later["messages"]), 1)

    @unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
    def test_browser_tools_through_toolbox(self):
        site = LocalSite()
        try:
            self.cfg.browser.headless = True
            self.cfg.browser.profile_dir = str(Path(self.tmp.name) / "profil")
            self.cfg.web.allow_local = True
            self.fake.chat_script = [reply("Egy webbolt főoldala, kávéfőző akcióval.")]
            box = Toolbox(self.cfg, llm=self.llm)
            for name in ("browser_open", "browser_click", "browser_type", "browser_scroll",
                         "browser_back", "browser_read", "browser_look", "browser_choose", "browser_do"):
                self.assertIn(name, box.names())
            view = box.call("browser_open", {"url": site.url("index.html")})
            self.assertIn("Kezelhető elemek", view)
            # a modell néha szövegként vagy "#3" alakban adja meg a számot
            search = next(i for i, e in box.browser.elements.items() if e["kind"] == "input:search")
            view = box.call("browser_type", {"element": f"#{search}", "text": "kávé", "submit": "true"})
            self.assertIn("Találatok: kávé", view)
            self.assertTrue(box.call("browser_click", {"element": "nem szám"}).startswith("HIBA"))
            box.call("browser_open", {"url": site.url("index.html")})
            name = next(i for i, e in box.browser.elements.items() if e["label"] == "Név")
            size = next(i for i, e in box.browser.elements.items() if e.get("select"))
            actions = json.dumps([{"do": "type", "element": f"#{name}", "text": "Teszt Elek"},
                                  {"do": "choose", "element": size, "text": "nagy"}])
            view = box.call("browser_do", {"actions": actions})     # a listát néha szövegként küldi
            self.assertIn("Elvégeztem mind a 2 lépést", view)
            self.assertIn('kiválasztva: "Nagy"', view)
            size = next(i for i, e in box.browser.elements.items() if e.get("select"))
            self.assertIn('Kiválasztottam: "Kicsi"', box.call("browser_choose", {"element": size, "option": "kicsi"}))
            self.assertTrue(box.call("browser_do", {"actions": "nem lista"}).startswith("HIBA"))
            look = box.call("browser_look", {"question": "Mi ez az oldal?"})
            self.assertIn("webbolt", look)
            self.assertTrue(self.fake.chat_bodies()[0]["messages"][0]["images"])
            box.close()
            self.assertFalse(box._browser)
        finally:
            site.close()


# --- az oldal adatkérései: kivárja őket, de nem marad utánuk semmi a memóriában --------------------------

NETWORK_PAGES = {
    # A találatok későn jönnek, és közben semmi nem változik a lapon (nincs „Betöltés…” felirat sem).
    "/lassu.html": """<!doctype html><html><head><meta charset="utf-8"><title>Webshop</title></head><body>
<h1>Találatok</h1><ul id="lista"></ul>
<script>fetch('/adat').then(r => r.json()).then(d => { for (const t of d) {
  const li = document.createElement('li'); li.textContent = t; document.getElementById('lista').appendChild(li); } });
</script></body></html>""",
    # Mint egy hirdetési oldal analitikája: sok apró kérés.
    "/sok.html": """<!doctype html><html><head><meta charset="utf-8"><title>Sok kérés</title></head><body>
<h1>Profil</h1><script>window.kesz = false;
const keresek = Array.from({length: 300}, (_, i) => fetch('/apro?i=' + i).then(r => r.text()));
Promise.all(keresek).then(() => window.kesz = true);
</script></body></html>""",
    # Semmi nem tölt: egy kattintás után azonnal kész.
    "/statikus.html": """<!doctype html><html><head><meta charset="utf-8"><title>Statikus</title></head><body>
<p id="szoveg">Eredeti</p><button onclick="szoveg.textContent = 'Átírva'">Átírás</button></body></html>""",
    # A tartalom a betöltés után 600 ms-mal, egy időzítőből jön (közben semmi nem változik, és nem is tölt).
    "/kesobb.html": """<!doctype html><html><head><meta charset="utf-8"><title>Később</title></head><body>
<h1>Hirdetés</h1><script>setTimeout(() => { const p = document.createElement('p');
  p.textContent = 'Későn jött tartalom'; document.body.appendChild(p); }, 600);</script></body></html>""",
    # Kattintásra indul a (lassú) adatkérés.
    "/gomb.html": """<!doctype html><html><head><meta charset="utf-8"><title>Gomb</title></head><body>
<ul id="lista"></ul><button onclick="fetch('/adat').then(r => r.json()).then(d => { for (const t of d) {
  const li = document.createElement('li'); li.textContent = t; lista.appendChild(li); } })">Betöltés</button>
</body></html>""",
    # Mint egy reklám és egy statisztika: egy másik oldal lassú kérései és egy beacon. Ezekre nem kell várni.
    "/kulso.html": """<!doctype html><html><head><meta charset="utf-8"><title>Reklámos</title></head><body>
<p id="szoveg">Hirdetés</p>
<button onclick="fetch('KULSO/lassu?gomb', {mode: 'no-cors'}); szoveg.textContent = 'Frissítve'"
  >Hirdetés frissítése</button>
<script>fetch('KULSO/lassu?betolt', {mode: 'no-cors'}); navigator.sendBeacon('/beacon', 'megnyitva');</script>
</body></html>""",
}


class _NetworkHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/adat":
            time.sleep(1.5)
            body, kind = json.dumps(["Kávéfőző X200 – 39 990 Ft"]).encode("utf-8"), "application/json"
        elif path == "/apro":
            body, kind = b"ok", "text/plain"
        elif path in NETWORK_PAGES:
            page = NETWORK_PAGES[path].replace("KULSO", f"http://localhost:{getattr(self.server, 'other_port', 0)}")
            body, kind = page.encode("utf-8"), "text/html; charset=utf-8"
        else:
            body, kind = b"", "text/plain"
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/beacon":   # a statisztika lassan válaszol
            self.server.beacons.append(time.monotonic())
            time.sleep(4)
        self.send_response(204)
        self.end_headers()


class _AdHandler(BaseHTTPRequestHandler):
    """Egy másik oldal (reklámszerver), ami lassan válaszol."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.hits.append(time.monotonic())
        time.sleep(4)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b"ok")


class NetworkBusyTests(unittest.TestCase):
    def test_a_request_that_never_ends_is_not_waited_for_forever(self):
        page = types.SimpleNamespace(_jarvis_inflight={"regi": time.monotonic() - 100, "uj": time.monotonic()})
        self.assertTrue(BrowserController._network_busy(page))
        self.assertEqual(list(page._jarvis_inflight), ["uj"], "a régóta függőt elengedte")
        page._jarvis_inflight.clear()
        self.assertFalse(BrowserController._network_busy(page))
        self.assertFalse(BrowserController._network_busy(types.SimpleNamespace()), "figyelés nélkül sem hiba")


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class NetworkWaitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _NetworkHandler)
        cls.other = ThreadingHTTPServer(("127.0.0.1", 0), _AdHandler)   # „localhost” néven: másik oldal
        cls.server.other_port, cls.server.beacons, cls.other.hits = cls.other.server_address[1], [], []
        for server in (cls.server, cls.other):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        cls.profile = tempfile.TemporaryDirectory()
        cfg = load_config(ROOT / "config.toml").browser
        cfg.headless = True
        cfg.profile_dir = cls.profile.name
        cfg.pace = 0.0
        cls.browser = BrowserController(cfg, allow_local=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        for server in (cls.server, cls.other):
            server.shutdown()
            server.server_close()
        cls.profile.cleanup()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}{path}"

    def find(self, label: str) -> int:
        return next(i for i, e in self.browser.elements.items() if e["label"] == label)

    def timed_settles(self) -> list[float]:
        """A _settle hívások ideje (a teszt végéig mérve)."""
        durations: list[float] = []
        original = self.browser._settle

        def timed(*args, **kwargs):
            started = time.monotonic()
            try:
                return original(*args, **kwargs)
            finally:
                durations.append(time.monotonic() - started)

        self.browser._settle = timed
        self.addCleanup(lambda: delattr(self.browser, "_settle"))
        return durations

    def test_a_static_page_settles_at_once(self):
        self.browser.open(self.url("/statikus.html"))
        durations = self.timed_settles()
        self.browser._settle(self.browser.page)
        view = self.browser.click(self.find("Átírás"))
        self.assertIn("Átírva", view)
        self.assertEqual(len(durations), 2)
        self.assertLess(max(durations), 0.4, f"nincs fix várakozás: {durations}")

    def test_late_content_is_still_waited_for(self):
        view = self.browser.open(self.url("/kesobb.html"))
        self.assertIn("Későn jött tartalom", view, "a betöltés után időzítővel beszúrt részt is megvárta")
        self.browser.open(self.url("/gomb.html"))
        view = self.browser.click(self.find("Betöltés"))
        self.assertIn("Kávéfőző X200", view, "a kattintásra indult saját adatkérést megvárta")

    def test_other_sites_requests_do_not_hold_it(self):
        self.browser.open(self.url("/kulso.html"))
        durations = self.timed_settles()
        self.browser._settle(self.browser.page)
        view = self.browser.click(self.find("Hirdetés frissítése"))
        self.assertIn("Frissítve", view)
        self.assertGreaterEqual(len(self.other.hits), 2, "a lassú kérések tényleg elindultak (betöltéskor és most)")
        self.assertTrue(self.server.beacons, "a beacon is elindult")
        self.assertLess(max(durations), 1.5, f"a reklám és a statisztika nem tartja vissza: {durations}")

    def test_waits_for_data_that_arrives_late(self):
        view = self.browser.open(self.url("/lassu.html"))
        self.assertIn("Kávéfőző X200", view, "megvárta a lassú adatkérést")
        self.assertFalse(BrowserController._network_busy(self.browser.page))

    def test_requests_leave_nothing_behind(self):
        # Régen a Playwright minden kérésről és válaszról tartott egy Python-objektumot (típusonként 10 000-ig):
        # egy hosszabb böngészésnél több száz MB, és egyszer MemoryError lett belőle.
        self.browser.open(self.url("/sok.html"))
        page = self.browser.page
        page.wait_for_function("() => window.kesz === true", timeout=30000)
        self.browser.snapshot()    # a közben jött események feldolgozása
        objects = getattr(getattr(getattr(page, "_impl_obj", None), "_connection", None), "_objects", None)
        if objects is None:
            self.skipTest("a Playwright belső szerkezete megváltozott")
        self.assertLess(len(objects), 150, "300 kérés után sem maradt utánuk objektum (régen kérésenként kettő)")

    def test_the_dossier_screenshot_is_tried_again(self):
        self.browser.open(self.url("/lassu.html"))
        page = self.browser.page
        original, calls = page.screenshot, []

        def flaky(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:   # pl. egy korábbi eseménykezelő hibája, amit a Playwright a következő hívásnál dob
                raise MemoryError()
            return original(**kwargs)

        page.screenshot = flaky
        try:
            shot, view = self.browser.capture()
        finally:
            del page.screenshot
        self.assertTrue(shot.startswith(b"\xff\xd8"), "másodszorra elkészült")
        self.assertFalse(calls[1]["full_page"], "másodszor csak a látható részről")
        self.assertEqual(self.browser.capture_error, "")
        self.assertIn("Találatok", view)

        def broken(**kwargs):
            raise MemoryError()

        page.screenshot = broken
        try:
            shot, _view = self.browser.capture()
        finally:
            del page.screenshot
        self.assertIsNone(shot)
        self.assertEqual(self.browser.capture_error, "MemoryError", "a hibacsomagban látszik, miért nincs kép")


if __name__ == "__main__":
    unittest.main(verbosity=2)
