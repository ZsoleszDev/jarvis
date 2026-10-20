"""Offline tesztek: Ollama, mikrofon, hangkártya és GPU nélkül is futnak.

Futtatás a projekt mappájából:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis_core import cuda_libs, textutil  # noqa: E402
from jarvis_core.assistant import FALLBACK_REPLY, TOO_LONG_REPLY, TOO_MANY_STEPS, Assistant  # noqa: E402
from jarvis_core.app import JarvisApp  # noqa: E402
from jarvis_core.config import Config, load_config  # noqa: E402
from jarvis_core.llm import OllamaClient, OllamaError, normalize_message  # noqa: E402
from jarvis_core.recorder import Recorder, resample  # noqa: E402
from jarvis_core.tools import Toolbox  # noqa: E402
from jarvis_core.tts import Speaker, voice_urls  # noqa: E402


# --- segédek ---------------------------------------------------------------------

def reply(content: str) -> dict:
    return {"message": {"role": "assistant", "content": content},
            "eval_count": 10, "eval_duration": 500_000_000, "done": True}


def tool_reply(name: str, args) -> dict:
    return {"message": {"role": "assistant", "content": "",
                        "tool_calls": [{"function": {"name": name, "arguments": args}}]},
            "eval_count": 5, "eval_duration": 250_000_000, "done": True}


def is_warm_up(body: dict | None) -> bool:
    """A Jarvis induláskori előmelegítő kérése (egyetlen token, lásd OllamaClient.warm)."""
    return bool(body) and (body.get("options") or {}).get("num_predict") == 1


class FakeOllama:
    """Kis HTTP-szerver, ami az Ollama API-t utánozza, előre megírt válaszokkal."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict | None]] = []
        self.chat_script: list = []
        self.models = ["qwen3.8:27b"]
        self.ps: dict = {"models": []}
        # Ha meg van adva: kontextusméret -> a modell mekkora része fér a videókártyákra (0–1),
        # vagy hibaszöveg. A betöltés (/api/generate) ez alapján állítja be az /api/ps válaszát.
        self.vram_fit = None
        self.warm_ups = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def chat_bodies(self) -> list[dict]:
        return [body for method, path, body in self.requests if path == "/api/chat" and not is_warm_up(body)]

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # csendben
                pass

            def _send(self, status: int, obj: dict) -> None:
                body = json.dumps(obj).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                fake.requests.append(("GET", self.path, None))
                if self.path == "/api/version":
                    self._send(200, {"version": "0.32.12"})
                elif self.path == "/api/tags":
                    self._send(200, {"models": [{"name": m} for m in fake.models]})
                elif self.path == "/api/ps":
                    self._send(200, fake.ps)
                else:
                    self._send(404, {"error": "not found"})

            def _send_stream(self, obj: dict) -> None:
                """Úgy válaszol, mint az Ollama stream módban: soronként egy JSON-darab."""
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()

                def line(chunk: dict) -> None:
                    self.wfile.write((json.dumps(chunk) + "\n").encode("utf-8"))
                    self.wfile.flush()

                msg = obj.get("message") or {}
                try:
                    for piece in re.findall(r"\S+\s*", msg.get("content") or ""):
                        line({"message": {"role": "assistant", "content": piece}, "done": False})
                        if obj.get("chunk_delay"):   # lassú modell: darabonként vár (megszakításhoz)
                            time.sleep(obj["chunk_delay"])
                except OSError:   # a kliens közben bontotta a kapcsolatot (Állj gomb)
                    return
                if obj.get("stream_error"):   # a válasz közben leáll (pl. az Ollama modellfolyamata)
                    line({"error": obj["stream_error"]})
                    return
                if obj.get("stream_cut"):     # a kapcsolat a válasz vége („done”) előtt, hibaüzenet nélkül zárul
                    return
                if msg.get("tool_calls"):
                    line({"message": {"role": "assistant", "content": "", "tool_calls": msg["tool_calls"]},
                          "done": False})
                final = {k: v for k, v in obj.items() if k not in ("message", "chunk_delay")}
                final.update({"message": {"role": "assistant", "content": ""}, "done": True})
                line(final)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                fake.requests.append(("POST", self.path, body))
                if self.path == "/api/generate":
                    if fake.vram_fit is not None and body.get("keep_alive") != 0:
                        ctx = (body.get("options") or {}).get("num_ctx") or 4096
                        share = fake.vram_fit(ctx)
                        if isinstance(share, str):
                            self._send(500, {"error": share})
                            return
                        size = 18_000_000_000
                        fake.ps = {"models": [{"name": body["model"], "size": size,
                                               "size_vram": int(size * share), "context_length": ctx}]}
                    self._send(200, {"done": True})
                elif self.path == "/api/chat" and is_warm_up(body):
                    # Előmelegítés (egy token): nem fogyasztja az előre megírt válaszokat.
                    fake.warm_ups += 1
                    self._send(200, reply("."))
                elif self.path == "/api/chat":
                    if not fake.chat_script:
                        self._send(500, {"error": "nincs több előre megírt válasz"})
                        return
                    step = fake.chat_script.pop(0)
                    status, obj = step(body) if callable(step) else (200, step)
                    if body.get("stream") and status == 200:
                        self._send_stream(obj)
                    else:
                        self._send(status, obj)
                else:
                    self._send(404, {"error": "not found"})

        return Handler


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_cfg(tmp: Path, host: str | None = None) -> Config:
    cfg = load_config(ROOT / "config.toml")
    cfg.llm.host = host or f"http://127.0.0.1:{free_port()}"
    cfg.tools.allowed_folders = [str(tmp)]
    cfg.tools.notes_dir = str(tmp / "notes")
    # a tesztek ne írjanak se a projektmappába, se a valódi adatmappába (bejelentkezések, tárgyak)
    cfg.work.dir = str(tmp / "munka")
    cfg.browser.profile_dir = str(tmp / "browser_profile")
    cfg.data_root = tmp / "adat"
    # Emberi tempó: a mozdulatok (egérút, billentyűnkénti gépelés) a tesztekben is futnak, de várakozás
    # nélkül, és két közzététel között sem kell várni.
    cfg.browser.pace = 0.0
    cfg.work.publish_gap_minutes = 0.0
    # A helyi „hirdetési oldalak” a modell és a begyakorolt lépések útját tesztelik; a Vinted-kitöltőt
    # (vinted.py) a saját tesztjei (test_vinted.py) kapcsolják be, a Vinted űrlapját utánzó oldallal.
    cfg.work.autofill = False
    return cfg


