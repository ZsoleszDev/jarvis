"""Értesítés a laptopra bezárt ablaknál is (jarvis_core/webpush.py, Web Push).

A titkosítást a teszt a böngésző oldaláról fejti vissza (RFC 8291, a böngésző kulcsaival), a VAPID-aláírást a gép
nyilvános kulcsával ellenőrzi; fejlesztéskor egy független megvalósítás (http_ece, a pywebpush alatt) is
visszafejtette a gép üzeneteit. A push-szolgáltatót egy hamis küldő helyettesíti.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis_core import webpush  # noqa: E402

HAS_CRYPTO = webpush.available()
EDGE = "https://wns2-by3p.notify.windows.com/w/?token=AwYAAAB"


class Browser:
    """A böngésző oldala: kulcsok a feliratkozáshoz, és a gép üzenetének visszafejtése (RFC 8291)."""

    def __init__(self, endpoint: str = EDGE):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        self.key = ec.generate_private_key(ec.SECP256R1())
        self.public = self.key.public_key().public_bytes(serialization.Encoding.X962,
                                                        serialization.PublicFormat.UncompressedPoint)
        self.auth = b"0123456789abcdef"
        self.endpoint = endpoint

    def subscription(self) -> dict:
        return {"endpoint": self.endpoint, "expirationTime": None,
                "keys": {"p256dh": webpush.b64u(self.public), "auth": webpush.b64u(self.auth)}}

    def decrypt(self, body: bytes) -> bytes:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        def hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
            prk = hmac.new(salt, ikm, hashlib.sha256).digest()
            return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]

        salt, (record_size, id_len) = body[:16], struct.unpack("!IB", body[16:21])
        server_public = body[21:21 + id_len]
        record = body[21 + id_len:]
        assert record_size >= len(record), "egy rekordba fér"
        shared = self.key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(),
                                                                                           server_public))
        ikm = hkdf(self.auth, shared, b"WebPush: info\x00" + self.public + server_public, 32)
        cek = hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
        nonce = hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
        plain = AESGCM(cek).decrypt(nonce, record, None)
        assert plain.endswith(b"\x02"), "az utolsó rekord jelzése"
        return plain[:-1]


def check_vapid(test: unittest.TestCase, header: str, endpoint: str) -> bytes:
    """Az Authorization fejléc ellenőrzése; a gép nyilvános kulcsát adja vissza."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    test.assertTrue(header.startswith("vapid t="), header)
    token, key = header[len("vapid t="):].split(", k=")
    head, body, signature = token.split(".")
    test.assertEqual(json.loads(webpush.unb64u(head)), {"typ": "JWT", "alg": "ES256"})
    claims = json.loads(webpush.unb64u(body))
    test.assertEqual(claims["aud"], "https://" + endpoint.split("/")[2])
    test.assertTrue(time.time() < claims["exp"] <= time.time() + 24 * 3600, "legfeljebb 24 órára")
    test.assertTrue(claims["sub"].startswith("mailto:"))
    raw = webpush.unb64u(signature)
    public = webpush.unb64u(key)
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public).verify(
        encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
        f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
    return public


class FakePushService:
    """A push-szolgáltató helyett: megjegyzi a kéréseket, és a megadott kóddal felel."""

    def __init__(self):
        self.calls: list[tuple[str, bytes, dict]] = []
        self.status = 201
        self.got = threading.Event()

    def __call__(self, url: str, body: bytes, headers: dict) -> int:
        self.calls.append((url, body, headers))
        self.got.set()
        return self.status

    def wait(self, count: int = 1, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while len(self.calls) < count and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(self.calls) >= count, f"{len(self.calls)} push ment ki, {count} kellett volna"


@unittest.skipUnless(HAS_CRYPTO, "a cryptography csomag nincs telepítve")
class WebPushTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.devices = {"aaaa1111", "bbbb2222"}
        self.service = FakePushService()
        self.push = webpush.WebPush(Path(self.tmp.name) / "push.json", devices=lambda: set(self.devices),
                                    sender=self.service)

    def tearDown(self):
        self.tmp.cleanup()

    def test_encryption_only_the_browser_can_read(self):
        browser = Browser()
        message = "Jóváhagyás kell: közzététel a Vintedre, 4000 Ft?".encode()
        body = webpush.encrypt(message, browser.public, browser.auth)
        self.assertEqual(browser.decrypt(body), message)
        self.assertNotIn(message, body)
        self.assertNotEqual(body, webpush.encrypt(message, browser.public, browser.auth), "minden üzenet más")
        with self.assertRaises(Exception):
            Browser().decrypt(body)                       # egy másik böngésző nem tudja kiolvasni

    def test_vapid_key_is_kept(self):
        key = self.push.public_key()
        self.assertEqual(len(webpush.unb64u(key)), 65)
        again = webpush.WebPush(Path(self.tmp.name) / "push.json")
        self.assertEqual(again.public_key(), key, "újraindítás után ugyanaz (a böngészők ehhez iratkoztak fel)")

    def test_subscribe_checks_the_subscription(self):
        browser = Browser()
        self.assertEqual(self.push.subscribe("aaaa1111", browser.subscription()), 1)
        self.assertEqual(self.push.subscribe("aaaa1111", browser.subscription()), 1, "ugyanaz csak egyszer")
        for endpoint in ("https://evil.example.com/push", "http://fcm.googleapis.com/fcm/send/x",
                         "https://127.0.0.1:8765/api/quit", "https://notify.windows.com.evil.example/w/"):
            with self.subTest(endpoint=endpoint), self.assertRaises(webpush.PushError):
                self.push.subscribe("aaaa1111", Browser(endpoint).subscription())
        broken = browser.subscription()
        broken["keys"]["p256dh"] = "kurta"
        with self.assertRaises(webpush.PushError):
            self.push.subscribe("aaaa1111", broken)
        with self.assertRaises(webpush.PushError):
            self.push.subscribe("aaaa1111", None)
        for i in range(webpush.MAX_PER_DEVICE + 2):
            count = self.push.subscribe("bbbb2222", Browser(f"https://fcm.googleapis.com/fcm/send/{i}").subscription())
        self.assertEqual(count, webpush.MAX_PER_DEVICE, "egy eszközről legfeljebb ennyi")
        self.assertEqual(self.push.unsubscribe("aaaa1111", EDGE), 1)
        self.assertEqual(len(self.push.subscriptions("aaaa1111")), 0)

    def test_notice_goes_to_every_browser(self):
        laptop, phone = Browser(), Browser("https://fcm.googleapis.com/fcm/send/abc")
        self.push.subscribe("aaaa1111", laptop.subscription())
        self.push.subscribe("bbbb2222", phone.subscription())
        self.push.on_event({"type": "state", "state": "thinking"})          # ez nem megy ki
        self.push.on_event({"type": "notification", "notice": {"id": "n1", "kind": "post", "title": "Feltöltve",
                                                              "text": "Hosszú farmer → Vinted: fent van."}})
        self.service.wait(2)
        time.sleep(0.1)
        self.assertEqual(len(self.service.calls), 2)
        for url, body, headers in self.service.calls:
            browser = laptop if url == laptop.endpoint else phone
            message = json.loads(browser.decrypt(body))
            self.assertEqual(message, {"kind": "notice", "title": "Feltöltve", "text": "Hosszú farmer → Vinted: fent van.",
                                       "tag": "notice-n1"})
            self.assertEqual(headers["Content-Encoding"], "aes128gcm")
            self.assertEqual(headers["TTL"], str(12 * 3600))
            self.assertEqual(check_vapid(self, headers["Authorization"], url), webpush.unb64u(self.push.public_key()))

    def test_notice_buttons_go_with_the_message(self):
        browser = Browser()
        self.push.subscribe("aaaa1111", browser.subscription())
        sold = {"action": "sold", "where": "vinted", "delist": True, "label": "Eladva itt, vedd le a többiről"}
        self.push.on_event({"type": "notification", "notice": {
            "id": "n2", "kind": "sold", "title": "Elkelt? Farmer", "text": "A Vinteden eladottnak látszik.",
            "item": "a1b2c3d4", "platform": "vinted", "actions": [sold, {"action": "delete", "label": "Törlés"}]}})
        self.service.wait()
        message = json.loads(browser.decrypt(self.service.calls[0][1]))
        self.assertEqual((message["item"], message["actions"]), ("a1b2c3d4", [sold]),
                         "csak az ismert gombok (a böngésző az ablakot nyitja meg, a döntés ott a tiéd)")

    def test_confirm_answer_key_is_single_use(self):
        browser = Browser()
        self.push.subscribe("aaaa1111", browser.subscription())
        self.push.on_event({"type": "confirm", "id": 7, "prompt": "Közzéteszem a hirdetést?"})
        self.service.wait()
        url, body, headers = self.service.calls[0]
        message = json.loads(browser.decrypt(body))
        self.assertEqual((message["kind"], message["id"], message["tag"]), ("confirm", 7, "confirm-7"))
        self.assertEqual(headers["Urgency"], "high")
        self.assertEqual(self.push.answer("rossz-kulcs"), None)
        self.assertEqual(self.push.answer(message["answer"]), 7)
        self.assertEqual(self.push.answer(message["answer"]), None, "egyszer használható")
        # Ha a gépen (vagy máshol) már válaszoltál, a kulcs érvényét veszti.
        self.push.on_event({"type": "confirm", "id": 8, "prompt": "Leveszem?"})
        self.service.wait(2)
        second = json.loads(browser.decrypt(self.service.calls[1][1]))
        self.push.on_event({"type": "confirm_done", "id": 8, "approved": False})
        self.assertIsNone(self.push.answer(second["answer"]))

    def test_expired_and_revoked_subscriptions_go(self):
        gone, kept = Browser(), Browser("https://fcm.googleapis.com/fcm/send/kept")
        self.push.subscribe("aaaa1111", gone.subscription())
        self.push.subscribe("bbbb2222", kept.subscription())
        self.service.status = 410                             # a böngésző leiratkozott (vagy törölték)
        self.assertEqual(self.push.send({"kind": "notice", "title": "x", "text": "y"}), 0)
        self.assertEqual(self.push.subscriptions(), [])
        self.service.status = 201
        self.push.subscribe("aaaa1111", gone.subscription())
        self.push.subscribe("bbbb2222", kept.subscription())
        self.devices.discard("aaaa1111")                      # a gépen visszavontad az eszközt
        self.assertEqual([s["device"] for s in self.push.subscriptions()], ["bbbb2222"])
        self.assertEqual(self.push.send({"kind": "notice", "title": "x", "text": "y"}), 1)
        saved = json.loads((Path(self.tmp.name) / "push.json").read_text(encoding="utf-8"))
        self.assertEqual([s["device"] for s in saved["subscriptions"]], ["bbbb2222"])


@unittest.skipUnless(HAS_CRYPTO, "a cryptography csomag nincs telepítve")
class WebPushApiTests(unittest.TestCase):
    """A telefonos kapcsolaton át: feliratkozás az eszköz kulcsával, értesítés és jóváhagyás a gombbal."""

    def setUp(self):
        from test_api import Harness

        self.h = Harness()
        self.service = FakePushService()
        self.h.api.push.sender = self.service

    def tearDown(self):
        self.h.close()

    def test_subscribe_notify_and_answer(self):
        h = self.h
        status, info, _ = h.request("GET", "/api/v1/push")
        self.assertEqual(status, 200)
        self.assertTrue(info["available"])
        self.assertEqual(info["subscriptions"], 0)
        self.assertEqual(h.request("GET", "/api/v1/push", token=None)[0], 401, "csak párosított eszköz")
        browser = Browser()
        status, result, _ = h.request("POST", "/api/v1/push/subscribe", {"subscription": browser.subscription()})
        self.assertEqual((status, result["subscriptions"]), (200, 1))
        status, result, _ = h.request("POST", "/api/v1/push/subscribe",
                                      {"subscription": Browser("https://evil.example.com/x").subscription()})
        self.assertEqual(status, 400)

        h.app.notices.add("post", "Feltöltve", "Hosszú farmer → Vinted: fent van.")
        self.service.wait()
        message = json.loads(browser.decrypt(self.service.calls[0][1]))
        self.assertEqual((message["kind"], message["title"]), ("notice", "Feltöltve"))

        answers: list[bool] = []
        worker = threading.Thread(target=lambda: answers.append(h.ui.confirm("Közzéteszem?", threading.Event())))
        worker.start()
        self.service.wait(2)
        message = json.loads(browser.decrypt(self.service.calls[1][1]))
        self.assertEqual(message["kind"], "confirm")
        self.assertEqual(h.request("POST", "/api/v1/push/answer", {"key": "rossz", "approved": True}, token=None)[0], 404)
        status, result, _ = h.request("POST", "/api/v1/push/answer", {"key": message["answer"], "approved": True},
                                      token=None)
        self.assertEqual((status, result["approved"]), (200, True))
        worker.join(5)
        self.assertEqual(answers, [True], "a jóváhagyás az értesítés gombjával")
        self.assertEqual(h.request("POST", "/api/v1/push/answer", {"key": message["answer"], "approved": True},
                                   token=None)[0], 404, "egyszer használható")

        status, result, _ = h.request("POST", "/api/v1/push/unsubscribe", {"endpoint": browser.endpoint})
        self.assertEqual((status, result["removed"]), (200, 1))


def has_chromium() -> bool:
    try:
        from test_web_browser import HAS_CHROMIUM
    except ImportError:
        return False
    return HAS_CHROMIUM


@unittest.skipUnless(HAS_CRYPTO and has_chromium(), "cryptography vagy Playwright/Chromium nincs telepítve")
class WebPushBrowserTests(unittest.TestCase):
    """A laptop ablaka (Chromium): engedélyezett értesítésnél magától feliratkozik a gép kulcsával. A böngésző
    push-szolgáltatását (ami a teszt alatt nem érhető el) egy kis csere helyettesíti, és az engedélyt is (a
    láthatatlan Chromium mindig „denied”-et mond)."""

    def test_window_subscribes_with_the_device_key(self):
        from playwright.sync_api import sync_playwright
        from test_api import Harness

        h = Harness()
        browser_keys = Browser("https://fcm.googleapis.com/fcm/send/teszt")
        fake = json.dumps(browser_keys.subscription())
        stub = """
          (() => {
            Object.defineProperty(Notification, "permission", { get: () => "granted" });
            let current = null;
            PushManager.prototype.getSubscription = async function () { return current; };
            PushManager.prototype.subscribe = async function (options) {
              const json = %s;
              current = { endpoint: json.endpoint, options, toJSON: () => json, unsubscribe: async () => true };
              window.__pushKey = Array.from(new Uint8Array(options.applicationServerKey));
              return current;
            };
          })();""" % fake
        try:
            with sync_playwright() as pw:
                chromium = pw.chromium.launch(headless=True)
                origin = f"http://127.0.0.1:{h.api.port}"
                context = chromium.new_context()
                context.add_init_script(stub)
                page = context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.goto(f"{origin}/?t={h.token}")
                device = h.api.auth.check(h.token)
                deadline = time.monotonic() + 15
                while not h.api.push.subscriptions(device) and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertEqual([s["endpoint"] for s in h.api.push.subscriptions(device)], [browser_keys.endpoint])
                self.assertEqual(bytes(page.evaluate("window.__pushKey")),
                                 webpush.unb64u(h.api.push.public_key()), "a gép kulcsával iratkozott fel")
                self.assertTrue(page.locator("#notify").is_hidden(), "már engedélyezve: nincs mit kérni")
                self.assertEqual(errors, [])
                chromium.close()
        finally:
            h.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
