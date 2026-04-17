"""
agent/stock_loop.py
Main agentic trading loop for stock day trading.

Flow:
  1. Wait for market open
  2. Morning scan → watchlist (with web search)
  3. Every 60s: ratchet-stop check on all open positions
  4. Every 5 min: Claude monitors positions + evaluates new entries from watchlist
  5. Force exit all positions by STOCK_FORCE_EXIT_TIME
  6. Hard stop at MARKET_CLOSE
"""

import time as _time
from datetime import datetime

import pytz

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
from agent.stock_decision import run_morning_scan, get_entry_decision, monitor_stock_positions
from agent.stock_exits import RatchetTracker, StockRiskManager
from tools.alpaca_stock_tools import (
    place_stock_order,
    close_stock_position,
    close_all_stock_positions,
    get_stock_quote,
)
from tools.logger import log_trade, log_error, log_pnl
from tools.signal_queue import drain as drain_signals, mark_acted, already_acted, reset_daily

# ── Shared state ───────────────────────────────────────────
state = {
    "morning_thesis":         None,
    "trackers":               {},    # ticker → RatchetTracker
    "scan_count":             0,
    "last_claude_call_time":  0,
    "midday_scan_done":       False,  # only one mid-day re-scan per session
}
risk_manager = StockRiskManager()

CLAUDE_CALL_INTERVAL = 300   # 5 min between Claude calls
MIDDAY_SCAN_TIME     = "10:00"  # PST — refresh watchlist once mid-morning


# ──────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────

def _pst_now() -> str:
    return datetime.now(pytz.timezone(config.TIMEZONE)).strftime("%H:%M")


def _print_status():
    state["scan_count"] += 1
    pos_str = ", ".join(state["trackers"].keys()) or "none"
    print(f"\n{'─'*60}")
    print(f"🔄 Scan #{state['scan_count']} | {_pst_now()} PST | {risk_manager.summary()}")
    print(f"   Open: {pos_str}")


# ──────────────────────────────────────────────────────────
# EXECUTE ENTRY
# ──────────────────────────────────────────────────────────

def execute_entry(ticker: str, decision: dict) -> bool:
    """Buy whole shares of `ticker` based on Claude's decision."""
    shares = int(decision.get("shares") or 0)
    price  = float(decision.get("current_price") or 0)

    if shares < 1 or price <= 0:
        print(f"⏭️  {ticker}: shares={shares} price={price} — skipping")
        return False

    # Portfolio-level guard
    current_exposure = sum(
        t.shares * t.entry_price for t in state["trackers"].values()
    )
    order_cost = shares * price
    if current_exposure + order_cost > config.STOCK_MAX_PORTFOLIO * 1.05:
        print(
            f"⏭️  {ticker}: portfolio limit — "
            f"${current_exposure:.0f} + ${order_cost:.0f} > ${config.STOCK_MAX_PORTFOLIO:.0f}"
        )
        return False

    can_trade, reason = risk_manager.can_trade(open_positions=len(state["trackers"]))
    if not can_trade:
        print(f"🛑 Risk gate: {reason}")
        return False

    confidence = float(decision.get("confidence") or 0)
    if confidence < 0.60:
        print(f"⏭️  {ticker}: confidence {confidence:.0%} < 60% — skipping")
        return False

    print(f"📤 BUY {shares}sh {ticker} @ ~${price:.2f}  (${order_cost:.0f})")

    if config.DRY_RUN:
        print(f"   🔍 DRY RUN — order not placed")
        result = {"id": "DRY_RUN", "status": "simulated"}
    else:
        result = place_stock_order(ticker, shares, "buy")

    if result.get("error"):
        log_error("stock_execute_entry", result["error"])
        print(f"❌ Order failed: {result['error']}")
        return False

    state["trackers"][ticker] = RatchetTracker(ticker, price, shares)

    log_trade({
        "symbol":     ticker,
        "side":       "buy",
        "qty":        shares,
        "order_id":   result.get("id"),
        "rationale":  decision.get("entry_rationale"),
        "confidence": confidence,
        "status":     result.get("status"),
    })

    tracker = state["trackers"][ticker]
    print(
        f"✅ Opened {ticker} | {shares}sh | "
        f"entry ${price:.2f} | stop ${tracker.stop_price:.2f}"
    )
    return True


