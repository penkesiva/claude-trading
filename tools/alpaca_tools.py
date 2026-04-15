"""
tools/alpaca_tools.py
All Alpaca API interactions: options chain, orders, positions, account
"""

import json
import requests
from datetime import datetime, date, timedelta
from typing import Optional
import pytz

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

# ── Alpaca REST headers ────────────────────────────────────
def _headers():
    return {
        "APCA-API-KEY-ID":     config.ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
        "Content-Type":        "application/json",
    }

BASE     = config.ALPACA_BASE_URL
DATA_URL = "https://data.alpaca.markets"

# ──────────────────────────────────────────────────────────
# ACCOUNT
# ──────────────────────────────────────────────────────────
def get_account() -> dict:
    """Return account equity, buying power, daily P&L"""
    r = requests.get(f"{BASE}/v2/account", headers=_headers())
    r.raise_for_status()
    a = r.json()
    return {
        "equity":        float(a.get("equity", 0)),
        "buying_power":  float(a.get("buying_power", 0)),
        "cash":          float(a.get("cash", 0)),
        "daytrade_count": int(a.get("daytrade_count", 0)),
        "pdt_check":     a.get("pattern_day_trader", False),
    }

# ──────────────────────────────────────────────────────────
# EQUITY QUOTES
# ──────────────────────────────────────────────────────────
def get_equity_mid(ticker: str) -> dict:
    """Latest quote for any US equity symbol: bid, ask, mid."""
    sym = ticker.upper().strip()
    r = requests.get(
        f"{DATA_URL}/v2/stocks/{sym}/quotes/latest",
        headers=_headers()
    )
    r.raise_for_status()
    q = r.json().get("quote", {})
    bid = float(q.get("bp", 0))
    ask = float(q.get("ap", 0))
    mid = round((bid + ask) / 2, 2) if bid and ask else 0
    return {"bid": bid, "ask": ask, "mid": mid}


def get_spy_price() -> dict:
    """Latest SPY quote: price, bid, ask, change %"""
    return get_equity_mid("SPY")

