"""spy-spread sleeve rules (prereg DJ-20260827-01, trade-refresh
strategy-prereg.json `spy-spread`).

What is under test is the set of refusals. Every gate here fails closed:
a missing regime tier blocks entry, a missing VIX blocks entry, an
unpriced open spread is flagged rather than silently held, and a short
put that cannot be paired with its long still demands full CSP
collateral. The preregistered numbers live in config/spread_params.py
and nowhere else — a test that hard-codes them alongside is the point:
moving a number breaks a test.
"""

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from config import spread_params as sp  # noqa: E402
from core import spread_sleeve as ss  # noqa: E402
import run_daily  # noqa: E402

TODAY = date(2026, 9, 1)


def fresh_book(**over):
    book = {
        "sleeve": "spy-spread",
        "start_notional": sp.SLEEVE_NOTIONAL,
        "realized_pnl": 0.0,
        "breaches": [],
        "killed": False,
        "kill_reason": None,
        "last_entry_date": None,
        "open_positions": [],
        "closed_positions": [],
    }
    book.update(over)
    return book


def regime(tier="clear", vix=17.0):
    return {"tier": tier, "vix": vix}


class TestEntryAllowed(unittest.TestCase):
    def test_clear_regime_in_band_allows(self):
        ok, _ = ss.entry_allowed(regime(), fresh_book(), TODAY)
        self.assertTrue(ok)

    def test_caution_tier_allows(self):
        ok, _ = ss.entry_allowed(regime(tier="caution"), fresh_book(), TODAY)
        self.assertTrue(ok)

    def test_missing_regime_blocks(self):
        ok, reason = ss.entry_allowed(None, fresh_book(), TODAY)
        self.assertFalse(ok)
        self.assertIn("regime", reason)

    def test_null_tier_blocks(self):
        ok, _ = ss.entry_allowed(regime(tier=None), fresh_book(), TODAY)
        self.assertFalse(ok)

    def test_defensive_and_halt_block(self):
        for tier in ("defensive", "halt"):
            ok, _ = ss.entry_allowed(regime(tier=tier), fresh_book(), TODAY)
            self.assertFalse(ok, tier)

    def test_missing_vix_blocks_never_read_as_zero(self):
        ok, reason = ss.entry_allowed(regime(vix=None), fresh_book(), TODAY)
        self.assertFalse(ok)
        self.assertIn("vix", reason.lower())

    def test_vix_outside_band_blocks(self):
        for vix in (sp.VIX_MIN - 0.5, sp.VIX_MAX + 0.5):
            ok, _ = ss.entry_allowed(regime(vix=vix), fresh_book(), TODAY)
            self.assertFalse(ok, vix)

    def test_vix_at_band_edges_allows(self):
        for vix in (sp.VIX_MIN, sp.VIX_MAX):
            ok, _ = ss.entry_allowed(regime(vix=vix), fresh_book(), TODAY)
            self.assertTrue(ok, vix)

    def test_entry_cooldown_one_per_week(self):
        book = fresh_book(last_entry_date="2026-08-28")  # 4 days ago
        ok, reason = ss.entry_allowed(regime(), book, TODAY)
        self.assertFalse(ok)
        self.assertIn("cooldown", reason)
        book = fresh_book(last_entry_date="2026-08-24")  # 8 days ago
        ok, _ = ss.entry_allowed(regime(), book, TODAY)
        self.assertTrue(ok)

    def test_max_concurrent_expiries(self):
        opens = [
            {"expiry": e, "qty": 1}
            for e in ("2026-10-02", "2026-10-09", "2026-10-16")
        ]
        book = fresh_book(open_positions=opens)
        ok, reason = ss.entry_allowed(regime(), book, TODAY)
        self.assertFalse(ok)
        self.assertIn("expiries", reason)

    def test_killed_sleeve_never_enters(self):
        book = fresh_book(killed=True, kill_reason="drawdown")
        ok, reason = ss.entry_allowed(regime(), book, TODAY)
        self.assertFalse(ok)
        self.assertIn("killed", reason)


