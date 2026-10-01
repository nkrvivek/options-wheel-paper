"""Wheel sector cap (2026-10-01): at most SECTOR_CAP short legs or share
lots per sector, counting what the book already holds. The wider universe
puts four banks on the list; without the cap one bad week for banks could
assign all four."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import params  # noqa: E402
from core.strategy import apply_sector_cap  # noqa: E402


def opt(sym):
    return SimpleNamespace(underlying=sym)


class SectorCap(unittest.TestCase):
    def test_every_listed_symbol_has_a_sector(self):
        symbols = [s.strip() for s in (ROOT / "config" / "symbol_list.txt").read_text().splitlines() if s.strip()]
        missing = [s for s in symbols if s not in params.SECTORS]
        self.assertEqual(missing, [])

    def test_keeps_order_and_drops_third_in_sector(self):
        picks = [opt("BAC"), opt("KO"), opt("C"), opt("WFC")]
        kept = apply_sector_cap(picks, held=[], cap=2)
        self.assertEqual([o.underlying for o in kept], ["BAC", "KO", "C"])

    def test_held_names_count_toward_the_cap(self):
        kept = apply_sector_cap([opt("BAC"), opt("XOM")], held=["SCHW", "C"], cap=2)
        self.assertEqual([o.underlying for o in kept], ["XOM"])

    def test_unknown_sector_is_refused_not_waved_in(self):
        kept = apply_sector_cap([opt("ZZZZ")], held=[], cap=2)
        self.assertEqual(kept, [])


if __name__ == "__main__":
    unittest.main()
