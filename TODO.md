# TODO – Adás/Vétel

## Kész (ezen az ágon, összefésülve, tesztelve)
- Vinted-feltöltés és -levétel modell nélkül; gyorsabb oldalnézet, `browser_choose`, `browser_do`.
- A négy félbemaradt csomag (loop, replay, flow, trade) összefésülve, a `wip-patches/` mappa törölve.
- Magyar ár mindenhol („ötezer”, „4,5 ezer”, „3 és fél ezer”), a tárgy szavai a böngészőnek (hosszú listák), elakadásfigyelés, tömörítés az első üzenet oldalnézetére is.
- Felület: ár- és címelcsúszás, háttérellenőrzés eredménye, „Nem igaz, fent van” (csak ugyanarra a jelre), az árkutatás részletei, „N napja fent”.
- Értesítésből egy gombnyomásos levétel (asztali Értesítések ablak, Web Push, Android) – mindig a megerősítő ablakon át.
- Vevői üzenetek: olvasás modell nélkül, válaszvázlat a tárgy adataiból, legkisebb elfogadható ár, küldés csak jóváhagyással.
- Fotóellenőrzés: homályos, sötét, túl világos, ellenfényes, kicsi, ismétlődő (a tárgyon belül és más tárgynál), borítójavaslat.
- Automata kereskedés: keresések szokásos árral, Vinted-ajánlat, Vatera-licitek, haszonkövetés; Felfedező (figyelt oldalak keresőszó nélkül), árbázis termékkulccsal, „Mennyit ér?”, eladási sebesség, esti leállás a liciteknél; böngészés napközben (munkamenetek, oldalanként pihenő, rögtön oldalváltás; készenlétben is), öntanulás, napló.
- Természetesebb hirdetéscím (márka + termék, szín és méret nélkül) és változatos zárómondat.
- Vételeim fül: vételár, posta, egyéb költség, honnan, mikor, úton/nálad/fent/eladva, tervezett eladási ár, eladási posta
  és díj, megjegyzés; várható és tényleges haszon, megtérülés, havonta; a „most kb.” ár az árbázisból; Új vétel kézzel.
- Böngésző nélküli olvasás (feeds.py) a Hardveraprón és a Jófogáson: a kör elején a háttérben, párhuzamosan a
  böngészővel; tempó, óránkénti keret, tiltásnál és üres oldalnál böngészős tartalék.
- Azonnali figyelő (instant.py): keresőszó vagy oldalcím, 2–15 percenként, saját szálon; csak akkor szól, ha új ÉS
  megéri (árbázis / keresés → a modell megnevezi → ár a másik oldalon → a hirdetés oldala → rövid modellkérdés); a
  modellre váró hirdetések megvárják, hogy ne zavarja a beszélgetést; sürgős push-értesítés.
- Automata kereskedés: egy Indítás/Leállítás gomb (keresések, figyelt oldalak, böngészés, azonnali figyelő; napszak
  nélkül), közös max. vételár és min. haszon.
- Két böngészési sor párhuzamosan (a Hardverapró és a Jófogás böngésző nélkül a saját szálán, a böngésző közben a Vinted
  és a Vatera); az Indítás állapota minden ablakban élőben; a napló a konzolon is; a modell tovább marad betöltve.
- Megérő találat ablaka (értesítésből, a listából): ár, mennyiért mennek, haszon; az eladónak szóló üzenet a vágólapra, és
  a hirdetés megnyitása. Android: „Megérő hirdetés” csatorna, 2 perc rezgés/hang, 10 perc szünet, legfeljebb 6 kör.
- Havi keret (a maradék limit), és szólás, ha felhalmozódnak az el nem adott tárgyak.
- A beszédfelismerés, a felolvasás és az ébresztőszó teljesen kikerült (Python, felület, Android, dokumentáció).
- Kezdőképernyő: Indítás gomb és Új tárgy csempe az első üzenetig; az Állj a Küldés mellett, csak futás közben; a
  Munka menüben a „Kérdezz Jarvistól” sor gombra nyílik; az Automata kereskedés tömör, összecsukható blokkokkal.
