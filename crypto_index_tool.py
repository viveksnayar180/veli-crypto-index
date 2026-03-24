"""
=============================================================================
  VELI — Crypto Direct Indexing Tool  |  MVP Python Script  |  v1.0
=============================================================================
  Developer Brief v1.0 by Stevan Radonjanin + call spec (Vlada / Vivek, 20 Mar 2026)

  6 Sections:
    1. Data Fetcher      — CoinGecko API with local caching
    2. Universe Selector — Top 300 or Top 500 by market cap
    3. Weighting Engine  — Market Cap Weighted or Equal Weighted (+ cap/floor)
    4. Rebalancing Engine — Monthly rebalancing
    5. Fee Engine        — AUM (1%/yr weekly) + Rebalancing (0.3%) + Performance (15% WoW)
    6. Backtesting Engine — Date-to-date simulation, metrics + equity curve chart

  NOTE on Performance Fee: Implemented as week-over-week gain per call spec
  (Vlada/Vivek March 20). The Developer Brief specifies high-watermark — flag
  with Stevan before moving to production.

  NOTE on Survivorship Bias: Universe is today's top N coins applied to
  historical data. Acceptable for MVP/content generation. Not suitable for
  rigorous academic backtesting.
=============================================================================
"""

import os
import json
import time
import math
import requests
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple


# =============================================================================
# CONFIGURATION
# =============================================================================

COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "")  # Set via env var
COINGECKO_BASE    = "https://api.coingecko.com/api/v3"
CACHE_DIR         = os.path.join(os.path.dirname(__file__), ".cache")
RATE_LIMIT_SLEEP  = 1.2   # seconds between API calls (conservative for pro tier)
MAX_RETRIES       = 3

# Fee constants (from call spec)
AUM_FEE_ANNUAL        = 0.01    # 1% per year
AUM_FEE_WEEKLY        = AUM_FEE_ANNUAL / 52
REBALANCING_FEE_RATE  = 0.003   # 0.3% on rebalanced amount
PERFORMANCE_FEE_RATE  = 0.15    # 15% on week-over-week gain

os.makedirs(CACHE_DIR, exist_ok=True)


# =============================================================================
# SECTION 1 — DATA FETCHER
# =============================================================================

