"""A Jarvis ablaka távolról: a telefonon (Android-alkalmazás) és a laptopon (böngészőből telepített
alkalmazás) ugyanaz a felület, mint a gépen, de minden a gépen fut (a modellek, a böngésző, a feltöltés).

A telefonos kapcsolat (api.py) portján érhető el, a Tailscale-en át (`tailscale serve --bg 8765`):
- `/` párosítás nélkül a belépő oldal (a gépen kért 6 jegyű kóddal párosít), utána a Jarvis ablaka;
- `/`, `/events` és a felület `/api/...` kérései az eszköz kulcsával mennek, és a gépen futó felülethez
  (gui.py) továbbítjuk őket, a helyi kulccsal;
- a telepíthető alkalmazáshoz (PWA): manifest, service worker, ikonok.

Ami csak a gép előtt ülve szabad, azt távolról nem engedjük: kilépés, új párosító kód, a gép mikrofonja.
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
from functools import lru_cache

# Távolról nem engedjük (a gép előtt ülve, a Jarvis ablakában lehet).
DENIED = ("/api/quit", "/api/listen", "/api/phone")

# Az Android-alkalmazás: a GitHub Actions fordítja (.github/workflows/android.yml, android/).
APK_URL = "https://github.com/ZsoleszDev/jarvis/releases/download/android-latest/jarvis.apk"

MANIFEST = {
    "name": "Jarvis", "short_name": "Jarvis", "description": "A saját gépeden futó asszisztens",
    "id": "/", "start_url": "/", "scope": "/", "display": "standalone", "orientation": "any",
    "background_color": "#000000", "theme_color": "#000000", "lang": "hu", "dir": "ltr",
    "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
              {"src": "/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
}

SERVICE_WORKER = """// Jarvis (távoli ablak): a telepíthetőséghez és az értesítésekhez. Nem tárol semmit, minden a gépről jön.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.pathname === "/events") return;
  event.respondWith(fetch(event.request));
});

// Értesítés a gépről (Web Push, webpush.py), akkor is, ha az ablak be van zárva. Ha az ablak látszik, ott úgyis
// megjelenik (a jóváhagyás kérdése is), ezért akkor nem szólunk.
self.addEventListener("push", (event) => {
  let message = {};
  try { message = event.data ? event.data.json() : {}; } catch { message = {}; }
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
    if (windows.some((w) => w.visibilityState === "visible")) return undefined;
    const options = { body: message.text || "", tag: message.tag || "jarvis", icon: "/icon-192.png",
                      badge: "/icon-192.png", data: message };
    if (message.kind === "confirm" && message.answer) {
      options.actions = [{ action: "approve", title: "Engedélyezem" }, { action: "deny", title: "Mégse" }];
      options.requireInteraction = true;
    } else if (Array.isArray(message.actions) && message.actions.length) {
      options.actions = message.actions.slice(0, 2).map((a, i) => ({ action: "notice-" + i,
                                                                      title: a.label || "Megnyitás" }));
    }
    return self.registration.showNotification(message.title || "Jarvis", options);
  }));
});

function openWindow() {
  return self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) =>
    windows.length ? windows[0].focus() : self.clients.openWindow("/"));
}

// Egy értesítés gombja (pl. „Eladva itt, vedd le a többiről”): megnyitja a Jarvis ablakát, és az a hirdetés Eladva
// ablakát mutatja, előre kiválasztva (app.js, work.js). Innen semmi nem indul el: a döntés az ablakban a tiéd.
function openAction(action) {
  return self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
    if (windows.length) {
      windows[0].postMessage({ type: "jarvis-action", action });
      return windows[0].focus();
    }
    const hash = new URLSearchParams({ act: action.action || "", item: action.item || "", where: action.where || "",
                                       platform: action.platform || "", delist: action.delist ? "1" : "0" });
    if (action.deal) hash.set("deal", action.deal);   // Automata kereskedés: melyik találatra tegyen ajánlatot
    return self.clients.openWindow("/#" + hash.toString());
  });
}

