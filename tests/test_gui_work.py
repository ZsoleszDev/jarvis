"""Az Adás/Vétel felületének tesztjei: valódi helyi szerver, valódi (láthatatlan) Chromium, hamis
modell, és a hirdetési oldalak helyett helyi tesztoldalak. A biztonsági tesztekhez nem kell böngésző.

Futtatás:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import base64
import http.client
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_offline import FakeOllama, make_cfg, reply, tool_reply  # noqa: E402
from test_web_browser import HAS_CHROMIUM  # noqa: E402
from test_work import SHIRT, Pages, extracted, jpeg, local_platforms  # noqa: E402

from jarvis_core import selling  # noqa: E402
from jarvis_core.app import JarvisApp  # noqa: E402
from jarvis_core.gui import EventBus, GuiServer, GuiUI  # noqa: E402


class WorkHarness:
    """Teljes Jarvis (Munka menüvel) grafikus felülettel, hamis modellel, hang nélkül."""

    def __init__(self):
        self.pages = Pages()
        self.patch = mock.patch.dict(selling.PLATFORMS, local_platforms(self.pages), clear=True)
        self.patch.start()
        self.fake = FakeOllama()
        self.fake.vram_fit = lambda ctx: 1.0
        self.tmp = tempfile.TemporaryDirectory()
        cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        cfg.browser.headless = True
        cfg.web.allow_local = True
        cfg.assistant.show_timings = False
        cfg.assistant.fillers = False
        self.bus = EventBus()
        self.ui = GuiUI(self.bus, "Jarvis")
        self.app = JarvisApp(cfg, self.ui, use_voice=False, use_speech=False, use_wake_word=False)
        assert self.app.start()
        self.app.start_agent()
        self.server = GuiServer(self.app, self.ui, self.bus)
        self.server.start()
        self.bus.set_hello(self.app.describe())
        self.store = self.app.work.store
        self.photo = Path(self.tmp.name) / "foto.jpg"
        self.photo.write_bytes(jpeg())

    def ready_item(self, targets=("vinted",), **values):
        item = self.store.create(targets=list(targets), prompt="Zara L-es fekete póló, 500 Ft")
        self.store.add_photo(item.id, jpeg())
        return self.store.update(item.id, **{**SHIRT, **values})

    def close(self) -> None:
        self.app.shutdown()
        self.server.stop()
        self.fake.close()
        self.patch.stop()
        self.pages.close()
        self.tmp.cleanup()


def request(port, method, path, headers=None, body=b""):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


class WorkSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = WorkHarness()
        cls.port = cls.h.server.port
        cls.item = cls.h.ready_item()
        cls.ok = {"X-Jarvis-Token": cls.h.server.token, "Host": f"127.0.0.1:{cls.port}"}

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def post(self, path, body, headers=None):
        status, data = request(self.port, "POST", path, {**(headers or self.ok), "Content-Type": "application/json"},
                               json.dumps(body).encode())
        return status, json.loads(data or b"{}") if data.startswith(b"{") else {}

    def test_reading_needs_token(self):
        host = {"Host": f"127.0.0.1:{self.port}"}
        self.assertEqual(request(self.port, "GET", "/api/work/items", host)[0], 403)
        status, data = request(self.port, "GET", f"/api/work/items?t={self.h.server.token}", host)
        self.assertEqual(status, 200)
        data = json.loads(data)
        self.assertIn(self.item.id, [i["id"] for i in data["items"]])
        self.assertEqual([p["key"] for p in data["platforms"]], ["vinted", "jofogas", "vatera", "hardverapro"])
        photo = f"/api/work/photo/{self.item.id}/01.jpg"
        self.assertEqual(request(self.port, "GET", photo, host)[0], 403)
        status, data = request(self.port, "GET", f"{photo}?t={self.h.server.token}&thumb=1", host)
        self.assertEqual(status, 200)
        self.assertTrue(data.startswith(b"\xff\xd8"), "JPEG")
        for bad in (f"/api/work/photo/{self.item.id}/..%2Fitem.json", f"/api/work/photo/{self.item.id}/09.jpg",
                    "/api/work/photo/../../01.jpg"):
            self.assertEqual(request(self.port, "GET", f"{bad}?t={self.h.server.token}", host)[0], 404, bad)

    def test_upload_needs_token_and_same_origin(self):
        item = self.h.store.create(targets=["vinted"])
        path = f"/api/work/items/{item.id}/photo"
        image = jpeg()
        self.assertEqual(request(self.port, "POST", path, {"Host": f"127.0.0.1:{self.port}"}, image)[0], 403)
        evil = {**self.ok, "Origin": "https://gonosz.example"}
        self.assertEqual(request(self.port, "POST", path, evil, image)[0], 403)
        status, data = request(self.port, "POST", path, self.ok, b"<script>nem kep</script>")
        self.assertEqual(status, 400)
        self.assertIn("nem kép", json.loads(data)["error"])
        status, _ = request(self.port, "POST", path, {**self.ok, "Content-Length": "30000000"}, b"x")
        self.assertEqual(status, 413)
        status, data = request(self.port, "POST", path, self.ok, image)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["item"]["photos"], ["01.jpg"])

    def test_new_and_update_validate(self):
        status, data = self.post("/api/work/new", {"targets": ["vinted", "ebay"], "prompt": "póló"})
        self.assertEqual(status, 400)
        self.assertIn("ebay", data["error"])
        status, data = self.post("/api/work/new", {"targets": ["jofogas"], "prompt": "<b>póló</b>",
                                                   "research_wanted": True, "price": 5})
        self.assertEqual(status, 200)
        self.assertEqual((data["item"]["prompt"], data["item"]["research_wanted"], data["item"]["price"]),
                         ("<b>póló</b>", True, None), "csak a három mezőt veszi át")
        status, data = self.post("/api/work/update", {"id": self.item.id, "fields": {"price": "sok"}})
        self.assertEqual(status, 400)
        self.assertIn("Az ár", data["error"])
        status, data = self.post("/api/work/update", {"id": self.item.id,
                                                      "fields": {"photos": [], "edited": [], "brand": "Mango"}})
        self.assertEqual((status, data["item"]["brand"], len(data["item"]["photos"])), (200, "Mango", 1),
                         "ismeretlen mezőt nem fogad el, a fotókhoz nem lehet így nyúlni")
        self.assertIn("brand", data["item"]["edited"])
        self.assertEqual(self.post("/api/work/update", {"id": "../../x", "fields": {"title": "x"}})[0], 400)

    def test_actions_validate(self):
        bad = [
            {"action": "rm -rf", "ids": [self.item.id]},
            {"action": "sell", "ids": []},
            {"action": "sell", "ids": ["deadbeef"]},
            {"action": "post", "ids": [self.item.id], "platform": "ebay"},
            {"action": "login", "platform": "ebay"},
            {"action": "reply", "ids": [self.item.id], "text": "   "},
        ]
        for body in bad:
            self.assertEqual(self.post("/api/work/action", body)[0], 400, body)
        evil = {**self.ok, "Origin": "https://gonosz.example"}
        self.assertEqual(self.post("/api/work/action", {"action": "login", "platform": "vinted"}, evil)[0], 403)

    def test_settings_and_acknowledgement(self):
        status, data = self.post("/api/work/settings", {"settings": {"city": "Szeged", "parcel_size": "nagy"}})
        self.assertEqual((status, data["settings"]["city"], data["settings"]["parcel_size"]), (200, "Szeged", "nagy"))
        self.assertEqual(self.post("/api/work/settings", {"settings": {"parcel_size": "óriási"}})[0], 400)
        self.assertEqual(self.post("/api/work/ack", {"platform": "ebay"})[0], 400)
        status, data = self.post("/api/work/ack", {"platform": "vatera"})
        self.assertEqual((status, data["acknowledged"]), (200, ["vatera"]))


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class WorkBrowserTests(unittest.TestCase):
    """Az Adás/Vétel a valódi felületen, egy láthatatlan Chromiumban."""

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
        self.h = WorkHarness()
        self.context = self.browser.new_context(viewport={"width": 1000, "height": 900})
        self.page = self.context.new_page()
        self.errors: list[str] = []
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.goto(self.h.server.url)
        self.page.wait_for_selector('body[data-state="idle"]', state="attached", timeout=15000)
        self.page.click("#tab-work")
        self.page.wait_for_selector('body[data-mode="work"]', state="attached")
        self.page.wait_for_selector('#w-sites input[value="vinted"]', state="attached")

    def tearDown(self):
        self.context.close()
        self.h.close()
        self.assertEqual(self.errors, [], "JavaScript-hiba az oldalon")

    def wait_for(self, check, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(0.05)
        self.fail("nem teljesült időben")

    def open_item(self, item_id):
        self.page.click(f'.card[data-id="{item_id}"] button.title')
        self.page.wait_for_selector("#f-title")

    def test_new_listing_asks_back_then_posts(self):
        page = self.page
        self.assertIn("Pipáld be", page.inner_text("#w-needs"))
        page.check('#w-sites input[value="vinted"]')
        page.check('#w-sites input[value="jofogas"]')
        needs = page.inner_text("#w-needs")
        self.assertIn("Vinted, ha ruha, cipő vagy kiegészítő: márka, méret.", needs)
        self.assertIn("Jófogás: átvétel helye – írd a leírásba, vagy add meg egyszer az Alapadatoknál.", needs)
        page.click("#w-go")
        self.assertIn("Adj hozzá legalább egy fotót", page.inner_text("#w-compose-msg"))
        with page.expect_file_chooser() as chooser:
            page.click('[data-pick="compose"]')
        chooser.value.set_files(str(self.h.photo))
        page.wait_for_selector("#w-new-photos .photo img")
        page.fill("#w-prompt", "Zara L-es fekete póló, 500 Ft")
        self.h.fake.chat_script = [extracted(**{**SHIRT, "condition": "", "location": ""})]
        page.click("#w-go")
        page.wait_for_selector("#ask-dialog[open]")
        self.assertIn("Vinted, Jófogás", page.inner_text("#ask-text"))
        page.click("#ask-yes")
        # Jarvis visszakérdez a kártyán
        page.wait_for_selector('.card .ask p:has-text("még kell: állapot")')
        [item] = self.h.store.all()
        self.assertEqual((item.targets, item.prompt, len(item.photos)),
                         (["vinted", "jofogas"], "Zara L-es fekete póló, 500 Ft", 1))
        self.assertEqual(sorted(self.h.app.work.settings["acknowledged"]), ["jofogas", "vinted"])
        self.assertEqual(page.input_value("#w-prompt"), "", "az űrlap kiürül a következő tárgynak")
        self.assertEqual(page.locator("#w-new-photos .photo").count(), 0)
        # a válasz gépelése közben frissülő kártya nem veszi el a beírt szöveget
        answer = f'[data-keep="reply-{item.id}"]'
        page.fill(answer, "Nagyon jó, Budapesten")
        self.h.store.set_busy(item.id, "Frissítés…")
        page.wait_for_selector('.card .busy:has-text("Frissítés")')
        self.h.store.set_busy(item.id, "")
        page.wait_for_selector(".card .busy", state="detached")
        self.assertEqual(page.input_value(answer), "Nagyon jó, Budapesten")
        self.h.fake.chat_script = [
            extracted(**SHIRT),
            tool_reply("item_post", {"item": item.id, "platform": "vinted"}),
            reply("Be kell jelentkezned a Vintedre."),
            tool_reply("item_post", {"item": item.id, "platform": "jofogas"}),
            tool_reply("item_mark", {"item": item.id, "status": "feltöltve", "platform": "jofogas"}),
            reply("Fent van a Jófogáson."),
        ]
        page.press(answer, "Enter")
        page.wait_for_selector('#w-log .msg.jarvis:has-text("Fent van: Jófogás. Nem sikerült: Vinted.")',
                               timeout=20000)
        self.assertIn("Nagyon jó, Budapesten", page.inner_text("#w-log"), "a válaszod a naplóban")
        page.wait_for_selector('.card .site.bad:has-text("Vinted")')
        page.wait_for_selector('.card .site.on:has-text("Jófogás")')
        self.assertIn("részben fent", page.inner_text(".card .chip"))
        self.assertEqual(self.h.store.get(item.id).prompt, "Zara L-es fekete póló, 500 Ft\nNagyon jó, Budapesten")
        # a lapján látszik, miért nem sikerült, és újra lehet próbálni
        self.open_item(item.id)
        self.assertIn("Be kell jelentkezned a Vintedre.", page.inner_text('.platform:has-text("Vinted") .pnote'))
        page.wait_for_selector('.platform:has-text("Vinted") [data-action="post"]:has-text("Újra")')

    def test_price_decision_buttons(self):
        item = self.h.ready_item(price=9000, size="")
        self.h.store.set_pending(item.id, {"kind": "price", "suggested": 4500,
                                           "question": "Hasonlót 4 000 és 5 000 Ft között kínálnak. Maradjon?"})
        self.page.wait_for_selector('[data-price="keep"]:has-text("Marad: 9 000 Ft")')
        self.h.fake.chat_script = [extracted(**{**SHIRT, "size": ""})]   # utána a méretre kérdez rá
        self.page.click('[data-price="suggested"]:has-text("Legyen: 4 500 Ft")')
        self.page.wait_for_selector('.card .ask p:has-text("még kell: méret (Vinted)")')
        stored = self.h.store.get(item.id)
        self.assertEqual(stored.price, 4500)
        self.assertIn("price", stored.edited)

    def test_settings_and_login(self):
        page = self.page
        page.check('#w-sites input[value="hardverapro"]')
        page.click("#w-setup summary")
        page.fill("#s-city", "Szeged")
        page.select_option("#s-parcel", "közepes")
        page.fill("#s-shipping", "Foxpost 1290 Ft")
        page.click("#s-save")
        page.wait_for_selector('.toast:has-text("Elmentettem")')
        self.assertEqual(self.h.app.work.settings["city"], "Szeged")
        self.assertEqual(self.h.app.work.settings["parcel_size"], "közepes")
        self.assertIn("az Alapadatokból: Szeged.", page.inner_text("#w-needs"))
        self.assertIn("az Alapadatokból: Foxpost 1290 Ft.", page.inner_text("#w-needs"))
        page.click('[data-login="vatera"]')
        page.wait_for_selector('#w-log .msg.jarvis:has-text("Megnyitottam a(z) Vatera oldalt")', timeout=20000)

    def test_listing_page_edits_targets_and_status(self):
        item = self.h.ready_item()
        page = self.page
        page.wait_for_selector(f'.card[data-id="{item.id}"]')
        self.open_item(item.id)
        page.fill("#f-title", "Zara póló, újszerű")
        page.press("#f-title", "Tab")
        self.wait_for(lambda: self.h.store.get(item.id).title == "Zara póló, újszerű")
        self.assertIn("title", self.h.store.get(item.id).edited, "Jarvis ezt már nem írja felül")
        page.check('[data-target="vatera"]')
        page.wait_for_selector("#ask-dialog[open]")
        page.click("#ask-yes")
        self.wait_for(lambda: self.h.store.get(item.id).targets == ["vinted", "vatera"])
        page.click('.platform:has-text("Vinted") [data-action="toggle"]')
        page.wait_for_selector('.platform:has-text("Vinted") .pstatus.on')
        self.assertEqual(self.h.store.get(item.id).status, "részben fent")
        page.click('[data-action="sold"]')
        # Eladva: megkérdezi, hol kelt el, és levegye-e a többi oldalról; most nem kérjük a levételt.
        page.wait_for_selector("#sold-dialog[open]")
        self.assertIn("Vinted", page.inner_text("#sold-where"))
        page.uncheck("#sold-delist")
        page.click("#sold-yes")
        page.wait_for_selector('.toast:has-text("Ne felejtsd el levenni innen: Vinted")')
        self.assertTrue(self.h.store.get(item.id).sold)
        page.click('[data-action="delete"]')
        page.wait_for_selector("#ask-dialog[open]")
        page.click("#ask-yes")
        self.wait_for(lambda: not self.h.store.all())
        page.wait_for_selector("#w-compose", state="visible")

    def test_dossier_button_on_the_listing_page(self):
        item = self.h.ready_item()
        work = self.h.app.work
        self.h.store.mark(item.id, "vinted", "hiba", note="Nem találom a kategóriát.")
        name = work.dossiers.create(task="post", platform="vinted", item=self.h.store.get(item.id),
                                    reason="Nem találom a kategóriát.", mode="modell")
        self.h.store.attach_dossier(item.id, "vinted", name)
        self.page.wait_for_selector(f'.card[data-id="{item.id}"]')
        self.open_item(item.id)
        button = '.platform:has-text("Vinted") [data-action="dossier"]'
        self.assertEqual(self.page.locator('[data-action="dossier"]').count(), 1, "csak ott, ahol elakadt")
        with mock.patch("jarvis_core.gui.open_folder", return_value=False) as opened:
            self.page.click(button)
            toast = self.page.wait_for_selector('.toast:has-text("A hibacsomag itt van")')
        self.assertIn(name, toast.inner_text(), "ha az Intéző nem nyílik meg, megmondja, hol van")
        opened.assert_called_once_with(work.dossiers.path(name))
        shutil.rmtree(work.dossiers.path(name))
        self.page.click(button)
        self.page.wait_for_selector('.toast:has-text("már nincs meg")')

    def test_photos_on_the_listing_page_survive_a_refresh(self):
        item = self.h.ready_item()
        self.page.wait_for_selector(f'.card[data-id="{item.id}"]')
        self.open_item(item.id)
        with self.page.expect_file_chooser() as chooser:
            self.page.click('[data-action="pickphotos"]')
        # Amíg a fájlválasztó nyitva van, a lap frissül (pl. Jarvis dolgozik rajta);
        # a kiválasztott fotó ettől még nem veszhet el.
        self.h.store.set_busy(item.id, "Gondolkodom…")
        self.page.wait_for_selector('.detail .busy:has-text("Gondolkodom")')
        self.h.store.set_busy(item.id, "")
        chooser.value.set_files(str(self.h.photo))
        self.wait_for(lambda: len(self.h.store.get(item.id).photos) == 2)
        self.page.wait_for_selector(".detail .photo:nth-child(2) img")
        self.page.click('.detail .photo:nth-child(2) [data-action="cover"]')
        self.wait_for(lambda: self.h.store.get(item.id).photos == ["02.jpg", "01.jpg"])

    def test_dropped_photos_go_to_the_new_listing(self):
        b64 = base64.b64encode(jpeg()).decode()
        self.page.evaluate("""(b64) => {
            const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
            const dt = new DataTransfer();
            dt.items.add(new File([bytes], "ledobott.jpg", { type: "image/jpeg" }));
            const area = document.getElementById("w-area");
            area.dispatchEvent(new DragEvent("dragenter", { dataTransfer: dt, bubbles: true }));
            area.dispatchEvent(new DragEvent("drop", { dataTransfer: dt, bubbles: true, cancelable: true }));
        }""", b64)
        self.page.wait_for_selector("#w-new-photos .photo img")
        self.assertEqual(self.h.store.all(), [], "csak a Mehet gombbal jön létre a hirdetés")
        self.page.click('#w-new-photos [data-unpick="0"]')
        self.page.wait_for_selector("#w-new-photos .photo", state="detached")

    def test_work_chat_and_mode_memory(self):
        self.h.fake.chat_script = [reply("Még nincs tárgyad.")]
        self.page.fill("#text", "Hány tárgyam van?")
        self.page.press("#text", "Enter")
        self.page.wait_for_selector("#w-log .msg.jarvis")
        self.assertIn("Még nincs tárgyad.", self.page.inner_text("#w-log"))
        self.assertIn("Munka menü", self.h.fake.chat_bodies()[0]["messages"][0]["content"])
        self.assertEqual(self.page.locator("#log .msg.jarvis").count(), 0, "a Beszélgetés naplója tiszta marad")
        self.page.reload()
        self.page.wait_for_selector('body[data-mode="work"]', state="attached", timeout=10000)
        self.assertEqual(self.h.app.mode, "work", "a hangos kérések is a Munka menübe mennek")
        self.page.click("#tab-chat")
        self.page.wait_for_selector('body[data-mode="chat"]', state="attached")
        self.wait_for(lambda: self.h.app.mode == "chat")


if __name__ == "__main__":
    unittest.main(verbosity=2)
