"""Jarvis saját böngészője (Playwright): látja az oldalt, és kezelni is tudja.

Minden lépés után "oldalnézetet" ad vissza a modellnek: a képernyőn látható szöveget és
a kezelhető elemek számozott listáját. A modell a számokra hivatkozva kattint és gépel.
A böngésző látható ablakban fut, külön profillal, így a saját böngésződet nem zavarja.

Biztonság:
- jelszót és bankkártyaadatot soha nem ír be,
- vásárlás, küldés, törlés jellegű gombokra kattintás előtt jóváhagyást kér,
- keresőmezőn kívül bármilyen űrlap elküldése (Enter) előtt is jóváhagyást kér.
"""
from __future__ import annotations

import base64
import functools
import re
import time
import urllib.parse
from typing import Any, Callable

from . import web
from .config import BrowserCfg, data_path, resolve_path
from .humanize import LONG_TEXT, Pace
from .textutil import fold

VISIBLE_TEXT_CHARS = 1500
# A modell nélküli kitöltés letapogatásának számai (az oldalnézeté 1-től, a visszajátszásé 9000-től megy).
# Bőven kell: a lenyíló listák gyakran a lap legvégére kerülnek, a fejléc és a lábléc linkjei után.
SCAN_BASE, SCAN_SPAN, SCAN_MAX = 5000, 3000, 1500
# Robotvédelmi köztes oldal (pl. Cloudflare "Just a moment..."): egy valódi böngészőben ez
# többnyire magától továbbenged pár másodperc alatt. Ha captchát kér, azt nem kerüljük meg.
_CHALLENGE_RE = re.compile(
    r"just a moment|checking your browser|attention required|verify (that )?you are (a )?human|"
    r"are you a robot|captcha|cf-chl|egy pillanat|ellenőrizzük, hogy|nem vagy robot|access denied",
    re.IGNORECASE)
CHALLENGE_WAIT = 12.0      # ennyit vár, hogy a köztes oldal magától továbbengedjen
RESTARTED_NOTE = ("A böngésző közben bezárult vagy összeomlott (pl. elfogyott a gép memóriája); újraindítottam, "
                  "az előző oldal már nincs nyitva.")
# Ennél régebben indult, be nem fejezett adatkérésre nem várunk (pl. egy hosszan nyitva tartott kapcsolat,
# vagy egy kérés, aminek a vége elveszett, mert közben átnavigált az oldal).
STUCK_REQUEST_SECONDS = 15.0
# Az oldal adatkéréseit (XHR, fetch, dokumentum, szkript) a Chromium hálózati eseményeiből számoljuk.
_TRACKED_TYPES = {"XHR", "Fetch", "Document", "Script"}
_CLOSED_RE = re.compile(r"has been closed|target closed|browser closed|browser has disconnected|"
                        r"connection closed", re.IGNORECASE)
# A Playwright-meghajtó (node) elhalt, pl. mert elfogyott a gép memóriája: ilyenkor a context.close() örökre
# várna a válaszára, ezért csak a Playwrightot állítjuk le.
_DRIVER_GONE_RE = re.compile(r"connection closed|reading from the driver", re.IGNORECASE)
_PROFILE_BUSY_RE = re.compile(r"ProcessSingleton|user data directory is already in use")


def is_closed_error(exc: BaseException) -> bool:
    """A böngészőt (vagy az ablakát) közben bezárták, vagy összeomlott (pl. elfogyott a gép memóriája, és a
    böngésző vagy a Playwright-meghajtó leállt): újra kell indítani. Csak az üzenet első sorát nézzük: a
    hibaüzenetünk után az oldalnézet jöhet, és abban az oldal szövege is (pl. „This item has been closed”)."""
    first = (str(exc).strip().splitlines() or [""])[0]
    return type(exc).__name__ == "TargetClosedError" or bool(_CLOSED_RE.search(first))


def _restarts_after_crash(method: Callable[..., Any]) -> Callable[..., Any]:
    """Ha a művelet közben a böngésző (vagy a Playwright-meghajtó) bezárult vagy összeomlott, elengedjük, és a
    következő lépés újraindítja (_ensure). Különben a halott lap minden további lépésnél ugyanazt a hibát adná.
    Ha a műveleten belül már újraindult (pl. az oldalnézet), azt nem zárjuk be újra."""
    @functools.wraps(method)
    def wrapper(self: "BrowserController", *args: Any, **kwargs: Any) -> Any:
        starts = self._starts
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            if self._starts == starts and self.running and is_closed_error(exc):
                self._release(exc)
            raise
    return wrapper

# Ezekre a szavakra (ékezet nélkül, szó elején) kattintás előtt jóváhagyás kell.
_RISKY_RE = re.compile(
    r"\b(fizet|vasarl|megrendel|rendel|checkout|penztar|buy|purchase|pay|order|torol|torl|torles|"
    r"delete|remove|elkuld|kuld|send|post|poszt|kozzete|publish|feliratkoz|subscribe|"
    r"leiratkoz|unsubscribe|regisztr|register|sign up|megerosit|confirm|jovahagy|utal|"
    r"transfer|lemond)"
)
# Hirdetés közzététele (Vinted: "Feltöltés"); a fotófeltöltő gomb viszont nem kockázatos.
_PUBLISH_RE = re.compile(r"\b(feltolt|tolts fel|upload|hirdetes ?felad|feladom|meghirdet|list it|list item)")
_PHOTO_WORDS_RE = re.compile(r"(foto|kep|photo|image|picture|kepek)")
_SEARCH_RE = re.compile(r"(keres|search|query)")

KIND_NAMES = {
    "link": "link", "button": "gomb", "input:submit": "gomb", "input:button": "gomb",
    "input:reset": "gomb", "input:image": "gomb", "input:text": "szövegmező",
    "input:search": "keresőmező", "searchbox": "keresőmező", "input:email": "e-mail mező",
    "input:password": "jelszómező", "input:number": "számmező", "input:tel": "telefonszám-mező",
    "input:url": "webcím-mező", "input:date": "dátummező",
    "input:checkbox": "jelölőnégyzet", "checkbox": "jelölőnégyzet", "input:radio": "választógomb",
    "radio": "választógomb", "textarea": "szövegdoboz", "textbox": "szövegmező",
    "select": "lenyíló lista", "combobox": "lenyíló lista", "tab": "fül", "menuitem": "menüpont",
    "option": "opció", "switch": "kapcsoló", "summary": "lenyitható rész", "input:file": "fájlfeltöltő",
}

# Az oldalon futó JavaScript: megszámozza a kezelhető elemeket és kigyűjti a látható szöveget.
# A kezelhető elemek felismerése (közös az oldalnézetnek, a visszajátszásnak és a felvételnek).
_JS_HELPERS = r"""
  const SELECTOR = [
    'a[href]', 'button', 'input:not([type=hidden])', 'textarea', 'select', 'summary',
    '[role=button]', '[role=link]', '[role=tab]', '[role=menuitem]', '[role=checkbox]',
    '[role=radio]', '[role=option]', '[role=switch]', '[role=textbox]', '[role=searchbox]',
    '[role=combobox]', '[contenteditable=""]', '[contenteditable="true"]', '[onclick]'
  ].join(',');
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    if (typeof el.checkVisibility === 'function' &&
        !el.checkVisibility({opacityProperty: true, visibilityProperty: true})) return false;
    const r = el.getBoundingClientRect();
    return r.width >= 2 && r.height >= 2;
  };
  const nameOf = (el) => {
    const aria = clean(el.getAttribute('aria-label'));
    if (aria) return aria;
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = clean(by.split(/\s+/).map(id => {
        const n = document.getElementById(id); return n ? n.innerText : '';
      }).join(' '));
      if (t) return t;
    }
    const tag = el.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
      if (el.labels && el.labels.length) {
        const t = clean(Array.from(el.labels).map(l => l.innerText).join(' '));
        if (t) return t;
      }
      if (tag === 'INPUT' && ['submit', 'button', 'reset'].includes(el.type)) {
        return clean(el.value) || el.type;
      }
      return clean(el.getAttribute('placeholder') || el.getAttribute('title') ||
                   el.getAttribute('name') || '');
    }
    const text = clean(el.innerText);
    if (text) return text;
    const img = el.querySelector('img[alt]');
    if (img && clean(img.alt)) return clean(img.alt);
    return clean(el.getAttribute('title') || el.getAttribute('alt') || '');
  };
  const kindOf = (el) => {
    const role = el.getAttribute('role');
    if (role) return role;
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') return 'input:' + (el.type || 'text');
    if (tag === 'a') return 'link';
    if (el.isContentEditable) return 'textbox';
    return tag;
  };
  const sensitive = (el) => {
    const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
    const key = ((el.getAttribute('name') || '') + ' ' + (el.id || '')).toLowerCase();
    return el.type === 'password' || ac.includes('password') || /(^|\s)cc-/.test(ac) ||
           /card|cvv|cvc|iban|kartya|kártya/.test(key);
  };
  const fold = (s) => clean(s).normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
  const fieldOf = (el) => ((el.getAttribute('name') || '') + ' ' + (el.id || '')).trim().slice(0, 40);
"""