self.addEventListener("notificationclick", (event) => {
  const message = event.notification.data || {};
  event.notification.close();
  if ((event.action === "approve" || event.action === "deny") && message.answer) {
    const approved = event.action === "approve";
    event.waitUntil(fetch("/api/v1/push/answer", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: message.answer, approved }),
    }).then((response) => {
      if (response.ok) return undefined;
      return self.registration.showNotification("Jarvis", { body: "Ez a kérdés már nem vár válaszra.",
                                                            tag: message.tag, icon: "/icon-192.png" });
    }).catch(() => self.registration.showNotification("Jarvis", {
      body: "Nem érem el a gépet; nyisd meg a Jarvist, és ott válaszolj.", tag: message.tag, icon: "/icon-192.png" })));
    return;
  }
  const button = String(event.action || "");
  const action = button.startsWith("notice-") && (message.actions || [])[Number(button.slice(7))];
  if (action) {
    event.waitUntil(openAction({ ...action, item: message.item || "" }));
    return;
  }
  event.waitUntil(openWindow());
});
"""

LOGIN_CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
             "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'")

LOGIN_PAGE = """<!doctype html>
<html lang="hu">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#000000">
<title>Jarvis</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon-192.png">
<style>
  :root { --black: #000; --graphite: #121212; --rule: #262626; --steel: #666; --ash: #9a9a9a; --paper: #ededed;
          --white: #fff; --ui: "Bahnschrift", "DIN Alternate", "Roboto Condensed", "Arial Narrow", sans-serif;
          --voice: "Sitka Text", "Constantia", "Iowan Old Style", "Charter", Georgia, serif; color-scheme: dark; }
  * { box-sizing: border-box; }
  html, body { margin: 0; min-height: 100%; background: var(--black); color: var(--paper); font: 15px/1.45 var(--ui); }
  main { max-width: 420px; margin: 0 auto; padding: 48px 22px; display: grid; gap: 18px; }
  .eye { width: 96px; height: 96px; justify-self: center; }
  h1 { margin: 0; text-align: center; font-size: 25px; font-weight: 350; }
  p { margin: 0; font: 16px/1.55 var(--voice); color: var(--ash); }
  form { display: grid; gap: 14px; min-width: 0; padding: 18px; border: 1px solid var(--rule); border-radius: 10px;
         background: var(--graphite); }
  label { display: grid; gap: 4px; min-width: 0; font-size: 13.5px; color: var(--ash); }
  input { width: 100%; min-width: 0; padding: 8px 2px; border: 0; border-bottom: 1px solid var(--steel);
          background: transparent; color: var(--paper); font: 17px var(--ui); }
  input:focus { outline: none; border-bottom: 2px solid var(--white); padding-bottom: 7px; }
  #code { font-size: 26px; letter-spacing: 0.18em; }
  button { padding: 10px 15px; border: 1px solid var(--white); border-radius: 6px; background: var(--white);
           color: var(--black); font: 600 15px var(--ui); cursor: pointer; }
  button:disabled { opacity: 0.5; }
  #message { min-height: 1.4em; color: var(--white); }
  a { color: var(--white); }
  a.button { display: block; padding: 11px 15px; border-radius: 6px; background: var(--white); color: var(--black);
             font: 600 15px var(--ui); text-align: center; text-decoration: none; }
  #in-app { display: grid; gap: 12px; }
  #in-app[hidden] { display: none; }
</style>
</head>
<body>
<main>
  <svg class="eye" viewBox="0 0 32 32" aria-hidden="true">
    <circle cx="16" cy="16" r="15" fill="#000" stroke="#666" stroke-width="1"/>
    <circle cx="16" cy="16" r="11" fill="#f2f2f2"/>
    <path d="M16 16 L10 6.5 A11 11 0 0 1 22 6.5 Z" fill="#111"/>
    <circle cx="16" cy="16" r="4" fill="#000"/>
  </svg>
  <h1>Jarvis</h1>
  <p>Ez az eszköz még nincs párosítva. A gépen, a Jarvis ablakában nyomd meg a <b>Telefon</b>, majd az
    <b>Új párosító kód</b> gombot, és írd be ide a hat számjegyet (5 percig érvényes).</p>
  <form id="pair" autocomplete="off">
    <label>Párosító kód
      <input id="code" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" autocomplete="one-time-code" required></label>
    <label>Az eszköz neve
      <input id="device" maxlength="60"></label>
    <button id="go" type="submit">Párosítás</button>
    <div id="message" role="status"></div>
  </form>
  <p id="android" hidden>Androidon kényelmesebb az alkalmazás: akkor is értesít, ha be van zárva.
    <a href="__APK__">Letöltés (jarvis.apk)</a></p>
  <div id="in-app" hidden>
    <a id="open-app" class="button" href="#">Párosítás az alkalmazásban</a>
    <p>Ha még nincs meg az alkalmazás, ez a gomb letölti; telepítés után olvasd be újra a QR-kódot. Vagy
      párosíts itt, a böngészőben (fent).</p>
  </div>
