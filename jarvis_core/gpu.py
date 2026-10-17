"""Videókártya-adatok: az NVIDIA-driver könyvtárából (NVML), vagy ha az nem érhető el, az nvidia-smi-ből."""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

FIELDS = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu"


@dataclass
class GpuInfo:
    index: int
    name: str
    utilization: int | None   # százalék
    used_mb: int
    total_mb: int
    temperature: int | None   # °C

    @property
    def free_mb(self) -> int:
        return max(0, self.total_mb - self.used_mb)


def gb(mb: int) -> str:
    """Megabájt gigabájtban, magyar tizedesvesszővel: 6,1."""
    return f"{mb / 1024:.1f}".replace(".", ",")


def short_name(name: str) -> str:
    """'NVIDIA GeForce RTX 5060 Ti' → 'RTX 5060 Ti'."""
    return name.replace("NVIDIA ", "").replace("GeForce ", "").strip() or name


def _number(text: str) -> int | None:
    try:
        return int(float(text))
    except ValueError:   # pl. "[N/A]"
        return None


def parse(output: str) -> list[GpuInfo]:
    gpus = []
    for row in output.strip().splitlines():
        parts = [p.strip() for p in row.split(",")]
        if len(parts) != 6:
            continue
        index, used, total = _number(parts[0]), _number(parts[3]), _number(parts[4])
        if index is None or used is None or not total:
            continue
        gpus.append(GpuInfo(index, parts[1], _number(parts[2]), used, total, _number(parts[5])))
    return gpus


