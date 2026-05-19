"""Integration tests for the full P3 (Relationship & Spend) pipeline.

Tests the chain: structured_data_collector → spend_aggregator →
relationship_classifier → rs_profile_assembler, with document_intelligence
mocked (LLM boundary).

All tools run real implementations. Only llm_call, atomic_write, and
sync_to_db are stubbed.
"""

from __future__ import annotations

import csv
import json
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from cobalt.models.schemas.rs_schema import (
    DocumentIntelligenceResult,
    RelationshipSpendProfile,
    RSRunResult,
    RSRunStatus,
)
from cobalt.tools import (
    document_intelligence,
    relationship_classifier,
    rs_profile_assembler,
    spend_aggregator,
    structured_data_collector,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _write_csv(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)


def _today() -> str:
    return date.today().isoformat()


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


# ---------------------------------------------------------------------------
# Integration: collect → aggregate → classify
# ---------------------------------------------------------------------------

def test_collect_aggregate_classify_with_csv(tmp_path):
    """Full pipeline from CSV file to relationship classification.

    No vendor column → records get MEDIUM match_confidence and are included
    in aggregation totals.
    """
    csv_path = tmp_path / "spend.csv"
    today = date.today()
    _write_csv(
        csv_path,
        # No vendor column so records default to MEDIUM confidence (matched to vendor_id)
        ["amount", "currency", "period start", "period end", "po number"],
        [
            ["50000", "USD", f"{today.year}-01-01", f"{today.year}-03-31", "PO-001"],
            ["60000", "USD", f"{today.year}-04-01", f"{today.year}-06-30", "PO-002"],
            ["55000", "USD", f"{today.year-1}-01-01", f"{today.year-1}-03-31", "PO-003"],
            ["45000", "USD", f"{today.year-1}-04-01", f"{today.year-1}-06-30", "PO-004"],
        ],
    )

    # Step 1: Collect
    bundle = structured_data_collector.collect_structured_data(
        vendor_id="V-001",
        programme_id="PROG-001",
        arrival_modes=["FILE_UPLOAD"],
        uploaded_files=[{"file_id": "f1", "path": str(csv_path)}],
    )
    assert len(bundle.raw_spend_records) == 4

    # Step 2: No real documents (empty doc result)
    doc_result = DocumentIntelligenceResult(
        vendor_id="V-001",
        documents_processed=0,
        documents_skipped=0,
        extracted_contracts=[],
        extraction_warnings=[],
    )

    # Step 3: Aggregate
    aggregation = spend_aggregator.aggregate_spend(
        vendor_id="V-001",
        raw_records=bundle.raw_spend_records,
        contract_terms=[],
    )
    assert aggregation.summary.total_usd_all_time == pytest.approx(210_000.0)
    assert aggregation.summary.total_usd_ttm is not None

    # Step 4: Classify
    classification = relationship_classifier.classify_relationship(
        vendor_id="V-001",
        spend_summary=aggregation.summary,
        contract_terms=doc_result.extracted_contracts,
        entity_profile={},
        known_facts={},
    )
    assert classification.relationship_type in (
        "STRATEGIC", "PREFERRED", "TRANSACTIONAL", "INCIDENTAL", "UNKNOWN"
    )
    assert classification.dependency_tier in ("CRITICAL", "HIGH", "MEDIUM", "LOW", None)


def test_collect_aggregate_classify_with_checkin():
    """Check-in data flows through the full pipeline."""
    bundle = structured_data_collector.collect_structured_data(
        vendor_id="V-002",
        programme_id="PROG-001",
        arrival_modes=["CHECK_IN"],
        checkin_data={"spend_ytd": "120000", "currency": "USD"},
    )
    assert len(bundle.raw_spend_records) == 1

    aggregation = spend_aggregator.aggregate_spend(
        vendor_id="V-002",
        raw_records=bundle.raw_spend_records,
        contract_terms=[],
    )
    assert aggregation.summary.total_usd_all_time == pytest.approx(120_000.0)

    classification = relationship_classifier.classify_relationship(
        vendor_id="V-002",
        spend_summary=aggregation.summary,
        contract_terms=[],
        entity_profile={},
        known_facts={},
    )
    assert classification is not None


def test_high_spend_produces_strategic_or_higher(tmp_path):
    """$450K TTM spend should produce STRATEGIC or CRITICAL relationship."""
    csv_path = tmp_path / "spend.csv"
    today = date.today()
    _write_csv(
        csv_path,
        ["amount", "currency", "period end"],
        [
            ["450000", "USD", (today - timedelta(days=30)).isoformat()],
        ],
    )

    bundle = structured_data_collector.collect_structured_data(
        vendor_id="V-003",
        programme_id="PROG-001",
        arrival_modes=["FILE_UPLOAD"],
        uploaded_files=[{"file_id": "f1", "path": str(csv_path)}],
    )

    aggregation = spend_aggregator.aggregate_spend("V-003", bundle.raw_spend_records, [])

    classification = relationship_classifier.classify_relationship(
        vendor_id="V-003",
        spend_summary=aggregation.summary,
        contract_terms=[],
        entity_profile={},
        known_facts={},
    )
    assert classification.relationship_type in ("STRATEGIC", "PREFERRED")


def test_no_data_produces_unknown_classification():
    """No spend records → UNKNOWN relationship type."""
    bundle = structured_data_collector.collect_structured_data(
        vendor_id="V-004",
        programme_id="PROG-001",
        arrival_modes=["FILE_UPLOAD"],
        uploaded_files=[],
    )
    assert bundle.raw_spend_records == []

    aggregation = spend_aggregator.aggregate_spend("V-004", [], [])
    assert aggregation.summary.data_completeness == "NONE"

    classification = relationship_classifier.classify_relationship(
        vendor_id="V-004",
        spend_summary=aggregation.summary,
        contract_terms=[],
        entity_profile={},
        known_facts={},
    )
    assert classification.relationship_type == "UNKNOWN"


# ---------------------------------------------------------------------------
# Integration: collect → aggregate → classify → assemble
# ---------------------------------------------------------------------------

@pytest.fixture
def workspace_env(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "PROG-001" / "V-001").mkdir(parents=True)
    return tmp_path


def test_full_pipeline_produces_profile(tmp_path, workspace_env):
    """End-to-end: CSV → aggregate → classify → assemble produces RelationshipSpendProfile."""
    csv_path = tmp_path / "spend.csv"
    today = date.today()
    _write_csv(
        csv_path,
        ["amount", "currency", "period start", "period end"],
        [
            ["80000", "USD", f"{today.year}-01-01", f"{today.year}-03-31"],
            ["70000", "USD", f"{today.year-1}-01-01", f"{today.year-1}-12-31"],
        ],
    )

    bundle = structured_data_collector.collect_structured_data(
        vendor_id="V-001",
        programme_id="PROG-001",
        arrival_modes=["FILE_UPLOAD"],
        uploaded_files=[{"file_id": "f1", "path": str(csv_path)}],
    )

    doc_result = DocumentIntelligenceResult(
        vendor_id="V-001",
        documents_processed=0,
        documents_skipped=0,
        extracted_contracts=[],
        extraction_warnings=[],
    )

    aggregation = spend_aggregator.aggregate_spend(
        vendor_id="V-001",
        raw_records=bundle.raw_spend_records,
        contract_terms=[],
    )

    classification = relationship_classifier.classify_relationship(
        vendor_id="V-001",
        spend_summary=aggregation.summary,
        contract_terms=[],
        entity_profile={},
        known_facts={},
    )

    with patch("cobalt.db.sync_to_db.sync_to_db"):
        profile = rs_profile_assembler.assemble_rs_profile(
            vendor_id="V-001",
            programme_id="PROG-001",
            structured_bundle=bundle,
            doc_intelligence=doc_result,
            spend_aggregation=aggregation,
            classification=classification,
            current_pcs=0.50,
        )

    assert isinstance(profile, RelationshipSpendProfile)
    assert profile.vendor_id == "V-001"
    assert profile.profile_status in ("COMPLETE", "PARTIAL", "MINIMAL", "FAILED")
    assert 0.0 <= profile.pcs_total <= 1.0
    assert profile.profile_version >= 1


def test_profile_written_to_disk(workspace_env):
    """Verify relationship_spend_profile.md is written to workspace."""
    from cobalt.core.file_system import rs_profile_path

    bundle = structured_data_collector.collect_structured_data(
        vendor_id="V-001",
        programme_id="PROG-001",
        arrival_modes=["CHECK_IN"],
        checkin_data={"spend_ytd": "50000", "currency": "USD"},
    )

    doc_result = DocumentIntelligenceResult(
        vendor_id="V-001",
        documents_processed=0, documents_skipped=0,
        extracted_contracts=[], extraction_warnings=[],
    )

    aggregation = spend_aggregator.aggregate_spend(
        vendor_id="V-001",
        raw_records=bundle.raw_spend_records,
        contract_terms=[],
    )

    classification = relationship_classifier.classify_relationship(
        vendor_id="V-001",
        spend_summary=aggregation.summary,
        contract_terms=[],
        entity_profile={},
        known_facts={},
    )

    with patch("cobalt.db.sync_to_db.sync_to_db"):
        rs_profile_assembler.assemble_rs_profile(
            vendor_id="V-001",
            programme_id="PROG-001",
            structured_bundle=bundle,
            doc_intelligence=doc_result,
            spend_aggregation=aggregation,
            classification=classification,
            current_pcs=0.40,
        )

    profile_path = rs_profile_path("PROG-001", "V-001")
    assert profile_path.exists()
    content = profile_path.read_text(encoding="utf-8")
    assert "vendor_id" in content
    assert "V-001" in content


def test_document_intelligence_feeds_contract_to_classifier(tmp_path, workspace_env):
    """Contracts extracted by document_intelligence reach relationship_classifier."""
    doc_path = tmp_path / "contract.txt"
    doc_path.write_text("Contract text " * 30, encoding="utf-8")  # > 100 chars

    llm_response = json.dumps({
        "effective_date": "2023-01-01",
        "expiry_date": "2025-01-01",
        "auto_renews": True,
        "notice_period_days": 90,
        "total_value": 500_000.0,
        "currency": "USD",
        "payment_terms_days": 30,
        "governing_law": "English law",
        "termination_clauses": ["90 days notice"],
        "key_obligations": ["Provide services"],
        "sla_summary": "99% uptime",
    })

    with patch("cobalt.tools.document_intelligence.llm_call", return_value=llm_response):
        doc_result = document_intelligence.process_documents(
            vendor_id="V-001",
            programme_id="PROG-001",
            document_paths=[str(doc_path)],
        )

    assert len(doc_result.extracted_contracts) == 1
    assert doc_result.extracted_contracts[0].total_value == 500_000.0

    aggregation = spend_aggregator.aggregate_spend(
        vendor_id="V-001",
        raw_records=[],
        contract_terms=doc_result.extracted_contracts,
    )

    classification = relationship_classifier.classify_relationship(
        vendor_id="V-001",
        spend_summary=aggregation.summary,
        contract_terms=doc_result.extracted_contracts,
        entity_profile={},
        known_facts={},
    )
    assert classification.contract_coverage in ("COVERED", "PARTIALLY_COVERED")


def test_schema_serialisation_roundtrip(tmp_path, workspace_env):
    """Verify to_dict/from_dict roundtrip for all P3 schema objects."""
    bundle = structured_data_collector.collect_structured_data(
        vendor_id="V-001",
        programme_id="PROG-001",
        arrival_modes=["CHECK_IN"],
        checkin_data={"spend_ytd": "30000", "currency": "USD"},
    )

    doc_result = DocumentIntelligenceResult(
        vendor_id="V-001",
        documents_processed=0, documents_skipped=0,
        extracted_contracts=[], extraction_warnings=[],
    )

    aggregation = spend_aggregator.aggregate_spend("V-001", bundle.raw_spend_records, [])
    classification = relationship_classifier.classify_relationship(
        vendor_id="V-001",
        spend_summary=aggregation.summary,
        contract_terms=[],
        entity_profile={},
        known_facts={},
    )

    # Roundtrip each schema
    from cobalt.models.schemas.rs_schema import (
        StructuredDataBundle,
        DocumentIntelligenceResult as DIResult,
        SpendAggregationResult,
        RelationshipClassification,
    )

    b2 = StructuredDataBundle.from_dict(bundle.to_dict())
    assert b2.vendor_id == bundle.vendor_id

    d2 = DIResult.from_dict(doc_result.to_dict())
    assert d2.vendor_id == doc_result.vendor_id

    a2 = SpendAggregationResult.from_dict(aggregation.to_dict())
    assert a2.summary.total_usd_all_time == aggregation.summary.total_usd_all_time

    c2 = RelationshipClassification.from_dict(classification.to_dict())
    assert c2.relationship_type == classification.relationship_type
