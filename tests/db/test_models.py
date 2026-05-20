"""Tests for DB model column definitions — no live DB connection required.

Verifies that ORM model class definitions contain the expected column names.
__table__.columns is part of the SQLAlchemy ORM metadata and requires no session.
"""

from cobalt.db.models import VendorIntelligence


def test_vendor_intelligence_has_an_columns():
    cols = [c.name for c in VendorIntelligence.__table__.columns]
    assert "cri_score" in cols
    assert "health_band" in cols
    assert "vendor_state" in cols
    assert "last_analysed_at" in cols


def test_vendor_intelligence_has_p3_columns():
    # P3 columns use explicit PascalCase DB column names (SQL Server convention)
    cols = [c.name for c in VendorIntelligence.__table__.columns]
    assert "RsLastUpdated" in cols
    assert "SpendTotalUsd" in cols
    assert "DependencyTier" in cols
    assert "RelationshipType" in cols


def test_vendor_intelligence_p4_columns_are_nullable():
    col_map = {c.name: c for c in VendorIntelligence.__table__.columns}
    for col_name in ("cri_score", "health_band", "vendor_state", "last_analysed_at"):
        assert col_map[col_name].nullable is True, f"{col_name} should be nullable"
