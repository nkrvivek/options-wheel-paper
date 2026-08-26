"""Daily paper-wheel run: earnings filter -> strategy -> state -> digest.

Runs in CI (see .github/workflows/wheel-daily.yml). Each step degrades loudly:
an unreachable earnings sheet blocks every new short leg rather than waving
them through, a failed strategy run still writes state and sends a RED digest.
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from config.params import EXPIRATION_MAX  # noqa: E402

OCC_RE = re.compile(r"^([A-Z.]+)(\d{6})([CP])(\d{8})$")


def load_symbols():
    path = ROOT / "config" / "symbol_list.txt"
    return [s.strip() for s in path.read_text().splitlines() if s.strip()]


def build_earnings_exclusions(symbols):
    """Names with a print inside the DTE window, or that could not be verified.

    Absent is never permissive: a name the sheet does not cover is excluded and
    named, and an unreachable sheet excludes everything.
    """
    url = os.getenv("TR_WORKER_URL")
    token = os.getenv("TR_WORKER_TOKEN")
    if not url or not token:
        return set(symbols), "no TR_WORKER_URL/TR_WORKER_TOKEN — cannot verify earnings, all names excluded"
    try:
        # Cloudflare answers the default Python-urllib UA with 403 error 1010,
        # so the request names itself.
        req = urllib.request.Request(
            f"{url}/marketdata-earnings?token={token}",
            headers={"User-Agent": "paper-wheel/1.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            sheet = json.loads(r.read())
    except Exception as e:
        return set(symbols), f"earnings sheet unreachable ({e.__class__.__name__}) — all names excluded"

    records = sheet.get("records") or {}
    horizon = (datetime.now(timezone.utc) + timedelta(days=EXPIRATION_MAX)).date()
    excluded, reasons = set(), []
    for sym in symbols:
        rec = records.get(sym)
        if not rec:
            excluded.add(sym)
            reasons.append(f"{sym}: not on the earnings sheet")
            continue
        next_date = rec.get("next_date")
        if rec.get("status") == "ok" and next_date:
            try:
                if datetime.strptime(next_date, "%Y-%m-%d").date() <= horizon:
                    excluded.add(sym)
                    reasons.append(f"{sym}: earnings {next_date} inside {EXPIRATION_MAX}d window")
            except ValueError:
                excluded.add(sym)
                reasons.append(f"{sym}: unreadable next_date {next_date!r}")
        elif rec.get("status") == "none":
            # UW answered and knows no forward date — verified, not absent.
            continue
        else:
            excluded.add(sym)
            reasons.append(f"{sym}: unreadable earnings status {rec.get('status')!r}")
    return excluded, "; ".join(reasons) if reasons else "none"


def run_strategy():
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_strategy.py"), "--strat-log"],
        env=env, cwd=ROOT, capture_output=True, text=True, timeout=1200,
    )
    return proc.returncode, (proc.stdout + proc.stderr)[-4000:]


def check_breaches(account, positions):
    """The kill criterion: any naked or non-cash-secured short leg.

    Fail-closed — a leg that cannot be parsed is a breach, never a skip.
    """
    breaches = []
    shares = {}
    for p in positions:
        if str(p.get("asset_class")) == "us_equity":
            shares[p["symbol"]] = shares.get(p["symbol"], 0) + float(p["qty"])
    put_collateral = 0.0
    for p in positions:
        if str(p.get("asset_class")) != "us_option" or float(p["qty"]) >= 0:
            continue
        qty = abs(float(p["qty"]))
        m = OCC_RE.match(p["symbol"])
        if not m:
            breaches.append(f"unparseable short option leg {p['symbol']}")
            continue
        underlying, _, right, strike_raw = m.groups()
        strike = int(strike_raw) / 1000
        if right == "C":
            if shares.get(underlying, 0) < 100 * qty:
                breaches.append(
                    f"naked call {p['symbol']}: {shares.get(underlying, 0):.0f} shares held, {100 * qty:.0f} needed"
                )
        else:
            put_collateral += strike * 100 * qty
    cash = float(account["cash"])
    if put_collateral > cash:
        breaches.append(f"short puts need ${put_collateral:,.0f} collateral against ${cash:,.0f} cash")
    return breaches


def send_digest(subject, body):
    key, frm, to = os.getenv("RESEND_API_KEY"), os.getenv("RESEND_FROM"), os.getenv("RESEND_TO")
    if not (key and frm and to):
        print("email skip: RESEND_* not configured")
        return
    payload = json.dumps({"from": frm, "to": [to], "subject": subject, "text": body}).encode()
    # Cloudflare fronts api.resend.com and answers the default Python-urllib
    # UA with 403, same as the earnings sheet above — the request names itself.
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "paper-wheel/1.0",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=30)
        print("digest sent")
    except Exception as e:
        print(f"digest send failed: {e}")


def main():
    today = datetime.now(timezone.utc).date().isoformat()
    symbols = load_symbols()
    excluded, exclusion_detail = build_earnings_exclusions(symbols)
    (ROOT / "config" / "earnings_exclude.txt").write_text("\n".join(sorted(excluded)) + "\n")

    rc, output = run_strategy()

    from config.credentials import ALPACA_API_KEY, ALPACA_SECRET_KEY, IS_PAPER
    from core.broker_client import BrokerClient
    client = BrokerClient(api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY, paper=IS_PAPER)
    acct = client.trade_client.get_account()
    account = {"equity": float(acct.equity), "cash": float(acct.cash)}
    positions = [
        {"asset_class": str(p.asset_class.value), "symbol": p.symbol, "qty": p.qty,
         "avg_entry": p.avg_entry_price, "unrealized_pl": p.unrealized_pl}
        for p in client.get_positions()
    ]
    breaches = check_breaches(account, positions)

    all_excluded = set(symbols) == excluded
    if breaches or rc != 0:
        status = "RED"
    elif all_excluded:
        status = "YELLOW"
    else:
        status = "GREEN"
    state = {
        "date": today,
        "status": status,
        "run_ok": rc == 0,
        "equity": account["equity"],
        "cash": account["cash"],
        "positions": positions,
        "breaches": breaches,
        "excluded": sorted(excluded),
        "exclusion_detail": exclusion_detail,
        "universe": symbols,
    }
    state_dir = ROOT / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "daily_state.json").write_text(json.dumps(state, indent=1) + "\n")
    with open(state_dir / "nav_history.jsonl", "a") as f:
        f.write(json.dumps({"date": today, "equity": account["equity"], "cash": account["cash"]}) + "\n")

    lines = [
        f"paper-wheel {today} — {status}",
        f"equity ${account['equity']:,.2f} · cash ${account['cash']:,.2f}",
        f"positions: {len(positions)}",
    ]
    for p in positions:
        lines.append(f"  {p['symbol']} {p['qty']} @ {p['avg_entry']} (uPnL {p['unrealized_pl']})")
    if breaches:
        lines.append("BREACHES (kill criterion):")
        lines.extend(f"  {b}" for b in breaches)
    if excluded:
        lines.append(f"earnings-excluded: {sorted(excluded)}")
        lines.append(f"  {exclusion_detail}")
    if rc != 0:
        lines.append(f"strategy run FAILED (rc {rc}):")
        lines.append(output[-1500:])
    send_digest(f"[paper-wheel] {today} {status} — equity ${account['equity']:,.0f}", "\n".join(lines))
    print(json.dumps({k: state[k] for k in ("date", "status", "equity", "breaches", "excluded")}, indent=1))
    return 0 if status != "RED" else 1


if __name__ == "__main__":
    sys.exit(main())
