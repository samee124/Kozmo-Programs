"""Tests for cobalt.db.queries."""

import logging
from datetime import datetime
from unittest.mock import MagicMock

import pytest

import cobalt.db.queries as queries_module
from cobalt.db.queries import get_due_vendors, insert_vendor, update_vendor_status


def _make_mock_factory(rows=None):
    """Return a mock session factory."""
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)

    if rows is not None:
        mock_result = MagicMock()
        mock_result.fetchall.return_value = rows
        mock_session.execute.return_value = mock_result

    return MagicMock(return_value=mock_session), mock_session


def test_get_due_vendors_returns_list_of_strings(monkeypatch):
    mock_factory, _ = _make_mock_factory(rows=[("v001",), ("v002",)])
    monkeypatch.setattr(queries_module, "_get_session_factory", lambda: mock_factory)

    result = get_due_vendors("p001")
    assert result == ["v001", "v002"]
    assert all(isinstance(v, str) for v in result)


def test_get_due_vendors_returns_empty_when_no_db_url(monkeypatch, caplog):
    monkeypatch.setattr(queries_module, "_get_session_factory", lambda: None)

    with caplog.at_level(logging.WARNING):
        result = get_due_vendors("p001")

    assert result == []
    assert any("DATABASE_URL" in r.message or "not set" in r.message for r in caplog.records)


def test_insert_vendor_runs_without_error(monkeypatch):
    mock_factory, mock_session = _make_mock_factory()
    monkeypatch.setattr(queries_module, "_get_session_factory", lambda: mock_factory)

    insert_vendor(
        vendor_id="v001",
        programme_id="p001",
        vendor_name="Acme Ltd",
        input_name="acme ltd",
    )
    # No exception raised is the contract


def test_insert_vendor_silent_when_no_db_url(monkeypatch, caplog):
    monkeypatch.setattr(queries_module, "_get_session_factory", lambda: None)

    with caplog.at_level(logging.WARNING):
        insert_vendor(
            vendor_id="v001",
            programme_id="p001",
            vendor_name="Acme",
            input_name="acme",
        )
    assert any("DATABASE_URL" in r.message or "not set" in r.message for r in caplog.records)


def test_update_vendor_status_runs_without_error(monkeypatch):
    mock_factory, mock_session = _make_mock_factory()
    monkeypatch.setattr(queries_module, "_get_session_factory", lambda: mock_factory)

    update_vendor_status("v001", "CHECKIN_SENT")
    mock_session.execute.assert_called_once()
    mock_session.commit.assert_called_once()


def test_update_vendor_status_with_next_action_due(monkeypatch):
    mock_factory, mock_session = _make_mock_factory()
    monkeypatch.setattr(queries_module, "_get_session_factory", lambda: mock_factory)

    update_vendor_status("v001", "NEEDS_ACTION", next_action_due=datetime(2026, 6, 1))
    mock_session.execute.assert_called_once()


def test_update_vendor_status_silent_when_no_db_url(monkeypatch, caplog):
    monkeypatch.setattr(queries_module, "_get_session_factory", lambda: None)

    with caplog.at_level(logging.WARNING):
        update_vendor_status("v001", "COMPLETE")
    assert any("DATABASE_URL" in r.message or "not set" in r.message for r in caplog.records)
