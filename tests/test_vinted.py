"""A Vinted-kitöltő tesztjei: a Jarvis maga tölti ki a Vinted hirdetésfeladását, a modell csak a kategóriafában
(és ha a méret nem egyértelmű) választ egy számot. A levevőé is: a hirdetés oldalán a Delete, a megerősítés, és
hogy tényleg eltűnt-e, modell nélkül.

A böngészős tesztek egy helyi oldalon futnak, ami a Vinted űrlapját utánozza, azokkal az elemekkel, ahogy egy
valódi feltöltés hibacsomagjában látszottak (angol felület): Title, Describe your item, Category (catalog-1904 →
catalog-4 → catalog-183 → Straight jeans választógomb), Search brands, Size (EU → EU 40), Condition (condition-2:
„Very good A lightly used item…”), Colours (Done gombbal), Price, package-size-1, Upload → /member/….
Ha a Playwright vagy a Chromium nincs telepítve, ezek kimaradnak. Internet és valódi modell nem kell.

Futtatás:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import functools
import json
import re
import sys
import tempfile
import threading
import unittest
from collections import Counter
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
from test_work import SHIRT, _QuietHandler, jpeg  # noqa: E402

from jarvis_core import selling, vinted  # noqa: E402
from jarvis_core.app import JarvisApp  # noqa: E402
from jarvis_core.selling import FAILED, POSTED, Platform  # noqa: E402
from jarvis_core.vinted import (ChoiceMemory, VintedFiller, brand_option, category_option,  # noqa: E402
                                category_words, choice_key, color_option, color_values, condition_option,
                                confirm_button, consent_button, custom_brand_option, delete_button, elem_key,
                                find_field, item_slug, listing_link, logged_out, login_needed, new_elements,
                                parcel_option, profile_url, publish_button, seller_profile, size_options)

# --- a Vinted űrlapját utánzó oldal -----------------------------------------------------------------------

FORM = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Sell an item | Vinted</title>
<style>.hidden { display: none; }</style></head><body>
<header><nav><a href="/catalog/1904-women">Women</a> <a href="/catalog/5-men">Men</a>
  <input name="search_text" aria-label="Search for items"></nav></header>
<div id="onetrust-banner-sdk"><p>We use cookies to improve your experience.</p>
  <button type="button" id="onetrust-accept-btn-handler"
          onclick="document.getElementById('onetrust-banner-sdk').remove()">Accept all</button></div>
<main>
<h1>Sell an item</h1>
<button type="button" onclick="document.getElementById('photos').click()">Upload photos</button>
<input type="file" id="photos" accept="image/*" multiple style="display:none">
<ul id="thumbs"></ul>
<label for="title">Title</label> <input id="title" name="title">
<label for="description">Describe your item</label> <textarea id="description" name="description"></textarea>
<label for="category">Category</label> <input id="category" name="category" readonly placeholder="Select a category">
<div id="catalog" class="hidden"></div>
<div id="details" class="hidden">
  <label for="brand">Brand</label> <input id="brand" name="brand" readonly>
  <div id="brands" class="hidden"><input id="brand-search-input" aria-label="Search brands"><div id="brand-list"></div></div>
  <div id="size-row"><label for="size">Size</label> <input id="size" name="size" readonly>
    <div id="sizes" class="hidden"></div></div>
  <label for="condition">Condition</label> <input id="condition" name="condition" readonly>
  <div id="conditions" class="hidden"></div>
  <label for="color">Colours</label> <input id="color" name="color" readonly>
  <div id="colors" class="hidden"></div>
</div>
<label for="price">Price</label> <input id="price" name="price">
<div id="parcel" class="hidden">
  <button type="button" id="package-size-1" aria-pressed="false">Small For items that’d fit in a large envelope.</button>
  <button type="button" id="package-size-2" aria-pressed="false">Medium For items that’d fit in a shoebox.</button>
  <button type="button" id="package-size-3" aria-pressed="false">Large For items that’d fit in a moving box.</button>
</div>
<p id="error" role="alert"></p>
<button type="button" onclick="document.getElementById('error').textContent = 'Draft saved'">Save draft</button>
<button type="button" onclick="upload()">Upload</button>
</main>
<script>
const TREE = [
  {id: 1904, name: 'Women', children: [
    {id: 4, name: 'Clothing', children: [
      {id: 183, name: 'Jeans', children: [{id: 1845, name: 'Straight jeans'}, {id: 1844, name: 'Skinny jeans'}]},
      {id: 12, name: 'Tops & t-shirts', children: [{id: 221, name: 'T-shirts'}, {id: 222, name: 'Blouses'}]}]},
    {id: 16, name: 'Shoes', children: [{id: 2632, name: 'Sneakers'}]}]},
  {id: 5, name: 'Men', children: [{id: 2050, name: 'Clothing', children: [{id: 76, name: 'T-shirts'}]}]},
  {id: 1918, name: 'Kids', children: [{id: 1195, name: 'Toys'}]},
];
const BRANDS = ['Zara', 'Zara Kids', 'H&M', 'Mango', "Levi's"];
const TOPS = ['XS / 34 / 6', 'S / 36 / 8', 'M / 38 / 10', 'L / 40 / 12', 'XL / 42 / 14'];
const JEANS = {EU: ['EU 38', 'EU 40', 'EU 42'], W: ['W27', 'W28', 'W30']};
const CONDITIONS = [[6, 'New with tags', 'A brand-new, unused item with tags attached or in the original packaging.'],
  [1, 'New without tags', 'A brand-new, unused item without tags or original packaging.'],
  [2, 'Very good', 'A lightly used item that may have slight imperfections, but still looks great.'],
  [3, 'Good', 'A used item that may show imperfections and signs of wear.'],
  [4, 'Satisfactory', 'A frequently used item with imperfections and signs of wear.']];
const COLORS = [[1, 'Black'], [3, 'Grey'], [12, 'White'], [9, 'Blue'], [26, 'Light blue'], [27, 'Navy'], [7, 'Red']];
const state = {photos: 0, category: '', sizes: '', brand: '', size: '', condition: '', colors: [], parcel: 0};
const $ = (id) => document.getElementById(id);
const show = (id, on) => $(id).classList.toggle('hidden', !on);
let path = [];

$('photos').addEventListener('change', (e) => {
  for (const f of e.target.files) {
    const li = document.createElement('li'); li.textContent = 'Photo: ' + f.name; $('thumbs').appendChild(li);
    state.photos++;
  }
});

function renderCatalog() {
  const box = $('catalog'); box.innerHTML = '';
  if (path.length) {
    const back = document.createElement('button'); back.type = 'button'; back.setAttribute('aria-label', 'Back');
    back.textContent = '←'; back.onclick = () => { path.pop(); renderCatalog(); }; box.appendChild(back);
  }
  const search = document.createElement('input'); search.setAttribute('aria-label', 'Find a category');
  box.appendChild(search);
  for (const node of (path.length ? path[path.length - 1].children : TREE)) {
    const row = document.createElement(node.children ? 'button' : 'div');
    row.id = 'catalog-' + node.id; row.textContent = node.name;
    if (node.children) { row.type = 'button'; row.onclick = () => { path.push(node); renderCatalog(); }; }
    else { row.setAttribute('role', 'radio'); row.setAttribute('aria-checked', 'false'); row.onclick = () => pickCategory(node); }
    box.appendChild(row);
  }
}
$('category').onclick = () => {
  if (!$('catalog').classList.contains('hidden')) { show('catalog', false); return; }
  path = []; renderCatalog(); show('catalog', true);
};
function pickCategory(node) {
  state.category = [...path.map((n) => n.name), node.name].join(' > ');
  state.sizes = path.some((n) => n.name === 'Jeans') ? 'jeans' : path.some((n) => n.name.startsWith('Tops')) ? 'tops' : '';
  $('category').value = node.name;
  show('catalog', false); show('details', true); show('parcel', true); show('size-row', !!state.sizes);
}

$('brand').onclick = () => { show('brands', true); renderBrands(); };
$('brand-search-input').addEventListener('input', renderBrands);
function renderBrands() {
  const q = $('brand-search-input').value.toLowerCase();
  const list = $('brand-list'); list.innerHTML = '';
  if (!q) return;
  BRANDS.filter((b) => b.toLowerCase().startsWith(q)).forEach((b, i) => {
    const row = document.createElement('div'); row.setAttribute('role', 'radio'); row.id = 'brand-' + (10 + i);
    row.textContent = b; row.onclick = () => { state.brand = b; $('brand').value = b; show('brands', false); };
    list.appendChild(row);
  });
}

$('size').onclick = () => { renderSizes(''); show('sizes', true); };
function renderSizes(system) {
  const box = $('sizes'); box.innerHTML = '';
  if (state.sizes === 'jeans' && !system) {
    for (const name of Object.keys(JEANS)) {
      const b = document.createElement('button'); b.type = 'button'; b.textContent = name;
      b.onclick = () => renderSizes(name); box.appendChild(b);
    }
    return;
  }
  (state.sizes === 'jeans' ? JEANS[system] : TOPS).forEach((label, i) => {
    const row = document.createElement('div'); row.setAttribute('role', state.sizes === 'jeans' ? 'checkbox' : 'radio');
    row.setAttribute('aria-checked', 'false'); row.id = 'size-' + (200 + i); row.textContent = label;
    row.onclick = () => { state.size = label; $('size').value = label; show('sizes', false); };
    box.appendChild(row);
  });
}

$('condition').onclick = () => {
  const box = $('conditions'); box.innerHTML = '';
  for (const [id, name, text] of CONDITIONS) {
    const row = document.createElement('div'); row.setAttribute('role', 'radio'); row.id = 'condition-' + id;
    row.setAttribute('aria-checked', String(state.condition === name)); row.textContent = name + ' ' + text;
    row.onclick = () => { state.condition = name; $('condition').value = name; show('conditions', false); };
    box.appendChild(row);
  }
  show('conditions', true);
};

$('color').onclick = () => {
  const box = $('colors'); box.innerHTML = '';
  for (const [id, name] of COLORS) {
    const row = document.createElement('div'); row.setAttribute('role', 'checkbox'); row.id = 'color-' + id;
    row.textContent = name; row.setAttribute('aria-checked', String(state.colors.includes(name)));
    row.onclick = () => {
      if (state.colors.includes(name)) state.colors = state.colors.filter((c) => c !== name);
      else if (state.colors.length < 2) state.colors.push(name);
      row.setAttribute('aria-checked', String(state.colors.includes(name))); $('color').value = state.colors.join(', ');
    };
    box.appendChild(row);
  }
  const done = document.createElement('button'); done.type = 'button'; done.textContent = 'Done';
  done.onclick = () => show('colors', false); box.appendChild(done);
  show('colors', true);
};

for (const n of [1, 2, 3]) {
  $('package-size-' + n).onclick = () => {
    state.parcel = n;
    for (const m of [1, 2, 3]) $('package-size-' + m).setAttribute('aria-pressed', String(m === n));
  };
}

function upload() {
  const need = [];
  if (!state.photos) need.push('photos');
  if (!$('title').value) need.push('title');
  if (!$('description').value) need.push('description');
  if (!state.category) need.push('category');
  else {
    if (state.sizes && !state.brand) need.push('brand');
    if (state.sizes && !state.size) need.push('size');
    if (!state.condition) need.push('condition');
    if (state.sizes && !state.colors.length) need.push('colour');
  }
  if (!$('price').value) need.push('price');
  if (need.length) { $('error').textContent = 'Fill in ' + need[0] + ' to continue'; return; }
  const q = new URLSearchParams({title: $('title').value, description: $('description').value, price: $('price').value,
    category: state.category, brand: state.brand, size: state.size, condition: state.condition,
    colors: state.colors.join(','), parcel: String(state.parcel), photos: String(state.photos)});
  location.href = '/member.html?id=240394916&promo_shown=true&' + q;
}
</script></body></html>"""

