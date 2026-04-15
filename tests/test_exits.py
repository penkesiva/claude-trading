"""
tests/test_exits.py
Test the ratchet trailing stop and exit management system.
No API calls, no Alpaca, costs $0.
Run: python tests/test_exits.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

print("\n" + "="*60)
print("🧪 EXIT SYSTEM TEST (ratchet + stop loss + time guards)")
print("="*60)

from agent.exits import check_exit, init_position, clear_position, calcFloor, risk_manager

passed = 0
failed = 0

def test(name: str, result: dict, expect_exit: bool, expect_fragment: str = ""):
    global passed, failed
    ok = result["exit"] == expect_exit
    if expect_fragment:
        ok = ok and expect_fragment.lower() in result["reason"].lower()
    status = "✅" if ok else "❌"
    print(f"  {status} {name}")
    if not ok:
        print(f"       Expected exit={expect_exit} fragment='{expect_fragment}'")
        print(f"       Got:    exit={result['exit']} reason='{result['reason']}'")
        failed += 1
    else:
        passed += 1

# ── 1. Hard stop loss ──────────────────────────────────────
print("\n[1/6] Hard stop loss (-25%)")
init_position("TEST1", 1.00)
test("Hold at -10%",  check_exit("TEST1", -10), False)
test("Hold at -24%",  check_exit("TEST1", -24), False)
test("Exit at -25%",  check_exit("TEST1", -25), True, "stop loss")
test("Exit at -30%",  check_exit("TEST1", -30), True, "stop loss")
clear_position("TEST1")

# ── 2. Ratchet floor activation ────────────────────────────
print("\n[2/6] Ratchet floor — breakeven at +15% peak")
init_position("TEST2", 1.00)
test("Hold at +10%",           check_exit("TEST2", 10),  False)
test("Hold at +14%",           check_exit("TEST2", 14),  False)
# Peak hits +15%, floor moves to 0%
check_exit("TEST2", 15)  # push peak to 15
test("Hold at +5% (floor 0%)", check_exit("TEST2", 5),   False)
test("Exit at 0% (floor hit)", check_exit("TEST2", 0),   True, "ratchet floor")
test("Exit at -1% (below)",    check_exit("TEST2", -1),  True, "ratchet floor")
clear_position("TEST2")

# ── 3. Ratchet escalation ─────────────────────────────────
print("\n[3/6] Ratchet escalation — floor trails peak by 15%")
scenarios = [
    (25,  10),   # peak +25 → floor +10
    (35,  20),   # peak +35 → floor +20
    (50,  35),   # peak +50 → floor +35
    (75,  60),   # peak +75 → floor +60
    (100, 85),   # peak +100 → floor +85
    (150, 135),  # peak +150 → floor +135
    (200, 185),  # peak +200 → floor +185
]
all_ok = True
for peak, expected_floor in scenarios:
    floor = calcFloor(peak)  
    ok = floor == expected_floor
    status = "✅" if ok else "❌"
    print(f"  {status} Peak +{peak}% → floor +{expected_floor}% (got {floor}%)")
    if not ok:
        failed += 1
        all_ok = False
    else:
        passed += 1

# ── 4. Full ratchet ride ───────────────────────────────────
print("\n[4/6] Full ratchet ride — should exit at floor not peak")
init_position("TEST4", 1.00)
# Simulate a strong momentum run
path = [0, 5, 10, 15, 20, 30, 40, 55, 70, 80, 90, 100, 90, 80, 75, 70, 65]
exit_found = False
for pnl in path:
    r = check_exit("TEST4", pnl)
    if r["exit"]:
        # Should exit at 85 floor (peak 100, floor 85)
        test(f"Exit at ratchet floor when peak=100%",
             r, True, "ratchet")
        print(f"       Exited at: {pnl}% | {r['reason']}")
        exit_found = True
        break
if not exit_found:
    print("  ❌ Never exited — ratchet not triggering")
    failed += 1
clear_position("TEST4")

# ── 5. Ratchet beats fixed 40% target ─────────────────────
print("\n[5/6] Ratchet vs fixed 40% — ratchet should capture more")
test_cases = [
    # (description, price_path, min_expected_exit_pct)
    ("Strong run to +80%, reverses",
     [0,10,20,30,40,50,60,70,80,75,70,65,60,55,50,45,40,35,30,25,20,15,10,5,0,-5,-10],
     60),   # should exit around +65% (floor at +65 when peak=80)
    ("Moderate run to +50%, reverses",
     [0,10,20,30,40,50,45,40,35,30,25,20,15,10,5,0],
     30),   # should exit at +35 floor
    ("Weak run to +30%, reverses",
     [0,5,10,15,20,25,30,20,10,0,-5,-10],
     0),    # should exit at breakeven (peak 30 > 25, floor = 15)
]

for desc, path, min_exit in test_cases:
    init_position("TESTX", 1.00)
    exit_pnl = None
    for pnl in path:
        r = check_exit("TESTX", pnl)
        if r["exit"]:
            exit_pnl = pnl
            break
    clear_position("TESTX")

    if exit_pnl is None:
        exit_pnl = path[-1]

    ok = exit_pnl >= min_exit
    status = "✅" if ok else "❌"
    fixed = 40 if max(path) >= 40 else max(path)
    extra = exit_pnl - fixed
    print(f"  {status} {desc}")
    print(f"       Ratchet exit: +{exit_pnl}% | Fixed would be: +{fixed}% | Extra: {'+' if extra>=0 else ''}{extra}%")
    if not ok:
        print(f"       ❌ Expected exit >= {min_exit}%, got {exit_pnl}%")
        failed += 1
    else:
        passed += 1

# ── 6. Daily risk manager ──────────────────────────────────
print("\n[6/6] Daily risk manager")
from agent.exits import DailyRiskManager
rm = DailyRiskManager()

can, _ = rm.can_trade()
test("Can trade at start", {"exit": can, "reason": ""}, True)

rm.record_trade(50)
rm.record_trade(-30)
can, _ = rm.can_trade()
test("Can trade after 2 trades", {"exit": can, "reason": ""}, True)

rm.record_trade(20)
can, reason = rm.can_trade()
test("Blocked after 3 trades", {"exit": not can, "reason": reason}, True, "max 3")

rm2 = DailyRiskManager()
rm2.record_trade(-250)
can, reason = rm2.can_trade()
test("Halted after $250 loss", {"exit": not can, "reason": reason}, True, "daily loss")

# ── Summary ────────────────────────────────────────────────
total = passed + failed
print(f"\n{'='*60}")
print(f"Results: {passed}/{total} passed")
if failed == 0:
    print("✅ All exit system tests passed!")
    print()
    print("   Ratchet system is working correctly.")
    print("   Ready to run: python main.py")
else:
    print(f"❌ {failed} test(s) failed — review output above")
print("="*60 + "\n")
