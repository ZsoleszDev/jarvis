"""Felolvasás Piperrel (helyi, magyar neurális hang)."""
from __future__ import annotations

import queue
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .config import AudioCfg, TtsCfg, resolve_path
from .recorder import parse_device
from .textutil import clean_for_speech

VOICE_RE = re.compile(
    r"^(?P<lang>[a-z]{2,3})_(?P<region>[A-Z]{2})-(?P<name>[A-Za-z0-9_]+)-(?P<quality>x_low|low|medium|high)$"
)
VOICES_BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
SENTENCE_PAUSE = 0.12  # másodperc szünet a mondatok között


def voice_urls(voice: str) -> tuple[str, str]:
    """A Piper-hang (.onnx) és beállításfájljának (.onnx.json) letöltési címe."""
    match = VOICE_RE.match(voice)
    if not match:
        raise ValueError(f"Érvénytelen hangnév: '{voice}' (helyes példa: hu_HU-imre-medium)")
    lang, region, name, quality = match.group("lang", "region", "name", "quality")
    base = f"{VOICES_BASE_URL}/{lang}/{lang}_{region}/{name}/{quality}/{voice}"
    return base + ".onnx?download=true", base + ".onnx.json?download=true"


def ensure_voice(voice: str, voices_dir: Path,
                 log: Callable[[str], None] = print) -> tuple[Path, Path]:
    """Letölti a hangot, ha még nincs meg (egyszer, kb. 60 MB)."""
    voices_dir.mkdir(parents=True, exist_ok=True)
    model_path = voices_dir / f"{voice}.onnx"
    config_path = voices_dir / f"{voice}.onnx.json"
    model_url, config_url = voice_urls(voice)
    for url, path in ((config_url, config_path), (model_url, model_path)):
        if path.exists() and path.stat().st_size > 0:
            continue
        log(f"Hang letöltése: {path.name} ...")
        partial = path.with_name(path.name + ".part")
        with urllib.request.urlopen(url, timeout=60) as resp, open(partial, "wb") as out:
            while chunk := resp.read(1 << 16):
                out.write(chunk)
        partial.replace(path)
    return model_path, config_path


class Speaker:
    """Szálbiztos felolvasó (az időzítők másik szálról is szólhatnak)."""

    def __init__(self, tts_cfg: TtsCfg, audio_cfg: AudioCfg,
                 log: Callable[[str], None] = print, backend: Any = None):
        self.cfg = tts_cfg
        self.enabled = tts_cfg.engine.strip().lower() == "piper"
        self.device = parse_device(audio_cfg.output_device)
        self._lock = threading.Lock()
        self._synth_lock = threading.Lock()   # a telefonos felolvasás is használhatja közben
        self._stop = threading.Event()
        self.voice = None
        self.sd = None
        self.sample_rate = 22050
        if not self.enabled:
            return

        if backend is None:
            import sounddevice as backend  # type: ignore[no-redef]
        self.sd = backend
        from piper import PiperVoice, SynthesisConfig

        model_path, config_path = ensure_voice(tts_cfg.voice, resolve_path(tts_cfg.voices_dir), log)
        self.voice = PiperVoice.load(str(model_path), config_path=str(config_path))
        self.syn_config = SynthesisConfig(length_scale=float(tts_cfg.length_scale),
                                          volume=float(tts_cfg.volume))
        config = getattr(self.voice, "config", None)
        self.sample_rate = int(getattr(config, "sample_rate", self.sample_rate))

    def synthesize(self, text: str) -> np.ndarray:
        parts: list[np.ndarray] = []
        for chunk in self.voice.synthesize(text, syn_config=self.syn_config):
            self.sample_rate = int(chunk.sample_rate)
            if parts:
                parts.append(np.zeros(int(self.sample_rate * SENTENCE_PAUSE), dtype=np.int16))
            parts.append(np.asarray(chunk.audio_int16_array, dtype=np.int16).reshape(-1))
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)

    def prepare(self, text: str) -> tuple[np.ndarray, int] | None:
        """Szövegből hang (még lejátszás nélkül). None, ha nincs mit felolvasni."""
        if not self.enabled or self.voice is None:
            return None
        speech = clean_for_speech(text)
        if not speech:
            return None
        with self._synth_lock:
            audio = self.synthesize(speech)
        return (audio, self.sample_rate) if audio.size else None

    def wav(self, text: str) -> bytes | None:
        """Felolvasás WAV-fájlként (a telefonnak), lejátszás nélkül."""
        import io
        import wave

        prepared = self.prepare(text)
        if prepared is None:
            return None
        audio, rate = prepared
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(int(rate))
            out.writeframes(np.asarray(audio, dtype=np.int16).tobytes())
        return buffer.getvalue()

    def close(self) -> None:
        """Elhallgat, és elengedi a hangmodellt (a Szünet gombhoz)."""
        self.stop()
        with self._synth_lock:
            self.voice = None
        self.enabled = False

    def play(self, audio: np.ndarray, rate: int) -> None:
        """Lejátssza a hangot, és megvárja a végét (a stop() bármelyik szálról megszakítja)."""
        with self._lock:
            self._stop.clear()
            self.sd.play(audio, rate, device=self.device)
            try:
                self._wait(audio.size / rate)
            except BaseException:
                self.sd.stop()  # Ctrl+C: azonnal elhallgat
                raise

    def stop(self) -> None:
        self._stop.set()
        if self.sd is not None:
            try:
                self.sd.stop()
            except Exception:  # noqa: BLE001
                pass

    def say(self, text: str) -> None:
        """Felolvas és megvárja a végét (egyszerű, blokkoló változat)."""
        prepared = self.prepare(text)
        if prepared is not None:
            self.play(*prepared)

    def _wait(self, seconds: float) -> None:
        # Rövid alvásokkal várunk (az sd.wait() Windows-on nem szakítható meg Ctrl+C-vel).
        deadline = time.monotonic() + seconds + 1.0
        try:
            stream = self.sd.get_stream()
        except Exception:  # noqa: BLE001
            stream = None
        while time.monotonic() < deadline and not self._stop.is_set():
            if stream is not None and not stream.active:
                return
            time.sleep(0.05)


