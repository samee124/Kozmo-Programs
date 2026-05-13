"""Tests for cobalt.workspace.builder."""

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
from cobalt.workspace.builder import WorkspaceBuildResult, _pcs_band, build_workspace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_frontmatter(path: Path) -> dict:
    """Parse the YAML front-matter from a .md file written by atomic_write."""
    text = path.read_text(encoding="utf-8")
    parts = text.split("---\n", 2)
    return yaml.safe_load(parts[1]) if len(parts) >= 3 else yaml.safe_load(text) or {}

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
# build_workspace — directory structure
# ---------------------------------------------------------------------------

def test_build_workspace_confirmed_creates_dirs(tmp_workspace):
    result = make_confirmed()
    bw = build_workspace(result, "prog-1")
    assert bw.success is True
    root = bw.workspace_path
    assert (root / "identity").is_dir()
    assert (root / "cost_file").is_dir()
    assert (root / "execution").is_dir()
    assert (root / "evidence").is_dir()


# ---------------------------------------------------------------------------
# entity.md
# ---------------------------------------------------------------------------

def test_entity_md_written(tmp_workspace):
    bw = build_workspace(make_confirmed(), "prog-1")
    entity_path = bw.workspace_path / "identity" / "entity.md"
    assert entity_path.exists()


def test_entity_md_input_name(tmp_workspace):
    bw = build_workspace(make_confirmed(raw_input="IBM Corp"), "prog-1")
    data = _read_frontmatter(bw.workspace_path / "identity" / "entity.md")
    assert data["input_name"] == "IBM Corp"


def test_entity_md_vendor_name(tmp_workspace):
    bw = build_workspace(make_confirmed(canonical_name="IBM Corporation"), "prog-1")
    data = _read_frontmatter(bw.workspace_path / "identity" / "entity.md")
    assert data["vendor_name"] == "IBM Corporation"


def test_entity_md_version_is_1(tmp_workspace):
    bw = build_workspace(make_confirmed(), "prog-1")
    data = _read_frontmatter(bw.workspace_path / "identity" / "entity.md")
    assert data["version"] == 1


# ---------------------------------------------------------------------------
# spend.md
# ---------------------------------------------------------------------------

def test_spend_md_observed_with_erp(tmp_workspace):
    erp = ErpSignal(exists=True, spend=Decimal("75000"), category="IT", vendor_ids=[])
    bw = build_workspace(make_confirmed(), "prog-1", erp_signal=erp)
    data = _read_frontmatter(bw.workspace_path / "cost_file" / "spend.md")
    assert data["status"] == "OBSERVED"
    assert data["confidence"] == pytest.approx(0.92)
    assert data["annual_spend"] == "75000"


def test_spend_md_inferred_without_erp(tmp_workspace):
    bw = build_workspace(make_confirmed(), "prog-1")
    data = _read_frontmatter(bw.workspace_path / "cost_file" / "spend.md")
    assert data["status"] == "INFERRED"
    assert data["confidence"] == pytest.approx(0.25)
    assert data["annual_spend"] is None


def test_spend_md_inferred_when_erp_has_no_spend(tmp_workspace):
    erp = ErpSignal(exists=True, spend=None, category=None, vendor_ids=[])
    bw = build_workspace(make_confirmed(), "prog-1", erp_signal=erp)
    data = _read_frontmatter(bw.workspace_path / "cost_file" / "spend.md")
    assert data["status"] == "INFERRED"


# ---------------------------------------------------------------------------
# contract.md
# ---------------------------------------------------------------------------

def test_contract_md_observed_with_terms(tmp_workspace):
    terms = {"renewal_date": "2026-01-01", "contract_value": "100000", "confidence": 0.88}
    bw = build_workspace(make_confirmed(), "prog-1", extracted_terms=terms)
    data = _read_frontmatter(bw.workspace_path / "cost_file" / "contract.md")
    assert data["status"] == "OBSERVED"
    assert data["overall_confidence"] == pytest.approx(0.88)
    assert data["renewal_date"] == "2026-01-01"


