import csv
from pathlib import Path

from trading_bot.backtest import run_backtest
from trading_bot.config import Config, WhaleDetectorConfig, WhaleTrackerConfig
from trading_bot.storage import Store


def _write_tape(path: Path) -> None:
    rows = []
    base_price = 100.0
    # Whale buy cluster at ts=1000
    rows.append((1000, "X/USDT", base_price, 5_000, "buy"))   # $500k
    rows.append((1200, "X/USDT", base_price, 4_000, "buy"))   # $400k
    # small filler trades moving the price up over 1 hour (3_600_000 ms)
    for i in range(1, 11):
        rows.append((1000 + i * 400_000, "X/USDT", base_price * (1 + 0.001 * i), 1, "buy"))
    # Final trade past evaluation horizon to trigger settlement
    rows.append((1000 + 4_000_000, "X/USDT", base_price * 1.02, 1, "buy"))

    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts_ms", "symbol", "price", "amount", "side"])
        for r in rows:
            w.writerow(r)


def test_backtest_settles_events(tmp_path):
    tape = tmp_path / "tape.csv"
    _write_tape(tape)
    cfg = Config(
        symbols=["X/USDT"],
        whale_detector=WhaleDetectorConfig(min_notional_usd=250_000,
                                           cluster_window_s=1),
        whale_tracker=WhaleTrackerConfig(evaluation_horizon_h=1.0,
                                         win_threshold_bps=40, loss_threshold_bps=40,
                                         min_events_for_scoring=1),
    )
    store = Store(str(tmp_path / "s.sqlite"))
    try:
        result = run_backtest(cfg, tape, store)
    finally:
        store.close()
    assert result.events >= 1
    assert (result.wins + result.losses + result.neutrals) >= 1
