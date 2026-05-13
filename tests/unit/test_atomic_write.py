"""Tests for cobalt.core.atomic_write."""

import pytest
import yaml
from pathlib import Path

from cobalt.core.atomic_write import atomic_write, append_md
from cobalt.core.exceptions import (
    FileOwnershipViolation,
    LedgerWriteError,
    SchemaValidationError,
)


def test_dict_written_as_markdown_with_frontmatter(tmp_workspace):
    path = tmp_workspace / "test.md"
    data = {"name": "acme", "value": 42}
    atomic_write(path, data)
    content = path.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    parts = content.split("---\n", 2)
    loaded = yaml.safe_load(parts[1])
    assert loaded == data


def test_tmp_file_does_not_exist_after_write(tmp_workspace):
    path = tmp_workspace / "test.md"
    atomic_write(path, {"x": 1})
    tmp = Path(str(path) + ".tmp")
    assert not tmp.exists()


def test_input_name_change_raises_file_ownership_violation(tmp_workspace):
    path = tmp_workspace / "entity.md"
    atomic_write(path, {"input_name": "original", "other": "data"})
    with pytest.raises(FileOwnershipViolation):
        atomic_write(path, {"input_name": "changed", "other": "data"})


def test_input_name_unchanged_on_entity_md_succeeds(tmp_workspace):
    path = tmp_workspace / "entity.md"
    atomic_write(path, {"input_name": "same", "status": "active"})
    atomic_write(path, {"input_name": "same", "status": "updated"})
    content = path.read_text(encoding="utf-8")
    parts = content.split("---\n", 2)
    loaded = yaml.safe_load(parts[1])
    assert loaded["status"] == "updated"
    assert loaded["input_name"] == "same"


def test_schema_validator_failure_raises_and_leaves_original(tmp_workspace):
    path = tmp_workspace / "doc.md"
    original_content = {"keep": "this"}
    atomic_write(path, original_content)

    def bad_validator(content):
        raise SchemaValidationError("invalid schema")

    with pytest.raises(SchemaValidationError):
        atomic_write(path, {"keep": "something else"}, schema_validator=bad_validator)

    # Original must be untouched — parse front-matter
    content = path.read_text(encoding="utf-8")
    parts = content.split("---\n", 2)
    loaded = yaml.safe_load(parts[1])
    assert loaded == original_content

    # .tmp must be cleaned up
    tmp = Path(str(path) + ".tmp")
    assert not tmp.exists()


def test_append_md_adds_separator_between_entries(tmp_workspace):
    path = tmp_workspace / "ledger.md"
    append_md(path, "entry one")
    append_md(path, "entry two")
    text = path.read_text(encoding="utf-8")
    assert "---" in text
    assert "entry one" in text
    assert "entry two" in text


def test_append_md_oserror_raises_ledger_write_error(tmp_workspace, monkeypatch):
    path = tmp_workspace / "ledger.md"

    import builtins

    real_open = builtins.open

    def patched_open(file, mode="r", **kwargs):
        if "a" in mode and str(file) == str(path):
            raise OSError("disk full")
        return real_open(file, mode, **kwargs)

    monkeypatch.setattr(builtins, "open", patched_open)

    with pytest.raises(LedgerWriteError):
        append_md(path, "this should fail")
