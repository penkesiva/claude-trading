"""
tests/test_agent.py
Phase 3: Test Claude agent reasoning — NO real orders placed
Run: python tests/test_agent.py
"""

import sys, os, json, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

print("\n" + "="*60)
print("🧪 PHASE 3: CLAUDE AGENT TEST (reasoning only, no orders)")
print("="*60)

import config
config.validate()

from agent.decision import run_morning_scan, get_entry_decision

# ── 1. Morning scan ────────────────────────────────────────
print("\n[1/2] Running morning news scan (uses web search)...")
print("      This will take 15-30 seconds...\n")
thesis = {"bias": "neutral", "thesis": "test fallback", "confidence": 0.5}
try:
    thesis = run_morning_scan()
    print(f"\n      ✅ Morning thesis generated!")
    print(f"      Bias:       {thesis.get('bias', 'unknown').upper()}")
    print(f"      Thesis:     {thesis.get('thesis', 'N/A')[:120]}")
    print(f"      Confidence: {thesis.get('confidence', 0):.0%}")
    if thesis.get("avoid_windows"):
        print(f"      Avoid:      {thesis.get('avoid_windows')}")
    if thesis.get("recommended_strategy"):
        print(f"      Strategy:   {thesis.get('recommended_strategy')}")
except Exception as e:
    print(f"      ❌ FAILED: {e}")
    print(f"      Full traceback:")
    traceback.print_exc()
    print(f"\n      → Using fallback thesis, continuing to Phase 2...\n")

# ── 2. Entry decision ──────────────────────────────────────
print("\n[2/2] Testing entry decision (reasoning only, no order placed)...")
print("      Note: Each run costs ~$0.02-0.08 in API credits\n")
try:
    decision = get_entry_decision(thesis, daily_pnl=0, trades_today=0)
    print(f"\n      ✅ Decision received!")
    print(f"      Action:     {decision.get('action')}")
    print(f"      Strategy:   {decision.get('strategy', 'N/A')}")
    print(f"      Direction:  {decision.get('direction', 'N/A')}")
    print(f"      Strike:     {decision.get('strike', 'N/A')}")
    print(f"      Expiry:     {decision.get('expiry', 'N/A')}")
    print(f"      Confidence: {decision.get('confidence', 0):.0%}")
    print(f"      Risk:       {decision.get('risk_assessment', 'N/A')}")
    rationale = decision.get('entry_rationale') or decision.get('exit_rationale') or 'N/A'
    print(f"      Rationale:  {rationale[:150]}")
    if decision.get("warnings"):
        print(f"      Warnings:")
        for w in decision["warnings"]:
            print(f"        ⚠️  {w[:100]}")
except Exception as e:
    print(f"      ❌ FAILED: {e}")
    traceback.print_exc()

print("\n" + "="*60)
print("✅ Phase 3 Complete!")
print()
print("   💡 TIP: Only run this test once to validate.")
print("      Each run costs ~$0.05-0.10 in API credits.")
print("      Monday 6:15 AM PST → run: python main.py")
print("="*60 + "\n")
