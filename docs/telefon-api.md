# Telefonos kapcsolat (Jarvis API)

Ezzel a saját (Angular + Capacitor) appodból irányíthatod a Jarvist: beszélgetés (írva vagy hangfelvétellel),
a Munka menü tárgyai és fotói, feltöltés, eladva/levétel, jóváhagyások, Szünet és Folytatás, háttérmunka,
figyelők és értesítések. A kész Angular-szolgáltatás: [`angular/jarvis-api.service.ts`](angular/jarvis-api.service.ts).

Minden kérés és válasz JSON (kivéve a fotókat és a hangot). Minden útvonal eleje: `/api/v1`.

## Beállítás (egyszer)

1. **config.toml** (alapból így van):

   ```toml
   [api]
   enabled = true
   host = "127.0.0.1"     # csak a gépen belül; a Tailscale innen veszi át
   port = 8765
   allowed_origins = ["capacitor://localhost", "ionic://localhost", "http://localhost",
                      "https://localhost", "http://localhost:4200", "http://localhost:8100"]
   ```

   Az `allowed_origins` a böngészős (CORS) kérésekhez kell: iOS-en a Capacitor-app `capacitor://localhost`,
   Androidon `https://localhost` (régebbi Capacitornál `http://localhost`), fejlesztés közben az `ng serve`
   `http://localhost:4200`. Ha a CapacitorHttp natív kéréseit használod, a CORS nem számít.

2. **Tailscale** a gépen és a telefonon, ugyanazzal a fiókkal. A gépen egyszer:

   ```powershell
   tailscale serve --bg 8765
   ```

   Ettől a Jarvis a tailneteden HTTPS-en elérhető: `https://<gép-neve>.<tailnet>.ts.net` (a pontos címet a
   `tailscale serve status` kiírja). Csak a saját eszközeid látják, az internet nem. Kikapcsolás:
   `tailscale serve reset`. Ha a Tailscale HTTPS-tanúsítványt kér, a felügyeleti felületén (DNS
   beállítások) kapcsold be a MagicDNS-t és a HTTPS-t.

3. **Párosítás:** a Jarvis ablakában **Telefon** › **Új párosító kód**. Az app elküldi a 6 jegyű kódot, és
   kap egy kulcsot, amit eltesz (pl. Capacitor Preferences vagy biztonságos tároló):

   ```http
   POST /api/v1/pair
   {"code": "123456", "device": "Pixel 8"}

   200 {"ok": true, "token": "…", "device_id": "a1b2c3d4", "name": "Pixel 8"}
   ```

   A kód 5 percig érvényes, és 5 rossz próbálkozás után elvész. A párosított eszközöket a Jarvis ablakában
   (Telefon) látod, és ott vissza is vonhatod őket.

## Hitelesítés

Minden kéréshez (a `/ping` és a `/pair` kivételével):

```http
Authorization: Bearer <kulcs>
```

A képet (`<img src>`) és a hangot (`<audio src>`) a böngésző nem tudja fejléccel kérni: ennél a két
végpontnál (`…/photos/01.jpg`, `/tts`) a kulcs a címben is lehet: `?access_token=<kulcs>`.

## Válaszok és hibák

Siker: `{"ok": true, …}`. Hiba: `{"ok": false, "error": "érthető magyar szöveg", "code": "…"}`.

| HTTP | code | Mikor |
|---|---|---|
| 400 | `bad_request` | hibás vagy hiányzó adat |
| 401 | `unauthorized` | nincs vagy rossz kulcs (újra kell párosítani) |
| 403 | `pairing_failed` | rossz vagy lejárt párosító kód |
| 404 | `not_found` | nincs ilyen végpont, tárgy, fotó, figyelő |
| 409 | `paused` | a Jarvis szünetel (lásd `resume`) |
| 409 | `acknowledge` | az oldalt még nem fogadtad el (botokra vonatkozó figyelmeztetés, lásd `/work/ack`) |
| 409 | `no_voice` / `not_posted` | nincs beszédfelismerés / nincs honnan levenni |
| 413 | `too_large` | túl nagy kérés (JSON 256 KB, fotó 25 MB, hang 15 MB) |
| 503 | `starting`, `no_speech`, `no_search`, `no_work` | még indul, vagy az a rész nem fut |

