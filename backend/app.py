"""
Veli — Crypto Direct Indexing Tool | Flask Backend v2.1
Serves REST API at /api/* and React SPA at /

Changes from v2.0:
  - engine/ package now correctly resolved via sys.path
  - init_db() called explicitly at startup, not on import
  - /api/coins annotates categories[] and is_cex_token per brief spec
  - /api/backtest accepts entry_fee, exit_fee, min_market_cap, min_volume_24h,
    exclude_ids, include_ids and surfaces floor_warning
  - Backtest response includes pnl_breakdown (brief section 2.7)
"""

import os, sys, json
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory

# Make engine and database importable from this directory
sys.path.insert(0, os.path.dirname(__file__) or ".")

from engine         import get_fetcher, compute_weights, run_backtest, STABLECOINS, CEX_SLUG
from engine.fetcher import CoinGeckoFetcher
from database       import init_db, save_strategy, list_strategies, \
                           get_strategy, update_strategy, delete_strategy

# ── Paths ──────────────────────────────────────────────────────────────────────
# app.py is at  <repo>/backend/app.py
# frontend is at <repo>/frontend/index.html
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")

# Initialise DB at startup (not on import)
init_db()

# ── MVP categories (CoinGecko slugs + display metadata) ───────────────────────
# Layer 2 and Gaming are included per extended brief discussion even though the
# written brief lists them as future scope. They use real CoinGecko slugs.
MVP_CATEGORIES = [
    {"id": "layer-1",                     "name": "Layer 1",  "color": "#6366f1"},
    {"id": "decentralized-finance-defi",  "name": "DeFi",     "color": "#10b981"},
    {"id": "meme-token",                  "name": "Meme",     "color": "#f59e0b"},
    {"id": "real-world-assets-rwa",       "name": "RWA",      "color": "#3b82f6"},
    {"id": "artificial-intelligence",     "name": "AI",       "color": "#a855f7"},
    {"id": "depin",                       "name": "DePIN",    "color": "#ec4899"},
    {"id": "layer-2",                     "name": "Layer 2",  "color": "#14b8a6"},
    {"id": "gaming",                      "name": "Gaming",   "color": "#f97316"},
]
MVP_CATEGORY_SLUGS = [c["id"] for c in MVP_CATEGORIES]

# ── Helpers ────────────────────────────────────────────────────────────────────

def error(msg: str, code: int = 400):
    return jsonify({"error": msg}), code

def parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d")


# ── GET /api/coins ─────────────────────────────────────────────────────────────

@app.route("/api/coins")
def api_coins():
    """
    GET /api/coins?n=500
    Returns top N coins annotated with:
      is_stablecoin  bool
      is_cex_token   bool   (from CoinGecko centralized-exchange category)
      categories     list   (MVP category slugs this coin belongs to)
    Category annotation is cached 24h per slug — first call may be slow.
    Gracefully degrades: if category fetch fails, coins are returned unannotated.
    """
    n = min(int(request.args.get("n", 500)), 500)
    try:
        fetcher = get_fetcher()
        coins   = fetcher.fetch_top_coins(n)

        # Build category + CEX map (each slug cached independently 24h)
        all_slugs = MVP_CATEGORY_SLUGS + [CEX_SLUG]
        try:
            cat_map = fetcher.build_coin_category_map(all_slugs)
        except Exception:
            cat_map = {}  # degrade gracefully

        for c in coins:
            c_slugs = cat_map.get(c["id"], [])
            c["is_stablecoin"] = c["id"] in STABLECOINS
            c["is_cex_token"]  = CEX_SLUG in c_slugs
            c["categories"]    = [s for s in c_slugs if s != CEX_SLUG]

        return jsonify({"coins": coins, "count": len(coins)})
    except Exception as e:
        return error(str(e), 500)


# ── GET /api/categories ────────────────────────────────────────────────────────

@app.route("/api/categories")
def api_categories():
    """GET /api/categories — returns MVP category list with display metadata."""
    return jsonify({"categories": MVP_CATEGORIES})


# ── GET /api/coins/preview ─────────────────────────────────────────────────────

