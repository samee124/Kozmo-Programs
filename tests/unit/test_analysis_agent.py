"""Tests for cobalt.agents.analysis_agent."""

import pytest

import cobalt.core.llm_call as llm_call_module
from cobalt.agents.analysis_agent import AnalysisAgent

agent = AnalysisAgent()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_llm(monkeypatch, response_json: str) -> None:
    def fake_call(prompt, system, model):
        return response_json, 10, 5
    monkeypatch.setattr(llm_call_module, "_call", fake_call)


def _patch_llm_raise(monkeypatch) -> None:
    def failing_call(prompt, system, model):
        raise RuntimeError("API down")
    monkeypatch.setattr(llm_call_module, "_call", failing_call)


# ---------------------------------------------------------------------------
# extract_vendor_name_from_doc
# ---------------------------------------------------------------------------

def test_extract_vendor_name_empty_text():
    result = agent.extract_vendor_name_from_doc("")
    assert result["vendor_name"] is None
    assert result["legal_name"] is None
    assert result["confidence"] == 0.0


def test_extract_vendor_name_whitespace_only():
    result = agent.extract_vendor_name_from_doc("   \n  ")
    assert result["confidence"] == 0.0


def test_extract_vendor_name_valid_text(mock_llm):
    _patch_llm(
        mock_llm,
        '{"vendor_name": "Acme Corp", "legal_name": "Acme Corporation Inc",'
        ' "doc_type_hint": "MSA", "confidence": 0.9}',
    )
    result = agent.extract_vendor_name_from_doc(
        "This Master Services Agreement is between Buyer Inc and Acme Corp.",
        filename="contract.pdf",
    )
    assert result["vendor_name"] == "Acme Corp"
    assert result["legal_name"] == "Acme Corporation Inc"
    assert result["doc_type_hint"] == "MSA"
    assert result["confidence"] == pytest.approx(0.9)


def test_extract_vendor_name_has_required_keys(mock_llm):
    result = agent.extract_vendor_name_from_doc("Some document text here.")
    assert "vendor_name" in result
    assert "legal_name" in result
    assert "doc_type_hint" in result
    assert "confidence" in result


def test_extract_vendor_name_llm_failure(mock_llm):
    _patch_llm_raise(mock_llm)
    result = agent.extract_vendor_name_from_doc("Some vendor text about a company.")
    assert result["vendor_name"] is None
    assert result["confidence"] == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# extract_contract_terms
# ---------------------------------------------------------------------------

def test_extract_contract_terms_empty_text():
    result = agent.extract_contract_terms("")
    assert result["renewal_date"] is None
    assert result["contract_value"] is None
    assert result["confidence"] == pytest.approx(0.10)


def test_extract_contract_terms_llm_failure(mock_llm):
    _patch_llm_raise(mock_llm)
    result = agent.extract_contract_terms("Some contract text here about terms and renewal.")
    assert isinstance(result, dict)
    assert result["confidence"] == pytest.approx(0.10)


def test_extract_contract_terms_valid_response(mock_llm):
    _patch_llm(
        mock_llm,
        '{"doc_type": "MSA", "counterparty_name": "IBM Corp",'
        ' "renewal_date": "2026-01-01", "contract_value": 250000,'
        ' "auto_renewal": true, "baa_present": false}',
    )
    result = agent.extract_contract_terms(
        "Master Services Agreement between Buyer and IBM Corp...",
        vendor_name="IBM",
    )
    assert result["renewal_date"] == "2026-01-01"
    assert result["contract_value"] == 250000
    assert result["doc_type"] == "MSA"


def test_extract_contract_terms_non_null_count_drives_confidence(mock_llm):
    # 5 non-null fields: doc_type, counterparty_name, renewal_date, contract_value, auto_renewal
    # base = 5/17 ≈ 0.294
    # +0.10 (doc_type != UNKNOWN)
    # +0.05 (IBM in IBM Corp)
    # total ≈ 0.444
    _patch_llm(
        mock_llm,
        '{"doc_type": "MSA", "counterparty_name": "IBM Corp",'
        ' "renewal_date": "2026-01-01", "contract_value": 250000, "auto_renewal": true}',
    )
    result = agent.extract_contract_terms("contract text", vendor_name="IBM")
    assert result["confidence"] > 0.10
    assert result["confidence"] <= 0.97


