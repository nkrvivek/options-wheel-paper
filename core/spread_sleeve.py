"""spy-spread sleeve: SPY put credit spreads inside the shared paper account.

Preregistered rules (DJ-20260827-01). The pure decision functions live at the
top and are what tests/test_spread_sleeve.py pins; run_sleeve at the bottom
wires them to the broker and the sleeve's own ledger (state/spread_book.json).

Every gate fails closed: no regime read -> no entry, no VIX -> no entry (never
read as 0), an unpriced open spread is flagged rather than silently held. GEX
is not readable from CI and is logged as "not read" — never a default strike
and never a block.
"""
import json
import os
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path

from config import spread_params as sp
from core.r2_state import SPREAD_BOOK_KEY, WheelState, get_state_store

BOOK_FILENAME = "spread_book.json"
FILL_POLL_SECONDS = 90
FILL_POLL_INTERVAL = 10
# Debit buffer when closing: pay up to 5% over the marked cost rather than
# leave a stop unexecuted over a penny.
CLOSE_LIMIT_BUFFER = 1.05


# ---------------------------------------------------------------- pure rules

def entry_allowed(regime, book, today):
    """(ok, reason). regime = parsed /regime body or None; None blocks."""
    if book.get("killed"):
        return False, f"sleeve killed: {book.get('kill_reason') or 'kill criterion fired'}"
    if not isinstance(regime, dict):
        return False, "no regime read — entry blocked (fail closed)"
    tier = regime.get("tier")
    if tier not in ("clear", "caution"):
        return False, f"regime tier {tier!r} is not clear/caution — entry blocked"
    vix = regime.get("vix")
    if not isinstance(vix, (int, float)):
        return False, "vix unread — never read as 0, entry blocked"
    if not (sp.VIX_MIN <= vix <= sp.VIX_MAX):
        return False, f"vix {vix} outside [{sp.VIX_MIN}, {sp.VIX_MAX}] band"
    last = book.get("last_entry_date")
    if last:
        days = (today - date.fromisoformat(last)).days
        if days < sp.ENTRY_COOLDOWN_DAYS:
            return False, f"cooldown: last entry {last} was {days}d ago (< {sp.ENTRY_COOLDOWN_DAYS}d)"
    expiries = {p["expiry"] for p in book.get("open_positions", [])}
    if len(expiries) >= sp.MAX_EXPIRIES:
        return False, f"expiries at cap ({len(expiries)} of {sp.MAX_EXPIRIES})"
    return True, "regime + band + cadence clear"


def _mid(c):
    bid, ask = c.get("bid"), c.get("ask")
    if not isinstance(bid, (int, float)) or not isinstance(ask, (int, float)):
        return None
    return (bid + ask) / 2


def select_spread(candidates, today):
    """Pick short put by delta in the preregistered band, long WIDTH lower.

    candidates: [{symbol, strike, expiry, delta, bid, ask}] (puts only).
    Returns (pick, reason, rejections): pick is {short, long, credit, expiry}
    or None. rejections is one STRUCTURED record per delta-band short that
    could not form a spread — {"symbol", "reason": "below_credit_floor" |
    "no_long_leg", "credit"?}. Full rejection detail persists in
    state/daily_state.json (via _try_entry); the digest only carries
    rejection_summary() + the top nearest_misses() (2026-08-29 template fix
    — the old "; ".join of every rejection dumped ~30 candidates inline
    into the email).
    """
    by_key = {}
    for c in candidates:
        if _mid(c) is None:
            continue
        by_key[(c["expiry"], c["strike"])] = c

    shorts = [
        c for c in by_key.values()
        if isinstance(c.get("delta"), (int, float))
        and sp.SHORT_DELTA_MIN <= abs(c["delta"]) <= sp.SHORT_DELTA_MAX
    ]
    if not shorts:
        return None, f"no short candidate with delta in [{sp.SHORT_DELTA_MIN}, {sp.SHORT_DELTA_MAX}] and a two-sided quote", []

    shorts.sort(key=lambda c: abs(abs(c["delta"]) - sp.SHORT_DELTA_TARGET))
    rejections = []
    for short in shorts:
        long_ = by_key.get((short["expiry"], short["strike"] - sp.WIDTH))
        if long_ is None:
            rejections.append({"symbol": short["symbol"], "reason": "no_long_leg"})
            continue
        credit = _mid(short) - _mid(long_)
        if credit < sp.MIN_CREDIT:
            rejections.append({"symbol": short["symbol"],
                               "reason": "below_credit_floor",
                               "credit": round(credit, 4)})
            continue
        return ({"short": short, "long": long_, "credit": round(credit, 4), "expiry": short["expiry"]},
                "selected", rejections)
    return None, rejection_summary(rejections), rejections


