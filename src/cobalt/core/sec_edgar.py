"""SEC EDGAR API wrapper — US public company financial signals.

No authentication required. User-Agent header mandatory per EDGAR policy.
Tickers list cached in-memory (7-day TTL) and on disk. Submissions cached on disk.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shelve
import time
import urllib.error
import urllib.request
from pathlib import Path

from cobalt.core.exceptions import SecEdgarError
from cobalt.core.search import _resolve_cache_dir

logger = logging.getLogger(__name__)

_BASE_URL = "https://data.sec.gov"
_DEFAULT_UA = "Cobalt VendorIntelligence (vendorintel@example.com)"
_TICKERS_TTL_SECONDS = 7 * 24 * 3600
_TICKERS_CACHE_FILE = "sec_tickers_cache"
_SUBMISSIONS_CACHE_FILE = "sec_submissions_cache"

# In-memory ticker cache — avoids repeated disk reads within a process run
_tickers_cache: dict | None = None
_tickers_cache_time: float = 0.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _user_agent() -> str:
    return os.environ.get("SEC_USER_AGENT", _DEFAULT_UA)


def _get_json(path: str) -> dict:
    """GET path from EDGAR data API with mandatory User-Agent header.

    Returns parsed JSON dict.
    Raises SecEdgarError on 404, other HTTP errors, or transport failure.
    """
    url = f"{_BASE_URL}{path}"
    headers = {
        "User-Agent": _user_agent(),
        "Accept-Encoding": "gzip, deflate",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise SecEdgarError(f"not found: {path}") from exc
        raise SecEdgarError(f"HTTP {exc.code}: {path}") from exc
    except urllib.error.URLError as exc:
        raise SecEdgarError(f"transport: {exc}") from exc


# ---------------------------------------------------------------------------
# Tickers cache (in-memory + disk, 7-day TTL)
# ---------------------------------------------------------------------------

def _read_tickers_disk(cache_dir: str) -> tuple[dict | None, float]:
    cache_path = str(Path(cache_dir) / _TICKERS_CACHE_FILE)
    try:
        with shelve.open(cache_path) as db:
            return db.get("tickers"), float(db.get("ts", 0.0))
    except Exception:
        return None, 0.0


def _write_tickers_disk(cache_dir: str, data: dict) -> None:
    cache_path = str(Path(cache_dir) / _TICKERS_CACHE_FILE)
    try:
        with shelve.open(cache_path) as db:
            db["tickers"] = data
            db["ts"] = time.time()
    except Exception:
        pass


def _load_tickers() -> dict:
    global _tickers_cache, _tickers_cache_time

    now = time.time()
    if _tickers_cache is not None and (now - _tickers_cache_time) < _TICKERS_TTL_SECONDS:
        return _tickers_cache

    cache_dir = _resolve_cache_dir()
    Path(cache_dir).mkdir(parents=True, exist_ok=True)

    disk_data, disk_ts = _read_tickers_disk(cache_dir)
    if disk_data is not None and (now - disk_ts) < _TICKERS_TTL_SECONDS:
        _tickers_cache = disk_data
        _tickers_cache_time = disk_ts
        return _tickers_cache

    try:
        raw = _get_json("/files/company_tickers.json")
    except SecEdgarError:
        logger.warning("Failed to load SEC tickers list — returning empty")
        return {}

    _tickers_cache = raw
    _tickers_cache_time = now
    _write_tickers_disk(cache_dir, raw)
    return _tickers_cache


# ---------------------------------------------------------------------------
# Submissions cache (disk only — no TTL; updated infrequently)
# ---------------------------------------------------------------------------

def _read_submissions_disk(cache_dir: str, cik: str) -> dict | None:
    cache_path = str(Path(cache_dir) / _SUBMISSIONS_CACHE_FILE)
    key = hashlib.md5(f"sub-{cik}".encode()).hexdigest()
    try:
        with shelve.open(cache_path) as db:
            return db.get(key)
    except Exception:
        return None


def _write_submissions_disk(cache_dir: str, cik: str, data: dict) -> None:
    cache_path = str(Path(cache_dir) / _SUBMISSIONS_CACHE_FILE)
    key = hashlib.md5(f"sub-{cik}".encode()).hexdigest()
    try:
        with shelve.open(cache_path) as db:
            db[key] = data
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Name normalisation helpers
# ---------------------------------------------------------------------------

def _strip_corporate_suffixes(name: str) -> str:
    suffixes = (
        " incorporated", " inc.", " inc",
        " corporation", " corp.", " corp",
        " limited", " ltd.", " ltd",
        " llc", " lp", " l.p.", " plc",
        " holdings", " group",
        " international", " technologies", " technology",
        " co.", " co",
    )
    n = name.lower().strip()
    for s in suffixes:
        if n.endswith(s):
            n = n[: -len(s)]
            break
    return n.strip()


def _score_name_match(title: str, target: str) -> float:
    t_norm = _strip_corporate_suffixes(target)
    title_norm = _strip_corporate_suffixes(title)
    if title_norm == t_norm:
        return 1.0
    if t_norm in title_norm or title_norm in t_norm:
        return 0.7
    t_tokens = set(t_norm.split())
    title_tokens = set(title_norm.split())
    if t_tokens and title_tokens:
        return len(t_tokens & title_tokens) / len(t_tokens | title_tokens)
    return 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sec_search_by_name(name: str, *, limit: int = 5) -> list[dict]:
    """Search SEC tickers list for companies matching name.

    Returns list of dicts with keys: cik, ticker, title, score.
    Returns [] on API failure (graceful no-op — no raise).
    """
    tickers = _load_tickers()
    if not tickers:
        return []

    scored: list[tuple[float, dict]] = []
    for entry in tickers.values():
        title = entry.get("title", "")
        score = _score_name_match(title, name)
        if score >= 0.4:
            scored.append((score, {
                "cik":    str(entry.get("cik_str", "")).zfill(10),
                "ticker": entry.get("ticker", ""),
                "title":  title,
                "score":  score,
            }))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:limit]]


def sec_search_by_ticker(ticker: str) -> dict | None:
    """Look up a company by exact ticker symbol (case-insensitive).

    Returns dict with cik/ticker/title, or None if not found.
    """
    tickers = _load_tickers()
    ticker_upper = ticker.upper().strip()
    for entry in tickers.values():
        if entry.get("ticker", "").upper() == ticker_upper:
            return {
                "cik":    str(entry.get("cik_str", "")).zfill(10),
                "ticker": entry.get("ticker", ""),
                "title":  entry.get("title", ""),
            }
    return None


def sec_get_company_submissions(cik: str) -> dict | None:
    """Fetch company submissions record from EDGAR by CIK.

    Returns parsed dict, or None on 404.
    Raises SecEdgarError on non-404 HTTP errors or transport failure.
    Disk-cached (no TTL).
    """
    cik_padded = cik.zfill(10)
    cache_dir = _resolve_cache_dir()
    Path(cache_dir).mkdir(parents=True, exist_ok=True)

    cached = _read_submissions_disk(cache_dir, cik_padded)
    if cached is not None:
        return cached

    try:
        data = _get_json(f"/submissions/CIK{cik_padded}.json")
    except SecEdgarError as exc:
        if "not found" in str(exc):
            return None
        raise

    _write_submissions_disk(cache_dir, cik_padded, data)
    return data