# --- beállítások -----------------------------------------------------------------

class ConfigTests(unittest.TestCase):
    def test_default_config_loads_cleanly(self):
        cfg = load_config(ROOT / "config.toml")
        self.assertEqual(cfg.warnings, [])
        self.assertEqual(cfg.llm.model, "qwen3.8:27b")
        self.assertEqual(cfg.llm.keep_alive, -1)
        self.assertIn("jegyzettömb", cfg.apps)
        self.assertIsInstance(cfg.stt.gpu_device_index, int)
        self.assertEqual(cfg.tts.voice, "hu_HU-imre-medium")

    def test_speech_recognition_can_be_switched_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text("[stt]\nenabled = false\n", encoding="utf-8")
            loaded = load_config(path)
            self.assertFalse(loaded.stt.enabled)
            self.assertFalse(any("enabled" in w for w in loaded.warnings))
            cfg = make_cfg(Path(tmp))
            cfg.stt.enabled = False
            app = JarvisApp(cfg, mock.Mock(), use_voice=True, use_speech=False, use_wake_word=False)
            self.assertFalse(app.use_voice, "mint a --text: a beszédfelismerő be sem töltődik")

    def test_browser_calls_run_on_the_agent_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = JarvisApp(make_cfg(Path(tmp)), mock.Mock(), use_voice=False, use_speech=False, use_wake_word=False)
            with self.assertRaises(Exception):
                app.call_browser(lambda browser: 1, timeout=0.5)     # nem fut a böngésző
            app.toolbox, app.work = mock.Mock(), mock.Mock(browser_ready=True)
            app.toolbox.browser = "böngésző"
            agent = threading.Thread(target=app._agent_loop, daemon=True)
            agent.start()
            try:
                seen = []
                self.assertEqual(app.call_browser(lambda b: (seen.append(threading.current_thread()), b)[1]), "böngésző")
                self.assertIs(seen[0], agent, "a Playwright csak az ágens szálán használható")
                with self.assertRaises(ValueError):
                    app.call_browser(lambda b: int("x"))
            finally:
                app.requests.put(("quit", None))
                agent.join(5)

    def test_unknown_keys_produce_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text('[llm]\nmodel = "x"\nfoo = 1\n[furcsa]\na = 1\n', encoding="utf-8")
            cfg = load_config(path)
        self.assertEqual(cfg.llm.model, "x")
        self.assertTrue(any("foo" in w for w in cfg.warnings))
        self.assertTrue(any("furcsa" in w for w in cfg.warnings))

    def test_missing_file_uses_defaults(self):
        cfg = load_config(ROOT / "nincs_ilyen.toml")
        self.assertEqual(cfg.llm.host, "http://localhost:11434")
        self.assertTrue(cfg.warnings)


# --- szövegtisztítás -----------------------------------------------------------------

