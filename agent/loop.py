"""
agent/loop.py
Main agentic trading loop — orchestrates signals, decisions, execution.
Uses professional exit management with trailing stops.
"""

import time
from datetime import datetime
import pytz

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
from agent.decision import run_morning_scan, get_entry_decision, monitor_positions
from agent.exits import check_exit, init_position, clear_position, risk_manager
from tools.trump_tools import check_new_trump_posts
from tools.alpaca_tools import (
    place_options_order,
    close_position,
    close_all_positions,
    get_positions,
    get_account,
    get_market_clock,
    resolve_option_occ_symbol,
    build_occ_option_symbol,
)
from tools.logger import log_trade, log_error, log_pnl

# ── State ──────────────────────────────────────────────────
state = {
    "morning_thesis":   None,
    "in_trade":         False,
    "current_symbol":   None,
    "entry_price":      0.0,
    "scan_count":       0,
}

def _pst_now() -> str:
    return datetime.now(pytz.timezone(config.TIMEZONE)).strftime("%H:%M")

def _is_elevated_window() -> bool:
    now = _pst_now()
    for start, end in config.ELEVATED_WINDOWS:
        if start <= now <= end:
            return True
    return False

def _print_status():
    now = _pst_now()
    summary = risk_manager.summary()
    print(f"\n{'─'*60}")
    print(f"🔄 Scan #{state['scan_count']} | {now} PST | {summary}")


def _normalize_expiry(s: str) -> str:
    if not s:
        return ""
    t = str(s).strip().replace("/", "-")
    return t[:10] if len(t) >= 10 else t

# ──────────────────────────────────────────────────────────
# EXECUTE ENTRY
# ──────────────────────────────────────────────────────────
def execute_entry(decision: dict) -> bool:
    # ── HARD GUARD: buy-only system ──────────────────────
    direction = decision.get("direction", "").lower()
    if direction not in ["call", "put"]:
        print(f"⏭️  Invalid direction '{direction}' — must be call or put")
        return False

    # Reject any attempt to sell-to-open
    if decision.get("side", "buy").lower() != "buy":
        print(f"🛑 BLOCKED: sell-to-open rejected — this system buys only")
        return False

    # Confidence check
    if decision.get("confidence", 0) < 0.55:
        print(f"⏭️  Confidence {decision['confidence']:.0%} too low — skipping")
        return False

    if decision.get("risk_assessment") == "HIGH" and decision.get("confidence", 0) < 0.75:
        print("⏭️  HIGH risk + low confidence — skipping")
        return False

    # Check daily risk limits
    can_trade, reason = risk_manager.can_trade()
    if not can_trade:
        print(f"🛑 Risk manager blocked entry: {reason}")
        return False

    # Premium guardrail — never spend more than MAX_PREMIUM_PER_TRADE
    # Get current ask price from decision or options chain
    ask_price = float(decision.get("ask") or decision.get("entry_price") or 0)
    if ask_price > 0:
        premium_cost = ask_price * 100  # 1 contract = 100 shares
        if premium_cost > config.MAX_PREMIUM_PER_TRADE:
            print(f"⏭️  Premium ${premium_cost:.0f} exceeds max ${config.MAX_PREMIUM_PER_TRADE:.0f} — skipping")
            return False
        print(f"   💰 Premium check: ${premium_cost:.0f} ≤ ${config.MAX_PREMIUM_PER_TRADE:.0f} ✅")

    # Favorites: must match configured ticker + expiry (from favorites.txt via main.py)
    ticker = getattr(config, "ACTIVE_TICKER", "SPY").upper().strip()
    active_exp = _normalize_expiry(getattr(config, "ACTIVE_EXPIRY", "") or "")
    dec_exp = _normalize_expiry(decision.get("expiry") or "")
    if not active_exp:
        print("⏭️  ACTIVE_EXPIRY not set — cannot validate favorites; skipping entry")
        return False
    if dec_exp != active_exp:
        print(
            f"⏭️  Expiry mismatch: model {dec_exp!r} vs favorites {active_exp!r} — "
            "only favorites expiry is allowed"
        )
        return False

    try:
        strike_f = float(decision["strike"])
    except (TypeError, ValueError, KeyError):
        print("⏭️  Invalid or missing strike — skipping")
        return False

    symbol = resolve_option_occ_symbol(
        ticker, active_exp, decision["direction"], strike_f
    )
    if not symbol:
        try:
            symbol = build_occ_option_symbol(
                ticker, active_exp, decision["direction"], strike_f
            )
            print(f"   ⚠️  OCC fallback symbol (no contracts API match): {symbol}")
        except Exception as e:
            print(f"⏭️  Could not resolve option symbol: {e}")
            return False

    print(f"📤 Order: {symbol} | 1 contract | BUY TO OPEN | market")

    # ── DRY RUN: log but don't place ──────────────────────
    if config.DRY_RUN:
        print(f"   🔍 DRY RUN — order NOT placed (--dry-run mode)")
        result = {"order_id": "DRY_RUN", "status": "simulated", "symbol": symbol}
        return True

    # HARD RULE: always buy-to-open, never sell-to-open
    result = place_options_order(symbol=symbol, qty=1, side="buy", order_type="market")

    if result.get("error"):
        log_error("execute_entry", result["error"])
        print(f"❌ Order failed: {result['error']}")
        return False

    # Get entry price from result or use last known price
    entry_price = float(decision.get("entry_price") or 1.0)

    # Initialize exit manager for this position
    init_position(symbol, entry_price)

    log_trade({
        "symbol":    symbol,
        "side":      "buy",
        "qty":       1,
        "order_id":  result.get("order_id"),
        "strategy":  decision.get("strategy"),
        "direction": decision.get("direction"),
        "strike":    decision.get("strike"),
        "expiry":    decision.get("expiry"),
        "rationale": decision.get("entry_rationale"),
        "confidence": decision.get("confidence"),
        "status":    result.get("status"),
    })

    state["in_trade"]       = True
    state["current_symbol"] = symbol
    state["entry_price"]    = entry_price
    print(f"✅ Position opened: {symbol} | Status: {result.get('status')}")
    return True

