"""Wheel short-put exit (DJ-20261001-01): close at 50% of the credit kept or
63% of the entry-to-expiry span elapsed, whichever comes first."""

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.early_exit import exit_reason  # noqa: E402

OPEN, EXPIRY = date(2026, 9, 1), date(2026, 10, 11)  # 40-day span


class ExitReason(unittest.TestCase):
    def test_half_the_credit_kept_closes(self):
        self.assertEqual(exit_reason(2.00, 1.00, OPEN, EXPIRY, date(2026, 9, 5)), "profit")

    def test_less_than_half_kept_holds(self):
        self.assertIsNone(exit_reason(2.00, 1.01, OPEN, EXPIRY, date(2026, 9, 5)))

    def test_63pct_of_span_closes(self):
        # 26 of 40 days = 65%
        self.assertEqual(exit_reason(2.00, 1.80, OPEN, EXPIRY, date(2026, 9, 27)), "time")

    def test_before_63pct_holds(self):
        # 25 of 40 days = 62.5%
        self.assertIsNone(exit_reason(2.00, 1.80, OPEN, EXPIRY, date(2026, 9, 26)))

    def test_unknown_open_date_still_takes_profit_but_never_time_exits(self):
        self.assertIsNone(exit_reason(2.00, 1.80, None, EXPIRY, date(2026, 10, 10)))
        self.assertEqual(exit_reason(2.00, 0.90, None, EXPIRY, date(2026, 10, 10)), "profit")

    def test_missing_mark_never_closes(self):
        self.assertIsNone(exit_reason(2.00, None, OPEN, EXPIRY, date(2026, 10, 10)))


if __name__ == "__main__":
    unittest.main()


from types import SimpleNamespace  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from alpaca.trading.enums import AssetClass  # noqa: E402
from core.early_exit import manage_short_puts  # noqa: E402


class FakeClient:
    def __init__(self, quotes, opened):
        self.quotes, self.opened, self.orders = quotes, opened, []
        self.trade_client = self

    def get_option_snapshot(self, sym):
        bid, ask = self.quotes[sym]
        return {sym: SimpleNamespace(latest_quote=SimpleNamespace(bid_price=bid, ask_price=ask))}

    def get_orders(self, req):
        return [SimpleNamespace(filled_at=self.opened)]

    def submit_order(self, req):
        self.orders.append(req)


def opt(sym, qty, price):
    return SimpleNamespace(symbol=sym, qty=str(qty), avg_entry_price=str(price),
                           asset_class=AssetClass.US_OPTION)


class ManageShortPuts(unittest.TestCase):
    def test_closes_only_the_put_that_hit_its_target(self):
        c = FakeClient(
            {"WMT261016P00095000": (0.40, 0.50), "UBER261016P00070000": (1.50, 1.60)},
            datetime(2026, 9, 14, 15, tzinfo=timezone.utc),
        )
        book = [
            opt("WMT261016P00095000", -1, 1.20),   # mid 0.45 <= 0.60: profit
            opt("UBER261016P00070000", -1, 1.80),  # holds on 2026-09-20
            opt("KO261016C00070000", -1, 0.90),    # covered call: never touched
        ]
        out = manage_short_puts(c, book, today=date(2026, 9, 20))
        self.assertEqual([x["symbol"] for x in out], ["WMT261016P00095000"])
        self.assertEqual(out[0]["reason"], "profit")
        self.assertEqual(float(c.orders[0].limit_price), 0.50)
