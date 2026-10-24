"""Szövegsegédek: a modell gondolkodásának levágása, összehasonlítás ékezet nélkül, összegek magyarul."""
from __future__ import annotations

import re
import unicodedata

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_think(text: str | None) -> str:
    """Eltávolítja a <think>…</think> blokkot, ha a modell mégis a válaszba írná."""
    text = _THINK_RE.sub("", text or "")
    return text.replace("<think>", "").replace("</think>", "").strip()


def fold(text: str) -> str:
    """Kisbetűs, ékezet nélküli alak összehasonlításhoz (pl. 'Számla' -> 'szamla')."""
    norm = unicodedata.normalize("NFKD", (text or "").casefold())
    return "".join(ch for ch in norm if not unicodedata.combining(ch))


# --- összegek magyarul (árak: „négyezer-ötszáz”, „4,5 ezer”, „3 és fél ezer”, „4.5k”) ----------------

# Számszavak részei (ékezet nélkül); a hosszabb előbb. Az „ezr” az „ezret”, „ezres” töve.
_NUMBER_PARTS = (
    ("masfel", 1.5), ("kilencven", 90), ("nyolcvan", 80), ("hetven", 70), ("hatvan", 60), ("otven", 50),
    ("negyven", 40), ("harminc", 30), ("huszon", 20), ("husz", 20), ("tizen", 10), ("tiz", 10),
    ("kilenc", 9), ("nyolc", 8), ("het", 7), ("hat", 6), ("ot", 5), ("negy", 4), ("harom", 3),
    ("ketto", 2), ("ket", 2), ("egy", 1), ("fel", 0.5), ("szaz", 100), ("ezer", 1000), ("ezr", 1000),
    ("millio", 1_000_000),
)
_MULTIPLIERS = {100, 1000, 1_000_000}
_HALVES = {0.5, 1.5}
# Ragok a számszó végén: „ötezerre”, „ezret”, „százat”, „ötezerért”, „ezres”
_NUMBER_SUFFIXES = {"", "t", "at", "et", "re", "ra", "ert", "ig", "es", "as", "os"}
# Ezek egymagukban mást is jelentenek (hát, hét, őt, egy kicsit, fel): így nem összegek
_AMBIGUOUS_WORDS = {"hat", "het", "ot", "egy", "fel"}
_CURRENCY_WORDS = {"ft", "huf", "forint", "forintot", "forintra", "forintert", "forintos"}
# Ha a válasz egyetlen számszó, ezek lehetnek mellette: „legyen negyven”
_FILLER_WORDS = {"legyen", "akkor", "mondjuk", "inkabb", "csak", "kerek", "a", "az", "ar", "ara", "osszesen"}
_GROUPED_RE = re.compile(r"\d{1,3}(?:[ .]\d{3})+")
_AMOUNT_TOKEN_RE = re.compile(r"\d{1,3}(?:[ .]\d{3})+(?![\d,]|\.\d)|\d+(?:[.,]\d+)?|[a-z]+|[,;.!?]")


def _fits(parts: list[float], value: float) -> bool:
    """Jöhet-e ez a rész az előzők után egy számszóban: „harmincöt” igen, „hatöt” és „félöt” nem."""
    if not parts:
        return True
    if value in _MULTIPLIERS:
        return parts[-1] != value
    if value in _HALVES:
        return False                       # a fél és a másfél csak elöl állhat („másfélezer”)
    last = parts[-1]
    return last in _MULTIPLIERS or (last >= 10 and last % 10 == 0 and value < 10)   # „huszonöt”


def _number_word(word: str) -> list[float] | None:
    """Egy számszó részei („negyezerotszaz” → [4, 1000, 5, 100]), vagy None, ha ez nem számszó."""
    def parse(rest: str, parts: list[float]) -> list[float] | None:
        if parts and rest in _NUMBER_SUFFIXES:
            return parts
        for text, value in _NUMBER_PARTS:
            if rest.startswith(text) and _fits(parts, value):
                found = parse(rest[len(text):], [*parts, value])
                if found is not None:
                    return found
        return None
    return parse(word, [])


def _joins(before: list[float], parts: list[float], digits_only: bool, after_and: bool) -> bool:
    """Folytatja-e a következő szó az összeget: „négyezer ötszáz”, „120 ezer”, „két millió”, „3 és fél ezer”."""
    if parts[0] in _HALVES:
        return after_and and not _HALVES & set(before)
    if parts[0] in _MULTIPLIERS:
        return before[-1] != parts[0]
    if digits_only:
        return False                       # „5000 öt”: két külön szám
    scale = max((p for p in before if p in _MULTIPLIERS), default=0)
    return before[-1] in _MULTIPLIERS and max((p for p in parts if p in _MULTIPLIERS), default=0) < scale