</main>
<script>
"use strict";
const KEY = "jarvis-token";
const say = (text) => { document.getElementById("message").textContent = text; };
const params = new URLSearchParams(location.search);
const android = /Android/i.test(navigator.userAgent) && !window.JarvisApp;   // a telefon böngészője
// A gép Telefon ablakának QR-kódja: a gép címe, a # után a párosító kód (a gépnek nem megy el).
const scanned = /^#pair=(\\d{6})$/.exec(location.hash);
let saved = null;
try { saved = localStorage.getItem(KEY); } catch { saved = null; }
if (scanned) {
  history.replaceState(null, "", "/");
  document.getElementById("code").value = scanned[1];
  if (android) {
    // Az Android-alkalmazás (ha telepítve van) átveszi a címet és a kódot; ha nincs, a letöltés jön.
    document.getElementById("open-app").href = "intent://pair?server=" + encodeURIComponent(location.origin) +
      "&code=" + scanned[1] + "#Intent;scheme=jarvis;package=hu.jarvis.app;S.browser_fallback_url=" +
      encodeURIComponent("__APK__") + ";end";
    document.getElementById("in-app").hidden = false;
  }
  say("A kódot beírtam a QR-kódból.");
} else if (params.get("t")) {
  // A gép nem fogadta el a kulcsot: lejárt, vagy a gépen visszavontad.
  try { localStorage.removeItem(KEY); } catch { /* nem baj */ }
  if (window.JarvisApp && window.JarvisApp.revoked) window.JarvisApp.revoked();   // az Android-alkalmazás maga párosít
  history.replaceState(null, "", "/");
  say("A párosítás megszűnt (talán a gépen visszavontad). Párosítsd újra.");
} else if (saved) {
  // Egy értesítés gombja (/#act=…, lásd a service workert) a Jarvis ablakában folytatódik.
  location.replace("/?t=" + encodeURIComponent(saved) + (/^#act=/.test(location.hash) ? location.hash : ""));
}
const guess = /Android/i.test(navigator.userAgent) ? "Telefon" : /Windows/i.test(navigator.userAgent) ? "Laptop" : "Eszköz";
document.getElementById("device").value = guess;
// A telefon böngészőjében: innen töltheted le az alkalmazást (az alkalmazásban ez nem kell).
document.getElementById("android").hidden = !android || !!scanned;
document.getElementById("pair").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = document.getElementById("go");
  button.disabled = true;
  say("Párosítás…");
  try {
    const response = await fetch("/api/v1/pair", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: document.getElementById("code").value.trim(),
                             device: document.getElementById("device").value.trim() || guess }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !result.token) { say(result.error || "Nem sikerült."); return; }
    try { localStorage.setItem(KEY, result.token); } catch { /* akkor minden indításkor párosítani kell */ }
    location.replace("/?t=" + encodeURIComponent(result.token));
  } catch {
    say("Nem érem el a gépet. Fut a Jarvis, és be van kapcsolva a Tailscale?");
  } finally {
    button.disabled = false;
  }
});
</script>
</body>
</html>
""".replace("__APK__", APK_URL).encode("utf-8")

# A Jarvis ablakának fejlécébe (index.html): a telepíthető alkalmazáshoz kell, és ebből tudja a felület, hogy
# távolról nézik (a mikrofon a telefoné vagy a laptopé, nem a gépé; értesítések).
HEAD_EXTRA = ('<meta name="jarvis-remote" content="1">\n'
              '<link rel="manifest" href="/manifest.webmanifest">\n'
              '<meta name="theme-color" content="#000000">\n'
              '<link rel="apple-touch-icon" href="/icon-192.png">\n')


def denied(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in DENIED)


def inject(page: bytes) -> bytes:
    """A gép felületének oldalába a telepíthető alkalmazás (PWA) kellékei."""
    return page.replace(b"</head>", HEAD_EXTRA.encode("utf-8") + b"</head>", 1)


@lru_cache(maxsize=4)
def icon_png(size: int, maskable: bool = False) -> bytes:
    """Az alkalmazás ikonja: a varázsszem, mint a felület fejlécében (fekete, fehér, szürke)."""
    from PIL import Image, ImageDraw

    big = size * 4                                   # nagyban rajzoljuk, kicsinyítve lesz sima a széle
    image = Image.new("RGBA", (big, big), (0, 0, 0, 255 if maskable else 0))
    draw = ImageDraw.Draw(image)
    scale = big / 32 * (0.72 if maskable else 1.0)   # a maszkolható ikonnál a biztonságos középső részen
    center = big / 2

    def circle(radius: float, **style: object) -> None:
        r = radius * scale
        draw.ellipse([center - r, center - r, center + r, center + r], **style)

    circle(15, fill=(0, 0, 0, 255), outline=(102, 102, 102, 255), width=max(1, round(scale)))
    circle(11, fill=(242, 242, 242, 255))
    start = math.degrees(math.atan2(-9.5, -6.0)) % 360   # a sötét cikk: (10; 6,5) és (22; 6,5) között
    end = math.degrees(math.atan2(-9.5, 6.0)) % 360
    r = 11 * scale
    draw.pieslice([center - r, center - r, center + r, center + r], start, end, fill=(17, 17, 17, 255))
    circle(4, fill=(0, 0, 0, 255))
    out = io.BytesIO()
    image.resize((size, size), Image.LANCZOS).save(out, "PNG", optimize=True)
    return out.getvalue()


def static_file(path: str) -> tuple[bytes, str] | None:
    """A telepíthető alkalmazás kellékei (kulcs nélkül is elérhetők: nincs bennük semmi titkos)."""
    if path == "/manifest.webmanifest":
        return json.dumps(MANIFEST, ensure_ascii=False).encode("utf-8"), "application/manifest+json"
    if path == "/sw.js":
        return SERVICE_WORKER.encode("utf-8"), "text/javascript; charset=utf-8"
    if path in ("/icon-192.png", "/icon-512.png", "/icon-maskable-512.png", "/favicon.ico"):
        size = 192 if path in ("/icon-192.png", "/favicon.ico") else 512
        return icon_png(size, maskable="maskable" in path), "image/png"
    return None


def pair_link(address: str, code: str) -> str:
    """A párosító QR-kód tartalma: a gép címe, a kód a # után (a gépnek nem megy el, a belépő oldal olvassa ki)."""
    return f"{address.rstrip('/')}/#pair={code}"


