"""
tools/trump_tools.py
Truth Social / Trump post signals for the trading agent.

When APIFY_API_KEY is not set, functions return safe empty payloads so the
bot runs without this data source. Wire Apify (or another feed) here later.
"""

import os
from typing import Any, Dict

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _apify_configured() -> bool:
    return bool(os.getenv("APIFY_API_KEY", "").strip())


def get_overnight_trump_posts() -> Dict[str, Any]:
    """
    Posts from the last ~12 hours relevant to the morning scan.

    Returns:
        available: whether the feed was reachable / configured
        market_relevant: count of posts flagged as market-moving
        summary: one-line human summary for logs
        flagged_posts: list of {"text": str, ...} for prompt injection
    """
    if not _apify_configured():
        return {
            "available": False,
            "market_relevant": 0,
            "summary": "",
            "flagged_posts": [],
        }

    # Apify actor not wired in-repo yet; empty feed, no error branch in caller.
    return {
        "available": True,
        "market_relevant": 0,
        "summary": "No overnight posts fetched (Truth Social actor not wired)",
        "flagged_posts": [],
    }


def check_new_trump_posts() -> Dict[str, Any]:
    """
    Lightweight poll for new posts during the main loop.

    Returns:
        new_post: whether a new post appeared since last check
        impact: coarse label, e.g. "HIGH BEARISH" / "NEUTRAL"
        alert: short string for console
    """
    if not _apify_configured():
        return {"new_post": False, "impact": "NEUTRAL", "alert": ""}

    return {"new_post": False, "impact": "NEUTRAL", "alert": ""}
