"""Az összehasonlító árak pontossága és frissessége: hogy egy hirdetés mellé tényleg a többi ugyanilyen hirdetés ára
kerüljön (és ne kevesebb, ne más termék), az ár pontosan legyen kiolvasva, és a mostani árak számítsanak, ne a régiek.

- A hirdetés azonosítója az oldal saját száma, nem a címében álló szám: két „i5-12400F” vagy két „Lego 75192” hirdetés
  nem esik egybe (különben az árbázisban egynek az ára maradna, és az új hirdetés „már látottnak” tűnne).
- A kártya egy szomszédos száma (a fotók száma, a cím végén álló típusszám) nem olvad az ezres tagolású árba.
- Ugyanannak a terméknek egy kulcsa van, akárhogy írják: „RTX 3060Ti”, „RTX3060TI”, „MSI 3060 Ti”; „iPhone 14 ProMax”;
  „Samsung S23 Ultra”; a memória a gyártója, sebessége és időzítése nélkül („DDR4-3200”, „2 x 8GB”, „8GB + 8GB”).
- A régi árak magukban nem elegendők: előbb a mostani árak (a többi oldalon is, a legújabb hirdetésekkel elöl), és ha az
  árszint lejjebb ment, az új számít (így egy régi, magasabb árhoz mérve nem látszik olcsónak, ami ma már nem az).

Futtatás a projekt mappájából:
    python -m unittest tests.test_comparison -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_deals import Clock, FakeBrowser, FakeLlm  # noqa: E402
from test_instant import AllVetted, InstantCase, ha_card, jf_card, page_html  # noqa: E402
from test_web_browser import HAS_CHROMIUM  # noqa: E402

from jarvis_core import specs  # noqa: E402
from jarvis_core.deals import DealHunter, FRESH_DAYS, matches_query  # noqa: E402
from jarvis_core.feeds import newest_first, parse_listings  # noqa: E402
from jarvis_core.pricebook import PriceBook, product_key, usable  # noqa: E402
from jarvis_core.selling import PLATFORMS, ItemStore, PriceResearcher, listing_key  # noqa: E402

DAY = 86400


def key(title: str) -> str:
    found = product_key(title)
    return found[0] if found else ""


class ListingIdTests(unittest.TestCase):
    def test_a_number_in_the_title_is_not_the_listing_id(self):
        """A Hardveraprón a webcím a hirdetés címéből van (szám nélkül), a Jófogáson és a Vaterán a hirdetés száma a
        végén, a Vinteden az /items/ után: a címben álló „12400”, „75192” nem azonosító."""
        ha = ["https://hardverapro.hu/apro/intel_core_i5_12400f/friss.html",
              "https://hardverapro.hu/apro/i5_12400f_talcas_garanciaval/friss.html"]
        self.assertNotEqual(listing_key(ha[0]), listing_key(ha[1]))
        self.assertEqual(listing_key(ha[0]), "hardverapro.hu/apro/intel_core_i5_12400f/friss.html")
        jf = ["https://www.jofogas.hu/vas/Gamer_PC__I5_12400F_32GB_Rtx_3060TI_1_25TB_Garancia__162585607.htm",
              "https://www.jofogas.hu/veszprem/I5_12400f__16gb_ram_gamer_pc_162582580.htm"]
        self.assertEqual([listing_key(url) for url in jf], ["jofogas.hu#162585607", "jofogas.hu#162582580"])
        self.assertEqual(listing_key("https://www.vinted.hu/items/5123456789-lego-75192-millennium-falcon"),
                         "vinted.hu#5123456789")
        self.assertEqual(listing_key("https://www.vinted.hu/items/5123456789-lego-75192?referrer=catalog"),
                         "vinted.hu#5123456789", "más paraméterekkel és címmel is ugyanaz a hirdetés")
        self.assertEqual(listing_key("https://www.vatera.hu/lego-75192-millennium-falcon-3123456789.html"),
                         "vatera.hu#3123456789")
        self.assertEqual(listing_key("https://www.jofogas.hu/1234567.htm"), "jofogas.hu#1234567", "a régi alak marad")

    def test_an_older_price_book_is_merged_by_the_new_keys(self):
        """A régebbi árbázis (kv 3) betöltéskor az új kulcsokra rendeződik: a gyártónként külön memóriakészletek és az
        egybeírt „RTX3060TI” ára egy termékhez kerül."""
        import json

        def samples(prefix: str, title: str, prices: list[int]) -> list[dict]:
            return [{"p": price, "at": 1.0, "last": Clock()(), "url": f"https://hardverapro.hu/apro/{prefix}_{n}/friss.html",
                     "site": "hardverapro", "t": f"{title} {n}"} for n, price in enumerate(prices)]

        old = {"kv": 3, "keys": {
            "2x8gb ddr4 kingston": {"name": "kingston 2x8gb ddr4", "samples": samples("k", "Kingston 2x8GB DDR4", [24000] * 3)},
            "2x8gb corsair ddr4": {"name": "corsair 2x8gb ddr4", "samples": samples("c", "Corsair 2x8GB DDR4", [26000] * 3)},
            "8gb rtx3060ti": {"name": "rtx3060ti 8gb", "samples": samples("r", "RTX3060TI 8GB", [90000] * 2)},
            "3060 8gb rtx ti": {"name": "rtx 3060 ti 8gb", "samples": samples("s", "RTX 3060 Ti 8GB", [95000] * 3)}}}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "arbazis.json").write_text(json.dumps(old), encoding="utf-8")
            book = PriceBook(Path(tmp), Clock())
            self.assertEqual(len(book.samples("2x8gb ddr4")), 6)
            self.assertEqual(len(book.samples("3060 8gb rtx ti")), 5)
            self.assertNotIn("8gb rtx3060ti", book.keys())

    def test_the_price_book_keeps_every_listing_of_a_model(self):
        """Hat i5-12400F hirdetés két oldalon: hat ár (régen a címükben álló „12400” miatt oldalanként egy maradt, és
        nem lett szokásos ár)."""
        with tempfile.TemporaryDirectory() as tmp:
            book = PriceBook(Path(tmp), Clock())
            rows = [{"title": f"Intel Core i5-12400F tálcás {n}", "text": f"Intel Core i5-12400F tálcás {n}",
                     "price": 30000 + n * 500, "platform": "hardverapro",
                     "url": f"https://hardverapro.hu/apro/intel_core_i5_12400f_talcas_{n}/friss.html"} for n in range(3)]
            rows += [{"title": f"I5 12400F processzor {n}", "text": f"I5 12400F processzor {n}", "price": 31000 + n * 500,
                      "platform": "jofogas",
                      "url": f"https://www.jofogas.hu/pest/I5_12400F_processzor_{n}_16258{n}607.htm"} for n in range(3)]
            self.assertEqual(book.add(rows), 6)
            found = book.reference(key("i5 12400F"))
            self.assertIsNotNone(found)
            self.assertEqual(found["n"], 6)


class PriceReadingTests(unittest.TestCase):
    PAGE = """<html><body><main>
      <div class="list-item"><a href="https://www.jofogas.hu/vas/Gamer_PC_162585607.htm">
        <h3>Gamer PC: I5 12400F-32GB-Rtx 3060TI-1,25TB-Garancia!</h3><span class="cnt">5</span>
        <div class="pr">299&nbsp;900 Ft</div><div>okt 22., 21:31</div></a></div>
      <div class="list-item"><a href="https://www.jofogas.hu/pest/PlayStation_5_162582580.htm"><h3>Sony PlayStation 5</h3>
        <span>199 990 Ft</span></a></div>
      <div class="list-item"><a href="https://www.jofogas.hu/pest/Xbox_162582581.htm"><h3>Xbox Series X 1TB</h3>
        <span><s>150 000 Ft</s> 139 990 Ft</span></a></div>
      </main></body></html>"""
    WANT = [299900, 199990, 139990]

    def test_a_number_before_the_price_does_not_merge_into_it(self):
        """A fotók száma („5”) vagy a cím végén álló típusszám („PlayStation 5”) a kártya szövegében az ezres tagolású
        ár elé kerül: az ár mégis 299 900 és 199 990 Ft, nem 5 299 900 és 5 199 990 (ha nincs „price” osztályú ármező,
        az az elem számít, aminek a szövege csak egy ár); az áthúzott régi ár továbbra sem."""
        rows = parse_listings(self.PAGE, "https://www.jofogas.hu/magyarorszag?q=x", "jofogas")
        self.assertEqual([row["price"] for row in rows], self.WANT)

    @unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
    def test_the_browser_reads_the_same_prices(self):
        """A böngészős kiolvasás (LISTINGS_JS: a Vinteden, a Vaterán, és ha a sima letöltés tilt) ugyanígy."""
        from playwright.sync_api import sync_playwright

        from jarvis_core.browser import LISTINGS_JS
        with sync_playwright() as play:
            browser = play.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(self.PAGE)
                rows = page.evaluate(LISTINGS_JS)
            finally:
                browser.close()
        self.assertEqual([row["price"] for row in rows], self.WANT)


class TitleTests(unittest.TestCase):
    def test_glued_gpu_names_are_the_same_product(self):
        """A jelző egybeírva is ugyanaz a kártya (a kulcsa és a termékadatai is): „RTX3060TI”, „RTX 3060Ti”, „6700XT”,
        „GTX1660S” (Super)."""
        for title in ("RTX3060TI 8GB", "RTX 3060Ti 8GB", "Gigabyte GeForce RTX 3060 Ti GAMING OC 8G"):
            self.assertEqual(key(title), "3060 8gb rtx ti", title)
            self.assertEqual(specs.extract(title)["category"], "gpu", title)
        self.assertEqual(key("RX6700XT 12GB"), key("Radeon RX 6700 XT 12 GB"))
        self.assertEqual(key("Sapphire Nitro+ RX 7900XTX"), key("RX 7900 XTX"))
        self.assertEqual(key("GTX1660S 6GB"), key("GTX 1660 Super 6GB"))
        self.assertEqual(key("Asus Dual RTX 2060S"), key("RTX 2060 Super"))
        self.assertNotEqual(key("RTX 3060Ti 8GB"), key("RTX 3060 12GB"), "a Ti más termék")

    def test_a_gpu_without_its_family_name(self):
        """A család (RTX, RX) nélkül is felismeri a kártyát, ha a cím biztos jelet ad (jelző, videókártya-gyártó,
        videókártya-szó); a processzor, az alaplap és a monitor száma nem lesz videókártya."""
        self.assertEqual(key("MSI Ventus 3060 Ti 8GB"), key("RTX 3060 Ti 8GB"))
        self.assertEqual(key("Zotac Twin Edge 3070 8GB"), key("RTX 3070 8GB"))
        self.assertEqual(key("Sapphire Pulse 6700 XT 12GB"), key("RX 6700 XT 12GB"))
        self.assertEqual(key("Palit 1660 Super"), key("GTX 1660 Super"))
        for title in ("AMD Ryzen 5 7600 dobozos", "AMD 7600 dobozos", "Asus 1080 monitor", "Gigabyte B550M DS3H",
                      "Intel Core i5 12400F"):
            self.assertNotIn("rtx", key(title).split(), title)
            self.assertNotIn("rx", key(title).split(), title)
            self.assertNotEqual(specs.extract(title)["category"], "gpu", title)

    def test_phone_names_written_together_or_without_galaxy(self):
        """Az „iPhone 14 ProMax” Pro Max (nem a sima iPhone 14 árával), a „13Pro” 13 Pro, és a „Samsung S23 Ultra” a
        Galaxy S23 Ultra."""
        self.assertEqual(key("iPhone 14 ProMax 256GB"), key("iPhone 14 Pro Max 256GB"))
        self.assertNotEqual(key("iPhone 14 ProMax 256GB"), key("iPhone 14 256GB"))
        self.assertEqual(key("iPhone 13Pro 128GB"), key("iPhone 13 Pro 128GB"))
        for title in ("Samsung S23 Ultra 256GB", "Galaxy S23Ultra 256GB"):
            self.assertEqual(key(title), key("Samsung Galaxy S23 Ultra 256GB"), title)
            self.assertEqual(specs.extract(title)["category"], "phone", title)
        self.assertEqual(key("Samsung 970 EVO Plus 1TB"), "1tb 970 plus samsung", "az SSD nem telefon")

    def test_memory_modules_by_what_sets_their_price(self):
        """A memóriánál a generáció, a kiszerelés, a kivitel és a méret számít; a gyártó, a sebesség és az időzítés
        csak közelítő eltérés (így a „3200” és a „3200MHz” írásmód sem két termék). A „DDR4-3200” DDR4; a „2 x 8GB” és a
        „8GB + 8GB” ugyanaz a készlet, mint a „2x8GB” (nem csomag, és nem egy 8 GB-os modul)."""
        kit = ("Kingston Fury 16GB 2x8GB DDR4 3200", "Kingston Fury 16GB 2x8GB DDR4 3200MHz",
               "Corsair Vengeance 16GB (2x8GB) DDR4-3600 CL18", "Crucial 8GB + 8GB DDR4", "Kingston 16GB 2 x 8GB DDR4")
        for title in kit:
            self.assertEqual(key(title), "2x8gb ddr4", title)
            self.assertTrue(usable(title), title)
        spec = specs.extract("Kingston 16GB DDR4-3200")
        self.assertEqual(spec["attrs"]["generation"]["value"], "ddr4")
        self.assertNotIn("generation", spec["missing"])
        self.assertNotEqual(key("Kingston 16GB DDR4 3200"), key("Kingston 2x8GB DDR4 3200"), "egy modul nem készlet")
        self.assertNotEqual(key("Kingston 16GB DDR4 3200"), key("Kingston 16GB DDR5 5600"))
        self.assertNotEqual(key("Kingston 16GB DDR4 3200"), key("Kingston 16GB DDR4 3200 SO-DIMM"))
        board = "MSI B450 Tomahawk DDR4 alaplap"
        self.assertIn("b450", key(board).split(), "az alaplap kulcsa a lapkakészlete marad")
        self.assertNotEqual(specs.extract(board)["category"], "ram", "a DDR4 szótól nem memóriamodul")
        self.assertEqual(specs.unsure_variant(specs.extract(board)), [], "nem vár memóriaméretet egy alaplaptól")

    def test_a_search_matches_the_glued_spellings_too(self):
        self.assertTrue(matches_query("RTX 3060 Ti", "MSI RTX3060TI Ventus 8GB"))
        self.assertTrue(matches_query("RTX 3060 Ti", "Gigabyte RTX 3060Ti Gaming OC"))
        self.assertTrue(matches_query("RTX3060Ti", "Asus RTX 3060 Ti Dual"))
        self.assertTrue(matches_query("Sony WH-1000XM4", "Sony WH 1000XM4 fejhallgató"))
        self.assertTrue(matches_query("PS5 kontroller", "PlayStation 5 DualSense kontroller"))
        self.assertFalse(matches_query("RTX 3060 Ti", "RTX 3060 12GB"))
        self.assertFalse(matches_query("RTX 3060 Ti", "RTX 3060 Ti hűtő ventilátor tartó"), "kiegészítő")


class RealTitleTests(unittest.TestCase):
    """Valódi Hardverapró-címek (a videókártya és a memória kategória első oldala, 2026. október)."""

    def test_a_dealer_who_takes_trade_ins_is_selling(self):
        """A „Beszámítás” a kereskedő eladó hirdetése (a régit beszámítja): az ára eladási ár, nem keresőhirdetés."""
        for title in ("Gainward GeForce RTX 4080 Phoenix GS 16GB - Beszámítás",
                      "BESZÁMÍTÁS! Asus ROG Strix RTX 3070 8GB videokártya garanciával hibátlan működéssel"):
            self.assertTrue(usable(title), title)
        self.assertFalse(usable("Videókártya felvásárlás"))
        self.assertFalse(usable("Keresek 3060/3060ti/3070/3070ti/3080 kártyákat"))

    def test_a_warranty_date_is_not_part_of_the_product(self):
        self.assertEqual(key("ASUS TUF RTX 3080 V2 10GB OC Edition / ALZA GARANCIA 2027.03. Kifogástalan gyári állapot!"),
                         key("RTX 3080 10GB"))
        self.assertEqual(key("Asus TUF RTX 5090 , DOBOZOS, magyar alza garancia, 2028.05.17.-ig"), key("RTX 5090"))
        self.assertEqual(key("iPhone 13 128GB garancia 2026"), key("iPhone 13 128GB"))
        self.assertIn("2020", key("iPhone SE 2020 64GB").split(), "a típus évszáma marad")

    def test_a_card_makers_model_name_is_not_a_variant(self):
        """A videókártyánál csak a lapka jelzője (Ti, Super, XT) számít: az „Ultra”, „Air” a kártyagyártó típusneve."""
        self.assertEqual(key("Új (open box) EVGA GeForce RTX 3070 FTW3 Ultra Gaming 8GB GDDR6"), key("RTX 3070 8GB"))
        self.assertEqual(key("XFX Radeon RX9070XT Mercury OC Magnetic White Air"), key("RX 9070 XT"))
        self.assertEqual(key("Eladó Asus Rog 1050TI"), key("GTX 1050 Ti"))

    def test_an_amd_processor_without_ryzen_in_its_title(self):
        """„Eladó: AMD 5800X” a Ryzen 7 5800X (a végződés nélküli szám csak egy processzor-szó mellett: az „AMD 7600”
        lehet videókártya is); a „8x5,5 GHz” (magok) nem típusjel."""
        self.assertEqual(key("Eladó: AMD 5800X"), key("Ryzen 7 5800X"))
        self.assertEqual(key("AMD 5800X3D tálcás"), key("AMD Ryzen 7 5800X3D"))
        self.assertEqual(key("AMD 3600 processzor"), key("Ryzen 5 3600"))
        self.assertNotEqual(key("AMD 7600"), key("Ryzen 5 7600"))
        self.assertEqual(key("Gyári új, 36 hónap garanciás (Skynet computer) Ryzen 7 9700X 8x5,5 GHz eladó"),
                         key("Ryzen 7 9700X"))

    def test_memory_on_a_graphics_card_is_not_a_memory_module(self):
        title = "Nvidia Quadro P400 2Gb DDR5 videókártya"
        self.assertNotEqual(key(title), key("2GB DDR5"))
        self.assertNotEqual(specs.extract(title)["category"], "ram")

    def test_real_memory_titles(self):
        """A készlet a „kit” szóval, az „x” nélkül („(4 16GB)”) és a modul méretével együtt is ugyanaz; a kereskedő
        „TÖBB DARAB … garancia” hirdetése darabár; a több generációt vagy méretet árusító hirdetés nem egy termék."""
        self.assertEqual(key("G.Skill Ripjaws V 16GB (2x8GB) DDR4 3600MHz CL19 Red (F4-3600C19D-16GVRB) RAM kit eladó"),
                         "2x8gb ddr4")
        self.assertEqual(key("Corsair Vengeance RGB Pro 64GB (4 16GB) DDR4 3200MHz CL16"), "4x16gb ddr4")
        self.assertEqual(key("16 GB NOTEBOOK DDR5 Kingston 5600MHz 8GB - KCP556SS6-8 RAM (2x8GB)"), "2x8gb ddr5 sodimm")
        self.assertEqual(key("Válogatott DDR4 csúcs ramok, 32GB (2 x 16GB) DDR4-4000Mhz CL18 kit eladó!"), "2x16gb ddr4")
        self.assertEqual(key("16gb DDR3 1333mhz PC ram"), "16gb ddr3", "a „PC RAM” asztali modul, nem egy gép")
        dealer = "Samsung DDR4 8GB SODIMM laptop RAM 3200MHz TÖBB DARAB - 1 év garancia + számla"
        self.assertTrue(usable(dealer))
        self.assertEqual(key(dealer), "8gb ddr4 sodimm")
        self.assertFalse(usable("Laptop RAM, memória, DDR4, DDR3, DDR3L, 2-4-8-16-32GB"))
        self.assertEqual(key("DDR4-2400 asztali memória 8GB és 16GB"), "")
        self.assertEqual(key("Kingston FURY Beast 32GB DDR5 5200MHz- több db elérhető! - akár 1x32Gb akár 2x32Gb akár "
                             "4x32Gb"), "")


class SearchTermTests(unittest.TestCase):
    """Amivel a többi oldalon megnézi, mennyiért adják: a termék szavai, ahogy a hirdetések többsége írja."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hunter = DealHunter(Path(self.tmp.name), PLATFORMS, None, None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_glued_and_multi_word_names(self):
        probe = self.hunter._probe_text
        self.assertEqual(probe("MSI GeForce RTX3060TI Ventus 8GB"), "rtx 3060 ti", "szétválasztva, a gyártó nélkül")
        self.assertEqual(probe("Gigabyte RTX 3060Ti Gaming OC"), "RTX 3060 ti", "egy szó egyszer")
        self.assertEqual(probe("iPhone 14 ProMax 256GB"), "iPhone 14 pro max")
        self.assertEqual(probe("PlayStation 5 Digital Edition"), "PlayStation 5 Digital", "nem csak „Digital”")
        self.assertEqual(probe("Sony PlayStation 5 DualSense kontroller"), "PlayStation 5 kontroller")
        self.assertEqual(probe("Xbox Series X 1TB"), "Xbox Series X")
        self.assertEqual(probe("Steam Deck OLED 512GB"), "Steam Deck OLED")
        self.assertEqual(probe("Sony WH-1000XM4 fejhallgató"), "Sony WH-1000XM4", "a kötőjeles alak marad")

    def test_a_memory_module_is_searched_by_its_size(self):
        """A memóriánál a méret maga a termék: „16GB DDR4” (nem csak „DDR4”), a gyártó és a sebesség nélkül."""
        probe = self.hunter._probe_text
        self.assertEqual(probe("Kingston Fury 16GB DDR4 3200"), "16GB DDR4")
        self.assertEqual(probe("Corsair Vengeance 16GB (2x8GB) DDR4-3600 CL18"), "2X8GB DDR4")
        self.assertEqual(probe("Kingston 8GB PC4-21300 laptop memória"), "8GB DDR4")


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()

    def tearDown(self):
        self.tmp.cleanup()

    def lite(self, book: PriceBook, n: int, price: int, site: str = "hardverapro") -> None:
        url = (f"https://hardverapro.hu/apro/switch_lite_{n}/friss.html" if site == "hardverapro" else
               f"https://www.jofogas.hu/b/Switch_Lite_{n}_1600000{n:02d}.htm")
        book.add([{"text": f"Nintendo Switch Lite szürke {n}", "price": price, "platform": site, "url": url}])

    def test_a_lower_recent_price_level_wins(self):
        """Ha a nemrég látott árak maguk is elegendők, és jóval olcsóbbak a régebbieknél (lejjebb ment az árszint),
        azokhoz mér; a régiek a kizártak között, az okkal."""
        book = PriceBook(Path(self.tmp.name), self.clock)
        for n, price in enumerate((100000, 102000, 98000, 101000, 99000, 103000)):
            self.lite(book, n, price)
        self.clock.now += 20 * DAY
        for n, price in enumerate((80000, 82000, 79000, 81000, 80500), start=10):
            self.lite(book, n, price)
        found = book.reference(key("Nintendo Switch Lite"))
        self.assertLess(found["price"], 85000)
        self.assertEqual(found["n"], 5)
        self.assertIn("olcsóbbak", found["note"])
        self.assertEqual(sum(1 for entry in found["basis"]["out"] if entry["why"].startswith("régebbi ár")), 6)

    def test_a_higher_recent_price_does_not_raise_the_reference(self):
        """Fordítva nem: a drágább friss kínálat nem viszi feljebb a szokásos árat (a vételhez az óvatos becslés kell)."""
        book = PriceBook(Path(self.tmp.name), self.clock)
        for n, price in enumerate((80000, 82000, 79000, 81000, 80500, 80000)):
            self.lite(book, n, price)
        self.clock.now += 20 * DAY
        for n, price in enumerate((100000, 102000, 98000, 101000, 99000), start=10):
            self.lite(book, n, price)
        found = book.reference(key("Nintendo Switch Lite"))
        self.assertEqual(found["n"], 11)
        self.assertLess(found["price"], 98000)

    def test_a_revisited_listing_that_is_still_up_counts_as_seen_now(self):
        book = PriceBook(Path(self.tmp.name), self.clock)
        self.lite(book, 1, 60000)
        self.clock.now += 5 * DAY
        url = "https://hardverapro.hu/apro/switch_lite_1/friss.html"
        book.revisited(key("Nintendo Switch Lite"), url, gone=False)
        [sample] = book.samples(key("Nintendo Switch Lite"))
        self.assertEqual(sample["last"], self.clock.now)

    def test_old_prices_alone_are_not_enough(self):
        """Hat hibátlan összehasonlító két oldalról elég ár, de ha mindet több mint FRESH_DAYS napja láttuk, nem
        biztos (előbb a mostani árakat nézi meg); a friss árakkal már az."""
        hunter = DealHunter(Path(self.tmp.name), PLATFORMS, lambda: FakeBrowser(), FakeLlm(), clock=self.clock)
        hunter._vetted = AllVetted()
        for n, (price, site) in enumerate(((60000, "hardverapro"), (58000, "jofogas"), (59000, "hardverapro"),
                                           (61000, "jofogas"), (62000, "hardverapro"), (60500, "jofogas"))):
            self.lite(hunter.book, n, price, site)
        lite = key("Nintendo Switch Lite")
        self.assertTrue(hunter._screened(hunter.book.reference(lite))["enough"])
        self.clock.now += (FRESH_DAYS + 3) * DAY
        stale = hunter._screened(hunter.book.reference(lite))
        self.assertFalse(stale["enough"])
        self.assertFalse(stale["sure"])
        self.assertIn("régebben", stale["stale"])
        for n, (price, site) in enumerate(((60000, "hardverapro"), (59500, "jofogas"), (60500, "hardverapro"))):
            self.lite(hunter.book, n, price, site)        # újra látta őket (a találati oldalon)
        self.assertTrue(hunter._screened(hunter.book.reference(lite))["sure"])


