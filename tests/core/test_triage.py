"""Tests for cobalt.core.triage — generate_triage_tasks() and build_triage_task()."""

from datetime import date, timedelta

from cobalt.core.triage import build_triage_task, generate_triage_tasks

_TODAY = date.today().isoformat()
_DUE_7 = (date.today() + timedelta(days=7)).isoformat()


def _blocking_gap(description: str = "Missing contract coverage") -> dict:
    return {"severity": "BLOCKING", "description": description, "suggested_action": "Upload contract"}


def _enrichment_gap(description: str = "Website URL unknown") -> dict:
    return {"severity": "ENRICHMENT", "description": description, "suggested_action": "Search web"}


# ---------------------------------------------------------------------------
# generate_triage_tasks
# ---------------------------------------------------------------------------

def test_one_blocking_gap_produces_one_task():
    tasks = generate_triage_tasks(
        gaps=[_blocking_gap()],
        flags=[],
        vendor_id="v-001",
        programme_id="prog-1",
    )
    assert len(tasks) == 1
    task = tasks[0]
    assert task["severity"] == "BLOCKING"
    assert task["due_date"] == _DUE_7
    assert task["vendor_id"] == "v-001"
    assert task["programme_id"] == "prog-1"


def test_enrichment_gap_returns_empty():
    tasks = generate_triage_tasks(
        gaps=[_enrichment_gap()],
        flags=[],
        vendor_id="v-001",
        programme_id="prog-1",
    )
    assert tasks == []


def test_mixed_gaps_only_blocking_returned():
    gaps = [_blocking_gap(), _enrichment_gap(), _blocking_gap("Another contract issue")]
    tasks = generate_triage_tasks(
        gaps=gaps,
        flags=[],
        vendor_id="v-001",
        programme_id="prog-1",
    )
    assert len(tasks) == 2
    for task in tasks:
        assert task["severity"] == "BLOCKING"


def test_empty_gaps_returns_empty():
    tasks = generate_triage_tasks(gaps=[], flags=[], vendor_id="v-001", programme_id="prog-1")
    assert tasks == []


def test_contract_keyword_infers_contract_owner():
    gap = {"severity": "BLOCKING", "description": "Missing contract document", "suggested_action": "Upload"}
    tasks = generate_triage_tasks(gaps=[gap], flags=[], vendor_id="v-001", programme_id="prog-1")
    assert tasks[0]["recommended_owner"] == "contract_owner"


def test_renewal_keyword_infers_contract_owner():
    gap = {"severity": "BLOCKING", "description": "Renewal date unknown", "suggested_action": "Check"}
    tasks = generate_triage_tasks(gaps=[gap], flags=[], vendor_id="v-001", programme_id="prog-1")
    assert tasks[0]["recommended_owner"] == "contract_owner"


def test_compliance_keyword_infers_compliance_owner():
    gap = {"severity": "BLOCKING", "description": "Missing compliance certificate", "suggested_action": "Obtain cert"}
    tasks = generate_triage_tasks(gaps=[gap], flags=[], vendor_id="v-001", programme_id="prog-1")
    assert tasks[0]["recommended_owner"] == "compliance_owner"


def test_certificate_keyword_infers_compliance_owner():
    gap = {"severity": "BLOCKING", "description": "ISO certificate expired", "suggested_action": "Renew"}
    tasks = generate_triage_tasks(gaps=[gap], flags=[], vendor_id="v-001", programme_id="prog-1")
    assert tasks[0]["recommended_owner"] == "compliance_owner"


def test_spend_keyword_infers_finance_owner():
    gap = {"severity": "BLOCKING", "description": "Spend data missing for Q3", "suggested_action": "Export"}
    tasks = generate_triage_tasks(gaps=[gap], flags=[], vendor_id="v-001", programme_id="prog-1")
    assert tasks[0]["recommended_owner"] == "finance_owner"


def test_invoice_keyword_infers_finance_owner():
    gap = {"severity": "BLOCKING", "description": "Invoice records incomplete", "suggested_action": "Reconcile"}
    tasks = generate_triage_tasks(gaps=[gap], flags=[], vendor_id="v-001", programme_id="prog-1")
    assert tasks[0]["recommended_owner"] == "finance_owner"


def test_no_keyword_uses_default_owner():
    gap = {"severity": "BLOCKING", "description": "Unknown issue", "suggested_action": "Investigate"}
    tasks = generate_triage_tasks(
        gaps=[gap], flags=[], vendor_id="v-001", programme_id="prog-1",
        default_owner="procurement_owner",
    )
    assert tasks[0]["recommended_owner"] == "procurement_owner"


def test_no_keyword_no_default_falls_back_to_vendor_owner():
    gap = {"severity": "BLOCKING", "description": "Unknown issue", "suggested_action": "Investigate"}
    tasks = generate_triage_tasks(gaps=[gap], flags=[], vendor_id="v-001", programme_id="prog-1")
    assert tasks[0]["recommended_owner"] == "vendor_owner"


def test_due_date_uses_due_days_blocking():
    gap = _blocking_gap()
    tasks = generate_triage_tasks(
        gaps=[gap], flags=[], vendor_id="v-001", programme_id="prog-1",
        due_days_blocking=14,
    )
    expected = (date.today() + timedelta(days=14)).isoformat()
    assert tasks[0]["due_date"] == expected


# ---------------------------------------------------------------------------
# build_triage_task
# ---------------------------------------------------------------------------

def test_build_triage_task_all_required_keys():
    task = build_triage_task(
        triage_type="GAP_RESOLUTION",
        description="Missing contract",
        question="Upload contract?",
        severity="BLOCKING",
        vendor_id="v-001",
        programme_id="prog-1",
        due_date="2026-06-01",
        recommended_owner="contract_owner",
    )
    required_keys = {
        "triage_type", "description", "question", "severity",
        "vendor_id", "programme_id", "due_date", "recommended_owner",
    }
    assert required_keys.issubset(task.keys())


def test_build_triage_task_values_preserved():
    task = build_triage_task(
        triage_type="GAP_RESOLUTION",
        description="desc",
        question="q",
        severity="BLOCKING",
        vendor_id="v-xyz",
        programme_id="p-abc",
        due_date="2026-07-01",
        recommended_owner="finance_owner",
    )
    assert task["triage_type"] == "GAP_RESOLUTION"
    assert task["description"] == "desc"
    assert task["question"] == "q"
    assert task["severity"] == "BLOCKING"
    assert task["vendor_id"] == "v-xyz"
    assert task["programme_id"] == "p-abc"
    assert task["due_date"] == "2026-07-01"
    assert task["recommended_owner"] == "finance_owner"


def test_build_triage_task_owner_none_allowed():
    task = build_triage_task(
        triage_type="GAP_RESOLUTION",
        description="d",
        question="q",
        severity="BLOCKING",
        vendor_id="v-001",
        programme_id="p-001",
        due_date="2026-06-01",
        recommended_owner=None,
    )
    assert task["recommended_owner"] is None
