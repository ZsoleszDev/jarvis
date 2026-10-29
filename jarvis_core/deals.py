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

Csomagbontás: a csomaghirdetéseket (több darab egy áron), amiket a keresés, a figyelt oldalak, a böngészés és az azonnali
figyelő eddig kihagyott, és a csomagkeresések (pl. „ruhacsomag”) találatait darabonként is felméri, ha bekapcsolod: a
modell a címük alapján kiválasztja a legígéretesebbeket, aztán a hirdetés leírásából és fotóiból tételesen felsorolja,
mi van benne; a darabárak az árbázisból vagy a gyors oldalak mostani hirdetéseiből jönnek (ahol nincs ilyen, a modell
becsléséből, óvatosan). Ha darabonként eladva (a posta és a csomagolás nélkül) jóval többet ér, szól róla; a Megvettem
után a darabjai külön eladandó tárgyak lesznek, a vételár az értékük arányában szétosztva.

Gyorsaság: a találatokat a legújabb elöl nézi. Keresésenként megadható, milyen sűrűn nézze (30 perc – 6 óra); a
Vinteden egy keresés legfeljebb kétóránként fut (robot-ellenőrzés), a böngészés viszont a Vinted legújabb hirdetéseit
alapból 20 percenként megnézi, egy oldalt, ahogy egy ember frissíti (friss lista: GLANCE_DEFAULT). Az egész egy
kapcsolóval be- és kikapcsolható (a Munka menü Automata kereskedés fülén); alapból ki van kapcsolva.
"""
from __future__ import annotations

import hashlib
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
from typing import Any, Callable, Iterable

from . import condition as conditions
from . import parts, specs
from .browser import DECLINED, LISTING_PAGE_JS, BrowserError, is_risky
from .feeds import NEWEST, FeedError, chosen_sort, fetch_image, newest_first  # noqa: F401 (NEWEST: innen is)
from .listings import BLOCKED, GONE, SOLD, classify
from .llm import background_calls
from .journal import Journal, day_of
from .pricebook import (SOLD_MIN, TITLE_KEEP, PriceBook, _words, basis_entry, fake_prone, implied_size, independent,
                        is_capacity, listed_days, pace, price_scope, product_key, ram_context, size_conflict, sized,
                        sizes_of, speed_text, unusable, usable, wearable)
from .replay import norm_text
from .selling import (_PARTS_RE, _SALE_RE, _STOPWORDS, _SWAP_RE, _WANTED_RE, ON_SITE, Item, ItemError, Platform, _shared,
                      _tokens, format_price, listing_key, parse_price, prefilter, price_stats, round_price, vision_jpeg)
from .textutil import fold
from .vinted import BUTTON_KINDS, consent_button, elem_key, logged_out, new_elements
from .web import UNTRUSTED_NOTE

# A Vaterán a keresés csak a fix áras („azonnal megvásárolható”) hirdetéseket nézi; a licitekre a licitfigyelő van.
DEAL_SITES = ("vinted", "jofogas", "vatera", "hardverapro")
RESALE_SITES = ("vinted", "jofogas", "hardverapro")   # egy licit viszonteladási árához ezeken keres
MAX_SEARCHES = 20
MAX_PER_SITE = 120         # egy keresés egy oldalán ennyi találatot néz meg (az első oldal egésze; a Hardverapró 100-at mutat)
MIN_PRICES = 5             # az ármozgás figyeléséhez legalább ennyi hasonló ár kell egy körben
DEFAULT_DISCOUNT = 30      # ennyi %-kal a szokásos ár (medián) alatt számít olcsónak
FLOOR = 0.15               # a medián 15%-a alatti ár többnyire nem valódi ár (1 Ft, „ár megegyezés szerint”)
MAX_CHECKS = 8             # egy körben legfeljebb ennyi jelöltet néz meg a modellel (a többi a következő körre marad)
MAX_DEALS = 60             # ennyi találatot őriz meg a listában
MAX_SEEN = 5000
EVERY_CHOICES = (30, 60, 180, 360)   # percben: milyen sűrűn nézi meg egy keresés találatait
DEFAULT_EVERY = 60
SITE_MIN_MINUTES = {"vinted": 120}   # a Vintedet legfeljebb kétóránként (ott a robot-ellenőrzés a kockázat)
# (A legújabb hirdetések elöl: feeds.NEWEST, feeds.newest_first; innen is elérhető.)
MAX_PAGES = 8              # egy körben legfeljebb ennyi jelölt hirdetés oldalát nyitja meg (a többi a következőre)
# A gyors oldalak (Hardverapró, Jófogás) találati oldalát, ha az azonnali figyelő ennyi másodpercen belül már letöltötte,
# a kör nem kéri le újra (feeds.SiteFetcher.listings max_age): ugyanannyi kérésből több idő jut az új hirdetésekre.
LISTING_REUSE = 300
# Ha egy gyors oldal óránkénti kerete elfogyott (vagy átmeneti hálózati hiba volt), azt nem a böngészővel pótolja (a
# keret az oldal kímélése, a böngésző a Vintedé és a Vateráé, és a böngészőben a Hardverapró listája sem a legújabb
# hirdetésekkel elöl jön): a keresés vagy a figyelt oldal ennyi másodperc múlva újra próbálkozik (nem a teljes
# gyakorisága után), a jelöltet pedig a hirdetés oldala nélkül, figyelmeztetéssel méri fel. A böngésző csak akkor veszi
# át, ha az oldal a böngésző nélküli olvasást tiltja, vagy azon nem talál hirdetést (feeds.SiteFetcher.usable).
BUSY_RETRY = 600
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
# Alkuképes hirdetések: ami egy ideje fent van (legalább HAGGLE_DAYS napja, vagy már leárazták), és csak kicsit drágább
# annál, ami még megéri (legfeljebb HAGGLE_MAX-szal), arra érdemes ajánlatot tenni: a régóta fent lévő hirdetés eladója
# többnyire enged. Az árbázisból keresi (ott van, mikor láttuk először, és hogy leárazták-e), HAGGLE_EVERY percenként;
# körönként legfeljebb HAGGLE_CHECKS hirdetés oldalát nyitja meg (elkelt, fix áras, gyanús?), és naponta legfeljebb
# HAGGLE_PER_DAY-ről szól (a többi csak a listában). Csak ott, ahol üzenetben szokás alkudni (a Vinteden ajánlattal).
# Napi számok (tally): hány hirdetést látott, hány volt új, hány jelölt, mi miért esett ki, és hány jó találat: ebből
# látszik, miért nincs találat egy napon. Egy hirdetés egy nap egy számban egyszer számít (legfeljebb TALLY_MAX-ig).
TALLY_MAX = 60000
HAGGLE_SITES = ("hardverapro", "jofogas", "vinted")
HAGGLE_DAYS = 7
HAGGLE_MAX = 0.25
HAGGLE_FRESH = 2 * 86400   # csak amit az utóbbi két napban még fent láttunk
HAGGLE_EVERY = 30
HAGGLE_CHECKS = 2
HAGGLE_PER_DAY = 6
MAX_HAGGLES = 20           # ennyi alkuképes hirdetés lehet a találatok között (a régebbiek kiesnek)
# Érdemes megnézni (bizonytalan ár): ami olcsónak látszott, de kevés hasonló ára volt (sure_price), az nem vész el. A
# tippek közé kerül (legfeljebb MAX_TIPS, TIP_DAYS napig), egy értesítéssel (a telefonon egy nagyon rövid pittyenéssel,
# notice.tip; naponta legfeljebb TIPS_PER_DAY, utána csak a listában), és minden körben újra felméri az árbázisból,
# letöltés nélkül: ha közben elég ár gyűlt össze, és még mindig megéri, rendes jelölt lesz belőle (a hirdetés oldala, a
# modellkérdés, a megérő értesítés).
MAX_TIPS = 60
TIP_DAYS = 3
TIPS_PER_DAY = 100
# Alkuval megérheti (friss hirdetés): ami most kicsit drága, de legfeljebb FRESH_HAGGLE_CUT engedménnyel már megérne
# (sok eladó enged az árból), arról is egy „Érdemes megnézni” tipp jön, „alkuval” címkével és a javasolt ajánlattal (a
# telefonon egy rövid pittyenés; az Alkuképes hirdetések kapcsolója ezt is ki-be kapcsolja). Ezt nem méri fel újra: ez
# neked szól, nézd át, és írj az eladónak. A Vinteden ilyenkor ajánlattételt javasol (OFFER_CUT, rendes jelöltként).
FRESH_HAGGLE_CUT = 0.15
MAX_HAGGLE_DONE = 3000     # ennyi már megnézett hirdetést jegyez meg (ezekről nem szól újra)
DEFAULT_HAGGLE_MESSAGE = "Szia!\nMegvan még? {ár}-ért odaadnád?\nKöszi előre is\nÜdv, Zsolt"
# Vatera-licitfigyelő: alapból óránként nézi, a zárás előtt legfeljebb ennyi órával szól (hogy legyen időd licitálni),
# legalább ennyi haszonnal; egy oldalról legfeljebb EXPLORE_ROWS ismeretlen licitet kap a modell, és ezekből legfeljebb
# EXPLORE_PICKS-nek nézi meg az árát máshol (RESALE_SITES).
MAX_WATCHES = 10
# Licitlista (Vatera): egy friss, 1 Ft-ról induló licitnél a mostani ár még semmit nem mond (nő), de az már most kiderül,
# meddig érdemes licitálni (a szokásos ára szerint: max_bid), és a Vaterán ez az összeg legmagasabb ajánlatként rögtön
# beállítható (a Vatera addig licitál helyetted). Ezért a még nem hamarosan záruló, de megérő licit nem vész el: a
# licitlistára kerül (legfeljebb MAX_AUCTIONS, a hamarabb zárók elöl), egy csendes értesítéssel (naponta legfeljebb
# AUCTION_TIPS_PER_DAY), és a zárás előtt (a figyelő órái szerint, a gép esti leállása előtt) a kör még egyszer megnézi:
# ha még fent van, és nem lépte túl a javasolt összeget, szól (Licit: …). Ha közben egy oldalon látja, hogy a mostani ára
# már a javasolt legmagasabb ajánlat fölött van, lekerül.
MAX_AUCTIONS = 60
AUCTION_TIPS_PER_DAY = 10
WATCH_EVERY = 60
WATCH_HOURS = 6
WATCH_PROFIT = 5000
EXPLORE_ROWS = 20
EXPLORE_PICKS = 3
# Felfedező (figyelt oldalak, keresőszó nélkül): az árbázishoz (vagy a máshol talált árhoz) mérve legalább ennyivel
# olcsóbb, és legalább a megadott haszon jön ki belőle.
EXPLORE_CUT = 0.15
# Olcsó termék: a legkisebb haszon legfeljebb a szokásos ár negyede (legalább CHEAP_MIN_PROFIT), hogy a gyanúsan olcsó
# határ (SUSPECT_RATIO) és a haszon között maradjon ár, ami megéri (profit_floor).
CHEAP_SHARE = 0.25
CHEAP_MIN_PROFIT = 1000
# Gyanúsan olcsó: a szokásos ár SUSPECT_RATIO-ja alatt (a gyakran hamisított márkáknál FAKE_RATIO-ja alatt) többnyire
# átverés, hamisítvány vagy hibás; ezekről nem szól. (A FLOOR alatti ár nem is valódi ár: azt meg sem nézi.)
SUSPECT_RATIO = 0.4
FAKE_RATIO = 0.6
# Biztos ár: egy felfedezett (nem a kereséseddel felmért) termék szokásos ára csak akkor számít, ha legalább SURE_MIN
# hasonló áron alapul, legalább két oldalról (ha csak egy oldalon láttuk, ott legalább SURE_ALONE áron). Ha ennyi nincs,
# előbb a többi oldalon is megnézi, mennyiért adják (oldalanként egy keresés, böngésző nélkül, ahol lehet).
SURE_MIN = 5
MARKET_GROUP_MIN = 3       # egy kapacitás-változat ára ennyi hasonlóból számít (_market_of)
# Ha a most talált hirdetések legalább ennyivel olcsóbbak a korábban látottaknál, lejjebb ment az árszint: azokhoz mér.
FRESH_GAP = 0.05
# Ha egy összehasonlító hirdetés címéből nem derül ki a változat (kapacitás), legfeljebb ennyinek a leírását olvassa el
# egy árkutatásban (a kör vagy a figyelő kéréskeretéből), ha a biztos összehasonlításhoz kell (_clarify).
DETAIL_READS = 2
# Az összehasonlító hirdetések állapota (_vet): egy árkutatásban a legfontosabb (a szokásos árhoz legközelebbi)
# legfeljebb VET_READS összehasonlító leírását is elolvassa, és az eredményt eltárolja (allapotok.json, legfeljebb
# MAX_VETTED hirdetés, a legrégebbiek kiesnek: egy későbbi összehasonlítás ugyanazt nem olvassa el újra). A hibás vagy
# hiányos (pl. kijelzőhibás, töltő nélküli) összehasonlító nem számít a hibátlan ár közé. Egy felfedezett (nem a
# kereséseddel felmért) termék szokásos ára csak akkor biztos, ha legalább VET_MIN összehasonlítóról a leírása szerint
# is kiderült, hogy hibátlan: a címekből ez nem dől el.
VET_READS = 3
VET_MIN = 2
CHEAP_STAYED = 1.0         # napok: egy kilógóan olcsó, hibátlan hasonló, ami legalább ennyi ideje fent van, versenytárs
# Előmelegítés (warm): ráérő időben (az azonnali figyelő üres sorában, a kimaradó keretből, csúcsidőn kívül) a gyakran
# felbukkanó termékek összehasonlítóinak leírását előre elolvassa (egyszerre egyet), a kevés árúaknál csendes órában a
# többi oldalon is megnézi az árát. Így amikor egy friss hirdetés jön, az ára már biztos, és rögtön jöhet az adatlap és a
# csengetés. Gyakori: a friss hirdetései súlyozott száma (a régebbieké WARM_HALF alatt feleződik) legalább WARM_SCORE
# (két hirdetés fél napon belül; utána pár óráig az marad). Egy terméket WARM_RETRY-onként vesz elő (amíg még nem biztos
# az ára, WARM_AGAIN múlva folytatja a következő leírással).
WARM_SCORE = 1.5
WARM_HALF = 12 * 3600
WARM_RETRY = 3 * 3600
WARM_AGAIN = 120
WARM_LOOK = 10             # egy előmelegítésnél legfeljebb ennyi termék közül választ (a legkeresettebbek)
MAX_DEMAND = 500
TIP_RETRY = 1800           # az ellenőrizetlen tippet (nem nyílt meg az adatlap, nem válaszolt a modell) ennyi mp múlva újra
MAX_VETTED = 3000
MAX_COSTS = 40
# A felületen választható pótolni- vagy javítanivalók (más is beírható).
COST_PARTS = ("töltő", "kábel", "kontroller", "kijelző", "akkumulátor")
# Ellenőrzött ajánlat (a sürgős „Megéri”): az adatlapja megnyílt, van leírása (legalább MIN_DETAILS jel), a modell
# válaszolt, és az állapota szerint újraszámolt haszon is megéri (_verdict). Előtte rögtön egy gyors jelzés megy
# („Ígéretes …, ellenőrzés folyamatban”; egy hirdetésről PING_TTL-en belül egyszer). Ha valamelyik nem teljesül, a
# hirdetés ellenőrizetlen marad: az Érdemes megnézni listába kerül, az okkal (_unverified).
MIN_DETAILS = 8
PING_TTL = 6 * 3600
# Ha a modell épp foglalt (pl. egy feltöltés használja), nem válaszolt, vagy nincs is: az adatlap és az újraszámolás
# után akkor is sürgősen szól (minél több csengetés, annál jobb), „a modell még nem nézte meg” jelzéssel; amint lehet, a
# modell utólag megnézi (followups: legfeljebb FOLLOWUP_TTL-ig, FOLLOWUP_TRIES próbával), és ha mégsem jó, szól.
FOLLOWUP_TTL = 6 * 3600
FOLLOWUP_TRIES = 3
FOLLOWUP_BATCH = 2
BASIS_MAX = 15             # a találat ablakában legfeljebb ennyi beszámított és ennyi kizárt összehasonlító
# Árkutatás közben talált hirdetések: ha egy összehasonlító hirdetés a többihez mérve (saját maga nélkül) megéri, önálló
# jelölt lesz (_peers): egy árkutatásból legfeljebb SPAWN_MAX, egy hirdetést SPAWN_TTL-en belül csak egyszer néz meg így,
# és az így talált jelölt a már meglévő összehasonlításhoz mér (maga nem indít újabb árkutatást): nincs végtelen lánc.
SPAWN_MAX = 2
SPAWN_TTL = 12 * 3600
# Ha egy így talált jelölt a kör keretébe már nem fér bele, nem vész el: a következő kör ezekkel kezd (_step_carry;
# legfeljebb CARRY_MAX, CARRY_TTL másodpercig). A többi jelölt úgyis újra előjön a saját oldalán.
CARRY_MAX = 20
CARRY_TTL = 3600
# Egy jó találat eladójának többi hirdetése (seller_bundle): a megnyitott hirdetés oldaláról az eladó „további hirdetései”
# linkje, és a listájáról az, amit külön is haszonnal lehetne továbbadni (az árbázis vagy a kereséseid szerint). Egy közös
# vétel több hasznot hozhat: egy posta (vagy egy átvétel), a Vinteden a vevővédelmi díj (kb. VINTED_FEE_FIXED + a végösszeg
# VINTED_FEE_RATE-je) a végösszegre egyszer, és a közös árra alkudni is lehet (BUNDLE_OFFER_CUT; a Vinteden csomagajánlat).
# Legfeljebb SELLER_ROWS hirdetését nézi; csak akkor szól, ha együtt legalább SELLER_GAIN-nel több a haszon. Egy darab
# meghirdetése kb. LISTING_MINUTES perc munka (fotó, szöveg, feltöltés; Pro feltöltéssel kevesebb).
SELLER_ROWS = 30
SELLER_GAIN = 2000
LISTING_MINUTES = 10
VINTED_FEE_FIXED = 300
VINTED_FEE_RATE = 0.05
BUNDLE_OFFER_CUT = 0.1
SURE_ALONE = 10
# Egy oldalról kevesebb ár is elég, ha szorosan együtt vannak: legalább SURE_TIGHT, és a középső felük (alsó és felső
# negyed) távolsága legfeljebb a középérték TIGHT_SPREAD-szerese (kb. ±15%). A hardver piaca szinte csak a Hardverapró: ott
# ritkán van második oldal.
SURE_TIGHT = 6
TIGHT_SPREAD = 0.3
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
# A becslés megbízhatósága (reliability): nem csak a darabszám, hanem a független árak, az oldalak, a frissesség (ennyi
# napon belül látott ár friss), a szórás, a változat biztonsága és az összehasonlítók ellenőrzött állapota. A gyors
# eladási becslés bizonytalansága (sale_range) ehhez igazodik: ennyivel lehet kevesebb vagy több.
FRESH_DAYS = 7
# Friss összehasonlítás: egy szokásos ár csak akkor elég (enough), ha legalább FRESH_MIN hasonló árat FRESH_DAYS napon
# belül láttunk (vagy most találtunk). A régebben látott árak elavultak lehetnek (a hirdetés azóta elkelt, lejjebb ment az
# árszint): ilyenkor előbb a többi oldalon is megnézi, mennyiért adják most (a kör _resale-je, az azonnali figyelő
# _recheck-je, ráérő időben az előmelegítés), és csak a friss árakkal szól.
FRESH_MIN = 3
SALE_MARGIN = {"magas": 0.05, "közepes": 0.1, "alacsony": 0.2, "megadott": 0.1}
# Az eladás költségei a beállításokból (set_fees): csomagolás (Ft), eladási díj (az eladási ár %-a), egyéb eladási
# költség (Ft, pl. kiemelés), tartalék (az eladási ár %-a, a váratlanra). Ami nincs megadva, az nem csendben nulla: a
# haszonban 0-val szerepel, de a találatnál és az értesítésben látszik, hogy nincs benne (fees_unset).
FEE_NAMES = {"packaging": "csomagolás", "sell_pct": "eladási díj", "sell_fixed": "egyéb eladási költség",
             "reserve_pct": "tartalék"}
FEE_MAX = {"packaging": 20000, "sell_pct": 50, "sell_fixed": 20000, "reserve_pct": 50}
SLOW_OK_ROI = 0.35
SPEED_CHECKS = 2           # egy körben ennyi régebben látott hirdetést néz meg újra (elkelt-e; a Vintedet nem)
# Csomagbontás: a csomaghirdetés (több darab egy áron: „ruhacsomag”, „PC-alkatrészek egyben”, „Lego ömlesztve”)
# darabonként eladva gyakran jóval többet ér, mert a legtöbb vevő nem számolja ki. A csomagokat a modell előbb a címük
# alapján rostálja (körönként legfeljebb BUNDLE_PICKS-et nyit meg), aztán a hirdetés leírásából és első BUNDLE_PHOTOS
# fotójából tételesen felsorolja, mi van benne. A darabár: az árbázis, vagy a gyors oldalak mostani hirdetései
# (csomagonként legfeljebb BUNDLE_LOOKUPS keresés, legalább BUNDLE_MARKET_MIN hasonlóval), óvatosan (safe_sale), de
# legfeljebb a modell becslésének GUESS_CAP-szorosa (ha a modell félreismerte, ne abból jöjjön a haszon); ahol nincs ilyen,
# a modell becslésének GUESS_RATIO-ja. A PIECE_MIN alatti darabokat nem éri meg külön hirdetni: azok egy maradék
# csomagban mennek, az értékük LEFTOVER_RATIO-jáért. Költség: a vétel postája (SHIPPING), és darabonként PIECE_COST
# (csomagolás, hirdetés). Csak akkor szól, ha legalább a megadott haszon (alapból BUNDLE_PROFIT), és a vételár
# legalább BUNDLE_ROI-ja jön ki belőle (sok munka: többet kell hoznia, mint egy darabnak).
BUNDLE_PROFIT = 8000
BUNDLE_ROI = 0.5
PIECE_MIN = 1500
PIECE_COST = 300
LEFTOVER_RATIO = 0.5
GUESS_RATIO = 0.6
GUESS_CAP = 2.0
BUNDLE_MIN_PRICE = 1000    # ez alatt nem valódi ár („1 Ft”, „ár megegyezés szerint”)
BUNDLE_PICKS = 2
BUNDLE_PHOTOS = 4
BUNDLE_LOOKUPS = 2
BUNDLE_MARKET_MIN = 3
MAX_LINES = 15             # ennyi tételt sorol fel a modell (az azonos darabok egy sorban, darabszámmal)
MAX_PIECES = 60            # egy tétel legfeljebb ennyi darab
BUNDLE_INBOX = 40          # ennyi csomag várhat a felmérésre (a legújabbak elöl; a régebbiek kiesnek)
BUNDLE_TRIES = 2           # ha a modell ennyiszer sem válaszolt egy csomagra, kimarad
MAX_BUNDLE_SEARCHES = 8
# Nyugodt szívvel (assess_bundle): az érték legalább BUNDLE_SURE része hirdetési árakból jön (nem becslés), vagy a
# haszon a vételár legalább BUNDLE_SAFE_ROI-ja; BUNDLE_BIG-nél több külön hirdetés sok munka.
BUNDLE_SURE = 0.6
BUNDLE_SAFE_ROI = 1.0
BUNDLE_BIG = 15
# A ruhacsomag nincs köztük: ruhával az automata kereskedés nem foglalkozik (pricebook.wearable).
BUNDLE_PRESETS = (("Lego ömlesztve", "lego ömlesztve", ("jofogas", "vatera")),
                  ("Videojáték-csomag", "videojáték csomag", ("jofogas", "hardverapro", "vatera")),
                  ("PC-alkatrészek egyben", "alkatrészek egyben", ("hardverapro", "jofogas")))
# Csomagra utaló szó (ékezet nélkül): „ruhacsomag”, „csomagban”, „ömlesztve”, „gyűjtemény”, „játékok egyben”,
# „15 db”. A „csomagolás”, a „csomagautomata” nem; a „szett” sem (a „Lego szett” egy termék).
_BUNDLE_RE = re.compile(r"\b\w*csomag(?:ban|ot|kent|om|ja)?\b|\b(?:lot|kotegben|tetelben|omlesztve|omlesztett|"
                        r"gyujtemeny\w*|egyben elado\w*|elado\w* egyben)\b|\b\w{3,}k egyben\b|"
                        r"\btobb (?:db|darab)\b|\b(?:[3-9]|[1-9]\d) ?(?:db|darab|darabos)\b")
DEFAULT_DAY = (7, 23)      # a gép általában ekkor megy (óra): a licitekről még az esti leállás előtt szól
# Böngészés napközben: a gép üzemidejében (DEFAULT_DAY) munkamenetekben dolgozik, ahogy egy ember: egy oldalon kb.
# BROWSE_WORK percig nézelődik (két oldalbetöltés között STEP_PAUSE másodperc), aztán egy rövid SWITCH_PAUSE után a
# következő oldalra vált (BROWSE_ORDER). Csak az az oldal pihen, amelyiken épp dolgozott: legalább BROWSE_REST percig
# (±30%), a Vinted SITE_GAP percig; a Jarvis maga csak akkor áll meg, ha minden bejelölt oldal pihen. Egy munkamenetben
# legfeljebb BROWSE_PAGES oldalt tölt be. Ha egy oldal robot-ellenőrzést kér, aznap már nem nézi. (A Vinteden alapból
# nem munkamenetek, hanem a friss lista pillantásai: GLANCE_DEFAULT.)
BROWSE_WORK = 20
BROWSE_REST = 10
DEFAULT_STEP = (15, 50)
STEP_PAUSE = {"vinted": (45, 120)}
SWITCH_PAUSE = (20, 60)
BROWSE_PAGES = {"vinted": 10}
BROWSE_MAX_PAGES = 30
SITE_GAP = {"vinted": 120}
# Friss lista (pillantás): a Vinteden úgy, ahogy egy ember, aki napközben 20 percenként frissíti a legújabbakat. Az
# indulóoldalát (a legújabb elöl) GLANCE_DEFAULT percenként (±20%, GLANCE_JITTER) egyszer tölti be, a többi oldal
# munkamenetei közé ékelve (mint egy másik fülön), és OPEN_CHANCE eséllyel megnyit egy hirdetést is. Ez váltja a
# kétóránkénti munkamenetet (BROWSE_PAGES oldal kb. 20 perc alatt, utána SITE_GAP perc szünet): óránként nem több kérés,
# csak egyenletesen elosztva, és egy új hirdetés legfeljebb kb. ennyi perc múlva kiderül (eddig akár 2,5 óra). Több
# Vinted-indulóoldalnál felváltva (a legrégebben nézett jön), így a kérések száma ugyanannyi marad. A felületen
# állítható (GLANCE_CHOICES); 0: a régi, kétóránkénti munkamenetek. A pillantásokról óránként egy naplóbejegyzés
# (GLANCE_LOG perc), nem mindegyikről külön.
GLANCE_SITES = ("vinted",)
GLANCE_CHOICES = (0, 10, 15, 20, 30, 60)
GLANCE_DEFAULT = 20
GLANCE_JITTER = (0.8, 1.2)
GLANCE_LOG = 60
OPEN_CHANCE = 0.35         # egy találati oldal után ekkora eséllyel megnyit egy hirdetést (a Vinted kezdőoldalán mindig)
# A modellre váró ismeretlenek: EXPLORE_ROWS-onként egy hívás; a munkamenet végén csak legalább MIN_FLUSH (vagy egy
# licit) miatt tölti be a modellt, a maradék a következő munkamenetre vár (legfeljebb MAX_PENDING).
MIN_FLUSH = 8
MAX_PENDING = 60
# Turbó (a gombja az Indítás mellett): a modellt gyakrabban kérdezi, hogy a megérő hirdetés hamarabb kiderüljön. A
# böngészés ismeretlenjeit már TURBO_ROWS összegyűlése után (vagyis minden oldal után) megnevezteti, nem várja meg az
# EXPLORE_ROWS-t, és a munkamenet végén a maradékot is (MIN_FLUSH nélkül); a modellkapu pedig a tartalék helyet is a
# háttérnek adja, amíg nem dolgozol a Jarvisszal (llm.TURBO_IDLE). Az oldalak kéréskerete nem változik: Turbóban sem
# kér többet a weboldalaktól, csak hamarabb dönt arról, amit letöltött.
TURBO_ROWS = 1
BROWSE_ORDER = ("hardverapro", "jofogas", "vinted", "vatera")
# Két böngészési sor párhuzamosan (ha a Jarvis elindítja a gyors sor szálát, start_fast_lane): a böngészőé (Vinted,
# Vatera) az ágens-szálon, és a gyors (Hardverapró, Jófogás böngésző nélkül, feeds.py) a sajátján. A munkamenet és a
# modellre váró ismeretlenek soronként külön vannak.
LANE_BROWSER, LANE_FAST = "browser", "fast"
SESSION_KEYS = {LANE_BROWSER: "session", LANE_FAST: "fast_session"}
FAST_TICK = 3.0            # a gyors sor szála ilyen sűrűn nézi meg, van-e dolga
BOOK_SAVE = 600            # a gyors sor az árbázist legfeljebb ennyi másodpercenként írja ki (nagy fájl)
# Indulóoldalak: (név, keresőszó vagy teljes cím; üres = a kezdőoldal). Alapból egész kategóriák, a legújabb hirdetésekkel
# elöl: nem egy-egy termékre keres, hanem mindent megnéz, ami újonnan felkerül (amit ismer, az árbázishoz méri, az
# ismeretleneket a modell nevezi meg). A Vinteden az Elektronika (a ruhával nem kereskedik), a Jófogáson a Műszaki cikkek,
# elektronika, a Vaterán a Műszaki cikk és mobil (innen jönnek a friss, 1 Ft-ról induló licitek is), a Hardveraprón a
# hardver egésze és a felkapott kategóriái (2026 októberében ellenőrizve). A felületen bármelyik kikapcsolható, és
# saját is felvehető (egy oldal címe vagy egy keresőszó: lane_add; a gyakoriak egy gombnyomással: LANE_PRESETS).
_HA = "https://hardverapro.hu/aprok/hardver/"
BROWSE_LANES: dict[str, list[tuple[str, str]]] = {
    "vinted": [("Elektronika", "https://www.vinted.hu/catalog?catalog[]=2994&order=newest_first")],
    "hardverapro": [("Minden hardver", _HA + "index.html"), ("Videókártyák", _HA + "videokartya/index.html"),
                    ("Memóriák", _HA + "memoria/index.html"), ("SSD-k", _HA + "merevlemez_ssd/ssd/index.html"),
                    ("Processzorok", _HA + "processzor/index.html"), ("Alaplapok", _HA + "alaplap/index.html")],
    "jofogas": [("Műszaki cikkek, elektronika", "https://www.jofogas.hu/magyarorszag/muszaki-cikkek-elektronika?sp=3")],
    "vatera": [("Műszaki cikk és mobil", "https://www.vatera.hu/muszaki-cikk-es-mobil/index-c12082.html?ob=5&obd=2")],
}
# Saját indulóoldal egy gombnyomással (keresőszó, minden bejelölt oldalon): a korábbi beépített termékkeresések.
LANE_PRESETS = ("Lego", "PlayStation", "Nintendo Switch", "AirPods", "iPhone", "Dyson", "Makita")
MAX_OWN_LANES = 30
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
_NO_REVIEWS_RE = re.compile(r"nincs(enek)? (meg )?ertekeles\w*|meg nincs ertekeles\w*|no reviews yet|\b0 ertekeles")
# Alku (ékezet nélkül): a fix áras hirdetésre nem érdemes ajánlatot tenni; az alkuképes, a sürgős, a költözés miatti
# eladó többnyire enged.
_FIRM_RE = re.compile(r"\b(fix ?ar\w*|nem alkukepes|alku nincs|nincs alku|alkut nem|nem alkuszom|ne alkudj\w*|"
                      r"arbol nem engedek|nem engedek az arbol|csak ennyiert)\b")
_FLEX_WORDS = (("alkukepes", "alkuképes"), ("alkudhato", "alkuképes"), ("alku van", "alkuképes"),
               ("alku lehet", "alkuképes"), ("iranyar", "irányár"), ("surgos", "sürgős"), ("koltoz", "költözés miatt"),
               ("kell a hely", "kell a hely"), ("gyorsan el kell", "sürgős"))

CHECK_SYSTEM = (
    "Egy használtcikk-kereskedőnek segítesz. Megkapod, mit keres, és egy hirdetés címét, szövegét és árát. "
    "ok=true, ha ez tényleg a keresett termék (nem kiegészítő, nem alkatrész, nem más típus), egy darab, és eladó (nem "
    "keres vagy cserél). Az állapota (hiba, hiányzó tartozék) ettől még nem ok=false: azt a condition részben add meg. "
    "suspicious=true, ha átverésnek vagy hamisítványnak látszik: túl szép, hogy igaz legyen, előre fizetést, más oldalon "
    "vagy alkalmazásban való kapcsolatfelvételt vagy futárcéget kér, sürget, vagy a leírás nem illik a termékhez. Ha a "
    "hirdetés fotóit is megkapod: ok=false, ha nem a keresett termék van rajta; suspicious=true, ha a fotó letöltött "
    "(webshop- vagy katalógus-) képnek látszik a saját fotó helyett, vagy hamisítványnak tűnik; a fotón látott sérülést "
    "vagy hiányt a condition részben jelezd. " + conditions.CONDITION_RULES + " reason: egy rövid mondat magyarul. "
    + UNTRUSTED_NOTE)
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
    "alkatrész, a hibás, a csomag és a hamisítványnak látszó nem ilyen. A ruhát, a cipőt és a divatárut hagyd ki: azzal nem "
    "kereskedünk. reason: egy rövid mondat magyarul. " + UNTRUSTED_NOTE)
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
    "properties": {"ok": {"type": "boolean"}, "suspicious": {"type": "boolean"}, "reason": {"type": "string"},
                   "condition": conditions.CONDITION_SCHEMA},
    "required": ["ok", "suspicious", "reason", "condition"],
}
CHECK_MAX_TOKENS = 700     # az állapot szempontonként, idézettel
# A hirdetés leírása szerint csak alkatrésznek jó (vagy csak a doboza eladó): nem egy működő darab.
_PARTS_ONLY_RE = re.compile(r"\b(alkatresz\w*|for parts|csak (a )?doboz\w*|ures doboz\w*)\b")
BUNDLE_PICK_SYSTEM = (
    "Egy használtcikk-kereskedőnek segítesz csomagokat (több darab egy áron) felmérni: ha darabonként eladja őket, "
    "többet kaphat érte. Megkapod a csomaghirdetéseket (számozva, árral). Válaszd ki legfeljebb kettőt, amelyik "
    "darabonként eladva valószínűleg jóval többet ér, mint a csomag ára: keresett vagy márkás darabok (pl. Lego, "
    "videojátékok, konzolok és kiegészítőik, PC-alkatrészek, szerszámok), elég darabbal. Ne válaszd: az egy darabos "
    "terméket (pl. egy Lego szett), a keresőhirdetést, a hibásat, a ruhát és a cipőt (azzal nem kereskedünk), és a "
    "tömegárut, amiből egy darab csak pár száz forintot ér (régi könyvek, apró játékok). Ha egyik sem ilyen, üres "
    "listát adj. reason: egy rövid mondat magyarul. " + UNTRUSTED_NOTE)
BUNDLE_PICK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"picks": {"type": "array", "items": {"type": "integer"}}, "reason": {"type": "string"}},
    "required": ["picks", "reason"],
}
BUNDLE_SYSTEM = (
    "Egy használtcikk-kereskedőnek segítesz egy csomaghirdetést (több darab egy áron) felmérni: mennyit érne, ha "
    "darabonként adná el. Megkapod a hirdetés címét, leírását, árát, és ha vannak, a fotóit. Sorold fel tételenként, "
    "mi van benne (legfeljebb 15 sor; az azonos darabokat egy sorba, darabszámmal). Minden sorhoz: name (rövid magyar "
    "név, a márkával és a típussal, ha kiderül), query (keresőszó, amivel egy használtpiacon megtalálni a hasonlót: "
    "márka és típus, 2–5 szó, pl. „Lego 42115”, „Nike Air Force 1”, „FIFA 23 PS4”), count (darabszám), estimate (egy "
    "darab reális ára forintban, amennyiért egy magyar használtpiacon – Vinted, Jófogás, Hardverapró – gyorsan elkel, "
    "az állapotát is nézve; légy óvatos). Csak azt sorold fel, ami a leírásból vagy a fotókról tényleg kiderül; ha a "
    "darabszám nem derül ki, óvatosan becsüld. bundle=false, ha ez nem csomag (egy termék, vagy csak egy darab "
    "eladó). suspicious=true, ha átverésnek látszik (letöltött képek, túl szép, hogy igaz legyen, előre fizetést vagy "
    "más oldalon való kapcsolatfelvételt kér). reason: egy rövid mondat magyarul. " + UNTRUSTED_NOTE)
BUNDLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "bundle": {"type": "boolean"},
        "lines": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "query": {"type": "string"}, "count": {"type": "integer"},
            "estimate": {"type": "integer"}}, "required": ["name", "query", "count", "estimate"]}},
        "suspicious": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["bundle", "lines", "suspicious", "reason"],
}


def _glance_minutes(value: Any) -> int:
    """A friss lista gyakorisága percben (GLANCE_CHOICES; ami nem ezek közül való, vagy hiányzik: GLANCE_DEFAULT)."""
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return GLANCE_DEFAULT
    return minutes if minutes in GLANCE_CHOICES else GLANCE_DEFAULT


def _glance_text(name: str, log: dict[str, Any], end: float) -> str:
    """A friss lista egy órája a naplóba: „09:02–10:01 Vinted, a legújabbak: 3 pillantás, 288 hirdetés (61 új)”."""
    clock = lambda t: time.strftime("%H:%M", time.localtime(t))  # noqa: E731
    text = (f"{clock(float(log.get('start') or end))}–{clock(end)} {name}, a legújabbak: {int(log.get('looks') or 0)} "
            f"pillantás, {int(log.get('rows') or 0)} hirdetés ({int(log.get('fresh') or 0)} új)")
    problems = log.get("problems") or {}
    if problems:
        text += "; akadály: " + "; ".join(f"{reason} ({n}×)" for reason, n in
                                          sorted(problems.items(), key=lambda kv: -kv[1])[:3])
    return text + "."


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


# A licit mostani ára az adatlapján („Aktuális ár: 12 000 Ft”, „Jelenlegi licit: 12.000 Ft”, „Legmagasabb ajánlat …”);
# a kikiáltási (kezdő) ár nem ez.
_AUCTION_PRICE_RE = re.compile(r"\b(?:aktualis|jelenlegi|legmagasabb)\s+(?:ar|licit|ajanlat)\w*\W{0,5}"
                               r"(\d{1,3}(?:[\s.]\d{3})+|\d+)\s*(?:ft|huf)\b")


def auction_price(page: dict[str, Any]) -> int | None:
    """A licit mostani ára a licit oldaláról (a szövegéből vagy a rövid feliratokból); None, ha nem olvasható le."""
    text = fold(" ".join([str(page.get("text") or ""), *[str(item) for item in page.get("short") or []]]))
    match = _AUCTION_PRICE_RE.search(text)
    return int(re.sub(r"\D", "", match.group(1))) if match else None


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


def pays(search: dict[str, Any], price: int, reference: int, fee: int = 0) -> bool:
    """Megéri-e: a szokásos árnál a megadott %-kal olcsóbb, vagy legalább a megadott haszon jön ki belőle (és
    egyáltalán hoz hasznot); nem gyanúsan olcsó (FLOOR), és a megadott ársávban van. fee: a vevő díja (buy_fee): a
    %-ot és a hasznot a díjjal együtt fizetett árral számolja."""
    if price < reference * FLOOR:
        return False
    if (search.get("min_price") and price < search["min_price"]) or \
            (search.get("max_price") and price > search["max_price"]):
        return False
    profit = expected_profit(reference, price) - fee
    if profit <= 0:
        return False
    by_percent = price + fee <= reference * (100 - int(search.get("discount") or DEFAULT_DISCOUNT)) / 100
    return by_percent or bool(search.get("min_profit") and profit >= int(search["min_profit"]))


def highest_ok(search: dict[str, Any], reference: int, platform: str = "") -> int | None:
    """A legmagasabb ár, ami ennél a keresésnél még megéri (pays; a platform vevői díjával együtt): ennyit érdemes
    ajánlani vagy licitálni. None, ha a megadott ársávban nincs ilyen. Az ár csökkenésével csak jobb lesz, ezért
    felezéssel keresi meg a határt."""
    low = max(math.ceil(reference * FLOOR), int(search.get("min_price") or 0), 1)
    high = int(search.get("max_price") or reference)
    if high < low or not pays(search, low, reference, buy_fee(platform, low)):
        return None
    while low < high:
        middle = (low + high + 1) // 2
        if pays(search, middle, reference, buy_fee(platform, middle)):
            low = middle
        else:
            high = middle - 1
    return low


def profit_floor(reference: int, min_profit: int) -> int:
    """A legkisebb haszon egy termékre: a megadott, de olcsó terméknél legfeljebb a szokásos ár CHEAP_SHARE-e (legalább
    CHEAP_MIN_PROFIT). Különben egy 10 000 Ft-os terméknél (9 000-ért eladva, 1 500 posta, 5 000 haszon: legfeljebb
    2 500 Ft-ért) semmi nem menne át, hiszen 4 000 Ft alatt már gyanúsan olcsó (SUSPECT_RATIO)."""
    return int(min(min_profit, max(CHEAP_MIN_PROFIT, round(reference * CHEAP_SHARE))))


def explore_top(reference: int, min_profit: int, low: int | None = None) -> int:
    """Felfedező: a legmagasabb ár (vagy licit), ami még megéri: legalább EXPLORE_CUT-tal a szokásos ár alatt, és
    legalább min_profit haszonnal (olcsó terméknél arányosan kevesebbel: profit_floor; az óvatos eladási áron eladva, a
    postával; safe_sale)."""
    floor = profit_floor(reference, min_profit)
    return math.floor(min(safe_sale(reference, low) - SHIPPING - floor, reference * (1 - EXPLORE_CUT)))


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
                             "n": stats["n"], "sites": sorted({sample[1] for sample in kept if sample[1]}),
                             "range": [stats["min"], stats["max"]]}
    sold = [sample[0] for sample in kept if len(sample) > 2 and sample[2]]
    if len(sold) >= SOLD_MIN:
        found["sold"] = {"n": len(sold), "median": round_price(statistics.median(sold))}
    found["sure"] = stats["confidence"] == "megbízható" and sure_price(found)
    return found


def band_of(found: dict[str, Any]) -> list[int] | None:
    """A kínálati ársáv (a hasonlók alsó és felső negyede), ha tudjuk."""
    try:
        return [int(found["p25"]), int(found["p75"])] if found.get("p25") and found.get("p75") else None
    except (TypeError, ValueError):
        return None


def careful_low(found: dict[str, Any]) -> int | None:
    """Az óvatos eladási árhoz (safe_sale) a felső határ: a hasonlók alsó negyede, és ha tudjuk, mennyiért kínálták az
    eltűnt (talán elkelt) hasonlókat (sold), legfeljebb az. Ha most is fent van egy kilógóan olcsó, a leírása szerint
    ugyanilyen és hibátlan hirdetés (cheap, _cheap), az versenytárs: a vevő azt találja meg először, így legfeljebb a
    kettő közti középár (amíg az fent van, ennyiért kel el gyorsan)."""
    low, sold = found.get("p25"), (found.get("sold") or {}).get("median")
    candidates = [int(v) for v in (low, sold) if v]
    cheap = (found.get("cheap") or {}).get("p")
    if cheap and candidates:
        candidates.append(int(round((int(cheap) + min(candidates)) / 2)))
    return min(candidates) if candidates else None


def spread_of(found: dict[str, Any]) -> float | None:
    """Mennyire szórnak a hasonlók árai: (felső negyed − alsó negyed) / középérték; None, ha nem tudni."""
    try:
        p25, p75, median = int(found["p25"]), int(found["p75"]), int(found.get("price") or found.get("median"))
    except (KeyError, TypeError, ValueError):
        return None
    return round((p75 - p25) / median, 2) if median > 0 and p75 >= p25 else None


def market_info(found: dict[str, Any] | None) -> dict[str, Any]:
    """A piaci becslés adatai, amik a találathoz is kellenek (a megbízhatósághoz és a felülethez): az azonosított
    változat (identity), ha a változat nem derül ki (variant_note), hány összehasonlító ellenőrzötten hibátlan
    (clean_n), és a kilógóan olcsó versenytárs (competitor, _cheap_label)."""
    if not found:
        return {}
    out = {"identity": found.get("identity") or None, "variant_note": found.get("variant_note") or None,
           "clean_n": (found.get("vetted") or {}).get("clean"), "competitor": found.get("cheap") or None}
    return {k: v for k, v in out.items() if v is not None}


def reliability(deal: dict[str, Any], now: float) -> dict[str, Any]:
    """Mennyire megbízható a becslés (nem csak a darabszám): {level: magas | közepes | alacsony, why: ami ellene szól,
    good: ami mellette}. Szempontok: hány hasonló ár (a független: egy hirdetés egyszer, egy eladótól legfeljebb kettő),
    hány oldalról, mennyire frissek (FRESH_DAYS), mennyire szórnak, mennyire biztos a változat (variant_note, a közelítő
    összehasonlítók aránya), és hány hasonló állapota ellenőrzött (clean_n). A hirdetési árak kínálati árak: egy eltűnt
    hirdetés nem bizonyítja, hogy annyiért elkelt."""
    basis = deal.get("basis") or {}
    if basis.get("given"):                     # a keresésed szokásos ára: te adtad meg, vagy hagytad jóvá
        return {"level": "megadott", "why": [], "good": ["a szokásos árat te adtad meg vagy hagytad jóvá"]}
    used = [entry for entry in basis.get("used") or [] if entry.get("p")]
    n = int(basis.get("n_used") or len(used) or deal.get("similar") or 0)
    score, why, good = 0, [], []
    if n >= 8:
        score += 2
        good.append(f"{n} hasonló ár")
    elif n >= 5:
        score += 1
        good.append(f"{n} hasonló ár")
    else:
        why.append(f"csak {n} hasonló ár" if n else "nincs mihez mérni")
    sites = {entry.get("s") for entry in used if entry.get("s")} or set(deal.get("sites") or [])
    if len(sites) >= 2:
        score += 1
        good.append(f"{len(sites)} oldalról")
    elif sites:
        why.append("egy oldalról")
    dated = [entry for entry in used if entry.get("src") == "most" or entry.get("seen")]
    if dated:
        fresh = sum(1 for entry in dated if entry.get("src") == "most"
                    or now - float(entry["seen"]) <= FRESH_DAYS * 86400) / len(dated)
        if fresh >= 0.6:
            score += 1
            good.append("friss árak")
        elif fresh < 0.3:
            score -= 1
            why.append(f"a hasonló árak nagy része {FRESH_DAYS} napnál régebbi")
    spread = deal.get("spread")
    band = deal.get("band") or []
    if spread is None and len(band) == 2 and int(deal.get("median") or 0) > 0:
        spread = (int(band[1]) - int(band[0])) / int(deal["median"])
    if spread is not None:
        if spread <= 0.15:
            score += 1
            good.append("szorosak az árak")
        elif spread > WIDE_SPREAD:
            score -= 1
            why.append("nagyon szórnak az árak")
    approx = sum(1 for entry in used if entry.get("approx"))
    if used and approx * 2 > len(used):
        score -= 1
        why.append(f"a hasonlók többsége csak közelítő ({approx} a {len(used)}-ból)")
    if deal.get("variant_note"):
        score -= 1
        why.append(str(deal["variant_note"]))
    clean = deal.get("clean_n")
    if clean is not None:
        if int(clean) >= VET_MIN:
            score += 1
            good.append(f"{clean} hasonló a leírása szerint is hibátlan")
        else:
            why.append("a hasonlók állapota a leírásuk szerint nagyrészt nem ismert")
    level = "magas" if score >= 5 else "közepes" if score >= 2 else "alacsony"
    return {"level": level, "why": why, "good": good}


def fresh_count(entries: list[dict[str, Any]], now: float) -> int:
    """Hány összehasonlító ár friss: most találtuk (src „most”), vagy FRESH_DAYS napon belül láttuk (seen)."""
    return sum(1 for entry in entries if entry.get("src") == "most"
               or (entry.get("seen") is not None and now - float(entry["seen"]) <= FRESH_DAYS * 86400))


def stale_text(searched: bool = False) -> str:
    """Miért nem elég a szokásos ár, ha a hasonló árak régiek (fresh_count): searched, ha már most is keresett."""
    if searched:
        return f"most kevés hasonló hirdetést találtam, a többi hasonló árat {FRESH_DAYS} napnál régebben láttam"
    return f"a hasonló árak nagy részét {FRESH_DAYS} napnál régebben láttam (előbb a mostani árakat nézem meg)"


def sale_range(sale: int, median: int, level: str) -> list[int]:
    """A gyors eladási becslés sávja a megbízhatóság szerint (SALE_MARGIN): ennyivel lehet kevesebb vagy több (a felső
    vége legfeljebb a kínálati ár mediánja)."""
    margin = SALE_MARGIN.get(level, SALE_MARGIN["alacsony"])
    return [round_price(sale * (1 - margin)), round_price(min(sale * (1 + margin), max(median, sale)))]


def buy_fee(platform: str, price: int) -> int:
    """A vevő díja a vételnél: a Vinteden a vevővédelmi díj (kb. VINTED_FEE_FIXED + az ár VINTED_FEE_RATE-je), máshol 0."""
    return int(round(VINTED_FEE_FIXED + VINTED_FEE_RATE * price)) if platform == "vinted" else 0


def before_fee(platform: str, top: int) -> int:
    """A legmagasabb vételár, aminél a vevő díjával (buy_fee) együtt sem fizetsz többet top-nál."""
    if platform != "vinted" or top <= 0:
        return top
    price = int(math.floor((top - VINTED_FEE_FIXED) / (1 + VINTED_FEE_RATE)))
    while price > 0 and price + buy_fee(platform, price) > top:
        price -= 1
    return max(price, 0)


def assess(deal: dict[str, Any]) -> dict[str, Any]:
    """Nyugodt szívvel megvehető-e: level „biztos” (ellenőrzött ajánlat: verified; legalább SAFE_ROI haszon a befektetett
    pénzhez képest, nem szórnak nagyon az árak, nem lassan kelő, és az állapotában sincs figyelmeztetés), „jó”, vagy
    „kockázatos” (RISKY_ROI alatti haszon, vagy lassan kelő, kevés haszonnal). A magas számított haszon magában nem
    „biztos”: az ellenőrizetlen (az adatlapja, az állapota nincs megnézve) legfeljebb „jó”. why: a kockázatok és a
    figyelmeztetések, good: ami mellette szól (a felületre és az értesítésbe)."""
    price = int(deal.get("offer") or deal["price"])
    roi = int(deal.get("profit") or 0) / price if price > 0 else 0.0
    risks, cautions, good = [], [], []
    if not deal.get("auction"):                # a licit mostani ára még nő: ott a haszonráta nem mond sokat
        if roi < RISKY_ROI:
            risks.append(f"kicsi a haszon a befektetett pénzhez képest ({round(roi * 100)}%)")
        elif roi >= SAFE_ROI:
            good.append(f"kb. {round(roi * 100)}% becsült haszon")
    if deal.get("pace") == "lassú":
        (cautions if roi >= SLOW_OK_ROI else risks).append("lassan kel el (a hasonlók sokáig fent maradnak)")
    elif deal.get("pace") == "gyors":
        good.append("gyorsan kel el")
    spread = deal.get("spread")
    if spread is not None and spread > WIDE_SPREAD:
        cautions.append("nagyon szórnak a hasonlók árai")
    if deal.get("sold"):
        good.append(f"{deal['sold']['n']} eltűnt (talán elkelt) hasonló alapján is")
    if deal.get("similar") and len(deal.get("sites") or []) >= 2:
        good.append(f"{deal['similar']} hasonló, {len(deal['sites'])} oldalon")
    cond = deal.get("condition") or {}
    if cond:
        state = lambda aspect: (cond.get(aspect) or {}).get("state") or conditions.UNKNOWN  # noqa: E731
        if state("function") == "működik":
            good.append("a leírása szerint hibátlanul működik")
        elif deal.get("verified") and state("function") == conditions.UNKNOWN:
            cautions.append("a leírásból nem derül ki, hogy hibátlanul működik")
        if state("display") == "karcos":
            cautions.append("karcos a kijelzője")
        if "doboz" in (cond.get("missing") or []):
            cautions.append("doboz nélkül")
        if state("warranty") == "van":
            good.append("garanciális")
    if deal.get("cond_band"):
        cautions.append(f"hasonló hibájúakhoz mértem ({', '.join(deal['cond_band']['issues'])})")
    for part in deal.get("cost_parts") or []:
        if part.get("repair"):
            cautions.append(f"{part['what']} levonva (kb. {format_price(part['amount'])})")
    level = ("kockázatos" if risks else
             "biztos" if not cautions and deal.get("verified") and (deal.get("auction") or roi >= SAFE_ROI) else "jó")
    return {"level": level, "why": risks + cautions, "good": good}


def safety_text(safety: dict[str, Any] | None) -> str:
    """„Alacsony kockázat: kb. 32% becsült haszon, gyorsan kel el.” / „Jó vétel, de figyelj: nagyon szórnak az árak.”
    (A „biztos” szint sem ígéret: a haszon hirdetési árakból becsült.)"""
    if not safety:
        return ""
    level, why, good = safety.get("level"), safety.get("why") or [], safety.get("good") or []
    if level == "biztos":
        return "Alacsony kockázat" + (f": {', '.join(good)}." if good else ".")
    if level == "kockázatos":
        return "Kockázatos: " + "; ".join(why) + "."
    return "Jó vétel" + (f", de figyelj: {'; '.join(why)}." if why else (f": {', '.join(good)}." if good else "."))


def valuation_text(deal: dict[str, Any]) -> str:
    """A becslés az értesítésbe (deal vagy az azonnali figyelő találata): a gyors eladási ár sávja, a javasolt
    legmagasabb vételár (minden költséggel), a becslés megbízhatósága (miért nem magas), és ami a költségek közül nincs
    megadva (0 Ft-tal számolva). „” ha nincs mit mondani."""
    bits = []
    span = deal.get("resale_range") or []
    if len(span) == 2:
        bits.append(f"gyors eladásnál kb. {format_price(int(span[0]))}–{format_price(int(span[1]))} (becslés)")
    if deal.get("max_buy") is not None and not deal.get("auction"):
        bits.append(f"legfeljebb {format_price(int(deal['max_buy']))}-ért érdemes megvenni (minden költséggel)")
    rel = deal.get("reliability") or {}
    if rel.get("level") == "megadott":
        bits.append("a szokásos árat te adtad meg (vagy hagytad jóvá)")
    elif rel.get("level"):
        bits.append(f"a becslés megbízhatósága {rel['level']}"
                    + (f" ({'; '.join(rel['why'][:2])})" if rel.get("why") and rel["level"] != "magas" else ""))
    if deal.get("fees_unset"):
        bits.append(f"nincs megadva: {', '.join(deal['fees_unset'])} (0 Ft-tal számoltam)")
    if not bits:
        return ""
    text = "; ".join(bits)
    return f" {text[0].upper()}{text[1:]}."


VERIFIED_TEXT = ("Ellenőrizve: az adatlapja megnyílt, a leírása és az állapota alapján újraszámoltam, és a modell is "
                 "átnézte. A haszon hirdetési árakból becsült, nem garancia.")


def _not_for_sale(text: str) -> bool:
    """Kereső- vagy cserehirdetés (valaki venni vagy cserélni akar)."""
    folded = fold(text or "")
    return bool(_WANTED_RE.search(folded) or (_SWAP_RE.search(folded) and not _SALE_RE.search(folded)))


def bundle_like(text: str) -> bool:
    """Csomaghirdetésnek látszik-e (több darab egy áron), és nem kereső- vagy cserehirdetés."""
    return not _not_for_sale(text) and bool(_BUNDLE_RE.search(fold(text or "")))


def bundle_value(lines: list[dict[str, Any]], price: int) -> dict[str, Any]:
    """Mennyit ér egy csomag darabonként eladva (a beárazott tételekből: count, unit, source „piac”/„becslés”): a
    PIECE_MIN alatti darabok egy maradék csomagban (LEFTOVER_RATIO), a többi egyenként. value: az óvatos eladási érték,
    listings: hány hirdetés kell hozzá, profit: a vételár, a posta (SHIPPING) és darabonként PIECE_COST nélkül, sure: az
    érték mekkora része jön hirdetési árakból (nem becslésből), top: a legértékesebb darab része az értékből (ha nagy, a
    haszon egyetlen darabon múlik: ha a modell azt félreismerte, nincs haszon)."""
    big = [line for line in lines if line["unit"] >= PIECE_MIN]
    small = [line for line in lines if 0 < line["unit"] < PIECE_MIN]
    worth = lambda group: sum(line["unit"] * line["count"] for line in group)  # noqa: E731
    leftover = int(round(worth(small) * LEFTOVER_RATIO))
    value = worth(big) + leftover
    listings = sum(line["count"] for line in big) + (1 if leftover else 0)
    sure = worth([line for line in big if line["source"] == "piac"]) + \
        worth([line for line in small if line["source"] == "piac"]) * LEFTOVER_RATIO
    return {"value": value, "leftover": leftover, "small": sum(line["count"] for line in small),
            "listings": listings, "profit": int(value - price - SHIPPING - listings * PIECE_COST),
            "sure": round(sure / value, 2) if value else 0.0,
            "top": round(max((line["unit"] for line in big), default=0) / value, 2) if value else 0.0}


def assess_bundle(deal: dict[str, Any]) -> dict[str, Any]:
    """Egy csomag nyugodt szívvel megvehető-e (mint az assess): „biztos”, ha az értéke nagyrészt hirdetési árakból
    jön (BUNDLE_SURE), vagy a haszon a vételár legalább BUNDLE_SAFE_ROI-ja, és nincs figyelmeztetés; „kockázatos”, ha
    a darabárak többsége csak becslés, és a haszon sem nagy; különben „jó”."""
    bundle, price = deal["bundle"], int(deal["price"])
    roi = int(deal.get("profit") or 0) / price if price > 0 else 0.0
    risks, cautions, good = [], [], []
    if bundle["sure"] < 0.3 and roi < BUNDLE_SAFE_ROI:
        risks.append("a darabárak többsége csak becslés, és nem hozza a vételár dupláját")
    elif bundle["sure"] < BUNDLE_SURE:
        cautions.append("a darabárak egy része csak becslés")
    else:
        good.append(f"az érték {round(bundle['sure'] * 100)}%-a mostani hirdetési árakból")
    if bundle["listings"] > BUNDLE_BIG:
        cautions.append(f"sok munka: {bundle['listings']} külön hirdetés")
    if bundle["top"] >= 0.6 and len(bundle["lines"]) > 1:
        cautions.append("az érték nagy része egy tételen múlik: nézd meg a fotón, tényleg az-e")
    if roi >= BUNDLE_ROI:
        good.insert(0, f"kb. {round(roi * 100)}% becsült haszon")
    level = ("kockázatos" if risks else
             "biztos" if not cautions and (roi >= BUNDLE_SAFE_ROI or bundle["sure"] >= BUNDLE_SURE) else "jó")
    return {"level": level, "why": risks + cautions, "good": good}


def bundle_parts(deal: dict[str, Any], paid: int, shipping: int = 0) -> list[dict[str, Any]]:
    """Egy megvett csomag darabjai eladási tárgynak (tételenként egy, a PIECE_MIN alattiak együtt egy maradék
    csomagban): a név, a keresőszó, hogy mennyiért adnád tovább, és a vételár meg a posta, az értékük arányában
    szétosztva (forintra pontosan: az összegük a kifizetett összeg)."""
    bundle = deal["bundle"]
    title = str(deal.get("title") or deal["text"])[:60]
    parts: list[dict[str, Any]] = []
    for line in bundle["lines"]:
        if line["unit"] < PIECE_MIN:
            continue
        count = int(line["count"])
        parts.append({"prompt": f"{line['name']}" + (f", {count} db" if count > 1 else "") + f", {line['unit']} Ft",
                      "search_query": line.get("query") or "", "target_price": line["unit"] * count,
                      "trade_note": f"Csomagból („{title}”): " + (f"{count} db, darabonként kb. {line['unit']} Ft"
                                                                   if count > 1 else f"kb. {line['unit']} Ft"),
                      "weight": line["unit"] * count})
    if bundle.get("leftover"):
        parts.append({"prompt": f"Apróságok egyben a csomagból ({bundle.get('small') or 0} db), {bundle['leftover']} Ft",
                      "search_query": "", "target_price": bundle["leftover"],
                      "trade_note": f"Csomagból („{title}”): a {PIECE_MIN} Ft alatti darabok egyben",
                      "weight": bundle["leftover"]})
    if not parts:
        return []
    total = sum(part["weight"] for part in parts) or 1
    for field_name, amount in (("bought_price", paid), ("bought_shipping", shipping)):
        shares = [amount * part["weight"] // total for part in parts]
        shares[max(range(len(parts)), key=lambda i: parts[i]["weight"])] += amount - sum(shares)
        for part, share in zip(parts, shares):
            part[field_name] = share
    for part in parts:
        part.pop("weight")
    return parts


def sure_price(reference: dict[str, Any]) -> bool:
    """Elég biztos-e egy szokásos ár: legalább SURE_MIN hasonló áron alapul, két oldalról (vagy egy oldalról legalább
    SURE_ALONE áron, vagy legalább SURE_TIGHT szorosan együtt lévő áron)."""
    n, sites = int(reference.get("n") or 0), reference.get("sites") or []
    if n < SURE_MIN:
        return False
    if len(sites) >= 2 or n >= SURE_ALONE:
        return True
    spread = spread_of(reference)
    return n >= SURE_TIGHT and spread is not None and spread <= TIGHT_SPREAD


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


def offer_price(search: dict[str, Any], price: int, reference: int, platform: str = "") -> int | None:
    """Vinted-ajánlat: ha a kért ár kicsit több annál, ami még megéri (highest_ok, a vevővédelmi díjjal együtt), de
    legfeljebb OFFER_CUT-tal, ennyit ajánlj (kerek százasra lefelé). None, ha a kért ár már most is megéri, vagy túl
    nagy engedmény kellene."""
    top = highest_ok(search, reference, platform)
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


class Deferred(Exception):
    """Az értékelés most nem folytatható (Szünet, vagy elfogyott az azonnali figyelő kéréskerete): a még le nem zárt
    hirdetések megmaradnak, és később újra sorra kerülnek (appraise). sites: melyik oldal(ak) kerete fogyott el (ezekre
    vár az a hirdetés, aminél megállt: row)."""

    def __init__(self, reason: str = "", sites: Iterable[str] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.sites = tuple(sites)
        self.row: dict[str, Any] | None = None
        self.extra: list[dict[str, Any]] = []   # az árkutatás közben talált, félbemaradt jelöltek: ezek is várnak


class _Later(BrowserError):
    """Egy gyors oldal most nem olvasható böngésző nélkül, mert elfogyott az óránkénti kerete (vagy átmeneti hálózati
    hiba volt), és ezt nem a böngésző pótolja (BUSY_RETRY): ami ezen múlt, később kerül sorra."""


class _Round:
    """Egy kör állapota: a böngésző szálán sorra kerülő lépések (work), a modell-sor, a keret (hány hirdetés oldalát
    nyithatja meg, hányszor kérdezheti a modellt), és az összefoglaló."""

    def __init__(self, check: Callable[[], None], notify: Callable[..., None], lane: ModelLane | None) -> None:
        self.check, self.notify, self.lane = check, notify, lane
        self.work: deque[Callable[[], None]] = deque()
        self.pages, self.checks, self.resale, self.blocked = MAX_PAGES, MAX_CHECKS, EXPLORE_PICKS, False
        self.bundle_picks = BUNDLE_PICKS       # ennyi csomag oldalát nyithatja meg (a modell választja ki)
        self.found = self.done = self.bundled = self.haggles = 0
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
        self.tips: list[str] = []              # az ebben a körben ellenőrzött „Érdemes megnézni” tippek (_step_tips)
        # minden lépés után: a közben gyűlt tipp-értesítések rögtön kimennek (DealHunter.flush_tips), nem a kör végén
        self.flush: Callable[[], None] = lambda: None
        self.carry: list[str] = []             # az ebben a körben sorra vett, korábbról átvitt jelöltek (_step_carry)

    def fetch_ahead(self, fetcher: Any, url: str, platform: str) -> None:
        """Egy gyors oldal letöltése a háttérben (ha még nem indult el, és az oldal most így olvasható)."""
        if self.pool is not None and fetcher is not None and url not in self.prefetch and fetcher.usable(platform):
            self.prefetch[url] = self.pool.submit(fetcher.listings, url, platform, self.check, LISTING_REUSE)

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
                self.flush()
                continue
            if self.work:
                self.work.popleft()()
                self.flush()
                continue
            if self.lane is not None and self.lane.busy:
                self.lane.deliver(wait=0.3)
                continue
            return


_SIZE_GAP = re.compile(r"(?<=\d)\s+(?=(?:gb|tb)\b)", re.IGNORECASE)
_RAM_WORD = ("ram", "memori", "memory", "vram")   # egy méret fajtája („16GB RAM”): a keresésben nem kötelező szó


def _joined_sizes(text: str) -> str:
    """„16 GB” → „16GB”: a kapacitás egy szó, ahogy a keresőszóban is (pricebook.capacities)."""
    return _SIZE_GAP.sub("", text or "")


def _norm_tokens(text: str) -> set[str]:
    """A szöveg szavai a termékkulcs egységes alakjában (pricebook._words), a semmitmondó szavak nélkül."""
    return {word for word in _words(text) if len(word) > 1 and word not in _STOPWORDS}


def _split_size(key: str) -> tuple[str, str]:
    """Egy termékkulcs kapacitás nélkül, és a kapacitása („1080 8gb gtx” → („1080 gtx”, „8gb”))."""
    words = key.split()
    return " ".join(w for w in words if not is_capacity(w)), " ".join(w for w in words if is_capacity(w))


# A memória kivitelének szavai (a keresésben): ha a hirdetés kivitele ugyanaz, nem kell betű szerint benne lenniük.
_FORM_WORDS = {"laptop", "laptopba", "notebook", "notebookba", "sodimm", "so", "dimm", "udimm", "asztali", "desktop", "pc",
               "mini", "macbook"}


def matches_query(query: str, text: str, strict_size: bool = False) -> bool:
    """A hirdetés tényleg a keresett dolog-e: a keresés minden szava (vagy a rokon szava, SYNONYMS) benne van; négy
    szónál hosszabb keresésnél egy hiányozhat, de a kapacitás nem: ha a keresésben van (500GB, 1TB), és a hirdetésben
    más szerepel, az más termék (más ár). A RAM és a tárhely külön számít (size_conflict): az „iPhone 13 128GB”
    keresésre a „128GB, 4GB RAM” hirdetés is jó. strict_size (az árösszehasonlításhoz): a keresett kapacitásnak benne
    is kell lennie a hirdetésben (a kapacitás nélküli hirdetés is lehet más változat). Így a „PS5 kontroller” keresésre
    a PS5-játék nem jelölt, és egy 500 GB-os SSD ára nem az 1 TB-osokéból jön."""
    query, text = _joined_sizes(query), _joined_sizes(text)
    # a szavak az írásmódjuk szerint és egységes alakban is (pricebook._words: az „RTX3060Ti” = „RTX 3060 Ti”, a
    # „PlayStation 5” = „PS5”, az „i5-12400F” = „i5 12400F”): a keresés bármelyik alakja egyezhet
    readings = [reading for reading in (_tokens(query), _norm_tokens(query)) if reading]
    have = _tokens(text) | _norm_tokens(text)
    if not readings:
        return True
    # a laptop- (SO-DIMM) és az asztali memória más termék: a kivitel nélküli keresés („16GB DDR4”) az asztalié (vagy
    # amiről nem derül ki); a laptop-memóriát a keresésben kell kérni („SO-DIMM”, „laptop”). Ha a kivitel egyezik, a
    # szava nem kell betű szerint („laptop” a keresésben, a hirdetésben csak a cikkszáma: parts.ram_form)
    if ram_context(_words(query)) or ram_context(_words(text)):
        want_form, have_form = parts.ram_form(query).form, parts.ram_form(text).form
        if have_form and have_form != (want_form or parts.DIMM):
            return False
        if want_form and want_form == have_form:
            readings = [reading - _FORM_WORDS for reading in readings]
            readings = [reading for reading in readings if reading] or [set()]
    want_sizes = sized(query)
    if want_sizes and size_conflict(want_sizes, sized(text), strict_size):
        return False                           # más kapacitás (a RAM és a tárhely külön számít, lásd size_conflict)
    for wanted in readings:
        if want_sizes:
            wanted = {w for w in wanted if not w.startswith(_RAM_WORD)} or wanted   # a „RAM” csak a méretet írja le
        hits = sum(1 for word in wanted if _shared({word, *SYNONYMS.get(word, ())}, have))
        if hits >= (len(wanted) if len(wanted) <= 4 else len(wanted) - 1):
            break
    else:
        return False
    folded = fold(query)
    # egy kiegészítő (a cím fejszava egy tok, töltő…; a „töltővel”, „töltő nélkül” a készülék: parts.accessory) vagy a
    # termék egy része („gyári hűtő”, „kijelző”: parts.component) nem maga a termék, hacsak nem ezt keresed
    stem = parts.accessory(text)
    if stem and stem not in folded:
        return False
    key = product_key(text)
    part, name = parts.part_of(text, key[0].split() if key else ())
    if name and not (part and re.search(rf"\b{part}", folded)):
        asked = product_key(query)             # („iPhone 13 kijelző”, „ThinkPad billentyűzet”: épp ezt keresed)
        if not parts.component(query, asked[0].split() if asked else ()):
            return False
    for_it = [w for w in have if w.endswith(("hoz", "hez")) and any(w.startswith(q) and w != q for q in wanted)]
    return not for_it


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


LEDGER_MONTHS = 6          # az Elemzés fülön ennyi hónap haszna látszik havonta
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
    """Haszonkövetés (Elemzés fül) a vételárral megjelölt tárgyakból (Megvettem, vagy a tárgy lapján a Vételár), és a
    saját holmidból, amit a Jarvis adott el (nincs vételára, de elkelt: own). Egy tárgy bekerülése a vételár, a posta és
    az egyéb költség (a saját holminál 0); a haszna az eladási ár, mínusz az eladás költségei (posta, díj) és a
    bekerülés. Ebben a hónapban, összesen és havonta (LEDGER_MONTHS); átlagosan hány nap alatt kelt el (a saját holmi a
    feltöltésétől), mennyi pénz van még bent a meg nem eladott (megvett) tárgyakban, és mennyi haszon várható belőlük
    (sale_estimate). A megtérülés (roi) csak a megvett tárgyakból számol. Ha az árbázis megvan (book), a meg nem eladott
    tárgyaknál azt is, mennyiért adják most."""
    now = time.time() if now is None else now
    month = time.strftime("%Y-%m", time.localtime(now))
    rows: list[dict[str, Any]] = []
    total = {"profit": 0, "count": 0, "spent": 0, "revenue": 0, "roi": None, "own": 0, "own_revenue": 0}
    this_month = {"profit": 0, "count": 0, "revenue": 0}
    stock = {"count": 0, "spent": 0, "expected": 0, "value": 0, "unknown": 0}
    by_month: dict[str, dict[str, Any]] = {}
    stages: Counter = Counter()
    days: list[int] = []
    bought_profit = 0
    for item in items:
        own = item.bought_price is None
        if own and not (item.sold and item.sold_price is not None):
            continue                # a saját holmid csak akkor számít, ha elkelt (a bevétele a haszna)
        cost, sell_cost = item_cost(item), int(item.sell_shipping or 0) + int(item.sell_fee or 0)
        bought_at = item.bought_at or (item.online_since() if own else None) or item.created
        where = stage(item)
        stages[where] += 1
        row: dict[str, Any] = {
            "id": item.id, "name": item.name, "stage": where, "sold": item.sold, "status": item.status, "own": own,
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
            row.update(profit=profit, roi=None if own else _percent(profit, cost))
            total["profit"] += profit
            total["count"] += 1
            total["revenue"] += item.sold_price
            if own:
                total["own"] += 1
                total["own_revenue"] += item.sold_price
            else:
                total["spent"] += cost
                bought_profit += profit
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
    total["roi"] = _percent(bought_profit, total["spent"])
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
        self._haggle_queue: list[dict[str, Any]] = []   # a legutóbbi átnézés legjobb alkuképes hirdetései (_step_haggle)
        self._tallied: set[tuple[str, str]] = set()      # ma már beszámolt (szám, hirdetés) párok (tally)
        self._tip_notices: list[tuple[tuple, dict]] = []   # a kiküldendő „Érdemes megnézni” értesítések (flush_tips)
        self._tips_busy: set[str] = set()     # az épp ellenőrzött tippek (egyszerre csak az egyik sor nézi)
        self._lock = threading.RLock()
        self._buy_lock = threading.Lock()     # a Megvettem egyszerre egy: egy találatból egy vásárlás (buy)
        self._spawned: dict[str, float] = {}  # az árkutatásból jelöltnek vett hirdetések (_peers): mikor
        # az árkutatásból jött jelöltek, amelyek a kör keretébe már nem fértek: hirdetés → (mióta vár, keresés, jelölt)
        self._carry: dict[str, tuple[float, dict[str, Any], dict[str, Any]]] = {}
        self._carry_busy: set[str] = set()   # amit egy kör épp ellenőriz (egyszerre csak az egyik sor nézi)
        # keresés:oldal → mikor esedékes újra a keret fogyása miatt (_retry_soon): addig az ütemező sűrűbben néz
        self._retries: dict[str, float] = {}
        self._pinged: dict[str, float] = {}   # a gyors jelzés („Ígéretes …”) hirdetésenként: mikor (_ping)
        self._followup_busy: set[str] = set()  # az épp utólag ellenőrzött találatok (followups)
        # akik tudni akarják, ha egy találat utólag megváltozik (az azonnali figyelő friss hirdetései: listen)
        self._listeners: list[Callable[[str, dict[str, Any] | None], None]] = []
        # termék → [mennyire keresett (felező súllyal), mikor láttuk utoljára, mikor melegítettük elő] (warm)
        self._demand: dict[str, list[float]] = {}
        self._demand_rows: dict[str, None] = {}   # a már beszámított hirdetések (egy hirdetés egyszer számít)
        self._warm_skip: set[str] = set()     # előmelegítéskor meg nem nyílt összehasonlítók (többször nem próbálja)
        # az összehasonlító hirdetések állapota a leírásuk szerint (_vet): hirdetés → {at, issues, gone}
        self._vetted_path = Path(root) / "allapotok.json"
        self._vetted = self._load_vetted()
        self._vet_dirty = False
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
                # gyors riasztás: az adatlap és az újraszámolás után rögtön csenget, a modell utána nézi meg (alapból be)
                "fast_alert": data.get("fast_alert") is not False,
                "turbo": bool(data.get("turbo")),     # Turbó: gyakrabban kérdezi a modellt (TURBO_ROWS)
                "message": str(data.get("message") or DEFAULT_MESSAGE)[:500],
                # pénzügyi keret: havi keret, és mikor szóljon, hogy túl sok megvett tárgy vár eladásra (money_check)
                "money": {"budget": None, "stock_count": None, "stock_value": None, "told": {},
                          **(data.get("money") if isinstance(data.get("money"), dict) else {})},
                "started": float(data.get("started") or 0),
                "browse": {"on": bool(browse.get("on")),
                           "sites": [s for s in browse.get("sites") or BROWSE_ORDER if s in BROWSE_ORDER],
                           "min_profit": int(browse.get("min_profit") or WATCH_PROFIT),
                           # öntanulás: a gyorsan kelő termékekből magától is felvesz indulóoldalt (alapból be)
                           "learn": browse.get("learn") is not False,
                           # a Vinted friss listája: hány percenként nézi meg a legújabbakat (0: a régi munkamenetek)
                           "glance": _glance_minutes(browse.get("glance"))},
                # indulóoldal → mit hozott, és a tanultak meg a sajátjaid címe; a kikapcsolt („disabled”), a törölt
                # tanult („removed”: nem tanulja újra)
                "lanes": dicts("lanes"),
                "session": data.get("session") if isinstance(data.get("session"), dict) else {},
                "fast_session": data.get("fast_session") if isinstance(data.get("fast_session"), dict) else {},
                # (glance: oldalanként a friss lista következő és legutóbbi pillantása, és a naplóba gyűjtött óra)
                "sites_state": {"ended": {}, "rest": {}, "blocked": {}, "learned": "", "glance": {},
                                **(data.get("sites_state") if isinstance(data.get("sites_state"), dict) else {})},
                "feedback": dicts("feedback"),    # termék → elrejtve / megvetted (öntanulás)
                # csomagbontás: be/ki, a legkisebb haszon, a csomagkeresések, és a felmérésre váró csomagok (inbox)
                "bundles": self._load_bundles(data.get("bundles")),
                # alkuképes hirdetések: be/ki (alapból be), az üzenet, mikor nézte át az árbázist, a már megnézettek,
                # és hányról szólt ma
                "haggle": self._load_haggle(data.get("haggle")),
                "tips": [t for t in data.get("tips") or [] if isinstance(t, dict) and t.get("url")
                         and isinstance(t.get("price"), int)][:MAX_TIPS],
                "tips_told": {"day": str((data.get("tips_told") or {}).get("day") or ""),
                              "n": int((data.get("tips_told") or {}).get("n") or 0)},
                # a licitlista (Vatera): a megérő, még nem hamarosan záruló licitek, és hányról szólt ma (_follow_auction)
                "auctions": [a for a in data.get("auctions") or [] if isinstance(a, dict) and a.get("url")
                             and isinstance(a.get("max_bid"), int) and isinstance(a.get("ends"), (int, float))][:MAX_AUCTIONS],
                "auction_told": {"day": str((data.get("auction_told") or {}).get("day") or ""),
                                 "n": int((data.get("auction_told") or {}).get("n") or 0)},
                "funnel": {"day": str((data.get("funnel") or {}).get("day") or ""),
                           "counts": {str(k): int(v) for k, v in ((data.get("funnel") or {}).get("counts") or {}).items()
                                      if isinstance(v, int)}},
                "searches": [s for s in data.get("searches") or [] if isinstance(s, dict) and s.get("query")],
                "watches": [w for w in data.get("watches") or [] if isinstance(w, dict) and w.get("url")],
                "deals": [d for d in data.get("deals") or [] if isinstance(d, dict) and d.get("url")],
                "seen": numbers("seen"),       # hirdetés → mikor néztük meg utoljára (egy keresés ehhez igazodik)
                "cancelled": numbers("cancelled"),   # visszamondott vétel hirdetése → mikor: többé nem szól róla
                "prices": {k: int(v) for k, v in numbers("prices").items()},   # hirdetés → az ára akkor (leárazás)
                "last": numbers("last"),       # keresés:oldal → mikor néztük meg (gyakoriság)
                # pótlási és javítási költségek (amit te adtál meg): {id, part (pl. töltő, kijelző), product (üres: mind),
                # amount}; ezekkel számol az állapothoz igazított haszon (_reprice), és mikor változtak utoljára
                "costs": [c for c in data.get("costs") or [] if isinstance(c, dict) and c.get("part")
                          and isinstance(c.get("amount"), int)][:MAX_COSTS],
                "costs_at": float(data.get("costs_at") or 0),
                "fees": self._load_fees(data.get("fees"))}

    def _load_vetted(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self._vetted_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}

    def _save_vetted(self) -> None:
        with self._lock:
            self._vet_dirty = False
            if len(self._vetted) > MAX_VETTED:     # a legrégebbiek kiesnek
                for key in sorted(self._vetted, key=lambda k: float(self._vetted[k].get("at") or 0))[
                        :len(self._vetted) - MAX_VETTED]:
                    self._vetted.pop(key, None)
            try:
                self._vetted_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._vetted_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._vetted, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, self._vetted_path)
            except OSError:
                pass

    @staticmethod
    def _load_haggle(saved: Any) -> dict[str, Any]:
        saved = saved if isinstance(saved, dict) else {}
        told = saved.get("told") if isinstance(saved.get("told"), dict) else {}
        return {"on": saved.get("on") is not False, "message": str(saved.get("message") or DEFAULT_HAGGLE_MESSAGE)[:500],
                "scanned": float(saved.get("scanned") or 0),
                "done": {str(k): float(v) for k, v in (saved.get("done") or {}).items() if isinstance(v, (int, float))},
                "told": {"day": str(told.get("day") or ""), "n": int(told.get("n") or 0)}}

    @staticmethod
    def _load_fees(saved: Any) -> dict[str, Any]:
        """Az eladás költségei (FEE_NAMES): a Ft egész, a % tizedes; None: nincs megadva."""
        saved = saved if isinstance(saved, dict) else {}
        out: dict[str, Any] = {}
        for name in FEE_NAMES:
            try:
                value = float(saved[name]) if saved.get(name) is not None else None
            except (TypeError, ValueError):
                value = None
            out[name] = None if value is None else (round(min(max(value, 0.0), FEE_MAX[name]), 1)
                                                    if name.endswith("_pct") else int(min(max(value, 0), FEE_MAX[name])))
        return out

    @staticmethod
    def _load_bundles(saved: Any) -> dict[str, Any]:
        saved = saved if isinstance(saved, dict) else {}
        return {"on": bool(saved.get("on")), "min_profit": int(saved.get("min_profit") or BUNDLE_PROFIT),
                # a ruhás csomagkeresés (a régi „Ruhacsomag” alapbeállítás) kimarad: ruhával nem kereskedik
                "searches": [s for s in saved.get("searches") or [] if isinstance(s, dict) and s.get("query")
                             and not wearable(str(s["query"]))],
                "inbox": [r for r in saved.get("inbox") or [] if isinstance(r, dict) and r.get("url")
                          and isinstance(r.get("price"), int)][:BUNDLE_INBOX]}

    def _save(self) -> None:
        with self._lock:                      # két szál is menthet (a böngésző sora és a gyors sor)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            if self._vet_dirty:               # egy jelölt adatlapjáról eltárolt állapot (_inspect) is megmarad
                self._save_vetted()

    def state(self) -> dict[str, Any]:
        """A felületnek: a kapcsoló, a keresések (az állapotukkal) és a talált hirdetések (a legújabb elöl)."""
        with self._lock:
            deals = [d for d in self._data["deals"] if not d.get("dismissed")]
            searches = [{**json.loads(json.dumps(s)), "status": search_status(s)} for s in self._data["searches"]]
            return {"enabled": self._data["enabled"], "running": self._data["enabled"],
                    "started": self._data["started"] or None, "max_buy": self._data["max_buy"],
                    "safe_only": self._data["safe_only"], "turbo": self._data["turbo"],
                    "fast_alert": self._data["fast_alert"],
                    "message": self._data["message"], "money": self.money_status(), "searches": searches,
                    "watches": json.loads(json.dumps(self._data["watches"])),
                    "deals": sorted((dict(d) for d in deals), key=lambda d: -float(d.get("at") or 0)),
                    "sites": [{"key": k, "name": p.name} for k, p in self.platforms.items()],
                    "browser": self.browser is not None, "sale_ratio": SALE_RATIO, "shipping": SHIPPING,
                    "day": list(self._data["day"]), "book": self.book.stats(),
                    "browse": {**json.loads(json.dumps(self._data["browse"])),
                               "status": self._browse_status(self.clock()), "glance_choices": list(GLANCE_CHOICES),
                               "glance_sites": [k for k in GLANCE_SITES if k in self.platforms]},
                    "lanes": self._lane_list(), "lane_presets": list(LANE_PRESETS),
                    "journal": self.journal.entries(), "journal_days": self.journal.days(),
                    "bundles": self._bundle_state(),
                    "funnel": self.funnel(),
                    "tips": sorted((dict(t) for t in self._data["tips"]), key=lambda t: -float(t.get("at") or 0)),
                    "auctions": sorted((dict(a) for a in self._data["auctions"]), key=lambda a: float(a["ends"])),
                    "haggle": {"on": self._data["haggle"]["on"], "message": self._data["haggle"]["message"],
                               "per_day": HAGGLE_PER_DAY, "days": HAGGLE_DAYS, "max_cut": HAGGLE_MAX},
                    "costs": [dict(c) for c in self._data["costs"]], "cost_parts": list(COST_PARTS),
                    "fees": dict(self._data["fees"]), "fee_names": dict(FEE_NAMES)}

    # --- pótlási és javítási költségek (az állapothoz igazított haszonhoz) ---

    def add_cost(self, part: Any, product: Any, amount: Any) -> dict[str, Any]:
        """Egy pótlási vagy javítási költség (pl. „töltő”, 4 000 Ft; „kijelző”, „switch lite”, 18 000 Ft): ha egy
        jelöltnél ez hiányzik vagy hibás, ennyit von le a hasznából (_reprice). A termék üres: minden termékre; ha
        megadod, csak arra (a szavai a hirdetésben), és az erősebb, mint az általános."""
        name = conditions.part_name(str(part or ""))
        if not name:
            raise DealError("Mit kell pótolni vagy javítani (pl. töltő, kijelző)?")
        value = self._amount(amount, "Az összeg")
        if not value:
            raise DealError("Mennyibe kerül (Ft)?")
        entry = {"id": secrets.token_hex(4), "part": name, "product": " ".join(str(product or "").split())[:60],
                 "amount": value}
        with self._lock:
            costs = [c for c in self._data["costs"] if not (c["part"] == name and fold(c.get("product") or "")
                                                             == fold(entry["product"]))]
            self._data["costs"] = (costs + [entry])[-MAX_COSTS:]
            self._data["costs_at"] = self.clock()
            self._save()
        product = f" ({entry['product']})" if entry["product"] else ""
        self.journal.add(f"Költség: {name}{product} {format_price(value)}; az ilyen hibájú vagy hiányos jelölteknél "
                         "ennyit vonok le.")
        return self.state()

    def set_fees(self, values: dict[str, Any]) -> dict[str, Any]:
        """Az eladás költségei (FEE_NAMES): csomagolás és egyéb eladási költség Ft-ban, az eladási díj és a tartalék az
        eladási ár %-ában. Az üres mező: nincs megadva (a haszonban 0-val szerepel, de a találatnál látszik); a 0 megadott
        nulla. Csak a megadott kulcsok változnak."""
        with self._lock:
            fees = dict(self._data["fees"])
        for name, value in (values or {}).items():
            if name not in FEE_NAMES:
                continue
            if value is None or str(value).strip() == "":
                fees[name] = None
                continue
            try:
                number = float(str(value).replace(" ", "").replace(",", ".").rstrip("%"))
            except ValueError:
                raise DealError(f"{FEE_NAMES[name].capitalize()}: számot írj (vagy hagyd üresen).") from None
            unit = "%" if name.endswith("_pct") else " Ft"
            if not 0 <= number <= FEE_MAX[name]:
                raise DealError(f"{FEE_NAMES[name].capitalize()}: 0 és {FEE_MAX[name]}{unit} között legyen.")
            fees[name] = round(number, 1) if name.endswith("_pct") else int(round(number))
        with self._lock:
            self._data["fees"] = fees
            self._save()
        shown = [f"{FEE_NAMES[k]} {v:g}%" if k.endswith("_pct") else f"{FEE_NAMES[k]} {format_price(int(v))}"
                 for k, v in fees.items() if v is not None]
        self.journal.add("Eladási költségek: " + (", ".join(shown) if shown else "nincs megadva") + ".")
        return self.state()

    def _sale_parts(self, sale: int, count: int = 1) -> list[dict[str, Any]]:
        """Az eladás költségtételei (a beállításokból, set_fees) egy sale eladási árra, count hirdetésre: csomagolás,
        egyéb eladási költség (darabonként), eladási díj és tartalék (az eladási ár %-a). A meg nem adott nincs köztük
        (az _fees_unset sorolja fel)."""
        with self._lock:
            fees = dict(self._data["fees"])
        parts: list[dict[str, Any]] = []
        for name in ("packaging", "sell_fixed"):
            if fees.get(name):
                parts.append({"what": FEE_NAMES[name], "amount": int(fees[name]) * max(count, 1), "sale": True})
        for name in ("sell_pct", "reserve_pct"):
            if fees.get(name):
                parts.append({"what": f"{FEE_NAMES[name]} ({fees[name]:g}%)",
                              "amount": int(round(max(sale, 0) * float(fees[name]) / 100)), "sale": True})
        return parts

    def _piece_cost(self, sale: int) -> int:
        """Egy darab továbbadásának költsége (a csomagoknál, az eladó többi hirdetésénél): a megadott csomagolás (ha nincs
        megadva, a beépített becslés, PIECE_COST), és az eladás többi költsége (set_fees) a darab eladási árára."""
        with self._lock:
            packaging = self._data["fees"].get("packaging")
        rest = [part for part in self._sale_parts(sale) if part["what"] != FEE_NAMES["packaging"]]
        return (PIECE_COST if packaging is None else int(packaging)) + sum(part["amount"] for part in rest)

    def _fees_unset(self) -> list[str]:
        with self._lock:
            return [FEE_NAMES[name] for name in FEE_NAMES if self._data["fees"].get(name) is None]

    def remove_cost(self, cost_id: str) -> dict[str, Any]:
        with self._lock:
            self._data["costs"] = [c for c in self._data["costs"] if c.get("id") != cost_id]
            self._save()
        return self.state()

    def _cost(self, issue: str, deal: dict[str, Any]) -> int | None:
        """Egy hiba vagy hiányzó tartozék pótlási/javítási költsége (amit megadtál; a termékre szabott előbb), vagy None."""
        text = fold(" ".join(str(deal.get(k) or "") for k in ("title", "text", "query")))
        with self._lock:
            costs = list(self._data["costs"])
        best: tuple[int, int] | None = None
        for entry in costs:
            if entry["part"] != issue:
                continue
            words = fold(entry.get("product") or "").split()
            if words and not all(word in text for word in words):
                continue
            if best is None or len(words) > best[0]:
                best = (len(words), int(entry["amount"]))
        return best[1] if best else None

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
            if self._bundles_due(now) or self._browse_due(now) or self._auctions_due(now) or self._glance_due(now):
                return True
        return self._haggle_pending(now)

    def running_searches(self) -> list[dict[str, Any]]:
        """A futó keresések (amelyiknek van jóváhagyott szokásos ára, vagy épp felméri): az azonnali figyelő ezeket a
        Hardveraprón és a Jófogáson pár percenként is megnézi (InstantWatch.linked)."""
        now = self.clock()
        with self._lock:
            if not self._data["enabled"]:
                return []
            return [json.loads(json.dumps(s)) for s in self._data["searches"] if not self._search_waits(s, now)]

    def running_watches(self) -> list[dict[str, Any]]:
        """A figyelt oldalak, amíg az automata kereskedés fut: az azonnali figyelő a Hardveraprón és a Jófogáson ezeket
        is pár percenként megnézi (InstantWatch.link_searches), nem csak a kör (óránként)."""
        with self._lock:
            if not self._data["enabled"]:
                return []
            return [json.loads(json.dumps(w)) for w in self._data["watches"] if w.get("url")]

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

    def set_browse(self, on: Any = None, sites: Any = None, min_profit: Any = None, learn: Any = None,
                   glance: Any = None) -> dict[str, Any]:
        """Böngészés napközben: be/ki, melyik oldalakon, legalább mennyi haszonnal szóljon, felvehet-e magától
        indulóoldalt a gyorsan kelő termékekből (öntanulás), és hány percenként nézze meg a Vinted legújabb hirdetéseit
        (glance, GLANCE_CHOICES; 0: a régi, kétóránkénti munkamenetek)."""
        profit = self._amount(min_profit, "A legkisebb haszon") if min_profit not in (None, "") else None
        minutes = None
        if glance not in (None, ""):
            try:
                minutes = int(glance)
            except (TypeError, ValueError):
                minutes = -1
            if minutes not in GLANCE_CHOICES:
                raise DealError("A Vinted friss listáját " + ", ".join(str(m) for m in GLANCE_CHOICES if m) +
                                " percenként nézhetem (vagy 0: kétóránként, hosszabb munkamenetekben).")
        with self._lock:
            if minutes is not None and minutes != self._data["browse"]["glance"]:
                self._data["browse"]["glance"] = minutes
                now = self.clock()
                for mark in self._data["sites_state"]["glance"].values():
                    # az új gyakoriság rögtön érvényes: a következő pillantás a legutóbbihoz mérve
                    if isinstance(mark, dict) and minutes:
                        mark["next"] = float(mark.get("at") or now) + minutes * 60
                session = self._data[SESSION_KEYS[LANE_BROWSER]]
                if minutes and session.get("phase") == "work" and session.get("site") in GLANCE_SITES:
                    self._data[SESSION_KEYS[LANE_BROWSER]] = {}   # a régi Vinted-munkamenet helyett a friss lista
                self._save()
            else:
                minutes = None                 # (nem változott: nem kerül a naplóba)
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
            if learn is not None:
                browse["learn"] = bool(learn)
            self._save()
        if on is not None:
            self.journal.add("Bekapcsoltad a napközbeni böngészést." if on else "Kikapcsoltad a napközbeni böngészést.")
        if learn is not None:
            self.journal.add("Az öntanulás be van kapcsolva: a gyorsan kelő termékekből magam is veszek fel indulóoldalt."
                             if learn else "Kikapcsoltad az öntanulást: új indulóoldalt csak te veszel fel.")
        if minutes is not None:
            self.journal.add(f"A Vinted legújabb hirdetéseit ezentúl {minutes} percenként nézem meg (egy oldal)."
                             if minutes else "A Vintedet ezentúl kétóránként nézem, hosszabb munkamenetekben.")
        return self.state()

    def lane_add(self, target: str, site: str = "") -> dict[str, Any]:
        """Saját indulóoldal a böngészéshez: egy oldal címe (egy kategória vagy egy találati oldal; a Vinteden és a
        Jófogáson a legújabb elöl, ha nem adtál meg más rendezést), vagy egy keresőszó a megadott oldalon (ha nincs
        megadva, a böngészés minden bejelölt oldalán, a legújabb elöl)."""
        target = " ".join(str(target or "").split())[:300]
        if not target:
            raise DealError("Írj egy keresőszót, vagy másold be egy találati oldal vagy kategória címét.")
        added: list[dict[str, Any]] = []
        if re.match(r"https?://", target):
            parts = urllib.parse.urlsplit(target)
            host = (parts.hostname or "").removeprefix("www.")
            found = next((k for k, p in self.platforms.items()
                          if any(host == d or host.endswith("." + d) for d in p.domains)), None)
            if found is None:
                names = ", ".join(p.name for p in self.platforms.values())
                raise DealError(f"Ezt az oldalt nem ismerem; ezekről másolj be egy oldalt: {names}.")
            segments = [seg for seg in parts.path.split("/") if seg and not seg.startswith("index")
                        and not seg.endswith((".php", ".html", ".htm"))]
            query = urllib.parse.parse_qs(parts.query)
            label = " ".join((query.get("q") or query.get("search_text") or query.get("stext") or
                              [urllib.parse.unquote(segments[-1]).replace("-", " ").replace("_", " ") if segments
                               else "saját oldal"])[0].split())[:40]
            added.append({"site": found, "label": label, "url": newest_first(target, found)})
        else:
            keyword = target[:60]
            chosen = [site] if site else list(self._data["browse"]["sites"])
            for key in chosen:
                platform = self.platforms.get(key)
                if platform is None:
                    raise DealError("Ezt az oldalt nem ismerem.")
                url = platform.search_url.format(q=urllib.parse.quote(keyword))
                added.append({"site": key, "label": keyword, "url": newest_first(url, key)})
        names = []
        with self._lock:
            lanes = self._data["lanes"]
            if sum(1 for lane in lanes.values() if lane.get("kind") == "sajat") + len(added) > MAX_OWN_LANES:
                raise DealError(f"Legfeljebb {MAX_OWN_LANES} saját indulóoldal lehet.")
            for item in added:
                platform = self.platforms[item["site"]]
                if any(lane["url"] == item["url"] for lane in self._all_lanes(item["site"])):
                    continue                   # ez már az indulóoldalak között van
                ident = f"sajat:{item['site']}:" + (re.sub(r"[^a-z0-9]+", "_", fold(item["label"])).strip("_") or "oldal")
                if ident in lanes:             # ugyanaz a név, más oldal
                    ident += "_" + hashlib.sha1(item["url"].encode("utf-8")).hexdigest()[:6]
                name = f"{platform.name}: {item['label']}"
                lanes[ident] = {"kind": "sajat", "site": item["site"], "name": name, "url": item["url"],
                                "at": self.clock()}
                names.append(name)
            if not names:
                raise DealError("Ez már az indulóoldalak között van.")
            self._save()
        self.journal.add("Új indulóoldal: " + ", ".join(names) + ".")
        return self.state()

    def lane_switch(self, lane_id: str, on: Any) -> dict[str, Any]:
        """Egy indulóoldal be- vagy kikapcsolása (a beépítetteké és a sajátjaidé; a kikapcsoltat a böngészés kihagyja,
        de a listában marad, és visszakapcsolható)."""
        with self._lock:
            lane = next((lane for site in BROWSE_ORDER for lane in self._all_lanes(site) if lane["id"] == lane_id), None)
            if lane is None:
                raise DealError("Ilyen indulóoldal nincs.")
            if lane["kind"] == "tanult":
                raise DealError("A tanult indulóoldalt törölni lehet (és nem tanulja meg újra).")
            stats = self._data["lanes"].setdefault(lane_id, {})
            if on:
                stats.pop("disabled", None)
            else:
                stats["disabled"] = True
            self._save()
        self.journal.add(("Bekapcsoltad" if on else "Kikapcsoltad") + f" ezt az indulóoldalt: {lane['name']}.")
        return self.state()

    def lane_remove(self, lane_id: str) -> dict[str, Any]:
        """Egy saját indulóoldal törlése, vagy egy tanulté (ezt a terméket többé nem veszi fel magától). A beépítettet
        kikapcsolni lehet (lane_switch)."""
        with self._lock:
            lane = self._data["lanes"].get(lane_id) or {}
            if lane.get("kind") == "sajat":
                self._data["lanes"].pop(lane_id)
            elif lane.get("kind") == "tanult" and not lane.get("removed"):
                lane["removed"] = self.clock()
            else:
                raise DealError("Ezt az indulóoldalt nem lehet törölni (a beépítettet kikapcsolhatod).")
            self._save()
        self.journal.add(f"Törölted ezt az indulóoldalt: {lane.get('name') or lane_id}"
                         + (" (nem veszem fel újra magamtól)." if lane.get("kind") == "tanult" else "."))
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

    def set_limits(self, max_buy: Any = None, min_profit: Any = None, safe_only: Any = None,
                   fast_alert: Any = None) -> dict[str, Any]:
        """Minden keresésre: legfeljebb ennyiért vegyen (üresen: bármennyiért), legalább ennyi haszon legyen benne (a
        figyelt oldalaknál, a böngészésnél és az azonnali figyelőnél; a keresésnek saját szabálya van), csak a nyugodt
        szívvel megvehetőkről szóljon-e (safe_only), és a gyors riasztás (fast_alert: az adatlap után rögtön csenget, a
        modell utána nézi meg; None: marad)."""
        cap = self._amount(max_buy, "A legmagasabb vételár") if max_buy not in (None, "") else None
        profit = self._amount(min_profit, "A legkisebb haszon") if min_profit not in (None, "") else None
        with self._lock:
            self._data["max_buy"] = cap
            if profit:
                self._data["browse"]["min_profit"] = profit
            if safe_only is not None:
                self._data["safe_only"] = bool(safe_only)
            if fast_alert is not None:
                self._data["fast_alert"] = bool(fast_alert)
            self._save()
        return self.state()

    def set_fast_alert(self, on: Any) -> dict[str, Any]:
        """Gyors riasztás be/ki (lásd set_limits)."""
        with self._lock:
            self._data["fast_alert"] = bool(on)
            self._save()
        return self.state()

    def _fast(self) -> bool:
        """Gyors riasztás: van modell, és be van kapcsolva (akkor a modell nem a csengetés előtt, hanem utána kérdez)."""
        with self._lock:
            return self.llm is not None and bool(self._data["fast_alert"])

    def state_running(self) -> bool:
        """Fut-e (az Indítás után, a Leállításig)."""
        with self._lock:
            return bool(self._data["enabled"])

    @property
    def turbo(self) -> bool:
        """Be van-e kapcsolva a Turbó (TURBO_ROWS). Zár nélkül olvasható: a modellkapu is ebből dönt (llm.ModelGate)."""
        return bool(self._data.get("turbo"))

    def set_turbo(self, on: Any) -> dict[str, Any]:
        """Turbó be/ki (a gombja az Indítás mellett): a böngészés ismeretlenjeit minden oldal után megnevezteti, és a
        modellkapu a tartalék helyet is a kereskedésnek adja, amíg nem dolgozol a Jarvisszal. Megmarad a következő
        indításra is."""
        on = bool(on)
        with self._lock:
            changed = self._data["turbo"] != on
            self._data["turbo"] = on
            self._save()
        if changed:
            self.journal.add("Bekapcsoltad a Turbót: gyakrabban kérdezem a modellt (minden oldal ismeretlen hirdetéseit "
                             "rögtön megnevezem, és amíg nem dolgozol velem, a modell mindkét helyét használom). A "
                             "weboldalaktól nem kérek többet." if on else
                             "Kikapcsoltad a Turbót: a modellt a szokásos ütemben kérdezem.")
        return self.state()

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
            notify("info", title, text, actions=[{"action": "ledger", "label": "Elemzés"}])
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

    def set_haggle(self, on: Any = None, message: Any = None) -> dict[str, Any]:
        """Alkuképes hirdetések: be/ki, és az eladónak szóló ajánlat ({ár} helyére a javasolt összeg kerül; üresen az
        alapüzenet)."""
        with self._lock:
            haggle = self._data["haggle"]
            if on is not None:
                haggle["on"] = bool(on)
            if message is not None:
                lines = [" ".join(line.split()) for line in str(message or "").strip().splitlines()]
                haggle["message"] = "\n".join(lines).strip()[:500] or DEFAULT_HAGGLE_MESSAGE
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
            # a Vinteden és a Jófogáson a legújabb elöl (ha nem adtál meg más rendezést); a Vaterán a te rendezésed
            # marad (a licitekhez a „hamarosan lejár” a jó)
            url = newest_first(target, platform) if platform in ("vinted", "jofogas") else target
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

    def mark_bought(self, deal_id: str, item_id: str, items: list[str] | None = None) -> dict[str, Any]:
        """Megvetted: a találatból eladási tárgy lett (item_id; egy csomagból több is: items, a darabjai); a listában
        jelölve marad. Öntanulás: az indulóoldala és a termék jó pontot kap (ha elrejtetted volna, újra szól róla)."""
        found = None
        with self._lock:
            for deal in self._data["deals"]:
                if deal.get("id") == deal_id:
                    deal["bought"] = item_id
                    if items:
                        deal["bought_items"] = list(items)
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

    def buy(self, deal_id: str, exists: Callable[[str], bool],
            create: Callable[[dict[str, Any]], list[str]]) -> tuple[list[str], bool]:
        """Megvettem, egyszer: egy zár alatt nézi meg, hogy ebből a találatból készült-e már vásárlás (a dupla
        kattintás, egy újraküldött kérés ne könyvelje kétszer ugyanazt). Ha igen, és a tárgyai megvannak (`exists`), azokat
        adja vissza (False: nem új); ha nem, a `create` hozza létre a tárgyakat (egy csomagnál a darabjait, az első a
        fő), és megjelöli a találatot (mark_bought). Visszaadja a tárgyak azonosítóit, és hogy most készültek-e."""
        with self._buy_lock:
            deal = self.get(deal_id)
            done = list(deal.get("bought_items") or ([deal["bought"]] if deal.get("bought") else []))
            if done and all(exists(item_id) for item_id in done):
                return done, False
            items = create(deal)
            if not items:
                raise DealError("Ebből a találatból nem lett tárgy.")
            self.mark_bought(deal_id, items[0], items if deal.get("bundle") else None)
            return items, True

    def cancel_bought(self, item_id: str, name: str = "") -> dict[str, Any] | None:
        """Visszamondtad a vételt (a tárgyat a hívó törli): a találat, amiből lett, kikerül a találatok közül, a jó pontja
        (a termék, az indulóoldal) visszavonódik, a havi keret maradéka rögtön újraszámolódik, és erről a hirdetésről
        többé nem szól. Visszaadja a találatot (None, ha kézzel felvett vétel volt). Egy csomagnál a darabjait
        (bought_group) a hívó mind törli."""
        with self._lock:
            found = next((d for d in self._data["deals"] if d.get("bought") == item_id
                          or item_id in (d.get("bought_items") or [])), None)
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
                         + "; töröltem az Elemzés fülről és az eladandók közül" + (", és a találatok közül" if found else "")
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
                if key and not deal.get("haggle"):     # egy elrejtett alku nem ítélet a termékről
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
        """A háttérmunka gyakorisága: a leggyakoribb keresésé (a többi csak akkor fut, ha esedékes, lásd _due). Ha egy
        keresés vagy figyelt oldal a keret fogyása miatt hamarabb esedékes újra (_retry_soon), vagy a licitlistán van
        valami (az ellenőrizetlenek adatlapja, a zárás előtti ellenőrzés), az ütemező percenként megnézi, van-e dolga
        (available dönt), különben a 10 perces újrapróbálkozás is a teljes gyakoriságra várna."""
        now = self.clock()
        with self._lock:
            jobs = [int(s.get("every") or DEFAULT_EVERY) for s in self._data["searches"]]
            jobs += [int(w.get("every") or WATCH_EVERY) for w in self._data["watches"]]
            bundles = self._data["bundles"]
            if bundles["on"]:
                jobs += [int(s.get("every") or DEFAULT_EVERY) for s in bundles["searches"]]
                if bundles["inbox"]:
                    jobs.append(5)             # a felmérésre váró csomagok (a többi oldalról) hamar sorra kerülnek
            ids = {str(item.get("id")) for item in self._data["searches"] + self._data["watches"]}
            self._retries = {key: at for key, at in self._retries.items()   # a törölt és a régen lejárt kimarad
                             if key.split(":")[0] in ids and now - at < 3600}
            soon = bool(self._retries or self._data["auctions"])
            if not self._data["enabled"] or not (jobs or self._data["browse"]["on"] or soon):
                return 0.0
            # böngészés közben (és az újrapróbálkozásnál, a licitlistánál) percenként megnézi, van-e dolga (csak akkor
            # fut, ha van: lásd available)
            return 1.0 if self._data["browse"]["on"] or soon or not jobs else float(min(jobs))

    def _due(self, search: dict[str, Any], key: str, now: float) -> bool:
        every = max(int(search.get("every") or DEFAULT_EVERY), SITE_MIN_MINUTES.get(key, 0))
        last = float(self._data["last"].get(f"{search['id']}:{key}") or 0)
        return now - last >= every * 60 - 90     # másfél perc ráhagyás: az ütemező percre pontosan nem indít

    def _retry_soon(self, search: dict[str, Any], key: str) -> None:
        """Egy keresés vagy figyelt oldal ezen az oldalon BUSY_RETRY múlva esedékes újra (nem a teljes gyakorisága után):
        most csak az oldal óránkénti kerete fogyott el, és azt nem a böngésző pótolja. Az ütemező is tud róla
        (every_minutes), így tényleg BUSY_RETRY múlva jön, nem a következő óránkénti körben."""
        every = max(int(search.get("every") or DEFAULT_EVERY), SITE_MIN_MINUTES.get(key, 0))
        now = self.clock()
        with self._lock:
            self._data["last"][f"{search['id']}:{key}"] = now - every * 60 + BUSY_RETRY
            self._retries[f"{search['id']}:{key}"] = now + BUSY_RETRY

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
            bundles = json.loads(json.dumps({k: v for k, v in self._data["bundles"].items() if k != "inbox"}))
        if self.browser is None:
            return "Az automata kereskedéshez Jarvis saját böngészője kell."
        now = self.clock()
        game = _Round(check, notify, ModelLane() if self.llm is not None else None)
        game.flush = partial(self.flush_tips, notify)
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
        bundle_searches = []
        for bundle_search in bundles["searches"] if bundles["on"] else []:
            sites = [key for key in bundle_search.get("sites") or list(self.platforms) if key in self.platforms
                     and (force or self._due(bundle_search, key, now))]
            if sites:
                game.done += 1
                names.append(f"csomag: „{bundle_search['query']}”")
                bundle_searches.append(bundle_search)
                game.work.append(partial(self._step_bundle_search, game, bundle_search, sites))
        browse = False
        with self._lock:                      # a Vinted friss listája: ha esedékes, ebben a körben elöl
            glance = self._glance_due(now)
        if glance:
            game.work.appendleft(partial(self._step_glance, game, running, glance))
        if game.done:
            game.work.append(partial(self._step_revisit, game))
        elif not glance:                      # „Keresés most” is: ha nincs keresés, a böngészés lép
            with self._lock:
                browse = self._browse_due(now)
            if browse:
                game.work.append(partial(self._step_browse, game, running))
        if bundles["on"]:                     # a felmérésre váró csomagok (a keresések, a böngészés után)
            game.work.append(partial(self._step_bundles, game))
        if self._haggle_pending(now):         # alkuképes hirdetések az árbázisból (félóránként)
            game.work.append(partial(self._step_haggle, game))
        with self._lock:
            auctions = self._auctions_due(now)
        if auctions:                          # a licitlista: a zárás előtt még egyszer (elöl: ott sürget az idő)
            game.work.appendleft(partial(self._step_auctions, game))
        with self._lock:
            tips, carry = bool(self._data["tips"]), bool(self._carry)
        if tips:                              # a bizonytalan árú tippek újra (az árbázisból, letöltés nélkül)
            game.work.append(partial(self._step_tips, game))
        if game.lane is not None and self._pending_checks():   # amit a modell még nem nézett meg (foglalt volt)
            game.work.append(partial(self._step_followups, game))
        if carry:                             # az előző kör keretébe nem fért, árkutatásból jött jelöltek: elöl
            game.work.appendleft(partial(self._step_carry, game))
        if self.fetcher is not None and game.done:
            # a Hardverapró és a Jófogás találati oldalai rögtön, a háttérben (oldalanként egyszerre egy, szünetekkel),
            # amíg a böngésző a Vintedet és a Vaterát nézi
            game.pool = ThreadPoolExecutor(max_workers=max(1, len(getattr(self.fetcher, "sites", ()) or (1,))),
                                           thread_name_prefix="jarvis-gyors")
            for url, platform in self._fast_loads(searches + bundle_searches, watches, now, force):
                game.fetch_ahead(self.fetcher, url, platform)
        try:
            game.loop()
        finally:
            if game.lane is not None:
                game.lane.close()
            if game.pool is not None:
                game.pool.shutdown(wait=False, cancel_futures=True)
            self.book.save()
            self.flush_tips(notify)
            self._release_tips(game)
            self._release_carry(game)
        if browse:
            return self._after_browse(game)
        if glance:
            looked = self._glance_done(game, glance)
            if not game.done:
                return looked
        extra = [n for n in game.notes if n] + game.errors
        if not game.done and (game.bundled or game.haggles):
            parts = [f"{game.bundled} csomag felmérve, " + (f"{game.found} megéri" if game.found else "egyik sem éri meg")
                     ] if game.bundled else []
            parts += [f"{game.haggles} alkuképes hirdetés"] if game.haggles else []
            return "; ".join(parts).capitalize() + "."
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
        return newest_first(self.platforms[key].search_url.format(q=urllib.parse.quote(search["query"])), key)

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
        failed = ""
        if future is not None or (self.fetcher is not None and self.fetcher.usable(platform)):
            try:
                rows = future.result() if future is not None else self.fetcher.listings(url, platform, check,
                                                                                        LISTING_REUSE)
            except FeedError as exc:
                if exc.kind in ("busy", "network"):     # csak a keret vagy a hálózat: később, nem a böngészővel
                    raise _Later(str(exc)) from None
                rows, failed = None, str(exc)
            if rows:
                if game is not None:
                    game.fast += 1
                return rows
        elif self.fetcher is not None and self.fetcher.handles(platform):
            failed = "tiltás vagy ellenőrzés miatt egy ideig nem kérdezem"
        if game is not None and game.no_browser:   # a gyors sor a böngészőhöz nem nyúl: az ok a naplóba
            raise BrowserError(failed or "böngésző nélkül most nem olvasható")
        return self.browser().extract_listings(url)

    def _platform_of(self, url: str) -> str:
        host = (urllib.parse.urlsplit(url).hostname or "").removeprefix("www.")
        return next((k for k, p in self.platforms.items() if any(host == d or host.endswith("." + d)
                                                                 for d in p.domains)), "")

    def _look(self, url: str, platform: str = "", check: Callable[[], None] = lambda: None,
              game: _Round | None = None, urgent: bool = False) -> dict[str, Any]:
        """Egy hirdetés oldala csak olvasva: a gyors oldalakon böngésző nélkül, különben (vagy ha így nem megy) a
        böngészővel (a gyors sorban soha). urgent: egy jelölt adatlapja, a letöltő sorában elsőbbséggel. BrowserError,
        ha nem sikerült."""
        platform = platform or self._platform_of(url)
        if self.fetcher is not None and self.fetcher.usable(platform):
            try:
                return self.fetcher.page(url, platform, check, urgent=urgent)
            except FeedError as exc:
                if exc.kind in ("busy", "network"):     # csak a keret vagy a hálózat: később, nem a böngészővel
                    raise _Later(str(exc)) from None
        if game is not None and game.no_browser:
            raise BrowserError("böngésző nélkül most nem olvasható (a gyors sor a böngészőhöz nem nyúl)")
        return self.browser().look(url)

    def _step_search(self, game: _Round, search: dict[str, Any], sites: list[str], status: str) -> None:
        """Egy keresés: a találati oldalak, majd a szokásos árhoz mért jelöltek, vagy (ha még nincs) a felmérés."""
        rows, problems = self._collect(search, sites, game.check, game=game)
        game.errors += problems
        if not rows:
            return
        self._to_bundles([row for row in rows if not usable(row.get("title") or row["text"])], f"Keresés: {search['query']}")
        kept = self._relevant(search, rows)
        relevant = {row["url"] for row in kept}
        self.tally("other", [row for row in rows if row["url"] not in relevant])   # nem a keresett termék, nem eladó
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
        extra = [part for part in deal.get("cost_parts") or [] if part.get("what") != "posta"]
        if extra:
            warn = (" Levonva: " + ", ".join(f"{part['what']} kb. {format_price(part['amount'])}" for part in extra)
                    + f".{warn}")
        if deal.get("cond_band"):
            band = deal["cond_band"]
            warn = (f" Hasonló hibájúakhoz mértem ({band['n']} hirdetés, {', '.join(band['issues'])}; a hibátlanok "
                    f"kb. {format_price(band['clean'])}).{warn}")
        if deal.get("condition_text"):
            warn = f" Állapot: {deal['condition_text']}.{warn}"
        if deal.get("verified"):
            warn = f" {VERIFIED_TEXT}{warn}"
        elif deal.get("unchecked"):
            later = " Amint lehet, a modell is megnézi, és ha mégsem jó, szólok." if deal.get("model_pending") else ""
            warn = (f" Az adatlapja és az állapota alapján megéri, de a modell még nem nézte meg ({deal['unchecked']})."
                    f"{later}{warn}")
        if deal.get("speed"):
            warn = f" Eladási sebesség: {deal['speed']}.{warn}"
        if deal.get("safety"):
            warn = f" {safety_text(deal['safety'])}{warn}"
        origin = (f"Böngészés közben ({deal['explore']})." if deal.get("lane") else
                  f"Figyelt oldal: {deal['explore']}." if deal.get("explore") else f"Keresés: „{search['query']}”.")
        title = f"„{deal['text'][:80]}”"
        if deal.get("auction"):
            bids = f", {deal['bids']} licit" if deal.get("bids") is not None else ""
            now = "legutóbb" if deal.get("followed") else "most"     # (a licitlistáról: amikor utoljára láttam)
            return (f"Vatera-licit: {title}, {left_text(deal.get('left'))} múlva zárul, {now} {format_price(deal['price'])}"
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
            where += f"; az eltűnt (talán elkelt) hasonlók kb. {format_price(deal['sold']['median'])}"
        usual = (f"Máshol kb. {format_price(deal['median'])}-ért adják ({deal['similar']} hasonló hirdetés alapján"
                 f"{where})" if deal.get("explore") and deal.get("similar") else
                 f"A szokásos ára {format_price(deal['median'])}")
        known = f" Változat: {deal['identity']}." if deal.get("identity") else ""
        return (f"{deal['site']}: {title} {format_price(deal['price'])}{drop}. {usual}, a várható haszon kb. "
                f"{format_price(deal['profit'])}.{valuation_text(deal)}{known} {origin}{when}{warn}")

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
                    self._retries.pop(f"{search['id']}:{key}", None)
            try:
                listings = self._listings(urls[key], key, check, game)
            except BrowserError as exc:
                errors.append(f"{site.name}: {str(exc).splitlines()[0][:120]}")
                if track and isinstance(exc, _Later):
                    self._retry_soon(search, key)      # elfogyott a keret: BUSY_RETRY múlva újra (nem a böngészővel)
                continue
            if not listings:
                errors.append(f"{site.name}: nem találtam árat a találati oldalon (nincs találat, ellenőrzést kér, "
                              "vagy megváltozott az oldal)")
            found = [{"site": site.name, "platform": key, "text": str(entry.get("text") or "")[:180],
                      "price": int(entry["price"]), "url": str(entry.get("url") or ""),
                      **({"title": str(entry["title"])[:160]} if entry.get("title") else {}),
                      **({"seller": str(entry["seller"])[:40]} if entry.get("seller") else {})}
                     for entry in listings[:MAX_PER_SITE] if not entry.get("auction")]   # a licit nem eladási ár
            self.book.add(found)
            if track:
                self.tally("seen", found)
            rows += found
        return rows, errors

    def _relevant(self, search: dict[str, Any], rows: list[dict[str, Any]],
                  strict_size: bool = False) -> list[dict[str, Any]]:
        """Modell nélkül, ami biztosan nem a keresett termék, kiesik: kereső-, csere- és csomaghirdetés, alkatrész
        (selling.prefilter), ha a keresés valamelyik szava hiányzik, más a kapacitása, kiegészítő (matches_query), egy
        kizáró szó van benne, vagy a minimum ár alatt van. Ugyanaz a hirdetés (webcím) egyszer. strict_size: lásd
        matches_query (az árösszehasonlításhoz)."""
        probe = Item(id="kereses", created=0.0, updated=0.0, title=search["query"], search_query=search["query"])
        # az azonos című, hasonló árú hirdetések itt nem ismétlések (egy népszerű termékből sok van): a webcím dönt
        kept, _dropped = prefilter(probe, rows, limit=len(rows) or 1, dedupe=False)
        unique: dict[str, dict[str, Any]] = {}
        for row in kept:
            unique.setdefault(listing_key(row["url"]) if row["url"] else row["text"], row)
        words, low = search.get("exclude") or [], search.get("min_price")
        return [row for row in unique.values()
                if matches_query(search["query"], str(row.get("title") or row["text"]), strict_size)
                and not excluded(words, row["text"]) and not (low and row["price"] < low)]

    @staticmethod
    def _cheap(search: dict[str, Any], price: int, reference: int, platform: str = "") -> bool:
        return pays(search, price, reference, buy_fee(platform, price))

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
            deal["product_key"] = self.book.canon(found[0])   # az elrejtés és a vétel ehhez a termékhez számít
            speed = self.book.speed(found[0])
            deal.update(speed=speed_text(speed), pace=pace(speed))
        if "spread" not in deal and search.get("basis"):     # a felmért keresésnél a felméréskori negyedek
            deal["spread"] = spread_of({**search["basis"], "price": reference})
        if "band" not in deal and (search.get("basis") or {}).get("p25") and search["basis"].get("p75"):
            deal["band"] = [int(search["basis"]["p25"]), int(search["basis"]["p75"])]
        # külön látszik: az óvatos eladási ár, és a költségek (a posta, a Vinteden a vevővédelmi díj; az állapot szerinti
        # pótlás és javítás az ellenőrzéskor jön hozzá: _reprice), ami után a haszon marad
        fee = buy_fee(str(row.get("platform") or ""), int(extra.get("offer") or row["price"]))
        sale = safe_sale(reference, extra.get("low"))
        selling = self._sale_parts(sale)       # csomagolás, eladási díj, tartalék (amit megadtál: set_fees)
        deal["profit"] = int(deal["profit"]) - fee - sum(part["amount"] for part in selling)
        deal["resale"], deal["costs"] = sale, SHIPPING + fee + sum(part["amount"] for part in selling)
        deal["cost_parts"] = [{"what": "posta", "amount": SHIPPING}] + (
            [{"what": "vevővédelmi díj", "amount": fee}] if fee else []) + selling
        if fee:
            deal["fee"] = fee
        deal.pop("product", None)
        deal = {k: v for k, v in deal.items() if v not in ("", None)}
        self._valuation(deal)
        return deal

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
        self.tally("suspicious", [deal])
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
        self.tally("risky", [deal])
        self.journal.add(f"Kockázatos, nem szóltam: „{(deal.get('title') or deal['text'])[:60]}” "
                         f"{format_price(deal['price'])} ({deal.get('site') or deal.get('platform') or '?'}) – "
                         f"{'; '.join(deal['safety']['why'])}.")
        return True

    def tally(self, name: str, rows: list[dict[str, Any]] | tuple = (), amount: int = 0) -> None:
        """A mai számokhoz: a hirdetések (rows: a webcímük szerint, egy nap egy számban egyszer), vagy egy szám
        (amount). A mentést a következő mentés végzi."""
        today = day_of(self.clock())
        with self._lock:
            funnel = self._data["funnel"]
            if funnel["day"] != today:
                funnel.update(day=today, counts={})
                self._tallied = set()
            fresh = amount
            for row in rows:
                ident = (name, listing_key(str(row.get("url") or "")))
                if not ident[1] or ident in self._tallied:
                    continue
                if len(self._tallied) < TALLY_MAX:
                    self._tallied.add(ident)
                fresh += 1
            if fresh > 0:
                funnel["counts"][name] = int(funnel["counts"].get(name) or 0) + fresh

    def funnel(self) -> dict[str, Any]:
        """A mai számok (a felületnek): {day, counts}; ha ma még nem volt semmi, üres."""
        today = day_of(self.clock())
        with self._lock:
            funnel = self._data["funnel"]
            return {"day": today, "counts": dict(funnel["counts"]) if funnel["day"] == today else {}}

    def _deal_keys(self) -> set[str]:
        """A hirdetések, amelyekről már szóltunk (vagy visszamondtad a vételt): ezekről többé nem szól."""
        return {listing_key(d["url"]) for d in self._data["deals"] if d.get("url")} | set(self._data["cancelled"])

    def _hunt(self, search: dict[str, Any], kept: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Modell nélkül, a jóváhagyott szokásos árhoz mérve: a még nem látott (vagy azóta leárazott) hirdetések
        közül, ami megéri. A többit látottnak jegyzi, az árukkal (a leárazás észrevételéhez)."""
        reference = int(search["reference"])
        # ha most (ezen az oldalon, elég hirdetés alapján) jóval olcsóbban adják, mint a jóváhagyott szokásos ár, lejjebb
        # ment az árszint: a jelölt hasznát a mostani árhoz méri (a jóváhagyott áron túlbecsülné; az ármozgásról a
        # _drift szól), és ezt a találatban meg is mondja
        lower = ""
        current = price_stats([int(row["price"]) for row in kept])
        if current and current["n"] >= MIN_PRICES and current["median"] < reference * (1 - DRIFT):
            lower = (f"most kb. {format_price(round_price(current['median']))}-ért adják ({current['n']} hirdetés), "
                     f"kevesebbért, mint a jóváhagyott {format_price(reference)}: a mostani árhoz mértem")
            reference = round_price(current["median"])
        since = float(search.get("checked_from") or 0)
        now = self.clock()
        out: list[dict[str, Any]] = []
        haggles: list[dict[str, Any]] = []
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
                self.tally("new", [row])
                platform = str(row.get("platform") or "")
                offer = offer_price(search, row["price"], reference, platform) if platform == "vinted" else None
                if offer and not self._affordable(offer):
                    offer = None               # a vételi limitbe az ajánlat sem fér bele
                if self._cheap(search, row["price"], reference, platform) and self._affordable(row["price"]):
                    out.append(self._deal(search, row, reference, drop=old if drop else None,
                                          basis=self._search_basis(search)))
                elif offer:            # kicsit drága: a Vinteden ajánlatot tehetsz (csak a jóváhagyásoddal)
                    out.append(self._deal(search, row, reference, offer=offer, profit=expected_profit(reference, offer),
                                          basis=self._search_basis(search)))
                elif self._cheap(search, row["price"], reference, platform):
                    prices[key] = row["price"]    # olcsó, de a vételi limit fölött: ha emeled, újra jelölt lehet
                    self.tally("over_limit", [row])
                else:
                    seen[key], prices[key] = now, row["price"]
                    self.tally("not_cheap", [row])
                    haggles.append(row)        # kicsit drága: alkuval megérheti? (a zár után)
            self._prune()
            self._save()
        for row in haggles:
            self._haggle_search(search, row, reference)
        if lower:
            for deal in out:
                deal.setdefault("warnings", []).append(lower)
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
        candidates = []
        for row in picked:                    # a jelölt szokásos ára saját maga nélkül (a többi kiválasztotthoz mérve)
            others = [r for r in picked if r is not row]
            alone = price_stats([r["price"] for r in others])
            usual = round_price(alone["median"]) if alone and alone["n"] >= CALIBRATE_MIN else 0
            if usual and row["url"] and listing_key(row["url"]) not in deals \
                    and self._cheap(search, row["price"], usual) and self._affordable(row["price"]):
                used = [{"t": r["text"][:TITLE_KEEP], "p": r["price"], "s": r["site"], "u": r["url"], "src": "most"}
                        for r in sorted(others, key=lambda r: r["price"])]
                candidates.append(self._deal(search, row, usual, calibration=True, band=[alone["p25"], alone["p75"]],
                                             basis={"used": used[:BASIS_MAX], "out": [], "n_used": len(used),
                                                    "n_out": 0, "note": "a felmérés hirdetései (a modell választotta "
                                                                        "ki, melyik ugyanaz a termék)"}))
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
                   page: dict[str, Any] | None = None, done: Callable[[], None] | None = None) -> None:
        """Egy jelölt: a hirdetés oldala (modell nélkül, a böngésző szálán; ha már megnyitottuk, page), majd egy rövid
        modellkérdés a modell-sorban. Ha a kör keretébe már nem fér bele, nem jegyzi látottnak: a következő körben
        újra jelölt. done: ha az ellenőrzés lezárult (találat, vagy kiderült, hogy nem az), ez jön (a tippeknél)."""
        finish = done or (lambda: None)
        if deal.get("auction") and deal.get("far"):
            self._follow_auction(game, search, deal)   # még messze a zárás: a licitlistára, a zárás előtt újra
            return finish()
        self._lane_count(deal.get("lane"), "candidates")
        self.tally("candidates", [deal])
        if why := self._suspect(deal):
            deal["suspect"] = why
            self._suspicious(deal)             # gyanúsan olcsó: az oldalát sem nyitja meg
            return finish()
        if self._risky(deal):
            return finish()
        if not deal.get("auction"):
            self._ping(deal, game.notify)      # gyors jelzés: ígéretes, az ellenőrzés most jön
        if game.blocked and page is None:
            deal.setdefault("warnings", []).append("a hirdetés oldalát nem néztem meg (robot-ellenőrzés)")
        else:
            if page is None:
                if game.pages <= 0:
                    self._carry_over(search, deal)     # (ha az árkutatásból jött: a következő kör ezzel kezd)
                    return                     # a kör keretébe már nem fér: nem zárult le
                game.pages -= 1
                game.loaded += 1
            verdict = self._inspect(deal, page, game, urgent=True)   # (a letöltő sorában elsőbbséggel)
            if verdict == "captcha":
                game.blocked = True            # ebben a körben több hirdetést nem nyit meg
            elif verdict == "gyanús":
                self._suspicious(deal)
                return finish()
            elif verdict:
                self._mark(deal)               # elkelt vagy hibás: nem szól róla
                self.tally("gone" if verdict == "elkelt" else "defect", [deal])
                return finish()
        if game.lane is None or self._fast():  # gyors riasztás: a modell a csengetés után nézi meg (_conclude)
            self._conclude(game, search, deal, None, done=done, fast=game.lane is not None)
            return
        if game.checks <= 0:
            self._carry_over(search, deal)
            return                             # a modellkérdés keretébe már nem fér: nem zárult le
        game.checks -= 1
        game.ask(partial(self._confirm, search, deal), partial(self._conclude, game, search, deal, done=done))

    def _conclude(self, game: _Round, search: dict[str, Any], deal: dict[str, Any], ok: bool | None,
                  done: Callable[[], None] | None = None, fast: bool = False) -> None:
        """A modell válasza után (a böngésző szálán): ha nem mondott nemet, a végső minősítés (_verdict). Sürgősen csak
        az ellenőrzött ajánlatról szól (az adatlapja megnyílt, a modell átnézte, és az állapota szerint újraszámolt haszon
        is megéri); az ellenőrizetlen az Érdemes megnézni listába kerül, az okkal (_unverified). Ha az ilyen terméket
        többször elrejtetted, egy ideig nem szól. A naplóba is beírja. done: lásd _candidate. fast: gyors riasztás (a
        modellt még nem kérdezte): a csengetés után a kör modell-sorában rögtön megnézeti (_followup_check)."""
        finish = done or (lambda: None)
        if ok is False:
            if deal.get("suspect"):
                self._suspicious(deal)
            else:
                self._mark(deal)
                self.tally("model_no", [deal])
            return finish()
        state, why, hold = self._verdict(deal, ok, fast=fast)
        if state in ("hibás", "nem éri meg"):
            self._drop_checked(deal, state, why)
            return finish()
        self._mark(deal)
        if self._muted(deal.get("product_key"), self.clock()):
            game.notes.append(f"„{deal['text'][:40]}”: az ilyet elrejtetted, nem szóltam")
            self.tally("muted", [deal])
            return finish()
        if state == "ellenőrizetlen":
            self._unverified(deal, why, hold)
            return finish()
        if state == "modell nélkül":
            self._unchecked(deal, why)         # a modell nem nézte meg: így is szól, és utólag megnézeti
        else:
            deal["verified"] = True
        if self._risky(deal):                  # az újraszámolt haszonnal újra felmérve
            return finish()
        if not self._remember(deal):          # a másik sor már szólt róla (ugyanaz a hirdetés): nem még egyszer
            return finish()
        game.found += 1
        self.tally("found", [deal])
        self._lane_count(deal.get("lane"), "deals")
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
        if fast and deal.get("model_pending") and game.lane is not None and game.checks > 0:
            game.checks -= 1                   # a modell rögtön utána nézi meg (a kör modell-sorában, a keretéből)
            game.ask(partial(self._followup_check, deal["id"]),
                     lambda result, deal_id=deal["id"]: self._followup_apply(deal_id, result, game.notify))
        if deal.get("seller_url") and not deal.get("auction"):
            game.next([partial(self._step_seller, game, deal)])   # az eladó többi hirdetése: közös vétel?
        finish()

    # --- Felfedező: figyelt oldalak, keresőszó nélkül (és a Vatera-licitek) ---

    def _step_watch(self, game: _Round, watch: dict[str, Any], platform: str, running: list[dict[str, Any]]) -> None:
        """Egy figyelt oldal friss hirdetései (_page_rows, _triage); az ismeretlenekből a modell egy hívásban megnevezi a
        termékeket, és kiválasztja, ami megérhet (_explore)."""
        game.check()
        with self._lock:
            self._data["last"][f"{watch['id']}:{platform}"] = self.clock()
            self._retries.pop(f"{watch['id']}:{platform}", None)
        rows = self._page_rows(game, watch["url"], platform, watch, retry=partial(self._retry_soon, watch, platform))
        if not rows:
            return
        # a licitek elöl (azoknál sürget az idő), aztán a fix árasak, ahogy az oldalon jönnek (a legújabb elöl), a
        # fizetett kiemelések (az oldal elején, többnyire régiek) a végén: a modellkeret előbb az újakra jut
        batch = sorted(self._triage(game, watch, rows, running),
                       key=lambda r: (not r.get("auction"), bool(r.get("promoted"))))[:EXPLORE_ROWS]
        if batch:
            game.ask(partial(self._explore, batch), partial(self._explored, game, watch, batch))

    def _page_rows(self, game: _Round, url: str, platform: str, source: dict[str, Any],
                   extra: dict[str, Any] | None = None,
                   retry: Callable[[], None] | None = None) -> list[dict[str, Any]] | None:
        """Egy találati oldal hirdetései (modell nélkül, csak olvasva). A fix árasak árai az árbázisba kerülnek; a
        licitek közül csak a hamarosan záruló (a gép esti leállása előttiek is) marad. None, ha nem sikerült (ha
        robot-ellenőrzést kér, game.captcha jelzi). retry: ha csak az oldal kerete fogyott el (_Later), ez jön (pl. a
        figyelt oldal hamar újra próbálkozik)."""
        site = self.platforms[platform]
        browser = self.browser()
        try:
            listings = self._listings(url, platform, game.check, game)
        except BrowserError as exc:
            game.errors.append(f"{site.name}: {str(exc).splitlines()[0][:120]}")
            if retry is not None and isinstance(exc, _Later):
                retry()
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
        for entry in listings[:MAX_PER_SITE]:
            if not entry.get("url"):
                continue
            row = {"site": site.name, "platform": platform, "text": str(entry.get("text") or "")[:180],
                   "price": int(entry["price"]), "url": str(entry["url"]),
                   **({"title": str(entry["title"])[:160]} if entry.get("title") else {}),
                   **({"promoted": True} if entry.get("promoted") else {}), **(extra or {})}
            if entry.get("auction"):
                info = parse_auction(row["text"])
                row.update(auction=True, **info)
                if info["left"] is not None and last_chance(now + info["left"], day) - now > window:
                    row.update(far=True, hours=window // 3600)   # még messze a zárás: ha megéri, a licitlistára kerül
            rows.append(row)
        self._auction_seen([row for row in rows if row.get("auction")])
        self.book.add(rows)                    # a licitet kihagyja (az nem eladási ár)
        self.tally("seen", rows)
        return rows

    def _triage(self, game: _Round, source: dict[str, Any], rows: list[dict[str, Any]],
                running: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Modell nélkül: ami egy felmért keresésedre illik, azt annak a szokásos árához méri; ami az árbázisból
        felismerhető, azt az árbázishoz; ha annak az ára nem elég biztos (sure_price), előbb a többi oldalon is
        megnézi (_resale). A jelöltek (_candidate) rögtön jönnek; visszaadja az ismeretleneket (ezeket a modell
        nevezheti meg). A már látottak (ha nem árazták le) és a nem eladási árak (kereső, csomag, alkatrész…)
        kimaradnak."""
        unknown, found, skip, recheck, lots, wear = [], [], [], [], [], []
        with self._lock:
            seen, prices, deals = dict(self._data["seen"]), dict(self._data["prices"]), self._deal_keys()
            cancelled = set(self._data["cancelled"])
        for row in rows:
            key = listing_key(row["url"])
            old = prices.get(key)
            drop = old is not None and not row.get("auction") and row["price"] <= old * (1 - DROP)
            if (key in seen and not drop) or (key in deals and not drop) or key in cancelled:
                continue
            self.tally("new", [row])
            # a cím dönt, ha van (a kártya szövegében a város, az idő és az eladó neve is ott van: „eladó41”)
            if not usable(row.get("title") or row["text"]):
                lots.append(row)               # nem eladási ár (kereső, csere, csomag, alkatrész…); a csomag felmérhető
                continue
            if not self._within_reach(row):
                self.tally("over_limit", [row])
                continue                       # a vételi limit fölött: nem jegyzi látottnak (ha emeled, sorra kerül)
            search = next((s for s in running if matches_query(s["query"], str(row.get("title") or row["text"]))
                           and not excluded(s.get("exclude") or [], row["text"])), None)
            if search is None and wearable(row.get("title") or row["text"]):
                wear.append(row)               # ruhával nem kereskedik (a kifejezett keresésed ettől még mehet)
                continue
            label = row.get("lane_name") or source["name"]
            if search is not None:             # egy keresésedre illik: annak a szokásos ára dönt
                reference = int(search["reference"])
                top = highest_ok(search, reference)
                if top:
                    top = self._capped(top)
                basis = self._search_basis(search)
                if row.get("auction") and top and row["price"] < top:
                    found.append((search, self._deal(search, row, reference, max_bid=top, explore=label, basis=basis)))
                elif not row.get("auction") and self._cheap(search, row["price"], reference, row.get("platform") or "") \
                        and self._affordable(row["price"]):
                    found.append((search, self._deal(search, row, reference, explore=label,
                                                     drop=old if drop else None, basis=basis)))
                else:
                    skip.append(row)
                    self._haggle_search(search, row, reference)   # kicsit drága: alkuval megérheti?
                continue
            product = self.book.key(row.get("title") or row["text"])   # a cím (a kártyán a város, az eladó is ott van)
            self._note_demand(product[0] if product else None, row["url"])
            reference = self._screened(self.book.reference(product[0], exclude=row["url"]), self._want(row)) \
                if product else None
            if reference is None:
                unknown.append(row)
                continue
            deal = self._explore_deal(source, row, reference, drop=old if drop else None)
            if deal is None:
                skip.append(row)
                self._haggle_explore(source, row, reference)       # kicsit drága: alkuval megérheti?
            elif not reference["sure"]:        # kevés az ár, vagy az összehasonlítók leírását még nem olvastuk el
                if game.resale > 0:            # a keretbe már nem férő a következő körben újra jön
                    game.resale -= 1
                    query = self.book.query(row.get("title") or row["text"]) or reference["name"]
                    recheck.append(partial(self._resale, game, source, row, query))
            else:
                found.append(({"id": source["id"], "query": reference["name"]}, deal))
        lane = next((row.get("lane_name") for row in lots if row.get("lane_name")), "")
        self.tally("not_cheap", skip)
        self.tally("clothing", wear)
        other = self._to_bundles(lots, f"Böngészés közben ({lane})" if lane else f"Figyelt oldal: {source['name']}")
        self.tally("other", other)
        self._mark(*skip, *wear, *other)
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
                "spread": spread_of(reference), "sold": reference.get("sold"), "band": band_of(reference),
                "basis": self._named_basis(reference.get("basis")),
                "refs": (reference.get("vetted") or {}).get("refs") or None,
                "floor": int(watch.get("min_profit") or WATCH_PROFIT), **market_info(reference), **extra}
        if row.get("auction"):
            top = self._capped(top)            # a vételi limitnél tovább nem érdemes licitálni
            return self._deal(probe, row, usual, max_bid=top, **info) if 0 < top and price < top else None
        if price < usual * FLOOR or top <= 0:
            return None
        top = before_fee(str(row.get("platform") or ""), top)     # a Vinteden a vevővédelmi díjjal együtt
        if price <= top and self._affordable(price):
            return self._deal(probe, row, usual, **info)
        offer = self._capped(top) // 100 * 100
        if row.get("platform") == "vinted" and 0 < offer < price and offer >= price * (1 - OFFER_CUT):
            return self._deal(probe, row, usual, offer=offer, profit=expected_profit(usual, offer, low), **info)
        return None

    def _named_basis(self, basis: dict[str, Any] | None) -> dict[str, Any] | None:
        """A „mihez mérte” lista az oldalak nevével (az árbázis az oldal kulcsát tárolja)."""
        if not basis:
            return None
        name = lambda entry: {**entry, "s": self.platforms[entry["s"]].name if entry.get("s") in self.platforms  # noqa: E731
                              else entry.get("s") or ""}
        return {**basis, "used": [name(e) for e in basis.get("used") or []][:BASIS_MAX],
                "out": [name(e) for e in basis.get("out") or []][:BASIS_MAX],
                "n_used": int(basis.get("n_used") or len(basis.get("used") or [])),
                "n_out": int(basis.get("n_out") or len(basis.get("out") or []))}

    def _search_basis(self, search: dict[str, Any]) -> dict[str, Any]:
        """Egy keresésed jelöltjének „mihez mérte” listája: a szokásos árat te hagytad jóvá (vagy adtad meg); ha felmérés
        volt, annak a hirdetései."""
        rows = (search.get("proposal") or {}).get("rows") or []
        used = [{"t": str(r.get("text") or "")[:TITLE_KEEP], "p": int(r["price"]), "s": str(r.get("site") or ""),
                 "u": str(r.get("url") or ""), "src": "felmérés"} for r in rows if r.get("price")]
        note = (f"A szokásos árat ({format_price(int(search['reference']))}) te hagytad jóvá"
                + ("; a felméréskor ezekből javasoltam." if used else " (kézzel megadva)."))
        return {"used": used[:BASIS_MAX], "out": [], "n_used": len(used), "n_out": 0, "note": note, "given": True}

    def _site_names(self, keys: list[str] | None) -> list[str]:
        return [self.platforms[k].name if k in self.platforms else k for k in keys or []]

    def _market_of(self, row: dict[str, Any], query: str, rows: list[dict[str, Any]],
                   read: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
                   reads: int = 0) -> dict[str, Any] | None:
        """Mennyiért adják biztosan: az árbázis friss árai (ha a terméket ismeri) és a most talált hasonlók,
        hirdetésenként egyszer, a jelölt nélkül (market). Csak azt méri össze, ami tényleg ugyanaz:
        - ugyanaz a termékkulcs (a márka, a kártyagyártó típusneve, a sorozatnév nem számít: „MSI GTX 1080 OC” =
          „Gigabyte GTX 1080 G1”); ha a névből nincs kulcs, aminek a szavaira illik (matches_query);
        - ugyanaz a változat: a kapacitások nem keverednek (8/16 GB, 500 GB/1 TB, és a készlet: 2×4 nem 2×8 GB); ha a
          névben nincs kapacitás, a legolcsóbb (legalább MARKET_GROUP_MIN árral) dönt, így egy ismeretlen kapacitású
          hirdetés nem látszik olcsónak a drágább változathoz mérve; ha a típus csak egy kapacitással készült (a GTX 1080
          mind 8 GB-os: implied_size), a kapacitás nélküli címek is ahhoz számítanak;
        - egy működő darab ára: a csomag, a több darab, a hibás vagy alkatrész, a kiegészítő, a „-tól” ár és a több
          terméket árusító hirdetés kimarad (pricebook.unusable).
        Ha egy most talált hirdetés címéből nem derül ki a változata, és a biztos összehasonlításhoz kellene, legfeljebb
        reads ilyennek elolvassa a leírását (read: a hirdetés oldala); ha abból sem tisztázható, nem számít bele.
        Ha a most talált hirdetések jóval olcsóbbak a korábban látottaknál (az árszint lejjebb ment), azokhoz mér.
        A market mellett: basis (mihez mérte, mit zárt ki és miért: a találat ablakában megnyitható), peers (a most
        talált, összehasonlítható hirdetések: önálló jelöltek lehetnek, _peers) és group (a választott változat árai)."""
        own = listing_key(row["url"])
        product = self.book.key(query)
        groups: dict[str, dict[str, dict[str, Any]]] = {}
        out: list[dict[str, Any]] = []         # a kizárt összehasonlítók, az okkal

        def put(size: str, sample: dict[str, Any]) -> None:
            groups.setdefault(size, {})[listing_key(sample["u"])] = sample

        def fresh(found: dict[str, Any], why: str = "", exact: bool = False) -> dict[str, Any]:
            sample = {"t": " ".join(str(found.get("title") or found.get("text") or "").split())[:TITLE_KEEP],
                      "p": int(found["price"]), "s": str(found.get("platform") or ""), "u": str(found["url"]),
                      "src": "most", "row": found, **({"v": str(found["seller"])} if found.get("seller") else {})}
            if exact:
                sample["exact"] = True
            return {**sample, "why": why} if why else sample

        want = ""
        # egy most talált hirdetés friss állapota felülírja a korábban eltárolt árát: ha most kizárt (pl. a címébe
        # bekerült, hogy hibás), a régi ára sem számít, és egy hirdetés nem lehet egyszerre beszámítva és kizárva
        now_seen = {listing_key(found["url"]) for found in rows if found.get("url")}
        first_seen: dict[str, float] = {}      # hirdetés → mikor láttuk először (mióta van fent: _outliers)
        if product is not None:
            base, want = _split_size(product[0])
            for key, size in self.book.family(product[0]):
                for sample in self.book.samples(key, exclude=row["url"]):
                    if not sample["url"]:
                        continue
                    first_seen[listing_key(sample["url"])] = sample["at"]
                    if listing_key(sample["url"]) not in now_seen:
                        up = listed_days(sample, self.clock())
                        put(size, {**basis_entry(sample), "exact": True, **({"up": up} if up is not None else {})})
            for found in rows:
                if not found.get("url") or listing_key(found["url"]) == own:
                    continue
                text = str(found.get("title") or found.get("text") or "")
                why = unusable(text)
                if why:
                    out.append(fresh(found, why))
                    continue
                other = self.book.key(text)
                if other is not None and _split_size(other[0])[0] == base:
                    put(_split_size(other[0])[1], fresh(found, exact=True))
                elif not found.get("title") and matches_query(query, text, strict_size=True):
                    put(want, fresh(found))    # cím nélkül (a kártya teljes szövege): a szavai szerint
                else:
                    out.append(fresh(found, "más termék vagy típus" if other else "a címből nem derül ki, melyik termék"))
            implied = implied_size(base)
            if implied and "" in groups:       # a típus csak egy kapacitással készült: a kapacitás nélküliek is az
                groups.setdefault(implied, {}).update(groups.pop(""))
            if want and "" in groups:          # a kapacitás nem derül ki a címből: nem tudni, melyik változat
                unclear = list(groups.pop("").values())
                if read is not None and len(groups.get(want, {})) < SURE_MIN:
                    unclear = self._clarify(unclear, want, read, reads, put, out)
                out += [{**sample, "why": sample.get("why") or "a címből nem derül ki, melyik változat (kapacitás)"}
                        for sample in unclear]
            if want:
                for size, group in groups.items():
                    if size != want:
                        out += [{**sample, "why": f"más változat ({size.upper()})"} for sample in group.values()]
                groups = {want: groups.get(want, {})}
        else:
            for found in rows:
                if not found.get("url") or listing_key(found["url"]) == own:
                    continue
                text = str(found.get("title") or found.get("text") or "")
                why = unusable(text)
                if why:
                    out.append(fresh(found, why))
                elif matches_query(query, text, strict_size=True):
                    put(" ".join(sorted(sizes_of(text))), fresh(found))
                else:
                    out.append(fresh(found, "más termék vagy típus"))
        for group in groups.values():
            group.pop(own, None)
        markets = [(size, market([(s["p"], s["s"], bool(s.get("gone"))) for s in group.values()]))
                   for size, group in groups.items() if group]
        markets = [(size, found) for size, found in markets if found]
        if not markets:
            return None
        enough = [(size, found) for size, found in markets if found["n"] >= MARKET_GROUP_MIN]
        size, found = (markets[0] if len(markets) == 1 else
                       min(enough, key=lambda entry: entry[1]["price"]) if enough else
                       max(markets, key=lambda entry: entry[1]["n"]))
        for other, group in groups.items():
            if other != size:
                label = other.upper() if other else "a kapacitás nem derül ki"
                out += [{**sample, "why": f"más változat ({label})"} for sample in group.values()]
        # a most látott elöl, utána a legutóbb látott (két egyforma hirdetésből a frissebb marad: independent)
        group = sorted(groups[size].values(), key=lambda sample: (sample["src"] != "most", -float(sample.get("seen") or 0)))
        found = dict(found)
        # összehasonlíthatóság a vizsgált hirdetéshez (a termékadataik szerint, specs.compare): a kizárt és a
        # bizonytalan kimarad, az okkal; a közelítő jelölve beszámít. Egy jó szöveges hasonlóság egy kritikus eltérést
        # (más memória, kiszerelés, plusz tartozék) nem ír felül. Egy hirdetés egyszer számít (a máshova is feltett
        # példánya, és a vizsgált hirdetés máshol fent lévő példánya sem), egy eladótól legfeljebb kettő.
        want = self._want(row)
        group, rejected = self._comparables(want, group)
        if rejected:
            out += rejected
            again = market([(s["p"], s["s"], bool(s.get("gone"))) for s in group]) if group else None
            if again is None:
                return None
            found = dict(again)
        now_rows = [sample for sample in group if sample["src"] == "most"]
        lower = market([(s["p"], s["s"]) for s in now_rows]) if len(now_rows) >= MARKET_GROUP_MIN else None
        if lower is not None and lower["price"] < found["price"] * (1 - FRESH_GAP):
            # a most talált hirdetések jóval olcsóbbak a korábban látottaknál: lejjebb ment az árszint, azokhoz mér
            found = {**lower, "note": "a mostani hirdetések olcsóbbak a korábban látottaknál: azokhoz mértem"}
            out += [{**sample, "why": "régebbi ár (azóta lejjebb ment az árszint)"} for sample in group
                    if sample["src"] != "most"]
            group = now_rows
        # az összehasonlítók állapota a leírásuk szerint (a címből nem derül ki, hogy repedt a kijelzője, vagy nincs
        # töltője): a szokásos árhoz legközelebbiekét elolvassa (ha lehet, _vet); a hibás vagy hiányos példány ára nem
        # számít a hibátlanok szokásos árába (a hasonló hibájú jelölthöz viszont ez a mérce: vetted.refs, _reprice)
        if read is not None:
            self._vet(group, read, VET_READS, int(found["price"]))
        group, flawed, refs, clean = self._screen(group)
        if flawed:
            out += flawed
            again = market([(s["p"], s["s"], bool(s.get("gone"))) for s in group]) if group else None
            if again is None:
                return None
            found = {**again, **({"note": found["note"]} if found.get("note") else {})}
        # a kilógóan olcsó, most is fent lévő hasonló: versenytárs vagy nem tudni, miért olcsó (_cheap_label; külön
        # kérés nélkül: az eltárolt állapota szerint, amit egy korábbi összehasonlításnál vagy jelöltként olvasott el)
        now = self.clock()
        for sample in group:                   # a most látott: mióta van fent (ha az árbázis már korábban látta)
            if sample["src"] == "most" and sample.get("u") and listing_key(sample["u"]) in first_seen:
                sample["up"] = round(max(0.0, now - first_seen[listing_key(sample["u"])]) / 86400, 1)
        labels = self._cheap_label(self._outliers(group, found), found)
        group = [labels.get(listing_key(sample["u"]), sample) if sample.get("u") else sample for sample in group]
        found["enough"] = bool(found["sure"])  # elég ár van (sure_price), de a leírások nélkül ez még nem biztos
        if found["enough"] and fresh_count(group, now) < FRESH_MIN:
            found["enough"], found["stale"] = False, stale_text(bool(rows))   # régi árak: a mostaniak kellenek
        unsure = specs.unsure_variant(want)
        if unsure and size:                    # egy ismert változathoz (a legolcsóbbhoz) mérve: óvatos, lehet biztos
            found["variant_note"] = (f"a hirdetésből nem derül ki: {', '.join(unsure)}; a legolcsóbb változathoz "
                                     f"({size.upper()}) mértem")
            unsure = []
        found["sure"] = found["enough"] and clean >= VET_MIN and not unsure
        if unsure:
            found["unsure_why"] = (f"a hirdetésből nem derül ki: {', '.join(unsure)} (a terméknek több ilyen változata "
                                   "van)")
        if want.get("category"):
            found["identity"] = specs.describe(want)
        found["vetted"] = {"refs": refs, "clean": clean}
        found["basis"] = self._basis(group, out, found.get("note", ""))
        found["group"], found["peers"] = group, [sample for sample in group if sample["src"] == "most"]
        # a többi jelölthöz: a kizártak (az okkal), és maga a vizsgált hirdetés, ha a címe szerint igazoltan ugyanaz a
        # termék és változat (a többieknek ő is összehasonlító; ha csak a modell nevezte meg, nem számít bele)
        found["excluded"] = out
        title = str(row.get("title") or row.get("text") or "")
        mine = self.book.key(title) if product is not None and usable(title) else None
        same = (mine is not None and _split_size(mine[0])[0] == base
                and (_split_size(mine[0])[1] or implied_size(base)) == size) if product is not None else \
            usable(title) and matches_query(query, title, strict_size=True)
        if same and row.get("url"):
            found["own"] = {**fresh(row), "exact": True}
        return found

    def _peers(self, row: dict[str, Any], found: dict[str, Any],
               judge: Callable[[dict[str, Any], dict[str, Any]], tuple[float, Any] | None],
               limit: int = SPAWN_MAX) -> list[tuple[float, Any]]:
        """Az árkutatás közben most talált, összehasonlítható hirdetések önálló vételi jelöltként: mindegyik a többihez
        mérve, saját maga nélkül (a kilógóan olcsó is, ami a szokásos árból kimaradt: lehet kivételes ajánlat, hibás vagy
        félrevezető ár; ezt a jelölt ellenőrzése dönti el). judge(minta, a saját szokásos ára) → (pontszám, jelölt) vagy
        None. Ami már találat, amit már láttunk (ezen az áron), vagy amit nemrég egy másik összehasonlításnál már jelöltnek
        vett (_spawned), kimarad; a legjobb pontszámú legfeljebb limit (ezeket jegyzi fel). Visszaadja őket, a legjobb
        elöl."""
        group, own, now = list(found.get("group") or []), listing_key(row["url"]), self.clock()
        with self._lock:
            known, seen, prices = self._deal_keys(), set(self._data["seen"]), dict(self._data["prices"])
            self._spawned = {k: t for k, t in self._spawned.items() if now - t < SPAWN_TTL}
            spawned = set(self._spawned)
        picked: list[tuple[float, Any, str]] = []
        for peer in found.get("peers") or []:
            key = listing_key(peer["u"])
            if key == own or key in known or key in spawned or not self._affordable(peer["p"]):
                continue
            if key in seen and key in prices and peer["p"] > int(prices[key]) * (1 - DROP):
                continue                       # már láttuk ezen az áron (és nem lett jelölt): nem nézi újra
            rest = [sample for sample in group if sample is not peer] + ([found["own"]] if found.get("own") else [])
            reference = market([(s["p"], s["s"], bool(s.get("gone"))) for s in rest])
            if reference is None:
                continue
            clean = self._screen(rest)[3]
            reference["enough"] = bool(reference["sure"])
            reference["sure"] = reference["enough"] and clean >= VET_MIN and \
                not specs.unsure_variant(specs.extract(str(peer.get("t") or "")))
            reference["vetted"] = {"refs": list((found.get("vetted") or {}).get("refs") or []), "clean": clean}
            reference["basis"] = self._basis(rest, list(found.get("excluded") or []), found.get("note", ""))
            verdict = judge(peer, reference)
            if verdict is not None:
                picked.append((verdict[0], verdict[1], key))
        picked.sort(key=lambda entry: -entry[0])
        with self._lock:
            for _score, _what, key in picked[:max(0, limit)]:
                self._spawned[key] = now
        return [(score, what) for score, what, _key in picked[:max(0, limit)]]

    def _unspawn(self, *urls: str) -> None:
        """Egy árkutatásból vett jelölt ellenőrzése nem zárult le (elfogyott a keret, Szünet): egy későbbi
        összehasonlításnál újra jelölt lehet (_peers)."""
        with self._lock:
            for url in urls:
                self._spawned.pop(listing_key(url), None)

    def _carry_over(self, search: dict[str, Any], deal: dict[str, Any]) -> None:
        """Egy árkutatásból jött jelölt (spawned) a kör keretébe már nem fért: a következő kör ezzel kezd (_step_carry).
        Mióta vár, az az első félretételtől számít (CARRY_TTL); legfeljebb CARRY_MAX vár (a legrégebbiek esnek ki)."""
        if not deal.get("spawned"):
            return
        key, now = listing_key(deal["url"]), self.clock()
        with self._lock:
            first = self._carry[key][0] if key in self._carry else now
            self._carry[key] = (first, search, deal)
            for old in sorted(self._carry, key=lambda k: self._carry[k][0])[:max(0, len(self._carry) - CARRY_MAX)]:
                self._carry.pop(old, None)
                self._spawned.pop(old, None)

    def _step_carry(self, game: _Round) -> None:
        """Az előző körök keretébe nem fért, árkutatásból jött jelöltek most előbb (a nagyobb haszon elöl). A lejártak
        (CARRY_TTL) és a közben megtaláltak lekerülnek; a gyors sor csak azt nézi, amit böngésző nélkül megnyithat, és
        amit a másik sor épp ellenőriz, azt kihagyja. Ami most sem fér bele, várhat tovább (_carry_over)."""
        now = self.clock()
        with self._lock:
            known = self._deal_keys()
            for key in [k for k, (at, _s, _d) in self._carry.items() if k not in self._carry_busy
                        and (now - at > CARRY_TTL or k in known)]:
                self._carry.pop(key, None)
                if key not in known:
                    self._spawned.pop(key, None)   # lejárt: egy későbbi árkutatásnál újra jelölt lehet
            ready = [(key, search, deal) for key, (_at, search, deal) in self._carry.items()
                     if key not in self._carry_busy and (not game.no_browser or (
                         self.fetcher is not None and self.fetcher.usable(str(deal.get("platform") or ""))))]
            self._carry_busy.update(key for key, _s, _d in ready)
        game.carry += [key for key, _s, _d in ready]
        ready.sort(key=lambda entry: -int(entry[2].get("profit") or 0))
        game.next([partial(self._candidate, game, search, deal, done=partial(self._carry_done, key))
                   for key, search, deal in ready])

    def _carry_done(self, key: str) -> None:
        """Egy átvitt jelölt ellenőrzése lezárult (_candidate): lekerül a sorról."""
        with self._lock:
            self._carry.pop(key, None)
            self._carry_busy.discard(key)

    def _release_carry(self, game: _Round) -> None:
        """A kör végén: az átvitt jelöltek közül, ami nem zárult le, a következő kör (akár a másik sor) újra megpróbálja."""
        with self._lock:
            self._carry_busy.difference_update(game.carry)

    def _clarify(self, unclear: list[dict[str, Any]], want: str, read: Callable[[dict[str, Any]], dict[str, Any] | None],
                 reads: int, put: Callable[[str, dict[str, Any]], None], out: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """A címből nem derül ki a változat (kapacitás): legfeljebb reads most talált hirdetés leírását elolvassa (a
        legolcsóbbakét előbb), és ha abból kiderül, hogy ez a keresett változat (és nem hibás), beszámítja; ha más
        változat vagy hibás, az okkal kizárja. Visszaadja, ami így sem tisztázódott (azok nem számítanak bele)."""
        left: list[dict[str, Any]] = []
        wanted = set(want.split())
        for sample in sorted(unclear, key=lambda sample: (sample["src"] != "most", sample["p"])):
            if reads <= 0 or sample["src"] != "most":
                left.append(sample)
                continue
            reads -= 1
            try:
                page = read(sample["row"])
            except Deferred as exc:
                if exc.reason != "kéréskeret":
                    raise
                reads, page = 0, None          # a keret elfogyott: a többit nem olvassa el
            text = f"{(page or {}).get('title') or ''} {(page or {}).get('text') or ''}"
            if not text.strip():
                left.append({**sample, "why": "a leírását nem tudtam elolvasni"})
                continue
            folded = fold(text[:PAGE_TEXT])
            sizes = sizes_of(text[:PAGE_TEXT])
            if sample.get("u"):
                self._vet_note(sample["u"], text[:PAGE_TEXT])
            if _PARTS_RE.search(folded) or _DEFECT_RE.search(folded):
                out.append({**sample, "why": "hibás vagy hiányos (a leírása szerint)"})
            elif wanted and wanted <= sizes and not size_conflict(sized(want), sized(text[:PAGE_TEXT])):
                put(want, {**sample, "read": True})
            elif sizes - wanted and not sizes & wanted:
                out.append({**sample, "why": f"más változat (a leírása szerint {', '.join(sorted(sizes)).upper()})"})
            else:
                left.append({**sample, "why": "a leírásából sem derül ki, melyik változat"})
        return left

    # --- az összehasonlítók állapota a leírásuk szerint (allapotok.json) ---

    def _vet_note(self, url: str, text: str) -> list[str]:
        """Egy hirdetés állapota a leírásából (kulcsszavakkal, conditions.from_text), eltárolva: az árat érintő hibái
        (issues), és state: „hibás” (van ilyen hibája), „hibátlan” (a leírása szerint működik, és nincs ilyen hibája:
        ehhez bizonyíték kell, conditions.clean_evidence), vagy „ismeretlen” (az állapotáról nem ír: ez nem hibátlan),
        röviden, mi a baj vagy mi szól mellette. Visszaadja a hibákat."""
        cond = conditions.from_text(text)
        found = conditions.issues(cond)
        proof = "" if found else conditions.clean_evidence(cond)
        entry: dict[str, Any] = {"at": self.clock(), "issues": found,
                                 "state": "hibás" if found else "hibátlan" if proof else conditions.UNKNOWN}
        if found:
            entry["note"] = conditions.describe(cond)[:200]
        elif proof:
            entry["note"] = proof[:200]
        with self._lock:
            self._vetted[listing_key(url)] = entry
            self._vet_dirty = True
        return found

    def _vet(self, samples: list[dict[str, Any]], read: Callable[[dict[str, Any]], dict[str, Any] | None] | None,
             reads: int, reference: int) -> int:
        """Az összehasonlítók közül a szokásos árhoz (reference) legközelebbi legfeljebb reads még el nem olvasott
        leírását elolvassa (a most találtakat előbb; az eltűntét nem), és az állapotukat eltárolja (_vet_note). Ami nem
        nyílt meg, eltűnt vagy robot-ellenőrzést kért, nem számít ellenőrzöttnek. Ha elfogy a kéréskeret, a többit nem
        olvassa el; legfeljebb kétszer annyit próbál, amennyit elolvasna. Visszaadja, hányat olvasott el."""
        if read is None or reads <= 0:
            return 0
        noted = 0
        with self._lock:
            todo = [sample for sample in samples if sample.get("u") and not sample.get("gone")
                    and self._unvetted(listing_key(sample["u"]))]
        todo.sort(key=lambda sample: (sample.get("src") != "most", abs(int(sample["p"]) - reference)))
        tries = reads * 2
        for sample in todo:
            if reads <= 0 or tries <= 0:
                break
            tries -= 1
            row = sample.get("row") or {"url": sample["u"], "platform": sample.get("s") or "", "price": int(sample["p"]),
                                        "text": sample.get("t") or "", "title": sample.get("t") or ""}
            try:
                page = read(row)
            except Deferred as exc:
                if exc.reason != "kéréskeret":
                    raise
                break                          # a keret elfogyott: a többit nem olvassa el
            if not page or page.get("captcha"):
                continue
            state, _detail = classify(page, sample["u"], str(row.get("text") or ""), str(row.get("platform") or ""))
            text = " ".join(str(page.get("text") or "").split())[:PAGE_TEXT]
            if state in (GONE, BLOCKED) or len(text) < MIN_DETAILS:
                continue
            reads -= 1
            noted += 1
            self._vet_note(sample["u"], f"{page.get('title') or sample.get('t') or ''} {text}")
        if self._vet_dirty:
            self._save_vetted()
        return noted

    @staticmethod
    def _outliers(group: list[dict[str, Any]], found: dict[str, Any]) -> list[dict[str, Any]]:
        """A kilógóan olcsó (a szokásos árból kimaradó: market range) hasonlók, amik most is fent vannak (a most
        találtak, vagy az árbázisból nemrég látottak, nem eltűntek: up, listed_days), a legolcsóbb elöl."""
        bounds = found.get("range") or []
        if not bounds:
            return []
        return sorted((sample for sample in group if int(sample["p"]) < int(bounds[0]) and not sample.get("gone")
                       and (sample.get("src") == "most" or sample.get("up") is not None)),
                      key=lambda sample: int(sample["p"]))

    def _cheap_label(self, cheap: list[dict[str, Any]], found: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """A kilógóan olcsó, most is fent lévő hasonlók (_outliers) nem esnek ki szó nélkül, a magyarázatuk:
        - a leírása szerint hibátlan, pontosan ugyanaz a termék (nem közelítő), és legalább CHEAP_STAYED napja fent van
          (ennyiért sem kelt el): versenytárs, found["cheap"] a legolcsóbbé, a gyors eladási becslés lejjebb megy
          (careful_low);
        - ugyanilyen, hibátlan, de friss: valószínűleg hamar elkel (kivételes ajánlat), nem a piaci ár; jelölt lehet
          (_peers);
        - nem derül ki, miért olcsó (nincs elolvasva, vagy az állapotáról nem ír): a szokásos árba nem számít.
        (A leírása szerint hibás már a kizártak között van, az okkal: _screen.) {a hirdetés kulcsa: a minta a
        magyarázattal (cheap, cheap_why)}."""
        with self._lock:
            states = {listing_key(s["u"]): (self._vetted.get(listing_key(s["u"])) or {}).get("state")
                      for s in cheap if s.get("u")}
        labels: dict[str, dict[str, Any]] = {}
        for sample in cheap:
            if not sample.get("u"):
                continue
            ident, up = listing_key(sample["u"]), sample.get("up")
            candidate = "; ha megéri, jelöltként külön ellenőrzöm" if sample.get("src") == "most" else ""
            if states.get(ident) == "hibátlan" and not sample.get("approx") and up is not None and up >= CHEAP_STAYED:
                found.setdefault("cheap", {"p": int(sample["p"]), "u": str(sample["u"]), "t": str(sample.get("t") or ""),
                                           "up": up})
                labels[ident] = {**sample, "cheap": "versenytárs",
                                 "cheap_why": f"olcsó, de a leírása szerint ugyanilyen és hibátlan, és {up:g} napja fent "
                                              "van (ennyiért sem kelt el): versenytárs, a gyors eladási árat lejjebb "
                                              "vettem"}
            elif states.get(ident) == "hibátlan" and not sample.get("approx"):
                labels[ident] = {**sample, "cheap": "friss",
                                 "cheap_why": "olcsó, a leírása szerint ugyanilyen és hibátlan, de friss hirdetés (hamar "
                                              "elkelhet, nem a piaci ár): a szokásos árba nem számít" + candidate}
            else:
                labels[ident] = {**sample, "cheap": "ismeretlen",
                                 "cheap_why": "kilógóan olcsó, nem derül ki, miért (hibás, félrevezető ár vagy kivételes "
                                              "ajánlat lehet): a szokásos árba nem számít" + candidate}
        return labels

    def _unvetted(self, ident: str) -> bool:
        """Nincs még eltárolt állapota a hirdetésnek (a hívó tartja a zárat). A régi, state nélküli „hibátlan” bejegyzés
        (amikor még a semmit nem mondó leírás is annak számított) újra olvasandó."""
        entry = self._vetted.get(ident)
        return entry is None or ("state" not in entry and not entry.get("issues"))

    def _screen(self, samples: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                                                              list[dict[str, Any]], int]:
        """Az összehasonlítók az eltárolt állapotukkal (_vet): (a beszámíthatók: a hibátlanok és az ismeretlen
        állapotúak; a leírásuk szerint hibásak vagy hiányosak, az okkal; ezek röviden a hasonló hibájú jelöltekhez:
        refs {p, u, t, s, issues}; és hány a leírása szerint is hibátlan)."""
        with self._lock:
            entries = [self._vetted.get(listing_key(sample["u"])) if sample.get("u") else None for sample in samples]
        kept, flawed, refs, clean = [], [], [], 0
        for sample, entry in zip(samples, entries):
            found = list((entry or {}).get("issues") or [])
            if entry is not None and not found and entry.get("state") == "hibátlan":
                clean += 1                     # (az állapotáról nem író leírás nem hibátlan, csak ismeretlen)
            if not found:
                kept.append(sample)
                continue
            flawed.append({**sample, "why": f"a leírása szerint {conditions.issue_text(found)}"})
            refs.append({"p": int(sample["p"]), "u": str(sample["u"]), "t": str(sample.get("t") or ""),
                         "s": str(sample.get("s") or ""), "issues": found})
        return kept, flawed, refs, clean

    def _screened(self, reference: dict[str, Any] | None, want: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Az árbázis szokásos ára (book.reference) az összehasonlítók eltárolt állapotával (_screen): a leírásuk
        szerint hibásak nélkül újraszámolva (azok a kizártak közé kerülnek, az okkal); enough: elég hasonló ár van
        (sure_price), sure: és ezek közül legalább VET_MIN a leírása szerint is hibátlan. want: a vizsgált hirdetés
        termékadatai (specs.extract): az összehasonlítók közül a kizártak és a bizonytalanok kimaradnak, a közelítők
        jelölve beszámítanak (_comparables); ha a vizsgált hirdetésből egy változat-adat nem derül ki (pl. az RX 580
        memóriája), az ára nem lehet biztos (unsure_why). None, ha nem marad elég ár."""
        if reference is None:
            return None
        basis = dict(reference.get("basis") or {})
        used, rejected = self._comparables(want, list(basis.get("used") or []))
        kept, flawed, refs, clean = self._screen(used)
        out = dict(reference)
        if flawed or rejected:
            again = market([(s["p"], s["s"], bool(s.get("gone"))) for s in kept]) if kept else None
            if again is None:
                return None
            out.update({k: again[k] for k in ("price", "p25", "p75", "n", "sites")})
            out.pop("sold", None)
            if again.get("sold"):
                out["sold"] = again["sold"]
            basis.update(used=kept, out=list(basis.get("out") or []) + rejected + flawed)
            out["basis"] = basis
        elif used != list(basis.get("used") or []):
            basis.update(used=kept)            # (a közelítők jelölve)
            out["basis"] = basis
        out["enough"] = sure_price(out)
        if out["enough"] and fresh_count(kept, self.clock()) < FRESH_MIN:
            out["enough"], out["stale"] = False, stale_text()   # elavult lehet: előbb a mostani árak (FRESH_MIN)
        unsure = specs.unsure_variant(want) if want else []
        if unsure and any(is_capacity(word) for word in str(out.get("key") or "").split()):
            # a vizsgáltnál nem derül ki a változat, de egy ismert változat (az árbázis szerint a legolcsóbb) a mérce
            out["variant_note"] = (f"a hirdetésből nem derül ki: {', '.join(unsure)}; a legolcsóbb változathoz "
                                   f"({_split_size(str(out['key']))[1].upper()}) mértem")
            unsure = []
        out["sure"] = out["enough"] and clean >= VET_MIN and not unsure
        if unsure:
            out["unsure_why"] = f"a hirdetésből nem derül ki: {', '.join(unsure)} (a terméknek több ilyen változata van)"
        out["vetted"] = {"refs": refs, "clean": clean}
        if want and want.get("category"):
            out["identity"] = specs.describe(want)
        # a kilógóan olcsó, most is fent lévő hasonlók: versenytárs (a leírása szerint ugyanilyen és hibátlan) vagy
        # nem tudni, miért olcsó (_cheap_label); csak az összehasonlítható (nem kizárt) számít
        outs = list((out.get("basis") or basis).get("out") or [])
        cheap, _other = self._comparables(want, [s for s in outs if s.get("outlier") == "olcsó"
                                                 and s.get("up") is not None])
        labels = self._cheap_label(cheap, out)
        if labels:
            out["basis"] = {**(out.get("basis") or basis), "out": [
                {**labels[listing_key(s["u"])], "why": labels[listing_key(s["u"])]["cheap_why"]}
                if s.get("u") and listing_key(s["u"]) in labels else s for s in outs]}
        return out

    @staticmethod
    def _comparables(want: dict[str, Any] | None, samples: list[dict[str, Any]]
                     ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Az összehasonlítók osztályozása a vizsgált hirdetéshez (specs.compare): a pontos és a közelítő (approx: miben
        más) beszámít; a kizárt (más változat, kapacitás, kiszerelés, plusz tartozék) és a bizonytalan (egy fontos adat
        hiányzik belőle) kimarad, az okkal. (beszámítottak, kizártak); want nélkül mind beszámít. Előtte a független árak
        (pricebook.independent): egy hirdetés egyszer, egy eladótól legfeljebb kettő, és a vizsgált hirdetés (want.own)
        máshol fent lévő példánya nem számít."""
        samples, out = independent(list(samples), (want or {}).get("own"))
        if not want or not want.get("category"):
            return samples, out
        kept = []
        for sample in samples:
            klass, why = specs.compare(want, specs.extract(str(sample.get("t") or "")))
            if klass in (specs.KIZART, specs.BIZONYTALAN):
                reason = "; ".join(why)
                out.append({**sample, "why": reason if klass == specs.KIZART else f"bizonytalan: {reason}",
                            "class": klass})
            elif klass == specs.KOZELITO:
                kept.append({**sample, "approx": "; ".join(why)})
            else:
                kept.append(sample)
        return kept, out

    @staticmethod
    def _want(row: dict[str, Any]) -> dict[str, Any]:
        """A vizsgált hirdetés termékadatai (a címéből, és ha megvan, az adatlapja leírásából); own: a címe, az ára és
        az eladója (a máshova is feltett példánya nem a saját összehasonlítója, _comparables)."""
        title = str(row.get("title") or row.get("text") or "")
        spec = specs.extract(title, str(row.get("details") or ""))
        if row.get("price"):
            spec["own"] = {"t": title, "p": int(row["price"]), "v": str(row.get("seller") or "")}
        return spec

    def _vet_reference(self, reference: dict[str, Any],
                       read: Callable[[dict[str, Any]], dict[str, Any] | None]) -> dict[str, Any] | None:
        """Az árbázis szokásos árának összehasonlítói közül néhánynak elolvassa a leírását (_vet), és újraszámolja
        (_screened)."""
        self._vet(list((reference.get("basis") or {}).get("used") or []), read, VET_READS, int(reference["price"]))
        return self._screened(reference)

    # --- előmelegítés: a gyakran felbukkanó termékek árai ráérő időben (warm) ---

    def _note_demand(self, key: str | None, url: str) -> None:
        """Egy friss hirdetés terméke (az előmelegítéshez: mennyire keresett, felező súllyal). Egy hirdetés egyszer
        számít (a címéből és a modell megnevezéséből is ide jöhet)."""
        if not key:
            return
        now, ident = self.clock(), listing_key(url)
        with self._lock:
            if ident in self._demand_rows:
                return
            self._demand_rows[ident] = None
            while len(self._demand_rows) > MAX_DEMAND * 8:
                self._demand_rows.pop(next(iter(self._demand_rows)))
            entry = self._demand.setdefault(key, [0.0, now, 0.0])
            entry[0] = entry[0] * 0.5 ** (max(0.0, now - entry[1]) / WARM_HALF) + 1.0
            entry[1] = now
            if len(self._demand) > MAX_DEMAND:     # a legrégebben látottak kiesnek
                for old in sorted(self._demand, key=lambda k: self._demand[k][1])[:len(self._demand) - MAX_DEMAND]:
                    self._demand.pop(old, None)

    def _next_warm(self, can_search: bool, readable: Callable[[str], bool] | None = None
                   ) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]] | None:
        """A következő előmelegítendő termék (a legkeresettebb, legalább WARM_SCORE, amit WARM_RETRY óta nem vett
        elő): (kulcs, a mostani ára (_screened; None, ha még nincs elég ára), és az összehasonlítói közül a még el nem
        olvasott, most olvasható leírásúak (readable: az oldaluk szerint)). A kevés árút csak akkor adja, ha most
        kereshet a többi oldalon (can_search; ekkor üres a lista); a már biztos árút, és aminek minden összehasonlítóját
        elolvasta, WARM_RETRY-ra félreteszi; amelyiknek most nincs olvasható összehasonlítója, átugorja."""
        now = self.clock()
        with self._lock:
            scored = sorted(((entry[0] * 0.5 ** (max(0.0, now - entry[1]) / WARM_HALF), key)
                             for key, entry in self._demand.items() if now - entry[2] >= WARM_RETRY), reverse=True)
        for score, key in scored[:WARM_LOOK]:
            if score < WARM_SCORE:
                break
            reference = self._screened(self.book.reference(key))
            if reference is None or not reference["enough"]:
                if can_search:
                    return key, reference, []
                continue
            with self._lock:
                unread = [] if reference["sure"] else [
                    sample for sample in (reference.get("basis") or {}).get("used") or []
                    if sample.get("u") and not sample.get("gone") and self._unvetted(listing_key(sample["u"]))
                    and listing_key(sample["u"]) not in self._warm_skip]
                if not unread:                 # biztos az ára (vagy nincs több elolvasható): WARM_RETRY múlva újra
                    if key in self._demand:
                        self._demand[key][2] = now
                    continue
            samples = [sample for sample in unread if readable is None or readable(str(sample.get("s") or ""))]
            if samples:
                return key, reference, samples
        return None

    def warm(self, read: Callable[[dict[str, Any]], dict[str, Any] | None],
             search: Callable[[str], list[dict[str, Any]]] | None = None,
             readable: Callable[[str], bool] | None = None) -> str:
        """Előmelegítés, egy lépés (ráérő időben, az azonnali figyelő hívja): a legkeresettebb, még nem biztos árú termék
        egy összehasonlítójának leírását elolvassa (read: a hirdetés oldala; readable: melyik oldalét olvashatja most;
        Deferred-del jelezheti, hogy most ne), vagy ha kevés az ára, és most kereshet (search), megnézi, mennyiért adják a
        többi oldalon (a találatok az árbázisba kerülnek). Ha utána még nem biztos az ára, WARM_AGAIN múlva folytatja.
        Visszaadja, mit csinált („” = semmit)."""
        picked = self._next_warm(search is not None, readable)
        if picked is None:
            return ""
        key, reference, samples = picked
        now = self.clock()

        def stamp(at: float) -> None:
            with self._lock:
                if key in self._demand:
                    self._demand[key][2] = at

        tried: list[str] = []

        def reading(row: dict[str, Any]) -> dict[str, Any] | None:
            page = read(row)                   # (a Deferred továbbmegy: az a hirdetés nem számít kipróbáltnak)
            tried.append(listing_key(str(row.get("url") or "")))
            return page

        with self._lock:
            before = self._demand[key][2] if key in self._demand else 0.0
        stamp(now)
        name = self.book.name(key) or key
        try:
            if reference is None or not reference["enough"]:
                # a keresőszó egy hirdetéscímből, ahogy írták („Sony WH-1000XM4”: a kulcs alakját nem találják az oldalak)
                titles = [str(sample["t"]) for sample in self.book.samples(key) if sample.get("t")]
                rows = search(self._probe_text(titles[-1] if titles else name)) if search is not None else []
                noted = 0
            else:
                noted = self._vet(samples, reading, 1, int(reference["price"]))
        except Exception:
            stamp(before)                      # most nem lehetett (keret, Szünet, Leállítás): a következő alkalommal
            raise
        with self._lock:                       # ami nem nyílt meg (eltűnt, robot-ellenőrzés), azt többé nem próbálja
            if len(self._warm_skip) > MAX_DEMAND * 4:
                self._warm_skip.clear()
            self._warm_skip.update(ident for ident in tried if ident and self._unvetted(ident))
        if reference is None or not reference["enough"]:
            self.book.add(rows)
            self.tally("warm_searches", amount=1)
            done = f"árkeresés: {name}"
        elif noted:
            self.tally("warm_reads", amount=noted)
            done = f"leírás: {name}"
        else:
            stamp(before)                      # most nem volt kérhető, vagy nem nyílt meg: legközelebb a következővel
            return ""
        again = self._screened(self.book.reference(key))
        if again is not None and not again["sure"]:   # még nem biztos: hamarosan a következő leírással folytatja
            stamp(now - WARM_RETRY + WARM_AGAIN)
        return done

    def _basis(self, group: list[dict[str, Any]], out: list[dict[str, Any]], note: str = "") -> dict[str, Any]:
        """Mihez mérte (a találat ablakához): a beszámított árak (a kilógók nélkül, olcsóbbtól), és a kizártak az
        okkal (a kilógó árak is: azok kimaradtak a szokásos árból, de maguk jelöltek lehetnek), legfeljebb BASIS_MAX."""
        stats = price_stats([sample["p"] for sample in group])
        kept = [sample for sample in group if stats and stats["min"] <= sample["p"] <= stats["max"]]
        outliers = [{**sample, "why": sample.get("cheap_why") or "kilógó ár (kimaradt a szokásos árból)"}
                    for sample in group if sample not in kept]

        def clean(sample: dict[str, Any]) -> dict[str, Any]:
            site = sample.get("s") or ""
            entry = {"t": sample.get("t") or "", "p": int(sample["p"]), "s": self.platforms[site].name
                     if site in self.platforms else site, "u": sample.get("u") or "", "src": sample.get("src") or ""}
            entry.update({k: sample[k] for k in ("why", "gone", "read", "approx", "cheap", "v", "seen") if sample.get(k)})
            return entry

        basis = {"used": [clean(s) for s in sorted(kept, key=lambda s: s["p"])][:BASIS_MAX],
                 "out": [clean(s) for s in sorted(outliers + out, key=lambda s: s["p"])][:BASIS_MAX],
                 "n_used": len(kept), "n_out": len(outliers) + len(out)}
        if note:
            basis["note"] = note
        return basis

    def _probe_text(self, query: str) -> str:
        """Keresőszó a többi oldalra egy termékhez: a nevének azok a szavai, amelyekből a termékkulcs lett, ahogy írták
        (a márka, a kártyagyártó típusneve nélkül: „MSI GeForce GTX 1080 OC 8GB” → „GTX 1080”), a kapacitás nélkül (a
        kapacitás nélküli címek is kellenek; a változatokat a _market_of választja szét)."""
        found = self.book.key(query)
        words = found[0].split() if found else []
        if words and all(re.match(r"^(?:lp)?ddr[2-5]$|^(?:\dx)?\d+[gt]b$|^(?:sodimm|ecc|reg|kit)$", word) for word in words):
            # memóriamodul: a mérete maga a termék, a gyártója, sebessége nem (a kulcsa sem): „16GB DDR4”, „2x8GB DDR4”
            # (a SO-DIMM és a szerver-memória a keresésben nem kötelező szó: a különbséget a kulcs választja szét)
            return " ".join(word.upper() for word in sorted(words, key=lambda word: not word[0].isdigit())
                            if not word.isalpha())
        text = self.book.query(query) or query
        text = re.sub(r"\b\d+\s?(?:gb|tb)\b", " ", text, flags=re.IGNORECASE)
        return " ".join(text.split()) or query

    def _unsure(self, row: dict[str, Any], query: str, found: dict[str, Any] | None,
                watch: dict[str, Any] | None = None) -> None:
        """Olcsónak látszott, de a szokásos ára nem elég biztos (kevés hasonló, egy oldalról): nem jelölt, de nem is
        vész el: a „Érdemes megnézni” tippek közé kerül (_tip, egy csendes értesítéssel), és a naplóba is beírja
        (látottnak jegyzi; a tippet a körök újra felmérik, _step_tips)."""
        self._mark(row)
        self.tally("unsure", [row])
        basis = (found["unsure_why"] if found and found.get("unsure_why") else
                 found["stale"] if found and found.get("stale") else
                 f"csak {found['n']} hasonló árat találtam ({', '.join(self._site_names(found['sites'])) or '?'})"
                 if found else "nem találtam hozzá elég hasonlót")
        self.journal.add(f"Nem biztos, hogy megéri: „{(row.get('title') or row['text'])[:60]}” "
                         f"{format_price(row['price'])} "
                         f"({row.get('site') or row.get('platform') or '?'}; „{query}”) – {basis}; az Érdemes megnézni "
                         "listába tettem.")
        if found is not None:
            self._tip(row, query, found, watch)

    def _tip(self, row: dict[str, Any], query: str, found: dict[str, Any], watch: dict[str, Any] | None) -> None:
        """Egy „Érdemes megnézni” tipp (bizonytalan ár): a hirdetés, a néhány hasonló ára és a várható haszon. Egy
        hirdetésről egyszer; értesítés (a telefonon egy rövid pittyenés: notice.tip) naponta legfeljebb TIPS_PER_DAY
        (utána csak a listában)."""
        usual = int(found["price"])
        low = careful_low(found)
        product = self.book.key(query)
        # ha a címéből nem derül ki, laptop- vagy asztali memória-e: a laptop-memóriák (az olcsóbbak) ára a mérce, ha
        # ismert; különben a figyelmeztetés a tippben (a leírása után úgyis újraméri: _respec)
        caution = ""
        spec = specs.extract(str(row.get("title") or row["text"]))
        if specs.needs_detail(spec) and spec.get("category") == "ram" and product:
            laptop = self.book.reference(specs.keyed(product[0], {**spec, "attrs": {
                **spec.get("attrs", {}), "form": {"value": parts.SODIMM}}}), exclude=row["url"])
            if laptop is not None and int(laptop["price"]) < usual:
                usual, low = int(laptop["price"]), careful_low(laptop)
                caution = (" A címéből nem derül ki, laptop- vagy asztali memória-e: a laptop-memóriák (az olcsóbbak) "
                           "árához mértem.")
            elif laptop is None:
                caution = (" A címéből nem derül ki, laptop- vagy asztali memória-e: a laptop-memória olcsóbb lehet, "
                           "nézd meg a fotóján.")
        profit = expected_profit(usual, int(row["price"]), low)
        if profit <= 0 or not row.get("url") or wearable(f"{row.get('title') or row['text']} {query}"):
            return
        now = self.clock()
        site = row.get("site") or (self.platforms[row["platform"]].name if row.get("platform") in self.platforms
                                   else row.get("platform") or "")
        tip = {"id": secrets.token_hex(5), "url": row["url"], "title": str(row.get("title") or row["text"])[:120],
               "price": int(row["price"]), "site": site, "platform": str(row.get("platform") or ""), "query": query,
               "key": product[0] if product else "", "usual": usual, "n": int(found.get("n") or 0),
               "sites": self._site_names(found.get("sites")), "profit": profit, "at": now,
               "watch": str((watch or {}).get("id") or ""),
               "min_profit": int((watch or {}).get("min_profit") or WATCH_PROFIT)}
        sites = ", ".join(tip["sites"]) or "?"
        text = (f"{site}: „{tip['title']}” {format_price(tip['price'])}. Bizonytalan ár: csak {tip['n']} hasonló "
                f"hirdetés ({sites}) szerint kb. {format_price(usual)}-ért adják, így kb. {format_price(profit)} haszon "
                f"lehet benne.{caution} Nézd meg magad, mielőtt megveszed; ha összegyűlik elég ár, újra felmérem.")
        self._put_tip(tip, f"Érdemes megnézni: {tip['title'][:50]}", text)

    def _put_tip(self, tip: dict[str, Any], title: str, text: str) -> bool:
        """Egy tipp a listára (egy hirdetésről egyszer; legfeljebb MAX_TIPS), és az értesítése (a telefonon egy rövid
        pittyenés: notice.tip; naponta legfeljebb TIPS_PER_DAY, utána csak a listában). Hamis, ha már volt ilyen."""
        ident, today = listing_key(tip["url"]), day_of(self.clock())
        with self._lock:
            tips = self._data["tips"]
            if any(listing_key(t["url"]) == ident for t in tips):
                return False
            tips.insert(0, tip)
            del tips[MAX_TIPS:]
            told = self._data["tips_told"]
            if told["day"] != today:
                told.update(day=today, n=0)
            told["n"] += 1
            quiet = told["n"] > TIPS_PER_DAY
            self._save()
            self._tip_notices.append((("deal", title, text), {"url": tip["url"], "actions": [DEALS_ACTION],
                                                              "quiet": quiet or None, "tip": tip["id"]}))
        return True

    # --- alkuval megérheti (friss hirdetés, kicsit drága) ---

    def _haggle_reach(self, price: int, top: int | None) -> int | None:
        """Ha a kért ár legfeljebb FRESH_HAGGLE_CUT-tal több annál, ami még megéri (top: a vevő díja nélkül), a javasolt
        ajánlat (kerek százasra lefelé, a vételi limiten belül); különben None."""
        if not top or top <= 0 or price <= top:
            return None
        offer = self._capped(top) // 100 * 100
        return offer if 0 < offer and offer >= price * (1 - FRESH_HAGGLE_CUT) and self._affordable(offer) else None

    def _haggle_tip(self, row: dict[str, Any], query: str, usual: int, low: int | None, top: int | None,
                    watch: dict[str, Any] | None, n: int = 0, sites: list[str] | None = None) -> bool:
        """Alkuval megérheti: egy friss, kicsit drága hirdetés (_haggle_reach) „Érdemes megnézni” tippként, a javasolt
        ajánlattal és az akkori haszonnal. A Vinteden nem (ott ajánlatot tehetsz, rendes jelöltként), a licitnél sem; ha
        kikapcsoltad az alkuképes hirdetéseket, ezt sem."""
        platform, price = str(row.get("platform") or ""), int(row["price"])
        if row.get("auction") or platform == "vinted" or not row.get("url") or price < usual * FLOOR:
            return False
        with self._lock:
            if not self._data["haggle"]["on"]:
                return False
        offer = self._haggle_reach(price, top)
        if offer is None or wearable(f"{row.get('title') or row['text']} {query}"):
            return False
        profit = expected_profit(usual, offer, low) - buy_fee(platform, offer)
        if profit <= 0:
            return False
        cut = round(1 - offer / price, 2)
        product = self.book.key(query)
        site = row.get("site") or (self.platforms[platform].name if platform in self.platforms else platform)
        tip = {"id": secrets.token_hex(5), "url": row["url"], "title": str(row.get("title") or row["text"])[:120],
               "price": price, "site": site, "platform": platform, "query": query,
               "key": product[0] if product else "", "usual": usual, "n": int(n or 0),
               "sites": self._site_names(sites), "profit": profit, "at": self.clock(),
               "watch": str((watch or {}).get("id") or ""), "haggle": {"offer": offer, "cut": cut},
               "why": f"alkuval: ha kb. {format_price(offer)}-ért megkapod"}
        title = tip["title"]
        text = (f"{site}: „{title}” {format_price(price)}. Most kicsit drága, de ha kb. {format_price(offer)}-ért "
                f"({round(cut * 100)}%-kal olcsóbban) megkapod, kb. {format_price(profit)} haszon lehet benne (a hasonlók "
                f"kb. {format_price(usual)}-ért mennek). Sok eladó enged: nézd meg, és írj neki.")
        if not self._put_tip(tip, f"Alkuval megérheti: {title[:45]}", text):
            return False
        self.tally("haggle_tips", [row])
        self.journal.add(f"Alkuval megérheti: „{title[:60]}” {format_price(price)} ({site}): ajánlj kb. "
                         f"{format_price(offer)}-ot (kb. {format_price(profit)} haszon); az Érdemes megnézni listába "
                         "tettem.")
        return True

    def _haggle_explore(self, watch: dict[str, Any], row: dict[str, Any], reference: dict[str, Any]) -> bool:
        """Egy figyelt oldal (vagy az árbázis) szerint nem elég olcsó hirdetés: alkuval megérheti-e (_haggle_tip)."""
        if not reference.get("enough", True):
            return False
        usual, low = int(reference["price"]), careful_low(reference)
        top = before_fee(str(row.get("platform") or ""),
                         explore_top(usual, int(watch.get("min_profit") or WATCH_PROFIT), low))
        return self._haggle_tip(row, str(reference.get("name") or watch.get("query") or row["text"][:60]), usual, low,
                                top, watch, reference.get("n") or 0, reference.get("sites"))

    def _haggle_value(self, row: dict[str, Any], value: dict[str, Any]) -> bool:
        """Az azonnali figyelő egy nem elég olcsó friss hirdetése (quick_value, _valued: a megérő legmagasabb ár, top):
        alkuval megérheti-e. Csak ha elég hasonló ár van (a leírások olvasása nélkül is: ez neked szóló tipp)."""
        if not (value.get("enough") or value.get("sure")):
            return False
        top = before_fee(str(row.get("platform") or ""), int(value.get("top") or 0)) if not value.get("search") \
            else value.get("top")
        return self._haggle_tip(row, str(value.get("name") or row["text"][:60]), int(value["reference"]),
                                value.get("low"), top, {"id": value.get("search") or "azonnali"},
                                value.get("n") or 0, value.get("sites"))

    def _haggle_search(self, search: dict[str, Any], row: dict[str, Any], reference: int) -> bool:
        """Egy keresésed szerint nem elég olcsó hirdetés: alkuval megérheti-e (a keresés szabályai szerint)."""
        top = highest_ok({**search, "min_price": None, "max_price": None}, reference, str(row.get("platform") or ""))
        return self._haggle_tip(row, str(search["query"]), reference, None, top, {"id": search["id"]})

    def _follow_auction(self, game: _Round, search: dict[str, Any], deal: dict[str, Any]) -> None:
        """Egy megérő, de még nem hamarosan záruló licit a licitlistára, előbb ellenőrizetlenként: a szokásos ára szerint
        megérheti, de az adatlapját még nem nézte meg, ezért konkrét licithatárt még nem javasol, és nem is szól róla.
        Rögtön megnyitja az adatlapját (_verify_auction; ha a kör keretébe már nem fér, egy későbbi kör): ha rendben van,
        ellenőrzött lesz, és akkor csendesen szól a javasolt legmagasabb ajánlattal. A zárás előtt a kör még egyszer
        megnézi (_final_auction)."""
        now, key = self.clock(), listing_key(deal["url"])
        entry = {"id": deal["id"], "url": deal["url"], "site": deal.get("site") or "", "platform": deal.get("platform"),
                 "title": str(deal.get("title") or deal["text"])[:120], "text": deal["text"], "price": int(deal["price"]),
                 "max_bid": int(deal["max_bid"]), "median": int(deal["median"]), "low": deal.get("low"),
                 "ends": now + int(deal.get("left") or 0), "hours": int(deal.get("hours") or WATCH_HOURS),
                 "search": search["id"], "query": search["query"], "explore": deal.get("explore") or "",
                 "similar": deal.get("similar"), "sites": deal.get("sites") or [], "at": now, "checked": False,
                 "tried": 0.0}
        with self._lock:
            known = next((a for a in self._data["auctions"] if listing_key(a["url"]) == key), None)
            if known is not None:
                entry.update(id=known["id"], at=known["at"], checked=bool(known.get("checked")),
                             tried=float(known.get("tried") or 0))
            self._data["auctions"] = sorted([a for a in self._data["auctions"] if listing_key(a["url"]) != key] + [entry],
                                            key=lambda a: float(a["ends"]))[:MAX_AUCTIONS]
            self._save()
        self._mark(deal)                       # a kör nem jelöltként nézi újra: a licitlista viszi tovább
        self.tally("auction", [deal])
        if known is None:
            self.journal.add(f"Licitlistára tettem, még ellenőrizetlenül: „{entry['title'][:60]}” most "
                             f"{format_price(entry['price'])}, {left_text(deal.get('left'))} múlva zárul ({entry['site']}); "
                             "az adatlapja után mondok licithatárt.")
        if not entry["checked"]:
            game.next([partial(self._verify_auction, game, key)])

    @staticmethod
    def _auction_text(entry: dict[str, Any], left: int | None) -> str:
        profit = expected_profit(entry["median"], entry["max_bid"], entry.get("low"))
        return (f"Vatera-licit: „{entry['title'][:80]}” most {format_price(entry['price'])}, {left_text(left)} múlva "
                f"zárul. Az adatlapját megnéztem, rendben van. Máshol kb. {format_price(entry['median'])}-ért adják; "
                f"legfeljebb {format_price(entry['max_bid'])}-ig érdemes licitálni (akkor is kb. {format_price(profit)} a "
                "haszon). A Vaterán ezt most legmagasabb ajánlatként beállíthatod (addig licitál helyetted a Vatera); a "
                "zárás előtt még szólok. Licitálni nem fogok helyetted.")

    def _auction(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            entry = next((a for a in self._data["auctions"] if listing_key(a["url"]) == key), None)
            return dict(entry) if entry is not None else None

    def _auction_update(self, key: str, **fields: Any) -> dict[str, Any] | None:
        with self._lock:
            entry = next((a for a in self._data["auctions"] if listing_key(a["url"]) == key), None)
            if entry is None:
                return None
            entry.update(fields)
            self._save()
            return dict(entry)

    def _auction_top(self, entry: dict[str, Any]) -> int:
        """A licithatár most, a mostani beállításokkal: ha egy keresésedből jött (és az még megvan), annak a szabálya
        (szokásos ár, %, haszon, ársáv), különben a figyelt oldal (vagy a böngészés) legkisebb haszna szerint; és
        legfeljebb a mostani vételi limit (a havi keret maradéka). 0: már nem éri meg."""
        with self._lock:
            search = next((s for s in self._data["searches"] if s.get("id") == entry.get("search")), None)
            watch = next((w for w in self._data["watches"] if w.get("id") == entry.get("search")), None)
            browse = int(self._data["browse"].get("min_profit") or WATCH_PROFIT)
        if search is not None and search.get("reference"):
            top = highest_ok(search, int(search["reference"])) or 0
        else:
            top = explore_top(int(entry["median"]), int((watch or {}).get("min_profit") or browse), entry.get("low"))
        return max(0, self._capped(top)) if top > 0 else 0

    def _auction_deal(self, entry: dict[str, Any], now: float) -> dict[str, Any]:
        """A licitlista egy eleméből jelölt (az ellenőrzéshez és a szóláshoz)."""
        deal = {k: v for k, v in entry.items()
                if k not in ("ends", "hours", "query", "search", "at", "checked", "tried") and v is not None}
        deal.update(id=secrets.token_hex(5), auction=True, left=int(float(entry["ends"]) - now))
        return deal

    def _look_auction(self, game: _Round, entry: dict[str, Any],
                      deal: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
        """A licit adatlapja a kör oldalkeretéből (_inspect: elkelt, hibás, gyanús?). (verdict, oldal); verdict None, ha
        most nem nézhető meg (elfogyott a keret, robot-ellenőrzés, nem nyílt meg): a licit a listán marad, és egy
        későbbi kör próbálja (elavult adatokkal nem szól)."""
        if game.blocked or game.pages <= 0:
            return None, None
        game.pages -= 1
        game.loaded += 1
        try:
            page = self._look(entry["url"], str(entry.get("platform") or ""), game.check, game)
        except BrowserError:
            return None, None
        verdict = self._inspect(deal, page, game)
        if verdict == "captcha":
            game.blocked = True
            return None, None
        return verdict, page

    def _auction_rejected(self, entry: dict[str, Any], deal: dict[str, Any], verdict: str,
                          page: dict[str, Any] | None) -> bool:
        """Az adatlap után: kiesik-e a licit (elkelt, hibás, gyanús, vagy a mostani ára már eléri a mostani beállításokkal
        újraszámolt licithatárt); ha igen, lekerül a listáról, és a naplóba írja, miért. Ha nem, a jelöltbe a mostani ár
        (az oldalról, ha leolvasható; különben a legutóbb látott, figyelmeztetéssel) és az új licithatár kerül."""
        key, title = listing_key(entry["url"]), entry["title"][:60]
        if verdict == "gyanús":
            self._drop_auction(key)
            self._suspicious(deal)
            return True
        if verdict:                            # elkelt, eltűnt, hibás
            self._drop_auction(key)
            self._mark(deal)
            self.journal.add(f"Lekerült a licitlistáról: „{title}” "
                             + ("(elkelt vagy eltűnt)." if verdict == "elkelt" else "(az adatlapja szerint hibás)."))
            return True
        now_price = auction_price(page or {})
        price, top = now_price or int(entry["price"]), self._auction_top(entry)
        if top <= 0 or price >= top:
            self._drop_auction(key)
            self.journal.add(f"Lekerült a licitlistáról: „{title}” most {format_price(price)}, a mostani beállításokkal "
                             + (f"legfeljebb {format_price(top)}-ig érné meg." if top > 0 else "már nem éri meg."))
            return True
        deal.update(price=price, max_bid=top,
                    profit=expected_profit(int(entry["median"]), top, entry.get("low")))
        if now_price is None:
            deal["followed"] = True            # a szövegben: „legutóbb” (az oldalról nem tudtam leolvasni)
            deal.setdefault("warnings", []).append("a mostani licitet nem tudtam leolvasni az oldalról: nézd meg")
        return False

    def _verify_auction(self, game: _Round, key: str) -> None:
        """Egy ellenőrizetlen licit adatlapja (_look_auction), és ha van modell, egy rövid modellkérdés (tényleg ez,
        működik?). Ha rendben van, ellenőrzött lesz, a mostani árával és a mostani beállításokkal számolt licithatárral,
        és csendesen szól a javasolt legmagasabb ajánlattal (naponta legfeljebb AUCTION_TIPS_PER_DAY); ha elkelt,
        hibás, gyanús, vagy a mostani ára már eléri a licithatárt, lekerül. Ha most nem nézhető meg, ellenőrizetlenként a
        listán marad (BUSY_RETRY múlva újra próbálja)."""
        now = self.clock()
        entry = self._auction_update(key, tried=now)
        if entry is None or entry.get("checked"):
            return
        deal = self._auction_deal(entry, now)
        verdict, page = self._look_auction(game, entry, deal)
        if verdict is None or self._auction_rejected(entry, deal, verdict, page):
            return
        if game.lane is None:                  # modell nélkül az adatlap dönt
            self._auction_checked(game, key, deal, None)
            return
        if game.checks <= 0:
            return                             # a modellkérdés keretébe már nem fér: később (ellenőrizetlen marad)
        game.checks -= 1
        game.ask(partial(self._confirm, {"id": entry["search"], "query": entry["query"]}, deal),
                 partial(self._auction_checked, game, key, deal))

    def _auction_checked(self, game: _Round, key: str, deal: dict[str, Any], ok: bool | None) -> None:
        """A modell válasza után: ha nemet mondott, a licit lekerül; különben ellenőrzött lesz (a mostani ár, a
        licithatár), és ha még nem szólt róla, csendesen szól (naponta legfeljebb AUCTION_TIPS_PER_DAY)."""
        if ok is False:
            self._drop_auction(key)
            if deal.get("suspect"):
                self._suspicious(deal)
            else:
                self._mark(deal)
                self.journal.add(f"Lekerült a licitlistáról: „{str(deal.get('title') or deal['text'])[:60]}” (a modell "
                                 "szerint nem az, vagy nem működik).")
            return
        today = day_of(self.clock())
        with self._lock:
            entry = next((a for a in self._data["auctions"] if listing_key(a["url"]) == key), None)
            if entry is None:
                return
            entry.update(checked=True, price=int(deal["price"]), max_bid=int(deal["max_bid"]))
            told = self._data["auction_told"]
            if told.get("day") != today:
                told.update(day=today, n=0)
            tell = not entry.get("told") and told["n"] < AUCTION_TIPS_PER_DAY
            told["n"] += tell
            entry["told"] = entry.get("told") or tell
            entry = dict(entry)
            self._save()
        self.journal.add(f"Ellenőriztem a licitlistán: „{entry['title'][:60]}” rendben van; legfeljebb "
                         f"{format_price(entry['max_bid'])}-ig érdemes licitálni.")
        if tell:
            game.notify("deal", f"Licit: {entry['title'][:50]} (legfeljebb {format_price(entry['max_bid'])})",
                        self._auction_text(entry, deal.get("left")), url=entry["url"], actions=[DEALS_ACTION],
                        quiet=True)

    def _auction_seen(self, rows: list[dict[str, Any]]) -> None:
        """A licitlistán lévő licit egy oldalon újra: a mostani ára és a hátralévő ideje frissül; ha a mostani ára már a
        licithatár fölött van (a mostani beállításokkal számolva), lekerül (már nem éri meg beszállni)."""
        if not rows:
            return
        now = self.clock()
        seen = {listing_key(row["url"]): row for row in rows}
        with self._lock:
            kept, changed = [], False
            for entry in self._data["auctions"]:
                row = seen.get(listing_key(entry["url"]))
                if row is not None:
                    changed = True
                    top = self._auction_top(entry)
                    if int(row["price"]) >= top:
                        self.journal.add(f"Lekerült a licitlistáról: „{entry['title'][:60]}” már "
                                         f"{format_price(int(row['price']))}, a licithatár ("
                                         f"{format_price(top)}) fölött.")
                        continue
                    entry["price"] = int(row["price"])
                    if row.get("left") is not None:
                        entry["ends"] = now + int(row["left"])
                kept.append(entry)
            if changed:
                self._data["auctions"] = kept
                self._save()

    def _auctions_due(self, now: float) -> bool:
        """Van-e dolga a licitlistának: egy licit zárás előtti ellenőrzése esedékes (vagy már lejárt: lekerül), vagy egy
        ellenőrizetlen licit adatlapját kell megnézni (legfeljebb BUSY_RETRY-onként próbálja)."""
        day = tuple(self._data["day"])
        return any(last_chance(float(a["ends"]), day) - now <= int(a.get("hours") or WATCH_HOURS) * 3600
                   or (not a.get("checked") and now - float(a.get("tried") or 0) >= BUSY_RETRY)
                   for a in self._data["auctions"])

    def _step_auctions(self, game: _Round) -> None:
        """A licitlista: ami lejárt, lekerül; aminek a zárása közel van (a figyelő órái szerint, a gép esti leállása
        előtt), azt még egyszer megnézi (_final_auction), és a még ellenőrizetlenek adatlapját is (_verify_auction). Ami
        a kör keretébe már nem fér bele, marad, és a következő kör próbálja."""
        now = self.clock()
        with self._lock:
            day = tuple(self._data["day"])
            expired = {listing_key(a["url"]) for a in self._data["auctions"] if float(a["ends"]) <= now}
            if expired:
                self._data["auctions"] = [a for a in self._data["auctions"] if listing_key(a["url"]) not in expired]
                self._save()
            due = [listing_key(a["url"]) for a in self._data["auctions"]
                   if last_chance(float(a["ends"]), day) - now <= int(a.get("hours") or WATCH_HOURS) * 3600]
            unchecked = [listing_key(a["url"]) for a in self._data["auctions"] if listing_key(a["url"]) not in due
                         and not a.get("checked") and now - float(a.get("tried") or 0) >= BUSY_RETRY]
        game.next([partial(self._final_auction, game, key) for key in due] +
                  [partial(self._verify_auction, game, key) for key in unchecked])

    def _final_auction(self, game: _Round, key: str) -> None:
        """A licit zárás előtti ellenőrzése: az adatlapja újra (elkelt, hibás, gyanús?), a mostani ára (az oldalról, ha
        leolvasható), és a licithatár újraszámolva a mostani beállításokkal (vételi limit, havi keret, a keresés vagy a
        figyelt oldal szabálya). Ha a mostani ár eléri a határt, vagy már nem éri meg, nem szól, és lekerül; különben egy
        rövid modellkérdés után sürgősen szól (_candidate), és lekerül. Ha most nem nézhető meg, a listán marad, és a
        következő kör próbálja: elavult adatokkal nem szól."""
        now = self.clock()
        entry = self._auction(key)
        if entry is None:
            return
        deal = self._auction_deal(entry, now)
        verdict, page = self._look_auction(game, entry, deal)
        if verdict is None or self._auction_rejected(entry, deal, verdict, page):
            return
        self._candidate(game, {"id": entry["search"], "query": entry["query"]}, deal, page,
                        done=partial(self._drop_auction, key))

    def _drop_auction(self, key: str) -> None:
        """Egy licit lekerül a licitlistáról (szólt róla a zárás előtt, vagy elkelt, hibás, már nem éri meg…)."""
        with self._lock:
            self._data["auctions"] = [a for a in self._data["auctions"] if listing_key(a["url"]) != key]
            self._save()

    def dismiss_auction(self, auction_id: str) -> dict[str, Any]:
        """Egy licit levétele a licitlistáról (nem érdekel): a zárás előtt sem szól róla."""
        with self._lock:
            self._data["auctions"] = [a for a in self._data["auctions"] if a.get("id") != auction_id]
            self._save()
        return self.state()

    def flush_tips(self, notify: Callable[..., None]) -> None:
        """A „Érdemes megnézni” tippek értesítései (_tip gyűjti, a kör vagy az azonnali figyelő küldi ki)."""
        with self._lock:
            notices, self._tip_notices = self._tip_notices, []
        for args, extra in notices:
            notify(*args, **{k: v for k, v in extra.items() if v is not None})

    def dismiss_tip(self, tip_id: str) -> dict[str, Any]:
        """Egy tipp elrejtése (nem érdekes): lekerül a listáról, és többé nem méri fel újra."""
        with self._lock:
            self._data["tips"] = [t for t in self._data["tips"] if t.get("id") != tip_id]
            self._save()
        return self.state()

    def _step_tips(self, game: _Round) -> None:
        """A tippek újra felmérése az árbázisból: ha közben elég hasonló ár gyűlt össze, és az összehasonlítók leírása is
        rendben (_screened; ha csak ez hiányzik, néhányat elolvas a kör keretéből), és még mindig megéri, rendes jelölt
        lesz belőle (_candidate). Az ellenőrizetlen tipp (_unverified) TIP_RETRY múlva jön újra; az állapota miatt
        bizonytalan csak akkor, ha azóta megadtál pótlási vagy javítási költséget, az alig leírt soha (azt nézd meg te).
        A tipp csak akkor kerül le, ha az ellenőrzése lezárult (találat, vagy kiderült, hogy elkelt, hibás, gyanús, nem
        éri meg): ha a körben elfogyott a keret, vagy az ellenőrzés félbemaradt, a listán marad, és a következő kör újra
        megpróbálja. A TIP_DAYS napnál régebbiek és a már megtalált hirdetések lekerülnek. A gyors sor (böngésző
        nélkül) csak a gyors oldalakét nézi; amit egy másik sor épp ellenőriz, azt kihagyja."""
        now = self.clock()
        with self._lock:
            tips, known, busy = list(self._data["tips"]), self._deal_keys(), set(self._tips_busy)
            costs_at = float(self._data["costs_at"])
        promote, drop = [], set()
        for tip in tips:
            if now - float(tip.get("at") or 0) > TIP_DAYS * 86400 or listing_key(tip["url"]) in known \
                    or wearable(f"{tip.get('title') or ''} {tip.get('query') or ''}"):
                drop.add(tip["id"])
                continue
            if tip["id"] in busy or (game.no_browser and not (self.fetcher is not None
                                                              and self.fetcher.usable(tip.get("platform") or ""))):
                continue
            if tip.get("auction") or tip.get("haggle"):
                continue                       # egy ellenőrizetlen licit (a licitlista követi), az alku: csak a listában
            if tip.get("hold") and (tip["hold"] != "állapot" or float(tip.get("checked") or 0) >= costs_at):
                continue                       # az állapota miatt bizonytalan: csak ha azóta megadtál költséget
            if not tip.get("hold") and now - float(tip.get("checked") or 0) < TIP_RETRY:
                continue                       # nemrég nézte meg (nem nyílt meg, a modell nem válaszolt): később
            reference = self._screened(self.book.reference(tip["key"], exclude=tip["url"]), self._want(tip)) \
                if tip.get("key") else None
            if reference is not None and reference["enough"] and not reference["sure"] and game.pages > 0:
                # elég ár van, de az összehasonlítók leírását még nem olvastuk el: néhányat most (a kör keretéből)
                reference = self._vet_reference(reference, lambda other: self._read_page(other, game))
            if reference is None or not reference["sure"]:
                continue
            row = {"site": tip["site"], "platform": tip["platform"], "text": tip["title"], "title": tip["title"],
                   "price": int(tip["price"]), "url": tip["url"]}
            watch = {"id": tip.get("watch") or "tipp", "name": "Érdemes megnézni (most már biztos az ára)",
                     "min_profit": int(tip.get("min_profit") or WATCH_PROFIT)}
            deal = self._explore_deal(watch, row, {**reference, "name": tip["query"]})
            if deal is None:
                drop.add(tip["id"])              # biztos lett az ára, és már nem éri meg: lezárva (letöltés nélkül)
            else:
                promote.append((tip["id"], watch, tip["query"], deal))
        with self._lock:
            if drop:
                self._data["tips"] = [t for t in self._data["tips"] if t.get("id") not in drop]
                self._save()
            # a foglalás egy lépésben a mostani állapottal: amit közben a másik sor lefoglalt (vagy lezárt), azt kihagyja,
            # így egy tippből nem lehet két jelölt (két találat, két értesítés)
            listed = {t.get("id") for t in self._data["tips"]}
            promote = [entry for entry in promote if entry[0] in listed and entry[0] not in self._tips_busy]
            self._tips_busy.update(tip_id for tip_id, _w, _q, _d in promote)
        game.tips += [tip_id for tip_id, _w, _q, _d in promote]
        game.next([partial(self._candidate, game, {"id": watch["id"], "query": query}, deal,
                           done=partial(self._drop_tip, tip_id)) for tip_id, watch, query, deal in promote])

    def _drop_tip(self, tip_id: str) -> None:
        """A tipp ellenőrzése lezárult (_candidate): lekerül a listáról."""
        with self._lock:
            self._data["tips"] = [t for t in self._data["tips"] if t.get("id") != tip_id]
            self._tips_busy.discard(tip_id)
            self._save()

    def _release_tips(self, game: _Round) -> None:
        """A kör végén: amit ez a kör ellenőrzött, de nem zárult le (keret, félbemaradt), azt a következő kör (akár a
        másik sor) újra megpróbálhatja."""
        with self._lock:
            self._tips_busy.difference_update(game.tips)

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
            # a modell nem találhat ki méretet: ami a hirdetésben nincs (pl. „RX 580” → „RX 580 8GB”), az kimarad
            query = specs.grounded(query, f"{batch[index].get('title') or ''} {batch[index]['text']}")
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
        self.tally("unknown", skip)            # a modell sem ismerte fel
        steps = []
        for row, query, buy in items:
            if wearable(query):                # a modell szerint ruha vagy cipő: azzal nem kereskedik
                skip.append(row)
                self.tally("clothing", [row])
                continue
            product = self.book.key(query)
            self._note_demand(product[0] if product else None, row["url"])
            reference = self._screened(self.book.reference(product[0], exclude=row["url"]), self._want(row)) \
                if product else None
            if reference is not None:
                deal = self._explore_deal(watch, row, {**reference, "name": query})
                if deal is not None and reference["sure"]:
                    steps.append(partial(self._candidate, game, {"id": watch["id"], "query": query}, deal))
                    continue
                if deal is None:
                    self._haggle_explore(watch, row, {**reference, "name": query})
                if deal is not None:           # olcsónak látszik, de az ára nem elég biztos: a többi oldalon is
                    buy, reference = True, None
            if buy and reference is None and game.resale > 0:
                game.resale -= 1
                steps.append(partial(self._resale, game, watch, row, query))
            elif not (buy and reference is None):
                skip.append(row)               # a megérő, de a keretbe már nem férő a következő körben újra jön
                self.tally("not_cheap", [row])
        self._mark(*skip)
        game.next(steps)

    def _resale(self, game: _Round, watch: dict[str, Any], row: dict[str, Any], query: str) -> None:
        """Egy hirdetés (vagy licit) viszonteladási ára biztosan: az árbázis árai (ha ismeri a terméket), és a
        keresőszó találatai azokon az oldalakon, ahol még nem láttuk (modell nélkül; a találatok árai az árbázisba is
        bekerülnek). Csak akkor jelölt, ha így elég hasonló ár van, több oldalról (sure_price), és a mostani ár jóval
        alatta van; különben nem szól róla (a naplóba beírja). Az árkutatás közben talált hirdetésekből is jelölt lehet
        (_peers: a többihez mérve, saját maga nélkül): ezek az eredeti hirdetéssel együtt, a várható haszon és az adatok
        megbízhatósága szerint kerülnek sorra (a jobb előbb); az eredeti vizsgálata is megmarad."""
        self._mark(row)
        probe = {"id": watch["id"], "query": self._probe_text(query)}
        product = self.book.key(query)
        # ahol az árbázisban már friss (FRESH_DAYS napon belül látott) ára van, ott nem keres újra; a régi ár nem elég
        now = self.clock()
        have = {s["site"] for s in self.book.samples(product[0], exclude=row["url"])
                if now - float(s.get("last") or 0) <= FRESH_DAYS * 86400} if product else set()
        sites = [k for k in RESALE_SITES if k in self.platforms and k not in have]
        if game.no_browser:
            sites = [k for k in sites if self.fetcher is not None and self.fetcher.usable(k)]
        own = [k for k in sites if k == row.get("platform")]
        rows: list[dict[str, Any]] = []
        # ha az árbázisban már elég ár van (csak az összehasonlítók leírását nem olvastuk még el), nem keres tovább
        found = self._market_of(row, query, []) if product else None
        for group in ([k for k in sites if k not in own], own):   # a hirdetés saját oldala csak, ha a többi kevés
            if not group or (found is not None and found["enough"]):
                continue
            rows += self._collect(probe, group, game.check, track=False, game=game)[0]
            found = self._market_of(row, query, rows)
        # a végső összevetés: ha egy hirdetés változata a címéből nem derül ki, a leírását is elolvassa (a kör keretéből)
        found = self._market_of(row, query, rows, read=lambda other: self._read_page(other, game), reads=DETAIL_READS)
        if found is None:
            self.tally("unknown", [row])
            return
        steps: list[tuple[float, Callable[[], None]]] = []
        deal = self._explore_deal(watch, row, {**found, "name": query})
        if deal is not None and found["sure"]:
            steps.append((deal["profit"], partial(self._candidate, game, probe, deal)))
        elif deal is not None:
            self._unsure(row, query, found, watch)
        else:
            self.tally("not_cheap", [row])
            self._haggle_explore(watch, row, {**found, "name": query})
        label = f"{watch['name']}: árkutatás közben („{str(row.get('title') or row['text'])[:40]}” mellett)"

        def judge(peer: dict[str, Any], reference: dict[str, Any]) -> tuple[float, Any] | None:
            other = self._explore_deal(watch, peer["row"], {**reference, "name": query}, explore=label, spawned=True)
            if other is None:
                return None
            if not reference["sure"]:
                return 0.0, ("tipp", peer["row"], reference)
            return other["profit"] * (1.0 if peer.get("exact") else 0.75), ("jelölt", other)

        for score, what in self._peers(row, found, judge):
            if what[0] == "jelölt":
                steps.append((score, partial(self._candidate, game, probe, what[1])))
            else:
                self._unsure(what[1], query, what[2], watch)
        game.next([step for _score, step in sorted(steps, key=lambda entry: -entry[0])])

    def _read_page(self, row: dict[str, Any], game: _Round) -> dict[str, Any] | None:
        """Egy összehasonlító hirdetés oldala (a változata miatt, _clarify), a kör oldalkeretéből; None, ha elfogyott, ha
        robot-ellenőrzést kér, vagy nem sikerült."""
        if game.pages <= 0 or game.blocked:
            return None
        game.pages -= 1
        game.loaded += 1
        try:
            page = self._look(row["url"], str(row.get("platform") or ""), game.check, game)
        except BrowserError:
            return None
        if page.get("captcha"):
            game.blocked = True
            return None
        return page

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

    # --- alkuképes hirdetések: a régóta fent lévő, kicsit drága hirdetésekre ajánlat ---

    def _haggle_pending(self, now: float) -> bool:
        """Van-e megnézendő alkuképes hirdetés: ha be van kapcsolva, és a gép üzemideje van, HAGGLE_EVERY percenként
        átnézi az árbázist (haggle_candidates, modell és letöltés nélkül), és a legígéretesebb HAGGLE_CHECKS a következő
        körre vár (_step_haggle). Ha nincs ilyen, nem jelez munkát."""
        with self._lock:
            haggle = self._data["haggle"]
            if not (self._data["enabled"] and haggle["on"] and self._day_ok(now)):
                return False
            if self._haggle_queue:
                return True
            if now - haggle["scanned"] < HAGGLE_EVERY * 60 - 90:
                return False
            haggle["scanned"] = now
            self._save()
        queue = self.haggle_candidates(now)[:HAGGLE_CHECKS]
        with self._lock:
            self._haggle_queue = queue
        return bool(queue)

    def _haggle_top(self, name: str, reference: dict[str, Any], floor: int,
                    running: list[dict[str, Any]]) -> tuple[int, int, int | None] | None:
        """A legmagasabb ár, amit ennél a terméknél érdemes ajánlani: ha egy felmért keresésedre illik, annak a
        szabályai szerint (highest_ok), különben a figyelt oldalakéi (explore_top); de legfeljebb annyi, hogy a haszon a
        vételár legalább RISKY_ROI-ja legyen (különben kockázatos: egy kis árcsökkenés elviszi). (ár, szokásos ár, a
        hasonlók alsó negyede); None, ha nincs ilyen."""
        search = next((s for s in running if matches_query(s["query"], name)), None)
        if search is not None:
            usual, low = int(search["reference"]), None
            top = highest_ok(search, usual)
        else:
            usual, low = int(reference["price"]), careful_low(reference)
            top = explore_top(usual, floor, low)
        if not top or top <= 0:
            return None
        return min(top, math.floor((safe_sale(usual, low) - SHIPPING) / (1 + RISKY_ROI))), usual, low

    def haggle_candidates(self, now: float | None = None) -> list[dict[str, Any]]:
        """Alkuképes hirdetések az árbázisból (modell és letöltés nélkül): amit az utóbbi két napban még fent láttunk
        (HAGGLE_FRESH), legalább HAGGLE_DAYS napja fent van vagy már leárazták, és legfeljebb HAGGLE_MAX-szal drágább
        annál, ami még megéri (a szokásos ára biztos: sure_price). A legígéretesebb elöl: minél régebben fent van, minél
        többször árazta le, és minél kisebb engedmény kell. Ami már megnéztünk (vagy szóltunk róla), kimarad."""
        now = self.clock() if now is None else now
        with self._lock:
            done = set(self._data["haggle"]["done"]) | self._deal_keys()
            floor = int(self._data["browse"].get("min_profit") or WATCH_PROFIT)
            running = [json.loads(json.dumps(s)) for s in self._data["searches"] if search_status(s) == "fut"]
        out: list[dict[str, Any]] = []
        for key in self.book.keys():
            if wearable(self.book.name(key)):
                continue                       # ruhával nem kereskedik
            samples = [sample for sample in self.book.samples(key) if sample["site"] in HAGGLE_SITES
                       and not sample["gone"] and now - sample["last"] <= HAGGLE_FRESH
                       and listing_key(sample["url"]) not in done]
            if not samples:
                continue
            name = self.book.name(key)
            search_id = next((s["id"] for s in running if matches_query(s["query"], name)), None)
            for sample in samples:
                # a szokásos ár a jelölt saját ára nélkül (önmagához nem mérhető); letöltés nélkül: az eltárolt leírások
                # szerint hibás összehasonlítók nélkül (az adatlapot az alku előtt úgyis megnézi)
                reference = self._screened(self.book.reference(key, exclude=sample["url"]),
                                           self._want({"title": sample.get("t") or name, "price": sample["p"],
                                                       "seller": sample.get("seller") or ""}))
                if reference is None or not reference["enough"]:
                    continue
                limits = self._haggle_top(name, reference, floor, running)
                if limits is None:
                    continue
                top, usual, low = limits
                price, days = int(sample["p"]), (now - sample["at"]) / 86400
                cut = 1 - top / price if price > 0 else 1.0
                if price <= top or cut > HAGGLE_MAX or (days < HAGGLE_DAYS and not sample["cuts"]):
                    continue                   # már olcsó (arról a többi út szól), túl drága, vagy még friss
                offer = top // 100 * 100
                if offer <= 0 or not self._affordable(offer):
                    continue
                score = min(days, 30) / 7 + min(sample["cuts"], 2) * 1.5 + (HAGGLE_MAX - cut) * 10
                out.append({"key": key, "name": name, "url": sample["url"], "platform": sample["site"], "price": price,
                            "offer": offer, "cut": round(1 - offer / price, 2), "days": int(days), "cuts": sample["cuts"],
                            "p0": sample["p0"], "score": round(score, 2), "reference": reference, "median": usual,
                            "low": low, "floor": floor, "search": search_id})
        return sorted(out, key=lambda c: -c["score"])

    def _step_haggle(self, game: _Round) -> None:
        """A legutóbbi átnézés legígéretesebb hirdetéseinek oldala (_haggle_check); a többi a következő átnézésnél újra
        sorra kerülhet."""
        with self._lock:
            queue, self._haggle_queue = self._haggle_queue, []
        game.next([partial(self._haggle_check, game, candidate) for candidate in queue
                   if candidate["platform"] in self.platforms])

    def _haggle_done(self, *urls: str) -> None:
        """Ezeket a hirdetéseket megnéztük (nem éri meg alkudni rájuk, vagy szóltunk róluk): többé nem jönnek elő."""
        with self._lock:
            done = self._data["haggle"]["done"]
            for url in urls:
                done[listing_key(url)] = self.clock()
            for key, _at in sorted(done.items(), key=lambda kv: kv[1])[:max(0, len(done) - MAX_HAGGLE_DONE)]:
                done.pop(key, None)
            self._save()

    def _haggle_check(self, game: _Round, candidate: dict[str, Any]) -> None:
        """Egy alkuképes hirdetés oldala (csak olvasva): elkelt, hibás, gyanús, fix áras? Az alkuképes, a sürgős, a
        költözés miatti hirdetés feljegyzi. Utána egy rövid modellkérdés (tényleg ez, működik?), mint a többi
        jelöltnél."""
        if game.blocked or game.pages <= 0:
            return
        reference = candidate["reference"]
        site = self.platforms[candidate["platform"]].name
        deal = {"id": secrets.token_hex(5), "site": site, "platform": candidate["platform"], "text": candidate["name"],
                "query": candidate["name"], "price": candidate["price"], "url": candidate["url"],
                "median": candidate["median"],
                "low": candidate["low"], "similar": reference.get("n"), "product_key": candidate["key"],
                "sites": self._site_names(reference.get("sites")) or None, "spread": spread_of(reference),
                "sold": reference.get("sold"), "explore": "Alkuképes (az árbázisból)",
                "profit": expected_profit(candidate["median"], candidate["offer"], candidate["low"]),
                "refs": (reference.get("vetted") or {}).get("refs") or None, "floor": candidate.get("floor"),
                "search": candidate.get("search"),
                "haggle": {k: candidate[k] for k in ("offer", "cut", "days", "cuts", "p0")}}
        deal = {k: v for k, v in deal.items() if v is not None}
        game.pages -= 1
        game.loaded += 1
        try:
            verdict = self._inspect(deal, None, game, title=True, later=True)
        except _Later:
            return                             # az oldal kerete elfogyott: a következő átnézéskor újra (nem a böngészővel)
        if verdict == "captcha":
            game.blocked = True                # később újra sorra kerül
            return
        self._haggle_done(deal["url"])
        if verdict == "gyanús":
            self._suspicious(deal)
            return
        if verdict:                            # elkelt, eltűnt, hibás
            self._mark(deal)
            return
        text = fold(deal.get("details") or "")
        if _FIRM_RE.search(text):
            self._mark(deal)
            self.journal.add(f"Fix áras, nem ajánlottam alkut: „{(deal.get('title') or deal['text'])[:60]}” "
                             f"{format_price(deal['price'])} ({site}).")
            return
        deal["haggle"]["flex"] = list(dict.fromkeys(label for word, label in _FLEX_WORDS if word in text))
        if deal["platform"] == "vinted":       # a Vinteden ajánlatot is tehetsz a Jarvisszal (jóváhagyással)
            deal["offer"] = deal["haggle"]["offer"]
        if game.lane is None or game.checks <= 0:
            self._haggle_conclude(game, deal, None)
            return
        game.checks -= 1
        game.ask(partial(self._confirm, {"id": "alku", "query": candidate["name"]}, deal),
                 partial(self._haggle_conclude, game, deal))

    def _haggle_conclude(self, game: _Round, deal: dict[str, Any], ok: bool | None) -> None:
        """A modell válasza után: ha nem mondott nemet, a találatok közé kerül (Alku), és szól róla, de másképp, mint egy
        megérő találatról: nem riaszt (nincs ismétlődő rezgés), a címe „Alku:”, és naponta legfeljebb HAGGLE_PER_DAY
        ilyen értesítés jön (a többi csak a listában és az Értesítések között)."""
        if ok is False:
            if deal.get("suspect"):
                self._suspicious(deal)
            else:
                self._mark(deal)
            return
        if self._muted(deal.get("product_key"), self.clock()):
            self._mark(deal)
            return
        state, why = self._haggle_reprice(deal)
        if state != "ok":
            title = str(deal.get("title") or deal["text"])[:60]
            self._mark(deal)
            if state == "bizonytalan":         # a javítás (pótlás) költsége nem ismert: tipp, nem ajánlat
                self._unverified(deal, f"alkuval, de {why}", "állapot")
                return
            self.tally("defect" if state == "hibás" else "not_cheap", [deal])
            self.journal.add(f"Alkuval sem éri meg: „{title}” {format_price(deal['price'])} ({deal['site']}) – {why}.")
            return
        offer = deal["haggle"]["offer"]
        deal["safety"] = assess({**deal, "price": offer, "offer": offer})
        with self._lock:
            safe_only = self._data["safe_only"]
        if deal["safety"]["level"] == "kockázatos" and safe_only:
            self._mark(deal)
            self.journal.add(f"Kockázatos alku, nem szóltam: „{str(deal.get('title') or deal['text'])[:60]}” "
                             f"{format_price(deal['price'])} ({deal['site']}) – {'; '.join(deal['safety']['why'])}.")
            return
        deal["at"] = self.clock()
        self._mark(deal)
        if not self._remember(deal):
            return
        today = day_of(self.clock())
        with self._lock:
            told = self._data["haggle"]["told"]
            if told["day"] != today:
                told.update(day=today, n=0)
            told["n"] += 1
            quiet = told["n"] > HAGGLE_PER_DAY
            self._save()
        game.haggles += 1
        self.tally("haggles", [deal])
        title = str(deal.get("title") or deal["text"])[:60]
        self.journal.add(f"Alkuképes: „{title}” {format_price(deal['price'])} ({deal['site']}), "
                         f"{deal['haggle']['days']} napja fent: ajánlj {format_price(offer)}-ot "
                         f"(kb. {format_price(deal['profit'])} haszon).")
        game.notify("haggle", f"Alku: {title}", self._haggle_text(deal), url=deal["url"],
                    actions=[{"action": "deal", "deal": deal["id"], "label": "Ajánlat"}], haggle=deal["id"],
                    quiet=quiet or None)

    def _haggle_limit(self, deal: dict[str, Any], extra: int) -> int | None:
        """A legmagasabb ajánlat, ami ennél az alkunál a teljes költséggel (extra: a pótlás és a javítás, a Vinteden a
        vevővédelmi díj nélkül) is megéri: a keresésed szabályai szerint (highest_ok), vagy a figyelt oldalakéi
        (explore_top), és legfeljebb annyi, hogy a haszon a vételár legalább RISKY_ROI-ja legyen (_haggle_top). Az
        állapot szerinti ár (deal median, low: a hasonló hibájúaké, vagy a hibátlanoké) a mérce."""
        usual, low, platform = int(deal["median"]), deal.get("low"), str(deal.get("platform") or "")
        with self._lock:
            search = next((json.loads(json.dumps(s)) for s in self._data["searches"]
                           if s.get("id") == deal.get("search")), None)
        floor = int(deal.get("floor") or WATCH_PROFIT)
        top = highest_ok({**search, "min_price": None, "max_price": None}, usual) if search is not None else \
            explore_top(usual, floor, low)
        if not top:
            return None
        top = min(top - extra, math.floor((safe_sale(usual, low) - SHIPPING - extra) / (1 + RISKY_ROI)))
        top = before_fee(platform, top)
        return top if top > 0 else None

    def _haggle_reprice(self, deal: dict[str, Any]) -> tuple[str, str]:
        """Egy alku végső számolása (ugyanaz, mint a többi jelöltnél, _reprice): az ajánlott összegre, az állapot szerinti
        árral és a teljes költséggel (posta, a Vinteden a vevővédelmi díj, a pótlás és a javítás). Ha így a javasolt
        ajánlat már nem éri meg, egy kisebbet javasol (_haggle_limit), ha az legfeljebb HAGGLE_MAX-szal kevesebb a kért
        árnál; különben „nem éri meg” (az okkal: mennyi lenne a haszon az ajánlott összegért). „bizonytalan”: egy hibához
        nincs megadott költség (ilyenkor nem ajánl összeget)."""
        haggle = deal["haggle"]
        offer = int(haggle["offer"])
        self._haggle_fees(deal, offer)
        state, why = self._reprice(deal, price=offer)
        if state in ("hibás", "bizonytalan"):
            return state, why
        extra = sum(int(part["amount"]) for part in deal.get("cost_parts") or []
                    if part.get("what") not in ("posta", "vevővédelmi díj"))
        top = self._haggle_limit(deal, extra)
        if top is not None and offer <= top:
            return "ok", ""
        better = self._capped(top) // 100 * 100 if top else 0
        price = int(deal["price"])
        costs = f" (levonva: {', '.join(f'{p['what']} {format_price(p['amount'])}' for p in deal.get('cost_parts') or [])})"
        if better <= 0 or 1 - better / price > HAGGLE_MAX or not self._affordable(better):
            return "nem éri meg", (f"{format_price(offer)}-ért a teljes költséggel{costs} kb. "
                                   f"{format_price(int(deal['profit']))} lenne a haszon")
        haggle.update(offer=better, cut=round(1 - better / price, 2))
        if deal.get("offer"):
            deal["offer"] = better
        self._haggle_fees(deal, better)
        state, why = self._reprice(deal, price=better)
        return state, why

    @staticmethod
    def _haggle_fees(deal: dict[str, Any], offer: int) -> None:
        """Az alku költségtételei az ajánlott összegre (mint a többi jelöltnél, _deal): a posta, és a Vinteden a vevő
        díja; a pótlás és a javítás az állapot szerint jön hozzá (_reprice)."""
        fee = buy_fee(str(deal.get("platform") or ""), offer)
        deal["cost_parts"] = [{"what": "posta", "amount": SHIPPING}] + (
            [{"what": "vevővédelmi díj", "amount": fee}] if fee else [])
        if fee:
            deal["fee"] = fee
        else:
            deal.pop("fee", None)

    def _haggle_text(self, deal: dict[str, Any]) -> str:
        """Az alkuképes hirdetés értesítése: mennyiért van fent, mióta (és leárazták-e), mennyit ajánlj, és akkor
        mennyi a haszon."""
        haggle = deal["haggle"]
        since = f"{haggle['days']} napja fent" if haggle["days"] >= 1 else "nemrég került fel"
        if haggle.get("cuts") and haggle.get("p0", 0) > deal["price"]:
            since += f", már leárazta: {format_price(haggle['p0'])} → {format_price(deal['price'])}"
        if haggle.get("flex"):
            since += f"; a hirdetésben: {', '.join(haggle['flex'])}"
        basis = f" ({deal['similar']} hasonló alapján)" if deal.get("similar") else ""
        extra = [part for part in deal.get("cost_parts") or [] if part.get("what") != "posta"]
        costs = (f" (a posta mellett levonva: {', '.join(f'{part['what']} kb. {format_price(part['amount'])}' for part in extra)})"
                 if extra else "")
        state = f" Állapot: {deal['condition_text']}." if deal.get("condition_text") else ""
        return (f"{deal['site']}: „{str(deal.get('title') or deal['text'])[:80]}” {format_price(deal['price'])} ({since}). "
                f"A szokásos ára kb. {format_price(deal['median'])}{basis}. Ha {format_price(haggle['offer'])}-ért "
                f"odaadja ({round(haggle['cut'] * 100)}%-kal kevesebb), kb. {format_price(deal['profit'])} a haszon"
                f"{costs}.{valuation_text(deal)}{state} Az ajánlatot a találat ablakából másolhatod ki.")

    # --- csomagbontás: csomaghirdetések, darabonként felmérve ---

    def _bundle_state(self) -> dict[str, Any]:
        with self._lock:
            bundles = self._data["bundles"]
            return {"on": bundles["on"], "min_profit": bundles["min_profit"],
                    "searches": json.loads(json.dumps(bundles["searches"])), "waiting": len(bundles["inbox"]),
                    "presets": [{"name": name, "query": query, "sites": [s for s in sites if s in self.platforms]}
                                for name, query, sites in BUNDLE_PRESETS],
                    "piece_min": PIECE_MIN, "max_searches": MAX_BUNDLE_SEARCHES}

    def _bundles_due(self, now: float) -> bool:
        """Van-e dolga a csomagbontásnak (a hívó fogja a zárat): egy esedékes csomagkeresés, vagy felmérésre váró csomag
        (modell nélkül nem mérhető fel)."""
        bundles = self._data["bundles"]
        if not bundles["on"]:
            return False
        if bundles["inbox"] and self.llm is not None:
            return True
        return any(self._due(search, key, now) for search in bundles["searches"]
                   for key in search.get("sites") or list(self.platforms) if key in self.platforms)

    def set_bundles(self, on: Any = None, min_profit: Any = None) -> dict[str, Any]:
        """Csomagbontás be/ki (kikapcsolva a felmérésre várók is elmaradnak), és legalább mennyi haszonnal szóljon."""
        profit = self._amount(min_profit, "A legkisebb haszon") if min_profit not in (None, "") else None
        with self._lock:
            bundles = self._data["bundles"]
            if on is not None:
                bundles["on"] = bool(on)
                if not bundles["on"]:
                    bundles["inbox"] = []
            if profit:
                bundles["min_profit"] = profit
            self._save()
        if on is not None:
            self.journal.add("Bekapcsoltad a csomagbontást: a csomaghirdetéseket darabonként is felmérem." if on
                             else "Kikapcsoltad a csomagbontást.")
        return self.state()

    def add_bundle_search(self, query: str, sites: list[str] | None = None, max_price: Any = None,
                          every: Any = None) -> dict[str, Any]:
        """Csomagkeresés (pl. „ruhacsomag”): a találatai mind a felmérésre várók közé kerülnek (szokásos ár nem kell).
        Bekapcsolja a csomagbontást is."""
        search = self._clean({"query": query, "sites": sites or list(self.platforms), "max_price": max_price,
                              "every": every})
        if wearable(search["query"]):
            raise DealError("Ruhával és cipővel nem kereskedem (ezt kérted): válassz más csomagot.")
        with self._lock:
            bundles = self._data["bundles"]
            if any(fold(s["query"]) == fold(search["query"]) for s in bundles["searches"]):
                raise DealError("Ezt a csomagot már keresem.")
            if len(bundles["searches"]) >= MAX_BUNDLE_SEARCHES:
                raise DealError(f"Legfeljebb {MAX_BUNDLE_SEARCHES} csomagkeresés lehet.")
            bundles["searches"].append({"id": "b" + secrets.token_hex(4), **search})
            bundles["on"] = True
            self._save()
        return self.state()

    def remove_bundle_search(self, search_id: str) -> dict[str, Any]:
        with self._lock:
            bundles = self._data["bundles"]
            bundles["searches"] = [s for s in bundles["searches"] if s.get("id") != search_id]
            for key in self.platforms:
                self._data["last"].pop(f"{search_id}:{key}", None)
            self._save()
        return self.state()

    def _to_bundles(self, rows: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
        """A csomagnak látszó hirdetések (bundle_like) a felmérésre várók közé (_inbox); a többit visszaadja (a hívó
        látottnak jegyzi, ahogy eddig). Ha a csomagbontás ki van kapcsolva, mindet visszaadja."""
        rest = self._inbox(rows, label, lambda row: bundle_like(row["text"]) and not wearable(row["text"]))
        left = {id(row) for row in rest}
        self.tally("bundle", [row for row in rows if id(row) not in left])   # a mai számokban: a csomagbontásé
        return rest

    def _inbox(self, rows: list[dict[str, Any]], label: str,
               wanted: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
        """Hirdetések a felmérésre várók elejére (a legújabbak elöl; legfeljebb BUNDLE_INBOX), ha be van kapcsolva a
        csomagbontás: ami nem licit, wanted szerint csomag, valódi ára van (BUNDLE_MIN_PRICE), és belefér a vételi
        limitbe. Ami már vár, amit már láttunk vagy amiről már szóltunk, kimarad. Visszaadja, amit nem vett fel."""
        if not rows:
            return []
        with self._lock:
            bundles = self._data["bundles"]
            if not bundles["on"]:
                return list(rows)
            have = {listing_key(r["url"]) for r in bundles["inbox"]} | self._deal_keys() | set(self._data["seen"])
        taken, rest = [], []
        for row in rows:
            price = int(row.get("price") or 0)
            if not row.get("url") or row.get("auction") or price < BUNDLE_MIN_PRICE or not wanted(row) \
                    or not self._affordable(price):
                rest.append(row)
                continue
            key = listing_key(row["url"])
            if key in have:
                continue
            have.add(key)
            taken.append({"site": str(row.get("site") or ""),
                          "platform": str(row.get("platform") or self._platform_of(row["url"])),
                          "text": str(row.get("text") or "")[:180], "price": price, "url": str(row["url"]),
                          **({"title": str(row["title"])[:160]} if row.get("title") else {}), "from": label[:80]})
        if taken:
            with self._lock:
                bundles = self._data["bundles"]
                fresh = {listing_key(r["url"]) for r in taken}
                bundles["inbox"] = (taken + [r for r in bundles["inbox"]
                                             if listing_key(r["url"]) not in fresh])[:BUNDLE_INBOX]
                self._save()
        return rest

    def _return_bundles(self, rows: list[dict[str, Any]], failed: bool = False) -> None:
        """Vissza a felmérésre várók elejére (a kör keretébe már nem fért bele, vagy a modell nem válaszolt: failed).
        Amire a modell BUNDLE_TRIES-szor sem válaszolt, az kimarad (látottnak jegyzi)."""
        if failed:
            rows = [{**row, "tries": int(row.get("tries") or 0) + 1} for row in rows]
            self._mark(*[row for row in rows if row["tries"] >= BUNDLE_TRIES])
            rows = [row for row in rows if row["tries"] < BUNDLE_TRIES]
        if not rows:
            return
        with self._lock:
            bundles = self._data["bundles"]
            if not bundles["on"]:
                return
            back = {listing_key(r["url"]) for r in rows}
            bundles["inbox"] = (rows + [r for r in bundles["inbox"] if listing_key(r["url"]) not in back])[:BUNDLE_INBOX]
            self._save()

    def _step_bundle_search(self, game: _Round, search: dict[str, Any], sites: list[str]) -> None:
        """Egy csomagkeresés találati oldalai (a legújabb elöl): a kereső- és a cserehirdetés kivételével minden
        találat a felmérésre várók közé kerül (a megadott ársávban)."""
        rows, problems = self._collect(search, sites, game.check, game=game)
        game.errors += problems
        cap = search.get("max_price")
        self._inbox([row for row in rows if not cap or row["price"] <= cap], f"Csomagkeresés: {search['query']}",
                    lambda row: not _not_for_sale(row["text"]))

    def _step_bundles(self, game: _Round) -> None:
        """A felmérésre váró csomagok: a modell a címük és az áruk alapján kiválasztja a legígéretesebbeket (egy hívás,
        legfeljebb EXPLORE_ROWS csomag; a többit látottnak jegyzi), és ezek oldalát megnyitja (_bundle_candidate). A
        gyors sor csak a böngésző nélkül olvasható oldalak csomagjait veszi el."""
        if game.bundle_picks <= 0 or game.blocked:
            return
        if self.llm is None:                   # modell nélkül nem derül ki, mi van benne
            with self._lock:
                rows, self._data["bundles"]["inbox"] = self._data["bundles"]["inbox"], []
                self._save()
            self._mark(*rows)
            return
        if game.lane is None:
            return                             # most nem lehet kérdezni a modellt: később
        fast = {k for k in self.platforms if self.fetcher is not None and self.fetcher.usable(k)}
        with self._lock:
            inbox = self._data["bundles"]["inbox"]
            batch = [row for row in inbox if row.get("platform") in self.platforms
                     and (not game.no_browser or row.get("platform") in fast)][:EXPLORE_ROWS]
            if not batch:
                return
            taken = {id(row) for row in batch}
            self._data["bundles"]["inbox"] = [row for row in inbox if id(row) not in taken]
            self._save()
        game.ask(partial(self._bundle_pick, batch), partial(self._bundle_picked, game, batch))

    def _bundle_pick(self, batch: list[dict[str, Any]]) -> list[int] | None:
        """Egy modellhívás: melyik csomag érhet darabonként jóval többet (a kérés szerint legfeljebb kettő; ha többet
        választ, a kör keretén felüliek a következő körre várnak). None: nem válaszolt."""
        lines = "\n".join(f"{n}. [{row['site']}] {row.get('title') or row['text']} – {format_price(row['price'])}"
                          for n, row in enumerate(batch, 1))
        messages = [{"role": "system", "content": BUNDLE_PICK_SYSTEM},
                    {"role": "user", "content": "Csomaghirdetések:\n" + lines}]
        try:
            data = self.llm.chat_json(messages, BUNDLE_PICK_SCHEMA, temperature=0.0, max_tokens=200)
        except Exception:  # noqa: BLE001 - a modell hibája ne állítsa le a kört
            return None
        picks: list[int] = []
        for number in data.get("picks") or []:
            try:
                index = int(number) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(batch) and index not in picks:
                picks.append(index)
        return picks[:EXPLORE_ROWS]

    def _bundle_picked(self, game: _Round, batch: list[dict[str, Any]], picks: list[int] | None) -> None:
        """A modell választása után: a kiválasztottak oldala jön (a kör keretébe nem férők a következő körre várnak), a
        többit látottnak jegyzi."""
        if picks is None:
            self._return_bundles(batch, failed=True)
            return
        chosen = [batch[index] for index in picks]
        now, later = chosen[:game.bundle_picks], chosen[game.bundle_picks:]
        game.bundle_picks -= len(now)
        picked = {id(row) for row in chosen}
        self._mark(*[row for row in batch if id(row) not in picked])
        self._return_bundles(later)
        game.next([partial(self._bundle_candidate, game, row) for row in now])

    def _bundle_candidate(self, game: _Round, row: dict[str, Any]) -> None:
        """Egy kiválasztott csomag oldala (csak olvasva: elkelt, gyanús?), a leírása és az első BUNDLE_PHOTOS fotója;
        utána a modell tételesen felsorolja, mi van benne (_itemize). Ha a kör keretébe már nem fér bele, vár."""
        if game.blocked or game.pages <= 0 or game.checks <= 0:
            self._return_bundles([row])
            return
        deal = {k: v for k, v in row.items() if k not in ("from", "tries")}
        deal.update(id=secrets.token_hex(5), explore=row.get("from") or "Csomagbontás")
        game.pages -= 1
        game.loaded += 1
        try:
            verdict = self._inspect(deal, None, game, photos=BUNDLE_PHOTOS, defects=False, later=True)
        except _Later:
            self._return_bundles([row])        # az oldal kerete elfogyott: vár (nem számít sikertelennek)
            return
        if verdict == "captcha":
            game.blocked = True
            self._return_bundles([row])
            return
        if not verdict and not deal.get("details"):
            self._return_bundles([row], failed=True)   # az oldala nem nyílt meg: a leírása és a fotói nélkül nem
            return                                     # mérhető fel (később, vagy a böngészővel újra)
        if verdict == "gyanús":
            self._suspicious(deal)
            return
        if verdict:                            # elkelt, eltűnt
            self._mark(deal)
            return
        # csomagként csak az mérhető fel, aminek az ára az egészre vonatkozik (ezt írja, vagy csomagnak hirdeti, és
        # darabárról nem ír): a darabár, a „-tól” ár (a legolcsóbb tételé), és ha mindkettőre utal, egyben nem (a modellt
        # sem kérdezi meg róla)
        scope, quote = price_scope(self._condition_source(deal))
        deal["price_scope"] = {"scope": scope or "egyben (csomagként hirdetve)", "quote": quote[:160]}
        if scope not in ("egyben", ""):
            self._mark(deal)
            why = ("az ára darabár vagy a legolcsóbb tételé" if scope == "darabár" else
                   "nem derül ki, hogy az ára az egészre vonatkozik-e, vagy darabár")
            self.journal.add(f"Csomag, de {why}" + (f" („{quote[:80]}”)" if quote else "") +
                             f": „{str(deal.get('title') or deal['text'])[:60]}” {format_price(deal['price'])} "
                             f"({deal['site']}) – egyben nem mértem fel (kérdezd meg az eladót, mennyi az egész).")
            return
        game.checks -= 1
        game.ask(partial(self._itemize, deal), partial(self._bundle_priced, game, row, deal))

    def _itemize(self, deal: dict[str, Any]) -> dict[str, Any] | None:
        """Egy modellhívás a csomag leírásával és fotóival: mi van benne tételenként (név, keresőszó, darabszám, a
        modell becsült darabára), csomag-e egyáltalán, és gyanús-e. None: nem válaszolt."""
        photos = self._photos(deal, BUNDLE_PHOTOS)
        text = (f"Csomaghirdetés ({deal['site']}): {deal.get('title') or deal['text']}\n"
                f"Ára: {format_price(deal['price'])}"
                + (f"\nA hirdetés oldala: {deal['details'][:1500]}" if deal.get("details") else "")
                + (f"\nA hirdetés fotói ({len(photos)} db) mellékelve." if photos else ""))
        messages = [{"role": "system", "content": BUNDLE_SYSTEM},
                    {"role": "user", "content": text, **({"images": photos} if photos else {})}]
        try:
            data = self.llm.chat_json(messages, BUNDLE_SCHEMA, temperature=0.0, max_tokens=900)
        except Exception:  # noqa: BLE001 - a modell hibája ne állítsa le a kört
            if not photos:
                return None
            try:                               # pl. a modell nem tud képet: fotók nélkül
                messages[1] = {"role": "user", "content": text.rsplit("\nA hirdetés fotói", 1)[0]}
                data = self.llm.chat_json(messages, BUNDLE_SCHEMA, temperature=0.0, max_tokens=900)
                photos = []
            except Exception:  # noqa: BLE001
                return None
        lines = []
        for line in data.get("lines") or []:
            if not isinstance(line, dict):
                continue
            name = " ".join(str(line.get("name") or "").split())[:60]
            try:
                count = min(max(int(line.get("count") or 1), 1), MAX_PIECES)
                estimate = min(max(int(line.get("estimate") or 0), 0), 2_000_000)
            except (TypeError, ValueError):
                continue
            if name:
                lines.append({"name": name, "query": " ".join(str(line.get("query") or "").split())[:60],
                              "count": count, "estimate": estimate})
        return {"bundle": bool(data.get("bundle")), "lines": lines[:MAX_LINES],
                "suspicious": bool(data.get("suspicious")), "reason": str(data.get("reason") or "")[:200],
                "photos": len(photos)}

    def _bundle_priced(self, game: _Round, row: dict[str, Any], deal: dict[str, Any],
                       result: dict[str, Any] | None) -> None:
        """A modell felsorolása után (a böngésző szálán): a darabárak (_price_lines), mennyit ér darabonként eladva
        (bundle_value), és ha megéri (legalább a megadott haszon, és a vételár legalább BUNDLE_ROI-ja), nyugodt szívvel
        megvehető-e (assess_bundle). Ami átment, arról szól; a többit a naplóba írja."""
        if result is None:
            self._return_bundles([row], failed=True)
            return
        deal.update(checked=True, photos_checked=result["photos"] or None, note=result["reason"])
        if result["suspicious"]:
            deal["suspect"] = "a modell szerint gyanús" + (f": {deal['note']}" if deal["note"] else "")
            self._suspicious(deal)
            return
        name = str(deal.get("title") or deal["text"])[:60]
        if not result["bundle"] or not result["lines"]:
            self._mark(deal)
            self.journal.add(f"Nem csomag (vagy nem derül ki, mi van benne): „{name}” {format_price(deal['price'])}.")
            return
        game.bundled += 1
        lines = self._price_lines(game, deal, result["lines"])
        info = bundle_value(lines, int(deal["price"]))
        # az eladás költségei hirdetésenként (a megadott csomagolás a beépített becslés helyett, az eladási díj és a
        # tartalék a darab árára: set_fees), mint a többi találatnál
        extra = sum((self._piece_cost(int(line["unit"])) - PIECE_COST) * int(line["count"]) for line in lines
                    if line["unit"] >= PIECE_MIN) + \
            (self._piece_cost(int(info["leftover"])) - PIECE_COST if info["leftover"] else 0)
        info["profit"] -= extra
        info["selling"] = info["listings"] * PIECE_COST + extra
        deal.update(bundle={**info, "lines": lines}, profit=info["profit"], value=info["value"], at=self.clock())
        unset = self._fees_unset()
        if unset:
            deal["fees_unset"] = unset
        with self._lock:
            floor = int(self._data["bundles"]["min_profit"] or BUNDLE_PROFIT)
            safe_only = self._data["safe_only"]
        summary = (f"darabonként kb. {format_price(info['value'])} ({info['listings']} hirdetés), a haszon kb. "
                   f"{format_price(info['profit'])}")
        if info["profit"] < floor or info["profit"] < deal["price"] * BUNDLE_ROI or not self._affordable(deal["price"]):
            self._mark(deal)
            self.journal.add(f"Csomag, nem éri meg: „{name}” {format_price(deal['price'])} ({deal['site']}) – {summary}.")
            return
        deal["safety"] = assess_bundle(deal)
        if deal["safety"]["level"] == "kockázatos" and safe_only:
            self._mark(deal)
            self.journal.add(f"Kockázatos csomag, nem szóltam: „{name}” {format_price(deal['price'])} ({deal['site']}) "
                             f"– {summary}; {'; '.join(deal['safety']['why'])}.")
            return
        self._mark(deal)
        if not self._remember(deal):          # közben már elmentette (egy másik sor): nem szól újra
            return
        game.found += 1
        self.tally("found", [deal])
        hit = f"„{name}” {format_price(deal['price'])} ({deal['site']}, csomag, kb. {format_price(info['profit'])} haszon)"
        game.hits.append(hit)
        self.journal.add(f"Jó csomag: {hit} – {deal['explore']}")
        game.notify("deal", f"Csomag: {name}", self._bundle_text(deal), url=deal["url"],
                    actions=[deal_action(deal["id"])], deal=deal["id"], urgent=True)

    def _price_lines(self, game: _Round, deal: dict[str, Any], lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Egy csomag tételeinek darabára (unit): az árbázis szokásos ára (ha ismeri a terméket), vagy a gyors oldalak
        mostani hirdetései (a legértékesebb tételekre, csomagonként legfeljebb BUNDLE_LOOKUPS keresés; az árak az
        árbázisba is bekerülnek), óvatosan (safe_sale), de legfeljebb a modell becslésének GUESS_CAP-szorosa (source:
        „piac”); ahol egyik sincs, a becslés GUESS_RATIO-ja (source: „becslés”)."""
        sites = [k for k in RESALE_SITES if k in self.platforms and self.fetcher is not None and self.fetcher.usable(k)]
        lookups = BUNDLE_LOOKUPS if sites else 0
        priced = [dict(line) for line in lines]
        for line in sorted(priced, key=lambda line: -line["estimate"] * line["count"]):
            found = None
            for text in (line["query"], line["name"]):
                product = self.book.key(text) if text else None
                found = self._screened(self.book.reference(product[0], exclude=deal["url"]),
                                       specs.extract(text)) if product else None
                if found:
                    break
            if found is None and lookups > 0 and line["query"] and line["estimate"] >= PIECE_MIN:
                lookups -= 1
                probe = {"id": "csomag", "query": self._probe_text(line["query"])}
                rows = self._collect(probe, sites, game.check, track=False, game=game)[0]
                market_now = self._market_of(deal, line["query"], rows)
                if market_now and market_now["n"] >= BUNDLE_MARKET_MIN:
                    found = market_now
            if found:
                unit = safe_sale(int(found["price"]), careful_low(found))
                if line["estimate"]:
                    unit = min(unit, int(line["estimate"] * GUESS_CAP))
                line.update(unit=round_price(unit), source="piac", n=int(found.get("n") or 0))
            else:
                line.update(unit=round_price(line["estimate"] * GUESS_RATIO) if line["estimate"] else 0,
                            source="becslés")
        return priced

    def _step_seller(self, game: _Round, deal: dict[str, Any]) -> None:
        """Egy jó találat eladójának többi hirdetése (a kör egy oldalbetöltéséből): ha együtt jobban megéri, szól róla
        (csendesen, a találat után; seller_bundle)."""
        url = str(deal.get("seller_url") or "")
        platform = str(deal.get("platform") or self._platform_of(url))
        if not url or platform not in self.platforms or game.blocked or game.pages <= 0:
            return
        game.check()
        game.pages -= 1
        game.loaded += 1
        try:
            listings = self._listings(url, platform, game.check, game)
        except BrowserError:
            return
        site = self.platforms[platform].name
        rows = [{"site": site, "platform": platform, "text": str(entry.get("text") or "")[:180],
                 "price": int(entry["price"]), "url": str(entry.get("url") or ""),
                 **({"title": str(entry["title"])[:160]} if entry.get("title") else {})}
                for entry in listings[:MAX_PER_SITE] if entry.get("url") and not entry.get("auction")]
        self.book.add(rows)
        summary = self.seller_bundle(deal, rows)
        if summary and summary["worth"]:
            game.notify("deal", f"Ugyanettől az eladótól: {str(deal.get('title') or deal['text'])[:50]}",
                        self.seller_text(deal, summary), url=url, actions=[deal_action(deal["id"])], deal=deal["id"],
                        quiet=True)

    def seller_bundle(self, deal: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Egy jó találat eladójának többi hirdetése (rows: a listája): melyik adható tovább külön is haszonnal. Minden
        darabra ugyanazok a vételi ellenőrzések, mint egy találatra: valódi ár-e (nem 1 Ft, nem a szokásos ár töredéke),
        nem gyanúsan olcsó és nem hamisítványgyanús, belefér-e a vételi limitbe, van-e elég hasonló ára (az árbázis vagy
        egy kereséseid szokásos ára szerint; ez csak javaslat, a darabok leírását nem olvasta el), nem lassan kelő,
        van-e rajta haszon, és nem kockázatos (ha csak a nyugodt szívvel megvehetőkről szólsz). A megmaradtakból a
        legjobbak kerülnek a közös vételbe, amíg a végösszeg belefér a vételi limitbe; utána számolja, mennyi a haszon
        együtt: egy posta (SHIPPING), a Vinteden a vevővédelmi díj a végösszegre egyszer, darabonként a csomagolás és a
        meghirdetés (PIECE_COST), és mennyi munka (darabonként LISTING_MINUTES). Összeveti azzal, ha csak ezt veszed
        meg, és azzal, ha külön-külön vennéd. A ruha, a csomag, a nem eladási árú hirdetés kimarad; amiről nem tudni,
        mennyit ér, azt csak megszámolja. A találatba is beírja (seller); None, ha az eladónak nincs más hirdetése."""
        own, vinted = listing_key(deal["url"]), deal.get("platform") == "vinted"
        fee = (lambda amount: int(round(VINTED_FEE_FIXED + VINTED_FEE_RATE * amount))) if vinted else (lambda amount: 0)
        with self._lock:
            safe_only = self._data["safe_only"]
        cap = self._cap()
        items: list[dict[str, Any]] = []
        unknown = 0
        for row in rows[:SELLER_ROWS]:
            text = str(row.get("title") or row.get("text") or "")
            if not row.get("url") or listing_key(row["url"]) == own or row.get("auction") or unusable(text) \
                    or wearable(text):
                continue
            value = self.quick_value({**row, "text": text}, 1)
            if value is None:
                unknown += 1
                continue
            price, reference = int(row["price"]), int(value["reference"])
            resale = safe_sale(reference, value.get("low"))
            product = self.book.key(text)
            pace_now = pace(self.book.speed(product[0])) if product else ""
            margin = resale - price - self._piece_cost(resale)
            suspect = self._suspect({"price": price, "median": reference, "product_key": product[0] if product else ""})
            why = ("nem valódi ár (pl. 1 Ft vagy „ár megegyezés szerint”)" if price < max(BUNDLE_MIN_PRICE, reference * FLOOR)
                   else "gyanúsan olcsó (átverés, hamisítvány vagy hibás lehet)" if suspect.startswith("gyanúsan")
                   else "gyakran hamisított márka, feltűnően olcsón" if suspect
                   else "a vételi limit fölött" if not self._affordable(price)
                   else "bizonytalan ár (kevés hasonló)" if not value.get("enough", value.get("sure"))
                   else "lassan kel" if pace_now == "lassú"
                   else "külön nem éri meg" if margin <= 0
                   else "kockázatos: kicsi a haszon a befektetett pénzhez képest" if safe_only and margin < price * RISKY_ROI
                   else "")
            items.append({"t": text[:TITLE_KEEP], "p": price, "u": str(row["url"]), "reference": reference,
                          "resale": resale, "margin": margin, "n": value.get("n"), "pace": pace_now,
                          "pick": not why, **({"why": why} if why else {})})
        if not items and not unknown:
            return None
        price = int(deal.get("offer") or deal["price"])
        total, picked = price, []
        for item in sorted((item for item in items if item["pick"]), key=lambda i: -i["margin"]):
            if cap is not None and total + item["p"] > cap:   # a legjobbak, amíg a végösszeg belefér a vételi limitbe
                item.update(pick=False, why="együtt már nem fér a vételi limitbe")
                continue
            picked.append(item)
            total += item["p"]
        own_resale = int(deal.get("resale") or safe_sale(int(deal["median"]), deal.get("low")))
        piece = self._piece_cost             # csomagolás és az eladás többi költsége darabonként
        single = own_resale - price - SHIPPING - fee(price) - piece(own_resale)
        together = own_resale + sum(item["resale"] for item in picked) - total - SHIPPING - fee(total) \
            - piece(own_resale) - sum(piece(item["resale"]) for item in picked)
        separately = single + sum(item["resale"] - item["p"] - SHIPPING - fee(item["p"]) - piece(item["resale"])
                                  for item in picked)
        summary = {"url": str(deal.get("seller_url") or ""), "items": sorted(items, key=lambda i: -i["margin"])[:12],
                   "picked": len(picked), "total": total, "shipping": SHIPPING, "fee": fee(total), "profit": together,
                   "single": single, "separately": separately, "listings": 1 + len(picked),
                   "minutes": (1 + len(picked)) * LISTING_MINUTES, "unknown": unknown, "vinted": vinted,
                   "offer": round_price(total * (1 - BUNDLE_OFFER_CUT)) if picked else None,
                   "worth": bool(picked) and together - single >= SELLER_GAIN}
        with self._lock:
            for stored in self._data["deals"]:
                if stored.get("id") == deal.get("id"):
                    stored["seller"] = summary
            self._save()
        deal["seller"] = summary
        if summary["worth"]:
            name = str(deal.get("title") or deal["text"])[:40]
            self.journal.add(f"Ugyanettől az eladótól még {len(picked)} darab megérheti („{name}” mellé): együtt kb. "
                             f"{format_price(together)} haszon (csak ez: {format_price(single)}).")
        return summary

    def seller_text(self, deal: dict[str, Any], summary: dict[str, Any]) -> str:
        """Az értesítés szövege az eladó többi hirdetéséről: mi éri meg még, együtt mennyi a haszon (egy posta, a díj a
        végösszegre), mennyi munka, és mennyit ajánlj a közös vételre."""
        picked = [item for item in summary["items"] if item["pick"]]
        pieces = "; ".join(f"„{item['t'][:50]}” {format_price(item['p'])} (külön is eladható, kb. "
                           f"{format_price(item['margin'])} haszon)" for item in picked[:4])
        fee = f"; a Vinted vevővédelmi díja kb. {format_price(summary['fee'])} a végösszegre egyszer" if summary["vinted"] \
            else ""
        offer = (f" Közös vételre ajánlj kb. {format_price(summary['offer'])}-ot" +
                 (" (csomagajánlat)." if summary["vinted"] else ".")) if summary.get("offer") else ""
        return (f"„{str(deal.get('title') or deal['text'])[:60]}” eladójánál még {summary['picked']} darab megérheti: "
                f"{pieces}. Együtt {format_price(summary['total'])} (egy posta: {format_price(summary['shipping'])}{fee}), "
                f"kb. {format_price(summary['profit'])} haszon (csak ez: kb. {format_price(summary['single'])}). Munka: "
                f"{summary['listings']} hirdetés, kb. {summary['minutes']} perc.{offer}")

    def _bundle_text(self, deal: dict[str, Any]) -> str:
        """Az értesítés szövege egy megérő csomagról: mi van benne (a legértékesebb tételek), mennyit ér darabonként,
        mennyi a haszon, és nyugodt szívvel megvehető-e."""
        bundle = deal["bundle"]
        big = sorted((line for line in bundle["lines"] if line["unit"] >= PIECE_MIN),
                     key=lambda line: -line["unit"] * line["count"])
        parts = [(f"{line['count']} db " if line["count"] > 1 else "") + line["name"] +
                 f" (kb. {format_price(line['unit'])}" + ("/db" if line["count"] > 1 else "") + ")" for line in big[:4]]
        if len(big) > 4:
            parts.append(f"és még {len(big) - 4} tétel")
        if bundle["leftover"]:
            parts.append(f"{bundle['small']} apróság egyben kb. {format_price(bundle['leftover'])}")
        warn = f" {safety_text(deal['safety'])}" if deal.get("safety") else ""
        unset = (f" Nincs megadva: {', '.join(deal['fees_unset'])} (0 Ft-tal számoltam)." if deal.get("fees_unset")
                 else "")
        return (f"{deal['site']}: csomag, „{str(deal.get('title') or deal['text'])[:80]}” "
                f"{format_price(deal['price'])}. Darabonként eladva kb. {format_price(bundle['value'])} "
                f"({bundle['listings']} hirdetés): {'; '.join(parts)}. A várható haszon kb. "
                f"{format_price(deal['profit'])} (a posta és a csomagolás benne van).{unset}{warn} {deal['explore']}.")

    def bought_group(self, item_id: str) -> list[str]:
        """Egy megvett tárgy és a csomagtársai (ha egy csomag darabja: a csomag minden darabja; különben csak ő)."""
        with self._lock:
            for deal in self._data["deals"]:
                if item_id in (deal.get("bought_items") or []):
                    return list(deal["bought_items"])
        return [item_id]

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

    def _all_lanes(self, site: str) -> list[dict[str, Any]]:
        """Egy oldal minden indulóoldala, az „on” jelzővel: a beépítettek (BROWSE_LANES; kikapcsolhatók), a tanultak
        (_learn; ami nem hozott semmit, vagy amit töröltél, az nincs itt) és a sajátjaid (lane_add)."""
        platform = self.platforms.get(site)
        if platform is None:
            return []
        out = []
        stats = self._data["lanes"]
        for name, target in BROWSE_LANES.get(site, ()):
            if not target:
                url, kind = platform.home, "feed"
            elif re.match(r"https?://", target):
                host = (urllib.parse.urlsplit(target).hostname or "").removeprefix("www.")
                ours = any(host == d or host.endswith("." + d) for d in platform.domains)
                url = target if ours else platform.search_url.format(q=urllib.parse.quote(name))
                kind = "page"
            else:
                url, kind = newest_first(platform.search_url.format(q=urllib.parse.quote(target)), site), "page"
            ident = f"{site}:" + re.sub(r"[^a-z0-9]+", "_", fold(name)).strip("_")
            out.append({"id": ident, "site": site, "name": f"{platform.name}: {name}", "url": url, "kind": kind,
                        "on": not (stats.get(ident) or {}).get("disabled")})
        for ident, lane in list(stats.items()):
            if lane.get("site") != site or not lane.get("url"):
                continue
            if lane.get("kind") == "tanult" and not lane.get("off") and not lane.get("removed") \
                    and not wearable(str(lane.get("name") or "")):
                out.append({"id": ident, "site": site, "name": str(lane.get("name") or ident), "url": lane["url"],
                            "kind": "tanult", "on": True})
            elif lane.get("kind") == "sajat":
                out.append({"id": ident, "site": site, "name": str(lane.get("name") or ident), "url": lane["url"],
                            "kind": "sajat", "on": not lane.get("disabled")})
        return out

    def _lanes(self, site: str) -> list[dict[str, Any]]:
        """Egy oldal bekapcsolt indulóoldalai (_all_lanes): ezek közül választ a böngészés."""
        return [lane for lane in self._all_lanes(site) if lane["on"]]

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
        """A felületnek: az indulóoldalak (a kikapcsoltak is), és mit hoztak eddig."""
        out = []
        for site in BROWSE_ORDER:
            for lane in self._all_lanes(site):
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
                 and self._lane_of(k) == lane and not self._glancing(k)]   # (a friss listás oldal pillantásokban megy)
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
        lanes = (LANE_FAST, LANE_BROWSER) if self.parallel else (LANE_BROWSER,)
        if not self._day_ok(now) and not any(self._session(lane).get("phase") == "work" for lane in lanes):
            start, end = self._data["day"]
            return f"Napközben böngészik ({start}–{end} óra); most nem dolgozik."
        parts = [self._lane_status(now, lane) for lane in lanes
                 if self._rotation(lane) or self._session(lane).get("phase") == "work"]
        parts = [part for n, part in enumerate(parts) if part and part not in parts[:n]] + [self._glance_status(now)]
        return " ".join(part for part in parts if part) or "Most egyik oldal sem esedékes."

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
                                                   or self._glancing(session["site"])
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
        start = self._pick_lane(site, used, not used)   # az indulóoldal (a sor maga: lane)
        if start is None:
            return
        game.check()
        source = self._browse_source()
        rows = self._page_rows(game, start["url"], site, source, extra={"lane": start["id"], "lane_name": start["name"]})
        self._lane_count(start["id"], "pages")
        if game.captcha:
            self._site_blocked(game, game.captcha)
            return
        unknown = self._triage(game, source, rows or [], running)
        if rows and (start["kind"] == "feed" or self.rng.random() < OPEN_CHANCE):
            self._open_random(game, source, rows, unknown)
        if game.captcha:
            self._site_blocked(game, game.captcha)
            return
        pending = self._queue(lane)
        known = {r["url"] for r in pending}
        self._set_queue(lane, (pending + [r for r in unknown if r["url"] not in known])[-MAX_PENDING:])
        if len(self._queue(lane)) >= (TURBO_ROWS if self.turbo else EXPLORE_ROWS) and game.lane is not None:
            self._flush(game)                  # (Turbóban minden oldal után)
        with self._lock:
            self._lane_count(start["id"], "rows", len(rows or []))
            session["lanes"].append(start["name"])
            session["rows"] = int(session.get("rows") or 0) + len(rows or [])
            session["next"] = self.clock() + self.rng.uniform(*STEP_PAUSE.get(site, DEFAULT_STEP))
            self._save()
        game.notes.append(f"{start['name']}: {len(rows or [])} hirdetés")

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
        if wearable(better["text"]):           # a címéből derült ki, hogy ruha vagy cipő: azzal nem kereskedik
            self._mark(row)
            self.tally("clothing", [row])
            if row in unknown:
                unknown.remove(row)
            return
        product = self.book.key(better["text"])
        reference = self._screened(self.book.reference(product[0], exclude=row["url"]), self._want(better)) \
            if product else None
        if reference is None:
            if row in unknown:
                unknown[unknown.index(row)] = better
            return
        if row in unknown:
            unknown.remove(row)
        deal = self._explore_deal(source, better, reference)
        if deal is None:
            self._mark(row)
        elif not reference["sure"]:            # az összehasonlítók leírását még nem olvastuk el (vagy kevés az ár)
            if game.resale > 0:                # a keretbe már nem férő egy későbbi oldalon újra jön
                game.resale -= 1
                game.next([partial(self._resale, game, source, better,
                                   self.book.query(better["text"]) or reference["name"])])
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
        """A munkamenet vége: a maradék ismeretlenek a modellnek (ha legalább MIN_FLUSH, vagy licit is van köztük, vagy
        Turbó van; ha kevesebb, a következő munkamenetre várnak, hogy pár hirdetésért ne töltse be a modellt), néhány régi
        hirdetés újranézése (eladási sebesség); a naplóbejegyzés és a pihenő a kör végén jön (_finish_session), amikor
        már minden eredmény megvan."""
        pending = self._queue(game.browse_lane)
        if game.lane is not None and pending and (self.turbo or len(pending) >= MIN_FLUSH
                                                  or any(r.get("auction") for r in pending)):
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
            problems = session.get("problems") or {}
            if problems:                       # miért kevés (vagy semmi): a letöltés akadályai
                text += "; akadály: " + "; ".join(f"{reason} ({n}×)" for reason, n in
                                                  sorted(problems.items(), key=lambda kv: -kv[1])[:3])
            elif not session.get("pages"):
                text += "; egy oldalt sem töltöttem be (nem volt indulóoldal, vagy nem jutott rá idő)"
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
                problems = session.setdefault("problems", {})   # mi akadályozta (a munkamenet végén a naplóba)
                for error in game.errors:
                    reason = error.split(": ", 1)[-1].rstrip(".")[:100]
                    if reason in problems or len(problems) < 4:
                        problems[reason] = int(problems.get(reason) or 0) + 1
                self._save()
        if getattr(game, "closing", False):
            return self._finish_session(game) or "Böngészés: pihenő."
        notes = [n for n in game.notes if n] + game.errors
        return "Böngészés: " + ("; ".join(notes[:3]) if notes else "nincs mit nézni") + "."

    # --- friss lista: a Vinted legújabb hirdetései pillantásokban (GLANCE_DEFAULT percenként egy oldal) ---

    def _glancing(self, site: str) -> bool:
        """Ezt az oldalt a friss lista pillantásai nézik-e a munkamenetek helyett (GLANCE_SITES, és nincs kikapcsolva)."""
        return site in GLANCE_SITES and int(self._data["browse"].get("glance") or 0) > 0

    def _glance_mark(self, site: str) -> dict[str, Any]:
        """Az oldal friss listájának állapota: next (a következő pillantás), at (a legutóbbi), log (a naplóba gyűjtött
        óra: start, last, looks, rows, fresh, problems)."""
        marks = self._data["sites_state"].setdefault("glance", {})
        mark = marks.get(site)
        if not isinstance(mark, dict):
            mark = marks[site] = {}
        return mark

    def _glance_due(self, now: float) -> str | None:
        """Melyik oldal friss listája esedékes most (a zár a hívónál): a böngészés be van kapcsolva, az oldal be van
        jelölve, van indulóoldala, napközben van, ma nem kért robot-ellenőrzést, és letelt az ideje. None, ha egyik sem."""
        if not (self._data["enabled"] and self._data["browse"]["on"]) or self.browser is None or not self._day_ok(now):
            return None
        state = self._data["sites_state"]
        for site in GLANCE_SITES:
            if not self._glancing(site) or site not in self._data["browse"]["sites"] or site not in self.platforms \
                    or state["blocked"].get(site) == day_of(now) or not self._lanes(site):
                continue
            if now >= float(self._glance_mark(site).get("next") or 0):
                return site
        return None

    def _glance_status(self, now: float) -> str:
        """A friss lista egy mondatban (a felületnek): mikor nézte utoljára, és mikor jön a következő."""
        clock = lambda t: time.strftime("%H:%M", time.localtime(t))  # noqa: E731
        out = []
        for site in GLANCE_SITES:
            if not self._glancing(site) or site not in self._data["browse"]["sites"] or site not in self.platforms \
                    or not self._lanes(site):
                continue
            name, mark = self.platforms[site].name, self._glance_mark(site)
            if self._data["sites_state"]["blocked"].get(site) == day_of(now):
                out.append(f"{name}: ma robot-ellenőrzést kért, holnap folytatom.")
                continue
            last, upcoming = float(mark.get("at") or 0), float(mark.get("next") or 0)
            soon = "hamarosan" if upcoming - now <= 90 else f"legközelebb kb. {clock(upcoming)}"
            out.append(f"{name}: a legújabbakat {clock(last)}-kor néztem, {soon}." if last else
                       f"{name}: a legújabbakat {soon} nézem meg.")
        return " ".join(out)

    def _step_glance(self, game: _Round, running: list[dict[str, Any]], site: str) -> None:
        """Egy pillantás a friss listára: az oldal egyik indulóoldala (a legrégebben nézett; a Vinteden alapból az
        Elektronika, a legújabb elöl) egyszer, ugyanúgy átnézve, mint a böngészésben (_triage: a jelöltek rögtön, az
        ismeretlenek a modellnek kötegben), és OPEN_CHANCE eséllyel egy hirdetés is, ahogy egy ember. A következő
        pillantás a gyakoriság szerint (±20%) jön. A böngészés munkamenetébe nem számít; a naplóba óránként egy
        bejegyzés kerül (_glance_done)."""
        now = self.clock()
        name = self.platforms[site].name
        with self._lock:
            lanes = self._lanes(site)
            if not lanes:
                return
            stats = self._data["lanes"]
            lane = min(lanes, key=lambda item: float((stats.get(item["id"]) or {}).get("glanced") or 0))
            minutes = int(self._data["browse"].get("glance") or GLANCE_DEFAULT)
            mark = self._glance_mark(site)
            mark.update(at=now, next=now + minutes * 60 * self.rng.uniform(*GLANCE_JITTER))
            stats.setdefault(lane["id"], {})["glanced"] = now
            # (új: amit még nem látott; a modellre váró ismeretlen sem új a következő pillantásnál)
            seen = set(self._data["seen"]) | {listing_key(r["url"]) for r in self._queue(LANE_BROWSER)}
            log = mark.setdefault("log", {})
            earlier, at = "", now
            if log.get("start") and now - float(log.get("last") or 0) > 2 * GLANCE_LOG * 60:
                earlier, at = _glance_text(name, log, float(log["last"])), float(log["last"])
                log.clear()                    # (egy hosszabb szünet után, pl. reggel, új óra kezdődik)
            if not log.get("start"):
                log.update(start=now, looks=0, rows=0, fresh=0, problems={})
            self._save()
        if earlier:
            self.journal.add(earlier, at=at)  # az előző (este félbemaradt) óra
        game.check()
        source = self._browse_source()
        rows = self._page_rows(game, lane["url"], site, source, extra={"lane": lane["id"], "lane_name": lane["name"]})
        self._lane_count(lane["id"], "pages")
        if not game.captcha:
            unknown = self._triage(game, source, rows or [], running)
            if rows and self.rng.random() < OPEN_CHANCE:
                self._open_random(game, source, rows, unknown)
            pending = self._queue(LANE_BROWSER)
            known = {r["url"] for r in pending}
            self._set_queue(LANE_BROWSER, (pending + [r for r in unknown if r["url"] not in known])[-MAX_PENDING:])
            if len(self._queue(LANE_BROWSER)) >= (TURBO_ROWS if self.turbo else EXPLORE_ROWS) and game.lane is not None:
                self._flush(game)              # (Turbóban minden pillantás után)
        if game.captcha:
            self._glance_blocked(game, site)
        fresh = [row for row in rows or [] if listing_key(row["url"]) not in seen]
        with self._lock:
            self._lane_count(lane["id"], "rows", len(rows or []))
            log.update(last=now, looks=int(log.get("looks") or 0) + 1, rows=int(log.get("rows") or 0) + len(rows or []),
                       fresh=int(log.get("fresh") or 0) + len(fresh))
            problems = log.setdefault("problems", {})
            for error in game.errors:          # mi akadályozta (a naplóba)
                reason = error.split(": ", 1)[-1].rstrip(".")[:100]
                if reason in problems or len(problems) < 4:
                    problems[reason] = int(problems.get(reason) or 0) + 1
            self._save()
        game.notes.append(f"{lane['name']}: {len(rows or [])} hirdetés, {len(fresh)} új")

    def _glance_blocked(self, game: _Round, site: str) -> None:
        """Robot-ellenőrzés a friss listán: ma ezt az oldalt már nem nézi (nem próbálja megkerülni), és egyszer szól."""
        now = self.clock()
        name = self.platforms[site].name if site in self.platforms else site
        with self._lock:
            self._data["sites_state"]["blocked"][site] = day_of(now)
            self._save()
        self.journal.add(f"{name}: robot-ellenőrzést kért, ma már nem nézem (nem próbálom megkerülni).")
        game.notify("deal", f"{name}: robot-ellenőrzés",
                    f"A {name} robot-ellenőrzést kért, amikor a legújabb hirdetéseit néztem. Ma már nem nézem; ha "
                    "szeretnéd, oldd meg a böngészőablakomban, és holnap folytatom.", actions=[DEALS_ACTION], quiet=True)

    def _glance_done(self, game: _Round, site: str) -> str:
        """A pillantás köre után: ha letelt az óra (GLANCE_LOG), egy naplóbejegyzés róla (hány pillantás, hány
        hirdetés, ebből hány új; a jó találatok a saját bejegyzésükben). Visszaadja a rövid eredményt."""
        now = self.clock()
        name = self.platforms[site].name if site in self.platforms else site
        text = ""
        with self._lock:
            log = self._glance_mark(site).setdefault("log", {})
            if log.get("start") and now - float(log["start"]) >= GLANCE_LOG * 60 - 90:
                text = _glance_text(name, log, now)
                log.clear()
                self._save()
        if text:
            self.journal.add(text)
        notes = [n for n in game.notes if n] + game.errors
        return f"{name}, a legújabbak: " + ("; ".join(notes[:3]) if notes else "nincs mit nézni") + "."

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
        game.flush = partial(self.flush_tips, notify)
        game.browse_lane, game.no_browser = LANE_FAST, True
        game.work.append(partial(self._step_browse, game, running, LANE_FAST))
        game.work.append(partial(self._step_bundles, game))   # a gyors oldalak felmérésre váró csomagjai
        with self._lock:
            tips, carry = bool(self._data["tips"]), bool(self._carry)
        if tips:
            game.work.append(partial(self._step_tips, game))
        if game.lane is not None and self._pending_checks():   # amit a modell még nem nézett meg (foglalt volt)
            game.work.append(partial(self._step_followups, game))
        if carry:                             # az előző kör keretébe nem fért, árkutatásból jött jelöltek: elöl
            game.work.appendleft(partial(self._step_carry, game))
        try:
            game.loop()
        finally:
            self.flush_tips(notify)
            self._release_tips(game)
            self._release_carry(game)
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
            if lane.get("kind") != "tanult" or lane.get("removed"):
                continue                       # (amit töröltél, azt nem tanulja meg újra)
            if lane.get("off"):
                if now - float(lane["off"]) > MUTE_DAYS * 86400:
                    lanes.pop(ident)           # egy hónap múlva újra megtanulhatja
            elif int(lane.get("pages") or 0) >= LEARN_IDLE and not lane.get("deals") and not lane.get("candidates"):
                lane["off"] = now
                notes.append(f"leállítottam a(z) {lane.get('name')} figyelését (nem hozott semmit)")
        if not self._data["browse"].get("learn", True):   # az öntanulást kikapcsoltad: új indulóoldalt nem vesz fel
            if notes:
                self.journal.add("Tanultam: " + "; ".join(notes) + ".", at=now)
            return
        learned = [lane for lane in lanes.values() if lane.get("kind") == "tanult" and not lane.get("off")
                   and not lane.get("removed")]
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
                    or site not in self._data["browse"]["sites"] or wearable(self.book.name(key)):
                continue
            platform, name = self.platforms[site], self.book.name(key)
            ident = f"tanult:{key}"
            lanes[ident] = {"kind": "tanult", "site": site, "key": key, "at": now, "name": f"{platform.name}: {name}",
                            "url": newest_first(platform.search_url.format(q=urllib.parse.quote(name)), site)}
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

    def _inspect(self, deal: dict[str, Any], page: dict[str, Any] | None = None, game: _Round | None = None,
                 photos: int = CHECK_PHOTOS, defects: bool = True, title: bool = False, later: bool = False,
                 urgent: bool = False) -> str:
        """A jelölt hirdetés oldala (ha még nincs meg, megnyitja), modell nélkül, csak olvasva. Visszaadja, miért esik
        ki („” = maradhat, „captcha”: robot-ellenőrzés, ilyenkor maradhat, de nem néztük meg). A figyelmeztetéseket, a
        leírás elejét és az első fotók címét (photos) a jelölthöz teszi (a modellkérdéshez). defects=False: a hibára
        utaló szó nem ejti ki (egy csomagban lehet pár hibás darab; azt a modell méri fel). title: a hirdetés címe az
        oldal címéből (az árbázisból jövő alkuképes hirdetésnél csak a termék neve van meg). later: ha az oldal csak az
        óránkénti keret miatt nem nyílt meg (_Later), azt a hívó kapja (az alkunál és a csomagnál az oldal nélkül nincs
        értelme: később újra); különben a friss jelölt figyelmeztetéssel megy tovább (az idő a fontosabb)."""
        if page is None:
            try:
                page = self._look(deal["url"], deal.get("platform", ""), game=game, urgent=urgent)
            except _Later:
                if later:
                    raise
                deal.setdefault("warnings", []).append("a hirdetés oldalát most nem nyitottam meg (elfogyott az oldal "
                                                       "óránkénti kerete): nézd meg, megvan-e még")
                return ""
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
        heading = re.split(r"\s+[-|–]\s+", " ".join(str(page.get("title") or "").split()))[0][:160]
        if title and len(heading) >= 4:
            deal["title"] = heading
        deal["images"] = [str(url) for url in page.get("images") or []][:photos]
        if page.get("seller"):                 # az eladó többi hirdetése (egy jó találatnál: seller_bundle)
            deal["seller_url"] = str(page["seller"])[:300]
        # az adatlapot megnéztük: az állapot a leírásából, kulcsszavakkal (a modell válasza ezt pontosítja: _confirm)
        deal["page_checked"] = True
        cond = conditions.from_text(self._condition_source(deal))
        deal["condition"] = conditions.compact(cond)
        # eltárolva is (allapotok.json): egy későbbi összehasonlításnál ő is összehasonlító (egy olcsó, hibátlan
        # példány versenytárs, _cheap_label), és nem kell újra elolvasni
        self._vet_note(deal["url"], self._condition_source(deal))
        for pattern, why in ((_SCAM_RE, "átverésre utaló jel a hirdetésben (előre fizetés, külföldről küldi…)"),
                             (_OFFSITE_RE, "az oldalon kívüli elérhetőséget ad meg (WhatsApp, Telegram, e-mail…)"),
                             (_NO_REVIEWS_RE, "az eladónak még nincs értékelése")):
            if pattern.search(text):
                deal["suspect"] = why
                return "gyanús"
        # kiesik az alkatrésznek, üres doboznak eladott, a nem működő, és amelyiknél hibára utaló szó van, de nem tudni,
        # mi a hiba; a felismert kijelzőhiba, hiányzó töltő tény, amihez az ár igazodik (_reprice)
        if defects and (_PARTS_ONLY_RE.search(text) or cond["function"]["state"] == "hibás" or (
                (_PARTS_RE.search(text) or _DEFECT_RE.search(text)) and not conditions.issues(cond))):
            return "hibás"
        return ""

    @staticmethod
    def _condition_source(deal: dict[str, Any]) -> str:
        """A jelölt szövege az állapotfelismeréshez: cím, a kártya szövege és az adatlap leírása."""
        return " ".join(str(deal.get(k) or "") for k in ("title", "text", "details"))

    def _confirm(self, search: dict[str, Any], deal: dict[str, Any]) -> bool | None:
        """Egy rövid modellkérdés egy jelöltről, a hirdetés fotóival együtt (ha le tudta tölteni őket). None: nem
        sikerült megkérdezni (ilyenkor ellenőrizetlen marad); False: nem ez, vagy gyanús (akkor a deal["suspect"]-ben,
        miért). Az állapotot (kijelző, akkumulátor, működés, tartozékok, garancia) bizonyítékkal a deal["condition"]-be
        teszi: a modell idézetei ellenőrizve, a kulcsszavakéval összevetve (a rosszabb győz)."""
        photos = self._photos(deal)
        text = (f"Keresett termék: {search['query']}\nHirdetés ({deal['site']}): {deal['text']}\nÁr: "
                f"{format_price(deal['price'])} (a szokásos ára {format_price(deal['median'])})"
                + (f"\nA hirdetés oldala: {deal['details'][:1200]}" if deal.get("details") else "")
                + (f"\nA hirdetés fotói ({len(photos)} db) mellékelve." if photos else ""))
        messages = [{"role": "system", "content": CHECK_SYSTEM},
                    {"role": "user", "content": text, **({"images": photos} if photos else {})}]
        try:
            data = self.llm.chat_json(messages, CHECK_SCHEMA, temperature=0.0, max_tokens=CHECK_MAX_TOKENS)
        except Exception:  # noqa: BLE001 - a modell hibája ne állítsa le a keresést
            if not photos:
                return None
            try:                               # pl. a modell nem tud képet: fotók nélkül
                messages[1] = {"role": "user", "content": text.rsplit("\nA hirdetés fotói", 1)[0]}
                data = self.llm.chat_json(messages, CHECK_SCHEMA, temperature=0.0, max_tokens=CHECK_MAX_TOKENS)
                photos = []
            except Exception:  # noqa: BLE001
                return None
        deal["photos_checked"] = len(photos) or None
        deal["checked"] = True
        deal["note"] = str(data.get("reason") or "")[:200]
        source = self._condition_source(deal)
        deal["condition"] = conditions.compact(conditions.merge(conditions.from_model(data.get("condition"), source),
                                                                conditions.from_text(source)))
        if data.get("suspicious"):
            deal["suspect"] = "a modell szerint gyanús" + (f": {deal['note']}" if deal["note"] else "")
            return False
        return bool(data.get("ok"))

    def _photos(self, deal: dict[str, Any], limit: int = CHECK_PHOTOS) -> list[str]:
        """A hirdetés első fotói a modellnek (letöltve, kicsinyítve); ami nem sikerül, kimarad."""
        out = []
        for url in (deal.get("images") or [])[:limit]:
            data = self.image_get(url) if self.image_get is not None else None
            if not data:
                continue
            try:
                out.append(vision_jpeg(data, CHECK_SIDE))
            except ItemError:
                continue
        return out

    # --- gyors jelzés, állapot szerinti újraszámolás, ellenőrzött vagy ellenőrizetlen ---

    def _ping(self, deal: dict[str, Any], notify: Callable[..., Any] | None) -> bool:
        """Gyors jelzés egy ígéretes új hirdetésről, még az ellenőrzés (adatlap, állapot, újraszámolt haszon) előtt: egy
        rövid pittyenés (notice.pending), nem a sürgős riasztás; az csak az ellenőrzött ajánlatról jön. Egy hirdetésről
        PING_TTL-en belül egyszer."""
        if notify is None or not deal.get("url"):
            return False
        if specs.needs_detail(specs.extract(str(deal.get("title") or deal.get("text") or ""))):
            return False                       # pl. laptop- vagy asztali memória: csak a leírása után tudni, mennyit ér
        key, now = listing_key(deal["url"]), self.clock()
        with self._lock:
            self._pinged = {k: at for k, at in self._pinged.items() if now - at < PING_TTL}
            if key in self._pinged:
                deal["pinged"] = True
                return False
            self._pinged[key] = now
        deal["pinged"] = True
        title = str(deal.get("title") or deal["text"])
        text = (f"{deal['site']}: „{title[:80]}” {format_price(deal['price'])}, a hasonlók kb. "
                f"{format_price(deal['median'])}-ért mennek. Ígéretes új hirdetés, még nem ellenőrzött (csak a címe "
                "és az ára alapján); ellenőrzés folyamatban (adatlap, állapot, költségek, újraszámolt haszon): ha "
                "megéri, külön szólok; addig ne vedd meg látatlanban.")
        try:
            notify("deal", f"Ígéretes új hirdetés: {title[:50]}", text, url=deal["url"], pending=deal["id"])
        except Exception:  # noqa: BLE001 - a jelzés hibája ne állítsa meg az ellenőrzést
            pass
        return True

    def _top_price(self, deal: dict[str, Any], reference: int, low: int | None) -> int | None:
        """A legmagasabb vételár (a vevő díja nélkül, de azzal együtt számolva), ami ennél a jelöltnél még megéri: a
        keresésed szabályai szerint (highest_ok, az ársáv nélkül), vagy a felfedező küszöbével (explore_top, a jelölt
        floor-ja)."""
        platform = str(deal.get("platform") or "")
        with self._lock:
            search = next((json.loads(json.dumps(s)) for s in self._data["searches"]
                           if s.get("id") == deal.get("search")), None)
        if search is not None:
            return highest_ok({**search, "min_price": None, "max_price": None}, reference, platform)
        return before_fee(platform, explore_top(reference, int(deal.get("floor") or WATCH_PROFIT), low))

    @staticmethod
    def _unpriced_text(unpriced: list[str]) -> str:
        repairs = [issue for issue in unpriced if issue in conditions.REPAIRS]
        parts = [issue for issue in unpriced if issue not in conditions.REPAIRS]
        bits = []
        if repairs:
            bits.append(f"{conditions.issue_text(repairs)}, és nincs hozzá hasonló hibájú összehasonlító vagy megadott "
                        "javítási költség")
        if parts:
            bits.append(f"{conditions.issue_text(parts)}, és nincs hozzá hasonló hiányú összehasonlító vagy megadott "
                        "pótlási költség")
        return "a leírása szerint " + "; ".join(bits) + " (Pótlás és javítás)"

    def _reprice(self, deal: dict[str, Any], price: int | None = None) -> tuple[str, str]:
        """Az állapothoz igazított ár és a teljes költség (_recount), utána a becslés megbízhatósága, sávja, és a
        javasolt legmagasabb vételár (_valuation): minden jelöltnél ugyanígy (a sima találat, az alku, az utólagos
        ellenőrzés)."""
        state, why = self._recount(deal, price)
        if state != "hibás":
            self._valuation(deal)
        return state, why

    def _valuation(self, deal: dict[str, Any]) -> None:
        """A becslés a találathoz: a megbízhatósága (reliability), a gyors eladási ár sávja (resale_range) és vele a
        haszoné (profit_range), hogyan adná el (sale_mode: ahogy van, hasonló hibájúként, vagy javítva/pótolva, a
        költséggel; semmi kétszer), és mi nincs megadva a költségek közül (fees_unset)."""
        rel = reliability(deal, self.clock())
        deal["reliability"] = rel
        sale = int(deal.get("resale") or 0)
        if sale > 0 and not deal.get("auction"):
            low, high = sale_range(sale, int(deal.get("median") or sale), rel["level"])
            profit = int(deal.get("profit") or 0)
            deal["resale_range"], deal["profit_range"] = [low, high], [profit - (sale - low), profit + (high - sale)]
        unset = self._fees_unset()
        if unset:
            deal["fees_unset"] = unset
        else:
            deal.pop("fees_unset", None)
        parts = deal.get("cost_parts") or []
        if deal.get("cond_band"):
            deal["sale_mode"] = (f"hibásan, ahogy van: a hasonló hibájúak árán ({', '.join(deal['cond_band']['issues'])}"
                                 "; a hibát nem javítod, nincs levonva még egyszer)")
        elif any(part.get("repair") for part in parts):
            deal["sale_mode"] = "javítva: a hibátlanok árán, a javítás költségével"
        elif any(str(part.get("what") or "").endswith("pótlása") for part in parts):
            deal["sale_mode"] = "pótolva: a hibátlanok árán, a pótlás költségével"
        elif deal.get("unpriced"):
            deal["sale_mode"] = (f"nem tudni: {conditions.issue_text(deal['unpriced'])}, és nincs hozzá költség vagy "
                                 "hasonló hibájú összehasonlító")
        else:
            deal["sale_mode"] = "ahogy van"

    def _recount(self, deal: dict[str, Any], price: int | None = None) -> tuple[str, str]:
        """Az adatlap (és a modell) szerinti állapothoz igazított ár és teljes költség: („ok” | „hibás” | „bizonytalan” |
        „nem éri meg”, miért). A nem működő hibás. A többi hibához (kijelző, akkumulátor, hiányzó töltő…) alap kell: ha
        legalább conditions.MATCH_MIN összehasonlító a leírása szerint ugyanígy hibás (best_band), az ő áruk a mérce (az
        a hiba már benne van az árban, nem vonja le még egyszer); ami így nincs lefedve, ahhoz a megadott pótlási vagy
        javítási költség (_cost) vonódik le. Ha egyik sincs, bizonytalan (ellenőrizetlen marad). A doboz hiánya
        önmagában nem hiba. A haszonban a posta, a vevővédelmi díj (Vinted), a pótlás és a javítás is benne van; ha így
        már nem éri meg (a keresésed szabályai, vagy a felfedező küszöbe szerint), „nem éri meg”. price: a vételár, amire
        számol (az alkunál az ajánlott összeg); alapból az ajánlat vagy a kért ár. Ha egy hibához nincs alap
        (bizonytalan), a haszon a többi költséggel így is kiszámolva marad, és az unpriced mondja meg, mi hiányzik belőle
        (nem nulla, hanem ismeretlen)."""
        cond = deal.get("condition") or {}
        found = list(cond.get("issues") or conditions.issues(cond)) if cond else []
        if cond:
            deal["condition_text"] = conditions.describe(cond)
        if "működés" in found:
            return "hibás", "a leírása szerint nem működik"
        given = price is not None              # (egy megadott vételárra mindig végigszámol)
        price = int(price or deal.get("offer") or deal["price"])
        deal.pop("unpriced", None)
        # (egy utólagos modellellenőrzés után újra számol: a hibátlanok ára és a díjtételek az eredetiek)
        rerun = "clean" in deal
        clean = deal.setdefault("clean", {"median": int(deal["median"]), "low": deal.get("low")})
        clean_ref, low = int(clean["median"]), clean.get("low")
        deal["cost_parts"] = [part for part in deal.get("cost_parts") or [] if part.get("what") in ("posta",
                                                                                                    "vevővédelmi díj")]
        deal.pop("cond_band", None)
        covered, reference, band_low, n = conditions.best_band(found, deal.get("refs") or [], clean_ref, low)
        parts, unpriced = [], []
        for issue in found:
            if issue in covered:
                continue
            amount = self._cost(issue, deal)
            if amount is None:
                unpriced.append(issue)
            else:
                repair = issue in conditions.REPAIRS
                parts.append({"what": f"{issue} {'javítása' if repair else 'pótlása'}", "amount": amount,
                              **({"repair": True} if repair else {})})
        extra = int(deal.get("fee") or 0) + sum(part["amount"] for part in parts)
        if unpriced:
            # a hiányzó költség nem nulla: a haszon csak a többivel, és látszik, mi nincs benne (a legmagasabb vételár
            # sem tudható)
            sale = safe_sale(clean_ref, low)
            selling = self._sale_parts(sale)
            extra += sum(part["amount"] for part in selling)
            deal["unpriced"] = unpriced
            deal["cost_parts"] = list(deal.get("cost_parts") or []) + parts + selling
            deal["costs"] = SHIPPING + extra
            deal["profit"] = sale - price - SHIPPING - extra
            deal.pop("max_buy", None)
            return "bizonytalan", self._unpriced_text(unpriced)
        selling = self._sale_parts(safe_sale(clean_ref, low))
        if not n and not extra and not selling and not rerun and not given:
            top = self._top_price(deal, clean_ref, low)
            if top is not None and not deal.get("auction"):
                deal["max_buy"] = max(0, top)  # (nincs más költség a postán és a díjon túl: az a határban van)
            return "ok", ""
        if n:                                  # a hasonló hibájúak ára a mérce
            deal["cond_band"] = {"issues": sorted(covered), "n": n, "price": reference, "clean": clean_ref}
            deal["median"], deal["low"] = reference, band_low
            low = band_low
        else:
            deal["median"], deal["low"] = clean_ref, low
        sale = safe_sale(reference, low)
        selling = self._sale_parts(sale)       # az eladás költségei az állapot szerinti eladási árra
        extra += sum(part["amount"] for part in selling)
        deal["resale"] = sale
        deal["cost_parts"] = list(deal.get("cost_parts") or []) + parts + selling
        deal["costs"] = SHIPPING + extra
        top = self._top_price(deal, reference, low)
        after = extra - int(deal.get("fee") or 0)   # a pótlás, a javítás és az eladás költségei (a díj a határban van)
        if deal.get("auction"):
            repairs = after
            bid = self._capped(top - repairs) if top else 0
            deal["max_bid"] = bid
            deal["profit"] = sale - price - SHIPPING - extra
            if bid <= price:
                return "nem éri meg", (f"az állapota és a költségek után legfeljebb {format_price(max(bid, 0))}-ig "
                                       "érdemes licitálni, és már ennél többen tart")
            return "ok", ""
        deal["profit"] = sale - price - SHIPPING - extra
        deal["margin"] = reference - price
        if top is not None:
            deal["max_buy"] = max(0, top - after)   # ennyiért még megéri, minden költséggel
        else:
            deal.pop("max_buy", None)
        if top is None or price + after > top:   # (a díj a határban már benne van)
            return "nem éri meg", (f"az állapota és a költségek ({format_price(SHIPPING + extra)}) után kb. "
                                   f"{format_price(deal['profit'])} haszon marad, ez már kevés")
        return "ok", ""

    def _respec(self, deal: dict[str, Any]) -> tuple[str, str, str] | None:
        """Az adatlap után a jelölt termékadatai a leírásával együtt (specs.extract, a leírásból csak ami biztonsággal
        kiolvasható: specs.DETAIL_TRUSTED). Ha a leírásból kiderül valami, ami a címből nem (pl. laptop-memória, SO-DIMM:
        a cikkszámáról, a „laptopba” szóból; zárolt telefon; csonkított videókártya), a szokásos árat ehhez igazítja: a
        laptop-memóriáét az árbázis laptop-memóriáiból (specs.keyed), és az összehasonlítókat a teljes termékadataihoz méri
        (_screened). Ha egy kiolvasandó adat (specs.DETAIL_ATTRS: a memória kivitele) a leírásból sem derül ki, az olcsóbb
        fajtához méri, ha azt elég biztosan ismeri; különben ellenőrizetlen (Érdemes megnézni), az okkal. Ha a leírása
        szerint nem eredeti (másolat, dummy), hibás. A felmért keresésed (a szokásos árát te hagytad jóvá), a licit, az alku
        és a csomag marad, ahogy volt. Visszaadja a végső állapotot (_verdict: állapot, miért, hold), ha emiatt kiesik vagy
        ellenőrizetlen lesz; None, ha marad (és közben a szokásos ára, a mihez mérte frissülhetett: a haszon, a költségek
        és a legmagasabb vételár utána, a _reprice-ban számolódik újra)."""
        if deal.get("auction") or deal.get("haggle") or deal.get("bundle") or (deal.get("basis") or {}).get("given"):
            return None
        title = str(deal.get("title") or deal.get("text") or "")
        details = str(deal.get("details") or "")
        copy = parts.not_original(details) if details else ""
        if copy:
            return "hibás", f"a leírása szerint nem eredeti („{copy}”)", ""
        first = specs.extract(title)
        full = specs.extract(title, details, only=specs.DETAIL_TRUSTED)
        news, open_ = specs.learned(first, full), specs.needs_detail(full)
        if not news and not open_:
            return None
        if "lock" in news:                     # zárolt telefon: nem eladható működő termék (nincs hozzá mérce)
            return "hibás", f"a leírása szerint zárolt („{full['attrs']['lock']['evidence']}”)", ""
        found = self.book.key(title)
        base = found[0] if found else str(deal.get("product_key") or "")
        if not base:
            return None
        own = {"t": title, "p": int(deal["price"]), "v": str(deal.get("seller") or "")}
        want = {**full, "own": own}
        key = specs.keyed(base, full)
        reference = self._screened(self.book.reference(key, exclude=deal["url"]), want)
        what = specs.facts(full, news)
        if open_ and full.get("category") == "ram":
            # a leírásból sem derül ki, laptop- vagy asztali memória-e: az olcsóbb fajtához mér (a kivitel nélküli
            # árak között mindkettő lehet), ha a laptop-memória árát elég biztosan ismeri
            laptop = {**full, "attrs": {**full.get("attrs", {}),
                                        "form": {"value": parts.SODIMM, "source": "feltevés", "evidence": ""}}}
            laptop_key = specs.keyed(base, laptop)
            other = self._screened(self.book.reference(laptop_key, exclude=deal["url"]), {**laptop, "own": own})
            if other is None or not other.get("sure"):
                return "ellenőrizetlen", ("nem derül ki, hogy laptop- (SO-DIMM) vagy asztali memória-e, és a "
                                          "laptop-memória árát még nem ismerem elég biztosan; nézd meg a fotóján (a "
                                          "laptop-modul fele olyan hosszú), vagy kérdezd meg az eladót"), "leírás"
            cheaper = "a laptop-memóriák" if reference is None or int(other["price"]) < int(reference["price"]) \
                else "az asztali memóriák"
            if cheaper == "a laptop-memóriák":
                reference, key = other, laptop_key
            what = f"nem derül ki, laptop- vagy asztali memória-e; {cheaper} (az olcsóbbak)"
            reference = {**reference, "variant_note": f"{what} árához mértem"}
        elif open_:
            return "ellenőrizetlen", f"a hirdetésből nem derül ki: {', '.join(open_)}", "leírás"
        if reference is None:
            return "ellenőrizetlen", (f"a leírása szerint {what}: ehhez még kevés a hasonló ár" if what else
                                      "kevés a hasonló ár"), ""
        price = int(deal.get("offer") or deal["price"])
        value = self._valued(price, reference, str(deal.get("query") or reference.get("name") or title),
                             int(deal.get("floor") or WATCH_PROFIT))
        if not value["cheap"]:
            if open_:
                return "nem éri meg", (f"{what} kb. {format_price(value['reference'])}-ért mennek, így nem éri "
                                       "meg"), ""
            return "nem éri meg", (f"a leírása szerint {what}: a hasonlók kb. {format_price(value['reference'])}-ért "
                                   "mennek, így nem éri meg"), ""
        if not value["sure"]:
            return "ellenőrizetlen", (reference.get("unsure_why") or reference.get("stale") or
                                      f"{'' if open_ else 'a leírása szerint '}{what}: ehhez kevés a biztos "
                                      "összehasonlító ár"), ""
        # marad: a szokásos ára és a mihez mérte a teljes termékadataihoz igazodik
        deal.update(median=int(value["reference"]), margin=int(value["reference"]) - int(deal["price"]),
                    low=value.get("low"), similar=value.get("n"), sites=self._site_names(value.get("sites")) or None,
                    spread=value.get("spread"), sold=value.get("sold"), band=value.get("band"),
                    basis=value.get("basis"), refs=value.get("refs"), product_key=self.book.canon(key), respec_key=key)
        for name in ("identity", "variant_note", "clean_n", "competitor"):
            if value.get(name) is not None:
                deal[name] = value[name]
            else:
                deal.pop(name, None)
        if what and isinstance(deal.get("basis"), dict):
            note = f"{what[0].upper()}{what[1:]} árához mértem." if open_ else f"A leírása szerint {what}: ehhez mértem."
            deal["basis"] = {**deal["basis"], "note": note}
        for name in [k for k, v in deal.items() if v is None]:
            deal.pop(name)
        return None

    def _verdict(self, deal: dict[str, Any], ok: bool | None, fast: bool = False) -> tuple[str, str, str]:
        """A jelölt végső minősítése az adatlap és a modellkérdés után: (állapot, miért, hold). Az állapot
        „ellenőrzött” (sürgősen szól róla), „ellenőrizetlen” (az Érdemes megnézni listába: _unverified), „hibás” vagy
        „nem éri meg” (kiesik). Ellenőrzött csak az, amelyiknek az adatlapja megnyílt, van rajta leírás (legalább
        MIN_DETAILS jel), a modell átnézte, és az állapota szerint újraszámolt haszon is megéri (_reprice); a magas
        számított haszon magában kevés. hold: az ellenőrizetlenség oka magától nem múlik el („állapot”: csak ha költséget
        adsz meg; „leírás”: alig van leírás), különben később újra megpróbálja. „modell nélkül”: minden rendben, csak a
        modell nem nézte meg (foglalt, nem válaszolt, vagy nincs): erről is sürgősen szól (_unchecked)."""
        if not deal.get("page_checked"):
            warnings = deal.get("warnings") or []
            return "ellenőrizetlen", warnings[-1] if warnings else "az adatlapját nem tudtam megnézni", ""
        respec = self._respec(deal)            # a leírásából kiderülhet, hogy más a terméke (pl. laptop-memória)
        if respec is not None:
            return respec
        state, why = self._reprice(deal)
        if state == "bizonytalan":
            return "ellenőrizetlen", why, "állapot"
        if state != "ok":
            return state, why, ""
        if len(" ".join(str(deal.get("details") or "").split())) < MIN_DETAILS:
            return "ellenőrizetlen", "az adatlapján alig van leírás", "leírás"
        if ok is None:                         # az adatlap és az állapot rendben, csak a modell nem nézte meg
            return "modell nélkül", ("nincs beállítva modell" if self.llm is None else
                                     "gyors riasztás: a modell utána nézi meg" if fast else
                                     "a modell most foglalt volt vagy nem válaszolt"), ""
        return "ellenőrzött", "", ""

    def _unchecked(self, deal: dict[str, Any], why: str) -> None:
        """Az adatlap és az újraszámolás rendben, csak a modell nem nézte meg (why: foglalt volt, nem válaszolt, vagy
        nincs): így is sürgősen szól (ellenőrizetlenként, legfeljebb „jó”), és ha van modell, utólag megnézeti
        (followups): ehhez a leírás eleje és a fotók címe a találatban marad (check)."""
        deal["verified"] = False
        deal["unchecked"] = why
        if self.llm is not None:
            deal["model_pending"] = True
            deal["check"] = {"details": str(deal.get("details") or "")[:1500],
                             "images": [str(url) for url in deal.get("images") or []][:CHECK_PHOTOS], "tries": 0}

    def _pending_checks(self) -> list[str]:
        """Az utólag megnézendő találatok (a modell nem nézte meg őket), a legfrissebb elöl; a FOLLOWUP_TTL-nél régebbiek
        és a megvettek lekerülnek a sorról."""
        now = self.clock()
        out, changed = [], False
        with self._lock:
            for deal in self._data["deals"]:
                if not deal.get("model_pending"):
                    continue
                if deal.get("bought") or now - float(deal.get("at") or 0) > FOLLOWUP_TTL:
                    deal.pop("model_pending", None)
                    deal.pop("check", None)
                    changed = True
                elif deal["id"] not in self._followup_busy:
                    out.append(deal["id"])
            if changed:
                self._save()
        return out

    def _followup_check(self, deal_id: str) -> dict[str, Any] | None:
        """Egy találat utólagos modellkérdése (a mentett leírással és fotókkal, _confirm). {ok, deal}; None, ha közben
        eltűnt (megvetted, elrejtetted)."""
        with self._lock:
            stored = next((d for d in self._data["deals"] if d.get("id") == deal_id and d.get("model_pending")), None)
            if stored is None or deal_id in self._followup_busy:
                return None
            self._followup_busy.add(deal_id)
            deal = json.loads(json.dumps(stored))
        check = deal.pop("check", None) or {}
        deal.update(details=check.get("details") or "", images=check.get("images") or [])
        try:
            ok = self._confirm({"id": deal.get("search") or "utólag", "query": deal.get("query") or ""}, deal)
        except Exception:  # noqa: BLE001 - egy hiba miatt ne álljon le a sor
            ok = None
        return {"ok": ok, "deal": deal, "tries": int(check.get("tries") or 0) + 1}

    def listen(self, callback: Callable[[str, dict[str, Any] | None], None]) -> None:
        """callback(találat azonosító, a találat mostani állapota; None, ha levette): ha egy találat utólag megváltozik
        (az utólagos modellellenőrzés után: ellenőrzött lett, mégsem jó, levette), hogy minden megjelenítés (az azonnali
        figyelő friss hirdetései) ugyanazt mutassa."""
        self._listeners.append(callback)

    def _changed(self, deal_id: str, deal: dict[str, Any] | None) -> None:
        for callback in list(self._listeners):
            try:
                callback(deal_id, json.loads(json.dumps(deal)) if deal is not None else None)
            except Exception:  # noqa: BLE001 - egy megjelenítés hibája ne állítsa meg a többit
                pass

    def _followup_apply(self, deal_id: str, result: dict[str, Any] | None, notify: Callable[..., Any]) -> None:
        """Az utólagos modellkérdés eredménye: ha rendben (és az állapota szerint is megéri), ellenőrzött lesz; ha a
        modell szerint nem ez, gyanús, vagy az állapota szerint mégsem éri meg, a találatba beírja, és szól („Mégsem: …”,
        nem riaszt): ez a korábbi (csengető) értesítés egyszeri helyesbítése (corrects: a telefon leállítja a
        csengetést, az Értesítések között a régi áthúzva). A változás minden megjelenítéshez eljut (listen). Ha nem
        válaszolt, FOLLOWUP_TRIES-ig újra próbálja."""
        try:
            if result is None:
                return
            deal, ok = result["deal"], result["ok"]
            title = str(deal.get("title") or deal["text"])[:60]
            changes: dict[str, Any] = {}
            drop = ["check"]
            message = ""
            if ok is None:
                if result["tries"] < FOLLOWUP_TRIES:
                    changes["check"] = {"details": deal.get("details") or "", "images": deal.get("images") or [],
                                        "tries": result["tries"]}
                    drop = []
                else:
                    drop.append("model_pending")
                    self.journal.add(f"A modell többszöri próbára sem nézte meg: „{title}” (az ellenőrizetlen jelzés "
                                     "marad).")
            else:
                drop.append("model_pending")
                state, why = ("hibás", deal.get("suspect") or f"a modell szerint nem ez: {deal.get('note') or '?'}") \
                    if ok is False else self._reprice(deal)
                changes.update({k: deal[k] for k in ("condition", "condition_text", "note", "photos_checked", "checked",
                                                    "median", "low", "profit", "resale", "costs", "cost_parts",
                                                    "cond_band", "clean", "max_bid", "margin")
                                if deal.get(k) is not None})
                if state == "ok":
                    changes.update(verified=True)
                    drop.append("unchecked")
                    changes["safety"] = assess({**deal, **changes})
                elif ok is False and deal.get("suspect"):
                    # a modell szerint gyanús: lekerül a találatok közül (mint egy gyanús hirdetés), és szól róla
                    message = (f"{deal.get('site') or ''}: „{title}” {format_price(deal['price'])}. Utólag a modell is "
                               f"megnézte: {deal['suspect']}. Ne vedd meg; levettem a találatok közül.")
                    with self._lock:
                        gone = next((d for d in self._data["deals"] if d.get("id") == deal_id), None)
                        if gone is not None and gone.get("corrected"):
                            message = ""        # már helyesbítette (egyszer szól)
                        self._data["deals"] = [d for d in self._data["deals"] if d.get("id") != deal_id]
                        self._save()
                    self._mark(deal)
                    self.tally("suspicious", [deal])
                    self.journal.add(f"Gyanús, utólag levettem: „{title}” {format_price(deal['price'])} "
                                     f"({deal.get('site') or '?'}) – {deal['suspect']}.")
                    self._changed(deal_id, None)
                    changes, drop = {}, []
                else:
                    reason = why if state != "bizonytalan" else f"az állapota miatt bizonytalan: {why}"
                    changes["model_no"] = reason[:200]
                    changes["warnings"] = list(deal.get("warnings") or []) + [f"utólag: {reason}"[:200]]
                    message = (f"{deal.get('site') or ''}: „{title}” {format_price(deal['price'])}. Utólag a modell is "
                               f"megnézte: {reason}. Ne vedd meg látatlanban.")
                    self.journal.add(f"Utólag mégsem jó: „{title}” – {reason}.")
            with self._lock:
                stored = next((d for d in self._data["deals"] if d.get("id") == deal_id), None)
                if stored is not None:         # (a gyanúsat már levette; közben el is rejthetted)
                    if message and stored.get("corrected"):
                        message = ""           # a helyesbítés egyszer jön
                    stored.update(changes)
                    for key in drop:
                        stored.pop(key, None)
                    if message:
                        stored["corrected"] = True
                    self._save()
                snapshot = json.loads(json.dumps(stored)) if stored is not None else None
            if snapshot is not None and (changes or drop):
                self._changed(deal_id, snapshot)
            if message:
                try:
                    notify("deal", f"Mégsem: {title}", message, url=deal.get("url"), actions=[deal_action(deal_id)],
                           corrects=deal_id)
                except Exception:  # noqa: BLE001
                    pass
        finally:
            with self._lock:
                self._followup_busy.discard(deal_id)

    def followups(self, can_ask: Callable[[], bool], notify: Callable[..., Any], limit: int = FOLLOWUP_BATCH) -> int:
        """Az utólag megnézendő találatok (_unchecked) modellkérdése, amíg a modell kérdezhető (can_ask), egyszerre
        legfeljebb limit; az azonnali figyelő értékelő szála hívja, amikor ráér. Visszaadja, hányat nézett meg."""
        done = 0
        for deal_id in self._pending_checks()[:max(0, limit)]:
            if not can_ask():
                break
            self._followup_apply(deal_id, self._followup_check(deal_id), notify)
            done += 1
        return done

    def _step_followups(self, game: _Round) -> None:
        """A kör modell-sorában: az utólag megnézendő találatok (legfeljebb FOLLOWUP_BATCH)."""
        if game.lane is None:
            return
        for deal_id in self._pending_checks()[:FOLLOWUP_BATCH]:
            game.ask(partial(self._followup_check, deal_id),
                     lambda result, deal_id=deal_id: self._followup_apply(deal_id, result, game.notify))

    def _drop_checked(self, deal: dict[str, Any], state: str, why: str) -> None:
        """Az ellenőrzés után kiesett (az állapota szerint nem működik, vagy az újraszámolt haszon kevés): nem szól róla,
        látottnak jegyzi, és a naplóba írja, miért."""
        self._mark(deal)
        self.tally("defect" if state == "hibás" else "not_cheap", [deal])
        self.journal.add(f"Az ellenőrzés után kimaradt: „{(deal.get('title') or deal['text'])[:60]}” "
                         f"{format_price(deal['price'])} ({deal.get('site') or deal.get('platform') or '?'}) – {why}.")

    def _unverified(self, deal: dict[str, Any], why: str, hold: str) -> None:
        """Ígéretes, de nem ellenőrzött ajánlat: nem jön róla sürgős értesítés, hanem az Érdemes megnézni listába kerül
        az okkal (ha már volt róla tipp, az frissül, a felvétele ideje marad). hold: lásd _verdict; ha nincs, TIP_RETRY
        múlva egy kör újra megpróbálja (_step_tips). Ha a gyors jelzés már szólt róla, nem pittyen újra."""
        self.tally("unverified", [deal])
        title = str(deal.get("title") or deal["text"])[:120]
        self.journal.add(f"Ellenőrizetlen, nem szóltam sürgősen: „{title[:60]}” {format_price(deal['price'])} "
                         f"({deal.get('site') or deal.get('platform') or '?'}) – {why}; az Érdemes megnézni listába "
                         "tettem.")
        if not deal.get("url") or int(deal.get("profit") or 0) <= 0:
            return
        now, ident = self.clock(), listing_key(deal["url"])
        product = self.book.key(str(deal.get("query") or ""))
        cond = deal.get("condition") or {}
        tip = {"id": secrets.token_hex(5), "url": deal["url"], "title": title, "price": int(deal["price"]),
               "site": deal.get("site") or "", "platform": str(deal.get("platform") or ""), "query": deal["query"],
               # (ha a leírása szerint más a terméke, pl. laptop-memória: _respec, azzal)
               "key": deal.get("respec_key") or (product[0] if product else ""), "usual": int(deal["median"]),
               "n": int(deal.get("similar") or 0), "sites": list(deal.get("sites") or []),
               "profit": int(deal["profit"]), "at": now, "checked": now, "watch": str(deal.get("search") or ""),
               "min_profit": int(deal.get("floor") or WATCH_PROFIT), "why": why, "unverified": True,
               **({"hold": hold} if hold else {}), **({"auction": True} if deal.get("auction") else {}),
               **({"haggle": {"offer": int(deal["haggle"]["offer"]), "cut": deal["haggle"]["cut"]}}
                  if deal.get("haggle") else {}),
               **({"unpriced": list(deal["unpriced"])} if deal.get("unpriced") else {}),
               **({"condition_text": deal["condition_text"]} if deal.get("condition_text") else {}),
               **({"issues": list(cond["issues"])} if cond.get("issues") else {})}
        today = day_of(now)
        with self._lock:
            old = next((t for t in self._data["tips"] if listing_key(t["url"]) == ident), None)
            if old is not None:
                tip["at"] = float(old.get("at") or now)
            self._data["tips"] = ([tip] + [t for t in self._data["tips"] if listing_key(t["url"]) != ident])[:MAX_TIPS]
            told = self._data["tips_told"]
            if told["day"] != today:
                told.update(day=today, n=0)
            quiet = old is not None or bool(deal.get("pinged"))
            if not quiet:
                told["n"] += 1
                quiet = told["n"] > TIPS_PER_DAY
            self._save()
            if old is None or not quiet:
                text = (f"{tip['site']}: „{title}” {format_price(tip['price'])}. Ígéretes, de nem ellenőriztem: {why}. "
                        f"A hasonlók kb. {format_price(tip['usual'])}-ért mennek, kb. {format_price(tip['profit'])} "
                        "haszon lehet benne. Nézd meg magad, mielőtt megveszed.")
                self._tip_notices.append((("deal", f"Ellenőrizetlen: {title[:50]}", text),
                                          {"url": tip["url"], "actions": [DEALS_ACTION], "quiet": quiet or None,
                                           "tip": tip["id"]}))

    def _remember(self, deal: dict[str, Any]) -> bool:
        """Egy találat a találatok közé. Egy hirdetésből (webcím) egy: ha egy másik sor (vagy a figyelő) közben már
        elmentette, nem lesz belőle még egy, és False (a hívó ilyenkor nem szól róla újra)."""
        # a leírás és a fotók címe csak a modellkérdéshez kellett
        deal = {k: v for k, v in deal.items() if k not in ("details", "images") and v is not None}
        with self._lock:
            if listing_key(deal["url"]) in self._deal_keys():
                return False
            deals = [deal] + self._data["deals"]
            haggles = [d for d in deals if d.get("haggle") and not d.get("bought")]
            if len(haggles) > MAX_HAGGLES:     # az alkuképesek ne szorítsák ki a megérő találatokat
                old = {id(d) for d in haggles[MAX_HAGGLES:]}
                deals = [d for d in deals if id(d) not in old]
            self._data["deals"] = deals[:MAX_DEALS]
            self._save()
        return True

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
        search = next((s for s in running if matches_query(s["query"], str(row.get("title") or row["text"]))
                       and not excluded(s.get("exclude") or [], row["text"])), None)
        if search is not None:                 # a jóváhagyott szokásos ára: biztos
            reference = int(search["reference"])
            return {"reference": reference, "n": None, "name": search["query"], "search": search["id"],
                    "profit": expected_profit(reference, price), "sure": True, "basis": self._search_basis(search),
                    "top": highest_ok({**search, "min_price": None, "max_price": None}, reference,
                                      str(row.get("platform") or "")),
                    "cheap": pays(search, price, reference) and self._affordable(price)}
        product = self.book.key(row.get("title") or row["text"])
        self._note_demand(product[0] if product else None, row["url"])
        found = self._screened(self.book.reference(product[0], exclude=row["url"]), self._want(row)) \
            if product else None
        if found is None:
            return None
        return self._valued(price, found, found["name"], floor)

    def _valued(self, price: int, found: dict[str, Any], name: str, floor: int) -> dict[str, Any]:
        """Egy friss hirdetés értéke egy szokásos árhoz (found: az árbázisból, vagy amennyiért máshol adják; price, p25,
        n, sites) mérve: megéri, ha legalább EXPLORE_CUT-tal olcsóbb, az óvatos eladási áron is legalább floor haszon
        jön ki belőle, és belefér a vételi limitbe. sure: elég biztos-e ez a szokásos ár (elég hasonló ár, és legalább
        VET_MIN összehasonlító a leírása szerint is hibátlan: _screened, _market_of); enough: elég ár van (a leírások
        nélkül); refs: a leírásuk szerint hibás összehasonlítók (a hasonló hibájú jelölthöz, _reprice)."""
        reference, low = int(found["price"]), careful_low(found)
        sure = bool(found["sure"]) if "sure" in found else False
        return {"reference": reference, "n": found.get("n"), "name": name, "low": low, "sites": found.get("sites"),
                "spread": spread_of(found), "sold": found.get("sold"), "band": band_of(found),
                "basis": self._named_basis(found.get("basis")), "floor": floor, **market_info(found),
                "refs": (found.get("vetted") or {}).get("refs") or None,
                "enough": bool(found.get("enough", sure)), "found": found,
                "profit": expected_profit(reference, price, low), "sure": sure,
                "top": explore_top(reference, floor, low),
                "cheap": reference * FLOOR <= price <= explore_top(reference, floor, low) and self._affordable(price)}

    def appraise(self, rows: list[dict[str, Any]], source: str, look: Callable[[dict[str, Any]], dict[str, Any] | None],
                 search: Callable[[str], list[dict[str, Any]]], can_ask: Callable[[], bool],
                 found: Callable[[dict[str, Any]], None], go: Callable[[], None] = lambda: None,
                 wear: bool = False, ping: Callable[..., Any] | None = None) -> list[dict[str, Any]]:
        """Az azonnali figyelő friss hirdetései: minél hamarabb kideríti, megéri-e megvenni, és csak a megérőkről szól
        (found, rögtön, ahogy egy kiderül). A gyorsabbtól a lassabbig:
        1. modell nélkül: egy felmért keresésed szokásos ára, vagy az árbázis a hirdetés címéből (quick_value); ha az
           árbázis ára nem elég biztos (kevés hasonló, csak egy oldalról: sure_price), előbb a többi oldalon is
           megnézi (search), és csak akkor szól, ha így is jóval olcsóbb;
        2. az ismeretleneket a modell nevezi meg (egy hívás az egész kötegre); ha az árbázis így már ismeri, ahhoz méri,
           ha nem, megnézi az árát a többi oldalon (search; legfeljebb EXPLORE_PICKS, a modell szerint megérők elöl);
        3. a megérő jelölt oldala (look: elkelt, hibás, gyanús → kiesik), és egy rövid modellkérdés (tényleg ez, egy
           darab, működik?).
        A vételi limit fölötti és a nem eladási árú hirdetés (kereső, csere, alkatrész…) meg sem jön. Visszaadja, ami
        még sorra kerül: a modellre várókat (ha most nem lehet kérdezni, pl. épp beszélgetsz a Jarvisszal), és amit a
        mostani keretbe (EXPLORE_PICKS ár-ellenőrzés) már nem fért bele; ezek nem vesznek el, a hívó később újra adja
        őket (a modell által már megnevezettek a nevükkel: named, így nem kell újra megnevezni). go: minden hirdetés
        letöltése, modellkérdése és a szólás előtt; Deferred-del jelzi, ha most meg kell állni (Szünet; a look és a
        search is jelezheti, ha elfogyott a kéréskeret): ilyenkor a még le nem zárt hirdetések mind visszajönnek. wear: a
        ruhát is felméri (a figyelő keresőszava maga ruha vagy cipő: a te kifejezett kérésed); különben ami a modell
        szerint ruha, kimarad (ruhával nem kereskedik). ping: a gyors jelzés a megérőnek látszó jelöltről, még az
        ellenőrzése előtt (notify-szerű: kind, title, text, **mezők)."""
        with self._lock:
            floor = int(self._data["browse"].get("min_profit") or WATCH_PROFIT)
            known = self._deal_keys()
        done: set[int] = set()                 # a lezárt hirdetések (id): ezek nem jönnek vissza
        names: dict[int, str] = {}             # amit a modell már megnevezett
        try:
            return self._appraise(rows, source, look, search, can_ask, found, go, floor, known, done, names, wear,
                                  ping)
        except Deferred as exc:
            # amelyik hirdetésnél elfogyott a keret, az azokra az oldalakra vár; a többi a saját oldalára (a hirdetés
            # oldalát mindenképp meg kell nyitni), így egy másik oldal kerete nem akasztja meg (wait_for). Az árkutatás
            # közben talált, félbemaradt jelöltek (exc.extra) is várnak, önálló hirdetésként; ami közben találat lett,
            # az nem jön vissza.
            blocked = listing_key(exc.row["url"]) if exc.row is not None and exc.sites else ""
            with self._lock:
                known |= self._deal_keys()
            pending, queued = [], set()
            for row in [*rows, *exc.extra]:
                key = listing_key(row["url"])
                if id(row) in done or key in known or key in queued:
                    continue
                queued.add(key)
                name = names.get(id(row)) or row.get("named")
                item = {**row, "named": name} if name else dict(row)
                if exc.sites:
                    item["wait_for"] = list(exc.sites) if key == blocked else [str(row.get("platform") or "")]
                pending.append(item)
            return pending

    def _appraise(self, rows: list[dict[str, Any]], source: str, look: Callable[[dict[str, Any]], dict[str, Any] | None],
                  search: Callable[[str], list[dict[str, Any]]], can_ask: Callable[[], bool],
                  found: Callable[[dict[str, Any]], None], go: Callable[[], None], floor: int, known: set[str],
                  done: set[int], names: dict[int, str], wear: bool = False,
                  ping: Callable[..., Any] | None = None) -> list[dict[str, Any]]:
        """Az appraise lépései; amit lezár (látottnak jegyez, felmér, a csomagbontásnak ad), azt done-ba teszi."""
        def attempt(row: dict[str, Any], call: Callable[[], None]) -> None:
            go()
            try:
                call()
            except Deferred as exc:
                if exc.row is None:
                    exc.row = row              # ennél a hirdetésnél állt meg (erre vár: exc.sites)
                raise
            done.add(id(row))

        unknown, skip, recheck, lots, named_before, settle = [], [], [], [], [], []
        for row in rows:
            if listing_key(row["url"]) in known:
                done.add(id(row))
                continue                       # erről már szóltunk (pl. a böngészős kör)
            if not usable(row.get("title") or row["text"]):     # a cím dönt, ha van (a kártyán az eladó neve is)
                lots.append(row)               # a csomagot a csomagbontás méri fel (ha be van kapcsolva)
                continue
            if not self._affordable(row["price"]):
                skip.append(row)
                self.tally("over_limit", [row])
                continue
            value = self.quick_value(row, floor)
            if value is None:
                (named_before if row.get("named") else unknown).append(row)
            elif value["cheap"] and value["sure"]:
                settle.append((row, value))
            elif value["cheap"]:                     # olcsónak látszik, de az ára nem elég biztos
                recheck.append((row, self.book.query(row.get("title") or row["text"]) or value["name"], value))
            else:
                skip.append(row)
                self.tally("not_cheap", [row])
                self._haggle_value(row, value)       # kicsit drága: alkuval megérheti?
        other = self._to_bundles(lots, f"Azonnali figyelő: {source}")
        self.tally("other", other)
        self._mark(*skip, *other)
        done.update(id(row) for row in (*skip, *lots))
        # a biztos árúak előbb (letöltés csak a hirdetés oldalához kell), a nagyobb haszon elöl
        for row, value in sorted(settle, key=lambda entry: -int(entry[1].get("profit") or 0)):
            attempt(row, partial(self._settle, row, value, source, look, can_ask, found, go, ping=ping))
        for row, name, value in recheck[:EXPLORE_PICKS]:
            attempt(row, partial(self._recheck, row, name, search, floor, source, look, can_ask, found, go,
                                 known=value, ping=ping))
        later = [row for row, _name, _value in recheck[EXPLORE_PICKS:]]   # a keretbe nem fért: a következő körben
        named: list[tuple[dict[str, Any], str, bool]] = [(row, str(row["named"]), True) for row in named_before]
        names.update({id(row): str(row["named"]) for row in named_before})
        rest: list[dict[str, Any]] = []
        if unknown and self.llm is None:
            self._mark(*unknown)               # modell nélkül az ismeretlenről nem derül ki, mennyit ér
            self.tally("unknown", unknown)
            done.update(id(row) for row in unknown)
        elif unknown and not can_ask():
            rest = unknown
        elif unknown:
            go()
            answer = self._explore(unknown[:EXPLORE_ROWS])
            if answer is None:
                rest = unknown                 # a modell nem válaszolt: később újra
            else:
                rest = unknown[EXPLORE_ROWS:]
                answered = {id(row) for row, _q, _b in answer}
                unnamed = [row for row in unknown[:EXPLORE_ROWS] if id(row) not in answered]
                self._mark(*unnamed)
                self.tally("unknown", unnamed)
                done.update(id(row) for row in unnamed)
                named += answer
                names.update({id(row): query for row, query, _b in answer})
        if not named:
            return rest + later
        priced: list[tuple[dict[str, Any], dict[str, Any]]] = []
        elsewhere: list[tuple[dict[str, Any], str, bool]] = []
        for row, query, buy in named:
            if wearable(query) and not wear:   # ruha vagy cipő: azzal nem kereskedik (hacsak nem ezt figyeled)
                self._mark(row)
                self.tally("clothing", [row])
                done.add(id(row))
                continue
            if not row.get("named"):           # (a korábban megnevezettet már feljegyezte)
                self.book.add([row], name=query)
                brand = self.book.learn_name(query)
                if brand:
                    self.journal.add(f"Tanultam egy új márkát: {brand.capitalize()} (a modell többször így nevezte meg "
                                     "a terméket); mostantól modell nélkül is felismerem.")
            product = self.book.key(query)
            self._note_demand(product[0] if product else None, row["url"])
            reference = self._screened(self.book.reference(product[0], exclude=row["url"]), self._want(row)) \
                if product else None
            value = self._valued(int(row["price"]), reference, query, floor) if reference is not None else None
            if value is not None and (value["sure"] or not value["cheap"]):
                priced.append((row, value))
            else:                              # ismeretlen, vagy olcsónak látszik, de az ára nem elég biztos
                elsewhere.append((row, query, buy or value is not None, value))
        # a biztos árúak előbb (ezekhez nem kell letöltés), a nagyobb haszon elöl
        for row, value in sorted(priced, key=lambda entry: -int(entry[1].get("profit") or 0)):
            if value["cheap"]:
                attempt(row, partial(self._settle, row, value, source, look, can_ask, found, go, ping=ping))
            else:
                self._mark(row)
                self.tally("not_cheap", [row])
                self._haggle_value(row, value)
                done.add(id(row))
        # amennyiért máshol adják (a modell szerint megérők elöl, legfeljebb EXPLORE_PICKS)
        elsewhere.sort(key=lambda entry: not entry[2])
        for row, query, _buy, value in elsewhere[:EXPLORE_PICKS]:
            attempt(row, partial(self._recheck, row, query, search, floor, source, look, can_ask, found, go,
                                 known=value, ping=ping))
        later += [{**row, "named": query} for row, query, _buy, _value in elsewhere[EXPLORE_PICKS:]]
        return rest + later

    def _recheck(self, row: dict[str, Any], query: str, search: Callable[[str], list[dict[str, Any]]], floor: int,
                 source: str, look: Callable[[dict[str, Any]], dict[str, Any] | None], can_ask: Callable[[], bool],
                 found: Callable[[dict[str, Any]], None], go: Callable[[], None] = lambda: None,
                 known: dict[str, Any] | None = None, ping: Callable[..., Any] | None = None) -> None:
        """Mennyiért adják máshol: a keresőszó találatai a többi oldalon (search; az árbázisba is bekerülnek), az
        árbázis áraival együtt (_market_of). Ha így elég biztos az ár, és jóval olcsóbb, a végső ellenőrzés jön
        (_settle); ha nem elég biztos, nem szól róla. known: az árbázis szerinti érték (_valued); ha abban elég ár van,
        csak az összehasonlítók leírását nem olvastuk még el, előbb azokat olvassa el (keresés nélkül, _vet_reference).
        Ha az árbázis szerint olcsó, a gyors jelzés (ping) már most megy, a leírások olvasása és a keresés előtt."""
        if known is not None and known.get("cheap"):
            self._ping({**row, "id": secrets.token_hex(5), "median": int(known["reference"]),
                        "site": row.get("site") or ""}, ping)
        if known is not None and known.get("enough") and not known.get("sure") and known.get("found"):
            reference = self._vet_reference(known["found"], look)
            if reference is not None and reference["sure"]:
                value = self._valued(int(row["price"]), reference, query, floor)
                if value["cheap"]:
                    self._settle(row, value, source, look, can_ask, found, go, ping=ping)
                else:
                    self._mark(row)
                    self.tally("not_cheap", [row])
                return
        others = search(self._probe_text(query))
        self.book.add(others)
        # ha egy összehasonlító hirdetés változata a címéből nem derül ki, a leírását is elolvassa (a figyelő keretéből)
        market_now = self._market_of(row, query, others, read=look, reads=DETAIL_READS)
        if market_now is None:
            self._mark(row)                    # nincs hasonló: nem tudni, mennyit ér
            self.tally("unknown", [row])
            return
        value = self._valued(int(row["price"]), market_now, query, floor)
        queue: list[tuple[float, dict[str, Any] | None, dict[str, Any]]] = []
        if not value["cheap"]:
            self._mark(row)
            self.tally("not_cheap", [row])
            self._haggle_value(row, value)
        elif not value["sure"]:
            self._unsure(row, query, market_now)
        else:
            queue.append((value["profit"], None, value))

        def judge(peer: dict[str, Any], reference: dict[str, Any]) -> tuple[float, Any] | None:
            other = self._valued(int(peer["p"]), reference, query, floor)
            if not other["cheap"]:
                return None
            if not other["sure"]:
                return 0.0, ("tipp", peer["row"], reference)
            return other["profit"] * (1.0 if peer.get("exact") else 0.75), ("jelölt", peer["row"], other)

        for score, what in self._peers(row, market_now, judge):
            if what[0] == "jelölt":
                queue.append((score, what[1], what[2]))
            else:
                self._unsure(what[1], query, what[2])
        # a jobb előbb (az árkutatás közben talált is megelőzheti az eredetit); ha közben meg kell állni (Szünet, keret),
        # ami még nem zárult le, vár: az eredeti is, és az árkutatás közben talált jelöltek is, önálló hirdetésként
        label = f"{source} · árkutatás közben („{str(row.get('title') or row['text'])[:40]}” mellett)"
        ordered = sorted(queue, key=lambda entry: -entry[0])
        for index, (_score, other, other_value) in enumerate(ordered):
            try:
                if other is None:
                    self._settle(row, value, source, look, can_ask, found, go, ping=ping)
                else:
                    self._settle(other, other_value, label, look, can_ask, found, go, spawned=True, ping=ping)
            except Exception as exc:
                # ami innentől jönne, nem zárult le: egy későbbi összehasonlításnál újra jelölt lehet, és ha most meg
                # kell állni, az árkutatás közben talált jelöltek a várakozók közé kerülnek (appraise: exc.extra)
                rest = [entry[1] for entry in ordered[index:] if entry[1] is not None]
                self._unspawn(*(str(other_row["url"]) for other_row in rest))
                if isinstance(exc, Deferred):
                    exc.extra = [*exc.extra, *rest]
                    if other is not None and exc.row is None:
                        exc.row = other        # ennél a hirdetésnél állt meg (erre vár: exc.sites)
                raise

    def _settle(self, row: dict[str, Any], value: dict[str, Any], source: str,
                look: Callable[[dict[str, Any]], dict[str, Any] | None], can_ask: Callable[[], bool],
                found: Callable[[dict[str, Any]], None], go: Callable[[], None] = lambda: None,
                spawned: bool = False, ping: Callable[..., Any] | None = None) -> None:
        """Egy megérő friss hirdetés végső ellenőrzése: előbb egy gyors jelzés (ping: „Ígéretes …, ellenőrzés
        folyamatban”), aztán az oldala (elkelt, hibás, gyanús?), az elrejtett termékek, egy rövid modellkérdés (ha most
        lehet), és az állapota szerint újraszámolt haszon (_verdict). Ha ellenőrzött, vagy csak a modell nem nézte meg
        (foglalt volt: _unchecked, utólag megnézi), a találatok közé kerül (Megvettem gomb), és found(találat): ez a
        sürgős értesítés; ha ellenőrizetlen (nem nyílt meg az oldala, az állapota miatt bizonytalan), az Érdemes megnézni
        listába kerül, az okkal. go: a modellkérdés és a
        szólás előtt (Szünetben Deferred: a hirdetés megmarad, később újra sorra kerül)."""
        probe = {"id": value.get("search") or "azonnali", "query": value["name"]}
        deal = self._deal(probe, row, int(value["reference"]), explore=source, similar=value.get("n"), quick=True,
                          product=value["name"], low=value.get("low"), sites=self._site_names(value.get("sites")) or None,
                          spread=value.get("spread"), sold=value.get("sold"), band=value.get("band"),
                          basis=value.get("basis"), spawned=spawned, refs=value.get("refs"), floor=value.get("floor"),
                          **{k: value.get(k) for k in ("identity", "variant_note", "clean_n", "competitor")})
        with self._lock:
            if listing_key(row["url"]) in self._deal_keys():
                return
        self.tally("candidates", [deal])
        if why := self._suspect(deal):
            deal["suspect"] = why
            self._suspicious(deal)             # gyanúsan olcsó: az oldalát sem nyitja meg
            return
        if self._risky(deal):
            return
        self._ping(deal, ping)
        page = look({**row, "urgent": True})   # a jelölt adatlapja: a letöltő sorában elsőbbséggel
        verdict = self._inspect(deal, page) if page is not None else ""
        if page is None:
            deal.setdefault("warnings", []).append("a hirdetés oldalát nem tudtam megnyitni")
        if verdict == "gyanús":
            self._suspicious(deal)
            return
        if verdict not in ("", "captcha") or self._muted(deal.get("product_key"), self.clock()):
            self._mark(deal)
            self.tally({"elkelt": "gone", "hibás": "defect"}.get(verdict, "muted"), [deal])
            return
        go()
        # ha a modell most foglalt (pl. egy feltöltés használja), nem vár rá: az adatlap és az újraszámolás után így is
        # szól, és a modell utólag nézi meg (followups); gyors riasztásnál eleve így (a köteg után kérdezi)
        fast = self._fast()
        ok = self._confirm(probe, deal) if self.llm is not None and can_ask() and not fast else None
        if ok is False:
            if deal.get("suspect"):
                self._suspicious(deal)
            else:
                self._mark(deal)
                self.tally("model_no", [deal])
            return
        state, why, hold = self._verdict(deal, ok, fast=fast)
        if state in ("hibás", "nem éri meg"):
            self._drop_checked(deal, state, why)
            return
        go()
        self._mark(deal)
        if state == "ellenőrizetlen":
            self._unverified(deal, why, hold)
            return
        if state == "modell nélkül":
            self._unchecked(deal, why)
        else:
            deal["verified"] = True
        if self._risky(deal):                  # az újraszámolt haszonnal újra felmérve
            return
        if not self._remember(deal):          # közben egy kör már megtalálta: nem szól róla még egyszer
            return
        self.tally("found", [deal])
        self.journal.add(f"Jó találat (azonnali figyelő): „{deal['text'][:60]}” {format_price(deal['price'])} "
                         f"({deal['site']}, kb. {format_price(deal['profit'])} haszon) – {source}")
        found(deal)
