"""Adás: eladásra szánt tárgyak, hirdetési oldalak, és a hirdetés adatai a leírásodból.

A menet: kiválasztod, hova kerüljön (Vinted, Jófogás, Vatera, Hardverapró), feltöltöd a
fotókat, és leírod egy-két mondatban, mit adsz el („Zara L-es fekete póló, egyszer hordott,
500 Ft”). Jarvis ebből egyetlen modellhívással kiolvassa az adatokat (a fotókat nem nézi, az
sok idő lenne). Ha valami hiányzik ahhoz, amit a kiválasztott oldalak kérnek, visszakérdez;
különben indulhat a feltöltés. Kérésre előtte megnézi, mennyiért kínálnak hasonlót.

Egy tárgy egy mappa a munka mappában: item.json (az adatok), a fotók (01.jpg, 02.jpg…) és a
kis előnézetek (thumbs/). A törölt tárgy a _torolt mappába kerül, így vissza lehet hozni. A fotókat
felvételkor modell nélkül megnézi (photos.py: homályos, sötét, kicsi, ismétlődő); ez csak jelzés.
"""
from __future__ import annotations

import base64
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

from . import photos as photocheck
from . import refresh as refreshing
from .feeds import FeedError, newest_first
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

LISTING_FIELDS = ("product", "title", "description", "category", "gender", "brand", "size", "condition",
                  "color", "material", "location", "shipping", "search_query")
# Kinek szól a ruha (a Vinted kategóriafájának első szintje): a kategóriaút első szava ehhez igazodik.
GENDERS = {"női": "Nők", "férfi": "Férfiak", "gyerek": "Gyerekek"}
GENDER_LEVELS = {"no", "nok", "noi", "ferfi", "ferfiak", "gyerek", "gyerekek", "gyermek", "women", "men", "kids"}
# A leírás vége (a te nevedben): ezt a Jarvis teszi hozzá, nem a modell írja, és mindig ugyanez (ruhánál, cipőnél,
# kiegészítőnél „ruhát”, máshol „holmit”). Fölötte az, amit kifejezetten kértél, alatta a kulcsszavak.
DESCRIPTION_FOOTER = ("Ha kérdésed van, vagy több képet szeretnél, írj bátran! Nézz körül a profilomon is, ott még "
                      "sok más {what} találsz. Ha egyszerre több darabot veszel, csomagban olcsóbban kapod meg.")
# A korábbi, váltakozó zárószövegek (az egyikből „van még sok ruhát;” lett): a még fel nem töltött tárgyak leírásában
# betöltéskor a mostanira cseréli (refresh_footer).
OLD_FOOTERS = (
    "Ha kell pontos méret vagy több kép, írj nyugodtan! A profilomon még sok {what} találsz: ha többet rendelsz "
    "egyben, a csomag-összeállítással olcsóbban kapod meg.",
    "Kérdés esetén vagy ha több képet szeretnél, írj bátran! Nézz körül a profilomon is, van még sok {what}; több "
    "darab egyben, csomagban olcsóbban jön ki.",
    "Pontos méretet vagy további fotót szívesen küldök, csak írj! A profilomon találsz még sok {what}, és ha "
    "többet viszel egyben, csomagban olcsóbb.",
)
# A leírásba alapból csak a zárószöveg (és a kulcsszavak) kerül: a tárgyról magáról a Jarvis csak akkor ír, ha a
# leírásodban (Pro: a megjegyzésben) kifejezetten kéred (pl. „a leírásba írd bele, hogy…”, „írd oda, hogy…”).
_DESCRIBE_RE = re.compile(r"leiras|\b(?:ird|irj|irjad|irjal|irjon)\s+(?:(?:bele|oda|hozza|rola|valamit|be)\b|"
                          r"(?:meg|azt),?\s+hogy\b)|\b(?:emlitsd|emeld ki|jelezd)\b")
TEXT_LIMITS = {"product": 80, "title": 100, "description": 3000, "category": 200, "brand": 80,
               "size": 40, "gender": 20, "condition": 40, "color": 60, "material": 120, "location": 80,
               "shipping": 300, "search_query": 100, "prompt": 3000, "kind": 20, "source": 300, "bought_from": 80,
               "trade_note": 500}
MULTILINE = {"description", "prompt", "trade_note"}
# Forintösszegek (parse_price) és időpontok a tárgyon.
AMOUNT_FIELDS = ("price", "min_price", "bought_price", "sold_price", "bought_shipping", "bought_extra",
                 "market_price", "target_price", "sell_shipping", "sell_fee", "price_estimate")
