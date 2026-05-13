"""Wikidata SPARQL wrapper — structured facts for ~100M global entities.

Two-step lookup: Wikidata search API (name → Q-IDs), then SPARQL query
(Q-IDs → structured facts). No authentication required. User-Agent mandatory.
Responses cached via shelve. Wikidata is a corroborating source, never
authoritative — treat all results as MEDIUM confidence unless corroborated.
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

from cobalt.core.exceptions import WikidataError
from cobalt.core.search import _resolve_cache_dir

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://www.wikidata.org/w/api.php"
_SPARQL_URL  = "https://query.wikidata.org/sparql"
_DEFAULT_UA  = "Cobalt VendorIntelligence (vendorintel@example.com)"
_CACHE_FILE  = "wikidata_cache"

_COMPANY_FACTS_SPARQL = """\
SELECT ?item ?itemLabel ?inception ?employeeCount ?website
       ?countryLabel ?countryCode ?hqCityLabel ?industryLabel
       ?parentLabel ?legalFormLabel ?tickerSymbol
WHERE {{
  VALUES ?item {{ {qid_values} }}

  OPTIONAL {{ ?item wdt:P571 ?inception . }}
  OPTIONAL {{ ?item wdt:P1128 ?employeeCount . }}
  OPTIONAL {{ ?item wdt:P856 ?website . }}
  OPTIONAL {{ ?item wdt:P17 ?country .
              OPTIONAL {{ ?country wdt:P297 ?countryCode . }} }}
  OPTIONAL {{ ?item wdt:P159 ?hqCity . }}
  OPTIONAL {{ ?item wdt:P452 ?industry . }}
  OPTIONAL {{ ?item wdt:P749 ?parent . }}
  OPTIONAL {{ ?item wdt:P1454 ?legalForm . }}
  OPTIONAL {{ ?item wdt:P249 ?tickerSymbol . }}

  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . }}
}}"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _user_agent() -> str:
    return (
        os.environ.get("WIKIDATA_USER_AGENT")
        or os.environ.get("SEC_USER_AGENT")
        or _DEFAULT_UA
    )


