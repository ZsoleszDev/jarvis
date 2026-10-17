"""A modell elhelyezésének tesztjei: ha nem fér el teljesen a videókártyákon, kisebb
kontextussal töltődik újra, és a grafikus ablak csak a modell betöltése után nyílik meg.

Futtatás a projekt mappájából:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_offline import FakeOllama, free_port, make_cfg, reply  # noqa: E402
from test_streaming import RecordingUI  # noqa: E402

from jarvis_core import gpu, gui, tools  # noqa: E402
from jarvis_core.app import JarvisApp  # noqa: E402
from jarvis_core.gui import EventBus, GuiUI  # noqa: E402
from jarvis_core.llm import OllamaClient, OllamaError, gpu_share  # noqa: E402

NVIDIA_SMI = ("0, NVIDIA GeForce RTX 3060 Ti, 3, 6091, 8192, 41\n"
              "1, NVIDIA GeForce RTX 5060 Ti, [N/A], 12016, 16311, [N/A]\n"
              "valami zagyvaság\n")


class FitTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        self.log: list[str] = []

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def loads(self) -> list[int]:
        """A betöltési kérések kontextusméretei, sorrendben."""
        return [b["options"]["num_ctx"] for m, p, b in self.fake.requests
                if p == "/api/generate" and b.get("keep_alive") != 0]


class ContextFitTests(FitTestCase):
    def client(self) -> OllamaClient:
        return OllamaClient(self.cfg.llm)

    def test_candidates(self):
        self.assertEqual(self.client().context_candidates(), [16384, 12288, 8192])
        self.cfg.llm.num_ctx = 32768
        self.assertEqual(self.client().context_candidates(), [32768, 16384, 12288, 8192])
        self.cfg.llm.num_ctx, self.cfg.llm.min_ctx = 16384, 4096
        self.assertEqual(self.client().context_candidates(), [16384, 12288, 8192, 4096])
        self.cfg.llm.min_ctx = 0                       # kikapcsolva
        self.assertEqual(self.client().context_candidates(), [16384])
        self.cfg.llm.num_ctx, self.cfg.llm.min_ctx = 8192, 8192
        self.assertEqual(self.client().context_candidates(), [8192])
        self.cfg.llm.num_ctx = 0                       # az Ollama beállítása: nem nyúlunk hozzá
        self.assertEqual(self.client().context_candidates(), [0])

    def test_fits_first_time(self):
        self.fake.vram_fit = lambda ctx: 1.0
        status = self.client().load_fitted(self.log.append)
        self.assertEqual(self.loads(), [16384])
        self.assertEqual(gpu_share(status), 1.0)
        self.assertEqual(self.log, [])

    def test_steps_down_until_it_fits(self):
        self.fake.vram_fit = lambda ctx: 1.0 if ctx <= 12288 else 0.9
        client = self.client()
        status = client.load_fitted(self.log.append)
        self.assertEqual(self.loads(), [16384, 12288])
        self.assertEqual(client.num_ctx, 12288)
        self.assertEqual(status["context_length"], 12288)
        self.assertIn("kb. 10%-a a processzorra került", self.log[0])
        self.assertIn("(12288 token)", self.log[1])
        # Utána minden kérés ezt a kontextust küldi, különben az Ollama újratöltené a modellt.
        self.fake.chat_script = [reply("ok"), reply("ok")]
        client.chat([{"role": "user", "content": "szia"}])
        client.vision("Mi van a képen?", "aGVsbG8=")
        self.assertEqual([b["options"]["num_ctx"] for b in self.fake.chat_bodies()], [12288, 12288])

    def test_keeps_the_smaller_context_when_it_helps(self):
        shares = {16384: 0.80, 12288: 0.85, 8192: 0.90}      # nem fér el, de egyre több jut a GPU-ra
        self.fake.vram_fit = shares.get
        client = self.client()
        status = client.load_fitted(self.log.append)
        self.assertEqual(self.loads(), [16384, 12288, 8192])
        self.assertEqual(client.num_ctx, 8192)
        self.assertAlmostEqual(gpu_share(status), 0.90)

    def test_restores_full_context_when_smaller_does_not_help(self):
        # Ha más programok foglalják a videómemóriát, a kisebb kontextus nem segít: ugyanolyan
        # lassú, csak kevesebb fér bele. Ilyenkor visszaáll a beállított méretre. A második próbából
        # már látszik, hogy a még kisebb sem segítene, ezért azt be sem tölti (egy betöltés 10–20 mp).
        self.fake.vram_fit = lambda ctx: 0.885 - ctx / 10_000_000
        client = self.client()
        status = client.load_fitted(self.log.append)
        self.assertEqual(self.loads(), [16384, 12288, 16384])
        self.assertEqual(client.num_ctx, 16384)
        self.assertEqual(status["context_length"], 16384)
        self.assertIn("visszaállok 16384 tokenre", self.log[-1])

    def test_a_few_points_do_not_cost_the_context(self):
        # Az Ollama 0.40 (llama-server) tartalékot hagy a kártyákon: 16K-val és 12K-val 62/66 réteg fér el,
        # 8K-val 64/66. Ezért a pár százalékért nem érdemes a böngészéshez kevés 8K-ra váltani.
        shares = {16384: 0.90, 12288: 0.90, 8192: 0.93}
        self.fake.vram_fit = shares.get
        client = self.client()
        client.load_fitted(self.log.append)
        self.assertEqual(client.num_ctx, 16384)
        self.assertEqual(self.loads(), [16384, 12288, 16384])

    def test_the_decision_is_remembered(self):
        memory = Path(self.tmp.name) / "illesztes.json"
        self.fake.vram_fit = {16384: 0.90, 12288: 0.90, 8192: 0.93}.get
        first = self.client()
        first.fit_memory, first.server_version = memory, "0.40.4"
        first.load_fitted(self.log.append)
        loads_before = len(self.loads())
        again = self.client()
        again.fit_memory, again.server_version = memory, "0.40.4"
        again.load_fitted(self.log.append)
        self.assertEqual(self.loads()[loads_before:], [16384], "legközelebb (pl. felébredéskor) egy betöltés")
        self.assertEqual(again.num_ctx, 16384)

        newer = self.client()                          # új Ollama: újra kipróbálja
        newer.fit_memory, newer.server_version = memory, "0.41.0"
        newer.load_fitted(self.log.append)
        self.assertEqual(self.loads()[loads_before + 1:], [16384, 12288, 16384])

        self.fake.vram_fit = lambda ctx: 0.70             # most kevesebb a hely (pl. játék fut)
        crowded = self.client()
        crowded.fit_memory, crowded.server_version = memory, "0.40.4"
        crowded.load_fitted(self.log.append)
        self.assertIn("kevesebb a hely, mint legutóbb", " ".join(self.log))

    def test_old_memory_is_ignored(self):
        import json
        import time
        memory = Path(self.tmp.name) / "illesztes.json"
        client = self.client()
        client.fit_memory = memory
        key = client._fit_key(client.context_candidates())
        memory.write_text(json.dumps({key: {"num_ctx": 8192, "share": 1.0, "at": time.time() - 30 * 86400}}),
                          encoding="utf-8")
        self.fake.vram_fit = lambda ctx: 1.0
        client.load_fitted(self.log.append)
        self.assertEqual(self.loads(), [16384], "egy hónapos emlék helyett újra megnézi")

    def test_one_layer_on_cpu_counts(self):
        # Egy réteg (a modell kb. 1,5%-a) is kb. 20%-kal lassít, ezért ezt is javítani kell.
        self.fake.vram_fit = lambda ctx: 1.0 if ctx <= 12288 else 0.985
        client = self.client()
        client.load_fitted(self.log.append)
        self.assertEqual(client.num_ctx, 12288)

    def test_no_retry_when_disabled(self):
        self.cfg.llm.min_ctx = 0
        self.fake.vram_fit = lambda ctx: 0.9
        client = self.client()
        client.load_fitted(self.log.append)
        self.assertEqual(self.loads(), [16384])
        self.assertEqual(client.num_ctx, 16384)

    def test_no_retry_without_placement_info(self):
        client = self.client()                         # a hamis Ollama /api/ps-e üres
        self.assertIsNone(client.load_fitted(self.log.append))
        self.assertEqual(self.loads(), [16384])

    def test_returns_to_previous_when_smaller_load_fails(self):
        self.fake.vram_fit = lambda ctx: "váratlan hiba" if ctx == 12288 else 0.9
        client = self.client()
        client.load_fitted(self.log.append)
        self.assertEqual(self.loads(), [16384, 12288, 16384])
        self.assertEqual(client.num_ctx, 16384)
        self.assertIn("maradok a(z) 16384 tokenesnél", self.log[-1])

    def test_first_load_error_is_raised(self):
        self.fake.vram_fit = lambda ctx: "model requires more system memory"
        with self.assertRaisesRegex(OllamaError, "more system memory"):
            self.client().load_fitted()


class AppPlacementTests(FitTestCase):
    def make_app(self) -> JarvisApp:
        return JarvisApp(self.cfg, RecordingUI(), use_voice=False, use_speech=False)

    @staticmethod
    def texts(app: JarvisApp, kind: str) -> list[str]:
        return [event[1] for event in app.ui.of(kind)]

    def test_smaller_context_is_reported_with_a_tip(self):
        self.fake.vram_fit = lambda ctx: 1.0 if ctx <= 12288 else 0.9
        app = self.make_app()
        self.assertTrue(app.load_model())
        self.assertEqual(app.context_length, 12288)
        info = "\n".join(self.texts(app, "info"))
        self.assertIn("Újratöltöm kisebb kontextussal (12288 token)", info)
        self.assertIn("num_ctx = 12288", info)
        self.assertEqual(self.texts(app, "warn"), [])

    def test_warning_with_gpu_memory_when_nothing_fits(self):
        self.fake.vram_fit = lambda ctx: 0.85
        app = self.make_app()
        busy = "0, NVIDIA GeForce RTX 3060 Ti, 3, 7900, 8192, 41\n1, NVIDIA GeForce RTX 5060 Ti, 9, 15800, 16311, 50\n"
        with mock.patch.object(gpu, "query_gpus", return_value=gpu.parse(busy)):
            self.assertTrue(app.load_model())
        warns = self.texts(app, "warn")
        self.assertIn("kb. 15%-a a processzoron fut, ezért lassabban válaszolok", warns[0])
        self.assertIn("Zárd be", warns[0], "a kártyák tényleg tele vannak: más programok foglalják")
        self.assertIn("GPU 1 (RTX 5060 Ti): 15,4 / 15,9 GB foglalt, 0,5 GB szabad", warns[2])
        self.assertEqual(app.context_length, 16384, "a kisebb kontextus nem segített, maradt a 16K")

    def test_warning_when_ollama_keeps_a_reserve(self):
        # A modell 15%-a (2,7 GB) a processzoron van, pedig a kártyákon 6,2 GB szabad: ez az Ollama tartaléka.
        self.fake.vram_fit = lambda ctx: 0.85
        app = self.make_app()
        with mock.patch.object(gpu, "query_gpus", return_value=gpu.parse(NVIDIA_SMI)):
            self.assertTrue(app.load_model())
        warns = self.texts(app, "warn")
        self.assertIn("pedig a videókártyákon van még kb. 6,2 GB szabad hely", warns[0])
        self.assertIn("LLAMA_ARG_FIT_TARGET", warns[0])
        self.assertNotIn("Zárd be", warns[0])

    def test_warning_mentions_the_reduced_context(self):
        self.fake.vram_fit = {16384: 0.80, 12288: 0.85, 8192: 0.90}.get
        app = self.make_app()
        with mock.patch.object(gpu, "query_gpus", return_value=[]):
            self.assertTrue(app.load_model())
        warns = self.texts(app, "warn")
        self.assertIn("kb. 10%-a a processzoron fut (pedig már 8192 tokenre csökkentettem", warns[0])
        self.assertEqual(app.context_length, 8192)

    def test_full_fit_is_quiet(self):
        self.fake.vram_fit = lambda ctx: 1.0
        app = self.make_app()
        self.assertTrue(app.load_model())
        self.assertEqual(self.texts(app, "warn"), [])
        self.assertEqual(app.context_length, 16384)

    def test_load_model_runs_once(self):
        self.fake.vram_fit = lambda ctx: 1.0
        app = self.make_app()
        self.assertTrue(app.load_model())
        self.assertTrue(app.load_model())
        self.assertEqual(self.loads(), [16384])

    def test_too_small_context_for_browsing(self):
        self.cfg.llm.num_ctx = 4096
        self.fake.vram_fit = lambda ctx: 1.0
        app = self.make_app()
        app.load_model()
        self.assertTrue(any("böngészéshez kevés" in w for w in self.texts(app, "warn")))

    def test_ollama_not_running(self):
        self.cfg.llm.host = f"http://127.0.0.1:{free_port()}"
        app = self.make_app()
        self.assertFalse(app.load_model())
        self.assertFalse(app.model_loaded)
        self.assertIn("Nem érem el az Ollamát", self.texts(app, "error")[0])


class StubApp:
    """Csak azt jegyzi fel, milyen sorrendben indul a felület és a Jarvis."""

    def __init__(self, cfg, load_ok: bool = True):
        self.cfg = cfg
        self.name = "Jarvis"
        self.events: list[str] = []
        self.exit_requested = threading.Event()
        self.load_ok = load_ok
        self.model_loaded = False
        self.assistant = None

    def load_model(self) -> bool:
        self.events.append("load_model")
        self.model_loaded = self.load_ok
        if not self.load_ok:
            self.exit_requested.set()     # hogy a teszt ne várjon az ablak bezárására
        return self.load_ok

    def start(self) -> bool:
        self.events.append("start")
        return True

    def start_agent(self) -> None:
        self.events.append("start_agent")

    def describe(self) -> dict:
        return {"name": self.name, "model": "x", "stt": None, "voice": False, "speech": False,
                "wake_word": None, "examples": []}

    def greet(self) -> None:
        self.events.append("greet")
        self.exit_requested.set()

    def say_goodbye(self) -> None:
        self.events.append("goodbye")


class GuiStartOrderTests(unittest.TestCase):
    """Az ablak a modell után nyílik meg: a böngészőablak is foglal videómemóriát."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def run_gui(self, app: StubApp) -> list[str]:
        bus = EventBus()
        ui = GuiUI(bus, "Jarvis")

        def fake_window(url, cfg):
            app.events.append("window" + (" (modell betöltve)" if app.model_loaded else ""))
            return True

        with mock.patch.object(gui, "open_app_window", side_effect=fake_window):
            gui.run_gui(app, ui, bus, self.cfg.gui, open_window=True)
        return app.events

    def test_window_opens_after_the_model_is_loaded(self):
        events = self.run_gui(StubApp(self.cfg))
        self.assertEqual(events, ["load_model", "window (modell betöltve)", "start", "start_agent",
                                  "greet", "goodbye"])

    def test_window_still_opens_to_show_the_error(self):
        events = self.run_gui(StubApp(self.cfg, load_ok=False))
        self.assertEqual(events, ["load_model", "window", "goodbye"])