TIME_FIELDS = ("bought_at", "sold_at")
# Az Elemzés fülön (és a telefonos API-ban) átírható vételi és eladási adatok (a vételár és az eladási ár mellett).
LEDGER_FIELDS = ("bought_shipping", "bought_extra", "bought_from", "bought_at", "in_transit", "market_price",
                 "target_price", "sell_shipping", "sell_fee", "trade_note", "sold_at")
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
    inbox: str = ""        # a vevői üzenetek (lásd messages.py); üres = itt nem olvassuk őket


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
        max_photos=20, inbox="https://www.vinted.hu/inbox"),
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
    # Kinek szól: ha nem kérdezzük meg, a kategóriafa első szintjén (Women, Men) a modell találgatna.
    Need("gender", "kinek szól (női, férfi, gyerek)", ("vinted",), clothing_only=True),
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
    # fotónként a felvételkor mért élesség, fényesség, méret és hash (photos.py); a jelzés ebből készül
    photo_checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    targets: list[str] = field(default_factory=list)     # hova töltse fel
    prompt: str = ""                  # a leírásod, és ha visszakérdezett, a válaszaid
    research_wanted: bool = False     # nézzen utána, mennyiért adják máshol (csak ajánlás)
    # Pro feltöltés: elég a fotó; az adatokat (márka, méret, állapot…) és az árat Jarvis a fotókról olvassa ki, és
    # feltöltés előtt mindet megmutatja (Nézd át), átírhatóan.
    pro: bool = False
    pro_sig: str = ""                 # melyik fotókból olvasta ki (ha új fotót adsz hozzá, újra megnézi)
    price_estimate: int | None = None   # a modell árbecslése a fotókról (ha nincs elég hasonló hirdetés)
    reviewed: bool = False            # Pro: átnézted az adatokat, mehet a feltöltés
    product: str = ""                 # mi ez, röviden
    kind: str = ""                    # ruha, cipő, kiegészítő, elektronika, egyéb
    title: str = ""
    description: str = ""
    category: str = ""
    brand: str = ""
    size: str = ""
    condition: str = ""
    color: str = ""
    gender: str = ""                  # női, férfi, gyerek, uniszex (ruhánál; a kategóriaút első szintje)
    material: str = ""
    price: int | None = None          # forint
    min_price: int | None = None      # alkunál ennél kevesebbet nem fogad el (üres: az ár; lásd messages.py)
    location: str = ""                # ha eltér az Alapadatoktól
    shipping: str = ""
    search_query: str = ""
    edited: list[str] = field(default_factory=list)      # kézzel átírt mezők: Jarvis nem írja felül
    extracted_sig: str = ""           # melyik leírásból olvasta ki az adatokat (ugyanabból nem olvassa ki újra)
    research: dict[str, Any] = field(default_factory=dict)
    platforms: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending: dict[str, Any] = field(default_factory=dict)  # visszakérdezés vagy árdöntés
    sold: bool = False
    # Haszonkövetés (deals.profit_report): mennyiért vetted, honnan, és mennyiért adtad el (az Eladva jelölésekor
    # az akkori ár, utána átírható).
    bought_price: int | None = None
    bought_at: float = 0.0
    source: str = ""                  # a hirdetés, ahol vetted (Automata kereskedés, Megvettem)
    sold_price: int | None = None
    sold_at: float = 0.0
    # Vételi napló (Elemzés fül): a vétel többi költsége, mennyit ér, mennyiért adnád tovább, és az eladás költségei.
    bought_shipping: int | None = None    # vételkor a posta (csomagautomata, futár)
    bought_extra: int | None = None       # egyéb költség (javítás, tisztítás, alkatrész, benzin)
    bought_from: str = ""             # honnan vetted (pl. „Hardverapró”, „bolhapiac”)
    in_transit: bool = False          # megvetted, de még nem érkezett meg (a Megvettem után, amíg nem jelölöd)
    market_price: int | None = None       # a szokásos ára a vételkor (a találatból)
    target_price: int | None = None       # mennyiért adnád tovább (a várható haszonhoz)
    sell_shipping: int | None = None      # eladáskor a posta, ha te fizeted
    sell_fee: int | None = None           # eladási díj (jutalék, kiemelés)
    trade_note: str = ""              # megjegyzés (csomagszám, az eladó, egy hiba)
    reminded: float = 0.0             # mikor szólt utoljára, hogy régóta fent van és nem kelt el
    # A második képsor: az automatikus frissítés (refresh.py) felrakásonként váltogatja a két képsort, így az újra
    # feltöltött hirdetés újnak is látszik (a fotói a tárgy mappájában, az alapéi mellett).
    photos_alt: list[str] = field(default_factory=list)
    # Az automatikus frissítés (refresh.py): on, days (hány naponta), hour (hány óra körül), next (mikortól esedékes),
    # last (mikor sikerült utoljára), set (melyik képsorral van fent: 0 az alap, 1 a második), count, since (mióta van
    # fent először: a frissítés nem nullázza), note (az utolsó eredmény), fails (egymás utáni sikertelen), repost
    # (ahonnan levette, de az újrafeltöltés még hátravan)
    refresh: dict[str, Any] = field(default_factory=dict)
    busy: str = ""                    # épp min dolgozik a Jarvis (nem mentjük)

    def __post_init__(self) -> None:
        # Fotó → [(másik tárgy, annak a fotója)], ahol ugyanez a fotó megvan. Az ItemStore tartja karban; nem
        # mező, így nem kerül az item.json-ba, és az asdict sem járja be (a két tárgy egymásra mutat).
        self.twins: dict[str, list[tuple[Item, str]]] = {}

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
        if self.pending.get("kind") == "review":
            return "átnézésre vár"
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
        data.update(self.photo_report())
        # az automatikus frissítés a felületnek: mikor legközelebb (szövegesen), és melyik képsorral van fent
        state = self.refresh
        data["refresh_next"] = (refreshing.when_text(float(state["next"]), time.time())
                                if state.get("on") and state.get("next") else "")
        data["refresh_last"] = (time.strftime("%Y.%m.%d. %H:%M", time.localtime(float(state["last"])))
                                if state.get("last") else "")
        return data

    def live_photos(self) -> list[str]:
        """Amelyik képsorral most kerül fel (az automatikus frissítés váltogatja): a második, ha az van soron és van."""
        return self.photos_alt if self.refresh.get("set") == 1 and self.photos_alt else self.photos

    def photo_report(self) -> dict[str, Any]:
        """A fotók jelzései (homályos, sötét, majdnem ugyanaz, egy másik tárgy fotója), a javasolt borító, ha az
        elsővel gond van, és egy rövid összefoglaló (photos.report)."""
        elsewhere = {name: [other.name for other, _ in twins] for name, twins in self.twins.items() if twins}
        return photocheck.report(self.photos, self.photo_checks, elsewhere)

    def online_since(self) -> float | None:
        """Mióta van fent (a legkorábban felkerült oldalon, ahol most is fent van); None, ha sehol. Az automatikus
        frissítés (levétel és újrafeltöltés) ezt nem nullázza: a tárgy azóta vár vevőre (refresh.since)."""
        posted = [float(e.get("posted_at") or e.get("at") or 0) for e in self.platforms.values()
                  if e.get("status") == POSTED]
        posted = [at for at in posted if at > 0]
        if not posted:
            return None
        since = float(self.refresh.get("since") or 0) if isinstance(self.refresh, dict) else 0.0
        return min(min(posted), since) if since > 0 else min(posted)

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
        values["photos_alt"] = [p for p in values.get("photos_alt") or [] if isinstance(p, str) and PHOTO_RE.match(p)
                                and p not in values["photos"]]
        values["refresh"] = values.get("refresh") if isinstance(values.get("refresh"), dict) else {}
        checks = values.get("photo_checks")    # a régebbi tárgyaknál nincs: a háttérben elkészül
        names = set(values["photos"]) | set(values["photos_alt"])
        values["photo_checks"] = ({name: check for name, check in checks.items()
                                   if name in names and isinstance(check, dict)}
                                  if isinstance(checks, dict) else {})
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
        self._check_failed: set[tuple[str, str]] = set()   # (tárgy, fotó): nem sikerült megmérni, nem próbálja újra
        self._backfilling = False
        self._load()
        self._relink()
        self._start_backfill()

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
            item.photos_alt = [p for p in item.photos_alt if (folder / p).is_file()]
            item.photo_checks = {k: v for k, v in item.photo_checks.items() if k in item.photos or k in item.photos_alt}
            self._items[item.id] = item
            self._refresh_footer(item)

    def _refresh_footer(self, item: Item) -> None:
        """Betöltéskor: a még fel nem töltött tárgy leírásában a korábbi zárószöveg helyén a mostani (refresh_footer;
        a kézzel átírt leírásban is csak a zárószöveget cseréli). A fent lévő hirdetésé marad, mint a többi oldalon
        (FROZEN_WHEN_POSTED)."""
        if any(e.get("status") == POSTED for e in item.platforms.values()):
            return
        fresh = refresh_footer(item.description)
        if fresh != item.description:
            item.description = fresh
            try:
                self._save(item)
            except OSError:
                pass                       # a memóriában már a mostani; a következő mentéssel kiíródik

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
        self._announce(item)
        return item

    def _announce(self, item: Item) -> None:
        """Szól a felületnek (az `updated` nem változik: pl. a fotójelzés miatt ne ez legyen a legutóbbi kérdés)."""
        if self.on_change is not None:
            try:
                self.on_change(item.public())
            except Exception:  # noqa: BLE001 - a felület hibája ne rontsa el a mentést
                pass

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
        """Beírja a mezőket a tárgyba; visszaadja, melyik változott. Előbb mindet ellenőrzi (_clean): ha egy is hibás
        (pl. egy új cím mellett egy hibás ár), ItemError, és a tárgyból semmi nem változik (egy későbbi mentés se írhassa
        ki a félig beírt adatot)."""
        changed = []
        for key, value in self._clean(values).items():
            if getattr(item, key) != value:
                setattr(item, key, value)
                changed.append(key)
        if item.bought_price is not None and not item.bought_at:
            item.bought_at = time.time()      # a vételár első megadásakor: ma vetted (az Elemzés fülön átírható)
        return changed

    def _clean(self, values: dict[str, Any]) -> dict[str, Any]:
        """A mezők ellenőrizve, egységes alakban (a tárgyhoz nem nyúl); hibás adatnál ItemError."""
        clean: dict[str, Any] = {}
        for key, value in values.items():
            if key in AMOUNT_FIELDS:
                value = parse_price(value)
            elif key in TIME_FIELDS:
                try:
                    value = max(0.0, float(value or 0))
                except (TypeError, ValueError):
                    raise ItemError("Az időpont nem érvényes.") from None
            elif key in ("research_wanted", "in_transit", "pro", "reviewed"):
                value = bool(value)
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
            elif key in ("extracted_sig", "pro_sig"):
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
            clean[key] = value
        return clean

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

    def update_if(self, item_id: str, still: Callable[[Item], bool], **values: Any) -> Item | None:
        """Mint az update (Jarvis írja), de csak ha a zár alatt `still(tárgy)` még igaz (pl. a modellhívás közben nem
        változott a leírás vagy a fotók); különben semmit nem ír, és None."""
        with self._lock:
            if not still(self.get(item_id)):
                return None
            return self.update(item_id, **values)

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
                if sold and not item.sold:          # most kelt el: az akkori ár és idő a haszonkövetéshez
                    item.sold_at = time.time()
                    if item.sold_price is None:
                        item.sold_price = item.price
                elif not sold:
                    item.sold_at = 0.0
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

    # --- automatikus frissítés (refresh.py) ----------------------------------------------------------

    def set_refresh(self, item_id: str, on: Any = None, days: Any = None, hour: Any = None,
                    now: float | None = None) -> Item:
        """Az automatikus frissítés beállítása: be/ki, hány naponta, hány óra körül. Bekapcsoláskor, és ha a napok
        vagy az óra változik, kiszámolja, mikor legközelebb (refresh.next_time): az utolsó frissítéstől (vagy a
        felkerüléstől) számolva. Ha egy újrafeltöltés még hátravan, az nem tolódik."""
        now = time.time() if now is None else now
        with self._lock:
            item = self.get(item_id)
            state = dict(item.refresh)
            try:
                if days not in (None, ""):
                    state["days"] = refreshing.clamp_days(days)
                if hour not in (None, ""):
                    state["hour"] = refreshing.clamp_hour(hour)
            except ValueError as exc:
                raise ItemError(str(exc)) from None
            state.setdefault("days", refreshing.DEFAULT_DAYS)
            state.setdefault("hour", refreshing.DEFAULT_HOUR)
            if on is not None:
                state["on"] = bool(on)
                if state["on"]:
                    state["fails"] = 0
            if state.get("on") and (on or days not in (None, "") or hour not in (None, "") or not state.get("next")):
                base = float(state.get("last") or item.online_since() or now)
                state["next"] = refreshing.next_time(base, state["days"], state["hour"], now)
                if state.get("repost"):
                    state["next"] = min(state["next"], now + refreshing.REPOST_RETRY)
            item.refresh = state
            return self._changed(item)

    def refresh_due(self, now: float | None = None) -> list[Item]:
        """A most esedékes frissítések (a legrégebben esedékes elöl): be van kapcsolva, nem kelt el, és fent van egy
        frissíthető oldalon, vagy az újrafeltöltése hátravan."""
        now = time.time() if now is None else now
        with self._lock:
            out = [item for item in self._items.values()
                   if item.refresh.get("on") and not item.sold and float(item.refresh.get("next") or 0) <= now
                   and (item.refresh.get("repost") or any(
                       refreshing.method(key) and entry.get("status") == POSTED
                       for key, entry in item.platforms.items()))]
        return sorted(out, key=lambda item: float(item.refresh.get("next") or 0))

    def refresh_begin(self, item_id: str, relist: list[str]) -> Item:
        """Egy frissítés eleje (a levétel előtt): mióta van fent (a frissítés ezt nem nullázza), és ahonnan leveszi
        (azokat újra fel kell tenni: repost; ha a levétel nem sikerül, a hívó törli: refresh_reposted)."""
        with self._lock:
            item = self.get(item_id)
            state = dict(item.refresh)
            since = item.online_since()
            if since and not state.get("since"):
                state["since"] = since
            if relist:
                state["repost"] = sorted(set(state.get("repost") or []) | set(relist))
            item.refresh = state
            return self._changed(item)

    def refresh_next_set(self, item_id: str) -> int:
        """A levétel után, az újrafeltöltés előtt: melyik képsorral kerül fel újra (ha van második, a mostanival
        ellentétes, különben az alap). Elmenti (Item.live_photos ezt adja a feltöltésnek), és visszaadja (0 vagy 1)."""
        with self._lock:
            item = self.get(item_id)
            state = dict(item.refresh)
            state["set"] = (1 - int(state.get("set") or 0)) if item.photos_alt else 0
            item.refresh = state
            self._changed(item)
            return state["set"]

    def refresh_reposted(self, item_id: str, platform: str) -> Item:
        """Az újrafeltöltés megvan ezen az oldalon (nincs több teendő vele)."""
        with self._lock:
            item = self.get(item_id)
            state = dict(item.refresh)
            state["repost"] = [key for key in state.get("repost") or [] if key != platform]
            if not state["repost"]:
                state.pop("repost")
            item.refresh = state
            return self._changed(item)

    def refresh_end(self, item_id: str, ok: bool, note: str, now: float | None = None) -> Item:
        """Egy frissítés vége: az eredménye, és a következő időpont. Sikerkor a mostanitól számolva a beállított nap és
        óra szerint; ha az újrafeltöltés maradt el, REPOST_RETRY múlva; más hibánál holnap ugyanekkor. MAX_FAILS
        sikertelen próbálkozás után egymás után kikapcsolja (a hívó szól róla)."""
        now = time.time() if now is None else now
        with self._lock:
            item = self.get(item_id)
            state = dict(item.refresh)
            days = int(state.get("days") or refreshing.DEFAULT_DAYS)
            hour = int(state.get("hour") if state.get("hour") is not None else refreshing.DEFAULT_HOUR)
            state["note"] = note[:300]
            state["tried"] = now
            if ok:
                state.update(last=now, fails=0, count=int(state.get("count") or 0) + 1)
                state["next"] = refreshing.next_time(now, days, hour, now)
            else:
                state["fails"] = int(state.get("fails") or 0) + 1
                state["next"] = now + refreshing.REPOST_RETRY if state.get("repost") else \
                    refreshing.next_time(now, 1, hour, now)
                if state["fails"] >= refreshing.MAX_FAILS:
                    state["on"] = False
            item.refresh = state
            return self._changed(item)

    def adopt_live_price(self, item_id: str, platform: str, price: Any) -> int | None:
        """A hirdetés oldalán látott ár (a háttérellenőrzés vagy a frissítés olvasta el): ha ott kézzel átírtad (más,
        mint amivel feltöltöttük), és itt azóta nem írtad át, átveszi: ez lesz a tárgy ára (és ezen az oldalon a
        feltöltéskori ár; a többi oldal régi árát a drift jelzi). Visszaadja a régi árat, ha átvette; különben None."""
        try:
            price = int(price)
        except (TypeError, ValueError):
            return None
        if not 0 < price < 100_000_000:
            return None
        with self._lock:
            item = self.get(item_id)
            entry = item.platforms.get(platform)
            if entry is None or entry.get("status") != POSTED or entry.get("posted_price") is None:
                return None
            posted = int(entry["posted_price"])
            if posted == price or (item.price is not None and item.price != posted):
                return None        # nem változott, vagy itt is átírtad (az a tiéd, nem írjuk felül)
            old = item.price
            item.price = price
            entry["posted_price"] = price
            item.edited = sorted(set(item.edited) | {"price"})   # te írtad át (ott): Jarvis nem írja felül
            self._changed(item)
            return old if old is not None else posted

    def delete(self, item_id: str) -> None:
        """A tárgy a _torolt mappába kerül (onnan kézzel visszahozható)."""
        with self._lock:
            gone = self.get(item_id)
            trash = self.root / "_torolt"
            trash.mkdir(exist_ok=True)
            shutil.move(str(self.folder(item_id)), str(trash / f"{item_id}-{int(time.time())}"))
            del self._items[item_id]
            for other in self._relink(gone):     # akiknél ennek a fotója is megvolt
                self._announce(other)
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
        if name not in item.photos and name not in item.photos_alt:
            raise ItemError("Nincs ilyen fotó.")
        folder = self.folder(item_id)
        return folder / "thumbs" / name if thumb else folder / name

    def photo_paths(self, item_id: str, alt: bool = False) -> list[Path]:
        """A tárgy fotói (a borítókép az első); alt: a második képsoré (lásd photos_alt)."""
        item = self.get(item_id)
        folder = self.folder(item_id)
        return [folder / name for name in (item.photos_alt if alt else item.photos)]

    def add_photo(self, item_id: str, data: bytes, alt: bool = False) -> Item:
        """Egy fotó a tárgyhoz; alt: a második képsorhoz (az automatikus frissítés váltogatja)."""
        full, thumb = process_photo(data)   # a lassabb rész zár nélkül
        check = self._measure(full)
        with self._lock:
            item = self.get(item_id)
            photos = item.photos_alt if alt else item.photos
            if len(photos) >= self.max_photos:
                raise ItemError(f"Egy képsorban legfeljebb {self.max_photos} fotó lehet." if alt else
                                f"Egy tárgyhoz legfeljebb {self.max_photos} fotó tartozhat.")
            folder = self.folder(item_id)
            (folder / "thumbs").mkdir(parents=True, exist_ok=True)
            numbers = [int(p[:2]) for p in item.photos + item.photos_alt] or [0]
            if max(numbers) >= 99:
                raise ItemError("Ehhez a tárgyhoz már nem fér több fotó; törölj párat.")
            name = f"{max(numbers) + 1:02d}.jpg"
            (folder / name).write_bytes(full)
            (folder / "thumbs" / name).write_bytes(thumb)
            photos.append(name)
            if check is not None:
                item.photo_checks = {**item.photo_checks, name: check}   # új szótár: a felület közben olvashatja
            return self._changed(item) if alt else self._relinked(item)

    def remove_photo(self, item_id: str, name: str) -> Item:
        with self._lock:
            path = self.photo_file(item_id, name)
            item = self.get(item_id)
            if name in item.photos_alt:
                item.photos_alt.remove(name)
            else:
                item.photos.remove(name)
            item.photo_checks = {k: v for k, v in item.photo_checks.items() if k != name}
            for file in (path, path.parent / "thumbs" / name):
                try:
                    file.unlink()
                except OSError:
                    pass
            return self._relinked(item)

    def reorder_photos(self, item_id: str, names: list[str]) -> Item:
        with self._lock:
            item = self.get(item_id)
            wanted = [n for n in names if n in item.photos]
            rest = [n for n in item.photos if n not in wanted]
            item.photos = list(dict.fromkeys(wanted)) + rest
            return self._changed(item)

    # --- fotóellenőrzés (photos.py) --------------------------------------------------------------

    @staticmethod
    def _measure(data: bytes) -> dict[str, Any] | None:
        try:
            return photocheck.measure(data)
        except Exception:  # noqa: BLE001 - csak jelzés: ha nem sikerül, a fotó attól még jó
            return None

    def _relink(self, only: Item | None = None) -> list[Item]:
        """Megkeresi, melyik fotó van meg egy másik tárgynál is (a tárgyon belüli ismétlést a tárgy maga nézi):
        mindegyikét (betöltéskor), vagy csak az `only` tárgy fotóiét, ha csak azok változtak (ha már törölve van,
        eltünteti a rá mutatókat). Visszaadja a tárgyakat, ahol ez megváltozott (róluk szólni kell a felületnek)."""
        entries = [(item, name, key) for item in self._items.values() for name in item.photos
                   if (key := photocheck.key(item.photo_checks.get(name))) is not None]
        if only is None:
            pairs = [(a, a_name, b, b_name) for index, (a, a_name, a_key) in enumerate(entries)
                     for b, b_name, b_key in entries[index + 1:] if a is not b and photocheck.same_key(a_key, b_key)]
        else:
            # a többiek közti párok maradnak (mindegyik kétszer van meg, egyszer elég), az `only` fotói újak
            pairs = [(a, a_name, b, b_name) for a in self._items.values() if a is not only
                     for a_name, twins in a.twins.items() for b, b_name in twins if b is not only and a.id < b.id]
            pairs += [(only, name, b, b_name) for item, name, key in entries if item is only
                      for b, b_name, b_key in entries if b is not only and photocheck.same_key(key, b_key)]
        found: dict[str, dict[str, list[tuple[Item, str]]]] = {item_id: {} for item_id in self._items}
        for a, a_name, b, b_name in pairs:
            found[a.id].setdefault(a_name, []).append((b, b_name))
            found[b.id].setdefault(b_name, []).append((a, a_name))
        changed = []
        for item in self._items.values():     # új szótár, nem helyben: a felület közben olvashatja
            before = {(name, other.id, photo) for name, twins in item.twins.items() for other, photo in twins}
            item.twins = found[item.id]
            if before != {(name, other.id, photo) for name, twins in item.twins.items() for other, photo in twins}:
                changed.append(item)
        return changed

    def _relinked(self, item: Item) -> Item:
        """Egy tárgy fotói változtak: elmenti, és szól róla, meg azokról, akiknél emiatt változott a jelzés."""
        others = [other for other in self._relink(item) if other is not item]
        self._changed(item)
        for other in others:
            self._announce(other)
        return item

    def _unchecked(self, item: Item) -> list[str]:
        return [name for name in item.photos if not photocheck.current(item.photo_checks.get(name))
                and (item.id, name) not in self._check_failed]

    def _check_item(self, item_id: str, relink: bool = True) -> bool:
        """Megméri a tárgy még mérés nélküli fotóit (a mentett JPEG-ből, zár nélkül). Igaz, ha lett új mérés."""
        with self._lock:
            item = self._items.get(item_id)
            todo = self._unchecked(item) if item is not None else []
            folder = self.root / item_id
        if not todo:
            return False
        measured: dict[str, dict[str, Any] | None] = {}
        for name in todo:
            try:
                measured[name] = self._measure((folder / name).read_bytes())
            except OSError:
                measured[name] = None
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return False
            for name, check in measured.items():
                if name not in item.photos:
                    continue
                if check is None:
                    self._check_failed.add((item_id, name))
                else:
                    item.photo_checks = {**item.photo_checks, name: check}   # új szótár: a felület közben olvashatja
            if not any(measured.values()):
                return False
            try:
                self._save(item)
            except OSError:
                pass
            others = [other for other in self._relink(item) if other is not item] if relink else []
            self._announce(item)
            for other in others:
                self._announce(other)
            return True

    def ensure_checks(self, item_id: str) -> Item:
        """A tárgy fotóinak hiányzó mértékei most rögtön (pl. a feltöltés elején egy régebbi tárgynál); a többi
        tárgyé a háttérben készül."""
        self._check_item(item_id)
        return self.get(item_id)

    def _start_backfill(self) -> None:
        """A régebbi (a fotóellenőrzés előtti) tárgyak fotóit a háttérben méri meg, hogy a felület ne várjon."""
        with self._lock:
            if self._backfilling or not any(self._unchecked(item) for item in self._items.values()):
                return
            self._backfilling = True
        threading.Thread(target=self._backfill, name="jarvis-fotoellenorzes", daemon=True).start()

    def _backfill(self) -> None:
        try:
            while True:
                with self._lock:
                    waiting = next((item.id for item in self._items.values() if self._unchecked(item)), None)
                if waiting is None:
                    break
                self._check_item(waiting, relink=False)   # a tárgyak közti összevetés egyszer, a végén
            with self._lock:
                for other in self._relink():
                    self._announce(other)
        except Exception:  # noqa: BLE001 - pl. közben törölték a mappát: a következő indításkor újra próbálja
            pass
        finally:
            with self._lock:
                self._backfilling = False


