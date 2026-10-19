# TODO – böngészés/feltöltés gyorsítás (félbemaradt munka)

## Kész (az ágon, összefésülve, tesztelve)
- vinted.py: Vinted-levétel modell nélkül (VintedDelister), a feltöltés a hirdetés saját címét jegyzi meg.
- browser.py (oldalnézet): rövidebb nézet (~40%), állandó elemszámok, `*` új elemek, csoportok, állapotok, hibaüzenetek, ár a hirdetéslink mellett, shadow DOM.
- browser.py (műveletek): MutationObserver-alapú gyors megnyugvás, reklám/analitika nem tartja fel, mezők fajta szerint, `browser_choose`, `browser_do`, fül-takarítás.
- tools.py / work.py / app.py: az új eszközök regisztrálva, a Munka menü is látja őket; régi scan-számok navigáció után érvénytelenek.

## Félbemaradt (a `wip-patches/` mappában, f32b6d7-hez képest; még NINCS összefésülve)
- loop.patch: gyorsítótár-barát tömörítés, eszközprofilok, num_predict, munka-hőmérséklet, melegítés módonként, elakadásfigyelés, kép a beszélgetésben (kész, saját ágán tesztelve).
- replay.patch: await_publish, felvétel-normalizálás, lépésjavítás, új értékek választása, űrlapcím megjegyzése (kész, saját ágán tesztelve).
- flow.patch: extract-once, publish gap a közzététel gomb előtt, fotólimit, posted_at, item_post előre (WIP, magyar árszám-értelmezés hiányzik).
- trade.patch: árkutatás előszűrés/statisztika, eladott/eltűnt hirdetés háttérellenőrzés, árelcsúszás (WIP, UI-rész félkész).
- Következő lépés: `git apply --3way wip-patches/loop.patch` (majd replay, flow, trade), ütközések feloldása (app.py, work.py, browser.py), utána `python -m pytest tests -q`.

## Ismert hibák / átmeneti
- A teljes tesztcsomag az összefésülés után nem futott; a böngésző-, Vinted-, replay- és Munka-tesztek átmentek.
- `BrowserController.hints` még nincs beállítva (a flow-nak kellene a tárgy szavaival).
- README nincs frissítve az új eszközökkel; a wip-patches mappát az összefésülés után törölni kell.

## Tervek (sorrendben)
1. A négy patch összefésülése és teljes tesztsor.
2. Magyar ár-értelmezés (számszavak, „4,5 ezer”), árelcsúszás jelzése a felületen.
3. Eladás-felismerés után egy gombnyomásos levétel a többi oldalról.
4. Vevői üzenetek olvasása (csak olvasás), válaszvázlat jóváhagyással.
5. Fotóminőség- és duplikátum-ellenőrzés.

## Döntések
- Emberi tempó és jóváhagyások érintetlenek; csak a nem emberi várakozást (polling, felesleges nézet) vágtuk.
- A Vinted 2026 őszétől állítólag szigorúbban tiltja a botokat (nem hivatalos források) – kockázat, alacsony gyakoriság.
- Munkacsomagok külön ágon készültek; a keretkorlát miatt patchként mentve, mert más ágra nem pusholtunk.
