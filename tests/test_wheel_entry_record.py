"""Wheel-sleeve entry recording (2026-09-01).

Five attended sessions, zero entries, and no recorded reason: the spread
sleeve has said WHY it stood aside since day one (spread.entry in
daily_state.json), while the wheel logged its rejections only to a dev-only
strategy log that never left the container. These tests pin the fix:
filter_options counts each rejected contract's first failing gate, and
run_daily folds the run's wheel decision into daily_state + digest.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from core.strategy import filter_options
from models.contract import Contract
import run_daily


def _put(symbol="KMI261016P00030000", delta=-0.25, bid=0.55, ask=0.60,
         strike=30.0, dte=38, oi=1200):
    return Contract(
        underlying="KMI", symbol=symbol, contract_type="put",
        dte=dte, strike=strike, delta=delta,
        bid_price=bid, ask_price=ask, oi=oi,
    )


class FilterRejectionCounts(unittest.TestCase):
    def test_a_passing_contract_is_kept_and_counts_nothing(self):
        rejections = {}
        kept = filter_options([_put()], rejections=rejections)
        self.assertEqual(len(kept), 1)
        self.assertEqual(rejections, {})

    def test_each_rejected_contract_counts_its_first_failing_gate(self):
        rejections = {}
        kept = filter_options(
            [
                _put(delta=None),                      # no_delta
                _put(delta=-0.45),                     # delta_band
                _put(bid=None),                        # no_quote (would crash before)
                _put(bid=0.005),                       # yield_band
                _put(oi=12),                           # low_oi
                _put(bid=0.40, ask=0.70),              # wide_spread
            ],
            rejections=rejections,
        )
        self.assertEqual(kept, [])
        self.assertEqual(
            rejections,
            {"no_delta": 1, "delta_band": 1, "no_quote": 1,
             "yield_band": 1, "low_oi": 1, "wide_spread": 1},
        )

    def test_min_strike_rejection_is_counted(self):
        rejections = {}
        kept = filter_options([_put(strike=30.0)], min_strike=35.0,
                              rejections=rejections)
        self.assertEqual(kept, [])
        self.assertEqual(rejections, {"below_min_strike": 1})

    def test_without_a_collector_behavior_is_unchanged(self):
        kept = filter_options([_put(), _put(delta=-0.45)])
        self.assertEqual(len(kept), 1)


class WheelBlock(unittest.TestCase):
    def _write_log(self, tmp, entry):
        p = Path(tmp) / "strategy_log.json"
        p.write_text(json.dumps([entry]))
        return p

    def test_a_missing_log_is_recorded_as_unrecorded_never_as_no_candidates(self):
        block = run_daily.build_wheel_block(Path("/nonexistent/strategy_log.json"),
                                            today="2026-09-01")
        self.assertFalse(block["recorded"])
        self.assertIn("unreadable", block["reason"])

    def test_a_stale_log_entry_from_another_day_is_named_stale(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_log(tmp, {"datetime": "2026-08-28T10:46:00-04:00"})
            block = run_daily.build_wheel_block(p, today="2026-09-01")
        self.assertFalse(block["recorded"])
        self.assertIn("stale", block["reason"])

    def test_zero_candidates_names_the_rejection_counts(self):
        import tempfile
        entry = {
            "datetime": "2026-09-01T10:46:00-04:00",
            "allowed_symbols": ["KMI", "KVUE"],
            "buying_power": 80000.0,
            "put_scan": {"scanned": 412, "rejections": {"delta_band": 300, "low_oi": 90, "wide_spread": 22}},
            "put_options": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_log(tmp, entry)
            block = run_daily.build_wheel_block(p, today="2026-09-01")
        self.assertTrue(block["recorded"])
        self.assertFalse(block["entered"])
        self.assertIn("0 of 412", block["reason"])
        self.assertIn("300 delta_band", block["reason"])

    def test_candidates_all_over_the_cap_name_the_cap(self):
        import tempfile
        entry = {
            "datetime": "2026-09-01T10:46:00-04:00",
            "allowed_symbols": ["NVDA"],
            "buying_power": 80000.0,
            "put_scan": {"scanned": 40, "rejections": {}},
            "put_options": [{"symbol": "NVDA261016P00205000"}],
            "cap_skips": [{"symbol": "NVDA261016P00205000", "collateral": 20500}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_log(tmp, entry)
            block = run_daily.build_wheel_block(p, today="2026-09-01")
        self.assertTrue(block["recorded"])
        self.assertFalse(block["entered"])
        self.assertIn("per-name cap", block["reason"])

    def test_sold_puts_arrive_nested_one_list_per_log_call(self):
        """log_sold_puts appends [p.to_dict()] per call — the live 2026-09-01
        run crashed on exactly this shape (AttributeError: 'list' object has
        no attribute 'get') AFTER selling the book's first CSPs."""
        import tempfile
        entry = {
            "datetime": "2026-09-01T10:46:00-04:00",
            "allowed_symbols": ["XOM", "UBER"],
            "buying_power": 80000.0,
            "put_scan": {"scanned": 200, "rejections": {}},
            "put_options": [{"symbol": "XOM261016P00155000"}],
            "sold_puts": [[{"symbol": "XOM261016P00155000"}],
                          [{"symbol": "UBER261016P00070000"}]],
        }
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_log(tmp, entry)
            block = run_daily.build_wheel_block(p, today="2026-09-01")
        self.assertTrue(block["entered"])
        self.assertIn("XOM261016P00155000", block["reason"])
        self.assertIn("UBER261016P00070000", block["reason"])
        self.assertEqual(len(block["sold"]), 2)

    def test_a_fill_is_recorded_as_entered(self):
        import tempfile
        entry = {
            "datetime": "2026-09-01T10:46:00-04:00",
            "allowed_symbols": ["KMI"],
            "buying_power": 80000.0,
            "put_scan": {"scanned": 40, "rejections": {}},
            "put_options": [{"symbol": "KMI261016P00030000"}],
            "sold_puts": [{"symbol": "KMI261016P00030000"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_log(tmp, entry)
            block = run_daily.build_wheel_block(p, today="2026-09-01")
        self.assertTrue(block["entered"])
        self.assertIn("KMI261016P00030000", block["reason"])


if __name__ == "__main__":
    unittest.main()
