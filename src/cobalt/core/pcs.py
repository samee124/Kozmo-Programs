"""P4 PCS contribution calculator.

Computes the Process 4 contribution to the Profile Confidence Score.
Maximum P4 contribution: 0.10. No LLM. No file I/O. Pure calculation.
"""

from __future__ import annotations


def compute_pcs(
    pcs_before: float,
    flags: list[str],
    process: str = "P4",
) -> tuple[float, float]:
    """Compute P4 PCS contribution and updated total.

    P4 maximum contribution: 0.10

    Components (additive):
      +0.05  if "CRI_COMPUTED" in flags
      +0.03  if "FINDINGS_DETECTED" in flags
      +0.02  if "ALL_DIMS_SCORED" in flags

    Returns: (pcs_contribution, pcs_total)
    pcs_total = min(1.0, pcs_before + pcs_contribution)
    """
    contribution = 0.0
    if "CRI_COMPUTED" in flags:
        contribution += 0.05
    if "FINDINGS_DETECTED" in flags:
        contribution += 0.03
    if "ALL_DIMS_SCORED" in flags:
        contribution += 0.02

    pcs_total = min(1.0, pcs_before + contribution)
    return contribution, pcs_total
