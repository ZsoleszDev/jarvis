"""A begyakorolt lépések tesztjei: a modell egyszer végigcsinál egy feltöltést (vagy levételt) egy
helyi „hirdetési oldalon”, Jarvis megjegyzi, és a következő tárgynál már modell nélkül csinálja.

A böngészős tesztek valódi (láthatatlan) Chromiumot indítanak helyi tesztoldalakkal; ha a Playwright
vagy a Chromium nincs telepítve, ezek kimaradnak. Internet és modell nem kell.

Futtatás:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import functools
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

    def test_plan_needs_every_choice_value(self):
        rec = self.recorder([
            {"do": "click", "el": el("button", "Kategória")},
            {"do": "click", "el": el("option", "Pólók")},
            {"do": "click", "el": el("button", "Feltöltés"), "risky": True, "url": "a", "url_after": "b"},
        ])
        recording, _ = rec.build(VALUES)
        replayer = Replayer(None, recording, {**VALUES, "category": "Nők > Ruhák > Nadrágok"}, ["1.jpg"])
        self.assertIn("Kategória", replayer.plan_problem())
        self.assertEqual(Replayer(None, recording, VALUES, ["1.jpg"]).plan_problem(), "")

    def test_helpers(self):
        self.assertEqual(field_for("Csomagméret"), "parcel_size", "nem a „méret”")
        self.assertEqual(field_for("Válassz kategóriát"), "category")
        self.assertEqual(match_field("Zara fekete póló L", VALUES), "title", "kicsit átfogalmazva is")
        self.assertEqual(match_field("500", VALUES), "price")
        self.assertEqual(match_field("Teljesen más", VALUES), "")
        self.assertEqual(url_shape("https://www.vinted.hu/items/123-zara-polo?ref=1"), "www.vinted.hu/items/#")


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

    def learn_posting(self, item):
        self.fake.chat_script = [
            tool_reply("item_post", {"item": item.id, "platform": "vinted"}),
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

    def test_a_changed_page_falls_back_to_the_model_and_relearns(self):
        first = self.item()
        self.learn_posting(first)
        # Az oldal megváltozott: a cím mező felirata és neve is más.
        self.site.write("urlap.html", form("Hirdetés címe", "cim"))
        second = self.item(title="H&M fehér póló, M", price=700, brand="H&M")
        self.fake.chat_script = [
            tool_reply("item_post", {"item": second.id, "platform": "vinted"}),
            reply("Most nem sikerült, mert hiányzik egy mező."),
        ]
        self.app.run_work({"action": "post", "ids": [second.id], "platform": "vinted"})
        self.assertEqual(self.fake.chat_script, [], "elakadt, ezért a modell vette át")
        infos = " ".join(e[1] for e in self.ui.of("info"))
        self.assertIn("A begyakorolt lépések most nem jók", infos)
        self.assertEqual(self.work.flows.get("vinted-post-ruha-nok-ruhak-polok")["fails"], 1)
        self.assertEqual(self.store.get(second.id).platforms["vinted"]["status"], "hiba")
        self.assertFalse([c for c in self.confirms() if "Begyakorolt" in c], "közzététel előtt állt meg")

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
