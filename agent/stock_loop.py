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
    get_order_fill,
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


def _pst_now_full() -> str:
    """HH:MM with seconds for trade prints."""
    return datetime.now(pytz.timezone(config.TIMEZONE)).strftime("%H:%M:%S")


def _hold_mins(entry_time: str) -> str:
    """Return 'Xm' since entry_time (HH:MM)."""
    try:
        now = datetime.now(pytz.timezone(config.TIMEZONE))
        h, m = map(int, entry_time.split(":"))
        entry = now.replace(hour=h, minute=m, second=0, microsecond=0)
        mins = int((now - entry).total_seconds() / 60)
        return f"{mins}m"
    except Exception:
        return "?"


def _print_status():
    state["scan_count"] += 1
    n   = len(state["trackers"])
    pos = f"{n} open ({', '.join(state['trackers'].keys())})" if n else "no positions"
    print(f"\n{'─'*60}")
    print(f"  {_pst_now()} PST  |  {risk_manager.summary()}  |  {pos}")


# ──────────────────────────────────────────────────────────
# EXECUTE ENTRY
# ──────────────────────────────────────────────────────────

def execute_entry(ticker: str, decision: dict, signal_source: str = "morning scan") -> bool:
    """Buy whole shares of `ticker` based on Claude's decision."""
    shares = int(decision.get("shares") or 0)
    price  = float(decision.get("current_price") or 0)

    if shares < 1 or price <= 0:
        return False

    current_exposure = sum(t.shares * t.entry_price for t in state["trackers"].values())
    order_cost = shares * price
    if current_exposure + order_cost > config.STOCK_MAX_PORTFOLIO * 1.05:
        print(f"   ⏭️  {ticker}: portfolio cap reached — skip")
        return False

    can_trade, reason = risk_manager.can_trade(open_positions=len(state["trackers"]))
    if not can_trade:
        print(f"   🛑 {ticker}: {reason}")
        return False

    confidence = float(decision.get("confidence") or 0)
    if confidence < 0.60:
        print(f"   ⏭️  {ticker}: confidence {confidence:.0%} < 60% — skip")
        return False

    if config.DRY_RUN:
        result = {"id": "DRY_RUN", "status": "simulated"}
        fill_price = price
    else:
        result = place_stock_order(ticker, shares, "buy")

    if result.get("error"):
        log_error("stock_execute_entry", result["error"])
        print(f"   ❌ {ticker}: order failed — {result['error']}")
        return False

    if not config.DRY_RUN:
        order_id = result.get("id", "")
        fill_price = get_order_fill(order_id, timeout_secs=15) if order_id else None
        if not fill_price:
            fill_price = price  # fallback to quote

    now_str = _pst_now_full()
    state["trackers"][ticker] = RatchetTracker(
        ticker, fill_price, shares, source=signal_source, entry_time=_pst_now()
    )
    tracker = state["trackers"][ticker]

    slippage_str = ""
    if not config.DRY_RUN and abs(fill_price - price) >= 0.05:
        slippage_str = f"  slip ${fill_price - price:+.2f}"

    dry_tag = "  [DRY RUN]" if config.DRY_RUN else ""
    print(
        f"\n  ✅ ENTERED  {ticker}{dry_tag}"
        f"\n     {shares}sh @ ${fill_price:.2f}{slippage_str}  =  ${fill_price*shares:,.0f}"
        f"\n     stop ${tracker.stop_price:.2f}  |  conf {confidence:.0%}"
        f"\n     catalyst: {(decision.get('news_thesis') or decision.get('entry_rationale') or '')[:80]}"
        f"\n     source: {signal_source}  |  time: {now_str} PST"
    )

    log_trade({
        "symbol":      ticker,
        "side":        "buy",
        "qty":         shares,
        "order_id":    result.get("id"),
        "fill_price":  fill_price,
        "quote_price": price,
        "source":      signal_source,
        "rationale":   decision.get("entry_rationale"),
        "confidence":  confidence,
        "status":      result.get("status"),
        "time":        now_str,
    })
    return True


# ──────────────────────────────────────────────────────────
# EXECUTE EXIT
# ──────────────────────────────────────────────────────────

