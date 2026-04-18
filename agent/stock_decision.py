"""
agent/stock_decision.py
Claude-powered decisions for stock day trading.
  - run_morning_scan()      → watchlist of 3-5 stocks with directional bias
  - get_entry_decision()    → ENTER or SKIP for a single ticker
  - monitor_stock_positions() → HOLD / EXIT for all open positions
"""

import json
import re
import time as _time
from datetime import datetime, date

import anthropic
import pytz

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
from prompts.stock_system_prompt import (
    get_stock_system_prompt,
    MORNING_SCAN_PROMPT,
    ENTRY_DECISION_PROMPT,
    POSITION_MONITOR_PROMPT,
)
from tools.alpaca_stock_tools import get_stock_quote, get_stock_bars, calc_share_count
from tools.news_tools import get_market_news, format_news_for_prompt
from tools.logger import log_decision, log_error, load_ticker_profiles

# Crypto-adjacent tickers that require BTC to not be in a down day
_CRYPTO_ADJACENT = {"COIN", "MSTR", "IBIT", "HOOD"}
_BTC_PROXY       = "IBIT"   # BTC ETF — available on Alpaca stock feed
_BTC_DOWN_THRESH = -1.0     # % from prev close: if IBIT < this, skip crypto entries

client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 5,
}


# ──────────────────────────────────────────────────────────
# MORNING SCAN
# ──────────────────────────────────────────────────────────

def run_morning_scan() -> dict:
    """
    Fetch 12h of news + web search → Claude returns today's stock watchlist.
    Called once at/just after market open.
    """
    print("🌅 Running stock morning scan (news + web search)...")
    pst = pytz.timezone(config.TIMEZONE)
    now = datetime.now(pst)

    news = get_market_news(hours_back=14)
    print(f"   📰 {len(news)} news articles loaded")
    news_summary = format_news_for_prompt(news, max_articles=25)

    # Load accumulated ticker history so Claude can factor in our past performance
    profiles = load_ticker_profiles()
    history_block = _format_all_profiles_for_scan(profiles)
    if history_block:
        print(f"   📚 Loaded history for {len(profiles)} ticker(s)")

    prompt = MORNING_SCAN_PROMPT.format(
        time_pst=now.strftime("%H:%M"),
        date=date.today().strftime("%B %d, %Y"),
        news_summary=news_summary,
        ticker_history=history_block,
    )

    try:
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=2000,
            system=get_stock_system_prompt(config.PAPER_MODE),
            tools=[WEB_SEARCH_TOOL],
            messages=[{"role": "user", "content": prompt}],
        )
        text   = _extract_text(response.content)
        result = _extract_json(text)
        if not result:
            result = {
                "market_bias": "mixed",
                "market_summary": text[:200],
                "watchlist": [],
            }

        watchlist = result.get("watchlist", [])
        bias_icon = {"bullish": "🟢", "bearish": "🔴"}.get(result.get("market_bias", ""), "⚪")
        print(f"   {bias_icon} Market bias: {result.get('market_bias')} — {result.get('market_summary', '')[:80]}")
        print(f"   📋 Watchlist ({len(watchlist)} stocks):")
        for w in watchlist:
            icon = "🟢" if w.get("bias") == "bullish" else "🔴"
            print(f"      {icon} {w.get('ticker'):6s} | {w.get('catalyst', '')[:75]}")

        return result

    except Exception as e:
        log_error("stock_morning_scan", str(e))
        print(f"❌ Morning scan failed: {e}")
        return {"market_bias": "mixed", "market_summary": "scan failed", "watchlist": []}


# ──────────────────────────────────────────────────────────
# ENTRY DECISION
# ──────────────────────────────────────────────────────────

