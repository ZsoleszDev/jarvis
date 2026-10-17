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

import psutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_offline import FakeOllama, make_cfg, reply, tool_reply  # noqa: E402
from test_web_browser import HAS_CHROMIUM, _QuietHandler  # noqa: E402
from test_work import SHIRT, jpeg  # noqa: E402

from jarvis_core import app as app_module  # noqa: E402
from jarvis_core import llm  # noqa: E402
from jarvis_core.app import JarvisApp, model_problem  # noqa: E402
from jarvis_core.browser import RESTARTED_NOTE, BrowserController, BrowserError, blocker_of  # noqa: E402
from jarvis_core.config import load_config  # noqa: E402
from jarvis_core.gui import EventBus, GuiUI  # noqa: E402
from jarvis_core.llm import OllamaClient, OllamaError, is_runner_crash  # noqa: E402
from jarvis_core import memory  # noqa: E402
from jarvis_core.memory import GB, MemoryReport, OllamaProcess  # noqa: E402
from jarvis_core.ollama_log import ServerLog, explain, find_cause  # noqa: E402
from jarvis_core.selling import FAILED  # noqa: E402

# Az Ollama 0.40 pontos hibaüzenetei, amikor a modellfolyamat leáll (a felhasználó naplójából)
CRASH_AFTER = ("model runner has unexpectedly stopped, this may be due to resource limitations or an internal "
               "error, check ollama server logs for details")
CRASH_DURING = ("error reading llama-server response: read tcp 127.0.0.1:62287->127.0.0.1:57861: wsarecv: "
                "A létező kapcsolatot a távoli állomás kényszerítetten bezárta.")


# Ami ilyenkor az Ollama naplójába (server.log) kerül, ha a gép memóriája fogyott el
BAD_ALLOC_LOG = ("terminate called after throwing an instance of 'std::bad_alloc'\n"
                 "  what():  std::bad_alloc\n"
                 'time=2026-10-17T10:15:09.004+02:00 level=ERROR source=server.go:764 msg="llama runner terminated" '
                 'error="exit status 0xc0000409"\n')


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


class ServerLogTests(unittest.TestCase):
    """Miért állt le a modell: az Ollama naplójából."""

    def test_finds_the_reason(self):
        cause = find_cause('time=... level=INFO msg="llama runner started in 21.4 seconds"\n' + BAD_ALLOC_LOG)
        self.assertEqual(cause.kind, "ram", "a memóriahiba számít, nem a kilépési kód")
        self.assertIn("std::bad_alloc", cause.line)
        vram = ("ggml_backend_cuda_buffer_type_alloc_buffer: allocating 1024.00 MiB on device 1: "
                "cudaMalloc failed: out of memory\n")
        self.assertEqual(find_cause(vram).kind, "vram")
        self.assertEqual(find_cause("llama-context.cpp:123: GGML_ASSERT(obj_new) failed\n").kind, "crash")
        self.assertIsNone(find_cause('time=... level=INFO msg="llama runner started"\n'))

    def test_reads_only_what_was_written_since_the_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.log"
            path.write_text("egy régi leállás: std::bad_alloc\n", encoding="utf-8")
            log = ServerLog(path)
            log.mark()
            self.assertEqual(log.read_new(), "")
            with path.open("a", encoding="utf-8") as handle:
                handle.write("CUDA error: out of memory\n")
            self.assertEqual(find_cause(log.read_new()).kind, "vram", "a régi sor nem számít")
            path.write_text("új napló\n", encoding="utf-8")      # az Ollama újraindult: új, rövidebb napló
            self.assertEqual(log.read_new(), "új napló\n")
            self.assertEqual(ServerLog(Path(tmp) / "nincs.log").read_new(), "")
        nowhere = ServerLog(Path("x"))
        nowhere.path = None                                      # Linuxon a rendszernaplóban van
        self.assertEqual(nowhere.read_new(), "")



# A felhasználó gépéhez hasonló állapot: a véglegesített memória (RAM + lapozófájl) majdnem betelt.
FULL = MemoryReport(ram_used=int(29.6 * GB), ram_total=int(31.9 * GB), commit_used=int(35.2 * GB),
                    commit_limit=int(35.9 * GB), pagefile="fixed:4096",
                    top=[("ollama.exe", int(14.1 * GB), 2), ("msedge.exe", int(6.2 * GB), 23),
                         ("python.exe", int(2.1 * GB), 1), ("node.exe", int(0.4 * GB), 1)])
NO_SETTING = {"LLAMA_ARG_CACHE_RAM": None, "LLAMA_ARG_CTX_CHECKPOINTS": None}
CACHE_OFF = {"LLAMA_ARG_CACHE_RAM": "0", "LLAMA_ARG_CTX_CHECKPOINTS": None}


