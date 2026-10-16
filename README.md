# Jarvis – helyi, magyarul beszélő asszisztens

Megszólítod („Hey Jarvis”), ő magyarul válaszol, és közben dolgokat csinál a gépeden és a weben: keres, olvas, kattint, kitölt, és ha kell, megnézi a képernyődet. A Kreatív menüben pedig mesélő: közösen írhattok egy kalandot (mint az AI Dungeon), vagy beszélgethetsz egy kitalált szereplővel (mint a Character.AI). Saját ablaka van, de konzolból is használható. Minden helyben fut; a modell, a beszéd és a képek nem hagyják el a gépet.

```
mikrofon ─► faster-whisper ─► Qwen3.8-27B (Ollama) ─► Piper ─► hangszóró
            (beszéd → szöveg)      │        ▲          (szöveg → beszéd)
                                   ▼        │
      eszközök: gép (programok, fájlok, jegyzet, időzítő)
                web (keresés, oldalolvasás)
                saját böngésző (lát és kattint)
                látás (képernyő és weboldal képe)
```

## Mit tud

| Terület | Példa |
|---|---|
| idő, gépállapot (CPU, RAM, lemez, GPU-k) | „Mennyi az idő?”, „Hogy van a gép?” |
| program indítása (csak a listáról) | „Nyisd meg a jegyzettömböt” |
| fájlkeresés, szöveges fájl olvasása, megnyitása | „Foglald össze a legutóbbi txt-t a letöltésekben” |
| jegyzet, időzítő | „Jegyezd fel…”, „Szólj 10 perc múlva…” |
| **webes keresés**: látja a találatokat | „Mi a legfrissebb hír a Forma–1-ről?” |
| **oldalolvasás**: elolvassa és összefoglalja | „Mit ír erről a Wikipédia?” |
| **saját böngésző**: kattint, gépel, görget | „Keress rá a Hasznaltauton egy 2018-as Octaviára, és mondd meg a legolcsóbbat” |
| **képernyőnézés**: megnézi a monitorodat | „Mi ez a hibaüzenet?”, „Mit látsz a képernyőn?” |
| **eladás** (Munka › Adás/Vétel): kipipálod, hova kerüljön, fotók, egy mondat, és feltölti | „Zara L-es fekete póló, egyszer hordott, 500 Ft” → Vinted, Jófogás |
| **eladva → levétel**: megmondod, hol kelt el, és a többi oldalról Jarvis leveszi | „A pulóver elkelt a Vinteden, vedd le a többiről” |
| **begyakorolt lépések**: egy oldalon egyszer a modell tölti fel (vagy veszi le), utána modell nélkül | – |
| **emberi tempó**: a böngészőben az egér odamegy a gombhoz, billentyűnként gépel, szünetet tart, és két közzététel között pár percet vár | – |
| **hibacsomag**: ha egy feltöltés vagy levétel elakad, a képernyőkép, az oldalnézet és a lépések egy mappában | **Hibacsomag** gomb |
| **figyelők**: a háttérben időnként újra keres, és szól, ha új találat jelent meg | „Szólj, ha lesz új Python-állás Debrecenben” |
| **több oldal egy lépésben**, egy adott oldalon vagy csak friss találatokra | „Mit írnak az állásoldalak a junior Python fizetésekről?” |
| **robotvédett oldalak**: ha egy oldal a sima letöltést elutasítja (403), a saját böngészőjével olvassa el, külön lapon | „Keress egy megbízható, olcsó ébresztőórát” |
| **Kreatív › kaland** (mint az AI Dungeon): te írod, mit teszel, a mesélő folytatja | `> You follow the tracks into the woods.` |
| **Kreatív › karakter** (mint a Character.AI): beszélgetés egy kitalált szereplővel | Mara Vale kapitány, Thorn professzor, Nyx, vagy a saját karaktered |
| **emlékezet a történetekhez**: lényeg, szerzői megjegyzés, világkártyák, automatikus összefoglalás; minden átírható és törölhető | Kreatív › **Emlékezet** |
| **Szünet**: minden modell leáll, a videókártya és a memória felszabadul (pl. játékhoz) | „Pihenj!”, vagy a Szünet gomb |
| **készenlét**: a gép bekapcsolásakor elindul, de modellek nélkül vár; az első kérés felébreszti | `start_jarvis.bat --autostart on` |
| **telefon**: a saját appodból (Tailscale-en át) írhatsz, beszélhetsz, tárgyat adhatsz fel, jóváhagyhatsz | lásd [docs/telefon-api.md](docs/telefon-api.md) |
| PowerShell (alapból **kikapcsolva**) | minden futtatás előtt gépelt jóváhagyást kér |

## Telepítés (Windows)

Előfeltétel: fut az Ollama, és le van töltve a modell (`ollama pull qwen3.8:27b`).

