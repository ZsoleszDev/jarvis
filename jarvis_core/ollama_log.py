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

from .memory import GB, MemoryReport, OllamaProcess, gb, process_env_note

CACHE_SETTING = "LLAMA_ARG_CACHE_RAM"
CHECKPOINT_SETTING = "LLAMA_ARG_CTX_CHECKPOINTS"
PARALLEL_SETTING = "OLLAMA_NUM_PARALLEL"   # hány kérést dolgoz fel egyszerre (alapból egyet)
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


def _count(value: str | None) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def server_parallel(proc: OllamaProcess, user: str | None) -> tuple[int, str]:
    """Hány kérést dolgoz fel egyszerre a futó Ollama (OLLAMA_NUM_PARALLEL; alapból egyet): a környezetéből, ha
    kiolvasható, különben a felhasználói beállításból (setx). A második érték egy tanács (üres, ha nincs mit
    tenni): ha a beállítás megvan, de a futó Ollama még előtte indult, azt mondja meg."""
    wanted = _count(user)
    if proc.found and proc.readable:
        running = _count(proc.env.get(PARALLEL_SETTING)) or 1
        if wanted and wanted > running:
            return running, (f"Az {PARALLEL_SETTING} be van állítva ({wanted}), de a futó Ollama még előtte indult: "
                             "lépj ki az Ollamából a tálcán (jobb klikk az ikonjára), indítsd újra a Start menüből, "
                             "és utána a Jarvist is.")
        return running, ""
    return wanted or 1, ""


def explain(cause: Cause | None, path: Path | None, user: dict[str, str | None],
            memory: MemoryReport | None = None, ollama: OllamaProcess | None = None,
            repeated: bool = False) -> list[tuple[str, str]]:
    """A leállás oka és a javítás, a gép tényleges állapota szerint: (szint, szöveg) sorok a naplóba, ahol a
    szint "warn" vagy "info". `user`: a felhasználói beállítások (setx) a CACHE_SETTING és CHECKPOINT_SETTING
    kulcson. `memory`, `ollama`: a gép memóriája és a futó Ollama (memory.py). `repeated`: erről az okról már
    szóltunk részletesen, most rövidebben (de a friss számokkal és a javítással: lehet, hogy a másik menüben
    hangzott el)."""
    facts = [("info", "  " + line) for line in (memory.lines() if memory is not None else [])]
    if cause is None:
        where = path or "journalctl -u ollama"
        lines = [("warn", f"Az okát az Ollama naplójában látod: {where}.")]
        if memory is not None and memory.commit_full:
            return lines + facts + [("warn", text) for text in _memory_fixes(memory)]
        return lines + [("warn", f"Ha ott „std::bad_alloc” áll, elfogyott a gép memóriája; ez ellen: PowerShellben "
                                 f"„setx {CACHE_SETTING} 0”, majd lépj ki az Ollamából a tálcán, és indítsd újra.")]
    if cause.kind == "ram":
        head = ("A leállás oka megint: elfogyott a gép memóriája." if repeated else
                f"Az ok az Ollama naplója szerint: elfogyott a gép memóriája ({cause.line}).")
        return [("warn", head), *facts, *[("warn", text) for text in _ram_fixes(user, memory, ollama, repeated)]]
    if cause.kind == "vram":
        if repeated:
            return [("warn", "A leállás oka megint: elfogyott a videókártya memóriája (más program is foglalja?).")]
        return [("warn", f"Az ok az Ollama naplója szerint: elfogyott a videókártya memóriája ({cause.line}). "
                         "Valószínűleg más program is foglalja a kártyát (játék, videó, böngésző); zárd be. Ha "
                         "gyakori, vedd kisebbre a kontextust ([llm] num_ctx a config.toml-ban).")]
    if repeated:
        return [("warn", f"A leállás oka megint: belső hiba az Ollamában ({cause.line}).")]
    return [("warn", f"Az ok az Ollama naplója szerint: belső hiba ({cause.line}). Ez az Ollama (llama.cpp) "
                     "hibája; ha gyakori, frissítsd az Ollamát.")]


