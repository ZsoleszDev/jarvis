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
    if _LOT_RE.search(folded) or _PIECES.search(folded) or _PARTS_RE.search(folded) or _ACCESSORY.search(folded):
        return False
    return not any(_FOR_IT.match(w) for w in _words(text))


def product_key(text: str) -> tuple[str, str] | None:
    """A termékkulcs egy hirdetés címéből: (kulcs, név). A kulcs a rendezett szavak (így a sorrend nem számít), a név
    ahogy a címben jönnek. Benne van a termékcsalád (vagy, ha nincs, a márka), a típusjel (szám vagy számot tartalmazó
    szó), a típus jelzői (Ti, Pro, OLED…) és a fajtája (kontroller, játék). None, ha a címből nem derül ki biztosan a
    termék (pl. nincs benne típusjel)."""
    words = _words(text)
    picked: list[str] = []
    families = [w for w in words if w in FAMILIES]
    brand = next((w for w in words if w in BRANDS), "") if not families else ""
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
                if not (before in FAMILIES or before in BRANDS or before in MODIFIERS or before in SERIES):
                    continue
                if before in BRANDS and before not in FAMILIES and 30 <= int(word) <= 56:
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
    return " ".join(sorted(picked)), " ".join(picked)


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
        keys = data.get("keys") if isinstance(data, dict) else None
        self._keys: dict[str, dict[str, Any]] = {k: v for k, v in (keys or {}).items()
                                                 if isinstance(v, dict) and isinstance(v.get("samples"), list)}

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"keys": self._keys}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.path)
            self._dirty = False

    def add(self, rows: list[dict[str, Any]], name: str = "") -> int:
        """A hirdetések árai a termékükhöz (a cím szerint, vagy ha megadod, a name szerint: pl. ahogy a modell nevezte
        meg). A licit nem ár (csak a mostani ajánlat). Visszaadja, hányat tett be."""
        now, added = self.clock(), 0
        named = product_key(name) if name else None
        with self._lock:
            for row in rows:
                text, url, price = str(row.get("text") or ""), str(row.get("url") or ""), int(row.get("price") or 0)
                if row.get("auction") or not url or price <= 0 or not usable(text):
                    continue
                found = named or product_key(text)
                if found is None:
                    continue
                key, label = found
                entry = self._keys.setdefault(key, {"name": label, "samples": []})
                ident = listing_key(url)
                sample = next((s for s in entry["samples"] if listing_key(s.get("url", "")) == ident), None)
                if sample is None:
                    sample = {"at": now, "url": url, "site": str(row.get("platform") or "")}
                    entry["samples"].append(sample)
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
            self._dirty = self._dirty or bool(added)
        return added

    def reference(self, key: str, exclude: str = "") -> dict[str, Any] | None:
        """A szokásos ár egy termékre: a friss árak középértéke (a kilógók nélkül), ha legalább MIN_SAMPLES van, és
        nem szórnak nagyon (price_stats: megbízható). Az exclude hirdetés (pl. maga a jelölt) nem számít bele."""
        now = self.clock()
        with self._lock:
            entry = self._keys.get(key)
            if not entry:
                return None
            skip = listing_key(exclude) if exclude else ""
            prices = [int(s["p"]) for s in entry["samples"] if now - float(s.get("last") or 0) <= MAX_AGE
                      and not (skip and listing_key(s.get("url", "")) == skip)]
            name = entry.get("name") or key
        stats = price_stats(prices)
        if not stats or stats["n"] < MIN_SAMPLES or stats["confidence"] != "megbízható":
            return None
        return {"price": round_price(stats["median"]), "n": stats["n"], "p25": stats["p25"], "p75": stats["p75"],
                "name": name, "key": key}

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
        found = product_key(text) or (" ".join(sorted(set(_words(text)) - _NOISE)), text)
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

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"products": len(self._keys), "prices": sum(len(e["samples"]) for e in self._keys.values()),
                    "ready": sum(1 for k in self._keys if len(self._keys[k]["samples"]) >= MIN_SAMPLES)}


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
