"""Adás/Vétel: eladásra szánt tárgyak, hirdetési oldalak, és a hirdetés adatai a leírásodból.

A menet: kiválasztod, hova kerüljön (Vinted, Jófogás, Vatera, Hardverapró), feltöltöd a
fotókat, és leírod egy-két mondatban, mit adsz el („Zara L-es fekete póló, egyszer hordott,
500 Ft”). Jarvis ebből egyetlen modellhívással kiolvassa az adatokat (a fotókat nem nézi, az
sok idő lenne). Ha valami hiányzik ahhoz, amit a kiválasztott oldalak kérnek, visszakérdez;
különben indulhat a feltöltés. Kérésre előtte megnézi, mennyiért kínálnak hasonlót.

Egy tárgy egy mappa a munka mappában: item.json (az adatok), a fotók (01.jpg, 02.jpg…) és a
kis előnézetek (thumbs/). A törölt tárgy a _torolt mappába kerül, így vissza lehet hozni.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import shutil
import statistics
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

from .textutil import fold, only_hu_amount

ITEM_ID_RE = re.compile(r"^[0-9a-f]{8}$")
PHOTO_RE = re.compile(r"^\d{2}\.jpg$")
CONDITIONS = ["Új, címkével", "Új", "Nagyon jó", "Jó", "Elfogadható", "Hibás"]
KINDS = ["ruha", "cipő", "kiegészítő", "elektronika", "egyéb"]
CLOTHING = {"ruha", "cipő", "kiegészítő"}
PARCEL_SIZES = ["kicsi", "közepes", "nagy"]
MAX_PHOTO_BYTES = 25 * 1024 * 1024
PHOTO_MAX_SIDE = 1600
THUMB_SIDE = 360
MAX_PRICE = 10_000_000

LISTING_FIELDS = ("product", "title", "description", "category", "brand", "size", "condition",
                  "color", "material", "location", "shipping", "search_query")
TEXT_LIMITS = {"product": 80, "title": 100, "description": 3000, "category": 200, "brand": 80,
               "size": 40, "condition": 40, "color": 60, "material": 120, "location": 80,
               "shipping": 300, "search_query": 100, "prompt": 3000, "kind": 20}
MULTILINE = {"description", "prompt"}
POSTED, NOT_POSTED, FAILED = "feltöltve", "nincs fent", "hiba"
# Egy fent lévő hirdetés újbóli megjelölésekor (ugyanaz a hirdetés) ezek maradnak: mióta van fent, a feltöltéskori ár és cím,
# és a háttérellenőrzés eredménye (lásd listings.py), a „Nem igaz, fent van” jelöléssel együtt.
REMARK_KEEPS = ("posted_at", "posted_price", "posted_title", "checked_at", "check", "check_note", "check_error",
                "check_dismissed", "check_dismissed_note")


class ItemError(Exception):
    """Érthető hibaüzenet a tárgyakkal kapcsolatban."""


# --- hirdetési oldalak -------------------------------------------------------------

DEFAULT_DELIST_HINTS = (
    "A saját hirdetésed oldalán (vagy a Hirdetéseim / Profilom listában) keresd a Törlés, Eltávolítás, "
    "Inaktiválás, Lezárás vagy „Eladottként jelölés” lehetőséget; néha a ... vagy a Szerkesztés menü alatt "
    "van. Ha az oldal megkérdezi, miért veszed le, válaszd, hogy máshol adtad el (vagy eladtad). A megerősítő "
    "kérdésre (Biztosan?) válaszolj igennel: a felhasználó már jóváhagyta a levételt. Fizetős lehetőséget "
    "(kiemelés, előresorolás) soha ne válassz."
)


@dataclass(frozen=True)
class Platform:
    key: str
    name: str
    home: str              # itt lehet bejelentkezni
    new_url: str           # ahol a hirdetésfeladás kezdődik
    search_url: str        # {q} = keresőszó; üres = itt nincs árkutatás
    domains: tuple[str, ...]
    hints: str             # tippek a Jarvisnak a kitöltéshez
    delist_hints: str = DEFAULT_DELIST_HINTS  # tippek a levételhez (eladás után)
    max_photos: int = 10   # ennyi fotót vesz fel egy hirdetéshez (a borítókép az első); a többit nem töltjük fel


PLATFORMS: dict[str, Platform] = {
    "vinted": Platform(
        "vinted", "Vinted", "https://www.vinted.hu/", "https://www.vinted.hu/items/new",
        "https://www.vinted.hu/catalog?search_text={q}", ("vinted.hu",),
        "1. Fotók: browser_upload_photos (elem nélkül, vagy a fotófeltöltő gomb számával). "
        "2. Cím (Title), leírás (Description) és ár (Price) egy browser_fill lépésben. 3. Kategória (Category): "
        "browser_choose a mező számával és a teljes úttal, pl. „Women > Clothing > Jeans” (szintenként nyílik, az "
        "utolsó szint egy választógomb). 4. A kategória után jelenik meg a márka, a méret, az állapot és a szín; "
        "ezekhez is browser_choose kell. Márka: a neve (ha nincs a listában, a „Search brands” keresőbe maga "
        "beírja). 5. Méret: ha előbb a méretrendszert kéri, az úttal (pl. „EU > EU 40”). 6. Állapot: New with "
        "tags = Új, címkével; New without tags = Új; Very good = Nagyon jó; Good = Jó; Satisfactory = Elfogadható "
        "vagy Hibás. 7. Szín (Colours, legfeljebb kettő): színenként egy browser_choose. 8. Csomagméret: Small = "
        "kicsi, Medium = közepes, Large = nagy. Ami egy oldalon egymás után jön (pl. állapot, szín, csomagméret), "
        "az egy browser_do lépésben is mehet. 9. Végül a „Feltöltés” (Upload) gomb; ha utána azt írja, hogy "
        "valamit ki kell tölteni (Fill in …), pótold, és kattints újra.",
        max_photos=20),
    "jofogas": Platform(
        "jofogas", "Jófogás", "https://www.jofogas.hu/", "https://www.jofogas.hu/",
        "https://www.jofogas.hu/magyarorszag?q={q}", ("jofogas.hu",),
        "1. Kattints a hirdetésfeladás gombra. 2. Kategória: a legközelebbi. 3. Cím, leírás, ár. "
        "4. Fotók: browser_upload_photos. 5. Állapot, ha kéri. 6. Helyszín: a megadott város. "
        "7. Az elérhetőséget az oldal a fiókból tölti ki; ha üres és kötelező, állj meg, és kérdezd meg. "
        "8. Végül a hirdetés feladása gomb."),
    "vatera": Platform(
        "vatera", "Vatera", "https://www.vatera.hu/", "https://www.vatera.hu/",
        "https://www.vatera.hu/listings/index.php?q={q}", ("vatera.hu",),
        "1. Bejelentkezve keresd meg az eladás vagy termékfeltöltés gombot. 2. Kategória: a "
        "legközelebbi. 3. Cím (ha hosszabbat kér, egészítsd ki a márkával és az állapottal) és leírás. "
        "4. Állapot: Új, Felújított, Használt – kiváló / jó / sérült közül a legközelebbi. 5. Eladás "
        "módja: fix áras (azonnal megvehető), az ár, darabszám 1. 6. Fotók (legfeljebb 8). 7. Szállítás: "
        "a megadott módok és díjak; fizetésnél amit az oldal alapból ajánl. 8. Végül a feltöltés gomb.",
        max_photos=8),
    "hardverapro": Platform(
        "hardverapro", "Hardverapró", "https://hardverapro.hu/", "https://hardverapro.hu/",
        "https://hardverapro.hu/aprok/keres.php?stext={q}", ("hardverapro.hu",),
        "1. Bejelentkezve kattints a hirdetésfeladás gombra. 2. Kategória: a legközelebbi. 3. Cím, ár, "
        "állapot (új, használt vagy hibás). 4. Leírás. 5. Település: a megadott város. 6. Szállítás: a "
        "megadott módok (pl. személyes átvétel, postázás, Foxpost). 7. Fotók: browser_upload_photos. "
        "8. Végül a feladás gomb."),
}


# Hol van fent, mondatba illesztve („a Vinteden még 9 000 Ft”).
ON_SITE = {"vinted": "a Vinteden", "jofogas": "a Jófogáson", "vatera": "a Vaterán", "hardverapro": "a Hardveraprón"}


# --- mit kér az egyes oldal ---------------------------------------------------------

@dataclass(frozen=True)
class Need:
    field: str
    label: str
    sites: tuple[str, ...] = ()    # üres = mindegyik oldal kéri
    clothing_only: bool = False    # csak ruhánál, cipőnél, kiegészítőnél
    setting: str = ""              # az Alapadatokból is jöhet (city, shipping)


NEEDS = [
    Need("product", "mit adsz el"),
    Need("price", "ár"),
    Need("condition", "állapot"),
    Need("brand", "márka", ("vinted",), clothing_only=True),
    Need("size", "méret", ("vinted",), clothing_only=True),
    # A Vinted a színt is kötelezően kéri („Fill in colour to continue”); ha nem kérdezzük meg, a modell kitalálná.
    Need("color", "szín", ("vinted",), clothing_only=True),
    Need("location", "átvétel helye", ("jofogas", "hardverapro"), setting="city"),
    Need("shipping", "szállítás módja és díja", ("vatera", "hardverapro"), setting="shipping"),
]


def needs_for(targets: list[str]) -> list[Need]:
    return [n for n in NEEDS if not n.sites or any(site in targets for site in n.sites)]


# --- egy tárgy ---------------------------------------------------------------------

@dataclass
class Item:
    id: str
    created: float
    updated: float
    photos: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)     # hova töltse fel
    prompt: str = ""                  # a leírásod, és ha visszakérdezett, a válaszaid
    research_wanted: bool = False     # nézzen utána, mennyiért adják máshol (csak ajánlás)
    product: str = ""                 # mi ez, röviden
    kind: str = ""                    # ruha, cipő, kiegészítő, elektronika, egyéb
    title: str = ""
    description: str = ""
    category: str = ""
    brand: str = ""
    size: str = ""
    condition: str = ""
    color: str = ""
    material: str = ""
    price: int | None = None          # forint
    location: str = ""                # ha eltér az Alapadatoktól
    shipping: str = ""
    search_query: str = ""
    edited: list[str] = field(default_factory=list)      # kézzel átírt mezők: Jarvis nem írja felül
    extracted_sig: str = ""           # melyik leírásból olvasta ki az adatokat (ugyanabból nem olvassa ki újra)
    research: dict[str, Any] = field(default_factory=dict)
    platforms: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending: dict[str, Any] = field(default_factory=dict)  # visszakérdezés vagy árdöntés
    sold: bool = False
    reminded: float = 0.0             # mikor szólt utoljára, hogy régóta fent van és nem kelt el
    busy: str = ""                    # épp min dolgozik a Jarvis (nem mentjük)

    @property
    def name(self) -> str:
        if self.title:
            return self.title
        if self.product:
            return self.product
        first = " ".join(self.prompt.split())[:50]
        return first or "Új tárgy"

    @property
    def status(self) -> str:
        if self.sold:
            return "eladva"
        if self.pending.get("kind") == "info":
            return "kérdés"
        if self.pending.get("kind") == "price":
            return "árdöntés"
        up = [t for t in self.targets if self.platforms.get(t, {}).get("status") == POSTED]
        if up and len(up) == len(self.targets):
            return "fent"
        if up:
            return "részben fent"
        return "vázlat"

    def drift(self) -> list[dict[str, Any]]:
        """Ahol a fent lévő hirdetés ára vagy címe már nem az, ami itt van (itt átírtuk, ott nem):
        [{"platform": "vinted", "price": 9000, "title": "…"}], csak az eltérő mezőkkel."""
        out = []
        for key, entry in self.platforms.items():
            if entry.get("status") != POSTED:
                continue
            differs: dict[str, Any] = {}
            if entry.get("posted_price") is not None and entry["posted_price"] != self.price:
                differs["price"] = entry["posted_price"]
            if entry.get("posted_title") and self.title and entry["posted_title"] != self.title:
                differs["title"] = entry["posted_title"]
            if differs:
                out.append({"platform": key, **differs})
        return out

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data["name"] = self.name
        data["status"] = self.status
        data["verdict"] = price_verdict(self).text if self.research.get("at") else ""
        since = self.online_since()
        data["online_since"] = since
        data["days_online"] = int((time.time() - since) // 86400) if since else None   # „12 napja fent”
        data["research_stale"] = bool(self.research.get("at")) and not research_fresh(self)
        data["drift"] = self.drift()
        return data

    def online_since(self) -> float | None:
        """Mióta van fent (a legkorábban felkerült oldalon, ahol most is fent van); None, ha sehol."""
        posted = [float(e.get("posted_at") or e.get("at") or 0) for e in self.platforms.values()
                  if e.get("status") == POSTED]
        posted = [at for at in posted if at > 0]
        return min(posted) if posted else None

    def stored(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("busy", None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Item:
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        if not values.get("prompt") and data.get("notes"):   # a 0.5-ös változat mezője
            values["prompt"] = str(data["notes"])
        values["busy"] = ""
        values["photos"] = [p for p in values.get("photos") or [] if isinstance(p, str) and PHOTO_RE.match(p)]
        return cls(**values)


def format_price(value: int | float | None) -> str:
    if value is None:
        return "nincs ár"
    return f"{int(round(value)):,}".replace(",", " ") + " Ft"


def parse_price(value: Any) -> int | None:
    """Ár forintban: számjegyekkel („4 500 Ft”, „4.500”) vagy magyarul („ötezer”, „4,5 ezer”, „3 és fél ezer”,
    „4.5k”). Ha a szöveg nem egyetlen összeg (pl. „hát”), ItemError."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ItemError("Az ár egy szám legyen, forintban.")
    text = re.sub(r"(?i)\s|ft|huf|forint|\.(?=\d{3}\b)", "", str(value)).replace(",", ".")
    try:
        price = int(round(float(text)))
    except (ValueError, OverflowError):
        price = only_hu_amount(str(value))
        if price is None:
            raise ItemError("Az ár egy szám legyen, forintban (pl. 4500 vagy „4,5 ezer”).") from None
    if not 0 <= price <= MAX_PRICE:
        raise ItemError("Az ár 0 és 10 millió forint között legyen.")
    return price


