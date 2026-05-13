"""Tests for cobalt.orchestrator.batch_context_builder."""

from decimal import Decimal

import pytest

from cobalt.agents.research_agent import ResearchAgent
from cobalt.brain.loader import BrainData, invalidate_cache
from cobalt.models.schemas.signal_profile_schema import BatchContext
from cobalt.orchestrator.batch_context_builder import build_batch_context
from cobalt.tools.source_intake import DocRecord, RawCandidate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _candidate(name: str, spend: Decimal | None = None, cat: str | None = None) -> RawCandidate:
    return RawCandidate(
        raw_name=name,
        source="VENDOR_LIST",
        source_refs=[],
        linked_doc_ids=[],
        spend_hint=spend,
        category_hint=cat,
        department_hint=None,
    )


def _doc(doc_id: str, vendor_key: str | None = None) -> DocRecord:
    return DocRecord(
        doc_id=doc_id,
        filename=f"{doc_id}.pdf",
        file_path=f"/docs/{doc_id}.pdf",
        raw_text="contract text",
        doc_type="MSA",
        vendor_key=vendor_key,
    )


def _mock_brain() -> BrainData:
    return BrainData(known_vendors={}, rebrand_map={}, alias_map={})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_empty_candidates_returns_empty_context(monkeypatch):
    invalidate_cache()
    monkeypatch.setattr("cobalt.orchestrator.batch_context_builder.load_brain", _mock_brain)
    result = build_batch_context([], ResearchAgent(), "prog-1", {})
    assert isinstance(result, BatchContext)
    assert result.all_keys == []
    assert result.erp_hits == {}
    assert result.ap_counts == {}
    assert result.candidate_hints == {}
    assert result.doc_registry == {}


def test_doc_registry_links_flow_to_context(monkeypatch):
    invalidate_cache()
    monkeypatch.setattr("cobalt.orchestrator.batch_context_builder.load_brain", _mock_brain)
    candidates = [_candidate("IBM")]
    docs = {"doc123": _doc("doc123", vendor_key="ibm")}
    result = build_batch_context(candidates, ResearchAgent(), "prog-1", docs)
    assert "ibm" in result.doc_registry
    assert "doc123" in result.doc_registry["ibm"]


def test_doc_without_vendor_key_not_in_context(monkeypatch):
    invalidate_cache()
    monkeypatch.setattr("cobalt.orchestrator.batch_context_builder.load_brain", _mock_brain)
    docs = {"doc456": _doc("doc456", vendor_key=None)}
    result = build_batch_context([], ResearchAgent(), "prog-1", docs)
    assert result.doc_registry == {}


def test_candidate_hints_populated_from_spend_hint(monkeypatch):
    invalidate_cache()
    monkeypatch.setattr("cobalt.orchestrator.batch_context_builder.load_brain", _mock_brain)
    candidates = [_candidate("IBM", spend=Decimal("50000"), cat="IT")]
    result = build_batch_context(candidates, ResearchAgent(), "prog-1", {})
    assert len(result.candidate_hints) >= 1
    hints = list(result.candidate_hints.values())[0]
    assert hints["spend_hint"] == Decimal("50000")
    assert hints["category_hint"] == "IT"


def test_candidate_no_hint_not_in_hints(monkeypatch):
    invalidate_cache()
    monkeypatch.setattr("cobalt.orchestrator.batch_context_builder.load_brain", _mock_brain)
    candidates = [_candidate("IBM")]  # no spend or category
    result = build_batch_context(candidates, ResearchAgent(), "prog-1", {})
    assert result.candidate_hints == {}


def test_erp_not_configured_returns_empty(monkeypatch):
    invalidate_cache()
    monkeypatch.setattr("cobalt.orchestrator.batch_context_builder.load_brain", _mock_brain)
    result = build_batch_context([_candidate("IBM")], ResearchAgent(), "prog-1", {})
    assert result.erp_hits == {}


def test_all_keys_populated(monkeypatch):
    invalidate_cache()
    monkeypatch.setattr("cobalt.orchestrator.batch_context_builder.load_brain", _mock_brain)
    candidates = [_candidate("IBM"), _candidate("Aramark")]
    result = build_batch_context(candidates, ResearchAgent(), "prog-1", {})
    assert len(result.all_keys) == 2


def test_never_raises_when_load_brain_fails(monkeypatch):
    invalidate_cache()

    def _raise():
        raise RuntimeError("brain down")

    monkeypatch.setattr("cobalt.orchestrator.batch_context_builder.load_brain", _raise)
    result = build_batch_context([_candidate("IBM")], ResearchAgent(), "prog-1", {})
    assert isinstance(result, BatchContext)
    assert result.known_vendors == {}
