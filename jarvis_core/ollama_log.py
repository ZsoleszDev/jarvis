"""Miért állt le az Ollama modellfolyamata? Az Ollama naplója (server.log) megmondja; ebből mondjuk meg
érthetően, és azt is, mit lehet tenni ellene.

A leggyakoribb ok az újabb Ollamában (0.40-től, beépített llama-server): elfogy a gép memóriája
(std::bad_alloc). A llama-server a beszélgetések elejét a rendszermemóriában is megőrzi (prompt-gyorsítótár,
alapból legfeljebb 8 GB, a hibrid modelleknél ellenőrzőpontokkal együtt), és menüváltáskor (Beszélgetés ↔
Munka) egyszerre több gigabájtot foglal. A böngésző és a többi program mellett ez már nem mindig fér el:
a modellfolyamat leáll, és az Ollama 20–40 másodperc alatt újratölti. A gyorsítótár kikapcsolható
(LLAMA_ARG_CACHE_RAM=0); ára, hogy menüváltáskor az első válasz pár másodperccel tovább tart.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

CACHE_SETTING = "LLAMA_ARG_CACHE_RAM"
CHECKPOINT_SETTING = "LLAMA_ARG_CTX_CHECKPOINTS"
READ_LIMIT = 512 * 1024      # a napló végéből legfeljebb ennyit olvasunk

# Az okok a valószínűség sorrendjében; mindegyiknél az utolsó illeszkedő sor számít.
CAUSES = (
    ("ram", re.compile(r"bad_alloc|bad allocation|cannot allocate memory|not enough memory|"
                       r"failed to allocate (?:\S+ )?(?:CPU|host)|0xc0000017", re.IGNORECASE)),
    ("vram", re.compile(r"cudaMalloc failed|CUDA error: out of memory|cudaErrorMemoryAllocation|"
                        r"failed to allocate (?:\S+ )?CUDA|allocating [\d.]+ MiB on device \d+: cudaMalloc",
                        re.IGNORECASE)),
    ("crash", re.compile(r"GGML_ASSERT|GGML_ABORT|segmentation fault|SIGSEGV|SIGABRT|access violation|"
                         r"0xc0000005|0xc0000409|panic:", re.IGNORECASE)),
)


@dataclass
class Cause:
    kind: str        # "ram" (a gép memóriája), "vram" (a videókártyáé), "crash" (belső hiba)
    line: str        # a naplóból az erre utaló sor (rövidítve)


def log_path() -> Path | None:
    """Az Ollama naplója: Windows-on %LOCALAPPDATA%\\Ollama\\server.log, macOS-en ~/.ollama/logs/server.log.
    Linuxon a rendszernaplóban van (journalctl -u ollama), azt nem olvassuk."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        return Path(base) / "Ollama" / "server.log" if base else None
    if sys.platform == "darwin":
        return Path.home() / ".ollama" / "logs" / "server.log"
    return None


