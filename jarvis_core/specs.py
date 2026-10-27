"""Strukturált termékadatok és összehasonlíthatóság (modell nélkül).

Egy hirdetés címéből (és ha megvan, a leírásából) termékkörönként kiolvassa az ár szempontjából lényeges
tulajdonságokat, mindegyiket a forrásával (cím, leírás, típus: a típusból következik) és a szövegrészlettel, amiből
jön (extract). Ami nem derül ki, az hiányzik: nem találgat (egy „8GB DDR4” hirdetésnél a modulok száma nem ismert; egy
„RX 580”-nál a memória sem).

Termékkörök és a lényeges tulajdonságaik:
- videókártya (gpu): a lapka (RTX 3060 Ti, RX 580), a memória (VRAM: a „4G” is az), a csonkított változat (2048SP);
- memória (ram): a DDR-generáció, az összkapacitás, a kiszerelés (2×8 GB; a „16GB” magában ismeretlen felosztású, a „kit”
  több modul), a kivitel (DIMM, SO-DIMM), a szerver-memória (ECC, regisztrált), a sebesség;
- telefon (phone): a típus, a tárhely, az aktiválási zár (iCloud, Google-fiók), a hálózatfüggés;
- konzol (console): a típus és a kiadás (Switch, Switch Lite, Switch OLED; PS5, PS5 Digital, PS5 Slim), a tartozékok
  (plusz kontroller, játékok);
- SSD (ssd): a kapacitás, a csatoló (NVMe, SATA), a típus.

compare: két hirdetés (a vizsgált és egy összehasonlító) viszonya: „pontos” (az ár szempontjából lényeges
tulajdonságok egyeznek), „közelítő” (nincs kritikus eltérés, de valamiben különbözik: ezt meg is mondja), „kizárt” (más
változat, kapacitás, kiszerelés, tartozék), „bizonytalan” (egy fontos adat hiányzik az összehasonlítóból). Egy jó szöveges
hasonlóság egy kritikus eltérést nem ír felül.

grounded: a modell által adott terméknévből kiveszi azt a méretet, ami a hirdetésben nincs benne (a modell nem találhat
ki specifikációt)."""
from __future__ import annotations

import re
from typing import Any

from .pricebook import (_GPU_CHIP, _KIT, BRANDS, GPU_FAMILIES, MODIFIERS, TYPE_WORDS, _words, capacities, implied_size,
                        is_capacity, product_key, ram_context, sized)
from .textutil import fold

PONTOS, KOZELITO, KIZART, BIZONYTALAN = "pontos", "közelítő", "kizárt", "bizonytalan"
CATEGORY_NAMES = {"gpu": "videókártya", "ram": "memória", "phone": "telefon", "console": "konzol", "ssd": "SSD"}
ATTR_NAMES = {"chip": "típus", "vram": "memória (VRAM)", "variant": "változat", "maker": "gyártó",
              "generation": "DDR-generáció", "total": "összkapacitás", "layout": "kiszerelés", "form": "kivitel",
              "server": "szerver-memória", "speed": "sebesség", "model": "típus", "storage": "tárhely",
              "lock": "zárolás", "network": "hálózatfüggés", "extras": "tartozékok", "capacity": "kapacitás",
              "interface": "csatoló", "health": "állapot (élettartam)", "written": "írásmennyiség"}
# Termékkörönként a kritikus tulajdonságok, és mit jelent, ha egy hirdetés nem írja: None: ismeretlen (az
# összehasonlító bizonytalan, ha a vizsgált hirdetésben ismert); egy érték: ha nem írja, ez (pl. aktiválási zár nincs, ha
# nem említi; tartozék sincs pluszban).
CRITICAL: dict[str, dict[str, str | None]] = {
    "gpu": {"chip": None, "vram": None, "variant": "teljes"},
    "ram": {"generation": None, "total": None, "layout": None, "form": None, "server": "nem"},
    "phone": {"model": None, "storage": None, "lock": "nincs"},
    "console": {"model": None, "extras": "nincs"},
    "ssd": {"model": None, "capacity": None, "interface": None},
}
# A közelítő eltérések: ezek mellett az összehasonlító beszámít, de látszik, miben más.
APPROX: dict[str, tuple[str, ...]] = {"gpu": ("maker",), "ram": ("speed", "maker"), "phone": ("network",),
                                      "console": (), "ssd": ("health", "written")}
