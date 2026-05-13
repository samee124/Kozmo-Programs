"""Tests for cobalt.brain.loader."""

import json
import os
import pytest
from pathlib import Path

from cobalt.brain.loader import load_brain, invalidate_cache, BrainData, KnownVendor


@pytest.fixture(autouse=True)
def clear_cache():
    """Ensure a clean cache before and after every test."""
    invalidate_cache()
    yield
    invalidate_cache()


def test_load_brain_returns_brain_data():
    result = load_brain()
    assert isinstance(result, BrainData)
    assert isinstance(result.known_vendors, dict)
    assert isinstance(result.rebrand_map, dict)
    assert isinstance(result.alias_map, dict)


def test_ibm_in_known_vendors_with_confidence():
    brain = load_brain()
    assert "ibm" in brain.known_vendors
    vendor = brain.known_vendors["ibm"]
    assert isinstance(vendor, KnownVendor)
    assert vendor.confidence == 0.99


def test_blackboard_in_rebrand_map():
    brain = load_brain()
    assert "blackboard" in brain.rebrand_map


def test_aws_in_alias_map():
    brain = load_brain()
    assert "aws" in brain.alias_map


def test_load_brain_returns_same_object_on_second_call():
    first = load_brain()
    second = load_brain()
    assert first is second


def test_invalidate_cache_causes_reread(monkeypatch, tmp_path):
    # Write a minimal Brain with a different vendor
    brain_dir = tmp_path / "Brain"
    brain_dir.mkdir()
    (brain_dir / "known_vendors.json").write_text(
        json.dumps({"test_vendor": {"canonical_name": "Test Corp", "confidence": 0.5, "country_code": "GB", "category": "TEST"}}),
        encoding="utf-8",
    )
    (brain_dir / "rebrand_map.json").write_text("{}", encoding="utf-8")
    (brain_dir / "alias_map.json").write_text("{}", encoding="utf-8")

    monkeypatch.setenv("BRAIN_ROOT", str(brain_dir))

    first = load_brain()
    assert "test_vendor" in first.known_vendors

    invalidate_cache()
    second = load_brain()
    assert second is not first
    assert "test_vendor" in second.known_vendors


def test_missing_brain_file_raises_file_not_found(monkeypatch, tmp_path):
    empty_dir = tmp_path / "EmptyBrain"
    empty_dir.mkdir()
    monkeypatch.setenv("BRAIN_ROOT", str(empty_dir))

    with pytest.raises(FileNotFoundError):
        load_brain()
