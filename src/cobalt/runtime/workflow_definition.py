"""Runtime layer — WorkflowDefinition: durable executable plan."""

import ast
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from cobalt.core.atomic_write import atomic_write
from cobalt.core.exceptions import (
    InvalidConditionExpression,
    StepIdCollision,
    WorkflowParseError,
    WorkflowSaveError,
)


# ---------------------------------------------------------------------------
# Enums and simple dataclasses
# ---------------------------------------------------------------------------

class StepStatus(str, Enum):
    PENDING  = "PENDING"
    RUNNING  = "RUNNING"
    DONE     = "DONE"
    FAILED   = "FAILED"
    SKIPPED  = "SKIPPED"


@dataclass
class RetryPolicy:
    max_attempts:    int = 1
    backoff_seconds: int = 0


@dataclass
class WorkflowStep:
    step_id:            str
    step_type:          str
    status:             StepStatus
    depends_on:         list[str]
    condition:          str | None
    retry_policy:       RetryPolicy
    planning_rationale: str
    added_in_version:   int = 1


@dataclass
class ReplanEvent:
    version:        int
    triggered_by:   str
    trigger_reason: str
    rationale:      str
    steps_added:    list[str]
    steps_removed:  list[str]
    steps_modified: list[str]
    replanned_at:   str


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _step_to_dict(s: WorkflowStep) -> dict:
    return {
        "step_id":            s.step_id,
        "step_type":          s.step_type,
        "status":             s.status.value,
        "depends_on":         s.depends_on,
        "condition":          s.condition,
        "retry_policy":       {
            "max_attempts":    s.retry_policy.max_attempts,
            "backoff_seconds": s.retry_policy.backoff_seconds,
        },
        "planning_rationale": s.planning_rationale,
        "added_in_version":   s.added_in_version,
    }


def _step_from_dict(d: dict) -> WorkflowStep:
    rp = d.get("retry_policy") or {}
    return WorkflowStep(
        step_id=d["step_id"],
        step_type=d["step_type"],
        status=StepStatus(d["status"]),
        depends_on=d.get("depends_on") or [],
        condition=d.get("condition"),
        retry_policy=RetryPolicy(
            max_attempts=rp.get("max_attempts", 1),
            backoff_seconds=rp.get("backoff_seconds", 0),
        ),
        planning_rationale=d.get("planning_rationale", ""),
        added_in_version=d.get("added_in_version", 1),
    )


def _replan_to_dict(r: ReplanEvent) -> dict:
    return {
        "version":        r.version,
        "triggered_by":   r.triggered_by,
        "trigger_reason": r.trigger_reason,
        "rationale":      r.rationale,
        "steps_added":    r.steps_added,
        "steps_removed":  r.steps_removed,
        "steps_modified": r.steps_modified,
        "replanned_at":   r.replanned_at,
    }


def _replan_from_dict(d: dict) -> ReplanEvent:
    return ReplanEvent(
        version=d["version"],
        triggered_by=d["triggered_by"],
        trigger_reason=d["trigger_reason"],
        rationale=d["rationale"],
        steps_added=d.get("steps_added") or [],
        steps_removed=d.get("steps_removed") or [],
        steps_modified=d.get("steps_modified") or [],
        replanned_at=d["replanned_at"],
    )


# ---------------------------------------------------------------------------
# Condition evaluator
# ---------------------------------------------------------------------------

_ALLOWED_NODES = frozenset({
    ast.Expression,
    ast.BoolOp, ast.And, ast.Or,
    ast.UnaryOp, ast.Not,
    ast.Compare,
    ast.Attribute,
    ast.Name,
    ast.Constant,
    ast.Eq, ast.NotEq, ast.Gt, ast.Lt, ast.GtE, ast.LtE,
    ast.Load,  # context node attached to Name/Attribute — not executable by itself
})

_MISSING = object()


