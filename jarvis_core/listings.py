"""A fent lévő hirdetések háttérellenőrzése: elkelt-e vagy eltűnt-e (lejárt, törölték) valamelyik.

Modell nélkül, és csak olvas: naponta egyszer néhány hirdetést nyit meg véletlen sorrendben, emberi
tempóban (betöltés, egy pillantás és egy kis görgetés, olvasás), és a lap szövegéből dönt. Semmire nem
kattint. Egy hirdetést naponta legfeljebb egyszer néz meg. Ha egy oldal robot-ellenőrzést (captchát) kér,
vagy be kell jelentkezni, az egész kört abbahagyja, és egyszer szól (az értesítés gombja megnyitja az oldalt
Jarvis böngészőjében, ott te lépsz be). Ha valahol elkeltnek vagy eltűntnek látszik egy hirdetés, értesítést
küld (kind: sold): a kártyán (és az értesítésben) az „Eladva itt, vedd le a többiről” gombbal, jóváhagyás
után, a többi oldalról is leveszi. Magától nem jelöl meg és nem vesz le semmit. Ha egy jelzésre azt mondtad,
hogy nem igaz (ItemStore.dismiss_check), ugyanazt a jelzést annál a hirdetésnél többé nem hozza fel.
"""
from __future__ import annotations

import random
import re
import time
import urllib.parse
from typing import Any, Callable

from .browser import BrowserError
from .replay import norm_text
from .selling import ON_SITE, POSTED, Item, ItemStore, Platform, listing_key
from .vinted import _GONE_RE, logged_out

LIVE, SOLD, GONE, BLOCKED, UNKNOWN = "live", "sold", "gone", "blocked", "unknown"
CHECK_SECONDS = 24 * 3600    # egy hirdetést naponta legfeljebb egyszer néz meg
# A modellnek és a felületnek: mit látott a háttérellenőrzés.
CHECK_WORDS = {SOLD: "a háttérellenőrzés szerint elkelt", GONE: "a háttérellenőrzés szerint már nincs fent"}

# „Eladva” címke a hirdetés oldalán (rövid, önálló szövegdarab, nem gomb): a Vinteden „Sold” / „Reserved”.
_SOLD_RE = re.compile(r"\b(sold(?! by)|eladva|elkelt|reserved|foglalt|lefoglalva)\b")
# Lejárt vagy lezárt hirdetés (a Vinted „nem található” szavai mellé).
_ENDED_RE = re.compile(r"(hirdetes lejart|lejart a hirdetes|lejart hirdetes|mar nem aktiv|hirdetes inaktiv|"
                       r"inaktiv hirdetes|hirdetes lezarult|mar nem vasarolhato)")
_LOGIN_RE = re.compile(r"/(login|signin|sign_in|sign-in|bejelentkez\w*|belepes)(/|$)")


def classify(page: dict[str, Any], url: str, title: str = "", platform: str = "") -> tuple[str, str]:
    """Egy megnyitott hirdetés állapota a lap adataiból (browser.look_listing): (állapot, miből látszik).
    LIVE: fent van; SOLD: „Eladva” / „Foglalt” címke; GONE: máshova irányít, vagy „nem található” oldal;
    BLOCKED: captcha, vagy be kell jelentkezni (ilyenkor a kör abbamarad)."""
    final = str(page.get("url") or "")
    if page.get("captcha"):
        return BLOCKED, "robot-ellenőrzést (captchát) kér"
    if _LOGIN_RE.search(urllib.parse.urlsplit(final).path.lower()):
        return BLOCKED, "be kell jelentkezni"
    if platform == "vinted" and logged_out([{"label": label} for label in page.get("labels") or []]):
        return BLOCKED, "ki vagy jelentkezve"
    if final and listing_key(final) != listing_key(url):
        return GONE, "a hirdetés címe máshova irányít"
    for text in page.get("short") or []:
        if _SOLD_RE.search(norm_text(str(text))):
            return SOLD, f"„{str(text)[:40]}” jelzés az oldalán"
    heading = norm_text(str(page.get("title") or ""))
    head = heading + " " + norm_text(str(page.get("text") or "")[:600])
    own = norm_text(title)[:30]
    if (_GONE_RE.search(head) or _ENDED_RE.search(head)) and not (own and own in heading):
        return GONE, "az oldala szerint már nem található"
    return LIVE, ""


