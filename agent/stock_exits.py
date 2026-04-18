"""
agent/stock_exits.py
Continuous trailing-stop logic and daily risk manager for stock day trading.

How the trailing stop works:
  Entry:      stop = entry × (1 − STOCK_INITIAL_STOP_PCT)   e.g. entry − 5%
  Every tick: stop = max(current_stop, peak × (1 − STOCK_TRAIL_PCT))

  The stop chases the running peak price continuously — no discrete levels,
  no cliffs.  Once the trail stop exceeds the initial stop it takes over and
  the position is always protected STOCK_TRAIL_PCT% below whatever the
  all-time high of the trade was.

  Example (trail = 3%, initial stop = −5%):
    Entry $100  → stop $95.00  (initial)
    Peak  $102  → stop $98.94  (trail kicks in: $102 × 0.97)
    Peak  $104.90 → stop $101.75  (profit locked, no cliff!)
    Price falls to $101.75 → EXIT at +1.75%

  Configurable via .env:
    STOCK_INITIAL_STOP_PCT=-5   (starting floor, default −5%)
    STOCK_TRAIL_PCT=3           (trail distance below peak, default 3%)

Stops only ever move UP — they never retreat on a pullback.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config


class RatchetTracker:
    """Continuous trailing stop for one open stock position."""

    def __init__(self, ticker: str, entry_price: float, shares: int,
                 source: str = "morning scan", entry_time: str = ""):
        import datetime as _dt, pytz as _pytz, config as _cfg
        self.ticker          = ticker.upper().strip()
        self.entry_price     = float(entry_price)
        self.shares          = int(shares)
        self.peak_price      = float(entry_price)
        initial_stop_pct     = float(getattr(config, "STOCK_INITIAL_STOP_PCT", -5.0))
        global_trail         = float(getattr(config, "STOCK_TRAIL_PCT", 2.5))

        # Use per-ticker suggested trail from learning profiles if available
        # (requires ≥ 3 past trades with that ticker — avoids reacting to noise)
        try:
            from tools.logger import load_ticker_profiles
            profile  = load_ticker_profiles().get(self.ticker, {})
            learned  = profile.get("suggested_trail_pct")
            n_trades = profile.get("trades", 0)
            if learned and n_trades >= 3:
                self.trail_pct   = float(learned)
                self._trail_src  = f"learned ({n_trades} trades)"
            else:
                self.trail_pct   = global_trail
                self._trail_src  = "global config"
        except Exception:
            self.trail_pct  = global_trail
            self._trail_src = "global config"

        self.stop_price      = round(entry_price * (1 + initial_stop_pct / 100), 4)
        self.level_desc      = f"initial ({initial_stop_pct:+.0f}%)"
        self.source          = source
        self.entry_time      = entry_time or _dt.datetime.now(
            _pytz.timezone(_cfg.TIMEZONE)
        ).strftime("%H:%M")

    # ── Public API ─────────────────────────────────────────

    def update(self, current_price: float) -> dict:
        """
        Call on every price scan.
        Returns a dict — check 'exit' key to know if stop was hit.
        """
        current_price = float(current_price)

        # Advance the peak and continuously trail it
        if current_price > self.peak_price:
            self.peak_price = current_price

        trail_stop = round(self.peak_price * (1 - self.trail_pct / 100), 4)
        if trail_stop > self.stop_price:
            old = self.stop_price
            self.stop_price = trail_stop
            peak_gain_pct = (self.peak_price - self.entry_price) / self.entry_price * 100
            self.level_desc = f"trail −{self.trail_pct:.0f}% (peak +{peak_gain_pct:.1f}%)"
            # Print only when stop advances by a meaningful amount (avoids tick-by-tick spam)
            if trail_stop - old >= 0.05:
                print(
                    f"   🔼 {self.ticker} stop ${old:.2f} → ${trail_stop:.2f} "
                    f"[{self.level_desc}]"
                )

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
            f"stop ${self.stop_price:.2f} [{self.level_desc}] | "
            f"trail {self.trail_pct:.1f}% [{self._trail_src}]"
        )


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
