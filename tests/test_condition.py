"""Az állapotfelismerés (condition.py) tesztjei: szempontonként (kijelző, akkumulátor, működés, tartozékok, garancia)
bizonyítékkal, az ismeretlen külön érték; a modell idézetei ellenőrizve; a két forrás összevetése; és hogy mi számít az
árban (a doboz hiánya nem), és mikor mérhető a jelölt a hasonló hibájúakhoz.

Futtatás:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis_core import condition as conditions  # noqa: E402


class FromTextTests(unittest.TestCase):
    def test_display_defect_and_missing_charger_with_evidence(self):
        cond = conditions.from_text("Nintendo Switch Lite. A kijelzője repedt, töltő nélkül adom. Egyébként hibátlanul "
                                    "működik, az akkumulátor 92%.")
        self.assertEqual(cond["display"], {"state": "hibás", "evidence": "kijelzője repedt"})
        self.assertEqual((cond["accessories"]["state"], cond["missing"]), ("hiányos", ["töltő"]))
        self.assertEqual(cond["accessories"]["evidence"], "töltő nélkül")
        self.assertEqual(cond["function"]["state"], "működik")
        self.assertEqual(cond["battery"]["state"], "jó", "92% fölött jó")
        self.assertEqual(cond["warranty"], {"state": conditions.UNKNOWN, "evidence": ""}, "amiről nem ír: ismeretlen")
        self.assertEqual(conditions.issues(cond), ["kijelző", "töltő"])

    def test_unknown_is_not_ok(self):
        cond = conditions.from_text("Nintendo Switch Lite, szürke.")
        self.assertTrue(all(cond[a]["state"] == conditions.UNKNOWN for a in conditions.ASPECTS))
        self.assertEqual(conditions.issues(cond), [])
        self.assertEqual(conditions.describe(cond), "", "csak az ismertek")
        self.assertIn("garancia: ismeretlen", conditions.describe(cond, known_only=False))
        self.assertEqual(conditions.clean_evidence(cond), "", "az állapotáról nem ír: nem bizonyítottan hibátlan")
        self.assertEqual(conditions.clean_evidence(conditions.from_text("Eladó RTX 3060, dobozában.")), "")
        self.assertEqual(conditions.clean_evidence(conditions.from_text("Hibátlanul működik, dobozában.")),
                         "Hibátlanul működik")

    def test_spots_pressure_marks_and_burn_in_on_the_display(self):
        """A kijelző foltja (világosabb folt), nyomáshibája, beégése is hiba; a nyomáshiba és a beégés magában is a
        kijelzőé. A kijelzővédő fólia hibája nem a kijelzőé."""
        for text, evidence in (("Világosabb folt a kijelzőn, egyébként működik.", "Világosabb folt a kijelzőn"),
                               ("Nyomáshiba a kijelző alján, de működik.", "Nyomáshiba a kijelző"),
                               ("Pár nyomáshiba látszik rajta.", "nyomáshiba"),
                               ("Beégés van a kijelzőn.", "Beégés van a kijelzőn"),
                               ("A kijelzőn egy sárga folt van.", "kijelzőn egy sárga folt")):
            cond = conditions.from_text(text)
            self.assertEqual((cond["display"]["state"], cond["display"]["evidence"]), ("hibás", evidence), text)
            self.assertEqual(conditions.issues(cond), ["kijelző"], text)
        self.assertEqual(conditions.from_text("A kijelzővédő fólia hibás, a kijelző ép.")["display"]["state"], "ép")

    def test_a_denied_defect_is_no_defect(self):
        """A tagadott hiba nem hiba („nincs folt a kijelzőn”, „a kijelzőn nincs folt”, „nem repedt”, „se karc, se folt”,
        „nyomáshiba nincs”), hanem a jó állapot bizonyítéka; a „de” után jövő viszont új állítás."""
        for text, evidence in (("Nincs folt a kijelzőn, hibátlan.", "Nincs folt a kijelzőn"),
                               ("A kijelzőn nincs folt, karc sincs.", "kijelzőn nincs folt"),
                               ("Nem repedt a kijelző.", "Nem repedt a kijelző"),
                               ("Se karc, se folt nincs a kijelzőn.", "se folt nincs a kijelzőn"),
                               ("Nyomáshiba nincs.", "Nyomáshiba"),
                               ("Foltmentes kijelző, hibátlan.", "Foltmentes kijelző")):
            cond = conditions.from_text(text)
            self.assertEqual((cond["display"]["state"], cond["display"]["evidence"]), ("ép", evidence), text)
            self.assertEqual(conditions.issues(cond), [], text)
        cond = conditions.from_text("Nincs karc, de folt van a kijelzőn.")
        self.assertEqual(cond["display"]["state"], "hibás", "a „de” után a folt állítás")
        self.assertEqual(conditions.from_text("A kijelző nem hibátlan.")["display"]["state"], conditions.UNKNOWN)

    def test_a_missing_charger_and_its_denial(self):
        """„Töltő nélkül”, „nincs hozzá töltő”, „a töltője nincs meg”: hiányzik; „a töltő nem hiányzik”: megvan."""
        for text in ("Töltő nélkül adom.", "Nincs hozzá töltő.", "Töltője nincs meg.", "Töltő nem tartozék."):
            cond = conditions.from_text(text)
            self.assertEqual((cond["accessories"]["state"], cond["missing"]), ("hiányos", ["töltő"]), text)
            self.assertEqual(conditions.issues(cond), ["töltő"], text)
        cond = conditions.from_text("A töltő nem hiányzik, megvan.")
        self.assertEqual((cond["accessories"]["state"], cond["missing"], cond["present"]),
                         (conditions.UNKNOWN, [], ["töltő"]))
        cond = conditions.from_text("Nincs töltő, működik.")
        self.assertEqual((cond["missing"], cond["function"]["state"]), (["töltő"], "működik"),
                         "a tagadás az előző tagmondatban marad")
        self.assertEqual(conditions.from_text("Nem tudom, működik-e.")["function"]["state"], conditions.UNKNOWN)

    def test_a_missing_box_alone_is_no_issue(self):
        cond = conditions.from_text("Doboz nélkül, a töltője megvan, hibátlan állapotú.")
        self.assertEqual(cond["missing"], ["doboz"])
        self.assertIn("töltő", cond["present"])
        self.assertEqual(conditions.issues(cond), [], "a doboz hiánya önmagában nem hiba")

    def test_a_comma_ends_the_clause(self):
        cond = conditions.from_text("Kábel nem jár hozzá, a töltő eredeti.")
        self.assertEqual(cond["missing"], ["kábel"], "a „nem jár hozzá” a kábelre vonatkozik, a töltőre nem")

    def test_not_working_is_found(self):
        cond = conditions.from_text("Nem kapcsol be, alkatrésznek.")
        self.assertEqual(cond["function"]["state"], "hibás")
        self.assertEqual(conditions.issues(cond), ["működés"])
        self.assertEqual(conditions.from_text("Nem hibátlan, de megy.")["function"]["state"], conditions.UNKNOWN)


class FromModelTests(unittest.TestCase):
    TEXT = "Switch Lite eladó. A kijelző sarkán egy kis karc van, töltő nincs hozzá. Garancia 2027-ig."

    def test_quotes_are_checked_against_the_listing(self):
        data = {"display": {"state": "karcos", "evidence": "a kijelző sarkán egy kis karc"},
                "battery": {"state": "jó", "evidence": "az akku kiváló"},          # kitalált idézet
                "function": {"state": "működik", "evidence": ""},                     # bizonyíték nélkül
                "accessories": {"state": "hiányos", "evidence": "töltő nincs hozzá"},
                "warranty": {"state": "van", "evidence": "Garancia 2027-ig"},
                "missing": ["Töltő (adapter)"]}
        cond = conditions.from_model(data, self.TEXT)
        self.assertEqual(cond["display"]["state"], "karcos")
        self.assertEqual(cond["battery"]["state"], conditions.UNKNOWN, "az idézet nincs a hirdetésben")
        self.assertEqual(cond["function"]["state"], conditions.UNKNOWN, "bizonyíték nélkül nem állíthat")
        self.assertEqual((cond["accessories"]["state"], cond["missing"]), ("hiányos", ["töltő"]))
        self.assertEqual(cond["warranty"]["state"], "van")

    def test_a_photo_can_only_show_damage(self):
        data = {"display": {"state": "hibás", "evidence": "", "from_photo": True},
                "function": {"state": "működik", "evidence": "", "from_photo": True},
                "battery": {"state": "nem tudom", "evidence": ""}, "missing": []}
        cond = conditions.from_model(data, self.TEXT)
        self.assertEqual(cond["display"], {"state": "hibás", "evidence": "a fotón látszik", "photo": True})
        self.assertEqual(cond["function"]["state"], conditions.UNKNOWN, "jót a fotóról nem állíthat")
        self.assertEqual(cond["battery"]["state"], conditions.UNKNOWN, "ismeretlen érték")
        self.assertIn("kijelző: hibás (a fotón)", conditions.describe(cond))
        self.assertEqual(conditions.from_model("nem szótár", self.TEXT), conditions.unknown())

    def test_merge_keeps_the_worse_known_state(self):
        model = conditions.from_model({"display": {"state": "ép", "evidence": "Switch Lite eladó"}, "missing": []},
                                      self.TEXT)
        words = conditions.from_text(self.TEXT)
        merged = conditions.merge(model, words)
        self.assertEqual(merged["display"]["state"], "karcos", "a „rendben” nem írja felül a karcot")
        self.assertEqual(merged["missing"], ["töltő"])
        self.assertEqual(merged["warranty"]["state"], "van")
        self.assertEqual(conditions.compact(merged)["issues"], ["töltő"])


class BandTests(unittest.TestCase):
    def test_similar_defects_set_the_band_without_double_counting(self):
        refs = [{"p": 34000, "issues": ["kijelző"]}, {"p": 36000, "issues": ["kijelző"]},
                {"p": 30000, "issues": ["kijelző", "töltő"]}, {"p": 50000, "issues": ["akkumulátor"]}]
        covered, price, low, n = conditions.best_band(["kijelző", "töltő"], refs, 60000, 58000)
        self.assertEqual((covered, price, low, n), ({"kijelző"}, 35000, 34000, 2),
                         "két ugyanígy hibás: az ő áruk; a töltőhöz külön alap kell")
        self.assertEqual(conditions.best_band(["töltő"], refs, 60000, 58000), (set(), 60000, 58000, 0),
                         "egy hasonló nem elég: a hibátlanok ára marad")
        self.assertEqual(conditions.best_band([], refs, 60000, None), (set(), 60000, None, 0))

    def test_issue_text(self):
        self.assertEqual(conditions.issue_text(["kijelző", "töltő"]), "hibás a kijelzője, nincs töltő")
        self.assertEqual((conditions.part_name("Adapter"), conditions.part_name("akku")), ("töltő", "akkumulátor"))


if __name__ == "__main__":
    unittest.main()
