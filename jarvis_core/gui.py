"""Grafikus felület: kis helyi webszerver és egy alkalmazásablak (Edge --app mód).

Nem kell hozzá semmilyen külön csomag. A Jarvis az eseményeket (állapot, szöveg, eszközök)
Server-Sent Events folyamon küldi az oldalnak; az oldal egyszerű POST-kérésekkel válaszol.

Biztonság: csak a 127.0.0.1-en figyel, minden kéréshez egy induláskor generált véletlen kulcs
kell, és ellenőrzi a Host és Origin fejlécet. Így egy weboldal (akár olyan, amit a Jarvis
böngészője nyitott meg) nem tud parancsot küldeni a Jarvisnak.
"""
from __future__ import annotations

import contextlib
import itertools
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from . import console, creative_templates, instance, qr, remote
from .app import STATE_TEXT, JarvisApp, tool_label
from .config import GuiCfg, app_data
from .creative import MAX_TEXT, StoryError
from .deals import SALE_RATIO, SHIPPING, DealError, bundle_parts, profit_report
from .dossier import open_folder
from .gpu import GpuMonitor
from .messages import CONV_ID_RE, MAX_TEXT as MAX_MESSAGE
from .selling import LEDGER_FIELDS, MAX_PHOTO_BYTES, ItemError, parse_price, round_price
from .tools import summarize_result
from .watches import WatchError
from .work import PRICE_CHOICES

UI_DIR = Path(__file__).resolve().parent / "ui"
PAGE = UI_DIR / "index.html"
STATIC = {"style.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
          "work.js": "text/javascript; charset=utf-8", "creative.js": "text/javascript; charset=utf-8"}
PHOTO_GET_RE = re.compile(r"^/api/work/photo/([0-9a-f]{8})/(\d{2}\.jpg)$")
PHOTO_POST_RE = re.compile(r"^/api/work/items/([0-9a-f]{8})/photo$")
# Az Automata kereskedés egy keresésének beállításai (deals.DealHunter.add_search, update_search).
DEAL_FIELDS = ("query", "sites", "max_price", "min_price", "discount", "min_profit", "every", "exclude", "reference")
# Amit a felületről át lehet írni egy tárgyon, és amit el lehet indítani.
EDITABLE = {"targets", "prompt", "research_wanted", "product", "title", "description", "category", "brand",
            "size", "condition", "color", "gender", "material", "price", "min_price", "location", "shipping", "bought_price",
            "sold_price", *LEDGER_FIELDS}
ACTIONS = {"sell", "reply", "price", "cancel", "post", "research", "login", "delist", "sold", "approve"}
IS_WINDOWS = sys.platform == "win32"
CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
       "connect-src 'self'; img-src 'self' data: blob:; base-uri 'none'; form-action 'none'")


