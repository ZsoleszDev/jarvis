"""Mi van eladó: maga a termék, vagy csak egy része, tartozéka, másolata (modell nélkül, a cím szavaiból); és a
memóriamodul kivitele (laptop- vagy asztali memória).

A termékkulcs (pricebook.product_key) a cím típusjeléből jön, ezért a „Ryzen 5 3600 gyári hűtő”, a „PS5 Slim
lemezmeghajtó”, a „Switch OLED kijelző” vagy az „iPhone 12 akkumulátor” kulcsa ugyanaz, mint a processzoré, a konzolé, a
telefoné. Ezek nem a termék árai: összehasonlítóként lehúznák a szokásos árat, jelöltként pedig „nagyon olcsónak”
látszanának. Fordítva: a „Switch OLED töltővel, dokkolóval” vagy az „iPhone 13 128GB töltő nélkül” maga a készülék, nem
egy kiegészítő.

Magyar címben a tárgy a fejszó, és a ragja dönti el, hogy a hirdetés tárgya-e:
- „iPhone 13 tok”, „PS5 lemezmeghajtó”, „Kijelző iPhone 13” (puszta alak, többes szám, birtokos: „tokja”): ez az eladó
  tárgy, egy rész vagy tartozék;
- „töltővel”, „dobozzal”, „kábelekkel”, „dobozostul” (-val/-vel, -stul): a készülék, ezzel együtt;
- „töltő nélkül”: a készülék, ez hiányzik belőle;
- „+ tok”, „és gyári hűtő”, „, töltő” (egy felsorolásban a készülék után): a készülék, ez plusz jár hozzá;
- „iPhone 13-hoz tok” (-hoz/-hez): a tartozék (ezt a pricebook._FOR_IT nézi).
Ha a címben a készülék saját adata is benne van (tárhely, memória, processzor; az akkunál a százalék, pl. „akku 89%”),
akkor a többi szó a készüléket írja le, nem egy külön eladott részt.

Memóriamodul: a SO-DIMM (laptop, mini PC) és a DIMM (asztali gép) más termék, más áron. Ha a cím nem írja ki, sokszor
akkor is kiderül: a modul címkéjéről (JEDEC: „PC4-2666V-SA1”, a „S” a SO-DIMM), a gyártói cikkszámból (Samsung M471 /
M378, Hynix …S6 / …U6, Crucial …SF / …DF, Kingston KVR…S / KVR…N, Corsair CMS… / CMK…, Micron …HZ / …AZ, G.Skill …RS),
a termékcsaládból (Kingston FURY Impact: SO-DIMM; FURY Beast, Vengeance LPX, Trident Z: asztali), vagy a szövegből
(„laptopba”, „notebook memória”, „asztali gépbe”). A cikkszám és a címke biztos jel (strong); a „laptop” szó csak akkor,
ha a cím nem egy egész gépé (abban processzor, SSD, kijelzőméret is szokott lenni)."""
from __future__ import annotations

import re
from typing import NamedTuple

from .textutil import fold

SODIMM, DIMM = "sodimm", "dimm"


class RamForm(NamedTuple):
    """Egy memóriamodul kivitele a szövegből: form („sodimm” | „dimm” | „” ha nem derül ki, vagy ellentmondásos),
    evidence (a szövegrész, amiből kiderül, ékezet nélkül), strong (cikkszám, címke, a kivitel neve, termékcsalád: nem csak
    egy „laptop” szó), server (a cikkszám szerint szerver-memória: ECC, regisztrált)."""
    form: str
    evidence: str
    strong: bool
    server: bool


