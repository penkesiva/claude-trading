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

# ── Shared state ───────────────────────────────────────────
state = {
    "morning_thesis":         None,
    "trackers":               {},    # ticker → RatchetTracker
    "scan_count":             0,
    "last_claude_call_time":  0,
}
risk_manager = StockRiskManager()

CLAUDE_CALL_INTERVAL = 300   # 5 min between Claude calls


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
    if confidence < 0.55:
        print(f"⏭️  {ticker}: confidence {confidence:.0%} < 55% — skipping")
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

        # ── Ratchet check (every 60s, free) ───────────────
        run_ratchet_check()

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
