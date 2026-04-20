"""Async runtime that wires detector, tracker, signals, risk, and executor."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from .config import Config
from .exchange import Exchange
from .executor import Executor
from .risk import RiskManager
from .signals import SignalEngine
from .storage import Store
from .types import AccountState, Signal
from .whale_detector import WhaleDetector
from .whale_tracker import WhaleTracker

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.store = Store(cfg.storage.path)
        self.exchange = Exchange(cfg.exchange)
        self.detector = WhaleDetector(cfg.whale_detector)
        self.tracker = WhaleTracker(cfg.whale_tracker, self.store, self.exchange)
        self.signals = SignalEngine(cfg.signals, self.tracker)
        self.risk = RiskManager(cfg.risk)
        self.executor = Executor(cfg.execution, self.exchange, self.store)
        self.account = AccountState(equity_usd=0.0, free_usd=0.0)
        self._stop = asyncio.Event()
        self._marks: dict[str, float] = {}

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    async def _refresh_account(self) -> None:
        try:
            eq = await self.exchange.fetch_balance_usd()
        except Exception as e:  # noqa: BLE001
            log.debug("fetch_balance_usd failed (dry_run=%s): %s",
                      self.cfg.execution.dry_run, e)
            eq = self.account.equity_usd or 10_000.0
        self.account.equity_usd = eq
        self.account.free_usd = eq - sum(p.notional for p in self.account.positions.values())
        if self.account.day_start_equity <= 0:
            self.risk.reset_day(eq, self.account)

    async def _account_loop(self) -> None:
        while not self._stop.is_set():
            await self._refresh_account()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                pass

    async def _mark_loop(self) -> None:
        while not self._stop.is_set():
            for sym in self.cfg.symbols:
                try:
                    self._marks[sym] = await self.exchange.fetch_ticker_price(sym)
                except Exception as e:  # noqa: BLE001
                    log.debug("mark %s: %s", sym, e)
            await self.executor.enforce_stops(self._marks, self.account)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    async def _stream_loop(self) -> None:
        async for trade in self.exchange.stream_trades(self.cfg.symbols):
            if self._stop.is_set():
                break
            for event in self.detector.ingest(trade):
                self.store.insert_event(event)
                sig = self.signals.from_event(event)
                if sig is not None:
                    await self._handle_signal(sig)

    async def _handle_signal(self, sig: Signal) -> None:
        if sig.symbol in self.account.positions:
            log.debug("signal ignored; already in position on %s", sig.symbol)
            return
        mark = self._marks.get(sig.symbol)
        if mark is None or mark <= 0:
            try:
                mark = await self.exchange.fetch_ticker_price(sig.symbol)
            except Exception:  # noqa: BLE001
                return
        decision = self.risk.size(sig, mark, self.account)
        if not decision.approved:
            log.info("signal skipped %s: %s", sig.symbol, decision.reason)
            return
        await self.executor.open_position(
            sig, decision.amount, mark, decision.stop_px, decision.take_px, self.account,
        )

    async def run(self) -> None:
        await self._refresh_account()
        tasks = [
            asyncio.create_task(self._account_loop(), name="account"),
            asyncio.create_task(self._mark_loop(), name="mark"),
            asyncio.create_task(
                self.tracker.run(self._now_ms, self._stop), name="tracker"
            ),
            asyncio.create_task(self._stream_loop(), name="stream"),
        ]
        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            await self.exchange.close()
            self.store.close()
