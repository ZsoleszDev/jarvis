"""Böngésző nélküli, gyors olvasás: a Hardverapró és a Jófogás találati oldalai és hirdetései sima letöltéssel.

Ez a két oldal a szerveren rajzolja ki a találatokat, így egy találati oldal egyetlen letöltés (kb. egy másodperc), a
böngésző nélkül: nem kell hozzá ablak, egér és görgetés, és közben a böngésző a Vintedet vagy a Vaterát nézheti
(párhuzamosan). Az elemzés ugyanaz, mint a böngészőben (browser.LISTINGS_JS): minden linktől felfelé az első doboz,
amiben ár van, a hirdetés kártyája; ha egy doboz egy másikat is tartalmaz, az csak a lista. Ha így semmi, és az oldalon
van szerkesztett adat (JSON-LD ItemList), abból.

Biztonság: oldalanként egyszerre egy kérés, két kérés között legalább GAP másodperc (véletlenszerűen), óránként
legfeljebb HOURLY. Ha az oldal tilt (403, 429, robot-ellenőrzés), egy órára (ismétlésnél egyre hosszabban, legfeljebb
egy napra) leáll azon az oldalon, és nem próbálja megkerülni. Ha a letöltött oldalon nem talál hirdetést (megváltozott,
vagy csak JavaScripttel rajzolja ki), azt is jelzi (FeedError „empty”), és egy ideig nem is próbálja: a hívó ilyenkor a
böngészőt használja, mint eddig. Csak olvas: nem küld űrlapot, nem jelentkezik be, sütit csak a saját munkamenetében tart.
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
import urllib.parse
from collections import deque
from html.parser import HTMLParser
from typing import Any, Callable

import requests

from .web import MAX_DOWNLOAD_BYTES, USER_AGENT, _decode

FAST_SITES = ("hardverapro", "jofogas")
GAP = (8.0, 16.0)          # két kérés között ugyanazon az oldalon (másodperc, véletlenszerűen)
HOURLY = 90                # óránként legfeljebb ennyi kérés oldalanként
TIMEOUT = 15.0
BLOCK_FIRST = 3600         # tiltás után ennyi szünet (másodperc); ismétlésnél a duplája, legfeljebb BLOCK_MAX
BLOCK_MAX = 86400
EMPTY_PAUSE = 6 * 3600     # ha egymás után EMPTY_LIMIT-szer nem talál hirdetést, ennyi ideig a böngésző marad
EMPTY_LIMIT = 3
MAX_ROWS = 40
# A hirdetések címe az oldalon (ha ilyen link is van a kártyák között, csak ezek számítanak; ha egy sem, mind).
LISTING_URL = {"hardverapro": re.compile(r"/apro/[^/]+/"), "jofogas": re.compile(r"_\d+\.htm")}
_PRICE = r"(\d{1,3}(?:[   .]\d{3})+|\d{2,7})(?:[,.]\d{1,2})?\s*(?:Ft|HUF|forint)(?![a-zá-ű])"
PRICE_RE = re.compile(_PRICE, re.IGNORECASE)
FIXED_RE = re.compile(r"(?:villámár|fix ?ár|azonnal megvehető|buy it now)\D{0,4}" + _PRICE, re.IGNORECASE)
AUCTION_RE = re.compile(r"(licit|aukció|árverés|jelenlegi ár|kikiáltási ár|\bbids?\b|auction)", re.IGNORECASE)
PROFILE_RE = re.compile(r"/(member|members|user|users|profil|profile|seller|felhasznalo)(/|$|\?)", re.IGNORECASE)
CHALLENGE_RE = re.compile(r"captcha|cf-chl|challenge-platform|just a moment|checking your browser|datadome|"
                          r"perimeterx|px-captcha|robot-ellenőrzés|nem vagy robot", re.IGNORECASE)
BLOCKED_STATUS = {401, 403, 429, 503}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
NO_TEXT = {"script", "style", "noscript", "template", "svg", "head"}
CHROME = {"header", "nav", "footer"}
CHROME_ROLES = {"banner", "navigation", "contentinfo"}
STRUCK = {"s", "del", "strike"}
# Egy hirdetés oldalán (page): ezekben nincs a hirdetés szövege (menük, gombok, linkek, mezők).
PAGE_SKIP = CHROME | {"aside", "a", "button", "input", "select", "textarea", "option", "label", "form"}
_HIDDEN_CLASS = re.compile(r"(^|\s)(hidden|d-none|sr-only|visually-hidden|invisible)(\s|$)")


class FeedError(Exception):
    """Nem sikerült böngésző nélkül: kind = blocked (tilt), busy (most nem lehet: szünet vagy óránkénti keret), empty
    (nincs hirdetés a letöltött oldalon), off (ezt az oldalt nem így olvassuk), network, http."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


