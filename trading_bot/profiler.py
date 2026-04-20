"""Derive a stable trader-profile fingerprint from a whale event.

Public exchange tapes don't expose wallet identity, so "trader" here is a
behavioral signature, not a person. Grouping by (notional size bucket,
UTC time-of-day bucket) collapses repeating actors — desks, market-making
bots, large retail — into profiles that can be scored independently and
reported with their pair preferences, lot-size ranges, and notional ranges.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .types import WhaleEvent

_SIZE_EDGES: tuple[tuple[float, str], ...] = (
    (500_000,     "250k"),
    (1_000_000,   "500k"),
    (2_500_000,   "1m"),
    (5_000_000,   "2p5m"),
    (10_000_000,  "5m"),
)
_SIZE_TOP = "10m+"

_HOUR_BUCKET = 4  # 4-hour blocks → 6 buckets per UTC day


def size_bucket(notional_usd: float) -> str:
    for edge, label in _SIZE_EDGES:
        if notional_usd < edge:
            return label
    return _SIZE_TOP


def time_bucket(ts_ms: int) -> str:
    hour = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).hour
    block_start = (hour // _HOUR_BUCKET) * _HOUR_BUCKET
    return f"{block_start:02d}h"


def profile_id_for(ev: WhaleEvent) -> str:
    return f"{size_bucket(ev.notional_usd)}@{time_bucket(ev.start_ms)}"
