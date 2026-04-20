from trading_bot.config import SignalsConfig, WhaleTrackerConfig
from trading_bot.signals import SignalEngine
from trading_bot.storage import Store
from trading_bot.types import Side, WhaleEvent
from trading_bot.whale_tracker import WhaleTracker


class _FakeExchange:
    async def fetch_ticker_price(self, symbol):  # pragma: no cover - unused here
        return 0.0


def _event(cohort="base"):
    return WhaleEvent(symbol="BTC/USDT", side=Side.BUY, start_ms=0, end_ms=0,
                     vwap=100.0, notional_usd=500_000, prints=1, cohort_id=cohort)


def test_no_signal_without_history(tmp_path):
    store = Store(str(tmp_path / "s.sqlite"))
    tracker = WhaleTracker(WhaleTrackerConfig(min_events_for_scoring=5),
                           store, _FakeExchange())
    engine = SignalEngine(SignalsConfig(), tracker)
    assert engine.from_event(_event()) is None


def test_signal_fires_for_qualified_cohort(tmp_path):
    store = Store(str(tmp_path / "s.sqlite"))
    # Seed 10 wins at +50bps for cohort=base / BTC/USDT
    for _ in range(10):
        store.bump_cohort("base", "BTC/USDT", outcome_bps=50.0, win=True, loss=False)
    tracker = WhaleTracker(
        WhaleTrackerConfig(min_events_for_scoring=5, win_threshold_bps=40),
        store, _FakeExchange(),
    )
    engine = SignalEngine(
        SignalsConfig(min_win_rate=0.6, min_expectancy_bps=5.0, signal_ttl_s=60),
        tracker,
    )
    sig = engine.from_event(_event())
    assert sig is not None
    assert sig.side is Side.BUY
    assert sig.strength > 0
    assert "cohort=base" in sig.reason


def test_signal_rejected_low_winrate(tmp_path):
    store = Store(str(tmp_path / "s.sqlite"))
    # 5 wins, 5 losses -> wr 50%
    for _ in range(5):
        store.bump_cohort("base", "BTC/USDT", 50.0, True, False)
    for _ in range(5):
        store.bump_cohort("base", "BTC/USDT", -60.0, False, True)
    tracker = WhaleTracker(
        WhaleTrackerConfig(min_events_for_scoring=5), store, _FakeExchange()
    )
    engine = SignalEngine(SignalsConfig(min_win_rate=0.6), tracker)
    assert engine.from_event(_event()) is None
