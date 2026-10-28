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
- Átnézés javításai (5b5a790 után): a kapacitás nélküli címek csak az ismerten egy memóriamérettel gyártott
  videókártyáknál (pricebook._SINGLE_CAPACITY) számítanak a változathoz, az árbázis tartalmából nem; a tipp csak lezárt
  ellenőrzés után kerül le; a figyelő minden kérése (oldal, árkeresés) a keretéből megy, kerethiánynál az értékelés
  vár; a várólista 300 (két teljes oldal), és ami kiesik vagy lejár, azt a figyelő elfelejti (újra jön); a RAM és a
  tárhely külön számít a szűrésnél (pricebook.sized, size_conflict); a Szünet egy köteg közben is megállít.
- Második átnézés (335bed5 után): a váró hirdetés csak a szükséges oldal keretére vár (Deferred.sites, wait_for); egy
  „RAM” szó csak egy méretet jelöl (8GB RAM 256GB); a figyelő tartaléka a közös letöltőben, minden tényleges kérésnél
  (feeds.Quota: címke, saját keret, tartalék; a lista beállításának POST-ja és újratöltése is).
- Hardverapró: a böngésző nélküli letöltő a munkamenetében beállítja a listát (új hirdetések elöl, 100 egy oldalon),
  az oldalon ellenőrzi, és ha a beállítás megváltozik, a figyelő egyszer csak megjegyzi az oldalt.
- Legújabb elöl a címben: Jófogás `sp=3`, Vatera `ob=5&obd=2` (a felhasználó ellenőrizte, 2026. október), a Vinted
  mellett; a Vaterán a bemásolt figyelt oldal a saját rendezésével marad, a címben megadott rendezést nem írja át. Ha
  egy figyelő címe így megváltozik, egy letöltés csak megjegyzi az oldalt.
- Pro feltöltés készenlétből: az eladás-előkészítő szál maga ébreszti fel a Jarvist (_awake_for, _wake_lock), nem vár
  a kereskedés szálára (eddig oda került, és ha az épp dolgozott vagy a felébredés nem sikerült, a tárgy szó nélkül
  vázlat maradt: a hiba a Munka oldalon rejtett Beszélgetésbe ment). A sikertelen felébredés a kártyán, a Munka
  naplójában és értesítésben is látszik; a Szünet alatt jött új tárgy (és a feltöltés) a Folytatás után magától megy
  (_held); amíg egy tárgyon dolgozik, nem áll vissza készenlétbe. Az OLLAMA_NUM_PARALLEL-t a Jarvis maga beállítja,
  ha hiányzik (mint a setx, tartósan; ollama_log.set_user_setting); a tesztekben `parallel = 1`, hogy ne írjon a
  valódi beállításokba.
- Tesztelés közben talált hibák javítva (regressziós tesztekkel): a Megvettem egy találatból egyszer könyvel
  (DealHunter.buy, zár alatt; a csomag is; a gomb a válaszig letiltva); a sikertelen mentés semmit nem módosít
  (ItemStore._clean: előbb minden mező ellenőrizve); a Leállítás a futó azonnali értékelést is megállítja (_Off: minden
  letöltés, modellkérdés és szólás előtt, a letöltő várakozása közben is); egy tippet csak az egyik sor foglalhat le (a
  kiválasztás és a foglalás egy zárolt lépés), és egy hirdetésből (webcím) egy találat (_remember); a modellhívás közben
  megváltozott leírás (vagy Pro fotók) régi válasza nem kerül a tárgyba (ItemStore.update_if, STALE_RETRIES).
- A hirdetés leírása alapból csak a zárószöveg és a kulcsszavak; a tárgyról csak akkor ír, ha a leírásban (Pro: a
  megjegyzésben) kéred (selling.wants_description: „leírás”, „írd bele/oda”, „írd meg, hogy”, „említsd”…). A modell
  szövegét kérés nélkül el is hagyja. EXTRACT_VERSION 4: a még fel nem töltött tárgyaké a következő kiolvasáskor így lesz.
- A leírás zárószövege egyetlen, helyes magyarságú szöveg (selling.DESCRIPTION_FOOTER; ruhánál „ruhát”, máshol
  „holmit”), mindig benne van: fölötte a kért szöveg, alatta a kulcsszavak. A korábbi három változatot (az egyikben
  „van még sok ruhát;”) a még fel nem töltött tárgyakban betöltéskor erre cseréli (ItemStore._refresh_footer).