def closest_condition(value: str) -> str:
    wanted = fold(value or "").strip()
    if not wanted:
        return ""
    for option in CONDITIONS:
        if fold(option) == wanted:
            return option
    for option in CONDITIONS:
        if wanted in fold(option) or fold(option) in wanted:
            return option
    return ""


# --- fotók ---------------------------------------------------------------------------

_heif_checked = False


def _enable_heif() -> None:
    """Az iPhone-os HEIC képekhez a pillow-heif kell (nem kötelező)."""
    global _heif_checked
    if _heif_checked:
        return
    _heif_checked = True
    try:
        from pillow_heif import register_heif_opener  # type: ignore[import-not-found]
        register_heif_opener()
    except ImportError:
        pass


def _open_image(data: bytes) -> Any:
    from PIL import Image, ImageOps, UnidentifiedImageError

    _enable_heif()
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError) as exc:
        raise ItemError("Ez nem kép, vagy ezt a formátumot nem ismerem. JPG, PNG és WEBP jó; "
                        "az iPhone-os HEIC-hez: pip install pillow-heif") from exc
    image = ImageOps.exif_transpose(image)   # a telefon által elforgatott képek álljanak jól
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.split()[-1])
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")
    return image


def _jpeg(image: Any, side: int, quality: int) -> bytes:
    from PIL import Image

    copy = image.copy()
    copy.thumbnail((side, side), Image.LANCZOS)
    buffer = io.BytesIO()
    copy.save(buffer, "JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def process_photo(data: bytes) -> tuple[bytes, bytes]:
    """Feltöltéshez kész JPEG (legfeljebb 1600 px) és előnézet (360 px)."""
    if len(data) > MAX_PHOTO_BYTES:
        raise ItemError("A kép túl nagy (legfeljebb 25 MB lehet).")
    image = _open_image(data)
    return _jpeg(image, PHOTO_MAX_SIDE, 88), _jpeg(image, THUMB_SIDE, 80)


# --- tárolás ---------------------------------------------------------------------------

class ItemStore:
    """Az eladásra szánt tárgyak. Több szálról is használható (felület és ágens)."""

    def __init__(self, root: Path, max_photos: int = 20,
                 on_change: Callable[[dict[str, Any]], None] | None = None,
                 on_remove: Callable[[str], None] | None = None,
                 platform_keys: list[str] | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_photos = max(1, int(max_photos))
        self.on_change = on_change
        self.on_remove = on_remove
        self.platform_keys = list(platform_keys or PLATFORMS)
        self._lock = threading.RLock()
        self._items: dict[str, Item] = {}
        self._load()

    def _load(self) -> None:
        for folder in sorted(self.root.iterdir()):
            if not (folder.is_dir() and ITEM_ID_RE.match(folder.name)):
                continue
            try:
                data = json.loads((folder / "item.json").read_text(encoding="utf-8"))
                item = Item.from_dict(data)
            except (OSError, ValueError, TypeError):
                continue
            item.id = folder.name
            item.photos = [p for p in item.photos if (folder / p).is_file()]
            self._items[item.id] = item

    # --- belső -------------------------------------------------------------------------

    def folder(self, item_id: str) -> Path:
        if not ITEM_ID_RE.match(item_id or ""):
            raise ItemError(f"Nincs ilyen tárgy: {item_id}")
        return self.root / item_id

    def _save(self, item: Item) -> None:
        folder = self.folder(item.id)
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / "item.json.tmp"
        tmp.write_text(json.dumps(item.stored(), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, folder / "item.json")

    def _changed(self, item: Item, save: bool = True) -> Item:
        item.updated = time.time()
        if save:
            self._save(item)
        if self.on_change is not None:
            try:
                self.on_change(item.public())
            except Exception:  # noqa: BLE001 - a felület hibája ne rontsa el a mentést
                pass
        return item

    # --- lekérdezés ------------------------------------------------------------------------

    def get(self, item_id: str) -> Item:
        with self._lock:
            item = self._items.get(item_id or "")
            if item is None:
                raise ItemError(f"Nincs ilyen tárgy: {item_id}")
            return item

    def find(self, ref: str) -> Item:
        """Azonosító vagy (ha egyértelmű) a név része alapján."""
        ref = (ref or "").strip()
        with self._lock:
            if ref in self._items:
                return self._items[ref]
            wanted = fold(ref)
            matches = [i for i in self._items.values() if wanted and wanted in fold(i.name)]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise ItemError(f"Több tárgy is illik erre: {ref}. Az azonosítóval add meg (item_list).")
        raise ItemError(f"Nincs ilyen tárgy: {ref}. A tárgyakat az item_list eszköz sorolja fel.")

    def all(self) -> list[Item]:
        with self._lock:
            return sorted(self._items.values(), key=lambda i: i.created, reverse=True)

    # --- módosítás -------------------------------------------------------------------------

    def create(self, **values: Any) -> Item:
        with self._lock:
            item_id = secrets.token_hex(4)
            while item_id in self._items or (self.root / item_id).exists():
                item_id = secrets.token_hex(4)
            now = time.time()
            item = Item(id=item_id, created=now, updated=now)
            self._apply(item, values)    # hibás adatnál ItemError, és nem jön létre semmi
            self._items[item_id] = item
            return self._changed(item)

    def _apply(self, item: Item, values: dict[str, Any]) -> list[str]:
        changed = []
        for key, value in values.items():
            if key == "price":
                value = parse_price(value)
            elif key == "condition":
                value = closest_condition(str(value or "")) if value else ""
            elif key == "kind":
                value = fold(str(value or "")).strip()
                value = next((k for k in KINDS if fold(k) == value), "egyéb" if value else "")
            elif key == "targets":
                if not isinstance(value, (list, tuple)):
                    raise ItemError("A hirdetési oldalakat listában add meg.")
                unknown = [t for t in value if t not in self.platform_keys]
                if unknown:
                    raise ItemError(f"Ismeretlen hirdetési oldal: {', '.join(map(str, unknown))}")
                value = [t for t in self.platform_keys if t in value]
            elif key == "research_wanted":
                value = bool(value)
            elif key == "extracted_sig":
                value = str(value or "")[:64]
            elif key in TEXT_LIMITS:
                text = str(value or "")
                if key in MULTILINE:
                    text = "\n".join(" ".join(line.split()) for line in text.strip().splitlines())
                    text = re.sub(r"\n{3,}", "\n\n", text)
                else:
                    text = " ".join(text.split())
                value = text[:TEXT_LIMITS[key]]
            else:
                raise ItemError(f"Ezt nem lehet módosítani: {key}")
            if getattr(item, key) != value:
                setattr(item, key, value)
                changed.append(key)
        return changed

    def update(self, item_id: str, user: bool = False, **values: Any) -> Item:
        """Módosítás. `user=True`: a felhasználó írta át, ezért Jarvis később nem írja felül."""
        with self._lock:
            item = self.get(item_id)
            if not user:
                values = {k: v for k, v in values.items() if k not in item.edited}
            changed = self._apply(item, values)
            if user:
                item.edited = sorted(set(item.edited) | {k for k in changed if k in (*LISTING_FIELDS, "price")})
            return self._changed(item)

    def set_busy(self, item_id: str, text: str) -> None:
        with self._lock:
            item = self._items.get(item_id)
            if item is not None and item.busy != text:
                item.busy = text
                self._changed(item, save=False)

    def set_pending(self, item_id: str, pending: dict[str, Any] | None) -> Item:
        with self._lock:
            item = self.get(item_id)
            item.pending = dict(pending or {})
            return self._changed(item)

    def set_research(self, item_id: str, research: dict[str, Any]) -> Item:
        with self._lock:
            item = self.get(item_id)
            item.research = research
            return self._changed(item)

    def mark(self, item_id: str, platform: str = "", status: str = POSTED, url: str = "",
             note: str = "", sold: bool | None = None) -> Item:
        with self._lock:
            item = self.get(item_id)
            if platform:
                if platform not in self.platform_keys:
                    raise ItemError(f"Ismeretlen oldal: {platform}. Lehet: {', '.join(self.platform_keys)}")
                if status not in (POSTED, NOT_POSTED, FAILED):
                    raise ItemError(f"Az állapot „{POSTED}”, „{NOT_POSTED}” vagy „{FAILED}” lehet.")
                entry: dict[str, Any] = {"status": status, "at": time.time()}
                previous = item.platforms.get(platform, {})
                if url.startswith(("http://", "https://")):
                    entry["url"] = url[:300]
                elif status == POSTED and previous.get("url"):
                    entry["url"] = previous["url"]
                if note:
                    entry["note"] = note[:300]
                if status == POSTED:
                    # Ugyanaz a hirdetés (pl. „Itt kelt el.” megjegyzéssel, vagy egy sikertelen levétel után): mióta
                    # van fent, a feltöltéskori ár és cím, és amit a háttérben láttunk róla, marad (a régi adatoknál
                    # a legutóbbi jelölés ideje). Új hirdetésnél (új címen) minden a mostani.
                    if previous.get("status") == POSTED and previous.get("url", entry.get("url")) == entry.get("url"):
                        entry.update({k: previous[k] for k in REMARK_KEEPS if k in previous})
                        entry.setdefault("posted_at", previous.get("at") or entry["at"])
                    entry.setdefault("posted_at", entry["at"])
                    if item.price is not None:
                        entry.setdefault("posted_price", item.price)
                    if item.title:
                        entry.setdefault("posted_title", item.title)
                item.platforms[platform] = entry
            if sold is not None:
                item.sold = bool(sold)
            return self._changed(item)

    def clear_note(self, item_id: str, platform: str) -> Item:
        """Egy új próbálkozás előtt: az előzőről szóló megjegyzés (pl. „el kell fogadni a feltételeket”) már
        nem érvényes, és a modellt csak félrevezetné."""
        with self._lock:
            item = self.get(item_id)
            entry = item.platforms.get(platform)
            if entry is None or "note" not in entry:
                return item
            del entry["note"]
            return self._changed(item)

    def attach_dossier(self, item_id: str, platform: str, name: str) -> Item:
        """Egy oldal sorához hozzáköti a hibacsomagot (a felületen megnyitható)."""
        with self._lock:
            item = self.get(item_id)
            entry = item.platforms.get(platform)
            if entry is None:
                entry = item.platforms[platform] = {"status": NOT_POSTED, "at": time.time()}
            entry["dossier"] = str(name)[:120]
            return self._changed(item)

    def set_check(self, item_id: str, platform: str, state: str, when: float | None = None,
                  detail: str = "", error: str = "") -> Item:
        """A háttérben megnézett hirdetés állapota (fent van, elkelt, eltűnt): mikor, mit láttunk és miből.
        `error`: a hirdetés most nem nyílt meg; az előző eredmény megjegyzése marad, a hiba külön (check_error)."""
        with self._lock:
            item = self.get(item_id)
            entry = item.platforms.get(platform)
            if entry is None:
                return item
            entry["checked_at"] = time.time() if when is None else float(when)
            entry["check"] = state
            if error:
                entry["check_error"] = error[:200]
                return self._changed(item)
            entry.pop("check_error", None)
            if detail:
                entry["check_note"] = detail[:200]
            else:
                entry.pop("check_note", None)
            return self._changed(item)

    def dismiss_check(self, item_id: str, platform: str) -> Item:
        """„Nem igaz, fent van”: a háttérellenőrzés jelzése (elkelt, eltűnt) téves. A jelzés eltűnik, és ha a
        következő ellenőrzés pontosan ugyanazt látja ennél a hirdetésnél, nem jelzi újra; egy másik jelet (pl. a
        „Foglalt” után „Eladva”) igen (lásd listings.py)."""
        with self._lock:
            item = self.get(item_id)
            entry = item.platforms.get(platform)
            if entry is None or entry.get("status") != POSTED:
                raise ItemError("Ezen az oldalon a nyilvántartás szerint nincs fent.")
            if entry.get("check") in ("sold", "gone"):
                entry["check_dismissed"] = entry["check"]
                entry["check_dismissed_note"] = str(entry.get("check_note") or "")   # pl. „Foglalt”, nem „Eladva”
            entry["check"] = "live"
            entry.pop("check_note", None)
            return self._changed(item)

    def set_reminded(self, item_id: str, when: float | None = None) -> Item:
        """Feljegyzi, hogy szóltunk: régóta fent van, de még nem kelt el (hét napig nem szól újra)."""
        with self._lock:
            item = self.get(item_id)
            item.reminded = time.time() if when is None else float(when)
            return self._changed(item)

    def delete(self, item_id: str) -> None:
        """A tárgy a _torolt mappába kerül (onnan kézzel visszahozható)."""
        with self._lock:
            self.get(item_id)
            trash = self.root / "_torolt"
            trash.mkdir(exist_ok=True)
            shutil.move(str(self.folder(item_id)), str(trash / f"{item_id}-{int(time.time())}"))
            del self._items[item_id]
        if self.on_remove is not None:
            try:
                self.on_remove(item_id)
            except Exception:  # noqa: BLE001
                pass

    # --- fotók -----------------------------------------------------------------------------

    def photo_file(self, item_id: str, name: str, thumb: bool = False) -> Path:
        if not PHOTO_RE.match(name or ""):
            raise ItemError("Nincs ilyen fotó.")
        item = self.get(item_id)
        if name not in item.photos:
            raise ItemError("Nincs ilyen fotó.")
        folder = self.folder(item_id)
        return folder / "thumbs" / name if thumb else folder / name

    def photo_paths(self, item_id: str) -> list[Path]:
        item = self.get(item_id)
        folder = self.folder(item_id)
        return [folder / name for name in item.photos]

    def add_photo(self, item_id: str, data: bytes) -> Item:
        full, thumb = process_photo(data)   # a lassabb rész zár nélkül
        with self._lock:
            item = self.get(item_id)
            if len(item.photos) >= self.max_photos:
                raise ItemError(f"Egy tárgyhoz legfeljebb {self.max_photos} fotó tartozhat.")
            folder = self.folder(item_id)
            (folder / "thumbs").mkdir(parents=True, exist_ok=True)
            numbers = [int(p[:2]) for p in item.photos] or [0]
            name = f"{max(numbers) + 1:02d}.jpg"
            (folder / name).write_bytes(full)
            (folder / "thumbs" / name).write_bytes(thumb)
            item.photos.append(name)
            return self._changed(item)

    def remove_photo(self, item_id: str, name: str) -> Item:
        with self._lock:
            path = self.photo_file(item_id, name)
            item = self.get(item_id)
            item.photos.remove(name)
            for file in (path, path.parent / "thumbs" / name):
                try:
                    file.unlink()
                except OSError:
                    pass
            return self._changed(item)

    def reorder_photos(self, item_id: str, names: list[str]) -> Item:
        with self._lock:
            item = self.get(item_id)
            wanted = [n for n in names if n in item.photos]
            rest = [n for n in item.photos if n not in wanted]
            item.photos = list(dict.fromkeys(wanted)) + rest
            return self._changed(item)


# --- az adatok a leírásból ----------------------------------------------------------------

EXTRACT_SYSTEM = (
    "Az eladó leírásából kigyűjtöd egy használt (vagy új) holmi hirdetésének adatait, magyarul.\n"
    "Szabályok:\n"
    "- Csak azt írd, ami a leírásból kiderül. Amit nem mondott, hagyd üresen (az ár ilyenkor 0). "
    "Ne találgass, és ne a fotók alapján gondolkodj, azokat nem látod.\n"
    "- product: röviden, mi ez (pl. „fekete póló”, „RTX 3060 Ti videókártya”).\n"
    "- kind: ezek egyike: " + ", ".join(KINDS) + " (a táska, öv, sapka, ékszer: kiegészítő).\n"
    "- title: legfeljebb 60 karakter, amit a vevő keres: márka, típus, fontos tulajdonság, méret.\n"
    "- description: 2–4 rövid, természetes mondat a leírásban szereplő adatokból (állapot, hibák, "
    "méret, tartozékok). Ne ígérj olyat, amit az eladó nem mondott (szállítás, garancia, alku, csere), "
    "és ne írj árat.\n"
    "- category: a legvalószínűbb kategóriaút, pl. „Nők > Ruhák > Pólók” vagy „Számítástechnika > "
    "Videókártyák”.\n"
    "- condition: ezek egyike: " + "; ".join(CONDITIONS) + ". Csak ha a leírásból kiderül (pl. "
    "„egyszer hordott” = Nagyon jó, „bontatlan” = Új), különben üres.\n"
    "- color: a szín magyarul (pl. „fekete”, „világoskék”), csak ha a leírásból kiderül, különben üres.\n"
    "- price: forintban, egész szám (pl. „500 Ft-ért” = 500, „5 ezer” = 5000); ha nincs megadva, 0.\n"
    "- location: csak ha az eladó említ átvételi helyet (pl. „Debrecenben”). shipping: csak ha említ "
    "szállítást (pl. „csak személyes átvétel”, „Foxpost-tal is”).\n"
    "- search_query: 2–4 szó a hasonló hirdetések kereséséhez (márka és típus, méret nélkül)."
)

# A JSON-válaszok felső határa (token): a hirdetés adatai (2–4 mondatos leírással) és az ár-ajánlás bőven
# beleférnek, egy beragadt, ismétlődő válasz viszont nem foglalja percekig a modellt.
EXTRACT_MAX_TOKENS = 800
SELECT_MAX_TOKENS = 400
EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **{key: {"type": "string"} for key in LISTING_FIELDS},
        "kind": {"type": "string", "enum": KINDS},
        "condition": {"type": "string", "enum": [*CONDITIONS, ""]},
        "price": {"type": "integer"},
    },
    "required": [*LISTING_FIELDS, "kind", "price"],
}


# Ha az EXTRACT_SYSTEM vagy a séma érdemben változik, emeld: a régi kiolvasások egyszer újra lefutnak.
EXTRACT_VERSION = "1"
# Ha a hirdetés már fent van valahol, ezeket Jarvis magától nem írja át (a többi oldalon is ugyanaz a
# hirdetés legyen, és a begyakorolt lépések kulcsa se változzon); kézzel persze átírhatod.
FROZEN_WHEN_POSTED = ("title", "description", "category", "product", "kind")


def extract_signature(prompt: str) -> str:
    """Melyik leírásból (és melyik kiolvasó-változattal) készültek az adatok."""
    return hashlib.sha1(f"{EXTRACT_VERSION}\n{prompt}".encode("utf-8")).hexdigest()


class ListingExtractor:
    """Az eladó egy-két mondatos leírásából kiolvassa a hirdetés adatait (egy modellhívás).

    Ugyanabból a leírásból csak egyszer: az árdöntés vagy a „Feltöltés a hiányzó helyekre” már nem olvassa ki
    újra (az idő, és a hirdetés sem változik oldalanként). Ha visszakérdezett, a válaszból csak a hiányzó
    adatokat olvassa ki (rövid séma, rövid válasz)."""

    def __init__(self, llm: Any, store: ItemStore):
        self.llm = llm
        self.store = store

    @staticmethod
    def up_to_date(item: Item) -> bool:
        """Ebből a leírásból már kiolvasta, és megvan a cím, a leírás és az ár."""
        return (item.extracted_sig == extract_signature(item.prompt)
                and bool(item.title and item.description and item.price))

    @staticmethod
    def appended(item: Item) -> bool:
        """A legutóbbi kiolvasás óta a leírás csak bővült (pl. a visszakérdezésre adott válasszal)."""
        if not item.extracted_sig:
            return False
        cut = item.prompt.rfind("\n")
        while cut > 0:
            if extract_signature(item.prompt[:cut].strip()) == item.extracted_sig:
                return True
            cut = item.prompt.rfind("\n", 0, cut)
        return False

    def extract(self, item_id: str, only: list[str] | None = None) -> Item:
        """Kiolvassa az adatokat. `only`: csak ezeket a mezőket (pl. a visszakérdezett állapotot és várost)."""
        item = self.store.get(item_id)
        if not item.prompt.strip():
            raise ItemError("Írd le röviden, mit adsz el (pl. „Zara L-es fekete póló, 500 Ft”).")
        schema, request = EXTRACT_SCHEMA, f"Az eladó leírása:\n{item.prompt}"
        if only:
            only = [key for key in dict.fromkeys(only) if key in EXTRACT_SCHEMA["properties"]]
            schema = {"type": "object", "required": only,
                      "properties": {key: EXTRACT_SCHEMA["properties"][key] for key in only}}
            request += f"\n\nMost csak ezeket töltsd ki (a többi már megvan): {', '.join(only)}."
        messages = [{"role": "system", "content": EXTRACT_SYSTEM}, {"role": "user", "content": request}]
        data = self.llm.chat_json(messages, schema, temperature=0.2, max_tokens=EXTRACT_MAX_TOKENS)
        values: dict[str, Any] = {key: str(data.get(key) or "").strip() for key in LISTING_FIELDS}
        values["kind"] = str(data.get("kind") or "").strip()
        try:
            values["price"] = parse_price(data.get("price")) or None
        except ItemError:
            values["price"] = None
        values = {k: v for k, v in values.items() if v not in ("", None)}   # amit nem mondott, nem törli
        if only:
            values = {k: v for k, v in values.items() if k in only}
        if any(e.get("status") == POSTED for e in item.platforms.values()):
            values = {k: v for k, v in values.items() if k not in FROZEN_WHEN_POSTED}
        return self.store.update(item_id, **values, extracted_sig=extract_signature(item.prompt))


def missing_needs(item: Item, settings: dict[str, Any]) -> list[tuple[Need, list[str]]]:
    """Ami a kiválasztott oldalakhoz még hiányzik: (igény, melyik oldalak kérik)."""
    out = []
    for need in needs_for(item.targets):
        if need.clothing_only and item.kind not in CLOTHING:
            continue
        have = item.price if need.field == "price" else getattr(item, need.field)
        if not have and need.setting:
            have = settings.get(need.setting)
        if not have:
            sites = [PLATFORMS[s].name for s in need.sites if s in item.targets] if need.sites else []
            out.append((need, sites))
    return out


def question_for(item: Item, missing: list[tuple[Need, list[str]]]) -> str:
    """Pl.: „A(z) „póló” feltöltéséhez még kell: állapot; márka, méret (Vinted). Írd meg, és folytatom.”"""
    groups: list[tuple[list[str], list[str]]] = []      # az azonos oldalakra vonatkozók együtt
    for need, sites in missing:
        if groups and groups[-1][1] == sites:
            groups[-1][0].append(need.label)
        else:
            groups.append(([need.label], sites))
    parts = [", ".join(labels) + (f" ({', '.join(sites)})" if sites else "") for labels, sites in groups]
    what = f"A(z) „{item.product}” feltöltéséhez" if item.product else "A feltöltéshez"
    text = f"{what} még kell: {'; '.join(parts)}."
    if any(need.setting for need, _ in missing):
        text += " A várost és a szállítást elég egyszer megadni az Alapadatoknál, akkor máskor nem kérdezem."
    return text + " Írd meg, és folytatom."


# --- árkutatás ---------------------------------------------------------------------------

SELECT_SYSTEM = (
    "Használt holmik árazásában segítesz. Megkapod az eladó tárgyát és hirdetési oldalak "
    "találatait (számozva, árral). Válaszd ki azokat, amelyek tényleg hasonlók: ugyanaz a típus, "
    "a márka, a méret és az állapot minél közelebb. A más dolgokat (más termék, kiegészítő, több "
    "darab egy csomagban) hagyd ki. Javasolj reális árat forintban, amiért várhatóan gyorsan elkel; "
    "használt holminál inkább a középérték körül vagy alatta. reason: egy rövid mondat magyarul."
)

SELECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "similar": {"type": "array", "items": {"type": "integer"}},
        "suggested_price": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["similar", "suggested_price", "reason"],
}