# --- egy egyszerű HTML-fa (a stdlib HTMLParser-rel) -------------------------------------------------------

class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "_text")

    def __init__(self, tag: str, attrs: dict[str, str], parent: Node | None):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children: list[Node | str] = []
        self._text: str | None = None

    def text(self) -> str:
        """A látható szöveg (mint az innerText, a szkriptek nélkül), egy sorban; egyszer számolja ki."""
        if self._text is None:
            parts = [child if isinstance(child, str) else child.text() for child in self.children
                     if isinstance(child, str) or child.tag not in NO_TEXT]
            self._text = clean(" ".join(parts))
        return self._text

    def text_without_struck(self) -> str:
        """A szöveg az áthúzott részek (régi ár) nélkül."""
        if self._struck():
            return ""
        parts = [child if isinstance(child, str) else child.text_without_struck() for child in self.children
                 if isinstance(child, str) or child.tag not in NO_TEXT]
        return clean(" ".join(parts))

    def _struck(self) -> bool:
        return self.tag in STRUCK or "line-through" in self.attrs.get("style", "").replace(" ", "")

    def contains(self, other: Node) -> bool:
        node = other.parent
        while node is not None:
            if node is self:
                return True
            node = node.parent
        return False

    def hidden(self) -> bool:
        style = self.attrs.get("style", "").replace(" ", "").lower()
        return ("hidden" in self.attrs or self.attrs.get("aria-hidden") == "true" or "display:none" in style
                or "visibility:hidden" in style or bool(_HIDDEN_CLASS.search(self.attrs.get("class", ""))))

    def chrome(self) -> bool:
        return self.tag in CHROME or self.attrs.get("role", "") in CHROME_ROLES


def clean(text: str) -> str:
    return " ".join(str(text or "").split())


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {}, None)
        self.stack: list[Node] = [self.root]
        self.anchors: list[Node] = []
        self.ld_json: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag == "a" and node.attrs.get("href"):
            self.anchors.append(node)
        if tag == "title":
            self._in_title = True
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        for index in range(len(self.stack) - 1, 0, -1):     # a rosszul lezárt elemeket is lezárja
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        node = self.stack[-1]
        if self._in_title:
            self.title += data
        if node.tag == "script" and "ld+json" in node.attrs.get("type", ""):
            self.ld_json.append(data)
        node.children.append(data)


def parse(html: str) -> _TreeBuilder:
    builder = _TreeBuilder()
    try:
        builder.feed(html)
        builder.close()
    except Exception:  # noqa: BLE001 - egy hibás oldal se állítsa le: ami addig összeállt, az marad
        pass
    return builder


def _amount(text: str) -> int:
    return int(re.sub(r"\D", "", text) or 0)


