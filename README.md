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
| **Vinted modell nélkül**: az űrlapot a Jarvis maga tölti ki, a modell csak a kategóriát választja ki a Vinted listájából; a levételhez (Törlés, megerősítés) a modell nem is kell | feltöltés kb. egy-másfél perc, nem 5–10; levétel kb. fél perc |
| **eladva → levétel**: megmondod, hol kelt el, és a többi oldalról Jarvis leveszi; naponta meg is nézi a hirdetéseidet, és ha valahol elkeltnek látszik, szól, és az értesítés gombjával, egy jóváhagyás után leveszi a többi oldalról | „A pulóver elkelt a Vinteden, vedd le a többiről”, **Eladva itt, vedd le a többiről** |
| **begyakorolt lépések**: egy oldalon egyszer a modell tölti fel (vagy veszi le), utána modell nélkül | – |
| **emberi tempó**: a böngészőben az egér odamegy a gombhoz, billentyűnként gépel, szünetet tart, és két közzététel között pár percet vár | – |
| **hibacsomag**: ha egy feltöltés vagy levétel elakad, a képernyőkép, az oldalnézet és a lépések egy mappában | **Hibacsomag** gomb |
| **automata kereskedés**: olcsó hirdetések keresése (Vinted, Jófogás, Vatera, Hardverapró), és az **azonnali figyelő**: a Hardveraprón és a Jófogáson pár percenként, böngésző nélkül; az új hirdetésekről rögtön kideríti, megéri-e, és csak akkor szól; egy Indítás/Leállítás gomb, max. vételár | „RTX 3080”, 3 percenként: csak ha új ÉS megéri |
| **Pro feltöltés**: elég a fotó (telefonon a kamerával is); a márkát, a méretet, az állapotot a fotókról olvassa ki, az árat a hasonló hirdetésekből adja meg, és feltöltés előtt mindent megmutat, átírhatóan | Munka › Új hirdetés › **Pro** |
| **Vételeim**: a megvett tárgyak vételára, postája, egyéb költsége, mennyiért adnád tovább, a várható és a tényleges haszon, hol tart (úton, nálad, fent, eladva); telefonon is | Munka › **Vételeim** |
| **figyelők**: a háttérben időnként újra keres, és szól, ha új találat jelent meg | „Szólj, ha lesz új Python-állás Debrecenben” |
| **több oldal egy lépésben**, egy adott oldalon vagy csak friss találatokra | „Mit írnak az állásoldalak a junior Python fizetésekről?” |
| **robotvédett oldalak**: ha egy oldal a sima letöltést elutasítja (403), a saját böngészőjével olvassa el, külön lapon | „Keress egy megbízható, olcsó ébresztőórát” |
| **Kreatív › kaland** (mint az AI Dungeon): te írod, mit teszel, a mesélő folytatja | `> You follow the tracks into the woods.` |
| **Kreatív › karakter** (mint a Character.AI): beszélgetés egy kitalált szereplővel | Mara Vale kapitány, Thorn professzor, Nyx, vagy a saját karaktered |
| **emlékezet a történetekhez**: lényeg, szerzői megjegyzés, világkártyák, automatikus összefoglalás; minden átírható és törölhető | Kreatív › **Emlékezet** |
| **Szünet**: minden modell leáll, a videókártya és a memória felszabadul (pl. játékhoz) | „Pihenj!”, vagy a Szünet gomb |
| **készenlét**: a gép bekapcsolásakor elindul, de modellek nélkül vár; az első kérés felébreszti | `start_jarvis.bat --autostart on` |
| **telefon és laptop**: Android-alkalmazás és a laptopra telepíthető ablak, ugyanaz a felület és mindhárom menü, de minden a gépen fut; értesítések (jóváhagyás, kész feladat, kérdés), a laptopon bezárt ablaknál is; párosítás QR-kóddal, fotók megosztása a galériából, hangutasítás egy érintéssel | lásd Telefonos és laptopos alkalmazás |
| **telefon (saját app)**: a saját appodból (Tailscale-en át) írhatsz, beszélhetsz, tárgyat adhatsz fel, jóváhagyhatsz | lásd [docs/telefon-api.md](docs/telefon-api.md) |
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
| **Értesítések** | amiről Jarvis a háttérben szólt (új találat egy figyelőben, kész vagy elakadt feltöltés, egy elkeltnek látszó hirdetés), a legutóbbi felül; a mellette lévő szám az olvasatlanoké. Egy értesítés gombja oda visz, ahol dönthetsz (pl. a hirdetés Eladva ablakába, lásd Munka menü) |
| **Automatikus munkák** | amit Jarvis magától, időnként megnéz (figyelők, régóta fent lévő hirdetések, a fent lévő hirdetések ellenőrzése): itt törölheted a figyelőket, és ki-be kapcsolhatod a háttérmunkákat (lásd Háttérmunka és figyelők) |
| **Telefon** | a telefonod és a laptopod párosítása (kód és QR-kód), és a párosított eszközök (lásd Telefonos és laptopos alkalmazás) |
| **Értesítések bekapcsolása** | csak a laptopra telepített ablakban: engedélyezi, hogy szóljon, ha kész egy feladat vagy jóváhagyás kell, bezárt ablaknál is |
| „Viszlát, Jarvis” vagy az ablak bezárása | kilépés (a modell kiürül a VRAM-ból); készenléti indításnál az ablak bezárása nem léptet ki |

Az ablak tetején kártyánként egy vékony sáv mutatja a videókártyák terhelését az utolsó percben, gördülő görbeként (mint a Windows Feladatkezelőjének Teljesítmény lapján): minél jobban dolgozik a kártya, annál magasabb és világosabb a vonal. Ha fölé viszed az egeret, látszik a pontos érték: terhelés, VRAM (pl. 15,2 / 16 GB) és hőmérséklet. Az adatokat közvetlenül az NVIDIA-drivertől kéri le (NVML, ha az nincs meg, az `nvidia-smi`-ből), csak amíg az ablak látszik. Kikapcsolás: `[gui] gpu_bars = false`.

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

Jarvis egyetlen modellhívással kiolvassa a mondatodból a hirdetés adatait (egy leírást csak egyszer; ha visszakérdezett, a válaszodból csak a kérdezett mezőket, és ha a tárgy már fent van valahol, a címét és a leírását magától nem írja át) (cím, leírás, kategória, márka, méret, állapot, ár). A fotókat nem nézi, mert az sok idő lenne, ezért amit nem írtál, azt nem is találja ki. Ha a kiválasztott oldalakhoz valami hiányzik, a hirdetés kártyáján (és hangosan is) visszakérdez, pl. „még kell: állapot; átvétel helye (Jófogás)”. A kártyán válaszolsz, vagy egyszerűen beírod (bemondod) a Munka menüben. Ha minden megvan, sorban feltölti minden kiválasztott oldalra.

**A cím** rövid és természetes, ahogy egy ember írná: a márka és hogy mi ez („Sinsay trikó”, „Zara blézer”, „Nike Air Max 90”), a szín, a méret és az állapot nélkül, mert azok külön mezőben látszanak (ha a modell mégis beleírja, a Jarvis kiveszi; elektronikánál a típusjel, pl. a memória marad). **A leírás** a te nevedben szól (nem „az eladó szerint”), és nem ismétli, ami külön mezőben látszik (márka, méret, szín, állapot); csak ha hibát vagy fontos részletet írtál (folt, kopás), azt. Utána egy zárómondat jön arról, hogy írjanak nyugodtan pontos méretért vagy több képért, és hogy a profilodon még sok minden van, amit csomagban olcsóbban kapnak meg; ebből három változat van, tárgyanként más, hogy ne minden hirdetésed vége legyen szóról szóra ugyanaz. A végén legfeljebb hat kulcsszó a Vinted keresőjének (pl. `#sinsay #trikó #sárga #nyári`). A még fel nem töltött tárgyak címét és leírását a következő kiolvasáskor ez alapján újraírja (amit kézzel átírtál, azt nem). **Női, férfi vagy gyerek:** ruhánál, cipőnél és kiegészítőnél a Vintedhez megkérdezi, ha nem írtad, és a kategória első szintje (Nők, Férfiak, Gyerekek) ehhez igazodik; nem találgat.

| Mit kér | Vinted | Jófogás | Vatera | Hardverapró |
|---|---|---|---|---|
| mit adsz el, ár, állapot, legalább egy fotó | ✓ | ✓ | ✓ | ✓ |
| márka, méret, szín | ruhánál, cipőnél, kiegészítőnél | | | |
| átvétel helye (város) | | ✓ | | ✓ |
| szállítás (módok és díjak) | | | ✓ | ✓ |

A várost, a szállítást és a Vinted csomagméretét elég egyszer megadni az **Alapadatok és bejelentkezés** résznél; utána Jarvis onnan veszi, és nem kérdez rá. Ha egy hirdetésnél mást írsz (pl. „csak személyes átvétel Debrecenben”), az az erősebb.

**Pro feltöltés: elég a fotó.** Az Új hirdetés tetején két fül van: **Egyszerű** (fotók és egy mondat, ahogy fent) és **Pro**. A Pro fülön csak a fotókat kell megadnod (a telefonos alkalmazásban és a telefonos böngészőben a **Fotó a kamerával** gombbal rögtön le is fényképezheted; érdemes a címkéről is egy fotót csinálni). A leírás itt csak megjegyzés, nem kötelező (pl. „egy kis folt van az ujján”).
1. **Az adatok a fotókról.** Jarvis az első négy fotót (kicsinyítve) a modellnek adja (a Qwen3.8 képet is ért), és az kiolvassa: mi ez, márka (logó, címke), méret (csak a címkéről, a kinézetből nem becsül), állapot (a látható kopás, folt alapján), szín, anyag, kinek szól, kategória, cím, leírás és kulcsszavak. Amit nem lát biztosan, üresen hagyja: egy rossz adat a hirdetésben rosszabb, mint egy kérdés.
2. **Ha valami hiányzik**, amit a kiválasztott oldal kér (pl. a méret a Vintednek), értesítést küld („Kérdés: …”; a telefonon is, rábökve a hirdetés nyílik meg), és a kártyán válaszolsz; a válaszodból csak azt olvassa ki, amire rákérdezett. Ha inkább a címkéről teszel fel egy fotót, és újra indítod, a fotókat újra megnézi.
3. **Az ár magától**, amennyiért jó áron elkel: a kiválasztott oldalakon megnézi a hasonló hirdetéseket (árkutatás; a modell kiválasztja a tényleg hasonlókat, és a középérték körül javasol), ha ott kevés a hasonló, az Automata kereskedés árbázisából (a hasonlók középértéke és alsó negyede között), végül a modell becsléséből a fotók alapján (ezt jelzi is, hogy nézd meg).
4. **Nézd át.** Feltöltés előtt értesítést küld („Nézd át: …”, a telefonra is), rábökve megnyílik a **Nézd át** ablak: a fotók, hogy honnan jön az ár (pl. „12 hasonló hirdetés alapján, többnyire 5 000–8 000 Ft”), és minden adat **átírhatóan** (cím, ár, állapot, márka, méret, szín, kinek, anyag, kategória, leírás). Ha minden stimmel, vagy átírtad, amit kellett, a **Mehet a feltöltés** gombbal indul; a **Mentés, később** csak elmenti. Amíg nem hagytad jóvá, nem tölt fel; ami itt átírtál, azt Jarvis később sem írja felül. A kártyán az állapot „átnézésre vár”, és az **Átnézem** gomb ugyanezt az ablakot nyitja meg. A feltöltés közben a közzététel előtt oldalanként még egyszer rákérdez, ahogy mindig.
Ha a jóváhagyás után új fotót adsz hozzá, újra megnézi a fotókat, és újra átnézed (a kézzel átírt mezők maradnak).

**Fotóellenőrzés.** A fotókat Jarvis a felvételkor modell nélkül megnézi: homályos-e, túl sötét, túl világos vagy ellenfényes, kicsi-e (a rövidebb oldala 600 px alatt), van-e kétszer ugyanaz a fotó a tárgynál (vagy egy kicsit szerkesztett másolata; két külön elkészített, nagyon hasonló képet nem mindig vesz észre), és megvan-e ugyanez a fotó egy másik tárgyadnál is (ez többnyire tévedés). Amelyikkel gond van, annak az előnézetén egy **!** jelzi (az egér alatt kiírja, mi a baj), a kártyán is, a fotók alatt pedig egy rövid összefoglaló áll. Ha a borítóval (az első fotóval) van gond, javasol helyette egy élesebb, rendben lévőt; a **Legyen a 3. a borító** gombbal előre teszi, magától nem cserél. A feltöltés elején is szól („A 3. fotó homályosnak tűnik; a 2. és a 4. majdnem ugyanaz. Ettől még feltöltöm.”), de nem áll meg miatta. A küszöbök óvatosak: egy átlagos telefonos fotónál (sima vagy fehér háttér, világos tárgy fehér háttér előtt, sötét ruha, ugyanazon az ágyon lefotózott eleje és háta) nem szól. A korábban felvett tárgyak fotóit indítás után a háttérben nézi meg.

