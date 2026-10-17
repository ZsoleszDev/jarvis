#!/usr/bin/env python3
"""Jarvis telepítés-ellenőrző.

Futtatás:
    python check_setup.py               minden teszt (a végén mikrofon- és hangszóróteszt)
    python check_setup.py --no-audio    mikrofon- és hangszóróteszt nélkül
"""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import platform
import shutil
import sys
import time

from jarvis_core import console, cuda_libs

# A GPU-könyvtárak keresési útját még a faster-whisper importja előtt be kell állítani.
cuda_libs.prepare()

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "", optional: bool = False) -> bool:
    """Feljegyez egy ellenőrzést. Az opcionális képesség hiánya csak figyelmeztetés."""
    text = f"{name}: {detail}" if detail else name
    if ok:
        console.ok(text)
    elif optional:
        console.warn(text + " (nem kötelező)")
    else:
        console.error(text)
    if ok or not optional:
        results.append((name, ok, detail))
    return ok


def check_python() -> None:
    console.heading("Python")
    ver = sys.version_info
    detail = f"{platform.python_version()} ({sys.executable})"
    record("Python-verzió", ver >= (3, 11), detail if ver >= (3, 11) else detail + " – legalább 3.11 kell")


PACKAGES = [
    ("numpy", "numpy"),
    ("requests", "requests"),
    ("psutil", "psutil"),
    ("sounddevice", "sounddevice"),
    ("faster_whisper", "faster-whisper"),
    ("ctranslate2", "ctranslate2"),
    ("piper", "piper-tts"),
    ("onnxruntime", "onnxruntime"),
    ("ddgs", "ddgs"),
    ("trafilatura", "trafilatura"),
    ("playwright", "playwright"),
    ("mss", "mss"),
    ("PIL", "pillow"),
]
OPTIONAL_PACKAGES = [("openwakeword", "openwakeword", "pip install -r requirements-wakeword.txt"),
                     ("cryptography", "cryptography", "a laptopos értesítésekhez bezárt ablaknál: pip install cryptography")]


def check_packages() -> None:
    console.heading("Csomagok")
    for module, dist in PACKAGES:
        try:
            importlib.import_module(module)
            try:
                version = importlib.metadata.version(dist)
            except importlib.metadata.PackageNotFoundError:
                version = "?"
            record(dist, True, version)
        except Exception as exc:  # noqa: BLE001
            record(dist, False, f"nem importálható ({type(exc).__name__}: {exc}) – pip install -r requirements.txt")
    for module, dist, hint in OPTIONAL_PACKAGES:
        try:
            importlib.import_module(module)
            record(dist, True, importlib.metadata.version(dist))
        except Exception as exc:  # noqa: BLE001
            record(dist, False, f"nincs telepítve ({type(exc).__name__}) – {hint}", optional=True)


def check_gpu() -> None:
    console.heading("Videókártyák")
    from jarvis_core import gpu

    if not shutil.which("nvidia-smi"):
        record("nvidia-smi", False, "nem található (NVIDIA-driver?)")
        return
    gpus = gpu.query_gpus(timeout=10)
    for line in gpu.memory_lines(gpus):
        console.info("  " + line)
    console.info("  (A config.toml stt.gpu_device_index értéke ezt a sorszámot használja.)")
    record("nvidia-smi", bool(gpus), "" if gpus else "nem adott vissza adatot")


