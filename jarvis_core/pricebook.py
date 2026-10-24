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
MAX_AGE = 45 * 86400       # ennél régebben látott ár már nem számít a szokásos árba
MIN_SAMPLES = 5            # legalább ennyi ár kell egy szokásos árhoz
REVISIT_AFTER = 2 * 86400  # egy hirdetést legkorábban ennyi idő múlva néz meg újra (elkelt-e)
REVISIT_AGAIN = 3 * 86400  # ha akkor még fent volt, ennyi múlva újra
REVISIT_UNTIL = 30 * 86400 # ennyi után már nem nézi (ami egy hónapja fent van, az lassan kel)
SLOW_DAYS = 14             # ennyi nap után is fent: lassan kel
SOLD_MIN = 3               # ennyi elkelt (újranézéskor eltűnt) hirdetés ára kell az elkelt árhoz
PRICE_CUT = 0.03           # ennyivel olcsóbb lett egy már látott hirdetés: leárazta
SLOW_RATE = 0.25           # ha az újranézett hirdetéseknek legfeljebb ennyi része tűnt el: lassan kel

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
              "joycon": "kontroller", "jatek": "jatek", "jatekok": "jatek", "game": "jatek"}
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
            (r"\bseries ([xs])\b", r"series\1"), (r"\bxiaomi redmi\b", "redmi"))
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


def _words(text: str) -> list[str]:
    folded = fold(text or "")
    for pattern, replacement in _ALIASES:
        folded = re.sub(pattern, replacement, folded)
    folded = _PRICE_IN_TITLE.sub(" ", folded)
    folded = re.sub(r"(?<=[a-z0-9])-(?=[a-z0-9])", "", folded)      # WH-1000XM4 → wh1000xm4, i5-12400 → i512400
    return re.findall(r"[a-z0-9]+", folded)


def usable(text: str) -> bool:
    """Az ára egy működő, egy darab termék ára-e (és nem kereső-, csere- vagy csomaghirdetés, alkatrész, hibás,
    kiegészítő)."""
    folded = fold(text or "")
    if _WANTED_RE.search(folded) or (_SWAP_RE.search(folded) and not _SALE_RE.search(folded)):
        return False
    if _LOT_RE.search(folded) or _PIECES.search(folded) or _HEAP.search(folded) or _PARTS_RE.search(folded) \
            or _ACCESSORY.search(folded):
        return False
    return not any(_FOR_IT.match(w) for w in _words(text))


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


def product_key(text: str, brands: set[str] | frozenset[str] = frozenset()) -> tuple[str, str] | None:
    """A termékkulcs egy hirdetés címéből: (kulcs, név). A kulcs a rendezett szavak (így a sorrend nem számít), a név
    ahogy a címben jönnek. Benne van a termékcsalád (vagy, ha nincs, a márka), a típusjel (szám vagy számot tartalmazó
    szó), a típus jelzői (Ti, Pro, OLED…), a fajtája (kontroller, játék), és ha a címben benne van, a kapacitás (16gb,
    1tb: más változat, más ár). None, ha a címből nem derül ki biztosan a termék (pl. nincs benne típusjel), vagy kettőnél
    több kapacitás van benne (több termék). brands: a tanult márkák (a BRANDS mellé)."""
    words = _words(text)
    picked: list[str] = []
    known = BRANDS | brands if brands else BRANDS
    families = [w for w in words if w in FAMILIES]
    brand = next((w for w in words if w in known), "") if not families else ""
    model = qualified = False
    for index, word in enumerate(words):
        before = words[index - 1] if index else ""
        after = words[index + 1] if index + 1 < len(words) else ""
        if word in _NOISE:
            continue
        if word in FAMILIES or word == brand:
            term = word
        elif word in MODIFIERS:
            term, qualified = word, True
        elif word in TYPE_WORDS:
            term = TYPE_WORDS[word]
            qualified = qualified or term == "kontroller"
        elif word in SERIES:
            if not re.search(r"\d", after):
                continue
            term = word
        elif re.search(r"\d", word):
            if _UNIT.match(word) or (word.isdigit() and (len(word) > 5 or set(word) == {"0"} or after in _UNIT_WORDS)):
                continue
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
    sizes = capacities(words)
    if len(sizes) > 2:
        return None
    return " ".join(sorted(picked + sizes)), " ".join(picked + sizes)


# Ezek a szavak a modell által adott név elején sem márkák (nem tanulja meg őket).
_NOT_BRANDS = {"elado", "uj", "hasznalt", "eredeti", "gyari", "noi", "ferfi", "gyerek", "fekete", "feher", "vezetek",
               "nelkuli", "okos", "mini", "gamer", "gaming", "retro", "vintage", "profi", "digitalis", "elektromos",
               "akkus", "vezeteknelkuli", "bluetooth", "wifi", "usb", "led", "smart", "the", "set", "szett"}
