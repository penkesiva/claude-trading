"""
prompts/stock_system_prompt.py
System prompt and decision prompts for the stock day-trading agent.
"""

# ── Tier 1: Analyst-preferred tickers ───────────────────────
# Frequently traded by Ashley (ashleytheprotrader), Kira (KirasEpicTrades),
# and unusual_whales. A CALL signal from any monitored analyst on these
# triggers a Claude entry check even if the ticker wasn't on the morning
# watchlist — the analyst signal IS the catalyst.
TIER_1_TICKERS = [
    # Ashley + Kira core names
    "NVDA", "AMD", "TSLA", "AAPL", "META", "MSFT", "GOOGL", "AMZN",
    # Crypto / BTC-adjacent (Kira)
    "COIN", "IBIT", "MSTR",
    # Cybersecurity (Ashley + unusual_whales flow)
    "PLTR", "CRWD", "PANW", "NET",
    # Semis (Ashley)
    "AVGO", "AMAT", "MU", "ARM", "SMCI",
    # Logistics / Consumer (Ashley)
    "FDX", "UPS", "WMT",
    # Fintech (unusual_whales flow)
    "HOOD", "V", "MA",
]

# ── Full tradeable universe ──────────────────────────────────
# All tickers the signal parser and morning scan may consider.
# Tier 1 tickers are included automatically.
STOCK_UNIVERSE = list(dict.fromkeys([
    # ── Tier 1 (analyst-preferred — see above) ──────────────
    *TIER_1_TICKERS,

    # ── Mega-cap tech (news-driven) ─────────────────────────
    "INTC", "QCOM", "LRCX",
    # Cloud / Enterprise software
    "CRM", "ORCL", "SNOW", "ZS", "ADBE",
    # Defense / Aerospace (war/contract news)
    "LMT", "RTX", "NOC", "GD", "BA", "AXON",
    # Financial
    "JPM", "GS", "BAC", "MS",
    # Healthcare (earnings / regulatory catalyst only)
    "UNH", "ABBV", "JNJ", "PFE",
    # Energy (oil price moves)
    "XOM", "CVX",
    # Consumer / Retail
    "COST", "TGT", "HD", "NKE",
    # EV / Automotive
    "RIVN", "F", "GM",
    # Media / Streaming
    "DIS", "NFLX", "SPOT",
    # Mobility
    "UBER",
    # Networking / Infra
    "ANET", "ROKU", "TTD",
    # Quantum computing (govt contracts, breakthroughs)
    "IONQ",
    # Bitcoin mining (BTC-correlated — BTC filter applies)
    "IREN",
    # Satellite / Space telecom (news-driven, volatile)
    "ASTS",
    # AI infrastructure / Cloud (CoreWeave — recent IPO, news-driven)
    "CRWV",
    # Clean energy (govt policy, energy contracts)
    "BE",
    # Fintech / Banking (Fed rate decisions, regulatory news)
    "SOFI",
    # Photonics / AI infra (moves with NVDA ecosystem)
    "COHR",
    # Optical networking (thin — dollar volume filter guards entries)
    "LITE",
    # EV battery tech (speculative, thin — dollar volume filter guards entries)
    "QS",
    # Storage (SanDisk spin-off, thin — dollar volume filter guards entries)
    "SNDK",
    # Micro-cap drone/wireless (very thin — dollar volume filter guards entries)
    "ONDS",
]))


# ── System prompt ───────────────────────────────────────────