def execute_exit(ticker: str, reason: str) -> bool:
    """Sell exactly the shares we opened this session for `ticker`.

    We deliberately avoid Alpaca's 'close entire position' endpoint so that
    any pre-existing shares in the account (opened manually or in a prior
    session) are never touched.
    """
    tracker = state["trackers"].get(ticker)
    if not tracker:
        print(f"   ⚠️  {ticker}: no open position tracked — nothing to sell")
        return False

    shares = tracker.shares   # only the qty WE opened

    if config.DRY_RUN:
        result = {}
        exit_fill = float(get_stock_quote(ticker).get("mid") or 0)
    else:
        result = place_stock_order(ticker, shares, "sell")

    if result.get("error"):
        log_error("stock_execute_exit", result["error"])
        print(f"   ❌ {ticker}: exit order failed — {result['error']}")
        return False

    if not config.DRY_RUN:
        order_id = result.get("id", "")
        exit_fill = get_order_fill(order_id, timeout_secs=15) if order_id else None
        if not exit_fill:
            exit_fill = float(get_stock_quote(ticker).get("mid") or 0)

    state["trackers"].pop(ticker, None)

    pnl = (exit_fill - tracker.entry_price) * tracker.shares if exit_fill else 0.0
    risk_manager.record_trade(pnl)

    pnl_pct  = pnl / (tracker.entry_price * tracker.shares) * 100 if tracker.entry_price else 0
    emoji    = "🟢" if pnl >= 0 else "🔴"
    held     = _hold_mins(tracker.entry_time)
    dry_tag  = "  [DRY RUN]" if config.DRY_RUN else ""

    print(
        f"\n  {emoji} EXITED   {ticker}{dry_tag}"
        f"\n     entry ${tracker.entry_price:.2f}  →  exit ${exit_fill:.2f}"
        f"  |  P&L ${pnl:+.2f} ({pnl_pct:+.1f}%)"
        f"\n     held {held}  |  reason: {reason}"
        f"\n     source: {tracker.source}  |  daily P&L: ${risk_manager.daily_pnl:+.2f}"
    )

    log_trade({
        "symbol":      ticker,
        "side":        "sell",
        "qty":         tracker.shares,
        "fill_price":  exit_fill,
        "entry_price": tracker.entry_price,
        "pnl":         round(pnl, 2),
        "pnl_pct":     round(pnl_pct, 2),
        "held_mins":   held,
        "source":      tracker.source,
        "reason":      reason,
        "time":        _pst_now_full(),
    })
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

    thesis = state.get("morning_thesis")
    if thesis is None:
        print("   ⏭️  Signal queue: morning scan hasn't run yet — signals deferred")
        # Put them back so they're re-evaluated after the scan runs
        from tools.signal_queue import push as _push_sig
        for sig in signals:
            _push_sig(sig)
        return
    thesis = thesis or {}

    for sig in signals:
        ticker      = sig["ticker"]
        signal_type = sig.get("signal_type", "call")
        src_label   = sig.get("source_label", sig.get("source", "unknown"))
        src_type    = sig.get("source", "unknown")   # "discord" | "twitter"
        src_display = (
            f"Discord: {src_label}" if src_type == "discord"
            else f"Twitter: @{src_label}"
        )

        # ── PUT signal: sell the stock if we're holding it ──────
        if signal_type == "put":
            if ticker in state["trackers"]:
                print(f"\n  📣 PUT signal  {ticker} from {src_display} (conf {sig['confidence']:.0%})")
                print(f"     \"{sig['raw_text'][:100]}\"")
                execute_exit(ticker, f"PUT signal from {src_display}")
            # Don't mark_acted for puts — a subsequent call signal should still be evaluated
            continue

        # ── CALL signal: evaluate entry ─────────────────────────

        if ticker in state["trackers"]:
            mark_acted(ticker)
            continue  # already holding — no noise

        if already_acted(ticker):
            continue  # already evaluated today — silent skip

        can_trade, reason = risk_manager.can_trade(open_positions=len(state["trackers"]))
        if not can_trade:
            print(f"   ⏭️  CALL {ticker} from {src_display}: {reason}")
            break

        mark_acted(ticker)

        print(f"\n  📣 CALL signal  {ticker} from {src_display} (conf {sig['confidence']:.0%})")
        print(f"     \"{sig['raw_text'][:100]}\"")

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
            execute_entry(ticker, decision, signal_source=src_display)
        else:
            print(
                f"   ⏭️  Claude skipped {ticker}: "
                f"{decision.get('entry_rationale', '')[:80]}"
            )