# --- az adatok a leírásból ----------------------------------------------------------------

EXTRACT_SYSTEM = (
    "Az eladó leírásából kigyűjtöd egy használt (vagy új) holmi hirdetésének adatait, magyarul.\n"
    "Szabályok:\n"
    "- Csak azt írd, ami a leírásból kiderül. Amit nem mondott, hagyd üresen (az ár ilyenkor 0). "
    "Ne találgass, és ne a fotók alapján gondolkodj, azokat nem látod.\n"
    "- product: röviden, mi ez (pl. „fekete póló”, „RTX 3060 Ti videókártya”).\n"
    "- kind: ezek egyike: " + ", ".join(KINDS) + " (a táska, öv, sapka, ékszer: kiegészítő).\n"
    "- title: rövid, természetes cím, ahogy egy ember írná: a márka és hogy mi ez (pl. „Sinsay trikó”, „Zara "
    "farmerdzseki”, „Nike Air Max 90”, „RTX 3060 Ti videókártya”), legfeljebb 40 karakter. A szín, a méret és az "
    "állapot ne legyen benne (azok külön mezőben látszanak), és ne írd nagybetűvel a köznevet.\n"
    "- description: alapból üres. Csak akkor írj bele, ha az eladó kifejezetten kéri, hogy valami a hirdetés "
    "leírásába kerüljön (pl. „a leírásba írd bele, hogy…”, „írd oda, hogy…”): akkor pontosan azt, amit kér, egy-két "
    "rövid mondatban, az ő nevében, egyes szám első személyben (soha nem harmadik személyben, „az eladó szerint”). "
    "Ne ígérj olyat, amit az eladó nem mondott (szállítás, garancia, alku, csere), és ne írj árat.\n"
    "- keywords: 5–8 rövid kulcsszó vesszővel, amire a vevők a Vinteden keresnek: márka, típus, szín, anyag, "
    "stílus, alkalom vagy évszak (pl. „zara, póló, fekete, pamut, basic, nyári”). Csak a leírásból.\n"
    "- gender: ruhánál, cipőnél, kiegészítőnél: női, férfi, gyerek vagy uniszex, csak ha a leírásból kiderül "
    "(pl. „női”, „férfi”, „kisfiú”), különben üres. A márkából vagy a termékből ne találgass.\n"
    "- category: a legvalószínűbb kategóriaút, pl. „Ruhák > Pólók” vagy „Számítástechnika > Videókártyák”. "
    "Az első szint (Nők, Férfiak, Gyerekek) csak akkor, ha a gender kiderül.\n"
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
        "gender": {"type": "string", "enum": ["női", "férfi", "gyerek", "uniszex", ""]},
        "keywords": {"type": "string"},
        "price": {"type": "integer"},
    },
    "required": [*LISTING_FIELDS, "kind", "price"],
}