def test_extract_contract_terms_confidence_capped_at_0_97(mock_llm):
    # All 17 fields non-null → base=1.0, +0.10 +0.05 would exceed 1 → capped at 0.97
    all_fields = (
        '{"doc_type": "MSA", "counterparty_name": "IBM", "renewal_date": "2026-01-01",'
        ' "opt_out_deadline": "2025-10-01", "auto_renewal": true, "contract_value": 100000,'
        ' "contract_value_type": "FIXED", "price_escalation": false, "escalation_rate_max": 3.0,'
        ' "payment_terms": "NET30", "early_termination": "PERMITTED", "liability_cap": 500000,'
        ' "baa_present": true, "phi_scope": "PHI", "nda_active": true,'
        ' "nda_expiry": "2027-01-01", "sla_uptime_pct": 99.9}'
    )
    _patch_llm(mock_llm, all_fields)
    result = agent.extract_contract_terms("full contract text", vendor_name="IBM")
    assert result["confidence"] == pytest.approx(0.97)


# ---------------------------------------------------------------------------
# consolidate_documents
# ---------------------------------------------------------------------------

def test_consolidate_empty_list():
    result = agent.consolidate_documents([])
    assert result["effective_terms"] == {}
    assert result["conflicts_detected"] == []


def test_consolidate_single_doc():
    doc = {"doc_type": "MSA", "renewal_date": "2025-01-01", "contract_value": 50000}
    result = agent.consolidate_documents([doc])
    assert result["effective_terms"]["renewal_date"] == "2025-01-01"
    assert result["effective_terms"]["contract_value"] == 50000
    assert result["conflicts_detected"] == []


def test_consolidate_amendment_overrides_msa():
    msa = {"doc_type": "MSA", "renewal_date": "2024-01-01", "payment_terms": "NET30"}
    amendment = {"doc_type": "AMENDMENT", "renewal_date": "2027-06-01"}
    result = agent.consolidate_documents([msa, amendment])
    assert result["effective_terms"]["renewal_date"] == "2027-06-01"
    assert "renewal_date" in result["conflicts_detected"]
    # payment_terms from MSA should still be present (no conflict)
    assert result["effective_terms"]["payment_terms"] == "NET30"


def test_consolidate_baa_any_true_wins():
    doc1 = {"doc_type": "MSA", "baa_present": False}
    doc2 = {"doc_type": "BAA", "baa_present": True}
    result = agent.consolidate_documents([doc1, doc2])
    assert result["effective_terms"]["baa_present"] is True


def test_consolidate_baa_false_does_not_override_true():
    doc1 = {"doc_type": "BAA", "baa_present": True}
    doc2 = {"doc_type": "AMENDMENT", "baa_present": False}
    result = agent.consolidate_documents([doc1, doc2])
    assert result["effective_terms"]["baa_present"] is True


def test_consolidate_conflict_detected():
    doc1 = {"doc_type": "MSA", "contract_value": 100000}
    doc2 = {"doc_type": "SOW", "contract_value": 150000}
    result = agent.consolidate_documents([doc1, doc2])
    assert "contract_value" in result["conflicts_detected"]


def test_consolidate_source_map_populated():
    doc = {"doc_type": "MSA", "renewal_date": "2025-06-01"}
    result = agent.consolidate_documents([doc])
    assert "renewal_date" in result["source_map"]
    assert result["source_map"]["renewal_date"] == "MSA"


# ---------------------------------------------------------------------------
# structure_entity
# ---------------------------------------------------------------------------

def test_structure_entity_empty_text():
    result = agent.structure_entity("IBM", "")
    assert result["legal_entity"] is None
    assert result["confidence"] == pytest.approx(0.20)


def test_structure_entity_whitespace_only():
    result = agent.structure_entity("IBM", "   ")
    assert result["confidence"] == pytest.approx(0.20)


def test_structure_entity_valid_response(mock_llm):
    _patch_llm(
        mock_llm,
        '{"legal_entity": "International Business Machines Corporation",'
        ' "ticker": "IBM", "company_status": "PUBLIC",'
        ' "hq_city": "Armonk", "hq_country": "US",'
        ' "acquisition_status": "ACTIVE", "category": "Technology"}',
    )
    result = agent.structure_entity("IBM", "IBM is a technology company headquartered in Armonk.")
    assert result["legal_entity"] == "International Business Machines Corporation"
    assert result["ticker"] == "IBM"
    assert "confidence" in result
    assert result["confidence"] > 0.20


def test_structure_entity_confidence_formula(mock_llm):
    # 4 non-null out of 12 entity fields
    # confidence = 4/12 * 0.55 + 0.40 = 0.183 + 0.40 = 0.583
    _patch_llm(
        mock_llm,
        '{"legal_entity": "IBM Corp", "ticker": "IBM",'
        ' "company_status": "PUBLIC", "hq_country": "US"}',
    )
    result = agent.structure_entity("IBM", "Some research text about IBM.")
    assert result["confidence"] == pytest.approx(4 / 12 * 0.55 + 0.40, abs=0.01)


