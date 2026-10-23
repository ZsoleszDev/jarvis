"""Értesítés a laptopra (és a telefon böngészőjébe) akkor is, ha a Jarvis ablaka be van zárva: Web Push.

A böngésző (Edge, Chrome) a gép kulcsával feliratkozik, és a feliratkozást a párosított eszköz kulcsával elküldi
(/api/v1/push/subscribe). Ha a Jarvis jóváhagyást kér, vagy értesítés jön (kész vagy elakadt feladat, kérdés, új
találat), a gép egy titkosított üzenetet küld a böngésző push-szolgáltatásának (Edge-nél a Microsoft, Chrome-nál a
Google szervere), az pedig továbbítja a böngészőnek. A tartalmat csak a böngésző tudja kiolvasni (RFC 8291), a
küldőt a gép kulcsa igazolja (VAPID, RFC 8292). Ehhez a gépen a cryptography csomag kell.

A jóváhagyás gombjai (Engedélyezem / Mégse) az értesítésben egyszer használható válaszkulccsal mennek vissza
(/api/v1/push/answer): a böngésző háttérszála (service worker) nem ismeri az eszköz kulcsát.

Egy értesítés gombjai (notices.py, pl. „Eladva itt, vedd le a többiről”) is az üzenetbe kerülnek; ezek csak
megnyitják a Jarvis ablakát a megfelelő lapon (pl. a hirdetés Eladva ablakát), a döntés ott a tiéd.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import queue
import secrets
import struct
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

SUBJECT = "mailto:jarvis@example.com"      # a VAPID kötelező kapcsolat-mezője (a push-szolgáltatók formailag nézik)
RECORD_SIZE = 4096
MAX_PER_DEVICE = 4
ANSWER_SECONDS = 300                       # ennyi ideig vár a Jarvis a jóváhagyásra (gui.GuiUI.confirm)
QUEUE_LIMIT = 100
# A böngészők push-szolgáltatásai: csak ide küldünk (egy párosított eszköz se irányíthassa máshová a gépet).
PUSH_HOSTS = ("notify.windows.com", "fcm.googleapis.com", "android.googleapis.com", "push.services.mozilla.com",
              "push.apple.com")
INSTALL_HINT = "A laptopos értesítésekhez a gépen ez kell: pip install cryptography (utána indítsd újra a Jarvist)."
# Az értesítések gombjai, amiket a push-üzenet is visz (a böngésző legfeljebb kettőt mutat).
NOTICE_ACTIONS = ("sold", "login", "deal", "item", "review")
NOTICE_ACTION_FIELDS = ("action", "where", "platform", "delist", "label", "deal")


class PushError(Exception):
    """Hibás feliratkozás (érthető üzenettel)."""


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64u(text: Any) -> bytes:
    text = str(text or "")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise PushError("Hibás kulcs a feliratkozásban.") from None


def available() -> bool:
    try:
        import cryptography.hazmat.primitives.asymmetric.ec  # noqa: F401
    except ImportError:
        return False
    return True


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def _public_bytes(key: Any) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def encrypt(payload: bytes, p256dh: bytes, auth: bytes, server_key: Any = None, salt: bytes | None = None) -> bytes:
    """Titkosítás a böngészőnek (RFC 8291, aes128gcm): csak az olvashatja, akié a p256dh kulcs és az auth titok.
    A server_key (egyszer használt kulcspár) és a salt csak a teszteknek adható meg."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    browser = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)
    server_key = server_key or ec.generate_private_key(ec.SECP256R1())
    server_public = _public_bytes(server_key)
    shared = server_key.exchange(ec.ECDH(), browser)
    ikm = _hkdf(auth, shared, b"WebPush: info\x00" + p256dh + server_public, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    record = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)   # \x02: az utolsó (egyetlen) rekord vége
    return salt + struct.pack("!IB", RECORD_SIZE, len(server_public)) + server_public + record


