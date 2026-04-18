#!/usr/bin/env python3
"""
dashboard/generate.py
Post-market summary generator.

Run after market close to:
  1. Print a concise P&L summary to the terminal
  2. Open dashboard/index.html in your browser (requires a local HTTP server
     because the HTML fetches trades.csv via fetch())

Usage:
  python3 dashboard/generate.py            # summary + open browser
  python3 dashboard/generate.py --summary  # terminal summary only
  python3 dashboard/generate.py --serve    # start local server + open browser
"""

import argparse
import csv
import http.server
import os
import sys
import threading
import webbrowser
from datetime import datetime, date

ROOT    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ROOT, "logs")
CSV     = os.path.join(LOG_DIR, "trades.csv")
DASH    = os.path.join(ROOT, "dashboard", "index.html")


# ─── helpers ───────────────────────────────────────────────

def _fmt(v: float, sign: bool = True) -> str:
    prefix = ("+" if v >= 0 else "-") if sign else ""
    return f"{prefix}${abs(v):.2f}"


def _pct(v: float) -> str:
    return f"{'+'if v>=0 else ''}{v:.1f}%"


def load_trades() -> list[dict]:
    if not os.path.exists(CSV):
        return []
    with open(CSV, newline="") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            try:
                row["pnl"]     = float(row.get("pnl") or 0)
                row["pnl_pct"] = float(row.get("pnl_pct") or 0)
                row["shares"]  = int(row.get("shares") or 0)
                rows.append(row)
            except Exception:
                pass
        return rows


# ─── print summary ─────────────────────────────────────────

def print_summary(trades: list[dict], target_date: str = None):
    if not trades:
        print("No trades in logs/trades.csv yet.")
        return

    today = target_date or str(date.today())
    today_trades = [t for t in trades if t["date"] == today]
    all_pnl      = sum(t["pnl"] for t in trades)
    today_pnl    = sum(t["pnl"] for t in today_trades)

    wins         = [t for t in trades if t["pnl"] > 0]
    losses       = [t for t in trades if t["pnl"] <= 0]
    gross_win    = sum(t["pnl"] for t in wins)
    gross_loss   = abs(sum(t["pnl"] for t in losses))
    pf           = gross_win / gross_loss if gross_loss else float("inf")
    win_rate     = len(wins) / len(trades) * 100 if trades else 0
    avg_win      = gross_win / len(wins) if wins else 0
    avg_loss     = gross_loss / len(losses) if losses else 0

    days         = sorted({t["date"] for t in trades})
    daily_pnl    = {d: sum(t["pnl"] for t in trades if t["date"] == d) for d in days}
    best_day     = max(daily_pnl, key=daily_pnl.get) if daily_pnl else None
    worst_day    = min(daily_pnl, key=daily_pnl.get) if daily_pnl else None

    W = 62
    sep = "─" * W

    print(f"\n{'═'*W}")
    print(f"  CLAUDE STOCK TRADER — POST-MARKET SUMMARY")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═'*W}")

    if today_trades:
        emoji = "🟢" if today_pnl >= 0 else "🔴"
        print(f"\n  TODAY ({today})")
        print(f"  {emoji} P&L: {_fmt(today_pnl)}  |  Trades: {len(today_trades)}")
        print(f"\n  {'Ticker':<8} {'Entry':>8} {'Exit':>8} {'Shares':>6} {'P&L':>9} {'Reason':<30} Source")
        print(f"  {sep}")
        for t in today_trades:
            e = "✅" if t["pnl"] >= 0 else "❌"
            print(
                f"  {e} {t['ticker']:<6} "
                f"${float(t['entry_price'] or 0):>7.2f} "
                f"${float(t['exit_price']  or 0):>7.2f} "
                f"{t['shares']:>5}sh "
                f"{_fmt(t['pnl']):>8}  "
                f"{(t['reason'] or '')[:28]:<30} "
                f"{(t['source'] or '')[:22]}"
            )
    else:
        print(f"\n  No trades recorded for today ({today}).")

    print(f"\n  {sep}")
    print(f"  ALL-TIME STATS  ({len(days)} trading day(s))")
    print(f"  {sep}")
    print(f"  Total P&L:      {_fmt(all_pnl)}")
    print(f"  Win rate:       {win_rate:.0f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Profit factor:  {pf:.2f}")
    print(f"  Avg winner:     {_fmt(avg_win)}")
    print(f"  Avg loser:      {_fmt(avg_loss)}")
    if best_day:
        print(f"  Best day:       {best_day}  {_fmt(daily_pnl[best_day])}")
    if worst_day:
        print(f"  Worst day:      {worst_day}  {_fmt(daily_pnl[worst_day])}")

    # Per-ticker breakdown
    tickers = sorted({t["ticker"] for t in trades})
    print(f"\n  PER-TICKER")
    print(f"  {'Ticker':<8} {'Trades':>6} {'Win%':>6} {'Avg P&L':>9} {'Total P&L':>10}")
    print(f"  {sep}")
    ticker_stats = []
    for tk in tickers:
        tt  = [t for t in trades if t["ticker"] == tk]
        tw  = [t for t in tt if t["pnl"] > 0]
        wr  = len(tw)/len(tt)*100 if tt else 0
        ap  = sum(t["pnl"] for t in tt) / len(tt)
        tp  = sum(t["pnl"] for t in tt)
        ticker_stats.append((tk, len(tt), wr, ap, tp))
    for tk, n, wr, ap, tp in sorted(ticker_stats, key=lambda x: -x[4]):
        e = "🟢" if tp >= 0 else "🔴"
        print(f"  {e} {tk:<7} {n:>5}   {wr:>5.0f}%  {_fmt(ap):>9}  {_fmt(tp):>9}")

    print(f"\n  Log file: {CSV}")
    print(f"  Dashboard: open dashboard/index.html (use --serve to auto-launch)\n")


# ─── local HTTP server ──────────────────────────────────────

def serve_and_open(port: int = 8765):
    """Serve the project root on localhost and open the dashboard."""
    os.chdir(ROOT)

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # suppress per-request logs

    server = http.server.HTTPServer(("127.0.0.1", port), QuietHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    url = f"http://127.0.0.1:{port}/dashboard/index.html"
    print(f"  🌐 Dashboard served at {url}")
    print(f"  Press Ctrl+C to stop.\n")
    webbrowser.open(url)

    try:
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Server stopped.")
        server.shutdown()


# ─── entry point ───────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Post-market summary + dashboard launcher")
    parser.add_argument("--summary", action="store_true", help="Print summary only, no browser")
    parser.add_argument("--serve",   action="store_true", help="Start local server + open browser")
    parser.add_argument("--date",    default=None,        help="Target date YYYY-MM-DD (default: today)")
    parser.add_argument("--port",    type=int, default=8765, help="Local server port (default 8765)")
    args = parser.parse_args()

    trades = load_trades()
    print_summary(trades, target_date=args.date)

    if args.summary:
        return

    if args.serve:
        serve_and_open(args.port)
    else:
        # Default: just open the file directly (works in most browsers)
        url = f"file://{DASH}"
        print(f"  Opening {url}")
        print(f"  Note: If charts don't load, run with --serve instead.\n")
        webbrowser.open(url)


if __name__ == "__main__":
    main()
