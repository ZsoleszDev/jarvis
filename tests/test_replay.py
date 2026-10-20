"""A begyakorolt lépések tesztjei: a modell egyszer végigcsinál egy feltöltést (vagy levételt) egy
helyi „hirdetési oldalon”, Jarvis megjegyzi, és a következő tárgynál már modell nélkül csinálja.

A böngészős tesztek valódi (láthatatlan) Chromiumot indítanak helyi tesztoldalakkal; ha a Playwright
vagy a Chromium nincs telepítve, ezek kimaradnak. Internet és modell nem kell.

Futtatás:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import dataclasses
import functools
import re
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_offline import FakeOllama, make_cfg, reply, tool_reply  # noqa: E402
from test_streaming import RecordingUI  # noqa: E402
from test_web_browser import HAS_CHROMIUM  # noqa: E402
from test_work import SHIRT, _QuietHandler, element_number, jpeg  # noqa: E402

from jarvis_core import replay, selling  # noqa: E402
from jarvis_core.app import JarvisApp  # noqa: E402
from jarvis_core.browser import distinct_options  # noqa: E402
from jarvis_core.replay import FlowRecorder, Replayer, field_for, match_field, url_shape  # noqa: E402
from jarvis_core.selling import NOT_POSTED, POSTED, Platform  # noqa: E402

# --- egy helyi „hirdetési oldal”: süti-ablak, fotók, mezők, kategória, állapot, márka, közzététel ---

FORM = """<!doctype html><html lang="hu"><head><meta charset="utf-8"><title>Új hirdetés</title></head><body>
<div id="cookie"><p>Sütiket használunk.</p>
  <button type="button" onclick="document.getElementById('cookie').remove()">Elfogadom</button></div>
<h1>Új hirdetés</h1>
<button type="button" onclick="document.getElementById('files').click()">Fotók feltöltése</button>
<input type="file" id="files" accept="image/*" multiple style="display:none">
<ul id="list"></ul>
<label>TITLE_LABEL <input name="TITLE_NAME" id="title"></label>
<label>Leírás <textarea name="description" id="desc"></textarea></label>
<button type="button" id="catbtn" onclick="document.getElementById('cats').hidden = false">Kategória</button>
<div id="cats" hidden>
  <div role="option" onclick="pick('Pólók')">Pólók</div>
  <div role="option" onclick="pick('Nadrágok')">Nadrágok</div>
</div>
<input type="hidden" id="cat">
<fieldset><legend>Állapot</legend>
  <label><input type="radio" name="allapot" value="uj"> Új</label>
  <label><input type="radio" name="allapot" value="nagyonjo"> Nagyon jó</label>
  <label><input type="radio" name="allapot" value="jo"> Jó</label>
</fieldset>
<label>Márka <input name="brand" id="brand" autocomplete="off"></label>
<div id="brands"></div>
<label>Ár <input name="price" id="price"></label>
<button type="button" onclick="feltolt()">Feltöltés</button>
<script>
function pick(name) {
  document.getElementById('cat').value = name;
  document.getElementById('cats').hidden = true;
  document.getElementById('catbtn').textContent = 'Kategória: ' + name;
}
document.getElementById('files').addEventListener('change', (e) => {
  for (const f of e.target.files) {
    const li = document.createElement('li'); li.textContent = 'Fotó: ' + f.name;
    document.getElementById('list').appendChild(li);
  }
});
document.getElementById('brand').addEventListener('input', (e) => {
  const box = document.getElementById('brands');
  box.innerHTML = '';
  for (const b of ['Zara', 'H&M', 'Mango']) {
    if (!e.target.value || !b.toLowerCase().startsWith(e.target.value.toLowerCase())) continue;
    const d = document.createElement('div');
    d.setAttribute('role', 'option');
    d.textContent = b;
    d.onclick = () => { document.getElementById('brand').value = b; box.innerHTML = ''; };
    box.appendChild(d);
  }
});
function feltolt() {
  const cond = document.querySelector('input[name=allapot]:checked');
  const q = new URLSearchParams({title: document.getElementById('title').value,
    desc: document.getElementById('desc').value, price: document.getElementById('price').value,
    cat: document.getElementById('cat').value, cond: cond ? cond.parentElement.textContent.trim() : '',
    brand: document.getElementById('brand').value, photos: document.querySelectorAll('#list li').length});
  location.href = '/kesz.html?' + q;
}
</script></body></html>"""



def form(label: str = "Cím", name: str = "title") -> str:
    return FORM.replace("TITLE_LABEL", label).replace("TITLE_NAME", name)


LISTING = """<!doctype html><html lang="hu"><head><meta charset="utf-8"><title>Hirdetésem</title></head><body>
<h1>Saját hirdetés</h1><p>Állapot: aktív</p>
<button type="button" onclick="document.getElementById('sure').hidden = false">Törlés</button>
<div id="sure" role="dialog" hidden><p>Biztosan törlöd a hirdetést?</p>
  <button type="button" onclick="location.href = '/torolve.html?' + location.search.slice(1)">Igen, törlöm</button>
  <button type="button" onclick="document.getElementById('sure').hidden = true">Mégse</button></div>
