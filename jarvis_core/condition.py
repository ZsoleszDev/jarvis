"""Állapotfelismerés: egy hirdetés kijelzője, akkumulátora, működése, tartozékai és garanciája, külön-külön, mindegyik
megállapításhoz a leírásból vett bizonyítékkal (szó szerinti idézettel). Amiről a leírás nem szól, az nem „rendben”,
hanem ismeretlen (UNKNOWN): ez külön érték.

Két forrás:
- a modell (deals.CHECK_SCHEMA „condition” része, a jelölt végső ellenőrzésekor): from_model. Egy megállapítás csak
  akkor marad meg, ha az idézete tényleg ott van a hirdetés szövegében (különben ismeretlen lesz: a modell nem
  találhat ki állapotot). A fotóról csak hibát vagy hiányt állíthat (from_photo), jót nem.
- kulcsszavak, modell nélkül (from_text): az összehasonlító hirdetések leírásához (deals._vet), és a jelöltnél
  ellenőrzésnek. A kettő összevetésénél (merge) a rosszabb, bizonyítékkal alátámasztott állapot győz.

Ezek tények, nem árlevonások: hogy egy hiba mennyit von le az árból, ahhoz további alap kell (deals._reprice: hasonló
hibájú összehasonlító hirdetések ára, vagy egy megadott pótlási vagy javítási költség); ha nincs ilyen, az értékelés
bizonytalan marad (best_band, issues)."""
from __future__ import annotations

import re
import statistics
from typing import Any

from .textutil import fold

UNKNOWN = "ismeretlen"
ASPECTS = ("display", "battery", "function", "accessories", "warranty")
NAMES = {"display": "kijelző", "battery": "akkumulátor", "function": "működés", "accessories": "tartozékok",
         "warranty": "garancia"}
STATES = {
    "display": ("ép", "karcos", "hibás", UNKNOWN),
    "battery": ("jó", "gyenge", "hibás", UNKNOWN),
    "function": ("működik", "hibás", UNKNOWN),
    "accessories": ("teljes", "hiányos", UNKNOWN),
    "warranty": ("van", "nincs", UNKNOWN),
}
# A rosszabb állapot (a merge-ben a bizonyítékkal alátámasztott rosszabb győz).
_BAD = {"display": ("hibás", "karcos"), "battery": ("hibás", "gyenge"), "function": ("hibás",),
        "accessories": ("hiányos",), "warranty": ()}
_RANK = {"hibás": 3, "gyenge": 2, "karcos": 1, "hiányos": 2, "nincs": 0}
# A hiányzó tartozékok egységes neve (a modell és a kulcsszavak szavaiból).
_PART_WORDS = (("tolto", "töltő"), ("adapter", "töltő"), ("charger", "töltő"), ("tapegyseg", "töltő"),
               ("kabel", "kábel"), ("cable", "kábel"), ("doboz", "doboz"), ("box", "doboz"),
               ("kontroller", "kontroller"), ("controller", "kontroller"), ("joy-con", "kontroller"),
               ("joycon", "kontroller"), ("kijelzo", "kijelző"), ("kepernyo", "kijelző"), ("display", "kijelző"),
               ("akku", "akkumulátor"), ("battery", "akkumulátor"), ("tok", "tok"))
# A doboz hiánya önmagában nem hiba (a működéshez nem kell): csak megjegyzés, nem kell rá pótlási költség.
MINOR_PARTS = {"doboz", "tok"}
EVIDENCE_MAX = 160
MATCH_MIN = 2              # ennyi hasonló hibájú összehasonlító kell, hogy az ő áruk legyen a jelölt ársávja

