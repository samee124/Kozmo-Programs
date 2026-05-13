# enriched_profile_creator

## Overview

**Process:** Process 2 — Vendor Profile Enrichment
**Stages covered:** Stages 8–10 (Attribute Reconciliation + Canonical Profile Creation + Confidence/Gaps/Triage)
**File:** `src/cobalt/tools/enriched_profile_creator.py`
**Role:** Reconcile all extracted and mapped data, resolve field-level conflicts, write canonical `vendor_profile.md` to workspace, score confidence, identify gaps, generate flags, produce triage tasks.
**Writes to workspace:** Yes — **the only tool in Process 2 that writes to vendor workspace.**
**Agent:** Vendor Manager Agent orchestrates write.

---

## Purpose

The commit step for Process 2. Takes all upstream outputs and produces one authoritative `vendor_profile.md` written atomically to the vendor workspace.

Write only happens after full reconciliation. If assembly fails or profile is below minimum quality threshold, prior profile (if any) is preserved and a `FAILED_ENRICHMENT` record is written instead.

---

## Inputs

| Input | Source | Purpose |
|---|---|---|
| `ExtractedAttributes` | Tool 3 | All extracted fields with per-field confidence and conflict records |
| `RelationshipMap` | Tool 4 | Parent, subsidiaries, brands, former names |
| `LifecycleSignals` | Tool 4 | Rebrand, acquisition, defunct events |
| `BrainUpdateSuggestions` | Tool 4 | Passed through to Program Orchestration Agent |
| `EnrichmentReadinessResult` | Tool 1 | Depth tier, sources used, known facts for metadata |
| Existing workspace | `vendor_profile.md` (if present) | Prior profile preserved if write fails |

---

## Output

**Primary:** `vendor_profile.md` written to vendor workspace.
**Secondary:** `coverage.md` updated, DB row synced, PCS recomputed, triage tasks where applicable.

### vendor_profile.md structure

```json
{
  "vendor_id": "V-XXXX-001",
  "canonical_name": "Anthology Inc.",
  "profile_status": "ENRICHED",
  "overall_confidence": "HIGH",
  "enriched_at": "2025-10-01T14:32:00Z",
  "identity": { "website": {...}, "description": {...}, ... },
  "classification": { "category": {...}, "subcategory": {...}, ... },
  "size": { "employee_count_range": {...}, ... },
  "organisation": {
    "parent_company": {...},
    "subsidiaries": [],
    "brands": [],
    "former_names": [...]
  },
  "products_and_services": [],
  "competitors": [],
  "certifications": [],
  "customer_segments": [],
  "reputation_signals": [],
  "lifecycle_signals": [...],
  "gaps": { "blocking": [], "enrichment": [...] },
  "flags": [...],
  "enrichment_metadata": {
    "depth_tier": "STANDARD",
    "sources_used": [...],
    "pcs_before": 0.24,
    "pcs_after": 0.76
  }
}
```

---

## Skills

### 1. Source priority reconciliation

| Priority | Source type | Quality |
|---|---|---|
| 1 | Official company website (own domain, validated) | OFFICIAL |
| 2 | Business registry (government source) | OFFICIAL |
| 3 | LinkedIn company page | SOCIAL |
| 4 | Financial data sources | DIRECTORY |
| 5 | News sources | NEWS |
| 6 | Web directory listings | DIRECTORY |

Within same tier: more recently retrieved wins.

**Outcomes:**
- Resolvable by priority → resolved field value, losing source noted
- Two equal-priority sources from different jurisdictions disagree → `UNRESOLVED_CONFLICT`, triage task

### 2. Null tolerance

Fields with no reliable evidence stored as explicit `null` with gap flag. Values never fabricated.

**V1 inference rules:**

| Inference | Rule | Tag |
|---|---|---|
| `company_size_band` from `employee_count_range` | When revenue absent, derive from employee count | INFERRED |
| `hq_country` from `hq_city` | When city unambiguously maps to one country | INFERRED |

Inferred values tagged `INFERRED`. Score MEDIUM at most — never HIGH.

### 3. Multi-field confidence scoring

| Level | Meaning |
|---|---|
| `HIGH` | 2+ independent sources agree, at least one OFFICIAL |
| `MEDIUM` | One reliable source, no conflicts |
| `LOW` | Single low-quality source, or significant disagreement |
| `INFERRED` | Derived via documented rule |
| `MISSING` | No evidence — stored as null |

### 4. Overall profile confidence

**Core fields:** `category`, `subcategory`, `hq_country`, `description`, `company_status`, `vendor_type`, `company_size_band`

| Classification | Conditions |
|---|---|
| `HIGH` | All core fields at MEDIUM+ confidence; no unresolved conflicts; no WRONG_ENTITY_RISK |
| `MEDIUM` | All core fields present; some LOW; or one missing |
| `LOW` | One+ core fields missing; significant conflicts |
| `PROVISIONAL` | Identity confidence < 0.60 at Tool 1; OR PROVISIONAL tier; OR WRONG_ENTITY_RISK |

### 5. Gap identification

**BLOCKING_GAP** — core field missing, blocks downstream use:

| Field | Downstream impact |
|---|---|
| `category` | Cannot classify for spend analysis |
| `hq_country` | Cannot assign geography |
| `description` | Cannot summarise for CPO reporting |
| `company_status` | Compliance cannot determine requirements |

Blocking gaps → `PARTIALLY_ENRICHED` or `PROVISIONAL` — never `ENRICHED`.

