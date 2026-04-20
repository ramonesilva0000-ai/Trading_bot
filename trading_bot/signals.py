"""Turn whale events into trading signals, gated by cohort quality."""

from __future__ import annotations

import logging
import time
from typing import Optional

from .config import SignalsConfig
from .types import Signal, WhaleEvent
from .whale_tracker import WhaleTracker

log = logging.getLogger(__name__)


class SignalEngine:
    def __init__(self, cfg: SignalsConfig, tracker: WhaleTracker) -> None:
        self.cfg = cfg
        self.tracker = tracker

    def from_event(self, ev: WhaleEvent) -> Optional[Signal]:
        source = "profile"
        stats = self.tracker.profile_quality(ev.profile_id)
        if stats is None or not stats.get("qualified"):
            source = "cohort"
            stats = self.tracker.cohort_quality(ev.cohort_id, ev.symbol)
        if stats is None or not stats.get("qualified"):
            return None
        if stats["win_rate"] < self.cfg.min_win_rate:
            return None
        if stats["expectancy_bps"] < self.cfg.min_expectancy_bps:
            return None
        strength = min(1.0, max(0.0,
            (stats["win_rate"] - self.cfg.min_win_rate) / max(1e-6, 1.0 - self.cfg.min_win_rate)
        ))
        label = ev.profile_id if source == "profile" else ev.cohort_id
        return Signal(
            symbol=ev.symbol,
            side=ev.side,
            strength=strength,
            created_ms=int(time.time() * 1000),
            ttl_s=self.cfg.signal_ttl_s,
            reason=(f"{source}={label} wr={stats['win_rate']:.2f} "
                    f"exp={stats['expectancy_bps']:.1f}bps n={stats['total']}"),
            expectancy_bps=stats["expectancy_bps"],
        )

    @staticmethod
    def is_fresh(sig: Signal, now_ms: Optional[int] = None) -> bool:
        now_ms = now_ms or int(time.time() * 1000)
        return (now_ms - sig.created_ms) <= sig.ttl_s * 1000