- Puhább értesítési hangok (android/make_sounds.py): a megérő hirdetésé egy halk, háromhangú csengés négy másodperc
  csenddel (a FLAG_INSISTENT ismétlésével ötmásodpercenként egy csengés), két rövid rezgéssel; a többié két halk hang; a
  csatornák -3-asok (a -2-esek az OLD_CHANNELS-ben). Az „Érdemes megnézni” tipp saját csatornán egy nagyon rövid
  pittyenéssel szól (notice.tip; az alkalmazás nyitva létében is), naponta legfeljebb 30 (TIPS_PER_DAY). A laptopos
  push rezgése is rövidebb.
- Elemzés fül (a régi Vételeim): a Teendőim elöl (todos.py: feladási kód, hely, vevő, a beszélgetés, a címke képe,
  Kész / mégsem kész / törlés; a fül neve mellett az elvégezetlenek száma), a saját holmid eladása is haszon
  (profit_report: own, a megtérülés csak a megvettekből), a Hirdetéseim › Eladva fölött összesítő. Esti emlékeztető
  (a „todos” háttérmunka, [scheduler] todo_reminder = "19:00"): ha van elvégezetlen, naponta egyszer szól.
- Vinted-eladás az üzenetekből (messages.MessageDesk._sale): az utolsó message_sales (8) beszélgetés közül a
  változottakat is megnyitja; „Eladva” + „Szállítási címke lekérése” gomb (sale_signals) → eladottnak jelöli (ahol
  elkelt, ott „Itt kelt el.”), megnyomja a gombot (legfeljebb LABEL_TRIES = 2), a feladási kódot és a helyet a címke
  ablakából olvassa ki (LABEL_JS, shipping_info), és teendőt ír a szövegével és a képével; ha nincs kód, a következő
  körben a beszélgetésben keresi. A nem olvasatlan beszélgetés üzeneteiről nem szól (count_new=False).
- Ruha kivéve az automata kereskedésből (pricebook.wearable: ruhaszavak, cipő, divatmárkák; a VW Polo nem): a
  böngészés, a figyelt oldalak, a csomagbontás (a régi „Ruhacsomag” keresés és alapgomb törölve, ruhás csomagkeresés nem
  vehető fel), az alku, a tippek és a modell megnevezése után is kiesik („ruha, cipő” a napi számokban); a Vinted
  indulóoldalai tárgyak. A kifejezett kérés (egy Keresés vagy egy azonnali figyelő keresőszava maga ruha) megmarad.
- Összehasonlíthatóság: a szokásos árba csak az egyező típus, változat, kapacitás, darabszám és állapot számít; a
  „-tól” áras és a több terméket felsoroló hirdetés kimarad (pricebook.unusable); a bizonytalan változatnál legfeljebb
  két leírást olvas el (_clarify); a mostani alacsonyabb árszint felülírja a régebbi árakat (FRESH_GAP).
- Az árkutatás közben talált hirdetések önálló jelöltek (_peers): saját maguk nélkül mérve, a kilógó olcsó ár is
  jelölt marad, a várható haszon és a megbízhatóság szerinti sorrendben, árkutatásonként legfeljebb SPAWN_MAX,
  hirdetésenként SPAWN_TTL-enként egyszer, a közös kéréskeretből; az eredeti vizsgálata is megmarad. Az azonnali
  figyelőben is (_recheck). A keretbe nem férő jelölt nem vész el: a körökben a következő kör ezzel kezd (_carry,
  CARRY_TTL), az azonnali figyelőben önálló hirdetésként vár (Deferred.extra); a biztos árúak a haszon szerint jönnek.
- A találat ablakában „Mihez mértem?”: a beszámított és a kizárt hirdetések az okkal (legfeljebb 15-15), kínálati
  ársáv, óvatos eladási ár, költség, haszon a költség után.
- Az eladó többi hirdetése (seller_bundle): egy jó találatnál az eladó oldala (feeds/browser seller_link), a darabok
  külön is eladhatók-e, közös vétel egy postával, a Vinteden a vevővédelmi díj a végösszegre egyszer, munka (hirdetés,
  perc), javasolt közös ajánlat; csendes értesítés, ha legalább SELLER_GAIN-nel jobb.
- Turbó (az Indítás mellett, megmarad): a böngészés ismeretlenjei minden oldal után a modellhez (TURBO_ROWS), a
  munkamenet végén a maradék is; a modellkapu a tartalék helyet is a kereskedésnek adja, ha TURBO_IDLE óta nincs
  elsőbbségi munka (llm.ModelGate._spare). A weboldalakra nem kér többet.
- Hiba javítva: a gyors sor (Hardverapró, Jófogás) ismeretlenjei a böngésző sorába kerültek (a _step_browse-ban a
  `lane` változó az indulóoldal lett), és ott vártak, amíg a böngésző sora meg nem nevezte őket (pihenőben sokáig).
