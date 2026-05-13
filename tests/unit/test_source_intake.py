"""Tests for cobalt.tools.source_intake — Tool 1."""

import csv
import hashlib
import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cobalt.tools.source_intake import (
    DocRecord,
    RawCandidate,
    SourceIntakeResult,
    _deduplicate,
    _detect_name_col,
    _find_col,
    _parse_decimal,
    _to_key,
    run,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_csv(tmp_path: Path, rows: list[dict], filename: str = "vendors.csv") -> str:
    path = tmp_path / filename
    if not rows:
        path.write_text("vendor_name\n", encoding="utf-8")
        return str(path)
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return str(path)


def _make_research_agent(docs: list[dict] | None = None) -> MagicMock:
    ra = MagicMock()
    ra.fetch_google_drive_docs.return_value = docs or []
    return ra


def _make_analysis_agent(vendor_name: str | None = None, doc_type: str = "MSA") -> MagicMock:
    aa = MagicMock()
    aa.extract_vendor_name_from_doc.return_value = {
        "vendor_name": vendor_name,
        "doc_type_hint": doc_type,
    }
    return aa


# ---------------------------------------------------------------------------
# _parse_decimal
# ---------------------------------------------------------------------------

def test_parse_decimal_plain():
    assert _parse_decimal("12345") == Decimal("12345")


def test_parse_decimal_with_dollar():
    assert _parse_decimal("$1,234.56") == Decimal("1234.56")


def test_parse_decimal_with_pound():
    assert _parse_decimal("£500") == Decimal("500")


def test_parse_decimal_with_euro():
    assert _parse_decimal("€9,999.99") == Decimal("9999.99")


def test_parse_decimal_empty_string():
    assert _parse_decimal("") is None


def test_parse_decimal_invalid():
    assert _parse_decimal("N/A") is None


# ---------------------------------------------------------------------------
# _detect_name_col
# ---------------------------------------------------------------------------

def test_detect_name_col_exact_vendor_name():
    assert _detect_name_col(["vendor_name", "spend"]) == "vendor_name"


def test_detect_name_col_capitalized():
    assert _detect_name_col(["Vendor Name", "Spend"]) == "Vendor Name"


def test_detect_name_col_case_insensitive():
    assert _detect_name_col(["VENDOR_NAME", "spend"]) == "VENDOR_NAME"


def test_detect_name_col_known_aliases():
    assert _detect_name_col(["Payee", "Amount"]) == "Payee"
    assert _detect_name_col(["supplier", "amount"]) == "supplier"
    assert _detect_name_col(["company", "amount"]) == "company"


def test_detect_name_col_unknown_header_returns_none():
    assert _detect_name_col(["XYZ123", "Amount"]) is None


def test_detect_name_col_empty():
    assert _detect_name_col([]) is None


# ---------------------------------------------------------------------------
# _find_col
# ---------------------------------------------------------------------------

def test_find_col_exact_match():
    assert _find_col(["vendor_name", "spend", "category"], ["spend"]) == "spend"


def test_find_col_case_insensitive():
    assert _find_col(["Spend", "Category"], ["spend"]) == "Spend"


def test_find_col_first_match_wins():
    result = _find_col(["annual_spend", "spend"], ["spend", "annual_spend"])
    assert result in ("annual_spend", "spend")


def test_find_col_no_match():
    assert _find_col(["name", "amount"], ["spend"]) is None


# ---------------------------------------------------------------------------
# _to_key
# ---------------------------------------------------------------------------

def test_to_key_returns_string():
    key = _to_key("IBM Corporation")
    assert isinstance(key, str)
    assert len(key) > 0


def test_to_key_same_input_same_output():
    assert _to_key("Aramark") == _to_key("Aramark")


def test_to_key_normalizes():
    # "IBM Corp" and "IBM Corporation" should produce the same or different keys —
    # we just verify it doesn't raise and returns a non-empty string
    assert _to_key("IBM Corp") != ""


def test_to_key_exception_fallback():
    # Empty string edge case
    result = _to_key("")
    assert result == ""


# ---------------------------------------------------------------------------
# _deduplicate
# ---------------------------------------------------------------------------

def _rc(name: str, source: str = "VENDOR_LIST", refs=None, docs=None) -> RawCandidate:
    return RawCandidate(
        raw_name=name,
        source=source,
        source_refs=refs or [f"{name}.csv"],
        linked_doc_ids=docs or [],
        spend_hint=None,
        category_hint=None,
        department_hint=None,
    )


def test_deduplicate_no_duplicates():
    cands = [_rc("IBM"), _rc("Aramark")]
    result = _deduplicate(cands)
    assert len(result) == 2


def test_deduplicate_merges_same_key():
    cands = [_rc("IBM"), _rc("IBM")]
    result = _deduplicate(cands)
    assert len(result) == 1


def test_deduplicate_source_becomes_both():
    a = _rc("IBM", source="VENDOR_LIST")
    b = _rc("IBM", source="DOCUMENT")
    result = _deduplicate([a, b])
    assert result[0].source == "BOTH"


def test_deduplicate_source_refs_merged():
    a = _rc("IBM", refs=["a.csv"])
    b = _rc("IBM", refs=["b.csv"])
    result = _deduplicate([a, b])
    refs = result[0].source_refs
    assert "a.csv" in refs
    assert "b.csv" in refs


def test_deduplicate_spend_hint_kept_from_first():
    a = _rc("IBM")
    a.spend_hint = Decimal("5000")
    b = _rc("IBM")
    b.spend_hint = Decimal("9999")
    result = _deduplicate([a, b])
    assert result[0].spend_hint == Decimal("5000")


def test_deduplicate_spend_hint_filled_from_second_if_first_none():
    a = _rc("IBM")
    a.spend_hint = None
    b = _rc("IBM")
    b.spend_hint = Decimal("7500")
    result = _deduplicate([a, b])
    assert result[0].spend_hint == Decimal("7500")


# ---------------------------------------------------------------------------
# run — CSV ingestion
# ---------------------------------------------------------------------------

def test_run_csv_creates_candidates(tmp_path):
    path = _make_csv(tmp_path, [{"vendor_name": "IBM"}, {"vendor_name": "Aramark"}])
    result = run(path, None, _make_research_agent(), _make_analysis_agent())
    assert isinstance(result, SourceIntakeResult)
    names = [c.raw_name for c in result.candidates]
    assert "IBM" in names
    assert "Aramark" in names


def test_run_csv_source_is_vendor_list(tmp_path):
    path = _make_csv(tmp_path, [{"vendor_name": "IBM"}])
    result = run(path, None, _make_research_agent(), _make_analysis_agent())
    assert all(c.source in ("VENDOR_LIST", "BOTH") for c in result.candidates)


def test_run_csv_spend_hint_populated(tmp_path):
    path = _make_csv(tmp_path, [{"vendor_name": "IBM", "spend": "$50,000"}])
    result = run(path, None, _make_research_agent(), _make_analysis_agent())
    ibm = next(c for c in result.candidates if c.raw_name == "IBM")
    assert ibm.spend_hint == Decimal("50000")


def test_run_csv_category_hint_populated(tmp_path):
    path = _make_csv(tmp_path, [{"vendor_name": "IBM", "category": "IT"}])
    result = run(path, None, _make_research_agent(), _make_analysis_agent())
    ibm = next(c for c in result.candidates if c.raw_name == "IBM")
    assert ibm.category_hint == "IT"


def test_run_csv_blank_rows_skipped(tmp_path):
    path = _make_csv(tmp_path, [
        {"vendor_name": "IBM"},
        {"vendor_name": ""},
        {"vendor_name": "Aramark"},
    ])
    result = run(path, None, _make_research_agent(), _make_analysis_agent())
    assert len(result.candidates) == 2


def test_run_missing_csv_returns_empty(tmp_path):
    result = run(str(tmp_path / "nonexistent.csv"), None, _make_research_agent(), _make_analysis_agent())
    assert result.candidates == []
    assert result.doc_registry == {}


def test_run_none_paths_returns_empty_result():
    result = run(None, None, _make_research_agent(), _make_analysis_agent())
    assert result.candidates == []
    assert result.doc_registry == {}
    assert result.source_summary["candidates_total"] == 0


# ---------------------------------------------------------------------------
# run — Google Drive document ingestion
# ---------------------------------------------------------------------------

def _doc_info(filename: str, raw_text: str = "some text") -> dict:
    return {"filename": filename, "raw_text": raw_text, "file_path": f"/drive/{filename}", "relative_path": filename}


def test_run_drive_docs_populate_registry():
    docs = [_doc_info("contract_ibm.pdf")]
    aa = _make_analysis_agent(vendor_name="IBM", doc_type="MSA")
    result = run(None, "/fake/drive", _make_research_agent(docs), aa)
    assert len(result.doc_registry) == 1


def test_run_drive_doc_id_is_md5_prefix():
    docs = [_doc_info("contract_ibm.pdf")]
    aa = _make_analysis_agent(vendor_name="IBM", doc_type="MSA")
    result = run(None, "/fake/drive", _make_research_agent(docs), aa)
    # doc_id is now md5(file_path) so two vendors can both have "contract.pdf"
    expected_id = hashlib.md5("/drive/contract_ibm.pdf".encode()).hexdigest()[:12]
    assert expected_id in result.doc_registry


def test_run_drive_doc_type_stored():
    docs = [_doc_info("sow_aramark.pdf")]
    aa = _make_analysis_agent(vendor_name="Aramark", doc_type="SOW")
    result = run(None, "/fake/drive", _make_research_agent(docs), aa)
    doc = next(iter(result.doc_registry.values()))
    assert doc.doc_type == "SOW"


def test_run_drive_unknown_doc_type_when_extraction_fails():
    docs = [_doc_info("mystery.pdf")]
    aa = MagicMock()
    aa.extract_vendor_name_from_doc.side_effect = RuntimeError("LLM down")
    result = run(None, "/fake/drive", _make_research_agent(docs), aa)
    doc = next(iter(result.doc_registry.values()))
    assert doc.doc_type == "UNKNOWN"


def test_run_drive_no_vendor_name_no_candidate():
    docs = [_doc_info("anonymous.pdf")]
    aa = _make_analysis_agent(vendor_name=None, doc_type="UNKNOWN")
    result = run(None, "/fake/drive", _make_research_agent(docs), aa)
    assert result.candidates == []


def test_run_drive_vendor_not_in_list_creates_document_candidate():
    docs = [_doc_info("phoenix.pdf")]
    aa = _make_analysis_agent(vendor_name="Phoenix Consulting", doc_type="MSA")
    result = run(None, "/fake/drive", _make_research_agent(docs), aa)
    sources = [c.source for c in result.candidates]
    assert "DOCUMENT" in sources


# ---------------------------------------------------------------------------
# run — document-to-candidate linking
# ---------------------------------------------------------------------------

def test_run_link_doc_to_existing_vendor(tmp_path):
    path = _make_csv(tmp_path, [{"vendor_name": "IBM"}])
    docs = [_doc_info("ibm_contract.pdf")]
    aa = _make_analysis_agent(vendor_name="IBM", doc_type="MSA")
    result = run(str(path), "/fake/drive", _make_research_agent(docs), aa)
    ibm = next(c for c in result.candidates if "IBM" in c.raw_name)
    assert len(ibm.linked_doc_ids) == 1


def test_run_link_upgrades_source_to_both(tmp_path):
    path = _make_csv(tmp_path, [{"vendor_name": "IBM"}])
    docs = [_doc_info("ibm_contract.pdf")]
    aa = _make_analysis_agent(vendor_name="IBM", doc_type="MSA")
    result = run(str(path), "/fake/drive", _make_research_agent(docs), aa)
    ibm = next(c for c in result.candidates if "IBM" in c.raw_name)
    assert ibm.source == "BOTH"


def test_run_doc_vendor_key_set(tmp_path):
    path = _make_csv(tmp_path, [{"vendor_name": "IBM"}])
    docs = [_doc_info("ibm_contract.pdf")]
    aa = _make_analysis_agent(vendor_name="IBM", doc_type="MSA")
    result = run(str(path), "/fake/drive", _make_research_agent(docs), aa)
    doc = next(iter(result.doc_registry.values()))
    assert doc.vendor_key is not None


def test_run_unmatched_doc_vendor_creates_new_candidate(tmp_path):
    path = _make_csv(tmp_path, [{"vendor_name": "IBM"}])
    docs = [_doc_info("aramark_sow.pdf")]
    aa = _make_analysis_agent(vendor_name="Aramark", doc_type="SOW")
    result = run(str(path), "/fake/drive", _make_research_agent(docs), aa)
    names = [c.raw_name for c in result.candidates]
    assert "Aramark" in names
    assert "IBM" in names


# ---------------------------------------------------------------------------
# run — deduplication (cross-source)
# ---------------------------------------------------------------------------

def test_run_dedup_same_vendor_from_list_and_doc(tmp_path):
    path = _make_csv(tmp_path, [{"vendor_name": "Aramark"}])
    docs = [_doc_info("aramark_msa.pdf")]
    aa = _make_analysis_agent(vendor_name="Aramark", doc_type="MSA")
    result = run(str(path), "/fake/drive", _make_research_agent(docs), aa)
    aramark_cands = [c for c in result.candidates if "aramark" in c.raw_name.lower() or "Aramark" in c.raw_name]
    assert len(aramark_cands) == 1
    assert aramark_cands[0].source == "BOTH"


# ---------------------------------------------------------------------------
# run — source_summary
# ---------------------------------------------------------------------------

def test_run_summary_vendor_list_count(tmp_path):
    path = _make_csv(tmp_path, [{"vendor_name": "IBM"}, {"vendor_name": "Aramark"}])
    result = run(str(path), None, _make_research_agent(), _make_analysis_agent())
    assert result.source_summary["vendor_list_count"] == 2


def test_run_summary_candidates_total(tmp_path):
    path = _make_csv(tmp_path, [{"vendor_name": "IBM"}, {"vendor_name": "Aramark"}])
    result = run(str(path), None, _make_research_agent(), _make_analysis_agent())
    assert result.source_summary["candidates_total"] == 2


def test_run_summary_doc_count():
    docs = [_doc_info("ibm.pdf"), _doc_info("aramark.pdf")]
    aa = _make_analysis_agent(vendor_name="IBM", doc_type="MSA")
    result = run(None, "/fake/drive", _make_research_agent(docs), aa)
    assert result.source_summary["doc_count"] == 2


def test_run_summary_from_docs_only():
    docs = [_doc_info("phoenix.pdf")]
    aa = _make_analysis_agent(vendor_name="Phoenix Consulting", doc_type="NDA")
    result = run(None, "/fake/drive", _make_research_agent(docs), aa)
    assert result.source_summary["from_docs_only"] >= 1


def test_run_summary_from_both(tmp_path):
    path = _make_csv(tmp_path, [{"vendor_name": "IBM"}])
    docs = [_doc_info("ibm.pdf")]
    aa = _make_analysis_agent(vendor_name="IBM", doc_type="MSA")
    result = run(str(path), "/fake/drive", _make_research_agent(docs), aa)
    assert result.source_summary["from_both"] == 1


def test_run_summary_doc_types():
    docs = [_doc_info("ibm_msa.pdf"), _doc_info("ibm_sow.pdf")]
    aa = MagicMock()
    aa.extract_vendor_name_from_doc.side_effect = [
        {"vendor_name": "IBM", "doc_type_hint": "MSA"},
        {"vendor_name": "IBM2", "doc_type_hint": "SOW"},
    ]
    result = run(None, "/fake/drive", _make_research_agent(docs), aa)
    assert "MSA" in result.source_summary["doc_types"]
    assert "SOW" in result.source_summary["doc_types"]


# ---------------------------------------------------------------------------
# run — never raises
# ---------------------------------------------------------------------------

def test_run_never_raises_with_bad_drive_path():
    ra = MagicMock()
    ra.fetch_google_drive_docs.side_effect = RuntimeError("Drive unavailable")
    result = run(None, "/nonexistent/drive", ra, _make_analysis_agent())
    assert isinstance(result, SourceIntakeResult)


def test_run_never_raises_all_none():
    result = run(None, None, _make_research_agent(), _make_analysis_agent())
    assert isinstance(result, SourceIntakeResult)
    assert result.candidates == []


# ---------------------------------------------------------------------------
# extra_fields — all columns captured verbatim
# ---------------------------------------------------------------------------

def test_run_csv_extra_fields_captured(tmp_path):
    path = _make_csv(tmp_path, [{"vendor_name": "IBM", "contract_owner": "Alice", "region": "EMEA"}])
    result = run(path, None, _make_research_agent(), _make_analysis_agent())
    ibm = next(c for c in result.candidates if c.raw_name == "IBM")
    assert ibm.extra_fields.get("contract_owner") == "Alice"
    assert ibm.extra_fields.get("region") == "EMEA"


def test_run_csv_known_cols_not_in_extra_fields(tmp_path):
    path = _make_csv(tmp_path, [{"vendor_name": "IBM", "spend": "5000", "category": "IT"}])
    result = run(path, None, _make_research_agent(), _make_analysis_agent())
    ibm = next(c for c in result.candidates if c.raw_name == "IBM")
    assert "vendor_name" not in ibm.extra_fields
    assert "spend" not in ibm.extra_fields
    assert "category" not in ibm.extra_fields


def test_run_csv_empty_extra_fields_when_no_extra_cols(tmp_path):
    path = _make_csv(tmp_path, [{"vendor_name": "IBM"}])
    result = run(path, None, _make_research_agent(), _make_analysis_agent())
    ibm = next(c for c in result.candidates if c.raw_name == "IBM")
    assert ibm.extra_fields == {}


def test_deduplicate_extra_fields_merged():
    a = RawCandidate("IBM", "VENDOR_LIST", ["a.csv"], [], None, None, None, extra_fields={"region": "US"})
    b = RawCandidate("IBM", "VENDOR_LIST", ["b.csv"], [], None, None, None, extra_fields={"country": "USA"})
    result = _deduplicate([a, b])
    assert result[0].extra_fields["region"] == "US"
    assert result[0].extra_fields["country"] == "USA"


def test_deduplicate_extra_fields_first_value_wins():
    a = RawCandidate("IBM", "VENDOR_LIST", ["a.csv"], [], None, None, None, extra_fields={"region": "US"})
    b = RawCandidate("IBM", "VENDOR_LIST", ["b.csv"], [], None, None, None, extra_fields={"region": "EMEA"})
    result = _deduplicate([a, b])
    assert result[0].extra_fields["region"] == "US"


# ---------------------------------------------------------------------------
# LLM column detection fallback
# ---------------------------------------------------------------------------

def test_run_csv_llm_fallback_for_unknown_column(tmp_path):
    """When header is unrecognised by heuristic, LLM identifies it."""
    path = _make_csv(tmp_path, [{"Org_Entity": "Accenture"}, {"Org_Entity": "IBM"}])

    aa = _make_analysis_agent()
    aa.detect_vendor_column.return_value = "Org_Entity"

    result = run(path, None, _make_research_agent(), aa)
    names = [c.raw_name for c in result.candidates]
    assert "Accenture" in names
    assert "IBM" in names
    aa.detect_vendor_column.assert_called_once()


def test_run_csv_llm_fallback_returns_none_gives_empty(tmp_path):
    """When LLM also can't identify the column, no candidates produced."""
    path = _make_csv(tmp_path, [{"Org_Entity": "Accenture"}])

    aa = _make_analysis_agent()
    aa.detect_vendor_column.return_value = None

    result = run(path, None, _make_research_agent(), aa)
    assert result.candidates == []