## Végpontok

### Állapot, beszélgetés, Szünet

| Módszer | Útvonal | Törzs / paraméterek | Válasz |
|---|---|---|---|
| GET | `/ping` | – (kulcs nélkül) | `{name, api}` |
| GET | `/status` | | `{ready, state, paused, standby, busy, mode, model, voice, speech, work, confirm, scheduler, unread, last_event}` |
| POST | `/chat` | `{text, mode?: "chat"\|"work", wait?: 0–120, speak?: false, resume?: false}` | `wait` nélkül 202 `{request_id}`; `wait`-tel 200 `{request_id, done, heard, reply, errors}` |
| POST | `/voice?mode=&wait=&speak=&resume=` | nyers hangfájl (m4a, aac, webm, ogg, wav, mp3) | mint a `/chat` (a `heard` a felismert szöveg) |
| GET | `/tts?text=` | | `audio/wav` (Jarvis hangja; a kulcs lehet `access_token`) |
| GET | `/events?after=&wait=` | `after`: az utolsó látott sorszám, `wait`: legfeljebb 55 mp | `{events: [...], last, gap}` |
| POST | `/stop` | | Állj: megszakítja, amit épp csinál |
| POST | `/pause` | | Szünet: minden modell leáll, a VRAM és a memória felszabadul |
| POST | `/resume` | | Folytatás: újra betölti a modelleket (10–20 mp) |
| POST | `/reset` | `{mode?}` | új beszélgetés |
| GET | `/confirm` | | `{confirm: {id, prompt} \| null}`: épp jóváhagyásra váró lépés |
| POST | `/confirm` | `{id, approved}` | jóváhagyás vagy elutasítás (pl. közzététel a telefonról) |

A `speak` alapból `false`: a telefonról jött kérésre a gép hangszórója nem szólal meg (a választ a
telefon felolvashatja a `/tts`-sel vagy a saját hangjával). Ha a Jarvis szünetel, a `/chat` és a `/voice`
409-et ad; `resume: true` (vagy `?resume=1`) esetén előbb újraindul, és utána válaszol.

**Készenlét** (`standby: true`; a gép bekapcsolásakor indított Jarvis, vagy tétlenség után): a modellek nincsenek
betöltve, de a `/chat` és a `/voice` nem ad 409-et, mert a kérés magától felébreszti (10–20 mp), és utána
válaszol; ilyenkor érdemes nagyobb `wait`-et adni (pl. 60). A `/pause` készenlétből igazi szünetre vált: onnan
csak a `/resume` (vagy a `resume: true`) hozza vissza. Készenlétben a modell nélküli háttérmunka (figyelők, régóta
fent lévő hirdetések) fut, szünetben semmi.

### Munka menü: Adás/Vétel