def get_entry_decision(
    ticker: str,
    bias: str,
    morning_thesis: dict,
    daily_pnl: float = 0.0,
    open_position_count: int = 0,
    analyst_signal: dict = None,   # populated when triggered by Discord/Twitter call signal
) -> dict:
    """
    Ask Claude whether to enter a position in `ticker` right now.
    Returns dict with at least {"action": "ENTER"|"SKIP", "ticker": ..., "shares": ...}.

    analyst_signal (optional): dict from signal_parser with keys:
        ticker, signal_type, raw_text, source, source_label, confidence, published_at
    When present, Claude sees the raw analyst quote and the source as an extra bullish signal.
    """
    trigger = "analyst signal" if analyst_signal else "watchlist scan"
    print(f"   🔍 Entry check: {ticker} [{trigger}]...")

    quote = get_stock_quote(ticker)
    price = float(quote.get("mid") or quote.get("ask") or 0)
    if not price:
        return {"action": "SKIP", "ticker": ticker, "entry_rationale": "no quote available"}

    shares = calc_share_count(price, config.STOCK_BASE_ALLOCATION)
    if shares < 1:
        return {
            "action": "SKIP",
            "ticker": ticker,
            "entry_rationale": (
                f"{ticker} @ ${price:.2f} too expensive "
                f"for base ${config.STOCK_BASE_ALLOCATION:.0f} allocation"
            ),
        }

    bars        = get_stock_bars(ticker, timeframe="5Min", limit=20)

    # ── Pre-Claude gate 1: momentum confirmation ──────────────
    # The most recent completed 5-min bar must close >= open (green candle).
    # Analyst-signal entries get a pass — the signal itself is the catalyst.
    if bars and not analyst_signal:
        last_bar = bars[0]   # bars are newest-first (sort=desc)
        bar_open  = float(last_bar.get("o", 0))
        bar_close = float(last_bar.get("c", 0))
        if bar_open > 0 and bar_close < bar_open:
            print(f"   ⏭️  {ticker}: last 5-min bar is red (O={bar_open:.2f} C={bar_close:.2f}) — skip")
            return {
                "action":          "SKIP",
                "ticker":          ticker,
                "entry_rationale": f"momentum: last bar red O={bar_open:.2f} C={bar_close:.2f}",
            }

    # ── Pre-Claude gate 2: dollar volume filter ────────────────
    # Skip tickers where the last 5-min bar traded < $2M notional.
    # This avoids thin markets where our $2k order creates real slippage.
    # Analyst-signal entries get a smaller threshold ($500k) — urgency overrides.
    min_dollar_vol = float(getattr(config, "STOCK_MIN_BAR_DOLLAR_VOL", 2_000_000))
    if analyst_signal:
        min_dollar_vol = min(min_dollar_vol, 500_000)
    if bars:
        last_bar   = bars[0]
        bar_shares = float(last_bar.get("v", 0))
        bar_price  = float(last_bar.get("c", 0))
        dollar_vol = bar_shares * bar_price
        if dollar_vol > 0 and dollar_vol < min_dollar_vol:
            print(
                f"   ⏭️  {ticker}: dollar volume ${dollar_vol:,.0f} < "
                f"${min_dollar_vol:,.0f} minimum — too thin, skip"
            )
            return {
                "action":          "SKIP",
                "ticker":          ticker,
                "entry_rationale": f"thin market: 5-min vol ${dollar_vol:,.0f} < ${min_dollar_vol:,.0f}",
            }

    # ── Pre-Claude gate 3: BTC sentiment for crypto-adjacent names ──
    # COIN, MSTR, IBIT, HOOD all move with Bitcoin. If IBIT (BTC ETF)
    # is down > 1% on the day, skip these entries — don't fight the crypto trend.
    if ticker in _CRYPTO_ADJACENT:
        btc_pct = _btc_day_pct()
        if btc_pct <= _BTC_DOWN_THRESH:
            print(
                f"   ⏭️  {ticker}: BTC proxy ({_BTC_PROXY}) down {btc_pct:+.1f}% today "
                f"— skip crypto entry"
            )
            return {
                "action":          "SKIP",
                "ticker":          ticker,
                "entry_rationale": (
                    f"BTC down day: {_BTC_PROXY} {btc_pct:+.1f}% — "
                    f"avoid crypto-adjacent entries"
                ),
            }

    news        = get_market_news(tickers=[ticker], hours_back=4)
    pst         = pytz.timezone(config.TIMEZONE)
    now_pst     = datetime.now(pst).strftime("%H:%M")
    market_sum  = morning_thesis.get("market_summary", "")
    profiles    = load_ticker_profiles()
    ticker_hist = _format_ticker_profile_for_entry(ticker, profiles)
    # Use real tracked exposure from stock_loop state if available; fall back to estimate
    try:
        from agent.stock_loop import state as _loop_state
        actual_exposure = sum(
            t.shares * t.entry_price for t in _loop_state["trackers"].values()
        )
    except Exception:
        actual_exposure = open_position_count * config.STOCK_BASE_ALLOCATION
    avail_cash  = max(0, config.STOCK_MAX_PORTFOLIO - actual_exposure)
    order_cost  = shares * price

    # Build analyst signal block for prompt (empty string if no signal)
    if analyst_signal:
        sig_block = (
            f"\nANALYST SIGNAL (from {analyst_signal.get('source_label', analyst_signal.get('source'))}):\n"
            f"  \"{analyst_signal.get('raw_text', '')[:200]}\"\n"
            f"  Signal confidence: {analyst_signal.get('confidence', 0):.0%}\n"
            f"  This analyst is calling {ticker} calls — treat as a directional bullish trigger.\n"
            f"  We trade the underlying STOCK, not the option.\n"
        )
    else:
        sig_block = ""

    prompt = ENTRY_DECISION_PROMPT.format(
        time_pst=now_pst,
        ticker=ticker,
        bias=bias,
        market_summary=market_sum,
        analyst_signal_block=sig_block,
        ticker_history=ticker_hist,
        price_data=json.dumps({"bid": quote.get("bid"), "ask": quote.get("ask"), "mid": price}, indent=2),
        bars_summary=_format_bars(bars),
        news_context=format_news_for_prompt(news, max_articles=6),
        open_positions=open_position_count,
        available_cash=avail_cash,
        daily_pnl=daily_pnl,
        base_allocation=config.STOCK_BASE_ALLOCATION,
        share_count=shares,
        current_price=price,
        order_cost=order_cost,
    )

    for attempt in range(3):
        try:
            response = client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=800,
                system=get_stock_system_prompt(config.PAPER_MODE),
                messages=[{"role": "user", "content": prompt}],
            )
            text     = _extract_text(response.content)
            decision = _extract_json(text)
            if not decision:
                decision = {
                    "action": "SKIP",
                    "entry_rationale": f"parse failed: {text[:120]}",
                }
            decision["ticker"]        = ticker
            decision["current_price"] = price
            decision.setdefault("shares", shares)

            log_decision({}, text, decision.get("action", "SKIP"), decision)
            _print_entry_decision(decision)
            return decision

        except Exception as e:
            err = str(e)
            if "429" in err and attempt < 2:
                wait = 30 * (attempt + 1)
                print(f"   ⏳ Rate limit — retrying in {wait}s...")
                _time.sleep(wait)
                continue
            log_error("stock_entry_decision", err)
            return {"action": "SKIP", "ticker": ticker, "entry_rationale": f"error: {err[:100]}"}

    return {"action": "SKIP", "ticker": ticker, "entry_rationale": "max retries exceeded"}


