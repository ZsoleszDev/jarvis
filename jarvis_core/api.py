"""Telefonos kapcsolat: JSON-végpontok a saját (Angular + Capacitor) appodnak.

Biztonság:
- Alapból csak a 127.0.0.1-en figyel; a telefon a Tailscale-en át éri el: `tailscale serve --bg 8765`
  HTTPS-t ad (https://<gép>.<tailnet>.ts.net), és innen veszi át a kéréseket. Az internetre nem nyílik ki.
- Minden kéréshez párosításkor kapott, eszközönkénti kulcs kell (Authorization: Bearer …). A kulcsot
  a Jarvis csak hash-ként tárolja, és a Jarvis ablakában (Telefon gomb) bármikor visszavonható.
- Párosítani csak a Jarvis ablakában kért, 5 percig érvényes, 6 jegyű kóddal lehet (5 rossz próbálkozás
  után a kód elvész), így a kódot csak az kaphatja meg, aki a gép előtt ül.
- A böngészőből jövő kérések (CORS) csak a beállított eredetekről (pl. capacitor://localhost) jöhetnek.

A végpontok listája a docs/telefon-api.md-ben van.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import secrets
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

from . import remote
from .config import ApiCfg
from .messages import MAX_TEXT
from .selling import FAILED, LEDGER_FIELDS, MAX_PHOTO_BYTES, NOT_POSTED, POSTED, ItemError
from .watches import WatchError
from .webpush import INSTALL_HINT, PushError, WebPush, available as push_available
from .work import PRICE_CHOICES

API_VERSION = 1
MAX_JSON = 256 * 1024
MAX_FORWARD = MAX_PHOTO_BYTES + 64 * 1024   # a távoli ablak kérései (pl. egy fotó)
FORWARD_TIMEOUT = 60.0
# A gép felületének válaszából ezeket a fejléceket adjuk tovább (pl. a történet letöltésénél a fájlnév).
FORWARD_HEADERS = ("Content-Security-Policy", "Referrer-Policy", "Content-Disposition")
CODE_SECONDS = 300
CODE_TRIES = 5
LOG_LIMIT = 800
LOG_SKIP = {"delta", "segment_end"}       # ezek túl sűrűk; a végleges szöveg "assistant" eseményként jön
EDITABLE = {"targets", "prompt", "research_wanted", "product", "title", "description", "category", "brand",
            "size", "condition", "color", "material", "price", "min_price", "location", "shipping", "bought_price",
            "sold_price", *LEDGER_FIELDS}
AGENT_ACTIONS = {"sell", "reply", "price", "post", "research", "delist"}


class AuthError(Exception):
    """Párosítási hiba (érthető üzenettel)."""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --- párosítás ----------------------------------------------------------------------------------

class PhoneAuth:
    """A párosított eszközök (a kulcsuk csak hash-ként) és az épp érvényes párosító kód."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.Lock()
        self._code: dict[str, Any] | None = None
        self._devices: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {d["id"]: d for d in data if isinstance(d, dict) and d.get("id") and d.get("hash")} \
            if isinstance(data, list) else {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(self._devices.values()), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def new_code(self) -> dict[str, Any]:
        """Új, 5 percig érvényes, 6 jegyű párosító kód (a régi érvényét veszti)."""
        with self._lock:
            code = f"{secrets.randbelow(1_000_000):06d}"
            self._code = {"code": code, "expires": self.clock() + CODE_SECONDS, "tries": 0}
            return {"code": code, "expires": self._code["expires"], "seconds": CODE_SECONDS}

    def pair(self, code: str, device: str) -> dict[str, Any]:
        with self._lock:
            current = self._code
            if current is None or self.clock() > current["expires"]:
                self._code = None
                raise AuthError("Nincs érvényes párosító kód. Kérj újat a Jarvis ablakában (Telefon gomb).")
            if not secrets.compare_digest(str(code or "").strip(), current["code"]):
                current["tries"] += 1
                if current["tries"] >= CODE_TRIES:
                    self._code = None
                    raise AuthError("Túl sok rossz próbálkozás; kérj új kódot a Jarvis ablakában.")
                raise AuthError("Rossz kód.")
            self._code = None
            token = secrets.token_urlsafe(32)
            device_id = secrets.token_hex(4)
            now = self.clock()
            name = " ".join(str(device or "").split())[:60] or "Telefon"
            self._devices[device_id] = {"id": device_id, "name": name, "hash": _hash(token),
                                        "created": now, "last_seen": now}
            self._save()
        return {"token": token, "device_id": device_id, "name": name}

    def check(self, token: str | None) -> str | None:
        """A kulcshoz tartozó eszköz azonosítója (vagy None)."""
        if not token:
            return None
        wanted = _hash(token)
        with self._lock:
            for device in self._devices.values():
                if secrets.compare_digest(device["hash"], wanted):
                    now = self.clock()
                    if now - float(device.get("last_seen") or 0) > 60:
                        device["last_seen"] = now
                        try:
                            self._save()
                        except OSError:
                            pass
                    return device["id"]
        return None

    def devices(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{k: v for k, v in d.items() if k != "hash"} for d in self._devices.values()]

    def revoke(self, device_id: str) -> bool:
        with self._lock:
            if self._devices.pop(device_id, None) is None:
                return False
            self._save()
            return True


# --- eseménynapló (hosszú lekérdezéshez) --------------------------------------------------------------

class EventLog:
    """A Jarvis eseményei sorszámmal: a telefon az utolsó látott sorszám után jövőket kéri le."""

    def __init__(self, limit: int = LOG_LIMIT):
        self._cond = threading.Condition()
        self._events: deque[tuple[int, dict[str, Any]]] = deque(maxlen=limit)
        self._seq = 0

    def add(self, event: dict[str, Any]) -> None:
        # a Kreatív menü eseményei (élő írás, egész történetek) a telefonnak nem kellenek
        if event.get("type") in LOG_SKIP or str(event.get("type") or "").startswith("creative_"):
            return
        with self._cond:
            self._seq += 1
            self._events.append((self._seq, {**event, "seq": self._seq, "at": time.time()}))
            self._cond.notify_all()

    def last_seq(self) -> int:
        with self._cond:
            return self._seq

    def since(self, after: int, wait: float = 0.0) -> tuple[list[dict[str, Any]], int, bool]:
        """Az `after` utáni események (ha nincs, legfeljebb `wait` másodpercig vár rájuk).
        A harmadik érték igaz, ha közben események vesztek el (túl régi az `after`)."""
        deadline = time.monotonic() + max(0.0, wait)
        with self._cond:
            while True:
                newer = [e for seq, e in self._events if seq > after]
                remaining = deadline - time.monotonic()
                if newer or remaining <= 0:
                    break
                self._cond.wait(remaining)
            oldest = self._events[0][0] if self._events else self._seq + 1
            gap = after < self._seq and after + 1 < oldest
            return newer, self._seq, gap

    def for_request(self, request_id: str) -> list[dict[str, Any]]:
        with self._cond:
            return [e for _seq, e in self._events if e.get("request_id") == request_id]


# --- szerver ----------------------------------------------------------------------------------------

class ApiServer:
    def __init__(self, app: Any, ui: Any, bus: Any, cfg: ApiCfg, auth: PhoneAuth, log: EventLog):
        self.app = app
        self.ui = ui
        self.bus = bus
        self.cfg = cfg
        self.auth = auth
        self.log = log
        self.gui: Any = None      # a gép felülete (gui.GuiServer): a távoli ablak ennek adja tovább a kéréseket
        self.push: WebPush | None = None    # értesítés a laptopra, ha az ablak be van zárva (webpush.py)
        handler = type("JarvisApiHandler", (_ApiHandler,), {"api": self})
        self.httpd = ThreadingHTTPServer((cfg.host, int(cfg.port)), handler)
        self.httpd.daemon_threads = True
        self.host = cfg.host
        self.port = int(self.httpd.server_address[1])
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="jarvis-api", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _number(value: Any, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


class _ApiHandler(BaseHTTPRequestHandler):
    api: ApiServer
    server_version = "Jarvis"
    timeout = 120    # egy beragadt (vagy lassú) kapcsolat se foglaljon szálat örökre

    def log_message(self, *args: Any) -> None:  # a konzolt nem töltjük tele kérésnaplóval
        pass

    # --- válaszok -------------------------------------------------------------------------

    def _allowed_origin(self) -> str | None:
        origin = self.headers.get("Origin")
        return origin if origin and origin in self.api.cfg.allowed_origins else None

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        origin = self._allowed_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: dict[str, Any]) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"ok": False, "error": message, "code": code})

    # --- kérések ---------------------------------------------------------------------------

    def do_OPTIONS(self) -> None:   # CORS előzetes kérdés
        if self._allowed_origin() is None:
            return self._send(403, b"", "text/plain")
        self._send(204, b"", "text/plain", {
            "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Access-Control-Max-Age": "600"})

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _bearer(self, query: dict[str, list[str]]) -> str:
        header = self.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return ""

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        query = parse_qs(parsed.query)
        try:
            if not path.startswith("/api/v1"):
                return self._remote(method, parsed)
            sub = path[len("/api/v1"):] or "/"
            if method == "GET" and sub == "/ping":
                return self._json(200, {"ok": True, "name": "Jarvis", "api": API_VERSION})
            if method == "POST" and sub == "/pair":
                return self._pair()
            if method == "POST" and sub == "/push/answer":
                return self._push_answer()      # az értesítés gombja: egyszer használható válaszkulccsal
            token = self._bearer(query)
            # Képet az <img> nem tud fejléccel kérni: ilyenkor a kulcs a címben is lehet.
            if not token and method == "GET" and sub.endswith(".jpg"):
                token = (query.get("access_token") or [""])[0]
            self.device = self.api.auth.check(token)
            if self.device is None:
                raise ApiError(401, "unauthorized", "Párosítás kell (Authorization: Bearer <kulcs>).")
            for route_method, pattern, name in ROUTES:
                if route_method != method:
                    continue
                match = pattern.fullmatch(sub)
                if match:
                    return getattr(self, name)(*match.groups(), query=query)
            raise ApiError(404, "not_found", "Nincs ilyen végpont.")
        except ApiError as exc:
            self._error(exc.status, exc.code, exc.message)
        except ItemError as exc:
            self._error(400, "bad_request", str(exc))
        except WatchError as exc:
            self._error(400, "bad_request", str(exc))
        except PushError as exc:
            self._error(400, "bad_request", str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 - egy hibás kérés ne állítsa meg a szervert
            try:
                self._error(500, "error", f"Váratlan hiba: {type(exc).__name__}: {exc}")
            except OSError:
                pass

    # --- segédek ---------------------------------------------------------------------------

    def _length(self, limit: int) -> int:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "bad_request", "Hibás Content-Length.") from None
        if length < 0:
            raise ApiError(400, "bad_request", "Hibás Content-Length.")
        if length > limit:
            raise ApiError(413, "too_large", "Túl nagy a kérés.")
        return length

    def _body(self) -> dict[str, Any]:
        length = self._length(MAX_JSON)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise ApiError(400, "bad_request", "A kérés törzse nem érvényes JSON.") from None
        if not isinstance(data, dict):
            raise ApiError(400, "bad_request", "A kérés törzse egy JSON-objektum legyen.")
        return data

    def _raw(self, limit: int) -> bytes:
        length = self._length(limit)
        if not length:
            raise ApiError(400, "bad_request", "Üres a kérés törzse.")
        return self.rfile.read(length)

    def _app(self) -> Any:
        app = self.api.app
        if app is None or app.assistant is None:
            raise ApiError(503, "starting", "A Jarvis még indul.")
        return app

    def _work(self) -> Any:
        app = self._app()
        if app.work is None or app.work_assistant is None:
            raise ApiError(503, "no_work", "A Munka menü ki van kapcsolva vagy még indul.")
        return app.work

    def _ready_for_agent(self, flag: Any) -> None:
        """Ha szünetel: hiba, kivéve, ha a kérés kifejezetten kéri a folytatást (resume).
        Készenlétből (standby) a kérés magától felébreszti, ehhez nem kell a resume."""
        app = self._app()
        if app.paused and not getattr(app, "standby", False):
            if not flag:
                raise ApiError(409, "paused", "Szünetelek. Előbb: POST /api/v1/resume (vagy a kérésben: resume).")
            app.submit_resume()

    def _result(self, request_id: str, wait: float) -> None:
        if wait <= 0:
            return self._json(202, {"ok": True, "request_id": request_id, "done": False})
        done = self.api.app.wait_request(request_id, wait)
        events = self.api.log.for_request(request_id)
        reply = "\n".join(e["text"] for e in events if e.get("type") == "assistant" and e.get("text"))
        heard = next((e["text"] for e in events if e.get("type") == "user"), "")
        errors = [e["text"] for e in events if e.get("type") == "error"]
        self._json(200, {"ok": True, "request_id": request_id, "done": done, "heard": heard, "reply": reply,
                         "errors": errors})

    @staticmethod
    def _flag(query: dict[str, list[str]], name: str) -> bool:
        return (query.get(name) or [""])[0].lower() in ("1", "true", "igen", "yes")

    # --- a Jarvis ablaka távolról (telefon, laptop) ---------------------------------------------------

    def _remote(self, method: str, parsed: Any) -> None:
        """Ugyanaz a felület, mint a gépen (remote.py): a kéréseket az eszköz kulcsával fogadjuk, és a gépen
        futó felületnek adjuk tovább a helyi kulccsal. A böngészős felület a kulcsot a címben (?t=) vagy az
        X-Jarvis-Token fejlécben küldi, ugyanúgy, mint a gépen."""
        path = parsed.path or "/"
        query = parse_qs(parsed.query, keep_blank_values=True)
        static = remote.static_file(path)
        if static is not None and method == "GET":
            return self._send(200, static[0], static[1], {"Cache-Control": "no-cache"})
        gui = self.api.gui
        if gui is None:
            raise ApiError(503, "no_window", "A Jarvis ablaka nem fut (konzolos módban nincs távoli ablak).")
        if method == "GET" and path.startswith("/ui/"):
            return self._forward(gui, method, path, query, device=False)   # a felület fájljai: nem titkosak
        token = (query.get("t") or [""])[0] or self.headers.get("X-Jarvis-Token", "")
        self.device = self.api.auth.check(token) if token else None
        if self.device is None:
            if method == "GET" and path == "/":
                return self._send(200, remote.LOGIN_PAGE, "text/html; charset=utf-8",
                                  {"Content-Security-Policy": remote.LOGIN_CSP, "Referrer-Policy": "no-referrer"})
            raise ApiError(401, "unauthorized", "Párosítás kell.")
        if remote.denied(path):
            raise ApiError(403, "forbidden", "Ezt csak a gépen, a Jarvis ablakában lehet.")
        if path not in ("/", "/events") and not path.startswith("/api/"):
            raise ApiError(404, "not_found", "Nincs ilyen oldal.")
        self._forward(gui, method, path, query, device=True)

    def _forward(self, gui: Any, method: str, path: str, query: dict[str, list[str]], device: bool) -> None:
        if device and "t" in query:
            query = {**query, "t": [gui.token]}
        target = path + ("?" + urlencode(query, doseq=True) if query else "")
        body = self.rfile.read(self._length(MAX_FORWARD)) if method != "GET" else None
        headers = {"Host": f"127.0.0.1:{gui.port}"}
        for name in ("Content-Type", "Accept"):
            if self.headers.get(name):
                headers[name] = self.headers[name]
        if device:
            headers["X-Jarvis-Token"] = gui.token
            headers["X-Jarvis-Remote"] = "1"     # távolról jött: pl. a gép ne olvassa fel a választ
        connection = http.client.HTTPConnection("127.0.0.1", gui.port, timeout=FORWARD_TIMEOUT)
        try:
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            kind = response.getheader("Content-Type") or "application/octet-stream"
            if kind.startswith("text/event-stream"):
                return self._stream(response)
            data = response.read()
        except OSError:
            raise ApiError(502, "no_window", "A Jarvis ablaka nem válaszol.") from None
        finally:
            connection.close()
        if path == "/" and kind.startswith("text/html"):
            data = remote.inject(data)
        extra = {name: value for name in FORWARD_HEADERS if (value := response.getheader(name))}
        self._send(response.status, data, kind, extra)

    def _stream(self, response: Any) -> None:
        """Az élő események (Server-Sent Events) folyamatosan, ahogy jönnek."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except OSError:     # a távoli ablakot bezárták, vagy a gép felülete leállt
            pass

    # --- nyilvános: párosítás -------------------------------------------------------------------

    def _pair(self) -> None:
        body = self._body()
        try:
            result = self.api.auth.pair(str(body.get("code") or ""), str(body.get("device") or ""))
        except AuthError as exc:
            raise ApiError(403, "pairing_failed", str(exc)) from None
        try:
            self.api.ui.info(f"Telefon párosítva: {result['name']}")
        except Exception:  # noqa: BLE001 - a felület hibája ne rontsa el a párosítást
            pass
        self._json(200, {"ok": True, **result})

    # --- értesítés a laptopra (Web Push) -------------------------------------------------------------

    def _push(self) -> WebPush:
        if self.api.push is None:
            raise ApiError(503, "no_push", "A laptopos értesítés nincs bekapcsolva.")
        return self.api.push

    def r_push_info(self, query: dict[str, list[str]]) -> None:
        push = self._push()
        key = push.public_key()
        self._json(200, {"ok": True, "available": bool(key), "key": key, "reason": "" if key else INSTALL_HINT,
                         "subscriptions": len(push.subscriptions(self.device))})

    def r_push_subscribe(self, query: dict[str, list[str]]) -> None:
        push = self._push()
        if not push_available():
            raise ApiError(503, "no_push", INSTALL_HINT)
        count = push.subscribe(self.device, self._body().get("subscription"))
        self._json(200, {"ok": True, "subscriptions": count})

    def r_push_unsubscribe(self, query: dict[str, list[str]]) -> None:
        removed = self._push().unsubscribe(self.device, str(self._body().get("endpoint") or ""))
        self._json(200, {"ok": True, "removed": removed})

    def _push_answer(self) -> None:
        body = self._body()
        confirm_id = self._push().answer(str(body.get("key") or ""))
        if confirm_id is None or not self.api.ui.resolve_confirm(confirm_id, bool(body.get("approved"))):
            raise ApiError(404, "not_found", "Ez a jóváhagyás már nem vár válaszra.")
        self._json(200, {"ok": True, "approved": bool(body.get("approved"))})

    # --- állapot, beszélgetés, események ---------------------------------------------------------

    def r_status(self, query: dict[str, list[str]]) -> None:
        app = self.api.app
        ready = app is not None and app.assistant is not None
        self._json(200, {
            "ok": True, "api": API_VERSION, "name": getattr(app, "name", "Jarvis"), "ready": ready,
            "state": self.api.bus.state or {}, "paused": bool(getattr(app, "paused", False)),
            "standby": bool(getattr(app, "paused", False) and getattr(app, "standby", False)),
            "busy": bool(getattr(app, "busy", False)), "mode": getattr(app, "mode", "chat"),
            "model": app.cfg.llm.model if app else "",
            "work": bool(ready and app.work is not None and app.work_assistant is not None),
            "confirm": self.api.bus.pending_confirm,
            "scheduler": app.scheduler.status() if ready and app.scheduler else None,
            "unread": app.notices.unread_count() if ready and app.notices else 0,
            "last_event": self.api.log.last_seq(), "device": self.device,
        })

    def r_chat(self, query: dict[str, list[str]]) -> None:
        body = self._body()
        text = str(body.get("text") or "").strip()[:4000]
        if not text:
            raise ApiError(400, "bad_request", "Üres a kérdés (text).")
        self._ready_for_agent(body.get("resume"))
        app = self.api.app
        mode = "work" if body.get("mode") == "work" else "chat"
        request_id = app.new_request_id()
        app.submit_text(text, mode=mode, request_id=request_id, via="phone")
        self._result(request_id, _number(body.get("wait"), 0.0, 0.0, 120.0))

    def r_events(self, query: dict[str, list[str]]) -> None:
        after = int(_number((query.get("after") or ["0"])[0], 0, 0, 1e12))
        wait = _number((query.get("wait") or ["25"])[0], 25.0, 0.0, 55.0)
        events, last, gap = self.api.log.since(after, wait)
        self._json(200, {"ok": True, "events": events, "last": last, "gap": gap})

    def r_stop(self, query: dict[str, list[str]]) -> None:
        self._app().stop_current()
        self._json(202, {"ok": True})

    def r_pause(self, query: dict[str, list[str]]) -> None:
        self._app().submit_pause()
        self._json(202, {"ok": True})

    def r_resume(self, query: dict[str, list[str]]) -> None:
        self._app().submit_resume()
        self._json(202, {"ok": True})

    def r_reset(self, query: dict[str, list[str]]) -> None:
        body = self._body()
        self._app().submit_reset(mode="work" if body.get("mode") == "work" else "chat")
        self._json(202, {"ok": True})

    def r_confirm_get(self, query: dict[str, list[str]]) -> None:
        self._json(200, {"ok": True, "confirm": self.api.bus.pending_confirm})

    def r_confirm_post(self, query: dict[str, list[str]]) -> None:
        body = self._body()
        try:
            confirm_id = int(body.get("id", 0))
        except (TypeError, ValueError):
            raise ApiError(400, "bad_request", "Hibás azonosító (id).") from None
        ok = self.api.ui.resolve_confirm(confirm_id, bool(body.get("approved")))
        if not ok:
            raise ApiError(404, "not_found", "Ez a jóváhagyás már nem vár válaszra.")
        self._json(200, {"ok": True})

    # --- Munka: Adás ---------------------------------------------------------------------

    def r_work(self, query: dict[str, list[str]]) -> None:
        work = self._work()
        self._json(200, {"ok": True, "items": [i.public() for i in work.store.all()], **work.describe()})

    def r_item_create(self, query: dict[str, list[str]]) -> None:
        work = self._work()
        body = self._body()
        item = work.store.create(targets=body.get("targets") or [], prompt=str(body.get("prompt") or ""),
                                 research_wanted=bool(body.get("research_wanted")), pro=bool(body.get("pro")))
        self._json(201, {"ok": True, "item": item.public()})

    def r_item_get(self, item_id: str, query: dict[str, list[str]]) -> None:
        self._json(200, {"ok": True, "item": self._work().store.get(item_id).public()})

    def r_item_update(self, item_id: str, query: dict[str, list[str]]) -> None:
        work = self._work()
        body = self._body()
        fields = body.get("fields") if isinstance(body.get("fields"), dict) else body
        values = {k: v for k, v in fields.items() if k in EDITABLE}
        if not values:
            raise ApiError(400, "bad_request", "Nincs módosítható mező.")
        item = work.store.update(item_id, user=True, **values)
        # ha a fent lévő hirdetésben még a régi ár vagy cím van, megmondja (különben üres)
        self._json(200, {"ok": True, "item": item.public(), "drift_note": work.drift_note(item, set(values))})

    def r_item_delete(self, item_id: str, query: dict[str, list[str]]) -> None:
        self._work().store.delete(item_id)
        self._json(200, {"ok": True})

    def r_photo_add(self, item_id: str, query: dict[str, list[str]]) -> None:
        work = self._work()
        data = self._raw(MAX_PHOTO_BYTES)
        self._json(201, {"ok": True, "item": work.store.add_photo(item_id, data).public()})

    def r_photo_get(self, item_id: str, name: str, query: dict[str, list[str]]) -> None:
        work = self._work()
        try:
            data = work.store.photo_file(item_id, name, thumb=self._flag(query, "thumb")).read_bytes()
        except (ItemError, OSError):
            raise ApiError(404, "not_found", "Nincs ilyen fotó.") from None
        self._send(200, data, "image/jpeg", {"Cache-Control": "private, max-age=300"})

    def r_photo_delete(self, item_id: str, name: str, query: dict[str, list[str]]) -> None:
        self._json(200, {"ok": True, "item": self._work().store.remove_photo(item_id, name).public()})

    def r_photo_order(self, item_id: str, query: dict[str, list[str]]) -> None:
        body = self._body()
        order = [str(n) for n in body.get("order") or []] if isinstance(body.get("order"), list) else []
        self._json(200, {"ok": True, "item": self._work().store.reorder_photos(item_id, order).public()})

    def _unacknowledged(self, work: Any, keys: list[str]) -> list[str]:
        known = set(work.settings.get("acknowledged", []))
        return [k for k in keys if k not in known]

    def r_item_action(self, item_id: str, action: str, query: dict[str, list[str]]) -> None:
        work = self._work()
        body = self._body()
        item = work.store.get(item_id)
        app = self.api.app
        platform = str(body.get("platform") or "")
        extra: dict[str, Any] = {}
        if action == "cancel":
            work.store.set_pending(item_id, None)
            return self._json(200, {"ok": True, "item": work.store.get(item_id).public()})
        if action == "sold":
            return self._sold(work, item, body)
        if action == "dismiss":       # „Nem igaz, fent van”: a háttérellenőrzés téves jelzése egy oldalon
            item = work.store.dismiss_check(item_id, platform)
            return self._json(200, {"ok": True, "item": item.public()})
        if action == "mark":
            status = str(body.get("status") or POSTED)
            if status not in (POSTED, NOT_POSTED, FAILED):
                raise ApiError(400, "bad_request", f"Az állapot: {POSTED}, {NOT_POSTED} vagy {FAILED}.")
            item = work.store.mark(item_id, platform=platform, status=status, url=str(body.get("url") or ""))
            return self._json(200, {"ok": True, "item": item.public()})
        if action == "approve" and work.approve_problem(item_id):     # Pro: a Mehet előtt még kell valami
            raise ApiError(409, "not_ready", work.approve_problem(item_id))
        if action in ("sell", "post", "approve"):
            targets = [platform] if action == "post" else item.targets
            if action == "post" and platform not in work.platforms:
                raise ApiError(400, "bad_request", "Ismeretlen hirdetési oldal (platform).")
            missing = self._unacknowledged(work, targets)
            if missing:
                raise ApiError(409, "acknowledge", "Ezeknél az oldalaknál előbb tudomásul kell venned, hogy a "
                               "szabályaik tilthatják a botokat (POST /api/v1/work/ack): " + ", ".join(missing))
        if action == "reply":
            extra["text"] = str(body.get("text") or "").strip()[:2000]
            if not extra["text"]:
                raise ApiError(400, "bad_request", "Üres válasz (text).")
        if action == "price":
            choice = body.get("choice")
            extra["choice"] = choice if choice in PRICE_CHOICES else str(choice or "")[:20]
        if action == "delist":
            posted = work.posted_on(item)
            wanted = [str(p) for p in body.get("platforms") or [] if str(p) in posted] or posted
            if not wanted:
                raise ApiError(409, "not_posted", "A nyilvántartás szerint sehol nincs fent.")
            extra["platforms"] = wanted
        self._ready_for_agent(body.get("resume"))
        app.submit_work(action, [item_id], platform, **extra)
        self._json(202, {"ok": True, **({"platforms": extra["platforms"]} if "platforms" in extra else {})})

    def _sold(self, work: Any, item: Any, body: dict[str, Any]) -> None:
        if body.get("sold") is False:
            item = work.store.mark(item.id, sold=False)
            return self._json(200, {"ok": True, "item": item.public()})
        where = str(body.get("where") or "")
        where = where if where in work.platforms else ""
        item = work.store.mark(item.id, sold=True)
        if where and item.platforms.get(where, {}).get("status") == POSTED:
            item = work.store.mark(item.id, where, POSTED, note="Itt kelt el.")
        others = [k for k in work.posted_on(item) if k != where]
        delisting = []
        if body.get("delist") and others:
            self._ready_for_agent(body.get("resume"))
            self.api.app.submit_work("delist", [item.id], "", platforms=others)
            delisting = others
        self._json(200, {"ok": True, "item": item.public(), "still_posted": others, "delisting": delisting})

    def r_settings(self, query: dict[str, list[str]]) -> None:
        body = self._body()
        values = body.get("settings") if isinstance(body.get("settings"), dict) else body
        self._json(200, {"ok": True, "settings": self._work().save_settings(values)})

    def r_ack(self, query: dict[str, list[str]]) -> None:
        work = self._work()
        work.acknowledge(str(self._body().get("platform") or ""))
        self._json(200, {"ok": True, "acknowledged": work.settings.get("acknowledged", [])})

    # --- vevői üzenetek ---------------------------------------------------------------------------

    def r_messages(self, query: dict[str, list[str]]) -> None:
        desk = self._work().messages
        self._json(200, {"ok": True, "conversations": desk.conversations(), "unread": desk.unread_count(),
                         "browser": desk.browser is not None})

    def r_message_get(self, conv_id: str, query: dict[str, list[str]]) -> None:
        desk = self._work().messages
        self._json(200, {"ok": True, "conversation": desk.public(desk.store.get(conv_id))})

    def r_message_save(self, conv_id: str, query: dict[str, list[str]]) -> None:
        desk = self._work().messages
        draft = str(self._body().get("draft") or "")[:MAX_TEXT]
        self._json(200, {"ok": True, "conversation": desk.public(desk.store.set_draft(conv_id, draft))})

    def r_message_action(self, conv_id: str, action: str, query: dict[str, list[str]]) -> None:
        """Vázlat kérése, megnézve, vagy küldés (a küldés gombja előtt jóváhagyást kér: POST /confirm)."""
        desk = self._work().messages
        body = self._body()
        conv = desk.store.get(conv_id)
        if action == "seen":
            return self._json(200, {"ok": True, "conversation": desk.public(desk.store.seen(conv_id))})
        extra: dict[str, Any] = {}
        if action == "send":
            extra["text"] = str(body.get("text") or "").strip()[:MAX_TEXT]
            if not extra["text"]:
                raise ApiError(400, "bad_request", "Üres üzenet (text).")
            if conv["platform"] not in desk.platforms or desk.browser is None:
                raise ApiError(409, "no_browser", "Ezt az üzenetet most nem tudom elküldeni (nincs böngésző, vagy ennek "
                               "az oldalnak az üzeneteit nem kezelem).")
        self._ready_for_agent(body.get("resume"))
        self.api.app.submit_work(f"message_{action}", conversation=conv_id, **extra)
        self._json(202, {"ok": True})

    def r_messages_check(self, query: dict[str, list[str]]) -> None:
        scheduler = self._scheduler()
        if "messages" not in scheduler.jobs:
            raise ApiError(409, "no_browser", "Az üzenetek olvasásához Jarvis saját böngészője kell.")
        self._ready_for_agent(self._flag(query, "resume"))
        self.api.app.run_job_now("messages")
        self._json(202, {"ok": True})

    # --- háttérmunka, figyelők, értesítések --------------------------------------------------------

    def _scheduler(self) -> Any:
        app = self._app()
        if app.scheduler is None:
            raise ApiError(503, "no_scheduler", "A háttérmunka nem fut.")
        return app.scheduler

    def r_jobs(self, query: dict[str, list[str]]) -> None:
        self._json(200, {"ok": True, **self._scheduler().status()})

    def r_job_run(self, key: str, query: dict[str, list[str]]) -> None:
        scheduler = self._scheduler()
        if key not in scheduler.jobs:
            raise ApiError(404, "not_found", "Nincs ilyen háttérmunka.")
        self._ready_for_agent(self._flag(query, "resume"))
        self.api.app.run_job_now(key)
        self._json(202, {"ok": True})

    def r_snooze(self, query: dict[str, list[str]]) -> None:
        until = self._scheduler().snooze(_number(self._body().get("minutes"), 0.0, 0.0, 24 * 60.0))
        self._json(200, {"ok": True, "snoozed_until": until or None})

    def _watches(self) -> Any:
        app = self._app()
        if app.watches is None or not app.toolbox.has("watch_add"):
            raise ApiError(503, "no_search", "A figyelőkhöz webes keresés kell (pip install ddgs).")
        return app.watches

    def r_watches(self, query: dict[str, list[str]]) -> None:
        self._json(200, {"ok": True, "watches": [w.public() for w in self._watches().all()]})

    def r_watch_add(self, query: dict[str, list[str]]) -> None:
        body = self._body()
        watch = self._watches().add(str(body.get("query") or ""), str(body.get("site") or ""),
                                    str(body.get("recency") or "hét"))
        self._json(201, {"ok": True, "watch": watch.public()})

    def r_watch_delete(self, watch_id: str, query: dict[str, list[str]]) -> None:
        self._json(200, {"ok": True, "watch": self._watches().remove(watch_id).public()})

    def r_notifications(self, query: dict[str, list[str]]) -> None:
        notices = self._app().notices
        items = notices.list(unread_only=self._flag(query, "unread"),
                             limit=int(_number((query.get("limit") or ["50"])[0], 50, 1, 150))) if notices else []
        self._json(200, {"ok": True, "notifications": items, "unread": notices.unread_count() if notices else 0})

    def r_notifications_read(self, query: dict[str, list[str]]) -> None:
        notices = self._app().notices
        body = self._body()
        ids = [str(i) for i in body.get("ids") or []] if isinstance(body.get("ids"), list) else None
        count = notices.mark_read(ids) if notices else 0
        self._json(200, {"ok": True, "marked": count})

    # --- eszközök ---------------------------------------------------------------------------------

    def r_devices(self, query: dict[str, list[str]]) -> None:
        devices = [{**d, "current": d["id"] == self.device} for d in self.api.auth.devices()]
        self._json(200, {"ok": True, "devices": devices})

    def r_device_delete(self, device_id: str, query: dict[str, list[str]]) -> None:
        if not self.api.auth.revoke(device_id):
            raise ApiError(404, "not_found", "Nincs ilyen eszköz.")
        self._json(200, {"ok": True})


_ID = r"([0-9a-f]{8})"
_CONV = r"([0-9a-f]{10})"
ROUTES: list[tuple[str, re.Pattern[str], str]] = [(m, re.compile(p), n) for m, p, n in (
    ("GET", r"/status", "r_status"),
    ("POST", r"/chat", "r_chat"),
    ("GET", r"/events", "r_events"),
    ("POST", r"/stop", "r_stop"),
    ("POST", r"/pause", "r_pause"),
    ("POST", r"/resume", "r_resume"),
    ("POST", r"/reset", "r_reset"),
    ("GET", r"/confirm", "r_confirm_get"),
    ("POST", r"/confirm", "r_confirm_post"),
    ("GET", r"/work", "r_work"),
    ("POST", r"/work/items", "r_item_create"),
    ("GET", rf"/work/items/{_ID}", "r_item_get"),
    ("PATCH", rf"/work/items/{_ID}", "r_item_update"),
    ("DELETE", rf"/work/items/{_ID}", "r_item_delete"),
    ("POST", rf"/work/items/{_ID}/photos", "r_photo_add"),
    ("POST", rf"/work/items/{_ID}/photos/order", "r_photo_order"),
    ("GET", rf"/work/items/{_ID}/photos/(\d{{2}}\.jpg)", "r_photo_get"),
    ("DELETE", rf"/work/items/{_ID}/photos/(\d{{2}}\.jpg)", "r_photo_delete"),
    ("POST", rf"/work/items/{_ID}/(sell|reply|price|cancel|post|research|sold|delist|mark|dismiss|approve)",
     "r_item_action"),
    ("PUT", r"/work/settings", "r_settings"),
    ("POST", r"/work/ack", "r_ack"),
    ("GET", r"/work/messages", "r_messages"),
    ("POST", r"/work/messages/check", "r_messages_check"),
    ("GET", rf"/work/messages/{_CONV}", "r_message_get"),
    ("PUT", rf"/work/messages/{_CONV}/draft", "r_message_save"),
    ("POST", rf"/work/messages/{_CONV}/(draft|seen|send)", "r_message_action"),
    ("GET", r"/jobs", "r_jobs"),
    ("POST", r"/jobs/snooze", "r_snooze"),
    ("POST", r"/jobs/([a-z_]+)/run", "r_job_run"),
    ("GET", r"/watches", "r_watches"),
    ("POST", r"/watches", "r_watch_add"),
    ("DELETE", r"/watches/([0-9a-f]{6})", "r_watch_delete"),
    ("GET", r"/notifications", "r_notifications"),
    ("POST", r"/notifications/read", "r_notifications_read"),
    ("GET", r"/push", "r_push_info"),
    ("POST", r"/push/subscribe", "r_push_subscribe"),
    ("POST", r"/push/unsubscribe", "r_push_unsubscribe"),
    ("GET", r"/devices", "r_devices"),
    ("DELETE", r"/devices/([0-9a-f]{8})", "r_device_delete"),
)]


def start_api(app: Any, ui: Any, bus: Any, data_root: Path) -> ApiServer:
    """Elindítja a telefonos kapcsolatot (a run_gui hívja, ha az [api] be van kapcsolva)."""
    log = EventLog()
    bus.add_listener(log.add)
    auth = PhoneAuth(Path(data_root) / "telefon.json")
    server = ApiServer(app, ui, bus, app.cfg.api, auth, log)
    server.push = WebPush(Path(data_root) / "push.json", devices=lambda: {d["id"] for d in auth.devices()})
    bus.add_listener(server.push.on_event)
    server.start()
    return server
