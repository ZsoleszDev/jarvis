"""A Jarvis értesítési hangjai (Android res/raw): puha, halk, marimbaszerű hangok, hogy hallásra felismerd, de ne
legyenek bántók. Szinusz alaphang egy halk, gyorsan lecsengő oktáv felhanggal, lágy indulással és lecsengéssel (nincs
kattanás); 16 bit, mono, a csúcs a teljes hangerő felénél sem több.

- jarvis_alert: a megérő hirdetés (három emelkedő hang, C-dúr), utána négy másodperc csend. A riasztás (FLAG_INSISTENT)
  a fájlt ismétli: így ötmásodpercenként egy halk csengés jön, nem egy folyamatos csengetés.
- jarvis_ping: a többi értesítés (jóváhagyás, értesítés, alku): két rövid, halk hang.
- jarvis_tip: az „Érdemes megnézni” tipp: egyetlen nagyon rövid, halk pittyenés.

Újragenerálás (pl. ha más hangot szeretnél): python android/make_sounds.py android/app/src/main/res/raw
Ha a hang megváltozik, a telefonon az értesítési csatornák azonosítóját is növelni kell (NotifyService), mert az
Android egy már létrehozott csatorna hangját nem cseréli le."""
import math
import struct
import sys
import wave
from pathlib import Path

RATE = 22050


def note(freq: float, start: float, length: float, volume: float, out: list[float], decay: float = 4.5,
         attack: float = 0.012, bright: float = 0.22) -> None:
    """Egy puha hang: lágy (koszinuszos) indulás, exponenciális lecsengés, a végén rövid elhalkulás."""
    first = int(start * RATE)
    for i in range(int(length * RATE)):
        t = i / RATE
        rise = 0.5 - 0.5 * math.cos(math.pi * min(1.0, t / attack))
        release = min(1.0, (length - t) / 0.03)
        sample = (math.sin(2 * math.pi * freq * t)
                  + bright * math.sin(2 * math.pi * freq * 2 * t) * math.exp(-t * decay * 3))
        index = first + i
        if index < len(out):
            out[index] += volume * rise * release * math.exp(-t * decay) * sample


def write(path: Path, notes: list[tuple[float, float, float, float]], total: float, peak: float,
          decay: float = 4.5, attack: float = 0.012, bright: float = 0.22) -> None:
    out = [0.0] * int(total * RATE)
    for freq, start, length, volume in notes:
        note(freq, start, length, volume, out, decay, attack, bright)
    top = max(abs(x) for x in out) or 1.0
    frames = bytearray()
    for x in out:
        frames += struct.pack("<h", int(max(-1.0, min(1.0, x / top * peak)) * 32767))
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(bytes(frames))


if __name__ == "__main__":
    raw = Path(sys.argv[1])
    C5, E5, G5, A5, B5 = 523.25, 659.25, 783.99, 880.00, 987.77
    # megérő hirdetés: három emelkedő, halk hang, aztán csend (ismételve ötmásodpercenként egy csengés)
    write(raw / "jarvis_alert.wav", [(C5, 0.00, 0.6, 0.8), (E5, 0.14, 0.6, 0.8), (G5, 0.28, 0.8, 0.9)], 5.0, 0.45)
    # a többi értesítés: két rövid, halk hang (egy kvart fel)
    write(raw / "jarvis_ping.wav", [(E5, 0.00, 0.35, 0.9), (A5, 0.10, 0.45, 1.0)], 0.6, 0.4, decay=6.0)
    # érdemes megnézni: egyetlen nagyon rövid pittyenés
    write(raw / "jarvis_tip.wav", [(B5, 0.00, 0.14, 1.0)], 0.16, 0.35, decay=18.0, attack=0.005, bright=0.12)
