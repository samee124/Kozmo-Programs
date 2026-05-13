"""Tests for cobalt.brain.loader — including the new acquisition_map and brand_map fields."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from cobalt.brain.loader import BrainData, invalidate_cache, load_brain


@pytest.fixture(autouse=True)
def _clean_brain_cache():
    invalidate_cache()
    yield
    invalidate_cache()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_VENDORS = json.dumps({
    "acme corp": {
        "canonical_name": "Acme Corp",
        "confidence": 0.95,
        "country_code": "US",
        "category": "Technology",
    }
})

_ACQUISITION_MAP = json.dumps({
    "citrix": {
        "acquired_by": "Cloud Software Group",
        "acquired_by_key": "cloud software group",
        "date": "2022-09-30",
        "status": "COMPLETED",
    },
    "instagram": {
        "acquired_by": "Meta Platforms Inc",
        "acquired_by_key": "meta platforms inc",
        "date": "2012-09-06",
        "status": "COMPLETED",
    },
})

_BRAND_MAP = json.dumps({
    "instagram": "meta platforms inc",
    "slack": "salesforce inc",
    "github": "microsoft",
})


def _write_minimal(root: Path) -> None:
    (root / "known_vendors.json").write_text(_MINIMAL_VENDORS, encoding="utf-8")
    (root / "rebrand_map.json").write_text("{}", encoding="utf-8")
    (root / "alias_map.json").write_text("{}", encoding="utf-8")


def _write_full(root: Path) -> None:
    _write_minimal(root)
    (root / "acquisition_map.json").write_text(_ACQUISITION_MAP, encoding="utf-8")
    (root / "brand_map.json").write_text(_BRAND_MAP, encoding="utf-8")


# ---------------------------------------------------------------------------
# Test 1 — BrainData has acquisition_map and brand_map fields after load
# ---------------------------------------------------------------------------

def test_brain_data_has_new_fields(tmp_path, monkeypatch):
    _write_full(tmp_path)
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path))
    invalidate_cache()
    brain = load_brain()
    assert hasattr(brain, "acquisition_map")
    assert hasattr(brain, "brand_map")
    assert isinstance(brain.acquisition_map, dict)
    assert isinstance(brain.brand_map, dict)


# ---------------------------------------------------------------------------
# Test 2 — All 5 files present → acquisition_map and brand_map populated
# ---------------------------------------------------------------------------

def test_all_five_files_loaded(tmp_path, monkeypatch):
    _write_full(tmp_path)
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path))
    invalidate_cache()
    brain = load_brain()
    assert "instagram" in brain.brand_map
    assert "citrix" in brain.acquisition_map
    assert brain.acquisition_map["citrix"]["acquired_by"] == "Cloud Software Group"


# ---------------------------------------------------------------------------
# Test 3 — acquisition_map.json missing → empty dict + warning logged
# ---------------------------------------------------------------------------

def test_missing_acquisition_map_loads_with_warning(tmp_path, monkeypatch, caplog):
    _write_minimal(tmp_path)
    (tmp_path / "brand_map.json").write_text(_BRAND_MAP, encoding="utf-8")
    # acquisition_map.json intentionally absent
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path))
    invalidate_cache()

    with caplog.at_level(logging.WARNING, logger="cobalt.brain.loader"):
        brain = load_brain()

    assert brain.acquisition_map == {}
    assert any("acquisition_map" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Test 4 — brand_map.json missing → empty dict + warning logged
# ---------------------------------------------------------------------------

def test_missing_brand_map_loads_with_warning(tmp_path, monkeypatch, caplog):
    _write_minimal(tmp_path)
    (tmp_path / "acquisition_map.json").write_text(_ACQUISITION_MAP, encoding="utf-8")
    # brand_map.json intentionally absent
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path))
    invalidate_cache()

    with caplog.at_level(logging.WARNING, logger="cobalt.brain.loader"):
        brain = load_brain()

    assert brain.brand_map == {}
    assert any("brand_map" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Test 5 — Brand lookup: brand_map["instagram"] == "meta platforms inc"
# ---------------------------------------------------------------------------

def test_brand_lookup(tmp_path, monkeypatch):
    _write_full(tmp_path)
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path))
    invalidate_cache()
    brain = load_brain()
    assert brain.brand_map["instagram"] == "meta platforms inc"
    assert brain.brand_map["slack"] == "salesforce inc"
