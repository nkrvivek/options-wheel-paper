#!/usr/bin/env python3
"""Flatten the retired options-wheel paper account to a clean cash baseline.

PAPER ONLY: pins paper-api.alpaca.markets and refuses any account other than
PA3OF0SFKF40. Cancels open orders, then buys back each short option and sells
each long position with a limit at the mid, steps toward the far side every
2 minutes (3 steps), and finally sends a market order. Prints the final
equity as the new baseline. DJ-20261028-04; user 2026-10-28: "reset the
paperbooks ... to start from a proper baseline".

Usage: python3 scripts/flatten_paper.py [--dry]
"""
import json, re, sys, time, urllib.request
from pathlib import Path

HOST = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"
ACCOUNT = "PA3OF0SFKF40"
env = {}
for l in (Path(__file__).resolve().parents[1] / ".env").read_text().splitlines():
    m = re.match(r"^(ALPACA_API_KEY|ALPACA_SECRET_KEY)=(.*)$", l.strip())
    if m: env[m.group(1)] = m.group(2).strip().strip("\"'")
H = {"APCA-API-KEY-ID": env["ALPACA_API_KEY"], "APCA-API-SECRET-KEY": env["ALPACA_SECRET_KEY"], "Content-Type": "application/json"}
DRY = "--dry" in sys.argv

def call(method, url, body=None):
    req = urllib.request.Request(url, headers=H, method=method, data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=30) as r:
        t = r.read()
        return json.loads(t) if t else None

acct = call("GET", f"{HOST}/v2/account")
assert acct["account_number"] == ACCOUNT, f"refusing: account {acct['account_number']} is not {ACCOUNT}"
clock = call("GET", f"{HOST}/v2/clock")
print("account", ACCOUNT, "equity", acct["equity"], "cash", acct["cash"], "market open", clock["is_open"])
if not DRY and not clock["is_open"]:
    sys.exit("market closed; nothing placed")
if not DRY:
    call("DELETE", f"{HOST}/v2/orders")

def quote(sym, opt):
    if opt:
        q = call("GET", f"{DATA}/v1beta1/options/quotes/latest?symbols={sym}&feed=indicative")["quotes"][sym]
    else:
        q = call("GET", f"{DATA}/v2/stocks/{sym}/quotes/latest")["quote"]
    return float(q["bp"]), float(q["ap"])

for p in call("GET", f"{HOST}/v2/positions"):
    sym, qty = p["symbol"], float(p["qty"]); opt = p["asset_class"] == "us_option"
    side = "buy" if qty < 0 else "sell"; n = abs(int(qty))
    bid, ask = quote(sym, opt)
    far = ask if side == "buy" else bid
    mid = round((bid + ask) / 2, 2)
    print(f"{sym} qty {qty} -> {side} {n} | bid {bid} ask {ask} mid {mid}")
    if DRY: continue
    filled = False
    for step in range(4):
        px = round(mid + (far - mid) * step / 3, 2)
        o = call("POST", f"{HOST}/v2/orders", {"symbol": sym, "qty": str(n), "side": side, "type": "limit", "limit_price": f"{max(px, 0.01):.2f}", "time_in_force": "day"})
        time.sleep(120)
        st = call("GET", f"{HOST}/v2/orders/{o['id']}")
        if st["status"] == "filled":
            print(f"  filled at {st['filled_avg_price']} (step {step})"); filled = True; break
        call("DELETE", f"{HOST}/v2/orders/{o['id']}"); time.sleep(2)
    if not filled:
        o = call("POST", f"{HOST}/v2/orders", {"symbol": sym, "qty": str(n), "side": side, "type": "market", "time_in_force": "day"})
        time.sleep(10); print("  market:", call("GET", f"{HOST}/v2/orders/{o['id']}")["status"])

a2 = call("GET", f"{HOST}/v2/account"); left = call("GET", f"{HOST}/v2/positions")
print("BASELINE equity", a2["equity"], "cash", a2["cash"], "positions left", len(left))
