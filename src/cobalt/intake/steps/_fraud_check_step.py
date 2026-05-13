"""Fraud check step — evaluates invoice and shell-company signals."""

from cobalt.intake.steps import StepResult
from cobalt.models.schemas.signal_profile_schema import SignalProfile

_RISK_LEVELS = [
    (0.80, "CRITICAL"),
    (0.60, "HIGH"),
    (0.30, "MEDIUM"),
    (0.00, "LOW"),
]

_ACTIONS = {"LOW": "PROCEED", "MEDIUM": "PROCEED", "HIGH": "TRIAGE", "CRITICAL": "BLOCK"}


def _risk_level(score: float) -> str:
    for threshold, level in _RISK_LEVELS:
        if score >= threshold:
            return level
    return "LOW"


def _make_result(step: str, signals: list[str], score: float) -> StepResult:
    level = _risk_level(score)
    action = _ACTIONS[level]
    critical = level == "CRITICAL"
    return StepResult(
        step=step,
        success=True,
        early_exit=critical,
        exit_status="BLOCKED" if critical else None,
        data={
            "fraud_signals": signals,
            "risk_score": round(score, 4),
            "risk_level": level,
            "recommended_action": action,
        },
        note="",
    )


def _web_empty(web_result: StepResult | None) -> bool:
    if web_result is None:
        return True
    return not web_result.data.get("research_text")


def run_basic(
    profile: SignalProfile,
    web_result: StepResult | None = None,
) -> StepResult:
    signals: list[str] = []
    score = 0.0
    spend = profile.erp_signal.spend

    # SHELL_NO_WEB_PRESENCE
    if _web_empty(web_result) and spend is not None and spend > 10000:
        signals.append("SHELL_NO_WEB_PRESENCE")
        score += 0.30

    # INVOICE_ROUND_NUMBERS
    if "ROUND_NUMBERS" in profile.ap_signal.flags:
        signals.append("INVOICE_ROUND_NUMBERS")
        score += 0.15

    # INVOICE_THRESHOLD_AVOIDANCE
    if "THRESHOLD_AVOIDANCE" in profile.ap_signal.flags:
        signals.append("INVOICE_THRESHOLD_AVOIDANCE")
        score += 0.25

    # INVOICE_SINGLE_APPROVER
    if profile.ap_signal.single_approver and spend is not None and spend > 25000:
        signals.append("INVOICE_SINGLE_APPROVER")
        score += 0.15

    return _make_result("FRAUD_CHECK_BASIC", signals, score)


def run_deep(
    profile: SignalProfile,
    web_result: StepResult | None = None,
) -> StepResult:
    signals: list[str] = []
    score = 0.0
    spend = profile.erp_signal.spend

    # All basic signals
    if _web_empty(web_result) and spend is not None and spend > 10000:
        signals.append("SHELL_NO_WEB_PRESENCE")
        score += 0.30

    if "ROUND_NUMBERS" in profile.ap_signal.flags:
        signals.append("INVOICE_ROUND_NUMBERS")
        score += 0.15

    if "THRESHOLD_AVOIDANCE" in profile.ap_signal.flags:
        signals.append("INVOICE_THRESHOLD_AVOIDANCE")
        score += 0.25

    if profile.ap_signal.single_approver and spend is not None and spend > 25000:
        signals.append("INVOICE_SINGLE_APPROVER")
        score += 0.15

    # VENDOR_SHELL_INDICATORS — 3+ of the four shell indicators
    shell_indicators = [
        _web_empty(web_result) and spend is not None and spend > 10000,
        profile.ap_signal.single_approver,
        "ROUND_NUMBERS" in profile.ap_signal.flags,
        "THRESHOLD_AVOIDANCE" in profile.ap_signal.flags,
    ]
    if sum(shell_indicators) >= 3:
        signals.append("VENDOR_SHELL_INDICATORS")
        score += 0.50

    return _make_result("FRAUD_CHECK_DEEP", signals, score)