**„Nézz utána, mennyiért adják máshol.”** Ha ezt is bepipálod, feltöltés előtt a kiválasztott oldalak keresőjében megnézi, mennyiért kínálnak hasonlót. Ha az árad jóval drágább (a hasonlók középértékénél 25%-kal többe kerül), megáll, és megkérdezi: „Maradjon a te árad, vagy legyen 4 500 Ft?”. Ez csak ajánlás: a **Marad** gombbal (vagy ha azt mondod, „maradjon”) a te áradon tölti fel; a **Legyen** gombbal, vagy ha egy összeget mondasz („legyen 4000”), azzal. Ha az összeg furcsa (pl. a félrehallott „3 és fél ezerből” 3 Ft lenne), előbb visszakérdez („3 Ft-ra gondoltál?”): az **Igen** gombbal (vagy ha igent mondasz) az marad, különben mondd meg újra. Ha az árad rendben van, vagy olcsóbb, csak szól, és megy tovább. A modell elé csak a hasonló hirdetések kerülnek: a keresett és a cserélendő holmi, a csomagban árult, az alkatrésznek vagy hibásan kínált, a saját és az ismétlődő hirdetés kimarad; a kilógó árakat levágja, és a középérték mellett a hasonlók középső felét (25–75%) és a bizonyosságot is megmondja. A licites árakat csak akkor számolja, ha nincs elég fix ár. Ha az árkutatás egy hétnél régebbi, feltöltés előtt megújítja. A hirdetés lapján az **Árkutatás** részben látod, hány találat volt, mennyi és miért maradt ki (kereső- vagy cserehirdetés, csomag, alkatrész vagy hibás, a sajátod, más termék, ismétlődő, vagy a legjobban illő 25 után kevésbé illő), a hasonlókat (a licitet „(licit)” jelzi, mert az csak a mostani ajánlat), a hasonlók középső felét (alsó és felső negyed, középérték), és hogy ez megbízható-e, vagy kevés, nagyon eltérő ár alapján csak tájékoztató.

**A feltöltés.** Jarvis a saját böngészőablakában, a szemed előtt tölti ki a hirdetésfeladást: fotók, cím, leírás, kategória, márka, méret, állapot, ár, helyszín, szállítás (a Vinteden modell nélkül, lásd lent). A **közzététel** gomb előtt oldalanként egyszer rákérdez; a kérdés első sorában látod, melyik oldal, melyik tárgy és mennyiért. A végén szól, hol van fent, és hol nem sikerült. Ami nem sikerült (pl. be kellett volna jelentkezni), azt a kártyán ✕ jelzi; a hirdetés lapján olvasható, miért, és az **Újra** gombbal folytatja. Ha elakadt, az oldal sorában egy **Hibacsomag** gomb is van (lásd lent). Ha a közzétételt jóváhagytad, de a modell a végén nem jelölte meg a hirdetést (pl. félbemaradt a válasza), Jarvis még egyszer megnézeti vele az oldalt, és ha fent van, megjelöli; újra feltölteni ilyenkor nem kezdi, nehogy kétszer kerüljön fel.

**Vinted: kitöltés modell nélkül.** A Vinted űrlapját nem a modell kattintgatja végig (az lépésenként egy modellkör volt egy 1–2 ezer tokenes oldalnézettel, egy feltöltés 25–40 kör, és a kategóriafában, a márkakeresőben, a méreteknél könnyen eltévedt), hanem a Jarvis maga tölti ki: feltölti a fotókat, beírja a címet, a leírást és az árat, a márkát beírja a „Search brands” keresőbe és kiválasztja, az állapotot, a színt és a csomagméretet pedig a Vinted saját lehetőségei közül választja ki, a magyar és az angol felületen is („Nagyon jó” = „Very good”, „fekete” = „Black”, „kicsi” = „Small”; a méretnél a „40” lehet „EU 40” vagy „L / 40 / 12”). A modellt csak ott kérdezi, ahol tényleg dönteni kell: a kategóriafa egy-egy szintjén (pl. Women › Clothing › Jeans › Straight jeans), vagy ha a méret, a márka vagy a szín nem egyértelmű. Ez egy rövid, számozott feleletválasztós kérdés (pár száz token, egy-két másodperc), és a válasz csak a lista egyik száma lehet, így a modell nem tud „kitalálni” egy nem létező kategóriát. Amit a modell egyszer kiválasztott, azt a sikeres feltöltés után megjegyzi (a tárgy fajtája, kategóriája és hogy mi ez alapján, a szín, a márka és a méret nélkül), így a következő hasonló tárgynál (pl. egy másik póló) a modell meg sem szólal. Egy feltöltés így emberi tempóban kb. egy-másfél perc. A közzététel után a Vinted a profilodra visz: a Jarvis ott megkeresi az új hirdetést, és annak a saját webcímét jegyzi meg (`/items/…`, a kártyán erre visz a link), a profilod címét pedig a levételhez. A közzététel előtt ugyanúgy jóváhagyást kér, és a kérdésben látod, mit választott („Kategória: Women > Clothing > Jeans > Straight jeans; Márka: Zara; Méret: EU 40; Állapot: Very good…”). Ha valamit nem talál (pl. a márka nincs a Vinted listájában), vagy a Vinted a közzétételkor hiányol valamit, a modell a **félig kitöltött űrlapon folytatja** (nem kezdi elölről), és pontosan megkapja, mi kész és mi hiányzik; a közzététel után az item_post tiltva van, nehogy kétszer kerüljön fel. Ha be kell jelentkezni, vagy robot-ellenőrzés jön, megáll, és szól. Ha az űrlapot egyáltalán nem ismeri fel (pl. a Vinted átalakította), a régi út jön: a begyakorolt lépések vagy a modell. A választások a `%LOCALAPPDATA%\Jarvis\munka\valasztasok\vinted.json` fájlban vannak; ha törlöd, újra megkérdezi a modellt. Kikapcsolás: `[work] autofill = false`.

**Figyelem:** a Vinted és a legtöbb hirdetési oldal szabályai tiltják a botokat; ha kiszúrják, korlátozhatják a fiókodat. Ezt Jarvis oldalanként egyszer meg is kérdezi, mielőtt először feltöltene oda.

**Emberi tempó.** A hirdetési oldalak robotszűrői a gépies viselkedést figyelik, ezért a Jarvis böngészője úgy dolgozik, mint egy ember: kattintás előtt az egér enyhe ívben odamegy a gombhoz (nem pont a közepére kattint, és a gombot egy pillanatig nyomva tartja), a rövid szövegeket (cím, ár, márka) billentyűnként gépeli, változó tempóban, a hosszút (leírás) beilleszti, ahogy te is a jegyzeteidből, és ha egy mező lejjebb van, az egérgörgővel teker oda (az oldal nem „ugrik”). A begyakorolt lépéseknél, ahol nincs modell, ami magától lassítana, gondolkodási és olvasási szüneteket is tart: egy új oldal után körülnéz, mezők és gombok előtt gondolkodik egy kicsit, a fotókat „kiválasztja”, a közzététel előtt pedig átnézi az űrlapot. Így egy begyakorolt feltöltés kb. egy perc, nem pár másodperc (a modellel ennél jóval több). Ugyanazon az oldalon két közzététel (vagy levétel) között legalább 2 percet vár (véletlenszerűen ±30%; egy jóváhagyást kérő jelölőnégyzet, pl. a feltételek elfogadása, ebben nem számít közzétételnek); ezt a kártyán látod („Vinted: várok még 1:45”), és az **Állj** gomb megszakítja. Ez csökkenti a lebukás esélyét, de nem szünteti meg: a böngésző ujjlenyomatát a Jarvis nem hamisítja, a robot-ellenőrzést (captcha) nem oldja meg helyetted, és az oldal szabályain sem változtat. Beállítás: `[browser] human_pace`, `pace` (a szünetek szorzója), `[work] publish_gap_minutes`.

**Hibacsomag.** Ha egy feltöltés, levétel vagy visszajátszás elakad, a Jarvis egy mappába gyűjt mindent, ami a javításhoz kell: képernyőkép az oldalról abban a pillanatban (`kepernyo.jpg`), az oldalnézet, ahogy a modell látta (`oldal.txt`), a böngészőben tett lépések (`lepesek.txt`, a lenyomott billentyűkkel, pl. Esc), a modell kérései és az eszközök válaszai (`beszelgetes.txt`), és egy összefoglaló (`info.json`: oldal, tárgy, feladat, a hiba oka, webcím; ha a képernyőkép nem készült el, az is, hogy miért). A hirdetés lapján az oldal sorában a **Hibacsomag** gomb megnyitja a mappát az Intézőben. A csomagok a `%LOCALAPPDATA%\Jarvis\munka\hibak` mappában vannak, a legutóbbi 30 marad meg. Ha a közzétételt te nem engedélyezted, az nem hiba: akkor nem készül csomag. A kép és az oldalnézet személyes adatot is tartalmazhat (pl. a felhasználóneved), ezért ha elküldöd valakinek, előtte nézd át.

**A hirdetés lapja** (kattints a kártya képére vagy címére): itt látod és átírhatod, amit Jarvis kiolvasott. Amit átírsz, azt később nem írja felül. Az árat számmal vagy szóval is megadhatod („4500”, „4 500 Ft”, „4,5 ezer”, „ötezer”, „3 és fél ezer”, „4.5k”); ami nem egy összeg (pl. „hát”), azt nem írja be árnak. Itt pipálhatsz be új oldalt, jelölheted kézzel, hogy fent van vagy már levetted, és itt van az **Eladva** gomb is.

**Eladva és levétel.** Az **Eladva** gombra Jarvis megkérdezi, hol kelt el, és (ha bent hagyod a pipát) a többi oldalról leveszi a hirdetést; ahol elkelt, ott nem nyúl hozzá. Egy oldalról külön is leveheted (**Levétel** gomb az oldal sorában), és szóban is kérheted: „a pulóver elkelt a Vinteden, vedd le a többiről”. A levételt a gombbal (vagy a kérdésére adott igennel) jóváhagyod; utána az adott oldalon a törlés és a megerősítés gombjaira már nem kérdez rá újra, fizetős lehetőségre (kiemelés, előresorolás) pedig soha nem kattint. A sikeres levétel után a sorban „Levéve” áll; ha nem sikerült, a hirdetés fent marad, és a sorban olvasható, miért.

**Elkelt máshol? Háttérellenőrzés.** Jarvis naponta megnéz néhány fent lévő hirdetésedet, csak olvasva (lásd Háttérmunka és figyelők). Ha valahol elkeltnek vagy eltűntnek látszik, szól, és a kártyán (fehér, „!” jellel) és az oldal sorában is látod („Elkeltnek látszik”, „Már nincs fent?”); a sorban azt is, mikor nézte meg, és miből gondolja („Ellenőrizve: okt. 19. 14:05, „Eladva” jelzés az oldalán.”), vagy hogy nem nyílt meg. Ha tényleg ott kelt el, az **Eladva itt, vedd le a többiről** gomb megnyitja az Eladva ablakot, előre kiválasztva; a többi oldalról a **Rendben** után veszi le. Ugyanez a gomb az értesítésen is ott van (a gép **Értesítések** ablakában, a laptop és az Android-alkalmazás értesítésében): oda visz, magától nem jelöl meg és nem vesz le semmit. Ha a jelzés téves, a **Nem igaz, fent van** gombbal elveted; ugyanazt a jelzést annál a hirdetésnél többé nem hozza fel. A **Nézd meg** szűrő azokat a hirdetéseket mutatja, ahol ilyen jelzés van, vagy ahol a fent lévő hirdetésben még a régi ár vagy cím szerepel.

**Ha átírod a fent lévő hirdetés árát vagy címét.** A hirdetés lapján átírt ár vagy cím csak a Jarvis nyilvántartásában változik: a fent lévő hirdetéseket Jarvis magától nem szerkeszti. Ezt rögtön meg is mondja („Csak itt írtam át; a Vinteden még 9 000 Ft. … ott kézzel kell átírni, vagy levenni, és újra feltölteni.”), és amíg a kettő eltér, az oldal sorában látod („Ár eltér: a hirdetésben még 9 000 Ft, itt 8 500 Ft.”, „Cím eltér: …”), a kártyán pedig „A hirdetésben még a régi ár: Vinted”. A kártyán az is látszik, mióta van fent („12 napja fent”).

