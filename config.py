"""
config.py - Central configuration loader
All env vars accessed through this module
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Anthropic ──────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL      = "claude-sonnet-4-6"  # default; escalate to opus when needed

# ── Alpaca ─────────────────────────────────────────────────
PAPER_MODE = os.getenv("PAPER_MODE", "true").lower() == "true"

if PAPER_MODE:
    ALPACA_API_KEY    = os.getenv("ALPACA_PAPER_KEY")
    ALPACA_SECRET_KEY = os.getenv("ALPACA_PAPER_SECRET")
    ALPACA_BASE_URL   = os.getenv("ALPACA_PAPER_URL", "https://paper-api.alpaca.markets")
else:
    ALPACA_API_KEY    = os.getenv("ALPACA_LIVE_KEY")
    ALPACA_SECRET_KEY = os.getenv("ALPACA_LIVE_SECRET")
    ALPACA_BASE_URL   = os.getenv("ALPACA_LIVE_URL", "https://api.alpaca.markets")

# ── Timezone ───────────────────────────────────────────────
TIMEZONE = os.getenv("TIMEZONE", "America/Los_Angeles")

# ── Risk Guardrails (HARD LIMITS - not Claude's decision) ──
MAX_DAILY_LOSS_USD       = float(os.getenv("MAX_DAILY_LOSS_USD", 200))
MAX_CONTRACTS            = int(os.getenv("MAX_CONTRACTS", 1))
MAX_PREMIUM_PER_TRADE    = float(os.getenv("MAX_PREMIUM_PER_TRADE_USD", 150))
MIN_PREMIUM_PER_TRADE    = float(os.getenv("MIN_PREMIUM_PER_TRADE_USD", 0.10))
MAX_TRADES_PER_DAY       = int(os.getenv("MAX_TRADES_PER_DAY", 3))
PROFIT_TARGET_PCT        = float(os.getenv("PROFIT_TARGET_PCT", 40))
STOP_LOSS_PCT            = float(os.getenv("STOP_LOSS_PCT", 25))

# ── Trading Schedule (PST) ─────────────────────────────────
AGENT_WAKE_TIME    = os.getenv("AGENT_WAKE_TIME_PST", "06:15")
MARKET_OPEN        = os.getenv("MARKET_OPEN_PST", "06:30")
NO_NEW_TRADES_TIME = os.getenv("NO_NEW_TRADES_PST", "11:30")
FORCE_EXIT_TIME    = os.getenv("FORCE_EXIT_PST", "12:45")
MARKET_CLOSE       = os.getenv("MARKET_CLOSE_PST", "13:00")

# ── Scan Intervals (seconds) ───────────────────────────────
PRICE_SCAN_INTERVAL   = int(os.getenv("PRICE_SCAN_INTERVAL", 60))
VIX_SCAN_INTERVAL     = int(os.getenv("VIX_SCAN_INTERVAL", 300))
OPTIONS_PULSE_INTERVAL = int(os.getenv("OPTIONS_PULSE_INTERVAL", 900))
FULL_CHAIN_INTERVAL   = int(os.getenv("FULL_CHAIN_INTERVAL", 1800))
ELEVATED_SCAN_INTERVAL = int(os.getenv("ELEVATED_SCAN_INTERVAL", 30))

# ── Strategy Alert Thresholds ──────────────────────────────
VIX_HIGH_THRESHOLD = float(os.getenv("VIX_HIGH_THRESHOLD", 27))
VIX_SPIKE_ALERT    = float(os.getenv("VIX_SPIKE_ALERT", 5))
SPY_MOVE_ALERT_PCT = float(os.getenv("SPY_MOVE_ALERT_PCT", 1.5))

# ── Elevated Scan Windows (PST) ────────────────────────────
ELEVATED_WINDOWS = [
    ("06:30", "07:30"),  # market open
    ("09:00", "09:30"),  # midday chop
    ("12:30", "13:00"),  # pre-close
]

# ── Runtime overrides (set by main.py from favorites.txt) ─
ACTIVE_TICKER  = "SPY"                          # from favorites.txt
ACTIVE_DTE     = 0                              # 0=today, 1=tomorrow
ACTIVE_EXPIRY  = ""                             # calculated expiry date
DRY_RUN        = os.getenv("DRY_RUN", "false").lower() == "true"

# ── News APIs ──────────────────────────────────────────────
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")      # optional: newsapi.org key

# ── Twitter/X Filtered Stream ──────────────────────────────
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN", "")
TWITTER_ACCOUNTS     = [
    a.strip().lstrip("@")
    for a in os.getenv("TWITTER_ACCOUNTS", "").split(",")
    if a.strip()
]

# ── Discord channel polling ────────────────────────────────
# Auth option A: pre-set user/bot token
DISCORD_TOKEN       = os.getenv("DISCORD_TOKEN", "")
# Auth option B: email + password (module logs in at startup to get a token)
DISCORD_EMAIL       = os.getenv("DISCORD_EMAIL", "")
DISCORD_PASSWORD    = os.getenv("DISCORD_PASSWORD", "")
DISCORD_CHANNEL_IDS = [
    c.strip()
    for c in os.getenv("DISCORD_CHANNEL_IDS", "").split(",")
    if c.strip()
]

# ── Stock Day-Trading Config (stock-only branch) ───────────
STOCK_MAX_PORTFOLIO      = float(os.getenv("STOCK_MAX_PORTFOLIO",      10_000))
STOCK_BASE_ALLOCATION    = float(os.getenv("STOCK_BASE_ALLOCATION",     2_000))
STOCK_MAX_POSITIONS      = int(os.getenv("STOCK_MAX_POSITIONS",             5))
STOCK_MAX_TRADES_PER_DAY = int(os.getenv("STOCK_MAX_TRADES_PER_DAY",      10))
STOCK_MAX_DAILY_LOSS_USD = float(os.getenv("STOCK_MAX_DAILY_LOSS_USD",    500))
STOCK_INITIAL_STOP_PCT   = float(os.getenv("STOCK_INITIAL_STOP_PCT",      -5.0))
STOCK_TRAIL_PCT          = float(os.getenv("STOCK_TRAIL_PCT",               3.0))  # % below peak
STOCK_NO_NEW_TRADES_TIME    = os.getenv("STOCK_NO_NEW_TRADES_PST",        "12:30")
STOCK_FORCE_EXIT_TIME       = os.getenv("STOCK_FORCE_EXIT_PST",           "12:45")
STOCK_MIN_BAR_DOLLAR_VOL    = float(os.getenv("STOCK_MIN_BAR_DOLLAR_VOL", 2_000_000))  # thin market filter
STOCK_WINNER_EXIT_TIME      = os.getenv("STOCK_WINNER_EXIT_PST",          "12:58")     # winners run until here

def validate():
    """Validate all required config is present on startup"""
    errors = []
    if not ANTHROPIC_API_KEY:
        errors.append("❌ ANTHROPIC_API_KEY missing")
    if not ALPACA_API_KEY:
        errors.append(f"❌ {'ALPACA_PAPER_KEY' if PAPER_MODE else 'ALPACA_LIVE_KEY'} missing")
    if not ALPACA_SECRET_KEY:
        errors.append(f"❌ {'ALPACA_PAPER_SECRET' if PAPER_MODE else 'ALPACA_LIVE_SECRET'} missing")
    if errors:
        for e in errors:
            print(e)
        raise SystemExit("Fix .env config before running")
    mode = "📄 PAPER" if PAPER_MODE else "🔴 LIVE"
    print(f"✅ Config loaded | Mode: {mode} | Model: {CLAUDE_MODEL}")
