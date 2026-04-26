"""HTTP + WebSocket API. Mobile PWA talks to this over the LAN.

The Runtime is started/stopped on demand inside the FastAPI event loop, so a
single uvicorn process owns the bot. SQLite WAL mode lets read endpoints
query the same DB while the writer (Runtime) is live.

Authentication: optional. If the API_KEY env var is set, every /api/* and /ws
request must carry `X-Api-Key: <value>`. Always set it if the API binds to
anything other than 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

from contextlib import asynccontextmanager

from fastapi import (
    Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect,
)
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Config
from .exchange import Exchange
from .recommend import recommend_for_profile
from .risk import RiskManager
from .runtime import Runtime
from .storage import Store
from .types import Side

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class _RuntimeManager:
    """Owns at most one Runtime task inside the API event loop."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._runtime: Optional[Runtime] = None
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def runtime(self) -> Optional[Runtime]:
        return self._runtime

    async def start(self) -> None:
        async with self._lock:
            if self.running:
                return
            self._runtime = Runtime(self.cfg)
            self._task = asyncio.create_task(self._runtime.run(), name="bot-runtime")

    async def stop(self) -> None:
        async with self._lock:
            if self._runtime is not None:
                self._runtime.stop()
            if self._task is not None:
                try:
                    await asyncio.wait_for(self._task, timeout=10.0)
                except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                    self._task.cancel()
            self._task = None
            self._runtime = None

    def status_dict(self) -> dict:
        rt = self._runtime
        positions = []
        equity = 0.0
        free = 0.0
        day_start = 0.0
        marks: dict[str, float] = {}
        if rt is not None:
            equity = rt.account.equity_usd
            free = rt.account.free_usd
            day_start = rt.account.day_start_equity
            marks = dict(rt._marks)
            for p in rt.account.positions.values():
                mark = marks.get(p.symbol, p.entry_price)
                positions.append({
                    "symbol": p.symbol,
                    "side": p.side.value,
                    "amount": p.amount,
                    "entry_price": p.entry_price,
                    "stop_px": p.stop_px,
                    "take_px": p.take_px,
                    "mark": mark,
                    "pnl_bps": p.pnl_bps(mark),
                })
        return {
            "running": self.running,
            "dry_run": self.cfg.execution.dry_run,
            "exchange": self.cfg.exchange.id,
            "market_type": self.cfg.exchange.market_type,
            "symbols": self.cfg.symbols,
            "equity_usd": equity,
            "free_usd": free,
            "day_start_equity": day_start,
            "marks": marks,
            "positions": positions,
        }


def create_app(cfg: Config) -> FastAPI:
    mgr = _RuntimeManager(cfg)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        try:
            yield
        finally:
            await mgr.stop()

    app = FastAPI(title="Trading Bot", version="0.1.0",
                  docs_url=None, redoc_url=None, lifespan=_lifespan)
    api_key = os.getenv("API_KEY", "").strip()

    def _auth(x_api_key: str = Header("", alias="X-Api-Key")) -> None:
        if not api_key:
            return  # auth disabled (loopback / dev only)
        if x_api_key != api_key:
            raise HTTPException(status_code=401, detail="invalid api key")

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True}

    @app.get("/api/status", dependencies=[Depends(_auth)])
    async def status() -> dict:
        return mgr.status_dict()

    @app.post("/api/start", dependencies=[Depends(_auth)])
    async def start() -> dict:
        await mgr.start()
        return mgr.status_dict()

    @app.post("/api/stop", dependencies=[Depends(_auth)])
    async def stop() -> dict:
        await mgr.stop()
        return mgr.status_dict()

    @app.get("/api/config", dependencies=[Depends(_auth)])
    async def get_config() -> dict:
        c = cfg.model_dump()
        c["exchange"].pop("api_key", None)
        c["exchange"].pop("api_secret", None)
        c["exchange"].pop("api_passphrase", None)
        return c

    @app.get("/api/traders", dependencies=[Depends(_auth)])
    async def traders(
        limit: int = Query(20, ge=1, le=100),
        equity: float = Query(0.0, ge=0.0),
    ) -> list[dict]:
        store = Store(cfg.storage.path)
        risk = RiskManager(cfg.risk)
        try:
            rows = store.top_profiles(
                min_events=cfg.whale_tracker.min_events_for_scoring, limit=limit,
            )
        finally:
            store.close()

        eq = equity if equity > 0 else (mgr.runtime.account.equity_usd
                                        if mgr.runtime else 0.0)
        if eq <= 0:
            eq = 10_000.0

        marks = mgr.runtime._marks if mgr.runtime else {}
        for r in rows:
            ref = r["pairs"][0]["symbol"] if r["pairs"] else ""
            mark = marks.get(ref, 0.0)
            if mark > 0:
                rec = recommend_for_profile(
                    profile_stats=r, mark=mark, equity_usd=eq,
                    risk=risk, signals_cfg=cfg.signals, side=Side.BUY,
                )
                r["recommendation"] = {
                    "approved": rec.approved,
                    "reason": rec.reason,
                    "usd_notional": rec.usd_notional,
                    "amount": rec.amount,
                    "equity_pct": rec.equity_pct,
                    "stop_px": rec.stop_px,
                    "take_px": rec.take_px,
                    "mark": mark,
                    "ref_symbol": ref,
                }
            else:
                r["recommendation"] = None
        return rows

    @app.get("/api/cohorts", dependencies=[Depends(_auth)])
    async def cohorts() -> list[dict]:
        store = Store(cfg.storage.path)
        try:
            return list(store.all_cohorts())
        finally:
            store.close()

    @app.get("/api/events", dependencies=[Depends(_auth)])
    async def events(limit: int = Query(50, ge=1, le=500)) -> list[dict]:
        return _query_events(cfg.storage.path, limit)

    @app.get("/api/fills", dependencies=[Depends(_auth)])
    async def fills(limit: int = Query(50, ge=1, le=500)) -> list[dict]:
        return _query_fills(cfg.storage.path, limit)

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        if api_key:
            token = websocket.query_params.get("api_key", "")
            if token != api_key:
                await websocket.close(code=4401)
                return
        await websocket.accept()
        try:
            while True:
                payload = json.dumps({"type": "status", "data": mgr.status_dict()})
                await websocket.send_text(payload)
                await asyncio.sleep(2.0)
        except WebSocketDisconnect:
            return
        except Exception as e:  # noqa: BLE001
            log.warning("ws error: %s", e)

    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    else:
        @app.get("/")
        async def _root() -> JSONResponse:
            return JSONResponse({"detail": "web/ not found"}, status_code=404)

    return app


def _query_events(db_path: str, limit: int) -> list[dict]:
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """SELECT id, symbol, side, start_ms, end_ms, vwap, notional_usd,
                      prints, cohort_id, profile_id, min_print_notional,
                      max_print_notional, outcome_bps, evaluated
               FROM whale_events ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    cols = ["id", "symbol", "side", "start_ms", "end_ms", "vwap", "notional_usd",
            "prints", "cohort_id", "profile_id", "min_print_notional",
            "max_print_notional", "outcome_bps", "evaluated"]
    return [dict(zip(cols, r)) for r in rows]


def _query_fills(db_path: str, limit: int) -> list[dict]:
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """SELECT id, ts_ms, symbol, side, price, amount, fee, order_id, meta
               FROM fills ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    cols = ["id", "ts_ms", "symbol", "side", "price", "amount", "fee",
            "order_id", "meta"]
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        if d.get("meta"):
            try:
                d["meta"] = json.loads(d["meta"])
            except Exception:  # noqa: BLE001
                pass
        out.append(d)
    return out