def parse_listings(html: str, base_url: str, platform: str = "") -> list[dict[str, Any]]:
    """Egy találati oldal hirdetései: [{text, price, url, auction?}], mint a böngészőben (LISTINGS_JS)."""
    tree = parse(html)
    cards: dict[int, dict[str, Any]] = {}
    for anchor in tree.anchors:
        href = urllib.parse.urljoin(base_url, anchor.attrs.get("href", ""))
        if not href.startswith(("http://", "https://")):
            continue
        node: Node | None = anchor
        skip = False
        while node is not None:                  # a fejléc, a menü és a lábléc linkjei nem hirdetések
            if node.chrome():
                skip = True
                break
            node = node.parent
        if skip:
            continue
        node, found = anchor, None
        for _depth in range(7):
            if node is None or node.tag in ("body", "#document"):
                break
            text = node.text()
            if len(text) > 600:
                break
            if PRICE_RE.search(text) or PRICE_RE.search(anchor.attrs.get("title", "")):
                found = node
                break
            node = node.parent
        if found is None:
            continue
        label = clean(anchor.attrs.get("title") or anchor.attrs.get("aria-label") or anchor.text())
        score = (-5 if PROFILE_RE.search(href) else 0) + (2 if anchor.attrs.get("title") else 0) + \
            min(len(label), 120) / 60
        known = cards.get(id(found))
        if known is None or score > known["score"]:
            cards[id(found)] = {"node": found, "href": href, "score": score, "label": label}
    nodes = [card["node"] for card in cards.values()]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in cards.values():
        if any(other is not card["node"] and card["node"].contains(other) for other in nodes):
            continue                              # egy nagyobb doboz (a lista), nem egy hirdetés
        if card["href"] in seen:
            continue
        text = card["node"].text_without_struck()
        match = FIXED_RE.search(text) or PRICE_RE.search(text) or PRICE_RE.search(card["label"])
        if not match:
            continue
        price = _amount(match.group(1))
        if not 0 < price < 10_000_000:
            continue
        seen.add(card["href"])
        label = card["label"]
        summary = f"{label} | {text}" if len(label) > 15 and label not in text else text
        entry: dict[str, Any] = {"text": summary[:240], "price": price, "url": card["href"]}
        if 3 < len(label) <= 160 and not PRICE_RE.search(label):
            entry["title"] = label                # a hirdetés címe magában (az értesítésbe)
        if not FIXED_RE.search(text) and AUCTION_RE.search(text):
            entry["auction"] = True
        out.append(entry)
        if len(out) >= MAX_ROWS:
            break
    pattern = LISTING_URL.get(platform)
    if pattern is not None and any(pattern.search(e["url"]) for e in out):
        out = [e for e in out if pattern.search(e["url"])]
    return out or _ld_listings(tree.ld_json, base_url)


def _ld_listings(blocks: list[str], base_url: str) -> list[dict[str, Any]]:
    """Tartalék: a szerkesztett adat (JSON-LD ItemList) hirdetései."""
    out: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        if value.get("@graph"):
            walk(value["@graph"])
        for element in value.get("itemListElement") or []:
            item = element.get("item") if isinstance(element, dict) and isinstance(element.get("item"), dict) \
                else element
            if not isinstance(item, dict):
                continue
            offers = item.get("offers")
            offers = offers[0] if isinstance(offers, list) and offers else offers
            try:
                price = int(float(str((offers or {}).get("price") or "0").replace(" ", "").replace(",", ".")))
            except (TypeError, ValueError, AttributeError):
                continue
            url = str(item.get("url") or (element.get("url") if isinstance(element, dict) else "") or "")
            name = clean(str(item.get("name") or ""))
            if price > 0 and url and name:
                out.append({"text": name[:240], "price": price, "url": urllib.parse.urljoin(base_url, url)})

    for block in blocks:
        try:
            walk(json.loads(block))
        except ValueError:
            continue
    return out[:MAX_ROWS]


def parse_page(html: str, url: str) -> dict[str, Any]:
    """Egy hirdetés oldala, mint a böngészőben (LISTING_PAGE_JS): a cím, a fő tartalom szövege a menük, gombok, linkek
    és mezők nélkül, a rövid szövegdarabok külön (egy „Eladva” címke ilyen), és a gombok, linkek feliratai. A rejtett
    elemeket (hidden, display:none) kihagyja."""
    tree = parse(html)
    root = _find(tree.root, lambda n: n.tag == "main" or n.attrs.get("role") == "main") or \
        _find(tree.root, lambda n: n.tag == "body") or tree.root
    parts: list[str] = []
    short: list[str] = []
    total = 0

    def walk(node: Node) -> None:
        nonlocal total
        for child in node.children:
            if total >= 4000:
                return
            if isinstance(child, str):
                text = clean(child)
                if text:
                    parts.append(text)
                    total += len(text) + 1
                    if len(text) <= 40 and len(short) < 200:
                        short.append(text)
            elif child.tag not in NO_TEXT and child.tag not in PAGE_SKIP and not child.chrome() and not child.hidden():
                walk(child)

    walk(root)
    labels = []
    for anchor in tree.anchors:
        label = clean(anchor.text() or anchor.attrs.get("aria-label", ""))
        if label and len(label) <= 40:
            labels.append(label)
        if len(labels) >= 150:
            break
    return {"title": clean(tree.title), "url": url, "text": " ".join(parts)[:4000], "short": short, "labels": labels,
            "captcha": False}


