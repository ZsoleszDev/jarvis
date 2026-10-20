"""Fotóellenőrzés modell nélkül: homályos, sötét, túl világos, ellenfényes, kicsi és ismétlődő fotók.

A fotó felvételekor (ItemStore.add_photo) egyszer kiszámolja a mértékeket a mentett, legfeljebb 1600 px-es
JPEG-ből, és a tárggyal együtt elmenti (item.json, photo_checks). Az ítélet (mi számít homályosnak) mindig a
mentett mértékekből, a mostani küszöbökkel készül, így egy küszöb átírása után nem kell újraszámolni. Ez csak
jelzés: a feltöltést soha nem állítja meg, a borítót pedig csak akkor cseréli, ha a gombbal kéred.

A küszöbök óvatosak: egy átlagos telefonos fotónál (sima háttér előtt lefényképezett ruha, fehér háttér, sötét
ruha) ne szóljon; csak akkor, ha a hiba szemmel is egyértelmű.
"""
from __future__ import annotations

import io
from typing import Any

import numpy as np

# Ha a mértékek számítása érdemben változik, emeld: a régi fotók mértékei a háttérben újra elkészülnek.
CHECK_VERSION = 1
# Az elemzés ekkora képen fut (a hosszabb oldal): egy hirdetésfotóból a telefon képernyőjén nagyjából ennyi
# részlet látszik, a szenzorzaj és a JPEG apró hibái eltűnnek, és egy fotó pár ezredmásodperc.
ANALYSIS_SIDE = 512

# Élesség: a Laplace-szűrt kép szórásnégyzete 8×8 csempén, a legélesebb negyed átlaga. Csak az a csempe számít,
# amin van valami (a fényesség szórása legalább 6): így a sima háttér (ágy, ajtó, fal) nem húzza le, elég, ha
# maga a tárgy éles. Mesterséges próbaképeken az éles kép 100 fölött van, egy sima, mintátlan doboz is 20
# fölött; az 1600 px-es képen 4 px-es elmosás (a telefonon már láthatóan puha) 25–30, a 6 px-es (rossz
# fókusz, bemozdulás) 10 körül. A küszöb óvatos: csak a szemmel is egyértelműen homályos fotónál szól.
SHARP_GRID = 8
STRUCTURE_STD = 6.0
BLURRY_BELOW = 12.0
# Sötét: az átlagos fényesség (0–255) 50 alatt, és a legvilágosabb 5% is 120 alatt van (nincs a képen igazán
# világos rész). A fényképező automatikája a sötét ruhát is 100 körüli átlagra exponálja; 50 alatti átlag
# szinte csak gyenge fénynél vagy alulexponálva jön ki.
DARK_MEAN = 50
DARK_PEAK = 120
# Túl világos (kiégett): a kép legalább 40%-a tiszta fehér (250 fölött), és a legsötétebb 5% is világosszürke
# (110 fölött). Így a fehér háttér előtti (vagy háttércserélt) termékfotó nem számít túl világosnak, mert maga
# a tárgy sötétebb; a fehér ruha fehér lepedőn sem, mert normál expozíciónál nem ég ki.
BRIGHT_CLIPPED = 0.40
BRIGHT_FLOOR = 110
# Ellenfény: egyszerre nagy kiégett (250 fölött) és nagy beforrt (6 alatt) terület, pl. ablak előtt fotózva.
# A fekete ruha egy telefonos fotón 15–40 körül van, nem tiszta fekete, ezért az önmagában nem számít.
CLIP_WHITE = 250
CLIP_BLACK = 6
CLIPPED_SHARE = 0.12
# Kicsi: a rövidebb oldal 600 px alatt. A hirdetés fotóját a telefon kb. 1080 px szélesen mutatja, nagyítva még
# többen; a mai telefonok 1080 px-nél nagyobbat fényképeznek, és a csevegők (WhatsApp, Messenger) is 720 px
# fölött hagyják a rövidebb oldalt. Ez alatt jellemzően képernyőkép vagy egy letöltött, összenyomott kép.
SMALL_SIDE = 600

# Ugyanaz a fotó: a 64 bites dHash (9×8-as szürke kép szomszédos pontjainak összevetése) legfeljebb 6 bitben,
# a 256 bites (17×16) legfeljebb 16 bitben, az átlagszín csatornánként legfeljebb 32-ben tér el. A 64 bites
# önmagában kevés: ugyanabban az elrendezésben (ugyanazon az ágyon) lefotózott másik oldal vagy másik tárgy
# csak 2–8 bitben más, pont mint egy enyhén levágott másolat. A finomabb hash ezt elválasztja (a másik oldal,
# a másik tárgy 20–40 bit; ugyanannak a fotónak az újratömörített, kicsinyített, kicsit világosított, 1–2%-kal
# levágott vagy 1 fokkal elforgatott változata 0–12 bit). Az átlagszín azért kell, mert egy egyszínű kép
# hash-e minden színnél ugyanaz (csupa nulla).
DUPLICATE_BITS = 6
DETAIL_BITS = 16
COLOR_DIFF = 32

