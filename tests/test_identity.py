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

from test_deals import Clock  # noqa: E402

from jarvis_core.pricebook import PriceBook, product_key, sizes_of  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
