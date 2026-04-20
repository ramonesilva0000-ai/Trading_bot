"""Simple backtest for the whale-follow strategy.

Replay a CSV of historical trades (ts_ms, symbol, price, amount, side) through
the same whale detector + tracker + signal engine used live. Perfect fidelity
is not the goal; the point is to answer "does following big-print bursts in
the configured cohort have an edge on this tape?".
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .config import Config
from .signals import SignalEngine
from .storage import Store
from .types import Side, Trade, WhaleEvent
from .whale_detector import WhaleDetector

log = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    events: int = 0
    signals: int = 0
    wins: int = 0
    losses: int = 0
    neutrals: int = 0
    pnl_bps_sum: float = 0.0

    @property
    def win_rate(self) -> float:
        decisive = self.wins + self.losses
        return self.wins / decisive if decisive else 0.0

    @property
    def expectancy_bps(self) -> float:
        n = self.wins + self.losses + self.neutrals
        return self.pnl_bps_sum / n if n else 0.0


def iter_trades_csv(path: Path) -> Iterator[Trade]:
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                yield Trade(
                    symbol=row["symbol"],
                    price=float(row["price"]),
                    amount=float(row["amount"]),
                    side=Side(row["side"].lower()),
                    ts_ms=int(row["ts_ms"]),
                    trade_id=row.get("trade_id") or None,
                )
            except (KeyError, ValueError):
                continue


def run_backtest(cfg: Config, csv_path: Path, store: Store) -> BacktestResult:
    detector = WhaleDetector(cfg.whale_detector)
    horizon_ms = int(cfg.whale_tracker.evaluation_horizon_h * 3_600_000)
    win_bps = cfg.whale_tracker.win_threshold_bps
    loss_bps = cfg.whale_tracker.loss_threshold_bps

    pending: list[tuple[WhaleEvent, int]] = []  # (event, evaluate_at_ms)
    result = BacktestResult()

    for trade in iter_trades_csv(csv_path):
        # Evaluate any event whose horizon elapsed at or before this trade.
        remaining: list[tuple[WhaleEvent, int]] = []
        for ev, due_ms in pending:
            if trade.ts_ms >= due_ms and trade.symbol == ev.symbol:
                move_bps = (trade.price - ev.vwap) / ev.vwap * 1e4 * ev.side.sign
                _score(result, move_bps, win_bps, loss_bps)
                store.bump_cohort(ev.cohort_id, ev.symbol, move_bps,
                                  move_bps >= win_bps, move_bps <= -loss_bps)
            else:
                remaining.append((ev, due_ms))
        pending = remaining

        for ev in detector.ingest(trade):
            result.events += 1
            store.insert_event(ev)
            pending.append((ev, ev.end_ms + horizon_ms))

    log.info(
        "backtest: events=%d wins=%d losses=%d neutrals=%d wr=%.2f exp=%.1fbps",
        result.events, result.wins, result.losses, result.neutrals,
        result.win_rate, result.expectancy_bps,
    )
    return result


def _score(result: BacktestResult, move_bps: float,
           win_bps: float, loss_bps: float) -> None:
    result.signals += 1
    result.pnl_bps_sum += move_bps
    if move_bps >= win_bps:
        result.wins += 1
    elif move_bps <= -loss_bps:
        result.losses += 1
    else:
        result.neutrals += 1
