# Sector Rotation Swing Scanner

A command-line scanner for a **stock-rotation swing strategy** built on the
11 SPDR sector ETFs. It ranks sectors by momentum and relative strength versus
SPY, gates on trend, optionally confirms with **Unusual Whales** options-flow
(sector tide), and tells you what to **rotate into**, **hold**, and **rotate out
of** — with ATR-based entry/stop/target levels for the leaders.

It ships with a **backtester** (`backtest.py`) that replays the exact same
signals over years of history so you can validate the rules before trading
them. See [Backtesting](#backtesting) and the [Roadmap](#roadmap) for what
else can be built on the same data stack.

## What it does

1. Pulls ~1 year of daily OHLC for SPY + the 11 sector ETFs from Unusual Whales.
2. Classifies the market **regime** (risk-on / risk-off) from SPY vs its 200-day SMA.
3. Scores each sector:
   - **Momentum** — a blend of 1M / 3M / 6M returns.
   - **Relative strength** — 3M return minus SPY's 3M return.
   - **Trend gate** — price vs the 50/200-day SMAs (weak trends get a score haircut).
   - **Flow confirmation** *(optional)* — net call vs put premium from the sector tide.
   - In risk-off regimes, defensive sectors (XLP/XLU/XLV) get a small boost.
4. Ranks all 11 and assigns a signal: `ROTATE IN` / `HOLD` / `ROTATE OUT`.
5. Prints ATR(14)-based swing levels (2R stop, 3R target) for the rotate-in names.

## Setup

Requires **Python 3.10+**. No third-party packages — it uses only the standard
library.

```bash
cp .env.example .env
# edit .env and paste your Unusual Whales API token
```

An Unusual Whales API token is required (a paid UW plan with API access).

## Usage

```bash
python scan.py                  # ranked table + swing levels
python scan.py --top 4          # rotate into the top 4 sectors
python scan.py --no-flow        # skip the options-flow calls (faster)
python scan.py --csv today.csv  # also save results to CSV
python scan.py --timeframe 2Y   # use a longer history window
```

### Example output

```
================================================================================================
  SECTOR ROTATION SCANNER            regime: RISK-ON  (SPY 123.05 above 200d SMA 113.53)
================================================================================================
 #  ETF  Sector                       1M     3M     6M     RS  Trend  Flow  Score  Signal
------------------------------------------------------------------------------------------------
 1  XLK  Technology               +5.2% +13.8% +22.1%  +8.2%     up  bull  100.0  ROTATE IN
 2  XLC  Communication Services   +4.1% +11.0% +18.4%  +5.4%     up  bull   90.0  ROTATE IN
 ...
 11 XLE  Energy                   -3.0%  -8.4%  -6.2% -14.0%     --  bear    0.0  ROTATE OUT
------------------------------------------------------------------------------------------------

  SWING LEVELS (leaders) — ATR(14) based, 2R stop / 3R target:
    XLK   entry 167.95   stop 158.01   target 182.84   ATR 4.97  [outperforming SPY, bullish flow]
```

## Backtesting

`backtest.py` replays the scanner's **exact** signals over historical data —
on each rebalance date it ranks the sectors *as of that date* (no lookahead),
buys the `ROTATE IN` names equal-weight, holds to the next rebalance, and parks
unfilled slots in cash (so the trend filter de-risks you in downturns). It then
reports the results against a SPY buy-and-hold benchmark.

```bash
python backtest.py                          # 5y history, weekly rebalance, top 3
python backtest.py --rebalance monthly      # monthly instead of weekly
python backtest.py --top 4 --cost-bps 5     # top 4 sectors, 5bps per-side cost
python backtest.py --timeframe 8Y --csv equity.csv --trades-csv trades.csv
```

Options: `--top`, `--bottom`, `--rebalance` (`weekly`/`biweekly`/`monthly` or a
number of trading days), `--capital`, `--cost-bps`, `--timeframe`, `--csv`
(equity curves), `--trades-csv` (per-rebalance holdings log).

### Example output

```
==============================================================================
  SECTOR ROTATION BACKTEST
==============================================================================
  window   : 2020-06-01 -> 2025-08-07  (5.2y)
  rules    : top 3 sectors, rebalance weekly, 2bps/side, $100,000 start
  activity : 270 rebalances, 74% avg exposure, 61% winning periods
------------------------------------------------------------------------------
  metric                      STRATEGY    SPY BUY & HOLD          edge
------------------------------------------------------------------------------
  Total return                +XXX.XX%          +XX.XX%       +XX.XX%
  CAGR                         +XX.XX%           +XX.XX%       +XX.XX%
  Max drawdown                 -XX.XX%           -XX.XX%       +XX.XX%
  Sharpe                          X.XX              X.XX         +X.XX
  ...
------------------------------------------------------------------------------
  $100,000 -> $XXX,XXX (strategy) vs $XXX,XXX (SPY)
```

**Reported metrics:** total return, CAGR, max drawdown, annual volatility,
Sharpe, Sortino, Calmar, win rate, and average exposure — each shown against
SPY buy-and-hold with the edge in the final column.

The backtester runs on price/momentum/trend only; the live options-flow overlay
(sector tide) is a real-time signal and is intentionally not modelled in
historical replay.

## Files

| File | Purpose |
|------|---------|
| `scan.py`     | Live scanner CLI — fetch, score, report, CSV export |
| `backtest.py` | Historical backtester — replays the signals, metrics vs SPY |
| `rotation.py` | Indicators (returns, SMA, ATR) and the scoring/ranking logic |
| `uw_client.py`| Minimal Unusual Whales REST client (auth, retries, unwrapping) |
| `.env.example`| Template for your API token |

The scanner and backtester share `rotation.py`, so what you backtest is exactly
what the scanner signals live.

## How the score works

```
score = 0.6 * momentum_percentile + 0.4 * relative_strength_percentile
        * 0.5  if not in a 50/200 uptrend
        * 0.6  if below the 200-day SMA
        + 8    if defensive and the regime is risk-off
```

Percentiles are computed across the 11 sectors each run, so the score is always
relative to the current opportunity set. Tune the weights, lookbacks, and gates
in `rotation.py` (`MOM_WEIGHTS`, `LB_1M/3M/6M`, `SMA_FAST/SLOW`).

## Roadmap

Built on the same Unusual Whales / market-data stack:

- **Daily swing dashboard** — HTML report of leaders + flow + dark-pool + entry levels.
- ~~**Backtester**~~ — ✅ done (`backtest.py`).
- **TradingView Pine Script** — the same ranking as a chart indicator/strategy with alerts.
- **Smart-money layer** — dark-pool prints + congress/insider buys as a conviction filter.
- **Automated Routine** — run pre-market each trading day and push the results.

## Disclaimer

For research and education only. Not investment advice. Signals are mechanical;
always confirm on your own chart and manage risk.
