"""Hibatűrés: ha az Ollama modellfolyamata futás közben leáll (pl. az Ollama 0.40 llama-server
std::bad_alloc hibája), a Jarvis egyszer újrapróbálja, és ott folytatja, ahol tartott; ha mégsem megy,
a hiba érthetően és a helyén jelenik meg (a feltöltésé a Munka naplójában és a hirdetés kártyáján).
A böngészőben pedig, ha egy nyitva maradt lenyíló lista vagy felugró ablak eltakarja, amire kattintana,
megnyomja az Esc-et, és ha az sem segít, megmondja, mi takarja.

Hamis Ollama; a böngészős részek valódi (láthatatlan) Chromiumot indítanak, ha a Playwright telepítve van.

Futtatás:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import functools
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_offline import FakeOllama, make_cfg, reply, tool_reply  # noqa: E402
from test_web_browser import HAS_CHROMIUM, _QuietHandler  # noqa: E402
from test_work import SHIRT, jpeg  # noqa: E402

from jarvis_core import llm  # noqa: E402
from jarvis_core.app import JarvisApp, model_problem  # noqa: E402
from jarvis_core.browser import BrowserController, BrowserError, blocker_of  # noqa: E402
from jarvis_core.config import load_config  # noqa: E402
from jarvis_core.gui import EventBus, GuiUI  # noqa: E402
from jarvis_core.llm import OllamaClient, OllamaError, is_runner_crash  # noqa: E402
from jarvis_core.selling import FAILED  # noqa: E402

# Az Ollama 0.40 pontos hibaüzenetei, amikor a modellfolyamat leáll (a felhasználó naplójából)
CRASH_AFTER = ("model runner has unexpectedly stopped, this may be due to resource limitations or an internal "
               "error, check ollama server logs for details")
CRASH_DURING = ("error reading llama-server response: read tcp 127.0.0.1:62287->127.0.0.1:57861: wsarecv: "
                "A létező kapcsolatot a távoli állomás kényszerítetten bezárta.")


def crash(message: str = CRASH_AFTER):
    return lambda body: (500, {"error": message})


class CrashDetectionTests(unittest.TestCase):
    def test_which_errors_mean_the_model_process_stopped(self):
        for text in (CRASH_AFTER, CRASH_DURING, "llama runner process has terminated: exit status 0xc0000409",
                     'Post "http://127.0.0.1:50263/completion": EOF'):
            self.assertTrue(is_runner_crash(f"Ollama-hiba (500): {text}"), text)
        for text in ("Nem érem el az Ollamát (http://localhost:11434). Fut az Ollama?",
                     "Ollama-hiba (404): model 'qwen3.8:27b' not found",
                     "Az Ollama nem válaszolt 180 másodperc alatt."):
            self.assertFalse(is_runner_crash(text), text)

    def test_readable_messages(self):
        self.assertIn("futás közben leállt", model_problem(OllamaError(f"Ollama-hiba (500): {CRASH_AFTER}")))
        self.assertIn("Az Újra gombbal", model_problem(OllamaError(CRASH_DURING), short=True))
        self.assertEqual(model_problem(OllamaError("Ollama-hiba (404): nincs ilyen modell")),
                         "Ollama-hiba (404): nincs ilyen modell")


class ClientRetryTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        self.client = OllamaClient(self.cfg.llm)
        self.restarts: list[str] = []
        self.client.on_runner_restart = self.restarts.append
        self.pause = mock.patch.object(llm, "RESTART_PAUSE", 0.01)
        self.pause.start()

    def tearDown(self):
        self.pause.stop()
        self.fake.close()
        self.tmp.cleanup()

    def test_one_crash_is_retried_transparently(self):
        self.fake.chat_script = [crash(), reply("Folytatom.")]
        message = self.client.chat([{"role": "user", "content": "szia"}])
        self.assertEqual(message["content"], "Folytatom.")
        self.assertEqual(len(self.restarts), 1)
        bodies = self.fake.chat_bodies()
        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[0], bodies[1], "ugyanazt a kérést küldi újra")

    def test_streamed_reply_is_retried_before_any_text(self):
        self.fake.chat_script = [crash(CRASH_DURING), reply("Szia, itt vagyok.")]
        heard: list[str] = []
        message = self.client.chat([{"role": "user", "content": "szia"}], on_text=heard.append)
        self.assertEqual("".join(heard), "Szia, itt vagyok.", "nem duplázódik")
        self.assertEqual(message["content"], "Szia, itt vagyok.")
        self.assertEqual(len(self.restarts), 1)

    def test_no_retry_once_text_was_already_spoken(self):
        broken = reply("Most éppen")
        broken["stream_error"] = CRASH_DURING
        self.fake.chat_script = [broken, reply("nem kérjük")]
        heard: list[str] = []
        with self.assertRaisesRegex(OllamaError, "llama-server"):
            self.client.chat([{"role": "user", "content": "szia"}], on_text=heard.append)
        self.assertEqual(self.restarts, [], "a félig elhangzott választ nem kezdi újra")
        self.assertEqual(len(self.fake.chat_script), 1)

    def test_a_reply_cut_before_its_end_is_not_a_reply(self):
        cut = reply("A hirdetés sikeresen feltöltődött! A hirdetés a „Processing” állapotban van (30-as link:")
        cut["stream_cut"] = True
        self.fake.chat_script = [cut]
        heard: list[str] = []
        with self.assertRaisesRegex(OllamaError, "félbeszakadt"):
            self.client.chat([{"role": "user", "content": "töltsd fel"}], on_text=heard.append)
        self.assertTrue("".join(heard).endswith("(30-as link:"), "ami megjött, az elhangzott")
        self.assertEqual(self.restarts, [], "a félig elhangzott választ nem kezdi újra")

    def test_a_second_crash_is_reported(self):
        self.fake.chat_script = [crash(), crash(), reply("nem jut ide")]
        with self.assertRaisesRegex(OllamaError, "unexpectedly stopped"):
            self.client.chat([{"role": "user", "content": "szia"}])
        self.assertEqual(len(self.restarts), 1, "csak egyszer próbálja újra")
        self.assertEqual(len(self.fake.chat_script), 1)

    def test_other_errors_are_not_retried(self):
        self.fake.chat_script = [lambda body: (404, {"error": "model 'x' not found"}), reply("nem jut ide")]
        with self.assertRaisesRegex(OllamaError, "not found"):
            self.client.chat([{"role": "user", "content": "szia"}])
        self.assertEqual(self.restarts, [])

    def test_json_and_image_requests_are_retried_too(self):
        self.fake.chat_script = [crash(), reply('{"ok": true}'), crash(), reply("Egy piros póló.")]
        self.assertEqual(self.client.chat_json([{"role": "user", "content": "?"}]), {"ok": True})
        self.assertEqual(self.client.vision("Mi van a képen?", "aGVsbG8="), "Egy piros póló.")
        self.assertEqual(len(self.restarts), 2)


class AppRecoveryTests(unittest.TestCase):
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
        self.assertTrue(self.app.start())
        self.pause = mock.patch.object(llm, "RESTART_PAUSE", 0.01)
        self.pause.start()

    def tearDown(self):
        self.pause.stop()
        self.app.shutdown()
        self.fake.close()
        self.tmp.cleanup()

    def events(self) -> list[dict]:
        client = self.bus.subscribe()
        events = [client.get_nowait() for _ in range(client.qsize())]
        self.bus.unsubscribe(client)
        return events

    def test_the_conversation_goes_on_after_a_crash(self):
        self.fake.chat_script = [tool_reply("get_datetime", {}), crash(CRASH_DURING), reply("Este hét óra van.")]
        self.app.answer("Mennyi az idő?")
        events = self.events()
        notes = [e for e in events if e["type"] == "warn"]
        self.assertTrue(any("Újraindítom" in e["text"] for e in notes))
        answers = [e["text"] for e in events if e["type"] == "assistant"]
        self.assertEqual(answers[-1], "Este hét óra van.", "ott folytatta, ahol tartott")
        self.assertFalse([e for e in events if e["type"] == "error"])
        tools = [m for m in self.app.assistant.history if m.get("role") == "tool"]
        self.assertEqual(len(tools), 1, "az eszközt nem futtatta le újra")

    def test_a_failed_post_reports_in_the_work_log_and_on_the_card(self):
        store = self.app.work.store
        item = store.create(targets=["vinted"], prompt="póló")
        store.add_photo(item.id, jpeg())
        store.update(item.id, **SHIRT)
        self.fake.chat_script = [crash(), crash()]
        self.app.run_work({"action": "post", "ids": [item.id], "platform": "vinted"})
        state = store.get(item.id).platforms["vinted"]
        self.assertEqual(state["status"], FAILED)
        self.assertIn("közben leállt", state["note"], "a kártyán a valódi ok")
        self.assertTrue(state.get("dossier"), "hibacsomag is készült")
        errors = [e for e in self.events() if e["type"] == "error"]
        self.assertEqual([e["channel"] for e in errors], ["work"], "a Munka naplójába került, nem a Beszélgetésbe")
        self.assertIn("futás közben leállt", errors[0]["text"])
        info = (self.app.work.dossiers.path(state["dossier"]) / "info.json").read_text(encoding="utf-8")
        self.assertIn("közben leállt", info)


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class CoveredClickTests(unittest.TestCase):
    PAGES = {
        "lista.html": """<!doctype html><html lang="hu"><head><meta charset="utf-8"><title>Lista</title></head><body>
