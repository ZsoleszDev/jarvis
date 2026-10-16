"""Ébresztőszó-figyelő: "Hey Jarvis" (openWakeWord, helyben, CPU-n).

Amíg figyel, ő használja a mikrofont. Ha meghallja az ébresztőszót, elengedi a mikrofont
(hogy a felvétel megkaphassa), és szól a Jarvisnak. Amíg a Jarvis dolgozik vagy beszél,
szünetel; utána a Jarvis újraindítja.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable

import numpy as np

from .config import AudioCfg, WakeWordCfg
from .recorder import TARGET_RATE, parse_device, resample

FRAME_SAMPLES = 1280  # 80 ms 16 kHz-en: ekkora darabokat vár az openWakeWord


class WakeWordError(Exception):
    """Érthető hibaüzenet, ha az ébresztőszó nem indítható."""


def load_model(cfg: WakeWordCfg) -> Any:
    """Betölti (és első alkalommal letölti) az openWakeWord modellt."""
    try:
        import openwakeword
        from openwakeword.model import Model
    except ImportError as exc:
        raise WakeWordError("telepítsd: pip install -r requirements-wakeword.txt") from exc
    try:
        try:
            openwakeword.utils.download_models([cfg.model])
        except TypeError:
            openwakeword.utils.download_models()
    except Exception as exc:  # noqa: BLE001
        raise WakeWordError(f"a modell letöltése nem sikerült ({exc})") from exc
    try:
        try:
            return Model(wakeword_models=[cfg.model], inference_framework="onnx")
        except TypeError:  # régebbi változat: nincs inference_framework paraméter
            return Model(wakeword_models=[cfg.model])
    except Exception as exc:  # noqa: BLE001
        raise WakeWordError(f"a modell nem tölthető be ({type(exc).__name__}: {exc})") from exc


def best_score(prediction: Any) -> float:
    if isinstance(prediction, dict):
        return max((float(v) for v in prediction.values()), default=0.0)
    return float(prediction or 0.0)


def chime(sd: Any, device: Any = None) -> None:
    """Rövid, kéthangú csengés: "hallak, mondhatod"."""
    rate = 22050
    t = np.arange(int(rate * 0.09)) / rate
    envelope = np.minimum(1.0, np.minimum(t / 0.01, (t[-1] - t) / 0.03))
    first = 0.16 * np.sin(2 * np.pi * 880 * t) * envelope
    second = 0.16 * np.sin(2 * np.pi * 1320 * t) * envelope
    audio = np.concatenate([first, np.zeros(int(rate * 0.03)), second]).astype(np.float32)
    try:
        sd.play(audio, rate, device=device)
        time.sleep(audio.size / rate + 0.05)
    except Exception:  # noqa: BLE001 - a csengés nem fontos annyira, hogy leálljon miatta bármi
        pass


class WakeWordListener:
    def __init__(self, cfg: WakeWordCfg, audio_cfg: AudioCfg, on_wake: Callable[[], None],
                 model: Any = None, backend: Any = None, log: Callable[[str], None] = print):
        self.cfg = cfg
        self.on_wake = on_wake
        self.log = log
        self.model = model if model is not None else load_model(cfg)
        if backend is None:
            import sounddevice as backend  # type: ignore[no-redef]
        self.sd = backend
        self.device = parse_device(audio_cfg.input_device)
        self._active = threading.Event()
        self._stopped = threading.Event()
        self._released = threading.Event()   # a mikrofon szabad
        self._released.set()
        self._last_wake = 0.0
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        self.resume()

    def resume(self) -> None:
        if not self._stopped.is_set():
            self._active.set()

    def pause(self, wait: bool = True) -> None:
        """Szünetel, és (alapból) megvárja, hogy elengedje a mikrofont."""
        self._active.clear()
        if wait:
            self._released.wait(1.5)

    @property
    def listening(self) -> bool:
        return self._active.is_set() and not self._released.is_set()

    def stop(self) -> None:
        self._stopped.set()
        self._active.clear()
        if self._thread.is_alive():
            self._thread.join(2)

    def _rate(self) -> int:
        try:
            self.sd.check_input_settings(device=self.device, samplerate=TARGET_RATE,
                                         channels=1, dtype="int16")
            return TARGET_RATE
        except Exception:  # noqa: BLE001
            return int(self.sd.query_devices(self.device, "input")["default_samplerate"])

    def _run(self) -> None:
        while not self._stopped.is_set():
            if not self._active.wait(0.2) or self._stopped.is_set():
                continue
            self._released.clear()
            woke = False
            try:
                woke = self._listen()
            except Exception as exc:  # noqa: BLE001 - pl. kihúzták a mikrofont
                self.log(f"Ébresztőszó-figyelő hiba: {exc}")
                time.sleep(2)
            finally:
                self._released.set()
            if woke:
                try:
                    self.on_wake()
                except Exception as exc:  # noqa: BLE001
                    self.log(f"Ébresztőszó-kezelési hiba: {exc}")

    def _listen(self) -> bool:
        """Addig figyel, amíg aktív. Igazzal tér vissza, ha meghallotta az ébresztőszót."""
        rate = self._rate()
        blocks: queue.Queue = queue.Queue()

        def callback(indata, frames, time_info, status):  # noqa: ARG001
            blocks.put(indata[:, 0].copy())

        reset = getattr(self.model, "reset", None)
        if callable(reset):
            reset()  # ne a korábbi hangfoszlányok alapján ébredjen
        blocksize = int(rate * FRAME_SAMPLES / TARGET_RATE)
        with self.sd.InputStream(samplerate=rate, channels=1, dtype="int16", blocksize=blocksize,
                                 device=self.device, callback=callback):
            while self._active.is_set() and not self._stopped.is_set():
                try:
                    block = blocks.get(timeout=0.2)
                except queue.Empty:
                    continue
                if rate != TARGET_RATE:
                    block = (resample(block.astype(np.float32) / 32768.0, rate, TARGET_RATE)
                             * 32767).astype(np.int16)
                score = best_score(self.model.predict(block))
                now = time.monotonic()
                if score >= float(self.cfg.threshold) and now - self._last_wake >= float(self.cfg.cooldown):
                    self._last_wake = now
                    self._active.clear()  # elengedjük a mikrofont a felvételnek
                    return True
        return False
