# Fraud Detection Specification

## Location
src/Cobalt/intake/steps/fraud_check_step.py

## run_basic(profile, web_result) → StepResult
Signals checked:
  SHELL_NO_WEB_PRESENCE:       weight=0.30
    web empty AND spend > 10000
  INVOICE_ROUND_NUMBERS:       weight=0.15
    ROUND_NUMBERS in ap_signal.flags
  INVOICE_THRESHOLD_AVOIDANCE: weight=0.25
    THRESHOLD_AVOIDANCE in ap_signal.flags
  INVOICE_SINGLE_APPROVER:     weight=0.15
    single_approver=True AND spend > 25000

risk_score = sum of weights
LOW < 0.30, MEDIUM < 0.60, HIGH < 0.80, CRITICAL >= 0.80
CRITICAL → early_exit=True, exit_status=BLOCKED

## run_deep(profile, web_result) → StepResult
All basic signals PLUS:
  VENDOR_SHELL_INDICATORS:     weight=0.50
    3+ of: no web, single approver, round numbers,
    threshold avoidance, recent registration
