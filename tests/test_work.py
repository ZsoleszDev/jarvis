"""Az Adás/Vétel (Munka menü) tesztjei: tárgyak és fotók, mit kér az egyes oldal, az adatok a
leírásból, visszakérdezés, árellenőrzés, és a feltöltés oldalanként.

A böngészős tesztek valódi (láthatatlan) Chromiumot indítanak helyi tesztoldalakkal; ha a
Playwright vagy a Chromium nincs telepítve, ezek kimaradnak. Internet és modell nem kell.

Futtatás a projekt mappájából:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import functools
import io
import json
import os
import re
import statistics
import sys
import tempfile
import threading
import time
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image  # noqa: E402

from test_offline import FakeOllama, make_cfg, reply, tool_reply  # noqa: E402
from test_streaming import RecordingUI  # noqa: E402
from test_web_browser import HAS_CHROMIUM  # noqa: E402

from jarvis_core import config, selling  # noqa: E402
from jarvis_core.app import JarvisApp  # noqa: E402
from jarvis_core.browser import BrowserController, BrowserError, is_risky, navigates  # noqa: E402
from jarvis_core.config import load_config  # noqa: E402
from jarvis_core.llm import OllamaClient, OllamaError, parse_json_object  # noqa: E402
from jarvis_core.selling import (FAILED, NOT_POSTED, POSTED, Item, ItemError, ItemStore,  # noqa: E402
                                 ListingExtractor,
                                 Platform, PriceResearcher, missing_needs, needs_for, parse_price,
                                 price_verdict, question_for, round_price)
from jarvis_core.textutil import clean_for_speech, hu_amounts, parse_hu_amount  # noqa: E402
from jarvis_core.tools import Toolbox  # noqa: E402
from jarvis_core.work import WorkDesk, interpret_price_reply, research_summary  # noqa: E402


def jpeg(width: int = 300, height: int = 400, color=(20, 20, 30), orientation: int | None = None) -> bytes:
    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        image.save(buffer, "JPEG", exif=exif.tobytes())
    else:
        image.save(buffer, "JPEG")
    return buffer.getvalue()


def png_with_alpha() -> bytes:
    image = Image.new("RGBA", (200, 200), (255, 0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


class FakeClock:
    """Hamis óra a work modulnak (mock.patch("jarvis_core.work.time", ...)): a várakozás azonnal lefut, csak
    előreállítja az órát, így mérhető, mi történt a közzétételek közti szünet előtt és után."""

    def __init__(self, now: float = 1_000_000.0):
        self.now = now
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def extracted(**values) -> dict:
    """A modell válasza az adatok kiolvasására (JSON); amit nem adunk meg, az üres."""
    data = {key: "" for key in selling.LISTING_FIELDS}
    data.update({"kind": "egyéb", "price": 0})
    data.update(values)
    return reply(json.dumps(data, ensure_ascii=False))


# Egy teljes ruhás hirdetés adatai (ezzel nincs visszakérdezés)
SHIRT = dict(product="fekete póló", kind="ruha", title="Zara fekete póló, L",
             description="Zara fekete póló L-es méretben, egyszer hordtam.", category="Nők > Ruhák > Pólók",
             brand="Zara", size="L", condition="Nagyon jó", color="fekete", price=500, location="Budapest",
             search_query="Zara póló")


# --- tárgyak és fotók ------------------------------------------------------------------

class ItemStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.changes: list[dict] = []
        self.removed: list[str] = []
        self.store = ItemStore(Path(self.tmp.name), max_photos=3, on_change=self.changes.append,
                               on_remove=self.removed.append)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_and_status(self):
        item = self.store.create(targets=["vatera", "vinted"], prompt="  Zara póló,\n\n\n\n L-es  ",
                                 research_wanted=1)
        self.assertEqual(item.targets, ["vinted", "vatera"], "az oldalak sorrendje mindig ugyanaz")
        self.assertEqual(item.prompt, "Zara póló,\n\nL-es")
        self.assertIs(item.research_wanted, True)
        self.assertEqual((item.status, item.name), ("vázlat", "Zara póló, L-es"))
        self.store.set_pending(item.id, {"kind": "info", "question": "Mennyi?"})
        self.assertEqual(self.store.get(item.id).status, "kérdés")
        self.store.set_pending(item.id, {"kind": "price", "question": "Marad?", "suggested": 400})
        self.assertEqual(self.store.get(item.id).status, "árdöntés")
        self.store.set_pending(item.id, None)
        self.store.mark(item.id, "vinted", POSTED, "https://www.vinted.hu/items/1")
        self.assertEqual(self.store.get(item.id).status, "részben fent")
        self.store.mark(item.id, "vatera", FAILED, note="Be kell jelentkezni.")
        self.assertEqual(self.store.get(item.id).platforms["vatera"]["note"], "Be kell jelentkezni.")
        self.assertEqual(self.store.get(item.id).status, "részben fent")
        self.store.mark(item.id, "vatera", POSTED)
        self.assertEqual(self.store.get(item.id).status, "fent")
        self.store.mark(item.id, sold=True)
        self.assertEqual(self.store.get(item.id).status, "eladva")
        self.assertEqual(self.changes[-1]["status"], "eladva")     # a felület mindig értesül

    def test_name_prefers_title_then_product_then_description(self):
        item = self.store.create()
        self.assertEqual(item.name, "Új tárgy")
        self.store.update(item.id, prompt="Egy nagyon hosszú leírás " * 5)
        self.assertEqual(len(self.store.get(item.id).name), 50)
        self.store.update(item.id, product="fekete póló")
        self.assertEqual(self.store.get(item.id).name, "fekete póló")
        self.store.update(item.id, title="Zara fekete póló, L")
        self.assertEqual(self.store.get(item.id).name, "Zara fekete póló, L")

    def test_validation(self):
        with self.assertRaises(ItemError):
            self.store.create(price="sok")
        with self.assertRaisesRegex(ItemError, "Ismeretlen hirdetési oldal: ebay"):
            self.store.create(targets=["vinted", "ebay"])
        with self.assertRaises(ItemError):
            self.store.create(targets="vinted")
        self.assertEqual(self.store.all(), [], "hibás adatnál nem jön létre tárgy")
        item = self.store.create()
        for bad in ({"price": -5}, {"price": "20000000"}, {"id": "x"}, {"photos": []}, {"edited": []}):
            with self.assertRaises(ItemError, msg=str(bad)):
                self.store.update(item.id, **bad)
        self.assertEqual(self.store.update(item.id, condition="nagyon jo").condition, "Nagyon jó")
        self.assertEqual(self.store.update(item.id, condition="valami más").condition, "")
        self.assertEqual(self.store.update(item.id, kind="Ruha").kind, "ruha")
        self.assertEqual(self.store.update(item.id, kind="bútor").kind, "egyéb")
        self.assertEqual(self.store.update(item.id, title="x" * 500).title, "x" * 100)
        with self.assertRaises(ItemError):
            self.store.mark(item.id, "ebay", POSTED)
        with self.assertRaises(ItemError):
            self.store.mark(item.id, "vinted", "valami")

    def test_your_edits_are_not_overwritten_by_jarvis(self):
        item = self.store.create(prompt="póló")
        self.store.update(item.id, user=True, title="Saját cím", price=900, prompt="póló, L")
        self.assertEqual(self.store.get(item.id).edited, ["price", "title"], "a leírásod nem hirdetésmező")
        item = self.store.update(item.id, title="Jarvis címe", price=500, brand="Zara")
        self.assertEqual((item.title, item.price, item.brand), ("Saját cím", 900, "Zara"))

    def test_time_online_starts_when_the_listing_goes_up(self):
        item = self.store.create(targets=["vinted"], prompt="farmer")
        day = 86400
        with mock.patch("jarvis_core.selling.time.time", return_value=1000.0):
            self.store.mark(item.id, "vinted", POSTED, url="https://www.vinted.hu/items/1")
        with mock.patch("jarvis_core.selling.time.time", return_value=1000.0 + 20 * day):
            item = self.store.mark(item.id, "vinted", POSTED, note="A levétel nem sikerült: nincs Törlés gomb.")
            public = item.public()
        entry = item.platforms["vinted"]
        self.assertEqual((entry["posted_at"], entry["at"]), (1000.0, 1000.0 + 20 * day), "az újbóli jelölés nem nulláz")
        self.assertEqual(entry["url"], "https://www.vinted.hu/items/1")
        self.assertEqual((public["online_since"], public["days_online"]), (1000.0, 20))
        item = self.store.mark(item.id, "vinted", NOT_POSTED)
        self.assertNotIn("posted_at", item.platforms["vinted"])
        self.assertIsNone(item.public()["days_online"])
        with mock.patch("jarvis_core.selling.time.time", return_value=5000.0):
            item = self.store.mark(item.id, "vinted", POSTED)
        self.assertEqual(item.platforms["vinted"]["posted_at"], 5000.0, "levétel után újra felkerült")
        item.platforms["vinted"] = {"status": POSTED, "at": 700.0}      # a régi adatoknál a jelölés ideje
        self.assertEqual(item.online_since(), 700.0)

    def test_prices(self):
        for text, value in (("4500", 4500), ("4 500 Ft", 4500), ("4.500", 4500), ("12,5", 12),
                            ("", None), (None, None), (3000, 3000), ("2 500 HUF", 2500)):
            self.assertEqual(parse_price(text), value, text)
        self.assertEqual(round_price(4321), 4300)
        self.assertEqual(round_price(260), 250)
        self.assertEqual(selling.format_price(12990), "12 990 Ft")
        self.assertEqual(clean_for_speech("Javasolt ár: 4 500 Ft."), "Javasolt ár: 4500 forint.")

    def test_posted_price_and_drift(self):
        item = self.store.create(targets=["vinted", "jofogas"], title="Levi's farmer", price=9000)
        url = "https://www.vinted.hu/items/1234567-levis-farmer"
        item = self.store.mark(item.id, "vinted", POSTED, url=url)
        self.assertEqual((item.platforms["vinted"]["posted_price"], item.platforms["vinted"]["posted_title"]),
                         (9000, "Levi's farmer"))
        self.assertEqual(item.public()["drift"], [])
        item = self.store.update(item.id, user=True, price=8500, title="Levi's 501 farmer")
        self.assertEqual(item.public()["drift"], [{"platform": "vinted", "price": 9000, "title": "Levi's farmer"}])
        self.store.set_check(item.id, "vinted", "live")
        item = self.store.mark(item.id, "vinted", POSTED, note="Itt kelt el.")
        entry = item.platforms["vinted"]
        self.assertEqual((entry["posted_price"], entry["url"], entry["check"]), (9000, url, "live"),
                         "ugyanaz a hirdetés: a feltöltéskori ár és az ellenőrzés marad")
        item = self.store.mark(item.id, "vinted", POSTED, url="https://www.vinted.hu/items/7654321-uj")
        self.assertEqual(item.platforms["vinted"]["posted_price"], 8500, "új hirdetés: a mostani ár")
        self.assertNotIn("check", item.platforms["vinted"])
        item = self.store.mark(item.id, "vinted", "nincs fent")
        self.assertEqual(item.drift(), [], "ami nincs fent, ott nincs mit átírni")

    def test_photos(self):
        item = self.store.create()
        self.store.add_photo(item.id, jpeg(300, 200, orientation=6))     # a telefon elforgatva menti
        self.store.add_photo(item.id, png_with_alpha())
        item = self.store.add_photo(item.id, jpeg(3000, 4000))
        self.assertEqual(item.photos, ["01.jpg", "02.jpg", "03.jpg"])
        with Image.open(self.store.photo_file(item.id, "01.jpg")) as first:
            self.assertEqual(first.size, (200, 300), "az EXIF szerint elforgatva")
        with Image.open(self.store.photo_file(item.id, "03.jpg")) as big:
            self.assertEqual(max(big.size), selling.PHOTO_MAX_SIDE)
        with Image.open(self.store.photo_file(item.id, "03.jpg", thumb=True)) as thumb:
            self.assertEqual(max(thumb.size), selling.THUMB_SIDE)
        with Image.open(self.store.photo_file(item.id, "02.jpg")) as converted:
            self.assertEqual(converted.mode, "RGB")
        with self.assertRaisesRegex(ItemError, "legfeljebb 3"):
            self.store.add_photo(item.id, jpeg())
        with self.assertRaisesRegex(ItemError, "nem kép"):
            self.store.add_photo(item.id, b"<html>nem kep</html>")
        for bad in ("../item.json", "01.png", "04.jpg", "01.jpg/../../x"):
            with self.assertRaises(ItemError, msg=bad):
                self.store.photo_file(item.id, bad)
        item = self.store.reorder_photos(item.id, ["03.jpg"])
        self.assertEqual(item.photos, ["03.jpg", "01.jpg", "02.jpg"])
        item = self.store.remove_photo(item.id, "01.jpg")
        self.assertEqual(item.photos, ["03.jpg", "02.jpg"])
        self.assertFalse((self.store.folder(item.id) / "01.jpg").exists())
        item = self.store.add_photo(item.id, jpeg())
        self.assertEqual(item.photos[-1], "04.jpg", "a fájlnév nem ismétlődik")

    def test_persistence_and_soft_delete(self):
        item = self.store.create(targets=["vinted"], prompt="kék pulóver", price=1000)
        self.store.add_photo(item.id, jpeg())
        self.store.set_busy(item.id, "Feltöltés…")
        self.store.set_pending(item.id, {"kind": "info", "question": "Milyen állapotú?"})
        loaded = ItemStore(Path(self.tmp.name)).get(item.id)
        self.assertEqual((loaded.price, loaded.photos, loaded.busy, loaded.targets), (1000, ["01.jpg"], "", ["vinted"]))
        self.assertEqual(loaded.pending["question"], "Milyen állapotú?", "a kérdés újraindítás után is megvan")
        self.store.delete(item.id)
        self.assertEqual(self.removed, [item.id])
        self.assertTrue(any((Path(self.tmp.name) / "_torolt").iterdir()), "a törölt tárgy visszahozható")
        self.assertEqual(ItemStore(Path(self.tmp.name)).all(), [])
        with self.assertRaises(ItemError):
            self.store.get(item.id)

    def test_items_from_the_previous_version_still_load(self):
        folder = Path(self.tmp.name) / "a1b2c3d4"
        folder.mkdir()
        (folder / "item.json").write_text(json.dumps({
            "id": "a1b2c3d4", "created": 1, "updated": 1, "photos": ["01.jpg"], "title": "Kék pulóver",
            "notes": "egyszer hordott", "price": 2000, "ismeretlen": True}), encoding="utf-8")
        item = ItemStore(Path(self.tmp.name)).get("a1b2c3d4")
        self.assertEqual((item.title, item.prompt, item.price, item.targets, item.photos),
                         ("Kék pulóver", "egyszer hordott", 2000, [], []))

    def test_find(self):
        a = self.store.create()
        self.store.update(a.id, title="Kék pulóver")
        b = self.store.create()
        self.store.update(b.id, title="Kék farmer")
        self.assertEqual(self.store.find(a.id).id, a.id)
        self.assertEqual(self.store.find("pulover").id, a.id)
        with self.assertRaisesRegex(ItemError, "Több"):
            self.store.find("kék")
        with self.assertRaisesRegex(ItemError, "Nincs ilyen"):
            self.store.find("kalap")


# --- mit kér az egyes oldal ----------------------------------------------------------------

class NeedsTests(unittest.TestCase):
    def item(self, targets, **values) -> Item:
        return Item(id="a1b2c3d4", created=0, updated=0, targets=targets, **values)

    def missing(self, item, settings=None):
        return [(need.field, sites) for need, sites in missing_needs(item, settings or {})]

    def test_every_site_needs_the_basics(self):
        self.assertEqual([n.field for n in needs_for(["vatera"])], ["product", "price", "condition", "shipping"])
        self.assertEqual(self.missing(self.item(["vatera"])),
                         [("product", []), ("price", []), ("condition", []), ("shipping", ["Vatera"])])

    def test_vinted_needs_brand_size_and_colour_only_for_clothes(self):
        base = dict(product="póló", price=500, condition="Jó")
        self.assertEqual(self.missing(self.item(["vinted"], kind="ruha", **base)),
                         [("brand", ["Vinted"]), ("size", ["Vinted"]), ("color", ["Vinted"])])
        self.assertEqual(self.missing(self.item(["vinted"], kind="cipő", brand="Nike", size="42", color="fehér",
                                                **base)), [])
        self.assertEqual(self.missing(self.item(["vinted"], kind="ruha", brand="NoName", size="40", **base)),
                         [("color", ["Vinted"])], "a Vinted a színt is kötelezően kéri, ne a modell találja ki")
        self.assertEqual(self.missing(self.item(["vinted"], kind="elektronika", **base)), [],
                         "egy videókártyának nincs mérete")
        self.assertEqual(self.missing(self.item(["jofogas", "vatera", "hardverapro"], kind="ruha", location="Pécs",
                                                shipping="Foxpost", **base)), [],
                         "a márka és a méret csak a Vintedhez kell")

    def test_city_and_shipping_can_come_from_the_settings(self):
        item = self.item(["jofogas", "hardverapro"], product="RTX 3060", price=90000, condition="Jó",
                         kind="elektronika")
        self.assertEqual(self.missing(item), [("location", ["Jófogás", "Hardverapró"]), ("shipping", ["Hardverapró"])])
        self.assertEqual(self.missing(item, {"city": "Budapest", "shipping": "személyes átvétel"}), [])

    def test_question_says_what_is_missing_and_where(self):
        item = self.item(["vinted", "jofogas"], product="fekete póló", kind="ruha", price=500)
        text = question_for(item, missing_needs(item, {}))
        self.assertTrue(text.startswith("A(z) „fekete póló” feltöltéséhez még kell: állapot; márka, méret, szín "
                                        "(Vinted); átvétel helye (Jófogás)."), text)
        self.assertIn("Alapadatok", text)
        self.assertTrue(text.endswith("Írd meg, és folytatom."))
        self.assertNotIn("Alapadatok", question_for(item, missing_needs(item, {"city": "Pécs"})))


# --- az adatok a leírásból -----------------------------------------------------------------

class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        self.store = ItemStore(Path(self.tmp.name) / "eladas")
        self.extractor = ListingExtractor(OllamaClient(cfg.llm), self.store)

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def test_one_call_reads_the_listing_from_your_words(self):
        item = self.store.create(targets=["vinted"], prompt="rakd fel az alábbi Zara L-es méretű pólót mondjuk 500Ft-ért")
        self.store.add_photo(item.id, jpeg())
        self.fake.chat_script = [extracted(product="póló", kind="ruha", title="Zara póló, L", brand="Zara",
                                           size="L", price=500, search_query="Zara póló")]
        item = self.extractor.extract(item.id)
        self.assertEqual((item.product, item.kind, item.brand, item.size, item.price, item.condition),
                         ("póló", "ruha", "Zara", "L", 500, ""))
        [body] = self.fake.chat_bodies()
        self.assertEqual(body["format"]["type"], "object", "kötött szerkezetű (JSON) választ kér")
        self.assertIn("condition", body["format"]["properties"])
        self.assertFalse(body["stream"])
        self.assertTrue(all("images" not in m for m in body["messages"]), "a fotókat nem nézi (gyorsabb)")
        self.assertIn("Zara L-es méretű pólót", body["messages"][1]["content"])

    def test_keeps_what_the_model_left_empty_and_what_you_edited(self):
        item = self.store.create(prompt="Zara póló, 500 Ft")
        self.store.update(item.id, brand="Zara", size="M")
        self.store.update(item.id, user=True, title="Saját cím")
        self.fake.chat_script = [extracted(product="póló", title="Zara póló", size="", price="sok",
                                           condition="nagyon jo")]
        item = self.extractor.extract(item.id)
        self.assertEqual((item.title, item.size, item.brand, item.price, item.condition),
                         ("Saját cím", "M", "Zara", None, "Nagyon jó"))

    def test_needs_a_description(self):
        item = self.store.create()
        with self.assertRaisesRegex(ItemError, "Írd le"):
            self.extractor.extract(item.id)

    def test_parse_json_object(self):
        self.assertEqual(parse_json_object('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(parse_json_object('Íme: {"a": {"b": 2}} remélem jó'), {"a": {"b": 2}})
        self.assertEqual(parse_json_object('<think>hmm</think>{"a": 3}'), {"a": 3})
        for bad in ("nem json", "[1, 2]", ""):
            with self.assertRaises(OllamaError):
                parse_json_object(bad)


# --- árellenőrzés ---------------------------------------------------------------------------

class PriceResearchTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        self.llm = OllamaClient(cfg.llm)
        self.store = ItemStore(Path(self.tmp.name) / "eladas")

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def research(self, results_by_site: dict, judgement: dict | None, only=None):
        class FakeBrowser:
            def __init__(self):
                self.urls: list[str] = []

            def extract_listings(self, url):
                self.urls.append(url)
                for key, value in results_by_site.items():
                    if key in url:
                        if isinstance(value, Exception):
                            raise value
                        return value
                return []

        browser = FakeBrowser()
        if judgement is not None:
            self.fake.chat_script = [reply(json.dumps(judgement))]
        item = self.store.create(price=5000)
        self.store.update(item.id, title="Zara fekete blézer, M", brand="Zara", size="M", search_query="Zara blézer")
        no_search = Platform("helyi", "Helyi", "", "", "", (), "")
        sites = [selling.PLATFORMS["vinted"], selling.PLATFORMS["jofogas"], no_search]
        item = PriceResearcher(self.llm, self.store, lambda: browser, sites).research(item.id, only=only)
        return item, browser

    def test_picks_similar_listings_and_suggests(self):
        vinted = [{"text": "Zara blézer M", "price": 4000, "url": "https://www.vinted.hu/items/1"},
                  {"text": "Zara blézer S", "price": 6000, "url": "https://www.vinted.hu/items/2"},
                  {"text": "Zara táska", "price": 9000, "url": "https://www.vinted.hu/items/3"}]
        jofogas = [{"text": "Zara női blézer", "price": 3000, "url": "https://www.jofogas.hu/x_1.htm"}]
        item, browser = self.research({"vinted": vinted, "jofogas": jofogas},
                                      {"similar": [1, 2, 4, 99, 1], "suggested_price": 4480, "reason": "Jó áron."})
        r = item.research
        self.assertEqual(len(browser.urls), 2, "ahol nincs keresés, oda nem megy")
        self.assertIn("search_text=Zara%20bl%C3%A9zer", browser.urls[0])
        self.assertEqual([s["price"] for s in r["similar"]], [4000, 6000, 3000], "a táska nem hasonló")
        self.assertEqual((r["min"], r["median"], r["max"], r["suggested"]), (3000, 4000, 6000, 4500))
        self.assertIn("4 500 Ft", research_summary(r))

    def test_only_on_the_chosen_sites(self):
        _, browser = self.research({}, None, only=["jofogas"])
        self.assertEqual(len(browser.urls), 1)
        self.assertIn("jofogas.hu", browser.urls[0])

    def test_unrealistic_suggestion_and_errors(self):
        vinted = [{"text": "Zara blézer M", "price": 4000, "url": "https://www.vinted.hu/items/1"}]
        item, _ = self.research({"vinted": vinted, "jofogas": BrowserError("ellenőrzés kell")},
                                {"similar": [1], "suggested_price": 90000, "reason": ""})
        self.assertEqual(item.research["suggested"], 4000, "irreális javaslat helyett a középérték")
        self.assertIn("Jófogás: ellenőrzés kell", item.research["errors"])

    def test_without_results(self):
        item, _ = self.research({}, None)
        self.assertEqual(item.research["count"], 0)
        self.assertEqual(len(item.research["errors"]), 2)
        self.assertIn("nem találtam", research_summary(item.research))

    def blazer(self, **values) -> Item:
        item = self.store.create(price=5000)
        values = {"title": "Zara fekete blézer, M", "product": "blézer", "brand": "Zara", "size": "M",
                  "search_query": "Zara blézer", **values}
        return self.store.update(item.id, **values)

    def test_prefilter_drops_noise_before_the_model(self):
        item = self.blazer()
        self.store.mark(item.id, "vinted", POSTED, url="https://www.vinted.hu/items/4455667-zara-blezer?ref=x")
        item = self.store.get(item.id)
        rows = [{"text": "Zara blézer M, fekete", "price": 4000, "url": "https://www.vinted.hu/items/1"},
                {"text": "Keresek Zara blézert M-es méretben", "price": 3000, "url": "https://jofogas.hu/k_1.htm"},
                {"text": "Zara blézer csere is érdekel", "price": 4200, "url": "https://jofogas.hu/k_2.htm"},
                {"text": "Eladó vagy csere: Zara blézer S", "price": 4100, "url": "https://jofogas.hu/k_3.htm"},
                {"text": "Zara blézer csomag (3 db)", "price": 9000, "url": "https://www.vinted.hu/items/2"},
                {"text": "Zara blézer x2", "price": 7000, "url": "https://www.vinted.hu/items/3"},
                {"text": "Zara blézer, hibás, alkatrésznek", "price": 800, "url": "https://www.vinted.hu/items/4"},
                {"text": "Zara blézer, nem hibás, nem törött", "price": 4300, "url": "https://www.vinted.hu/items/5"},
                {"text": "Mango kabát", "price": 6000, "url": "https://www.vinted.hu/items/6"},
                {"text": "A saját hirdetésem: Zara blézer", "price": 5000,
                 "url": "https://vinted.hu/items/4455667-zara-fekete-blezer"},
                {"text": "Zara blézer M, fekete", "price": 4050, "url": "https://jofogas.hu/ugyanaz_7.htm"},
                {"text": "Zara blézert adok, M", "price": 4600, "url": "https://www.vinted.hu/items/8"}]
        kept, dropped = selling.prefilter(item, rows)
        self.assertEqual([r["price"] for r in kept], [4000, 4100, 4300, 4600])
        self.assertEqual(dropped, {"kereső": 2, "csomag": 2, "alkatrész": 1, "más termék": 1, "saját": 1,
                                   "ismétlődő": 1})
        broken = self.blazer(condition="Hibás")
        kept, _ = selling.prefilter(broken, rows)
        self.assertIn(800, [r["price"] for r in kept], "ha a te tárgyad is hibás, a hibás a hasonló")
        gpu = self.store.update(self.blazer().id, kind="elektronika", title="RTX 3080 videókártya", product="RTX 3080",
                                brand="", size="", search_query="rtx 3080")
        kept, _ = selling.prefilter(gpu, [{"text": "RTX 3080, 2 db ventilátor, PCIe x4", "price": 1, "url": ""}])
        self.assertEqual(len(kept), 1, "elektronikánál a darabszám a leírás része, nem csomag")

    def test_the_judge_gets_at_most_25_best_rows(self):
        rows = [{"text": f"Zara blézer {'M fekete' if n % 2 else 'kék'} modell{n}", "price": 4000 + n,
                 "url": f"https://www.vinted.hu/items/{n}"} for n in range(40)]
        item, _ = self.research({"vinted": rows[:20], "jofogas": rows[20:]},
                                {"similar": [1, 2, 3], "suggested_price": 4000, "reason": ""})
        prompt = self.fake.chat_bodies()[-1]["messages"][-1]["content"]
        lines = [line for line in prompt.splitlines() if re.match(r"\d+\. \[", line)]
        self.assertEqual(len(lines), 25)
        self.assertEqual(sum("M fekete" in line for line in lines), 20, "a legjobban egyezők maradnak")
        self.assertEqual((item.research["count"], item.research["kept"]), (40, 25))
        self.assertEqual(item.research["dropped"], {"kevésbé illő": 15})

    def test_nothing_similar_means_no_model_call(self):
        rows = [{"text": "Mango kabát", "price": 6000, "url": "https://www.vinted.hu/items/6"}]
        item, _ = self.research({"vinted": rows}, None)
        self.assertEqual(self.fake.chat_bodies(), [])
        self.assertEqual((item.research["count"], item.research["kept"], item.research["similar"]), (1, 0, []))
        self.assertIn("nem volt a tárgyadhoz hasonló", item.research["reason"])

    def test_outliers_are_trimmed_and_the_suggestion_stays_in_the_band(self):
        prices = [4000, 4200, 4500, 4800, 5000, 50000, 100]
        rows = [{"text": f"Zara blézer modell{n}", "price": p, "url": f"https://www.vinted.hu/items/{n}"}
                for n, p in enumerate(prices)]
        item, _ = self.research({"vinted": rows}, {"similar": list(range(1, 8)), "suggested_price": 5500,
                                                   "reason": "Ennyiért mennek."})
        r = item.research
        self.assertEqual([s["price"] for s in r["similar"]], [4000, 4200, 4500, 4800, 5000], "a tízszeres kiesik")
        self.assertEqual((r["min"], r["p25"], r["median"], r["p75"], r["max"]), (4000, 4200, 4500, 4800, 5000))
        self.assertEqual((r["n"], r["outliers"], r["confidence"]), (5, 2, "megbízható"))
        self.assertEqual(r["suggested"], 4800, "a javaslat a szokásos sávban marad")
        self.assertIn("többnyire 4 200 Ft és 4 800 Ft között", research_summary(r))

    def test_auctions_are_tagged_and_left_out_when_there_are_enough_fixed_prices(self):
        rows = [{"text": f"Zara blézer modell{n}", "price": 4000 + 100 * n, "url": f"https://www.vatera.hu/{n}"}
                for n in range(3)]
        rows.append({"text": "Zara blézer, licit", "price": 500, "url": "https://www.vatera.hu/9", "auction": True})
        item, _ = self.research({"vinted": rows}, {"similar": [1, 2, 3, 4], "suggested_price": 4100, "reason": ""})
        prompt = self.fake.chat_bodies()[-1]["messages"][-1]["content"]
        self.assertIn("500 Ft (licit: a mostani ajánlat, nem végleges ár)", prompt)
        self.assertEqual([s["price"] for s in item.research["similar"]], [4000, 4100, 4200])

    def test_stats_are_robust(self):
        stats = selling.price_stats([5000, 5000, 5000, 5000, 5500])
        self.assertEqual((stats["n"], stats["max"]), (5, 5500), "sok egyforma árnál a kicsit eltérő is marad")
        wide = selling.price_stats([1000, 3000, 6000, 9000, 12000])
        self.assertEqual(wide["confidence"], "bizonytalan", "nagyon szóró árak")
        self.assertEqual(selling.price_stats([4000])["p25"], 4000)
        self.assertEqual(selling.price_stats([]), {})

    def test_research_gets_old_after_a_week(self):
        item = self.store.set_research(self.blazer().id, {"at": time.time() - 8 * 86400, "query": "Zara blézer",
                                                          "similar": []})
        self.assertFalse(selling.research_fresh(item))
        self.assertTrue(item.public()["research_stale"])
        self.assertIn("8 napos, elavult", research_summary(item.research))
        item = self.store.set_research(item.id, {"at": time.time(), "query": "Zara blézer", "similar": []})
        self.assertTrue(selling.research_fresh(item))
        self.assertFalse(item.public()["research_stale"])


class PriceVerdictTests(unittest.TestCase):
    def item(self, price, prices) -> Item:
        research = {"at": 1, "similar": [{"site": "Vinted", "text": "x", "price": p, "url": ""} for p in prices]}
        if prices:
            middle = statistics.median(prices)
            research.update({"min": min(prices), "max": max(prices), "median": int(middle),
                             "suggested": round_price(middle)})
        return Item(id="a1b2c3d4", created=0, updated=0, price=price, research=research)

    def test_too_expensive_pauses_with_a_suggestion(self):
        verdict = price_verdict(self.item(9000, [4000, 4500, 5000]))
        self.assertTrue(verdict.pause)
        self.assertEqual(verdict.suggested, 4500)
        self.assertIn("Maradjon a te árad, vagy legyen 4 500 Ft?", verdict.text)

    def test_fine_cheap_or_too_little_to_compare(self):
        fine = price_verdict(self.item(5000, [4000, 4500, 5000]))
        self.assertFalse(fine.pause)
        self.assertIn("rendben", fine.text)
        cheap = price_verdict(self.item(2000, [4000, 4500, 5000]))
        self.assertFalse(cheap.pause)
        self.assertIn("többet is kérhetnél", cheap.text)
        few = price_verdict(self.item(9000, [4000, 4500]))
        self.assertFalse(few.pause, "kevés adat alapján nem állítja meg")
        self.assertIn("marad az árad (9 000 Ft)", few.text)
        self.assertIn("Nem találtam", price_verdict(self.item(9000, [])).text)
        self.assertEqual(Item(id="a1b2c3d4", created=0, updated=0).public()["verdict"], "", "kutatás nélkül nincs")
        self.assertIn("rendben", self.item(5000, [4000, 4500, 5000]).public()["verdict"])

    def test_typical_band_condition_and_uncertainty(self):
        wide = self.item(6500, [2000, 3000, 5000, 7000, 9000, 10000])
        verdict = price_verdict(wide)
        self.assertFalse(verdict.pause, "a szokásos sávon (p25–p75) belül nem állít meg")
        self.assertIn("többnyire", verdict.text)
        self.assertIn("csak tájékoztató", verdict.text, "nagyon szórnak az árak")
        new = self.item(5000, [4000, 4500, 5000, 5500, 6000])
        new.condition = "Új, címkével"
        self.assertIn("Mivel új, a sáv felső része (kb. 5 500 Ft) is reális.", price_verdict(new).text)
        worn = self.item(9000, [4000, 4500, 5000, 5500, 6000])
        worn.condition = "Elfogadható"
        verdict = price_verdict(worn)
        self.assertTrue(verdict.pause)
        self.assertIn("inkább a sáv alsó része (kb. 4 500 Ft)", verdict.text)
        self.assertTrue(verdict.text.endswith("Maradjon a te árad, vagy legyen 5 000 Ft?"), verdict.text)

    def test_spoken_or_typed_price_answers(self):
        cases = {"Maradjon": "keep", "maradjon az enyém": "keep", "nem, jó így": "keep", "nem kell": "keep",
                 "Legyen a javasolt": "suggested", "oké, mehet": "suggested", "vidd lejjebb": "suggested",
                 "Legyen 4000": 4000, "3 500 Ft": 3500, "120 ezer": 120000,
                 "ne 150 ezer legyen, hanem 120 ezer": 120000, "hmm": None, "igen": None,
                 # számszavak (a Whisper gyakran betűvel írja), tizedes ezrek
                 "legyen ötezer": 5000, "legyen négyezer-ötszáz": 4500, "legyen 4,5 ezer": 4500,
                 "legyen 3 és fél ezer": 3500, "4.5k": 4500, "másfél ezer": 1500, "hát legyen ötezer": 5000,
                 # a tagadás csak arra vonatkozik, ami utána jön
                 "nem, legyen a javasolt": "suggested", "ne maradjon, legyen a javasolt": "suggested",
                 "ne a javasolt legyen": "keep", "ne legyen az ajánlott": "keep", "legyen a tiéd": "suggested",
                 # bizonytalan: inkább újra kérdez
                 "nem tudom": None, "mindegy": None, "hmm, nem tudom": None, "maradjon, vagy legyen a javasolt": None,
                 # a számszó máshol nem ár
                 "egy kicsit olcsóbb": "suggested", "hát legyen": "suggested"}
        for text, expected in cases.items():
            self.assertEqual(interpret_price_reply(text), expected, text)

    def test_answers_to_a_confirmation_question(self):
        cases = {"igen": "confirmed", "igen, pontosan": "confirmed", "oké, mehet": "confirmed", "nem": None,
                 "nem, 3500": 3500, "nem jó": None,
                 "maradjon": "keep", "hmm": None, "legyen a javasolt": "suggested"}
        for text, expected in cases.items():
            self.assertEqual(interpret_price_reply(text, confirming=True), expected, text)

    def test_hungarian_amounts(self):
        cases = {"ötezer": 5000, "négyezer ötszáz": 4500, "kétszázötvenezer": 250000, "tizenkétezer": 12000,
                 "huszonötezer": 25000, "egymillió kétszázezer": 1_200_000, "fél millió": 500_000,
                 "félmillió": 500_000, "két és fél millió": 2_500_000, "1,5 millió": 1_500_000, "5k": 5000,
                 "5e": 5000, "10.000": 10000, "4.500 Ft": 4500, "ötezerre": 5000, "ezret": 1000, "százhúsz": 120,
                 "öt forint": 5, "legyen negyven": 40, "vidd fel ötezerre": 5000, "5000-re": 5000,
                 "egy kicsit olcsóbb": None, "hát legyen": None, "egyszer hordott": None, "hét": None,
                 "fel": None, "5000 öt": 5000}
        for text, expected in cases.items():
            self.assertEqual(parse_hu_amount(text), expected, text)
        self.assertEqual(hu_amounts("ne 150 ezer legyen, hanem 120 ezer"), [150000, 120000])
        self.assertEqual(hu_amounts("ötezer, nem, hatezer"), [5000, 6000])


# --- a Munka menü asztala (böngésző nélkül) ---------------------------------------------------

class WorkDeskTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        self.cfg.browser.enabled = False
        self.box = Toolbox(self.cfg)
        self.desk = WorkDesk(self.cfg, OllamaClient(self.cfg.llm), self.box)
        self.store = self.desk.store
        self.steps: list[str] = []

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def item(self, targets, prompt="Zara L-es fekete póló, 500 Ft", **values) -> Item:
        item = self.store.create(targets=targets, prompt=prompt, **values)
        return self.store.add_photo(item.id, jpeg())

    def test_the_browser_counts_ordinals_only_while_recording(self):
        self.cfg.browser.enabled = True
        desk = WorkDesk(self.cfg, None, Toolbox(self.cfg))
        browser = desk.toolbox.browser
        self.assertFalse(browser.recording)
        desk.start_recording(self.item(["vinted"]), "vinted", "post", "https://www.vinted.hu/items/new")
        self.assertTrue(browser.recording, "felvétel közben kell, hányadik az elem a hasonlók között")
        desk.end_request()
        self.assertFalse(browser.recording, "visszajátszásnál és modell nélküli kitöltésnél nem")
        self.assertFalse(browser.running, "a böngészőt ettől nem indítja el")

    def test_describe_for_the_interface(self):
        info = self.desk.describe()
        self.assertEqual([p["name"] for p in info["platforms"]], ["Vinted", "Jófogás", "Vatera", "Hardverapró"])
        self.assertIn({"label": "márka", "sites": ["vinted"], "clothing_only": True, "setting": ""}, info["needs"])
        self.assertEqual(info["settings"], {"city": "", "shipping": "", "parcel_size": ""})
        self.assertFalse(info["browser"])
        self.assertFalse(info["research"])

    def test_asks_back_then_continues_with_your_answer(self):
        item = self.item(["vinted", "jofogas"])
        self.fake.chat_script = [extracted(**{**SHIRT, "condition": "", "location": ""})]
        step = self.desk.prepare(item.id, self.steps.append)
        self.assertEqual(step.kind, "ask")
        self.assertIn("még kell: állapot; átvétel helye (Jófogás).", step.text)
        self.assertEqual(self.store.get(item.id).pending, {"kind": "info", "question": step.text})
        self.desk.answer(item.id, "Nagyon jó állapotú, Budapesten veheted át.")
        item = self.store.get(item.id)
        self.assertEqual(item.prompt, "Zara L-es fekete póló, 500 Ft\nNagyon jó állapotú, Budapesten veheted át.")
        self.assertEqual(item.pending, {})
        self.fake.chat_script = [extracted(**SHIRT)]
        self.assertEqual(self.desk.prepare(item.id, self.steps.append).kind, "post")
        self.assertIn("Budapesten veheted át", self.fake.chat_bodies()[-1]["messages"][1]["content"],
                      "a válaszod a leírás része lett")
        self.assertEqual(self.steps, ["a leírásod értelmezése"] * 2)
        targeted = self.fake.chat_bodies()[-1]["format"]
        self.assertEqual(sorted(targeted["properties"]), ["condition", "location"], "csak amire rákérdezett")
        self.assertEqual(sorted(self.fake.chat_bodies()[0]["format"]["properties"]),
                         sorted([*selling.LISTING_FIELDS, "kind", "price"]))
        item = self.store.get(item.id)
        self.assertEqual((item.condition, item.location, item.title), ("Nagyon jó", "Budapest", SHIRT["title"]))
        self.assertEqual(self.desk.prepare(item.id, self.steps.append).kind, "post")
        self.assertEqual(len(self.fake.chat_bodies()), 2, "ugyanabból a leírásból nem olvassa ki újra")

    def test_extraction_runs_once_and_a_posted_listing_keeps_its_text(self):
        item = self.item(["vinted", "jofogas"])
        self.fake.chat_script = [extracted(**SHIRT)]
        self.assertEqual(self.desk.prepare(item.id, self.steps.append).kind, "post")
        self.assertEqual(self.desk.prepare(item.id, self.steps.append).kind, "post")
        self.assertEqual((len(self.fake.chat_bodies()), self.steps), (1, ["a leírásod értelmezése"]))
        self.store.mark(item.id, "vinted", POSTED)
        title = self.store.get(item.id).title
        # a leírást átírod: újra kiolvassa, de a már fent lévő hirdetés szövege nem változik magától
        self.store.update(item.id, user=True, prompt="Zara L-es fekete póló, egyszer hordtam, 600 Ft")
        self.fake.chat_script = [extracted(**{**SHIRT, "title": "Más cím", "description": "Más szöveg.",
                                              "category": "Férfi > Pólók", "price": 600})]
        self.desk.prepare(item.id, self.steps.append)
        item = self.store.get(item.id)
        self.assertEqual(len(self.fake.chat_bodies()), 2)
        self.assertEqual((item.title, item.description, item.category, item.price),
                         (title, SHIRT["description"], SHIRT["category"], 600), "az ár viszont frissül")
        self.store.update(item.id, user=True, title="Saját cím")
        self.assertEqual(self.store.get(item.id).title, "Saját cím", "a kézi átírás számít")
        self.store.update(item.id, extracted_sig="régi")         # pl. új kiolvasó-változat után
        self.assertFalse(selling.ListingExtractor.up_to_date(self.store.get(item.id)))

    def test_needs_sites_and_photos(self):
        bare = self.store.create(prompt="póló")
        with self.assertRaisesRegex(ItemError, "Pipáld be"):
            self.desk.prepare(bare.id, self.steps.append)
        self.store.update(bare.id, targets=["vinted"])
        with self.assertRaisesRegex(ItemError, "nincs fotó"):
            self.desk.prepare(bare.id, self.steps.append)

    def test_settings_fill_city_shipping_and_parcel_size(self):
        saved = self.desk.save_settings({"city": "  Budapest   XI. ", "shipping": "Foxpost 1290 Ft",
                                         "parcel_size": "közepes", "valami": 1})
        self.assertEqual(saved, {"city": "Budapest XI.", "shipping": "Foxpost 1290 Ft", "parcel_size": "közepes"})
        with self.assertRaisesRegex(ItemError, "csomagméret"):
            self.desk.save_settings({"parcel_size": "óriási"})
        item = self.item(["hardverapro", "vinted"], prompt="RTX 3060 Ti, hibátlan, 90 ezer")
        self.fake.chat_script = [extracted(product="RTX 3060 Ti videókártya", kind="elektronika",
                                           title="RTX 3060 Ti", condition="Nagyon jó", price=90000)]
        self.assertEqual(self.desk.prepare(item.id, self.steps.append).kind, "post",
                         "a várost és a szállítást az Alapadatokból veszi")
        block = self.desk.listing_block(self.store.get(item.id), "vinted")
        for expected in ("Ár: 90000 (forint)", "Átvétel helye (város): Budapest XI.", "Szállítás: Foxpost 1290 Ft",
                         "Csomagméret: közepes", "Fotók: 1 db"):
            self.assertIn(expected, block)
        self.assertNotIn("Csomagméret", self.desk.listing_block(self.store.get(item.id), "hardverapro"))
        self.assertEqual(WorkDesk(self.cfg, None, Toolbox(self.cfg)).settings["city"], "Budapest XI.",
                         "újraindítás után is megmarad")

    def test_price_check_is_only_a_recommendation(self):
        class FakeResearcher:
            def __init__(self, store):
                self.store = store
                self.calls = 0
                self.only = None

            def research(self, item_id, progress=None, only=None):
                self.calls += 1
                self.only = only
                similar = [{"site": "Vinted", "text": "Zara blézer", "price": p, "url": ""} for p in (4000, 4500, 5000)]
                return self.store.set_research(item_id, {"at": time.time(), "query": "Zara blézer", "count": 3,
                                                         "errors": [],
                                                         "similar": similar, "min": 4000, "max": 5000,
                                                         "median": 4500, "suggested": 4500})

        researcher = self.desk.researcher = FakeResearcher(self.store)
        item = self.item(["vinted"], prompt="Zara blézer, M, nagyon jó, 9000 Ft", research_wanted=True)
        blazer = {**SHIRT, "product": "blézer", "title": "Zara blézer, M", "size": "M", "price": 9000}
        self.fake.chat_script = [extracted(**blazer)]
        step = self.desk.prepare(item.id, self.steps.append)
        self.assertEqual(step.kind, "price")
        self.assertEqual(researcher.only, ["vinted"], "a kiválasztott oldalakon néz körül")
        self.assertEqual(self.store.get(item.id).pending["suggested"], 4500)
        self.assertEqual(self.store.get(item.id).status, "árdöntés")
        self.desk.decide_price(item.id, "keep")
        self.assertEqual(self.store.get(item.id).price, 9000, "ha neked így jó, marad az árad")
        step = self.desk.prepare(item.id, self.steps.append)
        self.assertEqual((step.kind, researcher.calls), ("post", 1), "egyszer néz utána, utána nem kérdez újra")
        self.assertEqual(len(self.fake.chat_bodies()), 1, "az árdöntés után nem olvassa ki újra a leírást")
        self.store.set_pending(item.id, {"kind": "price", "question": "?", "suggested": 4500})
        self.assertEqual(self.desk.decide_price(item.id, "suggested").price, 4500)
        self.assertEqual(self.desk.decide_price(item.id, 6000).price, 6000)
        self.assertIn("price", self.store.get(item.id).edited, "Jarvis később nem írja vissza")
        old = dict(self.store.get(item.id).research, at=time.time() - 8 * 86400)
        self.store.set_research(item.id, old)
        self.fake.chat_script = [extracted(**blazer)]
        step = self.desk.prepare(item.id, self.steps.append)
        self.assertEqual(researcher.calls, 2, "a hétnél régebbi árkutatást az árazás előtt frissíti")
        self.assertEqual(step.kind, "price", "a friss kutatás szerint a 6000 Ft is sok: újra megkérdezi")

    def test_odd_price_is_asked_back_before_it_is_set(self):
        item = self.item(["vinted"])
        self.store.update(item.id, price=9000)
        self.store.set_pending(item.id, {"kind": "price", "question": "Marad?", "suggested": 4500})
        item = self.desk.decide_price(item.id, 3)       # a „3 és fél ezer” félrehallva
        self.assertEqual((item.price, item.pending["kind"], item.pending["confirm"]), (9000, "price", 3))
        self.assertTrue(item.pending["question"].startswith("3 Ft-ra gondoltál?"), item.pending["question"])
        self.assertEqual(item.pending["suggested"], 4500, "a javasolt ár megmarad")
        self.assertEqual(item.status, "árdöntés")
        item = self.desk.decide_price(item.id, "confirmed")
        self.assertEqual((item.price, item.pending), (3, {}), "ha igent mond, az számít")

        self.store.update(item.id, user=True, price=9000)
        self.store.set_pending(item.id, {"kind": "price", "question": "Marad?", "suggested": 4500})
        self.assertEqual(self.desk.decide_price(item.id, 40000).pending["confirm"], 40000, "háromszorosnál több")
        self.assertEqual(self.desk.decide_price(item.id, 40000).price, 40000, "ugyanaz az összeg újra: igen")
        self.store.set_pending(item.id, {"kind": "price", "question": "Marad?", "suggested": 4500})
        self.assertEqual(self.desk.decide_price(item.id, 2000).price, 2000, "a javasolthoz képest nem furcsa")
        self.assertEqual(self.desk.decide_price(item.id, "keep").pending, {})

    def test_failed_price_check_does_not_block_posting(self):
        class Broken:
            def research(self, *args, **kwargs):
                raise ItemError("nincs mit keresni")

        self.desk.researcher = Broken()
        item = self.item(["vinted"], research_wanted=True)
        self.fake.chat_script = [extracted(**SHIRT)]
        step = self.desk.prepare(item.id, self.steps.append)
        self.assertEqual(step.kind, "post")
        self.assertIn("marad az árad", step.text)

    def test_unfinished_post_is_marked_with_the_reason(self):
        item = self.item(["vinted", "vatera"])
        self.store.update(item.id, title="Zara póló", price=500)
        self.desk.current = (item.id, "vinted")
        self.assertEqual(self.desk.confirm_context(), "Vinted: Zara póló – 500 Ft")
        self.store.set_busy(item.id, "Feltöltés: Vinted…")
        self.assertFalse(self.desk.finish_post(item.id, "vinted", "Be kell jelentkezned a Vintedre."))
        entry = self.store.get(item.id).platforms["vinted"]
        self.assertEqual((entry["status"], entry["note"]), (FAILED, "Be kell jelentkezned a Vintedre."))
        self.assertEqual((self.store.get(item.id).busy, self.desk.current), ("", None))
        self.desk.approved = True     # a közzétételt jóváhagytad, csak a megjelölés maradt el
        self.desk.finish_post(item.id, "vatera", "")
        self.assertIn("Lehet, hogy mégis fent van", self.store.get(item.id).platforms["vatera"]["note"])
        self.assertFalse(self.desk.approved)
        self.store.mark(item.id, "vinted", POSTED)
        self.assertTrue(self.desk.finish_post(item.id, "vinted"))
        self.assertEqual(self.desk.targets_left(item.id), ["vatera"])

    def test_each_site_gets_at_most_its_photo_limit(self):
        self.assertEqual({key: site.max_photos for key, site in self.desk.platforms.items()},
                         {"vinted": 20, "jofogas": 10, "vatera": 8, "hardverapro": 10})
        item = self.item(["vatera", "vinted"])
        for _ in range(11):
            self.store.add_photo(item.id, jpeg())
        item = self.store.reorder_photos(item.id, ["12.jpg"])        # a borítókép az utolsónak feltöltött
        uploads = []

        class FakeBrowser:
            host = "www.vatera.hu"

            def current_host(self):
                return self.host

            def upload_files(self, paths, element=None):
                uploads.append(paths)
                return f"Feltöltöttem {len(paths)} fotót."

        browser = FakeBrowser()
        self.desk.toolbox = mock.Mock(browser=browser)
        self.desk.current = (item.id, "vatera")
        self.assertEqual(self.desk.tool_upload_photos(), "Feltöltöttem 8 fotót.")
        self.assertEqual(Path(uploads[0][0]).name, "12.jpg", "a borítókép az első")
        self.assertIn("Fotók: 8 db (a(z) Vatera legfeljebb ennyit vesz fel, ezért 12 helyett az első 8;",
                      self.desk.listing_block(item, "vatera"))
        self.assertIn("Fotók: 12 db (a browser_upload_photos", self.desk.listing_block(item, "vinted"))
        self.assertEqual(len(self.desk.photos_for(item, "vinted")), 12)
        browser.host = "www.jofogas.hu"          # a böngésző más oldalon áll: annak a limitje
        self.desk.current = None
        self.desk.tool_upload_photos(item=item.id)
        self.assertEqual(len(uploads[-1]), 10)

    def test_publish_gap_is_waited_right_before_publishing(self):
        item = self.item(["vinted", "jofogas"])
        self.cfg.work.publish_gap_minutes = 2.0
        clock = FakeClock()
        with mock.patch("jarvis_core.work.time", clock):
            self.desk.note_publish("vinted")
            due = self.desk._next_allowed["vinted"]
            self.desk._before_publish("Feltöltés", "https://www.vinted.hu/items/new")
            self.assertEqual(clock.now, 1_000_000.0, "feltöltésen kívül (pl. a Beszélgetésben) nem vár")
            self.desk.current = (item.id, "vinted")
            self.desk.begin_trail()
            self.store.set_busy(item.id, "Feltöltés: Vinted…")
            self.desk._before_publish("Feltöltés", "https://www.vinted.hu/items/new")
            self.assertGreaterEqual(clock.now, due, "a közzététel gombja előtt kivárja")
            self.assertEqual(self.store.get(item.id).busy, "Feltöltés: Vinted…", "a visszaszámlálás után")
            self.desk._on_browser_action({"do": "click", "risky": True, "el": {}, "url": ""})
            self.assertGreater(self.desk.gap_remaining("vinted"), 0, "a következő közzététel innen számol")
            waited = clock.now
            self.desk._before_publish("Igen, feltöltöm", "https://www.vinted.hu/items/new")
            self.assertEqual(clock.now, waited, "a megerősítő gombnál ugyanebben a menetben már nem vár")
            self.desk.current = (item.id, "jofogas")
            self.desk.begin_trail()
            self.desk._before_publish("Hirdetés feladása", "https://www.jofogas.hu/")
            self.assertEqual(clock.now, waited, "másik oldalon nincs szünet")

    def test_a_failed_delist_does_not_reset_the_listing_age(self):
        item = self.item(["vinted"])
        self.store.mark(item.id, "vinted", POSTED)
        self.store.get(item.id).platforms["vinted"]["posted_at"] = time.time() - 20 * 86400
        self.assertFalse(self.desk.finish_delist(item.id, "vinted", "nem találtam a Törlés gombot."))
        entry = self.store.get(item.id).platforms["vinted"]
        self.assertEqual(entry["status"], POSTED)
        self.assertIn("A levétel nem sikerült", entry["note"])
        self.assertEqual([i.id for i in self.desk.stale_items(14)], [item.id], "20 napja fent van, nem 0")
        self.assertEqual(self.store.get(item.id).public()["days_online"], 20)

    def test_tools_for_the_work_chat(self):
        self.assertIn("Adás/Vétel fülön", self.box.call("item_list", {}))
        item = self.item(["vinted"])
        self.store.update(item.id, title="Kék pulóver", price=2000)
        self.assertIn(f"{item.id} | Kék pulóver | 2 000 Ft | vázlat | Vinted: még nincs fent",
                      self.box.call("item_list", {}))
        self.assertIn("Módosítva (Kék pulóver): ár", self.box.call("item_update", {"item": "pulóver", "price": 1500}))
        self.assertIn("price", self.store.get(item.id).edited)
        details = self.box.call("item_get", {"item": item.id})
        self.assertIn("Az eladó leírása: Zara L-es fekete póló, 500 Ft", details)
        self.assertIn("Árkutatás: még nem volt.", details)
        self.store.mark(item.id, "vinted", POSTED)
        self.assertIn("Ne felejtsd el levenni innen: Vinted",
                      self.box.call("item_mark", {"item": item.id, "status": "eladva"}))
        self.assertFalse(self.box.has("item_post"), "böngésző nélkül nincs feltöltés")
        self.assertFalse(self.box.has("item_research"))
        self.assertFalse(self.box.has("item_write"), "a hirdetést már nem a fotókból írja")

    def test_price_change_after_posting_is_answered_honestly(self):
        item = self.item(["vinted", "hardverapro"])
        self.store.update(item.id, title="Levi's farmer", price=9000)
        self.store.mark(item.id, "vinted", POSTED, url="https://www.vinted.hu/items/1234567-levis")
        self.store.mark(item.id, "hardverapro", POSTED, url="https://hardverapro.hu/apro/levis/friss.html")
        answer = self.box.call("item_update", {"item": item.id, "price": 8500})
        self.assertIn("Módosítva (Levi's farmer): ár. Csak itt írtam át; a Vinteden még 9 000 Ft, "
                      "a Hardveraprón még 9 000 Ft.", answer)
        self.assertIn("magamtól nem szerkesztem", answer)
        self.assertIn("Vinted: feltöltve (ott még 9 000 Ft)", self.box.call("item_list", {}))
        self.assertEqual(self.box.call("item_update", {"item": item.id, "description": "Alig hordott."}),
                         "Módosítva (Levi's farmer): leírás", "ha az ár nem változott, nincs mit mondani róla")
        self.store.set_check(item.id, "vinted", "sold")
        self.assertIn("Vinted: feltöltve (ott még 9 000 Ft) (a háttérellenőrzés szerint elkelt)",
                      self.box.call("item_get", {"item": item.id}))

    def test_acknowledgement_is_remembered(self):
        with self.assertRaises(ItemError):
            self.desk.acknowledge("ebay")
        self.desk.acknowledge("vinted")
        self.assertEqual(WorkDesk(self.cfg, None, Toolbox(self.cfg)).describe()["acknowledged"], ["vinted"])


# --- az adatmappa: a bejelentkezések és a tárgyak megmaradnak --------------------------------

class DataFolderTests(unittest.TestCase):
    def test_default_paths_are_outside_the_project(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}):
            home = Path(tmp) / "Jarvis"
            self.assertEqual(config.data_dir(), home)
            self.assertEqual(config.data_path("", "browser_profile"), home / "browser_profile")
            self.assertEqual(config.data_path("munka", "munka"), home / "munka", "a régi alapérték is ide mutat")
            self.assertEqual(config.data_path(str(Path(tmp) / "mashol"), "munka"), Path(tmp) / "mashol")

    def test_the_old_folder_is_moved_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, app_data = Path(tmp) / "projekt", Path(tmp) / "appdata"
            (project / "browser_profile" / "Default").mkdir(parents=True)
            (project / "browser_profile" / "Default" / "Cookies").write_text("süti", encoding="utf-8")
            logs: list[str] = []
            with mock.patch.object(config, "PROJECT_DIR", project), \
                    mock.patch.dict(os.environ, {"LOCALAPPDATA": str(app_data)}):
                config.adopt_legacy_data("browser_profile", "browser_profile", logs.append)
                moved = app_data / "Jarvis" / "browser_profile" / "Default" / "Cookies"
                self.assertEqual(moved.read_text(encoding="utf-8"), "süti")
                self.assertFalse((project / "browser_profile").exists())
                self.assertIn("áthelyeztem", logs[0])
                (project / "browser_profile").mkdir()          # ha már mindkettő megvan: nem nyúl hozzá
                config.adopt_legacy_data("", "browser_profile", logs.append)
                config.adopt_legacy_data(str(Path(tmp) / "sajat"), "browser_profile", logs.append)
                self.assertTrue((project / "browser_profile").exists())
                self.assertEqual(len(logs), 1)

    def test_retired_settings_do_not_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text('[work]\nvision_photos = 4\ndir = "munka"\n', encoding="utf-8")
            self.assertEqual(load_config(path).warnings, [])


# --- böngésző: árak kigyűjtése, fotófeltöltés, jóváhagyás -----------------------------------

PAGES = {
    "fooldal.html": """<!doctype html><html lang="hu"><head><meta charset="utf-8"><title>Főoldal</title></head>
