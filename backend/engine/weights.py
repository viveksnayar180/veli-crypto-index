"""
Weighting engine — market cap weighted (30/30/40 rule) and equal weighted.

Market Cap rule:
  - Coins sorted by market cap descending
  - Rank 1: 30%  |  Rank 2: 30%  |  Ranks 3-N: proportional share of 40%
  - Edge cases: n=1 → 100%; n=2 → 50%/50%

Equal Weight rule:
  - Every coin gets 1/N (20% each for a 5-coin index)
"""

from typing import Dict, List


def compute_weights(
    coins: List[dict],
    method: str,
    cap: float = 0.30,    # retained for API compat, unused in market_cap
    floor: float = 0.001, # retained for API compat, unused in market_cap
) -> Dict[str, float]:
    valid = [c for c in coins if c.get("market_cap") and c["market_cap"] > 0]
    n = len(valid)
    if n == 0:
        raise ValueError("No coins with valid market cap data")

    if method == "equal":
        return {c["id"]: 1.0 / n for c in valid}

    if method == "market_cap":
        ranked = sorted(valid, key=lambda c: c["market_cap"], reverse=True)
        if n == 1:
            return {ranked[0]["id"]: 1.0}
        if n == 2:
            return {ranked[0]["id"]: 0.50, ranked[1]["id"]: 0.50}
        # Top 2 fixed at 30% each; remainder split proportionally
        weights = {ranked[0]["id"]: 0.30, ranked[1]["id"]: 0.30}
        rest = ranked[2:]
        rem_total = sum(c["market_cap"] for c in rest)
        for c in rest:
            weights[c["id"]] = (0.40 * c["market_cap"] / rem_total
                                if rem_total > 0 else 0.40 / len(rest))
        return weights

    raise ValueError(f"Unknown method: {method}")


def compute_weights_from_mcaps(
    coin_ids: List[str],
    market_caps: Dict[str, float],
    method: str,
    cap: float = 0.30,
    floor: float = 0.001,
) -> Dict[str, float]:
    """Variant used during backtesting where we have historical market cap data."""
    coins_proxy = [{"id": cid, "market_cap": market_caps.get(cid, 0)} for cid in coin_ids]
    return compute_weights(coins_proxy, method=method, cap=cap, floor=floor)
