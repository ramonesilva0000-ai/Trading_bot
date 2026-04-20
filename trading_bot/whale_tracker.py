"""Score whale cohorts by how their events actually perform.

Every stored whale event is revisited after the evaluation horizon. We measure
the price move in bps from the event VWAP, signed by the whale's side, and
classify it as win / loss / neutral. Cohort win-rate and expectancy feed the
signal engine, which only follows cohorts with a proven edge on the sample.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .config import WhaleTrackerConfig
from .exchange import Exchange
from .storage import Store
from .types import WhaleEvent

log = logging.getLogger(__name__)


class WhaleTracker:
    def __init__(self, cfg: WhaleTrackerConfig, store: Store, exchange: Exchange) -> None:
        self.cfg = cfg
        self.store = store
        self.exchange = exchange

    async def run(self, now_ms_fn, stop: asyncio.Event) -> None:
        horizon_ms = int(self.cfg.evaluation_horizon_h * 3_600_000)
        while not stop.is_set():
            due_before = now_ms_fn() - horizon_ms
            for event_id, ev in self.store.pending_evaluations(due_before):
                try:
                    await self._settle(event_id, ev)
                except Exception as e:  # noqa: BLE001
                    log.warning("settle failed for event %s: %s", event_id, e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                pass

    async def _settle(self, event_id: int, ev: WhaleEvent) -> None:
        mark = await self.exchange.fetch_ticker_price(ev.symbol)
        if mark <= 0 or ev.vwap <= 0:
            return
        move_bps = (mark - ev.vwap) / ev.vwap * 1e4 * ev.side.sign
        win = move_bps >= self.cfg.win_threshold_bps
        loss = move_bps <= -self.cfg.loss_threshold_bps
        self.store.settle_event(event_id, move_bps, win, loss)
        self.store.bump_cohort(ev.cohort_id, ev.symbol, move_bps, win, loss)
        log.info(
            "settled event=%s cohort=%s sym=%s side=%s move=%.1fbps win=%s loss=%s",
            event_id, ev.cohort_id, ev.symbol, ev.side.value, move_bps, win, loss,
        )

    def cohort_quality(self, cohort_id: str, symbol: str) -> Optional[dict]:
        stats = self.store.cohort_stats(cohort_id, symbol)
        if stats is None:
            return None
        if stats["total"] < self.cfg.min_events_for_scoring:
            stats["qualified"] = False
        else:
            stats["qualified"] = True
        return stats