**Vinted: levétel modell nélkül.** A Vintedről a Jarvis maga veszi le a hirdetést, nem a modell keresgél az oldalakon (az lépésenként egy modellkör volt, és a hirdetés oldalsávjában lévő Törlés gombot a modell oldalnézete nem mindig mutatta, ezért görgetett és képernyőképeket nézegetett). Megnyitja a hirdetés oldalát (a feltöltéskor megjegyzett webcímen; ha az nincs meg, a profilodon keresi meg, végül a Vinted keresőjében, ahol a tiéd az, amelyiken ott a Törlés gomb; a hirdetésed oldaláról a profilod címét is megjegyzi), rákattint a **Delete** (Törlés) gombra, a megerősítő ablakban is, és megnézi, hogy a hirdetés tényleg eltűnt-e. Emberi tempóban dolgozik (körülnéz az oldalon, az egér odamegy a gombhoz, a megerősítés előtt gondolkodik egy kicsit): a helyi tesztoldalakon 11–19 másodperc, a valódi Vinteden az oldalak betöltése miatt valamivel több. Ha be kell jelentkezni, vagy robot-ellenőrzés jön, megáll, és szól; ha a hirdetés már nincs fent, „Levéve (már nem volt fent)”. Ha a hirdetés oldalán nem találja a Törlés gombot (pl. a Vinted átalakította az oldalt), máshol nem keresi (nehogy egy azonos című másik hirdetésedet törölje): a begyakorolt lépések vagy a modell folytatja, de már a hirdetés oldaláról, és hibacsomag készül arról, mit látott. Ha a törlés után nem egyértelmű, hogy eltűnt-e, a modell megnézi az oldalt. Akkor is így megy, ha a beszélgetésben kéred („a farmer elkelt, vedd le a Vintedről”). Kikapcsolás (a feltöltéssel együtt): `[work] autofill = false`.

**Vevői üzenetek.** A Jarvis időnként (alapból kétóránként, amikor ráér) megnézi a Vinteden az üzeneteidet: modell nélkül, emberi tempóban, és csak olvas. Megnyitja az üzenetek listáját, az olvasatlan beszélgetések közül egy körben legfeljebb hármat (a linkjükkel, semmire nem kattint), és elolvassa az üzeneteket: ki írta (te vagy a vevő), mit, és ha látszik, mikor. A beszélgetéseket a gépen jegyzi meg (`%LOCALAPPDATA%\Jarvis\munka\uzenetek.json`), és ha lehet, a tárgyadhoz köti (a beszélgetésben lévő hirdetéslink, vagy a hirdetés címe alapján). Ha új üzenet jött, szól (a telefonra is). Ha be kell jelentkezni, vagy robot-ellenőrzés jön, abbahagyja, és egyszer szól. A Munka menü **Üzenetek** fülén látod a beszélgetéseket (az újak elöl), az üzeneteket és a tárgyat. A **Válaszvázlat** gombra egy modellhívással vázlatot ír, csak a tárgy adataiból: nem talál ki semmit, nem ígér olyat, ami nincs az adatok között, a vevő szövegét adatnak veszi (ha utasítást ír bele, nem követi), és rövid, természetes magyarsággal ír (ha a vevő más nyelven írt, azon). Árajánlatnál a legkisebb elfogadható ár alá soha nem megy: ez a tárgy lapján az **Alkunál legalább** mező, ha üresen hagyod, az ár (alku nélkül); ha a modell vázlatában mégis kisebb összeg van, egy egyszerű, udvarias elutasítás lesz helyette. A vázlatot átírhatod. **Elküldeni csak te tudod**: a **Küldés** gombra a Jarvis beírja a szöveget a beszélgetésbe, és a küldés előtt rákérdez, a kérdésben pontosan azzal a szöveggel, ami elmegy; ha nemet mondasz, kitörli a mezőből. Utána megnézi, hogy megjelent-e, és feljegyzi. Ha a válaszmezőt vagy a Küldés gombot nem találja, megáll, és szól (nem találgat). A Munka menü beszélgetésében is kérdezheted („írt valaki?”, „írj választ Annának”): a modell felsorolhatja az üzeneteket és vázlatot írhat, de küldeni nem tud. A valódi Vinted üzenetoldalán ez még nincs kipróbálva (a tesztek egy helyi oldalon futnak, ami utánozza); ha nem ismeri fel a beszélgetéseket, azt írja, hogy nem talált beszélgetést. Beállítás: `[scheduler] message_minutes` (0 = ki), `message_reads`.

**Automata kereskedés** (Munka › Automata kereskedés). Megadod, mit keresel (pl. „RTX 3060 Ti”, „PS5 kontroller”), melyik oldalakon (Vinted, Jófogás, Vatera, Hardverapró; a Vaterán a fix áras hirdetések, a licitekre lásd lent), mikor számít olcsónak (alapból 30%-kal a szokásos ár alatt, **vagy** ha legalább a megadott forint haszon jön ki belőle), és ha akarod, egy ársávot (legalább/legfeljebb; a minimum ár alatti tok, alkatrész, „1 Ft-os” hirdetés így ki sem jön) és kizáró szavakat (pl. „tok, töltő, játék, doboz”).

**Indítás és Leállítás, vételi limit.** A fül tetején egyetlen gomb van: az **Indítás** egyszerre elindít mindent (a kereséseidet, a figyelt oldalakat, a böngészést és az azonnali figyelőt), napszaktól függetlenül, amíg a **Leállítás** gombbal le nem állítod; közben ugyanúgy pihenőket tart, és ha a Jarvist használod, vár. Mellette látod, mióta fut, és mit csinál épp. Alatta két közös szűrő: a **max. vételár** (legfeljebb ennyiért vegyen; üresen bármennyiért) és a **min. haszon** (alapból 5 000 Ft). A max. vételárnál drágább hirdetést egyik út sem nézi meg (a modellhez sem kerül, az oldalát sem nyitja meg); a licitnél legfeljebb eddig javasol licitálni, a Vinteden pedig ajánlatot javasol, ha az belefér. Ha később emeled, a korábban emiatt kihagyott, még fent lévő olcsó hirdetések is sorra kerülnek. A gomb állapota minden ablakban (a gépen és a telefonon) rögtön frissül, és amíg a fül nyitva van, félpercenként az is, mit csinál épp; a konzolon (és a fül alján, a Naplóban) a munkamenetek összefoglalója, a jó találatok és amit tanult.

**Egy megérő találat: az értesítéstől az üzenetig.** Ha a Jarvis megérő hirdetést talál, a telefon **két percig rezeg** (és ha a hang be van kapcsolva, csenget is), aztán tíz perc szünet, és ha addig nem nézted meg (és nem söpörted el), újra; legfeljebb kb. egy óráig (az Android-alkalmazásban a „Megérő hirdetés” értesítési csatorna; a néma módot tiszteletben tartja). Az értesítésre bökve (a telefonon és a laptopon is) a Jarvis a **találat ablakával** nyílik meg: a címe az áru neve, alatta az ára, hogy most mennyiért mennek a hasonlók (hány hirdetés alapján, melyik oldalakról), és mennyit kereshetsz vele (az óvatos eladási áron eladva, a postával együtt), a figyelmeztetések, és a modell rövid véleménye. Alul az **Üzenet másolása és a hirdetés megnyitása** gomb kimásolja a vágólapra az eladónak szóló üzenetet („Szia! / Megvan még a termék? / Köszi előre is / Üdv, Zsolt”; a fülön az **Üzenet az eladónak** alatt átírhatod), és megnyitja a hirdetést (a telefonon a hirdetési oldal alkalmazásában vagy a böngészőben), ahol rögtön beillesztheted és elküldheted. A **Szakértői tipp** gomb egy kérdést másol ki a hirdetés nevével, árával, a hasonlók árával és a linkjével („Találtam egy jónak tűnő árut, ami jó befektetés lehetne, ha vétel után kicsit drágábban továbbárusítanám. Megnéznéd, hogy tényleg jó vétel lenne-e?”), és megnyitja a ChatGPT-t (az Android-alkalmazásban a ChatGPT alkalmazást, ha telepítve van, különben a chatgpt.com-ot; a gépen új lapon): ott csak beilleszted, és pár másodperc alatt kapsz egy második véleményt. Ugyanez az ablak nyílik meg a találatok listájában a **Részletek** gombbal; a **Megvettem** innen is megy.

**Pénzügyi keret.** A Vételeim fülön (**Keret és figyelmeztetés**) megadhatsz egy **havi keretet**: ebbe az adott hónapban vett tárgyak vételára, postája és egyéb költsége számít, és ha fogy, a Jarvis csak a maradékba beleférő ajánlatokról szól (a maradék a max. vételárral együtt limit). Amikor a 80%-a elment, és amikor elfogyott, egyszer szól. Megadhatod azt is, hány megvett, de még el nem adott tárgy (vagy mennyi pénz bennük) után szóljon, hogy **felhalmozódtak**: ilyenkor naponta egyszer (és rögtön egy újabb vétel után) szól, a legrégebbiekkel együtt. A fül tetején egy sáv mutatja, mennyi ment el és mennyi maradt; a figyelmeztetések ott és az Automata kereskedés fülön is látszanak.

**A szokásos ár.** Egy új keresésnél a Jarvis először felméri, mennyiért adják most: modell nélkül kiszűri, ami biztosan nem az (hiányzik a keresés valamelyik szava, kiegészítő, kereső-, csere- vagy csomaghirdetés, alkatrész, kizáró szó, a minimum ár alatti), a maradékból **egyetlen modellhívás** választja ki a valódi, működő, egy darabos termékeket, és ezek középértéke lesz a javaslat. Az árat nem a modell találja ki (a mostani magyar használtpiaci árakat nem ismeri megbízhatóan), hanem a kiválasztott hirdetésekből jön. Erről szól (a telefonra is): „PS5 kontroller: szerintem most kb. 14 000 Ft (18 hasonló hirdetés alapján, a többség 12 500–15 500 Ft)”. A fülön látod, miből számolt, és **Elfogadom**, vagy **Átírom**; addig ezzel a kereséssel nem keres. Ha tudod a szokásos árat, már a keresés felvételekor megadhatod, akkor felmérés sem kell. Ha a felmérés közben jóval olcsóbbat talál, arról is szól. Ha kevés a hasonló hirdetés (4-nél kevesebb), szól, hogy add meg kézzel, és egy nap múlva újra próbálja. Ha a hirdetésekben 3 napja jóval (20%-nál többel) más az ár, mint a jóváhagyott, szól, hogy mérd fel újra (**Újra felmérem**), vagy írd át.

**A keresés.** Az Indítás után a Jarvis, amikor ráér, emberi tempóban megnézi az oldalak keresőjének első oldalát, a **legújabb hirdetésekkel elöl**; keresésenként megadhatod, milyen sűrűn (30 perc, 1, 3 vagy 6 óra; az új keresések alapértéke `[scheduler] deal_minutes`, 0 = a háttérben soha), a Vintedet a robot-ellenőrzés miatt legfeljebb kétóránként. Modell nélkül, a jóváhagyott szokásos árhoz méri a még nem látott hirdetéseket, és azokat is, amelyeknek azóta lement az ára (**leárazás**: „leárazta: 95 000 → 62 000 Ft”). A várható haszon: a szokásos ár 90%-a (a hirdetési árnál többnyire kicsit olcsóbban kel el), mínusz a vételár és kb. 1 500 Ft posta; ami nem hoz hasznot, az nem jelölt. A jelölt hirdetés oldalát megnyitja (csak olvasva, egy körben legfeljebb ötöt; a többi a következő körre marad), és modell nélkül átnézi: ha már elkelt, vagy a leírásban hibára utaló szó van (repedt, nem kapcsol be, alkatrésznek…), arról nem szól. Csak az átment jelöltekre kérdez rá röviden a modellel, a hirdetés leírásával együtt (tényleg ugyanaz, egy darab, nem hibás, nem gyanús?).

**A gyanús hirdetésekről egyáltalán nem szól** (a találatok közé sem kerülnek, a telefonra sem mennek; csak az Automata kereskedés naplójába írja be, mit hagyott ki és miért, így látod, ha túl szigorú): ha **gyanúsan olcsó** (a szokásos ár 40%-a alatt; ilyenkor az oldalát meg sem nyitja), ha egy gyakran hamisított márka (AirPods, Nike, Lego, The North Face…) feltűnően olcsó (a szokásos ár 60%-a alatt), ha **átverésre utaló jel** van a hirdetésben (csak előre utalással, külföldről küldi, Western Union, ajándékkártya), ha az oldalon kívül kér elérhetőséget (**WhatsApp, Telegram, Viber, e-mail**), ha az eladónak **még nincs értékelése**, vagy ha a modell szerint átverésnek vagy hamisítványnak látszik (túl szép, hogy igaz legyen, sürget, futárcéget kér, a leírás nem illik a termékhez). A licitek mostani ára nem számít gyanúsan olcsónak (az még nő). Ami átmegy, arról szól (a telefonra is), a hirdetés linkjével és a várható haszonnal; a listában is megmarad. Egy hirdetésről egyszer szól (leárazáskor újra). Csak olvas: semmit nem vesz meg, és nem ír senkinek. Az Indítás gombbal indul. Egy keresés beállításait a **Szerkesztés** gombbal módosíthatod (ha a keresett terméket írod át, a szokásos árat újra felméri).

