# Sector Rotation Swing Scanner

A command-line scanner for a **stock-rotation swing strategy** built on the
11 SPDR sector ETFs. It ranks sectors by momentum and relative strength versus
SPY, gates on trend, optionally confirms with **Unusual Whales** options-flow
(sector tide), and tells you what to **rotate into**, **hold**, and **rotate out
of** — with ATR-based entry/stop/target levels for the leaders.

It ships with a **backtester** (`backtest.py`) that replays the exact same
signals over years of history, and a **daily dashboard** (`dashboard.py`) that
renders the signals as a mobile-friendly HTML page you open each morning. See
[Backtesting](#backtesting), [Daily dashboard](#daily-dashboard), and the
[Roadmap](#roadmap) for what else can be built on the same data stack.

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

## Daily dashboard

`dashboard.py` renders the live signals as a **self-contained, responsive,
theme-aware HTML page** — open it on your phone before the open. It shows:

- **Regime KPIs** — market regime, how many sectors to rotate into, breadth
  above the 200-day, and the top-ranked sector.
- **Sector ranking table** — 30-day price sparklines, 1M/3M/6M returns,
  relative strength, trend and options-flow chips, a score bar, and the
  rotate-in / hold / rotate-out signal for all 11 sectors.
- **Swing levels** — Entry / Stop (2·ATR) / Target (3·ATR) and reward:risk
  cards for the rotate-in leaders.

```bash
python dashboard.py                    # writes dashboard.html
python dashboard.py --open             # write and open in your browser
python dashboard.py --out ~/swing.html --top 4
python dashboard.py --no-flow          # price-only, skip flow calls
```

The page is a single HTML file with everything inlined (no external assets),
respects your system light/dark preference, has a manual theme toggle, and
collapses to a phone layout.

### Rendering from connector data (for automation)

`dashboard.py` calls the REST API and needs `UW_TOKEN`. For scheduled runs the
dashboard is instead built from data pulled through an MCP connector (the
environment's egress proxy blocks direct API calls), using one of:

- **`render_from_massive.py`** *(primary)* — builds the dashboard from **Massive**
  data: adjusted daily OHLC (`/v2/aggs`) for momentum/RS/trend/ATR, plus the live
  snapshot (`/v3/snapshot`) for a real-time **Live** column (today's % change).
  Massive is deeper and more live than close-only history. It also layers in the
  **whale conviction** signal (see below) when `data/_whale.json` is present.

  ```bash
  # data/SPY.csv … data/XLC.csv (daily aggs) + data/_snapshot.csv
  # + data/_whale.json (Unusual Whales sector data), then:
  python render_from_massive.py --data-dir data --out dashboard.html
  ```

- **`render_from_mcp.py`** *(alternative)* — builds it from **Unusual Whales** MCP
  files (`get_ticker_ohlc_latest_or_date`), which include per-day options flow
  (bullish/bearish premium) for a **Flow** column instead of Live.

Both reuse `rotation.py` and `dashboard.py`, so the signals match the scanner.
The [automated morning Routine](#automated-morning-routine) uses the Massive path.

### Whale conviction layer

`whale.py` turns the Unusual Whales `get_market_sector_etfs` call (one call, all
12 sectors) into a per-sector **conviction score** in [-1, +1], from:

- **Options flow** — net options premium (`bullish_premium − bearish_premium`)
- **Accumulation** — 5-day net in/out share flow (institutional accumulation vs distribution)
- **Options bias** — call vs put premium

The dashboard shows it as a **Whale** column (conviction arrow + net premium,
hover for the breakdown), and `rank_sectors(whale_weight=…)` folds it into the
score (default ±12 points) so bullish flow + accumulation lifts a momentum pick
and distribution vetoes it. Save the call's JSON to `data/_whale.json`; pass
`--no-whale` to disable.

Note on scope: per-print dark-pool tape and rule-based flow alerts are dominated
by SPY/QQQ/bonds market-wide and are sparse for the sector ETFs themselves, so
the accumulation read uses each sector ETF's net in/out share flow rather than
individual prints.

## Dark-pool block view

`darkpool.py` renders a standalone report of recent off-exchange (dark pool /
TRF) **block prints** per sector ETF, from the Unusual Whales
`get_dark_pool_trades` call (one per ETF). For each sector it shows print count,
total dark-pool premium, the largest block, how much of the day's volume printed
off-exchange, and an **accumulation vs distribution** lean (premium-weighted
price vs the NBBO midpoint) — plus a "notable blocks" list.

```bash
# one data/_dp_<TICKER>.json per ETF (raw get_dark_pool_trades output), then:
python darkpool.py --data-dir data --out darkpool.html
python darkpool.py --min-premium 1000000     # only blocks over $1M
```

This is a separate on-demand view (not part of the morning dashboard) because
sector-ETF dark-pool prints are small enough to return inline rather than as
files. Ask to have it added to a routine if you want it pushed on a schedule.

## TradingView Pine Script

`pine/sector_rotation.pine` is a Pine v6 indicator that replicates the ranking
on a TradingView chart: paste it into the Pine Editor and add it to any chart
(daily timeframe works best). It pulls all 11 sector ETFs + SPY via
`request.security`, ranks them by blended momentum + relative strength gated on
the 50/200-day trend, and shows:

- a **ranked table** (rank, ETF, 3M %, RS %, score, signal),
- the **chart symbol's** rank + signal label and ATR-based entry/stop/target
  lines when it's a rotate-in name,
- **alerts** when the chart symbol's rotation signal changes.

Pine can't reach Unusual Whales or Massive, so the on-chart score is
price/momentum/trend only. To fold in the whale conviction, type each sector's
conviction (−1..+1, read off the dashboard) into the script's "Whale conviction"
inputs and set a whale weight; leave them 0 to ignore.

## Webhook backend

`server.py` is a FastAPI service that receives TradingView alert webhooks. It is
the entry point for turning on-chart alerts into scanner input — right now it
validates each alert and prints it to the terminal.

Unlike the scanner scripts, it needs third-party packages:

```bash
pip install -r requirements.txt
```

Run it:

```bash
uvicorn server:app --host 127.0.0.1 --port 8000 --reload
```

Then point a TradingView alert's *Webhook URL* at
`http://localhost:8000/api/tv-webhook` and set the alert message to JSON:

```json
{"ticker": "{{ticker}}", "condition": "RSI_Oversold", "price": {{close}}}
```

TradingView only reaches public URLs, so for live alerts expose the port with a
tunnel (`cloudflared tunnel --url http://localhost:8000`, ngrok, etc.) and use
the tunnel's hostname. To test locally without one:

```bash
curl -X POST http://localhost:8000/api/tv-webhook \
  -H "Content-Type: application/json" \
  -d '{"ticker": "TSLA", "condition": "RSI_Oversold", "price": 225.50}'
```

`ticker`, `condition`, and `price` are required; `ticker` is upper-cased and
`price` must be positive. Any extra keys you add to the alert message
(`{{interval}}`, `{{exchange}}`, ...) are kept and printed. A malformed body
returns 400, a body that fails validation returns 422 with the offending
fields. `GET /health` is there to confirm the tunnel is up, and interactive API
docs are at `http://localhost:8000/docs`.

## Conviction validator

`uw_validator.py` is the async half of the webhook pipeline: given a ticker off
a TradingView alert, it asks Unusual Whales whether institutions agree before
a trade gets priced. It needs `httpx` (`pip install -r requirements.txt`) and
reads `UW_API_KEY`, falling back to the `UW_TOKEN` name the scanner uses.

```python
from uw_validator import UnusualWhalesValidator

async with UnusualWhalesValidator() as uw:
    if await uw.check_options_flow("TSLA", "bullish"):
        levels = await uw.get_dark_pool_support_resistance("TSLA")
```

**`check_options_flow(ticker, direction)`** pulls `/api/option-trades/flow-alerts`,
keeps sweeps and floor (block) prints of ≥ $250k premium with ≥ 21 DTE, and
returns True when at least 60% of the *ask-side* premium sits on the side
matching `direction` — calls for bullish, puts for bearish. A verdict also
needs ≥ $500k of qualifying premium behind it, so one lone print can't confirm
a trade. `analyze_options_flow()` returns the same verdict as a `FlowAnalysis`
with the ratio, premium totals, and alert count.

**`get_dark_pool_support_resistance(ticker)`** merges
`/api/darkpool/{ticker}/price-levels` across the last 5 sessions and returns
the 3 heaviest off-exchange price shelves as `DarkPoolLevel` records — the
levels to place short credit-spread strikes behind. The endpoint serves one
session per call, so it walks back over weekdays and skips days that return
nothing (holidays).

Both methods **raise** `UWAuthError` / `UWAPIError` rather than returning a
falsey result when the API is unreachable — a network failure must not read as
"the institutions disagree".

This module is separate from `uw_client.py` on purpose: that one is the
synchronous, standard-library-only client the scanner depends on, and importing
httpx there would add a hard dependency to `scan.py`, `backtest.py`, and
`dashboard.py`.

## Automated morning Routine

A scheduled Routine regenerates the dashboard **every trading weekday at
6:00 AM PT** and delivers it to you. Each firing:

1. checks out this branch,
2. pulls adjusted daily OHLC (`/v2/aggs`) and the live snapshot (`/v3/snapshot`)
   for SPY and the 11 sector ETFs via the **Massive** MCP connector (no API token),
3. pulls sector options flow + accumulation via the **Unusual Whales**
   `get_market_sector_etfs` call (the whale conviction layer),
4. renders the dashboard with `render_from_massive.py`, and
5. sends you the HTML plus a one-line summary (regime + rotate-in / rotate-out).

**Why it binds to a session instead of spawning a fresh one:** market data here
is only reachable through an **MCP connector** (the environment's egress proxy
blocks direct API calls), and connector tools are only present in a
connector-holding session. So the Routine fires into this session, which holds
the Massive connector, and delivers the dashboard as a proactive file (phone
notification).

If you'd prefer a fresh-session Routine with email + push summaries, create it
from the **claude.ai Routines UI**, where you can attach the "Massive" connector
to the schedule (that attachment isn't available to programmatic setup). Point
its prompt at the same steps above.

Each run also republishes the dashboard to a fixed **claude.ai Artifact URL**
(via `to_artifact.py`, which strips the page down to the body-content the
Artifact host expects), so a single bookmarked link opens the latest dashboard
in Chrome — no download. The link:
<https://claude.ai/code/artifact/1cc53f7d-7cb7-4e1e-953e-0110e32300d6>

Manage it from chat: "list my routines", "change the dashboard routine to
7 AM PT", "pause the dashboard routine". The schedule is stored in UTC (13:00 =
6:00 AM PDT), so it lands one hour earlier (5:00 AM PST) during US winter unless
updated.

## Files

| File | Purpose |
|------|---------|
| `scan.py`     | Live scanner CLI — fetch, score, report, CSV export |
| `backtest.py` | Historical backtester — replays the signals, metrics vs SPY |
| `dashboard.py`| Daily HTML dashboard — regime KPIs, ranking, swing levels |
| `render_from_massive.py`| Renders the dashboard from Massive aggs + live snapshot + whale layer (Routine default) |
| `whale.py`    | Smart-money conviction from Unusual Whales sector data (flow + accumulation) |
| `darkpool.py` | Per-ETF dark-pool block-print view (count, premium, largest block, lean) |
| `pine/sector_rotation.pine` | TradingView Pine v6 indicator — ranking, signals, ATR levels, alerts |
| `server.py`   | FastAPI backend — receives and validates TradingView alert webhooks |
| `uw_validator.py` | Async UW validator — options-flow conviction + dark-pool support/resistance |
| `to_artifact.py` | Strips a dashboard HTML to Artifact body-content for hosting on claude.ai |
| `render_from_mcp.py`| Alternative renderer — dashboard from Unusual Whales MCP files (flow column) |
| `collect_mcp_data.py`| Gathers Unusual Whales MCP tool-result files into a data dir |
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

- ~~**Daily swing dashboard**~~ — ✅ done (`dashboard.py`).
- ~~**Backtester**~~ — ✅ done (`backtest.py`).
- ~~**TradingView Pine Script**~~ — ✅ done (`pine/sector_rotation.pine`).
- ~~**Dark-pool block view**~~ — ✅ done (`darkpool.py`).
- ~~**Smart-money layer**~~ — ✅ done (`whale.py`: options flow + accumulation conviction, folded into the score).
- ~~**Automated Routine**~~ — ✅ done (weekday 6 AM PT, pushes the dashboard).

## Disclaimer

For research and education only. Not investment advice. Signals are mechanical;
always confirm on your own chart and manage risk.
