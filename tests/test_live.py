"""
tests/test_live.py
Live API readiness test — connects to REAL Alpaca account.
Reads live data only. Places ZERO orders.
Run: python tests/test_live.py
"""

import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

print("\n" + "="*60)
print("🔴 LIVE API READINESS TEST")
print("   Reads real data. Places ZERO orders.")
print("="*60)

import config

# ── Confirm live mode ──────────────────────────────────────
print(f"\n[0/7] Checking mode...")
mode = "🔴 LIVE" if not config.PAPER_MODE else "📄 PAPER"
print(f"      Mode: {mode}")
print(f"      URL:  {config.ALPACA_BASE_URL}")
print(f"      Max loss:    ${config.MAX_DAILY_LOSS_USD}")
print(f"      Max premium: ${config.MAX_PREMIUM_PER_TRADE}")
print(f"      Max trades:  {config.MAX_TRADES_PER_DAY}/day")
print(f"      VIX limit:   {config.VIX_HIGH_THRESHOLD}")

if config.PAPER_MODE:
    print("\n  ⚠️  Still in PAPER mode!")
    print("     Set PAPER_MODE=false in .env to test live connection")
    print("     Continuing anyway to test connection logic...\n")

config.validate()

from tools.alpaca_tools import (
    get_account, get_spy_price, get_spy_bars,
    get_vix, get_spy_options_chain, get_market_clock,
    get_positions, is_market_open
)
from tools.market_tools import get_signal_snapshot

passed = 0
failed = 0
warnings = 0

def ok(msg):
    global passed
    passed += 1
    print(f"      ✅ {msg}")

def fail(msg):
    global failed
    failed += 1
    print(f"      ❌ {msg}")

def warn(msg):
    global warnings
    warnings += 1
    print(f"      ⚠️  {msg}")

# ── 1. Account ─────────────────────────────────────────────
print("\n[1/7] Live account connection...")
try:
    account = get_account()
    ok(f"Connected! Equity: ${account['equity']:,.2f}")
    ok(f"Buying power: ${account['buying_power']:,.2f}")

    if account['equity'] < 500:
        warn(f"Low equity ${account['equity']:.2f} — options may require more")
    else:
        ok(f"Equity sufficient for options trading")

    if account['daytrade_count'] >= 3:
        warn(f"Daytrade count: {account['daytrade_count']} — approaching PDT limit (3)")
    else:
        ok(f"Daytrade count: {account['daytrade_count']}/3 — safe")

    if account['pdt_check']:
        warn("Account flagged as Pattern Day Trader")
    else:
        ok("No PDT flag")

except Exception as e:
    fail(f"Account connection failed: {e}")
    print("\n  ❌ Cannot continue — fix Alpaca live keys in .env")
    sys.exit(1)

# ── 2. Market clock ────────────────────────────────────────
print("\n[2/7] Market status...")
try:
    clock = get_market_clock()
    status = "🟢 OPEN" if clock["is_open"] else "🔴 CLOSED"
    ok(f"Market: {status}")
    ok(f"Next open:  {clock['next_open']}")
    ok(f"Next close: {clock['next_close']}")
    if not clock["is_open"]:
        warn("Market closed — options chain will show yesterday's data")
except Exception as e:
    fail(f"Clock failed: {e}")

# ── 3. SPY live price ──────────────────────────────────────
print("\n[3/7] SPY live price...")
try:
    spy = get_spy_price()
    if spy["mid"] > 0:
        ok(f"SPY: ${spy['mid']:.2f} (bid: ${spy['bid']:.2f} / ask: ${spy['ask']:.2f})")
        spread = spy['ask'] - spy['bid']
        ok(f"Bid-ask spread: ${spread:.2f}")
    else:
        warn("SPY price returned 0 — market may be closed")
except Exception as e:
    fail(f"SPY price failed: {e}")