SNAPSHOT_JS = r"""
(opts) => {
  const MAX = opts.max || 60;
  const TEXT_CHARS = opts.textChars || 1500;
  const vh = window.innerHeight;
""" + _JS_HELPERS + r"""

  document.querySelectorAll('[data-jarvis-id]').forEach(el => el.removeAttribute('data-jarvis-id'));
  const CHROME = 'header, nav, footer, aside, [role=banner], [role=navigation], [role=contentinfo]';
  const candidates = [];
  const seen = new Set();
  for (const el of document.querySelectorAll(SELECTOR)) {
    if (el.disabled) continue;
    const r = el.getBoundingClientRect();
    if (r.bottom < -vh * 0.25 || r.top > vh * 1.5) continue;
    if (!visible(el)) continue;
    const name = nameOf(el);
    const kind = kindOf(el);
    const isField = /^(input|textbox|searchbox|combobox|textarea|select)/.test(kind);
    if (!name && !isField) continue;
    // ugyanaz a link többször (kép + cím ugyanarra a termékre): elég egyszer
    const key = kind + '|' + name + '|' + (el.getAttribute('href') || '');
    if (!isField && seen.has(key)) continue;
    seen.add(key);
    // a mezők (pl. a fejlécben lévő kereső) mindig fontosak, nem számítanak menünek
    candidates.push({el, name, kind, inView: r.bottom > 0 && r.top < vh,
                     chrome: !isField && !!el.closest(CHROME)});
  }
  // Sorrend: fő tartalom a képernyőn, fő tartalom alatta, menük a képernyőn, a többi menü.
  // Így egy webshopban a termékek és szűrők nem szorulnak ki a fejléc linkjei miatt.
  const rank = (c) => (c.chrome ? 2 : 0) + (c.inView ? 0 : 1);
  candidates.sort((a, b) => rank(a) - rank(b));
  const MAX_CHROME = Math.max(10, Math.floor(MAX / 3));
  let chromeCount = 0;
  const chosen = [];
  for (const c of candidates) {
    if (chosen.length >= MAX) break;
    if (c.chrome && chromeCount >= MAX_CHROME) continue;
    if (c.chrome) chromeCount++;
    chosen.push(c);
  }
  // A modell a lap sorrendjében könnyebben tájékozódik, ezért visszarendezzük.
  chosen.sort((a, b) => (a.el.compareDocumentPosition(b.el) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1);

  const elements = [];
  for (const c of chosen) {
    const el = c.el;
    const id = elements.length + 1;
    el.setAttribute('data-jarvis-id', String(id));
    const item = {id, kind: c.kind, label: c.name.slice(0, 100), inView: c.inView};
    if (el.tagName === 'A') item.href = (el.getAttribute('href') || '').slice(0, 120);
    const noValue = ['password', 'checkbox', 'radio', 'submit', 'button', 'reset', 'image', 'file'];
    if ((el.tagName === 'INPUT' && !noValue.includes(el.type)) || el.tagName === 'TEXTAREA') {
      item.value = (el.value || '').slice(0, 60);
    }
    if (el.type === 'checkbox' || el.type === 'radio') item.checked = !!el.checked;
    if (el.getAttribute('aria-checked')) item.checked = el.getAttribute('aria-checked') === 'true';
    if (el.isContentEditable) item.editable = true;
    if (el.tagName === 'SELECT') {
      item.select = true;
      const chosen = el.options[el.selectedIndex];
      item.value = chosen ? clean(chosen.text).slice(0, 60) : '';
      item.options = Array.from(el.options).slice(0, 12).map(o => clean(o.text).slice(0, 40));
    }
    if (sensitive(el)) item.sensitive = true;
    const fieldName = (el.getAttribute('name') || '') + ' ' + (el.id || '');
    if (fieldName.trim()) item.field = fieldName.trim().slice(0, 40);
    elements.push(item);
  }

  // A képernyőn látható szöveg: előbb a fő tartalom, a menük szövege csak a maradék helyre.
  const root = document.body || document.documentElement;
  const shown = new Map();
  const collect = (wantChrome, budget) => {
    const parts = [];
    let total = 0;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const p = node.parentElement;
        if (!p || ['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE'].includes(p.tagName)) {
          return NodeFilter.FILTER_REJECT;
        }
        return clean(node.textContent) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      }
    });
    while (total < budget && walker.nextNode()) {
      const p = walker.currentNode.parentElement;
      if (!!p.closest(CHROME) !== wantChrome) continue;
      let ok = shown.get(p);
      if (ok === undefined) {
        const r = p.getBoundingClientRect();
        ok = r.bottom > 0 && r.top < vh && r.width > 0 && r.height > 0 &&
             (typeof p.checkVisibility !== 'function' || p.checkVisibility({visibilityProperty: true}));
        shown.set(p, ok);
      }
      if (!ok) continue;
      const t = clean(walker.currentNode.textContent);
      parts.push(t);
      total += t.length + 1;
    }
    return parts.join(' ').slice(0, budget);
  };
  const mainText = collect(false, TEXT_CHARS);
  const rest = TEXT_CHARS - mainText.length;
  const chromeText = rest > 100 ? collect(true, Math.min(rest, 300)) : '';
  const doc = document.scrollingElement || document.documentElement;
  return {
    title: document.title,
    url: location.href,
    text: mainText + (chromeText ? ' | Menü: ' + chromeText : ''),
    scrollY: Math.round(window.scrollY),
    scrollHeight: Math.round(doc ? doc.scrollHeight : vh),
    viewportHeight: vh,
    elements,
  };
}
"""


# Keresési találati oldal (Vinted, Jófogás…) hirdetései: minden hirdetéskártya szövege, ára és
# címe. Nem oldalspecifikus: a linkektől felfelé haladva megkeresi a legkisebb elemet, amelyben
# ár is van (ez a kártya), és kártyánként egyszer veszi fel.
LISTINGS_JS = r"""
() => {
  const PRICE = /(\d{1,3}(?:[   .]\d{3})+|\d{2,7})(?:[,.]\d{1,2})?\s*(?:Ft|HUF|forint)(?![a-zá-ű])/i;
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const SKIP = 'header, nav, footer, [role=banner], [role=navigation], [role=contentinfo]';
  const PROFILE = /\/(member|members|user|users|profil|profile|seller|felhasznalo)(\/|$|\?)/i;
  const cards = new Map();
  for (const a of document.querySelectorAll('a[href]')) {
    if (a.closest(SKIP)) continue;
    const href = a.href || '';
    if (!/^https?:/.test(href)) continue;
    let node = a, found = null;
    for (let depth = 0; depth < 7 && node && node !== document.body; depth++) {
      const text = clean(node.innerText);
      if (text.length > 600) break;
      if (PRICE.test(text) || PRICE.test(a.getAttribute('title') || '')) { found = node; break; }
      node = node.parentElement;
    }
    if (!found) continue;
    const label = clean(a.getAttribute('title') || a.getAttribute('aria-label') || a.innerText);
    const score = (PROFILE.test(href) ? -5 : 0) + (a.getAttribute('title') ? 2 : 0) +
                  Math.min(label.length, 120) / 60;
    const known = cards.get(found);
    if (!known || score > known.score) cards.set(found, {node: found, href, score, label});
  }
  // ha egy "kártya" egy másikat is tartalmaz, az csak egy nagyobb doboz: a kisebb a hirdetés
  const nodes = Array.from(cards.keys());
  const out = [];
  const seen = new Set();
  for (const card of cards.values()) {
    if (nodes.some(other => other !== card.node && card.node.contains(other))) continue;
    if (seen.has(card.href)) continue;
    const text = clean(card.node.innerText);
    const m = text.match(PRICE) || card.label.match(PRICE);
    if (!m) continue;
    const price = parseInt(m[1].replace(/[^\d]/g, ''), 10);
    if (!(price > 0 && price < 10000000)) continue;
    seen.add(card.href);
    const summary = (card.label.length > 15 && !text.includes(card.label)) ? card.label + ' | ' + text : text;
    out.push({text: summary.slice(0, 240), price, url: card.href});
    if (out.length >= 40) break;
  }
  return out;
}
"""


