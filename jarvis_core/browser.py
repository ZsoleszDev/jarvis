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
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

from . import web
from .config import BrowserCfg, data_path, resolve_path
from .humanize import LONG_TEXT, Pace
from .textutil import fold

VISIBLE_TEXT_CHARS = 1500
# A modell nélküli kitöltés letapogatásának számai (az oldalnézeté 1-től, a visszajátszásé 9000-től megy).
# Bőven kell: a lenyíló listák gyakran a lap legvégére kerülnek, a fejléc és a lábléc linkjei után.
SCAN_BASE, SCAN_SPAN, SCAN_MAX = 5000, 3000, 1500
LOCATE_BASE = 9000         # a visszajátszás (locate) számai innen indulnak
BELOW_MAX = 15             # az oldalnézet alján legfeljebb ennyi lejjebb lévő űrlapelem („Lejjebb az űrlapon”)
HREF_CHARS = 60            # a linkek címe az oldalnézetben legfeljebb ilyen hosszú
ERRORS_CHARS = 400         # az oldalnézet „Hibaüzenetek” sora legfeljebb ilyen hosszú
# A link címéből ezek a paraméterek maradnak meg (keresés, lapozás, rendezés, kategória); a többi (követés,
# munkamenet, ajánló) csak a helyet foglalná. A teljes cím az elemek között (self.elements) megvan.
_HREF_KEEP = frozenset({"q", "query", "search", "search_text", "s", "page", "p", "order", "sort", "catalog",
                        "catalog[]", "category", "cat", "kategoria", "oldal"})
# Robotvédelmi köztes oldal (pl. Cloudflare "Just a moment..."): egy valódi böngészőben ez
# többnyire magától továbbenged pár másodperc alatt. Ha captchát kér, azt nem kerüljük meg.
_CHALLENGE_RE = re.compile(
    r"just a moment|checking your browser|attention required|verify (that )?you are (a )?human|"
    r"are you a robot|captcha|cf-chl|egy pillanat|ellenőrizzük, hogy|nem vagy robot|access denied",
    re.IGNORECASE)
CHALLENGE_WAIT = 12.0      # ennyit vár, hogy a köztes oldal magától továbbengedjen
# Egy captcha, amit tényleg meg kell oldani: látható, kattintható méretű captcha-keret. Sok oldal (a Vinted is)
# láthatatlan reCAPTCHA-t vagy robotvédelmi keretet tölt be minden oldalon (rejtve, 0 vagy kis méretben, a sarokban
# lévő jelvényben); az nem kér semmit, ezért az nem számít.
CAPTCHA_FRAME_JS = r"""[...document.querySelectorAll('iframe')].some((f) => {
    const src = f.getAttribute('src') || '';
    if (!/captcha/i.test(src) || /size=invisible/i.test(src) || f.closest('.grecaptcha-badge')) return false;
    const r = f.getBoundingClientRect(), s = getComputedStyle(f);
    return r.width >= 150 && r.height >= 60 && s.visibility !== 'hidden' && s.display !== 'none'
      && Number(s.opacity || 1) > 0.1 && r.right > 0 && r.bottom > 0 && r.left < innerWidth;
  })"""
RESTARTED_NOTE = ("A böngésző közben bezárult vagy összeomlott (pl. elfogyott a gép memóriája); újraindítottam, "
                  "az előző oldal már nincs nyitva.")