# ──────────────────────────────────────────────────────────
# POSITION MONITOR
# ──────────────────────────────────────────────────────────

def monitor_stock_positions(trackers: dict, daily_pnl: float = 0.0) -> dict:
    """
    Ask Claude about all open positions.
    `trackers`: dict of ticker → RatchetTracker
    Returns:  dict of ticker → {"action": "HOLD"|"EXIT", "rationale": ..., "urgency": ...}
    """
    if not trackers:
        return {}

    pst     = pytz.timezone(config.TIMEZONE)
    now_pst = datetime.now(pst).strftime("%H:%M")

    # Force-exit window — no Claude call needed
    if now_pst >= config.STOCK_FORCE_EXIT_TIME:
        return {
            t: {"action": "EXIT", "rationale": f"force exit at {now_pst}", "urgency": "immediate"}
            for t in trackers
        }

    news = get_market_news(tickers=list(trackers.keys()), hours_back=1)  # 60 min

    positions_summary = []
    no_quote_tickers = []   # tickers with stale/failed quotes — held automatically
    for ticker, tracker in trackers.items():
        quote = get_stock_quote(ticker)
        price = float(quote.get("mid") or 0)

        if not price:
            # API/network error returned 0 — never pass 0 to Claude (it always exits on 0).
            # Treat as "data unavailable" and hold.
            no_quote_tickers.append(ticker)
            print(f"   ⚠️  {ticker}: quote = 0 (feed error) — holding, not sending to Claude")
            continue

        # Read P&L without mutating tracker state (update() ratchets the stop)
        pnl_pct = ((price - tracker.entry_price) / tracker.entry_price) if tracker.entry_price else 0
        positions_summary.append({
            "ticker":       ticker,
            "shares":       tracker.shares,
            "entry":        tracker.entry_price,
            "current":      price,
            "pnl_pct":      round(pnl_pct, 4),
            "pnl_dollar":   round((price - tracker.entry_price) * tracker.shares, 2),
            "peak":         tracker.peak_price,
            "stop":         tracker.stop_price,
            "stop_level":   tracker.level_desc,
        })

    # If every position has a stale quote, skip Claude and hold everything
    if not positions_summary:
        return {t: {"action": "HOLD", "rationale": "no live quotes — data feed error"} for t in trackers}

    prompt = POSITION_MONITOR_PROMPT.format(
        time_pst=now_pst,
        positions_summary=json.dumps(positions_summary, indent=2),
        news_context=format_news_for_prompt(news, max_articles=8),
        market_notes=f"Daily P&L ${daily_pnl:+.2f}",
    )

    try:
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=800,
            system=get_stock_system_prompt(config.PAPER_MODE),
            messages=[{"role": "user", "content": prompt}],
        )
        text   = _extract_text(response.content)
        result = _extract_json(text)
        if not result:
            return {t: {"action": "HOLD", "rationale": "parse failed"} for t in trackers}

        decisions = {}
        for d in result.get("decisions", []):
            t = (d.get("ticker") or "").upper().strip()
            if t in trackers:
                decisions[t] = d

        # Default HOLD for any ticker Claude didn't mention
        for t in trackers:
            if t not in decisions:
                decisions[t] = {"action": "HOLD", "rationale": "not mentioned — holding"}

        # Auto-HOLD tickers with stale quotes (never shown to Claude)
        for t in no_quote_tickers:
            decisions[t] = {"action": "HOLD", "rationale": "quote=0, data feed error — holding"}

        return decisions

    except Exception as e:
        log_error("monitor_stock_positions", str(e))
        return {t: {"action": "HOLD", "rationale": f"monitor error: {e}"} for t in trackers}  # noqa: E501


