"""Early exit for the wheel's short puts (DJ-20261001-01).

The pure rule is `exit_reason`; `manage_short_puts` wires it to the broker.
A put whose open date or quote cannot be found is held, never guessed at.
"""

import logging
from datetime import date

from alpaca.trading.enums import AssetClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest

from config.params import PUT_PROFIT_TAKE, PUT_TIME_EXIT
from .utils import parse_option_symbol

logger = logging.getLogger(f"strategy.{__name__}")


def exit_reason(credit, mark, opened, expiry, today):
    """'profit', 'time', or None (hold)."""
    if mark is None or credit <= 0:
        return None
    if mark <= credit * (1 - PUT_PROFIT_TAKE):
        return "profit"
    if opened is not None:
        span = (expiry - opened).days
        if span > 0 and (today - opened).days / span >= PUT_TIME_EXIT:
            return "time"
    return None


def _open_date(client, symbol):
    """Date of the earliest filled sell-to-open for this contract, or None."""
    req = GetOrdersRequest(
        status=QueryOrderStatus.CLOSED, symbols=[symbol], side=OrderSide.SELL, limit=50
    )
    fills = [o.filled_at for o in client.trade_client.get_orders(req) if o.filled_at]
    return min(fills).date() if fills else None


def _quote(client, symbol):
    """(mid, ask) from the latest quote, or (None, None)."""
    snap = client.get_option_snapshot(symbol).get(symbol)
    q = getattr(snap, "latest_quote", None)
    if not q or not q.ask_price or q.bid_price is None:
        return None, None
    return (q.bid_price + q.ask_price) / 2, q.ask_price


def manage_short_puts(client, positions, today=None):
    """Buy back short puts the rule says to close. Returns one dict per close."""
    today = today or date.today()
    closed = []
    for p in positions:
        if p.asset_class != AssetClass.US_OPTION or int(p.qty) >= 0:
            continue
        underlying, kind, _ = parse_option_symbol(p.symbol)
        if kind != "P":
            continue
        try:
            mark, ask = _quote(client, p.symbol)
            opened = _open_date(client, p.symbol)
        except Exception as e:  # a lookup failure holds the put, and says so
            logger.error(f"early exit: lookup failed for {p.symbol}: {e}")
            continue
        credit = abs(float(p.avg_entry_price))
        expiry = _expiry(p.symbol)
        reason = exit_reason(credit, mark, opened, expiry, today)
        if opened is None:
            logger.warning(f"early exit: no open date for {p.symbol}; time rule skipped")
        if reason is None:
            continue
        # Pay the ask: a paper fill at mid would flatter the book.
        client.trade_client.submit_order(LimitOrderRequest(
            symbol=p.symbol, qty=abs(int(p.qty)), side=OrderSide.BUY,
            limit_price=round(ask, 2), time_in_force=TimeInForce.DAY,
        ))
        closed.append({"symbol": p.symbol, "underlying": underlying, "reason": reason,
                       "credit": credit, "mark": round(mark, 2), "limit": round(ask, 2)})
        logger.info(f"early exit: {p.symbol} {reason} credit {credit:.2f} mark {mark:.2f}")
    return closed


def _expiry(occ):
    """Expiry date from an OCC symbol (ROOT + YYMMDD + C/P + strike*1000)."""
    s = occ[-15:-9]
    return date(2000 + int(s[:2]), int(s[2:4]), int(s[4:6]))
