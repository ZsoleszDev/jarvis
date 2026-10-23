"""A telefonos kapcsolat (api.py) tesztjei: párosítás, kulcs, CORS, beszélgetés, események,
jóváhagyás, Szünet, tárgyak és fotók, eladva/levétel, háttérmunka, figyelők, értesítések.
Valódi helyi szerver, hamis modell; internet és GPU nem kell.

Futtatás:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_background import FakeDDGS, install_fake_ddgs  # noqa: E402
from test_offline import FakeOllama, make_cfg, reply  # noqa: E402
from test_work import SHIRT, extracted, jpeg  # noqa: E402

from test_web_browser import HAS_CHROMIUM  # noqa: E402

from jarvis_core import remote  # noqa: E402
from jarvis_core.api import start_api  # noqa: E402
from jarvis_core.app import JarvisApp  # noqa: E402
from jarvis_core.gui import EventBus, GuiServer, GuiUI  # noqa: E402
from jarvis_core.selling import POSTED  # noqa: E402


class Harness:
    def __init__(self):
        self.ddgs = install_fake_ddgs()
        self.ddgs.start()
        self.fake = FakeOllama()
        self.fake.vram_fit = lambda ctx: 1.0
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        self.cfg.api.port = 0
        self.cfg.assistant.show_timings = False
        self.cfg.assistant.fillers = False
        self.bus = EventBus()
        self.ui = GuiUI(self.bus, "Jarvis")
        self.app = JarvisApp(self.cfg, self.ui, use_voice=False, use_speech=False, use_wake_word=False)
        assert self.app.start()
        self.api = start_api(self.app, self.ui, self.bus, self.cfg.data_root)
        self.gui = GuiServer(self.app, self.ui, self.bus)
        self.gui.phone = self.api
        self.api.gui = self.gui          # a távoli ablak (telefon, laptop) ennek adja tovább a kéréseket
        self.gui.start()
        self.bus.set_hello(self.app.describe())
        self.app.start_agent()
        self.token = self.pair("Teszt telefon")

    def close(self) -> None:
        self.app.shutdown()
        self.api.stop()
        self.gui.stop()
        self.fake.close()
        self.tmp.cleanup()
        self.ddgs.stop()

    def request(self, method: str, path: str, body=None, token: str | None = "default", raw: bytes | None = None,
                headers: dict | None = None, content_type: str = "application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.api.port, timeout=30)
        send = dict(headers or {})
        if token == "default":
            token = self.token
        if token:
            send["Authorization"] = f"Bearer {token}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            send["Content-Type"] = content_type
        conn.request(method, path, body=data, headers=send)
        response = conn.getresponse()
        payload = response.read()
        result_headers = dict(response.getheaders())
        conn.close()
        if "json" in result_headers.get("Content-Type", ""):
            payload = json.loads(payload)
        return response.status, payload, result_headers

    def pair(self, name: str) -> str:
        code = self.api.auth.new_code()["code"]
        status, data, _ = self.request("POST", "/api/v1/pair", {"code": code, "device": name}, token=None)
        assert status == 200, data
        return data["token"]

    def wait(self, condition, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                raise AssertionError("nem teljesült időben")
            time.sleep(0.03)

    def idle(self) -> None:
        self.wait(lambda: not self.app.busy)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_pairing_and_keys(self):
        h = self.h
        self.assertEqual(h.request("GET", "/api/v1/ping", token=None)[1]["api"], 1)
        self.assertEqual(h.request("GET", "/api/v1/status", token=None)[0], 401)
        self.assertEqual(h.request("GET", "/api/v1/status", token="hamis")[0], 401)
        code = h.api.auth.new_code()["code"]
        wrong = "000000" if code != "000000" else "111111"
        status, data, _ = h.request("POST", "/api/v1/pair", {"code": wrong, "device": "x"}, token=None)
        self.assertEqual((status, data["error"]), (403, "Rossz kód."))
        for _ in range(4):
            status, data, _ = h.request("POST", "/api/v1/pair", {"code": wrong, "device": "x"}, token=None)
        self.assertIn("Túl sok", data["error"])
        status, data, _ = h.request("POST", "/api/v1/pair", {"code": code, "device": "x"}, token=None)
        self.assertIn("Nincs érvényes", data["error"], "öt rossz próbálkozás után a kód is elvész")

        status, data, _ = h.request("GET", "/api/v1/devices")
        self.assertEqual([(d["name"], d["current"]) for d in data["devices"]], [("Teszt telefon", True)])
        self.assertNotIn("hash", data["devices"][0])
        stored = (Path(h.cfg.data_root) / "telefon.json").read_text(encoding="utf-8")
        self.assertNotIn(h.token, stored, "a kulcsot csak hash-ként tárolja")
        own = data["devices"][0]["id"]
        self.assertEqual(h.request("DELETE", f"/api/v1/devices/{own}")[0], 200)
        self.assertEqual(h.request("GET", "/api/v1/status")[0], 401, "visszavonás után nem engedi be")

    def test_cors_only_for_the_app(self):
        h = self.h
        status, _, headers = h.request("OPTIONS", "/api/v1/status", token=None,
                                       headers={"Origin": "capacitor://localhost",
                                                "Access-Control-Request-Method": "GET"})
        self.assertEqual(status, 204)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "capacitor://localhost")
        self.assertIn("Authorization", headers["Access-Control-Allow-Headers"])
        status, _, _ = h.request("OPTIONS", "/api/v1/status", token=None, headers={"Origin": "https://gonosz.example"})
        self.assertEqual(status, 403)
        _, _, headers = h.request("GET", "/api/v1/status", headers={"Origin": "https://gonosz.example"})
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_chat_waits_for_the_reply_and_events_follow(self):
        h = self.h
        h.idle()
        h.fake.chat_script = [reply("Szia! Miben segíthetek?")]
        status, data, _ = h.request("POST", "/api/v1/chat", {"text": "Szia", "wait": 10})
        self.assertEqual(status, 200, data)
        self.assertEqual((data["done"], data["heard"], data["reply"]), (True, "Szia", "Szia! Miben segíthetek?"))
        status, data, _ = h.request("GET", "/api/v1/events?after=0&wait=0")
        mine = [e for e in data["events"] if e.get("request_id")]
        self.assertEqual([e["type"] for e in mine], ["user", "assistant", "request_done"])
        self.assertTrue(all(e["seq"] > 0 for e in data["events"]))
        started = time.monotonic()
        status, again, _ = h.request("GET", f"/api/v1/events?after={data['last']}&wait=0.4")
        self.assertEqual(again["events"], [])
        self.assertGreaterEqual(time.monotonic() - started, 0.35, "hosszú lekérdezés: vár az újakra")
        status, data, _ = h.request("POST", "/api/v1/chat", {"text": "  "})
        self.assertEqual(status, 400)

    def test_pause_and_resume_from_the_phone(self):
        h = self.h
        h.idle()
        self.assertEqual(h.request("POST", "/api/v1/pause")[0], 202)
        h.wait(lambda: h.app.paused and not h.app.busy)
        status, data, _ = h.request("GET", "/api/v1/status")
        self.assertTrue(data["paused"])
        status, data, _ = h.request("POST", "/api/v1/chat", {"text": "Szia"})
        self.assertEqual((status, data["code"]), (409, "paused"))
        h.fake.chat_script = [reply("Itt vagyok.")]
        status, data, _ = h.request("POST", "/api/v1/chat", {"text": "Szia", "resume": True, "wait": 15})
        self.assertEqual(data["reply"], "Itt vagyok.", "a kérés a folytatás után került sorra")
        self.assertFalse(h.app.paused)

    def test_confirm_from_the_phone(self):
        h = self.h
        answer = {}
        thread = threading.Thread(target=lambda: answer.setdefault("ok", h.ui.confirm("Közzétegyem?", h.app.stop_event)))
        thread.start()
        h.wait(lambda: h.request("GET", "/api/v1/confirm")[1]["confirm"] is not None)
        pending = h.request("GET", "/api/v1/confirm")[1]["confirm"]
        self.assertEqual(pending["prompt"], "Közzétegyem?")
        self.assertEqual(h.request("POST", "/api/v1/confirm", {"id": pending["id"], "approved": True})[0], 200)
        thread.join(5)
        self.assertTrue(answer["ok"])
        self.assertEqual(h.request("POST", "/api/v1/confirm", {"id": pending["id"], "approved": True})[0], 404)

    def test_items_photos_and_selling(self):
        h = self.h
        status, data, _ = h.request("POST", "/api/v1/work/items", {"targets": ["vinted"], "prompt": "Zara L-es póló"})
        self.assertEqual(status, 201)
        item_id = data["item"]["id"]
        status, data, _ = h.request("POST", f"/api/v1/work/items/{item_id}/photos", raw=jpeg(), content_type="image/jpeg")
        self.assertEqual((status, data["item"]["photos"]), (201, ["01.jpg"]))
        status, image, headers = h.request("GET", f"/api/v1/work/items/{item_id}/photos/01.jpg?thumb=1&access_token="
                                           + h.token, token=None)
        self.assertEqual((status, headers["Content-Type"], image[:2]), (200, "image/jpeg", b"\xff\xd8"),
                         "a képet <img src> is betöltheti (a kulcs a címben)")
        status, data, _ = h.request("PATCH", f"/api/v1/work/items/{item_id}", {"fields": {"price": "1 500 Ft"}})
        self.assertEqual(data["item"]["price"], 1500)
        status, data, _ = h.request("GET", "/api/v1/work")
        self.assertEqual([i["id"] for i in data["items"]], [item_id])
        self.assertIn("vinted", [p["key"] for p in data["platforms"]])
        status, data, _ = h.request("POST", f"/api/v1/work/items/{item_id}/sell")
        self.assertEqual((status, data["code"]), (409, "acknowledge"), "előbb a botokra vonatkozó figyelmeztetés")
        h.request("POST", "/api/v1/work/ack", {"platform": "vinted"})
        h.idle()
        h.fake.chat_script = [extracted(**{**SHIRT, "size": ""})]
        self.assertEqual(h.request("POST", f"/api/v1/work/items/{item_id}/sell")[0], 202)
        h.wait(lambda: h.app.work.store.get(item_id).status == "kérdés")
        status, data, _ = h.request("GET", f"/api/v1/work/items/{item_id}")
        self.assertIn("még kell: méret", data["item"]["pending"]["question"])
        self.assertEqual(h.request("DELETE", f"/api/v1/work/items/{item_id}/photos/01.jpg")[1]["item"]["photos"], [])
        self.assertEqual(h.request("GET", "/api/v1/work/items/zzzzzzzz")[0], 404)

    def test_sold_and_delist(self):
        h = self.h
        store = h.app.work.store
        item = store.create(targets=["vinted", "jofogas"], prompt="póló")
        store.mark(item.id, "vinted", POSTED, url="https://www.vinted.hu/items/1")
        store.mark(item.id, "jofogas", POSTED, url="https://www.jofogas.hu/x_1.htm")
        status, data, _ = h.request("POST", f"/api/v1/work/items/{item.id}/sold", {"where": "vinted", "delist": False})
        self.assertEqual((status, data["still_posted"], data["delisting"]), (200, ["jofogas"], []))
        self.assertTrue(data["item"]["sold"])
        self.assertEqual(data["item"]["platforms"]["vinted"]["note"], "Itt kelt el.")
        other = store.create(targets=["vinted"], prompt="nadrág")
        status, data, _ = h.request("POST", f"/api/v1/work/items/{other.id}/delist", {})
        self.assertEqual((status, data["code"]), (409, "not_posted"))
        status, data, _ = h.request("POST", f"/api/v1/work/items/{item.id}/sold", {"sold": False})
        self.assertFalse(data["item"]["sold"])

    def test_drift_note_false_flag_and_price_confirmation(self):
        h = self.h
        store = h.app.work.store
        item = store.create(targets=["vinted", "jofogas"], prompt="farmer", title="Levi's farmer", price=9000)
        store.mark(item.id, "vinted", POSTED, url="https://www.vinted.hu/items/1234567-levis")
        status, data, _ = h.request("PATCH", f"/api/v1/work/items/{item.id}", {"fields": {"price": 8500}})
        self.assertEqual((status, data["item"]["drift"]), (200, [{"platform": "vinted", "price": 9000}]))
        self.assertTrue(data["drift_note"].startswith("Csak itt írtam át; a Vinteden még 9 000 Ft."), data["drift_note"])
        status, data, _ = h.request("PATCH", f"/api/v1/work/items/{item.id}", {"fields": {"prompt": "Levi's 501"}})
        self.assertEqual(data["drift_note"], "", "a leírásod nincs a hirdetésben")
        # „Nem igaz, fent van”: a háttérellenőrzés téves jelzése
        store.set_check(item.id, "vinted", "sold", detail="„Eladva” jelzés az oldalán")
        status, data, _ = h.request("POST", f"/api/v1/work/items/{item.id}/dismiss", {"platform": "vinted"})
        self.assertEqual((status, data["item"]["platforms"]["vinted"]["check"]), (200, "live"))
        self.assertEqual(data["item"]["platforms"]["vinted"]["check_dismissed"], "sold")
        status, data, _ = h.request("POST", f"/api/v1/work/items/{item.id}/dismiss", {"platform": "jofogas"})
        self.assertEqual((status, data["code"]), (400, "bad_request"), "ott nincs fent")
        # az „Igen” a „… Ft-ra gondoltál?” kérdésre
        store.set_pending(item.id, {"kind": "price", "question": "45 Ft-ra gondoltál?", "suggested": 4500,
                                    "confirm": 45})
        self.assertEqual(h.request("POST", f"/api/v1/work/items/{item.id}/price", {"choice": "confirmed"})[0], 202)
        h.wait(lambda: store.get(item.id).price == 45)
        h.idle()
        self.assertEqual(store.get(item.id).pending, {})

    def test_jobs_watches_and_notifications(self):
        h = self.h
        status, data, _ = h.request("GET", "/api/v1/jobs")
        self.assertEqual({j["key"] for j in data["jobs"]}, {"watches", "stale", "listings", "messages", "deals"})
        status, data, _ = h.request("POST", "/api/v1/jobs/snooze", {"minutes": 30})
        self.assertGreater(data["snoozed_until"], time.time())
        self.assertIn("felfüggesztve", h.request("GET", "/api/v1/status")[1]["scheduler"]["blocked"])
        self.assertIsNone(h.request("POST", "/api/v1/jobs/snooze", {"minutes": 0})[1]["snoozed_until"])
        status, data, _ = h.request("POST", "/api/v1/watches", {"query": "python állás", "site": "profession.hu"})
        self.assertEqual(status, 201)
        watch_id = data["watch"]["id"]
        self.assertEqual(h.request("GET", "/api/v1/watches")[1]["watches"][0]["label"],
                         "„python állás” (profession.hu, az elmúlt hét)")
        h.idle()
        FakeDDGS.results = [[{"title": "Állás", "href": "https://profession.hu/1", "body": ""}]]
        self.assertEqual(h.request("POST", "/api/v1/jobs/watches/run")[0], 202)
        h.wait(lambda: h.app.watches.find(watch_id).checks == 1)
        self.assertEqual(h.request("DELETE", f"/api/v1/watches/{watch_id}")[0], 200)
        self.assertEqual(h.request("POST", "/api/v1/jobs/nincs/run")[0], 404)

        h.app.notices.add("watch", "Új találat", "Új állás jelent meg.")
        status, data, _ = h.request("GET", "/api/v1/notifications?unread=1")
        self.assertEqual((len(data["notifications"]), data["unread"]), (1, 1))
        self.assertEqual(h.request("POST", "/api/v1/notifications/read", {})[1]["marked"], 1)
        self.assertEqual(h.request("GET", "/api/v1/status")[1]["unread"], 0)

    def test_voice_tts_and_bad_requests(self):
        h = self.h
        status, data, _ = h.request("POST", "/api/v1/voice", raw=b"RIFF....", content_type="audio/wav")
        self.assertEqual((status, data["code"]), (409, "no_voice"))
        status, data, _ = h.request("GET", "/api/v1/tts?text=Szia")
        self.assertEqual((status, data["code"]), (503, "no_speech"))
        self.assertEqual(h.request("GET", "/api/v1/nincs-ilyen")[0], 404)
        status, data, _ = h.request("POST", "/api/v1/chat", raw=b"{nem json", content_type="application/json")
        self.assertEqual(status, 400)
        status, data, _ = h.request("POST", "/api/v1/chat", raw=b"x" * 300_000)
        self.assertEqual((status, data["code"]), (413, "too_large"))

    def test_pairing_from_the_jarvis_window(self):
        h = self.h
        conn = http.client.HTTPConnection("127.0.0.1", h.gui.port, timeout=5)
        conn.request("GET", f"/api/phone?t={h.gui.token}", headers={"Host": f"127.0.0.1:{h.gui.port}"})
        info = json.loads(conn.getresponse().read())
        self.assertTrue(info["enabled"])
        self.assertEqual([d["name"] for d in info["devices"]], ["Teszt telefon"])
        conn.request("POST", "/api/phone/code", body=b"{}",
                     headers={"Host": f"127.0.0.1:{h.gui.port}", "X-Jarvis-Token": h.gui.token,
                              "Content-Type": "application/json", "Content-Length": "2"})
        code = json.loads(conn.getresponse().read())["code"]
        conn.close()
        self.assertRegex(code, r"^\d{6}$")
        status, data, _ = h.request("POST", "/api/v1/pair", {"code": code, "device": "Tablet"}, token=None)
        self.assertEqual(status, 200)
        self.assertEqual(len(h.api.auth.devices()), 2)


class RemoteWindowTests(unittest.TestCase):
    """A Jarvis ablaka a telefonon és a laptopon (remote.py): belépés párosítással, ugyanaz a felület, minden a
    gépen fut; ami csak a gép előtt ülve szabad (kilépés, új párosító kód, a gép mikrofonja), az tiltva."""

    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def get(self, path: str, headers: dict | None = None):
        return self.h.request("GET", path, token=None, headers=headers)

    def post(self, path: str, body=None, raw: bytes | None = None, content_type: str = "application/json",
             token: str | None = None):
        return self.h.request("POST", path, body=body, raw=raw, token=None, content_type=content_type,
                              headers={"X-Jarvis-Token": token or self.h.token})

    def test_login_page_and_installable_app(self):
        status, page, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("Párosító kód".encode(), page)
        self.assertIn("form-action 'none'", headers["Content-Security-Policy"])
        self.assertIn("Párosító kód".encode(), self.get("/?t=rossz-kulcs")[1], "rossz kulccsal is a belépés jön")
        self.assertIn(f'href="{remote.APK_URL}"'.encode(), page, "a telefon böngészőjéből letölthető az alkalmazás")
        status, manifest, _ = self.get("/manifest.webmanifest")
        self.assertEqual((manifest["name"], manifest["start_url"], manifest["display"]), ("Jarvis", "/", "standalone"))
        self.assertIn(b"notificationclick", self.get("/sw.js")[1])
        from PIL import Image
        import io
        for path, size in (("/icon-192.png", 192), ("/icon-512.png", 512), ("/icon-maskable-512.png", 512)):
            status, data, headers = self.get(path)
            self.assertEqual((status, headers["Content-Type"]), (200, "image/png"))
            self.assertEqual(Image.open(io.BytesIO(data)).size, (size, size))

    def test_the_jarvis_window_with_the_device_key(self):
        h = self.h
        status, page, headers = self.get(f"/?t={h.token}")
        self.assertEqual(status, 200)
        self.assertIn(b'<meta name="jarvis-remote" content="1">', page)
        self.assertIn(b'rel="manifest"', page)
        self.assertIn("Beszélgetés".encode(), page)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(self.get("/ui/app.js")[0], 200, "a felület fájljai kulcs nélkül is jönnek")
        status, data, _ = self.get(f"/api/jobs?t={h.token}")
        self.assertEqual(status, 200)
        self.assertIn("ready", data)
        self.assertEqual(self.get("/api/jobs?t=rossz")[0], 401)
        self.assertEqual(self.get("/nincs/ilyen")[0], 401)
        self.assertEqual(self.get(f"/nincs/ilyen?t={h.token}")[0], 404)
        h.fake.chat_script = [reply("Itt vagyok.")]
        self.assertEqual(self.post("/api/send", {"text": "Ott vagy?", "mode": "chat"})[0], 202)
        h.wait(lambda: not h.fake.chat_script and not h.app.busy)   # a válasz megjött (gyorsabb is lehet egy várásnál)
        client = h.bus.subscribe()                  # a visszajátszásban ott a teljes beszélgetés
        events = [client.get_nowait() for _ in range(client.qsize())]
        h.bus.unsubscribe(client)
        sent = [e for e in events if e.get("type") == "user" and e.get("text") == "Ott vagy?"]
        self.assertEqual([e["via"] for e in sent], ["remote"], "távolról jött: a gép nem olvassa fel")
        self.assertTrue(any(e.get("type") == "assistant" and e.get("text") == "Itt vagyok." for e in events))

    def test_only_at_the_computer(self):
        h = self.h
        self.assertEqual(self.post("/api/quit", {})[0], 403)
        self.assertFalse(h.app.exit_requested.is_set(), "távolról nem lehet kilépni")
        self.assertEqual(self.post("/api/phone/code", {})[0], 403, "új eszközt csak a gép előtt ülve")
        self.assertEqual(self.get(f"/api/phone?t={h.token}")[0], 403)
        self.assertEqual(self.post("/api/listen", {})[0], 403, "a gép mikrofonját nem")
        self.assertEqual(self.post("/api/send", {"text": "szia"}, token="rossz")[0], 401)

    def test_live_events_stream_through(self):
        h = self.h
        conn = http.client.HTTPConnection("127.0.0.1", h.api.port, timeout=10)
        conn.request("GET", f"/events?t={h.token}")
        response = conn.getresponse()
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        seen = b""
        while b'"type": "hello"' not in seen:
            seen += response.read1(65536)
        h.ui.info("Üzenet a telefonnak")
        while "Üzenet a telefonnak".encode() not in seen:
            seen += response.read1(65536)
        conn.close()

    def test_voice_and_photos_from_the_phone(self):
        h = self.h
        status, data, _ = self.post("/api/voice?mode=chat", raw=b"\x1aE\xdf\xa3" + b"0" * 4000,
                                    content_type="audio/webm")
        self.assertEqual(status, 409, "beszédfelismerés nélkül szól")
        self.assertIn("beszédfelismerés", data["error"])
        calls = []
        h.app.stt = object()
        h.app.submit_audio = lambda data, mode="chat", request_id=None, silent=True: calls.append((len(data), mode, silent))
        self.assertEqual(self.post("/api/voice?mode=work", raw=b"0" * 5000, content_type="audio/webm")[0], 202)
        self.assertEqual(calls, [(5000, "work", True)])
        item = h.app.work.store.create(targets=["vinted"], prompt="póló")
        status, data, _ = self.post(f"/api/work/items/{item.id}/photo", raw=jpeg(), content_type="application/octet-stream")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["item"]["photos"]), 1, "a fotó a gépre került")


ANDROID_CHROME = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0.0.0 Mobile Safari/537.36")


class TailscaleAddressTests(unittest.TestCase):
    """A gép címe a Telefon ablakban: a `tailscale serve status` kiírásából, ha a mi portunkat adja tovább."""

    STATUS = ("# Funnel off:\n\n"
              "https://gep.tail1234.ts.net:8443 (tailnet only)\n|-- / proxy http://127.0.0.1:3000\n\n"
              "https://gep.tail1234.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:8765\n")

    def test_address_of_our_port(self):
        self.assertEqual(remote.serve_address(self.STATUS, 8765), "https://gep.tail1234.ts.net")
        self.assertEqual(remote.serve_address(self.STATUS, 3000), "https://gep.tail1234.ts.net:8443")
        self.assertEqual(remote.serve_address(self.STATUS.replace("127.0.0.1", "localhost"), 8765),
                         "https://gep.tail1234.ts.net")

    def test_not_served(self):
        self.assertEqual(remote.serve_address(self.STATUS, 876), "", "a 8765 nem a 876-os port")
        self.assertEqual(remote.serve_address(self.STATUS.replace("8765", "87650"), 8765), "")
        self.assertEqual(remote.serve_address("No serve config\n", 8765), "")
        self.assertEqual(remote.serve_address("", 8765), "")

    def test_runs_tailscale_when_installed(self):
        from unittest import mock
        import subprocess

        done = subprocess.CompletedProcess(["tailscale"], 0, stdout=self.STATUS, stderr="")
        with mock.patch.object(remote.shutil, "which", return_value="/usr/bin/tailscale"), \
                mock.patch.object(remote.subprocess, "run", return_value=done) as run:
            self.assertEqual(remote.tailscale_address(8765), "https://gep.tail1234.ts.net")
        self.assertEqual(run.call_args.args[0], ["/usr/bin/tailscale", "serve", "status"])
        with mock.patch.object(remote.shutil, "which", return_value="/usr/bin/tailscale"), \
                mock.patch.object(remote.subprocess, "run", side_effect=subprocess.TimeoutExpired("tailscale", 5)):
            self.assertEqual(remote.tailscale_address(8765), "", "ha a Tailscale nem felel, nincs cím")
        with mock.patch.object(remote.shutil, "which", return_value=None), \
                mock.patch.object(remote.sys, "platform", "linux"):
            self.assertEqual(remote.tailscale_address(8765), "", "Tailscale nélkül nincs cím")


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class RemoteWindowBrowserTests(unittest.TestCase):
    """A távoli ablak egy valódi (láthatatlan) Chromiumban, telefonméretben: párosítás, beszélgetés."""

    def test_pair_and_talk_from_a_phone(self):
        from playwright.sync_api import sync_playwright

        h = Harness()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                                        user_agent=ANDROID_CHROME)
                errors: list[str] = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.goto(f"http://127.0.0.1:{h.api.port}/")
                self.assertTrue(page.locator("#android a").is_visible(), "a telefon böngészőjében: letöltés")
                self.assertEqual(page.locator("#android a").get_attribute("href"), remote.APK_URL)
                # A gép QR-kódja: a cím a kóddal (#pair=…); a kód beíródik, és az alkalmazásba is átvihető.
                code = h.api.auth.new_code()["code"]
                page.goto("about:blank")          # ugyanarra a címre csak a # változna: az oldal nem töltődne újra
                page.goto(remote.pair_link(f"http://127.0.0.1:{h.api.port}", code))
                page.wait_for_selector("#in-app:not([hidden])")
                self.assertEqual(page.input_value("#code"), code)
                self.assertEqual(page.evaluate("location.hash"), "", "a kód ne maradjon a címben")
                self.assertTrue(page.locator("#android").is_hidden())
                link = page.locator("#open-app").get_attribute("href")
                self.assertTrue(link.startswith(f"intent://pair?server=http%3A%2F%2F127.0.0.1%3A{h.api.port}"
                                                f"&code={code}#Intent;scheme=jarvis;package=hu.jarvis.app;"), link)
                self.assertIn("S.browser_fallback_url=" + quote(remote.APK_URL, safe=""), link)
                page.fill("#device", "Pixel")
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
                page.click("#go")
                page.wait_for_selector("#text", timeout=15000)
                self.assertTrue(page.locator("#phone").is_hidden(), "a párosítás gomb csak a gépen van")
                self.assertIn("Pixel", [d["name"] for d in h.api.auth.devices()])
                h.fake.chat_script = [reply("Szia! Itt vagyok a gépen.")]
                page.fill("#text", "Szia")
                page.press("#text", "Enter")
                page.wait_for_selector("text=Szia! Itt vagyok a gépen.", timeout=15000)
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390,
                                     "nem lóg ki oldalra a telefonon")
                page.reload()                     # a kulcs a címben marad: újratöltés után is bent van
                page.wait_for_selector("text=Szia! Itt vagyok a gépen.", timeout=15000)
                page.goto(f"http://127.0.0.1:{h.api.port}/")   # a megjegyzett kulccsal egyből belép
                page.wait_for_selector("#text", timeout=15000)
                self.assertEqual(errors, [])
                browser.close()
        finally:
            h.close()

    def test_a_notification_button_opens_the_window_at_the_sold_dialog(self):
        """A push-értesítés gombja bezárt ablaknál: a service worker a /#act=sold&… címet nyitja meg, a belépő oldal
        a megjegyzett kulccsal továbbviszi, és a Jarvis ablaka az Eladva ablakot mutatja. Magától semmit nem jelöl."""
        from playwright.sync_api import sync_playwright

        h = Harness()
        try:
            store = h.app.work.store
            item = store.create(targets=["vinted", "jofogas"], prompt="farmer", title="Levi's farmer", price=9000)
            store.mark(item.id, "vinted", POSTED, url="https://www.vinted.hu/items/1234567-levis")
            store.mark(item.id, "jofogas", POSTED, url="https://www.jofogas.hu/x_1.htm")
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context()
                context.add_init_script("try { localStorage.setItem('jarvis-token', %s); } catch { /* about:blank */ }"
                                        % json.dumps(h.token))
                page = context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.goto(f"http://127.0.0.1:{h.api.port}/#act=sold&item={item.id}&where=vinted&platform=&delist=0")
                page.wait_for_selector("#sold-dialog[open]", timeout=15000)
                self.assertTrue(page.is_checked('#sold-where input[value="vinted"]'), "ahol a jelzés volt")
                self.assertFalse(page.is_checked("#sold-delist"), "ahogy az értesítés kérte")
                self.assertEqual(page.evaluate("location.hash"), "", "a kérés nem marad a címben")
                self.assertIn("Levi's farmer", page.inner_text("#w-detail h2"))
                page.click("#sold-no")
                self.assertFalse(store.get(item.id).sold, "a Mégse után semmi nem változik")
                self.assertEqual(errors, [])
                browser.close()
        finally:
            h.close()

    def test_service_worker_shows_the_buttons_of_a_notice(self):
        """A service worker (remote.SERVICE_WORKER) egy hamis böngészőkörnyezetben: a push-üzenet gombjai az
        értesítésen, a gomb pedig az ablakot nyitja meg (nyitott ablaknál üzenetet küld neki), semmi mást."""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            result = page.evaluate("""async (code) => {
                const listeners = {}, shown = [], opened = [], posted = [], fetched = [];
                let windows = [];
                const worker = { addEventListener: (type, fn) => { listeners[type] = fn; }, skipWaiting() {},
                  clients: { claim: async () => {}, matchAll: async () => windows,
                             openWindow: async (url) => { opened.push(url); } },
                  registration: { showNotification: async (title, options) => { shown.push({ title, options }); } } };
                new Function("self", "fetch", code)(worker, async (url) => { fetched.push(url); return { ok: true }; });
                const run = async (type, event) => {
                  let wait = null;
                  event.waitUntil = (p) => { wait = p; };
                  listeners[type](event);
                  await wait;
                };
                const message = { kind: "notice", title: "Elkelt? Farmer", text: "A Vinteden eladottnak látszik.",
                                  tag: "notice-n1", item: "a1b2c3d4", actions: [{ action: "sold", where: "vinted",
                                  delist: true, label: "Eladva itt, vedd le a többiről" }] };
                await run("push", { data: { json: () => message } });
                const options = shown[0].options;
                const click = (action) => run("notificationclick", { action, notification: { data: options.data,
                                                                                             close() {} } });
                await click(options.actions[0].action);            // bezárt ablak: megnyitja
                windows = [{ postMessage: (m) => posted.push(m), focus: async () => {} }];
                await click(options.actions[0].action);            // nyitott ablak: szól neki
                await click("");                                   // az értesítés maga: csak megnyitja
                return { actions: options.actions, opened, posted, fetched };
            }""", remote.SERVICE_WORKER)
            browser.close()
        self.assertEqual(result["actions"], [{"action": "notice-0", "title": "Eladva itt, vedd le a többiről"}])
        self.assertEqual(result["opened"], ["/#act=sold&item=a1b2c3d4&where=vinted&platform=&delist=1"])
        self.assertEqual(result["posted"], [{"type": "jarvis-action", "action": {
            "action": "sold", "where": "vinted", "delist": True, "label": "Eladva itt, vedd le a többiről",
            "item": "a1b2c3d4"}}])
        self.assertEqual(result["fetched"], [], "a gomb nem küld semmit a gépnek")



class FakeStt:
    def describe(self) -> str:
        return "teszt"


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class AndroidAppRequestsBrowserTests(unittest.TestCase):
    """A távoli ablak az Android-alkalmazásban (Chromium, az alkalmazás hídja helyett egy hamis JarvisApp): a
    galériából megosztott fotók az új hirdetéshez kerülnek, a megosztott link a beszélgetésbe, a hangutasítás
    (parancsikon, csempe, értesítés) azonnal felvesz."""

    UA = ANDROID_CHROME + " JarvisAndroid/1"

    def open(self, pw, h, pending: dict, fake_mic: bool = False):
        browser = pw.chromium.launch(headless=True, args=["--use-fake-ui-for-media-stream",
                                                          "--use-fake-device-for-media-stream"] if fake_mic else [])
        context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                                      user_agent=self.UA)
        context.add_init_script("""(() => {
            let wanted = %s;
            window.__asked = 0;
            window.JarvisApp = { pending() { window.__asked += 1; const out = JSON.stringify(wanted); wanted = {};
                                             return out; } };
          })();""" % json.dumps(pending))
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        return browser, page, errors

    def test_shared_photos_go_to_a_new_listing(self):
        from playwright.sync_api import sync_playwright

        h = Harness()
        try:
            self.assertTrue(h.app.describe()["work"])
            with sync_playwright() as pw:
                browser, page, errors = self.open(pw, h, {"photos": 2, "batch": 3, "voice": False, "text": ""})
                served = []

                def shared(route):
                    served.append(route.request.url.split("/__jarvis_shared/")[1])
                    route.fulfill(status=200, body=jpeg(), content_type="image/jpeg",
                                  headers={"X-Jarvis-Name": "farmer%20el%C5%91l.jpg"})

                page.route("**/__jarvis_shared/**", shared)
                page.goto(f"http://127.0.0.1:{h.api.port}/?t={h.token}")
                page.wait_for_selector("#w-new-photos img >> nth=1", timeout=15000)
                self.assertEqual(sorted(served), ["3/0", "3/1"])
                self.assertEqual(page.get_attribute("body", "data-mode"), "work")
                self.assertEqual(page.evaluate("document.activeElement.id"), "w-prompt")
                self.assertEqual(page.locator("#w-new-photos img").count(), 2)
                self.assertIn("2 fotó a megosztásból", page.inner_text(".toast"))
                # A megosztott fotókkal ugyanúgy megy a feltöltés, mint a kézzel választottakkal.
                page.fill("#w-prompt", "Hosszú farmer, 40-es")
                page.check('#w-sites input[value="vinted"]')
                h.app.submit_work = lambda *args, **kwargs: None
                page.click("#w-go")
                page.wait_for_selector("#ask-dialog[open]")
                page.click("#ask-yes")
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not any(len(i.photos) == 2 for i in h.app.work.store.all()):
                    time.sleep(0.1)
                self.assertTrue(any(len(i.photos) == 2 for i in h.app.work.store.all()), "a fotók a gépre kerültek")
                self.assertEqual(errors, [])
                browser.close()
        finally:
            h.close()

    def test_shared_link_and_voice_request(self):
        from playwright.sync_api import sync_playwright

        h = Harness()
        try:
            h.app.stt = FakeStt()
            h.bus.set_hello(h.app.describe())
            calls = []
            h.app.submit_audio = lambda data, mode="chat", request_id=None, silent=True: calls.append((len(data), mode))
            with sync_playwright() as pw:
                browser, page, errors = self.open(pw, h, {"text": "https://www.vinted.hu/items/123", "voice": True},
                                                  fake_mic=True)
                page.goto(f"http://127.0.0.1:{h.api.port}/?t={h.token}")
                page.wait_for_selector("#listen.recording", timeout=15000)
                self.assertEqual(page.input_value("#text"), "https://www.vinted.hu/items/123")
                self.assertEqual(page.get_attribute("body", "data-mode"), "chat")
                page.wait_for_timeout(900)
                page.click("#listen")                     # kész: a felvétel a gépre megy
                deadline = time.monotonic() + 10
                while not calls and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertEqual([mode for _, mode in calls], ["chat"])
                self.assertGreater(calls[0][0], 1000)
                # Az alkalmazás szól, hogy új kérés jött (pl. a csempéről), és a felület lekérdezi.
                page.evaluate("window.JarvisPending()")
                self.assertGreaterEqual(page.evaluate("window.__asked"), 2)
                self.assertEqual(errors, [])
                browser.close()
        finally:
            h.close()


    def test_sold_button_of_a_notification_opens_the_sold_dialog(self):
        """Az Android-alkalmazás értesítésének „Eladva itt, vedd le a többiről” gombja: az alkalmazás megnyílik, és
        a hirdetés Eladva ablaka előre kiválasztva jön fel; a levétel csak a Rendben gombra indul."""
        from playwright.sync_api import sync_playwright

        h = Harness()
        try:
            store = h.app.work.store
            item = store.create(targets=["vinted", "jofogas"], prompt="farmer", title="Levi's farmer", price=9000)
            store.mark(item.id, "vinted", POSTED, url="https://www.vinted.hu/items/1234567-levis")
            store.mark(item.id, "jofogas", POSTED, url="https://www.jofogas.hu/x_1.htm")
            calls = []
            h.app.submit_work = lambda *args, **kwargs: calls.append((args, kwargs))
            with sync_playwright() as pw:
                browser, page, errors = self.open(pw, h, {"sold": {"item": item.id, "where": "vinted", "delist": True}})
                page.goto(f"http://127.0.0.1:{h.api.port}/?t={h.token}")
                page.wait_for_selector("#sold-dialog[open]", timeout=15000)
                self.assertEqual(page.get_attribute("body", "data-mode"), "work")
                self.assertTrue(page.is_checked('#sold-where input[value="vinted"]'))
                self.assertTrue(page.is_checked("#sold-delist"))
                self.assertFalse(store.get(item.id).sold, "magától nem jelöl meg semmit")
                self.assertEqual(calls, [], "és nem is vesz le")
                page.click("#sold-yes")
                deadline = time.monotonic() + 10
                while not calls and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual([(args, kwargs["platforms"]) for args, kwargs in calls],
                                 [(("delist", [item.id], ""), ["jofogas"])], "a többi oldalról, a Rendben után")
                found = store.get(item.id)
                self.assertTrue(found.sold)
                self.assertEqual(found.platforms["vinted"]["note"], "Itt kelt el.")
                self.assertEqual(errors, [])
                browser.close()
        finally:
            h.close()

    def test_recording_stops_after_silence(self):
        """A felvétel magától leáll a beszéd utáni csendnél: a mikrofon helyett egy hangfájl (csend, 1 mp hang,
        csend), és senki sem nyomja meg a gombot."""
        import math
        import struct
        import wave
        from playwright.sync_api import sync_playwright

        h = Harness()
        try:
            rate = 48000
            frames = [0] * int(0.6 * rate) + [int(16000 * math.sin(2 * math.pi * 440 * i / rate))
                                              for i in range(rate)] + [0] * int(10 * rate)
            wav = Path(h.tmp.name) / "beszed.wav"
            with wave.open(str(wav), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(rate)
                out.writeframes(struct.pack(f"<{len(frames)}h", *frames))
            h.app.stt = FakeStt()
            h.bus.set_hello(h.app.describe())
            calls = []
            h.app.submit_audio = lambda data, mode="chat", request_id=None, silent=True: calls.append(len(data))
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=[
                    "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                    f"--use-file-for-fake-audio-capture={wav}%noloop"])
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{h.api.port}/?t={h.token}")
                page.wait_for_selector("#listen:not([disabled])", timeout=15000)
                started = time.monotonic()
                page.click("#listen")
                page.wait_for_selector("#listen.recording")
                page.wait_for_selector("#listen:not(.recording)", timeout=12000)
                took = time.monotonic() - started
                deadline = time.monotonic() + 10
                while not calls and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertEqual(len(calls), 1, "magától elküldte")
                self.assertGreater(took, 2.0, "a hang alatt nem állt le")
                self.assertLess(took, 6.5, "a csend után leállt (nem a fájl végén, és nem a 45 másodperces határnál)")
                browser.close()
        finally:
            h.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
