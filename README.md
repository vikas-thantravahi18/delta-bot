# Delta Exchange Trading Bot

An automated crypto trading bot for **Delta Exchange** perpetuals, written in
Python. It picks trade signals from a configurable strategy, sizes positions
with strict risk rules, manages each trade with a stop-loss and take-profit
(bracket order), and ships with a **backtester** so you can validate the strategy
over 1‑month, 3‑month, 6‑month, 1‑year and 2‑year windows.

> ⚠️ **Risk disclaimer.** This is educational software, not financial advice.
> Crypto trading (especially leveraged perpetuals) can lose money fast. No
> strategy guarantees a win rate or profit — backtest results never guarantee
> future performance. Run in **dry-run** and on **testnet** first, and only
> trade money you can afford to lose. You are responsible for your own trades.

---

## What it does (your requirements → how they map)

| Your requirement | Where it lives |
| --- | --- |
| Trade automatically & manage fully | [src/live/trader.py](src/live/trader.py) — entry + bracket stop/TP |
| Pick a strategy | EMA cross + RSI + ATR trend strategy in [src/strategies/ema_rsi_atr.py](src/strategies/ema_rsi_atr.py) |
| 1–2 trades/day | `risk.max_trades_per_day` in [config.example.yaml](config.example.yaml) |
| Win rate > 35% | At 1:2 R:R, break-even win rate ≈ **33%**, so >35% is profitable. Verify per period with the backtester. |
| Deploy 50% of balance | `risk.capital_allocation_pct: 0.50` |
| ~$5 risk per trade | `risk.risk_per_trade_usd: 5.0` → position auto-sized so the stop loses ≈ $5 |
| 1:2 or 1:3 reward | `risk.reward_risk_ratio: 2.0` (or `3.0`) |
| Backtest 1m/3m/6m/1y/2y | [scripts/run_backtest.py](scripts/run_backtest.py) |
| Python | ✅ |

### The math behind "win rate > 35%"
With a reward:risk of **1:2**, each win pays 2× what each loss costs. Break-even
win rate = `1 / (1 + R)` = `1 / 3` ≈ **33.3%**. So a real win rate above ~35%
(with this R:R) is net-positive *before* fees. The bot logs your actual win rate
per period so you can confirm it on real data.

---

## Project layout

```
.
├── config.example.yaml        # copy to config.yaml and tune
├── .env.example               # copy to .env and add API keys
├── requirements.txt
├── scripts/
│   ├── run_backtest.py        # backtest over multiple periods
│   └── run_live.py            # paper / live trading loop
├── src/
│   ├── config.py              # config + env loading/validation
│   ├── indicators.py          # EMA / RSI / ATR
│   ├── risk.py                # position sizing & R:R
│   ├── exchange/delta_client.py   # Delta REST client (public + signed)
│   ├── data/loader.py         # historical candle fetch (paginated + cached)
│   ├── strategies/            # strategy interface + ema_rsi_atr
│   ├── backtest/              # event-driven engine + metrics
│   └── live/trader.py         # live/dry-run trading loop
└── tests/test_strategy.py     # offline sanity tests
```

---

## Setup

Python 3.10+ (tested on 3.14). From the project folder:

```powershell
# 1. install dependencies (Windows: use the py launcher)
py -m pip install -r requirements.txt

# 2. create your config + secrets
copy config.example.yaml config.yaml
copy .env.example .env
#    then edit .env with your Delta API key/secret (only needed for LIVE trading)
```

`config.yaml` and `.env` are git-ignored.

---

## Backtesting

No API keys required (candle data is public).

```powershell
# all periods (1m, 3m, 6m, 1y, 2y) on the configured symbol/timeframe
py scripts/run_backtest.py

# specific periods, save equity-curve PNGs
py scripts/run_backtest.py --periods 1m 6m 1y --plot

# try another market / timeframe
py scripts/run_backtest.py --symbol ETHUSD --resolution 15m
```

For each period you get win rate, profit factor, expectancy, average R,
max drawdown and a final-balance summary table. Trades and the equity curve
are written to `results/`.

---

## Running the bot

**Dry-run (default, safe):** evaluates the strategy on every closed candle and
logs the exact trade it *would* place — no orders are sent.

```powershell
py scripts/run_live.py            # loop forever in dry-run
py scripts/run_live.py --once     # single evaluation then exit
```

**Live trading:** set `live.dry_run: false` in `config.yaml`, add API keys to
`.env`, then:

```powershell
py scripts/run_live.py --live     # asks you to type 'I UNDERSTAND' first
```

The bot keeps at most `max_open_positions`, respects `max_trades_per_day`, and
submits a bracket order (entry + stop-loss + take-profit) so every position is
risk-managed from the moment it opens.

> Start on **Delta testnet** (`https://testnet-api.delta.exchange` style base URL)
> or with the smallest possible size until you trust it.

---

## The strategy (default: `ema_rsi_atr`)

A classic trend/momentum approach:

1. **Trend filter** — only go long when price is above the long EMA (and the
   higher-timeframe trend is up); mirror for shorts.
2. **Entry trigger** — fast EMA crosses the slow EMA in the trend direction.
3. **Confirmation** — RSI agrees with the direction but isn't already at an
   overbought/oversold extreme (don't chase).
4. **Stop** — placed `atr_stop_mult × ATR` away (volatility-adaptive).
5. **Target** — `reward_risk_ratio ×` the stop distance (1:2 or 1:3).

All parameters live under `strategy.params` in the config — tune them with the
backtester. To add your own strategy, subclass `Strategy` in
[src/strategies/base.py](src/strategies/base.py) and register it in
[src/strategies/__init__.py](src/strategies/__init__.py).

---

## Tests

```powershell
py tests/test_strategy.py      # or: py -m pytest -q
```

---

## Important notes & limitations

- **Backtest realism:** fees and slippage are modelled (`backtest.fee_rate`,
  `backtest.slippage_rate`), and entries fill on the *next* bar's open to avoid
  look-ahead. Intrabar stop/target order is assumed worst-case (stop first).
  It still can't capture every real-world fill nuance.
- **Verify the Delta API details** (endpoint paths, `contract_value`, order
  params) against the current [Delta docs](https://docs.delta.exchange) for your
  region before trading real money — exchanges change APIs.
- **No guarantees.** Tune, backtest across all periods, paper-trade, then scale
  up slowly.
```
