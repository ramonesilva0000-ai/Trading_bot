# Trading_bot — whale-tracking framework

A trading bot that watches the public trade tape of a crypto exchange,
identifies bursts of large same-side prints ("whale events"), scores whale
cohorts by how those events actually resolve, and follows only the cohorts
that have shown a measurable edge on the stored history. Multi-exchange via
[ccxt](https://github.com/ccxt/ccxt): Binance, Bybit, OKX, Coinbase, Kraken,
KuCoin, Bitget, and ~100 others.

## Reality check

No bot "never loses." This repo is a framework for systematically copying
large-size order flow under strict risk controls. It will lose money when
followed cohorts break, during regime changes, to fees, and to adverse
selection. Always run in `dry_run: true` (paper) first, then testnet, then
with an amount you can afford to lose. Nothing here is investment advice.

## How it works

```
   trade tape        whale prints       whale events      signals
WS ──► Exchange ──► WhaleDetector ──► size-bucket ──► SignalEngine ──► RiskManager ──► Executor
                         │                 │                                  │
                         └─► Store ◄───────┴────── WhaleTracker (settles) ────┘
```

1. **Exchange** (`trading_bot/exchange.py`) streams public trades through
   `ccxt.pro` when available, else REST polling. Orders go through the same
   ccxt client.
2. **WhaleDetector** (`trading_bot/whale_detector.py`) flags trades whose
   notional clears `min_notional_usd` and clusters consecutive same-side
   prints into a single event. Events are bucketed by total notional
   (`base` / `large` / `super` / `mega`).
3. **Profiler** (`trading_bot/profiler.py`) fingerprints each event by
   `size-bucket @ UTC-time-bucket`, giving a stable trader-profile id that
   aggregates across pairs and sides.
4. **Store** (`trading_bot/storage.py`) persists events, fills, and rolling
   cohort + profile statistics in SQLite.
5. **WhaleTracker** (`trading_bot/whale_tracker.py`) revisits every stored
   event after `evaluation_horizon_h` hours, measures the signed move in
   bps from event VWAP, and updates win-rate + expectancy for both the
   cohort and the trader profile.
6. **SignalEngine** (`trading_bot/signals.py`) emits a `Signal` only for
   events whose **profile** passes `min_win_rate`, `min_expectancy_bps`,
   and `min_events_for_scoring`. Falls back to the coarser cohort when a
   profile has not yet been seen enough times.
7. **RiskManager** (`trading_bot/risk.py`) sizes positions using fractional
   Kelly, caps per-position and gross exposure, sets stop/target in bps,
   and halts trading after a configured daily drawdown.
8. **Executor** (`trading_bot/executor.py`) places `market` or post-only
   `limit` orders with slippage caps and retries; enforces stops/targets on
   each mark refresh. `dry_run: true` keeps everything paper.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
cp .env.example .env
# edit .env with EXCHANGE_API_KEY / EXCHANGE_API_SECRET (and PASSPHRASE if needed)

# paper-trade on Binance testnet:
python main.py live

# rank top trader profiles with pairs, lot/notional ranges, win-rate,
# expectancy, and a concrete size recommendation for your account:
python main.py traders --limit 10 --equity 10000

# inspect coarse cohort stats (size-bucket only):
python main.py cohorts

# replay a historical trade CSV (columns: ts_ms,symbol,price,amount,side):
python main.py backtest data/trades.csv

# serve the mobile PWA + REST/WebSocket API for phone control:
python main.py api --host 0.0.0.0 --port 8787
```

## Phone app (PWA)

A trading bot **cannot run reliably on a phone** — iOS suspends backgrounded
apps within seconds and Android is fragile under battery saver / network
loss. This repo solves it the right way: the bot runs somewhere stable
(VPS, Raspberry Pi, your laptop, or Termux on Android) and the phone is the
control surface, installed as a Progressive Web App.

What the app shows:

- **Dashboard** — equity, free margin, day PnL, mode (paper / live), open
  positions with live mark and bps PnL, Start / Stop controls.
- **Traders** — top trader profiles with their pairs, lot-size and
  trade-notional ranges, side skew, win-rate, expectancy, and the exact
  USD notional + %-of-equity the RiskManager would approve for *your*
  account if the profile fired right now.
- **Events** — recent whale events with realized bps outcomes, plus your
  recent fills.
- **Settings** — API key (must match the server's `API_KEY` env var),
  preview equity, current server config.

### Run the bot somewhere reliable

```bash
# on your VPS / Pi / laptop / Termux:
export API_KEY=$(openssl rand -hex 24)   # required for any non-loopback bind
python main.py api --host 0.0.0.0 --port 8787
```

### Install the app on your phone

1. On the same network as the host, open `http://<host-ip>:8787/` in
   Safari (iOS) or Chrome (Android).
2. **iOS**: Share → *Add to Home Screen*.
   **Android**: ⋮ menu → *Install app*.
3. Open the bot from your home screen, paste the `API_KEY` into Settings,
   and tap Save.

### Exposing it over the public internet (optional)

Always front it with HTTPS (Cloudflare Tunnel, Caddy, Nginx + Let's Encrypt,
or Tailscale). Never expose `--host 0.0.0.0` on a public IP without TLS and
a strong `API_KEY` — the API can place real orders when `dry_run: false`.

### Running the bot on the phone itself (Android-only, advanced)

Termux can run the Python stack directly on Android:

```bash
pkg install python git rust
git clone <this-repo> && cd Trading_bot
pip install -r requirements.txt
termux-wake-lock                         # keep CPU awake
python main.py api --host 127.0.0.1 --port 8787
```

Then install the PWA from `http://127.0.0.1:8787/` in the same phone's
browser. Expect the OS to kill the process if you close Termux, lose signal,
or hit aggressive battery saver — this is not a substitute for a VPS or Pi.

### `traders` output

Each row is a behavioral trader profile (size bucket × UTC time-of-day
bucket), aggregated across every pair in which it has fired:

```
profile      pairs (top 3)                     lot $            trade $             skew      n   wr     exp      rec
500k@12h     BTC/USDT(142), ETH/USDT(58), ...  $260k-$480k      $520k-$1.2M         B 58/S42  230  63.4%   8.4b    1.8% / $180
```

- **pairs**: the symbols this profile actually trades, ordered by event count.
- **lot $**: min/max single-print notional observed for this profile.
- **trade $**: min/max clustered-event notional (one whale "trade" = one burst).
- **skew**: share of events that were aggressor-buy vs aggressor-sell.
- **n / wr / exp**: settled events, win-rate on the horizon, expectancy in bps.
- **rec**: what the configured `RiskManager` would approve for *your* account
  if this profile fired a signal right now — shown as % of equity and USD
  notional. If the cap chain blocks it, you'll see `skip:<reason>`.

Every runtime parameter lives in `config.yaml`. Swap exchanges by changing
`exchange.id` to any ccxt id (`binance`, `binanceusdm`, `bybit`, `okx`,
`kraken`, `coinbase`, `kucoin`, `bitget`, ...).

## Going from paper to live

1. Run `dry_run: true` against the real tape for several days, then
   `python main.py cohorts` to see which buckets actually pass the gates on
   your universe.
2. Flip `execution.dry_run: false` only after at least one cohort shows
   `n >= min_events_for_scoring` and expectancy clearly above fees.
3. Keep `risk.max_position_pct` small (1–5%), `max_gross_exposure_pct`
   modest, and `daily_drawdown_halt_pct` tight. The halt is there to stop a
   bad day from turning into a blown account.

## Layout

```
trading_bot/
  config.py           typed config (pydantic) + YAML/env loading
  exchange.py         ccxt / ccxt.pro adapter (stream + orders)
  whale_detector.py   print detection + clustering
  whale_tracker.py    horizon evaluation + cohort scoring
  signals.py          cohort-gated signal generation
  risk.py             Kelly sizing, exposure caps, drawdown halt
  executor.py         order submission + stop/target enforcement
  storage.py          SQLite persistence
  backtest.py         CSV tape replay
  runtime.py          async orchestration
  logger.py           logging setup
  types.py            shared domain types
  api.py              FastAPI HTTP + WebSocket layer
  recommend.py        translate a profile into a sizing recommendation
main.py               CLI (live | backtest | cohorts | traders | api)
web/                  mobile-first PWA (HTML/CSS/JS, manifest, service worker)
tests/                unit tests
```

## Tests

```bash
pip install -r requirements.txt
pytest -q
```

## License

MIT.
