"""Pontos árazás: ami nem ugyanaz a termék, az nem mérce, és nem is jelölt.

- A laptop- (SO-DIMM) és az asztali memória (DIMM) más termék: a kivitel a címkéről, a gyártói cikkszámból, a
  termékcsaládból, a „laptop” szóból is kiderül, és ha a címből nem, az adatlap leírásából (újramérés, _respec).
- A termék külön eladott része (gyári hűtő, lemezmeghajtó, kijelző, akkumulátor, egy fél fülhallgató, hiányos Lego) és a
  másolata nem a termék ára; a „töltővel”, „dokkolóval”, „töltő nélkül” eladott készülék viszont maga a termék.
- A „Galaxy S21+” a Plus változat, a „kontroller nélkül” eladott konzol konzol, a „2 kontrollerrel” darabszáma nem
  típusjel, a szerszám akku nélküli alapgépe más változat, mint a készlet.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_instant import InstantCase, ha_card, page_html  # noqa: E402

from jarvis_core import parts, specs  # noqa: E402
from jarvis_core.deals import matches_query  # noqa: E402
from jarvis_core.pricebook import KEY_VERSION, PriceBook, product_key, unusable, usable  # noqa: E402


def key(text: str) -> str | None:
    found = product_key(text)
    return found[0] if found else None


class RamFormTests(unittest.TestCase):
    LAPTOP = ("Kingston 8GB DDR4 2666MHz laptop", "8GB DDR4 SO-DIMM", "Samsung 8GB 1Rx8 PC4-2666V-SA1-11",
              "SK Hynix 16GB DDR4 3200 HMAA2GS6AJR8N-XN", "Kingston KVR26S19S8/8 8GB DDR4",
              "Crucial 16GB DDR4 3200 CT16G4SFRA32A", "Samsung M471A1K43DB1-CWE 8GB DDR4 3200",
              "HyperX Impact 16GB DDR4 2666", "Kingston Fury Impact 16GB DDR4 3200", "4GB 2Rx8 PC3L-12800S",
              "Hynix HMT451S6BFR8A-PB 4GB DDR3L", "Micron 8GB DDR4 MTA8ATF1G64HZ-2G6E1",
              "Crucial 16GB DDR5 4800 CT16G48C40S5", "G.Skill Ripjaws F4-3200C22S-8GRS 8GB", "Notebook RAM 16GB DDR4",
              "16GB (2x8GB) DDR4 3200 laptopba", "Mini PC memória 16GB DDR4")
    DESKTOP = ("Kingston Fury Beast 16GB DDR4 3200MHz", "Corsair Vengeance LPX 16GB (2x8GB) DDR4 3200",
               "16GB DDR4 3200MHz asztali RAM", "Samsung 8GB DDR4 2666 M378A1K43CB2-CTD", "Crucial 8GB DDR4 CT8G4DFS8266",
               "8GB DDR4 PC RAM", "8GB DDR4 2400 PC4-19200 UDIMM", "Samsung 16GB 2Rx8 PC4-2400T-UA2-11",
               "G.Skill Trident Z RGB 32GB DDR4", "Kingston KVR26N19S8/8 8GB DDR4", "HyperX Fury HX426C16FB3/8 8GB DDR4")

    def test_the_form_is_read_from_labels_part_numbers_lines_and_words(self):
        """A modul kivitele a címkéjéről („PC4-2666V-SA1”: S = SO-DIMM), a gyártói cikkszámból, a termékcsaládból
        (Impact: laptop; Beast, LPX, Trident: asztali) és a szövegből; ellentmondásnál nem derül ki."""
        for title in self.LAPTOP:
            self.assertEqual(parts.ram_form(title).form, parts.SODIMM, title)
        for title in self.DESKTOP:
            self.assertEqual(parts.ram_form(title).form, parts.DIMM, title)
        for title in ("16GB DDR4 3200", "8GB DDR4 laptopba és asztali gépbe"):
            self.assertEqual(parts.ram_form(title).form, "", title)
        self.assertEqual(parts.ram_form("Kingston 8GB DDR4 SO-DIMM, asztali gépbe nem jó").form, parts.SODIMM,
                         "a biztos jel (a kivitel neve) erősebb a gyenge jel tagadásánál")
        self.assertTrue(parts.ram_form("Hynix 8GB PC4-2133P-RB0").server, "a címke szerint regisztrált (szerver)")

    def test_laptop_and_desktop_memory_get_different_keys(self):
        """A laptop-memória kulcsában benne van a „sodimm”: így az ára nem keveredik az asztali memóriáéval (ez volt a
        hiba: a cikkszámos vagy a „laptop” szót a végén hordozó cím az asztali memóriák árához mért)."""
        for title in self.LAPTOP:
            self.assertIn("sodimm", (key(title) or "").split(), title)
        for title in self.DESKTOP:
            self.assertNotIn("sodimm", (key(title) or "").split(), title)
        self.assertEqual(key("Samsung M471A1K43DB1-CWE 8GB DDR4 3200"), key("8GB DDR4 SO-DIMM"))
        self.assertEqual(key("Kingston Fury Beast 8GB DDR4 3200MHz"), key("8GB DDR4 3200"))
        self.assertEqual(key("Hynix HMT451S6BFR8A-PB 4GB DDR3L"), key("4GB DDR3 SO-DIMM"), "a DDR3L is DDR3")
        # egy egész laptop a memóriájával nem memóriamodul (a „laptop” szó ott a gépé)
        self.assertNotEqual(key("Lenovo laptop i5 8GB DDR4 256GB SSD"), key("8GB DDR4 SO-DIMM"))

    def test_the_module_label_is_the_generation_not_a_model_number(self):
        """A címke („PC4-2666V-SA1-11”) egészében a generáció: nem lesz belőle egy külön típusjel („ddr4sa111”)."""
        spec = specs.extract("Samsung 8GB 1Rx8 PC4-2666V-SA1-11")
        self.assertEqual({name: attr["value"] for name, attr in spec["attrs"].items() if name in ("generation", "form")},
                         {"generation": "ddr4", "form": "sodimm"})
        self.assertEqual(key("Samsung 16GB 2Rx8 PC4-2400T-UA2-11"), key("16GB DDR4"))

    def test_an_unknown_form_compares_only_approximately_and_needs_the_description(self):
        """Ha a vizsgált memória kivitele nem derül ki, egy ismert kivitelű összehasonlító csak közelítő, és amíg a
        leírásból sem derül ki, nem lehet biztos az ára (needs_detail)."""
        want = specs.extract("Kingston 8GB DDR4 2666")
        self.assertEqual(specs.needs_detail(want), ["kivitel"])
        klass, why = specs.compare(want, specs.extract("Kingston Fury Beast 8GB DDR4 3200"))
        self.assertEqual(klass, specs.KOZELITO)
        self.assertIn("nem derül ki: kivitel (ez DIMM (asztali memória))", why[0])
        laptop = specs.extract("Kingston 8GB DDR4 2666", "Laptopomból szereltem ki, hibátlan.", only=specs.DETAIL_TRUSTED)
        self.assertEqual((laptop["attrs"]["form"]["value"], laptop["attrs"]["form"]["source"]), ("sodimm", "leírás"))
        self.assertEqual(specs.learned(want, laptop), ["form"])
        self.assertEqual(specs.keyed("8gb ddr4", laptop), "8gb ddr4 sodimm")
        self.assertEqual(specs.compare(laptop, specs.extract("Kingston Fury Beast 8GB DDR4 3200"))[0], specs.KIZART)

    def test_desktop_memory_counts_unmarked_modules_laptop_memory_does_not(self):
        """Az asztali memóriához a kivitel nélküli hasonló közelítő (a felismerhető laptop-memóriák külön kulcson
        vannak); a laptop-memóriához csak a felismerten laptop-memória mérce."""
        unmarked = specs.extract("Kingston 8GB DDR4 3200")
        klass, why = specs.compare(specs.extract("Kingston Fury Beast 8GB DDR4 3200"), unmarked)
        self.assertEqual((klass, why), (specs.KOZELITO, ["a kivitele nem derül ki (valószínűleg asztali)"]))
        self.assertEqual(specs.compare(specs.extract("Kingston 8GB DDR4 SO-DIMM"), unmarked)[0], specs.BIZONYTALAN)

    def test_a_memory_title_without_ddr_is_known_from_its_line_and_speed(self):
        """A DDR szó nélküli cím („Kingston Fury Impact 16GB 3200”) a termékcsaládjáról és a sebességéről memóriamodul,
        és a generációja is kiderül: így a laptop-memória (Impact) nem keveredik az asztalival (Beast). A 2133 és a 2400
        DDR3 és DDR4 is lehet; az SSD-k (XPG Gammix S50, HyperX Savage SSD) nem memóriák."""
        self.assertEqual(product_key("Kingston Fury Beast 16GB 3200")[0], "16gb ddr4")
        self.assertEqual(product_key("Kingston Fury Impact 16GB 3200")[0], "16gb ddr4 sodimm")
        self.assertEqual(product_key("Kingston Fury Beast 32GB 5600")[0], "32gb ddr5")
        self.assertEqual(product_key("Corsair Vengeance 2x8GB 1600")[0], "2x8gb ddr3")
        self.assertEqual(parts.ram_generation("Kingston HyperX Fury 8GB 2133"), "")
        self.assertEqual(parts.ram_generation("XPG Gammix S50 Lite 512GB 3800 MB/s"), "")
        self.assertEqual(parts.ram_generation("Corsair Vengeance 2100 headset"), "")
        spec = specs.extract("Kingston Fury Impact 16GB 3200")
        self.assertEqual((spec["attrs"]["generation"]["value"], spec["attrs"]["generation"]["evidence"]), ("ddr4", "3200"))
        self.assertFalse(matches_query("16GB DDR4 3200", "Kingston Fury Impact 16GB 3200"))
        self.assertTrue(matches_query("16GB DDR4", "Kingston Fury Beast 16GB 3200"))

    def test_a_generic_memory_search_is_not_a_laptop_memory_search(self):
        """A kivitel nélküli keresés („16GB DDR4”) az asztali memóriáé: a laptop-memória nem jelölt hozzá (a jóváhagyott
        szokásos ára asztali memóriáké); a laptop-memóriát a keresésben kell kérni."""
        self.assertFalse(matches_query("16GB DDR4", "Samsung 16GB DDR4 3200 M471A2K43DB1-CWE"))
        self.assertTrue(matches_query("16GB DDR4 SO-DIMM", "Samsung 16GB DDR4 3200 M471A2K43DB1-CWE"))
        self.assertTrue(matches_query("16GB DDR4 laptop", "Samsung 16GB DDR4 3200 M471A2K43DB1-CWE"))
        self.assertTrue(matches_query("16GB DDR4", "Kingston Fury Beast 16GB DDR4 3200MHz"))
        self.assertTrue(matches_query("16GB DDR4", "Kingston 16GB DDR4 3200"), "amiről nem derül ki, az jöhet")
        self.assertFalse(matches_query("16GB DDR4 SO-DIMM", "Corsair Vengeance LPX 16GB DDR4 3200"))


class PartsTests(unittest.TestCase):
    def test_a_part_sold_alone_is_not_the_products_price(self):
        """A termék egy része külön eladva (a kulcsa a termékéé lenne): nem összehasonlító, nem jelölt."""
        for title, part in (("AMD Ryzen 5 3600 gyári hűtő", "hűtő"), ("Intel i7-8700 gyári hűtő", "hűtő"),
                            ("PS5 Slim lemezmeghajtó", "lemezmeghajtó"), ("PS4 Pro ventilátor", "ventilátor"),
                            ("Nintendo Switch Lite kijelző", "kijelző"), ("Switch OLED kijelző", "kijelző"),
                            ("iPhone 13 kijelző", "kijelző"), ("iPhone 13 Pro hátlap", "hátlap"),
                            ("iPhone 12 akkumulátor", "akkumulátor"), ("Samsung Galaxy S21 kijelző", "kijelző"),
                            ("RTX 3080 vízhűtés blokk", "vízhűtés"), ("RTX 3080 waterblock", "vízhűtés"),
                            ("RTX 3070 hűtő", "hűtő"), ("GTX 1080 backplate", "hátlap"),
                            ("ThinkPad T480 akkumulátor", "akkumulátor"), ("ThinkPad T480 billentyűzet", "billentyűzet"),
                            ("MacBook Air M1 kijelző", "kijelző"), ("MacBook Pro 2015 alaplap", "alaplap"),
                            ("Xbox Series X tápegység", "tápegység"), ("Dyson V11 akkumulátor", "akkumulátor")):
            self.assertEqual(unusable(title), f"a termék egy része, nem maga a termék ({part})", title)
        for title in ("AirPods Pro 2 bal fül", "AirPods 3 jobb fülhallgató", "AirPods Pro 2 tok nélkül",
                      "Lego 42115 doboz nélkül hiányos", "Lego 75192 csak a figurák", "Lego 10294 útmutató"):
            self.assertTrue(unusable(title).startswith("a termék egy része"), title)
        for title in ("iPhone 13 Pro Max dummy", "iPhone 14 Pro replika", "AirPods Pro 2 1:1 másolat",
                      "iPhone 12 utángyártott kijelző"):
            self.assertTrue(unusable(title).startswith("nem eredeti termék"), title)

    def test_a_device_described_with_its_parts_is_the_device(self):
        """A készülék a részeivel, tartozékaival leírva maga a termék: „töltővel”, „töltő nélkül”, „+ gyári hűtő”,
        „karcos kijelzővel”, „új akku” a tárhelyével, „2x ventilátor”, „gyári hűtő” a videókártyán."""
        for title in ("Nintendo Switch OLED töltővel", "Nintendo Switch OLED dokkolóval, töltővel",
                      "iPhone 13 128GB töltővel dobozzal", "iPhone 13 128GB töltő nélkül", "iPhone 13 128GB + tok + töltő",
                      "PS4 Pro kábelekkel", "Samsung Galaxy S21 128GB szilikon tokkal", "AirPods Pro 2 töltőtokkal",
                      "MacBook Air M1 8GB 256GB töltővel, dobozában", "Ryzen 5 3600 + gyári hűtő",
                      "Ryzen 5 3600 dobozos, hűtővel", "Ryzen 5 3600 hűtő nélkül", "Nintendo Switch Lite karcos kijelzővel",
                      "Nintendo Switch Lite kijelző karcos", "iPhone 12 64GB új akku", "iPhone 12 akku 87%",
                      "Galaxy S21 128GB kijelző repedt", "MSI RTX 3070 Gaming X Trio 3 ventilátoros",
                      "Sapphire Pulse RX 6600 2x ventilátor", "Gigabyte RTX 3070 Eagle OC, gyári hűtő, dobozában",
                      "RTX 3080 vízhűtéses", "Lenovo ThinkPad T480 i5 8GB 256GB új akku", "Lego 42115 doboz nélkül",
                      "AirPods Pro 2", "Steam Deck OLED 512GB",
                      # valódi hirdetéscímek mintájára: a készülék a tartozékaival, a javításával, az állapotával
                      "iPhone 13 128GB fekete, akku 89%, töltővel", "iPhone 12 64GB kék, új akkumulátorral",
                      "Samsung Galaxy S22 Ultra 256GB, tokkal, fóliával", "PS5 Slim Digital + 2 kontroller + töltőállomás",
                      "Nintendo Switch OLED, dokkoló, joy-con, töltő, doboz", "Xbox Series S 512GB + kontroller",
                      "MacBook Pro 14 M1 Pro 16GB 512GB, töltő, doboz", "Asus ROG Strix RTX 3070 8GB, gyári hűtő",
                      "AMD Ryzen 7 5800X3D tálcás, hűtő nélkül", "Intel Core i7-12700K, dobozos, hűtő nem tartozék",
                      "Ryzen 5 5600X Wraith Stealth hűtővel", "AMD Ryzen 5 3600, gyári hűtő",
                      "AirPods Pro 2 USB-C, töltőtokkal", "Dyson V15 Detect akkuval, töltővel",
                      "Makita DHP482 2x5Ah akkuval, töltővel, kofferben", "Lego Technic 42115 Lamborghini, hiánytalan, dobozával",
                      "Lego 75192 Millennium Falcon, összerakva, útmutatóval", "GoPro Hero 9 Black, 3 akkuval",
                      "Steam Deck 512GB, tokkal, dokkolóval", "Sony WH-1000XM4, tokkal, kábellel",
                      "PS4 Pro 1TB, 2 kontroller, 5 játék", "iPhone 11 64GB, kijelzőcsere után, új akku",
                      "iPhone 11 kijelző és akku csere után", "Nintendo Switch V2 + 4 játék + tok",
                      "Nintendo Switch Lite türkiz, töltő nélkül", "iPad Air 4 64GB wifi, tokkal, tollal",
                      "Asus TUF RTX 4070 Ti 12GB, hátlappal", "RTX 3070 vízhűtéssel", "RTX 3070 + vízhűtés blokk",
                      "MSI RTX 3080 Suprim X, gyári hűtővel, dobozában", "iPhone XR 64GB akkumulátor 86%",
                      "Galaxy S20 FE 5G 128GB, tok és fólia", "Lenovo ThinkPad T14 i5 16GB 512GB, töltővel"):
            self.assertEqual(unusable(title), "", title)

    def test_pc_parts_boards_and_lookalike_words_are_products(self):
        """A PC-alkatrész (ház, táp) és a lapkakészletével megnevezett alaplap maga a termék (a „Ryzen 5000 kompatibilis”
        csak a leírása); a Lego „Clone Trooper” egy figura, a „klónozva” lemezmásolás, a bemutató darab eredeti termék, a
        zajos ventilátor a készülék állapota. Az alaplapon a processzor, a memória és a tárhely is ott van: attól még
        alaplap."""
        for title in ("NZXT H510 PC ház", "Fractal Design Meshify C PC ház", "Gamer PC ház RGB ventilátorokkal",
                      "B550 alaplap Ryzen 5000 kompatibilis", "Gigabyte B450M DS3H alaplap AM4 Ryzen",
                      "LEGO Star Wars 75280 501st Legion Clone Troopers", "LEGO 75342 Republic Fighter Tank Clone Wars",
                      "Samsung 870 EVO 1TB SSD klónozva", "iPhone 13 128GB bemutató darab",
                      "iPhone 14 Pro kirakati darab, új állapot", "GTX 1070 ventilátor zajos", "PS4 Pro hangos ventilátor",
                      "Lenovo ThinkPad T480 i5 magyar billentyűzet", "Dell Latitude 5490 i5 FHD kijelző"):
            self.assertEqual(unusable(title), "", title)
        for title, part in (("ThinkPad T490 alaplap i5", "alaplap"), ("MacBook Air M1 alaplap 8GB 256GB", "alaplap"),
                            ("iPhone 13 alaplap 128GB", "alaplap"), ("Dell XPS 15 i7 ventilátor", "ventilátor"),
                            ("AMD Wraith Prism RGB hűtő Ryzen 7", "hűtő")):
            self.assertEqual(unusable(title), f"a termék egy része, nem maga a termék ({part})", title)
        self.assertTrue(matches_query("B550 alaplap", "B550 alaplap Ryzen 5000 kompatibilis"))
        self.assertTrue(matches_query("NZXT H510", "NZXT H510 PC ház"))
        self.assertTrue(matches_query("ThinkPad billentyűzet", "Lenovo ThinkPad T480 billentyűzet"), "épp ezt keresed")
        self.assertFalse(matches_query("Ryzen 5 3600", "Ryzen 5 3600 gyári hűtő"))

    def test_an_accessory_is_the_head_word_not_a_with_form(self):
        """Kiegészítő az, aminek a fejszava a tok, töltő, kábel… (nem a „töltővel”, „töltő nélkül”, „+ tok”)."""
        for title in ("iPhone 13 tok", "iPhone 13 töltő", "Switch OLED dokkoló", "AirPods Pro 2 töltőtok",
                      "Apple Watch Series 7 45mm szíj", "Lego 42115 matrica", "iPhone 13 tokja"):
            self.assertEqual(unusable(title), "kiegészítő, nem maga a termék", title)
        self.assertTrue(matches_query("Nintendo Switch OLED", "Nintendo Switch OLED dokkolóval, töltővel"),
                        "a keresésre a tartozékaival eladott készülék is jó")
        self.assertFalse(matches_query("Nintendo Switch OLED", "Nintendo Switch OLED kijelző"),
                         "a kijelzője nem a konzol")
        self.assertTrue(matches_query("iPhone 13 kijelző", "iPhone 13 kijelző"), "ha épp ezt keresed, jó")

    def test_variants_that_used_to_share_a_key(self):
        """A „+” a Plus változat; a „kontroller nélkül” eladott konzol konzol; a „2 kontrollerrel” darabszáma nem
        típusjel; a szerszám akku nélküli alapgépe más termék, mint a készlet."""
        self.assertEqual(key("Samsung Galaxy S21+ 128GB"), key("Samsung Galaxy S21 Plus 128GB"))
        self.assertNotEqual(key("Samsung Galaxy S21+ 128GB"), key("Samsung Galaxy S21 128GB"))
        self.assertNotEqual(key("Redmi Note 12 Pro+ 256GB"), key("Redmi Note 12 Pro 256GB"))
        self.assertEqual(key("Xbox Series X kontroller nélkül"), key("Xbox Series X"))
        self.assertNotEqual(key("Xbox Series X kontroller nélkül"), key("Xbox Series X kontroller"))
        self.assertEqual(key("PS5 Slim 2 kontrollerrel"), key("PS5 Slim"))
        self.assertNotEqual(key("Makita DHP482 akku nélkül"), key("Makita DHP482"))
        self.assertEqual(key("Makita DHP482 akku és töltő nélkül"), key("Makita DHP482 alapgép"))
        self.assertTrue(usable("Makita DHP482 akku nélkül"), "az alapgép eladható termék, csak más változat")


class RekeyTests(unittest.TestCase):
    def test_an_old_price_book_moves_the_laptop_memory_out_of_the_desktop_prices(self):
        """A régi (4-es kulcsú) árbázisban a cikkszámos laptop-memória az asztaliak között volt: betöltéskor a saját
        kulcsára kerül, a régi árai az asztali memóriák szokásos árából kimaradnak."""
        with tempfile.TemporaryDirectory() as tmp:
            now = 1_800_000_000.0
            samples = [{"p": 11000 + n * 200, "at": now, "last": now, "url": f"https://hardverapro.hu/apro/d{n}/friss.html",
                        "site": "hardverapro", "t": f"Kingston Fury Beast 8GB DDR4 3200 {n}"} for n in range(5)]
            samples += [{"p": 5000 + n * 100, "at": now, "last": now, "url": f"https://hardverapro.hu/apro/l{n}/friss.html",
                         "site": "hardverapro", "t": f"Samsung 8GB DDR4 2666 M471A1K43CB1-CTD {n}"} for n in range(5)]
            Path(tmp, "arbazis.json").write_text(json.dumps({"kv": 4, "keys": {"8gb ddr4": {"name": "ddr4 8gb",
                                                                                            "samples": samples}}}),
                                                 encoding="utf-8")
            book = PriceBook(Path(tmp), lambda: now)
            self.assertEqual(KEY_VERSION, 5)
            self.assertEqual(sorted(book.keys()), ["8gb ddr4", "8gb ddr4 sodimm"])
            self.assertEqual(book.reference("8gb ddr4")["price"], 11400, "csak az asztali memóriák ára")
            self.assertEqual(book.reference("8gb ddr4 sodimm")["price"], 5200)


# Az árbázis: asztali memóriák (Kingston FURY Beast, 11 500–12 500 Ft) és laptop-memóriák (SO-DIMM, 5 300–5 800 Ft), két
# oldalról. A jelölt egy 5 000 Ft-os „Kingston 8GB DDR4 2666MHz”: a címéből nem derül ki, melyik.
DESKTOP = [{"text": f"Kingston Fury Beast 8GB DDR4 3200MHz {n}", "price": p,
            "url": f"https://{site}/apro/beast_{n}/friss.html", "platform": site.split(".")[-2]}
           for n, (p, site) in enumerate(zip((11500, 12000, 12500, 11800, 12200, 11900),
                                             ("hardverapro.hu", "www.jofogas.hu") * 3))]
LAPTOP = [{"text": f"Samsung 8GB DDR4 SO-DIMM laptop RAM {n}", "price": p,
           "url": f"https://{site}/apro/sodimm_{n}/friss.html", "platform": site.split(".")[-2]}
          for n, (p, site) in enumerate(zip((5300, 5500, 5800, 5600, 5400, 5700), ("hardverapro.hu", "www.jofogas.hu") * 3))]


class RespecTests(InstantCase):
    def start(self, prices: list[dict]) -> None:
        self.hunter.set_fast_alert(False)
        self.hunter.set_enabled(True)
        self.hunter.book.add(prices)
        self.watch.add("8GB DDR4", ["hardverapro"])
        self.watch.set_enabled(True)
        self.results([ha_card(1, "Corsair 4GB DDR3 1600", "3 000 Ft")])
        self.watch.tick()                      # az első letöltés csak megjegyzi, ami fent van
        self.later()

    def new(self, title: str, price: str, page: str) -> None:
        self.results([ha_card(7, title, price), ha_card(1, "Corsair 4GB DDR3 1600", "3 000 Ft")])
        self.net.pages["_7/friss"] = page_html(page)
        self.watch.tick()

    def urgent(self) -> list[str]:
        return [title for _kind, title, _text, extra in self.notices if extra.get("urgent")]

    def test_the_description_says_laptop_memory(self):
        """A címből nem derül ki, de a leírás szerint laptop-memória: a laptop-memóriák árához méri (kb. 5 550 Ft), és
        így nem éri meg (eddig az asztaliakéhoz, 12 000 Ft-hoz mérte, és szólt). Előtte nem is pittyent."""
        self.start(DESKTOP + LAPTOP)
        self.new("Kingston 8GB DDR4 2666MHz", "5 000 Ft", "Laptopomból szereltem ki, hibátlanul működik.")
        self.assertEqual((self.urgent(), self.pings), ([], []))
        self.assertEqual(self.hunter.state()["deals"], [])
        self.assertIn("a leírása szerint laptop-memória (SO-DIMM): a hasonlók kb. 5",
                      " ".join(e["text"] for e in self.hunter.journal.entries()))

    def test_the_description_does_not_say_which(self):
        """Ha a leírásból sem derül ki, az olcsóbb fajtához (a laptop-memóriáéhoz) méri: így sem éri meg."""
        self.start(DESKTOP + LAPTOP)
        self.new("Kingston 8GB DDR4 2666MHz", "5 000 Ft", "Hibátlanul működik, tesztelve.")
        self.assertEqual(self.urgent(), [])
        self.assertEqual(self.hunter.state()["deals"], [])

    def test_without_laptop_prices_it_is_only_a_tip(self):
        """Ha a laptop-memória árát még nem ismeri elég biztosan, és a kivitel nem derül ki: nem riaszt, csak az Érdemes
        megnézni listába teszi, az okkal (nézd meg a fotóján)."""
        self.start(DESKTOP)
        self.new("Kingston 8GB DDR4 2666MHz", "5 000 Ft", "Hibátlanul működik, tesztelve.")
        self.assertEqual(self.urgent(), [])
        [tip] = self.hunter.state()["tips"]
        self.assertIn("nem derül ki, hogy laptop- (SO-DIMM) vagy asztali memória-e", tip["why"])

    def test_a_desktop_module_is_still_a_deal(self):
        """Az asztali memória (FURY Beast) az asztaliakhoz mérve: ez tényleg olcsó, szól róla."""
        self.start(DESKTOP + LAPTOP)
        self.new("Kingston Fury Beast 8GB DDR4 2666MHz", "5 000 Ft", "Asztali gépből, hibátlanul működik.")
        self.assertEqual(self.urgent(), ["Megéri: Kingston Fury Beast 8GB DDR4 2666MHz"])
        [deal] = self.hunter.state()["deals"]
        self.assertEqual(deal["median"], 12000)


if __name__ == "__main__":
    unittest.main()


class AuditTests(unittest.TestCase):
    def test_the_saved_hits_are_remeasured_without_touching_the_files(self):
        """python -m jarvis_core.audit: a régi árbázisban talált idegen árak (alkatrész, laptop-memória az asztaliak
        között), és a régi találatok az új szabályokkal: a laptop-memória a laptop-memóriákhoz mérve már nem éri meg, a
        processzor gyári hűtője nem processzor, a laptop-memória nem illik egy asztali memória keresésre; az asztali
        memória marad. A munka mappa fájljai nem változnak."""
        from jarvis_core.audit import audit, report
        from jarvis_core.selling import listing_key

        now = 1_800_000_000.0
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            desktop = [{"p": p, "at": now, "last": now, "url": f"https://{site}/apro/beast_{n}/friss.html",
                        "site": site.split(".")[-2], "t": f"Kingston Fury Beast 8GB DDR4 3200MHz {n}"}
                       for n, (p, site) in enumerate(zip((11500, 12000, 12500, 11800, 12200, 11900),
                                                         ("hardverapro.hu", "www.jofogas.hu") * 3))]
            laptop = [{"p": p, "at": now, "last": now, "url": f"https://{site}/apro/m471_{n}/friss.html",
                       "site": site.split(".")[-2], "t": f"Samsung 8GB DDR4 2666 M471A1K43CB1-CTD {n}"}
                      for n, (p, site) in enumerate(zip((5300, 5500, 5800, 5600, 5400, 5700),
                                                        ("hardverapro.hu", "www.jofogas.hu") * 3))]
            cpu = [{"p": 30000 + n * 500, "at": now, "last": now, "url": f"https://hardverapro.hu/apro/r3600_{n}/friss.html",
                    "site": "hardverapro", "t": f"AMD Ryzen 5 3600 {n}"} for n in range(5)]
            cpu.append({"p": 2000, "at": now, "last": now, "url": "https://hardverapro.hu/apro/huto/friss.html",
                        "site": "hardverapro", "t": "AMD Ryzen 5 3600 gyári hűtő"})
            book = {"kv": 4, "keys": {"8gb ddr4": {"name": "ddr4 8gb", "samples": desktop + laptop},
                                      "3600 ryzen": {"name": "ryzen 3600", "samples": cpu}}}
            vetted = {listing_key(s["url"]): {"at": now, "issues": [], "state": "hibátlan"}
                      for s in desktop + laptop + cpu}

            def hit(n: int, title: str, price: int, median: int, **extra) -> dict:
                return {"id": f"d{n}", "url": f"https://hardverapro.hu/apro/hit_{n}/friss.html", "title": title,
                        "text": title, "price": price, "median": median, "platform": "hardverapro", "site": "Hardverapró",
                        "search": "azonnali", "query": title, "at": now, **extra}

            deals = {"deals": [hit(1, "Kingston 8GB DDR4 2666MHz", 5000, 12000, product_key="8gb ddr4"),
                               hit(2, "AMD Ryzen 5 3600 gyári hűtő", 2000, 30000),
                               hit(3, "Kingston Fury Beast 8GB DDR4 2666MHz", 5000, 12000, product_key="8gb ddr4"),
                               hit(4, "Samsung 16GB DDR4 3200 M471A2K43DB1-CWE", 9000, 25000, query="16GB DDR4",
                                   search="s1", basis={"given": True, "used": []})]}
            files = {"arbazis.json": book, "allapotok.json": vetted, "kereskedes.json": deals}
            for name, data in files.items():
                Path(root, name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            before = {name: Path(root, name).read_bytes() for name in files}

            result = audit(root, clock=lambda: now)
            self.assertEqual({name: Path(root, name).read_bytes() for name in files}, before, "csak olvas")
            self.assertEqual(result["book"]["not_product"], {"a termék egy része, nem maga a termék": 1})
            self.assertEqual(result["book"]["laptop_moved"], 6)
            verdicts = {item["title"]: item["verdict"] for item in result["items"]}
            self.assertEqual(verdicts, {"Kingston 8GB DDR4 2666MHz": "nem érné meg",
                                        "AMD Ryzen 5 3600 gyári hűtő": "kiesne",
                                        "Kingston Fury Beast 8GB DDR4 2666MHz": "marad",
                                        "Samsung 16GB DDR4 3200 M471A2K43DB1-CWE": "kiesne"})
            text = report(result)
            self.assertIn("1 ár nem a termék ára", text)
            self.assertIn("6 laptop-memória (SO-DIMM) ára volt az asztali memóriák között", text)
            self.assertIn("„Kingston 8GB DDR4 2666MHz” 5 000 Ft", text)
            self.assertIn("a keresésedhez („16GB DDR4”) már nem illik", text)


class RespecUnitTests(unittest.TestCase):
    """Az adatlap leírása után (_verdict → _respec) a jelölt a teljes termékadataival: ami a címből nem derült ki."""

    def setUp(self):
        from test_deals import Clock
        from test_instant import AllVetted
        from jarvis_core.deals import DealHunter
        from jarvis_core.selling import PLATFORMS
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.hunter = DealHunter(Path(self.tmp.name), PLATFORMS, None, None, clock=self.clock)
        self.hunter._vetted = AllVetted()

    def tearDown(self):
        self.tmp.cleanup()

    def deal(self, title: str, price: int, prices: list[tuple[str, int]], details: str) -> dict:
        self.hunter.book.add([{"text": text, "price": p, "url": f"https://{site}/apro/c{n}/friss.html",
                               "platform": site.split(".")[-2]}
                              for n, ((text, p), site) in enumerate(zip(prices, ("hardverapro.hu", "www.jofogas.hu") * 9))])
        row = {"title": title, "text": title, "price": price, "url": "https://hardverapro.hu/apro/jelolt/friss.html",
               "platform": "hardverapro", "site": "Hardverapró"}
        key = self.hunter.book.key(title)[0]
        reference = self.hunter._screened(self.hunter.book.reference(key, exclude=row["url"]), self.hunter._want(row))
        deal = self.hunter._explore_deal({"id": "w", "name": "figyelt oldal", "min_profit": 5000}, row,
                                         {**reference, "name": title})
        self.assertIsNotNone(deal, "a címe alapján megérne")
        return {**deal, "page_checked": True, "details": details}

    def test_a_locked_phone_is_not_a_deal(self):
        """A címe szerint egy olcsó iPhone 13, a leírása szerint iCloud-zárolt: kiesik (nem csak bizonytalan)."""
        phones = [(f"iPhone 13 128GB {n}", p) for n, p in enumerate((210000, 205000, 215000, 208000, 212000, 206000))]
        deal = self.deal("iPhone 13 128GB", 120000, phones, "Sajnos iCloud zárolt, nem tudom kijelentkeztetni. Működik.")
        state, why, _hold = self.hunter._verdict(deal, True)
        self.assertEqual((state, why), ("hibás", "a leírása szerint zárolt („iCloud”)"))

    def test_a_dummy_is_not_a_deal(self):
        """A leírása szerint kirakati dummy (nem működő bemutatódarab): kiesik."""
        phones = [(f"iPhone 13 Pro 128GB {n}", p) for n, p in enumerate((260000, 255000, 265000, 258000, 262000, 256000))]
        deal = self.deal("iPhone 13 Pro 128GB", 90000, phones, "Kirakati dummy telefon, dekorációnak.")
        self.assertEqual(self.hunter._verdict(deal, True)[:2], ("hibás", "a leírása szerint nem eredeti („dummy”)"))

    def test_a_cut_down_graphics_card_is_measured_to_its_own_kind(self):
        """A címe szerint RX 580 8GB, a leírása szerint a csonkított 2048SP változat: a teljes RX 580-ak ára nem mérce,
        és ha a 2048SP-k árát nem ismeri elég biztosan, csak bizonytalan (nem riaszt)."""
        cards = [(f"RX 580 8GB Nitro {n}", p) for n, p in enumerate((32000, 31000, 33000, 31500, 32500, 30500))]
        deal = self.deal("Sapphire RX 580 8GB", 18000, cards, "RX 580 2048SP változat, hibátlanul működik.")
        state, why, _hold = self.hunter._verdict(deal, True)
        self.assertEqual(state, "ellenőrizetlen")
        self.assertIn("csonkított változat (2048SP)", why)

    def test_nothing_new_in_the_description_keeps_the_deal(self):
        """Ha a leírásból nem derül ki semmi új (ami az árat érintené), a jelölt marad, ahogy volt."""
        cards = [(f"RX 580 8GB Nitro {n}", p) for n, p in enumerate((32000, 31000, 33000, 31500, 32500, 30500))]
        deal = self.deal("Sapphire RX 580 8GB", 18000, cards, "Hibátlanul működik, dobozában, számlával.")
        median = deal["median"]
        self.assertEqual(self.hunter._verdict(deal, True)[0], "ellenőrzött")
        self.assertEqual(deal["median"], median)
