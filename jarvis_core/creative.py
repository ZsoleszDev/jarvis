"""Kreatív menü: közös történetmesélés a helyi modellel.

Két mód:
- Kaland (AI Dungeon-stílus): a modell a mesélő. Te azt írod, mit teszel (Tett), mit mondasz
  (Mondás), vagy magát a történetet viszed tovább (Történet); a modell folytatja.
- Karakter (Character.AI-stílus): egy szereplővel beszélgetsz, aki a saját hangján válaszol.

Emlékezet (a felületen minden része látható, szerkeszthető és törölhető):
- Lényeg: amit a modell mindig lát (a világ, a szereplők, a fontos tények).
- Szerzői megjegyzés: a stílus és a hangulat iránya; a prompt végére kerül, ott hat a legerősebben.
- Világkártyák: szereplők, helyek, tárgyak leírása. Akkor kerülnek a promptba, ha a kulcsszavuk
  előfordul a történet utolsó részében (kulcsszó nélkül vagy "mindig" jelöléssel mindig).
- Emlékek: ha a szó szerinti előzmény már a kontextus közel felét foglalná, a modell összefoglalja a
  régebbi részt, és onnantól az összefoglalót látja helyette. Ha az emlékek is túl hosszúak, a
  legrégebbieket egy "eddigi történet" összefoglalóba sűríti. Így a történet akármilyen hosszú lehet.

A promptok angolul vannak, és alapból angolul is mesél; magyar nyelvű történetnél a promptok is
magyarok, külön kéréssel a természetes magyarságra.

Sebesség: a prompt eleje (utasítások, lényeg, emlékek, eddigi szöveg) körről körre változatlan, csak a
vége nő, így az Ollama a korábbi részt nem dolgozza fel újra (prompt-gyorsítótár). Ezért összefoglalni
is ritkán, egyszerre nagyobb darabot kell.
"""
from __future__ import annotations

import copy
import json
import os
import re
import secrets
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable

from . import creative_templates as templates
from .llm import Cancelled, OllamaClient, OllamaError, parse_json_object
from .textutil import fold, strip_think

MODES = ("adventure", "character")
LANGS = ("en", "hu")
LENGTHS = ("short", "medium", "long")
PLAYER_KINDS = {"adventure": ("do", "say", "story"), "character": ("user",)}
ID_RE = re.compile(r"^[0-9a-f]{8}$")

MAX_TEXT = 20000          # egy bejegyzés
MAX_FIELD = 12000         # lényeg, karakterleírás, összefoglaló
MAX_CARDS = 80
MAX_ALTS = 8              # ennyi változatot őriz meg egy válaszból
MAX_REDO = 50
KEEP_VERBATIM = 6         # az utolsó ennyi bejegyzés sosem kerül összefoglalóba
CARD_SCAN = 6             # a világkártyák kulcsszavait az utolsó ennyi bejegyzésben keresi
HISTORY_SOFT = 0.45       # ha a szó szerinti előzmény a kontextus ennyi részét foglalná, összefoglal
HISTORY_KEEP = 0.25       # összefoglalás után nagyjából ennyi marad szó szerint
MEMORY_SOFT = 0.10        # ha az emlékek együtt ennyit foglalnak, a régebbieket összesűríti
CARD_BUDGET = 0.08        # a kulcsszóra bekerülő világkártyák legfeljebb ennyit
KEEP_MEMORIES = 3         # sűrítéskor a legutóbbi ennyi emlék külön marad
MESSAGE_OVERHEAD = 8      # token üzenetenként (chat-sablon)
CHARS_PER_TOKEN = {"en": 3.6, "hu": 2.5}
TOKENS_PER_WORD = {"en": 1.45, "hu": 2.3}
WORDS = {"adventure": {"short": 80, "medium": 160, "long": 300},
         "character": {"short": 60, "medium": 100, "long": 200}}
DEFAULT_CREATIVITY = {"en": 0.95, "hu": 0.8}   # magyarul óvatosabb, különben furcsa szavakat talál ki
SNIPPET = 150


class StoryError(Exception):
    """Érthető hiba a Kreatív menüben (a felület kiírja)."""


# --- promptok ------------------------------------------------------------------------------------------

