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
   → watchlist of 3-5 stocks with directional bias
   │
   ├── Every 60s ──────────────────────────────────────────────┐
   │    • Ratchet/trailing stop check (no Claude, fast)        │
   │    • Drain Discord + Twitter signal queue                 │
   │       - CALL signal  → evaluate entry via Claude          │
   │       - PUT signal   → exit that stock if held            │
   │       - QQQ/SPY PUT  → close ALL open positions (risk-off)│
   │                                                           │
   └── Every 5 min ───────────────────────────────────────────┘
        • Claude monitors open positions (HOLD / EXIT)
        • Claude evaluates watchlist for new entries
        • 10:00 PST: mid-morning re-scan refreshes watchlist

Force exit all positions at 12:55 PST
Market close at 13:00 PST
```

### Signal Sources
- **Alpaca News API** — real-time market news
- **Twitter/X Filtered Stream** — `@unusual_whales`, `@KirasEpicTrades`
- **Discord polling** — two analyst channels (Ashley, Kira)

### Risk Rules
| Parameter | Value |
|---|---|
| Portfolio max | $10,000 |
| Per position | ~$2,000 |
| Max positions | 5 |
| Trailing stop | 2.5% below running peak |
| Initial stop floor | −5% from entry |
| Daily loss halt | $300 |
| No new trades after | 12:30 PST |
| Force exit | 12:55 PST |

### Safety
- Defaults to **paper trading** unless `--live` is explicitly passed
- Sells only shares opened this session (never touches pre-existing positions)
- Skips tickers already closed today (no same-day re-entry)
- Skips Claude when price quote returns 0 (network error → hold, don't exit)

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

Every trading session writes to three places automatically:

| File | Purpose |
|---|---|
| `logs/YYYY-MM-DD.log` | Human-readable event log — one line per event (entry, exit, signal, scan, error) |
| `logs/trades.csv` | One CSV row per completed trade — feeds the dashboard |
| `logs/trades.json` | Full JSON trade records (for programmatic analysis) |
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
STOCK_TRAIL_PCT=2.5
STOCK_MAX_DAILY_LOSS_USD=300
```