1. **Python 3.12** a python.org-ról. Telepítéskor pipáld be: „Add python.exe to PATH”.
2. Csomagold ki a projektet, pl. ide: `C:\Projektek\jarvis`.
3. Nyiss PowerShellt abban a mappában:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-gpu.txt
pip install -r requirements-wakeword.txt
playwright install chromium
```

- A `requirements-gpu.txt` a GPU-s beszédfelismeréshez kell (cuBLAS + cuDNN, kb. 1 GB). Erősen ajánlott, mert magyarul a GPU-s „turbo” modell sokkal pontosabb és gyorsabb. CUDA Toolkitet nem kell külön telepíteni.
- A `requirements-wakeword.txt` a „Hey Jarvis” ébresztőszóhoz kell (openWakeWord). Külön fájlban van, mert a csomag 2024 óta nem frissült; ha nálad nem települne, a Jarvis nélküle is működik.
- A Jarvis böngészője alapból a gépen lévő **Edge**. A `playwright install chromium` (~150 MB) a tartalék, ha az Edge valamiért nem indulna.

Ha az `Activate.ps1` nem fut le („running scripts is disabled”), egyszer add ki ezt, majd próbáld újra:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

A Jarvis a modellt 16K kontextussal tölti be (`[llm] num_ctx`), mert egy weboldal-nézet 1–2 ezer token. Ehhez nem kell környezeti változót állítanod. Ha így a modell nem fér el teljesen a videókártyákon (mert pl. böngésző vagy játék is használja őket), a Jarvis kipróbálja kisebb kontextussal is (12K, majd 8K), de csak akkor marad annál, ha úgy teljesen a kártyákra kerül, vagy érdemben több jut rá: pár százalék sebességért nem éri meg, mert a kis kontextusban a modell egy hosszabb feladatnál (pl. egy hirdetés feltöltésénél) elveszíti a fonalat. A döntést megjegyzi, így legközelebb (és készenlétből felébredve) egyetlen betöltéssel megvan. Ha a modell egy része a processzorra kerül, induláskor szól, és kiírja, mennyi a szabad videómemória.

4. **Ellenőrzés:**

```powershell
python check_setup.py
```

Végigteszteli a Pythont, a csomagokat, az Ollamát (sebesség, eszközhívás, **képfelismerés**), a webes keresést, a saját böngészőt, a képernyőképet, az ébresztőszót, a beszédfelismerést, a hangot és a mikrofont. Ha valami piros, a sor megmondja, mi a teendő.

5. **Indítás:**

```powershell
python jarvis.py
```

vagy dupla katt a `start_jarvis.bat` fájlra. Amint a nyelvi modell betöltődött (10–20 mp), megnyílik a Jarvis ablaka; a konzolablakban addig is látszik, hol tart. Ha szeretnéd, hogy a gép bekapcsolásakor magától elinduljon (modellek nélkül, készenlétben), lásd: [Készenlét](#készenlét-indulás-a-gép-bekapcsolásakor).

Első indításkor letölti a beszédfelismerő modellt (GPU-n „turbo”, ~1,6 GB; CPU-n „small”, ~0,5 GB) és a magyar hangot (~60 MB). Ez egyszeri.

**Frissítés régebbi verzióról.** Csomagold ki az új verziót a régi mappájába (a `.venv` maradhat), és ha átírtál valamit a `config.toml`-ban, írd vissza. A bejelentkezéseid és az eladásra szánt tárgyaid a projektmappán kívül, a `%LOCALAPPDATA%\Jarvis` mappában vannak, így a frissítés nem nyúl hozzájuk. A régebbi verziók még a projektmappában tartották őket (`browser_profile`, `munka`); ezeket az első indításkor a Jarvis magától átteszi. Ha az új verziót másik mappába csomagolod ki, ezt a két mappát másold át kézzel a `%LOCALAPPDATA%\Jarvis` mappába.

## A felület

A Jarvis ablaka fekete, a közepén egy régi magyar csöves rádió „varázsszeme” fehéren világít, mint egy fekete-fehér tévé képcsöve: a mutatója mutatja, mit csinál éppen. A fehér máshol is azt jelzi, ahol tenni lehet valamit, vagy ahol rád vár valami (a fő gombok, egy kérdés a kártyán); az állapotot fényerő és jel mutatja, nem szín.

| A varázsszem | Jelentése |
|---|---|
| halvány, széles sötét cikk | Készen állok (vár rád, vagy a „Hey Jarvis”-ra) |
| a hangoddal együtt mozog | Hallgatlak; minél hangosabban beszélsz, annál keskenyebb a sötét cikk |
| lassan leng | Gondolkodom |
| gyorsabban leng | Dolgozom (keres, böngészik, fájlt olvas); alatta látszik, min |
| keskenyen, sűrűn leng | Írok (a Kreatív menüben: a történet folytatását vagy a karakter válaszát) |
| élénken villódzik | Beszélek |
| elhalványul, a sötét cikk a felét eltakarja | Szünetelek, vagy készenlétben várok (lásd Készenlét) |

| Mit csinálsz | Mi történik |
|---|---|
| „Hey Jarvis” | csengés, és hallgat; ha befejezted, maradj csendben egy pillanatig |
| **Hallgass meg** gomb | ugyanez, ébresztőszó nélkül |
| írsz, majd **Küldés** (vagy Enter) | gépelt kérdés |
| (válasz után) | még 4 másodpercig figyel: ha folytatod, nem kell újra megszólítanod |
| **Állj** gomb vagy **Esc** | azonnal megállítja a felvételt, a gondolkodást, a böngészést és a beszédet |
| **Új beszélgetés** | elfelejti az eddigieket (abban a menüben, amelyik elöl van) |
| **Szünet** | minden modell leáll, és a böngésző is bezárul: a videókártya és a memória szabad (lásd lent); a gomb **Folytatás** lesz |
| **Ébresztés** | készenlétben: betölti a modelleket (ha írsz, magától is felébred, lásd Készenlét) |
| **Telefon** | a telefonod párosítása és a párosított eszközök (lásd [docs/telefon-api.md](docs/telefon-api.md)) |
| „Viszlát, Jarvis” vagy az ablak bezárása | kilépés (a modell kiürül a VRAM-ból); készenléti indításnál az ablak bezárása nem léptet ki |

A naplóban Jarvis szavai talpas betűvel, a tieid talpatlannal jelennek meg, a lépései (keresés, kattintás) ezüstszürkével, pipával vagy ✕ jellel; a figyelmeztetés előtt felkiáltójel áll, a hiba fehér, ✕ jellel. A hirdetéseknél ami nem sikerült, azt fehér hullámos aláhúzás jelzi (mint a helyesírás-ellenőrzőben). Ha jóváhagyás kell (vásárlás, küldés, törlés), felugró ablak kérdez; az **Esc** itt a Mégse.

A konzolos mód továbbra is megvan:

```powershell
python jarvis.py --console   # konzolos mód (Enter = beszéd, q = kilépés, Ctrl+C = megállítás)
python jarvis.py --text      # csak gépelés, mikrofon nélkül (az eszközök kipróbálásához jó)
python jarvis.py --mute      # nem olvassa fel a válaszokat
python jarvis.py --no-wake   # ébresztőszó nélkül
python jarvis.py --no-window # az ablak nem nyílik meg magától; a címét kiírja a konzolba
python jarvis.py --standby   # készenlétben indul: ablak és betöltött modellek nélkül
python jarvis.py --autostart on   # a gép bekapcsolásakor készenlétben induljon (off: kikapcsolás)
python jarvis.py --quit      # leállítja a futó Jarvist (pl. a készenlétben lévőt)
```

A `start_jarvis.bat` ugyanezeket a kapcsolókat elfogadja (pl. `.\start_jarvis.bat --quit`), és a `.venv` Pythonját használja.

## Munka menü: Adás/Vétel

Az ablak tetején három menü van. A **Beszélgetés** ugyanaz, mint eddig: bármit kérdezhetsz, bármit kérhetsz. A **Kreatív** menüről lásd lent. A **Munka** menüben Jarvis külön beszélgetésben, a saját eszközeivel dolgozik; a kettő nem keveredik. Ha a Munka menü van elöl, a „Hey Jarvis” is oda megy. A Munka menün belül fülek vannak; most egy van, az **Adás/Vétel**, de később jöhetnek mellé más munkák is.

**Egy eladás:**

1. **Hova kerüljön?** Pipáld be: Vinted, Jófogás, Vatera, Hardverapró (több is lehet). A pipák alatt rögtön látod, mit kérnek az oldalak (lásd a táblázatot).
2. **Fotók.** Kattints a **+ Fotók** gombra, vagy húzd az ablakra a képeket.
3. **Egy mondat arról, mit adsz el**, pl. „Zara L-es fekete póló, egyszer hordott, 500 Ft”, majd **Mehet**.

Jarvis egyetlen modellhívással kiolvassa a mondatodból a hirdetés adatait (cím, leírás, kategória, márka, méret, állapot, ár). A fotókat nem nézi, mert az sok idő lenne, ezért amit nem írtál, azt nem is találja ki. Ha a kiválasztott oldalakhoz valami hiányzik, a hirdetés kártyáján (és hangosan is) visszakérdez, pl. „még kell: állapot; átvétel helye (Jófogás)”. A kártyán válaszolsz, vagy egyszerűen beírod (bemondod) a Munka menüben. Ha minden megvan, sorban feltölti minden kiválasztott oldalra.

| Mit kér | Vinted | Jófogás | Vatera | Hardverapró |
|---|---|---|---|---|
| mit adsz el, ár, állapot, legalább egy fotó | ✓ | ✓ | ✓ | ✓ |
| márka, méret, szín | ruhánál, cipőnél, kiegészítőnél | | | |
| átvétel helye (város) | | ✓ | | ✓ |
| szállítás (módok és díjak) | | | ✓ | ✓ |

A várost, a szállítást és a Vinted csomagméretét elég egyszer megadni az **Alapadatok és bejelentkezés** résznél; utána Jarvis onnan veszi, és nem kérdez rá. Ha egy hirdetésnél mást írsz (pl. „csak személyes átvétel Debrecenben”), az az erősebb.

**„Nézz utána, mennyiért adják máshol.”** Ha ezt is bepipálod, feltöltés előtt a kiválasztott oldalak keresőjében megnézi, mennyiért kínálnak hasonlót. Ha az árad jóval drágább (a hasonlók középértékénél 25%-kal többe kerül), megáll, és megkérdezi: „Maradjon a te árad, vagy legyen 4 500 Ft?”. Ez csak ajánlás: a **Marad** gombbal (vagy ha azt mondod, „maradjon”) a te áradon tölti fel; a **Legyen** gombbal, vagy ha egy összeget mondasz („legyen 4000”), azzal. Ha az árad rendben van, vagy olcsóbb, csak szól, és megy tovább.

**A feltöltés.** Jarvis a saját böngészőablakában, a szemed előtt tölti ki a hirdetésfeladást: fotók, cím, leírás, kategória, márka, méret, állapot, ár, helyszín, szállítás. A **közzététel** gomb előtt oldalanként egyszer rákérdez; a kérdés első sorában látod, melyik oldal, melyik tárgy és mennyiért. A végén szól, hol van fent, és hol nem sikerült. Ami nem sikerült (pl. be kellett volna jelentkezni), azt a kártyán ✕ jelzi; a hirdetés lapján olvasható, miért, és az **Újra** gombbal folytatja. Ha elakadt, az oldal sorában egy **Hibacsomag** gomb is van (lásd lent). Ha a közzétételt jóváhagytad, de a modell a végén nem jelölte meg a hirdetést (pl. félbemaradt a válasza), Jarvis még egyszer megnézeti vele az oldalt, és ha fent van, megjelöli; újra feltölteni ilyenkor nem kezdi, nehogy kétszer kerüljön fel.

**Figyelem:** a Vinted és a legtöbb hirdetési oldal szabályai tiltják a botokat; ha kiszúrják, korlátozhatják a fiókodat. Ezt Jarvis oldalanként egyszer meg is kérdezi, mielőtt először feltöltene oda.

**Emberi tempó.** A hirdetési oldalak robotszűrői a gépies viselkedést figyelik, ezért a Jarvis böngészője úgy dolgozik, mint egy ember: kattintás előtt az egér enyhe ívben odamegy a gombhoz (nem pont a közepére kattint, és a gombot egy pillanatig nyomva tartja), a rövid szövegeket (cím, ár, márka) billentyűnként gépeli, változó tempóban, a hosszút (leírás) beilleszti, ahogy te is a jegyzeteidből, és ha egy mező lejjebb van, az egérgörgővel teker oda (az oldal nem „ugrik”). A begyakorolt lépéseknél, ahol nincs modell, ami magától lassítana, gondolkodási és olvasási szüneteket is tart: egy új oldal után körülnéz, mezők és gombok előtt gondolkodik egy kicsit, a fotókat „kiválasztja”, a közzététel előtt pedig átnézi az űrlapot. Így egy begyakorolt feltöltés kb. egy perc, nem pár másodperc (a modellel ennél jóval több). Ugyanazon az oldalon két közzététel (vagy levétel) között legalább 2 percet vár (véletlenszerűen ±30%); ezt a kártyán látod („Vinted: várok még 1:45”), és az **Állj** gomb megszakítja. Ez csökkenti a lebukás esélyét, de nem szünteti meg: a böngésző ujjlenyomatát a Jarvis nem hamisítja, a robot-ellenőrzést (captcha) nem oldja meg helyetted, és az oldal szabályain sem változtat. Beállítás: `[browser] human_pace`, `pace` (a szünetek szorzója), `[work] publish_gap_minutes`.

**Hibacsomag.** Ha egy feltöltés, levétel vagy visszajátszás elakad, a Jarvis egy mappába gyűjt mindent, ami a javításhoz kell: képernyőkép az oldalról abban a pillanatban (`kepernyo.jpg`), az oldalnézet, ahogy a modell látta (`oldal.txt`), a böngészőben tett lépések (`lepesek.txt`), a modell kérései és az eszközök válaszai (`beszelgetes.txt`), és egy összefoglaló (`info.json`: oldal, tárgy, feladat, a hiba oka, webcím; ha a képernyőkép nem készült el, az is, hogy miért). A hirdetés lapján az oldal sorában a **Hibacsomag** gomb megnyitja a mappát az Intézőben. A csomagok a `%LOCALAPPDATA%\Jarvis\munka\hibak` mappában vannak, a legutóbbi 30 marad meg. Ha a közzétételt te nem engedélyezted, az nem hiba: akkor nem készül csomag. A kép és az oldalnézet személyes adatot is tartalmazhat (pl. a felhasználóneved), ezért ha elküldöd valakinek, előtte nézd át.

**A hirdetés lapja** (kattints a kártya képére vagy címére): itt látod és átírhatod, amit Jarvis kiolvasott. Amit átírsz, azt később nem írja felül. Itt pipálhatsz be új oldalt, jelölheted kézzel, hogy fent van vagy már levetted, és itt van az **Eladva** gomb is.

**Eladva és levétel.** Az **Eladva** gombra Jarvis megkérdezi, hol kelt el, és (ha bent hagyod a pipát) a többi oldalról leveszi a hirdetést; ahol elkelt, ott nem nyúl hozzá. Egy oldalról külön is leveheted (**Levétel** gomb az oldal sorában), és szóban is kérheted: „a pulóver elkelt a Vinteden, vedd le a többiről”. A levételt a gombbal (vagy a kérdésére adott igennel) jóváhagyod; utána az adott oldalon a törlés és a megerősítés gombjaira már nem kérdez rá újra, fizetős lehetőségre (kiemelés, előresorolás) pedig soha nem kattint. A sikeres levétel után a sorban „Levéve” áll; ha nem sikerült, a hirdetés fent marad, és a sorban olvasható, miért.

**Begyakorolt lépések.** Amikor a modell egy oldalon sikeresen feltölt (vagy levesz) egy hirdetést, Jarvis megjegyzi a lépéseit, és legközelebb modell nélkül, emberi tempóban játssza vissza (lásd lent): ugyanazokat a mezőket és gombokat keresi meg, és a mostani tárgy adataival tölti ki. A választásoknál (márka, méret, állapot, szín) megjegyzi, melyik értékhez mire kellett kattintani; ha a felirat maga az érték (pl. „L” a méretnél), bármelyik értékre működik. Feltöltésnél a recept tárgyfajtánként és kategóriánként külön van, mert a kategória kiválasztása és az űrlap is ettől függ: egy új kategóriát a modell tanul meg egyszer. A közzététel előtt ugyanúgy jóváhagyást kér, és a kérdésben azt is látod, mit választott ki („Kategória: …; Állapot: …”). Ha a közzététel elsőre nem ment át (pl. hiányzott egy kötelező mező, és utána pótolni kellett), azt a lépéssort nem jegyzi meg, mert a visszajátszás a félkész űrlapnál kérne jóváhagyást. Ha valami nem stimmel (az oldal megváltozott, vagy olyan érték jön, amit még nem tanult meg), a közzététel **előtt** megáll (és hibacsomagot készít arról, hol akadt el), majd a modell csinálja végig, közben újratanul; két egymás utáni kudarc után elfelejti a régi receptet. Közzététel után soha nem kezdi újra (nehogy kétszer kerüljön fel), legfeljebb megnézeti a modellel, sikerült-e. A receptek a `%LOCALAPPDATA%\Jarvis\munka\folyamatok` mappában vannak; ha egyet törölsz, a következő alkalommal újratanulja. Kikapcsolás: `[work] replay = false`.

A Munka menüben szóban vagy írásban is kérhetsz: „A farmer legyen ötszáz forinttal olcsóbb”, „Melyik tárgy nincs még fent sehol?”, „Mennyit ér a blézer?”, „A pulóver elkelt”.

A tárgyak a `%LOCALAPPDATA%\Jarvis\munka\eladas` mappában vannak, tárgyanként egy mappa (`item.json`, a fotók és az előnézetek). A törölt tárgy az `eladas\_torolt` mappába kerül, onnan visszahozható. Az iPhone-os HEIC fotókhoz: `pip install pillow-heif` (vagy a telefonon: Beállítások › Kamera › Formátumok › Legkompatibilisebb). A Munka menü saját utasításai a `work_prompt.txt`-ben vannak.

## Kreatív menü: kalandok és karakterek

A **Kreatív** menüben a Jarvis mesélő: közösen írtok egy történetet, vagy beszélgetsz egy kitalált szereplővel. A történetek alapból **angolul** készülnek, mert a modell angolul ír a legjobban (magyarul néha furcsa szavakat talál ki). Új történetnél, és később az Emlékezet › Beállításokban is átállíthatod magyarra; ilyenkor a mesélő utasításai is magyarok, és külön kérés szól a természetes magyarságról. Az alapnyelv: `[creative] language`.

**Kaland** (mint az AI Dungeon). A mesélő második személyben, jelen időben írja, mi történik veled, és a világ visszahat: nem minden sikerül, a szereplőknek saját céljuk van. Te háromféleképpen léphetsz:

| Gomb | Mit írsz | Ami a történetbe kerül |
|---|---|---|
| **Tett** | `open the door` (vagy magyarul: `kinyitom az ajtót`) | `> You open the door.` (angolul az „I”-ből is „You” lesz) |
| **Mondás** | `who's there?` | `> You say "Who's there?"` |
| **Történet** | bármilyen szöveg | szó szerint, mintha te írnád a történetet; a mesélő onnan folytatja |

