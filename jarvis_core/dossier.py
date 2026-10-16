"""Hibacsomag: ha egy feltöltés, levétel vagy visszajátszás elakad, minden, ami a javításhoz kell,
egy mappába kerül (munka\\hibak\\<időpont>-<oldal>-<tárgy>-<feladat>):

  info.json         mi történt: oldal, tárgy, feladat, a hiba oka, az oldal címe, Jarvis válasza
  kepernyo.jpg      az oldal képe abban a pillanatban (ha nem túl hosszú, az egész oldal)
  oldal.txt         az oldalnézet, ahogy a modell látja (szöveg és a számozott elemek)
  lepesek.txt       a böngészőben tett lépések (megnyitás, kattintás, gépelés, fotók)
  beszelgetes.txt   a modell kérései és az eszközök válaszai (ha a modell csinálta)

Csak a gépeden marad. A kép és az oldalnézet személyes adatot is tartalmazhat (pl. a felhasználóneved),
ezért ha valakinek elküldöd, előtte nézd át. A legutóbbi KEEP darab marad meg.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

KEEP = 30
NAME_RE = re.compile(r"^\d{8}-\d{6}-[a-z0-9]+-[0-9a-f]{8}-[a-z]+(-\d+)?$")
TOOL_CHARS = 2500


def _short(text: Any, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + f" … ({len(text) - limit} karakter kihagyva)"


def format_steps(events: list[dict[str, Any]]) -> str:
    """A böngésző lépései olvasható formában."""
    lines = []
    for number, event in enumerate(events, 1):
        do = event.get("do", "")
        el = event.get("el") or {}
        what = f"{el.get('kind', '')} „{el.get('label', '')}”".strip() if el else ""
        if el.get("field"):
            what += f" (mező: {el['field']})"
        if do == "goto":
            text = f"megnyitás: {event.get('final') or event.get('url', '')}"
        elif do == "click":
            text = f"kattintás: {what}" + (" [közzététel/törlés]" if event.get("risky") else "")
            if event.get("url_after") and event.get("url_after") != event.get("url"):
                text += f" → {event['url_after']}"
        elif do == "type":
            text = f"gépelés: {what} ← „{_short(event.get('text', ''), 200)}”"
            if event.get("submit"):
                text += " + Enter"
        elif do == "upload":
            text = f"fotók: {event.get('count', '?')} db" + (f" ({what})" if what else "")
        elif do == "declined":
            text = f"NEM ENGEDÉLYEZTED: {what}"
        elif do == "back":
            text = "vissza az előző oldalra"
        else:
            text = do
        lines.append(f"{number:3d}. {text}")
    return "\n".join(lines) or "(nincs feljegyzett lépés)"


def format_conversation(history: list[dict[str, Any]]) -> str:
    """A modell kérései és az eszközök válaszai (a hosszú oldalnézetek megrövidítve)."""
    lines = []
    for message in history:
        role = message.get("role", "")
        if role == "user":
            lines.append(f"### Kérés\n{message.get('content', '')}")
        elif role == "assistant":
            if message.get("content"):
                lines.append(f"### Jarvis\n{message['content']}")
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                args = json.dumps(function.get("arguments", {}), ensure_ascii=False)
                lines.append(f"### Eszköz: {function.get('name', '')}\n{_short(args, 800)}")
        elif role == "tool":
            content = _short(message.get("content", ""), TOOL_CHARS)
            lines.append(f"### Válasz ({message.get('tool_name', '')})\n{content}")
    return "\n\n".join(lines) or "(a modell ennél nem dolgozott)"


class Dossiers:
    def __init__(self, root: Path, keep: int = KEEP):
        self.root = Path(root)
        self.keep = max(1, int(keep))

    def create(self, *, task: str, platform: str, item: Any, reason: str, mode: str,
               browser: Any = None, steps: list[dict[str, Any]] | None = None,
               history: list[dict[str, Any]] | None = None, extra: dict[str, Any] | None = None) -> str:
        """Elkészíti a csomagot, és visszaadja a nevét (a mappa neve a hibak mappán belül)."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = f"{stamp}-{re.sub(r'[^a-z0-9]', '', platform.lower()) or 'oldal'}-{item.id}-{task}"
        name, number = base, 1
        while (self.root / name).exists():
            number += 1
            name = f"{base}-{number}"
        folder = self.root / name
        folder.mkdir(parents=True, exist_ok=True)
        info: dict[str, Any] = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"), "task": task, "mode": mode, "platform": platform,
            "reason": reason, "item": {"id": item.id, "name": item.name, "kind": item.kind,
                                       "category": item.category, "price": item.price, "targets": item.targets},
            "url": getattr(browser, "url", "") if browser is not None else "",
        }
        info.update(extra or {})
        shot, view = (None, "")
        if browser is not None:
            try:
                shot, view = browser.capture()
            except Exception as exc:  # noqa: BLE001 - a csomag kép nélkül is hasznos
                info["capture_error"] = f"{type(exc).__name__}: {exc}"
            if not shot and getattr(browser, "capture_error", ""):
                info.setdefault("capture_error", browser.capture_error)   # miért nincs képernyőkép
        if shot:
            (folder / "kepernyo.jpg").write_bytes(shot)
        (folder / "oldal.txt").write_text(view or "(a böngésző nem futott)", encoding="utf-8")
        (folder / "lepesek.txt").write_text(format_steps(steps or []), encoding="utf-8")
        (folder / "beszelgetes.txt").write_text(format_conversation(history or []), encoding="utf-8")
        (folder / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
        self._prune()
        return name

    def path(self, name: str) -> Path:
        """Egy csomag mappája (csak a hibak mappán belüli, létező csomag)."""
        if not NAME_RE.match(name or ""):
            raise FileNotFoundError(name)
        folder = self.root / name
        if not folder.is_dir():
            raise FileNotFoundError(name)
        return folder

    def names(self) -> list[str]:
        try:
            return sorted(p.name for p in self.root.iterdir() if p.is_dir() and NAME_RE.match(p.name))
        except OSError:
            return []

    def _prune(self) -> None:
        for name in self.names()[:-self.keep]:
            shutil.rmtree(self.root / name, ignore_errors=True)


def open_folder(folder: Path) -> bool:
    """Megnyitja a mappát a fájlkezelőben (Windows-on az Intézőben)."""
    try:
        if sys.platform == "win32":
            os.startfile(str(folder))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False