# Ha a tulajdonság hiányzik a vizsgált hirdetésből, ezeknél nem lehet biztos az ára (a termékből több ilyen változat is
# van, és nem tudni, melyik).
VARIANT_ATTRS = {"gpu": ("vram",), "ram": ("generation", "total"), "phone": ("storage",), "console": (),
                 "ssd": ("capacity",)}

_PHONE_BRANDS = {"apple", "samsung", "xiaomi", "redmi", "huawei", "oneplus", "oppo", "realme", "honor", "motorola", "nokia",
                 "google", "poco", "sony", "nothing"}
_PHONE_WORDS = re.compile(r"\b(?:telefon\w*|mobil\w*|okostelefon\w*|smartphone)\b")
_CONSOLE_FAMILIES = {"switch", "ps3", "ps4", "ps5", "xbox", "steamdeck"}
_SSD_WORDS = re.compile(r"\b(?:ssd|nvme|m2 ssd|m\.2)\b")
_SSD_MODELS = re.compile(r"\b(?:9[789]0 (?:evo|pro)|8[67]0 (?:evo|qvo)|sn[5-8]\d0|mx500|bx500|a400|nv[123]|kc[23]000|"
                         r"p[235] plus|p[235]|t[57] shield|990 pro)\b")
_NOT_PART = {"laptop", "pc", "kontroller", "jatek"}
_LOCK_RE = re.compile(r"\b(?:icloud\w*|aktivalasi zar\w*|aktivalasi zarral|fiokzar\w*|fiok ?zar\w*|frp\w*|"
                      r"google ?fiok\w* zar\w*|zarolt\w*|lopott\w*|blacklist\w*|feketelist\w*)\b")
_LOCK_DENIED = re.compile(r"\b(?:nincs|nem|mentes|nelkul|free|kijelentkez\w*|torolve|torolt)\b")
_UNLOCKED_RE = re.compile(r"\b(?:fuggetlen\w*|kartyafuggetlen\w*|unlocked)\b")
_NETLOCK_RE = re.compile(r"\b(?:telekom\w*|yettel\w*|vodafone\w*|telenor\w*|one ?(?:hu|mobil)\w*|halozatfugg\w*|"
                         r"szolgaltato ?fugg\w*|fuggo)\b")
_EXTRAS_RE = re.compile(r"(?:\+\s*|\bes\s+|\bplusz\s+)?\b(\d+)\s*(?:db\s*)?(jatek\w*|kontroller\w*|joy-?con\w*|controller\w*)"
                        r"|\b(jatekokkal|jatekkal|jatekcsomag\w*|extra kontroller\w*|plusz kontroller\w*|"
                        r"\+\s*kontroller\w*|\+\s*jatek\w*)")
_SPEED_RE = re.compile(r"\b(\d{4})\s*(?:mhz|mt/?s)?\b")
_GPU_VARIANT_RE = re.compile(r"\b(2048|2304|1792)\s*sp\b")
_INTERFACE_RE = (("nvme", re.compile(r"\b(?:nvme|pcie\s*(?:gen)?\s*[345]|pci-?e)\b")),
                 ("sata", re.compile(r"\b(?:sata\w*|2[.,]5 ?(?:col|\"|inch)?)\b")))
_HEALTH_RE = re.compile(r"\b(?:health|allapot|elettartam|egeszseg)\w*\W{0,3}(\d{2,3}) ?%")
_WRITTEN_RE = re.compile(r"\b(\d+(?:[.,]\d+)?) ?(tb|gb) (?:irva|irt|written|tbw)\b|\btbw\W{0,3}(\d+)")


