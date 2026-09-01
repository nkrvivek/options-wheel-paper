from config.params import DELTA_MIN, DELTA_MAX, YIELD_MIN, YIELD_MAX, OPEN_INTEREST_MIN, SCORE_MIN, SPREAD_MAX_FRAC


def spread_ok(contract):
    """Spread ≤ SPREAD_MAX_FRAC of mark. A contract missing either quote fails:
    an unpriced spread is unknown, not tight."""
    if not contract.bid_price or not contract.ask_price:
        return False
    mark = (contract.bid_price + contract.ask_price) / 2
    if mark <= 0:
        return False
    return (contract.ask_price - contract.bid_price) / mark <= SPREAD_MAX_FRAC

def filter_underlying(client, symbols, buying_power_limit):
    """
    Filter underlying symbols based on buying power.  Can add custom logic such as volatility or ranging / support metrics.
    """
    resp = client.get_stock_latest_trade(symbols)

    filtered_symbols = [symbol for symbol in resp if 100*resp[symbol].price <= buying_power_limit]

    return filtered_symbols

def _first_failing_gate(contract, min_strike):
    """Name of the first gate this contract fails, or None if it passes.

    Gate order matches the original filter expression so the counts read the
    way the filter actually short-circuits."""
    if not contract.delta:
        return "no_delta"
    if not (DELTA_MIN < abs(contract.delta) < DELTA_MAX):
        return "delta_band"
    if not contract.bid_price:
        return "no_quote"
    annualized_yield = (contract.bid_price / contract.strike) * (365 / (contract.dte + 1))
    if not (YIELD_MIN < annualized_yield < YIELD_MAX):
        return "yield_band"
    if not contract.oi or contract.oi <= OPEN_INTEREST_MIN:
        return "low_oi"
    if not spread_ok(contract):
        return "wide_spread"
    if contract.strike < min_strike:
        return "below_min_strike"
    return None


def filter_options(options, min_strike = 0, rejections = None):
    """
    Filter put options based on delta and open interest.

    When `rejections` (a dict) is passed, each rejected contract counts its
    first failing gate into it — the wheel's answer to "why did nothing
    sell today", which for five sessions had no recorded answer at all.
    """
    filtered_contracts = []
    for contract in options:
        reason = _first_failing_gate(contract, min_strike)
        if reason is None:
            filtered_contracts.append(contract)
        elif rejections is not None:
            rejections[reason] = rejections.get(reason, 0) + 1
    return filtered_contracts

def score_options(options):
    """
    Score options based on delta, days to expiration, and bid price.  
    The score is the annualized rate of return on selling the contract, discounted by the probability of assignment.
    """
    scores = [(1 - abs(p.delta)) * (250 / (p.dte + 5)) * (p.bid_price / p.strike) for p in options]
    return scores

def select_options(options, scores, n=None):
    """
    Select the top n options, keeping only the highest-scoring option per underlying symbol.
    """
    # Filter out low scores
    filtered = [(option, score) for option, score in zip(options, scores) if score > SCORE_MIN]

    # Pick the best option per underlying
    best_per_underlying = {}
    for option, score in filtered:
        underlying = option.underlying
        if (underlying not in best_per_underlying) or (score > best_per_underlying[underlying][1]):
            best_per_underlying[underlying] = (option, score)

    # Sort the best options by score
    sorted_best = sorted(best_per_underlying.values(), key=lambda x: x[1], reverse=True)

    # Return top n (or all if n not specified)
    return [option for option, _ in sorted_best[:n]] if n else [option for option, _ in sorted_best]
