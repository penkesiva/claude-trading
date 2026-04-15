"""
agent/exits.py
Professional exit management with ratcheting trailing stop.
Designed to capture maximum upside on strong momentum moves
while protecting capital on reversals.
"""

import time
from datetime import datetime
from typing import Optional
import pytz

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
from tools.logger import log_trade, log_error

# ──────────────────────────────────────────────────────────
# POSITION STATE
# ──────────────────────────────────────────────────────────
class PositionState:
    def __init__(self, symbol: str, entry_price: float):
        self.symbol       = symbol
        self.entry_price  = entry_price
        self.peak_pnl_pct = 0.0       # highest % ever seen
        self.ratchet_floor = None      # current locked floor %
        self.entry_time   = datetime.now(pytz.timezone(config.TIMEZONE))
        self.last_pnl_pct = 0.0
        self.phase        = "early"    # early → momentum → ratchet → late

    def minutes_held(self) -> float:
        pst = pytz.timezone(config.TIMEZONE)
        return (datetime.now(pst) - self.entry_time).total_seconds() / 60

_position_states: dict = {}

def init_position(symbol: str, entry_price: float):
    _position_states[symbol] = PositionState(symbol, entry_price)
    print(f"   📊 Exit manager: {symbol} @ ${entry_price:.2f}")
    print(f"   📊 Ratchet system: ACTIVE | Stop: -25% | First lock: +15%→BE")

def clear_position(symbol: str):
    if symbol in _position_states:
        del _position_states[symbol]

# ──────────────────────────────────────────────────────────
# RATCHET CALCULATOR
# ──────────────────────────────────────────────────────────
def calcFloor(peak: float) -> Optional[float]:
    """
    Given a peak gain %, return the ratchet floor %.
    Floor always trails 15% below peak once above +15%.

    Peak +15%  → floor 0%   (breakeven)
    Peak +25%  → floor +10%
    Peak +35%  → floor +20%
    Peak +50%  → floor +35%
    Peak +75%  → floor +60%
    Peak +100% → floor +85%
    Peak +150% → floor +135%
    Peak +200% → floor +185%
    """
    if peak < 15:
        return None          # no floor yet, original stop applies
    if peak < 25:
        return 0.0           # breakeven
    return round(peak - 15, 1)   # trail 15% below peak

# ──────────────────────────────────────────────────────────
# MASTER EXIT CHECK
# ──────────────────────────────────────────────────────────
def check_exit(symbol: str, current_pnl_pct: float) -> dict:
    """
    Call every 30-60 seconds. Returns exit decision.
    
    SYSTEM OVERVIEW:
    ─────────────────────────────────────────────────
    HARD STOPS (Python, instant)
    • Stop loss:     -25% (or ratchet floor if higher)
    • Time exits:    12:45 PM PST force close
    • Daily loss:    checked in loop.py

    RATCHET TRAILING STOP (never give back gains)
    • No floor below +15% peak → original -25% stop
    • Peak +15% → floor 0% (breakeven protected)
    • Peak +25% → floor +10%
    • Peak +50% → floor +35%
    • Peak +100% → floor +85%
    • Ratchet: every new high moves floor up with it

    THETA DECAY GUARDS (0DTE only)
    • After 9 AM PST:  exit if gain < +5% and held 30+ min
    • After 11 AM PST: exit any gain > 0%

    MOMENTUM COLLAPSE
    • If retraced 20%+ from peak in < 10 min → exit fast
    ─────────────────────────────────────────────────
    """
    pst     = pytz.timezone(config.TIMEZONE)
    now_pst = datetime.now(pst).strftime("%H:%M")

    if symbol not in _position_states:
        _position_states[symbol] = PositionState(symbol, 0)
    state = _position_states[symbol]

    # Update peak
    prev_peak = state.peak_pnl_pct
    if current_pnl_pct > state.peak_pnl_pct:
        state.peak_pnl_pct = current_pnl_pct

    # Update ratchet floor
    new_floor = calcFloor(state.peak_pnl_pct)
    if new_floor is not None:
        if state.ratchet_floor is None or new_floor > state.ratchet_floor:
            if new_floor != state.ratchet_floor:
                _announce_ratchet(state.ratchet_floor, new_floor, state.peak_pnl_pct)
            state.ratchet_floor = new_floor

    state.last_pnl_pct = current_pnl_pct
    mins = state.minutes_held()

    # ── HARD: Time exits ───────────────────────────────────
    if now_pst >= "12:45":
        return _exit(f"⏰ 12:45 PST hard cutoff | P&L: {current_pnl_pct:+.1f}%", "immediate")
    if now_pst >= "11:30":
        return _exit(f"⏰ 11:30 PST no-hold rule | P&L: {current_pnl_pct:+.1f}%", "immediate")

    # ── HARD: Ratchet floor or stop loss ──────────────────
    effective_stop = state.ratchet_floor if state.ratchet_floor is not None else -config.STOP_LOSS_PCT

    if current_pnl_pct <= effective_stop:
        if state.ratchet_floor is not None:
            return _exit(
                f"🔒 Ratchet floor hit: {current_pnl_pct:+.1f}% ≤ floor {state.ratchet_floor:+.1f}% "
                f"(peak was {state.peak_pnl_pct:+.1f}%)", "immediate"
            )
        else:
            return _exit(f"🛑 Stop loss: {current_pnl_pct:+.1f}% ≤ -{config.STOP_LOSS_PCT}%", "immediate")

    # ── MOMENTUM COLLAPSE detector ─────────────────────────
    # If we peaked above +20% and just crashed 20pts fast
    if state.peak_pnl_pct >= 20:
        retrace = state.peak_pnl_pct - current_pnl_pct
        if retrace >= 20 and mins < 10:
            return _exit(
                f"📉 Fast reversal: {retrace:.1f}% retrace in {mins:.1f}min "
                f"(peak {state.peak_pnl_pct:+.1f}% → now {current_pnl_pct:+.1f}%)", "immediate"
            )

    # ── THETA DECAY guards ─────────────────────────────────
    # Friday = major expiry day, decay is most aggressive
    from datetime import date
    is_friday = date.today().weekday() == 4

    if is_friday:
        # Friday: exit any gain after 8 AM PST (11 AM ET)
        if now_pst >= "08:00" and current_pnl_pct > 0:
            return _exit(f"⏳ Friday theta exit: {current_pnl_pct:+.1f}% after 8AM PST (expiry day)", "normal")
        # Friday: exit stale positions after 7:30 AM PST
        if now_pst >= "07:30" and current_pnl_pct < 5 and mins > 20:
            return _exit(
                f"⏳ Friday theta guard: only {current_pnl_pct:+.1f}% after {mins:.0f}min — "
                f"expiry decay severe", "normal"
            )
    else:
        # Normal days
        if now_pst >= "11:00" and current_pnl_pct > 0:
            return _exit(f"⏳ Theta exit: {current_pnl_pct:+.1f}% after 11AM PST", "normal")
        if now_pst >= "09:00" and current_pnl_pct < 5 and mins > 30:
            return _exit(
                f"⏳ Theta guard: only {current_pnl_pct:+.1f}% after {mins:.0f}min — "
                f"decay accelerating post 9AM", "normal"
            )

    # ── HOLD — show ratchet status ─────────────────────────
    floor_str = f"+{state.ratchet_floor:.1f}%" if state.ratchet_floor is not None else f"-{config.STOP_LOSS_PCT}%"
    return {
        "exit":    False,
        "reason":  (f"Holding | Now: {current_pnl_pct:+.1f}% | "
                    f"Peak: {state.peak_pnl_pct:+.1f}% | "
                    f"Floor: {floor_str} | "
                    f"{mins:.0f}min held"),
        "urgency": None,
    }