def _attr(value: Any, source: str, evidence: str) -> dict[str, Any]:
    return {"value": value, "source": source, "evidence": " ".join(str(evidence).split())[:80]}


def category(title: str) -> str:
    """A termékkör a címből: gpu, ram, phone, console, ssd, vagy „” (más)."""
    words = _words(title)
    folded = fold(title or "")
    if set(words) & _CONSOLE_FAMILIES and (_EXTRAS_RE.search(folded) or "konzol" in words or
                                           set(words) & {"lite", "oled", "v2", "digital", "slim", "seriesx", "seriess"}):
        return "console"                       # a konzol (a plusz kontrollerrel, játékokkal is), nem a kontroller
    key = product_key(title)
    picked = set(key[0].split()) if key else set()
    if picked & _NOT_PART and not (ram_context(words) and "laptop" not in picked):
        return ""                              # laptop, gép, kontroller, játék: nem az alkatrész maga
    if set(words) & GPU_FAMILIES and any(_GPU_CHIP.match(w) for w in words):
        return "gpu"
    if ram_context(words) and not set(words) & {"iphone", "ipad", "galaxy", "pixel", "macbook", "thinkpad"}:
        return "ram"
    if set(words) & _CONSOLE_FAMILIES and not {TYPE_WORDS[w] for w in words if w in TYPE_WORDS} & {"kontroller", "jatek"}:
        return "console"                       # (egy játék vagy egy kontroller a konzolhoz nem a konzol)
    if _SSD_WORDS.search(folded) or _SSD_MODELS.search(folded):
        return "ssd"
    if "iphone" in words or "pixel" in words or (_PHONE_WORDS.search(folded) and set(words) & _PHONE_BRANDS) or \
            ("galaxy" in words and any(re.match(r"^[saz]\d{1,2}\w*$|^note\d*$|^fold\d*$|^flip\d*$", w) for w in words)) \
            or set(words) & {"redmi", "poco", "oneplus"}:
        return "phone"
    return ""


def extract(title: str, details: str = "") -> dict[str, Any]:
    """A hirdetés termékadatai: {category, attrs: {név: {value, source, evidence}}, missing: [a hiányzó kritikus
    tulajdonságok]}. A cím az elsődleges; a leírásból csak az jön, ami a címben nincs (a forrása „leírás")."""
    kind = category(title)
    out: dict[str, Any] = {"category": kind, "attrs": {}, "missing": []}
    if not kind:
        return out
    attrs: dict[str, dict[str, Any]] = {}
    for source, text in (("cím", title), ("leírás", details)):
        if not text:
            continue
        found = _EXTRACT[kind](text)
        for name, (value, evidence) in found.items():
            if name not in attrs and value not in (None, ""):
                attrs[name] = _attr(value, source, evidence)
    chip_key = attrs.pop("chip_key", None)
    if kind == "gpu" and "vram" not in attrs and chip_key is not None:
        size = implied_size(str(chip_key["value"]))
        if size:                               # a típus csak egy memóriamérettel készült (pl. GTX 1080: 8 GB)
            attrs["vram"] = _attr(size, "típus", f"a {attrs['chip']['value'].upper()} csak {size.upper()}-os")
    out["attrs"] = attrs
    out["missing"] = [name for name, default in CRITICAL[kind].items() if default is None and name not in attrs]
    return out


def _gpu(text: str) -> dict[str, tuple[Any, str]]:
    words = _words(text)
    out: dict[str, tuple[Any, str]] = {}
    chips = [index for index, w in enumerate(words) if _GPU_CHIP.match(w) and index and words[index - 1] in GPU_FAMILIES]
    if chips:
        index = chips[0]
        parts = [words[index - 1], words[index]]
        for following in words[index + 1:index + 3]:
            if following in MODIFIERS and following not in ("pro", "max", "mini", "lite", "se", "oled", "slim"):
                parts.append(following)
            else:
                break
        chip = " ".join(parts)
        found = product_key(chip)
        out["chip"] = (chip, chip.upper())
        if found:
            out["chip_key"] = (found[0], chip)
    sizes = [size for size in capacities(words) if _gigabytes(size) <= 48]
    if sizes:
        out["vram"] = (sizes[0], sizes[0].upper())
    variant = _GPU_VARIANT_RE.search(fold(text))
    if variant:
        out["variant"] = (f"{variant.group(1)}sp", variant.group(0))
    maker = next((w for w in words if w in BRANDS or w in _GPU_MAKERS), "")
    if maker:
        out["maker"] = (maker, maker)
    return out


