# an_db_additions

## Overview

**Files to modify:**
- `src/cobalt/db/models.py`
- `src/cobalt/core/exceptions.py`

Then run: `alembic revision --autogenerate -m "add_an_columns"`
Then run: `alembic upgrade head`

---

## 1. src/cobalt/db/models.py — add to VendorIntelligence

Add these 4 columns after the existing RS columns (rs_last_updated, spend_total_usd, etc.):

```python
    # P4 Analysis columns
    cri_score:          Mapped[int | None]      = mapped_column(Integer, nullable=True)
    # CRI 0-100. Stored as int. Computed by scoring_engine, synced by analysis_orchestrator.

    health_band:        Mapped[str | None]      = mapped_column(String(20), nullable=True)
    # HEALTHY / WATCH / AT_RISK / CRITICAL

    vendor_state:       Mapped[str | None]      = mapped_column(String(20), nullable=True)
    # HEALTHY / WATCH / AT_RISK / CRITICAL / UNKNOWN / ARCHIVED
    # Set by state_classifier.classify_vendor_state() in analysis_orchestrator.

    last_analysed_at:   Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # UTC timestamp of last successful P4 run.
```

No FK constraints. No indexes needed in V1 (queries filter by vendor_id which is already indexed).

---

## 2. src/cobalt/core/exceptions.py — add after existing RS exceptions

```python
class ANSchemaError(Exception):
    """Raised when an AN schema dataclass receives an invalid value in __post_init__.
    Mirrors RSSchemaError pattern exactly.
    """


class ANOrchestrationError(Exception):
    """Raised when the analysis orchestrator encounters an unrecoverable configuration
    or infrastructure error. run_analysis catches this and returns a FAILED ANRunResult
    rather than propagating.
    """


class EvidenceValidationError(Exception):
    """Raised when evidence_validator cannot process its inputs due to corrupt or
    incompatible data structures. The orchestrator catches this, logs WARNING, and
    returns an ANRunResult with status=FAILED.
    """
```

---

## 3. Alembic migration

After modifying models.py, run from project root:
```bash
alembic revision --autogenerate -m "add_an_columns"
alembic upgrade head
```

Verify the generated migration adds:
- `cri_score INTEGER` nullable
- `health_band VARCHAR(20)` nullable
- `vendor_state VARCHAR(20)` nullable
- `last_analysed_at DATETIME` nullable

Do NOT modify existing columns. Do NOT add NOT NULL constraints.

---

## Tests

Run after migration:
```bash
python -m pytest tests/ -q --tb=short
```

Must stay at existing passing count + newly added AN tests.

Verify DB columns exist:
```python
# In tests/db/test_models.py (add to existing test file, do not replace)
def test_vendor_intelligence_has_an_columns(session):
    from cobalt.db.models import VendorIntelligence
    cols = [c.name for c in VendorIntelligence.__table__.columns]
    assert "cri_score" in cols
    assert "health_band" in cols
    assert "vendor_state" in cols
    assert "last_analysed_at" in cols
```
