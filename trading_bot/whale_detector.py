"""Detect whale trades and cluster consecutive same-side prints into events.

The exchange public trade tape does not reveal wallets, but it does reveal
aggressor side and size. A "whale print" is a single executed trade whose
notional USD value exceeds a configured threshold. A "whale event" is a burst
of same-side whale prints occurring within a rolling time window.
"""

from __future__ import annotations

import logging
import uuid
from collections import deque
from typing import Deque, Iterator, Optional

from .config import WhaleDetectorConfig
from .profiler import profile_id_for
from .types import Side, Trade, WhaleEvent, WhalePrint

log = logging.getLogger(__name__)


class WhaleDetector:
    def __init__(self, cfg: WhaleDetectorConfig) -> None:
        self.cfg = cfg
        self._open: dict[tuple[str, Side], _OpenCluster] = {}

    def ingest(self, trade: Trade) -> Iterator[WhaleEvent]:
        """Feed a trade; yield zero or more completed whale events."""
        # Emit any clusters that have timed out, regardless of whether this
        # trade itself is a whale print.
        yield from self._flush_stale(trade.ts_ms)

        notional = trade.notional
        if notional < self.cfg.min_notional_usd:
            return

        print_ = WhalePrint(trade=trade, notional_usd=notional)
        key = (trade.symbol, trade.side)
        cluster = self._open.get(key)

        if cluster is None or trade.ts_ms - cluster.last_ms > self.cfg.cluster_window_s * 1000:
            if cluster is not None:
                ev = cluster.close()
                if ev is not None:
                    yield ev
            self._open[key] = _OpenCluster(
                symbol=trade.symbol,
                side=trade.side,
                min_prints=self.cfg.min_cluster_prints,
            )
            cluster = self._open[key]

        cluster.add(print_)

    def _flush_stale(self, now_ms: int) -> Iterator[WhaleEvent]:
        stale: list[tuple[str, Side]] = []
        window_ms = self.cfg.cluster_window_s * 1000
        for key, cluster in self._open.items():
            if now_ms - cluster.last_ms > window_ms:
                stale.append(key)
        for key in stale:
            cluster = self._open.pop(key)
            ev = cluster.close()
            if ev is not None:
                yield ev


class _OpenCluster:
    __slots__ = ("symbol", "side", "min_prints", "prints", "notional", "vwap_num",
                 "vwap_den", "first_ms", "last_ms", "min_print", "max_print")

    def __init__(self, symbol: str, side: Side, min_prints: int) -> None:
        self.symbol = symbol
        self.side = side
        self.min_prints = min_prints
        self.prints: Deque[WhalePrint] = deque()
        self.notional = 0.0
        self.vwap_num = 0.0
        self.vwap_den = 0.0
        self.first_ms = 0
        self.last_ms = 0
        self.min_print = float("inf")
        self.max_print = 0.0

    def add(self, p: WhalePrint) -> None:
        if not self.prints:
            self.first_ms = p.trade.ts_ms
        self.prints.append(p)
        self.notional += p.notional_usd
        self.vwap_num += p.trade.price * p.trade.amount
        self.vwap_den += p.trade.amount
        self.last_ms = p.trade.ts_ms
        if p.notional_usd < self.min_print:
            self.min_print = p.notional_usd
        if p.notional_usd > self.max_print:
            self.max_print = p.notional_usd

    def close(self) -> Optional[WhaleEvent]:
        if len(self.prints) < self.min_prints or self.vwap_den <= 0:
            return None
        ev = WhaleEvent(
            symbol=self.symbol,
            side=self.side,
            start_ms=self.first_ms,
            end_ms=self.last_ms,
            vwap=self.vwap_num / self.vwap_den,
            notional_usd=self.notional,
            prints=len(self.prints),
            cohort_id=_cohort_for(self.notional),
            min_print_notional=0.0 if self.min_print == float("inf") else self.min_print,
            max_print_notional=self.max_print,
        )
        ev.profile_id = profile_id_for(ev)
        return ev


def _cohort_for(notional_usd: float) -> str:
    """Bucket whale events by size so we can score cohorts separately.

    Without on-chain identity we cannot track individual wallets, but size
    buckets are a reasonable proxy: a $5M single-side burst behaves very
    differently from a $250K one.
    """
    if notional_usd >= 10_000_000:
        return "mega"
    if notional_usd >= 2_500_000:
        return "super"
    if notional_usd >= 1_000_000:
        return "large"
    return "base"


def new_event_id() -> str:
    return uuid.uuid4().hex
