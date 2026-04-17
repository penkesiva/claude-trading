"""
stock_main.py — Entry point for the stock day-trading agent.

No options. No favorites.txt. Pure news-driven US equity day trading.

Usage:
  python stock_main.py --paper            → paper trading (safe, use first)
  python stock_main.py --live             → live trading  (real money!)
  python stock_main.py --paper --dry-run  → scan + decide, no orders placed

Portfolio: up to $10,000 across max 5 positions (~$2K baseline each).
Stop loss:  -5% initial, ratchets up as gains increase.
Signals:    Alpaca News, NewsAPI (optional), Claude web search.
"""

import sys
import argparse
import os

# ── Parse arguments ────────────────────────────────────────
parser = argparse.ArgumentParser(
    description="Stock Day Trader — news-driven, no options",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog="""
Examples:
  python stock_main.py --paper          (paper trading, safe to test)
  python stock_main.py --live           (real money — be sure!)
  python stock_main.py --paper --dry-run (analysis only, no orders)
    """,
)

mode_group = parser.add_mutually_exclusive_group()
mode_group.add_argument("--paper", action="store_true", help="Force paper trading mode")
mode_group.add_argument("--live",  action="store_true", help="Force live trading mode (real money!)")
parser.add_argument("--dry-run",   action="store_true", help="Decide but never place orders")

args = parser.parse_args()

# ── Apply mode overrides BEFORE importing config ───────────
# Safety default: always paper unless --live is explicitly passed.
# This overrides whatever PAPER_MODE is set to in .env so there is
# no way to accidentally go live by omitting a flag.
if args.live:
    os.environ["PAPER_MODE"] = "false"
    print("🔴 Mode: LIVE TRADING")
    print("\n⚠️  WARNING: This will place REAL orders with REAL MONEY.")
    print("   Make sure paper trading has been validated first.")
    print("   Press Enter to continue or Ctrl+C to cancel...")
    try:
        input()
    except KeyboardInterrupt:
        print("\n   Cancelled.")
        sys.exit(0)
else:
    # --paper OR no flag at all → always safe
    os.environ["PAPER_MODE"] = "true"
    if args.paper:
        print("📄 Mode: PAPER TRADING")
    else:
        print("📄 Mode: PAPER TRADING (default — pass --live for real money)")

if args.dry_run:
    os.environ["DRY_RUN"] = "true"
    print("🔍 Dry run: analysis + decisions only, no orders placed")

# ── Load config (after env overrides) ─────────────────────
import config
config.DRY_RUN = args.dry_run

config.validate()

# ── Live-mode pre-flight checks ────────────────────────────
if not config.PAPER_MODE and not config.DRY_RUN:
    from tools.alpaca_stock_tools import get_account_summary
    acct = get_account_summary()
    if acct.get("error"):
        print(f"❌ Cannot connect to live Alpaca account: {acct['error']}")
        sys.exit(1)

    equity       = acct["equity"]
    buying_power = acct["buying_power"]
    daytrades    = acct["daytrades"]

    print(f"\n📋 Live Account Check:")
    print(f"   Equity:        ${equity:,.2f}")
    print(f"   Buying power:  ${buying_power:,.2f}")
    print(f"   Day trades (5-day window): {daytrades}")

    # Block if buying power is critically low
    if buying_power < config.STOCK_BASE_ALLOCATION:
        print(f"\n❌ Insufficient buying power (${buying_power:,.0f}) — need at least ${config.STOCK_BASE_ALLOCATION:,.0f} for one position.")
        sys.exit(1)

    if buying_power < config.STOCK_MAX_PORTFOLIO * 0.5:
        print(f"\n⚠️  Buying power ${buying_power:,.0f} is less than half of STOCK_MAX_PORTFOLIO (${config.STOCK_MAX_PORTFOLIO:,.0f}).")
        print("   Consider lowering STOCK_MAX_PORTFOLIO in .env to match actual funds.")

    # PDT warning: < $25K equity + ≥ 3 day trades in rolling 5 days
    PDT_THRESHOLD = 25_000
    if equity < PDT_THRESHOLD:
        remaining_dt = max(0, 3 - daytrades)
        print(f"\n⚠️  PDT WARNING: Account equity ${equity:,.0f} < $25,000.")
        print(f"   You have {daytrades}/3 day trades used in the last 5 days.")
        if remaining_dt == 0:
            print("   ❌ No day trades remaining — all orders today will be REJECTED by Alpaca.")
            print("   Wait until the 5-day window rolls over, or deposit funds to reach $25K.")
            print("   Press Enter to exit, or Ctrl+C to proceed anyway (orders will fail)...")
            try:
                input()
                sys.exit(0)
            except KeyboardInterrupt:
                print("   ⚠️  Proceeding despite PDT limit — orders will likely be rejected.")
        else:
            print(f"   You can open/close {remaining_dt} more round-trip(s) today.")
            print(f"   STOCK_MAX_TRADES_PER_DAY is set to {config.STOCK_MAX_TRADES_PER_DAY} — consider lowering it to {remaining_dt} in .env.")

    print(f"\n   ✅ Live pre-flight passed.  Press Enter to start trading, Ctrl+C to cancel...")
    try:
        input()
    except KeyboardInterrupt:
        print("\n   Cancelled.")
        sys.exit(0)

print(f"\n{'='*60}")
print(
    f"{'📄 PAPER' if config.PAPER_MODE else '🔴 LIVE'} STOCK TRADER  |  "
    f"Max ${config.STOCK_MAX_PORTFOLIO:,.0f}  |  "
    f"Per position ${config.STOCK_BASE_ALLOCATION:,.0f}  |  "
    f"{'DRY RUN' if config.DRY_RUN else 'ORDERS ON'}"
)
print(f"   Ratcheting stop: {config.STOCK_INITIAL_STOP_PCT:+.0f}% → locks profit as gains grow")
print(f"   Force exit: {config.STOCK_FORCE_EXIT_TIME} PST  |  Close: {config.MARKET_CLOSE} PST")
if getattr(config, "NEWSAPI_KEY", ""):
    print("   📰 NewsAPI: enabled")
else:
    print("   📰 NewsAPI: not set (add NEWSAPI_KEY to .env for richer tech headlines)")
print(f"{'='*60}\n")

# ── Import loop last (avoids importing before env is set) ──
from agent.stock_loop import run

MAX_RESTARTS = 5
restart_count = 0

import time as _time

while restart_count < MAX_RESTARTS:
    try:
        run()
        break
    except KeyboardInterrupt:
        print("\n⛔ Stopped by user.")
        break
    except Exception as exc:
        restart_count += 1
        print(f"\n💥 Crash #{restart_count}: {exc}")
        import traceback
        traceback.print_exc()
        if restart_count < MAX_RESTARTS:
            print(f"   Restarting in 30s... (attempt {restart_count}/{MAX_RESTARTS})")
            _time.sleep(30)
        else:
            print("   Max restarts reached. Exiting.")
            sys.exit(1)