<label>Ár <input id="price" name="price"></label>
<button type="button" id="cat" onclick="document.getElementById('dd').hidden = false">Kategória</button>
<div id="dd" role="listbox" aria-label="Kategóriák" hidden
  style="position:fixed;left:0;top:0;width:100%;height:100%;background:rgba(0,0,0,.2)">Női · Férfi</div>
<script>document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') document.getElementById('dd').hidden = true; });</script>
</body></html>""",
        "suti.html": """<!doctype html><html lang="hu"><head><meta charset="utf-8"><title>Süti</title></head><body>
<button type="button" id="save" onclick="document.title = 'Mentve'">Mentés</button>
<div class="cookie-banner" style="position:fixed;left:0;top:0;width:100%;height:100%;background:#fff">
  Sütiket használunk. <button type="button" onclick="this.parentElement.remove()">Elfogadom</button></div>
</body></html>""",
    }

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        for name, html in cls.PAGES.items():
            (Path(cls.dir.name) / name).write_text(html, encoding="utf-8")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_QuietHandler, directory=cls.dir.name))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.profile = tempfile.TemporaryDirectory()
        cfg = load_config(ROOT / "config.toml").browser
        cfg.headless = True
        cfg.profile_dir = cls.profile.name
        cfg.pace = 0.0
        cls.browser = BrowserController(cfg, allow_local=True, confirm=lambda prompt: True)
        cls.browser.CLICK_TIMEOUT_MS, cls.browser.PROBE_TIMEOUT_MS = 2000, 700

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.server.shutdown()
        cls.server.server_close()
        cls.dir.cleanup()
        cls.profile.cleanup()

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/{name}"

    def find(self, label: str) -> int:
        return next(i for i, e in self.browser.elements.items() if e.get("label") == label)

    def test_an_open_dropdown_is_closed_with_escape(self):
        self.browser.open(self.url("lista.html"))
        self.browser.click(self.find("Kategória"))
        self.assertTrue(self.browser.page.is_visible("#dd"), "a lista nyitva maradt, eltakarja az árat")
        self.browser.type_text(self.find("Ár"), "4000")
        self.assertEqual(self.browser.page.input_value("#price"), "4000")
        self.assertFalse(self.browser.page.is_visible("#dd"), "az Esc bezárta")

    def test_what_covers_it_is_told_to_the_model(self):
        self.browser.open(self.url("suti.html"))
        started = time.monotonic()
        with self.assertRaises(BrowserError) as caught:
            self.browser.click(self.find("Mentés"))
        message = str(caught.exception)
        self.assertIn("eltakarja egy felugró ablak vagy sáv", message)
        self.assertIn("az Esc sem zárta be", message)
        self.assertIn("Elfogadom", message, "megmondja, mire kattintson")
        self.assertLess(time.monotonic() - started, 8, "nem vár sokáig feleslegesen")
        self.browser.click(self.find("Elfogadom"))
        self.browser.click(self.find("Mentés"))
        self.assertEqual(self.browser.page.title(), "Mentve")

    def test_blocker_descriptions(self):
        line = ('      - <div id="dd" role="listbox" aria-label="Kategória lista" class="web_ui__Dropdown">'
                'Női · Férfi</div> intercepts pointer events')
        self.assertEqual(blocker_of("Timeout 8000ms exceeded.\nCall log:\n" + line),
                         "egy nyitva maradt lenyíló lista („Kategória lista”)")
        nested = '- <span class="x">ok</span> from <label>…</label> subtree intercepts pointer events'
        self.assertEqual(blocker_of(nested),
                         "egy <span> elem („ok”)")
        self.assertEqual(blocker_of("Locator.click: Timeout 8000ms exceeded."), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