class EventBus:
    """Az események szétosztása a megnyitott ablakoknak, újracsatlakozáskor visszajátszással."""

    def __init__(self, limit: int = 400):
        self._lock = threading.Lock()
        self._clients: set[queue.Queue] = set()
        self._history: list[dict[str, Any]] = []
        self._limit = limit
        self._state: dict[str, Any] | None = None
        self._confirm: dict[str, Any] | None = None
        self.hello: dict[str, Any] = {}
        self.ever_connected = False
        self.last_disconnect = time.monotonic()
        self._listeners: list[Any] = []

    def add_listener(self, listener: Any) -> None:
        """Minden esemény ide is megy (pl. a telefonos eseménynaplóba)."""
        self._listeners.append(listener)

    def _notify_listeners(self, event: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - egy figyelő hibája ne állítsa meg a felületet
                pass

    @property
    def state(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._state) if self._state else None

    @property
    def pending_confirm(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._confirm) if self._confirm else None

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def _record(self, event: dict[str, Any]) -> None:
        if event.get("type") == "tool":  # egy eszközhívásnak csak a legutolsó állapota kell
            self._history = [e for e in self._history
                             if not (e.get("type") == "tool" and e.get("id") == event.get("id"))]
        self._history.append(event)
        del self._history[:-self._limit]

    def publish(self, event: dict[str, Any], record: bool = True) -> None:
        with self._lock:
            if record:
                self._record(event)
            clients = list(self._clients)
        for client in clients:
            client.put(event)
        self._notify_listeners(event)

    def record(self, event: dict[str, Any]) -> None:
        """Csak a visszajátszáshoz jegyzi fel (a nyitott ablakok már látták darabokban)."""
        with self._lock:
            self._record(event)
        self._notify_listeners(event)

    def set_state(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._state = event
        self.publish(event, record=False)

    def set_hello(self, hello: dict[str, Any]) -> None:
        with self._lock:
            self.hello = hello
        self.publish({"type": "hello", **hello, "replay": False}, record=False)

    def set_confirm(self, event: dict[str, Any] | None) -> None:
        with self._lock:
            self._confirm = event
        if event is not None:
            self.publish(event, record=False)

    def subscribe(self) -> queue.Queue:
        client: queue.Queue = queue.Queue()
        with self._lock:
            client.put({"type": "hello", **self.hello, "replay": True})
            for event in self._history:
                client.put(event)
            if self._state is not None:
                client.put(self._state)
            if self._confirm is not None:
                client.put(self._confirm)
            self._clients.add(client)
            self.ever_connected = True
        return client

    def unsubscribe(self, client: queue.Queue) -> None:
        with self._lock:
            self._clients.discard(client)
            self.last_disconnect = time.monotonic()


class GuiUI:
    """A Jarvis üzeneteit eseményekké alakítja az ablaknak, és a konzolra is naplóz.

    A `channel` mondja meg, melyik menü naplójába kerül az üzenet (chat = Beszélgetés,
    work = Munka); a JarvisApp állítja át, amíg egy Munka menüs kérésen dolgozik. Egy szálnak saját is lehet
    (own_channel: pl. az eladás előkészítése a Munka menübe ír, közben az ágens-szál beszélgetése a helyén marad).
    """

    def __init__(self, bus: EventBus, name: str):
        self.bus = bus
        self.name = name
        self._channel = "chat"
        self._own = threading.local()
        self._segment: list[str] = []
        self._segment_channel = "chat"
        self._lock = threading.Lock()
        self._confirm_ids = itertools.count(1)
        self._confirms: dict[int, dict[str, Any]] = {}
        self.request_id: str | None = None   # a telefonról jött kérés azonosítója (amíg Jarvis rajta dolgozik)

    @property
    def channel(self) -> str:
        return getattr(self._own, "channel", None) or self._channel

    @channel.setter
    def channel(self, value: str) -> None:
        self._channel = value

    @contextlib.contextmanager
    def own_channel(self, channel: str):
        """Ezen a szálon az üzenetek ebbe a menübe kerülnek (a többi szálé marad, ahol volt)."""
        previous = getattr(self._own, "channel", None)
        self._own.channel = channel
        try:
            yield
        finally:
            self._own.channel = previous

    def _publish(self, event: dict[str, Any], record: bool = True) -> None:
        event.setdefault("channel", self.channel)
        if self.request_id:
            event.setdefault("request_id", self.request_id)
        self.bus.publish(event, record=record)

    def hello(self, info: dict[str, Any]) -> None:
        """Megváltozott, mi fut (pl. Szünet után nincs hang): a felület és a telefon is megtudja."""
        self.bus.set_hello(info)

    def request_done(self, request_id: str) -> None:
        self.bus.publish({"type": "request_done", "request_id": request_id}, record=False)

    def notification(self, notice: dict[str, Any]) -> None:
        self.bus.publish({"type": "notification", "notice": notice}, record=False)

    def _close_segment(self) -> None:
        with self._lock:
            text = "".join(self._segment).strip()
            self._segment.clear()
            channel = self._segment_channel
        if text:
            event = {"type": "assistant", "text": text, "channel": channel}
            if self.request_id:
                event["request_id"] = self.request_id
            self.bus.record(event)
            self.bus.publish({"type": "segment_end", "channel": channel}, record=False)
            console.assistant(self.name, text)

    def state(self, state: str, detail: str = "") -> None:
        self.bus.set_state({"type": "state", "state": state,
                            "text": STATE_TEXT.get(state, state), "detail": detail})

    def user(self, text: str, via: str) -> None:
        self._publish({"type": "user", "text": text, "via": via})
        console.user(text)

    def delta(self, text: str) -> None:
        with self._lock:
            if not self._segment:
                self._segment_channel = self.channel
            self._segment.append(text)
        self._publish({"type": "delta", "text": text}, record=False)

    def assistant_end(self, text: str, streamed: bool) -> None:
        if streamed:
            self._close_segment()
        else:
            self._close_segment()
            self._publish({"type": "assistant", "text": text})
            console.assistant(self.name, text)

    def assistant_abort(self) -> None:
        self._close_segment()

    def tool_start(self, call_id: int, name: str, args: dict[str, Any]) -> None:
        self._close_segment()
        self._publish({"type": "tool", "id": call_id, "label": tool_label(name, args),
                       "status": "running", "summary": ""})

    def tool_done(self, call_id: int, name: str, args: dict[str, Any], result: str) -> None:
        failed = result.startswith("HIBA")
        summary = summarize_result(result.removeprefix("HIBA: "), limit=160)
        self._publish({"type": "tool", "id": call_id, "label": tool_label(name, args),
                       "status": "failed" if failed else "done", "summary": summary})
        console.tool(f"{name} -> {summary}")

    def notice(self, text: str) -> None:
        if getattr(self._own, "channel", None) is None:   # a saját menüjébe író szál ne vágja ketté a folyó választ
            self._close_segment()
        self._publish({"type": "assistant", "text": text})
        console.assistant(self.name, text)

    def info(self, text: str) -> None:
        self._publish({"type": "info", "text": text.strip()})
        console.info(text)

    def warn(self, text: str) -> None:
        self._publish({"type": "warn", "text": text})
        console.warn(text)

    def error(self, text: str) -> None:
        self._publish({"type": "error", "text": text})
        console.error(text)

    def timing(self, text: str) -> None:
        self._publish({"type": "timing", "text": text})

    def work_item(self, item: dict[str, Any]) -> None:
        """Egy tárgy megváltozott (a felület frissíti a kártyáját)."""
        self.bus.publish({"type": "work_item", "item": item}, record=False)

    def work_removed(self, item_id: str) -> None:
        self.bus.publish({"type": "work_removed", "id": item_id}, record=False)

    def work_message(self, conversation: dict[str, Any]) -> None:
        """Egy vevői beszélgetés megváltozott (új üzenet, vázlat, elküldve): a felület frissíti."""
        self.bus.publish({"type": "work_message", "conversation": conversation}, record=False)

    def work_deals(self) -> None:
        """Az Automata kereskedés állapota megváltozott (pl. a telefonról elindítottad): minden ablak újratölti."""
        self.bus.publish({"type": "work_deals"}, record=False)

    def creative_event(self, event: dict[str, Any]) -> None:
        """A Kreatív menü eseménye (élő írás, egy történet változása); visszajátszani nem kell, a felület
        újracsatlakozáskor lekéri a történetet."""
        self.bus.publish(event, record=False)

    def confirm(self, prompt: str, stop_event: threading.Event) -> bool:
        confirm_id = next(self._confirm_ids)
        done = threading.Event()
        entry = {"done": done, "approved": False}
        self._confirms[confirm_id] = entry
        self.bus.set_confirm({"type": "confirm", "id": confirm_id, "prompt": prompt})
        console.warn(prompt + "  (jóváhagyás az ablakban)")
        deadline = time.monotonic() + 300
        while not done.wait(0.1):
            if stop_event.is_set() or time.monotonic() > deadline:
                break
        self._confirms.pop(confirm_id, None)
        self.bus.set_confirm(None)
        approved = bool(entry["approved"])
        self.bus.publish({"type": "confirm_done", "id": confirm_id, "approved": approved})
        return approved

    def resolve_confirm(self, confirm_id: int, approved: bool) -> bool:
        entry = self._confirms.get(confirm_id)
        if entry is None:
            return False
        entry["approved"] = bool(approved)
        entry["done"].set()
        return True


class GuiServer:
    def __init__(self, app: JarvisApp | None, ui: GuiUI, bus: EventBus, port: int = 0):
        self.app = app
        self.ui = ui
        self.bus = bus
        self.phone: Any = None      # a telefonos kapcsolat (api.ApiServer), ha fut
        self.gpus: GpuMonitor | None = None   # a GPU-sávok mérése (az első lekérdezéskor indul)
        self._gpus_lock = threading.Lock()
        self.token = secrets.token_urlsafe(24)
        self.closing = threading.Event()
        handler = type("JarvisHandler", (_Handler,), {"gui": self})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", int(port)), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/?t={self.token}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="jarvis-gui", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def gpu_monitor(self) -> GpuMonitor:
        with self._gpus_lock:
            if self.gpus is None:
                self.gpus = GpuMonitor()
            return self.gpus

    def stop(self) -> None:
        self.closing.set()
        if self.gpus is not None:
            self.gpus.stop()
        self.httpd.shutdown()
        self.httpd.server_close()


class _Handler(BaseHTTPRequestHandler):
    gui: GuiServer
    server_version = "Jarvis"

    def log_message(self, *args: Any) -> None:  # a konzolt nem töltjük tele kérésnaplóval
        pass

    # --- ellenőrzések -----------------------------------------------------------------

    def _allowed_hosts(self) -> set[str]:
        port = self.gui.port
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in self._allowed_hosts()

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{h}" for h in self._allowed_hosts()}

    def _token_ok(self, token: str | None) -> bool:
        return bool(token) and secrets.compare_digest(token, self.gui.token)

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: dict[str, Any]) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _forbidden(self) -> None:
        self._send(403, "Hiányzó vagy rossz kulcs. Nyisd meg a Jarvis által kiírt címet.".encode("utf-8"),
                   "text/plain; charset=utf-8")

    # --- kérések ----------------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        token = parse_qs(parsed.query).get("t", [""])[0]
        if not self._host_ok():
            return self._forbidden()
        if parsed.path == "/":
            if not self._token_ok(token):
                return self._forbidden()
            page = PAGE.read_bytes()
            return self._send(200, page, "text/html; charset=utf-8",
                              {"Content-Security-Policy": CSP, "Referrer-Policy": "no-referrer"})
        if parsed.path == "/events":
            if not self._token_ok(token):
                return self._forbidden()
            return self._stream_events()
        if parsed.path.startswith("/ui/") and parsed.path[4:] in STATIC:
            name = parsed.path[4:]
            return self._send(200, (UI_DIR / name).read_bytes(), STATIC[name])
        if parsed.path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if parsed.path.startswith("/api/work/"):
            if not self._token_ok(token):
                return self._forbidden()
            return self._work_get(parsed.path, parse_qs(parsed.query))
        if parsed.path.startswith("/api/creative/"):
            if not self._token_ok(token):
                return self._forbidden()
            return self._creative_get(parsed.path, parse_qs(parsed.query))
        if parsed.path == "/api/phone":
            if not self._token_ok(token):
                return self._forbidden()
            return self._json(200, self._phone_info())
        if parsed.path == "/api/gpus":
            if not self._token_ok(token):
                return self._forbidden()
            app = self.gui.app
            if app is not None and not app.cfg.gui.gpu_bars:
                return self._json(200, {"ok": True, "available": False, "gpus": []})
            return self._json(200, {"ok": True, **self.gui.gpu_monitor().snapshot()})
        if parsed.path == "/api/browser/screen":   # a captcha megoldásához (telefonon is): a böngésző képe
            if not self._token_ok(token):
                return self._forbidden()
            app = self.gui.app
            try:
                data = app.call_browser(lambda browser: browser.screen()) if app is not None else b""
            except Exception as exc:  # noqa: BLE001 - nem fut a böngésző, vagy a Jarvis épp dolgozik
                return self._json(409, {"ok": False, "error": str(exc)})
            return self._send(200, data, "image/jpeg", {"Cache-Control": "no-store"})
        if parsed.path == "/api/jobs":
            if not self._token_ok(token):
                return self._forbidden()
            return self._json(200, self._jobs_info())
        if parsed.path == "/api/notices":
            if not self._token_ok(token):
                return self._forbidden()
            return self._json(200, self._notices_info())
        if parsed.path == "/api/ping":
            if not self._token_ok(token):
                return self._forbidden()
            return self._json(200, {"ok": True, "name": "Jarvis", "pid": os.getpid()})
        self._send(404, b"", "text/plain")

    def _phone_info(self) -> dict[str, Any]:
        phone = self.gui.phone
        if phone is None:
            return {"ok": True, "enabled": False}
        return {"ok": True, "enabled": True, "host": phone.host, "port": phone.port,
                "address": remote.tailscale_address(phone.port), "devices": phone.auth.devices()}

    def _jobs_info(self) -> dict[str, Any]:
        """Az Automatikus munkák ablaknak: a figyelők és a háttérmunkák (be- vagy kikapcsolva)."""
        app = self.gui.app
        scheduler = app.scheduler if app is not None else None
        if scheduler is None or app.watches is None:
            return {"ok": True, "ready": False}
        status = scheduler.status()
        return {"ok": True, "ready": True, "enabled": status["enabled"],
                "search": app.toolbox is not None and app.toolbox.has("watch_add"),
                "watches": [w.public() for w in app.watches.all()], "jobs": status["jobs"]}

    def _notices_info(self) -> dict[str, Any]:
        """Az Értesítések ablaknak: a legutóbbi értesítések (a gombjaikkal együtt), és hány olvasatlan."""
        app = self.gui.app
        notices = app.notices if app is not None else None
        if notices is None:
            return {"ok": True, "notifications": [], "unread": 0}
        return {"ok": True, "notifications": notices.list(limit=50), "unread": notices.unread_count()}

    def _work(self) -> Any:
        app = self.gui.app
        return app.work if app is not None and app.work_assistant is not None else None

    def _work_get(self, path: str, query: dict[str, list[str]]) -> None:
        work = self._work()
        if work is None:
            return self._json(503, {"ok": False, "error": "A Munka menü még indul."})
        if path == "/api/work/items":
            return self._json(200, {"ok": True, "items": [i.public() for i in work.store.all()],
                                    **work.describe()})
        if path == "/api/work/deals":
            return self._json(200, {"ok": True, **work.deals.state(), "profit": profit_report(work.store.all()),
                                    "instant": work.instant.state() if work.instant is not None else None})
        if path == "/api/work/ledger":       # Vételeim: a megvett tárgyak költségei, haszna, és mennyiért adják most
            return self._json(200, self._ledger(work))
        if path == "/api/work/messages":
            return self._json(200, {"ok": True, "conversations": work.messages.conversations(),
                                    "unread": work.messages.unread_count(), "browser": work.messages.browser is not None})
        match = PHOTO_GET_RE.match(path)
        if match:
            try:
                file = work.store.photo_file(match.group(1), match.group(2),
                                             thumb=query.get("thumb", [""])[0] == "1")
                data = file.read_bytes()
            except (ItemError, OSError):
                return self._send(404, b"", "text/plain")
            return self._send(200, data, "image/jpeg")
        self._send(404, b"", "text/plain")

    def _stream_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        client = self.gui.bus.subscribe()
        try:
            while not self.gui.closing.is_set():
                try:
                    event = client.get(timeout=3)
                except queue.Empty:
                    # Életjel: így néhány másodpercen belül kiderül, ha az ablakot bezárták.
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                data = json.dumps(event, ensure_ascii=False)
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except OSError:  # az ablakot bezárták vagy frissítették
            pass
        finally:
            self.gui.bus.unsubscribe(client)

    def do_POST(self) -> None:
        if not (self._host_ok() and self._origin_ok() and self._token_ok(self.headers.get("X-Jarvis-Token"))):
            return self._forbidden()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"ok": False})
        parsed = urlparse(self.path)
        path = parsed.path
        # A távoli ablakból (telefon, laptop) jött: a naplóban így jelenik meg.
        remote = self.headers.get("X-Jarvis-Remote") == "1"
        upload = PHOTO_POST_RE.match(path)
        if upload:   # fotó: nyers képadat, nem JSON
            return self._work_photo(upload.group(1), length)
        if length > 262144:
            return self._json(413, {"ok": False})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"ok": False})
        if not isinstance(body, dict):
            return self._json(400, {"ok": False})
        app = self.gui.app
        if path == "/api/confirm":
            ok = self.gui.ui.resolve_confirm(int(body.get("id", 0)), bool(body.get("approved")))
            return self._json(200 if ok else 404, {"ok": ok})
        if path.startswith("/api/phone/"):
            return self._phone_post(path, body)
        if path == "/api/quit":   # python jarvis.py --quit
            if app is not None:
                app.exit_requested.set()
            return self._json(202, {"ok": True})
        if app is None or app.assistant is None:
            return self._json(503, {"ok": False, "error": "A Jarvis még indul."})
        if path == "/api/browser/tap":   # amire a böngésző képén böktél: a te kattintásod (captcha)
            try:
                x, y = float(body.get("x")), float(body.get("y"))
                captcha = app.call_browser(lambda browser: (browser.tap(x, y), browser.challenged())[1])
            except (TypeError, ValueError):
                return self._json(400, {"ok": False})
            except Exception as exc:  # noqa: BLE001
                return self._json(409, {"ok": False, "error": str(exc)})
            return self._json(200, {"ok": True, "captcha": captcha})
        if path == "/api/pause":
            app.submit_pause()
            return self._json(202, {"ok": True})
        if path == "/api/resume":
            app.submit_resume()
            return self._json(202, {"ok": True})
        if path.startswith("/api/work/"):
            return self._work_post(path, body, remote)
        if path.startswith("/api/creative/"):
            return self._creative_post(path, body)
        if path.startswith("/api/jobs/"):
            return self._jobs_post(path, body)
        if path == "/api/notices/read":
            ids = [str(i) for i in body.get("ids")][:200] if isinstance(body.get("ids"), list) else None
            return self._json(200, {"ok": True, "marked": app.notices.mark_read(ids) if app.notices else 0})
        mode = str(body.get("mode") or "chat")
        if path == "/api/send":
            app.submit_text(str(body.get("text", ""))[:4000], mode=mode, via="remote" if remote else "text")
        elif path == "/api/stop":
            app.stop_current()
        elif path == "/api/reset":
            app.submit_reset(mode=mode)
        elif path == "/api/mode":
            app.set_mode(mode)
        else:
            return self._json(404, {"ok": False})
        self._json(202, {"ok": True})

    def _phone_post(self, path: str, body: dict[str, Any]) -> None:
        phone = self.gui.phone
        if phone is None:
            return self._json(503, {"ok": False, "error": "A telefonos kapcsolat ki van kapcsolva ([api] enabled)."})
        if path == "/api/phone/code":
            result = phone.auth.new_code()
            address = remote.tailscale_address(phone.port)
            if address:     # QR-kód: a telefon kamerája beolvassa, és a kóddal együtt nyitja meg a gép címét
                link = remote.pair_link(address, result["code"])
                result.update(link=link, qr=qr.svg(link))
            return self._json(200, {"ok": True, **result})
        if path == "/api/phone/revoke":
            ok = phone.auth.revoke(str(body.get("id") or ""))
            return self._json(200 if ok else 404, {"ok": ok, "devices": phone.auth.devices()})
        return self._json(404, {"ok": False})

    def _jobs_post(self, path: str, body: dict[str, Any]) -> None:
        app = self.gui.app
        if app.scheduler is None or app.watches is None:
            return self._json(503, {"ok": False, "error": "A háttérmunka még indul."})
        if path == "/api/jobs/watch-delete":
            ids = body.get("ids") if isinstance(body.get("ids"), list) else [body.get("id")]
            known = {w.id for w in app.watches.all()}
            wanted = [str(i) for i in ids if str(i) in known]
            if not wanted:
                return self._json(404, {"ok": False, "error": "Nincs ilyen figyelő (talán már törölted)."})
            removed = []
            for watch_id in wanted:
                try:
                    removed.append(app.watches.remove(watch_id).public())
                except WatchError:
                    continue
            return self._json(200, {**self._jobs_info(), "removed": removed})
        if path == "/api/jobs/toggle":
            if not app.scheduler.set_enabled(str(body.get("key") or ""), bool(body.get("on"))):
                return self._json(404, {"ok": False, "error": "Nincs ilyen háttérmunka."})
            return self._json(200, self._jobs_info())
        return self._json(404, {"ok": False})

    def _work_photo(self, item_id: str, length: int) -> None:
        work = self._work()
        if work is None:
            return self._json(503, {"ok": False, "error": "A Munka menü még indul."})
        if length <= 0:
            return self._json(400, {"ok": False, "error": "Üres kép."})
        if length > MAX_PHOTO_BYTES:
            return self._json(413, {"ok": False, "error": "A kép túl nagy (legfeljebb 25 MB)."})
        data = self.rfile.read(length)
        try:
            item = work.store.add_photo(item_id, data)
        except ItemError as exc:
            return self._json(400, {"ok": False, "error": str(exc)})
        self._json(200, {"ok": True, "item": item.public()})

    def _work_post(self, path: str, body: dict[str, Any], remote: bool = False) -> None:
        app = self.gui.app
        work = self._work()
        if work is None:
            return self._json(503, {"ok": False, "error": "A Munka menü még indul."})
        item_id = str(body.get("id") or "")
        try:
            if path.startswith("/api/work/messages/"):
                return self._messages_post(path, body, remote)
            if path == "/api/work/deals":
                return self._deals_post(body, remote)
            if path == "/api/work/ledger":
                return self._ledger_post(body)
            if path == "/api/work/new":
                values = {"targets": body.get("targets") or [], "prompt": str(body.get("prompt") or ""),
                          "research_wanted": bool(body.get("research_wanted")),
                          "pro": bool(body.get("pro"))}    # Pro: elég a fotó, az adatokat és az árat Jarvis adja
                return self._json(200, {"ok": True, "item": work.store.create(**values).public()})
            if path == "/api/work/update":
                fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
                values = {k: v for k, v in fields.items() if k in EDITABLE}
                item = work.store.update(item_id, user=True, **values)
                # ha a fent lévő hirdetésben még a régi ár vagy cím van, megmondja (a felület kiírja)
                return self._json(200, {"ok": True, "item": item.public(),
                                        "drift_note": work.drift_note(item, set(values))})
            if path == "/api/work/dismiss":
                # „Nem igaz, fent van”: a háttérellenőrzés téves jelzése (elkelt, eltűnt) egy oldalon
                item = work.store.dismiss_check(item_id, str(body.get("platform") or ""))
                return self._json(200, {"ok": True, "item": item.public()})
            if path == "/api/work/photos":
                if body.get("remove"):
                    item = work.store.remove_photo(item_id, str(body["remove"]))
                else:
                    order = body.get("order") if isinstance(body.get("order"), list) else []
                    item = work.store.reorder_photos(item_id, [str(n) for n in order])
                return self._json(200, {"ok": True, "item": item.public()})
            if path == "/api/work/delete":
                work.store.delete(item_id)
                return self._json(200, {"ok": True})
            if path == "/api/work/mark":
                sold = body.get("sold")
                item = work.store.mark(item_id, platform=str(body.get("platform") or ""),
                                       status=str(body.get("status") or "feltöltve"),
                                       note=str(body.get("note") or "")[:300],
                                       sold=None if sold is None else bool(sold))
                return self._json(200, {"ok": True, "item": item.public()})
            if path == "/api/work/action":
                action = str(body.get("action") or "")
                ids = [str(i) for i in body.get("ids") or [] if isinstance(i, str)][:50]
                platform = str(body.get("platform") or "")
                if action not in ACTIONS:
                    return self._json(400, {"ok": False, "error": "Ismeretlen feladat."})
                if action != "login":
                    if not ids:
                        return self._json(400, {"ok": False, "error": "Melyik tárgy?"})
                    work.store.get(ids[0])   # ItemError, ha nincs ilyen
                if action in ("post", "login") and platform not in work.platforms:
                    return self._json(400, {"ok": False, "error": "Ismeretlen hirdetési oldal."})
                extra: dict[str, Any] = {}
                if action == "reply":
                    extra["text"] = str(body.get("text") or "").strip()[:2000]
                    if not extra["text"]:
                        return self._json(400, {"ok": False, "error": "Üres válasz."})
                if action == "price":
                    choice = body.get("choice")
                    extra["choice"] = choice if choice in PRICE_CHOICES else str(choice or "")[:20]
                if action == "delist":
                    extra["platforms"] = [str(p) for p in body.get("platforms") or [] if str(p) in work.platforms]
                if action == "approve" and work.approve_problem(ids[0]):     # Pro: a Mehet előtt még kell valami
                    return self._json(400, {"ok": False, "error": work.approve_problem(ids[0])})
                if action == "sold":
                    extra["where"] = str(body.get("where") or "") if str(body.get("where") or "") in work.platforms else ""
                    extra["delist"] = bool(body.get("delist"))
                app.submit_work(action, ids, platform, **extra)
                return self._json(202, {"ok": True})
            if path == "/api/work/dossier":
                item = work.store.get(item_id)
                name = str(item.platforms.get(str(body.get("platform") or ""), {}).get("dossier") or "")
                try:
                    folder = work.dossiers.path(name)
                except FileNotFoundError:
                    return self._json(404, {"ok": False, "error": "Ez a hibacsomag már nincs meg (a régieket törlöm)."})
                return self._json(200, {"ok": True, "path": str(folder), "opened": open_folder(folder)})
            if path == "/api/work/settings":
                values = body.get("settings") if isinstance(body.get("settings"), dict) else {}
                return self._json(200, {"ok": True, "settings": work.save_settings(values)})
            if path == "/api/work/ack":
                work.acknowledge(str(body.get("platform") or ""))
                return self._json(200, {"ok": True, "acknowledged": work.settings.get("acknowledged", [])})
        except ItemError as exc:
            return self._json(400, {"ok": False, "error": str(exc)})
        except OSError as exc:
            return self._json(500, {"ok": False, "error": f"Nem sikerült: {exc}"})
        self._json(404, {"ok": False})

    def _deals_post(self, body: dict[str, Any], remote: bool = False) -> None:
        """Az Automata kereskedés gombjai: be/ki, keresés hozzáadása és törlése, egy találat elrejtése, keresés most.
        Ha valami megváltozott, a többi ablak (a gépen és a telefonon) is megtudja (work_deals)."""
        self._deals_action(body, remote)
        if str(body.get("action") or "") not in ("journal", "lookup", "offer", "run"):
            self.gui.ui.work_deals()

    def _deals_action(self, body: dict[str, Any], remote: bool = False) -> None:
        app, deals = self.gui.app, self._work().deals
        instant = self._work().instant
        action = str(body.get("action") or "")
        try:
            if action.startswith("instant_"):   # azonnali figyelő (Hardverapró, Jófogás; böngésző nélkül)
                if instant is None:
                    return self._json(409, {"ok": False, "error": "Az azonnali figyelő ki van kapcsolva ([work] "
                                                                  "fast_fetch = false)."})
                if action == "instant_enable":
                    state = instant.set_enabled(bool(body.get("on")))
                elif action == "instant_add":
                    state = instant.add(str(body.get("target") or ""), body.get("sites"), max_price=body.get("max_price"),
                                        min_price=body.get("min_price"), exclude=body.get("exclude"),
                                        every=body.get("every"))
                elif action == "instant_remove":
                    state = instant.remove(str(body.get("id") or ""))
                elif action == "instant_pause":
                    state = instant.pause(str(body.get("id") or ""), bool(body.get("off")))
                else:
                    return self._json(400, {"ok": False})
                return self._json(200, {"ok": True, **deals.state(), "instant": state})
            if action in ("start", "stop"):
                # Indítás / Leállítás: minden egyszerre (keresések, figyelt oldalak, böngészés, azonnali figyelő),
                # napszaktól függetlenül, amíg le nem állítod
                state = deals.start() if action == "start" else deals.stop()
                instant_state = instant.set_enabled(action == "start") if instant is not None else None
                if action == "start" and app is not None and app.scheduler is not None \
                        and "deals" in app.scheduler.jobs and deals.available():
                    app.run_job_now("deals")
                return self._json(200, {"ok": True, **state, "instant": instant_state})
            if action == "message":     # az eladónak szóló üzenet (a megérő találat ablakának gombja másolja ki)
                return self._json(200, {"ok": True, **deals.set_message(body.get("text"))})
            if action == "limits":      # legfeljebb ennyiért vegyen, legalább ennyi haszonnal (minden keresésre)
                return self._json(200, {"ok": True, **deals.set_limits(body.get("max_buy"), body.get("min_profit"),
                                                                       body.get("safe_only"))})
            if action == "enable":
                return self._json(200, {"ok": True, **deals.set_enabled(bool(body.get("on")))})
            if action == "bundles":     # csomagbontás: be/ki, legalább mennyi haszonnal szóljon
                return self._json(200, {"ok": True, **deals.set_bundles(body.get("on"), body.get("min_profit"))})
            if action == "bundle_add":  # csomagkeresés (pl. „ruhacsomag”)
                return self._json(200, {"ok": True, **deals.add_bundle_search(
                    str(body.get("query") or ""), body.get("sites"), body.get("max_price"), body.get("every"))})
            if action == "bundle_remove":
                return self._json(200, {"ok": True, **deals.remove_bundle_search(str(body.get("id") or ""))})
            fields = {k: body.get(k) for k in DEAL_FIELDS if k in body}
            if action == "add":
                return self._json(200, {"ok": True, **deals.add_search(**{"query": "", **fields})})
            if action == "update":
                return self._json(200, {"ok": True, **deals.update_search(str(body.get("id") or ""), **fields)})
            if action == "accept":      # a javasolt szokásos ár elfogadása, vagy (price) kézzel megadva
                return self._json(200, {"ok": True, **deals.accept(str(body.get("id") or ""), body.get("price"))})
            if action == "recalibrate":
                return self._json(200, {"ok": True, **deals.recalibrate(str(body.get("id") or ""))})
            if action == "offer":       # Vinted-ajánlat: az ágens-szálon, a küldés előtt jóváhagyást kér
                deal = deals.get(str(body.get("id") or ""))
                if app is None or not self._work().browser_ready:
                    return self._json(409, {"ok": False, "error": "Az ajánlattételhez Jarvis saját böngészője kell."})
                app.submit_work("deal_offer", deal=deal["id"], price=body.get("price"))
                return self._json(202, {"ok": True})
            if action == "watch_add":
                return self._json(200, {"ok": True, **deals.add_watch(str(body.get("target") or ""), body.get("min_profit"),
                                                                     body.get("hours"), body.get("every"))})
            if action == "browse":      # böngészés napközben: be/ki, oldalak, legkisebb haszon
                state = deals.set_browse(body.get("on"), body.get("sites"), body.get("min_profit"))
                # bekapcsoláskor rögtön kezd (nem várja meg az ütemező csendes perceit)
                if body.get("on") and app is not None and app.scheduler is not None and "deals" in app.scheduler.jobs \
                        and deals.available():
                    app.run_job_now("deals")
                return self._json(200, {"ok": True, **state})
            if action == "journal":     # a napló egy napja (alapból a mai)
                return self._json(200, {"ok": True, "entries": deals.journal_entries(str(body.get("day") or ""))})
            if action == "day":         # mikor megy általában a gép (a licitekről még az esti leállás előtt szól)
                return self._json(200, {"ok": True, **deals.set_day(body.get("start"), body.get("end"))})
            if action == "lookup":      # Mennyit ér? az árbázisból, modell és oldalbetöltés nélkül
                return self._json(200, {"ok": True, "results": deals.lookup(str(body.get("query") or ""))})
            if action == "watch_remove":
                return self._json(200, {"ok": True, **deals.remove_watch(str(body.get("id") or ""))})
            if action == "remove":
                return self._json(200, {"ok": True, **deals.remove_search(str(body.get("id") or ""))})
            if action == "dismiss":
                return self._json(200, {"ok": True, **deals.dismiss(str(body.get("id") or ""))})
            if action == "bought":      # Megvettem: eladási tárgy lesz belőle, a vételárral (Vételeim)
                deal = deals.get(str(body.get("id") or ""))
                store = self._work().store
                if deal.get("bundle"):  # egy csomag: a darabjai külön tárgyak, a vételár szétosztva
                    return self._bought_bundle(deal, body)
                median = int(deal.get("median") or 0) or None
                # a cím (a kártya szövegében a város, a dátum és az eladó is ott van), és amire az árkutatás keressen:
                # a keresésed, vagy a cím termékkulcsa („Sony WH-1000XM4”)
                name = str(deal.get("title") or deal["text"])
                query = ((deal.get("query") if not deal.get("explore") else "") or deals.book.query(name)
                         or str(deal.get("query") or ""))
                item = store.create(prompt=f"{name}, {deal['median']} Ft", source=deal["url"], search_query=query,
                                    bought_price=body.get("price") or deal["price"], bought_at=time.time(),
                                    bought_shipping=body.get("shipping"), bought_from=str(deal.get("site") or ""),
                                    in_transit=bool(body.get("in_transit", True)), market_price=median,
                                    target_price=round_price(median * SALE_RATIO) if median else None)
                state = deals.mark_bought(deal["id"], item.id)
                self._money_check(self._work())
                return self._json(200, {"ok": True, **deals.state(), "item": item.id, "profit": profit_report(store.all())})
            if action == "run":
                if app is None or app.scheduler is None or "deals" not in app.scheduler.jobs:
                    return self._json(409, {"ok": False, "error": "A kereséshez Jarvis saját böngészője kell."})
                if not deals.available():
                    return self._json(409, {"ok": False, "error": "Most nincs esedékes keresés vagy figyelt oldal (a "
                                                                  "böngészés is pihen, vagy ki van kapcsolva)."})
                app.run_job_now("deals")
                return self._json(202, {"ok": True})
        except (DealError, ItemError) as exc:
            return self._json(400, {"ok": False, "error": str(exc)})
        return self._json(400, {"ok": False})

    def _bought_bundle(self, deal: dict[str, Any], body: dict[str, Any]) -> None:
        """Megvettél egy csomagot: a darabjai (tételenként egy, az apróságok együtt) eladási tárgyak lesznek, a
        vételár és a posta az értékük arányában szétosztva (deals.bundle_parts), és mind a Vételeim közé kerül."""
        work = self._work()
        paid = parse_price(body.get("price") or deal["price"]) or int(deal["price"])
        shipping = parse_price(body.get("shipping")) or 0
        parts = bundle_parts(deal, paid, shipping)
        if not parts:
            raise DealError("Ebben a csomagban nincs külön eladható darab.")
        now = time.time()
        items = [work.store.create(prompt=part["prompt"], source=deal["url"], search_query=part["search_query"],
                                   bought_price=part["bought_price"], bought_at=now,
                                   bought_shipping=part["bought_shipping"] or None,
                                   bought_from=str(deal.get("site") or ""), in_transit=bool(body.get("in_transit", True)),
                                   target_price=part["target_price"], trade_note=part["trade_note"])
                 for part in parts]
        work.deals.mark_bought(deal["id"], items[0].id, [item.id for item in items])
        self._money_check(work)
        return self._json(200, {"ok": True, **work.deals.state(), "item": items[0].id,
                                "items": [item.id for item in items], "profit": profit_report(work.store.all())})

    @staticmethod
    def _ledger(work: Any) -> dict[str, Any]:
        return {"ok": True, **profit_report(work.store.all(), book=work.deals.book),
                "sites": [p.name for p in work.platforms.values()], "shipping": SHIPPING,
                "money": work.deals.money_status()}

    def _money_check(self, work: Any) -> None:
        """Vétel után (és a keret átírása után): ha a havi keret fogy, vagy túl sok tárgy vár eladásra, szól."""
        app = self.gui.app
        if app is not None:
            work.deals.money_check(app.notify_event)

    def _ledger_post(self, body: dict[str, Any]) -> None:
        """Vételeim: egy vétel kézzel (nem az Automata kereskedésből: pl. bolhapiacon vetted), és egy tárgy vételi
        adatainak mentése (a tárgy lapjának mentése is jó, ez a fülnek a friss összesítést is visszaadja)."""
        work = self._work()
        action = str(body.get("action") or "")
        if action == "add":
            name = " ".join(str(body.get("name") or "").split())[:100]
            if not name:
                return self._json(400, {"ok": False, "error": "Írd be, mit vettél."})
            if body.get("price") in (None, ""):
                return self._json(400, {"ok": False, "error": "Mennyiért vetted?"})
            values = {k: body.get(k) for k in ("bought_shipping", "bought_extra", "bought_from", "target_price",
                                                "trade_note") if body.get(k) not in (None, "")}
            item = work.store.create(prompt=name, bought_price=body.get("price"), bought_at=time.time(),
                                     in_transit=bool(body.get("in_transit")), **values)
            self._money_check(work)
            return self._json(200, {**self._ledger(work), "item": item.id})
        if action == "money":       # havi keret, és mikor szóljon, hogy túl sok megvett tárgy vár eladásra
            try:
                work.deals.set_money(body.get("budget"), body.get("stock_count"), body.get("stock_value"))
            except ItemError as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
            self._money_check(work)
            return self._json(200, self._ledger(work))
        if action == "update":
            fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
            values = {k: v for k, v in fields.items() if k in EDITABLE}
            if not values:
                return self._json(400, {"ok": False, "error": "Nincs mit menteni."})
            work.store.update(str(body.get("id") or ""), user=True, **values)
            return self._json(200, self._ledger(work))
        if action == "cancel":      # Visszamondva: mégsem vetted meg; mindenhonnan törlődik (a tárgy a _torolt mappába)
            item = work.store.get(str(body.get("id") or ""))
            if item.bought_price is None:
                return self._json(400, {"ok": False, "error": "Ezt nem jelölted megvettnek."})
            if item.sold:
                return self._json(400, {"ok": False, "error": "Ez már elkelt: a visszamondás csak a még meg nem érkezett "
                                                              "vagy el nem adott tárgyaknál van."})
            others = []                 # egy csomag darabjai együtt mennek (amit már töröltél, az kimarad)
            for other_id in work.deals.bought_group(item.id):
                if other_id != item.id:
                    try:
                        others.append(work.store.get(other_id))
                    except ItemError:
                        pass
            if any(other.sold for other in others):
                return self._json(400, {"ok": False, "error": "A csomag egy darabja már elkelt: a visszamondás csak a "
                                                              "még el nem adott csomagnál van."})
            deal = work.deals.cancel_bought(item.id, item.name)
            if deal is not None and work.instant is not None:
                work.instant.forget_deal(deal["id"])
            for gone in [item, *others]:
                work.store.delete(gone.id)
            self.gui.ui.work_deals()    # a találatok listája is frissül (a gépen és a telefonon)
            return self._json(200, {**self._ledger(work), "removed": [gone.id for gone in (item, *others)]})
        return self._json(400, {"ok": False, "error": "Ismeretlen művelet."})

    def _messages_post(self, path: str, body: dict[str, Any], remote: bool = False) -> None:
        """A vevői üzenetek gombjai: vázlat kérése, a vázlat mentése, megnézve, küldés (jóváhagyással), frissítés."""
        app = self.gui.app
        desk = self._work().messages
        if path == "/api/work/messages/check":
            if app.scheduler is None or "messages" not in app.scheduler.jobs:
                return self._json(409, {"ok": False, "error": "Az üzenetek olvasásához Jarvis saját böngészője kell."})
            app.run_job_now("messages")
            return self._json(202, {"ok": True})
        conv_id = str(body.get("id") or "")
        if not CONV_ID_RE.match(conv_id):
            return self._json(400, {"ok": False, "error": "Melyik beszélgetés?"})
        conv = desk.store.get(conv_id)     # MessageError (ItemError), ha nincs ilyen
        if path == "/api/work/messages/seen":
            return self._json(200, {"ok": True, "conversation": desk.public(desk.store.seen(conv_id))})
        if path == "/api/work/messages/save":
            draft = desk.store.set_draft(conv_id, str(body.get("draft") or "")[:MAX_MESSAGE])
            return self._json(200, {"ok": True, "conversation": desk.public(draft)})
        if path == "/api/work/messages/draft":
            app.submit_work("message_draft", conversation=conv_id)
            return self._json(202, {"ok": True})
        if path == "/api/work/messages/send":
            text = str(body.get("text") or "").strip()[:MAX_MESSAGE]
            if not text:
                return self._json(400, {"ok": False, "error": "Üres üzenetet nem küldök."})
            if conv["platform"] not in desk.platforms or desk.browser is None:
                return self._json(409, {"ok": False, "error": "Ezt az üzenetet most nem tudom elküldeni (nincs böngésző, "
                                                              "vagy ennek az oldalnak az üzeneteit nem kezelem)."})
            app.submit_work("message_send", conversation=conv_id, text=text)
            return self._json(202, {"ok": True})
        self._json(404, {"ok": False})


    # --- Kreatív menü ---------------------------------------------------------------------

    def _creative(self) -> Any:
        app = self.gui.app
        return app.creative if app is not None else None

    def _creative_get(self, path: str, query: dict[str, list[str]]) -> None:
        desk = self._creative()
        if desk is None:
            return self._json(503, {"ok": False, "error": "A Kreatív menü még indul."})
        story_id = query.get("id", [""])[0]
        try:
            if path == "/api/creative/stories":
                return self._json(200, {"ok": True, "stories": desk.stories(), "templates": creative_templates.public(),
                                        "language": desk.language})
            if path == "/api/creative/story":
                return self._json(200, {"ok": True, "story": desk.get(story_id)})
            if path == "/api/creative/export":
                name, text = desk.export(story_id)
                plain = name.encode("ascii", "ignore").decode() or "tortenet.md"
                return self._send(200, text.encode("utf-8"), "text/markdown; charset=utf-8", {
                    "Content-Disposition": f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(name)}"})
        except StoryError as exc:
            return self._json(404, {"ok": False, "error": str(exc)})
        self._send(404, b"", "text/plain")

    def _creative_post(self, path: str, body: dict[str, Any]) -> None:
        app = self.gui.app
        desk = self._creative()
        if desk is None:
            return self._json(503, {"ok": False, "error": "A Kreatív menü még indul."})
        story_id = str(body.get("id") or "")
        try:
            if path == "/api/creative/new":
                story, need = desk.create(body)
                if need:
                    app.submit_creative(need, story["id"])
                return self._json(200, {"ok": True, "story": desk.get(story["id"])})
            if path == "/api/creative/update":
                fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
                return self._json(200, {"ok": True, "story": desk.update(story_id, fields)})
            if path == "/api/creative/act":
                action = str(body.get("action") or "")
                if action == "stop":
                    if desk.busy(story_id):
                        app.stop_current()
                    return self._json(200, {"ok": True})
                if action == "undo":
                    return self._json(200, {"ok": True, "story": desk.undo(story_id)})
                if action == "redo":
                    return self._json(200, {"ok": True, "story": desk.redo(story_id)})
                if action not in ("send", "continue", "retry", "summarize"):
                    return self._json(400, {"ok": False, "error": "Ismeretlen művelet."})
                desk.require_idle(story_id)
                text = str(body.get("text") or "")[:MAX_TEXT]
                if action == "send" and text.strip():
                    desk.add_turn(story_id, str(body.get("kind") or ""), text)
                if action == "retry":
                    desk.check_retry(story_id)
                task = {"send": "write", "continue": "write"}.get(action, action)
                app.submit_creative(task, story_id)
                return self._json(202, {"ok": True, "story": desk.get(story_id)})
            if path == "/api/creative/entry":
                story = desk.entry(story_id, str(body.get("entry") or ""), str(body.get("op") or ""),
                                   str(body.get("text") or ""), int(body.get("index") or 0))
                return self._json(200, {"ok": True, "story": story})
            if path == "/api/creative/card":
                card = body.get("card") if isinstance(body.get("card"), dict) else {}
                return self._json(200, {"ok": True, "story": desk.card(story_id, str(body.get("op") or ""), card)})
            if path == "/api/creative/memory":
                story = desk.memory(story_id, str(body.get("op") or ""), str(body.get("memory") or ""),
                                    str(body.get("text") or ""))
                return self._json(200, {"ok": True, "story": story})
            if path == "/api/creative/delete":
                desk.delete(story_id)
                return self._json(200, {"ok": True})
        except StoryError as exc:
            return self._json(409 if desk.busy(story_id) else 400, {"ok": False, "error": str(exc)})
        except (TypeError, ValueError):
            return self._json(400, {"ok": False, "error": "Hibás kérés."})
        except OSError as exc:
            return self._json(500, {"ok": False, "error": f"Nem sikerült menteni: {exc}"})
        self._json(404, {"ok": False})


def open_app_window(url: str, cfg: GuiCfg) -> bool:
    """Alkalmazásablakként nyitja meg (címsor és fülek nélkül); ha nem megy, a böngészőben."""
    if cfg.window and cfg.browser:
        args = [f"--app={url}", "--window-size=1000,920"]
        try:
            if IS_WINDOWS:
                subprocess.Popen(["cmd", "/c", "start", "", cfg.browser, *args],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
                return True
            exe = shutil.which(cfg.browser) or shutil.which("chromium") or shutil.which("google-chrome")
            if exe:
                subprocess.Popen([exe, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
        except OSError:
            pass
    return webbrowser.open(url)


def run_gui(app: JarvisApp, ui: GuiUI, bus: EventBus, cfg: GuiCfg, open_window: bool = True,
            standby: bool = False, info_path: Path | None = None) -> None:
    """Elindítja a felületet, betölti a Jarvist, és addig fut, amíg az ablak nyitva van.

    `standby`: készenléti indulás (a gép bekapcsolásakor): a modellek nem töltődnek be, ablak nem
    nyílik, és az ablak bezárása sem állítja le (a telefonról és a következő megnyitásra vár).
    `info_path`: ide írja az ablak címét, hogy a második indítás megnyithassa (instance.py)."""
    server = GuiServer(app, ui, bus, port=cfg.port)
    server.start()
    if info_path is not None:
        instance.write_info(info_path, server.url, standby)
    bus.set_hello({"name": app.name, "model": app.cfg.llm.model})
    console.info(f"A felület címe (ha nem nyílna meg magától): {server.url}")
    phone = None
    if app.cfg.api.enabled:
        try:
            from .api import start_api

            phone = start_api(app, ui, bus, app_data(app.cfg))
            server.phone = phone
            phone.gui = server    # a távoli ablak (telefon, laptop) ennek a felületnek adja tovább a kéréseket
            console.info(f"Telefonos kapcsolat: http://{phone.host}:{phone.port}/api/v1 "
                         "(párosítás: Telefon gomb a Jarvis ablakában)")
        except OSError as exc:
            console.warn(f"A telefonos kapcsolat nem indult el ({exc}). Másik port: [api] port a config.toml-ban.")
    try:
        if standby:
            if app.start(standby=True):
                app.start_agent()
                bus.set_hello(app.describe())
            else:
                ui.error("A Jarvis nem tudott elindulni. A hibát javítás után indítsd újra.")
            if open_window:
                open_app_window(server.url, cfg)
        else:
            # Előbb a nyelvi modell, csak utána az ablak: a böngészőablak is foglal videómemóriát,
            # és az Ollama a betöltés pillanatában szabad memóriához igazítja, mi kerül a kártyákra.
            loaded = app.load_model()
            if open_window:
                open_app_window(server.url, cfg)
            if loaded and app.start():
                app.start_agent()
                bus.set_hello(app.describe())
                app.greet()
            else:
                ui.error("A Jarvis nem tudott elindulni. A hibát javítás után indítsd újra.")
        while not app.exit_requested.is_set():
            time.sleep(0.25)
            # Az oldal frissítése is bontja a kapcsolatot egy pillanatra, ezért kis türelmi idő kell.
            # Készenléti indulásnál az ablak bezárása nem állítja le (a telefon és a következő
            # megnyitás is elérje); a modelleket a tétlenség után magától leállítja.
            if (cfg.exit_on_close and not standby and bus.ever_connected and bus.client_count == 0
                    and time.monotonic() - bus.last_disconnect > 4):
                console.info("Az ablak bezárult, a Jarvis leáll.")
                break
    finally:
        if phone is not None:
            phone.stop()
        server.stop()
        if info_path is not None:
            instance.clear_info(info_path, server.url)