<body><h1>Teszt piactér</h1><a href="/feladas.html">Hirdetésfeladás</a>
<a href="#" onclick="document.getElementById('x').textContent = 'kész'; return false">Feltöltés</a>
<p id="x">Jelentkezz be a hirdetéshez.</p></body></html>""",
    "katalogus.html": """<!doctype html><html lang="hu"><head><meta charset="utf-8"><title>Találatok</title></head>
<body><header><a href="/akcio">Ma minden 990 Ft</a></header><main><div class="grid">
<div class="item"><a href="/member/77">Eladó: Kata</a>
  <a href="/items/1-zara-blezer" title="Zara blézer, márka: Zara, méret: M, 4 500 Ft"><img alt=""></a>
  <div>Zara</div><div>M · Nagyon jó</div><div>4 500 Ft</div></div>
<div class="item"><a href="/items/2-hm-blezer"><span>H&amp;M blézer</span></a><p>S</p><p>2&nbsp;000 Ft</p></div>
<div class="item"><a href="/items/3-zara-kabat">Zara kabát</a><span>12 990 Ft</span></div>
<div class="item"><a href="/items/4-nincs-ar">Ár nélküli</a></div>
<div class="item"><a href="/items/5-levis-farmer">Levi's farmer</a><s>9 000 Ft</s> <b>6 500 Ft</b></div>
<div class="item"><a href="/items/6-mango-szoknya">Mango szoknya</a>
  <span style="text-decoration: line-through">3 000 Ft</span><span>1 800 Ft</span></div>
