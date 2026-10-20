"""Beállítások betöltése a config.toml fájlból.

Minden beállításnak van alapértéke, így a config.toml-ból bármi elhagyható.
"""
from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_DIR / "config.toml"
# Régi beállítások, amiket már nem használunk: csendben kihagyjuk őket (nem hiba).
RETIRED_KEYS = {("work", "vision_photos")}


@dataclass
class AssistantCfg:
    name: str = "Jarvis"
    greeting: str = "Jarvis online. Miben segíthetek?"
    system_prompt_file: str = "system_prompt.txt"
    max_history_turns: int = 12
    max_tool_rounds: int = 20
    keep_page_views: int = 1
    show_timings: bool = True
    stream: bool = True
    fillers: bool = True


@dataclass
class LlmCfg:
    host: str = "http://localhost:11434"
    model: str = "qwen3.8:27b"
    think: bool = False
    temperature: float = 0.6
    keep_alive: int | str = -1
    unload_on_exit: bool = True
    num_ctx: int = 16384
    min_ctx: int = 8192
    request_timeout: float = 180.0
    warm_up: bool = True       # induláskor előre feldolgozza a rendszerpromptot (gyorsabb első válasz)


@dataclass
class SttCfg:
    enabled: bool = True   # false: nincs mikrofon és beszédfelismerés (mint a --text), csak gépelés
    device: str = "auto"
    language: str = "hu"
    gpu_model: str = "turbo"
    gpu_compute_type: str = "int8_float16"
    gpu_device_index: int = 1
    cpu_model: str = "small"
    cpu_compute_type: str = "int8"
    cpu_threads: int = 8
    beam_size: int = 1
    initial_prompt: str = ""


@dataclass
class AudioCfg:
    input_device: str | int = ""
    output_device: str | int = ""
    silence_seconds: float = 0.8
    no_speech_timeout: float = 6.0
    follow_up_seconds: float = 4.0
    max_record_seconds: float = 30.0
    min_threshold: float = 0.01
    threshold_multiplier: float = 3.0


@dataclass
class TtsCfg:
    engine: str = "piper"
    voice: str = "hu_HU-imre-medium"
    voices_dir: str = "voices"
    length_scale: float = 1.0
    volume: float = 1.0


@dataclass
class ToolsCfg:
    allowed_folders: list[str] = field(
        default_factory=lambda: ["~/Documents", "~/Downloads", "~/Desktop"]
    )
    notes_dir: str = "~/Documents/Jarvis jegyzetek"
    max_read_chars: int = 6000
    allow_powershell: bool = False
    powershell_timeout: float = 30.0


@dataclass
class WebCfg:
    search_region: str = "hu-hu"
    max_results: int = 5
    read_max_chars: int = 6000
    timeout: float = 15.0
    allow_local: bool = False


@dataclass
class BrowserCfg:
    enabled: bool = True
    headless: bool = False
    channel: str = "msedge"
    executable_path: str = ""
    profile_dir: str = ""      # üres = az adatmappában (lásd data_dir), így a bejelentkezések megmaradnak
    width: int = 1280
    height: int = 800
    max_elements: int = 60
    confirm_risky: bool = True
    # Emberi tempó: egérmozgás, billentyűnkénti gépelés, szünetek (a modell nélküli lépéseknél
    # gondolkodási és olvasási szünetek is). pace: a szünetek szorzója (0 = szünetek nélkül).
    human_pace: bool = True
    pace: float = 1.0


@dataclass
class ScreenCfg:
    enabled: bool = True
    monitor: int = 1
    max_width: int = 1600


@dataclass
class GuiCfg:
    window: bool = True
    browser: str = "msedge"
    port: int = 0
    exit_on_close: bool = True
    gpu_bars: bool = True          # a videókártyák terhelése az ablak tetején (kártyánként egy vékony sáv)


@dataclass
class WakeWordCfg:
    enabled: bool = True
    model: str = "hey_jarvis"
    threshold: float = 0.5
    chime: bool = True
    cooldown: float = 2.0