# (minta, kivitel, erős jel, szerver-memória). A minta ékezet nélküli, kisbetűs szövegen fut (a kötőjelek megvannak).
_FORM_SIGNS: tuple[tuple[re.Pattern[str], str, bool, bool], ...] = (
    # a kivitel neve
    (re.compile(r"\bso ?-?dimm\w*"), SODIMM, True, False),
    (re.compile(r"\b(?:u-?)?dimm\b"), DIMM, True, False),   # (a „SO-DIMM” előbb kiesik: _neutral)
    # JEDEC-címke. DDR3: a sebesség utáni betű a modul fajtája („PC3L-12800S”: SO-DIMM, „U”: asztali, „R”/„E”:
    # szerver); DDR4/DDR5: a második tag első betűje („PC4-2666V-SA1”, „PC4-2400T-UA2”, „PC4-2133P-RB0”)
    (re.compile(r"\bpc3l?-?\d{4,5}s\b"), SODIMM, True, False),
    (re.compile(r"\bpc3l?-?\d{4,5}u\b"), DIMM, True, False),
    (re.compile(r"\bpc3l?-?\d{4,5}[re]\b"), DIMM, True, True),
    (re.compile(r"\bpc[45]-?\d{4,5}[a-z]{1,2}-s[a-z]?\d"), SODIMM, True, False),
    (re.compile(r"\bpc[45]-?\d{4,5}[a-z]{1,2}-u[a-z]?\d"), DIMM, True, False),
    (re.compile(r"\bpc[45]-?\d{4,5}[a-z]{1,2}-[rle][a-z]?\d"), DIMM, True, True),
    # Samsung: M471/M474/M425 SO-DIMM, M378/M323 asztali, M391/M393/M386/M321 szerver
    (re.compile(r"\bm4(?:7[0-8]|25)[a-z]\d"), SODIMM, True, False),
    (re.compile(r"\bm3(?:78|23)[a-z]\d"), DIMM, True, False),
    (re.compile(r"\bm3(?:9[13]|86|21)[a-z]\d"), DIMM, True, True),
    # SK Hynix: HMA (DDR4), HMT (DDR3) a sűrűség után „S6” SO-DIMM, „U6” asztali, „R7”/„E7” szerver; HMCG (DDR5) „SA”/„UA”
    (re.compile(r"\bhm[at]a?[0-9a-z]{1,3}?s[678][a-z]"), SODIMM, True, False),
    (re.compile(r"\bhm[at]a?[0-9a-z]{1,3}?u[678][a-z]"), DIMM, True, False),
    (re.compile(r"\bhm[at]a?[0-9a-z]{1,3}?[re][678][a-z]"), DIMM, True, True),
    (re.compile(r"\bhmcg[0-9a-z]{4,6}?sa\d{3}"), SODIMM, True, False),
    (re.compile(r"\bhmcg[0-9a-z]{4,6}?ua\d{3}"), DIMM, True, False),
    # Crucial: CT8G4SF… SO-DIMM, CT8G4DF… asztali, CT…G4RF… szerver; DDR5: CT16G48C40S5 / …U5; DDR3: CT8G3S…, CT…BF…
    (re.compile(r"\bct\d{1,3}g4sf"), SODIMM, True, False),
    (re.compile(r"\bct\d{1,3}g4df"), DIMM, True, False),
    (re.compile(r"\bct\d{1,3}g4rf"), DIMM, True, True),
    (re.compile(r"\bct\d{1,3}g\d{2}c\d{2}s5\b"), SODIMM, True, False),
    (re.compile(r"\bct\d{1,3}g\d{2}c\d{2}u5\b"), DIMM, True, False),
    (re.compile(r"\bct\d{1,3}g3s\d"), SODIMM, True, False),
    (re.compile(r"\bct\d{5,6}bf\d"), SODIMM, True, False),
    (re.compile(r"\bct\d{5,6}b[ad]\d"), DIMM, True, False),
    # Kingston ValueRAM: KVR26S19… SO-DIMM, KVR26N19… / KVR48U… asztali, KVR…E…/R… szerver; FURY és HyperX: KF432S… /
    # HX426S… SO-DIMM (Impact), KF432C… / HX426C… asztali; KCP…S… / KCP…N…; KSM (szerver, KSM…S… SO-DIMM)
    (re.compile(r"\bkvr\d{2,3}l?s\d"), SODIMM, True, False),
    (re.compile(r"\bkvr\d{2,3}l?[nu]\d"), DIMM, True, False),
    (re.compile(r"\bkvr\d{2,3}l?[er]\d"), DIMM, True, True),
    (re.compile(r"\b(?:kf|hx)\d{3,4}s\d"), SODIMM, True, False),
    (re.compile(r"\b(?:kf|hx)\d{3,4}c\d"), DIMM, True, False),
    (re.compile(r"\bkcp\d{3,4}s"), SODIMM, True, False),
    (re.compile(r"\bkcp\d{3,4}n"), DIMM, True, False),
    (re.compile(r"\bksm\d{2}s[er]"), SODIMM, True, True),
    (re.compile(r"\bksm\d{2}[re]"), DIMM, True, True),
    # Corsair: CMSX/CMSO/CMSA SO-DIMM, CMK/CMW/CMH/CMT… asztali
    (re.compile(r"\bcms[xoa]\d"), SODIMM, True, False),
    (re.compile(r"\bcm[kwhtdzvgrulp]\d{1,3}gx\d"), DIMM, True, False),
    # Micron: …64HZ SO-DIMM, …64AZ asztali, …72AZ / …72PZ szerver
    (re.compile(r"\bmta?\d[0-9a-z]{2,14}?64hz"), SODIMM, True, False),
    (re.compile(r"\bmta?\d[0-9a-z]{2,14}?72hz"), SODIMM, True, True),
    (re.compile(r"\bmta?\d[0-9a-z]{2,14}?64az"), DIMM, True, False),
    (re.compile(r"\bmta?\d[0-9a-z]{2,14}?72[ap]z"), DIMM, True, True),
    # G.Skill: a „-RS” végű cikkszám (Ripjaws SO-DIMM): F4-3200C22S-8GRS, F5-4800S3434A16GX1-RS
    (re.compile(r"\bf[345]-?\d{4}[a-z0-9-]*?-?rs[lkb]?\b"), SODIMM, True, False),
    # termékcsaládok: az Impact SO-DIMM; a Beast, Renegade, LPX, Dominator, Trident, Ripjaws V asztali
    (re.compile(r"\bimpact\b"), SODIMM, True, False),
    (re.compile(r"\b(?:beast|renegade|lpx|dominator|trident|ripjaws ?(?:v|4|m5|s5)|vengeance rgb|aegis|spectrix|"
                r"flare x\d?|sniper x)\b"), DIMM, True, False),
    # a szövegből (gyenge jel: a „laptop” egy egész gépet is jelölhet)
    (re.compile(r"\b(?:laptop|notebook|macbook|ultrabook|netbook|nuc)\w*(?: (?:ram|memoria\w*|memory)\b)?|"
                r"\bmini ?-?pc\w*"), SODIMM, False, False),
    (re.compile(r"\b(?:asztali\w*|desktop\w*|szamitogep\w*|szgep\w*)|(?<!mini )(?<!mini-)\bpc ?-?(?:be|hez|re)\b|"
                r"(?<!mini )(?<!mini-)\bpc ?(?=(?:ram|memoria|memory|ddr[2-5])\b)"), DIMM, False, False),
)


