"""Translate a trader profile into a concrete sizing recommendation.

Given a profile's realized win-rate / expectancy and the configured risk
policy, this computes the position size the bot *would* take if the profile
fired a signal right now. It is not advice — it is "if you were to follow
this profile under your configured caps, here is what the risk manager would
approve."
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from .config import SignalsConfig
from .risk import RiskManager
from .types import AccountState, Side, Signal


@dataclass
class SizingRecommendation:
    approved: bool
    reason: str
    usd_notional: float
    amount: float
    equity_pct: float
    stop_px: Optional[float]
    take_px: Optional[float]


def recommend_for_profile(
    profile_stats: dict,
    mark: float,
    equity_usd: float,
    risk: RiskManager,
    signals_cfg: SignalsConfig,
    side: Side = Side.BUY,
) -> SizingRecommendation:
    wr = profile_stats.get("win_rate", 0.0)
    exp_bps = profile_stats.get("expectancy_bps", 0.0)
    strength = min(1.0, max(0.0,
        (wr - signals_cfg.min_win_rate) / max(1e-6, 1.0 - signals_cfg.min_win_rate)
    ))
    sig = Signal(
        symbol="<profile>", side=side, strength=strength,
        created_ms=int(time.time() * 1000),
        ttl_s=signals_cfg.signal_ttl_s,
        expectancy_bps=exp_bps,
    )
    account = AccountState(equity_usd=equity_usd, free_usd=equity_usd,
                           day_start_equity=equity_usd)
    decision = risk.size(sig, mark=mark, account=account)
    notional = decision.amount * mark if decision.approved else 0.0
    return SizingRecommendation(
        approved=decision.approved,
        reason=decision.reason,
        usd_notional=notional,
        amount=decision.amount if decision.approved else 0.0,
        equity_pct=(notional / equity_usd) if equity_usd > 0 else 0.0,
        stop_px=decision.stop_px,
        take_px=decision.take_px,
    )
