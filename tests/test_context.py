"""Regressziós teszt a "no user query found in messages" összeomlásra.

A valódi Ollama, ha a beszélgetés nem fér bele a kontextusba, csendben levágja az elejét.
Ha ezzel a felhasználó kérdése is kiesik, a Qwen sablonja hibát dob. Ez a hamis szerver
ugyanígy viselkedik, és a teszt egy hosszú, sok oldalnézetes böngészést játszik le.

Futtatás:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_offline import FakeOllama, make_cfg, reply, tool_reply  # noqa: E402

from jarvis_core.assistant import Assistant  # noqa: E402
from jarvis_core.llm import OllamaClient, OllamaError  # noqa: E402
from jarvis_core.tools import Toolbox, params  # noqa: E402

REAL_CHARS_PER_TOKEN = 2.2   # a "valódi" tokenizáló kicsit rosszabb, mint a Jarvis becslése


def ollama_like(response: dict, num_ctx: int):
    """Úgy dönt a kontextusról, mint az Ollama: a régi üzeneteket eldobja, a rendszerüzenetet nem."""

    def step(body):
        def tokens(messages):
            text = json.dumps(messages, ensure_ascii=False) + json.dumps(body.get("tools", []))
            return len(text) / REAL_CHARS_PER_TOKEN

        messages = list(body["messages"])
        while tokens(messages) > num_ctx and len(messages) > 2:
            del messages[1]   # a legrégebbi nem-rendszer üzenet esik ki
        if not any(m["role"] == "user" for m in messages):
            return 500, {"error": "no user query found in messages"}
        result = dict(response)
        result["prompt_eval_count"] = int(tokens(messages))
        return 200, result

    return step


class ContextOverflowTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        self.llm = OllamaClient(self.cfg.llm)
        self.box = Toolbox(self.cfg)
        counter = iter(range(1, 100))
        # egy nagy, menükkel teli webshop-oldalnézet (kb. 3500 token)
        self.box.register("browser_open", "teszt", params({"url": {"type": "string"}}),
                          lambda url: f"OLDAL-{next(counter)} " + "Kategória link " * 500,
                          ephemeral=True)

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def browse_script(self, steps: int, num_ctx: int) -> list:
        script = [ollama_like(tool_reply("browser_open", {"url": f"vinted.hu/{i}"}), num_ctx)
                  for i in range(steps)]
        script.append(ollama_like(reply("Nem találtam 2500 Ft alatti fekete L-es pólót."), num_ctx))
        return script

    def test_long_browsing_never_loses_the_question(self):
        assistant = Assistant(self.cfg, self.llm, self.box, "Rendszerprompt. " * 50)
        assistant.context_tokens = 8192
        self.fake.chat_script = self.browse_script(steps=9, num_ctx=8192)
        answer = assistant.ask("Keress nekem vinteden fekete L-es pólót 2500Ft-ig")
        self.assertIn("pólót", answer)
        for body in self.fake.chat_bodies():
            users = [m for m in body["messages"] if m["role"] == "user"]
            self.assertEqual(users[-1]["content"], "Keress nekem vinteden fekete L-es pólót 2500Ft-ig")

    def test_previous_turns_dropped_before_current_question(self):
        assistant = Assistant(self.cfg, self.llm, self.box, "Rendszerprompt.")
        assistant.context_tokens = 8192
        self.fake.chat_script = [reply("Szia!")] + self.browse_script(steps=6, num_ctx=8192)
        assistant.ask("Szia")
        assistant.ask("Keress pólót")
        self.assertEqual(assistant.history[0]["content"], "Keress pólót")

    def test_huge_single_page_is_shortened(self):
        self.box.register("browser_read", "teszt", params(), lambda: "Szöveg " * 6000, ephemeral=True)
        assistant = Assistant(self.cfg, self.llm, self.box, "Rendszerprompt.")
        assistant.context_tokens = 8192
        self.fake.chat_script = [ollama_like(tool_reply("browser_read", {}), 8192),
                                 ollama_like(reply("Összefoglaltam."), 8192)]
        self.assertEqual(assistant.ask("Olvasd el"), "Összefoglaltam.")
        tool_message = next(m for m in assistant.history if m["role"] == "tool")
        self.assertTrue(tool_message["content"].endswith("rövidítve…]"))

    def test_old_behaviour_would_have_crashed(self):
        """Ellenőrzés, hogy a hamis szerver tényleg előidézi a hibát, ha nem rövidítünk."""
        assistant = Assistant(self.cfg, self.llm, self.box, "Rendszerprompt.")
        assistant.context_tokens = 10_000_000    # a Jarvis azt hiszi, végtelen a hely
        self.fake.chat_script = self.browse_script(steps=9, num_ctx=8192)
        with self.assertRaisesRegex(OllamaError, "no user query found"):
            assistant.ask("Keress pólót")


if __name__ == "__main__":
    unittest.main(verbosity=2)