_GPU_MAKERS = {"sapphire", "powercolor", "xfx", "zotac", "palit", "gainward", "pny", "inno3d", "evga", "galax", "kfa2",
               "asrock", "biostar", "manli", "colorful", "nvidia", "amd", "intel"}


def _gigabytes(size: str) -> int:
    match = re.match(r"^(\d+)(gb|tb)$", size)
    return int(match.group(1)) * (1000 if match.group(2) == "tb" else 1) if match else 0


def _ram(text: str) -> dict[str, tuple[Any, str]]:
    words = _words(text)
    folded = fold(text or "")
    out: dict[str, tuple[Any, str]] = {}
    generation = next((w for w in words if re.match(r"^(?:lp)?ddr[2-5]$", w)), "")
    if generation:
        out["generation"] = (generation, generation.upper())
    kit = next((match for match in (_KIT.match(w) for w in words) if match), None)
    single = re.search(r"\b1 ?x ?(\d{1,2}) ?gb\b", folded)
    if kit:
        modules, size = int(kit.group(1)), int(kit.group(2))
        out["layout"] = (f"{modules}x{size}gb", kit.group(0).upper())
        out["total"] = (f"{modules * size}gb", kit.group(0).upper())
    else:
        sizes = [size for size, kind in sized(text) if kind != "storage" and _gigabytes(size) <= 512]
        if sizes:
            out["total"] = (sizes[0], sizes[0].upper())
            if single:
                out["layout"] = (f"1x{single.group(1)}gb", single.group(0).upper())
            elif "kit" in words or "keszlet" in words:
                out["layout"] = ("kit", "kit")          # több modul, de nem tudni, hogyan
    if "sodimm" in words:
        out["form"] = ("sodimm", "SO-DIMM")
    elif re.search(r"\b(?:u?dimm|asztali\w*|desktop)\b", folded):
        out["form"] = ("dimm", "DIMM")
    server = re.search(r"\b(?:ecc|reg|registered|rdimm|lrdimm|szerver\w*)\b", folded)
    if server:
        out["server"] = ("igen", server.group(0))
    speed = next((m for m in _SPEED_RE.finditer(folded) if 1066 <= int(m.group(1)) <= 9600), None)
    if speed:
        out["speed"] = (speed.group(1), speed.group(0))
    maker = next((w for w in words if w in BRANDS or w in _RAM_MAKERS), "")
    if maker:
        out["maker"] = (maker, maker)
    return out


_RAM_MAKERS = {"kingston", "corsair", "gskill", "crucial", "adata", "patriot", "teamgroup", "hyperx", "samsung", "hynix",
               "micron", "goodram", "lexar", "pny"}


def _phone(text: str) -> dict[str, tuple[Any, str]]:
    folded = fold(text or "")
    out: dict[str, tuple[Any, str]] = {}
    key = product_key(text)
    if key:
        model = " ".join(word for word in key[1].split() if not is_capacity(word))
        if model:
            out["model"] = (model, model)
    storage = [size for size, kind in sized(text) if kind == "storage" or (not kind and _gigabytes(size) >= 32)]
    if storage:
        out["storage"] = (storage[0], storage[0].upper())
    lock = _LOCK_RE.search(folded)
    if lock and not _LOCK_DENIED.search(folded[max(0, lock.start() - 12):lock.end() + 14]):
        out["lock"] = ("aktiválási zár", lock.group(0))
    unlocked, netlock = _UNLOCKED_RE.search(folded), _NETLOCK_RE.search(folded)
    if unlocked:
        out["network"] = ("független", unlocked.group(0))
    elif netlock:
        out["network"] = ("hálózatfüggő", netlock.group(0))
    return out


