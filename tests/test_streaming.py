"""A gyorsítások tesztjei: folyamatos válasz (a szöveg menet közben jelenik meg), és hogy a kérések a saját
ágens-szálon futnak, az Állj gomb pedig mindent leállít.

Futtatás a projekt mappájából:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_offline import FakeOllama, make_cfg, reply, tool_reply  # noqa: E402

from jarvis_core.app import JarvisApp  # noqa: E402
from jarvis_core.assistant import Assistant  # noqa: E402
from jarvis_core.llm import OllamaClient  # noqa: E402
from jarvis_core.tools import Toolbox  # noqa: E402


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        self.llm = OllamaClient(self.cfg.llm)

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def test_text_arrives_in_pieces(self):
        self.fake.chat_script = [reply("Most este hét óra van, uram.")]
        pieces: list[str] = []
        msg = self.llm.chat([{"role": "user", "content": "idő?"}], on_text=pieces.append)
        self.assertGreater(len(pieces), 3)
        self.assertEqual("".join(pieces), "Most este hét óra van, uram.")
        self.assertEqual(msg["content"], "Most este hét óra van, uram.")
        self.assertEqual(self.llm.last_stats["eval_count"], 10)
        self.assertTrue(self.fake.chat_bodies()[0]["stream"])

    def test_tool_calls_in_stream(self):
        self.fake.chat_script = [tool_reply("get_datetime", {})]
        msg = self.llm.chat([{"role": "user", "content": "x"}], tools=[], on_text=lambda t: None)
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "get_datetime")

    def test_cancel_stops_reading(self):
        self.fake.chat_script = [reply("egy kettő három négy öt hat")]
        cancel = threading.Event()
        pieces: list[str] = []

        def on_text(text: str) -> None:
            pieces.append(text)
            cancel.set()  # mintha a felhasználó Ctrl+C-t nyomott volna

        payload = {"model": self.cfg.llm.model, "messages": [], "stream": True}
        data = self.llm._stream_request(payload, on_text, cancel)
        self.assertEqual(pieces, ["egy "])
        self.assertEqual(data["message"]["content"], "egy ")

    def test_non_streaming_still_works(self):
        self.fake.chat_script = [reply("rendben")]
        self.assertEqual(self.llm.chat([{"role": "user", "content": "x"}])["content"], "rendben")
        self.assertFalse(self.fake.chat_bodies()[0]["stream"])

    def test_assistant_streams_across_tool_rounds(self):
        self.fake.chat_script = [tool_reply("get_datetime", {}), reply("Most este van. Jó estét!")]
        assistant = Assistant(self.cfg, self.llm, Toolbox(self.cfg), "rendszer")
        pieces: list[str] = []
        self.assertEqual(assistant.ask("idő?", on_text=pieces.append), "Most este van. Jó estét!")
        self.assertEqual("".join(pieces), "Most este van. Jó estét!")
        self.assertTrue(assistant.last_reply_streamed)


class RecordingUI:
    """Hamis felület: minden üzenetet feljegyez (a konzolos és a grafikus felület helyett)."""

    def __init__(self):
        self.events: list[tuple] = []
        self.confirm_answer = False

    def __getattr__(self, name):
        def record(*args):
            self.events.append((name, *args))
        return record

    def confirm(self, prompt, stop_event):
        self.events.append(("confirm", prompt))
        return self.confirm_answer

    def of(self, kind: str) -> list[tuple]:
        return [e for e in self.events if e[0] == kind]


def make_app(cfg, ui=None):
    """JarvisApp betöltés nélkül: a modell helyett a hamis Ollamával."""
    app = JarvisApp(cfg, ui or RecordingUI())
    app.toolbox = Toolbox(cfg, notify=app.notify, confirm=app.confirm)
    app.assistant = Assistant(cfg, app.llm, app.toolbox, "rendszer",
                              on_tool_call=app._on_tool_done, on_tool_start=app._on_tool_start)
    app.assistant.stop_event = app.stop_event
    return app


class JarvisPipelineTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        self.cfg.assistant.show_timings = False
        self.app = make_app(self.cfg)

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def test_the_reply_arrives_in_pieces_with_the_steps(self):
        self.fake.chat_script = [tool_reply("find_files", {"query": "szamla"}),
                                 reply("Nem találtam ilyen fájlt. Keressek máshol is?")]
        self.app.answer("Keresd meg a számlát")
        ui = self.app.ui
        self.assertEqual(ui.of("tool_start")[0][2], "find_files")
        self.assertIn(("state", "working", "Fájlokat keresek"), ui.events)
        self.assertEqual("".join(e[1] for e in ui.of("delta")), "Nem találtam ilyen fájlt. Keressek máshol is?")
        self.assertEqual(ui.of("assistant_end")[-1][2], True)   # folyamatosan érkezett

    def test_streaming_off_gives_the_whole_reply(self):
        self.cfg.assistant.stream = False
        self.fake.chat_script = [reply("Rendben, uram.")]
        self.app.answer("Szia")
        self.assertEqual(self.app.ui.of("assistant_end")[-1][1:], ("Rendben, uram.", False))
        self.assertEqual(self.app.ui.of("delta"), [])
        self.assertFalse(self.fake.chat_bodies()[0]["stream"])


class AgentThreadTests(unittest.TestCase):
    """A kérések a saját ágens-szálon futnak; az Állj gomb mindent leállít."""

    def setUp(self):
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        self.cfg.assistant.show_timings = False
        self.app = make_app(self.cfg)
        self.app.start_agent()

    def tearDown(self):
        self.app.shutdown()
        self.fake.close()
        self.tmp.cleanup()

    def wait_idle(self, timeout=10):
        deadline = time.monotonic() + timeout
        while self.app.busy and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.app.busy)

    def test_requests_run_in_order_on_one_thread(self):
        threads: list[str] = []
        self.app.toolbox.register("where", "teszt", {"type": "object", "properties": {}},
                                  lambda: threads.append(threading.current_thread().name) or "ok")
        self.fake.chat_script = [tool_reply("where", {}), reply("Első."),
                                 tool_reply("where", {}), reply("Második.")]
        self.app.submit_text("egy")
        self.app.submit_text("kettő")
        self.wait_idle()
        self.assertEqual(threads, ["jarvis-agent", "jarvis-agent"])
        self.assertEqual([e[1] for e in self.app.ui.of("user")], ["egy", "kettő"])
        self.assertEqual(self.app.ui.of("state")[-1][1], "idle")

    def test_stop_cancels_a_long_task(self):
        def slow(body):
            time.sleep(3)
            return 200, reply("Késő válasz.")

        self.fake.chat_script = [slow]
        self.app.submit_text("lassú kérés")
        time.sleep(0.3)
        started = time.monotonic()
        self.app.stop_current()
        self.wait_idle(timeout=5)
        self.assertLess(time.monotonic() - started, 1.5, "az Állj azonnal hat, nem várja meg a modellt")
        self.assertIn(("info", "Megszakítva."), self.app.ui.events)
        self.assertEqual(self.app.assistant.history, [])   # a félbehagyott kérés nem marad meg

    def test_confirmation_goes_through_the_ui(self):
        self.cfg.tools.allow_powershell = True
        app = make_app(self.cfg)
        app.start_agent()
        try:
            self.fake.chat_script = [tool_reply("run_powershell", {"command": "Get-Date"}),
                                     reply("Nem futtattam, mert nem engedélyezted.")]
            app.submit_text("futtasd")
            deadline = time.monotonic() + 10
            while app.busy and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertIn("Get-Date", app.ui.of("confirm")[0][1])
        finally:
            app.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