PROMPTS: dict[str, dict[str, Any]] = {
    "en": {
        "narrator": (
            "You are the narrator of an interactive story, a text adventure in the spirit of AI Dungeon. The "
            "player and you write the story together, turn by turn.\n\n"
            "How turns work:\n"
            "- Lines starting with \">\" are the player's turn: \"> You ...\" is something the player character "
            "does, \"> You say ...\" is something they say. Show what happens as a result.\n"
            "- Text from the player without \">\" is part of the story: treat it as canon and continue from it.\n"
            "- Text in [square brackets] from the player is an out-of-character instruction to you: follow it, "
            "but don't mention it in the story.\n\n"
            "How to write:\n"
            "- Second person, present tense (\"You push the door open. Cold air spills out.\"), matching the "
            "voice the story already uses.\n"
            "- Be vivid and specific: concrete sensory details, distinctive names, surprising but logical "
            "consequences. Show, don't tell. Avoid clichés and stock phrases.\n"
            "- Let the world push back. Actions can fail or have a cost, depending on the situation and the "
            "character's abilities. Other characters have their own goals, secrets and voices: they argue, lie, "
            "joke, help or betray.\n"
            "- Keep the story moving with complications, discoveries, choices and stakes. Don't resolve "
            "everything at once; leave threads open.\n"
            "- Never decide, speak or act for the player character beyond what their turn says. Stop at a "
            "moment that invites the player's next move.\n"
            "- Stay consistent with the established facts, characters, tone and world rules below. Don't recap "
            "or repeat earlier text; continue forward.\n"
            "- Write only story text: no headings, lists, menus of choices, notes, or questions to the player "
            "such as \"What do you do?\".\n"
            "- Length: {length}."),
        "character": (
            "You are {char}, a character in an ongoing roleplay chat with {user}. Write {char}'s next message.\n\n"
            "Who you are:\n{about}\n\n"
            "Writing rules:\n"
            "- Stay in character at all times: {char}'s voice, vocabulary, opinions, moods and secrets. Never "
            "say you are an AI, a language model or a character in a story.\n"
            "- Write {char}'s spoken words as plain text, and actions, gestures and expressions between "
            "asterisks, like *leans back with a crooked grin*.\n"
            "- Be lively and proactive: react with real emotion, tease, ask questions, share opinions, bring up "
            "new ideas and move the scene forward. Don't just agree with {user} or repeat what they said.\n"
            "- Write only {char}'s own words and actions. Never write {user}'s lines, thoughts or actions, and "
            "never decide what {user} does.\n"
            "- Remember details from earlier in the conversation and refer back to them naturally.\n"
            "- Vary your sentences; don't reuse phrases or openings from your earlier messages.\n"
            "- Text in [square brackets] from {user} is an out-of-character instruction: follow it without "
            "breaking character.\n"
            "- Length: {length}."),
        "length": {
            "adventure": {"short": "one short paragraph, about 40-80 words",
                          "medium": "one or two paragraphs, about 80-160 words",
                          "long": "two to four paragraphs, about 160-300 words"},
            "character": {"short": "one to three sentences",
                          "medium": "one short paragraph, about 40-100 words",
                          "long": "one or two paragraphs, about 100-200 words"},
        },
        "sec_instructions": "## Additional instructions from the player",
        "sec_player": "## The player character",
        "sec_plot": "## Plot essentials (always true)",
        "sec_user": "## About {user} (the person you are talking with)",
        "sec_scenario": "## Scenario",
        "sec_examples": "## How {char} talks (examples)",
        "sec_world": "## World info",
        "sec_summary": {"adventure": "## The story so far", "character": "## The conversation so far"},
        "sec_memories": "## Earlier events (oldest first)",
        "notes": {"adventure": "Notes for the narrator (background, not part of the story):",
                  "character": "Notes for {char} (background, not part of the conversation):"},
        "author_note": "Author's note:",
        "begin": {"adventure": "(The story begins.)", "character": "(The conversation begins.)"},
        "resume": {"adventure": "(The story continues.)", "character": "(The conversation continues.)"},
        "continue": {"adventure": "(Continue the story from where it left off.)",
                     "character": "(Continue: write {char}'s next message.)"},
        "opening": {
            "adventure": ("(Begin the story. Write the opening scene in second person: introduce the player "
                          "character and the setting through concrete details, establish an immediate hook or "
                          "problem, and end at a moment that invites the player's first action.)"),
            "character": ("(Start the conversation: write {char}'s first message to {user}, in character, "
                          "setting the scene with an action and a line of dialogue.)"),
        },
        "say": '> You say "{text}"',
        "summarize": (
            "You keep the long-term memory of an interactive story. Summarize the passage you are given into a "
            "memory entry the narrator can rely on later.\n\n"
            "Rules:\n"
            "- Past tense. {who}\n"
            "- Keep what matters for continuing the story: key events and their outcomes, decisions, promises "
            "and threats, injuries, items gained or lost, relationships and how they changed, secrets revealed, "
            "unresolved threads, and where the characters are at the end.\n"
            "- Keep names, places and specific details exact. Never invent anything that is not in the passage.\n"
            "- Be concise: at most {words} words. Plain text, no headings.\n"
            "- Output only the summary."),
        "summarize_user": ("Earlier memories, for context only (don't repeat them):\n{memories}\n\n"
                           "Passage to summarize:\n<<<\n{passage}\n>>>"),
        "consolidate": (
            "You keep the long-term memory of an interactive story. Merge the current summary and the memory "
            "entries below into one updated summary of the story so far. Past tense. {who} Keep the important "
            "facts, names, relationships, open threads and the current situation; drop minor details. At most "
            "{words} words. Plain text, no headings. Output only the summary."),
        "consolidate_user": "Current summary:\n{summary}\n\nMemory entries, oldest first:\n{memories}",
        "who": {"adventure": "Refer to the player character ({name}) as \"you\", like the story does.",
                "character": "Refer to the participants by name: {char} and {user}."},
        "none": "(none)",
        "idea_system": ("You design premises for interactive stories. Be original and specific: fresh settings, "
                        "vivid names, a clear hook. Avoid generic clichés. Answer with JSON only."),
        "idea": {
            "adventure": (
                "Create a text adventure from this idea: \"{idea}\". The player character is called {name}.\n"
                "JSON fields:\n"
                "- title: a short, evocative title\n"
                "- plot: 3-6 sentences: the setting, its tone and rules, the central conflict and what is at stake\n"
                "- persona: 1-2 sentences about who {name} is: role, skills, what drives them\n"
                "- style: one line of style guidance for the narrator (e.g. \"Gritty noir, short sentences, dry "
                "humor\")\n"
                "- cards: 3-5 important characters, places, factions or objects, each with name, keys (1-3 "
                "lowercase words likely to appear in the story, e.g. the name) and text (1-3 sentences)"),
            "character": (
                "Create a roleplay character from this idea: \"{idea}\". The user is called {user}.\n"
                "JSON fields:\n"
                "- name: the character's name\n"
                "- title: a short title for the chat (e.g. the name and a role)\n"
                "- about: a character sheet in 5-9 sentences: appearance, personality, background, how they "
                "speak, what they want, quirks, a secret\n"
                "- scenario: 1-3 sentences: where and how the conversation takes place, and the character's "
                "relationship to {user}\n"
                "- style: one line of style guidance\n"
                "- examples: two short example exchanges showing how the character talks, as lines starting "
                "with \"{user}:\" and with the character's name"),
        },
    },
    "hu": {
        "narrator": (
            "Egy interaktív történet mesélője vagy, egy olyan szöveges kalandjátéké, mint az AI Dungeon. A "
            "játékossal közösen írjátok a történetet, körről körre.\n\n"
            "A körök:\n"
            "- A \">\" jellel kezdődő sor a játékos köre: amit a szereplője tesz vagy mond. Írd le, mi történik "
            "ennek nyomán.\n"
            "- A játékos \">\" nélküli szövege a történet része: kezeld tényként, és onnan folytasd.\n"
            "- A játékos [szögletes zárójelbe] tett szövege neked szóló, játékon kívüli utasítás: kövesd, de a "
            "történetben ne említsd.\n\n"
            "Így írj:\n"
            "- Második személyben, jelen időben („Belököd az ajtót. Hideg levegő csap ki.”), ahogy a történet "
            "eddig is szólt.\n"
            "- Legyél élénk és konkrét: érzéki részletek, egyedi nevek, meglepő, de logikus következmények. "
            "Mutasd, ne magyarázd. Kerüld a közhelyeket és a sablonos fordulatokat.\n"
            "- A világ álljon ellen: egy próbálkozás kudarcot vallhat vagy ára lehet, a helyzettől és a "
            "szereplő képességeitől függően. A többi szereplőnek saját célja, titka és hangja van: vitatkoznak, "
            "hazudnak, viccelődnek, segítenek vagy elárulnak.\n"
            "- Vidd előre a történetet bonyodalmakkal, felfedezésekkel, döntésekkel és tétekkel. Ne oldj meg "
            "mindent egyszerre; hagyj nyitott szálakat.\n"
            "- Soha ne dönts, ne beszélj és ne cselekedj a játékos szereplője helyett azon túl, amit a köre "
            "leír. Olyan ponton állj meg, ahol a játékos léphet.\n"
            "- Maradj következetes a lenti tényekkel, szereplőkkel, hangvétellel és a világ szabályaival. Ne "
            "foglald össze és ne ismételd a korábbi szöveget; haladj előre.\n"
            "- Csak a történet szövegét írd: ne legyen cím, felsorolás, választási lista, megjegyzés, se olyan "
            "kérdés a játékoshoz, mint „Mit teszel?”.\n"
            "- Természetes, választékos, hibátlan magyarsággal írj. Kerüld a tükörfordításokat, az angolos "
            "szerkezeteket és a kitalált vagy ritka szavakat; ha nem vagy biztos egy szóban, válassz "
            "egyszerűbbet és gyakoribbat.\n"
            "- Terjedelem: {length}."),
        "character": (
            "Te {char} vagy, egy folyamatban lévő szerepjátékos beszélgetés szereplője; a beszélgetőtársad "
            "{user}. Írd meg {char} következő üzenetét.\n\n"
            "Ki vagy:\n{about}\n\n"
            "Szabályok:\n"
            "- Mindig maradj a szerepedben: {char} hangja, szókincse, véleménye, hangulata és titkai szerint. "
            "Soha ne mondd, hogy mesterséges intelligencia, nyelvi modell vagy egy történet szereplője vagy.\n"
            "- {char} szavait sima szövegként írd, a cselekvéseket, gesztusokat és arckifejezéseket csillagok "
            "közé, például: *hátradől, és ferdén elvigyorodik*.\n"
            "- Legyél élénk és kezdeményező: reagálj valódi érzelmekkel, ugrass, kérdezz, mondj véleményt, hozz "
            "fel új ötleteket, és vidd előre a jelenetet. Ne csak helyeselj, és ne ismételd vissza, amit a "
            "másik mondott.\n"
            "- Csak {char} saját szavait és tetteit írd. Soha ne írd meg a beszélgetőtársad mondatait, "
            "gondolatait vagy tetteit, és ne dönts helyette.\n"
            "- Emlékezz a beszélgetés korábbi részleteire, és természetesen utalj vissza rájuk.\n"
            "- Változatosan fogalmazz; ne ismételd a korábbi üzeneteid fordulatait és kezdését.\n"
            "- A beszélgetőtársad [szögletes zárójelbe] tett szövege játékon kívüli utasítás: kövesd, de "
            "maradj a szerepedben.\n"
            "- Természetes, választékos, hibátlan magyarsággal írj. Kerüld a tükörfordításokat, az angolos "
            "szerkezeteket és a kitalált vagy ritka szavakat; ha nem vagy biztos egy szóban, válassz "
            "egyszerűbbet és gyakoribbat.\n"
            "- Terjedelem: {length}."),
        "length": {
            "adventure": {"short": "egy rövid bekezdés, kb. 30-60 szó",
                          "medium": "egy-két bekezdés, kb. 60-120 szó",
                          "long": "két-négy bekezdés, kb. 120-230 szó"},
            "character": {"short": "egy-három mondat",
                          "medium": "egy rövid bekezdés, kb. 30-80 szó",
                          "long": "egy-két bekezdés, kb. 80-160 szó"},
        },
        "sec_instructions": "## További utasítások a játékostól",
        "sec_player": "## A játékos szereplője",
        "sec_plot": "## A lényeg (mindig érvényes)",
        "sec_user": "## A beszélgetőtársad: {user}",
        "sec_scenario": "## A helyzet",
        "sec_examples": "## Így beszél {char} (példák)",
        "sec_world": "## A világ",
        "sec_summary": {"adventure": "## Az eddigi történet", "character": "## Az eddigi beszélgetés"},
        "sec_memories": "## Korábbi események (a legrégebbitől)",
        "notes": {"adventure": "Háttér a mesélőnek (nem a történet része):",
                  "character": "Háttér {char} számára (nem a beszélgetés része):"},
        "author_note": "Szerzői megjegyzés:",
        "begin": {"adventure": "(Kezdődik a történet.)", "character": "(Kezdődik a beszélgetés.)"},
        "resume": {"adventure": "(Folytatódik a történet.)", "character": "(Folytatódik a beszélgetés.)"},
        "continue": {"adventure": "(Folytasd a történetet onnan, ahol abbamaradt.)",
                     "character": "(Folytasd: írd meg {char} következő üzenetét.)"},
        "opening": {
            "adventure": ("(Kezdd el a történetet. Írd meg a nyitó jelenetet második személyben: konkrét "
                          "részletekkel mutasd be a játékos szereplőjét és a helyszínt, teremts azonnali "
                          "bonyodalmat vagy rejtélyt, és olyan ponton állj meg, ahol a játékos megteheti az első "
                          "lépését.)"),
            "character": ("(Kezdd el a beszélgetést: írd meg {char} első üzenetét a szerepedben, egy "
                          "cselekvéssel és egy megszólalással megteremtve a jelenetet.)"),
        },
        "say": "> Azt mondod: „{text}”",
        "summarize": (
            "Egy interaktív történet hosszú távú emlékezetét vezeted. A kapott részletből írj olyan "
            "emlékbejegyzést, amire a mesélő később támaszkodhat.\n\n"
            "Szabályok:\n"
            "- Múlt időben írj. {who}\n"
            "- Tartsd meg, ami a folytatáshoz fontos: a fő eseményeket és kimenetelüket, a döntéseket, "
            "ígéreteket és fenyegetéseket, sérüléseket, megszerzett vagy elvesztett tárgyakat, a kapcsolatokat "
            "és azok változását, a kiderült titkokat, a nyitott szálakat, és hogy a végén hol vannak a "
            "szereplők.\n"
            "- A neveket, helyeket és konkrét részleteket pontosan őrizd meg. Semmit ne találj ki, ami nincs a "
            "részletben.\n"
            "- Légy tömör: legfeljebb {words} szó. Sima szöveg, cím nélkül, természetes, helyes magyarsággal.\n"
            "- Csak az összefoglalót írd le."),
        "summarize_user": ("Korábbi emlékek, csak tájékoztatásul (ne ismételd őket):\n{memories}\n\n"
                           "Az összefoglalandó részlet:\n<<<\n{passage}\n>>>"),
        "consolidate": (
            "Egy interaktív történet hosszú távú emlékezetét vezeted. Fésüld össze az eddigi összefoglalót és a "
            "lenti emlékbejegyzéseket egyetlen, frissített összefoglalóvá az eddigi történetről. Múlt időben. "
            "{who} Tartsd meg a fontos tényeket, neveket, kapcsolatokat, nyitott szálakat és a jelenlegi "
            "helyzetet; a mellékes részleteket hagyd el. Legfeljebb {words} szó. Sima szöveg, cím nélkül. Csak "
            "az összefoglalót írd le."),
        "consolidate_user": "Az eddigi összefoglaló:\n{summary}\n\nEmlékbejegyzések, a legrégebbitől:\n{memories}",
        "who": {"adventure": "A játékos szereplőjét ({name}) második személyben említsd („te”), ahogy a "
                             "történet is.",
                "character": "A szereplőket nevükön említsd: {char} és {user}."},
        "none": "(nincs)",
        "idea_system": ("Interaktív történetek alapötleteit dolgozod ki. Legyél eredeti és konkrét: friss "
                        "helyszínek, élénk nevek, egyértelmű bonyodalom. Kerüld a sablonos közhelyeket. Csak "
                        "JSON-nal válaszolj, és minden értéket természetes, helyes magyarsággal írj."),
        "idea": {
            "adventure": (
                "Készíts szöveges kalandot ebből az ötletből: „{idea}”. A játékos szereplőjének neve: {name}.\n"
                "JSON-mezők:\n"
                "- title: rövid, hangulatos cím\n"
                "- plot: 3-6 mondat: a helyszín, a hangvétele és szabályai, a fő konfliktus és a tét\n"
                "- persona: 1-2 mondat a játékos szereplőjéről: szerep, képességek, mi hajtja\n"
                "- style: egysoros stílusútmutató a mesélőnek (pl. „Kemény noir, rövid mondatok, száraz humor”)\n"
                "- cards: 3-5 fontos szereplő, helyszín, csoport vagy tárgy; mindegyiknek name, keys (1-3 "
                "kisbetűs szó, ami valószínűleg előfordul a történetben, pl. a név) és text (1-3 mondat)"),
            "character": (
                "Készíts szerepjátékos karaktert ebből az ötletből: „{idea}”. A felhasználó neve: {user}.\n"
                "JSON-mezők:\n"
                "- name: a karakter neve\n"
                "- title: rövid cím a beszélgetéshez (pl. a név és egy szerep)\n"
                "- about: karakterlap 5-9 mondatban: külső, személyiség, háttér, hogyan beszél, mit akar, "
                "furcsaságok, egy titok\n"
                "- scenario: 1-3 mondat: hol és hogyan zajlik a beszélgetés, és mi a karakter viszonya a "
                "felhasználóhoz\n"
                "- style: egysoros stílusútmutató\n"
                "- examples: két rövid példaváltás, ami megmutatja, hogyan beszél a karakter; a sorok "
                "„{user}:” és a karakter nevével kezdődjenek"),
        },
    },
}

