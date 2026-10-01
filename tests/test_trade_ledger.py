"""DJ-20261001-03: closed-trade ledger, stats, and the share hold-time cap."""

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.trade_ledger import (diff_closes, over_hold_cap, stats, stats_line,
                               update_open_since)

PUT = {"asset_class": "us_option", "symbol": "UBER261016P00070000", "qty": "-1", "avg_entry": "1.38"}
CALL = {"asset_class": "us_option", "symbol": "UBER261120C00072000", "qty": "-1", "avg_entry": "0.90"}
SHARES = {"asset_class": "us_equity", "symbol": "UBER", "qty": "100", "avg_entry": "70"}
SPY_LEG = {"asset_class": "us_option", "symbol": "SPY261120P00600000", "qty": "-1", "avg_entry": "2.0"}


def fills(table):
    return lambda sym, side: table.get((sym, side))


class DiffCloses(unittest.TestCase):
    def test_put_bought_back_uses_closing_fill(self):
        [t] = diff_closes([PUT], [], fills({(PUT["symbol"], "buy"): 0.60}), "2026-10-05")
        self.assertEqual((t["how"], t["pnl"]), ("bought back", 78.0))

    def test_put_gone_without_fill_keeps_credit_and_shares_carry_loss(self):
        [t] = diff_closes([PUT], [SHARES], fills({}), "2026-10-17")
        self.assertEqual((t["how"], t["pnl"]), ("expired or assigned", 138.0))

    def test_shares_called_away_at_call_strike(self):
        trades = diff_closes([SHARES, CALL], [], fills({}), "2026-11-21")
        by = {t["symbol"]: t for t in trades}
        self.assertEqual(by["UBER"]["how"], "called away")
        self.assertEqual(by["UBER"]["pnl"], 200.0)
        self.assertEqual(by[CALL["symbol"]]["pnl"], 90.0)

    def test_shares_sold_at_a_loss(self):
        [t] = diff_closes([SHARES], [], fills({("UBER", "sell"): 61.5}), "2027-01-15")
        self.assertEqual(t["pnl"], -850.0)

    def test_shares_with_no_fill_and_no_call_are_unresolved(self):
        [t] = diff_closes([SHARES], [], fills({}), "2027-01-15")
        self.assertEqual((t["how"], t["pnl"]), ("unresolved", None))

    def test_spread_sleeve_legs_ignored(self):
        self.assertEqual(diff_closes([SPY_LEG], [], fills({}), "2026-10-05"), [])

    def test_still_held_is_not_closed(self):
        self.assertEqual(diff_closes([PUT], [PUT], fills({}), "2026-10-05"), [])


class Stats(unittest.TestCase):
    def test_high_win_rate_with_one_big_loss(self):
        trades = [{"pnl": 100}] * 9 + [{"pnl": -850}, {"pnl": None}]
        s = stats(trades)
        self.assertEqual((s["closed"], s["unresolved"], s["win_rate"]), (10, 1, 0.9))
        self.assertEqual((s["avg_win"], s["avg_loss"], s["profit_factor"]), (100, -850, 1.06))
        line = stats_line(s)
        self.assertIn("won 90%", line)
        self.assertIn("profit factor 1.06", line)
        self.assertIn("1 unresolved", line)

    def test_no_losses_has_no_profit_factor(self):
        self.assertIsNone(stats([{"pnl": 50}])["profit_factor"])
        self.assertIn("no losses yet", stats_line(stats([{"pnl": 50}])))

    def test_empty_and_error_lines(self):
        self.assertEqual(stats_line(stats([])), "wheel closed trades: none yet")
        self.assertIn("FAILED", stats_line({"closed": 0, "unresolved": 0, "error": "x"}))


class HoldCap(unittest.TestCase):
    def test_open_since_keeps_old_dates_and_drops_closed(self):
        m = update_open_since({"UBER": "2026-07-01", "WMT": "2026-08-01"},
                              [SHARES, PUT, SPY_LEG], "2026-10-01")
        self.assertEqual(m, {"UBER": "2026-07-01", PUT["symbol"]: "2026-10-01"})

    def test_cap_boundary(self):
        self.assertFalse(over_hold_cap("2026-07-04", date(2026, 10, 1)))  # 89 days
        self.assertTrue(over_hold_cap("2026-07-03", date(2026, 10, 1)))   # 90 days

    def test_unknown_start_never_triggers(self):
        self.assertFalse(over_hold_cap(None, date(2026, 10, 1)))


if __name__ == "__main__":
    unittest.main()
