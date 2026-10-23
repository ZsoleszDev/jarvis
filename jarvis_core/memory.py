"""A gép memóriája: mennyi foglalt, mennyi a határ, és mely programok foglalják a legtöbbet.

Ha a nyelvi modell, a böngésző vagy a Playwright memóriahiba miatt áll le (std::bad_alloc, MemoryError,
„Zone Allocation failed”), nem elég annyit tudni, hogy elfogyott: az kell, hogy mi foglalja, és mennyi a határ.
Windows-on a határ a véglegesített memória (commit): a RAM és a lapozófájl együtt. Ha ez betelik, a programok
már nem kapnak új memóriát, akkor sem, ha a Feladatkezelő szerint van még szabad RAM.
"""
from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass, field
from typing import Any

GB = 1024 ** 3
MEMORY_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management"


def gb(size: float) -> str:
    """Bájt gigabájtban, magyar tizedesvesszővel: 27,9."""
    return f"{size / GB:.1f}".replace(".", ",")


@dataclass
class MemoryReport:
    ram_used: int = 0
    ram_total: int = 0
    commit_used: int = 0           # Windows: véglegesített memória (RAM + lapozófájl); máshol 0
    commit_limit: int = 0
    pagefile: str = ""             # Windows: "auto", "none", "fixed:<legnagyobb méret MB-ban>", vagy "" (nem tudjuk)
    top: list[tuple[str, int, int]] = field(default_factory=list)   # (program, bájt, folyamatok száma)

    @property
    def commit_full(self) -> bool:
        return bool(self.commit_limit) and self.commit_used >= 0.9 * self.commit_limit

    @property
    def pagefile_max_gb(self) -> float | None:
        """A lapozófájl legnagyobb mérete GB-ban: 0, ha nincs; None, ha a Windows kezeli, vagy nem tudjuk."""
        if self.pagefile == "none":
            return 0.0
        if self.pagefile.startswith("fixed:"):
            return int(self.pagefile[6:]) / 1024
        return None

    def lines(self) -> list[str]:
        """A felhasználónak: egy sor a memóriáról, egy a legtöbbet foglaló programokról."""
        out = []
        if self.ram_total:
            text = f"A gép memóriája most: RAM {gb(self.ram_used)} / {gb(self.ram_total)} GB foglalt"
            if self.commit_limit:
                text += (f"; véglegesítve (RAM + lapozófájl) {gb(self.commit_used)} / "
                         f"{gb(self.commit_limit)} GB")
            out.append(text + ".")
        if self.top:
            out.append("A legtöbbet foglalja: " + ", ".join(
                f"{name}{f' ({count} folyamat)' if count > 1 else ''} {gb(size)} GB" for name, size, count in self.top)
                + ".")
        return out

    def public(self) -> dict[str, Any]:
        """A hibacsomagba (info.json): GB-ban, olvashatóan."""
        return {"ram": [round(self.ram_used / GB, 1), round(self.ram_total / GB, 1)],
                "commit": [round(self.commit_used / GB, 1), round(self.commit_limit / GB, 1)],
                "pagefile": self.pagefile,
                "top": [[name, round(size / GB, 2), count] for name, size, count in self.top]}


def commit_charge() -> tuple[int, int] | None:
    """Windows: (véglegesített, határ) bájtban, a GetPerformanceInfo szerint; máshol None."""
    if sys.platform != "win32":
        return None

    class PerformanceInformation(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("CommitTotal", ctypes.c_size_t), ("CommitLimit", ctypes.c_size_t),
                    ("CommitPeak", ctypes.c_size_t), ("PhysicalTotal", ctypes.c_size_t),
                    ("PhysicalAvailable", ctypes.c_size_t), ("SystemCache", ctypes.c_size_t),
                    ("KernelTotal", ctypes.c_size_t), ("KernelPaged", ctypes.c_size_t),
                    ("KernelNonpaged", ctypes.c_size_t), ("PageSize", ctypes.c_size_t),
                    ("HandleCount", ctypes.c_ulong), ("ProcessCount", ctypes.c_ulong),
                    ("ThreadCount", ctypes.c_ulong)]

    info = PerformanceInformation()
    info.cb = ctypes.sizeof(info)
    try:
        if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
            return None
    except (AttributeError, OSError):
        return None
    return info.CommitTotal * info.PageSize, info.CommitLimit * info.PageSize


def pagefile_setting(registry: Any = None) -> str:
    """Windows: a lapozófájl beállítása. "auto": a Windows kezeli (szükség szerint nő), "none": nincs,
    "fixed:<MB>": kézzel megadott, legfeljebb ekkora (meghajtónként összeadva); "": nem tudjuk."""
    if registry is None:
        if sys.platform != "win32":
            return ""
        import winreg as registry
    try:
        with registry.OpenKey(registry.HKEY_LOCAL_MACHINE, MEMORY_KEY) as key:
            try:
                if int(registry.QueryValueEx(key, "AutomaticManagedPagefile")[0]):
                    return "auto"
            except OSError:
                pass
            files = registry.QueryValueEx(key, "PagingFiles")[0]
    except OSError:
        return ""
    entries = [str(f).split() for f in (files if isinstance(files, (list, tuple)) else [files]) if str(f).strip()]
    if not entries:
        return "none"
    largest = 0
    for parts in entries:
        if parts[0].startswith("?:") or len(parts) < 3:
            return "auto"                           # "?:\pagefile.sys": minden meghajtón a Windows kezeli
        try:
            minimum, maximum = int(parts[1]), int(parts[2])
        except ValueError:
            return ""
        if minimum == 0 and maximum == 0:
            return "auto"                           # "a rendszer által kezelt méret"
        largest += maximum
    return f"fixed:{largest}"


