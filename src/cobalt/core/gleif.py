"""GLEIF LEI registry wrapper — authoritative parent/subsidiary relationships.

No authentication required. User-Agent header recommended per GLEIF policy.
JSON-API response format (application/vnd.api+json).
Responses cached via shelve. Parent 404s cached as sentinel to avoid re-fetching.
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

from cobalt.core.exceptions import GleifError
from cobalt.core.search import _resolve_cache_dir

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.gleif.org/api/v1"
_DEFAULT_UA = "Cobalt VendorIntelligence (vendorintel@example.com)"
_CACHE_FILE = "gleif_cache"
_NO_PARENT_SENTINEL = "__NO_PARENT__"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _user_agent() -> str:
    return (
        os.environ.get("GLEIF_USER_AGENT")
        or os.environ.get("SEC_USER_AGENT")
        or _DEFAULT_UA
    )


def _get_json(path: str, params: dict | None = None) -> dict | None:
    """GET path from GLEIF API, return parsed body.

    Returns None on 404 (clean absence signal, not an error).
    Raises GleifError on 429, other HTTP errors, or transport failure.
    """
    url = f"{_BASE_URL}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"

    req = urllib.request.Request(url, headers={
        "User-Agent":      _user_agent(),
        "Accept":          "application/vnd.api+json",
        "Accept-Encoding": "gzip",
    })

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        if exc.code == 429:
            raise GleifError("rate limit hit (429)") from exc
        raise GleifError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise GleifError(f"transport: {exc}") from exc


def _read_gleif_cache(cache_dir: str, key: str):
    cache_path = str(Path(cache_dir) / _CACHE_FILE)
    try:
        with shelve.open(cache_path) as db:
            return db.get(key)
    except Exception:
        return None


def _write_gleif_cache(cache_dir: str, key: str, value) -> None:
    cache_path = str(Path(cache_dir) / _CACHE_FILE)
    try:
        with shelve.open(cache_path) as db:
            db[key] = value
    except Exception:
        logger.warning("Failed to write GLEIF cache for key %r", key)


def _normalise_lei_record(data: dict) -> dict:
    """Convert a GLEIF lei-record resource object into our flat dict shape."""
    attrs = data.get("attributes", {}) or {}
    entity = attrs.get("entity", {}) or {}
    legal_address = entity.get("legalAddress", {}) or {}
    legal_name_obj = entity.get("legalName", {}) or {}
    other_names = entity.get("otherNames", []) or []
    relationships = data.get("relationships", {}) or {}

    return {
        "lei":            attrs.get("lei") or data.get("id", ""),
        "legal_name":     legal_name_obj.get("name", ""),
        "other_names":    [n.get("name", "") for n in other_names if isinstance(n, dict)],
        "country":        legal_address.get("country", ""),
        "jurisdiction":   entity.get("jurisdiction", ""),
        "status":         entity.get("status", ""),
        "legal_form_id":  (entity.get("legalForm") or {}).get("id", ""),
        "registered_as":  entity.get("registeredAs", ""),
        "creation_date":  entity.get("creationDate", ""),
        "has_direct_parent_link": bool(
            (relationships.get("direct-parent") or {}).get("links", {}).get("related")
        ),
        "has_ultimate_parent_link": bool(
            (relationships.get("ultimate-parent") or {}).get("links", {}).get("related")
        ),
    }


# ---------------------------------------------------------------------------
# Parent endpoint helper (shared by direct + ultimate)
# ---------------------------------------------------------------------------

def _fetch_parent_endpoint(lei: str, kind: str) -> dict | None:
    if not lei:
        return None

    cache_dir = _resolve_cache_dir()
    cache_key = f"gleif-{kind}-{lei}"

    if cache_dir:
        hit = _read_gleif_cache(cache_dir, cache_key)
        if hit is not None:
            if hit == _NO_PARENT_SENTINEL:
                return None
            return hit

    try:
        data = _get_json(f"/lei-records/{lei}/{kind}")
    except GleifError:
        raise

    if data is None:
        # 404 → no parent; cache sentinel to avoid re-fetching a known absence
        if cache_dir:
            _write_gleif_cache(cache_dir, cache_key, _NO_PARENT_SENTINEL)
        return None

    parent_record = data.get("data")
    if not parent_record:
        return None

    out = _normalise_lei_record(parent_record)
    if cache_dir:
        _write_gleif_cache(cache_dir, cache_key, out)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def gleif_search_by_name(name: str, *, limit: int = 5) -> list[dict]:
    """Search GLEIF by legal entity name.

    Returns list of normalised LEI record dicts.
    Returns [] on no results or fetch failure — does not raise.
    """
    target = (name or "").strip()
    if not target:
        return []

    cache_dir = _resolve_cache_dir()
    cache_key = f"gleif-search-{target.lower()}-{limit}"

    if cache_dir:
        hit = _read_gleif_cache(cache_dir, cache_key)
        if hit is not None:
            return hit

    try:
        data = _get_json("/lei-records", {
            "filter[entity.legalName]": target,
            "page[size]": min(limit, 50),
        })
    except GleifError as exc:
        logger.warning("GLEIF search failed for %r: %s", target, exc)
        return []

    if not data:
        return []

    records = data.get("data", []) or []
    out = [_normalise_lei_record(r) for r in records[:limit]]

    if cache_dir:
        _write_gleif_cache(cache_dir, cache_key, out)
    return out


def gleif_get_direct_parent(lei: str) -> dict | None:
    """Fetch direct parent's LEI record.

    Returns normalised dict, or None if no direct parent (404).
    Raises GleifError on transport failure.
    """
    return _fetch_parent_endpoint(lei, "direct-parent")


def gleif_get_ultimate_parent(lei: str) -> dict | None:
    """Fetch ultimate parent's LEI record.

    Returns normalised dict, or None if entity is its own ultimate parent (404).
    Raises GleifError on transport failure.
    """
    return _fetch_parent_endpoint(lei, "ultimate-parent")
