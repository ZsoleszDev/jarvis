"""Termékazonosítás és összehasonlíthatóság (pricebook.py, specs.py, deals.py): egy olcsónak látszó hirdetés ne legyen
egy értékesebb változathoz (más memória, más kiszerelés, más konzolváltozat) mérve, mert az túlbecsüli a hasznot; a jó,
jól leírt találat viszont továbbra is átmenjen.

Futtatás:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_deals import Clock, HunterCase  # noqa: E402

from jarvis_core import specs  # noqa: E402
from jarvis_core.deals import careful_low, safe_sale  # noqa: E402
from jarvis_core.pricebook import MIN_SAMPLES, SELLER_CAP, PriceBook, product_key, sizes_of  # noqa: E402


def key(title: str) -> str:
    found = product_key(title)
    return found[0] if found else ""


class ProductKeyTests(unittest.TestCase):
    def test_rx_580_4g_is_not_the_8gb_card(self):
        """A „Sapphire RX580 4G Nitro” 4 GB-os: a „4G” a videókártyán a memória (VRAM), így nem a 8 GB-osokkal és nem
        a kapacitás nélküliekkel van egy kulcson; a GTX 1060 3 és 6 GB-os változata is külön termék."""
        self.assertEqual(key("Sapphire RX580 4G Nitro"), "4gb 580 rx")
        self.assertEqual(key("Sapphire RX 580 Nitro+ 4GB"), "4gb 580 rx")
        self.assertEqual(key("Sapphire RX 580 Nitro+ 4 GB"), "4gb 580 rx")
        self.assertEqual(key("Sapphire RX 580 Nitro+ 8GB"), "580 8gb rx")
        self.assertEqual(key("Sapphire RX 580 Nitro+"), "580 rx", "a kapacitás nélküli külön (nem tudni, melyik)")
        self.assertNotEqual(key("Gigabyte GTX 1060 3G"), key("MSI GTX 1060 6G Gaming"))
        self.assertEqual(sizes_of("RX 580 8G"), {"8gb"})

    def test_4g_on_a_phone_is_the_network(self):
        """Telefonnál a „4G”, „5G” a mobilhálózat, nem a tárhely vagy a memória."""
        self.assertEqual(key("Samsung Galaxy A12 4G 64GB"), "64gb a12 galaxy")
        self.assertEqual(key("Xiaomi Redmi Note 10 5G 128GB"), "10 128gb note redmi")
        self.assertEqual(sizes_of("Samsung Galaxy A12 4G 64GB"), {"64gb"})

    def test_ram_kit_layout_generation_and_form(self):
        """Memória: a 2×4 GB összesen 8 GB, de nem ugyanaz a kiszerelés, mint az 1×8 GB (az „1x8GB” és a „8GB” egy
        modul); a 2×8 GB összesen 16 GB, és nem egy 16 GB-os modul; a „16GB kit” sem az. A DDR3 és a DDR4 (a „DDR 3”,
        „PC4-21300” alakban is), a DIMM és a SO-DIMM (laptop-memória), és a szerver-memória (ECC, REG) külön termék."""
        self.assertEqual(key("Kingston 2x4GB DDR4 2666"), "2666 2x4gb ddr4 kingston")
        self.assertEqual(key("Kingston 1x8GB DDR4 2666"), key("Kingston 8GB DDR4 2666"))
        self.assertNotEqual(key("Kingston 2x4GB DDR4 2666"), key("Kingston 8GB DDR4 2666"))
        self.assertEqual(key("Corsair Vengeance 16GB (2x8GB) DDR4 3200"), "2x8gb 3200 corsair ddr4", "az összeg nem külön")
        self.assertNotEqual(key("Corsair Vengeance 2x8GB DDR4 3200"), key("Corsair Vengeance 16GB DDR4 3200"))
        self.assertNotEqual(key("Corsair Vengeance 16GB kit DDR4 3200"), key("Corsair Vengeance 16GB DDR4 3200"))
        self.assertNotEqual(key("Corsair 8GB DDR3 1600"), key("Corsair 8GB DDR4 1600"))
        self.assertEqual(key("Corsair 8GB DDR 3 1600"), key("Corsair 8GB DDR3 1600"))
        self.assertEqual(key("Kingston 8GB PC4-21300 laptop memória"), "8gb ddr4 kingston sodimm")
        self.assertEqual(key("Kingston 8GB DDR4 SO-DIMM"), key("Kingston 8GB DDR4 SO DIMM"))
        self.assertNotEqual(key("Kingston 8GB DDR4 SO-DIMM"), key("Kingston 8GB DDR4"))
        self.assertNotEqual(key("Samsung 16GB DDR4 ECC REG 2400"), key("Samsung 16GB DDR4 2400"))
        self.assertIn("laptop", key("Lenovo ThinkPad T480 8GB DDR4 256GB SSD laptop").split(),
                      "egy laptop a memóriájával laptop marad, nem SO-DIMM")

    def test_switch_lite_and_oled_are_separate_products(self):
        """A Switch, a Switch Lite és a Switch OLED külön termék (más ár); a sima Switch a címből (változat nélkül) nem
        kap kulcsot, így nem keveredik a Lite és az OLED áraival."""
        self.assertEqual(key("Nintendo Switch Lite szürke"), "lite switch")
        self.assertEqual(key("Nintendo Switch OLED fehér"), "oled switch")
        self.assertEqual(key("Nintendo Switch"), "")
        self.assertEqual(key("Nintendo Switch V2 konzol"), "switch v2")


class PriceBookVariantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.book = PriceBook(Path(self.tmp.name), clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, title: str, prices: list[int], tag: str) -> None:
        self.book.add([{"text": title, "price": p, "platform": "hardverapro" if n % 2 else "jofogas",
                        "url": f"https://hardverapro.hu/apro/{tag}_{n}/friss.html"} for n, p in enumerate(prices)])

    def test_a_4gb_card_gets_only_4gb_prices(self):
        """A 4 GB-os RX 580 szokásos ára csak a 4 GB-osokból jön; a 8 GB-osok (31 000 Ft körül) nem emelik."""
        self.add("Sapphire RX 580 Nitro+ 8GB", [30000, 31000, 31500, 32000, 30500, 31000], "r8")
        self.add("Sapphire RX580 4G Nitro", [20000, 21000, 20500, 21500, 22000], "r4")
        self.assertEqual(self.book.reference("4gb 580 rx")["price"], 21000)
        self.assertEqual(self.book.reference("580 8gb rx")["price"], 31000)
        self.assertEqual(self.book.reference(key("PowerColor RX 580 4GB"))["price"], 21000, "más gyártó, ugyanaz a változat")

    def test_ram_layouts_do_not_share_a_price(self):
        """A 2×4 GB-os készlet és az 1×8 GB-os modul ára külön: egy 2×4-es készlet nem látszik olcsónak a 8 GB-os
        modulokhoz mérve."""
        self.add("Kingston 8GB DDR4 2666", [9000, 9500, 10000, 9800, 9200], "k8")
        self.add("Kingston 2x4GB DDR4 2666", [7000, 7200, 6800, 7500, 7100], "k24")
        self.assertEqual(self.book.reference(key("Kingston 1x8GB DDR4 2666"))["price"], 9500)
        self.assertEqual(self.book.reference(key("Kingston 2x4GB DDR4 2666"))["price"], 7100)


def compare(want: str, have: str) -> tuple[str, list[str]]:
    return specs.compare(specs.extract(want), specs.extract(have))


class SpecsTests(unittest.TestCase):
    """A termékadatok kategóriánként (specs.extract): az érték, a forrás (cím, leírás, típus) és a szó szerinti idézet;
    a hiányzó adat hiányzó marad. Az összehasonlítás osztálya (specs.compare): pontos, közelítő (az eltéréssel),
    kizárt (kritikus eltérés: ezt egy jó szöveges hasonlóság sem írja felül), bizonytalan (egy fontos adat nem derül
    ki)."""

    def test_the_gpu_memory_decides(self):
        """Az RX 580 4 GB-os és 8 GB-os változata soha nincs egy biztos referenciában; más gyártó ugyanazzal a
        memóriával közelítő (beszámít, jelölve). Ha a vizsgáltból nem derül ki a memória, az nem kitalált adat:
        hiányzik, és az ára nem lehet biztos. A GTX 1080 csak 8 GB-os: ez a típusból jön (forrás: típus)."""
        card = specs.extract("Sapphire RX580 4G Nitro")
        self.assertEqual((card["category"], card["attrs"]["vram"]), ("gpu", {"value": "4gb", "source": "cím",
                                                                             "evidence": "4G"}))
        self.assertEqual(compare("Sapphire RX580 4G Nitro", "Sapphire RX 580 Nitro+ 8GB"),
                         (specs.KIZART, ["más memória (VRAM) (8GB, ez 4GB)"]))
        self.assertEqual(compare("Sapphire RX580 4G Nitro", "PowerColor RX 580 4GB Red Dragon"),
                         (specs.KOZELITO, ["más gyártó (powercolor, ez sapphire)"]))
        unknown = specs.extract("Sapphire RX 580 Nitro")
        self.assertNotIn("vram", unknown["attrs"])
        self.assertEqual((unknown["missing"], specs.unsure_variant(unknown)), (["vram"], ["memória (VRAM)"]))
        self.assertEqual(compare("Sapphire RX 580 Nitro", "PowerColor RX 580 8GB")[0], specs.KOZELITO)
        self.assertEqual(specs.extract("MSI GTX 1080 Gaming X")["attrs"]["vram"]["source"], "típus")

    def test_ram_generation_form_layout_and_total(self):
        """Memória: a DDR-generáció, az összkapacitás, a kiszerelés (modulszám × modulméret), a kivitel (DIMM vagy
        SO-DIMM), a szerver-memória (ECC, REG) és a sebesség. A 2×4 GB összesen 8 GB, de más kiszerelés, mint az 1×8 GB;
        a 2×8 GB összesen 16 GB, és egy „16GB” lehet egy modul is (bizonytalan); a DDR3 és a DDR4, a DIMM és a
        SO-DIMM, a szerver- és az asztali memória soha nem keveredik; csak a sebesség más: közelítő."""
        kit = specs.extract("Corsair Vengeance 16GB (2x8GB) DDR 4 3200")
        self.assertEqual({name: (a["value"], a["evidence"]) for name, a in kit["attrs"].items() if name != "maker"},
                         {"generation": ("ddr4", "DDR 4"), "layout": ("2x8gb", "2x8GB"), "total": ("16gb", "16GB"),
                          "speed": ("3200", "3200")})
        self.assertEqual(specs.extract("Kingston 2x4GB DDR4")["attrs"]["total"]["value"], "8gb")
        self.assertEqual(compare("Kingston 2x4GB DDR4 2666", "Kingston 1x8GB DDR4 2666"),
                         (specs.KIZART, ["más kiszerelés (1X8GB, ez 2X4GB)"]))
        self.assertEqual(compare("Kingston 2x8GB DDR4 3200", "Corsair 16GB DDR4 3200")[0], specs.BIZONYTALAN)
        self.assertEqual(compare("Kingston 8GB DDR3 1600", "Kingston 8GB DDR4 1600")[0], specs.KIZART)
        self.assertEqual(compare("Kingston 8GB DDR4 SO-DIMM 2666", "Kingston 8GB DDR4 2666 asztali"),
                         (specs.KIZART, ["más kivitel (dimm, ez sodimm)"]))
        self.assertEqual(compare("Kingston 8GB DDR4 SO-DIMM 2666", "Kingston 8GB DDR4 2666")[0], specs.BIZONYTALAN,
                         "a kivitel nem derül ki: lehet asztali")
        self.assertEqual(compare("Samsung 16GB DDR4 2400", "Samsung 16GB DDR4 ECC REG 2400")[0], specs.KIZART)
        self.assertEqual(compare("Kingston 8GB DDR4 SO-DIMM 2666", "Kingston 8GB DDR4 SO-DIMM 3200"),
                         (specs.KOZELITO, ["más sebesség (3200, ez 2666)"]))
        laptop = specs.extract("Kingston 8GB PC4-21300 laptop memória")["attrs"]
        self.assertEqual((laptop["generation"]["evidence"], laptop["form"]["evidence"]), ("PC4-21300", "laptop memória"))

    def test_console_editions_and_extras(self):
        """A Switch, a Switch Lite és a Switch OLED külön termék; a plusz játékkal, kontrollerrel árult konzol nem
        mérce a csupasz konzolnak (fordítva közelítő: a csomagnak kevesebb tartozékú a mérce); egy játék vagy egy
        kontroller nem konzol."""
        self.assertEqual(compare("Nintendo Switch Lite szürke", "Nintendo Switch OLED fehér")[0], specs.KIZART)
        self.assertEqual(compare("Nintendo Switch", "Nintendo Switch OLED")[0], specs.KIZART)
        self.assertEqual(compare("Nintendo Switch Lite szürke", "Nintendo Switch Lite + 2 játék")[0], specs.KIZART)
        self.assertEqual(compare("Nintendo Switch Lite + 2 játék", "Nintendo Switch Lite kék"),
                         (specs.KOZELITO, ["kevesebb tartozékkal, mint ez"]))
        self.assertEqual(compare("Nintendo Switch Lite szürke", "Nintendo Switch Lite kék"), (specs.PONTOS, []))
        self.assertEqual([specs.category(t) for t in ("Nintendo Switch OLED konzol", "Switch Pro kontroller",
                                                      "FIFA 23 Nintendo Switch játék")], ["console", "", ""])

    def test_phone_storage_lock_and_network(self):
        """Telefon: a tárhely kritikus, a zárolt (iCloud) telefon nem mérce (a tagadott zár, „iCloud mentes”, nem
        zár), a hálózatfüggés közelítő; a „4G” a hálózat, nem a tárhely."""
        self.assertEqual(compare("iPhone 12 128GB", "iPhone 12 64GB")[0], specs.KIZART)
        self.assertEqual(compare("iPhone 12 128GB", "iPhone 12 128GB iCloud zárolt")[0], specs.KIZART)
        self.assertEqual(compare("iPhone 12 128GB", "iPhone 12 128GB iCloud mentes")[0], specs.PONTOS)
        self.assertEqual(compare("iPhone 12 128GB független", "iPhone 12 128GB Telekom függő")[0], specs.KOZELITO)
        a12 = specs.extract("Samsung Galaxy A12 4G 64 GB")["attrs"]
        self.assertEqual((a12["storage"]["value"], a12["storage"]["evidence"]), ("64gb", "64 GB"))

    def test_ssd_capacity_interface_and_wear(self):
        """SSD: a kapacitás, a típus és a csatoló kritikus; a csatoló a típusból is kiderülhet (a 970 EVO Plus csak
        NVMe: forrás „típus”, nem kitalált), az elhasználtság (Health, írt TB) a leírásból, idézettel."""
        self.assertEqual(compare("Samsung 970 EVO Plus 1TB NVMe SSD", "Samsung 970 EVO Plus 500GB NVMe SSD")[0],
                         specs.KIZART)
        self.assertEqual(compare("Crucial MX500 1TB SATA SSD", "Samsung 970 EVO Plus 1TB NVMe SSD")[0], specs.KIZART)
        plain = specs.extract("Samsung 970 EVO Plus 1TB")["attrs"]["interface"]
        self.assertEqual((plain["value"], plain["source"]), ("nvme", "típus"))
        self.assertEqual(compare("Samsung 970 EVO Plus 1TB NVMe SSD", "Samsung 970 EVO Plus 1TB"), (specs.PONTOS, []))
        worn = specs.extract("Samsung 970 EVO Plus 1TB NVMe SSD", "Health 98%, 12 TB írva, hibátlan")["attrs"]
        self.assertEqual((worn["health"]["value"], worn["health"]["source"]), ("98%", "leírás"))
        self.assertEqual(worn["written"]["source"], "leírás")
        self.assertEqual(compare("Samsung 970 EVO Plus NVMe SSD", "Samsung 970 EVO Plus 1TB NVMe SSD")[0],
                         specs.KOZELITO, "a vizsgáltnál nem derül ki a kapacitás: a mérce az ismert változat, jelölve")

    def test_the_model_cannot_add_a_capacity(self):
        """A modell megnevezéséből kiesik az a kapacitás, ami a hirdetés szövegében nincs benne (nem találhatja ki); ami
        benne van (bármilyen alakban), marad."""
        self.assertEqual(specs.grounded("Sapphire RX 580 8GB", "Sapphire RX 580 Nitro, jól működik"), "Sapphire RX 580")
        self.assertEqual(specs.grounded("Sapphire RX 580 8GB", "Sapphire RX 580 Nitro 8 GB"), "Sapphire RX 580 8GB")
        self.assertIn("a címből: „4G”", specs.describe(specs.extract("Sapphire RX580 4G Nitro")))


class IndependentPriceTests(unittest.TestCase):
    """Egy termék szokásos ára független árakból: ugyanaz a hirdetés két oldalon egyszer számít, egy eladótól legfeljebb
    SELLER_CAP; így egy kereskedő sok hasonló hirdetése nem ad hamis biztonságot."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.book = PriceBook(Path(self.tmp.name), clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_listing_posted_on_two_sites_counts_once(self):
        self.book.add([{"text": f"Nintendo Switch Lite szürke {n}", "price": p, "platform": "hardverapro",
                        "url": f"https://hardverapro.hu/apro/lite_{n}/friss.html"}
                       for n, p in enumerate((58000, 59000, 60000, 61000, 62000))])
        self.book.add([{"text": "Nintendo Switch Lite szürke 2", "price": 60000, "platform": "jofogas",
                        "url": "https://www.jofogas.hu/budapest/lite_2.htm"}])
        found = self.book.reference(self.book.key("Nintendo Switch Lite")[0])
        self.assertEqual(found["n"], 5, "a két oldalon fent lévő egy hirdetés egyszer számít")
        self.assertEqual([e["why"] for e in found["basis"]["out"]],
                         ["valószínűleg ugyanaz a hirdetés, máshol is fent van (egyszer számít)"])

    def test_many_listings_of_one_seller_do_not_make_a_sure_price(self):
        self.book.add([{"text": f"Sapphire RX 580 8GB {n}", "price": 30000 + n * 100, "platform": "hardverapro",
                        "url": f"https://hardverapro.hu/apro/r8_{n}/friss.html", "seller": "gpubolt"} for n in range(6)]
                      + [{"text": f"PowerColor RX 580 8GB {n}", "price": 31000 + n * 500, "platform": "hardverapro",
                          "url": f"https://hardverapro.hu/apro/p8_{n}/friss.html", "seller": f"magan{n}"}
                         for n in range(2)])
        key = self.book.key("RX 580 8GB")[0]
        self.assertLess(SELLER_CAP + 2, MIN_SAMPLES)
        self.assertIsNone(self.book.reference(key), "8 ár, de csak 4 független (az eladótól legfeljebb 2): nem elég")
        self.book.add([{"text": f"XFX RX 580 8GB {n}", "price": 30500 + n * 300, "platform": "jofogas",
                        "url": f"https://www.jofogas.hu/x/x8_{n}.htm", "seller": f"masik{n}"} for n in range(2)])
        found = self.book.reference(key)
        self.assertEqual(found["n"], 6)
        out = [e for e in found["basis"]["out"] if "eladótól" in e["why"]]
        self.assertEqual(len(out), 4)
        self.assertIn("ugyanattól az eladótól (gpubolt): legfeljebb 2 hirdetése számít", out[0]["why"])