Szögletes zárójelben a mesélőnek szóló utasítást is írhatsz, pl. `[the next scene should be a storm]`: ezt követi, de a történetben nem említi.

**Karakter** (mint a Character.AI). Egy szereplővel beszélgetsz, aki a saját hangján, a saját személyiségével válaszol; a cselekvést *csillagok közé* írja (te is írhatod így). Kész szereplők: **Mara Vale kapitány** (csípős nyelvű léghajós), **Thorn professzor** (egy régi könyvtár szelleme), **Nyx** (rókaszellem az utolsó vonat után). Sajátot is írhatsz: név, jellem, első üzenet, példa párbeszéd.

**Új történet.** Válassz egy kiindulópontot (Fantasy, Krimi, Sci-fi, Világvége, Horror, Cyberpunk, vagy a karakterek), írd le pár szóban az ötleted (**Ötletből**: a Jarvis megtervezi a világot, a szereplőket és a kezdést, a kezdést élőben írja), vagy írd meg magad (**Saját**). Minden szereplő, hely és kezdés saját kitalálás, angolul és magyarul is.

| Gomb | Mit csinál |
|---|---|
| **Küldés** (Enter) | a köröd; üresen ugyanaz, mint a Folytasd. Új sor: Shift+Enter |
| **Folytasd** | a mesélő (vagy a karakter) magától folytatja |
| **Másik változat** | az utolsó választ újraírja; a változatok között a ‹ › nyilakkal lapozhatsz |
| **Visszavonás**, **Mégis** | az utolsó rész ki, illetve vissza |
| **Szerkesztés**, **Törlés** (egy rész fölé állva) | bármelyik részt átírhatod vagy törölheted, a mesélőét is |
| **Állj** (írás közben a Küldés helyén), vagy **Esc** | megállítja; amit addig írt, megmarad, a félbemaradt mondat nélkül |

**Emlékezet.** A modell egyszerre csak a kontextusába férő szöveget látja (16K tokenbe angolul kb. 12 ezer szó fér, ebből a szó szerinti előzmény kb. 5 ezer). Ezért az **Emlékezet** gombbal nyíló panelen ezek vannak:

- **Lényeg**: amit a mesélő mindig lát (a világ, a helyzet, a fontos tények). Ide írd, amit semmiképp nem szabad elfelejtenie.
- **Szerzői megjegyzés**: stílus és hangulat egy-két sorban (pl. „Sötét, feszült hangulat, rövid mondatok.”). Közvetlenül a válasza előtt látja, ezért erősen hat.
- **Világkártyák**: szereplők, helyek, tárgyak leírása kulcsszavakkal. Egy kártya akkor kerül a mesélő elé, ha a kulcsszava előfordul a történet utolsó részeiben (a magyar ragok nem zavarják: a „hamvasd” kulcsszó a „Hamvasdra” szóra is jelez); a **Mindig** jelölésűt mindig látja.
- **Emlékek**: ha a szó szerinti előzmény a kontextus kb. 45%-át elérné, a Jarvis a régebbi részt összefoglalja, és onnantól az összefoglalót látja helyette (a történetben szaggatott vonal jelzi: *eddig emlékként*). Ha az emlékek is túl sokan lesznek, a régieket egy „eddigi történet” összefoglalóba sűríti. Így a történet akármilyen hosszú lehet. Az emlékek átírhatók és **törölhetők**: amit törölsz, azt a modell elfelejti (a történet szövege megmarad). Az **Összefoglalás most** gomb azonnal összefoglal, az **Új emlék** gombbal te is írhatsz egyet (pl. „Mara megígérte, hogy elvisz a fővárosba.”).
- **Beállítások**: cím, nyelv, **Kreativitás** (alacsonyabb: kiszámíthatóbb; magasabb: meglepőbb, de többször téved; magyarul alapból óvatosabb), **Válaszhossz**, és **Egyedi utasítások**, amiket a mesélő mindig betart.

A panel tetején egy sáv mutatja, mennyit foglal a kontextusból az utasítás és a háttér, az emlékek és a szó szerinti szöveg.

**Sebesség.** A Kreatív menü ugyanazt a modellt használja, mint a többi menü, és a modell egyszerre egy dolgot csinál: ha épp a Beszélgetésben dolgozik, a történet „Sorra vár…”. Egy közepes hosszúságú válasz nálad kb. 20–40 másodperc, a szöveg közben élőben jelenik meg. A prompt eleje (utasítások, lényeg, emlékek, korábbi szöveg) körről körre ugyanaz marad, így az Ollama csak az új részt dolgozza fel; egy összefoglalás után (nagyjából 10–20 körönként) egyszer az egészet újra kell, az pár másodperccel lassabb.

A történetek a `%LOCALAPPDATA%\Jarvis\kreativ` mappában vannak (történetenként egy JSON-fájl), a frissítés nem nyúl hozzájuk. A **Letöltés (.md)** gomb a történet szövegét Markdown-fájlként menti. A Kreatív menü nem olvas fel, és a mikrofont sem használja.

## Bejelentkezés: hogy ne kelljen mindig belépni

