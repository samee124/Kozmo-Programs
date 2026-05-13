"""Tests for cobalt.models.schemas.signal_profile_schema."""

from decimal import Decimal

from cobalt.brain.loader import KnownVendor
from cobalt.models.schemas.signal_profile_schema import (
    ApSignal,
    BatchContext,
    BrainHit,
    DedupResult,
    EntityType,
    ErpSignal,
    IntakeStatus,
    ScriptType,
    SignalProfile,
)


def _make_brain_hit(matched=False) -> BrainHit:
    return BrainHit(
        matched=matched,
        confidence=0.0,
        canonical=None,
        match_type=None,
        rebrand_match=False,
        rebrand_target=None,
        alias_match=False,
        alias_target=None,
    )


def _make_erp_signal(exists=False) -> ErpSignal:
    return ErpSignal(exists=exists, spend=None, category=None)


def _make_ap_signal() -> ApSignal:
    return ApSignal(invoice_count=0)


def _make_dedup() -> DedupResult:
    return DedupResult(status="UNIQUE", match_key=None, match_name=None, similarity=0.0)


def test_signal_profile_instantiates_with_all_dimensions():
    sp = SignalProfile(
        raw="Acme Ltd",
        cleaned="acme ltd",
        script_type=ScriptType.LATIN,
        country_hint=None,
        normalized="acme",
        comparison_key="acme",
        brain_hit=_make_brain_hit(),
        erp_signal=_make_erp_signal(),
        ap_signal=_make_ap_signal(),
        linked_doc_ids=[],
        spend_hint=None,
        category_hint=None,
        entity_type=EntityType.COMPANY,
        dedup_result=_make_dedup(),
    )
    assert sp.raw == "Acme Ltd"
    assert sp.script_type == ScriptType.LATIN
    assert sp.entity_type == EntityType.COMPANY


def test_script_type_latin_equals_string():
    assert ScriptType.LATIN == "LATIN"


def test_entity_type_person_equals_string():
    assert EntityType.PERSON == "PERSON"


def test_brain_hit_with_matched_false_has_none_fields():
    hit = _make_brain_hit(matched=False)
    assert hit.canonical is None
    assert hit.match_type is None
    assert hit.rebrand_target is None
    assert hit.alias_target is None


def test_erp_signal_with_exists_false_has_spend_none():
    sig = _make_erp_signal(exists=False)
    assert sig.exists is False
    assert sig.spend is None


def test_ap_signal_with_empty_flags_has_single_approver_false():
    sig = ApSignal(invoice_count=3)
    assert sig.flags == []
    assert sig.single_approver is False


def test_intake_status_values_are_strings():
    assert IntakeStatus.CONFIRMED == "CONFIRMED"
    assert IntakeStatus.TRIAGE_REQUIRED == "TRIAGE_REQUIRED"
    assert IntakeStatus.DISCARDED == "DISCARDED"
    assert IntakeStatus.BLOCKED == "BLOCKED"
