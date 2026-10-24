#!/usr/bin/env python3
"""Jarvis – helyi, magyar nyelvű asszisztens.

Indítás:
    python jarvis.py              grafikus felület (alkalmazásablak); ha már fut, megnyitja az ablakát
    python jarvis.py --console    konzolos mód
    python jarvis.py --no-window  a felület ne nyíljon meg magától (a címét kiírja)
    python jarvis.py --standby    készenlétben indul: ablak nélkül, a modellek az első kérésnél töltődnek be
    python jarvis.py --autostart on|off   a gép bekapcsolásakor készenlétben induljon (Windows)
    python jarvis.py --quit       leállítja a futó Jarvist
"""
from __future__ import annotations

import argparse
import os
import sys
import time


def _ensure_output() -> None:
    """A pythonw (pl. az automatikus indítás) nem ad konzolt: ilyenkor minden a naplófájlba megy
    (%LOCALAPPDATA%\\Jarvis\\naplo.txt), különben az első kiírásnál leállna a program."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    from jarvis_core.config import data_dir

    path = data_dir() / "naplo.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists() and path.stat().st_size > 2_000_000:
            os.replace(path, path.with_name("naplo.1.txt"))
    except OSError:
        pass
    stream = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
    stream.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    sys.stdout = sys.stderr = stream


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jarvis – helyi, magyar nyelvű asszisztens")
    parser.add_argument("--console", action="store_true", help="konzolos mód grafikus felület helyett")
    parser.add_argument("--no-window", action="store_true", help="a felület ne nyíljon meg magától")
    parser.add_argument("--standby", action="store_true",
                        help="készenlétben indul (ablak nélkül; a modellek az első kérésnél töltődnek be)")
    parser.add_argument("--autostart", choices=["on", "off"],
                        help="a gép bekapcsolásakor (Windows-bejelentkezéskor) készenlétben induljon")
    parser.add_argument("--quit", action="store_true", help="leállítja a futó Jarvist")
    parser.add_argument("--config", default=None, help="a config.toml elérési útja")
    args = parser.parse_args(argv)

    _ensure_output()
    from jarvis_core import console, instance
    from jarvis_core.config import app_data, load_config

    console.setup()
    if args.autostart:
        from jarvis_core.autostart import set_autostart

        console.info(set_autostart(args.autostart == "on", config=args.config))
        return 0
    if args.standby and args.console:
        console.error("A készenléti indítás a grafikus felülettel megy (a --console nélkül).")
        return 2

    cfg = load_config(args.config)
    base = app_data(cfg)
    info_path = base / instance.INFO_FILE
    if args.quit:
        info = instance.read_info(info_path)
        if info and instance.request_quit(info["url"]):
            console.info("Szóltam a Jarvisnak, leáll.")
            return 0
        console.info("Nem fut Jarvis (vagy nem válaszol).")
        return 1

    lock = instance.InstanceLock(base / instance.LOCK_FILE)
    if not lock.acquire():
        return _already_running(args, cfg, info_path)
    try:
        return _run(args, cfg, info_path)
    finally:
        lock.release()


def _already_running(args: argparse.Namespace, cfg, info_path) -> int:
    """Már fut egy Jarvis: a második indítás csak megnyitja az ablakát."""
    from jarvis_core import console, instance

    info = instance.read_info(info_path)
    if info is None or not instance.reachable(info["url"]):
        console.error("Egy másik Jarvis már fut, de nem válaszol. Ha biztosan nem fut, állítsd le a "
                      "Feladatkezelőben (python / pythonw), vagy indítsd újra a gépet.")
        return 1
    if args.standby or args.console or args.no_window:
        console.info(f"A Jarvis már fut. Az ablaka: {info['url']}")
        return 0
    from jarvis_core.gui import open_app_window

    open_app_window(info["url"], cfg.gui)
    console.info("A Jarvis már fut, megnyitottam az ablakát.")
    return 0


def _run(args: argparse.Namespace, cfg, info_path) -> int:
    from jarvis_core.app import JarvisApp

    if args.console:
        from jarvis_core.ui_console import ConsoleUI, run_console

        ui = ConsoleUI(cfg.assistant.name)
        app = JarvisApp(cfg, ui)
        try:
            if not app.start():
                return 1
            app.start_agent()
            run_console(app, ui)
        except KeyboardInterrupt:
            print()
        finally:
            app.shutdown()
        return 0

    from jarvis_core.gui import EventBus, GuiUI, run_gui

    bus = EventBus()
    ui = GuiUI(bus, cfg.assistant.name)
    app = JarvisApp(cfg, ui)
    try:
        run_gui(app, ui, bus, cfg.gui, open_window=not (args.no_window or args.standby),
                standby=args.standby, info_path=info_path)
    except KeyboardInterrupt:
        print()
    finally:
        app.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