Jarvis böngészője külön, saját profillal fut (a te Edge-edet és Brave-edet nem zavarja), és ez a profil megőrzi a sütiket, vagyis a bejelentkezéseket is. Így elég oldalanként **egyszer** belépni:

1. Adás/Vétel › **Alapadatok és bejelentkezés** › kattints az oldal nevére (pl. **Vinted**). Jarvis megnyitja az oldalt a saját böngészőablakában.
2. Jelentkezz be abban az ablakban, és pipáld be, hogy **maradj bejelentkezve** („Emlékezz rám”, „Maradjak bejelentkezve”).
3. Kész: a Jarvis újraindítása, sőt egy új verzió után is bejelentkezve marad.

A profil a projektmappán kívül van (`%LOCALAPPDATA%\Jarvis\browser_profile`), ezért a frissítés nem törli. Ha az ablak felajánlja, a jelszót is elmentheted a böngészőben; Jarvis maga jelszót soha nem gépel be. Egy oldal néha magától kiléptet (pár hét után, vagy ha gyanúsnak talál valamit, és ellenőrzést kér). Ilyenkor a feltöltés megáll, a kártyán ✕ jelzi, hogy be kell jelentkezni; lépj be ugyanígy, és nyomd meg az **Újra** gombot.

Miért nem a saját böngésződ profilját használja? Mert azt a böngésződ zárolja, amíg fut, és a Chrome meg az Edge újabb verziói biztonsági okból nem is engedik távvezérelni a felhasználó alapértelmezett profilját. A külön profil a megbízható út.

## Szünet: ha kell a videókártya

A **Szünet** gomb (vagy ha azt mondod: „pihenj”, „szünet”) mindent leállít, amit a Jarvis használ: a nyelvi modellt (az Ollamában minden betöltött modellt), a beszédfelismerést, a felolvasót, az ébresztőszó-figyelést, és bezárja a saját böngészőjét is. A videókártya, a memória és a processzor felszabadul, pl. játékhoz (akár a másik ülésen). A naplóban látod, mennyi videómemória szabadult fel. Szünet alatt háttérmunka sincs, és a kérésekre csak annyit mond, hogy szünetel.

Az ablak és a telefonos kapcsolat megmarad: a **Folytatás** gombbal (vagy a telefonról) 10–20 másodperc alatt minden újra betöltődik. Ha szünet alatt írsz valamit, megkérdezi, folytassa-e. A teljes kilépés továbbra is az ablak bezárása vagy a „Viszlát, Jarvis”.

## Készenlét: indulás a gép bekapcsolásakor

Ha szeretnéd, hogy a Jarvis a gép bekapcsolásakor elinduljon, de ne foglalja a videókártyát, amíg nem kell, egyszer add ki a projektmappában:

```powershell
.\start_jarvis.bat --autostart on
```

Ez egy parancsikont tesz a Windows Indítópult mappájába (rendszergazdai jog nem kell). Bejelentkezéskor a Jarvis **készenlétben** indul: konzol- és Jarvis-ablak nélkül, betöltött modellek nélkül, a videókártya szabad. Közben fut a telefonos kapcsolat és a modell nélküli háttérmunka (figyelők, régóta fent lévő hirdetések).

- **Az ablak megnyitása:** dupla katt a `start_jarvis.bat`-ra. Mivel a Jarvis már fut, csak az ablakát nyitja meg, rögtön.
- **Felébresztés:** ha írsz (vagy a telefonról kérsz valamit), betölti a modelleket (10–20 mp), és válaszol. Az **Ébresztés** gomb ugyanezt csinálja kérdés nélkül. A „Hey Jarvis” készenlétben nem működik, mert a mikrofont sem figyeli; felébredés után igen.
- **Vissza készenlétbe:** ha 15 percig (`[standby] idle_minutes`) nem használod, magától leállítja a modelleket, és újra készenlétben vár.
- **Szünet:** ébren a **Szünet** gomb, készenlétben a „pihenj” beírása (vagy a telefon Szünet gombja) igazi szünetre vált: ilyenkor a kérések sem ébresztik fel (pl. játék közben), háttérmunka sincs, csak a **Folytatás** gomb hozza vissza.
- **Az ablak bezárása** ilyenkor nem állítja le a Jarvist (a telefon és a következő megnyitás is elérje). Teljes leállítás: `.\start_jarvis.bat --quit`; ébren a „Viszlát, Jarvis” is működik.
- **Kikapcsolás:** `.\start_jarvis.bat --autostart off`.

Egyszerre csak egy Jarvis fut: ha már fut (akár készenlétben), a második indítás csak az ablakát nyitja meg. Ha azt írja, hogy egy másik Jarvis fut, de nem válaszol, állítsd le a Feladatkezelőben (python vagy pythonw), vagy indítsd újra a gépet. Készenléti indításnál nincs konzolablak, ezért a Jarvis a `%LOCALAPPDATA%\Jarvis\naplo.txt` fájlba ír; ha valami nem indul el, ott látod, miért.

Ha nem kapcsolod be az automatikus indítást, minden a régi: a `start_jarvis.bat` betölti a modelleket, megnyitja az ablakot, és az ablak bezárásával a Jarvis is leáll.

## Háttérmunka és figyelők

A Jarvis néhány dolgot magától, a háttérben is elvégez, de csak akkor, amikor ráér: nincs Szünet, épp nem dolgozik másvalamin, és az utolsó beszélgetésetek óta eltelt pár perc (`quiet_minutes`, alapból 5). Ha nem ér rá, a munka nem marad ki, csak eltolódik; ha közben megszólítod, a háttérmunka a következő lépésnél abbahagyja, és később folytatja. A „Hey Jarvis” háttérmunka közben is működik.

- **Figyelők:** „szólj, ha lesz új Python-állás Debrecenben”, „figyeld a Hardverapró-n az RTX 3080-as hirdetéseket”. Jarvis óránként (`watch_minutes`) újra lefuttatja a keresést, és ha új találat van, szól (és felolvassa). Az első futáskor csak megjegyzi a meglévőket. Könnyű munka: egy webes keresés figyelőnként, böngésző és nyelvi modell nélkül. Kérdezheted: „milyen figyelőim vannak?”, „töröld a Python-figyelőt”, „nézd meg most a figyelőimet”.
- **Régóta fent lévő hirdetések:** naponta egyszer szól, ha valami legalább `stale_days` (14) napja fent van, de még nem kelt el (utána egy hétig nem szól újra ugyanarról).

Az értesítések a naplóba kerülnek, és a telefon is lekérdezheti őket. Ha valamikor nem szeretnél háttérmunkát (pl. pár óráig), a telefonról felfüggesztheted, vagy kapcsold ki: `[scheduler] enabled = false`.

## Telefon

A Jarvisnak van egy kis JSON-felülete (alapból a `127.0.0.1:8765`-ön), amit a saját appod a Tailscale-en át ér el: `tailscale serve --bg 8765`, és az app a gép `https://<gép>.<tailnet>.ts.net` címét használja. Párosítás a Jarvis ablakában (**Telefon** › **Új párosító kód**), eszközönkénti kulccsal, ami ugyanott vissza is vonható. A telefonról írhatsz és beszélhetsz (hangfelvételt küldesz, a Jarvis a gépen ismeri fel), követheted az élő naplót, jóváhagyhatod a közzétételt, fotót és tárgyat adhatsz fel, jelölheted, hogy elkelt (és leveheted a többi oldalról), szüneteltetheted és folytathatod a Jarvist, kezelheted a figyelőket és az értesítéseket.

A végpontok leírása: [docs/telefon-api.md](docs/telefon-api.md); kész Angular-szolgáltatás: [docs/angular/jarvis-api.service.ts](docs/angular/jarvis-api.service.ts).

## Ébresztőszó

A „Hey Jarvis” felismerése helyben, a processzoron fut (openWakeWord), a hang nem megy ki a gépről. Amíg vár, csak ezt az egy kifejezést figyeli; amikor meghallja, csengetéssel jelez, átadja a mikrofont a felvételnek, és meghallgatja a kérdésed. Amíg Jarvis dolgozik vagy beszél, nem figyel, így a saját hangjától nem ébred fel.

- Ha túl könnyen ébred (pl. a tévé hangjától), növeld a `[wake_word] threshold` értékét (pl. 0,6–0,7).
- Ha nehezen ébred, csökkentsd (pl. 0,35–0,4), és mondd tisztán, angolosan: „hey dzsárvisz”.

## Szemek és kezek: hogyan látja a webet és a képernyőt

**Webes keresés és olvasás.** A `web_search` DuckDuckGo-n keres (kulcs nem kell), egy adott oldalra (`site`, pl. profession.hu) és csak friss találatokra (`recency`: nap, hét, hónap, év) is. A `read_webpage` letölti és kinyeri egy oldal főszövegét. A `web_research` egy lépésben a legjobb 3 (legfeljebb 5) találatot egyszerre tölti le, és oldalanként csak a kérdéshez illő bekezdéseket adja át: ez egy modellkör a három helyett, ezért több forrásnál jóval gyorsabb. Mindegyik gyors, a böngésző meg sem nyílik.

**Ha egy oldal nem enged be.** Sok webshop és árösszehasonlító (pl. Alza, Árukereső) a sima letöltést 403-as hibával elutasítja, mert robotnak nézi. Ilyenkor a Jarvis a saját böngészőjével (a valódi Edge-dzsel) olvassa el az oldalt, egy külön lapon, amit utána bezár; az aktív lapot, amin épp dolgozik, nem zavarja. Az ilyen oldalakat hat órára megjegyzi, és legközelebb egyből a böngészővel nyitja, a `web_research` pedig a sor végére teszi őket, és a kieső oldal helyett a következő találatot olvassa el. Ha az oldal a böngészőt is csak robot-ellenőrzés (captcha) után engedné be, azt nem kerüli meg: szól, és másik forrást választ. Tanúsítványhibánál (SSLError) és a csak JavaScripttel betöltődő oldalaknál is a böngészővel próbálkozik. Egy kérésen belül ugyanazt a keresést vagy oldalt nem kéri le kétszer, a mai dátumot is tudja (nem keres rá a tavalyi évre), és ha fogynak a lépései, szól neki a rendszer, hogy válaszoljon abból, amit talált; ha mégis elfogynak, a meglévőből válaszol, és megmondja, mit nem sikerült ellenőrizni.

