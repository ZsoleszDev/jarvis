"""Az ébresztőszó-figyelő tesztjei hamis mikrofonnal és hamis openWakeWord-modellel.

Futtatás:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis_core.config import load_config  # noqa: E402
from jarvis_core.wakeword import (FRAME_SAMPLES, WakeWordError, WakeWordListener,  # noqa: E402
                                  best_score, load_model)


class FakeWakeModel:
    """Hangos keretre "hey jarvis"-t hall, csendre nem."""

    def __init__(self):
        self.frame_sizes: list[int] = []
        self.resets = 0

    def predict(self, frame):
        self.frame_sizes.append(len(frame))
        loud = float(np.abs(frame.astype(np.float32)).mean()) > 3000
        return {"hey_jarvis_v0.1": 0.93 if loud else 0.01}

    def reset(self):
        self.resets += 1


class FakeMic:
    """int16-os mikrofon: a `loud` esemény beállításakor "hey jarvis" szól."""

    def __init__(self, rate_ok: bool = True):
        self.rate_ok = rate_ok
        self.loud = threading.Event()
        self.open_streams = 0
        self.max_open = 0

    def check_input_settings(self, **kwargs):
        if not self.rate_ok:
            raise ValueError("nem támogatott")

    def query_devices(self, device=None, kind=None):
        return {"name": "Teszt mikrofon", "default_samplerate": 48000}

    def InputStream(self, samplerate, channels, dtype, blocksize, device, callback):
        mic = self

        class Stream:
            def __enter__(self):
                mic.open_streams += 1
                mic.max_open = max(mic.max_open, mic.open_streams)
                self.stop = threading.Event()

                def run():
                    while not self.stop.is_set():
                        level = 8000 if mic.loud.is_set() else 50
                        block = (np.random.default_rng().normal(0, level, blocksize)).astype(np.int16)
                        callback(block.reshape(-1, 1), blocksize, None, None)
                        time.sleep(0.005)

                self.thread = threading.Thread(target=run, daemon=True)
                self.thread.start()
                return self

            def __exit__(self, *exc):
                self.stop.set()
                self.thread.join(1)
                mic.open_streams -= 1

        return Stream()


class WakeWordTests(unittest.TestCase):
    def setUp(self):
        cfg = load_config(ROOT / "config.toml")
        self.wake_cfg, self.audio_cfg = cfg.wake_word, cfg.audio
        self.woke = threading.Event()
        self.mic = FakeMic()
        self.model = FakeWakeModel()

    def make(self, mic=None) -> WakeWordListener:
        listener = WakeWordListener(self.wake_cfg, self.audio_cfg, self.woke.set,
                                    model=self.model, backend=mic or self.mic, log=lambda m: None)
        self.addCleanup(listener.stop)
        return listener

    def test_wakes_and_releases_microphone(self):
        listener = self.make()
        listener.start()
        time.sleep(0.2)
        self.assertFalse(self.woke.is_set(), "csendre nem ébred")
        self.assertTrue(listener.listening)
        self.mic.loud.set()
        self.assertTrue(self.woke.wait(3))
        time.sleep(0.1)
        self.assertEqual(self.mic.open_streams, 0, "ébredés után elengedi a mikrofont a felvételnek")
        self.assertFalse(listener.listening)
        self.assertGreaterEqual(self.model.resets, 1)
        self.assertTrue(all(size == FRAME_SAMPLES for size in self.model.frame_sizes))

    def test_pause_and_resume(self):
        listener = self.make()
        listener.start()
        time.sleep(0.2)
        listener.pause()
        self.assertEqual(self.mic.open_streams, 0)
        self.mic.loud.set()
        time.sleep(0.3)
        self.assertFalse(self.woke.is_set(), "szünet alatt nem ébred")
        listener.resume()
        self.assertTrue(self.woke.wait(3))

    def test_cooldown(self):
        self.wake_cfg.cooldown = 30
        listener = self.make()
        listener.start()
        self.mic.loud.set()
        self.assertTrue(self.woke.wait(3))
        self.woke.clear()
        listener.resume()
        time.sleep(0.4)
        self.assertFalse(self.woke.is_set(), "a várakozási időn belül nem ébred újra")

    def test_resamples_other_rates(self):
        listener = self.make(FakeMic(rate_ok=False))
        listener.mic = listener.sd
        listener.start()
        listener.sd.loud.set()
        self.assertTrue(self.woke.wait(3))
        self.assertTrue(all(size == FRAME_SAMPLES for size in self.model.frame_sizes))

    def test_only_one_stream_at_a_time(self):
        listener = self.make()
        listener.start()
        for _ in range(5):
            listener.pause()
            listener.resume()
            time.sleep(0.05)
        self.assertLessEqual(self.mic.max_open, 1)

    def test_missing_package_gives_install_hint(self):
        with mock.patch.dict(sys.modules, {"openwakeword": None, "openwakeword.model": None}):
            with self.assertRaisesRegex(WakeWordError, "requirements-wakeword"):
                load_model(self.wake_cfg)

    def test_best_score(self):
        self.assertAlmostEqual(best_score({"a": 0.2, "b": 0.7}), 0.7)
        self.assertEqual(best_score({}), 0.0)
        self.assertAlmostEqual(best_score(0.4), 0.4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
