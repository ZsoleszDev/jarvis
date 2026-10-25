"""A Jarvis értesítési hangjai (Android res/raw): egy jellegzetes, emelkedő harangjáték a megérő hirdetésnek, és egy
rövid kéthangú jelzés a többi értesítésnek. Szinusz alaphang + felhangok, harangszerű lecsengéssel; 16 bit, mono.

Újragenerálás (pl. ha más hangot szeretnél): python android/make_sounds.py android/app/src/main/res/raw
Ha a hang megváltozik, a telefonon az értesítési csatornák azonosítóját is növelni kell (NotifyService), mert az
Android egy már létrehozott csatorna hangját nem cseréli le."""
import math
import struct
import sys
import wave
from pathlib import Path

RATE = 22050


def bell(freq: float, start: float, length: float, volume: float, out: list[float]) -> None:
    first = int(start * RATE)
    for i in range(int(length * RATE)):
        t = i / RATE
        attack = min(1.0, t / 0.008)                       # puha indulás (nincs kattanás)
        decay = math.exp(-t * 3.2)
        sample = (math.sin(2 * math.pi * freq * t) + 0.45 * math.sin(2 * math.pi * freq * 2 * t) * math.exp(-t * 6)
                  + 0.18 * math.sin(2 * math.pi * freq * 3.01 * t) * math.exp(-t * 9))
        index = first + i
        if index < len(out):
            out[index] += volume * attack * decay * sample


def write(path: Path, notes: list[tuple[float, float, float, float]], total: float) -> None:
    out = [0.0] * int(total * RATE)
    for freq, start, length, volume in notes:
        bell(freq, start, length, volume, out)
    peak = max(abs(x) for x in out) or 1.0
    fade = int(0.05 * RATE)
    frames = bytearray()
    for i, x in enumerate(out):
        x = x / peak * 0.85
        if i > len(out) - fade:
            x *= (len(out) - i) / fade
        frames += struct.pack("<h", int(max(-1.0, min(1.0, x)) * 32767))
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(bytes(frames))


raw = Path(sys.argv[1])
E5, GS5, B5, E6 = 659.25, 830.61, 987.77, 1318.51
# megérő hirdetés: négy emelkedő hang (E-dúr akkord felbontva), a végén egy hosszabb, magas harang
write(raw / "jarvis_alert.wav", [(E5, 0.00, 0.9, 0.8), (GS5, 0.14, 0.9, 0.8), (B5, 0.28, 0.9, 0.8),
                                 (E6, 0.42, 1.3, 1.0)], 1.8)
# a többi értesítés: két rövid hang (egy kvart fel), ugyanabból a hangkészletből, hogy felismerhető legyen
write(raw / "jarvis_ping.wav", [(B5, 0.00, 0.6, 0.9), (E6, 0.12, 0.7, 1.0)], 0.85)
