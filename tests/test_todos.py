"""A Teendőim (todos.py) tesztjei: felvétel, kész és mégsem kész, törlés, a címke képe, a sorrend, a mentés, és az
esti emlékeztető (naponta egyszer, a megadott időpont után, és csak ha van elvégezetlen).

Futtatás:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis_core.todos import TodoError, TodoList, parse_time  # noqa: E402


def local(hour: int, minute: int = 0, day: int = 26) -> float:
    """Egy helyi időpont 2026 októberében (a gép időzónája szerint, ahogy az emlékeztető is számol)."""
    return time.mktime((2026, 10, day, hour, minute, 0, 0, 0, -1))


class Clock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


class TodoListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock(local(10))
        self.changes: list[int] = []
        self.todos = TodoList(Path(self.tmp.name), on_change=self.changes.append, clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_done_remove_and_order(self):
        """Elöl az elvégezetlenek (a legrégebbi elöl: azt kell előbb feladni), utána az elvégzettek; a felület minden
        változásról tud (hány elvégezetlen van)."""
        first = self.todos.add("ship", "Csomagfeladás: Zara blézer", item_name="Zara blézer", code="8XK2 4471",
                               place="Foxpost automata", conversation="c1", partner="  zoli_v  ")
        self.clock.now += 60
        second = self.todos.add("ship", "Csomagfeladás: Nike cipő", item_name="Nike cipő", conversation="c2")
        self.assertEqual([t["id"] for t in self.todos.all()], [first["id"], second["id"]])
        self.assertEqual((first["partner"], first["done"], first["shot"]), ("zoli_v", False, ""))
        self.assertEqual(self.todos.find(conversation="c2")["id"], second["id"])
        self.assertIsNone(self.todos.find(conversation="nincs"))
        self.todos.set_done(first["id"])
        self.assertEqual([t["id"] for t in self.todos.all()], [second["id"], first["id"]], "a kész a végére kerül")
        self.assertEqual([t["id"] for t in self.todos.waiting()], [second["id"]])
        self.todos.set_done(first["id"], False)
        self.assertEqual(len(self.todos.waiting()), 2, "mégsem kész")
        self.todos.update(second["id"], code="1234 5678", text="Feladási kód\n1234 5678")
        self.assertEqual(self.todos.find(conversation="c2")["code"], "1234 5678")
        self.todos.remove(first["id"])
        self.assertEqual([t["id"] for t in self.todos.all()], [second["id"]])
        self.assertEqual(self.changes, [1, 2, 1, 2, 2, 1])
        with self.assertRaises(TodoError):
            self.todos.set_done("0123456789")
        with self.assertRaises(TodoError):
            self.todos.add("valami", "Ismeretlen fajta")

    def test_the_label_picture_and_saving(self):
        todo = self.todos.add("ship", "Csomagfeladás: Zara blézer", code="8XK2 4471")
        with self.assertRaises(TodoError):
            self.todos.shot_file(todo["id"])
        self.todos.save_shot(todo["id"], b"\xff\xd8kep")
        self.assertEqual(self.todos.shot_file(todo["id"]).read_bytes(), b"\xff\xd8kep")
        for bad in ("../teendok", "", "nincs-ilyen"):
            with self.assertRaises(TodoError):
                self.todos.shot_file(bad)
        again = TodoList(Path(self.tmp.name))
        self.assertEqual([(t["id"], t["code"], t["shot"]) for t in again.all()], [(todo["id"], "8XK2 4471",
                                                                                     f"{todo['id']}.jpg")])
        self.todos.remove(todo["id"])
        self.assertFalse((Path(self.tmp.name) / "teendok" / f"{todo['id']}.jpg").exists(), "a képe is törlődik")

    def test_the_evening_reminder_once_a_day(self):
        """Este (alapból 19:00) egyszer szól, ha van elvégezetlen; ha üres az időpont, soha."""
        self.assertEqual((parse_time("19:00"), parse_time("7.30"), parse_time(""), parse_time("25:00")),
                         (1140, 450, None, None))
        self.assertFalse(self.todos.reminder_due("19:00", local(19, 5)), "nincs teendő")
        todo = self.todos.add("ship", "Csomagfeladás: Zara blézer", item_name="Zara blézer", code="8XK2 4471",
                              place="Foxpost automata, Debrecen")
        self.todos.add("ship", "Csomagfeladás: Nike cipő", item_name="Nike cipő")
        self.assertFalse(self.todos.reminder_due("19:00", local(18, 59)), "még nincs este")
        self.assertTrue(self.todos.reminder_due("19:00", local(19, 0)))
        self.assertFalse(self.todos.reminder_due("", local(21)), "kikapcsolva")
        self.assertEqual(self.todos.reminder_text(),
                         "Még 2 teendőd van: csomagfeladás – Zara blézer (kód: 8XK2 4471, Foxpost automata, Debrecen); "
                         "Nike cipő. Ha megvan, az Elemzés fül Teendőim részén jelöld késznek.")
        self.todos.reminded(local(19, 2))
        self.assertFalse(self.todos.reminder_due("19:00", local(22)), "ma már szólt")
        self.assertFalse(TodoList(Path(self.tmp.name)).reminder_due("19:00", local(22)), "újraindítás után sem")
        self.assertTrue(self.todos.reminder_due("19:00", local(19, 30, day=27)), "másnap este újra")
        for item in self.todos.waiting():
            self.todos.set_done(item["id"])
        self.assertFalse(self.todos.reminder_due("19:00", local(19, 30, day=27)), "minden kész: nem szól")
        self.todos.set_done(todo["id"], False)
        self.assertIn("Még egy teendőd van", self.todos.reminder_text())


if __name__ == "__main__":
    unittest.main()