# A modul generációja a gyártói cikkszámból, ha a cím nem írja ki („Samsung 8GB M471A1K43DB1”: DDR4): G.Skill F3/F4/F5,
# Samsung M471A/M378A (DDR4), M471B (DDR3), M425R/M323R (DDR5), Hynix HMT (DDR3), HMA (DDR4), HMCG (DDR5), Crucial
# CT…G3/G4 és CT…G48C… (DDR5), Kingston KVR13/16 (DDR3), KVR21–32 (DDR4), KVR48–64 (DDR5), FURY/HyperX KF4…/HX3…,
# Corsair …GX3/GX4/GX5, Micron MT…KTF (DDR3), MTA…A (DDR4), MTC (DDR5).
_GENERATION_SIGNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bf([345])-?\d{4}[a-z]"), ""),
    (re.compile(r"\bm(?:4(?:7[0-8]|25)|3(?:78|23|9[13]|86|21))b\d"), "ddr3"),
    (re.compile(r"\bm(?:4(?:7[0-8]|25)|3(?:78|23|9[13]|86|21))a\d"), "ddr4"),
    (re.compile(r"\bm(?:4(?:7[0-8]|25)|3(?:78|23|9[13]|86|21))r\d"), "ddr5"),
    (re.compile(r"\bhmt[0-9a-z]{3,}"), "ddr3"), (re.compile(r"\bhmaa?[0-9][0-9a-z]{3,}"), "ddr4"),
    (re.compile(r"\bhmcg[0-9a-z]{4,}"), "ddr5"),
    (re.compile(r"\bct\d{1,3}g3"), "ddr3"), (re.compile(r"\bct\d{1,3}g4[sdr]f"), "ddr4"),
    (re.compile(r"\bct\d{1,3}g\d{2}c\d{2}[su]5\b"), "ddr5"),
    (re.compile(r"\bkvr1[0-8]l?[snuer]\d"), "ddr3"), (re.compile(r"\bkvr(?:2\d|3[0-2])[snuer]\d"), "ddr4"),
    (re.compile(r"\bkvr(?:4\d|5\d|6[0-4])[snuer]\d"), "ddr5"),
    (re.compile(r"\b(?:kf|hx)([345])\d{2}[sc]\d"), ""),
    (re.compile(r"\bcm\w{0,4}\d{1,3}gx([345])"), ""),
    (re.compile(r"\bmt\d{1,2}k[jt]f"), "ddr3"), (re.compile(r"\bmta\d{1,2}a"), "ddr4"), (re.compile(r"\bmtc\d"), "ddr5"),
)


# Gyors előszűrő: ezekkel kezdődik egy memória-cikkszám (a legtöbb címben nincs ilyen, és akkor a minták sem kellenek).
_PART_NUMBER_GATE = re.compile(r"\b(?:f[345]-?\d|m[34]\d\d|hm[atc]|ct\d|kvr\d|kf\d|hx\d|cm\w{0,4}\d|mt[ac0-9])")


# Memória-termékcsaládok: ha a címben nincs „DDR”, „RAM” vagy „memória” szó, ezekből (a kapacitással és a sebességgel)
# derül ki, hogy memóriamodul („Kingston Fury Beast 16GB 3200”, „Corsair Vengeance LPX 2x8GB 3600”). (Az SSD-családok
# nevei kimaradnak: XPG Spectrix S40G, Gammix S70, HyperX Fury SSD; a háttértár szava és a „/s” sebesség is kizárja.)
_RAM_LINE_RE = re.compile(r"\b(?:fury (?:beast|impact|renegade)|hyperx (?:fury|impact|predator|savage)|valueram|"
                          r"vengeance|dominator|ripjaws|trident ?z\w*|ballistix|t-?force (?:vulcan|delta|xtreem|dark)\w*|"
                          r"xpg (?:spectrix d\d+|gammix d\d+|lancer|caster)|viper (?:steel|elite|venom|4)|flare ?x\d?|"
                          r"sniper ?x|aegis)\b")