- Gyorsabb, szabályos keresés: 6–13 mp szünet, óránként 120 kérés oldalanként (a figyelőnek 80), lassú válasznál
  magától lassít; a találati oldalt 5 percig közösen használja a kör és a figyelő; körönként 8 jelölt; a keresések
  alapból óránként, és a Hardverapró/Jófogás-keresések ötpercenként az azonnali figyelőben is.
- Csomagbontás: a csomaghirdetések (csomagkeresések, és amit a többi út eddig kihagyott) várólistán; a modell a címük
  alapján kettőt választ, a leírásból és a fotókból tételesen felsorolja; darabár az árbázisból, a gyors oldalakról
  vagy óvatos becslésből; értesítés, kártya és ablak; Megvettem után a darabok külön tárgyak (a vételár szétosztva).

- Javítások egy külső átnézés alapján: az azonnali figyelő ár-ellenőrzésre váró, a körbe nem férő jelöltjei nem vesznek
  el; az ár előtti időpont perce nem olvad az árba; a kapacitás (GB/TB) a termékkulcsban; olcsó terméknél arányos
  min. haszon.
- Alkuképes hirdetések: az árbázisból (régóta fent, leárazva, kicsit drága), külön „Alku:” értesítés (nem riaszt,
  saját Android-csatorna, napi 6), ajánló üzenet a javasolt összeggel.
- Mai számok (látott, új, jelölt, miért esett ki, jó találat); azonnali figyelő: a következő oldal, ha csupa új jött, és
  az értékelés a saját szálán.
- Második átnézés javításai: más kapacitás az árösszehasonlításban sem hasonló (kapacitás nélküli keresésnél a
  legolcsóbb változat); a teljes találati oldal (100 hirdetés) és az oldal saját „legújabb elöl” rendezése; az
  értékelő szál Szünet alatt vár (a feladat megmarad), a szüneteltetett figyelőé a folytatásakor jön.
- Kiemelt hirdetések: a kártyák jelölése (osztály, „Kiemelt”/„Előresorolt” címke, a kiemeltek blokkja), a következő
  oldal az oldal alja szerint (a kiemeltek nem számítanak), a kiemeltek a sor végén.
- Több jó tipp: egységes termékkulcs (i5-10400F = i5 10400F, a kártyagyártó típusneve nem számít, a „2x8 GB” készlet,
  egy kapacitás-változatnál a kapacitás nélküliek is; a régi árbázis betöltéskor átrendeződik), a hasonlók a kulcs
  szerint (nem a modell elnevezésének minden szava kell), egy oldalról 6 szoros ár is biztos, az „Érdemes megnézni”
  lista (bizonytalan ár, csendes értesítés, körönként újra felméri, és ha biztos lesz, rendes jelölt), a köröknek
  fenntartott kéréskeret és a munkamenet akadálya a naplóban, a kártya címe a fejlécéből, a csomagok a mai számokban.
- Hardverapró: a böngésző nélküli letöltő a munkamenetében beállítja a listát (új hirdetések elöl, 100 egy oldalon),
  az oldalon ellenőrzi, és ha a beállítás megváltozik, a figyelő egyszer csak megjegyzi az oldalt.
- Legújabb elöl a címben: Jófogás `sp=3`, Vatera `ob=5&obd=2` (a felhasználó ellenőrizte, 2026. október), a Vinted
  mellett; a Vaterán a bemásolt figyelt oldal a saját rendezésével marad, a címben megadott rendezést nem írja át. Ha
  egy figyelő címe így megváltozik, egy letöltés csak megjegyzi az oldalt.

## Élesben kipróbálandó
- Az Android-alkalmazás új része (a megérő hirdetés értesítése: rezgés, hang, ismétlés, a találat ablaka, a vágólap) a
  GitHub Actions fordításában megy át, telefonon nincs kipróbálva; a rezgés a gyártói energiatakarékos módoktól is függ.
