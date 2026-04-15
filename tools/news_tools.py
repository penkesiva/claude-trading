"""
tools/news_tools.py
News fetching for the stock day-trading agent.

Primary:  Alpaca Markets News API (no extra key — uses existing Alpaca credentials)
Secondary: NewsAPI.org (optional, set NEWSAPI_KEY in .env for richer tech coverage)
"""

import os
import requests
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

_ALPACA_DATA = "https://data.alpaca.markets"
_NEWSAPI_URL = "https://newsapi.org/v2/everything"


def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID":     config.ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
    }


def fetch_alpaca_news(
    symbols: Optional[List[str]] = None,
    hours_back: int = 12,
    limit: int = 30,
) -> list:
    """
    Fetch recent news from Alpaca's v1beta1 news endpoint.
    If symbols is given, filters to those tickers; otherwise fetches broad market news.
    """
    end   = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours_back)

    params = {
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end":   end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": min(limit, 50),
        "sort":  "desc",
    }
    if symbols:
        params["symbols"] = ",".join(s.upper().strip() for s in symbols)

    try:
        r = requests.get(
            f"{_ALPACA_DATA}/v1beta1/news",
            headers=_alpaca_headers(),
            params=params,
            timeout=10,
        )
        if not r.ok:
            return []
        return [
            {
                "source":       a.get("source", ""),
                "headline":     a.get("headline", ""),
                "summary":      a.get("summary", ""),
                "symbols":      a.get("symbols", []),
                "published_at": a.get("created_at", ""),
                "url":          a.get("url", ""),
            }
            for a in r.json().get("news", [])
        ]
    except Exception as e:
        print(f"   ⚠️  Alpaca news error: {e}")
        return []


def fetch_newsapi_tech(query: str = "stock market technology earnings", limit: int = 15) -> list:
    """
    Fetch tech/financial headlines from NewsAPI.org.
    Requires NEWSAPI_KEY in .env.  Returns empty list silently if no key.
    """
    api_key = getattr(config, "NEWSAPI_KEY", "") or ""
    if not api_key.strip():
        return []

    params = {
        "q":        query,
        "language": "en",
        "sortBy":   "publishedAt",
        "pageSize": min(limit, 30),
        "apiKey":   api_key.strip(),
    }
    try:
        r = requests.get(_NEWSAPI_URL, params=params, timeout=10)
        if not r.ok:
            return []
        return [
            {
                "source":       a.get("source", {}).get("name", ""),
                "headline":     a.get("title", ""),
                "summary":      (a.get("description") or "")[:200],
                "symbols":      [],
                "published_at": a.get("publishedAt", ""),
                "url":          a.get("url", ""),
            }
            for a in r.json().get("articles", [])
            if a.get("title")
        ]
    except Exception as e:
        print(f"   ⚠️  NewsAPI error: {e}")
        return []


def get_market_news(
    tickers: Optional[List[str]] = None,
    hours_back: int = 12,
) -> list:
    """
    Combined, deduplicated news from Alpaca + NewsAPI.
    Alpaca is used for ticker-specific articles; NewsAPI adds broad tech/macro coverage.
    """
    alpaca  = fetch_alpaca_news(symbols=tickers, hours_back=hours_back, limit=30)

    # Broad tech/macro headlines for morning scan context
    tech_query = "technology earnings government defense semiconductor AI"
    newsapi = fetch_newsapi_tech(query=tech_query, limit=15)

    seen: set = set()
    combined: list = []
    for article in alpaca + newsapi:
        key = (article.get("headline") or "")[:80].lower().strip()
        if key and key not in seen:
            seen.add(key)
            combined.append(article)

    return combined[:40]


def format_news_for_prompt(articles: list, max_articles: int = 20) -> str:
    """Compact text block suitable for inserting into Claude prompts."""
    if not articles:
        return "(no recent news available)"

    lines = []
    for a in articles[:max_articles]:
        ts  = (a.get("published_at") or "")[:16]
        syms = ", ".join(a.get("symbols", []))
        sym_str = f" [{syms}]" if syms else ""
        lines.append(f"[{ts}]{sym_str} {a.get('headline', '')}")
        summary = (a.get("summary") or "").strip()
        if summary:
            lines.append(f"  → {summary[:150]}")
    return "\n".join(lines)
