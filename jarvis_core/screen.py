"""Képernyőkép készítése: Jarvis ezzel "látja" a monitorodat.

A kép nem hagyja el a gépet: a helyi Qwen-modell elemzi az Ollamán keresztül.
"""
from __future__ import annotations

import base64
import io
from typing import Any

from .config import ScreenCfg


class ScreenError(Exception):
    """Érthető hibaüzenet képernyőkép-készítésnél."""


def _mss() -> Any:
    try:
        import mss
    except ImportError as exc:
        raise ScreenError("A képernyőképhez telepítsd: pip install mss pillow") from exc
    return mss.mss()


def list_monitors() -> list[str]:
    """A monitorok listája: '1: 2560x1440 (0, 0)'. A 0 az összes monitor együtt."""
    with _mss() as sct:
        return [f"{i}: {m['width']}x{m['height']} ({m['left']}, {m['top']})"
                for i, m in enumerate(sct.monitors)]


def capture_b64(cfg: ScreenCfg) -> str:
    """Képernyőkép a beállított monitorról, kicsinyítve, base64 JPEG-ként."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise ScreenError("A képernyőképhez telepítsd: pip install pillow") from exc
    with _mss() as sct:
        monitors = sct.monitors
        index = int(cfg.monitor)
        if not 0 <= index < len(monitors):
            raise ScreenError(f"Nincs {index}. monitor. Elérhető: 1–{len(monitors) - 1} (0 = mind együtt).")
        shot = sct.grab(monitors[index])
        image = Image.frombytes("RGB", shot.size, shot.rgb)
    max_width = int(cfg.max_width)
    if image.width > max_width:
        image = image.resize((max_width, round(image.height * max_width / image.width)))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return base64.b64encode(buffer.getvalue()).decode("ascii")