<div class="item"><a href="/items/7-regi-erme">Régi érme</a><span>Licit: 1 200 Ft</span>
  <span>Villámár: 5 000 Ft</span></div>
<div class="item"><a href="/items/8-regi-belyeg">Régi bélyeg</a><span>Jelenlegi ár: 300 Ft, 4 licit</span></div>
</div></main><footer><a href="/kapcsolat">Szállítás 0 Ft</a></footer></body></html>""",
    "feladas.html": """<!doctype html><html lang="hu"><head><meta charset="utf-8"><title>Hirdetésfeladás</title></head>
<body><h1>Új hirdetés</h1>
<button type="button" onclick="document.getElementById('files').click()">Fotók feltöltése</button>
<input type="file" id="files" accept="image/*" multiple style="display:none">
<ul id="list"></ul>
<label>Cím <input name="title" id="title"></label>
<label>Leírás <textarea name="description" id="desc"></textarea></label>
<label>Ár <input name="price" id="price"></label>
<button type="button" onclick="feltolt()">Feltöltés</button>
<p id="done"></p>
<script>
document.getElementById('files').addEventListener('change', (e) => {
  for (const f of e.target.files) {
    const li = document.createElement('li');
    li.textContent = 'Fotó: ' + f.name;
    document.getElementById('list').appendChild(li);
  }
});
function feltolt() {
  const n = document.querySelectorAll('#list li').length;
  document.getElementById('done').textContent = 'Hirdetésed feltöltve: ' +
    document.getElementById('title').value + ' | ' + document.getElementById('price').value + ' Ft | ' + n + ' fotó';
}
</script></body></html>""",
    "egyes.html": """<!doctype html><html lang="hu"><head><meta charset="utf-8"><title>Egyesével</title></head>
