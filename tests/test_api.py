import os

import pytest
from fastapi.testclient import TestClient

from trading_bot.api import create_app
from trading_bot.config import Config, ExchangeConfig, StorageConfig
from trading_bot.profiler import profile_id_for
from trading_bot.storage import Store
from trading_bot.types import Side, WhaleEvent


@pytest.fixture
def cfg(tmp_path):
    db = tmp_path / "test.sqlite"
    return Config(
        exchange=ExchangeConfig(id="binance", testnet=True),
        symbols=["BTC/USDT"],
        storage=StorageConfig(path=str(db)),
    )


def _seed(cfg):
    store = Store(cfg.storage.path)
    for _ in range(25):
        ev = WhaleEvent(symbol="BTC/USDT", side=Side.BUY, start_ms=0, end_ms=0,
                        vwap=100.0, notional_usd=800_000, prints=2,
                        cohort_id="base", min_print_notional=300_000,
                        max_print_notional=500_000)
        ev.profile_id = profile_id_for(ev)
        store.insert_event(ev)
        store.bump_profile(ev.profile_id, 50.0, True, False)
    store.close()


def test_health_does_not_require_auth(cfg):
    os.environ["API_KEY"] = "secret"
    try:
        client = TestClient(create_app(cfg))
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {"ok": True}
    finally:
        del os.environ["API_KEY"]


def test_status_when_not_started(cfg):
    os.environ.pop("API_KEY", None)
    client = TestClient(create_app(cfg))
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is False
    assert body["exchange"] == "binance"
    assert body["symbols"] == ["BTC/USDT"]
    assert body["positions"] == []


def test_auth_blocks_when_key_set(cfg):
    os.environ["API_KEY"] = "secret"
    try:
        client = TestClient(create_app(cfg))
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/status",
                          headers={"X-Api-Key": "wrong"}).status_code == 401
        assert client.get("/api/status",
                          headers={"X-Api-Key": "secret"}).status_code == 200
    finally:
        del os.environ["API_KEY"]


def test_traders_endpoint_returns_seeded_profile(cfg):
    os.environ.pop("API_KEY", None)
    _seed(cfg)
    client = TestClient(create_app(cfg))
    r = client.get("/api/traders?equity=10000")
    assert r.status_code == 200
    rows = r.json()
    assert rows
    row = rows[0]
    assert row["total"] >= 25
    assert "lot_notional_min" in row
    assert "trade_notional_min" in row
    assert any(p["symbol"] == "BTC/USDT" for p in row["pairs"])
    # No live mark in tests, so recommendation should be null.
    assert row["recommendation"] is None


def test_events_and_fills_endpoints(cfg):
    os.environ.pop("API_KEY", None)
    _seed(cfg)
    client = TestClient(create_app(cfg))
    r = client.get("/api/events?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 5
    assert body[0]["symbol"] == "BTC/USDT"

    r = client.get("/api/fills")
    assert r.status_code == 200
    assert r.json() == []


def test_pwa_shell_served(cfg):
    os.environ.pop("API_KEY", None)
    client = TestClient(create_app(cfg))
    r = client.get("/")
    assert r.status_code == 200
    assert "Trading Bot" in r.text
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert "Trading Bot" in r.text
    r = client.get("/sw.js")
    assert r.status_code == 200