class TextUtilTests(unittest.TestCase):
    def test_clean_for_speech(self):
        raw = "**Szia!** Ez egy [link](http://x.y) és `kód`.\n- első\n- második 😀 50%"
        self.assertEqual(textutil.clean_for_speech(raw),
                         "Szia! Ez egy link és kód. első. második 50 százalék.")

    def test_urls_and_think(self):
        self.assertEqual(textutil.clean_for_speech("Nézd meg: https://idokep.hu/ma"), "Nézd meg: a link.")
        self.assertEqual(textutil.strip_think("<think>hmm</think>Válasz"), "Válasz")

    def test_fold(self):
        self.assertEqual(textutil.fold("Számla ÓRA Tükörfúrógép"), "szamla ora tukorfurogep")


# --- Ollama-kliens -----------------------------------------------------------------

class OllamaClientTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        self.client = OllamaClient(self.cfg.llm)

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def test_ensure_ready(self):
        self.assertEqual(self.client.ensure_ready(), "0.32.12")
        self.fake.models = ["llama3:latest"]
        with self.assertRaisesRegex(OllamaError, "ollama pull qwen3.8:27b"):
            self.client.ensure_ready()

    def test_connection_error_is_friendly(self):
        cfg = make_cfg(Path(self.tmp.name))
        with self.assertRaisesRegex(OllamaError, "Nem érem el az Ollamát"):
            OllamaClient(cfg.llm).version()

    def test_think_unsupported_retry(self):
        self.fake.chat_script = [
            lambda body: (400, {"error": '"qwen3.8:27b" does not support thinking'}),
            reply("rendben"),
        ]
        msg = self.client.chat([{"role": "user", "content": "szia"}])
        self.assertEqual(msg["content"], "rendben")
        first, second = self.fake.chat_bodies()
        self.assertIn("think", first)
        self.assertNotIn("think", second)
        self.assertFalse(self.client._think_supported)  # a következő kérések már think nélkül mennek

    def test_load_and_unload(self):
        self.client.load()
        self.client.unload()
        bodies = [b for m, p, b in self.fake.requests if p == "/api/generate"]
        self.assertEqual(bodies[0]["keep_alive"], -1)
        self.assertEqual(bodies[0]["prompt"], "")
        self.assertEqual(bodies[1]["keep_alive"], 0)

    def test_tokens_per_second(self):
        self.fake.chat_script = [reply("x")]
        self.client.chat([{"role": "user", "content": "x"}])
        self.assertAlmostEqual(self.client.tokens_per_second(), 20.0)

    def test_normalize_string_arguments(self):
        msg = normalize_message({"content": None, "tool_calls": [
            {"function": {"name": "f", "arguments": '{"a": 1}'}},
            {"function": {"name": "g", "arguments": "nem json"}},
        ]})
        self.assertEqual(msg["content"], "")
        self.assertEqual(msg["tool_calls"][0]["function"]["arguments"], {"a": 1})
        self.assertEqual(msg["tool_calls"][1]["function"]["arguments"], {})


# --- ágens-ciklus -------------------------------------------------------------------