def serve_address(status: str, port: int) -> str:
    """A `tailscale serve status` kiírásából a gép címe, ha a Tailscale a mi portunkat adja tovább, pl.
    „https://gep.tail1234.ts.net (tailnet only)”, alatta „|-- / proxy http://127.0.0.1:8765”. Különben ""."""
    ours = re.compile(rf"(?:^|[\s/])(?:127\.0\.0\.1|localhost):{port}(?!\d)")
    address = ""
    for line in status.splitlines():
        line = line.strip()
        if line.startswith("https://"):
            address = line.split()[0].rstrip("/")
        elif address and ours.search(line):
            return address
    return ""


def tailscale_address(port: int) -> str:
    """A gép címe a telefonnak és a laptopnak (https://<gép>.<tailnet>.ts.net), ha a `tailscale serve` már
    továbbadja a telefonos kapcsolat portját; "", ha nincs Tailscale, vagy még nincs beállítva."""
    exe = shutil.which("tailscale")
    if exe is None and sys.platform == "win32":
        default = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Tailscale", "tailscale.exe")
        exe = default if os.path.exists(default) else None
    if exe is None:
        return ""
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    try:
        proc = subprocess.run([exe, "serve", "status"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=5, **flags)
    except (OSError, subprocess.SubprocessError):
        return ""
    return serve_address(proc.stdout or "", port)