**Ajánlat a Vinteden.** Ha egy Vinted-hirdetés kicsit drágább annál, ami még megéri, de legfeljebb 20%-kal, a Jarvis ajánlattételt javasol akkora összeggel, amivel még megéri (kerek százasra lefelé): „Ha 70 000 Ft-ért megkapod (ajánlattétel), kb. 18 500 Ft a haszon. Ajánlatot tegyek?”. Az értesítés **Ajánlatot teszek** gombjával (vagy a fülön, ahol az összeget át is írhatod) a Jarvis megnyitja a hirdetést, rákattint az **Ajánlattétel** gombra, beírja az összeget, és a küldés előtt **rákérdez**, pontosan az összeggel és a hirdetéssel. Csak a jóváhagyásod után küldi el. Fizetni nem fizet: ha az eladó elfogadja, a Vinted értesít, és te veszed meg; a Jarvis utána nem figyeli. Ha ki vagy jelentkezve, vagy nem találja a gombot vagy a mezőt, megáll, és szól. A valódi Vinted ajánlattétel-ablakán ez nincs kipróbálva (a tesztek egy helyi oldalon futnak, ami utánozza).

**Figyelt oldalak (Felfedező): bármi, ami megéri.** Itt nem kell megadnod, mit keresel. A böngészőben nyiss meg egy kategóriát vagy találati oldalt bármelyik oldalon (pl. Hardverapró › Videókártyák, Jófogás › Konzolok, Vinted › Férfi cipők) a **legújabb hirdetésekkel elöl** (a Vinteden ezt a Jarvis maga hozzáteszi), a Vaterán a „hamarosan lejár” rendezéssel, és másold be az oldal címét (a Vaterán keresőszó is lehet). Megadhatod a legkisebb hasznot (alapból 5 000 Ft), a liciteknél azt, hogy a zárás előtt legfeljebb hány órával szóljon (alapból 6), és hogy milyen sűrűn nézze (alapból óránként; a Vintedet legfeljebb kétóránként). Egy oldalbetöltés 20–40 friss hirdetést hoz, és ezekből tölcsérszerűen szűr:
1. **Modell nélkül, minden hirdetésre.** Ami kereső-, csere- vagy csomaghirdetés, alkatrész, hibás vagy kiegészítő, kiesik. Ami illik egy fenti, felmért keresésedre, azt annak a szokásos árához méri. A többinek a címéből kiolvassa a terméket (lásd lent: árbázis), és ha az árbázis ismeri, ahhoz méri. Jelölt az, ami legalább 15%-kal olcsóbb a szokásosnál, és az **óvatos eladási áron** is legalább a megadott haszon jön ki belőle (a postával együtt). Az óvatos eladási ár a szokásos ár 90%-a, de legfeljebb a hasonlók olcsóbb negyedének ára: ha szórnak az árak, a vevő az olcsóbbakat találja meg előbb, így a haszon nem lesz túlbecsülve.

   **Biztos ár.** Egy ilyen (nem a kereséseddel felmért) szokásos ár csak akkor számít, ha legalább 5 hasonló hirdetésen alapul, **legalább két oldalról** (ha csak egy oldalon láttuk, ott legalább 10-en). Ha ennyi nincs, mielőtt szólna, gyorsan megnézi, mennyiért adják a többi oldalon (oldalanként egy keresés, ahol lehet, böngésző nélkül; az azonnali figyelő a két oldalt egyszerre nézi, a hirdetés címének kulcsszavaival, pl. „Sony WH-1000XM4”), és csak akkor szól, ha így is elég hasonló van, és ott is jóval drágábban adják. Ha nem biztos, nem szól, csak a naplóba írja („Nem biztos, hogy megéri…”, hány hasonlót talált, és hol). Az értesítésben és a találat ablakában látod, hány hirdetés alapján és melyik oldalakról jön a szokásos ár.
2. **Egy modellhívás az ismeretlenekre** (legfeljebb 20 hirdetés egyszerre, a licitek elöl): a modell megnevezi a termékeket (márka és típus), és kiválaszt legfeljebb hármat, ami megérhet. A megnevezett hirdetések ára a termék neve alatt bekerül az árbázisba, és ha így már ismert a szokásos ár, ahhoz méri. A kiválasztottaknak modell nélkül megnézi az árát a többi oldalon (Vinted, Jófogás, Hardverapró).
3. **A jelöltek** oldalát megnyitja (elkelt-e, hibás-e, gyanús-e), és egy rövid modellkérdés után szól, a hirdetés linkjével, a szokásos árral, a várható haszonnal és ha tudja, az eladási sebességgel. A gyanúsakról nem szól (lásd fent). A Vinteden a kicsit drága hirdetésnél ajánlattételt javasol (lásd fent).
4. **Liciteknél** a javasolt legmagasabb ajánlattal szól: „Licit, 1 óra 5 perc múlva zárul, most 20 000 Ft. Máshol kb. 100 000 Ft-ért adják. Legfeljebb 83 500 Ft-ig érdemes licitálni.” **Licitálni nem fog helyetted**; a Vaterán megadhatsz egy legmagasabb ajánlatot, és a Vatera licitál addig. A licitekről a zárás előtt legfeljebb a megadott órával szól (az Indítás után a nap bármelyik órájában). A licitek hátralévő idejét és a licitek számát a találati kártya szövegéből olvassa; a valódi Vatera-oldalon ez nincs kipróbálva.

Egy hirdetésről egyszer szól (ha később leárazzák, újra). Becsült modellhasználat: oldalanként és körönként legfeljebb egy megnevező hívás, és a jelöltekre egy-egy rövid kérdés; a modell csak akkor dolgozik, ha épp nem használod.

**Azonnali figyelő (Hardverapró, Jófogás): új ÉS megéri.** A jó vételek többnyire a friss hirdetésekből jönnek, és ezeken az oldalakon nincs „értesíts, ha új jön”, pedig itt az nyer, aki elsőnek ír. Az Automata kereskedés fülön megadod, mit figyeljen: egy keresőszót (pl. „RTX 3080”, a Hardveraprón és/vagy a Jófogáson), vagy egy találati oldal címét (pl. Hardverapró › Videókártyák; a felkapott Hardverapró-kategóriákat egy gombnyomással is felveheted), ha akarod, ársávot és kizáró szavakat, és hogy milyen sűrűn nézze (2, 3, 5, 10 vagy 15 percenként; alapból 3). A Jarvis **böngésző nélkül**, egy saját szálon tölti le a találatok elejét (lásd lent), így nem vár a böngészőre, és arra sem, hogy épp beszélgetsz-e vele. Az első letöltésnél csak megjegyzi, ami már fent van; utána minden új (vagy leárazott) hirdetésről rögtön kideríti, **megéri-e megvenni**, és **csak akkor szól, ha új ÉS megéri**: először modell nélkül a felmért kereséseid és az árbázis szokásos árához méri (ez egy pillanat); amit így nem ismer fel, azt a modell nevezi meg (egy hívás az egész kötegre), és ha az árbázis a nevét sem ismeri, a modell szerint megérőknek megnézi az árát a másik oldalon is; a megérő jelölt oldalát letölti és átnézi (elkelt, hibás, gyanús: nem szól róla; a gyanúsan olcsóét le sem tölti), végül egy rövid modellkérdés (tényleg ez, egy darab, működik?). Ami átment, arról szól, a telefonra is (sürgős értesítésként, hogy alvó telefonon is azonnal megjöjjön), a címével, árával, linkjével, a szokásos árral és a várható haszonnal, és a találatok közé teszi (**Megvettem** gomb). A max. vételárnál drágább, a kereső-, csere- és csomaghirdetés meg sem jön. Ha épp dolgozol a Jarvisszal, a modellt nem zavarja: ami csak modellel mérhető fel, az utána jön (legfeljebb fél óráig vár; a modell nélkül felmérhetőről akkor is rögtön szól). Hangosan csak a címét mondja, és akkor sem, ha épp dolgozol a Jarvisszal. A fülön látod a figyelőket (mikor nézte utoljára, hány megérőt talált, szüneteltethető), a **Friss hirdetéseket** (csak a megérők), és oldalanként, hány kérés megy így óránként. Az Indítás gombbal megy, a Leállítás és a Szünet leállítja, a készenlét nem. Óránként oldalanként legfeljebb 60 letöltést használ el a figyelésre (a többi az Automata kereskedés köreié); ha több figyelőt adsz meg, mint amennyi ebbe belefér, mindegyik ritkábban kerül sorra. Ha egy oldal tilt vagy robot-ellenőrzést kér, ott egy órára (ismétlésnél tovább) leáll, nem próbálja megkerülni, és egyszer szól. A Vintedet nem figyeli így: ott a Vinted saját értesítése van, és a robot-ellenőrzés miatt is jobb, ha ritkán nézi a Jarvis. Csak olvas: semmit nem vesz meg, és nem ír senkinek.

**Böngésző nélkül, párhuzamosan.** A Hardverapró és a Jófogás a szerveren rajzolja ki a találatokat, ezért ezeket a Jarvis sima letöltéssel olvassa (egy találati oldal kb. egy másodperc, ablak, egér és görgetés nélkül), ugyanazzal az elemzéssel, mint a böngészőben. Egy kör elején a két oldal összes esedékes találati oldala a háttérben töltődik le, miközben a böngésző a Vintedet és a Vaterát nézi; a jelöltek oldala és az eladási sebességhez újranézett hirdetések is így jönnek. Biztonság: oldalanként egyszerre egy kérés, két kérés között 8–16 másodperc (véletlenszerűen), óránként legfeljebb 90. Ha egy oldal tilt (403, 429, robot-ellenőrzés), egy órára (ismétlésnél egyre tovább, legfeljebb egy napig) ott csak a böngésző dolgozik; ha a letöltött oldalon háromszor egymás után nem talál hirdetést (pl. átalakították az oldalt), hat órára szintén a böngészőre vált, így a kereskedés akkor is megy, ha ez az út elromlik. Kikapcsolás: `[work] fast_fetch = false` (akkor az azonnali figyelő sincs). A valódi oldalakon ez nincs kipróbálva (a tesztek nem mehetnek ki a netre): a tesztek a két oldal kártyáit utánzó helyi oldalakon futnak.

**Árbázis és „Mennyit ér?”.** Minden hirdetés ára, amit a Jarvis lát (a keresések, a figyelt oldalak és az árkutatás találatai), a gépen marad (`%LOCALAPPDATA%\Jarvis\munka\arbazis.json`), termékenként. A terméket modell nélkül a címből olvassa ki: a termékcsalád vagy a márka és a típusjel („RTX 3060 Ti”, „iPhone 13 Pro”, „PS5 kontroller”, „Makita DHP482”, „Lego 42115”), a méret, a memória és az ár a címben nem számít. Ami nem egy biztos termék (csak a család, pl. „Nintendo Switch”, vagy több generáció egy néven, pl. „AirPods Pro” szám nélkül, vagy egy márka egy cipőmérettel), az nem kap kulcsot: azt csak a modell nevezheti meg. Óvatos: inkább legyen kevesebb ár egy termékhez, mint két különböző termék egy kalap alatt; szokásos árat csak akkor mond, ha legalább 5 friss (45 napon belüli) ára van, és nem szórnak nagyon (különben pl. a PS5-játék és a konzol keveredne). Pár nap alatt kézi beállítás nélkül összeáll a gyakori termékek ára. A fül **Mennyit ér?** mezőjébe írt termékre (pl. „RTX 3060”) megmutatja a szokásos árat és a hasonló termékekét (pl. „RTX 3060 Ti”), modell és oldalbetöltés nélkül. **Eladási sebesség:** körönként két régebben (2–30 napja) látott hirdetés oldalát újra megnézi (csak olvasva; a Vintedet nem, a robot-ellenőrzés miatt), és ha addigra eltűnt vagy elkelt, feljegyzi, hány nap után. Ebből lesz a „gyorsan kel: 3/4 újranézett hirdetés eltűnt, kb. 4 nap alatt” vagy a „lassan kel” megjegyzés a találatoknál. Ez a hirdetési (kínálati) árakból tanul; amennyiért ténylegesen elkel, az kb. 10%-kal kevesebb, ezért számol a 90%-kal.