def test_structure_entity_confidence_capped_at_0_92(mock_llm):
    # All 12 fields non-null → 12/12 * 0.55 + 0.40 = 0.95 → capped at 0.92
    _patch_llm(
        mock_llm,
        '{"legal_entity": "X", "ticker": "X", "parent_company": "X",'
        ' "acquired_by": "Y", "rebranded_to": "Z", "category": "X",'
        ' "subcategory": "X", "vendor_type": "X", "company_status": "PUBLIC",'
        ' "hq_city": "X", "hq_country": "X", "acquisition_status": "ACTIVE"}',
    )
    result = agent.structure_entity("X", "research text")
    assert result["confidence"] == pytest.approx(0.92)


def test_structure_entity_llm_failure(mock_llm):
    _patch_llm_raise(mock_llm)
    result = agent.structure_entity("IBM", "Research text about IBM Corporation.")
    assert result["legal_entity"] is None
    assert result["confidence"] == pytest.approx(0.20)


# ---------------------------------------------------------------------------
# score_confidence
# ---------------------------------------------------------------------------

def test_score_confidence_brain_lookup():
    assert agent.score_confidence(["erp"], "BRAIN_LOOKUP") == pytest.approx(0.97)


def test_score_confidence_web_research_one_source():
    assert agent.score_confidence(["web"], "WEB_RESEARCH") == pytest.approx(0.60)


def test_score_confidence_web_research_extra_source():
    # base=0.60, 1 extra source → 0.60 + 0.05 = 0.65
    assert agent.score_confidence(["web", "erp"], "WEB_RESEARCH") == pytest.approx(0.65)


def test_score_confidence_unknown_method():
    assert agent.score_confidence([], "MYSTERY") == pytest.approx(0.25)


def test_score_confidence_caps_at_0_99():
    many_sources = ["s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9", "s10"]
    result = agent.score_confidence(many_sources, "BRAIN_LOOKUP")
    assert result == pytest.approx(0.99)


# ---------------------------------------------------------------------------
# detect_contradictions
# ---------------------------------------------------------------------------

def test_detect_contradictions_empty():
    assert agent.detect_contradictions([]) == []


def test_detect_contradictions_single_item():
    assert agent.detect_contradictions([{"field": "value"}]) == []


def test_detect_contradictions_found():
    ev1 = {"source": "erp", "vendor_name": "IBM Corp", "spend": 100000}
    ev2 = {"source": "doc", "vendor_name": "IBM Inc", "spend": 100000}
    result = agent.detect_contradictions([ev1, ev2])
    fields = [r["field"] for r in result]
    assert "vendor_name" in fields
    assert "spend" not in fields  # same value — no contradiction


def test_detect_contradictions_same_values_no_result():
    ev1 = {"source": "a", "category": "IT"}
    ev2 = {"source": "b", "category": "IT"}
    assert agent.detect_contradictions([ev1, ev2]) == []


def test_detect_contradictions_result_structure():
    ev1 = {"source": "erp", "spend": 100000}
    ev2 = {"source": "doc", "spend": 200000}
    result = agent.detect_contradictions([ev1, ev2])
    assert len(result) == 1
    rec = result[0]
    assert rec["field"] == "spend"
    assert rec["value_a"] == 100000
    assert rec["value_b"] == 200000
    assert rec["source_a"] == "erp"
    assert rec["source_b"] == "doc"


def test_detect_contradictions_null_skipped():
    ev1 = {"source": "a", "renewal_date": None}
    ev2 = {"source": "b", "renewal_date": "2026-01-01"}
    result = agent.detect_contradictions([ev1, ev2])
    assert result == []


# ---------------------------------------------------------------------------
# detect_vendor_column
# ---------------------------------------------------------------------------

def test_detect_vendor_column_empty_headers():
    result = agent.detect_vendor_column([], [])
    assert result is None


def test_detect_vendor_column_llm_returns_valid_column(mock_llm):
    _patch_llm(mock_llm, '{"column": "Payee"}')
    headers = ["Ref No", "Payee", "Amount", "Dept"]
    sample = [["001", "Accenture", "50000", "IT"], ["002", "IBM Corp", "120000", "Finance"]]
    result = agent.detect_vendor_column(headers, sample)
    assert result == "Payee"


def test_detect_vendor_column_llm_returns_column_not_in_headers(mock_llm):
    _patch_llm(mock_llm, '{"column": "Supplier"}')
    headers = ["Ref No", "Payee", "Amount"]
    result = agent.detect_vendor_column(headers, [])
    assert result is None


def test_detect_vendor_column_llm_returns_null(mock_llm):
    _patch_llm(mock_llm, '{"column": null}')
    headers = ["A", "B", "C"]
    result = agent.detect_vendor_column(headers, [])
    assert result is None


def test_detect_vendor_column_llm_failure_returns_none(mock_llm):
    _patch_llm_raise(mock_llm)
    headers = ["Payee", "Amount"]
    result = agent.detect_vendor_column(headers, [["Accenture", "5000"]])
    assert result is None
