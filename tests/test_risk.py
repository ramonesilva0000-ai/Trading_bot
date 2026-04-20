from trading_bot.config import RiskConfig
from trading_bot.risk import RiskManager
from trading_bot.types import AccountState, Side, Signal


def _sig(strength=1.0, expectancy_bps=20.0):
    return Signal(symbol="BTC/USDT", side=Side.BUY, strength=strength,
                  created_ms=0, ttl_s=60, expectancy_bps=expectancy_bps)


def test_approves_within_caps_and_produces_stops():
    rm = RiskManager(RiskConfig(max_position_pct=0.05, kelly_fraction=0.5,
                                stop_loss_bps=50, take_profit_bps=100))
    acct = AccountState(equity_usd=10_000, free_usd=10_000, day_start_equity=10_000)
    d = rm.size(_sig(), mark=100.0, account=acct)
    assert d.approved
    assert d.amount > 0
    assert d.stop_px is not None and d.stop_px < 100.0
    assert d.take_px is not None and d.take_px > 100.0


def test_halts_on_daily_drawdown():
    rm = RiskManager(RiskConfig(daily_drawdown_halt_pct=0.03))
    acct = AccountState(equity_usd=9_600, free_usd=9_600, day_start_equity=10_000)
    d = rm.size(_sig(), mark=100.0, account=acct)
    assert not d.approved
    assert d.reason == "daily_drawdown_halt"


def test_respects_gross_exposure_cap():
    rm = RiskManager(RiskConfig(max_gross_exposure_pct=0.10, max_position_pct=0.50))
    acct = AccountState(equity_usd=10_000, free_usd=0, day_start_equity=10_000)
    from trading_bot.types import Position
    acct.positions["ETH/USDT"] = Position(
        symbol="ETH/USDT", side=Side.BUY, entry_price=100, amount=10, opened_ms=0,
    )  # notional 1000 == 10% of equity == cap
    d = rm.size(_sig(), mark=100.0, account=acct)
    assert not d.approved
    assert d.reason == "gross_exposure_cap"
