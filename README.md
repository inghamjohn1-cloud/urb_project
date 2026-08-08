# Sector Rotation Swing Scanner

A command-line scanner for a **stock-rotation swing strategy** built on the
11 SPDR sector ETFs. It ranks sectors by momentum and relative strength versus
SPY, gates on trend, optionally confirms with **Unusual Whales** options-flow
(sector tide), and tells you what to **rotate into**, **hold**, and **rotate out
of** — with ATR-based entry/stop/target levels for the leaders.

This is the first piece of a larger toolkit. See [Roadmap](#roadmap) for what
else can be built on the same data stack (daily dashboard, backtester,
TradingView Pine Script, automated alerts).

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

## Files

| File | Purpose |
|------|---------|
| `scan.py`     | CLI entry point — fetch, score, report, CSV export |
| `rotation.py` | Indicators (returns, SMA, ATR) and the scoring/ranking logic |
| `uw_client.py`| Minimal Unusual Whales REST client (auth, retries, unwrapping) |
| `.env.example`| Template for your API token |

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
- **Backtester** — validate the rotation rules on historical data (CAGR, drawdown, Sharpe).
- **TradingView Pine Script** — the same ranking as a chart indicator/strategy with alerts.
- **Smart-money layer** — dark-pool prints + congress/insider buys as a conviction filter.
- **Automated Routine** — run pre-market each trading day and push the results.

## Disclaimer

For research and education only. Not investment advice. Signals are mechanical;
always confirm on your own chart and manage risk.