def vapid_header(endpoint: str, key: Any, now: float | None = None) -> str:
    """Az Authorization fejléc (RFC 8292): a push-szolgáltatónak szóló, a gép kulcsával aláírt JWT."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    url = urlparse(endpoint)
    claims = {"aud": f"{url.scheme}://{url.netloc}", "exp": int((now or time.time()) + 12 * 3600), "sub": SUBJECT}
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64u(json.dumps(claims, separators=(",", ":")).encode())
    r, s = decode_dss_signature(key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
    signature = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={head}.{body}.{signature}, k={b64u(_public_bytes(key))}"


def notice_actions(notice: dict[str, Any]) -> list[dict[str, Any]]:
    """Az értesítés gombjai a push-üzenetbe: csak az ismert műveletek, rövid mezőkkel."""
    out = []
    for action in notice.get("actions") or []:
        if not isinstance(action, dict) or action.get("action") not in NOTICE_ACTIONS:
            continue
        out.append({k: (bool(action[k]) if k == "delist" else str(action[k])[:60])
                    for k in NOTICE_ACTION_FIELDS if k in action})
    return out[:2]


def push_host_ok(endpoint: str) -> bool:
    url = urlparse(endpoint)
    host = (url.hostname or "").lower()
    return url.scheme == "https" and any(host == h or host.endswith("." + h) for h in PUSH_HOSTS)


def _post(url: str, body: bytes, headers: dict[str, str], timeout: float = 15.0) -> int:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


class WebPush:
    """A feliratkozások (eszközönként) és a küldés. A küldés egy háttérszálon megy, hogy a Jarvis ne várjon."""

    def __init__(self, path: Path, devices: Callable[[], set[str]] | None = None,
                 sender: Callable[[str, bytes, dict[str, str]], int] = _post,
                 endpoint_ok: Callable[[str], bool] = push_host_ok, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.devices = devices
        self.sender = sender
        self.endpoint_ok = endpoint_ok
        self.clock = clock
        self._lock = threading.Lock()
        self._key: Any = None
        self._data = self._load()
        self._answers: dict[str, tuple[int, float]] = {}
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_LIMIT)
        self._worker: threading.Thread | None = None

    # --- tárolás -------------------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        subs = data.get("subscriptions") if isinstance(data, dict) else None
        return {"vapid": str(data.get("vapid") or "") if isinstance(data, dict) else "",
                "subscriptions": [s for s in subs or [] if isinstance(s, dict) and s.get("endpoint")]}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def _vapid(self) -> Any:
        """A gép VAPID-kulcsa: az első feliratkozáskor készül, és megmarad (enélkül a böngészők újra kérnék)."""
        from cryptography.hazmat.primitives.asymmetric import ec

        if self._key is None:
            raw = self._data["vapid"]
            if raw:
                self._key = ec.derive_private_key(int.from_bytes(unb64u(raw), "big"), ec.SECP256R1())
            else:
                self._key = ec.generate_private_key(ec.SECP256R1())
                self._data["vapid"] = b64u(self._key.private_numbers().private_value.to_bytes(32, "big"))
                self._save()
        return self._key

    def public_key(self) -> str:
        """A böngészőnek (applicationServerKey); "", ha nincs cryptography csomag."""
        if not available():
            return ""
        with self._lock:
            return b64u(_public_bytes(self._vapid()))

    def subscribe(self, device: str, subscription: Any) -> int:
        """Egy böngésző feliratkozása (a PushSubscription.toJSON() alakja). Visszaadja az eszköz feliratkozásainak
        számát (egy eszközről, pl. több böngészőből, legfeljebb MAX_PER_DEVICE)."""
        if not isinstance(subscription, dict):
            raise PushError("Hiányzik a feliratkozás (subscription).")
        endpoint = str(subscription.get("endpoint") or "")
        keys = subscription.get("keys") if isinstance(subscription.get("keys"), dict) else {}
        if len(endpoint) > 2048 or not self.endpoint_ok(endpoint):
            raise PushError("Ezt a push-címet nem ismerem (Edge, Chrome, Firefox és Safari push-szolgáltatása jó).")
        p256dh, auth = unb64u(keys.get("p256dh")), unb64u(keys.get("auth"))
        if len(p256dh) != 65 or p256dh[0] != 4 or len(auth) != 16:
            raise PushError("Hibás kulcs a feliratkozásban.")
        with self._lock:
            subs = [s for s in self._data["subscriptions"] if s["endpoint"] != endpoint]
            mine = [s for s in subs if s.get("device") == device]
            if len(mine) >= MAX_PER_DEVICE:
                subs.remove(mine[0])                          # a legrégebbi megy
            subs.append({"device": device, "endpoint": endpoint, "p256dh": b64u(p256dh), "auth": b64u(auth),
                         "created": self.clock()})
            self._data["subscriptions"] = subs
            self._save()
            return sum(1 for s in subs if s.get("device") == device)

    def unsubscribe(self, device: str, endpoint: str = "") -> int:
        """Leiratkozás (endpoint nélkül az eszköz minden feliratkozása); hány feliratkozás szűnt meg."""
        with self._lock:
            before = self._data["subscriptions"]
            kept = [s for s in before if not (s.get("device") == device and (not endpoint or s["endpoint"] == endpoint))]
            if len(kept) != len(before):
                self._data["subscriptions"] = kept
                self._save()
            return len(before) - len(kept)

    def subscriptions(self, device: str | None = None) -> list[dict[str, Any]]:
        """A feliratkozások; a visszavont eszközökéi közben törlődnek."""
        valid = self.devices() if self.devices is not None else None
        with self._lock:
            subs = self._data["subscriptions"]
            if valid is not None and any(s.get("device") not in valid for s in subs):
                subs = [s for s in subs if s.get("device") in valid]
                self._data["subscriptions"] = subs
                self._save()
            return [dict(s) for s in subs if device is None or s.get("device") == device]

    # --- események -----------------------------------------------------------------------------

    def on_event(self, event: dict[str, Any]) -> None:
        """A Jarvis eseményei (a gép felületének eseménybusza hívja, bármelyik szálról): ami fontos, az push-ként
        megy ki. Gyors marad: a küldés a háttérszálon történik."""
        kind = event.get("type")
        if kind == "confirm_done":
            with self._lock:
                self._answers = {k: v for k, v in self._answers.items() if v[0] != event.get("id")}
            return
        if kind not in ("confirm", "notification") or not self._data["subscriptions"] or not available():
            return
        if kind == "confirm":
            try:
                confirm_id = int(event.get("id", 0))
            except (TypeError, ValueError):
                return
            key = secrets.token_urlsafe(18)
            with self._lock:
                now = self.clock()
                self._answers = {k: v for k, v in self._answers.items() if v[1] > now}
                self._answers[key] = (confirm_id, now + ANSWER_SECONDS)
            message = {"kind": "confirm", "id": confirm_id, "title": "Jóváhagyás kell",
                       "text": str(event.get("prompt") or "")[:300], "answer": key, "tag": f"confirm-{confirm_id}"}
            self._enqueue(message, ttl=ANSWER_SECONDS, urgency="high")
        else:
            notice = event.get("notice") if isinstance(event.get("notice"), dict) else {}
            if notice.get("quiet"):
                return                             # nem fontos: csak az Értesítések listájában
            message = {"kind": "notice", "title": str(notice.get("title") or "Jarvis")[:80],
                       "text": str(notice.get("text") or "")[:300], "tag": f"notice-{notice.get('id') or ''}"}
            actions = notice_actions(notice)
            if notice.get("deal"):                 # egy megérő találat: a kattintás a találat ablakát nyitja meg
                message["deal"] = str(notice["deal"])[:16]
                message["urgent"] = True
            if actions:
                message["actions"] = actions
                if notice.get("item"):
                    message["item"] = str(notice["item"])[:16]
            if notice.get("open") in ("item", "review") and notice.get("item"):
                # egy tárgy kérdése vagy a Pro feltöltés Nézd át ablaka: a kattintás azt nyitja meg
                message.update(open=str(notice["open"]), item=str(notice["item"])[:16])
            # egy friss hirdetésnél számít a perc: a telefon akkor is rögtön kapja meg, ha alszik
            self._enqueue(message, ttl=12 * 3600, urgency="high" if notice.get("urgent") else "normal")

    def answer(self, key: str) -> int | None:
        """Az értesítés gombjának válaszkulcsa: melyik jóváhagyásra felel (egyszer használható)."""
        with self._lock:
            found = self._answers.pop(str(key or ""), None)
        if found is None or found[1] < self.clock():
            return None
        return found[0]

    def _enqueue(self, message: dict[str, Any], ttl: int, urgency: str) -> None:
        try:
            self._queue.put_nowait((message, ttl, urgency))
        except queue.Full:
            return
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, name="jarvis-push", daemon=True)
                self._worker.start()

    def _run(self) -> None:
        while True:
            message, ttl, urgency = self._queue.get()
            try:
                self.send(message, ttl, urgency)
            except Exception:  # noqa: BLE001 - a push hibája ne állítsa le a szálat
                pass

    def send(self, message: dict[str, Any], ttl: int = 3600, urgency: str = "normal") -> int:
        """Elküldi az üzenetet minden feliratkozásnak; hányhoz ment ki. A lejárt feliratkozást (404, 410) törli."""
        payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
        with self._lock:
            key = self._vapid()
        sent = 0
        for sub in self.subscriptions():
            endpoint = sub["endpoint"]
            if not self.endpoint_ok(endpoint):
                continue
            body = encrypt(payload, unb64u(sub["p256dh"]), unb64u(sub["auth"]))
            headers = {"Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
                       "TTL": str(int(ttl)), "Urgency": urgency, "Authorization": vapid_header(endpoint, key)}
            try:
                status = self.sender(endpoint, body, headers)
            except OSError:
                continue
            if status in (404, 410):
                self.unsubscribe(sub["device"], endpoint)
            elif 200 <= status < 300:
                sent += 1
        return sent