def texts(lines: list[tuple[str, str]]) -> str:
    return "\n".join(text for _, text in lines)


class CrashAdviceTests(unittest.TestCase):
    """Mit mondjon a Jarvis a leállás után: az okot, a gép memóriáját, és a javítást a tényleges állapot szerint."""

    def setUp(self):
        self.ram = find_cause(BAD_ALLOC_LOG)

    def test_without_the_setting(self):
        lines = explain(self.ram, Path("server.log"), NO_SETTING, FULL, OllamaProcess())
        self.assertEqual(lines[0][0], "warn")
        self.assertIn("elfogyott a gép memóriája (", lines[0][1])
        text = texts(lines)
        self.assertIn("RAM 29,6 / 31,9 GB foglalt; véglegesítve (RAM + lapozófájl) 35,2 / 35,9 GB", text)
        self.assertIn("A legtöbbet foglalja: ollama.exe (2 folyamat) 14,1 GB, msedge.exe (23 folyamat) 6,2 GB", text)
        self.assertIn("„setx LLAMA_ARG_CACHE_RAM 0” és „setx LLAMA_ARG_CTX_CHECKPOINTS 4”", text)
        self.assertIn("a lapozófájl legfeljebb 4 GB", text)
        self.assertIn("SystemPropertiesPerformance", text)
        self.assertIn("A böngésző (msedge.exe) 6,2 GB-ot foglal", text)
        self.assertEqual({level for level, line in lines if line.startswith("  A gép memóriája")}, {"info"})

    def test_ollama_was_not_restarted_after_the_setting(self):
        stale = OllamaProcess(found=True, readable=True, env={})
        text = texts(explain(self.ram, None, CACHE_OFF, None, stale))
        self.assertIn("A futó Ollama még a beállítás előtt indult (a környezetében nincs LLAMA_ARG_CACHE_RAM)", text)
        self.assertIn("lépj ki az Ollamából a tálcán", text)
        self.assertIn("„setx LLAMA_ARG_CTX_CHECKPOINTS 4”", text, "az ellenőrzőpontok még nincsenek beállítva")
        self.assertNotIn("setx LLAMA_ARG_CACHE_RAM", text, "az már megvan")

    def test_ollama_runs_with_the_settings(self):
        settings = {"LLAMA_ARG_CACHE_RAM": "0", "LLAMA_ARG_CTX_CHECKPOINTS": "4"}
        running = OllamaProcess(found=True, readable=True, env=dict(settings))
        roomy = MemoryReport(ram_used=20 * GB, ram_total=32 * GB, commit_used=30 * GB, commit_limit=64 * GB,
                             pagefile="auto", top=[("ollama.exe", 9 * GB, 1)])
        text = texts(explain(self.ram, None, settings, roomy, running))
        self.assertNotIn("még a beállítás előtt", text)
        self.assertNotIn("setx", text)
        self.assertNotIn("lapozófájl legfeljebb", text)
        self.assertIn("Zárj be más, sok memóriát foglaló programokat", text)

    def test_ollama_forces_its_own_cache_size(self):
        forced = OllamaProcess(found=True, readable=True, env={"LLAMA_ARG_CACHE_RAM": "0"},
                               flags={"LLAMA_ARG_CACHE_RAM": "8192"})
        self.assertIn("maga adja meg: --cache-ram 8192", texts(explain(self.ram, None, CACHE_OFF, None, forced)))

    def test_repeated_crash_is_shorter_but_complete(self):
        first = texts(explain(self.ram, None, NO_SETTING, FULL, OllamaProcess()))
        again = texts(explain(self.ram, None, NO_SETTING, FULL, OllamaProcess(), repeated=True))
        self.assertIn("A leállás oka megint: elfogyott a gép memóriája.", again)
        self.assertIn("setx LLAMA_ARG_CACHE_RAM 0", again)
        self.assertIn("35,2 / 35,9 GB", again, "a friss számokkal")
        self.assertLess(len(again), len(first))

    def test_unknown_cause(self):
        log = Path("C:/Users/x/AppData/Local/Ollama/server.log")
        self.assertIn("server.log", texts(explain(None, log, NO_SETTING)))
        self.assertIn("lapozófájl", texts(explain(None, log, NO_SETTING, FULL)), "ha betelt a memória, az a gyanús")


class FakeProc:
    def __init__(self, name, mb=0, cmdline=(), environ=None):
        self.info = {"name": name, "cmdline": list(cmdline),
                     "memory_info": mock.Mock(rss=mb * 2**20, vms=mb * 2**20, private=mb * 2**20)}
        self._environ = environ

    def environ(self):
        if isinstance(self._environ, Exception):
            raise self._environ
        return self._environ or {}