# ──────────────────────────────────────────────────────────
# EXECUTE EXIT
# ──────────────────────────────────────────────────────────
def execute_exit(symbol: str, reason: str) -> bool:
    print(f"📤 Closing: {symbol} | {reason}")

    result = close_position(symbol)
    if result.get("error"):
        # Fallback: place sell order
        sell = place_options_order(symbol=symbol, qty=1, side="sell", order_type="market")
        if sell.get("error"):
            log_error("execute_exit", sell["error"])
            print(f"❌ Exit failed: {sell['error']}")
            return False

    # Record P&L
    positions = get_positions()
    pnl = 0.0
    for pos in positions:
        if pos["symbol"] == symbol:
            pnl = pos["unrealized_pl"]
            break

    risk_manager.record_trade(pnl)
    clear_position(symbol)

    log_trade({
        "symbol": symbol,
        "side":   "sell",
        "qty":    1,
        "pnl":    pnl,
        "reason": reason,
    })

    state["in_trade"]       = False
    state["current_symbol"] = None
    state["entry_price"]    = 0.0

    emoji = "🟢" if pnl >= 0 else "🔴"
    print(f"{emoji} Position closed | P&L: ${pnl:+.2f} | Reason: {reason}")
    return True

# ──────────────────────────────────────────────────────────
# POSITION MONITOR (runs every scan while in trade)
# ──────────────────────────────────────────────────────────
def run_position_monitor() -> bool:
    """
    Returns True if position was exited.
    Uses hard Python rules first, Claude only for nuanced decisions.
    """
    symbol = state["current_symbol"]
    if not symbol:
        return False

    # Get current P&L
    positions = get_positions()
    current_pnl_pct = 0.0
    current_pnl_usd = 0.0
    for pos in positions:
        if pos["symbol"] == symbol:
            current_pnl_pct = pos["pnl_pct"]
            current_pnl_usd = pos["unrealized_pl"]
            break

    print(f"   📊 Position: {symbol} | P&L: ${current_pnl_usd:+.2f} ({current_pnl_pct:+.1f}%)")

    # ── Run hard Python exit rules first ──────────────────
    exit_check = check_exit(symbol, current_pnl_pct)

    if exit_check["exit"]:
        print(f"   {exit_check['reason']}")
        execute_exit(symbol, exit_check["reason"])
        return True

    # ── Print hold status ──────────────────────────────────
    print(f"   ⏸️  {exit_check['reason']}")

    # ── Ask Claude for nuanced read every 15 min ──────────
    # Only when not in elevated window (save API cost)
    scan = state["scan_count"]
    if scan % 15 == 0:  # approx every 15 min at 60s intervals
        claude_decision = monitor_positions()
        if claude_decision.get("action") == "EXIT_TRADE":
            reason = claude_decision.get("exit_rationale", "Claude exit signal")
            execute_exit(symbol, f"Claude: {reason}")
            return True

    return False