# ──────────────────────────────────────────────────────────
# EXECUTE EXIT
# ──────────────────────────────────────────────────────────

def execute_exit(ticker: str, reason: str) -> bool:
    """Sell all shares of `ticker`."""
    print(f"📤 SELL {ticker} | {reason}")
    tracker = state["trackers"].get(ticker)

    if config.DRY_RUN:
        print(f"   🔍 DRY RUN — not placed")
        result = {}
    else:
        result = close_stock_position(ticker)

    if result.get("error"):
        # Fallback: explicit sell order
        shares = tracker.shares if tracker else 1
        sell = place_stock_order(ticker, shares, "sell")
        if sell.get("error"):
            log_error("stock_execute_exit", sell["error"])
            print(f"❌ Exit failed: {sell['error']}")
            return False

    state["trackers"].pop(ticker, None)

    quote      = get_stock_quote(ticker)
    exit_price = float(quote.get("mid") or 0)
    pnl = 0.0
    if tracker and exit_price:
        pnl = (exit_price - tracker.entry_price) * tracker.shares

    risk_manager.record_trade(pnl)

    log_trade({
        "symbol": ticker,
        "side":   "sell",
        "qty":    tracker.shares if tracker else 0,
        "pnl":    pnl,
        "reason": reason,
    })

    emoji = "🟢" if pnl >= 0 else "🔴"
    print(f"{emoji} Closed {ticker} | P&L ${pnl:+.2f} | {reason}")
    return True


# ──────────────────────────────────────────────────────────
# ANALYST SIGNAL PROCESSING (every scan, ~60s)
# Drains Discord/Twitter call signals and fast-paths them to Claude
# ──────────────────────────────────────────────────────────

def process_analyst_signals():
    """
    Drain the signal queue, evaluate each new call signal via Claude, and
    enter the underlying stock if confirmed.  Runs every 60s alongside the
    ratchet check — much faster than the 5-min Claude cycle.
    """
    signals = drain_signals()
    if not signals:
        return

    now = _pst_now()
    if now >= config.STOCK_NO_NEW_TRADES_TIME:
        print(f"   ⏭️  Signal queue: {len(signals)} signal(s) skipped — past no-new-trades time")
        return

    can_trade, reason = risk_manager.can_trade(open_positions=len(state["trackers"]))
    if not can_trade:
        print(f"   ⏭️  Signal queue: {len(signals)} signal(s) skipped — {reason}")
        return

    thesis = state.get("morning_thesis") or {}

    for sig in signals:
        ticker      = sig["ticker"]
        signal_type = sig.get("signal_type", "call")

        print(
            f"\n   📣 ANALYST SIGNAL → {ticker} [{signal_type.upper()}] | "
            f"from [{sig['source_label']}] | conf {sig['confidence']:.0%}"
        )
        print(f"      \"{sig['raw_text'][:120]}\"")

        # ── PUT signal: sell the stock if we're holding it ──────
        if signal_type == "put":
            if ticker in state["trackers"]:
                execute_exit(
                    ticker,
                    f"analyst PUT signal from [{sig['source_label']}]: "
                    f"{sig['raw_text'][:60]}",
                )
            else:
                print(f"   ⏭️  PUT signal {ticker}: not in portfolio — nothing to sell")
            # Don't mark_acted for puts — a subsequent call signal should still be evaluated
            continue

        # ── CALL signal: evaluate entry ─────────────────────────

        # Skip if already holding this ticker
        if ticker in state["trackers"]:
            print(f"   ⏭️  CALL signal {ticker}: already in portfolio — skip")
            mark_acted(ticker)
            continue

        # Skip if already acted on this ticker's signal today
        if already_acted(ticker):
            print(f"   ⏭️  CALL signal {ticker}: already evaluated today — skip")
            continue

        mark_acted(ticker)

        # Re-check capacity before each entry attempt
        can_trade, reason = risk_manager.can_trade(open_positions=len(state["trackers"]))
        if not can_trade:
            print(f"   ⏭️  CALL signal {ticker}: {reason}")
            break

        # Find or construct a bias from the morning watchlist
        watchlist_entry = next(
            (s for s in thesis.get("watchlist", []) if s.get("ticker") == ticker),
            None,
        )
        bias = watchlist_entry.get("bias", "bullish") if watchlist_entry else "bullish"

        decision = get_entry_decision(
            ticker=ticker,
            bias=bias,
            morning_thesis=thesis,
            daily_pnl=risk_manager.daily_pnl,
            open_position_count=len(state["trackers"]),
            analyst_signal=sig,
        )

        if decision.get("action") == "ENTER":
            execute_entry(ticker, decision)
        else:
            print(
                f"   ⏭️  Claude skipped CALL signal {ticker}: "
                f"{decision.get('entry_rationale', '')[:80]}"
            )


