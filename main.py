"""
main.py — Entry point

Usage:
  python main.py                  → uses .env PAPER_MODE setting
  python main.py --paper          → force paper trading
  python main.py --live           → force live trading (careful!)
  python main.py --paper --dry-run → scan only, never place orders

Examples:
  python main.py --paper          ← safe, use this first
  python main.py --live           ← real money, be sure!
"""

import sys
import time as _time
import argparse

# ── Parse command line arguments ──────────────────────────
parser = argparse.ArgumentParser(
    description="SPY Options AI Trader",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog="""
Examples:
  python main.py --paper          (paper trading, safe)
  python main.py --live           (real money!)
  python main.py --paper --dry-run (decisions only, no orders)
    """
)

mode_group = parser.add_mutually_exclusive_group()
mode_group.add_argument(
    "--paper",
    action="store_true",
    help="Force paper trading mode (safe)"
)
mode_group.add_argument(
    "--live",
    action="store_true",
    help="Force live trading mode (real money!)"
)
parser.add_argument(
    "--dry-run",
    action="store_true",
    help="Scan and decide but never place real orders"
)

args = parser.parse_args()

# ── Override PAPER_MODE in env before loading config ──────
import os
if args.paper:
    os.environ["PAPER_MODE"] = "true"
    print("📄 Mode override: PAPER TRADING (--paper flag)")
elif args.live:
    os.environ["PAPER_MODE"] = "false"
    print("🔴 Mode override: LIVE TRADING (--live flag)")
    # Extra confirmation for live
    print("\n⚠️  WARNING: You are about to trade with REAL MONEY")
    print("   Press Enter to continue or Ctrl+C to cancel...")
    try:
        input()
    except KeyboardInterrupt:
        print("\n   Cancelled.")
        sys.exit(0)

if args.dry_run:
    os.environ["DRY_RUN"] = "true"
    print("🔍 Dry run mode: decisions only, no orders placed")

# ── Load config AFTER env overrides ───────────────────────
import config
from favorites import load_favorites

# Load and display favorites
favs = load_favorites()

# Override MAX_PREMIUM from favorites
config.MAX_PREMIUM_PER_TRADE = favs["max_premium"]

# Store favorites in config for use by other modules
config.ACTIVE_TICKER  = favs["ticker"]
config.ACTIVE_DTE     = favs["dte_days"]
config.ACTIVE_EXPIRY  = favs["expiry"]
config.DRY_RUN        = args.dry_run

from agent.loop import run

# ── Main with auto-reconnect ───────────────────────────────
MAX_RESTARTS = 5
restart_count = 0

print(f"\n{'='*60}")
print(f"{'📄 PAPER' if config.PAPER_MODE else '🔴 LIVE'} | "
      f"Ticker: {config.ACTIVE_TICKER} | "
      f"DTE: {favs['dte_str']} | "
      f"Max: ${config.MAX_PREMIUM_PER_TRADE:.0f} | "
      f"{'DRY RUN' if config.DRY_RUN else 'ORDERS ON'}")
print(f"{'='*60}\n")

while restart_count <= MAX_RESTARTS:
    try:
        run()
        break

    except KeyboardInterrupt:
        print("\n\n⛔ Manually stopped.")
        print("   Closing any open SPY options positions...")
        from tools.alpaca_tools import get_positions, close_position
        positions = get_positions(spy_only=True)  # SPY options ONLY
        if positions:
            for pos in positions:
                print(f"   Closing {pos['symbol']}...")
                close_position(pos['symbol'])
            print("   ✅ SPY options closed")
        else:
            print("   No open SPY options — other holdings untouched ✅")
        sys.exit(0)

    except Exception as e:
        err_str = str(e)

        network_errors = [
            "Connection reset", "ConnectionReset", "Connection aborted",
            "timeout", "Timeout", "peer", "network", "ConnectionError",
            "RemoteDisconnected", "BrokenPipe", "EOF occurred"
        ]
        is_network = any(x in err_str for x in network_errors)

        if is_network and restart_count < MAX_RESTARTS:
            restart_count += 1
            wait = 30
            print(f"\n⚠️  Network error (restart {restart_count}/{MAX_RESTARTS})")
            print(f"   {err_str[:100]}")
            print(f"   Reconnecting in {wait}s...")
            from tools.logger import log_error
            log_error("reconnect", err_str)
            _time.sleep(wait)
            continue

        print(f"\n❌ Fatal error: {err_str[:150]}")
        from tools.logger import log_error
        log_error("main", err_str)

        # SAFE: only close SPY options, never stocks
        print("   Closing open SPY options only (not stocks)...")
        try:
            from tools.alpaca_tools import get_positions, close_position
            positions = get_positions(spy_only=True)
            for pos in positions:
                close_position(pos['symbol'])
                print(f"   Closed {pos['symbol']}")
        except:
            pass
        sys.exit(1)

if restart_count > MAX_RESTARTS:
    print(f"\n❌ Max restarts ({MAX_RESTARTS}) reached — giving up")
    sys.exit(1)
