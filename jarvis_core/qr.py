"""QR-kód a párosításhoz (a gép Telefon ablakában): a telefon kamerája beolvassa, és megnyitja a gép címét a
párosító kóddal. Saját, kis kódoló, hogy ne kelljen hozzá külön csomag: bájt mód, 1–6. verzió, M (ha nem fér
el, L) hibajavítás. A párosító címhez (kb. 60 karakter) ez bőven elég; a 6-os verzióba M szinten 106 bájt fér.

A szabvány (ISO/IEC 18004) lépései: adatbitek, Reed–Solomon hibajavítás, a rögzített minták (keresők, időzítés,
igazítás), az adatok cikcakkban, a legjobb maszk (büntetőpontok szerint), végül a formátumbitek.
"""
from __future__ import annotations

RAW_CODEWORDS = {1: 26, 2: 44, 3: 70, 4: 100, 5: 134, 6: 172}       # verzióként az összes kódszó
EC_BLOCKS = {                                                        # (szint, verzió) -> (hibajavító/blokk, blokkok)
    ("M", 1): (10, 1), ("M", 2): (16, 1), ("M", 3): (26, 1), ("M", 4): (18, 2), ("M", 5): (24, 2), ("M", 6): (16, 4),
    ("L", 1): (7, 1), ("L", 2): (10, 1), ("L", 3): (15, 1), ("L", 4): (20, 1), ("L", 5): (26, 1), ("L", 6): (18, 2),
}
FORMAT_LEVEL = {"L": 1, "M": 0}
ALIGNMENT = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}
MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


def _gf_mul(x: int, y: int) -> int:
    """Szorzás a GF(256) testben (x^8 + x^4 + x^3 + x^2 + 1)."""
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _codewords(data: bytes, version: int, level: str) -> list[int] | None:
    """Az adatok és a hibajavító kódszavak, összefésülve; None, ha nem fér el."""
    ec_len, blocks = EC_BLOCKS[(level, version)]
    raw = RAW_CODEWORDS[version]
    capacity = raw - ec_len * blocks
    if len(data) > 255:
        return None
    bits = "0100" + f"{len(data):08b}" + "".join(f"{byte:08b}" for byte in data)
    if len(bits) > capacity * 8:
        return None
    bits += "0" * min(4, capacity * 8 - len(bits))
    bits += "0" * (-len(bits) % 8)
    words = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < capacity:
        words.append(pad)
        pad ^= 0xEC ^ 0x11
    short_blocks = blocks - raw % blocks
    short_len = raw // blocks
    divisor = _rs_divisor(ec_len)
    data_blocks, ec_blocks = [], []
    start = 0
    for i in range(blocks):
        length = short_len - ec_len + (0 if i < short_blocks else 1)
        block = words[start:start + length]
        start += length
        data_blocks.append(block)
        ec_blocks.append(_rs_remainder(block, divisor))
    result = [block[i] for i in range(max(map(len, data_blocks))) for block in data_blocks if i < len(block)]
    return result + [block[i] for i in range(ec_len) for block in ec_blocks]


def _format_bits(level: str, mask: int) -> int:
    data = FORMAT_LEVEL[level] << 3 | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return (data << 10 | rem) ^ 0x5412


