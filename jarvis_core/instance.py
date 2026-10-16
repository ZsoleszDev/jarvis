"""Egyszerre egy Jarvis fut. Ha már fut (pl. készenlétben, a gép bekapcsolása óta), a második
indítás csak megnyitja az ablakát, vagy leállítja (--quit).

A futó Jarvis a fut.json-ban hagyja az ablaka címét (a kulccsal együtt) az adatmappában. Az a
felhasználói profilodban van; a kulcs a weboldalak ellen véd, a saját programjaid elől nem kell.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

INFO_FILE = "fut.json"
LOCK_FILE = "jarvis.lock"


class InstanceLock:
    """Zár egy fájlon az operációs rendszer szintjén: amíg a Jarvis fut, másik nem kaphatja meg.
    Ha a Jarvis összeomlik, a rendszer magától elengedi."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def write_info(path: Path, url: str, standby: bool) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"url": url, "pid": os.getpid(), "started": time.time(), "standby": standby}),
                   encoding="utf-8")
    os.replace(tmp, path)


def read_info(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and str(data.get("url", "")).startswith("http://127.0.0.1:") else None


def clear_info(path: Path, url: str) -> None:
    """Kilépéskor törli a fut.json-t (ha még a miénk)."""
    info = read_info(path)
    if info is not None and info.get("url") == url:
        try:
            Path(path).unlink()
        except OSError:
            pass


def _split(url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(url)
    token = urllib.parse.parse_qs(parsed.query).get("t", [""])[0]
    return f"{parsed.scheme}://{parsed.netloc}", token


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))   # a saját gépünkhöz proxy nélkül


def reachable(url: str, timeout: float = 2.0) -> bool:
    """Válaszol-e a futó Jarvis ezen a címen (a kulccsal)."""
    base, token = _split(url)
    try:
        with _opener().open(f"{base}/api/ping?t={urllib.parse.quote(token)}", timeout=timeout) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001 - bármilyen hiba: nem válaszol
        return False


def request_quit(url: str, timeout: float = 5.0) -> bool:
    """Megkéri a futó Jarvist, hogy álljon le."""
    base, token = _split(url)
    request = urllib.request.Request(f"{base}/api/quit", data=b"{}", method="POST",
                                     headers={"X-Jarvis-Token": token, "Content-Type": "application/json"})
    try:
        with _opener().open(request, timeout=timeout) as response:
            return response.status in (200, 202)
    except Exception:  # noqa: BLE001
        return False
