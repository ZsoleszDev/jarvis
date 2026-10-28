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

from jarvis_core import web  # noqa: E402
from jarvis_core.assistant import KEEP_ABOVE, STALL_NOTE, STALL_REPLY, Assistant, page_orientation  # noqa: E402
from jarvis_core.browser import format_snapshot  # noqa: E402
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


def page_view(n: int, note: str = "", url: str = "", elements: int = 45) -> str:
    """Egy valódi alakú oldalnézet (browser.format_snapshot), kb. 4–5 ezer karakter, mint egy hirdetési űrlap."""
    items = [{"id": i, "kind": "link", "label": f"Termék {n}/{i} – fekete póló, M-es méret",
              "href": f"/termek/{n}-{i}?ref=lista&sid=abc{i}"} for i in range(1, elements + 1)]
    data = {"title": f"Lista {n}", "url": url or f"https://www.bolt.hu/lista/{n}?oldal={n}",
            "scrollHeight": 3000, "viewportHeight": 800, "scrollY": 0,
            "text": f"Találatok a(z) {n}. oldalon. " * 20, "elements": items}
    return format_snapshot(data, note)


def first_difference(earlier: list, later: list) -> int:
    for i, (a, b) in enumerate(zip(earlier, later)):
        if a != b:
            return i
    return min(len(earlier), len(later))


