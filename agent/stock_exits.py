"""
agent/stock_exits.py
Ratcheting trailing-stop logic and daily risk manager for stock day trading.

How ratcheting works:
  Entry:          stop = entry - 5%   (full initial risk)
  Peak gain ≥ 3%: stop → entry - 2%  (cut loss in half)
  Peak gain ≥ 5%: stop → entry + 1%  (locked in some profit)
  Peak gain ≥ 8%: stop → entry + 4%
  Peak gain ≥ 12%: stop → entry + 8%
  Peak gain ≥ 20%: stop → entry + 15%

Stops only ever move UP — they never retreat on a pullback.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

# (min peak gain %, new stop % relative to entry)
_RATCHET_LEVELS = [
    (20.0, 15.0),
    (12.0,  8.0),
    ( 8.0,  4.0),
    ( 5.0,  1.0),
    ( 3.0, -2.0),
]


class RatchetTracker:
    """Tracks ratcheting trailing stop for one open stock position."""

    def __init__(self, ticker: str, entry_price: float, shares: int):
        self.ticker       = ticker.upper().strip()
        self.entry_price  = float(entry_price)
        self.shares       = int(shares)
        self.peak_price   = float(entry_price)
        initial_stop_pct  = float(getattr(config, "STOCK_INITIAL_STOP_PCT", -5.0))
        self.stop_price   = round(entry_price * (1 + initial_stop_pct / 100), 4)
        self.level_desc   = f"initial ({initial_stop_pct:+.0f}%)"

    # ── Public API ─────────────────────────────────────────

    def update(self, current_price: float) -> dict:
        """
        Call on every price scan.
        Returns a dict — check 'exit' key to know if stop was hit.
        """
        current_price = float(current_price)

        if current_price > self.peak_price:
            self.peak_price = current_price
            self._ratchet()

        pnl_dollar = (current_price - self.entry_price) * self.shares
        pnl_pct    = (current_price - self.entry_price) / self.entry_price * 100
        from_peak  = (current_price - self.peak_price) / self.peak_price * 100

        if current_price <= self.stop_price:
            return {
                "exit":       True,
                "reason":     (
                    f"stop hit [{self.level_desc}]: "
                    f"${current_price:.2f} ≤ stop ${self.stop_price:.2f}"
                ),
                "pnl_pct":    round(pnl_pct, 2),
                "pnl_dollar": round(pnl_dollar, 2),
            }

        return {
            "exit":       False,
            "pnl_pct":    round(pnl_pct, 2),
            "pnl_dollar": round(pnl_dollar, 2),
            "current":    current_price,
            "peak":       round(self.peak_price, 2),
            "stop":       round(self.stop_price, 2),
            "from_peak":  round(from_peak, 2),
            "level":      self.level_desc,
        }

    def summary(self) -> str:
        return (
            f"{self.ticker} | {self.shares}sh | "
            f"entry ${self.entry_price:.2f} | "
            f"peak ${self.peak_price:.2f} | "
            f"stop ${self.stop_price:.2f} [{self.level_desc}]"
        )

    # ── Internal ───────────────────────────────────────────

    def _ratchet(self):
        """Tighten the stop based on the new peak gain."""
        peak_gain = (self.peak_price - self.entry_price) / self.entry_price * 100
        for min_gain, stop_pct in _RATCHET_LEVELS:
            if peak_gain >= min_gain:
                new_stop = round(self.entry_price * (1 + stop_pct / 100), 4)
                if new_stop > self.stop_price:
                    old = self.stop_price
                    self.stop_price = new_stop
                    self.level_desc = (
                        f"peak +{peak_gain:.1f}% → stop {stop_pct:+.0f}%"
                    )
                    print(
                        f"   🔼 {self.ticker} ratchet: stop ${old:.2f} → ${new_stop:.2f} "
                        f"[{self.level_desc}]"
                    )
                break


# ──────────────────────────────────────────────────────────

class StockRiskManager:
    """Daily P&L tracker and trade gate for the stock bot."""

    def __init__(self):
        self.daily_pnl    = 0.0
        self.trades_today = 0
        self.halted       = False
        self.halt_reason  = ""

    def record_trade(self, pnl: float):
        self.daily_pnl    += pnl
        self.trades_today += 1
        max_loss = float(getattr(config, "STOCK_MAX_DAILY_LOSS_USD", 500))
        if self.daily_pnl <= -max_loss and not self.halted:
            self.halted      = True
            self.halt_reason = (
                f"daily loss ${abs(self.daily_pnl):.0f} ≥ max ${max_loss:.0f}"
            )
            print(f"🛑 HALT triggered: {self.halt_reason}")

    def can_trade(self, open_positions: int = 0) -> tuple:
        """Returns (bool, reason_str)."""
        if self.halted:
            return False, f"halted: {self.halt_reason}"
        max_trades = int(getattr(config, "STOCK_MAX_TRADES_PER_DAY", 10))
        if self.trades_today >= max_trades:
            return False, f"max trades/day ({max_trades}) reached"
        max_pos = int(getattr(config, "STOCK_MAX_POSITIONS", 5))
        if open_positions >= max_pos:
            return False, f"max open positions ({max_pos}) reached"
        return True, "ok"

    def summary(self) -> str:
        status = "🛑 HALTED" if self.halted else "✅"
        return (
            f"P&L ${self.daily_pnl:+.2f} | "
            f"Trades {self.trades_today} | {status}"
        )
