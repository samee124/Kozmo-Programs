# external_source_collector

## Overview

**Process:** Process 2 — Vendor Profile Enrichment
**Stages covered:** Stages 2–3 (Source Collection + Source Validation)
**File:** `src/cobalt/tools/external_source_collector.py`
**Role:** Gather all raw evidence for the vendor from external sources. Returns structured evidence packages with full source attribution and provenance. **Nothing is extracted, classified, or interpreted here.**
**Writes to workspace:** No — returns `SourceEvidenceBundle` in memory.
**Agent:** Research Agent.

---

## Purpose

This tool is the sole evidence-gathering step in Process 2. It collects raw content from external sources, validates that each source refers to the correct vendor, and packages everything with attribution and provenance for downstream tools.

**The critical constraint is that nothing is interpreted here.** No fields are extracted. No classifications are made. The Analysis Agent does not run in this tool. The same evidence bundle can be reprocessed if extraction logic improves without re-running all external calls.

---

## Inputs

| Input | Source | Purpose |
|---|---|---|
| `EnrichmentReadinessResult` | Tool 1 output | Depth tier, source list, query count, known facts |
| `entity.md` | Vendor workspace | Canonical name, domain, hq_country, category_hint, aliases |

---

## Output

`SourceEvidenceBundle` — returned in memory, not persisted to workspace.

```json
{
  "vendor_id": "V-XXXX-001",
  "depth_tier": "STANDARD",
  "sources": {
    "company_website": [...],
    "linkedin": [...],
    "web_search": [...],
    "news": [...]
  },
  "disambiguation_notices": [],
  "collection_flags": []
}
```

---

## Skills

### 1. Web search

Executes targeted vendor searches using canonical name, known domain, and category hint. Query count = `depth_tier` from Tool 1.

**Query construction:**
- Primary: `"{canonical_name}" {category_hint}` — general company info
- Secondary (STANDARD+): `"{canonical_name}" {domain} company profile` — official listings
- Deep (DEEP): additional gap-targeted queries from `known_facts.gaps`

Results referencing a different company with the same name are flagged by entity disambiguation and excluded.

### 2. Company website fetch

Identifies the vendor's primary official website from `entity.md` domain or web search results. Fetches and packages:
- Homepage
- About page
- Products / Services page

**Validation before packaging:** Confirms website company name and description are consistent with vendor identity. Mismatched websites flagged `WRONG_ENTITY_RISK` and excluded as primary evidence.

### 3. LinkedIn company profile fetch

Retrieves raw LinkedIn company page content. Returns raw page content with full provenance. Does not extract structured fields. Validates LinkedIn entity name matches canonical name (allowing known aliases). Mismatch → `LINKEDIN_ENTITY_MISMATCH`.

**V1:** stub returning empty if no LinkedIn API configured.

### 4. Business registry lookup

**V2.1 scope:** Companies House (UK) + OpenCorporates (global aggregator covering ~140 jurisdictions). Cascade:

1. **UK vendors** → Companies House first (canonical UK source, authoritative)
2. **UK vendors with no Companies House match** → OpenCorporates as fallback (covers branches and foreign-registered UK entities)
3. **Non-UK vendors** → OpenCorporates directly
4. **No match anywhere** → `NO_REGISTRY_RECORD`

**Companies House** (`COMPANIES_HOUSE_API_KEY`): Free developer tier, 600 req/5 min. UK only.

**OpenCorporates** (`OPENCORPORATES_API_TOKEN`): Free 500 calls/month. Auth via `api_token` query parameter (not a header). Jurisdiction codes are lowercase — `gb`, `de`, `fr_paris`, `us_de` for Delaware. The wrapper passes the ISO country code in lowercase as the first attempt; if the jurisdiction-filtered search returns nothing, it retries unfiltered to handle non-ISO OC codes. Responses cached via shelve with no TTL (registry facts are stable).

Returns structured registry record with registry name, jurisdiction, retrieval URL. If no record → `NO_REGISTRY_RECORD`.
Multiple close matches → `REGISTRY_MULTIPLE_MATCHES` flag; best match still returned.
Transport/auth failure → `REGISTRY_FETCH_ERROR`.

### 5. Financial data collection

Retrieves available public financial signals from SEC EDGAR: ticker, CIK, entity type, SIC code, exchange listings, fiscal year end, incorporation state, filing category.

Unauthenticated. Requires `User-Agent` header (EDGAR policy). Tickers list cached in-memory (7-day TTL) and on disk. Submissions cached on disk with no TTL.

**V2.1 scope:** US-listed companies via SEC EDGAR only. Non-US primary markets (`CN`, `RU`, `BR`, `IN`, `JP`, `KR`) return `NO_PUBLIC_FINANCIAL_DATA` as a clean negative signal (not an error). Unknown/None `hq_country` → attempt EDGAR lookup.