# ──────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────

def _btc_day_pct() -> float:
    """
    Return today's IBIT % change from previous close.
    Uses the two most recent daily bars (today + yesterday).
    Returns 0.0 if data is unavailable so the check is skipped gracefully.
    """
    try:
        bars = get_stock_bars(_BTC_PROXY, timeframe="1Day", limit=2)
        if len(bars) < 2:
            return 0.0
        # bars are newest-first; [0]=today, [1]=yesterday
        today_close = float(bars[0].get("c", 0))
        prev_close  = float(bars[1].get("c", 0))
        if prev_close <= 0:
            return 0.0
        return round((today_close - prev_close) / prev_close * 100, 2)
    except Exception:
        return 0.0


def _format_all_profiles_for_scan(profiles: dict) -> str:
    """
    Compact table of every ticker we've traded before — injected into the
    morning scan prompt so Claude knows our historical win/loss per stock.
    """
    if not profiles:
        return ""
    lines = ["OUR TRADING HISTORY (learned from past sessions):"]
    for ticker, p in sorted(profiles.items(), key=lambda x: -x[1].get("total_pnl", 0)):
        n      = p.get("trades", 0)
        wr     = p.get("win_rate", 0) * 100
        avg    = p.get("avg_pnl", 0)
        total  = p.get("total_pnl", 0)
        best   = p.get("best_source") or "—"
        trail  = p.get("suggested_trail_pct")
        warn   = "⚠️  poor history — skip unless very strong catalyst" if wr < 40 and n >= 3 else ""
        trail_note = f"  suggested trail {trail}%" if trail else ""
        lines.append(
            f"  {ticker:<6} {n:>2} trades | win {wr:>3.0f}% | "
            f"avg P&L ${avg:+.2f} | total ${total:+.2f} | "
            f"best src: {best}{trail_note}  {warn}"
        )
    return "\n".join(lines)


