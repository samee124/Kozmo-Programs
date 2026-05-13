"""Companies House API wrapper — UK business registry lookups.

Free developer tier: 600 requests / 5 minutes per key. One search +
one fetch per UK vendor = 2 calls. Cache layer absorbs repeats.

Auth: HTTP Basic with API key as username, empty password.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shelve
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cobalt.core.exceptions import CompaniesHouseError
from cobalt.core.search import _resolve_cache_dir

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.company-information.service.gov.uk"
_CACHE_FILE = "companies_house_cache"


# ---------------------------------------------------------------------------
# Cache helpers (separate namespace from brave search cache)
# ---------------------------------------------------------------------------

def _read_ch_cache(cache_dir: str, cache_key: str) -> dict | None:
    key = hashlib.md5(cache_key.encode()).hexdigest()
    cache_path = str(Path(cache_dir) / _CACHE_FILE)
    try:
        with shelve.open(cache_path) as db:
            return db.get(key)
    except Exception:
        return None


def _write_ch_cache(cache_dir: str, cache_key: str, data: dict) -> None:
    key = hashlib.md5(cache_key.encode()).hexdigest()
    cache_path = str(Path(cache_dir) / _CACHE_FILE)
    try:
        with shelve.open(cache_path) as db:
            db[key] = data
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Private HTTP helper
# ---------------------------------------------------------------------------

def _auth_header() -> dict[str, str]:
    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY", "")
    if not api_key:
        raise CompaniesHouseError("COMPANIES_HOUSE_API_KEY not set")
    token = base64.b64encode(f"{api_key}:".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _get_json(path: str, params: dict | None = None) -> dict | None:
    """GET path with auth, parse JSON. Returns None on 404.

    Raises CompaniesHouseError on auth failure, rate limit, or transport error.
    Results are cached per (path, params) to avoid duplicate API calls.
    """
    cache_dir = _resolve_cache_dir()
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    cache_key = f"ch-{path}-{json.dumps(params or {}, sort_keys=True)}"

    cached = _read_ch_cache(cache_dir, cache_key)
    if cached is not None:
        return cached

    url = f"{_BASE_URL}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    headers = _auth_header()
    headers["Accept"] = "application/json"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data: dict = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        if exc.code == 429:
            raise CompaniesHouseError("rate limit hit (429)") from exc
        if exc.code in (401, 403):
            raise CompaniesHouseError(f"auth failed ({exc.code})") from exc
        raise CompaniesHouseError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise CompaniesHouseError(f"transport: {exc}") from exc

    _write_ch_cache(cache_dir, cache_key, data)
    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def companies_house_search(name: str, *, limit: int = 5) -> list[dict]:
    """Search Companies House by company name.

    Returns [] if no matches or no API key (graceful no-op).
    Raises CompaniesHouseError on transport/auth failure (after logging).
    """
    if not os.environ.get("COMPANIES_HOUSE_API_KEY"):
        return []
    try:
        data = _get_json("/search/companies", {"q": name, "items_per_page": min(limit, 100)})
    except CompaniesHouseError as exc:
        logger.warning("Companies House search failed for %r: %s", name, exc)
        return []
    if not data:
        return []
    items = data.get("items", []) or []
    out: list[dict] = []
    for item in items[:limit]:
        out.append({
            "title":           item.get("title", ""),
            "company_number":  item.get("company_number", ""),
            "company_status":  item.get("company_status", ""),
            "company_type":    item.get("company_type", ""),
            "date_of_creation": item.get("date_of_creation", ""),
            "address_snippet": item.get("address_snippet", ""),
            "links_self":      (item.get("links") or {}).get("self", ""),
        })
    return out


def companies_house_get_company(company_number: str) -> dict | None:
    """Fetch full company record by registration number.

    Returns None on 404. Raises CompaniesHouseError on other failures.
    """
    if not os.environ.get("COMPANIES_HOUSE_API_KEY"):
        return None
    try:
        data = _get_json(f"/company/{company_number}")
    except CompaniesHouseError as exc:
        logger.warning("Companies House get %r failed: %s", company_number, exc)
        return None
    return data