def check_ollama(cfg):
    """Visszaadja a klienst, ha betöltötte a modellt (a végén kiürítjük)."""
    from jarvis_core.llm import FULL_GPU, OllamaClient, OllamaError, gpu_share
    from jarvis_core.tools import Toolbox

    console.heading("Ollama és nyelvi modell")
    llm = OllamaClient(cfg.llm)
    try:
        version = llm.ensure_ready()
        record("Ollama", True, f"verzió {version}, modell: {cfg.llm.model}")
    except OllamaError as exc:
        record("Ollama", False, str(exc))
        return None

    try:
        t0 = time.perf_counter()
        status = llm.load_fitted(log=console.info) or {}
        record("Modell betöltése", True, f"{time.perf_counter() - t0:.1f} mp")
        share = gpu_share(status)
        if share is not None:
            percent = round(share * 100)
            record("Modell a GPU-n", share >= FULL_GPU, f"{percent}% a VRAM-ban, {status['size'] / 2**30:.1f} GB"
                   + ("" if share >= FULL_GPU else " – a többi a processzoron fut, lassú lesz. "
                      "Zárd be a videókártyát használó programokat (böngésző, játék), és próbáld újra."))
        context = int(status.get("context_length") or llm.num_ctx or 0)
        if context:
            note = ""
            if llm.num_ctx and llm.num_ctx < int(cfg.llm.num_ctx or 0):
                note = f" (a beállított {cfg.llm.num_ctx} helyett, hogy elférjen a videókártyákon)"
            console.info(f"  Kontextus: {context} token{note}")

        t0 = time.perf_counter()
        msg = llm.chat([{"role": "user", "content": "Mutatkozz be egy rövid mondatban, magyarul."}])
        speed = llm.tokens_per_second()
        record("Válasz", bool(msg.get("content")),
               f"{time.perf_counter() - t0:.1f} mp, {speed:.0f} token/mp" if speed else "")
        console.info(f"  „{msg.get('content', '').strip()[:150]}”")
        if speed is not None and speed < 12:
            console.warn("  Lassú generálás: nézd meg az 'ollama ps' parancsot, 100% GPU legyen.")

        first_word: list[float] = []
        t0 = time.perf_counter()
        llm.chat([{"role": "user", "content": "Mondj egy rövid mondatot az időjárásról."}],
                 on_text=lambda _t: first_word or first_word.append(time.perf_counter() - t0))
        if first_word:
            record("Első szó", first_word[0] < 3, f"{first_word[0]:.2f} mp után kezd válaszolni")

        toolbox = Toolbox(cfg)
        msg = llm.chat([{"role": "user", "content": "Hány óra van most?"}], tools=toolbox.schemas())
        names = [c["function"]["name"] for c in msg.get("tool_calls", [])]
        record("Eszközhívás", "get_datetime" in names,
               f"a modell ezt hívta: {', '.join(names) or 'semmit'}")
    except OllamaError as exc:
        record("Ollama-teszt", False, str(exc))
    return llm


def check_vision(llm) -> None:
    """Rajzol egy képet egy számmal, és megkérdezi a modellt, mit lát rajta."""
    from jarvis_core.llm import OllamaError

    console.heading("Látás (képelemzés a Qwen-nel)")
    try:
        import base64
        import io

        from PIL import Image, ImageDraw, ImageFont

        image = Image.new("RGB", (640, 240), "white")
        draw = ImageDraw.Draw(image)
        try:
            font = ImageFont.load_default(size=110)
        except TypeError:  # régi Pillow
            font = ImageFont.load_default()
        draw.text((40, 50), "JARVIS 427", fill="black", font=font)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        t0 = time.perf_counter()
        answer = llm.vision("Milyen szám látható a képen? Csak a számot írd le.",
                            base64.b64encode(buffer.getvalue()).decode("ascii"))
        record("Képfelismerés", "427" in answer,
               f"„{answer[:60]}” ({time.perf_counter() - t0:.1f} mp)"
               + ("" if "427" in answer else " – ha hibát ad, frissítsd az Ollamát"))
    except ImportError:
        record("Képfelismerés", False, "pip install pillow")
    except OllamaError as exc:
        record("Képfelismerés", False, f"{exc} – frissítsd az Ollamát")


def check_web(cfg) -> None:
    from jarvis_core import web

    console.heading("Web (keresés és oldalolvasás)")
    try:
        t0 = time.perf_counter()
        results = web.search("Magyarország fővárosa", cfg.web)
        record("Webes keresés", bool(results), f"{len(results)} találat, {time.perf_counter() - t0:.1f} mp")
    except web.WebError as exc:
        record("Webes keresés", False, str(exc))
    try:
        page = web.read_webpage("https://hu.wikipedia.org/wiki/Budapest", cfg.web)
        record("Oldalolvasás", "Budapest" in page, f"{len(page)} karakter a Wikipédiáról")
    except web.WebError as exc:
        record("Oldalolvasás", False, str(exc))


