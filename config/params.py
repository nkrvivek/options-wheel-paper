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
