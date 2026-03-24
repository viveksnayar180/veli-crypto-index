"""
Backtest engine — walk-forward simulation with fees, rebalancing, and metrics.

Fee order per period:
  Day 0:    Entry fee deducted from initial investment before portfolio build
  Weekly:   AUM fee → Performance fee (applied to post-AUM value)
  Monthly/Quarterly/Weekly/Yearly: Rebalancing fee on traded notional
  Final day: Exit fee deducted from final portfolio value

P&L breakdown in return dict:
  gross_return_pct  = what the strategy generated before all fees
  net_return_pct    = what the investor keeps after all fees
  platform_pct      = platform's take as % of initial investment
"""

import numpy as np
import pandas as pd
from datetime import datetime
from typing import Dict, List

from .weights   import compute_weights_from_mcaps
from .fees      import FeeEngine
from .rebalance import compute_rebalance_trades


def run_backtest(
    coin_universe:      List[dict],
    price_histories:    Dict[str, pd.DataFrame],
    start_date:         datetime,
    end_date:           datetime,
    weighting_method:   str   = "market_cap",
    initial_investment: float = 1_000.0,
    cap:                float = 0.30,
    floor:              float = 0.001,
    rebalance_freq:     str   = "monthly",
    entry_fee:          float = 0.0,
    exit_fee:           float = 0.0,
    aum_fee:            float = 0.01,
    rebalancing_fee:    float = 0.003,
    performance_fee:    float = 0.15,
) -> dict:

    fee_engine = FeeEngine(
        entry_fee          = entry_fee,
        exit_fee           = exit_fee,
        aum_fee_weekly     = aum_fee / 52,
        rebalance_fee      = rebalancing_fee,
        perf_fee           = performance_fee,
        initial_investment = initial_investment,
    )

    # ── Build aligned price + mcap matrices ──────────────────────────────────
    coin_ids  = [c["id"] for c in coin_universe if c["id"] in price_histories]
    all_dates = pd.date_range(start=start_date, end=end_date, freq="D")

    price_matrix = pd.DataFrame(index=all_dates)
    mcap_matrix  = pd.DataFrame(index=all_dates)
    for cid in coin_ids:
        df = price_histories[cid].reindex(all_dates)
        price_matrix[cid] = df["price"].ffill().bfill()
        mcap_matrix[cid]  = df["market_cap"].ffill().bfill()

    price_matrix = price_matrix.dropna(axis=1, how="all")
    mcap_matrix  = mcap_matrix.dropna(axis=1, how="all")
    coin_ids     = list(price_matrix.columns)

    if not coin_ids:
        raise ValueError("No coins with valid price data for the selected date range.")

    # ── Entry fee — reduces effective investment before portfolio build ────────
    entry_fee_amount = fee_engine.apply_entry_fee(initial_investment)
    effective_investment = initial_investment - entry_fee_amount

    # ── Initial portfolio ─────────────────────────────────────────────────────
    start_prices = price_matrix.iloc[0]
    start_mcaps  = {k: v for k, v in mcap_matrix.iloc[0].to_dict().items()
                    if pd.notna(v) and v > 0}
    initial_weights = compute_weights_from_mcaps(
        coin_ids, start_mcaps, weighting_method, cap, floor)
    units_held = {
        cid: (effective_investment * initial_weights.get(cid, 0.0)) / start_prices[cid]
        for cid in coin_ids
        if start_prices.get(cid, 0) > 0
    }

    # ── BTC benchmark ─────────────────────────────────────────────────────────
    btc_prices = price_matrix.get("bitcoin")
    btc_start  = btc_prices.iloc[0] if btc_prices is not None else None
    btc_units  = (initial_investment / btc_start) if btc_start else 0.0

    # ── Walk-forward ──────────────────────────────────────────────────────────
    equity_curve    = []
    benchmark_curve = []
    weights_history = []
    fee_events      = []

    last_weekly_date      = start_date
    last_rebalance_date   = start_date
    last_rebalance_month  = start_date.month
    last_rebalance_quarter = (start_date.month - 1) // 3

    for date in all_dates:
        prices_today = price_matrix.loc[date]

        pv      = sum(units_held.get(cid, 0.0) * prices_today.get(cid, 0.0) for cid in coin_ids)
        btc_val = btc_units * prices_today.get("bitcoin", 0.0) if btc_units else 0.0

        equity_curve.append({"date": date.strftime("%Y-%m-%d"), "value": round(pv, 4)})
        benchmark_curve.append({"date": date.strftime("%Y-%m-%d"), "btc": round(btc_val, 4)})

        # ── Weekly fees ───────────────────────────────────────────────────────
        if (date - last_weekly_date).days >= 7:
            aum_fee_amount = fee_engine.apply_aum_fee(pv)
            if pv > 0 and aum_fee_amount > 0:
                scale = 1.0 - aum_fee_amount / pv
                units_held = {cid: u * scale for cid, u in units_held.items()}
                pv *= scale
                fee_events.append({"date": date.strftime("%Y-%m-%d"),
                                   "type": "aum", "amount": round(aum_fee_amount, 4)})

            perf_fee_amount = fee_engine.apply_performance_fee(pv)
            if pv > 0 and perf_fee_amount > 0:
                scale = 1.0 - perf_fee_amount / pv
                units_held = {cid: u * scale for cid, u in units_held.items()}
                pv *= scale
                fee_events.append({"date": date.strftime("%Y-%m-%d"),
                                   "type": "performance", "amount": round(perf_fee_amount, 4)})

            last_weekly_date = date

        # ── Rebalancing ───────────────────────────────────────────────────────
        should_rebalance = False
        if rebalance_freq == "monthly" and date.month != last_rebalance_month:
            should_rebalance = True
            last_rebalance_month = date.month
        elif rebalance_freq == "quarterly" and (date.month-1)//3 != last_rebalance_quarter:
            should_rebalance = True
            last_rebalance_quarter = (date.month-1)//3
        elif rebalance_freq == "weekly" and (date - last_rebalance_date).days >= 7:
            should_rebalance = True
            last_rebalance_date = date
        elif rebalance_freq == "yearly" and (date - last_rebalance_date).days >= 365:
            should_rebalance = True
            last_rebalance_date = date

        if should_rebalance:
            current_values = {
                cid: units_held.get(cid, 0.0) * prices_today.get(cid, 0.0)
                for cid in coin_ids
            }
            current_mcaps = {
                cid: mcap_matrix.loc[date, cid]
                for cid in coin_ids
                if cid in mcap_matrix.columns and pd.notna(mcap_matrix.loc[date, cid])
            }
            try:
                target_weights = compute_weights_from_mcaps(
                    coin_ids, current_mcaps, weighting_method, cap, floor)
            except ValueError:
                target_weights = initial_weights

            _, reb_amount = compute_rebalance_trades(current_values, target_weights)
            reb_fee = fee_engine.apply_rebalancing_fee(reb_amount)
            net_pv  = pv - reb_fee
            fee_events.append({"date": date.strftime("%Y-%m-%d"),
                               "type": "rebalancing", "amount": round(reb_fee, 4)})

            for cid in coin_ids:
                price = prices_today.get(cid, 0)
                if price > 0:
                    units_held[cid] = (net_pv * target_weights.get(cid, 0.0)) / price

        # ── Monthly weight snapshot ───────────────────────────────────────────
        if date.day == 1:
            total_pv = sum(units_held.get(cid, 0.0) * prices_today.get(cid, 0.0)
                          for cid in coin_ids)
            if total_pv > 0:
                weights_history.append({
                    "date": date.strftime("%Y-%m-%d"),
                    "weights": {
                        cid: round(units_held.get(cid,0)*prices_today.get(cid,0)/total_pv, 4)
                        for cid in coin_ids
                    }
                })

    # ── Exit fee — deducted from final portfolio value ────────────────────────
    final_pv = equity_curve[-1]["value"] if equity_curve else 0.0
    exit_fee_amount = fee_engine.apply_exit_fee(final_pv)
    if exit_fee_amount > 0 and final_pv > 0:
        equity_curve[-1]["value"] = round(final_pv - exit_fee_amount, 4)
        fee_events.append({
            "date": equity_curve[-1]["date"],
            "type": "exit",
            "amount": round(exit_fee_amount, 4),
        })

    # ── Metrics ───────────────────────────────────────────────────────────────
    values = pd.Series([e["value"] for e in equity_curve])
    metrics = _compute_metrics(values, initial_investment)

    btc_vals    = pd.Series([e["btc"] for e in benchmark_curve])
    btc_metrics = _compute_metrics(btc_vals, initial_investment) if btc_vals.sum() > 0 else {}

    # ── P&L breakdown (brief section 2.7) ────────────────────────────────────
    fee_summary  = fee_engine.get_summary()
    total_fees   = fee_summary["total"]
    final_value  = values.iloc[-1] if len(values) > 0 else 0.0
    # Gross approximation: add back all fees to final value
    gross_final  = final_value + total_fees
    pnl_breakdown = {
        "gross_return_pct":  round((gross_final / initial_investment - 1) * 100, 2),
        "net_return_pct":    round((final_value  / initial_investment - 1) * 100, 2),
        "platform_pct":      round((total_fees   / initial_investment) * 100, 2),
        "gross_dollars":     round(gross_final  - initial_investment, 2),
        "net_dollars":       round(final_value  - initial_investment, 2),
        "platform_dollars":  round(total_fees, 2),
    }

    return {
        "equity_curve":    equity_curve,
        "benchmark_curve": benchmark_curve,
        "weights_history": weights_history,
        "fee_events":      fee_events,
        "fee_summary":     fee_summary,
        "metrics":         metrics,
        "btc_metrics":     btc_metrics,
        "pnl_breakdown":   pnl_breakdown,
        "coin_count":      len(coin_ids),
        "high_watermark":  round(fee_engine.high_watermark, 2),
    }


def _compute_metrics(values: pd.Series, initial: float) -> dict:
    v = values.dropna()
    if len(v) < 2 or v.iloc[0] == 0:
        return {}
    daily_ret   = v.pct_change().dropna()
    n_days      = len(v)
    n_years     = n_days / 365.25
    total_ret   = (v.iloc[-1] / initial) - 1
    ann_ret     = (1 + total_ret) ** (1 / n_years) - 1 if n_years > 0 else 0
    running_max = v.cummax()
    drawdowns   = (v - running_max) / running_max
    max_dd      = float(drawdowns.min())
    ann_vol     = float(daily_ret.std() * np.sqrt(365))
    sharpe      = (ann_ret / ann_vol) if ann_vol > 0 else 0.0
    return {
        "total_return_pct":      round(total_ret * 100, 2),
        "annualised_return_pct": round(ann_ret * 100, 2),
        "max_drawdown_pct":      round(max_dd * 100, 2),
        "sharpe_ratio":          round(sharpe, 3),
        "annual_volatility_pct": round(ann_vol * 100, 2),
        "start_value":           round(float(initial), 2),
        "end_value":             round(float(v.iloc[-1]), 2),
        "n_days":                n_days,
    }