def find_cause(text: str) -> Cause | None:
    """A napló egy részéből: miért állt le a modellfolyamat (None, ha nem derül ki)."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for kind, pattern in CAUSES:
        for line in reversed(lines):
            if pattern.search(line):
                return Cause(kind, line[:240])
    return None


class ServerLog:
    """Az Ollama naplójának az a része, ami egy adott pont (mark) óta íródott: így egy régi leállás sorai
    nem keverednek a mostaniba."""

    def __init__(self, path: Path | None = None):
        self.path = log_path() if path is None else Path(path)
        self.offset = 0

    def _size(self) -> int:
        try:
            return self.path.stat().st_size if self.path is not None else 0
        except OSError:
            return 0

    def mark(self) -> None:
        self.offset = self._size()

    def read_new(self) -> str:
        if self.path is None:
            return ""
        size = self._size()
        start = self.offset if self.offset <= size else 0   # kisebb lett: az Ollama újraindult, új napló
        start = max(start, size - READ_LIMIT)
        try:
            with open(self.path, "rb") as handle:
                handle.seek(start)
                data = handle.read(size - start)
        except OSError:
            return ""
        return data.decode("utf-8", "replace")


def user_setting(name: str) -> str | None:
    """Egy felhasználói környezeti változó értéke. Windows-on a regisztrációs adatbázisból, mert a setx
    után az újonnan induló programok (pl. az újraindított Ollama) már látják, a Jarvis még nem."""
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                return str(winreg.QueryValueEx(key, name)[0])
        except OSError:
            pass
    return os.environ.get(name)


def advice(cause: Cause | None, path: Path | None, cache_off: bool) -> str:
    """Mit mondjunk a felhasználónak a leállás okáról és a javításról (egyszer, a részletes változat)."""
    if cause is None:
        where = f"az Ollama naplójában látod: {path}" if path else "az Ollama naplójában látod (journalctl -u ollama)"
        return (f"Az okát {where}. Ha ott „std::bad_alloc” áll, elfogyott a gép memóriája; ez ellen: PowerShellben "
                f"„setx {CACHE_SETTING} 0”, majd lépj ki az Ollamából a tálcán, és indítsd újra.")
    if cause.kind == "ram":
        if cache_off:
            return (f"Az ok az Ollama naplója szerint: elfogyott a gép memóriája ({cause.line}), pedig a "
                    f"{CACHE_SETTING} már 0. Ha azóta nem indítottad újra az Ollamát, lépj ki belőle a tálcán, és "
                    "indítsd újra; ha igen, zárj be más, sok memóriát foglaló programokat (pl. a böngészőben a "
                    f"fölösleges lapokat), vagy vedd kisebbre az ellenőrzőpontokat: „setx {CHECKPOINT_SETTING} 4”.")
        return (f"Az ok az Ollama naplója szerint: elfogyott a gép memóriája ({cause.line}). Az Ollama a "
                "beszélgetések elejét a gép memóriájában is megőrzi (alapból akár 8 GB-ot), és menüváltáskor ez "
                "egyszerre több gigabájtot foglal; a böngésző és a többi program mellett ez már nem fér el. "
                f"Javítás (egyszer kell): PowerShellben „setx {CACHE_SETTING} 0” és „setx {CHECKPOINT_SETTING} 4”, "
                "majd lépj ki az Ollamából a tálcán (jobb klikk az ikonjára), és indítsd újra. Utána menüváltáskor "
                "az első válasz pár másodperccel tovább tarthat, de a modell nem fog leállni.")
    if cause.kind == "vram":
        return (f"Az ok az Ollama naplója szerint: elfogyott a videókártya memóriája ({cause.line}). Valószínűleg "
                "más program is foglalja a kártyát (játék, videó, böngésző); zárd be. Ha gyakori, vedd kisebbre "
                "a kontextust ([llm] num_ctx a config.toml-ban).")
    return (f"Az ok az Ollama naplója szerint: belső hiba ({cause.line}). Ez az Ollama (llama.cpp) hibája; ha "
            "gyakori, frissítsd az Ollamát.")


def short_advice(cause: Cause, cache_off: bool) -> str:
    """Az ismételt leállásokhoz: röviden az ok és a javítás (a részletes változat már elhangzott, de lehet,
    hogy a másik menü naplójában)."""
    if cause.kind == "ram":
        if cache_off:
            return (f"A leállás oka megint: elfogyott a gép memóriája (a {CACHE_SETTING} már 0; ha azóta nem "
                    "indítottad újra az Ollamát, indítsd újra).")
        return (f"A leállás oka megint: elfogyott a gép memóriája. Javítás: „setx {CACHE_SETTING} 0” és "
                f"„setx {CHECKPOINT_SETTING} 4” PowerShellben, majd az Ollama újraindítása (README, Hibaelhárítás).")
    if cause.kind == "vram":
        return "A leállás oka megint: elfogyott a videókártya memóriája (más program is foglalja?)."
    return f"A leállás oka megint: belső hiba az Ollamában ({cause.line})."