# --- a levételhez: a profilod, a hirdetésed oldala (a jobb oldalon a Bump, Mark as sold, Hide, Edit listing és
# Delete gombbal, ahogy egy valódi levételnél a képernyőn látszott), egy másik eladó azonos című hirdetése, a
# kereső és egy kijelentkezett oldal. A törlés megerősítő ablakának gombja is „Delete” (mint az oldalon).

ITEM_SCRIPT = """<script>
const id = '%s';
if (localStorage.getItem('deleted-' + id)) {
  document.title = 'Page not found | Vinted';
  document.body.innerHTML = "<h1>Sorry, this page doesn't exist</h1><p>The item may have been deleted.</p>";
} else {
  document.getElementById('del').onclick = () => {
    const modal = document.createElement('div');
    modal.setAttribute('role', 'dialog');
    modal.innerHTML = '<p>Are you sure you want to delete this item?</p>' +
      '<button type="button" id="no">Cancel</button> <button type="button" id="yes">Delete</button>';
    document.body.appendChild(modal);
    document.getElementById('no').onclick = () => modal.remove();
    document.getElementById('yes').onclick = () => {
      localStorage.setItem('deleted-' + id, '1');
      setTimeout(() => { location.href = '/member/240394916/?deleted=' + id; }, 300);
    };
  };
}
</script>"""


