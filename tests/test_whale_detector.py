from trading_bot.config import WhaleDetectorConfig
from trading_bot.types import Side, Trade
from trading_bot.whale_detector import WhaleDetector, _cohort_for


def _t(price, amount, side, ts_ms, sym="BTC/USDT"):
    return Trade(symbol=sym, price=price, amount=amount,
                 side=Side(side), ts_ms=ts_ms)


def test_ignores_small_trades():
    det = WhaleDetector(WhaleDetectorConfig(min_notional_usd=250_000))
    events = list(det.ingest(_t(50_000, 1.0, "buy", 1_000)))  # $50k
    assert events == []


def test_clusters_same_side_prints_within_window():
    det = WhaleDetector(
        WhaleDetectorConfig(min_notional_usd=100_000, cluster_window_s=5)
    )
    assert list(det.ingest(_t(50_000, 5, "buy", 1_000))) == []   # $250k
    assert list(det.ingest(_t(50_100, 4, "buy", 2_000))) == []   # $200k, 1s later
    # 10s later any trade triggers the stale-cluster flush.
    events = list(det.ingest(_t(50_200, 1, "sell", 12_000)))
    assert len(events) == 1
    ev = events[0]
    assert ev.side is Side.BUY
    assert ev.prints == 2
    assert ev.notional_usd > 400_000


def test_stale_cluster_flushes_on_next_trade():
    det = WhaleDetector(
        WhaleDetectorConfig(min_notional_usd=100_000, cluster_window_s=1)
    )
    list(det.ingest(_t(50_000, 5, "buy", 1_000)))
    # 10s later, same symbol/side but outside the window -> old one flushed.
    events = list(det.ingest(_t(50_500, 5, "buy", 11_000)))
    assert len(events) == 1
    assert events[0].prints == 1


def test_cohort_buckets_monotonic():
    assert _cohort_for(500_000) == "base"
    assert _cohort_for(1_200_000) == "large"
    assert _cohort_for(3_000_000) == "super"
    assert _cohort_for(15_000_000) == "mega"