class CacheFriendlyCompactionTests(unittest.TestCase):
    """Az oldalnézetek tömörítése úgy, hogy az Ollama prompt-gyorsítótára megmaradjon: minden kérés csak az
    előző kérés utolsó üzenetétől tér el, és mindig csak egy teljes oldalnézet van benne."""

    def setUp(self):
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        self.cfg.assistant.max_tool_rounds = 30
        self.llm = OllamaClient(self.cfg.llm)
        self.box = Toolbox(self.cfg)
        self.clicks = 0

        def click(element: int) -> str:
            self.clicks += 1
            return page_view(self.clicks, note=f'Rákattintottam: "Tovább {self.clicks}".')

        self.box.register("browser_click", "teszt", params({"element": {"type": "integer"}}, ["element"]), click,
                          ephemeral=True)

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def test_twenty_step_browsing_only_rewrites_the_message_before_the_new_view(self):
        assistant = Assistant(self.cfg, self.llm, self.box, "Rendszerprompt. " * 200)
        assistant.context_tokens = 16384
        self.fake.chat_script = [tool_reply("browser_click", {"element": 3}) for _ in range(20)]
        self.fake.chat_script.append(reply("Megvan a 20. oldal."))
        self.assertEqual(assistant.ask("Lapozz el a 20. oldalig"), "Megvan a 20. oldal.")
        bodies = self.fake.chat_bodies()
        self.assertEqual(len(bodies), 21)
        for earlier, later in zip(bodies, bodies[1:]):
            before, after = earlier["messages"], later["messages"]
            self.assertGreaterEqual(first_difference(before, after), len(before) - 1,
                                    "csak az előző kérés utolsó üzenete (a régi oldalnézet) változhat")
            views = [m for m in after if m["role"] == "tool" and "Kezelhető elemek" in m["content"]]
            self.assertEqual(len(views), 1, "mindig csak a legfrissebb oldalnézet teljes")
            self.assertEqual(earlier["tools"], later["tools"])
        # A tömörített nézet megtartja, mi történt és hol voltunk.
        tools = [m["content"] for m in assistant.history if m["role"] == "tool"]
        self.assertEqual(tools[2], '[Korábbi oldalnézet: Rákattintottam: "Tovább 3". Oldal: Lista 3 – '
                                   'bolt.hu/lista/3]')
        self.assertIn("Kezelhető elemek", tools[-1])
        # A kérés mérete nem nő oldalnézetenként: a végén is csak egy nézetnyi, plusz a rövid sorok.
        sizes = [len(json.dumps(body["messages"], ensure_ascii=False)) for body in bodies]
        self.assertLess(sizes[-1], sizes[1] + 20 * 350)

    def test_reads_and_errors_stay_until_the_context_fills(self):
        self.box.register("browser_read", "teszt", params(),
                          lambda: web.format_page("Cikk", "https://bolt.hu/cikk", "Hosszú cikk. " * 100, 6000),
                          ephemeral=True)
        self.box.register("browser_back", "teszt", params(), lambda: "HIBA: nem tudok visszalépni.", ephemeral=True)
        assistant = Assistant(self.cfg, self.llm, self.box, "Rendszerprompt.")
        assistant.context_tokens = 16384
        self.fake.chat_script = [tool_reply("browser_click", {"element": 1}), tool_reply("browser_read", {}),
                                 tool_reply("browser_back", {}), tool_reply("browser_click", {"element": 2}),
                                 reply("kész")]
        assistant.ask("olvasd el")
        tools = [m["content"] for m in assistant.history if m["role"] == "tool"]
        self.assertTrue(tools[0].startswith("[Korábbi oldalnézet: "), tools[0][:60])
        self.assertIn("Hosszú cikk.", tools[1], "az olvasmányból gyűjt a modell: helyhiányig marad")
        self.assertEqual(tools[2], "HIBA: nem tudok visszalépni.")
        self.assertIn("Kezelhető elemek", tools[3])
        # a sikertelen lépés után is látszik a legutóbbi oldal
        body = self.fake.chat_bodies()[3]
        self.assertIn("Kezelhető elemek", body["messages"][3]["content"])

    def test_listing_data_above_the_view_survives(self):
        view = page_view(7, note="Megnyitottam a hirdetésfeladást.", url="https://www.vinted.hu/items/new?x=1")
        assistant = Assistant(self.cfg, self.llm, self.box, "rendszer")
        assistant.history = [
            {"role": "user", "content": "töltsd fel"},
            {"role": "tool", "tool_name": "browser_click", "content": "Cím: Kék pulóver" + KEEP_ABOVE + view},
            {"role": "tool", "tool_name": "browser_click", "content": page_view(8)},
        ]
        assistant._fit_context()
        self.assertEqual(assistant.history[1]["content"],
                         "Cím: Kék pulóver\n[Korábbi oldalnézet: Megnyitottam a hirdetésfeladást. Oldal: Lista 7 – "
                         "vinted.hu/items/new]")
        self.assertEqual(assistant.history[2]["content"], page_view(8))
        assistant._fit_context()          # a már tömörítetthez többé nem nyúl
        self.assertTrue(assistant.history[1]["content"].endswith("vinted.hu/items/new]"))

    def test_the_view_in_the_first_message_of_a_task_is_compacted_too(self):
        # Egy munka első üzenetében (pl. a Jarvis már megnyitotta a hirdetésfeladást) is oldalnézet van: az első
        # lépés után az is egy tájékozódó sor lesz, a kérés és a hirdetés adatai maradnak.
        assistant = Assistant(self.cfg, self.llm, self.box, "Rendszerprompt. " * 200)
        assistant.context_tokens = 16384
        first = page_view(0, note="Megnyitottam a hirdetésfeladást.", url="https://www.vinted.hu/items/new")
        task = "Töltsd fel a pulóvert.\n\nA hirdetés adatai:\nCím: Kék pulóver"
        self.fake.chat_script = [tool_reply("browser_click", {"element": 3}) for _ in range(3)]
        self.fake.chat_script.append(reply("Kész."))
        self.assertEqual(assistant.ask(task + KEEP_ABOVE + first), "Kész.")
        bodies = self.fake.chat_bodies()
        self.assertEqual(bodies[0]["messages"][1]["content"], task + KEEP_ABOVE + first, "az első körben még teljes")
        for earlier, later in zip(bodies, bodies[1:]):
            before, after = earlier["messages"], later["messages"]
            self.assertGreaterEqual(first_difference(before, after), len(before) - 1,
                                    "csak az előző kérés utolsó üzenete (a régi oldalnézet) változhat")
            views = [m for m in after if m["role"] in ("user", "tool") and "Kezelhető elemek" in m["content"]]
            self.assertEqual(len(views), 1, "mindig csak a legfrissebb oldalnézet teljes")
        self.assertEqual(assistant.history[0]["content"],
                         task + "\n[Korábbi oldalnézet: Megnyitottam a hirdetésfeladást. Oldal: Lista 0 – "
                         "vinted.hu/items/new]")
        # a felhasználó saját üzenetéhez (KEEP_ABOVE nélkül) soha nem nyúl, akkor sem, ha oldalnézetnek látszik
        assistant.history = [{"role": "user", "content": page_view(5)},
                             {"role": "tool", "tool_name": "browser_click", "content": page_view(6)}]
        assistant._fit_context()
        self.assertEqual(assistant.history[0]["content"], page_view(5))

    def test_orientation_line(self):
        self.assertEqual(page_orientation(page_view(2)), "[Korábbi oldalnézet: Oldal: Lista 2 – bolt.hu/lista/2]")
        self.assertTrue(page_orientation("Találatok erre: póló").startswith("[Korábbi, már elavult"))


