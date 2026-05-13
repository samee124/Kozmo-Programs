"""Tests for cobalt.db.models."""

from cobalt.db.models import VendorIntelligence, ProgrammeRun, VendorCheckin, TriageItem


def test_vendor_intelligence_instantiates():
    v = VendorIntelligence(
        vendor_id="v001",
        programme_id="p001",
        vendor_name="Acme Ltd",
        input_name="acme ltd",
        status="NEEDS_ACTION",
    )
    assert v.vendor_id == "v001"


def test_vendor_intelligence_repr_contains_vendor_id():
    v = VendorIntelligence(vendor_id="v001", programme_id="p", vendor_name="x", input_name="x", status="NEEDS_ACTION")
    assert "v001" in repr(v)


def test_vendor_intelligence_default_pcs_score():
    v = VendorIntelligence(vendor_id="v002", programme_id="p", vendor_name="x", input_name="x", status="NEEDS_ACTION")
    assert v.pcs_score == 0


def test_vendor_intelligence_default_data_class():
    v = VendorIntelligence(vendor_id="v003", programme_id="p", vendor_name="x", input_name="x", status="NEEDS_ACTION")
    assert v.data_class == "CLASS_D"


def test_programme_run_instantiates():
    p = ProgrammeRun(programme_id="p001", programme_name="Test Programme")
    assert p.programme_id == "p001"


def test_programme_run_repr_contains_programme_id():
    p = ProgrammeRun(programme_id="p001")
    assert "p001" in repr(p)


def test_vendor_checkin_instantiates():
    c = VendorCheckin(checkin_id="ci001", vendor_id="v001", status="SENT")
    assert c.checkin_id == "ci001"


def test_vendor_checkin_repr_contains_checkin_id():
    c = VendorCheckin(checkin_id="ci001")
    assert "ci001" in repr(c)


def test_triage_item_instantiates():
    t = TriageItem(triage_id="t001", programme_id="p001", raw_input="ambiguous name")
    assert t.triage_id == "t001"


def test_triage_item_repr_contains_triage_id():
    t = TriageItem(triage_id="t001")
    assert "t001" in repr(t)


def test_triage_item_default_status():
    t = TriageItem(triage_id="t002")
    assert t.status == "PENDING"