# ──────────────────────────────────────────────────────────
# MAIN LOOP
# ──────────────────────────────────────────────────────────
def run():
    print("\n" + "="*60)
    print("🚀 SPY TRADER — STARTING")
    print(f"   Mode:  {'📄 PAPER' if config.PAPER_MODE else '🔴 LIVE'}")
    print(f"   Time:  {_pst_now()} PST")
    print(f"   Model: {config.CLAUDE_MODEL}")
    print("="*60 + "\n")

    config.validate()

    # Account check
    try:
        account = get_account()
        print(f"💰 Equity: ${account['equity']:,.2f} | "
              f"Buying Power: ${account['buying_power']:,.2f}")
    except Exception as e:
        print(f"❌ Alpaca connection failed: {e}")
        return

    # Morning scan
    print(f"\n🌅 Running morning scan...")
    state["morning_thesis"] = run_morning_scan()

    # Wait for market open
    while _pst_now() < config.MARKET_OPEN:
        print(f"⏳ Waiting for market open... {_pst_now()} PST", end="\r")
        time.sleep(30)
    print(f"\n🔔 Market open! {_pst_now()} PST\n")

    # ── Main loop ──────────────────────────────────────────
    while True:
        now = _pst_now()
        state["scan_count"] += 1
        _print_status()

        # ── Check for new Trump posts (every 60s, ~$0.00006/call) ──
        try:
            trump = check_new_trump_posts()
            if trump.get("new_post"):
                impact = trump.get("impact", "NEUTRAL")
                alert  = trump.get("alert", "")
                print(f"\n   🚨 NEW TRUMP POST DETECTED!")
                print(f"   Impact: {impact}")
                print(f"   {alert}")
                # High impact bearish → force Claude to reassess sooner
                if "HIGH" in impact:
                    state["last_claude_call_time"] = 0  # trigger immediate scan
                    print(f"   ⚡ High impact — triggering immediate Claude scan")
        except Exception:
            pass  # never let Trump check break the trading loop

        # ── Hard stop: market closed ───────────────────────
        if now >= config.MARKET_CLOSE:
            print("🏁 Market closed.")
            break

        # ── Hard stop: daily loss ──────────────────────────
        if risk_manager.halted:
            print(f"🛑 Trading halted: {risk_manager.halt_reason}")
            if state["in_trade"]:
                execute_exit(state["current_symbol"], "halt — daily loss limit")
            break

        # ── Monitor open position ──────────────────────────
        if state["in_trade"]:
            exited = run_position_monitor()
            sleep_secs = (config.ELEVATED_SCAN_INTERVAL
                         if _is_elevated_window() else 60)

        # ── Look for new entry ─────────────────────────────
        elif now < config.NO_NEW_TRADES_TIME:
            can_trade, reason = risk_manager.can_trade()
            if not can_trade:
                print(f"   ⏭️  {reason}")
                sleep_secs = 300
            else:
                # ── Claude call schedule ───────────────────
                # Price scans (Python): every 60s — FREE
                # Claude calls: every 5 min — ~$0.08 each
                # Mon-Thu cutoff: 11:30 AM PST
                # Friday cutoff:   8:30 AM PST
                from datetime import date as _date
                import time as _t
                is_friday = _date.today().weekday() == 4
                claude_cutoff = "08:30" if is_friday else config.NO_NEW_TRADES_TIME

                if now >= claude_cutoff:
                    day_str = "Friday" if is_friday else "today"
                    print(f"   ⏭️  {day_str} Claude cutoff ({claude_cutoff} PST) — no more API calls")
                    sleep_secs = 300
                else:
                    CLAUDE_CALL_INTERVAL = 300  # 5 min between Claude calls
                    last_call = state.get("last_claude_call_time", 0)
                    now_ts = _t.time()

                    if now_ts - last_call >= CLAUDE_CALL_INTERVAL:
                        state["last_claude_call_time"] = now_ts
                        decision = get_entry_decision(
                            state["morning_thesis"],
                            risk_manager.daily_pnl,
                            risk_manager.trades_today,
                        )
                        if decision.get("action") == "ENTER_TRADE":
                            execute_entry(decision)
                        elif decision.get("action") == "HALT_TRADING":
                            risk_manager.halted = True
                            risk_manager.halt_reason = "Claude called HALT"
                    else:
                        secs_left = int(CLAUDE_CALL_INTERVAL - (now_ts - last_call))
                        print(f"   ⏳ Next Claude scan in {secs_left}s (price check only)")

                    sleep_secs = 60  # price loop 60s, Claude every 5 min
        else:
            print(f"   ⏭️  No new trades after {config.NO_NEW_TRADES_TIME} PST")
            if state["in_trade"]:
                sleep_secs = 60
            else:
                # No position, no new trades → sleep 5 min, no Claude call
                sleep_secs = 300

        print(f"   💤 Next scan in {sleep_secs}s")
        time.sleep(sleep_secs)

    # ── End of day ─────────────────────────────────────────
    print("\n" + "="*60)
    print("📊 END OF DAY")
    print(f"   {risk_manager.summary()}")
    print("="*60)
    log_pnl(risk_manager.daily_pnl, risk_manager.trades_today)