IDEA_SCHEMA = {
    "adventure": {
        "type": "object",
        "properties": {
            "title": {"type": "string"}, "plot": {"type": "string"}, "persona": {"type": "string"},
            "style": {"type": "string"},
            "cards": {"type": "array", "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "keys": {"type": "array", "items": {"type": "string"}},
                               "text": {"type": "string"}},
                "required": ["name", "keys", "text"]}},
        },
        "required": ["title", "plot", "persona", "style", "cards"],
    },
    "character": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "title": {"type": "string"}, "about": {"type": "string"},
                       "scenario": {"type": "string"}, "style": {"type": "string"}, "examples": {"type": "string"}},
        "required": ["name", "title", "about", "scenario", "style", "examples"],
    },
}

ACTIVITY = {"write": "Írom a folytatást", "retry": "Írok egy másik változatot", "idea": "Megtervezem a történetet",
            "summarize": "Összefoglalom a korábbi részt"}


# --- kis segédek ------------------------------------------------------------------------------------------

def new_id() -> str:
    return secrets.token_hex(4)


def clip(value: Any, limit: int) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()[:limit]


def estimate(text: str, lang: str) -> int:
    """Óvatos tokenbecslés (angolul kb. 4 karakter egy token, magyarul kevesebb)."""
    return int(len(text or "") / CHARS_PER_TOKEN.get(lang, 2.5)) + 1


def predict_tokens(mode: str, length: str, lang: str) -> int:
    words = WORDS.get(mode, WORDS["adventure"]).get(length, WORDS[mode]["medium"])
    return int(words * TOKENS_PER_WORD.get(lang, 2.3)) + 60


def capitalize(text: str) -> str:
    return text[:1].upper() + text[1:]


# Egyes szám első személy → második (AI Dungeon-szerűen: "I open the door" → "You open the door").
# A sorrend számít: előbb az összetett alakok ("I am"), csak a végén a magában álló "I".
_PRONOUNS_EN = [
    (re.compile(r"\bI am\b", re.IGNORECASE), "you are"), (re.compile(r"\bI'm\b", re.IGNORECASE), "you're"),
    (re.compile(r"\bI've\b", re.IGNORECASE), "you've"), (re.compile(r"\bI'll\b", re.IGNORECASE), "you'll"),
    (re.compile(r"\bI'd\b", re.IGNORECASE), "you'd"), (re.compile(r"\bmyself\b", re.IGNORECASE), "yourself"),
    (re.compile(r"\bmine\b", re.IGNORECASE), "yours"), (re.compile(r"\bmy\b", re.IGNORECASE), "your"),
    (re.compile(r"\bme\b", re.IGNORECASE), "you"), (re.compile(r"\bI\b"), "you")]
_END_PUNCT = ".!?…\"'”»*)"


def format_turn(kind: str, text: str, lang: str) -> str:
    """A játékos köre a történetben: "> You open the door." (AI Dungeon-szerűen), a mondás idézőjelben."""
    text = clip(text, MAX_TEXT)
    if kind not in ("do", "say"):
        return text
    text = re.sub(r"^\s*>\s*", "", text)
    if kind == "say":
        text = re.sub(r"^(?:you say|i say|say|mondom|azt mondom|mondod)\s*:?\s*", "", text, flags=re.IGNORECASE)
        text = capitalize(text.strip().strip('"“”„»«\'').strip())
        if text and text[-1] not in ".!?…":
            text += "."
        return PROMPTS[lang]["say"].format(text=text)
    if lang == "en":
        for pattern, repl in _PRONOUNS_EN:
            text = pattern.sub(repl, text)
        if not re.match(r"you\b", text, re.IGNORECASE):
            first = text.split(" ", 1)[0]
            if first[:1].isupper() and not first.isupper():
                text = text[:1].lower() + text[1:]
            text = "you " + text
    text = capitalize(text)
    if text and text[-1] not in _END_PUNCT:
        text += "."
    return "> " + text


_SENTENCE_END_RE = re.compile(r"[.!?…][\"'”’»)\]*]*(?=\s|$)")
_WHAT_NOW_RE = re.compile(
    r"\s*(?:What (?:do|will|would) you do(?: next| now)?\?|Mit teszel(?: most)?\?|Mit csinálsz(?: most)?\?|"
    r"Mi a következő lépésed\?)\s*$", re.IGNORECASE)


def trim_to_sentence(text: str) -> str:
    """Ha a válasz a hosszkorlát miatt (vagy az Állj gombra) félbemaradt, az utolsó teljes mondatig tartja
    meg; ha nincs teljes mondat, három ponttal jelzi, hogy itt megszakadt."""
    text = text.rstrip()
    ends = list(_SENTENCE_END_RE.finditer(text))
    if ends and ends[-1].end() >= 12:
        return text[:ends[-1].end()].rstrip()
    return text + "…" if text and text[-1] not in _END_PUNCT else text


