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

from jarvis_core.app import JarvisApp  # noqa: E402
from jarvis_core.assistant import Assistant  # noqa: E402
from jarvis_core.gui import EventBus, GuiServer, GuiUI  # noqa: E402
from jarvis_core.tools import Toolbox  # noqa: E402


class GuiHarness:
    """Jarvis grafikus felülettel, de modell, hang és mikrofon nélkül."""

    def __init__(self, allow_powershell: bool = False):
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        self.cfg.tools.allow_powershell = allow_powershell
        self.bus = EventBus()
        self.ui = GuiUI(self.bus, "Jarvis")
        self.app = JarvisApp(self.cfg, self.ui, use_voice=False, use_speech=False)
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


class EventBusTests(unittest.TestCase):
    def test_replay_keeps_last_tool_state_and_current_state(self):
        bus = EventBus()
        bus.publish({"type": "user", "text": "szia"})
        bus.publish({"type": "tool", "id": 1, "status": "running"})
        bus.publish({"type": "tool", "id": 1, "status": "done"})
        bus.publish({"type": "level", "value": 0.5}, record=False)
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
        self.page.wait_for_selector("#examples button")

    def tearDown(self):
        self.page.close()
        self.h.close()

    def wait_idle(self):
        self.page.wait_for_selector('body[data-state="idle"]', state="attached", timeout=15000)
        deadline = time.monotonic() + 10
        while self.h.app.busy and time.monotonic() < deadline:
            time.sleep(0.02)

    def test_header_and_examples(self):
        self.assertEqual(self.page.title(), "Jarvis")
        self.assertIn("qwen3.8:27b", self.page.inner_text("#meta"))
        self.assertEqual(self.page.locator("#examples button").count(), 3)
        self.assertTrue(self.page.is_disabled("#listen"), "mikrofon nélkül a gomb nem nyomható")
        self.assertEqual(self.page.inner_text("#state"), "Készen állok")

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

    def test_example_button_sends_question(self):
        self.h.fake.chat_script = [reply("Holnap napos idő lesz.")]
        self.page.click("#examples button >> nth=0")
        self.page.wait_for_selector(".msg.jarvis")
        self.assertEqual(self.page.inner_text(".msg.you"), "Milyen idő lesz holnap?")

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
        from jarvis_core.api import start_api

        self.h.cfg.api.port = 0
        api = start_api(self.h.app, self.h.ui, self.h.bus, self.h.cfg.data_root)
        self.h.server.phone = api
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
        finally:
            api.stop()

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
            [sys.executable, str(ROOT / "jarvis.py"), "--text", "--mute", "--no-window",
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
