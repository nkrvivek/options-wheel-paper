"""Closed-trade ledger and share hold-time cap for the wheel (DJ-20261001-03).

Each daily run diffs yesterday's positions against today's. A position that
vanished is a closed trade; its P&L comes from the closing fill, or, with no
fill, from how a short option ends (expired or assigned keeps the credit; the
assigned shares carry the loss as their own trade). Win rate alone hides a few
large losers, so `stats` reports average win, average loss and profit factor.
"""

import logging
from datetime import date

from alpaca.trading.enums import OrderSide, QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

from config.params import SHARE_HOLD_MAX_DAYS
from config.spread_params import UNDERLYING as SPREAD_UNDERLYING
from .utils import parse_option_symbol

logger = logging.getLogger(f"strategy.{__name__}")

OPTION, EQUITY = "us_option", "us_equity"


def _underlying(p):
    if p["asset_class"] == OPTION:
        return parse_option_symbol(p["symbol"])[0]
    return p["symbol"]


def _wheel_only(positions):
    return [p for p in positions if _underlying(p) != SPREAD_UNDERLYING]


def diff_closes(prev, cur, fill_price, today):
    """Closed trades between two position snapshots.

    prev, cur: [{"asset_class", "symbol", "qty", "avg_entry"}] (daily_state shape).
    fill_price(symbol, side) -> float | None: the closing fill, if any.
    """
    prev, cur = _wheel_only(prev), _wheel_only(cur)
    held = {p["symbol"] for p in cur}
    gone = [p for p in prev if p["symbol"] not in held]
    trades = []
    for p in gone:
        qty = abs(int(float(p["qty"])))
        entry = abs(float(p["avg_entry"]))
        if p["asset_class"] == OPTION:
            px = fill_price(p["symbol"], "buy")
            how = "bought back" if px is not None else "expired or assigned"
            pnl = (entry - (px or 0.0)) * 100 * qty
        else:
            px = fill_price(p["symbol"], "sell")
            how = "sold"
            if px is None:
                px = _called_away_strike(p["symbol"], gone)
                how = "called away" if px is not None else "unresolved"
            pnl = None if px is None else (px - entry) * qty
        trades.append({"id": f"{p['symbol']}:{today}", "date": today, "symbol": p["symbol"],
                       "underlying": _underlying(p), "how": how, "entry": entry,
                       "exit": px, "qty": qty, "pnl": None if pnl is None else round(pnl, 2)})
    return trades


def _called_away_strike(underlying, gone):
    for g in gone:
        if g["asset_class"] == OPTION:
            sym, kind, strike = parse_option_symbol(g["symbol"])
            if sym == underlying and kind == "C":
                return strike
    return None


def stats(trades):
    """Win rate next to the numbers that win rate hides."""
    pnls = [t["pnl"] for t in trades if t.get("pnl") is not None]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    gross_loss = -sum(losses)
    return {
        "closed": len(pnls),
        "unresolved": len(trades) - len(pnls),
        "win_rate": round(len(wins) / len(pnls), 3) if pnls else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": round(sum(wins) / gross_loss, 2) if gross_loss > 0 else None,
        "net": round(sum(pnls), 2),
    }


def stats_line(s):
    if s.get("error"):
        return f"wheel trade ledger FAILED: {s['error']}"
    if not s["closed"]:
        return "wheel closed trades: none yet"
    def money(x):
        return "n/a" if x is None else f"${x:,.0f}"
    pf = "n/a (no losses yet)" if s["profit_factor"] is None else f"{s['profit_factor']:.2f}"
    line = (f"wheel closed trades: {s['closed']}, won {s['win_rate']:.0%}, "
            f"avg win {money(s['avg_win'])}, avg loss {money(s['avg_loss'])}, "
            f"profit factor {pf}, net {money(s['net'])}")
    if s["unresolved"]:
        line += f" ({s['unresolved']} unresolved, not counted)"
    return line


def update_open_since(prev_map, cur, today):
    """{symbol: first date seen held}. Dropped symbols leave; new ones start today."""
    return {p["symbol"]: prev_map.get(p["symbol"], today) for p in _wheel_only(cur)}


def over_hold_cap(first_seen, today):
    """True once shares have been held SHARE_HOLD_MAX_DAYS or more."""
    if not first_seen:
        return False
    return (today - date.fromisoformat(first_seen)).days >= SHARE_HOLD_MAX_DAYS


def broker_fill_price(client, since):
    """fill_price(symbol, side) backed by the latest order filled on or after
    `since` (the previous snapshot's date), so an older cycle's fill on the
    same ticker is never read as this close."""
    def fill_price(symbol, side):
        req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, symbols=[symbol],
                               side=OrderSide.BUY if side == "buy" else OrderSide.SELL, limit=50)
        filled = [o for o in client.trade_client.get_orders(req)
                  if o.filled_at and o.filled_avg_price and o.filled_at.date() >= since]
        if not filled:
            return None
        return float(max(filled, key=lambda o: o.filled_at).filled_avg_price)
    return fill_price