_STORAGE_RE = re.compile(r"\b(?:ssd|nvme|m\.?2|sata|pcie|hdd)\b|\b\d+ ?tb\b|(?:mb|gb) ?/ ?s\b")
_MODULE_SIZE_RE = re.compile(r"\b(?:[1-8] ?x ?)?(?:4|8|16|24|32|48|64|96|128) ?gb?\b")
_LINE_SPEED_RE = re.compile(r"\b([1-9]\d{3}) ?(?:mhz|mt/?s|c ?l?\d{2})?\b")


def _line_generation(folded: str) -> tuple[str, str]:
    """A DDR szó nélküli memóriacím generációja a termékcsaládjából és a sebességéből (_RAM_LINE_RE): DDR3 legfeljebb
    1866 MHz, DDR4 2666–4600, DDR5 4800-tól (a 2133 és a 2400 mindkettő lehet, a 4800–5333 a G.Skill Royalnál DDR4 is).
    (generáció, a sebesség szövegrésze), vagy („”, „”)."""
    if not _RAM_LINE_RE.search(folded) or _STORAGE_RE.search(folded) or not _MODULE_SIZE_RE.search(folded):
        return "", ""
    for match in _LINE_SPEED_RE.finditer(folded):
        speed = int(match.group(1))
        if 1066 <= speed <= 1866:
            return "ddr3", match.group(1)
        if 2666 <= speed <= 4600:
            return "ddr4", match.group(1)
        if 4800 <= speed <= 9600 and (speed > 5333 or "royal" not in folded):
            return "ddr5", match.group(1)
    return "", ""


def ram_generation(text: str) -> str:
    """A memóriamodul DDR-generációja a gyártói cikkszámából, vagy ha a címben nincs DDR szó, a termékcsaládjából és a
    sebességéből („ddr3” | „ddr4” | „ddr5”), ha kiderül; „”, ha nem (vagy ha a cikkszámok ellentmondanak egymásnak)."""
    return generation_sign(text)[0]


def generation_sign(text: str) -> tuple[str, str]:
    """(generáció, a szövegrész, amiből kiderül: a cikkszám eleje vagy a sebesség), lásd ram_generation; („”, „”), ha
    nem derül ki."""
    folded = fold(text or "")
    if _PART_NUMBER_GATE.search(folded):
        found: dict[str, str] = {}
        for pattern, generation in _GENERATION_SIGNS:
            match = pattern.search(folded)
            if match:
                found.setdefault(generation or f"ddr{match.group(1)}", match.group(0))
        if len(found) == 1:
            return next(iter(found.items()))
        if len(found) > 1:
            return "", ""
    return _line_generation(folded)


def _neutral(folded: str) -> str:
    """A SO-DIMM szó egy szó (a „DIMM” minta ne találja meg benne az asztalit)."""
    return re.sub(r"\bso ?-?dimm", "sodimm", folded)


def ram_form(text: str) -> RamForm:
    """Egy memóriamodul kivitele a szövegből (RamForm). Az erős jelek (cikkszám, címke, a kivitel neve, termékcsalád)
    előbb: ha ezek egybehangzók, ez a kivitel, a gyenge jelek (laptop, asztali, PC szó) ellentmondása ellenére is (pl.
    „SO-DIMM, asztali gépbe nem jó”). Ha az erős jelek egymásnak ellentmondanak, vagy erős jel nincs, és a gyengék
    ellentmondanak („laptopba és asztali gépbe”), nem derül ki („”)."""
    folded = _neutral(fold(text or ""))
    found: list[tuple[str, str, bool, bool]] = []
    for pattern, form, strong, server in _FORM_SIGNS:
        match = pattern.search(folded)
        if match:
            found.append((form, match.group(0), strong, server))
    server = any(entry[3] for entry in found)
    for strong in (True, False):
        forms = {entry[0] for entry in found if entry[2] == strong}
        if len(forms) == 1:
            first = next(entry for entry in found if entry[2] == strong)
            return RamForm(first[0], first[1], strong, server)
        if len(forms) > 1:
            return RamForm("", "", strong, server)
    return RamForm("", "", False, server)


# --- egy egész gép (a „laptop” szó ilyenkor nem a modul helye) ---

_MACHINE_RE = re.compile(r"\b(?:i[3579]|ryzen|celeron|pentium|athlon|core|ssd|hdd|nvme|emmc|windows|win ?1[01]|"
                         r"kijelzo\w*|\d{2}(?:[.,]\d)? ?(?:col|inch|huvelyk|\")|\d{3,4} ?x ?\d{3,4})\b")