CONDITION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **{aspect: {"type": "object", "properties": {
            "state": {"type": "string", "enum": list(STATES[aspect])},
            "evidence": {"type": "string"},
            "from_photo": {"type": "boolean"}}, "required": ["state", "evidence"]} for aspect in ASPECTS},
        "missing": {"type": "array", "items": {"type": "string"}},
    },
    "required": [*ASPECTS, "missing"],
}
CONDITION_RULES = (
    "condition: az állapot külön szempontonként: display (kijelző: ép, karcos, hibás), battery (akkumulátor: jó, gyenge, "
    "hibás), function (működés: működik, hibás), accessories (tartozékok: teljes, hiányos; a hiányzó darabok a missing "
    "listában, pl. „töltő”, „doboz”), warranty (garancia: van, nincs). Mindegyikhez evidence: egy rövid, szó szerinti "
    "idézet a hirdetés szövegéből, ami ezt mondja. Ne következtess és ne találgass: amiről a hirdetés nem ír, az "
    "ismeretlen, üres evidence-szel (pl. ha nem írja, hogy működik, a function ismeretlen). Fotó alapján csak sérülést "
    "vagy hiányt állíts (from_photo=true), jót nem."
)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w%]", " ", fold(text or "")).split())


def unknown() -> dict[str, Any]:
    """Minden szempont ismeretlen."""
    return {**{aspect: {"state": UNKNOWN, "evidence": ""} for aspect in ASPECTS}, "missing": [], "present": []}


def part_name(word: str) -> str:
    folded = fold(word or "").strip()
    for key, name in _PART_WORDS:
        if key in folded:
            return name
    return " ".join(str(word or "").split())[:30].lower()


def _quoted(evidence: str, text: str) -> bool:
    """Az idézet tényleg ott van-e a szövegben (ékezet, írásjel és kisbetű nélkül összevetve)."""
    quote = _norm(evidence)
    return len(quote) >= 3 and quote in _norm(text)


def from_model(data: Any, text: str) -> dict[str, Any]:
    """A modell válaszának condition része, ellenőrizve: egy megállapítás csak akkor marad meg, ha az idézete ott van a
    hirdetés szövegében (text), vagy ha fotón látott hibáról vagy hiányról szól (from_photo); különben ismeretlen. A
    hiányzó tartozékok csak akkor, ha a tartozékok megállapítása megmaradt."""
    out = unknown()
    if not isinstance(data, dict):
        return out
    for aspect in ASPECTS:
        entry = data.get(aspect) if isinstance(data.get(aspect), dict) else {}
        state = str(entry.get("state") or "").strip().lower()
        evidence = " ".join(str(entry.get("evidence") or "").split())[:EVIDENCE_MAX]
        if state not in STATES[aspect] or state == UNKNOWN:
            continue
        if evidence and _quoted(evidence, text):
            out[aspect] = {"state": state, "evidence": evidence}
        elif entry.get("from_photo") and state in _BAD[aspect]:
            out[aspect] = {"state": state, "evidence": evidence or "a fotón látszik", "photo": True}
    if out["accessories"]["state"] == "hiányos":
        missing = data.get("missing") if isinstance(data.get("missing"), list) else []
        out["missing"] = sorted({part_name(str(word)) for word in missing if str(word).strip()})[:6] or ["egyéb"]
    return out


# --- kulcsszavak (ékezet nélkül), modell nélkül -------------------------------------------------------------------

# (a kijelzővédő fólia nem a kijelző)
_SCREEN = r"(?:kijelzo(?!vedo|folia)\w*|kepernyo(?!vedo)\w*|display\w*|lcd|screen(?! ?protector)\w*|ernyo\w*)"
# a szavak között szóköz (vagy kettőspont, kötőjel), de vessző, pont nem: egy tagmondaton belül
_W = r"(?:\s+|\s*[:–-]\s*)"
_GAP = rf"{_W}(?:\w+\s+){{0,3}}?"
# A kijelző hibái (a hiba szava a „bad” csoport: a tagadást ehhez mérjük, _negated). A folt, a világosabb folt, a
# nyomáshiba, a beégés, a pixelhiba: a nyomás- és beégésnyom, a pixelhiba magában is a kijelzőé.
_DISPLAY_FAULT = (r"repedt|repedes\w*|torott|tores\w*|hibas\w*|csikos|beeg\w*|pixelhib\w*|halott pixel\w*|serult\w*|"
                  r"(?:vilagosabb |sotetebb |sarga |feher |fekete )?folt\w*|foltos|nyomashib\w*|nyomasfolt\w*|"
                  r"nyomod\w*|nem mukodik|nem jelenit\w*|szellemkep\w*")
