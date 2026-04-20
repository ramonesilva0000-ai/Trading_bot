"""Risk manager: position sizing, stops/targets, drawdown halts."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .config import RiskConfig
from .types import AccountState, Side, Signal

log = logging.getLogger(__name__)


@dataclass
class RiskDecision:
    approved: bool
    amount: float = 0.0
    stop_px: Optional[float] = None
    take_px: Optional[float] = None
    reason: str = ""


class RiskManager:
    def __init__(self, cfg: RiskConfig) -> None:
        self.cfg = cfg
        self._halted_day = False

    def reset_day(self, equity_now: float, account: AccountState) -> None:
        account.day_start_equity = equity_now
        self._halted_day = False

    def check_drawdown(self, account: AccountState) -> bool:
        """Return True if trading should be halted for the day."""
        if account.day_start_equity <= 0:
            return False
        dd = (account.day_start_equity - account.equity_usd) / account.day_start_equity
        if dd >= self.cfg.daily_drawdown_halt_pct:
            if not self._halted_day:
                log.warning("daily drawdown %.2f%% >= halt %.2f%%, halting",
                            dd * 100, self.cfg.daily_drawdown_halt_pct * 100)
            self._halted_day = True
        return self._halted_day

    def size(self, signal: Signal, mark: float, account: AccountState) -> RiskDecision:
        if self.check_drawdown(account):
            return RiskDecision(approved=False, reason="daily_drawdown_halt")
        if mark <= 0 or account.equity_usd <= 0:
            return RiskDecision(approved=False, reason="invalid_state")

        gross = sum(p.notional for p in account.positions.values())
        gross_cap = account.equity_usd * self.cfg.max_gross_exposure_pct
        room = max(0.0, gross_cap - gross)
        if room <= 0:
            return RiskDecision(approved=False, reason="gross_exposure_cap")

        kelly_frac = self._kelly_fraction(signal)
        alloc_pct = min(self.cfg.max_position_pct, kelly_frac * signal.strength)
        alloc_usd = min(account.equity_usd * alloc_pct, room)
        if alloc_usd <= 0:
            return RiskDecision(approved=False, reason="no_allocation")

        amount = alloc_usd / mark
        stop_px, take_px = self._stops(signal.side, mark)
        return RiskDecision(
            approved=True, amount=amount, stop_px=stop_px, take_px=take_px,
            reason=f"alloc_usd={alloc_usd:.2f} kelly_frac={kelly_frac:.3f}",
        )

    def _kelly_fraction(self, signal: Signal) -> float:
        # Kelly ~ edge / odds; with symmetric stop/target, odds = take/stop.
        edge_bps = signal.expectancy_bps
        odds = max(1e-6, self.cfg.take_profit_bps / max(1.0, self.cfg.stop_loss_bps))
        kelly = (edge_bps / 1e4) / odds
        kelly = max(0.0, min(1.0, kelly))
        return kelly * self.cfg.kelly_fraction

    def _stops(self, side: Side, entry: float) -> tuple[float, float]:
        sl = entry * (1 - self.cfg.stop_loss_bps / 1e4 * side.sign)
        tp = entry * (1 + self.cfg.take_profit_bps / 1e4 * side.sign)
        return sl, tp