def test_contract_md_not_found_without_terms(tmp_workspace):
    bw = build_workspace(make_confirmed(), "prog-1")
    data = _read_frontmatter(bw.workspace_path / "cost_file" / "contract.md")
    assert data["status"] == "NOT_FOUND"
    assert data["overall_confidence"] == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# coverage.md
# ---------------------------------------------------------------------------

def test_coverage_md_pcs_zero_no_signals(tmp_workspace):
    bw = build_workspace(make_confirmed(), "prog-1")
    data = _read_frontmatter(bw.workspace_path / "cost_file" / "coverage.md")
    assert data["overall_pcs"] == 0
    assert data["pcs_band"] == "INSUFFICIENT"


def test_coverage_md_pcs_with_erp_and_terms(tmp_workspace):
    erp = ErpSignal(exists=True, spend=Decimal("50000"), category=None, vendor_ids=[])
    terms = {"renewal_date": "2026-01-01", "contract_value": "50000", "confidence": 0.80}
    bw = build_workspace(make_confirmed(), "prog-1", extracted_terms=terms, erp_signal=erp)
    data = _read_frontmatter(bw.workspace_path / "cost_file" / "coverage.md")
    # +12 (spend) +15 (renewal_date) +8 (contract_value) = 35
    assert data["overall_pcs"] == 35
    assert data["pcs_band"] == "EXPLORATORY"


def test_coverage_md_has_blocking_gaps(tmp_workspace):
    bw = build_workspace(make_confirmed(), "prog-1")
    data = _read_frontmatter(bw.workspace_path / "cost_file" / "coverage.md")
    assert data["blocking_gaps"] == []


# ---------------------------------------------------------------------------
# evidence files
# ---------------------------------------------------------------------------

def test_evidence_written_per_doc_id(tmp_workspace):
    terms = {"doc_type": "MSA", "confidence": 0.85}
    result = make_confirmed(linked_doc_ids=["abc123", "def456"])
    bw = build_workspace(result, "prog-1", extracted_terms=terms)
    ev1 = bw.workspace_path / "evidence" / "ev-contract-abc123.md"
    ev2 = bw.workspace_path / "evidence" / "ev-contract-def456.md"
    assert ev1.exists()
    assert ev2.exists()


def test_evidence_not_written_without_terms(tmp_workspace):
    result = make_confirmed(linked_doc_ids=["abc123"])
    bw = build_workspace(result, "prog-1")  # no extracted_terms
    ev = bw.workspace_path / "evidence" / "ev-contract-abc123.md"
    assert not ev.exists()


def test_evidence_content_has_doc_id(tmp_workspace):
    terms = {"doc_type": "SOW", "confidence": 0.75}
    result = make_confirmed(linked_doc_ids=["xyz789"])
    bw = build_workspace(result, "prog-1", extracted_terms=terms)
    data = _read_frontmatter(bw.workspace_path / "evidence" / "ev-contract-xyz789.md")
    assert data["data"]["doc_id"] == "xyz789"
    assert data["type"] == "CONTRACT_DOCUMENT"


# ---------------------------------------------------------------------------
# ledger.md
# ---------------------------------------------------------------------------

def test_ledger_md_created(tmp_workspace):
    bw = build_workspace(make_confirmed(), "prog-1")
    ledger = bw.workspace_path / "execution" / "ledger.md"
    assert ledger.exists()


def test_ledger_md_contains_intake_completed(tmp_workspace):
    bw = build_workspace(make_confirmed(), "prog-1")
    content = (bw.workspace_path / "execution" / "ledger.md").read_text(encoding="utf-8")
    assert "INTAKE_COMPLETED" in content


# ---------------------------------------------------------------------------
# files_written tracking
# ---------------------------------------------------------------------------

def test_files_written_includes_entity(tmp_workspace):
    bw = build_workspace(make_confirmed(), "prog-1")
    assert any("entity.md" in f for f in bw.files_written)


def test_files_written_includes_ledger(tmp_workspace):
    bw = build_workspace(make_confirmed(), "prog-1")
    assert any("ledger.md" in f for f in bw.files_written)


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