def _console(text: str) -> dict[str, tuple[Any, str]]:
    words = _words(text)
    folded = fold(text or "")
    out: dict[str, tuple[Any, str]] = {}
    family = next((w for w in words if w in _CONSOLE_FAMILIES), "")
    if family:
        variants = {"switch": ("lite", "oled", "v2"), "ps5": ("digital", "slim", "pro"), "ps4": ("slim", "pro"),
                    "xbox": ("seriesx", "seriess", "one")}.get(family, ())
        variant = [w for w in words if w in variants]
        if family == "xbox" and "one" in variant and len(words) > words.index("one") + 1 and \
                words[words.index("one") + 1] in ("x", "s"):
            variant = [f"one {words[words.index('one') + 1]}"]
        name = " ".join([family, *variant[:1]]).replace("seriesx", "series x").replace("seriess", "series s")
        out["model"] = (name, name)
    extras = _EXTRAS_RE.search(folded)
    if extras:
        out["extras"] = ("plusz tartozékkal", extras.group(0).strip())
    return out


def _ssd(text: str) -> dict[str, tuple[Any, str]]:
    folded = fold(text or "")
    out: dict[str, tuple[Any, str]] = {}
    key = product_key(text)
    if key:
        model = " ".join(word for word in key[1].split() if not is_capacity(word))
        if model:
            out["model"] = (model, model)
    sizes = [size for size, kind in sized(text) if kind != "ram"]
    if sizes:
        out["capacity"] = (max(sizes, key=_gigabytes), max(sizes, key=_gigabytes).upper())
    for name, pattern in _INTERFACE_RE:
        match = pattern.search(folded)
        if match:
            out["interface"] = (name, match.group(0))
            break
    health = _HEALTH_RE.search(folded)
    if health:
        out["health"] = (f"{health.group(1)}%", health.group(0))
    written = _WRITTEN_RE.search(folded)
    if written:
        out["written"] = (written.group(0), written.group(0))
    return out


_EXTRACT = {"gpu": _gpu, "ram": _ram, "phone": _phone, "console": _console, "ssd": _ssd}


def _value(spec: dict[str, Any], name: str) -> Any:
    attr = (spec.get("attrs") or {}).get(name)
    if attr is not None:
        return attr["value"]
    return CRITICAL.get(spec.get("category") or "", {}).get(name)


def _shown(value: Any) -> str:
    text = str(value)
    return text.upper() if re.match(r"^\d+(?:x\d+)?(?:gb|tb)$", text) else text