# Pro feltöltés: a hirdetés adatai a fotókról (a modell képet is ért). Legfeljebb PRO_PHOTOS fotót kap, PRO_SIDE
# pixelesre kicsinyítve (a címke felirata még olvasható, de nem foglal túl sok helyet a modellben).
PRO_PHOTOS = 4
PRO_SIDE = 1024
PRO_FIELDS = ("product", "title", "description", "category", "gender", "brand", "size", "condition", "color",
              "material", "search_query")
PRO_SYSTEM = (
    "Egy eladásra szánt használt (vagy új) holmi fotóit kapod. A fotók alapján gyűjtsd ki a hirdetés adatait, "
    "magyarul.\n"
    "Szabályok:\n"
    "- Csak azt írd, amit a fotókon biztosan látsz, vagy ami egyértelműen kiderül (címke, logó, felirat, a tárgy "
    "formája). Amit nem látsz biztosan, hagyd üresen: arra rákérdezek, mert egy rossz adat a hirdetésben rosszabb, "
    "mint egy kérdés.\n"
    "- product: röviden, mi ez (pl. „farmerdzseki”, „RTX 3060 Ti videókártya”). kind: ezek egyike: "
    + ", ".join(KINDS) + " (a táska, öv, sapka, ékszer: kiegészítő).\n"
    "- brand: a márka a logóról, a címkéről vagy a feliratról; ha nem olvasható, üres.\n"
    "- size: a méret a címkéről (pl. „M”, „38”, „42”, „W32 L34”); ha nem látszik, üres. A kinézetből ne becsüld.\n"
    "- condition: ezek egyike: " + "; ".join(CONDITIONS) + ". A látható kopás, folt, sérülés alapján (rajta a bolti "
    "címke: Új, címkével). Ha a fotókról nem ítélhető meg, üres.\n"
    "- color: a szín magyarul. material: csak ha a címkén olvasható. gender: ruhánál, cipőnél, kiegészítőnél női, "
    "férfi, gyerek vagy uniszex, ha a fazon vagy a címke egyértelművé teszi, különben üres.\n"
    "- title: rövid, természetes cím, ahogy egy ember írná: a márka és hogy mi ez (pl. „Zara farmerdzseki”, „Nike "
    "Air Max 90”), legfeljebb 40 karakter, a szín, a méret és az állapot nélkül.\n"
    "- description: alapból üres. Csak akkor írj bele, ha az eladó megjegyzésében kifejezetten kéri, hogy valami a "
    "hirdetés leírásába kerüljön (pl. „a leírásba írd bele, hogy…”): akkor pontosan azt, amit kér, egy-két rövid "
    "mondatban, az ő nevében, egyes szám első személyben. Ne találj ki használatot („egyszer hordtam”), és ne írj "
    "árat.\n"
    "- keywords: 5–8 rövid kulcsszó vesszővel (márka, típus, szín, anyag, stílus), amire a vevők keresnek.\n"
    "- category: a legvalószínűbb kategóriaút, pl. „Ruhák > Kabátok” vagy „Számítástechnika > Videókártyák”.\n"
    "- search_query: 2–4 szó a hasonló hirdetések kereséséhez (márka és típus, méret nélkül).\n"
    "- price_estimate: reális ár forintban, amennyiért a magyar használtpiacon (Vinted, Jófogás, Hardverapró) "
    "gyorsan elkelne; ha nem tudod megítélni, 0."
)
PRO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **{key: {"type": "string"} for key in PRO_FIELDS},
        "kind": {"type": "string", "enum": KINDS},
        "condition": {"type": "string", "enum": [*CONDITIONS, ""]},
        "gender": {"type": "string", "enum": ["női", "férfi", "gyerek", "uniszex", ""]},
        "keywords": {"type": "string"},
        "price_estimate": {"type": "integer"},
    },
    "required": [*PRO_FIELDS, "kind", "keywords", "price_estimate"],
}


