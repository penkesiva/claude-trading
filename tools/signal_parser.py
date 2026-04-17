"""
tools/signal_parser.py
Parse Discord/Twitter messages for directional analyst signals.

We trade STOCKS, not options — but analysts post option calls/puts as
directional signals:
  "TSLA calls 🚀"  → bullish  → BUY the underlying stock
  "TSLA puts 🔴"   → bearish  → SELL the stock if we're holding it

Signal types:
  "call" — analyst is bullish, analyst says calls/buy/long/🚀
  "put"  — analyst is bearish, analyst says puts/sell/short/🔴

Returns an AnalystSignal dict:
  {
    "ticker":       "TSLA",
    "signal_type":  "call" | "put",
    "raw_text":     "original message (truncated to 300 chars)",
    "source":       "discord" | "twitter",
    "source_label": "ashleytheprotrader",
    "confidence":   0.0 – 1.0,
    "published_at": ISO-8601 string,
  }
"""

import re
from typing import Optional
from prompts.stock_system_prompt import STOCK_UNIVERSE

# Convert to set for O(1) lookups
_UNIVERSE_SET = set(STOCK_UNIVERSE)

# Bullish keywords → signal_type = "call"
_CALL_KEYWORDS = re.compile(
    r'\bcalls?\b|\blong\b|\bbuying\b|\bbuy\b|\bbullish\b|\bbreakout\b|\bbreaking\s+out\b',
    re.IGNORECASE,
)

# Bearish keywords → signal_type = "put"
_PUT_KEYWORDS = re.compile(
    r'\bputs?\b|\bshort(?:ing)?\b|\bsell(?:ing)?\b|\bbearish\b|\bbeware\b|\bdumping\b|\bexiting\b',
    re.IGNORECASE,
)

# Bullish emojis
_BULLISH_EMOJIS = {"🚀", "🔥", "🟢", "💚", "⬆️", "📈", "💰", "🎯", "🐂"}

# Bearish emojis
_BEARISH_EMOJIS = {"🔴", "📉", "🐻", "⬇️", "💀", "🩸"}

# Ticker patterns: $TICKER or bare TICKER at word boundary
# Also captures option-style: TSLA 250c, TSLA250c, $TSLA200C
_TICKER_PATTERN = re.compile(
    r'\$([A-Z]{1,5})'                          # $TSLA
    r'|(?<!\w)([A-Z]{2,5})(?=\d{2,4}[cCpP])'  # TSLA200c (option style)
    r'|(?<!\w)([A-Z]{2,5})(?!\w)',              # bare TSLA
)


def _has_bullish_emoji(text: str) -> bool:
    return any(e in text for e in _BULLISH_EMOJIS)


def _has_bearish_emoji(text: str) -> bool:
    return any(e in text for e in _BEARISH_EMOJIS)


def _confidence(text: str, ticker: str, explicit_keyword: bool) -> float:
    """Heuristic confidence score 0.0 – 1.0."""
    score = 0.4  # base

    if explicit_keyword:
        score += 0.30   # explicit "calls"/"puts" mentioned

    if _has_bullish_emoji(text) or _has_bearish_emoji(text):
        score += 0.15

    # Ticker mentioned multiple times → higher conviction
    count = len(re.findall(rf'(?<!\w){re.escape(ticker)}(?!\w)', text, re.IGNORECASE))
    if count >= 2:
        score += 0.10
    if count >= 3:
        score += 0.05

    if text.count("!") >= 2:
        score += 0.05

    return min(round(score, 2), 1.0)


def parse_signal(article: dict) -> Optional[dict]:
    """
    Given an article dict (from discord_tools or twitter_tools), return an
    AnalystSignal dict or None if no actionable signal is found.

    Decision logic:
      1. Count bullish vs bearish indicators.
      2. If bullish wins → signal_type = "call"
      3. If bearish wins → signal_type = "put"
      4. If tied or no clear direction → None
    """
    text   = (article.get("headline") or "") + " " + (article.get("summary") or "")
    source = article.get("source", "unknown")
    ts     = article.get("published_at", "")

    # Strip Discord username prefix "[author] message" — keep for label
    author_match = re.match(r'^\[([^\]]+)\]\s*', text)
    source_label = author_match.group(1) if author_match else source

    # Find all ticker candidates in the universe
    raw_tokens = _TICKER_PATTERN.findall(text)
    candidates = [t for group in raw_tokens for t in group if t and t in _UNIVERSE_SET]
    if not candidates:
        return None

    # Score bullish vs bearish signals
    has_call_kw   = bool(_CALL_KEYWORDS.search(text))
    has_put_kw    = bool(_PUT_KEYWORDS.search(text))
    has_bull_emo  = _has_bullish_emoji(text)
    has_bear_emo  = _has_bearish_emoji(text)

    bull_score = (2 if has_call_kw else 0) + (1 if has_bull_emo else 0)
    bear_score = (2 if has_put_kw  else 0) + (1 if has_bear_emo else 0)

    # Need at least one clear direction indicator
    if bull_score == 0 and bear_score == 0:
        return None

    # If both are equally strong, skip — ambiguous message
    if bull_score == bear_score:
        return None

    signal_type     = "call" if bull_score > bear_score else "put"
    explicit_keyword = has_call_kw if signal_type == "call" else has_put_kw

    # Pick the ticker with the most mentions as primary signal
    primary = max(set(candidates), key=candidates.count)

    conf = _confidence(text, primary, explicit_keyword)

    if conf < 0.45:
        return None

    return {
        "ticker":       primary,
        "signal_type":  signal_type,
        "raw_text":     text[:300],
        "source":       source,
        "source_label": source_label,
        "confidence":   conf,
        "published_at": ts,
    }


def extract_signals(articles: list) -> list:
    """
    Run parse_signal over a list of articles.
    Returns deduplicated signals (per ticker+type) sorted by confidence descending.
    """
    seen: set = set()
    results = []

    for article in articles:
        sig = parse_signal(article)
        if not sig:
            continue
        key = (sig["ticker"], sig["signal_type"])
        if key not in seen:
            seen.add(key)
            results.append(sig)

    results.sort(key=lambda s: s["confidence"], reverse=True)
    return results
