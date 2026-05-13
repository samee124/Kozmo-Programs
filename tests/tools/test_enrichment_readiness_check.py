"""Tests for enrichment_readiness_check — Process 2 Tool 1."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from cobalt.core.exceptions import EnrichmentReadinessReadError
from cobalt.tools.enrichment_readiness_check import check_enrichment_readiness


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROGRAMME_ID = "prog-enrich"
VENDOR_ID    = "V-ACME-001"


def _vendor_dir(workspace: Path) -> Path:
    return workspace / PROGRAMME_ID / VENDOR_ID


def _entity_path(workspace: Path) -> Path:
    return _vendor_dir(workspace) / "identity" / "entity.md"


def _coverage_path(workspace: Path) -> Path:
    return _vendor_dir(workspace) / "cost_file" / "coverage.md"


def _spend_path(workspace: Path) -> Path:
    return _vendor_dir(workspace) / "cost_file" / "spend.md"


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True), encoding="utf-8")


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _call(workspace: Path, **kwargs) -> object:
    return check_enrichment_readiness(
        VENDOR_ID, PROGRAMME_ID, workspace_root=workspace, **kwargs
    )


# ---------------------------------------------------------------------------
# Identity confidence gate (tests 1–6)
# ---------------------------------------------------------------------------

def test_high_confidence_proceeds_at_declared_depth(tmp_path):
    """Test 1: confidence=0.85, status=ACTIVE → proceed=True, depth=declared."""
    _write_yaml(_entity_path(tmp_path), {"confidence": 0.85, "status": "ACTIVE", "flags": []})
    result = _call(tmp_path, declared_depth="STANDARD")
    assert result.proceed is True
    assert result.depth_tier == "STANDARD"
    assert result.skip is False


def test_medium_confidence_with_standard_depth_downgrades_to_basic(tmp_path):
    """Test 2: confidence=0.65, declared=STANDARD → BASIC + both downgrade flags."""
    _write_yaml(_entity_path(tmp_path), {"confidence": 0.65, "status": "ACTIVE", "flags": []})
    result = _call(tmp_path, declared_depth="STANDARD")
    assert result.proceed is True
    assert result.depth_tier == "BASIC"
    assert "LOW_IDENTITY_CONFIDENCE" in result.flags
    assert "DEPTH_DOWNGRADED" in result.flags


def test_medium_confidence_already_basic_no_downgrade_flag(tmp_path):
    """Test 3: confidence=0.65, declared=BASIC → BASIC + LOW flag, no DEPTH_DOWNGRADED."""
    _write_yaml(_entity_path(tmp_path), {"confidence": 0.65, "status": "ACTIVE", "flags": []})
    result = _call(tmp_path, declared_depth="BASIC")
    assert result.proceed is True
    assert result.depth_tier == "BASIC"
    assert "LOW_IDENTITY_CONFIDENCE" in result.flags
    assert "DEPTH_DOWNGRADED" not in result.flags


def test_low_confidence_produces_provisional_tier(tmp_path):
    """Test 4: confidence=0.45 → depth_tier=PROVISIONAL, LOW_IDENTITY_CONFIDENCE flag."""
    _write_yaml(_entity_path(tmp_path), {"confidence": 0.45, "status": "ACTIVE", "flags": []})
    result = _call(tmp_path)
    assert result.depth_tier == "PROVISIONAL"
    assert "LOW_IDENTITY_CONFIDENCE" in result.flags


def test_triage_required_status_blocks_enrichment(tmp_path):
    """Test 5: status=TRIAGE_REQUIRED → proceed=False, ENRICHMENT_BLOCKED flag."""
    _write_yaml(_entity_path(tmp_path), {"confidence": 0.90, "status": "TRIAGE_REQUIRED", "flags": []})
    result = _call(tmp_path)
    assert result.proceed is False
    assert "ENRICHMENT_BLOCKED" in result.flags


def test_unresolved_status_blocks_enrichment(tmp_path):
    """Test 6: status=UNRESOLVED → proceed=False, ENRICHMENT_BLOCKED flag."""
    _write_yaml(_entity_path(tmp_path), {"confidence": 0.90, "status": "UNRESOLVED", "flags": []})
    result = _call(tmp_path)
    assert result.proceed is False
    assert "ENRICHMENT_BLOCKED" in result.flags


# ---------------------------------------------------------------------------
# Depth tier upgrade (tests 7–8)
# ---------------------------------------------------------------------------

def test_class_a_data_class_upgrades_to_deep(tmp_path):
    """Test 7: confidence=0.90, data_class=CLASS_A → depth_tier=DEEP."""
    _write_yaml(_entity_path(tmp_path), {"confidence": 0.90, "status": "ACTIVE", "flags": []})
    _write_yaml(_coverage_path(tmp_path), {"data_class": "CLASS_A"})
    result = _call(tmp_path, declared_depth="STANDARD")
    assert result.proceed is True
    assert result.depth_tier == "DEEP"


def test_tier_1_spend_upgrades_to_deep(tmp_path):
    """Test 8: confidence=0.90, spend_tier=TIER_1 → depth_tier=DEEP."""
    _write_yaml(_entity_path(tmp_path), {"confidence": 0.90, "status": "ACTIVE", "flags": []})
    _write_yaml(_spend_path(tmp_path), {"total_spend_tier": "TIER_1"})
    result = _call(tmp_path, declared_depth="STANDARD")
    assert result.proceed is True
    assert result.depth_tier == "DEEP"


# ---------------------------------------------------------------------------
# Staleness check (tests 9–13)
# ---------------------------------------------------------------------------

def test_recent_enrichment_no_triggers_returns_skip(tmp_path):
    """Test 9: enriched 30 days ago, no triggers → skip=True, skip_reason mentions 90."""
    _write_yaml(_entity_path(tmp_path), {"confidence": 0.85, "status": "ACTIVE", "flags": []})
    _write_yaml(_coverage_path(tmp_path), {"last_enriched_at": _iso_days_ago(30)})
    result = _call(tmp_path)
    assert result.skip is True
    assert result.proceed is False
    assert "90" in (result.skip_reason or "")


def test_stale_enrichment_over_90_days_proceeds(tmp_path):
    """Test 10: enriched 100 days ago → skip=False, proceed=True."""
    _write_yaml(_entity_path(tmp_path), {"confidence": 0.85, "status": "ACTIVE", "flags": []})
    _write_yaml(_coverage_path(tmp_path), {"last_enriched_at": _iso_days_ago(100)})
    result = _call(tmp_path)
    assert result.skip is False
    assert result.proceed is True


def test_manual_override_bypasses_staleness(tmp_path):
    """Test 11: enriched 30 days ago, manual_override=True → skip=False."""
    _write_yaml(_entity_path(tmp_path), {"confidence": 0.85, "status": "ACTIVE", "flags": []})
    _write_yaml(_coverage_path(tmp_path), {"last_enriched_at": _iso_days_ago(30)})
    result = _call(tmp_path, manual_override=True)
    assert result.skip is False


def test_rebrand_trigger_overrides_staleness(tmp_path):
    """Test 12: enriched 30 days ago, triggers=[REBRAND_DETECTED] → skip=False."""
    _write_yaml(_entity_path(tmp_path), {"confidence": 0.85, "status": "ACTIVE", "flags": []})
    _write_yaml(_coverage_path(tmp_path), {"last_enriched_at": _iso_days_ago(30)})
    result = _call(tmp_path, triggers=["REBRAND_DETECTED"])
    assert result.skip is False


def test_missing_coverage_file_is_not_stale(tmp_path):
    """Test 13: coverage.md missing → skip=False, proceed=True."""
    _write_yaml(_entity_path(tmp_path), {"confidence": 0.85, "status": "ACTIVE", "flags": []})
    # No coverage.md written
    result = _call(tmp_path)
    assert result.skip is False
    assert result.proceed is True


# ---------------------------------------------------------------------------
# Internal fact review (tests 14–16)
# ---------------------------------------------------------------------------

def test_confirmed_fields_appear_in_known_facts_confirmed(tmp_path):
    """Test 14: category_confidence=CONFIRMED, domain_confidence=CONFIRMED → in confirmed."""
    _write_yaml(_entity_path(tmp_path), {
        "confidence": 0.85,
        "status": "ACTIVE",
        "flags": [],
        "category": "Technology",
        "category_confidence": "CONFIRMED",
        "domain": "acme.com",
        "domain_confidence": "CONFIRMED",
    })
    result = _call(tmp_path)
    assert "category" in result.known_facts.confirmed
    assert "domain" in result.known_facts.confirmed


def test_missing_field_appears_in_gaps(tmp_path):
    """Test 15: entity missing description → description in known_facts.gaps."""
    _write_yaml(_entity_path(tmp_path), {
        "confidence": 0.85,
        "status": "ACTIVE",
        "flags": [],
        "canonical_name": "Acme Corp",
        # description intentionally absent
    })
    result = _call(tmp_path)
    assert "description" in result.known_facts.gaps


def test_conflict_confidence_appears_in_conflicts(tmp_path):
    """Test 16: founding_year_confidence=CONFLICT → founding_year in known_facts.conflicts."""
    _write_yaml(_entity_path(tmp_path), {
        "confidence": 0.85,
        "status": "ACTIVE",
        "flags": [],
        "founding_year": 2001,
        "founding_year_confidence": "CONFLICT",
    })
    result = _call(tmp_path)
    assert "founding_year" in result.known_facts.conflicts
    assert "founding_year" not in result.known_facts.confirmed


# ---------------------------------------------------------------------------
# Source scope (tests 17–20)
# ---------------------------------------------------------------------------

def test_basic_tier_source_list_is_company_website_only(tmp_path):
    """Test 17: depth_tier=BASIC → source_list=["company_website"], query_count=1."""
    # category must be present so it doesn't fall into gaps and trigger web_search override
    _write_yaml(_entity_path(tmp_path), {
        "confidence": 0.85, "status": "ACTIVE", "flags": [],
        "category": "Technology",
    })
    result = _call(tmp_path, declared_depth="BASIC")
    assert result.depth_tier == "BASIC"
    assert result.source_list == ["company_website"]
    assert result.query_count == 1


def test_standard_tier_source_list_includes_web_linkedin_news(tmp_path):
    """Test 18: depth_tier=STANDARD → source_list has web_search + linkedin + news, count=2."""
    # category must be present so it doesn't add an extra query via the MISSING_CATEGORY override
    _write_yaml(_entity_path(tmp_path), {
        "confidence": 0.85, "status": "ACTIVE", "flags": [],
        "category": "Technology",
    })
    result = _call(tmp_path, declared_depth="STANDARD")
    assert result.depth_tier == "STANDARD"
    assert "web_search" in result.source_list
    assert "linkedin" in result.source_list
    assert "news" in result.source_list
    assert result.query_count == 2


def test_deep_tier_source_list_includes_registry_and_financial(tmp_path):
    """Test 19: depth_tier=DEEP → source_list includes registry + financial."""
    _write_yaml(_entity_path(tmp_path), {"confidence": 0.90, "status": "ACTIVE", "flags": []})
    result = _call(tmp_path, declared_depth="DEEP")
    assert result.depth_tier == "DEEP"
    assert "registry" in result.source_list
    assert "financial" in result.source_list


def test_missing_category_flag_adds_web_search_to_basic(tmp_path):
    """Test 20: MISSING_CATEGORY flag + BASIC tier → web_search added, query_count=2."""
    _write_yaml(_entity_path(tmp_path), {
        "confidence": 0.85,
        "status": "ACTIVE",
        "flags": ["MISSING_CATEGORY"],
    })
    result = _call(tmp_path, declared_depth="BASIC")
    assert result.depth_tier == "BASIC"
    assert "web_search" in result.source_list
    assert result.query_count == 2


# ---------------------------------------------------------------------------
# Edge cases (tests 21–22)
# ---------------------------------------------------------------------------

def test_missing_entity_md_returns_blocked_result(tmp_path):
    """Test 21: entity.md missing → proceed=False, flag=MISSING_ENTITY_FILE."""
    # No entity.md written — vendor directory may not even exist
    result = _call(tmp_path)
    assert result.proceed is False
    assert result.skip is False
    assert result.flags == ["MISSING_ENTITY_FILE"]


def test_malformed_entity_md_raises_read_error(tmp_path):
    """Test 22: entity.md present but malformed YAML → EnrichmentReadinessReadError."""
    entity_path = _entity_path(tmp_path)
    entity_path.parent.mkdir(parents=True, exist_ok=True)
    entity_path.write_text("{not: valid: yaml: :::}", encoding="utf-8")
    with pytest.raises(EnrichmentReadinessReadError):
        _call(tmp_path)


# ---------------------------------------------------------------------------
# Wikidata in tier source lists (tests 23–25 / global 42–44)
# ---------------------------------------------------------------------------

def test_standard_tier_includes_wikidata(tmp_path):
    """Test 23 (global 42): depth_tier=STANDARD → wikidata in source_list."""
    _write_yaml(_entity_path(tmp_path), {
        "confidence": 0.85, "status": "ACTIVE", "flags": [],
        "category": "Technology",
    })
    result = _call(tmp_path, declared_depth="STANDARD")
    assert "wikidata" in result.source_list


def test_deep_tier_includes_wikidata(tmp_path):
    """Test 24 (global 43): depth_tier=DEEP → wikidata in source_list."""
    _write_yaml(_entity_path(tmp_path), {
        "confidence": 0.90, "status": "ACTIVE", "flags": [],
    })
    result = _call(tmp_path, declared_depth="DEEP")
    assert "wikidata" in result.source_list


def test_basic_tier_excludes_wikidata(tmp_path):
    """Test 25 (global 44): depth_tier=BASIC → wikidata NOT in source_list."""
    _write_yaml(_entity_path(tmp_path), {
        "confidence": 0.85, "status": "ACTIVE", "flags": [],
        "category": "Technology",
    })
    result = _call(tmp_path, declared_depth="BASIC")
    assert "wikidata" not in result.source_list