@dataclass
class WorkCfg:
    enabled: bool = True
    dir: str = ""              # üres = az adatmappában (lásd data_dir)
    prompt_file: str = "work_prompt.txt"
    platforms: list[str] = field(default_factory=lambda: ["vinted", "jofogas", "vatera", "hardverapro"])
    research_sites: list[str] = field(default_factory=lambda: ["vinted", "jofogas", "vatera", "hardverapro"])
    max_tool_rounds: int = 40
    max_photos: int = 20
    replay: bool = True        # a begyakorolt feltöltés/levétel visszajátszása modell nélkül
    # A Vinted űrlapját a Jarvis maga tölti ki (vinted.py); a modell csak a kategóriát választja ki.
    autofill: bool = True
    # Két közzététel (vagy levétel) között ugyanazon az oldalon legalább ennyi perc teljen el
    # (véletlenszerűen ±30%), ahogy egy embernél. 0 = nincs várakozás.
    publish_gap_minutes: float = 2.0
    # A Munka menü beszélgetésének mintavétele: kisebb, mint a Beszélgetésé ([llm] temperature), hogy az
    # űrlapkitöltésnél (elemszámok, mezőértékek) kevesebb legyen a véletlen. A modellt nem tölti újra.
    temperature: float = 0.3


@dataclass
class CreativeCfg:
    enabled: bool = True
    language: str = "en"       # az új történetek alapnyelve: "en" (angol) vagy "hu" (magyar)


@dataclass
class StandbyCfg:
    # Készenléti indítás (python jarvis.py --standby, pl. a gép bekapcsolásakor): a modellek nem
    # töltődnek be, amíg nem szólsz. Ha ennyi percig nem használod, újra készenlétbe áll (0 = soha).
    idle_minutes: float = 15.0


@dataclass
class SchedulerCfg:
    enabled: bool = True
    quiet_minutes: float = 5.0     # a legutóbbi beszélgetés után ennyi ideig nem indít háttérmunkát
    active_hours: str = ""         # pl. "08:00-20:00"; üres = bármikor, amíg a Jarvis fut
    watch_minutes: float = 60.0    # a figyelők (mentett keresések) ellenőrzése
    stale_days: int = 14           # ennyi nap után szól, ha egy hirdetés nem kelt el (0 = soha)
    listing_checks: int = 5        # naponta ennyi fent lévő hirdetést néz meg: elkelt-e, eltűnt-e (0 = soha)
    message_minutes: float = 120.0  # ilyen sűrűn nézi meg a vevői üzeneteket, csak olvasva (0 = soha)
    deal_minutes: float = 180.0    # automata kereskedés: az új keresések gyakorisága (0 = a háttérben soha)
    message_reads: int = 3         # egy körben legfeljebb ennyi olvasatlan beszélgetést nyit meg
    speak: bool = True             # a háttérmunka eredményét fel is olvassa


@dataclass
class ApiCfg:
    enabled: bool = True
    host: str = "127.0.0.1"        # Tailscale-lel így marad: a „tailscale serve” innen veszi át
    port: int = 8765
    allowed_origins: list[str] = field(default_factory=lambda: [
        "capacitor://localhost", "ionic://localhost", "http://localhost", "https://localhost",
        "http://localhost:4200", "http://localhost:8100"])


@dataclass
class Config:
    assistant: AssistantCfg = field(default_factory=AssistantCfg)
    llm: LlmCfg = field(default_factory=LlmCfg)
    stt: SttCfg = field(default_factory=SttCfg)
    audio: AudioCfg = field(default_factory=AudioCfg)
    tts: TtsCfg = field(default_factory=TtsCfg)
    tools: ToolsCfg = field(default_factory=ToolsCfg)
    web: WebCfg = field(default_factory=WebCfg)
    browser: BrowserCfg = field(default_factory=BrowserCfg)
    screen: ScreenCfg = field(default_factory=ScreenCfg)
    gui: GuiCfg = field(default_factory=GuiCfg)
    wake_word: WakeWordCfg = field(default_factory=WakeWordCfg)
    work: WorkCfg = field(default_factory=WorkCfg)
    creative: CreativeCfg = field(default_factory=CreativeCfg)
    scheduler: SchedulerCfg = field(default_factory=SchedulerCfg)
    standby: StandbyCfg = field(default_factory=StandbyCfg)
    api: ApiCfg = field(default_factory=ApiCfg)
    apps: dict[str, str] = field(default_factory=dict)
    source: Path | None = None
    warnings: list[str] = field(default_factory=list)
    # A háttérmunka, a figyelők, az értesítések és a telefonos párosítás adatai. None = az adatmappa
    # (lásd data_dir); a tesztek egy ideiglenes mappát adnak meg, hogy a valódihoz ne nyúljanak.
    data_root: Path | None = None