- Néma módban az Android nem rezeg és nem csenget (a rendszer döntése); rezgő módban csak rezeg.
- A Hardverapró és a Jófogás sima letöltéssel (feeds.py): a valódi oldalakon a kártyák kiolvasása (most a böngészős
  elemzés másolata, a tesztoldalak a két oldal szerkezetét utánozzák), hogy a Jófogás tényleg a szerveren rajzolja-e ki a
  találatokat (ha nem, háromszor üres után magától a böngészőre vált), és hogy a 6–13 másodperces szünet és az óránkénti
  120 kérés nem túl sok-e (ha egy oldal 403-at vagy 429-et ad, vagy sokszor „lassít”, a `feeds.GAP` és `HOURLY`, az
  azonnali figyelőnél az `instant.INSTANT_HOURLY` és a `LINKED_EVERY` visszaveendő). Egy Keresés a figyelőben
  ötpercenként fut: sok Hardverapró/Jófogás-keresésnél mindegyik ritkábban kerül sorra (az óránkénti keret véd).
- Az azonnali figyelő az előresorolt (felfrissített) régi hirdetéseket is újnak látja, ha az első letöltéskor nem voltak
  az első oldalon.
- A Vinted üzenetoldala: a beszélgetések és üzenetek felismerése általános szabályokkal megy, a valódi oldalon nincs kipróbálva. Ha „nem találtam beszélgetést / üzenetet” jön, egy hibacsomag vagy az oldal HTML-je kell.
- Az Android-módosítások (értesítés gombja → Eladva ablak) itt nem fordultak le: a GitHub Actions fordítását meg kell nézni.
- A fotóküszöbök valódi telefonos fotókon (főleg: világos tárgy fehér háttér előtt, gyenge fény).
- Automata kereskedés a valódi oldalakon: a kategóriaoldalak (Vinted catalog, Jófogás, Hardverapró, Vatera „hamarosan lejár”) hirdetéskártyáinak kiolvasása, a Vatera-kártyák hátralévő ideje, a Vinted ajánlattétel-ablaka, az újranézett hirdetés „elkelt / eltűnt” felismerése.
- A böngészés a valódi oldalakon: a Vinted kezdőoldalának kártyái (a kártyán sokszor csak a márka és a méret látszik), a Jófogás és a Vatera keresőjének rendezése (a legújabb elöl-e), a Hardverapró-kategóriák címe (2026 októberében ellenőrizve).
- Az új termékkulcs élesben: a GPU-szabály (a kártyagyártó típusneve kimarad) és a laptop/PC szavak, a „2x8” készlet;
  ha két különböző termék egy kulcsra esik (pl. egy laptop a „laptop” szó nélkül a kártyája árán), a pricebook.py
  GPU_FAMILIES / TYPE_WORDS / _SPLITS szabályán kell szűkíteni. A szoros ár (SURE_TIGHT 6, TIGHT_SPREAD 0.3) és a
  tippek (TIPS_PER_DAY 10, TIP_DAYS 3) hangolása a napi számok alapján.
- A termékkulcs valódi címeken: ha egy gyakori termék nem kap kulcsot, vagy két különböző termék egy kulcsra esik, a `pricebook.py` BRANDS / FAMILIES / MODIFIERS / TYPE_WORDS listáját kell bővíteni.

- Csomagbontás a valódi oldalakon: a modell darabszám- és darabár-becslése a fotókról (ruhánál, Legónál), hogy a
  csomaghirdetések címéből (bundle_like) mennyi jön, és hogy a GUESS_RATIO (60%), a PIECE_MIN (1 500 Ft), a
  PIECE_COST (300 Ft) és a BUNDLE_ROI (50%) jó-e; a Vinted csomagjainak oldala (fotók, leírás) a böngészővel.