def clean_output(text: str, story: dict[str, Any], cut: bool = False) -> str:
    """A modell válaszából a történet szövege: címkék, a játékos helyett megírt sorok és a félbemaradt mondat
    nélkül."""
    text = strip_think(text or "").replace("\r", "").strip()
    char = (story.get("character") or {}).get("name") or ""
    user = (story.get("persona") or {}).get("name") or templates.DEFAULT_NAME
    labels = ["narrator", "mesélő", "story", "történet"]
    if char:
        labels += [char, char.split()[0]]
    label_re = re.compile(r"^\s*\**\s*(?:" + "|".join(re.escape(x) for x in labels) + r")\s*\**\s*:(?:\*\*)?\s*",
                          re.IGNORECASE)
    text = label_re.sub("", text, count=1)
    user_re = re.compile(r"^\s*\**\s*(?:" + re.escape(user) + r"|user|you|te|{{user}})\s*\**\s*:", re.IGNORECASE)
    kept = []
    for line in text.split("\n"):
        stripped = line.strip()
        if story.get("mode") == "adventure" and stripped.startswith(">"):
            break        # a játékos következő körét nem írhatja meg helyette
        if story.get("mode") == "character" and user_re.match(stripped):
            break
        kept.append(line)
    text = "\n".join(kept).strip()
    if cut:
        text = trim_to_sentence(text)
    if story.get("mode") == "adventure":
        trimmed = _WHAT_NOW_RE.sub("", text).rstrip()
        text = trimmed or text
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clean_summary(text: str) -> str:
    text = strip_think(text or "").strip()
    text = re.sub(r"^(?:#+\s*)?(?:summary|összefoglaló|összefoglalás|memory|emlék)\s*:?\s*\n?", "", text,
                  flags=re.IGNORECASE).strip()
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    return clip(text.strip("<> \n"), MAX_FIELD)


def card_hits(card: dict[str, Any], folded: str) -> bool:
    for key in card.get("keys") or []:
        key = fold(key).strip()
        if key and re.search(r"(?<!\w)" + re.escape(key), folded):
            return True
    return False