| Módszer | Útvonal | Törzs | Megjegyzés |
|---|---|---|---|
| GET | `/work` | | `{items, platforms, needs, settings, acknowledged, conditions, max_photos, research, browser, messages}` (`messages`: van-e oldal, ahol a vevői üzeneteket olvassa) |
| POST | `/work/items` | `{targets: ["vinted", …], prompt, research_wanted?}` | új tárgy (201) |
| GET | `/work/items/{id}` | | egy tárgy |
| PATCH | `/work/items/{id}` | `{fields: {title?, description?, price?, …}}` | amit itt átírsz, azt Jarvis már nem írja felül; a válaszban `drift_note` (lásd lent) |
| DELETE | `/work/items/{id}` | | a `_torolt` mappába kerül |
| POST | `/work/items/{id}/photos` | nyers kép (JPG, PNG, WEBP, HEIC) | 201, a tárgy |
| GET | `/work/items/{id}/photos/01.jpg?thumb=1` | | `image/jpeg` (a kulcs lehet `access_token`) |
| DELETE | `/work/items/{id}/photos/01.jpg` | | |
| POST | `/work/items/{id}/photos/order` | `{order: ["03.jpg", …]}` | az első a borítókép |
| POST | `/work/items/{id}/sell` | `{resume?}` | adatok a leírásból, visszakérdezés, árkutatás, feltöltés (202) |
| POST | `/work/items/{id}/reply` | `{text}` | válasz Jarvis kérdésére (a tárgy `pending` mezője) |
| POST | `/work/items/{id}/price` | `{choice: "keep" \| "suggested" \| "confirmed" \| 4500}` | árdöntés; `confirmed`: igen a visszakérdezett összegre (`pending.confirm`) |
| POST | `/work/items/{id}/cancel` | | a függő kérdés elvetése |
| POST | `/work/items/{id}/post` | `{platform}` | egy oldalra (újra) |
| POST | `/work/items/{id}/research` | | „Mennyit ér?” |
| POST | `/work/items/{id}/sold` | `{where?: "vinted", delist?: true}` vagy `{sold: false}` | Eladva; `delist`-tel a többi oldalról Jarvis leveszi |
| POST | `/work/items/{id}/delist` | `{platforms?: ["jofogas"]}` | levétel (alapból mindenhonnan, ahol fent van) |
| POST | `/work/items/{id}/mark` | `{platform, status: "feltöltve" \| "nincs fent" \| "hiba", url?}` | csak a nyilvántartás |
| POST | `/work/items/{id}/dismiss` | `{platform}` | „Nem igaz, fent van”: a háttérellenőrzés téves jelzése (elkelt, eltűnt) azon az oldalon; 200, a tárgy (400, ha ott nincs fent) |
| PUT | `/work/settings` | `{settings: {city, shipping, parcel_size}}` | Alapadatok |
| POST | `/work/ack` | `{platform}` | tudomásul vetted, hogy az oldal szabályai tilthatják a botokat |

A tárgy fontos mezői:

- `status` (`vázlat`, `kérdés`, `árdöntés`, `részben fent`, `fent`, `eladva`), `busy` (min dolgozik épp), `photos`,
  `price`, `min_price` (alkunál ennél kevesebbet a válaszvázlat nem fogad el; üres: az ár), `sold`.
- `pending`: `{kind: "info" | "price", question, suggested?, confirm?}`. A `confirm` egy furcsa összeg, amire Jarvis
  visszakérdezett („3 Ft-ra gondoltál?”): ilyenkor a `price` `{choice: "confirmed"}` az igen.
- `platforms[kulcs]`: `{status, at, url?, note?, dossier?, posted_at?, posted_price?, posted_title?, check?,
  checked_at?, check_note?, check_dismissed?}`. `posted_at`: mióta van fent; `posted_price`, `posted_title`: az ár és
  a cím a feltöltéskor. A háttérellenőrzés (lásd lent) eredménye: `check` (`live` fent van, `sold` elkeltnek látszik,
  `gone` már nincs fent, `unknown` nem nyílt meg), `checked_at` (mikor), `check_note` (miből látszik, vagy miért
  nem nyílt meg); `check_dismissed`: ezt a jelzést nem igaznak jelölted (`/dismiss`), pontosan ugyanazt nem hozza fel újra (`check_dismissed_note`), egy másikat igen.
- `bought_price`, `bought_at`, `source`, `sold_price`, `sold_at`: haszonkövetés (mennyiért vetted, honnan, mennyiért
  adtad el; az `sold_price` az Eladva jelöléskor az akkori ár). A `bought_price` és a `sold_price` a `PATCH`-csel átírható.
- `drift`: ahol a fent lévő hirdetésben még a régi ár vagy cím van (itt átírtad, ott nem; Jarvis a fent lévő
  hirdetést magától nem szerkeszti): `[{platform, price?, title?}]`, csak az eltérő mezőkkel.
