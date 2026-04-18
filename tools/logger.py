"""
tools/logger.py
Structured logging for trades, decisions, errors, and daily P&L.

Files written to logs/:
  YYYY-MM-DD.log  — human-readable event log for each trading day
  trades.csv      — one row per completed trade (for the dashboard)
  trades.json     — full trade detail (JSON)
  decisions.json  — Claude reasoning log
  errors.json     — error log
  pnl.json        — daily P&L history
"""

import csv
import json
import os
from datetime import datetime
import pytz

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")

_TRADES_CSV = os.path.join(LOG_DIR, "trades.csv")
_CSV_FIELDS = [
    "date", "ticker", "entry_time", "exit_time",
    "entry_price", "exit_price", "shares",
    "pnl", "pnl_pct", "reason", "source",
]


def _ensure_logs():
    os.makedirs(LOG_DIR, exist_ok=True)


def _log_path(filename: str) -> str:
    _ensure_logs()
    return os.path.join(LOG_DIR, filename)


def _now_dt() -> datetime:
    return datetime.now(pytz.timezone(config.TIMEZONE))


def _now() -> str:
    return _now_dt().isoformat()


def _today_str() -> str:
    return _now_dt().strftime("%Y-%m-%d")


def _hms() -> str:
    """HH:MM:SS for event log lines."""
    return _now_dt().strftime("%H:%M:%S")


def _append_json(filepath: str, entry: dict):
    records = []
    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            try:
                records = json.load(f)
            except Exception:
                records = []
    records.append(entry)
    with open(filepath, "w") as f:
        json.dump(records, f, indent=2, default=str)

# ──────────────────────────────────────────────────────────
# HUMAN-READABLE DAILY LOG  (logs/YYYY-MM-DD.log)
# ──────────────────────────────────────────────────────────

def log_event(msg: str):
    """
    Append a timestamped line to today's human-readable log file.
    Call this for every meaningful trading event so a post-market
    analyst can read logs/YYYY-MM-DD.log end-to-end.
    """
    _ensure_logs()
    path = _log_path(f"{_today_str()}.log")
    line = f"[{_hms()}] {msg}\n"
    with open(path, "a") as f:
        f.write(line)


# ──────────────────────────────────────────────────────────
# CSV TRADE RECORD  (logs/trades.csv)
# ──────────────────────────────────────────────────────────

def log_trade_csv(trade: dict):
    """
    Append one row to logs/trades.csv for dashboard consumption.
    Expected keys (all optional with sensible defaults):
      date, ticker, entry_time, exit_time,
      entry_price, exit_price, shares, pnl, pnl_pct, reason, source
    """
    _ensure_logs()
    write_header = not os.path.exists(_TRADES_CSV)
    row = {f: trade.get(f, "") for f in _CSV_FIELDS}
    if not row["date"]:
        row["date"] = _today_str()
    with open(_TRADES_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


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
    """
    Persist trade to trades.json.
    If the trade is a completed sell (has exit_price), also write a CSV row
    and append a summary line to the daily event log.
    """
    trade["logged_at"] = _now()
    _append_json(_log_path("trades.json"), trade)

    side = trade.get("side", "")
    if side == "sell":
        # Write CSV row for dashboard
        log_trade_csv({
            "date":         _today_str(),
            "ticker":       trade.get("symbol", ""),
            "entry_time":   trade.get("entry_time", ""),
            "exit_time":    trade.get("time", ""),
            "entry_price":  trade.get("entry_price", ""),
            "exit_price":   trade.get("fill_price", ""),
            "shares":       trade.get("qty", ""),
            "pnl":          trade.get("pnl", ""),
            "pnl_pct":      trade.get("pnl_pct", ""),
            "reason":       trade.get("reason", ""),
            "source":       trade.get("source", ""),
        })
        # Write event log line
        pnl    = trade.get("pnl", 0) or 0
        pct    = trade.get("pnl_pct", 0) or 0
        ep     = trade.get("entry_price", 0)
        xp     = trade.get("fill_price", 0)
        held   = trade.get("held_mins", "?")
        reason = trade.get("reason", "")
        src    = trade.get("source", "")
        emoji  = "✅" if pnl >= 0 else "❌"
        log_event(
            f"EXIT  {emoji} {trade.get('symbol','?'):6s} | "
            f"entry ${ep:.2f} → exit ${xp:.2f} | "
            f"P&L ${pnl:+.2f} ({pct:+.1f}%) | held {held} | "
            f"{reason} | source: {src}"
        )
    elif side == "buy":
        ep  = trade.get("fill_price", 0) or trade.get("quote_price", 0)
        qty = trade.get("qty", 0)
        src = trade.get("source", "")
        conf = trade.get("confidence", 0) or 0
        log_event(
            f"ENTRY ✅ {trade.get('symbol','?'):6s} | "
            f"{qty}sh @ ${ep:.2f} | conf {conf:.0%} | source: {src}"
        )


def log_error(context: str, error: str):
    """Log an error to errors.json and the daily event log."""
    entry = {"timestamp": _now(), "context": context, "error": error}
    _append_json(_log_path("errors.json"), entry)
    log_event(f"ERROR [{context}]: {error[:120]}")
    print(f"[{_hms()}] ❌ ERROR [{context}]: {error}")


def log_pnl(daily_pnl: float, trades_today: int, note: str = ""):
    """Log daily P&L summary to pnl.json and the daily event log."""
    today = _today_str()
    entry = {
        "date":         today,
        "daily_pnl":    daily_pnl,
        "trades_today": trades_today,
        "note":         note,
    }
    _append_json(_log_path("pnl.json"), entry)
    emoji = "🟢" if daily_pnl >= 0 else "🔴"
    log_event(
        f"END OF DAY {emoji} | P&L ${daily_pnl:+.2f} | trades {trades_today} | {note}"
    )
    print(f"[{_hms()}] {emoji} Daily P&L: ${daily_pnl:.2f} ({trades_today} trades)")

def log_news(scan_type: str, headlines: list, thesis: str):
    """Log morning news scan results"""
    entry = {
        "timestamp":  _now(),
        "scan_type":  scan_type,
        "headlines":  headlines,
        "thesis":     thesis,
    }
    _append_json(_log_path("news.json"), entry)
