"""A beszélgetés kezelése és az eszközhívási ciklus.

Egy kérés menete:
  1. a felhasználó üzenete bekerül az előzmények közé,
  2. a modell vagy válaszol, vagy eszközt kér (tool call),
  3. az eszköz lefut, az eredménye visszamegy a modellnek, és jöhet a következő kör,
  4. amikor a modell már szövegesen válaszol, az lesz a végeredmény.

Kontextus: ha a beszélgetés nem fér bele a modell kontextusába, az Ollama csendben levágja
az elejét, akár a felhasználó kérdését is ("no user query found in messages" hiba). Ezért
minden modellhívás előtt ellenőrizzük a méretet, és ha kell, mi rövidítünk, okosan: előbb a
régi oldalnézeteket, aztán a korábbi köröket, a mostani kérdést soha.

Sebesség: az Ollama megjegyzi az előző kérés elejét (prompt-gyorsítótár), és csak az új
részt kell feldolgoznia. Ezért az előzményeket lehetőleg csak a végükön bővítjük: amikor új
oldalnézet érkezik, az előzőt rögtön egy rövid tájékozódó sorra cseréljük (az közvetlenül az új
rész előtt áll, így alig kell valamit újra feldolgozni), az eszközök listája pedig a kérés minden
körében ugyanaz. A korábbi körök elejét csak ritkán, egyszerre nagyobb darabban vágjuk.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import web
from .browser import NEW_LEGEND
from .config import Config
from .llm import Cancelled, OllamaClient
from .textutil import fold, strip_think
from .tools import HU_MONTHS, HU_WEEKDAYS, Toolbox

ToolCallback = Callable[[str, dict[str, Any], str], None]
ToolStartCallback = Callable[[str, dict[str, Any]], None]

FALLBACK_REPLY = "Elnézést, erre most nem tudok mit mondani."
TOO_MANY_STEPS = "Elnézést, ez a feladat túl sok lépésből állt, ezért félbehagytam."
CONTINUE_HINT = " Ha mondod, hogy folytassam, onnan megyek tovább."
# Ugyanez a hívás ugyanazokkal a paraméterekkel egy kérésen belül ugyanazt adná: nem futtatjuk újra.
DEDUP_TOOLS = {"web_search", "web_research", "read_webpage"}
# Olvasmány (cikk, oldalszöveg): a modell ebből gyűjt, ezért nem avul el a következő oldalnézettel; csak
# helyhiánykor tömörítjük, mint a keresési találatokat.
READ_TOOLS = {"read_webpage", "browser_read"}
REPEAT_NOTE = ("Ezt már lekérted ebben a kérésben, ugyanígy; az eredménye fent van. Ne kérd le újra: "
               "használd azt, próbálj mást, vagy válaszolj.")
BUDGET_NOTE = "\n[Még {left} lépésed maradt. Ha már van elég információd, válaszolj.]"
BUDGET_WARN_AT = 3
# Az utolsó körben is ugyanazok az eszközök mennek (különben az Ollamának az egész promptot újra fel kellene
# dolgoznia), de amit akkor hívna, azt nem futtatjuk; ez a kérés mondja meg a modellnek, hogy a meglévőből
# válaszoljon (enélkül a modell néha üres választ ad, mert eszközt hívna).
WRAP_UP = ("[Elfogyott a lépéskereted, több eszközt nem hívhatsz. Most válaszolj a kérdésre abból, amit "
           "eddig találtál, a szokásos stílusodban. Ha valamit nem sikerült ellenőrizni, mondd meg röviden.]")
_TOOL_TEXT_RE = re.compile(r"<tool_call>|^\s*\{\s*\"name\"\s*:", re.MULTILINE)
# Egy kör válaszának felső határa (num_predict), ha a beszélgetés mást nem kér: egy beragadt, ismétlődő
# válasz se foglalja percekig a modellt (és a böngészőt egy félig kitöltött űrlapon).
MAX_REPLY_TOKENS = 2048
# Ha a válasz a határnál félbemaradt (és eszközt hívott volna): egyszer újrakérdezzük ezzel, másodszor feladjuk.
CUT_SHORT_NOTE = ("[Az előző válaszod túl hosszú lett, és félbemaradt. Most röviden: egyetlen eszközhívás, "
                  "vagy egy-két mondatos válasz.]")
TOO_LONG_REPLY = ("Elnézést, a válaszom túl hosszúra nyúlt (valószínűleg ismételgettem magam), ezért "
                  "leállítottam.")
# Elakadás: ugyanaz a lépés ugyanazon az oldalon, és az oldal nem változott (vagy ugyanúgy hibát adott). Az
# ismétlésnél ezt a megjegyzést kapja a modell; STALL_LIMIT ismétlés után a kérés véget ér (ne égesse el a maradék
# lépéseket). Billentyűt (Esc) a modell nem tud nyomni, ezért a bezáró gombot javasoljuk.
STALL_NOTE = ("\n[Ez nem változtatott semmit; próbálj mást, vagy ha egy felugró ablak van nyitva, zárd be a "
              "bezáró gombjával.]")
STALL_LIMIT = 3
STALL_REPLY = ("Elakadtam: többször ugyanazt próbáltam ({name}), de az oldal nem változott, ezért "
               "abbahagytam.")
# Az új elemek jele az oldalnézetben („*[12]”, és a magyarázata a fejlécben): egy változó lépés után ott van, a
# következő, hatástalan lépés után már nincs, pedig az oldal ugyanaz. Az elakadás ujjlenyomatában nem számít.
_NEW_MARK_RE = re.compile(r"\*(?=\[\d+\])|" + re.escape(NEW_LEGEND))
# A képelemzés a futó beszélgetésben: a kérdéses eszközhívás helyén ez áll, a kép a következő üzenetben jön.
VISION_PENDING = "(A képernyőkép a következő üzenetben van.)"
# Egy képernyőkép kb. ennyi token a kérésben (1280 széles JPEG); ha így nem férne el, külön kérdés lesz belőle.
IMAGE_TOKENS = 1500


def date_line(now: dt.datetime | None = None) -> str:
    """A mai dátum a rendszerpromptba: enélkül a modell a tanulási ideje szerinti évre keres rá."""
    now = now or dt.datetime.now()
    return f"Mai dátum: {now.year}. {HU_MONTHS[now.month - 1]} {now.day}., {HU_WEEKDAYS[now.weekday()]}."


def call_key(name: str, args: Any) -> str | None:
    """Egy keresés vagy oldalolvasás azonosítója (kis- és nagybetű, ékezet, szóköz nem számít)."""
    if name not in DEDUP_TOOLS:
        return None
    norm: dict[str, Any] = {}
    for key, value in sorted((args or {}).items() if isinstance(args, dict) else []):
        if isinstance(value, str):
            value = " ".join(fold(value).split())
            if key == "url":
                value = re.sub(r"^https?://(www\.)?", "", value).rstrip("/")
        norm[key] = value
    return name + json.dumps(norm, sort_keys=True, ensure_ascii=False)


STALE_NOTE = "[Korábbi, már elavult oldalnézet vagy találati lista; a legfrissebb az érvényes.]"
# Az elavult oldalnézet helyén: mi történt (a nézet feletti megjegyzés) és hol voltunk (oldalcím, webcím).
ORIENT_PREFIX = "[Korábbi oldalnézet: "
ORIENT_NOTE_CHARS = 300
ORIENT_TITLE_CHARS = 80
ORIENT_URL_CHARS = 90
# Ha egy eszköz eredményében ez a jel van, tömörítéskor csak az utána jövő oldalnézet avul el,
# az előtte lévő rész (pl. a feltöltendő hirdetés adatai) megmarad.
KEEP_ABOVE = "\n--- oldalnézet ---\n"
SHORTENED_NOTE = "\n[…a kontextus miatt rövidítve…]"
CHARS_PER_TOKEN = 2.5      # óvatos becslés (magyar szöveg, webcímek, JSON)
COMPACT_AT = 0.6           # ennél a telítettségnél kezdünk tömöríteni
HARD_LIMIT = 0.8           # ezt semmiképp nem lépjük át
MESSAGE_OVERHEAD = 12      # token/üzenet a chat-sablon miatt


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _view_header(view: str) -> tuple[list[str], str, str] | None:
    """Egy oldalnézet (browser.format_snapshot, web.format_page) fejléce: (a megjegyzés sorai, oldalcím, webcím).
    None, ha a szöveg nem oldalnézet (pl. találati lista)."""
    lines = view.split("\n", 12)
    for i, line in enumerate(lines[:10]):
        if line.startswith("Oldal: "):
            url = lines[i + 1][len("Cím: "):] if i + 1 < len(lines) and lines[i + 1].startswith("Cím: ") else ""
            note = [n.strip() for n in lines[:i] if n.strip() and n.strip() != web.UNTRUSTED_NOTE]
            return note, line[len("Oldal: "):].strip(), url.strip()
    return None


def page_orientation(view: str) -> str:
    """Egy elavult oldalnézet helyére: egy sor, ami megtartja, mi történt és hol voltunk, pl.
    '[Korábbi oldalnézet: Rákattintottam: "Tovább". Oldal: Hirdetésfeladás – vinted.hu/items/new]'.
    Ha a szöveg nem oldalnézet, a sima STALE_NOTE."""
    header = _view_header(view)
    if header is None:
        return STALE_NOTE
    note, title, url = header
    url = re.sub(r"^[a-z]+://(www\.)?", "", url.split("#")[0].split("?")[0], flags=re.IGNORECASE)
    where = " – ".join(part for part in (_clip(title, ORIENT_TITLE_CHARS), _clip(url, ORIENT_URL_CHARS)) if part)
    parts = [_clip(" ".join(note), ORIENT_NOTE_CHARS)] if note else []
    parts.append(f"Oldal: {where or '?'}")
    return ORIENT_PREFIX + " ".join(parts) + "]"


def _has_view(content: str) -> bool:
    cut = content.find(KEEP_ABOVE)
    return _view_header(content[cut + len(KEEP_ABOVE):] if cut >= 0 else content) is not None


def page_state(result: str) -> str | None:
    """Az eszköz eredményében lévő oldalnézet ujjlenyomata (a fölötte álló megjegyzés és az új elemek jele nélkül):
    ha egy lépés után ugyanaz, az oldal nem változott. None, ha az eredményben nincs oldalnézet."""
    cut = result.find(KEEP_ABOVE)
    view = result[cut + len(KEEP_ABOVE):] if cut >= 0 else result
    if _view_header(view) is None:
        return None
    start = 0 if view.startswith("Oldal: ") else view.find("\nOldal: ") + 1
    return hashlib.sha1(_NEW_MARK_RE.sub("", view[start:]).encode("utf-8", "replace")).hexdigest()


def _is_compacted(content: str) -> bool:
    last = content.rsplit("\n", 1)[-1]
    return content.endswith(STALE_NOTE) or (last.startswith(ORIENT_PREFIX) and last.endswith("]"))


@dataclass
class TurnStats:
    llm_seconds: float = 0.0
    llm_calls: int = 0
    tool_calls: int = 0
    tokens: int = 0

    def tokens_per_second(self) -> float | None:
        return self.tokens / self.llm_seconds if self.tokens and self.llm_seconds else None


class Assistant:
    def __init__(self, cfg: Config, llm: OllamaClient, toolbox: Toolbox, system_prompt: str,
                 on_tool_call: ToolCallback | None = None,
                 on_tool_start: ToolStartCallback | None = None,
                 tool_names: set[str] | None = None, max_rounds: int | None = None,
                 options: dict[str, Any] | None = None):
        self.cfg = cfg
        self.llm = llm
        self.toolbox = toolbox
        self.system_prompt = system_prompt
        self.on_tool_call = on_tool_call
        self.on_tool_start = on_tool_start
        # Melyik eszközöket látja ez a beszélgetés (None = mindet); a Munka menü a sajátjait.
        self.tool_names = tool_names
        self.max_rounds = max_rounds
        # A beszélgetés saját mintavétele és válaszhossza (pl. a Munka menüben alacsonyabb temperature); a
        # kontextusméretet nem változtatja, így a modellt sem kell újratölteni.
        self.options: dict[str, Any] = {"num_predict": MAX_REPLY_TOKENS, **(options or {})}
        self.history: list[dict[str, Any]] = []
        self.last_stats = TurnStats()
        self.context_tokens = 8192           # a JarvisApp az Ollamától kérdezi le a valódit
        self.last_reply_streamed = False     # elhangzott-e már a válasz folyamatosan
        self.stop_event: threading.Event | None = None
        self._tool_chars = len(json.dumps(self.schemas(), ensure_ascii=False))
        self._calibration = 1.0              # a valódi tokenszám alapján nő, ha alábecsülnénk

    def reset(self) -> None:
        self.history.clear()

    def system_message(self) -> str:
        """A rendszerprompt a mai dátummal (naponta egyszer változik, a gyorsítótárat nem rontja)."""
        return f"{self.system_prompt}\n\n{date_line()}"

    def schemas(self, tool_names: set[str] | None = None) -> list[dict[str, Any]]:
        """Az eszközök leírása a modellnek: a kérés eszközei (`tool_names`), vagy a beszélgetés összes eszköze."""
        return self.toolbox.schemas(self.tool_names if tool_names is None else tool_names)

    def warm_request(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        """Az előmelegítő kérés (üzenetek, eszközök, beállítások): az eleje szó szerint ugyanaz, mint a következő
        igazi kérésé (rendszerprompt, eszközök, eddigi előzmények), így az Ollama gyorsítótárából folytatható."""
        messages = [*self._messages(), {"role": "user", "content": "."}]
        return messages, self.schemas(), dict(self.options)

    def _messages(self) -> list[dict[str, Any]]:
        return [{"role": "system", "content": self.system_message()}, *self.history]

    def _check_stop(self) -> None:
        if self.stop_event is not None and self.stop_event.is_set():
            raise Cancelled()

    def ask(self, text: str, on_text: Callable[[str], None] | None = None,
            tool_names: set[str] | None = None) -> str:
        """Megválaszolja a kérést, közben annyi eszközt hív, amennyi kell.

        `on_text`: ha meg van adva, a válasz szövege darabonként, azonnal érkezik ide
        (a kiíráshoz), még mielőtt a modell befejezné.
        `tool_names`: ebben a kérésben csak ezeket az eszközöket látja a modell (pl. egy feltöltés csak a
        böngészőt és a tárgy néhány eszközét); a kérés minden körében ugyanaz a lista megy, hogy a prompt eleje
        az Ollama gyorsítótárában maradjon. Alapból a beszélgetés összes eszköze.
        Ha közben hiba, megszakítás vagy Ctrl+C jön, a kérés nyomait eltávolítja az előzményekből.
        """
        stats = TurnStats()
        self.last_stats = stats
        self.last_reply_streamed = False
        question: dict[str, Any] = {"role": "user", "content": text}
        self.history.append(question)
        limit = self.max_rounds if self.max_rounds is not None else self.cfg.assistant.max_tool_rounds
        max_rounds = max(0, int(limit))
        allowed = self.tool_names if tool_names is None else tool_names
        # Minden körben ez megy, az utolsóban is: az eszközök a prompt elején vannak, ha változnának, az
        # Ollamának az egész promptot újra fel kellene dolgoznia.
        tools = self.schemas(allowed)
        self._tool_chars = len(json.dumps(tools, ensure_ascii=False))
        round_no = 0
        seen: dict[str, str] = {}      # a kérésen belül már lefutott keresések, oldalolvasások
        stalls: dict[str, int] = {}    # ugyanaz a lépés hányszor hagyta változatlanul az oldalt
        page: str | None = None        # a legutóbbi oldalnézet ujjlenyomata
        cut_short = 0                  # félbemaradt a válasz a hosszhatárnál (egyszer újrakérdezzük)
        self.toolbox.conversation = lambda: self._vision_context(tools)
        try:
            while True:
                self._check_stop()
                # Az utolsó körben a modellnek szövegesen kell válaszolnia; amit akkor hívna, nem futtatjuk.
                final = round_no >= max_rounds
                raw_estimate = self._fit_context()
                messages = self._messages()
                if final and round_no > 0:
                    messages.append({"role": "user", "content": WRAP_UP})
                if cut_short:
                    messages.append({"role": "user", "content": CUT_SHORT_NOTE})
                t0 = time.perf_counter()
                msg = self.llm.chat(messages, tools=tools or None, on_text=on_text, options=self.options)
                stats.llm_seconds += time.perf_counter() - t0
                stats.llm_calls += 1
                stats.tokens += int(self.llm.last_stats.get("eval_count") or 0)
                self._calibrate(raw_estimate)

                content = strip_think(msg.get("content", ""))
                calls = (msg.get("tool_calls") or []) if not final else []
                if self.llm.last_stats.get("done_reason") == "length" and not calls:
                    if content and not _TOOL_TEXT_RE.search(content):
                        content = content.rstrip() + " …"   # hosszú szöveges válasz: ami eddig megvan, az a válasz
                    elif cut_short and not final:
                        content = TOO_LONG_REPLY
                    elif not final:
                        # Eszközt hívott volna, de a hívás félbemaradt: egyszer újrakérdezzük, röviden.
                        cut_short += 1
                        round_no += 1
                        continue
                if final and _TOOL_TEXT_RE.search(content):
                    content = ""     # eszközt hívott volna szöveg helyett
                cut_short = 0
                entry: dict[str, Any] = {"role": "assistant", "content": content}
                if calls:
                    entry["tool_calls"] = calls
                self.history.append(entry)

                if not calls:
                    self.last_reply_streamed = bool(content) and on_text is not None
                    if not content:
                        content = self._salvage(question) if (final and round_no > 0) else FALLBACK_REPLY
                        entry["content"] = content
                    self._trim_history()
                    return content

                stalled = ""
                for call in calls:
                    self._check_stop()
                    name = call["function"]["name"]
                    args = call["function"]["arguments"]
                    if self.on_tool_start:
                        self.on_tool_start(name, args)
                    key = call_key(name, args)
                    if allowed is not None and name not in allowed:
                        result = f"HIBA: itt nincs ilyen eszköz: {name}."
                    elif key is not None and key in seen:
                        result = REPEAT_NOTE
                    else:
                        result = self.toolbox.call(name, args)
                        if key is not None:
                            seen[key] = result
                    after = page_state(result)
                    if after is None and result.startswith("HIBA") and name.startswith("browser_"):
                        after = page or "-"    # a sikertelen lépés sem változtatott az oldalon
                    if after is not None:
                        if after == page:      # a lépés után ugyanaz az oldal, mint előtte
                            step = name + json.dumps(args, sort_keys=True, ensure_ascii=False) + after
                            stalls[step] = stalls.get(step, 0) + 1
                            if stalls[step] >= 2:
                                result += STALL_NOTE
                            if stalls[step] > STALL_LIMIT:
                                stalled = name
                        page = after
                    stats.tool_calls += 1
                    if self.on_tool_call:
                        self.on_tool_call(name, args, result)
                    self.history.append({"role": "tool", "tool_name": name, "content": result})
                if stalled:
                    reply = STALL_REPLY.format(name=stalled)
                    self.history.append({"role": "assistant", "content": reply})
                    self._trim_history()
                    return reply
                round_no += 1
                left = max_rounds - round_no
                if 0 < left <= BUDGET_WARN_AT:
                    self.history[-1]["content"] += BUDGET_NOTE.format(left=left)
        except BaseException:
            for i, message in enumerate(self.history):
                if message is question:
                    del self.history[i:]
                    break
            raise
        finally:
            self.toolbox.conversation = None

    def _vision_context(self, tools: list[dict[str, Any]]) -> dict[str, Any] | None:
        """A futó beszélgetés a képelemzéshez (lásd OllamaClient.vision): ugyanaz a rendszerprompt, előzmény,
        eszközlista és beállítás, mint a következő körben. A most futó képes eszközhívás helyén egy jelzés áll.
        None, ha a kép már nem férne el a kontextusban (akkor külön kérdés lesz belőle)."""
        if self.estimated_tokens() + IMAGE_TOKENS > self.context_tokens * HARD_LIMIT:
            return None
        messages = self._messages()
        last = next((i for i in range(len(messages) - 1, -1, -1) if messages[i]["role"] == "assistant"), None)
        calls = (messages[last].get("tool_calls") or []) if last is not None else []
        answered = len(messages) - 1 - last if last is not None else 0
        if answered < len(calls):
            name = calls[answered].get("function", {}).get("name", "")
            messages.append({"role": "tool", "tool_name": name, "content": VISION_PENDING})
        return {"messages": messages, "tools": tools, "options": dict(self.options)}

    def _salvage(self, question: dict[str, Any]) -> str:
        """Ha a modell a lépéskeret végén sem válaszolt: legalább azt mondjuk meg, mit néztünk meg."""
        start = next((i for i, m in enumerate(self.history) if m is question), 0)
        hosts: list[str] = []
        for message in self.history[start:]:
            for call in message.get("tool_calls") or []:
                args = call.get("function", {}).get("arguments")
                url = args.get("url") if isinstance(args, dict) else None
                host = web.host_of(str(url)) if url else ""
                if host and host not in hosts:
                    hosts.append(host)
        seen = f" Ezeket néztem meg: {', '.join(hosts[:6])}." if hosts else ""
        return TOO_MANY_STEPS + seen + CONTINUE_HINT

    # --- kontextuskezelés ------------------------------------------------------------

    def _raw_tokens(self) -> float:
        chars = len(self.system_prompt) + self._tool_chars
        chars += sum(len(m.get("content") or "") + len(json.dumps(m.get("tool_calls", "")))
                     for m in self.history)
        return chars / CHARS_PER_TOKEN + MESSAGE_OVERHEAD * (len(self.history) + 1)

    def estimated_tokens(self) -> int:
        return int(self._raw_tokens() * self._calibration)

    def _calibrate(self, raw_estimate: float) -> None:
        """Ha az Ollama szerint több token volt a kérésben, mint becsültük, onnantól óvatosabbak vagyunk."""
        actual = self.llm.last_stats.get("prompt_eval_count")
        if actual and raw_estimate > 0:
            ratio = float(actual) / raw_estimate
            if ratio > self._calibration:
                self._calibration = min(ratio, 3.0)

    def _fit_context(self) -> float:
        """Minden modellhívás előtt: a régi oldalnézeteket kitömöríti, és ha nem férne bele a kontextusba,
        lépcsőzetesen rövidít.

        0. mindig: ha új oldalnézet jött, a korábbiak helyére egy-egy rövid tájékozódó sor kerül (mi történt,
           melyik oldalon), egy munka első üzenetében lévő nézet helyére is. A legutóbbi korábbi nézet közvetlenül
           az új rész előtt áll, így a prompt-gyorsítótár megmarad, és a kérés nem nő oldalnézetenként. Az
           olvasmányok (cikk, keresés) itt még maradnak.
        1. ha a kontextus 60%-a betelt: minden régi eszköz-eredményt tömörít (az olvasmányokat is),
        2. csak a legutolsó oldalnézetet hagyja meg,
        3. elhagyja a korábbi köröket (a mostani kérdés mindig marad),
        4. végső esetben a legutolsó eszköz-eredményt vágja meg.
        Visszaadja a nyers becslést a későbbi kalibráláshoz.
        """
        limit = float(self.context_tokens)
        keep = max(0, int(self.cfg.assistant.keep_page_views))
        self._compact(keep=max(1, keep), views_only=True)
        if self.estimated_tokens() > limit * COMPACT_AT:
            self._compact(keep=keep)
        if self.estimated_tokens() > limit * HARD_LIMIT:
            self._compact(keep=1)
        if self.estimated_tokens() > limit * HARD_LIMIT:
            self._drop_earlier_turns()
        if self.estimated_tokens() > limit * HARD_LIMIT:
            self._shrink_last_tool_result(limit * HARD_LIMIT)
        return self._raw_tokens()

    def _compact(self, keep: int, views_only: bool = False) -> None:
        """Az elavult, hosszú eszköz-eredményeket rövidre cseréli: egy oldalnézetből a tájékozódó sor marad
        (page_orientation), a KEEP_ABOVE fölötti rész (pl. a hirdetés adatai) változatlanul. Egy felhasználói
        üzenetből csak a KEEP_ABOVE utáni oldalnézet (egy munka első üzenete, pl. a megnyitott hirdetésfeladás):
        a kérés maga mindig megmarad.

        `views_only`: csak a böngésző oldalnézeteit (az olvasmányokat és a hibaüzeneteket nem: egy sikertelen
        lépés után így is látszik a legutóbbi oldal)."""
        positions = []
        for i, message in enumerate(self.history):
            name = message.get("tool_name", "")
            content = message.get("content") or ""
            if message["role"] == "user":
                if KEEP_ABOVE in content and _has_view(content) and not _is_compacted(content):
                    positions.append(i)
                continue
            if message["role"] != "tool" or not self.toolbox.is_ephemeral(name) or _is_compacted(content):
                continue
            if views_only and (name in READ_TOOLS or not _has_view(content)):
                continue
            positions.append(i)
        for i in positions[:-keep] if keep else positions:
            content = self.history[i]["content"]
            cut = content.find(KEEP_ABOVE)
            if cut >= 0:
                above, view = content[:cut], page_orientation(content[cut + len(KEEP_ABOVE):])
                content = f"{above}\n{view}" if above else view
            else:
                content = page_orientation(content)
            self.history[i]["content"] = content

    def _drop_earlier_turns(self) -> None:
        """Csak a mostani kör marad (a legutolsó felhasználói üzenettől kezdve)."""
        last_user = max((i for i, m in enumerate(self.history) if m["role"] == "user"), default=0)
        del self.history[:last_user]

    def _shrink_last_tool_result(self, budget_tokens: float) -> None:
        tool_positions = [i for i, m in enumerate(self.history) if m["role"] == "tool"]
        if not tool_positions:
            return
        message = self.history[tool_positions[-1]]
        excess = self.estimated_tokens() - budget_tokens
        if excess <= 0:
            return
        cut_chars = int(excess / self._calibration * CHARS_PER_TOKEN) + 200
        content = message["content"]
        keep_chars = max(500, len(content) - cut_chars)
        if keep_chars < len(content):
            message["content"] = content[:keep_chars] + SHORTENED_NOTE

    def _trim_history(self) -> None:
        """Ha túl sok a kör, egyszerre a felére vágja (mindig egy felhasználói üzenetnél).

        A nagyobb, ritkább vágás azért kell, hogy ne minden kérésnél változzon az előzmények
        eleje, mert az az egész prompt-gyorsítótárat érvényteleníti.
        """
        max_turns = max(1, int(self.cfg.assistant.max_history_turns))
        user_positions = [i for i, m in enumerate(self.history) if m["role"] == "user"]
        if len(user_positions) > max_turns:
            keep = max(1, max_turns // 2)
            del self.history[:user_positions[-keep]]
