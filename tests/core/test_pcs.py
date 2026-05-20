"""Tests for cobalt.core.pcs — compute_pcs()."""

from cobalt.core.pcs import compute_pcs


def test_all_three_flags_contribution():
    flags = ["CRI_COMPUTED", "FINDINGS_DETECTED", "ALL_DIMS_SCORED"]
    contribution, total = compute_pcs(0.5, flags)
    assert contribution == 0.10
    assert total == 0.60


def test_only_cri_computed():
    contribution, total = compute_pcs(0.5, ["CRI_COMPUTED"])
    assert contribution == 0.05
    assert total == 0.55


def test_only_findings_detected():
    contribution, total = compute_pcs(0.5, ["FINDINGS_DETECTED"])
    assert contribution == 0.03
    assert total == 0.53


def test_only_all_dims_scored():
    contribution, total = compute_pcs(0.5, ["ALL_DIMS_SCORED"])
    assert contribution == 0.02
    assert total == 0.52


def test_no_flags_zero_contribution():
    contribution, total = compute_pcs(0.5, [])
    assert contribution == 0.0
    assert total == 0.5


def test_pcs_clamped_at_one():
    # pcs_before=0.95, all flags → contribution=0.10, total clamped to 1.0
    flags = ["CRI_COMPUTED", "FINDINGS_DETECTED", "ALL_DIMS_SCORED"]
    contribution, total = compute_pcs(0.95, flags)
    assert contribution == 0.10
    assert total == 1.0


def test_pcs_before_zero_all_flags():
    flags = ["CRI_COMPUTED", "FINDINGS_DETECTED", "ALL_DIMS_SCORED"]
    contribution, total = compute_pcs(0.0, flags)
    assert contribution == 0.10
    assert abs(total - 0.10) < 1e-9


def test_pcs_before_zero_no_flags():
    contribution, total = compute_pcs(0.0, [])
    assert contribution == 0.0
    assert total == 0.0


def test_unknown_flags_ignored():
    # Flags not in the spec contribute 0
    contribution, total = compute_pcs(0.4, ["SOME_UNKNOWN_FLAG"])
    assert contribution == 0.0
    assert total == 0.4


def test_process_param_accepted():
    # process parameter is accepted without error (reserved for future use)
    contribution, total = compute_pcs(0.3, ["CRI_COMPUTED"], process="P4")
    assert contribution == 0.05
    assert abs(total - 0.35) < 1e-9