def _ram_fixes(user: dict[str, str | None], memory: MemoryReport | None, ollama: OllamaProcess | None,
               repeated: bool) -> list[str]:
    fixes = []
    if user.get(CACHE_SETTING) != "0":
        if repeated:
            fixes.append(f"Javítás: „setx {CACHE_SETTING} 0” és „setx {CHECKPOINT_SETTING} 4” PowerShellben, majd "
                         "az Ollama újraindítása (README, Hibaelhárítás).")
        else:
            fixes.append("Az Ollama a beszélgetések elejét a gép memóriájában is megőrzi (alapból akár 8 GB-ot), és "
                         "menüváltáskor ez egyszerre több gigabájtot foglal. Javítás (egyszer kell): PowerShellben "
                         f"„setx {CACHE_SETTING} 0” és „setx {CHECKPOINT_SETTING} 4”, majd lépj ki az Ollamából a "
                         "tálcán (jobb klikk az ikonjára), és indítsd újra. Utána menüváltáskor az első válasz pár "
                         "másodperccel tovább tarthat.")
    else:
        stale = process_env_note(ollama, {k: v for k, v in user.items() if v}) if ollama is not None else ""
        if not user.get(CHECKPOINT_SETTING):
            fixes.append(f"Vedd kisebbre az Ollama ellenőrzőpontjait is: „setx {CHECKPOINT_SETTING} 4” PowerShellben"
                         + ("." if stale else ", majd indítsd újra az Ollamát."))
        if stale:
            fixes.append(stale)
        elif ollama is None or not ollama.readable:
            fixes.append(f"A {CACHE_SETTING} már 0; ha azóta nem indítottad újra az Ollamát, lépj ki belőle a "
                         "tálcán, és indítsd újra.")
    forced = ollama.flags.get(CACHE_SETTING) if ollama is not None else None
    if forced not in (None, "0"):
        fixes.append(f"Az Ollama a modellfolyamatnak maga adja meg: --cache-ram {forced}. Ez erősebb a "
                     f"{CACHE_SETTING}-nál, ezért az nem hat; ezen csak az Ollama frissítése segíthet.")
    if memory is not None:
        fixes += _memory_fixes(memory)
    if not fixes:
        fixes.append("Zárj be más, sok memóriát foglaló programokat, és ha gyakori, vedd kisebbre a kontextust "
                     "([llm] num_ctx a config.toml-ban).")
    return fixes


BROWSERS = {"msedge", "chrome", "chromium", "firefox", "brave", "opera"}


def pagefile_tip(memory: MemoryReport, limit_gb: float = 8.0) -> str:
    """Ha nincs lapozófájl, vagy kicsi (vagy kézzel megadott, és betelt): mit állítson be."""
    maximum = memory.pagefile_max_gb
    if maximum is None or not (maximum < limit_gb or memory.commit_full):
        return ""
    what = "nincs lapozófájl" if maximum == 0 else f"a lapozófájl legfeljebb {maximum:.0f} GB"
    return (f"A Windows-on {what}, ezért a RAM-on felül nem tud több memóriát adni a programoknak. Állítsd a "
            "Windowsra: Win+R → SystemPropertiesPerformance → Speciális → Virtuális memória: Módosítás → „A "
            "lapozófájl méretének automatikus kezelése minden meghajtón”, majd indítsd újra a gépet.")


def _memory_fixes(memory: MemoryReport) -> list[str]:
    fixes = []
    tip = pagefile_tip(memory)
    if tip:
        fixes.append(tip)
    elif memory.commit_full and memory.pagefile == "auto":
        fixes.append("A lapozófájlt a Windows kezeli, mégis betelt: nézd meg, van-e elég szabad hely a C: "
                     "meghajtón (a lapozófájl ott nő).")
    browser = next((t for t in memory.top if t[0].lower().split(".")[0] in BROWSERS), None)
    if browser is not None and browser[1] >= 3 * GB:
        fixes.append(f"A böngésző ({browser[0]}) {gb(browser[1])} GB-ot foglal: zárd be a fölösleges lapokat. A "
                     "Jarvis böngészőjét is bezárhatod, a következő feladatnál újraindítja.")
    return fixes
