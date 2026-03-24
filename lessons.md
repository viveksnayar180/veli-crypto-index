# Lessons Learned — Veli Index Tool

## Session: March 20, 2026

### Rules from Session 1
1. Call transcript overrides the brief when they conflict — always re-read both before implementing a section
2. Fees must REDUCE actual portfolio value (units held), not just be logged as estimates
3. Cap/floor constraint redistribution must be iterative — single-pass is wrong
4. **HWM is confirmed correct** — brief section 2.7 explicitly states "collected on a high-watermark basis". The call notes referencing WoW were exploratory. Code is correct. Do not change to WoW.
5. Cache CoinGecko responses locally before doing any computation — never re-fetch what you have
6. Test fee math with a 2-coin portfolio manually before running full backtest

---

## Session: March 23, 2026 — Full Structural Refactor

### What Was Fixed

**Deployment blockers resolved:**
- Created correct `backend/engine/` package structure — engine files are no longer at repo root
- `requirements.txt` now lists Flask + gunicorn (was wrongly listing FastAPI stack)
- `Procfile` now uses `gunicorn --bind 0.0.0.0:$PORT --chdir backend app:app`
- `railway.json` `startCommand` updated to match
- `FRONTEND_DIR` path in `app.py` is now `os.path.abspath(__file__)/../frontend` — resolves correctly from `backend/app.py`
- `database.py` DB path uses `os.path.abspath` to avoid empty-string dirname edge case
- `init_db()` removed from module-level import in `database.py`; called explicitly by `app.py` at startup

**Logic fixes:**
- `database.py`: `init_db()` no longer fires on import — prevents crash if DB path unreachable at import time
- `fetcher.py`: cache writes are now atomic (`write .tmp → os.replace`) — prevents corrupt partial reads under concurrency
- `fetcher.py`: API base URL changed to Pro endpoint (`pro-api.coingecko.com`) to match the supplied API key; `RATE_LIMIT_SLEEP` reduced to 0.5s (Pro tier headroom)

**Missing spec features added:**
- `fees.py`: `entry_fee` and `exit_fee` added to `FeeEngine` — charged once at strategy open/close
- `backtest.py`: entry fee deducted from effective initial investment before portfolio build; exit fee deducted from final portfolio value on last day
- `backtest.py`: `pnl_breakdown` dict in return value — gross return, net return, platform take (brief section 2.7)
- `fetcher.py`: `fetch_category_coins(slug)` + `build_coin_category_map(slugs)` — per-slug results cached 24h
- `app.py` `/api/coins`: coins now annotated with `categories[]` and `is_cex_token` from CoinGecko category API
- `app.py` `/api/backtest`: now accepts `entry_fee`, `exit_fee`, `exclude_ids`, `include_ids`, `min_market_cap`, `min_volume_24h`
- `app.py` `/api/backtest`: surfaces `floor_warning` when floor is auto-adjusted (previously silent)

### Rules Added
7. **Never call `init_db()` at module level** — call it explicitly from the app entry point so import errors and test environments are not broken
8. **Use `os.path.abspath(__file__)` not `__file__` directly** when building relative paths — avoids empty string from `os.path.dirname` when file is in CWD
9. **Pro API base URL is `pro-api.coingecko.com`** not `api.coingecko.com` — wrong base returns 401 even with valid key
10. **Atomic cache writes**: always write to `.tmp` then `os.replace()` — never write directly to the final path in concurrent environments
11. **`pnl_breakdown` gross is an approximation** — we add back total fees to final value. True gross requires a fee-free parallel simulation. This approximation is fine for MVP display but understates gross slightly (fees compound with portfolio growth).
12. **Category map: build from per-slug caches, don't cache the combined map** — keeps each slug independently refreshable without invalidating the whole map

### Corrections Log
- March 23: Removed `init_db()` from bottom of `database.py` — was crashing on cold start when `../` path didn't resolve
- March 23: Fixed `requirements.txt` (had FastAPI stack; app uses Flask)
- March 23: Fixed `Procfile` (used `python`, not gunicorn — dev server not suitable for Railway)
- March 23: Lesson 4 corrected — HWM is right, WoW note in prior session was wrong
