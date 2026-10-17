# Jarvis Android-alkalmazás

A gépeden futó Jarvis ablaka a telefonon: ugyanaz a felület (Beszélgetés, Munka, Kreatív), ugyanaz a fekete-fehér
téma, de minden a gépen fut (a modellek, a böngésző, a feltöltés). A telefon csak mutatja, és onnan adod a
feladatot. Értesít, ha a Jarvis jóváhagyást kér (az értesítés gombjaival is megadhatod), ha kész vagy elakadt egy
feladat, ha kérdez valamit egy tárgyról, vagy ha egy figyelő új találatot hozott.

A telepítés és a párosítás leírása a fő README-ben van (Telefonos és laptopos alkalmazás).

## Felépítés

- `MainActivity`: a gép felülete egy WebView-ban (a cím: a gép Tailscale-címe, `?t=<eszközkulcs>`), fájlválasztó
  a fotókhoz, mikrofon a „Hallgass meg” gombhoz, letöltések. Párosítás nélkül, vagy ha a gép nem érhető el, a
  beállító oldal jön (`assets/setup.html`).
- `NotifyService`: háttérszolgáltatás, a gép eseményeit kérdezi le (`/api/v1/events`, hosszú lekérdezéssel), és
  értesít. Firebase nem kell hozzá.
- `ActionReceiver`: a jóváhagyás gombjai az értesítésben (`/api/v1/confirm`).
- `BootReceiver`: a telefon újraindulása után a háttérértesítések maguktól újraindulnak.

Külső könyvtár nincs; Java 17, Android 8.0-tól (minSdk 26).

## Fordítás

A GitHub Actions fordítja (`.github/workflows/android.yml`), minden olyan feltöltésnél, ami az `android/` mappát
érinti, és kiteszi ide: `https://github.com/<tulajdonos>/jarvis/releases/download/android-latest/jarvis.apk`.
Kézzel: Android Studio, vagy `gradle -p android assembleRelease` (Gradle 8.10+, JDK 17, Android SDK 34).

## Saját aláíró kulcs (nem kötelező)

Enélkül minden fordítás más kulccsal van aláírva, és egy új változat telepítése előtt a régit el kell távolítani
a telefonról (utána újra kell párosítani). Ha egyszer beállítod a saját kulcsot, az új változat simán felülírja
a régit:

```
keytool -genkeypair -keystore jarvis.keystore -alias jarvis -keyalg RSA -keysize 2048 -validity 10000
```

Majd a GitHubon (Settings › Secrets and variables › Actions) négy titok: `JARVIS_KEYSTORE_B64` (a fájl
base64-ben, PowerShellben: `[Convert]::ToBase64String([IO.File]::ReadAllBytes("jarvis.keystore"))`),
`JARVIS_KEYSTORE_PASSWORD`, `JARVIS_KEY_ALIAS` (`jarvis`) és `JARVIS_KEY_PASSWORD`. A kulcsfájlt tartsd meg
(és ne tedd a repóba).
