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
| GET | `/work` | | `{items, platforms, needs, settings, acknowledged, conditions, max_photos, research, browser}` |
| POST | `/work/items` | `{targets: ["vinted", …], prompt, research_wanted?}` | új tárgy (201) |
| GET | `/work/items/{id}` | | egy tárgy |
| PATCH | `/work/items/{id}` | `{fields: {title?, description?, price?, …}}` | amit itt átírsz, azt Jarvis már nem írja felül |
| DELETE | `/work/items/{id}` | | a `_torolt` mappába kerül |
| POST | `/work/items/{id}/photos` | nyers kép (JPG, PNG, WEBP, HEIC) | 201, a tárgy |
| GET | `/work/items/{id}/photos/01.jpg?thumb=1` | | `image/jpeg` (a kulcs lehet `access_token`) |
| DELETE | `/work/items/{id}/photos/01.jpg` | | |
| POST | `/work/items/{id}/photos/order` | `{order: ["03.jpg", …]}` | az első a borítókép |
| POST | `/work/items/{id}/sell` | `{resume?}` | adatok a leírásból, visszakérdezés, árkutatás, feltöltés (202) |
| POST | `/work/items/{id}/reply` | `{text}` | válasz Jarvis kérdésére (a tárgy `pending` mezője) |
| POST | `/work/items/{id}/price` | `{choice: "keep" \| "suggested" \| 4500}` | árdöntés |
| POST | `/work/items/{id}/cancel` | | a függő kérdés elvetése |
| POST | `/work/items/{id}/post` | `{platform}` | egy oldalra (újra) |
| POST | `/work/items/{id}/research` | | „Mennyit ér?” |
| POST | `/work/items/{id}/sold` | `{where?: "vinted", delist?: true}` vagy `{sold: false}` | Eladva; `delist`-tel a többi oldalról Jarvis leveszi |
| POST | `/work/items/{id}/delist` | `{platforms?: ["jofogas"]}` | levétel (alapból mindenhonnan, ahol fent van) |
| POST | `/work/items/{id}/mark` | `{platform, status: "feltöltve" \| "nincs fent" \| "hiba", url?}` | csak a nyilvántartás |
| PUT | `/work/settings` | `{settings: {city, shipping, parcel_size}}` | Alapadatok |
| POST | `/work/ack` | `{platform}` | tudomásul vetted, hogy az oldal szabályai tilthatják a botokat |

A tárgy fontos mezői: `status` (`vázlat`, `kérdés`, `árdöntés`, `részben fent`, `fent`, `eladva`),
`pending` (`{kind: "info" | "price", question, suggested?}`), `platforms[kulcs]`
(`{status, at, url?, note?}`), `busy` (min dolgozik épp), `photos`, `price`, `research`.

A feltöltés és a levétel menet közben jóváhagyást kérhet (közzététel): ilyenkor `confirm` esemény jön,
és a telefonról is jóváhagyhatod (`POST /confirm`).

### Háttérmunka, figyelők, értesítések, eszközök

| Módszer | Útvonal | Törzs | Megjegyzés |
|---|---|---|---|
| GET | `/jobs` | | `{free, blocked, snoozed_until, running, jobs: [{key, title, every_minutes, last_run, next_run, result, error}]}` |
| POST | `/jobs/{key}/run` | | most rögtön (`watches`, `stale`) |
| POST | `/jobs/snooze` | `{minutes}` | ennyi ideig nincs háttérmunka (0: újra mehet) |
| GET | `/watches` | | `{watches: [{id, query, site, recency, label, hits, last_check, error}]}` |
| POST | `/watches` | `{query, site?, recency?: "nap"\|"hét"\|"hónap"\|"év"}` | új figyelő (201) |
| DELETE | `/watches/{id}` | | |
| GET | `/notifications?unread=1&limit=50` | | `{notifications: [{id, at, kind, title, text, read, url?, item?}], unread}` |
| POST | `/notifications/read` | `{ids?: [...]}` | olvasottnak jelöl (ids nélkül mindet) |
| GET | `/devices` | | párosított eszközök (`current`: ez az) |
| DELETE | `/devices/{id}` | | visszavonás (a sajátodat is: kijelentkezés) |

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
| `notification` | `notice` | értesítés (figyelő, régóta fent lévő hirdetés, levétel) |
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
