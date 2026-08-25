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

def filter_options(options, min_strike = 0):
    """
    Filter put options based on delta and open interest.
    """
    filtered_contracts = [contract for contract in options 
                          if contract.delta 
                          and abs(contract.delta) > DELTA_MIN 
                          and abs(contract.delta) < DELTA_MAX
                          and (contract.bid_price / contract.strike) * (365 / (contract.dte + 1)) > YIELD_MIN
                          and (contract.bid_price / contract.strike) * (365 / (contract.dte + 1)) < YIELD_MAX
                          and contract.oi
                          and contract.oi > OPEN_INTEREST_MIN
                          and spread_ok(contract)
                          and contract.strike >= min_strike]
    
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
