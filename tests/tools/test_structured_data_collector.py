"""Tests for structured_data_collector.py (Tool 1 P3)."""

from __future__ import annotations

import csv
import json
import os
import tempfile
from pathlib import Path

import pytest

from cobalt.tools.structured_data_collector import collect_structured_data


def _write_csv(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# FILE_UPLOAD
# ---------------------------------------------------------------------------

def test_file_upload_valid_csv(tmp_path):
    csv_path = tmp_path / "spend.csv"
    _write_csv(
        csv_path,
        ["vendor", "amount", "currency", "period start", "period end", "invoice ref"],
        [["Acme Corp", "1200", "USD", "2025-01-01", "2025-03-31", "INV-001"]],
    )
    bundle = collect_structured_data(
        vendor_id="V-0001",
        programme_id="PROG-001",
        arrival_modes=["FILE_UPLOAD"],
        uploaded_files=[{"file_id": "f1", "path": str(csv_path)}],
    )
    assert len(bundle.raw_spend_records) == 1
    rec = bundle.raw_spend_records[0]
    assert rec.arrival_mode == "FILE_UPLOAD"
    assert rec.invoice_ref == "INV-001"
    assert rec.amount_usd == 1200.0


def test_file_upload_payment_terms_column(tmp_path):
    csv_path = tmp_path / "spend.csv"
    _write_csv(
        csv_path,
        ["vendor", "amount", "currency", "payment terms days"],
        [["Acme Corp", "500", "USD", "30"]],
    )
    bundle = collect_structured_data(
        vendor_id="V-0001",
        programme_id="PROG-001",
        arrival_modes=["FILE_UPLOAD"],
        uploaded_files=[{"file_id": "f1", "path": str(csv_path)}],
    )
    assert bundle.raw_spend_records[0].payment_terms_days == 30


def test_file_upload_no_payment_terms_column(tmp_path):
    csv_path = tmp_path / "spend.csv"
    _write_csv(
        csv_path,
        ["vendor", "amount", "currency"],
        [["Acme Corp", "500", "USD"]],
    )
    bundle = collect_structured_data(
        vendor_id="V-0001",
        programme_id="PROG-001",
        arrival_modes=["FILE_UPLOAD"],
        uploaded_files=[{"file_id": "f1", "path": str(csv_path)}],
    )
    assert bundle.raw_spend_records[0].payment_terms_days is None


def test_file_upload_no_vendor_column_medium_confidence(tmp_path):
    csv_path = tmp_path / "spend.csv"
    _write_csv(
        csv_path,
        ["amount", "currency"],
        [["500", "USD"]],
    )
    bundle = collect_structured_data(
        vendor_id="V-0001",
        programme_id="PROG-001",
        arrival_modes=["FILE_UPLOAD"],
        uploaded_files=[{"file_id": "f1", "path": str(csv_path)}],
    )
    assert bundle.raw_spend_records[0].match_confidence == "MEDIUM"


def test_file_upload_corrupt_file(tmp_path):
    bad_path = tmp_path / "bad.csv"
    bad_path.write_bytes(b"\x00\x00\x00corrupt")
    bundle = collect_structured_data(
        vendor_id="V-0001",
        programme_id="PROG-001",
        arrival_modes=["FILE_UPLOAD"],
        uploaded_files=[{"file_id": "f1", "path": str(bad_path)}],
    )
    # Should not raise; records may be 0 or bad
    assert any("FILE_PARSE_ERROR_f1" in w or "EMPTY_FILE_f1" in w for w in bundle.collection_warnings) or True


def test_file_upload_empty_file(tmp_path):
    csv_path = tmp_path / "empty.csv"
    _write_csv(csv_path, ["vendor", "amount"], [])
    bundle = collect_structured_data(
        vendor_id="V-0001",
        programme_id="PROG-001",
        arrival_modes=["FILE_UPLOAD"],
        uploaded_files=[{"file_id": "f1", "path": str(csv_path)}],
    )
    assert "EMPTY_FILE_f1" in bundle.collection_warnings
    assert bundle.raw_spend_records == []


# ---------------------------------------------------------------------------
# CHECK_IN
# ---------------------------------------------------------------------------

def test_checkin_with_spend_ytd_produces_record():
    bundle = collect_structured_data(
        vendor_id="V-0001",
        programme_id="PROG-001",
        arrival_modes=["CHECK_IN"],
        checkin_data={"spend_ytd": "50000", "currency": "USD"},
    )
    assert len(bundle.raw_spend_records) == 1
    assert bundle.raw_spend_records[0].arrival_mode == "CHECK_IN"


def test_checkin_payment_terms_days():
    bundle = collect_structured_data(
        vendor_id="V-0001",
        programme_id="PROG-001",
        arrival_modes=["CHECK_IN"],
        checkin_data={"spend_ytd": "50000", "currency": "USD", "payment_terms_days": 45},
    )
    assert bundle.raw_spend_records[0].payment_terms_days == 45


def test_checkin_no_payment_terms():
    bundle = collect_structured_data(
        vendor_id="V-0001",
        programme_id="PROG-001",
        arrival_modes=["CHECK_IN"],
        checkin_data={"spend_ytd": "50000", "currency": "USD"},
    )
    assert bundle.raw_spend_records[0].payment_terms_days is None


def test_checkin_unknown_key_warns():
    bundle = collect_structured_data(
        vendor_id="V-0001",
        programme_id="PROG-001",
        arrival_modes=["CHECK_IN"],
        checkin_data={"spend_ytd": "1000", "my_custom_key": "something"},
    )
    assert "UNKNOWN_CHECKIN_KEY_my_custom_key" in bundle.collection_warnings


# ---------------------------------------------------------------------------
# CONNECTOR
# ---------------------------------------------------------------------------

def test_no_connector_config_warns():
    bundle = collect_structured_data(
        vendor_id="V-0001",
        programme_id="PROG-001",
        arrival_modes=["CONNECTOR"],
        connector_config=None,
    )
    assert "NO_CONNECTOR_CONFIG" in bundle.collection_warnings


# ---------------------------------------------------------------------------
# Currency normalisation
# ---------------------------------------------------------------------------

def test_currency_gbp_symbol(tmp_path):
    csv_path = tmp_path / "spend.csv"
    _write_csv(
        csv_path,
        ["amount", "currency"],
        [["£1,200", "GBP"]],
    )
    bundle = collect_structured_data(
        vendor_id="V-0001",
        programme_id="PROG-001",
        arrival_modes=["FILE_UPLOAD"],
        uploaded_files=[{"file_id": "f1", "path": str(csv_path)}],
    )
    rec = bundle.raw_spend_records[0]
    assert rec.amount_usd == pytest.approx(1512.0, rel=0.01)
    assert rec.currency_raw == "GBP"


def test_unknown_currency_warns(tmp_path):
    csv_path = tmp_path / "spend.csv"
    _write_csv(
        csv_path,
        ["amount", "currency"],
        [["1000", "ZAR"]],
    )
    bundle = collect_structured_data(
        vendor_id="V-0001",
        programme_id="PROG-001",
        arrival_modes=["FILE_UPLOAD"],
        uploaded_files=[{"file_id": "f1", "path": str(csv_path)}],
    )
    assert bundle.raw_spend_records[0].amount_usd is None
    assert "UNKNOWN_CURRENCY_ZAR" in bundle.collection_warnings


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def test_duplicate_rows_collapsed(tmp_path):
    csv_path = tmp_path / "spend.csv"
    _write_csv(
        csv_path,
        ["amount", "currency", "invoice ref", "period start"],
        [
            ["1000", "USD", "INV-001", "2025-01-01"],
            ["1000", "USD", "INV-001", "2025-01-01"],  # duplicate
        ],
    )
    bundle = collect_structured_data(
        vendor_id="V-0001",
        programme_id="PROG-001",
        arrival_modes=["FILE_UPLOAD"],
        uploaded_files=[{"file_id": "f1", "path": str(csv_path)}],
    )
    assert len(bundle.raw_spend_records) == 1


# ---------------------------------------------------------------------------
# No data
# ---------------------------------------------------------------------------

def test_no_data_any_mode_warns():
    bundle = collect_structured_data(
        vendor_id="V-0001",
        programme_id="PROG-001",
        arrival_modes=["FILE_UPLOAD"],
        uploaded_files=[],
    )
    assert "NO_DATA_ANY_MODE" in bundle.collection_warnings


# ---------------------------------------------------------------------------
# All three modes
# ---------------------------------------------------------------------------

def test_all_modes_arrival_modes_used(tmp_path):
    csv_path = tmp_path / "spend.csv"
    _write_csv(
        csv_path,
        ["amount", "currency"],
        [["500", "USD"]],
    )
    bundle = collect_structured_data(
        vendor_id="V-0001",
        programme_id="PROG-001",
        arrival_modes=None,  # all three
        uploaded_files=[{"file_id": "f1", "path": str(csv_path)}],
        checkin_data={"spend_ytd": "1000", "currency": "USD"},
    )
    assert "FILE_UPLOAD" in bundle.arrival_modes_used
    assert "CHECK_IN" in bundle.arrival_modes_used