class CoinGeckoFetcher:
    """
    Fetches and caches data from CoinGecko API.
    Implements aggressive local caching to respect rate limits and
    avoid redundant API calls on subsequent runs.
    """

    def __init__(self, api_key: str = COINGECKO_API_KEY):
        self.api_key = api_key
        self.session = requests.Session()
        if api_key:
            self.session.headers.update({"x-cg-pro-api-key": api_key})

    def _cache_path(self, key: str) -> str:
        safe_key = key.replace("/", "_").replace("?", "_").replace("&", "_")
        return os.path.join(CACHE_DIR, f"{safe_key}.json")

    def _load_cache(self, key: str, max_age_hours: float = 24) -> Optional[dict]:
        path = self._cache_path(key)
        if not os.path.exists(path):
            return None
        age = (time.time() - os.path.getmtime(path)) / 3600
        if age > max_age_hours:
            return None
        with open(path, "r") as f:
            return json.load(f)

    def _save_cache(self, key: str, data) -> None:
        with open(self._cache_path(key), "w") as f:
            json.dump(data, f)

    def _get(self, endpoint: str, params: dict = None, cache_key: str = None,
             cache_hours: float = 24) -> dict:
        """Make a GET request with retry, rate limiting, and caching."""
        if cache_key:
            cached = self._load_cache(cache_key, cache_hours)
            if cached is not None:
                return cached

        url = f"{COINGECKO_BASE}{endpoint}"
        for attempt in range(MAX_RETRIES):
            try:
                time.sleep(RATE_LIMIT_SLEEP)
                resp = self.session.get(url, params=params, timeout=30)
                if resp.status_code == 429:
                    wait = 60 * (attempt + 1)
                    print(f"  [rate limit] waiting {wait}s before retry...")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if cache_key:
                    self._save_cache(cache_key, data)
                return data
            except requests.RequestException as e:
                if attempt == MAX_RETRIES - 1:
                    raise RuntimeError(f"API call failed after {MAX_RETRIES} attempts: {e}")
                time.sleep(5 * (attempt + 1))

    def fetch_top_coins(self, n: int = 500) -> List[dict]:
        """
        Fetch the top N coins by market cap with metadata.
        Returns list of dicts with id, symbol, name, market_cap, etc.
        CoinGecko /coins/markets max page size = 250, so we paginate.
        """
        cache_key = f"top_coins_{n}"
        cached = self._load_cache(cache_key, max_age_hours=12)
        if cached:
            print(f"  [cache] loaded top {n} coins from cache")
            return cached

        print(f"  [api] fetching top {n} coins by market cap...")
        coins = []
        per_page = 250
        pages = math.ceil(n / per_page)

        for page in range(1, pages + 1):
            data = self._get(
                "/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": per_page,
                    "page": page,
                    "sparkline": False,
                    "price_change_percentage": "24h",
                },
                cache_key=None,  # handled by outer cache
            )
            coins.extend(data)
            print(f"    page {page}/{pages}: fetched {len(data)} coins")

        coins = coins[:n]
        self._save_cache(cache_key, coins)
        print(f"  [done] {len(coins)} coins fetched and cached")
        return coins

    def fetch_coin_history(
        self,
        coin_id: str,
        start_date: datetime,
        end_date: datetime,
    ) -> pd.DataFrame:
        """
        Fetch daily OHLCV + market cap for a single coin over a date range.
        Returns DataFrame with columns: date, price, market_cap, volume.
        Uses /coins/{id}/market_chart/range endpoint.
        """
        from_ts = int(start_date.timestamp())
        to_ts   = int(end_date.timestamp())
        cache_key = f"history_{coin_id}_{from_ts}_{to_ts}"

        raw = self._load_cache(cache_key, max_age_hours=23)
        if raw is None:
            raw = self._get(
                f"/coins/{coin_id}/market_chart/range",
                params={"vs_currency": "usd", "from": from_ts, "to": to_ts},
                cache_key=cache_key,
                cache_hours=23,
            )

        if not raw or "prices" not in raw or len(raw["prices"]) == 0:
            return pd.DataFrame(columns=["date", "price", "market_cap", "volume"])

        prices     = pd.DataFrame(raw["prices"],      columns=["ts", "price"])
        mkt_caps   = pd.DataFrame(raw["market_caps"], columns=["ts", "market_cap"])
        volumes    = pd.DataFrame(raw["total_volumes"], columns=["ts", "volume"])

        df = prices.merge(mkt_caps, on="ts").merge(volumes, on="ts")
        df["date"] = pd.to_datetime(df["ts"], unit="ms").dt.normalize()
        df = df.drop(columns=["ts"]).drop_duplicates("date").sort_values("date")
        df = df.set_index("date")
        return df

    def fetch_universe_history(
        self,
        coins: List[dict],
        start_date: datetime,
        end_date: datetime,
    ) -> Dict[str, pd.DataFrame]:
        """
        Fetch price history for all coins in the universe.
        Returns dict: {coin_id: DataFrame}.
        Skips coins with insufficient data and reports how many were dropped.
        """
        histories = {}
        total = len(coins)
        print(f"\n  [data] fetching price history for {total} coins "
              f"({start_date.date()} → {end_date.date()})...")

        for i, coin in enumerate(coins, 1):
            cid = coin["id"]
            try:
                df = self.fetch_coin_history(cid, start_date, end_date)
                if len(df) < 10:  # skip coins with almost no data
                    continue
                histories[cid] = df
                if i % 25 == 0:
                    print(f"    {i}/{total} done ({len(histories)} with data)")
            except Exception as e:
                print(f"    [skip] {cid}: {e}")

        print(f"  [done] {len(histories)}/{total} coins have sufficient history")
        return histories


# =============================================================================
# SECTION 2 — UNIVERSE SELECTOR
# =============================================================================

def select_universe(
    all_coins: List[dict],
    size: int,
    exclude_stablecoins: bool = True,
) -> List[dict]:
    """
    Select the top N coins by market cap from the master universe.

    Args:
        all_coins:          Full list of coins from CoinGecko (sorted by market cap)
        size:               300 or 500 (will become a dropdown in the app)
        exclude_stablecoins: Remove known stablecoins (USDT, USDC, etc.)

    Returns:
        List of coin dicts, truncated to `size`
    """
    STABLECOINS = {
        "tether", "usd-coin", "binance-usd", "dai", "true-usd",
        "frax", "usdd", "paxos-standard", "gemini-dollar", "fei-usd",
        "nusd", "liquity-usd", "euro-coin", "palladium-coin", "first-digital-usd",
        "paypal-usd", "usde", "ethena-usde",
    }

    filtered = all_coins
    if exclude_stablecoins:
        filtered = [c for c in filtered if c["id"] not in STABLECOINS]

    selected = filtered[:size]
    print(f"\n[Universe] Selected top {len(selected)} coins "
          f"(stablecoins excluded: {exclude_stablecoins})")
    print(f"  Top 5: {', '.join(c['symbol'].upper() for c in selected[:5])}")
    return selected


