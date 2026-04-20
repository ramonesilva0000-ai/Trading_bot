"""Shared domain types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    def flip(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


@dataclass(frozen=True)
class Trade:
    symbol: str
    price: float
    amount: float
    side: Side
    ts_ms: int
    trade_id: Optional[str] = None

    @property
    def notional(self) -> float:
        return self.price * self.amount


@dataclass(frozen=True)
class WhalePrint:
    """A single trade large enough to count as a whale print."""

    trade: Trade
    notional_usd: float


@dataclass
class WhaleEvent:
    """A clustered group of whale prints on the same side within a window."""

    symbol: str
    side: Side
    start_ms: int
    end_ms: int
    vwap: float
    notional_usd: float
    prints: int
    cohort_id: str = "default"
    profile_id: str = "default"
    min_print_notional: float = 0.0
    max_print_notional: float = 0.0
    outcome_bps: Optional[float] = None  # realized move in bps (signed by side)
    evaluated: bool = False


@dataclass
class Signal:
    symbol: str
    side: Side
    strength: float  # 0..1
    created_ms: int
    ttl_s: float
    reason: str = ""
    expectancy_bps: float = 0.0


@dataclass
class Position:
    symbol: str
    side: Side
    entry_price: float
    amount: float
    opened_ms: int
    stop_px: Optional[float] = None
    take_px: Optional[float] = None
    order_id: Optional[str] = None

    @property
    def notional(self) -> float:
        return self.entry_price * self.amount

    def pnl_bps(self, mark: float) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (mark - self.entry_price) / self.entry_price * 1e4 * self.side.sign


@dataclass
class Fill:
    symbol: str
    side: Side
    price: float
    amount: float
    fee: float = 0.0
    ts_ms: int = 0
    order_id: Optional[str] = None


@dataclass
class AccountState:
    equity_usd: float
    free_usd: float
    positions: dict[str, Position] = field(default_factory=dict)
    day_start_equity: float = 0.0