<body><label>Kép <input type="file" id="one" accept="image/jpeg"></label><ul id="list"></ul>
<script>
document.getElementById('one').addEventListener('change', (e) => {
  for (const f of e.target.files) {
    const li = document.createElement('li'); li.textContent = 'Fotó: ' + f.name;
    document.getElementById('list').appendChild(li);
  }
});
</script></body></html>""",
}


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class Pages:
    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        for name, content in PAGES.items():
            (Path(self.dir.name) / name).write_text(content, encoding="utf-8")
        handler = functools.partial(_QuietHandler, directory=self.dir.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/{path}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()


def local_platforms(pages: Pages) -> dict[str, Platform]:
    """A négy hirdetési oldal helyett helyi tesztoldalak (internet nélkül)."""
    names = {"vinted": "Vinted", "jofogas": "Jófogás", "vatera": "Vatera", "hardverapro": "Hardverapró"}
    return {key: Platform(key, name, pages.url("fooldal.html"), pages.url("feladas.html"),
                          pages.url("katalogus.html") + "?q={q}", ("127.0.0.1",), f"{name}: tippek a teszthez.")
            for key, name in names.items()}


def element_number(snapshot: str, label: str) -> int:
    # egy elem a saját sorában ([3] szövegmező "Cím"), vagy egy csoport sorában (Állapot (...): [7] Nagyon jó, [8] Jó)
    match = (re.search(rf'\[(\d+)\] [^\n"\[]*"{re.escape(label)}', snapshot)
             or re.search(rf'\[(\d+)\] "?{re.escape(label)}"?(?: ✓)?(?=,|$| \()', snapshot, re.M)
             or re.search(rf'\[(\d+)\] "?{re.escape(label)}[^,\n\[]*', snapshot))   # a felirat eleje („Nagyon jó állapotú”)
    if not match:
        raise AssertionError(f"nincs ilyen elem: {label}\n{snapshot}")
    return int(match.group(1))


class RiskyWordsTests(unittest.TestCase):
    def test_publish_buttons_need_confirmation(self):
        for label in ("Feltöltés", "Tölts fel", "Hirdetés feladása", "Közzététel", "Upload", "Termék meghirdetése"):
            self.assertTrue(is_risky(label), label)
        for label in ("Fotók feltöltése", "Képek hozzáadása", "Upload photos", "Tovább", "Méret"):
            self.assertFalse(is_risky(label), label)

    def test_a_link_to_the_form_is_not_publishing(self):
        self.assertFalse(is_risky("Hirdetésfeladás", link=True))
        self.assertTrue(is_risky("Törlés", link=True), "a többi kockázatos szó linken is számít")
        self.assertTrue(navigates({"kind": "link", "href": "/ai/form"}))
        for element in ({"kind": "link", "href": "#"}, {"kind": "link", "href": "javascript:void(0)"},
                        {"kind": "button", "href": "/x"}, {"kind": "link"}):
            self.assertFalse(navigates(element), element)


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class BrowserWorkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pages = Pages()
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = make_cfg(Path(cls.tmp.name))
        cfg.browser.headless = True
        cls.prompts: list[str] = []
        cls.browser = BrowserController(cfg.browser, allow_local=True,
                                        confirm=lambda p: cls.prompts.append(p) or True)
        cls.files = []
        for n in (1, 2):
            path = Path(cls.tmp.name) / f"0{n}.jpg"
            path.write_bytes(jpeg())
            cls.files.append(str(path))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pages.close()
        cls.tmp.cleanup()

    def test_extract_listings(self):
        found = self.browser.extract_listings(self.pages.url("katalogus.html"))
        self.assertEqual([r["price"] for r in found], [4500, 2000, 12990, 6500, 1800, 5000, 300],
                         "az áthúzott régi ár nem számít; a villámár az ár")
        self.assertTrue(found[0]["url"].endswith("/items/1-zara-blezer"), "a hirdetés linkje, nem az eladóé")
        self.assertIn("Zara blézer", found[0]["text"])
        self.assertIn("H&M blézer", found[1]["text"])
        self.assertNotIn("9 000", found[3]["text"], "a modell sem látja a régi árat")
        self.assertEqual([bool(r.get("auction")) for r in found], [False] * 6 + [True],
                         "csak a villámár nélküli licites kártya jelzett")

    def test_upload_with_the_photo_button(self):
        view = self.browser.open(self.pages.url("feladas.html"))
        button = element_number(view, "Fotók feltöltése")
        clicked = self.browser.click(button)
        self.assertIn("browser_upload_photos", clicked, "kattintásra a fájlválasztó helyett szól")
        view = self.browser.upload_files(self.files, button)
        self.assertIn("Feltöltöttem 2 fotót", view)
        self.assertIn("Fotó: 01.jpg", view)
        self.assertIn("Fotó: 02.jpg", view)

    def test_upload_without_element_and_single_file_input(self):
        self.browser.open(self.pages.url("feladas.html"))
        self.assertIn("Fotó: 02.jpg", self.browser.upload_files(self.files))
        self.browser.open(self.pages.url("egyes.html"))
        self.assertIn("Fotó: 02.jpg", self.browser.upload_files(self.files), "egyesével is megy")
        with self.assertRaises(BrowserError):
            self.browser.open(self.pages.url("katalogus.html"))
            self.browser.upload_files(self.files)

    def test_a_single_file_input_gets_the_next_photo_once_it_took_the_previous(self):
        self.browser.open(self.pages.url("egyes.html"))
        started = time.monotonic()
        view = self.browser.upload_files(self.files)
        self.assertLess(time.monotonic() - started, 1.2, "nincs fotónkénti fix várakozás (régen 0,6 mp)")
        self.assertIn("Fotó: 01.jpg", view)
        self.assertIn("Fotó: 02.jpg", view)

    def test_fill_several_fields_in_one_step(self):
        view = self.browser.open(self.pages.url("feladas.html"))
        view = self.browser.fill_fields([{"element": element_number(view, "Cím"), "text": "Kék pulóver"},
                                         {"element": str(element_number(view, "Ár")), "text": "1500"},
                                         {"element": element_number(view, "Leírás"), "text": "Puha, meleg."}])
        self.assertIn('Kitöltöttem: "Cím", "Ár", "Leírás".', view)
        self.assertIn('(érték: "Kék pulóver")', view)
        self.browser.click(element_number(view, "Feltöltés"))
        self.assertIn("Hirdetésed feltöltve: Kék pulóver | 1500 Ft | 0 fotó", self.browser.snapshot())
        for bad in ([], [{"text": "x"}], [{"element": 999, "text": "x"}], "szöveg"):
            with self.assertRaises(BrowserError, msg=str(bad)):
                self.browser.fill_fields(bad)

    def test_opening_the_form_needs_no_confirmation_but_publishing_does(self):
        view = self.browser.open(self.pages.url("fooldal.html"))
        before = len(self.prompts)
        view = self.browser.click(element_number(view, "Hirdetésfeladás"))
        self.assertEqual(len(self.prompts), before, "a hirdetésfeladás linkje csak megnyitja az űrlapot")
        self.assertIn("Új hirdetés", view)
        view = self.browser.open(self.pages.url("fooldal.html"))
        self.browser.click(element_number(view, "Feltöltés"))
        self.assertEqual(len(self.prompts), before + 1, "egy nem-link „Feltöltés” már közzététel lehet")
        self.assertIn("Feltöltés", self.prompts[-1])


# --- a teljes eladás: JarvisApp, hamis modell, helyi „hirdetési oldalak” --------------------

@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class SellFlowTests(unittest.TestCase):
    def setUp(self):
        self.pages = Pages()
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        cfg.browser.headless = True
        cfg.web.allow_local = True
        cfg.assistant.show_timings = False
        cfg.assistant.fillers = False
        cfg.work.platforms = ["vinted", "jofogas"]
        cfg.work.research_sites = ["vinted"]
        self.patch = mock.patch.dict(selling.PLATFORMS, local_platforms(self.pages), clear=True)
        self.patch.start()
        self.ui = RecordingUI()
        self.ui.confirm_answer = True
        self.app = JarvisApp(cfg, self.ui, use_voice=False, use_speech=False, use_wake_word=False)
        self.fake.vram_fit = lambda ctx: 1.0
        self.assertTrue(self.app.start())
        self.store = self.app.work.store

    def tearDown(self):
        self.app.toolbox.close()
        self.patch.stop()
        self.fake.close()
        self.pages.close()
        self.tmp.cleanup()

    def new_item(self, targets, prompt="Zara L-es fekete póló, 500 Ft", **values) -> Item:
        item = self.store.create(targets=targets, prompt=prompt, **values)
        return self.store.add_photo(item.id, jpeg())

    def ready_item(self, targets) -> Item:
        item = self.new_item(targets)
        return self.store.update(item.id, **SHIRT)

    @staticmethod
    def last_tool(body) -> str:
        return next(m["content"] for m in reversed(body["messages"]) if m["role"] == "tool")

    def notices(self) -> list[str]:
        return [e[1] for e in self.ui.of("notice")]

    def test_tools_are_separate_per_menu(self):
        chat = {t["function"]["name"] for t in self.app.toolbox.schemas(self.app.assistant.tool_names)}
        work = {t["function"]["name"] for t in self.app.toolbox.schemas(self.app.work_assistant.tool_names)}
        self.assertNotIn("item_post", chat)
        self.assertNotIn("open_app", work)
        self.assertTrue({"item_list", "item_post", "browser_upload_photos", "browser_click"} <= work)
        self.assertIn("Adás/Vétel", self.app.work_assistant.system_prompt)

    def test_modes_have_their_own_conversation(self):
        self.fake.chat_script = [reply("Szia!"), reply("Két tárgyad van.")]
        self.app.answer("Szia", mode="chat")
        self.app.respond("Hány tárgyam van?", mode="work")
        chat_body, work_body = self.fake.chat_bodies()
        self.assertNotIn("most a Munka menü", chat_body["messages"][0]["content"])
        self.assertIn("most a Munka menü", work_body["messages"][0]["content"])
        self.assertEqual(len(self.app.assistant.history), 2)
        self.assertEqual(len(self.app.work_assistant.history), 2)

    def test_sell_asks_back_then_posts_to_every_chosen_site(self):
        item = self.new_item(["vinted", "jofogas"])
        self.fake.chat_script = [extracted(**{**SHIRT, "condition": "", "location": ""})]
        self.app.run_work({"action": "sell", "ids": [item.id]})
        question = self.store.get(item.id).pending["question"]
        self.assertIn("még kell: állapot; átvétel helye (Jófogás).", question)
        self.assertEqual(self.notices()[-1], question, "a naplóban is kérdez (és felolvassa)")

        def upload(body):
            # az item_post lépést a Jarvis már elvégezte: a modell első üzenetében ott az űrlap és a hirdetés
            assert [m["role"] for m in body["messages"]] == ["system", "user"], body["messages"]
            opened = body["messages"][-1]["content"]
            for expected in ("Az item_post lépést már elvégeztem", "Tippek (Vinted): Vinted: tippek a teszthez.",
                             "Ár: 500 (forint)", "Átvétel helye (város): Budapest", "Csomagméret: kicsi",
                             "Márka: Zara", "Új hirdetés"):
                assert expected in opened, (expected, opened)
            return 200, tool_reply("browser_upload_photos", {})

        def fill(body):
            view = self.last_tool(body)
            return 200, tool_reply("browser_fill", {"fields": [
                {"element": element_number(view, "Cím"), "text": "Zara fekete póló, L"},
                {"element": element_number(view, "Ár"), "text": "500"}]})

        def publish(body):
            return 200, tool_reply("browser_click", {"element": element_number(self.last_tool(body), "Feltöltés")})

        def mark(body):
            done = self.last_tool(body)
            assert "Hirdetésed feltöltve: Zara fekete póló, L | 500 Ft | 1 fotó" in done, done
            return 200, tool_reply("item_mark", {"item": item.id, "status": "feltöltve", "platform": "vinted"})

        self.fake.chat_script = [
            extracted(**SHIRT),
            upload, fill, publish, mark, reply("Fent van a Vinteden."),
            reply("Be kell jelentkezned a Jófogásra, utána az Újra gombbal folytatom."),
        ]
        # a Munka menüben írt (vagy mondott) válasz a függő kérdésre megy
        self.app.respond("Nagyon jó állapotú, Budapesten lehet átvenni.", mode="work")
        item = self.store.get(item.id)
        self.assertEqual(self.fake.chat_script, [], "minden lépés lefutott")
        self.assertEqual(item.platforms["vinted"]["status"], POSTED)
        self.assertEqual((item.platforms["jofogas"]["status"], item.platforms["jofogas"]["note"]),
                         (FAILED, "Be kell jelentkezned a Jófogásra, utána az Újra gombbal folytatom."))
        self.assertEqual((item.status, item.pending, item.busy), ("részben fent", {}, ""))
        self.assertTrue(item.prompt.endswith("\nNagyon jó állapotú, Budapesten lehet átvenni."))
        confirms = [e[1] for e in self.ui.of("confirm")]
        self.assertEqual(len(confirms), 1, "oldalanként egy jóváhagyás: a közzététel")
        self.assertTrue(confirms[0].startswith("Vinted: Zara fekete póló, L – 500 Ft\n\n"), confirms[0])
        self.assertIn("Fent van: Vinted. Nem sikerült: Jófogás.", self.notices()[-1])
        infos = [e[1] for e in self.ui.of("info")]
        self.assertIn("Feltöltés: Zara fekete póló, L → Vinted", infos)
        self.assertEqual(self.ui.of("user")[0][1], "Nagyon jó állapotú, Budapesten lehet átvenni.")
        self.assertEqual(self.app.work.current, None)

        # „Feltöltés a hiányzó helyekre”: nem olvassa ki újra a leírást, és a Jófogásra ugyanaz a cím kerül
        calls = len(self.fake.chat_bodies())

        def same_title(body):
            first = body["messages"][1]["content"]
            assert f"Cím: {item.title}\n" in first, first
            return 200, tool_reply("item_mark", {"item": item.id, "status": "feltöltve", "platform": "jofogas"})

        self.fake.chat_script = [same_title, reply("Fent van a Jófogáson.")]
        self.app.run_work({"action": "sell", "ids": [item.id]})
        bodies = self.fake.chat_bodies()[calls:]
        self.assertEqual(len(bodies), 2, "se újabb kiolvasás, se külön item_post kör")
        self.assertEqual((self.store.get(item.id).title, self.store.get(item.id).status), (item.title, "fent"))

    def test_price_check_then_your_decision(self):
        item = self.new_item(["vinted"], prompt="Zara blézer, M, nagyon jó, 9000 Ft", research_wanted=True)
        blazer = {**SHIRT, "product": "blézer", "title": "Zara blézer, M", "size": "M", "price": 9000,
                  "search_query": "Zara blézer"}
        self.fake.chat_script = [extracted(**blazer),
                                 reply(json.dumps({"similar": [1, 2, 3], "suggested_price": 4500,
                                                   "reason": "Ennyiért mennek."}))]
        self.app.run_work({"action": "sell", "ids": [item.id]})
        item = self.store.get(item.id)
        self.assertEqual(item.pending["kind"], "price")
        self.assertEqual(item.research["median"], 4500)
        self.assertIn("Maradjon a te árad, vagy legyen 4 500 Ft?", self.notices()[-1])

        def check_price(body):
            first = body["messages"][-1]["content"]
            assert "Ár: 6000 (forint)" in first, first
            return 200, tool_reply("item_mark", {"item": item.id, "status": "feltöltve", "platform": "vinted"})

        calls = len(self.fake.chat_bodies())
        self.fake.chat_script = [check_price, reply("Kész.")]
        self.app.respond("Legyen 6000", mode="work")
        item = self.store.get(item.id)
        self.assertEqual((item.price, item.status), (6000, "fent"), "a te döntésed számít")
        self.assertEqual(self.fake.chat_script, [])
        bodies = self.fake.chat_bodies()[calls:]
        self.assertFalse([b for b in bodies if b.get("format")], "az árdöntés után nem olvassa ki újra a leírást")

    def test_the_form_is_filled_during_the_publish_gap(self):
        item = self.ready_item(["vinted"])
        work = self.app.work
        browser = self.app.toolbox.browser
        clock = FakeClock(time.time())
        events = []
        record = browser.on_action
        browser.on_action = lambda event: events.append((event["do"], clock.now)) or record(event)

        def fill(body):
            view = body["messages"][-1]["content"]       # az első üzenet: a megnyitott űrlap
            return 200, tool_reply("browser_fill", {"fields": [
                {"element": element_number(view, "Cím"), "text": "Zara fekete póló, L"},
                {"element": element_number(view, "Ár"), "text": "500"}]})

        def publish(body):
            return 200, tool_reply("browser_click", {"element": element_number(self.last_tool(body), "Feltöltés")})

        mark = tool_reply("item_mark", {"item": item.id, "status": "feltöltve", "platform": "vinted"})
        self.fake.chat_script = [fill, publish, mark, reply("Fent van.")]
        self.app.cfg.work.publish_gap_minutes = 2.0
        with mock.patch("jarvis_core.work.time", clock):
            work.note_publish("vinted")
            due = work._next_allowed["vinted"]
            self.app.run_work({"action": "post", "ids": [item.id], "platform": "vinted"})
        typed = [at for do, at in events if do == "type"]
        clicks = [at for do, at in events if do == "click"]
        self.assertEqual(len(typed), 2)
        self.assertTrue(all(at < due for at in typed), "a mezőket a szünet alatt tölti ki")
        self.assertGreaterEqual(clicks[-1], due, "a közzététel a szünet után")
        self.assertEqual(self.store.get(item.id).platforms["vinted"]["status"], POSTED)
        self.assertTrue(any("nemrég tettem közzé" in e[1] for e in self.ui.of("info")))
        self.assertEqual(len(self.ui.of("confirm")), 1)

    def test_a_free_site_goes_first_while_the_other_waits(self):
        item = self.ready_item(["vinted", "jofogas"])
        self.app.work._next_allowed["vinted"] = time.time() + 600      # a Vinteden nemrég tett közzé
        self.fake.chat_script = [extracted(**SHIRT), reply("Be kell jelentkezned a Jófogásra."),
                                 reply("Be kell jelentkezned a Vintedre.")]
        self.app.run_work({"action": "sell", "ids": [item.id]})
        posts = [e[1] for e in self.ui.of("info") if e[1].startswith("Feltöltés:")]
        self.assertEqual(posts, ["Feltöltés: Zara fekete póló, L → Jófogás", "Feltöltés: Zara fekete póló, L → Vinted"])
        self.assertEqual(self.fake.chat_script, [])

    def test_unclear_price_answer_asks_again(self):
        item = self.ready_item(["vinted"])
        self.store.set_pending(item.id, {"kind": "price", "question": "Marad?", "suggested": 4500})
        self.app.run_work({"action": "reply", "ids": [item.id], "text": "hmm"})
        self.assertIn("Maradjon a te árad, vagy legyen a javasolt (4 500 Ft)?", self.notices()[-1])
        self.assertEqual(self.store.get(item.id).pending["kind"], "price")

    def test_odd_price_answer_is_asked_back(self):
        item = self.ready_item(["vinted"])          # 500 Ft
        self.store.set_pending(item.id, {"kind": "price", "question": "Marad?", "suggested": 450})
        self.app.run_work({"action": "reply", "ids": [item.id], "text": "legyen ötven"})
        self.assertTrue(self.notices()[-1].startswith("50 Ft-ra gondoltál?"), self.notices()[-1])
        self.assertEqual(self.store.get(item.id).pending["confirm"], 50)
        self.app.run_work({"action": "reply", "ids": [item.id], "text": "nem"})
        self.assertEqual(self.notices()[-1], "Akkor mennyi legyen? Maradjon a te árad (500 Ft), vagy legyen a "
                                             "javasolt (450 Ft)?")
        pending = self.store.get(item.id).pending
        self.assertEqual((pending["kind"], "confirm" in pending, self.store.get(item.id).price), ("price", False, 500))
        self.app.run_work({"action": "price", "ids": [item.id], "choice": "45"})
        self.assertTrue(self.notices()[-1].startswith("45 Ft-ra gondoltál?"), "a felületről beírt összegnél is")
        self.assertEqual(self.fake.chat_bodies(), [], "modell nélkül")

    def test_retry_one_site_then_everything_is_up(self):
        item = self.ready_item(["vinted", "jofogas"])
        self.store.mark(item.id, "vinted", POSTED)
        self.store.mark(item.id, "jofogas", FAILED, note="Be kell jelentkezni.")
        self.fake.chat_script = [tool_reply("item_mark", {"item": item.id, "status": "feltöltve", "platform": "jofogas",
                                                          "url": "https://www.jofogas.hu/x_1.htm"}),
                                 reply("Fent van a Jófogáson.")]
        self.app.run_work({"action": "post", "ids": [item.id], "platform": "jofogas"})
        item = self.store.get(item.id)
        self.assertEqual(item.status, "fent")
        self.assertEqual(item.platforms["jofogas"]["url"], "https://www.jofogas.hu/x_1.htm")
        self.fake.chat_script = [extracted(**SHIRT)]
        self.app.run_work({"action": "sell", "ids": [item.id]})
        self.assertIn("már fent van mindenhol", self.notices()[-1])

    def test_model_post_sees_only_the_task_tools(self):
        # Egy modellel végzett feltöltés csak a böngészőt és a tárgy néhány eszközét látja, végig ugyanazt a
        # listát (a prompt eleje így az Ollama gyorsítótárában marad); a Munka menü szabad beszélgetése mindet.
        item = self.ready_item(["jofogas"])
        self.fake.chat_script = [tool_reply("item_post", {"item": item.id, "platform": "jofogas"}),
                                 tool_reply("web_search", {"query": "póló ár"}),
                                 tool_reply("item_mark", {"item": item.id, "status": "feltöltve",
                                                          "platform": "jofogas"}),
                                 reply("Fent van a Jófogáson.")]
        self.app.run_work({"action": "post", "ids": [item.id], "platform": "jofogas"})
        self.assertEqual(self.store.get(item.id).platforms["jofogas"]["status"], POSTED)
        bodies = self.fake.chat_bodies()
        names = {t["function"]["name"] for t in bodies[0]["tools"]}
        self.assertEqual(names, self.app.work.task_tools)
        self.assertTrue({"item_post", "item_mark", "item_get", "browser_click", "browser_fill",
                         "browser_upload_photos"} <= names)
        self.assertFalse({"web_search", "item_update", "item_research", "item_list", "get_datetime"} & names)
        self.assertTrue(all(body["tools"] == bodies[0]["tools"] for body in bodies))
        self.assertIn("itt nincs ilyen eszköz: web_search", self.last_tool(bodies[2]))
        options = bodies[0]["options"]
        self.assertEqual((options["temperature"], options["num_predict"]), (0.3, 1024), "a Munka menü mintavétele")

        self.fake.chat_script = [reply("Egy tárgyad van.")]
        self.app.respond("Hány tárgyam van?", mode="work")
        names = {t["function"]["name"] for t in self.fake.chat_bodies()[-1]["tools"]}
        self.assertEqual(names, self.app.work.agent_tools, "a szabad beszélgetés minden eszközt lát")

    def test_login_opens_the_site_in_jarvis_browser(self):
        self.app.run_work({"action": "login", "platform": "jofogas"})
        self.assertIn("Jelentkezz be", self.notices()[-1])
        self.assertIn("Jelentkezz be a hirdetéshez", self.app.toolbox.browser.snapshot())

    def test_fill_accepts_the_field_list_as_text(self):
        view = self.app.toolbox.call("browser_open", {"url": self.pages.url("feladas.html")})
        fields = json.dumps([{"element": element_number(view, "Cím"), "text": "Kék pulóver"}])
        self.assertIn('Kitöltöttem: "Cím".', self.app.toolbox.call("browser_fill", {"fields": fields}))
        self.assertTrue(self.app.toolbox.call("browser_fill", {"fields": "nem lista"}).startswith("HIBA"))
        self.assertIn("browser_fill", self.app.work.agent_tools)

    def test_post_refuses_an_incomplete_item(self):
        item = self.store.create(targets=["vinted"])
        result = self.app.toolbox.call("item_post", {"item": item.id, "platform": "vinted"})
        self.assertTrue(result.startswith("HIBA"))
        self.assertIn("cím, leírás, ár, fotó", result)
        # a feltöltés első lépését a Jarvis végzi: ha nem megy, a modell el sem indul, a hiba a kártyára kerül
        self.app.run_work({"action": "post", "ids": [item.id], "platform": "vinted"})
        entry = self.store.get(item.id).platforms["vinted"]
        self.assertEqual(entry["status"], FAILED)
        self.assertTrue(entry["note"].startswith("A feltöltéshez még hiányzik: cím, leírás, ár, fotó."), entry["note"])
        self.assertEqual(self.fake.chat_bodies(), [], "egy modellkör sem kellett")
        self.assertEqual(self.ui.of("assistant_end")[-1][1], entry["note"], "mintha a modell mondta volna")

    def test_delist_starts_on_the_opened_listing(self):
        item = self.ready_item(["jofogas"])
        self.store.mark(item.id, "jofogas", POSTED, url=self.pages.url("fooldal.html"))

        def delete(body):
            assert [m["role"] for m in body["messages"]] == ["system", "user"], body["messages"]
            first = body["messages"][-1]["content"]
            assert "Az item_delist lépést már elvégeztem" in first, first
            assert "Megnyitottam a hirdetést: " + self.pages.url("fooldal.html") in first, first
            return 200, tool_reply("item_mark", {"item": item.id, "status": "nincs fent", "platform": "jofogas"})

        self.fake.chat_script = [delete, reply("Levettem a Jófogásról.")]
        self.app.run_work({"action": "delist", "ids": [item.id], "platform": "jofogas"})
        self.assertEqual(self.fake.chat_script, [])
        entry = self.store.get(item.id).platforms["jofogas"]
        self.assertEqual((entry["status"], entry.get("note")), (NOT_POSTED, "Levéve"))

    def test_queued_work_shows_on_the_card(self):
        item = self.new_item(["vinted"])
        self.app._outstanding = 1      # Jarvis épp mással dolgozik
        self.app.submit_work("sell", [item.id])
        self.assertEqual(self.store.get(item.id).busy, "Sorra vár…")
        self.app._outstanding = 0
        _, payload = self.app.requests.get_nowait()
        self.fake.chat_script = [extracted(**{**SHIRT, "size": ""})]
        self.app.run_work(payload)
        self.assertEqual(self.store.get(item.id).busy, "", "a végén nincs „dolgozom” jelzés")
        self.assertEqual(self.store.get(item.id).status, "kérdés")


class CompactionKeepsListingTests(unittest.TestCase):
    def test_listing_data_survives_when_the_page_view_gets_stale(self):
        from jarvis_core.assistant import KEEP_ABOVE, STALE_NOTE, Assistant
        from jarvis_core.tools import params

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(Path(tmp))
            box = Toolbox(cfg)
            box.register("item_post", "teszt", params(), lambda: "", ephemeral=True)
            assistant = Assistant(cfg, None, box, "rendszer")
            assistant.history = [
                {"role": "user", "content": "töltsd fel"},
                {"role": "tool", "tool_name": "item_post", "content": "Cím: Kék pulóver" + KEEP_ABOVE + "hosszú oldal…"},
                {"role": "tool", "tool_name": "item_post", "content": "csak oldalnézet"},
            ]
            assistant._compact(keep=0)
            assistant._compact(keep=0)
            self.assertEqual(assistant.history[1]["content"], "Cím: Kék pulóver\n" + STALE_NOTE)
            self.assertEqual(assistant.history[2]["content"], STALE_NOTE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