def photo_signature(item: Item) -> str:
    """Melyik fotókból készültek a Pro adatok (a fotók sorrendje és tartalma szerint)."""
    parts = [f"{name}:{(item.photo_checks.get(name) or {}).get('hash', '')}" for name in item.photos]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest() if parts else ""


def vision_jpeg(data: bytes, side: int = PRO_SIDE) -> str:
    """Egy kép a modellnek: kicsinyített JPEG, base64-ben. ItemError, ha nem kép."""
    return base64.b64encode(_jpeg(_open_image(data), side, 85)).decode("ascii")


def vision_images(store: ItemStore, item_id: str, limit: int = PRO_PHOTOS, side: int = PRO_SIDE) -> list[str]:
    """A tárgy első fotói a modellnek (base64 JPEG, kicsinyítve)."""
    out = []
    for path in store.photo_paths(item_id)[:limit]:
        try:
            out.append(vision_jpeg(path.read_bytes(), side))
        except (OSError, ItemError):
            continue
    return out


# Ha az EXTRACT_SYSTEM vagy a séma érdemben változik, emeld: a régi kiolvasások egyszer újra lefutnak.
EXTRACT_VERSION = "4"
# Ha a modellhívás közben megváltozik a leírás (vagy a Pro fotói), a régi válasz nem kerül a tárgyba: ennyiszer olvassa
# ki újra a frisset, utána szól (ItemError).
STALE_RETRIES = 2
# Ha a hirdetés már fent van valahol, ezeket Jarvis magától nem írja át (a többi oldalon is ugyanaz a
# hirdetés legyen, és a begyakorolt lépések kulcsa se változzon); kézzel persze átírhatod.
FROZEN_WHEN_POSTED = ("title", "description", "category", "product", "kind")


