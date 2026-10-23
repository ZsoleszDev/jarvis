"""Automata kereskedés: alulárazott hirdetések keresése a Vinteden, a Jófogáson, a Vaterán és a Hardverapróban.

Megadod, mit keresel (pl. „RTX 3060 Ti”, „PS5 kontroller”), és a Jarvis időnként, emberi tempóban megnézi az oldalak
keresőjét (csak olvas, semmire nem kattint, nem vesz és nem ír senkinek).

Szokásos ár: egy új keresésnél először felméri, mennyiért adják most a terméket. A találatokból modell nélkül kiszűri,
ami biztosan nem az (a keresés valamelyik szava hiányzik, kiegészítő, kereső-, csere- vagy csomaghirdetés, alkatrész,
a minimum ár alatti, vagy egy kizáró szó van benne), és a maradékból egyetlen modellhívás választja ki a valódi, működő,
egy darabos termékeket. Az árat nem a modell találja ki: a kiválasztott hirdetések középértéke lesz a javaslat. Erről
szól, és csak akkor keres vele, ha jóváhagytad (vagy kézzel megadod a szokásos árat; akkor felmérés sem kell). Ha a
felmérés közben talál jóval olcsóbbat, arról is szól.

Utána modell nélkül keres, ehhez a rögzített árhoz mérve: jelölt az a még nem látott (vagy azóta leárazott) hirdetés,
ami a megadott %-kal olcsóbb, vagy legalább a megadott forinttal hoz hasznot (a hirdetési ár 90%-áért eladva, a postával
együtt, SALE_RATIO, SHIPPING). A jelölt hirdetés oldalát megnyitja (csak olvasva), és modell nélkül megnézi: elkelt-e
már, van-e benne hibára utaló szó. A gyanús hirdetésről egyáltalán nem szól (csak a naplóba írja, miért hagyta ki):
gyanúsan olcsó (SUSPECT_RATIO; egy gyakran hamisított márkánál FAKE_RATIO), átverésre utaló jel (előre utalás,
külföldről küldi, az oldalon kívüli elérhetőség), az eladónak még nincs értékelése, vagy a modell szerint átverésnek
vagy hamisítványnak látszik. Csak az átment jelöltekre kérdez rá röviden a modellel. Ami átmegy, arról szól (kind:
deal), a hirdetés linkjével és a várható haszonnal. Ha a hirdetésekben napokig jóval más az ár, mint a jóváhagyott,
szól, hogy mérje fel újra.

Felfedező (figyelt oldalak): keresőszó nélkül, egy kategória- vagy találati oldal friss hirdetései (bármelyik oldalon; a
Vaterán a licitek is). Modell nélkül az árbázishoz méri őket (pricebook.py: minden látott hirdetés ára, termékenként, a
címből kiolvasott termékkulcs alatt); amit nem ismer fel, azt egy modellhívás nevezi meg (és ezzel az árbázis is tanul),
és ami a modell szerint megérhet, annak a többi oldalon nézi meg az árát. A régebben látott hirdetések közül néhányat
később újra megnéz (elkelt-e): ebből lesz az eladási sebesség.

Gyorsaság: a találatokat a legújabb elöl nézi. Keresésenként megadható, milyen sűrűn nézze (30 perc – 6 óra); a Vintedet
legfeljebb kétóránként (robot-ellenőrzés). Az egész egy kapcsolóval be- és kikapcsolható (a Munka menü Automata
kereskedés fülén); alapból ki van kapcsolva.
"""
from __future__ import annotations

import json
import math
import os
import queue
import random
import re
import secrets
import statistics
import threading
import time
import urllib.parse
from collections import Counter, deque
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any, Callable

from .browser import DECLINED, LISTING_PAGE_JS, BrowserError, is_risky
from .feeds import FeedError, fetch_image
from .listings import BLOCKED, GONE, SOLD, classify
from .llm import background_calls
from .journal import Journal, day_of
from .pricebook import SOLD_MIN, PriceBook, fake_prone, pace, speed_text, usable
from .replay import norm_text
from .selling import (_PARTS_RE, ON_SITE, Item, ItemError, Platform, _shared, _tokens, format_price, listing_key,
                      parse_price, prefilter, price_stats, round_price, vision_jpeg)
from .textutil import fold
from .vinted import BUTTON_KINDS, consent_button, elem_key, logged_out, new_elements
from .web import UNTRUSTED_NOTE

# A Vaterán a keresés csak a fix áras („azonnal megvásárolható”) hirdetéseket nézi; a licitekre a licitfigyelő van.
DEAL_SITES = ("vinted", "jofogas", "vatera", "hardverapro")
RESALE_SITES = ("vinted", "jofogas", "hardverapro")   # egy licit viszonteladási árához ezeken keres
MAX_SEARCHES = 20
MAX_PER_SITE = 30          # egy keresés egy oldalán ennyi találatot néz meg (az első oldal)
MIN_PRICES = 5             # az ármozgás figyeléséhez legalább ennyi hasonló ár kell egy körben
DEFAULT_DISCOUNT = 30      # ennyi %-kal a szokásos ár (medián) alatt számít olcsónak
FLOOR = 0.15               # a medián 15%-a alatti ár többnyire nem valódi ár (1 Ft, „ár megegyezés szerint”)
MAX_CHECKS = 5             # egy körben legfeljebb ennyi jelöltet néz meg a modellel (a többi a következő körre marad)
MAX_DEALS = 60             # ennyi találatot őriz meg a listában
MAX_SEEN = 5000
EVERY_CHOICES = (30, 60, 180, 360)   # percben: milyen sűrűn nézi meg egy keresés találatait
DEFAULT_EVERY = 180
SITE_MIN_MINUTES = {"vinted": 120}   # a Vintedet legfeljebb kétóránként (ott a robot-ellenőrzés a kockázat)
# A legújabb hirdetések elöl: a Vinteden külön kérni kell, a Jófogáson és a Hardverapróban ez az alap.
NEWEST = {"vinted": "&order=newest_first"}
MAX_PAGES = 5              # egy körben legfeljebb ennyi jelölt hirdetés oldalát nyitja meg (a többi a következőre)
# A szokásos ár felmérése: legfeljebb ennyi találatot kap a modell, és legalább ennyi hasonlóból mond árat. Ha kevés
# a hasonló, egy nap múlva próbálja újra (addig kézzel megadhatod).
CALIBRATE_ROWS = 25
CALIBRATE_MIN = 4
CALIBRATE_RETRY = 86400
# Várható haszon: a hirdetési (kínálati) ár többnyire többet mond, mint amennyiért ténylegesen elkel (kb. 10%-kal), és
# a postázás (csomagautomata, 1–2 ezer Ft) is a tiéd.
SALE_RATIO = 0.9
SHIPPING = 1500
DROP = 0.05                # egy már látott hirdetés legalább ennyivel olcsóbb lett: leárazás, újra jelölt lehet
DRIFT = 0.2                # ha a hirdetésekben ennyivel más az ár, mint a jóváhagyott…
DRIFT_DAYS = 3             # …ennyi napig, szól, hogy mérje fel újra
MAX_EXCLUDE = 12
DEALS_ACTION = {"action": "deals", "label": "Megnézem"}   # az értesítés gombja: az Automata kereskedés fül
# Az eladónak szóló üzenet: egy megérő találat ablakában a gomb kimásolja, és megnyitja a hirdetést (a fülön átírható).
DEFAULT_MESSAGE = "Szia!\nMegvan még a termék?\nKöszi előre is\nÜdv, Zsolt"


def deal_action(deal_id: str) -> dict[str, str]:
    """Egy megérő találat értesítésének gombja: az alkalmazás a találat ablakát nyitja meg (ár, mennyiért mennek, mennyit
    kereshetsz vele, és az üzenet az eladónak)."""
    return {"action": "deal", "deal": deal_id, "label": "Megnézem"}
# Vinted-ajánlat: ha a kért ár kicsit több annál, ami még megéri, ajánlatot javasol; legfeljebb ennyivel a kért ár
# alatt (ennél nagyobb engedményt ritkán fogadnak el).
OFFER_CUT = 0.2
# Vatera-licitfigyelő: alapból óránként nézi, a zárás előtt legfeljebb ennyi órával szól (hogy legyen időd licitálni),
# legalább ennyi haszonnal; egy oldalról legfeljebb EXPLORE_ROWS ismeretlen licitet kap a modell, és ezekből legfeljebb
# EXPLORE_PICKS-nek nézi meg az árát máshol (RESALE_SITES).
MAX_WATCHES = 10
WATCH_EVERY = 60
WATCH_HOURS = 6
WATCH_PROFIT = 5000
EXPLORE_ROWS = 20
EXPLORE_PICKS = 3
# Felfedező (figyelt oldalak, keresőszó nélkül): az árbázishoz (vagy a máshol talált árhoz) mérve legalább ennyivel
# olcsóbb, és legalább a megadott haszon jön ki belőle.
EXPLORE_CUT = 0.15
# Gyanúsan olcsó: a szokásos ár SUSPECT_RATIO-ja alatt (a gyakran hamisított márkáknál FAKE_RATIO-ja alatt) többnyire
# átverés, hamisítvány vagy hibás; ezekről nem szól. (A FLOOR alatti ár nem is valódi ár: azt meg sem nézi.)
SUSPECT_RATIO = 0.4
FAKE_RATIO = 0.6
# Biztos ár: egy felfedezett (nem a kereséseddel felmért) termék szokásos ára csak akkor számít, ha legalább SURE_MIN
# hasonló áron alapul, legalább két oldalról (ha csak egy oldalon láttuk, ott legalább SURE_ALONE áron). Ha ennyi nincs,
# előbb a többi oldalon is megnézi, mennyiért adják (oldalanként egy keresés, böngésző nélkül, ahol lehet).
SURE_MIN = 5
SURE_ALONE = 10
# Nyugodt szívvel megvehető-e (assess): a haszon a befektetett pénzhez képest (SAFE_ROI fölött biztos, RISKY_ROI alatt
# kockázatos: egy kis árcsökkenés elviszi), mennyire szórnak a hasonlók árai (WIDE_SPREAD: az alsó és a felső negyed
# távolsága a középérték arányában), és milyen gyorsan kel az ilyen (a lassan kelőnél csak SLOW_OK_ROI haszon fölött
# nem kockázatos: sokáig áll benne a pénzed). A kockázatosról alapból nem szól (safe_only).
# A végső modellkérdés a hirdetés első CHECK_PHOTOS fotóját is megkapja (CHECK_SIDE pixelesre kicsinyítve): a
# sérülés, a letöltött (webshop-) kép és a más termék a fotón látszik, a szövegben nem.
CHECK_PHOTOS = 2
CHECK_SIDE = 768
SAFE_ROI = 0.2
RISKY_ROI = 0.1
WIDE_SPREAD = 0.45
SLOW_OK_ROI = 0.35
SPEED_CHECKS = 2           # egy körben ennyi régebben látott hirdetést néz meg újra (elkelt-e; a Vintedet nem)
DEFAULT_DAY = (7, 23)      # a gép általában ekkor megy (óra): a licitekről még az esti leállás előtt szól
# Böngészés napközben: a gép üzemidejében (DEFAULT_DAY) munkamenetekben dolgozik, ahogy egy ember: egy oldalon kb.
# BROWSE_WORK percig nézelődik (két oldalbetöltés között STEP_PAUSE másodperc), aztán egy rövid SWITCH_PAUSE után a
# következő oldalra vált (BROWSE_ORDER). Csak az az oldal pihen, amelyiken épp dolgozott: legalább BROWSE_REST percig
# (±30%), a Vinted SITE_GAP percig; a Jarvis maga csak akkor áll meg, ha minden bejelölt oldal pihen. Egy munkamenetben
# legfeljebb BROWSE_PAGES oldalt tölt be. Ha egy oldal robot-ellenőrzést kér, aznap már nem nézi.
BROWSE_WORK = 20
BROWSE_REST = 10
DEFAULT_STEP = (25, 75)
STEP_PAUSE = {"vinted": (45, 120)}
SWITCH_PAUSE = (20, 60)
BROWSE_PAGES = {"vinted": 10}
BROWSE_MAX_PAGES = 30
SITE_GAP = {"vinted": 120}
OPEN_CHANCE = 0.35         # egy találati oldal után ekkora eséllyel megnyit egy hirdetést (a Vinted kezdőoldalán mindig)
# A modellre váró ismeretlenek: EXPLORE_ROWS-onként egy hívás; a munkamenet végén csak legalább MIN_FLUSH (vagy egy
# licit) miatt tölti be a modellt, a maradék a következő munkamenetre vár (legfeljebb MAX_PENDING).
MIN_FLUSH = 8
MAX_PENDING = 60
BROWSE_ORDER = ("hardverapro", "jofogas", "vinted", "vatera")
# Két böngészési sor párhuzamosan (ha a Jarvis elindítja a gyors sor szálát, start_fast_lane): a böngészőé (Vinted,
# Vatera) az ágens-szálon, és a gyors (Hardverapró, Jófogás böngésző nélkül, feeds.py) a sajátján. A munkamenet és a
# modellre váró ismeretlenek soronként külön vannak.
LANE_BROWSER, LANE_FAST = "browser", "fast"
SESSION_KEYS = {LANE_BROWSER: "session", LANE_FAST: "fast_session"}
FAST_TICK = 3.0            # a gyors sor szála ilyen sűrűn nézi meg, van-e dolga
BOOK_SAVE = 600            # a gyors sor az árbázist legfeljebb ennyi másodpercenként írja ki (nagy fájl)
# Indulóoldalak: (név, keresőszó vagy teljes cím; üres = a kezdőoldal). A Hardverapró kategóriáit a címükkel.
_HA = "https://hardverapro.hu/aprok/hardver/"
BROWSE_LANES: dict[str, list[tuple[str, str]]] = {
    "vinted": [("Kezdőoldal", ""), ("Nike", "nike"), ("Adidas", "adidas"), ("The North Face", "the north face"),
               ("Ralph Lauren", "ralph lauren"), ("Carhartt", "carhartt"), ("Lego", "lego"), ("AirPods", "airpods")],
    "hardverapro": [("Videókártyák", _HA + "videokartya/index.html"), ("Memóriák", _HA + "memoria/index.html"),
                    ("SSD-k", _HA + "merevlemez_ssd/ssd/index.html"), ("Processzorok", _HA + "processzor/index.html"),
                    ("Alaplapok", _HA + "alaplap/index.html")],
    "jofogas": [("iPhone", "iphone"), ("PlayStation", "playstation"), ("Nintendo Switch", "nintendo switch"),
                ("Lego", "lego"), ("AirPods", "airpods"), ("Dyson", "dyson"), ("Makita", "makita")],
    "vatera": [("iPhone", "iphone"), ("Lego", "lego"), ("PlayStation", "playstation"), ("Nintendo", "nintendo"),
               ("Fényképezőgép", "fényképezőgép")],
}
# Öntanulás: a gyorsan kelő (vagy már többször jó találatot hozó) termékekből saját indulóoldal lesz (legfeljebb
# MAX_LEARNED); ami LEARN_IDLE betöltés után sem hozott semmit, kimarad. Amit MUTE_AFTER-szer elrejtettél (és egyszer
# sem vettél meg), arról MUTE_DAYS napig nem szól.
MAX_LEARNED = 6
LEARN_IDLE = 15
MUTE_AFTER = 2
MUTE_DAYS = 30
_OFFER_OPEN_RE = re.compile(r"^(ajanlattetel|ajanlat tetele|ajanlatot teszek|ajanlat|make an offer|make offer|offer)$")
_OFFER_SEND_RE = re.compile(r"^(ajanlat kuldese|ajanlat elkuldese|ajanlatot kuld\w*|ajanlatot teszek|ajanlattetel|"
                            r"ajanlat tetele|kuldes|elkuldes|send offer|send|make offer|offer|ajanlok)$")
_OFFER_BOX_RE = re.compile(r"(ar|ajanlat|price|offer|osszeg|amount|ft)")
_PRICE_KINDS = {"input:number", "input:text", "input:tel", "textbox"}
PAGE_TEXT = 2000           # a hirdetés oldalának eleje (a cím, a leírás, az eladó); a lejjebb lévő ajánlók nem
# Átverésre utaló jel (ékezet nélkül): előre fizetés, külföldről küldés, pénzküldő szolgáltatás, ajándékkártya.
_SCAM_RE = re.compile(r"\b(csak (elore )?utal\w*|elore (kell )?utal\w*|utalas elore|elore fizet\w*|western union|"
                      r"moneygram|paypal (baratnak|friends)|kulfoldon (vagyok|elek|dolgozom)|kulfoldrol (kuldom|postazom)|"
                      r"ajandekkartya|gift ?card)\b")
# Az oldalon kívüli elérhetőség (csevegőalkalmazás, e-mail): a máshová hívás átverés jele (ott nincs vevővédelem, és
# onnan jön a hamis futár- vagy fizetési link).
_OFFSITE_RE = re.compile(r"\b(whatsapp\w*|telegram\w*|viber\w*)\b|[\w.+-]+@[\w-]+\.[a-z]{2,}")
# Hibára utaló szó a leírásban (a „nem hibás”, „nem törött” nem számít).
_DEFECT_RE = re.compile(r"(?<!nem )\b(repedt|repedes\w*|beegett|beeges|nem kapcsol be|nem indul|javitasra szorul|"
                        r"hianyos|lyukas|szakadt|foltos)\b")
# Ugyanannak a dolognak más szavai (ékezet nélkül): a keresés minden szavának meg kell lennie a hirdetésben, de
# „kontroller” helyett „DualSense”, „videókártya” helyett „VGA” is jó.
SYNONYMS = {
    "kontroller": ("controller", "kontroler", "dualsense", "dualshock", "joystick", "gamepad"),
    "controller": ("kontroller", "kontroler", "dualsense", "dualshock", "joystick", "gamepad"),
    "videokartya": ("vga", "gpu", "grafikus"), "vga": ("videokartya", "gpu"),
    "ps5": ("playstation",), "ps4": ("playstation",), "laptop": ("notebook",), "notebook": ("laptop",),
    "telefon": ("mobil", "mobiltelefon", "okostelefon"), "fulhallgato": ("headset", "fejhallgato"),
    "fejhallgato": ("headset", "fulhallgato"),
}
# Kiegészítő a keresett dologhoz (töltő, tok, kábel…), vagy „-hoz/-hez” alak („kontrollerhez”): nem maga a termék.
_ACCESSORY_RE = re.compile(r"\b(tolto\w*|dokkolo\w*|tok|tokja|vedotok\w*|borito\w*|skin|matrica\w*|kabel\w*|"
                           r"allvany\w*|tarto|szilikon\w*|grip\w*|csere ?gomb\w*|ujjvedo\w*)\b")
_NO_REVIEWS_RE = re.compile(r"nincs(enek)? (meg )?ertekeles\w*|meg nincs ertekeles\w*|no reviews yet|\b0 ertekeles")

CHECK_SYSTEM = (
    "Egy használtcikk-kereskedőnek segítesz. Megkapod, mit keres, és egy hirdetés címét, szövegét és árát. "
    "Döntsd el, megéri-e megnézni: ok=true, ha ez tényleg a keresett termék (nem kiegészítő, nem alkatrész, nem "
    "más típus), egy darab, eladó (nem keres vagy cserél), és nem hibás vagy hiányos. suspicious=true, ha átverésnek "
    "vagy hamisítványnak látszik: túl szép, hogy igaz legyen, előre fizetést, más oldalon vagy alkalmazásban való "
    "kapcsolatfelvételt vagy futárcéget kér, sürget, vagy a leírás nem illik a termékhez. Ha a hirdetés fotóit is "
    "megkapod: ok=false, ha a fotón sérülés vagy hiány látszik, vagy nem a keresett termék van rajta; suspicious=true, "
    "ha a fotó letöltött (webshop- vagy katalógus-) képnek látszik a saját fotó helyett, vagy hamisítványnak tűnik. "
    "reason: egy rövid mondat magyarul. " + UNTRUSTED_NOTE)
JUDGE_SYSTEM = (
    "Egy használtcikk-kereskedőnek segítesz felmérni, mennyiért adják most a keresett terméket. Megkapod a keresett "
    "terméket és hirdetéseket (számozva, árral). Válaszd ki azokat, amelyek pontosan a keresett termék: ugyanaz a "
    "típus, egy darab, működő, eladó. Ne válaszd a kiegészítőt (tok, töltő, kábel), a más típust vagy kiadást, az "
    "alkatrészt, a hibásat, a csomagot és a keresőhirdetést. Árat ne becsülj, csak a sorszámokat add meg. reason: egy "
    "rövid mondat magyarul. " + UNTRUSTED_NOTE)
JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"similar": {"type": "array", "items": {"type": "integer"}}, "reason": {"type": "string"}},
    "required": ["similar", "reason"],
}
EXPLORE_SYSTEM = (
    "Egy használtcikk-kereskedőnek segítesz. Megkapod egy oldal friss hirdetéseit (számozva, árral; a licitnél a mostani "
    "ár és a zárásig hátralévő idő). Minden sorhoz, amelyikben egy konkrét, felismerhető termék van, adj egy rövid "
    "keresőszót (márka és típus, 2–5 szó, pl. „Sony WH-1000XM4”, „Makita DHP482”), amivel máshol megnézhetem, mennyiért "
    "adják; a többit hagyd ki. buy=true legfeljebb háromnál: ami egy viszonteladónak ezen az áron megérhet (ismert "
    "márkájú, keresett, működőnek látszó termék, aminek az ára jóval a szokásos ára alatt lehet). A kiegészítő, az "
    "alkatrész, a hibás, a csomag és a hamisítványnak látszó nem ilyen. reason: egy rövid mondat magyarul. "
    + UNTRUSTED_NOTE)
EXPLORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
        "n": {"type": "integer"}, "query": {"type": "string"}, "buy": {"type": "boolean"}},
        "required": ["n", "query", "buy"]}},
        "reason": {"type": "string"}},
    "required": ["items", "reason"],
}
CHECK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "suspicious": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["ok", "suspicious", "reason"],
}


class DealError(ItemError):
    pass


class _FastStop(Exception):
    """A gyors sor szála leáll (kilépés)."""


_LEFT_UNITS = {"nap": 86400, "ora": 3600, "perc": 60}


def parse_auction(text: str) -> dict[str, int | None]:
    """Egy licit hátralévő ideje (másodperc) és a licitek száma a kártya szövegéből („2 nap 3 óra”, „5 óra 12 perc”,
    „3 licit”, „Licitek: 3”); amit nem talál, None."""
    folded = fold(text)
    parts = re.findall(r"(\d+)\s*(nap|ora|perc)\b", folded)
    bids = re.search(r"(\d+)\s*licit|licit(?:ek)?(?: szama)?:?\s*(\d+)", folded)
    return {"left": sum(int(n) * _LEFT_UNITS[unit] for n, unit in parts) if parts else None,
            "bids": int(bids.group(1) or bids.group(2)) if bids else None}


def left_text(seconds: int | None) -> str:
    """„2 nap 3 óra”, „5 óra 12 perc”, „40 perc”."""
    if seconds is None:
        return "nem tudom, mikor"
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = [f"{days} nap"] if days else []
    if hours:
        parts.append(f"{hours} óra")
    if minutes and not days:
        parts.append(f"{minutes} perc")
    return " ".join(parts) or "kevesebb mint egy perc"


def pays(search: dict[str, Any], price: int, reference: int) -> bool:
    """Megéri-e: a szokásos árnál a megadott %-kal olcsóbb, vagy legalább a megadott haszon jön ki belőle (és
    egyáltalán hoz hasznot); nem gyanúsan olcsó (FLOOR), és a megadott ársávban van."""
    if price < reference * FLOOR:
        return False
    if (search.get("min_price") and price < search["min_price"]) or \
            (search.get("max_price") and price > search["max_price"]):
        return False
    profit = expected_profit(reference, price)
    if profit <= 0:
        return False
    by_percent = price <= reference * (100 - int(search.get("discount") or DEFAULT_DISCOUNT)) / 100
    return by_percent or bool(search.get("min_profit") and profit >= int(search["min_profit"]))


def highest_ok(search: dict[str, Any], reference: int) -> int | None:
    """A legmagasabb ár, ami ennél a keresésnél még megéri (pays): ennyit érdemes ajánlani vagy licitálni. None, ha a
    megadott ársávban nincs ilyen. Az ár csökkenésével csak jobb lesz, ezért felezéssel keresi meg a határt."""
    low = max(math.ceil(reference * FLOOR), int(search.get("min_price") or 0), 1)
    high = int(search.get("max_price") or reference)
    if high < low or not pays(search, low, reference):
        return None
    while low < high:
        middle = (low + high + 1) // 2
        if pays(search, middle, reference):
            low = middle
        else:
            high = middle - 1
    return low


def explore_top(reference: int, min_profit: int, low: int | None = None) -> int:
    """Felfedező: a legmagasabb ár (vagy licit), ami még megéri: legalább EXPLORE_CUT-tal a szokásos ár alatt, és
    legalább min_profit haszonnal (az óvatos eladási áron eladva, a postával; safe_sale)."""
    return math.floor(min(safe_sale(reference, low) - SHIPPING - min_profit, reference * (1 - EXPLORE_CUT)))


def safe_sale(reference: int, low: int | None = None) -> int:
    """Óvatos eladási ár: a szokásos ár 90%-a (SALE_RATIO; a hirdetési árnál többnyire kicsit olcsóbban kel el), de
    legfeljebb a hasonlók alsó negyede (low, ha tudjuk): a vevő azokat találja meg először, drágábban lassan kel el."""
    sale = reference * SALE_RATIO
    return int(round(min(sale, low) if low else sale))


def market(samples: list[tuple[Any, ...]]) -> dict[str, Any] | None:
    """Az összehasonlító árak ((ár, oldal) vagy (ár, oldal, elkelt) hármasok) összegzése: a szokásos ár (a középérték,
    a kilógók nélkül), az alsó és a felső negyed, hány ár, melyik oldalakról, az elkelt (eltűnt) hirdetések középára
    (sold, ha legalább SOLD_MIN van), és hogy elég biztos-e (sure_price)."""
    stats = price_stats([sample[0] for sample in samples])
    if not stats:
        return None
    kept = [sample for sample in samples if stats["min"] <= sample[0] <= stats["max"]]   # a kilógók oldala nem számít
    found: dict[str, Any] = {"price": round_price(stats["median"]), "p25": stats["p25"], "p75": stats["p75"],
                             "n": stats["n"], "sites": sorted({sample[1] for sample in kept if sample[1]})}
    sold = [sample[0] for sample in kept if len(sample) > 2 and sample[2]]
    if len(sold) >= SOLD_MIN:
        found["sold"] = {"n": len(sold), "median": round_price(statistics.median(sold))}
    found["sure"] = stats["confidence"] == "megbízható" and sure_price(found)
    return found


def careful_low(found: dict[str, Any]) -> int | None:
    """Az óvatos eladási árhoz (safe_sale) a felső határ: a hasonlók alsó negyede, és ha tudjuk, mennyiért keltek el
    ténylegesen (sold), legfeljebb az."""
    low, sold = found.get("p25"), (found.get("sold") or {}).get("median")
    candidates = [int(v) for v in (low, sold) if v]
    return min(candidates) if candidates else None


def spread_of(found: dict[str, Any]) -> float | None:
    """Mennyire szórnak a hasonlók árai: (felső negyed − alsó negyed) / középérték; None, ha nem tudni."""
    try:
        p25, p75, median = int(found["p25"]), int(found["p75"]), int(found.get("price") or found.get("median"))
    except (KeyError, TypeError, ValueError):
        return None
    return round((p75 - p25) / median, 2) if median > 0 and p75 >= p25 else None


def assess(deal: dict[str, Any]) -> dict[str, Any]:
    """Nyugodt szívvel megvehető-e: level „biztos” (legalább SAFE_ROI haszon a befektetett pénzhez képest, nem szórnak
    nagyon az árak, és nem lassan kelő), „jó”, vagy „kockázatos” (RISKY_ROI alatti haszon, vagy lassan kelő, kevés
    haszonnal). why: a kockázatok és a figyelmeztetések, good: ami mellette szól (a felületre és az értesítésbe)."""
    price = int(deal.get("offer") or deal["price"])
    roi = int(deal.get("profit") or 0) / price if price > 0 else 0.0
    risks, cautions, good = [], [], []
    if not deal.get("auction"):                # a licit mostani ára még nő: ott a haszonráta nem mond sokat
        if roi < RISKY_ROI:
            risks.append(f"kicsi a haszon a befektetett pénzhez képest ({round(roi * 100)}%)")
        elif roi >= SAFE_ROI:
            good.append(f"{round(roi * 100)}% haszon")
    if deal.get("pace") == "lassú":
        (cautions if roi >= SLOW_OK_ROI else risks).append("lassan kel el (a hasonlók sokáig fent maradnak)")
    elif deal.get("pace") == "gyors":
        good.append("gyorsan kel el")
    spread = deal.get("spread")
    if spread is not None and spread > WIDE_SPREAD:
        cautions.append("nagyon szórnak a hasonlók árai")
    if deal.get("sold"):
        good.append(f"{deal['sold']['n']} elkelt hasonló alapján is")
    if deal.get("similar") and len(deal.get("sites") or []) >= 2:
        good.append(f"{deal['similar']} hasonló, {len(deal['sites'])} oldalon")
    level = ("kockázatos" if risks else
             "biztos" if not cautions and (deal.get("auction") or roi >= SAFE_ROI) else "jó")
    return {"level": level, "why": risks + cautions, "good": good}


def safety_text(safety: dict[str, Any] | None) -> str:
    """„Biztos befektetés: 32% haszon, gyorsan kel el.” / „Jó vétel, de figyelj: nagyon szórnak az árak.”"""
    if not safety:
        return ""
    level, why, good = safety.get("level"), safety.get("why") or [], safety.get("good") or []
    if level == "biztos":
        return "Biztos befektetés" + (f": {', '.join(good)}." if good else ".")
    if level == "kockázatos":
        return "Kockázatos: " + "; ".join(why) + "."
    return "Jó vétel" + (f", de figyelj: {'; '.join(why)}." if why else (f": {', '.join(good)}." if good else "."))


def sure_price(reference: dict[str, Any]) -> bool:
    """Elég biztos-e egy szokásos ár: legalább SURE_MIN hasonló áron alapul, két oldalról (vagy egy oldalról legalább
    SURE_ALONE áron)."""
    n, sites = int(reference.get("n") or 0), reference.get("sites") or []
    return n >= SURE_MIN and (len(sites) >= 2 or n >= SURE_ALONE)


def last_chance(close: float, day: tuple[int, int]) -> float:
    """Az utolsó időpont a zárás előtt, amikor a gép megy (day: reggel és este órája, helyi idő): ha napközben zárul, maga
    a zárás; ha éjjel vagy kora reggel, az előző este vége (addig kell szólni)."""
    start, end = day
    local = time.localtime(close)
    hour = local.tm_hour + local.tm_min / 60
    if start <= hour < end:
        return close
    evening = close - (local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec) + end * 3600
    return evening if hour >= end else evening - 86400


def offer_price(search: dict[str, Any], price: int, reference: int) -> int | None:
    """Vinted-ajánlat: ha a kért ár kicsit több annál, ami még megéri (highest_ok), de legfeljebb OFFER_CUT-tal, ennyit
    ajánlj (kerek százasra lefelé). None, ha a kért ár már most is megéri, vagy túl nagy engedmény kellene."""
    top = highest_ok(search, reference)
    if top is None or price <= top:
        return None
    offer = top // 100 * 100
    return offer if offer > 0 and offer >= price * (1 - OFFER_CUT) else None


class ModelLane:
    """Modell-sor: egy háttérszál, ami a modellfeladatokat egyenként végzi (egyszerre mindig csak egy modellhívás), amíg a
    böngésző a következő oldalt tölti. Az eredményt nem ő dolgozza fel: a kész feladat folytatása (then) a böngésző
    szálán fut le (deliver), mert onnan kell továbbmenni (pl. egy hirdetés oldalát megnyitni, értesíteni). A hívásai
    háttérhívások (llm.background_calls): a feltöltés és a beszélgetés előbb jön."""

    def __init__(self) -> None:
        self._tasks: queue.Queue = queue.Queue()
        self._done: queue.Queue = queue.Queue()
        self._pending = 0
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="jarvis-modell-sor", daemon=True)
        self._thread.start()

    def submit(self, call: Callable[[], Any], then: Callable[[Any], None]) -> None:
        with self._lock:
            self._pending += 1
        self._tasks.put((call, then))

    def _loop(self) -> None:
        with background_calls():
            self._work()

    def _work(self) -> None:
        while not self._closed.is_set():
            try:
                call, then = self._tasks.get(timeout=0.2)
            except queue.Empty:
                continue
            if self._closed.is_set():
                break
            try:
                result = call()
            except Exception:  # noqa: BLE001 - a hívások maguk kezelik a hibát; ami mégis kijön, az „nincs válasz”
                result = None
            self._done.put((then, result))

    def deliver(self, wait: float = 0.0) -> bool:
        """Egy kész modellfeladat folytatása a hívó (böngésző) szálán. Visszaadja, volt-e ilyen."""
        try:
            then, result = self._done.get(timeout=wait) if wait else self._done.get_nowait()
        except queue.Empty:
            return False
        with self._lock:
            self._pending -= 1
        then(result)
        return True

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._pending > 0

    def close(self) -> None:
        """A kör vége (vagy megszakadt): a szál a mostani hívása után kilép, a maradék feladat elmarad."""
        self._closed.set()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()


class _Round:
    """Egy kör állapota: a böngésző szálán sorra kerülő lépések (work), a modell-sor, a keret (hány hirdetés oldalát
    nyithatja meg, hányszor kérdezheti a modellt), és az összefoglaló."""

    def __init__(self, check: Callable[[], None], notify: Callable[..., None], lane: ModelLane | None) -> None:
        self.check, self.notify, self.lane = check, notify, lane
        self.work: deque[Callable[[], None]] = deque()
        self.pages, self.checks, self.resale, self.blocked = MAX_PAGES, MAX_CHECKS, EXPLORE_PICKS, False
        self.found = self.done = 0
        self.loaded = self.named = 0          # betöltött oldalak, a modell által megnevezett hirdetések
        self.captcha = ""                     # ezen az oldalon kért robot-ellenőrzést
        self.closing = False                  # böngészés: a munkamenet a kör végén lezárul
        self.hits: list[str] = []             # a jó találatok röviden (a naplóba)
        self.notes: list[str] = []
        self.errors: list[str] = []
        self.browse_lane = LANE_BROWSER       # a böngészés melyik sorában vagyunk
        self.no_browser = False                # a gyors sor szála: a böngészőhöz nem nyúlhat (az az ágens-szálé)
        # A gyors (böngésző nélküli) oldalak letöltése a háttérben, amíg a böngésző a többit nézi: webcím → eredmény.
        self.pool: ThreadPoolExecutor | None = None
        self.prefetch: dict[str, Future] = {}
        self.fast = 0                          # ennyi oldal jött böngésző nélkül

    def fetch_ahead(self, fetcher: Any, url: str, platform: str) -> None:
        """Egy gyors oldal letöltése a háttérben (ha még nem indult el, és az oldal most így olvasható)."""
        if self.pool is not None and fetcher is not None and url not in self.prefetch and fetcher.usable(platform):
            self.prefetch[url] = self.pool.submit(fetcher.listings, url, platform, self.check)

    def next(self, steps: list[Callable[[], None]]) -> None:
        """Ezek jönnek rögtön (pl. egy keresés jelöltjei a következő keresés előtt)."""
        self.work.extendleft(reversed(steps))

    def ask(self, call: Callable[[], Any], then: Callable[[Any], None]) -> None:
        """Modellfeladat a modell-sorba; modell nélkül rögtön a folytatás jön, „nincs válasz” (None) eredménnyel."""
        if self.lane is None:
            then(None)
        else:
            self.lane.submit(call, then)

    def loop(self) -> None:
        """A böngésző szála: a lépések sorban, közben a kész modellfeladatok folytatása; addig, amíg van mit tenni."""
        while True:
            self.check()
            if self.lane is not None and self.lane.deliver():
                continue
            if self.work:
                self.work.popleft()()
                continue
            if self.lane is not None and self.lane.busy:
                self.lane.deliver(wait=0.3)
                continue
            return


def matches_query(query: str, text: str) -> bool:
    """A hirdetés tényleg a keresett dolog-e: a keresés minden szava (vagy a rokon szava, SYNONYMS) benne van; négy
    szónál hosszabb keresésnél egy hiányozhat. Így a „PS5 kontroller” keresésre a PS5-játék nem jelölt."""
    wanted = _tokens(query)
    have = _tokens(text)
    if not wanted:
        return True
    hits = sum(1 for word in wanted if _shared({word, *SYNONYMS.get(word, ())}, have))
    if hits < (len(wanted) if len(wanted) <= 4 else len(wanted) - 1):
        return False
    folded = fold(query)
    accessory = [m.group(0) for m in _ACCESSORY_RE.finditer(fold(text)) if m.group(0) not in folded]
    for_it = [w for w in have if w.endswith(("hoz", "hez")) and any(w.startswith(q) and w != q for q in wanted)]
    return not accessory and not for_it


def expected_profit(reference: int, price: int, low: int | None = None) -> int:
    """Várható haszon, ha megveszed, és a szokásos áron hirdeted: a hirdetési árnál kicsit olcsóbban kel el
    (SALE_RATIO; ha tudjuk a hasonlók alsó negyedét, legfeljebb annyiért: safe_sale), és a postázás is a tiéd
    (SHIPPING)."""
    return int(round(safe_sale(reference, low) - price - SHIPPING))


def parse_words(value: Any) -> list[str]:
    """Kizáró szavak („tok, töltő, játék” vagy lista): kisbetűvel, ahogy beírtad, legfeljebb MAX_EXCLUDE (az
    ékezet csak az egyezésnél nem számít, lásd excluded)."""
    parts = re.split(r"[,;\n]+", value) if isinstance(value, str) else \
        [str(v) for v in value] if isinstance(value, (list, tuple)) else []
    out: list[str] = []
    for part in parts:
        word = " ".join(part.lower().split())[:30]
        if word and fold(word) not in {fold(w) for w in out}:
            out.append(word)
    return out[:MAX_EXCLUDE]


def excluded(words: list[str], text: str) -> bool:
    """Van-e a hirdetésben kizáró szó, ékezet nélkül is (a szó eleje számít: „tok” → „tokkal”, „tokot” is)."""
    folded = fold(text)
    return any(re.search(rf"\b{re.escape(fold(word))}", folded) for word in words)


def search_status(search: dict[str, Any]) -> str:
    """fut: van jóváhagyott szokásos ár; javaslat: jóváhagyásra vár; kevés: nem talált elég hasonlót (kézzel kell);
    felmérés: a következő körben felméri."""
    if search.get("reference"):
        return "fut"
    proposal = search.get("proposal") or {}
    if proposal.get("price"):
        return "javaslat"
    return "kevés" if proposal else "felmérés"


LEDGER_MONTHS = 6          # a Vételeim fülön ennyi hónap haszna látszik havonta
IDLE_DAYS = 3              # ennyi nap után szól, hogy a nálad lévő tárgy még nincs fent sehol
TRANSIT_DAYS = 7           # ennyi nap után szól, hogy a csomag még mindig úton van
PRICE_UP = 1.1             # ha most legalább ennyivel drágábban adják (a 90%-ot nézve), mint amennyiért eladnád


def item_cost(item: Item) -> int:
    """Amibe egy megvett tárgy került: a vételár, a posta és az egyéb költség (javítás, tisztítás…)."""
    return int(item.bought_price or 0) + int(item.bought_shipping or 0) + int(item.bought_extra or 0)


def sale_estimate(item: Item) -> int | None:
    """Mennyiért adod el várhatóan: amit megadtál (tervezett eladási ár), különben a hirdetési ár, különben a vételkori
    szokásos ár 90%-a (a hirdetési árnál többnyire kicsit olcsóbban kel el). None, ha egyik sincs meg."""
    if item.target_price:
        return int(item.target_price)
    if item.price:
        return int(item.price)
    return round_price(item.market_price * SALE_RATIO) if item.market_price else None


def stage(item: Item) -> str:
    """Hol tart egy megvett tárgy: úton (még nem érkezett meg), nálad (még nincs fent), fent, eladva."""
    if item.sold:
        return "eladva"
    if item.status in ("fent", "részben fent"):
        return "fent"
    return "úton" if item.in_transit else "nálad"


def _percent(profit: int, cost: int) -> int | None:
    return round(profit * 100 / cost) if cost > 0 else None


def _advice(row: dict[str, Any]) -> list[str]:
    """Tippek egy megvett tárgyhoz: ha most drágábban adják, mint amennyiért eladnád; ha napok óta nálad van, de még
    nincs fent; ha a csomag régóta úton van."""
    out = []
    market, estimate = row.get("market_now"), row.get("estimate")
    if market and estimate and market["price"] * SALE_RATIO >= estimate * PRICE_UP:
        out.append(f"Most drágábban adják: kérhetsz többet (kb. {format_price(round_price(market['price'] * SALE_RATIO))}"
                   f"-ért is elkelhet).")
    if row["stage"] == "nálad" and row["days"] >= IDLE_DAYS:
        out.append(f"{row['days']} napja nálad van, és még nincs fent sehol: tölts fel róla hirdetést.")
    if row["stage"] == "úton" and row["days"] >= TRANSIT_DAYS:
        out.append(f"{row['days']} napja úton van: kérdezd meg az eladót, vagy jelöld megérkezettnek.")
    return out


