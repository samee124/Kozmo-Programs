# relationship_and_lifecycle_mapper

## Overview

**Process:** Process 2 — Vendor Profile Enrichment
**Stages covered:** Stages 6–7 (Entity Relationship Mapping + Lifecycle Signal Detection)
**File:** `src/cobalt/tools/relationship_and_lifecycle_mapper.py`
**Role:** Map how this vendor relates to other entities and detect lifecycle events (rebrand, acquisition, merger, defunct). Suggests Brain updates when new events are discovered.
**Writes to workspace:** No — returns maps and signals in memory. Generates Brain update suggestions only; does not commit them.
**Agent:** Analysis Agent + Brain access.

---

## Purpose

Answers two coupled questions in one pass:
1. How does this vendor relate to other entities?
2. Has anything important changed about this vendor?

Relationship and lifecycle draw on the same sources and are structurally coupled — a rebrand changes the relationship map, an acquisition creates a new parent.

**Key constraint:** Suggests Brain updates but does not commit them. Suggestions reviewed by Program Orchestration Agent before persisting.

---

## Inputs

| Input | Source | Purpose |
|---|---|---|
| `SourceEvidenceBundle` | Tool 2 output | Web, news, registry evidence |
| Brain: `rebrand_map` | Brain | Check known rebrands first |
| Brain: `acquisition_map` | Brain | Check known acquisitions first |
| Brain: `alias_dictionary` | Brain | Former names already recorded |
| Brain: `brand_map` | Brain | Known brand-to-legal-entity mappings |

---

## Output

`RelationshipMap` + `LifecycleSignals` list + `BrainUpdateSuggestions` list.

```json
{
  "vendor_id": "V-XXXX-001",
  "relationship_map": {
    "parent_company": {
      "name": "Alphabet Inc.",
      "vendor_id": "V-ALPHABET-001",
      "relationship_type": "WHOLLY_OWNED",
      "confidence": "HIGH"
    },
    "subsidiaries": [],
    "brands": [],
    "former_names": []
  },
  "lifecycle_signals": [
    {
      "signal_type": "REBRANDED",
      "from": "Blackboard Inc.",
      "to": "Anthology Inc.",
      "date": "2021",
      "confidence": "HIGH",
      "source": "registry",
      "brain_update_required": false
    }
  ],
  "brain_update_suggestions": []
}
```

---

## Skills

### 1. Parent company identification

Sources checked (in order):

1. Brain `acquisition_map` (highest priority — known acquisitions, HIGH confidence)
2. GLEIF LEI registry (authoritative legal-entity parent relationships, V2.1)
3. Company website (About / Legal / Investor pages)
4. Business registry data (already covered via Tool 2 REGISTRY items)
5. LinkedIn (V1 stub)
6. News coverage of acquisitions (already covered via Tool 4 acquisition detection)

| Field | Values |
|---|---|
| `parent_company_name` | Canonical name |
| `parent_company_id` | Brain ID if known |
| `relationship_type` | WHOLLY_OWNED / MAJORITY_OWNED / PORTFOLIO_COMPANY / FRANCHISOR / UNKNOWN |
| `confidence` | HIGH / MEDIUM / LOW |

**GLEIF (V2.1):** The Global Legal Entity Identifier Foundation maintains an authoritative registry of ~2.5M regulated entities globally. Every LEI record includes direct-parent and ultimate-parent relationships where they exist. Coverage is best for financial services, banking, insurance, and large multinationals; weaker for small private companies. A GLEIF hit gives HIGH confidence parent resolution without requiring website scraping or Brain pre-seeding. GLEIF returns `relationship_type="WHOLLY_OWNED"` by default in V2.1 because the LEI registry does not distinguish ownership percentages. V3 may parse the relationship metadata for ownership share when available.

V1: direct parent only. V2: full hierarchy.

If no parent → `parent_company: null` (explicit null, not missing).

### 2. Subsidiary mapping

Extracts named subsidiaries explicitly referenced in evidence. Not exhaustive — no inference.

### 3. Brand-to-company resolution

Brain brand_map checked first. New mappings → Brain update suggestion.

Examples: Instagram → Meta Platforms Inc, Slack → Salesforce Inc, Google Cloud → Google LLC.

### 4. Former name mapping

| Field | Values |
|---|---|
| `former_name` | Previous company name |
| `transitioned_from` | Approximate date/year |
| `transition_type` | REBRAND / POST_ACQUISITION / POST_MERGER |

Former names → flagged for Brain alias_dictionary update.

### 5. Rebrand detection

**Check order:**
1. Brain `rebrand_map`
2. Web evidence: name change news, "formerly known as", LinkedIn history

