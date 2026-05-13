"""Investigation plan schema — output of Planning Agent Level 2."""

from dataclasses import dataclass, field
from enum import Enum


class InvestigationDepth(str, Enum):
    SKIP = "SKIP"
    FAST = "FAST"
    STANDARD = "STANDARD"
    DEEP = "DEEP"
    INTENSIVE = "INTENSIVE"
    TRIAGE = "TRIAGE"


class FraudRisk(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass
class InvestigationPlan:
    depth: InvestigationDepth
    steps: list[str]
    require_human_gate: bool
    require_legal_gate: bool
    fraud_risk: FraudRisk
    resolving_question: str | None
    reason: str
    # LLM-enriched fields — populated by _enrich_with_llm in write_investigation_plan
    step_rationale: dict = field(default_factory=dict)   # step_name → why + what to look for
    focus: str = ""                                       # single key question this investigation answers
