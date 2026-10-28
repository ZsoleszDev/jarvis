"""Csomagbontás (deals.py): a csomaghirdetések (több darab egy áron) felmérése darabonként. A modell előbb a címük alapján
választ, aztán a leírásból és a fotókból tételesen felsorolja, mi van benne; a darabár az árbázisból (vagy a gyors oldalak
hirdetéseiből), ahol nincs, a modell becsléséből, óvatosan. Csak akkor szól, ha darabonként eladva jóval többet ér.

Futtatás a projekt mappájából:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_deals import CATEGORY, SWITCH, SWITCH_VINTED, FakeLlm, HunterCase, listing  # noqa: E402
from test_work import jpeg  # noqa: E402

from jarvis_core.deals import (BUNDLE_INBOX, LEFTOVER_RATIO, PIECE_COST, SHIPPING, DealError,  # noqa: E402
                               DealHunter, assess_bundle, bundle_like, bundle_parts, bundle_value, careful_low,
                               safe_sale)
from jarvis_core.pricebook import price_scope  # noqa: E402
from jarvis_core.selling import round_price  # noqa: E402
from jarvis_core.selling import PLATFORMS  # noqa: E402

# A „konzolcsomag” keresés a Jófogáson: egy keresőhirdetés, egy márkás csomag és egy gyerekjáték-csomag (a ruha
# nincs köztük: azzal az automata kereskedés nem foglalkozik).
WANTED = listing(1, "Keresek konzolcsomagot", 5000, "www.jofogas.hu")
BRANDED = listing(2, "Márkás konzolcsomag 10 db", 6000, "www.jofogas.hu")
BABY = listing(3, "Gyerekjáték csomag", 3000, "www.jofogas.hu")
CONSOLES = [WANTED, BRANDED, BABY]
# Amit a modell a márkás csomagban lát (a fotókról és a leírásból): darabonként a becsült ára.
LINES = [{"name": "PS4 kontroller", "query": "PS4 kontroller", "count": 3, "estimate": 8000},
         {"name": "PS4 játék", "query": "PS4 játék", "count": 5, "estimate": 2500},
         {"name": "Kábel", "query": "", "count": 4, "estimate": 500}]
BRANDED_PAGE = {"title": "Márkás konzolcsomag 10 db", "images": ["https://img.jofogas.hu/1.jpg"],
                "text": "Márkás konzolcsomag: 3 PS4 kontroller, 5 PS4 játék, pár kábel. Szép állapotban. Eladó: Kata "
                        "(25 értékelés)"}


class BundleLlm(FakeLlm):
    """A csomagoknál: melyiket nézze meg (picks: a sorok közül, amelyikben benne van a szó), és mi van benne (lines)."""

    def __init__(self):
        super().__init__()
        self.pick_words = ["Márkás"]
        self.lines = [dict(line) for line in LINES]
        self.bundle, self.shady, self.silent = True, False, False

    def _answer(self, messages, schema):
        if "picks" in schema["properties"]:
            self.calls.append(messages)
            if self.silent:
                raise RuntimeError("nem válaszol")
            rows = [line for line in messages[1]["content"].splitlines() if line[:1].isdigit()]
            return {"picks": [int(row.split(".")[0]) for row in rows if any(w in row for w in self.pick_words)],
                    "reason": "Márkás darabok."}
        if "lines" in schema["properties"]:
            self.calls.append(messages)
            return {"bundle": self.bundle, "lines": self.lines, "suspicious": self.shady,
                    "reason": "PS4 kontrollerek és játékok."}
        return super()._answer(messages, schema)

    def asked(self, word: str) -> list:
        return [m for m in self.calls if word in m[0]["content"]]


class BundleCase(HunterCase):
    def setUp(self):
        super().setUp()
        self.llm = BundleLlm()
        self.hunter = self.make()
        self.browser.results = []
        self.browser.by_url = [("konzolcsomag", CONSOLES)]
        self.browser.pages[BRANDED["url"]] = BRANDED_PAGE

    def make(self, sub: str = "", llm="default") -> DealHunter:
        hunter = DealHunter(Path(self.tmp.name) / sub, PLATFORMS, lambda: self.browser,
                            self.llm if llm == "default" else llm, clock=self.clock, image_get=lambda url: jpeg())
        hunter.set_day(0, 24)
        return hunter

    def consoles(self) -> dict:
        state = self.hunter.add_bundle_search("konzolcsomag", ["jofogas"])
        self.hunter.set_enabled(True)
        return state["bundles"]["searches"][-1]

    def journal(self) -> list[str]:
        return [e["text"] for e in self.hunter.journal.entries()]


class BundleMathTests(unittest.TestCase):
    def test_what_looks_like_a_bundle(self):
        for text in ("Márkás konzolcsomag 10 db", "Lego ömlesztve 3 kg", "PC alkatrészek egyben", "15 db póló",
                     "Videojátékok egyben eladók", "Ruhák csomagban, 38-as", "Lego gyűjtemény"):
            self.assertTrue(bundle_like(text), text)
        for text in ("Keresek konzolcsomagot", "Lego 42115 szett", "Eredeti csomagolásában, bontatlan",
                     "Csomagautomatába is küldöm", "RTX 3060 Ti", "2 db ventilátorral"):
            self.assertFalse(bundle_like(text), text)

    def test_the_value_piece_by_piece(self):
        """A PIECE_MIN alatti darabok egy maradék csomagban (az értékük fele), a többi egyenként; költség: a vétel postája
        és darabonként a csomagolás."""
        lines = [{"name": "PS4 kontroller", "count": 3, "unit": 4800, "source": "piac"},
                 {"name": "PS4 játék", "count": 5, "unit": 1500, "source": "becslés"},
                 {"name": "Kábel", "count": 4, "unit": 300, "source": "becslés"}]
        value = bundle_value(lines, 6000)
        leftover = round(4 * 300 * LEFTOVER_RATIO)
        self.assertEqual((value["value"], value["leftover"], value["small"], value["listings"]),
                         (3 * 4800 + 5 * 1500 + leftover, leftover, 4, 3 + 5 + 1))
        self.assertEqual(value["profit"], value["value"] - 6000 - SHIPPING - 9 * PIECE_COST)
        self.assertEqual((value["sure"], value["top"]), (round(14400 / 22500, 2), round(4800 / 22500, 2)),
                         "top: a legértékesebb darab (nem a tétel) része")
        self.assertEqual(bundle_value([], 1000)["value"], 0)

    def test_how_safe_a_bundle_is(self):
        def deal(sure, profit, price=10000, listings=5, top=0.3, lines=3):
            return {"price": price, "profit": profit,
                    "bundle": {"sure": sure, "listings": listings, "top": top, "lines": [{}] * lines}}
        self.assertEqual(assess_bundle(deal(0.1, 6000))["level"], "kockázatos",
                         "a darabárak csak becslések, és nem hozza a dupláját")
        self.assertEqual(assess_bundle(deal(0.1, 12000))["level"], "jó", "becslés, de a dupláját hozza")
        self.assertIn("a darabárak egy része csak becslés", assess_bundle(deal(0.1, 12000))["why"])
        self.assertEqual(assess_bundle(deal(0.8, 6000))["level"], "biztos", "az értéke hirdetési árakból jön")
        many = assess_bundle(deal(0.8, 6000, listings=20))
        self.assertEqual((many["level"], many["why"]), ("jó", ["sok munka: 20 külön hirdetés"]))
        one = assess_bundle(deal(0.8, 6000, top=0.7))
        self.assertIn("az érték nagy része egy tételen múlik: nézd meg a fotón, tényleg az-e", one["why"])
        self.assertEqual(assess_bundle(deal(0.8, 6000))["good"][0], "kb. 60% becsült haszon")

    def test_a_bought_bundle_splits_into_items(self):
        """A vételár és a posta az értékük arányában oszlik szét, forintra pontosan; az apróságok egy tárgyban."""
        deal = {"text": "Márkás konzolcsomag 10 db", "price": 6000, "bundle": {
            "lines": [{"name": "PS4 kontroller", "query": "PS4 kontroller", "count": 3, "unit": 4800},
                      {"name": "PS4 játék", "query": "PS4 játék", "count": 1, "unit": 1500},
                      {"name": "Kábel", "query": "", "count": 4, "unit": 300}],
            "leftover": 600, "small": 4}}
        parts = bundle_parts(deal, 6001, 1500)
        self.assertEqual([p["prompt"] for p in parts], ["PS4 kontroller, 3 db, 4800 Ft", "PS4 játék, 1500 Ft",
                                                        "Apróságok egyben a csomagból (4 db), 600 Ft"])
        self.assertEqual([p["target_price"] for p in parts], [14400, 1500, 600])
        self.assertEqual(sum(p["bought_price"] for p in parts), 6001)
        self.assertEqual(sum(p["bought_shipping"] for p in parts), 1500)
        self.assertEqual(parts[1]["bought_price"], 6001 * 1500 // 16500)
        self.assertIn("Csomagból („Márkás konzolcsomag 10 db”): 3 db, darabonként kb. 4800 Ft", parts[0]["trade_note"])
        self.assertEqual(bundle_parts({**deal, "bundle": {"lines": [], "leftover": 0}}, 1000), [])


class BundleSearchTests(BundleCase):
    def test_a_bundle_search_finds_a_bundle_worth_more_in_pieces(self):
        """Csomagkeresés: a találatokból (a keresőhirdetés nélkül) a modell a márkásat választja; az oldala, a leírása és
        a fotója alapján a modell felsorolja, mi van benne; darabonként eladva megéri, ezért szól róla."""
        search = self.consoles()
        self.assertEqual((search["query"], search["sites"], search["every"]), ("konzolcsomag", ["jofogas"], 60))
        self.assertTrue(self.hunter.state()["bundles"]["on"], "a csomagkeresés bekapcsolja a csomagbontást")
        self.assertTrue(self.hunter.available())
        self.assertEqual(self.hunter.run(notify=self.notify), "1 keresés: 1 olcsó hirdetés.")
        [picked] = self.llm.asked("legfeljebb kettőt")
        self.assertNotIn("Keresek", picked[1]["content"], "a keresőhirdetés meg sem jön")
        self.assertIn("Gyerekjáték csomag – 3 000 Ft", picked[1]["content"])
        self.assertEqual(self.browser.looked, [BRANDED["url"]], "csak a kiválasztott csomag oldalát nyitotta meg")
        [itemized] = self.llm.asked("tételenként")
        self.assertIn("3 PS4 kontroller, 5 PS4 játék", itemized[1]["content"], "a modell a leírást is látja")
        self.assertEqual(len(itemized[1]["images"]), 1, "és a fotót is")
        # kontroller: 8 000 × 60% = 4 800, játék: 2 500 × 60% = 1 500 (még egyenként), kábel: 300 (egyben, a fele)
        [(_kind, title, text, extra)] = self.notices
        self.assertEqual(title, "Csomag: Márkás konzolcsomag 10 db")
        self.assertIn("Darabonként eladva kb. 22 500 Ft (9 hirdetés): 3 db PS4 kontroller (kb. 4 800 Ft/db); 5 db PS4 "
                      "játék (kb. 1 500 Ft/db); 4 apróság egyben kb. 600 Ft. A várható haszon kb. 12 300 Ft", text)
        self.assertIn("Jó vétel, de figyelj: a darabárak egy része csak becslés.", text)
        self.assertIn("Csomagkeresés: konzolcsomag.", text)
        self.assertTrue(extra["urgent"])
        [deal] = self.hunter.state()["deals"]
        self.assertEqual((deal["bundle"]["value"], deal["profit"], deal["bundle"]["listings"], deal["photos_checked"]),
                         (22500, 12300, 9, 1))
        self.assertEqual([(l["name"], l["unit"], l["source"]) for l in deal["bundle"]["lines"]],
                         [("PS4 kontroller", 4800, "becslés"), ("PS4 játék", 1500, "becslés"), ("Kábel", 300, "becslés")])
        self.assertNotIn("images", deal, "a fotók címe csak a modellkérdéshez kellett")
        self.assertTrue(any(t.startswith("Jó csomag: „Márkás konzolcsomag 10 db” 6 000 Ft (Jófogás, csomag, kb. 12 300 Ft "
                                         "haszon) – Csomagkeresés: konzolcsomag") for t in self.journal()))
        self.assertEqual(self.hunter.state()["bundles"]["waiting"], 0, "a gyerekjáték-csomagot is látottnak jegyezte")

        self.notices.clear()
        calls = len(self.llm.calls)
        self.hunter.run(notify=self.notify, force=True)
        self.assertEqual((self.notices, len(self.llm.calls)), ([], calls), "egy csomagról egyszer szól")

    def test_not_worth_it_or_not_a_bundle(self):
        self.browser.by_url = [("konzolcsomag", [{**BRANDED, "price": 20000}])]
        self.consoles()
        self.hunter.run(notify=self.notify)
        self.assertEqual(self.notices, [])
        self.assertIn("Csomag, nem éri meg: „Márkás konzolcsomag 10 db” 20 000 Ft (Jófogás) – darabonként kb. 22 500 Ft "
                      "(9 hirdetés), a haszon kb. -1 700 Ft.", self.journal())
        other = listing(4, "Márkás konzolok egyben eladók", 3000, "www.jofogas.hu")
        self.browser.by_url = [("konzolcsomag", [other])]
        self.browser.pages[other["url"]] = BRANDED_PAGE
        self.llm.bundle = False
        self.hunter.run(notify=self.notify, force=True)
        self.assertEqual(self.notices, [])
        self.assertIn("Nem csomag (vagy nem derül ki, mi van benne): „Márkás konzolok egyben eladók” 3 000 Ft.",
                      self.journal())

    def test_a_per_piece_price_is_not_a_bundle_price(self):
        """Egy többtételes hirdetés ára lehet darabár vagy a legolcsóbb tételé („3 000 Ft/db”, „2 000 Ft-tól”): azt
        egyben nem méri fel (a modellt sem kérdezi meg, mi van benne), és a naplóba írja, miért; ha darabárra és egyben
        árra is utal („2 000 Ft/db, egyben olcsóbban”), az sem tisztázott. A „darabonként is eladók” csak annyi, hogy külön
        is kapható: egy „egyben” ár mellett az az egész ára. A csomagnak hirdetett, darabárt nem író hirdetés ára az
        egészé."""
        self.browser.pages[BRANDED["url"]] = {**BRANDED_PAGE, "text": "Márkás konzolcsomag: 3 PS4 kontroller, 5 PS4 "
                                              "játék. Ár: 6 000 Ft/db, a kábelek ajándék. Eladó: Kata (25 értékelés)"}
        self.consoles()
        self.hunter.run(notify=self.notify)
        self.assertEqual((self.notices, self.llm.asked("tételenként")), ([], []), "darabárral nem csomag")
        [line] = [t for t in self.journal() if t.startswith("Csomag, de")]
        self.assertIn("az ára darabár vagy a legolcsóbb tételé („", line)
        self.assertIn("6 000 Ft/db", line)
        self.assertIn("egyben nem mértem fel (kérdezd meg az eladót, mennyi az egész)", line)
        mixed = listing(5, "Márkás konzolcsomag 8 db", 5000, "www.jofogas.hu")
        self.browser.by_url = [("konzolcsomag", [mixed])]
        self.browser.pages[mixed["url"]] = {**BRANDED_PAGE, "text": "PS4 játékok 2 000 Ft/db, egyben olcsóbban."}
        self.hunter.run(notify=self.notify, force=True)
        self.assertTrue(any("nem derül ki, hogy az ára az egészre vonatkozik-e, vagy darabár" in t and
                            "Márkás konzolcsomag 8 db" in t for t in self.journal()), self.journal())
        self.assertEqual(self.llm.asked("tételenként"), [])
        self.assertEqual(price_scope("Márkás konzolcsomag: 3 PS4 kontroller, 5 PS4 játék"), ("", ""),
                         "a csomagnak hirdetett, darabárt nem író: az egész ára (a csomagbontás felméri)")
        self.assertEqual(price_scope("Játékok egyben eladók")[0], "egyben")
        self.assertEqual(price_scope("PS4 játékok 2 000 Ft-tól")[0], "darabár")
        self.assertEqual(price_scope("PS4 játékok egyben 10 000 Ft, darabonként is eladók")[0], "egyben",
                         "külön is kapható, de az ár az egészé")
        self.assertEqual(price_scope("PS4 játékok, darabonként is eladók")[0], "kétséges",
                         "nem tudni, az ár egy darabé vagy mindé")

    def test_a_risky_bundle_is_not_told_unless_you_want_it(self):
        """Csak becsült darabárak, és nem hozza a dupláját: kockázatos; erről csak akkor szól, ha a „csak nyugodt szívvel”
        ki van kapcsolva."""
        self.browser.by_url = [("konzolcsomag", [{**BRANDED, "price": 9500}])]
        self.consoles()
        self.hunter.run(notify=self.notify)
        self.assertEqual(self.notices, [])
        self.assertTrue(any(t.startswith("Kockázatos csomag, nem szóltam") for t in self.journal()))
        self.hunter.set_limits(safe_only=False)
        other = listing(5, "Márkás konzolcsomag 11 db", 9500, "www.jofogas.hu")
        self.browser.by_url = [("konzolcsomag", [other])]
        self.browser.pages[other["url"]] = BRANDED_PAGE
        self.hunter.run(notify=self.notify, force=True)
        [(_kind, _title, text, _extra)] = self.notices
        self.assertIn("Kockázatos: a darabárak többsége csak becslés, és nem hozza a vételár dupláját.", text)

    def test_a_suspicious_bundle_is_left_out(self):
        self.llm.shady = True
        self.consoles()
        self.hunter.run(notify=self.notify)
        self.assertEqual((self.notices, self.hunter.state()["deals"]), ([], []))
        self.assertTrue(any(t.startswith("Gyanús, nem szóltam róla: „Márkás konzolcsomag 10 db”") for t in self.journal()))

    def test_market_prices_are_used_where_known(self):
        """Ha az árbázis ismeri egy tétel termékét, a mostani hirdetési árakból számol (óvatosan, de legfeljebb a becslés
        kétszereséig)."""
        self.hunter.book.add([{**r, "platform": "jofogas"} for r in SWITCH] +
                             [{**r, "platform": "vinted"} for r in SWITCH_VINTED])
        self.llm.lines = [{"name": "Nintendo Switch OLED", "query": "Nintendo Switch OLED", "count": 1, "estimate": 70000},
                          {"name": "Kábel", "query": "", "count": 2, "estimate": 500}]
        self.browser.by_url = [("konzolcsomag", [{**BRANDED, "price": 40000}])]
        self.consoles()
        self.hunter.run(notify=self.notify)
        [deal] = self.hunter.state()["deals"]
        switch = deal["bundle"]["lines"][0]
        reference = self.hunter.book.reference(self.hunter.book.key("Nintendo Switch OLED")[0])
        self.assertEqual((switch["source"], switch["n"], switch["unit"]),
                         ("piac", reference["n"], round_price(safe_sale(reference["price"], careful_low(reference)))),
                         "a szokásos ár 90%-a, de legfeljebb az alsó negyed")
        self.assertEqual(deal["bundle"]["sure"], round(switch["unit"] / deal["bundle"]["value"], 2))
        self.assertIn("az érték nagy része egy tételen múlik: nézd meg a fotón, tényleg az-e", deal["safety"]["why"])
        self.llm.lines[0]["estimate"] = 30000
        other = listing(6, "Márkás konzolcsomag 12 db", 20000, "www.jofogas.hu")
        self.browser.by_url = [("konzolcsomag", [other])]
        self.browser.pages[other["url"]] = BRANDED_PAGE
        self.hunter.run(notify=self.notify, force=True)
        self.assertEqual(self.hunter.state()["deals"][0]["bundle"]["lines"][0]["unit"], 60000,
                         "ha a modell jóval kevesebbre becsülte, legfeljebb a becslés kétszerese (félreismerhette)")

    def test_the_model_not_answering_keeps_the_bundle_for_later(self):
        self.llm.silent = True
        self.consoles()
        self.hunter.run(notify=self.notify)
        self.assertEqual(self.hunter.state()["bundles"]["waiting"], 2, "a modell nem válaszolt: vár")
        self.hunter.run(notify=self.notify, force=True)
        self.assertEqual(self.hunter.state()["bundles"]["waiting"], 0, "kétszer sem: kimarad")
        self.llm.silent = False
        self.hunter.run(notify=self.notify, force=True)
        self.assertEqual(self.notices, [], "látottnak jegyezte")

    def test_only_two_bundles_a_round(self):
        rows = [listing(10 + n, f"Márkás konzolcsomag {n}", 6000, "www.jofogas.hu") for n in range(4)]
        self.browser.by_url = [("konzolcsomag", rows)]
        for row in rows:
            self.browser.pages[row["url"]] = BRANDED_PAGE
        self.consoles()
        self.hunter.run(notify=self.notify)
        self.assertEqual((len(self.notices), len(self.browser.looked)), (2, 2))
        self.assertEqual(self.hunter.state()["bundles"]["waiting"], 2, "a többi a következő körre vár")
        self.hunter.run(notify=self.notify)
        self.assertEqual(len(self.notices), 4)

    def test_no_clothing_bundles(self):
        """Ruhával nem kereskedik: ruhás csomagkeresést nem vesz fel, a régi (a „Ruhacsomag” alapbeállításból) betöltéskor
        kimarad, az alapbeállítások között sincs, és a körök ruhás csomaghirdetése sem kerül a csomagbontásnak."""
        with self.assertRaisesRegex(DealError, "Ruhával és cipővel nem kereskedem"):
            self.hunter.add_bundle_search("ruhacsomag")
        self.consoles()
        with self.hunter._lock:
            self.hunter._data["bundles"]["searches"].append({"id": "bregi", "query": "ruhacsomag", "sites": ["jofogas"]})
            self.hunter._save()
        reloaded = self.make()
        self.assertEqual([s["query"] for s in reloaded.state()["bundles"]["searches"]], ["konzolcsomag"])
        self.assertNotIn("Ruhacsomag", [p["name"] for p in reloaded.state()["bundles"]["presets"]])
        rest = self.hunter._to_bundles([listing(7, "Márkás ruhacsomag 10 db", 6000, "www.jofogas.hu"),
                                        listing(8, "Lego ömlesztve 3 kg", 6000, "www.jofogas.hu")], "teszt")
        self.assertEqual([r["text"] for r in rest], ["Márkás ruhacsomag 10 db"], "a ruhás csomag nem megy oda")
        self.assertEqual(self.hunter.state()["bundles"]["waiting"], 1)

    def test_settings_and_limits(self):
        self.consoles()
        with self.assertRaises(DealError):
            self.hunter.add_bundle_search("Konzolcsomag")
        for n in range(7):
            self.hunter.add_bundle_search(f"csomag {n}")
        with self.assertRaises(DealError):
            self.hunter.add_bundle_search("még egy")
        state = self.hunter.set_bundles(min_profit="12 000")
        self.assertEqual(state["bundles"]["min_profit"], 12000)
        self.assertEqual([p["name"] for p in state["bundles"]["presets"]][:2], ["Lego ömlesztve", "Videojáték-csomag"])
        search = state["bundles"]["searches"][0]
        self.assertEqual(len(self.hunter.remove_bundle_search(search["id"])["bundles"]["searches"]), 7)
        self.hunter.set_bundles(on=False)
        self.assertFalse(self.make().state()["bundles"]["on"], "a gépen megmarad")
        self.assertIn("Kikapcsoltad a csomagbontást.", self.journal())

    def test_the_buy_limit_and_the_inbox_size(self):
        self.hunter.set_limits(max_buy=5000)
        self.consoles()
        self.llm.pick_words = ["Gyerekjáték", "Márkás"]
        self.hunter.run(notify=self.notify)
        [picked] = self.llm.asked("legfeljebb kettőt")
        self.assertNotIn("Márkás", picked[1]["content"], "a vételi limit fölötti csomag meg sem jön")
        self.hunter.set_limits(max_buy="")
        rows = [listing(100 + n, f"Konzolcsomag {n}", 5000, "www.jofogas.hu") for n in range(BUNDLE_INBOX + 5)]
        self.assertEqual(self.hunter._to_bundles(rows, "teszt"), [])
        self.assertEqual(self.hunter.state()["bundles"]["waiting"], BUNDLE_INBOX, "a legrégebbiek kiesnek")


class BundleSourceTests(BundleCase):
    def test_bundles_seen_on_a_watched_page_are_appraised_too(self):
        """Egy figyelt oldalon (és a böngészésben, a keresésekben, az azonnali figyelőben) a csomaghirdetés eddig
        kimaradt; bekapcsolt csomagbontással felméri."""
        lot = listing(30, "Videokártyák egyben eladók (3 db)", 30000)
        self.browser.by_url = [("videokartya", [lot])]
        self.browser.pages[lot["url"]] = {**BRANDED_PAGE, "title": "Videokártyák egyben"}
        self.llm.pick_words = ["Videokártyák"]
        self.llm.lines = [{"name": "RTX 2060", "query": "RTX 2060", "count": 3, "estimate": 40000}]
        self.hunter.add_watch(CATEGORY)
        self.hunter.set_enabled(True)
        self.hunter.run(notify=self.notify)
        self.assertEqual(self.notices, [], "kikapcsolt csomagbontással kimarad (ahogy eddig)")
        self.assertEqual(self.llm.asked("tételenként"), [])
        self.hunter.set_bundles(on=True)
        self.hunter._data["seen"].clear()
        self.hunter.run(notify=self.notify, force=True)
        [(_kind, title, text, _extra)] = self.notices
        self.assertEqual(title, "Csomag: Videokártyák egyben eladók (3 db)")
        self.assertIn("3 db RTX 2060 (kb. 24 000 Ft/db)", text)
        self.assertIn("Figyelt oldal: Hardverapró: videokartya.", text)

    def test_bundles_from_a_search_and_the_instant_watch(self):
        self.ready(query="RTX 3060 Ti")
        self.hunter.set_bundles(on=True)
        self.llm.pick_words = ["alkatrészek"]
        lot = listing(40, "RTX 3060 Ti + i5 PC alkatrészek egyben", 90000)
        self.browser.results = [lot]
        self.hunter.run(notify=self.notify)
        [picked] = self.llm.asked("legfeljebb kettőt")
        self.assertIn("RTX 3060 Ti + i5 PC alkatrészek egyben", picked[1]["content"], "a keresés csomagja is sorra kerül")
        self.assertEqual(self.browser.looked, [lot["url"]], "a kör végén rögtön")
        self.assertTrue(any(t.startswith("Csomag, nem éri meg: „RTX 3060 Ti + i5 PC alkatrészek egyben”")
                            for t in self.journal()))
        fresh = [{**listing(41, "Lego ömlesztve 5 kg", 15000, "www.jofogas.hu"), "site": "Jófogás",
                  "platform": "jofogas"}]
        waiting = self.hunter.appraise(fresh, "Lego", lambda row: None, lambda q: [], lambda: True, lambda d: None)
        self.assertEqual(waiting, [])
        [row] = self.hunter._data["bundles"]["inbox"]
        self.assertEqual((row["text"], row["from"]), ("Lego ömlesztve 5 kg", "Azonnali figyelő: Lego"))


if __name__ == "__main__":
    unittest.main()