def _preprocess_condition(condition: str) -> str:
    """Translate JSON-style literals to Python equivalents."""
    condition = re.sub(r'\btrue\b', 'True', condition)
    condition = re.sub(r'\bfalse\b', 'False', condition)
    condition = re.sub(r'\bnull\b', 'None', condition)
    return condition


def _validate_ast_nodes(node: ast.AST) -> None:
    if type(node) not in _ALLOWED_NODES:
        raise InvalidConditionExpression(
            f"Disallowed expression type: {type(node).__name__}"
        )
    for child in ast.iter_child_nodes(node):
        _validate_ast_nodes(child)


def _eval_node(node: ast.AST, step_results: dict) -> object:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, step_results)

    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        mapping = {"True": True, "False": False, "None": None}
        if node.id in mapping:
            return mapping[node.id]
        # Bare step_id reference without field — treat as missing.
        return _MISSING

    if isinstance(node, ast.Attribute):
        if not isinstance(node.value, ast.Name):
            raise InvalidConditionExpression("Only step_id.field references are allowed")
        step_id = node.value.id
        field_name = node.attr
        if step_id not in step_results:
            return _MISSING
        result = step_results[step_id]
        if field_name not in result:
            return _MISSING
        return result[field_name]

    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            val = _eval_node(node.operand, step_results)
            if val is _MISSING:
                val = False
            return not val
        raise InvalidConditionExpression(f"Unsupported unary op: {type(node.op).__name__}")

    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            for val_node in node.values:
                result = _eval_node(val_node, step_results)
                if result is _MISSING:
                    result = False
                if not result:
                    return False
            return True
        if isinstance(node.op, ast.Or):
            for val_node in node.values:
                result = _eval_node(val_node, step_results)
                if result is _MISSING:
                    result = False
                if result:
                    return True
            return False
        raise InvalidConditionExpression(f"Unsupported bool op: {type(node.op).__name__}")

    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, step_results)
        if left is _MISSING:
            return False
        for op, comparator_node in zip(node.ops, node.comparators):
            right = _eval_node(comparator_node, step_results)
            if right is _MISSING:
                return False
            try:
                if isinstance(op, ast.Eq):
                    result = (left == right)
                elif isinstance(op, ast.NotEq):
                    result = (left != right)
                elif isinstance(op, ast.Gt):
                    result = (left > right)
                elif isinstance(op, ast.Lt):
                    result = (left < right)
                elif isinstance(op, ast.GtE):
                    result = (left >= right)
                elif isinstance(op, ast.LtE):
                    result = (left <= right)
                else:
                    raise InvalidConditionExpression(
                        f"Unsupported operator: {type(op).__name__}"
                    )
                if not result:
                    return False
                left = right
            except TypeError:
                return False  # incompatible types (e.g. None >= 0.4)
        return True

    raise InvalidConditionExpression(f"Unexpected AST node: {type(node).__name__}")


def evaluate_condition(condition: str, step_results: dict[str, dict]) -> bool:
    """Evaluate a condition expression against step result data.

    step_results: map of step_id → result dict (from StepRunRecord.result).
    Returns False if any referenced step or field is missing.
    Raises InvalidConditionExpression for invalid syntax or disallowed constructs.
    """
    try:
        processed = _preprocess_condition(condition)
        tree = ast.parse(processed, mode="eval")
        _validate_ast_nodes(tree)
        result = _eval_node(tree, step_results)
        if result is _MISSING:
            return False
        return bool(result)
    except InvalidConditionExpression:
        raise
    except SyntaxError as exc:
        raise InvalidConditionExpression(f"Syntax error in condition: {exc}") from exc
    except Exception as exc:
        raise InvalidConditionExpression(f"Condition evaluation failed: {exc}") from exc


# ---------------------------------------------------------------------------
# WorkflowDefinition
# ---------------------------------------------------------------------------

def _workflow_path(programme_id: str, workflow_id: str) -> Path:
    root = Path(os.getenv("WORKSPACE_ROOT", "./workspace"))
    return root / programme_id / "workflows" / workflow_id / "workflow.json"