- A Hardverapró és a Jófogás nem kerül a böngészőbe, ha csak az óránkénti keret fogyott el vagy hálózati hiba volt
  (deals._Later): a keresés és a figyelt oldal BUSY_RETRY (10 perc) múlva újra, a friss jelölt figyelmeztetéssel, az alku
  és a csomag vár. A böngésző csak tiltásnál és üres oldalnál veszi át (eddig a keret fogyásakor is: ezért volt a
  böngészőben a Hardverapró, alap sorrenddel). A hardveraprós és jófogásos Figyelt oldalak az azonnali figyelőben is
  (link_searches, ötpercenként).

- Javítások egy átnézés után: a többtermékes szűrő egy termék szavait egynek veszi (i5 + 10400F, DDR4/CL16/AM4/GDDR6,
  évszám, mértékegység előtti szám nem típusjel), és a hirdetés címét nézi, nem a kártya szövegét (eladó neve); egy
  hirdetésnek egy ára van a friss állapota szerint (PriceBook._drop, _market_of: a most látott állapot felülírja a
  tároltat); az eladó többi hirdetésénél minden darabra a vételi ellenőrzések (valódi ár, gyanús, hamisítvány, vételi
  limit, a közös végösszeg is a limiten belül).
- Licitlista (Vatera): a még napokig futó, megérő licit csendes értesítéssel a listára kerül (a javasolt legmagasabb
  ajánlattal), az ára frissül, ha újra látja; a zárás előtt a kör megnézi, és szól (_follow_auction, _step_auctions).
  A Vatera böngészése a Műszaki cikk és mobil főkategóriát is nézi.
- Android: saját értesítési hangok (res/raw/jarvis_alert, jarvis_ping), új csatornaazonosítók (-2).
- Licitlista javítva: a listára vétel ellenőrizetlen (licithatár és értesítés csak az adatlap és a modellkérdés után:
  _verify_auction); a zárás előtti ellenőrzés az adatlapról olvassa a mostani árat (auction_price), és a licithatárt
  a mostani beállításokkal számolja újra (_auction_top: vételi limit, havi keret, a keresés vagy a figyelt oldal
  szabálya); ha a mostani ár eléri, nem szól; ha az adatlap nem nézhető meg, nem szól elavult adatokkal.
- Az ütemező tud a 10 perces újrapróbálkozásról és a licitlistáról (every_minutes: addig percenként néz).
- Gyors riasztás (alapból be, Beállítások): az adatlap és az állapot szerinti újraszámolás után rögtön csenget, a modell
  utána nézi meg (a körben a következő modellkérdésként, az azonnali figyelőben a köteg végén); ha gyanús, „Mégsem”, és
  leveszi a találatok közül. A jelölt adatlapja a letöltő sorában elsőbbséget kap (SiteFetcher urgent; a szünet marad).
  Előmelegítés ráérő időben (DealHunter.warm, InstantWatch._warm): a gyakori termékek összehasonlítóinak leírása,
  csendes órában a kevés árúak árkeresése; a mai számokban „Ráérő időben”.
- Megbízhatóbb „megéri” (egy átnézés hibái és kérései alapján): az alku az ajánlott áron a teljes költséggel számol
  (a reprodukált +5 050 Ft helyett −12 950 Ft), hiányzó javítási költségnél bizonytalan; videókártyán a „4G” 4 GB VRAM,
  telefonon a hálózat; memóriánál a kiszerelés, a DDR-generáció, a SO-DIMM és az ECC/REG külön; a Switch-változatok és a
  konzolcsomagok külön; termékadatok kategóriánként forrással és idézettel (specs.py), összehasonlítási osztályok
  (pontos, közelítő, kizárt, bizonytalan); független árak (egy hirdetés egyszer, egy eladótól legfeljebb 2, a vizsgált
  hirdetés máshol fent lévő példánya nem mérce); a kilógóan olcsó hasonló megmagyarázva (a napok óta fent lévő, hibátlan
  versenytárs lejjebb viszi a gyors eladási becslést); darabár, részletár, üres doboz, zárolt telefon kimarad; csomagnál
  a darabár vagy „-tól” ár nem csomagár; az állapotfelismerés tagadásai („nincs folt”), „világosabb folt”, „nyomáshiba”,
  „töltő nélkül”, és az állapotról hallgató leírás ismeretlen; a közös friss lista letöltve / vár / kiértékelve
  állapottal (más árszűrőnél sem vész el hirdetés, a szünetelő vagy elavult lista nem lassít); az utólag elutasított
  találat mindenhol frissül, és az értesítést egyszer helyesbíti (Android: a csengés leáll); a Hardverapró-kártyán a
  súgólink nem előzi meg a hirdetés linkjét. Árbecslés: a hirdetési ár mediánjától külön gyors eladási becslés sávval,
  megbízhatóság (független árak, oldalak, frissesség, szórás, változat, ellenőrzött állapot), eladási költségek
  (csomagolás, eladási díj, egyéb, tartalék; a meg nem adott látszik), javasolt legmagasabb vételár, az eladás módja
  (ahogy van, hasonló hibájúként, javítva); „Alacsony kockázat” a „Biztos befektetés” helyett, „eltűnt (talán
  elkelt)”, az „Ellenőrizve” pontos jelentése.