def query_gpus(timeout: float = 5.0) -> list[GpuInfo]:
    """Az NVIDIA-kártyák listája (üres, ha nincs nvidia-smi vagy nem válaszol)."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    try:
        proc = subprocess.run([exe, f"--query-gpu={FIELDS}", "--format=csv,noheader,nounits"],
                              capture_output=True, text=True, timeout=timeout, **flags)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse(proc.stdout)


def memory_lines(gpus: list[GpuInfo] | None = None) -> list[str]:
    """Kártyánként egy sor a videómemóriáról: 'GPU 1 (RTX 5060 Ti): 14,2 / 15,9 GB foglalt, 1,7 GB szabad'."""
    gpus = query_gpus() if gpus is None else gpus
    return [f"GPU {g.index} ({short_name(g.name)}): {gb(g.used_mb)} / {gb(g.total_mb)} GB "
            f"foglalt, {gb(g.free_mb)} GB szabad" for g in gpus]


# --- NVML: az NVIDIA-driver saját könyvtára -------------------------------------------------------
# Másodpercenként is olcsó lekérdezni (nem indít új folyamatot, mint az nvidia-smi).

class _Utilization(ctypes.Structure):        # nvmlUtilization_t
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _Memory(ctypes.Structure):             # nvmlMemory_t
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


class _MemoryV2(ctypes.Structure):           # nvmlMemory_v2_t: a "used" a driver tartaléka nélkül (mint az nvidia-smi)
    _fields_ = [("version", ctypes.c_uint), ("total", ctypes.c_ulonglong), ("reserved", ctypes.c_ulonglong),
                ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


MEMORY_V2 = ctypes.sizeof(_MemoryV2) | (2 << 24)
TEMPERATURE_GPU = 0
NAME_BUFFER = 96


def _nvml_names() -> list[str]:
    if sys.platform == "win32":
        system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvml.dll"
        legacy = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "NVIDIA Corporation" / "NVSMI" / "nvml.dll"
        return [str(system), str(legacy), "nvml.dll"]
    return ["libnvidia-ml.so.1", "libnvidia-ml.so"]


class Nvml:
    """A kártyák adatai közvetlenül a drivertől. OSError, ha nincs NVIDIA-driver vagy kártya."""

    def __init__(self, lib: Any = None):
        self.lib = lib if lib is not None else self._load()
        if self.lib.nvmlInit_v2() != 0:
            raise OSError("Az NVIDIA-driver (NVML) nem indult el.")
        count = ctypes.c_uint(0)
        if self.lib.nvmlDeviceGetCount_v2(ctypes.byref(count)) != 0 or not count.value:
            self.close()
            raise OSError("Nincs NVIDIA-kártya.")
        self.devices: list[tuple[int, Any, str]] = []
        for index in range(count.value):
            handle = ctypes.c_void_p()
            if self.lib.nvmlDeviceGetHandleByIndex_v2(index, ctypes.byref(handle)) != 0:
                continue
            name = ctypes.create_string_buffer(NAME_BUFFER)
            if self.lib.nvmlDeviceGetName(handle, name, NAME_BUFFER) != 0:
                name.value = b""
            self.devices.append((index, handle, name.value.decode("utf-8", "replace") or f"GPU {index}"))
        self._memory_v2 = True

    @staticmethod
    def _load() -> Any:
        for name in _nvml_names():
            try:
                return ctypes.CDLL(name)
            except OSError:
                continue
        raise OSError("Nincs NVIDIA-driver (NVML).")

    def _memory(self, handle: Any) -> tuple[int, int]:
        """(foglalt, összes) bájtban."""
        if self._memory_v2:
            memory = _MemoryV2(version=MEMORY_V2)
            fn = getattr(self.lib, "nvmlDeviceGetMemoryInfo_v2", None)
            if fn is not None and fn(handle, ctypes.byref(memory)) == 0:
                return memory.used, memory.total
            self._memory_v2 = False    # régi driver: marad a régi változat
        legacy = _Memory()
        if self.lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(legacy)) == 0:
            return legacy.used, legacy.total
        return 0, 0

    def read(self) -> list[GpuInfo]:
        gpus = []
        for index, handle, name in self.devices:
            rates = _Utilization()
            load = rates.gpu if self.lib.nvmlDeviceGetUtilizationRates(handle, ctypes.byref(rates)) == 0 else None
            used, total = self._memory(handle)
            temp = ctypes.c_uint(0)
            heat = temp.value if self.lib.nvmlDeviceGetTemperature(handle, TEMPERATURE_GPU, ctypes.byref(temp)) == 0 \
                else None
            if total:
                gpus.append(GpuInfo(index, name, load, used // 2**20, total // 2**20, heat))
        return gpus

    def close(self) -> None:
        try:
            self.lib.nvmlShutdown()
        except Exception:  # noqa: BLE001
            pass


def make_reader() -> tuple[Callable[[], list[GpuInfo]] | None, float]:
    """Amivel a kártyák adatai lekérdezhetők, és milyen gyakran érdemes: (olvasó, másodperc)."""
    try:
        return Nvml().read, 1.0
    except OSError:
        pass
    if shutil.which("nvidia-smi"):
        return (lambda: query_gpus(timeout=2.0)), 2.0   # új folyamat minden mintához: ritkábban
    return None, 0.0


class GpuMonitor:
    """A felület GPU-sávjaihoz: kártyánként egy minta másodpercenként, az utolsó percről.

    Csak addig mér, amíg valaki nézi: az ablak másodpercenként lekérdezi (snapshot); ha egy ideje senki
    (bezárták, vagy a háttérben van), a mérés leáll, és a következő lekérdezéskor újraindul."""

    def __init__(self, reader: Callable[[], list[GpuInfo]] | None = None, interval: float = 1.0,
                 keep_seconds: float = 60.0, idle_seconds: float = 15.0,
                 clock: Callable[[], float] = time.time):
        self._reader = reader
        self._resolved = reader is not None
        self.interval = interval
        self.keep_seconds = keep_seconds
        self.idle_seconds = idle_seconds
        self.clock = clock
        self._lock = threading.Lock()
        self._init_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._wanted = 0.0
        self._history: dict[int, deque] = {}
        self._names: dict[int, str] = {}
        self._totals: dict[int, int] = {}

    def _resolve(self) -> None:
        with self._init_lock:
            if not self._resolved:
                self._reader, interval = make_reader()
                self._resolved = True
                if interval:
                    self.interval = interval

    @property
    def available(self) -> bool:
        self._resolve()
        return self._reader is not None

    def sample(self) -> None:
        if self._reader is None:
            return
        try:
            gpus = self._reader()
        except Exception:  # noqa: BLE001 - pl. a driver épp frissül: a következő minta jó lesz
            gpus = []
        now = round(self.clock(), 2)
        size = int(self.keep_seconds / max(self.interval, 0.1)) + 2
        with self._lock:
            for g in gpus:
                history = self._history.get(g.index)
                if history is None:
                    history = self._history[g.index] = deque(maxlen=size)
                history.append((now, g.utilization, g.used_mb, g.temperature))
                self._names[g.index] = short_name(g.name)
                self._totals[g.index] = g.total_mb

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.sample()
            with self._lock:
                if self.clock() - self._wanted > self.idle_seconds:
                    self._thread = None        # senki sem nézi: megállunk
                    return
        with self._lock:
            self._thread = None

    def snapshot(self) -> dict[str, Any]:
        """Az utolsó perc kártyánként; elindítja a mérést, ha még nem fut."""
        if not self.available or self._stop.is_set():
            return {"available": False, "gpus": []}
        with self._lock:
            self._wanted = self.clock()
            thread = None
            if self._thread is None:
                thread = self._thread = threading.Thread(target=self._run, name="jarvis-gpus", daemon=True)
        if thread is not None:
            self.sample()     # induláskor (és szünet után) se kelljen egy teljes ütemet várni
            thread.start()
        with self._lock:
            gpus = [{"index": index, "name": self._names[index], "total_mb": self._totals[index],
                     "samples": [list(s) for s in history]}
                    for index, history in sorted(self._history.items())]
        return {"available": True, "interval": self.interval, "now": round(self.clock(), 2), "gpus": gpus}

    def stop(self) -> None:
        self._stop.set()