**Saját böngésző.** Ha egy oldalon kattintani vagy kitölteni kell, Jarvis megnyitja a saját böngészőjét (látható ablak, külön profil, a te Brave-edet nem zavarja). Minden lépés után „oldalnézetet” kap: a képernyőn látható szöveget és a kezelhető elemek számozott listáját, pl.:

```
Oldal: Hasznaltauto.hu – Keresés
Látható szöveg: Skoda Octavia 1.6 CR TDI Style, 2018, 4 290 000 Ft …
Kezelhető elemek (a számukkal hivatkozz rájuk):
[1] gomb "Elfogadom"
[2] keresőmező "Márka, modell"
[3] lenyíló lista "Évjárat" (kiválasztva: "Mindegy"; lehetőségek: …)
[4] link "Skoda Octavia 1.6 CR TDI Style" -> /szemelyauto/skoda/…
```

A modell a számokra hivatkozva kattint (`browser_click`), gépel (`browser_type`), egy űrlap több mezőjét egy lépésben kitölti (`browser_fill`), görget, visszalép, és el is olvassa az oldalt (`browser_read`). Ez a szöveges nézet pontosabb és gyorsabb, mintha képről kellene kitalálnia, hová kattintson. A régebbi oldalnézeteket a Jarvis kitömöríti, hogy ne teljen be a kontextus.

Webshopokban (Vinted, Jófogás, eMAG) fontos részletek:
- Az oldalnézetben a **fő tartalom** (termékek, szűrők, kereső) előrébb kerül, a fejléc és lábléc linkjei hátrébb, és legfeljebb a lista harmadát foglalhatják el.
- A Jarvis **kivárja a betöltődést**: amíg az oldal adatot tölt, vagy „Betöltés…” felirat látszik rajta, nem készít oldalnézetet.
- A rendszerprompt szerint a modell az oldal **saját keresőjét és szűrőit** használja, nem találgat webcímet. Ha három-négy próbálkozás után sem jut előre, megáll, és elmondja, meddig jutott.
- Ha az oldal **robot-ellenőrzést** (captcha) kér, szól, hogy oldd meg a böngészőablakban. A valódi Edge-ben ez ritkábban fordul elő, mint a Playwright Chromiumában.

A profil megőrzi a sütiket: ha egyszer belépsz valahova a Jarvis böngészőjében, legközelebb is bejelentkezve látja (lásd: Bejelentkezés).

**Látás.** A Qwen3.8 képet is ért. A `browser_look` lefényképezi a böngésző oldalát (grafikonok, képek, elrendezés), a `look_at_screen` a monitorodat („mi ez a hibaüzenet?”). A képet egy külön, egyszeri kérdésben kapja meg a modell, így nem foglalja a beszélgetés kontextusát.

## Sebesség: hol megy el az idő

Egy hangos kérés útja, és mit csinál a Jarvis, hogy ne kelljen várni:

| Szakasz | Idő (becslés) | Mit csinál a Jarvis |
|---|---|---|
| csend a mondat végén | 0,8 mp | `silence_seconds`; kisebbre is veheted, de akkor mondat közben is levághat |
| beszédfelismerés | ~0,3–0,5 mp GPU-n | a „turbo” modell a 5060 Ti-n; CPU-n 1–2 mp |
| a modell első szava | ~0,5–1 mp | a beszélgetés eleje az Ollama gyorsítótárában marad, csak az új rész kell |
| válasz | 31 token/mp | **folyamatos felolvasás**: az első mondat már szól, amíg a modell a többit írja |
| felolvasás | ~0,1 mp/mondat | a következő mondat hangja már készül, amíg az előző szól |

A folyamatos felolvasás a hosszú válaszoknál számít igazán. Szimulációban (31 token/mp-es modellel, Piper sebességű szintézissel) egy 68 szavas válasz első hangja 4,4 mp helyett 1,1 mp után szólalt meg. Egy rövid, egymondatos válasznál a különbség kicsi (1,3 → 1,1 mp).

További gyorsítások, amik már be vannak építve:

- **Előmelegítés.** Induláskor és Szünet után a Jarvis egy külön kéréssel előre feldolgoztatja a Beszélgetés elejét (rendszerprompt és eszközök), így az első kérdésnél már csak magát a kérdést kell. Kikapcsolás: `[llm] warm_up = false`.
- **Begyakorolt lépések.** Egy már megtanult feltöltés vagy levétel modell nélkül fut (lásd a Munka menünél).
- **Több oldal egy lépésben** (`web_research`): oldalanként egy modellkör helyett egy.

- **„Egy pillanat.”** Ha lassú lépés jön (keresés, böngészés, fájlkeresés), Jarvis azonnal mond valamit, így nem érzed, hogy lefagyott.
- **Gyorsítótár-barát előzmények.** Az Ollama megjegyzi az előző kérés elejét, és csak az új részt számolja. A régi oldalnézeteket a Jarvis csak akkor tömöríti, ha a kontextus 60%-a betelt, és akkor is egyszerre mindet; az előzmények elejét pedig ritkán, egyszerre a felére vágja. Ha minden lépésnél módosítaná, az Ollamának minden alkalommal újra kellene számolnia a későbbi szöveget.
- **Folytatólagos beszélgetés.** Válasz után pár másodpercig Enter nélkül is figyel.

Amit te tudsz még tenni:

- Ellenőrizd, hogy a beszédfelismerés GPU-n fut-e (a Jarvis induláskor kiírja: `turbo (GPU)`). Ha CPU-n, ez a legnagyobb nyereség.
- `ollama ps` → `100% GPU`. Ha a modell egy része a processzorra kerül, a válasz a felére-harmadára lassul. Indítás előtt zárd be a videókártyát használó programokat (böngészők, ChatGPT-alkalmazás, játék a másik ülésen).
- `think = false` maradjon. A gondolkodó mód válaszonként több száz rejtett tokent ír.
- **Kevesebb VRAM a kontextusnak:** az Ollamában a `OLLAMA_KV_CACHE_TYPE=q8_0` kb. a felére veszi a kontextus memóriáját, alig érezhető minőségvesztéssel (ehhez flash attention kell, ami az újabb Ollamában a Qwen-modelleknél ezeken a kártyákon magától bekapcsol). Windows-on: `setx OLLAMA_KV_CACHE_TYPE q8_0`, majd lépj ki az Ollamából a tálcán, és indítsd újra. A felszabaduló helyen elfér a 16K kontextus a böngésző mellett is, vagy beállíthatod az `OLLAMA_NUM_PARALLEL=2`-t: így a Beszélgetés és a Munka menü elejét is külön megjegyzi az Ollama, és menüváltáskor nem kell újra feldolgoznia (ennek ára, hogy a kontextus memóriája kétszer kell).
- A modellt ne cseréld gyorsabbra csak a sebesség miatt. A Qwen3.8-ból helyben csak a 27B létezik; a gyorsabb Qwen3.6-35B-A3B (MoE) Q4-ben kb. 22 GB, a beszédfelismerővel együtt nem fér el a 24 GB-ban, és gyengébb is.

## VRAM-beosztás

A Jarvis **előbb a nyelvi modellt** tölti be, csak utána a beszédfelismerőt, és a grafikus ablakot is csak ezután nyitja meg (a böngészőablak is foglal videómemóriát). Az Ollama ugyanis a betöltés pillanatában szabad memóriához igazítja, mennyi kerül a kártyákra, és amit nem tud oda tenni, azt a processzoron futtatja.

Ha a modell 16K kontextussal nem fér el teljesen, a Jarvis kisebb kontextussal (12K, majd 8K, de `min_ctx` alá nem) is kipróbálja. Annál a kisebbnél marad, amelyikkel a modell teljesen a kártyákra kerül, vagy legalább 5 százalékponttal több jut rá; különben visszaáll a 16K-ra, mert a kisebb kontextus alig gyorsítana, a modell viszont kevesebbet látna a feladatból. Ha a második próbából látszik, hogy a még kisebb sem segítene, azt be sem tölti (egy betöltés 10–20 másodperc). A döntést az adatmappában megjegyzi (`modell_illesztes.json`), így legközelebb egyből jó kontextussal tölt be; egy hét után, vagy ha frissül az Ollama, újra kipróbálja. Azt nem erőlteti, hogy minden réteg a kártyákra kerüljön: ha a memória tényleg nem elég, Windows-on a kártya a rendszermemóriába lóg át, az pedig még lassabb.

Az újabb Ollama (0.40-től) a modellt egy beépített llama-serverrel tölti be, és az kártyánként tartalékot hagy (alapból 1 GB-ot, plusz a képfelismerésnek). Ezért nálad a 27B-s modell 2–4 rétege akkor is a processzorra kerülhet, ha a kártyákon látszólag van még hely; a Jarvis ezt induláskor jelzi is. Így is működik, csak lassabb. Ha gyorsítanál rajta, lásd a Hibaelhárításban az `LLAMA_ARG_FIT_TARGET` sort.

- A Qwen3.8-27B (Q4) kb. 17–17,5 GB a két kártyán (8–16K kontextussal).
- A beszédfelismerő („turbo”, int8) kb. 1–1,5 GB, az **RTX 5060 Ti**-n fut (`gpu_device_index = 1`).
- A Jarvis böngészője pár száz MB-ot használ a monitort hajtó kártyán.
- Kilépéskor a Jarvis kiüríti a nyelvi modellt és bezárja a böngészőjét (`unload_on_exit = true`).

