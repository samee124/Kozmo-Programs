"""Tests for cobalt.tools.entity_resolution."""

import pytest

from cobalt.brain.loader import invalidate_cache, load_brain
from cobalt.models.schemas.signal_profile_schema import BatchContext, ErpSignal
from cobalt.tools.candidate_screening import screen
from cobalt.tools.entity_resolution import resolve, resolve_all, ResolutionResult


@pytest.fixture(autouse=True)
def _clear_brain_cache():
    invalidate_cache()
    yield
    invalidate_cache()


@pytest.fixture
def brain():
    return load_brain()


@pytest.fixture
def ctx(brain):
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
# resolve — core cases
# ---------------------------------------------------------------------------

def test_resolve_ibm_matched_auto_confirm(ctx):
    result = resolve(screen("IBM"), ctx)
    assert result.resolution_status == "MATCHED"
    assert result.recommendation == "AUTO_CONFIRM"


def test_resolve_blackboard_rebranded(ctx):
    result = resolve(screen("Blackboard Inc"), ctx)
    assert result.resolution_status == "MATCHED"
    assert "VENDOR_REBRANDED" in result.lifecycle_signals


def test_resolve_person_discard(ctx):
    result = resolve(screen("Mr. John Smith"), ctx)
    assert result.resolution_status == "DISCARD"


def test_resolve_unknown_vendor_unmatched_viable(ctx):
    result = resolve(screen("Unknown Vendor Xyz"), ctx)
    assert result.resolution_status == "UNMATCHED_VIABLE"
    assert result.recommendation == "INVESTIGATE"


def test_resolve_internal_discard(ctx):
    result = resolve(screen("Finance Department"), ctx)
    assert result.resolution_status == "DISCARD"


def test_resolve_confidence_above_threshold(ctx):
    result = resolve(screen("IBM"), ctx)
    assert result.confidence >= 0.90


# ---------------------------------------------------------------------------
# resolve_all
# ---------------------------------------------------------------------------

def test_resolve_all_same_length(ctx):
    candidates = [screen(r) for r in ["IBM", "Aramark", "John Smith"]]
    results = resolve_all(candidates, ctx)
    assert len(results) == len(candidates)


def test_resolve_all_never_raises(ctx):
    # ScreenedCandidate with empty raw — should not raise
    candidates = [screen("IBM"), screen("")]
    results = resolve_all(candidates, ctx)
    assert len(results) == 2
    for r in results:
        assert isinstance(r, ResolutionResult)


def test_resolve_all_mixed_dispositions(ctx):
    candidates = [screen(r) for r in ["Microsoft Corp", "Dr. Jane Doe", "HR Department"]]
    results = resolve_all(candidates, ctx)
    statuses = [r.resolution_status for r in results]
    assert "MATCHED" in statuses or "UNMATCHED_VIABLE" in statuses
    assert "DISCARD" in statuses