# =============================================================================
# SECTION 3 — WEIGHTING ENGINE
# =============================================================================

def compute_weights(
    coins: List[dict],
    method: str,
    cap: float = 0.30,
    floor: float = 0.001,
) -> Dict[str, float]:
    """
    Compute portfolio weights for a list of coins.

    Args:
        coins:   List of coin dicts (must have 'id' and 'market_cap')
        method:  'market_cap' or 'equal'
        cap:     Max weight per coin (e.g. 0.30 = 30%). Default 30%.
        floor:   Min weight per coin (e.g. 0.001 = 0.1%). Default 0.1%.

    Returns:
        Dict {coin_id: weight} where weights sum to ~1.0

    Note:
        Cap/floor redistribution is ITERATIVE — a single pass is wrong
        because capping one coin and redistributing can push another over the cap.
    """
    valid_coins = [c for c in coins if c.get("market_cap") and c["market_cap"] > 0]
    n = len(valid_coins)

    if n == 0:
        raise ValueError("No coins with valid market cap data")

    # Validate floor/cap feasibility
    if floor * n > 1.0:
        raise ValueError(
            f"Floor {floor:.1%} × {n} coins = {floor*n:.1%} > 100%. "
            "Reduce floor or use fewer coins."
        )
    if cap < 1.0 / n:
        raise ValueError(
            f"Cap {cap:.1%} is below equal weight {1/n:.1%}. Increase cap."
        )

    # --- Initial weights ---
    if method == "market_cap":
        total_mcap = sum(c["market_cap"] for c in valid_coins)
        weights = {c["id"]: c["market_cap"] / total_mcap for c in valid_coins}
    elif method == "equal":
        weights = {c["id"]: 1.0 / n for c in valid_coins}
    else:
        raise ValueError(f"Unknown weighting method: '{method}'. Use 'market_cap' or 'equal'")

    # --- Iterative cap/floor enforcement ---
    # This must iterate because redistributing excess from a capped coin
    # can push another coin above the cap.
    max_iterations = 200
    for iteration in range(max_iterations):
        changed = False

        # Apply floor — coins below floor get bumped up, deficit taken from top coins
        floor_deficit = 0.0
        for cid in list(weights):
            if weights[cid] < floor:
                floor_deficit += floor - weights[cid]
                weights[cid] = floor
                changed = True

        # Take floor_deficit from coins above floor proportionally
        if floor_deficit > 0:
            above_floor = {cid: w for cid, w in weights.items() if w > floor}
            total_above = sum(above_floor.values())
            if total_above > 0:
                for cid in above_floor:
                    weights[cid] -= floor_deficit * (above_floor[cid] / total_above)

        # Apply cap — coins above cap get trimmed, excess redistributed
        cap_excess = 0.0
        for cid in list(weights):
            if weights[cid] > cap:
                cap_excess += weights[cid] - cap
                weights[cid] = cap
                changed = True

        # Redistribute cap_excess to coins below cap proportionally
        if cap_excess > 0:
            below_cap = {cid: w for cid, w in weights.items() if w < cap}
            total_below = sum(below_cap.values())
            if total_below > 0:
                for cid in below_cap:
                    weights[cid] += cap_excess * (below_cap[cid] / total_below)

        if not changed:
            break
    else:
        print(f"  [warning] Cap/floor did not fully converge after {max_iterations} iterations")

    # Normalise to ensure exact sum = 1.0
    total = sum(weights.values())
    weights = {cid: w / total for cid, w in weights.items()}

    return weights


def compute_weights_from_prices(
    coin_ids: List[str],
    market_caps: Dict[str, float],
    method: str,
    cap: float = 0.30,
    floor: float = 0.001,
) -> Dict[str, float]:
    """
    Variant used during backtesting where we have historical market cap data
    rather than a coins-list-of-dicts.
    """
    coins_proxy = [
        {"id": cid, "market_cap": market_caps.get(cid, 0)}
        for cid in coin_ids
    ]
    return compute_weights(coins_proxy, method=method, cap=cap, floor=floor)


# =============================================================================
# SECTION 4 — REBALANCING ENGINE
# =============================================================================