</body></html>"""

PAGES = {
    "urlap.html": form(),
    "kesz.html": "<!doctype html><html><head><meta charset='utf-8'><title>Kész</title></head>"
                 "<body><h1>Hirdetésed feltöltve</h1></body></html>",
    "hirdetes.html": LISTING,
    "torolve.html": "<!doctype html><html><head><meta charset='utf-8'><title>Törölve</title></head>"
                    "<body><h1>A hirdetést töröltük</h1></body></html>",
    "fooldal.html": "<!doctype html><html><head><meta charset='utf-8'><title>Főoldal</title></head>"
                    "<body><h1>Teszt piactér</h1></body></html>",
}


class Site:
    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        for name, content in PAGES.items():
            self.write(name, content)
        handler = functools.partial(_QuietHandler, directory=self.dir.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def write(self, name: str, content: str) -> None:
        (Path(self.dir.name) / name).write_text(content, encoding="utf-8")

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/{path}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()


def last_tool(body) -> str:
    return next(m["content"] for m in reversed(body["messages"]) if m["role"] == "tool")


def click(label):
    return lambda body: (200, tool_reply("browser_click", {"element": element_number(last_tool(body), label)}))


def numbered(body, label) -> int:
    """Egy számozott kérdésben (lásd Replayer._ask) a felirat sorszáma."""
    match = re.search(rf"^(\d+)\. „{re.escape(label)}”", body["messages"][-1]["content"], re.M)
    if not match:
        raise AssertionError(f"nincs ilyen lehetőség: {label}\n{body['messages'][-1]['content']}")
    return int(match.group(1))


# --- a felvétel feldolgozása (böngésző nélkül) ----------------------------------------------------

VALUES = {"title": "Zara fekete póló, L", "description": "Egyszer hordtam.", "price": "500", "brand": "Zara",
          "size": "L", "condition": "Nagyon jó", "color": "fekete", "material": "", "category": "Nők > Ruhák > Pólók",
          "product": "fekete póló", "location": "Budapest", "shipping": "Foxpost", "parcel_size": "kicsi",
          "kind": "ruha"}


def el(kind, label, field=""):
    return {"kind": kind, "label": label, "field": field, "href": "", "ordinal": 0}


class RecorderTests(unittest.TestCase):
    def recorder(self, events, task="post"):
        rec = FlowRecorder("vinted", task, "vinted-post-ruha-x", "https://www.vinted.hu/items/new", ("vinted.hu",))
        rec.add({"do": "goto", "url": "https://www.vinted.hu/items/new", "final": "https://www.vinted.hu/items/new"})
        for event in events:
            rec.add(event)
        return rec

    def test_fields_choices_and_publish_become_a_recipe(self):
        rec = self.recorder([
            {"do": "click", "el": el("button", "Elfogadom")},
            {"do": "upload", "el": None},
            {"do": "type", "el": el("input:text", "Cím", "title"), "text": "Zara fekete póló, L"},
            {"do": "type", "el": el("textarea", "Leírás", "description"), "text": "Egyszer hordtam."},
            {"do": "type", "el": el("input:text", "Ár", "price"), "text": "500 Ft"},
            {"do": "click", "el": el("button", "Kategória")},
            {"do": "click", "el": el("option", "Nők")},
            {"do": "click", "el": el("option", "Pólók")},
            {"do": "click", "el": el("input:radio", "Nagyon jó", "allapot")},
            {"do": "click", "el": el("button", "Méret")},
            {"do": "click", "el": el("option", "L")},
            {"do": "click", "el": el("button", "Feltöltés"), "risky": True,
             "url": "https://www.vinted.hu/items/new", "url_after": "https://www.vinted.hu/items/123-zara"},
        ])
        recording, reason = rec.build(VALUES)
        self.assertIsNotNone(recording, reason)
        steps = recording["steps"]
        self.assertTrue(steps[0]["optional"], "a süti-ablak nem mindig jön elő: kihagyható")
        self.assertEqual([s.get("value") for s in steps if s["do"] == "type"], ["title", "description", "price"])
        memory = recording["memory"]
        self.assertEqual([a["el"]["label"] for a in memory["category"]["nok ruhak polok"]], ["Nők", "Pólók"],
                         "a kategória feliratai az értékhez kötve (más kategóriánál nem játssza vissza)")
        self.assertIn("*", memory["condition"], "a felirat maga az érték: bármelyik állapotnál működik")
        self.assertIn("*", memory["size"])
        self.assertTrue(steps[-1]["risky"])
        self.assertEqual(recording["success"], {"url": "www.vinted.hu/items/#"})

    def test_unclean_runs_are_not_learned(self):
        cases = {
            "visszalépett": [{"do": "back"}],
            "nem engedélyeztél": [{"do": "declined", "el": el("button", "Feltöltés")}],
            "a tárgy adataihoz kötni": [{"do": "type", "el": el("input:text", "Cím", "title"),
                                                   "text": "Valami egészen más szöveg"}],
            "a tárgy saját hirdetésére": [{"do": "click", "el": el("link", "Zara fekete póló L-es")}],
            "más oldalra": [{"do": "goto", "url": "https://example.com/", "final": "https://example.com/"}],
        }
        publish = {"do": "click", "el": el("button", "Feltöltés"), "risky": True, "url": "a", "url_after": "b"}
        for expected, events in cases.items():
            recording, reason = self.recorder([*events, publish]).build(VALUES)
            self.assertIsNone(recording, expected)
            self.assertIn(expected, reason)
        recording, reason = self.recorder([{"do": "upload", "el": None}]).build(VALUES)
        self.assertIsNone(recording)
        self.assertIn("közzététel", reason)

    # Egy tiszta futás és ugyanez a modell szokásos kitérőivel: a recept ugyanaz legyen.
    CLEAN = [
        {"do": "type", "el": el("input:text", "Cím", "title"), "text": "Zara fekete póló, L"},
        {"do": "click", "el": el("button", "Kategória")},
        {"do": "click", "el": el("option", "Pólók")},
        {"do": "type", "el": el("input:text", "Ár", "price"), "text": "500"},
        {"do": "click", "el": el("button", "Feltöltés"), "risky": True, "url": "https://www.vinted.hu/items/new",
         "url_after": "https://www.vinted.hu/items/9"},
    ]

    def test_a_corrected_field_is_learned_like_a_clean_run(self):
        clean, _ = self.recorder(self.CLEAN).build(VALUES)
        self.assertIsNotNone(clean)
        wrong = {"do": "type", "el": el("input:text", "Cím", "title"), "text": "Valami egészen más szöveg"}
        recording, reason = self.recorder([wrong, *self.CLEAN]).build(VALUES)
        self.assertIsNotNone(recording, reason)
        self.assertEqual(recording["steps"], clean["steps"], "csak az utolsó beírás számít")
        self.assertEqual(recording["memory"], clean["memory"])
        # A márkakereső más: ha közben kattintott (a lista egy lehetőségére), a korábbi beírás is kell.
        brand = [{"do": "type", "el": el("input:text", "Márka", "brand"), "text": "Za"},
                 {"do": "click", "el": el("option", "Zara")},
                 {"do": "type", "el": el("input:text", "Márka", "brand"), "text": "Zara"}]
        events, _ = self.recorder(brand).normalized()
        self.assertEqual([e["do"] for e in events], ["goto", "type", "click", "type"])

    def test_back_drops_the_abandoned_branch(self):
        clean, _ = self.recorder([{"do": "click", "el": el("link", "Ruházat"), "url": "https://www.vinted.hu/items/new",
                                   "url_after": "https://www.vinted.hu/items/new/ruhazat"}, *self.CLEAN]).build(VALUES)
        self.assertIsNotNone(clean)
        detour = [{"do": "click", "el": el("link", "Elektronika"), "url": "https://www.vinted.hu/items/new",
                   "url_after": "https://www.vinted.hu/items/new/elektronika"},
                  {"do": "scroll"},
                  {"do": "click", "el": el("button", "Mutass többet")},
                  {"do": "back"},
                  {"do": "click", "el": el("link", "Ruházat"), "url": "https://www.vinted.hu/items/new",
                   "url_after": "https://www.vinted.hu/items/new/ruhazat"}]
        recording, reason = self.recorder([*detour, *self.CLEAN]).build(VALUES)
        self.assertIsNotNone(recording, reason)
        self.assertEqual(recording["steps"], clean["steps"], "az elhagyott ág kimaradt")
        # Közzététel után visszalépni már nem tiszta.
        publish = self.CLEAN[-1]
        recording, reason = self.recorder([*self.CLEAN, {"do": "back"}, publish]).build(VALUES)
        self.assertIsNone(recording)
        self.assertIn("visszalépett", reason)

    def test_an_opener_opened_and_closed_is_dropped(self):
        clean, _ = self.recorder(self.CLEAN).build(VALUES)
        toggled = [*self.CLEAN[:1], {"do": "click", "el": el("button", "Állapot")},
                   {"do": "click", "el": el("button", "Állapot")}, *self.CLEAN[1:]]
        recording, reason = self.recorder(toggled).build(VALUES)
        self.assertIsNotNone(recording, reason)
        self.assertEqual(recording["steps"], clean["steps"])
        escaped = [*self.CLEAN[:1], {"do": "click", "el": el("button", "Méret")}, {"do": "press", "key": "Escape"},
                   *self.CLEAN[1:]]
        self.assertEqual(self.recorder(escaped).build(VALUES)[0]["steps"], clean["steps"])
        # A browser_choose a választás után Esc-cel zárja be a nyitva maradt listát: az nem változtat a recepten.
        closed = [*self.CLEAN[:3], {"do": "press", "key": "Escape"}, *self.CLEAN[3:]]
        self.assertEqual(self.recorder(closed).build(VALUES)[0], {**clean, "recorded_at": mock.ANY})

    def test_the_step_limit_counts_after_cleaning(self):
        retyped = [{"do": "type", "el": el("textarea", "Leírás", "description"), "text": f"vázlat {n}"}
                   for n in range(150)]
        retyped.append({"do": "type", "el": el("textarea", "Leírás", "description"), "text": "Egyszer hordtam."})
        recording, reason = self.recorder([*retyped, *self.CLEAN]).build(VALUES)
        self.assertIsNotNone(recording, reason)
        clicks = [{"do": "click", "el": el("button", f"Gomb {n}")} for n in range(130)]
        recording, reason = self.recorder([*clicks, *self.CLEAN]).build(VALUES)
        self.assertIsNone(recording)
        self.assertIn("túl sok lépés", reason)

    def test_native_select_is_a_choice(self):
        rec = self.recorder([
            {"do": "type", "el": {**el("select", "Állapot", "state"), "select": True}, "text": "Használt"},
            {"do": "click", "el": el("button", "Feltöltés"), "risky": True, "url": "a", "url_after": "a"},
        ])
        recording, reason = rec.build(VALUES)
        self.assertIsNotNone(recording, reason)
        self.assertEqual(recording["steps"][0], {"do": "choice", "field": "condition", "opener": None})
        self.assertEqual(recording["memory"]["condition"]["nagyon jo"][0]["text"], "Használt",
                         "a mi értékünkhöz (Nagyon jó) az oldalon a „Használt” tartozik")
        self.assertEqual(recording["success"], {}, "ha a cím nem változik, utólag ellenőrizni kell")

    def test_plan_needs_only_the_item_data(self):
        rec = self.recorder([
            {"do": "upload", "el": el("input:file", "Fotók")},
            {"do": "type", "el": el("input:text", "Cím", "title"), "text": VALUES["title"]},
            {"do": "click", "el": el("button", "Kategória")},
            {"do": "click", "el": el("option", "Pólók")},
            {"do": "click", "el": el("button", "Feltöltés"), "risky": True, "url": "a", "url_after": "b"},
        ])
        recording, _ = rec.build(VALUES)
        # Egy még nem látott választás (más kategória, új állapot) nem akadály: menet közben választja ki.
        replayer = Replayer(None, recording, {**VALUES, "category": "Nők > Ruhák > Nadrágok"}, ["1.jpg"])
        self.assertEqual(replayer.plan_problem(), "")
        self.assertEqual(Replayer(None, recording, VALUES, ["1.jpg"]).plan_problem(), "")
        self.assertIn("Cím", Replayer(None, recording, {**VALUES, "title": ""}, ["1.jpg"]).plan_problem())
        self.assertEqual(Replayer(None, recording, VALUES, []).plan_problem(), "nincs fotó")

    # A hibacsomagból (Vinted, angol felület): kategória, „EU 40” méret, „Very good …” állapot, és utána az ár.
    JEANS = {**VALUES, "title": "Hosszú farmer 40-es méret", "description": "40-es méretű hosszú farmer.",
             "price": "4000", "brand": "NoName", "size": "40", "color": "", "category": "Nők > Ruhák > Farmerek",
             "product": "hosszú farmer"}

    def vinted_jeans(self, *ending):
        return self.recorder([
            {"do": "upload", "el": None, "count": 1},
            {"do": "type", "el": el("input:text", "Title", "title title"), "text": "Hosszú farmer 40-es méret"},
            {"do": "type", "el": el("textarea", "Description", "description description"),
             "text": "40-es méretű hosszú farmer."},
            {"do": "click", "el": el("input:text", "Category", "category category")},
            {"do": "click", "el": el("button", "Women", "catalog-1904")},
            {"do": "click", "el": el("button", "Clothing", "catalog-4")},
            {"do": "click", "el": el("button", "Jeans", "catalog-183")},
            {"do": "click", "el": el("radio", "Straight jeans", "catalog-1845")},
            {"do": "click", "el": el("button", "EU")},
            {"do": "click", "el": el("checkbox", "EU 40")},
            {"do": "click", "el": el("input:text", "Condition", "condition condition")},
            {"do": "click", "el": el("radio", "Very good A lightly used item that may have slight imperfections",
                                     "condition-2")},
            {"do": "type", "el": el("input:text", "Price", "price price"), "text": "4000"},
            {"do": "click", "el": el("button", "Small For items that’d fit in a large envelope.", "package-size-1")},
            *ending,
        ])

    def test_vinted_size_and_price_are_not_baked_into_other_choices(self):
        rec = self.vinted_jeans({"do": "click", "el": el("button", "Upload"), "risky": True,
                                 "url": "https://www.vinted.hu/items/new",
                                 "url_after": "https://www.vinted.hu/member/240394916?promo_shown=true"})
        recording, reason = rec.build(self.JEANS)
        self.assertIsNotNone(recording, reason)
        memory = recording["memory"]
        category = [a["el"]["label"] for a in memory["category"][replay.vkey(self.JEANS["category"])]]
        self.assertNotIn("EU 40", category, "a méret nem a kategória része (különben minden farmer 40-es lenne)")
        self.assertEqual([a["el"]["label"] for a in memory["size"]["40"]], ["EU 40"])
        self.assertIn("price", [s.get("value") for s in recording["steps"] if s["do"] == "type"],
                      "az ár a tárgyé, nem az állapot választásáé")
        self.assertNotIn("4000", str(memory), "sehol nincs beégetett ár")
        self.assertIn("nagyon jo", memory["condition"])
        self.assertEqual(recording["success"], {"url": "www.vinted.hu/member/#"})
        self.assertNotIn("*", memory["size"], "más méretnél nem kattint vakon az „EU 40”-re: azt menet közben "
                                              "választja ki (és megtanulja)")
        self.assertEqual(Replayer(None, recording, {**self.JEANS, "price": "3500"}, ["1.jpg"]).plan_problem(), "")

    def test_a_publish_that_needed_fixing_is_not_learned(self):
        # Az első Upload után a Vinted: „Fill in brand / colour to continue”; a modell pótolta, és újra közzétette.
        rec = self.vinted_jeans(
            {"do": "click", "el": el("button", "Upload"), "risky": True, "url": "https://www.vinted.hu/items/new",
             "url_after": "https://www.vinted.hu/items/new"},
            {"do": "type", "el": el("input:text", "Search brands", "brand-search-input"), "text": "NoName"},
            {"do": "click", "el": el("input:text", "Colours", "color color")},
            {"do": "click", "el": el("button", "Upload"), "risky": True, "url": "https://www.vinted.hu/items/new",
             "url_after": "https://www.vinted.hu/member/240394916?promo_shown=true"})
        recording, reason = rec.build(self.JEANS)
        self.assertIsNone(recording)
        self.assertIn("elsőre nem ment át", reason)

    def test_two_step_publishing_and_delisting_are_still_learned(self):
        confirm = [{"do": "click", "el": el("button", "Feltöltés"), "risky": True, "url": "a", "url_after": "a"},
                   {"do": "click", "el": el("button", "Megerősítés"), "risky": True, "url": "a", "url_after": "b"}]
        recording, reason = self.recorder(confirm).build(VALUES)
        self.assertIsNotNone(recording, "közzététel, majd rögtön a megerősítése: rendben van")
        delist = [{"do": "click", "el": el("button", "Törlés"), "risky": True, "url": "a", "url_after": "a"},
                  {"do": "click", "el": el("input:radio", "Máshol adtam el")},
                  {"do": "click", "el": el("button", "Igen, törlöm"), "risky": True, "url": "a", "url_after": "b"}]
        recording, reason = self.recorder(delist, task="delist").build(VALUES)
        self.assertIsNotNone(recording, reason)

    def test_the_success_page_heading_is_remembered(self):
        publish = {"do": "click", "el": el("button", "Feltöltés"), "risky": True,
                   "url": "https://www.vinted.hu/items/new", "url_after": "https://www.vinted.hu/items/new"}
        rec = self.recorder([{"do": "type", "el": el("input:text", "Cím", "title"), "text": VALUES["title"]}, publish])
        # A modell a megjelöléskor már a kész hirdetés oldalát látta (az oldal csak később vitt tovább).
        rec.landing = {"url": "https://www.vinted.hu/items/77-zara", "title": "Vinted",
                       "heading": "Zara fekete póló, L – feltöltve, 2 fotó"}
        recording, reason = rec.build(VALUES)
        self.assertIsNotNone(recording, reason)
        self.assertEqual(recording["success"], {"url": "www.vinted.hu/items/#", "heading": "feltoltve # foto"},
                         "a tárgy saját szavai és a számok nélkül: minden tárgynál ugyanaz")

    def test_helpers(self):
        self.assertEqual(field_for("Csomagméret"), "parcel_size", "nem a „méret”")
        self.assertEqual(field_for("package-size-1"), "parcel_size", "a Vinted mezőneve: ez sem a „méret”")
        self.assertEqual(field_for("brand-search-input"), "brand")
        self.assertEqual(field_for("Válassz kategóriát"), "category")
        self.assertEqual(match_field("Zara fekete póló L", VALUES), "title", "kicsit átfogalmazva is")
        self.assertEqual(match_field("500", VALUES), "price")
        self.assertEqual(match_field("Teljesen más", VALUES), "")
        self.assertEqual(url_shape("https://www.vinted.hu/items/123-zara-polo?ref=1"), "www.vinted.hu/items/#")


class FakePage:
    """A közzététel utáni várakozáshoz: a webcím, a címsor és a hibaüzenetek a kérdezések számától függnek."""

    def __init__(self, urls, headings=("Új hirdetés",), errors=((),)):
        self.urls, self.headings, self.errors = list(urls), list(headings), list(errors)
        self.looks = 0

    def _at(self, values):
        return values[min(self.looks, len(values) - 1)]

    @property
    def url(self):
        self.looks += 1
        return self._at(self.urls)

    def headline(self):
        return "Teszt", self._at(self.headings)

    def form_errors(self):
        return list(self._at(self.errors))


class AwaitPublishTests(unittest.TestCase):
    FORM_URL = "https://www.jofogas.hu/uj-hirdetes"

    def wait(self, page, success=None, timeout=3.0):
        with mock.patch.object(replay.time, "sleep"):
            return replay.await_publish(page, self.FORM_URL, set(), success, timeout=timeout,
                                        before_heading="uj hirdetes")

    def test_a_late_redirect_is_waited_for(self):
        page = FakePage([self.FORM_URL] * 4 + ["https://www.jofogas.hu/hirdetes/123"])
        status, _, url = self.wait(page, {"url": "www.jofogas.hu/hirdetes/#"})
        self.assertEqual((status, url), ("done", "https://www.jofogas.hu/hirdetes/123"))

    def test_a_success_message_in_place_counts(self):
        page = FakePage([self.FORM_URL], headings=["Új hirdetés", "Új hirdetés", "Hirdetésed elbírálás alatt"])
        self.assertEqual(self.wait(page)[0], "done")
        stays = FakePage([self.FORM_URL], headings=["Új hirdetés"])
        self.assertEqual(self.wait(stays, timeout=0.0)[0], "unknown", "ugyanaz az űrlap: nem tudni")
        failed = FakePage([self.FORM_URL], headings=["Nem sikerült feltölteni"])
        self.assertEqual(self.wait(failed, timeout=0.0)[0], "unknown")

    def test_a_login_page_is_not_a_success(self):
        page = FakePage(["https://www.jofogas.hu/belepes?next=/uj-hirdetes"])
        self.assertEqual(self.wait(page, timeout=0.0)[0], "unknown")

    def test_a_lasting_form_error_is_a_failure(self):
        page = FakePage([self.FORM_URL], errors=[(), ("Kötelező mező: Ár",)])
        with mock.patch.object(replay.time, "monotonic", side_effect=[0.0] + [2.0] * 20):
            status, reason, _ = self.wait(page)
        self.assertEqual(status, "error")
        self.assertIn("Kötelező mező: Ár", reason)


class StubBrowser:
    """A visszajátszás egységtesztjeihez: egy „oldal” elemlistával; a kattintást és a gépelést csak feljegyzi.
    `goes`: melyik feliratra kattintva hová visz (pl. a közzététel a kész hirdetéshez)."""

    def __init__(self, elements, goes=None, redirects=None, heading=""):
        self.elements = [dict(e, id=5000 + n) for n, e in enumerate(elements)]
        self.goes = goes or {}
        self.redirects = redirects or {}
        self.heading = heading
        self.url = "https://www.jofogas.hu/uj-hirdetes"
        self.auto = False
        self.done: list[tuple] = []

    def _label(self, element):
        return next(e["label"] for e in self.elements if e["id"] == element)

    def open(self, url):
        self.done.append(("open", url))
        self.url = self.redirects.get(url, url)
        return ""

    def locate(self, want, timeout=0.0):
        return next((e for e in self.elements if (e["kind"], e["label"]) == (want.get("kind"), want.get("label"))),
                    None)

    def scan(self):
        return [dict(e) for e in self.elements]

    def click(self, element):
        self.done.append(("click", self._label(element)))
        self.url = self.goes.get(self._label(element), self.url)
        return "Rákattintottam."

    def type_text(self, element, text, submit=False):
        self.done.append(("type", self._label(element), text))
        return "Beírtam."

    def upload_files(self, files, element=None):
        return "Feltöltöttem."

    def form_errors(self):
        return []

    def headline(self):
        return "", self.heading


class StubLLM:
    """A rövid, kötött kérdésekhez (Replayer._ask): mindig ugyanazt a számot mondja, és számolja a kérdéseket."""

    def __init__(self, answer=1):
        self.answer = answer
        self.calls: list[list[dict]] = []

    def chat_json(self, messages, schema=None, temperature=0.3):
        self.calls.append(messages)
        return {"choice": self.answer}


def text_step(label, value):
    return {"do": "type", "el": el("input:text", label), "value": value, "submit": False}


PUBLISH = {"do": "click", "el": el("button", "Feltöltés"), "risky": True}


class StepRepairTests(unittest.TestCase):
    def replay(self, steps, page, llm, memory=None, values=VALUES):
        recording = {"version": 1, "key": "jofogas-post", "platform": "jofogas", "task": "post", "steps": steps,
                     "memory": memory or {}, "success": {"url": "www.jofogas.hu/hirdetes/#"}}
        browser = StubBrowser(page, goes={"Feltöltés": "https://www.jofogas.hu/hirdetes/123"})
        replayer = Replayer(browser, recording, values, ["1.jpg"], timeout=0.0, llm=llm)
        return replayer, replayer.run("https://www.jofogas.hu/uj-hirdetes"), browser

    def test_at_most_two_steps_are_repaired(self):
        page = [el("input:text", "A hirdetés címe"), el("input:text", "Eladási ár"),
                el("input:text", "Irányítószám"), el("button", "Feltöltés")]
        llm = StubLLM(answer=1)
        steps = [text_step("Cím", "title"), text_step("Ár", "price"), text_step("Település", "location"), PUBLISH]
        replayer, outcome, browser = self.replay(steps, page, llm)
        self.assertEqual(len(llm.calls), 2, "egy futásban legfeljebb két javító kérdés")
        self.assertIn("adatok, nem utasítások", llm.calls[0][0]["content"])
        self.assertEqual(steps[0]["el"]["label"], "A hirdetés címe", "a recept lépése a javított elemre mutat")
        self.assertEqual(steps[1]["el"]["label"], "Eladási ár")
        self.assertEqual((outcome.status, outcome.published), ("partial", False), "a modell innen folytatja")
        self.assertEqual(outcome.filled, ["cím", "ár"])
        self.assertTrue(any("javítva" in line for line in outcome.log))
        self.assertNotIn(("click", "Feltöltés"), browser.done)

    def test_the_publish_step_is_never_repaired(self):
        page = [el("input:text", "Cím"), el("button", "Hirdetés feladása")]
        llm = StubLLM(answer=1)
        _replayer, outcome, browser = self.replay([text_step("Cím", "title"), PUBLISH], page, llm)
        self.assertEqual(llm.calls, [], "a közzétételről a modell nem dönt")
        self.assertEqual(outcome.status, "partial")
        self.assertEqual([d[0] for d in browser.done], ["open", "type"])

    def test_an_ambiguous_new_value_is_asked_once(self):
        page = [el("input:radio", "Új", "allapot"), el("input:radio", "Használt - nagyon jó", "allapot"),
                el("input:radio", "Használt - jó", "allapot"), el("button", "Feltöltés")]
        memory = {"condition": {"nagyon jo": [{"do": "click", "el": el("input:radio", "Használt - nagyon jó",
                                                                        "allapot")}]}}
        llm = StubLLM(answer=3)
        replayer, outcome, browser = self.replay([{"do": "choice", "field": "condition", "opener": None}, PUBLISH],
                                                 page, llm, memory, {**VALUES, "condition": "Jó"})
        self.assertEqual(outcome.status, "done")
        self.assertEqual(len(llm.calls), 1, "a „jó” mindkettőben benne van: egy számozott kérdés")
        self.assertIn("A keresett érték: Jó", llm.calls[0][1]["content"])
        self.assertEqual(browser.done[1], ("click", "Használt - jó"))
        self.assertEqual(replayer.recording["memory"]["condition"]["jo"][0]["el"]["label"], "Használt - jó")
        self.assertTrue(replayer.changed)
        self.assertIn("Állapot: Jó (az oldalon: Használt - jó)", replayer.shown)

    def test_the_form_entry_skips_the_way_there(self):
        home, form_url = "https://www.jofogas.hu/", "https://www.jofogas.hu/uj-hirdetes"
        page = [el("link", "Hirdetésfeladás"), el("button", "Elfogadom"), el("input:text", "Cím"),
                el("button", "Feltöltés")]
        steps = [{"do": "click", "el": el("link", "Hirdetésfeladás"), "optional": False},
                 {"do": "click", "el": el("button", "Elfogadom"), "optional": True}, text_step("Cím", "title"), PUBLISH]
        recording = {"version": 1, "key": "jofogas-post", "platform": "jofogas", "task": "post", "steps": steps,
                     "memory": {}, "success": {}, "entry": {"url": form_url, "step": 2}}
        browser = StubBrowser(page, goes={"Feltöltés": "https://www.jofogas.hu/hirdetes/123"})
        outcome = Replayer(browser, recording, VALUES, [], timeout=0.0).run(home)
        self.assertEqual(outcome.status, "done")
        self.assertEqual(browser.done[0], ("open", form_url), "közvetlenül az űrlap")
        self.assertNotIn(("click", "Hirdetésfeladás"), browser.done)
        self.assertIn(("click", "Elfogadom"), browser.done, "a süti-ablak (ha van) marad")
        # Ha az űrlap címe a főoldalra (vagy a belépéshez) visz, minden lépéssel a kezdőoldalról indul.
        for target in (home, "https://www.jofogas.hu/belepes?vissza=/uj-hirdetes"):
            browser = StubBrowser(page, goes={"Feltöltés": "https://www.jofogas.hu/hirdetes/123"},
                                  redirects={form_url: target})
            replayer = Replayer(browser, recording, VALUES, [], timeout=0.0)
            self.assertEqual(replayer.run(home).status, "done")
            self.assertEqual(browser.done[:3], [("open", form_url), ("open", home), ("click", "Hirdetésfeladás")])
            self.assertIn("a megjegyzett űrlapcím nem jó", replayer.notes[0])

    def test_entry_helpers(self):
        domains = ("jofogas.hu",)
        self.assertEqual(replay.entry_for("https://www.jofogas.hu/uj-hirdetes?session=abc#top", domains),
                         "https://www.jofogas.hu/uj-hirdetes")
        self.assertEqual(replay.entry_for("https://evil.example/uj-hirdetes", domains), "", "csak az oldal címein")
        self.assertEqual(replay.entry_for("https://www.jofogas.hu/hirdetes/123/szerkesztes", domains), "",
                         "egy tárgy oldala nem belépési pont")
        page = StubBrowser([], heading="Az oldal nem található")
        page.url = "https://www.jofogas.hu/uj-hirdetes"
        self.assertEqual(replay.entry_problem(page, "https://www.jofogas.hu/uj-hirdetes"), "gone")
        page.heading = "Új hirdetés feladása"
        self.assertEqual(replay.entry_problem(page, "https://www.jofogas.hu/uj-hirdetes"), "")
        page.url = "https://www.jofogas.hu/belepes"
        self.assertEqual(replay.entry_problem(page, "https://www.jofogas.hu/uj-hirdetes"), "login")
        # A felvételből: az első oldal, ahol mezőt töltött ki (a főoldal és a link utáni süti-ablak még nem).
        rec = FlowRecorder("jofogas", "post", "jofogas-post-x", "https://www.jofogas.hu/", domains)
        for event in ({"do": "goto", "url": "https://www.jofogas.hu/", "final": "https://www.jofogas.hu/"},
                      {"do": "type", "el": el("input:search", "Keresés"), "text": "póló", "search": True},
                      {"do": "click", "el": el("link", "Hirdetésfeladás"), "url": "https://www.jofogas.hu/",
                       "url_after": "https://www.jofogas.hu/uj-hirdetes?honnan=menu"},
                      {"do": "click", "el": el("button", "Elfogadom"), "url": "https://www.jofogas.hu/uj-hirdetes"},
                      {"do": "type", "el": el("input:text", "Cím", "title"), "text": VALUES["title"]}):
            rec.add(event)
        self.assertEqual(rec.entry_url(), "https://www.jofogas.hu/uj-hirdetes")

    def test_new_values_are_picked_like_browser_choose_would(self):
        # A browser_choose a kattintható (onclick-es) li, div, span lehetőségeket is elfogadja, és a lista
        # vezérlőgombjait (Keresés, Mentés…) kihagyja; a visszajátszás egy új értéknél ugyanígy választ.
        page = [el("li", "Új"), el("li", "Nagyon jó"), el("li", "Jó"), el("li", "Keresés"), el("button", "Feltöltés")]
        for value, picked in (("Jó", "Jó"), ("Nagyon jó állapotú", "Nagyon jó")):
            memory = {"condition": {"uj": [{"do": "click", "el": el("li", "Új")}]}}
            llm = StubLLM(answer=1)
            replayer, outcome, browser = self.replay([{"do": "choice", "field": "condition", "opener": None}, PUBLISH],
                                                     page, llm, memory, {**VALUES, "condition": value})
            self.assertEqual(outcome.status, "done", value)
            self.assertEqual(browser.done[1], ("click", picked), value)
            self.assertEqual(llm.calls, [], "a felirat alapján, modell nélkül (mint a browser_choose)")
        self.assertTrue(replay.is_choice_option(el("div", "Piros")))
        for control in (el("li", "Keresés"), el("button", "Mentés"), el("span", "Vissza a kategóriákhoz")):
            self.assertFalse(replay.is_choice_option(control), control["label"])
        twice = [el("li", "Jó"), el("input:radio", "Jó"), el("li", "Új")]
        self.assertEqual([o["kind"] for o in distinct_options(twice)], ["input:radio", "li"],
                         "egy felirat egyszer, a pontosabb elemmel")

    def test_pick_label(self):
        labels = ["Új", "Nagyon jó állapotú", "Jó állapotú", "Elfogadható"]
        self.assertEqual(replay.pick_label(labels, "Új"), 0, "pontosan egyező")
        self.assertEqual(replay.pick_label(labels, "jó"), 2, "az egyetlen, amelyik vele kezdődik")
        self.assertEqual(replay.pick_label(["Használt - kiváló", "Új"], "Kiváló"), 0, "egyetlen, amiben benne van")
        self.assertIsNone(replay.pick_label(["Használt - nagyon jó", "Használt - jó"], "Jó"), "nem egyértelmű")
        self.assertIsNone(replay.pick_label(labels, "Felújított"))


# --- a teljes kör: tanulás a modellel, aztán visszajátszás modell nélkül -------------------------------

class ReplayHarness:
    """Jarvis egy helyi „hirdetési oldallal” (Vinted helyett) és hamis modellel; a hibacsomag és az
    emberi tempó tesztjei is ezt használják."""

    def setUp(self):
        self.site = Site()
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        cfg.browser.headless = True
        cfg.web.allow_local = True
        cfg.assistant.show_timings = False
        cfg.assistant.fillers = False
        cfg.work.platforms = ["vinted"]
        cfg.work.research_sites = []
        platform = Platform("vinted", "Vinted", self.site.url("fooldal.html"), self.site.url("urlap.html"), "",
                            ("127.0.0.1",), "Vinted: tippek a teszthez.")
        self.patches = [mock.patch.dict(selling.PLATFORMS, {"vinted": platform}, clear=True),
                        mock.patch.object(replay, "DEFAULT_TIMEOUT", 1.5)]
        for patch in self.patches:
            patch.start()
        self.ui = RecordingUI()
        self.ui.confirm_answer = True
        self.app = JarvisApp(cfg, self.ui, use_voice=False, use_speech=False, use_wake_word=False)
        self.fake.vram_fit = lambda ctx: 1.0
        self.assertTrue(self.app.start())
        self.work = self.app.work
        self.store = self.work.store

    def tearDown(self):
        self.app.toolbox.close()
        for patch in reversed(self.patches):
            patch.stop()
        self.fake.close()
        self.site.close()
        self.tmp.cleanup()

    def item(self, **values):
        item = self.store.create(targets=["vinted"], prompt="teszt")
        self.store.add_photo(item.id, jpeg())
        return self.store.update(item.id, **{**SHIRT, **values})

    def confirms(self) -> list[str]:
        return [e[1] for e in self.ui.of("confirm")]

    def learn_posting(self, item, *before):
        """A modell egyszer végigcsinálja a feltöltést (`before`: a lépései az űrlap előtt, pl. a főoldalról)."""
        self.fake.chat_script = [
            tool_reply("item_post", {"item": item.id, "platform": "vinted"}),
            *before,
            click("Elfogadom"),
            tool_reply("browser_upload_photos", {}),
            lambda body: (200, tool_reply("browser_fill", {"fields": [
                {"element": element_number(last_tool(body), "Cím"), "text": item.title},
                {"element": element_number(last_tool(body), "Leírás"), "text": item.description},
                {"element": element_number(last_tool(body), "Ár"), "text": str(item.price)}]})),
            click("Kategória"), click("Pólók"), click("Nagyon jó"),
            lambda body: (200, tool_reply("browser_type", {"element": element_number(last_tool(body), "Márka"),
                                                          "text": "Zara"})),
            click("Zara"),
            click("Feltöltés"),
            lambda body: (200, tool_reply("item_mark", {"item": item.id, "status": "feltöltve", "platform": "vinted",
                                                        "url": self.site.url("kesz.html")})),
            reply("Fent van a Vinteden."),
        ]
        self.app.run_work({"action": "post", "ids": [item.id], "platform": "vinted"})
        self.assertEqual(self.fake.chat_script, [], "minden modell-lépés lefutott")


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class ReplayFlowTests(ReplayHarness, unittest.TestCase):
    def test_learns_once_then_posts_without_the_model(self):
        first = self.item()
        self.learn_posting(first)
        self.assertEqual(self.store.get(first.id).platforms["vinted"]["status"], POSTED)
        key = "vinted-post-ruha-nok-ruhak-polok"
        self.assertIn(key, self.work.flows.keys(), "a sikeres feltöltés lépéseit megjegyezte")
        self.assertTrue(any("Megjegyeztem a lépéseket" in e[4] for e in self.ui.of("tool_done")),
                        "a megjelöléskor szól, hogy megjegyezte")

        second = self.item(title="H&M fehér póló, M", description="Szinte új.", price=700, brand="H&M",
                           condition="Jó", size="M", color="fehér")
        calls_before = len(self.fake.chat_bodies())
        self.app.run_work({"action": "post", "ids": [second.id], "platform": "vinted"})
        self.assertEqual(len(self.fake.chat_bodies()), calls_before, "a második feltöltés modell nélkül ment")
        state = self.store.get(second.id).platforms["vinted"]
        self.assertEqual(state["status"], POSTED)
        sent = parse_qs(urlparse(self.app.toolbox.browser.url).query)
        self.assertEqual({k: v[0] for k, v in sent.items()},
                         {"title": "H&M fehér póló, M", "desc": "Szinte új.", "price": "700", "cat": "Pólók",
                          "cond": "Jó", "brand": "H&M", "photos": "1"})
        self.assertTrue(state["url"].startswith(self.site.url("kesz.html")), "a sikeres oldal címe")
        prompt = self.confirms()[-1]
        self.assertIn("Begyakorolt lépésekkel töltöttem ki.", prompt)
        for expected in ("Kategória: Nők > Ruhák > Pólók", "Állapot: Jó", "Márka: H&M"):
            self.assertIn(expected, prompt, "a jóváhagyás előtt látod, mit választott")
        self.assertEqual(self.work.flows.get(key)["uses"], 1)

    def test_a_slow_redirect_after_publishing_is_still_a_success(self):
        first = self.item()
        self.learn_posting(first)
        # Az oldal most másfél másodperc múlva visz tovább a kész oldalra (eddig a kattintás után rögtön ítélt).
        self.site.write("urlap.html", form().replace(
            "location.href = '/kesz.html?' + q;", "setTimeout(() => { location.href = '/kesz.html?' + q; }, 1500);"))
        second = self.item(title="H&M fehér póló, M", price=700, brand="H&M")
        calls_before = len(self.fake.chat_bodies())
        self.app.run_work({"action": "post", "ids": [second.id], "platform": "vinted"})
        self.assertEqual(len(self.fake.chat_bodies()), calls_before, "nem kellett a modellel ellenőriztetni")
        state = self.store.get(second.id).platforms["vinted"]
        self.assertEqual(state["status"], POSTED)
        self.assertIn("kesz.html", state["url"])
        self.assertEqual(self.work.flows.get("vinted-post-ruha-nok-ruhak-polok")["success"],
                         {"url": "127.0.0.1/kesz.html", "heading": "hirdetesed feltoltve"})

    def test_a_form_error_after_publishing_is_reported_once(self):
        first = self.item()
        self.learn_posting(first)
        self.site.write("urlap.html", form().replace(
            "location.href = '/kesz.html?' + q;",
            "const a = document.createElement('div'); a.setAttribute('role', 'alert'); "
            "a.textContent = 'Kötelező mező: Telefonszám'; document.body.appendChild(a);"))
        second = self.item(title="H&M fehér póló, M", price=700, brand="H&M")
        confirms = len(self.confirms())
        outcome = self.work.replay(second.id, "vinted", "post")
        self.assertEqual(outcome.status, "uncertain")
        self.assertIn("Kötelező mező: Telefonszám", outcome.reason)
        self.assertEqual(len(self.confirms()), confirms + 1, "egyszer tette közzé, újra nem kattintott")

    def test_a_changed_page_is_handed_over_on_the_open_form(self):
        first = self.item()
        self.learn_posting(first)
        # Az oldal megváltozott: a cím mező felirata és neve is más, és a modell sem ismeri fel (0 = egyik sem).
        self.site.write("urlap.html", form("Hirdetés címe", "cim"))
        second = self.item(title="H&M fehér póló, M", price=700, brand="H&M")
        calls_before = len(self.fake.chat_bodies())
        self.fake.chat_script = [reply('{"choice": 0}'), reply("Most nem sikerült, mert hiányzik egy mező.")]
        self.app.run_work({"action": "post", "ids": [second.id], "platform": "vinted"})
        self.assertEqual(self.fake.chat_script, [], "egy rövid kérdés, aztán a modell vette át")
        repair, handover = self.fake.chat_bodies()[calls_before:]
        self.assertIn("choice", str(repair.get("format")), "kötött (számos) válasz")
        self.assertNotIn("tools", repair)
        self.assertIn("Hirdetés címe", repair["messages"][-1]["content"], "a hasonló elemek között ott van")
        request = next(m["content"] for m in handover["messages"] if m["role"] == "user")
        self.assertIn("Elkezdtem kitölteni", request, "a nyitott űrlapon folytatja, nem kezdi elölről")
        self.assertIn("fotók (1 db)", request)
        self.assertIn("item_post eszközt ne használd", request)
        infos = " ".join(e[1] for e in self.ui.of("info"))
        self.assertIn("a hiányzót a modell pótolja", infos)
        self.assertEqual(self.work.flows.get("vinted-post-ruha-nok-ruhak-polok")["fails"], 1)
        self.assertEqual(self.store.get(second.id).platforms["vinted"]["status"], "hiba")
        self.assertFalse([c for c in self.confirms() if "Begyakorolt" in c], "közzététel előtt állt meg")

    def test_a_renamed_button_is_repaired_with_one_question(self):
        first = self.item()
        self.learn_posting(first)
        key = "vinted-post-ruha-nok-ruhak-polok"
        # A gomb felirata és azonosítója is más lett: a megjegyzett leírás alapján már nincs meg.
        self.site.write("urlap.html", form().replace("catbtn", "valaszto")
                        .replace(">Kategória</button>", ">Válassz kategóriát</button>"))
        second = self.item(title="H&M fehér póló, M", price=700, brand="H&M")
        calls_before = len(self.fake.chat_bodies())
        self.fake.chat_script = [lambda body: (200, reply(f'{{"choice": {numbered(body, "Válassz kategóriát")}}}'))]
        self.app.run_work({"action": "post", "ids": [second.id], "platform": "vinted"})
        self.assertEqual(self.fake.chat_script, [])
        self.assertEqual(len(self.fake.chat_bodies()), calls_before + 1, "egyetlen rövid kérdés, ágens nélkül")
        self.assertNotIn("tools", self.fake.chat_bodies()[-1])
        self.assertEqual(self.store.get(second.id).platforms["vinted"]["status"], POSTED)
        self.assertEqual(parse_qs(urlparse(self.app.toolbox.browser.url).query)["cat"], ["Pólók"])
        opener = next(s for s in self.work.flows.get(key)["steps"] if s.get("field") == "category")["opener"]
        self.assertEqual(opener["label"], "Válassz kategóriát", "a javított lépés a receptben marad")
        self.assertEqual(self.work.flows.get(key)["uses"], 1)

    def test_an_unseen_condition_is_chosen_without_the_model(self):
        # Az oldal a saját szavaival írja az állapotot: a recept csak a „Nagyon jó”-hoz tudja, mire kattintson.
        self.site.write("urlap.html", form().replace("> Nagyon jó</label>", "> Nagyon jó állapotú</label>")
                        .replace("> Jó</label>", "> Jó állapotú</label>"))
        first = self.item()
        self.learn_posting(first)
        key = "vinted-post-ruha-nok-ruhak-polok"
        self.assertEqual(list(self.work.flows.get(key)["memory"]["condition"]), ["nagyon jo"])
        second = self.item(title="H&M fehér póló, M", price=700, brand="H&M", condition="Jó")
        self.assertTrue(self.work.can_replay(second.id, "vinted", "post"))
        calls_before = len(self.fake.chat_bodies())
        self.app.run_work({"action": "post", "ids": [second.id], "platform": "vinted"})
        self.assertEqual(len(self.fake.chat_bodies()), calls_before, "modell nélkül választotta ki")
        self.assertEqual(self.store.get(second.id).platforms["vinted"]["status"], POSTED)
        self.assertEqual(parse_qs(urlparse(self.app.toolbox.browser.url).query)["cond"], ["Jó állapotú"])
        self.assertIn("Állapot: Jó (az oldalon: Jó állapotú)", self.confirms()[-1], "a jóváhagyásnál látod")
        self.assertEqual(sorted(self.work.flows.get(key)["memory"]["condition"]), ["jo", "nagyon jo"],
                         "megjegyezte: legközelebb már keresés nélkül")

    def test_the_form_address_is_learned_and_opened_directly(self):
        # A hirdetésfeladás a főoldalon kezdődik (mint a Jófogáson): onnan egy link visz az űrlapra.
        self.site.write("fooldal.html", "<!doctype html><html><head><meta charset='utf-8'><title>Főoldal</title>"
                        "</head><body><h1>Teszt piactér</h1><a href='/urlap.html?honnan=fejlec'>Hirdetésfeladás</a>"
                        "</body></html>")
        self.work.platforms["vinted"] = dataclasses.replace(self.work.platforms["vinted"],
                                                            new_url=self.site.url("fooldal.html"))
        first = self.item()
        self.learn_posting(first, click("Hirdetésfeladás"))
        form_url = self.site.url("urlap.html")
        self.assertEqual(self.work.settings["entry_urls"], {"vinted": form_url}, "kérdőjeles rész nélkül")
        key = "vinted-post-ruha-nok-ruhak-polok"
        self.assertEqual(self.work.flows.get(key)["entry"], {"url": form_url, "step": 2},
                         "a „Hirdetésfeladás” és a süti-ablak után kezdődik az űrlap")

        # A visszajátszás közvetlenül az űrlapot nyitja meg, a főoldalt és a linket kihagyja.
        second = self.item(title="H&M fehér póló, M", price=700, brand="H&M")
        calls_before = len(self.fake.chat_bodies())
        self.app.run_work({"action": "post", "ids": [second.id], "platform": "vinted"})
        self.assertEqual(len(self.fake.chat_bodies()), calls_before)
        self.assertEqual(self.store.get(second.id).platforms["vinted"]["status"], POSTED)
        opened = [e["url"] for e in self.work.trail if e.get("do") == "goto"]
        self.assertEqual(opened, [form_url])
        self.assertNotIn("Hirdetésfeladás", str(self.work.trail))

        # A modell is ott kezd (pl. egy másik kategóriánál, amihez még nincs recept).
        third = self.item(title="Farmer, 38", category="Nők > Ruhák > Nadrágok", product="farmer")
        result = self.app.toolbox.call("item_post", {"item": third.id, "platform": "vinted"})
        self.assertIn("Közvetlenül a hirdetésfeladás oldalát nyitottam meg", result)
        self.assertIn('"Cím"', result, "az első oldalnézetben már az űrlap mezői vannak")
        self.work.end_request()

        # Ha a megjegyzett cím már nincs meg, a főoldalról indul, és elfelejti.
        self.site.write("regi.html", "<!doctype html><html><head><meta charset='utf-8'><title>404 – Az oldal nem "
                        "található</title></head><body><h1>Hoppá!</h1></body></html>")
        self.work.settings["entry_urls"] = {"vinted": self.site.url("regi.html")}
        fourth = self.item(title="Farmer, 40", category="Nők > Ruhák > Nadrágok", product="farmer")
        result = self.app.toolbox.call("item_post", {"item": fourth.id, "platform": "vinted"})
        self.assertNotIn("Közvetlenül", result)
        self.assertIn("Hirdetésfeladás", result, "a főoldal")
        self.assertEqual(self.work.settings["entry_urls"], {})
        self.work.end_request()

    def test_new_category_is_learned_by_the_model(self):
        first = self.item()
        self.learn_posting(first)
        other = self.item(category="Nők > Ruhák > Nadrágok", title="Farmer, 38", product="farmer")
        self.assertFalse(self.work.can_replay(other.id, "vinted", "post"), "más kategória: másik lépéssor")

    def test_delisting_is_learned_then_replayed_without_questions(self):
        first = self.item()
        self.store.mark(first.id, "vinted", POSTED, url=self.site.url("hirdetes.html?id=1"))
        self.fake.chat_script = [
            tool_reply("item_delist", {"item": first.id, "platform": "vinted"}),
            click("Törlés"), click("Igen, törlöm"),
            tool_reply("item_mark", {"item": first.id, "status": "nincs fent", "platform": "vinted"}),
            reply("Levettem."),
        ]
        self.app.run_work({"action": "delist", "ids": [first.id], "platforms": ["vinted"]})
        state = self.store.get(first.id).platforms["vinted"]
        self.assertEqual((state["status"], state.get("note")), (NOT_POSTED, "Levéve"))
        self.assertEqual(self.confirms(), [], "a levételt a gombbal már jóváhagytad, a törlésre nem kérdez")
        self.assertIn("vinted-delist", self.work.flows.keys())

        second = self.item(title="Kék pulóver", product="pulóver")
        self.store.mark(second.id, "vinted", POSTED, url=self.site.url("hirdetes.html?id=2"))
        calls_before = len(self.fake.chat_bodies())
        self.app.run_work({"action": "delist", "ids": [second.id], "platforms": ["vinted"]})
        self.assertEqual(len(self.fake.chat_bodies()), calls_before, "modell nélkül")
        self.assertEqual(self.store.get(second.id).platforms["vinted"]["status"], NOT_POSTED)
        self.assertIn("torolve.html", self.app.toolbox.browser.url)
        self.assertIn("Levettem: Vinted.", [e[1] for e in self.ui.of("notice")][-1])
        notices = self.app.notices.list()
        self.assertEqual(notices[0]["kind"], "delist")

        # A jóváhagyás csak a levétel idejére szólt: utána a törlés gombja megint kérdez.
        view = self.app.toolbox.call("browser_open", {"url": self.site.url("hirdetes.html?id=3")})
        self.app.toolbox.call("browser_click", {"element": element_number(view, "Törlés")})
        self.assertEqual(len(self.confirms()), 1)

    def test_delist_from_the_chat_asks_once(self):
        first = self.item()
        self.store.mark(first.id, "vinted", POSTED, url=self.site.url("hirdetes.html?id=1"))
        self.ui.confirm_answer = False
        result = self.app.toolbox.call("item_delist", {"item": first.id, "platform": "vinted"})
        self.assertIn("nem engedélyezte", result)
        self.assertIn("levenné", self.confirms()[-1])
        self.assertEqual(self.store.get(first.id).platforms["vinted"]["status"], POSTED)

    def test_sold_marks_and_delists_the_rest(self):
        item = self.item()
        self.store.mark(item.id, "vinted", POSTED, url=self.site.url("hirdetes.html?id=1"))
        self.app.run_work({"action": "sold", "ids": [item.id], "where": "vinted", "delist": True})
        item = self.store.get(item.id)
        self.assertTrue(item.sold)
        self.assertEqual(item.platforms["vinted"]["note"], "Itt kelt el.", "ahol elkelt, onnan nem veszi le")
        self.assertIn("megjelöltem, hogy elkelt", [e[1] for e in self.ui.of("notice")][-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