def _find(node: Node, wanted: Callable[[Node], bool]) -> Node | None:
    stack = [node]
    while stack:
        current = stack.pop()
        if wanted(current):
            return current
        stack.extend(reversed([c for c in current.children if isinstance(c, Node)]))
    return None


# --- letöltés oldalanként, tempóval ------------------------------------------------------------------------

class SiteFetcher:
    """A gyors (böngésző nélküli) letöltések oldalanként: egyszerre egy kérés, szünetekkel és óránkénti kerettel, a
    tiltás és a sikertelen elemzés megjegyzésével. Több szálról is használható (az automata kereskedés köre, az
    azonnali figyelő)."""

    def __init__(self, sites: tuple[str, ...] = FAST_SITES, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, rng: random.Random | None = None,
                 get: Callable[..., Any] | None = None, gap: tuple[float, float] = GAP, hourly: int = HOURLY,
                 domains: dict[str, tuple[str, ...]] | None = None):
        self.sites = tuple(sites)
        # oldal → a saját domainjei: csak ezekre megy (egy hirdetésben talált más cím a böngészős útra marad)
        self.domains = domains
        self.clock, self.sleep = clock, sleep
        self.rng = rng or random.Random()
        self.gap, self.hourly = gap, hourly
        self._get = get                     # tesztekhez: (url, headers, timeout) → válasz; alapból requests
        self._sessions: dict[str, requests.Session] = {}
        self._locks = {site: threading.Lock() for site in self.sites}
        self._state_lock = threading.RLock()
        self._next: dict[str, float] = {}            # oldal → mikortól jöhet a következő kérés
        self._recent: dict[str, deque] = {site: deque() for site in self.sites}
        self._blocked: dict[str, dict[str, Any]] = {}   # oldal → {until, reason, strikes}
        self._empty: dict[str, int] = {}             # oldal → hányszor egymás után nem talált hirdetést
        self._empty_until: dict[str, float] = {}
        self.requests = 0                            # összesen (a felületnek)

    def handles(self, platform: str) -> bool:
        return platform in self.sites

    def usable(self, platform: str) -> bool:
        """Olvashatja-e most ezt az oldalt böngésző nélkül (nincs tiltva, és legutóbb talált rajta hirdetést)."""
        if platform not in self.sites:
            return False
        now = self.clock()
        with self._state_lock:
            return (float((self._blocked.get(platform) or {}).get("until") or 0) <= now
                    and self._empty_until.get(platform, 0.0) <= now)

    def ready(self, platform: str) -> bool:
        """Mehet-e most rögtön a következő kérés ezen az oldalon (várakozás nélkül)."""
        now = self.clock()
        with self._state_lock:
            recent = self._recent.get(platform, deque())
            while recent and now - recent[0] > 3600:
                recent.popleft()
            return self.usable(platform) and self._next.get(platform, 0.0) <= now and len(recent) < self.hourly \
                and not self._locks[platform].locked()

    def status(self) -> dict[str, dict[str, Any]]:
        """A felületnek: oldalanként tilt-e (meddig, miért), és az utóbbi órában hány kérés volt."""
        now = self.clock()
        with self._state_lock:
            out = {}
            for site in self.sites:
                blocked = self._blocked.get(site) or {}
                out[site] = {"blocked_until": blocked.get("until") if float(blocked.get("until") or 0) > now else None,
                             "reason": blocked.get("reason", "") if float(blocked.get("until") or 0) > now else "",
                             "browser_only": self._empty_until.get(site, 0.0) > now,
                             "last_hour": sum(1 for t in self._recent[site] if now - t <= 3600)}
            return out

    def listings(self, url: str, platform: str, check: Callable[[], None] = lambda: None) -> list[dict[str, Any]]:
        """Egy találati oldal hirdetései böngésző nélkül. FeedError, ha nem sikerült (a hívó a böngészővel próbálja)."""
        final, html = self._fetch(url, platform, check)
        rows = parse_listings(html, final, platform)
        with self._state_lock:
            if rows:
                self._empty.pop(platform, None)
            else:
                self._empty[platform] = self._empty.get(platform, 0) + 1
                if self._empty[platform] >= EMPTY_LIMIT:     # egymás után többször semmi: inkább a böngésző
                    self._empty_until[platform] = self.clock() + EMPTY_PAUSE
                    self._empty.pop(platform, None)
        if not rows:
            raise FeedError("A letöltött oldalon nem találtam hirdetést.", "empty")
        return rows

    def page(self, url: str, platform: str, check: Callable[[], None] = lambda: None) -> dict[str, Any]:
        """Egy hirdetés oldala böngésző nélkül (mint a browser.look): cím, szöveg, rövid darabok, feliratok."""
        final, html = self._fetch(url, platform, check)
        return parse_page(html, final)

    def _fetch(self, url: str, platform: str, check: Callable[[], None]) -> tuple[str, str]:
        if platform not in self.sites or not self._ours(url, platform):
            raise FeedError("Ezt az oldalt a böngészővel olvasom.", "off")
        if not self.usable(platform):
            raise FeedError("Ez az oldal most csak a böngészővel megy (tiltott, vagy nem találtam rajta hirdetést).",
                            "blocked")
        with self._locks[platform]:              # oldalanként egyszerre egy kérés
            while True:
                check()
                now = self.clock()
                with self._state_lock:
                    recent = self._recent[platform]
                    while recent and now - recent[0] > 3600:
                        recent.popleft()
                    if len(recent) >= self.hourly:
                        raise FeedError("Ebben az órában már sokat kérdeztem ezt az oldalt.", "busy")
                    wait = self._next.get(platform, 0.0) - now
                if wait <= 0:
                    break
                self.sleep(min(wait, 1.0))
            with self._state_lock:
                self._recent[platform].append(self.clock())
                self.requests += 1
            try:
                final, status, html = self._download(url, platform)
            finally:
                with self._state_lock:
                    self._next[platform] = self.clock() + self.rng.uniform(*self.gap)
        if status in BLOCKED_STATUS or (status < 400 and len(html) < 20000 and CHALLENGE_RE.search(html[:20000])
                                        and not PRICE_RE.search(html)):
            self._block(platform, f"robot-ellenőrzés vagy tiltás ({status})")
            raise FeedError("Az oldal robot-ellenőrzést kér, vagy tilt: egy ideig csak a böngészővel nézem.", "blocked")
        if status >= 400:
            raise FeedError(f"Az oldal hibát adott ({status}).", "http")
        with self._state_lock:
            if platform in self._blocked:
                self._blocked[platform]["until"] = 0       # újra megy: a következő tiltás megint rövidebb
                self._blocked[platform]["strikes"] = max(0, int(self._blocked[platform].get("strikes", 1)) - 1)
        return final, html

    def _ours(self, url: str, platform: str) -> bool:
        if self.domains is None:
            return True
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").removeprefix("www.")
        return parts.scheme in ("http", "https") and any(host == d or host.endswith("." + d)
                                                         for d in self.domains.get(platform, ()))

    def _download(self, url: str, platform: str) -> tuple[str, int, str]:
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "hu-HU,hu;q=0.9,en;q=0.7",
                   "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
        try:
            if self._get is not None:
                response = self._get(url, headers=headers, timeout=TIMEOUT)
            else:
                session = self._sessions.setdefault(platform, requests.Session())
                response = session.get(url, headers=headers, timeout=TIMEOUT, stream=True)
            with response:
                body = b""
                for chunk in response.iter_content(65536):
                    body += chunk
                    if len(body) > MAX_DOWNLOAD_BYTES:
                        break
                declared = str(response.encoding or "").lower()
                # ha a válasz nem mondja meg (a requests ilyenkor latin-1-et feltételez), az oldal maga dönt
                text = body.decode(declared, errors="replace") if declared and declared != "iso-8859-1" \
                    else _decode(body)
                return str(response.url or url), int(response.status_code), text
        except requests.RequestException as exc:
            raise FeedError(f"Nem sikerült letölteni ({type(exc).__name__}).", "network") from None

    def _block(self, platform: str, reason: str) -> None:
        with self._state_lock:
            entry = self._blocked.setdefault(platform, {"strikes": 0})
            entry["strikes"] = int(entry.get("strikes") or 0) + 1
            entry["until"] = self.clock() + min(BLOCK_MAX, BLOCK_FIRST * 2 ** (entry["strikes"] - 1))
            entry["reason"] = reason

    def close(self) -> None:
        for session in self._sessions.values():
            session.close()
        self._sessions.clear()

    def blocked_until(self, platform: str) -> float:
        with self._state_lock:
            return float((self._blocked.get(platform) or {}).get("until") or 0)