def cand(strike, delta, bid, ask, expiry="2026-10-09"):
    return {
        "symbol": f"SPY261009P{int(strike * 1000):08d}",
        "strike": strike,
        "expiry": expiry,
        "delta": delta,
        "bid": bid,
        "ask": ask,
    }


class TestSelectSpread(unittest.TestCase):
    def test_picks_delta_closest_to_target_with_paired_long(self):
        cands = [
            cand(555, -0.13, 1.10, 1.20),
            cand(560, -0.145, 1.40, 1.50),  # closest to 0.14
            cand(565, -0.17, 1.80, 1.90),   # outside band
            cand(550, -0.10, 0.80, 0.90),   # outside band
            cand(555 - sp.WIDTH, -0.10, 0.70, 0.80),
            cand(560 - sp.WIDTH, -0.12, 0.80, 0.90),
        ]
        pick, reason, _rejections = ss.select_spread(cands, TODAY)
        self.assertIsNotNone(pick, reason)
        self.assertEqual(pick["short"]["strike"], 560)
        self.assertEqual(pick["long"]["strike"], 560 - sp.WIDTH)
        # credit = mid(1.45) - mid(0.85) = 0.60
        self.assertAlmostEqual(pick["credit"], 0.60, places=6)

    def test_credit_below_floor_refused(self):
        cands = [
            cand(560, -0.14, 1.00, 1.10),
            cand(560 - sp.WIDTH, -0.11, 0.70, 0.80),  # credit 0.30 < 0.55
        ]
        pick, reason, rejections = ss.select_spread(cands, TODAY)
        self.assertIsNone(pick)
        self.assertIn("credit", reason)
        # Full structured detail rides alongside the compact summary.
        self.assertEqual(rejections[0]["reason"], "below_credit_floor")
        self.assertAlmostEqual(rejections[0]["credit"], 0.30, places=6)

    def test_missing_delta_never_selected(self):
        cands = [
            cand(560, None, 1.40, 1.50),
            cand(560 - sp.WIDTH, -0.11, 0.80, 0.90),
        ]
        pick, reason, _rejections = ss.select_spread(cands, TODAY)
        self.assertIsNone(pick)

    def test_no_long_leg_available_refused(self):
        cands = [cand(560, -0.14, 1.40, 1.50)]
        pick, reason, rejections = ss.select_spread(cands, TODAY)
        self.assertIsNone(pick)
        self.assertEqual(rejections[0]["reason"], "no_long_leg")

    def test_missing_quote_side_never_selected(self):
        cands = [
            cand(560, -0.14, None, 1.50),
            cand(560 - sp.WIDTH, -0.11, 0.80, 0.90),
        ]
        pick, _, _ = ss.select_spread(cands, TODAY)
        self.assertIsNone(pick)


class TestPositionSize(unittest.TestCase):
    def test_size_from_max_risk(self):
        # risk/contract = (5 - 0.60) * 100 = 440; cap = 5% of 25000 = 1250 -> 2
        self.assertEqual(ss.position_size(0.60), 2)

    def test_never_rounds_up_past_the_cap(self):
        # risk/contract = (5 - 0.55) * 100 = 445; floor(1250/445) = 2, not 3
        self.assertEqual(ss.position_size(sp.MIN_CREDIT), 2)

    def test_nonsense_credit_refused(self):
        self.assertEqual(ss.position_size(0.0), 0)
        self.assertEqual(ss.position_size(-1.0), 0)
        self.assertEqual(ss.position_size(None), 0)
        self.assertEqual(ss.position_size(sp.WIDTH), 0)


def open_pos(credit=0.60, qty=2, expiry="2026-10-09", short="SPY261009P00560000", long_="SPY261009P00555000"):
    return {
        "id": "sp-1",
        "opened": "2026-09-01",
        "expiry": expiry,
        "short_symbol": short,
        "long_symbol": long_,
        "short_strike": 560.0,
        "long_strike": 555.0,
        "qty": qty,
        "credit": credit,
    }


