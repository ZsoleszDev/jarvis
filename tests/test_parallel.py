"""Párhuzamos munka: a modellkapu (hány modellhívás megy egyszerre, és a feltöltés, a beszélgetés előbb jön a
kereskedésnél), az Ollama OLLAMA_NUM_PARALLEL beállításának felismerése, az Állj gomb szálanként, és hogy egy eladási
lépés után a kereskedés hamar folytatódhat.

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
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_offline import make_cfg  # noqa: E402
from test_streaming import RecordingUI  # noqa: E402

from jarvis_core.app import AFTER_WORK, JarvisApp  # noqa: E402
from jarvis_core.config import LlmCfg  # noqa: E402
from jarvis_core.gui import EventBus, GuiUI  # noqa: E402
from jarvis_core.llm import Cancelled, ModelGate, OllamaClient, background_calls, in_background  # noqa: E402
from jarvis_core.memory import OllamaProcess  # noqa: E402
from jarvis_core.ollama_log import server_parallel  # noqa: E402


def wait_until(check, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.01)
    return False


class ModelGateTests(unittest.TestCase):
    def test_the_upload_goes_before_the_trading(self):
        """Egy hely: ha a kereskedés és a feltöltés is vár, a feltöltés kapja a következő helyet (akkor is, ha a
        kereskedés várt előbb)."""
        gate = ModelGate(1)
        order: list[str] = []

        def call(name: str, background: bool) -> None:
            with gate.hold(background=background):
                order.append(name)

        with gate.hold(background=True):          # egy kereskedési hívás fut
            trading = threading.Thread(target=call, args=("kereskedés", True))
            trading.start()
            time.sleep(0.1)
            upload = threading.Thread(target=call, args=("feltöltés", False))
            upload.start()
            self.assertTrue(wait_until(lambda: gate.waiting == 1))
        upload.join(5)
        trading.join(5)
        self.assertEqual(order, ["feltöltés", "kereskedés"])
        self.assertEqual((gate.active, gate.background, gate.waiting), (0, 0, 0))

    def test_with_two_slots_one_stays_free_for_the_upload(self):
        gate = ModelGate(2)
        with gate.hold(background=True):
            self.assertFalse(gate.background_ok(), "a második hely az elsőbbségi hívásé")
            with gate.hold(background=False):     # nem vár: egyszerre megy a kettő
                self.assertEqual(gate.active, 2)
        self.assertTrue(gate.background_ok())

    def test_with_one_slot_the_trading_waits_while_you_work(self):
        """Egy hellyel a feltöltés lépései közti szünetekben sem kérdez a kereskedés (különben minden lépés egy
        kereskedési hívást várna ki); két hellyel közben is mehet."""
        gate = ModelGate(1)
        self.assertTrue(gate.background_ok())
        with gate.session():
            self.assertFalse(gate.background_ok())
        self.assertTrue(gate.background_ok())
        two = ModelGate(2)
        with two.session():
            self.assertTrue(two.background_ok())
        two.set_slots(1)
        with two.session():
            self.assertFalse(two.background_ok())

    def test_a_waiting_call_can_be_stopped(self):
        gate = ModelGate(1)
        stop = threading.Event()
        stop.set()
        with gate.hold(background=True):
            with self.assertRaises(Cancelled):
                with gate.hold(background=False, cancel=stop):
                    pass
        self.assertEqual((gate.active, gate.waiting), (0, 0))

    def test_background_calls_are_marked_per_thread_and_the_stop_button_is_not_theirs(self):
        client = OllamaClient(LlmCfg())
        self.assertEqual(client.gate.slots, 2, "alapból kettő ([llm] parallel)")
        stop = threading.Event()
        client.stop_event = stop
        self.assertFalse(in_background())
        with background_calls():
            self.assertTrue(in_background())
            self.assertIsNone(client.stop_event, "a kereskedésnek saját leállítása van, az Állj gomb nem neki szól")
        self.assertIs(client.stop_event, stop)
        own = threading.Event()
        with client.stopped_by(own):
            self.assertIs(client.stop_event, own)
            seen: list = []
            other = threading.Thread(target=lambda: seen.append(client.stop_event))
            other.start()
            other.join()
            self.assertIs(seen[0], stop, "a többi szálon a közös Állj marad")
        self.assertIs(client.stop_event, stop)


class OwnChannelTests(unittest.TestCase):
    def test_the_selling_thread_writes_to_the_work_menu_and_leaves_the_chat_alone(self):
        """Az eladás-előkészítő szál üzenetei a Munka menübe kerülnek, közben az ágens-szál folyó válasza a
        Beszélgetésben marad, és nem vágja ketté."""
        bus = EventBus()
        ui = GuiUI(bus, "Jarvis")
        events: list[dict] = []
        bus.add_listener(events.append)
        ui.delta("Szia, ")

        def prep() -> None:
            with ui.own_channel("work"):
                ui.notice("Nézd át: Zara póló")

        worker = threading.Thread(target=prep)
        worker.start()
        worker.join()
        ui.delta("mi újság?")
        ui.assistant_end("", streamed=True)
        notices = [e for e in events if e.get("type") == "assistant"]
        self.assertEqual([(e["text"], e["channel"]) for e in notices],
                         [("Nézd át: Zara póló", "work"), ("Szia, mi újság?", "chat")])
        self.assertEqual(ui.channel, "chat")


class OllamaParallelTests(unittest.TestCase):
    def test_how_many_calls_ollama_takes_at_once(self):
        running = OllamaProcess(found=True, readable=True, env={"OLLAMA_NUM_PARALLEL": "2"})
        self.assertEqual(server_parallel(running, "2"), (2, ""))
        self.assertEqual(server_parallel(OllamaProcess(found=True, readable=True), None), (1, ""), "alapból egy")
        count, note = server_parallel(OllamaProcess(found=True, readable=True), "2")
        self.assertEqual(count, 1)
        self.assertIn("a futó Ollama még előtte indult", note)
        self.assertEqual(server_parallel(OllamaProcess(), "2"), (2, ""), "ha a futó Ollama nem olvasható: a beállítás")
        self.assertEqual(server_parallel(OllamaProcess(), "kettő"), (1, ""))


class AppParallelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name))
        self.ui = RecordingUI()
        self.app = JarvisApp(self.cfg, self.ui)

    def tearDown(self):
        self.tmp.cleanup()

    def infos(self) -> list[str]:
        return [e[1] for e in self.ui.of("info")]

    def test_two_calls_at_once_only_if_ollama_allows_it(self):
        with mock.patch("jarvis_core.memory.ollama_process",
                        return_value=OllamaProcess(found=True, readable=True, env={"OLLAMA_NUM_PARALLEL": "2"})):
            self.app._check_parallel()
        self.assertEqual(self.app.llm.gate.slots, 2)
        self.assertIn("Egyszerre 2 modellhívás mehet", self.infos()[-1])
        with mock.patch("jarvis_core.memory.ollama_process", return_value=OllamaProcess(found=True, readable=True)), \
                mock.patch("jarvis_core.app.user_setting", return_value=None):
            self.app._check_parallel()
        self.assertEqual(self.app.llm.gate.slots, 1)
        self.assertIn("setx OLLAMA_NUM_PARALLEL 2", self.infos()[-1])
        self.cfg.llm.parallel = 1
        with mock.patch("jarvis_core.memory.ollama_process",
                        return_value=OllamaProcess(found=True, readable=True, env={"OLLAMA_NUM_PARALLEL": "4"})):
            self.app._check_parallel()
        self.assertEqual(self.app.llm.gate.slots, 1, "ha egyesével kéred, egyesével megy")

    def test_after_an_upload_trading_goes_on_soon(self):
        """A beszélgetés után quiet_minutes, egy eladási lépés után csak AFTER_WORK másodperc a csend."""
        self.cfg.scheduler.quiet_minutes = 5
        now = time.time()
        self.app.last_activity = now - 3600
        self.app.last_work = now
        quiet = self.app._quiet_since()
        self.assertAlmostEqual(now - quiet, 5 * 60 - AFTER_WORK, delta=1)
        before = self.app.last_activity
        self.app._submit(("work", {"action": "upload", "ids": []}))
        self.assertEqual(self.app.last_activity, before, "az eladási lépés nem beszélgetés")
        self.assertTrue(self.app.preempt.is_set(), "de a futó kereskedést megszakítja")
        self.app._submit(("text", {"text": "Szia", "mode": "chat"}))
        self.assertGreater(self.app.last_activity, before)


if __name__ == "__main__":
    unittest.main()