- `online_since` (mióta van fent valahol, Unix-idő vagy `null`), `days_online` („12 napja fent”, vagy `null`).
- `research`: `{query, at, count, kept, dropped, similar, errors, reason?, min?, max?, p25?, median?, p75?, n?,
  confidence?, outliers?, suggested?}`. `count`: ennyi találat volt; `kept`: ennyi ment a modell elé; `dropped`: miért
  maradt ki a többi (`{"kereső": 3, "csomag": 1, "alkatrész": 1, "saját": 1, "más termék": 4, "ismétlődő": 1,
  "kevésbé illő": 5}`); `similar`: a hasonlók (`{site, text, price, url, auction?}`, az `auction` licit, csak a
  mostani ajánlat); `p25`–`p75`: a hasonlók középső fele, `median` a középérték, `n` ennyi ár alapján, `outliers`
  ennyi kilógó ár maradt ki; `confidence`: `megbízható` vagy `bizonytalan` (kevés vagy nagyon eltérő ár).
  `research_stale`: igaz, ha a kutatás egy hétnél régebbi (a következő árazás előtt frissül). `verdict`: Jarvis
  véleménye az árról, egy mondatban.

**`drift_note`** (a `PATCH /work/items/{id}` válaszában): ha a most átírt ár vagy cím a fent lévő hirdetésben még
a régi, Jarvis ezt mondja róla, pl. „Csak itt írtam át; a Vinteden még 9 000 Ft. A fent lévő hirdetést magamtól nem
szerkesztem: ott kézzel kell átírni (vagy levenni, és újra feltölteni).”; különben üres. Az app írja ki.

A fotóellenőrzés (modell nélkül, csak jelzés: a feltöltést nem állítja meg): `photo_warnings`
(`{"03.jpg": [{kind, text}]}`, csak a fotók, amelyekkel gond van; `kind`: `blurry` homályos, `dark` túl sötét,
`bright` túl világos, `clipped` ellenfényes, `small` kicsi, `duplicate` majdnem ugyanaz, mint egy másik fotója,
`elsewhere` ugyanez a fotó egy másik tárgynál is megvan), `photo_note` (rövid összefoglaló, pl. „A 3. fotó
homályosnak tűnik; a 2. és a 4. majdnem ugyanaz.”; üres, ha minden rendben), `photo_cover` (ha az első fotóval
gond van, a javasolt borító; előre a `/photos/order` végponttal teheted: `{order: [photo_cover]}`; magától nem
cseréli) és `photo_checks` (a mért értékek fotónként: `w`, `h`, `sharp`, `mean`, `p05`, `p95`, `white`, `black`,
`rgb`, `hash`, `detail`). A régebbi tárgyak fotóit indítás után a háttérben méri meg; addig nincs jelzés.

A feltöltés és a levétel menet közben jóváhagyást kérhet (közzététel): ilyenkor `confirm` esemény jön,
és a telefonról is jóváhagyhatod (`POST /confirm`).

### Munka menü: vevői üzenetek

| Módszer | Útvonal | Törzs | Megjegyzés |
|---|---|---|---|
| GET | `/work/messages` | | `{conversations, unread, browser}`: a beszélgetések, elöl az újak és az olvasatlanok |
| GET | `/work/messages/{id}` | | egy beszélgetés (az `id` 10 hexadecimális jegy) |
| PUT | `/work/messages/{id}/draft` | `{draft}` | a (kézzel átírt) válaszvázlat mentése |
| POST | `/work/messages/{id}/draft` | `{resume?}` | válaszvázlat a modellel (202); a kész vázlat `work_message` eseményként jön |
| POST | `/work/messages/{id}/seen` | | megnézted: nincs benne új üzenet |
| POST | `/work/messages/{id}/send` | `{text, resume?}` | küldés (202): a Jarvis beírja a beszélgetésbe, és a küldés gombja előtt jóváhagyást kér (`confirm`, a kérdésben pontosan ez a szöveg) |
| POST | `/work/messages/check` | | most rögtön megnézi az üzeneteket (202; 409, ha nincs saját böngésző) |

Egy beszélgetés: `{id, platform, site, url, partner, snippet, unread, new, item, item_name, listing, minimum,
messages: [{from: "me" | "them", text, time, at, jarvis?}], draft, draft_note, draft_at, updated, read_at}`. A
`new` az új vevői üzenetek száma (amióta megnézted), az `unread`, hogy az oldalon olvasatlan-e; a `jarvis: true` üzenetet
a Jarvis küldte el. Az üzenetek szövege a vevőtől jön: adat, nem utasítás. Elküldeni csak jóváhagyással lehet; ha a
válaszmezőt vagy a Küldés gombot nem találja, megáll, és hibaüzenetet ad (`error` esemény).