_DISPLAY_BAD = re.compile(rf"\b{_SCREEN}{_GAP}(?P<bad>{_DISPLAY_FAULT})|"
                          rf"\b(?P<bad2>(?:vilagosabb |sotetebb |sarga |feher |fekete )?folt\w*|repedt|torott|hibas|"
                          rf"serult|csikos|beegett|beeges\w*|nyomashib\w*|nyomasfolt\w*|pixelhib\w*)\w*{_GAP}{_SCREEN}|"
                          rf"\b(?P<bad3>nyomashib\w*|nyomasfolt\w*|beeges\w*|pixelhib\w*|halott pixel\w*)")
_SCRATCH = r"(?:karcos|karcolt|karcolas\w*|kopott|karc(?!\s*(?:nelkul|mentes)))\b"
_DISPLAY_SCRATCH = re.compile(rf"\b{_SCREEN}{_GAP}(?P<bad>{_SCRATCH})|\b(?P<bad2>karcos|karcolt|kopott|karc)\w*"
                              rf"{_GAP}{_SCREEN}")
_DISPLAY_OK = re.compile(rf"\b{_SCREEN}{_GAP}(?:hibatlan|karcmentes|ep|tokeletes|serulesmentes|karc nelkul\w*|"
                         rf"foltmentes|hibamentes)|"
                         rf"\b(?:hibatlan|karcmentes|tokeletes|foltmentes|hibamentes)\w*{_GAP}{_SCREEN}")
_BATTERY = r"(?:akku\w*|akkumulator\w*|battery|elem\w*)"
_BATTERY_BAD = re.compile(rf"\b{_BATTERY}{_GAP}(?P<bad>hibas|halott|felfujodott|duzzadt|cserelendo|nem tolt)")
_BATTERY_WEAK = re.compile(rf"\b{_BATTERY}{_GAP}(?P<bad>gyenge|hamar lemerul\w*|gyorsan merul\w*|gyorsan lemerul\w*|"
                           rf"rossz|kopott|fogy\w*)")
_BATTERY_OK = re.compile(rf"\b{_BATTERY}{_GAP}(?:jo|kivalo|uj|tokeletes|hibatlan|csereltetett|cserelt|bírja|birja)")
_BATTERY_PCT = re.compile(rf"\b{_BATTERY}{_W}(?:\w+{_W}){{0,3}}?(\d{{2,3}}) ?%")
_FUNCTION_BAD = re.compile(r"\b(?P<bad>nem kapcsol be|nem indul\w*|nem mukodik|nem mukodo\w*|mukodeskeptelen|"
                           r"javitasra szorul\w*|alkatresz\w*|for parts|nem tolt)\b")
# a magában álló „hibátlan” (pl. „Hibátlan, dobozában.”) is a működésről szól; a „hibátlan kijelző” csak a kijelzőről
# egy tagmondat elején („Működik, …”, „egyébként minden működik”; a tagadottat _negated kiszűri)
_FUNCTION_OK = re.compile(r"(?:^|[.;!?,]\s*)(?:\w+\s+){0,2}?mukodik\b(?!\s*-\s*e\b)|"
                          r"\b(?:(?:hibatlanul|tokeletesen|jol|rendesen|kifogastalanul|100 ?%-?(?:ban|osan)) mukodik|mukodokepes|"
                          r"mukodo|"
                          r"mukodo allapot\w*|minden funkcio\w* mukodik|hibatlan mukodes\w*|mukodese hibatlan|"
                          r"hibatlan allapot\w*|"
                          r"(?<!nem )hibatlan(?!\s+(?:kijelzo|kepernyo|akku|kulso|hatlap|burkolat|doboz)))\b")
