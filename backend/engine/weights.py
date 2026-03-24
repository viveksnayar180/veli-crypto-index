"""Weighting engine — market cap weighted and equal weighted with iterative cap/floor."""

from typing import Dict, List


def compute_weights(
    coins: List[dict],
    method: str,
    cap: float = 0.30,
    floor: float = 0.001,
) -> Dict[str, float]:
    """
    Compute portfolio weights for a list of coins.

    Cap/floor enforcement is ITERATIVE — a single pass is wrong because
    redistributing excess from a capped coin can push another over the cap.

    Auto-adjustments (silent, surface via app.py warnings):
      - Floor too high for coin count: reduced to 0.9/n
      - Cap below equal-weight:        raised to 1/n + epsilon
    """
    valid = [c for c in coins if c.get("market_cap") and c["market_cap"] > 0]
    n = len(valid)
    if n == 0:
        raise ValueError("No coins with valid market cap data")

    if floor * n > 1.0:
        floor = 0.9 / n

    min_viable_cap = 1.0 / n
    if cap < min_viable_cap:
        cap = min_viable_cap + 0.001

    if method == "market_cap":
        total = sum(c["market_cap"] for c in valid)
        weights = {c["id"]: c["market_cap"] / total for c in valid}
    elif method == "equal":
        weights = {c["id"]: 1.0 / n for c in valid}
    else:
        raise ValueError(f"Unknown method: {method}")

    # Iterative cap/floor enforcement
    for _ in range(200):
        changed = False
        floor_deficit = sum(max(0, floor - w) for w in weights.values())
        if floor_deficit > 0:
            for cid in list(weights):
                if weights[cid] < floor:
                    weights[cid] = floor
                    changed = True
            above = {cid: w for cid, w in weights.items() if w > floor}
            total_above = sum(above.values())
            if total_above > 0:
                for cid in above:
                    weights[cid] -= floor_deficit * (above[cid] / total_above)

        cap_excess = sum(max(0, w - cap) for w in weights.values())
        if cap_excess > 0:
            for cid in list(weights):
                if weights[cid] > cap:
                    weights[cid] = cap
                    changed = True
            below = {cid: w for cid, w in weights.items() if w < cap}
            total_below = sum(below.values())
            if total_below > 0:
                for cid in below:
                    weights[cid] += cap_excess * (below[cid] / total_below)

        if not changed:
            break

    total = sum(weights.values())
    return {cid: w / total for cid, w in weights.items()}


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
