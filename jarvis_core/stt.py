"""Beszédfelismerés faster-whisperrel: GPU-n, ha lehet, különben CPU-n."""
from __future__ import annotations

import gc
import os
from typing import Callable

import numpy as np

from . import cuda_libs
from .config import SttCfg
from .recorder import TARGET_RATE
from .textutil import fold

# Windows-on a Hugging Face gyorsítótár szimbolikus link nélkül is működik, a figyelmeztetés felesleges.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# A Whisper csendre vagy zajra néha "feliratos" szöveget talál ki – ezeket eldobjuk.
_HALLUCINATIONS = (
    "feliratozta", "feliratok:", "amara.org", "megnezted a videot",
    "koszonom a figyelmet", "iratkozz fel",
)
_APPROX_SIZES = {"small": "~0,5 GB", "medium": "~1,5 GB", "turbo": "~1,6 GB",
                 "large-v3-turbo": "~1,6 GB", "large-v3": "~3 GB"}
_MODEL_FILES = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]


class SpeechToText:
    def __init__(self, cfg: SttCfg, log: Callable[[str], None] = print):
        self.cfg = cfg
        self.log = log
        self.model = None
        self.device = "cpu"
        self.model_name = cfg.cpu_model

        wanted = cfg.device.strip().lower()
        if wanted not in ("auto", "cuda", "cpu"):
            log(f"Ismeretlen stt.device érték: '{cfg.device}', 'auto'-t használok.")
            wanted = "auto"

        if wanted in ("auto", "cuda"):
            usable, reason = cuda_libs.status()
            if usable:
                try:
                    self._load("cuda")
                    return
                except Exception as exc:  # noqa: BLE001
                    self._unload()
                    if wanted == "cuda":
                        raise RuntimeError(f"A GPU-s beszédfelismerés nem indult: {exc}") from exc
                    log(f"A GPU-s beszédfelismerés nem indult ({exc}), CPU-ra váltok.")
            elif wanted == "cuda":
                raise RuntimeError(f"A GPU-s beszédfelismerés nem elérhető: {reason}")
            else:
                log(f"GPU-s beszédfelismerés nem elérhető ({reason}), CPU-t használok.")
        self._load("cpu")

    def _ensure_downloaded(self, name: str) -> None:
        """Első alkalommal folyamatjelzővel tölti le a modellt (a faster-whisper ezt csendben tenné)."""
        try:
            from faster_whisper.utils import _MODELS, download_model
        except ImportError:
            return
        try:
            download_model(name, local_files_only=True)
            return  # már a gépen van
        except Exception:  # noqa: BLE001 - nincs még letöltve
            pass
        size = _APPROX_SIZES.get(name, "néhány száz MB")
        self.log(f"A(z) '{name}' beszédfelismerő modell letöltése ({size}, csak egyszer kell)...")
        try:
            from huggingface_hub import snapshot_download

            snapshot_download(_MODELS.get(name, name), allow_patterns=_MODEL_FILES)
        except Exception as exc:  # noqa: BLE001 - a WhisperModel még megpróbálja maga
            self.log(f"A letöltés folyamatjelző nélkül folytatódik ({exc}).")

    def _load(self, device: str) -> None:
        from faster_whisper import WhisperModel

        self._ensure_downloaded(self.cfg.gpu_model if device == "cuda" else self.cfg.cpu_model)
        if device == "cuda":
            name = self.cfg.gpu_model
            model = WhisperModel(name, device="cuda", device_index=int(self.cfg.gpu_device_index),
                                 compute_type=self.cfg.gpu_compute_type)
        else:
            name = self.cfg.cpu_model
            model = WhisperModel(name, device="cpu", compute_type=self.cfg.cpu_compute_type,
                                 cpu_threads=int(self.cfg.cpu_threads))
        self.model, self.device, self.model_name = model, device, name
        self._warmup()

    def _unload(self) -> None:
        self.model = None
        gc.collect()

    def _warmup(self) -> None:
        """Egy próbafelismerés: kiderül, hogy tényleg fut-e, és az első igazi kérés is gyors lesz."""
        noise = np.random.default_rng(0).normal(0.0, 0.02, TARGET_RATE).astype(np.float32)
        segments, _ = self.model.transcribe(noise, language=self.cfg.language or None, beam_size=1,
                                            vad_filter=False, without_timestamps=True)
        for _ in segments:
            pass

    def describe(self) -> str:
        return f"{self.model_name} ({'GPU' if self.device == 'cuda' else 'CPU'})"

    def close(self) -> None:
        """Felszabadítja a modellt (a Szünet gombhoz): a videókártyáról és a memóriából is."""
        model, self.model = self.model, None
        if model is None:
            return
        inner = getattr(model, "model", None)   # a CTranslate2-modell a faster-whisper belsejében
        unload = getattr(inner, "unload_model", None)
        if callable(unload):
            try:
                unload()
            except Exception:  # noqa: BLE001 - a felszabadítás ne álljon le egy régebbi változat miatt
                pass
        del inner, model
        gc.collect()

    def transcribe_file(self, data: bytes) -> str:
        """Hangfájl (pl. a telefonról: m4a, webm, ogg, wav) szöveggé alakítása."""
        import io

        from faster_whisper.audio import decode_audio

        audio = decode_audio(io.BytesIO(data), sampling_rate=TARGET_RATE)
        return self.transcribe(audio)

    def transcribe(self, audio: np.ndarray) -> str:
        segments, _ = self.model.transcribe(
            audio,
            language=self.cfg.language or None,
            beam_size=int(self.cfg.beam_size),
            vad_filter=True,
            initial_prompt=self.cfg.initial_prompt or None,
            condition_on_previous_text=False,
            without_timestamps=True,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        if any(marker in fold(text) for marker in _HALLUCINATIONS):
            return ""
        return text