_PART = r"(?P<part>tolto\w*|adapter\w*|tapegyseg\w*|kabel\w*|doboz\w*|kontroller\w*|joy-?con\w*)"
# A hiányzó tartozék („töltő nélkül”, „nincs hozzá töltő”, „a töltője nincs meg”); a tagadott („a töltő nem
# hiányzik”) nem az (_negated: a hiány szava a „bad” csoport).
_MISSING = (re.compile(rf"\b{_PART}{_W}(?:\w+{_W}){{0,1}}?(?P<bad>nelkul\w*)"),
            re.compile(rf"\b(?P<bad>nincs|nem jar|nem tartozik|hianyzik|hianyoznak)\s+(?:(?:hozza|vele|mellette|meg|a|az)"
                       rf"\s+){{0,2}}{_PART}"),
            re.compile(rf"\b{_PART}{_W}(?:\w+{_W}){{0,1}}?(?P<bad>nincs|nem jar|nem tartozik|nem tartozek|hianyzik|"
                       rf"elveszett)\b"))
_PRESENT = re.compile(r"\b(?:eredeti\s+)?(toltovel|toltojevel|adapterrel|kabellel|dobozzal|dobozaval|dobozaban|"
                      r"tolto(?:je)? is|eredeti tolto\w*)\b")
_PRESENT_TOO = re.compile(rf"\b{_PART}\s+(?:is\s+)?(?:megvan|van|jar hozza|tartozik hozza)\b")
_COMPLETE = re.compile(r"\b(?:teljes|minden|osszes)\s+(?:eredeti\s+)?tartozek\w*|\bhianytalan\b|\bkomplett\b")
_WARRANTY_YES = re.compile(rf"\bgarancia\w*{_W}(?:\w+{_W}){{0,3}}?(?:20\d\d|ig\b|van\b|meg\b|ervenyes)|"
                           r"\bgarancialis\b|\bgaranciaval\b")
_WARRANTY_NO = re.compile(rf"\b(?:nincs|lejart|nem){_W}(?:\w+{_W}){{0,1}}?garancia\w*|\bgarancia\w*{_W}(?:nincs|lejart|"
                          r"nelkul)")


def _snippet(match: re.Match[str], folded: str, text: str, span: tuple[int, int] | None = None) -> str:
    """A találat szövege az eredetiből (az ékezettel), ha a hossz egyezik; különben az ékezet nélküli."""
    start, end = span or match.span()
    source = text if len(text) == len(folded) else folded
    return " ".join(source[start:end].split()).lstrip(".,;:!? ")[:EVIDENCE_MAX]


# Tagadás: „nincs folt a kijelzőn”, „a kijelzőn nincs folt”, „nem repedt a kijelző”, „se karc, se folt”, „a töltő nem
# hiányzik”, „folt nélkül”. A tagadószó a hiba szava előtt (ugyanabban a tagmondatban, legfeljebb NEG_WORDS szóval
# előbb), a talált szakaszon belül, vagy rögtön utána („folt nincs”, „folt sincs”, „karc nem látszik”).
_NEGATOR = re.compile(r"\b(?:nincs\w*|sincs\w*|nem|sem|se|semmi\w*|semmilyen\w*|egyetlen)\b")
_AFTER_NEG = re.compile(r"^\W*(?:\w+\s+)?(?:mentes\w*|nelkul\w*|nincs\w*|sincs\w*|nem lat\w*|nem eszrevehet\w*)\b")
_CLAUSE_END = re.compile(r"[.;!?,()\n]|\b(?:de|viszont|azonban|hanem|csak|kiveve)\b")
NEG_WORDS = 3


