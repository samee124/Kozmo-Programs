"""Tests for cobalt.workspace.builder — single-file architecture."""

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from cobalt.models.schemas.intake_result_schema import IntakeResult, IntakeStatus
from cobalt.models.schemas.investigation_plan_schema import (
    FraudRisk,
    InvestigationDepth,
    InvestigationPlan,
)
from cobalt.models.schemas.signal_profile_schema import ErpSignal
from cobalt.workspace.builder import WorkspaceBuildResult, _make_slug, _pcs_band, build_workspace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_frontmatter(path: Path) -> dict:
    """Parse the YAML front-matter from a .md file written by atomic_write."""
    text = path.read_text(encoding="utf-8")
    parts = text.split("---\n", 2)
    return yaml.safe_load(parts[1]) if len(parts) >= 3 else yaml.safe_load(text) or {}


def _vendor_file(bw: WorkspaceBuildResult) -> Path:
    """Return the single *.md vendor file from a build result."""
    md_files = list(bw.workspace_path.glob("*.md"))
    assert md_files, f"No *.md file found in {bw.workspace_path}"
    return md_files[0]


def _make_plan() -> InvestigationPlan:
    return InvestigationPlan(
        depth=InvestigationDepth.FAST,
        steps=[],
        require_human_gate=False,
        require_legal_gate=False,
        fraud_risk=FraudRisk.LOW,
        resolving_question=None,
        reason="test",
    )


def make_confirmed(
    vendor_id: str = "v-abc12345",
    raw_input: str = "IBM",
    canonical_name: str = "IBM Corporation",
    linked_doc_ids: list | None = None,
    erp_category: str | None = None,
) -> IntakeResult:
    return IntakeResult(
        raw_input=raw_input,
        canonical_name=canonical_name,
        vendor_id=vendor_id,
        status=IntakeStatus.CONFIRMED,
        confidence=0.97,
        resolution_method="BRAIN_LOOKUP",
        country_code="US",
        erp_spend=None,
        erp_category=erp_category,
        data_class="CLASS_D",
        entity_type="COMPANY",
        triage_reason=None,
        triage_question=None,
        fraud_signals=[],
        fraud_risk="LOW",
        block_reason=None,
        aliases=[],
        linked_doc_ids=linked_doc_ids or [],
        extracted_terms=None,
        investigation_plan=_make_plan(),
    )


# ---------------------------------------------------------------------------
# _pcs_band
# ---------------------------------------------------------------------------

def test_pcs_band_insufficient():
    assert _pcs_band(0) == "INSUFFICIENT"
    assert _pcs_band(29) == "INSUFFICIENT"


def test_pcs_band_exploratory():
    assert _pcs_band(30) == "EXPLORATORY"
    assert _pcs_band(49) == "EXPLORATORY"


def test_pcs_band_guided():
    assert _pcs_band(50) == "GUIDED"
    assert _pcs_band(74) == "GUIDED"


def test_pcs_band_execution_ready():
    assert _pcs_band(75) == "EXECUTION_READY"
    assert _pcs_band(100) == "EXECUTION_READY"


# ---------------------------------------------------------------------------
# _make_slug
# ---------------------------------------------------------------------------

def test_make_slug_strips_corp():
    assert _make_slug("IBM Corporation") == "ibm"


def test_make_slug_strips_inc():
    assert _make_slug("Salesforce, Inc.") == "salesforce"


def test_make_slug_handles_spaces():
    assert _make_slug("Acme Corp") == "acme"


def test_make_slug_handles_numbers():
    assert _make_slug("3M Company") == "3m_company"


# ---------------------------------------------------------------------------
# build_workspace — single file created
# ---------------------------------------------------------------------------

def test_build_workspace_confirmed_creates_single_file(tmp_workspace):
    bw = build_workspace(make_confirmed(), "prog-1")
    assert bw.success is True
    md_files = list(bw.workspace_path.glob("*.md"))
    assert len(md_files) == 1


def test_build_workspace_file_named_by_slug(tmp_workspace):
    bw = build_workspace(make_confirmed(canonical_name="IBM Corporation"), "prog-1")
    md_files = list(bw.workspace_path.glob("*.md"))
    assert md_files[0].name == "ibm.md"


def test_build_workspace_no_subdirectories(tmp_workspace):
    bw = build_workspace(make_confirmed(), "prog-1")
    subdirs = [p for p in bw.workspace_path.iterdir() if p.is_dir()]
    assert subdirs == []


# ---------------------------------------------------------------------------
# Vendor file structure
# ---------------------------------------------------------------------------