# Visszajátszás: egy korábban rögzített elem megkeresése az egész oldalon (nem csak a látható
# részen), ugyanazzal az elemfelismeréssel, amit a modell is lát. A legjobb egyezést megjelöli.
LOCATE_JS = r"""
(want) => {
""" + _JS_HELPERS + r"""
  const norm = (s) => fold(s).replace(/\d+/g, '#').replace(/\s+/g, ' ').trim();
  const CHOICE = /^(radio|option|menuitem|menuitemradio|menuitemcheckbox|checkbox|switch|tab|link|button|summary|input:radio|input:checkbox|input:button|input:submit)$/;
  const group = (k) => {
    if (want.loose && CHOICE.test(k)) return 'choice';
    if (/^(button|input:submit|input:button|input:image|input:reset)$/.test(k)) return 'button';
    if (/^(input:text|input:search|searchbox|textbox|input:email|input:tel|input:number|input:url|textarea|combobox)$/.test(k)) return 'text';
    if (/^(radio|input:radio)$/.test(k)) return 'radio';
    if (/^(checkbox|input:checkbox)$/.test(k)) return 'checkbox';
    return k;
  };
  const wantGroup = group(want.kind || '');
  const wantLabel = fold(want.label || '');
  const wantNorm = norm(want.label || '');
  const wantField = want.field || '';
  const found = [];
  for (const el of document.querySelectorAll(SELECTOR)) {
    if (el.disabled) continue;
    const kind = kindOf(el);
    if (group(kind) !== wantGroup) continue;
    if (kind !== 'input:file' && !visible(el)) continue;
    const name = nameOf(el);
    const field = fieldOf(el);
    const label = fold(name);
    let score = 0;
    if (wantLabel) {
      if (label === wantLabel) score += 4;
      else if (wantNorm && norm(name) === wantNorm) score += 3;
      else if (wantLabel.length >= 4 && label.length >= 4 &&
               (label.startsWith(wantLabel) || wantLabel.startsWith(label))) score += 1;
    }
    if (wantField && field) {
      if (field === wantField) score += 3;
      else if (norm(field) === norm(wantField)) score += 2;
    }
    if (want.href && (el.getAttribute('href') || '') === want.href) score += 1;
    if (score >= (want.minScore || 3)) found.push({el, score, name, kind, field});
  }
  if (!found.length) return {count: 0};
  const best = Math.max(...found.map((f) => f.score));
  const top = found.filter((f) => f.score === best);
  const pick = top[Math.min(Math.max(0, want.ordinal || 0), top.length - 1)];
  document.querySelectorAll('[data-jarvis-id="' + want.id + '"]').forEach((e) => e.removeAttribute('data-jarvis-id'));
  const el = pick.el;
  el.setAttribute('data-jarvis-id', String(want.id));
  const item = {id: want.id, kind: pick.kind, label: pick.name.slice(0, 100), inView: true};
  if (el.tagName === 'A') item.href = (el.getAttribute('href') || '').slice(0, 120);
  if (el.tagName === 'SELECT') {
    item.select = true;
    item.options = Array.from(el.options).slice(0, 40).map((o) => clean(o.text).slice(0, 60));
  }
  if (el.isContentEditable) item.editable = true;
  if (sensitive(el)) item.sensitive = true;
  if (pick.field) item.field = pick.field;
  return {count: top.length, score: best, item};
}
"""

# Modell nélküli kitöltés (vinted.py): az oldal ÖSSZES látható, kezelhető eleme (nem csak a képernyőn
# lévők, és nincs 60-as korlát), megszámozva, hogy a click/type_text használhassa őket. A számok külön
# tartományban vannak (SCAN_BASE-től), és minden letapogatás az előzőét törli.
SCAN_JS = r"""
(opts) => {
""" + _JS_HELPERS + r"""
  const BASE = opts.base, END = opts.base + opts.span;
  document.querySelectorAll('[data-jarvis-scan]').forEach((el) => {
    el.removeAttribute('data-jarvis-scan');
    const id = parseInt(el.getAttribute('data-jarvis-id') || '0', 10);
    if (id >= BASE && id < END) el.removeAttribute('data-jarvis-id');
  });
  const CHROME = 'header, nav, footer, [role=banner], [role=navigation], [role=contentinfo]';
  const vh = window.innerHeight;
  const noValue = ['password', 'checkbox', 'radio', 'submit', 'button', 'reset', 'image', 'file'];
  const out = [];
  for (const el of document.querySelectorAll(SELECTOR)) {
    if (out.length >= opts.max) break;
    if (el.disabled) continue;
    const kind = kindOf(el);
    if (kind !== 'input:file' && !visible(el)) continue;
    const name = nameOf(el);
    const isField = /^(input|textbox|searchbox|combobox|textarea|select)/.test(kind);
    if (!name && !isField) continue;
    const id = BASE + out.length;
    el.setAttribute('data-jarvis-id', String(id));
    el.setAttribute('data-jarvis-scan', '1');
    const r = el.getBoundingClientRect();
    const item = {id, kind, label: name.slice(0, 160), field: fieldOf(el), inView: r.bottom > 0 && r.top < vh};
    if (el.closest(CHROME)) item.chrome = true;
    if (el.getAttribute('aria-disabled') === 'true') item.disabled = true;
    if (el.tagName === 'A') item.href = (el.getAttribute('href') || '').slice(0, 120);
    if ((el.tagName === 'INPUT' && !noValue.includes(el.type)) || el.tagName === 'TEXTAREA') {
      item.value = (el.value || '').slice(0, 100);
      if (el.readOnly) item.readonly = true;
    }
    if (el.type === 'checkbox' || el.type === 'radio') item.checked = !!el.checked;
    const state = el.getAttribute('aria-checked') || el.getAttribute('aria-selected') || el.getAttribute('aria-pressed');
    if (state) item.checked = state === 'true';
    if (el.getAttribute('aria-expanded')) item.expanded = el.getAttribute('aria-expanded') === 'true';
    if (el.isContentEditable) item.editable = true;
    if (el.tagName === 'SELECT') {
      item.select = true;
      const chosen = el.options[el.selectedIndex];
      item.value = chosen ? clean(chosen.text).slice(0, 60) : '';
      item.options = Array.from(el.options).slice(0, 60).map((o) => clean(o.text).slice(0, 60));
    }
    if (sensitive(el)) item.sensitive = true;
    out.push(item);
  }
  return out;
}
"""

