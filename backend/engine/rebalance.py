"""Rebalancing engine — computes trades needed to restore target weights."""

from typing import Dict, Tuple


def compute_rebalance_trades(
    current_values: Dict[str, float],
    target_weights: Dict[str, float],
) -> Tuple[Dict[str, float], float]:
    """
    Returns (trades, rebalanced_amount).
    trades:            {coin_id: dollar_delta}  (+buy, -sell)
    rebalanced_amount: sum of abs(trades) — used for rebalancing fee

    Note: rebalanced_amount = both buy and sell sides combined, matching the
    brief's definition "charged on the amount being traded".
    """
    total = sum(current_values.values())
    all_ids = set(list(current_values) + list(target_weights))
    target_values = {cid: total * target_weights.get(cid, 0.0) for cid in all_ids}
    trades = {
        cid: target_values[cid] - current_values.get(cid, 0.0)
        for cid in all_ids
        if abs(target_values[cid] - current_values.get(cid, 0.0)) > 0.01
    }
    rebalanced_amount = sum(abs(v) for v in trades.values())
    return trades, rebalanced_amount
