"""
favorites.py
Reads favorites.txt to get trading configuration.
Controls: which ticker, what DTE, max premium per trade.
"""

import os
from datetime import date, timedelta
from typing import Optional

FAVORITES_FILE = os.path.join(os.path.dirname(__file__), "favorites.txt")

def load_favorites() -> dict:
    """
    Parse favorites.txt and return active trading config.
    Returns first non-comment, non-empty line.

    Format: TICKER  DTE  MAX_PREMIUM
    Example: SPY  0DTE  100
    """
    if not os.path.exists(FAVORITES_FILE):
        print(f"⚠️  favorites.txt not found — using defaults (SPY 0DTE $100)")
        return _default()

    with open(FAVORITES_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            parts = line.split()
            if len(parts) < 3:
                continue

            ticker  = parts[0].upper()
            dte_str = parts[1].upper()   # 0DTE, 1DTE, 2DTE etc
            try:
                max_prem = float(parts[2])
            except ValueError:
                continue

            # Parse DTE
            dte_days = _parse_dte(dte_str)
            expiry   = _calc_expiry(dte_days)

            config = {
                "ticker":      ticker,
                "dte_str":     dte_str,
                "dte_days":    dte_days,
                "expiry":      expiry,
                "max_premium": max_prem,
                "raw_line":    line,
            }
            print(f"📋 Favorites loaded: {ticker} | {dte_str} (expires {expiry}) | max ${max_prem:.0f}/contract")
            return config

    print(f"⚠️  No valid entries in favorites.txt — using defaults")
    return _default()

def _parse_dte(dte_str: str) -> int:
    """Parse '0DTE' → 0, '1DTE' → 1, '2DTE' → 2 etc"""
    try:
        return int(dte_str.replace("DTE", "").strip())
    except:
        return 0  # default to 0DTE

def _calc_expiry(dte_days: int) -> str:
    """Calculate expiry date string from DTE days"""
    target = date.today()
    days_added = 0
    while days_added < dte_days:
        target += timedelta(days=1)
        # Skip weekends
        if target.weekday() < 5:
            days_added += 1
    return target.strftime("%Y-%m-%d")

def _default() -> dict:
    return {
        "ticker":      "SPY",
        "dte_str":     "0DTE",
        "dte_days":    0,
        "expiry":      date.today().strftime("%Y-%m-%d"),
        "max_premium": 100.0,
        "raw_line":    "SPY 0DTE 100 (default)",
    }