## Beállítások (`config.toml`)

**Beszéd és hang**
- `[stt] device`: `"auto"` (GPU, ha lehet), `"cuda"` vagy `"cpu"`.
- `[stt] gpu_device_index`: az `nvidia-smi` sorszáma. Nálad 0 = RTX 3060 Ti, 1 = RTX 5060 Ti.
- `[stt] initial_prompt`: gyakori szavak, nevek, így jobban felismeri őket.
- `[audio] input_device`: a mikrofon sorszáma vagy nevének része (a `check_setup.py` kilistázza). A multiseat miatt érdemes konkrétan megadni.
- `[audio] min_threshold`: ha nem veszi észre, hogy beszélsz, csökkentsd (pl. `0.005`).
- `[tts] voice`: `hu_HU-imre-medium` (férfi), `hu_HU-anna-medium` vagy `hu_HU-berta-medium` (női).

**Felület és ébresztőszó**
- `[gui] window`: alkalmazásablakban nyíljon meg (Edge `--app` mód, címsor nélkül). `exit_on_close`: az ablak bezárásával a Jarvis is leáll (készenléti indításnál nem).
- `[standby] idle_minutes`: a készenlétben indított Jarvis ennyi perc tétlenség után visszaáll készenlétbe (15; 0 = soha).
- `[wake_word] enabled`, `threshold`, `chime`: az ébresztőszó be/ki, érzékenysége és a csengés.

**Web, böngésző, képernyő**
- `[llm] num_ctx`: a kontextus mérete (16384). Ha induláskor azt írja, hogy kisebbel fér el, írd be ide azt az értéket, így nem kell kétszer betöltenie.
- `[llm] min_ctx`: ennél kisebbre nem csökkenti a kontextust, hogy elférjen (8192). `0` = ne csökkentse.
- `[web] allow_local`: alapból a Jarvis nem nyit meg helyi címet (localhost, router, 192.168.x.x). Fejlesztéshez (pl. „nézd meg, mi a hiba a localhost:4200-on”) kapcsold be.
- `[browser] channel`: alapból `"msedge"`, a gépen lévő Edge. Üresen hagyva a Playwright saját Chromiuma.
- `[browser] executable_path`: más Chromium-alapú böngésző, pl. Brave. Külön profillal fut, a saját Brave-edet nem zavarja:
  ```toml
  executable_path = 'C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe'
  ```
- `[browser] headless = true`: láthatatlanul böngészik (alapból látod, mit csinál).
- `[browser] human_pace`: emberi tempó a böngészőben (egérút, billentyűnkénti gépelés, szünetek; alapból be). `pace`: a szünetek szorzója (1 = alap, 1,5 = lassabb, 0 = szünetek nélkül).
- `[screen] monitor`: melyik monitort nézze (1 = fő, 2 = második, 0 = mind). Multiseat esetén a `check_setup.py` kiírja a monitorokat.

**Kreatív menü**
- `[creative] language`: az új történetek alapnyelve, `"en"` (angol, alapból) vagy `"hu"`. Történetenként a felületen is átállítható.
- `[creative] enabled = false`: a Kreatív menü kikapcsolása.

**Munka menü (Adás/Vétel)**
- `[work] dir`: ide kerülnek a tárgyak. Üresen a `%LOCALAPPDATA%\Jarvis\munka`; ha OneDrive-os mappát adsz meg, a fotók a telefonodon is megvannak.
- `[work] platforms`: mely oldalak jelenjenek meg (`vinted`, `jofogas`, `vatera`, `hardverapro`). `research_sites`: hol nézhet árat (a kiválasztottak közül).
- `[work] max_tool_rounds`: egy feltöltés legfeljebb ennyi lépés (40). `max_photos`: egy tárgyhoz legfeljebb ennyi fotó (20).
- `[work] enabled = false`: a Munka menü kikapcsolása.
- `[work] replay`: a begyakorolt feltöltés és levétel visszajátszása (alapból be).
- `[work] publish_gap_minutes`: ugyanazon az oldalon két közzététel (vagy levétel) között legalább ennyi perc teljen el (2, véletlenszerűen ±30%; 0 = nincs várakozás).

**Háttérmunka és telefon**
- `[scheduler] enabled`: háttérmunka be/ki. `quiet_minutes`: ennyi ideig nem indul háttérmunka a legutóbbi beszélgetés után (5). `active_hours`: pl. `"08:00-20:00"`, üresen bármikor, amíg a Jarvis fut. `watch_minutes`: a figyelők gyakorisága (60). `stale_days`: ennyi nap után szól a régóta fent lévő hirdetésről (14, 0 = soha). `speak`: a háttérmunka eredményét fel is olvassa.
- `[api] enabled`: a telefonos kapcsolat. `host` maradjon `127.0.0.1` (a Tailscale innen veszi át), `port`: 8765. `allowed_origins`: honnan jöhetnek böngészős (CORS) kérések (a Capacitor-app címei).
- `[browser] profile_dir`: a Jarvis böngészőjének profilja (bejelentkezések). Üresen a `%LOCALAPPDATA%\Jarvis\browser_profile`.

**Tempó**
- `[assistant] stream`: folyamatos válasz és felolvasás (alapból be).
- `[assistant] fillers`: „Egy pillanat.” a lassú lépések előtt.
- `[audio] silence_seconds`: ennyi csend után dolgozza fel, amit mondtál (0,8).
- `[audio] follow_up_seconds`: válasz után ennyi ideig figyel Enter nélkül (4; 0 = kikapcsolva).

**Egyéb**
- `[llm] think`: `true` esetén alaposabb, de jóval lassabb.
- `[llm] warm_up`: induláskor előre feldolgozza a rendszerpromptot (gyorsabb első válasz).
- `[assistant] max_tool_rounds`: egy kérésen belül legfeljebb ennyi lépés (20). Ha elfogy, a Jarvis a meglévőből válaszol.
- `[tools] allowed_folders`: a fájlos eszközök csak itt dolgozhatnak. Ha a OneDrive átirányítja a Dokumentumokat, add meg a valódi utat.
- `[apps]`: az indítható programok:

```toml
[apps]
"spotify" = "spotify"
"photoshop" = '"C:\Program Files\Adobe\Adobe Photoshop 2026\Photoshop.exe"'
```

## Biztonság

Mostantól a Jarvis idegen weboldalakat olvas és kattint rajtuk, ezért a védelem fontosabb lett:

- **Jelszót és bankkártyaadatot soha nem ír be.** A jelszómezőket és kártyaadat-mezőket a kód tiltja, nem csak a modellre bízza. Belépni neked kell a böngészőablakban.
- **Kockázatos kattintás előtt jóváhagyást kér**: vásárlás, fizetés, megrendelés, küldés, posztolás, regisztráció, törlés, feliratkozás. Az ablakban felugró kérdés jön (Esc = Mégse), konzolban be kell gépelned, hogy „igen”.
- **Űrlap elküldése előtt is kérdez** (keresőmezők kivételével), és kiírja, mit küldene el.
- **A weboldalak szövege adat, nem utasítás.** Egy rosszindulatú oldal megpróbálhatja „rábeszélni” a modellt valamire („hagyd figyelmen kívül az utasításaidat…”). A Jarvis minden webes tartalmat így jelöl, és a rendszerprompt is tiltja, hogy kövesse. Ez csökkenti a kockázatot, de nem zárja ki teljesen, ezért fontosak a kódszintű tiltások és jóváhagyások.
- **Helyi hálózat alapból tiltva** (`allow_local = false`), így egy weboldal nem tudja a routeredet vagy a helyi szolgáltatásaidat elérni a Jarvison keresztül.
- Programot csak az `[apps]` listáról indít, fájlokhoz csak az `allowed_folders` mappáiban nyúl, fájlból programot nem indít.
- A PowerShell-eszköz alapból ki van kapcsolva. Webes böngészéssel együtt különösen ne kapcsold be.
- **A Jarvis ablaka csak a te gépedről érhető el** (127.0.0.1), és minden kéréshez egy induláskor generált véletlen kulcs kell. Egy weboldal (akár olyan, amit a Jarvis böngészője nyitott meg) így nem tud parancsot küldeni neki. A tárgyak fotói is csak ezzel a kulccsal nyithatók meg.
- **Levételnél** a gombbal (vagy a kérdésére adott igennel) adott jóváhagyás csak arra az egy tárgyra, arra az egy oldalra és arra az egy kérésre szól, legfeljebb 15 percig; fizetős gombra (kiemelés, előresorolás, vásárlás) ilyenkor sem kattint.
- **Emberi tempó, nem álcázás:** a böngésző emberi tempóban dolgozik, de az ujjlenyomatát nem hamisítja, a robot-ellenőrzést nem kerüli meg, és a közzététel előtt ugyanúgy kérdez.
- **Hibacsomag:** csak a gépeden marad, és jelszó nem kerül bele (a Jarvis jelszót nem gépel). A képernyőkép és az oldalnézet viszont tartalmazhat személyes adatot, ezért ha elküldöd valakinek, előtte nézd át.
- **Begyakorolt lépések:** csak a tiszta, sikeres menetet jegyzi meg (ha a modell közben visszalépett, más oldalra ment, a tárgy saját hirdetésére kattintott, vagy olyan szöveget írt be, amit nem tud a tárgy adataihoz kötni, nem tanul belőle), és a közzététel előtt ugyanúgy jóváhagyást kér.
- **Telefon:** csak a `127.0.0.1`-en figyel, kívülről a Tailscale-en át érhető el. Eszközönkénti kulcs kell (a gépen csak a hash-e van meg), a párosító kód 5 percig érvényes és csak a Jarvis ablakában kérhető, a böngészős kérések csak a beállított eredetekről jöhetnek. Ne tedd `0.0.0.0`-ra, és ne nyiss rá portot a routeren.
- **Hirdetés közzététele előtt mindig kérdez** („Feltöltés”, „Közzététel”, „Hirdetés feladása” gombok), és a kérdésben ott van az oldal, a tárgy és az ár. Egy sima link (pl. a fejléc „Hirdetésfeladás” linkje, ami csak megnyitja az űrlapot) nem számít közzétételnek. Fotót csak az adott tárgy saját fotóiból tölthet fel, és ha nem egy beállított hirdetési oldalon van (Vinted, Jófogás, Vatera, Hardverapró), előtte is rákérdez.