class TestExitDecisions(unittest.TestCase):
    def quotes(self, short_bid, short_ask, long_bid, long_ask):
        return {
            "SPY261009P00560000": {"bid": short_bid, "ask": short_ask},
            "SPY261009P00555000": {"bid": long_bid, "ask": long_ask},
        }

    def test_take_profit_at_half_credit(self):
        # cost to close = mid(0.25) - mid(0.05) = 0.20 <= 0.30 -> TP
        q = self.quotes(0.20, 0.30, 0.00, 0.10)
        acts = ss.exit_decisions([open_pos()], q, TODAY)
        self.assertEqual(acts[0]["action"], "close")
        self.assertEqual(acts[0]["why"], "take-profit")

    def test_stop_at_two_times_credit(self):
        # cost to close = mid(1.30) - mid(0.05) = 1.25 >= 1.20 -> stop
        q = self.quotes(1.25, 1.35, 0.00, 0.10)
        acts = ss.exit_decisions([open_pos()], q, TODAY)
        self.assertEqual(acts[0]["action"], "close")
        self.assertEqual(acts[0]["why"], "stop")

    def test_hold_between_thresholds(self):
        # cost 0.55: neither TP (<=0.30) nor stop (>=1.20)
        q = self.quotes(0.55, 0.65, 0.00, 0.10)
        acts = ss.exit_decisions([open_pos()], q, TODAY)
        self.assertEqual(acts[0]["action"], "hold")

    def test_hard_close_inside_dte_window(self):
        q = self.quotes(0.55, 0.65, 0.00, 0.10)
        acts = ss.exit_decisions([open_pos()], q, date(2026, 10, 5))  # 4 DTE
        self.assertEqual(acts[0]["action"], "close")
        self.assertEqual(acts[0]["why"], "dte")

    def test_unpriced_is_flagged_never_silently_held(self):
        acts = ss.exit_decisions([open_pos()], {}, TODAY)
        self.assertEqual(acts[0]["action"], "unpriced")


class TestKillCheck(unittest.TestCase):
    def test_drawdown_kills(self):
        book = fresh_book(realized_pnl=-2000.0)
        killed, reason = ss.kill_check(book, unrealized_pnl=-600.0)  # -2600 > 10% of 25000
        self.assertTrue(killed)
        self.assertIn("drawdown", reason)

    def test_three_breaches_kill(self):
        book = fresh_book(breaches=["a", "b", "c"])
        killed, reason = ss.kill_check(book, unrealized_pnl=0.0)
        self.assertTrue(killed)
        self.assertIn("breach", reason)

    def test_healthy_book_lives(self):
        killed, _ = ss.kill_check(fresh_book(realized_pnl=-100.0), unrealized_pnl=-100.0)
        self.assertFalse(killed)

    def test_unknown_unrealized_never_read_as_zero(self):
        # A book through the threshold on realized alone must not be rescued
        # by marks that could not be read.
        book = fresh_book(realized_pnl=-2600.0)
        killed, reason = ss.kill_check(book, unrealized_pnl=None)
        self.assertTrue(killed)
        self.assertIn("unread", reason)

    def test_unknown_unrealized_with_healthy_realized_flags_not_kills(self):
        killed, reason = ss.kill_check(fresh_book(), unrealized_pnl=None)
        self.assertFalse(killed)
        self.assertIn("unread", reason)