- A Hardverapró lista beállítása (feeds.LIST_PREFS, _set_prefs): a menü (data-rios-action-pref-mod, data-pref) a
  valódi oldalról való, de magát a beállító kérést (a /muvelet/beallitasok/modosit.php-ra) nem láttuk: a letöltő
  POST-tal küldi a mezőket (order=time, dir=d, block=100; X-Requested-With, Referer), és az újra letöltött oldal aktív
  menüpontjából ellenőrzi. Élesben megnézendő a naplóban („a lista beállítása: új hirdetések, 100 db”); ha nem hat
  (pl. JSON-t vagy tokent vár), a böngésző Network fülén a modosit.php kérés Payloadja kell.
- A kiemelt kártyák jele a valódi oldalakon (feeds._PROMO_CLASS, _PROMO_TEXT): ha egy oldal másképp jelöli, a kiemeltek
  csak nem kerülnek a sor végére (az újnak látás a webcím szerint megy, arra nincs hatással).
- Az ármező felismerése („price” osztályú elem a kártyán) a valódi Hardverapró- és Jófogás-kártyákon.
- Alku: hány ajánlatot fogadnak el; a HAGGLE_DAYS (7 nap), a HAGGLE_MAX (25%) és a 10%-os minimum haszon hangolása.
  Az árbázis régi (kapacitás nélküli) kulcsai vegyesek maradnak, amíg ki nem öregednek (45 nap).

## Kisebb javítások (ismertek, nem sürgősek)
### Begyakorolt lépések (replay.py)
- A FlowRecorder egy kockázatos jelölőnégyzetet (feltételek elfogadása) még közzétételnek vesz; a `check` jelzést a receptbe is át kell vinni.
- A lista következő szintjének javítására (összevont azonos feliratok) nincs külön teszt.
- Az OPTION_KINDS bővítése a hiányzó lépés javításának jelöltjeit is bővíti: tesztelni.
- A `_human_click` saját Escape-jét nem jegyzi fel „press” lépésként.
### Ár és munka
- `parse_price`: nagyon hosszú számjegysor OverflowError-t ad ItemError helyett; a „másfél” magában 2 Ft; a tárgyszerkesztőben a furcsa összeg visszakérdezés nélkül mentődik.
- `WorkDesk.approved`: a feltöltés közbeni bármely jóváhagyás (pl. jelölőnégyzet) beállítja.
- Elakadásfigyelés: a feladat első üzenetében lévő oldalnézetnek nincs ujjlenyomata, az első hiábavaló lépést nem számolja.
### Felület és értesítések
- „Ott is átírtam” gomb az ár- és címelcsúszáshoz (most a jelzés addig marad, amíg vissza nem írod).
- A „Nem igaz, fent van” után a régi `checked_at` marad; a captcha-értesítés gombja után a „jelentkezz be” üzenet jön, és a főoldal nyílik meg, nem a hirdetés.
- Az értesítéslista betöltése közben érkező értesítés elveszhet a listából (versenyhelyzet).
- „N napja fent” 24 órás ablakokkal számol, és nyitva hagyott oldalon nem frissül.
- Android: a „login” értesítésgomb nincs megjelenítve; a telefonos API-ban nincs hozzá végpont. Az „eltűnt” értesítésnél nincs „Már levettem” gomb.
### Vevői üzenetek (messages.py)
- Ha az oldal csak a legutóbbi üzeneteket mutatja, és nincs átfedés a mentettekkel, a régebbiek elvesznek.
- A máshol (pl. a Vinted appban) már elolvasott beszélgetést nem nyitja meg újra; a lista megváltozott előnézete is jelezhetné.
- A legkisebb ár alatti vázlat helyett mindig ugyanaz a magyar elutasítás jön, akkor is, ha a vevő nem alkudott, vagy más nyelven írt.
- A fejléc/lábléc kiszűrése elrejtheti a láblécben lévő válaszmezőt; tesztoldal kell rá.
- Teszt hiányzik: `confirm_risky = false` ág, Állj a jóváhagyás alatt, a küldés után meg nem jelenő üzenet.
- A küldés eredménye ugyanolyan fajta értesítés (`message`), mint egy új vevői üzenet.
- A `min_price` nem látszik és nem állítható a Munka-beszélgetés eszközeiből (item_get, item_update).
### Automata kereskedés (deals.py, pricebook.py)
- Egy figyelt oldal beállításai (haszon, órák) nem szerkeszthetők, csak törlés és újra hozzáadás.
- A lezárult Vatera-licitek végső ára valódi eladási ár lenne az árbázisba; most kimarad.
- Az eladási sebességhez a Vinted-hirdetéseket nem nézi újra (robot-ellenőrzés); a ruháknál ezért nincs sebesség.
- Reggel csak a figyelt oldalak első oldalát nézi: ami éjjel jött, és azóta lecsúszott, kimarad.
- A böngészés indulóoldalai (BROWSE_LANES) a kódban vannak; a felületen nem lehet sajátot hozzáadni vagy kikapcsolni (a figyelt oldalak külön mennek).
- A böngészés a modellre váró ismeretleneket csak a memóriában gyűjti: újraindításkor elvesznek (a következő munkamenetben újra jöhetnek).
- Készenlétben a böngészés miatt a böngésző újraindul; egy app-szintű teszt (a kereskedés munkája készenlétben, valódi böngészővel) még hiányzik, csak az ütemezőé van meg.
- Az indulóoldalak súlya csak nő vagy csökken; ha egy oldal hetekig nem hoz semmit, a régi sikerei miatt még sokáig előre kerül.
- Ami csak a termékcsaládot nevezi meg típus nélkül („PS5 konzol”, „Nintendo Switch”), nem kap kulcsot (a játékkal, kiegészítővel keveredne): ezeket csak a modell nevezi meg, vagy egy keresés méri.

