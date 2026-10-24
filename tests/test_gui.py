"""A grafikus felület tesztjei: valódi helyi szerver, valódi (láthatatlan) Chromium, hamis modell.

Ha a Playwright vagy a Chromium nincs telepítve, a böngészős tesztek kimaradnak.

Futtatás:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_offline import FakeOllama, make_cfg, reply, tool_reply  # noqa: E402
from test_web_browser import HAS_CHROMIUM  # noqa: E402

from jarvis_core import gpu  # noqa: E402
from jarvis_core.app import JarvisApp  # noqa: E402
from jarvis_core.assistant import Assistant  # noqa: E402
from jarvis_core.gui import EventBus, GuiServer, GuiUI  # noqa: E402
from jarvis_core.tools import Toolbox  # noqa: E402


class GuiHarness:
    """Jarvis grafikus felülettel, de modell nélkül (a hamis Ollamával)."""

    def __init__(self, allow_powershell: bool = False):
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        self.cfg.tools.allow_powershell = allow_powershell
        self.bus = EventBus()
        self.ui = GuiUI(self.bus, "Jarvis")
        self.app = JarvisApp(self.cfg, self.ui)
        self.app.toolbox = Toolbox(self.cfg, notify=self.app.notify, confirm=self.app.confirm)
        self.app.assistant = Assistant(self.cfg, self.app.llm, self.app.toolbox, "rendszer",
                                       on_tool_call=self.app._on_tool_done,
                                       on_tool_start=self.app._on_tool_start)
        self.app.assistant.stop_event = self.app.stop_event
        self.server = GuiServer(self.app, self.ui, self.bus)
        self.server.start()
        self.bus.set_hello(self.app.describe())
        self.app.start_agent()

    def close(self) -> None:
        self.app.shutdown()
        self.server.stop()
        self.fake.close()
        self.tmp.cleanup()


def request(port: int, method: str, path: str, headers: dict | None = None, body: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=data, headers=headers or {})
    response = conn.getresponse()
    status = response.status
    response.read()
    conn.close()
    return status


def call(port: int, method: str, path: str, headers: dict | None = None, body: dict | None = None):
    """Mint a request, de a választ is visszaadja: (állapotkód, JSON vagy None)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=data, headers=headers or {})
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    try:
        return response.status, json.loads(raw)
    except ValueError:
        return response.status, None


def two_cards(loads: dict[int, int]):
    """Hamis olvasó a GPU-sávokhoz: a terhelést a teszt állítja (loads)."""
    return lambda: [gpu.GpuInfo(0, "NVIDIA GeForce RTX 3060 Ti", loads[0], 6420, 8192, 52),
                    gpu.GpuInfo(1, "NVIDIA GeForce RTX 5060 Ti", loads[1], 15565, 16311, 64)]


class GuiSecurityTests(unittest.TestCase):
    """Más weboldalak ne tudjanak parancsot küldeni a Jarvisnak."""

    @classmethod
    def setUpClass(cls):
        cls.h = GuiHarness()
        cls.port = cls.h.server.port
        cls.token = cls.h.server.token

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_page_needs_token(self):
        self.assertEqual(request(self.port, "GET", "/"), 403)
        self.assertEqual(request(self.port, "GET", "/?t=rossz"), 403)
        self.assertEqual(request(self.port, "GET", f"/?t={self.token}"), 200)

    def test_foreign_host_rejected(self):  # DNS-rebinding elleni védelem
        status = request(self.port, "GET", f"/?t={self.token}", headers={"Host": "gonosz.example:80"})
        self.assertEqual(status, 403)

    def test_post_needs_token_and_same_origin(self):
        self.h.fake.chat_script = [reply("ok")]
        self.assertEqual(request(self.port, "POST", "/api/send", body={"text": "szia"}), 403)
        headers = {"X-Jarvis-Token": self.token, "Origin": "https://gonosz.example",
                   "Content-Type": "application/json"}
        self.assertEqual(request(self.port, "POST", "/api/send", headers=headers, body={"text": "szia"}), 403)
        headers["Origin"] = f"http://127.0.0.1:{self.port}"
        self.assertEqual(request(self.port, "POST", "/api/send", headers=headers, body={"text": "szia"}), 202)


