"""
Veli — Crypto Direct Indexing Tool | Flask Backend
Serves the REST API at /api/* and the React SPA at /
"""

import os, sys, json
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory, abort

# Make engine importable
sys.path.insert(0, os.path.dirname(__file__))

from engine          import get_fetcher, compute_weights, run_backtest, STABLECOINS
from engine.fetcher  import CoinGeckoFetcher
from database        import (init_db, save_strategy, list_strategies,
                              get_strategy, update_strategy, delete_strategy)

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")

# Initialise DB at startup (not on import — per lessons.md)
init_db()

# ── CORS — allow all origins so the app works when deployed on any domain ─────
@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response

@app.route("/api/<path:path>", methods=["OPTIONS"])
def options_handler(path):
    from flask import Response
    r = Response("", status=204)
    r.headers["Access-Control-Allow-Origin"]  = "*"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return r

# MVP category definitions (CoinGecko slugs)
MVP_CATEGORIES = [
    {"id": "layer-1",                       "name": "Layer 1",  "color": "#6366f1"},
    {"id": "decentralized-finance-defi",    "name": "DeFi",     "color": "#10b981"},
    {"id": "meme-token",                    "name": "Meme",     "color": "#f59e0b"},
    {"id": "real-world-assets-rwa",         "name": "RWA",      "color": "#3b82f6"},
    {"id": "artificial-intelligence",       "name": "AI",       "color": "#a855f7"},
    {"id": "depin",                         "name": "DePIN",    "color": "#ec4899"},
    {"id": "layer-2",                       "name": "Layer 2",  "color": "#14b8a6"},
    {"id": "gaming",                        "name": "Gaming",   "color": "#f97316"},
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def error(msg: str, code: int = 400):
    r = jsonify({"error": msg, "status": code})
    r.status_code = code
    return r

def parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d")

# ── Coin endpoints ─────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    """GET /api/status — returns API key info and a live ping to CoinGecko."""
    from engine.fetcher import COINGECKO_API_KEY, _get_auth_header
    import requests as req

    key      = COINGECKO_API_KEY
    key_type = "Demo" if key.startswith("CG-") else "Pro" if key else "None"
    masked   = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else key

    # Live ping — bypass cache entirely
    ping_ok  = False
    ping_msg = ""
    try:
        headers = _get_auth_header(key)
        r = req.get("https://api.coingecko.com/api/v3/ping",
                    headers=headers, timeout=8)
        if r.status_code == 200:
            ping_ok  = True
            ping_msg = r.json().get("gecko_says", "(V3) To the Moon!")
        elif r.status_code == 401:
            ping_msg = "401 Unauthorized — wrong API key or header"
        elif r.status_code == 429:
            ping_msg = "429 Rate limited — wait 60s and try again"
        else:
            ping_msg = f"HTTP {r.status_code}"
    except req.exceptions.ConnectionError:
        ping_msg = "Cannot reach api.coingecko.com — check your internet connection"
    except req.exceptions.Timeout:
        ping_msg = "Request timed out (>8s) — CoinGecko may be slow"
    except Exception as e:
        ping_msg = f"Unexpected error: {type(e).__name__}"

    return jsonify({
        "key_masked":  masked,
        "key_type":    key_type,
        "header_used": list(_get_auth_header(key).keys())[0] if key else "none",
        "ping_ok":     ping_ok,
        "ping_msg":    ping_msg,
    })


@app.route("/api/coins/refresh")
def api_coins_refresh():
    """GET /api/coins/refresh?n=50
    Bypasses cache and fetches live from CoinGecko.
    Returns top N coins with real-time prices + key/source verification info.
    """
    from engine.fetcher import COINGECKO_API_KEY, _get_auth_header, CACHE_DIR
    import requests as req, math as _math

    n       = min(int(request.args.get("n", 50)), 250)
    key     = COINGECKO_API_KEY
    headers = _get_auth_header(key)

    try:
        r = req.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            headers=headers,
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": n,
                "page": 1,
                "sparkline": False,
                "price_change_percentage": "24h",
            },
            timeout=15,
        )

        if r.status_code == 401:
            return error("API key rejected (401 Unauthorized). Check key type/header.", 401)
        if r.status_code == 429:
            return error("Rate limited (429). Wait 60s and try again.", 429)
        r.raise_for_status()

        coins = r.json()
        if not isinstance(coins, list):
            return error(f"Unexpected response: {str(coins)[:200]}", 500)

        # Update the cache with fresh data so subsequent /api/coins calls use it
        fetcher = get_fetcher()
        for c in coins:
            c["is_stablecoin"] = c["id"] in STABLECOINS
        fetcher._save_cache(f"top_coins_{n}", coins)

        # Return verification data: real prices, mcaps, timestamps
        return jsonify({
            "source":      "CoinGecko live API (no cache)",
            "key_type":    "Demo" if key.startswith("CG-") else "Pro",
            "key_masked":  f"{key[:8]}...{key[-4:]}",
            "header_used": list(headers.keys())[0] if headers else "none",
            "fetched_at":  datetime.utcnow().isoformat() + "Z",
            "count":       len(coins),
            "coins":       [
                {
                    "rank":        c.get("market_cap_rank"),
                    "id":          c.get("id"),
                    "symbol":      c.get("symbol","").upper(),
                    "name":        c.get("name"),
                    "price_usd":   c.get("current_price"),
                    "market_cap":  c.get("market_cap"),
                    "volume_24h":  c.get("total_volume"),
                    "change_24h":  c.get("price_change_percentage_24h"),
                    "image":       c.get("image"),
                }
                for c in coins
            ],
        })
    except req.exceptions.ConnectionError:
        return jsonify({
            "error": True,
            "error_type": "connection",
            "message": "Cannot reach api.coingecko.com — check your internet connection",
            "key_type":   "Demo" if key.startswith("CG-") else "Pro",
            "key_masked": f"{key[:8]}...{key[-4:]}",
            "header_used": list(headers.keys())[0] if headers else "none",
        }), 503
    except req.exceptions.Timeout:
        return jsonify({
            "error": True,
            "error_type": "timeout",
            "message": "CoinGecko request timed out (>15s). Try again.",
        }), 503
    except Exception as e:
        return error(f"Fetch failed: {type(e).__name__} — {str(e)[:80]}", 500)