def round_price(value: float) -> int:
    """Kerek ár: 100 forintra (500 alatt 50-re)."""
    step = 50 if value < 500 else 100
    return int(max(step, round(value / step) * step))


def describe_item(item: Item) -> str:
    parts = [item.name]
    for label, value in (("márka", item.brand), ("méret", item.size), ("állapot", item.condition),
                         ("kategória", item.category)):
        if value:
            parts.append(f"{label}: {value}")
    return "; ".join(parts)


# --- előszűrés és robusztus statisztika (modell nélkül) ---------------------------------------

RESEARCH_MAX_AGE = 7 * 86400   # egy hét után az árkutatás elavult: a következő árazás előtt újra megnézi
JUDGE_ROWS = 25                # a modell legfeljebb ennyi találatot kap: a tárgyhoz leginkább illőket
NEW_CONDITIONS = {"Új, címkével", "Új"}
WORN_CONDITIONS = {"Elfogadható", "Hibás"}

_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Ezek szinte minden hirdetésben ott vannak, ezért arról, hogy ugyanaz-e a termék, nem mondanak semmit.
_STOPWORDS = {
    "az", "egy", "es", "is", "de", "vagy", "nem", "meg", "mar", "csak", "mint", "the", "and", "for", "with",
    "uj", "ujszeru", "hasznalt", "elado", "eladom", "jo", "nagyon", "szep", "allapot", "allapotu", "allapotban",
    "meret", "meretu", "szinu", "ft", "huf", "forint", "db", "darab", "szemelyes", "atvetel", "postazas",
    "szallitas", "foxpost", "new", "used", "size", "noi", "ferfi", "unisex",
}
# Kereső- és cserehirdetés: valaki venni vagy cserélni akar, az ára nem eladási ár. A „csere” csak akkor
# számít, ha az eladás szava nincs mellette („Eladó vagy csere” még eladás).
_WANTED_RE = re.compile(r"\b(keresek|keresem|keresunk|veszek|vennek|vennem|megvennem|felvasar\w*|beszamit\w*)\b")
_SWAP_RE = re.compile(r"\b(csere|cseret|csereben|cserere|cserelnem|cserelnek|cserelek|elcserelnem|cserebere)\b")
_SALE_RE = re.compile(r"\belad")
# Csomag vagy több darab egyben: az ár nem egy darabé. A darabszám („3 db”, „x2”) csak elektronikán kívül
# számít: ott a „2 db ventilátorral” vagy a „PCIe x4” a tárgy leírása, nem csomag.
_LOT_RE = re.compile(r"\b(csomag|csomagban|csomagot|csomagkent|szett|szettben|lot|kotegben|tetelben)\b|"
                     r"\btobb (db|darab)\b")