# ──────────────────────────────────────────────────────────
# RATCHET CHECK (every scan, ~60s, no Claude)
# ──────────────────────────────────────────────────────────

def run_ratchet_check():
    """Check all open positions against their ratcheting stops."""
    if not state["trackers"]:
        return
    lines = []
    for ticker in list(state["trackers"].keys()):
        tracker = state["trackers"].get(ticker)
        if not tracker:
            continue
        quote = get_stock_quote(ticker)
        price = float(quote.get("mid") or 0)
        if not price:
            continue
        result = tracker.update(price)
        if result.get("exit"):
            execute_exit(ticker, result["reason"])
        else:
            held = _hold_mins(tracker.entry_time)
            lines.append(
                f"   📊 {ticker}  ${price:.2f}"
                f"  P&L {result['pnl_pct']:+.1f}% (${result['pnl_dollar']:+.0f})"
                f"  stop ${result['stop']:.2f}  held {held}"
            )
    if lines:
        print("\n".join(lines))


# ──────────────────────────────────────────────────────────
# MAIN LOOP
# ──────────────────────────────────────────────────────────

def run():
    """Entry point called from stock_main.py."""
    twitter_accts  = ", ".join(f"@{a}" for a in (getattr(config, "TWITTER_ACCOUNTS", []) or []))
    discord_ids    = getattr(config, "DISCORD_CHANNEL_IDS", []) or []
    signal_sources = []
    if twitter_accts:
        signal_sources.append(f"Twitter ({twitter_accts})")
    if discord_ids:
        signal_sources.append(f"Discord ({len(discord_ids)} channel{'s' if len(discord_ids)!=1 else ''})")
    if not signal_sources:
        signal_sources.append("morning scan only")

    print("\n" + "=" * 60)
    print(f"{'📄 PAPER' if config.PAPER_MODE else '🔴 LIVE'} STOCK DAY TRADER")
    print(f"   Portfolio max:  ${config.STOCK_MAX_PORTFOLIO:,.0f}  |  per position ${config.STOCK_BASE_ALLOCATION:,.0f}")
    print(f"   Max positions:  {config.STOCK_MAX_POSITIONS}  |  stop {config.STOCK_INITIAL_STOP_PCT:+.0f}% ratcheting")
    print(f"   Force exit:     {config.STOCK_FORCE_EXIT_TIME} PST  |  no new trades after {config.STOCK_NO_NEW_TRADES_TIME} PST")
    print(f"   Signal sources: {', '.join(signal_sources)}")
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

    # ── Reset daily state ─────────────────────────────────
    reset_daily()
    state["force_exit_done"] = False

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
        # The flag ensures we attempt force-close exactly once per session
        if now >= config.STOCK_FORCE_EXIT_TIME and not state.get("force_exit_done"):
            if state["trackers"]:
                print(f"⏰ Force exit time ({now} PST) — closing all positions")
                for ticker in list(state["trackers"].keys()):
                    execute_exit(ticker, f"force exit at {now} PST")
            state["force_exit_done"] = True
            continue  # skip new-entry logic for the rest of this tick

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
                if added:
                    print(f"   ✅ Re-scan: added {', '.join(added)} to watchlist")
                else:
                    print(f"   ✅ Re-scan: watchlist unchanged")
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
                        execute_exit(ticker, f"Claude: {d.get('rationale', '')[:80]}")

            # 2. Look for new entries from watchlist
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
                            execute_entry(ticker, decision, signal_source="morning scan")

        sleep_secs = min(60, max(10, int(
            CLAUDE_CALL_INTERVAL - (_time.time() - state["last_claude_call_time"])
        )))
        _time.sleep(sleep_secs)

    # ── End of day summary ─────────────────────────────────
    emoji = "🟢" if risk_manager.daily_pnl >= 0 else "🔴"
    print("\n" + "=" * 60)
    print(f"{emoji} END OF DAY — STOCK TRADER")
    print(f"   Daily P&L: ${risk_manager.daily_pnl:+.2f}  |  Trades: {risk_manager.trades_today}")
    print("=" * 60)
    log_pnl(risk_manager.daily_pnl, risk_manager.trades_today)