## Élesben kipróbálandó
- Azonnali figyelő: a feltöltési idő a valódi kártyákon (instant._POSTED_RE: „ma 14:07”, „tegnap 23:50”; a Jófogáson
  más lehet, akkor nincs késés-adat); a keretarányos ütem (POLL_SHARE 0,75, PEAK_EVERY 1 perc, BURST 0,5) és a 90-es
  keret mennyire terheli az oldalakat. A Hardverapró kiemeltjeinek átugrása (LEAD_SITES, offset=): hogy a valódi oldalon
  az offset= tetszőleges értékkel is jól lapoz-e a 100-as listanézetben, és hogy az „Előresorolt” blokkot a kártyák
  felismerik-e (feeds._promoted). A közös friss lista (Hardverapró › Hardver) lefedettsége (COVER_MIN 5, COVER_MISS 0,1).
- Gyors riasztás: hány „Mégsem” jön utána naponta (ha sok, érdemes kikapcsolni, vagy a csengetés szövegén jelezni). Az
  előmelegítés (WARM_EVERY 60 mp, WARM_BELOW 0,5, WARM_SCORE 1,5) mennyit visz el a figyelő keretéből (a mai számokban
  „Ráérő időben”), és hogy a gyakori termékek friss hirdetéseinél tényleg elmarad-e a leírások olvasása a csengetés
  előtt. A csendes órák felismerése (tanult ritmus) a valódi forgalommal.
- Alkuval megérheti (FRESH_HAGGLE_CUT 15%): hány ilyen tipp jön naponta (a mai számokban „alkuval megérheti”), és
  hogy a javasolt ajánlatot elfogadják-e. Utólagos modellellenőrzés (FOLLOWUP_TTL 6 óra): mennyi idő alatt kerül sorra
  egy feltöltés után.
- Állapotfelismerés (condition.py): a kulcsszavak (kijelző, akku, működés, tartozékok, garancia) a valódi leírásokon;
  ha egy gyakori megfogalmazás kimarad, az állapot ismeretlen marad (nem hibás). A modell condition válasza (a
  CHECK_MAX_TOKENS 700 elég-e, és hogy az idézetei tényleg szó szerintiek-e). A vett összehasonlítók leírásának
  olvasása (VET_READS 3, VET_MIN 2) mennyit visz el a kör és az azonnali figyelő kéréskeretéből; a Pótlás és javítás
  költségei (a felhasználó adja meg). Az ellenőrizetlen tippek száma (a „Kiesett: ellenőrizetlen” a mai számokban).
- Licitlista: a Vatera-kártyák hátralévő ideje (parse_auction) a valódi oldalon; ha nincs meg, a licit nem kerül a
  listára. A licit mostani árának leolvasása az adatlapról (deals._AUCTION_PRICE_RE: „Aktuális ár”, „Jelenlegi licit”,
  „Legmagasabb ajánlat”): ha a valódi oldalon másképp szerepel, a zárás előtti szólás a legutóbb látott árral megy,
  figyelmeztetéssel. A „hamarosan lejár” rendezés címe (ob=…&obd=…) nem ismert: ha megvan, a böngészés licites oldalai azzal is
  mehetnének. Hány licitértesítés jön naponta (AUCTION_TIPS_PER_DAY 10).
- Android: a saját hang a telefonon (hangerő, hossz; a FLAG_INSISTENT ismétli, a csend a fájl végén szünetet tart), az
  „Érdemes megnézni” pittyenés, és hogy a régi csatornák törlése után a beállítások rendben vannak-e.
