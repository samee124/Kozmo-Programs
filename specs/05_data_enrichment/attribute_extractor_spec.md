# attribute_extractor

## Overview

**Process:** Process 2 — Vendor Profile Enrichment
**Stages covered:** Stages 4–5 (Attribute Extraction + Classification)
**File:** `src/cobalt/tools/attribute_extractor.py`
**Role:** Extract structured facts and classify the vendor from raw evidence. Returns structured attributes with per-field confidence scores.
**Writes to workspace:** No — returns `ExtractedAttributes` in memory.
**Agent:** Analysis Agent.

---

## Purpose

This is where raw evidence becomes structured intelligence. The Analysis Agent reads the `SourceEvidenceBundle` from Tool 2 and produces a structured `ExtractedAttributes` dict with confidence per field.

Extraction and classification run in the same pass over the same evidence. The marketing language filter runs **before** all other skills, cleaning evidence before any extraction begins.

---

## Inputs

| Input | Source | Purpose |
|---|---|---|
| `SourceEvidenceBundle` | Tool 2 output | All raw evidence with provenance |
| `known_facts.confirmed` | From Tool 1 | Skip re-extraction of already-confirmed fields |
| `known_facts.gaps` | From Tool 1 | Prioritise extraction of missing fields |

---

## Skills

### 0. Marketing language filter (RUNS FIRST)

Applied to all text from vendor-owned sources before any extraction. Strips:

- Superlative claims: "world's leading", "industry-first", "best-in-class", "market leader"
- Vague amplifiers: "cutting-edge", "innovative", "revolutionary", "next-generation"
- Unverifiable statistics: "trusted by thousands of companies" without named source
- Forward-looking puffery: "we are redefining...", "our mission is to transform..."

Replaced with `[MARKETING_CLAIM_REMOVED]`. Raw evidence in `SourceEvidenceBundle` is not modified — filter applies to extraction working copy only.

### 1. Core identity attribute extraction

| Field | Type | Source priority |
|---|---|---|
| `website` | string | company_website → registry → linkedin |
| `description` | string (2-3 sentences) | company_website → linkedin → web_search |
| `hq_city` | string | registry → linkedin → company_website |
| `hq_country` | string (ISO 3166-1 alpha-2) | registry → linkedin → company_website |
| `founding_year` | integer | registry → linkedin → company_website |
| `company_status` | enum PUBLIC/PRIVATE/SUBSIDIARY/NON_PROFIT/UNKNOWN | registry → financial → company_website |

If sources conflict → record conflict, defer to Tool 5.

### 2. Size and maturity extraction

| Field | Bands |
|---|---|
| `employee_count_range` | 1-10 / 11-50 / 51-200 / 201-500 / 501-1000 / 1001-5000 / 5001-10000 / 10000+ |
| `revenue_range` | <$1M / $1M-$10M / $10M-$50M / $50M-$100M / $100M-$500M / $500M-$1B / $1B-$10B / $10B+ |
| `company_size_band` | STARTUP / SMB / MID_MARKET / ENTERPRISE |
| `funding_stage` | BOOTSTRAPPED / SEED / SERIES_A / SERIES_B / SERIES_C_PLUS / PE_BACKED / PUBLIC / UNKNOWN |

**Derivation rules for `company_size_band` from employee count:**
- 1-50 → STARTUP (unless funding/revenue signals indicate otherwise)
- 51-500 → SMB
- 501-5000 → MID_MARKET
- 5001+ → ENTERPRISE

If employee count and revenue signals conflict → flag `SIZE_SIGNALS_CONFLICT`. Inferred values tagged `INFERRED` with inference rule noted.

### 3. Ticker and public market extraction

For PUBLIC vendors:

| Field | Source |
|---|---|
| `ticker` | financial data, company website, news |
| `exchange` | NYSE / NASDAQ / LSE / TSX / ASX / OTHER |
| `market_cap_range` | financial data |

### 4. Vendor taxonomy classification

| Field | Description |
|---|---|
| `category` | IT_SOFTWARE / PROFESSIONAL_SERVICES / FACILITIES / LOGISTICS / MARKETING / FINANCE / HR / OTHER |
| `subcategory` | Second-level (e.g. IT_SOFTWARE → SAAS_PRODUCTIVITY / SAAS_SECURITY / SAAS_ANALYTICS) |
| `industry` | CROSS_INDUSTRY / FINANCIAL_SERVICES / HEALTHCARE / PUBLIC_SECTOR / RETAIL / OTHER |
| `vendor_type` | SAAS / SERVICES / HARDWARE / CONSULTING / MARKETPLACE / STAFFING / INFRASTRUCTURE / OTHER |

