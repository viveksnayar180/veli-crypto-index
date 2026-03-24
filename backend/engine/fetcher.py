"""
CoinGecko data fetcher with local JSON caching and category annotation.

Key design points:
  - Pro API key via COINGECKO_API_KEY env var (x-cg-pro-api-key header)
  - Atomic cache writes (os.replace) prevent corrupt reads under concurrency
  - Category map built from per-slug calls, each cached 24h independently
  - Coins returned from /api/coins are annotated with categories[] and is_cex_token
"""

import os, json, time, math, requests
import pandas as pd
from datetime import datetime
from typing import Dict, List, Optional

COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "CG-uB6JDneXnjCYQrFR5Wf9dNH4")
COINGECKO_BASE    = "https://pro-api.coingecko.com/api/v3"
CACHE_DIR         = os.path.join(os.path.dirname(__file__), "..", ".cache")
RATE_LIMIT_SLEEP  = 0.5    # Pro tier allows ~30 req/min; 0.5s is comfortable
MAX_RETRIES       = 3

os.makedirs(CACHE_DIR, exist_ok=True)

STABLECOINS = {
    "tether","usd-coin","binance-usd","dai","true-usd","frax","usdd",
    "paxos-standard","gemini-dollar","fei-usd","nusd","liquity-usd",
    "euro-coin","first-digital-usd","paypal-usd","usde","ethena-usde",
    "tether-eurt","stasis-eurs","ageur",
}

# CoinGecko slugs for the categories we support
CEX_SLUG = "centralized-exchange"


def _get_auth_header(key: str) -> dict:
    """Return the correct auth header for the given API key type."""
    if not key:
        return {}
    if key.startswith("CG-"):
        return {"x-cg-demo-api-key": key}
    return {"x-cg-pro-api-key": key}


class CoinGeckoFetcher:
    def __init__(self):
        self.session = requests.Session()
        if COINGECKO_API_KEY:
            self.session.headers.update({"x-cg-pro-api-key": COINGECKO_API_KEY})

    # ── Cache helpers ─────────────────────────────────────────────────────────

    def _cache_path(self, key: str) -> str:
        safe = key.replace("/","_").replace("?","_").replace("&","_").replace(":","_")
        return os.path.join(CACHE_DIR, f"{safe}.json")

    def _load_cache(self, key: str, max_age_hours: float = 24) -> Optional[dict]:
        path = self._cache_path(key)
        if not os.path.exists(path):
            return None
        if (time.time() - os.path.getmtime(path)) / 3600 > max_age_hours:
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def _save_cache(self, key: str, data) -> None:
        """Atomic write: write to .tmp then rename — prevents corrupt partial reads."""
        path     = self._cache_path(key)
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(data, f)
            os.replace(tmp_path, path)   # atomic on Linux/macOS
        except OSError:
            pass  # non-critical — just means no cache

    # ── HTTP helper ───────────────────────────────────────────────────────────

    def _get(self, endpoint: str, params: dict = None, cache_key: str = None,
             cache_hours: float = 24):
        if cache_key:
            cached = self._load_cache(cache_key, cache_hours)
            if cached is not None:
                return cached
        url = f"{COINGECKO_BASE}{endpoint}"
        for attempt in range(MAX_RETRIES):
            try:
                time.sleep(RATE_LIMIT_SLEEP)
                r = self.session.get(url, params=params, timeout=30)
                if r.status_code == 429:
                    wait = 60 * (attempt + 1)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                data = r.json()
                if cache_key:
                    self._save_cache(cache_key, data)
                return data
            except requests.RequestException as e:
                if attempt == MAX_RETRIES - 1:
                    raise RuntimeError(f"CoinGecko API failed: {e}")
                time.sleep(5 * (attempt + 1))

    # ── Coin list ─────────────────────────────────────────────────────────────

    def fetch_top_coins(self, n: int = 500) -> List[dict]:
        """Top N coins by market cap. Cached 12h."""
        cache_key = f"top_coins_{n}"
        cached = self._load_cache(cache_key, max_age_hours=12)
        if cached:
            return cached
        coins, per_page, pages = [], 250, math.ceil(n / 250)
        for page in range(1, pages + 1):
            data = self._get("/coins/markets", params={
                "vs_currency": "usd", "order": "market_cap_desc",
                "per_page": per_page, "page": page, "sparkline": False,
            })
            coins.extend(data)
        coins = coins[:n]
        self._save_cache(cache_key, coins)
        return coins

    # ── Category annotation ───────────────────────────────────────────────────

    def fetch_category_coins(self, category_slug: str, per_page: int = 250) -> List[str]:
        """
        Returns list of coin_ids belonging to a CoinGecko category slug.
        Cached 24h per slug.
        """
        cache_key = f"cat_coins_{category_slug}"
        cached = self._load_cache(cache_key, max_age_hours=24)
        if cached is not None:
            return cached
        try:
            data = self._get("/coins/markets", params={
                "vs_currency": "usd",
                "category":    category_slug,
                "order":       "market_cap_desc",
                "per_page":    per_page,
                "page":        1,
                "sparkline":   False,
            })
        except Exception:
            return []
        coin_ids = [c["id"] for c in (data or [])]
        self._save_cache(cache_key, coin_ids)
        return coin_ids

    def build_coin_category_map(self, category_slugs: List[str]) -> Dict[str, List[str]]:
        """
        Returns {coin_id: [list of category slugs it belongs to]}.
        Aggregates per-slug caches — no extra network calls once slugs are cached.
        """
        coin_map: Dict[str, List[str]] = {}
        for slug in category_slugs:
            for cid in self.fetch_category_coins(slug):
                coin_map.setdefault(cid, []).append(slug)
        return coin_map

    # ── Price history ─────────────────────────────────────────────────────────

    def fetch_coin_history(self, coin_id: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Daily price + market_cap + volume for one coin. Cached 23h."""
        from_ts, to_ts = int(start.timestamp()), int(end.timestamp())
        cache_key = f"hist_{coin_id}_{from_ts}_{to_ts}"
        raw = self._load_cache(cache_key, max_age_hours=23)
        if raw is None:
            raw = self._get(f"/coins/{coin_id}/market_chart/range",
                            params={"vs_currency":"usd","from":from_ts,"to":to_ts},
                            cache_key=cache_key, cache_hours=23)
        if not raw or not raw.get("prices"):
            return pd.DataFrame(columns=["date","price","market_cap","volume"])
        df = (pd.DataFrame(raw["prices"], columns=["ts","price"])
              .merge(pd.DataFrame(raw["market_caps"],    columns=["ts","market_cap"]), on="ts")
              .merge(pd.DataFrame(raw["total_volumes"],  columns=["ts","volume"]),     on="ts"))
        df["date"] = pd.to_datetime(df["ts"], unit="ms").dt.normalize()
        return df.drop(columns=["ts"]).drop_duplicates("date").sort_values("date").set_index("date")

    def fetch_universe_history(
        self, coins: List[dict], start: datetime, end: datetime
    ) -> Dict[str, pd.DataFrame]:
        """Price history for all coins. Skips those with < 10 data points."""
        histories = {}
        for coin in coins:
            cid = coin["id"]
            try:
                df = self.fetch_coin_history(cid, start, end)
                if len(df) >= 10:
                    histories[cid] = df
            except Exception:
                pass
        return histories


# ── Shared singleton ──────────────────────────────────────────────────────────
_fetcher: Optional[CoinGeckoFetcher] = None

def get_fetcher() -> CoinGeckoFetcher:
    global _fetcher
    if _fetcher is None:
        _fetcher = CoinGeckoFetcher()
    return _fetcher
