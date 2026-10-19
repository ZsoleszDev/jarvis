"""A készenlét tesztjei: a Jarvis a gép bekapcsolásakor modellek nélkül indul (a videókártya szabad),
az első kérés felébreszti, tétlenség után visszaáll; a Szünet gombos szünetből viszont nem ébred fel
magától. Egyszerre egy Jarvis fut (a második indítás csak megnyitja az ablakát), a --quit leállítja,
és az automatikus indítás (Indítópult) parancsikonja.

Hamis modell, hang és mikrofon nélkül; a böngészős részek valódi (láthatatlan) Chromiumot indítanak,
ha a Playwright telepítve van.

Futtatás:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_background import install_fake_ddgs  # noqa: E402
from test_gui import GuiHarness  # noqa: E402
from test_offline import FakeOllama, free_port, make_cfg, reply  # noqa: E402
from test_streaming import RecordingUI  # noqa: E402
from test_web_browser import HAS_CHROMIUM  # noqa: E402

from jarvis_core import autostart, instance  # noqa: E402
from jarvis_core.api import start_api  # noqa: E402
from jarvis_core.app import PAUSED_REPLY, JarvisApp  # noqa: E402
from jarvis_core.config import PROJECT_DIR, SchedulerCfg  # noqa: E402
from jarvis_core.gui import EventBus, GuiServer, GuiUI  # noqa: E402
from jarvis_core.scheduler import Hooks, Job, Scheduler  # noqa: E402
from jarvis_core.selling import POSTED  # noqa: E402


def wait_for(condition, timeout: float = 15.0, message: str = "nem teljesült időben") -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(message)
        time.sleep(0.03)


def loads(fake: FakeOllama) -> list[dict]:
    """A nyelvi modell betöltései (a kiürítések nélkül)."""
    return [b for m, p, b in fake.requests if p == "/api/generate" and b.get("keep_alive") != 0]


def unloads(fake: FakeOllama) -> list[dict]:
    return [b for m, p, b in fake.requests if p == "/api/generate" and b.get("keep_alive") == 0]


# --- a Jarvis készenlétben ----------------------------------------------------------------------------

class StandbyAppTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.fake.vram_fit = lambda ctx: 1.0
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        self.cfg.assistant.show_timings = False
        self.cfg.assistant.fillers = False
        self.cfg.standby.idle_minutes = 0
        self.ui = RecordingUI()
        self.app = JarvisApp(self.cfg, self.ui, use_voice=False, use_speech=False, use_wake_word=False)
        self.app.STANDBY_CHECK_SECONDS = 0.05

    def tearDown(self):
        self.app.shutdown()
        self.fake.close()
        self.tmp.cleanup()

    def wait_idle(self, timeout: float = 15.0) -> None:
        wait_for(lambda: not self.app.busy, timeout, "a Jarvis nem végzett időben")

    def standby_start(self) -> None:
        self.assertTrue(self.app.start(standby=True))
        self.app.start_agent()

    def notices(self) -> list[str]:
        return [e[1] for e in self.ui.of("notice")]

    def infos(self) -> str:
        return "\n".join(e[1] for e in self.ui.of("info"))

    def test_starts_without_models_and_the_first_request_wakes_it(self):
        # Felébredéskor a 16K kontextus nem férne el, 12K-val igen: a beszélgetés ehhez igazodjon.
        self.fake.vram_fit = lambda ctx: 1.0 if ctx <= 12288 else 0.9
        self.standby_start()
        app = self.app
        self.assertTrue(app.paused and app.standby and app.standby_mode)
        time.sleep(0.2)
        self.assertEqual(loads(self.fake), [], "készenlétben a videókártya szabad")
        self.assertEqual(self.fake.warm_ups, 0)
        self.assertIn(("state", "standby", ""), self.ui.events)
        self.assertTrue(app.describe()["standby"])
        self.assertIn("Készenlétben indultam", self.infos())
        self.assertNotEqual(app.scheduler.blocked_reason(), "szünet", "a könnyű háttérmunka mehet")
        self.assertNotEqual(app.assistant.context_tokens, 12288)

        self.fake.chat_script = [reply("Fél kettő van.")]
        app.submit_text("Mennyi az idő?")
        self.wait_idle()
        self.assertFalse(app.paused or app.standby)
        self.assertEqual(self.ui.of("assistant_end")[-1][1], "Fél kettő van.")
        self.assertIn("Felébredek", self.infos())
        self.assertNotIn("Újra itt vagyok.", self.notices(), "felébredéskor rögtön a válasz jön")
        self.assertEqual(app.assistant.context_tokens, 12288, "a valódi kontextusmérettel számol")
        self.assertEqual(app.work_assistant.context_tokens, 12288)
        self.assertEqual(self.fake.chat_bodies()[-1]["options"]["num_ctx"], 12288)
        self.assertEqual(self.ui.events[-1][:2], ("state", "idle"))

    def test_goes_back_to_standby_when_unused(self):
        self.cfg.standby.idle_minutes = 0.003     # kb. 0,2 másodperc
        self.standby_start()
        self.fake.chat_script = [reply("Szia!")]
        self.app.submit_text("Szia")
        wait_for(lambda: self.ui.of("assistant_end"))
        wait_for(lambda: self.app.standby and not self.app.busy, 5, "nem állt vissza készenlétbe")
        self.assertTrue(self.app.paused)
        self.assertIn("qwen3.8:27b", [b["model"] for b in unloads(self.fake)], "a modell kiürült")
        self.assertIn("készenlétbe álltam", self.infos())
        self.fake.chat_script = [reply("Itt vagyok.")]
        self.app.submit_text("Ott vagy?")
        wait_for(lambda: self.ui.of("assistant_end")[-1][1] == "Itt vagyok.", message="nem ébredt fel újra")
        self.assertEqual(len(loads(self.fake)), 2)

    def test_a_normal_start_never_goes_to_standby(self):
        self.cfg.standby.idle_minutes = 0.001
        self.assertTrue(self.app.start())
        self.app.start_agent()
        time.sleep(0.4)
        self.assertFalse(self.app.paused, "csak a készenlétben indított Jarvis áll vissza magától")

    def test_the_pause_button_is_not_woken_by_requests(self):
        self.standby_start()
        self.app.submit_pause()
        self.wait_idle()
        self.assertTrue(self.app.paused)
        self.assertFalse(self.app.standby)
        self.assertIn("Szünetre váltottam", self.notices()[-1])
        self.assertIn(("state", "paused", ""), self.ui.events)
        self.app.submit_text("Mennyi az idő?")
        self.wait_idle()
        self.assertEqual(self.notices()[-1], PAUSED_REPLY)
        self.assertEqual(loads(self.fake), [], "pl. játék közben nem tölti be a modelleket")
        self.app.submit_resume()
        self.wait_idle()
        self.assertFalse(self.app.paused)
        self.assertEqual(self.notices()[-1], "Újra itt vagyok.")

    def test_typed_pause_in_standby_does_not_load_the_models(self):
        self.standby_start()
        self.app.submit_text("Pihenj!")
        self.wait_idle()
        self.assertTrue(self.app.paused)
        self.assertFalse(self.app.standby)
        self.assertEqual(loads(self.fake), [])
        self.assertEqual(self.ui.of("user")[-1][1], "Pihenj!")

    def test_model_free_background_work_runs_in_standby_only(self):
        self.standby_start()
        store = self.app.work.store
        old = store.create(targets=["vinted"], prompt="farmer")
        store.update(old.id, title="Farmer", price=5000)
        store.mark(old.id, "vinted", POSTED)
        store.get(old.id).platforms["vinted"].update(at=time.time() - 20 * 86400, posted_at=time.time() - 20 * 86400)
        self.app.run_job_now("stale")
        self.wait_idle()
        self.assertEqual([n["kind"] for n in self.app.notices.list()], ["stale"])
        self.assertTrue(self.app.standby, "a háttérmunka nem ébreszti fel")
        self.assertEqual(loads(self.fake), [])
        self.assertEqual(self.ui.events[-1][:2], ("state", "standby"))

        self.app.submit_pause()             # a Szünet gombbal semmi nem fut
        self.wait_idle()
        self.app.work.store.get(old.id).reminded = 0
        self.app.run_job_now("stale")
        self.wait_idle()
        self.assertEqual(len(self.app.notices.list()), 1)


class SchedulerStandbyTests(unittest.TestCase):
    def test_only_jobs_without_models_run_in_standby(self):
        flags = {"paused": True, "standby": True}
        with tempfile.TemporaryDirectory() as tmp:
            hooks = Hooks(paused=lambda: flags["paused"], busy=lambda: False, last_activity=lambda: 0.0,
                          submit=lambda key: None, preempted=lambda: False, standby=lambda: flags["standby"])
            scheduler = Scheduler(SchedulerCfg(quiet_minutes=0), Path(tmp) / "hatter.json", hooks)
            light = Job("watches", "Figyelők", lambda: 60.0, lambda ctx: "ok", needs_models=False)
            heavy = Job("digest", "Összefoglaló", lambda: 60.0, lambda ctx: "ok")
            browsing = Job("listings", "Hirdetések", lambda: 60.0, lambda ctx: "ok", needs_models=False,
                           needs_browser=True)
            scheduler.register(light)
            scheduler.register(heavy)
            scheduler.register(browsing)
            now = time.time()
            self.assertEqual(scheduler.blocked_reason(), "")
            self.assertFalse(scheduler.should_yield())
            self.assertTrue(scheduler.due(light, now))
            self.assertFalse(scheduler.due(heavy, now), "ehhez kellene a nyelvi modell")
            self.assertFalse(scheduler.due(browsing, now), "készenlétben a böngésző sem fut")
            self.assertEqual(scheduler.run("listings"), "Szünetelek, ezért most nem dolgozom.")
            self.assertEqual(scheduler.run("digest", forced=True), "Szünetelek, ezért most nem dolgozom.")
            self.assertEqual(scheduler.run("watches"), "ok")
            flags["standby"] = False         # a Szünet gombbal
            self.assertEqual(scheduler.blocked_reason(), "szünet")
            self.assertTrue(scheduler.should_yield())


# --- telefon és felület készenlétben -------------------------------------------------------------------

def http_json(port: int, method: str, path: str, body: dict | None = None, headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    data = json.dumps(body).encode() if body is not None else None
    send = dict(headers or {})
    if data is not None:
        send["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=send)
    response = conn.getresponse()
    payload = response.read()
    conn.close()
    return response.status, (json.loads(payload) if payload else {})


class PhoneStandbyTests(unittest.TestCase):
    def setUp(self):
        self.ddgs = install_fake_ddgs()
        self.ddgs.start()
        self.fake = FakeOllama()
        self.fake.vram_fit = lambda ctx: 1.0
        self.tmp = tempfile.TemporaryDirectory()
        cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        cfg.api.port = 0
        cfg.assistant.show_timings = False
        cfg.assistant.fillers = False
        self.bus = EventBus()
        self.ui = GuiUI(self.bus, "Jarvis")
        self.app = JarvisApp(cfg, self.ui, use_voice=False, use_speech=False, use_wake_word=False)
        self.assertTrue(self.app.start(standby=True))
        self.api = start_api(self.app, self.ui, self.bus, cfg.data_root)
        self.app.start_agent()
        code = self.api.auth.new_code()["code"]
        status, data = http_json(self.api.port, "POST", "/api/v1/pair", {"code": code, "device": "Telefon"})
        self.assertEqual(status, 200, data)
        self.headers = {"Authorization": f"Bearer {data['token']}"}

    def tearDown(self):
        self.app.shutdown()
        self.api.stop()
        self.fake.close()
        self.tmp.cleanup()
        self.ddgs.stop()

    def test_the_phone_wakes_it_without_asking_to_resume(self):
        status, data = http_json(self.api.port, "GET", "/api/v1/status", headers=self.headers)
        self.assertEqual((data["paused"], data["standby"]), (True, True))
        self.fake.chat_script = [reply("Itt vagyok, felébredtem.")]
        status, data = http_json(self.api.port, "POST", "/api/v1/chat", {"text": "Ott vagy?", "wait": 20},
                                 headers=self.headers)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["reply"], "Itt vagyok, felébredtem.")
        self.assertFalse(self.app.paused)
        status, data = http_json(self.api.port, "GET", "/api/v1/status", headers=self.headers)
        self.assertEqual((data["paused"], data["standby"]), (False, False))

        self.assertEqual(http_json(self.api.port, "POST", "/api/v1/pause", {}, headers=self.headers)[0], 202)
        wait_for(lambda: self.app.paused and not self.app.busy)
        status, data = http_json(self.api.port, "POST", "/api/v1/chat", {"text": "Szia"}, headers=self.headers)
        self.assertEqual((status, data["code"]), (409, "paused"), "a Szünet gombos szünetből nem ébred magától")


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class GuiStandbyTests(unittest.TestCase):
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
        self.fake = FakeOllama()
        self.fake.vram_fit = lambda ctx: 1.0
        self.tmp = tempfile.TemporaryDirectory()
        cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        cfg.assistant.show_timings = False
        cfg.assistant.fillers = False
        cfg.api.enabled = False
        self.bus = EventBus()
        self.ui = GuiUI(self.bus, "Jarvis")
        self.app = JarvisApp(cfg, self.ui, use_voice=False, use_speech=False, use_wake_word=False)
        self.assertTrue(self.app.start(standby=True))
        self.server = GuiServer(self.app, self.ui, self.bus)
        self.server.start()
        self.bus.set_hello(self.app.describe())
        self.app.start_agent()
        self.page = self.browser.new_page(viewport={"width": 560, "height": 900})
        self.errors: list[str] = []
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.goto(self.server.url)

    def tearDown(self):
        self.page.close()
        self.app.shutdown()
        self.server.stop()
        self.fake.close()
        self.tmp.cleanup()
        self.assertEqual(self.errors, [], "JavaScript-hiba az oldalon")

    def test_a_question_wakes_it_and_the_button_too(self):
        page = self.page
        page.wait_for_selector('body[data-state="standby"]', state="attached", timeout=15000)
        self.assertEqual(page.inner_text("#pause"), "Ébresztés")
        self.assertEqual(page.inner_text("#state"), "Készenlétben")
        self.assertIn("Írj, és felébredek", page.inner_text("#detail"))
        self.fake.chat_script = [reply("Jó reggelt! Miben segíthetek?")]
        page.fill("#text", "Jó reggelt")
        page.press("#text", "Enter")
        page.wait_for_selector('.toast:has-text("Felébredek")')
        self.assertEqual(page.locator("#ask-dialog[open]").count(), 0, "készenlétből nem kérdez rá")
        page.wait_for_selector('.msg.jarvis:has-text("Jó reggelt! Miben segíthetek?")', timeout=15000)
        page.wait_for_selector('#pause:has-text("Szünet")')

        self.app._submit(("standby", None))      # mintha egy ideje nem használtad volna
        page.wait_for_selector('#pause:has-text("Ébresztés")', timeout=15000)
        page.click("#pause")
        page.wait_for_selector('#pause:has-text("Szünet")', timeout=15000)
        page.wait_for_selector('text=Újra itt vagyok.')
        self.assertFalse(self.app.paused)


# --- egyszerre egy Jarvis, és a futó elérése ------------------------------------------------------------

class InstanceTests(unittest.TestCase):
    def test_only_one_may_hold_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = instance.InstanceLock(Path(tmp) / "x" / "jarvis.lock")
            second = instance.InstanceLock(Path(tmp) / "x" / "jarvis.lock")
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            first.release()                  # kétszer is elengedhető
            self.assertTrue(second.acquire())
            second.release()

    def test_info_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fut.json"
            self.assertIsNone(instance.read_info(path))
            instance.write_info(path, "http://127.0.0.1:5000/?t=abc", True)
            info = instance.read_info(path)
            self.assertEqual((info["url"], info["standby"], info["pid"]), ("http://127.0.0.1:5000/?t=abc", True,
                                                                           os.getpid()))
            instance.clear_info(path, "http://127.0.0.1:6000/?t=masik")
            self.assertTrue(path.exists(), "a másik Jarvis fájlját nem törli")
            instance.clear_info(path, "http://127.0.0.1:5000/?t=abc")
            self.assertFalse(path.exists())
            for bad in ("{nem json", json.dumps({"url": "http://gonosz.example/?t=x"}), json.dumps([1, 2])):
                path.write_text(bad, encoding="utf-8")
                self.assertIsNone(instance.read_info(path), bad)

    def test_ping_and_quit_through_the_window_server(self):
        h = GuiHarness()
        try:
            url = h.server.url
            self.assertTrue(instance.reachable(url))
            wrong = url.replace(urllib.parse.quote(h.server.token), "rossz").replace(h.server.token, "rossz")
            self.assertFalse(instance.reachable(wrong))
            self.assertFalse(instance.request_quit(wrong))
            self.assertFalse(h.app.exit_requested.is_set())
            self.assertTrue(instance.request_quit(url))
            self.assertTrue(h.app.exit_requested.is_set())
        finally:
            h.close()
        self.assertFalse(instance.reachable(url, timeout=1.0), "leállt Jarvis nem válaszol")


# --- automatikus indítás (Windows: Indítópult) ----------------------------------------------------------

class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"APPDATA": self.tmp.name})
        self.env.start()
        self.commands: list[list[str]] = []
        self.link = autostart.startup_folder() / autostart.LINK_NAME

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def fake_run(self, command, **kwargs):
        self.commands.append(command)
        self.link.write_bytes(b"parancsikon")
        return subprocess.CompletedProcess(command, 0, "", "")

    def test_on_and_off(self):
        self.assertEqual(self.link.parent, Path(self.tmp.name) / "Microsoft" / "Windows" / "Start Menu" /
                         "Programs" / "Startup")
        message = autostart.set_autostart(True, windows=True, run=self.fake_run)
        self.assertIn("Bekapcsoltam", message)
        command = self.commands[0]
        self.assertEqual(command[:2], ["powershell", "-NoProfile"])
        script = command[-1]
        for expected in ("WScript.Shell", f"'{self.link}'", f"'{autostart.pythonw_path()}'",
                         f'"{PROJECT_DIR / "jarvis.py"}" --standby', "$s.WindowStyle = 7", "$s.Save()"):
            self.assertIn(expected, script)
        self.assertIn("Kikapcsoltam", autostart.set_autostart(False, windows=True))
        self.assertFalse(self.link.exists())
        self.assertEqual(autostart.set_autostart(False, windows=True), "Nem volt bekapcsolva.")

    def test_own_config_and_failures(self):
        autostart.set_autostart(True, config="sajat.toml", windows=True, run=self.fake_run)
        self.assertIn(f'--config "{Path("sajat.toml").resolve()}"', self.commands[-1][-1])
        self.link.unlink()

        def refused(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "valami\nHozzáférés megtagadva")

        self.assertEqual(autostart.set_autostart(True, windows=True, run=refused),
                         "Nem sikerült bekapcsolni (Hozzáférés megtagadva).")

        def missing(command, **kwargs):
            raise FileNotFoundError("nincs powershell")

        self.assertEqual(autostart.set_autostart(True, windows=True, run=missing),
                         "Nem sikerült bekapcsolni (nincs powershell).")
        self.assertEqual(autostart.set_autostart(True, windows=False),
                         "Az automatikus indítás csak Windows-on működik.")
        self.assertEqual(autostart._ps("C:\\Users\\O'Brien"), "'C:\\Users\\O''Brien'")


class WindowlessOutputTests(unittest.TestCase):
    def test_without_a_console_everything_goes_to_the_log(self):
        spec = importlib.util.spec_from_file_location("jarvis_main", ROOT / "jarvis.py")
        module = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, "dont_write_bytecode", True):   # ne kerüljön __pycache__ a projektmappába
            spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "naplo.txt"
            log.write_bytes(b"x" * 2_000_001)            # a túl nagy naplót félreteszi
            with mock.patch.dict(os.environ, {"JARVIS_DATA_DIR": tmp}), \
                    mock.patch.object(sys, "stdout", None), mock.patch.object(sys, "stderr", None):
                module._ensure_output()
                stream = sys.stdout
                self.assertIs(sys.stderr, stream)
                print("Szia, ez a naplóba megy.")
            stream.close()
            self.assertEqual((Path(tmp) / "naplo.1.txt").stat().st_size, 2_000_001)
            text = log.read_text(encoding="utf-8")
            self.assertRegex(text, r"--- \d{4}-\d\d-\d\d \d\d:\d\d:\d\d ---")
            self.assertIn("Szia, ez a naplóba megy.", text)
        module._ensure_output()       # konzollal nem csinál semmit
        self.assertIsNotNone(sys.stdout)


# --- a valódi jarvis.py ---------------------------------------------------------------------------------

class EndToEndStandbyTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.fake.vram_fit = lambda ctx: 1.0
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data = root / "adat"
        self.cfg_path = root / "config.toml"
        self.cfg_path.write_text(
            f"[llm]\nhost = '{self.fake.url}'\n[api]\nenabled = false\n"
            f"[tools]\nallowed_folders = ['{root}']\nnotes_dir = '{root}'\n"
            f"[work]\ndir = '{root}/munka'\n[browser]\nprofile_dir = '{root}/profil'\n",
            encoding="utf-8")
        # saját adatmappa: egy épp futó (valódi) Jarvis zárolását és fut.json-ját ne lássa
        self.env = dict(os.environ, PYTHONUTF8="1", NO_COLOR="1", PYTHONUNBUFFERED="1",
                        JARVIS_DATA_DIR=str(self.data))

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def cli(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ROOT / "jarvis.py"), *args, "--config", str(self.cfg_path)],
                              capture_output=True, text=True, encoding="utf-8", timeout=timeout, env=self.env,
                              cwd=str(ROOT))

    def test_standby_start_second_launch_and_quit(self):
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "jarvis.py"), "--standby", "--text", "--mute", "--config", str(self.cfg_path)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", env=self.env,
            cwd=str(ROOT))
        output: list[str] = []
        reader = threading.Thread(target=lambda: output.extend(proc.stdout), daemon=True)
        reader.start()
        info_path = self.data / instance.INFO_FILE
        try:
            wait_for(lambda: instance.read_info(info_path) is not None or proc.poll() is not None, 30)
            info = instance.read_info(info_path)
            self.assertIsNotNone(info, "".join(output))
            self.assertTrue(info["standby"])
            wait_for(lambda: instance.reachable(info["url"]), 30)
            wait_for(lambda: any("Készenlétben indultam" in line for line in output), 30)
            self.assertEqual(loads(self.fake), [], "készenlétben nem tölt be modellt")

            second = self.cli("--no-window", "--text", "--mute")
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn("A Jarvis már fut", second.stdout)
            self.assertIn(info["url"], second.stdout)
            again = self.cli("--standby")
            self.assertEqual(again.returncode, 0, "a gép bekapcsolásakor sem indul kétszer")

            if HAS_CHROMIUM:   # az ablak bezárása nem állítja le (a telefon és a következő megnyitás elérje)
                from playwright.sync_api import sync_playwright

                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    page = browser.new_page()
                    page.goto(info["url"])
                    page.wait_for_selector('body[data-state="standby"]', state="attached", timeout=15000)
                    browser.close()
                time.sleep(5.5)
                self.assertIsNone(proc.poll(), "készenlétben az ablak bezárása után is fut")

            base, token = info["url"].split("/?t=")
            port = int(base.rsplit(":", 1)[1])
            self.fake.chat_script = [reply("Itt vagyok, uram.")]
            status, _ = http_json(port, "POST", "/api/send", {"text": "Ott vagy?"},
                                  headers={"X-Jarvis-Token": urllib.parse.unquote(token)})
            self.assertEqual(status, 202)
            wait_for(lambda: self.fake.chat_bodies() and not self.fake.chat_script, 30, "".join(output))
            self.assertEqual(len(loads(self.fake)), 1, "a kérés felébresztette")

            stop = self.cli("--quit")
            self.assertEqual(stop.returncode, 0, stop.stdout + stop.stderr)
            self.assertIn("Szóltam a Jarvisnak", stop.stdout)
            self.assertEqual(proc.wait(timeout=30), 0, "".join(output))
            self.assertFalse(info_path.exists(), "kilépéskor törli a fut.json-t")
            self.assertEqual(unloads(self.fake)[-1]["keep_alive"], 0, "kilépéskor kiüríti a modellt")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
            reader.join(5)
            proc.stdout.close()
        stopped = self.cli("--quit")
        self.assertEqual(stopped.returncode, 1)
        self.assertIn("Nem fut Jarvis", stopped.stdout)

    def test_a_frozen_instance_is_reported(self):
        lock = instance.InstanceLock(self.data / instance.LOCK_FILE)
        self.assertTrue(lock.acquire())
        try:
            instance.write_info(self.data / instance.INFO_FILE, f"http://127.0.0.1:{free_port()}/?t=x", False)
            result = self.cli("--no-window", "--text", "--mute")
        finally:
            lock.release()
        self.assertEqual(result.returncode, 1)
        self.assertIn("nem válaszol", result.stdout)

    def test_windowless_launch_logs_to_a_file(self):
        code = ("import runpy, sys; sys.stdout = sys.stderr = None; "
                f"sys.argv = ['jarvis.py', '--quit', '--config', {str(self.cfg_path)!r}]; "
                "runpy.run_path('jarvis.py', run_name='__main__')")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8",
                                env=self.env, cwd=str(ROOT), timeout=60)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "", "konzol nélkül semmi nem megy a konzolra")
        self.assertIn("Nem fut Jarvis", (self.data / "naplo.txt").read_text(encoding="utf-8"))
        if sys.platform != "win32":   # Windows-on valódi parancsikont tenne az Indítópultba
            cli = self.cli("--autostart", "on")
            self.assertEqual(cli.returncode, 0)
            self.assertIn("csak Windows-on", cli.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
