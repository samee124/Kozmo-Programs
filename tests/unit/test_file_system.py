"""Tests for cobalt.core.file_system."""

import pytest
import yaml
from pathlib import Path

import cobalt.core.file_system as fs


def test_vendor_path_returns_correct_path(tmp_workspace):
    result = fs.vendor_path("prog1", "vendor_a")
    assert result == tmp_workspace / "prog1" / "vendor_a"


def test_init_vendor_workspace_creates_root_dir(tmp_workspace):
    fs.init_vendor_workspace("prog1", "vendor_a")
    root = fs.vendor_path("prog1", "vendor_a")
    assert root.is_dir()
    # Single-file architecture: no subdirectories are created
    subdirs = [p for p in root.iterdir() if p.is_dir()]
    assert subdirs == []


def test_init_programme_workspace_creates_required_dirs(tmp_workspace):
    fs.init_programme_workspace("prog1")
    root = fs.programme_path("prog1")
    assert (root / "programme_run").is_dir()
    assert (root / "programme_run" / "intake_plans").is_dir()
    assert (root / "checkpoint").is_dir()


def test_write_md_creates_markdown_with_frontmatter(tmp_workspace):
    path = tmp_workspace / "test.md"
    data = {"key": "value", "num": 7}
    fs.write_md(path, data)
    content = path.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    parts = content.split("---\n", 2)
    loaded = yaml.safe_load(parts[1])
    assert loaded == data


def test_read_md_returns_dict_from_valid_file(tmp_workspace):
    path = tmp_workspace / "test.md"
    data = {"a": 1, "b": "hello"}
    fs.write_md(path, data)
    result = fs.read_md(path)
    assert result == data


def test_read_md_returns_none_for_missing_file(tmp_workspace):
    path = tmp_workspace / "nonexistent.md"
    assert fs.read_md(path) is None


def test_read_md_returns_none_for_empty_file(tmp_workspace):
    path = tmp_workspace / "empty.md"
    path.write_text("", encoding="utf-8")
    assert fs.read_md(path) is None


def test_md_exists_true_for_existing_file(tmp_workspace):
    path = tmp_workspace / "exists.md"
    path.write_text("content", encoding="utf-8")
    assert fs.md_exists(path) is True


def test_md_exists_false_for_missing_file(tmp_workspace):
    path = tmp_workspace / "missing.md"
    assert fs.md_exists(path) is False
