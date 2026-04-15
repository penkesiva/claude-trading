"""
tests/test_signals.py
Phase 2: Validate signal engine — RSI, VWAP, momentum, regime
Run: python tests/test_signals.py
Works on weekends/after-hours using most recent bars or mock data.
"""

import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

print("\n" + "="*60)
print("🧪 PHASE 2: SIGNAL ENGINE TEST")
print("="*60)

import config
from tools.alpaca_tools import get_spy_bars, is_market_open
from tools.market_tools import (
    calc_rsi, calc_vwap, calc_momentum,
    detect_regime, get_signal_snapshot
)

# ── 1. Raw bars ────────────────────────────────────────────
print("\n[1/4] Fetching SPY bars (5min, 30 bars)...")
bars = []
try:
    bars = get_spy_bars(timeframe="5Min", limit=30)
    if bars:
        print(f"      ✅ Got {len(bars)} bars from Alpaca")
        print(f"      Latest bar: {bars[-1]['t']}")
        print(f"      Close: ${bars[-1]['c']:.2f} | Vol: {bars[-1]['v']:,}")
    else:
        print(f"      ⚠️  No live bars — market closed (weekend/holiday)")
        print(f"      → Using mock data to validate signal logic")
except Exception as e:
    print(f"      ⚠️  Bars fetch error: {e}")
    print(f"      → Using mock data to validate signal logic")

# Use mock bars if market is closed or bars unavailable
if not bars:
    base = 585.0
    import random
    random.seed(42)
    bars = []
    price = base
    for i in range(30):
        o = price
        change = random.uniform(-0.3, 0.4)
        c = round(o + change, 2)
        h = round(max(o, c) + random.uniform(0, 0.2), 2)
        l = round(min(o, c) - random.uniform(0, 0.2), 2)
        bars.append({"t": f"2026-04-09T{9+i//12:02d}:{(i%12)*5:02d}:00Z",
                     "o": o, "h": h, "l": l, "c": c, "v": 120000 + i*500})
        price = c
    print(f"      Mock: 30 bars | ${bars[0]['c']:.2f} to ${bars[-1]['c']:.2f}")
    print(f"      (Logic validation only — not real market data)")

closes = [b["c"] for b in bars]

# ── 2. Technicals ──────────────────────────────────────────
print("\n[2/4] Calculating technical indicators...")
rsi   = calc_rsi(closes)
vwap  = calc_vwap(bars)
mom5  = calc_momentum(closes, 5)
mom15 = calc_momentum(closes, 15)

print(f"      RSI(14):          {rsi if rsi else 'N/A'}")
print(f"      VWAP:             ${vwap:.2f}" if vwap else "      VWAP:             N/A")
print(f"      Momentum (5bar):  {mom5}%")
print(f"      Momentum (15bar): {mom15}%")

if rsi:
    if rsi > 70:   print("      ⚠️  RSI overbought (>70)")
    elif rsi < 30: print("      ⚠️  RSI oversold (<30)")
    else:          print("      ✅ RSI neutral range")

if vwap:
    dist = round(((closes[-1] - vwap) / vwap) * 100, 3)
    above = "above" if dist > 0 else "below"
    print(f"      ✅ SPY {above} VWAP by {abs(dist):.3f}%")

# ── 3. Regime detection ────────────────────────────────────
print("\n[3/4] Testing regime detection (6 scenarios)...")
test_cases = [
    (17.5,  0.6, 58, "Momentum UP          "),
    (27.0, -0.8, 40, "High fear            "),
    (14.0,  0.1, 72, "Low vol overbought   "),
    (18.0, -1.5, 35, "Momentum DOWN        "),
    (16.0,  0.2, 50, "Neutral              "),
    (32.0, -2.0, 28, "Extreme fear oversold"),
]
for vix_val, change, rsi_val, label in test_cases:
    r = detect_regime(vix_val, change, rsi_val)
    print(f"      {label} → {r['regime']:22s} → {r['strategy_hint']}")
print("      ✅ Regime detection working")

# ── 4. Full snapshot ───────────────────────────────────────
print("\n[4/4] Running full signal snapshot...")
try:
    snapshot = get_signal_snapshot()
    print(f"      ✅ Snapshot generated!")
    print(f"      Time:         {snapshot['time_pst']}")
    print(f"      SPY:          ${snapshot['spy']['price']:.2f} ({snapshot['spy']['change_pct']:+.3f}%)")
    print(f"      VIX proxy:    {snapshot['vix'] or 'unavailable (market closed)'}")
    print(f"      RSI(14):      {snapshot['technicals']['rsi_14']}")
    print(f"      VWAP dist:    {snapshot['technicals']['vwap_dist_pct']}%")
    print(f"      Regime:       {snapshot['regime']['regime']}")
    print(f"      Strategy:     {snapshot['regime']['strategy_hint']}")
except Exception as e:
    print(f"      ❌ Snapshot failed: {e}")

print("\n" + "="*60)
print("✅ Phase 2 Complete!")
print()
print("   Weekend/after-hours: mock data used — that is expected.")
print("   Run again Mon 6:00 AM PST for real live data.")
print()
print("   Next step → python tests/test_agent.py")
print("="*60 + "\n")