def get_spy_bars(timeframe: str = "5Min", limit: int = 20) -> list:
    """Recent SPY OHLCV bars for RSI/VWAP calculation.
    Works on weekends/after-hours by fetching most recent historical bars.
    """
    # Try sip feed first (works for paper + live), fall back to iex
    for feed in ["sip", "iex"]:
        try:
            r = requests.get(
                f"{DATA_URL}/v2/stocks/SPY/bars",
                headers=_headers(),
                params={"timeframe": timeframe, "limit": limit, "feed": feed}
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
    return []  # return empty list, never None

# ──────────────────────────────────────────────────────────
# VIX
# ──────────────────────────────────────────────────────────
def get_vix() -> dict:
    """VIX level via VIXY ETF as proxy (or direct if available)"""
    try:
        r = requests.get(
            f"{DATA_URL}/v2/stocks/VIXY/quotes/latest",
            headers=_headers()
        )
        r.raise_for_status()
        q = r.json().get("quote", {})
        bid = float(q.get("bp", 0))
        ask = float(q.get("ap", 0))
        mid = round((bid + ask) / 2, 2)
        return {"vix_proxy": mid, "source": "VIXY"}
    except Exception as e:
        return {"vix_proxy": None, "source": "unavailable", "error": str(e)}

# ──────────────────────────────────────────────────────────
# OPTIONS CHAIN
# ──────────────────────────────────────────────────────────
def get_spy_options_chain(
    expiry_date: Optional[str] = None,
    option_type: Optional[str] = None,  # "call" | "put" | None = both
    strike_range: int = 10,             # strikes above/below ATM
    underlying: str = "SPY",
) -> dict:
    """
    Fetch options chain for an underlying (default SPY).
    Returns structured dict with calls + puts near ATM.
    expiry_date: "YYYY-MM-DD" or None (defaults to 0DTE = today)
    """
    root = underlying.upper().strip()
    if expiry_date is None:
        expiry_date = date.today().strftime("%Y-%m-%d")

    params = {
        "underlying_symbols": root,
        "expiration_date":    expiry_date,
        "limit":              200,
    }
    if option_type:
        params["type"] = option_type

    r = requests.get(
        f"{BASE}/v2/options/contracts",
        headers=_headers(),
        params=params
    )
    r.raise_for_status()
    contracts = r.json().get("option_contracts", [])

    if not contracts:
        return {"error": f"No contracts found for {root} {expiry_date}", "contracts": []}

    u = get_equity_mid(root)
    spot_mid = float(u.get("mid") or 0)
    strikes_sorted = sorted(
        {float(c.get("strike_price", 0)) for c in contracts if float(c.get("strike_price", 0) or 0) > 0}
    )
    if not spot_mid and strikes_sorted:
        spot_mid = strikes_sorted[len(strikes_sorted) // 2]
    if not spot_mid and root == "SPY":
        spot_mid = 590.0

    # Filter to ±strike_range strikes around ATM
    filtered = []
    for c in contracts:
        strike = float(c.get("strike_price", 0))
        if spot_mid and abs(strike - spot_mid) <= strike_range:
            filtered.append({
                "symbol":      c.get("symbol"),
                "type":        c.get("type"),          # call/put
                "strike":      strike,
                "expiry":      c.get("expiration_date"),
                "dte":         _calc_dte(c.get("expiration_date")),
                "open_interest": c.get("open_interest"),
                "close_price": c.get("close_price"),
            })

    # Sort by strike
    filtered.sort(key=lambda x: x["strike"])

    return {
        "spy_price":    spot_mid,
        "expiry":       expiry_date,
        "underlying":   root,
        "total_found":  len(filtered),
        "contracts":    filtered,
        "fetched_at":   datetime.now(pytz.timezone(config.TIMEZONE)).isoformat(),
    }

def get_options_snapshot(symbols: list) -> dict:
    """
    Get live bid/ask/IV/greeks for specific option symbols.
    Uses v1beta1 endpoint (correct for options data).
    Falls back gracefully if market closed or data unavailable.
    """
    if not symbols:
        return {}

    # Correct endpoint: v1beta1, not v2
    OPTIONS_DATA_URL = "https://data.alpaca.markets/v1beta1/options"

    params = {"symbols": ",".join(symbols[:10])}  # max 10
    try:
        r = requests.get(
            f"{OPTIONS_DATA_URL}/snapshots",
            headers=_headers(),
            params=params
        )
        if not r.ok:
            return {}
        snapshots = r.json().get("snapshots", {})
        result = {}
        for sym, s in snapshots.items():
            greeks = s.get("greeks", {})
            quote  = s.get("latestQuote", {})
            result[sym] = {
                "bid":   float(quote.get("bp", 0)),
                "ask":   float(quote.get("ap", 0)),
                "mid":   round((float(quote.get("bp", 0)) + float(quote.get("ap", 0))) / 2, 2),
                "iv":    round(float(s.get("impliedVolatility", 0)) * 100, 1),
                "delta": round(float(greeks.get("delta", 0)), 3),
                "gamma": round(float(greeks.get("gamma", 0)), 4),
                "theta": round(float(greeks.get("theta", 0)), 3),
                "vega":  round(float(greeks.get("vega", 0)), 3),
            }
        return result
    except Exception:
        return {}

def get_options_chain_live(
    underlying: str = "SPY",
    strike_range: int = 8,
    expiration_date: Optional[str] = None,
) -> dict:
    """
    Get full live options chain with greeks using v1beta1 chain endpoint.

    IMPORTANT: Alpaca does NOT provide greeks for 0DTE options (known limitation).
    For 0DTE we get bid/ask/price only.
    For 1DTE+ we get full greeks (delta, theta, IV).

    If expiration_date is set (YYYY-MM-DD), only that expiry is fetched (favorites mode).
    Otherwise tries today then tomorrow for backward compatibility.
    """
    OPTIONS_DATA_URL = "https://data.alpaca.markets/v1beta1/options"
    root = underlying.upper().strip()
    try:
        q = get_equity_mid(root)
        spot_mid = float(q.get("mid") or 0)
    except Exception:
        return {}
    if not spot_mid:
        if root == "SPY":
            spot_mid = 590.0
        else:
            return {}

    strike_low  = round(spot_mid - strike_range, 0)
    strike_high = round(spot_mid + strike_range, 0)
    today    = date.today().strftime("%Y-%m-%d")
    tomorrow = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    expiries = [expiration_date] if expiration_date else [today, tomorrow]

    for exp_date in expiries:
        try:
            r = requests.get(
                f"{OPTIONS_DATA_URL}/snapshots/{root}",
                headers=_headers(),
                params={
                    "feed":              "indicative",
                    "strike_price_gte":  strike_low,
                    "strike_price_lte":  strike_high,
                    "expiration_date":   exp_date,
                    "limit":             20,
                }
            )
            if not r.ok:
                continue

            data = r.json()
            # v1beta1 can return either "snapshots" dict or array
            raw = data.get("snapshots", {})
            contracts = []
            for sym, s in raw.items():
                greeks = s.get("greeks") or {}
                # Try multiple quote field names (Alpaca changes these)
                quote  = s.get("latestQuote") or s.get("quote") or {}
                # Try multiple detail field names
                detail = s.get("details") or s.get("detail") or {}

                bid = float(quote.get("bp") or quote.get("bidPrice") or 0)
                ask = float(quote.get("ap") or quote.get("askPrice") or 0)

                # Strike: try multiple field names
                strike_raw = (detail.get("strikePrice") or
                             detail.get("strike_price") or
                             detail.get("strike") or
                             s.get("strikePrice") or 0)
                strike = float(strike_raw) if strike_raw else 0

                # Type: call/put
                opt_type = (detail.get("type") or
                           detail.get("optionType") or
                           s.get("type") or "")

                # Contract expiry (avoid shadowing exp_date loop variable)
                c_exp = (detail.get("expirationDate") or
                         detail.get("expiration_date") or
                         detail.get("expiry") or
                         s.get("expirationDate") or "")

                # IV: try multiple fields
                iv_raw = (s.get("impliedVolatility") or
                         s.get("iv") or
                         greeks.get("impliedVolatility") or 0)
                iv = round(float(iv_raw) * 100, 1) if iv_raw else 0

                # Only include if we have valid strike data
                if strike > 0:
                    contracts.append({
                        "symbol":  sym,
                        "type":    opt_type.lower(),
                        "strike":  strike,
                        "expiry":  c_exp,
                        "dte":     _calc_dte(c_exp),
                        "bid":     bid,
                        "ask":     ask,
                        "mid":     round((bid + ask) / 2, 2),
                        "iv":      iv,
                        "delta":   round(float(greeks.get("delta") or 0), 3),
                        "theta":   round(float(greeks.get("theta") or 0), 3),
                        "volume":  (s.get("dailyBar") or {}).get("v", 0),
                    })

            contracts.sort(key=lambda x: (x["strike"], x["type"]))

            # If live chain returned no valid data, log for debugging
            if not contracts and raw:
                # Return first raw entry keys for debugging
                first_key = next(iter(raw))
                first_val = raw[first_key]
                print(f"   ⚠️  Chain debug — keys in response: {list(first_val.keys())}")
                if first_val.get("details"):
                    print(f"   ⚠️  details keys: {list(first_val['details'].keys())}")

            return {
                "spy_price":    spot_mid,
                "underlying":   root,
                "expiry":       exp_date,
                "total_found":  len(contracts),
                "contracts":    contracts,
            }
        except Exception as e:
            print(f"   ⚠️  get_options_chain_live error: {e}")
            return {}

    return {}


def build_occ_option_symbol(
    underlying: str,
    expiry_yyyy_mm_dd: str,
    direction: str,
    strike: float,
) -> str:
    """Compact OCC-style symbol (US equity options)."""
    root = underlying.upper().strip()
    d = expiry_yyyy_mm_dd.replace("-", "")
    if len(d) != 8:
        raise ValueError(f"invalid expiry {expiry_yyyy_mm_dd!r}")
    yymmdd = d[2:]
    cp = "C" if direction.lower() == "call" else "P"
    strike_int = int(round(float(strike) * 1000))
    return f"{root}{yymmdd}{cp}{strike_int:08d}"


def resolve_option_occ_symbol(
    underlying: str,
    expiry: str,
    direction: str,
    strike: float,
    strike_window: float = 30.0,
) -> Optional[str]:
    """
    Resolve broker option symbol from legs using contracts API.
    Returns None if no unique match.
    """
    root = underlying.upper().strip()
    want = "call" if direction.lower() == "call" else "put"
    try:
        chain = get_spy_options_chain(
            expiry_date=expiry,
            strike_range=int(strike_window) + 5,
            underlying=root,
        )
    except Exception:
        return None
    if chain.get("error"):
        return None
    k = float(strike)
    matches = []
    for c in chain.get("contracts", []):
        if (c.get("type") or "").lower() != want:
            continue
        if abs(float(c.get("strike", 0)) - k) > 0.02:
            continue
        ex = c.get("expiry") or ""
        if ex and ex != expiry:
            continue
        sym = c.get("symbol")
        if sym:
            matches.append(sym)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return matches[0]
    return None

# ──────────────────────────────────────────────────────────
# ORDERS
# ──────────────────────────────────────────────────────────
def place_options_order(
    symbol:     str,    # option contract symbol e.g. "SPY250410C00590000"
    qty:        int,    # number of contracts
    side:       str,    # "buy" | "sell"
    order_type: str = "market",
    limit_price: Optional[float] = None,
    time_in_force: str = "day",
) -> dict:
    """
    Place an options order.
    Enforces MAX_CONTRACTS and MAX_PREMIUM guardrails.
    """
    # ── Hard guardrails ──
    if qty > config.MAX_CONTRACTS:
        return {"error": f"qty {qty} exceeds MAX_CONTRACTS {config.MAX_CONTRACTS}"}

    payload = {
        "symbol":        symbol,
        "qty":           str(qty),
        "side":          side,
        "type":          order_type,
        "time_in_force": time_in_force,
    }
    if order_type == "limit" and limit_price:
        payload["limit_price"] = str(round(limit_price, 2))

    r = requests.post(
        f"{BASE}/v2/orders",
        headers=_headers(),
        json=payload
    )
    if not r.ok:
        return {"error": r.text, "status_code": r.status_code}

    o = r.json()
    return {
        "order_id":   o.get("id"),
        "symbol":     o.get("symbol"),
        "side":       o.get("side"),
        "qty":        o.get("qty"),
        "type":       o.get("type"),
        "status":     o.get("status"),
        "submitted":  o.get("submitted_at"),
    }

def cancel_order(order_id: str) -> dict:
    r = requests.delete(f"{BASE}/v2/orders/{order_id}", headers=_headers())
    return {"cancelled": r.ok, "status_code": r.status_code}

def cancel_all_orders() -> dict:
    r = requests.delete(f"{BASE}/v2/orders", headers=_headers())
    return {"cancelled_all": r.ok}

# ──────────────────────────────────────────────────────────
# POSITIONS
# ──────────────────────────────────────────────────────────
def get_positions(spy_only: bool = True) -> list:
    """
    Get open positions.
    spy_only=True (default): only returns SPY options opened by this script
    spy_only=False: returns all positions (used by test_live.py)
    """
    r = requests.get(f"{BASE}/v2/positions", headers=_headers())
    r.raise_for_status()
    positions = r.json()
    result = []
    for p in positions:
        symbol = p.get("symbol", "")
        # Filter to SPY options only (format: SPY + date + C/P + strike)
        if spy_only and not (symbol.startswith("SPY") and len(symbol) > 10):
            continue
        entry   = float(p.get("avg_entry_price", 0))
        current = float(p.get("current_price", 0))
        pnl_pct = round(((current - entry) / entry) * 100, 1) if entry else 0
        result.append({
            "symbol":        symbol,
            "qty":           int(p.get("qty", 0)),
            "entry_price":   entry,
            "current_price": current,
            "market_value":  float(p.get("market_value", 0)),
            "unrealized_pl": float(p.get("unrealized_pl", 0)),
            "pnl_pct":       pnl_pct,
        })
    return result

def close_position(symbol: str) -> dict:
    """Close entire position for a symbol"""
    r = requests.delete(
        f"{BASE}/v2/positions/{symbol}",
        headers=_headers()
    )
    if not r.ok:
        return {"error": r.text}
    return {"closed": True, "symbol": symbol}

def close_all_positions() -> dict:
    """
    SAFE VERSION: Only closes SPY options opened by this script.
    Never touches stocks or non-SPY positions.
    """
    spy_positions = get_positions(spy_only=True)
    if not spy_positions:
        return {"closed_all": True, "closed_count": 0, "note": "no SPY options to close"}

    closed = []
    errors = []
    for pos in spy_positions:
        sym = pos["symbol"]
        r = requests.delete(f"{BASE}/v2/positions/{sym}", headers=_headers())
        if r.ok:
            closed.append(sym)
        else:
            errors.append(sym)

    return {
        "closed_all":   len(errors) == 0,
        "closed_count": len(closed),
        "closed":       closed,
        "errors":       errors,
        "note":         "SPY options only — stocks untouched"
    }

def close_all_positions_NUCLEAR() -> dict:
    """
    ⚠️  DANGER: Closes EVERY position in account including stocks.
    Only call this manually if you explicitly want to nuke everything.
    Never called automatically by the trading system.
    """
    r = requests.delete(
        f"{BASE}/v2/positions",
        headers=_headers(),
        params={"cancel_orders": True}
    )
    return {"closed_all": r.ok, "status_code": r.status_code}

# ──────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────
def _calc_dte(expiry_str: Optional[str]) -> int:
    if not expiry_str:
        return 0
    try:
        expiry = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        return max(0, (expiry - date.today()).days)
    except:
        return 0

def is_market_open() -> bool:
    """Check if US market is currently open"""
    r = requests.get(f"{BASE}/v2/clock", headers=_headers())
    r.raise_for_status()
    return r.json().get("is_open", False)

def get_market_clock() -> dict:
    r = requests.get(f"{BASE}/v2/clock", headers=_headers())
    r.raise_for_status()
    c = r.json()
    return {
        "is_open":     c.get("is_open"),
        "next_open":   c.get("next_open"),
        "next_close":  c.get("next_close"),
        "timestamp":   c.get("timestamp"),
    }
