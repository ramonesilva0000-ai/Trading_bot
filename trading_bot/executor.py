"""Order executor. Supports dry-run (paper) and live modes."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from .config import ExecutionConfig
from .exchange import Exchange
from .storage import Store
from .types import AccountState, Fill, Position, Side, Signal

log = logging.getLogger(__name__)


class Executor:
    def __init__(self, cfg: ExecutionConfig, exchange: Exchange, store: Store) -> None:
        self.cfg = cfg
        self.exchange = exchange
        self.store = store

    async def open_position(
        self,
        signal: Signal,
        amount: float,
        mark: float,
        stop_px: Optional[float],
        take_px: Optional[float],
        account: AccountState,
    ) -> Optional[Position]:
        if amount <= 0 or mark <= 0:
            return None

        price = self._limit_price(signal.side, mark)
        fill = await self._submit(signal.symbol, signal.side, amount, price)
        if fill is None:
            return None

        pos = Position(
            symbol=signal.symbol, side=signal.side,
            entry_price=fill.price, amount=fill.amount,
            opened_ms=fill.ts_ms or int(time.time() * 1000),
            stop_px=stop_px, take_px=take_px, order_id=fill.order_id,
        )
        account.positions[signal.symbol] = pos
        self.store.record_fill(
            pos.opened_ms, pos.symbol, pos.side.value, pos.entry_price, pos.amount,
            fill.fee, pos.order_id, meta={"reason": signal.reason, "phase": "open"},
        )
        log.info("OPEN %s %s amount=%.6f @ %.4f (%s)",
                 signal.symbol, signal.side.value, pos.amount, pos.entry_price, signal.reason)
        return pos

    async def close_position(self, pos: Position, mark: float,
                             account: AccountState, reason: str) -> Optional[Fill]:
        flip = pos.side.flip()
        price = self._limit_price(flip, mark)
        fill = await self._submit(pos.symbol, flip, pos.amount, price)
        if fill is None:
            return None
        account.positions.pop(pos.symbol, None)
        self.store.record_fill(
            fill.ts_ms or int(time.time() * 1000),
            pos.symbol, flip.value, fill.price, fill.amount, fill.fee, fill.order_id,
            meta={"reason": reason, "phase": "close"},
        )
        log.info("CLOSE %s %s amount=%.6f @ %.4f (%s)",
                 pos.symbol, flip.value, fill.amount, fill.price, reason)
        return fill

    def _limit_price(self, side: Side, mark: float) -> float:
        slip = self.cfg.max_slippage_bps / 1e4
        return mark * (1 + slip * side.sign)

    async def _submit(self, symbol: str, side: Side, amount: float,
                      price: float) -> Optional[Fill]:
        if self.cfg.dry_run:
            return Fill(symbol=symbol, side=side, price=price, amount=amount,
                        fee=0.0, ts_ms=int(time.time() * 1000), order_id=None)
        last_err: Optional[Exception] = None
        for attempt in range(self.cfg.retries + 1):
            try:
                return await self.exchange.create_order(
                    symbol, side, amount, price, self.cfg.order_type
                )
            except Exception as e:  # noqa: BLE001
                last_err = e
                wait = 0.5 * (2 ** attempt)
                log.warning("order attempt %d/%d failed: %s (retrying in %.1fs)",
                            attempt + 1, self.cfg.retries + 1, e, wait)
                await asyncio.sleep(wait)
        log.error("order submission exhausted retries: %s", last_err)
        return None

    async def enforce_stops(self, mark_map: dict[str, float], account: AccountState) -> None:
        for sym, pos in list(account.positions.items()):
            mark = mark_map.get(sym)
            if mark is None:
                continue
            hit_stop = pos.stop_px is not None and (
                (pos.side == Side.BUY and mark <= pos.stop_px) or
                (pos.side == Side.SELL and mark >= pos.stop_px)
            )
            hit_take = pos.take_px is not None and (
                (pos.side == Side.BUY and mark >= pos.take_px) or
                (pos.side == Side.SELL and mark <= pos.take_px)
            )
            if hit_stop:
                await self.close_position(pos, mark, account, "stop_loss")
            elif hit_take:
                await self.close_position(pos, mark, account, "take_profit")
