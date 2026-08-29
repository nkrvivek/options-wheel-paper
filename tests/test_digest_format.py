"""Digest formatter fixes (2026-08-29), pinned against the 2026-08-28 digest
"[paper-wheel] 2026-08-29 GREEN — equity $100,000" sent 5:26 PM PT:

1. Market-session date: that digest stamped TOMORROW's date because
   datetime.now(timezone.utc).date() had already rolled over. The stamp is
   now the America/New_York session date.
2. Rejection dump: the spread entry line inlined ~30 per-candidate rejection
   strings into the email. It is now counts-by-reason + best near-miss, with
   a top-5 nearest-miss list max; full detail stays in
   state/daily_state.json.
"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import run_daily
from core import spread_sleeve as ss


def _below(symbol, credit):
    return {"symbol": symbol, "reason": "below_credit_floor", "credit": credit}


def _no_long(symbol):
    return {"symbol": symbol, "reason": "no_long_leg"}


class MarketSessionDate(unittest.TestCase):
    def test_utc_evening_rollover_stays_on_us_session_date(self):
        # The bug reproduction: 2026-08-29 00:26Z == 2026-08-28 5:26 PM PT /
        # 8:26 PM ET — the session date is still Aug 28.
        now = datetime(2026, 8, 29, 0, 26, tzinfo=timezone.utc)
        self.assertEqual(run_daily.market_session_date(now), "2026-08-28")

    def test_midday_utc_matches_utc_date(self):
        now = datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(run_daily.market_session_date(now), "2026-08-28")

    def test_winter_est_rollover(self):
        # EST (UTC-5): 02:00Z Dec 1 == 9:00 PM ET Nov 30.
        now = datetime(2026, 12, 1, 2, 0, tzinfo=timezone.utc)
        self.assertEqual(run_daily.market_session_date(now), "2026-11-30")

    def test_naive_datetime_treated_as_utc(self):
        now = datetime(2026, 8, 29, 0, 26)
        self.assertEqual(run_daily.market_session_date(now), "2026-08-28")


class RejectionSummary(unittest.TestCase):
    def test_reference_shape_counts_and_best_near_miss(self):
        rejections = (
            [_below(f"SPY26100{i % 10}P00{500 + i:03d}000", 0.20 + i * 0.01)
             for i in range(26)]
            + [_below("SPYBEST", 0.50)]
            + [_no_long(f"SPYNL{i}") for i in range(4)]
        )
        self.assertEqual(
            ss.rejection_summary(rejections),
            "31 candidates: 27 below $0.55 credit floor (best 0.50 at SPYBEST), "
            "4 no quoted long leg",
        )

    def test_only_no_long_leg(self):
        self.assertEqual(
            ss.rejection_summary([_no_long("A"), _no_long("B")]),
            "2 candidates: 2 no quoted long leg",
        )

    def test_empty(self):
        self.assertEqual(ss.rejection_summary([]), "no rejections")


class NearestMisses(unittest.TestCase):
    def test_caps_at_five_best_credit_first(self):
        rejections = [_below(f"S{i}", 0.10 + i * 0.05) for i in range(8)]
        rejections.append(_no_long("NL"))  # never a near-miss
        top = ss.nearest_misses(rejections)
        self.assertEqual(len(top), 5)
        credits = [r["credit"] for r in top]
        self.assertEqual(credits, sorted(credits, reverse=True))
        self.assertEqual(top[0]["symbol"], "S7")

    def test_no_long_leg_only_yields_nothing(self):
        self.assertEqual(ss.nearest_misses([_no_long("A")]), [])


class DigestLinesNearMiss(unittest.TestCase):
    def _spread(self, rejections):
        return {
            "killed": False, "kill_reason": None, "realized_pnl": 0.0,
            "unrealized_pnl": 0.0, "open_positions": [], "breaches": [],
            "notes": [],
            "entry": {"allowed": False,
                      "reason": "no spread selected: "
                                + ss.rejection_summary(rejections),
                      "rejections": rejections},
        }

    def test_at_most_five_near_miss_lines_and_no_inline_dump(self):
        rejections = [_below(f"S{i}", 0.10 + i * 0.01) for i in range(30)]
        lines = ss.digest_lines(self._spread(rejections))
        near = [ln for ln in lines if "near-miss" in ln]
        self.assertEqual(len(near), 5)
        # The old failure mode: every rejection inlined via "; ".join —
        # the digest must stay compact no matter how many candidates missed.
        self.assertLess(len(lines), 12)
        entry_line = next(ln for ln in lines if "entry:" in ln)
        self.assertIn("30 candidates", entry_line)
        self.assertNotIn(";", entry_line)

    def test_no_rejections_renders_no_near_miss_lines(self):
        lines = ss.digest_lines(self._spread([]))
        self.assertEqual([ln for ln in lines if "near-miss" in ln], [])


if __name__ == "__main__":
    unittest.main()