Private companies with no SEC filings return `NO_PUBLIC_FINANCIAL_DATA` — not an error. Ambiguous low-confidence matches (score < 0.6) also return `NO_PUBLIC_FINANCIAL_DATA`. EDGAR HTTP/transport failure → `FINANCIAL_FETCH_ERROR`.

**V3:** Crunchbase, PitchBook for private company funding data.

### 6. News and signal collection

Retrieves recent news articles. Scoped signal types: ACQUISITION, REBRAND, EXECUTIVE_CHANGE, LEGAL, DATA_SECURITY, ESG, FINANCIAL_DISTRESS, POSITIVE.

Recency window: 24 months default. Extended to 5 years when investigating a lifecycle signal.

Marketing announcements with no signal content excluded before packaging.

### 7. Wikidata SPARQL lookup

Retrieves structured reference facts from Wikidata (~100M global entities). Two-step:
1. **Search API** (`wikidata.org/w/api.php`) — name → Q-IDs (up to 5 candidates)
2. **SPARQL** (`query.wikidata.org/sparql`) — Q-IDs → structured facts

Facts returned per entity: inception date, employee count, official website, country, country code, HQ city, industries (list), parent companies (list), legal form, ticker symbol.

**Module:** `cobalt.core.wikidata`. No authentication required. User-Agent mandatory. Responses cached via shelve (keyed by search term and by sorted QID list).

**Confidence:** MEDIUM unless corroborated. Wikidata is a reference database — treat as DIRECTORY quality, not OFFICIAL filing.

Multiple similar name matches → `WIKIDATA_MULTIPLE_MATCHES` flag; best match still returned. No Wikidata entity found → `NO_WIKIDATA_RECORD`. API failure → `WIKIDATA_FETCH_ERROR`.

SPARQL is skipped entirely when search returns no candidates.

**Tier scope:** STANDARD and DEEP only.

### 8. Source validation

Before packaging, confirms evidence refers to the correct vendor.

| Validation status | Meaning |
|---|---|
| `CONFIRMED` | Source clearly refers to correct vendor |
| `LIKELY` | Name and category match, minor discrepancies |
| `UNCERTAIN` | Possible match but missing corroborating signals |
| `REJECTED` | Source refers to a different entity — excluded |

### 9. Entity disambiguation

Triggers when more than one distinct company appears in results, or source validation rejects multiple results.

Returns disambiguation notice listing: identified companies, distinguishing signals, primary match selected. If unresolvable → `DISAMBIGUATION_REQUIRED`.

### 10. Source attribution

Every evidence item tagged with:

| Attribute | Values |
|---|---|
| `source_type` | WEB_SEARCH / COMPANY_WEBSITE / LINKEDIN / REGISTRY / FINANCIAL / NEWS / WIKIDATA |
| `source_url` | Full URL |
| `retrieved_at` | ISO 8601 timestamp |
| `validation_status` | CONFIRMED / LIKELY / UNCERTAIN |
| `quality_signal` | OFFICIAL / DIRECTORY / NEWS / SOCIAL |

### 11. Evidence packaging

Assembles all collected, validated content into `SourceEvidenceBundle` keyed by source type. No extraction. No field mapping. No interpretation.

---

## Flags produced

| Flag | Condition |
|---|---|
| `WRONG_ENTITY_RISK` | Website content does not match vendor identity |
| `LINKEDIN_ENTITY_MISMATCH` | LinkedIn page appears to be a different company |
| `NO_REGISTRY_RECORD` | No business registry match found |
| `NO_PUBLIC_FINANCIAL_DATA` | No public financial information available |
| `DISAMBIGUATION_REQUIRED` | Multiple companies found under same name |
| `NO_WIKIDATA_RECORD` | No Wikidata entity found for vendor name |
| `WIKIDATA_MULTIPLE_MATCHES` | Multiple similar entities found; best match selected |
| `WIKIDATA_FETCH_ERROR` | Wikidata API or transport failure |

---

## Routing

| Result | Next step |
|---|---|
| Bundle collected successfully | Pass to Tool 3 (`attribute_extractor`) |
| `DISAMBIGUATION_REQUIRED` | Pass bundle with flag; Tool 5 generates triage task |
| `WRONG_ENTITY_RISK` | Pass bundle with flag; Tool 5 classifies as PROVISIONAL |
| All sources return empty | Return `FAILED_COLLECTION` to Planning Agent |

---

## Tests required

- Brave mock returns 2 results → bundle has
- Website fetch with matching name → validation_status=CONFIRMED
- Website fetch with mismatch → WRONG_ENTITY_RISK flag, excluded as primary
- Multiple distinct companies in results → DISAMBIGUATION_REQUIRED
- News recency 30 months → excluded (default 24 month window)
- LinkedIn stub returns empty when not configured → bundle continues
- All sources return empty → FAILED_COLLECTION