def machine(text: str) -> bool:
    """Egy egész gép (laptop, PC) címének látszik-e: processzor, háttértár, kijelző vagy kijelzőméret van benne."""
    return bool(_MACHINE_RE.search(fold(text or "")))


# --- részek, tartozékok: a cím fejszava ---

# Kiegészítő: a készülékhez külön vehető dolog (töltő, tok, kábel, dokkoló…).
ACCESSORY_STEMS = ("tolto", "dokkolo", "tok", "vedotok", "borito", "skin", "matrica", "kabel", "allvany", "tarto",
                   "szilikon", "grip", "ujjvedo", "kijelzovedo", "uvegfolia", "folia", "szij", "pant")
# A készülék egy része (alkatrész): hűtő, kijelző, akkumulátor, alaplap, tápegység, meghajtó, ház…
# (A „Cooler Master” márka, a „Wraith” a dobozos AMD-hűtő neve, amivel a processzort is árulják: ezek nem tőszavak.)
PART_STEMS = ("huto", "ventilator", "venti", "vizhutes", "waterblock", "vizblokk", "backplate", "kijelzo", "lcd",
              "digitizer", "erintopanel", "erintokepernyo", "akkumulator", "akku", "akksi", "battery", "alaplap",
              "logicboard", "tapegyseg", "tap", "lemezmeghajto", "meghajto", "haz", "hatlap", "keret", "burkolat",
              "fedlap", "billentyuzet", "keyboard", "touchpad", "kamera", "kameramodul", "hangszoro", "toltoport",
              "toltocsatlakozo", "csatlakozo", "flex", "zsaner", "panel", "modul", "szivofej", "kefe", "kefefej",
              "tartaly", "szuro", "porzsak")
# Ezek a szóösszetételek második tagjaként is a tárgyat nevezik meg („töltőkábel”, „hűtőborda”, „kijelzőpanel”).
_TAILS = ("kabel", "fej", "egyseg", "modul", "panel", "borda", "blokk", "csatlakozo", "port", "allomas", "adapter",
          "tok", "keszlet", "szett", "ventilator", "venti", "tap", "lap", "fedel")
# A puszta alak végződései: semmi, többes szám, birtokos („tokja”, „töltője”, „hűtői”).
_BARE_ENDINGS = ("", "k", "ok", "ek", "ja", "je", "jai", "jei", "jat", "jet", "t", "ot", "et", "at")
# A felsorolásban előtte: a készülék plusz tartozéka („+ tok”, „és gyári hűtő”, „, töltő”).
_EXTRA_BEFORE = {"+", ",", "es", "plusz", "valamint", "illetve", "with", "&", "meg", "ajandek", "ajandekba"}
# A fejszó előtti jelzők, amik nem változtatnak rajta („gyári hűtő”, „eredeti töltő”, „dobozos hűtő”).
_ADJECTIVES = {"gyari", "eredeti", "original", "stock", "box", "dobozos", "hasznalt", "fekete", "feher", "szurke",
               "kek", "piros", "vezetek", "nelkuli", "vezeteknelkuli", "gyorstolto", "usb", "usbc", "type", "c",
               "magsafe", "apple", "samsung", "sony", "a", "az", "egy", "csak", "is"}
# A rész a készülék állapotát írja le, nem egy külön eladott rész („karcos kijelző”, „új akku”, „cserélt akkumulátor”).
_STATE_BEFORE = {"karcos", "repedt", "torott", "serult", "foltos", "beegett", "kopott", "hibas", "uj", "cserelt",
                 "friss", "jo", "gyenge", "eros", "nagy", "orias", "hibatlan", "tokeletes", "zajos", "hangos", "zorgo",
                 "zorog", "kattog", "csendes", "halk"}
# A rész utáni szó a készülék javítását írja le („akku csere után”, „kijelző cserélve”, „javítva”).
_REPAIRED = ("csere", "cserel", "javit", "felujit")
_WITH_END = re.compile(r"(?:v[ae]l|([bcdfghjklmnprstvzx])\1[ae]l|stul|stul|ostul|estul|astul)$")
_TOKENS = re.compile(r"[a-z0-9]+|[+,&]")
_PERCENT = re.compile(r"\d{1,3} ?%")
# A típusjelhez tapadó „+” a Plus változat („Galaxy S21+”, „Redmi Note 12 Pro+”), nem egy felsorolás jele.
PLUS_MODEL = re.compile(r"(?<=[a-z0-9])\+(?=\s|$|[,;)/])")


def _with_form(token: str) -> bool:
    """„-val/-vel” (vagy „-stul”) ragos alak: a készülék ezzel együtt („töltővel”, „tokkal”, „dobozostul”)."""
    return bool(_WITH_END.search(token))