**Böngészés napközben (reggeltől estig).** Az Indítás után a Jarvis magától böngészi az oldalakat, ahogy egy ember, és szól (a telefonra is), amint olyan hirdetést talál, amiből haszon lehet (a fülön bejelölheted, melyik oldalakon). Munkamenetekben dolgozik: kb. 20 percig (15–25) nézelődik egy oldalon, két oldalbetöltés között fél-egy percet „olvas” (a Vinteden 45 másodperc és 2 perc között), néha megnyit egy hirdetést is, aztán fél-egy perc múlva a következő oldalra vált (Hardverapró → Jófogás → Vinted → Vatera, amelyiket bejelölted). Csak az az oldal pihen, amelyiken épp volt (legalább 10 percet, a Vinted két órát): a Jarvis közben a többin dolgozik, és csak akkor vár, ha minden bejelölt oldal pihen (pl. ha csak egyet jelöltél be). **Két sor párhuzamosan:** a Hardverapró és a Jófogás munkamenetei böngésző nélkül, a saját szálukon mennek (Hardverapró → Jófogás), és közben a böngésző a Vintedet és a Vaterát nézi (Vinted → Vatera), így egyik sem vár a másikra. Ha a Hardverapró vagy a Jófogás tiltaná a böngésző nélküli olvasást, azt a böngésző sora veszi át. A gyors sor a modellt csak akkor kérdezi, ha épp nem dolgozol a Jarvisszal (addig az ismeretlenek várnak). A Vinteden a kezdőoldallal kezd, és onnan nyit meg egy hirdetést, aztán márkákat néz (Nike, Adidas, The North Face, Ralph Lauren, Carhartt, Lego, AirPods); a Hardveraprón a felkapott kategóriákat (videókártya, memória, SSD, processzor, alaplap); a Jófogáson és a Vaterán népszerű termékeket (iPhone, PlayStation, Nintendo Switch, Lego, AirPods, Dyson, Makita; a Vaterán a hamarosan záruló licitekkel). Egy munkamenetben legfeljebb 30 oldalt tölt be (a Vinteden 10-et), a Vintedre két munkamenet között legalább két órát vár, és ha egy oldal robot-ellenőrzést kér, aznap már nem nézi (nem próbálja megkerülni), és egyszer szól. A hirdetéseket ugyanaz a tölcsér szűri, mint a figyelt oldalakat, a legkisebb haszon itt külön állítható (alapból 5 000 Ft). **Energiatakarékos:** a felismert termékeknél nincs modell, az ismeretleneket gyűjti, és csak 20-asával kérdezi a modellt (a munkamenet végén 8-tól, vagy ha licit is van köztük; a kevesebb a következő munkamenetre vár); ha minden oldal pihen, vagy este van, semmit nem csinál, és ha a Jarvist használod, a böngészés vár. Készenlétben (automatikus indításnál, vagy ha egy ideje nem használod) is fut: a böngésző ilyenkor elindul, a modell egy-egy kérdés után csak 5 percig marad betöltve; a Szünet gomb ezt is leállítja. Az Automatikus munkák ablakban és a fülön látszik, mit csinál épp (két sornál mindkettő: „Most: Hardverapró (09:12 óta). Most: Vinted (09:14 óta).”, „Hamarosan: Jófogás.”, „Pihen 09:47-ig, utána: Vinted.”). Amíg az automata kereskedés fut, a modell készenlétben is fél óráig marad betöltve egy kérdés után (különben 5 percig), hogy a friss hirdetések felmérése ne a betöltésre várjon.

**Öntanulás.** A Jarvis menet közben tanul, a gépen, modell nélkül:
- **Árak és eladási sebesség:** minden látott ár bekerül az árbázisba, és a régebben látott hirdetések újranézéséből tudja, mi kel el gyorsan (lásd fent).
- **Új márkák:** ha a modell egy terméket olyan márkával nevez meg, amit a kulcsfelismerés nem ismer (pl. „Anker PowerCore 20000”), háromszori előfordulás után megtanulja, és onnantól modell nélkül is felismeri.
- **Melyik oldal hoz többet:** indulóoldalanként számolja, hány oldalt nézett, hány jelölt és jó találat jött belőle, mit vettél meg és mit rejtettél el; amelyik többet hoz, azt gyakrabban nézi, a többit ritkábban (de nem hagyja el).
- **Saját figyelés a jól menő termékekre:** naponta egyszer a gyorsan kelő termékekből (és amiből már többször volt jó találat, vagy megvetted) saját indulóoldal lesz (legfeljebb hat, azon az oldalon, ahol a legtöbbször látta); ami 15 betöltés után sem hozott semmit, azt leállítja.
- **A visszajelzésedből:** amit kétszer elrejtesz (és nem vettél meg), arról egy hónapig nem szól; ha egy ilyet mégis megveszel, újra szól.

**Napló.** A fül alján látod, mit csinált a nap folyamán, röviden, időponttal: a munkamenetek összefoglalóját („09:12–09:31 Hardverapró: 9 oldal (Videókártyák, Memóriák, SSD-k), 214 hirdetés, a modell 18 hirdetést nevezett meg; jó találat: … Utána: Jófogás.”), a jó találatokat, a kereséseket, a robot-ellenőrzést, és hogy mit tanult. A korábbi napokat (két hétig) a Nap választóval nézheted meg; a gépen marad (`%LOCALAPPDATA%\Jarvis\munka\kereskedes-naplo.json`).

**Modell-sor.** A kör alatt a böngésző és a modell párhuzamosan dolgozik: a modellhívások (a szokásos ár felmérése, a jelöltek ellenőrzése, a figyelt oldalak átnézése) egy külön sorban mennek, mindig csak egy egyszerre, miközben a böngésző a következő oldalt tölti. Ha közben a Jarvishoz szólsz, a kör abbamarad, és a félbemaradt jelöltek a következő körben jönnek.

**Megvettem és Vételeim.** Egy talált hirdetésnél a **Megvettem** gombbal megadod, mennyiért vetted, mennyi volt a posta, és hogy postán jön-e; a Jarvis felveszi az eladandók közé (a hirdetés címével, honnan vetted, a vételkori szokásos árral, és tervezett eladási árnak a szokásos ár 90%-ával: ennyiért kel el többnyire); csak be kell pipálnod, hova töltse fel, és fotót adnod hozzá. A Munka menü **Vételeim** fülén minden megvett tárgy egy kártya (telefonon is jól olvasható, egymás alatt): mibe került (vételár + posta + egyéb költség, pl. javítás, tisztítás), mennyiért adnád tovább, és mennyi a várható haszon (forintban és százalékban), hol tart (**Úton**, **Nálad**, **Fent van**, **Eladva**; a **Megérkezett** gomb egy mozdulat), hány napja vetted, és az árbázisból, hogy **most mennyiért adják** a hasonlókat (és ha tudni, milyen gyorsan kel). A **Részletek** gombbal mindent átírhatsz: vételár, posta, egyéb költség, honnan, mikor vetted, a szokásos ár, mennyiért adnád tovább, az eladáskor a posta és a díj (jutalék, kiemelés), egy megjegyzés (csomagszám, az eladó, egy apró hiba); eladás után az eladási ár és a nap. Az **Új vétel** gombbal azt is felveheted, amit nem az Automata kereskedés talált (pl. bolhapiacon vetted). Ha mégsem lett belőle vétel (visszamondtad, vagy az eladó mondta vissza), a még el nem adott tárgy kártyáján (és a találatok között a „Megvetted ✓” mellett) a **Visszamondva** gomb egy megerősítés után **mindenhonnan törli**: a Vételeim és az eladandók közül (a munka mappán belül a `_torolt` mappába kerül), a találatok és az azonnali figyelő friss hirdetései közül, a havi keretbe sem számít bele, a „megvett” jó pontot visszavonja, és erről a hirdetésről (leárazva sem) többé nem szól. A fül tetején négy csempe: a haszon **ebben a hónapban** és **összesen** (hány eladás, a megtérülés %-a, átlagosan hány nap alatt kelt el), mennyi pénz van **bent** a meg nem eladott tárgyakban, és mennyi a **várható haszon** belőlük; alatta havonta (az utolsó hat hónap), és szűrők (Úton, Nálad, Fent, Eladva). A haszon: az eladási ár, mínusz az eladás költségei (posta, díj), mínusz amibe került. Amíg nem kelt el, a várható haszon a tervezett eladási árral számol (ha az nincs, a hirdetési árral, végül a szokásos ár 90%-ával). Bármelyik tárgy lapján is megadhatod a **Vételár**-at (ha nem az Automata kereskedésből vetted): attól a Vételeim között is ott van, a vétel napja a mai. Az **Eladva** jelöléskor a Jarvis megjegyzi az akkori árat eladási árként (átírhatod, ha alkuval kevesebbért ment el). Az Automata kereskedés fülön a **Haszon** sor ennek a rövid összefoglalója.

**Begyakorolt lépések.** (A Vinteden a feltöltést és a levételt is a Jarvis maga csinálja modell nélkül, lásd fent; ott ez csak akkor jön, ha azt kikapcsolod, vagy az oldalt nem ismeri fel.) Amikor a modell egy oldalon sikeresen feltölt (vagy levesz) egy hirdetést, Jarvis megjegyzi a lépéseit, és legközelebb modell nélkül, emberi tempóban játssza vissza (lásd lent): ugyanazokat a mezőket és gombokat keresi meg, és a mostani tárgy adataival tölti ki. A választásoknál (márka, méret, állapot, szín) megjegyzi, melyik értékhez mire kellett kattintani; ha a felirat maga az érték (pl. „L” a méretnél), bármelyik értékre működik. Feltöltésnél a recept tárgyfajtánként és kategóriánként külön van, mert a kategória kiválasztása és az űrlap is ettől függ: egy új kategóriát a modell tanul meg egyszer. A közzététel előtt ugyanúgy jóváhagyást kér, és a kérdésben azt is látod, mit választott ki („Kategória: …; Állapot: …”). Ha a közzététel elsőre nem ment át (pl. hiányzott egy kötelező mező, és utána pótolni kellett), azt a lépéssort nem jegyzi meg, mert a visszajátszás a félkész űrlapnál kérne jóváhagyást. Ha valami nem stimmel (az oldal megváltozott, vagy olyan érték jön, amit még nem tanult meg), a közzététel **előtt** megáll (és hibacsomagot készít arról, hol akadt el), majd a modell csinálja végig, közben újratanul; két egymás utáni kudarc után elfelejti a régi receptet. Közzététel után soha nem kezdi újra (nehogy kétszer kerüljön fel), legfeljebb megnézeti a modellel, sikerült-e. Közzététel után kivárja az eredményt (a sikeres oldal címe vagy fejléce, vagy egy hibaüzenet), nem rögtön dönt. A felvételt megtisztítja (görgetés, visszalépett ágak, újragépelt mezők, kinyitott és bezárt listák), így egy kis kerülővel végigvitt menetből is lesz recept. Ha egy elemet nem talál (pl. átírták a feliratát), a modell egy rövid, számozott kérdéssel kiválasztja a helyét (egy menetben legfeljebb kétszer; a közzététel gombját soha). Egy még nem látott értéket (pl. eddig csak „Nagyon jó” állapotú tárgyat töltött fel, most „Jó” jön) a lehetőségek feliratai alapján választ ki, ugyanúgy, mint a `browser_choose` (a kattintható listaelemek közül is, a lista vezérlőgombjai nélkül), és csak ha így nem egyértelmű, kérdezi meg röviden a modellt; a választást megjegyzi. Ha így sem megy, a modell a félig kitöltött űrlapon folytatja. A hirdetésfeladás űrlapjának címét is megjegyzi (`munka\beallitasok.json`), és legközelebb közvetlenül azt nyitja meg. A receptek a `%LOCALAPPDATA%\Jarvis\munka\folyamatok` mappában vannak; ha egyet törölsz, a következő alkalommal újratanulja. Kikapcsolás: `[work] replay = false`.

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

Ez egy parancsikont tesz a Windows Indítópult mappájába (rendszergazdai jog nem kell). Bejelentkezéskor a Jarvis **készenlétben** indul: konzol- és Jarvis-ablak nélkül, betöltött modellek nélkül, a videókártya szabad. Közben fut a telefonos kapcsolat és a modell nélküli háttérmunka (figyelők, régóta fent lévő hirdetések), és ha elindítottad, az Automata kereskedés is (böngészés, azonnali figyelő): ehhez a böngésző elindul, a modell pedig egy-egy kérdés után csak 5 percig marad a videókártyán, utána kiürül.

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
- **Fent lévő hirdetések ellenőrzése** (`listings`): naponta legfeljebb `listing_checks` (5) fent lévő hirdetésed oldalát nyitja meg Jarvis saját böngészőjében, véletlen sorrendben, emberi tempóban (betöltés, egy pillantás, egy kis görgetés), és csak olvas: semmire nem kattint. Egy hirdetést naponta legfeljebb egyszer néz meg, a frisset csak egy nap múlva. Ha elkeltnek („Eladva”, „Foglalt”) vagy eltűntnek látszik (nem található, lejárt, máshova irányít), szól; az értesítés gombja a hirdetés Eladva ablakát nyitja meg (lásd Munka menü), a levételt ott hagyod jóvá. Ha egy oldal robot-ellenőrzést kér, vagy ki vagy jelentkezve, az egész kört abbahagyja, egyszer szól, és az értesítés **Bejelentkezés a böngészőmben** (captchánál **Megnyitás a böngészőmben**) gombja megnyitja az oldalt Jarvis böngészőjében: ott te lépsz be, a captchát sem oldja meg helyetted. Modell nem kell hozzá, de a böngésző igen (készenlétben nem fut). Kikapcsolás: `listing_checks = 0`, vagy az Automatikus munkák ablakban.
- **Vevői üzenetek:** `message_minutes` (120) percenként, amíg van fent hirdetésed a Vinteden, elolvassa az olvasatlan üzeneteket (csak olvas, semmit nem küld, és a böngésző kell hozzá, ezért készenlétben nem fut), és ha új jött, szól. Lásd a Munka menünél.