class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        self.calls: list = []
        self.assistant = Assistant(self.cfg, OllamaClient(self.cfg.llm), Toolbox(self.cfg),
                                   "Teszt rendszerprompt", on_tool_call=lambda *a: self.calls.append(a))

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def test_tool_loop(self):
        def second(body):
            tool_msgs = [m for m in body["messages"] if m["role"] == "tool"]
            assert tool_msgs and tool_msgs[0]["tool_name"] == "get_datetime"
            return 200, reply("Most este van.")

        self.fake.chat_script = [tool_reply("get_datetime", {}), second]
        answer = self.assistant.ask("Hány óra van?")
        self.assertEqual(answer, "Most este van.")
        self.assertEqual([m["role"] for m in self.assistant.history],
                         ["user", "assistant", "tool", "assistant"])
        first_body = self.fake.chat_bodies()[0]
        self.assertEqual(first_body["messages"][0]["role"], "system")
        self.assertFalse(first_body["think"])
        self.assertEqual(first_body["keep_alive"], -1)
        self.assertTrue(any(t["function"]["name"] == "get_datetime" for t in first_body["tools"]))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.assistant.last_stats.tool_calls, 1)

    def test_last_round_keeps_the_tools(self):
        # Az utolsó körben is ugyanazok az eszközök mennek (különben az egész promptot újra kellene
        # feldolgozni), de amit akkor hívna, azt nem futtatjuk.
        self.cfg.assistant.max_tool_rounds = 2
        self.fake.chat_script = [tool_reply("get_datetime", {}), tool_reply("get_datetime", {}), reply("kész")]
        self.assertEqual(self.assistant.ask("csináld"), "kész")
        self.assertEqual(self.assistant.last_stats.tool_calls, 2)
        bodies = self.fake.chat_bodies()
        self.assertEqual(bodies[-1]["tools"], bodies[-2]["tools"])
        self.assertIn("Elfogyott a lépéskereted", bodies[-1]["messages"][-1]["content"])

        # ha az utolsó körben is eszközt hívna: nem fut le, és a mentőválasz jön
        self.fake.chat_script = [tool_reply("get_datetime", {})] * 3
        answer = self.assistant.ask("megint")
        self.assertTrue(answer.startswith(TOO_MANY_STEPS), answer)
        self.assertEqual(self.assistant.last_stats.tool_calls, 2)
        bodies = self.fake.chat_bodies()
        self.assertEqual(bodies[-1]["tools"], bodies[-2]["tools"])

    def test_reply_length_is_bounded(self):
        # Minden kör válasza korlátos (num_predict): egy beragadt, ismétlődő válasz se foglalja percekig a modellt.
        self.fake.chat_script = [reply("Szia!")]
        self.assistant.ask("szia")
        self.assertEqual(self.fake.chat_bodies()[0]["options"]["num_predict"], 2048)
        work = Assistant(self.cfg, OllamaClient(self.cfg.llm), Toolbox(self.cfg), "munka",
                         options={"temperature": 0.3, "num_predict": 1024})
        self.fake.chat_script = [reply("Kész.")]
        work.ask("töltsd fel")
        options = self.fake.chat_bodies()[1]["options"]
        self.assertEqual((options["temperature"], options["num_predict"]), (0.3, 1024))
        self.assertEqual(options["num_ctx"], self.fake.chat_bodies()[0]["options"]["num_ctx"],
                         "a kontextus ugyanaz, különben az Ollama újratöltené a modellt")

    def test_cut_short_tool_call_is_retried_once_then_given_up(self):
        # A válasz a hosszhatárnál félbemaradt, mielőtt az eszközhívás kész lett volna: egyszer újrakérdezzük
        # röviden, és ha megint így jár, tiszta hibaüzenet lesz belőle (nem akad el).
        cut = {**reply('<tool_call>{"name": "browser_click", "arguments": {"element": 1' + ", 1" * 50),
               "done_reason": "length"}
        self.fake.chat_script = [cut, tool_reply("get_datetime", {}), reply("Este van.")]
        self.assertEqual(self.assistant.ask("Hány óra?"), "Este van.")
        bodies = self.fake.chat_bodies()
        self.assertIn("félbemaradt", bodies[1]["messages"][-1]["content"])
        self.assertNotIn("félbemaradt", str(bodies[2]["messages"]), "a jelzés nem marad az előzményekben")
        self.assertEqual([m["role"] for m in self.assistant.history[-4:]], ["user", "assistant", "tool", "assistant"])

        self.fake.chat_script = [dict(cut), dict(cut)]
        self.assertEqual(self.assistant.ask("Hány óra?"), TOO_LONG_REPLY)
        self.assertEqual(self.assistant.history[-1]["content"], TOO_LONG_REPLY)

    def test_long_text_answer_is_kept_when_cut(self):
        self.fake.chat_script = [{**reply("Ez egy nagyon hosszú válasz"), "done_reason": "length"}]
        self.assertEqual(self.assistant.ask("Mesélj"), "Ez egy nagyon hosszú válasz …")

    def test_json_side_calls_are_bounded(self):
        client = OllamaClient(self.cfg.llm)
        self.fake.chat_script = [reply('{"choice": 2}')]
        self.assertEqual(client.chat_json([{"role": "user", "content": "melyik?"}], max_tokens=16), {"choice": 2})
        self.assertEqual(self.fake.chat_bodies()[-1]["options"]["num_predict"], 16)

    def test_empty_replies_get_polite_text(self):
        self.fake.chat_script = [reply("")]
        self.assertEqual(self.assistant.ask("x"), FALLBACK_REPLY)
        self.cfg.assistant.max_tool_rounds = 1
        self.fake.chat_script = [tool_reply("get_datetime", {}), reply("")]
        answer = self.assistant.ask("y")
        self.assertTrue(answer.startswith(TOO_MANY_STEPS), answer)
        self.assertIn("folytassam", answer, "megmondja, hogy onnan folytathatja")
        self.assertEqual(self.assistant.history[-1]["content"], answer)

    def test_history_rolled_back_on_error(self):
        self.fake.chat_script = [lambda body: (500, {"error": "elszállt"})]
        with self.assertRaises(OllamaError):
            self.assistant.ask("szia")
        self.assertEqual(self.assistant.history, [])

    def test_history_trimmed_in_big_steps(self):
        # 4 körnél többnél egyszerre a felére vág, utána sokáig nem nyúl az elejéhez
        # (így az Ollama prompt-gyorsítótára a legtöbb kérésnél megmarad).
        self.cfg.assistant.max_history_turns = 4
        self.fake.chat_script = [reply(str(i)) for i in range(6)]
        users = lambda: [m["content"] for m in self.assistant.history if m["role"] == "user"]
        for text in ("a", "b", "c", "d", "e"):
            self.assistant.ask(text)
        self.assertEqual(users(), ["d", "e"])
        self.assistant.ask("f")
        self.assertEqual(users(), ["d", "e", "f"])

    def test_unknown_tool_error_goes_back_to_model(self):
        self.fake.chat_script = [tool_reply("nincs_ilyen", {}), reply("Bocsánat.")]
        self.assertEqual(self.assistant.ask("x"), "Bocsánat.")
        self.assertTrue(self.assistant.history[2]["content"].startswith("HIBA"))