def _http_get_json(url: str, params: dict, accept: str) -> dict | None:
    """GET with params, parse JSON. Returns None on 404.

    Raises WikidataError on 429, other HTTP errors, or transport failure.
    """
    qs = urllib.parse.urlencode(params, doseq=True)
    full_url = f"{url}?{qs}"
    req = urllib.request.Request(full_url, headers={
        "User-Agent":      _user_agent(),
        "Accept":          accept,
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
        if exc.code == 429:
            raise WikidataError("rate limit hit (429)") from exc
        raise WikidataError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise WikidataError(f"transport: {exc}") from exc


def _read_wd_cache(cache_dir: str, key: str):
    cache_path = str(Path(cache_dir) / _CACHE_FILE)
    try:
        with shelve.open(cache_path) as db:
            return db.get(key)
    except Exception:
        return None


def _write_wd_cache(cache_dir: str, key: str, value) -> None:
    cache_path = str(Path(cache_dir) / _CACHE_FILE)
    try:
        with shelve.open(cache_path) as db:
            db[key] = value
    except Exception:
        logger.warning("Failed to write Wikidata cache for key %r", key)


def _consolidate_sparql_bindings(data: dict, qids: list[str]) -> dict[str, dict]:
    """Coalesce multi-row SPARQL results (one row per industry/parent) into one
    dict per Q-ID.  Empty records (no label, no inception, no industries) are
    dropped."""
    out: dict[str, dict] = {qid: {
        "qid":           qid,
        "label":         "",
        "inception":     "",
        "employee_count": None,
        "website":       "",
        "country":       "",
        "country_code":  "",
        "hq_city":       "",
        "industries":    [],
        "parents":       [],
        "legal_form":    "",
        "ticker":        "",
    } for qid in qids}

    bindings = (data.get("results") or {}).get("bindings") or []

    for binding in bindings:
        item_uri = (binding.get("item") or {}).get("value", "")
        qid = item_uri.rsplit("/", 1)[-1] if item_uri else ""
        if qid not in out:
            continue

        record = out[qid]

        def _val(var: str) -> str | None:
            return (binding.get(var) or {}).get("value")

        record["label"] = record["label"] or _val("itemLabel") or ""

        inception = _val("inception")
        if inception and not record["inception"]:
            record["inception"] = inception.split("T")[0]

        emp = _val("employeeCount")
        if emp is not None and record["employee_count"] is None:
            try:
                record["employee_count"] = int(float(emp))
            except (ValueError, TypeError):
                pass

        record["website"]  = record["website"]  or _val("website")  or ""
        record["country"]  = record["country"]  or _val("countryLabel") or ""
        cc = _val("countryCode")
        if cc and not record["country_code"]:
            record["country_code"] = cc.upper()
        record["hq_city"]    = record["hq_city"]    or _val("hqCityLabel")    or ""
        record["legal_form"] = record["legal_form"] or _val("legalFormLabel") or ""
        record["ticker"]     = record["ticker"]     or _val("tickerSymbol")   or ""

        industry = _val("industryLabel")
        if industry and industry not in record["industries"]:
            record["industries"].append(industry)

        parent = _val("parentLabel")
        if parent and parent not in record["parents"]:
            record["parents"].append(parent)

    return {
        qid: rec for qid, rec in out.items()
        if rec["label"] or rec["inception"] or rec["industries"]
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def wikidata_search_entities(name: str, *, limit: int = 5) -> list[dict]:
    """Search Wikidata for entities matching name via the MediaWiki search API.

    Returns list of {"qid": str, "label": str, "description": str}.
    Returns [] on no results or fetch failure — does not raise.
    """
    target = (name or "").strip()
    if not target:
        return []

    cache_dir = _resolve_cache_dir()
    cache_key = f"wd-search-{target.lower()}-{limit}"
    if cache_dir:
        hit = _read_wd_cache(cache_dir, cache_key)
        if hit is not None:
            return hit

    try:
        data = _http_get_json(
            _SEARCH_URL,
            {
                "action":   "wbsearchentities",
                "search":   target,
                "language": "en",
                "type":     "item",
                "limit":    min(limit, 50),
                "format":   "json",
            },
            accept="application/json",
        )
    except WikidataError as exc:
        logger.warning("Wikidata search failed for %r: %s", target, exc)
        return []

    if not data:
        return []

    out = [
        {
            "qid":         item.get("id", ""),
            "label":       item.get("label", "") or "",
            "description": item.get("description", "") or "",
        }
        for item in (data.get("search") or [])[:limit]
    ]

    if cache_dir:
        _write_wd_cache(cache_dir, cache_key, out)
    return out


def wikidata_get_company_facts(qids: list[str]) -> dict[str, dict]:
    """Run SPARQL for the given Q-IDs and return structured facts per Q-ID.

    Returns {} on fetch failure — does not raise.
    """
    qids = [q for q in qids if q and q.startswith("Q")]
    if not qids:
        return {}

    cache_dir = _resolve_cache_dir()
    cache_key = f"wd-facts-{'-'.join(sorted(qids))}"
    if cache_dir:
        hit = _read_wd_cache(cache_dir, cache_key)
        if hit is not None:
            return hit

    qid_values = " ".join(f"wd:{q}" for q in qids)
    sparql = _COMPANY_FACTS_SPARQL.format(qid_values=qid_values)

    try:
        data = _http_get_json(
            _SPARQL_URL,
            {"query": sparql, "format": "json"},
            accept="application/sparql-results+json",
        )
    except WikidataError as exc:
        logger.warning("Wikidata SPARQL failed for %r: %s", qids, exc)
        return {}

    if not data:
        return {}

    out = _consolidate_sparql_bindings(data, qids)

    if cache_dir:
        _write_wd_cache(cache_dir, cache_key, out)
    return out


def wikidata_lookup_by_name(name: str, *, limit: int = 3) -> list[dict]:
    """Convenience: search + SPARQL facts in one call.

    Returns list of fact dicts in search-result order, each augmented
    with "qid", "label", "description" from the search step.
    Returns [] when no search results found (SPARQL not called).
    """
    candidates = wikidata_search_entities(name, limit=limit)
    if not candidates:
        return []

    qids = [c["qid"] for c in candidates]
    facts_by_qid = wikidata_get_company_facts(qids)

    out: list[dict] = []
    for c in candidates:
        facts = dict(facts_by_qid.get(c["qid"], {}))
        facts.setdefault("label",       c["label"])
        facts.setdefault("description", c["description"])
        facts["qid"] = c["qid"]
        out.append(facts)
    return out