def _keys(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    keys = []
    for key in value if isinstance(value, list) else []:
        key = clip(key, 40).lower()
        if key and key not in keys:
            keys.append(key)
    return keys[:8]


def _card(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = clip(raw.get("name"), 80)
    text = clip(raw.get("text"), 2000)
    if not name and not text:
        return None
    card_id = str(raw.get("id") or "")
    return {"id": card_id if ID_RE.match(card_id) else new_id(), "name": name, "keys": _keys(raw.get("keys")),
            "text": text, "always": bool(raw.get("always"))}


def _entry(kind: str, text: str, mem: bool = False) -> dict[str, Any]:
    entry: dict[str, Any] = {"id": new_id(), "kind": kind, "text": text, "ts": time.time(), "mem": mem}
    if kind == "ai":
        entry["alts"], entry["alt"] = [text], 0
    return entry


def blank_story(mode: str, lang: str) -> dict[str, Any]:
    now = time.time()
    return {"v": 1, "id": new_id(), "rev": 0, "mode": mode, "language": lang, "title": "", "created": now,
            "updated": now, "template": "", "idea": "", "plot": "", "note": "", "instructions": "",
            "persona": {"name": templates.DEFAULT_NAME, "about": ""},
            "character": {"name": "", "about": "", "examples": ""},
            "cards": [], "memories": [], "summary": "", "entries": [], "redo": [],
            "settings": {"creativity": DEFAULT_CREATIVITY[lang], "length": "medium"}}


def normalize(data: Any) -> dict[str, Any]:
    """Egy beolvasott (esetleg régebbi vagy kézzel szerkesztett) történet ellenőrzött alakja."""
    if not isinstance(data, dict) or not ID_RE.match(str(data.get("id") or "")):
        raise StoryError("Érvénytelen történetfájl.")
    mode = data.get("mode") if data.get("mode") in MODES else "adventure"
    lang = data.get("language") if data.get("language") in LANGS else "en"
    story = blank_story(mode, lang)
    story["id"] = data["id"]
    story["rev"] = int(data.get("rev") or 0) if isinstance(data.get("rev"), int) else 0
    for key in ("created", "updated"):
        if isinstance(data.get(key), (int, float)):
            story[key] = float(data[key])
    for key, limit in (("title", 200), ("template", 40), ("idea", 2000), ("plot", MAX_FIELD), ("note", 2000),
                       ("instructions", MAX_FIELD), ("summary", MAX_FIELD)):
        story[key] = clip(data.get(key), limit)
    persona = data.get("persona") if isinstance(data.get("persona"), dict) else {}
    story["persona"] = {"name": clip(persona.get("name"), 60) or templates.DEFAULT_NAME,
                        "about": clip(persona.get("about"), MAX_FIELD)}
    char = data.get("character") if isinstance(data.get("character"), dict) else {}
    story["character"] = {"name": clip(char.get("name"), 60), "about": clip(char.get("about"), MAX_FIELD),
                          "examples": clip(char.get("examples"), MAX_FIELD)}
    story["cards"] = [c for c in (_card(raw) for raw in data.get("cards") or []) if c][:MAX_CARDS]
    memories = []
    for raw in data.get("memories") or []:
        if isinstance(raw, dict) and clip(raw.get("text"), MAX_FIELD):
            memory_id = str(raw.get("id") or "")
            memories.append({"id": memory_id if ID_RE.match(memory_id) else new_id(),
                             "text": clip(raw.get("text"), MAX_FIELD),
                             "created": float(raw.get("created") or time.time()),
                             "first": str(raw.get("first") or ""), "last": str(raw.get("last") or ""),
                             "count": int(raw.get("count") or 0), "auto": bool(raw.get("auto", True))})
    story["memories"] = memories

    def entries(values: Any) -> list[dict[str, Any]]:
        out = []
        for raw in values or []:
            if not isinstance(raw, dict) or raw.get("kind") not in ("ai", "do", "say", "story", "user"):
                continue
            entry = _entry(raw["kind"], clip(raw.get("text"), MAX_TEXT), bool(raw.get("mem")))
            entry_id = str(raw.get("id") or "")
            if ID_RE.match(entry_id):
                entry["id"] = entry_id
            if isinstance(raw.get("ts"), (int, float)):
                entry["ts"] = float(raw["ts"])
            if raw["kind"] == "ai":
                alts = [clip(a, MAX_TEXT) for a in raw.get("alts") or [] if isinstance(a, str)] or [entry["text"]]
                alt = int(raw.get("alt") or 0) if isinstance(raw.get("alt"), int) else 0
                alt = max(0, min(alt, len(alts) - 1))
                entry["alts"], entry["alt"] = alts[-MAX_ALTS:], min(alt, len(alts[-MAX_ALTS:]) - 1)
                entry["text"] = entry["alts"][entry["alt"]]
            out.append(entry)
        return out

    story["entries"] = entries(data.get("entries"))
    story["redo"] = entries(data.get("redo"))[-MAX_REDO:]
    settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    try:
        creativity = float(settings.get("creativity", DEFAULT_CREATIVITY[lang]))
    except (TypeError, ValueError):
        creativity = DEFAULT_CREATIVITY[lang]
    story["settings"] = {"creativity": round(max(0.3, min(1.5, creativity)), 2),
                         "length": settings.get("length") if settings.get("length") in LENGTHS else "medium"}
    return story


def brief(story: dict[str, Any]) -> dict[str, Any]:
    """A történetek listájához: cím, mód, nyelv, utolsó módosítás és egy kis részlet a végéből."""
    last = next((e["text"] for e in reversed(story["entries"]) if e["kind"] == "ai"), "")
    snippet = re.sub(r"\s+", " ", last.replace("*", "")).strip()
    if len(snippet) > SNIPPET:
        snippet = "…" + snippet[-SNIPPET:].split(" ", 1)[-1]
    return {"id": story["id"], "title": story["title"] or "Névtelen történet", "mode": story["mode"],
            "language": story["language"], "updated": story["updated"], "created": story["created"],
            "entries": len(story["entries"]), "snippet": snippet, "character": story["character"]["name"]}


# --- tároló ---------------------------------------------------------------------------------------------

class StoryStore:
    """A történetek JSON-fájlokban (történetenként egy), az adatmappában, hogy frissítéskor megmaradjanak."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: dict[str, dict[str, Any]] = {}

    def _path(self, story_id: str) -> Path:
        if not ID_RE.match(str(story_id or "")):
            raise StoryError("Nincs ilyen történet.")
        return self.folder / f"{story_id}.json"

    def _cached(self, story_id: str) -> dict[str, Any]:
        cached = self._cache.get(story_id)
        if cached is None:
            path = self._path(story_id)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise StoryError("Nincs ilyen történet (lehet, hogy közben törölted).") from None
            except (OSError, ValueError) as exc:
                raise StoryError(f"A történet fájlja sérült ({path.name}: {exc}).") from None
            cached = self._cache[story_id] = normalize(data)
        return cached

    def load(self, story_id: str) -> dict[str, Any]:
        """A történet másolata (a módosítás az edit-tel menjen, hogy elmentődjön)."""
        with self._lock:
            return copy.deepcopy(self._cached(story_id))

    def save(self, story: dict[str, Any]) -> None:
        with self._lock:
            story["updated"] = time.time()
            story["rev"] = int(story.get("rev") or 0) + 1   # a felület ebből tudja, melyik az újabb
            path = self._path(story["id"])
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(story, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, path)
            self._cache[story["id"]] = copy.deepcopy(story)

    def edit(self, story_id: str, change: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
        """Betölti, módosítja és elmenti (egyszerre csak egy szál nyúlhat hozzá)."""
        with self._lock:
            story = self.load(story_id)
            change(story)
            self.save(story)
            return story

    def delete(self, story_id: str) -> None:
        with self._lock:
            path = self._path(story_id)
            self._cache.pop(story_id, None)
            try:
                path.unlink()
            except FileNotFoundError:
                raise StoryError("Nincs ilyen történet.") from None

    def briefs(self) -> list[dict[str, Any]]:
        """A történetek listája röviden, a legutóbb módosított elöl (másolás nélkül, mert gyakran kell)."""
        out = []
        with self._lock:
            for path in self.folder.glob("*.json"):
                try:
                    out.append(brief(self._cached(path.stem)))
                except StoryError:
                    continue
        return sorted(out, key=lambda b: b["updated"], reverse=True)


# --- a mesélő ----------------------------------------------------------------------------------------------

class CreativeDesk:
    """A Kreatív menü: a történetek kezelése (a felület szálairól) és a modell munkája (az ágens-szálon).

    `publish`: események a felületnek (élő írás, változások); `language`: az új történetek alapnyelve."""

    def __init__(self, llm: OllamaClient, folder: Path, publish: Callable[[dict[str, Any]], None] | None = None,
                 language: str = "en", default_ctx: int = 8192):
        self.llm = llm
        self.store = StoryStore(folder)
        self.publish = publish or (lambda _event: None)
        self.language = language if language in LANGS else "en"
        self.default_ctx = int(default_ctx or 8192)
        self._lock = threading.Lock()
        self._queued: dict[str, int] = {}
        self._current: dict[str, Any] | None = None
        self._seq = 0      # sorszám: a felület ebből tudja, melyik állapot az újabb

    # --- állapot (sorban áll, épp ír) ---------------------------------------------------------------

    def queue(self, story_id: str) -> None:
        with self._lock:
            self._queued[story_id] = self._queued.get(story_id, 0) + 1
        self._publish_status(story_id)

    def finished(self, story_id: str) -> None:
        with self._lock:
            left = self._queued.get(story_id, 0) - 1
            if left > 0:
                self._queued[story_id] = left
            else:
                self._queued.pop(story_id, None)
            if self._current and self._current.get("id") == story_id:
                self._current = None
        self._publish_status(story_id)

    def busy(self, story_id: str) -> bool:
        with self._lock:
            return bool(self._queued.get(story_id)) or bool(self._current and self._current.get("id") == story_id)

    def status(self, story_id: str) -> dict[str, Any]:
        with self._lock:
            queued = self._queued.get(story_id, 0)
            current = self._current if self._current and self._current.get("id") == story_id else None
        text = current.get("text", "") if current else ("Sorra vár…" if queued else "")
        return {"busy": bool(queued or current), "text": text, "entry": current.get("entry") if current else None}

    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _publish_status(self, story_id: str) -> None:
        self.publish({"type": "creative_busy", "id": story_id, "seq": self.next_seq(), **self.status(story_id)})

    def _activity(self, story_id: str, text: str, entry: str | None = None) -> None:
        with self._lock:
            self._current = {"id": story_id, "text": text, "entry": entry}
        self._publish_status(story_id)

    def require_idle(self, story_id: str) -> None:
        if self.busy(story_id):
            raise StoryError("Épp ezen a történeten dolgozom. Várd meg, vagy állítsd le (Állj).")

    # --- a felületnek ---------------------------------------------------------------------------------

    def context_size(self) -> int:
        return int(getattr(self.llm, "num_ctx", 0) or self.default_ctx)

    def stories(self) -> list[dict[str, Any]]:
        return self.store.briefs()

    def public(self, story: dict[str, Any]) -> dict[str, Any]:
        out = copy.deepcopy(story)
        out["redo"] = len(story["redo"])
        out["usage"] = self.usage(story)
        out["status_seq"] = self.next_seq()
        out["status"] = self.status(story["id"])
        return out

    def get(self, story_id: str) -> dict[str, Any]:
        return self.public(self.store.load(story_id))

    def _saved(self, story: dict[str, Any]) -> dict[str, Any]:
        public = self.public(story)
        self.publish({"type": "creative_story", "story": public})
        self.publish({"type": "creative_list", "stories": self.stories()})
        return public

    def usage(self, story: dict[str, Any]) -> dict[str, Any]:
        """Mennyit foglal a kontextusból (becslés): utasítások, emlékek, a szó szerinti előzmény."""
        lang = story["language"]
        ctx = self.context_size()
        verbatim = [e for e in story["entries"] if not e.get("mem")]
        memory = estimate(story["summary"], lang) * bool(story["summary"]) + sum(
            estimate(m["text"], lang) for m in story["memories"])
        system = estimate(self.system_prompt(story), lang)
        return {"context": ctx, "system": max(0, system - memory), "memory": memory,
                "verbatim": sum(estimate(e["text"], lang) + MESSAGE_OVERHEAD for e in verbatim),
                "limit": int(ctx * HISTORY_SOFT), "entries": len(story["entries"]),
                "remembered": len(story["entries"]) - len(verbatim)}

    # --- létrehozás és szerkesztés (a felület szálain, modell nélkül) ---------------------------------

    def create(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        """Új történet. Visszaadja a történetet, és hogy kell-e még modellmunka hozzá ("idea": ötletből
        megtervezni, "write": a nyitó jelenetet vagy az első üzenetet megírni)."""
        mode = payload.get("mode") if payload.get("mode") in MODES else "adventure"
        lang = payload.get("language") if payload.get("language") in LANGS else self.language
        persona = payload.get("persona") if isinstance(payload.get("persona"), dict) else {}
        name = clip(persona.get("name"), 60) or templates.DEFAULT_NAME
        about = clip(persona.get("about"), MAX_FIELD)
        story = blank_story(mode, lang)
        story["persona"] = {"name": name, "about": about}
        key = str(payload.get("template") or "")
        template = templates.find(key)
        need: str | None = None
        if template is not None and (template in templates.ADVENTURES) == (mode == "adventure"):
            self._from_template(story, template, str(payload.get("choice") or ""))
        elif key == "idea":
            idea = clip(payload.get("idea"), 2000)
            if not idea:
                raise StoryError("Írd le pár szóban az ötletet.")
            story.update(template="idea", idea=idea, plot=idea, title=idea if len(idea) <= 60 else idea[:57] + "…")
            need = "idea"
        else:
            story["template"] = "custom"
            if mode == "adventure":
                story["title"] = clip(payload.get("title"), 120) or "Saját kaland"
                story["plot"] = clip(payload.get("plot"), MAX_FIELD)
                opening = clip(payload.get("opening"), MAX_TEXT)
                if opening:
                    story["entries"] = [_entry("ai", opening)]
                elif not story["plot"]:
                    raise StoryError("Írd le, miről szóljon (lényeg), vagy adj meg nyitó szöveget.")
                else:
                    need = "write"
            else:
                char = payload.get("character") if isinstance(payload.get("character"), dict) else {}
                story["character"] = {"name": clip(char.get("name"), 60), "about": clip(char.get("about"), MAX_FIELD),
                                      "examples": clip(char.get("examples"), MAX_FIELD)}
                if not story["character"]["name"] or not story["character"]["about"]:
                    raise StoryError("Add meg a karakter nevét és pár mondatban, milyen.")
                story["title"] = clip(payload.get("title"), 120) or story["character"]["name"]
                story["plot"] = clip(payload.get("plot"), MAX_FIELD)
                greeting = clip(char.get("greeting"), MAX_TEXT)
                if greeting:
                    story["entries"] = [_entry("ai", greeting)]
                else:
                    need = "write"
        self.store.save(story)
        self._saved(story)
        return self.public(story), need

    @staticmethod
    def _from_template(story: dict[str, Any], template: dict[str, Any], choice: str) -> None:
        lang, mode = story["language"], story["mode"]
        text = template[lang]
        name = story["persona"]["name"]
        values = {"{name}": name, "{Name}": capitalize(name), "{user}": name}
        roles = text.get("roles") or {}
        if roles:
            role = roles.get(choice) or next(iter(roles.values()))
            values.update({"{role}": role, "{Role}": capitalize(role)})

        def fill(value: str) -> str:
            for placeholder, replacement in values.items():
                value = value.replace(placeholder, replacement)
            return value

        story["template"] = template["key"]
        story["title"] = fill(text["title"])
        story["plot"] = fill(text.get("plot", ""))
        story["note"] = fill(text.get("note", ""))
        if mode == "adventure":
            if not story["persona"]["about"]:
                story["persona"]["about"] = fill(text.get("persona", ""))
            story["cards"] = [c for c in (_card({**card, "text": fill(card["text"])}) for card in text["cards"]) if c]
            story["entries"] = [_entry("ai", fill(text["opening"]))]
        else:
            story["character"] = {"name": text["name"], "about": fill(text["about"]),
                                  "examples": fill(text.get("examples", ""))}
            story["entries"] = [_entry("ai", fill(text["greeting"]))]

    def update(self, story_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """A lényeg, a szerzői megjegyzés, a szereplők, a beállítások stb. átírása."""
        def change(story: dict[str, Any]) -> None:
            for key, limit in (("title", 200), ("plot", MAX_FIELD), ("note", 2000), ("instructions", MAX_FIELD),
                               ("summary", MAX_FIELD)):
                if key in fields:
                    story[key] = clip(fields[key], limit)
            if isinstance(fields.get("persona"), dict):
                persona = fields["persona"]
                if "name" in persona:
                    story["persona"]["name"] = clip(persona["name"], 60) or templates.DEFAULT_NAME
                if "about" in persona:
                    story["persona"]["about"] = clip(persona["about"], MAX_FIELD)
            if isinstance(fields.get("character"), dict):
                for key in ("name", "about", "examples"):
                    if key in fields["character"]:
                        story["character"][key] = clip(fields["character"][key], 60 if key == "name" else MAX_FIELD)
            if fields.get("language") in LANGS:
                story["language"] = fields["language"]
            settings = fields.get("settings") if isinstance(fields.get("settings"), dict) else {}
            if "creativity" in settings:
                try:
                    story["settings"]["creativity"] = round(max(0.3, min(1.5, float(settings["creativity"]))), 2)
                except (TypeError, ValueError):
                    pass
            if settings.get("length") in LENGTHS:
                story["settings"]["length"] = settings["length"]

        return self._saved(self.store.edit(story_id, change))

    def add_turn(self, story_id: str, kind: str, text: str) -> dict[str, Any]:
        """A játékos köre (Tett, Mondás, Történet, vagy karakternél egy üzenet)."""
        self.require_idle(story_id)

        def change(story: dict[str, Any]) -> None:
            kinds = PLAYER_KINDS[story["mode"]]
            turn_kind = kind if kind in kinds else kinds[0]
            body = format_turn(turn_kind, text, story["language"])
            if not body:
                raise StoryError("Üres a szöveg.")
            story["entries"].append(_entry(turn_kind, body))
            story["redo"] = []

        return self._saved(self.store.edit(story_id, change))

    def check_retry(self, story_id: str) -> None:
        story = self.store.load(story_id)
        if not story["entries"] or story["entries"][-1]["kind"] != "ai":
            raise StoryError("A legutolsó rész nem az enyém, nincs mit újraírni. Használd a Folytasd gombot.")

    def undo(self, story_id: str) -> dict[str, Any]:
        self.require_idle(story_id)

        def change(story: dict[str, Any]) -> None:
            if not story["entries"]:
                raise StoryError("Nincs mit visszavonni.")
            story["redo"].append(story["entries"].pop())
            del story["redo"][:-MAX_REDO]

        return self._saved(self.store.edit(story_id, change))

    def redo(self, story_id: str) -> dict[str, Any]:
        self.require_idle(story_id)

        def change(story: dict[str, Any]) -> None:
            if not story["redo"]:
                raise StoryError("Nincs mit visszahozni.")
            story["entries"].append(story["redo"].pop())

        return self._saved(self.store.edit(story_id, change))

    def entry(self, story_id: str, entry_id: str, op: str, text: str = "", index: int = 0) -> dict[str, Any]:
        """Egy bejegyzés átírása, törlése, vagy egy másik változat választása."""
        self.require_idle(story_id)

        def change(story: dict[str, Any]) -> None:
            position = next((i for i, e in enumerate(story["entries"]) if e["id"] == entry_id), None)
            if position is None:
                raise StoryError("Ez a rész már nincs meg.")
            entry = story["entries"][position]
            if op == "delete":
                del story["entries"][position]
            elif op == "edit":
                body = clip(text, MAX_TEXT)
                if not body:
                    raise StoryError("Üres szöveget nem menthetek; ha nem kell, töröld.")
                entry["text"] = body
                if entry["kind"] == "ai":
                    entry["alts"][entry["alt"]] = body
            elif op == "alt" and entry["kind"] == "ai":
                entry["alt"] = max(0, min(int(index), len(entry["alts"]) - 1))
                entry["text"] = entry["alts"][entry["alt"]]
            else:
                raise StoryError("Ismeretlen művelet.")

        return self._saved(self.store.edit(story_id, change))

    def card(self, story_id: str, op: str, raw: dict[str, Any]) -> dict[str, Any]:
        def change(story: dict[str, Any]) -> None:
            cards = story["cards"]
            if op == "add":
                card = _card({**raw, "id": ""}) or _card({"name": "Új kártya", "text": ""})
                if len(cards) >= MAX_CARDS:
                    raise StoryError(f"Legfeljebb {MAX_CARDS} világkártya lehet.")
                cards.append(card)
                return
            position = next((i for i, c in enumerate(cards) if c["id"] == str(raw.get("id") or "")), None)
            if position is None:
                raise StoryError("Ez a kártya már nincs meg.")
            if op == "delete":
                del cards[position]
            elif op == "edit":
                card = _card({**cards[position], **{k: v for k, v in raw.items() if k != "id"}})
                if card is not None:
                    card["id"] = cards[position]["id"]
                    cards[position] = card
            else:
                raise StoryError("Ismeretlen művelet.")

        return self._saved(self.store.edit(story_id, change))

    def memory(self, story_id: str, op: str, memory_id: str = "", text: str = "") -> dict[str, Any]:
        """Az emlékek kézi kezelése: új, átírás, törlés (felejtés), mind törlése."""
        def change(story: dict[str, Any]) -> None:
            memories = story["memories"]
            if op == "add":
                body = clip(text, MAX_FIELD)
                if not body:
                    raise StoryError("Üres emléket nem menthetek.")
                memories.append({"id": new_id(), "text": body, "created": time.time(), "first": "", "last": "",
                                 "count": 0, "auto": False})
            elif op == "clear":
                story["memories"], story["summary"] = [], ""
            else:
                position = next((i for i, m in enumerate(memories) if m["id"] == memory_id), None)
                if position is None:
                    raise StoryError("Ez az emlék már nincs meg.")
                if op == "delete":
                    del memories[position]
                elif op == "edit":
                    body = clip(text, MAX_FIELD)
                    if not body:
                        raise StoryError("Üres emléket nem menthetek; ha nem kell, töröld.")
                    memories[position]["text"] = body
                else:
                    raise StoryError("Ismeretlen művelet.")

        return self._saved(self.store.edit(story_id, change))

    def delete(self, story_id: str) -> None:
        self.require_idle(story_id)
        self.store.delete(story_id)
        self.publish({"type": "creative_list", "stories": self.stories()})

    def export(self, story_id: str) -> tuple[str, str]:
        """A történet Markdown-fájlként: (fájlnév, szöveg)."""
        story = self.store.load(story_id)
        title = story["title"] or "Történet"
        char = story["character"]["name"] or "?"
        user = story["persona"]["name"]
        lines = [f"# {title}", ""]
        for entry in story["entries"]:
            text = entry["text"].strip()
            if story["mode"] == "character":
                lines.append(f"**{char if entry['kind'] == 'ai' else user}:** {text}")
            elif entry["kind"] in ("do", "say"):
                lines.append(text if text.startswith(">") else "> " + text)
            else:
                lines.append(text)
            lines.append("")
        plain = "".join(ch for ch in unicodedata.normalize("NFKD", title) if not unicodedata.combining(ch))
        safe = re.sub(r"[^\w\- ]+", "", plain).strip().replace(" ", "_")[:60] or "tortenet"
        return f"{safe}.md", "\n".join(lines).strip() + "\n"

    # --- a prompt -------------------------------------------------------------------------------------

    def system_prompt(self, story: dict[str, Any]) -> str:
        lang, mode = story["language"], story["mode"]
        prompts = PROMPTS[lang]
        length = prompts["length"][mode][story["settings"]["length"]]
        user = story["persona"]["name"] or templates.DEFAULT_NAME
        char = story["character"]
        if mode == "adventure":
            parts = [prompts["narrator"].replace("{length}", length)]
        else:
            parts = [prompts["character"].replace("{char}", char["name"] or "?").replace("{user}", user)
                     .replace("{about}", char["about"] or "-").replace("{length}", length)]
        if story["instructions"]:
            parts.append(prompts["sec_instructions"] + "\n" + story["instructions"])
        if mode == "adventure":
            about = story["persona"]["about"]
            parts.append(prompts["sec_player"] + "\n" + user + (f": {about}" if about else ""))
            if story["plot"]:
                parts.append(prompts["sec_plot"] + "\n" + story["plot"])
        else:
            if story["persona"]["about"]:
                parts.append(prompts["sec_user"].replace("{user}", user) + "\n" + story["persona"]["about"])
            if story["plot"]:
                parts.append(prompts["sec_scenario"] + "\n" + story["plot"])
            if char["examples"]:
                parts.append(prompts["sec_examples"].replace("{char}", char["name"] or "?") + "\n" + char["examples"])
        always = [c for c in story["cards"] if (c["always"] or not c["keys"]) and c["text"]]
        if always:
            parts.append(prompts["sec_world"] + "\n" + "\n".join(f"- {c['name']}: {c['text']}" for c in always))
        if story["summary"]:
            parts.append(prompts["sec_summary"][mode] + "\n" + story["summary"])
        if story["memories"]:
            parts.append(prompts["sec_memories"] + "\n" + "\n".join(f"- {m['text']}" for m in story["memories"]))
        return "\n\n".join(parts)

    def triggered_cards(self, story: dict[str, Any], recent: str) -> list[dict[str, Any]]:
        """A kulcsszavukkal az utolsó részekben előforduló világkártyák (a keretig)."""
        folded = fold(recent)
        budget = int(self.context_size() * CARD_BUDGET)
        chosen, used = [], 0
        for card in story["cards"]:
            if card["always"] or not card["keys"] or not card["text"] or not card_hits(card, folded):
                continue
            cost = estimate(card["name"] + card["text"], story["language"]) + 4
            if used + cost > budget:
                continue
            chosen.append(card)
            used += cost
        return chosen

    def notes_block(self, story: dict[str, Any], cards: list[dict[str, Any]]) -> str:
        prompts = PROMPTS[story["language"]]
        lines = [f"- {c['name']}: {c['text']}" for c in cards]
        if story["note"]:
            lines.append(f"{prompts['author_note']} {story['note']}")
        if not lines:
            return ""
        head = prompts["notes"][story["mode"]].replace("{char}", story["character"]["name"] or "?")
        return "[" + head + "\n" + "\n".join(lines) + "]"

    def build_messages(self, story: dict[str, Any],
                       entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """A modellnek küldött beszélgetés: rendszerprompt (utasítások, lényeg, emlékek), a szó szerinti
        előzmény (amennyi belefér), a végén a háttér (kártyák, szerzői megjegyzés) és a mostani kör."""
        lang, mode = story["language"], story["mode"]
        prompts = PROMPTS[lang]
        char = story["character"]["name"] or "?"
        user = story["persona"]["name"] or templates.DEFAULT_NAME
        system = self.system_prompt(story)
        verbatim = [e for e in entries if not e.get("mem")]
        recent = " ".join(e["text"] for e in verbatim[-CARD_SCAN:])
        block = self.notes_block(story, self.triggered_cards(story, recent))
        ctx = self.context_size()
        reserve = predict_tokens(mode, story["settings"]["length"], lang) + 96
        available = int(ctx * 0.95) - reserve - estimate(system, lang) - estimate(block, lang) - 64
        chosen: list[dict[str, Any]] = []
        used = 0
        for entry in reversed(verbatim):
            cost = estimate(entry["text"], lang) + MESSAGE_OVERHEAD
            if chosen and used + cost > available:
                break
            if not chosen and cost > available:   # egyetlen óriási bejegyzés: a vége fér bele
                keep = max(200, int(max(available, 100) * CHARS_PER_TOKEN[lang]))
                entry = {**entry, "text": "…" + entry["text"][-keep:]}
                cost = estimate(entry["text"], lang) + MESSAGE_OVERHEAD
            chosen.append(entry)
            used += cost
        chosen.reverse()
        sequence: list[dict[str, Any]] = []
        for entry in chosen:
            role = "assistant" if entry["kind"] == "ai" else "user"
            if sequence and sequence[-1]["role"] == role:
                sequence[-1]["content"] += "\n\n" + entry["text"]
            else:
                sequence.append({"role": role, "content": entry["text"]})
        prefix = block + "\n\n" if block else ""
        if not sequence:
            opening = prompts["opening"][mode].replace("{char}", char).replace("{user}", user)
            sequence = [{"role": "user", "content": prefix + opening}]
        else:
            if sequence[0]["role"] == "assistant":
                earlier = len(chosen) < len(entries) or bool(story["memories"] or story["summary"])
                sequence.insert(0, {"role": "user", "content": prompts["resume" if earlier else "begin"][mode]})
            if sequence[-1]["role"] == "user":
                sequence[-1]["content"] = prefix + sequence[-1]["content"]
            else:
                sequence.append({"role": "user",
                                 "content": prefix + prompts["continue"][mode].replace("{char}", char)})
        info = {"verbatim": used, "dropped": len(verbatim) - len(chosen), "available": available}
        return [{"role": "system", "content": system}, *sequence], info

    def sampling(self, story: dict[str, Any]) -> dict[str, Any]:
        """Mintavétel a kreatív íráshoz: magasabb hőmérséklet, min_p a furcsa szavak ellen, enyhe ismétlésfék."""
        lang, mode = story["language"], story["mode"]
        user = story["persona"]["name"] or templates.DEFAULT_NAME
        options: dict[str, Any] = {
            "temperature": float(story["settings"]["creativity"]), "top_p": 0.95, "top_k": 64,
            "min_p": 0.05 if lang == "en" else 0.08, "repeat_penalty": 1.07, "repeat_last_n": 256,
            "presence_penalty": 0.3, "num_predict": predict_tokens(mode, story["settings"]["length"], lang)}
        options["stop"] = ["\n> "] if mode == "adventure" else [f"\n{user}:", "\nUser:", "\n{{user}}:"]
        return options

    def transcript(self, story: dict[str, Any], entries: list[dict[str, Any]]) -> str:
        if story["mode"] == "adventure":
            return "\n\n".join(e["text"] for e in entries)
        char = story["character"]["name"] or "?"
        user = story["persona"]["name"] or templates.DEFAULT_NAME
        return "\n\n".join(f"{char if e['kind'] == 'ai' else user}: {e['text']}" for e in entries)

    def _who(self, story: dict[str, Any]) -> str:
        prompts = PROMPTS[story["language"]]
        user = story["persona"]["name"] or templates.DEFAULT_NAME
        return (prompts["who"][story["mode"]].replace("{name}", user).replace("{user}", user)
                .replace("{char}", story["character"]["name"] or "?"))

    # --- a modell munkája (az ágens-szálon) -----------------------------------------------------------

    def run(self, payload: dict[str, Any], stop_event: threading.Event | None = None,
            show: Callable[[str], None] | None = None) -> None:
        """Egy feladat: "write" (folytatás vagy válasz), "retry" (másik változat), "idea" (ötletből
        történet, utána a nyitó jelenet), "summarize" (összefoglalás most)."""
        action = str(payload.get("action") or "")
        story_id = str(payload.get("id") or "")
        show = show or (lambda _text: None)
        stop = stop_event or threading.Event()
        try:
            if action == "idea":
                self._idea(story_id, stop, show)
                action = "write"
            if action in ("write", "retry"):
                if self._write(story_id, stop, show, retry=action == "retry"):
                    self._maybe_summarize(story_id, stop, show)
            elif action == "summarize":
                self._summarize(story_id, stop, show, force=True)
            else:
                raise StoryError("Ismeretlen feladat.")
        finally:
            with self._lock:
                self._current = None
            self._publish_status(story_id)

    def _write(self, story_id: str, stop: threading.Event, show: Callable[[str], None], retry: bool = False) -> bool:
        story = self.store.load(story_id)
        entries = story["entries"]
        if retry:
            if not entries or entries[-1]["kind"] != "ai":
                raise StoryError("A legutolsó rész nem az enyém, nincs mit újraírni.")
            entry_id, context = entries[-1]["id"], entries[:-1]
        else:
            entry_id, context = new_id(), entries
        messages, _info = self.build_messages(story, context)
        writer = story["character"]["name"] if story["mode"] == "character" else ""
        activity = f"{writer} válaszol" if writer else ACTIVITY["retry" if retry else "write"]
        self._activity(story_id, activity + "…", entry_id)
        show(activity)
        self.publish({"type": "creative_begin", "id": story_id, "entry": entry_id, "retry": retry})
        started = time.perf_counter()

        def on_text(delta: str) -> None:
            self.publish({"type": "creative_delta", "id": story_id, "entry": entry_id, "text": delta})

        result = self.llm.complete(messages, options=self.sampling(story), on_text=on_text, stop_event=stop)
        seconds = time.perf_counter() - started
        cut = result["cancelled"] or result.get("done_reason") == "length"
        text = clean_output(result["content"], story, cut=cut)
        stats = {"seconds": round(seconds, 1), "tokens": int(result.get("eval_count") or 0)}
        if result.get("eval_count") and result.get("eval_duration"):
            stats["tps"] = round(result["eval_count"] / (result["eval_duration"] / 1e9), 1)
        if not text:
            self.publish({"type": "creative_end", "id": story_id, "entry": entry_id, "kept": False, "stats": stats})
            if result["cancelled"]:
                return False
            raise StoryError("A modell üres választ adott. Próbáld újra, vagy írj hozzá valamit.")

        def change(story: dict[str, Any]) -> None:
            if retry:
                entry = next((e for e in story["entries"] if e["id"] == entry_id), None)
                if entry is None:
                    return
                entry["alts"] = (entry["alts"] + [text])[-MAX_ALTS:]
                entry["alt"] = len(entry["alts"]) - 1
                entry["text"] = text
            else:
                entry = _entry("ai", text)
                entry["id"] = entry_id
                story["entries"].append(entry)
                story["redo"] = []

        self.publish({"type": "creative_end", "id": story_id, "entry": entry_id, "kept": True, "stats": stats,
                      "cancelled": bool(result["cancelled"])})
        self._saved(self.store.edit(story_id, change))
        return not result["cancelled"]

    def _complete_quietly(self, messages: list[dict[str, Any]], options: dict[str, Any],
                          stop: threading.Event, schema: dict[str, Any] | None = None,
                          on_text: Callable[[str], None] | None = None) -> dict[str, Any]:
        return self.llm.complete(messages, options=options, stop_event=stop, json_schema=schema, on_text=on_text)

    def _idea(self, story_id: str, stop: threading.Event, show: Callable[[str], None]) -> None:
        """Ötletből történet: cím, lényeg, főszereplő, stílus és világkártyák (kalandnál), illetve a karakter
        leírása (karakternél). Utána a nyitó jelenetet a szokásos írás készíti, élőben."""
        story = self.store.load(story_id)
        if not story["idea"]:
            return
        lang, mode = story["language"], story["mode"]
        prompts = PROMPTS[lang]
        name = story["persona"]["name"] or templates.DEFAULT_NAME
        request = prompts["idea"][mode].replace("{idea}", story["idea"]).replace("{name}", name).replace("{user}", name)
        messages = [{"role": "system", "content": prompts["idea_system"]}, {"role": "user", "content": request}]
        self._activity(story_id, ACTIVITY["idea"] + "…")
        show(ACTIVITY["idea"])
        written = [0]

        def progress(delta: str) -> None:
            before, written[0] = written[0], written[0] + len(delta)
            if before // 250 != written[0] // 250:
                self._activity(story_id, f"{ACTIVITY['idea']}… ({written[0]} karakter)")

        result = self._complete_quietly(messages, {"temperature": 0.9, "top_p": 0.95, "num_predict": 1400},
                                        stop, IDEA_SCHEMA[mode], progress)
        if result["cancelled"]:
            raise Cancelled()
        try:
            data = parse_json_object(result["content"])
        except OllamaError:
            data = {}

        def change(story: dict[str, Any]) -> None:
            def text(key: str, limit: int) -> str:
                return clip(data.get(key) if isinstance(data.get(key), str) else "", limit)

            if text("title", 120):
                story["title"] = text("title", 120)
            if text("style", 400):
                story["note"] = text("style", 400)
            if mode == "adventure":
                if text("plot", MAX_FIELD):
                    story["plot"] = text("plot", MAX_FIELD)
                if text("persona", 1000) and not story["persona"]["about"]:
                    story["persona"]["about"] = text("persona", 1000)
                cards = [c for c in (_card(raw) for raw in data.get("cards") or []) if c and c["text"]]
                story["cards"] = cards[:6] or story["cards"]
            else:
                char = story["character"]
                stranger = "Stranger" if lang == "en" else "Idegen"
                char["name"] = text("name", 60) or char["name"] or stranger
                char["about"] = text("about", MAX_FIELD) or char["about"] or story["idea"]
                char["examples"] = text("examples", MAX_FIELD) or char["examples"]
                if text("scenario", MAX_FIELD):
                    story["plot"] = text("scenario", MAX_FIELD)
                if not text("title", 120) and text("name", 60):
                    story["title"] = char["name"]

        self._saved(self.store.edit(story_id, change))

    def needs_summary(self, story: dict[str, Any]) -> bool:
        usage = self.usage(story)
        verbatim = len(story["entries"]) - usage["remembered"]
        return usage["verbatim"] > usage["limit"] and verbatim > KEEP_VERBATIM + 1

    def _maybe_summarize(self, story_id: str, stop: threading.Event, show: Callable[[str], None]) -> None:
        if stop.is_set():
            return
        if self.needs_summary(self.store.load(story_id)):
            self._summarize(story_id, stop, show)
        else:
            self._maybe_consolidate(story_id, stop, show)

    def _summarize(self, story_id: str, stop: threading.Event, show: Callable[[str], None],
                   force: bool = False) -> bool:
        """A szó szerinti előzmény régebbi részéből emlék lesz; onnantól a modell azt látja helyette."""
        story = self.store.load(story_id)
        lang = story["language"]
        verbatim = [e for e in story["entries"] if not e.get("mem")]
        if len(verbatim) <= KEEP_VERBATIM:
            if force:
                raise StoryError("Még rövid a történet: az utolsó részeket mindig szó szerint látom, "
                                 "összefoglalni még nincs mit.")
            return False
        ctx = self.context_size()
        total = sum(estimate(e["text"], lang) + MESSAGE_OVERHEAD for e in verbatim)
        keep, max_chunk = int(ctx * HISTORY_KEEP), int(ctx * 0.5)
        chunk: list[dict[str, Any]] = []
        used = 0
        for entry in verbatim[:-KEEP_VERBATIM]:
            cost = estimate(entry["text"], lang) + MESSAGE_OVERHEAD
            if chunk and (used + cost > max_chunk or (not force and total - used <= keep)):
                break
            chunk.append(entry)
            used += cost
        while len(chunk) > 1 and chunk[-1]["kind"] != "ai":   # a kör és a rá adott válasz maradjon együtt
            used -= estimate(chunk.pop()["text"], lang) + MESSAGE_OVERHEAD
        if not chunk:
            return False
        prompts = PROMPTS[lang]
        words = max(60, min(160, used // 22))
        earlier = "\n".join(f"- {m['text']}" for m in story["memories"][-KEEP_MEMORIES:]) or prompts["none"]
        messages = [
            {"role": "system", "content": prompts["summarize"].replace("{who}", self._who(story))
                .replace("{words}", str(words))},
            {"role": "user", "content": prompts["summarize_user"].replace("{memories}", earlier)
                .replace("{passage}", self.transcript(story, chunk))},
        ]
        self._activity(story_id, ACTIVITY["summarize"] + " (emlékezet)…")
        show(ACTIVITY["summarize"])
        result = self._complete_quietly(messages, {"temperature": 0.3, "top_p": 0.9, "repeat_penalty": 1.05,
                                                   "num_predict": int(words * TOKENS_PER_WORD[lang]) + 120}, stop)
        if result["cancelled"]:
            return False
        text = clean_summary(result["content"])
        if not text:
            raise StoryError("Az összefoglaló üres lett, ezért most kihagytam; a következő körben újra megpróbálom.")
        ids = {e["id"] for e in chunk}

        def change(story: dict[str, Any]) -> None:
            for entry in story["entries"]:
                if entry["id"] in ids:
                    entry["mem"] = True
            story["memories"].append({"id": new_id(), "text": text, "created": time.time(),
                                      "first": chunk[0]["id"], "last": chunk[-1]["id"], "count": len(chunk),
                                      "auto": True})

        self._saved(self.store.edit(story_id, change))
        self._maybe_consolidate(story_id, stop, show)
        return True

    def _maybe_consolidate(self, story_id: str, stop: threading.Event, show: Callable[[str], None]) -> None:
        """Ha az emlékek együtt már túl hosszúak, a régebbieket az "eddigi történet" összefoglalóba sűríti."""
        if stop.is_set():
            return
        story = self.store.load(story_id)
        lang = story["language"]
        memories = story["memories"]
        size = sum(estimate(m["text"], lang) for m in memories) + estimate(story["summary"], lang)
        if len(memories) <= KEEP_MEMORIES or size <= self.context_size() * MEMORY_SOFT:
            return
        merge = memories[:-KEEP_MEMORIES]
        prompts = PROMPTS[lang]
        words = 320 if lang == "en" else 260
        messages = [
            {"role": "system", "content": prompts["consolidate"].replace("{who}", self._who(story))
                .replace("{words}", str(words))},
            {"role": "user", "content": prompts["consolidate_user"]
                .replace("{summary}", story["summary"] or prompts["none"])
                .replace("{memories}", "\n".join(f"- {m['text']}" for m in merge))},
        ]
        self._activity(story_id, "Összesűrítem a régi emlékeket…")
        show("Összesűrítem a régi emlékeket")
        result = self._complete_quietly(messages, {"temperature": 0.3, "top_p": 0.9,
                                                   "num_predict": int(words * TOKENS_PER_WORD[lang]) + 160}, stop)
        text = clean_summary(result["content"]) if not result["cancelled"] else ""
        if not text:
            return
        ids = {m["id"] for m in merge}

        def change(story: dict[str, Any]) -> None:
            story["summary"] = text
            story["memories"] = [m for m in story["memories"] if m["id"] not in ids]

        self._saved(self.store.edit(story_id, change))