class TestSpreadAwareBreaches(unittest.TestCase):
    """run_daily.check_breaches must recognize a defined-risk pair.

    A short put with a paired long put (same underlying + expiry, lower
    strike) needs width-based risk cash, not full CSP collateral. Anything
    it cannot pair still demands the full strike — conservative by default.
    """

    def acct(self, cash):
        return {"equity": 100_000.0, "cash": cash}

    def test_paired_short_put_needs_width_not_strike(self):
        positions = [
            {"asset_class": "us_option", "symbol": "SPY261009P00560000", "qty": "-2"},
            {"asset_class": "us_option", "symbol": "SPY261009P00555000", "qty": "2"},
        ]
        # width risk = (560-555)*100*2 = 1000. Full CSP would be 112,000.
        self.assertEqual(run_daily.check_breaches(self.acct(cash=2_000.0), positions), [])

    def test_unpaired_short_put_still_full_collateral(self):
        positions = [
            {"asset_class": "us_option", "symbol": "SPY261009P00560000", "qty": "-2"},
        ]
        breaches = run_daily.check_breaches(self.acct(cash=2_000.0), positions)
        self.assertEqual(len(breaches), 1)
        self.assertIn("collateral", breaches[0])

    def test_long_put_different_expiry_does_not_pair(self):
        positions = [
            {"asset_class": "us_option", "symbol": "SPY261009P00560000", "qty": "-2"},
            {"asset_class": "us_option", "symbol": "SPY261016P00555000", "qty": "2"},
        ]
        breaches = run_daily.check_breaches(self.acct(cash=2_000.0), positions)
        self.assertEqual(len(breaches), 1)

    def test_partial_pairing_splits(self):
        # 2 short, 1 long: one paired (width 500), one naked (full 56,000)
        positions = [
            {"asset_class": "us_option", "symbol": "SPY261009P00560000", "qty": "-2"},
            {"asset_class": "us_option", "symbol": "SPY261009P00555000", "qty": "1"},
        ]
        self.assertEqual(run_daily.check_breaches(self.acct(cash=56_500.0), positions), [])
        breaches = run_daily.check_breaches(self.acct(cash=56_000.0), positions)
        self.assertEqual(len(breaches), 1)

    def test_csp_only_book_unchanged(self):
        positions = [
            {"asset_class": "us_option", "symbol": "KMI260918P00027000", "qty": "-1"},
        ]
        self.assertEqual(run_daily.check_breaches(self.acct(cash=2_700.0), positions), [])
        self.assertEqual(len(run_daily.check_breaches(self.acct(cash=2_699.0), positions)), 1)


class FakeClient:
    """Broker stub: no positions, no orders. Any order submit is recorded."""

    def __init__(self):
        self.orders = []

    def get_positions(self):
        return []

    def get_option_snapshot(self, symbols):
        return {}

    def submit_mleg_limit(self, legs, qty, limit_price):
        self.orders.append((legs, qty, limit_price))
        raise AssertionError("no order may be submitted in these scenarios")


class TestRunSleeve(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_regime_env_blocks_entry_and_persists_book(self):
        import os
        old = {k: os.environ.pop(k, None) for k in ("TR_WORKER_URL", "TR_WORKER_TOKEN")}
        try:
            summary = ss.run_sleeve(FakeClient(), self.root, TODAY)
        finally:
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v
        self.assertFalse(summary["entry"]["allowed"])
        self.assertIn("regime", summary["entry"]["reason"])
        self.assertTrue((self.root / "state" / "spread_book.json").exists())
        self.assertIn("not read", summary["gex"])

    def test_killed_book_stays_killed_and_never_enters(self):
        ss.save_book(self.root, fresh_book(killed=True, kill_reason="3 breaches logged"))
        summary = ss.run_sleeve(FakeClient(), self.root, TODAY)
        self.assertTrue(summary["killed"])
        self.assertIn("killed", summary["entry"]["reason"])

    def test_expired_spread_realizes_full_credit(self):
        book = fresh_book(open_positions=[open_pos(expiry="2026-08-28")])
        ss.save_book(self.root, book)
        import os
        old = {k: os.environ.pop(k, None) for k in ("TR_WORKER_URL", "TR_WORKER_TOKEN")}
        try:
            summary = ss.run_sleeve(FakeClient(), self.root, TODAY)
        finally:
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v
        # 0.60 credit * 100 * 2 = 120 realized, position moved to closed
        self.assertEqual(summary["realized_pnl"], 120.0)
        self.assertEqual(summary["open_positions"], [])


if __name__ == "__main__":
    unittest.main()
