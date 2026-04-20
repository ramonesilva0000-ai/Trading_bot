"""SQLite persistence for whale events, cohort and trader-profile stats."""

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
    profile_id TEXT NOT NULL DEFAULT 'default',
    min_print_notional REAL NOT NULL DEFAULT 0,
    max_print_notional REAL NOT NULL DEFAULT 0,
    outcome_bps REAL,
    evaluated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_whale_events_cohort ON whale_events(cohort_id, symbol);
CREATE INDEX IF NOT EXISTS ix_whale_events_profile ON whale_events(profile_id);
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

CREATE TABLE IF NOT EXISTS profile_stats (
    profile_id TEXT PRIMARY KEY,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    neutrals INTEGER NOT NULL DEFAULT 0,
    total_bps REAL NOT NULL DEFAULT 0
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

_MIGRATIONS = (
    "ALTER TABLE whale_events ADD COLUMN profile_id TEXT NOT NULL DEFAULT 'default'",
    "ALTER TABLE whale_events ADD COLUMN min_print_notional REAL NOT NULL DEFAULT 0",
    "ALTER TABLE whale_events ADD COLUMN max_print_notional REAL NOT NULL DEFAULT 0",
)


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
            self._migrate()

    def _migrate(self) -> None:
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(whale_events)")}
        for stmt in _MIGRATIONS:
            col = stmt.split("ADD COLUMN ")[1].split()[0]
            if col not in cols:
                try:
                    self._conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def insert_event(self, ev: WhaleEvent) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO whale_events
                   (symbol, side, start_ms, end_ms, vwap, notional_usd, prints,
                    cohort_id, profile_id, min_print_notional, max_print_notional, evaluated)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (ev.symbol, ev.side.value, ev.start_ms, ev.end_ms,
                 ev.vwap, ev.notional_usd, ev.prints, ev.cohort_id, ev.profile_id,
                 ev.min_print_notional, ev.max_print_notional),
            )
            return int(cur.lastrowid or 0)

    def pending_evaluations(self, before_ms: int) -> Iterator[tuple[int, WhaleEvent]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, symbol, side, start_ms, end_ms, vwap, notional_usd, prints,
                          cohort_id, profile_id, min_print_notional, max_print_notional
                   FROM whale_events WHERE evaluated = 0 AND end_ms <= ?""",
                (before_ms,),
            ).fetchall()
        for r in rows:
            yield r[0], WhaleEvent(
                symbol=r[1], side=Side(r[2]), start_ms=r[3], end_ms=r[4],
                vwap=r[5], notional_usd=r[6], prints=r[7], cohort_id=r[8],
                profile_id=r[9], min_print_notional=r[10], max_print_notional=r[11],
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

    def bump_profile(self, profile_id: str, outcome_bps: float,
                     win: bool, loss: bool) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO profile_stats (profile_id, wins, losses, neutrals, total_bps)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(profile_id) DO UPDATE SET
                     wins = wins + excluded.wins,
                     losses = losses + excluded.losses,
                     neutrals = neutrals + excluded.neutrals,
                     total_bps = total_bps + excluded.total_bps""",
                (profile_id,
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
        return _score_row(row, cohort_id=cohort_id, symbol=symbol)

    def profile_stats(self, profile_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                """SELECT wins, losses, neutrals, total_bps
                   FROM profile_stats WHERE profile_id=?""",
                (profile_id,),
            ).fetchone()
        return _score_row(row, profile_id=profile_id)

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

    def profile_aggregates(self, profile_id: str) -> Optional[dict]:
        """Aggregate event-level features for a profile across all symbols.

        Returns per-profile ranges (lot size, trade notional), side skew,
        totals, and a per-symbol breakdown. Unrelated to win/loss stats —
        combine with `profile_stats` to get performance.
        """
        with self._lock:
            head = self._conn.execute(
                """SELECT COUNT(*),
                          MIN(notional_usd), MAX(notional_usd), AVG(notional_usd),
                          MIN(min_print_notional), MAX(max_print_notional), AVG(min_print_notional),
                          SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END),
                          SUM(CASE WHEN side='sell' THEN 1 ELSE 0 END),
                          MIN(start_ms), MAX(end_ms)
                   FROM whale_events WHERE profile_id=?""",
                (profile_id,),
            ).fetchone()
            pairs = self._conn.execute(
                """SELECT symbol, COUNT(*) AS n
                   FROM whale_events WHERE profile_id=?
                   GROUP BY symbol ORDER BY n DESC LIMIT 10""",
                (profile_id,),
            ).fetchall()
        if head is None or head[0] == 0:
            return None
        n, min_not, max_not, avg_not, min_lot, max_lot, avg_lot, buys, sells, first_ms, last_ms = head
        total_sides = (buys or 0) + (sells or 0)
        return {
            "profile_id": profile_id,
            "events": int(n),
            "trade_notional_min": float(min_not or 0),
            "trade_notional_max": float(max_not or 0),
            "trade_notional_avg": float(avg_not or 0),
            "lot_notional_min": float(min_lot or 0),
            "lot_notional_max": float(max_lot or 0),
            "lot_notional_avg": float(avg_lot or 0),
            "buys": int(buys or 0),
            "sells": int(sells or 0),
            "buy_pct": (buys / total_sides) if total_sides else 0.0,
            "first_ms": int(first_ms or 0),
            "last_ms": int(last_ms or 0),
            "pairs": [{"symbol": s, "events": int(c)} for s, c in pairs],
        }

    def top_profiles(self, min_events: int, limit: int = 20) -> list[dict]:
        """Rank profiles by confidence-adjusted expectancy."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT profile_id FROM profile_stats
                   WHERE (wins + losses + neutrals) >= ?""",
                (min_events,),
            ).fetchall()
        out: list[dict] = []
        for (pid,) in rows:
            perf = self.profile_stats(pid)
            agg = self.profile_aggregates(pid)
            if perf is None or agg is None:
                continue
            out.append({**perf, **agg})
        # Rank by expectancy × log(sample size) so small hot profiles don't
        # outrank larger, steadier ones.
        import math
        out.sort(key=lambda r: r["expectancy_bps"] * math.log1p(r["total"]),
                 reverse=True)
        return out[:limit]


def _score_row(row, **extra) -> Optional[dict]:
    if row is None:
        return None
    wins, losses, neutrals, total_bps = row
    total = wins + losses + neutrals
    if total == 0:
        return None
    decisive = wins + losses
    return {
        **extra,
        "wins": int(wins),
        "losses": int(losses),
        "neutrals": int(neutrals),
        "total": int(total),
        "win_rate": (wins / decisive) if decisive else 0.0,
        "expectancy_bps": float(total_bps) / total,
    }