def wants_description(prompt: str) -> bool:
    """Kérted-e, hogy a Jarvis a tárgyról is írjon a leírásba (pl. „a leírásba írd bele, hogy a bal ujján folt van”).
    Ha nem, a leírás csak a zárószöveg és a kulcsszavak (compose_description)."""
    return bool(_DESCRIBE_RE.search(fold(prompt or "")))


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
        """Kiolvassa az adatokat. `only`: csak ezeket a mezőket (pl. a visszakérdezett állapotot és várost). Ha a
        modellhívás közben megváltozott a leírás (pl. a telefonról átírtad), a régi válasz nem kerül a tárgyba (és nem is
        számít naprakésznek): az új leírásból olvassa ki újra."""
        for _attempt in range(STALE_RETRIES + 1):
            item = self._extract_once(item_id, list(only) if only else None)
            if item is not None:
                return item
        raise ItemError("A leírás a kiolvasás közben többször is megváltozott; próbáld újra.")

    def _extract_once(self, item_id: str, only: list[str] | None) -> Item | None:
        """Egy kiolvasás; None, ha közben megváltozott a leírás (ilyenkor semmit nem írt a tárgyba)."""
        item = self.store.get(item_id)
        prompt = item.prompt                # ebből olvas ki: ha közben átírják, ez a válasz már nem az övé
        if not prompt.strip():
            raise ItemError("Írd le röviden, mit adsz el (pl. „Zara L-es fekete póló, 500 Ft”).")
        schema, request = EXTRACT_SCHEMA, f"Az eladó leírása:\n{prompt}"
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
        kind = (values.get("kind") if not only or "kind" in only else "") or item.kind
        if not only or "description" in only:
            # a tárgyról csak akkor ír, ha kérted; különben a leírás a zárószöveg és a kulcsszavak
            text = values["description"] if wants_description(prompt) else ""
            values["description"] = compose_description(text, str(data.get("keywords") or ""), kind)
        if values.get("title") and (not only or "title" in only):
            values["title"] = tidy_title(values["title"], values.get("brand") or item.brand, kind,
                                         values.get("size") or item.size, values.get("color") or item.color)
        values = {k: v for k, v in values.items() if v not in ("", None)}   # amit nem mondott, nem törli
        gender = (values.get("gender") if not only or "gender" in only else "") or item.gender
        category = (values.get("category") if not only or "category" in only else "") or item.category
        if gender and category and kind in CLOTHING:
            fixed = gendered_category(category, gender)
            if fixed != item.category:
                values["category"] = fixed
                if only:
                    only = [*only, "category"]
        if only:
            values = {k: v for k, v in values.items() if k in only}
        if any(e.get("status") == POSTED for e in item.platforms.values()):
            values = {k: v for k, v in values.items() if k not in FROZEN_WHEN_POSTED}
        return self.store.update_if(item_id, lambda now: now.prompt == prompt, **values,
                                    extracted_sig=extract_signature(prompt))


    def from_photos(self, item_id: str, images: list[str]) -> tuple[Item, int | None]:
        """Pro feltöltés: a hirdetés adatai a fotókról (egy modellhívás, a képekkel); ha az eladó írt megjegyzést (a
        leírás), azt is figyelembe veszi. Amit nem lát biztosan, üresen marad: arra a feltöltés rákérdez. A kézzel
        átírt mezőket nem írja felül. Visszaadja a tárgyat és a modell árbecslését (None, ha nem tudta). Ha a
        modellhívás közben megváltoztak a fotók vagy a megjegyzés, a régi válasz nem kerül a tárgyba: a mostani fotókkal
        újra megnézi."""
        for attempt in range(STALE_RETRIES + 1):
            if attempt:
                images = vision_images(self.store, item_id)
            done = self._from_photos_once(item_id, images)
            if done is not None:
                return done
        raise ItemError("A fotók vagy a megjegyzés az átnézés közben többször is megváltoztak; próbáld újra.")

    def _from_photos_once(self, item_id: str, images: list[str]) -> tuple[Item, int | None] | None:
        """Egy átnézés; None, ha közben megváltoztak a fotók vagy a megjegyzés (ilyenkor semmit nem írt a tárgyba)."""
        item = self.store.get(item_id)
        if not images:
            raise ItemError("Ehhez a tárgyhoz még nincs fotó.")
        prompt, seen = item.prompt, photo_signature(item)   # ezekből dolgozik: ha közben változnak, ez már nem az övé
        note = f"\nAz eladó megjegyzése: {prompt}" if prompt.strip() else ""
        messages = [{"role": "system", "content": PRO_SYSTEM},
                    {"role": "user", "content": f"Az eladásra szánt tárgy fotói ({len(images)} db).{note}",
                     "images": images}]
        data = self.llm.chat_json(messages, PRO_SCHEMA, temperature=0.1, max_tokens=EXTRACT_MAX_TOKENS)
        values: dict[str, Any] = {key: str(data.get(key) or "").strip() for key in PRO_FIELDS}
        values["kind"] = str(data.get("kind") or "").strip()
        kind = values["kind"] or item.kind
        text = values["description"] if wants_description(prompt) else ""   # a tárgyról csak kérésre
        values["description"] = compose_description(text, str(data.get("keywords") or ""), kind)
        if values["title"]:
            values["title"] = tidy_title(values["title"], values["brand"] or item.brand, kind,
                                         values["size"] or item.size, values["color"] or item.color)
        values = {k: v for k, v in values.items() if v}      # amit nem látott, nem törli
        gender, category = values.get("gender") or item.gender, values.get("category") or item.category
        if gender and category and kind in CLOTHING:
            values["category"] = gendered_category(category, gender)
        if any(e.get("status") == POSTED for e in item.platforms.values()):
            values = {k: v for k, v in values.items() if k not in FROZEN_WHEN_POSTED}
        try:
            estimate = parse_price(data.get("price_estimate")) or None
        except ItemError:
            estimate = None
        saved = self.store.update_if(item_id, lambda now: now.prompt == prompt and photo_signature(now) == seen,
                                     **values, price_estimate=estimate, pro_sig=seen)
        return None if saved is None else (saved, estimate)


