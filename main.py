"""CLI entrypoint for the whale-tracking trading bot."""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path

import typer
from dotenv import load_dotenv

from trading_bot.backtest import run_backtest
from trading_bot.config import Config
from trading_bot.exchange import Exchange
from trading_bot.logger import setup_logging
from trading_bot.recommend import recommend_for_profile
from trading_bot.risk import RiskManager
from trading_bot.runtime import Runtime
from trading_bot.storage import Store

app = typer.Typer(add_completion=False, help="Whale-tracking trading bot.")


@app.command()
def live(
    config: Path = typer.Option("config.yaml", help="Path to config YAML."),
) -> None:
    """Run the bot against a live exchange (honors execution.dry_run)."""
    load_dotenv()
    cfg = Config.load(config)
    setup_logging(cfg.logging.level, cfg.logging.file)
    runtime = Runtime(cfg)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _stop(*_):
        runtime.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(runtime.run())
    finally:
        loop.close()


@app.command()
def backtest(
    csv_path: Path = typer.Argument(..., help="CSV of historical trades."),
    config: Path = typer.Option("config.yaml", help="Path to config YAML."),
) -> None:
    """Replay a historical trade CSV and report cohort performance."""
    load_dotenv()
    cfg = Config.load(config)
    setup_logging(cfg.logging.level, cfg.logging.file)
    store = Store(cfg.storage.path)
    try:
        result = run_backtest(cfg, csv_path, store)
    finally:
        store.close()
    typer.echo(
        f"events={result.events} wins={result.wins} losses={result.losses} "
        f"neutrals={result.neutrals} win_rate={result.win_rate:.2%} "
        f"expectancy={result.expectancy_bps:.2f}bps"
    )


@app.command()
def cohorts(
    config: Path = typer.Option("config.yaml", help="Path to config YAML."),
    symbol: str = typer.Option("", help="Filter to a specific symbol."),
) -> None:
    """Print current whale cohort statistics from the local store."""
    load_dotenv()
    cfg = Config.load(config)
    store = Store(cfg.storage.path)
    try:
        for row in store.all_cohorts(symbol or None):
            typer.echo(
                f"{row['symbol']:<12} cohort={row['cohort_id']:<6} "
                f"n={row['total']:<5} wr={row['win_rate']:.2%} "
                f"exp={row['expectancy_bps']:.2f}bps "
                f"W/L/N={row['wins']}/{row['losses']}/{row['neutrals']}"
            )
    finally:
        store.close()


@app.command()
def traders(
    config: Path = typer.Option("config.yaml", help="Path to config YAML."),
    limit: int = typer.Option(10, help="Number of profiles to show."),
    equity: float = typer.Option(
        0.0,
        help="Account equity in USD for the sizing recommendation. "
             "0 = fetch from exchange balance.",
    ),
    mark_symbol: str = typer.Option(
        "",
        help="Symbol used to fetch a reference mark for the sizing preview. "
             "Defaults to the profile's most-traded pair.",
    ),
) -> None:
    """Rank trader profiles and report lot/notional ranges and suggested size."""
    import asyncio

    load_dotenv()
    cfg = Config.load(config)
    setup_logging(cfg.logging.level, cfg.logging.file)
    store = Store(cfg.storage.path)
    exchange = Exchange(cfg.exchange)
    risk = RiskManager(cfg.risk)

    async def _run() -> None:
        try:
            eq = equity
            if eq <= 0:
                try:
                    eq = await exchange.fetch_balance_usd()
                except Exception:  # noqa: BLE001
                    eq = 0.0
            if eq <= 0:
                eq = 10_000.0  # sensible default for the preview

            rows = store.top_profiles(
                min_events=cfg.whale_tracker.min_events_for_scoring,
                limit=limit,
            )
            if not rows:
                typer.echo(
                    "No qualified profiles yet. Let the bot observe the tape until "
                    f"each profile has >= {cfg.whale_tracker.min_events_for_scoring} "
                    "settled events."
                )
                return

            hdr = (f"{'profile':<12} {'pairs (top 3)':<40} {'lot $':<22} "
                   f"{'trade $':<22} {'skew':<9} {'n':>4} {'wr':>6} "
                   f"{'exp':>8} {'rec':<16}")
            typer.echo(hdr)
            typer.echo("-" * len(hdr))
            for r in rows:
                pairs = r["pairs"][:3]
                pairs_s = ", ".join(f"{p['symbol']}({p['events']})" for p in pairs) or "-"
                lot_s = f"{_fmt_usd(r['lot_notional_min'])}-{_fmt_usd(r['lot_notional_max'])}"
                nt_s = f"{_fmt_usd(r['trade_notional_min'])}-{_fmt_usd(r['trade_notional_max'])}"
                skew = f"B{r['buy_pct']*100:3.0f}/S{(1-r['buy_pct'])*100:3.0f}"

                ref = mark_symbol or (pairs[0]["symbol"] if pairs else "")
                mark = 0.0
                if ref:
                    try:
                        mark = await exchange.fetch_ticker_price(ref)
                    except Exception:  # noqa: BLE001
                        mark = 0.0
                rec_s = "-"
                if mark > 0:
                    rec = recommend_for_profile(
                        profile_stats=r, mark=mark, equity_usd=eq,
                        risk=risk, signals_cfg=cfg.signals,
                    )
                    if rec.approved:
                        rec_s = (f"{rec.equity_pct*100:.1f}% / "
                                 f"{_fmt_usd(rec.usd_notional)}")
                    else:
                        rec_s = f"skip:{rec.reason[:10]}"

                typer.echo(
                    f"{r['profile_id']:<12} {pairs_s[:40]:<40} "
                    f"{lot_s:<22} {nt_s:<22} {skew:<9} "
                    f"{r['total']:>4} {r['win_rate']*100:>5.1f}% "
                    f"{r['expectancy_bps']:>7.1f}b {rec_s:<16}"
                )
            typer.echo("")
            typer.echo(
                f"equity used for recommendation: ${eq:,.2f} | "
                f"caps: pos {cfg.risk.max_position_pct*100:.1f}% / "
                f"gross {cfg.risk.max_gross_exposure_pct*100:.1f}% / "
                f"kelly x{cfg.risk.kelly_fraction}"
            )
        finally:
            await exchange.close()
            store.close()

    asyncio.run(_run())


def _fmt_usd(x: float) -> str:
    if x >= 1_000_000:
        return f"${x/1_000_000:.1f}M"
    if x >= 1_000:
        return f"${x/1_000:.0f}k"
    return f"${x:.0f}"


@app.command()
def api(
    config: Path = typer.Option("config.yaml", help="Path to config YAML."),
    host: str = typer.Option("0.0.0.0",
                             help="Bind host. 0.0.0.0 to access from your phone on LAN."),
    port: int = typer.Option(8787, help="Bind port."),
) -> None:
    """Run the HTTP+WebSocket API and serve the mobile PWA."""
    import os
    import uvicorn
    from trading_bot.api import create_app

    load_dotenv()
    cfg = Config.load(config)
    setup_logging(cfg.logging.level, cfg.logging.file)

    if not os.getenv("API_KEY", "").strip() and host != "127.0.0.1":
        typer.echo(
            "WARNING: API_KEY is unset and you are binding to a non-loopback host. "
            "Anyone on the network can control the bot. Set API_KEY in .env "
            "before exposing this beyond localhost.",
            err=True,
        )
    typer.echo(f"open the app on your phone: http://<this-host>:{port}/")
    uvicorn.run(create_app(cfg), host=host, port=port, log_config=None)


if __name__ == "__main__":
    app()