_COUNT_RE = re.compile(r"\b([2-9]|[1-9]\d) ?(db|darab|par)\b|\bx[2-9]\b")
# Alkatrész, hibás, vagy csak a doboza: nem a működő tárgy ára („nem hibás”, „nem törött” nem számít).
_PARTS_RE = re.compile(r"(?<!nem )\b(alkatresz\w*|hibas|hibasan|hibasak|javitasra|torott|for parts|"
                       r"csak (a )?doboz\w*|ures doboz\w*)\b|\b(nem mukodik|nem mukodo|mukodeskeptelen)\b")


def research_fresh(item: Item, now: float | None = None) -> bool:
    """Van-e egy hétnél frissebb árkutatás (a régebbit a következő árazás előtt újra megnézi)."""
    at = float((item.research or {}).get("at") or 0)
    now = time.time() if now is None else now
    return bool(at) and now - at < RESEARCH_MAX_AGE


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(fold(text or "")) if len(t) > 1 and t not in _STOPWORDS}


def _shared(wanted: set[str], have: set[str]) -> set[str]:
    """A közös szavak; a toldalékos és az összetett alak is számít (blézer ~ blézert, farmer ~ farmernadrág)."""
    return {w for w in wanted if w in have or any((len(w) >= 4 and h.startswith(w)) or
                                                  (len(h) >= 4 and w.startswith(h)) for h in have)}