def own_item(item_id: str, title: str) -> str:
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{title} | Vinted</title></head><body>"
            "<header><a href='/member/240394916/'>zsolesz</a> <a href='/items/new'>Sell now</a></header>"
            f"<main><h1>{title}</h1><p>Ft4,000.00</p><p>Brand: Zara</p>"
            "<a href='/member/240394916/'>zsolesz</a> <a href='/items/986-feher-polo/'>Fehér póló</a></main>"
            "<aside><button type='button'>Bump</button> <button type='button'>Mark as sold</button> "
            "<button type='button'>Hide</button> <button type='button'>Edit listing</button> "
            "<button type='button' id='del'>Delete</button></aside>" + ITEM_SCRIPT % item_id + "</body></html>")


PROFILE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>zsolesz | Vinted</title></head><body>
<header><a href="/member/240394916/">zsolesz</a></header>
<main><h1>zsolesz</h1><div id="grid">
<a id="i986" href="/items/986-feher-polo/">Fehér póló, brand: H&amp;M, condition: Good, size: M, Ft700.00</a>
<a id="i987" href="/items/987-hosszu-farmer-40-es-meret/">Hosszú farmer 40-es méret, brand: Zara, condition: Very good, size: EU 40, Ft4,000.00</a>
</div></main>
<script>for (const id of ['986', '987']) if (localStorage.getItem('deleted-' + id)) document.getElementById('i' + id).remove();</script>
</body></html>"""

CATALOG = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Search | Vinted</title></head><body>
<header><a href="/member/240394916/">zsolesz</a></header>
<main><a href="/items/555-hosszu-farmer-40-es-meret/">Hosszú farmer 40-es méret, brand: Mango, Ft3,500.00</a>
<a href="/items/987-hosszu-farmer-40-es-meret/">Hosszú farmer 40-es méret, brand: Zara, Ft4,000.00</a>
<a href="/items/321-rovid-farmer/">Rövid farmer, Ft2,000.00</a></main>
</body></html>"""

OTHER_SELLER = ("<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Hosszú farmer 40-es méret | Vinted"
                "</title></head><body><header><a href='/member/240394916/'>zsolesz</a></header><main><h1>Hosszú farmer "
                "40-es méret</h1></main><aside><button type='button'>Buy now</button> <button type='button'>Message "
                "seller</button></aside></body></html>")

LOGGED_OUT_ITEM = ("<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Hosszú farmer 40-es méret | "
                   "Vinted</title></head><body><header><button type='button'>Sign up</button> <button type='button'>"
                   "Log in</button></header><main><h1>Hosszú farmer 40-es méret</h1></main><aside><button "
                   "type='button'>Buy now</button></aside></body></html>")

NEW_LAYOUT_ITEM = ("<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Hosszú farmer 40-es méret | "
                   "Vinted</title></head><body><header><a href='/member/240394916/'>zsolesz</a></header><main><h1>"
                   "Hosszú farmer 40-es méret</h1></main><aside><button type='button' aria-label='More options'>⋯"
                   "</button></aside></body></html>")

PAGES = {
    "vinted.html": FORM,
    # a közzététel után a profilodra visz, ahol az új hirdetés is látszik (a hirdetés címét ebből jegyzi meg)
    "vinted2.html": FORM.replace("location.href = '/member.html?id=240394916&promo_shown=true&' + q;",
                                 "location.href = '/member/240394916/?promo_shown=true&' + q;"),
    "member/240394916/index.html": PROFILE,
    "items/987-hosszu-farmer-40-es-meret/index.html": own_item("987", "Hosszú farmer 40-es méret"),
    "items/555-hosszu-farmer-40-es-meret/index.html": OTHER_SELLER,
    "items/988-hosszu-farmer-40-es-meret/index.html": LOGGED_OUT_ITEM,
    "items/990-hosszu-farmer-40-es-meret/index.html": NEW_LAYOUT_ITEM,
    "catalog/index.html": CATALOG,
    "member.html": "<!doctype html><html><head><meta charset='utf-8'><title>zsolesz | Vinted</title></head>"
                   "<body><h1>Your item is uploaded</h1></body></html>",
    "belepes.html": "<!doctype html><html><head><meta charset='utf-8'><title>Vinted</title></head><body>"
                    "<h1>Join and sell pre-loved clothes with no fees</h1><button type='button'>Sign up</button> "
                    "<button type='button'>Log in</button></body></html>",
    "fooldal.html": "<!doctype html><html><head><meta charset='utf-8'><title>Vinted</title></head>"
                    "<body><h1>Vinted</h1><p>Valami egészen más oldal.</p></body></html>",
}

JEANS = dict(product="hosszú farmer", kind="ruha", title="Hosszú farmer 40-es méret",
             description="40-es méretű hosszú farmer, egyszer hordtam.", category="Nők > Ruhák > Farmerek",
             brand="Zara", size="40", condition="Nagyon jó", color="kék", price=4000, search_query="farmer")


class LocalSite:
    def __init__(self, pages: dict[str, str]):
        self.dir = tempfile.TemporaryDirectory()
        for name, content in pages.items():
            path = Path(self.dir.name) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_QuietHandler, directory=self.dir.name))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/{path}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()