def _stem_of(token: str, stems: tuple[str, ...]) -> str:
    """Ha a szó egy tárgy puszta alakja (vagy többes, birtokos alakja, vagy egy összetétel, aminek az utolsó tagja is
    ilyen: „töltőkábel”, „hűtőventilátorok”), a tárgy töve; különben „” (pl. „töltővel”, „hűtős”, „kijelzős”)."""
    for stem in sorted(stems, key=len, reverse=True):
        if not token.startswith(stem):
            continue
        rest = token[len(stem):]
        if rest in _BARE_ENDINGS:
            return stem
        for tail in _TAILS + stems:
            if rest.startswith(tail) and rest[len(tail):] in _BARE_ENDINGS:
                return stem
    return ""


def head(text: str, stems: tuple[str, ...]) -> str:
    """A cím egy tárgyszava (stems), ami a hirdetés tárgya, nem a készüléket írja le: puszta (többes, birtokos) alak,
    nem követi a „nélkül”, nem egy „-val/-vel” ragos szó jelzője („kijelzővédő fóliával”), és nem a felsorolásban a
    készülék után áll („+ tok”, „és gyári hűtő”, „, töltő”). Visszaadja a tövét („tolto”), vagy „”."""
    folded = fold(text or "")
    if not any(stem in folded for stem in stems):
        return ""                          # (gyors út: egyik tárgyszó sincs benne)
    tokens = _TOKENS.findall(PLUS_MODEL.sub(" plus", folded))
    for index, token in enumerate(tokens):
        stem = _stem_of(token, stems)
        if not stem:
            continue
        after = tokens[index + 1] if index + 1 < len(tokens) else ""
        if after.startswith("nelkul") or (after and _with_form(after)):
            continue                       # „töltő nélkül”, „kijelzővédő fóliával”: a készülék
        if after in _STATE_BEFORE:
            continue                       # „kijelző karcos”, „akku gyenge”: a készülék állapota
        if any(word.startswith(_REPAIRED) for word in tokens[index + 1:index + 4]):
            continue                       # „kijelző és akku csere után”, „akku cserélve”: a készülék javítása
        if index and (re.match(r"^\d{1,2}x$", tokens[index - 1]) or tokens[index - 1] in ("db", "darab")):
            continue                       # „2x ventilátor”, „3 db ventilátor”: a készülék leírása (hány van rajta)
        before = index - 1
        while before >= 0 and tokens[before] in _ADJECTIVES:
            before -= 1
        if before >= 0 and (tokens[before] in _EXTRA_BEFORE or (before >= 1 and tokens[before - 1] in _EXTRA_BEFORE)):
            continue                       # „+ tok”, „és gyári hűtő”, „+ B550 alaplap”: egy plusz tétel a készülék mellett
        if before >= 0 and tokens[before] in _STATE_BEFORE:
            continue                       # „karcos kijelző”, „új akku”: a készülék állapota
        if index and tokens[index - 1] in _STATE_BEFORE:
            continue
        return stem
    return ""


def accessory(text: str) -> str:
    """A hirdetés tárgya egy kiegészítő (tok, töltő, kábel, dokkoló…): a töve, vagy „”."""
    return head(text, ACCESSORY_STEMS)


# A készülékfajták a termékkulcs szavai szerint (pricebook.product_key): ezeknél egy alkatrész szava a hirdetés
# tárgyát jelenti (egy márkanév magában nem ilyen: a „Logitech billentyűzet” maga a termék, a „Noctua hűtő” is).
HOSTS = {"iphone": "telefon", "galaxy": "telefon", "pixel": "telefon", "redmi": "telefon", "oneplus": "telefon",
         "ipad": "tablet", "kindle": "tablet", "ps3": "konzol", "ps4": "konzol", "ps5": "konzol", "switch": "konzol",
         "xbox": "konzol", "steamdeck": "konzol", "quest": "konzol", "macbook": "laptop", "imac": "számítógép",
         "thinkpad": "laptop", "laptop": "laptop", "rtx": "videókártya", "gtx": "videókártya",
         "rx": "videókártya", "arc": "videókártya", "ryzen": "processzor", "watch": "óra", "airpods": "fülhallgató",
         "buds": "fülhallgató", "dyson": "porszívó"}
_CPU_TIER = re.compile(r"^i[3579]$")
# Egy készülék saját adata a címben: ilyenkor a többi szó a készüléket írja le (a tárhely, a memória, a VRAM).
_CAPACITY = re.compile(r"\b\d{1,4} ?(?:gb|tb)\b")
# Az alaplap lapkakészlete vagy foglalata („B550”, „B450M”, „Z390-A”, „X570”, „AM4”, „LGA1700”): ez maga az alaplap, a
# processzor csak a kompatibilitása („B550 alaplap Ryzen 5000 kompatibilis”), nem egy processzor része.
_BOARD_RE = re.compile(r"\b(?:[abhqxz]\d{3}[a-z]?|x(?:58|79|99)|am[2-5]\+?|lga ?-?\d{3,4}|fm2\+?|s?trx?4)\b")
# Egy fülhallgató egyik fele, vagy a tokja nélkül: nem az egész.
_HALF_RE = re.compile(r"\b(?:bal|jobb)(?: oldali)? ?(?:ful\w*|fules\w*|fulhallgato\w*|earbud\w*|bud\w*)\b|"
                      r"\bcsak (?:a )?(?:bal|jobb)\b|\bfulek tok nelkul\b|\btok nelkul\b")