# --- eszközök -----------------------------------------------------------------------

class ToolboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name).resolve()
        self.cfg = make_cfg(self.tmp)
        self.notified: list[str] = []
        self.box = Toolbox(self.cfg, notify=self.notified.append, confirm=lambda _p: False)

    def tearDown(self):
        self.box.cancel_timers()
        self.tmp_dir.cleanup()

    def test_schemas(self):
        names = self.box.names()
        for schema in self.box.schemas():
            self.assertEqual(schema["type"], "function")
            self.assertEqual(schema["function"]["parameters"]["type"], "object")
        self.assertNotIn("run_powershell", names)
        open_app = next(s for s in self.box.schemas() if s["function"]["name"] == "open_app")
        self.assertIn("jegyzettömb", open_app["function"]["parameters"]["properties"]["name"]["enum"])

    def test_get_datetime_is_hungarian(self):
        result = self.box.call("get_datetime", {})
        self.assertTrue(any(day in result for day in ("hétfő", "kedd", "szerda", "csütörtök",
                                                      "péntek", "szombat", "vasárnap")))

    def test_system_status(self):
        self.assertIn("Processzor", self.box.call("system_status", {}))

    def test_find_files(self):
        (self.tmp / "Számla_október.pdf").write_text("x")
        (self.tmp / "sub").mkdir()
        (self.tmp / "sub" / "jegyzet.txt").write_text("x")
        (self.tmp / "node_modules").mkdir()
        (self.tmp / "node_modules" / "szamla_oktober.pdf").write_text("x")
        result = self.box.call("find_files", {"query": "szamla oktober"})
        self.assertIn("Számla_október.pdf", result)
        self.assertNotIn("node_modules", result)
        self.assertIn("jegyzet.txt", self.box.call("find_files", {"extension": ".txt"}))
        self.assertTrue(self.box.call("find_files", {}).startswith("HIBA"))
        self.assertEqual(self.box.call("find_files", {"query": "nincsilyen"}), "Nincs találat.")

    def test_read_text_file_encodings(self):
        text = "árvíztűrő tükörfúrógép"
        (self.tmp / "utf8.txt").write_text(text, encoding="utf-8")
        (self.tmp / "regi.txt").write_bytes(text.encode("cp1250"))
        self.assertIn(text, self.box.call("read_text_file", {"path": str(self.tmp / "utf8.txt")}))
        self.assertIn(text, self.box.call("read_text_file", {"path": str(self.tmp / "regi.txt")}))
        # relatív út az engedélyezett mappához képest
        self.assertIn(text, self.box.call("read_text_file", {"path": "utf8.txt"}))

    def test_read_text_file_truncates(self):
        self.cfg.tools.max_read_chars = 10
        (self.tmp / "hosszu.txt").write_text("a" * 100, encoding="utf-8")
        result = self.box.call("read_text_file", {"path": str(self.tmp / "hosszu.txt")})
        self.assertIn("csak az első 10 karakter", result)
        self.assertTrue(result.endswith("a" * 10))

    def test_read_outside_allowed_folders_denied(self):
        with tempfile.TemporaryDirectory() as other:
            secret = Path(other) / "titok.txt"
            secret.write_text("titok", encoding="utf-8")
            result = self.box.call("read_text_file", {"path": str(secret)})
        self.assertTrue(result.startswith("HIBA"))
        self.assertIn("engedélyezett", result)

    def test_read_unsupported_type(self):
        (self.tmp / "doc.pdf").write_bytes(b"%PDF")
        self.assertIn("fájltípust", self.box.call("read_text_file", {"path": str(self.tmp / "doc.pdf")}))

    def test_open_file_blocks_executables(self):
        (self.tmp / "setup.exe").write_bytes(b"MZ")
        (self.tmp / "valami.bat").write_text("echo")
        (self.tmp / "level.pdf").write_bytes(b"%PDF")
        with mock.patch("jarvis_core.tools._open_with_default_app") as opener:
            self.assertTrue(self.box.call("open_file", {"path": str(self.tmp / "setup.exe")}).startswith("HIBA"))
            self.assertTrue(self.box.call("open_file", {"path": str(self.tmp / "valami.bat")}).startswith("HIBA"))
            opener.assert_not_called()
            self.assertIn("Megnyitva", self.box.call("open_file", {"path": str(self.tmp / "level.pdf")}))
            opener.assert_called_once()

    def test_save_note(self):
        result = self.box.call("save_note", '{"text": "Hívd fel a szerelőt"}')
        self.assertIn("Jegyzet mentve", result)
        notes = list((self.tmp / "notes").glob("*.md"))
        self.assertEqual(len(notes), 1)
        self.assertIn("Hívd fel a szerelőt", notes[0].read_text(encoding="utf-8"))
        self.assertEqual(self.box.call("save_note", {}), "HIBA: hiányzó paraméter: text")

    def test_timer(self):
        self.assertTrue(self.box.call("set_timer", {"minutes": "abc"}).startswith("HIBA"))
        self.assertTrue(self.box.call("set_timer", {"minutes": 0}).startswith("HIBA"))
        result = self.box.call("set_timer", {"minutes": 1 / 60, "message": "teszt"})
        self.assertIn("1 másodperc", result)
        deadline = time.monotonic() + 3
        while not self.notified and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.notified, ["Időzítő: teszt"])

    def test_open_app_matching(self):
        with mock.patch("jarvis_core.tools._launch") as launch:
            self.assertEqual(self.box.call("open_app", {"name": "Jegyzettomb"}), "Elindítva: jegyzettömb")
            launch.assert_called_with("notepad.exe")
            self.assertEqual(self.box.call("open_app", {"name": "vscode"}), "Elindítva: vs code")
            self.assertTrue(self.box.call("open_app", {"name": "photoshop"}).startswith("HIBA"))

    def test_open_website_validation(self):
        with mock.patch("jarvis_core.tools.webbrowser.open") as browser:
            self.assertIn("https://idokep.hu", self.box.call("open_website", {"url": "idokep.hu"}))
            browser.assert_called_with("https://idokep.hu")
            self.assertTrue(self.box.call("open_website", {"url": "file:///etc/passwd"}).startswith("HIBA"))
            self.box.open_search_in_browser("holnapi időjárás")
            self.assertIn("google.com/search?q=", browser.call_args[0][0])

    def test_powershell_needs_confirmation(self):
        self.cfg.tools.allow_powershell = True
        box = Toolbox(self.cfg, confirm=lambda _p: False)
        self.assertIn("run_powershell", box.names())
        with mock.patch("jarvis_core.tools.subprocess.run") as run:
            result = box.call("run_powershell", {"command": "Remove-Item C:\\ -Recurse"})
            run.assert_not_called()
        self.assertIn("nem engedélyezte", result)