LEARN_AFTER = 3            # ennyiszer kell egy ismeretlen márkának előfordulnia a modell elnevezéseiben


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

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"keys": self._keys, "brands": sorted(self.brands),
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
        meg). A licit nem ár (csak a mostani ajánlat). Visszaadja, hányat tett be."""
        now, added = self.clock(), 0
        named = self.key(name) if name else None
        with self._lock:
            for row in rows:
                text, url, price = str(row.get("text") or ""), str(row.get("url") or ""), int(row.get("price") or 0)
                if row.get("auction") or not url or price <= 0 or not usable(text):
                    continue
                found = named or self.key(str(row.get("title") or text))   # a cím: a kártyán a város, az eladó is ott van
                if found is None:
                    continue
                key, label = found
                if key not in self._keys:
                    self._by_base = None       # új termék: a változatok jegyzéke újraépül
                entry = self._keys.setdefault(key, {"name": label, "samples": []})
                ident = listing_key(url)
                sample = next((s for s in entry["samples"] if listing_key(s.get("url", "")) == ident), None)
                if sample is None:
                    sample = {"at": now, "url": url, "site": str(row.get("platform") or "")}
                    entry["samples"].append(sample)
                elif sample.get("p") and price < int(sample["p"]) * (1 - PRICE_CUT):
                    sample["cuts"] = int(sample.get("cuts") or 0) + 1     # leárazta (az alkuhoz: engedni szokott)
                    sample.setdefault("p0", int(sample["p"]))
                sample.update(p=price, last=now)
                sample.pop("gone", None)
                if len(entry["samples"]) > MAX_SAMPLES:
                    entry["samples"].sort(key=lambda s: -float(s.get("last") or 0))
                    del entry["samples"][MAX_SAMPLES:]
                entry["seen"] = now
                added += 1
            if len(self._keys) > MAX_KEYS:
                for key in sorted(self._keys, key=lambda k: float(self._keys[k].get("seen") or 0))[:len(self._keys) - MAX_KEYS]:
                    self._keys.pop(key, None)
                self._by_base = None
            self._dirty = self._dirty or bool(added)
        return added

    def samples(self, key: str, exclude: str = "") -> list[dict[str, Any]]:
        """Egy termék friss árai (MAX_AGE-en belül látva): {p: ár, site: oldal, url, gone, at: először látva, last:
        utoljára látva, cuts: hányszor árazta le, p0: az első ára}. Az exclude hirdetés (pl. maga a jelölt) nincs
        köztük."""
        now = self.clock()
        skip = listing_key(exclude) if exclude else ""
        with self._lock:
            return [{"p": int(s["p"]), "site": str(s.get("site") or ""), "url": str(s.get("url") or ""),
                     "gone": bool(s.get("gone")), "at": float(s.get("at") or 0), "last": float(s.get("last") or 0),
                     "cuts": int(s.get("cuts") or 0), "p0": int(s.get("p0") or s["p"])}
                    for s in (self._keys.get(key) or {}).get("samples") or []
                    if now - float(s.get("last") or 0) <= MAX_AGE and not (skip and listing_key(s.get("url", "")) == skip)]

    def reference(self, key: str, exclude: str = "") -> dict[str, Any] | None:
        """A szokásos ár egy termékre (_reference). Ha a kulcsban nincs kapacitás (a címből nem derült ki, pl. „RTX 5060
        Ti”), de a terméknek vannak ilyen változatai („… 8gb”, „… 16gb”), óvatosan a legolcsóbb változat szokásos árával
        számol (a name és a key azé a változaté): így egy 8 GB-os kártya nem látszik olcsó 16 GB-osnak."""
        found = self._reference(key, exclude)
        variants = self.variants(key)
        if not variants:
            return found
        options = [option for option in [found, *(self._reference(v, exclude) for v in variants)] if option]
        return min(options, key=lambda option: option["price"]) if options else None

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

    def _reference(self, key: str, exclude: str = "") -> dict[str, Any] | None:
        """A szokásos ár egy kulcsra: a friss árak középértéke (a kilógók nélkül), ha legalább MIN_SAMPLES van, és
        nem szórnak nagyon (price_stats: megbízható). Az exclude hirdetés (pl. maga a jelölt) nem számít bele. A sites:
        melyik oldalakon láttuk ezeket az árakat; a sold: az elkelt (újranézéskor eltűnt) hirdetések középára, ha
        legalább SOLD_MIN van (a hirdetési árnál többnyire alacsonyabb: ennyiért kel el ténylegesen)."""
        samples = self.samples(key, exclude)
        with self._lock:
            name = (self._keys.get(key) or {}).get("name") or key
        stats = price_stats([s["p"] for s in samples])
        if not stats or stats["n"] < MIN_SAMPLES or stats["confidence"] != "megbízható":
            return None
        kept = [s for s in samples if stats["min"] <= s["p"] <= stats["max"]]   # a kilógók (és oldaluk) nem számít
        found = {"price": round_price(stats["median"]), "n": stats["n"], "p25": stats["p25"], "p75": stats["p75"],
                 "name": name, "key": key, "sites": sorted({s["site"] for s in kept if s["site"]})}
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