def _exit(reason: str, urgency: str) -> dict:
    return {"exit": True, "reason": reason, "urgency": urgency}

def _announce_ratchet(old_floor, new_floor, peak):
    """Print ratchet level change"""
    if old_floor is None:
        print(f"   🔒 Ratchet ACTIVATED: floor set to {new_floor:+.1f}% (peak: {peak:+.1f}%)")
    else:
        print(f"   🔒 Ratchet UP: floor {old_floor:+.1f}% → {new_floor:+.1f}% (peak: {peak:+.1f}%)")

# ──────────────────────────────────────────────────────────
# RATCHET SIMULATOR (for testing/visualization)
# ──────────────────────────────────────────────────────────
def simulate_ratchet(price_path: list) -> dict:
    """
    Simulate ratchet on a list of P&L % values.
    Returns exit point and final P&L.
    Usage: simulate_ratchet([0, 5, 12, 20, 35, 50, 45, 38, 30])
    """
    state = PositionState("SIM", 1.0)
    for i, pnl in enumerate(price_path):
        result = check_exit("SIM", pnl)
        if result["exit"]:
            clear_position("SIM")
            return {"exit_at_bar": i, "exit_pnl_pct": pnl, "reason": result["reason"],
                    "peak": state.peak_pnl_pct}
    clear_position("SIM")
    return {"exit_at_bar": len(price_path)-1, "exit_pnl_pct": price_path[-1],
            "reason": "end of path", "peak": state.peak_pnl_pct}

# ──────────────────────────────────────────────────────────
# DAILY RISK MANAGER
# ──────────────────────────────────────────────────────────
class DailyRiskManager:
    def __init__(self):
        self.daily_pnl    = 0.0
        self.trades_today = 0
        self.wins         = 0
        self.losses       = 0
        self.halted       = False
        self.halt_reason  = None

    def record_trade(self, pnl: float):
        self.daily_pnl    += pnl
        self.trades_today += 1
        if pnl > 0: self.wins   += 1
        else:        self.losses += 1
        if self.daily_pnl <= -config.MAX_DAILY_LOSS_USD:
            self.halted      = True
            self.halt_reason = f"Daily loss limit: ${self.daily_pnl:.2f}"
            print(f"🛑 {self.halt_reason}")

    def can_trade(self) -> tuple:
        if self.halted:
            return False, self.halt_reason or "halted"
        if self.trades_today >= config.MAX_TRADES_PER_DAY:
            return False, f"Max {config.MAX_TRADES_PER_DAY} trades/day reached ({self.trades_today})"
        return True, "ok"

    def summary(self) -> str:
        wr = (self.wins / self.trades_today * 100) if self.trades_today else 0
        return (f"P&L: ${self.daily_pnl:+.2f} | "
                f"Trades: {self.trades_today} | "
                f"W/L: {self.wins}/{self.losses} | "
                f"WR: {wr:.0f}%")

risk_manager = DailyRiskManager()