def rejection_summary(rejections):
    """Counts by rejection reason + the best near-miss, e.g.
    '31 candidates: 27 below $0.55 credit floor (best 0.50 at <symbol>),
    4 no quoted long leg'. Pure formatter — pinned by
    tests/test_digest_format.py."""
    below = [r for r in rejections
             if r.get("reason") == "below_credit_floor"
             and isinstance(r.get("credit"), (int, float))]
    no_long = [r for r in rejections if r.get("reason") == "no_long_leg"]
    parts = []
    if below:
        best = max(below, key=lambda r: r["credit"])
        parts.append(
            f"{len(below)} below ${sp.MIN_CREDIT:.2f} credit floor "
            f"(best {best['credit']:.2f} at {best['symbol']})")
    if no_long:
        parts.append(f"{len(no_long)} no quoted long leg")
    if not parts:
        return "no rejections"
    return f"{len(rejections)} candidates: " + ", ".join(parts)


def nearest_misses(rejections, limit=5):
    """Top-`limit` credit-floor rejections, best (highest) credit first —
    the digest's short near-miss list."""
    below = [r for r in rejections
             if r.get("reason") == "below_credit_floor"
             and isinstance(r.get("credit"), (int, float))]
    below.sort(key=lambda r: r["credit"], reverse=True)
    return below[:limit]


