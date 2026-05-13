"""Shared search module — Brave Search API wrapper + URL fetcher.

Used by both Process 1 (research_agent) and Process 2 (external_source_collector).
Single source of truth for all external search and URL-fetch logic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shelve
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from cobalt.core.exceptions import BraveSearchError

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_DIR = "runtime/cache/search_cache"
_BRAVE_WEB_ENDPOINT  = "https://api.search.brave.com/res/v1/web/search"
_BRAVE_NEWS_ENDPOINT = "https://api.search.brave.com/res/v1/news/search"
_FETCH_TIMEOUT = 10

_SOCIAL_PATTERNS = frozenset({
    "linkedin.com", "twitter.com", "facebook.com", "instagram.com",
    "x.com", "threads.net", "mastodon.social",
})
_NEWS_PATTERNS = frozenset({
    "reuters.com", "bloomberg.com", "techcrunch.com", "wsj.com",
    "ft.com", "forbes.com", "businesswire.com", "prnewswire.com",
    "cnbc.com", "businessinsider.com", "marketwatch.com", "axios.com",
    "theverge.com",
})
_DIRECTORY_PATTERNS = frozenset({
    "crunchbase.com", "zoominfo.com", "dnb.com", "pitchbook.com",
    "hoovers.com", "owler.com", "clearbit.com", "apollo.io",
    "builtin.com", "glassdoor.com", "wikipedia.org",
})


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _resolve_cache_dir() -> str:
    return os.environ.get("SEARCH_CACHE_DIR") or _DEFAULT_CACHE_DIR


def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def _http_get_json(url: str, params: dict, headers: dict, timeout: int = 10) -> str:
    """Fetch JSON from url with query params and headers. Returns response body as str."""
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full_url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_url_quality(url: str) -> str:
    """Returns OFFICIAL / DIRECTORY / NEWS / SOCIAL."""
    url_lower = url.lower()
    if any(p in url_lower for p in _SOCIAL_PATTERNS):
        return "SOCIAL"
    if any(p in url_lower for p in _NEWS_PATTERNS):
        return "NEWS"
    if any(p in url_lower for p in _DIRECTORY_PATTERNS):
        return "DIRECTORY"
    return "OFFICIAL"


def brave_search(
    query: str,
    *,
    result_type: str = "web",
    count: int = 10,
    freshness: str | None = None,
) -> list[dict]:
    """Call Brave Search API.

    Returns normalised list of {"url", "content", "title", "published_date"}.
    'content' is the Brave description snippet (short).

    Raises BraveSearchError on missing key, HTTP error, or transport failure.
    """
    api_key = os.environ.get("BRAVE_API_KEY")
    if not api_key:
        raise BraveSearchError("BRAVE_API_KEY not set")

    cache_dir = _resolve_cache_dir()
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    cache_path = str(Path(cache_dir) / "cache")
    cache_key = hashlib.md5(
        f"brave-{result_type}-{count}-{freshness or 'none'}-{query}".encode()
    ).hexdigest()

    try:
        with shelve.open(cache_path) as db:
            if cache_key in db:
                return db[cache_key]  # type: ignore[return-value]
    except Exception:
        pass

    endpoint = _BRAVE_NEWS_ENDPOINT if result_type == "news" else _BRAVE_WEB_ENDPOINT
    params: dict[str, Any] = {"q": query, "count": min(count, 20)}
    if freshness:
        params["freshness"] = freshness

    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": api_key,
    }

    try:
        response_text = _http_get_json(endpoint, params, headers, timeout=_FETCH_TIMEOUT)
        data: dict = json.loads(response_text)
    except urllib.error.HTTPError as exc:
        raise BraveSearchError(f"Brave API HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise BraveSearchError(f"Brave API transport error: {exc}") from exc

    results_key = "news" if result_type == "news" else "web"
    raw_results: list[dict] = (data.get(results_key) or {}).get("results", []) or []

    normalised = [
        {
            "url":            r.get("url", ""),
            "content":        r.get("description", "") or "",
            "title":          r.get("title", "") or "",
            "published_date": r.get("page_age") or r.get("age") or None,
        }
        for r in raw_results[:count]
    ]

    try:
        with shelve.open(cache_path) as db:
            db[cache_key] = normalised
    except Exception:
        pass

    logger.info("[Brave] %d results for %r (type=%s)", len(normalised), query[:60], result_type)
    return normalised


def fetch_url(url: str, *, timeout: int = 10) -> tuple[str, int]:
    """Fetch a URL via urllib. Returns (text, status_code).

    text is HTML-stripped, whitespace-collapsed, capped at 5000 chars.
    Returns ("", 0) on any network error. Never raises.
    """
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Cobalt/1.0; +https://cobalt.ai)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            html = resp.read().decode("utf-8", errors="replace")
        text = _strip_html(html)
        text = " ".join(text.split())[:5000]
        return text, status
    except Exception:
        return "", 0


def search_with_content(
    query: str,
    *,
    result_type: str = "web",
    count: int = 5,
    freshness: str | None = None,
    fetch_content: bool = True,
) -> list[dict]:
    """Brave Search + optional per-result page content fetch.

    Returns list of {"url", "content", "title", "published_date", "full_content"}.
    full_content is populated via fetch_url() when fetch_content=True.

    Caches the full result set (Brave + fetched content) under one cache key.
    Raises BraveSearchError if Brave API auth fails.
    """
    cache_dir = _resolve_cache_dir()
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    cache_path = str(Path(cache_dir) / "cache")
    cache_key = hashlib.md5(
        f"search-content-{result_type}-{count}-{freshness or 'none'}-{int(fetch_content)}-{query}".encode()
    ).hexdigest()

    try:
        with shelve.open(cache_path) as db:
            if cache_key in db:
                return db[cache_key]  # type: ignore[return-value]
    except Exception:
        pass

    results = brave_search(query, result_type=result_type, count=count, freshness=freshness)

    enriched: list[dict] = []
    for r in results:
        full_content = ""
        if fetch_content and r.get("url"):
            full_content, _ = fetch_url(r["url"])
        enriched.append({
            "url":            r["url"],
            "content":        r["content"],
            "title":          r["title"],
            "published_date": r["published_date"],
            "full_content":   full_content,
        })

    try:
        with shelve.open(cache_path) as db:
            db[cache_key] = enriched
    except Exception:
        pass

    return enriched
