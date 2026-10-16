"""A gyorsítások tesztjei: folyamatos válasz, mondatokra bontás, felolvasási sor,
töltelékmondat és folytatólagos hallgatás.

Futtatás a projekt mappájából:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import random
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_offline import FakeOllama, FakeSoundDevice, make_cfg, reply, tool_reply  # noqa: E402

from jarvis_core.app import FILLERS, JarvisApp  # noqa: E402
from jarvis_core.assistant import Assistant  # noqa: E402
from jarvis_core.config import load_config  # noqa: E402
from jarvis_core.llm import OllamaClient  # noqa: E402
from jarvis_core.recorder import Recorder  # noqa: E402
from jarvis_core.textutil import SentenceSplitter  # noqa: E402
from jarvis_core.tools import Toolbox  # noqa: E402
from jarvis_core.tts import SpeechQueue  # noqa: E402


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


class SentenceSplitterTests(unittest.TestCase):
    TEXT = ("Szia! Ma 2026. október 14. van, szerda. Az ára kb. 39 990 Ft, a garancia 2 év. "
            "J. K. Rowling könyve is itt van. A pontos idő: 21:15. Mehet a rendelés?\nKész.")
    EXPECTED = ["Szia! Ma 2026. október 14. van, szerda.", "Az ára kb. 39 990 Ft, a garancia 2 év.",
                "J. K. Rowling könyve is itt van.", "A pontos idő: 21:15.", "Mehet a rendelés?", "Kész."]

    def test_split_independent_of_chunking(self):
        for seed in range(5):
            rng = random.Random(seed)
            splitter, out, i = SentenceSplitter(), [], 0
            while i < len(self.TEXT):
                n = rng.randint(1, 9)
                out += splitter.feed(self.TEXT[i:i + n])
                i += n
            out += splitter.flush()
            self.assertEqual(out, self.EXPECTED, f"seed={seed}")

    def test_first_sentence_released_before_the_rest_arrives(self):
        splitter = SentenceSplitter()
        self.assertEqual(splitter.feed("Megnéztem az időjárást. "), ["Megnéztem az időjárást."])
        self.assertEqual(splitter.feed("Holnap esni fog"), [])
        self.assertEqual(splitter.flush(), ["Holnap esni fog"])


class FakeSpeaker:
    """Hamis felolvasó: minden mondat "lejátszása" egy kis ideig tart."""

    def __init__(self, play_seconds: float = 0.02):
        self.play_seconds = play_seconds
        self.prepared: list[str] = []
        self.played: list[str] = []
        self.stops = 0
        self._stop = threading.Event()

    def prepare(self, text):
        if text == "hiba":
            raise RuntimeError("szintézishiba")
        self.prepared.append(text)
        return np.array([len(self.prepared)], dtype=np.int16), 22050

    def play(self, audio, rate):
        self._stop.clear()
        self.played.append(self.prepared[int(audio[0]) - 1])
        deadline = time.monotonic() + self.play_seconds
        while time.monotonic() < deadline and not self._stop.is_set():
            time.sleep(0.005)

    def stop(self):
        self.stops += 1
        self._stop.set()


class SpeechQueueTests(unittest.TestCase):
    def test_plays_in_order(self):
        speaker = FakeSpeaker()
        speech = SpeechQueue(speaker)
        speech.mark_turn()
        for text in ("Első mondat.", "hiba", "Második mondat.", "Harmadik mondat."):
            speech.say(text)
        speech.wait(timeout=5)
        self.assertFalse(speech.busy)
        self.assertEqual(speaker.played, ["Első mondat.", "Második mondat.", "Harmadik mondat."])
        self.assertIsNotNone(speech.first_audio_at)

    def test_interrupt_drops_the_rest(self):
        speaker = FakeSpeaker(play_seconds=0.3)
        speech = SpeechQueue(speaker)
        for i in range(6):
            speech.say(f"Mondat {i}.")
        deadline = time.monotonic() + 2
        while not speaker.played and time.monotonic() < deadline:
            time.sleep(0.01)
        speech.interrupt()
        speech.wait(timeout=5)
        self.assertFalse(speech.busy)
        self.assertLess(len(speaker.played), 3)
        self.assertGreaterEqual(speaker.stops, 1)
        speech.say("Új mondat.")        # megszakítás után is használható
        speech.wait(timeout=5)
        self.assertEqual(speaker.played[-1], "Új mondat.")

    def test_say_from_many_threads(self):
        speaker = FakeSpeaker(play_seconds=0)
        speech = SpeechQueue(speaker)
        threads = [threading.Thread(target=speech.say, args=(f"Szál {i}.",)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        speech.wait(timeout=5)
        self.assertEqual(sorted(speaker.played), sorted(f"Szál {i}." for i in range(20)))


class FakeSpeech:
    """Csak feljegyzi, mit mondana Jarvis."""

    def __init__(self):
        self.spoken: list[str] = []
        self.first_audio_at = None
        self.busy = False

    def say(self, text):
        self.spoken.append(text)

    def mark_turn(self):
        pass

    def wait(self, timeout=None):
        pass

    def interrupt(self):
        pass


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
    """JarvisApp betöltés nélkül: modell, hang és mikrofon helyett hamis darabokkal."""
    app = JarvisApp(cfg, ui or RecordingUI(), use_voice=False, use_speech=False)
    app.speech = FakeSpeech()
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

    def test_filler_then_sentences(self):
        self.fake.chat_script = [tool_reply("find_files", {"query": "szamla"}),
                                 reply("Nem találtam ilyen fájlt. Keressek máshol is?")]
        self.app.answer("Keresd meg a számlát")
        spoken = self.app.speech.spoken
        self.assertIn(spoken[0], FILLERS)
        self.assertEqual(spoken[1:], ["Nem találtam ilyen fájlt.", "Keressek máshol is?"])
        ui = self.app.ui
        self.assertEqual(ui.of("tool_start")[0][2], "find_files")
        self.assertIn(("state", "working", "Fájlokat keresek"), ui.events)
        self.assertEqual("".join(e[1] for e in ui.of("delta")), "Nem találtam ilyen fájlt. Keressek máshol is?")
        self.assertEqual(ui.of("assistant_end")[-1][2], True)   # folyamatosan érkezett

    def test_no_filler_when_model_already_spoke(self):
        first = tool_reply("find_files", {"query": "szamla"})
        first["message"]["content"] = "Rögtön megnézem a fájljaidat."
        self.fake.chat_script = [first, reply("Megvan, a letöltésekben van.")]
        self.app.answer("Keresd meg a számlát")
        self.assertEqual(self.app.speech.spoken,
                         ["Rögtön megnézem a fájljaidat.", "Megvan, a letöltésekben van."])

    def test_no_filler_for_fast_tools(self):
        self.fake.chat_script = [tool_reply("get_datetime", {}), reply("Este hét óra van.")]
        self.app.answer("Mennyi az idő?")
        self.assertEqual(self.app.speech.spoken, ["Este hét óra van."])

    def test_streaming_off_still_speaks(self):
        self.cfg.assistant.stream = False
        self.fake.chat_script = [reply("Rendben, uram.")]
        self.app.answer("Szia")
        self.assertEqual(self.app.speech.spoken, ["Rendben, uram."])
        self.assertFalse(self.fake.chat_bodies()[0]["stream"])


class FollowUpTests(unittest.TestCase):
    def test_short_follow_up_window(self):
        cfg = load_config(ROOT / "config.toml").audio
        recorder = Recorder(cfg, backend=FakeSoundDevice([]))
        started = time.monotonic()
        self.assertIsNone(recorder.record(no_speech_timeout=0.5))
        self.assertLess(time.monotonic() - started, 3)

    def test_follow_up_continues_conversation(self):
        app = JarvisApp(load_config(ROOT / "config.toml"), RecordingUI(), use_voice=False, use_speech=False)
        heard = iter(["Mennyi az idő?", "És holnap esik?", ""])
        app.listen = lambda timeout=None, follow_up=False, ring=False: next(heard)
        answered: list[str] = []
        app.answer = lambda text, via="text", mode="chat": answered.append(text)
        self.assertFalse(app.voice_conversation())
        self.assertEqual(answered, ["Mennyi az idő?", "És holnap esik?"])

    def test_voice_exit(self):
        app = JarvisApp(load_config(ROOT / "config.toml"), RecordingUI(), use_voice=False, use_speech=False)
        app.listen = lambda timeout=None, follow_up=False, ring=False: "Viszlát, Jarvis!"
        app.answer = lambda text, via="text", mode="chat": self.fail("kilépésnél nem válaszol")
        self.assertTrue(app.voice_conversation())
        self.assertTrue(app.exit_requested.is_set())


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
            self.assertIn("Ehhez a jóváhagyásod kell.", app.speech.spoken)
        finally:
            app.shutdown()

    def test_wake_word_starts_voice_request(self):
        received: list[str] = []
        self.app.voice_conversation = lambda source="button": received.append(source) or False
        self.app.recorder = self.app.stt = object()   # van "mikrofon"
        self.app._on_wake()
        self.wait_idle()
        self.assertEqual(received, ["wake"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
