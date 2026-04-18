"""
tools/logger.py
Structured logging for trades, decisions, errors, and daily P&L.

Files written to logs/:
  YYYY-MM-DD.log       — human-readable event log for each trading day
  trades.csv           — one row per completed trade (for the dashboard)
  ticker_profiles.json — per-ticker learning: win rate, avg P&L, suggested trail %
  trades.json          — full trade detail (JSON)
  decisions.json       — Claude reasoning log
  errors.json          — error log
  pnl.json             — daily P&L history
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


# ──────────────────────────────────────────────────────────
# PER-TICKER LEARNING PROFILES  (logs/ticker_profiles.json)
# ──────────────────────────────────────────────────────────

_PROFILES_FILE = "ticker_profiles.json"


def load_ticker_profiles() -> dict:
    """
    Load per-ticker performance profiles from logs/ticker_profiles.json.
    Returns an empty dict if the file doesn't exist yet.
    """
    path = os.path.join(LOG_DIR, _PROFILES_FILE)
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        try:
            return json.load(f)
        except Exception:
            return {}


def update_ticker_profiles() -> dict:
    """
    Read ALL rows in logs/trades.csv, compute per-ticker stats, and write
    logs/ticker_profiles.json.  Called at end of every trading session and
    by dashboard/generate.py so the bot learns incrementally day over day.

    Stats computed per ticker:
      trades, wins, win_rate, total_pnl, avg_pnl, avg_win, avg_loss
      avg_hold_mins, avg_win_pct (typical winning move size)
      suggested_trail_pct  (calibrated to actual price behaviour)
      by_source (P&L + win rate per signal source)
      best_source, last_traded, trade_dates
    """
    _ensure_logs()
    if not os.path.exists(_TRADES_CSV):
        return {}

    # Load all trades from CSV
    trades: list[dict] = []
    with open(_TRADES_CSV, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                row["pnl"]         = float(row.get("pnl") or 0)
                row["pnl_pct"]     = float(row.get("pnl_pct") or 0)
                row["shares"]      = int(row.get("shares") or 0)
                row["entry_price"] = float(row.get("entry_price") or 0)
                row["exit_price"]  = float(row.get("exit_price") or 0)
                trades.append(row)
            except Exception:
                pass

    if not trades:
        return {}

    profiles: dict = {}
    tickers = sorted({t["ticker"] for t in trades if t.get("ticker")})

    for ticker in tickers:
        tt     = [t for t in trades if t["ticker"] == ticker]
        wins   = [t for t in tt if t["pnl"] > 0]
        losses = [t for t in tt if t["pnl"] <= 0]

        # ── Hold time ──
        hold_mins: list[int] = []
        for t in tt:
            hm = str(t.get("held_mins") or "").replace("m", "").strip()
            if hm.isdigit():
                hold_mins.append(int(hm))

        # ── Source breakdown ──
        by_source: dict = {}
        for t in tt:
            raw = (t.get("source") or "morning scan")
            src = raw.split(":")[0].strip().lower()
            if src not in by_source:
                by_source[src] = {"trades": 0, "wins": 0, "pnl": 0.0}
            by_source[src]["trades"] += 1
            if t["pnl"] > 0:
                by_source[src]["wins"] += 1
            by_source[src]["pnl"] = round(by_source[src]["pnl"] + t["pnl"], 2)

        # Best source: highest win rate among sources with ≥ 2 trades
        eligible = [(s, d) for s, d in by_source.items() if d["trades"] >= 2]
        best_src = (
            max(eligible, key=lambda x: x[1]["wins"] / x[1]["trades"])[0]
            if eligible else None
        )

        # ── Avg winning move size ──
        win_pcts    = [t["pnl_pct"] for t in wins if t["pnl_pct"] > 0]
        avg_win_pct = round(sum(win_pcts) / len(win_pcts), 2) if win_pcts else 0.0

        # ── Suggested trail % ──
        # Logic: trail at ~55% of the typical winning move, clamped 1.0–4.0%.
        # Requires ≥ 3 trades to avoid noise.
        suggested_trail: float | None = None
        if len(tt) >= 3 and avg_win_pct > 0:
            suggested_trail = round(max(1.0, min(4.0, avg_win_pct * 0.55)), 1)

        profiles[ticker] = {
            "trades":               len(tt),
            "wins":                 len(wins),
            "losses":               len(losses),
            "win_rate":             round(len(wins) / len(tt), 3) if tt else 0,
            "total_pnl":            round(sum(t["pnl"] for t in tt), 2),
            "avg_pnl":              round(sum(t["pnl"] for t in tt) / len(tt), 2) if tt else 0,
            "avg_win":              round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else 0,
            "avg_loss":             round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0,
            "avg_hold_mins":        int(sum(hold_mins) / len(hold_mins)) if hold_mins else None,
            "avg_win_pct":          avg_win_pct,
            "suggested_trail_pct":  suggested_trail,
            "by_source":            by_source,
            "best_source":          best_src,
            "last_traded":          max(t["date"] for t in tt),
            "trade_dates":          sorted({t["date"] for t in tt}),
        }

    path = _log_path(_PROFILES_FILE)
    with open(path, "w") as f:
        json.dump(profiles, f, indent=2, default=str)

    tickers_str = ", ".join(profiles.keys())
    print(f"   📊 Ticker profiles updated ({len(profiles)} tickers): {tickers_str}")
    log_event(f"PROFILES updated for {len(profiles)} ticker(s): {tickers_str}")
    return profiles
