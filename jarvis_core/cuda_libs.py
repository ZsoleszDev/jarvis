"""A GPU-s beszédfelismeréshez szükséges NVIDIA-könyvtárak (cuBLAS 12, cuDNN 9) előkészítése.

A faster-whisper (CTranslate2) a CUDA 12-es cuBLAS-t és a cuDNN 9-et keresi. Ha ezeket
pip-pel telepíted (requirements-gpu.txt), a fájlok a site-packages/nvidia/... mappákba
kerülnek, amiket a Windows magától nem talál meg. Ez a modul hozzáadja őket a keresési
úthoz, és még a modell betöltése előtt ellenőrzi, hogy betölthetők-e. Így hiányzó
könyvtár esetén nem omlik össze a program, hanem CPU-ra vált.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

REQUIRED_WINDOWS = ("cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll", "cudnn_ops64_9.dll")
REQUIRED_LINUX = ("libcublas.so.12", "libcublasLt.so.12", "libcudnn.so.9", "libcudnn_ops.so.9")

_dll_handles: list[object] = []   # az add_dll_directory csak addig él, amíg a handle él
_prepared_dirs: list[Path] = []
_prepared = False


def _nvidia_lib_dirs() -> list[Path]:
    """A pip-pel telepített nvidia-* csomagok bin (Windows) vagy lib (Linux) mappái."""
    try:
        import nvidia  # névtér-csomag: nvidia-cublas-cu12, nvidia-cudnn-cu12
    except ImportError:
        return []
    pattern = "*.dll" if IS_WINDOWS else "*.so*"
    dirs: list[Path] = []
    for root in getattr(nvidia, "__path__", []):
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for pkg in sorted(root_path.iterdir()):
            for sub in ("bin", "lib"):
                candidate = pkg / sub
                if candidate.is_dir() and any(candidate.glob(pattern)):
                    dirs.append(candidate)
    return dirs


def prepare() -> list[Path]:
    """Beállítja a GPU-sorrendet és a könyvtárak keresési útját. Többször is hívható."""
    global _prepared
    # Így a GPU-sorszámok ugyanazok, mint az nvidia-smi listájában.
    os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")
    if _prepared:
        return list(_prepared_dirs)
    _prepared = True
    for directory in _nvidia_lib_dirs():
        os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
        if IS_WINDOWS and hasattr(os, "add_dll_directory"):
            _dll_handles.append(os.add_dll_directory(str(directory)))
        _prepared_dirs.append(directory)
    return list(_prepared_dirs)


def _can_load(name: str) -> bool:
    candidates = [str(d / name) for d in _prepared_dirs if (d / name).exists()]
    candidates.append(name)
    found = ctypes.util.find_library(name)
    if found:
        candidates.append(found)
    for candidate in candidates:
        try:
            if IS_WINDOWS:
                ctypes.WinDLL(candidate)  # type: ignore[attr-defined]
            else:
                ctypes.CDLL(candidate, mode=ctypes.RTLD_GLOBAL)
            return True
        except OSError:
            continue
    return False


def status() -> tuple[bool, str]:
    """(használható-e a GPU-s beszédfelismerés, indoklás)."""
    prepare()
    required = REQUIRED_WINDOWS if IS_WINDOWS else REQUIRED_LINUX
    missing = [name for name in required if not _can_load(name)]
    if missing:
        return False, ("hiányzó CUDA-könyvtár: " + ", ".join(missing)
                       + " (telepítsd: pip install -r requirements-gpu.txt)")
    try:
        import ctranslate2

        count = ctranslate2.get_cuda_device_count()
    except Exception as exc:  # noqa: BLE001
        return False, f"CTranslate2-hiba: {exc}"
    if count < 1:
        return False, "nem található CUDA-képes videókártya"
    return True, f"{count} CUDA-eszköz"