def _bad_span(match: re.Match[str]) -> tuple[int, int]:
    """A találat kulcsszava (a „bad” csoport; ha nincs ilyen, az utolsó szava: „kijelző nem hibátlan” → hibátlan)."""
    for name in ("bad", "bad2", "bad3"):
        if name in match.re.groupindex and match.group(name) is not None:
            return match.span(name)
    start, end = match.span()
    last = re.search(r"\w+\W*$", match.group(0))
    return (start + last.start(), end) if last else (start, end)


def _negated(folded: str, match: re.Match[str]) -> bool:
    """A talált hiba tagadva van-e (lásd _NEGATOR): a hiba szavát magát („nem működik”) nem számolja tagadásnak."""
    start, end = match.span()
    bad_start, bad_end = _bad_span(match)
    inside = folded[start:bad_start] + " " + folded[bad_end:end]
    if _NEGATOR.search(inside):
        return True
    if re.search(r"(?:mentes|nelkul)\w*$", folded[bad_start:bad_end]) and not \
            re.match(r"nelkul", folded[bad_start:bad_end]):
        return True                            # „foltmentes”, „hibamentes”
    before = folded[:start]
    cut = max((m.end() for m in _CLAUSE_END.finditer(before)), default=0)
    words = before[cut:].split()[-NEG_WORDS:] if not _CLAUSE_END.match(folded, start) else []
    if any(_NEGATOR.fullmatch(word) for word in words):
        return True                            # (a találat egy új tagmondattal kezdődik: az előző nem számít)
    after = folded[end:]
    stop = _CLAUSE_END.search(after)
    return bool(_AFTER_NEG.match(after[:stop.start()] if stop else after))


def _find(pattern: re.Pattern[str], folded: str) -> tuple[re.Match[str] | None, re.Match[str] | None]:
    """Az első nem tagadott találat, és az első tagadott (ha van): („nincs folt a kijelzőn” → (None, a tagadott))."""
    negated = None
    for match in pattern.finditer(folded):
        if not _negated(folded, match):
            return match, negated
        negated = negated or match
    return None, negated


def from_text(text: str) -> dict[str, Any]:
    """Az állapot a hirdetés szövegéből kulcsszavakkal (az idézet a talált szövegrész). Ami nem derül ki, ismeretlen.
    A tagadott hiba („nincs folt a kijelzőn”, „nem repedt”) nem hiba; a kijelzőnél és az akkunál ez a jó állapot
    bizonyítéka, ha más nem szól róla."""
    out = unknown()
    raw = " ".join(str(text or "").split())
    folded = fold(raw)

    def find(pattern: re.Pattern[str]) -> str:
        match, _negated_match = _find(pattern, folded)
        return _snippet(match, folded, raw) if match else ""

    def denial(match: re.Match[str]) -> str:
        """A tagadott hiba idézete a tagadószóval együtt („Nincs folt a kijelzőn”)."""
        start, end = match.span()
        before = folded[:start]
        cut = max((m.end() for m in _CLAUSE_END.finditer(before)), default=0)
        negators = list(_NEGATOR.finditer(before, cut))
        return _snippet(match, folded, raw, (negators[-1].start() if negators else start, end))

    for aspect, rules, good in (
            ("display", ((_DISPLAY_BAD, "hibás"), (_DISPLAY_SCRATCH, "karcos"), (_DISPLAY_OK, "ép")), "ép"),
            ("battery", ((_BATTERY_BAD, "hibás"), (_BATTERY_WEAK, "gyenge"), (_BATTERY_OK, "jó")), "jó"),
            ("function", ((_FUNCTION_BAD, "hibás"), (_FUNCTION_OK, "működik")), ""),
            ("warranty", ((_WARRANTY_NO, "nincs"), (_WARRANTY_YES, "van")), "")):
        denied = ""
        for pattern, state in rules:
            match, negated = _find(pattern, folded)
            if match is not None:
                out[aspect] = {"state": state, "evidence": _snippet(match, folded, raw)}
                break
            if negated is not None and state in _BAD[aspect] and not denied:
                denied = denial(negated)
        if out[aspect]["state"] == UNKNOWN and denied and good:
            out[aspect] = {"state": good, "evidence": denied}   # „nincs folt a kijelzőn”: a kijelző ép
    if out["battery"]["state"] == UNKNOWN:
        match = _BATTERY_PCT.search(folded)
        if match:
            percent = int(match.group(1))
            state = "jó" if percent >= 85 else "gyenge" if percent < 80 else UNKNOWN
            if state != UNKNOWN:
                out["battery"] = {"state": state, "evidence": _snippet(match, folded, raw)}
    missing, evidence, denied_parts = set(), "", set()
    for pattern in _MISSING:
        for match in pattern.finditer(folded):
            part = part_name(match.group("part"))
            if _negated(folded, match):
                denied_parts.add(part)         # „a töltő nem hiányzik”: megvan
                continue
            missing.add(part)
            evidence = evidence or _snippet(match, folded, raw)
    out["present"] = sorted(({part_name(m.group("part") if "part" in m.re.groupindex else m.group(1))
                              for pattern in (_PRESENT, _PRESENT_TOO) for m in pattern.finditer(folded)}
                             | denied_parts) - missing)
    if missing:
        out["accessories"] = {"state": "hiányos", "evidence": evidence}
        out["missing"] = sorted(missing)
    else:
        complete = find(_COMPLETE)
        if complete:
            out["accessories"] = {"state": "teljes", "evidence": complete}
    return out


