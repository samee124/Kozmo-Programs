# enrichment_readiness_check

## Overview

**Process:** Process 2 — Vendor Profile Enrichment
**Stages covered:** Stage 1
**File:** `src/cobalt/tools/enrichment_readiness_check.py`
**Role:** Gate before enrichment starts. Determines whether enrichment should run, how deep, and what is already known internally — before any external source is touched.
**Writes to workspace:** No — reads workspace only.
**Agent:** Vendor Manager Agent triggers; Planning Agent evaluates output.

---

## Purpose

Prevents redundant enrichment runs. Enforces identity confidence requirements before external calls are made. Produces the depth and source-scope decision that all downstream tools depend on.

**No external network calls are made by this tool.** All inputs come from the vendor workspace.

---

## Inputs

| File | Fields read | Purpose |
|---|---|---|
| `entity.md` | `confidence`, `status`, `canonical_name`, `domain`, `hq_country`, `entity_type`, `flags` | Identity confidence gate and known-facts review |
| `coverage.md` | `last_enriched_at`, `profile_status`, `data_class`, `populated_fields`, `pcs` | Staleness check and gap identification |
| `spend.md` | `total_spend_tier`, `category_hint`, `contract_type_hints` | Depth tier decision and source scope |

---

## Output

Returns `EnrichmentReadinessResult` in memory. Not persisted to workspace.

```json
{
  "vendor_id": "V-XXXX-001",
  "proceed": true,
  "skip": false,
  "skip_reason": null,
  "depth_tier": "STANDARD",
  "source_list": ["web_search", "company_website", "linkedin"],
  "query_count": 2,
  "known_facts": {
    "confirmed": ["canonical_name", "domain", "hq_country"],
    "gaps": ["category", "company_size_band", "description"],
    "conflicts": []
  },
  "confidence_floor": 0.72,
  "flags": []
}
```

---

## Skills

### 1. Identity confidence gate

Reads `confidence` from `entity.md`:

| Confidence | Action |
|---|---|
| ≥ 0.80 | Proceed at declared depth tier |
| 0.60 – 0.79 | Proceed at BASIC depth regardless of declared tier |
| < 0.60 | Mark PROVISIONAL, restrict to shallow enrichment, flag `LOW_IDENTITY_CONFIDENCE` |
| Status = `TRIAGE_REQUIRED` or `UNRESOLVED` | Block enrichment entirely, return `ENRICHMENT_BLOCKED` |

### 2. Enrichment depth decision

| Tier | Trigger | Web queries | Sources |
|---|---|---|---|
| `BASIC` | Well-known, Brain-confirmed, light refresh | 1 | Website only |
| `STANDARD` | Confirmed identity, no profile or stale profile | 2 | Website + LinkedIn |
| `DEEP` | Strategic vendor, high spend, data_class upgrade requested | 3+ | All sources |
| `PROVISIONAL` | Identity confidence < 0.60 | 1 | Web only, results flagged |

### 3. Staleness check

Reads `last_enriched_at`. Returns `SKIP` if enrichment was within 90 days AND no trigger event has occurred.

**Trigger events that override staleness:**
- Lifecycle signal detected in a new source (rebrand, acquisition)
- New source intake has facts conflicting with existing profile
- Spend tier crossed a threshold since last enrichment
- `data_class` upgrade requested by Vendor Manager Agent
- Manual override flag set

### 4. Internal fact review

Builds `KnownFacts` record:
- **Confirmed:** fields populated at MEDIUM or HIGH confidence → skip in Tool 3 extraction
- **Gaps:** missing or LOW confidence → prioritise in Tool 2 search queries
- **Conflicted:** present but flagged → re-fetch and re-evaluate

### 5. Source scope determination

| Source | BASIC | STANDARD | DEEP | PROVISIONAL |
|---|---|---|---|---|
| Web search | 1 query | 2 queries | 3+ queries | 1 query |
| Company website | Yes | Yes | Yes | Yes |
| LinkedIn | No | Yes | Yes | No |
| Business registry | No | No | Yes | No |
| Financial / funding | No | No | Yes | No |
| News and signals | No | Yes | Yes | No |
| Wikidata SPARQL | No | Yes | Yes | No |

**Override triggers:**
- `MISSING_CATEGORY` flag → add taxonomy-focused query regardless of depth tier
- `POSSIBLY_DEFUNCT` flag → add registry and news check regardless of depth tier

---

## Flags produced

| Flag | Condition |
|---|---|
| `LOW_IDENTITY_CONFIDENCE` | Entity confidence between 0.60 and 0.79 |
| `ENRICHMENT_BLOCKED` | Entity status is TRIAGE_REQUIRED or UNRESOLVED |
| `SKIP` | Last enrichment within 90 days and no trigger event |
| `DEPTH_DOWNGRADED` | Declared tier was reduced due to low confidence |

---

## Routing

| Result | Next step |
|---|---|
| `proceed: true` | Pass `EnrichmentReadinessResult` to Tool 2 (`external_source_collector`) |
| `skip: true` | Return to Planning Agent — no further enrichment tools run |
| `ENRICHMENT_BLOCKED` | Surface to Vendor Manager Agent — resolve identity issues in Process 1 first |

---

## Tests required

- Confidence 0.85 → STANDARD depth proceeds
- Confidence 0.65 → depth downgraded to BASIC + LOW_IDENTITY_CONFIDENCE flag
- Confidence 0.45 → PROVISIONAL tier
- Status TRIAGE_REQUIRED → ENRICHMENT_BLOCKED
- last_enriched_at 30 days ago, no trigger → SKIP
- last_enriched_at 30 days ago + rebrand trigger → proceed
- DEEP tier + MISSING_CATEGORY → registry source added
- POSSIBLY_DEFUNCT flag → registry + news added regardless of tier
- known_facts correctly classifies confirmed/gaps/conflicts