class GuiPanelApiTests(unittest.TestCase):
    """A GPU-sávok és az Automatikus munkák ablak végpontjai."""

    def setUp(self):
        self.h = GuiHarness()
        self.port, self.token = self.h.server.port, self.h.server.token

    def tearDown(self):
        self.h.close()

    def get(self, path: str):
        return call(self.port, "GET", f"{path}?t={self.token}")

    def post(self, path: str, body: dict):
        return call(self.port, "POST", path, body=body,
                    headers={"X-Jarvis-Token": self.token, "Content-Type": "application/json"})

    def test_gpu_history_needs_the_token_and_can_be_switched_off(self):
        self.h.server.gpus = gpu.GpuMonitor(reader=two_cards({0: 7, 1: 87}))
        self.assertEqual(call(self.port, "GET", "/api/gpus")[0], 403)
        status, data = self.get("/api/gpus")
        self.assertEqual(status, 200)
        self.assertTrue(data["available"])
        self.assertEqual([g["name"] for g in data["gpus"]], ["RTX 3060 Ti", "RTX 5060 Ti"])
        self.assertEqual(data["gpus"][1]["total_mb"], 16311)
        self.assertEqual(data["gpus"][1]["samples"][-1][1:], [87, 15565, 64])
        self.h.cfg.gui.gpu_bars = False
        self.assertFalse(self.get("/api/gpus")[1]["available"], "[gui] gpu_bars = false")

    def test_watches_are_listed_and_deleted_by_id(self):
        self.assertEqual(call(self.port, "GET", "/api/jobs")[0], 403)
        self.assertFalse(self.get("/api/jobs")[1]["ready"], "amíg a háttérmunka nem indult el")
        self.h.app._start_background()
        first = self.h.app.watches.add("python állás Debrecen", "profession.hu")
        second = self.h.app.watches.add("RTX 3080")
        data = self.get("/api/jobs")[1]
        self.assertEqual([w["id"] for w in data["watches"]], [first.id, second.id])
        self.assertEqual(data["watches"][0]["label"], first.describe())
        self.assertEqual([j["key"] for j in data["jobs"]], ["watches"])
        self.assertTrue(data["jobs"][0]["on"])
        self.assertEqual(self.post("/api/jobs/watch-delete", {"id": "python"})[0], 404, "csak pontos azonosítóval")
        status, data = self.post("/api/jobs/watch-delete", {"id": first.id})
        self.assertEqual(status, 200)
        self.assertEqual(data["removed"][0]["query"], "python állás Debrecen")
        self.assertEqual([w["id"] for w in data["watches"]], [second.id])
        self.assertEqual(self.post("/api/jobs/watch-delete", {"ids": [second.id]})[1]["watches"], [])
        saved = Path(self.h.cfg.data_root) / "figyelok.json"
        self.assertEqual(json.loads(saved.read_text(encoding="utf-8")), [], "a fájlból is törlődött")

    def test_background_jobs_can_be_switched_off(self):
        self.h.app._start_background()
        status, data = self.post("/api/jobs/toggle", {"key": "watches", "on": False})
        self.assertEqual(status, 200)
        self.assertFalse(data["jobs"][0]["on"])
        self.assertFalse(self.h.app.scheduler.is_on("watches"))
        self.assertEqual(self.post("/api/jobs/toggle", {"key": "nincs ilyen", "on": False})[0], 404)
        self.post("/api/jobs/toggle", {"key": "watches", "on": True})
        self.assertTrue(self.h.app.scheduler.is_on("watches"))