### Alku és mai számok (deals.py)
- Az alkuképes hirdetések csak az árbázisban lévő (termékkulccsal felismert) hirdetésekből jönnek; a kulcs nélküli
  (pl. ruha) hirdetésekre nincs alku.
- A mai számok egy hirdetést egy nap egy számban egyszer számolnak, de újraindítás után újra (a memóriában
  jegyzi, mit számolt már).
- A Hardverapró/Jófogás egy kategóriájának egyszeri letöltése több keresésre (összevont gyűjtés) még nincs: minden
  keresés külön kérés (az óránkénti keret ezt korlátozza).

### Csomagbontás (deals.py)
- A Vatera csomag-licitjeit nem méri fel (csak a fix árasakat); a licit mostani ára még nő.
- A csomagkeresések nem kerülnek az azonnali figyelőbe (a körben futnak, alapból óránként); a csomagok felmérése úgyis
  lassabb (oldal, fotók, modell).
- Egy tétel több darabja (pl. „5 db Zara póló”) egy eladandó tárgy lesz; ha egyenként adod el, kézzel kell szétbontani.
- Körönként legfeljebb két csomag oldalát nyitja meg: egy új, sok találatos csomagkeresésnél az első napon a modell a
  várólistából csak a legígéretesebbeket választja, a többit látottnak jegyzi.

### Párhuzamos munka (eladás-előkészítő szál, modellkapu)
- A valódi Ollamával `OLLAMA_NUM_PARALLEL=2` mellett nincs kipróbálva: mennyit lassít a két egyszerre futó hívás
  egymáson, és hogy a kétszeres kontextusmemória mellett a modell elfér-e a kártyákon (különben a Jarvis kisebb
  kontextussal tölti be, ami a hosszú feltöltéseknél ronthat).
- Ha az Ollama szolgáltatásként, másik felhasználóként fut, a környezete nem olvasható: ilyenkor a felhasználói
  beállításból (setx) számol, ami nem biztos, hogy egyezik a futóval.
- A kereskedés a böngészőt csak a következő lépésénél adja át (egy oldalbetöltés, legfeljebb fél perc); egy épp futó
  kereskedési modellhívást nem lehet megszakítani, egy hellyel azt a feltöltés kivárja.

