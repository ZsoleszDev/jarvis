"""Konzolos kiírás egyszerű színezéssel."""
from __future__ import annotations

import os
import sys

_COLOR = False


def setup() -> None:
    """A kiírás ne álljon le furcsa karakteren, és legyen színes, ha a terminál tudja."""
    global _COLOR
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        _COLOR = False
        return
    if os.name == "nt":
        os.system("")  # bekapcsolja az ANSI-színeket a régi Windows-konzolban is
    _COLOR = True


def color(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def info(msg: str) -> None:
    print(color("2", msg), flush=True)


def ok(msg: str) -> None:
    print(color("32", "[OK] ") + msg, flush=True)


def warn(msg: str) -> None:
    print(color("33", "[!] " + msg), flush=True)


def error(msg: str) -> None:
    print(color("31", "[HIBA] " + msg), flush=True)


def user(text: str) -> None:
    print(color("36", "Te: ") + text, flush=True)


def assistant(name: str, text: str) -> None:
    print(color("1;32", f"{name}: ") + text, flush=True)


def tool(msg: str) -> None:
    print(color("35", "   [eszköz] ") + color("2", msg), flush=True)


def heading(msg: str) -> None:
    print("\n" + color("1", f"== {msg} =="), flush=True)


class StreamPrinter:
    """A folyamatosan érkező választ kiírja, ahogy jön ("Jarvis: ..." egy sorban)."""

    def __init__(self, name: str):
        self.name = name
        self.open = False
        self.wrote_any = False

    def write(self, text: str) -> None:
        text = text.replace("<think>", "").replace("</think>", "")
        if not self.open:
            text = text.lstrip()
            if not text:
                return
            sys.stdout.write(color("1;32", f"{self.name}: "))
            self.open = True
        sys.stdout.write(text)
        sys.stdout.flush()
        self.wrote_any = True

    def end(self) -> None:
        if self.open:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.open = False