QUALITY = ("blurry", "dark", "bright", "clipped", "small")
Key = tuple[int, int, tuple[int, ...]]   # a két hash számként és az átlagszín
# A fotó előnézetén (buborékban), egy-egy fotóról.
TIPS = {
    "blurry": "Homályosnak tűnik.",
    "dark": "Túl sötét.",
    "bright": "Túl világos, a részletek kiégtek.",
    "clipped": "Ellenfényes: egy része kiégett, más része beforrt.",
    "small": "Kicsi a felbontása ({w}×{h} px), nagyítva homályos lesz.",
}
# Az összefoglalóban, a fotók sorszáma után („a 3. fotó homályosnak tűnik”).
PHRASES = {
    "blurry": "homályosnak tűnik",
    "dark": "túl sötét",
    "bright": "túl világos",
    "clipped": "ellenfényes",
    "small": "kis felbontású",
}


# --- mértékek -----------------------------------------------------------------------------

def _hash(gray: Any, size: int) -> str:
    """dHash: a (size+1)×size-es kép minden sorában világosabb-e a következő pont (size² bit, hexában)."""
    from PIL import Image

    small = np.asarray(gray.resize((size + 1, size), Image.BOX), dtype=np.int16)
    bits = np.packbits((small[:, 1:] > small[:, :-1]).flatten())
    return bits.tobytes().hex()


