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
részt kell feldolgoznia. Ezért az előzmények elejét csak ritkán, egyszerre nagyobb
darabban módosítjuk.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import web
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
REPEAT_NOTE = ("Ezt már lekérted ebben a kérésben, ugyanígy; az eredménye fent van. Ne kérd le újra: "
               "használd azt, próbálj mást, vagy válaszolj.")
BUDGET_NOTE = "\n[Még {left} lépésed maradt. Ha már van elég információd, válaszolj.]"
BUDGET_WARN_AT = 3
# Az utolsó körben nincs eszköz; ez a kérés mondja meg a modellnek, hogy a meglévőből válaszoljon
# (enélkül a modell néha üres választ ad, mert eszközt hívna).
WRAP_UP = ("[Elfogyott a lépéskereted, több eszközt nem hívhatsz. Most válaszolj a kérdésre abból, amit "
           "eddig találtál, a szokásos stílusodban. Ha valamit nem sikerült ellenőrizni, mondd meg röviden.]")
_TOOL_TEXT_RE = re.compile(r"<tool_call>|^\s*\{\s*\"name\"\s*:", re.MULTILINE)


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
# Ha egy eszköz eredményében ez a jel van, tömörítéskor csak az utána jövő oldalnézet avul el,
# az előtte lévő rész (pl. a feltöltendő hirdetés adatai) megmarad.
KEEP_ABOVE = "\n--- oldalnézet ---\n"
SHORTENED_NOTE = "\n[…a kontextus miatt rövidítve…]"
CHARS_PER_TOKEN = 2.5      # óvatos becslés (magyar szöveg, webcímek, JSON)
COMPACT_AT = 0.6           # ennél a telítettségnél kezdünk tömöríteni
HARD_LIMIT = 0.8           # ezt semmiképp nem lépjük át
MESSAGE_OVERHEAD = 12      # token/üzenet a chat-sablon miatt


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
                 tool_names: set[str] | None = None, max_rounds: int | None = None):
        self.cfg = cfg
        self.llm = llm
        self.toolbox = toolbox
        self.system_prompt = system_prompt
        self.on_tool_call = on_tool_call
        self.on_tool_start = on_tool_start
        # Melyik eszközöket látja ez a beszélgetés (None = mindet); a Munka menü a sajátjait.
        self.tool_names = tool_names
        self.max_rounds = max_rounds
        self.history: list[dict[str, Any]] = []
        self.last_stats = TurnStats()
        self.context_tokens = 8192           # a JarvisApp az Ollamától kérdezi le a valódit
        self.last_reply_streamed = False     # elhangzott-e már a válasz folyamatosan
        self.stop_event: threading.Event | None = None
        self._tool_chars = len(json.dumps(toolbox.schemas(tool_names), ensure_ascii=False))
        self._calibration = 1.0              # a valódi tokenszám alapján nő, ha alábecsülnénk

    def reset(self) -> None:
        self.history.clear()

    def system_message(self) -> str:
        """A rendszerprompt a mai dátummal (naponta egyszer változik, a gyorsítótárat nem rontja)."""
        return f"{self.system_prompt}\n\n{date_line()}"

    def _messages(self) -> list[dict[str, Any]]:
        return [{"role": "system", "content": self.system_message()}, *self.history]

    def _check_stop(self) -> None:
        if self.stop_event is not None and self.stop_event.is_set():
            raise Cancelled()

    def ask(self, text: str, on_text: Callable[[str], None] | None = None) -> str:
        """Megválaszolja a kérést, közben annyi eszközt hív, amennyi kell.

        `on_text`: ha meg van adva, a válasz szövege darabonként, azonnal érkezik ide
        (felolvasáshoz, kiíráshoz), még mielőtt a modell befejezné.
        Ha közben hiba, megszakítás vagy Ctrl+C jön, a kérés nyomait eltávolítja az előzményekből.
        """
        stats = TurnStats()
        self.last_stats = stats
        self.last_reply_streamed = False
        question: dict[str, Any] = {"role": "user", "content": text}
        self.history.append(question)
        limit = self.max_rounds if self.max_rounds is not None else self.cfg.assistant.max_tool_rounds
        max_rounds = max(0, int(limit))
        round_no = 0
        seen: dict[str, str] = {}      # a kérésen belül már lefutott keresések, oldalolvasások
        try:
            while True:
                self._check_stop()
                # Az utolsó körben már nem adunk eszközt, így a modellnek szövegesen kell válaszolnia.
                final = round_no >= max_rounds
                tools = self.toolbox.schemas(self.tool_names) if not final else None
                raw_estimate = self._fit_context()
                messages = self._messages()
                if final and round_no > 0:
                    messages.append({"role": "user", "content": WRAP_UP})
                t0 = time.perf_counter()
                msg = self.llm.chat(messages, tools=tools, on_text=on_text)
                stats.llm_seconds += time.perf_counter() - t0
                stats.llm_calls += 1
                stats.tokens += int(self.llm.last_stats.get("eval_count") or 0)
                self._calibrate(raw_estimate)

                content = strip_think(msg.get("content", ""))
                if final and _TOOL_TEXT_RE.search(content):
                    content = ""     # eszközt hívott volna szöveg helyett
                calls = (msg.get("tool_calls") or []) if tools else []
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

                for call in calls:
                    self._check_stop()
                    name = call["function"]["name"]
                    args = call["function"]["arguments"]
                    if self.on_tool_start:
                        self.on_tool_start(name, args)
                    key = call_key(name, args)
                    if self.tool_names is not None and name not in self.tool_names:
                        result = f"HIBA: itt nincs ilyen eszköz: {name}."
                    elif key is not None and key in seen:
                        result = REPEAT_NOTE
                    else:
                        result = self.toolbox.call(name, args)
                        if key is not None:
                            seen[key] = result
                    stats.tool_calls += 1
                    if self.on_tool_call:
                        self.on_tool_call(name, args, result)
                    self.history.append({"role": "tool", "tool_name": name, "content": result})
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
        """Minden modellhívás előtt: ha nem férne bele a kontextusba, lépcsőzetesen rövidít.

        1. tömöríti a régi oldalnézeteket (csak ha a kontextus 60%-a betelt; így a
           prompt-gyorsítótár a legtöbb lépésnél megmarad),
        2. csak a legutolsó oldalnézetet hagyja meg,
        3. elhagyja a korábbi köröket (a mostani kérdés mindig marad),
        4. végső esetben a legutolsó eszköz-eredményt vágja meg.
        Visszaadja a nyers becslést a későbbi kalibráláshoz.
        """
        limit = float(self.context_tokens)
        if self.estimated_tokens() > limit * COMPACT_AT:
            self._compact(keep=max(0, int(self.cfg.assistant.keep_page_views)))
        if self.estimated_tokens() > limit * HARD_LIMIT:
            self._compact(keep=1)
        if self.estimated_tokens() > limit * HARD_LIMIT:
            self._drop_earlier_turns()
        if self.estimated_tokens() > limit * HARD_LIMIT:
            self._shrink_last_tool_result(limit * HARD_LIMIT)
        return self._raw_tokens()

    def _compact(self, keep: int) -> None:
        """Az elavult, hosszú eszköz-eredményeket (oldalnézetek) rövid jelzésre cseréli."""
        positions = [i for i, m in enumerate(self.history)
                     if m["role"] == "tool" and self.toolbox.is_ephemeral(m.get("tool_name", ""))
                     and not m["content"].endswith(STALE_NOTE)]
        for i in positions[:-keep] if keep else positions:
            content = self.history[i]["content"]
            cut = content.find(KEEP_ABOVE)
            self.history[i]["content"] = (content[:cut] + "\n" + STALE_NOTE) if cut > 0 else STALE_NOTE

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