# --- mikrofon (hamis hangkártyával) ---------------------------------------------------

class FakeInputStream:
    def __init__(self, sd, callback, blocksize):
        self.sd, self.callback, self.blocksize = sd, callback, blocksize
        self._stop = threading.Event()

    def __enter__(self):
        def run():
            for block in self.sd.source(self.blocksize):
                if self._stop.is_set():
                    break
                self.callback(block.reshape(-1, 1), len(block), None, None)
                time.sleep(0.0003)

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self.thread.join(1)


class FakeSoundDevice:
    """(másodperc, zajszint) szakaszok, utána végtelen csend."""

    def __init__(self, segments, supports_16k=True):
        self.segments = segments
        self.supports_16k = supports_16k
        self.rate = 16000

    def check_input_settings(self, **kwargs):
        if not self.supports_16k:
            raise ValueError("nem támogatott mintavétel")

    def query_devices(self, device=None, kind=None):
        return {"name": "Teszt mikrofon", "default_samplerate": 48000}

    def InputStream(self, samplerate, channels, dtype, blocksize, device, callback):
        self.rate = samplerate
        return FakeInputStream(self, callback, blocksize)

    def source(self, blocksize):
        rng = np.random.default_rng(1)
        for seconds, level in self.segments:
            for _ in range(int(round(seconds * self.rate / blocksize))):
                yield rng.normal(0, level, blocksize).astype(np.float32)
        while True:
            yield rng.normal(0, 0.001, blocksize).astype(np.float32)


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.toml").audio

    def test_records_until_silence(self):
        rec = Recorder(self.cfg, backend=FakeSoundDevice([(0.5, 0.001), (1.0, 0.2)]))
        audio = rec.record()
        self.assertIsNotNone(audio)
        expected = 0.5 + 1.0 + self.cfg.silence_seconds   # csend + beszéd + a lezáró csend
        self.assertAlmostEqual(audio.size / 16000, expected, delta=0.15)

    def test_no_speech_returns_none(self):
        rec = Recorder(self.cfg, backend=FakeSoundDevice([]))
        self.assertIsNone(rec.record())

    def test_click_is_not_speech(self):
        rec = Recorder(self.cfg, backend=FakeSoundDevice([(0.5, 0.001), (0.05, 0.5)]))
        self.assertIsNone(rec.record())

    def test_calibrate(self):
        rec = Recorder(self.cfg, backend=FakeSoundDevice([(1.5, 0.006)]))
        noise, threshold = rec.calibrate()
        self.assertAlmostEqual(noise, 0.006, delta=0.001)
        self.assertAlmostEqual(threshold, noise * 3, delta=1e-9)

    def test_resamples_when_16k_unsupported(self):
        rec = Recorder(self.cfg, backend=FakeSoundDevice([(0.5, 0.001), (1.0, 0.2)], supports_16k=False))
        self.assertEqual(rec.rate, 48000)
        audio = rec.record()
        self.assertAlmostEqual(audio.size / 16000, 1.5 + self.cfg.silence_seconds, delta=0.15)

    def test_resample_function(self):
        self.assertEqual(resample(np.zeros(48000, dtype=np.float32), 48000, 16000).size, 16000)


