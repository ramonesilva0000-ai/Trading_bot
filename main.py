"""CLI entrypoint for the whale-tracking trading bot."""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path

import typer
from dotenv import load_dotenv

from trading_bot.backtest import run_backtest
from trading_bot.config import Config
from trading_bot.logger import setup_logging
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


if __name__ == "__main__":
    app()
