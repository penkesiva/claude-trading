"""
tools/signal_queue.py
Thread-safe queue for actionable analyst signals extracted from Discord / Twitter.

Both discord_tools and twitter_tools push here.
stock_loop drains it on every 60-second scan.
"""

import threading
from collections import deque
from typing import List

# Holds up to 200 signals; oldest dropped when full
_queue: deque = deque(maxlen=200)
_lock  = threading.Lock()

# Track tickers already acted on this session to avoid double-entering
_acted: set = set()
_acted_lock = threading.Lock()


def push(signal: dict):
    """Add an analyst signal to the queue."""
    with _lock:
        _queue.appendleft(signal)


def drain() -> List[dict]:
    """Return all pending signals and clear the queue."""
    with _lock:
        items = list(_queue)
        _queue.clear()
    return items


def peek() -> List[dict]:
    """Return pending signals without clearing."""
    with _lock:
        return list(_queue)


def mark_acted(ticker: str):
    """Record that we already entered (or evaluated) this ticker from a signal today."""
    with _acted_lock:
        _acted.add(ticker.upper())


def already_acted(ticker: str) -> bool:
    """True if we've already acted on this ticker's signal today."""
    with _acted_lock:
        return ticker.upper() in _acted


def reset_daily():
    """Call at start of each session to clear acted-on tickers."""
    with _acted_lock:
        _acted.clear()
    with _lock:
        _queue.clear()