def user_text(body: dict) -> str:
    return next(m["content"] for m in reversed(body["messages"]) if m["role"] == "user")


def pick(label: str):
    """A hamis modell a kérdés számozott listájából ezt a lehetőséget választja."""
    def answer(body):
        found = re.search(rf"^(\d+)\. {re.escape(label)}$", user_text(body), re.MULTILINE)
        assert found, (label, user_text(body))
        assert body.get("format"), "a választás kötött formájú válasz (JSON-séma)"
        return 200, reply(json.dumps({"choice": int(found.group(1))}))
    return answer


# --- a segédek (böngésző nélkül) ------------------------------------------------------------------------

def el(kind, label, field="", **extra):
    return {"id": 1, "kind": kind, "label": label, "field": field, **extra}


class MatchingTests(unittest.TestCase):
    def test_fields_as_the_error_bundle_showed_them(self):
        elements = [el("link", "Women", chrome=True), el("input:text", "Search for items", "search_text", chrome=True),
                    el("input:text", "Title", "title title"), el("textarea", "Description", "description description"),
                    el("input:text", "Category", "category category", readonly=True),
                    el("input:text", "Search brands", "brand-search-input"),
                    el("input:text", "Condition", "condition condition"),
                    el("radio", "Very good A lightly used item that may have slight imperfections", "condition-2"),
                    el("input:text", "Colours", "color color"), el("input:text", "Price", "price price"),
                    el("button", "Small For items that’d fit in a large envelope.", "package-size-1"),
                    el("button", "Size guide")]
        found = {name: (find_field(elements, name) or {}).get("label") for name in vinted.FIELDS}
        self.assertEqual(found, {"title": "Title", "description": "Description", "category": "Category",
                                 "brand": "Search brands", "size": None, "condition": "Condition",
                                 "color": "Colours", "price": "Price"},
                         "a package-size nem a méret, a „Size guide” sem; a condition-2 egy lehetőség, nem a mező")
        hungarian = [el("input:text", "Cím"), el("textarea", "Leírás (nem kötelező)"), el("input:text", "Ár")]
        self.assertEqual([find_field(hungarian, n)["label"] for n in ("title", "description", "price")],
                         ["Cím", "Leírás (nem kötelező)", "Ár"])

    def test_conditions_in_english_and_hungarian(self):
        english = [el("radio", f"{name} {text}", f"condition-{n}") for n, name, text in (
            (6, "New with tags", "A brand-new item."), (1, "New without tags", "Unused."),
            (2, "Very good", "A lightly used item."), (3, "Good", "A used item."), (4, "Satisfactory", "Worn."))]
        expected = {"Új, címkével": "New with tags", "Új": "New without tags", "Nagyon jó": "Very good", "Jó": "Good",
                    "Elfogadható": "Satisfactory", "Hibás": "Satisfactory"}
        for ours, theirs in expected.items():
            option, shown = condition_option(english, ours)
            self.assertEqual(shown, theirs, ours)
        hungarian = [el("radio", "Nagyon jó Keveset használt"), el("radio", "Jó Használt, látszik rajta")]
        self.assertEqual(condition_option(hungarian, "Jó")[1], "Jó", "a „Nagyon jó” nem a „Jó”")
        self.assertEqual(condition_option(hungarian, "Nagyon jó")[1], "Nagyon jó")

    def test_colours(self):
        self.assertEqual(color_values("fekete-fehér"), ["fekete", "feher"])
        self.assertEqual(color_values("kék és fehér, piros"), ["kek", "feher"], "a Vinteden legfeljebb kettő")
        options = [el("checkbox", name) for name in ("Black", "Grey", "White", "Blue", "Light blue", "Navy")]
        found = {c: (color_option(options, c) or {}).get("label")
                 for c in ("fekete", "kék", "világos kék", "sötétkék", "fehér", "lila")}
        self.assertEqual(found, {"fekete": "Black", "kék": "Blue", "világos kék": "Light blue", "sötétkék": "Navy",
                                 "fehér": "White", "lila": None})
        self.assertEqual(color_option([el("checkbox", "Kék"), el("checkbox", "Világoskék")], "kék")["label"], "Kék")

    def test_sizes(self):
        options = [el("radio", label) for label in ("XS / 34 / 6", "M / 38 / 10", "L / 40 / 12", "EU 40", "40.5")]
        found = {s: [o["label"] for o in size_options(options, s)] for s in ("L", "M-es", "38-as", "40", "40,5", "42")}
        self.assertEqual(found, {"L": ["L / 40 / 12"], "M-es": ["M / 38 / 10"], "38-as": ["M / 38 / 10"],
                                 "40": ["L / 40 / 12", "EU 40"], "40,5": ["40.5"], "42": []})
        self.assertEqual([o["label"] for o in size_options([el("radio", "40"), el("radio", "EU 40")], "40")], ["40"],
                         "ha van pontosan egyező, csak az")

    def test_brands(self):
        options = [el("radio", name) for name in ("Zara", "Zara Kids", "Mango Kids", "Mango Man")]
        self.assertEqual(brand_option(options, "zara")["label"], "Zara")
        self.assertIsNone(brand_option(options, "Mango"), "két Mango is van: a modell dönt")
        self.assertEqual(brand_option([el("radio", "H&M")], "H&M")["label"], "H&M")
        custom = [el("radio", "Use “NoName” as brand")]
        self.assertIsNone(brand_option(custom, "NoName"))
        self.assertEqual(custom_brand_option(custom, "NoName")["label"], "Use “NoName” as brand")

    def test_parcel_publish_consent_and_login(self):
        elements = [el("button", "Small For items that’d fit in a large envelope.", "package-size-1"),
                    el("button", "Medium For items that’d fit in a shoebox.", "package-size-2"),
                    el("button", "Upload photos"), el("button", "Save draft"), el("button", "Upload")]
        self.assertEqual(parcel_option(elements, "közepes")[1], "Medium")
        self.assertEqual(parcel_option(elements, "")[1], "Small", "alapból kicsi")
        self.assertEqual(publish_button(elements)["label"], "Upload", "a fotók gombja és a piszkozat nem az")
        self.assertEqual(publish_button([el("button", "Feltöltés")])["label"], "Feltöltés")
        self.assertEqual(consent_button([el("button", "Accept all", "onetrust-accept-btn-handler")])["label"],
                         "Accept all")
        self.assertIsNone(consent_button([el("button", "Elfogadom")]),
                          "a felhasználási feltételeket nem fogadja el helyetted, csak a sütiket")
        self.assertTrue(login_needed("https://www.vinted.hu/member/signup/select_type?ref_url=/items/new", []))
        self.assertTrue(login_needed("https://www.vinted.hu/items/new", [el("button", "Log in")]))
        self.assertFalse(login_needed("https://www.vinted.hu/items/new", [el("button", "Upload")]))

    def test_category_words_and_memory_key(self):
        words = category_words("Nők > Ruhák > Farmerek")
        self.assertTrue({"nok", "women", "ruhak", "clothing", "farmerek"} <= words)
        options = [el("button", "Women"), el("button", "Men")]
        self.assertEqual(category_option(options, words)["label"], "Women")
        self.assertIsNone(category_option([el("button", "Jeans"), el("button", "Tops")], words), "ezt a modell dönti el")
        black = choice_key({"kind": "ruha", "category": "Nők > Ruhák > Pólók", "product": "fekete L-es póló",
                            "brand": "Zara", "size": "L"})
        white = choice_key({"kind": "ruha", "category": "Nők > Ruhák > Pólók", "product": "H&M fehér póló",
                            "brand": "H&M", "size": "M"})
        skinny = choice_key({"kind": "ruha", "category": "Nők > Ruhák > Pólók", "product": "skinny póló"})
        self.assertEqual(black, white, "a szín, a márka és a méret nem számít")
        self.assertNotEqual(black, skinny)

    def test_list_options(self):
        filler = VintedFiller(None, None, {}, [])
        elements = [el("input:text", "Category", "category category"), el("div", "Women"),
                    el("button", "Women", "catalog-1904"), el("button", "Men", "catalog-5"), el("button", "Back"),
                    el("input:text", "Find a category"), el("button", "Small For items…", "package-size-1"),
                    el("link", "Women", chrome=True)]
        base = {vinted.elem_key(elements[0])}
        self.assertEqual([(o["kind"], o["label"]) for o in filler._options(elements, base, "category")],
                         [("button", "Women"), ("button", "Men")],
                         "egy felirat egyszer; a vissza gomb, a kereső, a csomagméret és a fejléc nem lehetőség")

    def test_memory_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = ChoiceMemory(Path(tmp) / "valasztasok" / "vinted.json")
            self.assertIsNone(memory.get("category", "x"))
            steps = [{"label": "Women", "field": "catalog-1904"}, {"label": "Jeans", "field": "catalog-183"}]
            memory.update({"category": {"x": steps}})
            memory.update({"category": {"y": steps[:1]}})
            self.assertEqual(memory.get("category", "x"), steps)
            self.assertEqual(memory.get("category", "y"), steps[:1])

    def test_delist_helpers(self):
        self.assertEqual(item_slug("Hosszú farmer 40-es méret"), "hosszu-farmer-40-es-meret")
        self.assertEqual(profile_url("https://www.vinted.hu/member/240394916?promo_shown=true&x=1"),
                         "https://www.vinted.hu/member/240394916")
        self.assertEqual(profile_url("https://www.vinted.hu/items/new"), "")
        cards = [el("link", "Fehér póló, brand: H&M", href="/items/986-feher-polo"),
                 el("link", "Ft4,000.00", href="/items/987-hosszu-farmer-40-es-meret?referrer=catalog"),
                 el("link", "Hosszú farmer 40-es méret, brand: Zara, size: EU 40", href="/items/987-x")]
        self.assertEqual(listing_link(cards, "Hosszú farmer 40-es méret")["href"],
                         "/items/987-hosszu-farmer-40-es-meret?referrer=catalog", "a webcím alapján is")
        self.assertIsNone(listing_link(cards[:1], "Hosszú farmer 40-es méret"))
        self.assertIsNone(listing_link([el("link", "Hosszú farmer 40-es méret", href="/member/1")],
                                       "Hosszú farmer 40-es méret"), "csak hirdetés")
        side = [el("button", "Bump"), el("button", "Mark as sold"), el("button", "Hide"),
                el("button", "Edit listing"), el("button", "Delete")]
        self.assertEqual(delete_button(side)["label"], "Delete")
        self.assertEqual(delete_button([el("button", "Törlés")])["label"], "Törlés")
        self.assertIsNone(delete_button([el("button", "Buy now"), el("button", "Delete account", chrome=True)]))
        page = [el("link", "zsolesz", href="/member/1-me", chrome=True),
                el("link", "zsolesz", href="/member/240394916")]
        self.assertEqual(seller_profile(page, "https://www.vinted.hu/items/987-x"),
                         "https://www.vinted.hu/member/240394916", "a fejlécen kívüli eladó-link")
        self.assertEqual(seller_profile(page + [el("link", "anna", href="/member/77-anna")], "https://www.vinted.hu/"),
                         "", "két eladó: nem egyértelmű")
        self.assertTrue(logged_out([el("button", "Log in", chrome=True)]))
        self.assertFalse(logged_out(side))
        before = Counter(elem_key(e) for e in side)
        after = side + [el("button", "Cancel"), el("button", "Delete")]
        fresh = new_elements(before, after)
        self.assertEqual([e["label"] for e in fresh], ["Cancel", "Delete"], "az ablak új „Delete” gombja")
        self.assertEqual(confirm_button(fresh)["label"], "Delete")
        self.assertEqual(confirm_button([el("button", "Mégse"), el("button", "Igen")])["label"], "Igen")
        self.assertIsNone(confirm_button([el("button", "Cancel")]))

    def test_the_model_answers_with_a_number(self):
        class FakeLlm:
            def __init__(self, answer):
                self.answer, self.asked = answer, []

            def chat_json(self, messages, schema, temperature=0.3, max_tokens=None):
                self.asked.append((messages, schema, temperature))
                self.max_tokens = max_tokens
                return self.answer

        options = [el("button", "Jeans"), el("button", "Tops & t-shirts")]
        llm = FakeLlm({"choice": 2})
        filler = VintedFiller(None, llm, {"title": "Fehér póló", "product": "póló", "kind": "ruha",
                                          "category": "Nők > Ruhák > Pólók"}, [])
        self.assertEqual(filler._ask("Kategória", options, ["Women", "Clothing"], allow_none=False)["label"],
                         "Tops & t-shirts")
        messages, schema, temperature = llm.asked[0]
        text = messages[1]["content"]
        for expected in ("A tárgy: Fehér póló", "Az eladó szerint a kategória: Nők > Ruhák > Pólók",
                         "eddig kiválasztva: Women > Clothing", "1. Jeans", "2. Tops & t-shirts"):
            self.assertIn(expected, text)
        self.assertEqual(schema["properties"]["choice"]["enum"], [1, 2], "kötött válasz: csak a lehetőségek száma")
        self.assertEqual(temperature, 0.0)
        self.assertIsNone(VintedFiller(None, FakeLlm({"choice": 0}), {}, [])._ask("Méret", options, [], "40"))
        self.assertIsNone(VintedFiller(None, FakeLlm({"choice": 7}), {}, [])._ask("Méret", options, [], "40"))