@app.route("/api/coins")
def api_coins():
    """GET /api/coins?n=500
    Returns top N coins with metadata, market cap, and category tags.
    Annotates is_stablecoin, is_meme, is_cex using CoinGecko category API (cached 24h).
    """
    n = min(int(request.args.get("n", 500)), 500)
    try:
        fetcher = get_fetcher()
        coins   = fetcher.fetch_top_coins(n)

        # Pull category memberships from CoinGecko (each cached 24h independently)
        try:
            stable_ids = set(fetcher.fetch_category_coins("stablecoins"))
        except Exception:
            stable_ids = set()
        try:
            meme_ids = set(fetcher.fetch_category_coins("meme-token"))
        except Exception:
            meme_ids = set()
        try:
            cex_ids = set(fetcher.fetch_category_coins("centralized-exchange"))
        except Exception:
            cex_ids = set()

        for c in coins:
            cid = c["id"]
            c["is_stablecoin"] = cid in STABLECOINS or cid in stable_ids
            c["is_meme"]       = cid in meme_ids
            c["is_cex"]        = cid in cex_ids

        return jsonify({"coins": coins, "count": len(coins)})
    except Exception as e:
        return error(str(e), 500)


@app.route("/api/categories")
def api_categories():
    """GET /api/categories — returns MVP category list."""
    return jsonify({"categories": MVP_CATEGORIES})