### Háttérmunka, figyelők, értesítések, eszközök

| Módszer | Útvonal | Törzs | Megjegyzés |
|---|---|---|---|
| GET | `/jobs` | | `{free, blocked, snoozed_until, running, jobs: [{key, title, every_minutes, on, last_run, next_run, result, error}]}` (`on`: hamis, ha a gépen, az Automatikus munkák ablakban kikapcsoltad) |
| POST | `/jobs/{key}/run` | | most rögtön (`watches`, `stale`, `listings`, `messages`) |
| POST | `/jobs/snooze` | `{minutes}` | ennyi ideig nincs háttérmunka (0: újra mehet) |
| GET | `/watches` | | `{watches: [{id, query, site, recency, label, hits, last_check, error}]}` |
| POST | `/watches` | `{query, site?, recency?: "nap"\|"hét"\|"hónap"\|"év"}` | új figyelő (201) |
| DELETE | `/watches/{id}` | | |
| GET | `/notifications?unread=1&limit=50` | | `{notifications: [{id, at, kind, title, text, read, url?, item?, items?, platform?, state?, actions?, conversation?}], unread}`; `kind`: `watch`, `stale`, `sold` (elkeltnek vagy eltűntnek látszik, lásd lent), `delist`, `post` (kész vagy elakadt feltöltés), `question` (kérdés egy tárgyról), `message` (új vevői üzenet, vagy egy küldés eredménye; `conversation`, `item?`), `error`, `info` |
| POST | `/notifications/read` | `{ids?: [...]}` | olvasottnak jelöl (ids nélkül mindet) |
| GET | `/devices` | | párosított eszközök (`current`: ez az) |
| DELETE | `/devices/{id}` | | visszavonás (a sajátodat is: kijelentkezés) |

A háttérmunkák (`key`): `watches` (figyelők), `stale` (régóta fent lévő hirdetések), `listings` (a fent lévő
hirdetések ellenőrzése: naponta legfeljebb `[scheduler] listing_checks` hirdetés oldalát nyitja meg Jarvis
böngészőjében, csak olvasva, emberi tempóban; böngésző kell hozzá, modell nem).

**Elkelt máshol? (`kind: "sold"`)** Ha a `listings` szerint egy hirdetés elkeltnek vagy eltűntnek látszik:
`{kind: "sold", item, platform: "vinted", state: "sold" | "gone", actions: [{action: "sold", where: "vinted",
delist: true, label: "Eladva itt, vedd le a többiről"}]}` (ha máshol nincs fent: `delist: false`, `label: "Eladva
itt"`). Az app gombként mutassa: nyissa meg a tárgy Eladva-kérdését előre kiválasztva (`where`, `delist`), és csak a
felhasználó jóváhagyása után küldje: `POST /work/items/{item}/sold {where, delist}`. Ha a jelzés téves:
`POST /work/items/{item}/dismiss {platform}`. Ha az ellenőrzés robot-ellenőrzésbe (captcha) vagy kijelentkezett
oldalba ütközött: `{kind: "error", platform, actions: [{action: "login", platform, label: "Bejelentkezés a
böngészőmben"}]}`; ez a gép Jarvis-ablakában (`/api/work/action {action: "login", platform}`) megnyitja az oldalt
Jarvis böngészőjében, ahol a felhasználó maga lép be.

### Értesítés bezárt ablaknál (Web Push)

A böngésző (pl. a laptopon az Edge) a gép kulcsával feliratkozik; a gép a `confirm` és a `notification`
eseményeknél titkosított push-üzenetet küld a böngésző push-szolgáltatásának (RFC 8291, VAPID: RFC 8292). Ehhez a
gépen a `cryptography` csomag kell. Csak az ismert push-szolgáltatók címei fogadhatók el (Microsoft, Google,
Mozilla, Apple).