def compute_rebalance_trades(
    current_values: Dict[str, float],
    target_weights: Dict[str, float],
) -> Tuple[Dict[str, float], float]:
    """
    Compute the trades needed to rebalance a portfolio to target weights.

    Args:
        current_values:  {coin_id: current_dollar_value}
        target_weights:  {coin_id: target_weight}

    Returns:
        trades:            {coin_id: dollar_amount (+buy, -sell)}
        rebalanced_amount: sum of absolute trade values (for fee calculation)

    The rebalanced_amount is the total turnover (buys + sells) which is what
    the 0.3% rebalancing fee is charged on (per Vlada's call spec).
    """
    total_portfolio_value = sum(current_values.values())
    target_values = {
        cid: total_portfolio_value * target_weights.get(cid, 0.0)
        for cid in set(list(current_values.keys()) + list(target_weights.keys()))
    }

    trades = {}
    for cid in target_values:
        current = current_values.get(cid, 0.0)
        target  = target_values[cid]
        delta   = target - current
        if abs(delta) > 0.01:  # ignore dust trades (< $0.01)
            trades[cid] = delta

    rebalanced_amount = sum(abs(v) for v in trades.values())
    return trades, rebalanced_amount


# =============================================================================
# SECTION 5 — FEE ENGINE
# =============================================================================

class FeeEngine:
    """
    Calculates and deducts three types of fees from the portfolio.
    All fees reduce actual portfolio value (units held), not just estimates.

    Fee types:
      1. AUM fee:          1% p.a., charged weekly (1%/52 per week)
      2. Rebalancing fee:  0.3% on the total rebalanced amount (monthly)
      3. Performance fee:  15% on gains above the high-watermark (HWM)

    High-Watermark logic (confirmed with team):
      - Track the all-time high portfolio value (the watermark)
      - On each weekly check, if current value > HWM:
          * Charge 15% ONLY on the amount above the previous HWM
          * Set the new HWM to current value AFTER fee deduction
            (investor keeps the post-fee value as the new benchmark)
      - If portfolio is below or at HWM: zero performance fee
      - Portfolio must fully recover AND break the previous HWM
        before any further performance fees are charged
    """

    def __init__(
        self,
        aum_fee_weekly:     float = AUM_FEE_WEEKLY,
        rebalance_fee:      float = REBALANCING_FEE_RATE,
        perf_fee:           float = PERFORMANCE_FEE_RATE,
        initial_investment: float = 0.0,
    ):
        self.aum_fee_weekly = aum_fee_weekly
        self.rebalance_fee  = rebalance_fee
        self.perf_fee       = perf_fee
        # HWM starts at initial investment — must beat this to owe any perf fee
        self.high_watermark = initial_investment
        self.total_fees_paid = {
            "aum": 0.0,
            "rebalancing": 0.0,
            "performance": 0.0,
        }

    def apply_aum_fee(self, portfolio_value: float) -> float:
        """Deduct weekly AUM fee (1% p.a. / 52). Returns fee amount in dollars."""
        fee = portfolio_value * self.aum_fee_weekly
        self.total_fees_paid["aum"] += fee
        return fee

    def apply_rebalancing_fee(self, rebalanced_amount: float) -> float:
        """Deduct 0.3% on total traded amount. Returns fee amount in dollars."""
        fee = rebalanced_amount * self.rebalance_fee
        self.total_fees_paid["rebalancing"] += fee
        return fee

    def apply_performance_fee(self, current_value: float) -> float:
        """
        High-watermark performance fee — 15% on gains above previous ATH.

        - No fee if current_value <= high_watermark
        - If current_value > high_watermark:
            fee = 15% * (current_value - high_watermark)
            new HWM = current_value - fee  (what investor actually keeps)
        - Must break the new watermark again before next fee triggers

        Returns fee amount in dollars (0.0 if at or below watermark).
        """
        if current_value <= self.high_watermark:
            return 0.0

        gain_above_hwm = current_value - self.high_watermark
        fee = gain_above_hwm * self.perf_fee

        # New HWM is post-fee value — investor must beat THIS to incur next fee
        self.high_watermark = current_value - fee
        self.total_fees_paid["performance"] += fee
        return fee

    def get_summary(self) -> dict:
        total = sum(self.total_fees_paid.values())
        return {**self.total_fees_paid, "total": total}

    def get_high_watermark(self) -> float:
        return self.high_watermark


# =============================================================================
# SECTION 6 — BACKTESTING ENGINE
# =============================================================================

