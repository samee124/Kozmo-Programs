"""OpenCorporates API wrapper — global business registry lookups (~140 jurisdictions).

Auth: api_token query parameter (not a header — unlike Companies House).
Free tier: 500 calls/month. No per-second rate limit published; cache aggressively.
Responses cached via shelve with no TTL (registry facts are stable).
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shelve
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cobalt.core.exceptions import OpenCorporatesError
from cobalt.core.search import _resolve_cache_dir

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.opencorporates.com/v0.4"
_DEFAULT_UA = "Cobalt VendorIntelligence (vendorintel@example.com)"
_CACHE_FILE = "opencorporates_cache"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _user_agent() -> str:
    return (
        os.environ.get("OPENCORPORATES_USER_AGENT")
        or os.environ.get("SEC_USER_AGENT")
        or _DEFAULT_UA
    )


def _get_json(path: str, params: dict) -> dict | None:
    """GET path with params; api_token injected automatically.

    Returns parsed dict. Returns None on 404.
    Raises OpenCorporatesError on auth failure, rate limit, or transport error.
    """
    token = os.environ.get("OPENCORPORATES_API_TOKEN", "").strip()
    if not token:
        raise OpenCorporatesError("OPENCORPORATES_API_TOKEN not set")

    params = dict(params)
    params["api_token"] = token

    url = f"{_BASE_URL}{path}?{urllib.parse.urlencode(params, doseq=True)}"
    req = urllib.request.Request(url, headers={
        "User-Agent":      _user_agent(),
        "Accept":          "application/json",
        "Accept-Encoding": "gzip",
    })

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        if exc.code in (401, 403):
            raise OpenCorporatesError(f"auth failed ({exc.code})") from exc
        if exc.code == 429:
            raise OpenCorporatesError("rate limit hit (429)") from exc
        raise OpenCorporatesError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise OpenCorporatesError(f"transport: {exc}") from exc


def _read_oc_cache(cache_dir: str, key: str):
    cache_path = str(Path(cache_dir) / _CACHE_FILE)
    try:
        with shelve.open(cache_path, flag="r") as db:
            return db.get(key)
    except Exception:
        return None


def _write_oc_cache(cache_dir: str, key: str, value) -> None:
    cache_path = str(Path(cache_dir) / _CACHE_FILE)
    try:
        with shelve.open(cache_path) as db:
            db[key] = value
    except Exception:
        logger.warning("Failed to write OpenCorporates cache for key %r", key)


def _normalise_company(c: dict) -> dict:
    addr = c.get("registered_address") or {}
    return {
        "name":               c.get("name", "") or "",
        "company_number":     c.get("company_number", "") or "",
        "jurisdiction_code":  c.get("jurisdiction_code", "") or "",
        "incorporation_date": c.get("incorporation_date", "") or "",
        "dissolution_date":   c.get("dissolution_date", "") or "",
        "company_type":       c.get("company_type", "") or "",
        "current_status":     c.get("current_status", "") or "",
        "inactive":           bool(c.get("inactive", False)),
        "registry_url":       c.get("registry_url", "") or "",
        "opencorporates_url": c.get("opencorporates_url", "") or "",
        "address": {
            "street_address": addr.get("street_address", "") or "",
            "locality":       addr.get("locality", "") or "",
            "region":         addr.get("region", "") or "",
            "postal_code":    addr.get("postal_code", "") or "",
            "country":        addr.get("country", "") or "",
        },
        "country": addr.get("country", "") or "",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def opencorporates_search(
    name: str,
    *,
    jurisdiction_code: str | None = None,
    limit: int = 5,
    include_inactive: bool = True,
) -> list[dict]:
    """Search OpenCorporates by company name, optionally scoped by jurisdiction.

    jurisdiction_code uses OC's lowercase convention: "gb", "de", "fr_paris",
    "us_de" (Delaware), etc.

    Returns list of normalised company dicts. Returns [] on no results,
    missing token, or fetch failure. Never raises at the public API layer.
    """
    target = (name or "").strip()
    if not target:
        return []
    if not os.environ.get("OPENCORPORATES_API_TOKEN", "").strip():
        return []

    cache_dir = _resolve_cache_dir()
    cache_key = (
        f"oc-search-{target.lower()}-{jurisdiction_code or 'any'}"
        f"-{limit}-{include_inactive}"
    )
    if cache_dir:
        hit = _read_oc_cache(cache_dir, cache_key)
        if hit is not None:
            return hit

    params: dict = {
        "q":        target,
        "per_page": min(limit, 30),
        "inactive": "true" if include_inactive else "false",
    }
    if jurisdiction_code:
        params["jurisdiction_code"] = jurisdiction_code

    try:
        data = _get_json("/companies/search", params)
    except OpenCorporatesError as exc:
        logger.warning("OpenCorporates search failed for %r: %s", target, exc)
        return []

    if not data:
        return []

    companies = ((data.get("results") or {}).get("companies") or [])[:limit]
    out = [_normalise_company(c.get("company") or {}) for c in companies]

    if cache_dir:
        _write_oc_cache(cache_dir, cache_key, out)
    return out
