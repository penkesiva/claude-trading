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
if args.paper:
    os.environ["PAPER_MODE"] = "true"
    print("📄 Mode: PAPER TRADING")
elif args.live:
    os.environ["PAPER_MODE"] = "false"
    print("🔴 Mode: LIVE TRADING")
    print("\n⚠️  WARNING: This will trade with REAL MONEY.")
    print("   Press Enter to continue or Ctrl+C to cancel...")
    try:
        input()
    except KeyboardInterrupt:
        print("\n   Cancelled.")
        sys.exit(0)

if args.dry_run:
    os.environ["DRY_RUN"] = "true"
    print("🔍 Dry run: analysis + decisions only, no orders placed")

# ── Load config (after env overrides) ─────────────────────
import config
config.DRY_RUN = args.dry_run

config.validate()

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
