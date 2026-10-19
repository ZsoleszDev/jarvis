"""A hibacsomag tesztjei: ha egy feltöltés, levétel vagy visszajátszás elakad, minden, ami a javításhoz
kell (képernyőkép, oldalnézet, lépések, a modell beszélgetése) egy mappába kerül, és a hirdetés
lapjáról megnyitható. Ha a közzétételt te nem engedélyezted, az nem hiba: akkor nem készül csomag.

A teljes körös tesztek valódi (láthatatlan) Chromiumot indítanak helyi tesztoldalakkal; ha a
Playwright vagy a Chromium nincs telepítve, ezek kimaradnak. Internet és modell nem kell.

Futtatás:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import http.client
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_offline import reply, tool_reply  # noqa: E402
from test_replay import ReplayHarness, click, form, last_tool  # noqa: E402
from test_web_browser import HAS_CHROMIUM  # noqa: E402

from jarvis_core.dossier import Dossiers, format_conversation, format_steps  # noqa: E402
from jarvis_core.gui import EventBus, GuiServer, GuiUI  # noqa: E402
from jarvis_core.selling import FAILED, POSTED  # noqa: E402


class FakeItem:
    def __init__(self, item_id: str = "a1b2c3d4"):
        self.id, self.name, self.kind, self.category = item_id, "Zara póló", "ruha", "Nők > Ruhák > Pólók"
        self.price, self.targets = 500, ["vinted"]


class FakeBrowser:
    url = "https://www.vinted.hu/items/new"

    def __init__(self, fail: bool = False):
        self.fail = fail

    def capture(self):
        if self.fail:
            raise RuntimeError("a lap bezárult")
        return b"\xff\xd8kep", "Oldal: Új hirdetés\n[3] gomb \"Feltöltés\""


STEPS = [
    {"do": "goto", "url": "https://www.vinted.hu/items/new", "final": "https://www.vinted.hu/items/new"},
    {"do": "upload", "el": None, "count": 3},
    {"do": "type", "el": {"kind": "input:text", "label": "Cím", "field": "title"}, "text": "Zara póló"},
    {"do": "type", "el": {"kind": "input:search", "label": "Márka"}, "text": "Zara", "submit": True},
    {"do": "click", "el": {"kind": "button", "label": "Feltöltés"}, "risky": True,
     "url": "https://www.vinted.hu/items/new", "url_after": "https://www.vinted.hu/items/123"},
    {"do": "declined", "el": {"kind": "button", "label": "Feltöltés"}},
    {"do": "back"},
]


class DossierUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "hibak"

    def tearDown(self):
        self.tmp.cleanup()

    def test_everything_needed_for_a_fix_in_one_folder(self):
        dossiers = Dossiers(self.root)
        history = [{"role": "user", "content": "Töltsd fel"},
                   {"role": "assistant", "content": "", "tool_calls": [
                       {"function": {"name": "item_post", "arguments": {"item": "a1b2c3d4", "platform": "vinted"}}}]},
                   {"role": "tool", "tool_name": "item_post", "content": "Oldal: Új hirdetés"},
                   {"role": "assistant", "content": "Nem találom a kategóriát."}]
        name = dossiers.create(task="post", platform="Vinted", item=FakeItem(), reason="Nem találom a kategóriát.",
                               mode="modell", browser=FakeBrowser(), steps=STEPS, history=history,
                               extra={"url": "https://www.vinted.hu/items/new"})
        self.assertRegex(name, r"^\d{8}-\d{6}-vinted-a1b2c3d4-post$")
        folder = dossiers.path(name)
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ["beszelgetes.txt", "info.json", "kepernyo.jpg", "lepesek.txt", "oldal.txt"])
        info = json.loads((folder / "info.json").read_text(encoding="utf-8"))
        self.assertEqual((info["task"], info["platform"], info["mode"], info["reason"]),
                         ("post", "Vinted", "modell", "Nem találom a kategóriát."))
        self.assertEqual(info["item"]["price"], 500)
        self.assertEqual(info["url"], "https://www.vinted.hu/items/new")
        self.assertEqual((folder / "kepernyo.jpg").read_bytes(), b"\xff\xd8kep")
        self.assertIn("[3] gomb", (folder / "oldal.txt").read_text(encoding="utf-8"))
        self.assertIn("4. gépelés", (folder / "lepesek.txt").read_text(encoding="utf-8"))
        talk = (folder / "beszelgetes.txt").read_text(encoding="utf-8")
        self.assertIn("### Eszköz: item_post", talk)
        self.assertIn("### Jarvis\nNem találom a kategóriát.", talk)

        again = dossiers.create(task="post", platform="Vinted", item=FakeItem(), reason="újra", mode="modell")
        if again.startswith(name[:15]):   # ugyanabban a másodpercben: sorszámot kap, nem írja felül
            self.assertEqual(again, name + "-2")
        self.assertEqual(len(dossiers.names()), 2)

    def test_without_a_browser_or_when_the_capture_fails(self):
        dossiers = Dossiers(self.root)
        folder = dossiers.path(dossiers.create(task="delist", platform="vinted", item=FakeItem(), reason="x",
                                               mode="begyakorolt lépések", browser=FakeBrowser(fail=True)))
        info = json.loads((folder / "info.json").read_text(encoding="utf-8"))
        self.assertEqual(info["capture_error"], "RuntimeError: a lap bezárult")
        self.assertFalse((folder / "kepernyo.jpg").exists())
        folder = dossiers.path(dossiers.create(task="post", platform="vinted", item=FakeItem("e5f6a7b8"), reason="y",
                                               mode="modell"))
        self.assertEqual((folder / "oldal.txt").read_text(encoding="utf-8"), "(a böngésző nem futott)")
        self.assertEqual((folder / "beszelgetes.txt").read_text(encoding="utf-8"), "(a modell ennél nem dolgozott)")

    def test_the_reason_for_a_missing_screenshot_is_kept(self):
        class NoScreenshot(FakeBrowser):
            capture_error = "MemoryError"

            def capture(self):
                return None, "Oldal: zsoleszdev - member profile | Vinted"

        dossiers = Dossiers(self.root)
        folder = dossiers.path(dossiers.create(task="post", platform="vinted", item=FakeItem(), reason="x",
                                               mode="modell", browser=NoScreenshot()))
        info = json.loads((folder / "info.json").read_text(encoding="utf-8"))
        self.assertEqual(info["capture_error"], "MemoryError", "látszik, miért nincs kép")
        self.assertFalse((folder / "kepernyo.jpg").exists())
        self.assertIn("member profile", (folder / "oldal.txt").read_text(encoding="utf-8"))
        folder = dossiers.path(dossiers.create(task="post", platform="vinted", item=FakeItem("e5f6a7b8"), reason="y",
                                               mode="modell", browser=FakeBrowser()))
        self.assertNotIn("capture_error", json.loads((folder / "info.json").read_text(encoding="utf-8")))

    def test_only_the_last_ones_are_kept(self):
        dossiers = Dossiers(self.root, keep=3)
        names = []
        for number in range(5):
            names.append(dossiers.create(task="post", platform="vinted", item=FakeItem(f"0000000{number}"),
                                         reason="x", mode="modell"))
        self.assertEqual(sorted(dossiers.names()), sorted(names)[-3:])
        (self.root / "valami-mas").mkdir()          # amit nem ő készített, ahhoz nem nyúl
        dossiers.create(task="post", platform="vinted", item=FakeItem("00000009"), reason="x", mode="modell")
        self.assertTrue((self.root / "valami-mas").is_dir())
        self.assertEqual(len(dossiers.names()), 3)

    def test_path_stays_inside_the_folder(self):
        dossiers = Dossiers(self.root)
        name = dossiers.create(task="post", platform="vinted", item=FakeItem(), reason="x", mode="modell")
        self.assertEqual(dossiers.path(name), self.root / name)
        for bad in ("", "..", "../hibak", f"../{name}", name + "/..", "20260101-120000-vinted-a1b2c3d4-post",
                    "C:\\Windows"):
            with self.assertRaises(FileNotFoundError, msg=bad):
                dossiers.path(bad)

    def test_readable_steps_and_conversation(self):
        steps = format_steps(STEPS)
        for expected in ("  1. megnyitás: https://www.vinted.hu/items/new", "  2. fotók: 3 db",
                         "  3. gépelés: input:text „Cím” (mező: title) ← „Zara póló”",
                         "  4. gépelés: input:search „Márka” ← „Zara” + Enter",
                         "  5. kattintás: button „Feltöltés” [közzététel/törlés] → https://www.vinted.hu/items/123",
                         "  6. NEM ENGEDÉLYEZTED: button „Feltöltés”", "  7. vissza az előző oldalra"):
            self.assertIn(expected, steps)
        self.assertEqual(format_steps([]), "(nincs feljegyzett lépés)")
        long_view = "Oldal: x\n" + "y" * 5000
        talk = format_conversation([{"role": "tool", "tool_name": "browser_open", "content": long_view}])
        self.assertIn("karakter kihagyva", talk)
        self.assertLess(len(talk), 2700, "a hosszú oldalnézetek megrövidítve")


# --- a teljes kör: elakadás egy helyi „hirdetési oldalon” -------------------------------------------------

@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class DossierFlowTests(ReplayHarness, unittest.TestCase):
    def folder(self, item_id: str) -> Path:
        name = self.store.get(item_id).platforms["vinted"].get("dossier")
        self.assertTrue(name, "a hirdetés sorában ott a hibacsomag")
        return self.work.dossiers.path(name)

    def test_a_failed_post_leaves_a_dossier(self):
        item = self.item()
        self.fake.chat_script = [tool_reply("item_post", {"item": item.id, "platform": "vinted"}),
                                 reply("Nem találom a kategória mezőt, ezért abbahagytam.")]
        self.app.run_work({"action": "post", "ids": [item.id], "platform": "vinted"})
        self.assertEqual(self.store.get(item.id).platforms["vinted"]["status"], FAILED)
        folder = self.folder(item.id)
        info = json.loads((folder / "info.json").read_text(encoding="utf-8"))
        self.assertEqual((info["task"], info["mode"]), ("post", "modell"))
        self.assertEqual(info["reason"], "Nem találom a kategória mezőt, ezért abbahagytam.")
        self.assertIn("urlap.html", info["url"])
        self.assertTrue((folder / "kepernyo.jpg").read_bytes().startswith(b"\xff\xd8"), "JPEG kép az oldalról")
        self.assertIn("Új hirdetés", (folder / "oldal.txt").read_text(encoding="utf-8"))
        self.assertIn("megnyitás:", (folder / "lepesek.txt").read_text(encoding="utf-8"))
        talk = (folder / "beszelgetes.txt").read_text(encoding="utf-8")
        self.assertIn("### Eszköz: item_post", talk)
        self.assertIn("Nem találom a kategória mezőt", talk)
        self.assertTrue(any("Hibacsomag a javításhoz" in e[1] for e in self.ui.of("info")))

    def test_a_declined_publish_is_not_an_error(self):
        item = self.item()
        self.ui.confirm_answer = False
        self.fake.chat_script = [tool_reply("item_post", {"item": item.id, "platform": "vinted"}),
                                 click("Feltöltés"), reply("Nem engedélyezted a közzétételt.")]
        self.app.run_work({"action": "post", "ids": [item.id], "platform": "vinted"})
        state = self.store.get(item.id).platforms["vinted"]
        self.assertEqual(state["status"], FAILED)
        self.assertNotIn("dossier", state)
        self.assertEqual(self.work.dossiers.names(), [])

    def test_a_replay_that_gets_stuck_leaves_its_steps(self):
        first = self.item()
        self.learn_posting(first)
        self.assertEqual(self.work.dossiers.names(), [], "ami sikerült, arról nem készül csomag")
        self.site.write("urlap.html", form("Hirdetés címe", "cim"))   # megváltozott az oldal
        second = self.item(title="H&M fehér póló, M", price=700, brand="H&M")
        # A javító kérdésre a modell sem tud jobbat (0), ezért a nyitott űrlapon ő folytatja.
        self.fake.chat_script = [reply('{"choice": 0}'), reply("Most nem sikerült, mert hiányzik egy mező.")]
        self.app.run_work({"action": "post", "ids": [second.id], "platform": "vinted"})
        names = self.work.dossiers.names()
        self.assertEqual(len(names), 2, "egy a begyakorolt lépésekről, egy a modellről")
        replay_folder = self.work.dossiers.path(names[0])
        info = json.loads((replay_folder / "info.json").read_text(encoding="utf-8"))
        self.assertEqual(info["mode"], "begyakorolt lépések")
        self.assertTrue(info["reason"])
        self.assertIn("urlap.html", (replay_folder / "lepesek.txt").read_text(encoding="utf-8"))
        self.assertEqual((replay_folder / "beszelgetes.txt").read_text(encoding="utf-8"),
                         "(a modell ennél nem dolgozott)")
        self.assertEqual(self.folder(second.id).name, names[1], "a hirdetés a legutóbbit mutatja")

    def test_a_later_success_hides_the_old_dossier(self):
        item = self.item()
        self.fake.chat_script = [tool_reply("item_post", {"item": item.id, "platform": "vinted"}),
                                 reply("Nem sikerült.")]
        self.app.run_work({"action": "post", "ids": [item.id], "platform": "vinted"})
        self.folder(item.id)
        self.learn_posting(item)    # az Újra gombbal már sikerül
        state = self.store.get(item.id).platforms["vinted"]
        self.assertEqual(state["status"], POSTED)
        self.assertNotIn("dossier", state)

    def test_the_dossier_button_opens_the_folder(self):
        item = self.item()
        self.fake.chat_script = [tool_reply("item_post", {"item": item.id, "platform": "vinted"}),
                                 reply("Nem sikerült.")]
        self.app.run_work({"action": "post", "ids": [item.id], "platform": "vinted"})
        folder = self.folder(item.id)
        bus = EventBus()
        server = GuiServer(self.app, GuiUI(bus, "Jarvis"), bus)
        server.start()
        try:
            def post(body: dict) -> tuple[int, dict]:
                conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
                conn.request("POST", "/api/work/dossier", body=json.dumps(body).encode(),
                             headers={"X-Jarvis-Token": server.token, "Content-Type": "application/json"})
                response = conn.getresponse()
                data = json.loads(response.read())
                conn.close()
                return response.status, data

            with mock.patch("jarvis_core.gui.open_folder", return_value=True) as opened:
                status, data = post({"id": item.id, "platform": "vinted"})
            self.assertEqual((status, data["opened"], data["path"]), (200, True, str(folder)))
            opened.assert_called_once_with(folder)
            self.assertEqual(post({"id": item.id, "platform": "jofogas"})[0], 404, "ott nincs csomag")
            self.store.get(item.id).platforms["vinted"]["dossier"] = "../../Windows"
            with mock.patch("jarvis_core.gui.open_folder") as opened:
                self.assertEqual(post({"id": item.id, "platform": "vinted"})[0], 404)
            opened.assert_not_called()
            self.store.get(item.id).platforms["vinted"]["dossier"] = folder.name
            shutil.rmtree(folder)
            status, data = post({"id": item.id, "platform": "vinted"})
            self.assertEqual(status, 404)
            self.assertIn("már nincs meg", data["error"])
        finally:
            server.stop()


# --- a közzététel megtörtént, de a modell nem jelölte meg ------------------------------------------------

@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class PublishCheckTests(ReplayHarness, unittest.TestCase):
    """A hibacsomag esete (Vinted): a hirdetés felkerült, de a modell válasza félbemaradt, és a tárgyat nem
    jelölte meg. Régen ez „hiba” lett, és az Újra gomb másodszor is feltöltötte volna."""

    TRUNCATED = "A hirdetés sikeresen feltöltődött! A hirdetés a „Processing” állapotban van (30-as link:"

    def publish_then(self, item, ending, *check):
        self.fake.chat_script = [tool_reply("item_post", {"item": item.id, "platform": "vinted"}),
                                 click("Feltöltés"), ending, *check]
        self.app.run_work({"action": "post", "ids": [item.id], "platform": "vinted"})
        self.assertEqual(self.fake.chat_script, [], "minden modell-lépés lefutott")

    def mark(self, item, seen=None):
        def step(body):
            if seen is not None:
                seen.append(body)
            return 200, tool_reply("item_mark", {"item": item.id, "status": "feltöltve", "platform": "vinted",
                                                 "url": self.site.url("kesz.html")})
        return step

    def checks(self) -> list[str]:
        return [e[1] for e in self.ui.of("info") if e[1].startswith("Ellenőrzés:")]

    def test_an_unmarked_publish_is_checked_not_failed(self):
        item = self.item()
        seen: list[dict] = []
        self.publish_then(item, reply(self.TRUNCATED), self.mark(item, seen), reply("Fent van a Vinteden."))
        state = self.store.get(item.id).platforms["vinted"]
        self.assertEqual(state["status"], POSTED)
        self.assertTrue(state["url"].startswith(self.site.url("kesz.html")))
        self.assertEqual(self.work.dossiers.names(), [], "ami sikerült, arról nem készül hibacsomag")
        self.assertEqual(self.checks(), ["Ellenőrzés: Zara fekete póló, L → Vinted"])
        request = seen[0]["messages"][-1]["content"]
        self.assertIn("közzétételét a felhasználó jóváhagyta", request)
        self.assertIn("item_post eszközt ne használd", request)
        self.assertTrue(any(m["content"].startswith("Töltsd fel") for m in seen[0]["messages"] if m["role"] == "user"),
                        "ugyanabban a beszélgetésben: a modell látja, mit csinált")
        self.assertEqual(len(self.confirms()), 1, "egyszer tette közzé")

    def test_a_check_that_cannot_tell_leaves_a_dossier(self):
        item = self.item()
        self.publish_then(item, reply(self.TRUNCATED), reply("Nem tudom eldönteni, fent van-e."))
        state = self.store.get(item.id).platforms["vinted"]
        self.assertEqual(state["status"], FAILED)
        self.assertIn("Lehet, hogy mégis fent van", state["note"], "a kártya szól: nem az Újra kell")
        self.assertIn("Nem tudom eldönteni", state["note"])
        talk = (self.work.dossiers.path(state["dossier"]) / "beszelgetes.txt").read_text(encoding="utf-8")
        self.assertIn("(30-as link:", talk, "a hibacsomagban az eredeti feltöltés is benne van")
        self.assertIn("közzétételét a felhasználó jóváhagyta", talk)

    def test_no_second_upload_while_checking(self):
        item = self.item()
        results: list[str] = []

        def after_refusal(body):
            results.append(last_tool(body))
            return self.mark(item)(body)

        self.publish_then(item, reply(self.TRUNCATED), tool_reply("item_post", {"item": item.id, "platform": "vinted"}),
                          after_refusal, reply("Fent van."))
        self.assertIn("újra ne töltsd fel", results[0])
        self.assertEqual(self.store.get(item.id).platforms["vinted"]["status"], POSTED)
        self.assertEqual(len(self.confirms()), 1, "nem tette közzé másodszor")
        self.assertIsNone(self.work.checking)

    def test_a_reply_cut_off_after_publishing_is_checked_too(self):
        item = self.item()
        cut = reply(self.TRUNCATED)
        cut["stream_cut"] = True   # az Ollama a válasz vége előtt lezárta a kapcsolatot
        self.publish_then(item, cut, self.mark(item), reply("Fent van a Vinteden."))
        self.assertEqual(self.store.get(item.id).platforms["vinted"]["status"], POSTED)
        self.assertEqual(self.ui.of("error"), [], "az ellenőrzés rendben volt: nem hibaként végződik")

    def test_no_check_without_an_approved_publish(self):
        item = self.item()
        self.fake.chat_script = [tool_reply("item_post", {"item": item.id, "platform": "vinted"}),
                                 reply("Be kell jelentkezned a Vintedre.")]
        self.app.run_work({"action": "post", "ids": [item.id], "platform": "vinted"})
        self.assertEqual(self.fake.chat_script, [])
        self.assertEqual(self.checks(), [])
        self.assertEqual(self.store.get(item.id).platforms["vinted"]["note"], "Be kell jelentkezned a Vintedre.")

    def test_a_new_attempt_does_not_see_the_old_note(self):
        item = self.item()
        self.store.mark(item.id, "vinted", FAILED, note="A Vinted frissített felhasználói feltételeket kér.")
        self.app.toolbox.call("item_post", {"item": item.id, "platform": "vinted"})
        entry = self.store.get(item.id).platforms["vinted"]
        self.assertEqual(entry["status"], FAILED, "az állapot marad, amíg ki nem derül, sikerült-e")
        self.assertNotIn("note", entry, "az előző próbálkozás megjegyzése nem vezeti félre a modellt")
        self.assertNotIn("feltételeket", self.app.toolbox.call("item_get", {"item": item.id}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