def run_backtest(
    coin_universe:    List[dict],
    price_histories:  Dict[str, pd.DataFrame],
    start_date:       datetime,
    end_date:         datetime,
    weighting_method: str    = "market_cap",
    initial_investment: float = 1_000.0,
    cap:              float   = 0.30,
    floor:            float   = 0.001,
    verbose:          bool    = True,
) -> dict:
    """
    Walk-forward backtesting simulation.

    Timeline rules:
      - Daily:   update portfolio value based on price changes
      - Weekly:  apply AUM fee + performance fee (every 7 calendar days)
      - Monthly: rebalance portfolio + apply rebalancing fee (end of each month)

    Returns a results dict with equity curve, metrics, fee summary, and
    a benchmark (BTC buy-and-hold) series.
    """
    fee_engine = FeeEngine(initial_investment=initial_investment)

    # --- Build price matrix ---
    # Align all coins to same date index, forward-fill missing values
    coin_ids    = [c["id"] for c in coin_universe if c["id"] in price_histories]
    all_dates   = pd.date_range(start=start_date, end=end_date, freq="D")

    price_matrix = pd.DataFrame(index=all_dates)
    mcap_matrix  = pd.DataFrame(index=all_dates)

    for cid in coin_ids:
        df = price_histories[cid]
        df_reindexed = df.reindex(all_dates)
        price_matrix[cid] = df_reindexed["price"].ffill().bfill()
        mcap_matrix[cid]  = df_reindexed["market_cap"].ffill().bfill()

    # Drop coins with all-NaN prices
    price_matrix = price_matrix.dropna(axis=1, how="all")
    mcap_matrix  = mcap_matrix.dropna(axis=1, how="all")
    coin_ids     = list(price_matrix.columns)

    if len(coin_ids) == 0:
        raise ValueError("No coins with valid price data for the selected date range.")

    if verbose:
        print(f"\n[Backtest] {len(coin_ids)} coins | "
              f"{start_date.date()} → {end_date.date()} | "
              f"Method: {weighting_method.replace('_', ' ').title()}")

    # --- BTC benchmark setup ---
    btc_prices = price_matrix.get("bitcoin")
    if btc_prices is not None:
        btc_start_price = btc_prices.iloc[0]
        btc_units       = initial_investment / btc_start_price
    else:
        btc_units = 0.0
        print("  [warning] BTC price not available — benchmark will be skipped")

    # --- Initial portfolio setup ---
    start_prices = price_matrix.iloc[0]
    start_mcaps  = mcap_matrix.iloc[0].to_dict()
    start_mcaps  = {k: v for k, v in start_mcaps.items() if pd.notna(v) and v > 0}

    initial_weights = compute_weights_from_prices(
        coin_ids=coin_ids,
        market_caps=start_mcaps,
        method=weighting_method,
        cap=cap,
        floor=floor,
    )

    # Portfolio: track units held per coin
    units_held: Dict[str, float] = {}
    for cid in coin_ids:
        price = start_prices.get(cid)
        if price and price > 0:
            alloc = initial_investment * initial_weights.get(cid, 0.0)
            units_held[cid] = alloc / price
        else:
            units_held[cid] = 0.0

    # --- Walk-forward simulation ---
    equity_curve  = []           # (date, portfolio_value)
    benchmark_curve = []         # (date, btc_value)
    fee_log       = []           # (date, fee_type, amount)
    rebalance_log = []           # (date, rebalanced_amount)

    last_rebalance_month = start_date.month
    last_weekly_date     = start_date

    for date in all_dates:
        prices_today = price_matrix.loc[date]

        # --- Daily portfolio value ---
        portfolio_value = sum(
            units_held.get(cid, 0.0) * prices_today.get(cid, 0.0)
            for cid in coin_ids
        )

        # --- BTC benchmark value ---
        btc_value = btc_units * prices_today.get("bitcoin", 0.0) if btc_units > 0 else 0.0

        equity_curve.append((date, portfolio_value))
        benchmark_curve.append((date, btc_value))

        # --- Weekly fee processing (every 7 calendar days) ---
        days_since_weekly = (date - last_weekly_date).days
        if days_since_weekly >= 7:
            # AUM fee — deduct proportionally from all holdings
            aum_fee = fee_engine.apply_aum_fee(portfolio_value)
            if portfolio_value > 0:
                scale = 1.0 - (aum_fee / portfolio_value)
                units_held = {cid: u * scale for cid, u in units_held.items()}
                portfolio_value *= scale
                fee_log.append((date, "aum", aum_fee))

            # Performance fee — high-watermark (only charges on new ATH above HWM)
            perf_fee = fee_engine.apply_performance_fee(portfolio_value)
            if perf_fee > 0 and portfolio_value > 0:
                scale = 1.0 - (perf_fee / portfolio_value)
                units_held = {cid: u * scale for cid, u in units_held.items()}
                portfolio_value *= scale
                fee_log.append((date, "performance", perf_fee))

            last_weekly_date  = date

        # --- Monthly rebalancing (end of each calendar month) ---
        if date.month != last_rebalance_month:
            if verbose:
                print(f"  [rebalance] {date.date()} — rebalancing portfolio...")

            # Current values for each coin
            current_values = {
                cid: units_held.get(cid, 0.0) * prices_today.get(cid, 0.0)
                for cid in coin_ids
            }

            # Target weights using historical market caps
            current_mcaps = {
                cid: mcap_matrix.loc[date, cid]
                for cid in coin_ids
                if cid in mcap_matrix.columns and pd.notna(mcap_matrix.loc[date, cid])
            }

            try:
                target_weights = compute_weights_from_prices(
                    coin_ids=coin_ids,
                    market_caps=current_mcaps,
                    method=weighting_method,
                    cap=cap,
                    floor=floor,
                )
            except ValueError:
                target_weights = initial_weights  # fallback

            # Compute trades + rebalancing fee
            trades, rebalanced_amount = compute_rebalance_trades(
                current_values, target_weights
            )

            reb_fee = fee_engine.apply_rebalancing_fee(rebalanced_amount)
            fee_log.append((date, "rebalancing", reb_fee))
            rebalance_log.append((date, rebalanced_amount))

            # Update units held to reflect new weights (after fees)
            total_value_after_fee = portfolio_value - reb_fee
            for cid in coin_ids:
                price = prices_today.get(cid)
                if price and price > 0:
                    alloc = total_value_after_fee * target_weights.get(cid, 0.0)
                    units_held[cid] = alloc / price
                else:
                    units_held[cid] = 0.0

            last_rebalance_month = date.month

    # --- Build DataFrames from results ---
    equity_df    = pd.DataFrame(equity_curve, columns=["date", "value"]).set_index("date")
    benchmark_df = pd.DataFrame(benchmark_curve, columns=["date", "btc_value"]).set_index("date")

    # --- Metrics calculation ---
    metrics = compute_metrics(equity_df["value"], initial_investment)

    # --- Fee summary ---
    fee_summary = fee_engine.get_summary()

    if verbose:
        _print_results(metrics, fee_summary, initial_investment, equity_df["value"].iloc[-1])

    return {
        "equity_curve":    equity_df,
        "benchmark_curve": benchmark_df,
        "metrics":         metrics,
        "fee_summary":     fee_summary,
        "rebalance_log":   rebalance_log,
        "fee_log":         fee_log,
        "coin_ids":        coin_ids,
    }