def merge(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """Két megállapítás (pl. a modellé és a kulcsszavaké) összevetve: szempontonként az ismert az ismeretlen helyett, és
    ha mindkettő ismert, a rosszabb (a hibát nem írhatja felül egy „rendben”). A hiányzó tartozékok uniója."""
    out = unknown()
    for aspect in ASPECTS:
        a, b = first.get(aspect) or {}, second.get(aspect) or {}
        sa, sb = a.get("state", UNKNOWN), b.get("state", UNKNOWN)
        if sa == UNKNOWN:
            out[aspect] = dict(b) if sb != UNKNOWN else out[aspect]
        elif sb == UNKNOWN or _RANK.get(sa, -1) >= _RANK.get(sb, -1):
            out[aspect] = dict(a)
        else:
            out[aspect] = dict(b)
    out["missing"] = sorted(set(first.get("missing") or []) | set(second.get("missing") or []))
    if out["missing"] and out["accessories"]["state"] != "hiányos":
        source = first if first.get("missing") else second
        out["accessories"] = dict(source.get("accessories") or {"state": "hiányos", "evidence": ""})
    out["present"] = sorted((set(first.get("present") or []) | set(second.get("present") or [])) - set(out["missing"]))
    return out


# --- mit jelent az árra -----------------------------------------------------------------------------------------

def issues(cond: dict[str, Any] | None) -> list[str]:
    """Az árat érintő hibák (ezekhez kell alap: hasonló hibájú összehasonlító, vagy pótlási/javítási költség):
    „működés” (nem működik), „kijelző”, „akkumulátor”, és a hiányzó tartozékok (a doboz és a tok nem)."""
    cond = cond or {}
    out = []
    if (cond.get("function") or {}).get("state") == "hibás":
        out.append("működés")
    if (cond.get("display") or {}).get("state") == "hibás":
        out.append("kijelző")
    if (cond.get("battery") or {}).get("state") in ("gyenge", "hibás"):
        out.append("akkumulátor")
    out += [part for part in cond.get("missing") or [] if part not in MINOR_PARTS]
    return out


_ISSUE_WORDS = {"működés": "nem működik", "kijelző": "hibás a kijelzője",
                "akkumulátor": "gyenge vagy hibás az akkumulátora"}
REPAIRS = ("kijelző", "akkumulátor")    # ezekhez javítás kell (a hiányzó tartozék pótolható)


def issue_text(found: list[str]) -> str:
    """Az árat érintő hibák szövegesen: „hibás a kijelzője, nincs töltő”."""
    return ", ".join(_ISSUE_WORDS.get(issue, f"nincs {issue}") for issue in found)


def clean(cond: dict[str, Any] | None) -> bool:
    """Nincs az árat érintő hibája (az ismeretlen szempont nem hiba). Hibátlannak ez még kevés: clean_evidence."""
    return not issues(cond)


def clean_evidence(cond: dict[str, Any] | None) -> str:
    """Bizonyítottan hibátlan-e (egy összehasonlító „ellenőrzötten hibátlan” csak ezzel lehet): nincs az árat érintő
    hibája, és a leírása szerint működik (a „működik”, „hibátlan” idézettel). Az idézet, vagy „”, ha a leírás nem
    mond ilyet: egy az állapotáról nem író hirdetés ismeretlen állapotú, nem hibátlan."""
    cond = cond or {}
    if issues(cond):
        return ""
    function = cond.get("function") or {}
    return str(function.get("evidence") or "működik") if function.get("state") == "működik" else ""


def best_band(wanted: list[str], refs: list[dict[str, Any]], reference: int,
              low: int | None) -> tuple[set[str], int, int | None, int]:
    """A jelölt ársávja a hibái szerint (wanted): ha legalább MATCH_MIN összehasonlítónak pontosan ugyanazok a hibái
    vannak (vagy egy részük, és több hibát fed le, mint a többi), az ő áruk (középérték, a legolcsóbb mint alsó határ);
    különben a hibátlanoké (reference, low). Visszaadja: melyik hibákat fedi le az ár (ezekre nincs levonás, így semmi nem
    vonódik le kétszer), a szokásos árat, az alsó határt, és hány összehasonlítóból számolt (0: a hibátlanokéból)."""
    best: tuple[set[str], int, int | None, int] = (set(), reference, low, 0)
    if not wanted:
        return best
    groups: dict[frozenset[str], list[int]] = {}
    for ref in refs:
        own = frozenset(ref.get("issues") or [])
        if own and own <= set(wanted):
            groups.setdefault(own, []).append(int(ref["p"]))
    for own, prices in groups.items():
        if len(prices) >= MATCH_MIN and len(own) > len(best[0]):
            best = (set(own), int(statistics.median(prices)), min(prices), len(prices))
    return best


def state_text(cond: dict[str, Any] | None, aspect: str) -> str:
    entry = (cond or {}).get(aspect) or {}
    state = entry.get("state") or UNKNOWN
    if aspect == "accessories" and state == "hiányos":
        return "hiányzik: " + ", ".join(cond.get("missing") or ["egyéb"])
    return state


def describe(cond: dict[str, Any] | None, known_only: bool = True) -> str:
    """Rövid szöveg az értesítéshez: „kijelző: hibás („repedt a kijelző”); tartozékok: hiányzik: töltő („töltő
    nélkül”)”. known_only: csak az ismert szempontok."""
    parts = []
    for aspect in ASPECTS:
        entry = (cond or {}).get(aspect) or {}
        if entry.get("state", UNKNOWN) == UNKNOWN and known_only:
            continue
        evidence = entry.get("evidence") or ""
        quote = f" („{evidence}”)" if evidence and not entry.get("photo") else " (a fotón)" if entry.get("photo") else ""
        parts.append(f"{NAMES[aspect]}: {state_text(cond, aspect)}{quote}")
    return "; ".join(parts)


def compact(cond: dict[str, Any] | None) -> dict[str, Any]:
    """Tároláshoz és a felületnek: a szempontok (állapot, idézet), a hiányzók, és az árat érintő hibák."""
    cond = cond or unknown()
    return {**{aspect: dict(cond.get(aspect) or {"state": UNKNOWN, "evidence": ""}) for aspect in ASPECTS},
            "missing": list(cond.get("missing") or []), "present": list(cond.get("present") or []),
            "issues": issues(cond)}
