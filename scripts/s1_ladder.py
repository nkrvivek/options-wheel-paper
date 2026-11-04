#!/usr/bin/env python3
"""S1 ladder paper test: IV/RV-gated SPY/QQQ put credit verticals (from the Synthetix Alpha hackathon entry).

PAPER ONLY. Pins paper-api.alpaca.markets and refuses any account other than PA3OF0SFKF40.
Decision DJ-20261104-01; backtest: obsidian wiki/trading/research/hackathon-strategies-backtest-2026-11-03.md.

Two sleeves run side by side on SPY and QQQ:
  A: sell 0.20 delta put / buy 0.10 delta put     (backtest Sharpe 1.62, 2019-2026)
  B: sell 0.28 delta put / buy 0.15 delta put     (backtest Sharpe 1.51, more P/L)
Entry: first run of each ISO week, expiry 55-70 DTE (nearest 63), only if ORATS IV30 / 20-day HV >= 1.27,
       at most 4 open per sleeve per ticker, size = floor($2,000 / max loss per spread), min 1.
Exit (checked every run, on mid): cost <= 35% of credit (take 65%), cost >= 3x credit (loss = 2x credit),
       or 21 DTE or less.
State: state/s1_ladder.json; every action appended to state/s1_ladder_log.jsonl.

    python3 scripts/s1_ladder.py            # dry run
    python3 scripts/s1_ladder.py --live
"""
from __future__ import annotations

import json
import math
import sys
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"
ACCOUNT = "PA3OF0SFKF40"
STATE = ROOT / "state" / "s1_ladder.json"
LOG = ROOT / "state" / "s1_ladder_log.jsonl"
SLEEVES = {"A": (0.20, 0.10), "B": (0.28, 0.15)}
TICKERS = ["SPY", "QQQ"]
GATE = 1.27
RISK = 2000.0
MAX_OPEN = 4

env = dict(l.strip().split("=", 1) for l in (ROOT / ".env").read_text().splitlines() if "=" in l and not l.startswith("#"))
H = {"APCA-API-KEY-ID": env["ALPACA_API_KEY"].strip('"'), "APCA-API-SECRET-KEY": env["ALPACA_SECRET_KEY"].strip('"'),
     "Content-Type": "application/json"}
ORATS = [l.split("=", 1)[1].strip().strip('"') for l in open("/Users/Vivek/Development/autopilot-experiment/.env") if l.startswith("ORATS_TOKEN=")][0]


def req(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, headers=H, method=method)
    with urllib.request.urlopen(r, timeout=30) as resp:
        t = resp.read()
        return json.loads(t) if t else {}


