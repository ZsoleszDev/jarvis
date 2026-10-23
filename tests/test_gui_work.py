"""Az Adás/Vétel felületének tesztjei: valódi helyi szerver, valódi (láthatatlan) Chromium, hamis
modell, és a hirdetési oldalak helyett helyi tesztoldalak. A biztonsági tesztekhez nem kell böngésző.

Futtatás:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import base64
import http.client
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_offline import FakeOllama, make_cfg, reply, tool_reply  # noqa: E402
from test_web_browser import HAS_CHROMIUM  # noqa: E402
from test_work import SHIRT, Pages, extracted, jpeg, local_platforms, seen  # noqa: E402

from jarvis_core import selling  # noqa: E402
from jarvis_core.app import JarvisApp  # noqa: E402
from jarvis_core.gui import EventBus, GuiServer, GuiUI  # noqa: E402


class WorkHarness:
    """Teljes Jarvis (Munka menüvel) grafikus felülettel, hamis modellel, hang nélkül."""

    def __init__(self):
        self.pages = Pages()
        self.patch = mock.patch.dict(selling.PLATFORMS, local_platforms(self.pages), clear=True)
        self.patch.start()
        self.fake = FakeOllama()
        self.fake.vram_fit = lambda ctx: 1.0
        self.tmp = tempfile.TemporaryDirectory()
        cfg = make_cfg(Path(self.tmp.name), self.fake.url)
        cfg.browser.headless = True
        cfg.web.allow_local = True
        cfg.assistant.show_timings = False
        cfg.assistant.fillers = False
        self.bus = EventBus()
        self.ui = GuiUI(self.bus, "Jarvis")
        self.app = JarvisApp(cfg, self.ui, use_voice=False, use_speech=False, use_wake_word=False)
        assert self.app.start()
        self.app.start_agent()
        self.server = GuiServer(self.app, self.ui, self.bus)
        self.server.start()
        self.bus.set_hello(self.app.describe())
        self.store = self.app.work.store
        self.photo = Path(self.tmp.name) / "foto.jpg"
        self.photo.write_bytes(jpeg())

    def ready_item(self, targets=("vinted",), **values):
        item = self.store.create(targets=list(targets), prompt="Zara L-es fekete póló, 500 Ft")
        self.store.add_photo(item.id, jpeg())
        return self.store.update(item.id, **{**SHIRT, **values})

    def close(self) -> None:
        self.app.shutdown()
        self.server.stop()
        self.fake.close()
        self.patch.stop()
        self.pages.close()
        self.tmp.cleanup()


def request(port, method, path, headers=None, body=b""):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


class WorkSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = WorkHarness()
        cls.port = cls.h.server.port
        cls.item = cls.h.ready_item()
        cls.ok = {"X-Jarvis-Token": cls.h.server.token, "Host": f"127.0.0.1:{cls.port}"}

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def post(self, path, body, headers=None):
        status, data = request(self.port, "POST", path, {**(headers or self.ok), "Content-Type": "application/json"},
                               json.dumps(body).encode())
        return status, json.loads(data or b"{}") if data.startswith(b"{") else {}

    def test_reading_needs_token(self):
        host = {"Host": f"127.0.0.1:{self.port}"}
        self.assertEqual(request(self.port, "GET", "/api/work/items", host)[0], 403)
        status, data = request(self.port, "GET", f"/api/work/items?t={self.h.server.token}", host)
        self.assertEqual(status, 200)
        data = json.loads(data)
        self.assertIn(self.item.id, [i["id"] for i in data["items"]])
        self.assertEqual([p["key"] for p in data["platforms"]], ["vinted", "jofogas", "vatera", "hardverapro"])
        photo = f"/api/work/photo/{self.item.id}/01.jpg"
        self.assertEqual(request(self.port, "GET", photo, host)[0], 403)
        status, data = request(self.port, "GET", f"{photo}?t={self.h.server.token}&thumb=1", host)
        self.assertEqual(status, 200)
        self.assertTrue(data.startswith(b"\xff\xd8"), "JPEG")
        for bad in (f"/api/work/photo/{self.item.id}/..%2Fitem.json", f"/api/work/photo/{self.item.id}/09.jpg",
                    "/api/work/photo/../../01.jpg"):
            self.assertEqual(request(self.port, "GET", f"{bad}?t={self.h.server.token}", host)[0], 404, bad)

    def test_deal_searches_from_the_window(self):
        status, data = self.post("/api/work/deals", {"action": "add", "query": "PS5 kontroller", "sites": ["vinted"],
                                                     "min_price": "8000", "exclude": "tok, töltő", "min_profit": "3000"})
        self.assertEqual(status, 200, data)
        search = data["searches"][-1]
        self.assertEqual((search["status"], search["min_price"], search["exclude"], search["min_profit"]),
                         ("felmérés", 8000, ["tok", "töltő"], 3000))
        status, data = self.post("/api/work/deals", {"action": "accept", "id": search["id"]})
        self.assertEqual(status, 400, "még nincs javaslat")
        status, data = self.post("/api/work/deals", {"action": "accept", "id": search["id"], "price": "14000"})
        self.assertEqual((status, data["searches"][-1]["status"], data["searches"][-1]["reference"]), (200, "fut", 14000))
        status, data = self.post("/api/work/deals", {"action": "update", "id": search["id"], "max_price": "20000"})
        self.assertEqual((status, data["searches"][-1]["max_price"]), (200, 20000))
        status, data = self.post("/api/work/deals", {"action": "recalibrate", "id": search["id"]})
        self.assertEqual((status, data["searches"][-1]["status"]), (200, "felmérés"))
        self.post("/api/work/deals", {"action": "remove", "id": search["id"]})

    def test_vatera_watches_and_offers_from_the_window(self):
        status, data = self.post("/api/work/deals", {"action": "watch_add", "target": "PS5", "min_profit": "4000",
                                                     "hours": "4", "every": "60"})
        self.assertEqual(status, 200, data)
        [watch] = data["watches"]
        self.assertEqual((watch["name"], watch["min_profit"], watch["hours"], watch["every"]), ("PS5", 4000, 4, 60))
        status, data = self.post("/api/work/deals", {"action": "watch_add", "target": "https://example.com/x"})
        self.assertEqual(status, 400, "csak az ismert oldalak")
        # egy figyelt oldal (itt a helyi „Vinted” katalógusa): a legújabb elöl
        status, data = self.post("/api/work/deals", {"action": "watch_add", "target": "http://127.0.0.1:9/katalogus.html?q=konzol"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["watches"][-1]["name"], "Vinted: konzol")
        self.assertTrue(data["watches"][-1]["url"].endswith("?q=konzol&order=newest_first"))
        for w in data["watches"]:
            status, data = self.post("/api/work/deals", {"action": "watch_remove", "id": w["id"]})
        self.assertEqual((status, data["watches"]), (200, []))
        status, data = self.post("/api/work/deals", {"action": "browse", "on": True, "sites": ["hardverapro", "vinted"],
                                                     "min_profit": "8 000"})
        self.assertEqual((status, data["browse"]["on"], data["browse"]["sites"], data["browse"]["min_profit"]),
                         (200, True, ["hardverapro", "vinted"], 8000))
        self.assertEqual(data["journal"][0]["text"], "Bekapcsoltad a napközbeni böngészést.")
        self.assertTrue(any(lane["name"] == "Vinted: Kezdőoldal" for lane in data["lanes"]))
        self.assertEqual(self.post("/api/work/deals", {"action": "browse", "sites": []})[0], 400, "legalább egy oldal")
        status, data = self.post("/api/work/deals", {"action": "journal", "day": data["journal_days"][0]})
        self.assertEqual((status, data["entries"][0]["text"]), (200, "Bekapcsoltad a napközbeni böngészést."))
        self.post("/api/work/deals", {"action": "browse", "on": False})
        status, data = self.post("/api/work/deals", {"action": "day", "start": "8", "end": "22"})
        self.assertEqual((status, data["day"]), (200, [8, 22]))
        self.assertEqual(self.post("/api/work/deals", {"action": "day", "start": "22", "end": "8"})[0], 400)
        self.h.app.work.deals.book.add([{"text": f"RTX 3060 Ti videókártya {n}", "price": 100000 + n * 1000,
                                         "url": f"https://hardverapro.hu/apro/{100000 + n}.html"} for n in range(5)])
        status, data = self.post("/api/work/deals", {"action": "lookup", "query": "rtx 3060 ti"})
        self.assertEqual((status, data["results"][0]["reference"]["price"]), (200, 102000))
        status, data = self.post("/api/work/deals", {"action": "offer", "id": "nincs-ilyen"})
        self.assertEqual(status, 400)
        self.h.app.work.deals._remember({"id": "o1", "url": "https://www.jofogas.hu/1", "text": "PS5 kontroller",
                                         "site": "Jófogás", "platform": "jofogas", "price": 16000, "median": 20000,
                                         "offer": 14000, "at": time.time()})
        status, data = self.post("/api/work/deals", {"action": "offer", "id": "o1"})
        self.assertEqual(status, 202, "az ágens-szálon fut, és a küldés előtt rákérdez")

    def test_bought_deal_becomes_an_item_and_profit_is_tracked(self):
        deals = self.h.app.work.deals
        deals._remember({"id": "d1", "url": "https://hardverapro.hu/apro/1.html", "text": "RTX 3060 Ti", "site": "Hardverapró",
                         "price": 55000, "median": 100000, "margin": 45000, "at": time.time()})
        status, data = self.post("/api/work/deals", {"action": "bought", "id": "d1", "price": "52 000",
                                                     "shipping": "1500", "in_transit": True})
        self.assertEqual(status, 200, data)
        item = self.h.store.get(data["item"])
        self.assertEqual((item.bought_price, item.source, item.prompt, item.search_query),
                         (52000, "https://hardverapro.hu/apro/1.html", "RTX 3060 Ti, 100000 Ft", "RTX 3060 Ti"),
                         "az árkutatás tudja, mit keressen")
        self.assertEqual((item.bought_shipping, item.bought_from, item.in_transit, item.market_price, item.target_price),
                         (1500, "Hardverapró", True, 100000, 90000), "a találatból: honnan, a szokásos ár és a 90%-a")
        self.assertEqual(data["deals"][0]["bought"], item.id)
        self.assertEqual((data["profit"]["open"]["count"], data["profit"]["open"]["spent"],
                          data["profit"]["open"]["expected"]), (1, 53500, 36500))
        status, data = request(self.port, "GET", f"/api/work/ledger?t={self.h.server.token}", self.ok)
        ledger = json.loads(data)
        self.assertEqual((status, ledger["rows"][0]["stage"], ledger["rows"][0]["cost"]), (200, "úton", 53500))
        status, data = self.post("/api/work/ledger", {"action": "update", "id": item.id,
                                                      "fields": {"in_transit": False, "bought_extra": "2 000",
                                                                 "trade_note": "Foxpost, 123", "status": "x"}})
        self.assertEqual((status, data["rows"][0]["stage"], data["rows"][0]["cost"], data["rows"][0]["trade_note"]),
                         (200, "nálad", 55500, "Foxpost, 123"), "megérkezett; a nem átírható mező kimarad")
        self.assertEqual(self.post("/api/work/ledger", {"action": "update", "id": item.id,
                                                        "fields": {"sold": True}})[0], 400, "nincs mit menteni")
        self.h.store.update(item.id, price=95000)
        self.h.store.mark(item.id, sold=True)
        status, data = request(self.port, "GET", f"/api/work/deals?t={self.h.server.token}", self.ok)
        profit = json.loads(data)["profit"]
        self.assertEqual((profit["month"], profit["total"]["profit"], profit["open"]["count"]),
                         ({"profit": 39500, "count": 1, "revenue": 95000}, 39500, 0),
                         "95 000 − (52 000 + 1 500 posta + 2 000 egyéb)")
        status, data = self.post("/api/work/update", {"id": item.id, "fields": {"sold_price": "90000"}})
        self.assertEqual((status, self.h.store.get(item.id).sold_price), (200, 90000), "az eladási ár átírható")
        self.h.store.delete(item.id)

    def test_a_cancelled_purchase_goes_away_everywhere(self):
        """Vételeim › Visszamondva: a tárgy, a találat (és az azonnali figyelő friss hirdetései közül is) eltűnik, a havi
        keretbe nem számít bele; az eladottat és a nem megvettet nem lehet visszamondani."""
        deals, instant = self.h.app.work.deals, self.h.app.work.instant
        url = "https://hardverapro.hu/apro/visszamondott/friss.html"
        deals._remember({"id": "dv", "url": url, "text": "Makita DHP482", "site": "Hardverapró", "price": 20000,
                         "median": 40000, "margin": 20000, "at": time.time()})
        with instant._lock:
            instant._data["hits"].insert(0, {"url": url, "text": "Makita DHP482", "price": 20000, "deal": "dv"})
        status, data = self.post("/api/work/deals", {"action": "bought", "id": "dv"})
        item_id = data["item"]
        status, data = self.post("/api/work/ledger", {"action": "cancel", "id": item_id})
        self.assertEqual(status, 200, data)
        self.assertNotIn(item_id, [r["id"] for r in data["rows"]])
        self.assertNotIn(item_id, [i.id for i in self.h.store.all()], "az eladandók közül is")
        self.assertNotIn("dv", [d["id"] for d in deals.state()["deals"]], "a találatok közül is")
        self.assertNotIn("dv", [h.get("deal") for h in instant.state()["hits"]], "a friss hirdetések közül is")
        self.assertEqual(data["money"]["spent"], 0)
        self.assertEqual(self.post("/api/work/ledger", {"action": "cancel", "id": item_id})[0], 400, "már nincs meg")
        plain = self.h.store.create(prompt="Nem vett tárgy")
        self.assertEqual(self.post("/api/work/ledger", {"action": "cancel", "id": plain.id})[0], 400)
        sold = self.h.store.create(prompt="Eladott", bought_price=1000)
        self.h.store.mark(sold.id, sold=True)
        self.assertEqual(self.post("/api/work/ledger", {"action": "cancel", "id": sold.id})[0], 400)
        for item in (plain, sold):
            self.h.store.delete(item.id)
        # böngésző nélkül olvasott kártya: a cím kerül a leírásba (a város, az eladó nélkül)
        deals._remember({"id": "d2", "url": "https://hardverapro.hu/apro/2.html", "site": "Hardverapró",
                         "text": "Sony WH-1000XM4 fejhallgató Budapest 30 000 Ft ma 14:01 eladó31",
                         "title": "Sony WH-1000XM4 fejhallgató", "explore": "azonnali figyelő: Fejhallgatók",
                         "query": "sony wh1000xm4", "price": 30000, "median": 71000, "at": time.time()})
        status, data = self.post("/api/work/deals", {"action": "bought", "id": "d2"})
        item = self.h.store.get(data["item"])
        self.assertEqual((item.prompt, item.search_query), ("Sony WH-1000XM4 fejhallgató, 71000 Ft", "Sony WH-1000XM4"))
        self.h.store.delete(item.id)

    def test_instant_watch_from_the_window(self):
        """Azonnali figyelő: egy keresőszó két oldalon, egy oldalcím, szünet és törlés; az Indítás és a Leállítás vele együtt
        mindent elindít és leállít; a vételi limit."""
        status, data = request(self.port, "GET", f"/api/work/deals?t={self.h.server.token}", self.ok)
        instant = json.loads(data)["instant"]
        self.assertEqual((status, instant["enabled"], [s["key"] for s in instant["sites"]]),
                         (200, False, ["jofogas", "hardverapro"]))
        self.assertTrue(self.h.app.work.instant.working(), "a szála a Jarvisszal együtt indul")
        status, data = self.post("/api/work/deals", {"action": "instant_add", "target": "RTX 3080", "sites": ["jofogas"],
                                                     "max_price": "200 000", "every": "4"})
        alert = data["instant"]["alerts"][0]
        self.assertEqual((status, alert["sites"], alert["max_price"], alert["every"]), (200, ["jofogas"], 200000, 3))
        self.assertIn("searches", data, "a fül többi része is jön")
        page = self.h.pages.url("katalogus.html")
        status, data = self.post("/api/work/deals", {"action": "instant_add", "target": page})
        self.assertEqual((status, data["instant"]["alerts"][1]["url"]), (200, page))
        self.assertEqual(self.post("/api/work/deals", {"action": "instant_add", "target": ""})[0], 400)
        status, data = self.post("/api/work/deals", {"action": "instant_pause", "id": alert["id"], "off": True})
        self.assertTrue(data["instant"]["alerts"][0]["off"])
        status, data = self.post("/api/work/deals", {"action": "start"})
        self.assertEqual((status, data["running"], data["browse"]["on"], data["instant"]["enabled"], data["day"]),
                         (200, True, True, True, [0, 24]), "egy gomb: minden elindul, napszaktól függetlenül")
        status, data = self.post("/api/work/deals", {"action": "stop"})
        self.assertEqual((status, data["running"], data["browse"]["on"], data["instant"]["enabled"]),
                         (200, False, False, False))
        alerts = data["instant"]["alerts"]
        status, data = self.post("/api/work/deals", {"action": "limits", "max_buy": "30 000", "min_profit": "4000"})
        self.assertEqual((status, data["max_buy"], data["browse"]["min_profit"]), (200, 30000, 4000))
        self.assertEqual(self.post("/api/work/deals", {"action": "limits", "max_buy": "sok"})[0], 400)
        self.post("/api/work/deals", {"action": "limits", "max_buy": ""})
        for stored in alerts:
            status, data = self.post("/api/work/deals", {"action": "instant_remove", "id": stored["id"]})
        self.assertEqual((status, data["instant"]["alerts"]), (200, []))
        self.assertEqual(self.post("/api/work/deals", {"action": "instant_nincs"})[0], 400)
        app = self.h.app
        self.assertTrue(app._instant_allowed())
        app.scheduler.snooze(30)
        self.assertFalse(app._instant_allowed(), "a háttérmunka felfüggesztése őt is megállítja")
        app.scheduler.snooze(0)
        app.cfg.scheduler.active_hours = "00:00-00:01" if time.localtime().tm_hour else "23:00-23:59"
        self.assertFalse(app._instant_allowed(), "az aktív órákon kívül")
        app.cfg.scheduler.active_hours = ""
        self.assertTrue(app._instant_allowed())

    def test_budget_and_piling_up_from_the_window(self):
        """Vételeim › Keret: a havi keret és a felhalmozódás határa; egy új vétel után rögtön szól, ha átlépte."""
        status, data = self.post("/api/work/ledger", {"action": "money", "budget": "100 000", "stock_count": "1"})
        self.assertEqual((status, data["money"]["budget"], data["money"]["limits"]["stock_count"]), (200, 100000, 1))
        self.assertEqual(self.post("/api/work/ledger", {"action": "money", "budget": "sok"})[0], 400)
        ids = []
        for name in ("Makita DHP482", "Bosch GSR 18V"):
            status, data = self.post("/api/work/ledger", {"action": "add", "name": name, "price": "45000"})
            ids.append(data["item"])
        self.assertIn("Felhalmozódtak a megvett tárgyak: 2 vár eladásra", " ".join(data["money"]["warnings"]))
        self.assertEqual((data["money"]["spent"], data["money"]["remaining"]), (90000, 10000))
        titles = [n["title"] for n in self.h.app.notices.list()]
        self.assertIn("Sok tárgy vár eladásra", titles)
        self.assertIn("A havi keret nagy része elment", titles)
        status, data = request(self.port, "GET", f"/api/work/deals?t={self.h.server.token}", self.ok)
        self.assertEqual(json.loads(data)["money"]["remaining"], 10000, "az Automata kereskedés fülön is látszik")
        for item_id in ids:
            self.h.store.delete(item_id)
        self.post("/api/work/ledger", {"action": "money", "budget": "", "stock_count": ""})

    def test_a_purchase_can_be_added_by_hand(self):
        """Vételeim › Új vétel: ami nem az Automata kereskedésből jött (pl. bolhapiacon vetted)."""
        status, data = self.post("/api/work/ledger", {"action": "add", "name": "Makita DHP482", "price": "12 000",
                                                      "bought_shipping": "", "bought_from": "bolhapiac",
                                                      "target_price": "25000", "in_transit": False})
        self.assertEqual(status, 200, data)
        item = self.h.store.get(data["item"])
        self.assertEqual((item.prompt, item.bought_price, item.bought_from, item.target_price, item.in_transit),
                         ("Makita DHP482", 12000, "bolhapiac", 25000, False))
        row = next(r for r in data["rows"] if r["id"] == item.id)
        self.assertEqual((row["stage"], row["expected"]), ("nálad", 13000))
        self.assertEqual(self.post("/api/work/ledger", {"action": "add", "name": "", "price": "1"})[0], 400)
        self.assertEqual(self.post("/api/work/ledger", {"action": "add", "name": "Valami"})[0], 400, "a vételár kell")
        self.assertEqual(self.post("/api/work/ledger", {"action": "nincs"})[0], 400)
        self.h.store.delete(item.id)

    def test_upload_needs_token_and_same_origin(self):
        item = self.h.store.create(targets=["vinted"])
        path = f"/api/work/items/{item.id}/photo"
        image = jpeg()
        self.assertEqual(request(self.port, "POST", path, {"Host": f"127.0.0.1:{self.port}"}, image)[0], 403)
        evil = {**self.ok, "Origin": "https://gonosz.example"}
        self.assertEqual(request(self.port, "POST", path, evil, image)[0], 403)
        status, data = request(self.port, "POST", path, self.ok, b"<script>nem kep</script>")
        self.assertEqual(status, 400)
        self.assertIn("nem kép", json.loads(data)["error"])
        status, _ = request(self.port, "POST", path, {**self.ok, "Content-Length": "30000000"}, b"x")
        self.assertEqual(status, 413)
        status, data = request(self.port, "POST", path, self.ok, image)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["item"]["photos"], ["01.jpg"])

    def test_new_and_update_validate(self):
        status, data = self.post("/api/work/new", {"targets": ["vinted", "ebay"], "prompt": "póló"})
        self.assertEqual(status, 400)
        self.assertIn("ebay", data["error"])
        status, data = self.post("/api/work/new", {"targets": ["jofogas"], "prompt": "<b>póló</b>",
                                                   "research_wanted": True, "price": 5})
        self.assertEqual(status, 200)
        self.assertEqual((data["item"]["prompt"], data["item"]["research_wanted"], data["item"]["price"]),
                         ("<b>póló</b>", True, None), "csak a három mezőt veszi át")
        status, data = self.post("/api/work/update", {"id": self.item.id, "fields": {"price": "sok"}})
        self.assertEqual(status, 400)
        self.assertIn("Az ár", data["error"])
        status, data = self.post("/api/work/update", {"id": self.item.id,
                                                      "fields": {"photos": [], "edited": [], "brand": "Mango"}})
        self.assertEqual((status, data["item"]["brand"], len(data["item"]["photos"])), (200, "Mango", 1),
                         "ismeretlen mezőt nem fogad el, a fotókhoz nem lehet így nyúlni")
        self.assertIn("brand", data["item"]["edited"])
        self.assertEqual(self.post("/api/work/update", {"id": "../../x", "fields": {"title": "x"}})[0], 400)

    def test_actions_validate(self):
        bad = [
            {"action": "rm -rf", "ids": [self.item.id]},
            {"action": "sell", "ids": []},
            {"action": "sell", "ids": ["deadbeef"]},
            {"action": "post", "ids": [self.item.id], "platform": "ebay"},
            {"action": "login", "platform": "ebay"},
            {"action": "reply", "ids": [self.item.id], "text": "   "},
        ]
        for body in bad:
            self.assertEqual(self.post("/api/work/action", body)[0], 400, body)
        evil = {**self.ok, "Origin": "https://gonosz.example"}
        self.assertEqual(self.post("/api/work/action", {"action": "login", "platform": "vinted"}, evil)[0], 403)

    def test_dismiss_drift_note_and_notices(self):
        store = self.h.store
        item = store.create(targets=["vinted", "jofogas"], prompt="farmer", title="Levi's farmer", price=9000)
        store.mark(item.id, "vinted", "feltöltve", url="https://www.vinted.hu/items/1234567-levis")
        status, data = self.post("/api/work/update", {"id": item.id, "fields": {"price": 8500}})
        self.assertEqual(status, 200)
        self.assertTrue(data["drift_note"].startswith("Csak itt írtam át; a Vinteden még 9 000 Ft."), data)
        store.set_check(item.id, "vinted", "gone", detail="az oldala szerint már nem található")
        for platform in ("jofogas", "ebay"):
            self.assertEqual(self.post("/api/work/dismiss", {"id": item.id, "platform": platform})[0], 400, platform)
        status, data = self.post("/api/work/dismiss", {"id": item.id, "platform": "vinted"})
        self.assertEqual((status, data["item"]["platforms"]["vinted"]["check"]), (200, "live"))
        evil = {**self.ok, "Origin": "https://gonosz.example"}
        self.assertEqual(self.post("/api/work/dismiss", {"id": item.id, "platform": "vinted"}, evil)[0], 403)
        # az Értesítések ablak: a kulccsal olvasható, és olvasottnak jelölhető
        host = {"Host": f"127.0.0.1:{self.port}"}
        notice = self.h.app.notices.add("sold", "Elkelt? Farmer", "A Vinteden eladottnak látszik.", item=item.id,
                                        actions=[{"action": "sold", "where": "vinted", "delist": True,
                                                  "label": "Eladva itt, vedd le a többiről"}])
        self.assertEqual(request(self.port, "GET", "/api/notices", host)[0], 403)
        status, raw = request(self.port, "GET", f"/api/notices?t={self.h.server.token}", host)
        data = json.loads(raw)
        self.assertEqual((status, data["notifications"][0]["id"], data["notifications"][0]["actions"][0]["where"]),
                         (200, notice["id"], "vinted"))
        self.assertGreaterEqual(data["unread"], 1)
        self.assertEqual(self.post("/api/notices/read", {"ids": [notice["id"]]}, {"Host": host["Host"]})[0], 403)
        self.assertEqual(self.post("/api/notices/read", {"ids": [notice["id"]]})[1]["marked"], 1)

    def test_settings_and_acknowledgement(self):
        status, data = self.post("/api/work/settings", {"settings": {"city": "Szeged", "parcel_size": "nagy"}})
        self.assertEqual((status, data["settings"]["city"], data["settings"]["parcel_size"]), (200, "Szeged", "nagy"))
        self.assertEqual(self.post("/api/work/settings", {"settings": {"parcel_size": "óriási"}})[0], 400)
        self.assertEqual(self.post("/api/work/ack", {"platform": "ebay"})[0], 400)
        status, data = self.post("/api/work/ack", {"platform": "vatera"})
        self.assertEqual((status, data["acknowledged"]), (200, ["vatera"]))


@unittest.skipUnless(HAS_CHROMIUM, "Playwright/Chromium nincs telepítve")
class WorkBrowserTests(unittest.TestCase):
    """Az Adás/Vétel a valódi felületen, egy láthatatlan Chromiumban."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.h = WorkHarness()
        self.context = self.browser.new_context(viewport={"width": 1000, "height": 900})
        self.page = self.context.new_page()
        self.errors: list[str] = []
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.goto(self.h.server.url)
        self.page.wait_for_selector('body[data-state="idle"]', state="attached", timeout=15000)
        self.page.click("#tab-work")
        self.page.wait_for_selector('body[data-mode="work"]', state="attached")
        self.page.wait_for_selector('#w-sites input[value="vinted"]', state="attached")

    def tearDown(self):
        self.context.close()
        self.h.close()
        self.assertEqual(self.errors, [], "JavaScript-hiba az oldalon")

    def wait_for(self, check, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(0.05)
        self.fail("nem teljesült időben")

    def open_item(self, item_id):
        self.page.click(f'.card[data-id="{item_id}"] button.title')
        self.page.wait_for_selector("#f-title")

    def test_new_listing_asks_back_then_posts(self):
        page = self.page
        self.assertIn("Pipáld be", page.inner_text("#w-needs"))
        page.check('#w-sites input[value="vinted"]')
        page.check('#w-sites input[value="jofogas"]')
        needs = page.inner_text("#w-needs")
        self.assertIn("Vinted, ha ruha, cipő vagy kiegészítő: kinek szól (női, férfi, gyerek), márka, méret, szín.", needs)
        self.assertIn("Jófogás: átvétel helye – írd a leírásba, vagy add meg egyszer az Alapadatoknál.", needs)
        page.click("#w-go")
        self.assertIn("Adj hozzá legalább egy fotót", page.inner_text("#w-compose-msg"))
        with page.expect_file_chooser() as chooser:
            page.click('[data-pick="compose"]')
        chooser.value.set_files(str(self.h.photo))
        page.wait_for_selector("#w-new-photos .photo img")
        page.fill("#w-prompt", "Zara L-es fekete póló, 500 Ft")
        self.h.fake.chat_script = [extracted(**{**SHIRT, "condition": "", "location": ""})]
        page.click("#w-go")
        page.wait_for_selector("#ask-dialog[open]")
        self.assertIn("Vinted, Jófogás", page.inner_text("#ask-text"))
        page.click("#ask-yes")
        # Jarvis visszakérdez a kártyán
        page.wait_for_selector('.card .ask p:has-text("még kell: állapot")')
        [item] = self.h.store.all()
        self.assertEqual((item.targets, item.prompt, len(item.photos)),
                         (["vinted", "jofogas"], "Zara L-es fekete póló, 500 Ft", 1))
        self.assertEqual(sorted(self.h.app.work.settings["acknowledged"]), ["jofogas", "vinted"])
        self.assertEqual(page.input_value("#w-prompt"), "", "az űrlap kiürül a következő tárgynak")
        self.assertEqual(page.locator("#w-new-photos .photo").count(), 0)
        # a válasz gépelése közben frissülő kártya nem veszi el a beírt szöveget
        answer = f'[data-keep="reply-{item.id}"]'
        page.fill(answer, "Nagyon jó, Budapesten")
        self.h.store.set_busy(item.id, "Frissítés…")
        page.wait_for_selector('.card .busy:has-text("Frissítés")')
        self.h.store.set_busy(item.id, "")
        page.wait_for_selector(".card .busy", state="detached")
        self.assertEqual(page.input_value(answer), "Nagyon jó, Budapesten")
        self.h.fake.chat_script = [
            extracted(**SHIRT),
            tool_reply("item_post", {"item": item.id, "platform": "vinted"}),
            reply("Be kell jelentkezned a Vintedre."),
            tool_reply("item_post", {"item": item.id, "platform": "jofogas"}),
            tool_reply("item_mark", {"item": item.id, "status": "feltöltve", "platform": "jofogas"}),
            reply("Fent van a Jófogáson."),
        ]
        page.press(answer, "Enter")
        page.wait_for_selector('#w-log .msg.jarvis:has-text("Fent van: Jófogás. Nem sikerült: Vinted.")',
                               timeout=20000)
        self.assertIn("Nagyon jó, Budapesten", page.inner_text("#w-log"), "a válaszod a naplóban")
        page.wait_for_selector('.card .site.bad:has-text("Vinted")')
        page.wait_for_selector('.card .site.on:has-text("Jófogás")')
        self.assertIn("részben fent", page.inner_text(".card .chip"))
        self.assertEqual(self.h.store.get(item.id).prompt, "Zara L-es fekete póló, 500 Ft\nNagyon jó, Budapesten")
        # a lapján látszik, miért nem sikerült, és újra lehet próbálni
        self.open_item(item.id)
        self.assertIn("Be kell jelentkezned a Vintedre.", page.inner_text('.platform:has-text("Vinted") .pnote'))
        page.wait_for_selector('.platform:has-text("Vinted") [data-action="post"]:has-text("Újra")')

    def test_price_decision_buttons(self):
        item = self.h.ready_item(price=9000, size="")
        self.h.store.set_pending(item.id, {"kind": "price", "suggested": 4500,
                                           "question": "Hasonlót 4 000 és 5 000 Ft között kínálnak. Maradjon?"})
        self.page.wait_for_selector('[data-price="keep"]:has-text("Marad: 9 000 Ft")')
        self.h.fake.chat_script = [extracted(**{**SHIRT, "size": ""})]   # utána a méretre kérdez rá
        self.page.click('[data-price="suggested"]:has-text("Legyen: 4 500 Ft")')
        self.page.wait_for_selector('.card .ask p:has-text("még kell: méret (Vinted)")')
        stored = self.h.store.get(item.id)
        self.assertEqual(stored.price, 4500)
        self.assertIn("price", stored.edited)

    def test_pro_upload_from_photos_with_a_review_before_posting(self):
        """Pro fül: elég a fotó; amit a fotókról nem lát, arra a kártyán rákérdez; utána a Nézd át ablakban minden
        adat és az ár átírható, és a Mehet után kezd feltölteni. A telefonos értesítés ugyanezt az ablakot nyitja meg."""
        page = self.page
        self.h.app.work.researcher = None                      # az ár a fotók alapján (a teszt ne böngésszen)
        page.click("#w-mode-pro")
        self.assertTrue(page.is_visible("#w-pro-help") and page.is_hidden("#w-research-row"))
        self.assertEqual(page.inner_text("#w-prompt-label"), "Megjegyzés (nem kötelező)")
        self.assertTrue(page.is_hidden("#w-camera-row"), "a gépen nincs kamera gomb")
        page.check('#w-sites input[value="vinted"]')
        with page.expect_file_chooser() as chooser:
            page.click('[data-pick="compose"]')
        chooser.value.set_files(str(self.h.photo))
        page.wait_for_selector("#w-new-photos .photo img")
        jacket = dict(product="farmerdzseki", kind="ruha", title="Zara farmerdzseki", brand="Zara", condition="Jó",
                      color="kék", gender="női", category="Ruhák > Kabátok", price_estimate=6300)
        self.h.fake.chat_script = [seen(**jacket)]
        page.click("#w-go")                                    # leírás nélkül is mehet
        page.wait_for_selector("#ask-dialog[open]")
        page.click("#ask-yes")
        page.wait_for_selector('.card .ask p:has-text("még kell: méret")')
        [item] = self.h.store.all()
        self.assertTrue(item.pro)
        self.h.fake.chat_script = [extracted(size="M")]
        page.fill(f'[data-keep="reply-{item.id}"]', "M-es")
        page.click(f'form[data-reply="{item.id}"] button[type="submit"]')
        page.wait_for_selector(f'[data-review="{item.id}"]')
        self.assertIn("átnézésre vár", page.inner_text(f'.card[data-id="{item.id}"]'))
        page.click(f'[data-review="{item.id}"]')
        page.wait_for_selector("#review-dialog[open]")
        self.assertIn("becslés a fotók alapján", page.inner_text("#review-basis"))
        self.assertEqual((page.input_value("#r-title"), page.input_value("#r-price"), page.input_value("#r-size"),
                          page.input_value("#r-gender")), ("Zara farmerdzseki", "6300", "M", "női"))
        page.fill("#r-price", "")
        page.click('#review-form button[type="submit"]')
        page.wait_for_selector('#review-msg:has-text("Add meg az árat")')
        page.fill("#r-price", "5900")
        page.fill("#r-description", "Alig hordtam.")
        self.h.fake.chat_script = [reply("Be kell jelentkezned a Vintedre, utána az Újra gombbal folytatom.")]
        page.click('#review-form button[type="submit"]')
        page.wait_for_selector('.toast:has-text("Mehet: feltöltöm")')
        self.wait_for(lambda: "vinted" in self.h.store.get(item.id).platforms)
        item = self.h.store.get(item.id)
        self.assertEqual((item.price, item.description, item.reviewed), (5900, "Alig hordtam.", True))
        self.assertIn("price", item.edited)
        # a telefonról: a Nézd át értesítésére bökve ugyanez az ablak nyílik meg
        other = self.h.store.create(targets=["vinted"], pro=True, title="Nike Air Max 90", price=12500)
        self.h.store.set_pending(other.id, {"kind": "review", "question": "Nézd át", "basis": "3 hasonló alapján"})
        context = self.browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                                           user_agent="Mozilla/5.0 (Linux; Android 14) Chrome/141.0 Mobile JarvisAndroid/1")
        context.add_init_script("""(() => {
            let wanted = {item: {id: "%s", open: "review"}};
            window.JarvisApp = { pending() { const out = JSON.stringify(wanted); wanted = {}; return out; } };
          })();""" % other.id)
        phone = context.new_page()
        phone.goto(self.h.server.url)
        phone.wait_for_selector("#review-dialog[open]", timeout=15000)
        self.assertEqual(phone.input_value("#r-title"), "Nike Air Max 90")
        self.assertIn("Az ár: 3 hasonló alapján.", phone.inner_text("#review-basis"))
        wide = phone.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
        self.assertFalse(wide, "telefonon nincs vízszintes görgetés")
        phone.click("#review-close")
        phone.click('[data-action="back"]')
        if "folded" in (phone.get_attribute("#w-compose", "class") or ""):
            phone.click("#w-compose-title")                     # telefonon a kész tárgyak mellett össze van csukva
        phone.click("#w-mode-pro")
        self.assertTrue(phone.is_visible("#w-camera-btn"), "a telefonon kamera gomb is van")
        self.assertEqual(phone.get_attribute("#w-camera", "capture"), "environment")
        context.close()

    def test_settings_and_login(self):
        page = self.page
        page.check('#w-sites input[value="hardverapro"]')
        page.click("#w-setup summary")
        page.fill("#s-city", "Szeged")
        page.select_option("#s-parcel", "közepes")
        page.fill("#s-shipping", "Foxpost 1290 Ft")
        page.click("#s-save")
        page.wait_for_selector('.toast:has-text("Elmentettem")')
        self.assertEqual(self.h.app.work.settings["city"], "Szeged")
        self.assertEqual(self.h.app.work.settings["parcel_size"], "közepes")
        self.assertIn("az Alapadatokból: Szeged.", page.inner_text("#w-needs"))
        self.assertIn("az Alapadatokból: Foxpost 1290 Ft.", page.inner_text("#w-needs"))
        page.click('[data-login="vatera"]')
        page.wait_for_selector('#w-log .msg.jarvis:has-text("Megnyitottam a(z) Vatera oldalt")', timeout=20000)

    def test_listing_page_edits_targets_and_status(self):
        item = self.h.ready_item()
        page = self.page
        page.wait_for_selector(f'.card[data-id="{item.id}"]')
        self.open_item(item.id)
        page.fill("#f-title", "Zara póló, újszerű")
        page.press("#f-title", "Tab")
        self.wait_for(lambda: self.h.store.get(item.id).title == "Zara póló, újszerű")
        self.assertIn("title", self.h.store.get(item.id).edited, "Jarvis ezt már nem írja felül")
        page.check('[data-target="vatera"]')
        page.wait_for_selector("#ask-dialog[open]")
        page.click("#ask-yes")
        self.wait_for(lambda: self.h.store.get(item.id).targets == ["vinted", "vatera"])
        page.click('.platform:has-text("Vinted") [data-action="toggle"]')
        page.wait_for_selector('.platform:has-text("Vinted") .pstatus.on')
        self.assertEqual(self.h.store.get(item.id).status, "részben fent")
        page.click('[data-action="sold"]')
        # Eladva: megkérdezi, hol kelt el, és levegye-e a többi oldalról; most nem kérjük a levételt.
        page.wait_for_selector("#sold-dialog[open]")
        self.assertIn("Vinted", page.inner_text("#sold-where"))
        page.uncheck("#sold-delist")
        page.click("#sold-yes")
        page.wait_for_selector('.toast:has-text("Ne felejtsd el levenni innen: Vinted")')
        self.assertTrue(self.h.store.get(item.id).sold)
        page.click('[data-action="delete"]')
        page.wait_for_selector("#ask-dialog[open]")
        page.click("#ask-yes")
        self.wait_for(lambda: not self.h.store.all())
        page.wait_for_selector("#w-compose", state="visible")

    def test_sold_elsewhere_and_price_drift_on_the_listing_page(self):
        item = self.h.ready_item(("vinted", "hardverapro"), price=5000)
        store = self.h.store
        store.mark(item.id, "vinted", "feltöltve", url="https://www.vinted.hu/items/1234567-zara")
        store.mark(item.id, "hardverapro", "feltöltve", url="https://hardverapro.hu/apro/zara/friss.html")
        store.update(item.id, user=True, price=4500)
        store.set_check(item.id, "vinted", "sold", detail="„Eladva” jelzés az oldalán")
        page = self.page
        page.wait_for_selector(f'.card[data-id="{item.id}"] .site.bad:has-text("Vinted ?")')
        self.open_item(item.id)
        row = '.platform:has-text("Vinted")'
        self.assertEqual(page.inner_text(f"{row} .pstatus").split(" (")[0], "Elkeltnek látszik")
        self.assertIn("Ár eltér: a hirdetésben még 5 000 Ft, itt 4 500 Ft.", page.inner_text(row))
        self.assertIn("„Eladva” jelzés az oldalán", page.inner_text(row))
        self.assertEqual(page.locator('[data-action="soldhere"]').count(), 1, "csak ott, ahol elkeltnek látszik")
        page.click(f'{row} [data-action="soldhere"]')
        page.wait_for_selector("#sold-dialog[open]")
        self.assertTrue(page.is_checked('#sold-where input[value="vinted"]'), "ahol a jelzés volt, az van kiválasztva")
        page.uncheck("#sold-delist")
        page.click("#sold-yes")
        page.wait_for_selector('.toast:has-text("Ne felejtsd el levenni innen: Hardverapró")')
        found = store.get(item.id)
        self.assertTrue(found.sold)
        self.assertEqual(found.platforms["vinted"]["note"], "Itt kelt el.")

    def test_ledger_tab_on_a_phone(self):
        """Vételeim telefonméretben: az összesítő, a kártyák (úton, nálad, eladva), a Megérkezett gomb, a részletek
        szerkesztése és egy új vétel; vízszintes görgetés nélkül."""
        store = self.h.store
        now = time.time()
        road = store.create(title="RTX 3060 Ti Gaming OC", bought_price=55000, bought_shipping=1500, target_price=90000,
                            in_transit=True, bought_from="Hardverapró", source="https://hardverapro.hu/apro/1.html")
        sold = store.create(title="Nike Air Max 90", bought_price=15000, sold_price=24000, sell_fee=800,
                            bought_at=now - 4 * 86400)
        store.mark(sold.id, sold=True)
        page = self.page
        page.set_viewport_size({"width": 390, "height": 844})
        page.click("#job-ledger")
        page.wait_for_selector(".lcard")
        self.assertTrue(page.is_hidden("#w-list-view") and page.is_hidden("#w-filters"))
        tiles = page.inner_text(".tiles")
        self.assertIn("+8 200 Ft", tiles, "a hónap haszna: 24 000 − 800 − 15 000")
        self.assertIn("56 500 Ft", tiles, "ennyi van bent (vételár + posta)")
        self.assertIn("+33 500 Ft", tiles, "várható haszon a tervezett árral")
        first = page.locator(".lcard").first
        self.assertIn("Úton", first.inner_text())
        self.assertIn("55 000 Ft + posta 1 500 Ft", first.inner_text())
        page.click('[data-lfilter="eladva"]')
        self.assertEqual(page.locator(".lcard").count(), 1)
        self.assertIn("Nike Air Max 90", page.inner_text(".lcard"))
        page.click('[data-lfilter="all"]')
        page.click(f'[data-larrived="{road.id}"]')
        page.wait_for_selector('.toast:has-text("Megérkezett")')
        self.wait_for(lambda: not store.get(road.id).in_transit)
        page.click(f'.lcard .row [data-lopen="{road.id}"]')
        page.fill("#l-bought_extra", "2000")
        page.fill("#l-trade_note", "Foxpost 123")
        page.click('[data-lsave] button[type="submit"]')
        page.wait_for_selector('.toast:has-text("Mentve")')
        self.assertEqual((store.get(road.id).bought_extra, store.get(road.id).trade_note), (2000, "Foxpost 123"))
        self.assertGreater(store.get(road.id).bought_at, 0, "a vétel napja nem változott, nem lett 0")
        self.assertIn("Foxpost 123", page.inner_text(".ledger-list"))
        page.click("#l-add-toggle")
        page.fill("#la-name", "Makita DHP482")
        page.fill("#la-price", "12000")
        page.fill("#la-target", "25000")
        page.click('#l-add button[type="submit"]')
        page.wait_for_selector('.lcard:has-text("Makita DHP482")')
        self.assertIn("+13 000 Ft", page.inner_text('.lcard:has-text("Makita DHP482")'))
        wide = page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
        self.assertFalse(wide, "telefonon nincs vízszintes görgetés")
        page.click('.lcard:has-text("Makita DHP482") [data-open]')
        page.wait_for_selector("#f-title")
        self.assertTrue(page.is_hidden("#w-ledger"))

    def test_cancel_a_purchase_from_the_ledger_and_the_deals_list(self):
        """Visszamondva: a Vételeim kártyáján és a találatok között (Megvetted ✓ mellett) is; megerősítés után
        mindenhonnan eltűnik. Az eladott tárgynál nincs ilyen gomb."""
        deals, store = self.h.app.work.deals, self.h.store
        for n, name in enumerate(("Makita DHP482", "Bosch GSR 18V")):
            deals._remember({"id": f"d{n}", "url": f"https://hardverapro.hu/apro/{n}/friss.html", "text": name,
                             "site": "Hardverapró", "price": 20000, "median": 40000, "margin": 20000, "at": time.time()})
            item = store.create(prompt=name, bought_price=20000, bought_at=time.time())
            deals.mark_bought(f"d{n}", item.id)
        sold = store.create(title="Nike Air Max 90", bought_price=15000, sold_price=24000)
        store.mark(sold.id, sold=True)
        page = self.page
        page.set_viewport_size({"width": 390, "height": 844})
        page.click("#job-ledger")
        page.wait_for_selector('.lcard:has-text("Makita DHP482")')
        self.assertEqual(page.locator('.lcard:has-text("Nike Air Max 90") [data-cancel-bought]').count(), 0,
                         "az eladottnál nincs Visszamondva")
        page.click('.lcard:has-text("Makita DHP482") [data-cancel-bought]')
        page.wait_for_selector("#ask-dialog[open]")
        self.assertIn("Visszamondtad a vételt: Makita DHP482?", page.inner_text("#ask-text"))
        page.click("#ask-no")
        self.assertEqual(len([i for i in store.all() if i.bought_price]), 3, "Mégse: semmi nem változott")
        page.click('.lcard:has-text("Makita DHP482") [data-cancel-bought]')
        page.click("#ask-yes")
        page.wait_for_selector('.toast:has-text("Visszamondva")')
        page.wait_for_selector('.lcard:has-text("Makita DHP482")', state="detached")
        self.assertNotIn("Makita DHP482", [i.name for i in store.all()])
        self.assertEqual([d["text"] for d in deals.state()["deals"]], ["Bosch GSR 18V"])
        page.click("#job-deals")
        row = '.dealist li:has([data-deal-open="d1"])'      # a találat sora (a napló sorai nem)
        page.wait_for_selector(f"{row} [data-cancel-bought]")
        self.assertIn("Megvetted ✓", page.inner_text(row))
        page.click(f"{row} [data-cancel-bought]")
        page.click("#ask-yes")
        page.wait_for_selector(row, state="detached")
        self.wait_for(lambda: deals.state()["deals"] == [])
        self.assertEqual([i.name for i in store.all()], ["Nike Air Max 90"])

    def test_start_button_limits_and_instant_watch_on_a_phone(self):
        """Az Automata kereskedés fülön egy gomb: Indítás (minden elindul), utána Leállítás; a vételi limit; az azonnali
        figyelő felvétele, és a friss, megérő hirdetéseknél a Megvettem a találathoz visz."""
        instant, deals = self.h.app.work.instant, self.h.app.work.deals
        instant.allowed = lambda: False          # a teszt alatt ne töltsön le semmit
        deals._remember({"id": "q1", "url": "https://hardverapro.hu/apro/palit/friss.html", "text": "Palit RTX 3060 Ti",
                         "site": "Hardverapró", "platform": "hardverapro", "price": 52000, "median": 100000,
                         "profit": 36500, "at": time.time(), "quick": True})
        with instant._lock:
            instant._data["hits"] = [
                {"id": "h1", "alert": "a", "alert_name": "Videókártyák", "at": time.time() - 120, "site": "Hardverapró",
                 "platform": "hardverapro", "text": "Palit RTX 3060 Ti Dual Budapest", "title": "Palit RTX 3060 Ti",
                 "price": 52000, "url": "https://hardverapro.hu/apro/palit/friss.html", "cheap": True,
                 "reference": 100000, "profit": 36500, "deal": "q1"}]
        page = self.page
        page.set_viewport_size({"width": 390, "height": 844})
        page.click("#job-deals")
        page.wait_for_selector("#d-run-toggle")
        self.assertEqual(page.inner_text("#d-run-toggle"), "Indítás")
        self.assertEqual(page.locator("#d-enabled, #d-browse, #d-day-start, #di-enabled, #di-mode").count(), 0,
                         "nincs külön kapcsoló és napszak")
        page.click("#d-run-toggle")
        page.wait_for_selector('#d-run-toggle:has-text("Leállítás")')
        self.assertTrue(deals.state()["running"] and deals.state()["browse"]["on"] and instant.state()["enabled"])
        self.assertIn("Fut", page.inner_text(".runstate"))
        page.fill("#d-maxbuy", "30000")
        page.click("#d-limits")
        page.wait_for_selector('.toast:has-text("30 000 Ft fölött nem keresek")')
        self.assertEqual(deals.state()["max_buy"], 30000)
        page.fill("#di-target", "RTX 3080 Ti")
        page.fill("#di-max", "250000")
        page.click('#di-add button[type="submit"]')
        page.wait_for_selector('.dealist li:has-text("RTX 3080 Ti")')
        alert = instant.state()["alerts"][0]
        self.assertEqual((alert["query"], alert["max_price"]), ("RTX 3080 Ti", 250000))
        hits = page.inner_text("details:has-text('Friss hirdetések')")
        self.assertIn("Palit RTX 3060 Ti", hits)
        self.assertIn("2 perce", hits)
        page.click('details:has-text("Friss hirdetések") [data-deal-buy="q1"]')
        page.wait_for_selector("#d-paid")
        self.assertEqual(page.input_value("#d-paid"), "52000")
        wide = page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
        self.assertFalse(wide, "telefonon nincs vízszintes görgetés")
        page.click("#d-run-toggle")
        page.wait_for_selector('#d-run-toggle:has-text("Indítás")')
        self.assertFalse(deals.state()["running"] or instant.state()["enabled"])
        self.assertIn("Áll", page.inner_text(".runstate"))

    def test_starting_in_one_window_shows_in_the_other(self):
        """A telefonról indítod: a gép ablakában is rögtön Leállítás lesz (újratöltés nélkül), és fordítva."""
        self.h.app.work.instant.allowed = lambda: False
        other = self.browser.new_context(viewport={"width": 390, "height": 844}).new_page()
        other.goto(self.h.server.url)
        other.wait_for_selector('body[data-state="idle"]', state="attached", timeout=15000)
        other.click("#tab-work")
        other.click("#job-deals")
        other.wait_for_selector('#d-run-toggle:has-text("Indítás")')
        page = self.page
        page.click("#job-deals")
        page.click("#d-run-toggle")
        page.wait_for_selector('#d-run-toggle:has-text("Leállítás")')
        other.wait_for_selector('#d-run-toggle:has-text("Leállítás")', timeout=5000)
        other.click("#d-run-toggle")
        page.wait_for_selector('#d-run-toggle:has-text("Indítás")', timeout=5000)
        other.context.close()

    def test_deal_window_from_a_notification(self):
        """Az Android-alkalmazás értesítésére bökve (hamis alkalmazás-híddal) a találat ablaka nyílik meg: a címe az áru
        neve, alatta az ára, mennyiért mennek most és mennyit kereshetsz vele; a gomb kimásolja az eladónak szóló
        üzenetet, és megnyitja a hirdetést. A Szakértői tipp kimásolja a kérdést a hirdetés linkjével, és megnyitja a
        ChatGPT alkalmazást."""
        deals = self.h.app.work.deals
        self.h.app.work.instant.allowed = lambda: False
        listing = self.h.pages.url("katalogus.html") + "?hirdetes=palit"
        deals._remember({"id": "q1", "url": listing, "text": "Palit RTX 3060 Ti Dual Budapest 52 000 Ft",
                         "title": "Palit RTX 3060 Ti Dual", "site": "Hardverapró", "platform": "hardverapro",
                         "price": 52000, "median": 100000, "similar": 12, "profit": 31500, "at": time.time() - 150,
                         "low": 85000, "sites": ["Hardverapró", "Jófogás"],
                         "quick": True, "note": "Ugyanaz a kártya.", "explore": "azonnali figyelő: Videókártyák"})
        context = self.browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                                           user_agent="Mozilla/5.0 (Linux; Android 14) Chrome/141.0 Mobile JarvisAndroid/1")
        context.add_init_script("""(() => {
            let wanted = {deal: "q1"};
            window.JarvisApp = { pending() { const out = JSON.stringify(wanted); wanted = {}; return out; },
                                 copy(text) { window.__copied = text; },
                                 openChatGpt() { window.__chatgpt = (window.__chatgpt || 0) + 1; } };
          })();""")
        page = context.new_page()
        page.goto(self.h.server.url)
        page.wait_for_selector("#deal-dialog[open]", timeout=15000)
        self.assertEqual(page.inner_text("#deal-title"), "Palit RTX 3060 Ti Dual")
        body = page.inner_text("#deal-body")
        for part in ("52 000 Ft", "Most ennyiért mennek", "kb. 100 000 Ft (12 hasonló alapján, Hardverapró és Jófogás)",
                     "Ennyit kereshetsz vele", "kb. 31 500 Ft (ha kb. 85 000 Ft-ért eladod", "Ugyanaz a kártya.", "3 perce"):
            self.assertIn(part, body)
        self.assertEqual(page.inner_text(".msgpreview"), "Szia!\nMegvan még a termék?\nKöszi előre is\nÜdv, Zsolt")
        self.assertEqual(page.get_attribute("#deal-go", "href"), listing)
        with context.expect_page() as opened:
            page.click("#deal-go")
        self.assertEqual(opened.value.url, listing, "a hirdetés megnyílik")
        self.assertEqual(page.evaluate("window.__copied"), "Szia!\nMegvan még a termék?\nKöszi előre is\nÜdv, Zsolt")
        self.assertFalse(page.evaluate("document.getElementById('deal-dialog').open"))
        page.click('[data-deal-open="q1"]')
        page.wait_for_selector("#deal-dialog[open]")
        pages = len(context.pages)
        page.click("#deal-expert")
        self.assertEqual(page.evaluate("window.__chatgpt"), 1, "a ChatGPT alkalmazás nyílik meg")
        self.assertEqual(len(context.pages), pages, "nem a böngészőben")
        asked = page.evaluate("window.__copied")
        self.assertTrue(asked.startswith("Találtam egy jónak tűnő árut, ami jó befektetés lehetne, ha vétel után kicsit "
                                         "drágábban továbbárusítanám. Megnéznéd, hogy tényleg jó vétel lenne-e?"), asked)
        self.assertIn("Palit RTX 3060 Ti Dual\nÁra: 52 000 Ft (Hardverapró)\n", asked)
        self.assertTrue(asked.endswith(listing), "a végén a hirdetés linkje")
        self.assertFalse(page.evaluate("document.getElementById('deal-dialog').open"))
        page.click('[data-deal-open="q1"]')
        page.wait_for_selector("#deal-dialog[open]")
        page.click("#deal-bought")
        page.wait_for_selector("#d-paid")
        self.assertEqual(page.input_value("#d-paid"), "52000")
        page.click('summary:has-text("Üzenet az eladónak")')
        page.fill("#d-message", "Szia! Megvan még?")
        page.click("#d-message-save")
        page.wait_for_selector('.toast:has-text("ezt másolom ki")')
        self.assertEqual(deals.state()["message"], "Szia! Megvan még?")
        context.close()

    def test_flags_filter_last_check_and_a_false_flag(self):
        store = self.h.store
        item = self.h.ready_item(("vinted", "hardverapro", "jofogas"), price=5000)
        store.mark(item.id, "vinted", "feltöltve", url="https://www.vinted.hu/items/1234567-zara")
        store.mark(item.id, "hardverapro", "feltöltve", url="https://hardverapro.hu/apro/zara/friss.html")
        store.mark(item.id, "jofogas", "feltöltve", url="https://www.jofogas.hu/zara_1.htm")
        store.update(item.id, user=True, title="Zara póló, újszerű")
        when = time.time() - 3600
        store.set_check(item.id, "vinted", "sold", when=when, detail="„Eladva” jelzés az oldalán")
        store.set_check(item.id, "hardverapro", "live", when=when)
        store.set_check(item.id, "jofogas", "live", when=when, detail="Timeout 30000ms exceeded")
        other = self.h.ready_item()
        page = self.page
        card = f'.card[data-id="{item.id}"]'
        page.wait_for_selector(f'{card} .flag:has-text("Vinted: elkeltnek látszik")')
        flags = page.inner_text(f"{card} .flags")
        self.assertIn("A hirdetésben még a régi cím: Vinted, Hardverapró, Jófogás", flags)
        self.assertIn("ma került fel", page.inner_text(f"{card} .facts"), "mióta van fent")
        page.click('[data-filter="check"]')
        page.wait_for_selector(f'.card[data-id="{other.id}"]', state="detached")
        self.assertIn("1", page.inner_text('[data-filter="check"] .count'))
        self.open_item(item.id)
        stamp = page.evaluate("(t) => new Date(t * 1000).toLocaleString('hu-HU', { month: 'short', day: 'numeric', "
                              "hour: '2-digit', minute: '2-digit' })", when)
        vinted, hardverapro = '.platform:has-text("Vinted")', '.platform:has-text("Hardverapró")'
        self.assertIn(f"Ellenőrizve: {stamp}, „Eladva” jelzés az oldalán.", page.inner_text(vinted))
        self.assertIn(f"Ellenőrizve: {stamp}, fent van.", page.inner_text(hardverapro))
        self.assertIn("Cím eltér: a hirdetésben még „Zara fekete póló, L”.", page.inner_text(hardverapro))
        self.assertIn("de nem nyílt meg (Timeout 30000ms exceeded); holnap újra megnézem.",
                      page.inner_text('.platform:has-text("Jófogás")'))
        page.click(f'{vinted} [data-action="dismiss"]')
        page.wait_for_selector('.toast:has-text("Ezt a jelzést többé nem hozom fel")')
        page.wait_for_selector(f"{vinted} .pstatus.on")
        entry = store.get(item.id).platforms["vinted"]
        self.assertEqual((entry["check"], entry["check_dismissed"]), ("live", "sold"))
        self.assertEqual(page.locator('[data-action="dismiss"]').count(), 0)
        self.assertNotIn("elkeltnek látszik", page.inner_text("#w-detail"))

    def test_research_breakdown_and_auctions(self):
        item = self.h.ready_item(price=4500)
        similar = [{"site": "Vinted", "text": f"Zara póló {n}", "price": 4000 + 100 * n,
                    "url": f"https://www.vinted.hu/items/{n}"} for n in range(5)]
        similar.append({"site": "Vatera", "text": "Zara póló licit", "price": 3000, "url": "https://www.vatera.hu/1",
                        "auction": True})
        self.h.store.set_research(item.id, {
            "query": "Zara póló", "at": time.time(), "count": 30, "kept": 25, "similar": similar, "errors": [],
            "dropped": {"kereső": 2, "más termék": 1, "ismétlődő": 1, "kevésbé illő": 1},
            "min": 3000, "max": 4400, "p25": 4000, "median": 4200, "p75": 4300, "n": 6, "confidence": "bizonytalan",
            "outliers": 1, "suggested": 4200, "reason": "Ennyiért mennek."})
        self.page.wait_for_selector(f'.card[data-id="{item.id}"]')
        self.open_item(item.id)
        text = self.page.inner_text(".research")
        self.assertIn("30 találat (5 kiszűrve: 2 kereső vagy csere, 1 más termék, 1 ismétlődő, 1 kevésbé illő "
                      "(a 25 legjobban illő maradt)), ebből 6 hasonló:", text)
        self.assertIn("A hasonlók középső fele: 4 000 Ft – 4 300 Ft, középérték 4 200 Ft (6 ár alapján, bizonytalan: "
                      "kevés vagy nagyon eltérő ár). 1 kilógó árat kihagytam.", text)
        self.assertIn("Zara póló licit – 3 000 Ft (licit)", text)
        self.assertEqual(self.page.locator(".research .auction").count(), 1, "csak a licites sor")

    def test_drift_message_after_editing_a_posted_listing(self):
        item = self.h.ready_item(("vinted",), price=5000)
        self.h.store.mark(item.id, "vinted", "feltöltve", url="https://www.vinted.hu/items/1234567-zara")
        page = self.page
        page.wait_for_selector(f'.card[data-id="{item.id}"]')
        self.open_item(item.id)
        self.assertEqual(page.locator(".drift-note").count(), 0)
        page.fill("#f-price", "4500")
        page.press("#f-price", "Tab")
        note = page.wait_for_selector(".drift-note")
        self.assertIn("Csak itt írtam át; a Vinteden még 5 000 Ft.", note.inner_text())
        self.assertIn("ott kézzel kell átírni", note.inner_text())
        self.assertIn("Ár eltér: a hirdetésben még 5 000 Ft, itt 4 500 Ft.",
                      page.inner_text('.platform:has-text("Vinted")'))
        page.fill("#f-price", "5000")              # vissza a fent lévő árra: nincs mit mondani
        page.press("#f-price", "Tab")
        page.wait_for_selector(".drift-note", state="detached")

    def test_price_confirmation_button(self):
        item = self.h.ready_item(price=9000, size="")
        self.h.store.set_pending(item.id, {"kind": "price", "suggested": 4500, "confirm": 3,
                                           "question": "3 Ft-ra gondoltál? Ha igen, mondd, hogy igen."})
        self.page.wait_for_selector('[data-price="confirmed"]:has-text("Igen: 3 Ft")')
        self.assertEqual(self.page.locator('.card [data-price]').count(), 3, "Igen, Marad, Legyen")
        self.h.fake.chat_script = [extracted(**{**SHIRT, "size": ""})]   # utána a méretre kérdez rá
        self.page.click('[data-price="confirmed"]')
        self.page.wait_for_selector('.card .ask p:has-text("még kell: méret (Vinted)")')
        self.assertEqual(self.h.store.get(item.id).price, 3, "az Igen: a visszakérdezett összeg")

    def test_notification_button_opens_the_sold_dialog(self):
        item = self.h.ready_item(("vinted", "hardverapro"))
        store = self.h.store
        store.mark(item.id, "vinted", "feltöltve", url="https://www.vinted.hu/items/1234567-zara")
        store.mark(item.id, "hardverapro", "feltöltve", url="https://hardverapro.hu/apro/zara/friss.html")
        calls = []
        self.h.app.submit_work = lambda *args, **kwargs: calls.append((args, kwargs))
        page = self.page
        self.h.app.notices.add("sold", "Elkelt? Zara póló", "Zara póló: a Vinteden eladottnak látszik.", item=item.id,
                               platform="vinted", state="sold",
                               actions=[{"action": "sold", "where": "vinted", "delist": True,
                                         "label": "Eladva itt, vedd le a többiről"}])
        self.h.app.notices.add("error", "Hirdetések ellenőrzése: Vatera", "Be kell jelentkezni.", platform="vatera",
                               actions=[{"action": "login", "platform": "vatera", "label": "Bejelentkezés a böngészőmben"}])
        page.wait_for_selector('#notices-unread:has-text("2")')
        page.click("#notices")
        page.wait_for_selector("#notices-dialog[open] .notices li.new")
        self.assertIn("Zara póló: a Vinteden eladottnak látszik.", page.inner_text("#notices-body"))
        self.wait_for(lambda: self.h.app.notices.unread_count() == 0)
        self.assertEqual(page.inner_text("#notices-unread"), "", "megnyitva olvasott")
        page.click('#notices-body button:has-text("Eladva itt, vedd le a többiről")')
        page.wait_for_selector("#sold-dialog[open]")
        self.assertEqual(page.get_attribute("body", "data-mode"), "work")
        self.assertTrue(page.is_checked('#sold-where input[value="vinted"]'), "ahol elkeltnek látszik")
        self.assertTrue(page.is_checked("#sold-delist"))
        self.assertFalse(store.get(item.id).sold, "magától nem jelöl meg semmit")
        page.click("#sold-no")
        self.assertEqual(calls, [], "a Mégse után nem vesz le semmit")
        self.assertFalse(store.get(item.id).sold)
        page.click("#notices")
        page.click('#notices-body button:has-text("Eladva itt, vedd le a többiről")')
        page.wait_for_selector("#sold-dialog[open]")
        page.click("#sold-yes")
        page.wait_for_selector('.toast:has-text("Jarvis leveszi innen: Hardverapró")')
        self.assertEqual([(args, kwargs["platforms"]) for args, kwargs in calls],
                         [(("delist", [item.id], ""), ["hardverapro"])], "a jóváhagyás után, a többi oldalról")
        self.assertTrue(store.get(item.id).sold)
        calls.clear()
        page.click("#notices")
        page.click('#notices-body button:has-text("Bejelentkezés a böngészőmben")')
        page.wait_for_selector('.toast:has-text("Megnyitom a Jarvis böngészőjében")')
        self.assertEqual([args for args, _ in calls], [("login", [], "vatera")])

    def test_dossier_button_on_the_listing_page(self):
        item = self.h.ready_item()
        work = self.h.app.work
        self.h.store.mark(item.id, "vinted", "hiba", note="Nem találom a kategóriát.")
        name = work.dossiers.create(task="post", platform="vinted", item=self.h.store.get(item.id),
                                    reason="Nem találom a kategóriát.", mode="modell")
        self.h.store.attach_dossier(item.id, "vinted", name)
        self.page.wait_for_selector(f'.card[data-id="{item.id}"]')
        self.open_item(item.id)
        button = '.platform:has-text("Vinted") [data-action="dossier"]'
        self.assertEqual(self.page.locator('[data-action="dossier"]').count(), 1, "csak ott, ahol elakadt")
        with mock.patch("jarvis_core.gui.open_folder", return_value=False) as opened:
            self.page.click(button)
            toast = self.page.wait_for_selector('.toast:has-text("A hibacsomag itt van")')
        self.assertIn(name, toast.inner_text(), "ha az Intéző nem nyílik meg, megmondja, hol van")
        opened.assert_called_once_with(work.dossiers.path(name))
        shutil.rmtree(work.dossiers.path(name))
        self.page.click(button)
        self.page.wait_for_selector('.toast:has-text("már nincs meg")')

    def test_photos_on_the_listing_page_survive_a_refresh(self):
        item = self.h.ready_item()
        self.page.wait_for_selector(f'.card[data-id="{item.id}"]')
        self.open_item(item.id)
        with self.page.expect_file_chooser() as chooser:
            self.page.click('[data-action="pickphotos"]')
        # Amíg a fájlválasztó nyitva van, a lap frissül (pl. Jarvis dolgozik rajta);
        # a kiválasztott fotó ettől még nem veszhet el.
        self.h.store.set_busy(item.id, "Gondolkodom…")
        self.page.wait_for_selector('.detail .busy:has-text("Gondolkodom")')
        self.h.store.set_busy(item.id, "")
        chooser.value.set_files(str(self.h.photo))
        self.wait_for(lambda: len(self.h.store.get(item.id).photos) == 2)
        self.page.wait_for_selector(".detail .photo:nth-child(2) img")
        self.page.click('.detail .photo:nth-child(2) [data-action="cover"]')
        self.wait_for(lambda: self.h.store.get(item.id).photos == ["02.jpg", "01.jpg"])

    def test_dropped_photos_go_to_the_new_listing(self):
        b64 = base64.b64encode(jpeg()).decode()
        self.page.evaluate("""(b64) => {
            const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
            const dt = new DataTransfer();
            dt.items.add(new File([bytes], "ledobott.jpg", { type: "image/jpeg" }));
            const area = document.getElementById("w-area");
            area.dispatchEvent(new DragEvent("dragenter", { dataTransfer: dt, bubbles: true }));
            area.dispatchEvent(new DragEvent("drop", { dataTransfer: dt, bubbles: true, cancelable: true }));
        }""", b64)
        self.page.wait_for_selector("#w-new-photos .photo img")
        self.assertEqual(self.h.store.all(), [], "csak a Mehet gombbal jön létre a hirdetés")
        self.page.click('#w-new-photos [data-unpick="0"]')
        self.page.wait_for_selector("#w-new-photos .photo", state="detached")

    def test_work_chat_and_mode_memory(self):
        self.h.fake.chat_script = [reply("Még nincs tárgyad.")]
        self.page.fill("#text", "Hány tárgyam van?")
        self.page.press("#text", "Enter")
        self.page.wait_for_selector("#w-log .msg.jarvis")
        self.assertIn("Még nincs tárgyad.", self.page.inner_text("#w-log"))
        self.assertIn("Munka menü", self.h.fake.chat_bodies()[0]["messages"][0]["content"])
        self.assertEqual(self.page.locator("#log .msg.jarvis").count(), 0, "a Beszélgetés naplója tiszta marad")
        self.page.reload()
        self.page.wait_for_selector('body[data-mode="work"]', state="attached", timeout=10000)
        self.assertEqual(self.h.app.mode, "work", "a hangos kérések is a Munka menübe mennek")
        self.page.click("#tab-chat")
        self.page.wait_for_selector('body[data-mode="chat"]', state="attached")
        self.wait_for(lambda: self.h.app.mode == "chat")


if __name__ == "__main__":
    unittest.main(verbosity=2)