def _format_ticker_profile_for_entry(ticker: str, profiles: dict) -> str:
    """
    Single-ticker history block injected into the entry decision prompt.
    Returns empty string if no history yet.
    """
    p = profiles.get(ticker.upper())
    if not p or p.get("trades", 0) == 0:
        return ""
    n     = p["trades"]
    wr    = p["win_rate"] * 100
    avg   = p["avg_pnl"]
    total = p["total_pnl"]
    hold  = p.get("avg_hold_mins")
    trail = p.get("suggested_trail_pct")
    best  = p.get("best_source") or "—"
    dates = p.get("trade_dates", [])

    lines = [
        f"\nOUR HISTORY WITH {ticker} ({n} past trades, {len(dates)} day(s)):",
        f"  Win rate: {wr:.0f}%  |  Avg P&L: ${avg:+.2f}  |  Total P&L: ${total:+.2f}",
    ]
    if hold:
        lines.append(f"  Avg hold: {hold}m  |  Best signal source: {best}")
    if trail:
        lines.append(
            f"  Suggested trail stop: {trail}% "
            f"(calibrated to this ticker's typical intraday move)"
        )
    if wr < 40 and n >= 3:
        lines.append(
            f"  ⚠️  CAUTION: {ticker} has a poor win rate ({wr:.0f}%) in our history. "
            f"Require a very strong, specific catalyst before entering."
        )
    return "\n".join(lines)


def _extract_text(blocks) -> str:
    last = ""
    for b in blocks:
        if hasattr(b, "text") and b.text and b.text.strip():
            last = b.text.strip()
    return last


def _extract_json(text: str) -> dict:
    if not text:
        return {}
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    return {}


def _format_bars(bars: list) -> str:
    if not bars:
        return "(no bar data — market may not have opened yet)"
    # bars come back newest-first (sort=desc from API); show most recent 10
    recent = bars[:10]
    lines = [f"5-min bars (today's session, {len(bars)} total, newest first):"]
    for b in recent:
        lines.append(
            f"  {b['t'][:16]}: O={b['o']:.2f} H={b['h']:.2f} "
            f"L={b['l']:.2f} C={b['c']:.2f} V={b['v']:,}"
        )
    return "\n".join(lines)


def _print_entry_decision(d: dict):
    action = d.get("action", "?")
    ticker = d.get("ticker", "?")
    conf   = d.get("confidence", 0)
    risk   = d.get("risk_assessment", "?")
    icon   = "✅" if action == "ENTER" else "⏭️ "
    print(f"   {icon} {action} {ticker} | conf {conf:.0%} | risk {risk}")
    rationale = d.get("entry_rationale") or d.get("news_thesis", "")
    if rationale:
        print(f"      {rationale[:100]}")
    for w in d.get("warnings", []):
        print(f"      ⚠️  {w}")