class SpeechQueue:
    """Folyamatos felolvasás két háttérszállal.

    Az egyik a beérkező mondatokat hanggá alakítja, a másik lejátssza őket. Így amíg az
    első mondat szól, a következő már készül, és a válasz eleje akkor is megszólal, amikor
    a modell még a végét írja. Az időzítők másik szálról is küldhetnek ide szöveget.
    """

    def __init__(self, speaker: Speaker, on_change: Callable[[bool], None] | None = None):
        self.speaker = speaker
        self.on_change = on_change  # True: elkezdett beszélni, False: elhallgatott
        self._texts: queue.Queue = queue.Queue()
        self._audio: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._pending = 0
        self._generation = 0  # megszakításkor nő: a régebbi tételek kimaradnak
        self.first_audio_at: float | None = None
        for target in (self._synth_loop, self._play_loop):
            threading.Thread(target=target, daemon=True).start()

    def _notify(self, busy: bool) -> None:
        if self.on_change is not None:
            try:
                self.on_change(busy)
            except Exception:  # noqa: BLE001 - a felület hibája ne állítsa le a hangot
                pass

    def say(self, text: str) -> None:
        if not text or not text.strip():
            return
        with self._lock:
            started = self._pending == 0
            self._pending += 1
            generation = self._generation
        if started:
            self._notify(True)
        self._texts.put((generation, text))

    def mark_turn(self) -> None:
        """Új kérés kezdődik: innen mérjük, mikor szólal meg először."""
        self.first_audio_at = None

    @property
    def busy(self) -> bool:
        return self._pending > 0

    def wait(self, timeout: float | None = None) -> None:
        """Megvárja, amíg minden elhangzik (rövid alvásokkal, hogy a Ctrl+C működjön)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.busy and (deadline is None or time.monotonic() < deadline):
            time.sleep(0.05)

    def interrupt(self) -> None:
        """Azonnal elhallgat, és eldobja a még fel nem olvasott mondatokat."""
        with self._lock:
            self._generation += 1
        self.speaker.stop()

    def _done(self) -> None:
        with self._lock:
            self._pending = max(0, self._pending - 1)
            finished = self._pending == 0
        if finished:
            self._notify(False)

    def _synth_loop(self) -> None:
        while True:
            generation, text = self._texts.get()
            prepared = None
            if generation == self._generation:
                try:
                    prepared = self.speaker.prepare(text)
                except Exception:  # noqa: BLE001 - egy hibás mondat ne állítsa le a felolvasást
                    prepared = None
            if prepared is None or generation != self._generation:
                self._done()
                continue
            self._audio.put((generation, prepared))

    def _play_loop(self) -> None:
        while True:
            generation, (audio, rate) = self._audio.get()
            if generation == self._generation:
                if self.first_audio_at is None:
                    self.first_audio_at = time.perf_counter()
                try:
                    self.speaker.play(audio, rate)
                except Exception:  # noqa: BLE001
                    pass
            self._done()