def description_footer(kind: str) -> str:
    """A leírás zárószövege: mindig ugyanaz (ruhánál, cipőnél, kiegészítőnél „ruhát”, máshol „holmit”)."""
    return DESCRIPTION_FOOTER.format(what="ruhát" if kind in CLOTHING else "holmit")


def refresh_footer(description: str) -> str:
    """A leírás, a korábbi zárószövegek (OLD_FOOTERS) helyén a mostanival, ugyanazzal a szóval („ruhát” vagy
    „holmit”); a többi részéhez nem nyúl."""
    for old in OLD_FOOTERS:
        for what in ("ruhát", "holmit"):
            description = description.replace(old.format(what=what), DESCRIPTION_FOOTER.format(what=what))
    return description


def compose_description(text: str, keywords: str, kind: str) -> str:
    """A hirdetés leírása: felül, amit kifejezetten kértél (a modell rövid, első személyű szövege), alatta mindig a
    zárószöveg, legalul a kulcsszavak a Vinted keresőjének (#zara #póló …, legfeljebb hat)."""
    text, footer = text.strip(), description_footer(kind)
    parts = [text] if fold(footer) in fold(text) else [text, footer]   # ha szó szerint benne van, nem kétszer
    tags = []
    for word in re.split(r"[,;\n]+", keywords):
        tag = re.sub(r"[^\w-]", "", word.strip().lower().replace(" ", ""))
        if tag and f"#{tag}" not in tags:
            tags.append(f"#{tag}")
    if tags:
        parts.append(" ".join(tags[:6]))
    return "\n\n".join(p for p in parts if p)


# Színek és állapotszavak (ékezet nélkül): a címben nem kellenek, külön mezőben látszanak.
_COLORS = ("fekete", "feher", "szurke", "kek", "vilagoskek", "sotetkek", "piros", "voros", "bordo", "zold", "sotetzold",
           "sarga", "narancs", "narancssarga", "lila", "rozsaszin", "pink", "barna", "bezs", "drapp", "krem", "arany",
           "ezust", "khaki", "menta", "turkiz", "mustar", "tarka", "mintas", "csikos", "kockas", "farmerkek",
           "black", "white", "grey", "gray", "blue", "red", "green", "yellow", "orange", "purple", "brown", "beige")
_CONDITION_WORDS = ("uj", "ujszeru", "hasznalt", "cimkes", "cimkevel", "hibatlan", "alig hordott", "alig hasznalt",
                    "egyszer hordott", "nagyon jo allapotu", "jo allapotu", "szep allapotu")
# Ruhaméret a címben: S, M-es, XL, 2XL, 38-as, EU 40, 42 2/3, W32 (a 16–56 közti szám: a 90-es Air Max nem méret).
_SIZE_RE = re.compile(r"(?<![\w.'’&+-])(?:(?:[2-5]?x{0,3}[sml]|xxs|one size|onesize)(?:-?[ea]s|-?os)?|"
                      r"(?:eu |us |uk )?(?:1[6-9]|[2-5]\d)(?:[.,]5)?(?: ?[0-9]/[0-9])?(?:-?[ea]s|-?os|-?ös)?|[wl]\d{2})"
                      r"(?: ?(?:méret\w*|meret\w*|size))?(?![\w.'’&+-])", re.I)


def _drop_phrases(words: list[str], phrases: tuple[str, ...]) -> list[str]:
    """A szavak, a kifejezések nélkül (ékezettől és kisbetűtől függetlenül; pl. „Újszerű”, „alig hordott”)."""
    folded = [fold(w).strip(".,;:!–()") for w in words]
    out, index = [], 0
    while index < len(words):
        for phrase in phrases:
            parts = phrase.split()
            if folded[index:index + len(parts)] == parts:
                index += len(parts)
                break
        else:
            out.append(words[index])
            index += 1
    return out


def tidy_title(title: str, brand: str = "", kind: str = "", size: str = "", color: str = "") -> str:
    """Természetesebb cím: a márka és hogy mi ez („Sinsay trikó”), szín, méret és állapot nélkül (azok külön mezőben
    látszanak, és a hosszú, mindent felsoroló cím robotikus). Ruhánál a méretet és a színt is kiveszi; elektronikánál a
    típusjel (pl. a memória) marad. Ha a márka nincs benne, elé teszi. Ha nem maradna értelmes cím, az eredetit adja."""
    original = " ".join(str(title or "").split())
    words = original.split(" ")
    clothing = kind in CLOTHING
    colors = set(_COLORS) | ({fold(w) for w in re.split(r"[\s,/]+", color) if w} if color else set())
    kept: list[str] = []
    for word in _drop_phrases(words, _CONDITION_WORDS):
        bare = fold(word).strip(".,;:!-–()")
        if clothing and (bare in colors or re.sub(r"(es|as|os|u)$", "", bare) in colors):
            continue
        kept.append(word)
    text = " ".join(kept)
    if clothing:
        text = _SIZE_RE.sub(" ", text)
        if size.strip():
            text = re.sub(rf"(?i)(?<![\w-]){re.escape(size.strip())}(?:-?[ea]s|-?os)?(?![\w-])", " ", text)
    text = re.sub(r"\s*[,;/|–-]\s*(?=[,;/|–-]|$)", "", " ".join(text.split())).strip(" ,;/|–-")
    if brand.strip() and fold(brand.strip()) not in fold(text):
        text = f"{brand.strip()} {text}".strip()
    if len(text) < 3 or not re.search(r"[^\W\d_]{2,}", text):
        return original
    return text if brand.strip() and text.startswith(brand.strip()) else text[0].upper() + text[1:]


def gendered_category(category: str, gender: str) -> str:
    """A kategóriaút első szintje a gender szerint (Nők, Férfiak, Gyerekek); uniszexnél marad, ahogy van."""
    level = GENDERS.get(gender)
    if not level:
        return category
    parts = [p.strip() for p in category.split(">") if p.strip()]
    if parts and fold(parts[0]) in GENDER_LEVELS:
        parts = parts[1:]
    return " > ".join([level, *parts])


def item_gender(item: Item) -> str:
    """Kinek szól: a megadott, vagy a kategóriaút első szintjéből (a korábbi tárgyaknál)."""
    if item.gender:
        return item.gender
    first = fold(item.category.split(">")[0].strip()) if item.category else ""
    return first if first in GENDER_LEVELS else ""


def missing_needs(item: Item, settings: dict[str, Any]) -> list[tuple[Need, list[str]]]:
    """Ami a kiválasztott oldalakhoz még hiányzik: (igény, melyik oldalak kérik)."""
    out = []
    for need in needs_for(item.targets):
        if need.clothing_only and item.kind not in CLOTHING:
            continue
        have = (item.price if need.field == "price" else item_gender(item) if need.field == "gender"
                else getattr(item, need.field))
        if not have and need.setting:
            have = settings.get(need.setting)
        if not have:
            sites = [PLATFORMS[s].name for s in need.sites if s in item.targets] if need.sites else []
            out.append((need, sites))
    return out