| Módszer | Útvonal | Törzs | Megjegyzés |
|---|---|---|---|
| GET | `/push` | | `{available, key, reason, subscriptions}`: a gép VAPID-kulcsa (`applicationServerKey`); ha nincs, `reason` mondja meg, miért |
| POST | `/push/subscribe` | `{subscription}` | a `PushSubscription.toJSON()`; eszközönként legfeljebb 4 |
| POST | `/push/unsubscribe` | `{endpoint?}` | endpoint nélkül az eszköz minden feliratkozása |
| POST | `/push/answer` | `{key, approved}` | **kulcs nélkül**: a jóváhagyó push-üzenet egyszer használható `answer` kulcsával (5 percig, vagy amíg a jóváhagyás vár) |

A push-üzenet (JSON, titkosítva): `{kind: "notice", title, text, tag, item?, actions?}` vagy `{kind: "confirm", id,
title, text, answer, tag}`. Az `actions` az értesítés gombjai (`sold`, `login`, legfeljebb kettő, lásd fent); a
service worker gombként mutatja, és a gomb a Jarvis ablakát nyitja meg (`/#act=sold&item=…&where=…&delist=1`, nyitott
ablaknak `postMessage`-dzsel szól), ahol a felhasználó dönt. A gomb maga semmit nem küld a gépnek.

## Események (élő napló)

A Jarvis minden lépése esemény: amit mondasz és amit válaszol, az eszközei, az állapota, a jóváhagyások,
a tárgyak változásai, az értesítések. Az app egy hosszú lekérdezéssel követi őket:

```http
GET /api/v1/events?after=0&wait=25
200 {"events": [{"seq": 41, "type": "user", "text": "Szia", "via": "phone", "channel": "chat", "request_id": "…"},
                {"seq": 42, "type": "assistant", "text": "Szia! Miben segíthetek?", "channel": "chat", …}],
     "last": 42, "gap": false}
```

Ha nincs új esemény, a kérés legfeljebb `wait` másodpercig vár, aztán üresen tér vissza; utána a `last`
értékkel kérd újra. Ha `gap: true`, közben események vesztek el (pl. sokáig nem kérdezted): töltsd újra a
`/status`-t és a `/work`-öt.

| type | Mezők | Jelentése |
|---|---|---|
| `hello` | `name, model, voice, speech, wake_word, work, paused, standby, api` | mi fut (szünet után is jön) |
| `state` | `state, text, detail` | `idle`, `listening`, `transcribing`, `thinking`, `working`, `speaking`, `paused`, `standby`, `starting`, `error` |
| `user` | `text, via, channel` | amit mondtál vagy írtál (`via`: `text`, `voice`, `phone`) |
| `assistant` | `text, channel` | Jarvis válasza vagy üzenete |
| `tool` | `id, label, status, summary, channel` | egy lépés (`running`, `done`, `failed`) |
| `info`, `warn`, `error`, `timing` | `text, channel` | megjegyzések |
| `confirm` / `confirm_done` | `id, prompt` / `id, approved` | jóváhagyás kell / megvolt |
| `work_item` / `work_removed` | `item` / `id` | egy tárgy változott / törölve |
| `work_message` | `conversation` | egy vevői beszélgetés változott (új üzenet, vázlat, elküldve) |
| `notification` | `notice` | értesítés (figyelő, régóta fent lévő hirdetés, elkeltnek látszó hirdetés, levétel), a gombjaival (`actions`) |
| `request_done` | `request_id` | a kérésed feldolgozása véget ért |

A `channel` mondja meg, melyik menühöz tartozik (`chat` = Beszélgetés, `work` = Munka). A telefonról
indított kérés eseményein ott a `request_id` is.

## Hang a telefonról

```http
POST /api/v1/voice?mode=chat&wait=60
Content-Type: audio/mp4

<a felvétel bájtjai>

200 {"heard": "Milyen idő lesz holnap?", "reply": "Holnap 18 fok lesz, napos idő.", "done": true}
```

A Jarvis a gépen ugyanazzal a beszédfelismerővel (faster-whisper) alakítja szöveggé, amivel a mikrofonodat
hallgatja, és ugyanúgy válaszol, mintha mondtad volna. A válasz felolvasása a telefonon:
`GET /api/v1/tts?text=…&access_token=…` (WAV), vagy a telefon saját felolvasójával.