class FakePsutil:
    def __init__(self, procs):
        self.procs = procs

    def process_iter(self, attrs=None):
        return iter(self.procs)

    def virtual_memory(self):
        return mock.Mock(total=32 * GB, available=4 * GB)


class FakeRegistry:
    HKEY_LOCAL_MACHINE = "HKLM"

    def __init__(self, values):
        self.values = values

    def OpenKey(self, root, path):
        if self.values is None:
            raise OSError("nincs ilyen kulcs")
        return mock.MagicMock()

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise OSError(name)
        return self.values[name], 7


class MemoryTests(unittest.TestCase):
    def test_heavy_programs_are_grouped_by_name(self):
        fake = FakePsutil([FakeProc("msedge.exe", 900), FakeProc("msedge.exe", 1200), FakeProc("ollama.exe", 9000),
                           FakeProc("python.exe", 1500), FakeProc("", 5000), FakeProc("Idle", 0)])
        self.assertEqual(memory.heavy_programs(3, fake), [("ollama.exe", 9000 * 2**20, 1),
                                                          ("msedge.exe", 2100 * 2**20, 2),
                                                          ("python.exe", 1500 * 2**20, 1)])
        report = memory.report(2, fake)
        self.assertEqual((report.ram_used, report.ram_total), (28 * GB, 32 * GB))
        self.assertEqual(len(report.top), 2)

    def test_pagefile_setting(self):
        cases = [({"AutomaticManagedPagefile": 1, "PagingFiles": ["?:\\pagefile.sys"]}, "auto"),
                 ({"AutomaticManagedPagefile": 0, "PagingFiles": ["?:\\pagefile.sys"]}, "auto"),
                 ({"PagingFiles": ["C:\\pagefile.sys 0 0"]}, "auto"),
                 ({"PagingFiles": ["C:\\pagefile.sys 2048 4096", "D:\\pagefile.sys 1024 1024"]}, "fixed:5120"),
                 ({"PagingFiles": [""]}, "none"),
                 ({"PagingFiles": []}, "none"),
                 (None, "")]
        for values, expected in cases:
            self.assertEqual(memory.pagefile_setting(FakeRegistry(values)), expected, values)
        self.assertEqual(MemoryReport(pagefile="fixed:5120").pagefile_max_gb, 5.0)
        self.assertEqual(MemoryReport(pagefile="none").pagefile_max_gb, 0.0)
        self.assertIsNone(MemoryReport(pagefile="auto").pagefile_max_gb)

    def test_the_running_ollama_and_its_settings(self):
        fake = FakePsutil([
            FakeProc("ollama app.exe", 50, ["C:\\Ollama\\ollama app.exe"]),
            FakeProc("ollama.exe", 80, ["C:\\Ollama\\ollama.exe", "serve"],
                     {"LLAMA_ARG_CACHE_RAM": "0", "PATH": "x"}),
            FakeProc("llama-server.exe", 9000, ["llama-server.exe", "--model", "m.gguf", "--ctx-checkpoints", "8"]),
        ])
        found = memory.ollama_process(fake)
        self.assertTrue(found.found and found.readable)
        self.assertEqual(found.env, {"LLAMA_ARG_CACHE_RAM": "0"})
        self.assertEqual(found.flags, {"LLAMA_ARG_CTX_CHECKPOINTS": "8"})
        hidden = memory.ollama_process(FakePsutil([FakeProc("ollama.exe", 80, ["ollama", "serve"],
                                                            PermissionError("másik felhasználó"))]))
        self.assertTrue(hidden.found)
        self.assertFalse(hidden.readable)
        self.assertFalse(memory.ollama_process(FakePsutil([FakeProc("python.exe")])).found)


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

    def wait_for_warn(self, text: str) -> str:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for event in self.events():
                if event["type"] == "warn" and text in event["text"]:
                    return event["text"]
            time.sleep(0.02)
        self.fail(f"nem jött ilyen figyelmeztetés: {text}")

    def test_the_reason_of_a_crash_is_explained_once(self):
        log_file = Path(self.tmp.name) / "server.log"
        log_file.write_text("a korábbi futás sorai\n", encoding="utf-8")
        self.app.server_log = ServerLog(log_file)
        self.app.server_log.mark()
        with mock.patch.object(app_module, "RESTART_LOG_DELAY", 0.01), \
                mock.patch.object(app_module, "user_setting", return_value=None), \
                mock.patch.object(memory, "report", return_value=FULL), \
                mock.patch.object(memory, "ollama_process", return_value=OllamaProcess()):
            with log_file.open("a", encoding="utf-8") as handle:
                handle.write(BAD_ALLOC_LOG)              # ezt írja az Ollama a naplójába, amikor a modell leáll
            self.fake.chat_script = [crash(CRASH_DURING), reply("Itt vagyok.")]
            self.app.answer("Szia")
            self.wait_for_warn("elfogyott a gép memóriája (")
            first = self.notes_after("elfogyott a gép memóriája (")
            self.assertIn("setx LLAMA_ARG_CACHE_RAM 0", first)
            self.assertIn("véglegesítve (RAM + lapozófájl) 35,2 / 35,9 GB", first, "a gép memóriája is látszik")
            with log_file.open("a", encoding="utf-8") as handle:
                handle.write(BAD_ALLOC_LOG)
            self.fake.chat_script = [crash(CRASH_DURING), reply("Megint itt.")]
            self.app.answer("Szia újra")
            self.wait_for_warn("A leállás oka megint")
            again = self.notes_after("A leállás oka megint")
        self.assertIn("setx LLAMA_ARG_CACHE_RAM 0", again, "röviden, de a javítással együtt")
        self.assertLess(len(again), len(first), "a részletes magyarázat csak egyszer")
        self.assertEqual([r["cause"] for r in self.app.model_restarts], ["ram", "ram"])
        self.assertEqual(self.app.model_restarts[0]["memory"]["commit"], [35.2, 35.9], "a hibacsomagba is bekerül")

    def notes_after(self, head: str) -> str:
        """A figyelmeztetés és az utána jövő magyarázó sorok (amíg a következő leállás nem jön)."""
        events = [e for e in self.events() if e["type"] in ("warn", "info")]
        start = max(i for i, e in enumerate(events) if head in e["text"])
        lines = []
        for event in events[start:]:
            if lines and "futás közben leállt" in event["text"]:
                break
            lines.append(event["text"])
        return "\n".join(lines)

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


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class BrowserCrashTests(unittest.TestCase):
    """Ha a böngésző vagy a Playwright-meghajtó (node) elhal (pl. mert elfogyott a gép memóriája), a következő
    lépés újraindítja, és nem akad el. A felhasználónál így jött: „Target page, context or browser has been
    closed”, utána „Playwright connection closed”, és minden további böngészős lépés ugyanígy elbukott."""

    PAGES = {"a.html": "<!doctype html><title>A</title><button>Gomb</button>",
             "b.html": "<!doctype html><title>B</title><a href='a.html'>Vissza</a>"}

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        for name, html in cls.PAGES.items():
            (Path(cls.dir.name) / name).write_text(html, encoding="utf-8")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_QuietHandler, directory=cls.dir.name))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.dir.cleanup()

    def setUp(self):
        self.profile = tempfile.TemporaryDirectory()
        cfg = load_config(ROOT / "config.toml").browser
        cfg.headless = True
        cfg.channel = ""
        cfg.profile_dir = self.profile.name
        cfg.pace = 0.0
        before = {p.pid for p in psutil.Process().children(recursive=True)}
        self.browser = BrowserController(cfg, allow_local=True, confirm=lambda prompt: True)
        self.assertIn("Gomb", self.browser.open(self.url("a.html")))
        self.spawned = [p for p in psutil.Process().children(recursive=True) if p.pid not in before]

    def tearDown(self):
        self.browser.close()
        self.profile.cleanup()

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/{name}"

    def kill(self, driver: bool) -> None:
        drivers = [p for p in self.spawned if "node" in p.name().lower()]
        self.assertTrue(drivers, "a Playwright-meghajtó megvan")
        targets = drivers if driver else [p for p in self.spawned
                                          if p not in drivers and p.ppid() in {d.pid for d in drivers}]
        self.assertTrue(targets)
        for proc in targets:
            proc.kill()
        psutil.wait_procs(targets, timeout=5)
        time.sleep(0.5)

    def check_recovers(self) -> None:
        started = time.monotonic()
        view = self.browser.snapshot()
        self.assertIn(RESTARTED_NOTE, view, "megmondja a modellnek, hogy az oldal elveszett")
        self.assertLess(time.monotonic() - started, 30, "nem akad el")
        self.assertIn("Vissza", self.browser.open(self.url("b.html")), "utána minden megy tovább")

    def test_the_browser_crashed(self):
        self.kill(driver=False)
        self.check_recovers()

    def test_the_playwright_driver_died(self):
        self.kill(driver=True)
        self.check_recovers()

    def test_a_failed_step_lets_the_next_one_restart(self):
        self.kill(driver=True)
        with self.assertRaises(BrowserError):
            self.browser.press("Escape")
        self.assertFalse(self.browser.running, "elengedte a halott böngészőt")
        self.assertIn("Vissza", self.browser.open(self.url("b.html")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
