"""A párosító QR-kód (jarvis_core/qr.py): saját kódoló, külső csomag nélkül.

A várt mátrixokat egy ismert, helyes kódolóval (Nayuki qrcodegen) készítettük, rögzített verzióval és maszkkal;
a fejlesztéskor mind a 712 esetet (1–6. verzió, M és L szint, mind a 8 maszk, többféle szöveg) bitre egyezőnek
találtuk vele, és egy QR-olvasó (zxing) mindet visszafejtette.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis_core import qr  # noqa: E402

# (szöveg, verzió, maszk) -> a sorok hexában (a bal szélső modul a legmagasabb helyi értékű bit)
GOLDEN = {
    ("https://gep.tail1234.ts.net/#pair=123456", 3, 2): [
        "1fc2437f", "1042ac41", "1756415d", "175cbd5d", "17574f5d", "105c7841", "1fd5557f", "001d2700", "17c0ba7c",
        "123f4e71", "01d57800", "14b275aa", "18c0990c", "141746d1", "0ac8e19c", "141d0212", "0bc6392c", "1e1d92f5",
        "14f7f0f4", "1091a632", "145a4df7", "001b1f1f", "1fc03b5c", "10546112", "175a2df7", "1752d60f", "1751ecfe",
        "104ba36a", "1fd138b4"],
    ("https://asztali-gep.tail9f3c2a.ts.net/#pair=000917", 4, 5): [
        "1fcb44c7f", "105cc8041", "17558d95d", "175800c5d", "174f2475d", "104415441", "1fd55557f", "001b40000",
        "1052b97ce", "1aa97661c", "0b6034914", "0c8d630ff", "0e7efd663", "068859fcb", "07ee469d2", "1e99223ad",
        "124e4a0b1", "14373e24f", "09d6e3fed", "0411ac31c", "06ccb02ba", "1d319da18", "16e1b1c22", "1737c75ad",
        "1af7e61fb", "0014e3b17", "1fc365b56", "1044e031c", "1749651fb", "174b9a6bf", "17495e337", "1046f043c",
        "1fde487aa"],
}


def rows_hex(matrix: list[list[bool]]) -> list[str]:
    width = (len(matrix) + 3) // 4
    return [f"{int(''.join('1' if v else '0' for v in row), 2):0{width}x}" for row in matrix]


def read_format(matrix: list[list[bool]]) -> tuple[int, int]:
    """A bal felső formátumbitek visszafejtve: (szint bitjei, maszk)."""
    def at(x: int, y: int) -> int:
        return 1 if matrix[y][x] else 0

    positions = [(8, i) for i in range(6)] + [(8, 7), (8, 8), (7, 8)] + [(14 - i, 8) for i in range(9, 15)]
    bits = sum(at(x, y) << i for i, (x, y) in enumerate(positions)) ^ 0x5412
    return bits >> 13, (bits >> 10) & 7


class QrTests(unittest.TestCase):
    def test_matches_reference_encoder(self):
        for (text, version, mask), expected in GOLDEN.items():
            with self.subTest(text=text):
                self.assertEqual(rows_hex(qr.encode(text, version=version, level="M", mask=mask)), expected)

    def test_pairing_link_structure(self):
        text = "https://gep.tail1234.ts.net/#pair=123456"
        matrix = qr.encode(text)
        size = len(matrix)
        self.assertEqual(size, 29, "40 bájt M szinten a 3-as verzióba fér")
        finder = ["1111111", "1000001", "1011101", "1011101", "1011101", "1000001", "1111111"]
        for x0, y0 in ((0, 0), (size - 7, 0), (0, size - 7)):
            got = ["".join("1" if matrix[y0 + y][x0 + x] else "0" for x in range(7)) for y in range(7)]
            self.assertEqual(got, finder, f"kereső a ({x0}; {y0}) sarokban")
        self.assertEqual([matrix[6][x] for x in range(8, size - 8)], [x % 2 == 0 for x in range(8, size - 8)])
        self.assertTrue(matrix[size - 8][8], "a mindig sötét modul")
        level, mask = read_format(matrix)
        self.assertEqual(level, 0, "M szint")
        self.assertEqual(matrix, qr.encode(text, version=3, level="M", mask=mask), "a választott maszkkal")

    def test_capacity(self):
        self.assertEqual(len(qr.encode("x" * 106)), 41, "106 bájt még M szinten, a 6-os verzióban")
        self.assertEqual(read_format(qr.encode("x" * 107))[0], 1, "107 bájt már csak L szinten")
        self.assertIsNone(qr.encode("x" * 135), "ennyi már nem fér el")

    def test_svg(self):
        image = qr.svg("https://gep.tail1234.ts.net/#pair=123456")
        self.assertIn('viewBox="0 0 37 37"', image, "29 modul és körben 4 üres")
        self.assertRegex(image, r'<path d="(M\d+ \d+h\d+v1h-\d+z)+" fill="#000"/>')
        self.assertFalse(re.search(r"[<>]", image.split("<path d=")[1].split('"')[1]))
        self.assertIsNone(qr.svg("x" * 200))


if __name__ == "__main__":
    unittest.main(verbosity=2)