def _amount_value(parts: list[float]) -> float:
    total, current = 0.0, 0.0
    for value in parts:
        if value == 100:
            current = (current or 1) * 100
        elif value == 1000:
            total += (current or 1) * 1000
            current = 0.0
        elif value == 1_000_000:
            total = (total + (current or 1)) * 1_000_000
            current = 0.0
        else:
            current += value
    return total + current


def _amount_phrases(text: str) -> tuple[list[str], list[dict]]:
    """A válasz szavai és a benne talált összegek, sorrendben (lásd hu_amounts): mindegyiknek a részei, és hogy
    hányadik szótól hányadikig tart."""
    folded = re.sub(r"(?<=[a-z])-(?=[a-z])", "", fold(text or ""))
    tokens = _AMOUNT_TOKEN_RE.findall(folded)
    phrases: list[dict] = []        # egy-egy összeg: részei, és mely szavak tartoznak hozzá
    current: dict | None = None
    for i, token in enumerate(tokens):
        joined = current is not None and current["end"] == i
        if token[0].isdigit():
            value = float(re.sub(r"[ .]", "", token) if _GROUPED_RE.fullmatch(token) else token.replace(",", "."))
            current = {"parts": [value], "digits": True, "start": i, "end": i + 1, "words": []}
            phrases.append(current)
            continue
        if joined and token in ("e", "k") and current["digits"] and len(current["parts"]) == 1:
            current["parts"].append(1000)          # „5k”, „4.5k”, „5e”
            current["end"] = i + 1
            continue
        if joined and token == "es" and i + 1 < len(tokens) and tokens[i + 1].startswith(("fel", "masfel")):
            current["end"] = i + 1                 # „három és fél ezer”: az „és” is a szám része
            continue
        parts = _number_word(token) if token.isalpha() else None
        if parts is None:
            current = None
        elif joined and _joins(current["parts"], parts, current["digits"] and len(current["parts"]) == 1,
                               tokens[i - 1] == "es"):
            current["parts"] += parts
            current["words"].append(token)
            current["end"] = i + 1
        else:
            current = {"parts": parts, "digits": False, "start": i, "end": i + 1, "words": [token]}
            phrases.append(current)
    kept = []
    for phrase in phrases:
        after = tokens[phrase["end"]] if phrase["end"] < len(tokens) else ""
        if not (phrase["digits"] or _MULTIPLIERS & set(phrase["parts"]) or after in _CURRENCY_WORDS):
            rest = [t for j, t in enumerate(tokens) if t.isalnum() and not phrase["start"] <= j < phrase["end"]]
            alone = len(phrases) == 1 and all(t in _FILLER_WORDS for t in rest)
            if not alone or phrase["words"] in ([w] for w in _AMBIGUOUS_WORDS):
                continue
        kept.append(phrase)
    return tokens, kept


def hu_amounts(text: str) -> list[int]:
    """Az összegek egy írt vagy mondott válaszban, sorrendben: számjegyek („3 500”, „4,5 ezer”, „4.5k”, „5e”) és
    számszavak („ötezer”, „négyezer-ötszáz”, „másfél ezer”, „3 és fél ezer”, „félmillió”).

    Egy számszó csak akkor összeg, ha szorzó van benne (száz, ezer, millió), ha Ft követi, vagy ha egymagában ez
    a válasz („legyen negyven”). Így az „egy kicsit olcsóbb” nem 1 Ft, és a „hát legyen” sem 6 Ft."""
    return [int(round(_amount_value(phrase["parts"]))) for phrase in _amount_phrases(text)[1]]


def only_hu_amount(text: str) -> int | None:
    """Ha a szöveg egyetlen összeg (mellette legfeljebb pénznem vagy töltelékszó: „5 ezer Ft”, „legyen ötezer”), az
    összeg; különben None. Az ármezőhöz: a „hát”, a „38-as, piros” vagy a „4500 vagy 5000” nem egy ár."""
    tokens, phrases = _amount_phrases(text)
    if len(phrases) != 1:
        return None
    phrase = phrases[0]
    rest = [t for j, t in enumerate(tokens) if t.isalnum() and not phrase["start"] <= j < phrase["end"]]
    if any(t not in _CURRENCY_WORDS and t not in _FILLER_WORDS for t in rest):
        return None
    return int(round(_amount_value(phrase["parts"])))


def parse_hu_amount(text: str) -> int | None:
    """Az utolsó összeg a válaszban („ne 150 ezer legyen, hanem 120 ezer” → 120 000), vagy None."""
    amounts = hu_amounts(text)
    return amounts[-1] if amounts else None