def test_vendor_file_has_vendor_id(tmp_workspace):
    bw = build_workspace(make_confirmed(vendor_id="v-abc12345"), "prog-1")
    data = _read_frontmatter(_vendor_file(bw))
    assert data["vendor_id"] == "v-abc12345"


def test_vendor_file_has_canonical_name(tmp_workspace):
    bw = build_workspace(make_confirmed(canonical_name="IBM Corporation"), "prog-1")
    data = _read_frontmatter(_vendor_file(bw))
    assert data["canonical_name"] == "IBM Corporation"


def test_vendor_file_has_slug(tmp_workspace):
    bw = build_workspace(make_confirmed(canonical_name="IBM Corporation"), "prog-1")
    data = _read_frontmatter(_vendor_file(bw))
    assert data["slug"] == "ibm"


def test_vendor_file_status_intake_completed(tmp_workspace):
    bw = build_workspace(make_confirmed(), "prog-1")
    data = _read_frontmatter(_vendor_file(bw))
    assert data["status"] == "INTAKE_COMPLETED"


def test_vendor_file_intake_section_immutable_fields(tmp_workspace):
    bw = build_workspace(make_confirmed(raw_input="IBM Corp"), "prog-1")
    data = _read_frontmatter(_vendor_file(bw))
    intake = data["intake"]
    assert intake["input_name"] == "IBM Corp"
    assert intake["resolution_method"] == "BRAIN_LOOKUP"
    assert intake["data_class"] == "CLASS_D"
    assert intake["confidence"] == pytest.approx(0.97)


# ---------------------------------------------------------------------------
# Financial section
# ---------------------------------------------------------------------------

def test_financial_annual_spend_observed_with_erp(tmp_workspace):
    erp = ErpSignal(exists=True, spend=Decimal("75000"), category="IT", vendor_ids=[])
    bw = build_workspace(make_confirmed(), "prog-1", erp_signal=erp)
    data = _read_frontmatter(_vendor_file(bw))
    fin = data["financial"]
    assert fin["spend_status"] == "OBSERVED"
    assert fin["annual_spend"]["value"] == "75000"


def test_financial_annual_spend_inferred_without_erp(tmp_workspace):
    bw = build_workspace(make_confirmed(), "prog-1")
    data = _read_frontmatter(_vendor_file(bw))
    fin = data["financial"]
    assert fin["spend_status"] == "INFERRED"
    assert fin["annual_spend"]["confidence"] == "INSUF"


def test_financial_currency_none_when_erp_has_no_currency(tmp_workspace):
    erp = ErpSignal(exists=True, spend=Decimal("50000"), category="IT", vendor_ids=[])
    bw = build_workspace(make_confirmed(), "prog-1", erp_signal=erp)
    data = _read_frontmatter(_vendor_file(bw))
    # currency should be None (ErpSignal has no currency attr)
    assert data["financial"]["currency"] is None


# ---------------------------------------------------------------------------
# Legal section
# ---------------------------------------------------------------------------

def test_legal_section_observed_with_terms(tmp_workspace):
    terms = {"renewal_date": "2026-01-01", "contract_value": "100000", "confidence": 0.88}
    bw = build_workspace(make_confirmed(), "prog-1", extracted_terms=terms)
    data = _read_frontmatter(_vendor_file(bw))
    legal = data["legal"]
    assert legal["renewal_date"]["value"] == "2026-01-01"
    assert legal["contract_value"]["value"] == "100000"


def test_legal_section_insuf_without_terms(tmp_workspace):
    bw = build_workspace(make_confirmed(), "prog-1")
    data = _read_frontmatter(_vendor_file(bw))
    legal = data["legal"]
    assert legal["renewal_date"]["confidence"] == "INSUF"
    assert legal["renewal_date"]["value"] is None


# ---------------------------------------------------------------------------
# PCS section
# ---------------------------------------------------------------------------

def test_pcs_zero_no_signals(tmp_workspace):
    bw = build_workspace(make_confirmed(), "prog-1")
    data = _read_frontmatter(_vendor_file(bw))
    assert data["pcs"]["score"] == 0
    assert data["pcs"]["band"] == "INSUFFICIENT"


def test_pcs_with_erp_and_terms(tmp_workspace):
    erp = ErpSignal(exists=True, spend=Decimal("50000"), category=None, vendor_ids=[])
    terms = {"renewal_date": "2026-01-01", "contract_value": "50000", "confidence": 0.80}
    bw = build_workspace(make_confirmed(), "prog-1", extracted_terms=terms, erp_signal=erp)
    data = _read_frontmatter(_vendor_file(bw))
    # +12 (spend) +15 (renewal_date) +8 (contract_value) = 35
    assert data["pcs"]["score"] == 35
    assert data["pcs"]["band"] == "EXPLORATORY"


