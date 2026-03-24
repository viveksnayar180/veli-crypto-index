"""
SQLite persistence for saved strategy configurations.
Uses Python's built-in sqlite3 — no extra dependencies.

DB path: one level above backend/ (repo root), so it persists across deploys
on platforms that mount a persistent volume at the project root.

NOTE: init_db() is NOT called on import. It is called explicitly by app.py
at startup to avoid errors during test imports or cold-start path issues.
"""

import sqlite3, json, os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "veli_strategies.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategies (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                config      TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)
        conn.commit()


def save_strategy(name: str, config: dict) -> dict:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO strategies (name, config, created_at, updated_at) VALUES (?,?,?,?)",
            (name, json.dumps(config), now, now)
        )
        conn.commit()
        return {"id": cur.lastrowid, "name": name, "config": config,
                "created_at": now, "updated_at": now}


def list_strategies() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM strategies ORDER BY updated_at DESC"
        ).fetchall()
    return [{"id": r["id"], "name": r["name"], "config": json.loads(r["config"]),
             "created_at": r["created_at"], "updated_at": r["updated_at"]} for r in rows]


def get_strategy(strategy_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM strategies WHERE id=?", (strategy_id,)
        ).fetchone()
    if not row:
        return None
    return {"id": row["id"], "name": row["name"], "config": json.loads(row["config"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def update_strategy(strategy_id: int, name: str, config: dict) -> dict | None:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE strategies SET name=?, config=?, updated_at=? WHERE id=?",
            (name, json.dumps(config), now, strategy_id)
        )
        conn.commit()
    return get_strategy(strategy_id)


def delete_strategy(strategy_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM strategies WHERE id=?", (strategy_id,))
        conn.commit()
    return cur.rowcount > 0