Az értesítések a naplóba kerülnek, az ablak alján az **Értesítések** gombbal is látod őket (a gombjaikkal együtt), és a telefon is lekérdezheti őket.

**Csak a fontos megy a telefonra.** A telefonra (és a laptopra) csak az jön, amivel teendőd van, vagy amiről tudnod kell: megérő hirdetés, egy figyelő új találata, vevő üzenete, elkeltnek látszó hirdetés, Jarvis kérdése egy tárgyról és a Pro feltöltés Nézd át ablaka, a feltöltés vagy levétel eredménye és a hibák, a szokásos ár jóváhagyása, a havi keret és a felhalmozódott tárgyak. A kevésbé fontosak csak az **Értesítések** listájában vannak meg, olvasottként (nem jeleznek, nem rezeg miattuk a telefon, és nem olvassa fel őket): ha egy oldal böngészés közben robot-ellenőrzést kért (holnap magától folytatja), ha az azonnali figyelő egy oldalon szünetel, ha a hirdetések ellenőrzése egy oldalon megállt, a „Változott az ár?” javaslat, és a régóta fent lévő hirdetések emlékeztetője.

Az **Automatikus munkák** gomb (az ablak alján) megmutatja, mit néz meg Jarvis magától: a figyelőket (mikor nézte utoljára, volt-e új találat), és a háttérmunkákat (milyen gyakran, mikor futott utoljára). Ami nem kell, azt itt törölheted (egyenként vagy mind), a háttérmunkákat pedig egyenként kikapcsolhatod; a kikapcsolás újraindítás után is megmarad. Ha valamikor egyáltalán nem szeretnél háttérmunkát (pl. pár óráig), a telefonról felfüggesztheted, vagy kapcsold ki: `[scheduler] enabled = false`.

## Telefon

A Jarvisnak van egy kis JSON-felülete (alapból a `127.0.0.1:8765`-ön), amit a saját appod a Tailscale-en át ér el: `tailscale serve --bg 8765`, és az app a gép `https://<gép>.<tailnet>.ts.net` címét használja. Párosítás a Jarvis ablakában (**Telefon** › **Új párosító kód**), eszközönkénti kulccsal, ami ugyanott vissza is vonható. A telefonról írhatsz és beszélhetsz (hangfelvételt küldesz, a Jarvis a gépen ismeri fel), követheted az élő naplót, jóváhagyhatod a közzétételt, fotót és tárgyat adhatsz fel, jelölheted, hogy elkelt (és leveheted a többi oldalról), elvetheted a háttérellenőrzés téves jelzését, elolvashatod a vevői üzeneteket (és jóváhagyással válaszolhatsz), szüneteltetheted és folytathatod a Jarvist, kezelheted a figyelőket és az értesítéseket.

A végpontok leírása: [docs/telefon-api.md](docs/telefon-api.md); kész Angular-szolgáltatás: [docs/angular/jarvis-api.service.ts](docs/angular/jarvis-api.service.ts).

## Telefonos és laptopos alkalmazás

A telefonon (Android-alkalmazás) és a laptopon (telepíthető ablak) ugyanaz a Jarvis jelenik meg, mint a gépen: mindhárom menü (Beszélgetés, Munka, Kreatív), ugyanazzal a fekete-fehér felülettel és ugyanazzal a beszélgetéssel. De **minden a gépen fut**: a modellek, a böngésző, a hirdetések feltöltése. A telefon és a laptop csak mutatja, és onnan adod a feladatot, így a laptopnak semmilyen modellt nem kell futtatnia. Ha a feladatot a telefonról vagy a laptopról adod, a gép nem olvassa fel a választ (te nem ott vagy).

**Egyszer kell beállítani:**

1. Tailscale a gépen, a telefonon és a laptopon is, ugyanazzal a fiókkal (ingyenes): <https://tailscale.com/download>.
2. A gépen PowerShellben: `tailscale serve --bg 8765` (ettől a gép a Tailscale-en belül HTTPS-en elérhető; az internetre nem nyílik ki). Ha azt írja ki, hogy ehhez előbb be kell kapcsolni valamit, nyisd meg a kiírt linket; vagy a Tailscale admin felületén a **DNS** oldalon kapcsold be a **MagicDNS**-t és a **HTTPS Certificates**-t, majd futtasd újra. A gép címe: `https://<gép-neve>.<tailnet>.ts.net`; ha a serve be van állítva, a Jarvis ablakában a **Telefon** gomb pontosan kiírja. Az első megnyitás egy percig is eltarthat, amíg a tanúsítvány elkészül.
3. A gépen, a Jarvis ablakában: **Telefon** › **Új párosító kód** (6 számjegy, 5 percig érvényes). Ha a serve be van állítva, mellette egy **QR-kód** is megjelenik: a telefon kamerájával (vagy a gyorsbeállítások QR-olvasójával) beolvasva megnyílik a gép címe a kóddal, és ott egy gombnyomással párosíthatsz, az alkalmazásban vagy a böngészőben; begépelni semmit nem kell. Minden eszközt egyszer kell párosítani; ugyanitt bármikor vissza is vonhatod.

**Telefon (Android):** töltsd le az alkalmazást a telefonon: <https://github.com/ZsoleszDev/jarvis/releases/download/android-latest/jarvis.apk> (vagy nyisd meg a gép címét a telefon böngészőjében: a belépő oldalon ott a letöltés linkje), és telepítsd (a telefon megkérdezi, engedélyezed-e a telepítést ebből a forrásból). Indítás után add meg a gép címét és a párosító kódot. Az alkalmazás akkor is szól, ha be van zárva:
- **jóváhagyás kell** (pl. közzététel előtt): az értesítés **Engedélyezem** / **Mégse** gombjával ott helyben válaszolhatsz;
- **kész vagy elakadt egy feltöltés**, **kérdez valamit egy tárgyról** (pl. a színét), egy figyelő **új találatot** hozott, régóta fent lévő hirdetés;
- egy hirdetés **elkeltnek látszik** valahol: az értesítés **Eladva itt, vedd le a többiről** gombja megnyitja az alkalmazást a hirdetés Eladva ablakánál (előre kiválasztva); a levétel csak az ottani **Rendben** után indul.

Ehhez egy csendes, állandó értesítés jelzi, hogy kapcsolatban van a géppel, vagy hogy épp nem éri el (a telefon beállításaiban elrejthető), és az energiatakarékos módból ki kell venni az alkalmazást (az első párosításkor megkérdezi). Az állandó értesítés két gombja: **Hang** (hangutasítás), és **Szünet** / **Folytatás** (a gépen minden modell leáll, pl. játékhoz, és újra betöltődik). A készenlét (amikor a modellek épp nincsenek betöltve, de a háttérmunka megy, és az első kérés felébreszti) nem szünet: az állandó értesítés ettől nem változik.

A **Hallgass meg** gomb a telefon mikrofonjával vesz fel, a felismerés a gépen történik; ha befejezted a mondatot, kb. két másodperc csend után magától elküldi (a gombbal előbb is). Hangutasítás egy érintéssel, az alkalmazás megnyitása nélkül:
- **parancsikon:** az alkalmazás ikonját hosszan nyomva **Hang** (vagy **Új hirdetés**); a kezdőképernyőre is kihúzhatod;
- **gyorsbeállítás csempe:** a lehúzható sávban a ceruzával add hozzá a **Jarvis: hang** csempét;
- az állandó értesítés **Hang** gombja.

**Megosztás a Jarvisnak:** a galériában jelöld ki a fotókat, **Megosztás** › **Jarvis**: az új hirdetés fotóiként jelennek meg a Munka menüben, csak le kell írnod, mit adsz el, és be kell pipálnod, hova kerüljön (a feltöltést a gép végzi). A HEIC-fotókat az alkalmazás JPEG-gé alakítja. Egy linket vagy szöveget (pl. egy hirdetést a böngészőből) megosztva a beszélgetésbe kerül, és elküldés előtt még kiegészítheted. Új változat: ugyanerről a linkről; ha a telepítés ütközést jelez, előbb távolítsd el a régit (lásd [android/README.md](android/README.md), saját aláíró kulccsal ez nem kell).

**Laptop (Windows):** nyisd meg az Edge-ben a gép címét (`https://<gép-neve>.<tailnet>.ts.net`), és párosítsd a kóddal. Utána a címsorban az **Alkalmazás telepítése** ikonnal (vagy ⋯ › Alkalmazások › A webhely telepítése alkalmazásként) saját ablakos alkalmazás lesz belőle, a Start menüben **Jarvis** néven. Az ablak alján az **Értesítések bekapcsolása** gombbal kérheted, hogy szóljon, ha kész egy feladat, vagy jóváhagyás kell; **akkor is, ha az ablak be van zárva** (Web Push: a gép a Microsoft push-szolgáltatásán át küldi, titkosítva, csak az Edge tudja kiolvasni). A jóváhagyásnál az értesítés **Engedélyezem** / **Mégse** gombjával ott helyben válaszolhatsz; egy elkeltnek látszó hirdetés értesítésének **Eladva itt, vedd le a többiről** gombja pedig megnyitja a Jarvist a hirdetés Eladva ablakánál (a levétel ott, a **Rendben** után indul). Ehhez a gépen a `cryptography` csomag kell (benne van a `requirements.txt`-ben; ha korábban telepítettél: `pip install -r requirements.txt`), és az Edge-ben maradjon bekapcsolva: Beállítások › Rendszer és teljesítmény › „A bővítmények és alkalmazások futtatásának folytatása a Microsoft Edge bezárásakor” (alapból be van kapcsolva). A **Hallgass meg** itt a laptop mikrofonját használja.

Távolról nem lehet: a Jarvist leállítani, új eszközt párosítani, és a gép mikrofonját használni; ezekhez a gép előtt kell ülni.

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
Cím: https://www.hasznaltauto.hu/…
Hibaüzenetek: A keresőmező nem lehet üres.

Látható szöveg:
Keresés Üzemanyag Skoda Octavia 1.6 CR TDI Style 4 290 000 Ft …