def compare(want: dict[str, Any], have: dict[str, Any]) -> tuple[str, list[str]]:
    """A vizsgált hirdetés (want) és egy összehasonlító (have) viszonya: (pontos | közelítő | kizárt | bizonytalan,
    az okok szövegesen). Kizárt, ha egy kritikus tulajdonságuk mindkettőben ismert, és más (vagy az összehasonlító
    plusz tartozékkal jön); bizonytalan, ha a vizsgáltban ismert, az összehasonlítóban nem derül ki; közelítő, ha csak
    egy nem kritikus tulajdonságuk más (gyártó, sebesség, hálózatfüggés), vagy a vizsgáltban hiányzik egy változat-adat.
    Más termékkör: kizárt; ha valamelyiknek nincs termékköre, a termékkulcs dönt (pontos)."""
    kind = want.get("category") or ""
    if not kind or not have.get("category"):
        return PONTOS, []
    if have["category"] != kind:
        return KIZART, [f"más termék ({CATEGORY_NAMES.get(have['category'], have['category'])})"]
    excluded, unsure, approx = [], [], []
    for name, default in CRITICAL[kind].items():
        mine, theirs = _value(want, name), _value(have, name)
        if name == "extras":
            if theirs != "nincs" and mine == "nincs":
                excluded.append(f"plusz tartozékkal ({(have['attrs'].get('extras') or {}).get('evidence') or 'tartozék'})")
            elif mine != "nincs" and theirs == "nincs":
                approx.append("kevesebb tartozékkal, mint ez")
            continue
        if name == "layout" and mine is not None and theirs is not None and mine != theirs and \
                "kit" in (mine, theirs) and str(mine if theirs == "kit" else theirs).split("x")[0] not in ("1",):
            approx.append("kit (a felosztása nem derül ki)")
            continue
        if mine is not None and theirs is not None:
            if mine != theirs:
                excluded.append(f"más {ATTR_NAMES[name]} ({_shown(theirs)}, ez {_shown(mine)})")
        elif mine is not None and theirs is None:
            unsure.append(f"nem derül ki: {ATTR_NAMES[name]}")
        elif mine is None and theirs is not None and name in VARIANT_ATTRS.get(kind, ()):
            # a vizsgáltnál nem derül ki (pl. a memória): egy ismert változathoz mérve közelítő (a hívó a legolcsóbb
            # változatot választja, így óvatos); ha csak a változatlanokhoz mérhető, az ára nem biztos (unsure_variant)
            approx.append(f"ennél a hirdetésnél nem derül ki: {ATTR_NAMES[name]} (ez {_shown(theirs)})")
        elif mine is None and theirs is not None and name == "layout" and not str(theirs).startswith("1x"):
            unsure.append(f"más kiszerelés lehet ({_shown(theirs)})")
    for name in APPROX.get(kind, ()):
        mine, theirs = _value(want, name), _value(have, name)
        if mine is not None and theirs is not None and mine != theirs:
            approx.append(f"más {ATTR_NAMES[name]} ({_shown(theirs)}, ez {_shown(mine)})")
    if excluded:
        return KIZART, excluded
    if unsure:
        return BIZONYTALAN, unsure
    if approx:
        return KOZELITO, approx
    return PONTOS, []


def unsure_variant(spec: dict[str, Any]) -> list[str]:
    """Ami a vizsgált hirdetésből nem derül ki, pedig a termékből több ilyen változat van (VARIANT_ATTRS): ilyenkor az
    ára nem lehet biztos (pl. egy „RX 580” lehet 4 és 8 GB-os is)."""
    kind = spec.get("category") or ""
    return [ATTR_NAMES[name] for name in VARIANT_ATTRS.get(kind, ()) if name in (spec.get("missing") or [])]


def describe(spec: dict[str, Any]) -> str:
    """Röviden, a felületnek: „videókártya: RX 580, memória (VRAM) 4GB (a címből: „4G”)”."""
    kind = spec.get("category") or ""
    if not kind:
        return ""
    parts = []
    for name, attr in (spec.get("attrs") or {}).items():
        source = {"cím": "a címből", "leírás": "a leírásból", "típus": "a típusból"}.get(attr.get("source"), "")
        evidence = f": „{attr['evidence']}”" if attr.get("evidence") and attr.get("source") != "típus" else ""
        parts.append(f"{ATTR_NAMES.get(name, name)} {_shown(attr['value'])} ({source}{evidence})")
    missing = [ATTR_NAMES[name] for name in spec.get("missing") or []]
    text = f"{CATEGORY_NAMES[kind]}: " + "; ".join(parts) if parts else CATEGORY_NAMES[kind]
    return text + (f"; nem derül ki: {', '.join(missing)}" if missing else "")


def grounded(name: str, text: str) -> str:
    """A modell által adott terméknév a hirdetéshez kötve: a benne lévő méret (8GB, 1TB) csak akkor marad, ha a
    hirdetés szövegében (a címében, a kártyáján) is benne van; így a modell nem tehet egy „RX 580”-ból „RX 580 8GB”-ot."""
    have = set(capacities(_words(text)))
    words = name.split()
    kept = [word for word in words if not (capacities(_words(word)) and set(capacities(_words(word))) - have)]
    return " ".join(kept) if kept else name