def position_size(credit):
    """Contracts such that (WIDTH - credit) * 100 * qty <= 5% of the sleeve."""
    if not isinstance(credit, (int, float)) or credit <= 0 or credit >= sp.WIDTH:
        return 0
    risk_per_contract = (sp.WIDTH - credit) * 100
    return int(sp.MAX_RISK_FRAC * sp.SLEEVE_NOTIONAL // risk_per_contract)


def exit_decisions(open_positions, quotes, today):
    """One decision per open spread: close (dte | take-profit | stop), hold,
    or unpriced. An unpriced spread outside the DTE window is a flag, never a
    silent hold — the digest must show the profit trigger did not run."""
    decisions = []
    for pos in open_positions:
        dte = (date.fromisoformat(pos["expiry"]) - today).days
        base = {"id": pos["id"], "dte": dte}
        if dte <= sp.CLOSE_DTE:
            decisions.append({**base, "action": "close", "why": "dte"})
            continue
        cost_short = _mid(quotes.get(pos["short_symbol"], {}))
        cost_long = _mid(quotes.get(pos["long_symbol"], {}))
        if cost_short is None or cost_long is None:
            decisions.append({**base, "action": "unpriced"})
            continue
        cost = cost_short - cost_long
        base["cost"] = round(cost, 4)
        if cost <= sp.TP_FRAC * pos["credit"]:
            decisions.append({**base, "action": "close", "why": "take-profit"})
        elif cost >= sp.STOP_MULT * pos["credit"]:
            decisions.append({**base, "action": "close", "why": "stop"})
        else:
            decisions.append({**base, "action": "hold"})
    return decisions


def kill_check(book, unrealized_pnl):
    """(killed, reason). Drawdown measured against the sleeve notional.

    unrealized_pnl=None means the marks could not be read: the check runs on
    realized alone and SAYS so — what could not be measured never rescues a
    book that is already through the threshold on what could."""
    if book.get("killed"):
        return True, book.get("kill_reason") or "already killed"
    n = len(book.get("breaches", []))
    if n >= sp.KILL_BREACHES:
        return True, f"{n} breaches logged (kill at {sp.KILL_BREACHES})"
    threshold = sp.KILL_DRAWDOWN_FRAC * sp.SLEEVE_NOTIONAL
    realized = float(book.get("realized_pnl", 0.0))
    if unrealized_pnl is None:
        if realized <= -threshold:
            return True, f"drawdown ${-realized:,.0f} on realized alone (unrealized unread)"
        return False, "ok on realized; unrealized unread — drawdown partially measured"
    total = realized + unrealized_pnl
    if total <= -threshold:
        return True, f"drawdown ${-total:,.0f} >= ${threshold:,.0f} ({sp.KILL_DRAWDOWN_FRAC:.0%} of sleeve)"
    return False, "ok"


# ------------------------------------------------------------- broker wiring

def fetch_regime():
    """GET the trade-refresh worker's /regime. Any failure -> None (blocks)."""
    url, token = os.getenv("TR_WORKER_URL"), os.getenv("TR_WORKER_TOKEN")
    if not url or not token:
        return None
    try:
        req = urllib.request.Request(
            f"{url}/regime?token={token}",
            headers={"User-Agent": "paper-wheel/1.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _book_store(root):
    """R2 when credentialed, otherwise a store rooted at ``root``.

    Keeping ``root`` meaningful on the local path is what lets the sleeve
    tests point at a tmpdir (and keeps `python scripts/run_daily.py` on a
    laptop writing ./state/spread_book.json exactly as before). In the
    container the R2 client wins and ``root`` is unused.
    """
    store = get_state_store()
    return store if store.remote else WheelState(local_root=root)


def load_book(root):
    """The sleeve ledger, from R2 (or <root>/state/spread_book.json in dev)."""
    book = _book_store(root).read_json(SPREAD_BOOK_KEY)
    if book is not None:
        return book
    return {
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


def save_book(root, book):
    _book_store(root).write_json(SPREAD_BOOK_KEY, book)


def _quotes_for(client, symbols):
    """symbol -> {bid, ask} from snapshots; a missing side stays None."""
    if not symbols:
        return {}
    quotes = {}
    for sym, snap in (client.get_option_snapshot(list(symbols)) or {}).items():
        q = getattr(snap, "latest_quote", None)
        quotes[sym] = {
            "bid": getattr(q, "bid_price", None),
            "ask": getattr(q, "ask_price", None),
        }
    return quotes


def _submit_and_poll(client, legs, qty, limit_price, note):
    """Submit an MLEG limit order, poll for a fill, cancel if unfilled.

    Returns (filled: bool, detail: str, avg_prices: {symbol: price}|None)."""
    order = client.submit_mleg_limit(legs, qty, limit_price)
    order_id = str(order.id)
    deadline = time.time() + FILL_POLL_SECONDS
    while time.time() < deadline:
        current = client.get_order(order_id)
        status = str(getattr(current.status, "value", current.status))
        if status == "filled":
            prices = {
                str(l.symbol): float(l.filled_avg_price)
                for l in (current.legs or [])
                if getattr(l, "filled_avg_price", None) is not None
            }
            return True, f"{note}: filled", prices
        if status in ("canceled", "expired", "rejected"):
            return False, f"{note}: order {status}", None
        time.sleep(FILL_POLL_INTERVAL)
    try:
        client.cancel_order(order_id)
    except Exception:
        pass
    return False, f"{note}: unfilled after {FILL_POLL_SECONDS}s, canceled", None


def _reconcile(book, broker_symbols, today):
    """Match the ledger against what the broker actually holds.

    A spread whose legs vanished before expiry is ledger drift the sleeve did
    not order — logged as a breach, never silently dropped. Past expiry with
    both legs gone, the spread expired worthless: full credit realized (the
    7-DTE hard close makes assignment-at-expiry a rule breach, not a path)."""
    still_open, notes = [], []
    for pos in book["open_positions"]:
        short_held = pos["short_symbol"] in broker_symbols
        long_held = pos["long_symbol"] in broker_symbols
        expired = date.fromisoformat(pos["expiry"]) < today
        if short_held or long_held:
            if short_held != long_held:
                msg = f"{pos['id']}: one leg missing at broker (short={short_held}, long={long_held})"
                notes.append(msg)
                if msg not in book["breaches"]:
                    book["breaches"].append(msg)
            still_open.append(pos)
        elif expired:
            realized = pos["credit"] * 100 * pos["qty"]
            book["realized_pnl"] = round(book["realized_pnl"] + realized, 2)
            book["closed_positions"].append(
                {**pos, "closed": today.isoformat(), "why": "expired", "realized": round(realized, 2)}
            )
            notes.append(f"{pos['id']}: expired worthless, +${realized:,.0f}")
        else:
            msg = f"{pos['id']}: both legs missing at broker before expiry — unexplained"
            notes.append(msg)
            if msg not in book["breaches"]:
                book["breaches"].append(msg)
            book["closed_positions"].append(
                {**pos, "closed": today.isoformat(), "why": "missing-at-broker", "realized": None}
            )
    book["open_positions"] = still_open
    return notes


def _close_spread(client, book, pos, decision, today):
    cost = decision.get("cost")
    if cost is None:
        # dte-close on an unpriced spread: cross wide with the width as ceiling.
        limit = float(sp.WIDTH)
    else:
        limit = max(0.01, round(cost * CLOSE_LIMIT_BUFFER, 2))
    legs = [
        {"symbol": pos["short_symbol"], "side": "buy"},
        {"symbol": pos["long_symbol"], "side": "sell"},
    ]
    filled, detail, prices = _submit_and_poll(
        client, legs, pos["qty"], limit, f"close {pos['id']} ({decision['why']})"
    )
    if not filled:
        return detail
    if prices and pos["short_symbol"] in prices and pos["long_symbol"] in prices:
        debit = prices[pos["short_symbol"]] - prices[pos["long_symbol"]]
    else:
        debit = limit
    realized = round((pos["credit"] - debit) * 100 * pos["qty"], 2)
    book["realized_pnl"] = round(book["realized_pnl"] + realized, 2)
    book["open_positions"] = [p for p in book["open_positions"] if p["id"] != pos["id"]]
    book["closed_positions"].append(
        {**pos, "closed": today.isoformat(), "why": decision["why"], "realized": realized}
    )
    return f"{detail}, realized ${realized:,.2f}"


def _entry_candidates(client, today):
    """SPY put contracts in the sleeve's own DTE window, quoted with greeks."""
    contracts = client.get_options_contracts([sp.UNDERLYING], "put")
    in_window = []
    for c in contracts:
        exp = c.expiration_date if isinstance(c.expiration_date, date) else date.fromisoformat(str(c.expiration_date))
        if sp.DTE_MIN <= (exp - today).days <= sp.DTE_MAX:
            in_window.append((str(c.symbol), float(c.strike_price), exp.isoformat()))
    spot = float(client.get_stock_latest_trade(sp.UNDERLYING)[sp.UNDERLYING].price)
    near = [(s, k, e) for s, k, e in in_window if 0.75 * spot <= k <= spot]
    snaps = client.get_option_snapshot([s for s, _, _ in near]) or {}
    candidates = []
    for sym, strike, expiry in near:
        snap = snaps.get(sym)
        if snap is None:
            continue
        q = getattr(snap, "latest_quote", None)
        greeks = getattr(snap, "greeks", None)
        candidates.append({
            "symbol": sym,
            "strike": strike,
            "expiry": expiry,
            "delta": getattr(greeks, "delta", None),
            "bid": getattr(q, "bid_price", None),
            "ask": getattr(q, "ask_price", None),
        })
    return candidates


def _try_entry(client, book, today):
    regime = fetch_regime()
    ok, reason = entry_allowed(regime, book, today)
    result = {"regime": regime, "allowed": ok, "reason": reason}
    if not ok:
        return result
    pick, why, rejections = select_spread(_entry_candidates(client, today), today)
    if pick is None:
        # `reason` is the compact summary (digest); `rejections` is the full
        # per-candidate detail, persisted in state/daily_state.json under
        # spread.entry.rejections.
        result.update({"allowed": False,
                       "reason": f"no spread selected: {why}",
                       "rejections": rejections})
        return result
    qty = position_size(pick["credit"])
    if qty < 1:
        result.update({"allowed": False, "reason": f"sized to 0 at credit {pick['credit']}"})
        return result
    legs = [
        {"symbol": pick["short"]["symbol"], "side": "sell"},
        {"symbol": pick["long"]["symbol"], "side": "buy"},
    ]
    # Negative limit = net credit; ask for the mid.
    filled, detail, prices = _submit_and_poll(
        client, legs, qty, -round(pick["credit"], 2),
        f"open {pick['short']['symbol']}/{pick['long']['symbol']} x{qty}",
    )
    result["order"] = detail
    if not filled:
        return result
    if prices and pick["short"]["symbol"] in prices and pick["long"]["symbol"] in prices:
        credit = round(prices[pick["short"]["symbol"]] - prices[pick["long"]["symbol"]], 4)
    else:
        credit = pick["credit"]
    book["open_positions"].append({
        "id": f"sp-{today.isoformat()}",
        "opened": today.isoformat(),
        "expiry": pick["expiry"],
        "short_symbol": pick["short"]["symbol"],
        "long_symbol": pick["long"]["symbol"],
        "short_strike": pick["short"]["strike"],
        "long_strike": pick["long"]["strike"],
        "qty": qty,
        "credit": credit,
    })
    book["last_entry_date"] = today.isoformat()
    return result


def run_sleeve(client, root, today=None):
    """The sleeve's daily step. Returns the summary dict run_daily persists
    under state["spread"] and renders into the shared digest."""
    today = today or datetime.now(__import__("zoneinfo").ZoneInfo("America/New_York")).date()
    book = load_book(root)

    positions = {str(p.symbol): p for p in client.get_positions()}
    notes = _reconcile(book, set(positions), today)

    unrealized = 0.0
    for pos in book["open_positions"]:
        for sym in (pos["short_symbol"], pos["long_symbol"]):
            pl = getattr(positions.get(sym), "unrealized_pl", None)
            if pl is None:
                unrealized = None
                break
            unrealized += float(pl)
        if unrealized is None:
            break

    quotes = _quotes_for(client, [s for p in book["open_positions"] for s in (p["short_symbol"], p["long_symbol"])])
    decisions = exit_decisions(list(book["open_positions"]), quotes, today)
    for d in decisions:
        if d["action"] == "close":
            pos = next(p for p in book["open_positions"] if p["id"] == d["id"])
            notes.append(_close_spread(client, book, pos, d, today))
        elif d["action"] == "unpriced":
            notes.append(f"{d['id']}: UNPRICED — profit trigger not run")

    killed, kill_reason = kill_check(book, unrealized)
    if killed and not book["killed"]:
        book["killed"] = True
        book["kill_reason"] = kill_reason
        notes.append(f"KILL: {kill_reason}")

    if book["killed"]:
        entry = {"allowed": False, "reason": f"sleeve killed: {book['kill_reason']}"}
    else:
        entry = _try_entry(client, book, today)

    save_book(root, book)
    return {
        "sleeve": "spy-spread",
        "date": today.isoformat(),
        "killed": book["killed"],
        "kill_reason": book["kill_reason"],
        "realized_pnl": book["realized_pnl"],
        "unrealized_pnl": unrealized,
        "open_positions": book["open_positions"],
        "breaches": book["breaches"],
        "entry": entry,
        "exits": decisions,
        "notes": notes,
        "gex": "not read (no UW access in CI) — logged, never a default",
    }


def digest_lines(spread):
    """Lines for the shared paper-wheel digest — same email, sleeve section."""
    if spread.get("status") == "error":
        return ["spy-spread sleeve: ERROR — " + spread.get("detail", "?")]
    unreal = spread.get("unrealized_pnl")
    unreal_s = f"${unreal:,.2f}" if unreal is not None else "unread"
    lines = [
        f"spy-spread sleeve: {'KILLED — ' + str(spread.get('kill_reason')) if spread.get('killed') else 'alive'}",
        f"  realized ${spread.get('realized_pnl', 0):,.2f} · unrealized {unreal_s} · open {len(spread.get('open_positions', []))}",
    ]
    entry = spread.get("entry", {})
    lines.append(f"  entry: {'OPENED' if entry.get('allowed') and 'filled' in str(entry.get('order', '')) else 'no'} — {entry.get('order') or entry.get('reason', '?')}")
    # Top-5 nearest misses max (2026-08-29 template fix) — the full
    # rejection detail stays in state/daily_state.json, never in the email.
    for r in nearest_misses(entry.get("rejections") or []):
        lines.append(f"    near-miss: {r['symbol']} credit {r['credit']:.2f}")
    for note in spread.get("notes", []):
        lines.append(f"  {note}")
    if spread.get("breaches"):
        lines.append(f"  sleeve breaches: {len(spread['breaches'])}")
    return lines