def profit_report(items: list[Item], now: float | None = None, book: PriceBook | None = None) -> dict[str, Any]:
    """Haszonkövetés (Vételeim) a vételárral megjelölt tárgyakból (Megvettem, vagy a tárgy lapján a Vételár). Egy tárgy
    bekerülése a vételár, a posta és az egyéb költség; a haszna az eladási ár, mínusz az eladás költségei (posta, díj) és
    a bekerülés. Ebben a hónapban, összesen és havonta (LEDGER_MONTHS); átlagosan hány nap alatt kelt el, mennyi pénz
    van még bent a meg nem eladott tárgyakban, és mennyi haszon várható belőlük (sale_estimate). Ha az árbázis megvan
    (book), a meg nem eladott tárgyaknál azt is, mennyiért adják most."""
    now = time.time() if now is None else now
    month = time.strftime("%Y-%m", time.localtime(now))
    rows: list[dict[str, Any]] = []
    total = {"profit": 0, "count": 0, "spent": 0, "revenue": 0, "roi": None}
    this_month = {"profit": 0, "count": 0, "revenue": 0}
    stock = {"count": 0, "spent": 0, "expected": 0, "value": 0, "unknown": 0}
    by_month: dict[str, dict[str, Any]] = {}
    stages: Counter = Counter()
    days: list[int] = []
    for item in items:
        if item.bought_price is None:
            continue
        cost, sell_cost = item_cost(item), int(item.sell_shipping or 0) + int(item.sell_fee or 0)
        bought_at = item.bought_at or item.created
        where = stage(item)
        stages[where] += 1
        row: dict[str, Any] = {
            "id": item.id, "name": item.name, "stage": where, "sold": item.sold, "status": item.status,
            "photo": item.photos[0] if item.photos else "", "source": item.source, "bought_from": item.bought_from,
            "bought_at": bought_at, "bought_price": item.bought_price, "bought_shipping": item.bought_shipping,
            "bought_extra": item.bought_extra, "cost": cost, "market_price": item.market_price,
            "target_price": item.target_price, "price": item.price, "sold_price": item.sold_price,
            "sold_at": item.sold_at or None, "sell_shipping": item.sell_shipping, "sell_fee": item.sell_fee,
            "in_transit": item.in_transit, "trade_note": item.trade_note, "profit": None, "roi": None,
            "expected": None, "estimate": None, "market_now": None,
            "days": max(0, int(((item.sold_at if item.sold and item.sold_at else now) - bought_at) // 86400))}
        if item.sold and item.sold_price is not None:
            profit = item.sold_price - sell_cost - cost
            row.update(profit=profit, roi=_percent(profit, cost))
            total["profit"] += profit
            total["count"] += 1
            total["spent"] += cost
            total["revenue"] += item.sold_price
            if item.sold_at:
                days.append(row["days"])
                when = time.strftime("%Y-%m", time.localtime(item.sold_at))
                bucket = by_month.setdefault(when, {"month": when, "profit": 0, "count": 0})
                bucket["profit"] += profit
                bucket["count"] += 1
                if when == month:
                    this_month["profit"] += profit
                    this_month["count"] += 1
                    this_month["revenue"] += item.sold_price
        elif not item.sold:
            stock["count"] += 1
            stock["spent"] += cost
            estimate = sale_estimate(item)
            if estimate is None:
                stock["unknown"] += 1
            else:
                expected = estimate - sell_cost - cost
                row.update(estimate=estimate, expected=expected, roi=_percent(expected, cost))
                stock["expected"] += expected
                stock["value"] += estimate
            if book is not None:
                found = next((k for k in (book.key(t) for t in (item.title, item.product, item.search_query) if t)
                              if k), None)
                reference = book.reference(found[0]) if found else None
                if reference:
                    row["market_now"] = {"price": reference["price"], "n": reference["n"], "name": reference["name"],
                                         "speed": speed_text(book.speed(found[0]))}
        row["advice"] = _advice(row)
        rows.append(row)
    total["roi"] = _percent(total["profit"], total["spent"])
    order = {"úton": 0, "nálad": 1, "fent": 2, "eladva": 3}
    rows.sort(key=lambda r: (order[r["stage"]], -(r["sold_at"] or r["bought_at"] or 0)))
    months = [by_month[key] for key in sorted(by_month, reverse=True)[:LEDGER_MONTHS]]
    return {"month": this_month, "total": total, "open": stock, "months": months, "stages": dict(stages),
            "days": round(sum(days) / len(days)) if days else None, "rows": rows}


class DealHunter:
    """A keresések, a kapcsoló és a talált olcsó hirdetések (munka/kereskedes.json)."""

    def __init__(self, root: Path, platforms: dict[str, Platform], browser: Callable[[], Any] | None,
                 llm: Any = None, clock: Callable[[], float] = time.time, default_every: float = DEFAULT_EVERY,
                 rng: random.Random | None = None, fetcher: Any = None,
                 image_get: Callable[[str], bytes | None] = fetch_image):
        self.path = Path(root) / "kereskedes.json"
        self.image_get = image_get            # a hirdetés fotói a végső modellkérdéshez (_confirm)
        self.platforms = {k: p for k, p in platforms.items() if k in DEAL_SITES and p.search_url}
        self.browser = browser
        # A Hardverapró és a Jófogás böngésző nélkül (feeds.SiteFetcher): gyorsabb, és a böngészővel párhuzamosan megy.
        self.fetcher = fetcher
        self.llm = llm
        self.clock = clock
        self.default_every = default_every    # az új keresések gyakorisága ([scheduler] deal_minutes)
        self.offering = ""                    # ajánlattétel közben a jóváhagyó ablak első sora (WorkDesk.confirm_context)
        self.book = PriceBook(Path(root), clock)   # árbázis: minden látott hirdetés ára, termékenként
        self.journal = Journal(Path(root) / "kereskedes-naplo.json", clock)   # mit csinált a nap folyamán
        self.rng = rng or random.Random()
        self._pending: list[dict[str, Any]] = []   # böngészés: a modellre váró ismeretlen hirdetések (kötegben)
        self._fast_pending: list[dict[str, Any]] = []   # ugyanez a gyors sorban
        self.parallel = False                 # fut-e a gyors sor szála (start_fast_lane)
        self._fast_stop = threading.Event()
        self._fast_thread: threading.Thread | None = None
        self._book_saved = 0.0
        self.items: Callable[[], list[Item]] | None = None   # a tárgyak (a havi kerethez; a WorkDesk állítja be)
        self._remaining: tuple[float, int | None] = (0.0, None)   # a havi keretből maradt (pár másodpercig érvényes)
        self._lock = threading.RLock()
        self._data = self._load()

    # --- tárolás ---

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        numbers = lambda key: {str(k): float(v) for k, v in (data.get(key) or {}).items()  # noqa: E731
                               if isinstance(v, (int, float))}
        day = data.get("day")
        if not (isinstance(day, list) and len(day) == 2 and all(isinstance(h, int) for h in day) and day[0] < day[1]):
            day = list(DEFAULT_DAY)
        browse = data.get("browse") if isinstance(data.get("browse"), dict) else {}
        dicts = lambda key: {str(k): v for k, v in (data.get(key) or {}).items() if isinstance(v, dict)}  # noqa: E731
        return {"enabled": bool(data.get("enabled")), "day": day,
                "max_buy": int(data.get("max_buy") or 0) or None,   # legfeljebb ennyiért vegyen (minden keresésnél)
                "safe_only": data.get("safe_only") is not False,    # csak a nyugodt szívvel megvehetőkről szól (assess)
                "message": str(data.get("message") or DEFAULT_MESSAGE)[:500],
                # pénzügyi keret: havi keret, és mikor szóljon, hogy túl sok megvett tárgy vár eladásra (money_check)
                "money": {"budget": None, "stock_count": None, "stock_value": None, "told": {},
                          **(data.get("money") if isinstance(data.get("money"), dict) else {})},
                "started": float(data.get("started") or 0),
                "browse": {"on": bool(browse.get("on")),
                           "sites": [s for s in browse.get("sites") or BROWSE_ORDER if s in BROWSE_ORDER],
                           "min_profit": int(browse.get("min_profit") or WATCH_PROFIT)},
                "lanes": dicts("lanes"),          # indulóoldal → mit hozott (és a tanultak címe)
                "session": data.get("session") if isinstance(data.get("session"), dict) else {},
                "fast_session": data.get("fast_session") if isinstance(data.get("fast_session"), dict) else {},
                "sites_state": {"ended": {}, "rest": {}, "blocked": {}, "learned": "",
                                **(data.get("sites_state") if isinstance(data.get("sites_state"), dict) else {})},
                "feedback": dicts("feedback"),    # termék → elrejtve / megvetted (öntanulás)
                "searches": [s for s in data.get("searches") or [] if isinstance(s, dict) and s.get("query")],
                "watches": [w for w in data.get("watches") or [] if isinstance(w, dict) and w.get("url")],
                "deals": [d for d in data.get("deals") or [] if isinstance(d, dict) and d.get("url")],
                "seen": numbers("seen"),       # hirdetés → mikor néztük meg utoljára (egy keresés ehhez igazodik)
                "cancelled": numbers("cancelled"),   # visszamondott vétel hirdetése → mikor: többé nem szól róla
                "prices": {k: int(v) for k, v in numbers("prices").items()},   # hirdetés → az ára akkor (leárazás)
                "last": numbers("last")}       # keresés:oldal → mikor néztük meg (gyakoriság)

    def _save(self) -> None:
        with self._lock:                      # két szál is menthet (a böngésző sora és a gyors sor)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)

    def state(self) -> dict[str, Any]:
        """A felületnek: a kapcsoló, a keresések (az állapotukkal) és a talált hirdetések (a legújabb elöl)."""
        with self._lock:
            deals = [d for d in self._data["deals"] if not d.get("dismissed")]
            searches = [{**json.loads(json.dumps(s)), "status": search_status(s)} for s in self._data["searches"]]
            return {"enabled": self._data["enabled"], "running": self._data["enabled"],
                    "started": self._data["started"] or None, "max_buy": self._data["max_buy"],
                    "safe_only": self._data["safe_only"],
                    "message": self._data["message"], "money": self.money_status(), "searches": searches,
                    "watches": json.loads(json.dumps(self._data["watches"])),
                    "deals": sorted((dict(d) for d in deals), key=lambda d: -float(d.get("at") or 0)),
                    "sites": [{"key": k, "name": p.name} for k, p in self.platforms.items()],
                    "browser": self.browser is not None, "sale_ratio": SALE_RATIO, "shipping": SHIPPING,
                    "day": list(self._data["day"]), "book": self.book.stats(),
                    "browse": {**json.loads(json.dumps(self._data["browse"])),
                               "status": self._browse_status(self.clock())},
                    "lanes": self._lane_list(), "journal": self.journal.entries(), "journal_days": self.journal.days()}

    def available(self) -> bool:
        """Van-e most dolga: egy esedékes keresés, figyelt oldal, vagy a böngészés következő lépése. Az ütemező csak
        ekkor indítja (így nem fut feleslegesen, és a felület sem jelez minden percben munkát)."""
        if self.browser is None:
            return False
        now = self.clock()
        with self._lock:
            if not self._data["enabled"]:
                return False
            for search in self._data["searches"]:
                if not self._search_waits(search, now) and any(
                        self._due(search, key, now) for key in search.get("sites") or list(self.platforms)
                        if key in self.platforms):
                    return True
            for watch in self._data["watches"]:
                platform = watch.get("platform") or "vatera"
                if platform in self.platforms and self._due(watch, platform, now):
                    return True
            return self._browse_due(now)

    @staticmethod
    def _search_waits(search: dict[str, Any], now: float) -> bool:
        """A keresés most nem fut: a szokásos ár jóváhagyásra vár, vagy kevés volt a hasonló (egy nap múlva újra)."""
        status, proposal = search_status(search), search.get("proposal") or {}
        return status == "javaslat" or (status == "kevés" and now - float(proposal.get("at") or 0) < CALIBRATE_RETRY)

    def browsing(self) -> bool:
        """Most a napközbeni böngészés ideje van-e (be van kapcsolva, és a gép üzemidejében vagyunk): ilyenkor a
        kereskedés a Jarvis készenlétében is fut (az ütemező standby_ok-ja)."""
        with self._lock:
            return (self._data["enabled"] and self._data["browse"]["on"] and self.browser is not None
                    and self._day_ok(self.clock()))

    def note(self) -> str:
        """Az Automatikus munkák ablakának: mit csinál most a böngészés (üres, ha nincs bekapcsolva)."""
        with self._lock:
            return self._browse_status(self.clock()) if self._data["enabled"] else ""

    def set_browse(self, on: Any = None, sites: Any = None, min_profit: Any = None) -> dict[str, Any]:
        """Böngészés napközben: be/ki, melyik oldalakon, és legalább mennyi haszonnal szóljon."""
        profit = self._amount(min_profit, "A legkisebb haszon") if min_profit not in (None, "") else None
        with self._lock:
            browse = self._data["browse"]
            if on is not None:
                browse["on"] = bool(on)
                if not browse["on"]:
                    self._data["session"] = self._data["fast_session"] = {}
            if sites is not None:
                chosen = [k for k in BROWSE_ORDER if k in (sites if isinstance(sites, (list, tuple)) else [])]
                if not chosen:
                    raise DealError("Legalább egy oldalt jelölj be.")
                browse["sites"] = chosen
            if profit:
                browse["min_profit"] = profit
            self._save()
        if on is not None:
            self.journal.add("Bekapcsoltad a napközbeni böngészést." if on else "Kikapcsoltad a napközbeni böngészést.")
        return self.state()

    def journal_entries(self, day: str = "") -> list[dict[str, Any]]:
        return self.journal.entries(day if re.match(r"^\d{4}-\d{2}-\d{2}$", day or "") else "")

    def set_enabled(self, on: bool) -> dict[str, Any]:
        with self._lock:
            self._data["enabled"] = bool(on)
            self._save()
        return self.state()

    def start(self) -> dict[str, Any]:
        """Indítás (a fül gombja): minden keresés, a figyelt oldalak és a böngészés is, a nap bármelyik órájában, amíg le
        nem állítod (a pihenők maradnak). Az azonnali figyelőt a hívó indítja (instant.py)."""
        with self._lock:
            already = self._data["enabled"] and self._data["browse"]["on"]
            self._data["enabled"] = True
            self._data["browse"]["on"] = True
            self._data["day"] = [0, 24]           # nincs napszak: amíg fut, dolgozik (az esti leállás sem számít)
            if not already:
                self._data["started"] = self.clock()
            self._save()
        if not already:
            self.journal.add("Elindítottad az automata kereskedést (keresések, figyelt oldalak, böngészés, azonnali "
                             "figyelő).")
        return self.state()

    def stop(self) -> dict[str, Any]:
        """Leállítás: semmi nem fut, amíg újra el nem indítod (a beállítások, a találatok és az árbázis megmaradnak)."""
        with self._lock:
            running = self._data["enabled"]
            self._data["enabled"] = False
            self._data["browse"]["on"] = False
            self._data["session"] = self._data["fast_session"] = {}
            self._data["started"] = 0.0
            self._save()
        if running:
            self.journal.add("Leállítottad az automata kereskedést.")
        return self.state()

    def set_limits(self, max_buy: Any = None, min_profit: Any = None, safe_only: Any = None) -> dict[str, Any]:
        """Minden keresésre: legfeljebb ennyiért vegyen (üresen: bármennyiért), legalább ennyi haszon legyen benne (a
        figyelt oldalaknál, a böngészésnél és az azonnali figyelőnél; a keresésnek saját szabálya van), és csak a
        nyugodt szívvel megvehetőkről szóljon-e (safe_only; None: marad)."""
        cap = self._amount(max_buy, "A legmagasabb vételár") if max_buy not in (None, "") else None
        profit = self._amount(min_profit, "A legkisebb haszon") if min_profit not in (None, "") else None
        with self._lock:
            self._data["max_buy"] = cap
            if profit:
                self._data["browse"]["min_profit"] = profit
            if safe_only is not None:
                self._data["safe_only"] = bool(safe_only)
            self._save()
        return self.state()

    def state_running(self) -> bool:
        """Fut-e (az Indítás után, a Leállításig)."""
        with self._lock:
            return bool(self._data["enabled"])

    def _cap(self) -> int | None:
        """A vételi limit: a max. vételár, és ha van havi keret, a belőle maradt összeg (a kisebbik); None: nincs."""
        with self._lock:
            caps = [c for c in (self._data["max_buy"], self._budget_left()) if c is not None]
        return min(caps) if caps else None

    def _budget_left(self) -> int | None:
        now = self.clock()
        if now - self._remaining[0] > 30:     # a tárgyak összeadása ne minden hirdetésnél fusson le
            self._remaining = (now, self.money_status(now)["remaining"])
        return self._remaining[1]

    def _affordable(self, price: int) -> bool:
        """Belefér-e a vételi limitbe (set_limits, és a havi keret maradéka; üresen bármi)."""
        cap = self._cap()
        return cap is None or int(price) <= cap

    def set_money(self, budget: Any = None, stock_count: Any = None, stock_value: Any = None) -> dict[str, Any]:
        """Pénzügyi keret: havi keret (ennyit költhetsz egy hónapban vételre; ha fogy, csak a maradékba beleférő
        ajánlatokról szól), és mikor szóljon, hogy túl sok megvett tárgy vár eladásra (darab, és mennyi pénz van bennük).
        Üresen nincs."""
        values = {"budget": self._amount(budget, "A havi keret") if budget not in (None, "") else None,
                  "stock_value": self._amount(stock_value, "A bent lévő pénz") if stock_value not in (None, "") else None}
        try:
            values["stock_count"] = max(1, int(stock_count)) if stock_count not in (None, "") else None
        except (TypeError, ValueError):
            raise DealError("A tárgyak száma egész szám legyen.") from None
        with self._lock:
            self._data["money"].update(values)
            self._remaining = (0.0, None)
            self._save()
        return self.state()

    def money_status(self, now: float | None = None) -> dict[str, Any]:
        """A havi keret és a meg nem eladott tárgyak: mennyit költöttél ebben a hónapban vételre (vételár + posta +
        egyéb), mennyi maradt, hány megvett tárgy vár eladásra és mennyi pénz van bennük, és a figyelmeztetések."""
        now = self.clock() if now is None else now
        items = list(self.items() if self.items is not None else [])
        with self._lock:
            money = dict(self._data["money"])
        month = time.strftime("%Y-%m", time.localtime(now))
        bought = [i for i in items if i.bought_price is not None]
        spent = sum(item_cost(i) for i in bought
                    if time.strftime("%Y-%m", time.localtime(i.bought_at or i.created)) == month)
        stock = sorted((i for i in bought if not i.sold), key=lambda i: i.bought_at or i.created)
        value = sum(item_cost(i) for i in stock)
        budget = money.get("budget")
        remaining = max(0, int(budget) - spent) if budget else None
        warnings = []
        if budget and spent >= budget:
            warnings.append(f"Elfogyott a havi keret ({format_price(budget)}): ebben a hónapban már nem szólok újabb "
                            "ajánlatról.")
        elif budget and spent >= budget * 0.8:
            warnings.append(f"A havi keret {round(spent * 100 / budget)}%-a elment: már csak {format_price(remaining)} "
                            "maradt, csak az ebbe beleférő ajánlatokról szólok.")
        over_count = money.get("stock_count") and len(stock) > int(money["stock_count"])
        over_value = money.get("stock_value") and value > int(money["stock_value"])
        if over_count or over_value:
            ages = [max(0, int((now - (i.bought_at or i.created)) // 86400)) for i in stock[:3]]
            oldest = ", ".join(f"{i.name[:40]} ({f'{age} napja' if age else 'ma vetted'})"
                               for i, age in zip(stock[:3], ages))
            warnings.append(f"Felhalmozódtak a megvett tárgyak: {len(stock)} vár eladásra, {format_price(value)} van "
                            f"bennük. Előbb ezeket add el; a legrégebbiek: {oldest}.")
        return {"budget": budget, "spent": spent, "remaining": remaining, "stock_count": len(stock),
                "stock_value": value, "limits": {k: money.get(k) for k in ("budget", "stock_count", "stock_value")},
                "warnings": warnings, "over_stock": bool(over_count or over_value)}

    def money_check(self, notify: Callable[..., None], now: float | None = None) -> list[str]:
        """Szól, ha a havi keret 80%-a elment, ha elfogyott (havonta egyszer), vagy ha túl sok megvett tárgy vár
        eladásra (naponta egyszer, amíg így van). Vételkor és naponta fut. Visszaadja, miről szólt."""
        now = self.clock() if now is None else now
        self._remaining = (0.0, None)         # egy vétel után rögtön az új maradékkal számol
        status = self.money_status(now)
        month, today = time.strftime("%Y-%m", time.localtime(now)), day_of(now)
        told_now = []
        with self._lock:
            told = self._data["money"].setdefault("told", {})
            budget = status["budget"]
            if budget and status["spent"] >= budget and told.get("budget_out") != month:
                told["budget_out"] = told["budget_80"] = month
                told_now.append(("Elfogyott a havi keret", status["warnings"][0]))
            elif budget and status["spent"] >= budget * 0.8 and told.get("budget_80") != month:
                told["budget_80"] = month
                told_now.append(("A havi keret nagy része elment", status["warnings"][0]))
            if status["over_stock"] and told.get("stock") != today:
                told["stock"] = today
                told_now.append(("Sok tárgy vár eladásra", status["warnings"][-1]))
            if told_now:
                self._save()
        for title, text in told_now:
            notify("info", title, text, actions=[{"action": "ledger", "label": "Vételeim"}])
            self.journal.add(text)
        return [text for _title, text in told_now]

    def set_message(self, text: Any) -> dict[str, Any]:
        """Az eladónak szóló üzenet (a megérő találat ablakának gombja ezt másolja ki); üresen az alapüzenet."""
        lines = [" ".join(line.split()) for line in str(text or "").strip().splitlines()]
        message = "\n".join(lines).strip()[:500] or DEFAULT_MESSAGE
        with self._lock:
            self._data["message"] = message
            self._save()
        return self.state()

    def _capped(self, amount: int) -> int:
        """Legfeljebb a vételi limit (pl. a legmagasabb licit vagy ajánlat)."""
        cap = self._cap()
        return min(int(amount), cap) if cap is not None else int(amount)

    def _within_reach(self, row: dict[str, Any]) -> bool:
        """Érdemes-e egyáltalán megnézni a limit miatt: belefér, vagy a Vinteden egy ajánlattal beleférhet."""
        cap = self._cap()
        price = int(row["price"])
        return cap is None or price <= cap or (row.get("platform") == "vinted" and not row.get("auction")
                                               and price * (1 - OFFER_CUT) <= cap)

    def set_day(self, start: Any, end: Any) -> dict[str, Any]:
        """Mikor megy általában a gép (óra, helyi idő): a licitekről még az esti leállás előtt szól."""
        try:
            first, last = int(start), int(end)
        except (TypeError, ValueError):
            raise DealError("Az órák egész számok legyenek (pl. 7 és 23).") from None
        if not 0 <= first < last <= 24:
            raise DealError("Az este legyen később, mint a reggel (0–24 óra).")
        with self._lock:
            self._data["day"] = [first, last]
            self._save()
        return self.state()

    def lookup(self, text: str) -> list[dict[str, Any]]:
        """Mennyit ér? Az árbázisból: a beírt termék szokásos ára és eladási sebessége (modell és oldalbetöltés nélkül)."""
        rows = self.book.lookup(" ".join(str(text or "").split())[:80])
        for row in rows:
            row["speed_text"] = speed_text(row["speed"])
        return rows

    # --- keresések ---

    @staticmethod
    def _amount(value: Any, what: str) -> int | None:
        """Egy forintösszeg a felületről („52000”, „52 000 Ft”, „ötvenezer”); üresen None."""
        try:
            amount = parse_price(value)
        except ItemError:
            raise DealError(f"{what}: nem értem ezt az összeget.") from None
        return amount or None

    def _clean(self, fields: dict[str, Any]) -> dict[str, Any]:
        """A keresés beállításai ellenőrizve (a hozzáadáshoz és a módosításhoz)."""
        out: dict[str, Any] = {}
        if "query" in fields:
            out["query"] = " ".join(str(fields["query"] or "").split())[:80]
            if not out["query"]:
                raise DealError("Írd be, mit keressek (pl. „RTX 3060 Ti”).")
        if "sites" in fields:
            sites = fields["sites"] if isinstance(fields["sites"], (list, tuple)) else []
            out["sites"] = [s for s in sites if s in self.platforms] or list(self.platforms)
        for key, what in (("max_price", "A legmagasabb ár"), ("min_price", "A legalacsonyabb ár"),
                          ("min_profit", "A legkisebb haszon"), ("reference", "A szokásos ár")):
            if key in fields:
                out[key] = self._amount(fields[key], what)
        if "discount" in fields:
            try:
                off = int(fields["discount"]) if fields["discount"] not in (None, "") else DEFAULT_DISCOUNT
            except (TypeError, ValueError):
                raise DealError("A kedvezmény egész szám legyen (pl. 30).") from None
            out["discount"] = min(max(off, 10), 80)
        if "every" in fields:
            try:
                minutes = int(fields["every"]) if fields["every"] not in (None, "") else int(self.default_every)
            except (TypeError, ValueError):
                minutes = DEFAULT_EVERY
            out["every"] = min(EVERY_CHOICES, key=lambda choice: abs(choice - minutes))
        if "exclude" in fields:
            out["exclude"] = parse_words(fields["exclude"])
        return out

    def add_search(self, query: str, sites: list[str] | None = None, max_price: Any = None, discount: Any = None,
                   every: Any = None, min_price: Any = None, min_profit: Any = None, exclude: Any = None,
                   reference: Any = None) -> dict[str, Any]:
        """Új keresés. Ha megadod a szokásos árat (reference), azzal keres; különben a következő körben felméri."""
        search = self._clean({"query": query, "sites": sites or list(self.platforms), "max_price": max_price,
                              "discount": discount, "every": every, "min_price": min_price, "min_profit": min_profit,
                              "exclude": exclude or [], "reference": reference})
        self._check_range(search)
        if search.get("reference"):
            now = self.clock()
            search.update(ref_source="kézi", ref_at=now, checked_from=now)
        else:
            search.pop("reference", None)
        with self._lock:
            if len(self._data["searches"]) >= MAX_SEARCHES:
                raise DealError(f"Legfeljebb {MAX_SEARCHES} keresés lehet.")
            self._data["searches"].append({"id": secrets.token_hex(4), **search})
            self._save()
        return self.state()

    @staticmethod
    def _check_range(search: dict[str, Any]) -> None:
        low, high = search.get("min_price"), search.get("max_price")
        if low and high and low > high:
            raise DealError("A legalacsonyabb ár nagyobb, mint a legmagasabb.")

    def update_search(self, search_id: str, **fields: Any) -> dict[str, Any]:
        """Egy keresés beállításainak módosítása. A szokásos árat az accept állítja (vagy a recalibrate törli)."""
        fields.pop("reference", None)
        clean = self._clean(fields)
        with self._lock:
            search = self._search(search_id)
            merged = {**search, **clean}
            self._check_range(merged)
            if clean.get("query") and fold(clean["query"]) != fold(search["query"]):
                for key in ("reference", "ref_source", "ref_at", "checked_from", "basis", "proposal", "market"):
                    merged.pop(key, None)       # más termék: a szokásos árat újra fel kell mérni
            search.clear()
            search.update(merged)
            self._reset_due(search)
            self._save()
        return self.state()

    def accept(self, search_id: str, price: Any = None) -> dict[str, Any]:
        """A szokásos ár: a javaslat elfogadása, vagy (price) kézzel megadva. Innen ezzel keres; az első kör minden
        mostani hirdetést megnéz (nem csak az újakat)."""
        now = self.clock()
        with self._lock:
            search = self._search(search_id)
            amount = self._amount(price, "A szokásos ár") if price not in (None, "") else None
            if amount:
                search.update(reference=amount, ref_source="kézi", ref_at=now, checked_from=now)
                search.pop("basis", None)
            else:
                proposal = search.get("proposal") or {}
                if not proposal.get("price"):
                    raise DealError("Nincs még javaslat: add meg kézzel a szokásos árat.")
                search.update(reference=int(proposal["price"]), ref_source="felmérés", ref_at=now,
                              checked_from=float(proposal.get("at") or now),
                              basis={k: proposal.get(k) for k in ("n", "p25", "p75", "at")})
            search.pop("proposal", None)
            search.pop("market", None)
            self._reset_due(search)
            self._save()
        return self.state()

    def recalibrate(self, search_id: str) -> dict[str, Any]:
        """Újra felméri a szokásos árat (a következő körben); addig ezzel a kereséssel nem keres."""
        with self._lock:
            search = self._search(search_id)
            for key in ("reference", "ref_source", "ref_at", "checked_from", "basis", "proposal", "market"):
                search.pop(key, None)
            self._reset_due(search)
            self._save()
        return self.state()

    def remove_search(self, search_id: str) -> dict[str, Any]:
        with self._lock:
            self._data["searches"] = [s for s in self._data["searches"] if s.get("id") != search_id]
            self._save()
        return self.state()

    def add_watch(self, target: str, min_profit: Any = None, hours: Any = None, every: Any = None) -> dict[str, Any]:
        """Felfedező: egy figyelt oldal (bármelyik oldal egy kategóriája vagy találati oldala, a legújabb elöl; a Vaterán
        a „hamarosan lejár” rendezéssel a licitekhez), vagy egy keresőszó (a Vaterán). Nem kell megadni, mit keresel:
        a hirdetéseket az árbázishoz méri, az ismeretlenekből a modell választ, és megnézi, máshol mennyiért adják."""
        target = " ".join(str(target or "").split())[:300]
        if not target:
            raise DealError("Másold be a figyelendő oldal címét (a böngésző címsorából), vagy írj egy keresőszót.")
        if re.match(r"https?://", target):
            parts = urllib.parse.urlsplit(target)
            host = (parts.hostname or "").removeprefix("www.")
            platform = next((k for k, p in self.platforms.items()
                             if any(host == d or host.endswith("." + d) for d in p.domains)), None)
            if platform is None:
                names = ", ".join(p.name for p in self.platforms.values())
                raise DealError(f"Ezt az oldalt nem ismerem; ezekről másolj be egy találati oldalt: {names}.")
            url = target
            if platform == "vinted" and "order=" not in parts.query:
                url += ("&" if parts.query else "?") + NEWEST["vinted"].lstrip("&")
            segments = [seg for seg in parts.path.split("/") if seg and not seg.startswith("index")
                        and not seg.endswith((".php", ".html"))]
            query = urllib.parse.parse_qs(parts.query)
            label = " ".join((query.get("q") or query.get("search_text") or query.get("stext") or
                              [urllib.parse.unquote(segments[-1]).replace("-", " ") if segments else ""])[0].split())
            name = self.platforms[platform].name + (f": {label[:40]}" if label else "")
        else:
            site = self.platforms.get("vatera")
            if site is None:
                raise DealError("Keresőszóval csak a Vaterán figyelek; máshol másold be az oldal címét.")
            platform, url, name = "vatera", site.search_url.format(q=urllib.parse.quote(target)), target[:80]
        try:
            profit = self._amount(min_profit, "A legkisebb haszon") or WATCH_PROFIT
            window = min(max(int(hours or WATCH_HOURS), 1), 72)
            minutes = min(EVERY_CHOICES, key=lambda choice: abs(choice - int(every or WATCH_EVERY)))
        except (TypeError, ValueError):
            raise DealError("A zárásig hátralévő órák és a gyakoriság egész szám legyen.") from None
        with self._lock:
            if len(self._data["watches"]) >= MAX_WATCHES:
                raise DealError(f"Legfeljebb {MAX_WATCHES} figyelt oldal lehet.")
            self._data["watches"].append({"id": secrets.token_hex(4), "name": name, "url": url, "platform": platform,
                                          "min_profit": profit, "hours": window, "every": minutes})
            self._save()
        return self.state()

    def remove_watch(self, watch_id: str) -> dict[str, Any]:
        with self._lock:
            self._data["watches"] = [w for w in self._data["watches"] if w.get("id") != watch_id]
            self._save()
        return self.state()

    def _search(self, search_id: str) -> dict[str, Any]:
        for search in self._data["searches"]:
            if search.get("id") == search_id:
                return search
        raise DealError("Ez a keresés már nincs meg.")

    def _set(self, search_id: str, **fields: Any) -> None:
        """Egy keresés mezői a kör közben (javaslat, ármozgás); ha közben törölted, nem csinál semmit."""
        with self._lock:
            for search in self._data["searches"]:
                if search.get("id") == search_id:
                    search.update(fields)
                    self._save()

    def _reset_due(self, search: dict[str, Any]) -> None:
        for key in self.platforms:
            self._data["last"].pop(f"{search['id']}:{key}", None)

    # --- találatok ---

    def get(self, deal_id: str) -> dict[str, Any]:
        with self._lock:
            for deal in self._data["deals"]:
                if deal.get("id") == deal_id:
                    return dict(deal)
        raise DealError("Ez a találat már nincs meg.")

    def mark_bought(self, deal_id: str, item_id: str) -> dict[str, Any]:
        """Megvetted: a találatból eladási tárgy lett (item_id); a listában jelölve marad. Öntanulás: az indulóoldala
        és a termék jó pontot kap (ha elrejtetted volna, újra szól róla)."""
        found = None
        with self._lock:
            for deal in self._data["deals"]:
                if deal.get("id") == deal_id:
                    deal["bought"] = item_id
                    found = deal
            if found is not None:
                if found.get("product_key"):
                    feedback = self._data["feedback"].setdefault(found["product_key"], {})
                    feedback["bought"] = int(feedback.get("bought") or 0) + 1
                    feedback.pop("muted", None)
                self._lane_count(found.get("lane"), "bought")
            self._save()
        if found is not None:
            self.journal.add(f"Megvetted: „{found['text'][:60]}” ({format_price(found['price'])}).")
        return self.state()

    def cancel_bought(self, item_id: str, name: str = "") -> dict[str, Any] | None:
        """Visszamondtad a vételt (a tárgyat a hívó törli): a találat, amiből lett, kikerül a találatok közül, a jó pontja
        (a termék, az indulóoldal) visszavonódik, a havi keret maradéka rögtön újraszámolódik, és erről a hirdetésről
        többé nem szól. Visszaadja a találatot (None, ha kézzel felvett vétel volt)."""
        with self._lock:
            found = next((d for d in self._data["deals"] if d.get("bought") == item_id), None)
            if found is not None:
                self._data["deals"].remove(found)
                feedback = self._data["feedback"].get(found.get("product_key") or "")
                if feedback and feedback.get("bought"):
                    feedback["bought"] = int(feedback["bought"]) - 1
                lane = self._data["lanes"].get(found.get("lane") or "")
                if lane and lane.get("bought"):
                    lane["bought"] = int(lane["bought"]) - 1
                cancelled = self._data["cancelled"]
                cancelled[listing_key(found["url"])] = self.clock()
                for key, _at in sorted(cancelled.items(), key=lambda kv: kv[1])[:max(0, len(cancelled) - MAX_SEEN)]:
                    cancelled.pop(key, None)
                self._save()
            self._remaining = (0.0, None)
        what = found["text"][:60] if found is not None else name[:60]
        self.journal.add(f"Visszamondtad a vételt: „{what}”" + (f" ({format_price(found['price'])})" if found else "")
                         + "; töröltem a Vételeim és az eladandók közül" + (", és a találatok közül" if found else "")
                         + ".")
        return found

    def dismiss(self, deal_id: str) -> dict[str, Any]:
        """Elrejtés. Öntanulás: amit MUTE_AFTER-szer elrejtettél (és nem vettél meg), arról MUTE_DAYS napig nem szól."""
        muted = ""
        with self._lock:
            for deal in self._data["deals"]:
                if deal.get("id") != deal_id or deal.get("dismissed"):
                    continue
                deal["dismissed"] = True
                self._lane_count(deal.get("lane"), "dismissed")
                key = deal.get("product_key")
                if key:
                    feedback = self._data["feedback"].setdefault(key, {})
                    feedback["dismissed"] = int(feedback.get("dismissed") or 0) + 1
                    if feedback["dismissed"] >= MUTE_AFTER and not feedback.get("bought") and not feedback.get("muted"):
                        feedback["muted"] = self.clock()
                        muted = self.book.name(key)
            self._save()
        if muted:
            self.journal.add(f"Az ilyen találatokat („{muted}”) {MUTE_AFTER}-szer elrejtetted: {MUTE_DAYS} napig nem "
                             "szólok róluk.")
        return self.state()

    def _muted(self, key: str | None, now: float) -> bool:
        if not key:
            return False
        with self._lock:
            since = float((self._data["feedback"].get(key) or {}).get("muted") or 0)
        return bool(since) and now - since < MUTE_DAYS * 86400

    def _lane_count(self, lane_id: str | None, field: str, amount: int = 1) -> None:
        """Egy indulóoldal eredménye (oldalak, hirdetések, jelöltek, jó találatok, megvett, elrejtett): ebből tanul,
        melyiket nézze gyakrabban. A mentést a hívó végzi."""
        if not lane_id:
            return
        with self._lock:
            stats = self._data["lanes"].setdefault(lane_id, {})
            stats[field] = int(stats.get(field) or 0) + amount

    # --- a keresés (háttérmunka) ---

    def every_minutes(self) -> float:
        """A háttérmunka gyakorisága: a leggyakoribb keresésé (a többi csak akkor fut, ha esedékes, lásd _due)."""
        with self._lock:
            jobs = [int(s.get("every") or DEFAULT_EVERY) for s in self._data["searches"]]
            jobs += [int(w.get("every") or WATCH_EVERY) for w in self._data["watches"]]
            if not self._data["enabled"] or not (jobs or self._data["browse"]["on"]):
                return 0.0
            # böngészés közben percenként megnézi, van-e dolga (csak akkor fut, ha van: lásd available)
            return 1.0 if self._data["browse"]["on"] else float(min(jobs))

    def _due(self, search: dict[str, Any], key: str, now: float) -> bool:
        every = max(int(search.get("every") or DEFAULT_EVERY), SITE_MIN_MINUTES.get(key, 0))
        last = float(self._data["last"].get(f"{search['id']}:{key}") or 0)
        return now - last >= every * 60 - 90     # másfél perc ráhagyás: az ütemező percre pontosan nem indít

    def run(self, check: Callable[[], None] = lambda: None,
            notify: Callable[..., None] = lambda *a, **k: None, force: bool = False) -> str:
        """Egy kör (a modellhívásai háttérhívások: a feltöltés és a beszélgetés előbb jön); lásd _run."""
        with background_calls():
            return self._run(check, notify, force)

    def _run(self, check: Callable[[], None], notify: Callable[..., None], force: bool) -> str:
        """Egy kör: az esedékes keresések (force: mind) oldalain az első találati oldal, a legújabb elöl, modell
        nélkül. Ha egy keresésnek még nincs szokásos ára, felméri (egy modellhívás), különben ahhoz méri a
        hirdetéseket. A Vatera-licitfigyelők a hamarosan záruló liciteket nézik. A jelöltek oldalát megnyitja (csak
        olvasva), és a megmaradtakra egy rövid modellkérdés. A modellhívások a modell-sorban mennek (ModelLane),
        egyenként, miközben a böngésző a következő oldalt tölti. Ha egyik sem esedékes, és be van kapcsolva a
        napközbeni böngészés, annak egy lépése jön (_step_browse). Visszaadja, mit talált (és a naplóba is beírja)."""
        with self._lock:
            if not self._data["enabled"]:
                return "Az automata kereskedés ki van kapcsolva."
            searches = json.loads(json.dumps(self._data["searches"]))
            watches = json.loads(json.dumps(self._data["watches"]))
        if self.browser is None:
            return "Az automata kereskedéshez Jarvis saját böngészője kell."
        now = self.clock()
        game = _Round(check, notify, ModelLane() if self.llm is not None else None)
        names: list[str] = []
        for search in searches:
            sites = [key for key in search.get("sites") or list(self.platforms) if key in self.platforms
                     and (force or self._due(search, key, now))]
            if not sites:
                continue
            status, proposal = search_status(search), search.get("proposal") or {}
            if status == "javaslat":           # jóváhagyásig ezzel nem keres (az oldalakat sem tölti be)
                game.notes.append(f"„{search['query']}”: a szokásos ár ({format_price(proposal['price'])}) "
                                  "jóváhagyásra vár")
                continue
            if status == "kevés" and now - float(proposal.get("at") or 0) < CALIBRATE_RETRY:
                game.notes.append(f"„{search['query']}”: kevés hasonló hirdetés volt, add meg kézzel a szokásos árat")
                continue
            game.done += 1
            names.append(f"„{search['query']}”")
            game.work.append(partial(self._step_search, game, search, sites, status))
        running = [s for s in searches if search_status(s) == "fut"]
        for watch in watches:
            platform = watch.get("platform") or "vatera"
            if platform in self.platforms and (force or self._due(watch, platform, now)):
                game.done += 1
                names.append(watch["name"])
                game.work.append(partial(self._step_watch, game, watch, platform, running))
        browse = False
        if game.done:
            game.work.append(partial(self._step_revisit, game))
        else:                                 # „Keresés most” is: ha nincs keresés, a böngészés lép
            with self._lock:
                browse = self._browse_due(now)
            if browse:
                game.work.append(partial(self._step_browse, game, running))
        if self.fetcher is not None and game.done:
            # a Hardverapró és a Jófogás találati oldalai rögtön, a háttérben (oldalanként egyszerre egy, szünetekkel),
            # amíg a böngésző a Vintedet és a Vaterát nézi
            game.pool = ThreadPoolExecutor(max_workers=max(1, len(getattr(self.fetcher, "sites", ()) or (1,))),
                                           thread_name_prefix="jarvis-gyors")
            for url, platform in self._fast_loads(searches, watches, now, force):
                game.fetch_ahead(self.fetcher, url, platform)
        try:
            game.loop()
        finally:
            if game.lane is not None:
                game.lane.close()
            if game.pool is not None:
                game.pool.shutdown(wait=False, cancel_futures=True)
            self.book.save()
        if browse:
            return self._after_browse(game)
        extra = [n for n in game.notes if n] + game.errors
        if not game.done:
            return "Most egyik keresés sem esedékes" + (f" ({'; '.join(extra[:4])})" if extra else "") + "."
        text = (f"{game.done} keresés: {game.found} olcsó hirdetés" if game.found
                else f"{game.done} keresés: nincs új olcsó hirdetés")
        if extra:
            text += f" ({'; '.join(extra[:4])})"
        self.journal.add(f"{', '.join(names)[:200]}: " + (f"{game.found} olcsó hirdetés" if game.found
                                                          else "nincs új olcsó hirdetés") + ".")
        return text + "."

    def _search_url(self, search: dict[str, Any], key: str) -> str:
        return self.platforms[key].search_url.format(q=urllib.parse.quote(search["query"])) + NEWEST.get(key, "")

    def _fast_loads(self, searches: list[dict[str, Any]], watches: list[dict[str, Any]], now: float,
                    force: bool) -> list[tuple[str, str]]:
        """A kör gyors (böngésző nélkül olvasható) oldalai: az esedékes, futó keresések és figyelt oldalak címe."""
        out = []
        for search in searches:
            if self._search_waits(search, now):
                continue
            for key in search.get("sites") or list(self.platforms):
                if key in self.platforms and self.fetcher.handles(key) and (force or self._due(search, key, now)):
                    out.append((self._search_url(search, key), key))
        for watch in watches:
            platform = watch.get("platform") or "vatera"
            if platform in self.platforms and self.fetcher.handles(platform) and (force or self._due(watch, platform,
                                                                                                     now)):
                out.append((watch["url"], platform))
        return out

    def _listings(self, url: str, platform: str, check: Callable[[], None],
                  game: _Round | None = None) -> list[dict[str, Any]]:
        """Egy találati oldal hirdetései: a Hardverapróról és a Jófogásról böngésző nélkül (ha a kör elején már
        letöltötte, onnan), különben, vagy ha így nem sikerült (tilt, üres), a böngészővel. BrowserError, ha egyik sem."""
        future = game.prefetch.pop(url, None) if game is not None else None
        if future is not None or (self.fetcher is not None and self.fetcher.usable(platform)):
            try:
                rows = future.result() if future is not None else self.fetcher.listings(url, platform, check)
            except FeedError:
                rows = None
            if rows:
                if game is not None:
                    game.fast += 1
                return rows
        if game is not None and game.no_browser:
            raise BrowserError("böngésző nélkül most nem olvasható (a gyors sor a böngészőhöz nem nyúl)")
        return self.browser().extract_listings(url)

    def _platform_of(self, url: str) -> str:
        host = (urllib.parse.urlsplit(url).hostname or "").removeprefix("www.")
        return next((k for k, p in self.platforms.items() if any(host == d or host.endswith("." + d)
                                                                 for d in p.domains)), "")

    def _look(self, url: str, platform: str = "", check: Callable[[], None] = lambda: None,
              game: _Round | None = None) -> dict[str, Any]:
        """Egy hirdetés oldala csak olvasva: a gyors oldalakon böngésző nélkül, különben (vagy ha így nem megy) a
        böngészővel (a gyors sorban soha). BrowserError, ha nem sikerült."""
        platform = platform or self._platform_of(url)
        if self.fetcher is not None and self.fetcher.usable(platform):
            try:
                return self.fetcher.page(url, platform, check)
            except FeedError:
                pass
        if game is not None and game.no_browser:
            raise BrowserError("böngésző nélkül most nem olvasható (a gyors sor a böngészőhöz nem nyúl)")
        return self.browser().look(url)

    def _step_search(self, game: _Round, search: dict[str, Any], sites: list[str], status: str) -> None:
        """Egy keresés: a találati oldalak, majd a szokásos árhoz mért jelöltek, vagy (ha még nincs) a felmérés."""
        rows, problems = self._collect(search, sites, game.check, game=game)
        game.errors += problems
        if not rows:
            return
        kept = self._relevant(search, rows)
        if status == "fut":
            game.next([partial(self._candidate, game, search, deal) for deal in self._hunt(search, kept)])
            self._drift(search, kept, game.notify)
            return
        rows = kept[:CALIBRATE_ROWS]
        if rows:
            game.ask(partial(self._judge, search, rows), partial(self._calibrated, game, search, rows))
        else:
            self._calibrated(game, search, rows, None)

    def _deal_text(self, search: dict[str, Any], deal: dict[str, Any]) -> str:
        warn = "".join(f" Figyelj: {w}." for w in deal.get("warnings") or [])
        if deal.get("speed"):
            warn = f" Eladási sebesség: {deal['speed']}.{warn}"
        if deal.get("safety"):
            warn = f" {safety_text(deal['safety'])}{warn}"
        origin = (f"Böngészés közben ({deal['explore']})." if deal.get("lane") else
                  f"Figyelt oldal: {deal['explore']}." if deal.get("explore") else f"Keresés: „{search['query']}”.")
        title = f"„{deal['text'][:80]}”"
        if deal.get("auction"):
            bids = f", {deal['bids']} licit" if deal.get("bids") is not None else ""
            return (f"Vatera-licit: {title}, {left_text(deal.get('left'))} múlva zárul, most {format_price(deal['price'])}"
                    f"{bids}. Máshol kb. {format_price(deal['median'])}-ért adják. Legfeljebb "
                    f"{format_price(deal['max_bid'])}-ig érdemes licitálni (akkor is kb. "
                    f"{format_price(expected_profit(deal['median'], deal['max_bid'], deal.get('low')))} a haszon). "
                    "Licitálni nem fogok "
                    f"helyetted.{warn}" + (f" {origin}" if deal.get("explore") else ""))
        if deal.get("offer"):
            return (f"Vinted: {title} {format_price(deal['price'])}, a szokásos ára {format_price(deal['median'])}. "
                    f"Ha {format_price(deal['offer'])}-ért megkapod (ajánlattétel), kb. {format_price(deal['profit'])} "
                    f"a haszon. Ajánlatot tegyek? Csak a jóváhagyásod után küldöm el. {origin}{warn}")
        drop = (f" (leárazta: {format_price(deal['drop'])} → {format_price(deal['price'])})"
                if deal.get("drop") else "")
        when = " A felmérés közben találtam." if deal.get("calibration") else ""
        where = f", {' és '.join(deal['sites'])}" if deal.get("sites") else ""
        if deal.get("sold"):
            where += f"; az elkeltek kb. {format_price(deal['sold']['median'])}"
        usual = (f"Máshol kb. {format_price(deal['median'])}-ért adják ({deal['similar']} hasonló hirdetés alapján"
                 f"{where})" if deal.get("explore") and deal.get("similar") else
                 f"A szokásos ára {format_price(deal['median'])}")
        return (f"{deal['site']}: {title} {format_price(deal['price'])}{drop}. {usual}, a várható haszon kb. "
                f"{format_price(deal['profit'])}. {origin}{when}{warn}")

    def _collect(self, search: dict[str, Any], sites: list[str], check: Callable[[], None],
                 track: bool = True, game: _Round | None = None) -> tuple[list[dict[str, Any]], list[str]]:
        rows, errors = [], []
        urls = {key: self._search_url(search, key) for key in sites}
        if game is not None and game.pool is not None:
            # a gyors oldalak a háttérben töltődnek, amíg a böngésző a többit nézi: azok jönnek előbb
            for key in sites:
                game.fetch_ahead(self.fetcher, urls[key], key)
            sites = sorted(sites, key=lambda key: urls[key] in game.prefetch)
        for key in sites:
            site = self.platforms[key]
            check()
            if track:
                with self._lock:
                    self._data["last"][f"{search['id']}:{key}"] = self.clock()
            try:
                listings = self._listings(urls[key], key, check, game)
            except BrowserError as exc:
                errors.append(f"{site.name}: {str(exc).splitlines()[0][:120]}")
                continue
            if not listings:
                errors.append(f"{site.name}: nem találtam árat a találati oldalon (nincs találat, ellenőrzést kér, "
                              "vagy megváltozott az oldal)")
            found = [{"site": site.name, "platform": key, "text": str(entry.get("text") or "")[:180],
                      "price": int(entry["price"]), "url": str(entry.get("url") or ""),
                      **({"title": str(entry["title"])[:160]} if entry.get("title") else {})}
                     for entry in listings[:MAX_PER_SITE] if not entry.get("auction")]   # a licit nem eladási ár
            self.book.add(found)
            rows += found
        return rows, errors

    def _relevant(self, search: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Modell nélkül, ami biztosan nem a keresett termék, kiesik: kereső-, csere- és csomaghirdetés, alkatrész
        (selling.prefilter), ha a keresés valamelyik szava hiányzik, kiegészítő (matches_query), egy kizáró szó van
        benne, vagy a minimum ár alatt van. Ugyanaz a hirdetés (webcím) egyszer."""
        probe = Item(id="kereses", created=0.0, updated=0.0, title=search["query"], search_query=search["query"])
        # az azonos című, hasonló árú hirdetések itt nem ismétlések (egy népszerű termékből sok van): a webcím dönt
        kept, _dropped = prefilter(probe, rows, limit=len(rows) or 1, dedupe=False)
        unique: dict[str, dict[str, Any]] = {}
        for row in kept:
            unique.setdefault(listing_key(row["url"]) if row["url"] else row["text"], row)
        words, low = search.get("exclude") or [], search.get("min_price")
        return [row for row in unique.values() if matches_query(search["query"], row["text"])
                and not excluded(words, row["text"]) and not (low and row["price"] < low)]

    @staticmethod
    def _cheap(search: dict[str, Any], price: int, reference: int) -> bool:
        return pays(search, price, reference)

    def _deal(self, search: dict[str, Any], row: dict[str, Any], reference: int, **extra: Any) -> dict[str, Any]:
        """Egy jelölt. Az árbázisból hozzáteszi, milyen gyorsan kel el az ilyen (ha tudjuk), és a termékkulcsát (a
        gyakran hamisított márkákhoz, _suspect)."""
        deal = {**row, "id": secrets.token_hex(5), "search": search["id"], "query": search["query"],
                "median": reference, "margin": reference - row["price"],
                "profit": expected_profit(reference, row["price"], extra.get("low")), "at": self.clock(),
                **{k: v for k, v in extra.items() if v is not None and v is not False}}
        found = self.book.key(str(extra.get("product") or "")) or self.book.key(row.get("title") or row["text"]) or \
            self.book.key(search["query"])
        if found:
            deal["product_key"] = found[0]
            speed = self.book.speed(found[0])
            deal.update(speed=speed_text(speed), pace=pace(speed))
        if "spread" not in deal and search.get("basis"):     # a felmért keresésnél a felméréskori negyedek
            deal["spread"] = spread_of({**search["basis"], "price": reference})
        deal.pop("product", None)
        return {k: v for k, v in deal.items() if v not in ("", None)}

    @staticmethod
    def _suspect(deal: dict[str, Any]) -> str:
        """Modell és oldalbetöltés nélkül: gyanúsan olcsó-e (miért; „” = nem). A licit mostani ára nem ilyen."""
        reference, price = int(deal.get("median") or 0), int(deal["price"])
        if deal.get("auction") or reference <= 0:
            return ""
        if price < reference * SUSPECT_RATIO:
            return (f"gyanúsan olcsó: a szokásos ára (kb. {format_price(reference)}) {round(price * 100 / reference)}%-a "
                    "(átverés, hamisítvány vagy hibás szokott lenni)")
        if fake_prone(str(deal.get("product_key") or "")) and price < reference * FAKE_RATIO:
            return "ezt a márkát gyakran hamisítják, és feltűnően olcsó"
        return ""

    def _suspicious(self, deal: dict[str, Any]) -> None:
        """Gyanús hirdetés: nem szól róla, és a találatok közé sem kerül; látottnak jegyzi, és a naplóba írja, miért
        hagyta ki."""
        self._mark(deal)
        self.journal.add(f"Gyanús, nem szóltam róla: „{(deal.get('title') or deal['text'])[:60]}” "
                         f"{format_price(deal['price'])} "
                         f"({deal.get('site') or deal.get('platform') or '?'}) – {deal.get('suspect') or 'gyanús'}.")

    def _risky(self, deal: dict[str, Any]) -> bool:
        """Nyugodt szívvel megvehető-e (assess; a találatba is beírja). Ha kockázatos, és csak a nyugodt szívvel
        megvehetőkről szól (safe_only), látottnak jegyzi, a naplóba írja, miért hagyta ki, és igazat ad."""
        deal["safety"] = assess(deal)
        with self._lock:
            safe_only = self._data["safe_only"]
        if deal["safety"]["level"] != "kockázatos" or not safe_only:
            return False
        self._mark(deal)
        self.journal.add(f"Kockázatos, nem szóltam: „{(deal.get('title') or deal['text'])[:60]}” "
                         f"{format_price(deal['price'])} ({deal.get('site') or deal.get('platform') or '?'}) – "
                         f"{'; '.join(deal['safety']['why'])}.")
        return True

    def _deal_keys(self) -> set[str]:
        """A hirdetések, amelyekről már szóltunk (vagy visszamondtad a vételt): ezekről többé nem szól."""
        return {listing_key(d["url"]) for d in self._data["deals"] if d.get("url")} | set(self._data["cancelled"])

    def _hunt(self, search: dict[str, Any], kept: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Modell nélkül, a jóváhagyott szokásos árhoz mérve: a még nem látott (vagy azóta leárazott) hirdetések
        közül, ami megéri. A többit látottnak jegyzi, az árukkal (a leárazás észrevételéhez)."""
        reference = int(search["reference"])
        since = float(search.get("checked_from") or 0)
        now = self.clock()
        out = []
        with self._lock:
            seen, prices, deals = self._data["seen"], self._data["prices"], self._deal_keys()
            cancelled = self._data["cancelled"]    # visszamondott vétel: leárazva sem szól róla
            for row in kept:
                key = listing_key(row["url"]) if row["url"] else ""
                if not key:
                    continue
                old = prices.get(key)
                new = key not in seen or seen[key] < since
                drop = old is not None and row["price"] <= old * (1 - DROP)
                if not (new or drop) or (key in deals and not drop) or key in cancelled:
                    prices[key] = row["price"]
                    continue
                offer = offer_price(search, row["price"], reference) if row.get("platform") == "vinted" else None
                if offer and not self._affordable(offer):
                    offer = None               # a vételi limitbe az ajánlat sem fér bele
                if self._cheap(search, row["price"], reference) and self._affordable(row["price"]):
                    out.append(self._deal(search, row, reference, drop=old if drop else None))
                elif offer:            # kicsit drága: a Vinteden ajánlatot tehetsz (csak a jóváhagyásoddal)
                    out.append(self._deal(search, row, reference, offer=offer, profit=expected_profit(reference, offer)))
                elif self._cheap(search, row["price"], reference):
                    prices[key] = row["price"]    # olcsó, de a vételi limit fölött: ha emeled, újra jelölt lehet
                else:
                    seen[key], prices[key] = now, row["price"]
            self._prune()
            self._save()
        return sorted(out, key=lambda d: -d["profit"])

    def _calibrated(self, game: _Round, search: dict[str, Any], rows: list[dict[str, Any]],
                    picked: list[dict[str, Any]] | None) -> None:
        """A szokásos ár felmérése (keresésenként egyszer), a modell választása (picked) után: a kiválasztott valódi,
        működő, egy darabos termékek középértéke a javaslat. Erről szól; jóváhagyásig ezzel nem keres. Ha a
        kiválasztottak között jóval olcsóbb is van, az jelölt (felmérés közben talált). Modell nélkül (vagy ha nem
        válaszolt) az előszűrt találatokból számol."""
        now = self.clock()
        query = search["query"]
        earlier = search.get("proposal") or {}     # egy korábbi, kevés hasonlóval végződött felmérés
        if picked is None:
            picked = rows
        stats = price_stats([r["price"] for r in picked])
        if not stats or stats["n"] < CALIBRATE_MIN:
            self._set(search["id"], proposal={"price": None, "n": len(picked), "at": now})
            if not earlier:                   # egyszer szól, nem minden próbálkozáskor
                game.notify("deal", f"Szokásos ár kell: {query}",
                            f"„{query}”: kevés hasonló hirdetést találtam ({len(picked)}), ebből nem mondok szokásos "
                            "árat. Add meg kézzel az Automata kereskedés fülön, vagy adj meg gyakoribb terméket.",
                            actions=[DEALS_ACTION])
            game.notes.append(f"„{query}”: kevés hasonló hirdetés ({len(picked)}), add meg kézzel a szokásos árat")
            return
        price = round_price(stats["median"])
        proposal = {"price": price, "p25": stats["p25"], "p75": stats["p75"], "n": stats["n"], "at": now,
                    "rows": [{"text": r["text"][:120], "price": r["price"], "url": r["url"], "site": r["site"]}
                             for r in sorted(picked, key=lambda r: r["price"])[:15]]}
        self._set(search["id"], proposal=proposal)
        search["proposal"] = proposal
        game.notify("deal", f"Szokásos ár: {query}",
                    f"„{query}”: szerintem most kb. {format_price(price)} ({stats['n']} hasonló hirdetés alapján, a "
                    f"többség {format_price(stats['p25'])}–{format_price(stats['p75'])}). Ha jó, fogadd el az Automata "
                    "kereskedés fülön, vagy írd át; addig ezzel nem keresek.", actions=[DEALS_ACTION])
        game.notes.append(f"„{query}”: szokásos ár javaslat {format_price(price)}, jóváhagyásra vár")
        with self._lock:
            deals = self._deal_keys()
        candidates = [self._deal(search, row, price, calibration=True) for row in picked
                      if row["url"] and listing_key(row["url"]) not in deals and self._cheap(search, row["price"], price)
                      and self._affordable(row["price"])]
        game.next([partial(self._candidate, game, search, deal)
                   for deal in sorted(candidates, key=lambda d: -d["profit"])])

    def _judge(self, search: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        """Egy modellhívás: a találatok közül melyik pontosan a keresett termék. None: a modell nem válaszolt."""
        listing = "\n".join(f"{n}. [{r['site']}] {r['text']} – {format_price(r['price'])}"
                            for n, r in enumerate(rows, 1))
        messages = [{"role": "system", "content": JUDGE_SYSTEM},
                    {"role": "user", "content": f"Keresett termék: {search['query']}\n\nTalálatok:\n{listing}"}]
        try:
            data = self.llm.chat_json(messages, JUDGE_SCHEMA, temperature=0.0, max_tokens=300)
        except Exception:  # noqa: BLE001 - a modell hibája ne állítsa le a keresést
            return None
        picked: list[dict[str, Any]] = []
        for number in data.get("similar") or []:
            try:
                index = int(number) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(rows) and rows[index] not in picked:
                picked.append(rows[index])
        return picked

    def _drift(self, search: dict[str, Any], kept: list[dict[str, Any]], notify: Callable[..., None]) -> None:
        """Ármozgás: ha a hirdetésekben napok óta jóval más az ár, mint a jóváhagyott szokásos ár, egyszer szól."""
        stats = price_stats([r["price"] for r in kept])
        if not stats or stats["n"] < MIN_PRICES:
            return
        now, median, reference = self.clock(), stats["median"], int(search["reference"])
        market = dict(search.get("market") or {})
        off = abs(median - reference) > DRIFT * reference
        since = float(market.get("since") or now) if off else None
        market.update(median=median, at=now, since=since)
        if not off:
            market.pop("noted", None)
        elif now - since >= DRIFT_DAYS * 86400 and not market.get("noted"):
            notify("deal", f"Változott az ár? {search['query']}",
                   f"„{search['query']}”: szokásos árnak {format_price(reference)}-ot fogadtál el, de a hirdetésekben "
                   f"{DRIFT_DAYS} napja inkább {format_price(median)} körül van. Ha szeretnéd, mérd fel újra, vagy írd át "
                   "az Automata kereskedés fülön.", actions=[DEALS_ACTION], quiet=True)
            market["noted"] = now
        self._set(search["id"], market=market)

    def _candidate(self, game: _Round, search: dict[str, Any], deal: dict[str, Any],
                   page: dict[str, Any] | None = None) -> None:
        """Egy jelölt: a hirdetés oldala (modell nélkül, a böngésző szálán; ha már megnyitottuk, page), majd egy rövid
        modellkérdés a modell-sorban. Ha a kör keretébe már nem fér bele, nem jegyzi látottnak: a következő körben
        újra jelölt."""
        self._lane_count(deal.get("lane"), "candidates")
        if why := self._suspect(deal):
            deal["suspect"] = why
            self._suspicious(deal)             # gyanúsan olcsó: az oldalát sem nyitja meg
            return
        if self._risky(deal):
            return
        if game.blocked and page is None:
            deal.setdefault("warnings", []).append("a hirdetés oldalát nem néztem meg (robot-ellenőrzés)")
        else:
            if page is None:
                if game.pages <= 0:
                    return
                game.pages -= 1
                game.loaded += 1
            verdict = self._inspect(deal, page, game)
            if verdict == "captcha":
                game.blocked = True            # ebben a körben több hirdetést nem nyit meg
            elif verdict == "gyanús":
                self._suspicious(deal)
                return
            elif verdict:
                self._mark(deal)               # elkelt vagy hibás: nem szól róla
                return
        if game.lane is None:
            self._conclude(game, search, deal, None)
            return
        if game.checks <= 0:
            return
        game.checks -= 1
        game.ask(partial(self._confirm, search, deal), partial(self._conclude, game, search, deal))

    def _conclude(self, game: _Round, search: dict[str, Any], deal: dict[str, Any], ok: bool | None) -> None:
        """A modell válasza után (a böngésző szálán): ha nem mondott nemet, szól róla (kivéve, ha az ilyen terméket
        többször elrejtetted: arról egy ideig nem szól). A naplóba is beírja."""
        if ok is False:
            if deal.get("suspect"):
                self._suspicious(deal)
            else:
                self._mark(deal)
            return
        self._mark(deal)
        if self._muted(deal.get("product_key"), self.clock()):
            game.notes.append(f"„{deal['text'][:40]}”: az ilyet elrejtetted, nem szóltam")
            return
        game.found += 1
        self._lane_count(deal.get("lane"), "deals")
        self._remember(deal)
        hit = (f"„{deal['text'][:60]}” {format_price(deal['price'])} ({deal['site']}"
               + (f", licit, legfeljebb {format_price(deal['max_bid'])}" if deal.get("auction") else
                  f", kb. {format_price(deal['profit'])} haszon") + ")")
        game.hits.append(hit)
        self.journal.add(f"Jó találat: {hit}" + (f" – {deal['explore']}" if deal.get("explore") else
                                                f" – keresés: {search['query']}"))
        actions = [deal_action(deal["id"])]
        if deal.get("offer"):
            actions.insert(0, {"action": "offer", "deal": deal["id"],
                               "label": f"Ajánlatot teszek ({format_price(deal['offer'])})"})
        title = f"Licit: {deal['text'][:60]}" if deal.get("auction") else f"Olcsó: {deal['text'][:60]}"
        game.notify("deal", title, self._deal_text(search, deal), url=deal["url"], actions=actions, deal=deal["id"],
                    urgent=True)

    # --- Felfedező: figyelt oldalak, keresőszó nélkül (és a Vatera-licitek) ---

    def _step_watch(self, game: _Round, watch: dict[str, Any], platform: str, running: list[dict[str, Any]]) -> None:
        """Egy figyelt oldal friss hirdetései (_page_rows, _triage); az ismeretlenekből a modell egy hívásban megnevezi a
        termékeket, és kiválasztja, ami megérhet (_explore)."""
        game.check()
        with self._lock:
            self._data["last"][f"{watch['id']}:{platform}"] = self.clock()
        rows = self._page_rows(game, watch["url"], platform, watch)
        if not rows:
            return
        # a licitek elöl (azoknál sürget az idő), aztán a fix árasak, ahogy az oldalon jönnek (a legújabb elöl)
        batch = sorted(self._triage(game, watch, rows, running), key=lambda r: not r.get("auction"))[:EXPLORE_ROWS]
        if batch:
            game.ask(partial(self._explore, batch), partial(self._explored, game, watch, batch))

    def _page_rows(self, game: _Round, url: str, platform: str, source: dict[str, Any],
                   extra: dict[str, Any] | None = None) -> list[dict[str, Any]] | None:
        """Egy találati oldal hirdetései (modell nélkül, csak olvasva). A fix árasak árai az árbázisba kerülnek; a
        licitek közül csak a hamarosan záruló (a gép esti leállása előttiek is) marad. None, ha nem sikerült (ha
        robot-ellenőrzést kér, game.captcha jelzi)."""
        site = self.platforms[platform]
        browser = self.browser()
        try:
            listings = self._listings(url, platform, game.check, game)
        except BrowserError as exc:
            game.errors.append(f"{site.name}: {str(exc).splitlines()[0][:120]}")
            return None
        game.loaded += 1
        if not listings:
            try:
                blocked = bool(getattr(browser, "challenged", lambda: False)())
            except BrowserError:
                blocked = False
            if blocked:
                game.captcha = platform
                game.errors.append(f"{site.name}: robot-ellenőrzést kér")
                return None
            label = source["name"] if source["name"].startswith(site.name) else f"{site.name} ({source['name']})"
            game.errors.append(f"{label}: nem találtam hirdetést az oldalon (ellenőrzést kér, vagy megváltozott)")
            return None
        now = self.clock()
        window = int(source.get("hours") or WATCH_HOURS) * 3600
        with self._lock:
            day = tuple(self._data["day"])
        rows: list[dict[str, Any]] = []
        for entry in listings[:MAX_PER_SITE * 2]:
            if not entry.get("url"):
                continue
            row = {"site": site.name, "platform": platform, "text": str(entry.get("text") or "")[:180],
                   "price": int(entry["price"]), "url": str(entry["url"]),
                   **({"title": str(entry["title"])[:160]} if entry.get("title") else {}), **(extra or {})}
            if entry.get("auction"):
                info = parse_auction(row["text"])
                if info["left"] is not None and last_chance(now + info["left"], day) - now > window:
                    continue                   # még messze a zárás: egy későbbi körben
                row.update(auction=True, **info)
            rows.append(row)
        self.book.add(rows)                    # a licitet kihagyja (az nem eladási ár)
        return rows

    def _triage(self, game: _Round, source: dict[str, Any], rows: list[dict[str, Any]],
                running: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Modell nélkül: ami egy felmért keresésedre illik, azt annak a szokásos árához méri; ami az árbázisból
        felismerhető, azt az árbázishoz; ha annak az ára nem elég biztos (sure_price), előbb a többi oldalon is
        megnézi (_resale). A jelöltek (_candidate) rögtön jönnek; visszaadja az ismeretleneket (ezeket a modell
        nevezheti meg). A már látottak (ha nem árazták le) és a nem eladási árak (kereső, csomag, alkatrész…)
        kimaradnak."""
        unknown, found, skip, recheck = [], [], [], []
        with self._lock:
            seen, prices, deals = dict(self._data["seen"]), dict(self._data["prices"]), self._deal_keys()
            cancelled = set(self._data["cancelled"])
        for row in rows:
            key = listing_key(row["url"])
            old = prices.get(key)
            drop = old is not None and not row.get("auction") and row["price"] <= old * (1 - DROP)
            if (key in seen and not drop) or (key in deals and not drop) or key in cancelled:
                continue
            if not usable(row["text"]):
                skip.append(row)               # nem eladási ár (kereső, csere, csomag, alkatrész…)
                continue
            if not self._within_reach(row):
                continue                       # a vételi limit fölött: nem jegyzi látottnak (ha emeled, sorra kerül)
            search = next((s for s in running if matches_query(s["query"], row["text"])
                           and not excluded(s.get("exclude") or [], row["text"])), None)
            label = row.get("lane_name") or source["name"]
            if search is not None:             # egy keresésedre illik: annak a szokásos ára dönt
                reference = int(search["reference"])
                top = highest_ok(search, reference)
                if top:
                    top = self._capped(top)
                if row.get("auction") and top and row["price"] < top:
                    found.append((search, self._deal(search, row, reference, max_bid=top, explore=label)))
                elif not row.get("auction") and pays(search, row["price"], reference) and self._affordable(row["price"]):
                    found.append((search, self._deal(search, row, reference, explore=label,
                                                     drop=old if drop else None)))
                else:
                    skip.append(row)
                continue
            product = self.book.key(row.get("title") or row["text"])   # a cím (a kártyán a város, az eladó is ott van)
            reference = self.book.reference(product[0], exclude=row["url"]) if product else None
            if reference is None:
                unknown.append(row)
                continue
            deal = self._explore_deal(source, row, reference, drop=old if drop else None)
            if deal is None:
                skip.append(row)
            elif not sure_price(reference):
                if game.resale > 0:            # a keretbe már nem férő a következő körben újra jön
                    game.resale -= 1
                    query = self.book.query(row.get("title") or row["text"]) or reference["name"]
                    recheck.append(partial(self._resale, game, source, row, query))
            else:
                found.append(({"id": source["id"], "query": reference["name"]}, deal))
        self._mark(*skip)
        game.next([partial(self._candidate, game, search, deal) for search, deal in found] + recheck)
        return unknown

    def _explore_deal(self, watch: dict[str, Any], row: dict[str, Any], reference: dict[str, Any],
                      **extra: Any) -> dict[str, Any] | None:
        """Megéri-e egy figyelt oldal hirdetése a szokásos árhoz (reference: az árbázisból vagy máshonnan) mérve
        (explore_top). A licitnél a legmagasabb ajánlatot adja meg (max_bid), a kicsit drága Vinted-hirdetésnél
        ajánlattételt javasol. None, ha nem éri meg."""
        price, usual, low = int(row["price"]), int(reference["price"]), careful_low(reference)
        top = explore_top(usual, int(watch.get("min_profit") or WATCH_PROFIT), low)
        probe = {"id": watch["id"], "query": reference.get("name") or row["text"][:60]}
        info = {"explore": row.get("lane_name") or watch["name"], "similar": reference.get("n"),
                "product": reference.get("name"), "low": low, "sites": self._site_names(reference.get("sites")) or None,
                "spread": spread_of(reference), "sold": reference.get("sold"), **extra}
        if row.get("auction"):
            top = self._capped(top)            # a vételi limitnél tovább nem érdemes licitálni
            return self._deal(probe, row, usual, max_bid=top, **info) if 0 < top and price < top else None
        if price < usual * FLOOR or top <= 0:
            return None
        if price <= top and self._affordable(price):
            return self._deal(probe, row, usual, **info)
        offer = self._capped(top) // 100 * 100
        if row.get("platform") == "vinted" and 0 < offer < price and offer >= price * (1 - OFFER_CUT):
            return self._deal(probe, row, usual, offer=offer, profit=expected_profit(usual, offer, low), **info)
        return None

    def _site_names(self, keys: list[str] | None) -> list[str]:
        return [self.platforms[k].name if k in self.platforms else k for k in keys or []]

    def _market_of(self, row: dict[str, Any], query: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Mennyiért adják biztosan: az árbázis friss árai (ha a terméket ismeri) és a most talált hasonlók (rows: ami a
        keresőszóra illik), hirdetésenként egyszer, a jelölt nélkül (market)."""
        product = self.book.key(query)
        known = self.book.samples(product[0], exclude=row["url"]) if product else []
        merged = {listing_key(s["url"]): (s["p"], s["site"], s["gone"]) for s in known if s["url"]}
        merged.update({listing_key(r["url"]): (int(r["price"]), str(r.get("platform") or ""))
                       for r in self._relevant({"id": "ellenorzes", "query": query}, rows) if r.get("url")})
        merged.pop(listing_key(row["url"]), None)
        return market(list(merged.values()))

    def _unsure(self, row: dict[str, Any], query: str, found: dict[str, Any] | None) -> None:
        """Olcsónak látszott, de a szokásos ára nem elég biztos (kevés hasonló, egy oldalról): nem szól róla, csak a
        naplóba írja (látottnak jegyzi)."""
        self._mark(row)
        basis = (f"csak {found['n']} hasonló árat találtam ({', '.join(self._site_names(found['sites'])) or '?'})"
                 if found else "nem találtam hozzá elég hasonlót")
        self.journal.add(f"Nem biztos, hogy megéri, nem szóltam: „{(row.get('title') or row['text'])[:60]}” "
                         f"{format_price(row['price'])} "
                         f"({row.get('site') or row.get('platform') or '?'}; „{query}”) – {basis}.")

    def _explore(self, batch: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str, bool]] | None:
        """Egy modellhívás: a hirdetések termékének neve (keresőszó), és melyik érhet meg egy viszonteladónak (buy)."""
        lines = []
        for n, row in enumerate(batch, 1):
            when = (f" (licit: most {format_price(row['price'])}, {left_text(row.get('left'))} múlva zárul)"
                    if row.get("auction") else f" – {format_price(row['price'])}")
            lines.append(f"{n}. {row['text']}{when}")
        messages = [{"role": "system", "content": EXPLORE_SYSTEM},
                    {"role": "user", "content": "Friss hirdetések egy figyelt oldalon:\n" + "\n".join(lines)}]
        try:
            data = self.llm.chat_json(messages, EXPLORE_SCHEMA, temperature=0.0, max_tokens=700)
        except Exception:  # noqa: BLE001 - a modell hibája ne állítsa le a keresést
            return None
        items: list[tuple[dict[str, Any], str, bool]] = []
        buys = 0
        for item in data.get("items") or []:
            try:
                index = int(item.get("n")) - 1
            except (AttributeError, TypeError, ValueError):
                continue
            query = " ".join(str(item.get("query") or "").split())[:60]
            if not (0 <= index < len(batch)) or not query or any(batch[index] is row for row, _q, _b in items):
                continue
            buy = bool(item.get("buy")) and buys < EXPLORE_PICKS
            buys += buy
            items.append((batch[index], query, buy))
        return items

    def _explored(self, game: _Round, watch: dict[str, Any], batch: list[dict[str, Any]],
                  items: list[tuple[dict[str, Any], str, bool]] | None) -> None:
        """A modell válasza után: a megnevezett fix áras hirdetések ára az árbázisba kerül a termék neve alatt (így
        legközelebb több termékre lesz szokásos ár); ami így felismerhető, azt az árbázishoz méri; amit a modell
        megérőnek tart, annak máshol nézi meg az árát (_resale, körönként legfeljebb EXPLORE_PICKS)."""
        if items is None:
            return                             # a modell nem válaszolt: a következő körben újra jönnek
        game.named += len(items)
        for row, query, _buy in items:
            self.book.add([row], name=query)
            brand = self.book.learn_name(query)
            if brand:
                self.journal.add(f"Tanultam egy új márkát: {brand.capitalize()} (a modell többször így nevezte meg a "
                                 "terméket); mostantól modell nélkül is felismerem.")
        named = {id(row) for row, _q, _b in items}
        skip = [row for row in batch if id(row) not in named]   # egy hirdetést egyszer néz meg
        steps = []
        for row, query, buy in items:
            product = self.book.key(query)
            reference = self.book.reference(product[0], exclude=row["url"]) if product else None
            if reference is not None:
                deal = self._explore_deal(watch, row, {**reference, "name": query})
                if deal is not None and sure_price(reference):
                    steps.append(partial(self._candidate, game, {"id": watch["id"], "query": query}, deal))
                    continue
                if deal is not None:           # olcsónak látszik, de az ára nem elég biztos: a többi oldalon is
                    buy, reference = True, None
            if buy and reference is None and game.resale > 0:
                game.resale -= 1
                steps.append(partial(self._resale, game, watch, row, query))
            elif not (buy and reference is None):
                skip.append(row)               # a megérő, de a keretbe már nem férő a következő körben újra jön
        self._mark(*skip)
        game.next(steps)

    def _resale(self, game: _Round, watch: dict[str, Any], row: dict[str, Any], query: str) -> None:
        """Egy hirdetés (vagy licit) viszonteladási ára biztosan: az árbázis árai (ha ismeri a terméket), és a
        keresőszó találatai azokon az oldalakon, ahol még nem láttuk (modell nélkül; a találatok árai az árbázisba is
        bekerülnek). Csak akkor jelölt, ha így elég hasonló ár van, több oldalról (sure_price), és a mostani ár jóval
        alatta van; különben nem szól róla (a naplóba beírja)."""
        self._mark(row)
        probe = {"id": watch["id"], "query": query}
        product = self.book.key(query)
        have = {s["site"] for s in self.book.samples(product[0], exclude=row["url"])} if product else set()
        sites = [k for k in RESALE_SITES if k in self.platforms and k not in have]
        if game.no_browser:
            sites = [k for k in sites if self.fetcher is not None and self.fetcher.usable(k)]
        own = [k for k in sites if k == row.get("platform")]
        rows: list[dict[str, Any]] = []
        found = None
        for group in ([k for k in sites if k not in own], own):   # a hirdetés saját oldala csak, ha a többi kevés
            if not group or (found is not None and found["sure"]):
                continue
            rows += self._collect(probe, group, game.check, track=False, game=game)[0]
            found = self._market_of(row, query, rows)
        found = found or self._market_of(row, query, rows)
        if not found or not found["sure"]:
            if found is not None and self._explore_deal(watch, row, {**found, "name": query}) is not None:
                self._unsure(row, query, found)
            return
        deal = self._explore_deal(watch, row, {**found, "name": query})
        if deal is not None:
            game.next([partial(self._candidate, game, probe, deal)])

    def _step_revisit(self, game: _Round) -> None:
        """Eladási sebesség: néhány régebben látott hirdetés oldala újra (csak olvasva; a Vintedet nem): ha eltűnt vagy
        elkelt, az árbázis feljegyzi, hány nap után."""
        skip = ("vinted",)
        if game.no_browser:                    # a gyors sor csak a böngésző nélkül olvasható oldalakat nézi újra
            skip = tuple(k for k in self.platforms if not (self.fetcher and self.fetcher.usable(k)))
        for key, url, _name in self.book.revisit(SPEED_CHECKS, skip_sites=skip):
            if game.blocked:
                return
            game.check()
            try:
                page = self._look(url, check=game.check, game=game)
            except BrowserError:
                continue
            state, _detail = classify(page, url)
            if state == BLOCKED:
                game.blocked = True
                return
            self.book.revisited(key, url, state in (SOLD, GONE))

    # --- böngészés napközben (munkamenetek, pihenők, oldalváltás) ---

    def _day_ok(self, now: float) -> bool:
        start, end = self._data["day"]
        local = time.localtime(now)
        return start <= local.tm_hour + local.tm_min / 60 < end

    def _budget(self, site: str) -> int:
        return BROWSE_PAGES.get(site, BROWSE_MAX_PAGES)

    def _browse_source(self) -> dict[str, Any]:
        """A böngészés találatainak forrása (a legkisebb haszon, a licitek ablaka) a _triage és az _explored számára."""
        return {"id": "bongeszes", "name": "Böngészés", "hours": WATCH_HOURS,
                "min_profit": int(self._data["browse"].get("min_profit") or WATCH_PROFIT)}

    def _lanes(self, site: str) -> list[dict[str, Any]]:
        """Egy oldal indulóoldalai: a beépítettek (BROWSE_LANES) és a tanultak (_learn)."""
        platform = self.platforms.get(site)
        if platform is None:
            return []
        out = []
        for name, target in BROWSE_LANES.get(site, ()):
            if not target:
                url, kind = platform.home, "feed"
            elif re.match(r"https?://", target):
                host = (urllib.parse.urlsplit(target).hostname or "").removeprefix("www.")
                ours = any(host == d or host.endswith("." + d) for d in platform.domains)
                url = target if ours else platform.search_url.format(q=urllib.parse.quote(name))
                kind = "page"
            else:
                url, kind = platform.search_url.format(q=urllib.parse.quote(target)) + NEWEST.get(site, ""), "page"
            ident = f"{site}:" + re.sub(r"[^a-z0-9]+", "_", fold(name)).strip("_")
            out.append({"id": ident, "site": site, "name": f"{platform.name}: {name}", "url": url, "kind": kind})
        for ident, lane in self._data["lanes"].items():
            if lane.get("kind") == "tanult" and lane.get("site") == site and lane.get("url") and not lane.get("off"):
                out.append({"id": ident, "site": site, "name": str(lane.get("name") or ident), "url": lane["url"],
                            "kind": "tanult"})
        return out

    def _lane_weight(self, lane_id: str) -> float:
        """Öntanulás: amelyik indulóoldal több jelöltet és jó találatot hozott (és amit meg is vettél), azt gyakrabban
        nézi; ami sokszor semmit, azt ritkábban (de nem hagyja el teljesen)."""
        stats = self._data["lanes"].get(lane_id) or {}
        good = (3 * int(stats.get("deals") or 0) + int(stats.get("candidates") or 0) + 5 * int(stats.get("bought") or 0)
                - 2 * int(stats.get("dismissed") or 0))
        return max(0.2, (1 + good) / (1 + 0.1 * int(stats.get("pages") or 0)))

    def _pick_lane(self, site: str, used: list[str], first: bool) -> dict[str, Any] | None:
        lanes = self._lanes(site)
        if not lanes:
            return None
        if first:
            feed = [lane for lane in lanes if lane["kind"] == "feed"]
            if feed:
                return feed[0]                 # a Vinteden a kezdőoldallal kezd, ahogy egy ember
        fresh = [lane for lane in lanes if lane["name"] not in used] or lanes
        with self._lock:
            weights = [self._lane_weight(lane["id"]) for lane in fresh]
        return self.rng.choices(fresh, weights)[0]

    def _lane_list(self) -> list[dict[str, Any]]:
        """A felületnek: az indulóoldalak, és mit hoztak eddig."""
        out = []
        for site in BROWSE_ORDER:
            for lane in self._lanes(site):
                stats = self._data["lanes"].get(lane["id"]) or {}
                out.append({**lane, **{k: int(stats.get(k) or 0) for k in
                                       ("pages", "rows", "candidates", "deals", "bought", "dismissed")}})
        return out

    def _session(self, lane: str) -> dict[str, Any]:
        return self._data[SESSION_KEYS[lane]]

    def _queue(self, lane: str) -> list[dict[str, Any]]:
        return self._fast_pending if lane == LANE_FAST else self._pending

    def _set_queue(self, lane: str, rows: list[dict[str, Any]]) -> None:
        if lane == LANE_FAST:
            self._fast_pending = rows
        else:
            self._pending = rows

    def _lane_of(self, site: str) -> str:
        """Melyik sorban megy egy oldal: a gyorsban, ha a gyors sor fut, és az oldal most böngésző nélkül olvasható
        (nem tilt); különben a böngészőében (így egy tiltó oldalt a böngésző vesz át)."""
        fast = self.parallel and self.fetcher is not None and self.fetcher.usable(site)
        return LANE_FAST if fast else LANE_BROWSER

    def _rotation(self, lane: str = LANE_BROWSER) -> list[str]:
        """A sor bejelölt oldalai sorban (BROWSE_ORDER), a legutóbbi utánitól kezdve."""
        order = [k for k in BROWSE_ORDER if k in self._data["browse"]["sites"] and k in self.platforms and self._lanes(k)
                 and self._lane_of(k) == lane]
        last = self._session(lane).get("site")
        start = order.index(last) + 1 if last in order else 0
        return order[start:] + order[:start]

    def _ready_at(self, site: str, now: float) -> float | None:
        """Mikortól nézheti újra ezt az oldalt: a saját pihenője (BROWSE_REST) és a Vinted két órája (SITE_GAP) után.
        None, ha ma már nem (robot-ellenőrzést kért)."""
        state = self._data["sites_state"]
        if state["blocked"].get(site) == day_of(now):
            return None
        ended = float(state["ended"].get(site) or 0)
        return max(float(state["rest"].get(site) or 0), ended + SITE_GAP.get(site, 0) * 60 if ended else 0.0)

    def _next_site(self, now: float, lane: str = LANE_BROWSER) -> str | None:
        """A következő oldal a sorban, ami már kipihente magát (a többi oldal pihenője nem számít)."""
        for site in self._rotation(lane):
            ready = self._ready_at(site, now)
            if ready is not None and ready <= now:
                return site
        return None

    def _next_free(self, now: float, lane: str = LANE_BROWSER) -> tuple[str, float] | None:
        """Melyik oldal jöhet legközelebb, és mikor (ha most mind pihen, a legkorábban kipihent)."""
        best: tuple[str, float] | None = None
        for site in self._rotation(lane):
            ready = self._ready_at(site, now)
            if ready is not None and (best is None or max(ready, now) < best[1]):
                best = (site, max(ready, now))
        return best

    def _browse_due(self, now: float, lane: str = LANE_BROWSER) -> bool:
        """Esedékes-e a böngészés következő lépése ebben a sorban (a zár a hívónál)."""
        if not (self._data["enabled"] and self._data["browse"]["on"]):
            return False
        if (self.browser is None) if lane == LANE_BROWSER else not (self.parallel and self.fetcher is not None):
            return False
        session = self._session(lane)
        if session.get("phase") == "work":
            return (now >= float(session.get("next") or 0) or now >= float(session.get("until") or 0)
                    or not self._day_ok(now))
        if not self._day_ok(now):
            return False
        if session.get("phase") == "rest" and now < float(session.get("until") or 0):
            return False
        return self._next_site(now, lane) is not None

    def _browse_status(self, now: float) -> str:
        """Egy mondat a böngészés mostani állapotáról (a felületnek); ha két sor fut, mindkettőé."""
        if not self._data["browse"]["on"]:
            return ""
        if not self.parallel:
            return self._lane_status(now, LANE_BROWSER)
        parts = [self._lane_status(now, lane) for lane in (LANE_FAST, LANE_BROWSER)
                 if self._rotation(lane) or self._session(lane).get("phase") == "work"]
        parts = [part for n, part in enumerate(parts) if part and part not in parts[:n]]
        return " ".join(parts) or "Most egyik oldal sem esedékes."

    def _lane_status(self, now: float, lane: str) -> str:
        session, start, end = self._session(lane), *self._data["day"]
        clock = lambda t: time.strftime("%H:%M", time.localtime(t))  # noqa: E731
        name = lambda key: self.platforms[key].name if key in self.platforms else key  # noqa: E731
        if session.get("phase") == "work" and now < float(session.get("until") or 0):
            return f"Most: {name(session['site'])} ({clock(float(session['start']))} óta)."
        if not self._day_ok(now):
            return f"Napközben böngészik ({start}–{end} óra); most nem dolgozik."
        upcoming = self._next_free(max(now, float(session.get("until") or 0)), lane)
        if upcoming is None:
            return "Most egyik oldal sem esedékes."
        site, at = upcoming
        return f"Pihen {clock(at)}-ig, utána: {name(site)}." if at - now > 90 else f"Hamarosan: {name(site)}."

    def _step_browse(self, game: _Round, running: list[dict[str, Any]], lane: str = LANE_BROWSER) -> None:
        """Egy lépés a napközbeni böngészésből: ha kell, új munkamenetet kezd a következő oldalon, vagy lezárja a
        mostanit (pihenő); különben betölt egy indulóoldalt, átnézi (modell nélkül, _triage), és néha megnyit egy
        hirdetést is, ahogy egy ember (_open_random). Az ismeretleneket gyűjti, és csak kötegben kérdezi a modellt."""
        now = self.clock()
        game.browse_lane = lane
        with self._lock:
            session = self._session(lane)
            if session.get("phase") == "work" and (now >= float(session.get("until") or 0) or not self._day_ok(now)
                                                   or int(session.get("pages") or 0) >= self._budget(session["site"])
                                                   or session["site"] not in self._data["browse"]["sites"]
                                                   or self._lane_of(session["site"]) != lane
                                                   or not self._lanes(session["site"])):
                self._close_session(game)
                return
            if session.get("phase") != "work":
                site = self._next_site(now, lane)
                if site is None or not self._day_ok(now):
                    return
                self._learn(now)              # naponta egyszer, a nap első munkamenete előtt
                session = {"phase": "work", "site": site, "start": now, "next": now, "pages": 0, "rows": 0, "named": 0,
                           "until": now + BROWSE_WORK * 60 * self.rng.uniform(0.75, 1.25), "hits": [], "lanes": []}
                self._data[SESSION_KEYS[lane]] = session
                self._save()
            site, used = session["site"], list(session["lanes"])
        lane = self._pick_lane(site, used, not used)
        if lane is None:
            return
        game.check()
        source = self._browse_source()
        rows = self._page_rows(game, lane["url"], site, source, extra={"lane": lane["id"], "lane_name": lane["name"]})
        self._lane_count(lane["id"], "pages")
        if game.captcha:
            self._site_blocked(game, game.captcha)
            return
        unknown = self._triage(game, source, rows or [], running)
        if rows and (lane["kind"] == "feed" or self.rng.random() < OPEN_CHANCE):
            self._open_random(game, source, rows, unknown)
        if game.captcha:
            self._site_blocked(game, game.captcha)
            return
        pending = self._queue(lane)
        known = {r["url"] for r in pending}
        self._set_queue(lane, (pending + [r for r in unknown if r["url"] not in known])[-MAX_PENDING:])
        if len(self._queue(lane)) >= EXPLORE_ROWS and game.lane is not None:
            self._flush(game)
        with self._lock:
            self._lane_count(lane["id"], "rows", len(rows or []))
            session["lanes"].append(lane["name"])
            session["rows"] = int(session.get("rows") or 0) + len(rows or [])
            session["next"] = self.clock() + self.rng.uniform(*STEP_PAUSE.get(site, DEFAULT_STEP))
            self._save()
        game.notes.append(f"{lane['name']}: {len(rows or [])} hirdetés")

    def _open_random(self, game: _Round, source: dict[str, Any], rows: list[dict[str, Any]],
                     unknown: list[dict[str, Any]]) -> None:
        """Ahogy egy ember: megnyit egy hirdetést a találatok közül (inkább egy ismeretlent), és elolvassa. A hirdetés
        címéből gyakran kiderül, mi ez (a kártyán sokszor csak a márka és a méret látszik); ha így ismert a szokásos ára,
        modell nélkül eldönti, megéri-e; különben a címével együtt kerül a modell elé."""
        with self._lock:
            seen = set(self._data["seen"])
        pool = [r for r in unknown if not r.get("auction")] or \
            [r for r in rows if not r.get("auction") and listing_key(r["url"]) not in seen]
        if not pool:
            return
        row = self.rng.choice(pool)
        game.check()
        try:
            page = self._look(row["url"], row.get("platform", ""), game.check, game)
        except BrowserError:
            return
        game.loaded += 1
        if page.get("captcha"):
            game.captcha = row["platform"]
            return
        state, _detail = classify(page, row["url"], "", row.get("platform", ""))
        if state in (SOLD, GONE):
            self._mark(row)
            if row in unknown:
                unknown.remove(row)
            return
        title = " ".join(str(page.get("title") or "").split())[:120]
        better = {**row, "text": f"{title} – {row['text']}"[:180] if title and fold(title) not in fold(row["text"])
                  else row["text"]}
        product = self.book.key(better["text"])
        reference = self.book.reference(product[0], exclude=row["url"]) if product else None
        if reference is None:
            if row in unknown:
                unknown[unknown.index(row)] = better
            return
        if row in unknown:
            unknown.remove(row)
        deal = self._explore_deal(source, better, reference)
        if deal is None:
            self._mark(row)
        else:
            game.next([partial(self._candidate, game, {"id": source["id"], "query": reference["name"]}, deal, page)])

    def _flush(self, game: _Round) -> None:
        """A gyűjtött ismeretlenek egy modellhívásban (energiatakarékos: nem oldalanként)."""
        pending = self._queue(game.browse_lane)
        batch = pending[:EXPLORE_ROWS]
        self._set_queue(game.browse_lane, pending[EXPLORE_ROWS:])
        if batch:
            batch.sort(key=lambda r: not r.get("auction"))
            game.ask(partial(self._explore, batch), partial(self._explored, game, self._browse_source(), batch))

    def _close_session(self, game: _Round) -> None:
        """A munkamenet vége: a maradék ismeretlenek a modellnek (ha legalább MIN_FLUSH, vagy licit is van köztük; ha
        kevesebb, a következő munkamenetre várnak, hogy pár hirdetésért ne töltse be a modellt), néhány régi hirdetés
        újranézése (eladási sebesség); a naplóbejegyzés és a pihenő a kör végén jön (_finish_session), amikor már minden
        eredmény megvan."""
        pending = self._queue(game.browse_lane)
        if game.lane is not None and (len(pending) >= MIN_FLUSH or any(r.get("auction") for r in pending)):
            self._flush(game)                  # (ha most nem lehet kérdezni a modellt, a következő munkamenetre vár)
        game.closing = True
        game.work.append(partial(self._step_revisit, game))

    def _site_blocked(self, game: _Round, site: str) -> None:
        """Robot-ellenőrzés: aznap ezt az oldalt már nem nézi (nem próbálja megkerülni), és egyszer szól."""
        now = self.clock()
        name = self.platforms[site].name if site in self.platforms else site
        with self._lock:
            self._data["sites_state"]["blocked"][site] = day_of(now)
            self._save()
        self.journal.add(f"{name}: robot-ellenőrzést kért, ma már nem nézem (nem próbálom megkerülni).")
        game.notify("deal", f"{name}: robot-ellenőrzés",
                    f"A {name} robot-ellenőrzést kért böngészés közben. Ma már nem nézem; ha szeretnéd, oldd meg a "
                    "böngészőablakomban, és holnap folytatom.", actions=[DEALS_ACTION], quiet=True)
        self._close_session(game)

    def _finish_session(self, game: _Round) -> str:
        """A munkamenet lezárása a kör végén: egy naplóbejegyzés (mikor, hol, hány oldal és hirdetés, mit talált). Ez az
        oldal BROWSE_REST percig (±30%) pihen, a Jarvis pedig egy rövid szünet (SWITCH_PAUSE) után a következő oldalon
        folytatja; csak akkor vár tovább, ha minden oldal pihen."""
        now = self.clock()
        lane = game.browse_lane
        with self._lock:
            session = self._session(lane)
            if session.get("phase") != "work":
                return ""
            site, start = session["site"], float(session["start"])
            end = min(now, max(float(session.get("until") or now), start))
            name = self.platforms[site].name if site in self.platforms else site
            lanes = list(dict.fromkeys(lane.split(": ", 1)[-1] for lane in session.get("lanes") or []))
            clock = lambda t: time.strftime("%H:%M", time.localtime(t))  # noqa: E731
            text = (f"{clock(start)}–{clock(end)} {name}: {session.get('pages', 0)} oldal"
                    + (f" ({', '.join(lanes[:6])})" if lanes else "") + f", {session.get('rows', 0)} hirdetés")
            if session.get("named"):
                text += f", a modell {session['named']} hirdetést nevezett meg"
            hits = session.get("hits") or []
            text += f"; jó találat: {'; '.join(hits[:3])}." if hits else "; nem volt jó találat."
            state = self._data["sites_state"]
            state["ended"][site] = now
            state["rest"][site] = now + BROWSE_REST * 60 * self.rng.uniform(0.7, 1.3)
            switch = now + self.rng.uniform(*SWITCH_PAUSE)
            self._data[SESSION_KEYS[lane]] = {"phase": "rest", "site": site, "until": switch}
            following = self._next_free(switch, lane)
            if following and self._day_ok(now):
                other = self.platforms[following[0]].name if following[0] in self.platforms else following[0]
                text += (f" Utána: {other}." if following[1] <= switch + 1 else
                         f" Pihenő {clock(following[1])}-ig, utána: {other}.")
            self._save()
        self.journal.add(text, at=end)
        return text

    def _after_browse(self, game: _Round) -> str:
        """A böngészési kör után: a munkamenet számlálói, és ha véget ért, a lezárása. Visszaadja a rövid eredményt."""
        with self._lock:
            session = self._session(game.browse_lane)
            if session.get("phase") == "work":
                session["pages"] = int(session.get("pages") or 0) + game.loaded
                session["named"] = int(session.get("named") or 0) + game.named
                session["hits"] = (session.get("hits") or []) + game.hits
                self._save()
        if getattr(game, "closing", False):
            return self._finish_session(game) or "Böngészés: pihenő."
        notes = [n for n in game.notes if n] + game.errors
        return "Böngészés: " + ("; ".join(notes[:3]) if notes else "nincs mit nézni") + "."

    def run_fast(self, check: Callable[[], None] = lambda: None, notify: Callable[..., None] = lambda *a, **k: None,
                 model_ok: Callable[[], bool] = lambda: True) -> str:
        """Egy lépés a gyors sorból (a saját szálán, start_fast_lane): a Hardverapró és a Jófogás böngészése böngésző
        nélkül, ugyanúgy, mint a böngésző sora (munkamenetek, pihenők, a tölcsér), amíg a böngésző a Vintedet és a
        Vaterát nézi. A böngészőhöz nem nyúl; a modellt csak akkor kérdezi, ha most lehet (model_ok). Üres, ha nem volt
        dolga."""
        with self._lock:
            if not self._browse_due(self.clock(), LANE_FAST):
                return ""
            running = [json.loads(json.dumps(s)) for s in self._data["searches"] if search_status(s) == "fut"]
        game = _Round(check, notify, ModelLane() if self.llm is not None and model_ok() else None)
        game.browse_lane, game.no_browser = LANE_FAST, True
        game.work.append(partial(self._step_browse, game, running, LANE_FAST))
        try:
            game.loop()
        finally:
            if game.lane is not None:
                game.lane.close()
            if self.clock() - self._book_saved >= BOOK_SAVE:
                self._book_saved = self.clock()
                self.book.save()
        return self._after_browse(game)

    def start_fast_lane(self, allowed: Callable[[], bool], model_ok: Callable[[], bool],
                        notify: Callable[..., None]) -> None:
        """A gyors sor szála (a JarvisApp indítja): innentől a Hardverapró és a Jófogás böngészése a böngészővel
        párhuzamosan megy (parallel). allowed: most dolgozhat-e (nincs Szünet stb.)."""
        if self.fetcher is None or (self._fast_thread is not None and self._fast_thread.is_alive()):
            return
        self.parallel = True
        self._fast_stop.clear()

        def check() -> None:
            if self._fast_stop.is_set():
                raise _FastStop()

        def loop() -> None:
            with background_calls():
                work()

        def work() -> None:
            while not self._fast_stop.wait(FAST_TICK):
                try:
                    if allowed():
                        self.run_fast(check, notify, model_ok)
                except _FastStop:
                    return
                except Exception:  # noqa: BLE001 - a gyors sor egy hiba miatt ne álljon le
                    pass

        self._fast_thread = threading.Thread(target=loop, name="jarvis-gyors-sor", daemon=True)
        self._fast_thread.start()

    def stop_fast_lane(self) -> None:
        """Leállítja a gyors sor szálát, és megvárja (legfeljebb pár másodpercig), hogy a mostani lépését befejezze."""
        self._fast_stop.set()
        thread = self._fast_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)

    def _learn(self, now: float) -> None:
        """Öntanulás (naponta egyszer): a gyorsan kelő termékekből (az árbázis eladási sebessége szerint) és amiből már
        többször volt jó találat, saját indulóoldal lesz (egy keresés a legújabb elöl, azon az oldalon, ahol a
        legtöbbször láttuk); ami LEARN_IDLE betöltés után sem hozott semmit, kimarad. A naplóba írja, mit tanult."""
        state = self._data["sites_state"]
        today = day_of(now)
        if state.get("learned") == today:
            return
        state["learned"] = today
        lanes, notes = self._data["lanes"], []
        for ident, lane in list(lanes.items()):
            if lane.get("kind") != "tanult":
                continue
            if lane.get("off"):
                if now - float(lane["off"]) > MUTE_DAYS * 86400:
                    lanes.pop(ident)           # egy hónap múlva újra megtanulhatja
            elif int(lane.get("pages") or 0) >= LEARN_IDLE and not lane.get("deals") and not lane.get("candidates"):
                lane["off"] = now
                notes.append(f"leállítottam a(z) {lane.get('name')} figyelését (nem hozott semmit)")
        learned = [lane for lane in lanes.values() if lane.get("kind") == "tanult" and not lane.get("off")]
        have = {lane.get("key") for lane in lanes.values() if lane.get("kind") == "tanult"}
        recent = Counter(d["product_key"] for d in self._data["deals"] if d.get("product_key")
                         and not d.get("dismissed") and now - float(d.get("at") or 0) < 14 * 86400)
        bought = {d.get("product_key") for d in self._data["deals"] if d.get("bought")}
        picks = [(key, "többször volt belőle jó találat") for key, n in recent.most_common() if n >= 2 or key in bought]
        picks += [(key, speed_text(speed)) for key, speed in self.book.fast()]
        for key, why in picks:
            if len(learned) >= MAX_LEARNED:
                break
            site = self.book.home_site(key)
            if key in have or self._muted(key, now) or site not in self.platforms \
                    or site not in self._data["browse"]["sites"]:
                continue
            platform, name = self.platforms[site], self.book.name(key)
            ident = f"tanult:{key}"
            lanes[ident] = {"kind": "tanult", "site": site, "key": key, "at": now, "name": f"{platform.name}: {name}",
                            "url": platform.search_url.format(q=urllib.parse.quote(name)) + NEWEST.get(site, "")}
            learned.append(lanes[ident])
            have.add(key)
            notes.append(f"mostantól figyelem: {name} ({ON_SITE.get(site, platform.name)}; {why})")
        if notes:
            self.journal.add("Tanultam: " + "; ".join(notes) + ".", at=now)

    # --- Vinted-ajánlat (csak a jóváhagyásod után) ---

    def make_offer(self, deal_id: str, amount: Any, confirm: Callable[[str], bool],
                   progress: Callable[[str], None] = lambda _text: None) -> str:
        """Ajánlattétel a Vinteden egy talált hirdetésre, emberi tempóban: megnyitja a hirdetést, az Ajánlattétel
        gombra kattint, beírja az összeget, és a küldés előtt jóváhagyást kér, pontosan ezzel az összeggel. Fizetni
        nem fizet: ha az eladó elfogadja, a Vinted értesít, és te veszed meg. Visszaadja a rövid eredményt; ha nem
        tudja megtenni, DealError."""
        deal = self.get(deal_id)
        if deal.get("platform") != "vinted":
            raise DealError("Ajánlatot csak a Vinteden tudok tenni.")
        if deal.get("offer_sent"):
            raise DealError(f"Erre már tettél ajánlatot ({format_price(deal['offer_sent']['price'])}); ha az eladó "
                            "elfogadja, a Vinted értesít.")
        offer = self._amount(amount if amount not in (None, "") else deal.get("offer"), "Az ajánlat")
        if not offer:
            raise DealError("Mennyit ajánljak?")
        if offer >= int(deal["price"]):
            raise DealError("Az ajánlat legyen kevesebb a kért árnál (különben vedd meg simán).")
        if self.browser is None:
            raise DealError("Az ajánlattételhez Jarvis saját böngészője kell.")
        browser = self.browser()
        previous = getattr(browser, "auto", False)
        browser.auto = True                    # modell nélküli lépéssor: emberi tempó, gondolkodási szünetekkel
        self.offering = (f"Ajánlat a Vinteden: {format_price(offer)} – „{deal['text'][:80]}” (most "
                         f"{format_price(deal['price'])}). Ha az eladó elfogadja, a Vinted szól, és ennyiért megveheted; "
                         "fizetni magától nem fogok.")
        try:
            return self._offer(browser, deal, offer, confirm, progress)
        finally:
            browser.auto = previous
            self.offering = ""

    def _offer(self, browser: Any, deal: dict[str, Any], offer: int, confirm: Callable[[str], bool],
               progress: Callable[[str], None]) -> str:
        try:
            progress("a hirdetés megnyitása")
            browser.open(deal["url"])
            if browser.challenged():
                raise DealError("A Vinted robot-ellenőrzést kér; oldd meg a böngészőablakomban, és próbáld újra.")
            elements = browser.scan()
            consent = consent_button(elements)
            if consent is not None:
                browser.click(consent["id"])
                elements = browser.scan()
            if logged_out(elements):
                raise DealError("Ki vagy jelentkezve a Vinteden: jelentkezz be a böngészőmben, és próbáld újra.")
            openers = [e for e in elements if e.get("kind", "") in BUTTON_KINDS | {"link"} and not e.get("disabled")
                       and _OFFER_OPEN_RE.match(norm_text(e.get("label", "")))]
            if len(openers) != 1:
                raise DealError("Nem találom a hirdetésen az Ajánlattétel gombot (lehet, hogy már elkelt, vagy az eladó "
                                "nem fogad ajánlatot), ezért nem tettem ajánlatot.")
            before = Counter(elem_key(e) for e in elements)
            progress("ajánlattétel")
            browser.click(openers[0]["id"])
            fresh = new_elements(before, browser.scan())
            boxes = [e for e in fresh if e.get("kind", "") in _PRICE_KINDS and not (e.get("disabled") or e.get("readonly")
                                                                                 or e.get("sensitive"))]
            labeled = [e for e in boxes if _OFFER_BOX_RE.search(norm_text(f"{e.get('label', '')} {e.get('field', '')}"))]
            box = (labeled or boxes)[0] if len(labeled or boxes) == 1 else None
            if box is None:
                browser.press("Escape")
                raise DealError("Nem találom az ajánlat összegének mezőjét, ezért nem tettem ajánlatot. Nézd meg a "
                                "böngészőablakomban.")
            browser.type_text(box["id"], str(offer))
            fresh = new_elements(before, browser.scan())
            buttons = [e for e in fresh if e.get("kind", "") in BUTTON_KINDS and not e.get("disabled")
                       and _OFFER_SEND_RE.match(norm_text(e.get("label", "")))]
            if len(buttons) != 1:
                browser.press("Escape")
                raise DealError("Nem találom az ajánlat küldés gombját, ezért nem tettem ajánlatot. Nézd meg a "
                                "böngészőablakomban.")
        except BrowserError as exc:
            raise DealError(f"Nem tettem ajánlatot: a böngészőben hiba volt ({str(exc).splitlines()[0][:160]}).") from None
        progress("küldés (jóváhagyásra vár)")
        label = buttons[0].get("label", "")
        if not (browser.cfg.confirm_risky and is_risky(label)):
            # A gomb felirata alapján a böngésző nem kérdezne rá (vagy ki van kapcsolva): ajánlat csak jóváhagyással.
            if not confirm(f'{self.offering}\n\nJarvis elküldené (erre kattintana: "{label}").'):
                browser.press("Escape")
                return "Rendben, nem tettem ajánlatot."
        try:
            result = browser.click(buttons[0]["id"])
        except BrowserError as exc:
            return (f"Az ajánlat küldésénél hiba volt ({str(exc).splitlines()[0][:160]}). Nézd meg a böngészőablakomban, "
                    "elment-e.")
        if result.startswith(DECLINED):
            browser.press("Escape")
            return "Rendben, nem tettem ajánlatot."
        with self._lock:
            for stored in self._data["deals"]:
                if stored.get("id") == deal["id"]:
                    stored["offer_sent"] = {"price": offer, "at": self.clock()}
            self._save()
        try:
            shown = str(browser.page_data(LISTING_PAGE_JS).get("text") or "")
        except BrowserError:
            shown = ""
        seen = str(offer) in re.sub(r"\D", "", shown) or format_price(offer).replace(" Ft", "") in shown
        return (f"Elküldtem az ajánlatot ({format_price(offer)}) a Vinteden. Ha az eladó elfogadja, a Vinted értesít, és "
                "akkor megveheted." + ("" if seen else " (Az oldalon nem láttam visszaigazolást; nézd meg a "
                                                     "böngészőablakomban.)"))

    def _mark(self, *deals: dict[str, Any]) -> None:
        """Ezeket a hirdetéseket láttuk (az árukkal, a leárazás észrevételéhez); egy mentéssel."""
        deals = tuple(d for d in deals if d.get("url"))
        if not deals:
            return
        with self._lock:
            for deal in deals:
                key = listing_key(deal["url"])
                self._data["seen"][key], self._data["prices"][key] = self.clock(), int(deal["price"])
            self._prune()
            self._save()

    def _prune(self) -> None:
        seen = self._data["seen"]
        if len(seen) > MAX_SEEN:       # a legrégebbiek kiesnek
            for key, _at in sorted(seen.items(), key=lambda kv: kv[1])[:len(seen) - MAX_SEEN]:
                seen.pop(key, None)
        prices = self._data["prices"]
        for key in [k for k in prices if k not in seen][:max(0, len(prices) - MAX_SEEN)]:
            prices.pop(key, None)

    def _inspect(self, deal: dict[str, Any], page: dict[str, Any] | None = None, game: _Round | None = None) -> str:
        """A jelölt hirdetés oldala (ha még nincs meg, megnyitja), modell nélkül, csak olvasva. Visszaadja, miért esik
        ki („” = maradhat, „captcha”: robot-ellenőrzés, ilyenkor maradhat, de nem néztük meg). A figyelmeztetéseket és a
        leírás elejét a jelölthöz teszi (a modellkérdéshez)."""
        if page is None:
            try:
                page = self._look(deal["url"], deal.get("platform", ""), game=game)
            except BrowserError:
                deal.setdefault("warnings", []).append("a hirdetés oldalát nem tudtam megnyitni")
                return ""
        state, _detail = classify(page, deal["url"], deal["text"], deal.get("platform", ""))
        if page.get("captcha"):
            deal.setdefault("warnings", []).append("a hirdetés oldalát nem néztem meg (robot-ellenőrzés)")
            return "captcha"
        if state in (SOLD, GONE):
            return "elkelt"
        text = fold(str(page.get("text") or "")[:PAGE_TEXT])
        deal["details"] = " ".join(str(page.get("text") or "").split())[:PAGE_TEXT]
        deal["images"] = [str(url) for url in page.get("images") or []][:CHECK_PHOTOS]
        for pattern, why in ((_SCAM_RE, "átverésre utaló jel a hirdetésben (előre fizetés, külföldről küldi…)"),
                             (_OFFSITE_RE, "az oldalon kívüli elérhetőséget ad meg (WhatsApp, Telegram, e-mail…)"),
                             (_NO_REVIEWS_RE, "az eladónak még nincs értékelése")):
            if pattern.search(text):
                deal["suspect"] = why
                return "gyanús"
        if _PARTS_RE.search(text) or _DEFECT_RE.search(text):
            return "hibás"
        return ""

    def _confirm(self, search: dict[str, Any], deal: dict[str, Any]) -> bool | None:
        """Egy rövid modellkérdés egy jelöltről, a hirdetés fotóival együtt (ha le tudta tölteni őket). None: nem
        sikerült megkérdezni (ilyenkor szól, de nem ellenőrzött); False: nem ez, hibás, vagy gyanús (akkor a
        deal["suspect"]-ben, miért)."""
        photos = self._photos(deal)
        text = (f"Keresett termék: {search['query']}\nHirdetés ({deal['site']}): {deal['text']}\nÁr: "
                f"{format_price(deal['price'])} (a szokásos ára {format_price(deal['median'])})"
                + (f"\nA hirdetés oldala: {deal['details'][:1200]}" if deal.get("details") else "")
                + (f"\nA hirdetés fotói ({len(photos)} db) mellékelve." if photos else ""))
        messages = [{"role": "system", "content": CHECK_SYSTEM},
                    {"role": "user", "content": text, **({"images": photos} if photos else {})}]
        try:
            data = self.llm.chat_json(messages, CHECK_SCHEMA, temperature=0.0, max_tokens=120)
        except Exception:  # noqa: BLE001 - a modell hibája ne állítsa le a keresést
            if not photos:
                return None
            try:                               # pl. a modell nem tud képet: fotók nélkül
                messages[1] = {"role": "user", "content": text.rsplit("\nA hirdetés fotói", 1)[0]}
                data = self.llm.chat_json(messages, CHECK_SCHEMA, temperature=0.0, max_tokens=120)
                photos = []
            except Exception:  # noqa: BLE001
                return None
        deal["photos_checked"] = len(photos) or None
        deal["checked"] = True
        deal["note"] = str(data.get("reason") or "")[:200]
        if data.get("suspicious"):
            deal["suspect"] = "a modell szerint gyanús" + (f": {deal['note']}" if deal["note"] else "")
            return False
        return bool(data.get("ok"))

    def _photos(self, deal: dict[str, Any]) -> list[str]:
        """A hirdetés első fotói a modellnek (letöltve, kicsinyítve); ami nem sikerül, kimarad."""
        out = []
        for url in (deal.get("images") or [])[:CHECK_PHOTOS]:
            data = self.image_get(url) if self.image_get is not None else None
            if not data:
                continue
            try:
                out.append(vision_jpeg(data, CHECK_SIDE))
            except ItemError:
                continue
        return out

    def _remember(self, deal: dict[str, Any]) -> None:
        # a leírás és a fotók címe csak a modellkérdéshez kellett
        deal = {k: v for k, v in deal.items() if k not in ("details", "images") and v is not None}
        with self._lock:
            self._data["deals"] = ([deal] + self._data["deals"])[:MAX_DEALS]
            self._save()

    # --- az azonnali figyelőnek (instant.py): egy friss hirdetés értéke, modell nélkül ---

    def in_day(self) -> bool:
        """A gép üzemideje van-e (a fül tetején megadott órák): az azonnali figyelő is csak ekkor dolgozik."""
        with self._lock:
            return self._day_ok(self.clock())

    def quick_value(self, row: dict[str, Any], min_profit: int | None = None) -> dict[str, Any] | None:
        """Mennyit ér egy friss hirdetés, modell nélkül: ha egy felmért keresésedre illik, annak a szokásos ára (és a
        keresés szabályai: kedvezmény, haszon, ársáv), különben az árbázisé (legalább EXPLORE_CUT-tal olcsóbb, és
        legalább min_profit haszon). {reference, n, name, profit, cheap, search?}; None, ha nem tudni."""
        with self._lock:
            running = [json.loads(json.dumps(s)) for s in self._data["searches"] if search_status(s) == "fut"]
            floor = int(min_profit or self._data["browse"].get("min_profit") or WATCH_PROFIT)
        price = int(row["price"])
        search = next((s for s in running if matches_query(s["query"], row["text"])
                       and not excluded(s.get("exclude") or [], row["text"])), None)
        if search is not None:                 # a jóváhagyott szokásos ára: biztos
            reference = int(search["reference"])
            return {"reference": reference, "n": None, "name": search["query"], "search": search["id"],
                    "profit": expected_profit(reference, price), "sure": True,
                    "cheap": pays(search, price, reference) and self._affordable(price)}
        product = self.book.key(row.get("title") or row["text"])
        found = self.book.reference(product[0], exclude=row["url"]) if product else None
        if found is None:
            return None
        return self._valued(price, found, found["name"], floor)

    def _valued(self, price: int, found: dict[str, Any], name: str, floor: int) -> dict[str, Any]:
        """Egy friss hirdetés értéke egy szokásos árhoz (found: az árbázisból, vagy amennyiért máshol adják; price, p25,
        n, sites) mérve: megéri, ha legalább EXPLORE_CUT-tal olcsóbb, az óvatos eladási áron is legalább floor haszon
        jön ki belőle, és belefér a vételi limitbe. sure: elég biztos-e ez a szokásos ár (sure_price)."""
        reference, low = int(found["price"]), careful_low(found)
        return {"reference": reference, "n": found.get("n"), "name": name, "low": low, "sites": found.get("sites"),
                "spread": spread_of(found), "sold": found.get("sold"),
                "profit": expected_profit(reference, price, low), "sure": sure_price(found),
                "cheap": reference * FLOOR <= price <= explore_top(reference, floor, low) and self._affordable(price)}

    def appraise(self, rows: list[dict[str, Any]], source: str, look: Callable[[dict[str, Any]], dict[str, Any] | None],
                 search: Callable[[str], list[dict[str, Any]]], can_ask: Callable[[], bool],
                 found: Callable[[dict[str, Any]], None]) -> list[dict[str, Any]]:
        """Az azonnali figyelő friss hirdetései: minél hamarabb kideríti, megéri-e megvenni, és csak a megérőkről szól
        (found, rögtön, ahogy egy kiderül). A gyorsabbtól a lassabbig:
        1. modell nélkül: egy felmért keresésed szokásos ára, vagy az árbázis a hirdetés címéből (quick_value); ha az
           árbázis ára nem elég biztos (kevés hasonló, csak egy oldalról: sure_price), előbb a többi oldalon is
           megnézi (search), és csak akkor szól, ha így is jóval olcsóbb;
        2. az ismeretleneket a modell nevezi meg (egy hívás az egész kötegre); ha az árbázis így már ismeri, ahhoz méri,
           ha nem, megnézi az árát a többi oldalon (search; legfeljebb EXPLORE_PICKS, a modell szerint megérők elöl);
        3. a megérő jelölt oldala (look: elkelt, hibás, gyanús → kiesik), és egy rövid modellkérdés (tényleg ez, egy
           darab, működik?).
        A vételi limit fölötti és a nem eladási árú hirdetés (kereső, csere, alkatrész…) meg sem jön. Visszaadja a
        modellre váró hirdetéseket (ha most nem lehet kérdezni, pl. épp beszélgetsz a Jarvisszal)."""
        with self._lock:
            floor = int(self._data["browse"].get("min_profit") or WATCH_PROFIT)
            known = self._deal_keys()
        unknown, skip, recheck = [], [], []
        for row in rows:
            if listing_key(row["url"]) in known:
                continue                       # erről már szóltunk (pl. a böngészős kör)
            if not usable(row["text"]) or not self._affordable(row["price"]):
                skip.append(row)
                continue
            value = self.quick_value(row, floor)
            if value is None:
                unknown.append(row)
            elif value["cheap"] and value["sure"]:
                self._settle(row, value, source, look, can_ask, found)
            elif value["cheap"]:                     # olcsónak látszik, de az ára nem elég biztos
                recheck.append((row, self.book.query(row.get("title") or row["text"]) or value["name"]))
            else:
                skip.append(row)
        self._mark(*skip)
        for row, name in recheck[:EXPLORE_PICKS]:
            self._recheck(row, name, search, floor, source, look, can_ask, found)
        if not unknown:
            return []
        if self.llm is None:
            self._mark(*unknown)               # modell nélkül az ismeretlenről nem derül ki, mennyit ér
            return []
        if not can_ask():
            return unknown
        named = self._explore(unknown[:EXPLORE_ROWS])
        if named is None:
            return unknown                     # a modell nem válaszolt: később újra
        rest = unknown[EXPLORE_ROWS:]
        done = {id(row) for row, _q, _b in named}
        self._mark(*[row for row in unknown[:EXPLORE_ROWS] if id(row) not in done])
        priced: list[tuple[dict[str, Any], dict[str, Any]]] = []
        elsewhere: list[tuple[dict[str, Any], str, bool]] = []
        for row, query, buy in named:
            self.book.add([row], name=query)
            brand = self.book.learn_name(query)
            if brand:
                self.journal.add(f"Tanultam egy új márkát: {brand.capitalize()} (a modell többször így nevezte meg a "
                                 "terméket); mostantól modell nélkül is felismerem.")
            product = self.book.key(query)
            reference = self.book.reference(product[0], exclude=row["url"]) if product else None
            value = self._valued(int(row["price"]), reference, query, floor) if reference is not None else None
            if value is not None and (value["sure"] or not value["cheap"]):
                priced.append((row, value))
            else:                              # ismeretlen, vagy olcsónak látszik, de az ára nem elég biztos
                elsewhere.append((row, query, buy or value is not None))
        for row, value in priced:              # a biztos árúak előbb (ezekhez nem kell letöltés)
            if value["cheap"]:
                self._settle(row, value, source, look, can_ask, found)
            else:
                self._mark(row)
        # amennyiért máshol adják (a modell szerint megérők elöl, legfeljebb EXPLORE_PICKS)
        elsewhere.sort(key=lambda entry: not entry[2])
        for row, query, _buy in elsewhere[:EXPLORE_PICKS]:
            self._recheck(row, query, search, floor, source, look, can_ask, found)
        self._mark(*[row for row, _q, _b in elsewhere[EXPLORE_PICKS:]])
        return rest

    def _recheck(self, row: dict[str, Any], query: str, search: Callable[[str], list[dict[str, Any]]], floor: int,
                 source: str, look: Callable[[dict[str, Any]], dict[str, Any] | None], can_ask: Callable[[], bool],
                 found: Callable[[dict[str, Any]], None]) -> None:
        """Mennyiért adják máshol: a keresőszó találatai a többi oldalon (search; az árbázisba is bekerülnek), az
        árbázis áraival együtt (_market_of). Ha így elég biztos az ár, és jóval olcsóbb, a végső ellenőrzés jön
        (_settle); ha nem elég biztos, nem szól róla."""
        others = search(query)
        self.book.add(others)
        market_now = self._market_of(row, query, others)
        if market_now is None:
            self._mark(row)                    # nincs hasonló: nem tudni, mennyit ér
            return
        value = self._valued(int(row["price"]), market_now, query, floor)
        if not value["cheap"]:
            self._mark(row)
        elif not value["sure"]:
            self._unsure(row, query, market_now)
        else:
            self._settle(row, value, source, look, can_ask, found)

    def _settle(self, row: dict[str, Any], value: dict[str, Any], source: str,
                look: Callable[[dict[str, Any]], dict[str, Any] | None], can_ask: Callable[[], bool],
                found: Callable[[dict[str, Any]], None]) -> None:
        """Egy megérő friss hirdetés végső ellenőrzése: az oldala (elkelt, hibás, gyanús?), az elrejtett termékek, és egy
        rövid modellkérdés (ha most lehet). Ha átment, a találatok közé kerül (Megvettem gomb), és found(találat)."""
        probe = {"id": value.get("search") or "azonnali", "query": value["name"]}
        deal = self._deal(probe, row, int(value["reference"]), explore=source, similar=value.get("n"), quick=True,
                          product=value["name"], low=value.get("low"), sites=self._site_names(value.get("sites")) or None,
                          spread=value.get("spread"), sold=value.get("sold"))
        with self._lock:
            if listing_key(row["url"]) in self._deal_keys():
                return
        if why := self._suspect(deal):
            deal["suspect"] = why
            self._suspicious(deal)             # gyanúsan olcsó: az oldalát sem nyitja meg
            return
        if self._risky(deal):
            return
        page = look(row)
        verdict = self._inspect(deal, page) if page is not None else ""
        if page is None:
            deal.setdefault("warnings", []).append("a hirdetés oldalát nem tudtam megnyitni")
        if verdict == "gyanús":
            self._suspicious(deal)
            return
        if verdict not in ("", "captcha") or self._muted(deal.get("product_key"), self.clock()):
            self._mark(deal)
            return
        if self.llm is not None and can_ask() and self._confirm(probe, deal) is False:
            if deal.get("suspect"):
                self._suspicious(deal)
            else:
                self._mark(deal)
            return
        self._mark(deal)
        self._remember(deal)
        self.journal.add(f"Jó találat (azonnali figyelő): „{deal['text'][:60]}” {format_price(deal['price'])} "
                         f"({deal['site']}, kb. {format_price(deal['profit'])} haszon) – {source}")
        found(deal)
