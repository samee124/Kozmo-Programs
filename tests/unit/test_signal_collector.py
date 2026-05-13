"""Tests for cobalt.intake._signal_collector."""

from decimal import Decimal

import pytest

from cobalt.brain.loader import BrainData, invalidate_cache, load_brain
from cobalt.intake._signal_collector import (
    brain_lookup,
    collect_signals,
    dedup_check,
    detect_script,
)
from cobalt.models.schemas.signal_profile_schema import (
    ApSignal,
    BatchContext,
    EntityType,
    ErpSignal,
    ScriptType,
)
from cobalt.tools.candidate_screening import entity_type_detect


@pytest.fixture(autouse=True)
def _clear_brain_cache():
    invalidate_cache()
    yield
    invalidate_cache()


@pytest.fixture
def brain():
    return load_brain()


@pytest.fixture
def empty_ctx(brain):
    return BatchContext(
        known_vendors=brain.known_vendors,
        rebrand_map=brain.rebrand_map,
        alias_map=brain.alias_map,
        erp_hits={},
        ap_counts={},
        all_keys=[],
        doc_registry={},
        candidate_hints={},
    )


# ---------------------------------------------------------------------------
# detect_script
# ---------------------------------------------------------------------------

def test_detect_script_latin():
    assert detect_script("IBM") == ScriptType.LATIN

def test_detect_script_cjk():
    assert detect_script("株式会社ソニー") == ScriptType.CJK

def test_detect_script_cyrillic():
    assert detect_script("Сбербанк") == ScriptType.CYRILLIC

def test_detect_script_arabic():
    assert detect_script("مايكروسوفت") == ScriptType.ARABIC


# ---------------------------------------------------------------------------
# brain_lookup
# ---------------------------------------------------------------------------

def test_brain_lookup_known_vendor_ibm(brain):
    hit = brain_lookup("ibm", brain)
    assert hit.matched is True
    assert hit.match_type == "KNOWN_VENDOR"
    assert hit.confidence == 0.99

def test_brain_lookup_rebrand_blackboard(brain):
    hit = brain_lookup("blackboard", brain)
    assert hit.matched is True
    assert hit.match_type == "REBRAND"
    assert hit.rebrand_target == "Anthology Inc"

def test_brain_lookup_alias_aws(brain):
    hit = brain_lookup("aws", brain)
    assert hit.matched is True
    assert hit.match_type == "ALIAS"
    assert hit.alias_target == "amazon web services"

def test_brain_lookup_no_match(brain):
    hit = brain_lookup("xyz unknown vendor", brain)
    assert hit.matched is False
    assert hit.confidence == 0.0
    assert hit.canonical is None


# ---------------------------------------------------------------------------
# dedup_check
# ---------------------------------------------------------------------------

def test_dedup_auto_merge():
    result = dedup_check("microsoft", ["microsoft", "microsfot"])
    assert result.status == "AUTO_MERGE"
    assert result.similarity >= 0.90

def test_dedup_unique_empty_list():
    result = dedup_check("xyz", [])
    assert result.status == "UNIQUE"
    assert result.similarity == 0.0

def test_dedup_skips_exact_self():
    # If the only other key is itself, should still be UNIQUE
    result = dedup_check("microsoft", ["microsoft"])
    assert result.status == "UNIQUE"

def test_dedup_candidate_range():
    # A moderately similar but not identical string
    result = dedup_check("amazon", ["amazen"])
    # Jaro-Winkler for "amazon"/"amazen" — should be in CANDIDATE or AUTO_MERGE range
    assert result.status in ("CANDIDATE", "AUTO_MERGE")


# ---------------------------------------------------------------------------
# entity_type_detect (spot-check via collect_signals)
# ---------------------------------------------------------------------------

def test_entity_type_person_with_title(empty_ctx):
    profile = collect_signals("Mr. John Smith", empty_ctx)
    assert profile.entity_type == EntityType.PERSON

def test_entity_type_person_no_title_is_company(empty_ctx):
    # Without title, "John Smith" treated as COMPANY to avoid false-positive discards
    profile = collect_signals("John Smith", empty_ctx)
    assert profile.entity_type == EntityType.COMPANY

def test_entity_type_internal(empty_ctx):
    profile = collect_signals("HR Department", empty_ctx)
    assert profile.entity_type == EntityType.INTERNAL

def test_entity_type_company(empty_ctx):
    # "Consulting" is a corporate indicator — COMPANY not AMBIGUOUS
    profile = collect_signals("Rao Consulting", empty_ctx)
    assert profile.entity_type == EntityType.COMPANY


# ---------------------------------------------------------------------------
# collect_signals end-to-end
# ---------------------------------------------------------------------------

def test_collect_signals_ibm_brain_matched(empty_ctx):
    profile = collect_signals("IBM", empty_ctx)
    assert profile.comparison_key == "ibm"
    assert profile.brain_hit.matched is True

def test_collect_signals_aws_alias(empty_ctx):
    profile = collect_signals("aws", empty_ctx)
    assert profile.brain_hit.match_type == "ALIAS"

def test_collect_signals_titled_person(empty_ctx):
    profile = collect_signals("Dr. John Smith", empty_ctx)
    assert profile.entity_type == EntityType.PERSON

def test_collect_signals_erp_signal_flows_through(brain):
    erp_ctx = BatchContext(
        known_vendors=brain.known_vendors,
        rebrand_map=brain.rebrand_map,
        alias_map=brain.alias_map,
        erp_hits={
            "ibm": ErpSignal(
                exists=True,
                spend=Decimal("480000"),
                category="IT_INFRASTRUCTURE",
                vendor_ids=["ERP-IBM-001"],
            )
        },
        ap_counts={},
        all_keys=[],
        doc_registry={},
        candidate_hints={},
    )
    profile = collect_signals("IBM", erp_ctx)
    assert profile.erp_signal.exists is True
    assert profile.erp_signal.spend == Decimal("480000")