def _matrix(words: list[int], version: int, level: str, mask: int) -> list[list[bool]]:
    size = 17 + 4 * version
    dark = [[False] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def put(x: int, y: int, value: bool) -> None:   # x: oszlop, y: sor
        dark[y][x] = value
        fixed[y][x] = True

    for i in range(size):                                       # időzítő minták
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):     # a három kereső, elválasztóval
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                if 0 <= cx + dx < size and 0 <= cy + dy < size:
                    put(cx + dx, cy + dy, max(abs(dx), abs(dy)) not in (2, 4))
    centers = ALIGNMENT[version]
    for i, cx in enumerate(centers):                            # igazító minták (a keresők helyén nem)
        for j, cy in enumerate(centers):
            if (i, j) in ((0, 0), (0, len(centers) - 1), (len(centers) - 1, 0)):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    put(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
    _draw_format(put, size, level, mask)                        # a helyét most foglaljuk le

    index, total = 0, len(words) * 8                            # adatok: kétoszlopos cikcakk, alulról
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5                                           # a függőleges időzítő oszlopot kihagyjuk
        upward = ((right + 1) & 2) == 0
        for vert in range(size):
            y = size - 1 - vert if upward else vert
            for x in (right, right - 1):
                if not fixed[y][x] and index < total:
                    dark[y][x] = (words[index >> 3] >> (7 - (index & 7))) & 1 == 1
                    index += 1
        right -= 2
    rule = MASKS[mask]
    for y in range(size):
        for x in range(size):
            if not fixed[y][x] and rule(x, y):
                dark[y][x] = not dark[y][x]
    return dark


def _draw_format(put, size: int, level: str, mask: int) -> None:
    bits = _format_bits(level, mask)

    def bit(i: int) -> bool:
        return (bits >> i) & 1 == 1

    for i in range(6):
        put(8, i, bit(i))
    put(8, 7, bit(6))
    put(8, 8, bit(7))
    put(7, 8, bit(8))
    for i in range(9, 15):
        put(14 - i, 8, bit(i))
    for i in range(8):
        put(size - 1 - i, 8, bit(i))
    for i in range(8, 15):
        put(8, size - 15 + i, bit(i))
    put(8, size - 8, True)                                      # a mindig sötét modul


def _penalty(dark: list[list[bool]]) -> int:
    """A szabvány büntetőpontjai: hosszú egyszínű sorok, 2×2-es foltok, keresőszerű minták, sötét arány."""
    size = len(dark)
    score = 0
    lines = [row for row in dark] + [[dark[y][x] for y in range(size)] for x in range(size)]
    for line in lines:
        run = 1
        for i in range(1, size + 1):
            if i < size and line[i] == line[i - 1]:
                run += 1
                continue
            if run >= 5:
                score += 3 + run - 5
            run = 1
        text = "".join("1" if v else "0" for v in line)
        for pattern in ("10111010000", "00001011101"):
            at = text.find(pattern)
            while at != -1:
                score += 40
                at = text.find(pattern, at + 1)
    for y in range(size - 1):
        for x in range(size - 1):
            if dark[y][x] == dark[y][x + 1] == dark[y + 1][x] == dark[y + 1][x + 1]:
                score += 3
    total = size * size
    darks = sum(map(sum, dark))
    return score + ((abs(darks * 20 - total * 10) + total - 1) // total - 1) * 10


def encode(text: str, version: int | None = None, level: str | None = None,
           mask: int | None = None) -> list[list[bool]] | None:
    """A QR-kód mátrixa (sorok, True: sötét), vagy None, ha a szöveg túl hosszú. Alapból a legkisebb verzió,
    M szint (ha úgy nem fér el, L), és a legjobb maszk."""
    data = text.encode("utf-8")
    levels = [level] if level else ["M", "L"]
    versions = [version] if version else list(RAW_CODEWORDS)
    for lvl in levels:
        for ver in versions:
            words = _codewords(data, ver, lvl)
            if words is None:
                continue
            if mask is not None:
                return _matrix(words, ver, lvl, mask)
            return min((_matrix(words, ver, lvl, m) for m in range(8)), key=_penalty)
    return None


def svg(text: str, quiet: int = 4) -> str | None:
    """A QR-kód SVG-ben (fehér alapon fekete, a körülötte kötelező üres sávval), vagy None."""
    matrix = encode(text)
    if matrix is None:
        return None
    size = len(matrix) + 2 * quiet
    parts = []
    for y, row in enumerate(matrix):
        x = 0
        while x < len(row):
            if not row[x]:
                x += 1
                continue
            start = x
            while x < len(row) and row[x]:
                x += 1
            parts.append(f"M{start + quiet} {y + quiet}h{x - start}v1h-{x - start}z")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" shape-rendering="crispEdges" '
            f'role="img" aria-label="QR-kód a párosításhoz"><rect width="{size}" height="{size}" fill="#fff"/>'
            f'<path d="{"".join(parts)}" fill="#000"/></svg>')