**ENRICHMENT_GAP** — non-core, reduces richness:
- competitors, certifications, customer_segments, revenue_range, funding_stage, products_and_services

Enrichment gaps do not affect status.

### 6. Flag generation

| Flag | Trigger |
|---|---|
| `NO_DIGITAL_PRESENCE` | No website, no LinkedIn, no registry |
| `WRONG_ENTITY_RISK` | Source validation warnings from Tool 2 |
| `ACQUISITION_UNRESOLVED` | Acquisition signals but acquirer unconfirmed |
| `CONFLICTING_DESCRIPTION` | Two sources describe vendor materially differently |
| `POSSIBLY_DEFUNCT` | Carried from Tool 4 |
| `SINGLE_SOURCE_ONLY` | All data from one source |
| `PARTIAL_PROFILE` | One+ enrichment gaps |
| `MISSING_CATEGORY` | Category undetermined |
| `MISSING_HQ` | HQ country undetermined |
| `LOW_IDENTITY_CONFIDENCE` | Carried from Tool 1 |
| `LIFECYCLE_EVENT_DETECTED` | Lifecycle signals from Tool 4 |
| `BRAIN_UPDATE_PENDING` | Brain update suggestions from Tool 4 |

### 7. Profile status classification

| Status | Conditions |
|---|---|
| `ENRICHED` | All core fields at MEDIUM+ confidence; no blocking gaps; no WRONG_ENTITY_RISK |
| `PARTIALLY_ENRICHED` | Core fields present but enrichment gaps; or one core field LOW |
| `PROVISIONAL` | Blocking gaps present; OR low identity confidence at intake; OR WRONG_ENTITY_RISK |
| `FAILED_ENRICHMENT` | No core fields could be populated; prior profile preserved |

### 8. Triage task generation

```json
{
  "vendor_id": "V-XXXX-001",
  "canonical_name": "ABC Solutions",
  "triage_type": "ENTITY_DISAMBIGUATION",
  "question": "Confirm whether this is 'ABC Solutions Ltd (UK)' or 'ABC Solutions Inc (US)'.",
  "evidence": "Web search returned two distinct companies. Address from invoice is US, domain registered in UK.",
  "downstream_impact": "Cannot assign geography until confirmed.",
  "suggested_action": "Check invoice billing address or contact vendor.",
  "created_at": "2025-10-01T14:32:00Z"
}
```

**Triage types:**

| Type | Trigger |
|---|---|
| `ENTITY_DISAMBIGUATION` | DISAMBIGUATION_REQUIRED or WRONG_ENTITY_RISK |
| `BLOCKING_GAP_RESOLUTION` | Blocking gaps remain after reconciliation |
| `LIFECYCLE_CONFIRMATION` | POSSIBLY_DEFUNCT or ACQUISITION_UNRESOLVED |
| `UNRESOLVED_CONFLICT` | Source priority could not resolve core field conflict |
| `WRONG_ENTITY_CONFIRMATION` | WRONG_ENTITY_RISK — human must confirm or reject |

### 9. Workspace write

**Atomic.** File only updated if full assembly is valid. If assembly fails, prior profile preserved and `FAILED_ENRICHMENT` record written.

After write, appends enrichment ledger entry to `coverage.md`:

```json
{
  "enriched_at": "2025-10-01T14:32:00Z",
  "depth_tier": "STANDARD",
  "profile_status": "ENRICHED",
  "overall_confidence": "HIGH",
  "sources_used": [...],
  "flags": [...],
  "pcs_before": 0.24,
  "pcs_after": 0.76
}
```

Syncs DB row with: `category`, `data_class`, `profile_status`, `last_enriched_at`.

### 10. PCS update

**Process 2 PCS contributions:**

| Field | Weight |
|---|---|
| `category` | 0.10 |
| `hq_country` | 0.06 |
| `description` | 0.06 |
| `company_status` | 0.05 |
| `vendor_type` | 0.05 |
| `company_size_band` | 0.04 |
| `parent_company` resolved | 0.04 |
| `lifecycle_signals` evaluated | 0.03 |
| `certifications` present | 0.02 |
| `reputation_signals` evaluated | 0.02 |

**Total Process 2 max contribution: 0.47**

PCS before/after recorded in ledger entry. Returned to Planning Agent.

---

## Routing

| Result | Next step |
|---|---|
| `ENRICHED` | Vendor advances to Process 3 |
| `PARTIALLY_ENRICHED` | Advances to Process 3 with gaps noted |
| `PROVISIONAL` | Triage tasks generated; held at Process 2 pending resolution |
| `FAILED_ENRICHMENT` | Triage task; cannot advance until human review |
| `BRAIN_UPDATE_PENDING` | Suggestions forwarded to Program Orchestration Agent |

---

## Tests required

- All core fields present at HIGH confidence → status=ENRICHED, overall=HIGH
- One core field MISSING → status=PARTIALLY_ENRICHED, BLOCKING_GAP recorded
- WRONG_ENTITY_RISK active → status=PROVISIONAL regardless of fields
- LOW_IDENTITY_CONFIDENCE at intake → status=PROVISIONAL
- Two LinkedIn sources conflict → resolved by recency
- LinkedIn vs registry conflict → registry wins (priority 2 vs 3)
- Two registries (US + EU) conflict → UNRESOLVED_CONFLICT, triage task
- Atomic write failure → prior vendor_profile.md preserved
- PCS before=0.20, after enrichment with all core fields → after ≥ 0.50
- Brain update suggestions passed through unchanged
- POSSIBLY_DEFUNCT → LIFECYCLE_CONFIRMATION triage task
