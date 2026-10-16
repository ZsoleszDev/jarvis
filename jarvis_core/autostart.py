"""Automatikus indítás: a gép bekapcsolásakor (Windows-bejelentkezéskor) a Jarvis készenlétben indul.

Egy parancsikont tesz az Indítópult mappába (shell:startup), ami a pythonw.exe-vel (konzolablak
nélkül) indítja a jarvis.py --standby-t. Rendszergazdai jog nem kell hozzá, és bármikor kikapcsolható:
    python jarvis.py --autostart on
    python jarvis.py --autostart off
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from .config import PROJECT_DIR

LINK_NAME = "Jarvis.lnk"


def startup_folder() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def pythonw_path() -> Path:
    """A pythonw.exe (azzal nem nyílik konzolablak): a projekt .venv-jéből, ha van (abban vannak a
    csomagok, akkor is, ha a parancsot véletlenül a gépen lévő Pythonnal adtad ki), különben a mostani
    Python mellől."""
    venv = PROJECT_DIR / ".venv" / "Scripts" / "pythonw.exe"
    if venv.exists():
        return venv
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    return windowless if windowless.exists() else exe


def _ps(text: str) -> str:
    """PowerShell-szöveg aposztrófok között (a benne lévő aposztróf duplázva)."""
    return "'" + str(text).replace("'", "''") + "'"


def shortcut_command(link: Path, target: Path, arguments: str, workdir: Path) -> list[str]:
    script = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps(link)}); "
              f"$s.TargetPath = {_ps(target)}; $s.Arguments = {_ps(arguments)}; "
              f"$s.WorkingDirectory = {_ps(workdir)}; $s.WindowStyle = 7; "
              f"$s.Description = {_ps('Jarvis készenlétben (a gép bekapcsolásakor)')}; $s.Save()")
    return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]


def set_autostart(enable: bool, config: str | None = None, windows: bool | None = None,
                  run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str:
    """Be- vagy kikapcsolja az automatikus indítást. Visszaadja, mi történt (magyarul)."""
    if not (os.name == "nt" if windows is None else windows):
        return "Az automatikus indítás csak Windows-on működik."
    link = startup_folder() / LINK_NAME
    if not enable:
        try:
            link.unlink()
        except FileNotFoundError:
            return "Nem volt bekapcsolva."
        except OSError as exc:
            return f"Nem sikerült kikapcsolni ({exc}). Töröld kézzel: {link}"
        return "Kikapcsoltam: a gép bekapcsolásakor már nem indul el a Jarvis."
    arguments = f'"{PROJECT_DIR / "jarvis.py"}" --standby'
    if config:
        arguments += f' --config "{Path(config).resolve()}"'
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        result = run(shortcut_command(link, pythonw_path(), arguments, PROJECT_DIR),
                     capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Nem sikerült bekapcsolni ({exc})."
    if result.returncode != 0 or not link.exists():
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return "Nem sikerült bekapcsolni" + (f" ({detail[-1][:200]})." if detail else ".")
    return ("Bekapcsoltam: a gép bekapcsolásakor (bejelentkezéskor) a Jarvis készenlétben indul, ablak és "
            "betöltött modellek nélkül. Az ablakát a start_jarvis.bat-tal (vagy python jarvis.py) nyitod meg; "
            f"a parancsikon: {link}")
