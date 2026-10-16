"""Szövegtisztítás: a modell kimenetéből felolvasható, egyszerű szöveg lesz."""
from __future__ import annotations

import re
import unicodedata

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s*", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"(\*{1,3}|_{2,3}|~~)")
_WS_RE = re.compile(r"\s+")

_REPLACEMENTS = (
    ("°C", " fok"),
    ("%", " százalék"),
    (" & ", " és "),
    ("->", ", "),
    ("=>", ", "),
    ("→", ", "),
)


def strip_think(text: str | None) -> str:
    """Eltávolítja a <think>…</think> blokkot, ha a modell mégis a válaszba írná."""
    text = _THINK_RE.sub("", text or "")
    return text.replace("<think>", "").replace("</think>", "").strip()


def _is_emoji(ch: str) -> bool:
    cp = ord(ch)
    return cp >= 0x1F000 or 0x2600 <= cp <= 0x27BF or 0xFE00 <= cp <= 0xFE0F or cp == 0x200D


def clean_for_speech(text: str) -> str:
    """Markdown, linkek, emojik nélküli, felolvasható szöveg."""
    text = strip_think(text)
    text = _CODE_BLOCK_RE.sub(" A kódot a képernyőn látod. ", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub("a link", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = _HEADING_RE.sub("", text)
    text = _BULLET_RE.sub("", text)
    text = _EMPHASIS_RE.sub("", text)
    text = "".join(ch for ch in text if not _is_emoji(ch))
    for old, new in _REPLACEMENTS:
        text = text.replace(old, new)
    # Árak: "4 500 Ft" -> "4500 forint" (a szóközzel tagolt számot a felolvasó külön számoknak olvasná)
    text = re.sub(r"(?<=\d)[   ](?=\d{3}\b)", "", text)
    text = re.sub(r"(?<=\d)\s*Ft\b", " forint", text)

    # A felsorolások sorai mondatként hangozzanak el, ne folyjanak egybe.
    sentences = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line[-1] not in ".!?:;,":
            line += "."
        sentences.append(line)
    return _WS_RE.sub(" ", " ".join(sentences)).strip()


_BOUNDARY_RE = re.compile(r"[.!?…][\"'”»)\]]*\s+|\n+")
_ABBREVIATIONS = {
    "pl", "kb", "stb", "ill", "un", "dr", "u", "ui", "vo", "kft", "zrt", "bt", "nyrt", "ifj", "id",
    "ozv", "sz", "ny", "mr", "mrs", "ms", "etc", "eg", "ie", "vs", "jan", "febr", "marc", "apr",
    "jun", "jul", "aug", "szept", "okt", "nov", "dec", "max", "min", "tel", "ker", "krt",
}


class SentenceSplitter:
    """Folyamatosan érkező szövegből teljes mondatokat ad ki, hogy a felolvasás már az első
    mondatnál indulhasson. A magyar sorszámoknál (2026. október 14.) és rövidítéseknél
    (kb., pl.) nem vág."""

    def __init__(self, min_chars: int = 12):
        self.min_chars = min_chars
        self.buffer = ""

    def feed(self, text: str) -> list[str]:
        self.buffer += text
        sentences = []
        while True:
            cut = self._find_cut()
            if cut is None:
                return sentences
            sentence, self.buffer = self.buffer[:cut].strip(), self.buffer[cut:]
            if sentence:
                sentences.append(sentence)

    def flush(self) -> list[str]:
        rest, self.buffer = self.buffer.strip(), ""
        return [rest] if rest else []

    def _find_cut(self) -> int | None:
        for match in _BOUNDARY_RE.finditer(self.buffer):
            candidate = self.buffer[:match.start() + 1].strip()
            if len(candidate) < self.min_chars:
                continue
            if self.buffer[match.start()] == "." and self._keeps_going(candidate):
                continue
            return match.end()
        return None

    @staticmethod
    def _keeps_going(candidate: str) -> bool:
        """Igaz, ha a pont nem mondatvég: szám (sorszám, dátum), rövidítés vagy monogram."""
        words = candidate[:-1].split()
        if not words:
            return False
        last = fold(words[-1].strip("(\"'„"))
        return last.isdigit() or last in _ABBREVIATIONS or len(last) == 1


def fold(text: str) -> str:
    """Kisbetűs, ékezet nélküli alak összehasonlításhoz (pl. 'Számla' -> 'szamla')."""
    norm = unicodedata.normalize("NFKD", (text or "").casefold())
    return "".join(ch for ch in norm if not unicodedata.combining(ch))
