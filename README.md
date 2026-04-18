# claude-trading

AI-powered day trading bot using Claude (Anthropic) + Alpaca.

---

## Branches

| Branch | Strategy |
|---|---|
| `main` | Options trading (SPY/favorites.txt driven) |
| `stock-only` | Stock day trading — news + social signals |

---

## Stock Agent Flow (`stock-only`)

```
Market open (6:30 PST)
   │
   ▼
Morning scan (Claude + web search + Alpaca news)
   → loads ticker learning profiles (logs/ticker_profiles.json)
   → watchlist of 3-5 stocks with directional bias
   │
   ├── Every 60s ──────────────────────────────────────────────┐
   │    • Trailing stop check (no Claude, free)                │
   │    • Drain Discord + Twitter signal queue                 │
   │       - CALL signal (Tier 1 ticker) → Claude entry check  │
   │         even if NOT on morning watchlist                  │
   │       - CALL signal (Tier 2 ticker) → Claude entry check  │
   │       - PUT signal  → exit that stock if held             │
   │       - QQQ/SPY/IWM PUT → close ALL open positions        │
   │                                                           │
   └── Every 5 min ───────────────────────────────────────────┘
        • Claude monitors open positions (HOLD / EXIT)
        • Pre-Claude entry gates (free, no API cost):
            1. Last 5-min bar must be green (momentum)
            2. Last 5-min bar must trade ≥ $2M notional (liquidity)
            3. Confidence ≥ 60%
        • Claude evaluates watchlist for new entries
        • 10:00 PST: mid-morning re-scan refreshes watchlist

12:55 PST — Wave 1 force exit: close all flat/losing positions
12:58 PST — Wave 2 force exit: close remaining winners
13:00 PST — Market close hard stop
```

### Signal Sources
- **Alpaca News API** — real-time market news (morning scan + position monitor)
- **Twitter/X Filtered Stream** — `@unusual_whales`, `@KirasEpicTrades`
- **Discord polling** — Ashley (`ashleytheprotrader`) + Kira channels

### Stock Universe
Defined in `prompts/stock_system_prompt.py`. Two tiers:

**Tier 1 — Analyst-preferred** (Ashley, Kira, unusual_whales core names):
`NVDA, AMD, TSLA, AAPL, META, MSFT, GOOGL, AMZN, COIN, IBIT, MSTR, PLTR, CRWD, PANW, NET, AVGO, AMAT, MU, ARM, SMCI, FDX, UPS, WMT, HOOD, V, MA`

Tier 1 signals bypass the morning watchlist — the analyst call itself is the catalyst.

**Tier 2 — News/catalyst driven** (morning scan only):
Defense, financials, energy, healthcare, media, retail — only entered when there is a specific news catalyst.

### Risk Rules
| Parameter | Value |
|---|---|
| Portfolio max | $10,000 |
| Per position | ~$2,000 (Claude decides) |
| Max positions | 5 |
| Trailing stop | 2.5% below running peak (global default) |
| Per-ticker trail | Auto-calibrated after 3+ trades (from learning profiles) |
| Initial stop floor | −5% from entry |
| Daily loss halt | $300 |
| No new trades after | 12:30 PST |
| Force exit losers | 12:55 PST |
| Force exit winners | 12:58 PST |

### Entry Gates (pre-Claude, no API cost)
1. **Momentum** — last 5-min bar must close green; analyst signals bypass
2. **Liquidity** — last 5-min bar must trade ≥ $2M notional; analyst signals threshold $500k
3. **Confidence** — Claude must return ≥ 60%

### Safety
- Defaults to **paper trading** unless `--live` is explicitly passed
- Sells only shares opened this session (never touches pre-existing positions)
- Skips tickers already closed today (no same-day re-entry)
- Skips Claude when price quote returns 0 (network error → hold, don't exit)
- Live mode pre-flight: checks account balance, buying power, PDT count before trading
- On restart: re-adopts any open Alpaca positions into tracker so stops still fire

---

## Quick Start

```bash
# Activate venv
source venv/bin/activate

# Paper trade
python3 stock_main.py --paper

# Live trade (real money — prompts for confirmation)
python3 stock_main.py --live
```

---

## Logging

Every trading session writes automatically:

| File | Purpose |
|---|---|
| `logs/YYYY-MM-DD.log` | Human-readable event log — one timestamped line per event |
| `logs/trades.csv` | One CSV row per completed trade — feeds the dashboard |
| `logs/ticker_profiles.json` | Per-ticker learning: win rate, avg P&L, suggested trail % |
| `logs/trades.json` | Full JSON trade records |
| `logs/errors.json` | Error log |
| `logs/pnl.json` | Daily P&L history |

Sample event log line:
```
[07:15:33] ENTRY ✅ NVDA   | 8sh @ $875.40 | conf 72% | source: morning scan
[09:10:12] EXIT  ✅ NVDA   | entry $875.40 → exit $882.10 | P&L +$53.60 (+0.77%) | held 114m | trail stop
[09:10:12] END OF DAY 🟢 | P&L +$53.60 | trades 2
```

---

## Dashboard

### Terminal summary (after market close)
```bash
python3 dashboard/generate.py --summary
```

### Browser dashboard (interactive charts)
```bash
python3 dashboard/generate.py --serve    # starts local server + opens browser
```

Dashboard panels:
- **KPI bar** — Total P&L, win rate, profit factor, avg winner/loser, best/worst day
- **Daily P&L bar chart** — green/red bars per trading day
- **Cumulative P&L line** — running total equity curve
- **Exit reasons pie** — trail stop / force exit / Claude exit / macro risk-off
- **Signal source P&L** — morning scan vs Discord vs Twitter performance
- **Win/loss distribution** — histogram of trade outcomes
- **Per-ticker table** — win%, avg P&L, total P&L per stock
- **Full trade log** — every closed trade with source badge

---

## Learning System

The bot gets smarter after every session:

1. Each closed trade is written to `logs/trades.csv`
2. At end of day, `update_ticker_profiles()` reads all trades and writes `logs/ticker_profiles.json`
3. Next morning, Claude sees each ticker's win rate, avg P&L, and a ⚠️ warning for poor performers
4. `RatchetTracker` reads the profile and uses a **per-ticker calibrated trail %** instead of the global default (requires ≥ 3 trades with that ticker)

Example — after 12 NVDA trades with avg winning move of 1.8%, trail auto-adjusts to 1.0% instead of 2.5%.

---

## Config

All settings in `.env` — see `.env` for full list. Key vars:

```
ALPACA_PAPER_KEY / ALPACA_PAPER_SECRET
ALPACA_LIVE_KEY  / ALPACA_LIVE_SECRET
ANTHROPIC_API_KEY
TWITTER_BEARER_TOKEN
TWITTER_ACCOUNTS=unusual_whales,KirasEpicTrades
DISCORD_EMAIL / DISCORD_PASSWORD
DISCORD_CHANNEL_IDS=748401380288364575,992547504413491220
STOCK_TRAIL_PCT=2.5              # global trailing stop % (per-ticker overrides after 3+ trades)
STOCK_MAX_DAILY_LOSS_USD=300     # session halts at this loss
STOCK_FORCE_EXIT_PST=12:55       # wave 1: close losers
STOCK_WINNER_EXIT_PST=12:58      # wave 2: close winners
STOCK_MIN_BAR_DOLLAR_VOL=2000000 # min 5-min bar liquidity filter
```
