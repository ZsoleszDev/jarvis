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

## Élesben kipróbálandó
- Az Android-alkalmazás új része (a megérő hirdetés értesítése: rezgés, hang, ismétlés, a találat ablaka, a vágólap) a
  GitHub Actions fordításában megy át, telefonon nincs kipróbálva; a rezgés a gyártói energiatakarékos módoktól is függ.
- Néma módban az Android nem rezeg és nem csenget (a rendszer döntése); rezgő módban csak rezeg.
- A Hardverapró és a Jófogás sima letöltéssel (feeds.py): a valódi oldalakon a kártyák kiolvasása (most a böngészős
  elemzés másolata, a tesztoldalak a két oldal szerkezetét utánozzák), hogy a Jófogás tényleg a szerveren rajzolja-e ki a
  találatokat (ha nem, háromszor üres után magától a böngészőre vált), és hogy a 8–16 másodperces szünet és az óránkénti
  90 kérés nem túl sok-e (ha egy oldal 403-at vagy 429-et ad, a `feeds.GAP` és `HOURLY` csökkentendő).
- Az azonnali figyelő az előresorolt (felfrissített) régi hirdetéseket is újnak látja, ha az első letöltéskor nem voltak
  az első oldalon.
- A Vinted üzenetoldala: a beszélgetések és üzenetek felismerése általános szabályokkal megy, a valódi oldalon nincs kipróbálva. Ha „nem találtam beszélgetést / üzenetet” jön, egy hibacsomag vagy az oldal HTML-je kell.
- Az Android-módosítások (értesítés gombja → Eladva ablak) itt nem fordultak le: a GitHub Actions fordítását meg kell nézni.
- A fotóküszöbök valódi telefonos fotókon (főleg: világos tárgy fehér háttér előtt, gyenge fény).
- Automata kereskedés a valódi oldalakon: a kategóriaoldalak (Vinted catalog, Jófogás, Hardverapró, Vatera „hamarosan lejár”) hirdetéskártyáinak kiolvasása, a Vatera-kártyák hátralévő ideje, a Vinted ajánlattétel-ablaka, az újranézett hirdetés „elkelt / eltűnt” felismerése.
- A böngészés a valódi oldalakon: a Vinted kezdőoldalának kártyái (a kártyán sokszor csak a márka és a méret látszik), a Jófogás és a Vatera keresőjének rendezése (a legújabb elöl-e), a Hardverapró-kategóriák címe (2026 októberében ellenőrizve).
- A termékkulcs valódi címeken: ha egy gyakori termék nem kap kulcsot, vagy két különböző termék egy kulcsra esik, a `pricebook.py` BRANDS / FAMILIES / MODIFIERS / TYPE_WORDS listáját kell bővíteni.

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