def listing_key(url: str) -> str:
    """Egy hirdetés azonosítója a webcíméből: a gép és a hirdetés száma (ha van), különben az útvonal. Így a
    saját hirdetésed más paraméterekkel vagy más címmel (slug) is felismerhető."""
    parts = urllib.parse.urlsplit(url or "")
    host = (parts.hostname or "").removeprefix("www.")
    number = re.search(r"\d{5,}", parts.path)
    return f"{host}#{number.group(0)}" if number else host + parts.path.rstrip("/").lower()


def _same_listing(words: set[str], price: int, other: set[str], other_price: int) -> bool:
    """Ugyanaz a hirdetés még egyszer (pl. az eladó több oldalra is feltette): szinte ugyanaz a cím és az ár."""
    if not words or not other or abs(price - other_price) > 0.05 * max(price, other_price, 1):
        return False
    return len(words & other) / len(words | other) >= 0.8


def prefilter(item: Item, rows: list[dict[str, Any]], limit: int = JUDGE_ROWS
              ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Modell nélküli előszűrés a bíráló modellhívás előtt. Kimarad: a kereső- és a cserehirdetés, a csomag,
    az alkatrész és a hibás (hacsak a te tárgyad nem ilyen), ami egy szóban sem egyezik a tárgyad címével,
    márkájával, nevével vagy keresőszavával, a saját hirdetésed, és ugyanannak a hirdetésnek az ismétlése.
    A maradékból a legjobban egyező `limit` darab marad, az eredeti sorrendben (a végső szót a modell
    mondja ki). Visszaadja a megmaradtakat, és hogy miért mennyi esett ki."""
    own = fold(" ".join((item.title, item.product, item.description, item.brand)))
    lot_ok = bool(_LOT_RE.search(own) or _COUNT_RE.search(own))
    parts_ok = item.condition == "Hibás" or bool(_PARTS_RE.search(own))
    counts = item.kind != "elektronika"
    wanted = _tokens(" ".join((item.title, item.brand, item.product, item.search_query)))
    brand = _tokens(item.brand)
    size = set(_TOKEN_RE.findall(fold(item.size)))
    mine = {listing_key(e["url"]) for e in item.platforms.values() if isinstance(e, dict) and e.get("url")}
    dropped: dict[str, int] = {}
    scored: list[tuple[float, int, dict[str, Any]]] = []
    seen: list[tuple[set[str], int]] = []
    for index, row in enumerate(rows):
        text = fold(str(row.get("text") or ""))
        words, price = _tokens(text), int(row.get("price") or 0)
        reason = ""
        if row.get("url") and listing_key(str(row["url"])) in mine:
            reason = "saját"
        elif _WANTED_RE.search(text) or (_SWAP_RE.search(text) and not _SALE_RE.search(text)):
            reason = "kereső"
        elif not lot_ok and (_LOT_RE.search(text) or (counts and _COUNT_RE.search(text))):
            reason = "csomag"
        elif not parts_ok and _PARTS_RE.search(text):
            reason = "alkatrész"
        elif wanted and not _shared(wanted, words):
            reason = "más termék"
        elif any(_same_listing(words, price, other, other_price) for other, other_price in seen):
            reason = "ismétlődő"
        if reason:
            dropped[reason] = dropped.get(reason, 0) + 1
            continue
        seen.append((words, price))
        score = (len(_shared(wanted, words)) + (1.0 if brand and _shared(brand, words) else 0.0)
                 + (0.5 if size and size & set(_TOKEN_RE.findall(text)) else 0.0))
        scored.append((score, index, row))
    best = sorted(scored, key=lambda s: (-s[0], s[1]))[:max(1, limit)]
    if len(scored) > len(best):
        dropped["kevésbé illő"] = len(scored) - len(best)
    return [row for _, _, row in sorted(best, key=lambda s: s[1])], dropped


def price_stats(prices: list[int] | list[float]) -> dict[str, Any]:
    """Robusztus árstatisztika: a kilógó árakat (pl. egy tízszeres vagy egy 1 forintos hirdetés) a
    negyedek közti terjedelemmel kidobja, a maradékból alsó negyed (p25), medián és felső negyed (p75).
    „bizonytalan”, ha ötnél kevesebb ár maradt, vagy nagyon szórnak (a sáv a medián 60%-ánál szélesebb)."""
    values = sorted(int(p) for p in prices)
    if not values:
        return {}
    if len(values) >= 4:
        q1, middle, q3 = statistics.quantiles(values, n=4, method="inclusive")
        spread = max(q3 - q1, 0.2 * middle)   # sok egyforma árnál se essen ki minden kicsit eltérő
        values = [v for v in values if q1 - 1.5 * spread <= v <= q3 + 1.5 * spread]
    if len(values) >= 2:
        p25, median, p75 = statistics.quantiles(values, n=4, method="inclusive")
    else:
        p25 = median = p75 = values[0]
    confidence = "bizonytalan" if len(values) < 5 or p75 - p25 > 0.6 * median else "megbízható"
    return {"min": values[0], "max": values[-1], "p25": int(round(p25)), "median": int(round(median)),
            "p75": int(round(p75)), "n": len(values), "confidence": confidence}


class PriceResearcher:
    """Megnézi a hirdetési oldalakon, mennyiért kínálnak hasonlót."""

    def __init__(self, llm: Any, store: ItemStore, browser: Callable[[], Any], sites: list[Platform]):
        self.llm = llm
        self.store = store
        self.browser = browser
        self.sites = [site for site in sites if site.search_url]

    def query_for(self, item: Item) -> str:
        if item.search_query:
            return item.search_query
        words = " ".join(p for p in (item.brand, item.product or item.title) if p).split()
        return " ".join(words[:5])

    def research(self, item_id: str, progress: Callable[[str], None] | None = None,
                 only: list[str] | None = None) -> Item:
        item = self.store.get(item_id)
        query = self.query_for(item)
        if not query:
            raise ItemError("Az árkutatáshoz tudnom kell, mi ez (írd le röviden).")
        sites = [s for s in self.sites if not only or s.key in only] or self.sites
        if not sites:
            raise ItemError("Nincs beállítva oldal az árkutatáshoz ([work] research_sites).")
        results: list[dict[str, Any]] = []
        errors: list[str] = []
        for site in sites:
            if progress:
                progress(f"{site.name}: „{query}”")
            url = site.search_url.format(q=urllib.parse.quote(query))
            try:
                found = self.browser().extract_listings(url)
            except Exception as exc:  # noqa: BLE001 - egy oldal hibája ne állítsa le a többit
                errors.append(f"{site.name}: {str(exc).splitlines()[0][:160]}")
                continue
            if not found:
                errors.append(f"{site.name}: nem találtam árakat (lehet, hogy ellenőrzést kér az oldal)")
            for entry in found[:20]:
                row = {"site": site.name, "text": entry["text"][:180], "price": int(entry["price"]),
                       "url": entry["url"]}
                if entry.get("auction"):
                    row["auction"] = True
                results.append(row)
        rows, dropped = prefilter(item, results)
        research: dict[str, Any] = {"query": query, "at": time.time(), "count": len(results), "kept": len(rows),
                                    "dropped": dropped, "similar": [], "errors": errors}
        if rows:
            if progress:
                progress("a hasonlók kiválasztása")
            research.update(self._judge(item, rows))
        elif results:
            research["reason"] = ("A találatok között nem volt a tárgyadhoz hasonló (kereső- vagy csomaghirdetés, "
                                  "alkatrész vagy más termék).")
        return self.store.set_research(item_id, research)

    def _judge(self, item: Item, results: list[dict[str, Any]]) -> dict[str, Any]:
        listing = "\n".join(f"{n}. [{r['site']}] {r['text']} – {format_price(r['price'])}"
                            + (" (licit: a mostani ajánlat, nem végleges ár)" if r.get("auction") else "")
                            for n, r in enumerate(results, 1))
        messages = [
            {"role": "system", "content": SELECT_SYSTEM},
            {"role": "user", "content": f"A tárgy: {describe_item(item)}\n\nTalálatok:\n{listing}"},
        ]
        data = self.llm.chat_json(messages, SELECT_SCHEMA, temperature=0.2, max_tokens=SELECT_MAX_TOKENS)
        picked = []
        for number in data.get("similar") or []:
            try:
                index = int(number) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(results) and results[index] not in picked:
                picked.append(results[index])
        out: dict[str, Any] = {"similar": picked[:12], "reason": str(data.get("reason") or "")[:300]}
        if not picked:
            out["reason"] = out["reason"] or "Nem találtam elég hasonló hirdetést."
            return out
        fixed = [r for r in picked if not r.get("auction")]
        basis = fixed if len(fixed) >= 3 else picked    # a licit csak a mostani ajánlat: ha lehet, kimarad
        stats = price_stats([r["price"] for r in basis])
        kept = [r for r in basis if stats["min"] <= r["price"] <= stats["max"]]
        out["similar"] = kept[:12]
        out.update(stats)
        if len(kept) < len(basis):
            out["outliers"] = len(basis) - len(kept)
        try:
            suggested = int(data.get("suggested_price") or 0)
        except (TypeError, ValueError):
            suggested = 0
        if not stats["min"] * 0.5 <= suggested <= stats["max"] * 1.2:
            suggested = stats["median"]   # irreális javaslat helyett a középérték
        out["suggested"] = round_price(min(max(suggested, stats["p25"]), stats["p75"]))
        return out


@dataclass
class Verdict:
    pause: bool               # érdemes megállni, és megkérdezni az árat
    text: str
    suggested: int | None = None


def _condition_note(item: Item, p25: int, p75: int) -> str:
    """Az állapot szerint a sáv melyik része reális (csak tájékoztatás, a javasolt árat nem írja át)."""
    if p25 >= p75:
        return ""
    if item.condition in NEW_CONDITIONS:
        return f" Mivel új, a sáv felső része (kb. {format_price(p75)}) is reális."
    if item.condition in WORN_CONDITIONS:
        return f" Az állapota miatt inkább a sáv alsó része (kb. {format_price(p25)}) a reális."
    return ""


def price_verdict(item: Item) -> Verdict:
    """Az árkutatás alapján: rendben van-e az ár. Csak ajánlás; a döntés az eladóé. A szokásos sáv a hasonlók
    alsó és felső negyede közt van: ha az árad e fölött, és a középérték 1,25-szörösénél is feljebb van,
    megáll és megkérdezi."""
    r = item.research or {}
    similar = r.get("similar") or []
    prices = sorted(int(s["price"]) for s in similar if isinstance(s, dict) and isinstance(s.get("price"), (int, float)))
    price = item.price or 0
    if not price:
        return Verdict(False, "Még nincs ár, ezért nem tudom összevetni a hasonlókkal.")
    if len(prices) < 3:
        found = (f"Csak {len(prices)} hasonló hirdetést találtam, ez kevés az összevetéshez"
                 if prices else "Nem találtam hasonló hirdetést az összevetéshez")
        return Verdict(False, f"{found}; marad az árad ({format_price(price)}).")
    stats = price_stats(prices)       # a régi (negyedek nélküli) kutatásnál ebből
    median = int(r.get("median") or stats["median"])
    p25, p75 = int(r.get("p25") or stats["p25"]), int(r.get("p75") or stats["p75"])
    span = (f"többnyire {format_price(p25)} és {format_price(p75)} között (középérték {format_price(median)})"
            if p25 < p75 else f"{format_price(median)} körül")
    unsure = (" Kevés vagy nagyon eltérő ár alapján mondom, ezért csak tájékoztató."
              if (r.get("confidence") or stats["confidence"]) == "bizonytalan" else "")
    note = _condition_note(item, p25, p75)
    if price > max(median * 1.25, p75):
        suggested = r.get("suggested") or round_price(median)
        return Verdict(True, f"Hasonlót {span} kínálnak.{unsure}{note} {format_price(price)}-ért valószínűleg "
                             f"nehezen kel el, én {format_price(suggested)}-ot kérnék. Maradjon a te árad, vagy "
                             f"legyen {format_price(suggested)}?", suggested)
    if price < min(median * 0.6, p25):
        return Verdict(False, f"Ennél többet is kérhetnél: hasonlót {span} kínálnak.{note} Marad a te árad "
                              f"({format_price(price)}).")
    return Verdict(False, f"Az árad rendben van: hasonlót {span} kínálnak.{unsure}{note}")
