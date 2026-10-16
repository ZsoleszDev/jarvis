"""Mikrofonfelvétel: elindítás után addig vesz, amíg beszéd után csend nem lesz."""
from __future__ import annotations

import queue
from typing import Any, Callable

import numpy as np

from .config import AudioCfg

TARGET_RATE = 16000          # a Whisper 16 kHz-es monó hangot vár
BLOCK_SECONDS = 0.05         # 50 ms-os blokkokban dolgozunk
MIN_SPEECH_SECONDS = 0.15    # ennél rövidebb zaj (koppanás, kattanás) nem számít beszédnek
MAX_THRESHOLD = 0.2
STALL_SECONDS = 5.0          # ha ennyi ideig nem jön hang a mikrofonból, hibát jelzünk


def parse_device(spec: Any) -> int | str | None:
    """'' -> alapértelmezett eszköz, '3' -> 3-as sorszám, más szöveg -> névrészlet."""
    if spec is None or isinstance(spec, int):
        return spec
    text = str(spec).strip()
    if not text:
        return None
    return int(text) if text.isdigit() else text


def resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Egyszerű lineáris átmintavételezés (beszédhez bőven elég)."""
    audio = np.asarray(audio, dtype=np.float32)
    if src_rate == dst_rate or audio.size == 0:
        return audio
    n_out = int(round(audio.size * dst_rate / src_rate))
    x_old = np.linspace(0.0, 1.0, num=audio.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


def rms(block: np.ndarray) -> float:
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))


class Recorder:
    def __init__(self, cfg: AudioCfg, backend: Any = None):
        if backend is None:
            try:
                import sounddevice as backend  # type: ignore[no-redef]
            except OSError as exc:
                raise RuntimeError(
                    "A sounddevice nem találja a PortAudio hangkönyvtárat. "
                    "Windows-on a 'pip install sounddevice' tartalmazza; telepítsd újra."
                ) from exc
        self.sd = backend
        self.cfg = cfg
        self.device = parse_device(cfg.input_device)
        self.rate = self._pick_rate()
        self.threshold = float(cfg.min_threshold)
        self.noise_floor = 0.0

    def _pick_rate(self) -> int:
        """16 kHz, ha az eszköz tudja; különben az eszköz saját mintavétele (és átmintavételezés)."""
        try:
            self.sd.check_input_settings(device=self.device, samplerate=TARGET_RATE,
                                         channels=1, dtype="float32")
            return TARGET_RATE
        except Exception:  # noqa: BLE001 - PortAudio-hibák típusa eszközfüggő
            info = self.sd.query_devices(self.device, "input")
            return int(info["default_samplerate"])

    def device_name(self) -> str:
        try:
            return str(self.sd.query_devices(self.device, "input")["name"])
        except Exception:  # noqa: BLE001
            return "ismeretlen eszköz"

    def _open(self, blocks: queue.Queue) -> Any:
        def callback(indata, frames, time_info, status):  # noqa: ARG001
            blocks.put(indata[:, 0].copy())

        return self.sd.InputStream(samplerate=self.rate, channels=1, dtype="float32",
                                   blocksize=int(self.rate * BLOCK_SECONDS),
                                   device=self.device, callback=callback)

    @staticmethod
    def _next_block(blocks: queue.Queue) -> np.ndarray:
        # Rövid timeouttal várunk, hogy a Ctrl+C Windows alatt is működjön.
        waited = 0.0
        while True:
            try:
                return blocks.get(timeout=0.2)
            except queue.Empty:
                waited += 0.2
                if waited >= STALL_SECONDS:
                    raise RuntimeError("Nem érkezik hang a mikrofonból. Jó eszköz van beállítva?")

    def calibrate(self, seconds: float = 1.0) -> tuple[float, float]:
        """Megméri a háttérzajt, és ebből számolja a beszéd küszöbszintjét."""
        blocks: queue.Queue = queue.Queue()
        levels: list[float] = []
        elapsed = 0.0
        with self._open(blocks):
            while elapsed < seconds:
                block = self._next_block(blocks)
                levels.append(rms(block))
                elapsed += block.size / self.rate
        self.noise_floor = float(np.median(levels)) if levels else 0.0
        self.threshold = min(max(float(self.cfg.min_threshold),
                                 self.noise_floor * float(self.cfg.threshold_multiplier)),
                             MAX_THRESHOLD)
        return self.noise_floor, self.threshold

    def record(self, on_speech: Callable[[], None] | None = None,
               no_speech_timeout: float | None = None,
               cancel: Any = None,
               on_level: Callable[[float], None] | None = None) -> np.ndarray | None:
        """Felvesz egy mondatot. None, ha nem volt beszéd, vagy leállították (`cancel`).

        A felvétel akkor áll le, ha a beszéd után `silence_seconds` csend jön, vagy
        eléri a `max_record_seconds` hosszt. Ha `no_speech_timeout` ideig senki nem szól,
        None-nal tér vissza (alapból a beállításokból). Az `on_level` 0–1 közötti hangerőt
        kap blokkonként (a felület kijelzőjéhez).
        """
        wait_for_speech = (self.cfg.no_speech_timeout if no_speech_timeout is None
                           else no_speech_timeout)
        blocks: queue.Queue = queue.Queue()
        captured: list[np.ndarray] = []
        speech_started = False
        speech_time = silence_time = total_time = 0.0
        with self._open(blocks):
            while True:
                if cancel is not None and cancel.is_set():
                    return None
                block = self._next_block(blocks)
                captured.append(block)
                duration = block.size / self.rate
                total_time += duration
                level = rms(block)
                if on_level is not None:
                    on_level(min(1.0, level / max(self.threshold * 6, 1e-4)))
                if level >= self.threshold:
                    if not speech_started and on_speech:
                        on_speech()
                    speech_started = True
                    speech_time += duration
                    silence_time = 0.0
                elif speech_started:
                    silence_time += duration

                if speech_started and silence_time >= self.cfg.silence_seconds:
                    break
                if not speech_started and total_time >= wait_for_speech:
                    return None
                if total_time >= self.cfg.max_record_seconds:
                    break

        if speech_time < MIN_SPEECH_SECONDS:
            return None
        return resample(np.concatenate(captured), self.rate, TARGET_RATE)