class EventBusTests(unittest.TestCase):
    def test_replay_keeps_last_tool_state_and_current_state(self):
        bus = EventBus()
        bus.publish({"type": "user", "text": "szia"})
        bus.publish({"type": "tool", "id": 1, "status": "running"})
        bus.publish({"type": "tool", "id": 1, "status": "done"})
        bus.publish({"type": "delta", "text": "Sz"}, record=False)
        bus.set_state({"type": "state", "state": "idle"})
        client = bus.subscribe()
        events = [client.get_nowait() for _ in range(client.qsize())]
        types = [e["type"] for e in events]
        self.assertEqual(types, ["hello", "user", "tool", "state"])
        self.assertEqual(events[2]["status"], "done")


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class GuiBrowserTests(unittest.TestCase):
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
        self.h = GuiHarness(allow_powershell=True)
        self.page = self.browser.new_page(viewport={"width": 560, "height": 900})
        self.page.goto(self.h.server.url)
        self.page.wait_for_selector("#meta:has-text('qwen')")

    def tearDown(self):
        self.page.close()
        self.h.close()

    def wait_idle(self):
        self.page.wait_for_selector('body[data-state="idle"]', state="attached", timeout=15000)
        deadline = time.monotonic() + 10
        while self.h.app.busy and time.monotonic() < deadline:
            time.sleep(0.02)

    def test_header_and_start(self):
        self.assertEqual(self.page.title(), "Jarvis")
        self.assertEqual(self.page.inner_text("#meta"), "qwen3.8:27b")
        self.assertEqual(self.page.inner_text("#state"), "Készen állok")
        self.assertEqual(self.page.locator("#listen").count(), 0, "nincs Hallgass meg gomb")
        self.assertTrue(self.page.locator("#stop").is_hidden(), "az Állj csak dolgozás közben látszik")
        self.assertTrue(self.page.locator("#home-tiles").is_hidden(), "a Munka menü nélkül nincsenek csempék")
        self.assertEqual(self.page.inner_text("#empty-hint"), "Kérdezz bármit.")

    def test_ask_and_get_streamed_answer_with_tool(self):
        self.h.fake.chat_script = [tool_reply("get_datetime", {}), reply("Most este hét óra van, uram.")]
        self.page.fill("#text", "Mennyi az idő?")
        self.page.press("#text", "Enter")
        self.page.wait_for_selector(".msg.jarvis")
        self.wait_idle()
        self.assertEqual(self.page.inner_text(".msg.you"), "Mennyi az idő?")
        self.assertEqual(self.page.inner_text(".msg.jarvis"), "Most este hét óra van, uram.")
        tool = self.page.locator(".msg.tool")
        self.assertEqual(tool.count(), 1)
        self.assertIn("done", tool.get_attribute("class"))
        self.assertIn("Pontos idő", tool.inner_text())
        # Kimásolva a lépés és az eredménye nem íródik egybe ("www.alza.huAz oldal…"): sima szövegként
        # külön sorba kerülnek, formázott beillesztésnél (HTML) egy " — " választja el őket, ami nem látszik.
        copied = self.page.evaluate(
            "() => { const r = document.createRange(); r.selectNodeContents(document.querySelector('.msg.tool'));"
            " const s = getSelection(); s.removeAllRanges(); s.addRange(r); return s.toString(); }")
        label = self.page.inner_text(".msg.tool .label")
        summary = self.page.inner_text(".msg.tool .summary .text")
        self.assertTrue(summary)
        self.assertIn(label + "\n" + summary, copied)
        as_html = self.page.evaluate("() => document.querySelector('.msg.tool').textContent")
        self.assertEqual(as_html, label + " — " + summary)
        self.assertEqual(self.page.locator(".msg.tool .sep").bounding_box()["width"], 0)
        self.assertTrue(self.page.locator("#empty").is_hidden())

    def test_the_stop_button_shows_next_to_send_while_working(self):
        def slow(body):
            time.sleep(3)
            return 200, reply("Késő válasz.")

        self.h.fake.chat_script = [slow]
        self.page.fill("#text", "Lassú kérdés")
        self.page.press("#text", "Enter")
        self.page.wait_for_selector("#ask #stop:visible", timeout=5000)
        boxes = [self.page.locator(sel).bounding_box() for sel in ("#send", "#stop")]
        self.assertAlmostEqual(boxes[0]["y"], boxes[1]["y"], delta=4, msg="egy sorban a Küldéssel")
        self.assertGreater(boxes[1]["x"], boxes[0]["x"], "a Küldés után")
        self.page.click("#stop")
        self.wait_idle()
        self.page.wait_for_selector("#stop", state="hidden")
        self.page.wait_for_selector(".msg.note:has-text('Megszakítva.')")

    def test_confirmation_dialog_allow(self):
        self.h.fake.chat_script = [tool_reply("run_powershell", {"command": "Get-Date"}), reply("Kész.")]
        with mock.patch("jarvis_core.tools.shutil.which", return_value=None):
            self.page.fill("#text", "futtasd a Get-Date parancsot")
            self.page.press("#text", "Enter")
            self.page.wait_for_selector("dialog#confirm[open]")
            self.assertIn("Get-Date", self.page.inner_text("#confirm-text"))
            self.page.click("#allow")
            self.wait_idle()
        self.assertFalse(self.page.locator("dialog#confirm").evaluate("d => d.open"))
        tool_message = next(m for m in self.h.app.assistant.history if m["role"] == "tool")
        self.assertNotIn("nem engedélyezte", tool_message["content"])

    def test_confirmation_dialog_deny_with_escape(self):
        self.h.fake.chat_script = [tool_reply("run_powershell", {"command": "Remove-Item x"}), reply("Rendben.")]
        self.page.fill("#text", "töröld")
        self.page.press("#text", "Enter")
        self.page.wait_for_selector("dialog#confirm[open]")
        self.page.keyboard.press("Escape")
        self.wait_idle()
        tool_message = next(m for m in self.h.app.assistant.history if m["role"] == "tool")
        self.assertIn("nem engedélyezte", tool_message["content"])

    def test_stop_button(self):
        def slow(body):
            time.sleep(3)
            return 200, reply("Késő válasz.")

        self.h.fake.chat_script = [slow]
        self.page.fill("#text", "valami lassú")
        self.page.press("#text", "Enter")
        self.page.wait_for_selector('body[data-state="thinking"]', state="attached")
        self.page.click("#stop")
        self.page.wait_for_selector("text=Megszakítva.", timeout=3000)
        self.wait_idle()

    def test_pause_and_resume_buttons(self):
        self.page.click("#pause")
        self.page.wait_for_selector('body[data-state="paused"]', state="attached")
        self.page.wait_for_selector('#pause:has-text("Folytatás")')
        self.assertTrue(self.h.app.paused)
        self.assertIn("Szünetelek. Leállítottam", self.page.inner_text("#log"))
        # Szünetben írt kérdésnél megkérdezi, folytassa-e, és utána válaszol.
        self.h.fake.chat_script = [reply("Itt vagyok.")]
        self.page.fill("#text", "Ott vagy?")
        self.page.press("#text", "Enter")
        self.page.wait_for_selector("#ask-dialog[open]")
        self.page.click("#ask-yes")
        self.page.wait_for_selector('.msg.jarvis:has-text("Itt vagyok.")', timeout=15000)
        self.page.wait_for_selector('#pause:has-text("Szünet")')
        self.assertFalse(self.h.app.paused)

    def test_phone_dialog_pairs_a_device(self):
        from jarvis_core import remote
        from jarvis_core.api import start_api

        self.h.cfg.api.port = 0
        api = start_api(self.h.app, self.h.ui, self.h.bus, self.h.cfg.data_root)
        self.h.server.phone = api
        tailscale = mock.patch.object(remote, "tailscale_address", return_value="").start()
        self.addCleanup(mock.patch.stopall)
        try:
            self.page.click("#phone")
            self.page.wait_for_selector("#phone-dialog[open]")
            self.page.wait_for_selector("text=Még nincs párosított eszköz.")
            self.assertIn(f"tailscale serve --bg {api.port}", self.page.inner_text("#phone-body"))
            self.page.click("#phone-pair")
            code = self.page.inner_text(".pair-code")
            self.assertRegex(code, r"^\d{6}$")
            api.auth.pair(code, "Pixel 8")
            self.page.click("#phone-close")
            self.page.click("#phone")
            self.page.wait_for_selector("text=Pixel 8")
            self.page.click("[data-revoke]")
            self.page.wait_for_selector("#ask-dialog[open]")
            self.page.click("#ask-yes")
            self.page.wait_for_selector("text=Még nincs párosított eszköz.")
            self.assertEqual(api.auth.devices(), [])
            # Ha a tailscale serve már be van állítva, a gép pontos címe látszik (ezt kell megadni a telefonon).
            tailscale.return_value = "https://gep.tail1234.ts.net"
            self.page.click("#phone-close")
            self.page.click("#phone")
            self.page.wait_for_selector(".address code:has-text('https://gep.tail1234.ts.net')")
            self.assertNotIn("tailscale serve --bg", self.page.inner_text("#phone-body"))
            tailscale.assert_called_with(api.port)
            # A kód mellé QR-kód: a telefon kamerája beolvassa (a gép címe, a # után a kód).
            self.page.click("#phone-pair")
            self.page.wait_for_selector(".pair-qr svg")
            code = self.page.inner_text(".pair-code")
            status, result = call(self.h.server.port, "POST", "/api/phone/code", body={},
                                  headers={"X-Jarvis-Token": self.h.server.token, "Content-Type": "application/json"})
            self.assertEqual(status, 200)
            self.assertEqual(result["link"], f"https://gep.tail1234.ts.net/#pair={result['code']}")
            self.assertIn("<svg", result["qr"])
            self.assertNotEqual(result["code"], code, "minden kéréssel új kód")
        finally:
            api.stop()

    def test_gpu_strips_show_the_load_and_details_on_hover(self):
        loads = {0: 7, 1: 87}
        self.h.server.gpus = gpu.GpuMonitor(reader=two_cards(loads))
        self.page.reload()
        self.page.wait_for_selector("#gpus:not([hidden]) .gpu >> nth=1")
        self.assertEqual(self.page.locator("#gpus .gpu").count(), 2, "kártyánként egy sáv")
        self.assertEqual(self.page.locator("#gpus .gpu >> nth=1").get_attribute("aria-label"),
                         "RTX 5060 Ti terhelése az utolsó percben")
        # a görbe kirajzolódik: a 87%-os kártya sávjának jobb szélén, a felső felében is van fény
        self.page.wait_for_function("""() => {
            const c = document.querySelectorAll('#gpus canvas')[1];
            if (!c || !c.width) return false;
            const d = c.getContext('2d').getImageData(Math.floor(c.width * 0.9), 0, Math.ceil(c.width * 0.1),
                                                      Math.floor(c.height / 2)).data;
            for (let i = 3; i < d.length; i += 4) if (d[i] > 0) return true;
            return false; }""", timeout=8000)
        self.page.hover("#gpus .gpu >> nth=1")
        self.page.wait_for_selector("#gpu-tip:not([hidden])")
        self.assertEqual(self.page.inner_text("#gpu-tip"), "RTX 5060 Ti · terhelés 87% · VRAM 15,2 / 16 GB · 64 °C")
        loads[1] = 12
        self.page.wait_for_selector("#gpu-tip:has-text('terhelés 12%')", timeout=5000)   # élőben frissül
        self.page.mouse.move(5, 600)
        self.page.wait_for_selector("#gpu-tip", state="hidden")

    def test_gpu_strips_stay_hidden_without_an_nvidia_card(self):
        with mock.patch.object(gpu, "make_reader", return_value=(None, 0.0)):
            self.h.server.gpus = gpu.GpuMonitor()
            self.page.reload()
            self.page.wait_for_selector("#meta:has-text('qwen')")
            self.page.wait_for_timeout(1500)
        self.assertTrue(self.page.locator("#gpus").is_hidden())
        self.assertEqual(self.page.locator("#gpus .gpu").count(), 0)

    def test_jobs_dialog_deletes_watches_and_switches_jobs_off(self):
        self.h.app._start_background()
        watches = self.h.app.watches
        keep = watches.add("RTX 3080 hirdetés", "hardverapro.hu")
        gone = watches.add("python állás Debrecen")
        self.page.click("#jobs")
        self.page.wait_for_selector("#jobs-dialog[open] [data-watch]")
        body = self.page.inner_text("#jobs-body")
        self.assertIn("„RTX 3080 hirdetés” (hardverapro.hu", body)
        self.assertIn("még nem néztem meg", body)
        self.page.click(f"[data-watch='{gone.id}']")
        self.page.wait_for_selector("#ask-dialog[open]")
        self.assertIn("python állás Debrecen", self.page.inner_text("#ask-text"))
        self.page.click("#ask-yes")
        self.page.wait_for_function(f"() => !document.querySelector(\"[data-watch='{gone.id}']\")")
        self.assertEqual([w.id for w in watches.all()], [keep.id])
        self.assertTrue(self.page.locator("#jobs-dialog").evaluate("d => d.open"), "az ablak nyitva marad")
        box = self.page.locator("[data-job='watches']")
        self.assertTrue(box.is_checked())
        box.uncheck()
        self.page.wait_for_selector("text=kikapcsolva: magától nem nézi meg")
        self.assertFalse(self.h.app.scheduler.is_on("watches"))
        self.page.click("#jobs-close")
        self.assertFalse(self.page.locator("#jobs-dialog").evaluate("d => d.open"))

    def test_reload_restores_conversation(self):
        self.h.fake.chat_script = [reply("Szia! Miben segíthetek?")]
        self.page.fill("#text", "Szia")
        self.page.press("#text", "Enter")
        self.page.wait_for_selector(".msg.jarvis")
        self.wait_idle()
        self.page.reload()
        self.page.wait_for_selector(".msg.jarvis")
        self.assertEqual(self.page.inner_text(".msg.you"), "Szia")
        self.assertEqual(self.page.inner_text(".msg.jarvis"), "Szia! Miben segíthetek?")
        self.assertEqual(self.page.locator(".msg.jarvis").count(), 1, "nem duplázódik")


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class GuiEndToEndTests(unittest.TestCase):
    """A valódi jarvis.py grafikus módban: indul, válaszol, és az ablak bezárásával leáll."""

    def test_full_session(self):
        import os
        import re
        import subprocess

        from playwright.sync_api import sync_playwright

        fake = FakeOllama()
        fake.chat_script = [tool_reply("get_datetime", {}), reply("Most este hét óra van.")]
        tmp = tempfile.TemporaryDirectory()
        cfg_path = Path(tmp.name) / "config.toml"
        cfg_path.write_text(
            f"[llm]\nhost = '{fake.url}'\n[gui]\nexit_on_close = true\n"
            f"[tools]\nallowed_folders = ['{tmp.name}']\nnotes_dir = '{tmp.name}'\n"
            f"[work]\ndir = '{tmp.name}/munka'\n[browser]\nprofile_dir = '{tmp.name}/profil'\n",
            encoding="utf-8")
        # saját adatmappa: egy épp futó (valódi) Jarvis zárolását és fut.json-ját ne lássa
        env = dict(os.environ, PYTHONUTF8="1", NO_COLOR="1", PYTHONUNBUFFERED="1",
                   JARVIS_DATA_DIR=str(Path(tmp.name) / "adat"))
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "jarvis.py"), "--no-window",
             "--config", str(cfg_path)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            env=env, cwd=str(ROOT))
        output: list[str] = []
        found = threading.Event()

        def read_output():   # folyamatosan olvassuk, különben megtelne a cső
            for line in proc.stdout:
                output.append(line)
                if re.search(r"http://127\.0\.0\.1:\d+/\?t=\S+", line):
                    found.set()

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        try:
            self.assertTrue(found.wait(30), "a felület címe nem jelent meg:\n" + "".join(output))
            url = next(re.search(r"(http://127\.0\.0\.1:\d+/\?t=\S+)", line).group(1)
                       for line in output if "127.0.0.1" in line)
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url)
                page.wait_for_selector('body[data-state="idle"]', state="attached", timeout=20000)
                page.fill("#text", "Mennyi az idő?")
                page.press("#text", "Enter")
                page.wait_for_selector("text=Most este hét óra van.", timeout=15000)
                browser.close()   # az ablak bezárása
            closed_at = time.monotonic()
            self.assertEqual(proc.wait(timeout=30), 0, "".join(output))
            self.assertLess(time.monotonic() - closed_at, 15, "az ablak bezárása után gyorsan leáll")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
            reader.join(5)
            proc.stdout.close()
            fake.close()
            tmp.cleanup()
        generate = [b for m, p, b in fake.requests if p == "/api/generate"]
        self.assertEqual(generate[-1]["keep_alive"], 0, "kilépéskor kiüríti a modellt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