def heavy_programs(limit: int = 5, psutil_module: Any = None) -> list[tuple[str, int, int]]:
    """A legtöbb memóriát foglaló programok, az azonos nevű folyamatok együtt (pl. a böngésző sok folyamata):
    Windows-on a véglegesített (private) memóriájuk, máshol a RAM-ban lévő részük."""
    psutil = psutil_module
    if psutil is None:
        try:
            import psutil
        except ImportError:
            return []
    totals: dict[str, list[Any]] = {}
    for proc in psutil.process_iter(["name", "memory_info"]):
        info = proc.info.get("memory_info")
        name = proc.info.get("name") or ""
        if info is None or not name:
            continue
        size = (getattr(info, "private", 0) or info.vms) if sys.platform == "win32" else info.rss
        entry = totals.setdefault(name.lower(), [name, 0, 0])
        entry[1] += int(size or 0)
        entry[2] += 1
    ranked = sorted(totals.values(), key=lambda e: -e[1])
    return [(name, size, count) for name, size, count in ranked[:limit] if size > 0]


def report(top: int = 5, psutil_module: Any = None) -> MemoryReport:
    psutil = psutil_module
    if psutil is None:
        try:
            import psutil
        except ImportError:
            return MemoryReport()
    vm = psutil.virtual_memory()
    result = MemoryReport(ram_used=int(vm.total - vm.available), ram_total=int(vm.total))
    commit = commit_charge()
    if commit:
        result.commit_used, result.commit_limit = commit
        result.pagefile = pagefile_setting()
    if top:
        result.top = heavy_programs(top, psutil)
    return result


@dataclass
class OllamaProcess:
    found: bool = False                  # megtaláltuk-e a futó Ollamát (ollama serve)
    env: dict[str, str] = field(default_factory=dict)   # a minket érdeklő beállítások a környezetében
    readable: bool = False               # ki tudtuk-e olvasni a környezetét
    flags: dict[str, str] = field(default_factory=dict)  # a modellfolyamatnak kézzel átadott kapcsolók


SETTINGS = ("LLAMA_ARG_CACHE_RAM", "LLAMA_ARG_CTX_CHECKPOINTS", "OLLAMA_NUM_PARALLEL")
FLAGS = {"--cache-ram": "LLAMA_ARG_CACHE_RAM", "-cram": "LLAMA_ARG_CACHE_RAM",
         "--ctx-checkpoints": "LLAMA_ARG_CTX_CHECKPOINTS", "-ctxcp": "LLAMA_ARG_CTX_CHECKPOINTS",
         "--swa-checkpoints": "LLAMA_ARG_CTX_CHECKPOINTS"}


def ollama_process(psutil_module: Any = None) -> OllamaProcess:
    """A futó Ollama: milyen beállításokkal indult (a környezete), és a modellfolyamatnak (llama-server) adott-e
    kézzel --cache-ram vagy --ctx-checkpoints kapcsolót (az erősebb a környezeti változónál). Így kiderül, hogy
    a setx után tényleg újraindult-e az Ollama."""
    psutil = psutil_module
    if psutil is None:
        try:
            import psutil
        except ImportError:
            return OllamaProcess()
    result = OllamaProcess()
    for proc in psutil.process_iter(["name", "cmdline"]):
        name = (proc.info.get("name") or "").lower()
        cmdline = [str(part) for part in proc.info.get("cmdline") or []]
        if not (name.startswith("ollama") or name.startswith("llama")):
            continue
        for index, part in enumerate(cmdline[:-1]):
            if part in FLAGS:
                result.flags[FLAGS[part]] = cmdline[index + 1]
        if "serve" in cmdline and not result.found:
            result.found = True
            try:
                environ = proc.environ()
            except Exception:  # noqa: BLE001 - pl. másik felhasználóé (szolgáltatásként fut)
                continue
            result.readable = True
            result.env = {key: environ[key] for key in SETTINGS if key in environ}
    return result


def process_env_note(proc: OllamaProcess, wanted: dict[str, str]) -> str:
    """Ha a felhasználói beállítás (setx) megvan, de a futó Ollamában nincs: még a beállítás előtt indult."""
    if not proc.found or not proc.readable:
        return ""
    missing = [key for key, value in wanted.items() if value and proc.env.get(key) != value]
    if not missing:
        return ""
    return ("A futó Ollama még a beállítás előtt indult (a környezetében nincs " + ", ".join(missing) +
            "), ezért a javítás még nem él: lépj ki az Ollamából a tálcán (jobb klikk az ikonjára), és indítsd "
            "újra a Start menüből.")