- Vinted-eladás: a valódi beszélgetésoldalon az „Eladva” jelzés és a „Szállítási címke lekérése” gomb felirata
  (messages._SOLD_RE, _LABEL_RE), a címke ablaka (LABEL_JS: role=dialog vagy a főrész), és a feladási kód és a hely
  kiolvasása (shipping_info: a „kód” szó után vagy alatta, nagybetűk és számjegyek; a hely egy csomagautomata,
  csomagpont vagy futárcég nevéből). Ha a Vinted csak QR-kódot ad, a teendőben a kép marad. A tárgy párosítása a
  beszélgetés hirdetéslinkjéből (match_item); az eladási ár a hirdetési ár (alkunál átírandó).
- Ruha felismerése (pricebook._WEAR_RE) valódi címeken: ha egy nem ruha kiesik (pl. egy márkanév más termék nevében),
  vagy egy ruha átjut, a listát kell szűkíteni vagy bővíteni (a napló „ruha, cipő” sorából látszik).
- Az eladó többi hirdetésének linkje (feeds.SELLER_TEXT_RE, SELLER_HREF_RE, a böngészőben a LISTING_PAGE_JS) a valódi
  Hardverapró-, Jófogás-, Vinted- és Vatera-oldalakon; a Vinted vevővédelmi díja (VINTED_FEE_FIXED 300 Ft,
  VINTED_FEE_RATE 5%) a valódi díjszabás szerint hangolandó, és a 10 perc/hirdetés munkaidő is.
- A leírások elolvasása a bizonytalan változatnál (DETAIL_READS 2) a kéréskeretből megy: ha sok kör üresen fut, vagy a
  napló szerint sokszor „a leírásából sem derül ki”, csökkentendő.
- Turbó élesben: mennyivel több modellhívás (és mennyi melegedés), és hogy egy feltöltés tényleg csak pár másodpercet
  vár-e (TURBO_IDLE 60 mp); ha a kereskedés két hívása együtt túl lassú, a Turbó egyik része külön kapcsolóra tehető.
- Pro feltöltés készenlétből: a felébredés az előkészítő szálon, miközben a kereskedés szála böngészik (a modell
  betöltése a kereskedés egy modellhívása közben); ha egyszerre ébresztené a beszélgetés is, egyszer tölt be.
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
- Az egy memóriamérettel gyártott videókártyák listája (pricebook._SINGLE_CAPACITY) az ismert asztali kártyákat
  tartalmazza; új típusnál bővíteni kell (csak adatlap alapján, ahol tényleg egy méret van). A RAM/tárhely felismerése a
  mellette álló szóból jön (RAM, DDR, GDDR, SSD, NVMe, tárhely); a szokatlan írásmódokat a naplóból kell pótolni.
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
- A Hardverapró-kártya a valódi oldalon: hogy a kiemelt kártyán a „Kerülj te is előre!” súgólink tényleg a mentett
  minta szerinti helyen és alakban van-e (tests/data/hardverapro_eloresorolt.html, feeds._HELP_TEXT, _HELP_HREF, a
  böngészős LISTINGS_JS), és hogy az eladó linkje `/tag/…html` alakú-e (feeds._SELLER_HREF). A Jófogás-kártyán az eladó
  neve nincs kiolvasva: ott az egy eladótól legfeljebb 2 ár szabály nem hat (csak az egy hirdetés egyszer).
- A termékadatok (specs.py) és az összehasonlítási osztályok valódi hirdetéscímeken: a típus- és gyártószavak listái
  (_GPU_MAKERS, _RAM_MAKERS, _SSD_MODELS, _SSD_IFACE_BY_MODEL) bővítendők; egy túl szigorú „bizonytalan” (pl. a kivitel
  nem derül ki) kevesebb összehasonlítót hagy, egy túl laza „közelítő” túl sokat.
- A gyors eladási becslés sávja (deals.SALE_MARGIN: ±5/10/20%), a megbízhatóság pontozása (deals.reliability) és az
  olcsó versenytárs szabálya (CHEAP_STAYED 1 nap, középár) becslés, valódi eladásokon nincs hangolva.
- Az eladási költségek alapból nincsenek megadva (Eladási költségek): amíg nem adod meg őket, minden találatnál és
  értesítésben ott áll, hogy 0 Ft-tal számolt.
- Az Android-alkalmazás helyesbítő értesítése (NotifyService.correct: a csengő riasztás leállítása) a GitHub Actionsön
  lefordul, de telefonon nincs kipróbálva.

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
- Az „eltűnt (talán elkelt)” ár az újranézéskor eltűnt hirdetésekből jön: a törölt (nem eladott) hirdetés is eltűnik,
  és hogy mennyiért kelt el, nem tudjuk (a hirdetési ár kínálati ár); körönként csak két hirdetést néz újra, ezért
  lassan gyűlik.
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