# ──────────────────────────────────────────────────────────
# RATCHET CHECK (every scan, ~60s, no Claude)
# ──────────────────────────────────────────────────────────

def run_ratchet_check():
    """Check all open positions against their ratcheting stops."""
    for ticker in list(state["trackers"].keys()):
        tracker = state["trackers"].get(ticker)
        if not tracker:
            continue
        quote = get_stock_quote(ticker)
        price = float(quote.get("mid") or 0)
        if not price:
            print(f"   ⚠️  {ticker}: no quote — skipping ratchet check")
            continue
        result = tracker.update(price)
        if result.get("exit"):
            execute_exit(ticker, result["reason"])
        else:
            print(
                f"   📊 {ticker}: ${price:.2f} | "
                f"P&L {result['pnl_pct']:+.1f}% (${result['pnl_dollar']:+.0f}) | "
                f"stop ${result['stop']:.2f} [{result['level']}]"
            )


# ──────────────────────────────────────────────────────────
# MAIN LOOP
# ──────────────────────────────────────────────────────────

def run():
    """Entry point called from stock_main.py."""
    print("\n" + "=" * 60)
    print(f"{'📄 PAPER' if config.PAPER_MODE else '🔴 LIVE'} STOCK DAY TRADER")
    print(f"   Portfolio max:  ${config.STOCK_MAX_PORTFOLIO:,.0f}")
    print(f"   Per position:   ${config.STOCK_BASE_ALLOCATION:,.0f} baseline")
    print(f"   Max positions:  {config.STOCK_MAX_POSITIONS}")
    print(f"   Initial stop:   {config.STOCK_INITIAL_STOP_PCT:+.0f}%  (ratchets up)")
    print(f"   Force exit:     {config.STOCK_FORCE_EXIT_TIME} PST")
    print("=" * 60 + "\n")

    # ── Start background signal threads ────────────────────
    # Both are no-ops if their credentials are not set in .env
    try:
        from tools.twitter_tools import start_twitter_stream
        start_twitter_stream()
    except Exception as e:
        print(f"   ⚠️  Twitter stream init failed: {e}")

    try:
        from tools.discord_tools import start_discord_poller
        start_discord_poller()
    except Exception as e:
        print(f"   ⚠️  Discord poller init failed: {e}")

    # ── Reset daily signal state ───────────────────────────
    reset_daily()

    # ── Wait for market open ───────────────────────────────
    while True:
        now = _pst_now()
        if now >= config.MARKET_OPEN:
            break
        print(f"⏳ Waiting for market open... {now} PST", end="\r")
        _time.sleep(30)
    print(f"\n🔔 Market open! {_pst_now()} PST\n")

    # ── Morning scan ───────────────────────────────────────
    state["morning_thesis"] = run_morning_scan()
    state["last_claude_call_time"] = _time.time()

    # ── Main loop ──────────────────────────────────────────
    while True:
        now = _pst_now()
        _print_status()

        # Hard stop: market closed
        if now >= config.MARKET_CLOSE:
            if state["trackers"]:
                print("🏁 Market close — force-exiting all positions")
                for ticker in list(state["trackers"].keys()):
                    execute_exit(ticker, "end-of-day force exit")
            print("🏁 Market closed.")
            break

        # Force exit window (configurable, default 12:45 PST)
        if now >= config.STOCK_FORCE_EXIT_TIME and state["trackers"]:
            print(f"⏰ Force exit time ({now} PST) — closing all positions")
            for ticker in list(state["trackers"].keys()):
                execute_exit(ticker, f"force exit at {now} PST")

        # Daily loss halt
        if risk_manager.halted:
            if state["trackers"]:
                for ticker in list(state["trackers"].keys()):
                    execute_exit(ticker, f"halt: {risk_manager.halt_reason}")
            print(f"🛑 Halted: {risk_manager.halt_reason}")
            _time.sleep(300)
            continue

        # ── Analyst signal processing (every 60s) ─────────
        # Drains Discord/Twitter call signals; fast-paths to Claude entry check
        process_analyst_signals()

        # ── Ratchet check (every 60s, free) ───────────────
        run_ratchet_check()

        # ── Mid-morning re-scan (once, ~10:00 PST) ────────────
        # Refreshes the watchlist with any new catalysts that broke after open.
        if (
            not state["midday_scan_done"]
            and now >= MIDDAY_SCAN_TIME
            and now < config.STOCK_NO_NEW_TRADES_TIME
            and not risk_manager.halted
        ):
            print(f"\n🔄 Mid-morning re-scan ({now} PST) — refreshing watchlist…")
            fresh = run_morning_scan()
            if fresh and fresh.get("watchlist"):
                # Merge: keep tickers already on watchlist, append new ones
                existing_tickers = {
                    s["ticker"] for s in state["morning_thesis"].get("watchlist", [])
                }
                new_picks = [
                    s for s in fresh["watchlist"]
                    if s.get("ticker") not in existing_tickers
                ]
                state["morning_thesis"]["watchlist"].extend(new_picks)
                # Update market-level context
                state["morning_thesis"]["market_summary"] = fresh.get(
                    "market_summary", state["morning_thesis"].get("market_summary", "")
                )
                state["morning_thesis"]["market_bias"] = fresh.get(
                    "market_bias", state["morning_thesis"].get("market_bias", "mixed")
                )
                added = [s["ticker"] for s in new_picks]
                print(
                    f"   ✅ Re-scan complete. "
                    f"New tickers added: {added if added else 'none (all already on list)'}"
                )
            state["midday_scan_done"] = True

        # ── Claude calls (every 5 min) ─────────────────────
        now_ts = _time.time()
        if now_ts - state["last_claude_call_time"] >= CLAUDE_CALL_INTERVAL:
            state["last_claude_call_time"] = now_ts

            # 1. Monitor open positions
            if state["trackers"]:
                decisions = monitor_stock_positions(
                    state["trackers"],
                    daily_pnl=risk_manager.daily_pnl,
                )
                for ticker, d in decisions.items():
                    if d.get("action") == "EXIT":
                        execute_exit(ticker, f"Claude: {d.get('rationale', '')}")

            # 2. Look for new entries
            if now < config.STOCK_NO_NEW_TRADES_TIME:
                can_trade, reason = risk_manager.can_trade(
                    open_positions=len(state["trackers"])
                )
                if can_trade:
                    watchlist = state["morning_thesis"].get("watchlist", [])
                    for stock in watchlist:
                        ticker = (stock.get("ticker") or "").upper().strip()
                        if not ticker or ticker in state["trackers"]:
                            continue
                        if len(state["trackers"]) >= config.STOCK_MAX_POSITIONS:
                            break
                        decision = get_entry_decision(
                            ticker=ticker,
                            bias=stock.get("bias", "neutral"),
                            morning_thesis=state["morning_thesis"],
                            daily_pnl=risk_manager.daily_pnl,
                            open_position_count=len(state["trackers"]),
                        )
                        if decision.get("action") == "ENTER":
                            execute_entry(ticker, decision)
                else:
                    print(f"   ⏭️  {reason}")
            else:
                print(f"   ⏭️  No new entries after {config.STOCK_NO_NEW_TRADES_TIME} PST")

            secs_to_next = CLAUDE_CALL_INTERVAL
        else:
            secs_to_next = int(CLAUDE_CALL_INTERVAL - (now_ts - state["last_claude_call_time"]))
            print(f"   ⏳ Next Claude call in {secs_to_next}s")

        sleep_secs = min(60, max(10, secs_to_next))
        print(f"   💤 Next scan in {sleep_secs}s")
        _time.sleep(sleep_secs)

    # ── End of day summary ─────────────────────────────────
    print("\n" + "=" * 60)
    print("📊 END OF DAY — STOCK TRADER")
    print(f"   {risk_manager.summary()}")
    print("=" * 60)
    log_pnl(risk_manager.daily_pnl, risk_manager.trades_today)