### Nyugodt szívvel (biztonsági értékelés, elkelt árak, fotók)
- A határok (`deals.SAFE_ROI`, `RISKY_ROI`, `WIDE_SPREAD`, `SLOW_OK_ROI`) becslések, valódi eredményekkel nincsenek
  hangolva; a Vételeim tényleges hasznából később tanulhatna.
- Az elkelt ár az újranézéskor eltűnt hirdetésekből jön: a törölt (nem eladott) hirdetés is eltűnik, és körönként csak
  két hirdetést néz újra, ezért lassan gyűlik.
- A hirdetés fotóit az og:image és a fő tartalom képei adják; a valódi Hardverapró-, Jófogás- és Vinted-oldalakon nincs
  kipróbálva, hogy melyik kép jön (lehet, hogy a Vinteden a képek csak bejelentkezve tölthetők le).

### Pro feltöltés
- A fotókról való kiolvasás és az árbecslés valódi fotókkal és a valódi modellel nincs kipróbálva (a tesztek hamis
  modellel futnak). A modell legfeljebb az első négy fotót kapja (`selling.PRO_PHOTOS`, 1024 px): ha a címke az ötödik
  fotón van, a méretre rákérdez.
- A Nézd át ablakban az oldalak (Vinted, Jófogás…) nem módosíthatók; a hirdetés lapján igen.
- Az Android kamera gombja (PhotoProvider) egyszerre egy fotót ad; több fotóhoz többször kell megnyomni.

### Vételeim és azonnali figyelő
- Az azonnali figyelő egy letöltésből legfeljebb három, az árbázis által nem ismert hirdetés árát nézi meg máshol (a modell
  szerint megérők elöl); ha egy kategóriában egyszerre sok ismeretlen új jön, a többi kimarad (`deals.EXPLORE_PICKS`).
- A régi napszak-beállítás (`set_day`, „day” művelet) a háttérben megvan, az Indítás 0–24 órára állítja; a felületen nincs.
- A gyanús hirdetések szűrője (`deals.SUSPECT_RATIO`, értékelés nélküli eladó, oldalon kívüli elérhetőség) valódi
  oldalakon nincs kipróbálva: ha egy oldal minden hirdetésénél kiír valamit, ami „nincs értékelés”-nek vagy e-mail-címnek
  látszik, ott minden jelölt kiesne. A napló („Gyanús, nem szóltam róla…”) mutatja; akkor az oldalszöveg-mintán kell
  szűkíteni (`_NO_REVIEWS_RE`, `_OFFSITE_RE`).
- A Visszamondva a már feltöltött hirdetést az oldalakról nem veszi le (a tárgy törlése sem).
- A „most kb.” ár (Vételeim) a tárgy címéből vagy termékéből keres kulcsot; a csak leírással (prompt) felvett tárgynál
  az első kiolvasás után jelenik meg.
- Az azonnali figyelő ütemezése (mikor jön a következő) a memóriában van: újraindítás után rögtön néz egyet.

### Fotók (photos.py)
- Két külön elkészített, nagyon hasonló fotót (kicsit elmozdult kamera) nem ismer fel ismétlődőnek.
- Egy hibás, de mostani változatú mentett mérés a `report()`-ban hibát dobhat (a `sharp` nem szám).
- Indításkor a tárgyak közti összevetés O(P²); sok fotónál a háttérszálba kellene tenni, vagy vödrökre bontani.

## Ismert hibák / átmeneti
- `tests.test_creative.test_a_story_from_an_idea` néha elbukik (a régi commiton is), nem a kereskedés része.

## Döntések
- Emberi tempó és jóváhagyások érintetlenek; közzététel, törlés és üzenetküldés csak jóváhagyással.
- A Vinted 2026 őszétől állítólag szigorúbban tiltja a botokat (nem hivatalos források) – kockázat, ezért alacsony gyakoriság (hirdetésellenőrzés naponta, üzenetek kétóránként, csak olvasva).
