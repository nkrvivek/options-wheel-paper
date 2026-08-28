# spy-spread sleeve — preregistered numbers (DJ-20260827-01, trade-refresh
# strategy-prereg.json `spy-spread`, registered 2026-08-27 BEFORE any backtest).
#
# These live in their own file on purpose: the wheel's params.py can move with
# the wheel, and nothing that moves there may move a preregistered number here.
# Changing any value below before the 2026-11-30 review is a prereg violation,
# not a tune.

UNDERLYING = "SPY"

# Sleeve capital inside the shared 100k paper account.
SLEEVE_NOTIONAL = 25_000

# Short leg: 12-16 delta put, target 14.
SHORT_DELTA_MIN = 0.12
SHORT_DELTA_MAX = 0.16
SHORT_DELTA_TARGET = 0.14

# $5-wide put credit spread.
WIDTH = 5

# The sleeve's own DTE window (numerically equal to the wheel's today, but
# owned here — see file header).
DTE_MIN = 30
DTE_MAX = 45

# Minimum net credit at mid for the pair.
MIN_CREDIT = 0.55

# Entry regime band: no entry when VIX is outside [12, 28]. The upper edge
# coheres with the autopilot mirror's defensive floor at 28.
VIX_MIN = 12
VIX_MAX = 28

# Risk per position (width - credit) * 100 * qty capped at 5% of the sleeve.
MAX_RISK_FRAC = 0.05

# At most 3 concurrent expiries, at most one new position per week.
MAX_EXPIRIES = 3
ENTRY_COOLDOWN_DAYS = 7

# Exits: take profit at 50% of credit, stop at 2x credit, hard close at 7 DTE.
TP_FRAC = 0.50
STOP_MULT = 2.0
CLOSE_DTE = 7

# Kill: -10% sleeve drawdown or 3 logged breaches ends the sleeve.
KILL_DRAWDOWN_FRAC = 0.10
KILL_BREACHES = 3