# Lego: hiányos, vagy csak egy része (figurák, útmutató, elemek).
_LEGO_PART_RE = re.compile(r"(?<!nem )\bhianyos\w*|\bnem teljes\w*|\bcsak (?:a )?(?:figur\w*|minifigur\w*|utmutato\w*|"
                           r"elem\w*|kock\w*|matric\w*)|\b(?:minifigurak|figurak|utmutatok?|osszerakasi utmutatok?)\b"
                           r"(?! ?-?(?:val|vel|kal|kel|al|el)\b)")


def host(key_words: set[str] | list[str] | tuple[str, ...], text: str = "") -> str:
    """Milyen készülék a cím termékkulcsa szerint (HOSTS; a processzor az i3–i9 szintjéről is), vagy „”."""
    words = set(key_words)
    for word, kind in HOSTS.items():
        if word in words:
            return kind
    if any(_CPU_TIER.match(word) for word in words):
        return "processzor"
    return ""


def might_be_part(text: str) -> bool:
    """Lehet-e a cím egy alkatrészé (gyors előszűrő a component előtt: van-e benne alkatrész-szó, fél fülhallgató vagy
    hiányos Lego)."""
    folded = fold(text or "")
    return any(stem in folded for stem in PART_STEMS) or "lego" in folded or bool(_HALF_RE.search(folded))


def component(text: str, key_words: set[str] | list[str] | tuple[str, ...]) -> str:
    """Ha a hirdetés egy készüléknek (host) csak egy része: mi („hűtő”, „kijelző”, „fél fülhallgató”, „hiányos
    Lego”…); különben „”. Lásd part_of."""
    return part_of(text, key_words)[1]


def part_of(text: str, key_words: set[str] | list[str] | tuple[str, ...]) -> tuple[str, str]:
    """Ha a hirdetés egy készüléknek (host) csak egy része: (a rész szavának töve, a neve), pl. („huto”, „hűtő”); a fél
    fülhallgatónál és a hiányos Legónál a tő üres; különben („”, „”). A készülék saját adata (tárhely, memória) a címben:
    a készülék maga („iPhone 12 64GB új akku”, „Galaxy S21 128GB kijelző repedt”); az akkumulátor a százalékával („akku
    89%”) az állapota. A videókártya „gyári hűtője” a kártya leírása (nem vízhűtéses), a processzoré a dobozos hűtő
    (külön eladva). A PC (a kulcsában a „pc”) egy egész gép, vagy maga a PC-alkatrész („NZXT H510 PC ház”); az alaplap a
    lapkakészletével maga az alaplap („B550 alaplap Ryzen 5000 kompatibilis”)."""
    folded = fold(text or "")
    words = set(key_words)
    kind = host(words, text)
    if "lego" in words and _LEGO_PART_RE.search(folded):
        return "", "hiányos, vagy csak egy része (figurák, útmutató, elemek)"
    if kind == "fülhallgató" and _HALF_RE.search(folded):
        return "", "a fülhallgató egyik fele, vagy a töltőtokja nélkül"
    if not kind or "pc" in words:
        return "", ""                      # (a PC egy egész gép, vagy maga a PC-alkatrész: „Gamer PC RTX 3060 RGB ház”)
    stem = head(text, PART_STEMS)
    if not stem:
        return "", ""
    board = stem in ("alaplap", "logicboard")
    if kind in ("telefon", "tablet", "konzol", "laptop", "számítógép", "videókártya", "óra") and not board and \
            _CAPACITY.search(folded):
        return "", ""                      # a készülék saját adata (tárhely, memória) is benne van: a készülék maga
        # (az alaplapon is ott a memória és a tárhely: „MacBook Air M1 alaplap 8GB 256GB” egy alaplap)
    if stem in ("akku", "akkumulator", "akksi", "battery") and _PERCENT.search(folded):
        return "", ""                      # „akku 89%”: az akkumulátor állapota
    if kind == "videókártya" and stem in ("huto", "ventilator", "venti", "cooler") and \
            re.search(r"\b(?:gyari|stock|eredeti|original) (?:huto|ventilator|venti|cooler)", folded):
        return "", ""                      # a kártya gyári hűtővel (nem vízhűtéses): a kártya maga
    if kind == "konzol" and stem == "lcd" and "steamdeck" in words:
        return "", ""                      # a Steam Deck LCD a típusa (nem OLED), nem egy kijelző
    if kind == "laptop" and any(_CPU_TIER.match(word) for word in words) and \
            not (board or stem in ("huto", "ventilator", "venti")):
        return "", ""                      # egy laptop a processzorával: a gép maga (de az alaplapon, a hűtőn is ott a
        # processzor neve: „ThinkPad T490 alaplap i5”, „Dell XPS 15 i7 ventilátor”)
    if kind == "processzor" and stem in ("alaplap", "logicboard") and _BOARD_RE.search(folded):
        return "", ""                      # az alaplap a lapkakészletével: maga az alaplap (a processzor a kompatibilitása)
    return stem, {"huto": "hűtő", "ventilator": "ventilátor", "venti": "ventilátor", "cooler": "hűtő", "wraith": "hűtő",
            "vizhutes": "vízhűtés", "waterblock": "vízhűtés", "vizblokk": "vízhűtés", "backplate": "hátlap",
            "kijelzo": "kijelző", "lcd": "kijelző", "digitizer": "érintőpanel", "erintopanel": "érintőpanel",
            "erintokepernyo": "érintőpanel", "akkumulator": "akkumulátor", "akku": "akkumulátor",
            "akksi": "akkumulátor", "battery": "akkumulátor", "alaplap": "alaplap", "logicboard": "alaplap",
            "tapegyseg": "tápegység", "tap": "tápegység", "lemezmeghajto": "lemezmeghajtó", "meghajto": "meghajtó",
            "haz": "ház", "hatlap": "hátlap", "keret": "keret", "burkolat": "burkolat", "fedlap": "fedlap",
            "billentyuzet": "billentyűzet", "keyboard": "billentyűzet", "touchpad": "touchpad", "kamera": "kamera",
            "kameramodul": "kamera", "hangszoro": "hangszóró", "toltoport": "töltőcsatlakozó",
            "toltocsatlakozo": "töltőcsatlakozó", "csatlakozo": "csatlakozó", "flex": "flexkábel",
            "zsaner": "zsanér", "panel": "panel", "modul": "modul", "szivofej": "szívófej", "kefe": "kefe",
            "kefefej": "kefefej", "tartaly": "tartály", "szuro": "szűrő", "porzsak": "porzsák"}.get(stem, stem)