## Capacitor tippek

- **HTTPS a Tailscale-lel:** a `tailscale serve` valódi tanúsítványt ad, így Androidon nem kell külön
  engedélyezni a sima HTTP-t, iOS-en sincs ATS-gond.
- **CORS vagy natív kérés:** az Angular `HttpClient` a WebView-ból fut, ezért a Jarvis a fenti
  `allowed_origins`-re válaszol CORS-fejlécekkel. Ha a `CapacitorHttp` be van kapcsolva
  (`plugins: { CapacitorHttp: { enabled: true } }`), a kérések natívan mennek, és a CORS nem számít.
- **A kulcs tárolása:** a mintaszolgáltatás a `localStorage`-ot használja; éles appban jobb a
  `@capacitor/preferences` vagy egy biztonságos tároló bővítmény.
- **Kamera:** `Camera.getPhoto({ resultType: CameraResultType.Uri })`, majd
  `fetch(photo.webPath).then(r => r.blob())`, és ezt a blobot küldd a `/photos` végpontra.
- **Hangfelvétel:** bármelyik felvevő bővítmény jó, ami fájlt vagy base64-et ad (AAC/M4A/WEBM).
- **Értesítés háttérben:** az események csak akkor jönnek, amíg az app nyitva van. A Jarvis gépe
  napközben fut; ha az app előtérbe kerül, a `/notifications?unread=1` megmutatja, mi történt közben.

## Biztonság röviden

- Alapból csak a `127.0.0.1`-en figyel; kívülről csak a Tailscale-en át érhető el. Ne tedd `0.0.0.0`-ra,
  és ne irányíts rá portot a routeren.
- Eszközönkénti kulcs; a gépen csak a hash-e van meg (`%LOCALAPPDATA%\Jarvis\telefon.json`).
- Jelszót és bankkártyaadatot a Jarvis a telefonról sem ír be; a kockázatos lépések (közzététel, törlés)
  ugyanúgy jóváhagyást kérnek, csak a telefonon is jóváhagyhatod őket.

## A Jarvis ablaka (telefon, laptop)

Ugyanezen a porton (a Tailscale-en át) a Jarvis teljes ablaka is elérhető: `GET /` párosítás nélkül a belépő oldal
(itt a 6 jegyű kóddal párosíthatsz, a kulcs a böngésző tárában marad), a kulccsal (`/?t=<kulcs>`) a gép felülete.
A felület kérései (`/events`, `/api/...`, kulccsal a címben vagy az `X-Jarvis-Token` fejlécben) a gépen futó
felülethez mennek tovább; ami csak a gép előtt ülve szabad (kilépés, új párosító kód, a gép mikrofonja), az 403.
Telepíthető alkalmazás (PWA): `/manifest.webmanifest`, `/sw.js`, `/icon-192.png`, `/icon-512.png`. Az
Android-alkalmazás (android/) ezt mutatja, és a `GET /api/v1/events` hosszú lekérdezéssel értesít a háttérben
(`confirm`, `confirm_done`, `notification`, és a `state` alapján az állandó értesítés Szünet / Folytatás gombja). Egy
`sold` gombos értesítésnél az Android-értesítésen is ott a gomb: az alkalmazást a tárgy Eladva ablakánál nyitja meg
(a felület a `JarvisApp.pending()` válaszának `sold: {item, where, delist}` részéből), a levétel ott, jóváhagyással
indul. A gép felületének saját végpontjai ehhez: `GET /api/notices` (az Értesítések ablak), `POST /api/notices/read`,
`POST /api/work/dismiss {id, platform}`, és a `POST /api/work/update` válaszában is ott a `drift_note`.

Párosítás QR-kóddal: a gép Telefon ablaka (ha a `tailscale serve` már átadja a portot) a `https://<gép>/#pair=<kód>`
címet mutatja QR-kódként. A belépő oldal a `#` utáni kódot beírja (a gépnek nem megy el), Android-böngészőben pedig
az alkalmazásnak is átadhatja: `intent://pair?server=…&code=…#Intent;scheme=jarvis;package=hu.jarvis.app;…`.
