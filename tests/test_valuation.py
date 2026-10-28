"""Árbecslés és haszon (deals.py): a hirdetési ár (kínálati ár) mediánja, a gyors eladási becslés a bizonytalanságával,
a becslés megbízhatósága (nem csak a darabszám), az eladás költségei (csomagolás, eladási díj, egyéb, tartalék: a meg
nem adott nem csendben nulla), és a javasolt legmagasabb vételár, minden költséggel; ugyanígy a sima találatnál, az
alkunál és a csomagnál.

Futtatás:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_deals import HunterCase  # noqa: E402

from jarvis_core.deals import (PIECE_COST, SHIPPING, DealError, bundle_value, reliability, safe_sale,  # noqa: E402
                               sale_range, valuation_text)

DAY = 86400
NOW = 1_800_000_000.0


def used(n: int, sites: tuple[str, ...] = ("Hardverapró", "Jófogás"), age_days: float = 0.0, **extra) -> list[dict]:
    return [{"t": f"RTX 3060 Ti {i}", "p": 95000 + i * 500, "s": sites[i % len(sites)], "u": f"https://x.hu/{i}",
             "src": "árbázis", "seen": int(NOW - age_days * DAY), **extra} for i in range(n)]


class ReliabilityTests(unittest.TestCase):
    """A becslés megbízhatósága: hány hasonló ár, hány oldalról, mennyire frissek, mennyire szórnak, mennyire biztos a
    változat, és hány hasonló állapota ellenőrzött; a sávja ehhez igazodik."""

    def deal(self, basis_used: list[dict], **extra) -> dict:
        return {"median": 100000, "band": [96000, 104000], "basis": {"used": basis_used, "n_used": len(basis_used)},
                **extra}

    def test_many_fresh_independent_prices_are_reliable(self):
        rel = reliability(self.deal(used(9), clean_n=3), NOW)
        self.assertEqual(rel["level"], "magas")
        self.assertEqual(rel["why"], [])
        self.assertEqual(rel["good"], ["9 hasonló ár", "2 oldalról", "friss árak", "szorosak az árak",
                                       "3 hasonló a leírása szerint is hibátlan"])

    def test_few_old_one_site_prices_are_not(self):
        """Kevés, régi, egy oldalról jövő ár: alacsony, és látszik, miért (nem csak a darabszám számít)."""
        rel = reliability(self.deal(used(4, sites=("Hardverapró",), age_days=20), clean_n=0), NOW)
        self.assertEqual(rel["level"], "alacsony")
        self.assertEqual(rel["why"], ["csak 4 hasonló ár", "egy oldalról", "a hasonló árak nagy része 7 napnál régebbi",
                                      "a hasonlók állapota a leírásuk szerint nagyrészt nem ismert"])

    def test_approximate_comparables_and_an_unclear_variant_lower_it(self):
        """Ugyanannyi ár, de a többségük csak közelítő (más gyártó, más sebesség), és a vizsgált hirdetésből nem derül ki
        a változat: egy szinttel lejjebb, az okkal."""
        note = "a hirdetésből nem derül ki: memória (VRAM); a legolcsóbb változathoz (8GB) mértem"
        exact = reliability(self.deal(used(9), clean_n=3), NOW)
        rough = reliability(self.deal(used(9, approx="más gyártó"), clean_n=3, variant_note=note), NOW)
        self.assertEqual((exact["level"], rough["level"]), ("magas", "közepes"))
        self.assertEqual(rough["why"], ["a hasonlók többsége csak közelítő (9 a 9-ból)", note])

    def test_a_reference_you_gave_is_yours(self):
        rel = reliability({"median": 100000, "basis": {"used": [], "given": True}}, NOW)
        self.assertEqual(rel["level"], "megadott")

    def test_the_quick_sale_range_follows_the_reliability(self):
        """A gyors eladási becslés sávja: magasnál ±5%, közepesnél ±10%, alacsonynál ±20%; a felső vége legfeljebb a
        hirdetési árak mediánja (gyorsan annál drágábban nem kel el)."""
        sale = safe_sale(100000)                         # 90 000
        self.assertEqual(sale_range(sale, 100000, "magas"), [85500, 94500])
        self.assertEqual(sale_range(sale, 100000, "közepes"), [81000, 99000])
        self.assertEqual(sale_range(sale, 100000, "alacsony"), [72000, 100000])


class FeeTests(HunterCase):
    """Az eladás költségei (Eladási költségek): a meg nem adott látszik, a megadott levonódik, és a javasolt legmagasabb
    vételár is ezzel számol."""

    ROW = {"text": "Palit RTX 3060 Ti Dual", "title": "Palit RTX 3060 Ti Dual", "price": 60000, "site": "Hardverapró",
           "platform": "hardverapro", "url": "https://hardverapro.hu/apro/palit/friss.html"}

    def test_unset_costs_are_shown_not_silently_zero(self):
        """Alapból egyik sincs megadva: a haszonban 0-val szerepelnek, de a találatnál és az értesítésben látszik. A 0
        megadott nulla (nem „nincs megadva”); az üres mező visszaállít; a hibás érték hibaüzenet."""
        self.assertEqual(self.hunter.state()["fees"], {"packaging": None, "sell_pct": None, "sell_fixed": None,
                                                       "reserve_pct": None})
        search = self.ready()
        deal = self.hunter._deal(search, self.ROW, 100000)
        self.assertEqual(deal["fees_unset"], ["csomagolás", "eladási díj", "egyéb eladási költség", "tartalék"])
        self.assertIn("nincs megadva: csomagolás, eladási díj, egyéb eladási költség, tartalék (0 Ft-tal számoltam)",
                      valuation_text(deal))
        state = self.hunter.set_fees({"packaging": "500", "sell_pct": "5", "sell_fixed": "0", "reserve_pct": ""})
        self.assertEqual(state["fees"], {"packaging": 500, "sell_pct": 5.0, "sell_fixed": 0, "reserve_pct": None})
        self.assertEqual(self.hunter._deal(search, self.ROW, 100000)["fees_unset"], ["tartalék"])
        for bad in ({"sell_pct": "sok"}, {"reserve_pct": "60"}, {"packaging": "-1"}):
            with self.assertRaises(DealError):
                self.hunter.set_fees(bad)
        self.assertEqual(self.hunter.state()["fees"]["packaging"], 500, "a hibás beállítás nem módosít semmit")
        self.assertIn("Eladási költségek: csomagolás 500 Ft, eladási díj 5%, egyéb eladási költség 0 Ft.",
                      [e["text"] for e in self.hunter.journal.entries()])

    def test_selling_costs_reduce_the_profit_and_the_highest_price(self):
        """A megadott csomagolás (500 Ft), eladási díj (5%) és tartalék (3%) a haszonból levonódik (a gyors eladási
        árra számolva), tételesen látszik, és a javasolt legmagasabb vételár is ennyivel kisebb: ennyiért még meglenne a
        beállított haszon (a keresés 30%-os kedvezménye), minden költséggel."""
        search = self.ready()
        before = self.hunter._deal(search, self.ROW, 100000)
        self.hunter._reprice(before)
        self.hunter.set_fees({"packaging": "500", "sell_pct": "5", "sell_fixed": "", "reserve_pct": "3"})
        deal = self.hunter._deal(search, self.ROW, 100000)
        sale = safe_sale(100000)
        self.assertEqual([(c["what"], c["amount"]) for c in deal["cost_parts"]],
                         [("posta", SHIPPING), ("csomagolás", 500), ("eladási díj (5%)", round(sale * 0.05)),
                          ("tartalék (3%)", round(sale * 0.03))])
        selling = 500 + round(sale * 0.05) + round(sale * 0.03)
        self.assertEqual(deal["profit"], sale - 60000 - SHIPPING - selling)
        self.assertEqual(deal["profit"], before["profit"] - selling)
        self.assertEqual(self.hunter._reprice(deal), ("ok", ""))
        self.assertEqual(deal["profit"], sale - 60000 - SHIPPING - selling, "az újraszámolás sem vonja le kétszer")
        self.assertEqual(before["max_buy"], 70000, "a 30% kedvezmény határa")
        self.assertEqual(deal["max_buy"], 70000 - selling)
        self.assertEqual(deal["sale_mode"], "ahogy van")
        self.assertEqual(deal["fees_unset"], ["egyéb eladási költség"], "ami nincs megadva, az látszik")
        self.assertEqual(len(deal["resale_range"]), 2)
        self.assertEqual(deal["profit_range"][1] - deal["profit_range"][0],
                         deal["resale_range"][1] - deal["resale_range"][0])

    def test_too_much_selling_cost_means_not_worth_it(self):
        """A 60 000 Ft-os ár a keresés határa (70 000) alatt van, de a 20 000 Ft-os eladási költséggel már a határ fölé
        kerül: az újraszámolás „nem éri meg” (nem szól róla sürgősen), az okkal."""
        search = self.ready()
        deal = self.hunter._deal(search, self.ROW, 100000)
        self.assertEqual(self.hunter._reprice(deal), ("ok", ""))
        self.hunter.set_fees({"packaging": "20000"})
        deal = self.hunter._deal(search, self.ROW, 100000)
        state, why = self.hunter._reprice(deal)
        self.assertEqual(state, "nem éri meg")
        self.assertIn("haszon marad, ez már kevés", why)
        self.assertEqual(deal["max_buy"], 50000)

    def test_a_bundle_uses_the_given_packaging_instead_of_the_estimate(self):
        """A csomagnál darabonként a beépített becslés (PIECE_COST) helyett a megadott csomagolás, és az eladási díj a
        darab árára."""
        lines = [{"name": "PS4 kontroller", "count": 3, "unit": 4800, "source": "piac"},
                 {"name": "PS4 játék", "count": 5, "unit": 1500, "source": "becslés"}]
        info = bundle_value(lines, 6000)
        self.assertEqual(info["profit"], 3 * 4800 + 5 * 1500 - 6000 - SHIPPING - 8 * PIECE_COST)
        self.assertEqual(self.hunter._piece_cost(4800), PIECE_COST, "megadott csomagolás nélkül a becslés")
        self.hunter.set_fees({"packaging": "800", "sell_pct": "10"})
        self.assertEqual(self.hunter._piece_cost(4800), 800 + 480)
        self.assertEqual(self.hunter._piece_cost(1500), 800 + 150)


if __name__ == "__main__":
    unittest.main()