@app.route("/api/coins/preview")
def api_coins_preview():
    """
    GET /api/coins/preview?ids=bitcoin,ethereum,...&method=market_cap&cap=0.30&floor=0.001
    Returns current weight preview for the Step 3 live pie chart.
    """
    ids_param = request.args.get("ids", "")
    method    = request.args.get("method", "market_cap")
    cap       = float(request.args.get("cap",   0.30))
    floor     = float(request.args.get("floor", 0.001))

    if not ids_param:
        return jsonify({"weights": []})

    coin_ids = [i.strip() for i in ids_param.split(",") if i.strip()]
    try:
        fetcher   = get_fetcher()
        all_coins = fetcher.fetch_top_coins(500)
        coin_map  = {c["id"]: c for c in all_coins}
        filtered  = [coin_map[cid] for cid in coin_ids if cid in coin_map]
        weights   = compute_weights(filtered, method=method, cap=cap, floor=floor)
        result = [
            {
                "id":     cid,
                "symbol": coin_map[cid]["symbol"].upper() if cid in coin_map else cid,
                "name":   coin_map[cid]["name"]           if cid in coin_map else cid,
                "weight": round(w * 100, 2),
            }
            for cid, w in sorted(weights.items(), key=lambda x: -x[1])
        ]
        return jsonify({"weights": result})
    except Exception as e:
        return error(str(e), 500)


# ── POST /api/backtest ─────────────────────────────────────────────────────────