class StallTests(unittest.TestCase):
    """Elakadás: ugyanaz a lépés, és az oldal nem változik."""

    def setUp(self):
        self.fake = FakeOllama()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        self.cfg.assistant.max_tool_rounds = 20
        self.llm = OllamaClient(self.cfg.llm)
        self.box = Toolbox(self.cfg)
        self.scrolled = 0

        def scroll(direction: str = "down") -> str:
            self.scrolled = min(self.scrolled + 1, 3)       # a 3. görgetés után az oldal alján vagyunk
            return page_view(self.scrolled)

        self.box.register("browser_click", "teszt", params({"element": {"type": "integer"}}, ["element"]),
                          lambda element: page_view(0, note=f"Rákattintottam: [{element}]."), ephemeral=True)
        self.box.register("browser_scroll", "teszt", params({"direction": {"type": "string"}}), scroll,
                          ephemeral=True)

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def test_same_click_on_an_unchanged_page_gets_a_note_then_the_run_ends(self):
        assistant = Assistant(self.cfg, self.llm, self.box, "rendszer")
        self.fake.chat_script = [tool_reply("browser_click", {"element": 5}) for _ in range(10)]
        answer = assistant.ask("kattints a Tovább gombra")
        self.assertEqual(answer, STALL_REPLY.format(name="browser_click"))
        self.assertEqual(len(self.fake.chat_bodies()), 5, "3 hiábavaló ismétlés után abbahagyja, nem égeti el a 20-at")
        # az egyes kattintások eredménye (amit a modell a következő körben kapott)
        results = [body["messages"][-1]["content"] for body in self.fake.chat_bodies()[1:]]
        results.append(next(m["content"] for m in reversed(assistant.history) if m["role"] == "tool"))
        self.assertFalse(results[0].endswith(STALL_NOTE))
        self.assertFalse(results[1].endswith(STALL_NOTE), "az első üres lépés lehet még jogos (pl. lassú oldal)")
        self.assertTrue(all(r.endswith(STALL_NOTE) for r in results[2:]), "az ismétlésnél jelzést kap")
        self.assertEqual(assistant.history[-1]["content"], answer)

    def test_the_new_element_marks_do_not_hide_a_stall(self):
        # A változó lépés után az új elemeken csillag van („*[4]”, és a magyarázata), a következő, hatástalan lépés
        # után már nincs: az oldal ettől még ugyanaz, az elakadás innen számol (nem egy körrel később).
        def view(new: bool) -> str:
            items = [{"id": i, "kind": "button", "label": f"Gomb {i}", "new": new and i > 3} for i in range(1, 8)]
            data = {"title": "Űrlap", "url": "https://bolt.hu/urlap", "text": "Kitöltés", "elements": items}
            return format_snapshot(data, 'Rákattintottam: "Tovább".')

        self.assertIn("*[4]", view(True))
        views = iter([view(True)] + [view(False)] * 10)
        self.box.register("browser_click", "teszt", params({"element": {"type": "integer"}}, ["element"]),
                          lambda element: next(views), ephemeral=True)
        assistant = Assistant(self.cfg, self.llm, self.box, "rendszer")
        self.fake.chat_script = [tool_reply("browser_click", {"element": 5}) for _ in range(10)]
        self.assertEqual(assistant.ask("kattints a Tovább gombra"), STALL_REPLY.format(name="browser_click"))
        self.assertEqual(len(self.fake.chat_bodies()), 5, "ugyanannyi kör, mint csillag nélkül")
        results = [body["messages"][-1]["content"] for body in self.fake.chat_bodies()[1:]]
        self.assertFalse(results[1].endswith(STALL_NOTE), "az első üres lépés lehet még jogos")
        self.assertTrue(results[2].endswith(STALL_NOTE), "a második ismétlésnél már jelzést kap")

    def test_the_same_failing_step_counts_too(self):
        # Ugyanaz a sikertelen lépés (HIBA), közben az oldal nem változik: ez is elakadás.
        self.box.register("browser_type", "teszt", params({"element": {"type": "integer"}, "text": {"type": "string"}},
                                                         ["element", "text"]),
                          lambda element, text: "HIBA: ez nem beírható mező.", ephemeral=True)
        assistant = Assistant(self.cfg, self.llm, self.box, "rendszer")
        # Az eszközleírások már kb. 3400 tokent foglalnak: 8192-es kontextusban a 4. körre átlépné a 60%-ot, és a
        # (szándékos) teljes tömörítés a hibaüzenetet is elavultnak jelölné. Itt az elakadás-jelzés a téma.
        assistant.context_tokens = 16384
        self.fake.chat_script = [tool_reply("browser_click", {"element": 1})]
        self.fake.chat_script += [tool_reply("browser_type", {"element": 4, "text": "póló"}) for _ in range(10)]
        self.assertEqual(assistant.ask("írd be"), STALL_REPLY.format(name="browser_type"))
        self.assertEqual(len(self.fake.chat_bodies()), 5)
        errors = [m["content"] for m in assistant.history if m["role"] == "tool"][1:]
        self.assertEqual(errors[0], "HIBA: ez nem beírható mező.")
        self.assertTrue(all(e.endswith(STALL_NOTE) for e in errors[1:]))

    def test_scrolling_that_changes_the_page_is_not_a_stall(self):
        assistant = Assistant(self.cfg, self.llm, self.box, "rendszer")
        self.fake.chat_script = [tool_reply("browser_scroll", {"direction": "down"}) for _ in range(5)]
        self.fake.chat_script.append(reply("Az oldal alján vagyunk."))
        self.assertEqual(assistant.ask("görgess le"), "Az oldal alján vagyunk.")
        results = [body["messages"][-1]["content"] for body in self.fake.chat_bodies()[1:]]
        self.assertEqual(len(results), 5)
        self.assertFalse(any(r.endswith(STALL_NOTE) for r in results[:4]), "amíg változik az oldal, nincs jelzés")
        self.assertTrue(results[4].endswith(STALL_NOTE), "az oldal alján a további görgetés már nem változtat")


if __name__ == "__main__":
    unittest.main(verbosity=2)