# --- a teljes kör: a Jarvis kitölti, a modell csak a kategóriát választja ki ---------------------------------

class VintedBrowserBase(unittest.TestCase):
    """Egy helyi „Vinted” (LocalSite), fej nélküli Chromium és a hamis Ollama; a tesztek a leszármazottakban."""

    def setUp(self):
        self.site = LocalSite(PAGES)
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        cfg.browser.headless = True
        cfg.web.allow_local = True
        cfg.assistant.show_timings = False
        cfg.work.platforms = ["vinted"]
        cfg.work.research_sites = []
        cfg.work.autofill = True
        self.platform = Platform("vinted", "Vinted", self.site.url("fooldal.html"), self.site.url("vinted.html"), "",
                                 ("127.0.0.1",), "Vinted: tippek a teszthez.")
        self.patches = [mock.patch.dict(selling.PLATFORMS, {"vinted": self.platform}, clear=True),
                        mock.patch.object(vinted, "PUBLISH_WAIT", 4.0)]
        for patch in self.patches:
            patch.start()
        self.ui = RecordingUI()
        self.ui.confirm_answer = True
        self.app = JarvisApp(cfg, self.ui)
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
        return self.store.update(item.id, **values)

    def post(self, item):
        self.app.run_work({"action": "post", "ids": [item.id], "platform": "vinted"})
        return self.store.get(item.id).platforms["vinted"]

    def sent(self) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(urlparse(self.app.toolbox.browser.url).query).items()}

    def agent_calls(self) -> list[dict]:
        """A Munka menü beszélgetésének modellhívásai (eszközökkel); a kategória kérdései nem ilyenek."""
        return [b for b in self.fake.chat_bodies() if b.get("tools")]


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class VintedFlowTests(VintedBrowserBase):
    def test_fills_the_whole_form_and_the_model_only_picks_the_category(self):
        item = self.item(**JEANS)
        self.fake.chat_script = [pick("Jeans"), pick("Straight jeans"), pick("EU")]
        state = self.post(item)
        self.assertEqual(self.fake.chat_script, [], "három rövid kérdés: két kategóriaszint és a méretrendszer")
        self.assertEqual(state["status"], POSTED)
        self.assertTrue(state["url"].startswith(self.site.url("member.html")), "a sikeres oldal címe")
        self.assertEqual(self.sent(), {
            "id": "240394916", "promo_shown": "true", "title": "Hosszú farmer 40-es méret",
            "description": "40-es méretű hosszú farmer, egyszer hordtam.", "price": "4000",
            "category": "Women > Clothing > Jeans > Straight jeans", "brand": "Zara", "size": "EU 40",
            "condition": "Very good", "colors": "Blue", "parcel": "1", "photos": "1"})
        self.assertEqual(self.agent_calls(), [], "a Munka menü modellje (eszközökkel, oldalnézetekkel) nem dolgozott")
        prompts = [e[1] for e in self.ui.of("confirm")]
        self.assertEqual(len(prompts), 1, "egy jóváhagyás: a közzététel")
        for expected in ("Vinted: Hosszú farmer 40-es méret – 4 000 Ft", "Modell nélkül töltöttem ki.",
                         "Kategória: Women > Clothing > Jeans > Straight jeans", "Márka: Zara", "Méret: EU 40",
                         "Állapot: Very good", "Szín: Blue", "Csomagméret: Small", "Upload"):
            self.assertIn(expected, prompts[0])
        key = choice_key({**{k: str(v) for k, v in JEANS.items()}})
        self.assertEqual([s["field"] for s in self.work.choices.get("category", key)],
                         ["catalog-1904", "catalog-4", "catalog-183", "catalog-1845"], "megjegyezte a kategóriautat")
        self.assertIn("Feltöltés modell nélkül: Hosszú farmer 40-es méret → Vinted", [e[1] for e in self.ui.of("info")])

    def test_a_remembered_category_needs_no_model_at_all(self):
        first = self.item(**SHIRT)
        self.fake.chat_script = [pick("Tops & t-shirts"), pick("T-shirts")]
        self.assertEqual(self.post(first)["status"], POSTED)
        self.assertEqual(self.sent()["size"], "L / 40 / 12", "az „L” a Vinted „L / 40 / 12” mérete")
        self.assertEqual(self.sent()["colors"], "Black")
        calls = len(self.fake.chat_bodies())
        second = self.item(**{**SHIRT, "product": "H&M fehér póló", "title": "H&M fehér póló, M", "brand": "H&M",
                              "size": "M", "color": "fehér", "condition": "Jó", "price": 700})
        self.assertEqual(self.post(second)["status"], POSTED)
        self.assertEqual(len(self.fake.chat_bodies()), calls, "a második póló már modell nélkül ment")
        sent = self.sent()
        self.assertEqual((sent["category"], sent["brand"], sent["size"], sent["condition"], sent["colors"], sent["price"]),
                         ("Women > Clothing > Tops & t-shirts > T-shirts", "H&M", "M / 38 / 10", "Good", "White", "700"))

    def test_an_unknown_brand_is_left_to_the_model_on_the_open_form(self):
        item = self.item(**{**JEANS, "brand": "NoName"})
        requests = []

        def model(body):
            requests.append(user_text(body))
            return 200, reply("A NoName márkát nem találom a Vinteden. Írd meg a pontos márkát, és folytatom.")

        self.fake.chat_script = [pick("Jeans"), pick("Straight jeans"), pick("EU"), model]
        state = self.post(item)
        self.assertEqual(self.fake.chat_script, [])
        request = requests[0]
        for expected in ("Nem sikerült: Márka: „NoName”: nem találom a Vinted márkái között",
                         "Kész: fotók: 1 db, cím, leírás, kategória: Women > Clothing > Jeans > Straight jeans",
                         "méret: EU 40", "állapot: Very good", "szín: Blue", "Az item_post eszközt ne használd",
                         "Márka: NoName", "--- oldalnézet ---", "Kezelhető elemek"):
            self.assertIn(expected, request)
        self.assertEqual((state["status"], state["note"]),
                         (FAILED, "A NoName márkát nem találom a Vinteden. Írd meg a pontos márkát, és folytatom."))
        self.assertEqual(self.ui.of("confirm"), [], "a félkész űrlapot nem tette közzé")
        self.assertIsNone(self.work.choices.get("category", choice_key({k: str(v) for k, v in JEANS.items()})),
                          "amíg nincs fent, nem jegyzi meg")
        info = json.loads((self.work.dossiers.path(state["dossier"]) / "info.json").read_text(encoding="utf-8"))
        self.assertEqual(info["mode"], "modell")
        folders = sorted(self.work.dossiers.root.iterdir())
        first = json.loads((folders[0] / "info.json").read_text(encoding="utf-8"))
        self.assertEqual(first["mode"], "Vinted-kitöltő")
        self.assertTrue(any("NoName" in line for line in first["kitoltes"]), first["kitoltes"])

    def test_rejected_publish_is_checked_and_never_posted_again(self):
        item = self.item(**{**JEANS, "color": ""})   # a Vinted ruhánál kötelezően kéri a színt
        results = []

        def try_again(body):
            assert "a Vinted ezt írta ki: Fill in colour to continue" in user_text(body), user_text(body)
            return 200, tool_reply("item_post", {"item": item.id, "platform": "vinted"})

        def give_up(body):
            results.append(next(m["content"] for m in reversed(body["messages"]) if m["role"] == "tool"))
            return 200, reply("A Vinted színt kér, de a tárgynál nincs megadva. Írd meg, milyen színű.")

        self.fake.chat_script = [pick("Jeans"), pick("Straight jeans"), pick("EU"), try_again, give_up]
        state = self.post(item)
        self.assertEqual(self.fake.chat_script, [], "az ellenőrzés után nincs második ellenőrzés")
        self.assertIn("kétszer kerülne fel", results[0], "közzététel után az item_post tilos")
        self.assertEqual(state["status"], FAILED)
        self.assertIn("A Vinted színt kér", state["note"])
        self.assertIn("Ellenőrzés: Hosszú farmer 40-es méret → Vinted", [e[1] for e in self.ui.of("info")])

    def test_login_page_stops_without_the_model(self):
        self.platform = Platform("vinted", "Vinted", self.site.url("fooldal.html"), self.site.url("belepes.html"), "",
                                 ("127.0.0.1",), "Vinted: tippek a teszthez.")
        self.work.platforms["vinted"] = self.platform
        state = self.post(self.item(**JEANS))
        self.assertEqual(self.fake.chat_bodies(), [], "se a modell, se egy kérdés")
        self.assertEqual(state["status"], FAILED)
        self.assertTrue(state["note"].startswith("Be kell jelentkezned a Vintedre"), state["note"])

    def test_declined_publish_is_not_an_error_bundle(self):
        self.ui.confirm_answer = False
        self.fake.chat_script = [pick("Jeans"), pick("Straight jeans"), pick("EU")]
        state = self.post(self.item(**JEANS))
        self.assertEqual((state["status"], state["note"]), (FAILED, "Nem engedélyezted a közzétételt."))
        self.assertNotIn("dossier", state)
        self.assertEqual(self.agent_calls(), [])
        self.assertTrue(self.app.toolbox.browser.url.endswith("vinted.html"), "nem tette közzé")

    def test_an_unknown_page_falls_back_to_the_model(self):
        self.work.platforms["vinted"] = Platform("vinted", "Vinted", self.site.url("fooldal.html"),
                                                 self.site.url("fooldal.html"), "", ("127.0.0.1",), "Tippek.")
        item = self.item(**JEANS)
        self.fake.chat_script = [tool_reply("item_post", {"item": item.id, "platform": "vinted"}),
                                 reply("Nem találom a hirdetésfeladást.")]
        state = self.post(item)
        self.assertEqual(self.fake.chat_script, [], "a modell csinálta végig, az elejétől")
        self.assertEqual(state["status"], FAILED)
        infos = " ".join(e[1] for e in self.ui.of("info"))
        self.assertIn("A modell nélküli kitöltés most nem megy (nem ismerem fel a hirdetési űrlapot", infos)

    def test_switched_off(self):
        self.app.cfg.work.autofill = False
        item = self.item(**JEANS)
        self.assertFalse(self.work.can_autofill(item.id, "vinted"))
        self.app.cfg.work.autofill = True
        self.assertTrue(self.work.can_autofill(item.id, "vinted"))
        self.assertFalse(self.work.can_autofill(self.store.create(targets=["vinted"]).id, "vinted"),
                         "cím, leírás, ár és fotó nélkül nem kezdi el")


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class VintedDelistTests(VintedBrowserBase):
    """A levétel modell nélkül: a hirdetés oldalán a Delete, a megerősítés, és hogy tényleg eltűnt-e."""

    def setUp(self):
        super().setUp()
        self.platform = Platform("vinted", "Vinted", self.site.url("fooldal.html"), self.site.url("vinted2.html"),
                                 self.site.url("catalog/?search_text={q}"), ("127.0.0.1",),
                                 "Vinted: tippek a teszthez.")
        self.work.platforms["vinted"] = self.platform

    def posted(self, url: str = ""):
        item = self.item(**JEANS)
        self.store.mark(item.id, "vinted", POSTED, url=url)
        return item

    def delist(self, item):
        self.app.run_work({"action": "delist", "ids": [item.id], "platforms": ["vinted"]})
        return self.store.get(item.id).platforms["vinted"]

    def deleted(self, item_id: str) -> bool:
        return bool(self.app.toolbox.browser.page.evaluate(f"() => localStorage.getItem('deleted-{item_id}')"))

    def test_known_listing_is_deleted_without_the_model(self):
        item = self.posted(self.site.url("items/987-hosszu-farmer-40-es-meret/"))
        state = self.delist(item)
        self.assertEqual((state["status"], state["note"]), (selling.NOT_POSTED, "Levéve"))
        self.assertTrue(self.deleted("987"), "a megerősítő ablak „Delete” gombjával törölte")
        self.assertEqual(self.fake.chat_bodies(), [], "se a modell, se egy kérdés")
        self.assertEqual(self.ui.of("confirm"), [], "a levételt már jóváhagytad: a Delete gombra nem kérdez újra")
        infos = [e[1] for e in self.ui.of("info")]
        self.assertIn("Levétel modell nélkül: Hosszú farmer 40-es méret → Vinted", infos)
        self.assertNotIn("dossier", state)

    def test_profile_url_from_the_upload_leads_to_the_listing(self):
        item = self.posted(self.site.url("member/240394916/?promo_shown=true"))   # ahogy a feltöltés után megmaradt
        state = self.delist(item)
        self.assertEqual(state["status"], selling.NOT_POSTED)
        self.assertTrue(self.deleted("987"), "a profilon megtalálta a hirdetést")
        self.assertEqual(self.work.settings["vinted_profile"], self.site.url("member/240394916/"),
                         "a profilod címét is megjegyezte")
        self.assertEqual(self.fake.chat_bodies(), [])

    def test_without_url_it_finds_your_listing_in_the_search(self):
        item = self.posted()
        state = self.delist(item)
        self.assertEqual(state["status"], selling.NOT_POSTED)
        self.assertTrue(self.deleted("987"), "a keresőben a másik eladó azonos című hirdetését átugrotta")
        self.assertFalse(self.deleted("555"))
        self.assertEqual(self.fake.chat_bodies(), [])
        self.assertEqual(self.work.settings["vinted_profile"], self.site.url("member/240394916/"),
                         "a hirdetésed oldaláról a profilod címét is megjegyezte")

    def test_logged_out_stops_without_the_model(self):
        item = self.posted(self.site.url("items/988-hosszu-farmer-40-es-meret/"))
        state = self.delist(item)
        self.assertEqual(state["status"], POSTED, "fent marad")
        self.assertTrue(state["note"].startswith("A levétel nem sikerült: Be kell jelentkezned a Vintedre"),
                        state["note"])
        self.assertEqual(self.fake.chat_bodies(), [])

    def test_already_gone_is_marked_as_delisted(self):
        item = self.posted(self.site.url("items/989-hosszu-farmer-40-es-meret/"))   # 404
        state = self.delist(item)
        self.assertEqual((state["status"], state["note"]), (selling.NOT_POSTED, "Levéve (már nem volt fent)"))
        self.assertEqual(self.fake.chat_bodies(), [])

    def test_unknown_layout_falls_back_to_the_model_on_the_listing(self):
        item = self.posted(self.site.url("items/990-hosszu-farmer-40-es-meret/"))
        requests, results = [], []

        def model(body):
            requests.append(user_text(body))
            return 200, tool_reply("item_delist", {"item": item.id, "platform": "vinted"})

        def give_up(body):
            results.append(next(m["content"] for m in reversed(body["messages"]) if m["role"] == "tool"))
            return 200, reply("Nem találom a törlés gombját.")

        self.fake.chat_script = [model, give_up]
        state = self.delist(item)
        self.assertEqual(self.fake.chat_script, [], "a modell vette át")
        self.assertIn("Vedd le a(z) „Hosszú farmer 40-es méret” hirdetést", requests[0])
        self.assertIn("Megnyitottam a hirdetést: " + self.site.url("items/990-hosszu-farmer-40-es-meret/"), results[0])
        self.assertNotIn("modell nélküli levétel", results[0], "az item_delist nem kezdte újra a levevőt")
        self.assertEqual(state["status"], POSTED)
        infos = [e[1] for e in self.ui.of("info")]
        self.assertEqual(sum(i.startswith("Levétel modell nélkül") for i in infos), 1)
        self.assertIn("A modell nélküli levétel most nem megy", " ".join(infos))

    def test_delist_from_the_chat_uses_the_delister_too(self):
        item = self.posted(self.site.url("items/987-hosszu-farmer-40-es-meret/"))
        results = []

        def done(body):
            results.append(next(m["content"] for m in reversed(body["messages"]) if m["role"] == "tool"))
            return 200, reply("Levettem a Vintedről.")

        self.fake.chat_script = [tool_reply("item_delist", {"item": item.id, "platform": "vinted"}), done]
        self.app.respond("A farmer elkelt, vedd le a Vintedről.", mode="work")
        self.assertEqual(self.fake.chat_script, [])
        self.assertEqual(results[0], "Levettem a(z) Vinted oldalról (modell nélkül). Megjelöltem: nincs fent.")
        self.assertTrue(self.deleted("987"))
        state = self.store.get(item.id).platforms["vinted"]
        self.assertEqual((state["status"], state["note"]), (selling.NOT_POSTED, "Levéve"))
        self.assertEqual(len(self.ui.of("confirm")), 1, "egyszer megkérdezte, levegye-e; a Delete gombokra már nem")

    def test_upload_remembers_the_listing_url(self):
        self.fake.chat_script = [pick("Jeans"), pick("Straight jeans"), pick("EU")]
        state = self.post(self.item(**JEANS))
        self.assertEqual(state["status"], POSTED)
        self.assertEqual(state["url"], self.site.url("items/987-hosszu-farmer-40-es-meret/"),
                         "a profilon látszó hirdetés címe, nem a profilé")
        self.assertEqual(self.work.settings["vinted_profile"], self.site.url("member/240394916/"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
