"""The spy-spread sleeve shares this account. Its legs are not wheel
positions: update_state raises on a long option, and calculate_risk would
book the short SPY put at full strike (~$65k) against the wheel's $80k."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpaca.trading.enums import AssetClass  # noqa: E402
from core.state_manager import wheel_positions, update_state, calculate_risk  # noqa: E402


def pos(symbol, qty, cls=AssetClass.US_OPTION, price="1.0"):
    return SimpleNamespace(symbol=symbol, qty=str(qty), asset_class=cls, avg_entry_price=price)


class WheelPositions(unittest.TestCase):
    def test_drops_sleeve_legs_keeps_wheel(self):
        book = [
            pos("SPY261113P00640000", -1),
            pos("SPY261113P00635000", 1),
            pos("WMT261016P00095000", -1),
        ]
        kept = wheel_positions(book)
        self.assertEqual([p.symbol for p in kept], ["WMT261016P00095000"])
        self.assertEqual(update_state(kept)["WMT"]["type"], "short_put")
        self.assertEqual(calculate_risk(kept), 9500)


if __name__ == "__main__":
    unittest.main()
