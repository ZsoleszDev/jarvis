"""Konzolos felület (python jarvis.py --console).

A billentyűzetről csak a főszál olvas; ha az ágens-szál jóváhagyást kér, a főszál kérdezi meg.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from . import console
from .app import JarvisApp
from .textutil import fold
from .tools import summarize_result

EXIT_COMMANDS = {"q", "kilepes", "exit", "quit", "viszlat"}
YES = {"igen", "i", "yes", "y"}

HELP = """
  Enter            beszéd (Jarvis addig hallgat, amíg csend nem lesz)
  szöveg + Enter   gépelt kérdés
  /új              új beszélgetés (elfelejti az eddigieket)
  /súgó            ez a súgó
  q                kilépés
  Ctrl+C           a folyamatban lévő felvétel, válasz vagy felolvasás megszakítása
"""


class ConsoleUI:
    def __init__(self, name: str):
        self.name = name
        self._printer: console.StreamPrinter | None = None
        self._lock = threading.RLock()
        self._confirm: tuple[threading.Event, list[bool]] | None = None

    def _end_line(self) -> None:
        if self._printer is not None:
            self._printer.end()
            self._printer = None

    # --- a Jarvis üzenetei ----------------------------------------------------------

    def state(self, state: str, detail: str = "") -> None:
        if state == "listening":
            with self._lock:
                self._end_line()
                if detail == "folytatás":
                    console.info("(Ha van még kérdésed, mondhatod, figyelek...)")
                else:
                    console.info("Hallgatlak... (ha befejezted, maradj csendben egy pillanatig)")

    def user(self, text: str, via: str) -> None:
        if via != "text":  # a gépelt szöveg már ott van a képernyőn
            with self._lock:
                self._end_line()
                console.user(text)

    def delta(self, text: str) -> None:
        with self._lock:
            if self._printer is None:
                self._printer = console.StreamPrinter(self.name)
            self._printer.write(text)

    def assistant_end(self, text: str, streamed: bool) -> None:
        with self._lock:
            self._end_line()
            if not streamed:
                console.assistant(self.name, text)

    def assistant_abort(self) -> None:
        with self._lock:
            self._end_line()

    def tool_start(self, call_id: int, name: str, args: dict[str, Any]) -> None:
        with self._lock:
            self._end_line()

    def tool_done(self, call_id: int, name: str, args: dict[str, Any], result: str) -> None:
        arg_text = json.dumps(args, ensure_ascii=False) if args else ""
        with self._lock:
            console.tool(f"{name}({arg_text}) -> {summarize_result(result)}")

    def notice(self, text: str) -> None:
        with self._lock:
            self._end_line()
            console.assistant(self.name, text)

    def info(self, text: str) -> None:
        with self._lock:
            self._end_line()
            console.info(text)

    def warn(self, text: str) -> None:
        with self._lock:
            self._end_line()
            console.warn(text)

    def error(self, text: str) -> None:
        with self._lock:
            self._end_line()
            console.error(text)

    def timing(self, text: str) -> None:
        with self._lock:
            console.info(f"  ({text})")

    def level(self, value: float) -> None:
        pass

    # --- jóváhagyás: az ágens-szál vár, a főszál kérdez -------------------------------------

    @property
    def confirm_pending(self) -> bool:
        return self._confirm is not None

    def confirm(self, prompt: str, stop_event: threading.Event) -> bool:
        with self._lock:
            self._end_line()
            console.warn(prompt)
        done = threading.Event()
        answer: list[bool] = []
        self._confirm = (done, answer)
        while not done.wait(0.1):
            if stop_event.is_set():
                break
        self._confirm = None
        return bool(answer and answer[0])

    def answer_confirm(self, line: str) -> None:
        pending = self._confirm
        if pending is not None:
            pending[1].append(fold(line.strip()) in YES)
            pending[0].set()


def _wait_for_agent(app: JarvisApp, ui: ConsoleUI) -> None:
    """Vár, amíg a kérés lefut; közben kezeli a jóváhagyást és a Ctrl+C-t."""
    while (app.busy or (app.speech is not None and app.speech.busy)) and not app.exit_requested.is_set():
        try:
            if ui.confirm_pending:
                ui.answer_confirm(input("   Engedélyezed? Írd be: igen / nem > "))
                continue
            if not app.busy:
                break  # a válasz kész; a felolvasás a háttérben szólhat tovább
            time.sleep(0.05)
        except KeyboardInterrupt:
            app.stop_current()
        except EOFError:
            app.stop_current()
            return


def run_console(app: JarvisApp, ui: ConsoleUI) -> None:
    voice = app.voice_ready
    prompt = ("\n[Enter] beszéd | írj kérdést | /súgó | q = kilépés > "
              if voice else "\nTe (q = kilépés) > ")
    print(HELP if voice else "\nGépelős mód. Írd be a kérdésed, majd Enter. Kilépés: q")
    app.greet()
    while not app.exit_requested.is_set():
        try:
            line = input(console.color("2", prompt))
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            if app.busy or (app.speech is not None and app.speech.busy):
                app.stop_current()
                continue
            print()
            break
        if ui.confirm_pending:  # pl. ébresztőszóval indított kérés jóváhagyása
            ui.answer_confirm(line)
            continue
        command = fold(line.strip())
        if command in EXIT_COMMANDS:
            break
        if command in ("/sugo", "/help", "?"):
            print(HELP)
            continue
        if command in ("/uj", "/reset"):
            app.submit_reset()
        elif line.strip():
            app.submit_text(line.strip())
        elif voice:
            app.submit_voice("button")
        else:
            continue
        _wait_for_agent(app, ui)
    app.say_goodbye()