# --- felolvasás (hamis Piperrel) -------------------------------------------------------

class TtsTests(unittest.TestCase):
    def test_voice_urls(self):
        model, config = voice_urls("hu_HU-imre-medium")
        self.assertEqual(model, "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
                                "hu/hu_HU/imre/medium/hu_HU-imre-medium.onnx?download=true")
        self.assertTrue(config.endswith("hu_HU-imre-medium.onnx.json?download=true"))
        with self.assertRaises(ValueError):
            voice_urls("imre")

    def test_speaker_cleans_text_and_plays(self):
        received: list[str] = []

        class FakeChunk:
            sample_rate = 22050
            audio_int16_array = np.ones(100, dtype=np.int16)

        class FakeVoice:
            config = types.SimpleNamespace(sample_rate=22050)

            @staticmethod
            def load(model_path, config_path=None):
                return FakeVoice()

            def synthesize(self, text, syn_config=None):
                received.append(text)
                yield FakeChunk()
                yield FakeChunk()

        class FakeOut:
            def __init__(self):
                self.played = []

            def play(self, data, samplerate, device=None):
                self.played.append((data, samplerate))

            def get_stream(self):
                return types.SimpleNamespace(active=False)

            def stop(self):
                pass

        fake_piper = types.ModuleType("piper")
        fake_piper.PiperVoice = FakeVoice
        fake_piper.SynthesisConfig = lambda **kw: kw
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules, {"piper": fake_piper}):
            cfg = load_config(ROOT / "config.toml")
            cfg.tts.voices_dir = tmp
            (Path(tmp) / "hu_HU-imre-medium.onnx").write_bytes(b"x")
            (Path(tmp) / "hu_HU-imre-medium.onnx.json").write_text("{}")
            out = FakeOut()
            speaker = Speaker(cfg.tts, cfg.audio, backend=out)
            speaker.say("**Szia!** 50%")
        self.assertEqual(received, ["Szia! 50 százalék."])
        data, rate = out.played[0]
        self.assertEqual(rate, 22050)
        self.assertEqual(data.dtype, np.int16)
        self.assertEqual(data.size, 200 + int(22050 * 0.12))  # két mondat és köztük szünet


class CudaLibsTests(unittest.TestCase):
    def test_status_never_crashes(self):
        usable, reason = cuda_libs.status()
        self.assertIsInstance(usable, bool)
        self.assertTrue(reason)


# --- beszédfelismerés (hamis faster-whisperrel) -------------------------------------------

class FakeSegment:
    def __init__(self, text):
        self.text = text


class FakeWhisperModel:
    fail_on_cuda = False
    text = " Szia Jarvis, mennyi az idő?"

    def __init__(self, name, device="cpu", **kwargs):
        if device == "cuda" and FakeWhisperModel.fail_on_cuda:
            raise RuntimeError("no kernel image is available for execution on the device")
        self.name, self.device, self.kwargs = name, device, kwargs
        self.last_kwargs = {}

    def transcribe(self, audio, **kwargs):
        self.last_kwargs = kwargs
        return iter([FakeSegment(FakeWhisperModel.text)]), None