def measure(data: bytes) -> dict[str, Any]:
    """Egy (mentett) JPEG mértékei: méret, élesség, fényesség, kiégett és beforrt arány, átlagszín, két hash."""
    from PIL import Image

    image = Image.open(io.BytesIO(data))
    width, height = image.size
    image.draft("RGB", (ANALYSIS_SIDE, ANALYSIS_SIDE))   # JPEG: eleve kicsiben bontja ki (gyors)
    image = image.convert("RGB")
    image.thumbnail((ANALYSIS_SIDE, ANALYSIS_SIDE), Image.BOX)
    gray_image = image.convert("L")
    gray = np.asarray(gray_image, dtype=np.float32)
    inner = gray[1:-1, 1:-1]
    laplace = 4 * inner - gray[:-2, 1:-1] - gray[2:, 1:-1] - gray[1:-1, :-2] - gray[1:-1, 2:]
    tiles = sorted(float(lap.var())
                   for lap_band, band in zip(np.array_split(laplace, SHARP_GRID, axis=0),
                                             np.array_split(inner, SHARP_GRID, axis=0))
                   for lap, tile in zip(np.array_split(lap_band, SHARP_GRID, axis=1),
                                        np.array_split(band, SHARP_GRID, axis=1))
                   if tile.size and tile.std() >= STRUCTURE_STD)
    best = tiles[-max(1, len(tiles) // 4):]
    sharp = sum(best) / len(best) if tiles else 0.0   # egyszínű kép: nincs rajta semmi éles
    p05, p95 = np.percentile(gray, (5, 95))
    rgb = np.asarray(image, dtype=np.float32).reshape(-1, 3).mean(axis=0)
    return {
        "v": CHECK_VERSION, "w": width, "h": height,
        "sharp": round(sharp, 1), "mean": round(float(gray.mean()), 1),
        "p05": int(p05), "p95": int(p95),
        "white": round(float((gray >= CLIP_WHITE).mean()), 3), "black": round(float((gray <= CLIP_BLACK).mean()), 3),
        "rgb": [int(round(float(c))) for c in rgb],
        "hash": _hash(gray_image, 8), "detail": _hash(gray_image, 16),
    }


def current(check: Any) -> bool:
    """Megvannak-e a mostani változat mértékei (különben a háttérben újra kiszámolja)."""
    return isinstance(check, dict) and check.get("v") == CHECK_VERSION


# --- ítélet ---------------------------------------------------------------------------------

def problems(check: Any) -> list[str]:
    """A fotó gondjai (QUALITY sorrendben); üres, ha rendben van, vagy még nincs mérve."""
    if not current(check) or "sharp" not in check:
        return []
    try:
        dark = check["mean"] < DARK_MEAN and check["p95"] < DARK_PEAK
        bright = check["white"] >= BRIGHT_CLIPPED and check["p05"] >= BRIGHT_FLOOR
        clipped = not (dark or bright) and check["white"] >= CLIPPED_SHARE and check["black"] >= CLIPPED_SHARE
        # sötét vagy kiégett képen a részletek amúgy is eltűnnek: ott az élesség nem mond semmit
        blurry = check["sharp"] < BLURRY_BELOW and not (dark or bright)
        small = min(int(check["w"]), int(check["h"])) < SMALL_SIDE
    except (KeyError, TypeError, ValueError):
        return []
    found = {"blurry": blurry, "dark": dark, "bright": bright, "clipped": clipped, "small": small}
    return [kind for kind in QUALITY if found[kind]]


def key(check: Any) -> Key | None:
    """Az összevetéshez (sok párnál egyszer kiszámolva); None, ha nincs mérve."""
    if not current(check):
        return None
    try:
        return int(check["hash"], 16), int(check["detail"], 16), tuple(int(c) for c in check["rgb"])
    except (KeyError, TypeError, ValueError):
        return None


def same_key(a: Key | None, b: Key | None) -> bool:
    """Ugyanaz (vagy majdnem ugyanaz) a két fotó, a key() eredményéből."""
    if a is None or b is None or len(a[2]) != len(b[2]):
        return False
    return ((a[0] ^ b[0]).bit_count() <= DUPLICATE_BITS and (a[1] ^ b[1]).bit_count() <= DETAIL_BITS
            and all(abs(x - y) <= COLOR_DIFF for x, y in zip(a[2], b[2])))


def same(a: Any, b: Any) -> bool:
    """Ugyanaz (vagy majdnem ugyanaz) a két fotó."""
    return same_key(key(a), key(b))


def duplicate_groups(names: list[str], checks: dict[str, Any]) -> list[list[str]]:
    """A tárgyon belül majdnem azonos fotók csoportjai (legalább kettő), a fotók sorrendjében."""
    keys = {name: key(checks.get(name)) for name in names}
    groups: list[list[str]] = []
    for name in names:
        for group in groups:
            if any(same_key(keys[name], keys[other]) for other in group):
                group.append(name)
                break
        else:
            groups.append([name])
    return [group for group in groups if len(group) > 1]


# --- szövegek ------------------------------------------------------------------------------------

def _article(number: int) -> str:
    """„az 1.”, „az 5.”, de „a 2.”, „a 10.”."""
    return "az" if number == 1 or str(number).startswith("5") and number < 1000 else "a"


def ordinals(numbers: list[int]) -> str:
    """[3] → „a 3.”; [1, 4] → „az 1. és a 4.”; [2, 3, 5] → „a 2., a 3. és az 5.”."""
    parts = [f"{_article(n)} {n}." for n in numbers]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " és " + parts[-1]


def report(names: list[str], checks: dict[str, Any], elsewhere: dict[str, list[str]] | None = None
           ) -> dict[str, Any]:
    """A felületnek és a feltöltés elejére: fotónként a jelzések, a javasolt borító, és egy rövid összefoglaló.

    `elsewhere`: fotó → a többi tárgy neve, ahol ugyanez a fotó megvan (az ItemStore keresi meg).
    {"photo_warnings": {"03.jpg": [{"kind": "blurry", "text": "Homályosnak tűnik."}]},
     "photo_cover": "02.jpg" (vagy üres), "photo_note": "A 3. fotó homályosnak tűnik; …"}"""
    elsewhere = elsewhere or {}
    number = {name: index for index, name in enumerate(names, 1)}
    warnings: dict[str, list[dict[str, str]]] = {}
    found = {name: problems(checks.get(name)) for name in names}

    def add(name: str, kind: str, text: str) -> None:
        warnings.setdefault(name, []).append({"kind": kind, "text": text})

    for name in names:
        check = checks.get(name) or {}
        for kind in found[name]:
            add(name, kind, TIPS[kind].format(w=check.get("w"), h=check.get("h")))
    groups = duplicate_groups(names, checks)
    for group in groups:
        for name in group:
            others = ordinals([number[o] for o in group if o != name])
            add(name, "duplicate", f"Majdnem ugyanaz, mint {others}; az egyiket törölheted.")
    for name in names:
        places = list(dict.fromkeys(elsewhere.get(name) or []))
        if places:
            where = " és ".join(f"„{place}”" for place in places)
            add(name, "elsewhere", f"Ugyanez a fotó a(z) {where} tárgynál is megvan. Biztos, hogy ide való?")

    # Borító: ha az elsővel gond van, a legélesebb rendben lévő (ami nem egy másik tárgy fotója).
    cover = ""
    if names and found[names[0]]:
        good = [n for n in names[1:] if not found[n] and not elsewhere.get(n) and key(checks.get(n))]
        if good:
            cover = max(good, key=lambda n: float(checks[n]["sharp"]))

    parts = []
    for kind in QUALITY:
        flagged = [number[n] for n in names if kind in found[n]]
        if flagged:
            parts.append(f"{ordinals(flagged)} fotó {PHRASES[kind]}")
    for group in groups:
        parts.append(f"{ordinals([number[n] for n in group])} majdnem ugyanaz")
    by_place: dict[str, list[int]] = {}
    for name in names:
        for place in dict.fromkeys(elsewhere.get(name) or []):
            by_place.setdefault(place, []).append(number[name])
    for place, numbers in by_place.items():
        parts.append(f"{ordinals(numbers)} fotó a(z) „{place}” tárgynál is megvan")
    note = "; ".join(parts)
    if note:
        note = note[0].upper() + note[1:] + "."
        if cover:
            note += f" Borítónak {ordinals([number[cover]])} jobb lenne."
    return {"photo_warnings": warnings, "photo_cover": cover, "photo_note": note}