def get_stock_system_prompt(paper_mode: bool = True) -> str:
    mode = "PAPER TRADING (simulated money)" if paper_mode else "LIVE TRADING (real money)"
    universe_str = ", ".join(STOCK_UNIVERSE)
    tier1_str    = ", ".join(TIER_1_TICKERS)
    return f"""You are an AI intraday stock-trading assistant operating in {mode}.

MANDATE
- Day-trade individual US equities (buy long only, no shorting, no options, no ETFs).
- Every position must OPEN AND CLOSE within the same market session.
- Max portfolio exposure: $10,000 across all positions.
- Baseline allocation: $2,000 per position (adjust smaller if risk is HIGH).
- Max 5 simultaneous open positions.
- Ratcheting trailing stops are managed by Python — you do NOT set stop prices.

SIGNAL HIERARCHY (weight in this order)
1. Breaking news — tech announcements, earnings beats/misses, government actions,
   geopolitical / defense developments, regulatory decisions.
2. Technical momentum — price vs VWAP, volume surge, 5-min bar direction.
3. Macro / sector context — Fed, inflation, yields, sector rotation.

TRADEABLE UNIVERSE
Only recommend stocks from: {universe_str}

TIER 1 TICKERS (analyst-preferred — lower evidence bar):
{tier1_str}
These are frequently traded by our signal analysts (Ashley, Kira, unusual_whales).
When an analyst CALL signal arrives for a Tier 1 ticker, treat it as a meaningful
directional catalyst even without independent news confirmation.
For all other tickers, require clear independent news catalyst.

HARD RULES
- Confidence < 60% → output SKIP, never ENTER.
- Do not chase stocks already up > 3% without fresh catalyst.
- If you cannot find a clear catalyst today, output SKIP.
- COIN requires a crypto-specific catalyst (e.g., Bitcoin surge, crypto regulation news,
  exchange volume spike). General macro risk-on sentiment is NOT sufficient to enter COIN.
- All responses must be valid JSON objects only.
  Start your JSON with {{ and end with }}. No prose before or after.
"""


# ── Morning scan ─────────────────────────────────────────────

MORNING_SCAN_PROMPT = """
Current time: {time_pst} PST
Today: {date}

Recent market news (last 12 hours):
{news_summary}

{ticker_history}

Analyze the news and identify 3-5 stocks with the strongest directional setups today.
Consider: tech launches, earnings, government contracts, geopolitical events, sector catalysts.
Use our trading history above to avoid tickers with poor win rates unless there is a very
strong, specific catalyst today that is different from previous sessions.

Return ONLY a valid JSON object:
{{
  "market_bias": "bullish" | "bearish" | "mixed",
  "market_summary": "2-3 sentence macro context",
  "watchlist": [
    {{
      "ticker": "NVDA",
      "bias": "bullish" | "bearish",
      "catalyst": "one sentence — specific reason for today",
      "confidence": 0.0,
      "risk": "LOW" | "MEDIUM" | "HIGH"
    }}
  ],
  "avoid_sectors": ["energy"],
  "key_risks": ["Fed speaker at 10am PST"]
}}
"""


# ── Entry decision ────────────────────────────────────────────

ENTRY_DECISION_PROMPT = """
Current time: {time_pst} PST
Ticker: {ticker}
Morning bias: {bias}
Market context: {market_summary}
{analyst_signal_block}{ticker_history}

Current quote:
{price_data}

Recent 5-min bars (today's session):
{bars_summary}

Recent news for {ticker}:
{news_context}

Portfolio state:
- Open positions: {open_positions}
- Available cash (est.): ~${available_cash:.0f}
- Daily P&L: ${daily_pnl:+.2f}

Proposed entry: {share_count} shares × ${current_price:.2f} = ${order_cost:.0f}
(Base allocation ${base_allocation:.0f} — suggest fewer shares if HIGH risk)

Should we enter {ticker} right now?

Return ONLY a valid JSON object:
{{
  "action": "ENTER" | "SKIP",
  "ticker": "{ticker}",
  "shares": {share_count},
  "entry_rationale": "2-3 sentence explanation of the setup",
  "risk_assessment": "LOW" | "MEDIUM" | "HIGH",
  "confidence": 0.0,
  "news_thesis": "one-line catalyst",
  "warnings": ["any concerns"]
}}
"""


# ── Position monitor ──────────────────────────────────────────

POSITION_MONITOR_PROMPT = """
Current time: {time_pst} PST

Open positions (ratchet stops managed by Python):
{positions_summary}

Recent news (last 60 min):
{news_context}

Market notes: {market_notes}

For each position, decide: HOLD or EXIT (based on news/fundamentals, not stop levels).
Only EXIT if you see a fundamental reason beyond normal price movement
(e.g., bad earnings print, news reversal, sector meltdown, force-exit time approaching).

Return ONLY a valid JSON object:
{{
  "decisions": [
    {{
      "ticker": "AAPL",
      "action": "HOLD" | "EXIT",
      "rationale": "one sentence",
      "urgency": "normal" | "immediate"
    }}
  ],
  "market_notes": "brief market color"
}}
"""
