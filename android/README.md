# Jarvis Android-alkalmazás

A gépeden futó Jarvis ablaka a telefonon: ugyanaz a felület (Beszélgetés, Munka, Kreatív), ugyanaz a fekete-fehér
téma, de minden a gépen fut (a modellek, a böngésző, a feltöltés). A telefon csak mutatja, és onnan adod a
feladatot. Értesít, ha a Jarvis jóváhagyást kér (az értesítés gombjaival is megadhatod), ha kész vagy elakadt egy
feladat, ha kérdez valamit egy tárgyról, ha egy figyelő új találatot hozott, vagy ha egy hirdetés elkeltnek látszik
(az értesítés „Eladva itt, vedd le a többiről” gombja a hirdetés Eladva ablakát nyitja meg; a levételt ott hagyod
jóvá).

A telepítés és a párosítás leírása a fő README-ben van (Telefonos és laptopos alkalmazás).

## Felépítés

- `MainActivity`: a gép felülete egy WebView-ban (a cím: a gép Tailscale-címe, `?t=<eszközkulcs>`), fájlválasztó
  a fotókhoz, letöltések. Párosítás nélkül, vagy ha a gép nem érhető el, a
  beállító oldal jön (`assets/setup.html`).
- `NotifyService`: háttérszolgáltatás, a gép eseményeit kérdezi le (`/api/v1/events`, hosszú lekérdezéssel), és
  értesít. Firebase nem kell hozzá. Egy `sold` gombos értesítésnél (`notice.actions`) a gomb a `MainActivity`-t
  nyitja meg (`hu.jarvis.app.SOLD`, a tárggyal); a felület a `JarvisApp.pending()` `sold` részéből a tárgy Eladva
  ablakát mutatja. Az értesítésből közvetlenül semmit nem jelöl meg és nem vesz le. Egy megérő, ellenőrzött hirdetés (`notice.deal`)
  a „Megérő hirdetés” csatornán szól, akkor is, ha az alkalmazás nyitva van: `FLAG_INSISTENT`-tel két percig
  ötmásodpercenként halkan csenget (ha van hang) és kettőt rezeg, aztán tíz percre a csendes csatornára vált, és ha
  addig nem nyitottad meg, újra (legfeljebb hatszor). Egy „Érdemes megnézni” tipp (`notice.tip`, a bizonytalan árú és az
  ellenőrizetlen is) és egy ígéretes új hirdetés gyors jelzése még az ellenőrzés előtt (`notice.pending`) a tipp
  csatornáján egy nagyon rövid pittyenéssel szól (akkor is, ha az alkalmazás nyitva van; rábökve az alkalmazás nyílik meg). Egy alkuképes hirdetés (`notice.haggle`) a saját „Alkuképes hirdetés” csatornáján egyszer szól (nem riaszt,
  és csak ha az alkalmazás nincs előtérben). Rábökve mindkettőnél a `MainActivity` nyílik (`hu.jarvis.app.DEAL`), és a
  felület a `pending()` `deal` részéből a találat ablakát mutatja; az üzenetet a `JarvisApp.copy()` teszi a vágólapra. A Szakértői tipp gomb a kérdést
  szintén így másolja ki, és a `JarvisApp.openChatGpt()` megnyitja a ChatGPT alkalmazást (`com.openai.chatgpt`; a
  manifest `<queries>` része miatt látja, telepítve van-e), vagy ha nincs, a chatgpt.com-ot a böngészőben. Egy
  tárgy kérdésének és a Pro feltöltés Nézd át értesítésének (`notice.item`, `notice.open`) rábökve a `MainActivity`
  `hu.jarvis.app.ITEM` szándékkal nyílik, és a felület a `pending()` `item` részéből a tárgyat, illetve a Nézd át
  ablakot mutatja. A nem fontos értesítést (`notice.quiet`, pl. egy oldal robot-ellenőrzést kért) nem mutatja: az
  csak az alkalmazás Értesítések listájában van meg. A készenlét (`state` = `standby`) nem szünet: az állandó
  értesítés ettől nem változik.
- Kamera (Pro feltöltés): a felület `<input capture>` mezőjére a `MainActivity` a kamera-alkalmazást indítja
  (`ACTION_IMAGE_CAPTURE`); a fotó a `PhotoProvider`-en át az alkalmazás gyorsítótárába kerül (nem a galériába), onnan
  olvassa be a WebView. Kamera-engedély nem kell hozzá (a kamera-alkalmazás fényképez).
- Saját, puha hang: a megérő hirdetés csatornájáé `res/raw/jarvis_alert.wav` (halk, háromhangú csengés, utána négy
  másodperc csend: a `FLAG_INSISTENT` ezt ismétli, így ötmásodpercenként egy csengés, nem folyamatos csengetés), a
  jóváhagyásé, az értesítéseké és az alkué `res/raw/jarvis_ping.wav` (két rövid, halk hang), az „Érdemes megnézni”
  csatornáé `res/raw/jarvis_tip.wav` (egyetlen nagyon rövid pittyenés). A hangokat a `make_sounds.py` állítja elő;
  ha változnak, a csatornák azonosítóját is növelni kell (`NotifyService`, most `-3`), mert az Android egy már
  létrehozott csatorna hangját nem cseréli le (a régieket az `OLD_CHANNELS` szerint törli).
- `ActionReceiver`: a jóváhagyás gombjai az értesítésben (`/api/v1/confirm`), és az állandó értesítés Szünet /
  Folytatás gombja (`/api/v1/pause`, `/api/v1/resume`).
- `res/xml/shortcuts.xml`: parancsikon (Új hirdetés).
- Megosztás a Jarvisnak: a galériából megosztott fotókat a `MainActivity` a WebView-ban szolgálja ki
  (`/__jarvis_shared/<köteg>/<sorszám>`, a HEIC-et JPEG-gé alakítva), a gép felülete (`work.js`) ezekből tölti ki
  az új hirdetést; a szöveg a beszélgetésbe kerül. A kéréseket a felület a `JarvisApp.pending()` hívással kérdezi le.
- Párosító link a gép QR-kódjából: `jarvis://pair?server=…&code=…` (a belépő oldal gombja adja át); kitölti a
  beállító oldalt, a párosítást te indítod (így egy idegen link nem párosíthat észrevétlenül másik géphez).
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