class SpeechToTextTests(unittest.TestCase):
    def setUp(self):
        from jarvis_core import stt as stt_module

        self.stt_module = stt_module
        fake = types.ModuleType("faster_whisper")
        fake.WhisperModel = FakeWhisperModel
        self.patches = [mock.patch.dict(sys.modules, {"faster_whisper": fake})]
        for p in self.patches:
            p.start()
        FakeWhisperModel.fail_on_cuda = False
        FakeWhisperModel.text = " Szia Jarvis, mennyi az idő?"
        self.cfg = load_config(ROOT / "config.toml").stt
        self.log: list[str] = []

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def make(self, status):
        with mock.patch.object(self.stt_module.cuda_libs, "status", return_value=status):
            return self.stt_module.SpeechToText(self.cfg, log=self.log.append)

    def test_uses_gpu_when_available(self):
        stt = self.make((True, "2 CUDA-eszköz"))
        self.assertEqual(stt.describe(), "turbo (GPU)")
        self.assertEqual(stt.model.kwargs["device_index"], 1)
        self.assertEqual(stt.model.kwargs["compute_type"], "int8_float16")

    def test_falls_back_to_cpu_when_gpu_fails(self):
        FakeWhisperModel.fail_on_cuda = True
        stt = self.make((True, "2 CUDA-eszköz"))
        self.assertEqual(stt.describe(), "small (CPU)")
        self.assertTrue(any("CPU-ra váltok" in line for line in self.log))

    def test_cpu_when_libraries_missing(self):
        stt = self.make((False, "hiányzó CUDA-könyvtár: cudnn64_9.dll"))
        self.assertEqual(stt.device, "cpu")
        self.assertTrue(any("cudnn64_9.dll" in line for line in self.log))

    def test_forced_cuda_raises_when_unavailable(self):
        self.cfg.device = "cuda"
        with self.assertRaises(RuntimeError):
            self.make((False, "nincs GPU"))

    def test_transcribe_and_hallucination_filter(self):
        stt = self.make((False, "nincs GPU"))
        audio = np.zeros(16000, dtype=np.float32)
        self.assertEqual(stt.transcribe(audio), "Szia Jarvis, mennyi az idő?")
        self.assertEqual(stt.model.last_kwargs["language"], "hu")
        self.assertTrue(stt.model.last_kwargs["vad_filter"])
        FakeWhisperModel.text = " Feliratozta: valaki"
        self.assertEqual(stt.transcribe(audio), "")


# --- teljes indítás gépelős módban --------------------------------------------------------

class EndToEndTests(unittest.TestCase):
    def test_text_mode_session(self):
        fake = FakeOllama()
        fake.chat_script = [tool_reply("get_datetime", {}), reply("Most este hét óra van.")]
        # 16K-val a modell 10%-a a processzorra kerülne, 12K-val már teljesen elfér.
        fake.vram_fit = lambda ctx: 1.0 if ctx <= 12288 else 0.9
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg_path = Path(tmp) / "config.toml"
                cfg_path.write_text(
                    f"[llm]\nhost = '{fake.url}'\n"
                    f"[tools]\nallowed_folders = ['{tmp}']\nnotes_dir = '{tmp}'\n"
                    f"[work]\ndir = '{tmp}/munka'\n[browser]\nprofile_dir = '{tmp}/profil'\n"
                    "[apps]\n'jegyzettömb' = 'notepad.exe'\n",
                    encoding="utf-8",
                )
                env = dict(os.environ, PYTHONUTF8="1", NO_COLOR="1", JARVIS_DATA_DIR=str(Path(tmp) / "adat"))
                proc = subprocess.run(
                    [sys.executable, str(ROOT / "jarvis.py"), "--console", "--text", "--mute",
                     "--config", str(cfg_path)],
                    input="Hány óra van?\nq\n", capture_output=True, text=True, encoding="utf-8",
                    timeout=60, env=env, cwd=str(ROOT),
                )
        finally:
            fake.close()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Most este hét óra van.", proc.stdout)
        self.assertIn("[eszköz] get_datetime", proc.stdout)
        self.assertIn("kb. 10%-a a processzorra került", proc.stdout)
        self.assertIn("Újratöltöm kisebb kontextussal (12288 token)", proc.stdout)
        self.assertIn("num_ctx = 12288", proc.stdout)                 # tipp a gyorsabb induláshoz
        self.assertNotIn("a processzoron fut", proc.stdout)           # a végén már nincs mit javítani
        generate = [b for m, p, b in fake.requests if p == "/api/generate"]
        self.assertEqual(generate[0]["options"]["num_ctx"], 16384, "a Jarvis maga kéri a 16K kontextust")
        self.assertEqual(generate[1]["options"]["num_ctx"], 12288, "nem fért el, kisebbel újratölt")
        self.assertEqual(generate[-1]["keep_alive"], 0, "kilépéskor ki kell üríteni a modellt")
        self.assertEqual({b["options"]["num_ctx"] for b in fake.chat_bodies()}, {12288},
                         "a kérések a betöltött kontextust küldik, különben az Ollama újratöltene")


if __name__ == "__main__":
    unittest.main(verbosity=2)
