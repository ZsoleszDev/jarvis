"""Árbázis: a Jarvis által látott hirdetések árai termékenként (munka/arbazis.json), modell nélkül.

Minden hirdetésből, amit az automata kereskedés lát (a keresések, a figyelt oldalak, az árkutatás találatai), a címéből
modell nélkül kiolvas egy termékkulcsot (product_key): a márkát vagy a termékcsaládot és a típusjelet („rtx 3060 ti”,
„ps5 kontroller”, „makita dhp482”, „iphone 13 pro”), és eltárolja az árát. Így pár nap alatt kézi beállítás nélkül
összeáll, mennyiért adják a gyakori termékeket, és egy új hirdetésről ezredmásodpercek alatt kiderül, hogy jóval
olcsóbb-e a szokásosnál (reference). Ami nem a termék ára (kereső-, csere- és csomaghirdetés, alkatrész, hibás,
kiegészítő, „-hoz/-hez”), az nem kerül be.

Óvatos: a kulcs inkább legyen túl szűk (kevesebb ár egy termékhez), mint túl tág (két különböző termék egy kulcson). Csak
akkor mond szokásos árat, ha elég friss ár van, és nem szórnak nagyon (különben pl. a játék és a konzol keveredhet).

Eladási sebesség: néhány régebben látott hirdetést később újra megnéz (revisit, csak olvasva); ha addigra eltűnt vagy
elkelt, feljegyzi, hány nap után. Ebből látszik, mi kel el gyorsan, és mi áll hetekig.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import threading
import time
from pathlib import Path
from typing import Any

from .selling import (_LOT_RE, _PARTS_RE, _SALE_RE, _SWAP_RE, _WANTED_RE, listing_key, price_stats, round_price)
from .textutil import fold

MAX_KEYS = 1500            # ennyi terméket őriz (a legrégebben látottak esnek ki)
MAX_SAMPLES = 30           # termékenként ennyi árat (a legújabbakat)
TITLE_KEEP = 100           # egy ár mellé a hirdetés címének eleje (a találatnál látszik, mihez mérte; a termékadatai)
MAX_AGE = 45 * 86400       # ennél régebben látott ár már nem számít a szokásos árba
MIN_SAMPLES = 5            # legalább ennyi ár kell egy szokásos árhoz
REVISIT_AFTER = 2 * 86400  # egy hirdetést legkorábban ennyi idő múlva néz meg újra (elkelt-e)
REVISIT_AGAIN = 3 * 86400  # ha akkor még fent volt, ennyi múlva újra
REVISIT_UNTIL = 30 * 86400 # ennyi után már nem nézi (ami egy hónapja fent van, az lassan kel)
SLOW_DAYS = 14             # ennyi nap után is fent: lassan kel
SOLD_MIN = 3               # ennyi elkelt (újranézéskor eltűnt) hirdetés ára kell az elkelt árhoz
PRICE_CUT = 0.03           # ennyivel olcsóbb lett egy már látott hirdetés: leárazta
SLOW_RATE = 0.25           # ha az újranézett hirdetéseknek legfeljebb ennyi része tűnt el: lassan kel
# A termékkulcs képzésének változata: ha a mentett árbázis régebbi, betöltéskor a kulcsokat újraképzi a nevükből, és az
# így egybeeső termékek árait összevonja (rekey).
KEY_VERSION = 3

# Márkák (ékezet nélkül, kisbetűvel; a több szavasak egybeírva, lásd _ALIASES).
BRANDS = {
    "apple", "samsung", "sony", "nintendo", "microsoft", "xiaomi", "huawei", "lenovo", "asus", "acer", "hp", "dell",
    "msi", "gigabyte", "logitech", "razer", "steelseries", "corsair", "hyperx", "jbl", "bose", "sennheiser", "beats",
    "marshall", "garmin", "fitbit", "dyson", "philips", "braun", "bosch", "makita", "dewalt", "milwaukee", "metabo",
    "hilti", "festool", "ryobi", "stihl", "husqvarna", "lego", "playmobil", "nike", "adidas", "puma", "newbalance",
    "asics", "vans", "converse", "jordan", "reebok", "salomon", "timberland", "drmartens", "northface", "patagonia",
    "levis", "lacoste", "tommy", "boss", "gucci", "moncler", "stoneisland", "rayban", "canon", "nikon", "fujifilm",
    "olympus", "panasonic", "gopro", "dji", "lg", "tcl", "oneplus", "google", "oppo", "realme", "honor", "motorola", "redmi",
    "nokia", "amd", "intel", "nvidia", "kingston", "crucial", "seagate", "toshiba", "synology", "tplink", "netgear",
    "trek", "specialized", "cube", "giant", "scott", "delonghi", "krups", "kenwood", "kitchenaid", "thermomix",
    "rowenta", "tefal", "karcher", "roborock", "ecovacs", "irobot", "nespresso", "bang", "casio", "seiko", "tissot",
}
# Termékcsaládok: már a nevük megmondja, mi a termék, ezért mellettük a márka (vagy a kártyagyártó) nem kell a kulcsba.
FAMILIES = {
    "rtx", "gtx", "rx", "arc", "ryzen", "iphone", "ipad", "macbook", "imac", "airpods", "galaxy", "pixel",
    "xbox", "ps3", "ps4", "ps5", "switch", "kindle", "thinkpad", "steamdeck", "quest", "watch",
}
# Ezeknél a család és egy jelző (vagy „kontroller”) típusjel nélkül is egy terméket jelöl („Switch OLED”, „PS5 Digital”,
# „Xbox Series X”); a többinél kell a típusjel is (az „AirPods Pro” lehet az első és a második is, más áron).
FAMILY_COMPLETE = {"switch", "ps4", "ps5", "steamdeck", "xbox"}
MODIFIERS = {"ti", "super", "xt", "xtx", "pro", "max", "plus", "ultra", "mini", "lite", "se", "oled", "slim",
             "digital", "air", "fe", "seriesx", "seriess"}
# Sorozatnév, ami után a típusszám jön („Redmi Note 10”, „Galaxy Tab S7”, „Mi Band 7”): csak a szám előtt számít.
SERIES = {"note", "tab", "mi", "buds", "band", "fold", "flip", "gen", "series", "mark", "mk", "book", "pad"}
# A termék fajtája, ha ugyanaz a család mást is jelölhet (PS5 konzol, kontroller vagy játék).
TYPE_WORDS = {"kontroller": "kontroller", "controller": "kontroller", "kontroler": "kontroller",
              "dualsense": "kontroller", "dualshock": "kontroller", "joystick": "kontroller", "gamepad": "kontroller",
              "joycon": "kontroller", "jatek": "jatek", "jatekok": "jatek", "game": "jatek",
              # egy laptop vagy egy egész gép a benne lévő videókártyával vagy processzorral: nem a kártya ára
              "laptop": "laptop", "notebook": "laptop", "pc": "pc", "szamitogep": "pc", "konfig": "pc",
              "konfiguracio": "pc", "gamerpc": "pc",
              # laptopcsaládok (a kártyagyártók típusneveivel nem ütköznek): a „Legion 5 RTX 3060” laptop, nem kártya
              **{name: "laptop" for name in ("legion", "omen", "zephyrus", "victus", "ideapad", "vivobook", "zenbook",
                                             "aspire", "pavilion", "latitude", "inspiron", "alienware", "helios",
                                             "katana", "thinkbook", "elitebook", "probook")}}
# Videókártya: a lapka (RTX 3060, RX 6600, Arc A770), a jelzője (Ti, Super, XT) és a memória dönti el az árát; a
# kártyagyártó típusneve (G1, Ventus, Eagle…) csak kicsit, ezért nem kerül a kulcsba (különben a hasonló árak szétesnek).
GPU_FAMILIES = {"rtx", "gtx", "rx", "arc"}
_GPU_CHIP = re.compile(r"^[a-z]?\d{3,4}$")
# Processzor: az „Intel Core i5-10400F” és az „i5 10400F” ugyanaz; az Intel/AMD márka mellette nem kell a kulcsba.
_CPU_TIER = re.compile(r"^i[3579]$")
CPU_BRANDS = {"intel", "amd"}
# A gyakran hamisított márkák: ha feltűnően olcsó, figyelmeztet (nem lelkesedik).
FAKE_PRONE = {"airpods", "nike", "jordan", "adidas", "northface", "moncler", "stoneisland", "gucci", "rayban",
              "lacoste", "dyson", "lego", "dualsense", "kontroller", "watch", "beats"}
# Ezek nem mondanak semmit a termékről (és két cím között eltérhetnek).
_NOISE = {"geforce", "radeon", "nvidia", "edition", "gaming", "oc", "dual"}
_ALIASES = ((r"\bplay ?station ?(\d)\b", r"ps\1"), (r"\bnew balance\b", "newbalance"), (r"\bdr\.? ?martens\b", "drmartens"),
            (r"\b(the )?north face\b", "northface"), (r"\bstone island\b", "stoneisland"), (r"\bray ?-?ban\b", "rayban"),
            (r"\btp ?-?link\b", "tplink"), (r"\bsteam deck\b", "steamdeck"), (r"\bjoy ?-?con\w*\b", "joycon"),
            (r"\bde ?'?longhi\b", "delonghi"), (r"\bx ?box\b", "xbox"), (r"\bapple watch\b", "watch"),
            (r"\blevi'?s\b", "levis"), (r"\bmeta quest\b", "quest"), (r"\boculus quest\b", "quest"),
            (r"\bseries ([xs])\b", r"series\1"), (r"\bxiaomi redmi\b", "redmi"), (r"\bgamer ?pc\b", "gamerpc"),
            # memória: a „SO DIMM” egy szó, a „DDR 4” is „ddr4”; a modul szabványneve („PC4-21300”) a generációja
            (r"\bso ?-?dimm\b", "sodimm"), (r"\b(lp)?ddr ([2-5])\b", r"\1ddr\2"), (r"\bpc([2-5])l?-?\d{4,5}[a-z]?\b", r"ddr\1"),
            # a laptop-memória SO-DIMM (de egy laptop a benne lévő memóriával nem az: ehhez a memória szava kell mellé)
            (r"\b(?:laptop|notebook)(?:ba|hoz|os)? (ram|memoria\w*|memory)\b", r"sodimm \1"),
            (r"\b(ram|memoria\w*|memory) (?:laptop|notebook)\w*", r"\1 sodimm"))
# Ugyanannak a típusjelnek egy alakja (a kötőjel már ki van véve): az „i5-10400F”, „i5 10400F”, „i510400f” egyaránt
# „i5 10400f”, az „RTX3060” „rtx 3060”, a memóriakészlet „2x8 GB”, „2 x 8GB”, „2x8” egyaránt „2x8gb”.
_SPLITS = ((re.compile(r"\bi([3579]) ?(\d{4,5}[a-z]{0,3})\b"), r"i\1 \2"),
           (re.compile(r"\b(rtx|gtx|rx)(\d{3,4})\b"), r"\1 \2"),
           (re.compile(r"\b([1-8]) ?x ?(2|4|8|16|32|64) ?gb\b"), r"\1x\2gb"),
           (re.compile(r"\b([1-8])x(4|8|16|32|64)\b(?! ?(?:cm|mm|m|db)\b)"), r"\1x\2gb"))
_KIT = re.compile(r"^([1-8])x(\d{1,2})gb$")
# Videókártyán a „4G”, „6G”, „12G” a memória (VRAM): ugyanaz, mint a „4GB” (a GTX 1060 3 és 6 GB-os is, más áron).
# Telefonnál (és máshol) a „4G”, „5G” a mobilhálózat, nem méret: ott nem így olvassa.
_GPU_CONTEXT = {"rtx", "gtx", "rx", "arc", "geforce", "radeon", "quadro", "videokartya", "vga", "vram"}
_GPU_VRAM = re.compile(r"^(1|2|3|4|6|8|10|11|12|16|20|24|32|48)g$")
# Memóriamodul: a kiszerelés és a fajta is a termék része (más áron kel): SO-DIMM (laptop), szerver (ECC, regisztrált),
# és a „kit” (több modul, a felosztása nélkül), csak memóriánál (egy DDR-generáció vagy a „RAM” szó mellett).
_RAM_FORMS = {"sodimm": "sodimm", "ecc": "ecc", "reg": "reg", "registered": "reg", "rdimm": "reg", "lrdimm": "reg",
              "kit": "kit", "keszlet": "kit"}
_RAM_CONTEXT = re.compile(r"^(?:lp)?ddr[2-5]\w*$|^(?:ram|memoria\w*|memory|dimm|sodimm|rdimm)$")
# Ár a címben („95 000 Ft”, „25e”, „25.000,-”): nem a termék része.
_PRICE_IN_TITLE = re.compile(r"\b\d+(?:[ .]\d{3})*\s*(?:,-|(?:ft|huf|forint|e ?ft|ezer)\b)")
# Mértékegység vagy méret egy szám után (8gb, 4k, 42es, 38-as, 120hz): nem a típusjel.
_UNIT = re.compile(r"^\d+(?:gb|tb|mb|ghz|mhz|hz|w|v|mah|wh|cm|mm|m|l|ml|kg|col|inch|in|db|ft|e|p|es|as|os|"
                   r"ig|tol|ra|re|ban|ben|bol)$|^\d{1,2}[xk]$|^\d{1,3}g$")   # a 5600X, 12600K, 5600G típusjel
_UNIT_WORDS = {"gb", "tb", "mb", "ghz", "mhz", "hz", "w", "v", "mah", "wh", "cm", "mm", "m", "l", "ml", "kg", "g", "col",
               "inch", "db", "darab", "ft", "ev", "eves", "honap", "honapos", "nap", "ora", "perc", "licit", "meret",
               "es", "as", "os"}
_PIECES = re.compile(r"\b[2-9] ?(x|db|darab|par)\b")   # „2x”, „3 db”: több darab egy áron
# Ömlesztett vagy összetett csomag („ruhacsomag”, „ömlesztve”, „gyűjtemény”, „játékok egyben”): az ár nem egy darabé.
_HEAP = re.compile(r"\b\w+csomag\w*|\b(omlesztve|omlesztett|gyujtemeny\w*|egyben elado\w*|elado\w* egyben)\b|"
                   r"\b\w{3,}k egyben\b")
_FOR_IT = re.compile(r"\w{2,}(?:hoz|hez)$")     # „iphonehoz”, „13hoz”, „kontrollerhez”: kiegészítő hozzá
# Kiegészítő (tok, töltő, kábel…): nem maga a termék ára.
_ACCESSORY = re.compile(r"\b(tolto\w*|dokkolo\w*|tok|tokja|tokok|vedotok\w*|borito\w*|skin|matrica\w*|kabel\w*|"
                        r"allvany\w*|tarto|szilikon\w*|grip\w*|csere ?gomb\w*|ujjvedo\w*|kijelzovedo\w*|uvegfolia\w*|"
                        r"folia|szij\w*|pant)\b")


# Ruha, cipő és a divatmárkák (ékezet nélkül): ezekkel az automata kereskedés nem foglalkozik (nem veszi meg, hogy
# drágábban eladja), a felhasználó így döntött. A kifejezett keresésed (Keresések, azonnali figyelő keresőszóval) ettől
# még mehet; a böngészés, a figyelt oldalak, a csomagbontás és az alku kihagyja őket.
_WEAR_RE = re.compile(
    r"\b(?:(?<!vw )(?<!volkswagen )polo\w{0,3}|pulover\w*|pulcsi\w*|hoodie\w*|kapucnis\w*|sweatshirt\w*|"
    r"melegito\w*|kardigan\w*|cardigan\w*|kabat\w*|dzseki\w*|jacket\w*|parka\w*|anorak\w*|melleny\w*|"
    r"\w*nadrag\w*|farmer\w*|jeans|"
    r"leggings?|szoknya\w*|\w*ruha(?:k|t|ja|i|csomag\w*)?|bluz\w*|ing|ingek|triko\w{0,2}|zokni\w*|harisnya\w*|"
    r"fehernemu\w*|melltarto\w*|bugyi\w*|pizsama\w*|bikini\w*|overal\w*|kezeslabas\w*|kosztum\w*|zako\w*|"
    r"oltony\w*|blezer\w*|tunika\w*|sal|salat|sapka\w*|kesztyu\w*|nyakkendo\w*|t[ -]?shirt\w*|sweater\w*|"
    r"\w*cipo\w*|csizma\w*|bakancs\w*|papucs\w*|szandal\w*|sneaker\w*|magassarku\w*|balerina\w*|mokaszin\w*|"
    r"air max|air force|yeezy|dunk (?:low|high)|stan smith|old skool|chuck taylor|"
    r"zara|h ?& ?m|bershka|stradivarius|pull ?(?:and|&) ?bear|mango|primark|sinsay|carhartt|ralph lauren|lacoste|"
    r"tommy hilfiger|levi'?s|north ?face|patagonia|moncler|stone island|gucci|prada|versace|armani|burberry|"
    r"balenciaga|louis vuitton|michael kors|calvin klein|superdry|hollister|abercrombie|uniqlo|ellesse|converse|"
    r"vans|dr\.? ?martens|timberland|ugg|crocs|birkenstock|skechers|new ?balance|asics|reebok|jordan|nike|adidas|"
    r"puma)\b")


def wearable(text: str) -> bool:
    """Ruha, cipő vagy divatmárka-e (a hirdetés címe vagy egy keresőszó alapján): az automata kereskedés ezekkel nem
    foglalkozik (_WEAR_RE)."""
    return bool(_WEAR_RE.search(fold(text or "")))


def _words(text: str) -> list[str]:
    folded = fold(text or "")
    for pattern, replacement in _ALIASES:
        folded = re.sub(pattern, replacement, folded)
    folded = _PRICE_IN_TITLE.sub(" ", folded)
    folded = re.sub(r"(?<=[a-z0-9])-(?=[a-z0-9])", "", folded)      # WH-1000XM4 → wh1000xm4, i5-12400 → i512400
    for pattern, replacement in _SPLITS:                               # i512400 → i5 12400 (mint az „i5 12400”)
        folded = pattern.sub(replacement, folded)
    words = [re.sub(r"^1x(\d{1,2}gb)$", r"\1", word) for word in re.findall(r"[a-z0-9]+", folded)]   # 1x8GB = 8GB
    if any(word in _GPU_CONTEXT or word.startswith("gddr") for word in words):
        words = [f"{m.group(1)}gb" if (m := _GPU_VRAM.match(word)) else word for word in words]
    return words


def ram_context(words: list[str]) -> bool:
    """Memóriamodulról szól-e (egy DDR-generáció, vagy a RAM, memória, DIMM szó; _RAM_FORMS)."""
    return any(_RAM_CONTEXT.match(word) for word in words)


def usable(text: str) -> bool:
    """Az ára egy működő, egy darab, meghatározott termék ára-e (unusable: miért nem)."""
    return not unusable(text)


# Nem a termék vételára: részletfizetés, bérlet („havi 5 000 Ft”, „25 000 Ft/hó”, „részletre”); darabár („/db”,
# „darabár”); a termék doboza („iPhone 13 doboza”, „üres doboz”); zárolt telefon (iCloud, aktiválási vagy Google-fiók
# zár, lopott: a tagadott, pl. „iCloud mentes”, „nincs iCloud”, nem); és egy árlista (három vagy több méret egy címben).
_INSTALLMENT = re.compile(r"\b(?:havi|havonta)\s+\d|\d\s*(?:ft|huf|e|ezer)?\s*/\s*(?:ho|honap)\b|"
                          r"\b(?:reszletre|reszletfizet\w*|thm|lizing\w*|berelhet\w*|berbe|kolcsonz\w*)\b")
_PER_PIECE = re.compile(r"\b(?:darabar\w*|db ?ar\w*|ar ?/ ?db|darabonkent|darabja|egyenkent)\b|\d\s*(?:ft)?\s*/\s*db\b|"
                        r"(?:^|\s)/\s*db\b")
_BOX_ONLY = re.compile(r"\b(?:ures|csak a|csak)\s+doboz\w*|\bdoboza\s*$|\bdoboz(?:a)?\s*(?:elado|kiado)\b|"
                       r"\b(?:box only|empty box)\b|^\W*(?:eredeti\s+|gyari\s+)?doboz\b")
_LOCKED = re.compile(r"\b(?:icloud\w*|aktivalasi zar\w*|fiok ?zar\w*|google ?fiok\w* zar\w*|frp ?zar\w*|zarolt\w*|"
                     r"lopott\w*|feketelist\w*|blacklist\w*)\b")
_LOCK_DENIED = re.compile(r"\b(?:nincs|nem|mentes\w*|nelkul|free|kijelentkez\w*|torolve|torolt|lekotve|off)\b")


def locked(text: str) -> bool:
    """Zárolt telefon (iCloud, aktiválási vagy Google-fiók zár, lopott), és nincs tagadva („iCloud mentes”)."""
    folded = fold(text or "")
    for match in _LOCKED.finditer(folded):
        if not _LOCK_DENIED.search(folded[max(0, match.start() - 12):match.end() + 14]):
            return True
    return False


def unusable(text: str) -> str:
    """Miért nem egy működő, egy darab, meghatározott termék ára (az összehasonlításnál ez a kizárás oka): kereső- vagy
    cserehirdetés, csomag vagy több darab, alkatrész vagy hibás, a termék doboza, zárolt telefon, részletfizetési vagy
    bérleti összeg, darabár, árlista (több méret), kiegészítő, „-tól” ár, vagy több termék egy hirdetésben. Üres, ha
    az ára használható."""
    folded = fold(text or "")
    if _WANTED_RE.search(folded) or (_SWAP_RE.search(folded) and not _SALE_RE.search(folded)):
        return "kereső- vagy cserehirdetés"
    if _INSTALLMENT.search(folded):
        return "részletfizetési vagy bérleti összeg, nem a vételár"
    if _PER_PIECE.search(folded):
        return "darabár, nem az egész ára"
    if _BOX_ONLY.search(folded):
        return "a termék doboza, nem a termék"
    if locked(text):
        return "zárolt (iCloud, aktiválási vagy fiókzár), nem eladható működő termék"
    if len(capacities(_words(text))) >= 3:
        return "több méret egy hirdetésben (az ár valamelyiké, vagy a legolcsóbbé)"
    if _LOT_RE.search(folded) or _PIECES.search(folded) or _HEAP.search(folded):
        return "csomag vagy több darab egy áron"
    if _PARTS_RE.search(folded):
        return "alkatrész vagy hibás"
    if _ACCESSORY.search(folded) or any(_FOR_IT.match(w) for w in _words(text)):
        return "kiegészítő, nem maga a termék"
    if from_price(text):
        return "„-tól” ár: nem egy meghatározott darab ára"
    if several_products(text):
        return "több termék egy hirdetésben"
    return ""


# „-tól” ár („90 000 Ft-tól”, „25e-től”, „árak 5000-től”): több darab vagy változat közül a legolcsóbb ára, nem egy
# meghatározott darabé. Az évszám („2019-től használom”) és a kis szám („10-től 18-ig”) nem ilyen.
_FROM_PRICE = re.compile(r"(\d[\d .]*\d|\d)\s*(ft|huf|e|ezer|,-)?\s*-?\s*tol\b")


def from_price(text: str) -> bool:
    for match in _FROM_PRICE.finditer(fold(text or "")):
        number = int(re.sub(r"\D", "", match.group(1)) or 0)
        if match.group(2) or (number >= 100 and not 1900 <= number <= 2099):
            return True
    return False


def several_products(text: str) -> bool:
    """Több különböző termék egy hirdetésben, egy áron („RTX 3060 / 3070 / 3080”, „Ryzen 5 5600X + B550 alaplap”): az
    ár nem egy meghatározott darabé, és a címből nem rendelhető egyértelműen egyikhez sem. Két valódi termék kell hozzá:
    két videókártya-lapka egy videókártya-családdal, vagy a felsorolás (/, +, vessző, „és”, „vagy”) legalább két
    részében más-más típusjel. Egy termék több szava egynek számít (az „Intel Core i5-10400F”: i5 és 10400F, a „16GB DDR4
    3200 CL16”), és a felsorolás típusjel nélküli része („dobozos”, „RGB”, „AM4”, „GDDR6”) nem külön termék."""
    words = _words(text)
    if sum(1 for w in _model_tokens(words) if _GPU_CHIP.match(w)) >= 2 and set(words) & GPU_FAMILIES:
        return True
    parts = {tuple(tokens) for tokens in (_model_tokens(_words(part))
                                          for part in re.split(r"[/+,;]|\b(?:es|vagy)\b", fold(text or ""))) if tokens}
    return len(parts) >= 2


# Műszaki jellemző, nem típusjel: memóriatípus (DDR4, GDDR6X), késleltetés (CL16), foglalat (AM4, LGA1700), csatoló és
# szabvány (PCIe4, USB3, HDMI21, SATA3, WiFi6, Gen4), sávszélesség (x16).
_SPEC = re.compile(r"^(?:(?:lp|g)?ddr\d\w*|cl\d{1,2}|am[2-5]|lga\d{3,4}|pcie\d*|usb\d\w*|hdmi\d\w*|sata\d|wifi\d\w*|"
                   r"gen\d|x\d{1,2})$")
# Ezek előtt a szám mennyiség, nem típusjel („3200 MHz”, „256 bit”).
_AFTER_NUMBER = _UNIT_WORDS | {"bit", "fps", "rpm", "dpi", "mp", "mpx"}


def _model_tokens(words: list[str]) -> list[str]:
    """A cím típusjelnek látszó szavai (several_products): van bennük szám, és nem mértékegység, kapacitás,
    memóriakészlet vagy műszaki jellemző; a puszta szám csak 3–5 jegyű, nem évszám, és nem egy mértékegység előtt áll."""
    out: list[str] = []
    for index, word in enumerate(words):
        if not re.search(r"\d", word) or _UNIT.match(word) or _CAPACITY.match(word) or _KIT.match(word) \
                or _SPEC.match(word):
            continue
        if word.isdigit() and (not 3 <= len(word) <= 5 or 1990 <= int(word) <= 2039
                               or (index + 1 < len(words) and words[index + 1] in _AFTER_NUMBER)):
            continue
        if word not in out:
            out.append(word)
    return out


_CAPACITY = re.compile(r"^(\d{1,4})(gb|tb)$")


def capacities(words: list[str]) -> list[str]:
    """A tárhely vagy a memória mérete a cím szavaiból („16gb”, „1tb”; a „16 GB” két szóban is), egységes alakban (1000
    GB = 1 TB). Ez a termék változata (RTX 5060 Ti 8 vagy 16 GB, 500 GB-os vagy 1 TB-os SSD, 128 vagy 256 GB-os
    iPhone), ami az árát lényegesen befolyásolja."""
    out: list[str] = []
    for index, word in enumerate(words):
        match = _CAPACITY.match(word)
        if match:
            number, unit = int(match.group(1)), match.group(2)
        elif word.isdigit() and len(word) <= 4 and index + 1 < len(words) and words[index + 1] in ("gb", "tb"):
            number, unit = int(word), words[index + 1]
        else:
            continue
        if number <= 0:
            continue
        if unit == "gb" and number >= 1000 and number % 1000 == 0:
            number, unit = number // 1000, "tb"
        token = f"{number}{unit}"
        if token not in out:
            out.append(token)
    return out


def is_capacity(word: str) -> bool:
    return bool(_CAPACITY.match(word))


def sizes_of(text: str) -> set[str]:
    """A szövegben (címben, keresőszóban) lévő kapacitások egységes alakban („16 GB” → „16gb”, „1000GB” → „1tb”)."""
    return set(capacities(_words(text)))


# A kapacitás fajtája a mellette álló szóból: memória (RAM, a videókártyáé is) vagy tárhely.
_RAM_WORDS = ("ram", "memori", "memory", "ddr", "lpddr", "gddr", "vram")
_STORAGE_WORDS = ("ssd", "hdd", "nvme", "tarhel", "rom", "emmc", "storage", "hattertar", "sata")


def _kind_of(word: str) -> str:
    return "ram" if word.startswith(_RAM_WORDS) else "storage" if word.startswith(_STORAGE_WORDS) else ""


def _gigabytes(size: str) -> int:
    match = _CAPACITY.match(size)
    return int(match.group(1)) * (1000 if match.group(2) == "tb" else 1) if match else 0


def sized(text: str) -> list[tuple[str, str]]:
    """A kapacitások a fajtájukkal: [(„16gb”, „ram” | „storage” | „”)]. A fajta a mellette álló szóból jön („16GB RAM”,
    „512GB SSD”, „RAM: 8GB”, „12GB GDDR6”); ha egy címben két méret van, és csak az egyiknek tudni a fajtáját, a másik
    a másik fajta, és ha egyikét sem, de nagyon különböznek („8GB 256GB”), a kisebb a RAM."""
    words = _words(text)
    out: list[tuple[str, str]] = []
    used: set[int] = set()                     # a már egy méretet leíró szavak (egy szó csak egy méretet jelöl)
    for index, word in enumerate(words):
        match = _CAPACITY.match(word)
        span = 1
        if match is None and word.isdigit() and len(word) <= 4 and index + 1 < len(words) \
                and words[index + 1] in ("gb", "tb"):
            match, span = _CAPACITY.match(word + words[index + 1]), 2
        if match is None or int(match.group(1)) <= 0:
            continue
        size = capacities([match.group(0)])[0]
        before, after = index - 1, index + span
        before_kind = _kind_of(words[before]) if before >= 0 and before not in used else ""
        after_kind = _kind_of(words[after]) if after < len(words) else ""
        follows = after + 1 < len(words) and bool(_CAPACITY.match(words[after + 1]) or words[after + 1].isdigit())
        # „RAM: 16GB, SSD: 512GB”: az utána álló szó a következő méreté, ha utána egy újabb méret jön
        if before_kind and (not after_kind or follows):
            kind = before_kind
            used.add(before)
        else:
            kind = after_kind
            if after_kind:
                used.add(after)
        if all(size != known for known, _k in out):
            out.append((size, kind))
    if len(out) == 2:
        (first, first_kind), (second, second_kind) = out
        if first_kind and not second_kind:
            out[1] = (second, "storage" if first_kind == "ram" else "ram")
        elif second_kind and not first_kind:
            out[0] = (first, "storage" if second_kind == "ram" else "ram")
        elif not first_kind and not second_kind:
            small, large = sorted(out, key=lambda entry: _gigabytes(entry[0]))
            if _gigabytes(small[0]) <= 64 and _gigabytes(large[0]) >= 4 * _gigabytes(small[0]):
                out = [(size, "ram" if size == small[0] else "storage") for size, _k in out]
    return out


def size_conflict(wanted: list[tuple[str, str]], have: list[tuple[str, str]], strict: bool = False) -> bool:
    """Más változat-e a hirdetés a kapacitása szerint (sized): ugyanannak a fajtának (RAM, tárhely) más a mérete; egy
    ismeretlen fajtájú méret, ami a másik oldalon nincs meg (és a fajtája a nagyságából sem válik el: legfeljebb 48 GB
    inkább RAM, fölötte tárhely). A RAM tehát nem számít egy másik tárhely-változatnak. strict (az
    árösszehasonlításhoz): a keresett minden méretnek benne is kell lennie a hirdetésben."""
    def group(entries: list[tuple[str, str]]) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {"ram": set(), "storage": set(), "": set()}
        for size, kind in entries:
            out[kind].add(size)
        return out

    def guess(size: str) -> str:
        return "ram" if _gigabytes(size) <= 48 else "storage"

    want, got = group(wanted), group(have)
    want_all, got_all = {s for s, _k in wanted}, {s for s, _k in have}
    for kind in ("ram", "storage"):
        if want[kind] and got[kind] and got[kind] - want[kind]:
            return True
    for size in got[""] - want_all:            # a hirdetés ismeretlen fajtájú mérete, ami a keresésben nincs
        if want[""] or want[guess(size)]:
            return True
    for size in want[""] - got_all:            # a keresés ismeretlen fajtájú mérete, ami a hirdetésben nincs
        if got[""] or got[guess(size)]:
            return True
    return strict and not want_all <= got_all


def product_key(text: str, brands: set[str] | frozenset[str] = frozenset()) -> tuple[str, str] | None:
    """A termékkulcs egy hirdetés címéből: (kulcs, név). A kulcs a rendezett szavak (így a sorrend nem számít), a név
    ahogy a címben jönnek. Benne van a termékcsalád (vagy, ha nincs, a márka), a típusjel (szám vagy számot tartalmazó
    szó), a típus jelzői (Ti, Pro, OLED…), a fajtája (kontroller, játék), és ha a címben benne van, a kapacitás (16gb,
    1tb: más változat, más ár). None, ha a címből nem derül ki biztosan a termék (pl. nincs benne típusjel), vagy kettőnél
    több kapacitás van benne (több termék). brands: a tanult márkák (a BRANDS mellé)."""
    words = _words(text)
    picked: list[str] = []
    known = BRANDS | brands if brands else BRANDS
    memory = ram_context(words)
    if any(_CPU_TIER.match(w) for w in words):
        known = known - CPU_BRANDS             # „Intel Core i5-10400F” = „i5 10400F”
    families = [w for w in words if w in FAMILIES]
    brand = next((w for w in words if w in known), "") if not families else ""
    model = qualified = False
    for index, word in enumerate(words):
        before = words[index - 1] if index else ""
        after = words[index + 1] if index + 1 < len(words) else ""
        if word in _NOISE:
            continue
        if memory and word in _RAM_FORMS:
            term = _RAM_FORMS[word]            # SO-DIMM, ECC, regisztrált, kit: más termék (más ár)
        elif word in FAMILIES or word == brand:
            term = word
        elif word in MODIFIERS:
            term, qualified = word, True
        elif word in TYPE_WORDS:
            term = TYPE_WORDS[word]
            if term in ("kontroller", "jatek") and families and (before.isdigit() or before in ("es", "plusz")) and \
                    index > 1:
                continue                       # „PS5 + 2 kontroller”: a konzol plusz tartozéka, nem kontroller-hirdetés
            qualified = qualified or term == "kontroller"
        elif word in SERIES:
            if not re.search(r"\d", after):
                continue
            term = word
        elif re.search(r"\d", word):
            if _UNIT.match(word) or (word.isdigit() and (len(word) > 5 or set(word) == {"0"} or after in _UNIT_WORDS)):
                continue
            if word.isdigit() and families and TYPE_WORDS.get(after) in ("kontroller", "jatek") and index > 1:
                continue                       # „Switch OLED + 3 játék”: a darabszám nem típusjel
            # rövid szám csak a család, a márka, egy jelző vagy egy sorozatnév után típusjel („iPhone 13”, „Air Max 90”,
            # „Note 10”), különben inkább méret vagy darabszám; a márka utáni 30–56 is inkább méret („Nike 42”)
            if word.isdigit() and len(word) <= 2:
                if not (before in FAMILIES or before in known or before in MODIFIERS or before in SERIES):
                    continue
                if before in known and before not in FAMILIES and 30 <= int(word) <= 56:
                    continue
            term, model = word, True
        else:
            continue
        if term not in picked:
            picked.append(term)
    if not picked or not (model or (qualified and any(f in FAMILY_COMPLETE for f in families))):
        return None
    if len(picked) == 1 and not (model and len(picked[0]) >= 5 and not picked[0].isdigit()):
        return None
    if set(families) & GPU_FAMILIES and not {"laptop", "pc"} & set(picked):
        # videókártya: a lapka, a jelzője és a fajtája marad, a kártyagyártó típusneve (G1, F15…) nem
        kept = [t for t in picked if t in FAMILIES or t in MODIFIERS or t in TYPE_WORDS.values() or _GPU_CHIP.match(t)]
        if any(_GPU_CHIP.match(t) for t in kept):
            picked = kept
    if any(len(t) >= 4 and re.search(r"\d", t) and not _KIT.match(t) for t in picked):
        # a hosszú típusjel mellett az egy számjegy csak a szint („Ryzen 5 5600X” = „Ryzen 5600X”)
        picked = [t for t in picked if not (t.isdigit() and len(t) == 1)]
    sizes = capacities(words)
    kits = [_KIT.match(t) for t in picked if _KIT.match(t)]
    for kit in kits:                           # a „16GB (2x8GB)” összege nem külön változat
        total = f"{int(kit.group(1)) * int(kit.group(2))}gb"
        sizes = [size for size in sizes if size != total]
    if len(sizes) > 2:
        return None
    return " ".join(sorted(picked + sizes)), " ".join(picked + sizes)


# Videókártyák, amelyek csak egy memóriamérettel készültek (a gyártói adatlapok szerint): ezeknél a kapacitás nélküli cím
# („GTX 1080”) is ez a változat. Más terméknél az árbázis pillanatnyi tartalmából nem következik, hogy nincs más változat
# (az RTX 5060 Ti 8 és 16 GB-os is), ezért ott a kapacitás nélküli címek nem számítanak egy változathoz (implied_size).
_SINGLE_CAPACITY = (
    ("GTX 970", "4gb"), ("GTX 980", "4gb"), ("GTX 980 Ti", "6gb"), ("GTX 1050 Ti", "4gb"), ("GTX 1070", "8gb"),
    ("GTX 1070 Ti", "8gb"), ("GTX 1080", "8gb"), ("GTX 1080 Ti", "11gb"), ("GTX 1650", "4gb"), ("GTX 1650 Super", "4gb"),
    ("GTX 1660", "6gb"), ("GTX 1660 Super", "6gb"), ("GTX 1660 Ti", "6gb"), ("RTX 2060 Super", "8gb"),
    ("RTX 2070", "8gb"), ("RTX 2070 Super", "8gb"), ("RTX 2080", "8gb"), ("RTX 2080 Super", "8gb"),
    ("RTX 2080 Ti", "11gb"), ("RTX 3060 Ti", "8gb"), ("RTX 3070", "8gb"), ("RTX 3070 Ti", "8gb"),
    ("RTX 3080 Ti", "12gb"), ("RTX 3090", "24gb"), ("RTX 3090 Ti", "24gb"), ("RTX 4060", "8gb"), ("RTX 4070", "12gb"),
    ("RTX 4070 Super", "12gb"), ("RTX 4070 Ti", "12gb"), ("RTX 4070 Ti Super", "16gb"), ("RTX 4080", "16gb"),
    ("RTX 4080 Super", "16gb"), ("RTX 4090", "24gb"), ("RTX 5060", "8gb"), ("RTX 5070", "12gb"), ("RTX 5070 Ti", "16gb"),
    ("RTX 5080", "16gb"), ("RTX 5090", "32gb"), ("RX 5700", "8gb"), ("RX 5700 XT", "8gb"), ("RX 6600", "8gb"),
    ("RX 6600 XT", "8gb"), ("RX 6650 XT", "8gb"), ("RX 6700 XT", "12gb"), ("RX 6750 XT", "12gb"), ("RX 6800", "16gb"),
    ("RX 6800 XT", "16gb"), ("RX 6900 XT", "16gb"), ("RX 6950 XT", "16gb"), ("RX 7600", "8gb"), ("RX 7700 XT", "12gb"),
    ("RX 7800 XT", "16gb"), ("RX 7900 XT", "20gb"), ("RX 7900 XTX", "24gb"), ("RX 9070", "16gb"), ("RX 9070 XT", "16gb"))
_IMPLIED = {product_key(name)[0]: size for name, size in _SINGLE_CAPACITY}


def implied_size(key: str) -> str:
    """Egy kapacitás nélküli termékkulcs egyetlen kapacitása, ha a típus csak azzal készült (_SINGLE_CAPACITY); üres,
    ha nem tudni (akkor a kapacitás nélküli cím bármelyik változat lehet)."""
    return _IMPLIED.get(key, "")


# Ezek a szavak a modell által adott név elején sem márkák (nem tanulja meg őket).
_NOT_BRANDS = {"elado", "uj", "hasznalt", "eredeti", "gyari", "noi", "ferfi", "gyerek", "fekete", "feher", "vezetek",
               "nelkuli", "okos", "mini", "gamer", "gaming", "retro", "vintage", "profi", "digitalis", "elektromos",
               "akkus", "vezeteknelkuli", "bluetooth", "wifi", "usb", "led", "smart", "the", "set", "szett"}
LEARN_AFTER = 3            # ennyiszer kell egy ismeretlen márkának előfordulnia a modell elnevezéseiben


def basis_entry(sample: dict[str, Any], source: str = "árbázis") -> dict[str, Any]:
    """Egy összehasonlító ár a találat „mihez mérte” listájához: cím, ár, oldal (kulcs), webcím, és honnan (az árbázisból,
    vagy most találta: „most”); ha azóta eltűnt (lehet, hogy elkelt, de nem tudni, mennyiért), azt is; az eladó (v), ha
    tudjuk."""
    entry = {"t": str(sample.get("t") or ""), "p": int(sample["p"]), "s": str(sample.get("site") or ""),
             "u": str(sample.get("url") or ""), "src": source}
    if sample.get("gone"):
        entry["gone"] = True
    if sample.get("seller"):
        entry["v"] = str(sample["seller"])
    return entry


SELLER_CAP = 2             # egy eladótól legfeljebb ennyi ár számít egy termék szokásos árába


def _same_title(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", fold(text or "")).split())


def independent(samples: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """A független árak egy termékhez: ugyanaz a hirdetés több helyen (ugyanaz a cím és az ár, pl. a Hardveraprón és a
    Jófogáson is fent van) egyszer számít; egy eladótól legfeljebb SELLER_CAP (sok hasonló hirdetése nem sok független
    ár: egy kereskedő a saját árszintjét mutatja). Az első marad (a hívó sorrendje: a frissebb elöl). (megtartottak,
    kizártak: a mintájuk a why okkal). A minta book.samples- vagy basis_entry-alakú (t, p, site/s, seller/v)."""
    kept, out = [], []
    titles: dict[tuple[str, int], dict[str, Any]] = {}
    sellers: dict[str, int] = {}
    for sample in samples:
        title = _same_title(str(sample.get("t") or ""))
        twin = titles.get((title, int(sample["p"]))) if len(title) >= 8 else None
        if twin is not None:
            out.append({**sample, "why": "valószínűleg ugyanaz a hirdetés, máshol is fent van (egyszer számít)"})
            continue
        seller = fold(str(sample.get("seller") or sample.get("v") or "")).strip()
        if seller and sellers.get(seller, 0) >= SELLER_CAP:
            out.append({**sample, "why": f"ugyanattól az eladótól ({sample.get('seller') or sample.get('v')}): "
                                         f"legfeljebb {SELLER_CAP} hirdetése számít"})
            continue
        if seller:
            sellers[seller] = sellers.get(seller, 0) + 1
        titles[(title, int(sample["p"]))] = sample
        kept.append(sample)
    return kept, out


def fake_prone(key: str) -> bool:
    return bool(set(key.split()) & FAKE_PRONE)


class PriceBook:
    """A termékenkénti árak (munka/arbazis.json). Egy termék: {name, samples: [{p: ár, at: először látva, last: utoljára
    látva, url, site, checked: utoljára újranézve, gone: mikor tűnt el}]}."""

    def __init__(self, root: Path, clock: Any = time.time):
        self.path = Path(root) / "arbazis.json"
        self.clock = clock
        self._lock = threading.RLock()
        self._dirty = False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        keys = data.get("keys")
        self._keys: dict[str, dict[str, Any]] = {k: v for k, v in (keys or {}).items()
                                                 if isinstance(v, dict) and isinstance(v.get("samples"), list)}
        # tanult márkák (a modell elnevezéseiből) és a még nem elég gyakori jelöltek
        self.brands: set[str] = {str(b) for b in data.get("brands") or [] if isinstance(b, str)}
        self._brand_counts: dict[str, int] = {str(k): int(v) for k, v in (data.get("brand_counts") or {}).items()
                                              if isinstance(v, int)}
        self._by_base: dict[str, list[str]] | None = None   # kapacitás nélküli kulcs → a változatai (variants)
        self._where: dict[str, str] | None = None           # hirdetés → a terméke, ahol az ára van (_locate)
        if data.get("kv") != KEY_VERSION and self._keys:
            self._rekey()

    def _rekey(self) -> None:
        """A régi kulcsok újraképzése a nevükből (a mostani product_key szerint), és az így egybeeső termékek árainak
        összevonása (egy hirdetés egyszer, a legutóbb látott alakjában; termékenként legfeljebb MAX_SAMPLES). Egy ár a
        saját címe szerinti változatához kerül, ha az a termék pontosabb alakja (a kulcs minden szava benne van, és még
        egy változat is: pl. a régi „580 rx” alatt a „Sapphire RX580 4G” a „4gb 580 rx”-hez; a „4G” régen nem
        számított méretnek); más esetben (pl. amit a modell nevezett meg) marad, ahol volt."""
        merged: dict[str, dict[str, Any]] = {}
        for old, entry in self._keys.items():
            found = self.key(str(entry.get("name") or old))
            key, label = found if found is not None else (old, str(entry.get("name") or old))
            for sample in entry["samples"]:
                title = str(sample.get("t") or "")
                own = self.key(title) if title and usable(title) else None
                place, name = (own if own is not None and own[0] != key and set(key.split()) < set(own[0].split())
                               else (key, label))
                target = merged.setdefault(place, {**entry, "name": name, "samples": []})
                ident = listing_key(str(sample.get("url") or ""))
                known = next((s for s in target["samples"] if listing_key(str(s.get("url") or "")) == ident), None)
                if known is None:
                    target["samples"].append(sample)
                elif float(sample.get("last") or 0) > float(known.get("last") or 0):
                    target["samples"][target["samples"].index(known)] = sample
                target["seen"] = max(float(target.get("seen") or 0), float(entry.get("seen") or 0))
        for target in merged.values():
            target["samples"] = sorted(target["samples"], key=lambda s: -float(s.get("last") or 0))[:MAX_SAMPLES]
        self._keys = {key: entry for key, entry in merged.items() if entry["samples"]}
        self._where = None
        self._by_base = None
        self._dirty = True

    def _locate(self) -> dict[str, str]:
        """Hirdetés (listing_key) → a termék kulcsa, ahol az ára van: egy hirdetésnek egy ára van (a legutóbbi állapota
        szerint). Lustán épül, és a nagyobb változások után újra."""
        if self._where is None:
            self._where = {listing_key(str(s.get("url") or "")): key for key, entry in self._keys.items()
                           for s in entry["samples"] if s.get("url")}
        return self._where

    def _drop(self, ident: str, keep: str = "") -> None:
        """Egy hirdetés korábbi ára kikerül, mert a friss állapota felülírja: ha most nem használható ár (hibás lett,
        „-tól” ár, csomag…), mindenhonnan; ha a címe szerint most másik termék, a régi helyéről (keep: az új helye)."""
        where = self._locate()
        key = where.get(ident)
        if key is None or key == keep:
            return
        entry = self._keys.get(key)
        if entry is not None:
            entry["samples"] = [s for s in entry["samples"] if listing_key(str(s.get("url") or "")) != ident]
            self._dirty = True
        where.pop(ident, None)
    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"kv": KEY_VERSION, "keys": self._keys, "brands": sorted(self.brands),
                                       "brand_counts": self._brand_counts}, ensure_ascii=False, separators=(",", ":")),
                           encoding="utf-8")
            os.replace(tmp, self.path)
            self._dirty = False

    def key(self, text: str) -> tuple[str, str] | None:
        """A termékkulcs a tanult márkákkal együtt (lásd product_key)."""
        return product_key(text, frozenset(self.brands))

    def query(self, text: str) -> str:
        """Keresőszó egy hirdetés termékéhez: a címének azok a szavai, amelyekből a termékkulcs lett, ahogy a címben
        írták („Sony WH-1000XM4”: az oldalak keresője a kötőjeles alakot találja meg). Üres, ha a címből nincs kulcs."""
        found = self.key(text)
        if found is None:
            return ""
        wanted = set(found[0].split())
        picked = [token for token in text.split() if set(_words(token)) & wanted]
        have = set(_words(" ".join(picked)))
        sizes = [size.upper() for size in capacities(_words(text)) if size not in have]   # a „16 GB” két szóban
        return " ".join(picked + sizes) if picked else found[1]

    def learn_name(self, name: str) -> str:
        """Öntanulás: ha a modell egy terméket olyan márkával nevez meg, amit a kulcsfelismerés nem ismer (és emiatt
        nem kap kulcsot, pl. „Anker PowerCore 20000”), feljegyzi; LEARN_AFTER előfordulás után megtanulja, és onnantól
        modell nélkül is felismeri. Visszaadja a most megtanult márkát (különben üres)."""
        words = _words(name)
        first = words[0] if words else ""
        if (len(first) < 3 or not first.isalpha() or first in BRANDS or first in self.brands or first in FAMILIES
                or first in MODIFIERS or first in SERIES or first in TYPE_WORDS or first in _NOISE
                or first in _NOT_BRANDS or self.key(name) is not None
                or product_key(name, frozenset(self.brands | {first})) is None):
            return ""
        with self._lock:
            self._brand_counts[first] = self._brand_counts.get(first, 0) + 1
            self._dirty = True
            if self._brand_counts[first] < LEARN_AFTER:
                return ""
            self._brand_counts.pop(first, None)
            self.brands.add(first)
        return first

    def add(self, rows: list[dict[str, Any]], name: str = "") -> int:
        """A hirdetések árai a termékükhöz (a cím szerint, vagy ha megadod, a name szerint: pl. ahogy a modell nevezte
        meg). A licit nem ár (csak a mostani ajánlat). Egy hirdetésnek egy ára van, a friss állapota szerint: ha már
        nem használható ár (pl. a címébe bekerült, hogy hibás), a korábbi ára sem számít tovább; ha a címe szerint most
        másik termék, ott lesz. Visszaadja, hányat tett be."""
        now, added = self.clock(), 0
        named = self.key(name) if name else None
        with self._lock:
            for row in rows:
                text, url, price = str(row.get("text") or ""), str(row.get("url") or ""), int(row.get("price") or 0)
                title = str(row.get("title") or text)   # a cím dönt, ha van (a kártyán a város, az eladó neve is ott van)
                if row.get("auction") or not url or price <= 0:
                    continue
                ident = listing_key(url)
                if not usable(title):
                    self._drop(ident)          # a friss állapota szerint már nem használható ár: a régi sem számít
                    continue
                found = named or self.key(title)
                if found is None:
                    continue
                key, label = found
                self._drop(ident, keep=key)    # a címe szerint most másik termék: a régi helyéről kikerül
                if key not in self._keys:
                    self._by_base = None       # új termék: a változatok jegyzéke újraépül
                entry = self._keys.setdefault(key, {"name": label, "samples": []})
                sample = next((s for s in entry["samples"] if listing_key(s.get("url", "")) == ident), None)
                if sample is None:
                    sample = {"at": now, "url": url, "site": str(row.get("platform") or "")}
                    entry["samples"].append(sample)
                    self._locate()[ident] = key
                if row.get("seller"):
                    sample["v"] = str(row["seller"])[:40]
                elif sample.get("p") and price < int(sample["p"]) * (1 - PRICE_CUT):
                    sample["cuts"] = int(sample.get("cuts") or 0) + 1     # leárazta (az alkuhoz: engedni szokott)
                    sample.setdefault("p0", int(sample["p"]))
                sample.update(p=price, last=now, t=" ".join(str(row.get("title") or text).split())[:TITLE_KEEP])
                sample.pop("gone", None)
                if len(entry["samples"]) > MAX_SAMPLES:
                    entry["samples"].sort(key=lambda s: -float(s.get("last") or 0))
                    del entry["samples"][MAX_SAMPLES:]
                    self._where = None
                entry["seen"] = now
                added += 1
            if len(self._keys) > MAX_KEYS:
                for key in sorted(self._keys, key=lambda k: float(self._keys[k].get("seen") or 0))[:len(self._keys) - MAX_KEYS]:
                    self._keys.pop(key, None)
                self._by_base = self._where = None
            self._dirty = self._dirty or bool(added)
        return added

    def samples(self, key: str, exclude: str = "") -> list[dict[str, Any]]:
        """Egy termék friss árai (MAX_AGE-en belül látva): {p: ár, site: oldal, url, gone, at: először látva, last:
        utoljára látva, cuts: hányszor árazta le, p0: az első ára, t: a hirdetés címének eleje}. Az exclude hirdetés
        (pl. maga a jelölt) nincs köztük."""
        now = self.clock()
        skip = listing_key(exclude) if exclude else ""
        with self._lock:
            return [{"p": int(s["p"]), "site": str(s.get("site") or ""), "url": str(s.get("url") or ""),
                     "gone": bool(s.get("gone")), "at": float(s.get("at") or 0), "last": float(s.get("last") or 0),
                     "cuts": int(s.get("cuts") or 0), "p0": int(s.get("p0") or s["p"]), "t": str(s.get("t") or ""),
                     **({"seller": str(s["v"])} if s.get("v") else {})}
                    for s in (self._keys.get(key) or {}).get("samples") or []
                    if now - float(s.get("last") or 0) <= MAX_AGE and not (skip and listing_key(s.get("url", "")) == skip)]

    def reference(self, key: str, exclude: str = "") -> dict[str, Any] | None:
        """A szokásos ár egy termékre (_reference). Ha a kulcsban nincs kapacitás (a címből nem derült ki, pl. „RTX 5060
        Ti”), de a terméknek vannak ilyen változatai („… 8gb”, „… 16gb”), óvatosan a legolcsóbb változat szokásos árával
        számol (a name és a key azé a változaté): így egy 8 GB-os kártya nem látszik olcsó 16 GB-osnak. Ha csak egy
        típus csak egy kapacitással készült (a GTX 1080 mind 8 GB-os: implied_size), a kapacitás nélküli címek is az; az
        árbázis tartalmából ez nem következik (attól, hogy csak 8 GB-osat láttunk, lehet 16 GB-os is)."""
        base = " ".join(word for word in key.split() if not is_capacity(word))
        implied = implied_size(base)
        if base != key:                        # kapacitásos kulcs: a kapacitás nélküliek csak, ha a típus ilyen
            size = " ".join(word for word in key.split() if is_capacity(word))
            return self._reference(key, exclude, also=[base] if implied and implied == size else [])
        if implied:
            return self._reference(key, exclude, also=[" ".join(sorted(key.split() + [implied]))])
        sized = self.variants(base)
        found = self._reference(key, exclude)
        if not sized:
            return found
        options = [option for option in [found, *(self._reference(v, exclude) for v in sized)] if option]
        return min(options, key=lambda option: option["price"]) if options else None

    @staticmethod
    def canon(key: str) -> str:
        """Egy termék egy kulcsa: ha a típus csak egy kapacitással készült (implied_size), a kapacitás nélküli alak
        („1080 8gb gtx” → „1080 gtx”), így az elrejtés és a vétel ugyanarra a termékre számít, akárhogy írták."""
        base = " ".join(word for word in key.split() if not is_capacity(word))
        size = " ".join(word for word in key.split() if is_capacity(word))
        return base if size and implied_size(base) == size else key

    def family(self, key: str) -> list[tuple[str, str]]:
        """Egy termék és a kapacitás-változatai az árbázisban: [(kulcs, kapacitás)], a kapacitás nélkülié üres
        kapacitással, vagy ha a típus csak egy kapacitással készült (implied_size), azzal."""
        base = " ".join(word for word in key.split() if not is_capacity(word))
        with self._lock:
            out = [(base, implied_size(base))] if base in self._keys else []
        return out + [(v, " ".join(w for w in v.split() if is_capacity(w))) for v in self.variants(base)]

    def variants(self, key: str) -> list[str]:
        """A kapacitás nélküli kulcs változatai a kapacitással (az árbázisban lévők); egy kapacitásos kulcsnak nincs."""
        words = key.split()
        if any(is_capacity(word) for word in words):
            return []
        with self._lock:
            if self._by_base is None:
                index: dict[str, list[str]] = {}
                for other in self._keys:
                    parts = other.split()
                    base = [word for word in parts if not is_capacity(word)]
                    if len(base) < len(parts):
                        index.setdefault(" ".join(base), []).append(other)
                self._by_base = index
            return list(self._by_base.get(" ".join(words), []))

    def _reference(self, key: str, exclude: str = "", also: list[str] | tuple = ()) -> dict[str, Any] | None:
        """A szokásos ár egy kulcsra: a friss árak középértéke (a kilógók nélkül), ha legalább MIN_SAMPLES van, és
        nem szórnak nagyon (price_stats: megbízható). Az exclude hirdetés (pl. maga a jelölt) nem számít bele. A sites:
        melyik oldalakon láttuk ezeket az árakat; a sold: az elkelt (újranézéskor eltűnt) hirdetések középára, ha
        legalább SOLD_MIN van (a hirdetési árnál többnyire alacsonyabb: ennyiért kel el ténylegesen). also: ugyanennek
        a terméknek más kulcsai (a kapacitás nélküli címek, reference), az árai ezekből is (egy hirdetés egyszer)."""
        samples = self.samples(key, exclude)
        if also:
            have = {listing_key(sample["url"]) for sample in samples}
            for other in also:
                for sample in self.samples(other, exclude):
                    if listing_key(sample["url"]) not in have:
                        have.add(listing_key(sample["url"]))
                        samples.append(sample)
        with self._lock:
            name = (self._keys.get(key) or {}).get("name") or key
        samples.sort(key=lambda s: -float(s.get("last") or 0))
        samples, twins = independent(samples)   # egy hirdetés egyszer, egy eladótól legfeljebb SELLER_CAP
        stats = price_stats([s["p"] for s in samples])
        if not stats or stats["n"] < MIN_SAMPLES or stats["confidence"] != "megbízható":
            return None
        kept = [s for s in samples if stats["min"] <= s["p"] <= stats["max"]]   # a kilógók (és oldaluk) nem számít
        found = {"price": round_price(stats["median"]), "n": stats["n"], "p25": stats["p25"], "p75": stats["p75"],
                 "name": name, "key": key, "sites": sorted({s["site"] for s in kept if s["site"]}),
                 # mihez mérte: a beszámított árak, és a kilógók (nagyon olcsó vagy drága: kimaradt a szokásos árból)
                 "basis": {"used": [basis_entry(s) for s in sorted(kept, key=lambda s: s["p"])],
                           "out": [{**basis_entry(s), "why": "kilógó ár (kimaradt a szokásos árból)"}
                                   for s in sorted(samples, key=lambda s: s["p"]) if s not in kept]
                           + [{**basis_entry(s), "why": s["why"]} for s in twins]}}
        sold = [s["p"] for s in kept if s["gone"]]
        if len(sold) >= SOLD_MIN:
            found["sold"] = {"n": len(sold), "median": round_price(statistics.median(sold))}
        return found

    def speed(self, key: str) -> dict[str, Any]:
        """Eladási sebesség az újranézett hirdetésekből: hányat néztünk meg (checked), hány tűnt el / kelt el (sold),
        és ezek átlagosan hány nap után (days; felső becslés: addigra biztosan elkelt); slow: hány volt fent még
        SLOW_DAYS nap után is."""
        with self._lock:
            samples = list((self._keys.get(key) or {}).get("samples") or [])
        sold = [(float(s["gone"]) - float(s["at"])) / 86400 for s in samples if s.get("gone")]
        alive = [(float(s["checked"]) - float(s["at"])) / 86400 for s in samples if s.get("checked") and not s.get("gone")]
        return {"checked": len(sold) + len(alive), "sold": len(sold),
                "days": round(statistics.median(sold)) if sold else None,
                "slow": sum(1 for days in alive if days >= SLOW_DAYS)}

    def revisit(self, limit: int, skip_sites: tuple[str, ...] = ()) -> list[tuple[str, str, str]]:
        """Melyik régebben látott hirdetéseket nézze meg újra (elkelt-e): (kulcs, webcím, cím). Termékenként egyet, a
        legtöbb árral bíró termékekből (azokra van a legtöbb esély, hogy jelölt lesz belőlük)."""
        now = self.clock()
        out: list[tuple[str, str, str]] = []
        with self._lock:
            for key, entry in sorted(self._keys.items(), key=lambda kv: -len(kv[1]["samples"])):
                if len(entry["samples"]) < MIN_SAMPLES:
                    break
                for sample in entry["samples"]:
                    age = now - float(sample.get("at") or now)
                    if (sample.get("gone") or sample.get("site") in skip_sites or age < REVISIT_AFTER
                            or age > REVISIT_UNTIL or now - float(sample.get("checked") or 0) < REVISIT_AGAIN
                            or now - float(sample.get("last") or 0) < REVISIT_AFTER):
                        continue
                    out.append((key, str(sample["url"]), str(entry.get("name") or key)))
                    break
                if len(out) >= limit:
                    break
        return out

    def revisited(self, key: str, url: str, gone: bool) -> None:
        now, ident = self.clock(), listing_key(url)
        with self._lock:
            for sample in (self._keys.get(key) or {}).get("samples") or []:
                if listing_key(sample.get("url", "")) == ident:
                    sample["checked"] = now
                    if gone:
                        sample["gone"] = now
                    self._dirty = True

    def lookup(self, text: str, limit: int = 6) -> list[dict[str, Any]]:
        """Mennyit ér? A beírt termék (pl. „RTX 3060”) kulcsa, és azok a termékek, amelyeknek ez a része (pl. „RTX
        3060 Ti”): a szokásos ár (ha van), hány ár alapján, és az eladási sebesség."""
        found = self.key(text) or (" ".join(sorted(set(_words(text)) - _NOISE)), text)
        wanted = set(found[0].split())
        with self._lock:
            keys = [k for k in self._keys if wanted <= set(k.split())]
            keys.sort(key=lambda k: (k != found[0], -len(self._keys[k]["samples"])))
            names = {k: self._keys[k].get("name") or k for k in keys}
            counts = {k: len(self._keys[k]["samples"]) for k in keys}
        out = []
        for key in keys[:limit]:
            out.append({"key": key, "name": names[key], "samples": counts[key], "reference": self.reference(key),
                        "speed": self.speed(key)})
        return out

    def name(self, key: str) -> str:
        with self._lock:
            return str((self._keys.get(key) or {}).get("name") or key)

    def home_site(self, key: str) -> str:
        """Melyik oldalon láttuk a legtöbbször (ott érdemes figyelni)."""
        with self._lock:
            sites = [str(s.get("site") or "") for s in (self._keys.get(key) or {}).get("samples") or []]
        sites = [site for site in sites if site]
        return max(set(sites), key=sites.count) if sites else ""

    def fast(self, limit: int = 10) -> list[tuple[str, dict[str, Any]]]:
        """Öntanulás: a gyorsan kelő termékek (legalább két újranézett hirdetésük eltűnt, átlagosan egy héten belül),
        amelyeknek van szokásos áruk; a legtöbbször elkeltek elöl."""
        with self._lock:
            keys = list(self._keys)
        out = []
        for key in keys:
            speed = self.speed(key)
            if speed["sold"] >= 2 and (speed["days"] if speed["days"] is not None else 99) <= 7 and self.reference(key):
                out.append((key, speed))
        out.sort(key=lambda kv: (-kv[1]["sold"], kv[1]["days"]))
        return out[:limit]

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._keys)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"products": len(self._keys), "prices": sum(len(e["samples"]) for e in self._keys.values()),
                    "ready": sum(1 for k in self._keys if len(self._keys[k]["samples"]) >= MIN_SAMPLES)}


def pace(speed: dict[str, Any]) -> str:
    """Milyen gyorsan kel az ilyen: „gyors” (az újranézettek legalább fele egy héten belül eltűnt), „lassú” (legfeljebb
    SLOW_RATE része tűnt el, és legalább kettő SLOW_DAYS nap után is fent volt), vagy „” (nem tudni, vagy átlagos)."""
    checked, sold = int(speed.get("checked") or 0), int(speed.get("sold") or 0)
    if checked < 2:
        return ""
    if sold and sold * 2 >= checked and (speed.get("days") or 0) <= 7:
        return "gyors"
    if sold <= checked * SLOW_RATE and int(speed.get("slow") or 0) >= 2:
        return "lassú"
    return ""


def speed_text(speed: dict[str, Any]) -> str:
    """„gyorsan kel: 3/4 elkelt, kb. 5 nap alatt” / „lassan kel: 2 hirdetés 2 hét után is fent volt” / „”."""
    if not speed.get("checked"):
        return ""
    if speed.get("sold"):
        pace = "gyorsan kel" if (speed.get("days") or 0) <= 7 and speed["sold"] * 2 >= speed["checked"] else "elkel"
        return f"{pace}: {speed['sold']}/{speed['checked']} újranézett hirdetés eltűnt, kb. {speed['days']} nap alatt"
    if speed.get("slow"):
        return f"lassan kel: {speed['slow']} hirdetés {SLOW_DAYS} nap után is fent volt"
    return ""
