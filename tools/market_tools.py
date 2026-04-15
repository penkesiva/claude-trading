"""
tools/market_tools.py
Technical signal calculations: RSI, VWAP, momentum, regime detection
"""

import math
from datetime import datetime
from typing import Optional
import pytz

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
from tools.alpaca_tools import get_spy_bars, get_spy_price, get_vix

# ──────────────────────────────────────────────────────────
# RSI
# ──────────────────────────────────────────────────────────
def calc_rsi(closes: list, period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)

# ──────────────────────────────────────────────────────────
# VWAP
# ──────────────────────────────────────────────────────────
def calc_vwap(bars: list) -> Optional[float]:
    if not bars:
        return None
    cum_pv = sum(((b["h"] + b["l"] + b["c"]) / 3) * b["v"] for b in bars)
    cum_v  = sum(b["v"] for b in bars)
    return round(cum_pv / cum_v, 2) if cum_v else None

# ──────────────────────────────────────────────────────────
# MOMENTUM
# ──────────────────────────────────────────────────────────
def calc_momentum(closes: list, period: int = 5) -> Optional[float]:
    """% change over last N bars"""
    if len(closes) < period + 1:
        return None
    return round(((closes[-1] - closes[-period]) / closes[-period]) * 100, 3)

def calc_premarket_move(bars: list) -> Optional[float]:
    """Estimate pre-market move % from first available bar vs prev close"""
    if len(bars) < 2:
        return None
    first_open = bars[0]["o"]
    prev_close = bars[0]["c"]  # approximation
    return round(((first_open - prev_close) / prev_close) * 100, 2)

# ──────────────────────────────────────────────────────────
# MARKET REGIME
# ──────────────────────────────────────────────────────────
def detect_regime(vix: float, spy_change_pct: float, rsi: float) -> dict:
    """
    Classify current market regime.
    Returns: regime name + suggested strategy
    """
    regime = "NEUTRAL"
    strategy_hint = "wait"

    if vix is None:
        return {"regime": "UNKNOWN", "strategy_hint": "skip - no VIX data"}

    # VIX zone-based regime detection
    if vix > 30:
        regime = "EXTREME_FEAR"
        strategy_hint = "hard skip — VIX > 30"
    elif vix > config.VIX_HIGH_THRESHOLD:  # 27-30
        regime = "HIGH_FEAR"
        strategy_hint = "skip or 0.90+ confidence only"
    elif vix > 22:  # 22-27 elevated zone
        regime = "ELEVATED_FEAR"
        strategy_hint = "proceed with 0.80+ confidence only"

    # Strong momentum up
    elif spy_change_pct >= 0.5 and rsi and 45 < rsi < 75:
        regime = "MOMENTUM_UP"
        strategy_hint = "0dte_momentum_call"

    # Strong momentum down
    elif spy_change_pct <= -0.5 and rsi and 25 < rsi < 55:
        regime = "MOMENTUM_DOWN"
        strategy_hint = "0dte_momentum_put"

    # Overbought - potential mean reversion
    elif rsi and rsi > 75:
        regime = "OVERBOUGHT"
        strategy_hint = "mean_reversion_put or skip"

    # Oversold - potential mean reversion
    elif rsi and rsi < 25:
        regime = "OVERSOLD"
        strategy_hint = "mean_reversion_call or skip"

    # Low vol trending day
    elif vix < 15 and abs(spy_change_pct) < 0.3:
        regime = "LOW_VOL_DRIFT"
        strategy_hint = "trend_continuation_1_5dte"

    else:
        regime = "NEUTRAL"
        strategy_hint = "wait_for_clearer_signal"

    return {
        "regime":         regime,
        "strategy_hint":  strategy_hint,
        "vix":            vix,
        "spy_change_pct": spy_change_pct,
        "rsi":            rsi,
    }

# ──────────────────────────────────────────────────────────
# FULL SIGNAL SNAPSHOT
# ──────────────────────────────────────────────────────────
def get_signal_snapshot() -> dict:
    """
    Master function: returns all signals Claude needs to make a decision.
    Called every 15-30 min by the agent loop.
    Falls back to daily bars if 5min bars unavailable (weekend/after-hours).
    """
    pst = pytz.timezone(config.TIMEZONE)
    now_pst = datetime.now(pst).strftime("%H:%M PST")

    # Price data
    spy_quote = get_spy_price()
    vix_data  = get_vix()

    # Try 5min bars first, fall back to 1Day bars
    bars = get_spy_bars(timeframe="5Min", limit=30)
    bar_source = "5min"
    if not bars:
        bars = get_spy_bars(timeframe="1Day", limit=30)
        bar_source = "daily"

    closes = [b["c"] for b in bars] if bars else []
    spy_mid = spy_quote.get("mid", 0)

    # Technicals
    rsi    = calc_rsi(closes) if closes else None
    vwap   = calc_vwap(bars) if bars else None
    mom_5  = calc_momentum(closes, 5) if closes else None
    mom_15 = calc_momentum(closes, 15) if closes else None

    # SPY change from open
    spy_change_pct = 0.0
    if bars and len(bars) > 1:
        day_open = bars[0]["o"]
        spy_change_pct = round(((spy_mid - day_open) / day_open) * 100, 3) if day_open else 0

    vix_level = vix_data.get("vix_proxy")

    # VWAP distance
    vwap_dist = round(((spy_mid - vwap) / vwap) * 100, 3) if vwap and spy_mid else None

    # Regime detection
    regime = detect_regime(vix_level, spy_change_pct, rsi)

    return {
        "time_pst":        now_pst,
        "spy": {
            "price":        spy_mid,
            "bid":          spy_quote.get("bid"),
            "ask":          spy_quote.get("ask"),
            "change_pct":   spy_change_pct,
        },
        "technicals": {
            "rsi_14":       rsi,
            "vwap":         vwap,
            "vwap_dist_pct": vwap_dist,
            "momentum_5bar": mom_5,
            "momentum_15bar": mom_15,
        },
        "vix":             vix_level,
        "regime":          regime,
        "bars_available":  len(bars),
        "bar_source":      bar_source if bars else "none",
    }