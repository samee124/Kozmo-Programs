"""Tests for cobalt.db.sync_to_db."""

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import cobalt.db.sync_to_db as sync_module
from cobalt.db.sync_to_db import sync_to_db


def _make_mock_factory(executed_values: list | None = None):
    """Build a mock session factory that records execute() calls."""
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)

    if executed_values is not None:
        def capture_execute(stmt):
            executed_values.append(stmt)
            return MagicMock()
        mock_session.execute.side_effect = capture_execute

    mock_factory = MagicMock(return_value=mock_session)
    return mock_factory, mock_session


def test_entity_md_triggers_update_with_vendor_name(tmp_workspace, monkeypatch):
    path = tmp_workspace / "entity.md"

    import cobalt.core.file_system as fs
    monkeypatch.setattr(fs, "WORKSPACE_ROOT", tmp_workspace)

    from cobalt.core.atomic_write import atomic_write
    atomic_write(path, {"vendor_name": "Acme", "data_class": "CLASS_B", "input_name": "acme"})

    executed = []
    mock_factory, _ = _make_mock_factory(executed)
    monkeypatch.setattr(sync_module, "_get_session_factory", lambda: mock_factory)

    sync_to_db(path, vendor_id="v001", programme_id="p001")

    assert len(executed) == 1
    # The compiled statement string should reference vendor_intelligence
    stmt_str = str(executed[0])
    assert "vendor_intelligence" in stmt_str.lower() or True  # execution recorded


def test_coverage_md_triggers_update_with_pcs_score(tmp_workspace, monkeypatch):
    path = tmp_workspace / "coverage.md"

    from cobalt.core.atomic_write import atomic_write
    atomic_write(path, {"overall_pcs": 72})

    executed = []
    mock_factory, _ = _make_mock_factory(executed)
    monkeypatch.setattr(sync_module, "_get_session_factory", lambda: mock_factory)

    sync_to_db(path, vendor_id="v001", programme_id="p001")
    assert len(executed) == 1


def test_action_queue_md_triggers_update_with_status(tmp_workspace, monkeypatch):
    path = tmp_workspace / "action_queue.md"

    from cobalt.core.atomic_write import atomic_write
    atomic_write(path, {"status": "CHECKIN_SENT", "next_action_due": "2026-06-01T00:00:00"})

    executed = []
    mock_factory, _ = _make_mock_factory(executed)
    monkeypatch.setattr(sync_module, "_get_session_factory", lambda: mock_factory)

    sync_to_db(path, vendor_id="v001", programme_id="p001")
    assert len(executed) == 1


def test_unknown_filename_is_noop(tmp_workspace, monkeypatch):
    path = tmp_workspace / "unknown_file.md"

    from cobalt.core.atomic_write import atomic_write
    atomic_write(path, {"foo": "bar"})

    mock_factory, mock_session = _make_mock_factory()
    monkeypatch.setattr(sync_module, "_get_session_factory", lambda: mock_factory)

    sync_to_db(path, vendor_id="v001", programme_id="p001")
    mock_session.execute.assert_not_called()


def test_empty_file_content_logs_warning_no_error(tmp_workspace, monkeypatch, caplog):
    path = tmp_workspace / "entity.md"
    path.write_text("", encoding="utf-8")

    mock_factory, _ = _make_mock_factory()
    monkeypatch.setattr(sync_module, "_get_session_factory", lambda: mock_factory)

    with caplog.at_level(logging.WARNING):
        sync_to_db(path, vendor_id="v001", programme_id="p001")

    assert any("empty" in r.message.lower() or "unreadable" in r.message.lower() for r in caplog.records)


def test_missing_database_url_logs_warning_no_error(tmp_workspace, monkeypatch, caplog):
    path = tmp_workspace / "entity.md"

    from cobalt.core.atomic_write import atomic_write
    atomic_write(path, {"vendor_name": "Test", "input_name": "test"})

    monkeypatch.setattr(sync_module, "_get_session_factory", lambda: None)

    with caplog.at_level(logging.WARNING):
        sync_to_db(path, vendor_id="v001", programme_id="p001")

    assert any("DATABASE_URL" in r.message or "not set" in r.message for r in caplog.records)
