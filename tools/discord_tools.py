"""
tools/discord_tools.py
Discord channel message poller for trading signals.

Polls configured channel(s) every 60 seconds via Discord REST API.
Tracks last-seen message ID per channel so only new messages are fetched.

Auth options (in priority order):
  1. Email + Password  — set DISCORD_EMAIL and DISCORD_PASSWORD in .env
                         The module logs in at startup to obtain a session token.
  2. Pre-existing token — set DISCORD_TOKEN in .env directly (skip login step).

WARNING: Using personal Discord credentials (self-botting) violates Discord's
Terms of Service (https://discord.com/terms). You risk account suspension.
A Discord Bot Token is the compliant alternative.

Requires:
  DISCORD_EMAIL       — your Discord account email  (used if DISCORD_TOKEN not set)
  DISCORD_PASSWORD    — your Discord account password
  DISCORD_CHANNEL_IDS — comma-separated numeric channel IDs
                        (Discord > Settings > Advanced > Developer Mode ON,
                         then right-click channel > Copy Channel ID)
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import re
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

import requests

import config

# ── Constants ─────────────────────────────────────────────
_BASE_URL        = "https://discord.com/api/v9"
_LOGIN_URL       = f"{_BASE_URL}/auth/login"
_POLL_SECS       = 60    # how often to check each channel
_MSG_LIMIT       = 50    # messages per poll (max 100)
_TOKEN_RETRY_MIN = 30    # minutes between re-auth attempts on token expiry

# Thread-safe signal store
_signal_cache: list = []
_cache_lock = threading.Lock()

# Per-channel cursor: channel_id → last seen snowflake message ID
_last_seen: Dict[str, str] = {}

# Runtime token (may be obtained via login or from config directly)
_runtime_token: Optional[str] = None
_token_lock = threading.Lock()

_poller_thread: threading.Thread | None = None


# ── Auth ──────────────────────────────────────────────────

def _login_with_credentials() -> Optional[str]:
    """
    Log in with DISCORD_EMAIL + DISCORD_PASSWORD.
    Returns the user token string, or None on failure.

    Note: If the account has 2FA enabled, Discord returns a ticket instead of
    a token and this function will return None — disable 2FA or use a pre-set
    DISCORD_TOKEN instead.
    """
    email    = (getattr(config, "DISCORD_EMAIL",    "") or "").strip()
    password = (getattr(config, "DISCORD_PASSWORD", "") or "").strip()

    if not email or not password:
        return None

    payload = {
        "login":    email,
        "password": password,
        "undelete": False,
        "captcha_key": None,
        "login_source": None,
        "gift_code_sku_id": None,
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }

    try:
        r = requests.post(_LOGIN_URL, json=payload, headers=headers, timeout=15)
        data = r.json()

        if r.status_code == 200 and "token" in data:
            token = data["token"]
            print(f"   ✅ Discord: logged in as {email[:email.index('@')]}***")
            return token

        if "mfa" in data or data.get("totp"):
            print(
                "   ❌ Discord: account has 2FA enabled — "
                "set DISCORD_TOKEN manually in .env instead."
            )
            return None

        err = data.get("message") or data.get("errors") or r.text[:200]
        print(f"   ❌ Discord login failed: {err}")
        return None

    except Exception as e:
        print(f"   ❌ Discord login error: {e}")
        return None


def _get_token() -> Optional[str]:
    """
    Returns the active token, trying (in order):
      1. Already-cached runtime token
      2. DISCORD_TOKEN from config (.env)
      3. Login with DISCORD_EMAIL + DISCORD_PASSWORD
    """
    global _runtime_token

    with _token_lock:
        if _runtime_token:
            return _runtime_token

        # Prefer a pre-set token if provided
        static_token = (getattr(config, "DISCORD_TOKEN", "") or "").strip()
        if static_token:
            _runtime_token = static_token
            return _runtime_token

        # Fall back to email/password login
        token = _login_with_credentials()
        if token:
            _runtime_token = token
        return _runtime_token


def _clear_token():
    """Force a re-login on the next API call (e.g. after a 401)."""
    global _runtime_token
    with _token_lock:
        _runtime_token = None


def _auth_headers() -> dict:
    token = _get_token() or ""
    if token.startswith("Bot ") or token.startswith("Bearer "):
        return {"Authorization": token}
    return {"Authorization": token}


def _is_configured() -> bool:
    channels = getattr(config, "DISCORD_CHANNEL_IDS", []) or []
    if not channels:
        return False
    # Need either a static token OR email+password credentials
    has_token = bool((getattr(config, "DISCORD_TOKEN", "") or "").strip())
    has_creds = bool(
        (getattr(config, "DISCORD_EMAIL",    "") or "").strip() and
        (getattr(config, "DISCORD_PASSWORD", "") or "").strip()
    )
    return has_token or has_creds


# ── Message parser ────────────────────────────────────────

def _parse_message(msg: dict) -> Optional[dict]:
    """Convert a Discord message object into the standard article schema."""
    content   = (msg.get("content") or "").strip()
    msg_id    = msg.get("id", "")
    timestamp = msg.get("timestamp", datetime.now(timezone.utc).isoformat())
    author    = (msg.get("author") or {}).get("username", "unknown")

    # Also capture embeds (linked articles, rich previews)
    embeds = msg.get("embeds") or []
    embed_text = " | ".join(
        e.get("title") or e.get("description") or ""
        for e in embeds
        if e.get("title") or e.get("description")
    )

    full_text = f"{content} {embed_text}".strip()
    if not full_text or len(full_text) < 4:
        return None

    # Extract ticker mentions: $TICKER or standalone 2-5 uppercase letters
    from prompts.stock_system_prompt import STOCK_UNIVERSE
    raw_tokens = re.findall(r'\$([A-Z]{1,5})|(?<!\w)([A-Z]{2,5})(?!\w)', full_text)
    tickers = list({
        t for group in raw_tokens for t in group
        if t and t in STOCK_UNIVERSE
    })

    return {
        "source":       "discord",
        "headline":     f"[{author}] {full_text[:200]}",
        "summary":      embed_text[:150] if embed_text else "",
        "symbols":      tickers,
        "published_at": timestamp,
        "url":          f"https://discord.com/channels/@me/{msg_id}",
        "msg_id":       msg_id,
    }


# ── Channel poller ────────────────────────────────────────

def _fetch_channel_messages(channel_id: str) -> list:
    """Fetch new messages from one channel since last seen ID."""
    params: dict = {"limit": _MSG_LIMIT}
    after = _last_seen.get(channel_id)
    if after:
        params["after"] = after

    try:
        r = requests.get(
            f"{_BASE_URL}/channels/{channel_id}/messages",
            headers=_auth_headers(),
            params=params,
            timeout=10,
        )

        if r.status_code == 401:
            print("   ⚠️  Discord: token expired — re-authenticating…")
            _clear_token()
            # Retry once with fresh token
            r = requests.get(
                f"{_BASE_URL}/channels/{channel_id}/messages",
                headers=_auth_headers(),
                params=params,
                timeout=10,
            )

        if r.status_code == 403:
            print(f"   ⚠️  Discord: no access to channel {channel_id} (403 Forbidden)")
            return []
        if r.status_code == 401:
            print(f"   ❌ Discord: still unauthorized after re-auth — check credentials")
            return []
        if not r.ok:
            print(f"   ⚠️  Discord: channel {channel_id} HTTP {r.status_code}")
            return []

        messages = r.json()
        return messages if isinstance(messages, list) else []

    except Exception as e:
        print(f"   ⚠️  Discord: fetch error for channel {channel_id}: {e}")
        return []


def _poll_once():
    """Poll all configured channels once and append new signals to cache."""
    from tools.signal_parser import parse_signal
    from tools.signal_queue  import push as push_signal

    channel_ids = getattr(config, "DISCORD_CHANNEL_IDS", []) or []
    new_articles = []

    for channel_id in channel_ids:
        messages = _fetch_channel_messages(channel_id)
        if not messages:
            continue

        # Discord returns newest-first; update cursor to the most recent message ID
        newest_id = messages[0].get("id")
        if newest_id:
            _last_seen[channel_id] = newest_id

        for msg in messages:
            article = _parse_message(msg)
            if article:
                new_articles.append(article)
                if article["symbols"]:
                    print(
                        f"   💬 Discord [{', '.join(article['symbols'])}]: "
                        f"{article['headline'][:80]}…"
                    )
                # Try to extract an actionable call signal and push to queue
                sig = parse_signal(article)
                if sig:
                    push_signal(sig)
                    print(
                        f"   📣 Signal [{sig['source_label']}] → {sig['ticker']} call "
                        f"(conf {sig['confidence']:.0%}): {sig['raw_text'][:60]}…"
                    )

    if new_articles:
        with _cache_lock:
            _signal_cache[:0] = new_articles
            del _signal_cache[200:]


# ── Poller worker thread ──────────────────────────────────

def _poller_worker():
    print(f"   💬 Discord poller running — checking every {_POLL_SECS}s")
    while True:
        try:
            _poll_once()
        except Exception as e:
            print(f"   ⚠️  Discord poller error: {e}")
        time.sleep(_POLL_SECS)


# ── Public API ────────────────────────────────────────────

def start_discord_poller():
    """Start the background polling thread. No-op if not configured."""
    global _poller_thread

    if not _is_configured():
        print(
            "   ℹ️  Discord poller disabled "
            "(set DISCORD_TOKEN or DISCORD_EMAIL+DISCORD_PASSWORD, and DISCORD_CHANNEL_IDS)"
        )
        return

    if _poller_thread and _poller_thread.is_alive():
        return

    # Authenticate eagerly so we fail fast before market open
    token = _get_token()
    if not token:
        print("   ❌ Discord: could not obtain token — poller not started")
        return

    # Immediate first fetch so morning scan has data
    try:
        _poll_once()
    except Exception:
        pass

    _poller_thread = threading.Thread(target=_poller_worker, daemon=True, name="discord-poller")
    _poller_thread.start()
    print("   💬 Discord poller thread started.")


def get_recent_discord_signals(hours_back: int = 4) -> List[dict]:
    """Return Discord messages from the cache within the last `hours_back` hours."""
    if not _is_configured():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    results = []
    with _cache_lock:
        for article in _signal_cache:
            try:
                ts_str = (article.get("published_at") or "").replace("Z", "+00:00")
                ts = datetime.fromisoformat(ts_str)
                if ts >= cutoff:
                    results.append(article)
            except Exception:
                results.append(article)
    return results