# --- nem eredeti: másolat, bemutatódarab ---

# (A „bemutató darab”, „kirakati darab” egy üzlet bemutató példánya: eredeti, működik; a nem működő makett a „dummy”. A
# „klónozva” egy lemez tartalmának másolása, a Lego „Clone Trooper” egy figura.)
_NOT_ORIGINAL_RE = re.compile(r"(?<!nem )\b(?:dummy|replika\w*|replica|masolat\w*|hamisitvany\w*|fake|klon(?:ja|ok|t)?|"
                              r"clone(?! ?(?:wars?|troopers?|commanders?|captain|army|force|kommando\w*|katona\w*)\b))\b|"
                              r"\b1 ?: ?1\b|\baaa\+? ?(?:minoseg\w*|replika\w*)")
_AFTERMARKET_RE = re.compile(r"(?<!nem )\butangyartott\b")


def not_original(text: str) -> str:
    """A termék nem eredeti (másolat, hamisítvány, kirakati dummy), vagy utángyártott („utángyártott kijelző”): a szó,
    vagy „”. Egy utángyártott tartozékkal („utángyártott töltővel”) a készülék maga eredeti lehet: az nem ilyen."""
    folded = fold(text or "")
    match = _NOT_ORIGINAL_RE.search(folded)
    if match:
        return match.group(0)
    match = _AFTERMARKET_RE.search(folded)
    if match:
        after = folded[match.end():].split()
        if after and (_with_form(after[0]) or after[0].startswith("nelkul")):
            return ""
        return match.group(0)
    return ""


# --- szerszám: akku és töltő nélküli alapgép ---

TOOL_BRANDS = {"makita", "dewalt", "milwaukee", "bosch", "metabo", "ryobi", "einhell", "hilti", "festool", "aeg",
               "hikoki", "hitachi", "parkside", "worx", "stanley"}
_BODY_ONLY_RE = re.compile(r"\b(?:akku(?:mulator)?(?:ok)?(?: ?(?:es|\+|,) ?tolto)? nelkul\w*|"
                           r"tolto(?: ?(?:es|\+|,) ?akku\w*)? nelkul\w*|szolo|solo|alapgep\w*|body only|geptest|"
                           r"csak (?:a )?gep(?:test)?)\b")


_TOOL_RE = re.compile(r"\b(?:" + "|".join(sorted(TOOL_BRANDS)) + r")\b")


def body_only(text: str) -> str:
    """Egy szerszám akku és töltő nélkül (alapgép, „szóló”): ez más változat, mint a készlet (ára töredéke): a szó, vagy
    „”. Csak az ismert szerszámmárkáknál (TOOL_BRANDS)."""
    folded = fold(text or "")
    if not _TOOL_RE.search(folded):
        return ""
    match = _BODY_ONLY_RE.search(folded)
    return match.group(0) if match else ""
