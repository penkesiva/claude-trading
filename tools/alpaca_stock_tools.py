"""
tools/alpaca_stock_tools.py
Alpaca API interactions for stock day trading (no options).
"""

import math
import requests
from datetime import datetime
from typing import Optional

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

BASE     = config.ALPACA_BASE_URL
DATA_URL = "https://data.alpaca.markets"


def _headers() -> dict:
    return {
        "APCA-API-KEY-ID":     config.ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
        "Content-Type":        "application/json",
    }


# ──────────────────────────────────────────────────────────
# QUOTES & BARS
# ──────────────────────────────────────────────────────────

def get_stock_quote(ticker: str) -> dict:
    """Latest bid/ask/mid for a stock."""
    sym = ticker.upper().strip()
    try:
        r = requests.get(
            f"{DATA_URL}/v2/stocks/{sym}/quotes/latest",
            headers=_headers(),
            timeout=8,
        )
        r.raise_for_status()
        q   = r.json().get("quote", {})
        bid = float(q.get("bp", 0))
        ask = float(q.get("ap", 0))
        mid = round((bid + ask) / 2, 2) if bid and ask else 0.0
        return {"ticker": sym, "bid": bid, "ask": ask, "mid": mid}
    except Exception as e:
        return {"ticker": sym, "bid": 0, "ask": 0, "mid": 0, "error": str(e)}


def get_stock_bars(ticker: str, timeframe: str = "5Min", limit: int = 20) -> list:
    """Recent OHLCV bars for a stock anchored to today's regular session (9:30 ET).

    Passing `start` ensures we only get bars from today's regular session and
    never receive stale pre-market bars from a prior session.
    """
    import datetime, pytz

    sym = ticker.upper().strip()

    # Anchor to today's market open in ET so we never get yesterday's bars.
    et = pytz.timezone("America/New_York")
    now_et = datetime.datetime.now(et)
    market_open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    # If called before 9:30 ET (pre-market), still use today's date so we get
    # whatever has printed so far; Alpaca will just return an empty list or the
    # most recent bars, which is fine.
    start_iso = market_open_et.isoformat()  # includes correct UTC offset (EDT -04:00 / EST -05:00)

    for feed in ["sip", "iex"]:
        try:
            r = requests.get(
                f"{DATA_URL}/v2/stocks/{sym}/bars",
                headers=_headers(),
                params={
                    "timeframe": timeframe,
                    "limit": limit,
                    "feed": feed,
                    "start": start_iso,
                    "sort": "desc",          # newest first so [0] is the most recent bar
                },
                timeout=8,
            )
            r.raise_for_status()
            bars = r.json().get("bars") or []
            if bars:
                return [
                    {
                        "t": b["t"],
                        "o": float(b["o"]),
                        "h": float(b["h"]),
                        "l": float(b["l"]),
                        "c": float(b["c"]),
                        "v": int(b["v"]),
                    }
                    for b in bars
                ]
        except Exception:
            continue
    return []


# ──────────────────────────────────────────────────────────
# POSITIONS
# ──────────────────────────────────────────────────────────

def get_stock_positions() -> list:
    """
    Open stock positions on the account (skips option symbols).
    OCC option symbols are longer than 6 chars; simple tickers are ≤ 5 letters.
    """
    try:
        r = requests.get(f"{BASE}/v2/positions", headers=_headers(), timeout=8)
        if not r.ok:
            return []
        result = []
        for p in r.json():
            sym = p.get("symbol", "")
            # Skip options: OCC symbols contain digits
            if any(ch.isdigit() for ch in sym):
                continue
            qty = int(float(p.get("qty", 0)))
            if qty <= 0:
                continue
            entry  = float(p.get("avg_entry_price", 0))
            current = float(p.get("current_price", 0))
            pl      = float(p.get("unrealized_pl", 0))
            plpc    = float(p.get("unrealized_plpc", 0)) * 100
            result.append({
                "ticker":        sym,
                "qty":           qty,
                "entry_price":   entry,
                "current_price": current,
                "market_value":  float(p.get("market_value", 0)),
                "unrealized_pl": pl,
                "pnl_pct":       round(plpc, 2),
            })
        return result
    except Exception:
        return []


# ──────────────────────────────────────────────────────────
# ORDERS
# ──────────────────────────────────────────────────────────

def place_stock_order(
    ticker: str,
    qty: int,
    side: str,              # "buy" | "sell"
    order_type: str = "market",
    limit_price: Optional[float] = None,
) -> dict:
    """Place a stock order (whole shares, day time-in-force)."""
    if qty <= 0:
        return {"error": f"invalid qty {qty}"}
    payload = {
        "symbol":        ticker.upper().strip(),
        "qty":           str(qty),
        "side":          side.lower(),
        "type":          order_type,
        "time_in_force": "day",
    }
    if order_type == "limit" and limit_price:
        payload["limit_price"] = str(round(limit_price, 2))
    try:
        r = requests.post(f"{BASE}/v2/orders", headers=_headers(), json=payload, timeout=10)
        if not r.ok:
            return {"error": r.text, "status_code": r.status_code}
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def get_order_fill(order_id: str, timeout_secs: int = 15) -> Optional[float]:
    """
    Poll until the order is filled and return the average fill price.
    Returns None if the order doesn't fill within timeout_secs or on error.

    Market orders in liquid stocks typically fill in < 1 second during market hours.
    """
    import time as _t
    deadline = _t.time() + timeout_secs
    while _t.time() < deadline:
        try:
            r = requests.get(f"{BASE}/v2/orders/{order_id}", headers=_headers(), timeout=8)
            if not r.ok:
                return None
            o = r.json()
            status = o.get("status", "")
            filled_avg = o.get("filled_avg_price")
            if status == "filled" and filled_avg:
                return float(filled_avg)
            if status in ("canceled", "expired", "rejected"):
                return None
        except Exception:
            return None
        _t.sleep(0.5)
    return None


def close_stock_position(ticker: str) -> dict:
    """Close (market sell) the full position in a single stock."""
    sym = ticker.upper().strip()
    try:
        r = requests.delete(f"{BASE}/v2/positions/{sym}", headers=_headers(), timeout=10)
        if not r.ok:
            return {"error": r.text}
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def close_all_stock_positions() -> dict:
    """Cancel all open orders and close all stock positions immediately."""
    try:
        r = requests.delete(
            f"{BASE}/v2/positions",
            headers=_headers(),
            params={"cancel_orders": True},
            timeout=15,
        )
        if not r.ok:
            return {"error": r.text}
        return {"status": "ok", "body": r.json()}
    except Exception as e:
        return {"error": str(e)}


# ──────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────

def calc_share_count(price: float, allocation: float = 2_000.0) -> int:
    """Largest whole-share count buyable within allocation at price."""
    if price <= 0 or allocation <= 0:
        return 0
    return int(math.floor(allocation / price))


def get_account_summary() -> dict:
    """Return equity, buying power, cash."""
    try:
        r = requests.get(f"{BASE}/v2/account", headers=_headers(), timeout=8)
        r.raise_for_status()
        a = r.json()
        return {
            "equity":       float(a.get("equity", 0)),
            "buying_power": float(a.get("buying_power", 0)),
            "cash":         float(a.get("cash", 0)),
            "daytrades":    int(a.get("daytrade_count", 0)),
        }
    except Exception as e:
        return {"error": str(e)}