def log(ev, **kw):
    kw.update(ev=ev, at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    with LOG.open("a") as f:
        f.write(json.dumps(kw) + "\n")
    print(ev, json.dumps(kw)[:300])


def load():
    return json.loads(STATE.read_text()) if STATE.exists() else {"open": [], "closed": [], "entered_weeks": []}


def save(s):
    STATE.write_text(json.dumps(s, indent=1))


def chain(tk, exp):
    out, tok = {}, None
    while True:
        u = f"{DATA}/v1beta1/options/snapshots/{tk}?feed=indicative&type=put&expiration_date={exp}&limit=1000" + (f"&page_token={tok}" if tok else "")
        d = req("GET", u)
        out.update(d.get("snapshots", {}))
        tok = d.get("next_page_token")
        if not tok:
            return out


def quote(sym):
    d = req("GET", f"{DATA}/v1beta1/options/snapshots?symbols={sym}&feed=indicative")["snapshots"].get(sym)
    if not d:
        return None
    q = d["latestQuote"]
    return float(q["bp"]), float(q["ap"])


def spread_cost(p):
    qs, ql = quote(p["short"]), quote(p["long"])
    if not qs or not ql or qs[1] <= 0:
        return None
    return (qs[0] + qs[1]) / 2 - (ql[0] + ql[1]) / 2


def gate(tk):
    c = req("GET", f"https://api.orats.io/datav2/cores?token={ORATS}&ticker={tk}&fields=tradeDate,iv30d,clsHv20d")["data"][0]
    return c["iv30d"] / c["clsHv20d"], c


def place(legs, qty, limit, intent):
    body = {"order_class": "mleg", "qty": str(qty), "type": "limit", "time_in_force": "day",
            "limit_price": f"{limit:.2f}", "legs": legs}
    return req("POST", f"{HOST}/v2/orders", body)


def wait(oid, secs=60):
    t0 = time.time()
    while time.time() - t0 < secs:
        o = req("GET", f"{HOST}/v2/orders/{oid}")
        if o["status"] == "filled":
            return o
        if o["status"] in ("canceled", "expired", "rejected"):
            return None
        time.sleep(5)
    try:
        req("DELETE", f"{HOST}/v2/orders/{oid}")
    except Exception:  # noqa: BLE001
        pass
    time.sleep(2)
    o = req("GET", f"{HOST}/v2/orders/{oid}")
    return o if o["status"] == "filled" else None


def main():
    live = "--live" in sys.argv
    acct = req("GET", f"{HOST}/v2/account")
    if acct["account_number"] != ACCOUNT or "paper-api" not in HOST:
        sys.exit(f"REFUSE: account {acct['account_number']} is not {ACCOUNT}")
    s = load()
    today = date.today()
    print(f"S1 ladder | account {ACCOUNT} equity {acct['equity']} cash {acct['cash']} | open {len(s['open'])} | live={live}")
    held = {p["symbol"]: float(p["qty"]) for p in req("GET", f"{HOST}/v2/positions")}

    # ---- manage open spreads
    still = []
    for p in s["open"]:
        dte = (date.fromisoformat(p["expiry"]) - today).days
        cost = spread_cost(p)
        why = None
        if cost is not None:
            if cost <= 0.35 * p["credit"]:
                why = "take65"
            elif cost >= 3 * p["credit"]:
                why = "loss2x"
        if dte <= 21:
            why = why or "dte21"
        print(f"  {p['sleeve']} {p['tk']} {p['short']}/{p['long']} x{p['qty']} credit {p['credit']:.2f} cost {cost if cost is None else round(cost, 2)} dte {dte} -> {why or 'hold'}")
        if not why:
            still.append(p)
            continue
        if not live:
            still.append(p)
            continue
        if held.get(p["short"], 0) >= 0 and held.get(p["long"], 0) <= 0:
            log("missing_legs", **p)
            continue
        legs = [{"symbol": p["short"], "side": "buy", "ratio_qty": "1", "position_intent": "buy_to_close"},
                {"symbol": p["long"], "side": "sell", "ratio_qty": "1", "position_intent": "sell_to_close"}]
        done = None
        for k, bump in enumerate((0.0, 0.05, 0.10, 0.20)):
            px = max(0.01, round((cost or p["credit"]) + bump, 2))
            o = place(legs, p["qty"], px, "close")
            done = wait(o["id"], 45)
            if done:
                break
        if done:
            fill = abs(float(done.get("filled_avg_price") or px))
            p.update(exit=today.isoformat(), why=why, exit_cost=fill,
                     pnl=round((p["credit"] - fill) * 100 * p["qty"] - 0.65 * 4 * p["qty"], 2))
            s["closed"].append(p)
            log("closed", **p)
        else:
            log("close_unfilled", **p)
            still.append(p)
    s["open"] = still

    # ---- weekly entries
    wk = f"{today.isocalendar()[0]}-W{today.isocalendar()[1]:02d}"
    if wk in s["entered_weeks"]:
        print(f"entries already done for {wk}")
    else:
        for tk in TICKERS:
            ratio, c = gate(tk)
            print(f"[gate] {tk} IV30 {c['iv30d']} / HV20 {c['clsHv20d']} = {ratio:.2f} (>= {GATE}?) as of {c['tradeDate']}")
            if ratio < GATE:
                if live:
                    log("gate_skip", tk=tk, ratio=round(ratio, 3), week=wk)
                continue
            lo, hi = today.fromordinal(today.toordinal() + 55), today.fromordinal(today.toordinal() + 70)
            cons = req("GET", f"{HOST}/v2/options/contracts?underlying_symbols={tk}&type=put&expiration_date_gte={lo}&expiration_date_lte={hi}&limit=1000")["option_contracts"]
            exps = sorted({x["expiration_date"] for x in cons}, key=lambda e: abs((date.fromisoformat(e) - today).days - 63))
            if not exps:
                log("no_expiry", tk=tk)
                continue
            exp = exps[0]
            snaps = chain(tk, exp)
            rows = []
            for sym, v in snaps.items():
                g, q = v.get("greeks") or {}, v.get("latestQuote") or {}
                if g.get("delta") is None or not q.get("ap"):
                    continue
                rows.append((sym, int(sym[-8:]) / 1000, abs(g["delta"]), float(q["bp"]), float(q["ap"])))
            for sl, (sd, ld) in SLEEVES.items():
                n_open = sum(1 for p in s["open"] if p["tk"] == tk and p["sleeve"] == sl)
                if n_open >= MAX_OPEN:
                    continue
                sh = min(rows, key=lambda r: abs(r[2] - sd))
                lg = min((r for r in rows if r[1] < sh[1]), key=lambda r: abs(r[2] - ld), default=None)
                if not lg:
                    continue
                credit = (sh[3] + sh[4]) / 2 - (lg[3] + lg[4]) / 2
                width = sh[1] - lg[1]
                maxloss = (width - credit) * 100
                if credit <= 0.10 or maxloss <= 0:
                    continue
                qty = max(1, math.floor(RISK / maxloss))
                spec = dict(sleeve=sl, tk=tk, short=sh[0], long=lg[0], short_strike=sh[1], long_strike=lg[1],
                            short_delta=round(sh[2], 3), expiry=exp, width=width, qty=qty, mid_credit=round(credit, 2),
                            gate=round(ratio, 3), week=wk)
                if not live:
                    print("  DRY RUN entry", json.dumps(spec))
                    continue
                legs = [{"symbol": sh[0], "side": "sell", "ratio_qty": "1", "position_intent": "sell_to_open"},
                        {"symbol": lg[0], "side": "buy", "ratio_qty": "1", "position_intent": "buy_to_open"}]
                done = None
                for give in (0.0, 0.05, 0.10):
                    px = -round(credit - give, 2)  # negative limit = net credit on Alpaca mleg
                    o = place(legs, qty, px, "open")
                    done = wait(o["id"], 45)
                    if done:
                        break
                if done:
                    spec.update(entry=today.isoformat(), credit=abs(float(done.get("filled_avg_price") or px)), order=done["id"])
                    s["open"].append(spec)
                    log("opened", **spec)
                else:
                    log("open_unfilled", **spec)
        if live:
            s["entered_weeks"].append(wk)
    if live:
        save(s)
    cl = s["closed"]
    if cl:
        wins = sum(1 for p in cl if p["pnl"] > 0)
        print(f"closed {len(cl)} | win {wins}/{len(cl)} | P/L ${sum(p['pnl'] for p in cl):,.2f}")


if __name__ == "__main__":
    main()