class InstantFreshnessTests(InstantCase):
    """Az azonnali figyelő régi árbázissal: előbb a mostani árakat nézi meg a többi oldalon (a legújabb hirdetésekkel
    elöl), és csak azokhoz mérve szól."""

    def setUp(self):
        super().setUp()
        self.hunter._vetted = AllVetted()
        comps = [(60000, "hardverapro"), (58000, "jofogas"), (59000, "hardverapro"), (61000, "jofogas"),
                 (62000, "hardverapro"), (60500, "jofogas")]
        self.hunter.book.add([{"text": f"Nintendo Switch Lite szürke {n}", "price": p, "platform": site,
                               "url": f"https://{'hardverapro.hu/apro' if site == 'hardverapro' else 'www.jofogas.hu/b'}"
                                      f"/lite_{n}/friss.html"} for n, (p, site) in enumerate(comps)])
        self.clock.now += (FRESH_DAYS + 3) * DAY     # az árbázis árai régiek
        self.watch.add("Nintendo Switch Lite", ["hardverapro"])
        self.watch.set_enabled(True)
        self.results([ha_card(1, "Régi kontroller", "2 000 Ft")])
        self.watch.tick()
        self.later()
        self.results([ha_card(7, "Nintendo Switch Lite", "30 000 Ft"), ha_card(1, "Régi kontroller", "2 000 Ft")])
        self.net.pages["_7/friss"] = page_html("Nintendo Switch Lite, hibátlanul működik, töltővel.")

    def jofogas(self, prices: list[int]) -> None:
        cards = "".join(jf_card(20 + n, f"Nintendo Switch Lite szürke {n}", f"{price // 1000} {price % 1000:03d}")
                        for n, price in enumerate(prices))
        self.net.pages["magyarorszag?q=Switch%20Lite"] = (200, f"<html><body>{cards}</body></html>")

    def searched(self) -> list[str]:
        return [url for _at, url in self.net.asked if "jofogas.hu/magyarorszag" in url]

    def test_with_old_prices_it_checks_the_current_ones_first(self):
        self.jofogas([60000, 59000, 61000, 60500])
        self.watch.tick()
        self.assertEqual(self.searched(), [newest_first("https://www.jofogas.hu/magyarorszag?q=Switch%20Lite", "jofogas")],
                         "előbb a mostani árak, a legújabb elöl")
        self.assertEqual([n[1] for n in self.notices], ["Megéri: Nintendo Switch Lite"])

    def test_a_market_that_fell_is_not_a_false_bargain(self):
        """A régi árak szerint (kb. 60 000) a 30 000-es hirdetés nagy fogás lenne, de most kb. 33 000-ért adják: nem szól
        róla (a régi árbázissal korábban sürgősen szólt volna)."""
        self.jofogas([33000, 32000, 34000, 33500, 32500])
        self.watch.tick()
        self.assertTrue(self.searched())
        self.assertFalse(any(n[1].startswith("Megéri") for n in self.notices), [n[1] for n in self.notices])
        self.assertEqual(self.hunter.state()["deals"], [])
        today = self.hunter.funnel()["counts"]
        self.assertEqual(today.get("not_cheap"), 1, "a mostani árakhoz mérve nem olcsó (nem a kevés ár miatt maradt ki)")


class ResearchTests(unittest.TestCase):
    def test_the_selling_price_research_looks_at_the_newest_listings(self):
        """A saját tárgyad árkutatása is a legújabb hirdetésekkel elöl keres (a mostani árak)."""
        asked: list[str] = []

        class Fetcher:
            def handles(self, site):
                return True

            def usable(self, site):
                return True

            def listings(self, url, site):
                asked.append(url)
                return []

        with tempfile.TemporaryDirectory() as tmp:
            store = ItemStore(Path(tmp))
            item = store.create(title="Nintendo Switch Lite", search_query="Nintendo Switch Lite", price=60000)
            sites = [PLATFORMS[key] for key in ("jofogas", "vinted")]
            PriceResearcher(None, store, lambda: None, sites, fetcher=Fetcher()).research(item.id, browser=False)
        self.assertEqual(asked, ["https://www.jofogas.hu/magyarorszag?q=Nintendo%20Switch%20Lite&sp=3",
                                 "https://www.vinted.hu/catalog?search_text=Nintendo%20Switch%20Lite&order=newest_first"])


if __name__ == "__main__":
    unittest.main()