@dataclass
class WorkflowDefinition:
    workflow_id:        str
    programme_id:       str
    vendor_key:         str | None
    vendor_id:          str | None
    workflow_type:      str
    created_by:         str
    created_at:         str
    version:            int = 1
    replanning_count:   int = 0
    context:            dict = field(default_factory=dict)
    steps:              list[WorkflowStep] = field(default_factory=list)
    replanning_history: list[ReplanEvent] = field(default_factory=list)

    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "workflow_id":        self.workflow_id,
            "programme_id":       self.programme_id,
            "vendor_key":         self.vendor_key,
            "vendor_id":          self.vendor_id,
            "workflow_type":      self.workflow_type,
            "created_by":         self.created_by,
            "created_at":         self.created_at,
            "version":            self.version,
            "replanning_count":   self.replanning_count,
            "context":            self.context,
            "steps":              [_step_to_dict(s) for s in self.steps],
            "replanning_history": [_replan_to_dict(r) for r in self.replanning_history],
        }

    def save(self, path: Path | None = None) -> Path:
        if path is None:
            path = _workflow_path(self.programme_id, self.workflow_id)
        json_str = json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
        try:
            atomic_write(path, json_str)
        except Exception as exc:
            raise WorkflowSaveError(f"Failed to save workflow {self.workflow_id}: {exc}") from exc
        return path

    @classmethod
    def load(cls, workflow_id: str, programme_id: str) -> "WorkflowDefinition":
        path = _workflow_path(programme_id, workflow_id)
        if not path.exists():
            raise FileNotFoundError(f"workflow.json not found: {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise WorkflowParseError(f"Malformed JSON in workflow.json: {exc}") from exc
        try:
            return cls(
                workflow_id=raw["workflow_id"],
                programme_id=raw["programme_id"],
                vendor_key=raw.get("vendor_key"),
                vendor_id=raw.get("vendor_id"),
                workflow_type=raw["workflow_type"],
                created_by=raw["created_by"],
                created_at=raw["created_at"],
                version=raw.get("version", 1),
                replanning_count=raw.get("replanning_count", 0),
                context=raw.get("context") or {},
                steps=[_step_from_dict(s) for s in raw.get("steps") or []],
                replanning_history=[_replan_from_dict(r) for r in raw.get("replanning_history") or []],
            )
        except (KeyError, ValueError) as exc:
            raise WorkflowParseError(f"workflow.json schema mismatch: {exc}") from exc

    def apply_revision(
        self,
        revised_steps: list[WorkflowStep],
        triggered_by: str,
        trigger_reason: str,
        rationale: str,
    ) -> None:
        """Replace pending/running steps with revised_steps; preserve completed ones."""
        _TERMINAL = {StepStatus.DONE, StepStatus.FAILED, StepStatus.SKIPPED}
        completed_ids = {s.step_id for s in self.steps if s.status in _TERMINAL}

        for step in revised_steps:
            if step.step_id in completed_ids:
                raise StepIdCollision(
                    f"Step {step.step_id!r} already completed — cannot override via replan"
                )

        new_version = self.version + 1

        old_pending_ids = {
            s.step_id for s in self.steps if s.status not in _TERMINAL
        }
        new_ids = {s.step_id for s in revised_steps}
        steps_added    = sorted(new_ids - old_pending_ids)
        steps_removed  = sorted(old_pending_ids - new_ids)
        steps_modified = sorted(new_ids & old_pending_ids)

        for step in revised_steps:
            step.added_in_version = new_version

        preserved = [s for s in self.steps if s.status in _TERMINAL]
        self.steps = preserved + revised_steps
        self.version = new_version
        self.replanning_count += 1
        self.replanning_history.append(ReplanEvent(
            version=new_version,
            triggered_by=triggered_by,
            trigger_reason=trigger_reason,
            rationale=rationale,
            steps_added=steps_added,
            steps_removed=steps_removed,
            steps_modified=steps_modified,
            replanned_at=datetime.now(tz=timezone.utc).isoformat(),
        ))
