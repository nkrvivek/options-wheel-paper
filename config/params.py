# The max dollar risk allowed by the strategy.  
MAX_RISK = 80_000

# The range of allowed Delta (absolute value) when choosing puts or calls to sell.  
# The goal is to balance low assignment risk (lower Delta) with high premiums (higher Delta).
DELTA_MIN = 0.20  # paper-wheel prereg 2026-08-25: CSP/CC delta band 0.20-0.30
DELTA_MAX = 0.30

# The range of allowed yield when choosing puts or calls to sell.
YIELD_MIN = 0.04
YIELD_MAX = 1.00

# The range of allowed days till expiry when choosing puts or calls to sell.
# The goal is to balance shorter expiry for consistent income generation with longer expiry for time value premium.
EXPIRATION_MIN = 30  # paper-wheel prereg 2026-08-25: 30-45 DTE
EXPIRATION_MAX = 45

# Only trade contracts with at least this much open interest.
OPEN_INTEREST_MIN = 500  # paper-wheel prereg 2026-08-25: liquidity floor

# The minimum score passed to core.strategy.select_options().
SCORE_MIN = 0.05

# paper-wheel prereg 2026-08-25: no single name may hold more than 20% of the
# book's cash as CSP collateral. Enforced in core.execution.sell_puts.
PER_NAME_CAP = 20_000

# paper-wheel prereg 2026-08-25: max bid-ask spread as a fraction of mark.
# Wider than this and a paper fill flatters the book. Enforced in
# core.strategy.filter_options.
SPREAD_MAX_FRAC = 0.10

# 2026-10-01: universe widened from 8 names to 26 (all priced so one contract
# fits PER_NAME_CAP). At most SECTOR_CAP names per sector, counting names
# already held. A name with no entry here is refused, never waved in.
SECTOR_CAP = 2
SECTORS = {
    "XOM": "energy", "KMI": "energy",
    "MRK": "health", "PFE": "health",
    "WMT": "staples", "KO": "staples", "PEP": "staples", "KVUE": "staples",
    "SCHW": "financials", "BAC": "financials", "C": "financials",
    "WFC": "financials", "PYPL": "financials",
    "CSCO": "tech", "ORCL": "tech", "INTC": "tech", "PLTR": "tech",
    "NFLX": "comms", "DIS": "comms", "T": "comms", "VZ": "comms",
    "UBER": "discretionary", "SBUX": "discretionary", "GM": "discretionary",
    "NKE": "discretionary",
    "BA": "industrials",
}

# DJ-20261001-01 (joincfu "close early vs let it run"): a wheel short put is
# bought back once PUT_PROFIT_TAKE of its credit is kept, or once PUT_TIME_EXIT
# of the entry-to-expiry span has passed, whichever comes first. Covered calls
# are left to run to expiry (the article's ~96%). Enforced in core.early_exit.
# Fixed until a review on 2026-12-31; the spy-spread sleeve keeps its own rules.
PUT_PROFIT_TAKE = 0.50
PUT_TIME_EXIT = 0.63

# DJ-20261001-03 (joincfu performance page: losers held 520-598 days): the
# wheel sells assigned shares once they have been held SHARE_HOLD_MAX_DAYS.
# No new covered call is written past the cap; shares still under a call wait
# for it to expire or be assigned, so the worst case is the cap plus one call
# (about 135 days). Enforced in scripts/run_strategy.py via core.trade_ledger.
SHARE_HOLD_MAX_DAYS = 90
