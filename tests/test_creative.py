"""A Kreatív menü tesztjei: történetek (kaland és karakter), a prompt felépítése, az élő írás, a másik
változat, a megszakítás, az emlékezet (összefoglalás, sűrítés, törlés), az ötletből készülő történet, a
felület végpontjai és egy valódi (láthatatlan) böngészős végigjátszás, hamis modellel.

Futtatás a projekt mappájából:
    python -m unittest discover -s tests -v
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

from test_offline import FakeOllama, make_cfg, reply  # noqa: E402
from test_recovery import CRASH_AFTER, crash  # noqa: E402
from test_web_browser import HAS_CHROMIUM  # noqa: E402

from jarvis_core import creative_templates as templates, llm  # noqa: E402
from jarvis_core.app import PAUSED_REPLY, JarvisApp  # noqa: E402
from jarvis_core.creative import (KEEP_VERBATIM, PROMPTS, CreativeDesk, StoryError, card_hits,  # noqa: E402
                                  clean_output, format_turn, trim_to_sentence)
from jarvis_core.gui import EventBus, GuiServer, GuiUI  # noqa: E402
from jarvis_core.llm import OllamaClient  # noqa: E402

LONG = ("The rain does not stop. You walk past shuttered shops and flickering signs, counting doors, listening for "
        "footsteps behind you. ") * 6


def wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class DeskCase(unittest.TestCase):
    """Egy Kreatív menü hamis modellel (az események listába gyűlnek)."""

    def setUp(self):
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        self.llm = OllamaClient(self.cfg.llm)
        self.events: list[dict] = []
        self.desk = CreativeDesk(self.llm, Path(self.tmp.name) / "kreativ", publish=self.events.append)

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def new(self, **payload) -> dict:
        story, _need = self.desk.create({"mode": "adventure", "language": "en", "template": "mystery",
                                         "persona": {"name": "Sam"}, **payload})
        return story

    def run_task(self, action: str, story_id: str, stop: threading.Event | None = None) -> None:
        self.desk.queue(story_id)
        try:
            self.desk.run({"action": action, "id": story_id}, stop)
        finally:
            self.desk.finished(story_id)

    def bodies(self) -> list[dict]:
        return self.fake.chat_bodies()


# --- szöveg -----------------------------------------------------------------------------------------------

class TextTests(unittest.TestCase):
    def test_turns_are_written_like_in_ai_dungeon(self):
        self.assertEqual(format_turn("do", "open the door", "en"), "> You open the door.")
        self.assertEqual(format_turn("do", "I draw my sword and tell him I'm ready", "en"),
                         "> You draw your sword and tell him you're ready.")
        self.assertEqual(format_turn("do", "You look around!", "en"), "> You look around!")
        self.assertEqual(format_turn("say", '"who are you"', "en"), '> You say "Who are you."')
        self.assertEqual(format_turn("say", "Who are you?", "en"), '> You say "Who are you?"')
        self.assertEqual(format_turn("do", "kinyitom az ajtót", "hu"), "> Kinyitom az ajtót.")
        self.assertEqual(format_turn("say", "Ki vagy?", "hu"), "> Azt mondod: „Ki vagy?”")
        self.assertEqual(format_turn("story", "  The wind dies down.  ", "en"), "The wind dies down.")

    def test_output_is_cleaned(self):
        story = {"mode": "adventure", "character": {"name": ""}, "persona": {"name": "Sam"}}
        self.assertEqual(clean_output("Narrator: The door opens.\n\nCold air. What do you do?", story),
                         "The door opens.\n\nCold air.")
        self.assertEqual(clean_output("The door opens.\n> You step inside.\nMore.", story), "The door opens.",
                         "a játékos következő körét nem írhatja meg")
        self.assertEqual(clean_output("<think>hmm</think>A long sentence. And a cut one that", story, cut=True),
                         "A long sentence.")
        chat = {"mode": "character", "character": {"name": "Mara Vale"}, "persona": {"name": "Sam"}}
        self.assertEqual(clean_output("Mara: *grins* Well, well.\nSam: hi\nMara: more", chat), "*grins* Well, well.")
        self.assertEqual(clean_output("**Mara Vale:** *nods* Fine.", chat), "*nods* Fine.")
        self.assertEqual(trim_to_sentence("No full stop here"), "No full stop here…")

    def test_cards_match_with_suffixes(self):
        card = {"keys": ["hamvasd", "fehér kabát"]}
        self.assertTrue(card_hits(card, "a kocsi hamvasdra tart"))          # ékezet és rag nem számít
        self.assertTrue(card_hits(card, "egy feher kabatos no"))
        self.assertFalse(card_hits(card, "a kabát fehér volt"))
        self.assertFalse(card_hits({"keys": ["art"]}, "the cart rolls"), "csak szó elején")


# --- történetek ------------------------------------------------------------------------------------------

class StoreTests(DeskCase):
    def test_every_template_starts_in_both_languages(self):
        for template in templates.ADVENTURES + templates.CHARACTERS:
            mode = "adventure" if template in templates.ADVENTURES else "character"
            for lang in ("en", "hu"):
                story, need = self.desk.create({"mode": mode, "language": lang, "template": template["key"],
                                                "persona": {"name": "Zoé"}})
                self.assertIsNone(need, "a sablonnak kész kezdése van")
                self.assertEqual(len(story["entries"]), 1)
                self.assertEqual(story["entries"][0]["kind"], "ai")
                text = json.dumps({k: story[k] for k in ("title", "plot", "note", "persona", "character", "cards",
                                                          "entries")}, ensure_ascii=False)
                self.assertNotIn("{", text.replace("{\"", "").replace("{}", ""), f"{template['key']}/{lang}")
                if mode == "adventure":
                    self.assertIn("Zoé", story["entries"][0]["text"])
                    self.assertTrue(story["cards"])
                else:
                    self.assertTrue(story["character"]["name"] and story["character"]["about"])

    def test_role_choice_and_custom_stories(self):
        story, _ = self.desk.create({"mode": "adventure", "language": "hu", "template": "fantasy", "choice": "rogue",
                                     "persona": {"name": "Rowan"}})
        self.assertTrue(story["entries"][0]["text"].startswith("Rowan vagy, fürge ujjú"))
        self.assertTrue(story["persona"]["about"].startswith("Fürge ujjú"))
        story, need = self.desk.create({"mode": "adventure", "template": "custom", "title": "Saját",
                                        "plot": "Egy hajón vagy.", "language": "hu"})
        self.assertEqual((need, story["entries"]), ("write", []), "kezdés nélkül a mesélő írja meg")
        story, need = self.desk.create({"mode": "character", "template": "custom",
                                        "character": {"name": "Bori", "about": "Morcos sárkány.", "greeting": "Mi van?"}})
        self.assertIsNone(need)
        self.assertEqual(story["title"], "Bori")
        with self.assertRaisesRegex(StoryError, "karakter nevét"):
            self.desk.create({"mode": "character", "template": "custom", "character": {"name": "Bori"}})
        with self.assertRaisesRegex(StoryError, "ötletet"):
            self.desk.create({"mode": "adventure", "template": "idea", "idea": "  "})
        self.assertEqual(self.desk.language, "en", "alapból angolul")

    def test_stories_survive_a_restart_and_bad_files_are_skipped(self):
        story = self.new()
        self.desk.add_turn(story["id"], "do", "call the police")
        folder = Path(self.tmp.name) / "kreativ"
        (folder / "deadbeef.json").write_text("{nem json", encoding="utf-8")
        (folder / "notes.txt").write_text("x", encoding="utf-8")
        fresh = CreativeDesk(self.llm, folder)
        self.assertEqual([s["id"] for s in fresh.stories()], [story["id"]])
        again = fresh.get(story["id"])
        self.assertEqual(again["entries"][-1]["text"], "> You call the police.")
        self.assertGreater(again["rev"], story["rev"])
        with self.assertRaisesRegex(StoryError, "sérült"):
            fresh.store.load("deadbeef")
        with self.assertRaises(StoryError):
            fresh.store.load("../../titok")
        fresh.delete(story["id"])
        self.assertEqual(fresh.stories(), [])

    def test_undo_redo_edit_and_delete(self):
        story = self.new()
        sid = story["id"]
        self.desk.add_turn(sid, "say", "hello?")
        self.desk.undo(sid)
        self.assertEqual(len(self.desk.get(sid)["entries"]), 1)
        story = self.desk.redo(sid)
        self.assertEqual(story["entries"][-1]["text"], '> You say "Hello?"')
        entry = story["entries"][-1]["id"]
        story = self.desk.entry(sid, entry, "edit", text="> You whisper.")
        self.assertEqual(story["entries"][-1]["text"], "> You whisper.")
        story = self.desk.entry(sid, entry, "delete")
        self.assertEqual(len(story["entries"]), 1)
        with self.assertRaisesRegex(StoryError, "visszahozni"):
            self.desk.redo(sid)
        self.desk.queue(sid)   # amíg ír, a történethez nem lehet nyúlni
        with self.assertRaisesRegex(StoryError, "Épp ezen"):
            self.desk.undo(sid)
        self.desk.finished(sid)

    def test_export_as_markdown(self):
        story = self.new()
        self.desk.add_turn(story["id"], "do", "pick up the phone")
        name, text = self.desk.export(story["id"])
        self.assertEqual(name, "Rain_on_Calder_Street.md")
        self.assertTrue(text.startswith("# Rain on Calder Street\n"))
        self.assertIn("\n> You pick up the phone.\n", text)


# --- a prompt -------------------------------------------------------------------------------------------

class PromptTests(DeskCase):
    def test_layout_of_the_prompt(self):
        story = self.new()
        sid = story["id"]
        self.desk.update(sid, {"note": "Short punchy sentences.", "instructions": "Keep it PG-13."})
        self.desk.card(sid, "add", {"name": "Okafor", "keys": ["okafor"], "text": "An honest lieutenant."})
        self.desk.card(sid, "add", {"name": "City", "keys": [], "text": "Always raining."})
        self.desk.add_turn(sid, "do", "call Lieutenant Okafor")
        story = self.desk.store.load(sid)
        messages, _ = self.desk.build_messages(story, story["entries"])
        system = messages[0]["content"]
        self.assertTrue(system.startswith("You are the narrator of an interactive story"))
        for part in ("## Additional instructions from the player\nKeep it PG-13.", "## The player character\nSam:",
                     "## Plot essentials (always true)\nPort Calder, 1926", "- City: Always raining."):
            self.assertIn(part, system)
        self.assertNotIn("Okafor: An honest", system, "a kulcsszavas kártya nem a rendszerpromptba kerül")
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "user"])
        self.assertEqual(messages[1]["content"], "(The story begins.)")
        last = messages[-1]["content"]
        self.assertIn("- Okafor: An honest lieutenant.", last, "a kulcsszó előfordult: bekerül")
        self.assertIn("Author's note: Short punchy sentences.", last)
        self.assertTrue(last.endswith("> You call Lieutenant Okafor."), "a háttér a kör előtt, a kör a végén")

    def test_continue_merge_opening_and_hungarian(self):
        story = self.new(language="hu")
        sid = story["id"]
        self.desk.add_turn(sid, "story", "Esik.")
        self.desk.add_turn(sid, "do", "felveszem a telefont")
        story = self.desk.store.load(sid)
        messages, _ = self.desk.build_messages(story, story["entries"])
        self.assertIn("magyarsággal", messages[0]["content"])
        self.assertEqual(messages[-1]["content"].split("]\n\n")[-1], "Esik.\n\n> Felveszem a telefont.",
                         "két egymás utáni saját rész egy üzenet")
        messages, _ = self.desk.build_messages(story, story["entries"][:1])
        self.assertEqual(messages[-1]["content"].split("]\n\n")[-1], PROMPTS["hu"]["continue"]["adventure"])
        messages, _ = self.desk.build_messages(story, [])
        self.assertTrue(messages[-1]["content"].endswith(PROMPTS["hu"]["opening"]["adventure"]))
        options = self.desk.sampling(story)
        self.assertEqual((options["temperature"], options["min_p"], options["stop"]), (0.8, 0.08, ["\n> "]))

    def test_character_prompt(self):
        story, _ = self.desk.create({"mode": "character", "language": "en", "template": "nyx",
                                     "persona": {"name": "Sam", "about": "A tired nurse."}})
        self.desk.add_turn(story["id"], "user", "Who are you?")
        story = self.desk.store.load(story["id"])
        messages, _ = self.desk.build_messages(story, story["entries"])
        system = messages[0]["content"]
        self.assertTrue(system.startswith("You are Nyx, a character in an ongoing roleplay chat with Sam."))
        self.assertIn("## About Sam (the person you are talking with)\nA tired nurse.", system)
        self.assertIn("## How Nyx talks (examples)\nSam: Are you real?", system)
        self.assertEqual(self.desk.sampling(story)["stop"][0], "\nSam:")

    def test_old_parts_are_dropped_when_the_context_is_small(self):
        self.llm.num_ctx = 2048
        story = self.new()
        sid = story["id"]
        for i in range(12):
            self.desk.add_turn(sid, "story", f"Part {i}. " + LONG)
        story = self.desk.store.load(sid)
        messages, info = self.desk.build_messages(story, story["entries"])
        self.assertGreater(info["dropped"], 0)
        text = "\n".join(m["content"] for m in messages)
        self.assertIn("Part 11.", text)
        self.assertNotIn("Part 0.", text)
        self.assertEqual(messages[1]["role"], "user")

    def test_memorized_parts_are_replaced_by_their_memory(self):
        story = self.new()
        sid = story["id"]
        self.desk.add_turn(sid, "do", "go to the hotel")

        def forget(s):
            s["entries"][0]["mem"] = True
            s["memories"].append({"id": "aaaaaaaa", "text": "A woman called about room 614.", "created": 0,
                                  "first": "", "last": "", "count": 1, "auto": True})
            s["summary"] = "Sam is a private eye."

        self.desk.store.edit(sid, forget)
        story = self.desk.store.load(sid)
        messages, _ = self.desk.build_messages(story, story["entries"])
        self.assertIn("## The story so far\nSam is a private eye.", messages[0]["content"])
        self.assertIn("## Earlier events (oldest first)\n- A woman called about room 614.", messages[0]["content"])
        self.assertNotIn("Blue Heron", "\n".join(m["content"] for m in messages[1:]), "a régi szöveget nem küldi")
        self.desk.memory(sid, "clear")
        story = self.desk.store.load(sid)
        messages, _ = self.desk.build_messages(story, story["entries"])
        self.assertNotIn("room 614", messages[0]["content"], "a törölt emléket elfelejti")


# --- írás ------------------------------------------------------------------------------------------------

class WritingTests(DeskCase):
    def setUp(self):
        super().setUp()
        self.pause = mock.patch.object(llm, "RESTART_PAUSE", 0.01)
        self.pause.start()

    def tearDown(self):
        self.pause.stop()
        super().tearDown()

    def test_write_streams_and_saves(self):
        story = self.new()
        sid = story["id"]
        self.desk.add_turn(sid, "do", "answer the phone")
        self.fake.chat_script = [reply("Narrator: The line crackles. A woman breathes.\n\nWhat do you do?")]
        self.run_task("write", sid)
        story = self.desk.get(sid)
        self.assertEqual(story["entries"][-1]["text"], "The line crackles. A woman breathes.")
        kinds = [e["type"] for e in self.events]
        self.assertLess(kinds.index("creative_begin"), kinds.index("creative_delta"))
        self.assertLess(kinds.index("creative_end"), len(kinds) - 1 - kinds[::-1].index("creative_story"),
                        "előbb ér véget az élő írás, utána jön a mentett történet")
        streamed = "".join(e["text"] for e in self.events if e["type"] == "creative_delta")
        self.assertIn("The line crackles.", streamed)
        body = self.bodies()[-1]
        self.assertFalse(body["think"])
        self.assertTrue(body["stream"])
        self.assertEqual(body["options"]["num_ctx"], self.llm.num_ctx, "ugyanaz a kontextus: nincs újratöltés")
        self.assertFalse(self.desk.busy(sid))

    def test_retry_keeps_the_versions(self):
        story = self.new()
        sid = story["id"]
        self.fake.chat_script = [reply("First version."), reply("Second version.")]
        self.run_task("write", sid)
        self.run_task("retry", sid)
        story = self.desk.get(sid)
        last = story["entries"][-1]
        self.assertEqual((last["alts"], last["alt"], last["text"]), (["First version.", "Second version."], 1,
                                                                     "Second version."))
        retry_body = self.bodies()[-1]
        self.assertNotIn("First version.", json.dumps(retry_body), "az újraírt választ nem látja")
        story = self.desk.entry(sid, last["id"], "alt", index=0)
        self.assertEqual(story["entries"][-1]["text"], "First version.")
        self.desk.add_turn(sid, "do", "wait")
        with self.assertRaisesRegex(StoryError, "nem az enyém"):
            self.desk.check_retry(sid)

    def test_stop_keeps_what_was_written(self):
        story = self.new()
        sid = story["id"]
        slow = reply("One full sentence here. And another one that never quite gets to")
        slow["chunk_delay"] = 0.08
        self.fake.chat_script = [slow]
        stop = threading.Event()
        threading.Timer(0.6, stop.set).start()
        self.run_task("write", sid, stop)
        last = self.desk.get(sid)["entries"][-1]
        self.assertEqual(last["text"], "One full sentence here.", "a félbemaradt mondat nélkül")
        end = [e for e in self.events if e["type"] == "creative_end"][-1]
        self.assertTrue(end["cancelled"])

    def test_opening_is_written_when_missing(self):
        story, need = self.desk.create({"mode": "character", "template": "custom", "language": "en",
                                        "character": {"name": "Bori", "about": "A grumpy dragon who runs a bookshop."},
                                        "persona": {"name": "Sam"}})
        self.assertEqual(need, "write")
        self.fake.chat_script = [reply("*looks up from a ledger* We're closed. Unless you're buying.")]
        self.run_task("write", story["id"])
        body = self.bodies()[-1]
        self.assertEqual(body["messages"][-1]["content"], PROMPTS["en"]["opening"]["character"]
                         .replace("{char}", "Bori").replace("{user}", "Sam"))
        self.assertEqual(self.desk.get(story["id"])["entries"][0]["kind"], "ai")

    def test_a_model_crash_is_retried_once(self):
        story = self.new()
        self.fake.chat_script = [crash(), reply("The phone rings again.")]
        self.run_task("write", story["id"])
        self.assertEqual(self.desk.get(story["id"])["entries"][-1]["text"], "The phone rings again.")

    def test_empty_answer_is_an_error(self):
        story = self.new()
        self.fake.chat_script = [reply("   ")]
        with self.assertRaisesRegex(StoryError, "üres"):
            self.run_task("write", story["id"])
        self.assertEqual(len(self.desk.get(story["id"])["entries"]), 1)
        self.assertFalse(self.desk.busy(story["id"]))


# --- emlékezet -------------------------------------------------------------------------------------------

class MemoryTests(DeskCase):
    def long_story(self, parts: int = 14) -> str:
        self.llm.num_ctx = 4096
        story = self.new()
        sid = story["id"]
        for i in range(parts):
            def add(s, i=i):
                s["entries"].append({"id": f"{0xa0000000 + i:08x}", "kind": "do" if i % 2 else "ai",
                                     "text": f"[{i}] " + LONG, "ts": 0, "mem": False, "alts": [f"[{i}] " + LONG],
                                     "alt": 0})
            self.desk.store.edit(sid, add)
        return sid

    def test_long_stories_are_summarized_into_memories(self):
        sid = self.long_story()
        story = self.desk.store.load(sid)
        self.assertTrue(self.desk.needs_summary(story))
        self.fake.chat_script = [reply("The rain kept falling."),
                                 reply("Summary: Sam walked the rainy streets and counted doors.")]
        self.run_task("write", sid)
        story = self.desk.get(sid)
        self.assertEqual([m["text"] for m in story["memories"]], ["Sam walked the rainy streets and counted doors."])
        remembered = [e for e in story["entries"] if e["mem"]]
        self.assertTrue(remembered)
        self.assertEqual(remembered[-1]["kind"], "ai", "a kör és a rá adott válasz együtt marad")
        verbatim = [e for e in story["entries"] if not e["mem"]]
        self.assertGreaterEqual(len(verbatim), KEEP_VERBATIM)
        self.assertLessEqual(story["usage"]["verbatim"], story["usage"]["limit"])
        summary_request = self.bodies()[-1]
        self.assertIn("long-term memory", summary_request["messages"][0]["content"])
        self.assertIn("[0] The rain", summary_request["messages"][1]["content"])
        self.assertEqual(summary_request["options"]["temperature"], 0.3)
        self.assertEqual(story["memories"][0]["count"], len(remembered))

    def test_memories_are_condensed_when_they_grow(self):
        sid = self.long_story(parts=8)
        def many(s):
            for i in range(6):
                s["memories"].append({"id": f"b{i:07d}", "text": f"Memory {i}. " + LONG[:300], "created": 0,
                                      "first": "", "last": "", "count": 2, "auto": True})
        self.desk.store.edit(sid, many)
        self.fake.chat_script = [reply("Everything so far, in short.")]
        self.desk._maybe_consolidate(sid, threading.Event(), lambda _t: None)
        story = self.desk.get(sid)
        self.assertEqual(story["summary"], "Everything so far, in short.")
        self.assertEqual([m["text"][:8] for m in story["memories"]], ["Memory 3", "Memory 4", "Memory 5"])
        self.assertIn("- Memory 0.", self.bodies()[-1]["messages"][1]["content"])

    def test_manual_memories(self):
        story = self.new()
        sid = story["id"]
        story = self.desk.memory(sid, "add", text="Sam owes Velasco money.")
        memory = story["memories"][0]
        self.assertFalse(memory["auto"])
        story = self.desk.memory(sid, "edit", memory["id"], "Sam owes the Velascos a lot.")
        self.assertEqual(story["memories"][0]["text"], "Sam owes the Velascos a lot.")
        story = self.desk.memory(sid, "delete", memory["id"])
        self.assertEqual(story["memories"], [])
        with self.assertRaisesRegex(StoryError, "rövid"):
            self.run_task("summarize", sid)


class IdeaTests(DeskCase):
    def test_a_story_from_an_idea(self):
        story, need = self.desk.create({"mode": "adventure", "language": "en", "template": "idea",
                                        "idea": "pirates on flying islands and a cursed map", "persona": {"name": "Kit"}})
        self.assertEqual(need, "idea")
        plan = {"title": "The Cursed Chart", "plot": "Sky pirates hunt a map that redraws itself.",
                "persona": "A young cartographer.", "style": "Swashbuckling, witty.",
                "cards": [{"name": "The Chart", "keys": ["chart", "map"], "text": "It moves at night."}]}
        self.fake.chat_script = [reply(json.dumps(plan)), reply("Wind howls through the rigging.")]
        self.run_task("idea", story["id"])
        story = self.desk.get(story["id"])
        self.assertEqual((story["title"], story["note"]), ("The Cursed Chart", "Swashbuckling, witty."))
        self.assertEqual(story["persona"]["about"], "A young cartographer.")
        self.assertEqual(story["cards"][0]["keys"], ["chart", "map"])
        self.assertEqual(story["entries"][0]["text"], "Wind howls through the rigging.")
        plan_request = self.bodies()[0]
        self.assertEqual(plan_request["format"]["required"][:2], ["title", "plot"])
        self.assertIn("cursed map", plan_request["messages"][1]["content"])

    def test_a_broken_plan_still_starts_the_story(self):
        story, _ = self.desk.create({"mode": "character", "language": "hu", "template": "idea",
                                     "idea": "egy morcos sárkány, aki könyvesboltot visz"})
        self.fake.chat_script = [reply("ez nem json"), reply("*felnéz* Zárva vagyunk.")]
        self.run_task("idea", story["id"])
        story = self.desk.get(story["id"])
        self.assertEqual(story["plot"], "egy morcos sárkány, aki könyvesboltot visz")
        self.assertEqual(story["entries"][0]["text"], "*felnéz* Zárva vagyunk.")


# --- a Jarvisban ---------------------------------------------------------------------------------------------

class AppTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.fake.vram_fit = lambda ctx: 1.0
        self.tmp = tempfile.TemporaryDirectory()
        cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        cfg.api.enabled = False
        self.bus = EventBus()
        self.ui = GuiUI(self.bus, "Jarvis")
        self.seen: list[dict] = []
        self.bus.add_listener(self.seen.append)
        self.app = JarvisApp(cfg, self.ui)
        self.assertTrue(self.app.start())
        self.app.start_agent()
        self.desk = self.app.creative
        self.pause = mock.patch.object(llm, "RESTART_PAUSE", 0.01)
        self.pause.start()

    def tearDown(self):
        self.pause.stop()
        self.app.shutdown()
        self.fake.close()
        self.tmp.cleanup()

    def story(self) -> str:
        story, _ = self.desk.create({"mode": "adventure", "template": "horror", "language": "en"})
        return story["id"]

    def test_creative_runs_on_the_agent_thread_and_reports_in_its_own_place(self):
        self.assertTrue(self.app.describe()["creative"])
        sid = self.story()
        self.fake.chat_script = [crash(CRASH_AFTER), crash(CRASH_AFTER)]
        self.app.submit_creative("write", sid)
        self.assertTrue(wait_for(lambda: not self.app.busy and not self.desk.busy(sid)))
        errors = [e for e in self.seen if e.get("type") == "error"]
        self.assertEqual([e["channel"] for e in errors], ["creative"], "a Kreatív menüben látszik, nem a Beszélgetésben")
        self.assertIn("futás közben leállt", errors[0]["text"])
        warns = [e for e in self.seen if e.get("type") == "warn" and "Újraindítom" in e.get("text", "")]
        self.assertEqual(warns[0]["channel"], "creative")
        states = [e["state"] for e in self.seen if e.get("type") == "state"]
        self.assertIn("writing", states)

    def test_paused_jarvis_answers_in_the_creative_menu(self):
        sid = self.story()
        self.app.paused = True
        self.app.submit_creative("write", sid)
        self.assertTrue(wait_for(lambda: not self.app.busy))
        notes = [e for e in self.seen if e.get("type") == "assistant" and e.get("text") == PAUSED_REPLY]
        self.assertEqual(notes[0]["channel"], "creative")
        self.assertFalse(self.desk.busy(sid), "a sorban álló feladat lezárult")
        self.app.paused = False

    def test_pause_drops_queued_creative_work(self):
        first, second = self.story(), self.story()
        slow = reply("word " * 80)
        slow["chunk_delay"] = 0.02
        self.fake.chat_script = [slow]
        self.app.submit_creative("write", first)
        self.assertTrue(wait_for(lambda: self.desk.status(first)["entry"]), "már ír")
        self.app.submit_creative("write", second)
        self.assertEqual(self.desk.status(second)["text"], "Sorra vár…")
        self.app._drop_queued()      # ezt csinálja a Szünet is
        self.assertFalse(self.desk.busy(second), "a sorban álló feladat lekerült")
        self.app.stop_current()
        self.assertTrue(wait_for(lambda: not self.app.busy and not self.desk.busy(first)))


def call(port: int, method: str, path: str, token: str | None = None, body: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Jarvis-Token"] = token
    conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
    response = conn.getresponse()
    data = response.read()
    result = (response.status, data, dict(response.getheaders()))
    conn.close()
    return result


class EndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeOllama()
        cls.fake.vram_fit = lambda ctx: 1.0
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = make_cfg(Path(cls.tmp.name), cls.fake.url)
        cfg.api.enabled = False
        cls.bus = EventBus()
        cls.ui = GuiUI(cls.bus, "Jarvis")
        cls.app = JarvisApp(cfg, cls.ui)
        assert cls.app.start()
        cls.app.start_agent()
        cls.server = GuiServer(cls.app, cls.ui, cls.bus)
        cls.server.start()
        cls.deltas: list[dict] = []
        cls.bus.add_listener(lambda e: cls.deltas.append(e) if e.get("type") == "creative_delta" else None)

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown()
        cls.server.stop()
        cls.fake.close()
        cls.tmp.cleanup()

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        status, data, _ = call(self.server.port, "POST", path, self.server.token, body)
        return status, json.loads(data or b"{}")

    def get(self, path: str) -> tuple[int, bytes, dict]:
        return call(self.server.port, "GET", path + ("&" if "?" in path else "?") + "t=" + self.server.token)

    def test_a_whole_round_over_http(self):
        port = self.server.port
        self.assertEqual(call(port, "GET", "/api/creative/stories")[0], 403, "kulcs nélkül nem")
        status, data, _ = self.get("/api/creative/stories")
        listing = json.loads(data)
        self.assertEqual(listing["language"], "en")
        self.assertEqual({t["key"] for t in listing["templates"]["character"]}, {"mara", "thorn", "nyx"})
        status, result = self.post("/api/creative/new", {"mode": "adventure", "template": "scifi",
                                                         "persona": {"name": "Kit"}})
        self.assertEqual(status, 200)
        sid = result["story"]["id"]
        self.fake.chat_script = [reply("HOLLIS hums nervously.")]
        status, result = self.post("/api/creative/act", {"id": sid, "action": "send", "kind": "do",
                                                         "text": "dock with the Meridian"})
        self.assertEqual(status, 202)
        self.assertIn("> You dock with the Meridian.", [e["text"] for e in result["story"]["entries"]])
        self.assertTrue(wait_for(lambda: not self.app.creative.busy(sid)))
        story = json.loads(self.get(f"/api/creative/story?id={sid}")[1])["story"]
        self.assertEqual(story["entries"][-1]["text"], "HOLLIS hums nervously.")
        status, result = self.post("/api/creative/update", {"id": sid, "fields": {
            "plot": "Changed.", "persona": {"about": "Brave."}, "settings": {"creativity": 9, "length": "long"},
            "language": "hu"}})
        story = result["story"]
        self.assertEqual((story["plot"], story["persona"]["about"], story["settings"], story["language"]),
                         ("Changed.", "Brave.", {"creativity": 1.5, "length": "long"}, "hu"))
        status, result = self.post("/api/creative/card", {"id": sid, "op": "add", "card": {"name": "Dock", "keys": "dock, port"}})
        card = result["story"]["cards"][-1]
        self.assertEqual(card["keys"], ["dock", "port"])
        status, result = self.post("/api/creative/card", {"id": sid, "op": "edit", "card": {"id": card["id"], "always": True}})
        self.assertTrue(result["story"]["cards"][-1]["always"])
        status, result = self.post("/api/creative/memory", {"id": sid, "op": "add", "text": "Kit fears the dark."})
        self.assertEqual(result["story"]["memories"][0]["text"], "Kit fears the dark.")
        status, result = self.post("/api/creative/act", {"id": sid, "action": "undo"})
        self.assertEqual(result["story"]["redo"], 1)
        status, body, headers = self.get(f"/api/creative/export?id={sid}")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertTrue(body.decode("utf-8").startswith("# Salvage Rights"))
        status, result = self.post("/api/creative/act", {"id": sid, "action": "retry"})
        self.assertEqual(status, 400, "a visszavonás után az utolsó rész a tiéd")
        self.assertIn("nem az enyém", result["error"])
        self.assertEqual(self.post("/api/creative/delete", {"id": sid})[0], 200)
        self.assertEqual(json.loads(self.get(f"/api/creative/story?id={sid}")[1])["ok"], False)

    def test_busy_story_is_protected(self):
        status, result = self.post("/api/creative/new", {"mode": "character", "template": "thorn"})
        sid = result["story"]["id"]
        slow = reply("Ah, " + "words " * 60)
        slow["chunk_delay"] = 0.02
        self.fake.chat_script = [slow]
        self.assertEqual(self.post("/api/creative/act", {"id": sid, "action": "continue"})[0], 202)
        self.assertTrue(wait_for(lambda: any(e["id"] == sid for e in self.deltas)), "már ír")
        status, result = self.post("/api/creative/act", {"id": sid, "action": "undo"})
        self.assertEqual(status, 409)
        self.assertIn("Épp ezen", result["error"])
        self.assertEqual(self.post("/api/creative/act", {"id": sid, "action": "stop"})[0], 200)
        self.assertTrue(wait_for(lambda: not self.app.creative.busy(sid)))
        last = self.app.creative.get(sid)["entries"][-1]
        self.assertTrue(last["text"].startswith("Ah,"), "a megállításig írt rész megmaradt")


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class CreativeBrowserTests(unittest.TestCase):
    """Végigjátszás a felületen: új kaland, egy kör, másik változat, visszavonás, emlékezet."""

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
        cfg.api.enabled = False
        cfg.browser.enabled = False
        self.bus = EventBus()
        self.ui = GuiUI(self.bus, "Jarvis")
        self.app = JarvisApp(cfg, self.ui)
        self.assertTrue(self.app.start())
        self.app.start_agent()
        self.server = GuiServer(self.app, self.ui, self.bus)
        self.server.start()
        self.bus.set_hello(self.app.describe())
        self.page = self.browser.new_page(viewport={"width": 1100, "height": 900})
        self.errors: list[str] = []
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.goto(self.server.url)
        self.page.wait_for_selector("#tab-creative:not([hidden])")

    def tearDown(self):
        self.page.close()
        self.app.shutdown()
        self.server.stop()
        self.fake.close()
        self.tmp.cleanup()

    def test_play_a_round(self):
        page = self.page
        page.click("#tab-creative")
        page.wait_for_selector(".c-empty")
        page.click("#c-new")
        page.click('[data-tpl="fantasy"]')
        page.check('input[name="c-choice"][value="ranger"]')
        page.fill("#cf-name", "Ilka")
        page.check('input[name="c-newlang"][value="hu"]')
        page.click('#c-create button[type="submit"]')
        page.wait_for_selector("#c-reader:not([hidden]) .c-entry.c-ai")
        self.assertIn("Ilka vagy, erdőjáró", page.inner_text("#c-story"))
        self.assertEqual(page.get_attribute("#c-text", "placeholder"), "Mit teszel?")

        self.fake.chat_script = [reply("A nyomok a fák közé vezetnek."), reply("A köd megmozdul.")]
        page.fill("#c-text", "követem a nyomokat")
        page.press("#c-text", "Enter")
        page.wait_for_selector("text=A nyomok a fák közé vezetnek.")
        self.assertIn("Követem a nyomokat.", page.inner_text(".c-entry.c-you"))
        page.wait_for_selector("#c-retry:not([disabled])")
        page.click("#c-retry")
        page.wait_for_selector("text=A köd megmozdul.")
        page.wait_for_selector(".c-alts")
        self.assertIn("2/2", page.inner_text(".c-alts"))
        page.hover(".c-entry.c-ai >> nth=-1")
        page.click('.c-alts [aria-label="Előző változat"]')
        page.wait_for_selector("text=A nyomok a fák közé vezetnek.")

        page.click("#c-memtoggle")
        page.click("#c-memory summary:has-text('Szerzői megjegyzés')")
        page.fill('#c-memory [data-field="note"]', "Rövid, feszült mondatok.")
        page.locator('#c-memory [data-field="note"]').blur()
        sid = page.evaluate("() => localStorage.getItem('jarvis-creative-story')")
        self.assertTrue(wait_for(lambda: self.app.creative.store.load(sid)["note"] == "Rövid, feszült mondatok."))

        page.click("#c-undo")
        page.wait_for_function("() => document.querySelectorAll('#c-story .c-entry').length === 2")
        self.assertFalse(page.is_disabled("#c-redo"))
        self.assertEqual(self.errors, [])

    def test_character_editing_and_memory_panel(self):
        page = self.page
        page.click("#tab-creative")
        page.click("#c-new")
        page.click('[data-cmode="character"]')
        page.click('[data-tpl="custom"]')
        page.fill("#cf-char", "Bori")
        page.fill("#cf-charabout", "A grumpy dragon who runs a bookshop.")
        page.fill("#cf-greeting", "*looks up* We're closed.")
        page.fill("#cf-name", "Sam")
        page.click('#c-create button[type="submit"]')
        page.wait_for_selector("#c-reader:not([hidden]) .c-entry.c-ai")
        self.assertEqual(page.inner_text(".c-entry.c-ai .c-name"), "BORI")
        self.assertEqual(page.inner_text(".c-entry.c-ai .c-text em"), "looks up")
        self.assertTrue(page.is_hidden("#c-kinds"), "karakternél nincs Tett/Mondás/Történet")
        sid = page.evaluate("() => localStorage.getItem('jarvis-creative-story')")
        desk = self.app.creative

        page.hover(".c-entry.c-ai")
        page.click('.c-entry.c-ai [data-act="edit"]')
        page.fill("textarea.c-edit", "*sighs* Fine. Come in.")
        page.click('[data-act="edit-save"]')
        self.assertTrue(wait_for(lambda: desk.store.load(sid)["entries"][0]["text"] == "*sighs* Fine. Come in."))

        page.click("#c-memtoggle")
        page.click("#c-memory summary:has-text('Világkártyák')")
        page.click('[data-act="card-add"]')
        page.wait_for_selector(".c-card")
        page.fill('.c-card [data-cfield="name"]', "The bookshop")
        page.locator('.c-card [data-cfield="name"]').blur()
        page.fill('.c-card [data-cfield="keys"]', "shop, books")
        page.locator('.c-card [data-cfield="keys"]').blur()
        self.assertTrue(wait_for(lambda: desk.store.load(sid)["cards"][0]["keys"] == ["shop", "books"]))
        self.assertEqual(desk.store.load(sid)["cards"][0]["name"], "The bookshop")
        page.click('[data-act="card-delete"]')
        page.click("#ask-yes")
        self.assertTrue(wait_for(lambda: desk.store.load(sid)["cards"] == []))

        page.click('[data-act="memory-add"]')
        page.fill("#cm-newmem", "Sam promised to return the atlas.")
        page.click('[data-act="memory-save"]')
        page.wait_for_selector(".c-memo textarea")
        self.assertEqual(desk.store.load(sid)["memories"][0]["text"], "Sam promised to return the atlas.")
        page.click('[data-act="memory-delete"]')
        page.click("#ask-yes")
        self.assertTrue(wait_for(lambda: desk.store.load(sid)["memories"] == []))

        page.click("#c-memory summary:has-text('Beállítások')")
        page.check('input[name="cm-lang"][value="hu"]')
        page.wait_for_function("() => document.getElementById('c-text').placeholder === 'Üzenet Bori számára…'")
        self.assertEqual(desk.store.load(sid)["language"], "hu")

        page.click('[data-act="delete"]')
        page.click("#ask-yes")
        page.wait_for_selector(".c-empty")
        self.assertEqual(desk.stories(), [])
        self.assertEqual(self.errors, [])

    def test_a_story_from_an_idea(self):
        page = self.page
        plan = {"title": "Sky Thieves", "plot": "Pirates steal clouds.", "persona": "A deckhand.",
                "style": "Breezy.", "cards": [{"name": "The Gull", "keys": ["gull"], "text": "A flying ship."}]}
        self.fake.chat_script = [reply(json.dumps(plan)), reply("The wind tastes of rain as the Gull lifts off.")]
        page.click("#tab-creative")
        page.click("#c-new")
        page.click('[data-tpl="idea"]')
        page.fill("#cf-idea", "pirates who steal clouds")
        page.click('#c-create button[type="submit"]')
        page.wait_for_selector("text=The wind tastes of rain as the Gull lifts off.")
        page.wait_for_function("() => document.getElementById('c-title').textContent === 'Sky Thieves'")
        self.assertFalse(page.is_disabled("#c-retry"))
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
