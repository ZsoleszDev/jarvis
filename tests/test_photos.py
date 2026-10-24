"""A fotóellenőrzés (photos.py) tesztjei: élesség, fényviszonyok, felbontás, ismétlődő fotók a tárgyon belül és a
tárgyak között, a régi tárgyak betöltése és utólagos mérése, a jelzés a feltöltés elején, és a felület.

A próbaképek mesterségesek (zajminta, sakktábla, és egy „telefonos” hirdetésfotó: póló alakú tárgy ráncokkal,
sima háttér előtt), így a teszt internet és valódi fotók nélkül is lefut.

Futtatás:  python -m unittest tests.test_photos -v
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
from PIL import Image, ImageEnhance, ImageFilter  # noqa: E402

from test_offline import FakeOllama, make_cfg  # noqa: E402
from test_streaming import RecordingUI  # noqa: E402
from test_web_browser import HAS_CHROMIUM  # noqa: E402
from test_work import SHIRT, extracted, jpeg  # noqa: E402

from jarvis_core import photos  # noqa: E402
from jarvis_core.app import JarvisApp  # noqa: E402
from jarvis_core.llm import OllamaClient  # noqa: E402
from jarvis_core.selling import Item, ItemStore, process_photo  # noqa: E402
from jarvis_core.tools import Toolbox  # noqa: E402
from jarvis_core.work import WorkDesk  # noqa: E402


# --- próbaképek ----------------------------------------------------------------------------------

def scene(seed: int = 1, color=(40, 40, 45), bg: float = 205, size=(960, 1280), texture: float = 6.0,
          noise: float = 2.5) -> Image.Image:
    """Egy hirdetésfotó: póló alakú tárgy sima háttér (ágy, ajtó) előtt, enyhe fényátmenettel és szenzorzajjal. A
    ráncok a seed-től függnek: ugyanaz az elrendezés más seed-del olyan, mint a póló másik oldala."""
    rng = np.random.default_rng(seed)
    w, h = size
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    base = bg - 35 * (x / w) - 25 * (y / h)
    image = np.stack([base, base * 0.97, base * 0.93], -1)
    cx, cy = w / 2, h / 2
    body = (abs(x - cx) < w * 0.25) & (y > cy - h * 0.25) & (y < cy + h * 0.33)
    sleeves = (abs(x - cx) < w * 0.42) & (y > cy - h * 0.25) & (y < cy - h * 0.08)
    neck = ((x - cx) ** 2 + (y - (cy - h * 0.25)) ** 2) < (w * 0.08) ** 2
    mask = (body | sleeves) & ~neck
    coarse = rng.normal(0, 1, (h // 8, w // 8)).astype(np.float32)
    coarse = ((coarse - coarse.min()) / np.ptp(coarse) * 255).astype(np.uint8)
    folds = np.asarray(Image.fromarray(coarse).resize((w, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(40)),
                       dtype=np.float32)
    folds = (folds - folds.mean()) / (folds.std() + 1e-6)
    fabric = rng.normal(0, 1, (h, w)).astype(np.float32)
    for channel in range(3):
        image[..., channel] = np.where(mask, color[channel] * (1 + 0.25 * folds) + texture * fabric,
                                       image[..., channel])
    image += rng.normal(0, noise, image.shape)
    return Image.fromarray(np.clip(image, 0, 255).astype(np.uint8))


def noise_texture(size=(1280, 960), block: int = 4) -> Image.Image:
    rng = np.random.default_rng(7)
    w, h = size
    values = rng.integers(30, 225, (h // block, w // block)).astype(np.uint8)
    return Image.fromarray(values).resize((w, h), Image.NEAREST).convert("RGB")


def checkerboard(size=(1280, 960), cell: int = 16) -> Image.Image:
    w, h = size
    y, x = np.mgrid[0:h, 0:w]
    return Image.fromarray(((((x // cell) + (y // cell)) % 2) * 200 + 28).astype(np.uint8)).convert("RGB")


def scaled(image: Image.Image, factor: float) -> Image.Image:
    """Alul- vagy túlexponálás: minden pont fényessége szorozva (255-nél levágva)."""
    return Image.fromarray(np.clip(np.asarray(image, np.float32) * factor, 0, 255).astype(np.uint8))


def backlit() -> Image.Image:
    """Ablak előtt fotózva: a felső harmad kiégett, a sötét tárgy teljesen sötét."""
    image = np.asarray(scene(7, color=(30, 30, 35)), np.float32).copy()
    image[: image.shape[0] * 35 // 100] = 255
    image = np.where(image < 60, image * 0.05, image)
    return Image.fromarray(np.clip(image, 0, 255).astype(np.uint8))


def raw(image: Image.Image, quality: int = 92) -> bytes:
    """A telefonról érkező fájl."""
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


def stored(image: Image.Image) -> bytes:
    """Amit a Jarvis elment (legfeljebb 1600 px-es JPEG): ebből mér."""
    return process_photo(raw(image))[0]


def measured(image: Image.Image) -> dict:
    return photos.measure(stored(image))


def check(sharp: float = 300.0, seed: int = 0, **values) -> dict:
    """Kézzel összerakott mérés (a szövegek teszteléséhez): alapból egy rendben lévő, egyedi fotó."""
    rng = np.random.default_rng(seed)
    data = {"v": photos.CHECK_VERSION, "w": 1200, "h": 1600, "sharp": sharp, "mean": 120.0, "p05": 30, "p95": 200,
            "white": 0.0, "black": 0.0, "rgb": [120, 118, 115],
            "hash": bytes(rng.integers(0, 256, 8, dtype=np.uint8)).hex(),
            "detail": bytes(rng.integers(0, 256, 32, dtype=np.uint8)).hex()}
    data.update(values)
    return data


# --- mértékek és küszöbök ---------------------------------------------------------------------------

class MeasureTests(unittest.TestCase):
    def test_a_sharp_texture_and_its_blurred_copy(self):
        for name, image in (("zajminta", noise_texture()), ("sakktábla", checkerboard())):
            sharp, blurred = measured(image), measured(image.filter(ImageFilter.GaussianBlur(10)))
            self.assertEqual(photos.problems(sharp), [], name)
            self.assertEqual(photos.problems(blurred), ["blurry"], name)
            self.assertGreater(sharp["sharp"], 20 * blurred["sharp"], name)

    def test_ordinary_phone_photos_get_no_warning(self):
        # amitől egy óvatlan ellenőrzés jelezne: sima vagy fehér háttér, sötét ruha, kis kontraszt, enyhe puhaság
        smooth_box = scene(9, color=(150, 150, 150), bg=185, texture=0, noise=1.5)
        cases = {
            "fekete póló világos háttér előtt": scene(1),
            "enyhén puha (2 px)": scene(1).filter(ImageFilter.GaussianBlur(2)),
            "fehér háttér (háttércsere)": scene(2, bg=300),
            "világos pulóver fehér háttér előtt": scene(2, color=(205, 200, 190), bg=300),
            "szürke ing háttércserével": scene(4, color=(215, 215, 218), bg=300),
            "fehér póló háttércserével": scene(4, color=(232, 232, 230), bg=300),
            "fehér póló fehér lepedőn": scene(3, color=(232, 232, 230), bg=235),
            "bézs a bézsen": scene(5, color=(190, 175, 150), bg=200),
            "kicsit sötétebb szoba": scaled(scene(1), 0.5),
            "sima, mintátlan doboz": smooth_box,
            "nagy, 12 megapixeles fotó": scene(1, size=(3000, 4000)),
        }
        for name, image in cases.items():
            self.assertEqual(photos.problems(measured(image)), [], name)

    def test_blurry_dark_bright_backlit_and_small(self):
        cases = {
            "blurry": scene(1).filter(ImageFilter.GaussianBlur(7)),
            "dark": scaled(scene(1), 0.25),
            "bright": scaled(scene(3, color=(232, 232, 230), bg=235), 1.3),
            "clipped": backlit(),
            "small": scene(1, size=(450, 600)),
        }
        for kind, image in cases.items():
            self.assertEqual(photos.problems(measured(image)), [kind], kind)
        self.assertEqual(photos.problems(photos.measure(jpeg())), ["dark", "small"],
                         "egyszínű, sötét, 300×400 px: a sötétnél nem mondja még homályosnak is")

    def test_the_verdict_uses_the_current_thresholds(self):
        small = measured(scene(1, size=(450, 600)))
        self.assertEqual((small["w"], small["h"], small["v"]), (450, 600, photos.CHECK_VERSION))
        with mock.patch.object(photos, "SMALL_SIDE", 400):
            self.assertEqual(photos.problems(small), [], "a mentett mértékből, újramérés nélkül")

    def test_missing_old_or_broken_checks_mean_no_warning(self):
        for bad in (None, {}, "x", {**check(sharp=1), "v": 0}, {**check(), "sharp": "sok"}, {"v": photos.CHECK_VERSION}):
            self.assertEqual(photos.problems(bad), [], bad)
        self.assertFalse(photos.current({**check(), "v": 0}))
        self.assertIsNone(photos.key({**check(), "hash": "nem hex"}))
        with self.assertRaises(Exception):
            photos.measure(b"<html>nem kep</html>")


# --- ugyanaz a fotó ------------------------------------------------------------------------------------

class DuplicateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = scene(1)
        cls.check = measured(cls.base)

    def test_copies_and_slightly_changed_copies_are_the_same_photo(self):
        w, h = self.base.size
        copies = {
            "újratömörítve": Image.open(io.BytesIO(raw(self.base, 50))),
            "fele akkora": self.base.resize((w // 2, h // 2), Image.LANCZOS),
            "10%-kal világosabb": ImageEnhance.Brightness(self.base).enhance(1.1),
            "2%-kal levágva": self.base.crop((w // 50, h // 50, w - w // 50, h - h // 50)),
            "1 fokkal elforgatva": self.base.rotate(1, resample=Image.BICUBIC, fillcolor=(190, 190, 190)),
        }
        for name, image in copies.items():
            self.assertTrue(photos.same(self.check, measured(image)), name)

    def test_the_other_side_or_another_item_in_the_same_layout_is_not(self):
        for seed in (4, 5, 6, 7):     # a póló másik oldala (más ráncokkal), ugyanott lefotózva
            back = measured(scene(seed))
            self.assertFalse(photos.same(self.check, back), seed)
            coarse = (int(self.check["hash"], 16) ^ int(back["hash"], 16)).bit_count()
            self.assertLessEqual(coarse, photos.DUPLICATE_BITS, f"{seed}: a 64 bites hash önmagában ugyanannak látná")
        self.assertFalse(photos.same(self.check, measured(scene(4, color=(150, 30, 30)))), "másik tárgy")
        self.assertFalse(photos.same(self.check, measured(scene(1, bg=120))), "más háttér előtt")

    def test_single_colour_photos_differ_by_colour(self):
        red = photos.measure(jpeg(color=(200, 30, 30)))
        blue = photos.measure(jpeg(color=(30, 30, 200)))
        self.assertEqual(red["hash"], blue["hash"], "az egyszínű kép hash-e mindig ugyanaz")
        self.assertFalse(photos.same(red, blue))
        self.assertTrue(photos.same(red, photos.measure(jpeg(color=(200, 30, 30)))))


# --- a jelzések szövege ---------------------------------------------------------------------------

class ReportTests(unittest.TestCase):
    def test_ordinals_with_the_right_article(self):
        self.assertEqual(photos.ordinals([3]), "a 3.")
        self.assertEqual(photos.ordinals([1, 4]), "az 1. és a 4.")
        self.assertEqual(photos.ordinals([2, 3, 5]), "a 2., a 3. és az 5.")
        self.assertEqual([photos.ordinals([n]) for n in (10, 11, 15, 20)], ["a 10.", "a 11.", "a 15.", "a 20."])

    def test_grouped_note_warnings_and_cover(self):
        names = ["01.jpg", "02.jpg", "03.jpg", "04.jpg", "05.jpg"]
        twin = check(sharp=900, seed=3)
        checks = {"01.jpg": check(sharp=3, seed=1), "02.jpg": check(sharp=200, seed=2), "03.jpg": twin,
                  "04.jpg": dict(twin), "05.jpg": check(sharp=2000, seed=5)}
        result = photos.report(names, checks, {"05.jpg": ["Kék farmer"]})
        self.assertEqual(result["photo_note"],
                         "Az 1. fotó homályosnak tűnik; a 3. és a 4. majdnem ugyanaz; az 5. fotó a(z) „Kék farmer” "
                         "tárgynál is megvan. Borítónak a 3. jobb lenne.")
        self.assertEqual(result["photo_cover"], "03.jpg", "a legélesebb rendben lévő, ami nem egy másik tárgy fotója")
        warnings = result["photo_warnings"]
        self.assertEqual(sorted(warnings), ["01.jpg", "03.jpg", "04.jpg", "05.jpg"])
        self.assertEqual(warnings["01.jpg"], [{"kind": "blurry", "text": "Homályosnak tűnik."}])
        self.assertEqual(warnings["04.jpg"], [{"kind": "duplicate",
                                               "text": "Majdnem ugyanaz, mint a 3.; az egyiket törölheted."}])
        self.assertEqual(warnings["05.jpg"][0]["kind"], "elsewhere")
        self.assertIn("„Kék farmer”", warnings["05.jpg"][0]["text"])

    def test_several_photos_with_the_same_problem_and_the_size_in_the_tip(self):
        names = ["01.jpg", "02.jpg", "03.jpg"]
        checks = {"01.jpg": check(seed=1, mean=30.0, p95=60), "02.jpg": check(seed=2),
                  "03.jpg": check(seed=3, mean=25.0, p95=50, w=480, h=640)}
        result = photos.report(names, checks)
        self.assertEqual(result["photo_note"], "Az 1. és a 3. fotó túl sötét; a 3. fotó kis felbontású. "
                                               "Borítónak a 2. jobb lenne.")
        self.assertEqual([w["text"] for w in result["photo_warnings"]["03.jpg"]],
                         ["Túl sötét.", "Kicsi a felbontása (480×640 px), nagyítva homályos lesz."])

    def test_no_cover_suggestion_when_the_first_is_fine_or_nothing_is_better(self):
        names = ["01.jpg", "02.jpg"]
        fine_first = photos.report(names, {"01.jpg": check(seed=1), "02.jpg": check(sharp=3, seed=2)})
        self.assertEqual((fine_first["photo_cover"], fine_first["photo_note"]), ("", "A 2. fotó homályosnak tűnik."))
        all_bad = photos.report(names, {"01.jpg": check(sharp=3, seed=1), "02.jpg": check(sharp=4, seed=2)})
        self.assertEqual((all_bad["photo_cover"], all_bad["photo_note"]), ("", "Az 1. és a 2. fotó homályosnak tűnik."))
        self.assertEqual(photos.report(names, {}), {"photo_warnings": {}, "photo_cover": "", "photo_note": ""},
                         "amíg nincs mérve, nincs jelzés")


# --- tárolás: mérés a felvételkor, ismétlődés a tárgyak között, régi tárgyak ---------------------------------

class StoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.good = raw(scene(1))
        cls.other = raw(scene(3))
        cls.blurry = raw(scene(5).filter(ImageFilter.GaussianBlur(7)))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.changes: list[dict] = []
        self.store = ItemStore(self.root, on_change=self.changes.append)

    def tearDown(self):
        self.tmp.cleanup()

    def wait_for(self, condition, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail("nem teljesült időben")
            time.sleep(0.03)

    def test_checks_are_measured_when_added_and_saved_with_the_item(self):
        item = self.store.create(prompt="póló")
        self.store.add_photo(item.id, self.good)
        item = self.store.add_photo(item.id, self.blurry)
        self.assertEqual(sorted(item.photo_checks), ["01.jpg", "02.jpg"])
        saved = json.loads((self.root / item.id / "item.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["photo_checks"]["02.jpg"]["v"], photos.CHECK_VERSION)
        self.assertNotIn("twins", saved)
        loaded = ItemStore(self.root).get(item.id)
        self.assertEqual(loaded.photo_checks, item.photo_checks, "újraindítás után sem kell újramérni")
        self.assertEqual(list(loaded.public()["photo_warnings"]), ["02.jpg"])
        item = self.store.remove_photo(item.id, "02.jpg")
        self.assertEqual(list(item.photo_checks), ["01.jpg"])
        self.assertEqual(item.public()["photo_note"], "")

    def test_the_same_photo_within_an_item_and_in_another_item(self):
        a = self.store.create(prompt="Fekete póló")
        for data in (self.good, self.other, raw(Image.open(io.BytesIO(self.good)), 70)):
            self.store.add_photo(a.id, data)
        self.assertEqual(self.store.get(a.id).public()["photo_note"], "Az 1. és a 3. majdnem ugyanaz.")
        b = self.store.create(prompt="Kék farmer")
        before = self.store.get(a.id).updated
        self.changes.clear()
        item_b = self.store.add_photo(b.id, self.other)
        self.assertEqual(item_b.public()["photo_note"], "Az 1. fotó a(z) „Fekete póló” tárgynál is megvan.")
        self.assertEqual([c["id"] for c in self.changes], [b.id, a.id], "a másik tárgy kártyája is frissül")
        self.assertIn("a 2. fotó a(z) „Kék farmer” tárgynál is megvan", self.changes[-1]["photo_note"])
        self.assertEqual(self.store.get(a.id).updated, before, "attól nem ez lesz a legutóbb módosított tárgy")
        self.store.update(b.id, title="Levi's farmer")
        self.assertIn("„Levi's farmer”", self.store.get(a.id).public()["photo_note"], "mindig a mostani neve")
        json.dumps(self.store.get(a.id).public())       # a másik tárgyra mutató hivatkozás nem kerül bele
        self.changes.clear()
        self.store.delete(b.id)
        self.assertEqual([c["id"] for c in self.changes], [a.id])
        self.assertEqual(self.store.get(a.id).public()["photo_note"], "Az 1. és a 3. majdnem ugyanaz.")

    def test_cover_is_only_suggested_and_the_button_reorders(self):
        item = self.store.create(prompt="póló")
        softer = raw(scene(3).filter(ImageFilter.GaussianBlur(2)))     # rendben van, de nem olyan éles
        for data in (self.blurry, self.good, softer):
            item = self.store.add_photo(item.id, data)
        report = item.public()
        self.assertEqual(report["photo_cover"], "02.jpg")
        self.assertEqual(item.photos, ["01.jpg", "02.jpg", "03.jpg"], "magától nem cseréli")
        item = self.store.reorder_photos(item.id, [report["photo_cover"]])   # a felület gombja ezt hívja
        self.assertEqual((item.photos[0], item.public()["photo_cover"]), ("02.jpg", ""))
        self.assertEqual(item.public()["photo_note"], "A 2. fotó homályosnak tűnik.", "a sorszám az új sorrendé")

    def write_old_item(self, item_id: str, photos_data: dict[str, bytes], **extra) -> None:
        folder = self.root / item_id
        (folder / "thumbs").mkdir(parents=True)
        for name, data in photos_data.items():
            (folder / name).write_bytes(data)
            (folder / "thumbs" / name).write_bytes(data)
        (folder / "item.json").write_text(json.dumps({
            "id": item_id, "created": 1, "updated": 1, "photos": list(photos_data), "title": "Régi tárgy " + item_id,
            "ismeretlen": True, **extra}), encoding="utf-8")

    def test_items_from_before_the_photo_check_are_measured_in_the_background(self):
        self.write_old_item("a1b2c3d4", {"01.jpg": stored(scene(5).filter(ImageFilter.GaussianBlur(7))),
                                         "02.jpg": stored(scene(1)), "03.jpg": b"nem kep"})
        self.write_old_item("b1b2c3d4", {"01.jpg": stored(scene(1))},
                            photo_checks={"01.jpg": {"v": 0, "sharp": 1}, "07.jpg": {"v": 1}})
        changes: list[dict] = []
        store = ItemStore(self.root, on_change=changes.append)
        old = store.get("a1b2c3d4")
        self.assertEqual((old.title, old.photos), ("Régi tárgy a1b2c3d4", ["01.jpg", "02.jpg", "03.jpg"]))
        self.wait_for(lambda: not store._backfilling)
        old = store.get("a1b2c3d4")
        self.assertEqual(sorted(old.photo_checks), ["01.jpg", "02.jpg"], "a hibás fájlt kihagyja, és nem akad el")
        self.assertEqual(old.public()["photo_note"],
                         "Az 1. fotó homályosnak tűnik; a 2. fotó a(z) „Régi tárgy b1b2c3d4” tárgynál is megvan.",
                         "borítónak nem javasol olyat, ami egy másik tárgy fotója")
        other = store.get("b1b2c3d4")
        self.assertEqual((list(other.photo_checks), other.photo_checks["01.jpg"]["v"]), (["01.jpg"], photos.CHECK_VERSION),
                         "a régi változatú mérést újra elvégzi, a nem létező fotóét eldobja")
        self.assertIn("a1b2c3d4", [c["id"] for c in changes], "a kártya frissül, amint megvan")
        saved = json.loads((self.root / "a1b2c3d4" / "item.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(saved["photo_checks"]), ["01.jpg", "02.jpg"])
        self.assertEqual(old.updated, 1, "a régi tárgy attól nem lesz frissen módosított")

    def test_ensure_checks_measures_one_item_right_away(self):
        self.write_old_item("c1b2c3d4", {"01.jpg": stored(scene(1, size=(450, 600)))})
        with mock.patch.object(ItemStore, "_start_backfill"):
            store = ItemStore(self.root)
        self.assertEqual(store.get("c1b2c3d4").photo_checks, {})
        item = store.ensure_checks("c1b2c3d4")
        self.assertEqual(photos.problems(item.photo_checks["01.jpg"]), ["small"])

    def test_from_dict_keeps_only_sane_checks(self):
        good = check()
        item = Item.from_dict({"id": "a1b2c3d4", "created": 1, "updated": 1, "photos": ["01.jpg", "02.jpg"],
                               "photo_checks": {"01.jpg": good, "02.jpg": "x", "09.jpg": good}})
        self.assertEqual(item.photo_checks, {"01.jpg": good})
        item = Item.from_dict({"id": "a1b2c3d4", "created": 1, "updated": 1, "photo_checks": ["x"]})
        self.assertEqual((item.photo_checks, item.twins), ({}, {}))
        data = item.public()
        self.assertEqual((data["photo_checks"], data["photo_warnings"], data["photo_cover"], data["photo_note"]),
                         ({}, {}, "", ""))


# --- a feltöltés elején --------------------------------------------------------------------------------

class PrepareNoteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.good = raw(scene(1))
        cls.blurry = raw(scene(5).filter(ImageFilter.GaussianBlur(7)))

    def setUp(self):
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        self.cfg.browser.enabled = False
        self.desk = WorkDesk(self.cfg, OllamaClient(self.cfg.llm), Toolbox(self.cfg))
        self.store = self.desk.store

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def item(self, *photo_data: bytes) -> Item:
        item = self.store.create(targets=["vinted"], prompt="Zara L-es fekete póló, 500 Ft")
        for data in photo_data:
            item = self.store.add_photo(item.id, data)
        return item

    def test_the_note_comes_with_the_upload_and_does_not_block_it(self):
        item = self.item(self.blurry, self.good, self.good)
        self.fake.chat_script = [extracted(**SHIRT)]
        step = self.desk.prepare(item.id, lambda _detail: None)
        self.assertEqual(step.kind, "post")
        self.assertEqual(step.note, "Az 1. fotó homályosnak tűnik; a 2. és a 3. majdnem ugyanaz. Borítónak a 2. "
                                    "jobb lenne. Ettől még feltöltöm.")
        self.assertEqual(self.store.get(item.id).photos, ["01.jpg", "02.jpg", "03.jpg"], "a sorrendhez nem nyúl")

    def test_no_note_when_the_photos_are_fine_or_jarvis_asks_first(self):
        item = self.item(self.good)
        self.fake.chat_script = [extracted(**{**SHIRT, "size": ""})]
        step = self.desk.prepare(item.id, lambda _detail: None)
        self.assertEqual((step.kind, step.note), ("ask", ""), "kérdésnél még nem, csak amikor a feltöltés indul")
        self.store.update(item.id, size="L")
        step = self.desk.prepare(item.id, lambda _detail: None)
        self.assertEqual((step.kind, step.note), ("post", ""))

    def test_an_old_item_is_measured_before_the_note(self):
        item = self.item(self.blurry)
        folder = self.store.folder(item.id)
        saved = json.loads((folder / "item.json").read_text(encoding="utf-8"))
        saved.pop("photo_checks")
        (folder / "item.json").write_text(json.dumps(saved), encoding="utf-8")
        with mock.patch.object(ItemStore, "_start_backfill"):     # a háttérmérés most ne előzze meg
            desk = WorkDesk(self.cfg, OllamaClient(self.cfg.llm), Toolbox(self.cfg))
        self.assertEqual(desk.store.get(item.id).photo_checks, {})
        self.fake.chat_script = [extracted(**SHIRT)]
        self.assertEqual(desk.prepare(item.id, lambda _detail: None).note,
                         "Az 1. fotó homályosnak tűnik. Ettől még feltöltöm.")


class SellNoteTests(unittest.TestCase):
    """A JarvisApp a jelzést kiírja a Munka naplójába, és megy tovább a feltöltéssel (a böngésző itt nem indul)."""

    def setUp(self):
        self.fake = FakeOllama()
        self.fake.vram_fit = lambda ctx: 1.0
        self.tmp = tempfile.TemporaryDirectory()
        cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        cfg.browser.headless = True
        cfg.assistant.show_timings = False
        self.ui = RecordingUI()
        self.app = JarvisApp(cfg, self.ui)
        self.assertTrue(self.app.start())
        if not self.app.work.browser_ready:
            self.skipTest("a Munka menü feltöltéséhez böngésző kell (Playwright)")

    def tearDown(self):
        self.app.toolbox.close()
        self.fake.close()
        self.tmp.cleanup()

    def test_the_note_is_shown_and_the_upload_goes_on(self):
        store = self.app.work.store
        item = store.create(targets=["vinted"], prompt="Zara L-es fekete póló, 500 Ft")
        store.add_photo(item.id, raw(scene(5).filter(ImageFilter.GaussianBlur(7))))
        self.fake.chat_script = [extracted(**SHIRT)]
        with mock.patch.object(self.app, "_post_all") as post_all:
            self.app.run_work({"action": "sell", "ids": [item.id]})
        post_all.assert_called_once_with(item.id)
        notices = [e[1] for e in self.ui.of("notice")]
        self.assertIn("Az 1. fotó homályosnak tűnik. Ettől még feltöltöm.", notices)


# --- a felület és a telefonos kapcsolat --------------------------------------------------------------------

class PayloadTests(unittest.TestCase):
    def test_gui_items_carry_the_photo_checks(self):
        from test_gui_work import WorkHarness, request

        h = WorkHarness()
        try:
            item = h.store.create(targets=["vinted"], prompt="póló")
            headers = {"X-Jarvis-Token": h.server.token, "Host": f"127.0.0.1:{h.server.port}"}
            status, data = request(h.server.port, "POST", f"/api/work/items/{item.id}/photo", headers,
                                   raw(scene(1, size=(450, 600))))
            self.assertEqual(status, 200)
            uploaded = json.loads(data)["item"]
            self.assertEqual([w["kind"] for w in uploaded["photo_warnings"]["01.jpg"]], ["small"])
            status, data = request(h.server.port, "GET", f"/api/work/items?t={h.server.token}",
                                   {"Host": f"127.0.0.1:{h.server.port}"})
            listed = next(i for i in json.loads(data)["items"] if i["id"] == item.id)
            self.assertEqual(listed["photo_note"], "Az 1. fotó kis felbontású.")
            self.assertEqual((listed["photo_cover"], listed["photo_checks"]["01.jpg"]["w"]), ("", 450))
        finally:
            h.close()

    def test_phone_api_items_carry_the_photo_checks(self):
        from test_api import Harness

        h = Harness()
        try:
            status, data, _ = h.request("POST", "/api/v1/work/items", {"targets": ["vinted"], "prompt": "póló"})
            item_id = data["item"]["id"]
            for image in (scene(5).filter(ImageFilter.GaussianBlur(7)), scene(1)):
                status, data, _ = h.request("POST", f"/api/v1/work/items/{item_id}/photos", raw=raw(image),
                                            content_type="image/jpeg")
                self.assertEqual(status, 201)
            status, data, _ = h.request("GET", f"/api/v1/work/items/{item_id}")
            found = data["item"]
            self.assertEqual(found["photo_note"], "Az 1. fotó homályosnak tűnik. Borítónak a 2. jobb lenne.")
            self.assertEqual((found["photo_cover"], list(found["photo_warnings"])), ("02.jpg", ["01.jpg"]))
            status, data, _ = h.request("POST", f"/api/v1/work/items/{item_id}/photos/order", {"order": ["02.jpg"]})
            self.assertEqual((data["item"]["photos"], data["item"]["photo_cover"]), (["02.jpg", "01.jpg"], ""))
        finally:
            h.close()


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class PhotoBadgeBrowserTests(unittest.TestCase):
    """A jelzés a fotók előnézetén és a kártyán, és a borító gombja, a valódi felületen."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        from test_gui_work import WorkHarness

        self.h = WorkHarness()
        self.context = self.browser.new_context(viewport={"width": 1000, "height": 900})
        self.page = self.context.new_page()
        self.errors: list[str] = []
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.goto(self.h.server.url)
        self.page.wait_for_selector('body[data-state="idle"]', state="attached", timeout=15000)
        self.page.click("#tab-work")
        self.page.wait_for_selector('body[data-mode="work"]', state="attached")

    def tearDown(self):
        self.context.close()
        self.h.close()
        self.assertEqual(self.errors, [], "JavaScript-hiba az oldalon")

    def test_badges_tooltips_and_the_cover_button(self):
        store = self.h.store
        item = store.create(targets=["vinted"], prompt="Zara L-es fekete póló, 500 Ft")
        for image in (scaled(scene(1), 0.25), scene(3).filter(ImageFilter.GaussianBlur(2)), scene(1)):
            store.add_photo(item.id, raw(image))
        page = self.page
        # a kártya fotónként frissül: az utolsó után ez áll rajta
        page.wait_for_selector(f'.card[data-id="{item.id}"] .cardthumb .warn'
                               '[title="Az 1. fotó túl sötét. Borítónak a 3. jobb lenne."]')
        page.click(f'.card[data-id="{item.id}"] button.title')
        page.wait_for_selector("#f-title")
        self.assertEqual(page.get_attribute(".detail .photo:nth-child(1) .warn", "title"), "Túl sötét.")
        self.assertEqual(page.locator(".detail .photo .warn").count(), 1, "csak ott, ahol gond van")
        self.assertIn("Borítónak a 3. jobb lenne.", page.inner_text(".detail .photonote"))
        self.assertEqual(store.get(item.id).photos, ["01.jpg", "02.jpg", "03.jpg"], "kérés nélkül nem cserél")
        page.click('.detail .photonote [data-action="cover"]:has-text("Legyen a 3. a borító")')
        deadline = time.monotonic() + 10
        while store.get(item.id).photos[0] != "03.jpg" and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(store.get(item.id).photos, ["03.jpg", "01.jpg", "02.jpg"])
        page.wait_for_selector('.detail .photonote:has-text("A 2. fotó túl sötét.")')
        self.assertEqual(page.locator('.detail .photonote [data-action="cover"]').count(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