_SECTIONS: dict[str, type] = {
    "assistant": AssistantCfg,
    "llm": LlmCfg,
    "stt": SttCfg,
    "audio": AudioCfg,
    "tts": TtsCfg,
    "tools": ToolsCfg,
    "web": WebCfg,
    "browser": BrowserCfg,
    "screen": ScreenCfg,
    "gui": GuiCfg,
    "wake_word": WakeWordCfg,
    "work": WorkCfg,
    "creative": CreativeCfg,
    "scheduler": SchedulerCfg,
    "standby": StandbyCfg,
    "api": ApiCfg,
}


def _build(cls: type, data: Any, section: str, warnings: list[str]) -> Any:
    if not isinstance(data, dict):
        warnings.append(f"[{section}] nem táblázat, az alapértékeket használom.")
        return cls()
    known = {f.name for f in fields(cls)}
    for key in data:
        if key not in known and (section, key) not in RETIRED_KEYS:
            warnings.append(f"Ismeretlen beállítás: [{section}] {key} (kihagyva)")
    return cls(**{k: v for k, v in data.items() if k in known})


def load_config(path: str | Path | None = None) -> Config:
    """Betölti a config.toml-t; ami hiányzik belőle, az alapértéket kap."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    warnings: list[str] = []
    data: dict[str, Any] = {}
    if cfg_path.exists():
        with open(cfg_path, "rb") as fh:
            try:
                data = tomllib.load(fh)
            except tomllib.TOMLDecodeError as exc:
                raise SystemExit(f"Hibás config.toml ({cfg_path}): {exc}") from exc
    else:
        warnings.append(f"Nem található {cfg_path}, alapértékekkel indulok.")

    cfg = Config(source=cfg_path if cfg_path.exists() else None, warnings=warnings)
    for name, cls in _SECTIONS.items():
        if name in data:
            setattr(cfg, name, _build(cls, data[name], name, warnings))

    apps = data.get("apps", {})
    if isinstance(apps, dict):
        cfg.apps = {str(k): str(v) for k, v in apps.items()}

    for key in data:
        if key not in _SECTIONS and key != "apps":
            warnings.append(f"Ismeretlen szakasz: [{key}] (kihagyva)")
    return cfg


def resolve_path(value: str | Path) -> Path:
    """Kibontja a ~ és %VÁLTOZÓ% részeket; a relatív út a projektmappához képest értendő."""
    p = Path(os.path.expandvars(os.path.expanduser(str(value))))
    return p if p.is_absolute() else PROJECT_DIR / p


def data_dir() -> Path:
    """A Jarvis saját adatai (böngészőprofil, Munka menü) a projektmappán KÍVÜL vannak, hogy egy
    új verzió kicsomagolásakor (akár új mappába) se vesszenek el: Windows-on %LOCALAPPDATA%\\Jarvis."""
    override = os.environ.get("JARVIS_DATA_DIR")   # pl. a tesztekben: egy ideiglenes mappa
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "Jarvis" if base else Path.home() / ".local" / "share" / "jarvis"


def app_data(cfg: "Config") -> Path:
    """A Jarvis saját adatainak mappája (a beállítás szerint, vagy az adatmappa)."""
    return Path(cfg.data_root) if cfg.data_root else data_dir()


def data_path(value: str, name: str) -> Path:
    """A beállított út; ha üres (vagy a régi, projektmappás alapérték), az adatmappán belül."""
    value = (value or "").strip()
    return data_dir() / name if value in ("", name) else resolve_path(value)


def adopt_legacy_data(value: str, name: str, log: Callable[[str], None]) -> None:
    """A régi verziók a projektmappában tartották az adatot; egyszer áthelyezzük az adatmappába."""
    if (value or "").strip() not in ("", name):
        return
    old, new = PROJECT_DIR / name, data_dir() / name
    if old.is_dir() and not new.exists():
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), str(new))
            log(f"A(z) „{name}” mappát áthelyeztem ide, hogy a frissítések ne töröljék: {new}")
        except OSError as exc:
            log(f"A(z) „{name}” mappát nem tudtam áthelyezni ({exc}). Másold át kézzel: {old} → {new}")
