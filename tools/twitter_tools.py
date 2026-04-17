"""
tools/twitter_tools.py
Real-time Twitter/X signal feed via the Filtered Stream API.

Runs a persistent HTTP stream in a background daemon thread.
Every matching tweet lands in a thread-safe deque that get_recent_tweets()
reads from — no blocking of the main trading loop.

Requires:
  TWITTER_BEARER_TOKEN  — App-only Bearer Token from developer.x.com
  TWITTER_ACCOUNTS      — Comma-separated handles to follow, no @ (e.g. "unusual_whales,DeItaone")
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import json
import threading
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import List

import requests

import config

# ── Constants ──────────────────────────────────────────────
_STREAM_URL    = "https://api.x.com/2/tweets/search/stream"
_RULES_URL     = "https://api.x.com/2/tweets/search/stream/rules"
_RULE_TAG      = "claude-trading-signal"
_RECONNECT_WAIT = 5    # seconds before reconnecting on error
_MAX_RECONNECTS = 999  # effectively unlimited

# Thread-safe circular buffer — holds the last 100 tweets
_signal_cache: deque = deque(maxlen=100)
_cache_lock = threading.Lock()

_stream_thread: threading.Thread | None = None


# ── Auth ──────────────────────────────────────────────────

def _bearer_headers() -> dict:
    token = getattr(config, "TWITTER_BEARER_TOKEN", "") or ""
    return {"Authorization": f"Bearer {token.strip()}"}


def _is_configured() -> bool:
    token    = getattr(config, "TWITTER_BEARER_TOKEN", "") or ""
    accounts = getattr(config, "TWITTER_ACCOUNTS", []) or []
    return bool(token.strip()) and bool(accounts)


# ── Rule management ───────────────────────────────────────

def _build_rule() -> str:
    """Returns a Filtered Stream rule matching tweets from configured accounts."""
    accounts = getattr(config, "TWITTER_ACCOUNTS", [])
    froms = " OR ".join(f"from:{a.strip().lstrip('@')}" for a in accounts if a.strip())
    return f"({froms}) -is:retweet lang:en"


def _get_existing_rules() -> list:
    try:
        r = requests.get(_RULES_URL, headers=_bearer_headers(), timeout=10)
        r.raise_for_status()
        return r.json().get("data") or []
    except Exception as e:
        print(f"   ⚠️  Twitter: could not fetch rules: {e}")
        return []


def _delete_rules(rule_ids: list):
    if not rule_ids:
        return
    payload = {"delete": {"ids": rule_ids}}
    try:
        requests.post(_RULES_URL, headers=_bearer_headers(), json=payload, timeout=10)
    except Exception as e:
        print(f"   ⚠️  Twitter: could not delete old rules: {e}")


def _upsert_rule():
    """Delete any existing rules with our tag, then add the current rule."""
    existing = _get_existing_rules()
    old_ids = [r["id"] for r in existing if r.get("tag") == _RULE_TAG]
    if old_ids:
        _delete_rules(old_ids)

    rule_value = _build_rule()
    payload = {"add": [{"value": rule_value, "tag": _RULE_TAG}]}
    try:
        r = requests.post(
            _RULES_URL,
            headers={**_bearer_headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
        r.raise_for_status()
        errors = r.json().get("errors") or []
        if errors:
            print(f"   ⚠️  Twitter rule error: {errors}")
        else:
            print(f"   ✅ Twitter rule set: {rule_value}")
    except Exception as e:
        print(f"   ❌ Twitter: failed to set rule: {e}")


# ── Tweet parser ─────────────────────────────────────────

def _parse_tweet(raw: dict) -> dict | None:
    """Convert a raw stream payload into the standard article schema."""
    data = raw.get("data") or {}
    tweet_id   = data.get("id", "")
    text       = data.get("text", "").strip()
    author_id  = data.get("author_id", "")
    created_at = data.get("created_at", datetime.now(timezone.utc).isoformat())

    if not text:
        return None

    # Extract uppercase ticker-like tokens (2-5 caps, optionally preceded by $)
    import re
    from prompts.stock_system_prompt import STOCK_UNIVERSE
    raw_tokens = re.findall(r'\$([A-Z]{1,5})|(?<!\w)([A-Z]{2,5})(?!\w)', text)
    tickers = list({
        t for group in raw_tokens for t in group
        if t and t in STOCK_UNIVERSE
    })

    return {
        "source":       "twitter",
        "headline":     text[:280],
        "summary":      "",
        "symbols":      tickers,
        "published_at": created_at,
        "url":          f"https://x.com/i/web/status/{tweet_id}",
        "author_id":    author_id,
    }


# ── Stream worker ─────────────────────────────────────────

def _stream_worker():
    """Runs forever in a daemon thread, reconnecting on any error."""
    _upsert_rule()

    params = {
        "tweet.fields": "created_at,author_id,text",
        "expansions":   "author_id",
    }

    reconnects = 0
    while reconnects < _MAX_RECONNECTS:
        try:
            print(f"   🐦 Twitter stream connecting (attempt {reconnects + 1})…")
            with requests.get(
                _STREAM_URL,
                headers=_bearer_headers(),
                params=params,
                stream=True,
                timeout=(10, 90),  # connect timeout, read timeout
            ) as resp:
                if resp.status_code != 200:
                    print(f"   ⚠️  Twitter stream HTTP {resp.status_code}: {resp.text[:200]}")
                    time.sleep(_RECONNECT_WAIT * (reconnects + 1))
                    reconnects += 1
                    continue

                print("   ✅ Twitter stream connected.")
                reconnects = 0  # reset on successful connect

                for raw_line in resp.iter_lines():
                    if not raw_line:
                        # Keep-alive blank line — connection is alive
                        continue
                    try:
                        payload = json.loads(raw_line)
                        article = _parse_tweet(payload)
                        if article:
                            with _cache_lock:
                                _signal_cache.appendleft(article)
                            print(
                                f"   🐦 Tweet [{', '.join(article['symbols']) or 'no ticker'}]: "
                                f"{article['headline'][:80]}…"
                            )
                            # Parse for actionable call signals and push to shared queue
                            try:
                                from tools.signal_parser import parse_signal
                                from tools.signal_queue  import push as push_signal
                                sig = parse_signal(article)
                                if sig:
                                    push_signal(sig)
                                    print(
                                        f"   📣 Signal [@{sig['source_label']}] → "
                                        f"{sig['ticker']} {sig['signal_type']} "
                                        f"(conf {sig['confidence']:.0%}): "
                                        f"{sig['raw_text'][:60]}…"
                                    )
                            except Exception:
                                pass
                    except json.JSONDecodeError:
                        continue

        except requests.exceptions.ChunkedEncodingError:
            # Normal: Twitter closes the connection every few hours
            print("   🐦 Twitter stream disconnected — reconnecting…")
        except Exception as e:
            print(f"   ⚠️  Twitter stream error: {e} — reconnecting in {_RECONNECT_WAIT}s")

        time.sleep(_RECONNECT_WAIT)
        reconnects += 1

    print("   ❌ Twitter stream: max reconnects reached — giving up.")


# ── Public API ────────────────────────────────────────────

def start_twitter_stream():
    """Start the background stream thread. No-op if not configured."""
    global _stream_thread

    if not _is_configured():
        print("   ℹ️  Twitter stream disabled (TWITTER_BEARER_TOKEN / TWITTER_ACCOUNTS not set)")
        return

    if _stream_thread and _stream_thread.is_alive():
        return  # already running

    _stream_thread = threading.Thread(target=_stream_worker, daemon=True, name="twitter-stream")
    _stream_thread.start()
    print("   🐦 Twitter stream thread started.")


def get_recent_tweets(hours_back: int = 4) -> List[dict]:
    """Return tweets from the cache that arrived within the last `hours_back` hours."""
    if not _is_configured():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    results = []
    with _cache_lock:
        for article in _signal_cache:
            try:
                ts_str = article.get("published_at", "")
                # Handle both Z and +00:00 suffixes
                ts_str = ts_str.replace("Z", "+00:00")
                ts = datetime.fromisoformat(ts_str)
                if ts >= cutoff:
                    results.append(article)
            except Exception:
                results.append(article)  # include if timestamp unparseable
    return results