# Hibaüzenetek az űrlapon (pl. a Vinted: „Fill in colour to continue”), a közzététel után.
FORM_ERRORS_JS = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const shown = (el) => {
    if (typeof el.checkVisibility === 'function' && !el.checkVisibility({visibilityProperty: true})) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const out = [];
  const nodes = document.querySelectorAll('[role=alert], [aria-live=assertive], [class*=error i], [class*=invalid i], ' +
                                          '[data-testid*=error i]');
  for (const el of nodes) {
    if (out.length >= 6) break;
    if (!shown(el)) continue;
    const text = clean(el.innerText);
    if (text && text.length <= 200 && !out.some((t) => t.includes(text) || text.includes(t))) out.push(text);
  }
  return out;
}
"""

# Felvétel: hányadik az elem a hozzá hasonlók (ugyanolyan fajta és felirat) között az egész oldalon.
ORDINAL_JS = r"""
(id) => {
""" + _JS_HELPERS + r"""
  const target = document.querySelector('[data-jarvis-id="' + id + '"]');
  if (!target) return 0;
  const kind = kindOf(target);
  const label = fold(nameOf(target));
  let n = 0;
  for (const el of document.querySelectorAll(SELECTOR)) {
    if (el === target) return n;
    if (el.disabled || kindOf(el) !== kind) continue;
    if (kind !== 'input:file' && !visible(el)) continue;
    if (fold(nameOf(el)) === label) n++;
  }
  return 0;
}
"""

_SETTLE_JS = r"""
() => {
  const body = document.body;
  if (!body) return [0, true];
  const text = body.innerText;
  const loading = /(^|\s)(betöltés|töltés|loading)\s*(…|\.\.\.)/i.test(text.slice(0, 20000)) ||
                  !!document.querySelector('[aria-busy="true"]');
  return [text.length, loading];
}
"""


class BrowserError(Exception):
    """Érthető hibaüzenet böngészős műveletnél."""


class Covered(Exception):
    """Egy másik elem (nyitva maradt lenyíló lista, felugró ablak, süti-sáv) eltakarja, amire kattintana."""


UNCOVER_HINT = ("Előbb zárd be: válassz a nyitott listából, vagy kattints a felugró ablak gombjára (pl. „Elfogadom”, "
                "„Bezárás”, „Kész”), aztán próbáld újra.")


_BLOCKER_KINDS = (
    (re.compile(r"listbox|menu|combobox|dropdown|select|popover|tooltip", re.I), "egy nyitva maradt lenyíló lista"),
    (re.compile(r"dialog|modal|overlay|backdrop|cookie|consent|banner", re.I), "egy felugró ablak vagy sáv"),
)


def blocker_of(error: Any) -> str:
    """Ha a Playwright szerint egy másik elem eltakarja a célt („… intercepts pointer events”), annak rövid,
    a modellnek is érthető leírása; különben üres."""
    for line in str(error or "").splitlines():
        if "intercepts pointer events" not in line:
            continue
        tag = re.search(r"<(\w+)([^>]*)>", line)
        if tag is None:
            return "egy másik elem"
        attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', tag.group(2)))
        hints = " ".join(attrs.get(k, "") for k in ("role", "class", "id", "aria-modal"))
        kind = next((name for pattern, name in _BLOCKER_KINDS if pattern.search(hints)), f"egy <{tag.group(1)}> elem")
        end = line.find(" from <", tag.end())
        end = end if end >= 0 else line.find("intercepts pointer events")
        inner = re.sub(r"<[^>]*>", " ", line[tag.end():end])
        inner = " ".join(inner.replace("…", " ").split())
        name = attrs.get("aria-label") or attrs.get("title") or inner
        return kind + (f" („{name[:60]}”)" if name else "")
    return ""


def is_risky(label: str, link: bool = False) -> bool:
    """Kell-e jóváhagyás a kattintáshoz. `link`: sima link egy másik oldalra (pl. a „Hirdetésfeladás”
    link csak megnyitja az űrlapot, a közzététel gombja viszont kockázatos)."""
    folded = fold(label or "")
    if _RISKY_RE.search(folded):
        return True
    if link:
        return False
    return bool(_PUBLISH_RE.search(folded)) and not _PHOTO_WORDS_RE.search(folded)


def navigates(element: dict[str, Any]) -> bool:
    """Sima link-e (egy másik oldalt nyit meg, nem gomb és nem szkript)."""
    href = str(element.get("href") or "").strip().lower()
    return element.get("kind") == "link" and bool(href) and not href.startswith(("#", "javascript:"))


_NUMBER_TEXT_RE = re.compile(r"^[\d\s.,'+-]*\d[\d\s.,'+-]*(?:\s*(?:ft|huf|eur|€|\$))?$")


def same_value(value: str, text: str) -> bool:
    """Az áll-e a mezőben, amit beírtunk. Számnál csak a számjegyek számítanak, mert a mező
    tagolhatja vagy pénznemet tehet mellé (5000 → „5 000 Ft”); a „0500” viszont nem „500”."""
    wanted = " ".join(fold(text).split())
    got = " ".join(fold(value or "").split())
    if wanted == got:
        return True
    if wanted and _NUMBER_TEXT_RE.match(wanted):
        return re.sub(r"\D", "", wanted) == re.sub(r"\D", "", got)
    return False


def is_search_field(element: dict[str, Any]) -> bool:
    """Keresőmező-e (ott az Enter nem "elküldés", nem kell hozzá jóváhagyás)."""
    if element.get("kind") in ("input:search", "searchbox", "combobox"):
        return True
    words = fold(f"{element.get('label', '')} {element.get('field', '')}").split()
    return any(word == "q" or _SEARCH_RE.search(word) for word in words)


def _short(exc: BaseException | None) -> str:
    text = str(exc or "").strip().splitlines()
    return text[0][:200] if text else type(exc).__name__


def format_element(el: dict[str, Any]) -> str:
    text = f"[{el['id']}] {KIND_NAMES.get(el['kind'], el['kind'])}"
    if el.get("label"):
        text += f' "{el["label"]}"'
    href = el.get("href") or ""
    if href and not href.startswith(("javascript", "#")):
        text += f" -> {href}"
    if el.get("select"):
        text += f' (kiválasztva: "{el.get("value", "")}"'
        if el.get("options"):
            text += "; lehetőségek: " + ", ".join(el["options"])
        text += ")"
    elif el.get("value"):
        text += f' (érték: "{el["value"]}")'
    if "checked" in el:
        text += " [bejelölve]" if el["checked"] else " [nincs bejelölve]"
    if el.get("sensitive"):
        text += " (érzékeny adat, ezt nem töltöm ki)"
    if not el.get("inView", True):
        text += " (a látható részen kívül)"
    return text


def format_snapshot(data: dict[str, Any], note: str = "") -> str:
    lines = [web.UNTRUSTED_NOTE]
    if note:
        lines.append(note)
    lines.append(f"Oldal: {data.get('title') or '(cím nélkül)'}")
    lines.append(f"Cím: {data.get('url', '')}")
    height, view, pos = data.get("scrollHeight", 0), data.get("viewportHeight", 1), data.get("scrollY", 0)
    if height > view * 1.05:
        percent = min(100, round((pos + view) / height * 100))
        more = "lejjebb is van még tartalom" if pos + view < height - 5 else "az oldal alján vagyunk"
        lines.append(f"Görgetés: az oldal {percent}%-áig látszik ({more}).")
    lines += ["", "Látható szöveg:", data.get("text") or "(nincs)", ""]
    elements = data.get("elements") or []
    if elements:
        lines.append("Kezelhető elemek (a számukkal hivatkozz rájuk):")
        lines += [format_element(el) for el in elements]
    else:
        lines.append("Nincs kezelhető elem a látható részen.")
    return "\n".join(lines)


class BrowserController:
    CLICK_TIMEOUT_MS = 8000    # ennyit vár, hogy egy elem kattintható legyen
    PROBE_TIMEOUT_MS = 1500    # a próbakattintás (eltakarja-e valami) ennyit vár

    def __init__(self, cfg: BrowserCfg, allow_local: bool = False,
                 confirm: Callable[[str], bool] | None = None, read_max_chars: int = 6000):
        self.cfg = cfg
        self.allow_local = allow_local
        self.confirm = confirm or (lambda _prompt: False)
        self.read_max_chars = read_max_chars
        self._pw: Any = None
        self._context: Any = None
        self.page: Any = None
        self.engine = ""
        self.elements: dict[int, dict[str, Any]] = {}
        # Felvétel: minden sikeres lépés (megnyitás, kattintás, gépelés, fotó) ide is megy, ha be van
        # állítva; ebből tanulja meg a Jarvis egy feltöltés vagy levétel lépéseit.
        self.on_action: Callable[[dict[str, Any]], None] | None = None
        # Előre megadott jóváhagyás (pl. a levételt már jóváhagytad): (felirat, webcím) → igaz/hamis.
        self.approve_hook: Callable[[str, str], bool] | None = None
        self.capture_error = ""   # a hibacsomaghoz: miért nem készült képernyőkép (capture)
        self._locate_seq = 9000
        # Emberi tempó (humanize.py). Az `auto` igaz, amíg modell nélküli lépéssor fut (begyakorolt
        # lépések): akkor gondolkodási és olvasási szüneteket is tart, mert nincs modell, ami lassítana.
        self.pace = Pace(getattr(cfg, "human_pace", True), getattr(cfg, "pace", 1.0))
        self.pace.sleep = self._sleep
        self.auto = False
        self._mouse: tuple[float, float] | None = None
        self._starts = 0          # hányszor indult el a böngésző (lásd _restarts_after_crash)

    # --- indítás és leállítás ------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._context is not None

    def _start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserError("A böngészőhöz telepítsd: pip install playwright, "
                               "majd: playwright install chromium") from exc
        # A profil (sütik, bejelentkezések) az adatmappában van, így a frissítések után is megmarad.
        profile = data_path(self.cfg.profile_dir, "browser_profile")
        profile.mkdir(parents=True, exist_ok=True)
        options: dict[str, Any] = {
            "headless": bool(self.cfg.headless),
            "viewport": {"width": int(self.cfg.width), "height": int(self.cfg.height)},
            "locale": "hu-HU",
            "args": ["--disable-blink-features=AutomationControlled"],
            "ignore_default_args": ["--enable-automation"],
        }
        attempts: list[dict[str, Any]] = []
        if self.cfg.executable_path:
            attempts.append({"executable_path": str(resolve_path(self.cfg.executable_path))})
        elif self.cfg.channel:
            attempts.append({"channel": self.cfg.channel})
        attempts.append({})  # tartalék: a Playwright saját Chromiuma
        self._starts += 1
        self._pw = sync_playwright().start()
        last_error: Exception | None = None
        for round_no in range(3):
            for extra in attempts:
                try:
                    self._context = self._pw.chromium.launch_persistent_context(
                        str(profile), **options, **extra)
                    self.engine = extra.get("channel") or ("saját" if extra else "chromium")
                    break
                except Exception as exc:  # noqa: BLE001 - pl. nincs Edge, próbáljuk a következőt
                    last_error = exc
            # Egy összeomlás után a régi böngésző még pár pillanatig fogja a profilt: kicsit várunk rá.
            if self._context is not None or not _PROFILE_BUSY_RE.search(str(last_error)) or round_no == 2:
                break
            time.sleep(1.5)
        if self._context is None:
            self._pw.stop()
            self._pw = None
            message = str(last_error)
            if "playwright install" in message or "Executable doesn't exist" in message:
                raise BrowserError("Hiányzik a böngésző. Futtasd: playwright install chromium") from last_error
            if _PROFILE_BUSY_RE.search(message):
                raise BrowserError("A böngészőprofil már használatban van (fut egy másik Jarvis?).") from last_error
            raise BrowserError(f"A böngésző nem indult: {_short(last_error)}") from last_error
        pages = self._context.pages
        self.page = pages[0] if pages else self._context.new_page()

    def _ensure(self) -> Any:
        """Visszaadja az aktív lapot; ha a felhasználó bezárta az ablakot, újraindítja."""
        if self._context is not None:
            try:
                if self.page is None or self.page.is_closed():
                    pages = [p for p in self._context.pages if not p.is_closed()]
                    self.page = pages[-1] if pages else self._context.new_page()
                self._watch_network(self.page)
                return self.page
            except Exception as exc:  # noqa: BLE001 - bezárt böngésző
                self._release(exc)
        self._start()
        self._watch_network(self.page)
        return self.page

    def close(self, driver_gone: bool = False) -> None:
        """Bezárja a böngészőt. `driver_gone`: a Playwright-meghajtó már elhalt; ilyenkor a böngészőhöz nem
        szólunk (a válaszát örökre várnánk), csak a Playwrightot állítjuk le."""
        context = None if driver_gone else self._context
        for obj, method in ((context, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:  # noqa: BLE001
                    pass
        self._context = self._pw = self.page = None
        self.elements = {}

    def _release(self, exc: BaseException) -> None:
        """Elengedi a bezárult vagy összeomlott böngészőt: a következő lépés újraindítja (_ensure)."""
        first = (str(exc).strip().splitlines() or [""])[0]
        self.close(driver_gone=bool(_DRIVER_GONE_RE.search(first)))

    # --- segédek ------------------------------------------------------------------

    def _watch_network(self, page: Any) -> None:
        """Számolja az oldal folyamatban lévő adatkéréseit (a webshopok így töltik a találatokat).

        A Chromium saját hálózati eseményeiből (külön CDP-munkamenet), nem a Playwright request-eseményeiből:
        azoknál a Playwright minden kéréshez és válaszhoz Python-objektumot készít, és típusonként 10 000-et
        meg is tart. Egy hosszabb böngészésnél (pl. a Vinted egy oldala több száz kérés) ez több száz MB, és
        amíg a modell gondolkodik, az események feltorlódnak, majd egyszerre jönnek: egyszer MemoryError lett
        belőle. A CDP-eseményekből nem marad meg semmi, csak a kérés azonosítója, amíg tart."""
        if getattr(page, "_jarvis_watched", False):
            return
        page._jarvis_watched = True
        inflight: dict[str, float] = {}     # kérésazonosító -> mikor indult
        page._jarvis_inflight = inflight
        try:
            session = page.context.new_cdp_session(page)
            try:
                # A válaszok tartalmát ez a munkamenet nem őrzi meg, a feltöltött fotókat sem kéri vissza.
                session.send("Network.enable", {"maxTotalBufferSize": 0, "maxResourceBufferSize": 0,
                                                "maxPostDataSize": 0})
            except Exception:  # noqa: BLE001 - ha a böngésző ezeket nem ismeri, az alapbeállítással
                session.send("Network.enable")
        except Exception:  # noqa: BLE001 - pl. nem Chromium: akkor csak a szöveg megnyugvását figyeljük
            return

        def started(params: dict[str, Any]) -> None:
            if params.get("type") in _TRACKED_TYPES and params.get("requestId"):
                inflight[params["requestId"]] = time.monotonic()

        def finished(params: dict[str, Any]) -> None:
            inflight.pop(params.get("requestId"), None)

        session.on("Network.requestWillBeSent", started)
        session.on("Network.loadingFinished", finished)
        session.on("Network.loadingFailed", finished)
        page._jarvis_network = session

    @staticmethod
    def _network_busy(page: Any) -> bool:
        """Folyamatban van-e adatkérés az oldalon (a régóta függőket elengedi, lásd STUCK_REQUEST_SECONDS)."""
        inflight = getattr(page, "_jarvis_inflight", None)
        if not inflight:
            return False
        now = time.monotonic()
        for request_id, started in list(inflight.items()):
            if now - started > STUCK_REQUEST_SECONDS:
                inflight.pop(request_id, None)
        return bool(inflight)

    def _settle(self, page: Any, max_wait: float = 5.0) -> None:
        """Megvárja, amíg az oldal betölt és a tartalma megnyugszik.

        A modern webshopok (pl. Vinted) a találatokat a betöltés után, JavaScripttel töltik
        be. Ezért addig várunk, amíg nincs folyamatban adatkérés, az oldal szövege fél
        másodpercig nem változik, és nem látszik rajta "Betöltés…" jellegű felirat.
        Egy egyszerű oldalnál ez fél másodperc, egy lassan töltődő webshopnál több.
        """
        self._watch_network(page)
        for state, timeout in (("domcontentloaded", 10000), ("load", 4000)):
            try:
                page.wait_for_load_state(state, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                if is_closed_error(exc):
                    return            # bezárult vagy összeomlott: nincs mire várni
        previous, stable = -1, 0
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            try:
                page.wait_for_timeout(250)
                size, loading = page.evaluate(_SETTLE_JS)
            except Exception as exc:  # noqa: BLE001 - épp navigál
                if is_closed_error(exc):
                    return
                stable = 0
                continue
            busy = self._network_busy(page) or loading
            stable = stable + 1 if size == previous and size > 0 and not busy else 0
            previous = size
            if stable >= 2:
                return

    def _adopt_new_page(self, pages_before: int) -> bool:
        pages = [p for p in self._context.pages if not p.is_closed()]
        if len(pages) > pages_before:
            self.page = pages[-1]
            try:
                self.page.bring_to_front()
            except Exception:  # noqa: BLE001
                pass
            self._settle(self.page)
            return True
        return False

    def _element(self, number: Any) -> dict[str, Any]:
        try:
            n = int(number)
        except (TypeError, ValueError):
            raise BrowserError("Az elemet a számával add meg (pl. 12).") from None
        if n not in self.elements:
            raise BrowserError(f"Nincs {n}. számú elem a legutóbbi oldalnézetben.")
        return self.elements[n]

    def _locator(self, number: int) -> Any:
        return self.page.locator(f'[data-jarvis-id="{int(number)}"]').first

    def _fail(self, message: str) -> BrowserError:
        """Hibaüzenet friss oldalnézettel, hogy a modell újra tudjon tervezni."""
        try:
            return BrowserError(f"{message}\n\nFriss oldalnézet:\n{self.snapshot()}")
        except BrowserError:
            return BrowserError(message)

    # --- felvétel és visszajátszás --------------------------------------------------------

    def _emit(self, event: dict[str, Any]) -> None:
        if self.on_action is not None:
            try:
                self.on_action(event)
            except Exception:  # noqa: BLE001 - a felvétel hibája ne rontsa el a böngészést
                pass

    def _ordinal(self, el: dict[str, Any]) -> int:
        """Hányadik az elem az ugyanilyen feliratúak között (csak felvételkor számoljuk)."""
        if self.on_action is None or self.page is None:
            return 0
        try:
            return int(self.page.evaluate(ORDINAL_JS, int(el["id"])) or 0)
        except Exception:  # noqa: BLE001
            return 0

    @staticmethod
    def describe(el: dict[str, Any], ordinal: int = 0) -> dict[str, Any]:
        """Egy elem leírása, ami alapján később (másik oldalbetöltésnél is) megtalálható."""
        desc = {"kind": el.get("kind", ""), "label": el.get("label", ""), "field": el.get("field", ""),
                "href": el.get("href", ""), "ordinal": int(ordinal)}
        if el.get("select"):
            desc["select"] = True
        if el.get("editable"):
            desc["editable"] = True
        return desc

    @_restarts_after_crash
    def locate(self, want: dict[str, Any], timeout: float = 6.0) -> dict[str, Any] | None:
        """Megkeres egy korábban rögzített elemet (lásd describe), és használhatóvá teszi a
        click/type_text/upload_files számára. Ha `timeout` alatt sem jelenik meg, None."""
        page = self._ensure()
        self._locate_seq = 9000 if self._locate_seq > 9900 else self._locate_seq + 1
        query = {**want, "id": self._locate_seq}
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                found = page.evaluate(LOCATE_JS, query)
            except Exception:  # noqa: BLE001 - épp navigál az oldal
                found = None
            if found and found.get("count"):
                item = found["item"]
                self.elements[int(item["id"])] = item
                return item
            if time.monotonic() >= deadline:
                return None
            try:
                page.wait_for_timeout(300)
            except Exception:  # noqa: BLE001
                time.sleep(0.3)

    @_restarts_after_crash
    def scan(self) -> list[dict[str, Any]]:
        """Az oldal összes látható, kezelhető eleme (a látható részen kívüliek is), megszámozva: a modell nélküli
        kitöltés (vinted.py) ebből keresi meg a mezőket és egy lenyitott lista lehetőségeit, és a számukkal
        kattint vagy gépel (click, type_text). Egy kattintás vagy gépelés után a számok már nem érvényesek
        (az oldalnézet újraszámoz), ezért utána újra le kell tapogatni."""
        page = self._ensure()
        found = None
        for attempt in range(3):
            try:
                found = page.evaluate(SCAN_JS, {"base": SCAN_BASE, "span": SCAN_SPAN, "max": SCAN_MAX})
                break
            except Exception as exc:  # noqa: BLE001 - pl. épp navigál az oldal
                if attempt == 2 or is_closed_error(exc):
                    raise BrowserError(f"Nem tudom beolvasni az oldalt ({_short(exc)}).") from None
                self._settle(page)
        elements = [e for e in found or [] if isinstance(e, dict)]
        self.elements = {n: e for n, e in self.elements.items() if not SCAN_BASE <= n < SCAN_BASE + SCAN_SPAN}
        self.elements.update({int(e["id"]): e for e in elements})
        return elements

    @_restarts_after_crash
    def press(self, key: str = "Escape") -> None:
        """Egy billentyű (pl. Esc: bezárja a nyitva maradt listát), ahogy az ember is megnyomná."""
        page = self._ensure()
        self.pace.wait(0.15, 0.4)
        try:
            page.keyboard.press(key)
            page.wait_for_timeout(250)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"Nem sikerült megnyomni: {key} ({_short(exc)}).") from None

    def form_errors(self) -> list[str]:
        """Az űrlap látható hibaüzenetei (pl. egy kötelező, de üres mezőnél), legfeljebb hat."""
        page = self._ensure()
        try:
            found = page.evaluate(FORM_ERRORS_JS)
        except Exception:  # noqa: BLE001 - épp navigál az oldal
            return []
        return [str(text) for text in found or [] if text]

    def challenged(self) -> bool:
        """Robot-ellenőrzést (captcha) vagy robotvédelmi köztes oldalt mutat-e az aktív lap."""
        page = self._ensure()
        try:
            if page.evaluate("() => !!document.querySelector('iframe[src*=\"captcha\" i]')"):
                return True
        except Exception:  # noqa: BLE001 - épp továbbirányít
            return False
        return self._challenge(page, unsure=False)

    def capture(self) -> tuple[bytes | None, str]:
        """A hibacsomaghoz: képernyőkép (JPEG, ha nem túl hosszú, az egész oldalról) és oldalnézet.
        Ha a böngésző nem fut (vagy bezárták az ablakát), nem indítja el: (None, "").
        Ha a kép nem készül el, a capture_error mondja meg, miért."""
        self.capture_error = ""
        page = self.page
        if not self.running or page is None:
            return None, ""
        try:
            if page.is_closed():
                return None, ""
        except Exception:  # noqa: BLE001
            return None, ""
        shot = None
        # Ha az egész oldal képe nem megy, még egyszer a látható részről. Egy korábbi eseménykezelő hibáját
        # (pl. MemoryError) a Playwright a következő hívásnál dobja: ilyenkor a második próba már sikerül.
        for whole in (True, False):
            try:
                height = int(page.evaluate("() => document.documentElement.scrollHeight") or 0) if whole else 0
                shot = page.screenshot(type="jpeg", quality=70, full_page=0 < height <= 9000, timeout=10000)
                self.capture_error = ""
                break
            except Exception as exc:  # noqa: BLE001 - kép nélkül is hasznos a csomag
                detail = str(exc).strip().splitlines()
                self.capture_error = type(exc).__name__ + (f": {detail[0][:200]}" if detail else "")
        try:
            view = self.snapshot()
        except Exception as exc:  # noqa: BLE001
            view = f"(Az oldalnézet nem készült el: {_short(exc)})"
        return shot, view

    @property
    def url(self) -> str:
        try:
            return self.page.url if self.page is not None else ""
        except Exception:  # noqa: BLE001 - bezárt lap
            return ""

    # --- műveletek --------------------------------------------------------------------

    @_restarts_after_crash
    def snapshot(self, note: str = "") -> str:
        try:
            data = self._snapshot_data(self._ensure())
        except BrowserError as exc:
            if not is_closed_error(exc):
                raise
            # A böngésző közben bezárult vagy összeomlott: újraindítjuk, hogy a következő lépés már működjön.
            self._release(exc)
            data = self._snapshot_data(self._ensure())
            note = " ".join(part for part in (note, RESTARTED_NOTE) if part)
        self.elements = {int(e["id"]): e for e in data.get("elements", [])}
        return format_snapshot(data, note)

    def _snapshot_data(self, page: Any) -> dict[str, Any]:
        for attempt in range(3):
            try:
                return page.evaluate(SNAPSHOT_JS, {"max": int(self.cfg.max_elements),
                                                   "textChars": VISIBLE_TEXT_CHARS})
            except Exception as exc:  # noqa: BLE001 - pl. épp navigál az oldal
                if attempt == 2 or is_closed_error(exc):
                    raise BrowserError(f"Nem tudom beolvasni az oldalt ({_short(exc)}).") from None
                self._settle(page)
        raise BrowserError("Nem tudom beolvasni az oldalt.")

    def _goto(self, url: str, timeout: int = 30000) -> Any:
        """Az aktív lapon megnyitja a címet. Ha közben bezárták a böngészőt (vagy az ablakát), újraindítja,
        és még egyszer megpróbálja: így egy bezárt ablak nem rontja el a következő kérést."""
        page = self._ensure()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            return page
        except Exception as exc:  # noqa: BLE001
            if not is_closed_error(exc):
                raise BrowserError(f"Nem sikerült megnyitni: {url} ({_short(exc)})") from None
            self._release(exc)
        page = self._ensure()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"Nem sikerült megnyitni: {url} ({_short(exc)})") from None
        return page

    @_restarts_after_crash
    def open(self, url: str) -> str:
        try:
            url = web.normalize_url(url, allow_local=self.allow_local)
        except web.WebError as exc:
            raise BrowserError(str(exc)) from None
        page = self._goto(url)
        self._settle(page)
        if self.auto:
            self.pace.read()
        self._emit({"do": "goto", "url": url, "final": page.url})
        return self.snapshot()

    @_restarts_after_crash
    def click(self, element: Any) -> str:
        page = self._ensure()
        el = self._element(element)
        label = el.get("label", "")
        ordinal = self._ordinal(el)
        url_before = page.url
        risky = bool(self.cfg.confirm_risky and is_risky(label, link=navigates(el)))
        if self.auto:   # modell nélkül: egy ember is megnézi, mire kattint (a közzététel előtt az egészet)
            if risky:
                self.pace.review()
            else:
                self.pace.think()
        if risky:
            preapproved = False
            if self.approve_hook is not None:
                try:
                    preapproved = bool(self.approve_hook(label, page.url))
                except Exception:  # noqa: BLE001
                    preapproved = False
            if not preapproved and not self.confirm(f'Jarvis erre kattintana: "{label}"\n    ({page.url})'):
                self._emit({"do": "declined", "el": self.describe(el, ordinal), "url": url_before})
                return "A felhasználó nem engedélyezte ezt a kattintást."
        pages_before = len([p for p in self._context.pages if not p.is_closed()])
        # Ha a gomb fájlválasztót nyitna, nem engedjük feljönni a Windows-ablakot (azt a Jarvis
        # nem tudná kezelni); helyette szólunk, hogy a feltöltő eszköz kell.
        choosers: list[Any] = []

        def on_chooser(chooser: Any) -> None:
            choosers.append(chooser)

        page.on("filechooser", on_chooser)
        try:
            self._human_click(page, self._locator(el["id"]))
            self._settle(page)
        except Covered as exc:
            raise self._fail(f'Nem sikerült rákattintani erre: "{label}", mert {exc}. {UNCOVER_HINT}') from None
        except Exception as exc:  # noqa: BLE001
            raise self._fail(f'Nem sikerült rákattintani erre: "{label}" ({_short(exc)}). '
                             "Lehet, hogy valami (pl. süti-ablak) eltakarja.") from None
        finally:
            page.remove_listener("filechooser", on_chooser)
        note = f'Rákattintottam: "{label}".'
        if choosers:
            note += (" Ez a gomb fájlt (pl. fotót) kér. A feltöltéshez a browser_upload_photos "
                     "eszközt használd ugyanerre az elemre.")
        if self._adopt_new_page(pages_before):
            note += " Új lapon nyílt meg."
        self._emit({"do": "click", "el": self.describe(el, ordinal), "url": url_before, "url_after": self.url,
                    "risky": risky, "chooser": bool(choosers)})
        return self.snapshot(note)

    @_restarts_after_crash
    def type_text(self, element: Any, text: str, submit: bool = False) -> str:
        page = self._ensure()
        el = self._element(element)
        if el.get("sensitive") or el.get("kind") == "input:password":
            raise BrowserError("Jelszót és bankkártyaadatot nem írok be. Kérlek, ezt a "
                               "böngészőablakban add meg te, utána folytatom.")
        ordinal = self._ordinal(el)
        if submit and self.cfg.confirm_risky and not is_search_field(el):
            label = el.get("label") or "mező"
            if not self.confirm(f'Jarvis ezt írná be és küldené el ("{label}"):\n    {text}'):
                self._emit({"do": "declined", "el": self.describe(el, ordinal), "url": page.url})
                return "A felhasználó nem engedélyezte az elküldést."
        if self.auto:
            self.pace.think()
        try:
            locator = self._put_text(page, el, text)
            if submit:
                self.pace.wait(0.2, 0.6)
                locator.press("Enter")
        except Covered as exc:
            raise self._fail(f"Nem sikerült beírni a szöveget, mert {exc}. {UNCOVER_HINT}") from None
        except Exception as exc:  # noqa: BLE001
            raise self._fail(f"Nem sikerült beírni a szöveget ({_short(exc)}).") from None
        self._settle(page)
        self._emit({"do": "type", "el": self.describe(el, ordinal), "text": text, "submit": bool(submit),
                    "search": is_search_field(el)})
        note = f'Beírtam: "{text}"' + (", és elküldtem (Enter)." if submit else ".")
        return self.snapshot(note)

    @_restarts_after_crash
    def fill_fields(self, fields: list[dict[str, Any]]) -> str:
        """Több mezőt tölt ki egy lépésben (a legutóbbi oldalnézet számai szerint), Enter nélkül.

        Egy űrlapnál (cím, leírás, ár…) így egy modellkör az, ami mezőnként öt lenne, és csak
        egy oldalnézet kerül a beszélgetésbe."""
        page = self._ensure()
        if not isinstance(fields, list) or not fields:
            raise BrowserError('Add meg a mezőket, pl.: [{"element": 3, "text": "Zara póló"}]')
        plan = []
        for entry in fields[:20]:
            if not isinstance(entry, dict) or "element" not in entry:
                raise BrowserError('Minden mezőnél kell az elem száma és a szöveg: {"element": 3, "text": "…"}')
            el = self._element(entry["element"])
            if el.get("sensitive") or el.get("kind") == "input:password":
                raise BrowserError("Jelszót és bankkártyaadatot nem írok be. Kérlek, ezt a "
                                   "böngészőablakban add meg te, utána folytatom.")
            plan.append((el, str(entry.get("text") if entry.get("text") is not None else "")))
        ordinals = [self._ordinal(el) for el, _text in plan]
        done, failed = [], []
        for index, ((el, text), ordinal) in enumerate(zip(plan, ordinals)):
            label = el.get("label") or f"{el['id']}. elem"
            if index:
                self.pace.wait(0.3, 0.9)   # a következő mezőre átnéz
            try:
                self._put_text(page, el, text)
                done.append(f'"{label}"')
                self._emit({"do": "type", "el": self.describe(el, ordinal), "text": text, "submit": False,
                            "search": is_search_field(el)})
            except Exception as exc:  # noqa: BLE001 - a többi mezőt attól még kitöltjük
                failed.append(f'"{label}" ({_short(exc)})')
        self._settle(page)
        note = f"Kitöltöttem: {', '.join(done)}." if done else ""
        if failed:
            note += (f" Nem sikerült: {'; '.join(failed)}. Ezeket a friss oldalnézet alapján egyenként "
                     "próbáld (browser_type).")
        return self.snapshot(note.strip())

    def _put_text(self, page: Any, el: dict[str, Any], text: str) -> Any:
        """Szöveg egy mezőbe (lenyíló listánál a lehetőség kiválasztása).

        Emberi tempóban: a mezőbe kattint, kijelöli a régi szöveget, és a rövid szöveget
        billentyűnként begépeli, a hosszút (pl. leírás) beilleszti, ahogy az ember a jegyzetéből."""
        locator = self._locator(el["id"])
        if el.get("select"):
            try:
                locator.select_option(label=text, timeout=5000)
            except Exception:  # noqa: BLE001
                locator.select_option(value=text, timeout=5000)
            return locator
        if not self.pace.enabled:
            if el.get("editable"):
                locator.click(timeout=5000)
                page.keyboard.press("Control+A")
                page.keyboard.type(text, delay=10)
            else:
                locator.fill(text, timeout=8000)
            return locator
        self._human_click(page, locator)
        self.pace.wait(0.1, 0.35)
        page.keyboard.press("Control+A")
        if not text:
            page.keyboard.press("Delete")
        elif len(text) > LONG_TEXT or "\n" in text:
            self.pace.wait(0.3, 0.9)
            page.keyboard.insert_text(text)
        else:
            for char, delay in zip(text, self.pace.key_delays(text)):
                page.keyboard.type(char)
                if delay:
                    self._sleep(delay)
        if not el.get("editable"):
            self._ensure_value(locator, text)
        return locator

    @staticmethod
    def _ensure_value(locator: Any, text: str) -> None:
        """Ha a mezőben mégsem az áll, amit beírt (pl. a Ctrl+A nem jelölte ki a régit, és a „0”
        mögé került az „500”), beállítja."""
        try:
            value = locator.input_value(timeout=2000)
        except Exception:  # noqa: BLE001 - nem sima mező (pl. egyedi komponens): így hagyjuk
            return
        if not same_value(value, text):
            locator.fill(text, timeout=8000)

    # --- emberi mozdulatok (humanize.py) ---------------------------------------------------

    def _sleep(self, seconds: float) -> None:
        """Várakozás úgy, hogy közben a böngésző eseményei (pl. fájlválasztó) is feldolgozódjanak."""
        if seconds <= 0:
            return
        page = self.page
        try:
            if page is not None and not page.is_closed():
                page.wait_for_timeout(seconds * 1000)
                return
        except Exception:  # noqa: BLE001 - épp bezárult vagy navigál
            pass
        time.sleep(seconds)

    def _move_mouse(self, page: Any, x: float, y: float) -> None:
        """Az egér odamegy (x, y)-ra, enyhe ívben, ahogy a kéz mozgatja."""
        start = self._mouse or (self.cfg.width * self.pace.rng.uniform(0.3, 0.7),
                                self.cfg.height * self.pace.rng.uniform(0.3, 0.7))
        points = self.pace.path(start, (x, y))
        step = self.pace.move_seconds(start, (x, y)) / max(1, len(points))
        for px, py in points:
            page.mouse.move(px, py)
            if step > 0:
                self._sleep(step)
        self._mouse = (x, y)

    def _view_height(self, page: Any) -> float:
        try:
            return float((page.viewport_size or {}).get("height") or self.cfg.height)
        except Exception:  # noqa: BLE001
            return float(self.cfg.height)

    def _wheel(self, page: Any, pixels: float) -> None:
        """Görgetés az egérgörgővel, több kisebb „kattanásban”, ahogy a görgő forog."""
        done, total = 0.0, abs(pixels)
        while done < total:
            tick = min(total - done, self.pace.rng.uniform(90, 160))
            page.mouse.wheel(0, tick if pixels > 0 else -tick)
            done += tick
            self.pace.wait(0.03, 0.09)

    def _wheel_into_view(self, page: Any, locator: Any, box: dict[str, float] | None) -> dict[str, float] | None:
        """Ha az elem a látható részen kívül van, az egérgörgővel tekeri oda, ahogy az ember (az oldal nem
        „ugrik” oda). Ha a görgetés nem viszi (pl. egy belső, saját görgetésű listában van), abbahagyja;
        utána a scroll_into_view_if_needed úgyis odaviszi."""
        if not box:
            return box
        height = self._view_height(page)
        if box["y"] >= 0 and box["y"] + box["height"] <= height:
            return box
        if self._mouse is None:   # a görgetés oda megy, ahol az egér áll: legyen az oldal fölött
            self._move_mouse(page, self.cfg.width * self.pace.rng.uniform(0.35, 0.65),
                             height * self.pace.rng.uniform(0.35, 0.65))
        for _ in range(12):       # legfeljebb kb. 12 × 4 „kattanás”
            delta = box["y"] - height * self.pace.rng.uniform(0.3, 0.6)   # >0: lefelé kell görgetni
            if abs(delta) < 60:
                break
            self._wheel(page, max(-560.0, min(560.0, delta)))
            self.pace.wait(0.08, 0.2)
            moved = self._box_after_scroll(locator, box)
            if not moved or abs(moved["y"] - box["y"]) < 5:   # nem mozdult: nem ez a görgethető rész
                return moved or box
            box = moved
            if box["y"] >= 0 and box["y"] + box["height"] <= height:
                break
        return box

    def _box_after_scroll(self, locator: Any, box: dict[str, float]) -> dict[str, float] | None:
        """Az elem helye a görgetés után (a böngésző kicsit később görget, mint ahogy a görgő forgott)."""
        moved = None
        for _ in range(6):
            moved = locator.bounding_box(timeout=3000)
            if not moved or abs(moved["y"] - box["y"]) >= 5:
                break
            self._sleep(0.04)
        return moved

    def _glance(self, page: Any) -> None:
        """Modell nélküli olvasásnál (pl. árkutatás): az egér a tartalom fölé megy, és lejjebb görget egy
        kicsit, ahogy az ember átfutja a találatokat."""
        if not self.pace.enabled:
            return
        try:
            height = self._view_height(page)
            self._move_mouse(page, self.cfg.width * self.pace.rng.uniform(0.35, 0.65),
                             height * self.pace.rng.uniform(0.35, 0.6))
            self._wheel(page, height * self.pace.rng.uniform(0.3, 0.7))
        except Exception:  # noqa: BLE001 - csak a tempó része, a találatok enélkül is beolvashatók
            pass

    def _human_click(self, page: Any, locator: Any) -> None:
        """Kattintás: ha kell, az egérgörgővel odagörget, az egér odamegy az elemhez (nem pont a közepére),
        és egy pillanatig nyomja a gombot.

        Ha egy nyitva maradt lenyíló lista vagy felugró ablak eltakarja az elemet, megnyomja az Esc-et (ahogy
        az ember is), és úgy kattint. Ha az sem segít, Covered kivétel jelzi, mi takarja."""
        try:
            self._click_once(page, locator, trial=True)
            blocker = ""
        except Exception as exc:  # noqa: BLE001 - csak a próba: a valódi kattintás majd megmondja, mi a baj
            blocker = blocker_of(exc)
        if blocker:
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(350)
            except Exception:  # noqa: BLE001
                pass
        try:
            self._click_once(page, locator)
        except Exception as exc:  # noqa: BLE001
            still = blocker_of(exc)
            if still:
                raise Covered(f"eltakarja {still}" + (", és az Esc sem zárta be" if blocker else "")) from None
            raise

    def _click_once(self, page: Any, locator: Any, trial: bool = False) -> None:
        """Egy kattintás (trial: csak kipróbálja, kattintható-e, rövid várakozással)."""
        timeout = self.PROBE_TIMEOUT_MS if trial else self.CLICK_TIMEOUT_MS
        if not self.pace.enabled:
            locator.click(timeout=timeout, trial=trial)
            return
        try:
            box = self._wheel_into_view(page, locator, locator.bounding_box(timeout=3000))
            locator.scroll_into_view_if_needed(timeout=5000)
            box = locator.bounding_box(timeout=3000)
        except Exception:  # noqa: BLE001 - pl. épp eltűnt: a sima kattintás majd megmondja, mi a baj
            box = None
        if not box or box["width"] < 2 or box["height"] < 2:
            locator.click(timeout=timeout, trial=trial, delay=0 if trial else self.pace.press_ms())
            return
        if trial:   # a próbánál még nem mozdul az egér, csak azt nézzük, nem takarja-e el valami a közepét
            locator.click(timeout=timeout, trial=True)
            return
        x, y = self.pace.point_in(box)
        try:
            self._move_mouse(page, x, y)
        except Exception:  # noqa: BLE001 - az egérút csak díszítés, a kattintás a lényeg
            pass
        self.pace.wait(0.05, 0.18)
        locator.click(timeout=timeout, position={"x": x - box["x"], "y": y - box["y"]}, delay=self.pace.press_ms())
        self._mouse = (x, y)

    @_restarts_after_crash
    def scroll(self, direction: str = "down") -> str:
        page = self._ensure()
        up = fold(direction).strip() in ("up", "fel", "felfele", "felfelé")
        total = int(self.cfg.height * 0.8)
        if self.pace.enabled:   # több kisebb görgetés, ahogy az egérgörgő forog
            self._move_mouse(page, self.cfg.width * self.pace.rng.uniform(0.35, 0.65),
                             self.cfg.height * self.pace.rng.uniform(0.4, 0.7))
            self._wheel(page, -total if up else total)
        else:
            page.mouse.move(int(self.cfg.width) // 2, int(self.cfg.height) // 2)
            page.mouse.wheel(0, -total if up else total)
        self._settle(page, max_wait=2.5)  # végtelen görgetésű oldalak ilyenkor töltenek újabb elemeket
        return self.snapshot("Felfelé görgettem." if up else "Lefelé görgettem.")

    @_restarts_after_crash
    def back(self) -> str:
        page = self._ensure()
        before = page.url
        try:
            page.go_back(wait_until="domcontentloaded", timeout=15000)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"Nem sikerült visszalépni ({_short(exc)}).") from None
        self._settle(page)
        self._emit({"do": "back"})
        note = "Visszaléptem az előző oldalra." if page.url != before else "Nincs hova visszalépni."
        return self.snapshot(note)

    @_restarts_after_crash
    def read(self) -> str:
        """Az aktuális oldal teljes (fő)szövege, pl. egy cikk elolvasásához."""
        page = self._ensure()
        try:
            # Cikkeknél a kinyert főszöveg a jobb; terméklistáknál, táblázatoknál viszont a
            # kinyerő sokszor kidobja a lényeget, ilyenkor a látható szöveg a teljesebb.
            title, text = self._page_text(page)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"Nem tudom beolvasni az oldalt ({_short(exc)}).") from None
        return web.format_page(title, page.url, text, self.read_max_chars, js_hint=False)

    def _page_text(self, page: Any) -> tuple[str, str]:
        """(oldalcím, szöveg): cikknél a kinyert főszöveg, listánál, táblázatnál a látható szöveg."""
        content = page.content()
        visible_text = page.evaluate(
            "() => { const m = document.querySelector('main, [role=main]') || document.body;"
            " return m ? m.innerText : ''; }")
        title, text = web.extract_text(content, page.url)
        if len(text) < len(visible_text or "") * 0.4:
            text = web.tidy(visible_text)
        return title, text

    def _challenge(self, page: Any, unsure: bool = True) -> bool:
        try:
            title = page.title() or ""
            head = page.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 1500)") or ""
        except Exception:  # noqa: BLE001 - épp továbbirányít
            return unsure
        return bool(_CHALLENGE_RE.search(title)) or (len(head) < 600 and bool(_CHALLENGE_RE.search(head)))

    @_restarts_after_crash
    def fetch_text(self, url: str) -> tuple[str, str, str]:
        """Elolvas egy oldalt egy KÜLÖN lapon, és bezárja (az aktív lap, amin a modell épp dolgozik,
        nem változik): (végső cím, oldalcím, szöveg). Azokhoz az oldalakhoz, amelyek a sima
        letöltést elutasítják (403), de egy valódi böngészőnek megnyílnak. A robotvédelmi köztes
        oldalt megvárja (CHALLENGE_WAIT); ha captchát kér, nem kerüli meg, hanem hibát ad."""
        try:
            url = web.normalize_url(url, allow_local=self.allow_local)
        except web.WebError as exc:
            raise BrowserError(str(exc)) from None
        self._ensure()
        try:
            page = self._context.new_page()
        except Exception as exc:  # noqa: BLE001
            if not is_closed_error(exc):
                raise BrowserError(f"Nem sikerült új lapot nyitni ({_short(exc)}).") from None
            self._release(exc)
            self._ensure()
            page = self._context.new_page()
        try:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:  # noqa: BLE001
                raise BrowserError(f"Nem sikerült megnyitni: {url} ({_short(exc)})") from None
            self._settle(page, max_wait=6.0)
            deadline = time.monotonic() + CHALLENGE_WAIT
            while self._challenge(page):
                if time.monotonic() >= deadline:
                    raise BrowserError(f"A(z) {web.host_of(url)} a böngészőmet sem engedi be (robotvédelem). "
                                       f"Az ellenőrzést nem kerülöm meg; ha kell, nyisd meg te: {url}")
                page.wait_for_timeout(1000)
                self._settle(page, max_wait=3.0)
            try:
                title, text = self._page_text(page)
            except Exception as exc:  # noqa: BLE001
                raise BrowserError(f"Nem tudom beolvasni az oldalt ({_short(exc)}).") from None
            return page.url, title, text
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass

    def read_url(self, url: str) -> str:
        """Mint a read_webpage, de a saját böngészővel (külön lapon)."""
        final_url, title, text = self.fetch_text(url)
        return web.format_page(title, final_url, text, self.read_max_chars, js_hint=False)

    @_restarts_after_crash
    def screenshot_b64(self) -> str:
        page = self._ensure()
        try:
            data = page.screenshot(type="jpeg", quality=75)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"Nem sikerült képet készíteni az oldalról ({_short(exc)}).") from None
        return base64.b64encode(data).decode("ascii")

    def current_host(self) -> str:
        """Az aktuális oldal gépneve (pl. www.vinted.hu), vagy üres, ha nincs nyitott oldal."""
        if self.page is None:
            return ""
        try:
            return urllib.parse.urlparse(self.page.url).hostname or ""
        except Exception:  # noqa: BLE001 - bezárt lap
            return ""

    @_restarts_after_crash
    def upload_files(self, files: list[str], element: Any = None) -> str:
        """Fájlokat (fotókat) tölt fel az oldal fájlfeltöltő mezőjébe.

        Elem nélkül az oldal képfeltöltő mezőjét keresi meg (ez akkor is működik, ha a mező
        rejtett, és csak egy szép gomb látszik belőle); elemmel arra kattint, és a felugró
        fájlválasztót tölti ki. A Windows fájlválasztó ablaka így nem jelenik meg.
        """
        page = self._ensure()
        if not files:
            raise BrowserError("Nincs feltölthető fotó.")
        note = f"Feltöltöttem {len(files)} fotót."
        used: dict[str, Any] | None = None
        if self.auto:
            self.pace.pick_files()
        if element not in (None, ""):
            el = self._element(element)
            used = self.describe(el, self._ordinal(el))
            locator = self._locator(el["id"])
            try:
                if el.get("kind") == "input:file":
                    self._fill_input(page, locator, files)
                else:
                    with page.expect_file_chooser(timeout=6000) as info:
                        locator.click(timeout=8000)
                    chooser = info.value
                    if chooser.is_multiple() or len(files) == 1:
                        chooser.set_files(files)
                    else:
                        chooser.set_files(files[:1])
                        note = ("Ez a gomb egyszerre csak egy fotót fogad, ezért az elsőt töltöttem fel. "
                                "A többihez hívd a browser_upload_photos eszközt elem nélkül.")
            except Exception as exc:  # noqa: BLE001
                raise self._fail(f"Ezzel az elemmel nem sikerült feltölteni ({_short(exc)}). "
                                 "Próbáld elem nélkül, vagy a fotófeltöltő gomb számával.") from None
        else:
            inputs = page.locator("input[type=file]")
            try:
                count = inputs.count()
            except Exception:  # noqa: BLE001
                count = 0
            if not count:
                raise self._fail("Nem találok fájlfeltöltő mezőt az oldalon. Add meg a "
                                 "fotófeltöltő gomb számát a legutóbbi oldalnézetből.")
            target = inputs.first
            for index in range(count):
                accept = (inputs.nth(index).get_attribute("accept") or "").lower()
                if not accept or "image" in accept or "jp" in accept:
                    target = inputs.nth(index)
                    break
            try:
                self._fill_input(page, target, files)
            except Exception as exc:  # noqa: BLE001
                raise self._fail(f"Nem sikerült feltölteni a fotókat ({_short(exc)}).") from None
        self._settle(page)
        self._emit({"do": "upload", "el": used, "count": len(files)})
        return self.snapshot(note)

    @staticmethod
    def _fill_input(page: Any, locator: Any, files: list[str]) -> None:
        multiple = locator.get_attribute("multiple") is not None
        if multiple or len(files) == 1:
            locator.set_input_files(files, timeout=15000)
            return
        for file in files:   # egyesével fogadó mező: fotónként újra
            locator.set_input_files([file], timeout=15000)
            page.wait_for_timeout(600)

    @_restarts_after_crash
    def extract_listings(self, url: str) -> list[dict[str, Any]]:
        """Egy keresési találati oldal hirdetései árral (az árkutatáshoz)."""
        try:
            url = web.normalize_url(url, allow_local=self.allow_local)
        except web.WebError as exc:
            raise BrowserError(str(exc)) from None
        page = self._goto(url)
        self._settle(page, max_wait=8.0)
        self._glance(page)   # modell nélküli böngészés: egy ember is átfutja a találatokat
        self.pace.read()
        self.elements = {}   # a korábbi oldalnézet számai itt már nem érvényesek
        try:
            found = page.evaluate(LISTINGS_JS)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"Nem tudom beolvasni a találatokat ({_short(exc)}).") from None
        return [entry for entry in found or [] if isinstance(entry, dict) and entry.get("price")]