# Ennél régebben indult, be nem fejezett adatkérésre nem várunk (pl. egy hosszan nyitva tartott kapcsolat,
# vagy egy kérés, aminek a vége elveszett, mert közben átnavigált az oldal).
STUCK_REQUEST_SECONDS = 15.0
# Egy másik oldalról jövő szkriptre (pl. a webshop CDN-jéről, de egy reklámé is lehet) csak ennyit várunk.
FOREIGN_SCRIPT_SECONDS = 2.0
# Az oldal adatkéréseit (XHR, fetch, dokumentum, szkript) a Chromium hálózati eseményeiből számoljuk.
_TRACKED_TYPES = {"XHR", "Fetch", "Document", "Script"}
# Az oldal megnyugvása (_settle): ennyi ideig nem változhat a lap (új oldalnál tovább, mert ott a szkriptek még
# szakaszosan rajzolnak), ilyen sűrűn nézzük, és egy folyton változó lapnál (pl. futó óra) ennyi ideig nem változhat
# a szöveg hossza. Ez nem az emberi tempó (az a humanize.py-ban van), csak a lap figyelése.
QUIET_SECONDS = 0.2
QUIET_AFTER_LOAD = 0.4
SETTLE_POLL = 0.1
CHURN_AFTER, CHURN_STABLE = 0.6, 0.5
# Egy új oldal legalább ennyi idős legyen (a megnyitása óta), mire kész: a szkriptjei a betöltés után még
# késleltetve is rajzolhatnak (egy valódi oldal betöltése úgyis tovább tart, ez a gyors oldalaknál számít).
MIN_DOC_AGE = 1.0
# Gépelés után egy kereső- vagy javaslatos mezőnél legalább ennyit várunk a javaslatokra (az oldal a gépelés
# végét kivárva, késleltetve kérdezi le őket).
SUGGEST_REACT = 0.8
# Egyesével fogadó fájlmezőnél legfeljebb ennyit várunk, hogy az oldal átvegye a fotót (lásd _wait_taken).
UPLOAD_TAKE_SECONDS = 5.0
# Ezek a második szintű tartományok (pl. co.uk) a webcím „oldalának” részei (lásd same_site).
_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "edu", "ac", "or", "ne", "go", "info", "nom"}
DECLINED = "A felhasználó nem engedélyezte"
MAX_ACTIONS = 5            # a browser_do legfeljebb ennyi lépést tesz egy körben
MAX_CHOOSE_LEVELS = 6      # a browser_choose fája (pl. Nők > Ruházat > Pólók) legfeljebb ennyi szint
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
# Fizetős extrák (kiemelés, előresorolás): a browser_choose és a browser_do ezekre soha nem kattint.
_PAID_RE = re.compile(r"(kiemel|elore ?sorol|elorehoz|elofizet|fizetos|\bbump|\bboost|\bpromot)")
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
      const home = el.getRootNode();   // árnyék-DOM-ban a hivatkozott elem is ott van
      const t = clean(by.split(/\s+/).map(id => {
        const n = (home.getElementById && home.getElementById(id)) || document.getElementById(id);
        return n ? n.innerText : '';
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
  const nodes = document.querySelectorAll('[role=alert], [aria-live=assertive], [class*=error i], ' +
                                          '[class*=invalid i], [data-testid*=error i]');
  for (const el of nodes) {
    if (out.length >= 6) break;
    if (!shown(el)) continue;
    const text = clean(el.innerText);
    if (text && text.length <= 200 && !out.some((t) => t.includes(text) || text.includes(t))) out.push(text);
  }
  return out;
}
"""

SNAPSHOT_JS = r"""
(opts) => {
  const MAX = opts.max || 60;
  const TEXT_CHARS = opts.textChars || 1500;
  const BASE = opts.base || 5000;
  const BELOW_MAX = opts.below || 15;
  const HINTS = opts.hints || [];
  const vh = window.innerHeight;
""" + _JS_HELPERS + r"""
  // Nyitott shadow DOM (pl. süti-ablak, beágyazott űrlapelem) tartalma is: a Playwright CSS-keresője ezekbe is
  // belát, így a számukkal ugyanúgy kattinthatók. Ha nincs ilyen az oldalon, marad a gyors keresés.
  const hosts = [];   // az árnyék-DOM gazdái (a beágyazottak is), a lap sorrendjében
  const findHosts = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) {
        hosts.push(el);
        findHosts(el.shadowRoot);
      }
    }
  };
  findHosts(document);
  const deep = hosts.length > 0;
  const deepAll = (selector) => {
    if (!deep) return Array.from(document.querySelectorAll(selector));
    const out = [];
    const visit = (root) => {
      const hit = new Set(root.querySelectorAll(selector));   // gyorsabb, mint elemenként a matches()
      for (const el of root.querySelectorAll('*')) {
        if (hit.has(el)) out.push(el);
        if (el.shadowRoot) visit(el.shadowRoot);
      }
    };
    visit(document);
    return out;
  };
  const closestDeep = (el, selector) => {
    for (let node = el; node;) {
      const hit = node.closest(selector);
      if (hit) return hit;
      const root = node.getRootNode();
      node = root && root.host ? root.host : null;
    }
    return null;
  };
  const cut = (s, n) => {
    s = clean(s);
    if (s.length <= n) return s;
    const head = s.slice(0, n - 1);
    const space = head.lastIndexOf(' ');
    return (space > n / 2 ? head.slice(0, space) : head) + '…';
  };
  const byIds = (el, ids) => clean((ids || '').split(/\s+/).map((id) => {
    const root = el.getRootNode();
    const n = id && ((root.getElementById && root.getElementById(id)) || document.getElementById(id));
    return n ? n.innerText : '';
  }).join(' '));

  // Állandó számok: egy elem a dokumentum élete alatt megtartja a számát, új számot csak az új elemek kapnak.
  // A számok BASE (a letapogatásé) alatt maradnak; a letapogatás és a visszajátszás számai külön attribútumban
  // vannak, azokhoz nem nyúlunk.
  let state = window.__jarvisView;
  const newDoc = !state;
  if (!state) {
    state = {seq: 0, ids: new WeakMap()};
    try {
      Object.defineProperty(window, '__jarvisView', {value: state, configurable: true});
    } catch (e) { /* nem bővíthető ablak: minden nézet elölről számoz */ }
  }
  let renumbered = false;
  if (state.seq + MAX + BELOW_MAX >= BASE) {
    state.seq = 0;
    state.ids = new WeakMap();
    renumbered = true;
  }
  const seqBefore = state.seq;
  // Egy lemásolt elemen (vagy az újraszámozás előtti) szám nem érvényes: csak az marad, amit mi adtunk neki.
  for (const el of deepAll('[data-jarvis-id]')) {
    if (state.ids.get(el) !== el.getAttribute('data-jarvis-id')) el.removeAttribute('data-jarvis-id');
  }

  const CHROME = 'header, nav, footer, aside, [role=banner], [role=navigation], [role=contentinfo]';
  const FIELD = /^(input|textbox|searchbox|combobox|textarea|select)/;
  const CHOICE = /^(input:radio|input:checkbox|radio|checkbox|option|switch)$/;
  const BUTTON = /^(button|input:submit|input:button|input:image)$/;
  // Küldő jellegű gomb akkor is, ha nincs <form> körülötte (pl. React-űrlap „Feltöltés” gombja).
  const SUBMIT = new RegExp('^(tovabb|kovetkezo|feltolt|kozzetesz|hirdetes ?felad|feladom|mentes|elkuld|kuld|' +
                            'befejez|next|continue|submit|save|publish|upload)');
  // Űrlapelem: mező, választó, vagy egy űrlap (küldő) gombja; ezeket az ablak alatt is felsoroljuk.
  const formControl = (el, kind, name) => (
    FIELD.test(kind) || (CHOICE.test(kind) && kind !== 'option') ||
    (BUTTON.test(kind) && (!!el.closest('form') || el.getAttribute('type') === 'submit' || SUBMIT.test(fold(name)))));
  const candidates = [];
  const beyond = [];
  const seen = new Set();
  let order = 0;
  for (const el of deepAll(SELECTOR)) {
    order++;
    if (el.disabled) continue;
    const r = el.getBoundingClientRect();
    if (r.bottom < -vh * 0.25) continue;
    const far = r.top > vh * 1.5;
    const kind = kindOf(el);
    if (far && !FIELD.test(kind) && !CHOICE.test(kind)) {
      // a lap aljáról csak a küldő jellegű gombok kellenek: előbb olcsón, a nyers szövegükből szűrünk
      if (!BUTTON.test(kind)) continue;
      const quick = el.getAttribute('aria-label') || el.value || (el.textContent || '').slice(0, 60);
      if (!el.closest('form') && el.getAttribute('type') !== 'submit' && !SUBMIT.test(fold(quick))) continue;
    }
    if (!visible(el)) continue;
    const name = nameOf(el);
    const isField = FIELD.test(kind);
    if (!name && !isField) continue;
    const inChrome = !!closestDeep(el, CHROME);
    const form = !inChrome && formControl(el, kind, name);
    if (far && !form) continue;
    // ugyanaz a link többször (kép + cím ugyanarra a termékre): elég egyszer
    const key = kind + '|' + name + '|' + (el.getAttribute('href') || '');
    if (!isField && seen.has(key)) continue;
    seen.add(key);
    // a mezők (pl. a fejlécben lévő kereső) mindig fontosak, nem számítanak menünek
    (far ? beyond : candidates).push({el, name, kind, order, inView: r.bottom > 0 && r.top < vh,
                                      below: r.top >= vh, chrome: !isField && inChrome, form});
  }
  // Sorrend: fő tartalom a képernyőn, fő tartalom alatta, menük a képernyőn, a többi menü.
  // Így egy webshopban a termékek és szűrők nem szorulnak ki a fejléc linkjei miatt.
  const rank = (c) => (c.chrome ? 2 : 0) + (c.inView ? 0 : 1);
  candidates.sort((a, b) => rank(a) - rank(b) || a.order - b.order);
  const MAX_CHROME = Math.max(10, Math.floor(MAX / 3));
  let chromeCount = 0;
  const chosen = [];
  const left = [];
  for (const c of candidates) {
    if (chosen.length >= MAX || (c.chrome && chromeCount >= MAX_CHROME)) { left.push(c); continue; }
    if (c.chrome) chromeCount++;
    chosen.push(c);
  }
  // Az űrlap lejjebb lévő mezői és gombjai (az ablak alatt, vagy ami nem fért ki) külön, röviden: így egy hosszú
  // űrlap végén a küldőgomb sem vész el. A kattintás és a gépelés úgyis odagörget hozzájuk.
  const pool = beyond.concat(left.filter((c) => c.below)).filter((c) => c.form);
  pool.sort((a, b) => a.order - b.order);
  const below = pool.filter((c) => BUTTON.test(c.kind)).slice(-3);
  for (const c of pool) {
    if (below.length >= BELOW_MAX) break;
    if (!below.includes(c)) below.push(c);
  }
  below.sort((a, b) => a.order - b.order);
  const dropped = left.filter((c) => !below.includes(c));
  const droppedChrome = dropped.filter((c) => c.chrome).length;
  const belowMore = pool.length - below.length;
  // A modell a lap sorrendjében könnyebben tájékozódik, ezért visszarendezzük.
  chosen.sort((a, b) => a.order - b.order);
  for (const c of chosen.concat(below).sort((a, b) => a.order - b.order)) {
    let id = state.ids.get(c.el);
    if (!id) {
      id = String(++state.seq);
      state.ids.set(c.el, id);
    }
    c.id = parseInt(id, 10);
    c.el.setAttribute('data-jarvis-id', id);
  }

  // Választók (rádiógomb, jelölőnégyzet, opció) csoportja és kérdése: a fieldset címe, a csoport felirata,
  // vagy a csoport előtti cím (pl. „Állapot”, „Szín”).
  const GROUP = 'fieldset, [role=radiogroup], [role=group], [role=listbox]';
  const CONTROL = 'input:not([type=hidden]), select, textarea, button, [role=radio], [role=checkbox], ' +
                  '[role=option], [role=combobox], [role=switch]';
  const HEADING = 'h1, h2, h3, h4, h5, h6, legend, label, strong, b, [class*=label i], [class*=title i], ' +
                  '[class*=heading i]';
  const titleBefore = (el) => {
    let node = el;
    for (let depth = 0; depth < 4 && node && node.parentElement; depth++) {
      let sib = node.previousElementSibling;
      for (let k = 0; k < 3 && sib; k++, sib = sib.previousElementSibling) {
        if (sib.matches(CONTROL) || sib.querySelector(CONTROL)) return '';
        const t = clean(sib.innerText);
        if (t && t.length <= 60 && (sib.matches(HEADING) || sib.querySelector(HEADING))) return t;
      }
      node = node.parentElement;
    }
    return '';
  };
  const boxTitle = (box) => {
    const aria = clean(box.getAttribute('aria-label')) || byIds(box, box.getAttribute('aria-labelledby'));
    if (aria) return aria;
    const legend = box.tagName === 'FIELDSET' ? box.querySelector(':scope > legend') : null;
    if (legend && clean(legend.innerText)) return clean(legend.innerText);
    if (box.id) {
      const owner = document.querySelector('[aria-controls~="' + CSS.escape(box.id) + '"]');
      if (owner) return nameOf(owner);
    }
    return titleBefore(box);
  };
  const boxes = new Map();
  const named = new Map();
  const groupOf = (el, kind) => {
    const sort = kind.replace('input:', '');
    // a rádiógombok csoportját a nevük adja (egy dobozban több kérdés is lehet)
    const name = el.tagName === 'INPUT' ? el.getAttribute('name') || '' : '';
    const box = el.closest(GROUP);
    if (box) {
      if (!boxes.has(box)) {
        const size = box.querySelectorAll('input[type=radio], input[type=checkbox], [role=radio], [role=checkbox], ' +
                                          '[role=option], [role=switch]').length;
        boxes.set(box, {n: boxes.size, size, question: size > 1 ? cut(boxTitle(box), 60) : ''});
      }
      const b = boxes.get(box);
      return b.size > 1 ? {key: 'g' + b.n + sort + (sort === 'radio' ? ':' + name : ''), question: b.question} : null;
    }
    if (!name) return null;
    const key = sort + ':' + name;
    if (!named.has(key)) {
      const members = Array.from(el.getRootNode().querySelectorAll('input[name="' + CSS.escape(name) + '"]'))
        .filter((x) => x.type === el.type);
      // a kérdés a csoport első tagja előtt áll (akkor is, ha az épp nem fért a nézetbe)
      const question = members.length > 1 ? cut(titleBefore(members[0]), 60) : '';
      named.set(key, {key: 'n' + named.size + sort, size: members.length, question});
    }
    const g = named.get(key);
    return g.size > 1 ? g : null;
  };
  // A választó saját, rövid felirata: a címke első sora, a beágyazott magyarázat (small, leírás) nélkül.
  const SUBTEXT = 'small, [class*=desc i], [class*=help i], [class*=hint i], [class*=subtitle i], ' +
                  '[class*=caption i], [class*=note i]';
  const shortOf = (el, name) => {
    if (clean(el.getAttribute('aria-label'))) return cut(name, 40);
    let src = el.labels && el.labels.length ? el.labels[0] : null;
    const by = el.getAttribute('aria-labelledby');
    if (!src && by) {
      const root = el.getRootNode();
      const first = by.split(/\s+/)[0];
      src = (root.getElementById && root.getElementById(first)) || document.getElementById(first);
    }
    if (!src && el.tagName !== 'INPUT') src = el;
    if (!src) return cut(name, 40);
    let text = (src.innerText || '').split('\n').map(clean).find(Boolean) || '';
    for (const sub of src.querySelectorAll(SUBTEXT)) {
      const t = clean(sub.innerText);
      if (t && t.length < text.length && text.includes(t)) text = clean(text.replace(t, ''));
    }
    return cut(text || name, 40);
  };
  // A mezőt körülölelő címke saját szövege, a lenyíló lista lehetőségei nélkül (a teljes felirat a felvételé marad).
  const ownLabel = (el) => {
    const l = el.labels && el.labels.length ? el.labels[0] : null;
    if (!l || !l.contains(el) || el.getAttribute('aria-label') || el.getAttribute('aria-labelledby')) return '';
    const copy = l.cloneNode(true);
    copy.querySelectorAll('select, textarea, option, [role=listbox]').forEach((n) => n.remove());
    return clean(copy.textContent);
  };
  // Hirdetéskártya ára a hirdetés linkje mellé (ha a link felirata nem mondja): a kártya első, nem profil-linkje
  // kapja, az eladó profiljára mutató link nem. Mint az árkutatásnál (LISTINGS_JS): az áthúzott (régi) ár nem
  // számít, és ha a kártyán villámár vagy fix ár is van, az az ár.
  const PRICE = /(\d{1,3}(?:[ \u00a0\u202f.]\d{3})+|\d+)(?:,\d{1,2})?\s?(?:Ft|HUF)(?![a-zá-ű])/i;
  const PRICE_FIRST = /(?:Ft|HUF)\s?\d[\d \u00a0\u202f.]*\d/;
  const FIXED = new RegExp('(?:villámár|fix ?ár|azonnal megvehető|buy it now)\\D{0,4}(' + PRICE.source + ')', 'i');
  const PROFILE = /\/(member|members|user|users|profil|profile|seller|felhasznalo)(\/|$|\?)/i;
  const CARD = 'li, article, [class*=card i], [class*=item i], [class*=product i]';
  const struck = (el) => el.matches('s, del, strike') ||
                         (getComputedStyle(el).textDecorationLine || '').includes('line-through');
  const priceOf = (el, name) => {
    if (PRICE.test(name) || PRICE_FIRST.test(name)) return '';
    const card = el.parentElement && el.parentElement.closest(CARD);
    if (!card) return '';
    let text = clean(card.innerText);
    if (text.length > 400 || !(PRICE.test(text) || PRICE_FIRST.test(text))) return '';
    const links = Array.from(card.querySelectorAll('a[href]'));
    const first = links.find((a) => !PROFILE.test(a.getAttribute('href') || ''));
    if (first !== el) return '';
    for (const node of card.querySelectorAll('*')) {
      const old = struck(node) ? clean(node.innerText) : '';
      if (old) text = text.replace(old, ' ');
    }
    const fixed = text.match(FIXED);
    const m = fixed ? [fixed[1]] : (text.match(PRICE) || text.match(PRICE_FIRST));
    return m ? clean(m[0]) : '';
  };

  const noValue = ['password', 'checkbox', 'radio', 'submit', 'button', 'reset', 'image', 'file'];
  const itemOf = (c, inView) => {
    const el = c.el;
    const item = {id: c.id, kind: c.kind, label: c.name.slice(0, 100), inView};
    if (c.chrome) item.chrome = true;
    if (el.tagName === 'A') {
      item.href = (el.getAttribute('href') || '').slice(0, 120);
      if (typeof el.href === 'string') item.url = el.href.slice(0, 600);   // a feloldott, teljes cím
      const price = c.chrome ? '' : priceOf(el, c.name);
      if (price) item.price = price;
    }
    if ((el.tagName === 'INPUT' && !noValue.includes(el.type)) || el.tagName === 'TEXTAREA') {
      item.value = (el.value || '').slice(0, 60);
    }
    if (el.type === 'checkbox' || el.type === 'radio') item.checked = !!el.checked;
    if (el.getAttribute('aria-checked')) item.checked = el.getAttribute('aria-checked') === 'true';
    if (c.kind === 'option' && el.getAttribute('aria-selected') === 'true') item.checked = true;
    if (el.isContentEditable) item.editable = true;
    const expanded = el.getAttribute('aria-expanded');
    if (expanded === 'true' || expanded === 'false') item.expanded = expanded === 'true';
    if (el.required || el.getAttribute('aria-required') === 'true') item.required = true;
    let invalid = el.getAttribute('aria-invalid') === 'true';
    try { invalid = invalid || el.matches(':user-invalid'); } catch (e) { /* régi böngésző */ }
    if (invalid) item.invalid = true;
    if (el.getAttribute('aria-disabled') === 'true') item.disabled = true;
    if (el.tagName === 'SELECT' || el.tagName === 'TEXTAREA') {
      const own = ownLabel(el);
      if (own && own !== item.label) item.short = cut(own, 60);
    }
    if (el.tagName === 'SELECT') {
      item.select = true;
      const picked = el.options[el.selectedIndex];
      item.value = picked ? clean(picked.text).slice(0, 60) : '';
      const all = Array.from(el.options).map((o) => clean(o.text).slice(0, 40));
      if (all.length > 12) {
        // Sok lehetőség: elöl azok, amelyek a hirdetés szavaival (BrowserController.hints) egyeznek.
        const score = (t) => {
          const words = fold(t).split(/[^a-z0-9]+/).filter(Boolean);
          const hit = (h) => words.some((w) => w.startsWith(h) || (w.length >= 4 && h.startsWith(w)));
          return HINTS.filter(hit).length;
        };
        const ranked = all.map((t, i) => ({t, i, s: HINTS.length ? score(t) : 0}));
        ranked.sort((a, b) => b.s - a.s || a.i - b.i);
        item.options = ranked.slice(0, 12).map((o) => o.t);
        item.more = all.length - 12;
      } else {
        item.options = all;
      }
    }
    if (CHOICE.test(c.kind)) {
      const group = groupOf(el, c.kind);
      if (group) {
        item.group = group.key;
        if (group.question) item.question = group.question;
      }
      const short = shortOf(el, c.name);
      if (short && short !== item.label) item.short = short;
    }
    if (sensitive(el)) item.sensitive = true;
    const fieldName = (el.getAttribute('name') || '') + ' ' + (el.id || '');
    if (fieldName.trim()) item.field = fieldName.trim().slice(0, 40);
    return item;
  };
  const elements = chosen.map((c) => itemOf(c, c.inView));
  const belowItems = below.map((c) => itemOf(c, false));

  // A képernyőn látható szöveg: előbb a fő tartalom, a menük szövege csak a maradék helyre. A felsorolt elemek
  // felirata (link, gomb, mező címkéje) nem kerül bele még egyszer, az elemlistában úgyis ott van; a menükből
  // csak a nem kattintható szöveg.
  const listed = new Set();
  chosen.forEach((c, i) => {
    // a felirata nem fért ki: a szövegben marad
    if ((elements[i].short || c.name).length > 100 && !CHOICE.test(c.kind)) return;
    listed.add(c.el);
    if (c.el.labels) for (const l of c.el.labels) listed.add(l);
    const by = c.el.getAttribute('aria-labelledby');
    for (const id of by ? by.split(/\s+/) : []) {
      const home = c.el.getRootNode();
      const n = (home.getElementById && home.getElementById(id)) || document.getElementById(id);
      if (n) listed.add(n);
    }
  });
  const isListed = (node) => {
    for (let n = node; n; n = n.parentElement) if (listed.has(n)) return true;
    return false;
  };
  const ACTIVE = 'a[href], button, [role=button], [role=link], [role=menuitem], [role=tab]';
  const root = document.body || document.documentElement;
  const shown = new Map();
  const collect = (wantChrome, budget) => {
    const parts = [];
    let total = 0;
    const walk = (start) => {
      const walker = document.createTreeWalker(start, NodeFilter.SHOW_TEXT, {
        acceptNode(node) {
          const p = node.parentElement;
          if (!p || ['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE'].includes(p.tagName)) {
            return NodeFilter.FILTER_REJECT;
          }
          return clean(node.textContent) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
        }
      });
      while (total < budget && walker.nextNode()) {
        const node = walker.currentNode;
        const p = node.parentElement;
        if (!!closestDeep(p, CHROME) !== wantChrome || isListed(p)) continue;
        if (wantChrome && p.closest(ACTIVE)) continue;
        let ok = shown.get(p);
        if (ok === undefined) {
          const r = p.getBoundingClientRect();
          ok = r.bottom > 0 && r.top < vh && r.width > 0 && r.height > 0 &&
               (typeof p.checkVisibility !== 'function' || p.checkVisibility({visibilityProperty: true}));
          shown.set(p, ok);
        }
        if (!ok) continue;
        const t = clean(node.textContent);
        parts.push(t);
        total += t.length + 1;
      }
    };
    walk(root);
    for (const host of hosts) if (total < budget) walk(host.shadowRoot);   // az árnyék-DOM szövege a végén
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
    below: belowItems,
    dropped: dropped.length,
    droppedChrome,
    belowMore,
    newDoc,
    renumbered,
    seqBefore,
    errors: (""" + FORM_ERRORS_JS + r""")(),
  };
}
"""


# Keresési találati oldal (Vinted, Jófogás…) hirdetései: minden hirdetéskártya szövege, ára és
# címe. Nem oldalspecifikus: a linkektől felfelé haladva megkeresi a legkisebb elemet, amelyben
# ár is van (ez a kártya), és kártyánként egyszer veszi fel. Az áthúzott (régi) ár nem számít; ha a
# kártyán villámár vagy fix ár is van, az az ár; a licites kártya jelzést kap (auction), mert az ott
# látható ár csak a mostani ajánlat.
LISTINGS_JS = r"""
() => {
  const PRICE = /(\d{1,3}(?:[   .]\d{3})+|\d{2,7})(?:[,.]\d{1,2})?\s*(?:Ft|HUF|forint)(?![a-zá-ű])/i;
  const FIXED = new RegExp('(?:villámár|fix ?ár|azonnal megvehető|buy it now)\\D{0,4}' + PRICE.source, 'i');
  const AUCTION = /(licit|aukció|árverés|jelenlegi ár|kikiáltási ár|\bbids?\b|auction)/i;
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const struck = (el) => el.matches('s, del, strike') ||
                         (getComputedStyle(el).textDecorationLine || '').includes('line-through');
  const priceText = (node) => {   // a kártya szövege az áthúzott árak nélkül
    let text = clean(node.innerText);
    for (const el of node.querySelectorAll('*')) {
      const old = struck(el) ? clean(el.innerText) : '';
      if (old) text = text.replace(old, ' ');
    }
    return clean(text);
  };
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
    const text = priceText(card.node);
    const fixed = text.match(FIXED);
    const m = fixed || text.match(PRICE) || card.label.match(PRICE);
    if (!m) continue;
    const price = parseInt(m[1].replace(/[^\d]/g, ''), 10);
    if (!(price > 0 && price < 10000000)) continue;
    seen.add(card.href);
    const summary = (card.label.length > 15 && !text.includes(card.label)) ? card.label + ' | ' + text : text;
    const entry = {text: summary.slice(0, 240), price, url: card.href};
    if (card.label.length > 3 && card.label.length <= 160 && !PRICE.test(card.label)) entry.title = card.label;
    if (!fixed && AUCTION.test(text)) entry.auction = true;
    out.push(entry);
    if (out.length >= 40) break;
  }
  return out;
}
"""

# Egy hirdetés saját oldala a háttérellenőrzéshez (listings.py): a fő tartalom látható szövege a gombok,
# linkek, mezők és menük nélkül (így a saját hirdetéseden lévő „Megjelölés eladottként” gomb nem számít
# „Eladva” jelzésnek), a rövid szövegdarabok külön (egy „Eladva” címke ilyen), és a gombok, linkek
# feliratai (ebből látszik, ha ki vagy jelentkezve).
LISTING_PAGE_JS = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const SKIP = 'header, nav, footer, aside, [role=banner], [role=navigation], [role=contentinfo], script, style, ' +
               'noscript, template, a, button, [role=button], [role=link], [role=menuitem], input, select, ' +
               'textarea, option, label';
  const root = document.querySelector('main, [role=main]') || document.body || document.documentElement;
  const shown = new Map();
  const parts = [], short = [];
  let total = 0;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  while (total < 4000 && walker.nextNode()) {
    const p = walker.currentNode.parentElement;
    const t = clean(walker.currentNode.textContent);
    if (!p || !t || p.closest(SKIP)) continue;
    let ok = shown.get(p);
    if (ok === undefined) {
      ok = typeof p.checkVisibility !== 'function' || p.checkVisibility({visibilityProperty: true});
      shown.set(p, ok);
    }
    if (!ok) continue;
    parts.push(t);
    total += t.length + 1;
    if (t.length <= 40 && short.length < 200) short.push(t);
  }
  const labels = [];
  for (const el of document.querySelectorAll('a, button, [role=button]')) {
    const t = clean(el.innerText || el.getAttribute('aria-label'));
    if (t && t.length <= 40) labels.push(t);
    if (labels.length >= 150) break;
  }
  // a hirdetés fotói (a képellenőrzéshez): a megosztáskor mutatott kép, majd a fő tartalom nagy, látható képei
  const images = [];
  const shared = document.querySelector('meta[property="og:image"], meta[name="twitter:image"]');
  if (shared && shared.content) images.push(shared.content);
  for (const img of root.querySelectorAll('img')) {
    const src = img.currentSrc || img.src || '';
    if (!/^https?:/.test(src) || /logo|icon|avatar|sprite|placeholder|badge|flag/i.test(src)) continue;
    if ((img.naturalWidth || img.width) < 200 || (img.naturalHeight || img.height) < 200) continue;
    if (!images.includes(src)) images.push(src);
    if (images.length >= 4) break;
  }
  return {title: document.title, url: location.href, text: parts.join(' ').slice(0, 4000), short, labels,
          images: images.slice(0, 4), captcha: """ + CAPTCHA_FRAME_JS + r"""};
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
  document.querySelectorAll('[data-jarvis-loc="' + want.id + '"]').forEach((e) => e.removeAttribute('data-jarvis-loc'));
  const el = pick.el;
  el.setAttribute('data-jarvis-loc', String(want.id));
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
# tartományban (SCAN_BASE-től) és külön attribútumban vannak, így az oldalnézet nem írja felül őket; minden
# letapogatás az előzőét törli.
SCAN_JS = r"""
(opts) => {
""" + _JS_HELPERS + r"""
  const BASE = opts.base;
  document.querySelectorAll('[data-jarvis-scan]').forEach((el) => el.removeAttribute('data-jarvis-scan'));
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
    el.setAttribute('data-jarvis-scan', String(id));
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

# Felvétel: hányadik az elem a hozzá hasonlók (ugyanolyan fajta és felirat) között az egész oldalon.
ORDINAL_JS = r"""
(selector) => {
""" + _JS_HELPERS + r"""
  const target = document.querySelector(selector);
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

# A lap változásainak figyelése (lásd _settle): egy MutationObserver számolja a DOM változásait, és jegyzi az
# utolsó idejét. A context.add_init_script minden új dokumentumban az oldal saját szkriptjei előtt indítja, így a
# betöltés közbeni változásokat is látja; a beágyazott keretekben (pl. reklámok) nem indul el, ott nincs rá szükség.
# A stílus változásait nem nézi: egy folyamatos animáció (pl. egy körhinta) különben soha nem engedné megnyugodni
# a lapot.
_QUIET_INSTALL = r"""
  if (!window.__jarvisQuiet) {
    const state = {count: 0, last: performance.now()};
    Object.defineProperty(window, '__jarvisQuiet', {value: state});
    try {
      new MutationObserver((records) => { state.count += records.length; state.last = performance.now(); })
        .observe(document, {subtree: true, childList: true, characterData: true, attributes: true,
                            attributeFilter: ['class', 'hidden', 'open', 'disabled', 'aria-busy', 'aria-expanded',
                                              'aria-hidden', 'aria-checked', 'aria-selected']});
    } catch (e) {}
  }
"""
QUIET_INIT_JS = "(() => {\n  if (window !== window.top) return;" + _QUIET_INSTALL + "})();"

# Egy pillanatkép a lap nyugalmáról: mióta nem változik (de legfeljebb amióta várunk), új dokumentum-e, és ha már
# csendes (vagy régóta nyugtalan), a szöveg hossza, a „Betöltés…” felirat és a rövid animációk (pl. egy lenyíló
# lista beúszása). Az innerText a nagy oldalakon drága, ezért csak akkor olvassuk ki, ha kell.
_SETTLE_JS = r"""
(opts) => {
""" + _QUIET_INSTALL + r"""
  const q = window.__jarvisQuiet;
  const fresh = performance.timeOrigin !== opts.doc;
  const windowSec = fresh ? opts.longQuiet : opts.quiet;
  const quietFor = Math.min((performance.now() - q.last) / 1000, opts.elapsed);
  const res = {doc: performance.timeOrigin, count: q.count, quietFor, window: windowSec, size: -1,
               loading: false, moving: false, young: performance.now() / 1000 < opts.minAge};
  const body = document.body;
  if (!body) { res.loading = true; return res; }
  if (!opts.churn && (quietFor < windowSec || res.young)) return res;
  const text = body.innerText;
  res.size = text.length;
  res.loading = /(^|\s)(betöltés|töltés|loading)\s*(…|\.\.\.)/i.test(text.slice(0, 20000)) ||
                !!document.querySelector('[aria-busy="true"]');
  if (typeof document.getAnimations === 'function') {
    res.moving = document.getAnimations().some((a) => {
      if (a.playState !== 'running' || !a.effect) return false;
      const t = a.effect.getComputedTiming();
      return isFinite(t.endTime) && t.endTime - (t.localTime || 0) < 1000;
    });
  }
  return res;
}
"""

# Írás előtt: milyen az elem valójában (szövegmező, lenyíló lista, jelölőnégyzet, vagy egyik sem).
_WRITE_INFO_JS = r"""
(el) => {
  const tag = el.tagName;
  const type = (el.type || '').toLowerCase();
  const role = (el.getAttribute('role') || '').toLowerCase();
  const NOT_TEXT = ['checkbox', 'radio', 'submit', 'button', 'reset', 'image', 'file', 'range', 'color'];
  const info = {select: tag === 'SELECT', check: false, checked: false, radio: false, text: false,
                readonly: !!el.readOnly || el.getAttribute('aria-readonly') === 'true'};
  if (tag === 'INPUT' && (type === 'checkbox' || type === 'radio')) {
    info.check = true; info.checked = !!el.checked; info.radio = type === 'radio';
  } else if (['checkbox', 'radio', 'switch', 'menuitemcheckbox', 'menuitemradio', 'option'].includes(role) ||
             el.hasAttribute('aria-checked') || el.hasAttribute('aria-pressed')) {
    info.check = true;
    info.radio = ['radio', 'menuitemradio', 'option'].includes(role);
    info.checked = ['aria-checked', 'aria-pressed', 'aria-selected'].some((a) => el.getAttribute(a) === 'true');
  }
  info.text = !info.check && !info.select && !el.disabled &&
              ((tag === 'INPUT' && !NOT_TEXT.includes(type)) || tag === 'TEXTAREA' || el.isContentEditable);
  return info;
}
"""

# Kattintás után: hová került a fókusz (abba gépelünk). A „suggests”: javaslatos, kereső jellegű mező.
_FOCUS_JS = r"""
(target) => {
  let el = document.activeElement;
  while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
  if (!el || el === document.body) return {editable: false};
  const tag = el.tagName;
  const type = (el.type || '').toLowerCase();
  const NOT_TEXT = ['checkbox', 'radio', 'submit', 'button', 'reset', 'image', 'file', 'range', 'color'];
  const editable = !el.disabled && !el.readOnly &&
                   ((tag === 'INPUT' && !NOT_TEXT.includes(type)) || tag === 'TEXTAREA' || el.isContentEditable);
  const suggests = type === 'search' || (el.getAttribute('role') || '') === 'combobox' ||
                   ['aria-autocomplete', 'aria-haspopup', 'aria-controls', 'aria-owns', 'list']
                     .some((a) => el.hasAttribute(a));
  return {editable, same: el === target, suggests, input: tag === 'INPUT' || tag === 'TEXTAREA',
          value: tag === 'INPUT' || tag === 'TEXTAREA' ? el.value : ''};
}
"""

_SELECT_OPTIONS_JS = r"""
(el) => Array.from(el.options).map((o) => ({text: (o.text || '').replace(/\s+/g, ' ').trim(), value: o.value,
                                            disabled: !!o.disabled}))
"""

# browser_do: a modell által látott elem még ugyanaz-e (a lap nem rajzolta-e újra), és újra megjelöli a számával.
_IDENTITY_JS = r"""
(el, id) => {
""" + _JS_HELPERS + r"""
  if (!el.isConnected) return {connected: false};
  el.setAttribute('data-jarvis-id', String(id));
  return {connected: true, kind: kindOf(el), label: nameOf(el).slice(0, 100)};
}
"""

# Hány felugró ablak (párbeszédablak) látszik: ha egy lépés után több lett, a browser_do megáll.
_DIALOGS_JS = r"""
() => {
""" + _JS_HELPERS + r"""
  return Array.from(document.querySelectorAll('dialog[open], [role=dialog], [role=alertdialog], [aria-modal="true"]'))
    .filter(visible).length;
}
"""


class BrowserError(Exception):
    """Érthető hibaüzenet böngészős műveletnél."""


class Covered(Exception):
    """Egy másik elem (nyitva maradt lenyíló lista, felugró ablak, süti-sáv) eltakarja, amire kattintana."""


@dataclass
class _Written:
    """Egy mező kitöltésének eredménye (lásd _write)."""
    kind: str             # text (beírta) | select (kiválasztotta) | check (bejelölte vagy kivette a jelölést)
    shown: str            # röviden, mit állított be (a megjegyzésbe)
    locator: Any = None
    react: float = 0.0    # utána legalább ennyit várjon az oldal reakciójára (pl. javaslatok)
    clicked: bool = False   # jelölőnégyzetnél: rá kellett-e kattintani (vagy már jó állásban volt)
    declined: bool = False  # a kattintást nem engedélyezted


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


def is_paid(label: str) -> bool:
    """Fizetős extra-e (kiemelés, előresorolás, Bump): a többlépéses eszközök ezekre soha nem kattintanak."""
    return bool(_PAID_RE.search(fold(label or "")))


def site_of(url: str) -> str:
    """A webcím „oldala” (egyszerűsített regisztrálható tartomány): www.vinted.hu → vinted.hu, a.b.co.uk → b.co.uk."""
    try:
        host = (urllib.parse.urlsplit(url or "").hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    labels = host.split(".")
    if len(labels) <= 2 or re.fullmatch(r"[\d.]+", host) or ":" in host:
        return host
    take = 3 if len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL else 2
    return ".".join(labels[-take:])


def same_site(url: str, document_url: str) -> bool:
    """Ugyanannak az oldalnak a kérése-e (az api.vinted.hu a www.vinted.hu-é igen, egy reklámszerveré nem).
    Ha valamelyik nem webcím (pl. about:blank), nem tudjuk eldönteni: akkor igen."""
    first, second = site_of(url), site_of(document_url)
    return not first or not second or first == second


def _norm(text: str) -> str:
    """Összehasonlításhoz: kisbetűs, ékezet és írásjelek nélkül, egy szóközzel."""
    return " ".join(re.sub(r"[^\w\s]", " ", fold(text or "")).split())


def match_option(labels: list[str], wanted: str) -> tuple[int | None, list[int]]:
    """A kért lehetőség egy lista feliratai közül, ékezet és kis-nagybetű nélkül: a pontos egyezés, az egyetlen
    ezzel kezdődő, az egyetlen, amelyben egy szó így kezdődik, végül a legtöbb közös szót tartalmazó (ha az
    egyértelmű). Visszaadja a választott indexét (vagy None-t, ha nincs vagy nem egyértelmű) és a jelölteket."""
    want = _norm(wanted)
    if not want:
        return None, []
    norms = [_norm(label) for label in labels]
    exact = [i for i, n in enumerate(norms) if n == want]
    if exact:
        return exact[0], exact
    word_start = re.compile(r"(?<!\w)" + re.escape(want))
    for hits in ([i for i, n in enumerate(norms) if n.startswith(want)],
                 [i for i, n in enumerate(norms) if word_start.search(n)]):
        if len(hits) == 1:
            return hits[0], hits
        if hits:
            return None, hits
    tokens = set(want.split())
    scores = [len(tokens & set(n.split())) for n in norms]
    best = max(scores, default=0)
    if not best:
        return None, []
    top = [i for i, score in enumerate(scores) if score == best]
    return (top[0] if len(top) == 1 else None), top


_ON_WORDS = {"igen", "i", "true", "x", "✓", "✔", "be", "bekapcsol", "bekapcsolva", "bejelol", "bejelolve", "on",
             "yes", "1", "pipa", "kell", "valaszt", "kivalaszt", "kivalasztva"}
_OFF_WORDS = {"nem", "n", "false", "ki", "kikapcsol", "kikapcsolva", "off", "no", "0", "nem kell", "torol", "ures"}


def wanted_state(text: str, label: str) -> bool | None:
    """Be vagy ki legyen-e egy jelölőnégyzet (választógomb, kapcsoló) a szöveg szerint: igen/x/✓/be, vagy a saját
    felirata: be; nem/ki: ki. None, ha nem lehet eldönteni."""
    word = _norm(text) or (text or "").strip()
    if word in _ON_WORDS:
        return True
    if word in _OFF_WORDS:
        return False
    own = _norm(label)
    if word and own and (word.replace(" ", "") == own.replace(" ", "") or own.startswith(word)
                         or word.startswith(own)):
        return True
    return None


# browser_choose: ami egy kinyitott listában választható (az onclick-es div, li is), és a lista vezérlőgombjai. A
# begyakorolt lépések (replay.is_choice_option) ugyanezeket fogadják el.
CHECK_KINDS = {"checkbox", "input:checkbox", "switch"}   # bepipálható elemek (nem gombok)
OPTION_KINDS = {"button", "radio", "input:radio", "checkbox", "input:checkbox", "option", "menuitem",
                "menuitemradio", "menuitemcheckbox", "switch", "link", "input:button", "div", "li", "span",
                "label", "td", "p", "treeitem", "gridcell"}
_FINAL_KINDS = {"radio", "input:radio", "checkbox", "input:checkbox", "option", "menuitemradio",
                "menuitemcheckbox"}
_TEXT_KINDS = {"input:text", "input:search", "searchbox", "textbox", "combobox", "textarea"}
CONTROL_RE = re.compile(r"^(vissza|back|bezar|bezaras|close|megse|cancel|torles|clear|clear all|kesz|done|ok|"
                        r"rendben|alkalmaz|apply|mentes|save|tovabb|next|elozo|previous|kereses|search|"
                        r"(back to|vissza a|vissza az) .*)$")
_DONE_RE = re.compile(r"^(kesz|done|ok|rendben|alkalmaz|apply|bezar|bezaras|close)$")


def _elem_key(el: dict[str, Any]) -> tuple[str, str, str]:
    """Egy elem azonosítása két letapogatás között (a szám közben megváltozik)."""
    return el.get("kind", ""), _norm(el.get("label", ""))[:60], el.get("field", "")


def _appeared(before: Counter, elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ami a letapogatás óta jelent meg (ha egy felirat korábban is ott volt, a későbbi előfordulás az új)."""
    seen: Counter = Counter()
    fresh = []
    for el in elements:
        key = _elem_key(el)
        seen[key] += 1
        if seen[key] > before.get(key, 0):
            fresh.append(el)
    return fresh


def distinct_options(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ha egy felirat kétszer is szerepel (pl. a lista sora és a benne lévő gomb), csak egyszer, a pontosabbal: a
    választógomb, jelölőnégyzet, opció előbb, aztán a gomb, végül a többi (div, li…). A sorrend marad."""
    def rank(item: dict[str, Any]) -> int:
        return 0 if item.get("kind") in _FINAL_KINDS else 1 if item.get("kind") in ("button", "option") else 2

    best: dict[str, dict[str, Any]] = {}
    for item in found:
        key = _norm(item.get("label", ""))
        if key not in best or rank(item) < rank(best[key]):
            best[key] = item
    return [item for item in found if best.get(_norm(item.get("label", ""))) is item]


def _is_option(el: dict[str, Any]) -> bool:
    """Választható lehetőség-e egy kinyitott listában (a mező, a vezérlőgomb és a fejléc nem)."""
    if el.get("chrome") or el.get("disabled") or el.get("kind", "") not in OPTION_KINDS:
        return False
    label = _norm(el.get("label", ""))
    return bool(label) and not CONTROL_RE.match(label)


def _short(exc: BaseException | None) -> str:
    text = str(exc or "").strip().splitlines()
    return text[0][:200] if text else type(exc).__name__


def id_selector(number: Any) -> str:
    """Az elem CSS-választója a száma alapján. Az oldalnézet (1-től), a letapogatás (SCAN_BASE-től) és a
    visszajátszás (LOCATE_BASE-től) számai külön attribútumban vannak, így egymást nem írják felül: egy
    letapogatás után készült oldalnézet mellett a letapogatás számai is érvényesek maradnak."""
    n = int(number)
    if n >= LOCATE_BASE:
        return f'[data-jarvis-loc="{n}"]'
    if n >= SCAN_BASE:
        return f'[data-jarvis-scan="{n}"]'
    return f'[data-jarvis-id="{n}"]'


def _readable_path(path: str) -> str:
    """Az ékezetes, %-kódolt útvonal olvasható alakja (ha a visszafejtés nem változtat a jelentésén)."""
    text = urllib.parse.unquote(path)
    return path if any(ch in text for ch in "?# ") else text


def short_href(el: dict[str, Any], page_url: str = "") -> str:
    """A link címe az oldalnézetben: azonos oldalon csak az útvonal, máshova a gépnév és az útvonal, a lekérdezésből
    csak a keresés, lapozás, rendezés, kategória (_HREF_KEEP), legfeljebb HREF_CHARS karakter. A menük (fejléc,
    navigáció, lábléc) linkjeinél semmi: azokat a feliratuk úgyis megmondja. Üres, ha nincs mit mutatni."""
    raw = str(el.get("href") or "").strip()
    if not raw or raw.startswith(("#", "javascript")) or el.get("chrome"):
        return ""
    full = str(el.get("url") or "") or urllib.parse.urljoin(page_url, raw)
    try:
        parts = urllib.parse.urlsplit(full)
        here = urllib.parse.urlsplit(page_url) if page_url else None
    except ValueError:
        parts, here = None, None
    if parts is None or parts.scheme not in ("http", "https"):
        text = full
    else:
        kept = [(key, value) for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                if key.lower() in _HREF_KEEP]
        query = "&".join(f"{key}={value.replace('&', '%26').replace('#', '%23').replace(' ', '+')}"
                         for key, value in kept)
        path = _readable_path(parts.path or "/")
        same = here is not None and parts.netloc.lower() == here.netloc.lower()
        text = (path if same else parts.netloc + path) + (f"?{query}" if query else "")
    return text if len(text) <= HREF_CHARS else text[:HREF_CHARS - 1] + "…"


_STATE_NAMES = (("required", "kötelező"), ("invalid", "hibás"), ("disabled", "tiltva"))
# Az új elemek csillagának magyarázata az oldalnézet fejlécében (lásd _mark).
NEW_LEGEND = "; *: új az előző nézet óta"


def _states(el: dict[str, Any], skip: frozenset[str] = frozenset()) -> str:
    """Az elem állapota röviden: nyitva/csukva, kötelező, hibás, tiltva (a `skip`-beliek nélkül)."""
    text = ""
    if "expanded" in el:
        text += " (nyitva)" if el["expanded"] else " (csukva)"
    for key, name in _STATE_NAMES:
        if el.get(key) and key not in skip:
            text += f" ({name})"
    return text


def _mark(el: dict[str, Any]) -> str:
    """Az elem száma; a csillag jelzi, hogy az előző nézet óta jelent meg (ugyanazon az oldalon)."""
    return f"{'*' if el.get('new') else ''}[{el['id']}]"


def format_element(el: dict[str, Any], page_url: str = "") -> str:
    text = f"{_mark(el)} {KIND_NAMES.get(el['kind'], el['kind'])}"
    label = el.get("short") or el.get("label") or ""
    if el.get("price"):
        label = f"{label} · {el['price']}" if label else el["price"]
    if label:
        text += f' "{label}"'
    if el.get("question"):
        text += f" (csoport: {el['question']})"
    href = short_href(el, page_url)
    if href:
        text += f" -> {href}"
    if el.get("select"):
        text += f' (kiválasztva: "{el.get("value", "")}"'
        if el.get("options"):
            text += "; lehetőségek: " + ", ".join(el["options"])
            if el.get("more"):
                text += f" (+{el['more']} további; írd be a nevét)"
        text += ")"
    elif el.get("value"):
        text += f' (érték: "{el["value"]}")'
    if "checked" in el:
        text += " [bejelölve]" if el["checked"] else " [nincs bejelölve]"
    text += _states(el)
    if el.get("sensitive"):
        text += " (érzékeny adat, ezt nem töltöm ki)"
    return text


# Egy csoport választói egy sorban: „Szín (jelölőnégyzetek): [30] Fekete, [31] Barna ✓, …”.
_CHOICE_PLURAL = {"input:radio": "választógombok", "radio": "választógombok", "input:checkbox": "jelölőnégyzetek",
                  "checkbox": "jelölőnégyzetek", "option": "opciók", "switch": "kapcsolók"}


def format_choices(run: list[dict[str, Any]]) -> str:
    """Egy csoport egymás utáni választói (rádiógombok, jelölőnégyzetek, opciók) egy sorban, a kérdésükkel; a
    bejelöltek mellett ✓. Minden szám külön kattintható."""
    plural = _CHOICE_PLURAL.get(run[0].get("kind", ""), "lehetőségek")
    question = run[0].get("question") or ""
    # a mindegyikre igaz állapot (pl. kötelező) egyszer, a csoport címénél
    common = frozenset(key for key, _ in _STATE_NAMES if all(el.get(key) for el in run))
    parts = []
    for el in run:
        label = el.get("short") or el.get("label") or "?"
        text = f"{_mark(el)} " + (f'"{label}"' if "," in label else label)
        if el.get("checked"):
            text += " ✓"
        parts.append(text + _states(el, common))
    head = f"{question} ({plural})" if question else plural[:1].upper() + plural[1:]
    head += "".join(f" ({name})" for key, name in _STATE_NAMES if key in common)
    return f"{head}: " + ", ".join(parts)


def format_elements(elements: list[dict[str, Any]], page_url: str = "") -> list[str]:
    """Az elemek sorai; egy csoport egymás utáni választói egy sorba kerülnek (format_choices)."""
    lines, index = [], 0
    while index < len(elements):
        group, end = elements[index].get("group"), index + 1
        if group and elements[index].get("kind") in _CHOICE_PLURAL:
            while end < len(elements) and elements[end].get("group") == group:
                end += 1
        if end - index > 1:
            lines.append(format_choices(elements[index:end]))
        else:
            lines.append(format_element(elements[index], page_url))
        index = end
    return lines


def format_snapshot(data: dict[str, Any], note: str = "") -> str:
    lines = [web.UNTRUSTED_NOTE]
    if note:
        lines.append(note)
    lines.append(f"Oldal: {data.get('title') or '(cím nélkül)'}")
    page_url = str(data.get("url") or "")
    lines.append(f"Cím: {page_url}")
    errors = " | ".join(str(text) for text in data.get("errors") or [] if text)
    if errors:   # az űrlap látható hibaüzenetei (FORM_ERRORS_JS), pl. egy kötelező, de üres mező
        lines.append("Hibaüzenetek: " + (errors if len(errors) <= ERRORS_CHARS else errors[:ERRORS_CHARS - 1] + "…"))
    height, view, pos = data.get("scrollHeight", 0), data.get("viewportHeight", 1), data.get("scrollY", 0)
    if height > view * 1.05:
        percent = min(100, round((pos + view) / height * 100))
        more = "lejjebb is van még tartalom" if pos + view < height - 5 else "az oldal alján vagyunk"
        lines.append(f"Görgetés: az oldal {percent}%-áig látszik ({more}).")
    lines += ["", "Látható szöveg:", data.get("text") or "(nincs)", ""]
    elements = data.get("elements") or []
    below = data.get("below") or []
    # A csillag magyarázata egyszer, az első fejlécben.
    legend = NEW_LEGEND if any(el.get("new") for el in elements + below) else ""
    if elements:
        lines.append(f"Kezelhető elemek (a számukkal hivatkozz rájuk{legend}):")
        legend = ""
        outside = [el for el in elements if not el.get("inView", True)]
        lines += format_elements([el for el in elements if el.get("inView", True)], page_url)
        if outside:
            lines.append("A látható részen kívül (görgetés nélkül is elérhetők):")
            lines += format_elements(outside, page_url)
    else:
        lines.append("Nincs kezelhető elem a látható részen.")
    dropped, menu = int(data.get("dropped") or 0), int(data.get("droppedChrome") or 0)
    if dropped:   # a korlát miatt kimaradt elemek (a menük linkjei külön számolva)
        lines.append(f"(+{menu} menüpont nem fért ki)" if menu == dropped else
                     f"(+{dropped} elem nem fért ki" + (f", ebből {menu} menüpont)" if menu else ")"))
    if below:
        lines.append(f"Lejjebb az űrlapon (görgetés nélkül is elérhetők{legend}):")
        lines += format_elements(below, page_url)
        if data.get("belowMore"):
            lines.append(f"(+{data['belowMore']} további űrlapelem lejjebb)")
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
        # Igaz, amíg egy felvétel tart (a WorkDesk állítja): csak ilyenkor számoljuk ki, hányadik az elem a
        # hasonlók között (ORDINAL_JS, az egész lapot bejárja).
        self.recording = False
        # Előre megadott jóváhagyás (pl. a levételt már jóváhagytad): (felirat, webcím) → igaz/hamis.
        self.approve_hook: Callable[[str, str], bool] | None = None
        # Közzététel (kockázatos kattintás) előtt, még a jóváhagyó kérdés előtt: (felirat, webcím). Pl. a Munka
        # menü itt várja ki a két közzététel közti szünetet, így az űrlap a szünet alatt töltődik ki.
        self.before_publish: Callable[[str, str], None] | None = None
        self.capture_error = ""   # a hibacsomaghoz: miért nem készült képernyőkép (capture)
        self._locate_seq = LOCATE_BASE
        # A hirdetés szavai (pl. kategória, állapot, település): egy hosszú lenyíló listából az oldalnézet ezekkel
        # egyező lehetőségeit mutatja elöl. Üres lista: a lista elejét.
        self.hints: list[str] = []
        self._view_url = ""       # az előző oldalnézet címe (az új elemek csillagához)
        self._numbers_url = ""    # az oldal címe, amelyen a letapogatás (scan) és a locate számai érvényesek
        # Emberi tempó (humanize.py). Az `auto` igaz, amíg modell nélküli lépéssor fut (begyakorolt
        # lépések): akkor gondolkodási és olvasási szüneteket is tart, mert nincs modell, ami lassítana.
        self.pace = Pace(getattr(cfg, "human_pace", True), getattr(cfg, "pace", 1.0))
        self.pace.sleep = self._sleep
        self.auto = False
        self._mouse: tuple[float, float] | None = None
        self._starts = 0          # hányszor indult el a böngésző (lásd _restarts_after_crash)
        # A Jarvis kattintásaira megnyílt lapok, sorrendben: ezekből csak az aktív és az előző marad nyitva
        # (a felhasználó által nyitott lapokat soha nem zárja be).
        self._own_tabs: list[Any] = []
        # Igaz, amíg egy többlépéses művelet (browser_choose, browser_do) fut: a közbenső hibákhoz nem kell
        # oldalnézet, a végén úgyis egy készül.
        self._batch = False

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
        try:   # a lap változásainak figyelése minden új dokumentumban (lásd _settle)
            self._context.add_init_script(script=QUIET_INIT_JS)
        except Exception:  # noqa: BLE001 - nélküle is működik: a _settle maga indítja el
            pass
        self._own_tabs = []
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
        self._own_tabs = []

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
        belőle. A CDP-eseményekből nem marad meg semmi, csak a kérés azonosítója, amíg tart.

        Csak a fő keret kérései számítanak, és az adatkérések (XHR, fetch) közül csak az oldal sajátjai: a
        reklámok, a statisztikák és a beágyazott keretek folyton küldenek valamit, és miattuk minden lépés a
        teljes várakozási időt kivárná. Egy másik oldalról jövő szkriptre (pl. a webshop CDN-jéről) csak
        rövid ideig várunk (FOREIGN_SCRIPT_SECONDS)."""
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
        except Exception:  # noqa: BLE001 - pl. nem Chromium: akkor csak a lap megnyugvását figyeljük
            return
        try:
            main = str(session.send("Page.getFrameTree")["frameTree"]["frame"]["id"])
        except Exception:  # noqa: BLE001 - ha nem derül ki, minden keret kérése számít
            main = ""

        def started(params: dict[str, Any]) -> None:
            kind, request_id = params.get("type"), params.get("requestId")
            if kind not in _TRACKED_TYPES or not request_id:
                return
            if main and params.get("frameId") and params["frameId"] != main:
                return            # egy beágyazott keret (pl. reklám) kérése
            when = time.monotonic()
            if kind != "Document" and not same_site((params.get("request") or {}).get("url", ""),
                                                    params.get("documentURL", "")):
                if kind != "Script":
                    return        # egy másik oldal adatkérése (reklám, statisztika): nem várunk rá
                when -= STUCK_REQUEST_SECONDS - FOREIGN_SCRIPT_SECONDS   # így hamarabb „elakadtnak” számít
            inflight[request_id] = when

        def finished(params: dict[str, Any]) -> None:
            inflight.pop(params.get("requestId"), None)

        session.on("Network.requestWillBeSent", started)
        session.on("Network.loadingFinished", finished)
        session.on("Network.loadingFailed", finished)
        page._jarvis_network = session

    @staticmethod
    def _unwatch(page: Any) -> None:
        """Leválasztja a lap hálózatfigyelését (háttérbe került vagy bezárjuk): a háttérben futó reklámok és
        frissítések eseményei így nem jönnek feleslegesen a Pythonnak. Ha újra aktív lesz, a _watch_network
        újra bekapcsolja."""
        if page is None:
            return
        session = getattr(page, "_jarvis_network", None)
        page._jarvis_network = None
        page._jarvis_watched = False
        inflight = getattr(page, "_jarvis_inflight", None)
        if inflight:
            inflight.clear()
        if session is not None:
            try:
                session.detach()
            except Exception:  # noqa: BLE001 - pl. már bezárult a lap
                pass

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

    @staticmethod
    def _loaded(page: Any) -> bool:
        """Megvárja a dokumentum betöltését. Hamis, ha közben a lap bezárult vagy összeomlott."""
        for state, timeout in (("domcontentloaded", 10000), ("load", 4000)):
            try:
                page.wait_for_load_state(state, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                if is_closed_error(exc):
                    return False      # bezárult vagy összeomlott: nincs mire várni
        return True

    def _settle(self, page: Any, max_wait: float = 5.0, react: float = 0.0) -> bool:
        """Megvárja, amíg az oldal betölt és a tartalma megnyugszik.

        A modern webshopok (pl. Vinted) a találatokat a betöltés után, JavaScripttel töltik be. Ezért addig
        várunk, amíg nincs folyamatban az oldal saját adatkérése (lásd _watch_network), a lap egy rövid ideig
        (QUIET_SECONDS, egy új oldalnál QUIET_AFTER_LOAD, és az új oldal legalább MIN_DOC_AGE idős) nem változik,
        nem úszik be épp valami (rövid animáció), és nem látszik rajta "Betöltés…" jellegű felirat. Egy egyszerű
        oldalnál ez kb. 0,2 másodperc, egy lassan töltődő webshopnál több, de legfeljebb `max_wait`. Ha a lap
        folyton változik (pl. egy futó óra), a szöveg hossza dönt (CHURN_STABLE).

        `react`: legalább ennyi ideig várja az oldal első változását (pl. egy kereső javaslatai a gépelés után
        késleltetve jönnek); ha addig sem változik semmi, kész. Ez nem az emberi tempó (az a humanize.py-ban
        van, és ettől független), csak a lap figyelése: a szükségesnél nem tovább.

        Igaz, ha az oldal megnyugodott; hamis, ha letelt az idő, vagy a lap közben bezárult."""
        self._watch_network(page)
        if not self._loaded(page):
            return False
        start = time.monotonic()
        deadline = start + max_wait
        first_doc = first_count = None
        size, size_since = -2, start
        while True:
            now = time.monotonic()
            elapsed = now - start
            churn = elapsed >= CHURN_AFTER
            try:
                state = page.evaluate(_SETTLE_JS, {"doc": getattr(page, "_jarvis_doc", None), "elapsed": elapsed,
                                                   "quiet": QUIET_SECONDS, "longQuiet": QUIET_AFTER_LOAD,
                                                   "minAge": MIN_DOC_AGE, "churn": churn})
            except Exception as exc:  # noqa: BLE001 - épp navigál
                if is_closed_error(exc):
                    return False
                state = None
            wait = SETTLE_POLL
            if state:
                if first_doc is None or state["doc"] != first_doc:
                    if first_doc is not None and not self._loaded(page):   # közben átnavigált
                        return False
                    first_doc, first_count = state["doc"], state["count"]
                if state["size"] >= 0 and state["size"] != size:
                    size, size_since = state["size"], now
                busy = self._network_busy(page) or state["loading"] or state["moving"]
                reacted = state["count"] != first_count or elapsed >= react
                calm = not state.get("young") and (state["quietFor"] >= state["window"] or
                                                   (churn and size > 0 and now - size_since >= CHURN_STABLE))
                if not busy and reacted and calm:
                    page._jarvis_doc = state["doc"]
                    return True
                if not busy and not state.get("young"):   # csak a hiányzó időt várja ki, ne egy egész kört
                    left = max(state["window"] - state["quietFor"], react - elapsed if not reacted else 0.0)
                    wait = min(SETTLE_POLL, max(0.02, left))
            if time.monotonic() + wait > deadline:
                if state:
                    page._jarvis_doc = state["doc"]
                return False
            try:
                page.wait_for_timeout(wait * 1000)
            except Exception as exc:  # noqa: BLE001
                if is_closed_error(exc):
                    return False
                time.sleep(wait)

    def _adopt_new_page(self, pages_before: int) -> bool:
        """Ha a kattintás új lapot nyitott, arra vált. Az előző lap hálózatfigyelését leválasztja, és a Jarvis
        kattintásaira nyílt régebbi lapokat bezárja (lásd _prune_tabs)."""
        pages = [p for p in self._context.pages if not p.is_closed()]
        if len(pages) <= pages_before:
            return False
        previous = self.page
        self.page = pages[-1]
        self._own_tabs.extend(p for p in pages[pages_before:] if p not in self._own_tabs)
        try:
            self.page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        if previous is not None and previous is not self.page:
            self._unwatch(previous)
        self._prune_tabs(keep=(self.page, previous))
        self._settle(self.page)
        return True

    def _prune_tabs(self, keep: tuple[Any, ...]) -> None:
        """A Jarvis kattintásaira megnyílt lapok közül csak az aktív és az előző marad nyitva: a többi
        (pl. egy hirdetés, ami új lapon nyílt meg, és már továbbmentünk) memóriát és processzort fogyaszt. A
        felhasználó által nyitott lapokhoz nem nyúl."""
        for tab in list(self._own_tabs):
            if any(tab is other for other in keep):
                continue
            self._own_tabs.remove(tab)
            self._close_page(tab)

    def _close_page(self, page: Any) -> None:
        self._unwatch(page)
        try:
            if not page.is_closed():
                page.close()
        except Exception:  # noqa: BLE001 - pl. már bezárult
            pass

    def _element(self, number: Any) -> dict[str, Any]:
        try:   # a modell néha szövegként vagy "#3" alakban adja meg (egy lista elemeiben is)
            n = int(number) if isinstance(number, int) else int(float(str(number).strip().lstrip("#[").rstrip("]")))
        except (TypeError, ValueError):
            raise BrowserError("Az elemet a számával add meg (pl. 12).") from None
        if n not in self.elements:
            raise BrowserError(f"Nincs {n}. számú elem a legutóbbi oldalnézetben.")
        return self.elements[n]

    def _locator(self, number: int) -> Any:
        return self.page.locator(id_selector(number)).first

    def _fail(self, message: str) -> BrowserError:
        """Hibaüzenet friss oldalnézettel, hogy a modell újra tudjon tervezni. Modell nélküli lépéssorban
        (auto) nincs oldalnézet: azt senki nem olvasná, és a letapogatás számait is érvényben hagyjuk. Egy
        többlépéses művelet közben sincs (lásd _batched): az a végén egyszer ad oldalnézetet."""
        if self.auto or self._batch:
            return BrowserError(message)
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
        if self.on_action is None or not self.recording or self.page is None:
            return 0
        try:
            return int(self.page.evaluate(ORDINAL_JS, id_selector(el["id"])) or 0)
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
        self._locate_seq = LOCATE_BASE if self._locate_seq > LOCATE_BASE + 900 else self._locate_seq + 1
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
                self._numbers_url = page.url.split("#")[0]
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
        kattint vagy gépel (click, type_text). A számok egy oldalnézet után is érvényesek (külön attribútumban
        vannak), de egy kattintás vagy gépelés után az oldal megváltozhat, ezért utána érdemes újra letapogatni."""
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
        self._numbers_url = page.url.split("#")[0]
        return elements

    @_restarts_after_crash
    def press(self, key: str = "Escape") -> None:
        """Egy billentyű (pl. Esc: bezárja a nyitva maradt listát), ahogy az ember is megnyomná. A felvétel is
        látja: egy kinyitott, de választás nélkül bezárt lista így kimarad a receptből (FlowRecorder.normalized)."""
        page = self._ensure()
        self.pace.wait(0.15, 0.4)
        try:
            page.keyboard.press(key)
            page.wait_for_timeout(250)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"Nem sikerült megnyomni: {key} ({_short(exc)}).") from None
        self._emit({"do": "press", "key": key})

    def form_errors(self) -> list[str]:
        """Az űrlap látható hibaüzenetei (pl. egy kötelező, de üres mezőnél), legfeljebb hat."""
        page = self._ensure()
        try:
            found = page.evaluate(FORM_ERRORS_JS)
        except Exception:  # noqa: BLE001 - épp navigál az oldal
            return []
        return [str(text) for text in found or [] if text]

    def headline(self) -> tuple[str, str]:
        """Az aktív lap címe és első látható címsora (pl. „Hirdetésed feltöltve”): a közzététel eredményéhez."""
        page = self._ensure()
        try:
            found = page.evaluate("() => [document.title, ((Array.from(document.querySelectorAll("
                                  "'h1, h2, [role=heading]')).find((h) => h.offsetParent !== null) || {})"
                                  ".innerText || '').slice(0, 200)]")
        except Exception:  # noqa: BLE001 - épp navigál az oldal
            return "", ""
        title, heading = (list(found or []) + ["", ""])[:2]
        return " ".join(str(title or "").split()), " ".join(str(heading or "").split())

    def screen(self) -> bytes:
        """Az aktív lap látható része JPEG-ben (a captcha megoldásához a telefonon, lásd tap)."""
        page = self._ensure()
        try:
            return page.screenshot(type="jpeg", quality=60, timeout=8000)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"Nem sikerült képet készíteni ({_short(exc)}).") from None

    def tap(self, x: float, y: float) -> None:
        """Egy kattintás oda, ahova a telefonon a képre böktél (x, y: 0–1, a látható rész arányában). Ez a te
        kattintásod, nem a Jarvisé: így oldhatod meg távolról a captchát (a Jarvis maga soha nem kattint rá)."""
        page = self._ensure()
        size = page.viewport_size or page.evaluate("() => ({width: innerWidth, height: innerHeight})")
        try:
            page.mouse.click(min(max(x, 0.0), 1.0) * size["width"], min(max(y, 0.0), 1.0) * size["height"])
            self._settle(page, max_wait=2.0)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"Nem sikerült a kattintás ({_short(exc)}).") from None

    def challenged(self) -> bool:
        """Robot-ellenőrzést (captcha) vagy robotvédelmi köztes oldalt mutat-e az aktív lap."""
        page = self._ensure()
        try:
            if page.evaluate(f"() => {CAPTCHA_FRAME_JS}"):
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
        self._remember_view(data)
        return format_snapshot(data, note)

    def _remember_view(self, data: dict[str, Any]) -> None:
        """Az oldalnézet elemei lesznek érvényesek; a letapogatás (scan) és a visszajátszás (locate) számai is
        megmaradnak. Ugyanazon az oldalon megjelöli az előző nézet óta megjelent elemeket (csillag)."""
        shown = [el for el in (data.get("elements") or []) + (data.get("below") or []) if isinstance(el, dict)]
        url = str(data.get("url") or "").split("#")[0]
        before = int(data.get("seqBefore") or 0)
        if (url == self._view_url and not data.get("newDoc") and not data.get("renumbered")
                and any(int(el["id"]) <= before for el in shown)):
            for el in shown:
                if int(el["id"]) > before:
                    el["new"] = True
        self._view_url = url
        # egy másik oldalon a korábbi letapogatás számai már nem érvényesek (az elemeik ott nincsenek meg)
        keep = url == self._numbers_url and not data.get("newDoc")
        self.elements = {n: el for n, el in self.elements.items() if n >= SCAN_BASE and keep}
        self.elements.update({int(el["id"]): el for el in shown})

    def _snapshot_data(self, page: Any) -> dict[str, Any]:
        hints = sorted({word for hint in self.hints for word in re.split(r"[^a-z0-9]+", fold(str(hint)))
                        if len(word) >= 3})
        options = {"max": int(self.cfg.max_elements), "textChars": VISIBLE_TEXT_CHARS, "base": SCAN_BASE,
                   "below": BELOW_MAX, "hints": hints}
        for attempt in range(3):
            try:
                return page.evaluate(SNAPSHOT_JS, options)
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
        note, outcome = self._press(page, self._element(element))
        # Modell nélküli lépéssornál (auto) nincs oldalnézet: senki nem olvasná, és a letapogatás számai így
        # érvényben maradnak (a vinted.py és a replay.py csak azt nézi, engedélyezted-e).
        return note if self.auto or outcome == "declined" else self.snapshot(note)

    def _approved(self, page: Any, label: str) -> bool:
        """Egy kockázatos kattintás jóváhagyása: előre megadva (approve_hook, pl. a levételnél), vagy most kérdezi."""
        if self.approve_hook is not None:
            try:
                if self.approve_hook(label, page.url):
                    return True
            except Exception:  # noqa: BLE001
                pass
        return bool(self.confirm(f'Jarvis erre kattintana: "{label}"\n    ({page.url})'))

    def _press(self, page: Any, el: dict[str, Any]) -> tuple[str, str]:
        """Kattintás egy elemre, oldalnézet nélkül: kockázatosnál jóváhagyással, emberi mozdulattal. Visszaadja a
        megjegyzést, és hogy mi történt: "" (rendben), "declined" (nem engedélyezted), "chooser" (fájlt kér)
        vagy "tab" (új lapon nyílt meg)."""
        label = el.get("label", "")
        ordinal = self._ordinal(el)
        url_before = page.url
        risky = bool(self.cfg.confirm_risky and is_risky(label, link=navigates(el)))
        if risky and self.before_publish is not None:
            self.before_publish(label, page.url)   # az Állj gomb itt is megszakíthatja (Cancelled)
        if self.auto:   # modell nélkül: egy ember is megnézi, mire kattint (a közzététel előtt az egészet)
            if risky:
                self.pace.review()
            else:
                self.pace.think()
        if risky and not self._approved(page, label):
            self._emit({"do": "declined", "el": self.describe(el, ordinal), "url": url_before})
            return f"{DECLINED} ezt a kattintást.", "declined"
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
        note, outcome = f'Rákattintottam: "{label}".', ""
        if choosers:
            note += (" Ez a gomb fájlt (pl. fotót) kér. A feltöltéshez a browser_upload_photos "
                     "eszközt használd ugyanerre az elemre.")
            outcome = "chooser"
        if self._adopt_new_page(pages_before):
            note += " Új lapon nyílt meg."
            outcome = outcome or "tab"
        event = {"do": "click", "el": self.describe(el, ordinal), "url": url_before, "url_after": self.url,
                 "risky": risky, "chooser": bool(choosers)}
        if el.get("kind") in CHECK_KINDS:
            event["check"] = True   # egy jelölőnégyzet vagy kapcsoló nem a közzététel gombja (lásd _record_write)
        self._emit(event)
        return note, outcome

    @_restarts_after_crash
    def type_text(self, element: Any, text: str, submit: bool = False) -> str:
        page = self._ensure()
        note = self._type(page, self._element(element), text, submit)
        return note if self.auto or note.startswith(DECLINED) else self.snapshot(note)

    @staticmethod
    def _refuse_sensitive(el: dict[str, Any]) -> None:
        if el.get("sensitive") or el.get("kind") == "input:password":
            raise BrowserError("Jelszót és bankkártyaadatot nem írok be. Kérlek, ezt a "
                               "böngészőablakban add meg te, utána folytatom.")

    def _type(self, page: Any, el: dict[str, Any], text: str, submit: bool = False) -> str:
        """Egy mező kitöltése oldalnézet nélkül, a mező fajtája szerint (lásd _write). Visszaadja a megjegyzést."""
        self._refuse_sensitive(el)
        ordinal = self._ordinal(el)
        if submit and self.cfg.confirm_risky and not is_search_field(el):
            label = el.get("label") or "mező"
            if self.before_publish is not None:   # egy űrlap elküldése is közzététel lehet: előbb a szünet
                self.before_publish(label, page.url)
            if not self.confirm(f'Jarvis ezt írná be és küldené el ("{label}"):\n    {text}'):
                self._emit({"do": "declined", "el": self.describe(el, ordinal), "url": page.url})
                return f"{DECLINED} az elküldést."
        if self.auto:
            self.pace.think()
        url_before = page.url
        try:
            written = self._write(page, el, text)
            if submit and written.kind == "text":
                self.pace.wait(0.2, 0.6)
                page.keyboard.press("Enter")   # oda megy, ahová gépelt
        except Covered as exc:
            raise self._fail(f"Nem sikerült beírni a szöveget, mert {exc}. {UNCOVER_HINT}") from None
        except BrowserError as exc:
            raise self._fail(str(exc)) from None
        except Exception as exc:  # noqa: BLE001
            raise self._fail(f"Nem sikerült beírni a szöveget ({_short(exc)}).") from None
        if written.declined:
            return f"{DECLINED} ezt a kattintást."
        self._settle(page, react=SUGGEST_REACT if submit else written.react)
        self._record_write(el, ordinal, text, written, url_before, submit)
        return self._written_note(el, text, written, submit)

    def _record_write(self, el: dict[str, Any], ordinal: int, text: str, written: _Written, url_before: str,
                      submit: bool) -> None:
        """A felvételnek: a beírás és a kiválasztás gépelésként, a jelölőnégyzet kattintásként (ha rákattintott). A
        jelölőnégyzetnél a `check` jelzi, hogy ez nem gomb: egy kockázatos (pl. a feltételek elfogadása) sem maga a
        közzététel."""
        if written.kind != "check":
            self._emit({"do": "type", "el": self.describe(el, ordinal), "text": text, "submit": bool(submit),
                        "search": is_search_field(el)})
        elif written.clicked:
            self._emit({"do": "click", "el": self.describe(el, ordinal), "url": url_before, "url_after": self.url,
                        "risky": bool(self.cfg.confirm_risky and is_risky(el.get("label", ""))), "chooser": False,
                        "check": True})

    @staticmethod
    def _written_note(el: dict[str, Any], text: str, written: _Written, submit: bool = False) -> str:
        label = el.get("label") or f"{el['id']}. elem"
        if written.kind == "select":
            return f'Kiválasztottam: "{written.shown}" ("{label}").'
        if written.kind == "check":
            return f'"{label}": {written.shown}.'
        note = f'Beírtam: "{text}"' + (f" ({written.shown})" if written.shown else "")
        return note + (", és elküldtem (Enter)." if submit else ".")

    @_restarts_after_crash
    def fill_fields(self, fields: list[dict[str, Any]]) -> str:
        """Több mezőt tölt ki egy lépésben (a legutóbbi oldalnézet számai szerint), Enter nélkül.

        Egy űrlapnál (cím, leírás, ár…) így egy modellkör az, ami mezőnként öt lenne, és csak
        egy oldalnézet kerül a beszélgetésbe. A mezők fajtája szerint: a szövegmezőbe gépel, a lenyíló
        listából (select) kiválasztja a lehetőséget, a jelölőnégyzetet a kért állásba teszi (lásd _write)."""
        page = self._ensure()
        if not isinstance(fields, list) or not fields:
            raise BrowserError('Add meg a mezőket, pl.: [{"element": 3, "text": "Zara póló"}]')
        plan = []
        for entry in fields[:20]:
            if not isinstance(entry, dict) or "element" not in entry:
                raise BrowserError('Minden mezőnél kell az elem száma és a szöveg: {"element": 3, "text": "…"}')
            el = self._element(entry["element"])
            self._refuse_sensitive(el)
            plan.append((el, str(entry.get("text") if entry.get("text") is not None else "")))
        ordinals = [self._ordinal(el) for el, _text in plan]
        done, failed = [], []
        react = 0.0
        for index, ((el, text), ordinal) in enumerate(zip(plan, ordinals)):
            label = el.get("label") or f"{el['id']}. elem"
            if index:
                self.pace.wait(0.3, 0.9)   # a következő mezőre átnéz
            url_before = page.url
            try:
                written = self._write(page, el, text)
            except Exception as exc:  # noqa: BLE001 - a többi mezőt attól még kitöltjük
                failed.append(f'"{label}" ({_short(exc)})')
                continue
            if written.declined:
                failed.append(f'"{label}" (nem engedélyezted)')
                continue
            react = max(react, written.react)
            done.append(f'"{label}"' + (f" ({written.shown})" if written.kind != "text" else ""))
            self._record_write(el, ordinal, text, written, url_before, False)
        self._settle(page, react=react)
        note = f"Kitöltöttem: {', '.join(done)}." if done else ""
        if failed:
            note += (f" Nem sikerült: {'; '.join(failed)}. Ezeket a friss oldalnézet alapján egyenként "
                     "próbáld (browser_type; saját lenyíló listánál browser_choose).")
        return note.strip() if self.auto else self.snapshot(note.strip())

    def _write(self, page: Any, el: dict[str, Any], text: str) -> _Written:
        """Egy mező beállítása a fajtája szerint (amit az oldalon ténylegesen talál):
        - szövegmező: begépeli (lásd _write_text),
        - lenyíló lista (select): a felirata alapján kiválasztja a lehetőséget (lásd _select),
        - jelölőnégyzet, választógomb, kapcsoló: a kért állásba teszi, és csak akkor kattint, ha nem abban áll
          (lásd _set_checked); gépelni soha nem gépel bele.
        Ami egyik sem (pl. egy gomb), abba nem gépel: hibát ad, és a megfelelő eszközt ajánlja."""
        locator = self._locator(el["id"])
        try:
            info = locator.evaluate(_WRITE_INFO_JS, timeout=5000) or {}
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f'Nem találom ezt a mezőt: "{el.get("label", "")}" ({_short(exc)}).') from None
        if info.get("select"):
            return self._select(el, locator, text)
        if info.get("check"):
            return self._set_checked(page, el, locator, info, text)
        return self._write_text(page, el, locator, info, text)

    @staticmethod
    def _not_text(el: dict[str, Any]) -> str:
        kind = KIND_NAMES.get(el.get("kind", ""), el.get("kind", "") or "elem")
        return (f'"{el.get("label", "")}" nem szövegmező ({kind}), ezért nem írtam bele semmit. Saját lenyíló '
                "listánál a browser_choose, gombnál a browser_click eszközt használd.")

    def _write_text(self, page: Any, el: dict[str, Any], locator: Any, info: dict[str, Any], text: str) -> _Written:
        """Szöveg egy mezőbe. Emberi tempóban: a mezőbe kattint, kijelöli a régi szöveget, és a rövid szöveget
        billentyűnként begépeli, a hosszút (pl. leírás) beilleszti, ahogy az ember a jegyzetéből.

        Ami nem szövegmező (pl. egy gomb vagy egy saját lenyíló lista), abba nem gépel (különben „Beírtam”-ot
        mondana, pedig semmi nem változott). Ha a mező csak olvasható, de a kattintás egy másik szövegmezőbe vitte
        a kurzort (pl. a megnyílt lista keresőjébe), oda gépel."""
        if not info.get("text"):
            raise BrowserError(self._not_text(el))
        if not self.pace.enabled:
            if not info.get("text") or info.get("readonly"):
                raise BrowserError(self._not_text(el))
            if el.get("editable"):
                locator.click(timeout=5000)
                page.keyboard.press("Control+A")
                page.keyboard.type(text, delay=10)
            else:
                locator.fill(text, timeout=8000)
            return _Written("text", "", locator, react=self._suggest_react(el, self._focus(locator)))
        self._human_click(page, locator)
        focus = self._focus(locator)
        if not focus.get("editable"):
            raise BrowserError(self._not_text(el))
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
        if focus.get("same") and not el.get("editable"):
            self._ensure_value(locator, text)
        shown = "" if focus.get("same") else "a megnyílt mezőbe"
        return _Written("text", shown, locator, react=self._suggest_react(el, focus))

    @staticmethod
    def _focus(locator: Any) -> dict[str, Any]:
        try:
            return locator.evaluate(_FOCUS_JS, timeout=3000) or {}
        except Exception:  # noqa: BLE001 - pl. épp újrarajzolta az oldal
            return {}

    @staticmethod
    def _suggest_react(el: dict[str, Any], focus: dict[str, Any]) -> float:
        """Kereső vagy javaslatos mezőnél a gépelés után kivárja a javaslatokat (lásd SUGGEST_REACT)."""
        return SUGGEST_REACT if focus.get("suggests") or is_search_field(el) else 0.0

    def _select(self, el: dict[str, Any], locator: Any, text: str) -> _Written:
        """Lenyíló lista (select): a lehetőség a felirata alapján (pontosan, vagy ha egyértelmű, közelítőleg:
        ékezet és kis-nagybetű nélkül, az elejéből vagy egy szavából), és a sorszámával választja ki, így nincs
        várakozás egy nem létező feliratra. Ha több is illik, vagy egyik sem, a jelölteket adja vissza."""
        options = [o for o in (locator.evaluate(_SELECT_OPTIONS_JS, timeout=5000) or []) if isinstance(o, dict)]
        usable = [i for i, o in enumerate(options) if not o.get("disabled")]
        exact = ([i for i in usable if _norm(options[i]["text"]) == _norm(text)] or
                 [i for i in usable if text and options[i].get("value") == text])
        if exact:
            chosen = exact[0]
        else:
            labels = [options[i]["text"] for i in usable]
            index, hits = match_option(labels, text)
            if index is None:
                shown = ", ".join(f"„{labels[i]}”" for i in (hits or range(len(labels)))[:5])
                if hits:
                    raise BrowserError(f'Több lehetőség is illik erre: „{text}” ({len(hits)} db, pl. {shown}). '
                                       "Írd be pontosan a választottat.")
                more = " …" if len(labels) > 5 else ""
                raise BrowserError(f"Nincs ilyen lehetőség a listában: „{text}”. A lehetőségek: {shown}{more}.")
            chosen = usable[index]
        locator.select_option(index=chosen, timeout=5000)
        return _Written("select", options[chosen]["text"], locator)

    def _set_checked(self, page: Any, el: dict[str, Any], locator: Any, info: dict[str, Any],
                     text: str) -> _Written:
        """Jelölőnégyzet, választógomb, kapcsoló: a kért állásba teszi. A szöveg: igen/x/✓/be (vagy a saját
        felirata) = bejelölve, nem/ki = nincs bejelölve. Csak akkor kattint, ha nem abban áll (egy második
        kattintás kivenné a jelölést), és utána megnézi, átállt-e."""
        label = el.get("label", "")
        want = wanted_state(text, label)
        if want is None:
            kind = KIND_NAMES.get(el.get("kind", ""), "jelölőnégyzet")
            raise BrowserError(f'„{text}”: ez egy {kind} („{label}”); a szöveg legyen igen (bejelölve) vagy nem.')
        if bool(info.get("checked")) == want:
            return _Written("check", "már be volt jelölve" if want else "nem volt bejelölve", locator)
        if not want and info.get("radio"):
            raise BrowserError(f'„{label}”: a választógombot nem lehet kikapcsolni; jelöld be a másik lehetőséget.')
        if is_paid(label):
            raise BrowserError(f'„{label}” fizetős lehetőségnek látszik (kiemelés, előresorolás): erre nem kattintok.')
        if self.cfg.confirm_risky and is_risky(label):
            if self.before_publish is not None:   # mint a gombnál: a közzétételek közti szünet a jóváhagyás előtt
                self.before_publish(label, page.url)
            if not self._approved(page, label):
                return _Written("check", "", locator, declined=True)
        self._human_click(page, locator)
        for _ in range(20):     # a lap kicsit később is átírhatja (pl. egy keretrendszer újrarajzolja)
            try:
                if bool((locator.evaluate(_WRITE_INFO_JS, timeout=2000) or {}).get("checked")) == want:
                    return _Written("check", "bejelöltem" if want else "kivettem a jelölést", locator, clicked=True)
            except Exception:  # noqa: BLE001 - pl. épp újrarajzolja
                pass
            self._sleep(0.05)
        raise BrowserError(f'Rákattintottam erre: „{label}”, de nem állt át ({"be" if want else "ki"}).')

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

    # --- választás listából, több lépés egy körben -------------------------------------------------------

    @_restarts_after_crash
    def choose(self, element: Any, option: str) -> str:
        """Kiválaszt egy lehetőséget egy lenyíló listából vagy választóból (a modell browser_choose eszköze).

        Natív listánál (select) a felirat alapján választ (lásd _select). Saját listánál (combobox, listbox,
        lenyíló gomb) emberi kattintással kinyitja, megkeresi a megjelent lehetőségek közül a kértet (közelítő
        egyezéssel, ha egyértelmű), és rákattint; fánál szintenként („Nők > Ruházat > Pólók”). Kereshető
        mezőnél előbb beírja a szöveget, és a javaslatok közül választ. Ha a lista nyitva marad, bezárja
        („Kész” gomb vagy Esc). Ha nem egyértelmű, a jelölteket adja vissza; minden kattintás a szokásos módon,
        kockázatos feliratnál jóváhagyással megy."""
        page = self._ensure()
        el = self._element(element)
        note = self._batched(lambda: self._choose(page, el, str(option or "")))
        return note if self.auto or note.startswith(DECLINED) else self.snapshot(note)

    def _batched(self, action: Callable[[], Any]) -> Any:
        """Egy többlépéses művelet: a közbenső hibák oldalnézet nélkül jönnek (lásd _fail), és ha elakad, a
        hibához a végén egyetlen friss oldalnézet kerül."""
        if self._batch:
            return action()
        self._batch = True
        try:
            return action()
        except BrowserError as exc:
            message = str(exc)
        finally:
            self._batch = False
        raise self._fail(message) from None

    def _choose(self, page: Any, el: dict[str, Any], option: str) -> str:
        label = el.get("label", "") or f"{el['id']}. elem"
        path = [part.strip() for part in re.split(r"\s*[>›»]\s*", option) if part.strip()]
        if not path:
            raise BrowserError("Add meg, mit válasszak (pl. „M”, vagy fánál „Nők > Ruházat > Pólók”).")
        if len(path) > MAX_CHOOSE_LEVELS:
            raise BrowserError(f"Legfeljebb {MAX_CHOOSE_LEVELS} szintet tudok egyszerre választani.")
        self._refuse_sensitive(el)
        locator = self._locator(el["id"])
        try:
            info = locator.evaluate(_WRITE_INFO_JS, timeout=5000) or {}
            handle = locator.element_handle(timeout=3000)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f'Nem találom ezt az elemet: "{label}" ({_short(exc)}).') from None
        if info.get("select") or info.get("check"):
            return self._type(page, el, option)
        before = self.scan()
        self._retag(handle, el)        # a letapogatás átszámozta: a modell számával is megtalálható maradjon
        base = Counter(_elem_key(e) for e in before)
        if info.get("text") and not info.get("readonly"):     # kereshető mező: előbb beírja, aztán választ
            typed = self._type(page, el, path[0])
            if typed.startswith(DECLINED):
                return typed
            options, search, seen = self._list_state(base, handle, el)
        else:
            note, outcome = self._press(page, el)
            if outcome:
                return note
            options, search, seen = self._list_state(base, handle, el)
            if not options and search is None:   # lehet, hogy a lista már nyitva volt, és a kattintás bezárta
                base = seen
                note, outcome = self._press(page, el)
                if outcome:
                    return note
                options, search, seen = self._list_state(base, handle, el)
        if not options and search is None:
            raise BrowserError(f'Erre kattintva nem nyílt ki lista: "{label}". Ha nem lenyíló lista, a browser_click '
                               "vagy a browser_type kell.")
        chosen: list[str] = []
        shown = list(options)
        for level, wanted in enumerate(path):
            pick, seen = self._pick_option(page, options, search, wanted, base, handle, el, chosen, seen)
            note, outcome = self._press(page, pick)
            if outcome:
                return note
            chosen.append(pick.get("label", ""))
            shown.append(pick)
            if level == len(path) - 1:
                break
            options, search, seen = self._list_state(base, handle, el, chosen, since=seen)
            if not options and search is None:
                raise BrowserError(f'„{" > ".join(chosen)}” után nem nyílt újabb szint, pedig ezt is kérted: '
                                   f'„{" > ".join(path[level + 1:])}”.')
            shown += options
        self._close_list(page, shown, base)
        return f'Kiválasztottam: "{" > ".join(chosen)}" ("{label}").'

    def _retag(self, handle: Any, el: dict[str, Any]) -> bool:
        """Az elem újra a modell által látott számot kapja (egy letapogatás vagy újrarajzolás után), ha még
        ugyanaz az elem: ugyanolyan fajta és felirat. Hamis, ha már nincs az oldalon, vagy megváltozott."""
        try:
            found = handle.evaluate(_IDENTITY_JS, int(el["id"])) or {}
        except Exception:  # noqa: BLE001 - pl. a lap közben átnavigált
            return False
        if not found.get("connected") or found.get("kind") != el.get("kind"):
            return False
        now, seen = _norm(found.get("label", "")), _norm(el.get("label", ""))
        # az oldalnézet a feliratot rövidebbre vághatja: elég, ha az egyik a másik eleje
        return now == seen or bool(now and seen and (now.startswith(seen) or seen.startswith(now)))

    def _list_state(self, base: Counter, handle: Any, el: dict[str, Any], chosen: list[str] | None = None,
                    since: Counter | None = None) -> tuple[list[dict[str, Any]], dict[str, Any] | None, Counter]:
        """A kinyitott lista: a lehetőségei (ami a kinyitás óta jelent meg; a lista mezője, a vezérlőgombok és az
        eddig kiválasztottak nem), a saját keresője, ha van, és hogy mi látszik most (a következő szinthez). Egy
        mélyebb szinten (`since`: ami az előző szinten látszott) előbb az újonnan megjelentek közül választ, így
        egy egymás mellett nyíló menüben a felső szint lehetőségei nem keverednek bele. Ha egy felirat kétszer
        is szerepel (pl. a sor és a benne lévő gomb), csak egyszer, a pontosabbal."""
        elements = self.scan()
        self._retag(handle, el)
        taken = {_norm(c) for c in chosen or []}
        opener = _elem_key(el)

        def usable(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [e for e in items if _is_option(e) and _elem_key(e) != opener
                    and _norm(e.get("label", "")) not in taken]

        fresh = _appeared(base, elements)
        found = usable(_appeared(since, elements)) if since is not None else []
        found = found or usable(fresh)
        search = next((e for e in fresh if e.get("kind") in _TEXT_KINDS and not e.get("readonly")
                       and not e.get("chrome") and not e.get("disabled")), None)
        return distinct_options(found), search, Counter(_elem_key(e) for e in elements)

    def _pick_option(self, page: Any, options: list[dict[str, Any]], search: dict[str, Any] | None, wanted: str,
                     base: Counter, handle: Any, el: dict[str, Any], chosen: list[str],
                     seen: Counter) -> tuple[dict[str, Any], Counter]:
        """A kért lehetőség a lista elemei közül (lásd match_option), és ami most látszik. Ha nincs meg (vagy
        nem egyértelmű), de a listának van saját keresője (pl. egy márkalista), beírja oda, és a találatok közül
        választ."""
        index, hits = match_option([o.get("label", "") for o in options], wanted)
        if index is None and search is not None:
            typed = self._type(page, search, wanted)
            if typed.startswith(DECLINED):
                raise BrowserError(typed)
            options, _search, seen = self._list_state(base, handle, el, chosen)
            index, hits = match_option([o.get("label", "") for o in options], wanted)
        where = f" (eddig: {' > '.join(chosen)})" if chosen else ""
        if index is None:
            labels = [options[i].get("label", "") for i in (hits or range(len(options)))]
            listed = ", ".join(f"„{text[:40]}”" for text in labels[:8]) + (" …" if len(labels) > 8 else "")
            if hits:
                raise BrowserError(f"Több lehetőség is illik erre: „{wanted}”{where}: {listed}. Add meg pontosabban "
                                   "(a teljes felirattal); a lista nyitva maradt.")
            tree = " Ha ez egy fa, add meg az utat: „Felső > Alsó”." if not chosen else ""
            raise BrowserError(f"Nem találom a listában: „{wanted}”{where}. A lehetőségek: {listed or '(nincs)'}."
                               f"{tree}")
        pick = options[index]
        if is_paid(pick.get("label", "")):
            raise BrowserError(f'„{pick.get("label", "")}” fizetős lehetőségnek látszik (kiemelés, előresorolás): '
                               "erre nem kattintok.")
        return pick, seen

    def _close_list(self, page: Any, shown: list[dict[str, Any]], base: Counter) -> None:
        """Ha a lista nyitva maradt (pl. több színt is lehet választani), bezárja: a lista „Kész” gombjával
        (csak az a gomb, ami a listával jelent meg), vagy Esc-cel, ahogy az ember. Csak a listával megjelent
        elemeket nézi: ami már előtte is látszott (pl. az űrlap, ha egy felugró ablakban van), az nem a lista."""
        keys = {_elem_key(o) for o in shown}
        for _ in range(2):
            fresh = _appeared(base, self.scan())
            if not any(_elem_key(e) in keys for e in fresh):
                return
            done = next((e for e in fresh if e.get("kind") in ("button", "input:button", "input:submit")
                         and not e.get("chrome") and _DONE_RE.match(_norm(e.get("label", "")))), None)
            if done is not None:
                self._press(page, done)
            else:
                self.press("Escape")
                self._settle(page)

    @_restarts_after_crash
    def do_actions(self, actions: list[dict[str, Any]]) -> str:
        """Több lépés egy körben ugyanazon az oldalon (a modell browser_do eszköze): kattintás, gépelés, választás
        listából, jelölőnégyzet, sorban, emberi tempóban, és a végén egyetlen oldalnézet.

        Megáll (és megmondja, mi futott le és mi maradt ki), ha közben megváltozik az oldal címe, új lap vagy
        fájlválasztó nyílik, új felugró ablak jelenik meg, vagy ha egy elem már nem az, amit a modell az
        oldalnézetben látott. Kockázatos kattintás (pl. közzététel) legfeljebb egy lehet, és csak utolsóként;
        fizetős lehetőség (kiemelés) egy sem."""
        page = self._ensure()
        plan = self._plan_actions(actions)
        handles = []
        for _do, el, _text in plan:
            try:
                handles.append(self._locator(el["id"]).element_handle(timeout=2000))
            except Exception:  # noqa: BLE001
                raise self._fail(f'Nincs már ilyen elem az oldalon: [{el["id"]}] "{el.get("label", "")}". '
                                 "Nézd meg a friss oldalnézetet.") from None
        start_url, dialogs = page.url, self._dialogs(page)
        done: list[str] = []
        stop = ""
        self._batch = True
        try:
            for index, ((do, el, text), handle) in enumerate(zip(plan, handles)):
                page = self.page
                if not self._retag(handle, el):
                    stop = f'a(z) [{el["id"]}] "{el.get("label", "")}" elem már nem az, ami az oldalnézetben volt'
                    break
                if index:
                    self.pace.wait(0.3, 0.9)   # a következő mezőre, gombra átnéz
                try:
                    note, outcome = self._do_one(page, do, el, text)
                except BrowserError as exc:
                    message = str(exc).split("\n\nFriss oldalnézet:")[0].strip()
                    stop = f"a(z) {index + 1}. lépés nem sikerült: {message[:400].rstrip('.')}"
                    break
                done.append(f"{index + 1}. {note}")
                if outcome == "declined":
                    stop = "ezt nem engedélyezted"
                elif outcome == "chooser":
                    stop = "a gomb fájlt kér (a feltöltéshez a browser_upload_photos kell)"
                elif outcome == "tab":
                    stop = "új lapon nyílt meg valami"
                elif index < len(plan) - 1 and self.url != start_url:
                    stop = "megváltozott az oldal címe"
                elif index < len(plan) - 1 and self._dialogs(self.page) > dialogs:
                    stop = "felugró ablak nyílt meg"
                if stop:
                    break
        finally:
            self._batch = False
        if len(done) == len(plan):
            note = f"Elvégeztem mind a {len(plan)} lépést: " + " ".join(done)
        else:
            note = f"A {len(plan)} lépésből {len(done)} lett kész" + (": " + " ".join(done) if done else ".")
        if stop:
            failed = stop.startswith(f"a(z) {len(done) + 1}. lépés")
            rest = [f"{n}. {self._action_text(*plan[n - 1])}" for n in range(len(done) + 1 + failed, len(plan) + 1)]
            note += f" Megálltam, mert {stop}." + (f" Kihagytam: {'; '.join(rest)}." if rest else "")
            if rest:
                note += " Az új oldalnézet alapján folytasd."
        return note if self.auto else self.snapshot(note)

    def _plan_actions(self, actions: Any) -> list[tuple[str, dict[str, Any], str]]:
        """A browser_do lépéseinek ellenőrzése, mielőtt bármit csinálna."""
        example = '[{"do": "type", "element": 3, "text": "Zara póló"}, {"do": "click", "element": 7}]'
        if not isinstance(actions, list) or not actions:
            raise BrowserError(f"Add meg a lépéseket listában, pl.: {example}")
        if len(actions) > MAX_ACTIONS:
            raise BrowserError(f"Egyszerre legfeljebb {MAX_ACTIONS} lépést teszek; a többit a következő körben kérd.")
        plan = []
        for entry in actions:
            if not isinstance(entry, dict) or "element" not in entry:
                raise BrowserError(f"Minden lépésnél kell a fajtája és az elem száma, pl.: {example}")
            do = str(entry.get("do") or "click").strip().lower()
            do = {"select": "choose", "fill": "type", "write": "type"}.get(do, do)
            if do not in ("click", "type", "choose", "check"):
                raise BrowserError(f"Ismeretlen lépés: „{do}”. Lehet: click, type, choose, check.")
            el = self._element(entry["element"])
            text = str(entry.get("text") if entry.get("text") is not None else "")
            if do in ("type", "choose", "check"):
                self._refuse_sensitive(el)
            if do == "choose" and not text.strip():
                raise BrowserError(f'A választáshoz add meg a lehetőséget is: [{el["id"]}] "{el.get("label", "")}".')
            plan.append((do, el, text))
        risky = [n for n, (do, el, _text) in enumerate(plan) if self._risky_action(do, el)]
        for do, el, _text in plan:
            if do in ("click", "check") and is_paid(el.get("label", "")):
                raise BrowserError(f'„{el.get("label", "")}” fizetős lehetőségnek látszik (kiemelés, előresorolás): '
                                   "erre nem kattintok.")
        if len(risky) > 1 or (risky and risky[0] != len(plan) - 1):
            label = plan[risky[0]][1].get("label", "")
            raise BrowserError(f"Kockázatos lépés (pl. közzététel, törlés, küldés: „{label}”) csak egy lehet, és "
                               "csak utolsóként: előbb a többit végezd el, és nézd meg az eredményt.")
        return plan

    def _risky_action(self, do: str, el: dict[str, Any]) -> bool:
        if not self.cfg.confirm_risky or do not in ("click", "check"):
            return False
        return is_risky(el.get("label", ""), link=do == "click" and navigates(el))

    @staticmethod
    def _action_text(do: str, el: dict[str, Any], text: str) -> str:
        names = {"click": "kattintás", "type": "gépelés", "choose": "választás", "check": "jelölés"}
        return f'{names[do]} [{el["id"]}] "{el.get("label", "")}"' + (f": „{text}”" if text and do != "click" else "")

    def _do_one(self, page: Any, do: str, el: dict[str, Any], text: str) -> tuple[str, str]:
        """Egy lépés a browser_do-ból, oldalnézet nélkül: (megjegyzés, mi történt; lásd _press)."""
        if do == "click":
            return self._press(page, el)
        if do == "choose":
            note = self._choose(page, el, text)
        elif do == "check":
            try:
                info = self._locator(el["id"]).evaluate(_WRITE_INFO_JS, timeout=5000) or {}
            except Exception as exc:  # noqa: BLE001
                raise BrowserError(f'Nem találom ezt az elemet: "{el.get("label", "")}" ({_short(exc)}).') from None
            if not info.get("check"):
                raise BrowserError(f'"{el.get("label", "")}" nem jelölőnégyzet; gépeléshez a "type" lépés kell.')
            note = self._type(page, el, text or "igen")
        else:
            note = self._type(page, el, text)
        return note, "declined" if note.startswith(DECLINED) else ""

    def _dialogs(self, page: Any) -> int:
        try:
            return int(page.evaluate(_DIALOGS_JS) or 0)
        except Exception:  # noqa: BLE001 - épp navigál
            return 0

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
        """Az egér odamegy (x, y)-ra, enyhe ívben, ahogy a kéz mozgatja. A mozdulat a tervezett ideig tart
        (Pace.move_seconds): egy pont után csak a hátralévő időt várja ki, mert a böngészőnek küldött mozdulat
        maga is időbe telik (különben a kéz lassabb lenne, mint ahogy az emberi tempó előírja)."""
        start = self._mouse or (self.cfg.width * self.pace.rng.uniform(0.3, 0.7),
                                self.cfg.height * self.pace.rng.uniform(0.3, 0.7))
        points = self.pace.path(start, (x, y))
        step = self.pace.move_seconds(start, (x, y)) / max(1, len(points))
        began = time.monotonic()
        for index, (px, py) in enumerate(points, 1):
            page.mouse.move(px, py)
            if step > 0:
                left = began + index * step - time.monotonic()
                if left > 0:
                    self._sleep(left)
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
            self._close_page(page)

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
        return note if self.auto else self.snapshot(note)

    def _fill_input(self, page: Any, locator: Any, files: list[str]) -> None:
        """A fájlok egy fájlfeltöltő mezőbe. Ha a mező egyszerre csak egyet fogad, fotónként újra: a következőt
        akkor adja, amikor az oldal az előzőt átvette (lásd _wait_taken)."""
        multiple = locator.get_attribute("multiple") is not None
        if multiple or len(files) == 1:
            locator.set_input_files(files, timeout=15000)
            return
        for index, file in enumerate(files):   # egyesével fogadó mező: fotónként újra
            locator.set_input_files([file], timeout=15000)
            if index < len(files) - 1:
                self._wait_taken(page, locator)

    def _wait_taken(self, page: Any, locator: Any) -> None:
        """Megvárja, amíg az oldal átveszi a mezőbe tett fotót: kiüríti vagy lecseréli a mezőt, vagy (ha a mezőben
        hagyja) már nem tölt semmit, és nem változik a lap. Legfeljebb UPLOAD_TAKE_SECONDS-ig; egy fix szünet
        helyett, ami egy gyors oldalnál felesleges, egy lassúnál kevés."""
        deadline = time.monotonic() + UPLOAD_TAKE_SECONDS
        while True:
            try:
                if not locator.evaluate("(el) => (el.isConnected && el.files) ? el.files.length : 0", timeout=2000):
                    return        # átvette: kiürítette, vagy már egy új (üres) mező van a helyén
            except Exception:  # noqa: BLE001 - a mező eltűnt: az oldal átvette
                return
            left = deadline - time.monotonic()
            if left <= 0 or self._settle(page, max_wait=min(1.0, left)):
                return

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

    def look_listing(self, url: str) -> dict[str, Any]:
        """Egy hirdetés oldala a háttérellenőrzéshez (listings.py), csak olvasva (lásd look)."""
        return self.look(url, LISTING_PAGE_JS)

    @_restarts_after_crash
    def look(self, url: str, script: str = LISTING_PAGE_JS) -> dict[str, Any]:
        """Egy oldal csak olvasva (pl. egy hirdetés a háttérellenőrzéshez, vagy a vevői üzenetek): megnyitja az
        aktív lapon, emberi tempóban átfutja (egy pillantás, egy kis görgetés, olvasás), és visszaadja a lap adatait
        (a `script` szerint, pl. LISTING_PAGE_JS) a captcha jelzéssel. Egy magától átengedő robotvédelmi köztes
        oldalt kivár (CHALLENGE_WAIT), de a captchát nem próbálja megoldani. Semmire nem kattint."""
        try:
            url = web.normalize_url(url, allow_local=self.allow_local)
        except web.WebError as exc:
            raise BrowserError(str(exc)) from None
        page = self._goto(url)
        self._settle(page, max_wait=8.0)
        deadline = time.monotonic() + CHALLENGE_WAIT
        while self._challenge(page, unsure=False) and time.monotonic() < deadline:
            page.wait_for_timeout(1000)
            self._settle(page, max_wait=3.0)
        self._glance(page)   # modell nélküli olvasás: egy ember is átfutja az oldalt
        self.pace.read()
        self.elements = {}   # a korábbi oldalnézet számai itt már nem érvényesek
        return self._page_data(page, script)

    @_restarts_after_crash
    def page_data(self, script: str) -> dict[str, Any]:
        """Az aktív lap adatai egy olvasó szkripttel (pl. a beszélgetés üzenetei a küldés után), navigálás, görgetés
        és kattintás nélkül; a letapogatás (scan) számai érvényben maradnak."""
        return self._page_data(self._ensure(), script)

    def _page_data(self, page: Any, script: str) -> dict[str, Any]:
        try:
            data = page.evaluate(script)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"Nem tudom beolvasni az oldalt ({_short(exc)}).") from None
        data = data if isinstance(data, dict) else {}
        data["captcha"] = bool(data.get("captcha")) or self._challenge(page, unsure=False)
        return data