@app.route("/api/coins/preview")
def api_coins_preview():
    """GET /api/coins/preview?ids=bitcoin,ethereum,...
    Returns current weights preview for a list of coin IDs.
    Used for the live pie chart preview in Step 3.
    """
    ids_param = request.args.get("ids", "")
    method    = request.args.get("method", "market_cap")
    cap       = float(request.args.get("cap",   0.30))
    floor     = float(request.args.get("floor", 0.001))

    if not ids_param:
        return jsonify({"weights": []})

    coin_ids = [i.strip() for i in ids_param.split(",") if i.strip()]
    try:
        fetcher    = get_fetcher()
        all_coins  = fetcher.fetch_top_coins(500)
        coin_map   = {c["id"]: c for c in all_coins}
        filtered   = [coin_map[cid] for cid in coin_ids if cid in coin_map]
        weights    = compute_weights(filtered, method=method, cap=cap, floor=floor)
        result = [
            {
                "id":     cid,
                "symbol": coin_map[cid]["symbol"].upper() if cid in coin_map else cid,
                "name":   coin_map[cid]["name"] if cid in coin_map else cid,
                "weight": round(w * 100, 2),
            }
            for cid, w in sorted(weights.items(), key=lambda x: -x[1])
        ]
        return jsonify({"weights": result})
    except Exception as e:
        return error(str(e), 500)


# ── Backtest endpoint ──────────────────────────────────────────────────────────

@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    """
    POST /api/backtest
    Body: {
        coin_ids: string[],
        start_date: "YYYY-MM-DD",
        end_date: "YYYY-MM-DD",
        method: "market_cap" | "equal",
        cap: float,
        floor: float,
        rebalance_freq: "weekly"|"monthly"|"quarterly"|"yearly",
        initial_investment: float,
        aum_fee: float,
        rebalancing_fee: float,
        performance_fee: float,
    }
    """
    body = request.get_json(force=True)
    if not body:
        return error("Request body required")

    # Validate required fields
    required = ["coin_ids", "start_date", "end_date"]
    for f in required:
        if f not in body:
            return error(f"Missing field: {f}")

    try:
        start_date = parse_date(body["start_date"])
        end_date   = parse_date(body["end_date"])
    except ValueError:
        return error("Invalid date format — use YYYY-MM-DD")

    if end_date <= start_date:
        return error("end_date must be after start_date")
    if (end_date - start_date).days < 30:
        return error("Date range must be at least 30 days")

    coin_ids    = body["coin_ids"]
    method      = body.get("method", "market_cap")
    cap         = float(body.get("cap", 0.30))
    floor_val   = float(body.get("floor", 0.001))
    reb_freq    = body.get("rebalance_freq", "monthly")
    initial     = float(body.get("initial_investment", 1000.0))
    aum_fee     = float(body.get("aum_fee", 0.01))
    reb_fee     = float(body.get("rebalancing_fee", 0.003))
    perf_fee    = float(body.get("performance_fee", 0.15))
    index_size  = int(body["index_size"]) if body.get("index_size") else None

    if not coin_ids:
        return error("coin_ids cannot be empty")
    if index_size and index_size < 2:
        return error("index_size must be at least 2")

    # Warn if cap is too tight for coin count (won't crash — engine auto-adjusts)
    effective_n = index_size if index_size and index_size < len(coin_ids) else len(coin_ids)
    if effective_n < 2:
        return error("At least 2 coins required")
    min_viable_cap = 1.0 / effective_n
    cap_warning = None
    if cap < min_viable_cap:
        auto_cap = round((min_viable_cap + 0.001) * 100, 1)
        cap_warning = (
            f"Cap {cap*100:.1f}% is below the minimum viable {min_viable_cap*100:.1f}% "
            f"for {effective_n} coins. Auto-adjusted to {auto_cap}%."
        )
    if method not in ("market_cap", "equal"):
        return error("method must be 'market_cap' or 'equal'")
    if reb_freq not in ("weekly", "monthly", "quarterly", "yearly"):
        return error("Invalid rebalance_freq")

    try:
        fetcher    = get_fetcher()
        all_coins  = fetcher.fetch_top_coins(500)
        coin_map   = {c["id"]: c for c in all_coins}
        universe   = [coin_map[cid] for cid in coin_ids if cid in coin_map]

        if not universe:
            return error("None of the provided coin_ids were found")

        # When index_size is set, cap the universe for history fetching:
        # only need the top (index_size × 4) coins — enough to cover all historical
        # entrants/exits with a generous buffer, without fetching hundreds of histories.
        fetch_universe = universe
        if index_size and index_size < len(universe):
            fetch_cap = min(len(universe), max(index_size * 4, 30))
            # Sort by current market cap descending before capping
            fetch_universe = sorted(
                universe,
                key=lambda c: c.get("market_cap") or 0,
                reverse=True
            )[:fetch_cap]

        # Fetch price histories
        fetch_start = start_date - timedelta(days=7)
        histories   = fetcher.fetch_universe_history(fetch_universe, fetch_start, end_date)

        if len(histories) < 2:
            return error(
                f"Only {len(histories)} coin(s) returned price data for "
                f"{body['start_date']} to {body['end_date']}. "
                "This usually means the date range predates the CoinGecko demo API limit "
                "(demo key supports ~1 year of daily history). Try a more recent date range."
            )

        # Always include BTC for benchmark (skip re-fetch if user already selected BTC)
        if "bitcoin" not in histories:
            try:
                btc_df = fetcher.fetch_coin_history("bitcoin", fetch_start, end_date)
                if len(btc_df) >= 10:
                    histories["bitcoin"] = btc_df
            except Exception as e:
                app.logger.warning(f"BTC benchmark fetch failed: {e}")

        results = run_backtest(
            coin_universe      = universe,
            price_histories    = histories,
            start_date         = start_date,
            end_date           = end_date,
            weighting_method   = method,
            initial_investment = initial,
            cap                = cap,
            floor              = floor_val,
            rebalance_freq     = reb_freq,
            aum_fee            = aum_fee,
            rebalancing_fee    = reb_fee,
            performance_fee    = perf_fee,
        )
        if cap_warning:
            results["cap_warning"] = cap_warning
        return jsonify(results)

    except ValueError as e:
        return error(str(e))
    except Exception as e:
        app.logger.exception("Backtest error")
        return error(f"Backtest failed: {str(e)}", 500)