class ComparableTests(HunterCase):
    """Az összehasonlítók a vizsgált hirdetéshez (DealHunter): a vizsgált hirdetés máshol fent lévő példánya nem a saját
    mércéje; a kilógóan olcsó hasonló nem esik ki szó nélkül (miért olcsó?), és ha ugyanilyen, hibátlan, és napok óta
    fent van, lejjebb viszi a gyors eladási becslést."""

    LITE = [(58000, "hardverapro"), (59000, "jofogas"), (60000, "hardverapro"), (61000, "jofogas"),
            (62000, "hardverapro"), (60500, "jofogas")]
    CHEAP = {"text": "Nintendo Switch Lite kék, hibátlan", "price": 35000, "platform": "jofogas",
             "url": "https://www.jofogas.hu/b/olcso_lite.htm"}
    MINE = {"title": "Nintendo Switch Lite szürke", "text": "Nintendo Switch Lite szürke", "price": 40000,
            "platform": "hardverapro", "url": "https://hardverapro.hu/apro/enyem/friss.html"}

    def lite(self) -> list[dict]:
        rows = [{"text": f"Nintendo Switch Lite szürke {n}", "price": p, "platform": site,
                 "url": f"https://hardverapro.hu/apro/lite_{n}/friss.html"} for n, (p, site) in enumerate(self.LITE)]
        self.hunter.book.add(rows)
        self.vetted(self.hunter, rows)
        return rows

    def screened(self) -> dict:
        key = self.hunter.book.key("Nintendo Switch Lite")[0]
        return self.hunter._screened(self.hunter.book.reference(key, exclude=self.MINE["url"]),
                                     self.hunter._want(self.MINE))

    def test_the_candidates_own_copy_elsewhere_is_not_its_comparable(self):
        """Ugyanaz a cím és az ár egy másik oldalon (vagy ugyanaz a cím ugyanattól az eladótól): az maga a vizsgált
        hirdetés, nem a mércéje; a különböző hirdetések maradnak."""
        want = self.hunter._want({"title": "Sapphire RX 580 Nitro+ 8GB", "price": 30000, "seller": "kisferi"})
        samples = [{"t": "Sapphire RX 580 Nitro+ 8GB", "p": 30000, "s": "jofogas", "u": "https://www.jofogas.hu/a/1.htm"},
                   {"t": "Sapphire RX 580 Nitro+ 8GB", "p": 32000, "s": "hardverapro", "v": "kisferi",
                    "u": "https://hardverapro.hu/apro/2/friss.html"},
                   {"t": "PowerColor RX 580 8GB", "p": 31000, "s": "hardverapro",
                    "u": "https://hardverapro.hu/apro/3/friss.html"}]
        kept, out = self.hunter._comparables(want, samples)
        self.assertEqual([s["t"] for s in kept], ["PowerColor RX 580 8GB"])
        self.assertEqual(kept[0]["approx"], "más gyártó (powercolor, ez sapphire)")
        self.assertEqual([s["why"] for s in out], ["valószínűleg maga a vizsgált hirdetés (máshol is fent van)"] * 2)

    def test_a_cheap_clean_listing_that_stays_lowers_the_quick_sale(self):
        """A 35 000 Ft-os Switch Lite a 60 000 körüliek mellett kilógóan olcsó: a szokásos árba nem számít, de a
        leírása szerint hibátlan, ugyanilyen, és három napja fent van (ennyiért sem kelt el): versenytárs, a gyors
        eladási becslés a kettő közti középár; a találat ablakában ott az oka."""
        self.lite()
        self.clock.now -= 3 * 86400
        self.hunter.book.add([self.CHEAP])
        self.clock.now += 3 * 86400
        self.hunter.book.add([self.CHEAP])     # ma is látta: még fent van
        self.vetted(self.hunter, [self.CHEAP])
        found = self.screened()
        self.assertEqual((found["price"], found["n"]), (60200, 6), "a szokásos ár a hat hasonlóból (az olcsó nélkül)")
        self.assertEqual(found["cheap"]["p"], 35000)
        self.assertEqual(careful_low(found), round((35000 + found["p25"]) / 2))
        self.assertLess(safe_sale(found["price"], careful_low(found)), safe_sale(found["price"], found["p25"]))
        [entry] = [e for e in found["basis"]["out"] if e["u"] == self.CHEAP["url"]]
        self.assertEqual(entry["why"], "olcsó, de a leírása szerint ugyanilyen és hibátlan, és 3 napja fent van (ennyiért "
                                       "sem kelt el): versenytárs, a gyors eladási árat lejjebb vettem")

    def test_a_fresh_or_unread_cheap_listing_does_not_set_the_price(self):
        """Ha a kilógóan olcsó hasonló friss (ma jelent meg: valószínűleg hamar elkel, kivételes ajánlat), vagy nem
        tudni, miért olcsó (nincs elolvasva), nem viszi lejjebb a becslést, de a találatnál látszik, miért maradt ki."""
        self.lite()
        self.hunter.book.add([self.CHEAP])
        self.vetted(self.hunter, [self.CHEAP])
        found = self.screened()
        self.assertNotIn("cheap", found)
        self.assertEqual(careful_low(found), found["p25"])
        [entry] = [e for e in found["basis"]["out"] if e["u"] == self.CHEAP["url"]]
        self.assertIn("friss hirdetés (hamar elkelhet, nem a piaci ár)", entry["why"])
        self.hunter._vetted.clear()
        self.clock.now += 3 * 86400
        self.hunter.book.add([self.CHEAP] + [{"text": f"Nintendo Switch Lite szürke {n}", "price": p, "platform": site,
                                              "url": f"https://hardverapro.hu/apro/lite_{n}/friss.html"}
                                             for n, (p, site) in enumerate(self.LITE)])
        found = self.screened()
        self.assertNotIn("cheap", found)
        [entry] = [e for e in found["basis"]["out"] if e["u"] == self.CHEAP["url"]]
        self.assertIn("kilógóan olcsó, nem derül ki, miért", entry["why"])

    def test_a_cheap_competitor_found_now_counts_in_the_instant_check_too(self):
        """Az azonnali ellenőrzésnél (most talált hirdetések, _market_of) ugyanígy: a most is fent lévő, három napja
        látott, hibátlan olcsó hasonló versenytárs."""
        rows = self.lite()
        self.clock.now -= 3 * 86400
        self.hunter.book.add([self.CHEAP])
        self.clock.now += 3 * 86400
        self.vetted(self.hunter, [self.CHEAP])
        fresh = [{**row, "title": row["text"]} for row in rows[:2]] + [{**self.CHEAP, "title": self.CHEAP["text"]}]
        found = self.hunter._market_of(self.MINE, "Nintendo Switch Lite", fresh)
        self.assertEqual(found["cheap"]["p"], 35000)
        [entry] = [e for e in found["basis"]["out"] if e["u"] == self.CHEAP["url"]]
        self.assertIn("versenytárs", entry["why"])
        self.assertEqual(entry["cheap"], "versenytárs")


if __name__ == "__main__":
    unittest.main()
