from trading_bot.config import RiskConfig, SignalsConfig
from trading_bot.profiler import profile_id_for, size_bucket, time_bucket
from trading_bot.recommend import recommend_for_profile
from trading_bot.risk import RiskManager
from trading_bot.storage import Store
from trading_bot.types import Side, WhaleEvent


def _event(notional, ts_ms=0, symbol="BTC/USDT", side=Side.BUY, vwap=100.0):
    ev = WhaleEvent(symbol=symbol, side=side, start_ms=ts_ms, end_ms=ts_ms,
                    vwap=vwap, notional_usd=notional, prints=1,
                    cohort_id="base", min_print_notional=notional,
                    max_print_notional=notional)
    ev.profile_id = profile_id_for(ev)
    return ev


def test_size_and_time_buckets():
    assert size_bucket(300_000) == "250k"
    assert size_bucket(900_000) == "500k"
    assert size_bucket(20_000_000) == "10m+"
    # ts 0 is 1970-01-01 00:00:00 UTC -> 00h bucket
    assert time_bucket(0) == "00h"


def test_profile_id_is_deterministic_for_same_features():
    a = _event(notional=800_000, ts_ms=0)
    b = _event(notional=900_000, ts_ms=1000)  # same bucket
    assert a.profile_id == b.profile_id


def test_storage_aggregates_profile_ranges_and_pairs(tmp_path):
    store = Store(str(tmp_path / "s.sqlite"))
    for notional, sym in [(800_000, "BTC/USDT"),
                          (600_000, "BTC/USDT"),
                          (900_000, "ETH/USDT")]:
        ev = _event(notional=notional, symbol=sym)
        store.insert_event(ev)
        store.bump_profile(ev.profile_id, outcome_bps=50.0, win=True, loss=False)

    # Grab the single profile they all share
    pid = _event(notional=800_000).profile_id
    agg = store.profile_aggregates(pid)
    assert agg is not None
    assert agg["events"] == 3
    assert agg["trade_notional_min"] == 600_000
    assert agg["trade_notional_max"] == 900_000
    symbols = [p["symbol"] for p in agg["pairs"]]
    assert symbols[0] == "BTC/USDT"
    assert "ETH/USDT" in symbols

    top = store.top_profiles(min_events=1, limit=5)
    assert top and top[0]["profile_id"] == pid


def test_recommendation_respects_caps(tmp_path):
    store = Store(str(tmp_path / "s.sqlite"))
    ev = _event(notional=800_000)
    for _ in range(10):
        store.insert_event(ev)
        store.bump_profile(ev.profile_id, 50.0, True, False)

    stats = store.top_profiles(min_events=5, limit=1)[0]
    risk = RiskManager(RiskConfig(max_position_pct=0.05, kelly_fraction=0.25,
                                  stop_loss_bps=80, take_profit_bps=120))
    rec = recommend_for_profile(
        profile_stats=stats, mark=100.0, equity_usd=10_000,
        risk=risk, signals_cfg=SignalsConfig(min_win_rate=0.6, min_expectancy_bps=5.0),
    )
    assert rec.approved
    # Never exceed the configured single-position cap of 5% equity.
    assert rec.equity_pct <= 0.05 + 1e-9
    assert rec.stop_px is not None and rec.take_px is not None