@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    """
    POST /api/backtest

    Required body fields:
      coin_ids          string[]   — base coin selection
      start_date        YYYY-MM-DD
      end_date          YYYY-MM-DD

    Optional body fields (all have sensible defaults):
      method            market_cap | equal      (default: market_cap)
      cap               float 0-1               (default: 0.30)
      floor             float 0-1               (default: 0.001)
      rebalance_freq    weekly|monthly|quarterly|yearly  (default: monthly)
      initial_investment float                  (default: 1000.0)
      entry_fee         float 0-1               (default: 0.0)
      exit_fee          float 0-1               (default: 0.0)
      aum_fee           float 0-1 annual        (default: 0.01)
      rebalancing_fee   float 0-1               (default: 0.003)
      performance_fee   float 0-1               (default: 0.15)
      exclude_ids       string[]   — remove these coins from universe
      include_ids       string[]   — force-add these coins to universe
      min_market_cap    float USD  — filter out coins below this market cap
      min_volume_24h    float USD  — filter out coins below this 24h volume

    Response includes:
      equity_curve, benchmark_curve, weights_history, fee_events,
      fee_summary, metrics, btc_metrics, pnl_breakdown, coin_count,
      high_watermark, plus any warnings (cap_warning, floor_warning)
    """
    body = request.get_json(force=True)
    if not body:
        return error("Request body required")

    for f in ["coin_ids", "start_date", "end_date"]:
        if f not in body:
            return error(f"Missing required field: {f}")

    try:
        start_date = parse_date(body["start_date"])
        end_date   = parse_date(body["end_date"])
    except ValueError:
        return error("Invalid date format — use YYYY-MM-DD")

    if end_date <= start_date:
        return error("end_date must be after start_date")
    if (end_date - start_date).days < 30:
        return error("Date range must be at least 30 days")

    coin_ids     = body["coin_ids"]
    method       = body.get("method", "market_cap")
    cap          = float(body.get("cap", 0.30))
    floor_val    = float(body.get("floor", 0.001))
    reb_freq     = body.get("rebalance_freq", "monthly")
    initial      = float(body.get("initial_investment", 1000.0))
    entry_fee    = float(body.get("entry_fee", 0.0))
    exit_fee     = float(body.get("exit_fee", 0.0))
    aum_fee      = float(body.get("aum_fee", 0.01))
    reb_fee      = float(body.get("rebalancing_fee", 0.003))
    perf_fee     = float(body.get("performance_fee", 0.15))
    exclude_ids  = set(body.get("exclude_ids", []))
    include_ids  = body.get("include_ids", [])
    min_mcap     = float(body.get("min_market_cap", 0))
    min_vol      = float(body.get("min_volume_24h", 0))

    if not coin_ids:
        return error("coin_ids cannot be empty")
    if len(coin_ids) < 2:
        return error("At least 2 coins required")
    if method not in ("market_cap", "equal"):
        return error("method must be 'market_cap' or 'equal'")
    if reb_freq not in ("weekly", "monthly", "quarterly", "yearly"):
        return error("Invalid rebalance_freq")

    try:
        fetcher   = get_fetcher()
        all_coins = fetcher.fetch_top_coins(500)
        coin_map  = {c["id"]: c for c in all_coins}

        # Build base universe from requested coin_ids
        universe = [coin_map[cid] for cid in coin_ids if cid in coin_map]

        # Apply exclusions
        if exclude_ids:
            universe = [c for c in universe if c["id"] not in exclude_ids]

        # Apply market cap threshold
        if min_mcap > 0:
            universe = [c for c in universe if (c.get("market_cap") or 0) >= min_mcap]

        # Apply 24h volume threshold
        if min_vol > 0:
            universe = [c for c in universe if (c.get("total_volume") or 0) >= min_vol]

        # Force-include specific coins (even if not in base universe)
        if include_ids:
            existing_ids = {c["id"] for c in universe}
            extras = [coin_map[cid] for cid in include_ids
                      if cid in coin_map and cid not in existing_ids]
            universe.extend(extras)

        if not universe:
            return error("No coins remaining after filters")
        if len(universe) < 2:
            return error("At least 2 coins required after filters")

        # ── Warnings ──────────────────────────────────────────────────────────
        warnings = {}

        min_viable_cap = 1.0 / len(universe)
        if cap < min_viable_cap:
            auto_cap = round((min_viable_cap + 0.001) * 100, 1)
            warnings["cap_warning"] = (
                f"Cap {cap*100:.1f}% is below minimum viable {min_viable_cap*100:.1f}% "
                f"for {len(universe)} coins. Auto-adjusted to {auto_cap}%."
            )

        if floor_val * len(universe) > 1.0:
            effective_floor = round(0.9 / len(universe) * 100, 4)
            warnings["floor_warning"] = (
                f"Floor {floor_val*100:.3f}% × {len(universe)} coins > 100%. "
                f"Auto-adjusted to {effective_floor:.4f}%."
            )

        # ── Fetch histories ────────────────────────────────────────────────────
        fetch_start = start_date - timedelta(days=7)
        histories   = fetcher.fetch_universe_history(universe, fetch_start, end_date)

        if "bitcoin" not in histories:
            btc_df = fetcher.fetch_coin_history("bitcoin", fetch_start, end_date)
            if len(btc_df) >= 10:
                histories["bitcoin"] = btc_df

        # ── Run simulation ────────────────────────────────────────────────────
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
            entry_fee          = entry_fee,
            exit_fee           = exit_fee,
            aum_fee            = aum_fee,
            rebalancing_fee    = reb_fee,
            performance_fee    = perf_fee,
        )

        results.update(warnings)
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
    return jsonify(s) if s else error("Strategy not found", 404)

@app.route("/api/strategies/<int:sid>", methods=["PUT"])
def api_update_strategy(sid):
    body = request.get_json(force=True)
    s = update_strategy(sid, body.get("name", ""), body.get("config", {}))
    return jsonify(s) if s else error("Strategy not found", 404)

@app.route("/api/strategies/<int:sid>", methods=["DELETE"])
def api_delete_strategy(sid):
    if not delete_strategy(sid):
        return error("Strategy not found", 404)
    return jsonify({"deleted": True})


# ── Health check ───────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "version": "2.1.0"})


# ── SPA fallback ───────────────────────────────────────────────────────────────

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_spa(path):
    index = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index):
        return send_from_directory(FRONTEND_DIR, "index.html")
    return "<h2>Frontend not found — check FRONTEND_DIR path</h2>", 404


if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 8000))
    debug = os.environ.get("FLASK_ENV") == "development"
    print(f"\n  Veli Index Tool  |  http://localhost:{port}")
    print(f"  API base:        http://localhost:{port}/api")
    print(f"  Frontend dir:    {FRONTEND_DIR}\n")
    app.run(host="0.0.0.0", port=port, debug=debug)