# ── Strategy CRUD ──────────────────────────────────────────────────────────────

@app.route("/api/strategies", methods=["GET"])
def api_list_strategies():
    return jsonify({"strategies": list_strategies()})

@app.route("/api/strategies", methods=["POST"])
def api_save_strategy():
    body = request.get_json(force=True)
    if not body or not body.get("name"):
        return error("name is required")
    strategy = save_strategy(body["name"], body.get("config", {}))
    return jsonify(strategy), 201

@app.route("/api/strategies/<int:sid>", methods=["GET"])
def api_get_strategy(sid):
    s = get_strategy(sid)
    if not s:
        return error("Strategy not found", 404)
    return jsonify(s)

@app.route("/api/strategies/<int:sid>", methods=["PUT"])
def api_update_strategy(sid):
    body = request.get_json(force=True)
    s = update_strategy(sid, body.get("name", ""), body.get("config", {}))
    if not s:
        return error("Strategy not found", 404)
    return jsonify(s)

@app.route("/api/strategies/<int:sid>", methods=["DELETE"])
def api_delete_strategy(sid):
    if not delete_strategy(sid):
        return error("Strategy not found", 404)
    return jsonify({"deleted": True})


# ── Health check ──────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "version": "2.0.0"})


# ── SPA fallback — serve React for all non-API routes ─────────────────────────

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_spa(path):
    index = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index):
        return send_from_directory(FRONTEND_DIR, "index.html")
    return "<h2>Frontend not built yet — run the dev server</h2>", 404


# JSON error handlers so browser never gets HTML to parse
@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found", "status": 404}), 404

@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Server error", "status": 500}), 500

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "Method not allowed", "status": 405}), 405

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    debug = os.environ.get("FLASK_ENV") == "development"
    print(f"\n  Veli Index Tool  |  http://localhost:{port}")
    print(f"  API base:        http://localhost:{port}/api")
    print(f"  Frontend:        {FRONTEND_DIR}\n")
    app.run(host="0.0.0.0", port=port, debug=debug)