def compute_metrics(equity_series: pd.Series, initial_investment: float) -> dict:
    """
    Compute standard performance metrics from an equity curve.

    Metrics:
      - Total return %
      - Annualised return % (CAGR)
      - Max drawdown %
      - Sharpe ratio (risk-free rate = 0, annualised)
      - Annualised volatility %
    """
    values = equity_series.dropna()
    if len(values) < 2:
        return {}

    daily_returns = values.pct_change().dropna()
    n_days        = (values.index[-1] - values.index[0]).days
    n_years       = n_days / 365.25

    # Total return
    total_return = (values.iloc[-1] / initial_investment) - 1

    # Annualised return (CAGR)
    if n_years > 0:
        annualised_return = (1 + total_return) ** (1 / n_years) - 1
    else:
        annualised_return = 0.0

    # Max drawdown
    running_max = values.cummax()
    drawdowns   = (values - running_max) / running_max
    max_drawdown = drawdowns.min()

    # Volatility (annualised)
    daily_vol     = daily_returns.std()
    annual_vol    = daily_vol * np.sqrt(365)

    # Sharpe ratio (risk-free = 0 for crypto)
    sharpe = (annualised_return / annual_vol) if annual_vol > 0 else 0.0

    return {
        "total_return_pct":      round(total_return * 100, 2),
        "annualised_return_pct": round(annualised_return * 100, 2),
        "max_drawdown_pct":      round(max_drawdown * 100, 2),
        "sharpe_ratio":          round(sharpe, 3),
        "annual_volatility_pct": round(annual_vol * 100, 2),
        "start_value":           round(initial_investment, 2),
        "end_value":             round(values.iloc[-1], 2),
        "n_days":                n_days,
    }