def review_text(item: Item, basis: str = "") -> str:
    """A Pro feltöltés Nézd át üzenete: a hirdetés adatai az árral (és hogy honnan az ár)."""
    sites = ", ".join(PLATFORMS[t].name for t in item.targets if t in PLATFORMS)
    facts = " · ".join(f"{label}: {value}" for label, value in (
        ("Márka", item.brand), ("Méret", item.size), ("Állapot", item.condition), ("Szín", item.color),
        ("Kinek", item.gender)) if value)
    price = (f"Ár: {format_price(item.price)}" + (f" ({basis})" if basis else "") if item.price else
             "Ár: nem tudtam megbecsülni, írd be")
    lines = [f"Nézd át, mielőtt feltöltöm ({sites}):" if sites else "Nézd át, mielőtt feltöltöm:",
             f"Cím: {item.title or item.product or '?'}", price]
    if facts:
        lines.append(facts)
    if item.category:
        lines.append(f"Kategória: {item.category}")
    lines.append("Ha minden stimmel, nyomd meg a Mehet gombot; amit kell, előtte írd át.")
    return "\n".join(lines)


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
# (A „Beszámítás” nem ilyen: a kereskedő eladja a terméket, és a régit beszámítja az árba; a hirdetés ára eladási ár.)
_WANTED_RE = re.compile(r"\b(keresek|keresem|keresunk|veszek|vennek|vennem|megvennem|felvasar\w*)\b")
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


# A hirdetés száma az oldal saját webcímformája szerint: a Vinteden /items/<szám>-<cím>, a Jófogáson <cím>_<szám>.htm, a
# Vaterán <cím>-<szám>.html (a szám a végén). A hirdetés címéből lett szakaszban lévő szám nem azonosító („i5-12400F”,
# „Lego 75192”, „20000 mAh”): ha azt venné, minden ilyen hirdetés egynek látszana (egy ár az árbázisban, és az új
# hirdetés „már látott”). A Hardveraprón nincs szám a webcímben (/apro/<cím>/friss.html, az azonos címűeknél _2, _5 a
# cím végén): ott az útvonal az azonosító.
_LISTING_IDS = (("vinted", re.compile(r"/items/(\d{5,})")),
                ("jofogas", re.compile(r"_(\d{5,})\.html?$")),
                ("vatera", re.compile(r"[-_](\d{5,})\.html?$")))


def listing_key(url: str) -> str:
    """Egy hirdetés azonosítója a webcíméből: a gép és a hirdetés száma (az oldal webcímformája szerint, _LISTING_IDS),
    különben az útvonal (a Hardveraprón mindig az). Így a saját hirdetésed más paraméterekkel vagy más címmel (slug)
    is felismerhető, és két különböző hirdetés nem esik egybe attól, hogy a címükben ugyanaz a szám van."""
    parts = urllib.parse.urlsplit(url or "")
    host = (parts.hostname or "").removeprefix("www.")
    labels = host.split(".")
    path = host + parts.path.rstrip("/").lower()
    for name, pattern in _LISTING_IDS:
        if name in labels:
            found = pattern.search(parts.path)
            if found:
                return f"{host}#{found.group(1)}"
    if "hardverapro" in labels:
        return path
    number = re.search(r"\d{5,}", parts.path)
    return f"{host}#{number.group(0)}" if number else path


def _same_listing(words: set[str], price: int, other: set[str], other_price: int) -> bool:
    """Ugyanaz a hirdetés még egyszer (pl. az eladó több oldalra is feltette): szinte ugyanaz a cím és az ár."""
    if not words or not other or abs(price - other_price) > 0.05 * max(price, other_price, 1):
        return False
    return len(words & other) / len(words | other) >= 0.8


def prefilter(item: Item, rows: list[dict[str, Any]], limit: int = JUDGE_ROWS, dedupe: bool = True
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
        elif dedupe and any(_same_listing(words, price, other, other_price) for other, other_price in seen):
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


_AMOUNT_RE = re.compile(r"\d[\d\s.,]*\s*(?:ft|huf|forint)\b", re.IGNORECASE)


def prompt_query(text: str) -> str:
    """Keresőszó a leírásból, modell nélkül (ha még nincs kiolvasva, és a modell sem segített): az első sor eleje az
    első vesszőig vagy árig, legfeljebb öt szó (pl. „Sony WH-1000XM4 fejhallgató, 71000 Ft” → „Sony WH-1000XM4
    fejhallgató”)."""
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    first = re.split(r"[,;|(]", _AMOUNT_RE.split(first)[0])[0]
    return " ".join(first.split()[:5])


class NeedsBrowser(Exception):
    """Ehhez a lépéshez a böngésző kell: az eladás az ágens-szálon folytatódik (az előkészítő szál nem nyúlhat hozzá)."""


class PriceResearcher:
    """Megnézi a hirdetési oldalakon, mennyiért kínálnak hasonlót. A Hardverapró és a Jófogás találati oldalát
    böngésző nélkül tölti le (fetcher, ha van), a többit a böngészővel."""

    def __init__(self, llm: Any, store: ItemStore, browser: Callable[[], Any], sites: list[Platform],
                 fetcher: Any = None):
        self.llm = llm
        self.store = store
        self.browser = browser
        self.sites = [site for site in sites if site.search_url]
        self.fetcher = fetcher

    def _sites(self, only: list[str] | None) -> list[Platform]:
        return [s for s in self.sites if not only or s.key in only] or self.sites

    def _fast(self, site: Platform) -> bool:
        return self.fetcher is not None and self.fetcher.handles(site.key) and self.fetcher.usable(site.key)

    def needs_browser(self, only: list[str] | None = None) -> bool:
        """Kell-e a böngésző az árkutatáshoz (van olyan oldal, ami böngésző nélkül most nem olvasható)."""
        return any(not self._fast(site) for site in self._sites(only))

    def _listings(self, site: Platform, url: str, browser: bool) -> list[dict[str, Any]]:
        if self._fast(site):
            try:
                return self.fetcher.listings(url, site.key)
            except FeedError:
                if not browser:
                    raise NeedsBrowser() from None
        elif not browser:
            raise NeedsBrowser()
        return self.browser().extract_listings(url)

    def query_for(self, item: Item) -> str:
        if item.search_query:
            return item.search_query
        words = " ".join(p for p in (item.brand, item.product or item.title) if p).split()
        return " ".join(words[:5]) or prompt_query(item.prompt)

    def research(self, item_id: str, progress: Callable[[str], None] | None = None,
                 only: list[str] | None = None, browser: bool = True) -> Item:
        """Az árkutatás a kiválasztott oldalakon. browser=False: csak böngésző nélkül (az eladás-előkészítő szálon);
        ha valamelyik oldalhoz böngésző kellene, NeedsBrowser."""
        item = self.store.get(item_id)
        query = self.query_for(item)
        if not query:
            raise ItemError("Az árkutatáshoz tudnom kell, mi ez (írd le röviden).")
        sites = self._sites(only)
        if not sites:
            raise ItemError("Nincs beállítva oldal az árkutatáshoz ([work] research_sites).")
        if not browser and self.needs_browser(only):
            raise NeedsBrowser()
        results: list[dict[str, Any]] = []
        errors: list[str] = []
        for site in sites:
            if progress:
                progress(f"{site.name}: „{query}”")
            # a legújabb hirdetések elöl: a mostani árak (nem a régóta fent lévőké vagy a kiemelteké)
            url = newest_first(site.search_url.format(q=urllib.parse.quote(query)), site.key)
            try:
                found = self._listings(site, url, browser)
            except NeedsBrowser:
                raise
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
