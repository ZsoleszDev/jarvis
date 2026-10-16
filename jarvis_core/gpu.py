"""Videókártya-adatok az nvidia-smi segítségével (ha van NVIDIA-kártya és driver)."""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass

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
    return [f"GPU {g.index} ({g.name.replace('NVIDIA GeForce ', '')}): {gb(g.used_mb)} / {gb(g.total_mb)} GB "
            f"foglalt, {gb(g.free_mb)} GB szabad" for g in gpus]