Kezelhető elemek (a számukkal hivatkozz rájuk):
[3] gomb "Elfogadom"
[4] keresőmező "Márka, modell"
[5] lenyíló lista "Évjárat" (kiválasztva: "Mindegy"; lehetőségek: Mindegy, 2018, 2019)
Üzemanyag (választógombok): [6] Benzin ✓, [7] Dízel
*[8] link "Skoda Octavia 1.6 CR TDI Style · 4 290 000 Ft" -> /szemelyauto/skoda/…
```

Az oldalnézet tömör (egy választógomb-csoport egy sor, a bejelöltet ✓ jelzi), egy oldalon belül az elemek száma nem változik a lépések között, a `*` az előző nézet óta megjelent elemet jelöli, a hibaüzenetek (pl. egy kötelező, de üres mező) külön sorban állnak, és egy hirdetéskártya linkje mellett ott az ára is.

A modell a számokra hivatkozva kattint (`browser_click`), gépel (`browser_type`), egy űrlap több mezőjét egy lépésben kitölti (`browser_fill`), görget, visszalép, és el is olvassa az oldalt (`browser_read`). Egy saját (nem natív) lenyíló listából a `browser_choose` választ: kinyitja, kereshető listánál beírja, megkeresi és rákattint, fánál szintenként („Nők > Ruházat > Pólók”), és ha nyitva marad, bezárja; ez egy modellkör a négy-öt kattintás helyett. A `browser_do` legfeljebb öt lépést tesz meg egy körben ugyanazon az oldalon (kattintás, gépelés, választás, jelölés); ha közben új oldal vagy felugró ablak jön, megáll, és megmondja, mi maradt ki, kockázatos gomb (pl. közzététel) pedig csak az utolsó lehet. Ez a szöveges nézet pontosabb és gyorsabb, mintha képről kellene kitalálnia, hová kattintson. A régebbi oldalnézetek helyén egy rövid sor marad (mi történt, melyik oldalon), hogy ne teljen be a kontextus.

Webshopokban (Vinted, Jófogás, eMAG) fontos részletek:
- Az oldalnézetben a **fő tartalom** (termékek, szűrők, kereső) előrébb kerül, a fejléc és lábléc linkjei hátrébb, és legfeljebb a lista harmadát foglalhatják el.
- A Jarvis **kivárja a betöltődést**: amíg az oldal adatot tölt, változik, vagy „Betöltés…” felirat látszik rajta, nem készít oldalnézetet; ha megnyugodott, rögtön folytatja (a reklámok és a mérőkódok kérései nem tartják fel).
- A rendszerprompt szerint a modell az oldal **saját keresőjét és szűrőit** használja, nem találgat webcímet. Ha három-négy próbálkozás után sem jut előre, megáll, és elmondja, meddig jutott. Ha ugyanaz a lépés nem változtat semmit az oldalon, a második hiábavaló ismétlésnél a rendszer is szól neki, hogy próbáljon mást, a negyediknél pedig abbahagyja (az új elemek csillaga az oldalnézetben ebben nem számít változásnak).
- Egy hosszú lenyíló listából (pl. 150 kategória) az oldalnézet az első 12 lehetőséget mutatja; feltöltés és levétel közben elöl azokat, amelyek a tárgy szavaival (kategória, márka, állapot, szín, település…) egyeznek.
- A hirdetéskártyák linkje mellett az ár is ott van: akciónál a mostani (akciós ár, villámár), nem az áthúzott régi, ugyanúgy, ahogy az árkutatás is számol.
- Ha az oldal **robot-ellenőrzést** (captcha) kér, szól, hogy oldd meg a böngészőablakban. Csak a látható, megoldandó captcha számít (a háttérben betöltött, láthatatlan reCAPTCHA nem). Ha nem vagy otthon: feltöltésnél vagy levételnél a telefonra is jön egy értesítés **Megoldom a telefonon** gombbal. Ez megmutatja a Jarvis böngészőjének képét, és ahova böksz, oda kattint a gépen, így a pipát vagy a képes feladatot te oldod meg távolról (a Jarvis magától soha nem kattint a captchára). Utána az **Újra** gombbal folytatja. A valódi Edge-ben ez ritkábban fordul elő, mint a Playwright Chromiumában.

A profil megőrzi a sütiket: ha egyszer belépsz valahova a Jarvis böngészőjében, legközelebb is bejelentkezve látja (lásd: Bejelentkezés).

**Látás.** A Qwen3.8 képet is ért. A `browser_look` lefényképezi a böngésző oldalát (grafikonok, képek, elrendezés), a `look_at_screen` a monitorodat („mi ez a hibaüzenet?”). A képet a futó beszélgetésben kapja meg a modell (látja, mit csinált eddig, és a gyorsítótár is megmarad); ha a kép már nem férne bele a kontextusba, egy külön, egyszeri kérdésben.

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
- **Vinted modell nélkül.** A Vinted űrlapját a Jarvis maga tölti ki; a modell csak a kategóriafában választ egy számot. A levételhez (Törlés, megerősítés) a modell nem is kell (lásd a Munka menünél).
- **Begyakorolt lépések.** Egy már megtanult feltöltés vagy levétel modell nélkül fut (lásd a Munka menünél).
- **Az első lépést a Jarvis teszi meg.** Egy feltöltésnél vagy levételnél a hirdetésfeladást (a hirdetést) a Jarvis maga nyitja meg, és a modell már az oldalnézettel kezd: egy modellkör megspórolva.
- **A szünet a közzététel előtt.** A két közzététel közti pár perc szünet a közzététel gombja előtt jön, nem az űrlap megnyitása előtt: az űrlap kitöltése addig is halad.
- **Több oldal egy lépésben** (`web_research`): oldalanként egy modellkör helyett egy.

- **„Egy pillanat.”** Ha lassú lépés jön (keresés, böngészés, fájlkeresés), Jarvis azonnal mond valamit, így nem érzed, hogy lefagyott.
- **Gyorsítótár-barát előzmények.** Az Ollama megjegyzi az előző kérés elejét, és csak az új részt számolja. Ezért egy kérésen belül végig ugyanaz az eszközlista (a feltöltésnél és a levételnél csak a böngésző és a tárgy eszközei), a régi oldalnézet helyére rögtön egy rövid sor kerül (`[assistant] keep_page_views`; egy feltöltés vagy levétel első üzenetében lévő nézet helyére is, a feladat és a hirdetés adatai maradnak), az előzmények elejét pedig ritkán, egyszerre a felére vágja. Melegíteni is módonként melegít (a Munka menüre váltáskor, ébredéskor, Szünet után), az eddigi előzményekkel együtt.
- **Rövid válaszok.** Egy modellkör legfeljebb 2048 (a Munka menüben 1024) tokent írhat, a JSON-kérdések még kevesebbet; ha egy eszközhívás félbemaradna, egyszer újrakéri.
- **Elakadásfigyelés.** Ha a modell ugyanazt a lépést ismétli, és az oldal nem változik, szól neki (az új elemek csillaga nem számít változásnak), a negyedik után pedig abbahagyja, és elmondja, hol akadt el (nem égeti el a lépéskeretet).
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
- `[stt] enabled = false`: csak gépelés, mint a `--text` kapcsoló, de az automatikus indításnál is érvényes. Nem tölti be a beszédfelismerőt (a „turbo” modell kb. 1,6 GB VRAM), így gyorsabban indul, és több hely marad a nyelvi modellnek. A mikrofon és az ébresztőszó sem fut, és a telefonról küldött hangüzenetre azt írja, hogy küldd el szövegként.
- `[stt] device`: `"auto"` (GPU, ha lehet), `"cuda"` vagy `"cpu"`.
- `[stt] gpu_device_index`: az `nvidia-smi` sorszáma. Nálad 0 = RTX 3060 Ti, 1 = RTX 5060 Ti.
- `[stt] initial_prompt`: gyakori szavak, nevek, így jobban felismeri őket.
- `[audio] input_device`: a mikrofon sorszáma vagy nevének része (a `check_setup.py` kilistázza). A multiseat miatt érdemes konkrétan megadni.
- `[audio] min_threshold`: ha nem veszi észre, hogy beszélsz, csökkentsd (pl. `0.005`).
- `[tts] voice`: `hu_HU-imre-medium` (férfi), `hu_HU-anna-medium` vagy `hu_HU-berta-medium` (női).

**Felület és ébresztőszó**
- `[gui] window`: alkalmazásablakban nyíljon meg (Edge `--app` mód, címsor nélkül). `exit_on_close`: az ablak bezárásával a Jarvis is leáll (készenléti indításnál nem). `gpu_bars`: a videókártyák terhelése az ablak tetején (kártyánként egy vékony sáv).
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
- `[work] fast_fetch`: a Hardverapró és a Jófogás böngésző nélkül is (gyorsabb, párhuzamos; erre épül az azonnali figyelő). Alapból be; ha egy oldal tilt, ott magától a böngésző marad.
- `[work] temperature`: a Munka menü beszélgetésének mintavétele (0,3; a Beszélgetésé az `[llm] temperature`): kisebb érték = kevesebb véletlen az űrlapkitöltésnél. A modellt nem tölti újra.
- `[work] autofill`: a Vinted űrlapját a Jarvis maga tölti ki, a modell csak a kategóriát választja ki; a Vinted-levételt is modell nélkül csinálja (alapból be).
- `[work] publish_gap_minutes`: ugyanazon az oldalon két közzététel (vagy levétel) között legalább ennyi perc teljen el (2, véletlenszerűen ±30%; 0 = nincs várakozás).

**Háttérmunka és telefon**
- `[scheduler] enabled`: háttérmunka be/ki. `quiet_minutes`: ennyi ideig nem indul háttérmunka a legutóbbi beszélgetés után (5). `active_hours`: pl. `"08:00-20:00"`, üresen bármikor, amíg a Jarvis fut. `watch_minutes`: a figyelők gyakorisága (60). `stale_days`: ennyi nap után szól a régóta fent lévő hirdetésről (14, 0 = soha). `listing_checks`: naponta ennyi fent lévő hirdetésed oldalát nézi meg, csak olvasva (elkelt-e, eltűnt-e; 5, 0 = soha). `message_minutes`: ennyi percenként nézi meg a vevői üzeneteket (120, 0 = soha); `message_reads`: egy körben legfeljebb ennyi olvasatlan beszélgetést nyit meg (3). `speak`: a háttérmunka eredményét fel is olvassa.
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
- `[assistant] keep_page_views`: hány legutóbbi oldalnézet marad meg teljes egészében (1); a régebbiek helyén egy rövid sor (mi történt, melyik oldalon).
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
- **Vevői üzenetek:** olvasáskor semmire nem kattint és semmit nem küld (a beszélgetéseket a linkjükkel nyitja meg). Üzenetet csak te küldhetsz, a felület (vagy a telefon) Küldés gombjával, és a küldés előtt mindig rákérdez, pontosan azzal a szöveggel, ami elmegy; a modellnek nincs küldő eszköze. A vevők szövege adat, nem utasítás: a vázlatnál a modell ezt kapja meg, és személyes adatot vagy az oldalon kívüli fizetést nem ajánl fel. A beszélgetések csak a gépeden vannak (`munka\uzenetek.json`).
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
| „A nyelvi modell (Ollama) futás közben leállt. Újraindítom…”, „Ollama-hiba (500): model runner has unexpectedly stopped” vagy „error reading llama-server response … kényszerítetten bezárta” | Az Ollama modellfolyamata futás közben leállt; a Jarvis egyszer magától újrapróbálja (az Ollama közben 20–40 másodperc alatt újratölti a modellt), és ott folytatja, ahol tartott. Pár másodperc múlva azt is kiírja, mi volt az ok az Ollama naplója (`%LOCALAPPDATA%\Ollama\server.log`) szerint, és mit tehetsz ellene. A leggyakoribb ok: **elfogyott a gép memóriája** (a naplóban `std::bad_alloc`). Az újabb Ollama beépített llama-servere a beszélgetések elejét a gép memóriájában is megőrzi (prompt-gyorsítótár, alapból akár 8 GB, a hibrid modelleknél ellenőrzőpontokkal), és menüváltáskor (Beszélgetés ↔ Munka) egyszerre több gigabájtot foglal; a böngésző és a többi program mellett ez már nem mindig fér el. Javítás (egyszer kell): PowerShellben `setx LLAMA_ARG_CACHE_RAM 0` és `setx LLAMA_ARG_CTX_CHECKPOINTS 4`, majd lépj ki az Ollamából a tálcán (jobb klikk az ikonjára), és indítsd újra. Utána menüváltáskor az első válasz pár másodperccel tovább tarthat (újra fel kell dolgoznia a beszélgetés elejét). Memóriahibánál a Jarvis azt is kiírja, mennyi a gép memóriája (a RAM és a **véglegesített** memória: RAM + lapozófájl), melyik program foglalja a legtöbbet, és hogy a futó Ollama tényleg a beállítással indult-e (ha a `setx` után nem indítottad újra, szól). Ha a Windows-on nincs lapozófájl, vagy kicsi, a programok a RAM-on felül nem kapnak memóriát: állítsd automatikusra (Win+R → `SystemPropertiesPerformance` → Speciális → Virtuális memória: Módosítás → „A lapozófájl méretének automatikus kezelése minden meghajtón”, majd indítsd újra a gépet); erre a Jarvis indításkor is figyelmeztet. Ha a napló szerint a videókártya memóriája fogyott el, zárd be a kártyát használó programokat, vagy vedd kisebbre a kontextust. |
| „Target page, context or browser has been closed”, „Playwright connection closed”, vagy a konzolon „FATAL ERROR: Zone Allocation failed - process out of memory” | A Jarvis böngészője, vagy a Playwright-meghajtója (`node.exe`) leállt, többnyire azért, mert elfogyott a gép memóriája (lásd az előző sort). A Jarvis a következő lépésnél magától újraindítja a böngészőt (a bejelentkezések megmaradnak), és szól a modellnek, hogy az előző oldal elveszett. Ha gyakori, a memóriát kell rendbe tenni (lapozófájl, a fölösleges böngészőlapok, az Ollama beállításai). |
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
| A Vinteden rossz kategóriát választott | A jóváhagyásnál látod, mit választott: nyomd a Mégse gombot, írd át a hirdetés lapján a kategóriát a Vinted szavaival (pl. „Women > Clothing > Jeans > Skinny jeans”), és **Újra**: így a modell nem is kell hozzá. Egy rossz megjegyzett választást a `munka\valasztasok\vinted.json` törlésével felejt el. |
| „A modell nélküli kitöltés most nem megy” | A Jarvis nem ismerte fel a Vinted űrlapját (pl. az oldal megváltozott); ilyenkor a modell csinálja végig, mint eddig. A hibacsomagban (`info.json` › `kitoltes`) látod, meddig jutott és mit döntött. |
| „A modell nélküli levétel most nem megy” | A Jarvis nem találta meg a hirdetést (se a webcímén, se a profilodon, se a keresőben), vagy a hirdetés oldalán a Törlés gombot; ilyenkor a begyakorolt lépések vagy a modell folytatja (ha a hirdetést megtalálta, annak az oldaláról). A hibacsomagban (`info.json` › `kitoltes`) látod, hol kereste. Ha a Vinted kijelentkeztetett, jelentkezz be a böngészőablakában (Alapadatok és bejelentkezés › Vinted), és **Újra**. |
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

743 teszt; Ollama, mikrofon, GPU és internet nélkül is lefutnak (hamis, folyamatosan válaszoló Ollama-szerverrel, hamis hangkártyával és ébresztőszó-modellel, helyi tesztoldalakkal). Köztük egy regressziós teszt a „no user query found” hibára: egy hamis Ollama ugyanúgy levágja a túl hosszú beszélgetést, mint a valódi. A hamis Ollama azt is el tudja játszani, hogy a modell egy része nem fér a videókártyákra, így a kisebb kontextusos újratöltés is tesztelve van. Az Adás/Vételhez helyi „hirdetési oldalak” vannak: rajtuk a Jarvis végigcsinálja az egészet (visszakérdezés, árellenőrzés, fotók, mezők, jóváhagyás, megjelölés, több oldal sorban), a felületen is. A valódi Vinted, Jófogás, Vatera és Hardverapró oldalain ez nincs kipróbálva, mert azok változnak, és a tesztek nem mehetnek ki a netre. A Vinted modell nélküli kitöltését egy helyi oldal teszteli, ami a Vinted űrlapját utánozza, azokkal az elemekkel, ahogy egy valódi feltöltés hibacsomagjában látszottak (kategóriafa catalog-azonosítókkal, „Search brands”, „EU” → „EU 40”, condition-2, színek „Done” gombbal, package-size-1, Upload → /member/…): a teljes kitöltés úgy, hogy a modell csak a kategóriaszinteken és a méretrendszernél választ egy számot, a megjegyzett kategória (a második póló már modell nélkül), az ismeretlen márka (a modell a nyitott űrlapon folytatja), a közzétételkor hiányolt szín (ellenőrzés, és az item_post tiltva), a bejelentkezést kérő oldal, az elutasított közzététel és az ismeretlen oldal (a régi út). A modell nélküli levételt helyi oldalak tesztelik, amik a Vinted hirdetésoldalát utánozzák (oldalsáv Bump, Mark as sold, Hide, Edit listing és Delete gombbal, a Delete után megerősítő ablak, törlés után átirányítás a profilra): a megjegyzett webcímen, a feltöltés utáni profilcímről (a profilon megtalálja a hirdetést, és megjegyzi a címét), a keresőből (egy másik eladó azonos című hirdetését átugorja, és a hirdetésed oldaláról megjegyzi a profilod címét), kijelentkezve (megáll, és szól), a már nem létező hirdetés („Levéve (már nem volt fent)”), az ismeretlen elrendezés (a modell a hirdetés oldaláról folytatja, nem töröl egy azonos című másik hirdetést, és az item_delist nem kezdi újra a levevőt), és a beszélgetésből kért levétel (az item_delist is a levevővel veszi le); a feltöltésnél pedig, hogy a profiloldal helyett a hirdetés saját webcímét jegyzi meg. A begyakorolt lépéseket is egy helyi hirdetési oldalon tesztelik: a hamis modell egyszer végigcsinálja a feltöltést (süti-ablak, fotók, mezők, kategória, állapot, márka-kereső, közzététel) és a levételt, a következő tárgynál pedig a Jarvis modell nélkül csinálja, és ellenőrzik, hogy minden mezőbe a második tárgy adatai kerültek. A vevői üzeneteket egy helyi oldal teszteli, ami a Vinted üzenetoldalát utánozza (olvasás kattintás nélkül, legfeljebb N beszélgetés egy körben, kijelentkezett és captchás oldal, a felismerhetetlen beszélgetés, a vázlat legkisebb ára, a küldés jóváhagyással, elutasítva semmi nem megy el), a fotóellenőrzést mesterséges próbaképek (éles és elmosott minta, sötét, kiégett, ellenfényes, kicsi, ismétlődő, világos tárgy fehér háttér előtt), a hirdetések háttérellenőrzését helyi hirdetésoldalak (fent van, eladva, foglalt, törölve, máshova irányít, captcha). A böngésző nélküli olvasást a Hardverapró és a Jófogás kártyáit utánzó helyi oldalak és egy helyi szerver tesztelik (a kártyák kiolvasása a fejléc, a menü és az eladó linkje nélkül, az áthúzott régi ár, a szerkesztett adat, a rejtett szöveg, az ékezetek; oldalanként egy kérés szünettel, a 403-as, a 429-es és a robot-ellenőrző oldal, az óránkénti keret, a más domainre mutató cím, és hogy háromszor üres oldal után a böngésző jön), az azonnali figyelőt hamis letöltésekkel (az első letöltés csak megjegyez, az új és a leárazott hirdetés, a csak megérő mód a hirdetés oldalának átnézésével, a több új egy értesítésben, a tiltás egyszeri jelzése, a Szünet, az üzemidő, a felfüggesztés és a saját szál), az automata kereskedés körét pedig úgy, hogy a gyors oldalak a háttérben, a böngésző közben a Vintedet tölti. A Vételeim számításait (költségek, várható és tényleges haszon, tippek) külön tesztek nézik, a fület pedig telefonméretű Chromiumban játsszák végig (összesítő, szűrők, Megérkezett, szerkesztés, új vétel, vízszintes görgetés nélkül). A telefonos kapcsolat tesztjei valódi helyi szervert indítanak (párosítás, kulcs, CORS, beszélgetés, események, Szünet, tárgyak és fotók). Az emberi tempót egy helyi oldalon mérik (billentyűleütések, egérmozgás a kattintás előtt, a gombnyomás hossza, a szünetek a begyakorolt lépéseknél, a várakozás két közzététel között), a hibacsomagot elakadó feltöltésekkel, a közzététel utáni ellenőrzést egy félbemaradt modellválasszal (megnézeti, fent van-e, és nem tölti fel kétszer), a böngésző hálózatfigyelését egy lassú adatkéréssel és 300 apró kéréssel (kivárja őket, de nem marad utánuk semmi a memóriában), a begyakorolt lépések felvételét egy valódi Vinted-feltöltés lépéseivel (a méret és az ár nem ég bele más választásokba), az Ollama modellfolyamatának leállását egy hamis Ollama-hibával (újrapróbálás, a hiba helye és szövege), a készenlétet pedig a valódi `jarvis.py`-val is: indulás modellek nélkül, második indítás, felébresztés kéréssel, `--quit`, és a konzol nélküli (naplófájlos) indulás. A webes kutatás javításait egy helyi, 403-at adó „webshoppal” és egy robot-ellenőrző köztes oldallal tesztelik (böngészős olvasás külön lapon, a blokkoló oldal megjegyzése, a kieső oldal pótlása, a captcha meg nem kerülése, a bezárt böngésző újraindítása, az ismételt hívások kiszűrése és a válasz a lépéskeret végén). A Kreatív menüt mindegyik kiindulóponttal, mindkét nyelven, hamis modellel tesztelik: a prompt felépítése (lényeg, kártyák, szerzői megjegyzés, emlékek, keret), az élő írás, a másik változat, a megszakítás (a félbemaradt mondat nélkül), az összefoglalás és a sűrítés, az ötletből készülő történet, a végpontok, és egy böngészős végigjátszás a felületen. A GPU-sávokat egy hamis NVIDIA-driverrel (NVML) és hamis kártyákkal tesztelik (a régi driver memórialekérdezése, a mérés leállása, ha senki sem nézi, a görbe kirajzolása és a pontos értékek az egér alatt), az Automatikus munkák ablakot a felületen (figyelő törlése jóváhagyással, a háttérmunka kikapcsolása, ami újraindítás után is megmarad), az Ollama-leállás okát egy hamis Ollama-naplóval (csak az új sorok számítanak, a részletes javaslat egyszer jön), a memória-diagnózist hamis folyamatlistával és hamis Windows-beállításokkal (lapozófájl, a futó Ollama környezete), a böngésző újraindítását pedig úgy, hogy a teszt megöli a böngészőt, illetve a Playwright-meghajtót. A telefonos és laptopos ablakot a telefonos kapcsolat valódi szerverén tesztelik (belépés párosítással, a felület kérései és az élő események továbbítása, a tiltott műveletek, hang és fotó a telefonról, a telepíthető alkalmazás kellékei, a gép címe a Telefon ablakban egy hamis `tailscale serve status` kiírásából, a QR-kódos párosítás), és telefonméretű Chromiumban végig is játsszák. A QR-kódolót egy ismert kódoló mátrixaival vetik össze, a laptopos értesítést (Web Push) úgy, hogy a teszt a böngésző oldaláról visszafejti a gép titkosított üzenetét, és ellenőrzi az aláírását (a jóváhagyás az értesítés gombjával is, egyszer használható kulccsal). Az Android-alkalmazás kéréseit (megosztott fotók az új hirdetéshez, megosztott link, hangutasítás) egy hamis alkalmazás-híddal játsszák végig Chromiumban, a felvétel csend utáni leállását pedig egy hangfájllal, ami a mikrofon helyett szól. Az Android-alkalmazást a GitHub Actions fordítja. A böngészős és a felület-tesztek valódi Chromiumot indítanak láthatatlanul; ha a `playwright install chromium` még nem futott le, ezek kimaradnak.

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
  ui/app.js            varázsszem, napló, menük, kezelőszervek, az Értesítések ablak
  ui/work.js           Adás/Vétel: új hirdetés, kérdések, alapadatok, a hirdetések és a lapjuk
  ui/creative.js       Kreatív: történetek, új történet, élő írás, a körök, az Emlékezet panel
  creative.py          Kreatív menü: történetek, promptok (angol és magyar), kontextus, emlékezet, összefoglalás
  creative_templates.py a kész kalandok és karakterek, angolul és magyarul
  work.py              a Munka menü: az eladás és a levétel lépései, eszközök a Munka menü beszélgetéséhez
  selling.py           tárgyak, fotók, az oldalak és amit kérnek, adatok a leírásból, árkutatás
  photos.py            fotóellenőrzés modell nélkül: homályos, sötét, kicsi és ismétlődő fotók
  replay.py            begyakorolt lépések: felvétel, recept, visszajátszás modell nélkül
  vinted.py            a Vinted űrlapjának kitöltése (a modell csak a kategóriát választja ki) és a levétel, modell nélkül
  messages.py          vevői üzenetek: olvasás modell nélkül, válaszvázlat a tárgy adataiból, küldés jóváhagyással
  humanize.py          emberi tempó a böngészőben: egérút, gépelési ritmus, szünetek
  dossier.py           hibacsomag: képernyőkép, oldalnézet, lépések, ha egy feltöltés elakad
  scheduler.py         háttérmunka: mikor ér rá a Jarvis, elhalasztás, megszakítás
  watches.py           figyelők (mentett keresések)
  deals.py             automata kereskedés: keresések, szokásos ár, jelöltek, figyelt oldalak, böngészés, Vételeim összesítése
  pricebook.py         árbázis: termékkulcs a címből, szokásos ár, eladási sebesség
  feeds.py             böngésző nélküli olvasás (Hardverapró, Jófogás): letöltés tempóval, tiltáskezelés, a találatok elemzése
  instant.py           azonnali figyelő: az új hirdetések pár percenként, saját szálon, értesítéssel
  listings.py          a fent lévő hirdetések háttérellenőrzése (elkelt, eltűnt), csak olvasva
  notices.py           értesítések és a gombjaik (a telefonnak is)
  api.py               telefonos kapcsolat: párosítás, kulcsok, JSON-végpontok, eseménynapló, a távoli ablak
  remote.py            a Jarvis ablaka a telefonon és a laptopon: belépés, továbbítás a géphez, telepíthető alkalmazás
  qr.py                QR-kód a párosításhoz (saját kódoló, külső csomag nélkül)
  webpush.py           értesítés a laptopra bezárt ablaknál is (Web Push: titkosítás, VAPID, feliratkozások)
  config.py            beállítások betöltése; az adatmappa (%LOCALAPPDATA%\Jarvis) és a régi adatok áthozása
  instance.py          egyszerre egy Jarvis: zárolás, a futó ablakának címe, leállítás (--quit)
  autostart.py         indulás a gép bekapcsolásakor (parancsikon az Indítópultban)
  ui_console.py        konzolos felület
  wakeword.py          „Hey Jarvis” ébresztőszó
  assistant.py         beszélgetés, eszközhívási ciklus, kontextuskezelés
  llm.py               Ollama REST kliens (szöveg és kép), betöltés úgy, hogy elférjen a videókártyákon
  gpu.py               videókártya-adatok (NVML vagy nvidia-smi), a GPU-sávok mérése
  ollama_log.py        az Ollama naplója: miért állt le a modellfolyamat, és mit lehet tenni ellene
  memory.py            a gép memóriája: RAM, véglegesített memória, lapozófájl, ki foglalja, a futó Ollama beállításai
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
                       folyamatos válasz, begyakorolt lépések, Vinted-kitöltő és -levevő, háttérmunka és Szünet, telefonos kapcsolat,
                       emberi tempó, hibacsomag, készenlét, hibatűrés, webes kutatás, Kreatív menü, vevői üzenetek)
docs/                  telefon-api.md (a telefonos végpontok), angular/jarvis-api.service.ts (kész Angular-szolgáltatás)
android/               az Android-alkalmazás (Java, a GitHub Actions fordítja: .github/workflows/android.yml)
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
