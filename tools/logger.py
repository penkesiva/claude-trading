"""
tools/logger.py
Structured logging for trades, decisions, errors, and daily P&L
"""

import json
import os
from datetime import datetime
import pytz

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")

def _ensure_logs():
    os.makedirs(LOG_DIR, exist_ok=True)

def _log_path(filename: str) -> str:
    _ensure_logs()
    return os.path.join(LOG_DIR, filename)

def _now() -> str:
    return datetime.now(pytz.timezone(config.TIMEZONE)).isoformat()

def _append_json(filepath: str, entry: dict):
    records = []
    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            try:
                records = json.load(f)
            except:
                records = []
    records.append(entry)
    with open(filepath, "w") as f:
        json.dump(records, f, indent=2, default=str)

# ──────────────────────────────────────────────────────────

def log_decision(signal_snapshot: dict, claude_reasoning: str, action: str, trade: dict = None):
    """Log Claude's full reasoning and decision (file only — no console noise)."""
    entry = {
        "timestamp":  _now(),
        "signals":    signal_snapshot,
        "reasoning":  claude_reasoning,
        "action":     action,
        "trade":      trade,
    }
    _append_json(_log_path("decisions.json"), entry)

def log_trade(trade: dict):
    """Persist trade to trades.json (file only — entry/exit prints own summary line)."""
    trade["logged_at"] = _now()
    _append_json(_log_path("trades.json"), trade)

def log_error(context: str, error: str):
    """Log an error"""
    entry = {"timestamp": _now(), "context": context, "error": error}
    _append_json(_log_path("errors.json"), entry)
    print(f"[{_now()}] ❌ ERROR [{context}]: {error}")

def log_pnl(daily_pnl: float, trades_today: int, note: str = ""):
    """Log daily P&L summary"""
    entry = {
        "date":         datetime.now(pytz.timezone(config.TIMEZONE)).strftime("%Y-%m-%d"),
        "daily_pnl":    daily_pnl,
        "trades_today": trades_today,
        "note":         note,
    }
    _append_json(_log_path("pnl.json"), entry)
    emoji = "🟢" if daily_pnl >= 0 else "🔴"
    print(f"[{_now()}] {emoji} Daily P&L: ${daily_pnl:.2f} ({trades_today} trades)")

def log_news(scan_type: str, headlines: list, thesis: str):
    """Log morning news scan results"""
    entry = {
        "timestamp":  _now(),
        "scan_type":  scan_type,
        "headlines":  headlines,
        "thesis":     thesis,
    }
    _append_json(_log_path("news.json"), entry)