## Hibaelhárítás

| Tünet | Megoldás |
|---|---|
| „Nem érem el az Ollamát” | Indítsd el az Ollamát (Start menü), vagy `ollama serve`. |
| Lassú válaszok | `ollama ps` → `100% GPU` legyen. A Jarvis is szól induláskor, ha nem. |
| „A modell kb. 10%-a a processzoron fut” | A videókártyákon nincs elég szabad memória, mert más programok is használják őket (böngésző, ChatGPT-alkalmazás, játék a másik ülésen). Zárd be őket, és indítsd újra a Jarvist. A figyelmeztetés alatt látszik, melyik kártyán mennyi a szabad hely. |
| „…processzoron fut, pedig a videókártyákon van még … szabad hely” | Az Ollama (0.40-től) tartalékot hagy a kártyákon. Kisebbre veheted: PowerShellben `setx LLAMA_ARG_FIT_TARGET 512`, majd lépj ki az Ollamából a tálcán, és indítsd újra (utána a Jarvist is). A modell így teljesen a kártyákra kerülhet, és gyorsabb lesz; cserébe kevesebb hely marad a beszédfelismerőnek és a böngészőnek. Ha utána a beszédfelismerés CPU-ra vált, vagy a böngésző akadozik, vedd vissza: `setx LLAMA_ARG_FIT_TARGET 1024`. |
| „Ollama-hiba (500): model runner has unexpectedly stopped” vagy „error reading llama-server response … kényszerítetten bezárta” | Az Ollama modellfolyamata futás közben leállt. Az Ollama 0.40.x a Qwen3.8-cal Windows-on időnként ezt csinálja (a naplójában, `%LOCALAPPDATA%\Ollama\server.log`: `std::bad_alloc`); ez az Ollama-ba épített llama.cpp ismert, még javítatlan hibája, nem a gépedé. A Jarvis ilyenkor egyszer magától újrapróbálja (az Ollama közben 20–40 másodperc alatt újraindítja a modellt), és ott folytatja, ahol tartott; csak akkor szól, ha másodszorra sem megy. Ha nagyon gyakori, frissítsd az Ollamát, amint kijön javított változat. |
| „Hiányzik a böngésző” | `playwright install chromium`, vagy `[browser] channel = "msedge"`. |
| A keresés nem sikerül | A DuckDuckGo néha korlátozza a sűrű keresést. A hibaüzenet ilyenkor a böngészős keresést (Bing) javasolja a modellnek, így általában magától átvált. |
| „Oldal olvasása: … Az oldal hibát adott (403)” | Az oldal a gépi letöltést elutasítja. A Jarvis ilyenkor magától a saját böngészőjével olvassa el (rövid időre megnyílik egy lap az ablakában). Ha a sorban az is áll, hogy „a böngészőmet sem engedi be”, az oldal robot-ellenőrzést kér: a Jarvis másik forrást keres, vagy nyisd meg te. |
| „Elnézést, ez a feladat túl sok lépésből állt” | Ritkán fordul elő: a Jarvis a lépései végén a meglévőből válaszol. Ha mégis ez jön, felsorolja, mit nézett meg; mondd, hogy „folytasd”, és onnan megy tovább. A lépések száma: `[assistant] max_tool_rounds`. |
| Kreatív: furcsa, nem létező magyar szavak | Állítsd a történetet angolra (Emlékezet › Beállítások › Nyelv), vagy vedd lejjebb a **Kreativitást**. Egy rossz választ a **Másik változat** gomb újraír, a **Szerkesztés** gombbal pedig kijavíthatod. |
| Kreatív: ismétli magát, vagy elfelejt valamit | **Másik változat**, vagy írd be a **Szerzői megjegyzésbe**, mit szeretnél (pl. „Vidd előre a cselekményt.”). Amit nem szabad elfelejtenie, azt a **Lényegbe** vagy egy **Világkártyára** írd, vagy adj hozzá egy **Új emléket**. |
| Kreatív: „Sorra vár…” | A modell épp mással dolgozik (pl. a Beszélgetésben vagy egy feltöltésen); amint végzett, ír. |
| A képfelismerés hibát ad | Frissítsd az Ollamát (a korai Qwen3.8-buildeknél volt ilyen hiba). |
| „no user query found in messages” | Javítva: a kontextus betelt, és az Ollama levágta a kérdést. Most a Jarvis 16K kontextust kér, és ha szűkös a hely, előbb a régi oldalnézeteket és korábbi köröket rövidíti, a kérdést soha. |
| Nem nyílik meg az ablak | A konzol kiírja a címet („A felület címe…”); másold be egy böngészőbe. |
| Az ébresztőszó nem működik | `python check_setup.py` megmutatja, miért. Ha az openWakeWord nem települ, `[wake_word] enabled = false`, és gombbal szólj. |
| Túl gyakran ébred fel magától | `[wake_word] threshold` legyen nagyobb (0,6–0,7). |
| „GPU-s beszédfelismerés nem elérhető” | `pip install -r requirements-gpu.txt`, majd `python check_setup.py`. |
| A program kilép a beszédfelismerő betöltésekor | Próbáld: `gpu_device_index = 0` és `gpu_model = "small"`, vagy `device = "cpu"`. |
| Nem hallja, hogy beszélsz | Állítsd be az `input_device`-t, és csökkentsd a `min_threshold`-ot. |
| Rossz monitort néz | `[screen] monitor` (a `check_setup.py` kilistázza a monitorokat). |
| Minden feltöltésnél be kell jelentkeznem | Lépj be egyszer az Alapadatok és bejelentkezés gombjaival, és pipáld be, hogy maradj bejelentkezve. Ha a `config.toml`-ban a `[browser] profile_dir` nem üres, legyen üres (akkor a profil a `%LOCALAPPDATA%\Jarvis` mappában van). |
| ✕ egy oldalnál a kártyán | Nyisd meg a hirdetést: a Hova kerüljön résznél olvasható, mi történt (pl. be kell jelentkezni). Utána **Újra**. Ha azt írja, „lehet, hogy mégis fent van”, nézd meg az oldalon, és ha fent van, nyomd meg a **Fent van** gombot (az **Újra** ilyenkor másodszor is feltöltené). |
| „Error occurred in event listener … MemoryError” a konzolon | A gépen épp elfogyott a memória (RAM és lapozófájl), miközben a Jarvis böngészője feldolgozta az oldal eseményeit. A böngésző korábban minden hálózati kérésről tartott egy objektumot a memóriában (egy hosszabb böngészésnél több száz MB-ot); ez javítva, már csak a folyamatban lévő kéréseket jegyzi. Ha mégis előjön: `ollama ps` (ha a modell egy része a processzoron, azaz a rendszermemóriában fut, az sokat foglal), zárj be más programokat, vagy növeld a lapozófájlt. |
| Mindig visszakérdez ugyanarra | Írd bele a mondatodba (pl. „nagyon jó állapotú”), vagy add meg egyszer az Alapadatoknál (város, szállítás). |
| Az árellenőrzés nem talál árat | Lehet, hogy az oldal robot-ellenőrzést kér: nézd meg a Jarvis böngészőablakát, oldd meg, és próbáld újra (a hirdetés lapján: **Mennyit ér?**). Ha nem talál elég hasonlót, nem állítja meg a feltöltést, a te áradon megy. |
| Egy feltöltés elakadt, és nem tudom, miért | A hirdetés lapján az oldal sorában a **Hibacsomag** gomb megnyitja a mappát: képernyőkép, oldalnézet, a lépések és a modell beszélgetése abból a pillanatból. |
| „Vinted: várok még 1:45” a kártyán | Emberi tempó: ugyanazon az oldalon két közzététel között pár percet vár. Az **Állj** gomb megszakítja; a hossza: `[work] publish_gap_minutes`. |
| „Egy másik Jarvis már fut, de nem válaszol” | Állítsd le a Feladatkezelőben (python vagy pythonw), vagy indítsd újra a gépet. |
| A gép bekapcsolása után nem látszik a Jarvis | Készenlétben, ablak nélkül fut: a `start_jarvis.bat` megnyitja az ablakát. Ha nem indult el, nézd meg a `%LOCALAPPDATA%\Jarvis\naplo.txt`-t. |
| „…mert eltakarja egy nyitva maradt lenyíló lista / felugró ablak” | Egy lista vagy ablak a mező előtt van. A Jarvis ilyenkor megnyomja az Esc-et, és újra próbálja; ha az sem zárja be (pl. süti-sáv), megmondja a modellnek, mi takarja, és az bezárja (pl. „Elfogadom”). |
| A kitöltés elakad egy mezőnél | Jarvis 3–4 próbálkozás után továbblép, és a végén elmondja, mit kell kézzel pótolni; a böngészőablakában be tudod fejezni. |
| „Ez nem kép, vagy ezt a formátumot nem ismerem” | iPhone-os HEIC fotó: `pip install pillow-heif`, vagy állítsd a kamerát JPG-re. |

## Új eszköz hozzáadása

Az eszközök a `jarvis_core/tools.py` fájlban vannak. Egy eszköz egy függvény, ami szöveget ad vissza; a `_register_builtin()` metódusban regisztrálod:

```python
# a fájl elején: import random
self.register(
    "flip_coin",                                          # név
    "Feldob egy érmét, és megmondja, fej vagy írás lett.",  # ebből tudja a modell, mikor hívja
    params(),                                             # paraméterek (JSON Schema)
    lambda: random.choice(["fej", "írás"]),
)
```