class ListingChecker:
    """Naponta néhány fent lévő hirdetés oldala, csak olvasva (lásd a modul leírását)."""

    def __init__(self, store: ItemStore, platforms: dict[str, Platform], browser: Callable[[], Any],
                 clock: Callable[[], float] = time.time, rng: random.Random | None = None):
        self.store = store
        self.platforms = platforms
        self.browser = browser
        self.clock = clock
        self.rng = rng or random.Random()

    def due(self, now: float | None = None) -> list[tuple[Item, str]]:
        """(tárgy, oldal) párok, amelyeket egy napja nem néztünk meg: fent vannak, ismerjük a webcímüket, és a
        tárgy nincs eladottnak jelölve. A frissen feltöltöttet is csak egy nap múlva nézi meg."""
        now = self.clock() if now is None else now
        out = []
        for item in self.store.all():
            if item.sold:
                continue
            for key, entry in item.platforms.items():
                if key not in self.platforms or entry.get("status") != POSTED or not entry.get("url"):
                    continue
                last = max(float(entry.get("at") or 0), float(entry.get("checked_at") or 0))
                if now - last >= CHECK_SECONDS:
                    out.append((item, key))
        return out

    def run(self, limit: int, check: Callable[[], None] = lambda: None,
            notify: Callable[..., None] = lambda *a, **k: None) -> str:
        """Egy kör: legfeljebb `limit` esedékes hirdetés, véletlen sorrendben. `check` a lépések között
        megszakíthatja (a felhasználó közbeszólt), `notify` az értesítés. Visszaadja a rövid eredményt."""
        picks = self.due()
        self.rng.shuffle(picks)
        picks = picks[:max(0, int(limit))]
        if not picks:
            return "Nincs ma megnézendő hirdetés."
        browser = self.browser()
        counts = {LIVE: 0, SOLD: 0, GONE: 0, UNKNOWN: 0}
        for n, (item, key) in enumerate(picks):
            check()
            if n:
                browser.pace.think()     # a következő hirdetés előtt, ahogy az ember is
            try:
                item = self.store.get(item.id)      # közben változhatott (pl. eladottnak jelölted)
            except Exception:  # noqa: BLE001 - közben törölted
                continue
            entry = item.platforms.get(key) or {}
            if item.sold or entry.get("status") != POSTED or not entry.get("url"):
                continue
            site = self.platforms[key]
            try:
                page = browser.look_listing(entry["url"])
            except BrowserError as exc:
                counts[UNKNOWN] += 1      # ma már nem próbálja újra (naponta egy látogatás)
                self.store.set_check(item.id, key, str(entry.get("check") or UNKNOWN), self.clock(),
                                     str(exc).splitlines()[0][:160])
                continue
            state, detail = classify(page, entry["url"], str(entry.get("posted_title") or item.title), key)
            if state == BLOCKED:
                # be kell jelentkezni, vagy ki vagy jelentkezve: belépés; captchánál csak megnyitja (te oldod meg)
                label = "Bejelentkezés a böngészőmben" if "jelentkez" in detail else "Megnyitás a böngészőmben"
                notify("error", f"Hirdetések ellenőrzése: {site.name}",
                       f"A hirdetéseid ellenőrzését abbahagytam: a(z) {site.name} {detail}. Nézd meg a "
                       "böngészőablakomban; holnap újra megpróbálom.", platform=key,
                       actions=[{"action": "login", "platform": key, "label": label}])
                return f"Abbahagytam: {site.name} – {detail}."
            if state in (SOLD, GONE) and entry.get("check_dismissed") == state:
                state, detail = LIVE, ""      # ezt a jelzést már nem igaznak jelölted: nem hozza fel újra
            before = entry.get("check")
            self.store.set_check(item.id, key, state, self.clock(), detail)
            counts[state] += 1
            if state in (SOLD, GONE) and before != state:
                self._notify(notify, self.store.get(item.id), key, state, detail)
        parts = [f"{counts[LIVE]} fent van"]
        for state, word in ((SOLD, "elkeltnek látszik"), (GONE, "már nincs fent"), (UNKNOWN, "nem nyílt meg")):
            if counts[state]:
                parts.append(f"{counts[state]} {word}")
        return f"{len(picks)} hirdetést néztem meg: " + ", ".join(parts) + "."

    def _notify(self, notify: Callable[..., None], item: Item, key: str, state: str, detail: str) -> None:
        site = self.platforms[key]
        where = ON_SITE.get(key) or f"a(z) {site.name} oldalon"
        others = [self.platforms[k].name for k, e in item.platforms.items()
                  if k != key and k in self.platforms and e.get("status") == POSTED]
        if state == SOLD:
            title, text = f"Elkelt? {item.name}", f"{item.name}: {where} eladottnak látszik ({detail})."
        else:
            title, text = (f"Már nincs fent? {item.name}",
                           f"{item.name}: {where} már nincs fent a hirdetés ({detail}). Ha csak lejárt vagy "
                           "törölték, a kártyán a „Már levettem” gombbal jelöld meg.")
        if others:
            text += (f" Ha ott kelt el, a kártyán az „Eladva itt, vedd le a többiről” gombbal (jóváhagyás után) "
                     f"innen is leveszem: {', '.join(others)}.")
        else:
            text += " Ha tényleg elkelt, a kártyán az „Eladva itt” gombbal jelöld meg."
        label = "Eladva itt, vedd le a többiről" if others else "Eladva itt"
        notify("sold", title, text, item=item.id, platform=key, state=state,
               actions=[{"action": "sold", "where": key, "delist": bool(others), "label": label}])