# ---------------------------------------------------------------------------
# Classification section
# ---------------------------------------------------------------------------

def test_classification_category_from_erp(tmp_workspace):
    bw = build_workspace(make_confirmed(erp_category="IT_SOFTWARE"), "prog-1")
    data = _read_frontmatter(_vendor_file(bw))
    assert data["classification"]["category"]["value"] == "IT_SOFTWARE"
    assert data["classification"]["category"]["source"] == "ERP"


def test_classification_category_insuf_without_erp_category(tmp_workspace):
    bw = build_workspace(make_confirmed(erp_category=None), "prog-1")
    data = _read_frontmatter(_vendor_file(bw))
    assert data["classification"]["category"]["confidence"] == "INSUF"


# ---------------------------------------------------------------------------
# Change log
# ---------------------------------------------------------------------------

def test_change_log_has_intake_entry(tmp_workspace):
    bw = build_workspace(make_confirmed(), "prog-1")
    data = _read_frontmatter(_vendor_file(bw))
    assert len(data["change_log"]) == 1
    assert data["change_log"][0]["event"] == "INTAKE_COMPLETED"


def test_change_log_pcs_score(tmp_workspace):
    erp = ErpSignal(exists=True, spend=Decimal("50000"), category=None, vendor_ids=[])
    bw = build_workspace(make_confirmed(), "prog-1", erp_signal=erp)
    data = _read_frontmatter(_vendor_file(bw))
    assert data["change_log"][0]["pcs_score"] == 12


# ---------------------------------------------------------------------------
# Contract documents
# ---------------------------------------------------------------------------

def test_documents_written_per_doc_id(tmp_workspace):
    terms = {"doc_type": "MSA", "confidence": 0.85}
    result = make_confirmed(linked_doc_ids=["abc123", "def456"])
    bw = build_workspace(result, "prog-1", extracted_terms=terms)
    data = _read_frontmatter(_vendor_file(bw))
    doc_ids = [d["doc_id"] for d in data["commercial"]["documents"]]
    assert "abc123" in doc_ids
    assert "def456" in doc_ids


def test_documents_not_written_without_terms(tmp_workspace):
    result = make_confirmed(linked_doc_ids=["abc123"])
    bw = build_workspace(result, "prog-1")  # no extracted_terms
    data = _read_frontmatter(_vendor_file(bw))
    assert data["commercial"]["documents"] == []


def test_document_content_has_doc_type(tmp_workspace):
    terms = {"doc_type": "SOW", "confidence": 0.75}
    result = make_confirmed(linked_doc_ids=["xyz789"])
    bw = build_workspace(result, "prog-1", extracted_terms=terms)
    data = _read_frontmatter(_vendor_file(bw))
    doc = data["commercial"]["documents"][0]
    assert doc["doc_id"] == "xyz789"
    assert doc["doc_type"] == "SOW"
    assert doc["type"] == "CONTRACT_DOCUMENT"


# ---------------------------------------------------------------------------
# files_written tracking
# ---------------------------------------------------------------------------

def test_files_written_contains_single_entry(tmp_workspace):
    bw = build_workspace(make_confirmed(), "prog-1")
    assert len(bw.files_written) == 1
    assert bw.files_written[0].endswith(".md")


# ---------------------------------------------------------------------------
# Non-CONFIRMED raises ValueError
# ---------------------------------------------------------------------------

def test_non_confirmed_raises_value_error(tmp_workspace):
    triage_result = IntakeResult(
        raw_input="IBM",
        canonical_name=None,
        vendor_id=None,
        status=IntakeStatus.TRIAGE_REQUIRED,
        confidence=0.0,
        resolution_method="TRIAGE",
        country_code=None,
        erp_spend=None,
        erp_category=None,
        data_class="CLASS_D",
        entity_type="AMBIGUOUS",
        triage_reason="manual review",
        triage_question="Is this a real vendor?",
        fraud_signals=[],
        fraud_risk="LOW",
        block_reason=None,
        aliases=[],
        linked_doc_ids=[],
        extracted_terms=None,
        investigation_plan=_make_plan(),
    )
    with pytest.raises(ValueError):
        build_workspace(triage_result, "prog-1")


def test_discarded_raises_value_error(tmp_workspace):
    from cobalt.models.schemas.intake_result_schema import make_discarded
    result = make_discarded("nobody", "PERSON type")
    with pytest.raises(ValueError):
        build_workspace(result, "prog-1")