def _print_results(metrics: dict, fee_summary: dict, initial: float, final: float) -> None:
    """Print a clean results summary to console."""
    print("\n" + "=" * 60)
    print("  BACKTEST RESULTS")
    print("=" * 60)
    print(f"  Initial investment:    ${initial:>12,.2f}")
    print(f"  Final portfolio value: ${final:>12,.2f}")
    print("-" * 60)
    print(f"  Total return:          {metrics.get('total_return_pct', 0):>11.2f}%")
    print(f"  Annualised return:     {metrics.get('annualised_return_pct', 0):>11.2f}%")
    print(f"  Max drawdown:          {metrics.get('max_drawdown_pct', 0):>11.2f}%")
    print(f"  Sharpe ratio:          {metrics.get('sharpe_ratio', 0):>12.3f}")
    print(f"  Annual volatility:     {metrics.get('annual_volatility_pct', 0):>11.2f}%")
    print("-" * 60)
    print(f"  Total fees paid:       ${fee_summary.get('total', 0):>12,.2f}")
    print(f"    AUM fees:            ${fee_summary.get('aum', 0):>12,.2f}")
    print(f"    Rebalancing fees:    ${fee_summary.get('rebalancing', 0):>12,.2f}")
    print(f"    Performance fees:    ${fee_summary.get('performance', 0):>12,.2f}")
    print("=" * 60)