| Field | Description |
|---|---|
| `rebranded` | true / false |
| `rebrand_from` | Former name |
| `rebrand_to` | New canonical name |
| `rebrand_date` | Year/date |
| `rebrand_confidence` | HIGH (Brain/official) / MEDIUM (news only) / LOW (inferred) |

New rebrand not in Brain → REBRAND_MAP update suggestion.

### 6. Acquisition detection

**Check order:**
1. Brain `acquisition_map`
2. News evidence

| Field | Description |
|---|---|
| `acquired` | true / false |
| `acquired_by` | Acquiring entity |
| `acquisition_date` | Year/date |
| `acquisition_status` | COMPLETED / ANNOUNCED / RUMOURED |

**Direction matters:**
- This vendor was acquired → parent_company updated
- This vendor acquired another → subsidiary recorded

New acquisition not in Brain → ACQUISITION_MAP update suggestion.

### 7. Merger detection

| Field | Description |
|---|---|
| `merged` | true / false |
| `merged_from` | Predecessor entities |
| `merger_date` | Year/date |
| `merger_type` | EQUALS_MERGER / ABSORPTION |

Predecessor names preserved as aliases.

### 8. Defunct detection

| Signal | Weight |
|---|---|
| Website 404 / parked | Strong |
| No news in 24 months | Moderate |
| Registry dissolved/struck off/inactive | Strong |
| LinkedIn "no longer exists" | Moderate |
| No contact response | Weak |

Returns `POSSIBLY_DEFUNCT` flag — never definitive `DEFUNCT`. Definitive requires human review (Tool 5 triage task).

### 9. Spin-off detection

| Field | Description |
|---|---|
| `spun_off` | true / false |
| `spun_off_from` | Former parent |
| `spinoff_date` | Year/date |

Former parent recorded as `FORMER_PARENT_SPUN_OFF`, not current parent.

### 10. Lifecycle signal generation

Assembles all detected events into `LifecycleSignals` list.

**Signal types:**

| Signal | Meaning |
|---|---|
| `REBRANDED` | Name change |
| `ACQUIRED` | Was acquired |
| `MERGED` | Formed from/involved in merger |
| `POSSIBLY_DEFUNCT` | No recent activity |
| `SPUN_OFF` | Spun from parent |
| `WENT_PUBLIC` | IPO |
| `WENT_PRIVATE` | Taken private |
| `PARENT_CHANGED` | Ultimate parent changed without direct acquisition |

### 11. Brain update suggestion

```json
{
  "update_type": "REBRAND_MAP",
  "from": "Blackboard Inc.",
  "to": "Anthology Inc.",
  "confidence": "HIGH",
  "source_url": "https://anthology.com/about",
  "suggested_by_vendor_id": "V-ANTHOLOGY-001",
  "review_required": true
}
```

Update types: REBRAND_MAP / ACQUISITION_MAP / ALIAS_DICTIONARY / BRAND_MAP.

**Tool does not write to Brain.** Suggestions returned in output.

---

## Flags produced

| Flag | Condition |
|---|---|
| `POSSIBLY_DEFUNCT` | No recent activity across multiple sources |
| `ACQUISITION_UNRESOLVED` | Acquisition signals but acquiring entity not confirmed |
| `LIFECYCLE_EVENT_DETECTED` | One or more lifecycle signals found |
| `BRAIN_UPDATE_PENDING` | One or more Brain update suggestions generated |
| `PARENT_CHANGED` | Parent differs from previously recorded |

---

## Routing

| Result | Next step |
|---|---|
| Maps + signals produced | Pass to Tool 5 alongside `ExtractedAttributes` |
| `BRAIN_UPDATE_PENDING` | Suggestions also passed to Program Orchestration Agent |
| `POSSIBLY_DEFUNCT` | Tool 5 generates triage task |

---

## Tests required

- Brain rebrand_map hit → REBRANDED signal, no Brain update needed
- Web evidence rebrand (not in Brain) → REBRANDED signal + REBRAND_MAP update suggestion
- Brain acquisition_map hit → ACQUIRED signal, parent updated
- News-only acquisition → ACQUIRED with MEDIUM confidence + ACQUISITION_MAP suggestion
- Brain brand_map: "instagram" → parent="meta platforms inc"
- New brand discovered → BRAND_MAP update suggestion
- Defunct signals (website 404 + no news 24mo + registry dissolved) → POSSIBLY_DEFUNCT flag
- This vendor acquired another → subsidiary recorded, not parent_company
- This vendor was acquired → parent_company updated, not subsidiary
