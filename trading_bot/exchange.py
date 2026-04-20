"""ccxt-based multi-exchange adapter.

Works with any ccxt exchange (Binance, Bybit, OKX, Kraken, Coinbase, KuCoin,
Bitget, ...). Streaming uses ccxt.pro when installed; otherwise it falls back
to polling REST trades, which is good enough for a whale detector that cares
about large prints and does not need microsecond granularity.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator, Iterable, Optional

from .config import ExchangeConfig
from .types import Fill, Side, Trade

log = logging.getLogger(__name__)


class Exchange:
    def __init__(self, cfg: ExchangeConfig) -> None:
        self.cfg = cfg
        self._client = self._build_client()
        self._pro = self._build_pro_client()

    def _ccxt_params(self) -> dict:
        params = {
            "apiKey": self.cfg.api_key,
            "secret": self.cfg.api_secret,
            "enableRateLimit": True,
            "options": {},
        }
        if self.cfg.api_passphrase:
            params["password"] = self.cfg.api_passphrase
        if self.cfg.market_type == "future":
            params["options"]["defaultType"] = "future"
        elif self.cfg.market_type == "margin":
            params["options"]["defaultType"] = "margin"
        else:
            params["options"]["defaultType"] = "spot"
        return params

    def _build_client(self):
        import ccxt  # type: ignore

        klass = getattr(ccxt, self.cfg.id, None)
        if klass is None:
            raise ValueError(f"Unknown ccxt exchange id: {self.cfg.id}")
        client = klass(self._ccxt_params())
        if self.cfg.testnet and hasattr(client, "set_sandbox_mode"):
            try:
                client.set_sandbox_mode(True)
            except Exception as e:  # noqa: BLE001
                log.warning("Sandbox mode unavailable for %s: %s", self.cfg.id, e)
        return client

    def _build_pro_client(self):
        try:
            import ccxt.pro as ccxtpro  # type: ignore
        except Exception:
            return None
        klass = getattr(ccxtpro, self.cfg.id, None)
        if klass is None:
            return None
        client = klass(self._ccxt_params())
        if self.cfg.testnet and hasattr(client, "set_sandbox_mode"):
            try:
                client.set_sandbox_mode(True)
            except Exception:  # noqa: BLE001
                pass
        return client

    async def close(self) -> None:
        for c in (self._pro, self._client):
            if c is None:
                continue
            close = getattr(c, "close", None)
            if close is None:
                continue
            try:
                res = close()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:  # noqa: BLE001
                pass

    async def fetch_ticker_price(self, symbol: str) -> float:
        t = await asyncio.to_thread(self._client.fetch_ticker, symbol)
        return float(t.get("last") or t.get("close") or 0.0)

    async def fetch_balance_usd(self, quote: str = "USDT") -> float:
        bal = await asyncio.to_thread(self._client.fetch_balance)
        total = bal.get("total", {}) or {}
        return float(total.get(quote, 0.0))

    async def stream_trades(self, symbols: Iterable[str]) -> AsyncIterator[Trade]:
        """Yield live trades for every given symbol."""
        symbols = list(symbols)
        if self._pro is not None and hasattr(self._pro, "watch_trades"):
            async for t in self._stream_pro(symbols):
                yield t
        else:
            async for t in self._stream_poll(symbols):
                yield t

    async def _stream_pro(self, symbols: list[str]) -> AsyncIterator[Trade]:
        queue: asyncio.Queue[Trade] = asyncio.Queue(maxsize=10_000)

        async def pump(sym: str) -> None:
            while True:
                try:
                    batch = await self._pro.watch_trades(sym)
                except Exception as e:  # noqa: BLE001
                    log.warning("watch_trades(%s) error: %s", sym, e)
                    await asyncio.sleep(1.0)
                    continue
                for raw in batch:
                    tr = _parse_ccxt_trade(sym, raw)
                    if tr is not None:
                        try:
                            queue.put_nowait(tr)
                        except asyncio.QueueFull:
                            pass

        tasks = [asyncio.create_task(pump(s)) for s in symbols]
        try:
            while True:
                yield await queue.get()
        finally:
            for t in tasks:
                t.cancel()

    async def _stream_poll(self, symbols: list[str]) -> AsyncIterator[Trade]:
        last_id: dict[str, Optional[str]] = {s: None for s in symbols}
        last_ts: dict[str, int] = {s: int(time.time() * 1000) for s in symbols}
        while True:
            for sym in symbols:
                try:
                    raw_list = await asyncio.to_thread(
                        self._client.fetch_trades, sym, last_ts[sym]
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("fetch_trades(%s) error: %s", sym, e)
                    raw_list = []
                for raw in raw_list:
                    tid = str(raw.get("id") or "")
                    if last_id[sym] and tid and tid == last_id[sym]:
                        continue
                    tr = _parse_ccxt_trade(sym, raw)
                    if tr is None:
                        continue
                    last_ts[sym] = max(last_ts[sym], tr.ts_ms)
                    last_id[sym] = tid or last_id[sym]
                    yield tr
            await asyncio.sleep(max(self.cfg.rate_limit_ms, 200) / 1000.0)

    async def create_order(
        self,
        symbol: str,
        side: Side,
        amount: float,
        price: Optional[float],
        order_type: str,
    ) -> Fill:
        params: dict = {}
        ctype = "limit" if order_type == "limit_postonly" else "market"
        if order_type == "limit_postonly":
            params["postOnly"] = True
        order = await asyncio.to_thread(
            self._client.create_order, symbol, ctype, side.value, amount, price, params
        )
        return Fill(
            symbol=symbol,
            side=side,
            price=float(order.get("average") or order.get("price") or price or 0.0),
            amount=float(order.get("filled") or amount),
            fee=float((order.get("fee") or {}).get("cost") or 0.0),
            ts_ms=int(order.get("timestamp") or time.time() * 1000),
            order_id=str(order.get("id") or ""),
        )


def _parse_ccxt_trade(symbol: str, raw: dict) -> Optional[Trade]:
    try:
        price = float(raw["price"])
        amount = float(raw["amount"])
        side_raw = (raw.get("side") or "").lower()
        if side_raw not in ("buy", "sell"):
            return None
        ts = int(raw.get("timestamp") or time.time() * 1000)
        return Trade(
            symbol=symbol,
            price=price,
            amount=amount,
            side=Side(side_raw),
            ts_ms=ts,
            trade_id=str(raw.get("id") or "") or None,
        )
    except (KeyError, TypeError, ValueError):
        return None