def plot_results(
    results: dict,
    title: str = "Veli — Crypto Index Backtest",
    save_path: str = "backtest_results.png",
) -> None:
    """
    Plot equity curve vs BTC benchmark.
    Saves to PNG and attempts to display inline.
    """
    eq   = results["equity_curve"]
    bm   = results["benchmark_curve"]
    m    = results["metrics"]
    fees = results["fee_summary"]

    fig, axes = plt.subplots(2, 1, figsize=(14, 9),
                             gridspec_kw={"height_ratios": [3, 1]})
    fig.patch.set_facecolor("#0d0d0d")
    for ax in axes:
        ax.set_facecolor("#111111")
        ax.tick_params(colors="#aaaaaa")
        ax.spines[:].set_color("#333333")
        ax.yaxis.label.set_color("#aaaaaa")
        ax.xaxis.label.set_color("#aaaaaa")
        ax.title.set_color("#ffffff")

    # --- Top panel: equity curve vs benchmark ---
    ax1 = axes[0]
    ax1.plot(eq.index, eq["value"],    color="#00d4ff", linewidth=1.8,
             label=f"Strategy  (+{m.get('total_return_pct', 0):.1f}%)")
    if bm["btc_value"].sum() > 0:
        btc_total = (bm["btc_value"].iloc[-1] / bm["btc_value"].iloc[0] - 1) * 100
        ax1.plot(bm.index, bm["btc_value"], color="#f7931a", linewidth=1.4,
                 linestyle="--", label=f"BTC Benchmark  (+{btc_total:.1f}%)")

    # Rebalance markers
    for date, _ in results.get("rebalance_log", []):
        val = eq.loc[date, "value"] if date in eq.index else None
        if val:
            ax1.axvline(x=date, color="#444444", linewidth=0.5, alpha=0.6)

    ax1.set_title(title, fontsize=14, pad=10)
    ax1.set_ylabel("Portfolio Value (USD)", fontsize=10)
    ax1.legend(loc="upper left", framealpha=0.2, facecolor="#222222",
               labelcolor="white", fontsize=9)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax1.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=30, ha="right")
    ax1.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda x, _: f"${x:,.0f}")
    )

    # --- Bottom panel: drawdown ---
    ax2 = axes[1]
    running_max = eq["value"].cummax()
    drawdown    = ((eq["value"] - running_max) / running_max) * 100
    ax2.fill_between(drawdown.index, drawdown, 0,
                     color="#ff4444", alpha=0.5, label="Drawdown")
    ax2.plot(drawdown.index, drawdown, color="#ff4444", linewidth=0.8)
    ax2.set_ylabel("Drawdown %", fontsize=9)
    ax2.set_xlabel("Date", fontsize=9)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=30, ha="right")
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:.0f}%"))

    # --- Metrics box ---
    metrics_text = (
        f"Total Return: {m.get('total_return_pct', 0):.1f}%  |  "
        f"Ann. Return: {m.get('annualised_return_pct', 0):.1f}%  |  "
        f"Max DD: {m.get('max_drawdown_pct', 0):.1f}%  |  "
        f"Sharpe: {m.get('sharpe_ratio', 0):.2f}  |  "
        f"Volatility: {m.get('annual_volatility_pct', 0):.1f}%  |  "
        f"Total Fees: ${fees.get('total', 0):,.0f}"
    )
    fig.text(0.5, 0.01, metrics_text, ha="center", fontsize=8.5,
             color="#888888", style="italic")

    plt.tight_layout(rect=[0, 0.03, 1, 1])
    plt.savefig(save_path, dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    print(f"\n  [chart] saved → {save_path}")
    try:
        plt.show()
    except Exception:
        pass
    plt.close()


# =============================================================================
# MAIN — INTERACTIVE CLI
# =============================================================================

def get_date_input(prompt: str) -> datetime:
    while True:
        raw = input(prompt).strip()
        try:
            return datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            print("  Invalid format. Please use YYYY-MM-DD (e.g. 2022-01-01)")


def get_choice(prompt: str, options: List[str]) -> str:
    options_lower = [o.lower() for o in options]
    while True:
        val = input(prompt).strip().lower()
        if val in options_lower:
            return val
        print(f"  Please enter one of: {', '.join(options)}")


def main():
    print("\n" + "=" * 60)
    print("  VELI — Crypto Direct Indexing Tool  |  MVP v1.0")
    print("=" * 60)
    print("  Sections: Data → Universe → Weights → Rebalance → Fees → Backtest\n")

    # ── Section 2: Universe size selection ──────────────────────────────────
    print("STEP 1 — Universe Selection")
    print("  [300] Top 300 coins by market cap")
    print("  [500] Top 500 coins by market cap")
    universe_size = int(get_choice("  Select universe size (300 / 500): ", ["300", "500"]))

    # ── Section 3: Methodology ───────────────────────────────────────────────
    print("\nSTEP 2 — Weighting Methodology")
    print("  [market_cap] Market Cap Weighted  (larger cap = bigger weight)")
    print("  [equal]      Equal Weighted       (each coin gets 1/N weight)")
    method = get_choice("  Select method (market_cap / equal): ", ["market_cap", "equal"])

    # ── Backtest date range ──────────────────────────────────────────────────
    print("\nSTEP 3 — Backtest Date Range")
    print("  Format: YYYY-MM-DD  |  Example: 2022-01-01")
    start_date = get_date_input("  Start date: ")
    end_date   = get_date_input("  End date:   ")
    if end_date <= start_date:
        print("  End date must be after start date. Exiting.")
        return

    # ── Initial investment ───────────────────────────────────────────────────
    inv_input = input("\n  Initial investment in USD (default 1000): ").strip()
    initial_investment = float(inv_input) if inv_input else 1000.0

    # ── Section 1: Fetch data ────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("  SECTION 1 — Fetching Data from CoinGecko")
    print(f"{'='*60}")

    if not COINGECKO_API_KEY:
        print("\n  [!] COINGECKO_API_KEY not set in environment.")
        print("      Set it via: export COINGECKO_API_KEY=your_key_here")
        print("      The script will attempt to use the free tier (rate limits apply).\n")

    fetcher    = CoinGeckoFetcher()
    all_coins  = fetcher.fetch_top_coins(n=max(universe_size, 500))

    # ── Select universe ──────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("  SECTION 2 — Universe Selection")
    print(f"{'='*60}")
    universe = select_universe(all_coins, size=universe_size)

    # ── Fetch price histories ────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("  SECTION 1b — Fetching Historical Price Data")
    print(f"{'='*60}")
    # Add a small buffer to start date for initial weight computation
    fetch_start = start_date - timedelta(days=7)
    histories = fetcher.fetch_universe_history(universe, fetch_start, end_date)

    # Also make sure BTC is in there for benchmark
    if "bitcoin" not in histories:
        btc_df = fetcher.fetch_coin_history("bitcoin", fetch_start, end_date)
        if len(btc_df) > 0:
            histories["bitcoin"] = btc_df

    # ── Run backtest (sections 3, 4, 5, 6) ──────────────────────────────────
    print(f"\n{'='*60}")
    print("  SECTIONS 3-6 — Weights | Rebalancing | Fees | Backtest")
    print(f"{'='*60}")

    results = run_backtest(
        coin_universe=universe,
        price_histories=histories,
        start_date=start_date,
        end_date=end_date,
        weighting_method=method,
        initial_investment=initial_investment,
        cap=0.30,
        floor=0.001,
        verbose=True,
    )

    # ── Plot results ─────────────────────────────────────────────────────────
    n     = universe_size
    mname = "Market Cap Weighted" if method == "market_cap" else "Equal Weighted"
    title = (f"Veli — Top {n} Crypto Index ({mname})\n"
             f"{start_date.date()} → {end_date.date()}")

    chart_path = (
        f"backtest_top{n}_{method}_{start_date.strftime('%Y%m%d')}"
        f"_{end_date.strftime('%Y%m%d')}.png"
    )
    plot_results(results, title=title, save_path=chart_path)

    print("\n  Done. Backtest complete.")
    print(f"  Chart saved: {chart_path}")
    print("\n  ⚠  Survivorship bias note: Universe reflects TODAY's top coins.")
    print("     Historical simulation does not account for coins that have")
    print("     since left the top N (common in crypto). Acceptable for MVP.\n")


if __name__ == "__main__":
    main()
