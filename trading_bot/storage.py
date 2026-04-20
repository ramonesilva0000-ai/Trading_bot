"""SQLite persistence for whale events and cohort statistics."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .types import Side, WhaleEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS whale_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    vwap REAL NOT NULL,
    notional_usd REAL NOT NULL,
    prints INTEGER NOT NULL,
    cohort_id TEXT NOT NULL,
    outcome_bps REAL,
    evaluated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_whale_events_cohort ON whale_events(cohort_id, symbol);
CREATE INDEX IF NOT EXISTS ix_whale_events_pending ON whale_events(evaluated, end_ms);

CREATE TABLE IF NOT EXISTS cohort_stats (
    cohort_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    neutrals INTEGER NOT NULL DEFAULT 0,
    total_bps REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (cohort_id, symbol)
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    amount REAL NOT NULL,
    fee REAL NOT NULL,
    order_id TEXT,
    meta TEXT
);
"""


class Store:
    def __init__(self, path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(p), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def insert_event(self, ev: WhaleEvent) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO whale_events
                   (symbol, side, start_ms, end_ms, vwap, notional_usd, prints, cohort_id, evaluated)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (ev.symbol, ev.side.value, ev.start_ms, ev.end_ms,
                 ev.vwap, ev.notional_usd, ev.prints, ev.cohort_id),
            )
            return int(cur.lastrowid or 0)

    def pending_evaluations(self, before_ms: int) -> Iterator[tuple[int, WhaleEvent]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, symbol, side, start_ms, end_ms, vwap, notional_usd, prints, cohort_id
                   FROM whale_events WHERE evaluated = 0 AND end_ms <= ?""",
                (before_ms,),
            ).fetchall()
        for r in rows:
            yield r[0], WhaleEvent(
                symbol=r[1], side=Side(r[2]), start_ms=r[3], end_ms=r[4],
                vwap=r[5], notional_usd=r[6], prints=r[7], cohort_id=r[8],
            )

    def settle_event(self, event_id: int, outcome_bps: float,
                     win: bool, loss: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE whale_events SET outcome_bps=?, evaluated=1 WHERE id=?",
                (outcome_bps, event_id),
            )

    def bump_cohort(self, cohort_id: str, symbol: str,
                    outcome_bps: float, win: bool, loss: bool) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO cohort_stats (cohort_id, symbol, wins, losses, neutrals, total_bps)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(cohort_id, symbol) DO UPDATE SET
                     wins = wins + excluded.wins,
                     losses = losses + excluded.losses,
                     neutrals = neutrals + excluded.neutrals,
                     total_bps = total_bps + excluded.total_bps""",
                (cohort_id, symbol,
                 1 if win else 0,
                 1 if loss else 0,
                 0 if (win or loss) else 1,
                 outcome_bps),
            )

    def cohort_stats(self, cohort_id: str, symbol: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                """SELECT wins, losses, neutrals, total_bps
                   FROM cohort_stats WHERE cohort_id=? AND symbol=?""",
                (cohort_id, symbol),
            ).fetchone()
        if row is None:
            return None
        wins, losses, neutrals, total_bps = row
        total = wins + losses + neutrals
        if total == 0:
            return None
        decisive = wins + losses
        return {
            "cohort_id": cohort_id,
            "symbol": symbol,
            "wins": int(wins),
            "losses": int(losses),
            "neutrals": int(neutrals),
            "total": int(total),
            "win_rate": (wins / decisive) if decisive else 0.0,
            "expectancy_bps": float(total_bps) / total,
        }

    def record_fill(self, ts_ms: int, symbol: str, side: str, price: float,
                    amount: float, fee: float, order_id: Optional[str],
                    meta: Optional[dict] = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO fills (ts_ms, symbol, side, price, amount, fee, order_id, meta)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts_ms, symbol, side, price, amount, fee, order_id,
                 json.dumps(meta) if meta else None),
            )

    def all_cohorts(self, symbol: Optional[str] = None) -> Iterable[dict]:
        with self._lock:
            if symbol:
                rows = self._conn.execute(
                    "SELECT cohort_id, symbol FROM cohort_stats WHERE symbol=?",
                    (symbol,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT cohort_id, symbol FROM cohort_stats"
                ).fetchall()
        for cid, sym in rows:
            s = self.cohort_stats(cid, sym)
            if s:
                yield s