**Rules:**
- Category hint in `spend.md` is strong prior. Confirm or flag `CATEGORY_CONFLICT`.
- V1: single primary classification. Hybrid vendors: dominant category in `category`, others in `additional_categories`.
- Cannot determine → flag `MISSING_CATEGORY`. Never default to UNKNOWN without flagging.

### 5. Primary use case extraction

Short factual statement of what the vendor does from buyer perspective.

Examples:
- "Cloud infrastructure and hosting services"
- "HR information system and payroll processing"
- "Managed print and document services"

### 6. Product and service portfolio extraction

Top 5-8 primary offerings:

```json
{ "name": "Analytics Cloud", "type": "PRODUCT", "description": "..." }
```

Type values: PRODUCT / SERVICE / PLATFORM / MODULE / ADD_ON. Limited to verifiable items from evidence.

### 7. Competitor identification

**Does not infer competitors** — records only names explicitly referenced in evidence. Source-attributed.

### 8. Certification and credential extraction

| Type | Examples |
|---|---|
| Security | SOC 2 Type II, ISO 27001, FedRAMP, HITRUST, PCI DSS |
| Quality | ISO 9001, CMMI |
| Diversity | MBE, WBE, WOSB, SDVOSB, 8(a) |
| Partner status | AWS Partner, Microsoft Gold, Google Cloud Partner |
| Regulatory | HIPAA, GDPR, SOX |

Confidence: `SELF_REPORTED` (vendor's own site) vs `THIRD_PARTY_VERIFIED` (external source).

### 9. Public customer and segment detection

| Source | Confidence |
|---|---|
| Named customer on vendor's site | SELF_REPORTED — MEDIUM |
| Named customer in joint press release | THIRD_PARTY_CONFIRMED — HIGH |
| Customer segment ("Fortune 500") | CLAIMED — LOW |

### 10. Reputation and risk signal extraction

| Signal | Examples |
|---|---|
| LEGAL | Litigation, regulatory actions, consent decrees |
| FINANCIAL_RISK | Distress, downgrades, bankruptcy |
| DATA_SECURITY | Breaches, fines under data protection law |
| ETHICS | Executive misconduct, labour disputes |
| ESG | Environmental violations, supply chain issues |
| POSITIVE | Awards, major contract wins |

Records as found. No editorial judgement.

### 11. Conflict detection

```json
{
  "field": "employee_count_range",
  "source_a": { "value": "501-1000", "source_type": "linkedin" },
  "source_b": { "value": "51-200",   "source_type": "registry" },
  "resolution": "DEFERRED_TO_TOOL_5"
}
```

Conflicts not resolved here. Field gets `confidence: "CONFLICT"`.

---

## Confidence levels

| Level | Meaning |
|---|---|
| `HIGH` | 2+ independent sources agree, at least one OFFICIAL |
| `MEDIUM` | One reliable source, no conflicts |
| `LOW` | Single low-quality source, or conflict resolved by priority |
| `INFERRED` | Derived via documented rule |
| `CONFLICT` | Two sources disagree, deferred to Tool 5 |
| `MISSING` | No evidence found |

---

## Flags produced

| Flag | Condition |
|---|---|
| `MISSING_CATEGORY` | Category could not be determined |
| `CATEGORY_CONFLICT` | Evidence contradicts spend.md category_hint |
| `SIZE_SIGNALS_CONFLICT` | Employee count and revenue suggest different bands |
| `NO_DESCRIPTION_EXTRACTABLE` | No factual description after marketing filter |

---

## Tests required

- Marketing language stripped from working copy before extraction
- Original SourceEvidenceBundle unchanged after extraction
- Two sources agree → confidence=HIGH
- Two sources conflict → confidence=CONFLICT, conflict recorded
- Employee count 1500 + no revenue → company_size_band=MID_MARKET (INFERRED)
- Employee count 100 + revenue $1B → SIZE_SIGNALS_CONFLICT
- Category hint matches evidence → confirmed
- Category hint conflicts → CATEGORY_CONFLICT flag, both recorded
- Hybrid vendor (Amazon: cloud + retail) → primary + additional_categories