# ── 4. VIX check ───────────────────────────────────────────
print("\n[4/7] VIX level (VIXY proxy)...")
try:
    vix = get_vix()
    vix_val = vix.get("vix_proxy")
    if vix_val:
        ok(f"VIX proxy: {vix_val} (source: {vix['source']})")
        if vix_val > 30:
            warn(f"🚨 VIX {vix_val} > 30 — SKIP trading today, too dangerous")
        elif vix_val > config.VIX_HIGH_THRESHOLD:
            warn(f"VIX {vix_val} > {config.VIX_HIGH_THRESHOLD} threshold — Claude will be cautious")
        else:
            ok(f"VIX {vix_val} within acceptable range (<{config.VIX_HIGH_THRESHOLD})")
    else:
        warn("VIX unavailable — market closed or data issue")
except Exception as e:
    warn(f"VIX check failed (non-critical): {e}")

# ── 5. Options chain ───────────────────────────────────────
print("\n[5/7] SPY options chain...")
try:
    chain = get_spy_options_chain(strike_range=5)
    if chain.get("error"):
        warn(f"Options chain: {chain['error']}")
        warn("Enable options trading in Alpaca dashboard if not already done")
    else:
        contracts = chain.get("contracts", [])
        calls = [c for c in contracts if c.get("type") == "call"]
        puts  = [c for c in contracts if c.get("type") == "put"]
        ok(f"Chain fetched: {len(contracts)} contracts near ATM")
        ok(f"Calls: {len(calls)} | Puts: {len(puts)}")
        ok(f"SPY price: ${chain['spy_price']:.2f}")
        ok(f"Expiry: {chain['expiry']}")

        if contracts:
            sample = contracts[0]
            ok(f"Sample: {sample.get('type','?').upper()} ${sample.get('strike')} "
               f"DTE:{sample.get('dte')} OI:{sample.get('open_interest','?')}")

        if len(contracts) == 0:
            warn("No contracts found — options may not be enabled on live account")

except Exception as e:
    fail(f"Options chain failed: {e}")
    warn("Go to alpaca.markets → live account → enable options trading")

# ── 6. Positions check ─────────────────────────────────────
print("\n[6/7] Open positions...")
try:
    positions = get_positions(spy_only=False)  # show all for pre-flight check
    if positions:
        warn(f"You have {len(positions)} open position(s)!")
        for p in positions:
            print(f"         {p['symbol']} | qty: {p['qty']} | P&L: ${p['unrealized_pl']:+.2f} ({p['pnl_pct']:+.1f}%)")
        warn("Close open positions before running main.py tomorrow")
    else:
        ok("No open positions — clean slate for tomorrow")
except Exception as e:
    fail(f"Positions check failed: {e}")

# ── 7. Full signal snapshot ────────────────────────────────
print("\n[7/7] Full signal snapshot...")
try:
    snapshot = get_signal_snapshot()
    ok(f"Snapshot generated at {snapshot['time_pst']}")
    ok(f"SPY: ${snapshot['spy']['price']:.2f} ({snapshot['spy']['change_pct']:+.3f}%)")
    ok(f"VIX: {snapshot['vix'] or 'unavailable'}")
    ok(f"RSI: {snapshot['technicals']['rsi_14'] or 'unavailable (closed)'}")
    ok(f"Regime: {snapshot['regime']['regime']}")
    ok(f"Strategy hint: {snapshot['regime']['strategy_hint']}")
except Exception as e:
    fail(f"Signal snapshot failed: {e}")

# ── Summary ────────────────────────────────────────────────
total = passed + failed
print(f"\n{'='*60}")
print(f"Results: {passed} passed | {failed} failed | {warnings} warnings")

if failed > 0:
    print(f"\n❌ {failed} critical issue(s) — fix before going live")
elif warnings > 0:
    print(f"\n⚠️  {warnings} warning(s) — review above before trading")
    print("   Warnings are non-critical but worth checking")
else:
    print("\n✅ All live API checks passed!")

print()
if failed == 0:
    print("   Pre-flight checklist for tomorrow:")
    print("   □ VIX < 25 at 6:15 AM PST → proceed")
    print("   □ SPY pre-market direction clear")
    print("   □ No open positions")
    print("   □ Terminal open, venv active")
    print("   □ python main.py at 6:15 AM PST")
    print()
    print("   🚀 You are READY for live trading tomorrow!")
print("="*60 + "\n")
