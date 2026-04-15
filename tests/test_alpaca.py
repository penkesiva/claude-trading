"""
tests/test_alpaca.py
Phase 1: Validate Alpaca connection, SPY price, options chain
Run: python tests/test_alpaca.py
"""

import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

print("\n" + "="*60)
print("🧪 PHASE 1: ALPACA CONNECTION TEST")
print("="*60)

# ── 1. Config ──────────────────────────────────────────────
print("\n[1/6] Loading config...")
try:
    import config
    config.validate()
    mode = "📄 PAPER" if config.PAPER_MODE else "🔴 LIVE"
    print(f"      Mode: {mode}")
    print(f"      URL:  {config.ALPACA_BASE_URL}")
except Exception as e:
    print(f"❌ Config failed: {e}")
    sys.exit(1)

from tools.alpaca_tools import (
    get_account, get_spy_price, get_spy_bars,
    get_vix, get_spy_options_chain, get_market_clock, is_market_open
)

# ── 2. Account ─────────────────────────────────────────────
print("\n[2/6] Testing account connection...")
try:
    account = get_account()
    print(f"      ✅ Connected!")
    print(f"      Equity:       ${account['equity']:,.2f}")
    print(f"      Buying Power: ${account['buying_power']:,.2f}")
    print(f"      Daytrades:    {account['daytrade_count']}")
except Exception as e:
    print(f"      ❌ FAILED: {e}")
    print("         → Check ALPACA keys in .env")
    sys.exit(1)

# ── 3. Market clock ────────────────────────────────────────
print("\n[3/6] Checking market clock...")
try:
    clock = get_market_clock()
    open_status = "🟢 OPEN" if clock["is_open"] else "🔴 CLOSED"
    print(f"      {open_status}")
    print(f"      Next open:  {clock['next_open']}")
    print(f"      Next close: {clock['next_close']}")
except Exception as e:
    print(f"      ❌ FAILED: {e}")

# ── 4. SPY price ───────────────────────────────────────────
print("\n[4/6] Fetching SPY price...")
try:
    spy = get_spy_price()
    print(f"      ✅ SPY Quote:")
    print(f"      Bid: ${spy['bid']:.2f} | Ask: ${spy['ask']:.2f} | Mid: ${spy['mid']:.2f}")
except Exception as e:
    print(f"      ❌ FAILED: {e}")

# ── 5. VIX ─────────────────────────────────────────────────
print("\n[5/6] Fetching VIX proxy (VIXY)...")
try:
    vix = get_vix()
    if vix.get("vix_proxy"):
        print(f"      ✅ VIX Proxy: {vix['vix_proxy']} ({vix['source']})")
    else:
        print(f"      ⚠️  VIX unavailable: {vix.get('error', 'unknown')}")
        print("         → This is OK if market is closed")
except Exception as e:
    print(f"      ⚠️  VIX failed: {e}")

# ── 6. Options chain ───────────────────────────────────────
print("\n[6/6] Fetching SPY options chain (0DTE)...")
try:
    chain = get_spy_options_chain(strike_range=5)
    if chain.get("error"):
        print(f"      ⚠️  {chain['error']}")
        print("         → This is expected if market is closed or 0DTE not yet listed")
    else:
        print(f"      ✅ Chain fetched!")
        print(f"      SPY Price:   ${chain['spy_price']:.2f}")
        print(f"      Contracts:   {chain['total_found']} near ATM")
        print(f"      Expiry:      {chain['expiry']}")
        print(f"\n      Sample contracts:")
        for c in chain["contracts"][:6]:
            print(f"        {c['type'].upper():4s} ${c['strike']:.0f} "
                  f"| DTE: {c['dte']} "
                  f"| OI: {c.get('open_interest', 'N/A')}")
except Exception as e:
    print(f"      ❌ FAILED: {e}")
    print("         → Check if options trading is enabled on your Alpaca account")

# ── Summary ────────────────────────────────────────────────
print("\n" + "="*60)
print("✅ Phase 1 Complete!")
print("   If all checks passed → run: python tests/test_signals.py")
print("   If options chain failed → enable options in Alpaca dashboard")
print("="*60 + "\n")