Paraméterrel:

```python
self.register(
    "say_hello",
    "Köszön valakinek név szerint.",
    params({"name": {"type": "string", "description": "A személy neve."}}, ["name"]),
    lambda name: f"Szia, {name}!",
)
```

- A **leírás** a legfontosabb: ebből dönti el a modell, mikor kell az eszköz.
- Hibánál dobj `ToolError("érthető üzenet")`-et; a modell megkapja, és elmondja.
- Veszélyes művelet előtt kérj jóváhagyást: `if not self.confirm("Mit csinálnál"): return "Nem engedélyezte."`
- Ha az eredmény hosszú és gyorsan elavul (pl. egy oldal tartalma), regisztráld `ephemeral=True`-val, így a régebbieket a Jarvis kitömöríti.

## Tesztek

```powershell
python -m unittest discover -s tests -v
```

373 teszt; Ollama, mikrofon, GPU és internet nélkül is lefutnak (hamis, folyamatosan válaszoló Ollama-szerverrel, hamis hangkártyával és ébresztőszó-modellel, helyi tesztoldalakkal). Köztük egy regressziós teszt a „no user query found” hibára: egy hamis Ollama ugyanúgy levágja a túl hosszú beszélgetést, mint a valódi. A hamis Ollama azt is el tudja játszani, hogy a modell egy része nem fér a videókártyákra, így a kisebb kontextusos újratöltés is tesztelve van. Az Adás/Vételhez helyi „hirdetési oldalak” vannak: rajtuk a Jarvis végigcsinálja az egészet (visszakérdezés, árellenőrzés, fotók, mezők, jóváhagyás, megjelölés, több oldal sorban), a felületen is. A valódi Vinted, Jófogás, Vatera és Hardverapró oldalain ez nincs kipróbálva, mert azok változnak, és a tesztek nem mehetnek ki a netre. A begyakorolt lépéseket is egy helyi hirdetési oldalon tesztelik: a hamis modell egyszer végigcsinálja a feltöltést (süti-ablak, fotók, mezők, kategória, állapot, márka-kereső, közzététel) és a levételt, a következő tárgynál pedig a Jarvis modell nélkül csinálja, és ellenőrzik, hogy minden mezőbe a második tárgy adatai kerültek. A telefonos kapcsolat tesztjei valódi helyi szervert indítanak (párosítás, kulcs, CORS, beszélgetés, események, Szünet, tárgyak és fotók). Az emberi tempót egy helyi oldalon mérik (billentyűleütések, egérmozgás a kattintás előtt, a gombnyomás hossza, a szünetek a begyakorolt lépéseknél, a várakozás két közzététel között), a hibacsomagot elakadó feltöltésekkel, a közzététel utáni ellenőrzést egy félbemaradt modellválasszal (megnézeti, fent van-e, és nem tölti fel kétszer), a böngésző hálózatfigyelését egy lassú adatkéréssel és 300 apró kéréssel (kivárja őket, de nem marad utánuk semmi a memóriában), a begyakorolt lépések felvételét egy valódi Vinted-feltöltés lépéseivel (a méret és az ár nem ég bele más választásokba), az Ollama modellfolyamatának leállását egy hamis Ollama-hibával (újrapróbálás, a hiba helye és szövege), a készenlétet pedig a valódi `jarvis.py`-val is: indulás modellek nélkül, második indítás, felébresztés kéréssel, `--quit`, és a konzol nélküli (naplófájlos) indulás. A webes kutatás javításait egy helyi, 403-at adó „webshoppal” és egy robot-ellenőrző köztes oldallal tesztelik (böngészős olvasás külön lapon, a blokkoló oldal megjegyzése, a kieső oldal pótlása, a captcha meg nem kerülése, a bezárt böngésző újraindítása, az ismételt hívások kiszűrése és a válasz a lépéskeret végén). A Kreatív menüt mindegyik kiindulóponttal, mindkét nyelven, hamis modellel tesztelik: a prompt felépítése (lényeg, kártyák, szerzői megjegyzés, emlékek, keret), az élő írás, a másik változat, a megszakítás (a félbemaradt mondat nélkül), az összefoglalás és a sűrítés, az ötletből készülő történet, a végpontok, és egy böngészős végigjátszás a felületen. A böngészős és a felület-tesztek valódi Chromiumot indítanak láthatatlanul; ha a `playwright install chromium` még nem futott le, ezek kimaradnak.

## Projektszerkezet

```
jarvis.py              indítás (grafikus vagy konzolos mód)
check_setup.py         telepítés-ellenőrző
config.toml            beállítások
system_prompt.txt      Jarvis "személyisége" és szabályai (Beszélgetés menü)
work_prompt.txt        a Munka menü szabályai (eladás, feltöltés)
                       (a bejelentkezések és a tárgyak a %LOCALAPPDATA%\Jarvis mappában vannak)
jarvis_core/
  app.py               a Jarvis "agya": betöltés, ágens-szál, a menük, hallgatás, válasz, megszakítás
  gui.py               grafikus felület: helyi szerver, események, jóváhagyás, a Munka menü kérései
  ui/index.html        az ablak szerkezete; ui/style.css a kinézete
  ui/app.js            varázsszem, napló, menük, kezelőszervek
  ui/work.js           Adás/Vétel: új hirdetés, kérdések, alapadatok, a hirdetések és a lapjuk
  ui/creative.js       Kreatív: történetek, új történet, élő írás, a körök, az Emlékezet panel
  creative.py          Kreatív menü: történetek, promptok (angol és magyar), kontextus, emlékezet, összefoglalás
  creative_templates.py a kész kalandok és karakterek, angolul és magyarul
  work.py              a Munka menü: az eladás és a levétel lépései, eszközök a Munka menü beszélgetéséhez
  selling.py           tárgyak, fotók, az oldalak és amit kérnek, adatok a leírásból, árkutatás
  replay.py            begyakorolt lépések: felvétel, recept, visszajátszás modell nélkül
  humanize.py          emberi tempó a böngészőben: egérút, gépelési ritmus, szünetek
  dossier.py           hibacsomag: képernyőkép, oldalnézet, lépések, ha egy feltöltés elakad
  scheduler.py         háttérmunka: mikor ér rá a Jarvis, elhalasztás, megszakítás
  watches.py           figyelők (mentett keresések)
  notices.py           értesítések (a telefonnak is)
  api.py               telefonos kapcsolat: párosítás, kulcsok, JSON-végpontok, eseménynapló
  config.py            beállítások betöltése; az adatmappa (%LOCALAPPDATA%\Jarvis) és a régi adatok áthozása
  instance.py          egyszerre egy Jarvis: zárolás, a futó ablakának címe, leállítás (--quit)
  autostart.py         indulás a gép bekapcsolásakor (parancsikon az Indítópultban)
  ui_console.py        konzolos felület
  wakeword.py          „Hey Jarvis” ébresztőszó
  assistant.py         beszélgetés, eszközhívási ciklus, kontextuskezelés
  llm.py               Ollama REST kliens (szöveg és kép), betöltés úgy, hogy elférjen a videókártyákon
  gpu.py               videókártya-adatok (nvidia-smi)
  tools.py             eszközök (itt bővíthető)
  web.py               webes keresés, oldalolvasás, címellenőrzés
  browser.py           saját böngésző: oldalnézet, kattintás, gépelés, jóváhagyások
  screen.py            képernyőkép
  recorder.py          mikrofonfelvétel, csendérzékelés
  stt.py               beszédfelismerés (faster-whisper, GPU/CPU)
  cuda_libs.py         a GPU-s könyvtárak megtalálása Windows-on
  tts.py               felolvasás (Piper), folyamatos felolvasási sor
  textutil.py          szövegtisztítás, mondatokra bontás a felolvasáshoz
tests/                 tesztek (offline, kontextus, videómemória, web és böngésző, felület, Munka menü, ébresztőszó,
                       folyamatos válasz, begyakorolt lépések, háttérmunka és Szünet, telefonos kapcsolat,
                       emberi tempó, hibacsomag, készenlét, hibatűrés, webes kutatás, Kreatív menü)
docs/                  telefon-api.md (a telefonos végpontok), angular/jarvis-api.service.ts (kész Angular-szolgáltatás)
```

## Következő szintek

- **Asztali alkalmazások vezérlése** (3. szint): egérrel és billentyűzettel bármelyik programban, a képernyőkép alapján. Egy helyi 27B-s modellel ez még megbízhatatlan, és sokkal kockázatosabb, ezért most csak nézni tud, kattintani a böngészőben.
- **Közbevágás hanggal**: ha beszéd közben azt mondod, „Hey Jarvis”, elhallgat és figyel. Hangszóróval ehhez visszhangszűrés kell, fejhallgatóval egyszerűbb. Addig az Állj gomb és az Esc állítja meg.
- **Szebb hang**: az XTTS-v2 tud magyarul, és természetesebb a Pipernél, de ~2 GB VRAM kell neki, ami most nincs szabadon.
- **Hosszú távú memória** a Beszélgetésben is: jegyzetekben keresés, korábbi beszélgetések felidézése (a Kreatív menüben a történeteknek már van saját emlékezetük).
- **Kreatív menü hanggal**: a történet felolvasása (az angol szöveghez angol Piper-hang kell, pl. `en_US-lessac-medium`) és a körök diktálása.
- **MCP-kliens**: kész eszközszerverek (naptár, e-mail, Home Assistant) bekötése.
- **Értesítés a telefonra háttérben is** (pl. ntfy vagy a saját appod push-értesítése), amikor egy figyelő új találatot hoz.
- **Gyors parancsok modell nélkül**: az idő, egy időzítő vagy egy program indítása szabályokkal, a nyelvi modell nélkül, azonnal.