def check_browser(cfg) -> None:
    from jarvis_core.browser import BrowserController, BrowserError

    console.heading("Saját böngésző (Playwright)")
    if not cfg.browser.enabled:
        record("Böngésző", True, "kikapcsolva ([browser] enabled = false)")
        return
    browser = BrowserController(cfg.browser, allow_local=cfg.web.allow_local)
    try:
        t0 = time.perf_counter()
        view = browser.open("https://example.com")
        engine = {"msedge": "Edge", "chromium": "Playwright Chromium"}.get(browser.engine, browser.engine)
        record("Böngésző", "Example Domain" in view,
               f"{engine}, megnyitotta az example.com-ot, {len(browser.elements)} kezelhető elem, "
               f"{time.perf_counter() - t0:.1f} mp")
        if cfg.browser.channel == "msedge" and browser.engine != "msedge":
            console.warn("  Az Edge nem indult, a Playwright saját Chromiumát használom helyette.")
    except BrowserError as exc:
        record("Böngésző", False, str(exc))
    finally:
        browser.close()


def check_phone(cfg) -> None:
    """A telefonos kapcsolat: szabad-e a port, és a Tailscale átadja-e a telefonnak (tailscale serve)."""
    import shutil
    import socket
    import subprocess

    console.heading("Telefonos kapcsolat (Tailscale)")
    if not cfg.api.enabled:
        record("Telefonos kapcsolat", True, "kikapcsolva ([api] enabled = false)")
        return
    try:
        with socket.socket() as sock:
            sock.bind((cfg.api.host, int(cfg.api.port)))
        record("Telefonos port", True, f"{cfg.api.host}:{cfg.api.port} szabad")
    except OSError:
        record("Telefonos port", False, f"{cfg.api.host}:{cfg.api.port} foglalt (fut már a Jarvis, vagy más program "
               "használja); másik port: [api] port a config.toml-ban", optional=True)
    if cfg.api.host not in ("127.0.0.1", "localhost", "::1"):
        console.warn("  Az [api] host nem 127.0.0.1, így a végpontok a helyi hálózatról is elérhetők. "
                     "Tailscale-lel maradjon 127.0.0.1.")
    exe = shutil.which("tailscale")
    if not exe:
        record("Tailscale", False, "nincs telepítve (tailscale.com/download); a telefon ezen át éri el a gépet",
               optional=True)
        return
    try:
        proc = subprocess.run([exe, "serve", "status"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        record("Tailscale serve", False, str(exc), optional=True)
        return
    text = (proc.stdout + proc.stderr).strip()
    served = f"127.0.0.1:{cfg.api.port}" in text or f"localhost:{cfg.api.port}" in text
    url = next((line.split()[0] for line in text.splitlines() if line.strip().startswith("https://")), "")
    record("Tailscale serve", served,
           (f"a telefon ezt a címet használja: {url}" if url else "beállítva") if served
           else f"még nincs beállítva; futtasd egyszer: tailscale serve --bg {cfg.api.port}", optional=True)


def check_running(cfg) -> None:
    """Fut-e már a Jarvis (pl. készenlétben a gép bekapcsolása óta), és be van-e kapcsolva az
    automatikus indítás. Ha fut, a telefonos port és a böngészőprofil alább foglaltnak látszhat."""
    import os

    from jarvis_core import autostart, instance
    from jarvis_core.config import app_data

    console.heading("Indítás")
    info = instance.read_info(app_data(cfg) / instance.INFO_FILE)
    if info and instance.reachable(info["url"]):
        how = "készenléti indítással" if info.get("standby") else "ablakkal"
        console.info(f"  A Jarvis már fut ({how}). Ha alább a telefonos port vagy a böngésző foglaltnak "
                     "látszik, az emiatt van. Leállítás: python jarvis.py --quit")
    else:
        console.info("  A Jarvis most nem fut.")
    if os.name == "nt":
        on = (autostart.startup_folder() / autostart.LINK_NAME).exists()
        console.info("  Automatikus indítás: " + ("be (a gép bekapcsolásakor készenlétben indul; kikapcsolás: "
                                                 "python jarvis.py --autostart off)" if on else
                                                 "ki (bekapcsolás: python jarvis.py --autostart on)"))


def check_screen(cfg) -> None:
    from jarvis_core import screen

    console.heading("Képernyőnézés")
    if not cfg.screen.enabled:
        record("Képernyőkép", True, "kikapcsolva ([screen] enabled = false)")
        return
    try:
        for line in screen.list_monitors():
            console.info("  Monitor " + line + ("   <- ezt nézi" if line.startswith(f"{cfg.screen.monitor}:") else ""))
        data = screen.capture_b64(cfg.screen)
        record("Képernyőkép", bool(data), f"{len(data) * 3 // 4 // 1024} KB")
    except screen.ScreenError as exc:
        record("Képernyőkép", False, str(exc))
    except Exception as exc:  # noqa: BLE001 - pl. multiseat-ülés jogosultsági gondja
        record("Képernyőkép", False, f"{type(exc).__name__}: {exc}")


def check_wake_word(cfg) -> None:
    from jarvis_core.wakeword import WakeWordError, best_score, load_model

    console.heading("Ébresztőszó („Hey Jarvis”)")
    if not cfg.wake_word.enabled:
        record("Ébresztőszó", True, "kikapcsolva ([wake_word] enabled = false)")
        return
    try:
        import numpy as np

        t0 = time.perf_counter()
        model = load_model(cfg.wake_word)
        silence = np.zeros(1280, dtype=np.int16)
        score = max(best_score(model.predict(silence)) for _ in range(10))
        record("Ébresztőszó", score < cfg.wake_word.threshold,
               f"modell betöltve ({time.perf_counter() - t0:.1f} mp), csendre {score:.2f} pont "
               f"(küszöb: {cfg.wake_word.threshold})", optional=True)
    except WakeWordError as exc:
        record("Ébresztőszó", False, str(exc), optional=True)
    except Exception as exc:  # noqa: BLE001 - a régi openWakeWord új Pythonnal elhasalhat
        record("Ébresztőszó", False, f"{type(exc).__name__}: {exc}", optional=True)


def check_stt(cfg):
    from jarvis_core.stt import SpeechToText

    console.heading("Beszédfelismerés (faster-whisper)")
    usable, reason = cuda_libs.status()
    record("CUDA-könyvtárak", usable, reason)
    if not usable:
        console.info("  GPU nélkül is működik, csak lassabb és pontatlanabb (CPU-s 'small' modell).")
    try:
        t0 = time.perf_counter()
        stt = SpeechToText(cfg.stt, log=console.warn)
        record("Felismerő modell", True, f"{stt.describe()}, betöltés {time.perf_counter() - t0:.1f} mp")
        return stt
    except Exception as exc:  # noqa: BLE001
        record("Felismerő modell", False, f"{type(exc).__name__}: {exc}")
        return None


def list_audio_devices():
    console.heading("Hangeszközök")
    try:
        import sounddevice as sd
    except Exception as exc:  # noqa: BLE001
        record("sounddevice", False, str(exc))
        return None
    devices = sd.query_devices()
    default_in, default_out = sd.default.device
    for idx, dev in enumerate(devices):
        roles = []
        if dev["max_input_channels"] > 0:
            roles.append("BE" + ("*" if idx == default_in else ""))
        if dev["max_output_channels"] > 0:
            roles.append("KI" + ("*" if idx == default_out else ""))
        console.info(f"  [{idx:2}] {'/'.join(roles):7} {dev['name']}")
    console.info("  (* = alapértelmezett. A config.toml [audio] részében sorszámmal vagy névvel választhatsz.)")
    return sd


def check_tts(cfg) -> None:
    from jarvis_core.tts import Speaker

    console.heading("Felolvasás (Piper)")
    if cfg.tts.engine.strip().lower() == "none":
        record("Felolvasás", True, "kikapcsolva (tts.engine = none)")
        return
    try:
        speaker = Speaker(cfg.tts, cfg.audio, log=console.info)
        t0 = time.perf_counter()
        audio = speaker.synthesize("Minden rendszer működik. Jó estét kívánok!")
        record("Hangszintézis", audio.size > 0,
               f"{cfg.tts.voice}, {audio.size / speaker.sample_rate:.1f} mp hang "
               f"{time.perf_counter() - t0:.2f} mp alatt")
        console.info("  Lejátszás... (hallod?)")
        speaker.say("Minden rendszer működik. Jó estét kívánok!")
    except Exception as exc:  # noqa: BLE001
        record("Felolvasás", False, f"{type(exc).__name__}: {exc}")


def check_microphone(cfg, stt) -> None:
    import numpy as np

    from jarvis_core.recorder import Recorder

    console.heading("Mikrofon")
    try:
        recorder = Recorder(cfg.audio)
        console.info(f"  Eszköz: {recorder.device_name()} ({recorder.rate} Hz)")
        console.info("  Zajszint mérése, maradj csendben egy másodpercig...")
        noise, threshold = recorder.calibrate()
        console.info(f"  zajszint {noise:.4f}, beszédküszöb {threshold:.4f}")
        input("  Nyomj Entert, majd mondj egy mondatot (pl. „Jarvis, nyisd meg a jegyzettömböt”)... ")
        audio = recorder.record(on_speech=lambda: console.info("  ...beszéd észlelve"))
    except Exception as exc:  # noqa: BLE001
        record("Mikrofon", False, f"{type(exc).__name__}: {exc}")
        return
    if audio is None:
        record("Mikrofon", False, "nem érzékelt beszédet – jó eszköz van beállítva? "
                                  "Próbáld csökkenteni a min_threshold értékét.")
        return
    peak = float(np.max(np.abs(audio)))
    record("Mikrofon", True, f"{audio.size / 16000:.1f} mp felvétel, csúcsszint {peak:.2f}")
    if peak > 0.98:
        console.warn("  A hang túlvezérelt, vedd lejjebb a mikrofon erősítését a Windows hangbeállításaiban.")
    if stt is not None:
        t0 = time.perf_counter()
        text = stt.transcribe(audio)
        record("Felismert szöveg", bool(text), f"„{text}” ({time.perf_counter() - t0:.1f} mp)"
               if text else "üres")


def main() -> int:
    parser = argparse.ArgumentParser(description="Jarvis telepítés-ellenőrző")
    parser.add_argument("--no-audio", action="store_true", help="mikrofon- és hangszóróteszt kihagyása")
    parser.add_argument("--no-llm", action="store_true", help="az Ollama-teszt kihagyása")
    parser.add_argument("--config", default=None, help="a config.toml elérési útja")
    args = parser.parse_args()

    console.setup()
    check_python()
    check_packages()

    from jarvis_core.config import adopt_legacy_data, data_dir, load_config

    cfg = load_config(args.config)
    console.heading("Beállítások")
    record("config.toml", True, str(cfg.source or "nincs, alapértékek"))
    for warning in cfg.warnings:
        console.warn("  " + warning)
    # A régi verziók a projektmappában tartották a bejelentkezéseket és a tárgyakat: ha még ott
    # vannak, előbb áthozzuk őket (különben a böngészőteszt egy új, üres profilt kezdene).
    adopt_legacy_data(cfg.browser.profile_dir, "browser_profile", lambda text: console.info("  " + text))
    adopt_legacy_data(cfg.work.dir, "munka", lambda text: console.info("  " + text))
    console.info(f"  Adatmappa (bejelentkezések, eladásra szánt tárgyak): {data_dir()}")
    check_running(cfg)

    check_gpu()
    llm = None if args.no_llm else check_ollama(cfg)
    if llm is not None:
        check_vision(llm)
    check_web(cfg)
    check_browser(cfg)
    check_phone(cfg)
    check_screen(cfg)
    check_wake_word(cfg)
    stt = check_stt(cfg)
    sd = list_audio_devices()
    if not args.no_audio and sd is not None:
        check_tts(cfg)
        check_microphone(cfg, stt)

    if llm is not None and cfg.llm.unload_on_exit:
        try:
            llm.unload()
        except Exception:  # noqa: BLE001
            pass

    console.heading("Összegzés")
    failed = [r for r in results if not r[1]]
    if failed:
        for name, _, detail in failed:
            console.error(f"{name}: {detail}")
        console.info(f"{len(results) - len(failed)} rendben, {len(failed)} hiba.")
        return 1
    console.ok(f"Mind a {len(results)} ellenőrzés rendben. Indítás: python jarvis.py")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