class GpuInfoTests(unittest.TestCase):
    def test_parse_nvidia_smi(self):
        gpus = gpu.parse(NVIDIA_SMI)
        self.assertEqual([g.index for g in gpus], [0, 1])
        self.assertIsNone(gpus[1].utilization)
        self.assertEqual(gpus[0].free_mb, 2101)
        self.assertEqual(gpu.memory_lines(gpus), [
            "GPU 0 (RTX 3060 Ti): 5,9 / 8,0 GB foglalt, 2,1 GB szabad",
            "GPU 1 (RTX 5060 Ti): 11,7 / 15,9 GB foglalt, 4,2 GB szabad",
        ])

    def test_system_status_lines(self):
        with mock.patch.object(gpu, "query_gpus", return_value=gpu.parse(NVIDIA_SMI)):
            self.assertEqual(tools._nvidia_smi_summary(), [
                "NVIDIA GeForce RTX 3060 Ti: terhelés 3%, VRAM 5,9 / 8,0 GB, 41 °C",
                "NVIDIA GeForce RTX 5060 Ti: VRAM 11,7 / 15,9 GB",
            ])

    def test_no_nvidia_smi(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(gpu.query_gpus(), [])


class FakeNvml:
    """Az NVIDIA-driver könyvtára (nvml.dll) helyett: ugyanúgy a ctypes-mutatókon át ad vissza értékeket."""

    def __init__(self, cards, memory_v2=True):
        self.cards = cards        # (név, terhelés vagy None, foglalt MB, összes MB, hőmérséklet)
        self.closed = False
        if not memory_v2:
            self.nvmlDeviceGetMemoryInfo_v2 = lambda handle, memory: 3   # NVML_ERROR_NOT_SUPPORTED

    def card(self, handle):
        return self.cards[handle.value - 1]

    def nvmlInit_v2(self):
        return 0

    def nvmlShutdown(self):
        self.closed = True
        return 0

    def nvmlDeviceGetCount_v2(self, count):
        count._obj.value = len(self.cards)
        return 0

    def nvmlDeviceGetHandleByIndex_v2(self, index, handle):
        handle._obj.value = index + 1
        return 0

    def nvmlDeviceGetName(self, handle, buffer, size):
        buffer.value = self.card(handle)[0].encode()
        return 0

    def nvmlDeviceGetUtilizationRates(self, handle, rates):
        load = self.card(handle)[1]
        if load is None:
            return 3
        rates._obj.gpu = load
        return 0

    def nvmlDeviceGetMemoryInfo_v2(self, handle, memory):
        assert memory._obj.version == gpu.MEMORY_V2, "a v2-es szerkezet verziószámmal megy"
        _, _, used, total, _ = self.card(handle)
        memory._obj.used, memory._obj.total = used * 2**20, total * 2**20
        memory._obj.reserved, memory._obj.free = 300 * 2**20, (total - used - 300) * 2**20
        return 0

    def nvmlDeviceGetMemoryInfo(self, handle, memory):
        _, _, used, total, _ = self.card(handle)
        memory._obj.used, memory._obj.total = (used + 300) * 2**20, total * 2**20   # a régi a tartalékkal együtt
        return 0

    def nvmlDeviceGetTemperature(self, handle, sensor, temperature):
        temperature._obj.value = self.card(handle)[4]
        return 0


CARDS = [("NVIDIA GeForce RTX 3060 Ti", 7, 6420, 8192, 52), ("NVIDIA GeForce RTX 5060 Ti", 87, 15565, 16311, 64)]


class NvmlTests(unittest.TestCase):
    def test_reads_every_card_from_the_driver(self):
        lib = FakeNvml(CARDS)
        nvml = gpu.Nvml(lib)
        cards = nvml.read()
        self.assertEqual([(c.index, c.name, c.utilization, c.used_mb, c.total_mb, c.temperature) for c in cards], [
            (0, "NVIDIA GeForce RTX 3060 Ti", 7, 6420, 8192, 52),
            (1, "NVIDIA GeForce RTX 5060 Ti", 87, 15565, 16311, 64)])
        nvml.close()
        self.assertTrue(lib.closed)

    def test_older_driver_and_missing_load(self):
        nvml = gpu.Nvml(FakeNvml([("NVIDIA GeForce RTX 3060 Ti", None, 6420, 8192, 52)], memory_v2=False))
        card = nvml.read()[0]
        self.assertIsNone(card.utilization)
        self.assertEqual(card.used_mb, 6720, "a régi lekérdezés a driver tartalékát is beleszámolja")
        self.assertEqual(nvml.read()[0].used_mb, 6720, "utána már egyből a régit kérdezi")

    def test_no_card_or_no_driver(self):
        with self.assertRaises(OSError):
            gpu.Nvml(FakeNvml([]))
        with mock.patch.object(gpu.Nvml, "_load", side_effect=OSError("nincs")):
            with mock.patch("shutil.which", return_value=None):
                self.assertEqual(gpu.make_reader(), (None, 0.0))
            with mock.patch("shutil.which", return_value="nvidia-smi"):
                reader, interval = gpu.make_reader()
                self.assertIsNotNone(reader)
                self.assertEqual(interval, 2.0, "az nvidia-smi új folyamat: ritkábban kérdezzük")

    def test_short_names(self):
        self.assertEqual(gpu.short_name("NVIDIA GeForce RTX 5060 Ti"), "RTX 5060 Ti")
        self.assertEqual(gpu.short_name("NVIDIA RTX A2000"), "RTX A2000")


class Clock:
    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class GpuMonitorTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.loads = iter(range(100))
        self.monitor = gpu.GpuMonitor(reader=self.read, interval=60.0, keep_seconds=300.0, idle_seconds=15.0,
                                      clock=self.clock)

    def tearDown(self):
        self.monitor.stop()

    def read(self):
        load = next(self.loads)
        return [gpu.GpuInfo(0, "NVIDIA GeForce RTX 3060 Ti", load, 6420, 8192, 52),
                gpu.GpuInfo(1, "NVIDIA GeForce RTX 5060 Ti", 100 - load, 15565, 16311, None)]

    def test_snapshot_has_the_last_minute_per_card(self):
        first = self.monitor.snapshot()
        self.assertTrue(first["available"])
        self.assertEqual([(g["index"], g["name"], g["total_mb"]) for g in first["gpus"]],
                         [(0, "RTX 3060 Ti", 8192), (1, "RTX 5060 Ti", 16311)])
        self.assertEqual(first["gpus"][1]["samples"], [[self.clock.now, 100, 15565, None]],
                         "az első lekérdezés sem üres")
        for _ in range(8):
            self.clock.now += 60
            self.monitor.sample()
        samples = self.monitor.snapshot()["gpus"][0]["samples"]
        self.assertEqual(len(samples), 7, "csak a megőrzött időszak (300 mp) mintái maradnak")
        self.assertEqual(samples[-1], [self.clock.now, 8, 6420, 52])

    def test_measures_only_while_someone_looks(self):
        self.monitor.snapshot()
        background = self.monitor._thread
        self.monitor.stop()                       # a háttérszál helyett itt futtatjuk a ciklusát
        background.join(5)
        self.monitor._stop = threading.Event()
        self.monitor.interval = 0.0
        self.monitor._thread = threading.current_thread()
        self.clock.now += 16                      # 16 mp-e senki sem kérdezte
        self.monitor._run()
        self.assertIsNone(self.monitor._thread, "leállt")
        with mock.patch.object(threading.Thread, "start") as start:
            self.monitor.snapshot()
        start.assert_called_once()

    def test_reader_errors_and_no_cards(self):
        broken = gpu.GpuMonitor(reader=mock.Mock(side_effect=RuntimeError("driver")), clock=self.clock)
        self.assertEqual(broken.snapshot()["gpus"], [])
        broken.stop()
        with mock.patch.object(gpu, "make_reader", return_value=(None, 0.0)):
            nothing = gpu.GpuMonitor()
            self.assertEqual(nothing.snapshot(), {"available": False, "gpus": []})


if __name__ == "__main__":
    unittest.main(verbosity=2)
